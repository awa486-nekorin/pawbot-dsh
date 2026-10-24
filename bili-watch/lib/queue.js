/**
 * 請求佇列掃描（fs，零依賴，可用臨時目錄自測）。
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync } from 'node:fs'
import { join, basename } from 'node:path'

/**
 * 掃描 requests 目錄裡所有 *.json，依 requested_at 升序排序。
 * @param {string} requestsDir
 * @returns {{ok: Array<{file:string, name:string, id?:string, kind?:string, value?:string, requested_at?:string, session?:string, attempts?:number, last_error?:string}>, bad: Array<{file:string, name:string, error:string}>}}
 */
export function scanRequestsDetailed(requestsDir) {
  let names = []
  try {
    names = readdirSync(requestsDir)
  } catch {
    return { ok: [], bad: [] }
  }
  const ok = []
  const bad = []
  for (const name of names) {
    if (!name.endsWith('.json')) continue
    const file = join(requestsDir, name)
    try {
      const obj = JSON.parse(readFileSync(file, 'utf8'))
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
        throw new Error('不是 JSON 对象')
      }
      ok.push({ file, name, ...obj })
    } catch (e) {
      // 壞檔不再靜默跳過：回報給呼叫方，讓 tick 隔離到 requests/bad/
      bad.push({ file, name, error: String(e?.message || e) })
    }
  }
  ok.sort((a, b) => {
    const ta = Date.parse(a?.requested_at || '') || 0
    const tb = Date.parse(b?.requested_at || '') || 0
    return ta - tb || String(a?.name).localeCompare(String(b?.name))
  })
  return { ok, bad }
}

/**
 * 掃描 requests 目錄（舊形狀：只回可用的請求陣列，壞檔仍跳過）。
 * 需要知道壞檔的呼叫點請用 scanRequestsDetailed。
 * @param {string} requestsDir
 * @returns {Array<{file:string, name:string, id?:string, kind?:string, value?:string, requested_at?:string, session?:string}>}
 */
export function scanRequests(requestsDir) {
  return scanRequestsDetailed(requestsDir).ok
}

/**
 * 把掃到的壞檔搬到 requests/bad/（同名檔已存在時加時間戳，避免覆蓋）。
 * @param {string} requestsDir
 * @param {Array<{file:string, name?:string}>} bad scanRequestsDetailed 回傳的 bad
 * @param {{badDir?: string, now?: () => number}} [opts]
 * @returns {{dir: string, moved: string[], failed: Array<{file:string, error:string}>}}
 */
export function quarantineBad(requestsDir, bad, { badDir, now = () => Date.now() } = {}) {
  const dir = badDir || join(requestsDir, 'bad')
  mkdirSync(dir, { recursive: true })
  const moved = []
  const failed = []
  for (const entry of bad || []) {
    const file = typeof entry === 'string' ? entry : entry?.file
    if (!file) continue
    let target = join(dir, basename(file))
    if (existsSync(target)) target = join(dir, `${basename(file, '.json')}.${now()}.json`)
    try {
      renameSync(file, target)
      moved.push(basename(target))
    } catch (e) {
      failed.push({ file, error: String(e?.message || e) })
    }
  }
  return { dir, moved, failed }
}

/**
 * 正規化請求的 `extra`（關鍵詞搜尋參數，向後兼容舊請求沒有 extra 的情況）。
 * @param {any} extra
 * @returns {{order: string, days: number, num: number}}
 */
export function normalizeExtra(extra) {
  const e = extra && typeof extra === 'object' ? extra : {}
  return {
    order: typeof e.order === 'string' && e.order.trim() ? e.order.trim() : '',
    days: Number.isFinite(Number(e.days)) ? Math.max(0, Math.floor(Number(e.days))) : 0,
    num: Number.isFinite(Number(e.num)) ? Math.max(0, Math.floor(Number(e.num))) : 0,
  }
}
