// 真实进程验收（Host 侧接线）：用临时 app 副本起真实 run.py，
// 验证 Host 的 sendControlShutdown → 真实控制面 ack → 进程退出码 10 → Store 已落盘 + 锁释放。
// 只走 127.0.0.1 临时端口，不联网、不发 QQ、不调用 LLM，不使用真实 app/data。
// 用法：node test/host-runpy-integration.mjs
import { spawn, spawnSync } from 'node:child_process'
import {
  cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync,
} from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createControlToken, sendControlShutdown } from '../lib/lifecycle.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const APP = join(ROOT, 'app')
const PANEL = 'it-panel-token'

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
function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed')
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function findPython() {
  const tries = process.platform === 'win32'
    ? [['py', ['-3.13']], ['py', ['-3']], ['python', []]]
    : [['python3', []], ['python', []]]
  for (const [exe, args] of tries) {
    const r = spawnSync(exe, [...args, '-c', 'import sys; print(sys.executable)'], {
      encoding: 'utf8', windowsHide: true, timeout: 15000,
    })
    if (r.status === 0 && String(r.stdout || '').trim()) return String(r.stdout).trim()
  }
  return null
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

function copyApp(destination) {
  mkdirSync(destination, { recursive: true })
  cpSync(join(APP, 'run.py'), join(destination, 'run.py'))
  for (const name of ['pawbot', 'plugins', 'web']) {
    cpSync(join(APP, name), join(destination, name), {
      recursive: true,
      filter: (src) => !/[\\/](__pycache__|.*\.pyc)$/.test(src),
    })
  }
}

async function waitReady(port, token, proc, output, timeoutMs = 40000) {
  const deadline = Date.now() + timeoutMs
  let last = null
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) throw new Error(`run.py 提前退出 code=${proc.exitCode}\n${output()}`)
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/control/ready`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(1500),
      })
      const payload = await response.json()
      if (response.ok && payload?.ready === true) return payload
      last = `HTTP ${response.status} ${JSON.stringify(payload)}`
    } catch (error) {
      last = `${error?.name}: ${error?.message}`
    }
    await sleep(100)
  }
  throw new Error(`等待 /api/control/ready 超时：${last}\n${output()}`)
}

function waitExit(proc, timeoutMs = 25000) {
  return new Promise((resolve) => {
    if (proc.exitCode !== null) return resolve(proc.exitCode)
    const timer = setTimeout(() => resolve(null), timeoutMs)
    proc.once('exit', (code) => {
      clearTimeout(timer)
      resolve(code)
    })
  })
}

async function postJson(port, path, token, body) {
  const response = await fetch(`http://127.0.0.1:${port}${path}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    signal: AbortSignal.timeout(5000),
  })
  return { status: response.status, payload: await response.json() }
}

const python = findPython()
if (!python) {
  console.log('↷ 没有可用的 Python，跳过真实 run.py 接线验收')
  process.exit(0)
}

const sandbox = mkdtempSync(join(tmpdir(), 'pawbot-host-runpy-'))
const appDir = join(sandbox, 'app')
copyApp(appDir)
const dataDir = join(appDir, 'data')
mkdirSync(dataDir, { recursive: true })
const serverPort = await freePort()
const webuiPort = await freePort()
const controlToken = createControlToken()
writeFileSync(join(dataDir, 'config.json'), JSON.stringify({
  server: { host: '127.0.0.1', port: serverPort, path: '/ws', access_token: '' },
  webui: { host: '127.0.0.1', port: webuiPort, token: PANEL },
  tts: { enabled: false },
  proactive: { enabled: false },
  reminders: { enabled: false },
  sync: { enabled: false },
}, null, 2), 'utf8')
writeFileSync(join(dataDir, 'sessions.json'), JSON.stringify({
  s1: { messages: [{ role: 'user', content: '旧会话', who: '主人', ts: 1 }], archive: [], summary: '', updated: 1 },
}), 'utf8')

