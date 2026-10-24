/**
 * lib/http.js —— B 站請求的純邏輯：標頭、風控判定、重試決策、錯誤訊息、可中斷 sleep。
 * fetch / sleep / random 都可注入，自測不碰網路。
 */

export const BILI_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
export const BILI_REFERER = 'https://www.bilibili.com/'
export const BILI_ORIGIN = 'https://www.bilibili.com'
export const BILI_ACCEPT_LANGUAGE = 'zh-CN,zh;q=0.9,en;q=0.8'

/** 風控預設最多重試次數 */
export const DEFAULT_MAX_RETRIES = 2

/** 單次 fetch 的硬性 timeout：一次 hang 不可以把整個佇列卡死。 */
export const FETCH_TIMEOUT_MS = 15000

/** LLM 單次呼叫的 timeout（index.js 用；放這裡方便自測與調整）。 */
export const LLM_TIMEOUT_MS = 90_000

/**
 * 去掉 URL 的 query / hash，只留 origin + pathname。
 * 錯誤訊息、日誌只准帶這個——字幕 CDN 的 `auth_key` 就在 query 裡。
 * @param {unknown} url
 * @returns {string}
 */
export function redactUrl(url) {
  const raw = String(url ?? '')
  try {
    const u = new URL(raw)
    return u.origin + u.pathname
  } catch {
    return raw.split('?')[0].split('#')[0]
  }
}

/**
 * 把已知的憑證樣式從文字裡抹掉（last_error / 日誌的縱深防禦）。
 * 正常路徑已用 redactUrl，這裡防的是第三方錯誤訊息直接塞完整 URL。
 * @param {unknown} text
 * @returns {string}
 */
