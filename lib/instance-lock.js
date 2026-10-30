/**
 * 运行时单实例锁（Node 侧；协议实现只有一份：app/pawbot/runtime_lock.py）。
 *
 * 文件：`<dataRoot>/.runtime.lock`，内容：
 *   { pid, started_at, host_pid, version, nonce }
 *
 * 算法（与 Python 侧完全一致）：
 *   - 互斥不用可陈旧的 mutex 文件，而是对固定且永不删除的
 *     `<dataRoot>/.locks/guard.lock` 取 OS advisory lock（Windows LockFile /
 *     POSIX flock）；helper 进程退出或崩溃时由内核自动释放，不存在"活锁被墙钟偷走"。
 *   - 正式锁先写 temp 并 fsync，再 link/rename 原子发布；崩溃最多留下无害 temp，
 *     不会留下半截 JSON 让下一次启动永久 fail-closed。
 *   - 只有 owner pid **明确不存在**才归档接管；活 pid / 无法判断一律 fail-closed。
 *   - 归档后回读核对内容，失败尽力还原，不静默吞掉别人的新锁。
 *
 * Node 侧文件操作全部委托给系统 Python helper（OS advisory lock 没有标准 Node
 * API，也不引入 native addon）；owner pid 始终是 Node 进程，helper 只是执行者。
 * helper 只拿最小 env（不含任何 token），且 Node 侧不 spawn 任何长期子进程。
 *
 * 纯 Node + 系统 Python，可离线测试。
 */
import { spawnSync } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const RUNTIME_LOCK_FILE = '.runtime.lock'
export const LOCK_ARCHIVE_DIR = '.locks'
export const LOCK_GUARD_FILE = 'guard.lock'
export const LOCK_FIELDS = Object.freeze(['pid', 'started_at', 'host_pid', 'version', 'nonce'])

const HERE = dirname(fileURLToPath(import.meta.url))
const DEFAULT_HELPER = join(HERE, '..', 'app', 'pawbot', 'runtime_lock.py')
const HELPER_ENV_KEYS = [
  'PATH', 'PATHEXT', 'SystemRoot', 'SYSTEMROOT', 'windir', 'WINDIR',
  'TEMP', 'TMP', 'TMPDIR', 'HOME', 'LANG', 'LC_ALL',
]
let cachedPython = null

export function runtimeLockPath(dataRoot) {
  return join(resolve(String(dataRoot || '.')), RUNTIME_LOCK_FILE)
}

export function lockArchiveDir(dataRoot) {
  return join(resolve(String(dataRoot || '.')), LOCK_ARCHIVE_DIR)
}

export function lockGuardPath(dataRoot) {
  return join(lockArchiveDir(dataRoot), LOCK_GUARD_FILE)
}

export function newLockNonce() {
  return randomBytes(16).toString('hex')
}

/** 进程存活探测：true=活；false=明确不存在；null=无法判断（调用方必须 fail-closed）。 */
export function processAlive(pid) {
  const value = Number(pid)
  if (!Number.isInteger(value) || value <= 0) return null
  try {
    process.kill(value, 0)
    return true
  } catch (error) {
    if (error?.code === 'ESRCH') return false
    if (error?.code === 'EPERM') return true
    return null
  }
}

function validateLock(lock) {
  if (!lock || typeof lock !== 'object' || Array.isArray(lock)) return { ok: false, reason: '不是对象' }
  if (!Number.isInteger(lock.pid) || lock.pid <= 0) return { ok: false, reason: 'pid 非法' }
  if (typeof lock.started_at !== 'string' || !lock.started_at.trim()) {
    return { ok: false, reason: 'started_at 缺失' }
  }
  if (!Number.isInteger(lock.host_pid) || lock.host_pid < 0) return { ok: false, reason: 'host_pid 非法' }
  if (typeof lock.version !== 'string') return { ok: false, reason: 'version 非法' }
  if (typeof lock.nonce !== 'string' || lock.nonce.length < 8) return { ok: false, reason: 'nonce 非法' }
  return { ok: true }
}

