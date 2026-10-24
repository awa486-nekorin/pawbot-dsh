/**
 * 報告純函數：schema 驗證、watched_level 修正、時間戳剝離。零依賴。
 */

/** 截斷字串（把多餘空白收成單一空格） */
export function truncate(s, n) {
  const t = String(s ?? '').replace(/\s+/g, ' ').trim()
  return t.length > n ? t.slice(0, n) + '…' : t
}

/** 程式決定 watched_level（不信模型） */
export function determineWatchedLevel({ hasSubtitle, hasDesc, hasComments }) {
  if (hasSubtitle) return 'subtitle'
  if (hasDesc || hasComments) return 'desc_comments'
  return 'title_only'
}

// 開頭的時間戳（`03:12`、`[01:02:03]`、`03:12 講了…`）
const TS_RE = /^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*[-–—:：]?\s*)/

/** 剝掉開頭的時間戳（非 subtitle 等級時用） */
export function stripTimestamp(s) {
  return String(s ?? '').replace(TS_RE, '').trim()
}

function toStr(v, max) {
  return truncate(v, max)
}

function toInt(v) {
  const n = Number(v)
  return Number.isFinite(n) ? Math.floor(n) : 0
}

function toBool(v) {
  return v === true || v === 'true' || v === 1 || v === '1'
}

function capArray(v, max) {
  if (!Array.isArray(v)) return []
  return v.slice(0, max)
}

/**
 * 把 LLM 產出的內容 + 程式抓到的 metadata，組合成符合 schema 的報告。
 * 程式補齊 metadata、強制 watched_level、修正字段型別/長度。
 * @param {object} meta 抓取層 metadata（bvid/title/.../availability 旗標）
 * @param {object} content LLM 回傳的內容欄位
 * @param {string|null} sourceRequest
 * @param {string} [createdAt] 可注入的 created_at（自測用）
 * @returns {object}
 */
export function buildReport(meta, content, sourceRequest, createdAt) {
  const hasSubtitle = Boolean(meta.hasSubtitle)
  const hasDesc = Boolean(meta.hasDesc)
  const hasComments = Boolean(meta.hasComments)
  const watched = determineWatchedLevel({ hasSubtitle, hasDesc, hasComments })

  const highlights = capArray(content?.highlights, 5)
    .map((s) => (watched === 'subtitle' ? toStr(s, 200) : stripTimestamp(toStr(s, 200))))
    .filter(Boolean)

  const topComments = capArray(content?.top_comments, 5)
    .map((c) => ({ text: truncate(c?.text ?? '', 120), like: toInt(c?.like) }))
    .filter((c) => c.text)

  return {
    version: 1,
    bvid: String(meta.bvid ?? ''),
    url: `https://www.bilibili.com/video/${meta.bvid}`,
    title: toStr(meta.title, 200),
    author: toStr(meta.author, 100),
    duration_sec: toInt(meta.durationSec),
    play: toInt(meta.play),
    pubdate: toStr(meta.pubdate, 40),
    tname: toStr(meta.tname, 100),
    watched_level: watched,
    summary: toStr(content?.summary, 800),
    highlights,
    comment_vibe: toStr(content?.comment_vibe, 300),
    top_comments: topComments,
    take: toStr(content?.take, 300),
    share_worthy: toBool(content?.share_worthy),
    share_reason: toStr(content?.share_reason, 300),
    created_at: createdAt || new Date().toISOString(),
    source_request: sourceRequest || null,
  }
}

/** 報告摘要（工具回傳用） */
export function reportBrief(report) {
  return {
    bvid: report?.bvid,
    title: report?.title,
    watched_level: report?.watched_level,
    summary: report?.summary,
    share_worthy: report?.share_worthy,
    url: report?.url,
  }
}
