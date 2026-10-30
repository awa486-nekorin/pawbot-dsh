// 离线测试：Host 侧生命周期（控制 token / 停机顺序 / Windows 兜底 / 并发 stop 去重 / 遮罩）。
// 用法：node test/lifecycle-offline.mjs
import { createServer } from 'node:http'
import {
  CONTROL_TOKEN_ENV,
  CONTROL_TOKEN_PREFIX,
  createControlToken,
  createOperationGate,
  gracefulStop,
  redactControlTokens,
  redactControlTokensDeep,
  resolveControlHost,
  sendControlShutdown,
} from '../lib/lifecycle.js'

let passed = 0
let failed = 0
function check(name, run) {
  try {
    run()
    passed += 1
    console.log(`  ✔ ${name}`)
  } catch (error) {
    failed += 1
    console.error(`  ✘ ${name}\n      ${error?.message || error}`)
  }
}
async function acheck(name, run) {
  try {
    await run()
    passed += 1
    console.log(`  ✔ ${name}`)
  } catch (error) {
    failed += 1
    console.error(`  ✘ ${name}\n      ${error?.message || error}`)
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed')
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

console.log('== 控制 token ==')

check('每次生成新的 pbc_ + 64 hex，且互不相同', () => {
  assert(CONTROL_TOKEN_ENV === 'PAWBOT_CONTROL_TOKEN', `env=${CONTROL_TOKEN_ENV}`)
  const a = createControlToken()
  const b = createControlToken()
  assert(new RegExp(`^${CONTROL_TOKEN_PREFIX}[a-f0-9]{64}$`).test(a), `a=${a}`)
  assert(new RegExp(`^${CONTROL_TOKEN_PREFIX}[a-f0-9]{64}$`).test(b), `b=${b}`)
  assert(a !== b, 'token 必须每次更新')
})

check('日志遮罩：已知 token 与 pbc_ 形态都不外泄', () => {
  const token = createControlToken()
  const extra = 'standalone-token-value'
  const text = `header=Bearer ${token} url=?token=${extra} other=${'pbc_' + 'a'.repeat(64)}`
  const masked = redactControlTokens(text, [extra])
  assert(!masked.includes(token), `masked=${masked}`)
  assert(!masked.includes(extra), `masked=${masked}`)
  assert(!masked.includes('a'.repeat(64)), `masked=${masked}`)
  assert(masked.includes('***'), `masked=${masked}`)
})

console.log('== 并发 stop 去重 ==')

await acheck('并发 runStop 共用一次执行；结束后可再次执行', async () => {
  const gate = createOperationGate()
  let calls = 0
  const p1 = gate.runStop(async () => { calls += 1; await sleep(30); return 'first' })
  const p2 = gate.runStop(async () => { calls += 1; return 'second' })
  assert(gate.busy === true, '执行中 busy=true')
  assert((await p1) === 'first' && (await p2) === 'first', '两次拿到同一结果')
  assert(calls === 1, `calls=${calls}`)
  assert(gate.busy === false, '完成后 busy=false')
  const third = await gate.runStop(async () => { calls += 1; return 'third' })
  assert(third === 'third' && calls === 2, `second run calls=${calls}`)
})

console.log('== 优雅停机顺序 ==')

await acheck('控制面 ack + 正常退出：只走 control，不 taskkill / 不 SIGTERM', async () => {
  const phases = []
  const r = await gracefulStop({
    pid: 4242,
    isWin: false,
    graceMs: 200,
    isRunning: () => true,
    requestShutdown: async () => ({ ok: true, ack: { ok: true, flushed: true } }),
    waitExit: async () => true,
    signalTerm: () => phases.push('sigterm'),
    killTree: async () => phases.push('taskkill'),
    kill: () => phases.push('kill'),
    onPhase: (phase) => phases.push(phase),
  })
  assert(r.stopped === true, JSON.stringify(r))
  assert(r.method === 'control', `method=${r.method}`)
  assert(r.ack?.ack?.flushed === true, '应带回真实 ack')
  assert(!phases.includes('taskkill') && !phases.includes('sigterm'), `phases=${phases}`)
  assert(phases[0] === 'control-request', `phases=${phases}`)
})

await acheck('Windows：控制失败 → taskkill /T /F → 等退出', async () => {
  const phases = []
  let exits = 0
  const r = await gracefulStop({
    pid: 4243,
    isWin: true,
    graceMs: 100,
    isRunning: () => true,
    requestShutdown: async () => ({ ok: false, error: 'ECONNREFUSED' }),
    waitExit: async () => { exits += 1; return exits >= 1 },
    signalTerm: () => phases.push('sigterm'),
    killTree: async () => phases.push('taskkill'),
    kill: () => phases.push('kill'),
    onPhase: (phase) => phases.push(phase),
  })
  assert(r.stopped === true, JSON.stringify(r))
  assert(r.method === 'taskkill', `method=${r.method}`)
  assert(phases.includes('taskkill'), `phases=${phases}`)
  assert(!phases.includes('sigterm'), 'Windows 不发 SIGTERM')
})

await acheck('POSIX：控制 ack 后不退出 → SIGTERM 生效', async () => {
  const phases = []
  let exits = 0
  const r = await gracefulStop({
    pid: 4244,
    isWin: false,
    graceMs: 50,
    termWaitMs: 800,
    isRunning: () => true,
    requestShutdown: async () => ({ ok: true, ack: { ok: true, flushed: true } }),
    waitExit: async (ms) => { exits += 1; return exits >= 2 },
    signalTerm: () => phases.push('sigterm'),
    kill: () => phases.push('kill'),
    killTree: async () => phases.push('taskkill'),
    onPhase: (phase) => phases.push(phase),
  })
  assert(r.stopped === true, JSON.stringify(r))
  assert(r.method === 'sigterm', `method=${r.method}`)
  assert(phases.includes('sigterm'), `phases=${phases}`)
  assert(!phases.includes('taskkill'), 'POSIX 不应 taskkill')
})

await acheck('全部兜底仍杀不掉 → stopped:false / alive:true，不伪报', async () => {
  const phases = []
  const r = await gracefulStop({
    pid: 4245,
    isWin: false,
    graceMs: 30,
    termWaitMs: 30,
    killWaitMs: 30,
    isRunning: () => true,
    requestShutdown: async () => ({ ok: false, error: 'ECONNREFUSED' }),
    waitExit: async () => false,
    signalTerm: () => phases.push('sigterm'),
    kill: () => phases.push('sigkill'),
    killTree: async () => phases.push('taskkill'),
    onPhase: (phase) => phases.push(phase),
  })
  assert(r.stopped === false && r.alive === true, JSON.stringify(r))
  assert(typeof r.error === 'string' && r.error.includes('4245'), '错误里应有 pid')
  assert(phases.includes('sigterm') && phases.includes('sigkill'), `phases=${phases}`)
})

await acheck('控制 ack 失败但进程已退出 → stopped:true，不额外强杀', async () => {
  const phases = []
  let running = true
  const r = await gracefulStop({
    pid: 4246,
    isWin: true,
    graceMs: 30,
    isRunning: () => running,
    requestShutdown: async () => { running = false; return { ok: false, error: 'socket hang up' } },
    waitExit: async () => true,
    killTree: async () => phases.push('taskkill'),
    kill: () => phases.push('kill'),
    onPhase: (phase) => phases.push(phase),
  })
  assert(r.stopped === true, JSON.stringify(r))
  assert(!phases.includes('taskkill'), `phases=${phases}`)
})

await acheck('graceMs 只用于控制面等待，超时后立刻走兜底', async () => {
  const started = Date.now()
  const waits = []
  const r = await gracefulStop({
    pid: 4247,
    isWin: true,
    graceMs: 40,
    killWaitMs: 40,
    isRunning: () => true,
    requestShutdown: async () => ({ ok: true, ack: { ok: true, flushed: true } }),
    waitExit: async (ms) => { waits.push(ms); return waits.length >= 2 },
    killTree: async () => {},
    kill: () => {},
    onPhase: () => {},
  })
  const elapsed = Date.now() - started
  assert(r.stopped === true, JSON.stringify(r))
  assert(waits[0] <= 60 && waits[0] >= 1, `first wait=${waits[0]}`)
  assert(elapsed < 1000, `elapsed=${elapsed}ms（fake waitExit 不应真的阻塞）`)
})

console.log('== 控制面 HTTP 客户端 ==')

function listen(server) {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)))
}

