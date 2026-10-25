import assert from 'node:assert/strict'
import test from 'node:test'
import { EventEmitter } from 'node:events'
import childProcess from 'node:child_process'
import { syncBuiltinESMExports } from 'node:module'
import { fileURLToPath } from 'node:url'
import { BRIDGE_PATH, BRIDGE_TOKEN_ENV, BRIDGE_URL_ENV } from '../lib/dsh-tool-bridge.js'

// No Python, subprocess, external DSH or network needed. The fake owns every spawn.
const originalSpawn = childProcess.spawn
const spawned = []
childProcess.spawn = (exe, args, options) => {
  assert.notEqual(exe, 'taskkill', 'test must end fake child before disposal')
  const child = new EventEmitter()
  child.pid = 10000 + spawned.length; child.exitCode = null; child.signalCode = null
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter()
  child.stdout.setEncoding = child.stderr.setEncoding = () => {}
  child.kill = () => { child.exitCode = 0; child.emit('exit', 0, null) }
  spawned.push({ exe, args, options, child })
  return child
}
syncBuiltinESMExports()
const { apply } = await import('../index.js')
const appDir = fileURLToPath(new URL('../app', import.meta.url))

function harness(config = {}) {
  const routes = [], tools = new Map(), effects = [], logs = [], calls = []
  const ctx = {
    logger: { info: (msg) => logs.push(msg) },
    tools: { register: (tool) => { tools.set(tool.name, tool); return () => tools.delete(tool.name) }, execute: async (input) => { calls.push(input); return { isError: false, value: { ok: true, queue_length: 1 } } } },
    webServer: { getOrigin: () => 'http://127.0.0.1:51225', register: (route) => { routes.push(route); return () => routes.splice(routes.indexOf(route), 1) } },
    effect: (callback) => { effects.push(callback()) },
  }
  const cfg = { appDir, projectDir: appDir, autostart: true, ...config }
  apply(ctx, cfg)
  return { cfg, routes, tools, logs, calls, async dispose() {
    for (const entry of spawned) if (entry.child.exitCode === null) entry.child.kill()
    for (const effect of effects.reverse()) await effect?.()
  } }
}
async function request(route, token) {
  const req = new EventEmitter(); req.method = 'POST'; req.socket = { remoteAddress: '127.0.0.1' }
  req.headers = { authorization: `Bearer ${token}`, 'content-type': 'application/json' }; req.pause = () => {}
  const res = new EventEmitter(); res.writeHead = (status) => { res.status = status }; res.end = (text) => { res.body = JSON.parse(text); res.writableEnded = true }
  const pending = route.handler(req, res)
  setImmediate(() => { if (!res.writableEnded) { req.emit('data', Buffer.from('{"name":"bili_watch_status","arguments":{}}')); req.emit('end') } })
  await pending
  return res
}

test('Host default/false/string config never registers bridge or inherits credentials', async () => {
  const priorToken = process.env[BRIDGE_TOKEN_ENV], priorUrl = process.env[BRIDGE_URL_ENV]
  process.env[BRIDGE_TOKEN_ENV] = 'inherited-secret'; process.env[BRIDGE_URL_ENV] = 'http://bad'
  try {
    for (const config of [{}, { dshBridgeEnabled: false }, { dshBridgeEnabled: 'true' }]) {
      const h = harness(config)
      try {
        assert.ok(h.routes.some((route) => route.path === '/pawbot-ui/config'))
        assert.ok(!h.routes.some((route) => route.path === BRIDGE_PATH))
        const env = spawned.at(-1).options.env
        assert.ok(!Object.hasOwn(env, BRIDGE_TOKEN_ENV))
        assert.ok(!Object.hasOwn(env, BRIDGE_URL_ENV))
        assert.equal(process.env[BRIDGE_TOKEN_ENV], 'inherited-secret')
      } finally { await h.dispose() }
    }
  } finally {
    if (priorToken === undefined) delete process.env[BRIDGE_TOKEN_ENV]; else process.env[BRIDGE_TOKEN_ENV] = priorToken
    if (priorUrl === undefined) delete process.env[BRIDGE_URL_ENV]; else process.env[BRIDGE_URL_ENV] = priorUrl
  }
})

test('enabled Host exact isolated route; only PawBot child env gets token; logs redact', async () => {
  const priorToken = process.env[BRIDGE_TOKEN_ENV]
  const h = harness({ dshBridgeEnabled: true })
  try {
    const routes = h.routes.filter((route) => route.path === BRIDGE_PATH)
    assert.equal(routes.length, 1); assert.equal(routes[0].kind, 'exact')
    const entry = spawned.at(-1), env = entry.options.env, token = env[BRIDGE_TOKEN_ENV]
    assert.match(token, /^pbdsh_[a-f0-9]{64}$/)
    assert.equal(env[BRIDGE_URL_ENV], `http://127.0.0.1:51225${BRIDGE_PATH}`)
    assert.equal(process.env[BRIDGE_TOKEN_ENV], priorToken)
    assert.equal((await request(routes[0], token)).status, 200)
    assert.equal(h.calls[0].name, 'bili_watch_status')
    // Simulate accidental environment logging split across stdout chunks.
    entry.child.stdout.emit('data', `env token=${token.slice(0, 20)}`)
    entry.child.stdout.emit('data', `${token.slice(20)} url=${env[BRIDGE_URL_ENV]}\n`)
    entry.child.stderr.emit('data', `${token}\n`)
    const status = await h.tools.get('pawbot_status').execute({})
    const logs = await h.tools.get('pawbot_logs').execute({})
    const published = JSON.stringify({ status, logs, host: h.logs })
    assert.ok(!published.includes(token))
    assert.ok(!published.includes(env[BRIDGE_URL_ENV]))
    entry.child.kill()
    assert.equal((await request(routes[0], token)).status, 503)
    await h.tools.get('pawbot_restart').execute({})
    const second = spawned.at(-1).options.env[BRIDGE_TOKEN_ENV]
    assert.notEqual(second, token)
    assert.equal((await request(routes[0], token)).status, 401)
    assert.equal((await request(routes[0], second)).status, 200)
    h.cfg.dshBridgeEnabled = false
    assert.equal((await request(routes[0], second)).status, 503)
    spawned.at(-1).child.kill()
    await h.tools.get('pawbot_restart').execute({})
    assert.ok(!Object.hasOwn(spawned.at(-1).options.env, BRIDGE_TOKEN_ENV))
  } finally { await h.dispose() }
  assert.ok(!h.routes.some((route) => route.path === BRIDGE_PATH))
})

test('Host dispose revokes saved handler and cancels credentials', async () => {
  const h = harness({ dshBridgeEnabled: true })
  const route = h.routes.find((item) => item.path === BRIDGE_PATH)
  const token = spawned.at(-1).options.env[BRIDGE_TOKEN_ENV]
  await h.dispose()
  assert.equal((await request(route, token)).status, 503)
})

test.after(() => { childProcess.spawn = originalSpawn; syncBuiltinESMExports() })
