// 离线测试：index.js 的 DataRoot / 控制 token / 优雅 stop 接线（全部用 fake 子进程 + 本地 HTTP）。
// 用法：node test/host-runtime-offline.mjs
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { createServer } from 'node:http'
import { syncBuiltinESMExports } from 'node:module'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import childProcess from 'node:child_process'

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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const originalSpawn = childProcess.spawn
const spawned = []
const taskkills = []
function fakeChild(exe, args, options) {
  const child = new EventEmitter()
  child.pid = 20000 + spawned.length
  child.exitCode = null
  child.signalCode = null
  child.stdout = new EventEmitter()
  child.stderr = new EventEmitter()
  child.stdout.setEncoding = child.stderr.setEncoding = () => {}
  child.killed = false
  child.kill = (signal) => {
    child.killed = true
    child.exitCode = signal === 'SIGKILL' ? -1 : 0
    child.emit('exit', child.exitCode, signal || null)
  }
  child.exit = () => {
    if (child.exitCode !== null) return
    child.exitCode = 0
    child.emit('exit', 0, null)
  }
  spawned.push({ exe, args, options, child })
  return child
}
childProcess.spawn = (exe, args, options) => {
  if (exe === 'taskkill') {
    taskkills.push(args)
    const killer = new EventEmitter()
    setTimeout(() => {
      for (const entry of spawned) {
        if (entry.child.exitCode === null) entry.child.exit()
      }
      killer.emit('exit', 0)
    }, 5)
    return killer
  }
  return fakeChild(exe, args, options)
}
syncBuiltinESMExports()

const { apply } = await import('../index.js')

function freePort() {
  return new Promise((resolvePort) => {
    const server = createServer()
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port
      server.close(() => resolvePort(port))
    })
  })
}

async function startControlServer(port) {
  const requests = []
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', (d) => { body += d })
    req.on('end', () => {
      requests.push({ method: req.method, url: req.url, headers: req.headers, body })
      res.writeHead(200, { 'content-type': 'application/json', connection: 'close' })
      res.end(JSON.stringify({ ok: true, flushed: true }))
      // 收到 ack 后让 fake 子进程真的退出（模拟 Windows 上 HTTP 优雅路径）。
      setTimeout(() => {
        for (const entry of spawned) {
          if (entry.child.exitCode === null) entry.child.exit()
        }
      }, 0)
    })
  })
  await new Promise((resolveReady) => server.listen(port, '127.0.0.1', resolveReady))
  return { server, requests }
}

const sandbox = mkdtempSync(join(tmpdir(), 'pawbot-host-runtime-'))
const appDir = join(sandbox, 'app')
mkdirSync(appDir, { recursive: true })
mkdirSync(join(appDir, 'data'), { recursive: true })
writeFileSync(join(appDir, 'run.py'), '# fake run.py for host wiring test\n', 'utf8')
const webuiPort = await freePort()
const control = await startControlServer(webuiPort)

