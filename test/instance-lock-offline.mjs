// 离线测试：单实例运行锁（guard 协议 / 活 pid 拒绝 / 未知 fail-closed / stale 归档接管 /
// 并发竞争 / 崩溃恢复 / guard 超时有界 / nonce 释放 / Node↔Python 互斥 / 强杀残留恢复）。
//
// 负例来自 docs/BATCH2-REVIEW-2026-10-30.md 的 L1–L6；刻意不复用 happy path 的断言。
// 用法：node test/instance-lock-offline.mjs
import { spawn, spawnSync } from 'node:child_process'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  LOCK_ARCHIVE_DIR,
  RUNTIME_LOCK_FILE,
  acquireRuntimeLock,
  inspectRuntimeLock,
  lockArchiveDir,
  lockGuardPath,
  processAlive,
  readRuntimeLock,
  releaseRuntimeLock,
  runtimeLockPath,
} from '../lib/instance-lock.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const APP = join(ROOT, 'app')
const WORKER = join(HERE, 'fixtures', 'runtime-lock-worker.mjs')
const PY_PROBE = join(HERE, 'fixtures', 'lock_probe.py')

let passed = 0
let failed = 0
let skipped = 0
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
function tmp(name) {
  return mkdtempSync(join(tmpdir(), `pawbot-lock-${name}-`))
}
function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms)
}
function staleFiles(root) {
  try {
    return readdirSync(lockArchiveDir(root)).filter((name) => /^stale-.*\.lock\.json$/.test(name))
  } catch {
    return []
  }
}
function writeLock(root, lock) {
  mkdirSync(root, { recursive: true })
  writeFileSync(runtimeLockPath(root), JSON.stringify(lock), 'utf8')
}
function deadLock(overrides = {}) {
  return {
    pid: 999999,
    started_at: new Date().toISOString(),
    host_pid: 0,
    version: 'crashed',
    nonce: 'd'.repeat(32),
    ...overrides,
  }
}

function findPython() {
  const tries = process.platform === 'win32'
    ? [['py', ['-3.13']], ['py', ['-3']], ['python', []]]
    : [['python3', []], ['python', []]]
  for (const [exe, args] of tries) {
    const r = spawnSync(exe, [...args, '-c', 'import sys; print(sys.executable)'], {
      encoding: 'utf8',
      windowsHide: true,
      timeout: 15000,
    })
    if (r.status === 0 && String(r.stdout || '').trim()) {
      return { exe, args, python: String(r.stdout).trim() }
    }
  }
  return null
}

function probeEnv(root, extra = {}) {
  const env = {
    ...process.env,
    PAWBOT_APP_DIR: APP,
    PAWBOT_DATA_ROOT: root,
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    ...extra,
  }
  // 真实运行环境可能带着这些；测试子进程不应继承任何 bridge/control token。
  for (const key of [
    'PAWBOT_DATA_DIR', 'PAWBOT_CONTROL_TOKEN', 'PAWBOT_DSH_BRIDGE_URL',
    'PAWBOT_DSH_BRIDGE_TOKEN', 'PAWBOT_DSH_BRAIN_URL', 'PAWBOT_DSH_BRAIN_TOKEN',
  ]) delete env[key]
  return env
}

function runWorker(dataRoot, tag, startAt, holdMs) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [WORKER, dataRoot, tag, String(startAt), String(holdMs)], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let out = ''
    let err = ''
    child.stdout.on('data', (d) => { out += d })
    child.stderr.on('data', (d) => { err += d })
    child.on('error', (error) => resolve({ tag, ok: false, code: 'SPAWN_ERROR', error: String(error.message), err }))
    child.on('exit', (code) => {
      let parsed = null
      for (const line of out.split(/\r?\n/)) {
        if (!line.trim()) continue
        try { parsed = JSON.parse(line) } catch { /* ignore */ }
      }
      resolve({ tag, code, ...(parsed || { ok: false, code: 'NO_OUTPUT' }), err })
    })
  })
}

function waitForLine(child, marker, timeoutMs = 20000) {
  let out = ''
  child.stdout.on('data', (d) => { out += d })
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`等待 ${marker} 超时：${out}`)), timeoutMs)
    child.stdout.on('data', () => {
      if (out.includes(marker)) {
        clearTimeout(timer)
        resolve(out)
      }
    })
    child.on('exit', (code) => {
      clearTimeout(timer)
      reject(new Error(`子进程提前退出 code=${code}：${out}`))
    })
  })
}

