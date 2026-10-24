/**
 * test/offline.mjs —— 離線單測（不用真的起 PawBot / DSH）。
 * 用法：`node test/offline.mjs`
 * 全綠 → exit 0；有紅 → exit 1。
 *
 * 覆蓋：(a) 遮罩 secrets (b) 遮罩值不會被寫回 (c) 深合併 (d) section 過濾
 *       (e) 大檔案拒絕 + proxy 動作（假 fetch）的讀/寫/發送。
 */
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import {
  MASK,
  maskSecrets,
  pickUpdatable,
  deepMerge,
  pickSection,
  diffKeys,
  PERSONA_MAX_BYTES,
} from '../lib/config-utils.js'
import { createPawbotProxy, PERSONA_API_PATH } from '../lib/webui-proxy.js'

let passed = 0
let failed = 0
async function t(name, fn) {
  try {
    await fn()
    passed += 1
    console.log(`  ✔ ${name}`)
  } catch (e) {
    failed += 1
    console.log(`  ✘ ${name}`)
    const msg = String((e && e.message) || e).split('\n').join('\n      ')
    console.log(`      ${msg}`)
  }
}

console.log('== config-utils 純函數 ==')

// (a) 遮罩
await t('(a) maskSecrets 遮掉 api_key / token / access_token', () => {
  const src = {
    llm: { api_key: 'sk-123', vision_api_key: 'sk-456', model: 'deepseek-chat', max_tokens: 900 },
    server: { host: '0.0.0.0', access_token: 'napcat-secret', port: 6199 },
    webui: { host: '127.0.0.1', token: 'w-token', port: 6185 },
    tools: { search_api_key: 'sk-search', pixiv_refresh_token: 'pix-tok', pixiv_sessid: 'sess-1' },
  }
  const { value, maskedKeys } = maskSecrets(src)
  assert.equal(value.llm.api_key, MASK)
  assert.equal(value.llm.vision_api_key, MASK)
  assert.equal(value.server.access_token, MASK)
  assert.equal(value.webui.token, MASK)
  assert.equal(value.tools.search_api_key, MASK)
  assert.equal(value.tools.pixiv_refresh_token, MASK)
  assert.equal(value.tools.pixiv_sessid, MASK)
  // 非 secret 不遮
  assert.equal(value.llm.model, 'deepseek-chat')
  assert.equal(value.llm.max_tokens, 900)
  assert.equal(value.webui.port, 6185)
  assert.equal(value.server.host, '0.0.0.0')
  // masked_keys 是 dotted path
  assert.ok(maskedKeys.includes('llm.api_key'), '應含 llm.api_key')
  assert.ok(maskedKeys.includes('server.access_token'), '應含 server.access_token')
  assert.ok(maskedKeys.includes('webui.token'), '應含 webui.token')
  assert.ok(!maskedKeys.includes('llm.max_tokens'), '不應把 max_tokens 當 secret')
  // 不污染原物件
  assert.equal(src.llm.api_key, 'sk-123')
})

// (b) 遮罩值不會被寫回
await t('(b) pickUpdatable 丟掉遮罩值', () => {
  const patch = {
    llm: { api_key: MASK, temperature: 0.7 },
    server: { access_token: MASK, port: 6199 },
  }
  const { value, dropped } = pickUpdatable(patch)
  assert.deepStrictEqual(value.llm, { temperature: 0.7 }) // api_key 被丟掉
  assert.deepStrictEqual(value.server, { port: 6199 }) // access_token 被丟掉
  assert.deepStrictEqual(dropped.sort(), ['llm.api_key', 'server.access_token'].sort())
})

// (c) 深合併
await t('(c) deepMerge：dict 遞迴合併、其餘（含陣列）取代', () => {
  const base = { a: { x: 1, y: 2 }, b: 3, arr: [1, 2] }
  const patch = { a: { y: 20 }, c: { z: 9 }, arr: [3] }
  assert.deepStrictEqual(deepMerge(base, patch), { a: { x: 1, y: 20 }, b: 3, arr: [3], c: { z: 9 } })
  // 不污染原物件
  assert.deepStrictEqual(base, { a: { x: 1, y: 2 }, b: 3, arr: [1, 2] })
  // primitive 取代
  assert.deepStrictEqual(deepMerge({ x: 1 }, { x: 2 }), { x: 2 })
})

