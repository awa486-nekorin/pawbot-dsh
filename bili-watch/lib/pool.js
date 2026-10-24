/**
 * lib/pool.js —— 進程內候選池與「判重前移」的純函數（可離線自測）。
 * ============================================================================
 * 候選池只存在進程內、這一輪有效；跨輪判重靠 seen.json（watched）＋
 * shared.json（shared）＋ reports TTL（hasRecent）。
 * ============================================================================
 */

/**
 * 從一批簡表裡剔掉「已看過 / 已分享 / 池內已有」的，回 fresh 與跳過數。
 * @param {Array<{bvid?: string}>} briefs
 * @param {object} options
 * @param {Record<string, any>} [options.seen] seen.json 內容
 * @param {Record<string, any>} [options.shared] shared.json 內容
 * @param {(bvid: string) => boolean} [options.hasRecent] 近期已有報告的判斷（TTL）
 * @param {{has(bvid:string):boolean}|Set<string>|Map<string,any>} [options.pool] 池內 bvid
 * @returns {{fresh: Array, skippedSeen: number}}
 */
export function filterFresh(briefs, { seen = {}, shared = {}, hasRecent, pool } = {}) {
  const seenMap = seen && typeof seen === 'object' ? seen : {}
  const sharedMap = shared && typeof shared === 'object' ? shared : {}
  const recent = typeof hasRecent === 'function' ? hasRecent : () => false
  const poolHas = pool && typeof pool.has === 'function' ? pool : null

  const fresh = []
  const batch = new Set()
  let skippedSeen = 0
  for (const b of briefs || []) {
    const bvid = String(b?.bvid ?? '').trim()
    if (!bvid) continue
    if (batch.has(bvid)) continue // 同一批內部去重
    batch.add(bvid)
    if (poolHas && poolHas.has(bvid)) continue // 池內已有，不算 skip（是重複入池）
    if (seenMap[bvid] || sharedMap[bvid] || recent(bvid)) {
      skippedSeen++
      continue
    }
    fresh.push(b)
  }
  return { fresh, skippedSeen }
}

/**
 * 候選池目標大小（仿上游：Math.max(5, searchNum || 20)）。
 * @param {number|string} [searchNum]
 * @returns {number}
 */
export function poolTargetFrom(searchNum) {
  return Math.max(5, Number(searchNum) || 20)
}