await acheck('POST /api/control/shutdown，Bearer 头 + JSON，token 不进 URL', async () => {
  const seen = []
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', (d) => { body += d })
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, body })
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ ok: true, flushed: true }))
    })
  })
  const port = await listen(server)
  try {
    const token = createControlToken()
    const r = await sendControlShutdown({ host: '127.0.0.1', port, token, timeoutMs: 2000 })
    assert(r.ok === true, JSON.stringify(r))
    assert(r.ack.flushed === true, 'ack.flushed')
    assert(seen.length === 1, `seen=${seen.length}`)
    assert(seen[0].method === 'POST', seen[0].method)
    assert(seen[0].url === '/api/control/shutdown', `url=${seen[0].url}`)
    assert(!String(seen[0].url).includes(token), 'token 不得出现在 URL')
    assert(seen[0].headers.authorization === `Bearer ${token}`, '必须用 Bearer 头')
    assert(seen[0].headers['content-type'] === 'application/json', '必须是 JSON')
  } finally {
    server.closeAllConnections?.()
    await new Promise((resolve) => server.close(resolve))
  }
})

await acheck('flush 失败（500 / ok:false）不得当成功', async () => {
  const server = createServer((req, res) => {
    req.resume()
    req.on('end', () => {
      res.writeHead(500, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ ok: false, flushed: false, error: 'disk full' }))
    })
  })
  const port = await listen(server)
  try {
    const r = await sendControlShutdown({ port, token: createControlToken(), timeoutMs: 2000 })
    assert(r.ok === false, JSON.stringify(r))
    assert(r.http_status === 500, `http=${r.http_status}`)
    assert(r.ack?.flushed === false, '应保留真实 ack')
  } finally {
    server.closeAllConnections?.()
    await new Promise((resolve) => server.close(resolve))
  }
})

