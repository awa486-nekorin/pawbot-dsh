/**
 * lib/seen.js —— 判重表 seen.json（插件寫 watched/skipped，PawBot 寫 shared）。
 * ============================================================================
 * 格式：{ "<bvid>": { "seen_at": ISO, "last_kind": "watched"|"shared"|"skipped",
 *                    "watched_at": ISO, "shared_at": ISO, "skipped_at": ISO,
 *                    "skip_reason": string } }
 * - 插件自己寫 `watched`（真正看完一條後記一筆）。
 * - `skipped`：片太長（too_long）或明確失敗（view 拿不到 / LLM 失敗 / 網路錯誤）。
 *   不記的話，熱門榜首若是 2 小時直播回放，每 90 分鐘都會再挑到同一條、白打一次請求。
 * - `shared` 仍由 PawBot 寫在 shared.json（跨進程契約，不動）；
 *   seen.json 是插件內部、可手工清的表，格式保留 shared 欄位以備日後對齊。
 * ============================================================================
 */
import { readJson, writeJson } from './atomic.js'
import { join } from 'node:path'

/**
 * 合併一次「看到」事件（純函數，可離線自測）。
 * @param {Record<string, any>} existing 現有 seen 表
 * @param {string} bvid
 * @param {'watched'|'shared'|'skipped'} kind
 * @param {string} [at] ISO8601；省略用現在時間
 * @param {{reason?: string}} [meta] kind='skipped' 時可帶原因（只記簡短字串）
 * @returns {Record<string, any>} 新的 seen 表
 */
export function mergeSeen(existing, bvid, kind, at, meta = null) {
  const out = { ...(existing && typeof existing === 'object' ? existing : {}) }
  const b = String(bvid ?? '').trim()
  if (!b) return out
  const iso = at || new Date().toISOString()
  const prev = out[b] && typeof out[b] === 'object' ? out[b] : {}
  const next = { ...prev, seen_at: iso, last_kind: kind }
  if (kind === 'watched') next.watched_at = iso
  if (kind === 'shared') next.shared_at = iso
  if (kind === 'skipped') {
    next.skipped_at = iso
    const reason = String(meta?.reason ?? '').trim()
    if (reason) next.skip_reason = reason
  }
  out[b] = next
  return out
}

/**
 * 這個 watchBvid 結果要不要記一筆 skipped（純函數，可離線自測）。
 * - 成功（ok:true）→ 不記（成功路徑自己寫 watched）。
 * - recent → 不記：它只是「72 小時內已有報告」，TTL 過了本來就該能再看。
 * - too_long → 記：再挑一百次也還是太長。
 * - 明確失敗（有 error）→ 記：避免每輪都重打同一個壞掉的請求。
 * @param {any} result
 * @returns {boolean}
 */
export function shouldMarkSkipped(result) {
  const r = result && typeof result === 'object' ? result : {}
  if (r.ok) return false
  if (r.aborted) return false // 被 dispose/呼叫方中止不是這條片的問題
  if (r.skipped) return r.reason === 'too_long'
  return Boolean(String(r.error ?? '').trim())
}

/** 讀 ROOT/seen.json（壞檔/缺檔回空表）。 */
export function readSeen(rootDir) {
  const s = readJson(join(rootDir, 'seen.json'), {})
  return s && typeof s === 'object' ? s : {}
}

/** 記一筆並落盤。 */
export function markSeen(rootDir, bvid, kind, at, meta = null) {
  const next = mergeSeen(readSeen(rootDir), bvid, kind, at, meta)
  writeJson(join(rootDir, 'seen.json'), next)
  return next
}

/** 某 bvid 是否已記在看過/分享過表裡。 */
export function hasSeen(seen, bvid) {
  return Boolean(seen && typeof seen === 'object' && seen[String(bvid)])
}
