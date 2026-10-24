/**
 * B 站 WBI 簽名（純函數，零依賴）。
 * 演算法來源：bilibili-API-collect 公開文檔的 WBI 簽名規範。
 */
import { createHash } from 'node:crypto'

// 固定的 mixinKey 抽取表（64 個索引）
const MIXIN_KEY_ENC_TAB = [
  46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
  33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
  61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
  36, 20, 34, 44, 52,
]

/** md5 十六進位（Node 內建） */
export function md5Hex(str) {
  return createHash('md5').update(String(str)).digest('hex')
}

/** 從 img_key + sub_key 拼接字串算出 mixinKey（取前 32 位） */
export function getMixinKey(orig) {
  return MIXIN_KEY_ENC_TAB.map((n) => orig[n]).join('').slice(0, 32)
}

/**
 * 對參數做 WBI 簽名。
 * @param {Record<string, string|number>} params 要簽名的參數
 * @param {string} imgKey nav 介面的 img_key
 * @param {string} subKey nav 介面的 sub_key
 * @param {number} [wts] 秒級時間戳；省略用現在時間（可測性用）
 * @returns {{query: string, wts: number, wRid: string}}
 */
export function encWbi(params, imgKey, subKey, wts) {
  const mixinKey = getMixinKey(String(imgKey) + String(subKey))
  const t = wts ?? Math.round(Date.now() / 1000)
  const chrFilter = /[!'()*]/g
  // wts 也要進簽名參數（加入後再一起排序），w_rid = md5(query + mixinKey)
  const merged = { ...params, wts: t }
  const query = Object.keys(merged)
    .sort()
    .map((key) => {
      const value = String(merged[key]).replace(chrFilter, '')
      return `${encodeURIComponent(key)}=${encodeURIComponent(value)}`
    })
    .join('&')
  const wRid = md5Hex(`${query}${mixinKey}`)
  return { query: `${query}&w_rid=${wRid}`, wts: t, wRid }
}
