/**
 * 模型守門（白名單，純函數，可離線自測）
 * ============================================================================
 * ⛔ 用戶 2026-10-25 定下的硬規則：**不允許任何 V4 Pro 的調用，只能用 V4 Flash。**
 *    （用戶原話：V4.1 Flash 比 V4 Pro 強很多，而且 pro 烧 token。）
 *
 * 為什麼是白名單、不是黑名單：
 *   黑名單（舊版 /v4[\s_-]?pro/i）實測擋不住 `deepseek-v4.1-pro`、`deepseek-pro`、
 *   `deepseek-v4-0528-pro`、`deepseek-v4.0-pro`、`v4–pro`（en dash）這些寫法，
 *   而且 gemini / claude 之類的非 flash 模型會原樣通過。
 *   規則應該是「只准我明確列出來的名字」，所以這裡改成白名單：
 *   不在 ALLOWED_MODELS 裡的一律退回 HARD_FALLBACK，並回 changed/reason 讓呼叫方留日誌。
 *
 * `allowedModel` 本身也要在白名單內：若有人把 allowedModel 也設成 v4-pro，
 * 一樣退回 HARD_FALLBACK——「允許的模型」不能拿來繞過規則。
 * ============================================================================
 */

/** 硬性兜底：任何情況下的最終模型。 */
export const HARD_FALLBACK = 'deepseek-flash'

/**
 * 唯一允許的模型清單（精確比對、大小寫敏感）。
 * 要加別名只能在這裡明確列舉，不能模糊匹配——模糊匹配就是下一個黑名單。
 */
export const ALLOWED_MODELS = Object.freeze(['deepseek-flash'])

/** 預設允許的模型（= 硬性兜底）。 */
export const ALLOWED_MODEL = HARD_FALLBACK

/** 這個名字是不是在白名單裡（空值不算）。 */
export function isAllowedModel(name) {
  const n = String(name ?? '').trim()
  return n.length > 0 && ALLOWED_MODELS.includes(n)
}

/**
 * 這個名字是不是「不在白名單」（含空值）。
 * 函數名保留給既有呼叫點；語義已從「命中 v4-pro 黑名單」改成「不在白名單」。
 */
export function isBannedModel(name) {
  return !isAllowedModel(name)
}

/**
 * 把模型名收斂到允許的範圍。
 * @param {unknown} name     設定裡讀出來的模型名（model）
 * @param {unknown} allowed  允許使用的模型（allowedModel），預設 flash
 * @returns {{ model: string, changed: boolean, requested: string, allowed: string, reason: string }}
 *   changed=true 表示原本的名字沒被採用（不在白名單、或是空值、或 allowed 本身違規）。
 *   reason: 'ok' | 'empty' | 'not-allowed'
 */
export function guardModel(name, allowed = ALLOWED_MODEL) {
  const requested = String(name ?? '').trim()
  const allowedRaw = String(allowed ?? '').trim()

  // 連「允許的模型」都必須在白名單裡，否則規則就被繞過去了
  const allowedSafe = isAllowedModel(allowedRaw) ? allowedRaw : HARD_FALLBACK

  if (!requested) {
    return { model: allowedSafe, changed: true, requested, allowed: allowedSafe, reason: 'empty' }
  }
  if (!isAllowedModel(requested)) {
    return { model: allowedSafe, changed: true, requested, allowed: allowedSafe, reason: 'not-allowed' }
  }
  return { model: requested, changed: false, requested, allowed: allowedSafe, reason: 'ok' }
}
