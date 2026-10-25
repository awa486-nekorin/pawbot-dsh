import assert from 'node:assert/strict'
import test from 'node:test'
import { EventEmitter } from 'node:events'
import { createServer, request } from 'node:http'
import {
  BRIDGE_LIMITS, BRIDGE_PATH, BRIDGE_TOKEN_ENV, BRIDGE_URL_ENV, BRIDGE_TOOLS,
  createBridgeBudget, createToolBridge, isLoopback, projectStatus, redactBridgeText, stripBridgeEnv,
} from '../lib/dsh-tool-bridge.js'

const valid = { name: 'bili_watch_status', arguments: {} }
const success = { isError: false, value: { ok: true, queue_length: 3, inflight_watches: 1, watched_today: 4, daily_limit: 10, auto_stock: true, has_credential: true, credential_source: 'file' } }
function fixture(options = {}) {
  const calls = [], audit = []
  const bridge = createToolBridge({ execute: async (input) => { calls.push(input); return success }, getOrigin: () => 'http://127.0.0.1:51225', isEnabled: () => true, audit: (entry) => audit.push(entry), budget: createBridgeBudget(), ...options })
  const env = bridge.childEnv({ PATH: 'unchanged' })
  return { bridge, env, calls, audit }
}
function invoke(f, { body = valid, raw, headers = {}, method = 'POST', peer = '127.0.0.1', send = true } = {}) {
  const req = new EventEmitter()
  req.method = method; req.socket = { remoteAddress: peer }
  req.headers = { authorization: `Bearer ${f.env[BRIDGE_TOKEN_ENV]}`, 'content-type': 'application/json', ...headers }
  req.pause = () => {}
  const res = new EventEmitter()
  res.writableEnded = false
  res.writeHead = (status, outputHeaders) => { res.status = status; res.headers = outputHeaders }
  res.end = (text) => { res.text = text; res.body = JSON.parse(text); res.writableEnded = true }
  const finished = f.bridge.dispatch(req, res).then(() => res)
  if (send) setImmediate(() => {
    if (res.writableEnded) return
    const chunks = raw === undefined ? [Buffer.from(JSON.stringify(body))] : Array.isArray(raw) ? raw : [Buffer.from(raw)]
    for (const chunk of chunks) req.emit('data', chunk)
    req.emit('end')
  })
  return { req, res, finished }
}
const call = (f, options) => invoke(f, options).finished

test('fixed sole allowlist; actual Cordis execution shape and scalar projection', async () => {
  const f = fixture()
  assert.deepEqual(BRIDGE_TOOLS, ['bili_watch_status'])
  assert.ok(Object.isFrozen(BRIDGE_TOOLS))
  const res = await call(f)
  assert.equal(res.status, 200)
  assert.deepEqual(res.body, { ok: true, name: 'bili_watch_status', value: success.value })
  assert.deepEqual(Object.keys(f.calls[0]).sort(), ['arguments', 'callId', 'name', 'signal'])
  assert.equal(f.calls[0].name, valid.name)
  assert.deepEqual(f.calls[0].arguments, {})
  assert.ok(f.calls[0].signal instanceof AbortSignal)
  assert.match(f.calls[0].callId, /^pawbot-bridge-/)
  assert.equal(res.headers['Cache-Control'], 'no-store')
  assert.ok(!Object.hasOwn(res.headers, 'Access-Control-Allow-Origin'))
})

test('all other tools, pawbot_* recursion, arbitrary arguments and extra fields denied', async () => {
  const f = fixture()
  for (const name of ['pawbot_status', 'pawbot_restart', 'pawbot_config_get', 'pawbot_config_set', 'bili_watch', 'read', '__proto__', 'constructor', 'BILI_WATCH_STATUS']) {
    const res = await call(f, { body: { name, arguments: {} } })
    assert.equal(res.status, 403)
    assert.equal(res.body.error.code, 'TOOL_NOT_ALLOWED')
  }
  for (const body of [null, [], {}, { name: valid.name }, { ...valid, token: 'secret' }, { ...valid, arguments: [] }, { ...valid, arguments: { anything: true } }]) {
    assert.equal((await call(f, { body })).status, 400)
  }
  assert.equal(f.calls.length, 0)
  assert.ok(f.audit.every((entry) => ['denied', valid.name].includes(entry.tool)))
})

test('token mismatch/missing, nonloopback and spoofed proxy address fail closed', async () => {
  const f = fixture()
  for (const authorization of [undefined, '', 'Bearer x', `Bearer pbdsh_${'0'.repeat(64)}`, ['Bearer bad']]) {
    assert.equal((await call(f, { headers: { authorization } })).status, 401)
  }
  for (const peer of ['203.0.113.1', '192.168.1.2', undefined, 'localhost', '127.0.0.1.attacker', '::ffff:192.168.1.1']) {
    const res = await call(f, { peer: peer ?? '', headers: { 'x-forwarded-for': '127.0.0.1' } })
    assert.equal(res.status, 403)
  }
  for (const peer of ['127.0.0.1', '127.0.0.2', '::1', '::ffff:127.0.0.1']) assert.ok(isLoopback(peer))
  assert.equal(f.calls.length, 0)
})