// (d) section 過濾
await t('(d) pickSection：只回指定區段', () => {
  const full = { llm: { api_key: 'x' }, tools: { enabled: true }, chat: {} }
  assert.deepStrictEqual(pickSection(full, 'llm'), { llm: { api_key: 'x' } })
  assert.deepStrictEqual(pickSection(full, ''), full)
  assert.deepStrictEqual(pickSection(full, null), full)
  assert.deepStrictEqual(pickSection(full, 'nope'), {})
})

// diffKeys 只列鍵名不列值
await t('diffKeys 只回變更的 dotted 鍵名', () => {
  const before = { llm: { api_key: 'a', model: 'm' }, chat: { n: 1 } }
  const after = { llm: { api_key: 'b', model: 'm' }, chat: { n: 1, extra: true } }
  assert.deepStrictEqual(diffKeys(before, after).sort(), ['chat.extra', 'llm.api_key'].sort())
})

console.log('\n== webui-proxy 動作（假 fetch） ==')

function makeEnv(initialConfig) {
  let config = JSON.parse(JSON.stringify(initialConfig))
  const calls = []
  async function pawbotFetch(method, apiPath, body) {
    calls.push({ method, apiPath, body })
    if (method === 'GET' && apiPath === '/api/config') {
      return { ok: true, http_status: 200, data: { ok: true, data: JSON.parse(JSON.stringify(config)) } }
    }
    if (method === 'POST' && apiPath === '/api/config') {
      config = deepMerge(config, body || {})
      return { ok: true, http_status: 200, data: { ok: true, data: JSON.parse(JSON.stringify(config)) } }
    }
    if (method === 'GET' && apiPath.startsWith('/api/file?path=')) {
      return { ok: true, http_status: 200, data: { ok: true, data: { path: PERSONA_API_PATH, content: 'hello persona' } } }
    }
    if (method === 'POST' && apiPath === '/api/file') {
      return { ok: true, http_status: 200, data: { ok: true, data: { path: body && body.path } } }
    }
    if (method === 'GET' && apiPath === '/api/status') {
      return { ok: true, http_status: 200, data: { ok: true, data: { uptime: 42, connections: 1 } } }
    }
    if (method === 'POST' && apiPath === '/api/plugins/reload') {
      return { ok: true, http_status: 200, data: { ok: true, data: { loaded: [], failed: [] } } }
    }
    if (method === 'POST' && apiPath === '/api/send') {
      return { ok: true, http_status: 200, data: { ok: true, data: { message_id: 'abc' } } }
    }
    return { ok: false, http_status: 404, data: { ok: false, error: 'not found' }, error: 'not found' }
  }
  return { pawbotFetch, calls, getConfig: () => config }
}

const processInfo = () => ({
  running: true, pid: 123, uptime_sec: 10, spawn_count: 1,
  last_exit: null, last_spawn_error: null, last_error: null,
})

await t('getConfig 遮罩後才回傳', async () => {
  const env = makeEnv({ llm: { api_key: 'sk-secret', model: 'deepseek-chat' }, server: { access_token: 'nap-token' } })
  const { actions } = createPawbotProxy({ pawbotFetch: env.pawbotFetch, processInfo, restart: async () => ({ ok: true }) })
  const r = await actions.getConfig()
  assert.ok(r.payload.ok)
  assert.equal(r.payload.config.llm.api_key, MASK)
  assert.equal(r.payload.config.server.access_token, MASK)
  assert.equal(r.payload.config.llm.model, 'deepseek-chat')
  assert.ok(r.payload.masked_keys.includes('llm.api_key'))
})

await t('setConfig 把遮罩值丟掉再 POST', async () => {
  const env = makeEnv({ llm: { api_key: 'sk-secret', model: 'deepseek-chat', temperature: 0.8 } })
  const { actions } = createPawbotProxy({ pawbotFetch: env.pawbotFetch, processInfo, restart: async () => ({ ok: true }) })
  const r = await actions.setConfig({ llm: { api_key: MASK, temperature: 0.5 } })
  assert.ok(r.payload.ok)
  const post = env.calls.filter((c) => c.method === 'POST' && c.apiPath === '/api/config').pop()
  assert.deepStrictEqual(post.body, { llm: { temperature: 0.5 } }) // 遮罩值沒被送出去
  assert.deepStrictEqual(r.payload.dropped_keys, ['llm.api_key'])
  // PawBot 端深合併後：api_key 原值保留、temperature 已改
  assert.equal(env.getConfig().llm.api_key, 'sk-secret')
  assert.equal(env.getConfig().llm.temperature, 0.5)
})