let output = ''
const childEnv = { ...process.env }
// T1：真实子进程必须显式隔离继承环境，不能依赖运行环境恰好干净。
for (const key of [
  'PAWBOT_DATA_DIR', 'PAWBOT_CONTROL_TOKEN', 'PAWBOT_HOST_PID',
  'PAWBOT_RUNTIME_VERSION', 'PAWBOT_DSH_BRIDGE_URL', 'PAWBOT_DSH_BRIDGE_TOKEN',
  'PAWBOT_DSH_BRAIN_URL', 'PAWBOT_DSH_BRAIN_TOKEN', 'PAWBOT_PYTHON_EXE',
]) delete childEnv[key]
Object.assign(childEnv, {
  PYTHONPATH: appDir,
  PYTHONUNBUFFERED: '1',
  PYTHONIOENCODING: 'utf-8',
  PAWBOT_DATA_DIR: dataDir,
  PAWBOT_CONTROL_TOKEN: controlToken,
  PAWBOT_HOST_PID: String(process.pid),
  PAWBOT_RUNTIME_VERSION: 'node-integration',
})
const proc = spawn(python, ['run.py'], {
  cwd: appDir,
  env: childEnv,
  stdio: ['ignore', 'pipe', 'pipe'],
  windowsHide: true,
})
proc.stdout.on('data', (d) => { output = (output + d).slice(-65536) })
proc.stderr.on('data', (d) => { output = (output + d).slice(-65536) })

try {
  const ready = await waitReady(webuiPort, controlToken, proc, () => output)
  check('真实 run.py 控制面 ready（loopback + Bearer）', () => {
    assert(ready.ready === true, JSON.stringify(ready))
    assert(ready.db_writable === true, JSON.stringify(ready))
    assert(ready.store_dirty === false, JSON.stringify(ready))
    assert(!JSON.stringify(ready).includes(controlToken), 'ready 回包不得含 control token')
  })

  check('真实 Python 进程持有运行锁（Host 未持锁），pid 为 python 本身', () => {
    const lockPath = join(dataDir, '.runtime.lock')
    assert(existsSync(lockPath), '运行锁应存在')
    const lock = JSON.parse(readFileSync(lockPath, 'utf8'))
    assert(lock.pid === proc.pid, `lock.pid=${lock.pid} proc.pid=${proc.pid}`)
    assert(lock.host_pid === process.pid, `host_pid=${lock.host_pid}`)
    assert(lock.version === 'node-integration', `version=${lock.version}`)
  })

  await postJson(webuiPort, '/api/session/clear', PANEL, { session_id: 's1' })

  const ack = await sendControlShutdown({ host: '127.0.0.1', port: webuiPort, token: controlToken, timeoutMs: 8000 })
  check('Host sendControlShutdown 收到真实 ack {ok:true,flushed:true}', () => {
    assert(ack.ok === true, JSON.stringify(ack))
    assert(ack.ack?.flushed === true, JSON.stringify(ack.ack))
    assert(ack.http_status === 200, `http=${ack.http_status}`)
  })

  const code = await waitExit(proc)
  check('进程以退出码 10 结束，且锁已释放、Store 已落盘', () => {
    assert(code === 10, `exit=${code}\n${output}`)
    assert(!existsSync(join(dataDir, '.runtime.lock')), '退出后不得残留运行锁')
    const sessions = JSON.parse(readFileSync(join(dataDir, 'sessions.json'), 'utf8'))
    assert(!('s1' in sessions), 'shutdown 应把清除后的 Store 真正落盘')
  })
} catch (error) {
  failed += 1
  console.error(`  ✘ 真实 run.py 接线验收异常\n      ${error?.message || error}`)
} finally {
  if (proc.exitCode === null) {
    try { proc.kill() } catch {}
    await waitExit(proc, 5000)
  }
  rmSync(sandbox, { recursive: true, force: true })
}

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
