/**
 * lib/webui-proxy.js —— /pawbot-ui/* 同源代理路由（Host 側）。
 * ============================================================================
 * 這是「瀏覽器的唯一入口」：DSH Web GUI 同源打 /pawbot-ui/*，這裡再去打 PawBot 的
 * `http://<webuiHost>:<webuiPort>/api/...`。同源 → 沒有 CORS；PawBot 的 6185 網頁
 * 只當內部通道保留，不對使用者暴露。
 *
 * 全部動作拆成純函數 `actions.*`（輸入是已解析的參數，輸出是 { status, payload }），
 * 方便離線單測（test/offline.mjs 用假 pawbotFetch 直接測），HTTP 層 `dispatch` 只做
 * 解析 req/res 後轉發給 actions。
 * ============================================================================
 */
import { MASK, maskSecrets, pickUpdatable, PERSONA_MAX_BYTES } from './config-utils.js'

const JSON_HEADERS = {
  'Content-Type': 'application/json; charset=utf-8',
  'Cache-Control': 'no-store',
}

/** 讀 body 的整體逾時（毫秒）；避免半開連線讓 promise 永遠 pending。 */
const BODY_READ_TIMEOUT_MS = 15000

const CROSS_ORIGIN_DENIED = { ok: false, error: 'cross-origin denied' }

/** 讀單一 header（Node 可能給 string[]）。 */
function headerValue(req, name) {
  const headers = req && req.headers
  if (!headers) return ''
  const value = headers[name]
  if (Array.isArray(value)) return value.length ? String(value[0]) : ''
  return value == null ? '' : String(value)
}

/** header 裡的值收斂成 origin（`new URL(x).origin`）；不是合法 URL 回 null。 */
function toOrigin(value) {
  if (!value) return null
  try {
    return new URL(String(value)).origin
  } catch {
    return null
  }
}

/**
 * PawBot 的 /api/file 只允許讀寫 `data/` 底下的相對路徑（見 PawBot webui.py 的
 * `_safe_path`：base = `<base_dir>/data`）。所以人設檔「專案根的 data/persona.md」
 * 在 API 裡的路徑是 `persona.md`，**不是** `data/persona.md`（後者會變成
 * `data/data/persona.md` 而 404 / 寫錯地方）。
 */
export const PERSONA_API_PATH = 'persona.md'

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload)
  try {
    res.writeHead(status, JSON_HEADERS)
    res.end(body)
  } catch {
    /* 連線已斷，無可奈何 */
  }
}

/**
 * 拆 PawBot API 的 `{ ok, data }` 信封；沒有 data 鍵就當成已經是 payload。
 * 注意不能用 `data?.data ?? data`：PawBot 回 `{ok:true,data:null}` 時會誤把信封當設定。
 */
function unwrapPayload(data) {
  if (data && typeof data === 'object' && !Array.isArray(data) &&
      Object.prototype.hasOwnProperty.call(data, 'data')) {
    return data.data
  }
  return data
}

function readBody(req, maxBytes, timeoutMs = BODY_READ_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    let settled = false
    let timer = null

    const cleanup = () => {
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
      req.removeListener('data', onData)
      req.removeListener('end', onEnd)
      req.removeListener('error', onError)
      req.removeListener('close', onClose)
      req.removeListener('aborted', onAborted)
    }
    const settle = (fn, value) => {
      if (settled) return
      settled = true
      cleanup()
      fn(value)
    }
    const fail = (code, message) => {
      const err = new Error(message)
      err.code = code
      settle(reject, err)
    }

    function onData(chunk) {
      if (settled) return
      size += chunk.length
      if (size > maxBytes) {
        // 不要 req.destroy()：那會讓客戶端只看到 ECONNRESET，看不到 413。
        // 先把剩下的資料排掉，dispatch 才能把 413 JSON 寫回去。
        try { req.resume() } catch {}
        fail('BODY_TOO_LARGE', 'body too large')
        return
      }
      chunks.push(chunk)
    }
    function onEnd() {
      settle(resolve, Buffer.concat(chunks).toString('utf8'))
    }
    function onError(err) {
      fail('BODY_READ_ERROR', `读取 body 失败：${err?.message || err}`)
    }
    function onClose() {
      // 只有「還沒收完就關掉」才算 aborted（正常 end 之後也會有 close）。
      if (!req.complete) fail('BODY_ABORTED', '连接在读 body 前中断')
    }
    function onAborted() {
      fail('BODY_ABORTED', '請求已中止')
    }

    req.on('data', onData)
    req.on('end', onEnd)
    req.on('error', onError)
    req.on('close', onClose)
    req.on('aborted', onAborted)

    timer = setTimeout(() => {
      fail('BODY_TIMEOUT', `讀取 body 超過 ${Math.round(timeoutMs / 1000)} 秒`)
    }, timeoutMs)
    timer.unref?.()
  })
}

