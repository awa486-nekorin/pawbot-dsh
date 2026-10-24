/**
 * B 站字幕 JSON → 帶時間戳純文字（純函數，零依賴）。
 */
function pad(n) {
  return String(n).padStart(2, '0')
}

/** 秒 → `MM:SS` 或 `HH:MM:SS` */
export function secToTs(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(r)}` : `${pad(m)}:${pad(r)}`
}

/**
 * 把字幕 JSON 拼成 `[MM:SS] 內容` 的純文字，超長截斷。
 * @param {object} json B 站字幕 JSON（{ body: [{ from, to, content }] }）
 * @param {number} maxChars 最大字數；<=0 表示不截斷
 * @returns {string}
 */
export function subtitleJsonToText(json, maxChars) {
  const body = Array.isArray(json?.body) ? json.body : []
  const lines = []
  let total = 0
  for (const item of body) {
    const content = String(item?.content ?? '').replace(/\s+/g, ' ').trim()
    if (!content) continue
    const line = `[${secToTs(item?.from)}] ${content}`
    if (maxChars > 0 && total + line.length + 1 > maxChars) break
    lines.push(line)
    total += line.length + 1
  }
  return lines.join('\n')
}