/** 只读读取；不抛。 */
export function readRuntimeLock(dataRoot) {
  const path = runtimeLockPath(dataRoot)
  let raw
  try {
    raw = readFileSync(path, 'utf8')
  } catch (error) {
    if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR') {
      return { path, present: false, lock: null, raw: null, error: null }
    }
    return { path, present: true, lock: null, raw: null, error: `读取运行锁失败：${error?.message || error}` }
  }
  try {
    const lock = JSON.parse(raw)
    const valid = validateLock(lock)
    if (!valid.ok) return { path, present: true, lock: null, raw, error: `运行锁字段非法：${valid.reason}` }
    return { path, present: true, lock, raw, error: null }
  } catch {
    return { path, present: true, lock: null, raw, error: '运行锁不是合法 JSON' }
  }
}

/**
 * 只读预检（给 Host 状态/启动提示用，不获取锁、不改变任何文件、不 spawn）。
 */
export function inspectRuntimeLock(dataRoot, { isAlive = processAlive } = {}) {
  const state = readRuntimeLock(dataRoot)
  if (!state.present) return { path: state.path, present: false, lock: null, alive: null, stale: false, error: null }
  if (!state.lock) {
    return { path: state.path, present: true, lock: null, alive: null, stale: false, error: state.error }
  }
  let alive
  try {
    alive = isAlive(state.lock.pid)
  } catch {
    alive = null
  }
  return {
    path: state.path,
    present: true,
    lock: state.lock,
    alive,
    stale: alive === false,
    error: alive === null ? `无法确认 pid=${state.lock.pid} 是否存活` : null,
  }
}

function helperEnv() {
  const env = {}
  for (const key of HELPER_ENV_KEYS) {
    if (process.env[key] !== undefined) env[key] = process.env[key]
  }
  env.PYTHONUTF8 = '1'
  env.PYTHONIOENCODING = 'utf-8'
  return env
}

function discoverPython(pythonExe) {
  if (pythonExe) return { ok: true, exe: String(pythonExe), args: [] }
  if (process.env.PAWBOT_PYTHON_EXE) {
    return { ok: true, exe: String(process.env.PAWBOT_PYTHON_EXE), args: [] }
  }
  if (cachedPython) return cachedPython
  const candidates = process.platform === 'win32'
    ? [['py', ['-3.13']], ['py', ['-3']], ['python', []], ['python3', []]]
    : [['python3', []], ['python', []]]
  const failures = []
  for (const [exe, args] of candidates) {
    const result = spawnSync(exe, [...args, '-c', 'import sys; print(sys.executable)'], {
      encoding: 'utf8',
      windowsHide: true,
      timeout: 15000,
      env: helperEnv(),
    })
    if (result.error) {
      failures.push(`${exe}: ${result.error.code || result.error.message}`)
      continue
    }
    const path = String(result.stdout || '').trim().split(/\r?\n/).pop() || ''
    if (result.status === 0 && path) {
      cachedPython = { ok: true, exe: path, args: [] }
      return cachedPython
    }
    failures.push(`${exe}: exit=${result.status}`)
  }
  return {
    ok: false,
    code: 'LOCK_HELPER_UNAVAILABLE',
    message: `找不到可用的 Python 3（helper 需要；${failures.slice(0, 4).join('；')}）`,
  }
}

function runHelper(op, payload, { pythonExe, timeoutMs = 15000 } = {}) {
  const python = discoverPython(pythonExe)
  if (!python.ok) return python
  const helper = DEFAULT_HELPER
  if (!existsSync(helper)) {
    return { ok: false, code: 'LOCK_HELPER_UNAVAILABLE', message: `运行锁 helper 不存在：${helper}` }
  }
  const result = spawnSync(python.exe, [...python.args, helper, op], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    windowsHide: true,
    timeout: Math.max(5000, Number(timeoutMs) || 15000),
    env: helperEnv(),
    maxBuffer: 1024 * 1024,
  })
  if (result.error) {
    const missing = result.error.code === 'ENOENT'
    const code = missing
      ? 'LOCK_HELPER_UNAVAILABLE'
      : (result.error.code === 'ETIMEDOUT' ? 'LOCK_HELPER_TIMEOUT' : 'LOCK_HELPER_FAILED')
    return {
      ok: false,
      code,
      message: missing
        ? `运行锁 helper 无法执行：找不到 Python（${python.exe}）`
        : `运行锁 helper 失败：${result.error.code || result.error.message}`,
    }
  }
  if (result.signal) {
    return { ok: false, code: 'LOCK_HELPER_TIMEOUT', message: `运行锁 helper 被终止（signal=${result.signal}）` }
  }
  const lines = String(result.stdout || '').trim().split(/\r?\n/).filter(Boolean)
  let parsed = null
  try {
    parsed = lines.length ? JSON.parse(lines[lines.length - 1]) : null
  } catch {
    parsed = null
  }
  if (!parsed || typeof parsed !== 'object') {
    const detail = String(result.stderr || '').trim().split(/\r?\n/).slice(-3).join(' | ').slice(0, 400)
    return {
      ok: false,
      code: 'LOCK_HELPER_FAILED',
      message: `运行锁 helper 输出不可解析（exit=${result.status}）${detail ? `：${detail}` : ''}`,
    }
  }
  return parsed
}