/** 讀並解析 JSON body；回 { ok, value?, error?, status? } */
async function readJson(req, maxBytes) {
  let text
  try {
    text = await readBody(req, maxBytes)
  } catch (e) {
    if (e?.code === 'BODY_TOO_LARGE') {
      return { ok: false, status: 413, error: `body 过大（上限 ${maxBytes} 位元組）` }
    }
    if (e?.code === 'BODY_TIMEOUT') {
      return { ok: false, status: 408, error: '讀取 body 逾時' }
    }
    if (e?.code === 'BODY_ABORTED') {
      return { ok: false, status: 400, error: '连接在读 body 前中断' }
    }
    return { ok: false, status: 400, error: 'body 讀取失敗' }
  }
  if (!text) return { ok: true, value: {} }
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch {
    return { ok: false, status: 400, error: 'JSON 解析失敗' }
  }
}

/**
 * 建出 /pawbot-ui/* 的路由處理器。
 *
 * deps：
 *   pawbotFetch(method, apiPath, body?, timeoutMs?) -> { ok, http_status, data, error }
 *   processInfo()  -> { running, pid, uptime_sec, spawn_count, last_exit, last_spawn_error, last_error }
 *   restart()      -> { ok, restarted_from_running, pid, spawn_count, error? }
 *   log(msg)       -> void（可選）
 *   getOrigin()    -> string | null（可選；DSH webServer.getOrigin()，用來擋跨來源請求）
 *   origin         -> string（可選；getOrigin 不可用時的靜態備援）
 *   personaPath    -> string（可選，預設 PERSONA_API_PATH）
 *   personaMaxBytes-> number（可選，預設 PERSONA_MAX_BYTES）
 *
 * 回傳 { dispatch, actions }；dispatch 是 (req, res) => Promise<void>。
 */
