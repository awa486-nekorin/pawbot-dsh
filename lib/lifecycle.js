/**
 * Host 侧生命周期小工具（纯 Node，可离线测试）：
 *   - 控制面 token 生成/遮罩（每次 spawn 更新，绝不进日志/工具回包）；
 *   - POST /api/control/shutdown 客户端（loopback + Bearer + JSON，token 不进 URL）；
 *   - gracefulStop：控制面 shutdown → 等 ack/等退出 → POSIX SIGTERM/Windows taskkill /T /F
 *     → 最后兜底 kill；返回真实结果，不伪报 stopped。
 *   - createOperationGate：并发 stop/restart/dispose 只执行一次 stop，避免双 spawn/漏清理。
 */
import { randomBytes } from 'node:crypto'
import { isIP } from 'node:net'

export const CONTROL_TOKEN_ENV = 'PAWBOT_CONTROL_TOKEN'
export const HOST_PID_ENV = 'PAWBOT_HOST_PID'
export const RUNTIME_VERSION_ENV = 'PAWBOT_RUNTIME_VERSION'
export const CONTROL_TOKEN_PREFIX = 'pbc_'
export const CONTROL_PATHS = Object.freeze({
  ready: '/api/control/ready',
  shutdown: '/api/control/shutdown',
})

/** 每次 spawn 生成一个新的控制 token（pbc_ + 64 hex）。 */
export function createControlToken() {
  return `${CONTROL_TOKEN_PREFIX}${randomBytes(32).toString('hex')}`
}

/** 日志/工具输出统一遮罩：pbc_ 形态与「已知 token 原值」都不外泄。 */
export function redactControlTokens(text, extra = []) {
  let output = typeof text === 'string' ? text : String(text ?? '')
  output = output.replace(/\bpbc_[a-f0-9]{64}\b/g, 'pbc_***')
  for (const token of extra) {
    const secret = String(token || '')
    if (secret) output = output.split(secret).join('***')
  }
  return output
}

/** 对 ack/error 的任意嵌套值做完整遮罩（含对象 key）。 */
export function redactControlTokensDeep(value, extra = []) {
  if (typeof value === 'string') return redactControlTokens(value, extra)
  if (Array.isArray(value)) return value.map((item) => redactControlTokensDeep(item, extra))
  if (value && typeof value === 'object') {
    const output = {}
    for (const [key, item] of Object.entries(value)) {
      output[redactControlTokens(key, extra)] = redactControlTokensDeep(item, extra)
    }
    return output
  }
  return value
}

function isLoopbackIpv4(text) {
  if (isIP(text) !== 4) return false
  const first = Number(text.split('.')[0])
  return first === 127
}

/**
 * 出站控制面目标只允许**字面 loopback IP**（不接受 localhost/域名）。
 * @returns {{ok:true, bare:string, version:4|6, urlHost:string} | {ok:false, message:string}}
 */
export function resolveControlHost(host) {
  const text = String(host ?? '').trim()
  if (!text) return { ok: false, message: 'control host 为空' }
  const brackets = text.startsWith('[') && text.endsWith(']')
  const bare = brackets ? text.slice(1, -1) : text
  if (bare.includes('%')) return { ok: false, message: `control host 不允许带 zone：${text}` }
  const version = isIP(bare)
  if (version === 4) {
    if (!isLoopbackIpv4(bare)) {
      return { ok: false, message: `拒绝非 loopback 控制目标：${text}（只允许字面 loopback IP）` }
    }
    return { ok: true, bare, version: 4, urlHost: bare }
  }
  if (version === 6) {
    const lowered = bare.toLowerCase()
    const mapped = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/.exec(lowered)
    if (lowered === '::1' || (mapped && isLoopbackIpv4(mapped[1]))) {
      return { ok: true, bare, version: 6, urlHost: `[${bare}]` }
    }
    return { ok: false, message: `拒绝非 loopback 控制目标：${text}（只允许 ::1 或 IPv4-mapped loopback）` }
  }
  return { ok: false, message: `拒绝非字面 IP 的控制目标：${text}（localhost/域名不算）` }
}

/** 并发 stop 去重：进行中的再次调用直接拿同一个 Promise。 */
export function createOperationGate() {
  let active = null
  return {
    get busy() {
      return active !== null
    },
    runStop(fn) {
      if (active) return active
      active = Promise.resolve()
        .then(fn)
        .finally(() => {
          active = null
        })
      return active
    },
  }
}

/**
 * 调用 PawBot 控制面 shutdown。
 * 只发 loopback HTTP + Authorization: Bearer；token 绝不进 URL/错误信息。
 * 非字面 loopback 目标直接拒绝且**不调用 fetch**；重定向一律当失败（不泄漏 Authorization）。
 * 返回的 ack/error 做递归遮罩，任何嵌套反射的 token 都不进工具回包。
 * @returns {{ok:boolean, http_status:number|null, ack:object|null, error?:string}}
 */