console.log('== 锁文件格式 / guard 协议 ==')

check('acquire 写入 {pid,started_at,host_pid,version,nonce}，guard 固定且不被删除', () => {
  const root = tmp('format')
  const r = acquireRuntimeLock({ dataRoot: root, hostPid: process.pid, version: '1.2.3' })
  assert(r.ok === true, JSON.stringify(r))
  assert(runtimeLockPath(root) === join(root, RUNTIME_LOCK_FILE), '锁路径契约')
  assert(lockGuardPath(root) === join(root, LOCK_ARCHIVE_DIR, 'guard.lock'), 'guard 路径契约')
  assert(existsSync(runtimeLockPath(root)), '锁文件应存在')
  const raw = JSON.parse(readFileSync(runtimeLockPath(root), 'utf8'))
  assert(JSON.stringify(Object.keys(raw).sort()) === JSON.stringify(
    ['host_pid', 'nonce', 'pid', 'started_at', 'version']), `字段=${Object.keys(raw)}`)
  assert(raw.pid === process.pid, `pid=${raw.pid}`)
  assert(raw.host_pid === process.pid, `host_pid=${raw.host_pid}`)
  assert(raw.version === '1.2.3', `version=${raw.version}`)
  assert(typeof raw.started_at === 'string' && raw.started_at.length > 10, 'started_at 应是时间串')
  assert(/^[a-f0-9]{32}$/.test(raw.nonce), `nonce=${raw.nonce}`)
  assert(existsSync(lockGuardPath(root)), 'guard 文件应存在')
  assert(!existsSync(join(lockArchiveDir(root), 'takeover.lock')), '不得再创建可陈旧的 takeover.lock')
  assert(releaseRuntimeLock({ dataRoot: root, nonce: r.lock.nonce }).ok === true, '释放成功')
  assert(!existsSync(runtimeLockPath(root)), '释放后锁应删除')
  assert(existsSync(lockGuardPath(root)), 'guard 文件必须永不删除')
})

console.log('== L1/L2：活 owner 不因墙钟被抢 ==')

check('L1：活 pid + 1 小时前 started_at 仍拒绝，锁内容不变', () => {
  const root = tmp('l1-live')
  const ancient = deadLock({
    pid: process.pid,
    started_at: new Date(Date.now() - 3600_000).toISOString(),
    nonce: 'a'.repeat(32),
  })
  writeLock(root, ancient)
  const r = acquireRuntimeLock({ dataRoot: root, timeoutMs: 200 })
  assert(r.ok === false, '活 owner 不得被抢')
  assert(r.code === 'LOCKED', `code=${r.code}`)
  const raw = readFileSync(runtimeLockPath(root), 'utf8')
  assert(raw === JSON.stringify(ancient), '原锁内容不得被改动')
  assert(existsSync(lockGuardPath(root)), 'guard 文件应存在')
  assert(staleFiles(root).length === 0, '不得归档活 owner 的锁')
})

check('L1：未知存活 + 1 小时前 started_at 仍 fail-closed，锁内容不变', () => {
  const root = tmp('l1-unknown')
  const ancient = deadLock({ started_at: new Date(Date.now() - 3600_000).toISOString() })
  writeLock(root, ancient)
  const r = acquireRuntimeLock({ dataRoot: root, isAlive: () => null, timeoutMs: 200 })
  assert(r.ok === false, '未知存活必须拒绝')
  assert(r.code === 'LOCK_UNKNOWN', `code=${r.code}`)
  assert(readFileSync(runtimeLockPath(root), 'utf8') === JSON.stringify(ancient), '锁内容不能变')
})

check('损坏锁 fail-closed、原文件不动', () => {
  const root = tmp('corrupt')
  mkdirSync(root, { recursive: true })
  writeFileSync(runtimeLockPath(root), 'not-json', 'utf8')
  const r = acquireRuntimeLock({ dataRoot: root })
  assert(r.ok === false && r.code === 'LOCK_UNREADABLE', `code=${r.code}`)
  assert(readFileSync(runtimeLockPath(root), 'utf8') === 'not-json', '损坏文件不被删除')
})

console.log('== stale 接管 ==')

