/**
 * @local/pawbot —— 把 PawBot（QQ 機器人）包成 DSH 受管服務。
 * ============================================================================
 * 它只管一件事：把 PawBot 當成一個受管子進程。
 *   1. apply 時（autostart）用 spawn 起 `pythonExe run.py`，cwd = projectDir。
 *   2. stdout / stderr 收進環形緩衝區（按行、帶時間戳），可 tail。
 *   3. dispose / restart 時收掉「整棵子進程樹」，不留孤兒 python。
 *   4. 暴露 6 個工具：pawbot_status / pawbot_logs / pawbot_restart / pawbot_health /
 *      pawbot_config_get / pawbot_config_set。
 *   5. 註冊同源代理路由 /pawbot-ui/*（狀態/設定/人設/重啟都收進 DSH 網頁，不再開 6185）。
 * 它不讀寫 PawBot 的 data/、不改任何 py；PawBot 自己寫自己的 data 是它的事。
 * ============================================================================
 */
import { loadHostDeps } from './lib/hostdeps.js'

const hostDeps = await loadHostDeps()
const z = hostDeps.z
const defineTool = hostDeps.defineTool

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { createPawbotProxy } from './lib/webui-proxy.js'
import { maskSecrets, pickUpdatable, pickSection, diffKeys } from './lib/config-utils.js'

export const name = 'pawbot'
export const inject = ['tools', 'webServer']

const isWin = process.platform === 'win32'

/* ── Config（schemastery 拿不到就 undefined，設置頁降級、本體照常跑） ── */
export const Config = (() => {
  if (!z) return undefined
  try {
    return z.object({
      /** PawBot 專案根目錄（run.py 所在） */
      projectDir: z.string().default('D:/WorkBuddy/2026-10-20-00-14-25/pawbot').extra('volatile', true),
      /** 用哪個 python 起（`py` = Windows launcher，等同 start.bat 的 Python313） */
      pythonExe: z.string().default('py').extra('volatile', true),
      /** apply 時是否自動啟動 */
      autostart: z.boolean().default(true).extra('volatile', true),
      /** 進程意外退出後是否自動重啟（有意 stop 不會觸發） */
      restartOnCrash: z.boolean().default(false).extra('volatile', true),
      /** pawbot_status 預設回傳的日誌行數 */
      logLines: z.natural().default(200).extra('volatile', true),
      /** 健康檢查打的 WebUI host（讀自 PawBot 的 webui.host 預設值） */
      webuiHost: z.string().default('127.0.0.1').extra('volatile', true),
      /** 健康檢查打的 WebUI port（讀自 PawBot 的 webui.port 預設值） */
      webuiPort: z.natural().default(6185).extra('volatile', true),
      /** WebUI token（PawBot 預設為空；設了就走 Authorization: Bearer） */
      webuiToken: z.string().default('').role('secret').extra('volatile', true),
      /** 健康檢查 HTTP 超時（毫秒） */
      healthTimeoutMs: z.natural().default(3000).extra('volatile', true),
      /** 崩潰自動重啟最多幾次（之後停止並記 last_error） */
      maxCrashRestarts: z.natural().default(5).extra('volatile', true),
    })
  } catch {
    return undefined
  }
})()