export function createPawbotProxy(deps) {
  const {
    pawbotFetch,
    processInfo,
    restart,
    log = () => {},
    getOrigin,
    origin,
    personaPath = PERSONA_API_PATH,
    personaMaxBytes = PERSONA_MAX_BYTES,
  } = deps

  /** 目前 DSH WebServer 實際綁定的 origin；拿不到回 null（呼叫端會 fail-closed）。 */
  const resolveOrigin = () => {
    try {
      const raw = typeof getOrigin === 'function' ? getOrigin() : (getOrigin ?? origin)
      return toOrigin(raw)
    } catch {
      return null
    }
  }

  let warnedNoOrigin = false

  /**
   * 同源 / CSRF 守衛。任何不符都回 403 JSON，且不呼叫 actions。
   *
   * 規則：
   *   1. Sec-Fetch-Site 存在且不是 same-origin / none → 403。
   *   2. Origin 與 DSH webServer 的 origin 不同 → 403；GET 另外檢查 Referer 的 origin。
   *   3. 非 GET 必須 content-type: application/json 開頭 → 否則 415。
   *   4. 拿不到 DSH origin 時 fail-closed：拒絕所有非 GET（並只記一次日誌）。
   *
   * 這裡刻意不「因為 Origin header 不存在就拒絕」：同源 GET 與用 curl / 腳本
   * 打本機代理都不帶 Origin。真正擋 CSRF 的是 Sec-Fetch-Site、Origin/Referer 比對，
   * 以及「非 GET 必須是 application/json」（HTML form 做不到）。
   */
  function guardRequest(req, method) {
    const expected = resolveOrigin()
    const secFetchSite = headerValue(req, 'sec-fetch-site').trim().toLowerCase()
    const originHeader = headerValue(req, 'origin')
    const refererHeader = headerValue(req, 'referer')
    const requestOrigin = toOrigin(originHeader)
    const refererOrigin = toOrigin(refererHeader)

    if (secFetchSite && secFetchSite !== 'same-origin' && secFetchSite !== 'none') {
      return { ok: false, status: 403, reason: `Sec-Fetch-Site=${secFetchSite}` }
    }

    if (!expected) {
      if (method !== 'GET') {
        if (!warnedNoOrigin) {
          warnedNoOrigin = true
          log('無法取得 DSH WebServer origin（webServer.getOrigin 不可用）——/pawbot-ui 的非 GET 請求一律拒絕（fail-closed）')
        }
        return { ok: false, status: 403, reason: 'Host origin 未知（fail-closed）' }
      }
    } else {
      if (originHeader && requestOrigin !== expected) {
        return { ok: false, status: 403, reason: `Origin ${originHeader} ≠ ${expected}` }
      }
      if (method === 'GET' && refererHeader && refererOrigin !== expected) {
        return { ok: false, status: 403, reason: `Referer ${refererHeader} 不是同源` }
      }
    }

    if (method !== 'GET') {
      const contentType = headerValue(req, 'content-type').trim().toLowerCase()
      if (!contentType.startsWith('application/json')) {
        return { ok: false, status: 415, error: 'content-type 必须是 application/json' }
      }
    }

    return { ok: true }
  }

  const actions = {
    /** GET /pawbot-ui/status：進程資訊 + PawBot /api/status（可達才併入） */
    async status() {
      const proc = processInfo()
      const http = await pawbotFetch('GET', '/api/status')
      const pawbot = http.ok && http.data?.ok ? (http.data.data ?? null) : null
      return {
        status: 200,
        payload: {
          ok: true,
          running: proc.running,
          pid: proc.pid,
          uptime_sec: proc.uptime_sec,
          spawn_count: proc.spawn_count,
          last_exit: proc.last_exit,
          last_spawn_error: proc.last_spawn_error,
          last_error: proc.last_error,
          reason: proc.running
            ? null
            : (proc.last_spawn_error || proc.last_error || 'PawBot 進程未運行'),
          webui_reachable: !!(http.ok && http.data?.ok),
          // ↓ 給 client 的契約欄位（兩種命名都給，避免兩邊對不上）
          http_ok: !!(http.ok && http.data?.ok),
          process: {
            running: proc.running,
            pid: proc.pid,
            uptime_sec: proc.uptime_sec,
            spawn_count: proc.spawn_count,
            last_exit: proc.last_exit,
            last_spawn_error: proc.last_spawn_error,
            last_error: proc.last_error,
            project_dir: proc.project_dir,
            python_exe: proc.python_exe,
          },
          pawbot, // PawBot /api/status 的 data（可達才有；否則 null）
        },
      }
    },

    /** GET /pawbot-ui/config：整份 config，secrets 遮罩 */
    async getConfig() {
      const http = await pawbotFetch('GET', '/api/config')
      if (!http.ok) return { status: 502, payload: { ok: false, error: http.error } }
      if (http.data && typeof http.data === 'object' && http.data.ok === false) {
        return {
          status: 502,
          payload: {
            ok: false,
            error: (typeof http.data.error === 'string' && http.data.error) || 'PawBot 回報讀取設定失敗',
          },
        }
      }
      const full = unwrapPayload(http.data)
      // 非 object（null / 陣列 / 字串）都是 PawBot 回應異常：不要假裝成功回 {config:{}}。
      if (full === null || typeof full !== 'object' || Array.isArray(full)) {
        return { status: 502, payload: { ok: false, error: 'PawBot 响应不是 config 对象' } }
      }
      const masked = maskSecrets(full)
      return { status: 200, payload: { ok: true, config: masked.value, masked_keys: masked.maskedKeys } }
    },

    /** POST /pawbot-ui/config：partial config（PawBot 深合併）；先丟掉遮罩值 */
    async setConfig(patch) {
      const cleaned = pickUpdatable(patch && typeof patch === 'object' && !Array.isArray(patch) ? patch : {})
      const http = await pawbotFetch('POST', '/api/config', cleaned.value)
      if (!http.ok) {
        return { status: 502, payload: { ok: false, error: http.error, dropped_keys: cleaned.dropped } }
      }
      const full = unwrapPayload(http.data)
      if (full === null || typeof full !== 'object' || Array.isArray(full)) {
        return { status: 502, payload: { ok: false, error: 'PawBot 响应不是 config 对象', dropped_keys: cleaned.dropped } }
      }
      const masked = maskSecrets(full)
      return {
        status: 200,
        payload: { ok: true, config: masked.value, masked_keys: masked.maskedKeys, dropped_keys: cleaned.dropped },
      }
    },

    /** GET /pawbot-ui/persona：讀人設文字 */
    async getPersona() {
      const http = await pawbotFetch('GET', `/api/file?path=${encodeURIComponent(personaPath)}`)
      if (!http.ok) return { status: 502, payload: { ok: false, error: http.error } }
      const d = http.data?.data ?? http.data ?? {}
      // 同時給 text 與 content：兩個子代理約定不同名，這裡一次滿足，避免面板讀不到。
      const text = d.content ?? d.text ?? ''
      return { status: 200, payload: { ok: true, text, content: text, path: d.path ?? personaPath } }
    },

    /** GET /pawbot-ui/context | /memory：上下文與記憶總覽（面板「工具」頁用） */
    async contextView() {
      const http = await pawbotFetch('GET', '/api/context')
      if (!http.ok) return { status: 502, payload: { ok: false, error: http.error } }
      return { status: 200, payload: { ok: true, data: http.data?.data ?? http.data ?? {} } }
    },
    async memoryView() {
      const http = await pawbotFetch('GET', '/api/memory')
      if (!http.ok) return { status: 502, payload: { ok: false, error: http.error } }
      return { status: 200, payload: { ok: true, data: http.data?.data ?? http.data ?? {} } }
    },

    /** POST /pawbot-ui/persona { text }：寫人設；超過 200KB 拒絕 */
    async setPersona(text) {
      const content = typeof text === 'string' ? text : ''
      const bytes = Buffer.byteLength(content, 'utf8')
      if (bytes > personaMaxBytes) {
        return { status: 413, payload: { ok: false, error: `人设文件过大（上限 ${personaMaxBytes} 字节）` } }
      }
      const http = await pawbotFetch('POST', '/api/file', { path: personaPath, content })
      if (!http.ok) return { status: 502, payload: { ok: false, error: http.error } }
      return { status: 200, payload: { ok: true, path: personaPath, bytes } }
    },

    /** POST /pawbot-ui/reload-plugins：热重载全部插件 */
    async reloadPlugins() {
      const http = await pawbotFetch('POST', '/api/plugins/reload', {})
      if (!http.ok) return { status: 502, payload: { ok: false, error: http.error } }
      return { status: 200, payload: { ok: true, result: http.data?.data ?? http.data ?? {} } }
    },

    /** POST /pawbot-ui/send { target, message, is_group }：以機器人帳號真發消息 */
    async send(body) {
      const target = String(body?.target ?? '').trim()
      const message = String(body?.message ?? '')
      const is_group = body?.is_group !== false // 與 PawBot 預設一致（缺省為群）
      if (!target) return { status: 400, payload: { ok: false, error: '目标不能为空' } }
      const http = await pawbotFetch('POST', '/api/send', { target, message, is_group })
      // PawBot 在 NapCat 斷線時會回 HTTP 200 + {ok:false}；只看 HTTP status 會誤報 sent:true。
      if (!http.ok || http.data?.ok === false) {
        const detail =
          (http.data && typeof http.data.error === 'string' && http.data.error) ||
          http.error ||
          'PawBot 回報發送失敗'
        return { status: 502, payload: { ok: false, error: detail } }
      }
      return {
        status: 200,
        payload: {
          ok: true,
          sent: true,
          note: '已真的以机器人账号送出此消息（不是模拟）',
          target,
          is_group,
          result: http.data ?? {},
        },
      }
    },

    /** POST /pawbot-ui/restart：用本插件的 stopChild + startChild，不繞 PawBot webui */
    async restartAction() {
      const r = await restart()
      if (!r || !r.ok) {
        return { status: 500, payload: { ok: false, error: (r && r.error) || '重启失败' } }
      }
      return { status: 200, payload: r }
    },
  }

  async function dispatch(req, res) {
    let sub, method
    try {
      const url = new URL(req.url || '/', 'http://127.0.0.1')
      // 不同匹配方式下 req.url 可能是 '/pawbot-ui/status'、'status'、'/status' 三種，
      // 一律收斂成不含前導斜線的 'status'。
      sub = url.pathname
        .replace(/^\/pawbot-ui\/?/, '')
        .replace(/^\/+/, '')
        .replace(/\/+$/, '')
      method = (req.method || 'GET').toUpperCase()
    } catch {
      return sendJson(res, 400, { ok: false, error: '无法解析请求路径' })
    }
    const emit = (r) => sendJson(res, r.status, r.payload)
    const guard = guardRequest(req, method)
    if (!guard.ok) {
      log(`拒絕 /pawbot-ui/${sub || ''} ${method}：${guard.reason || guard.error}`)
      return sendJson(res, guard.status, guard.error
        ? { ok: false, error: guard.error }
        : { ...CROSS_ORIGIN_DENIED })
    }
    try {
      if (method === 'GET' && sub === 'status') return emit(await actions.status())
      if (method === 'GET' && sub === 'config') return emit(await actions.getConfig())
      if (method === 'GET' && sub === 'persona') return emit(await actions.getPersona())

      if (method === 'POST' && sub === 'config') {
        const r = await readJson(req, 1024 * 1024)
        if (!r.ok) return sendJson(res, r.status || 400, { ok: false, error: r.error })
        // 兩種 body 都收：{ patch: {...} }（面板）或直接 partial config（腳本/curl）
        const body = r.value
        const payload = body && typeof body === 'object' && body.patch && typeof body.patch === 'object'
          ? body.patch
          : body
        return emit(await actions.setConfig(payload))
      }
      if (method === 'POST' && sub === 'persona') {
        const r = await readJson(req, personaMaxBytes + 64 * 1024)
        if (!r.ok) return sendJson(res, r.status || 400, { ok: false, error: r.error })
        // 純字串 / {text} / {content} 三種都收
        const text = typeof r.value === 'string'
          ? r.value
          : String(r.value?.text ?? r.value?.content ?? '')
        return emit(await actions.setPersona(text))
      }
      // 面板與腳本用了兩種路徑名，兩個都認
      if (method === 'POST' && (sub === 'reload-plugins' || sub === 'plugins/reload')) {
        return emit(await actions.reloadPlugins())
      }
      if (method === 'GET' && sub === 'context') return emit(await actions.contextView())
      if (method === 'GET' && sub === 'memory') return emit(await actions.memoryView())
      if (method === 'POST' && sub === 'send') {
        const r = await readJson(req, 256 * 1024)
        if (!r.ok) return sendJson(res, r.status || 400, { ok: false, error: r.error })
        return emit(await actions.send(r.value))
      }
      if (method === 'POST' && sub === 'restart') return emit(await actions.restartAction())

      return sendJson(res, 404, { ok: false, error: '未知的 /pawbot-ui 子路径' })
    } catch (e) {
      return sendJson(res, 500, { ok: false, error: `处理失败：${e?.message || e}` })
    }
  }

  return { dispatch, actions }
}