check('死 pid 归档后接管；归档保留原内容；guard 不归档', () => {
  const root = tmp('stale')
  const dead = deadLock()
  writeLock(root, dead)
  const r = acquireRuntimeLock({ dataRoot: root, version: 'new' })
  assert(r.ok === true, JSON.stringify(r))
  assert(r.staleArchived.length === 1, `archived=${JSON.stringify(r.staleArchived)}`)
  const archived = JSON.parse(readFileSync(r.staleArchived[0], 'utf8'))
  assert(archived.nonce === dead.nonce, '归档应是旧锁')
  assert(r.lock.nonce !== dead.nonce, '新锁 nonce 必须不同')
  assert(JSON.parse(readFileSync(runtimeLockPath(root), 'utf8')).nonce === r.lock.nonce, '新锁已就位')
  assert(staleFiles(root).length === 1, `stale 归档数量=${staleFiles(root).length}`)
  const inspected = inspectRuntimeLock(root)
  assert(inspected.present && inspected.alive === true && inspected.stale === false, JSON.stringify(inspected))
  releaseRuntimeLock({ dataRoot: root, nonce: r.lock.nonce })
})

check('死 pid 的 started_at 是未来时间也照样接管（不靠墙钟判断）', () => {
  const root = tmp('stale-future')
  const dead = deadLock({ started_at: new Date(Date.now() + 3600_000).toISOString() })
  writeLock(root, dead)
  const r = acquireRuntimeLock({ dataRoot: root })
  assert(r.ok === true, `未来 started_at 的死 owner 应可接管：${JSON.stringify(r)}`)
  assert(r.staleArchived.length === 1, '应归档旧锁')
  releaseRuntimeLock({ dataRoot: root, nonce: r.lock.nonce })
})

check('L3：崩溃只留下 temp 半截文件时，下次启动可接管且正式锁 JSON 完整', () => {
  const root = tmp('crash-temp')
  mkdirSync(root, { recursive: true })
  // 模拟「写完 temp、还没发布」就被强杀：正式锁不存在，temp 是半截 JSON。
  writeFileSync(`${runtimeLockPath(root)}.tmp-1-deadbeef`, '{"pid": 12', 'utf8')
  const r = acquireRuntimeLock({ dataRoot: root, version: 'after-crash' })
  assert(r.ok === true, JSON.stringify(r))
  const parsed = JSON.parse(readFileSync(runtimeLockPath(root), 'utf8'))
  assert(parsed.nonce === r.lock.nonce && parsed.version === 'after-crash', '正式锁必须完整')
  releaseRuntimeLock({ dataRoot: root, nonce: r.lock.nonce })
})

console.log('== L4：guard 争用/崩溃恢复有界 ==')

