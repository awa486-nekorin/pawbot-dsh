/**
 * lib/request-lifecycle.js —— 請求檔的結案規則（fs，可用臨時目錄自測）。
 * ============================================================================
 * 舊版 `processRequest` 無條件 moveToDone：`watchBvid` 回 `{ok:false}` 被丟掉，
 * 使用者說「看這條」失敗了就永遠沒有報告、也沒有重試。
 *
 * 新規則：
 *   - 成功（ok:true）或「明確的終點跳過」（skipped:true，例如 recent / too_long /
 *     搜尋成功但都看過）→ 移到 requests/done/；
 *   - 失敗（ok:false 且沒有 skipped，例如 view 拿不到 / LLM 兩次失敗 / 網路錯誤）
 *     → 檔內記 `attempts`、`last_error`；第 1 次失敗留在 requests/ 等下一輪重試，
 *       第 MAX_REQUEST_ATTEMPTS 次失敗才移到 requests/failed/。
 *
 * `last_error` 只留去敏後的字串（沒有 auth_key / SESSDATA），並截斷長度。
 * ============================================================================
 */
import { mkdirSync, readFileSync, renameSync } from 'node:fs'
import { basename, join } from 'node:path'

import { atomicWriteFile } from './atomic.js'
import { redactSecrets } from './http.js'

/** 同一個請求最多嘗試幾次（含第一次）。 */
export const MAX_REQUEST_ATTEMPTS = 2

/** 記在檔內的錯誤字串上限，避免壞錯誤訊息把請求檔撐爆。 */
export const MAX_LAST_ERROR_CHARS = 500

/**
 * 依 outcome / 已嘗試次數決定結案動作（純函數）。
 * @param {{ok?: boolean, skipped?: boolean, reason?: string, error?: string}} outcome
 * @param {number} attempts 這個檔案先前已失敗的次數（第一次嘗試 = 0）
 * @param {number} [maxAttempts]
 * @returns {'done'|'retry'|'failed'}
 */
export function nextRequestAction(outcome, attempts = 0, maxAttempts = MAX_REQUEST_ATTEMPTS) {
  const o = outcome && typeof outcome === 'object' ? outcome : {}
  if (o.ok) return 'done'
  if (o.skipped) return 'done' // 明確終點（recent / too_long / 沒候選）：重試也不會變成功
  const max = Math.max(1, Math.floor(Number(maxAttempts) || MAX_REQUEST_ATTEMPTS))
  const n = Math.max(0, Math.floor(Number(attempts) || 0))
  return n + 1 >= max ? 'failed' : 'retry'
}

/** 讀回請求檔（壞掉就從空物件開始，不要讓結案本身失敗）。 */
function readRequestObj(file) {
  try {
    const obj = JSON.parse(readFileSync(file, 'utf8'))
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {}
  } catch {
    return {}
  }
}

/**
 * 結案：改寫 attempts/last_error 並依規則搬檔。
 * @param {object} params
 * @param {string} params.file 請求檔絕對路徑
 * @param {object} params.outcome watchBvid / processRequest 的結果
 * @param {number} [params.attempts] 先前已失敗次數
 * @param {string} params.doneDir
 * @param {string} params.failedDir
 * @param {number} [params.maxAttempts]
 * @param {() => string} [params.now] ISO 時鐘（自測用）
 * @returns {{action: 'done'|'retry'|'failed', file: string, movedTo?: string, attempts: number}}
 */
export function settleRequest({
  file,
  outcome,
  attempts = 0,
  doneDir,
  failedDir,
  maxAttempts = MAX_REQUEST_ATTEMPTS,
  now = () => new Date().toISOString(),
}) {
  const action = nextRequestAction(outcome, attempts, maxAttempts)
  const name = basename(file)
  const n = Math.max(0, Math.floor(Number(attempts) || 0))

  if (action === 'done') {
    mkdirSync(doneDir, { recursive: true })
    const target = join(doneDir, name)
    renameSync(file, target)
    return { action, file, movedTo: target, attempts: n }
  }

  const next = {
    ...readRequestObj(file),
    attempts: n + 1,
    last_attempt_at: now(),
  }
  const error = String(outcome?.error ?? outcome?.reason ?? 'unknown')
  next.last_error = redactSecrets(error).slice(0, MAX_LAST_ERROR_CHARS)

  if (action === 'failed') {
    mkdirSync(failedDir, { recursive: true })
    // 先把 attempts/last_error 寫回原檔，再搬到 failed/，failed/ 裡的那份才是完整的
    atomicWriteFile(file, JSON.stringify(next, null, 2))
    const target = join(failedDir, name)
    renameSync(file, target)
    return { action, file, movedTo: target, attempts: next.attempts }
  }

  // retry：留在 requests/，下一輪 tick 會再撿起來
  atomicWriteFile(file, JSON.stringify(next, null, 2))
  return { action, file, attempts: next.attempts }
}
