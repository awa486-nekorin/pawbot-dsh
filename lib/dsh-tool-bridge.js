/** Private child-to-Host transport, deliberately independent of browser /pawbot-ui routes. */
import { createHash, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import { isIP } from 'node:net'
import { TextDecoder } from 'node:util'

export const BRIDGE_PATH = '/pawbot-dsh-bridge/execute'
export const BRIDGE_URL_ENV = 'PAWBOT_DSH_BRIDGE_URL'
export const BRIDGE_TOKEN_ENV = 'PAWBOT_DSH_BRIDGE_TOKEN'
export const BRIDGE_LIMITS = Object.freeze({ bodyBytes: 1024, responseBytes: 1024, requestsPerMinute: 30, concurrency: 2, timeoutMs: 2000 })
// Not configurable: in particular, NO pawbot_* tools (recursion/mutation) or generic dispatch.
export const BRIDGE_TOOLS = Object.freeze(['bili_watch_status'])

export function isLoopback(peer) {
  if (typeof peer !== 'string') return false
  if (peer === '::1') return true
  const v4 = peer.startsWith('::ffff:') ? peer.slice(7) : peer
  return isIP(v4) === 4 && v4.split('.')[0] === '127'
}

export function stripBridgeEnv(base) {
  const env = { ...base }
  for (const key of Object.keys(env)) {
    if ([BRIDGE_URL_ENV, BRIDGE_TOKEN_ENV].includes(key.toUpperCase())) delete env[key]
  }
  return env
}

// Also protect captured child stdout/stderr if a child accidentally prints its environment.
export function redactBridgeText(value) {
  return String(value).replace(/pbdsh_[a-f0-9]{64}/gi, '[bridge-token-redacted]')
    .replace(/pdbrain_[a-f0-9]{64}/gi, '[brain-token-redacted]')
    .replace(/https?:\/\/[^\s"'<>]*\/pawbot-dsh-bridge\/execute[^\s"'<>]*/gi, '[bridge-url-redacted]')
    .replace(/https?:\/\/[^\s"'<>]*\/pawbot-dsh-brain\/turn[^\s"'<>]*/gi, '[brain-url-redacted]')
}

// One budget shared by every bridge instance in this Host process, including child restarts.
// The factory is exported only to give offline tests independent clocks/budgets.
export function createBridgeBudget(now = () => performance.now()) {
  let start = now(), requests = 0, active = 0
  return {
    takeRate() {
      const at = now()
      if (at - start >= 60_000) { start = at; requests = 0 }
      return ++requests <= BRIDGE_LIMITS.requestsPerMinute
    },
    acquire() {
      if (active >= BRIDGE_LIMITS.concurrency) return null
      active += 1
      let released = false
      return () => { if (!released) { released = true; active -= 1 } }
    },
  }
}
const processBudget = createBridgeBudget()
const digest = (text) => createHash('sha256').update(text).digest()
const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)

/** No free text, titles, paths, model names, exception strings, content, or meta survive. */
export function projectStatus(value) {
  if (!object(value) || value.ok !== true) return null
  const out = { ok: true }
  for (const key of ['queue_length', 'inflight_watches', 'watched_today', 'daily_limit']) {
    if (typeof value[key] === 'number' && Number.isFinite(value[key]) && value[key] >= 0) {
      out[key] = Math.min(1_000_000, Math.trunc(value[key]))
    }
  }
  for (const key of ['auto_stock', 'has_credential']) {
    if (typeof value[key] === 'boolean') out[key] = value[key]
  }
  if (value.credential_source === null || ['env', 'file', 'config'].includes(value.credential_source)) {
    out.credential_source = value.credential_source
  }
  return out
}

function readBody(req, signal) {
  return new Promise((resolve) => {
    const chunks = []
    let size = 0, done = false
    const finish = (result) => {
      if (done) return
      done = true
      req.off('data', data); req.off('end', end); req.off('error', error); req.off('aborted', error)
      signal.removeEventListener('abort', aborted)
      chunks.length = 0
      req.pause?.()
      resolve(result)
    }
    const data = (chunk) => {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
      size += bytes.length
      if (size > BRIDGE_LIMITS.bodyBytes) return finish({ code: 'BODY_TOO_LARGE', status: 413 })
      chunks.push(bytes)
    }
    const end = () => {
      try {
        const text = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks))
        finish({ value: JSON.parse(text) })
      } catch { finish({ code: 'INVALID_JSON', status: 400 }) }
    }
    const error = () => finish({ code: 'CANCELLED', status: 499 })
    const aborted = () => finish({ code: signal.reason === 'TIMEOUT' ? 'TIMEOUT' : 'CANCELLED', status: signal.reason === 'TIMEOUT' ? 504 : 499 })
    if (signal.aborted) return aborted()
    req.on('data', data); req.once('end', end); req.once('error', error); req.once('aborted', error)
    signal.addEventListener('abort', aborted, { once: true })
  })
}

export function createToolBridge({ execute, getOrigin, isEnabled = () => false, audit = () => {}, budget = processBudget }) {
  let tokenHash = null, closed = false
  const controllers = new Set()
  const enabled = () => !closed && isEnabled() === true
  const revoke = () => {
    tokenHash = null
    for (const controller of controllers) controller.abort('CANCELLED')
  }
  const childEnv = (base) => {
    const env = stripBridgeEnv(base)
    revoke()
    if (!enabled()) return env
    // Never send a child credential to a remote origin, including configured DNS names.
    const url = new URL(getOrigin())
    const host = url.hostname.replace(/^\[|\]$/g, '')
    if (host === '0.0.0.0') url.hostname = '127.0.0.1'
    else if (host === '::') url.hostname = '[::1]'
    else if (!isLoopback(host)) throw new Error('BRIDGE_ORIGIN_UNAVAILABLE')
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('BRIDGE_ORIGIN_UNAVAILABLE')
    url.pathname = BRIDGE_PATH; url.search = ''; url.hash = ''
    const token = `pbdsh_${randomBytes(32).toString('hex')}`
    tokenHash = digest(token)
    env[BRIDGE_URL_ENV] = url.href
    env[BRIDGE_TOKEN_ENV] = token
    return env
  }

  const dispatch = async (req, res) => {
    const started = performance.now()
    let tool = 'denied', outcome = 'INTERNAL_ERROR', release = null, controller, timer, close
    const reply = (status, code, value) => {
      outcome = code
      if (res.destroyed || res.writableEnded) return
      const payload = value === undefined ? { ok: false, error: { code } } : { ok: true, name: 'bili_watch_status', value }
      const body = JSON.stringify(payload)
      if (Buffer.byteLength(body) > BRIDGE_LIMITS.responseBytes) return reply(502, 'INVALID_RESULT')
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Connection': 'close' })
      res.end(body)
    }
    try {
      if (!enabled()) { revoke(); return reply(503, 'BRIDGE_UNAVAILABLE') }
      if (!tokenHash) return reply(503, 'BRIDGE_UNAVAILABLE')
      if (!isLoopback(req.socket?.remoteAddress)) return reply(403, 'FORBIDDEN')
      if (req.method !== 'POST') return reply(405, 'METHOD_NOT_ALLOWED')
      // This is not a browser API: no CORS, no cookie auth, no Origin or Fetch Metadata.
      if (req.headers.origin !== undefined || req.headers['sec-fetch-site'] !== undefined) return reply(403, 'FORBIDDEN')
      const authorization = req.headers.authorization
      const match = typeof authorization === 'string' && /^Bearer (pbdsh_[a-f0-9]{64})$/.exec(authorization)
      const candidate = digest(match ? match[1] : '')
      const equal = timingSafeEqual(candidate, tokenHash)
      if (!match || !equal) return reply(401, 'UNAUTHORIZED')
      if (!budget.takeRate()) return reply(429, 'RATE_LIMITED')
      if (typeof req.headers['content-type'] !== 'string' || !/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(req.headers['content-type'])) return reply(415, 'UNSUPPORTED_MEDIA_TYPE')
      if (req.headers['content-encoding'] !== undefined) return reply(415, 'UNSUPPORTED_MEDIA_TYPE')
      const length = req.headers['content-length']
      if (length !== undefined && (typeof length !== 'string' || !/^\d+$/.test(length))) return reply(400, 'INVALID_REQUEST')
      if (length !== undefined && Number(length) > BRIDGE_LIMITS.bodyBytes) return reply(413, 'BODY_TOO_LARGE')
      release = budget.acquire()
      if (!release) return reply(429, 'BUSY')
      controller = new AbortController()
      controllers.add(controller)
      timer = setTimeout(() => controller.abort('TIMEOUT'), BRIDGE_LIMITS.timeoutMs)
      close = () => { if (!res.writableEnded) controller.abort('CANCELLED') }
      res.once('close', close)
      const parsed = await readBody(req, controller.signal)
      if (parsed.code) return reply(parsed.status, parsed.code)
      const body = parsed.value
      if (!object(body) || Object.keys(body).length !== 2 || !Object.hasOwn(body, 'name') || !Object.hasOwn(body, 'arguments')) return reply(400, 'INVALID_REQUEST')
      if (!BRIDGE_TOOLS.includes(body.name)) return reply(403, 'TOOL_NOT_ALLOWED')
      tool = body.name
      if (!object(body.arguments) || Object.keys(body.arguments).length !== 0) return reply(400, 'INVALID_ARGUMENTS')
      if (!enabled()) { revoke(); return reply(503, 'BRIDGE_UNAVAILABLE') }
      if (!tokenHash) return reply(503, 'BRIDGE_UNAVAILABLE')
      if (controller.signal.aborted) return reply(499, 'CANCELLED')
      // Keep the concurrency slot until execution really settles, even if it ignores abort.
      const executionRelease = release
      release = null
      const execution = Promise.resolve().then(() => execute({ callId: `pawbot-bridge-${randomUUID()}`, name: tool, arguments: {}, signal: controller.signal }))
        .then((result) => ({ result }), () => ({ failed: true }))
        .finally(() => { executionRelease(); controllers.delete(controller) })
      let onAbort
      const cancelled = new Promise((resolve) => {
        onAbort = () => resolve({ cancelled: true })
        if (controller.signal.aborted) onAbort()
        else controller.signal.addEventListener('abort', onAbort, { once: true })
      })
      const result = await Promise.race([execution, cancelled])
      controller.signal.removeEventListener('abort', onAbort)
      if (result.cancelled || controller.signal.aborted) {
        return reply(controller.signal.reason === 'TIMEOUT' ? 504 : 499, controller.signal.reason === 'TIMEOUT' ? 'TIMEOUT' : 'CANCELLED')
      }
      if (!enabled()) { revoke(); return reply(503, 'BRIDGE_UNAVAILABLE') }
      if (result.failed || result.result?.isError !== false) return reply(502, 'TOOL_FAILED')
      const value = projectStatus(result.result.value)
      if (!value) return reply(502, 'INVALID_RESULT')
      return reply(200, 'OK', value)
    } catch {
      return reply(500, 'INTERNAL_ERROR')
    } finally {
      clearTimeout(timer)
      if (close) res.off('close', close)
      if (release) { release(); controllers.delete(controller) }
      try { audit({ tool, outcome, elapsed_ms: Math.max(0, Math.trunc(performance.now() - started)) }) } catch {}
    }
  }
  return { childEnv, dispatch, revoke, dispose() { closed = true; revoke() } }
}
