/**
 * lib/bili.js —— B 站抓取（Node 內建 fetch，零依賴）。
 * 只讀不寫：拿影片資訊 / 字幕 / 熱門評論 / 熱門榜 / 搜尋 / 搜用戶 / UP 投稿。
 * 不發任何訊息或評論。
 */
import { encWbi } from './wbi.js'
import { subtitleJsonToText } from './subtitle.js'
import {
  FINGER_SPI_URL,
  DEFAULT_TTL_MS,
  createCookieManager,
  sessdataFromCookie,
} from './cookie.js'
import {
  buildHeaders,
  fetchWithRetry,
  isBilibiliHost,
} from './http.js'

const BASE = 'https://api.bilibili.com'

/* ── buvid 指紋：首次請求前抓一次，進程內快取；412 時可 force 刷新 ── */
const cookieManager = createCookieManager({
  ttlMs: DEFAULT_TTL_MS,
  fetchSpi: async ({ signal } = {}) => {
    const res = await fetch(FINGER_SPI_URL, { headers: buildHeaders(''), signal })
    if (!res.ok) throw new Error(`finger/spi HTTP ${res.status}`)
    return res.json()
  },
})

/** 插件 dispose 時清掉指紋快取（不清也行，但讓生命週期乾淨）。 */
export function disposeCookieCache() {
  cookieManager.dispose()
}

/**
 * 發請求 + 風控重試：412（或 403 且 body 提示風控）等 800~1500ms、刷新 buvid、
 * 最多再試 2 次；仍失敗拋出明確的風控錯誤。
 * Cookie（buvid + SESSDATA）只送給 bilibili.com；字幕 CDN（hdslb.com）不帶。
 */
function fetchJson(url, { cookie = '', signal, headers } = {}) {
  const withCookie = isBilibiliHost(url)
  return fetchWithRetry(url, {
    cookieManager: withCookie ? cookieManager : null,
    sessdata: withCookie ? sessdataFromCookie(cookie) : '',
    headers,
    signal,
  })
}

/** 從 wbi 圖片 URL 抽出 32 位 key */
function wbiKeyFromUrl(url) {
  const m = /\/([0-9a-f]{32})\.(?:png|jpe?g)/i.exec(String(url || ''))
  return m ? m[1] : String(url || '').split('/').pop().split('.')[0]
}

function isoFromUnix(sec) {
  const n = Number(sec)
  if (!Number.isFinite(n) || n <= 0) return ''
  return new Date(n * 1000).toISOString()
}

function clip120(s) {
  const t = String(s ?? '').replace(/\s+/g, ' ').trim()
  return t.length > 120 ? t.slice(0, 120) + '…' : t
}

/** 去標題裡的 HTML 標籤（搜尋結果常帶 `<em class="keyword">` 高亮）與常見實體 */
function cleanTitle(s) {
  return String(s ?? '')
    .replace(/<[^>]*>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/\s+/g, ' ')
    .trim()
}

/** B 站時長字串（`MM:SS` / `HH:MM:SS` / 純秒）→ 秒 */
export function parseDuration(s) {
  const str = String(s ?? '').trim()
  if (!str) return 0
  if (str.includes(':')) {
    const parts = str.split(':').map((x) => parseInt(x, 10) || 0)
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if (parts.length === 2) return parts[0] * 60 + parts[1]
  }
  const n = Number(str)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
}

/** 播放量：數字或 `1.2万` / `3.4亿` → 整數 */
export function parsePlay(v) {
  const n = Number(v)
  if (Number.isFinite(n)) return n >= 0 ? Math.floor(n) : 0
  const s = String(v ?? '').trim()
  if (!s) return 0
  const m = /^([\d.]+)\s*(万|萬|亿|億)?$/.exec(s)
  if (!m) return 0
  const base = parseFloat(m[1])
  if (!Number.isFinite(base)) return 0
  const unit = m[2]
  if (unit === '万' || unit === '萬') return Math.floor(base * 10000)
  if (unit === '亿' || unit === '億') return Math.floor(base * 100000000)
  return Math.floor(base)
}

