// Real Windows fallback: loopback control timeout -> kill py.exe/Python tree.
// Synthetic processes only; no PawBot data, QQ, browser, plugin, or external HTTP.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { createControlToken, gracefulStop, sendControlShutdown } from '../lib/lifecycle.js'

if (process.platform !== 'win32') {
  console.log('0 passed, 0 failed, 1 skipped (Windows process tree only)')
  process.exit(0)
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const alive = (pid) => {
  try { process.kill(pid, 0); return true } catch (error) {
    if (error?.code === 'ESRCH') return false
    throw error
  }
}
const waitExit = (proc, ms) => new Promise((resolve) => {
  if (proc.exitCode !== null || proc.signalCode !== null) return resolve(true)
  const onExit = () => { clearTimeout(timer); resolve(true) }
  const timer = setTimeout(() => { proc.removeListener('exit', onExit); resolve(false) }, ms)
  proc.once('exit', onExit)
})
const killTree = (pid) => new Promise((resolve, reject) => {
  const killer = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
    windowsHide: true, stdio: 'ignore',
  })
  killer.once('error', reject)
  killer.once('exit', (code) => code === 0 ? resolve() : reject(new Error(`taskkill exit=${code}`)))
})

const env = { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
for (const key of Object.keys(env)) if (/^(PAWBOT_|DSH_)/.test(key)) delete env[key]
const marker = `pawbot-fixture-${randomUUID()}`
const script = `import json, os, subprocess, sys, time\n` +
  `child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', '${marker}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n` +
  `print(json.dumps({'python':os.getpid(),'grandchild':child.pid}), flush=True)\n` +
  `time.sleep(120)\n`
let proc
let metadata
let stderr = ''
let recorded = false
let failure
const sockets = new Set()
const server = createServer((req) => {
  if (req.method === 'POST' && req.url === '/api/control/shutdown') recorded = true
  req.resume() // Intentionally never acknowledge: exercise the real HTTP deadline.
})
server.on('connection', (socket) => {
  sockets.add(socket)
  socket.once('close', () => sockets.delete(socket))
})

try {
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  proc = spawn('py', ['-3.13', '-c', script], {
    env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  })
  proc.stderr.on('data', (data) => { stderr += data.toString() })
  metadata = await new Promise((resolve, reject) => {
    let text = ''
    const timer = setTimeout(() => reject(new Error('fixture startup timeout')), 15_000)
    proc.once('error', (error) => { clearTimeout(timer); reject(error) })
    proc.once('exit', (code) => {
      if (!text.includes('\n')) { clearTimeout(timer); reject(new Error(`fixture exit=${code}: ${stderr}`)) }
    })
    proc.stdout.on('data', (data) => {
      text += data.toString()
      if (!text.includes('\n')) return
      clearTimeout(timer)
      try { resolve(JSON.parse(text.split('\n')[0])) } catch (error) { reject(error) }
    })
  })
  assert.notEqual(metadata.python, proc.pid, 'py launcher must be a distinct owner')
  assert(alive(metadata.python) && alive(metadata.grandchild), 'fixture tree must be alive')
  const token = createControlToken()
  const port = server.address().port
  const result = await gracefulStop({
    pid: proc.pid, isWin: true, graceMs: 250, killWaitMs: 3000,
    isRunning: () => proc.exitCode === null && proc.signalCode === null,
    requestShutdown: ({ timeoutMs }) => sendControlShutdown({ port, token, timeoutMs }),
    waitExit: (ms) => waitExit(proc, ms),
    killTree: () => killTree(proc.pid),
    kill: () => proc.kill('SIGKILL'),
  })
  assert(recorded, 'control HTTP must be attempted before forced termination')
  assert.equal(result.stopped, true, JSON.stringify(result))
  assert.equal(result.method, 'taskkill', JSON.stringify(result))
  assert.deepEqual(result.phases.slice(0, 2), ['control-request', 'taskkill'])
  const deadline = Date.now() + 3000
  while (Date.now() < deadline && [proc.pid, metadata.python, metadata.grandchild].some(alive)) await sleep(50)
  assert([proc.pid, metadata.python, metadata.grandchild].every((pid) => !alive(pid)),
    'taskkill /T /F must leave no launcher, Python, or grandchild')
  console.log('1 passed, 0 failed — real control timeout then Windows process-tree termination')
} catch (error) {
  failure = error
  console.error(error?.stack || error)
} finally {
  // Only known, freshly spawned fixture PIDs are eligible for cleanup.
  if (proc && proc.exitCode === null && proc.signalCode === null) {
    try { await killTree(proc.pid) } catch {}
    await waitExit(proc, 3000)
  }
  for (const pid of [metadata?.grandchild, metadata?.python]) {
    if (pid && alive(pid)) { try { await killTree(pid) } catch {} }
  }
  for (const socket of sockets) socket.destroy()
  await new Promise((resolve) => server.close(resolve))
}
process.exitCode = failure ? 1 : 0