export async function sendControlShutdown({
  host = '127.0.0.1',
  port,
  token,
  timeoutMs = 5000,
  fetchImpl = globalThis.fetch,
  reason = 'host-stop',
} = {}) {
  const secret = String(token || '').trim()
  if (!secret) return { ok: false, http_status: null, ack: null, error: 'control token 未配置' }
  const target = resolveControlHost(host)
  if (!target.ok) return { ok: false, http_status: null, ack: null, error: target.message }
  if (typeof fetchImpl !== 'function') {
    return { ok: false, http_status: null, ack: null, error: 'fetch 不可用' }
  }
  const numericPort = Number(port)
  if (!Number.isInteger(numericPort) || numericPort <= 0 || numericPort > 65535) {
    return { ok: false, http_status: null, ack: null, error: `control port 非法：${port}` }
  }
  const timeout = Math.max(50, Number(timeoutMs) || 5000)
  const url = `http://${target.urlHost}:${numericPort}${CONTROL_PATHS.shutdown}`
  let response
  try {
    response = await fetchImpl(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${secret}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ reason: String(reason || '').slice(0, 200) }),
      signal: AbortSignal.timeout(timeout),
      // 绝不跟随重定向：否则 Authorization 会被带到另一个 origin。
      redirect: 'error',
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
    })
  } catch (error) {
    return {
      ok: false,
      http_status: null,
      ack: null,
      error: redactControlTokens(`${error?.name || 'Error'}: ${error?.message || error}`, [secret]),
    }
  }
  let ack = null
  try {
    const raw = await response.text()
    if (raw && raw.length <= 64 * 1024) ack = JSON.parse(raw)
  } catch {
    ack = null
  }
  const safeAck = redactControlTokensDeep(ack, [secret])
  const ok = response.ok === true && ack?.ok === true && ack?.flushed === true
  return {
    ok,
    http_status: response.status ?? null,
    ack: safeAck,
    ...(ok ? {} : {
      error: redactControlTokens(
        String(ack?.error || `HTTP ${response.status}`), [secret]),
    }),
  }
}

/**
 * 停机顺序：
 *   1) 控制面 POST shutdown，等 ack，并在 graceMs 内等进程退出；
 *   2) 没退出 → Windows `taskkill /T /F`；POSIX `SIGTERM` 等 termWaitMs；
 *   3) 仍未退出 → SIGKILL（Windows 再兜底 child.kill）。
 * 全部回调可注入，便于离线测试真实的调用顺序；失败如实返回 stopped:false。
 *
 * @returns {{stopped:boolean, alive?:boolean, pid:number, method:string,
 *            ack:object|null, phases:string[], error?:string}}
 */
export async function gracefulStop({
  pid,
  isWin = process.platform === 'win32',
  graceMs = 10_000,
  termWaitMs = 1500,
  killWaitMs = 1500,
  isRunning = async () => true,
  requestShutdown = async () => ({ ok: false, error: '没有控制面' }),
  waitExit = async () => false,
  signalTerm = () => {},
  killTree = async () => {},
  kill = () => {},
  onPhase = () => {},
  now = Date.now,
} = {}) {
  const phases = []
  const phase = (name) => {
    phases.push(name)
    try {
      onPhase(name)
    } catch {}
  }
  const running = async () => Boolean(await isRunning())
  const grace = Math.max(50, Number(graceMs) || 10_000)
  const startedAt = now()
  let ack = null

  if (!(await running())) {
    return { stopped: true, alive: false, pid, method: 'already-exited', ack: null, phases }
  }

  phase('control-request')
  try {
    ack = await requestShutdown({ timeoutMs: grace })
  } catch (error) {
    ack = {
      ok: false,
      error: redactControlTokens(`${error?.name || 'Error'}: ${error?.message || error}`),
    }
  }

  if (ack?.ok === true) {
    const remaining = Math.max(50, grace - (now() - startedAt))
    if (await waitExit(remaining)) {
      return { stopped: true, alive: false, pid, method: 'control', ack, phases }
    }
  }

  if (!(await running())) {
    return { stopped: true, alive: false, pid, method: ack?.ok ? 'control' : 'exit', ack, phases }
  }

  let method = ack?.ok ? 'control-timeout' : 'control-failed'
  if (isWin) {
    phase('taskkill')
    method = 'taskkill'
    try {
      await killTree()
    } catch {}
    if (await waitExit(Math.max(50, Number(killWaitMs) || 1500))) {
      return { stopped: true, alive: false, pid, method, ack, phases }
    }
    phase('sigkill')
    method = 'sigkill'
    try {
      kill()
    } catch {}
    if (await waitExit(Math.max(50, Number(killWaitMs) || 1500))) {
      return { stopped: true, alive: false, pid, method, ack, phases }
    }
  } else {
    phase('sigterm')
    method = 'sigterm'
    try {
      signalTerm()
    } catch {}
    if (await waitExit(Math.max(50, Number(termWaitMs) || 1500))) {
      return { stopped: true, alive: false, pid, method, ack, phases }
    }
    phase('sigkill')
    method = 'sigkill'
    try {
      kill()
    } catch {}
    if (await waitExit(Math.max(50, Number(killWaitMs) || 1500))) {
      return { stopped: true, alive: false, pid, method, ack, phases }
    }
  }

  if (!(await running())) {
    return { stopped: true, alive: false, pid, method, ack, phases }
  }
  return {
    stopped: false,
    alive: true,
    pid,
    method,
    ack,
    phases,
    error: `PawBot 进程（pid=${pid}）杀不掉，仍存活`,
  }
}
