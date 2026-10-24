/**
 * @local/pawbot/bili-watch —— 讓大肥魚真的去看 B 站影片。
 * ============================================================================
 * 它只做兩件事：「看」與「寫報告」。
 *   1. 消費 ROOT/requests/*.json（PawBot 寫入），處理完移到 requests/done/。
 *   2. 讀 B 站影片資訊 / 字幕 / 熱門評論（純 Node fetch + WBI 簽名），
 *      用一次 ctx.llm 單次呼叫把內容消化成 JSON，程式補齊 metadata、決定 watched_level，
 *      原子寫入 ROOT/reports/<bvid>.json，並維護 ROOT/reports/index.json。
 * 它不發任何 QQ / B 站消息、不發評論；怎麼在群裡說話是 PawBot 表達層的事。
 * 憑證（B 站 SESSDATA）優先序 env > ROOT/credential.json > 設置頁 sessdata，
 * 絕不寫進日誌、報告、工具回傳。
 * ============================================================================
 */
import { loadHostDeps } from './lib/hostdeps.js'

const hostDeps = await loadHostDeps()
const z = hostDeps.z
const defineTool = hostDeps.defineTool

import { mkdirSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { buildReport, reportBrief, truncate } from './lib/report.js'
import { atomicWriteFile, readJson, writeJson } from './lib/atomic.js'
import { scanRequests, scanRequestsDetailed, quarantineBad, normalizeExtra } from './lib/queue.js'
import { resolveCredential, credentialStatus } from './lib/credential.js'
import { readSeen, markSeen, shouldMarkSkipped } from './lib/seen.js'
import { filterFresh, poolTargetFrom } from './lib/pool.js'
import { buildNoteMd } from './lib/notes.js'
import { guardModel } from './lib/model-guard.js'
import { dedupeInflight } from './lib/inflight.js'
import { settleRequest } from './lib/request-lifecycle.js'
import { withTimeoutSignal, LLM_TIMEOUT_MS } from './lib/http.js'
import * as bili from './lib/bili.js'

export const name = 'pawbot-bili-watch'
export const inject = ['tools', 'llm']

/* ── Config（schemastery 拿不到就 undefined，設置頁降級、本體照常跑） ── */
export const Config = (() => {
  if (!z) return undefined
  try {
    return z.object({
      /** LLM provider（單次呼叫） */
      provider: z.string().default('deepseek-official').extra('volatile', true),
      /** LLM model。⛔ 白名單只有 deepseek-flash：任何不在白名單的寫法都會被 allowedModel 取代 */
      model: z.string().default('deepseek-flash').extra('volatile', true),
      /** 被擋掉（或留空）時實際使用的模型；本身也必須在白名單內，否則退回 deepseek-flash */
      allowedModel: z.string().default('deepseek-flash').extra('volatile', true),
      /** 共享目錄 ROOT（與 PawBot 的契約路徑） */
      dataRoot: z.string().default('D:/WorkBuddy/pawbot-bili-watch-data').extra('volatile', true),
      /** 從哪個環境變數讀 SESSDATA（絕不寫進日誌/報告） */
      cookieEnv: z.string().default('BILI_SESSDATA').extra('volatile', true),
      /** 設置頁欄位 SESSDATA（最低優先，標 secret 不落日誌/不落遠端設定回讀） */
      sessdata: z.string().default('').role('secret').extra('volatile', true),
      /** 搜尋排序 order：totalrank/click/pubdate/dm/stow/scores */
      searchOrder: z.string().default('totalrank').extra('volatile', true),
      /** 搜尋日期過濾：只搜最近 N 天（0 = 不篩，交給 B 站篩） */
      searchDays: z.natural().default(0).extra('volatile', true),
      /** 搜尋目標條數（候選池目標） */
      searchNum: z.natural().default(20).extra('volatile', true),
      /** UP 主定向：先試投稿接口；被反爬擋（-352）就降級用「UP 名字搜尋 + 作者篩」 */
      upSearch: z.boolean().default(true).extra('volatile', true),
      /** 每條報告順手寫一份人讀 md 到 ROOT/notes/<bvid>.md */
      notesMd: z.boolean().default(true).extra('volatile', true),
      /** 字幕文字最多餵給模型幾個字 */
      maxSubtitleChars: z.natural().default(12000).extra('volatile', true),
      /** 超過這個時長（秒）的長片直接跳過 */
      maxDurationSec: z.natural().default(3600).extra('volatile', true),
      /** 請求佇列輪詢間隔（秒） */
      pollSec: z.natural().default(30).extra('volatile', true),
      /** 已有報告幾小時內不重看 */
      reportTtlHours: z.natural().default(72).extra('volatile', true),
      /** 自動備貨總開關 */
      autoStock: z.boolean().default(true).extra('volatile', true),
      /** 庫存目標：未分享且值得分享的報告少於這個數就自動補 */
      stockTarget: z.natural().default(3).extra('volatile', true),
      /** 自動備貨間隔（分鐘） */
      autoIntervalMin: z.natural().default(90).extra('volatile', true),
      /** 每日自動備貨上限（落盤在 state.json，重啟不重置） */
      dailyLimit: z.natural().default(12).extra('volatile', true),
      /** 自動備貨允許時段，`HH:MM-HH:MM`，可跨午夜；空陣列 = 全天 */
      autoWindows: z.array(z.string()).default(['12:00-14:00', '00:30-08:30']).extra('volatile', true),
      /** 單次 LLM 呼叫的最大輸出 tokens */
      maxTokens: z.natural().default(8192).extra('volatile', true),
    })
  } catch {
    return undefined
  }
})()

/* ── 人設 system prompt ── */
const SYSTEM_PROMPT = [
  '你是「大肥鱼」，一只住在网络上的小鱼：黏人、爱撒娇，从帮助别人里得到成就感。你现在真的去看了一个 B 站视频，要写一份内部报告交给搭档 PawBot（这份报告不是直接发在群里的，怎么说由 PawBot 决定）。',
  '',
  '规则（很重要）：',
  '1. 只能根据下面给你的「真实材料」来写 summary / highlights / comment_vibe / top_comments，不能脑补、不能假装。',
  '2. 有字幕时：summary 与 highlights 要基于字幕内容；highlights 可以带时间点，例如「03:12 提到…」。',
  '3. 没有字幕时：你没真的看到画面、没真的听到内容，summary 只能基于简介＋评论，并在 summary 开头如实说「没读到字幕，以下是靠简介和评论推测的」。这种时候 highlights 一律不要带时间点，也不要装成看过/听过。',
  '4. watched_level 由程序判断，你不需要输出、更不准自己编。',
  '5. take 是你自己的看法：一两句、口语、有观点；不要用「唔」开头；不要写成总结报告腔；不要重复 summary。',
  '6. 字幕里某个细节没讲清楚时，summary / highlights 就写「视频里没细说」，不要拿你已有的背景知识去补；没字幕时不要假装看过（同规则 3）。',
  '',
  '只输出一个 JSON 对象，不要解释、不要输出 JSON 以外的文字。格式：',
  '{"summary":"…","highlights":["…"],"comment_vibe":"…","top_comments":[{"text":"…","like":0}],"take":"…","share_worthy":true,"share_reason":"…"}',
].join('\n')

export function apply(ctx, config) {
  /* ── 配置：逐欄位按默認值類型挑一遍，防 apply 傳進壞值 ── */
  const DEFAULTS = {
    provider: 'deepseek-official',
    model: 'deepseek-flash',
    allowedModel: 'deepseek-flash',
    dataRoot: 'D:/WorkBuddy/pawbot-bili-watch-data',
    cookieEnv: 'BILI_SESSDATA',
    sessdata: '',
    searchOrder: 'totalrank',
    searchDays: 0,
    searchNum: 20,
    upSearch: true,
    notesMd: true,
    maxSubtitleChars: 12000,
    maxDurationSec: 3600,
    pollSec: 30,
    reportTtlHours: 72,
    autoStock: true,
    stockTarget: 3,
    autoIntervalMin: 90,
    dailyLimit: 12,
    autoWindows: ['12:00-14:00', '00:30-08:30'],
    maxTokens: 8192,
  }
  const normalize = (src, base) => {
    const out = { ...base }
    for (const k of Object.keys(base)) {
      const v = src?.[k]
      if (v === undefined || v === null) continue
      if (Array.isArray(base[k])) {
        if (Array.isArray(v)) out[k] = v.filter((x) => typeof x === 'string')
      } else if (typeof base[k] === 'number') {
        const n = Number(v)
        if (Number.isFinite(n)) out[k] = n
      } else if (typeof base[k] === 'boolean') {
        if (typeof v === 'boolean') out[k] = v
      } else if (typeof base[k] === 'string') {
        if (typeof v === 'string') out[k] = v
      }
    }
    return out
  }
  const cfg = normalize(config, DEFAULTS)
  const refreshCfg = () => {
    Object.assign(cfg, normalize(config, DEFAULTS))
  }

  /* ── 日誌：ctx.logger 優先，否則 console；絕不打 cookie ── */
  const log = (msg) => {
    const line = `[pawbot-bili-watch] ${msg}`
    try {
      if (ctx.logger?.info) ctx.logger.info(line)
      else console.log(line)
    } catch {
      console.log(line)
    }
  }
  for (const n of hostDeps.notes) log(n)

  /* ── 目錄 ── */
  const root = String(cfg.dataRoot || DEFAULTS.dataRoot)
  const requestsDir = join(root, 'requests')
  const doneDir = join(requestsDir, 'done')
  const failedDir = join(requestsDir, 'failed')
  const badDir = join(requestsDir, 'bad')
  const reportsDir = join(root, 'reports')
  const notesDir = join(root, 'notes')
  const indexPath = join(reportsDir, 'index.json')
  const sharedPath = join(root, 'shared.json')
  const statePath = join(root, 'state.json')
  const credentialPath = join(root, 'credential.json')
  const accountsPath = join(root, 'accounts.json')
  const ensureDirs = () => {
    for (const d of [requestsDir, doneDir, failedDir, badDir, reportsDir, notesDir]) {
      try {
        mkdirSync(d, { recursive: true })
      } catch {
        /* 建目錄失敗不致命 */
      }
    }
  }
  ensureDirs()

  /* ── 中斷 / 資源回收 ── */
  const active = new Set()
  let disposed = false
  const watchSignal = (externalSignal) => {
    const c = new AbortController()
    active.add(c)
    const onAbort = () => c.abort()
    if (externalSignal) {
      if (externalSignal.aborted) c.abort()
      else externalSignal.addEventListener('abort', onAbort, { once: true })
    }
    return {
      signal: c.signal,
      release: () => {
        if (externalSignal) externalSignal.removeEventListener('abort', onAbort)
        active.delete(c)
      },
    }
  }
  const abortAll = () => {
    for (const c of active) c.abort()
  }

  /* ── 憑證：env > ROOT/credential.json > 設置頁 sessdata（值絕不打出去） ── */
  const readCredentialState = () => {
    let fileCredential = null
    try {
      const obj = readJson(credentialPath, null)
      if (obj && typeof obj === 'object') fileCredential = obj
    } catch {
      /* 讀不到就當沒有 */
    }
    return resolveCredential({
      env: process.env,
      cookieEnv: String(cfg.cookieEnv || DEFAULTS.cookieEnv),
      fileCredential,
      configSessdata: String(cfg.sessdata ?? ''),
    })
  }
  const readCookie = () => {
    const { sessdata } = readCredentialState()
    if (!sessdata) return ''
    return 'SESSDATA=' + encodeURIComponent(String(sessdata))
  }

  /* ── 狀態 / 共享 / 報告索引 ── */
  const dayKey = (d = new Date()) => {
    const p = (n) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
  }
  const loadState = () => {
    const s = readJson(statePath, {}) || {}
    const today = dayKey()
    if (s.date !== today) return { date: today, watchedToday: 0 }
    return { date: today, watchedToday: Number(s.watchedToday) || 0 }
  }
  const dailyCount = () => loadState().watchedToday
  const incrementDailyCount = () => {
    const s = loadState()
    s.watchedToday += 1
    writeJson(statePath, s)
  }
  const readShared = () => {
    const s = readJson(sharedPath, {})
    return s && typeof s === 'object' ? s : {}
  }
  const readIndex = () => {
    const idx = readJson(indexPath, [])
    return Array.isArray(idx) ? idx : []
  }

  const hasRecentReport = (bvid) => {
    const report = readJson(join(reportsDir, bvid + '.json'), null)
    if (!report) return false
    const t = Date.parse(report.created_at || '')
    if (!Number.isFinite(t)) return false
    const ttl = Math.max(0, Number(cfg.reportTtlHours || 0)) * 3600 * 1000
    return Date.now() - t < ttl
  }

  const saveReport = (report) => {
    ensureDirs()
    atomicWriteFile(join(reportsDir, report.bvid + '.json'), JSON.stringify(report, null, 2))
    const idx = readIndex().filter((e) => e?.bvid !== report.bvid)
    idx.unshift({
      bvid: report.bvid,
      title: report.title,
      created_at: report.created_at,
      watched_level: report.watched_level,
    })
    atomicWriteFile(indexPath, JSON.stringify(idx.slice(0, 200), null, 2))
  }

  /* ── 進程內候選池（這一輪有效，跨輪判重靠 seen/shared/reports） ── */
  let candidatePool = null // Map<bvid, brief>
  const poolReset = () => {
    candidatePool = new Map()
  }
  const poolAdd = (briefs) => {
    if (!candidatePool) candidatePool = new Map()
    const seen = readSeen(root)
    const shared = readShared()
    const { fresh, skippedSeen } = filterFresh(briefs, {
      seen,
      shared,
      hasRecent: (bvid) => hasRecentReport(bvid),
      pool: candidatePool,
    })
    for (const b of fresh) candidatePool.set(b.bvid, b)
    const target = poolTargetFrom(cfg.searchNum)
    return {
      videos: fresh,
      skippedSeen,
      poolCount: candidatePool.size,
      poolTarget: target,
      poolEnough: candidatePool.size >= target,
    }
  }

  /* ── LLM 單次呼叫（非 agent） ── */
  const extractJsonObject = (text) => {
    const s = String(text || '').trim()
    if (!s) return null
    const start = s.indexOf('{')
    const end = s.lastIndexOf('}')
    if (start < 0 || end <= start) return null
    try {
      return JSON.parse(s.slice(start, end + 1))
    } catch {
      return null
    }
  }

  const buildUserPrompt = (meta, subtitleText, comments, availability) => {
    const lines = []
    lines.push('【视频信息】')
    lines.push(`标题：${meta.title}`)
    lines.push(`UP主：${meta.author}`)
    lines.push(`时长：${meta.durationSec} 秒`)
    lines.push(`播放：${meta.play}`)
    lines.push(`分区：${meta.tname}`)
    lines.push(`发布：${meta.pubdate || '未知'}`)
    if (availability.hasDesc && meta.desc) {
      lines.push('')
      lines.push('【简介】')
      lines.push(truncate(meta.desc, 2000))
    }
    lines.push('')
    if (availability.hasSubtitle && subtitleText) {
      lines.push('【字幕（带时间戳）】')
      lines.push(subtitleText)
    } else if (availability.subtitleState === 'error' && availability.hasCredential) {
      lines.push('【字幕】无（抓字幕时出错；你已登录，可能是凭证过期或风控，这个视频可能其实有字幕——但你没读到，按「没读到字幕」处理，别假装看过）')
    } else if (availability.subtitleState === 'error') {
      lines.push('【字幕】无（抓字幕时出错，别假装听过内容）')
    } else {
      lines.push('【字幕】无（这个视频没有字幕，别假装听过内容）')
    }
    lines.push('')
    if (comments.length) {
      lines.push('【热门评论】')
      for (const c of comments) lines.push(`- (赞 ${c.like}) ${c.text}`)
    } else {
      lines.push('【热门评论】无')
    }
    lines.push('')
    lines.push('请根据以上真实材料，只输出 JSON 报告。')
    return lines.join('\n')
  }

  // ⛔ 硬規則（用戶 2026-10-25 定）：不允許任何 V4 Pro 的調用，只能用 V4 Flash。
  //    白名單在 lib/model-guard.js（只允許 deepseek-flash）：設定頁就算被填成 v4-pro
  //    （或 gemini / claude / 任何非 flash 名字），也會被改回 flash 並在日誌留一行說明
  //    —— 不要靜默，否則以後查不到為什麼模型跟設定不一樣。
  const callLlm = async ({ meta, subtitleText, comments, availability, signal }) => {
    const provider = String(cfg.provider || DEFAULTS.provider)
    const guard = guardModel(cfg.model || DEFAULTS.model, cfg.allowedModel || DEFAULTS.allowedModel)
    if (guard.changed) {
      log(`模型「${guard.requested || '(空)'}」不在白名单 → 已改用 ${guard.model}（只允许 deepseek-flash）`)
    }
    const model = guard.model
    const user = buildUserPrompt(meta, subtitleText, comments, availability)
    for (let attempt = 1; attempt <= 2; attempt++) {
      // 舊 bug：被 abort 後還用同一個 aborted signal 立刻重試第二次，白等一輪
      if (signal?.aborted) {
        log(`LLM 已中止（${signal.reason?.message || 'aborted'}），不再重试`)
        return null
      }
      let text = ''
      // 每次嘗試各自 90 秒 timeout：stream hang 住也不會讓 busy 永遠 true
      const timed = withTimeoutSignal(signal, LLM_TIMEOUT_MS)
      try {
        const opts = {
          provider,
          model,
          system: SYSTEM_PROMPT,
          messages: [{ role: 'user', content: [{ type: 'text', text: user }] }],
          signal: timed.signal,
        }
        const mt = Number(cfg.maxTokens)
        if (mt > 0) opts.maxTokens = mt
        const stream = ctx.llm.stream(opts)
        for await (const chunk of stream) {
          if (chunk?.type === 'text-delta') text += chunk.text
          else if (chunk?.type === 'finish') {
            if (chunk.reason?.kind === 'error' || chunk.reason?.kind === 'aborted') {
              throw new Error(chunk.reason?.failure?.message || `llm ${chunk.reason?.kind}`)
            }
          }
        }
      } catch (e) {
        if (signal?.aborted) {
          log(`LLM 已中止（${e?.message}），不再重试`)
          return null
        }
        log(`LLM 调用失败（第 ${attempt} 次）：${e?.message}`)
        continue
      } finally {
        timed.cleanup()
      }
      const parsed = extractJsonObject(text)
      if (parsed) return parsed
      log(`LLM 输出不是 JSON（第 ${attempt} 次），前 160 字：${text.slice(0, 160)}`)
    }
    return null
  }

  /* ── 挑片：沒看過 / 沒分享過 ── */
  const pickUnseen = (candidates) => {
    const shared = readShared()
    const seen = readSeen(root)
    for (const c of candidates || []) {
      const bvid = String(c?.bvid || '').trim()
      if (!bvid) continue
      if (hasRecentReport(bvid)) continue
      if (shared[bvid]) continue
      if (seen[bvid]) continue
      return bvid
    }
    return null
  }

  /**
   * 用關鍵詞挑片（帶排序/日期/條數 + 候選池）。
   * 搜尋失敗時先降級成「熱門榜 + 標題關鍵詞篩選」；若降級也挑不到任何候選，
   * 把原始搜尋錯誤往上丟，讓上層回報「搜尋失敗」而不是誤導性的「都看過了」。
   * @returns {Promise<{picked: string|null, pool: object}>}
   */
  const pickByKeyword = async (keyword, signal, extra = {}) => {
    const kw = String(keyword || '').trim()
    const kwLower = kw.toLowerCase()
    const ex = normalizeExtra(extra)
    const order = ex.order || String(cfg.searchOrder || DEFAULTS.searchOrder)
    const days = ex.days || Math.max(0, Number(cfg.searchDays) || 0)
    const num = ex.num || Math.max(1, Number(cfg.searchNum) || 20)
    let searchError = null
    let briefs = []
    try {
      briefs = await bili.searchBriefs(kw, { order, days, num, pageSize: 20, cookie: readCookie(), signal })
    } catch (e) {
      searchError = e
      log(`B站搜索失败（${e?.message}），降级用热门榜标题过滤`)
      try {
        briefs = (await bili.getPopularBriefs({ cookie: readCookie(), signal })).filter((b) =>
          (b.title || '').toLowerCase().includes(kwLower),
        )
      } catch (e2) {
        log(`热门榜也失败（${e2?.message}），以搜索错误为准往上报`)
        throw searchError
      }
      if (!briefs.length) {
        log('热门榜也没有含此关键词的标题，以搜索错误为准往上报')
        throw searchError
      }
    }
    const pooled = poolAdd(briefs)
    const picked = pickUnseen(pooled.videos)
    if (!picked && searchError) {
      // 搜尋沒成功，熱門榜過濾出的候選又都看過/分享過 —— 不能說「都看過了」，
      // 真實原因是搜尋被限流，候選池本來就不完整。
      log('热门榜候选都看过/分享过，但搜索本身失败，仍以搜索错误为准')
      throw searchError
    }
    return { picked, pool: pooled }
  }

  const pickFromPopular = async (signal) => {
    const briefs = await bili.getPopularBriefs({ cookie: readCookie(), signal })
    return pickUnseen(briefs)
  }

  /* ── UP 主定向：查表 → 搜用戶 → 取投稿 ── */
  const readAccounts = () => {
    const a = readJson(accountsPath, {})
    return a && typeof a === 'object' ? a : {}
  }
  const isUid = (v) => /^\d+$/.test(String(v).trim())
  const resolveUid = async (nameOrUid, signal) => {
    const v = String(nameOrUid || '').trim()
    if (!v) return null
    if (isUid(v)) return v
    const key = v.toLowerCase()
    const accounts = readAccounts()
    if (accounts[key] && accounts[key].uid) return String(accounts[key].uid)
    const u = await bili.searchUser(v, { cookie: readCookie(), signal })
    if (!u) return null
    accounts[key] = { name: u.name || v, uid: u.uid, fans: u.fans, at: new Date().toISOString() }
    writeJson(accountsPath, accounts)
    return u.uid
  }
  const processUpRequest = async (value, id) => {
    if (!cfg.upSearch) {
      log(`UP「${value}」定向已停用（Config upSearch=false）`)
      return { ok: true, skipped: true, reason: 'up-search-disabled' }
    }
    // ── 兩條路：先準的，再退而求其次 ──
    // ① 投稿接口 x/space/wbi/arc/search：最準，但 2026-10 實測被反爬擋（code=-352，
    //    要 dm_img_* / w_webid 那套網頁風控參數）。
    // ② 降級：拿 UP 名字去 search/type 搜影片，再只留 author 對得上的 ——
    //    實測可用，代價是只能撈到「被搜到的」而不是完整投稿列表。
    // 兩條都失敗才放棄；不再因為一個布林就讓整條路死掉。
    let briefs = []
    let uid = null
    try {
      uid = await resolveUid(value)
    } catch (e) {
      log(`UP「${value}」解析 uid 失败（继续用名字搜）：${e?.message}`)
    }
    if (uid) {
      try {
        briefs = await bili.userVideos(uid, { ps: 30, order: 'pubdate', cookie: readCookie() })
      } catch (e) {
        log(`UP「${value}」投稿接口不可用（${e?.message}），降级用作者名筛选`)
      }
    }
    if (!briefs.length) {
      const name = String(value || '').trim()
      try {
        const all = await bili.searchBriefs(name, {
          order: 'pubdate',
          num: Math.max(10, Number(cfg.searchNum) || 20),
          cookie: readCookie(),
        })
        // author 完全相等優先，其次包含；免得把「某某的粉丝号」也當成他本人
        const exact = (all || []).filter((v) => String(v.author || '').trim() === name)
        briefs = exact.length
          ? exact
          : (all || []).filter((v) => String(v.author || '').includes(name))
        log(`UP「${name}」用搜索结果按作者筛：${briefs.length} 条候选`)
      } catch (e) {
        log(`UP「${value}」搜索失败：${e?.message}`)
        return { ok: false, error: `UP 搜索失败：${e?.message || 'unknown'}` }
      }
    }
    if (!briefs.length) {
      log(`UP「${value}」没捞到投稿（名字对不上，或都被看过）`)
      return { ok: true, skipped: true, reason: 'no-candidates' }
    }
    const pooled = poolAdd(briefs)
    const picked = pickUnseen(pooled.videos)
    if (picked) return await watchBvid(picked, { sourceRequest: id })
    log(`UP「${value}」投稿都看过/分享过，没挑到新的`)
    return { ok: true, skipped: true, reason: 'no-candidates' }
  }

  /* ── 看一條（in-flight 去重：工具與 tick 共用同一個表） ── */
  const inflightWatches = new Map()

  /** 記一筆 skipped，讓 pickUnseen / filterFresh 之後都跳過它（避免每輪重打同一條）。 */
  const markSkippedSeen = (bvid, reason) => {
    try {
      markSeen(root, bvid, 'skipped', undefined, { reason: String(reason || '').slice(0, 120) })
      log(`已记下跳过 ${bvid}（${reason}），之后自动挑片不会再选它`)
    } catch (e) {
      log(`写 seen skipped 失败（${e?.message}）`)
    }
  }

  const watchBvidOnce = async (bvid, { sourceRequest = null, signal: extSignal } = {}) => {
    const { signal, release } = watchSignal(extSignal)
    const cookie = readCookie()
    /** 明確失敗 → 回 ok:false 並依規則記 seen skipped（too_long 也算）。 */
    const fail = (error, extra = {}) => {
      const result = { ok: false, error: String(error || 'unknown'), ...extra }
      if (shouldMarkSkipped(result)) markSkippedSeen(bvid, result.reason || result.error)
      return result
    }
    try {
      if (hasRecentReport(bvid)) return { ok: false, skipped: true, reason: 'recent' }

      const meta = await bili.getVideoInfo(bvid, { cookie, signal })
      if (!meta) return fail('view 接口拿不到这个视频')
      if (meta.durationSec > Math.max(0, Number(cfg.maxDurationSec || 0))) {
        return fail(`视频太长（${meta.durationSec} 秒 > 上限 ${Math.max(0, Number(cfg.maxDurationSec || 0))} 秒），跳过`, {
          skipped: true,
          reason: 'too_long',
          duration_sec: meta.durationSec,
        })
      }

      const hasCredential = readCredentialState().has
      let subtitleText = ''
      let subtitleState = 'none' // 'ok' | 'none'(這條片沒字幕) | 'error'(抓取失敗)
      try {
        const { subtitles } = await bili.getPlayerInfo(bvid, meta.cid, { cookie, signal })
        const pick = bili.pickSubtitle(subtitles)
        if (pick) {
          subtitleText = await bili.getSubtitleText(pick, { signal, maxChars: Math.max(0, Number(cfg.maxSubtitleChars || 0)) })
          subtitleState = (subtitleText || '').trim() ? 'ok' : 'error'
          if (subtitleState === 'error') log(`字幕拿到但内容为空：${bvid}`)
        } else {
          subtitleState = 'none'
          log(`这个视频没有字幕（或没有可用的中文字幕）：${bvid}`)
        }
      } catch (e) {
        subtitleState = 'error'
        if (hasCredential) {
          log(`字幕抓取失败且有凭证（${e?.message}），可能凭证过期或风控（这个视频可能其实有字幕）`)
        } else {
          log(`字幕抓取失败（${e?.message}），watched_level 会降级`)
        }
      }

      let comments = []
      try {
        comments = await bili.getComments(meta.aid, { cookie, signal, count: 5 })
      } catch (e) {
        log(`评论拿不到（${e?.message}）`)
      }

      const availability = {
        hasSubtitle: (subtitleText || '').trim().length > 0,
        hasDesc: (meta.desc || '').trim().length > 0,
        hasComments: comments.length > 0,
        subtitleState,
        hasCredential,
      }

      const content = await callLlm({ meta, subtitleText, comments, availability, signal })
      if (!content) {
        // dispose/abort 讓 LLM 回 null：不是這條片有問題，不要記 skipped
        if (signal.aborted) return { ok: false, error: '已中止', aborted: true }
        return fail('LLM 两次都失败，跳过')
      }

      const report = buildReport({ ...meta, ...availability }, content, sourceRequest)
      saveReport(report)
      markSeen(root, bvid, 'watched')
      if (cfg.notesMd) {
        try {
          atomicWriteFile(join(notesDir, report.bvid + '.md'), buildNoteMd(report))
        } catch (e) {
          log(`写笔记 md 失败（${e?.message}）`)
        }
      }
      incrementDailyCount()
      log(`看完了 ${bvid}「${report.title}」（watched_level=${report.watched_level}）`)
      return { ok: true, report }
    } catch (e) {
      // 插件關閉/呼叫方 abort 不是「這條片壞了」：不要永久記成 skipped
      if (extSignal?.aborted || signal.aborted) {
        log(`看 ${bvid} 被中止（插件关闭或调用方 abort），不记 skipped`)
        return { ok: false, error: '已中止', aborted: true }
      }
      log(`看 ${bvid} 失败：${e?.message}`)
      return fail(e?.message)
    } finally {
      release()
    }
  }

  /**
   * 同一條 bvid 同時只跑一次：`bili_watch` 工具與佇列 tick 共用。
   * 後到的呼叫直接 await 既有的 Promise → 不會兩次 LLM、報告互蓋、watchedToday +2。
   */
  const watchBvid = (bvid, opts = {}) => {
    const key = String(bvid || '').trim()
    if (!key) return Promise.resolve({ ok: false, error: 'bvid 是空的' })
    return dedupeInflight(inflightWatches, key, () => watchBvidOnce(key, opts))
  }

  /* ── 請求佇列處理（一次只跑一個，加鎖） ── */
  let busy = false
  const processRequest = async (req) => {
    const id = String(req.id || '')
    const kind = String(req.kind || '')
    const value = String(req.value || '')
    let outcome = { ok: true }
    try {
      if (kind === 'bvid') {
        outcome = await watchBvid(value, { sourceRequest: id })
      } else if (kind === 'keyword') {
        let picked = null
        try {
          const r = await pickByKeyword(value, undefined, req.extra)
          picked = r.picked
        } catch (e) {
          log(`keyword「${value}」搜索失败：${e?.message}（这是 B 站风控/搜索失败，不是「都看过了」）`)
          outcome = { ok: false, error: `关键词搜索失败：${e?.message || 'unknown'}` }
        }
        if (outcome.ok) {
          if (picked) outcome = await watchBvid(picked, { sourceRequest: id })
          else {
            log(`keyword 搜索成功但没挑到没看过/没分享过的：${value}`)
            outcome = { ok: true, skipped: true, reason: 'no-candidates' }
          }
        }
      } else if (kind === 'up') {
        outcome = await processUpRequest(value, id)
      } else if (kind === 'auto') {
        const picked = await pickFromPopular()
        if (picked) outcome = await watchBvid(picked, { sourceRequest: id })
        else {
          log('auto 没挑到没看过/没分享过的热门片')
          outcome = { ok: true, skipped: true, reason: 'no-candidates' }
        }
      } else {
        log(`未知 kind：${kind}`)
        outcome = { ok: false, error: `未知 kind：${kind}` }
      }
    } catch (e) {
      log(`处理请求 ${id} 失败：${e?.message}`)
      outcome = { ok: false, error: String(e?.message || 'unknown') }
    }
    // 插件關閉造成的中止不是「失敗」：留在 requests/，不要消耗重試次數
    if (outcome?.aborted) {
      log(`请求 ${id || req.name} 因中止/关闭而暂停，留在 requests/ 不计失败次数`)
      return
    }
    // 成功/明確終點才進 done；失敗第 1 次留在 requests/ 重試，第 2 次進 failed
    try {
      const settled = settleRequest({
        file: req.file,
        outcome,
        attempts: Number(req.attempts) || 0,
        doneDir,
        failedDir,
      })
      if (settled.action === 'retry') {
        log(`请求 ${id || req.name} 第 ${settled.attempts} 次失败，留在 requests/ 下一轮重试`)
      } else if (settled.action === 'failed') {
        log(`请求 ${id || req.name} 失败 ${settled.attempts} 次，已隔离到 requests/failed/`)
      }
    } catch (e) {
      log(`请求 ${id || req.name} 结案失败：${e?.message}`)
    }
  }

  const tick = async () => {
    if (busy || disposed) return
    busy = true
    try {
      refreshCfg()
      ensureDirs()
      poolReset()
      const { ok: reqs, bad } = scanRequestsDetailed(requestsDir)
      if (bad.length) {
        // 壞 JSON 不再每 30 秒重讀一次：搬到 requests/bad/ 並只 log 這一輪
        try {
          const q = quarantineBad(requestsDir, bad, { badDir })
          log(
            `隔离 ${q.moved.length} 个坏掉的请求文件到 requests/bad/：${q.moved.join(', ') || '(无)'}` +
              (q.failed.length ? `；${q.failed.length} 个搬不动（见下一行）` : ''),
          )
          for (const f of q.failed) log(`隔离失败：${f.file}（${f.error}）`)
        } catch (e) {
          log(`隔离坏请求文件失败（下轮再试）：${e?.message}`)
        }
      }
      for (const req of reqs) {
        if (disposed) break
        await processRequest(req)
      }
    } catch (e) {
      log(`队列这一轮出错（已兜住）：${e?.message}`)
    } finally {
      busy = false
    }
  }

  /* ── 自動備貨 ── */
  const parseWindow = (s) => {
    const m = /^(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2})$/.exec(String(s).trim())
    if (!m) return [null, null]
    return [Number(m[1]) * 60 + Number(m[2]), Number(m[3]) * 60 + Number(m[4])]
  }
  const inAutoWindow = (now = new Date()) => {
    const windows = Array.isArray(cfg.autoWindows) ? cfg.autoWindows : []
    if (!windows.length) return true
    const hm = now.getHours() * 60 + now.getMinutes()
    for (const w of windows) {
      const [a, b] = parseWindow(w)
      if (a == null || b == null) continue
      if (a <= b) {
        if (hm >= a && hm < b) return true
      } else if (hm >= a || hm < b) {
        return true
      }
    }
    return false
  }
  const countUnsharedShareWorthy = () => {
    const shared = readShared()
    const cutoff = Date.now() - 72 * 3600 * 1000
    let n = 0
    let files = []
    try {
      files = readdirSync(reportsDir)
    } catch {
      return 0
    }
    for (const f of files) {
      if (!f.endsWith('.json') || f === 'index.json') continue
      const r = readJson(join(reportsDir, f), null)
      if (!r || typeof r !== 'object') continue
      if (Date.parse(r.created_at || '') < cutoff) continue
      if (!r.share_worthy) continue
      if (shared[r.bvid]) continue
      n++
    }
    return n
  }

  const autoTick = async () => {
    if (busy || disposed) return
    busy = true
    try {
      refreshCfg()
      ensureDirs()
      poolReset()
      if (!cfg.autoStock) return
      if (!inAutoWindow()) return
      if (dailyCount() >= Math.max(0, Number(cfg.dailyLimit || 0))) return
      if (countUnsharedShareWorthy() >= Math.max(0, Number(cfg.stockTarget || 0))) return
      const { signal, release } = watchSignal()
      try {
        const picked = await pickFromPopular(signal)
        if (picked) await watchBvid(picked, { sourceRequest: null })
        else log('自动备货：热门榜没挑到没看过/没分享过的')
      } finally {
        release()
      }
    } catch (e) {
      log(`自动备货这一轮出错（已兜住）：${e?.message}`)
    } finally {
      busy = false
    }
  }

  /* ── DSH 工具 ── */
  const safe = (v) => {
    if (v === undefined || v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return v
    try {
      return JSON.parse(JSON.stringify(v))
    } catch {
      return String(v)
    }
  }
  const textOutput = {
    schema: { type: 'object', properties: {}, additionalProperties: true },
    render: (_args, value) => [
      { type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) },
    ],
  }
  const tool = (spec) =>
    ctx.tools.register(
      defineTool({
        ...spec,
        output: textOutput,
        async execute(args, exec) {
          return safe(await spec.execute(args, exec))
        },
      }),
    )

  const recentReports = (n = 5) =>
    readIndex()
      .slice(0, n)
      .map((e) => ({ bvid: e.bvid, title: e.title, watched_level: e.watched_level, created_at: e.created_at }))

  tool({
    name: 'bili_watch',
    description:
      '真的去 B 站看一个视频并回报：给 bvid 就是「定点看这一个」，给 keyword 就搜索后挑一个（优先挑没看过、没分享过的）。它会真的去读这个视频的字幕（拿得到的话）和热门评论，再产出一份结构化报告，回传报告摘要。'
      + '报告里的 watched_level 表示它「真的看到了多少」：subtitle=读了字幕、desc_comments=只看了简介＋评论、title_only=只有标题（后两者没有真的看到/听到画面内容，别假装它看过）。'
      + '这个工具只负责「看」和「写报告」，不会替你在群里说话，也不会发 B 站评论。',
    parameters: {
      bvid: { type: 'string', description: '可选。BV 号，给了就是「定点看这一个」，不搜不挑。' },
      keyword: { type: 'string', description: '可选。关键词，搜索后挑一个来看。bvid 与 keyword 至少给一个。' },
      order: { type: 'string', description: '可选。搜索排序：totalrank(综合)/click(最多播放)/pubdate(最新)/dm(最多弹幕)/stow(最多收藏)/scores(评分)。' },
      days: { type: 'number', description: '可选。只搜最近 N 天发布（>0 交给 B 站自己筛）。' },
      num: { type: 'number', description: '可选。搜索目标条数（候选池目标，默认 config.searchNum）。' },
    },
    async execute(args) {
      const bvid = String(args?.bvid || '').trim()
      const keyword = String(args?.keyword || '').trim()
      if (!bvid && !keyword) return { ok: false, error: 'bvid 与 keyword 至少要给一个' }
      let target = bvid
      let searchPool = null
      if (!target) {
        refreshCfg()
        // poolReset() 只給 tick 用：工具不要洗掉佇列這一輪正在用的共用候選池
        const { signal, release } = watchSignal()
        let searchError = null
        try {
          const r = await pickByKeyword(keyword, signal, {
            order: args?.order,
            days: args?.days,
            num: args?.num,
          })
          target = r.picked
          searchPool = r.pool
        } catch (e) {
          searchError = e
        } finally {
          release()
        }
        if (searchError) {
          return { ok: false, error: `B站搜索失败：${searchError?.message || 'unknown'}` }
        }
        if (!target) {
          return {
            ok: false,
            error: '没挑到没看过/没分享过的视频（搜索正常，候选都已看过或分享过）',
            ...(searchPool || {}),
          }
        }
      }
      const r = await watchBvid(target, { sourceRequest: null })
      if (!r.ok) return r
      const brief = reportBrief(r.report)
      if (searchPool) {
        return {
          ok: true,
          report: brief,
          videos: searchPool.videos,
          skippedSeen: searchPool.skippedSeen,
          poolCount: searchPool.poolCount,
          poolTarget: searchPool.poolTarget,
          poolEnough: searchPool.poolEnough,
        }
      }
      return { ok: true, report: brief }
    },
  })

  tool({
    name: 'bili_watch_status',
    description:
      '看「大肥鱼看 B 站」插件的运行状态：请求队列长度、今日已看几条、每日自动备货上限、自动备货开关、是否持有登录凭证与凭证来源（只回有无与来源，绝不回凭证值）、最近几份报告标题。只回状态，不触发看片。',
    parameters: {},
    async execute() {
      refreshCfg()
      const cred = credentialStatus(readCredentialState())
      const 模型 = guardModel(cfg.model || DEFAULTS.model, cfg.allowedModel || DEFAULTS.allowedModel)
      return {
        ok: true,
        // 實際會用來呼叫 LLM 的模型（白名單只有 deepseek-flash，其餘一律被擋）
        model: 模型.model,
        model_requested: 模型.requested,
        model_allowed: 模型.allowed,
        model_blocked: 模型.changed,
        model_block_reason: 模型.reason,
        queue_length: scanRequests(requestsDir).length,
        // 同一條 bvid 的 in-flight 去重：工具與佇列共用，這裡只是觀測值
        inflight_watches: inflightWatches.size,
        watched_today: dailyCount(),
        daily_limit: Number(cfg.dailyLimit) || 0,
        auto_stock: Boolean(cfg.autoStock),
        has_credential: cred.has,
        credential_source: cred.source,
        recent_reports: recentReports(5),
      }
    },
  })

  /* ── 定時器：全部透過 ctx.effect 回收 ── */
  const pollMs = Math.max(1, Number(cfg.pollSec || DEFAULTS.pollSec)) * 1000
  const autoMs = Math.max(1, Number(cfg.autoIntervalMin || DEFAULTS.autoIntervalMin)) * 60 * 1000
  const pollTimer = setInterval(() => {
    tick().catch((e) => log(`tick 未捕获异常：${e?.message}`))
  }, pollMs)
  const autoTimer = setInterval(() => {
    autoTick().catch((e) => log(`autoTick 未捕获异常：${e?.message}`))
  }, autoMs)
  pollTimer.unref?.()
  autoTimer.unref?.()

  ctx.effect(() => () => {
    disposed = true
    clearInterval(pollTimer)
    clearInterval(autoTimer)
    abortAll()
    bili.disposeCookieCache()
  }, 'pawbot-bili-watch: 定时器')

  // 啟動時先清一輪排隊的請求
  Promise.resolve()
    .then(() => tick())
    .catch((e) => log(`启动 tick 失败：${e?.message}`))

  const 启动模型 = guardModel(cfg.model || DEFAULTS.model, cfg.allowedModel || DEFAULTS.allowedModel)
  log(`已启动：root=${root}（provider=${cfg.provider} model=${启动模型.model}${启动模型.changed ? `，设置写的是 ${启动模型.requested || '(空)'} 已被挡掉` : ''}）`)
}