test('POST JSON only; browser origins, compressed bodies and invalid lengths denied', async () => {
  const f = fixture()
  for (const method of ['GET', 'OPTIONS', 'PUT']) assert.equal((await call(f, { method })).status, 405)
  for (const headers of [{ origin: 'http://127.0.0.1:51225' }, { origin: 'null' }, { 'sec-fetch-site': 'same-origin' }]) {
    assert.equal((await call(f, { headers })).status, 403)
  }
  for (const headers of [{ 'content-type': 'text/plain' }, { 'content-type': 'application/jsonp' }, { 'content-type': undefined }, { 'content-encoding': 'gzip' }]) {
    assert.equal((await call(f, { headers })).status, 415)
  }
  assert.equal((await call(f, { headers: { 'content-length': '-1' } })).status, 400)
  assert.equal(f.calls.length, 0)
})

test('strict streamed/header byte cap, malformed JSON, invalid UTF8 and no execution', async () => {
  const f = fixture()
  assert.equal((await call(f, { headers: { 'content-length': String(BRIDGE_LIMITS.bodyBytes + 1) } })).status, 413)
  assert.equal((await call(f, { raw: [Buffer.alloc(600, 32), Buffer.alloc(425, 32)] })).status, 413)
  for (const raw of ['{', '', 'null', '[]', '{}']) assert.equal((await call(f, { raw })).status, 400)
  assert.equal((await call(f, { raw: [Buffer.from([0xff])] })).status, 400)
  assert.equal(f.calls.length, 0)
})

test('projection discards secrets/content/meta/report text and clamps scalar values', async () => {
  const unsafe = { ...success.value, queue_length: 1e99, watched_today: 4.7, daily_limit: -10, inflight_watches: '999', model: 'secret', token: 'secret', recent_reports: [{ title: 'secret' }], credential_source: 'secret' }
  const f = fixture({ execute: async () => ({ isError: false, value: unsafe, content: [{ text: 'secret' }], meta: { token: 'secret' } }) })
  const res = await call(f)
  assert.deepEqual(res.body.value, { ok: true, queue_length: 1_000_000, watched_today: 4, auto_stock: true, has_credential: true })
  assert.ok(!res.text.includes('secret'))
  assert.ok(Buffer.byteLength(res.text) <= BRIDGE_LIMITS.responseBytes)
  assert.equal(projectStatus('arbitrary'), null)
  assert.equal(projectStatus({ ok: false, error: 'secret' }), null)
})

test('failure and thrown exception projection never includes content or exception text', async () => {
  for (const execute of [async () => { throw new Error('SECRET_EXCEPTION') }, async () => ({ isError: true, error: { message: 'SECRET_EXCEPTION' }, value: success.value, content: ['SECRET_EXCEPTION'], meta: 'SECRET_EXCEPTION' })]) {
    const f = fixture({ execute })
    const res = await call(f)
    assert.equal(res.status, 502)
    assert.deepEqual(res.body, { ok: false, error: { code: 'TOOL_FAILED' } })
    assert.ok(!JSON.stringify(f.audit).includes('SECRET'))
  }
  const f = fixture({ execute: async () => ({ isError: false, value: 'SECRET_EXCEPTION' }) })
  assert.equal((await call(f)).body.error.code, 'INVALID_RESULT')
})

test('unauthenticated requests cannot exhaust the authenticated rate budget', async () => {
  const f = fixture()
  for (let i = 0; i <= BRIDGE_LIMITS.requestsPerMinute; i++) {
    assert.equal((await call(f, { headers: { authorization: undefined } })).status, 401)
  }
  assert.equal((await call(f)).status, 200)
  assert.equal(f.calls.length, 1)
})

test('per-process rate budget shared across bridge instances and token rotations', async () => {
  let now = 0
  const budget = createBridgeBudget(() => now)
  const a = fixture({ budget }), b = fixture({ budget })
  for (let i = 0; i < BRIDGE_LIMITS.requestsPerMinute; i++) assert.equal((await call(i % 2 ? a : b)).status, 200)
  a.env = a.bridge.childEnv({})
  assert.equal((await call(a)).status, 429)
  assert.equal((await call(b)).body.error.code, 'RATE_LIMITED')
  now = 60_001
  assert.equal((await call(b)).status, 200)
})

test('concurrency remains bounded until cancelled noncooperative tools settle', async () => {
  const pending = [], entered = []
  const f = fixture({ execute: (input) => { entered.push(input); return new Promise((resolve) => pending.push(resolve)) } })
  const first = invoke(f), second = invoke(f)
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(entered.length, 2)
  assert.equal((await call(f)).body.error.code, 'BUSY')
  first.res.emit('close')
  assert.equal((await first.finished).body.error.code, 'CANCELLED')
  assert.ok(entered[0].signal.aborted)
  assert.equal((await call(f)).body.error.code, 'BUSY')
  pending.shift()(success)
  pending.shift()(success)
  await second.finished
  assert.equal((await call(f, { body: { ...valid, arguments: { no: true } } })).status, 400)
})

