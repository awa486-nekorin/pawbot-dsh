/**
 * test/life-proxy-offline.mjs —— LifeTab 真实 HTTP loopback 回归。
 * 用法：`node test/life-proxy-offline.mjs`
 *
 * 验证：浏览器侧只请求 `GET /pawbot-ui/life`；Host 代理经真实 HTTP 转发到
 * 模拟 PawBot 后端的 `GET /api/life`，成功/错误语义原样保留。
 * 代理本身不 mock：起真实 node:http 服务并调用 createPawbotProxy(...).dispatch；
 * 只模拟 PawBot 后端。不启动 Bot、QQ、浏览器，也不读运行 data。
 */
import assert from 'node:assert/strict'
import http from 'node:http'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createPawbotProxy } from '../lib/webui-proxy.js'

const REPO_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))

let passed = 0
let failed = 0
async function t(name, fn) {
  try {
    await fn()
    passed += 1
    console.log(`  ✔ ${name}`)
  } catch (error) {
    failed += 1
    console.log(`  ✘ ${name}`)
    console.log(`      ${String((error && error.stack) || error).split('\n').join('\n      ')}`)
  }
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      server.removeListener('error', reject)
      resolve(server.address())
    })
  })
}

function close(server) {
  return new Promise((resolve) => server.close(() => resolve()))
}

function origin(address) {
  return `http://127.0.0.1:${address.port}`
}

async function readJson(req) {
  if (req.method === 'GET') return null
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  const text = Buffer.concat(chunks).toString('utf8')
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return { raw: text }
  }
}

/** 与 index.js 的 pawbotFetch 同契约：真实 HTTP，不 mock 转发函数。 */
function makePawbotFetch(backendBase, calls) {
  return async function pawbotFetch(method, apiPath, body, timeoutMs = 5000) {
    const headers = {}
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    let res
    try {
      res = await fetch(backendBase + apiPath, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(Math.max(200, Number(timeoutMs) || 5000)),
      })
    } catch (error) {
      return {
        ok: false,
        http_status: null,
        data: null,
        error: `PawBot 连接失败（${error?.name || 'Error'}）`,
      }
    }
    let json
    try {
      json = await res.json()
    } catch {
      return { ok: false, http_status: res.status, data: null, error: 'PawBot 响应不是 JSON' }
    }
    calls.push({ method, apiPath })
    return {
      ok: res.ok,
      http_status: res.status,
      data: json,
      error: res.ok ? undefined : (typeof json?.error === 'string' ? json.error : `HTTP ${res.status}`),
    }
  }
}

async function requestJson(url) {
  const res = await fetch(url)
  let body = null
  try {
    body = await res.json()
  } catch {
    body = null
  }
  return { status: res.status, body }
}

async function testFixture() {
  const calls = []
  const requests = []
  let backendMode = 'ok'

  const backend = http.createServer(async (req, res) => {
    const body = await readJson(req)
    requests.push({ method: req.method, url: req.url, body })
    res.setHeader('Content-Type', 'application/json; charset=utf-8')

    if (backendMode === 'soft-error') {
      res.statusCode = 200
      res.end(JSON.stringify({ ok: false, error: '生活与成长状态暂时读不到' }))
      return
    }
    if (backendMode === 'http-error') {
      res.statusCode = 500
      res.end(JSON.stringify({ ok: false, error: '后端内部错误' }))
      return
    }
    res.statusCode = 200
    res.end(JSON.stringify({
      ok: true,
      data: {
        growth: {
          goals: [{ id: 'g1', type: 'task', text: '整理海边照片', status: 'active' }],
          changes: [{ target_kind: 'goal', op: 'add', reason: '主人托付' }],
        },
        autonomy: { enabled: true, next_at: 1700000000, history: [] },
      },
    }))
  })
  const backendAddress = await listen(backend)
  const backendBase = origin(backendAddress)

  const proxy = createPawbotProxy({
    pawbotFetch: makePawbotFetch(backendBase, calls),
    processInfo: () => ({ running: true, pid: 1, uptime_sec: 1, spawn_count: 1 }),
    restart: async () => ({ ok: true }),
    // 不注入 Referer 时，真实 loopback GET 不带 Origin；保持与线上一致的 origin 解析。
    getOrigin: () => proxyOrigin,
    log: () => {},
  })
  const proxyServer = http.createServer((req, res) => proxy.dispatch(req, res))
  const proxyAddress = await listen(proxyServer)
  const proxyOrigin = origin(proxyAddress)
  const proxyBase = proxyOrigin

  return {
    calls,
    requests,
    proxyBase,
    setMode(mode) { backendMode = mode },
    async dispose() {
      await close(proxyServer)
      await close(backend)
    },
  }
}

console.log('== LifeTab /pawbot-ui/life 真实 loopback ==')

await t('GET /pawbot-ui/life → 真实后端 GET /api/life（200，data 原样）', async () => {
  const fixture = await testFixture()
  try {
    const r = await requestJson(`${fixture.proxyBase}/pawbot-ui/life`)
    assert.equal(r.status, 200, 'HTTP status')
    assert.equal(r.body?.ok, true, 'ok')
    assert.equal(r.body?.data?.growth?.goals?.[0]?.text, '整理海边照片', 'growth 透传')
    assert.equal(r.body?.data?.autonomy?.enabled, true, 'autonomy 透传')
    assert.deepEqual(fixture.calls, [{ method: 'GET', apiPath: '/api/life' }], '后端调用')
    assert.deepEqual(
      fixture.requests.map((row) => `${row.method} ${row.url}`),
      ['GET /api/life'],
      '真实后端收到的请求',
    )
  } finally {
    await fixture.dispose()
  }
})

await t('后端 HTTP 200 + {ok:false} → 代理 502 且保留错误文本', async () => {
  const fixture = await testFixture()
  try {
    fixture.setMode('soft-error')
    const r = await requestJson(`${fixture.proxyBase}/pawbot-ui/life`)
    assert.equal(r.status, 502, 'HTTP status')
    assert.equal(r.body?.ok, false, 'ok')
    assert.equal(r.body?.error, '生活与成长状态暂时读不到', '错误文本')
    assert.deepEqual(fixture.calls, [{ method: 'GET', apiPath: '/api/life' }], '后端调用')
  } finally {
    await fixture.dispose()
  }
})

await t('后端 HTTP 500 → 代理 502 且不伪装成功', async () => {
  const fixture = await testFixture()
  try {
    fixture.setMode('http-error')
    const r = await requestJson(`${fixture.proxyBase}/pawbot-ui/life`)
    assert.equal(r.status, 502, 'HTTP status')
    assert.equal(r.body?.ok, false, 'ok')
    assert.equal(r.body?.error, '后端内部错误', '错误文本')
    assert.ok(!('data' in (r.body || {})), '失败时不应返回 data')
  } finally {
    await fixture.dispose()
  }
})

await t('client.js 走统一前缀 /life，不再拼成 /pawbot-ui/api/life', () => {
  const source = readFileSync(join(REPO_ROOT, 'client.js'), 'utf8')
  assert.ok(source.includes("api('GET', '/life'"), 'LifeTab 应请求 /life')
  assert.ok(!source.includes("api('GET', '/api/life'"), '不应再请求 /api/life')
})

console.log(`\n${passed} passed, ${failed} failed`)
// 不用 process.exit：Windows 上在最末一个 keep-alive socket 关闭时强制退出会触发
// libuv async handle 断言；只设置退出码，让事件循环自然结束。
process.exitCode = failed ? 1 : 0
