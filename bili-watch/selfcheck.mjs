/**
 * selfcheck.mjs —— 離線自測（不依賴 DSH、不碰網路）。
 * 用法：node selfcheck.mjs
 */
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, readFileSync, rmSync, existsSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { encWbi, getMixinKey } from './lib/wbi.js'
import { subtitleJsonToText, secToTs } from './lib/subtitle.js'
import { determineWatchedLevel, buildReport, stripTimestamp, truncate } from './lib/report.js'
import { atomicWriteFile } from './lib/atomic.js'
import { scanRequests, scanRequestsDetailed, quarantineBad, normalizeExtra } from './lib/queue.js'
import {
  composeCookieHeader,
  sessdataFromCookie,
  parseFingerSpi,
  createCookieManager,
} from './lib/cookie.js'
import {
  resolveCredential,
  parseCredentialFile,
  credentialStatus,
} from './lib/credential.js'
import { readSeen, markSeen, mergeSeen, hasSeen, shouldMarkSkipped } from './lib/seen.js'
import {
  guardModel,
  isBannedModel,
  isAllowedModel,
  ALLOWED_MODEL,
  ALLOWED_MODELS,
  HARD_FALLBACK,
} from './lib/model-guard.js'
import { buildNoteMd, sanitizeTitle, fmtDur } from './lib/notes.js'
import { filterFresh, poolTargetFrom } from './lib/pool.js'
import {
  buildSearchParams,
  parseDuration,
  parsePlay,
  getPopularBriefs,
  getPlayerInfo,
  getComments,
  getSubtitleText,
} from './lib/bili.js'
import {
  buildHeaders,
  isRiskControl,
  decideRetry,
  retryDelayMs,
  riskControlError,
  fetchWithRetry,
  isBilibiliHost,
  redactUrl,
  redactSecrets,
  withTimeoutSignal,
  DEFAULT_MAX_RETRIES,
  FETCH_TIMEOUT_MS,
  LLM_TIMEOUT_MS,
} from './lib/http.js'
import { dedupeInflight } from './lib/inflight.js'
import {
  nextRequestAction,
  settleRequest,
  MAX_REQUEST_ATTEMPTS,
} from './lib/request-lifecycle.js'

const tests = []
function t(name, fn) {
  tests.push({ name, fn })
}

t('WBI 簽名：公開示例向量', () => {
  const imgKey = '7cd084941338484aae1ad9425b84077c'
  const subKey = '4932caff0ff746eab6f01bf08b70ac45'
  const params = { foo: 114, bar: 514, zab: 1919810 }
  const wts = 1702204169
  const { wRid, query } = encWbi(params, imgKey, subKey, wts)
  assert.equal(wRid, '8f6f2b5b3d485fe1886cec6a0be8c5d4')
  assert.equal(query, 'bar=514&foo=114&wts=1702204169&zab=1919810&w_rid=8f6f2b5b3d485fe1886cec6a0be8c5d4')
})

t('mixinKey 長度 32', () => {
  const key = getMixinKey('7cd084941338484aae1ad9425b84077c4932caff0ff746eab6f01bf08b70ac45')
  assert.equal(key.length, 32)
})

t('字幕 JSON → 帶時間戳文字', () => {
  const json = {
    body: [
      { from: 0, to: 3, content: '大家好' },
      { from: 192.5, to: 195, content: '  這是  第二句 ' },
      { from: 3700, to: 3702, content: '一小時後' },
    ],
  }
  const text = subtitleJsonToText(json, 0)
  assert.equal(text, '[00:00] 大家好\n[03:12] 這是 第二句\n[01:01:40] 一小時後')
})

t('secToTs 邊界', () => {
  assert.equal(secToTs(0), '00:00')
  assert.equal(secToTs(59), '00:59')
  assert.equal(secToTs(60), '01:00')
  assert.equal(secToTs(3600), '01:00:00')
})

t('字幕文字截斷', () => {
  const json = {
    body: [
      { from: 0, to: 1, content: 'A'.repeat(50) },
      { from: 1, to: 2, content: 'B'.repeat(50) },
    ],
  }
  const text = subtitleJsonToText(json, 60)
  assert.equal(text.split('\n').length, 1)
  assert.ok(text.startsWith('[00:00]'))
})

t('watched_level 由程式決定', () => {
  assert.equal(determineWatchedLevel({ hasSubtitle: true, hasDesc: false, hasComments: false }), 'subtitle')
  assert.equal(determineWatchedLevel({ hasSubtitle: false, hasDesc: true, hasComments: false }), 'desc_comments')
  assert.equal(determineWatchedLevel({ hasSubtitle: false, hasDesc: false, hasComments: true }), 'desc_comments')
  assert.equal(determineWatchedLevel({ hasSubtitle: false, hasDesc: false, hasComments: false }), 'title_only')
})

t('buildReport 補齊 metadata、強制 watched_level、去時間戳', () => {
  const meta = {
    bvid: 'BV1xx411c7mD', title: '測試', author: 'UP', durationSec: 120, play: 10,
    pubdate: '2024-01-01T00:00:00.000Z', tname: '科技', desc: '簡介',
    hasSubtitle: false, hasDesc: true, hasComments: false,
  }
  const content = {
    summary: '概要', highlights: ['03:12 某細節', '普通細節'], comment_vibe: '評論',
    top_comments: [{ text: 'x'.repeat(200), like: 5 }],
    take: '我喜歡', share_worthy: 'true', share_reason: '原因',
  }
  const r = buildReport(meta, content, 'req1', '2024-06-01T00:00:00.000Z')
  assert.equal(r.version, 1)
  assert.equal(r.watched_level, 'desc_comments')
  assert.equal(r.bvid, 'BV1xx411c7mD')
  assert.equal(r.url, 'https://www.bilibili.com/video/BV1xx411c7mD')
  assert.equal(r.highlights[0], '某細節') // 時間戳被剝掉
  assert.equal(r.share_worthy, true) // 'true' 被修正成布林
  assert.ok(r.top_comments[0].text.length <= 121)
  assert.equal(r.created_at, '2024-06-01T00:00:00.000Z')
  assert.equal(r.source_request, 'req1')
})