/** search/type 接受的 order 白名單 */
const SEARCH_ORDERS = ['totalrank', 'click', 'pubdate', 'dm', 'stow', 'scores']

/**
 * 組 search/type 的請求參數（純函數，可離線自測）。
 * - order → `order`（白名單外不帶，交給 B 站預設綜合排序）
 * - days>0 → `pubtime_begin_s` / `pubtime_end_s`（秒級時間戳，交給 B 站自己篩）
 * @returns {Record<string, string|number>}
 */
export function buildSearchParams({ keyword = '', order = 'totalrank', days = 0, pageSize = 20, page = 1 } = {}) {
  const params = {
    search_type: 'video',
    keyword: String(keyword ?? ''),
    page: Number.isFinite(Number(page)) ? Number(page) : 1,
    page_size: Number.isFinite(Number(pageSize)) ? Math.min(50, Math.max(1, Number(pageSize))) : 20,
  }
  const ord = String(order ?? 'totalrank')
  if (SEARCH_ORDERS.includes(ord)) params.order = ord
  const d = Number(days)
  if (d > 0) {
    const nowSec = Math.floor(Date.now() / 1000)
    params.pubtime_begin_s = nowSec - Math.floor(d) * 86400
    params.pubtime_end_s = nowSec
  }
  return params
}

/** search 介面回非 0 時的明確錯誤（-412 標記風控、-352 標記反爬、其餘標 code） */
function throwSearchError(res, label) {
  const code = Number(res?.code)
  if (code === -412 || code === 412) {
    const err = new Error(`B站风控 412（${label}回 code=-412，可能是请求太频繁或缺 Cookie）`)
    err.name = 'BiliRiskControlError'
    err.status = 412
    err.risk = true
    err.attempts = 0
    throw err
  }
  if (code === -352) {
    throw new Error(`${label}回 code=-352（风控校验失败：该接口需要额外的网页反爬参数 dm_img_* / w_webid，本插件未实现）`)
  }
  throw new Error(`${label} code=${res?.code}：${res?.message || ''}`)
}

/** 把 search/type 的一條結果映射成完整簡表 */
function mapSearchItem(item) {
  return {
    bvid: String(item?.bvid ?? ''),
    title: cleanTitle(item?.title),
    author: String(item?.author ?? ''),
    play: parsePlay(item?.play),
    duration: String(item?.duration ?? '0'),
    durationSec: parseDuration(item?.duration),
    tname: String(item?.tag ?? item?.typename ?? ''),
    desc: String(item?.description ?? '').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim(),
    pubdate: isoFromUnix(item?.pubdate),
  }
}

/** nav 介面：取 wbi 簽名用的 img_key / sub_key */
export async function getWbiKeys({ signal, cookie } = {}) {
  const res = await fetchJson(`${BASE}/x/web-interface/nav`, { cookie, signal })
  const imgUrl = res?.data?.wbi_img?.img_url
  const subUrl = res?.data?.wbi_img?.sub_url
  if (!imgUrl || !subUrl) throw new Error('nav 接口没有 wbi_img')
  return { imgKey: wbiKeyFromUrl(imgUrl), subKey: wbiKeyFromUrl(subUrl) }
}

/** 影片資訊 view */
export async function getVideoInfo(bvid, { cookie, signal } = {}) {
  const res = await fetchJson(`${BASE}/x/web-interface/view?bvid=${encodeURIComponent(bvid)}`, { cookie, signal })
  const d = res?.data
  if (!d || res?.code !== 0) return null
  return {
    bvid: String(d.bvid ?? bvid),
    aid: Number(d.aid ?? 0),
    cid: Number(d.cid ?? 0),
    title: cleanTitle(d.title),
    author: String(d.owner?.name ?? ''),
    durationSec: Number(d.duration ?? 0),
    play: Number(d.stat?.view ?? 0),
    pubdate: isoFromUnix(d.pubdate),
    tname: String(d.tname ?? ''),
    desc: String(d.desc ?? ''),
    pic: String(d.pic ?? ''),
  }
}