await acheck('没配 token 时不发请求', async () => {
  let requested = false
  const r = await sendControlShutdown({
    port: 1,
    token: '',
    fetchImpl: async () => { requested = true; throw new Error('should not request') },
  })
  assert(r.ok === false && r.error.includes('token'), JSON.stringify(r))
  assert(requested === false, '无 token 不应发请求')
})

await acheck('超时失败信息不含 token', async () => {
  const token = createControlToken()
  const server = createServer(() => { /* 永不响应 */ })
  const port = await listen(server)
  try {
    const started = Date.now()
    const r = await sendControlShutdown({ port, token, timeoutMs: 80 })
    assert(r.ok === false, JSON.stringify(r))
    assert(!JSON.stringify(r).includes(token), `响应泄漏 token：${JSON.stringify(r)}`)
    assert(Date.now() - started < 3000, '应按超时返回')
  } finally {
    server.closeAllConnections?.()
    await new Promise((resolve) => server.close(resolve))
  }
})


console.log('== C1：出站控制面目标必须是字面 loopback ==')

await acheck('非 loopback / 域名 / 非法 host 一律拒绝，且不调用 fetch', async () => {
  const hosts = ['203.0.113.9', '192.168.1.5', 'localhost', 'example.com', '::2', '', '10.0.0.1']
  for (const host of hosts) {
    let requested = false
    const token = createControlToken()
    const r = await sendControlShutdown({
      host, port: 6185, token,
      fetchImpl: async () => { requested = true; throw new Error('must not fetch') },
    })
    assert(r.ok === false, `${host}: ${JSON.stringify(r)}`)
    assert(requested === false, `${host} 不应调用 fetch`)
    assert(!JSON.stringify(r).includes(token), `${host} 泄漏 token`)
  }
})

