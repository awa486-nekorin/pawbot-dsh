/**
 * lib/config-utils.js —— 純函數：config 的遮罩 / 去遮罩值 / 深合併 / 區段過濾 / 差異。
 * ============================================================================
 * 全部是無副作用純函數，方便離線單測（test/offline.mjs）。
 * 這些函數**不碰 PawBot 的 data/config.json**：遮罩 / 過濾只在記憶體裡做，
 * 真正的讀寫一律走 PawBot 的 HTTP API（見 index.js 的 pawbotFetch）。
 * ============================================================================
 */

/** 回給瀏覽器 / 模型時，secrets 一律換成這個值 */
export const MASK = '***已设定***'

/** 人設檔大小上限（位元組，200KB） */
export const PERSONA_MAX_BYTES = 200 * 1024

/**
 * 判斷一個 key 是不是要遮的 secret。
 * 覆蓋：api_key / access_token / refresh_token / cloud_key / *secret* / password /
 *       authorization / bearer / sessid / sessdata / cookie / private_key / sign_key，
 * 以及「剛好叫 token」的 key——但刻意**不**誤傷 `max_tokens` 這類數量欄位。
 */
const SECRET_KEY_RE =
  /(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|cloud[_-]?key|secret|password|passwd|authorization|bearer|sessid|sessdata|cookie|private[_-]?key|sign[_-]?key)/i

export function isSecretKey(key) {
  if (key == null) return false
  const k = String(key)
  if (SECRET_KEY_RE.test(k)) return true
  if (/^token$/i.test(k)) return true
  return false
}

function isPlainObject(v) {
  return v != null && typeof v === 'object' && !Array.isArray(v)
}

function clone(v) {
  if (Array.isArray(v)) return v.map(clone)
  if (isPlainObject(v)) {
    const out = {}
    for (const [k, val] of Object.entries(v)) out[k] = clone(val)
    return out
  }
  return v
}

/**
 * 深複製一份，並把 secret key 的值遮成 MASK（不遮原物件）。
 * 空值（'' / null / undefined）不遮也不列入 maskedKeys——面板才能誠實顯示「未設定」，
 * 而不是把沒填的 key 也說成「已設定」。
 * @returns {{ value: any, maskedKeys: string[] }} maskedKeys 是 dotted path（如 "llm.api_key"）
 */
export function maskSecrets(value) {
  const maskedKeys = []
  const walk = (v, path) => {
    if (Array.isArray(v)) return v.map((x, i) => walk(x, `${path}[${i}]`))
    if (isPlainObject(v)) {
      const out = {}
      for (const [k, val] of Object.entries(v)) {
        const p = path ? `${path}.${k}` : k
        if (isSecretKey(k) && val !== '' && val != null) {
          out[k] = MASK
          maskedKeys.push(p)
        } else {
          out[k] = walk(val, p)
        }
      }
      return out
    }
    return v
  }
  return { value: walk(value, ''), maskedKeys }
}

/**
 * 深複製一份，並把「值等於 MASK 的 key」整個丟掉——避免把遮罩當真值寫回 config。
 * @returns {{ value: any, dropped: string[] }} dropped 是被丟掉的 dotted path
 */
export function pickUpdatable(payload) {
  const dropped = []
  const walk = (v, path) => {
    if (Array.isArray(v)) return v.map((x, i) => walk(x, `${path}[${i}]`))
    if (isPlainObject(v)) {
      const out = {}
      for (const [k, val] of Object.entries(v)) {
        const p = path ? `${path}.${k}` : k
        if (val === MASK) {
          dropped.push(p)
          continue
        }
        out[k] = walk(val, p)
      }
      return out
    }
    return v
  }
  return { value: walk(payload, ''), dropped }
}

/**
 * 深合併（鏡像 PawBot config.py 的 `_deep_merge` 語義）：
 * patch 的 dict 值與 base 的 dict 值遞迴合併，其餘（含陣列）直接取代。
 * 不回傳共享引用（base/patch 都不會被改）。
 */
export function deepMerge(base, patch) {
  const out = clone(base)
  if (!isPlainObject(patch)) return out
  for (const [k, v] of Object.entries(patch)) {
    if (isPlainObject(v) && isPlainObject(out[k])) {
      out[k] = deepMerge(out[k], v)
    } else {
      out[k] = clone(v)
    }
  }
  return out
}

/**
 * 區段過濾：section 給定就只回 `{ [section]: ... }`；不給 / 空字串回整份（深複製）。
 * section 不存在回 `{}`。
 */
export function pickSection(config, section) {
  if (section == null || String(section) === '') return clone(config)
  const k = String(section)
  if (!isPlainObject(config) || !(k in config)) return {}
  return { [k]: clone(config[k]) }
}

/**
 * 差異摘要：只列「變了」的 dotted key 名，不列值。
 * 用於 `pawbot_config_set` 回報改動前後差異。
 */
export function diffKeys(before, after) {
  const changed = []
  const walk = (a, b, path) => {
    if (isPlainObject(a) && isPlainObject(b)) {
      const keys = new Set([...Object.keys(a), ...Object.keys(b)])
      for (const k of [...keys].sort()) walk(a[k], b[k], path ? `${path}.${k}` : k)
      return
    }
    if (Array.isArray(a) && Array.isArray(b)) {
      if (JSON.stringify(a) !== JSON.stringify(b)) changed.push(path || '(root)')
      return
    }
    if (!Object.is(a, b)) changed.push(path || '(root)')
  }
  walk(before, after, '')
  return changed
}