/** player wbi v2：拿字幕清單。`code !== 0` 一律 throw（被風控時不能回空清單假裝「沒字幕」）。 */
export async function getPlayerInfo(bvid, cid, { cookie, signal } = {}) {
  const { imgKey, subKey } = await getWbiKeys({ signal, cookie })
  const signed = encWbi({ bvid, cid: String(cid) }, imgKey, subKey)
  const res = await fetchJson(`${BASE}/x/player/wbi/v2?${signed.query}`, { cookie, signal })
  if (res?.code !== 0) throwSearchError(res, 'player 接口')
  const subs = res?.data?.subtitle?.subtitles || []
  return {
    subtitles: subs.map((s) => ({
      lan: String(s.lan ?? ''),
      lanDoc: String(s.lan_doc ?? ''),
      url: String(s.subtitle_url ?? ''),
    })),
  }
}

/** 挑字幕：優先 zh-CN / ai-zh，其次任何中文，其次第一個 */
export function pickSubtitle(subtitles) {
  const list = Array.isArray(subtitles) ? subtitles : []
  const valid = list.filter((s) => {
    const u = String(s?.url || '')
    return u.startsWith('//') || u.startsWith('http')
  })
  return (
    valid.find((s) => s.lan === 'zh-CN') ||
    valid.find((s) => s.lan === 'ai-zh') ||
    valid.find((s) => /zh/i.test(s.lan)) ||
    valid[0] ||
    null
  )
}

/** 下載字幕 JSON 並轉成帶時間戳文字 */
export async function getSubtitleText(subtitle, { signal, maxChars = 12000 } = {}) {
  const raw = String(subtitle?.url || '')
  if (!raw) return ''
  const url = raw.startsWith('//') ? 'https:' + raw : raw
  const json = await fetchJson(url, { signal })
  return subtitleJsonToText(json, maxChars)
}

/** 熱門影片（簡表）。`code !== 0` 一律 throw，上層才能區分「限流」與「真的都看過」。 */
export async function getPopularBriefs({ signal, ps = 20, cookie } = {}) {
  const res = await fetchJson(`${BASE}/x/web-interface/popular?ps=${ps}`, { cookie, signal })
  if (res?.code !== 0) throwSearchError(res, '热门榜接口')
  const list = res?.data?.list || []
  return list
    .map((item) => ({ bvid: String(item?.bvid ?? ''), title: cleanTitle(item?.title) }))
    .filter((x) => x.bvid)
}

/**
 * 搜尋影片（完整簡表）。帶完整瀏覽器標頭 + buvid 指紋；遇 412 由 fetchJson 重試，
 * 重試耗盡會 throw 明確的風控錯誤（呼叫方藉此區分「被限流」與「真的沒結果」）。
 * @param {string} keyword
 * @param {object} [opts]
 * @param {string} [opts.order] totalrank/click/pubdate/dm/stow/scores
 * @param {number} [opts.days] >0 交給 B 站按發佈時間篩（pubtime_begin_s/pubtime_end_s）
 * @param {number} [opts.num] 目標條數（跨頁最多抓 3 頁）
 * @param {number} [opts.pageSize] 每頁條數
 */
export async function searchBriefs(keyword, { order = 'totalrank', days = 0, num = 20, pageSize = 20, cookie, signal } = {}) {
  const pageSizeN = Math.min(50, Math.max(1, Number(pageSize) || 20))
  const numN = Math.max(1, Number(num) || pageSizeN)
  const maxPages = Math.min(3, Math.ceil(numN / pageSizeN))
  const { imgKey, subKey } = await getWbiKeys({ signal, cookie })
  const out = []
  for (let page = 1; page <= maxPages; page++) {
    const params = buildSearchParams({ keyword, order, days, pageSize: pageSizeN, page })
    const signed = encWbi(params, imgKey, subKey)
    const res = await fetchJson(`${BASE}/x/web-interface/search/type?${signed.query}`, { cookie, signal })
    if (res?.code !== 0) throwSearchError(res, '搜索接口')
    const list = res?.data?.result || []
    for (const item of list) {
      const b = mapSearchItem(item)
      if (b.bvid) out.push(b)
    }
    if (out.length >= numN) break
    const total = Number(res?.data?.numResults)
    if (total > 0 && out.length >= total) break
    if (!list.length) break
  }
  return out.slice(0, numN)
}