export function redactSecrets(text) {
  return String(text ?? '')
    .replace(/(auth_key|access_key)=[^&\s"'<>]+/gi, '$1=***')
    .replace(/SESSDATA=[^;\s"'<>]+/gi, 'SESSDATA=***')
}

/**
 * 把「呼叫方的 AbortSignal」與「timeout」疊成一個 signal。
 * 優先用 AbortSignal.timeout + AbortSignal.any；runtime 沒有就手動 race + controller。
 * 回傳的 cleanup() 一定要在 finally 呼叫（手動路徑要清 timer / listener）。
 * @param {AbortSignal|undefined} signal 呼叫方的 signal
 * @param {number} [timeoutMs]
 * @returns {{signal: AbortSignal|undefined, cleanup: () => void}}
 */
export function withTimeoutSignal(signal, timeoutMs = FETCH_TIMEOUT_MS) {
  const ms = Number(timeoutMs)
  if (!Number.isFinite(ms) || ms <= 0) return { signal, cleanup: () => {} }

  if (typeof AbortSignal.timeout === 'function' && typeof AbortSignal.any === 'function') {
    if (!signal) return { signal: AbortSignal.timeout(ms), cleanup: () => {} }
    if (signal.aborted) return { signal, cleanup: () => {} }
    const timeoutSignal = AbortSignal.timeout(ms)
    return { signal: AbortSignal.any([signal, timeoutSignal]), cleanup: () => {} }
  }

  // 舊 runtime：手動 race（controller + timer + listener，全部自己回收）
  const c = new AbortController()
  const timer = setTimeout(() => {
    const err = new Error(`timeout ${ms}ms`)
    err.name = 'TimeoutError'
    c.abort(err)
  }, ms)
  timer.unref?.()
  let onAbort = null
  if (signal) {
    if (signal.aborted) c.abort(signal.reason)
    else {
      onAbort = () => c.abort(signal.reason)
      signal.addEventListener('abort', onAbort, { once: true })
    }
  }
  return {
    signal: c.signal,
    cleanup: () => {
      clearTimeout(timer)
      if (onAbort && signal) signal.removeEventListener('abort', onAbort)
    },
  }
}

/** 風控 403 的 body 關鍵字（B 站有時用 403 + HTML/JSON 提示，而非 412） */
const RISK_HINT = /风控|風控|风险控制|風險控制|risk\s*control|请求过于频繁|請求過於頻繁|访问频率|訪問頻率|请求被拦截|請求被攔截|拦截|攔截|forbidden/i

/**
 * 是否 B 站自家網域（bilibili.com / *.bilibili.com）。
 * Cookie（尤其 SESSDATA）只可以送給這個網域，字幕 CDN 的 hdslb.com 不行。
 * @param {string} url
 * @returns {boolean}
 */
export function isBilibiliHost(url) {
  try {
    const u = new URL(String(url))
    return u.protocol === 'https:' && (u.hostname === 'bilibili.com' || u.hostname.endsWith('.bilibili.com'))
  } catch {
    return false
  }
}

/**
 * 組 B 站請求標頭。
 * @param {string} [cookie] 完整的 Cookie 標頭值（buvid + 選配 SESSDATA）
 * @param {Record<string,string>} [extra] 額外/覆寫標頭
 * @returns {Record<string,string>}
 */
export function buildHeaders(cookie = '', extra = {}) {
  const h = {
    'User-Agent': BILI_UA,
    Accept: 'application/json, text/plain, */*',
    'Accept-Language': BILI_ACCEPT_LANGUAGE,
    Referer: BILI_REFERER,
    Origin: BILI_ORIGIN,
  }
  if (cookie) h.Cookie = cookie
  return { ...h, ...(extra || {}) }
}

/**
 * 判斷這次回應是否為 B 站風控。
 * 412 一律算；403 要看 body 是否有風控提示（單純 403 可能只是權限/不存在，不重試）。
 * @param {number} status HTTP 狀態碼
 * @param {string} [body] 回應文字（只在失敗時讀）
 * @returns {boolean}
 */
export function isRiskControl(status, body = '') {
  const code = Number(status)
  if (code === 412) return true
  if (code === 403) return RISK_HINT.test(String(body ?? ''))
  return false
}

/**
 * 重試前等待時間：800~1500ms。
 * @param {() => number} [random] 注入亂數（自測用）
 * @returns {number}
 */
export function retryDelayMs(random = Math.random) {
  const raw = Number(random())
  const r = Number.isFinite(raw) ? Math.min(1, Math.max(0, raw)) : 0
  return 800 + Math.floor(r * 700)
}

/**
 * 依「目前失敗次數」決定要不要重試。
 * @param {object} options
 * @param {number} options.status HTTP 狀態碼
 * @param {string} [options.body] 回應文字
 * @param {number} [options.attempt] 已經重試過的次數（第一次失敗 = 0）
 * @param {number} [options.maxRetries]
 * @param {() => number} [options.random]
 * @returns {{retry: boolean, refreshBuvid: boolean, risk: boolean, status: number, delayMs: number}}
 */
export function decideRetry({ status, body = '', attempt = 0, maxRetries = DEFAULT_MAX_RETRIES, random = Math.random } = {}) {
  const risk = isRiskControl(status, body)
  const retry = risk && Number(attempt) < Number(maxRetries)
  return {
    retry,
    // 風控時刷新 buvid 指紋再試（快取失效或指紋被盯上時有用）
    refreshBuvid: retry,
    risk,
    status: Number(status),
    delayMs: retry ? retryDelayMs(random) : 0,
  }
}

/**
 * 重試耗盡後拋出的明確風控錯誤。訊息只含狀態碼與次數，絕不含 cookie。
 * @param {number} status
 * @param {number} [attempts] 已重試次數
 * @param {unknown} [cause]
 * @returns {Error & {status: number, attempts: number, risk: true}}
 */
export function riskControlError(status, attempts = 0, cause) {
  const code = Number(status) || 412
  const err = new Error(`B站风控 ${code}（已重试 ${attempts} 次，可能是请求太频繁或缺 Cookie）`)
  err.name = 'BiliRiskControlError'
  err.status = code
  err.attempts = attempts
  err.risk = true
  if (cause !== undefined) err.cause = cause
  return err
}

/**
 * 可被 AbortSignal 中斷的 sleep（重試等待時不卡住 dispose）。
 * @param {number} ms
 * @param {AbortSignal} [signal]
 * @returns {Promise<void>}
 */
export function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const delay = Math.max(0, Number(ms) || 0)
    if (signal?.aborted) {
      reject(signal.reason ?? new Error('aborted'))
      return
    }
    const onAbort = () => {
      clearTimeout(timer)
      reject(signal.reason ?? new Error('aborted'))
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, delay)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * 發請求並在風控時重試：412（或 403 且 body 提示風控）→ 等 800~1500ms、
 * 刷新 buvid 指紋、最多再試 maxRetries 次；仍失敗拋出明確風控錯誤。
 * fetch / sleep / random 都可注入，方便離線自測。
 * @param {string} url
 * @param {object} [options]
 * @param {{getCookieHeader?: Function, refresh?: Function}|null} [options.cookieManager]
 * @param {string} [options.cookie] 沒有 cookieManager 時直接用的 Cookie 標頭
 * @param {string} [options.sessdata]
 * @param {Record<string,string>} [options.headers]
 * @param {AbortSignal} [options.signal]
 * @param {typeof fetch} [options.fetchImpl]
 * @param {number} [options.maxRetries]
 * @param {() => number} [options.random]
 * @param {(ms:number, signal?:AbortSignal)=>Promise<void>} [options.sleepImpl]
 * @param {number} [options.timeoutMs] 單次 fetch 的 timeout（預設 15 秒）
 * @returns {Promise<any>} 回應 JSON
 */
export async function fetchWithRetry(
  url,
  {
    cookieManager = null,
    cookie = '',
    sessdata = '',
    headers = {},
    signal,
    fetchImpl = fetch,
    maxRetries = DEFAULT_MAX_RETRIES,
    random = Math.random,
    sleepImpl = sleep,
    timeoutMs = FETCH_TIMEOUT_MS,
  } = {},
) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetchWithRetry 需要 fetchImpl')
  let retries = 0
  for (;;) {
    // 呼叫方已經中止（例如插件 dispose）：不要再發請求、也不要用同一個 aborted signal 重試
    if (signal?.aborted) throw signal.reason ?? new Error('aborted')

    let res
    let json
    let body = ''
    const combined = withTimeoutSignal(signal, timeoutMs)
    try {
      let cookieHeader = cookie
      if (typeof cookieManager?.getCookieHeader === 'function') {
        cookieHeader = await cookieManager.getCookieHeader({ sessdata, signal: combined.signal })
      }
      res = await fetchImpl(url, { headers: buildHeaders(cookieHeader, headers), signal: combined.signal })
      if (res.ok) {
        json = await res.json()
      } else {
        try {
          body = await res.text()
        } catch {
          /* body 讀不到不影響判定 */
        }
      }
    } finally {
      combined.cleanup()
    }
    if (res.ok) return json

    const plan = decideRetry({ status: res.status, body, attempt: retries, maxRetries, random })
    if (!plan.retry) {
      if (plan.risk) throw riskControlError(res.status, retries)
      // ⛔ 錯誤訊息只留 origin + pathname：query 裡可能有 w_rid / auth_key
      throw new Error(`HTTP ${res.status}：${redactUrl(url)}`)
    }
    if (signal?.aborted) throw signal.reason ?? new Error('aborted')
    await sleepImpl(plan.delayMs, signal)
    if (plan.refreshBuvid && typeof cookieManager?.refresh === 'function') {
      // 指紋刷新也吃同一個 timeout，否則「重試」這一步本身可以 hang
      const refreshTimed = withTimeoutSignal(signal, timeoutMs)
      try {
        await cookieManager.refresh({ signal: refreshTimed.signal })
      } catch {
        /* 指紋刷新失敗就沿用舊的，重試本身仍照做 */
      } finally {
        refreshTimed.cleanup()
      }
    }
    retries += 1
  }
}