await t('getPersona 讀回 text', async () => {
  const env = makeEnv({})
  const { actions } = createPawbotProxy({ pawbotFetch: env.pawbotFetch, processInfo, restart: async () => ({ ok: true }) })
  const r = await actions.getPersona()
  assert.ok(r.payload.ok)
  assert.equal(r.payload.text, 'hello persona')
})

await t('(e) setPersona 大檔案拒絕（>200KB）', async () => {
  const env = makeEnv({})
  const { actions } = createPawbotProxy({ pawbotFetch: env.pawbotFetch, processInfo, restart: async () => ({ ok: true }) })
  const r = await actions.setPersona('x'.repeat(PERSONA_MAX_BYTES + 1))
  assert.equal(r.status, 413)
  assert.ok(!r.payload.ok)
  assert.ok(!env.calls.some((c) => c.method === 'POST' && c.apiPath === '/api/file'), '不應真的寫入')
})

await t('send 標示真的發出', async () => {
  const env = makeEnv({})
  const { actions } = createPawbotProxy({ pawbotFetch: env.pawbotFetch, processInfo, restart: async () => ({ ok: true }) })
  const r = await actions.send({ target: '123', message: 'hi', is_group: true })
  assert.ok(r.payload.ok)
  assert.equal(r.payload.sent, true)
  assert.ok(/真的/.test(r.payload.note), 'note 要明確說已真的送出')
})

await t('status 併入進程資訊與 PawBot /api/status', async () => {
  const env = makeEnv({})
  const { actions } = createPawbotProxy({ pawbotFetch: env.pawbotFetch, processInfo, restart: async () => ({ ok: true }) })
  const r = await actions.status()
  assert.ok(r.payload.ok)
  assert.equal(r.payload.running, true)
  assert.equal(r.payload.pid, 123)
  assert.equal(r.payload.pawbot.uptime, 42)
})

console.log('\n== /pawbot-ui dispatch：CSRF 守衛與錯誤映射 ==')

const SAME_ORIGIN = 'http://127.0.0.1:61025'

function mockReq({ method = 'GET', url = '/pawbot-ui/status', headers = {}, body } = {}) {
  const req = new EventEmitter()
  req.method = method
  req.url = url
  req.headers = headers
  req.complete = false
  req.resume = () => {}
  if (body !== undefined) {
    setImmediate(() => {
      if (req.complete) return
      req.complete = true
      req.emit('data', Buffer.from(typeof body === 'string' ? body : JSON.stringify(body)))
      req.emit('end')
    })
  }
  return req
}

function mockRes() {
  const res = { statusCode: null, body: '', finished: false }
  res.writeHead = (status) => { res.statusCode = status }
  res.end = (chunk) => {
    if (chunk !== undefined) res.body += String(chunk)
    res.finished = true
  }
  res.json = () => JSON.parse(res.body)
  return res
}

function proxyWith(env, extra = {}) {
  return createPawbotProxy({
    pawbotFetch: env.pawbotFetch,
    processInfo,
    restart: async () => ({ ok: true }),
    getOrigin: () => SAME_ORIGIN,
    ...extra,
  })
}

await t('跨來源 GET（Origin 不同 + Sec-Fetch-Site: cross-site）→ 403 cross-origin denied', async () => {
  const env = makeEnv({})
  const { dispatch } = proxyWith(env)
  const req = mockReq({ headers: { origin: 'http://evil.example', 'sec-fetch-site': 'cross-site' } })
  const res = mockRes()
  await dispatch(req, res)
  assert.equal(res.statusCode, 403)
  assert.deepStrictEqual(res.json(), { ok: false, error: 'cross-origin denied' })
  assert.equal(env.calls.length, 0, '被擋下時不應呼叫 PawBot')
})

