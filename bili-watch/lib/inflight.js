/**
 * lib/inflight.js —— 同一個 key 的 in-flight 去重（純邏輯，可離線自測）。
 * ============================================================================
 * 為什麼需要：佇列 tick 與 `bili_watch` 工具是兩條獨立路徑，舊版只用 `busy` 互斥
 * tick/autoTick，工具路徑完全不碰它，於是同一條 bvid 可能被同時看兩次：
 * 兩次 LLM、報告互相覆蓋、watchedToday 重複 +1。
 * 這裡把「同一個 key 只跑一次」做成共用的小工具：後到的呼叫直接 await 既有 Promise，
 * 跑完（成功或失敗）就從 Map 移除，下一次呼叫才會重新執行。
 * ============================================================================
 */

/**
 * 若 key 已在跑，回傳既有的 Promise；否則跑 task 並把 Promise 放進 map。
 * @template T
 * @param {Map<string, Promise<T>>} map 呼叫方持有的 in-flight 表
 * @param {string} key 去重鍵（這裡是 bvid）
 * @param {() => Promise<T>|T} task
 * @returns {Promise<T>}
 */
export function dedupeInflight(map, key, task) {
  if (!(map instanceof Map)) throw new TypeError('dedupeInflight 需要 Map')
  const k = String(key ?? '').trim()
  if (!k) throw new TypeError('dedupeInflight 需要非空 key')
  if (typeof task !== 'function') throw new TypeError('dedupeInflight 需要 task 函数')

  const existing = map.get(k)
  if (existing) return existing

  // Promise.resolve().then 讓 task 一定在 map.set 之後才開始跑，避免同步 throw 導致洩漏
  const p = Promise.resolve()
    .then(task)
    .finally(() => {
      if (map.get(k) === p) map.delete(k)
    })
  map.set(k, p)
  return p
}

/** 目前有幾條在跑（狀態工具/測試用）。 */
export function inflightSize(map) {
  return map instanceof Map ? map.size : 0
}