test('timeout aborts execution and body reads; disposal cancels and rejects later calls', async () => {
  let execution
  const f = fixture({ execute: (input) => { execution = input; return new Promise((resolve) => input.signal.addEventListener('abort', () => resolve(success), { once: true })) } })
  const start = Date.now()
  const [tool, body] = await Promise.all([call(f), call(f, { send: false })])
  assert.equal(tool.status, 504)
  assert.equal(body.status, 504)
  assert.equal(tool.body.error.code, 'TIMEOUT')
  assert.ok(execution.signal.aborted)
  assert.ok(Date.now() - start < 4000)
  const waiting = invoke(f, { send: false })
  f.bridge.dispose()
  assert.equal((await waiting.finished).body.error.code, 'CANCELLED')
  assert.equal((await call(f)).status, 503)
})

test('child-only random token, revocation, disabled stripping, safe origin and redaction', async () => {
  const f = fixture()
  const old = f.env[BRIDGE_TOKEN_ENV]
  assert.match(old, /^pbdsh_[a-f0-9]{64}$/)
  assert.equal(f.env[BRIDGE_URL_ENV], `http://127.0.0.1:51225${BRIDGE_PATH}`)
  f.env = f.bridge.childEnv({ PAWBOT_DSH_BRIDGE_TOKEN: 'stale' })
  assert.notEqual(f.env[BRIDGE_TOKEN_ENV], old)
  assert.equal((await call(f, { headers: { authorization: `Bearer ${old}` } })).status, 401)
  f.bridge.revoke()
  assert.equal((await call(f)).status, 503)
  assert.deepEqual(stripBridgeEnv({ PAWBOT_DSH_BRIDGE_TOKEN: 'bad', pawbot_dsh_bridge_url: 'bad', PATH: 'ok' }), { PATH: 'ok' })
  const off = createToolBridge({ execute: () => assert.fail(), getOrigin: () => assert.fail() })
  assert.deepEqual(off.childEnv({ PAWBOT_DSH_BRIDGE_URL: 'bad' }), {})
  for (const origin of ['http://example.com:51225', 'http://localhost:51225', 'http://user:pass@127.0.0.1:51225']) {
    assert.throws(() => fixture({ getOrigin: () => origin }), /BRIDGE_ORIGIN_UNAVAILABLE/)
  }
  assert.equal(fixture({ getOrigin: () => 'http://0.0.0.0:51225' }).env[BRIDGE_URL_ENV], `http://127.0.0.1:51225${BRIDGE_PATH}`)
  const text = redactBridgeText(`${old} ${f.env[BRIDGE_URL_ENV]}`)
  assert.ok(!text.includes(old))
  assert.ok(!text.includes(BRIDGE_PATH))
})

test('disable during body read and client request abort never dispatch a tool', async () => {
  let enabled = true
  const f = fixture({ isEnabled: () => enabled })
  const waiting = invoke(f, { send: false })
  enabled = false
  waiting.req.emit('data', Buffer.from(JSON.stringify(valid)))
  waiting.req.emit('end')
  assert.equal((await waiting.finished).status, 503)
  enabled = true
  f.env = f.bridge.childEnv({})
  const aborted = invoke(f, { send: false })
  aborted.req.emit('aborted')
  assert.equal((await aborted.finished).body.error.code, 'CANCELLED')
  assert.equal(f.calls.length, 0)
})

test('audit consists only of sanitized tool, outcome, elapsed; no attacker strings', async () => {
  const f = fixture()
  await call(f, { body: { name: 'SECRET_TOOL\nINJECT_LOG', arguments: { token: 'SECRET_ARG' } } })
  await call(f)
  for (const entry of f.audit) {
    assert.deepEqual(Object.keys(entry).sort(), ['elapsed_ms', 'outcome', 'tool'])
    assert.ok(Number.isInteger(entry.elapsed_ms))
  }
  assert.ok(!JSON.stringify(f.audit).includes('SECRET'))
  assert.ok(!JSON.stringify(f.audit).includes(f.env[BRIDGE_TOKEN_ENV]))
})

test('real HTTP transport rejects chunked oversize and accepts authenticated JSON', async () => {
  const f = fixture()
  const server = createServer(f.bridge.dispatch)
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const send = (chunks) => new Promise((resolve, reject) => {
    const req = request({ hostname: '127.0.0.1', port: server.address().port, path: BRIDGE_PATH, method: 'POST', headers: { authorization: `Bearer ${f.env[BRIDGE_TOKEN_ENV]}`, 'content-type': 'application/json' } }, (res) => {
      let text = ''; res.on('data', (chunk) => { text += chunk }); res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(text) }))
    })
    req.on('error', reject)
    for (const chunk of chunks) req.write(chunk)
    req.end()
  })
  try {
    assert.equal((await send([JSON.stringify(valid)])).status, 200)
    assert.equal((await send([' '.repeat(700), ' '.repeat(400)])).status, 413)
  } finally { f.bridge.dispose(); await new Promise((resolve) => server.close(resolve)) }
})