await t('同源無 Origin 的 GET 仍然 200（不誤傷 DSH GUI）', async () => {
  const env = makeEnv({})
  const { dispatch } = proxyWith(env)
  const req = mockReq({})
  const res = mockRes()
  await dispatch(req, res)
  assert.equal(res.statusCode, 200)
  assert.equal(res.json().ok, true)
})

await t('拿不到 Host origin 時 fail-closed：非 GET 一律 403', async () => {
  const env = makeEnv({})
  const { dispatch } = createPawbotProxy({
    pawbotFetch: env.pawbotFetch,
    processInfo,
    restart: async () => ({ ok: true }),
  })
  const req = mockReq({
    method: 'POST',
    url: '/pawbot-ui/restart',
    headers: { 'content-type': 'application/json' },
  })
  const res = mockRes()
  await dispatch(req, res)
  assert.equal(res.statusCode, 403)
  assert.deepStrictEqual(res.json(), { ok: false, error: 'cross-origin denied' })
})

await t('getConfig：PawBot 回非 object 時 502，不回 {config:{}}', async () => {
  const pawbotFetch = async () => ({ ok: true, http_status: 200, data: { ok: true, data: null } })
  const { actions } = createPawbotProxy({ pawbotFetch, processInfo, restart: async () => ({ ok: true }) })
  const r = await actions.getConfig()
  assert.equal(r.status, 502)
  assert.equal(r.payload.ok, false)
  assert.equal(r.payload.config, undefined)
  assert.match(r.payload.error, /不是 config 对象/)
})

await t('非 GET 沒有 content-type: application/json → 415', async () => {
  const env = makeEnv({})
  const { dispatch } = proxyWith(env)
  const req = mockReq({
    method: 'POST',
    url: '/pawbot-ui/restart',
    headers: { origin: SAME_ORIGIN, 'content-type': 'application/x-www-form-urlencoded' },
  })
  const res = mockRes()
  await dispatch(req, res)
  assert.equal(res.statusCode, 415)
  assert.equal(res.json().ok, false)
})

await t('send：PawBot HTTP 200 + {ok:false}（NapCat 斷線）時代理必須 502，不能回 sent:true', async () => {
  const pawbotFetch = async (method, apiPath) => {
    if (method === 'POST' && apiPath === '/api/send') {
      return { ok: true, http_status: 200, data: { ok: false, error: 'NapCat 未連線' } }
    }
    return { ok: false, http_status: 404, data: null, error: 'not found' }
  }
  const { dispatch } = createPawbotProxy({
    pawbotFetch,
    processInfo,
    restart: async () => ({ ok: true }),
    getOrigin: () => SAME_ORIGIN,
  })
  const req = mockReq({
    method: 'POST',
    url: '/pawbot-ui/send',
    headers: { origin: SAME_ORIGIN, 'content-type': 'application/json' },
    body: { target: '123', message: 'hi' },
  })
  const res = mockRes()
  await dispatch(req, res)
  assert.equal(res.statusCode, 502)
  const payload = res.json()
  assert.equal(payload.ok, false)
  assert.equal(payload.sent, undefined)
  assert.match(payload.error, /NapCat/)
})

await t('readBody 超限回 413 JSON（不 destroy 連線）', async () => {
  const env = makeEnv({})
  const { dispatch } = proxyWith(env)
  const req = mockReq({
    method: 'POST',
    url: '/pawbot-ui/config',
    headers: { origin: SAME_ORIGIN, 'content-type': 'application/json' },
    body: 'x'.repeat(1024 * 1024 + 32),
  })
  const res = mockRes()
  await dispatch(req, res)
  assert.equal(res.statusCode, 413)
  assert.equal(res.json().ok, false)
  assert.match(res.json().error, /body 过大/)
})

await t('body 沒收完就斷線 → 回 400，不會永遠 pending', async () => {
  const env = makeEnv({})
  const { dispatch } = proxyWith(env)
  const req = mockReq({
    method: 'POST',
    url: '/pawbot-ui/config',
    headers: { origin: SAME_ORIGIN, 'content-type': 'application/json' },
  })
  const res = mockRes()
  const running = dispatch(req, res)
  setImmediate(() => req.emit('close'))
  await running
  assert.equal(res.statusCode, 400)
  assert.equal(res.json().ok, false)
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