await acheck('字面 loopback（127.0.0.0/8 / ::1 / IPv4-mapped）可用且 URL 正确', async () => {
  const token = createControlToken()
  const cases = [
    ['127.0.0.1', 'http://127.0.0.1:6185/api/control/shutdown'],
    ['127.0.0.2', 'http://127.0.0.2:6185/api/control/shutdown'],
    ['::1', 'http://[::1]:6185/api/control/shutdown'],
    ['[::1]', 'http://[::1]:6185/api/control/shutdown'],
    ['::ffff:127.0.0.1', 'http://[::ffff:127.0.0.1]:6185/api/control/shutdown'],
  ]
  for (const [host, expectedUrl] of cases) {
    const seen = []
    const r = await sendControlShutdown({
      host, port: 6185, token,
      fetchImpl: async (url, options) => {
        seen.push({ url, options })
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, flushed: true }) }
      },
    })
    assert(r.ok === true, `${host}: ${JSON.stringify(r)}`)
    assert(seen.length === 1, `${host}: seen=${seen.length}`)
    assert(seen[0].url === expectedUrl, `${host}: url=${seen[0].url}`)
    assert(seen[0].options.redirect === 'error', `${host}: 必须禁止重定向`)
    assert(seen[0].options.headers.Authorization === `Bearer ${token}`, `${host}: Bearer`)
  }
})

await acheck('真实 HTTP 302 重定向：不把 Authorization 带给第二个 origin', async () => {
  let redirectedHits = 0
  const second = createServer((req, res) => {
    redirectedHits += 1
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ ok: true, flushed: true }))
  })
  const secondPort = await listen(second)
  const first = createServer((req, res) => {
    res.writeHead(302, { Location: `http://127.0.0.1:${secondPort}/api/control/shutdown` })
    res.end()
  })
  const firstPort = await listen(first)
  try {
    const token = createControlToken()
    const r = await sendControlShutdown({ host: '127.0.0.1', port: firstPort, token, timeoutMs: 3000 })
    assert(r.ok === false, JSON.stringify(r))
    assert(redirectedHits === 0, `重定向目标不应收到请求：${redirectedHits}`)
    assert(!JSON.stringify(r).includes(token), '错误信息不得含 token')
  } finally {
    first.closeAllConnections?.()
    second.closeAllConnections?.()
    await new Promise((resolve) => first.close(resolve))
    await new Promise((resolve) => second.close(resolve))
  }
})

console.log('== C2：ack/error 的嵌套 token 反射必须遮罩 ==')

check('deep redact 覆盖对象、数组与 key', () => {
  const token = 'pbc_' + 'b'.repeat(64)
  const value = {
    error: `bad ${token}`,
    echo: { Authorization: `Bearer ${token}`, list: [{ value: token }] },
    [token]: 'as-key',
  }
  const masked = redactControlTokensDeep(value, [token])
  const rendered = JSON.stringify(masked)
  assert(!rendered.includes(token), rendered)
  assert(rendered.includes('***'), rendered)
})

await acheck('HTTP 500 ack 反射 token 时完整遮罩，工具回包不含原值', async () => {
  const token = createControlToken()
  const server = createServer((req, res) => {
    res.writeHead(500, { 'content-type': 'application/json' })
    res.end(JSON.stringify({
      ok: false,
      flushed: false,
      error: `flush failed for ${token}`,
      echo: { Authorization: `Bearer ${token}`, nested: [{ token }] },
      [token]: 'key-reflect',
    }))
  })
  const port = await listen(server)
  try {
    const r = await sendControlShutdown({ host: '127.0.0.1', port, token, timeoutMs: 3000 })
    assert(r.ok === false, JSON.stringify(r))
    const rendered = JSON.stringify(r)
    assert(!rendered.includes(token), `回包泄漏 token：${rendered}`)
    assert(rendered.includes('***'), rendered)
    assert(r.http_status === 500, `http=${r.http_status}`)
  } finally {
    server.closeAllConnections?.()
    await new Promise((resolve) => server.close(resolve))
  }
})

check('resolveControlHost 明确区分字面 IP 与域名', () => {
  assert(resolveControlHost('127.0.0.1').ok === true)
  assert(resolveControlHost('::1').ok === true)
  assert(resolveControlHost('localhost').ok === false)
  assert(resolveControlHost('203.0.113.9').ok === false)
})

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