const python = findPython()
if (!python) {
  skipped += 6
  console.log('  ↷ Python 不可用，跳过 guard/跨语言用例')
} else {
  async function startGuardHolder(root, seconds = 30) {
    const child = spawn(python.python, [PY_PROBE, root], {
      windowsHide: true,
      env: probeEnv(root, { PAWBOT_HOLD_GUARD_SECONDS: String(seconds) }),
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    await waitForLine(child, '"guard": true')
    return child
  }

  await acheck('L4：guard 被占时 acquire 有界超时（不忙等），释放后立即可拿', async () => {
    const root = tmp('guard-contend')
    const holder = await startGuardHolder(root)
    try {
      const started = Date.now()
      const r = acquireRuntimeLock({ dataRoot: root, timeoutMs: 200 })
      const elapsed = Date.now() - started
      assert(r.ok === false, 'guard 被占时不得成功')
      assert(r.code === 'LOCK_RACE', `code=${r.code}`)
      assert(elapsed < 5000, `有界超时：elapsed=${elapsed}ms`)
      assert(!existsSync(runtimeLockPath(root)), '超时不得发布半成品正式锁')
    } finally {
      holder.kill('SIGKILL')
      await new Promise((resolve) => holder.once('exit', resolve))
    }
    const after = acquireRuntimeLock({ dataRoot: root, timeoutMs: 3000 })
    assert(after.ok === true, `guard 释放后应能获取：${JSON.stringify(after)}`)
    releaseRuntimeLock({ dataRoot: root, nonce: after.lock.nonce })
  })

  await acheck('L4：持有 guard 的进程被强杀后 OS 自动释放，不产生永久锁', async () => {
    const root = tmp('guard-crash')
    const holder = await startGuardHolder(root, 30)
    holder.kill('SIGKILL')
    await new Promise((resolve) => holder.once('exit', resolve))
    const started = Date.now()
    const r = acquireRuntimeLock({ dataRoot: root, timeoutMs: 3000 })
    const elapsed = Date.now() - started
    assert(r.ok === true, `崩溃后 guard 应由内核释放：${JSON.stringify(r)}`)
    assert(elapsed < 5000, `恢复耗时应有限：${elapsed}ms`)
    assert(!existsSync(runtimeLockPath(root)) || readRuntimeLock(root).lock?.pid === process.pid, '正式锁应属于本进程')
    releaseRuntimeLock({ dataRoot: root, nonce: r.lock.nonce })
  })

  await acheck('L6：helper 不可用 fail-closed（不写锁、不误删）', async () => {
    const root = tmp('no-helper')
    const bad = acquireRuntimeLock({ dataRoot: root, pythonExe: 'pawbot-no-such-python-xyz' })
    assert(bad.ok === false && bad.code === 'LOCK_HELPER_UNAVAILABLE', JSON.stringify(bad))
    assert(!existsSync(runtimeLockPath(root)), 'helper 不可用不得发布锁')
    const held = acquireRuntimeLock({ dataRoot: root })
    assert(held.ok === true, JSON.stringify(held))
    const release = releaseRuntimeLock({
      dataRoot: root, nonce: held.lock.nonce, pythonExe: 'pawbot-no-such-python-xyz',
    })
    assert(release.ok === false && release.code === 'LOCK_HELPER_UNAVAILABLE', JSON.stringify(release))
    assert(existsSync(runtimeLockPath(root)), 'release 失败不得误删锁')
    assert(releaseRuntimeLock({ dataRoot: root, nonce: held.lock.nonce }).ok === true, '正常释放仍可用')
  })

  console.log('== 释放核对 nonce ==')

  check('nonce 不匹配不删除锁；匹配才删除', () => {
    const root = tmp('nonce')
    const r = acquireRuntimeLock({ dataRoot: root })
    const bad = releaseRuntimeLock({ dataRoot: root, nonce: 'f'.repeat(32) })
    assert(bad.ok === false && bad.code === 'NONCE_MISMATCH', JSON.stringify(bad))
    assert(existsSync(runtimeLockPath(root)), '错 nonce 不得删锁')
    const good = releaseRuntimeLock({ dataRoot: root, nonce: r.lock.nonce })
    assert(good.ok === true && good.removed === true, JSON.stringify(good))
    assert(!existsSync(runtimeLockPath(root)), '正确 nonce 删除')
  })

  check('锁内 pid 已被替换时释放也必须拒绝', () => {
    const root = tmp('nonce-pid')
    const r = acquireRuntimeLock({ dataRoot: root })
    const other = { ...r.lock, pid: 999999, nonce: 'c'.repeat(32) }
    writeLock(root, other)
    const bad = releaseRuntimeLock({ dataRoot: root, nonce: r.lock.nonce })
    assert(bad.ok === false, 'pid/nonce 不一致应拒绝')
    assert(existsSync(runtimeLockPath(root)), '锁仍在')
  })

  console.log('== 并发竞争 ==')

  await acheck('活 owner 在场时 3 个进程同时抢锁：全部 LOCKED，不产生 stale 归档', async () => {
    const root = tmp('race-live')
    const held = acquireRuntimeLock({ dataRoot: root, version: 'holder' })
    assert(held.ok, JSON.stringify(held))
    const startAt = Date.now() + 400
    const results = await Promise.all(Array.from({ length: 3 }, (_, i) =>
      runWorker(root, `l${i}`, startAt, 300)))
    for (const result of results) {
      assert(result.ok === false && result.code === 'LOCKED',
        `contender=${JSON.stringify(result)}`)
    }
    assert(staleFiles(root).length === 0, `活 owner 不得被归档：${JSON.stringify(staleFiles(root))}`)
    releaseRuntimeLock({ dataRoot: root, nonce: held.lock.nonce })
  })

  await acheck('5 个进程同时抢 stale 锁：恰好 1 个 winner，旧锁只归档一次', async () => {
    const root = tmp('race-stale')
    writeLock(root, deadLock())
    const startAt = Date.now() + 700
    const results = await Promise.all(Array.from({ length: 5 }, (_, i) =>
      runWorker(root, `s${i}`, startAt, 1500)))
    const winners = results.filter((r) => r.ok === true)
    assert(winners.length === 1, `winner=${winners.length} results=${JSON.stringify(results)}`)
    for (const loser of results.filter((r) => r.ok !== true)) {
      assert(loser.code === 'LOCKED', `loser code=${loser.code} err=${loser.err}`)
    }
    assert(staleFiles(root).length === 1, `旧 stale 只能归档一次：${JSON.stringify(staleFiles(root))}`)
    assert(!existsSync(runtimeLockPath(root)), 'winner 释放后不应残留锁')
  })

  console.log('== 跨语言（Node ↔ Python）互斥 ==')

  await acheck('Node 持锁时 Python 拒绝；释放后 Python 可获取', async () => {
    const root = tmp('xlang-node-holds')
    const nodeLock = acquireRuntimeLock({ dataRoot: root, version: 'node-holder' })
    assert(nodeLock.ok, JSON.stringify(nodeLock))
    const rejected = spawnSync(python.python, [PY_PROBE, root], {
      encoding: 'utf8', windowsHide: true, env: probeEnv(root), timeout: 20000,
    })
    assert(rejected.status === 2, `python status=${rejected.status} stderr=${rejected.stderr}`)
    const parsed = JSON.parse(String(rejected.stdout).trim().split(/\r?\n/).pop())
    assert(parsed.ok === false && parsed.code === 'LOCKED', JSON.stringify(parsed))
    assert(parsed.message.includes(String(process.pid)), `message=${parsed.message}`)
    releaseRuntimeLock({ dataRoot: root, nonce: nodeLock.lock.nonce })
    const accepted = spawnSync(python.python, [PY_PROBE, root], {
      encoding: 'utf8', windowsHide: true, env: probeEnv(root), timeout: 20000,
    })
    assert(accepted.status === 0, `python status=${accepted.status} stderr=${accepted.stderr}`)
    const got = JSON.parse(String(accepted.stdout).trim().split(/\r?\n/).pop())
    assert(got.ok === true, JSON.stringify(got))
    assert(got.nonce !== nodeLock.lock.nonce, 'Python 新锁 nonce 应不同')
    assert(!existsSync(runtimeLockPath(root)), 'Python 退出后释放锁')
    assert(existsSync(lockGuardPath(root)), 'guard 仍保留')
  })

  await acheck('Python 持锁时 Node 拒绝；强杀 Python 后 Node 识别 stale 并接管', async () => {
    const root = tmp('xlang-python-holds')
    const child = spawn(python.python, [PY_PROBE, root], {
      windowsHide: true, env: probeEnv(root, { PAWBOT_HOLD_SECONDS: '20' }), stdio: ['ignore', 'pipe', 'pipe'],
    })
    await waitForLine(child, '"ok": true')
    const pyLock = readRuntimeLock(root).lock
    assert(pyLock && pyLock.pid === child.pid, `py lock pid=${pyLock?.pid} child=${child.pid}`)
    const rejected = acquireRuntimeLock({ dataRoot: root, version: 'node' })
    assert(rejected.ok === false && rejected.code === 'LOCKED', JSON.stringify(rejected))
    // 模拟 taskkill /F：强杀后锁残留，下一次启动必须能接管。
    child.kill('SIGKILL')
    await new Promise((resolve) => child.once('exit', resolve))
    assert(existsSync(runtimeLockPath(root)), '强杀后锁应残留')
    const takeover = acquireRuntimeLock({ dataRoot: root, version: 'node-after-kill' })
    assert(takeover.ok === true, JSON.stringify(takeover))
    assert(takeover.staleArchived.length === 1, '应归档 Python 残留锁')
    const archived = JSON.parse(readFileSync(takeover.staleArchived[0], 'utf8'))
    assert(archived.pid === child.pid, '归档的应是 Python 的锁')
    releaseRuntimeLock({ dataRoot: root, nonce: takeover.lock.nonce })
  })

  check('进程存活探测：自身 true，明显死亡 false', () => {
    assert(processAlive(process.pid) === true, 'self 应 alive')
    assert(processAlive(999999) === false, '999999 应 dead')
    assert(processAlive(0) === null, '非法 pid 应为未知')
  })
}

console.log(`\n${passed} passed, ${failed} failed, ${skipped} skipped`)
process.exit(failed ? 1 : 0)