t('subtitle 等級保留時間戳', () => {
  const meta = {
    bvid: 'BV1', title: 't', author: 'a', durationSec: 1, play: 1,
    pubdate: '', tname: '', desc: '', hasSubtitle: true, hasDesc: false, hasComments: false,
  }
  const r = buildReport(meta, { highlights: ['03:12 某細節'] }, null, '2024-01-01T00:00:00.000Z')
  assert.equal(r.watched_level, 'subtitle')
  assert.equal(r.highlights[0], '03:12 某細節')
})

t('stripTimestamp 與 truncate', () => {
  assert.equal(stripTimestamp('[03:12] 內容'), '內容')
  assert.equal(stripTimestamp('03:12 - 內容'), '內容')
  assert.equal(truncate('a b  c', 3), 'a b…')
})

t('原子寫入：先 .tmp 再 rename', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pawbot-selftest-'))
  try {
    const file = join(dir, 'x.json')
    atomicWriteFile(file, '{"a":1}')
    assert.equal(readFileSync(file, 'utf8'), '{"a":1}')
    assert.equal(existsSync(file + '.tmp'), false)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

t('請求佇列掃描：排序 + 壞檔跳過', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pawbot-queue-'))
  try {
    writeFileSync(join(dir, 'b.json'), JSON.stringify({ id: 'b', kind: 'bvid', value: 'BV2', requested_at: '2024-01-02T00:00:00Z' }))
    writeFileSync(join(dir, 'a.json'), JSON.stringify({ id: 'a', kind: 'keyword', value: '貓', requested_at: '2024-01-01T00:00:00Z' }))
    writeFileSync(join(dir, 'bad.json'), '{ not json')
    writeFileSync(join(dir, 'note.txt'), 'hi')
    const list = scanRequests(dir)
    assert.equal(list.length, 2)
    assert.equal(list[0].id, 'a')
    assert.equal(list[1].id, 'b')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

t('extra 向後兼容：缺欄位 / 壞型別', () => {
  assert.deepEqual(normalizeExtra(undefined), { order: '', days: 0, num: 0 })
  assert.deepEqual(normalizeExtra(null), { order: '', days: 0, num: 0 })
  assert.deepEqual(normalizeExtra('bad'), { order: '', days: 0, num: 0 })
  assert.deepEqual(normalizeExtra({ order: 'pubdate', days: 3, num: 10 }), { order: 'pubdate', days: 3, num: 10 })
  assert.deepEqual(normalizeExtra({ order: '', days: -5, num: 0 }), { order: '', days: 0, num: 0 })
  assert.deepEqual(normalizeExtra({ order: ' click ', days: '7', num: '50' }), { order: 'click', days: 7, num: 50 })
})

t('Cookie 拼接：buvid 指紋 + SESSDATA', () => {
  assert.equal(composeCookieHeader({ buvid3: 'B3', buvid4: 'B4' }), 'buvid3=B3; buvid4=B4')
  assert.equal(
    composeCookieHeader({ buvid3: 'B3', buvid4: 'B4', sessdata: 'abc%3D' }),
    'buvid3=B3; buvid4=B4; SESSDATA=abc%3D',
  )
  assert.equal(composeCookieHeader(), '')
  assert.equal(composeCookieHeader({ buvid3: ' B3 ', buvid4: ' ', sessdata: '  ' }), 'buvid3=B3')
  // SESSDATA 排在最後，且不會重複
  assert.equal(
    composeCookieHeader({ buvid3: 'b3', buvid4: 'b4', sessdata: 'S' }),
    'buvid3=b3; buvid4=b4; SESSDATA=S',
  )
})

t('從 Cookie 字串取 SESSDATA', () => {
  assert.equal(sessdataFromCookie('SESSDATA=abc'), 'abc')
  assert.equal(sessdataFromCookie('a=1; SESSDATA=xyz; b=2'), 'xyz')
  assert.equal(sessdataFromCookie(''), '')
  assert.equal(sessdataFromCookie(undefined), '')
  assert.equal(sessdataFromCookie('SESSDATA='), '')
})

t('finger/spi 解析', () => {
  assert.deepEqual(parseFingerSpi({ code: 0, data: { b_3: 'x', b_4: 'y' } }), { buvid3: 'x', buvid4: 'y' })
  assert.deepEqual(parseFingerSpi({ data: { b_3: 'x' } }), { buvid3: 'x', buvid4: '' })
  assert.equal(parseFingerSpi({ code: -1, data: {} }), null)
  assert.equal(parseFingerSpi(null), null)
})

t('412 / 風控 403 判定', () => {
  assert.equal(isRiskControl(412, ''), true)
  assert.equal(isRiskControl(412, '<html>请求被拦截</html>'), true)
  assert.equal(isRiskControl(403, '触发风控，请稍后再试'), true)
  assert.equal(isRiskControl(403, '请求过于频繁'), true)
  assert.equal(isRiskControl(403, 'Access Denied'), false)
  assert.equal(isRiskControl(404, '风控'), false)
  assert.equal(isRiskControl(200, ''), false)
})

t('重試決策：風控最多重試 2 次', () => {
  const first = decideRetry({ status: 412, attempt: 0, random: () => 0 })
  assert.equal(first.retry, true)
  assert.equal(first.refreshBuvid, true)
  assert.equal(first.risk, true)
  assert.equal(first.delayMs, 800)

  const second = decideRetry({ status: 403, body: '触发风控，请稍后再试', attempt: 1 })
  assert.equal(second.retry, true)
  assert.equal(second.refreshBuvid, true)

  const third = decideRetry({ status: 412, attempt: DEFAULT_MAX_RETRIES })
  assert.equal(third.retry, false)
  assert.equal(third.refreshBuvid, false)
  assert.equal(third.risk, true)

  const plain403 = decideRetry({ status: 403, body: 'Access Denied', attempt: 0 })
  assert.equal(plain403.retry, false)
  assert.equal(plain403.risk, false)

  const server500 = decideRetry({ status: 500, attempt: 0 })
  assert.equal(server500.retry, false)
})

t('重試延遲落在 800~1500ms', () => {
  assert.equal(retryDelayMs(() => 0), 800)
  assert.equal(retryDelayMs(() => 1), 1500)
  const mid = retryDelayMs(() => 0.5)
  assert.ok(mid >= 800 && mid <= 1500, `mid=${mid}`)
  assert.ok(retryDelayMs(() => -1) >= 800)
  assert.ok(retryDelayMs(() => Number.NaN) >= 800)
})

t('風控錯誤訊息明確且不含憑證', () => {
  const e = riskControlError(412, 2)
  assert.equal(e.message, 'B站风控 412（已重试 2 次，可能是请求太频繁或缺 Cookie）')
  assert.equal(e.status, 412)
  assert.equal(e.attempts, 2)
  assert.equal(e.risk, true)
  assert.ok(!/buvid|SESSDATA/i.test(e.message))
})

t('請求標頭：zh-CN / Referer / Origin / UA / Cookie', () => {
  const h = buildHeaders('buvid3=b3; buvid4=b4')
  assert.match(h['User-Agent'], /Mozilla\/5\.0/)
  assert.match(h['Accept-Language'], /^zh-CN/)
  assert.equal(h.Referer, 'https://www.bilibili.com/')
  assert.equal(h.Origin, 'https://www.bilibili.com')
  assert.equal(h.Cookie, 'buvid3=b3; buvid4=b4')
  assert.equal(buildHeaders('').Cookie, undefined)
  assert.equal(buildHeaders('', { Referer: 'x' }).Referer, 'x')
})

t('Cookie 只送 bilibili.com（字幕 CDN 不送）', () => {
  assert.equal(isBilibiliHost('https://api.bilibili.com/x/web-interface/nav'), true)
  assert.equal(isBilibiliHost('https://www.bilibili.com/video/BV1xx'), true)
  assert.equal(isBilibiliHost('https://bilibili.com'), true)
  assert.equal(isBilibiliHost('https://aisubtitle.hdslb.com/bfs/subtitle/x.json'), false)
  assert.equal(isBilibiliHost('https://i0.hdslb.com/bfs/x.png'), false)
  assert.equal(isBilibiliHost('http://api.bilibili.com/x'), false)
  assert.equal(isBilibiliHost('not a url'), false)
})

t('cookie 管理器：快取、刷新、過期、dispose', async () => {
  let calls = 0
  let clock = 1000
  const mgr = createCookieManager({
    now: () => clock,
    ttlMs: 1000,
    fetchSpi: async () => {
      calls++
      return { code: 0, data: { b_3: `b3-${calls}`, b_4: `b4-${calls}` } }
    },
  })
  assert.equal(await mgr.getCookieHeader({ sessdata: 'S' }), 'buvid3=b3-1; buvid4=b4-1; SESSDATA=S')
  assert.equal(await mgr.getCookieHeader({}), 'buvid3=b3-1; buvid4=b4-1') // 快取命中
  assert.equal(calls, 1)

  const refreshed = await mgr.refresh()
  assert.equal(refreshed.buvid3, 'b3-2')
  assert.equal(calls, 2)

  clock += 1000 // 剛好過期
  assert.equal(await mgr.getCookieHeader({}), 'buvid3=b3-3; buvid4=b4-3')
  assert.equal(calls, 3)

  mgr.dispose()
  assert.equal(await mgr.getCookieHeader({ sessdata: 'S' }), 'SESSDATA=S')
  assert.equal(await mgr.getCookieHeader({}), '')
  assert.equal(calls, 3)
})

t('cookie 管理器：併發只抓一次、失敗只帶 SESSDATA', async () => {
  let calls = 0
  const mgr = createCookieManager({
    fetchSpi: async () => {
      calls++
      await new Promise((r) => setTimeout(r, 10))
      return { data: { b_3: 'a', b_4: 'b' } }
    },
  })
  const [h1, h2] = await Promise.all([mgr.getCookieHeader({}), mgr.getCookieHeader({})])
  assert.equal(h1, 'buvid3=a; buvid4=b')
  assert.equal(h2, h1)
  assert.equal(calls, 1)
  mgr.dispose()

  const failing = createCookieManager({ fetchSpi: async () => { throw new Error('boom') } })
  assert.equal(await failing.getCookieHeader({ sessdata: 'S' }), 'SESSDATA=S')
})

t('fetchWithRetry：412 刷新指紋、等待後重試成功', async () => {
  const seen = []
  const sleeps = []
  let refreshes = 0
  let calls = 0
  const fakeFetch = async (_url, opts) => {
    calls++
    seen.push(opts)
    if (calls <= 2) return new Response('<html>请求被拦截</html>', { status: 412 })
    return new Response(JSON.stringify({ code: 0, ok: 1 }), { status: 200 })
  }
  const out = await fetchWithRetry('https://api.bilibili.com/x/web-interface/search/type?x=1', {
    cookieManager: {
      getCookieHeader: async ({ sessdata }) => 'buvid3=b3; buvid4=b4' + (sessdata ? '; SESSDATA=' + sessdata : ''),
      refresh: async () => { refreshes++ },
    },
    sessdata: 'S',
    fetchImpl: fakeFetch,
    sleepImpl: async (ms) => { sleeps.push(ms) },
    random: () => 0,
  })
  assert.deepEqual(out, { code: 0, ok: 1 })
  assert.equal(calls, 3)
  assert.equal(refreshes, 2) // 每次風控都刷新指紋
  assert.deepEqual(sleeps, [800, 800])
  assert.equal(seen[0].headers.Cookie, 'buvid3=b3; buvid4=b4; SESSDATA=S')
  assert.match(seen[0].headers['Accept-Language'], /^zh-CN/)
  assert.equal(seen[0].headers.Referer, 'https://www.bilibili.com/')
  assert.equal(seen[0].headers.Origin, 'https://www.bilibili.com')
})

t('fetchWithRetry：重試耗盡拋明確風控錯誤', async () => {
  let calls = 0
  let sleeps = 0
  let refreshes = 0
  await assert.rejects(
    () =>
      fetchWithRetry('https://api.bilibili.com/x/web-interface/search/type?x=1', {
        cookieManager: {
          getCookieHeader: async () => 'buvid3=b3',
          refresh: async () => { refreshes++ },
        },
        fetchImpl: async () => {
          calls++
          return new Response('请求被拦截', { status: 412 })
        },
        sleepImpl: async () => { sleeps++ },
        random: () => 0,
      }),
    (e) => {
      assert.equal(e.name, 'BiliRiskControlError')
      assert.equal(e.status, 412)
      assert.equal(e.attempts, 2)
      assert.match(e.message, /^B站风控 412（已重试 2 次，可能是请求太频繁或缺 Cookie）$/)
      return true
    },
  )
  assert.equal(calls, 3) // 首發 + 2 次重試
  assert.equal(sleeps, 2)
  assert.equal(refreshes, 2)
})

t('fetchWithRetry：一般 403 不重試、直接拋 HTTP 錯誤', async () => {
  let calls = 0
  await assert.rejects(
    () =>
      fetchWithRetry('https://api.bilibili.com/x', {
        fetchImpl: async () => {
          calls++
          return new Response('Access Denied', { status: 403 })
        },
        sleepImpl: async () => {
          throw new Error('不該進到重試等待')
        },
      }),
    /HTTP 403/,
  )
  assert.equal(calls, 1)
})

/* ── 新增：憑證多來源 ── */
t('憑證優先序：env > file > config', () => {
  const fileCred = { sessdata: 'FILE_TOKEN', updated_at: '2024-01-01T00:00:00Z' }
  const configSessdata = 'CONFIG_TOKEN'
  assert.deepEqual(
    resolveCredential({ env: { BILI_SESSDATA: 'ENV_TOKEN' }, cookieEnv: 'BILI_SESSDATA', fileCredential: fileCred, configSessdata }),
    { sessdata: 'ENV_TOKEN', source: 'env', has: true },
  )
  assert.deepEqual(
    resolveCredential({ env: {}, cookieEnv: 'BILI_SESSDATA', fileCredential: fileCred, configSessdata }),
    { sessdata: 'FILE_TOKEN', source: 'file', has: true },
  )
  assert.deepEqual(
    resolveCredential({ env: {}, cookieEnv: 'BILI_SESSDATA', fileCredential: null, configSessdata }),
    { sessdata: 'CONFIG_TOKEN', source: 'config', has: true },
  )
  assert.deepEqual(
    resolveCredential({ env: {}, cookieEnv: 'BILI_SESSDATA', fileCredential: null, configSessdata: '' }),
    { sessdata: '', source: null, has: false },
  )
  assert.deepEqual(
    resolveCredential({ env: { MY_COOKIE: 'X' }, cookieEnv: 'MY_COOKIE', fileCredential: null, configSessdata: '' }),
    { sessdata: 'X', source: 'env', has: true },
  )
})

t('credential.json 解析：壞 JSON / 缺 sessdata 回 null', () => {
  assert.deepEqual(parseCredentialFile('{"sessdata":"abc","updated_at":"2024-01-01T00:00:00Z"}'), {
    sessdata: 'abc',
    updated_at: '2024-01-01T00:00:00Z',
  })
  assert.equal(parseCredentialFile('{ not json'), null)
  assert.equal(parseCredentialFile('{"sessdata":""}'), null)
  assert.equal(parseCredentialFile('{"updated_at":"x"}'), null)
  assert.equal(parseCredentialFile(''), null)
  assert.equal(parseCredentialFile(null), null)
})

t('憑證狀態不洩漏 sessdata 值', () => {
  const status = credentialStatus({ sessdata: 'SUPER-SECRET-TOKEN', source: 'env', has: true })
  assert.deepEqual(status, { has: true, source: 'env' })
  assert.ok(!JSON.stringify(status).includes('SUPER-SECRET-TOKEN'))
  assert.ok(!JSON.stringify(status).includes('SESSDATA'))
})

/* ── 新增：搜尋參數 ── */
t('搜尋參數拼接：order / days / pageSize', () => {
  const p1 = buildSearchParams({ keyword: '貓', order: 'pubdate', days: 0, pageSize: 20, page: 1 })
  assert.equal(p1.search_type, 'video')
  assert.equal(p1.keyword, '貓')
  assert.equal(p1.order, 'pubdate')
  assert.equal(p1.page, 1)
  assert.equal(p1.page_size, 20)
  assert.ok(!('pubtime_begin_s' in p1))

  const p2 = buildSearchParams({ keyword: '狗', order: 'click', days: 3, pageSize: 30 })
  assert.equal(p2.order, 'click')
  assert.equal(p2.page_size, 30)
  assert.ok(p2.pubtime_begin_s > 0)
  assert.ok(p2.pubtime_end_s > p2.pubtime_begin_s)
  assert.equal(p2.pubtime_end_s - p2.pubtime_begin_s, 3 * 86400)

  const p3 = buildSearchParams({ keyword: 'x', order: 'bogus', days: 0 })
  assert.ok(!('order' in p3)) // 白名單外不帶

  const p4 = buildSearchParams({ keyword: 'x', days: 0 })
  assert.equal(p4.order, 'totalrank') // 預設
})

t('時長 / 播放量解析', () => {
  assert.equal(parseDuration('12:34'), 754)
  assert.equal(parseDuration('01:02:03'), 3723)
  assert.equal(parseDuration('0'), 0)
  assert.equal(parseDuration(''), 0)
  assert.equal(parseDuration(90), 90)
  assert.equal(parsePlay(12345), 12345)
  assert.equal(parsePlay('1.2万'), 12000)
  assert.equal(parsePlay('3.4亿'), 340000000)
  assert.equal(parsePlay('abc'), 0)
})

/* ── 新增：候選池去重 ── */
t('候選池去重 + skippedSeen', () => {
  const seen = { BV1: { seen_at: '2024-01-01T00:00:00Z', last_kind: 'watched' } }
  const shared = { BV2: { shared_at: '2024-01-01T00:00:00Z' } }
  const pool = new Set(['BV3'])
  const briefs = [
    { bvid: 'BV1', title: '看過' },
    { bvid: 'BV2', title: '分享過' },
    { bvid: 'BV3', title: '池內已有' },
    { bvid: 'BV4', title: '新鮮' },
    { bvid: 'BV4', title: '重複' },
    { bvid: '', title: '無 bvid' },
  ]
  const r = filterFresh(briefs, { seen, shared, pool, hasRecent: () => false })
  assert.equal(r.skippedSeen, 2) // BV1(看過) + BV2(分享過)；BV3 池內重複不算 skip
  assert.equal(r.fresh.length, 1)
  assert.equal(r.fresh[0].bvid, 'BV4')

  const r2 = filterFresh([{ bvid: 'BV9' }], { seen: {}, shared: {}, hasRecent: () => true })
  assert.equal(r2.skippedSeen, 1)
  assert.equal(r2.fresh.length, 0)
})

t('候選池目標：Math.max(5, searchNum||20)', () => {
  assert.equal(poolTargetFrom(20), 20)
  assert.equal(poolTargetFrom(3), 5)
  assert.equal(poolTargetFrom(0), 20)
  assert.equal(poolTargetFrom(undefined), 20)
})

/* ── 新增：seen.json ── */
t('mergeSeen 純函數：保留時間戳、記錄 last_kind', () => {
  const a = mergeSeen({}, 'BV1', 'watched', '2024-01-01T00:00:00Z')
  assert.equal(a.BV1.watched_at, '2024-01-01T00:00:00Z')
  assert.equal(a.BV1.last_kind, 'watched')
  const b = mergeSeen(a, 'BV1', 'shared', '2024-01-02T00:00:00Z')
  assert.equal(b.BV1.watched_at, '2024-01-01T00:00:00Z') // 不被覆蓋
  assert.equal(b.BV1.shared_at, '2024-01-02T00:00:00Z')
  assert.equal(b.BV1.last_kind, 'shared')
  assert.deepEqual(mergeSeen(null, '', 'watched'), {}) // 空 bvid 不改
})

t('seen.json 讀寫（臨時目錄）', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pawbot-seen-'))
  try {
    assert.deepEqual(readSeen(dir), {})
    markSeen(dir, 'BV1', 'watched', '2024-01-01T00:00:00Z')
    const s = readSeen(dir)
    assert.equal(s.BV1.last_kind, 'watched')
    assert.equal(s.BV1.watched_at, '2024-01-01T00:00:00Z')
    markSeen(dir, 'BV1', 'shared', '2024-01-02T00:00:00Z')
    const s2 = readSeen(dir)
    assert.equal(s2.BV1.watched_at, '2024-01-01T00:00:00Z') // 保留
    assert.equal(s2.BV1.shared_at, '2024-01-02T00:00:00Z')
    assert.equal(hasSeen(s2, 'BV1'), true)
    assert.equal(hasSeen(s2, 'BV2'), false)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

/* ── 新增：人讀 md 模板 ── */
t('md 模板：六段結構', () => {
  const report = {
    bvid: 'BV1xx411c7mD', title: '測試：標題/帶斜線', author: '某UP', play: 10000, duration_sec: 125,
    url: 'https://www.bilibili.com/video/BV1xx411c7mD',
    summary: '這條在講 A 和 B。', highlights: ['關鍵1', '關鍵2', '關鍵3'],
    share_reason: '值得看', take: '我覺得不錯',
  }
  const md = buildNoteMd(report)
  assert.ok(md.startsWith('# 測試：標題/帶斜線'))
  assert.ok(md.includes('> 這條在講 A 和 B。'))
  assert.ok(md.includes('- UP：某UP / 链接：https://www.bilibili.com/video/BV1xx411c7mD / 播放量：10000 / 时长：2:05'))
  assert.ok(md.includes('## 讲了什么'))
  assert.ok(md.includes('## 关键点'))
  assert.ok(md.includes('- 關鍵1'))
  assert.ok(md.includes('## 为什么值得看'))
  assert.ok(md.includes('## 它怎么说'))
  assert.ok(md.includes('我覺得不錯'))
})

t('md 缺欄位有兜底', () => {
  const md = buildNoteMd({ bvid: 'BV1', title: '', author: '', play: 0, duration_sec: 0 })
  assert.ok(md.includes('# （无标题）'))
  assert.ok(md.includes('- UP：未知 / 链接：https://www.bilibili.com/video/BV1 / 播放量：0 / 时长：0:00'))
  assert.ok(md.includes('- （无）'))
})

t('檔名淨化：非法字符換 -、保留中文', () => {
  assert.equal(sanitizeTitle('a/b\\c:d*e?f"g<h>i|j'), 'a-b-c-d-e-f-g-h-i-j')
  assert.equal(sanitizeTitle(' 繁體中文標題 '), '繁體中文標題')
  assert.equal(sanitizeTitle(''), '')
})

t('時長格式化', () => {
  assert.equal(fmtDur(125), '2:05')
  assert.equal(fmtDur(3723), '1:02:03')
  assert.equal(fmtDur(0), '0:00')
})

/* ── 新增：模型白名單（P0-1） ── */
t('模型守門（白名單）：非 deepseek-flash 的 12 種寫法一律改寫，只有 flash 原樣通過', () => {
  const bad = [
    'deepseek-v4.1-pro', // 舊黑名單漏掉的寫法
    'deepseek-pro', // 舊黑名單漏掉
    'deepseek-v4-0528-pro', // 舊黑名單漏掉
    'deepseek-v4.0-pro', // 舊黑名單漏掉
    'v4–pro', // en dash，舊黑名單漏掉
    'deepseek-v4-pro',
    'deepseek-v4_pro',
    'V4-Pro',
    'v4 pro',
    'deepseek-v4pro',
    'gemini-4-argon', // 非 flash 的別家模型：舊版原樣放行
    'claude-3-opus', // 同上
    'deepseek-flash-2', // 名字裡有 flash 但不在白名單，不准放行
    'DeepSeek-Flash', // 大小寫不同也不算同一個名字
  ]
  assert.ok(bad.length >= 10, '至少列 10 個變體')
  for (const name of bad) {
    const g = guardModel(name)
    assert.equal(g.model, HARD_FALLBACK, name + ' 應改用 HARD_FALLBACK')
    assert.equal(g.changed, true, name + ' 應標記 changed')
    assert.equal(g.reason, 'not-allowed', name + ' 的 reason 應是 not-allowed')
    assert.equal(g.requested, name)
    assert.equal(isBannedModel(name), true, name + ' 不在白名單')
    assert.equal(isAllowedModel(name), false, name + ' 不應被視為允許')
  }

  // 白名單只有一個名字（要加別名只能在 ALLOWED_MODELS 明確列舉）
  assert.deepEqual([...ALLOWED_MODELS], ['deepseek-flash'])
  assert.equal(ALLOWED_MODEL, 'deepseek-flash')
  assert.equal(HARD_FALLBACK, 'deepseek-flash')

  // 唯一放行的名字：原樣通過
  assert.deepEqual(guardModel('deepseek-flash'), {
    model: 'deepseek-flash',
    changed: false,
    requested: 'deepseek-flash',
    allowed: 'deepseek-flash',
    reason: 'ok',
  })
  assert.equal(isAllowedModel('deepseek-flash'), true)
  assert.equal(isBannedModel('deepseek-flash'), false)

  // 空值 / 全空白：改寫成 HARD_FALLBACK（reason 用 empty 區分「沒填」）
  for (const empty of ['', '   ', undefined, null]) {
    const g = guardModel(empty)
    assert.equal(g.model, HARD_FALLBACK)
    assert.equal(g.changed, true)
    assert.equal(g.reason, 'empty')
  }
})

t('模型守門：allowedModel 本身也必須在白名單，否則退回 HARD_FALLBACK', () => {
  const g = guardModel('deepseek-flash', 'deepseek-v4-pro')
  assert.equal(g.allowed, HARD_FALLBACK, 'allowed 違規時 allowed 自身也要被收斂')
  assert.equal(g.model, 'deepseek-flash') // requested 合法就照 requested
  assert.equal(g.changed, false)

  const g2 = guardModel('gemini-4-argon', 'gemini-4-argon')
  assert.equal(g2.model, HARD_FALLBACK)
  assert.equal(g2.allowed, HARD_FALLBACK)
  assert.equal(g2.changed, true)
  assert.equal(g2.reason, 'not-allowed')

  const g3 = guardModel('deepseek-flash', '')
  assert.equal(g3.allowed, HARD_FALLBACK)
  assert.equal(g3.model, 'deepseek-flash')
})

/* ── 新增：in-flight 去重（P1-2） ── */
t('in-flight 去重：同一 bvid 併發只跑一次，結束後才允許重跑', async () => {
  const inflight = new Map()
  let calls = 0
  let release
  const gate = new Promise((resolve) => {
    release = resolve
  })
  const task = async () => {
    calls++
    await gate
    return { ok: true, calls }
  }

  const p1 = dedupeInflight(inflight, 'BV1', task)
  const p2 = dedupeInflight(inflight, 'BV1', task)
  assert.equal(p1, p2, '後到的呼叫要拿到同一個 Promise')
  await new Promise((r) => setTimeout(r, 0))
  assert.equal(calls, 1, '同一個 bvid 只能跑一次（不會兩次 LLM）')
  assert.equal(inflight.size, 1)

  release()
  const [r1, r2] = await Promise.all([p1, p2])
  assert.deepEqual(r1, { ok: true, calls: 1 })
  assert.deepEqual(r2, r1)
  assert.equal(inflight.size, 0, '跑完要從表裡移除，下一輪才能重跑')

  const r3 = await dedupeInflight(inflight, 'BV1', async () => {
    calls++
    return { ok: true, calls }
  })
  assert.equal(r3.calls, 2, '結束後再呼叫要真的重跑')
  assert.equal(inflight.size, 0)

  // 不同 bvid 可以併發；失敗也要清掉，不能卡住
  const inflight2 = new Map()
  let ran = 0
  await Promise.all([
    dedupeInflight(inflight2, 'BV2', async () => {
      ran++
    }),
    dedupeInflight(inflight2, 'BV3', async () => {
      ran++
    }),
  ])
  assert.equal(ran, 2)
  await assert.rejects(
    () =>
      dedupeInflight(inflight2, 'BV4', async () => {
        throw new Error('boom')
      }),
    /boom/,
  )
  assert.equal(inflight2.has('BV4'), false, '失敗後也要移除 in-flight 標記')
})

/* ── 新增：壞請求檔隔離（P2-8） ── */
t('壞掉的請求檔：scanRequestsDetailed 回報 bad，quarantineBad 搬到 requests/bad 只隔離一次', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pawbot-queue-bad-'))
  try {
    writeFileSync(
      join(dir, 'good.json'),
      JSON.stringify({ id: 'g', kind: 'bvid', value: 'BV1', requested_at: '2024-01-01T00:00:00Z' }),
    )
    writeFileSync(join(dir, 'bad.json'), '{ not json')
    writeFileSync(join(dir, 'array.json'), '[]')
    writeFileSync(join(dir, 'note.txt'), 'hi')

    const { ok: good, bad } = scanRequestsDetailed(dir)
    assert.equal(good.length, 1)
    assert.equal(good[0].id, 'g')
    assert.equal(bad.length, 2, '兩個壞檔都要回報（不再靜默）')
    assert.ok(bad.every((b) => b.file && b.error))
    assert.equal(scanRequests(dir).length, 1, '舊形狀 scanRequests 仍只回可用請求')

    const q = quarantineBad(dir, bad)
    assert.equal(q.moved.length, 2)
    assert.equal(existsSync(join(dir, 'bad')), true)
    assert.equal(existsSync(join(dir, 'bad', 'bad.json')), true)
    assert.equal(existsSync(join(dir, 'bad', 'array.json')), true)
    assert.equal(existsSync(join(dir, 'bad.json')), false)

    const again = scanRequestsDetailed(dir)
    assert.equal(again.bad.length, 0, '隔離後不該每 30 秒再被掃到')
    assert.equal(again.ok.length, 1)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

/* ── 新增：請求失敗重試 / 隔離到 failed（P1-5） ── */
t('請求失敗：第 1 次留 requests/ 重試、第 2 次進 requests/failed（含 attempts/last_error 且去敏）', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pawbot-req-'))
  try {
    const requests = join(dir, 'requests')
    const done = join(requests, 'done')
    const failed = join(requests, 'failed') // 故意不先建：settleRequest 要自己 mkdirSync
    mkdirSync(requests, { recursive: true })

    const file = join(requests, 'req1.json')
    writeFileSync(
      file,
      JSON.stringify({ id: 'r1', kind: 'bvid', value: 'BV1', requested_at: '2024-01-01T00:00:00Z' }),
    )

    // 錯誤訊息裡帶著 auth_key：寫進檔前必須去敏
    const badOutcome = { ok: false, error: 'view 介面拿不到這條影片（https://x/y?auth_key=SECRET）' }

    const first = settleRequest({
      file,
      outcome: badOutcome,
      attempts: 0,
      doneDir: done,
      failedDir: failed,
      now: () => '2024-01-01T00:00:00Z',
    })
    assert.equal(first.action, 'retry', '第 1 次失敗要留著重試')
    assert.equal(first.attempts, 1)
    assert.equal(existsSync(file), true, '第 1 次失敗不能進 done/')
    assert.equal(existsSync(join(done, 'req1.json')), false)
    const kept = JSON.parse(readFileSync(file, 'utf8'))
    assert.equal(kept.attempts, 1)
    assert.match(kept.last_error, /view 介面拿不到這條影片/)
    assert.ok(!kept.last_error.includes('SECRET'), 'last_error 不能帶 auth_key')
    assert.equal(kept.id, 'r1', '改寫 attempts 時不能弄丟原欄位')

    const second = settleRequest({
      file,
      outcome: badOutcome,
      attempts: kept.attempts,
      doneDir: done,
      failedDir: failed,
      now: () => '2024-01-01T00:05:00Z',
    })
    assert.equal(second.action, 'failed', '第 2 次失敗進 failed/')
    assert.equal(second.attempts, 2)
    assert.equal(existsSync(file), false)
    assert.equal(existsSync(failed), true, 'requests/failed/ 要由程式建立')
    const moved = JSON.parse(readFileSync(join(failed, 'req1.json'), 'utf8'))
    assert.equal(moved.attempts, 2)
    assert.equal(moved.id, 'r1')
    assert.ok(!JSON.stringify(moved).includes('SECRET'))

    // 成功 → done/
    const okFile = join(requests, 'req2.json')
    writeFileSync(okFile, JSON.stringify({ id: 'r2', kind: 'bvid', value: 'BV2' }))
    const third = settleRequest({ file: okFile, outcome: { ok: true }, attempts: 0, doneDir: done, failedDir: failed })
    assert.equal(third.action, 'done')
    assert.equal(existsSync(join(done, 'req2.json')), true)
    assert.equal(existsSync(okFile), false)

    // 純函數：明確終點（skipped）不重試；只有失敗才 retry/failed
    assert.equal(MAX_REQUEST_ATTEMPTS, 2)
    assert.equal(nextRequestAction({ ok: true }, 0), 'done')
    assert.equal(nextRequestAction({ ok: false, skipped: true, reason: 'recent' }, 0), 'done')
    assert.equal(nextRequestAction({ ok: false, skipped: true, reason: 'too_long' }, 0), 'done')
    assert.equal(nextRequestAction({ ok: false, error: 'x' }, 0), 'retry')
    assert.equal(nextRequestAction({ ok: false, error: 'x' }, 1), 'failed')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

/* ── 新增：seen 記 skipped（P1-6） ── */
t('seen：too_long / 明確失敗記 skipped，pickUnseen/filterFresh 會跳過；recent 不記', () => {
  assert.equal(shouldMarkSkipped({ ok: false, skipped: true, reason: 'too_long' }), true)
  assert.equal(shouldMarkSkipped({ ok: false, error: 'view 介面拿不到這條影片' }), true)
  assert.equal(shouldMarkSkipped({ ok: false, error: 'LLM 兩次都失敗，跳過' }), true)
  assert.equal(shouldMarkSkipped({ ok: false, skipped: true, reason: 'recent' }), false, 'TTL 過了要能再看')
  assert.equal(shouldMarkSkipped({ ok: false, error: '已中止', aborted: true }), false, 'dispose 中止不算片的問題')
  assert.equal(shouldMarkSkipped({ ok: true, report: {} }), false)

  const dir = mkdtempSync(join(tmpdir(), 'pawbot-seen-skip-'))
  try {
    markSeen(dir, 'BV9', 'skipped', '2024-01-01T00:00:00Z', { reason: 'too_long' })
    const seen = readSeen(dir)
    assert.equal(seen.BV9.last_kind, 'skipped')
    assert.equal(seen.BV9.skipped_at, '2024-01-01T00:00:00Z')
    assert.equal(seen.BV9.skip_reason, 'too_long')
    assert.equal(seen.BV9.watched_at, undefined, 'skipped 不是 watched')

    // filterFresh / pickUnseen 看的是「seen 有沒有這個 bvid」→ 直接跳過
    const r = filterFresh(
      [
        { bvid: 'BV9', title: '太長' },
        { bvid: 'BV8', title: '新的' },
      ],
      { seen, shared: {}, hasRecent: () => false },
    )
    assert.equal(r.skippedSeen, 1)
    assert.deepEqual(r.fresh.map((x) => x.bvid), ['BV8'])
    assert.equal(hasSeen(seen, 'BV9'), true)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

/* ── 新增：錯誤訊息去敏（P2-7） ── */
t('錯誤訊息/日誌去敏：URL 只留 origin + pathname，auth_key / SESSDATA 被抹掉', () => {
  const url = 'https://aisubtitle.hdslb.com/bfs/subtitle/123.json?auth_key=1700000000-SECRET-0-abc'
  assert.equal(redactUrl(url), 'https://aisubtitle.hdslb.com/bfs/subtitle/123.json')
  assert.ok(!redactUrl(url).includes('SECRET'))
  assert.equal(
    redactUrl('https://api.bilibili.com/x/web-interface/search/type?w_rid=1&keyword=%E7%8C%AB'),
    'https://api.bilibili.com/x/web-interface/search/type',
  )
  assert.equal(redactUrl('not a url?x=1'), 'not a url')
  assert.equal(
    redactSecrets('fetch https://x/y?auth_key=SECRET failed; SESSDATA=abc'),
    'fetch https://x/y?auth_key=*** failed; SESSDATA=***',
  )
})

t('fetchWithRetry：一般 HTTP 錯誤訊息只帶去敏 URL（不含 query/auth_key）', async () => {
  await assert.rejects(
    () =>
      fetchWithRetry('https://api.bilibili.com/x/player?auth_key=SECRET&x=1', {
        fetchImpl: async () => new Response('nope', { status: 500 }),
        sleepImpl: async () => {},
      }),
    (e) => {
      assert.equal(e.message, 'HTTP 500：https://api.bilibili.com/x/player')
      assert.ok(!e.message.includes('SECRET'))
      return true
    },
  )
})

t('字幕下載失敗：錯誤訊息不含 auth_key（只留 origin + pathname）', async () => {
  const realFetch = globalThis.fetch
  try {
    globalThis.fetch = async (url) => {
      if (String(url).includes('hdslb.com')) return new Response('nope', { status: 500 })
      throw new Error('unexpected fake fetch url: ' + url)
    }
    await assert.rejects(
      () => getSubtitleText({ url: '//aisubtitle.hdslb.com/bfs/subtitle/x.json?auth_key=SECRET' }, {}),
      (e) => {
        assert.ok(!e.message.includes('SECRET'), '不能帶 auth_key 的值')
        assert.ok(!e.message.includes('auth_key'), '不能帶 auth_key 參數')
        assert.equal(e.message, 'HTTP 500：https://aisubtitle.hdslb.com/bfs/subtitle/x.json')
        return true
      },
    )
  } finally {
    globalThis.fetch = realFetch
  }
})

/* ── 新增：timeout（P1-3） ── */
t('timeout：預設 15 秒 / LLM 90 秒，withTimeoutSignal 準時中止且呼叫方 abort 會傳遞', async () => {
  assert.equal(FETCH_TIMEOUT_MS, 15000)
  assert.equal(LLM_TIMEOUT_MS, 90000)

  const t1 = withTimeoutSignal(undefined, 30)
  assert.ok(t1.signal)
  assert.equal(t1.signal.aborted, false)
  await new Promise((r) => setTimeout(r, 60))
  assert.equal(t1.signal.aborted, true, 'timeout 到了要中止')
  t1.cleanup()

  const pre = new AbortController()
  pre.abort(new Error('pre-aborted'))
  const t2 = withTimeoutSignal(pre.signal, 30)
  assert.equal(t2.signal.aborted, true, '呼叫方已 abort 要立刻反映')
  t2.cleanup()

  const caller = new AbortController()
  const t3 = withTimeoutSignal(caller.signal, 60_000)
  assert.equal(t3.signal.aborted, false)
  caller.abort(new Error('stop'))
  assert.equal(t3.signal.aborted, true, '呼叫方 abort 要傳到疊起來的 signal')
  t3.cleanup()
})

t('fetchWithRetry：fetch hang 住會被 timeout 中止（不會讓 busy 永遠 true）', async () => {
  // AbortSignal.timeout 的 timer 是 unref 的：測試裡沒有其他 handle 時，
  // 事件迴圈會在 timeout 前就清空。用一個 ref 的 interval 把迴圈撐住。
  const keepAlive = setInterval(() => {}, 1000)
  try {
    let seenSignal = null
    const hangingFetch = (_url, { signal }) => {
      seenSignal = signal
      return new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason ?? new Error('aborted')), { once: true })
      })
    }
    const started = Date.now()
    await assert.rejects(
      () =>
        fetchWithRetry('https://api.bilibili.com/x/whatever?auth_key=SECRET', {
          fetchImpl: hangingFetch,
          timeoutMs: 30,
        }),
      (e) => {
        assert.match(String(e?.message || ''), /timeout|aborted/i)
        return true
      },
    )
    assert.ok(Date.now() - started < 5000, '不該真的等 15 秒')
    assert.ok(seenSignal?.aborted, '傳進 fetch 的 signal 應被 timeout 中止')
  } finally {
    clearInterval(keepAlive)
  }
})

t('fetchWithRetry：呼叫方已 abort 就不發請求、也不拿同一個 aborted signal 重試', async () => {
  const c = new AbortController()
  c.abort(new Error('dispose'))
  let calls = 0
  await assert.rejects(
    () =>
      fetchWithRetry('https://api.bilibili.com/x/whatever', {
        signal: c.signal,
        fetchImpl: async () => {
          calls++
          return new Response('{}', { status: 200 })
        },
      }),
    /dispose/,
  )
  assert.equal(calls, 0, '已 abort 不該再發請求')
})

/* ── 新增：三個 B 站介面要看 code（P1-4） ── */
t('B站介面 code !== 0 要拋錯（popular/player/comments 不能回空陣列假裝沒有）', async () => {
  const realFetch = globalThis.fetch
  const jsonResponse = (obj, status = 200) =>
    new Response(JSON.stringify(obj), { status, headers: { 'content-type': 'application/json' } })
  const wbi = {
    code: 0,
    data: {
      wbi_img: {
        img_url: 'https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png',
        sub_url: 'https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png',
      },
    },
  }
  try {
    globalThis.fetch = async (url) => {
      const u = String(url)
      if (u.includes('/x/frontend/finger/spi')) return jsonResponse({ code: 0, data: { b_3: 'b3', b_4: 'b4' } })
      if (u.includes('/x/web-interface/nav')) return jsonResponse(wbi)
      if (u.includes('/x/web-interface/popular')) return jsonResponse({ code: -412, message: '请求被拦截' })
      if (u.includes('/x/player/wbi/v2')) return jsonResponse({ code: -404, message: '啥都木有' })
      if (u.includes('/x/v2/reply/main')) return jsonResponse({ code: -400, message: '请求错误' })
      throw new Error('unexpected fake fetch url: ' + u)
    }

    await assert.rejects(
      () => getPopularBriefs({}),
      (e) => {
        assert.equal(e.name, 'BiliRiskControlError', '熱門榜被風控要拋風控錯誤')
        assert.equal(e.status, 412)
        return true
      },
    )
    await assert.rejects(() => getPlayerInfo('BV1xx411c7mD', 123, {}), /player 接口 code=-404/)
    await assert.rejects(() => getComments(123, {}), /评论接口 code=-400/)

    // code === 0 時行為不變
    globalThis.fetch = async (url) => {
      const u = String(url)
      if (u.includes('/x/frontend/finger/spi')) return jsonResponse({ code: 0, data: { b_3: 'b3', b_4: 'b4' } })
      if (u.includes('/x/web-interface/popular'))
        return jsonResponse({ code: 0, data: { list: [{ bvid: 'BV1', title: ' 標題 ' }] } })
      throw new Error('unexpected fake fetch url: ' + u)
    }
    const list = await getPopularBriefs({})
    assert.deepEqual(list, [{ bvid: 'BV1', title: '標題' }])
  } finally {
    globalThis.fetch = realFetch
  }
})

let failed = 0
for (const { name, fn } of tests) {
  try {
    await fn()
    console.log('✓ ' + name)
  } catch (e) {
    failed++
    console.error('✗ ' + name + '\n    ' + (e?.message || e))
  }
}
if (failed) {
  console.error(`\n${failed} / ${tests.length} 個測試失敗`)
  process.exit(1)
}
console.log(`\n全部 ${tests.length} 個測試通過 ✓`)