function toTimeoutMs(value, fallback = 5000) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return fallback
  return Math.min(60_000, Math.max(50, Math.trunc(numeric)))
}

/**
 * 获取运行锁。
 * @returns {{ok:true, lock:object, path:string, staleArchived:string[]} |
 *           {ok:false, code:string, message:string, lock?:object, path?:string, staleArchived:string[]}}
 */
export function acquireRuntimeLock({
  dataRoot,
  hostPid = 0,
  version = '',
  nonce = newLockNonce(),
  isAlive = processAlive,
  now = Date.now,
  timeoutMs = 5000,
  pythonExe,
  log,
} = {}) {
  const root = resolve(String(dataRoot || '.'))
  const path = runtimeLockPath(root)
  const timeout = toTimeoutMs(timeoutMs)
  const owner = {
    pid: process.pid,
    started_at: new Date(now()).toISOString(),
    host_pid: Number.isInteger(Number(hostPid)) && Number(hostPid) >= 0 ? Number(hostPid) : 0,
    version: String(version || ''),
    nonce: String(nonce),
  }

  // 把调用方注入的 isAlive 语义安全地交给 helper：只对「同一份 raw」生效，
  // helper 会核对 SHA-256；锁内容变了就自己用真实 pid 探测，不会误杀新 owner。
  let probe = null
  const state = readRuntimeLock(root)
  if (state.present && state.lock && state.raw !== null) {
    let alive
    try {
      alive = isAlive(state.lock.pid)
    } catch {
      alive = null
    }
    if (alive !== true && alive !== false) alive = null
    probe = {
      sha256: createHash('sha256').update(state.raw, 'utf8').digest('hex'),
      alive,
    }
  }

  const result = runHelper('acquire', {
    data_root: root,
    timeout_ms: timeout,
    owner,
    probe,
  }, { pythonExe, timeoutMs: timeout + 20_000 })
  if (!result.ok) {
    const code = String(result.code || 'LOCK_HELPER_FAILED')
    return {
      ok: false,
      code,
      path,
      ...(result.lock ? { lock: result.lock } : {}),
      message: String(result.message || '获取运行锁失败'),
      staleArchived: [],
    }
  }
  const staleArchived = Array.isArray(result.archived) ? result.archived.filter(Boolean) : []
  for (const archivedPath of staleArchived) {
    log?.(`运行锁：陈旧锁已归档到 ${archivedPath}`)
  }
  return { ok: true, lock: result.lock, path, staleArchived }
}

/**
 * 释放运行锁（核对 pid + nonce）。返回 {ok, removed, code?}。
 * 锁不存在视为已释放成功（removed:false）。
 */
export function releaseRuntimeLock({
  dataRoot,
  nonce,
  timeoutMs = 3000,
  pythonExe,
} = {}) {
  const root = resolve(String(dataRoot || '.'))
  const timeout = toTimeoutMs(timeoutMs)
  const result = runHelper('release', {
    data_root: root,
    timeout_ms: timeout,
    expected: { pid: process.pid, nonce: String(nonce || '') },
  }, { pythonExe, timeoutMs: timeout + 20_000 })
  if (!result.ok) {
    return {
      ok: false,
      removed: false,
      code: String(result.code || 'LOCK_HELPER_FAILED'),
      message: String(result.message || '释放运行锁失败'),
    }
  }
  return { ok: true, removed: Boolean(result.removed) }
}