/**
 * 搜用戶（暱稱 → uid）。`search/type?search_type=bili_user`。
 * 回 null 表示「沒找到」；介面報錯則 throw（不靜默回空）。
 */
export async function searchUser(keyword, { cookie, signal } = {}) {
  const { imgKey, subKey } = await getWbiKeys({ signal, cookie })
  const params = { search_type: 'bili_user', keyword: String(keyword ?? ''), page: 1, page_size: 10 }
  const signed = encWbi(params, imgKey, subKey)
  const res = await fetchJson(`${BASE}/x/web-interface/search/type?${signed.query}`, { cookie, signal })
  if (res?.code !== 0) throwSearchError(res, '搜索用户接口')
  const list = res?.data?.result || []
  const u = list.find((x) => x?.type === 'bili_user' || x?.mid) || list[0]
  if (!u || !u.mid) return null
  return {
    uid: String(u.mid ?? ''),
    name: String(u.uname ?? u.name ?? ''),
    fans: Number(u.fans ?? 0),
    videos: Number(u.videos ?? 0),
    sign: String(u.usign ?? u.sign ?? ''),
  }
}

/**
 * UP 主投稿（簡表）。`x/space/wbi/arc/search`（WBI 簽名）。
 * order ∈ {pubdate, click, stow}（預設 pubdate 最新）。
 */
export async function userVideos(uid, { ps = 30, order = 'pubdate', cookie, signal } = {}) {
  const { imgKey, subKey } = await getWbiKeys({ signal, cookie })
  const params = {
    mid: String(uid ?? ''),
    ps: Math.min(50, Math.max(1, Number(ps) || 30)),
    pn: 1,
    order: String(order || 'pubdate'),
    tid: 0,
    keyword: '',
  }
  const signed = encWbi(params, imgKey, subKey)
  const res = await fetchJson(`${BASE}/x/space/wbi/arc/search?${signed.query}`, { cookie, signal })
  if (res?.code !== 0) throwSearchError(res, 'UP 投稿接口')
  const list = res?.data?.list?.vlist || []
  return list
    .map((v) => ({
      bvid: String(v?.bvid ?? ''),
      title: cleanTitle(v?.title),
      author: String(v?.author ?? ''),
      play: parsePlay(v?.play),
      duration: String(v?.length ?? '0'),
      durationSec: parseDuration(v?.length),
      tname: String(v?.tname ?? v?.typename ?? ''),
      desc: String(v?.description ?? '').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim(),
      pubdate: isoFromUnix(v?.created),
    }))
    .filter((x) => x.bvid)
}

/** 熱門評論（mode=3 熱評），取前 N 條（按讚數排序）。`code !== 0` 一律 throw。 */
export async function getComments(aid, { signal, count = 5, cookie } = {}) {
  const res = await fetchJson(`${BASE}/x/v2/reply/main?type=1&oid=${Number(aid)}&mode=3&ps=30`, { cookie, signal })
  if (res?.code !== 0) throwSearchError(res, '评论接口')
  const all = []
  for (const key of ['top_replies', 'replies']) {
    for (const r of res?.data?.[key] || []) all.push(r)
  }
  const seen = new Set()
  const out = []
  for (const r of all) {
    if (seen.has(r?.rpid)) continue
    seen.add(r?.rpid)
    const text = clip120(r?.content?.message)
    if (!text) continue
    out.push({ text, like: Number(r?.like ?? 0) })
  }
  out.sort((a, b) => b.like - a.like)
  return out.slice(0, count)
}