export function apply(ctx, config) {
  const DEFAULTS = {
    projectDir: 'D:/WorkBuddy/2026-10-20-00-14-25/pawbot',
    pythonExe: 'py',
    autostart: true,
    restartOnCrash: false,
    logLines: 200,
    webuiHost: '127.0.0.1',
    webuiPort: 6185,
    webuiToken: '',
    healthTimeoutMs: 3000,
    maxCrashRestarts: 5,
  }
  const normalize = (src, base) => {
    const out = { ...base }
    for (const k of Object.keys(base)) {
      const v = src?.[k]
      if (v === undefined || v === null) continue
      if (typeof base[k] === 'number') {
        const n = Number(v)
        if (Number.isFinite(n)) out[k] = n
      } else if (typeof base[k] === 'boolean') {
        if (typeof v === 'boolean') out[k] = v
      } else if (typeof base[k] === 'string') {
        if (typeof v === 'string') out[k] = v
      }
    }
    return out
  }
  const cfg = normalize(config, DEFAULTS)
  const refreshCfg = () => Object.assign(cfg, normalize(config, DEFAULTS))

  const log = (msg) => {
    const line = `[pawbot] ${msg}`
    try {
      if (ctx.logger?.info) ctx.logger.info(line)
      else console.log(line)
    } catch {
      console.log(line)
    }
  }
  for (const n of hostDeps.notes) log(n)

  /* ── 環形緩衝區（stdout + stderr 合併，按行存放，帶時間戳） ── */
  const MAX_BUFFER = 5000
  const ring = [] // { ts, stream, line }
  const pushLine = (stream, line) => {
    ring.push({ ts: Date.now(), stream, line })
    if (ring.length > MAX_BUFFER) ring.splice(0, ring.length - MAX_BUFFER)
  }
  const tail = (n) => ring.slice(-Math.max(0, n))

  // 行分割器：處理跨 chunk 的半行（多字節中文也不怕，因為 stdout/stderr 已 setEncoding('utf8')）
  const acc = { out: '', err: '' }
  const feed = (stream, chunk) => {
    const key = stream === 'stderr' ? 'err' : 'out'
    acc[key] += String(chunk)
    let i
    while ((i = acc[key].indexOf('\n')) >= 0) {
      pushLine(stream, acc[key].slice(0, i).replace(/\r$/, ''))
      acc[key] = acc[key].slice(i + 1)
    }
  }
  const flushBuf = () => {
    for (const [stream, key] of [['stdout', 'out'], ['stderr', 'err']]) {
      if (acc[key]) {
        pushLine(stream, acc[key])
        acc[key] = ''
      }
    }
  }

  /* ── 子進程狀態 ── */
  let currentChild = null
  let disposed = false
  let stopping = false
  let restarting = false // 重啟互斥：同一時間只允許一個 restart（避免雙 spawn / 互殺）
  let crashRestarts = 0 // 連續崩潰重啟次數（手動重啟或穩定運行後歸零）
  let startedAt = 0
  let spawnCount = 0
  let lastExit = null // { code, signal, at }
  let lastError = '' // 最後一次 stderr 非空行或 spawn error
  let lastSpawnError = '' // 最近一次 spawn 失敗訊息
  // /pawbot-ui/* 同源代理路由的註冊結果（'pending' | 'registered' | 'unavailable' | 'error: …'）
  // UI 打不開時 pawbot_status 會回這個，先看它再猜。
  let uiRouteState = 'pending'
  const timers = new Set()

  const isRunning = (c) => !!(c && c.exitCode === null && c.signalCode === null)

  /* ── Windows 整棵進程樹收口 ──
   * 為什麼必須 taskkill /T /F：
   *   - `py run.py` 實際是 py.exe（launcher）→ python.exe 兩層；直接 child.kill() 只殺 py.exe，
   *     會把 python.exe 留下變孤兒。
   *   - Windows 上 Node 的 child.kill('SIGTERM'/'SIGINT') 都只是 TerminateProcess，沒有 POSIX 信號語義；
   *     run.py 的 SIGINT/SIGTERM handler 在 Windows 上也收不到（add_signal_handler 會 NotImplementedError）。
   *   - 所以「優雅停止」在 Windows 上不可得，只能用 taskkill /T /F 原子收掉整棵樹（含 PawBot 可能
   *     拉起的 TTS 等孫進程），保證不留孤兒 python。
   */
  const taskkillTree = (pid) =>
    new Promise((resolve) => {
      if (!isWin || !pid) return resolve(false)
      try {
        const k = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
        let done = false
        const finish = (v) => {
          if (!done) {
            done = true
            resolve(v)
          }
        }
        k.once('exit', (code) => finish(code === 0))
        k.once('error', () => finish(false))
        const guard = setTimeout(() => finish(false), 5000)
        guard.unref?.()
      } catch {
        resolve(false)
      }
    })

  const waitForExit = (c, ms) =>
    new Promise((resolve) => {
      if (!isRunning(c)) return resolve(true)
      const timer = setTimeout(() => {
        c.removeListener('exit', onExit)
        resolve(false)
      }, ms)
      const onExit = () => {
        clearTimeout(timer)
        resolve(true)
      }
      c.once('exit', onExit)
    })

  /**
   * 停掉 currentChild（整棵進程樹）。
   * 回傳 { stopped, alive?, pid?, error? }：
   *   - stopped:true          確認已退出
   *   - stopped:false,alive:true  SIGKILL 後仍活著（殺不掉）——呼叫端不能假裝成功
   *   - stopped:false,alive:false 本來就沒在跑
   */
  const stopChild = async () => {
    const child = currentChild
    if (!child) return { stopped: false, alive: false }
    const pid = child.pid
    if (!isRunning(child)) {
      if (currentChild === child) currentChild = null
      return { stopped: false, alive: false }
    }
    stopping = true
    try {
      if (isWin) {
        await taskkillTree(pid)
      } else {
        // POSIX：SIGTERM 讓 run.py 的 signal handler 優雅關閉（flush 存儲、關 WS/WebUI）。
        try {
          child.kill('SIGTERM')
        } catch {}
      }
      let exited = await waitForExit(child, 1500)
      if (!exited) {
        try {
          child.kill('SIGKILL')
        } catch {}
        exited = await waitForExit(child, 1000)
      }
      if (!exited || isRunning(child)) {
        // 1500ms 等不到退出、SIGKILL 也沒效：坦白回報，不要回 stopped:true。
        return { stopped: false, alive: true, pid, error: `PawBot 进程（pid=${pid}）杀不掉，仍存活` }
      }
      if (currentChild === child) currentChild = null
      return { stopped: true, pid }
    } finally {
      stopping = false
    }
  }

  const startChild = () => {
    // 禁止雙 spawn：已經有一個活著的子進程就直接回它。
    if (isRunning(currentChild)) {
      log(`startChild 略过：已有运行中的 PawBot（pid=${currentChild.pid}）`)
      return currentChild
    }
    refreshCfg()
    const cwd = resolve(String(cfg.projectDir || DEFAULTS.projectDir))
    const exe = String(cfg.pythonExe || DEFAULTS.pythonExe)
    const runPy = join(cwd, 'run.py')
    if (!existsSync(runPy)) {
      const msg = `run.py 不存在：${runPy}`
      lastSpawnError = msg
      lastError = msg
      log(msg)
      return null
    }
    let child
    try {
      child = spawn(exe, ['run.py'], {
        cwd,
        env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (e) {
      const msg = `spawn 抛错：${e?.message}`
      lastSpawnError = msg
      lastError = msg
      log(msg)
      return null
    }
    spawnCount += 1
    startedAt = Date.now()
    currentChild = child
    lastSpawnError = ''
    log(`spawn ${exe} run.py (cwd=${cwd}, pid=${child.pid})`)

    try {
      child.stdout?.setEncoding('utf8')
      child.stderr?.setEncoding('utf8')
    } catch {}
    child.stdout?.on('data', (d) => feed('stdout', d))
    child.stderr?.on('data', (d) => feed('stderr', d))
    child.on('error', (err) => {
      const msg = `进程错误：${err?.message}`
      lastError = msg
      lastSpawnError = msg
      pushLine('stderr', msg)
      log(msg)
    })
    child.on('exit', (code, signal) => {
      // 舊 child 晚到的 exit 不能清掉新 child 的追蹤：只有「目前這一個」才處理。
      if (currentChild !== child) {
        log(`忽略旧子进程（pid=${child.pid}）晚到的 exit（currentChild=${currentChild?.pid ?? 'null'}）`)
        return
      }
      flushBuf()
      lastExit = { code, signal, at: Date.now() }
      pushLine('stderr', `[pawbot] process exited code=${code} signal=${signal}`)
      log(`进程退出 code=${code} signal=${signal}`)
      currentChild = null
      // 非有意停止 && 配置了崩潰重啟 && 未 dispose → 指數退避後重啟（2s→4s→8s…上限 60s）
      if (!disposed && !stopping && cfg.restartOnCrash) {
        // 穩定跑過一段時間才崩，視為新的連續崩潰序列（不要累積到幾個月後才停）。
        if (startedAt && Date.now() - startedAt >= 120000) crashRestarts = 0
        const maxRestarts = Math.max(1, Number(cfg.maxCrashRestarts) || DEFAULTS.maxCrashRestarts)
        if (crashRestarts >= maxRestarts) {
          const msg = `崩溃重启已达上限（${maxRestarts} 次），停止自动重启；请看日志检查 PawBot 为何一直退出`
          lastError = msg
          pushLine('stderr', `[pawbot] ${msg}`)
          log(msg)
          return
        }
        crashRestarts += 1
        const delay = Math.min(60000, 2000 * 2 ** (crashRestarts - 1))
        log(`崩溃后 ${Math.round(delay / 1000)} 秒重启（第 ${crashRestarts}/${maxRestarts} 次）`)
        const t = setTimeout(() => {
          timers.delete(t)
          if (!disposed && !stopping) startChild()
        }, delay)
        t.unref?.()
        timers.add(t)
      }
    })
    return child
  }

  /**
   * 重啟（互斥）：清掉 restartOnCrash 排的 timer → 停掉舊進程樹 → 再 spawn 一個。
   * 重入直接回 {ok:false,error:'restart 進行中'}，不會雙 spawn 或互殺。
   */
  const restartChild = async () => {
    if (restarting) return { ok: false, error: 'restart 进行中' }
    restarting = true
    try {
      refreshCfg()
      const wasRunning = isRunning(currentChild)
      for (const t of timers) clearTimeout(t)
      timers.clear()
      crashRestarts = 0
      const stop = await stopChild()
      if (stop && stop.stopped === false && stop.alive) {
        return {
          ok: false,
          error: stop.error || '旧 PawBot 进程杀不掉，已放弃重启',
          alive: true,
          pid: stop.pid,
        }
      }
      const started = startChild()
      if (!started) return { ok: false, error: lastSpawnError || 'spawn 失败' }
      await new Promise((r) => setTimeout(r, 500))
      return { ok: true, restarted_from_running: wasRunning, pid: started.pid, spawn_count: spawnCount }
    } finally {
      restarting = false
    }
  }

  /* ── DSH 工具 ── */
  const safe = (v) => {
    if (v === undefined || v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return v
    try {
      return JSON.parse(JSON.stringify(v))
    } catch {
      return String(v)
    }
  }
  const textOutput = {
    schema: { type: 'object', properties: {}, additionalProperties: true },
    render: (_args, value) => [
      { type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) },
    ],
  }
  const tool = (spec) =>
    ctx.tools.register(
      defineTool({
        ...spec,
        output: textOutput,
        async execute(args, exec) {
          return safe(await spec.execute(args, exec))
        },
      }),
    )

  const tailMap = (n) => tail(n).map((l) => ({ stream: l.stream, line: l.line }))

  tool({
    name: 'pawbot_status',
    description:
      '查 PawBot（QQ 机器人）这个 DSH 受管服务的运行状态：是否在跑、PID、已运行秒数、spawn 次数、' +
      '最近一次退出原因、最后错误、日志尾部。只读状态，不改变任何东西。',
    parameters: {},
    async execute() {
      refreshCfg()
      const c = currentChild
      const running = isRunning(c)
      const n = Math.max(1, Math.min(MAX_BUFFER, Number(cfg.logLines) || DEFAULTS.logLines))
      return {
        ok: true,
        running,
        pid: running && c ? c.pid : null,
        uptime_sec: running && startedAt ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000)) : 0,
        spawn_count: spawnCount,
        last_exit: lastExit,
        last_spawn_error: lastSpawnError || null,
        last_error: lastError || null,
        project_dir: resolve(String(cfg.projectDir || DEFAULTS.projectDir)),
        python_exe: String(cfg.pythonExe || DEFAULTS.pythonExe),
        // 同源代理路由的註冊結果——UI 打不開時先看這個，不要瞎猜
        ui_route: uiRouteState,
        // webServer 實際綁在哪個 origin（面板就是從這個 origin 的同源路徑打的）
        web_origin: (() => {
          try {
            return typeof ctx.webServer?.getOrigin === 'function' ? ctx.webServer.getOrigin() : null
          } catch (e) {
            return `error: ${e?.message || e}`
          }
        })(),
        log_tail: tailMap(n),
      }
    },
  })

  tool({
    name: 'pawbot_logs',
    description:
      '回传 PawBot 子进程 stdout/stderr 的日志「尾部」（不是全文）：预设最近 80 行，最多 500 行，' +
      '按时间顺序（旧在前）。每行带 stream（stdout/stderr）与 line。',
    parameters: {
      lines: { type: 'integer', description: '要回传的行数，预设 80，最多 500。' },
    },
    async execute(args) {
      let n = Number(args?.lines)
      if (!Number.isFinite(n)) n = 80
      n = Math.max(1, Math.min(500, Math.floor(n)))
      const logs = tailMap(n)
      return { ok: true, lines: logs.length, logs }
    },
  })

  tool({
    name: 'pawbot_restart',
    description:
      '重启 PawBot 子进程。**副作用（请先想清楚再调用）**：会先杀掉整棵进程树再重新 spawn——' +
      '正在进行/正在回的话会丢失、与 NapCat 的连接会断开（NapCat 通常会自动重连）、服务会短暂中断。' +
      '重启后回传 spawn 结果（pid）与日志尾部。',
    parameters: {},
    async execute() {
      const r = await restartChild()
      return { ...r, log_tail: tailMap(40) }
    },
  })

  const httpStatus = async () => {
    const host = String(cfg.webuiHost || DEFAULTS.webuiHost)
    const port = Number(cfg.webuiPort) || DEFAULTS.webuiPort
    const token = String(cfg.webuiToken || '')
    const timeout = Math.max(200, Number(cfg.healthTimeoutMs) || DEFAULTS.healthTimeoutMs)
    const url = `http://${host}:${port}/api/status`
    const headers = {}
    if (token) headers.Authorization = `Bearer ${token}`
    try {
      const res = await fetch(url, { headers, signal: AbortSignal.timeout(timeout) })
      if (!res.ok) return { ok: false, http_status: res.status }
      const json = await res.json()
      return { ok: true, http_status: res.status, data: json }
    } catch (e) {
      return { ok: false, error: `${e?.name || 'Error'}: ${e?.message || e}` }
    }
  }

  tool({
    name: 'pawbot_health',
    description:
      '检查 PawBot 健康：真的去打 PawBot WebUI 的 GET /api/status（预设 http://127.0.0.1:6185，' +
      '可由 Config 改），拿回 uptime/连接/统计等；打不到（进程没起或 port 不同）就只回进程层状态，' +
      '不会自己造 API。',
    parameters: {},
    async execute() {
      refreshCfg()
      const c = currentChild
      const running = isRunning(c)
      const http = await httpStatus()
      return {
        ok: true,
        process_running: running,
        pid: running && c ? c.pid : null,
        http,
      }
    },
  })

  /* ── 同源代理 + config 工具共用的 HTTP 通道 ──
   * 一律走 PawBot 自己的 HTTP API，不直接讀寫 data/config.json（避免跟它搶寫檔）。 */
  const pawbotFetch = async (method, apiPath, body, timeoutMs = 5000) => {
    refreshCfg()
    const host = String(cfg.webuiHost || DEFAULTS.webuiHost)
    const port = Number(cfg.webuiPort) || DEFAULTS.webuiPort
    const token = String(cfg.webuiToken || '')
    const url = `http://${host}:${port}${apiPath}`
    const headers = {}
    if (token) headers.Authorization = `Bearer ${token}`
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    let res
    try {
      res = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(Math.max(200, Number(timeoutMs) || 5000)),
      })
    } catch (e) {
      const name = e?.name || 'Error'
      return { ok: false, http_status: null, data: null, error: `PawBot 连接失败（${name}：${e?.message || e}）` }
    }
    let json
    try {
      json = await res.json()
    } catch {
      // 不要吞成 data:null 還回 ok:true——呼叫端會把「PawBot 回垃圾」當成成功。
      return { ok: false, http_status: res.status, data: null, error: 'PawBot 响应不是 JSON' }
    }
    return {
      ok: res.ok,
      http_status: res.status,
      data: json,
      error: res.ok ? undefined : (json && typeof json.error === 'string' ? json.error : `HTTP ${res.status}`),
    }
  }

  const processInfo = () => {
    refreshCfg()
    const c = currentChild
    const running = isRunning(c)
    return {
      running,
      pid: running && c ? c.pid : null,
      uptime_sec: running && startedAt ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000)) : 0,
      spawn_count: spawnCount,
      last_exit: lastExit,
      last_spawn_error: lastSpawnError || null,
      last_error: lastError || null,
      // 面板「專案目錄 / Python」那兩列需要這兩個欄位
      project_dir: resolve(String(cfg.projectDir || DEFAULTS.projectDir)),
      python_exe: String(cfg.pythonExe || DEFAULTS.pythonExe),
    }
  }

  /* ── Agent 工具：讓模型也能讀/改 config（遮罩 secrets、只回鍵名差異） ── */
  tool({
    name: 'pawbot_config_get',
    description:
      '读 PawBot 的完整 config.json（或某个 section）。回传一律遮罩 secrets（api_key / token / access_token / cloud_key / *secret* 等会变成 "***已设定***"），' +
      '除非给 reveal:true 才回真值。section 可选（只回该区段，如 "llm" / "tools"）。' +
      '⚠ reveal:true 会在上下文里暴露真实 key，除非用户明确要看，不要用。' +
      '⚠ 这是 data/config.json 的同一份设置；部分改动要重启 PawBot 才完全生效。',
    parameters: {
      section: { type: 'string', description: '可选：只回这个区段（如 "llm"、"tools"）；不给回全部。' },
      reveal: { type: 'boolean', description: '预设 false。true 才回 secrets 真值——会在上下文里暴露 key，除非用户明确要看，不要用。' },
    },
    async execute(args) {
      refreshCfg()
      const http = await pawbotFetch('GET', '/api/config')
      if (!http.ok) return { ok: false, error: http.error }
      const full = http.data?.data ?? http.data ?? {}
      const section = args?.section == null ? '' : String(args.section)
      if (section && !(section in full)) return { ok: false, error: `找不到区段「${section}」` }
      const picked = pickSection(full, section)
      if (args?.reveal === true) {
        return { ok: true, reveal: true, config: picked, note: '已回传真值（含 secrets）——请谨慎处理，勿泄漏到对外输出。' }
      }
      const masked = maskSecrets(picked)
      return { ok: true, config: masked.value, masked_keys: masked.maskedKeys }
    },
  })

  tool({
    name: 'pawbot_config_set',
    description:
      '改 PawBot 的 config.json（直接写文件、当下生效；部分设置要重启 PawBot 才完全生效）。' +
      'patch 是要改的 partial config（object，必填，依区段深合并）。回传改动前后的差异摘要（只列 dotted 键名，不列值）。' +
      '遮罩值 "***已设定***" 会被丢掉，不会把遮罩当真值写进去。reason 只进日志、不回传。',
    parameters: {
      patch: {
        type: 'object',
        // 宿主會驗 schema：object 型參數必須顯式宣告 additionalProperties（動態鍵正是這裡的用途）
        additionalProperties: true,
        required: true,
        description: '要改的 partial config（object，必填）。遮罩值会被忽略。',
      },
      reason: { type: 'string', description: '可选：为什么改（只进日志，不回传）。' },
    },
    async execute(args) {
      refreshCfg()
      const patch = args?.patch
      if (!patch || typeof patch !== 'object' || Array.isArray(patch)) {
        return { ok: false, error: 'patch 必须是 object' }
      }
      if (args?.reason) log(`pawbot_config_set 原因：${String(args.reason).slice(0, 500)}`)
      const beforeRes = await pawbotFetch('GET', '/api/config')
      if (!beforeRes.ok) return { ok: false, error: `读取旧 config 失败：${beforeRes.error}` }
      const before = beforeRes.data?.data ?? beforeRes.data ?? {}
      const cleaned = pickUpdatable(patch)
      const afterRes = await pawbotFetch('POST', '/api/config', cleaned.value)
      if (!afterRes.ok) {
        return { ok: false, error: `写入 config 失败：${afterRes.error}`, dropped_keys: cleaned.dropped }
      }
      const after = afterRes.data?.data ?? afterRes.data ?? {}
      const changed = diffKeys(before, after)
      return {
        ok: true,
        changed_keys: changed,
        dropped_keys: cleaned.dropped,
        note: '已直接改写 data/config.json；部分设置要重启 PawBot 才完全生效。',
      }
    },
  })

  /* ── 同源代理路由：/pawbot-ui/*（瀏覽器唯一入口，內部再打 PawBot WebUI） ── */
  const proxy = createPawbotProxy({
    pawbotFetch,
    processInfo,
    restart: restartChild,
    // 把 DSH WebServer 實際綁定的 origin 傳給代理：/pawbot-ui/* 用它做同源/CSRF 檢查。
    // 拿不到時代理會 fail-closed（拒絕所有非 GET）並記日誌。
    getOrigin: () => {
      try {
        return typeof ctx.webServer?.getOrigin === 'function' ? ctx.webServer.getOrigin() : null
      } catch (e) {
        log(`取得 webServer origin 失败：${e?.message || e}`)
        return null
      }
    },
    log,
  })

  ctx.effect(() => {
    if (!ctx.webServer || typeof ctx.webServer.register !== 'function') {
      uiRouteState = 'unavailable'
      log('webServer.register 不可用 —— /pawbot-ui/* 同源代理路由未注册')
      return () => {}
    }
    // ⚠️ 這裡同時註冊 prefix 與逐條 exact：
    //   實測本版 webServer 對 `prefix: '/pawbot-ui/'` 的匹配跟預期不同（請求直接 404、連 handler 都沒進），
    //   而 (kind,path) 只要不同就不算衝突，所以兩套都掛上，任何一種匹配生效都能用。
    const disposers = []
    const failures = []
    const add = (route) => {
      try {
        disposers.push(ctx.webServer.register({ ...route, handler: proxy.dispatch }))
      } catch (e) {
        failures.push(`${route.kind} ${route.path}: ${e?.message || e}`)
      }
    }
    add({ kind: 'prefix', path: '/pawbot-ui' })
    add({ kind: 'prefix', path: '/pawbot-ui/' })
    for (const p of [
      'status', 'config', 'persona', 'reload-plugins', 'plugins/reload',
      'send', 'restart', 'context', 'memory',
    ]) {
      add({ kind: 'exact', path: `/pawbot-ui/${p}` })
    }
    uiRouteState = failures.length
      ? `partial: ${disposers.length} ok / ${failures.length} failed（${failures[0]}）`
      : `registered (${disposers.length} routes)`
    log(`已注册同源代理路由：${uiRouteState}`)
    return () => {
      for (const d of disposers) {
        try { d?.() } catch {}
      }
    }
  }, 'pawbot: /pawbot-ui 同源代理路由')

  /* ── 回收：子進程 + 定時器都經 ctx.effect（dispose 時收整棵樹） ── */
  ctx.effect(
    () => () => {
      disposed = true
      stopping = true
      for (const t of timers) clearTimeout(t)
      timers.clear()
      const child = currentChild
      currentChild = null
      return (async () => {
        try {
          if (child && isRunning(child)) {
            if (isWin) {
              await taskkillTree(child.pid)
              await waitForExit(child, 1000)
              if (isRunning(child)) {
                try {
                  child.kill('SIGKILL')
                } catch {}
              }
            } else {
              try {
                child.kill('SIGTERM')
              } catch {}
              const exited = await waitForExit(child, 1200)
              if (!exited) {
                try {
                  child.kill('SIGKILL')
                } catch {}
              }
            }
          }
        } catch {
          /* dispose 時絕不拋 */
        }
      })()
    },
    'pawbot: 子进程/定时器回收',
  )

  if (cfg.autostart) {
    startChild()
  }

  log(`已就绪：projectDir=${resolve(String(cfg.projectDir || DEFAULTS.projectDir))} autostart=${cfg.autostart} restartOnCrash=${cfg.restartOnCrash}`)
}
