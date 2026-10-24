/**
 * lib/notes.js —— 人讀筆記 md（純函數，可離線自測）。
 * ============================================================================
 * 以 JSON 報告為主，md 是可選增值（Config `notesMd` 預設 true）。
 * 模板照規格 §5.4（去尖刻人設，改「它怎麼說」用大肥魚口吻）。
 * ============================================================================
 */

/** 秒 → `MM:SS` 或 `H:MM:SS`（給人看） */
export function fmtDur(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const p = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${p(m)}:${p(r)}` : `${m}:${p(r)}`
}

/** 標題轉檔名：非法字符換 `-`，保留中文，去空白，截 80 字。 */
export function sanitizeTitle(title) {
  return String(title ?? '')
    .replace(/[\\/:*?"<>|\r\n\t]+/g, '-')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80)
}

/**
 * 把一份報告拼成 md（模板 §5.4）。
 * @param {object} report buildReport 的輸出
 * @returns {string}
 */
export function buildNoteMd(report) {
  const r = report && typeof report === 'object' ? report : {}
  const title = String(r.title ?? '').trim() || '（无标题）'
  const up = String(r.author ?? '').trim() || '未知'
  const url = String(r.url ?? `https://www.bilibili.com/video/${r.bvid ?? ''}`)
  const play = Number(r.play ?? 0) || 0
  const durationSec = Number(r.duration_sec ?? 0) || 0

  const summary = String(r.summary ?? '').trim() || '（无摘要）'
  const lead = summary.length > 120 ? summary.slice(0, 120) + '…' : summary

  const highlights = (Array.isArray(r.highlights) ? r.highlights : []).filter((x) => String(x ?? '').trim())
  const shareReason = String(r.share_reason ?? '').trim()
  const take = String(r.take ?? '').trim()

  const lines = []
  lines.push(`# ${title}`)
  lines.push(`> ${lead}`)
  lines.push(`- UP：${up} / 链接：${url} / 播放量：${play} / 时长：${fmtDur(durationSec)}`)
  lines.push('')
  lines.push('## 讲了什么')
  lines.push(summary)
  lines.push('')
  lines.push('## 关键点')
  if (highlights.length) {
    for (const h of highlights) lines.push(`- ${h}`)
  } else {
    lines.push('- （无）')
  }
  lines.push('')
  lines.push('## 为什么值得看')
  lines.push(shareReason || '（未说明）')
  lines.push('')
  lines.push('## 它怎么说')
  lines.push(take || '（无）')
  return lines.join('\n') + '\n'
}