const effects = []
const tools = new Map()
const logs = []
const ctx = {
  logger: { info: (msg) => logs.push(msg) },
  tools: {
    register: (tool) => { tools.set(tool.name, tool); return () => tools.delete(tool.name) },
    execute: async () => ({ isError: false, value: {} }),
  },
  webServer: {
    getOrigin: () => 'http://127.0.0.1:51225',
    register: () => () => {},
  },
  effect: (callback) => { effects.push(callback()) },
}
const cfg = {
  appDir,
  projectDir: appDir,
  pythonExe: process.platform === 'win32' ? 'py' : 'python3',
  autostart: true,
  webuiHost: '127.0.0.1',
  webuiPort,
  gracefulStopMs: 3000,
  dataGuardEnabled: true,
}
const priorDataRoot = process.env.PAWBOT_DATA_DIR
delete process.env.PAWBOT_DATA_DIR
const priorControlToken = process.env.PAWBOT_CONTROL_TOKEN
process.env.PAWBOT_CONTROL_TOKEN = 'inherited-secret-should-be-overridden'
try {
  apply(ctx, cfg)
  if (!spawned.length) console.error('DEBUG logs:', JSON.stringify(logs, null, 2))
  const first = spawned.at(-1)
  check('spawn 注入统一数据根 / host_pid / version / 全新控制 token', () => {
    const env = first.options.env
    assert.equal(env.PAWBOT_DATA_DIR, resolve(join(appDir, 'data')))
    assert.equal(env.PAWBOT_HOST_PID, String(process.pid))
    assert.equal(env.PAWBOT_RUNTIME_VERSION, '1.0.0')
    assert.match(env.PAWBOT_CONTROL_TOKEN, /^pbc_[a-f0-9]{64}$/)
    assert.notEqual(env.PAWBOT_CONTROL_TOKEN, 'inherited-secret-should-be-overridden',
      '必须覆盖父进程继承的旧 token')
  })

  const status = await tools.get('pawbot_status').execute({})
  check('pawbot_status 暴露数据根/控制面状态但不含 token', () => {
    assert.equal(status.data_guard.data_root, resolve(join(appDir, 'data')))
    assert.equal(status.data_guard.source, 'default')
    assert.equal(status.control_plane.enabled, true)
    assert.equal(status.control_plane.shutdown_path, '/api/control/shutdown')
    const published = JSON.stringify(status)
    assert.ok(!published.includes(first.options.env.PAWBOT_CONTROL_TOKEN), 'status 泄漏 token')
  })

  first.child.stdout.emit('data', `Authorization: Bearer ${first.options.env.PAWBOT_CONTROL_TOKEN}\n`)
  const tail = await tools.get('pawbot_logs').execute({ lines: 20 })
  check('子进程输出里的控制 token 被遮罩', () => {
    const published = JSON.stringify({ tail, logs })
    assert.ok(!published.includes(first.options.env.PAWBOT_CONTROL_TOKEN), '日志泄漏 token')
    assert.ok(published.includes('pbc_***'), '应留下遮罩痕迹')
  })

  await acheck('restart 先走控制面 shutdown（Bearer + 固定路径），收到 ack 后等退出再 spawn', async () => {
    const before = spawned.length
    const restart = await tools.get('pawbot_restart').execute({})
    assert.equal(restart.ok, true, JSON.stringify(restart))
    assert.equal(restart.stop?.graceful, true, '重启结果必须保留优雅停止证据')
    assert.equal(restart.stop?.ack?.ack?.flushed, true, '重启结果必须保留落盘回执')
    assert.equal(spawned.length, before + 1, '应只 spawn 一个新进程')
    assert.equal(taskkills.length, 0, '控制面成功时不应 taskkill')
    assert.ok(control.requests.length >= 1, '应收到控制面请求')
    const request = control.requests.at(-1)
    assert.equal(request.method, 'POST')
    assert.equal(request.url, '/api/control/shutdown')
    assert.equal(request.headers.authorization, `Bearer ${first.options.env.PAWBOT_CONTROL_TOKEN}`)
    assert.ok(!request.url.includes(first.options.env.PAWBOT_CONTROL_TOKEN), 'token 不得进 URL')
    const second = spawned.at(-1)
    assert.notEqual(second.options.env.PAWBOT_CONTROL_TOKEN, first.options.env.PAWBOT_CONTROL_TOKEN,
      '每次 spawn 必须换新控制 token')
  })

  await acheck('控制面不可用（超时/拒绝）时走平台兜底，并继续完成 restart', async () => {
    // 关掉控制面端口：模拟 run.py 卡死 / 控制面未起来。
    await new Promise((r) => control.server.close(r))
    cfg.gracefulStopMs = 200
    const before = spawned.length
    const previousToken = spawned.at(-1).options.env.PAWBOT_CONTROL_TOKEN
    const restart = await tools.get('pawbot_restart').execute({})
    assert.equal(restart.ok, true, JSON.stringify(restart))
    assert.equal(restart.stop?.graceful, false, '兜底重启不得伪报优雅停止')
    assert.notEqual(restart.stop?.method, 'control')
    assert.ok(restart.stop?.ack, '失败控制回执必须可观察')
    assert.equal(spawned.length, before + 1, '兜底停机后应只 spawn 一个新进程')
    if (process.platform === 'win32') {
      assert.ok(taskkills.length >= 1, 'Windows 兜底必须 taskkill /T /F')
      assert.ok(taskkills.at(-1).includes('/T') && taskkills.at(-1).includes('/F'), JSON.stringify(taskkills))
    } else {
      assert.ok(spawned.at(-2).child.killed, 'POSIX 兜底应发送 SIGTERM')
    }
    assert.notEqual(spawned.at(-1).options.env.PAWBOT_CONTROL_TOKEN, previousToken,
      '兜底重启也必须换新控制 token')
  })

  await acheck('dispose 走同一串行 stop，不残留第二个进程', async () => {
    for (const effect of effects.reverse()) {
      await effect?.()
    }
    assert.equal(spawned.length, 3, 'dispose 不应再 spawn')
    if (process.platform === 'win32') {
      assert.ok(taskkills.length >= 2, '控制面不可用时 dispose 也必须走 taskkill')
    }
  })
} finally {
  for (const entry of spawned) {
    if (entry.child.exitCode === null) entry.child.exit()
  }
  childProcess.spawn = originalSpawn
  syncBuiltinESMExports()
  if (priorDataRoot === undefined) delete process.env.PAWBOT_DATA_DIR
  else process.env.PAWBOT_DATA_DIR = priorDataRoot
  if (priorControlToken === undefined) delete process.env.PAWBOT_CONTROL_TOKEN
  else process.env.PAWBOT_CONTROL_TOKEN = priorControlToken
  if (control.server.listening) await new Promise((r) => control.server.close(r))
  await sleep(20)
  rmSync(sandbox, { recursive: true, force: true })
}

console.log(`\n${passed} passed, ${failed} failed`)
process.exitCode = failed ? 1 : 0
