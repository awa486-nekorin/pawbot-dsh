/**
 * lib/credential.js —— SESSDATA 憑證多來源解析（純函數，可離線自測）。
 * ============================================================================
 * 優先序：process.env[cookieEnv] > ROOT/credential.json > config.sessdata。
 * 硬規則：sessdata 的值只在「內部拼 Cookie」這一步用，絕不進日誌 / 報告 /
 *         錯誤訊息 / 工具回傳。本檔只回傳值本身，防洩漏由呼叫方負責。
 * ============================================================================
 */

/**
 * 解析 credential.json 的「字串內容」，取出 { sessdata, updated_at }。
 * @param {string} text
 * @returns {{sessdata: string, updated_at: string}|null}
 */
export function parseCredentialFile(text) {
  let obj
  try {
    obj = JSON.parse(String(text ?? ''))
  } catch {
    return null
  }
  if (!obj || typeof obj !== 'object') return null
  const sessdata = String(obj.sessdata ?? '').trim()
  if (!sessdata) return null
  return {
    sessdata,
    updated_at: obj.updated_at != null ? String(obj.updated_at) : '',
  }
}

/**
 * 依優先序解析 SESSDATA：env > file > config。來源都空時回 source=null、has=false。
 * @param {object} [options]
 * @param {Record<string,string|undefined>} [options.env] 環境變數（自測注入，預設 process.env）
 * @param {string} [options.cookieEnv]
 * @param {{sessdata?: string}|null} [options.fileCredential] parseCredentialFile 的結果
 * @param {string} [options.configSessdata] 設置頁欄位 sessdata
 * @returns {{sessdata: string, source: 'env'|'file'|'config'|null, has: boolean}}
 */
export function resolveCredential({
  env = process.env,
  cookieEnv = 'BILI_SESSDATA',
  fileCredential = null,
  configSessdata = '',
} = {}) {
  const envName = String(cookieEnv || 'BILI_SESSDATA')
  const fromEnv = String((env && env[envName]) ?? '').trim()
  if (fromEnv) return { sessdata: fromEnv, source: 'env', has: true }

  const fileObj = fileCredential && typeof fileCredential === 'object' ? fileCredential : null
  const fromFile = String(fileObj?.sessdata ?? '').trim()
  if (fromFile) return { sessdata: fromFile, source: 'file', has: true }

  const fromConfig = String(configSessdata ?? '').trim()
  if (fromConfig) return { sessdata: fromConfig, source: 'config', has: true }

  return { sessdata: '', source: null, has: false }
}

/**
 * 憑證狀態（給工具回傳用）：只回 has 與 source，**絕不回 sessdata 值**。
 * 這是「憑證不洩漏」的硬閘：任何要對外回憑證狀態的地方都走這裡。
 * @param {{has?: boolean, source?: string|null}} cred
 * @returns {{has: boolean, source: string|null}}
 */
export function credentialStatus(cred) {
  return {
    has: Boolean(cred?.has),
    source: cred?.source ?? null,
  }
}
