/**
 * lib/cookie.js —— B 站 Cookie 指紋（buvid3 / buvid4）拼接與進程內快取。
 * ============================================================================
 * 硬規則：cookie 的內容（buvid / SESSDATA）一律不進日誌、不進報告、不進錯誤訊息。
 * 這個檔案只做兩件事：
 *   1. 純函數：解析 SESSDATA、解析 finger/spi 回應、拼 Cookie 標頭（可離線自測）。
 *   2. createCookieManager：首次請求前抓一次 buvid 指紋，進程內快取（預設 6 小時），
 *      遇到風控可 force 刷新；dispose() 清掉快取。
 * ============================================================================
 */

export const FINGER_SPI_URL = 'https://api.bilibili.com/x/frontend/finger/spi'

/** 指紋快取預設有效期：6 小時 */
export const DEFAULT_TTL_MS = 6 * 60 * 60 * 1000

/**
 * 從 index.js 組好的 `SESSDATA=...`（也接受完整 cookie 字串）取出 SESSDATA 的值。
 * 值保持原樣（index 已做 encodeURIComponent），不 decode、不驗證。
 * @param {string} cookie
 * @returns {string}
 */
export function sessdataFromCookie(cookie) {
  const m = /(?:^|;\s*)SESSDATA=([^;]*)/.exec(String(cookie ?? ''))
  return m ? m[1].trim() : ''
}

/**
 * 拼 Cookie 標頭：buvid3 / buvid4 有就帶，SESSDATA 有才接在最後。
 * @param {{buvid3?: string, buvid4?: string, sessdata?: string}} [parts]
 * @returns {string}
 */
export function composeCookieHeader({ buvid3, buvid4, sessdata } = {}) {
  const out = []
  const b3 = String(buvid3 ?? '').trim()
  const b4 = String(buvid4 ?? '').trim()
  const sd = String(sessdata ?? '').trim()
  if (b3) out.push(`buvid3=${b3}`)
  if (b4) out.push(`buvid4=${b4}`)
  if (sd) out.push(`SESSDATA=${sd}`)
  return out.join('; ')
}

/**
 * 從 finger/spi 回應取出 b_3 / b_4。拿不到任何一個回 null。
 * @param {any} json
 * @returns {{buvid3: string, buvid4: string}|null}
 */
export function parseFingerSpi(json) {
  const d = json?.data ?? json
  const buvid3 = String(d?.b_3 ?? '').trim()
  const buvid4 = String(d?.b_4 ?? '').trim()
  if (!buvid3 && !buvid4) return null
  return { buvid3, buvid4 }
}

/**
 * 建立 cookie 管理器。
 * @param {object} options
 * @param {(ctx: {signal?: AbortSignal}) => Promise<any>} options.fetchSpi 抓 finger/spi 的函數（回傳 JSON）
 * @param {number} [options.ttlMs] 快取有效期（毫秒）
 * @param {() => number} [options.now] 時鐘（自測用）
 */
export function createCookieManager({ fetchSpi, ttlMs = DEFAULT_TTL_MS, now = () => Date.now() } = {}) {
  if (typeof fetchSpi !== 'function') throw new TypeError('createCookieManager 需要 fetchSpi')

  const ttl = Number.isFinite(ttlMs) && ttlMs > 0 ? ttlMs : DEFAULT_TTL_MS
  let cache = null // { buvid3, buvid4, fetchedAt }
  let inflight = null
  let disposed = false

  const isFresh = () => Boolean(cache) && Number(now()) - cache.fetchedAt < ttl

  async function fetchFresh(signal) {
    const json = await fetchSpi({ signal })
    const parsed = parseFingerSpi(json)
    if (!parsed) throw new Error('finger/spi 没有回传 buvid 指纹')
    if (disposed) throw new Error('cookie 管理器已 dispose')
    cache = { ...parsed, fetchedAt: Number(now()) }
    return cache
  }

  /**
   * 取得指紋（必要時抓 finger/spi）。
   * @param {{signal?: AbortSignal, force?: boolean}} [options]
   * @returns {Promise<{buvid3: string, buvid4: string, fetchedAt: number}|null>}
   */
  async function getBuvid({ signal, force = false } = {}) {
    if (disposed) return null
    if (!force && isFresh()) return cache
    if (!inflight) {
      inflight = fetchFresh(signal).finally(() => {
        inflight = null
      })
    }
    return inflight
  }

  /**
   * 取得要放進 Cookie 標頭的字串。指紋抓不到時盡力而為（只帶 SESSDATA），不讓整條請求失敗。
   * @param {{sessdata?: string, signal?: AbortSignal, force?: boolean}} [options]
   * @returns {Promise<string>}
   */
  async function getCookieHeader({ sessdata = '', signal, force = false } = {}) {
    if (disposed) return composeCookieHeader({ sessdata })
    let buvid = cache
    try {
      buvid = await getBuvid({ signal, force })
    } catch {
      // 指紋抓不到就沿用舊快取（可能沒有）；絕不把錯誤內容往外塞，避免帶出 cookie。
      buvid = cache
    }
    return composeCookieHeader({ buvid3: buvid?.buvid3, buvid4: buvid?.buvid4, sessdata })
  }

  /** 強制刷新指紋（412 時用）。 */
  const refresh = ({ signal } = {}) => getBuvid({ signal, force: true })

  /** 清掉快取並停用（插件 dispose 時呼叫）。 */
  function dispose() {
    disposed = true
    cache = null
    inflight = null
  }

  return { getBuvid, getCookieHeader, refresh, dispose }
}
