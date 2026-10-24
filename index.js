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
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createPawbotProxy } from './lib/webui-proxy.js'
import { maskSecrets, pickUpdatable, pickSection, diffKeys } from './lib/config-utils.js'
import {
  createBootOrchestrator,
  createSetupJob,
  defaultRun,
  ensureConfig,
  runEnvSetup,
  setupState,
  venvPaths,
} from './lib/setup.js'
import {
  NAPCAT_IMAGE,
  NAPCAT_WEBUI_PORT,
  collectNapcatLogs,
  detectNapcat,
  isLoggedIn,
  manualGuide,
  manualHint,
  napcatConfigDir,
  napcatRunning,
  qrFromLog,
  readWebui,
  startNapcat,
  writeOnebotConfig,
} from './lib/napcat.js'

export const name = 'pawbot'
export const inject = ['tools', 'webServer']

const isWin = process.platform === 'win32'

/** 插件自帶的 PawBot 源碼目錄（app/ 由安裝包提供；一鍵安裝的 venv / data 都在這裡）。 */
const DEFAULT_APP_DIR = fileURLToPath(new URL('./app', import.meta.url))

/* ── Config（schemastery 拿不到就 undefined，設置頁降級、本體照常跑） ── */
export const Config = (() => {
  if (!z) return undefined
  try {
    return z.object({
      /** PawBot 專案根目錄（run.py 所在）。Linux/macOS 上請改成實際路徑，例如 /opt/pawbot */
      projectDir: z.string().default(isWin ? 'D:/WorkBuddy/2026-10-20-00-14-25/pawbot' : '/opt/pawbot').extra('volatile', true),
      /** 用哪個 python 起（Windows 是 `py` launcher；Linux/macOS 用 `python3`） */
      pythonExe: z.string().default(isWin ? 'py' : 'python3').extra('volatile', true),
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
      /** PawBot 源碼目錄（run.py / requirements.txt / data/ 所在）。預設＝插件目錄下的 app/ */
      appDir: z.string().default(DEFAULT_APP_DIR).extra('volatile', true),
      /** NapCat docker 鏡像（沒有 docker 時用不到） */
      napcatImage: z.string().default(NAPCAT_IMAGE).extra('volatile', true),
      /** NapCat WebUI 對外埠（docker 的 -p；預設 6099） */
      napcatWebuiPort: z.natural().default(NAPCAT_WEBUI_PORT).extra('volatile', true),
      /** OneBot 反向 WS 的存取 token（空＝不驗證）。填了會同時寫進 PawBot 的 server.access_token 與 NapCat 的 token。 */
      onebotToken: z.string().default('').role('secret').extra('volatile', true),
    })
  } catch {
    return undefined
  }
})()

export function apply(ctx, config) {
  const DEFAULTS = {
    projectDir: isWin ? 'D:/WorkBuddy/2026-10-20-00-14-25/pawbot' : '/opt/pawbot',
    pythonExe: isWin ? 'py' : 'python3',
    autostart: true,
    restartOnCrash: false,
    logLines: 200,
    webuiHost: '127.0.0.1',
    webuiPort: 6185,
    webuiToken: '',
    healthTimeoutMs: 3000,
    maxCrashRestarts: 5,
    appDir: DEFAULT_APP_DIR,
    napcatImage: NAPCAT_IMAGE,
    napcatWebuiPort: NAPCAT_WEBUI_PORT,
    onebotToken: '',
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

  /* ── 一鍵安裝的專用環形緩衝區（只放安裝/啟動輸出；setup/status 只回最後 60 行） ── */
  const SETUP_LOG_MAX = 400
  const SETUP_LOG_TAIL = 60
  const setupRing = []
  const setupLog = (msg) => {
    const text = String(msg == null ? '' : msg)
    if (!text) return
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trimEnd()
      if (!line) continue
      setupRing.push(line)
      if (setupRing.length > SETUP_LOG_MAX) setupRing.splice(0, setupRing.length - SETUP_LOG_MAX)
      // 同時進主環形緩衝區，pawbot_logs / log_tail 也看得到安裝過程（加 [setup] 前綴避免跟 PawBot 日誌混淆）
      pushLine('stdout', `[setup] ${line}`)
    }
  }
  const setupLogTail = (n = SETUP_LOG_TAIL) =>
    setupRing.slice(-Math.max(1, Math.min(SETUP_LOG_MAX, Number(n) || SETUP_LOG_TAIL)))

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

  /* ── 路徑解析（appDir / projectDir / 用哪個 python） ──
   * appDir 是「源碼 + venv + data」的家；projectDir 是既有設定（相容舊佈局）。
   * 一鍵安裝把東西都放 appDir，所以 app/run.py 存在時直接以 appDir 當 cwd；
   * 有 app/.venv 時優先拿 venv 的 python 起 PawBot（依賴才裝得到）。
   */
  const appDirectory = () => resolve(String(cfg.appDir || DEFAULTS.appDir))
  const dataDirectory = () => join(appDirectory(), 'data')
  // 專案目錄（run.py 所在）：
  //   1. 使用者**明確**設了非預設的 projectDir，且那裡有 run.py → 尊重它（升級/自訂安裝不被搶走）
  //   2. 否則 app/ 有 run.py → 用 app/（一鍵安裝的預設佈局，venv/data 也都在這裡）
  //   3. 都沒有 → 退回 projectDir 設定（spawn 時會回報 run.py 不存在）
  const projectDirectory = () => {
    const configured = resolve(String(cfg.projectDir || DEFAULTS.projectDir))
    const fallback = resolve(String(DEFAULTS.projectDir))
    if (configured !== fallback && existsSync(join(configured, 'run.py'))) return configured
    const app = appDirectory()
    if (existsSync(join(app, 'run.py'))) return app
    return configured
  }
  const venvPythonPath = () => venvPaths(appDirectory()).python
  const pythonForRun = () => {
    const vp = venvPythonPath()
    if (vp && existsSync(vp)) return vp
    return String(cfg.pythonExe || DEFAULTS.pythonExe)
  }

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
    const cwd = projectDirectory()
    const exe = pythonForRun()
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
        project_dir: projectDirectory(),
        python_exe: pythonForRun(),
        app_dir: appDirectory(),
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
      project_dir: projectDirectory(),
      python_exe: pythonForRun(),
      app_dir: appDirectory(),
    }
  }

  /* ── 一鍵安裝 + NapCat 編排（Host 側） ──
   * 三個狀態：
   *   1. setupJob    —— POST /pawbot-ui/setup/run：只跑「環境準備」（Python / venv / pip / config）
   *   2. boot        —— POST /pawbot-ui/boot：一鍵四步（env → napcat → pawbot → ready）
   *   3. napcatState —— NapCat 偵測/啟動後的小快取（pid、onebot 設定、上次錯誤）
   * 全部冪等：venv 已存在就跳過、容器已存在就 docker start、PawBot 已在跑就不重 spawn。
   * 安裝/啟動的輸出全部進 setupRing（只留 400 行；setup/status 只回最後 60 行）。
   */
  const napcatState = { mode: 'none', pid: null, onebot: null, started_at: 0, last_error: '' }

  const readPawbotServer = () => {
    const fallback = { host: '0.0.0.0', port: 6199, path: '/ws', access_token: '' }
    try {
      const json = JSON.parse(readFileSync(join(dataDirectory(), 'config.json'), 'utf8'))
      const server = json && typeof json.server === 'object' && json.server ? json.server : {}
      return { ...fallback, ...server }
    } catch {
      return fallback
    }
  }

  // NapCat 反向 WS 的目標：PawBot 的 server.{host,port,path}。
  // docker 容器裡的 127.0.0.1 是容器自己，所以改用 host.docker.internal（docker run 已加 --add-host）。
  const onebotWsUrl = (mode) => {
    const server = readPawbotServer()
    let host = String(server.host || '127.0.0.1')
    if (!host || host === '0.0.0.0' || host === '::') host = '127.0.0.1'
    if (mode === 'docker') host = 'host.docker.internal'
    const port = Number(server.port) || 6199
    const rawPath = String(server.path || '/ws')
    const path = rawPath.startsWith('/') ? rawPath : `/${rawPath}`
    return `ws://${host}:${port}${path}`
  }

  // Config 的 onebotToken 為空時，若 PawBot 的 config 已經有 server.access_token（舊安裝留下的），
  // 就沿用它——否則 NapCat 送空 token、PawBot 卻要求 token，反向 WS 會一直被拒。
  const effectiveOnebotToken = () => {
    const configured = String(cfg.onebotToken || '')
    if (configured) return configured
    const fromPawbot = String(readPawbotServer().access_token || '')
    return fromPawbot
  }

  const findQqUin = (configDir) => {
    try {
      for (const name of readdirSync(configDir)) {
        const m = /^onebot11_(\d+)\.json$/i.exec(name)
        if (m) return m[1]
      }
    } catch {
      /* 目錄不存在 / 沒權限：當未知 */
    }
    return ''
  }

  const currentNapcatConfigDir = (mode) =>
    napcatConfigDir({ appDir: appDirectory(), dataDir: dataDirectory(), mode, exists: existsSync })

  const setupJob = createSetupJob({
    runEnv: ({ force, onProgress } = {}) => {
      refreshCfg()
      return runEnvSetup({
        appDir: appDirectory(),
        pythonExe: String(cfg.pythonExe || DEFAULTS.pythonExe),
        onLog: setupLog,
        onProgress,
        platform: process.platform,
        onebotToken: String(cfg.onebotToken || ''),
        force: !!force,
      })
    },
    log: (m) => {
      setupLog(m)
      log(m)
    },
  })

  /* ── boot 的四步 ── */
  const envStep = async ({ force } = {}) => {
    refreshCfg()
    let r
    try {
      r = await runEnvSetup({
        appDir: appDirectory(),
        pythonExe: String(cfg.pythonExe || DEFAULTS.pythonExe),
        onLog: setupLog,
        platform: process.platform,
        onebotToken: String(cfg.onebotToken || ''),
        force: !!force,
      })
    } catch (e) {
      const msg = `环境准备异常：${e?.message || e}`
      return { ok: false, error: msg, detail: msg }
    }
    if (r.ok) {
      const depText = r.deps?.skipped ? '已安装（跳过）' : '安装完成'
      return {
        ok: true,
        detail: `Python ${r.python?.version || '?'}（${r.python?.exe || '?'}）· venv 就绪 · 依赖${depText} · 配置就绪`,
      }
    }
    return {
      ok: false,
      error: r.error || `${r.phase} 失败`,
      hint: r.hint,
      detail: r.hint || r.error || `${r.phase} 失败`,
    }
  }

  const napcatStep = async () => {
    refreshCfg()
    let det
    try {
      det = await detectNapcat({ run: defaultRun })
    } catch (e) {
      det = { mode: 'none', checked: [], detail: `检测失败：${e?.message || e}` }
    }
    napcatState.mode = det.mode

    if (det.mode === 'none') {
      napcatState.last_error = det.detail || '没有 Docker / NapCat'
      setupLog(`NapCat：${napcatState.last_error}`)
      return {
        ok: false,
        skip: true,
        mode: 'none',
        started: false,
        manual: manualGuide(),
        detail: '没有 Docker / NapCat，已跳过（PawBot 仍可启动，但不会连 QQ）',
        hint: manualHint(),
      }
    }
    setupLog(`NapCat 侦测结果：${det.mode}（${det.detail || ''}）`)

    const token = effectiveOnebotToken()
    // 設了 onebotToken 才同步寫回 PawBot 的 server.access_token（不設就不動既有值）
    if (String(cfg.onebotToken || '')) {
      try {
        const cfgRes = ensureConfig({ appDir: appDirectory(), onebotToken: String(cfg.onebotToken || '') })
        if (!cfgRes.ok) setupLog(`同步 server.access_token 失败：${cfgRes.error || ''}`)
        else if (cfgRes.token_updated) setupLog('已把 onebotToken 写进 PawBot 的 server.access_token（PawBot 需重启才生效）')
      } catch (e) {
        setupLog(`同步 server.access_token 异常：${e?.message || e}`)
      }
    }

    let written
    try {
      const configDir = currentNapcatConfigDir(det.mode)
      written = writeOnebotConfig({
        appDir: appDirectory(),
        dataDir: dataDirectory(),
        configDir,
        mode: det.mode,
        qqUin: findQqUin(configDir),
        wsUrl: onebotWsUrl(det.mode),
        token,
      })
    } catch (e) {
      written = { ok: false, error: `写 OneBot 配置异常：${e?.message || e}` }
    }
    if (!written.ok) {
      napcatState.last_error = written.error || '写 OneBot 配置失败'
      return { ok: false, mode: det.mode, started: false, detail: napcatState.last_error, hint: '检查 app/data/napcat 是否可写' }
    }
    const wsUrl = written.config?.network?.websocketClients?.[0]?.url || onebotWsUrl(det.mode)
    napcatState.onebot = { wsUrl, token, qqUin: written.qqUin || '' }
    setupLog(`已写 NapCat OneBot 配置：${written.file}${written.backup ? `（旧档已备份为 ${written.backup}）` : ''}`)
    setupLog(`OneBot 反向 WS 目标：${wsUrl}（token 一律不记日志）`)

    let started
    try {
      started = await startNapcat({
        mode: det.mode,
        run: defaultRun,
        dataDir: dataDirectory(),
        image: String(cfg.napcatImage || NAPCAT_IMAGE),
        webuiPort: Number(cfg.napcatWebuiPort) || NAPCAT_WEBUI_PORT,
        log: setupLog,
      })
    } catch (e) {
      started = { ok: false, mode: det.mode, error: `启动 NapCat 异常：${e?.message || e}` }
    }
    if (started.pid) napcatState.pid = started.pid
    if (started.ok) {
      napcatState.started_at = Date.now()
      napcatState.last_error = ''
    } else {
      napcatState.last_error = started.error || started.hint || '启动 NapCat 失败'
    }
    return {
      ok: started.ok === true,
      mode: det.mode,
      started: started.ok === true,
      manual: started.manual,
      detail: started.detail || (started.ok ? 'NapCat 已启动' : napcatState.last_error),
      hint: started.ok ? undefined : (started.hint || started.error),
      onebot: napcatState.onebot,
    }
  }

  const pawbotStep = async () => {
    refreshCfg()
    const wasRunning = isRunning(currentChild)
    const child = startChild()
    if (!child) {
      const reason = lastSpawnError || lastError || 'spawn 失败'
      return {
        ok: false,
        error: reason,
        detail: `PawBot 启动失败：${reason}`,
        hint: '看 /pawbot-ui/status 的 last_error 与 setup log；常见原因是依赖未装好或 run.py 不存在',
      }
    }
    return {
      ok: true,
      pid: child.pid,
      detail: wasRunning
        ? `PawBot 已在运行（pid=${child.pid}），未重复启动`
        : `已启动 PawBot（pid=${child.pid}，python=${pythonForRun()}）`,
    }
  }

  const READY_TIMEOUT_MS = 60_000
  const readyStep = async () => {
    const deadline = Date.now() + READY_TIMEOUT_MS
    let last = null
    while (Date.now() < deadline) {
      const http = await httpStatus()
      if (http.ok) return { ok: true, detail: `PawBot /api/status 可达（HTTP ${http.http_status}）` }
      last = http
      if (!isRunning(currentChild)) {
        const reason = lastSpawnError || lastError || '进程已退出'
        return {
          ok: false,
          error: reason,
          detail: `PawBot 进程已退出：${reason}`,
          hint: '看 setup log 与 pawbot_logs；常见原因是 Python 依赖没装好或 config 写错',
        }
      }
      await new Promise((r) => setTimeout(r, 1500))
    }
    const why = last?.error || '未知原因'
    return {
      ok: false,
      error: why,
      detail: `等待 /api/status 超时（${READY_TIMEOUT_MS / 1000}s）：${why}`,
      hint: 'PawBot 可能还没起来：看 setup log / pawbot_logs，或确认 webuiHost、webuiPort 设定',
    }
  }

  const boot = createBootOrchestrator({
    env: envStep,
    napcat: napcatStep,
    pawbot: pawbotStep,
    ready: readyStep,
    log: (m) => {
      setupLog(m)
      log(m)
    },
  })

  /* ── 給 /pawbot-ui 路由與工具用的 payload ── */
  const setupStatusPayload = async () => {
    refreshCfg()
    let state
    try {
      state = await setupState({
        appDir: appDirectory(),
        pythonExe: String(cfg.pythonExe || DEFAULTS.pythonExe),
        run: defaultRun,
        platform: process.platform,
        onebotToken: String(cfg.onebotToken || ''),
        logs: setupLogTail(SETUP_LOG_TAIL),
      })
    } catch (e) {
      state = {
        appDir: appDirectory(),
        appPresent: false,
        python: { ok: false, version: null, exe: null, error: String(e?.message || e) },
        venv: { exists: false, path: venvPaths(appDirectory()).dir },
        deps: { pillow: false, websockets: false, ok: false, checked: false },
        config: { exists: false, valid: false },
        phase: 'error',
        progress: 0,
        message: `自检失败：${e?.message || e}`,
        log: setupLogTail(SETUP_LOG_TAIL),
      }
    }
    const job = setupJob.status()
    const b = boot.status()
    if (job.running) {
      state.phase = 'installing'
      state.progress = job.progress
      state.message = job.message
    }
    if (b.running) {
      state.phase = b.phase
      state.progress = b.progress
      state.message = b.message
    } else if (b.done && b.phase === 'failed') {
      state.message = b.message
    }
    return { ok: true, ...state, install: job, boot: b }
  }

  const maskWebui = (webui) => {
    // token 是機密：只在 /pawbot-ui/napcat/status 回給本機面板；工具回傳一律遮掉
    if (!webui || !webui.url) return webui ? { ...webui, token: webui.token ? '***' : '' } : null
    let url = String(webui.url)
    try {
      const parsed = new URL(url)
      if (parsed.searchParams.has('token')) {
        parsed.searchParams.set('token', '***')
        url = parsed.href
      }
    } catch {
      /* URL 壞掉就原樣回，但 token 已遮 */
    }
    return { url, token: webui.token ? '***' : '', source: webui.source || null }
  }

  const napcatStatusPayload = async ({ revealToken = false } = {}) => {
    refreshCfg()
    let det
    try {
      det = await detectNapcat({ run: defaultRun })
    } catch (e) {
      det = { mode: 'none', checked: [], detail: `检测失败：${e?.message || e}` }
    }
    const mode = det.mode
    if (napcatState.mode !== mode) napcatState.mode = mode
    const configDir = currentNapcatConfigDir(mode)

    const [running, logText] = await Promise.all([
      napcatRunning({ mode, run: defaultRun, pid: napcatState.pid }).catch(() => false),
      collectNapcatLogs({ mode, run: defaultRun }).catch(() => ''),
    ])
    // 注意：logText 可能含 WebUI token，只拿來解析，絕不寫進 log / 回給模型
    const webui = readWebui({ mode, logText, configDir, exists: existsSync })
    const qr = qrFromLog(logText)
    const http = await httpStatus()
    const pawbotStatus = http.ok ? (http.data?.data ?? http.data ?? null) : null
    const login = isLoggedIn({ pawbotStatus, logText })
    const onebot = napcatState.onebot || {
      wsUrl: onebotWsUrl(mode),
      token: effectiveOnebotToken(),
      qqUin: findQqUin(configDir),
    }

    let hint
    if (mode === 'none') hint = manualHint()
    else if (!running) hint = 'NapCat 未运行：按「一键」或 POST /pawbot-ui/napcat/setup 重试'
    else if (login.loggedIn) hint = 'NapCat 已登录，PawBot 已收到反向 WS 连接'
    else if (webui) hint = '打开 WebUI 用 QQ 扫码登录；登录成功后 PawBot 会自动收到 WS 连接'
    else hint = 'NapCat 在跑但还没拿到 WebUI 地址：等几秒后按「刷新二维码」'

    return {
      ok: true,
      mode,
      running: !!running,
      webui: revealToken ? webui : maskWebui(webui),
      qr,
      loggedIn: login.loggedIn,
      logged_in_by: login.evidence,
      onebot,
      hint,
      manual: mode === 'none' ? manualGuide() : undefined,
      checked: det.checked,
      detail: det.detail || null,
      last_error: napcatState.last_error || null,
    }
  }

  // 路由版：請求本身成功就回 ok:true（NapCat 沒啟動用 started:false / hint 表達），
  // 因為面板的 api() 會把 ok:false 當成拋錯，這樣才看得到 manual.steps。
  // 工具版（strictOk）則保留 r.ok，讓模型不會把「沒啟動」誤讀成成功。
  const napcatSetupPayload = async ({ strictOk = false } = {}) => {
    refreshCfg()
    const r = await napcatStep()
    return {
      ok: strictOk ? r.ok === true : true,
      mode: r.mode || napcatState.mode,
      started: r.started === true,
      manual: r.manual,
      hint: r.hint || r.detail,
      error: r.ok === true ? undefined : (r.hint || r.detail),
      onebot: r.onebot || napcatState.onebot,
    }
  }

  const napcatQrPayload = async () => {
    refreshCfg()
    let mode = napcatState.mode
    if (!mode || mode === 'none') {
      try {
        mode = (await detectNapcat({ run: defaultRun })).mode
      } catch {
        mode = 'none'
      }
      napcatState.mode = mode
    }
    const configDir = currentNapcatConfigDir(mode)
    const logText = await collectNapcatLogs({ mode, run: defaultRun }).catch(() => '')
    const webui = readWebui({ mode, logText, configDir, exists: existsSync })
    const qr = qrFromLog(logText)
    // ok:true = 「這次抓取完成了」；抓不到碼用 qr/webui 都是 null + hint 表達
    return {
      ok: true,
      mode,
      qr,
      webui,
      hint: (webui || qr)
        ? '用 WebUI（或二维码链接）在 QQ 扫码登录'
        : '还没拿到二维码：NapCat 刚启动的话等几秒再试；也可以打开 WebUI 扫码',
    }
  }

  const setupRunPayload = ({ force = false } = {}) => {
    refreshCfg()
    // 一鍵流程本身就含環境安裝：不要讓兩套 pip 同時跑
    if (boot.status().running) {
      return {
        ok: true,
        started: false,
        already_running: true,
        hint: '一键流程正在执行，环境安装已包含在里面；用 GET /pawbot-ui/setup/status 看进度',
      }
    }
    const r = setupJob.start({ force: force === true })
    return { ok: true, started: r.started === true, already_running: r.already_running === true }
  }

  const bootStartPayload = ({ force = false } = {}) => {
    refreshCfg()
    const job = setupJob.status()
    if (job.running) {
      return {
        ok: true,
        started: false,
        already_running: true,
        phase: 'installing',
        progress: job.progress,
        steps: boot.status().steps,
        hint: '环境安装正在进行，等它完成再按一键；或先看 setup/status',
      }
    }
    const r = boot.start({ force: force === true })
    const status = boot.status()
    return {
      ok: true,
      started: r.started === true,
      already_running: r.already_running === true,
      phase: status.phase,
      progress: status.progress,
      steps: status.steps,
    }
  }

  const bootStatusPayload = () => ({ ok: true, ...boot.status() })

  /* ── Agent 工具：一鍵安裝 / NapCat ── */
  tool({
    name: 'pawbot_setup',
    description:
      '一键安装并启动 PawBot（含 NapCat 编排）：建 app/.venv → pip 装 app/requirements.txt（通常要几分钟，' +
      '会写入 app/.venv 与 app/data）→ 准备 config.json → 检测并启动 NapCat → 启动 PawBot 子进程 → 等 /api/status 可达。' +
      '非阻塞：启动后最多等约 120 秒；没跑完再调用本工具（不加 force）就是查进度。' +
      '幂等：重复调用不会重复建 venv、不会起第二个 NapCat 容器、不会 spawn 第二个 PawBot。' +
      '依赖安装失败、没有 Python、没有 docker/NapCat 时都会回结构化错误与 hint，不会假装成功。',
    parameters: {
      force: {
        type: 'boolean',
        description: 'true = 强制重装 pip 依赖并重跑四步；默认 false（已完成的步骤会跳过）。',
      },
    },
    async execute(args) {
      refreshCfg()
      const before = boot.status()
      const install = setupJob.status()
      if (install.running && !before.running) {
        return {
          ok: true,
          started: false,
          already_running: true,
          done: false,
          phase: 'installing',
          progress: install.progress,
          message: install.message,
          steps: before.steps,
          hint: '环境安装正在进行（pip 可能要几分钟）；稍后再调用 pawbot_setup 查进度，不需要 force',
        }
      }
      if (before.running) {
        return {
          ok: true,
          started: false,
          already_running: true,
          done: false,
          phase: before.phase,
          progress: before.progress,
          message: before.message,
          steps: before.steps,
          hint: '一键流程已在执行中；稍后再调用本工具即可查进度',
        }
      }
      const r = boot.start({ force: args?.force === true })
      const fin = await boot.waitForDone(120_000)
      const payload = {
        ok: fin.done ? fin.phase !== 'failed' : true,
        started: r.started === true,
        done: !!fin.done,
        phase: fin.phase,
        progress: fin.progress,
        message: fin.message,
        steps: fin.steps,
        log_tail: setupLogTail(40),
      }
      if (fin.error) payload.error = fin.error
      if (!fin.done) {
        payload.hint = '安装仍在后台进行（pip 可能要几分钟）；稍后再调用 pawbot_setup 查进度，不需要 force'
      }
      return payload
    },
  })

  tool({
    name: 'pawbot_napcat',
    description:
      'NapCat（QQ 登录端）编排：action=status 查执行方式/是否在跑/WebUI/二维码/是否已登录；' +
      'action=start 检测 + 写 OneBot 反向 WS 配置 + 尝试启动（docker 或 systemd/子进程）；action=qr 重新读 WebUI/日志拿二维码。' +
      '⚠ 这台机器若没有 docker/NapCat，只能回 manual.steps 让人手动装（未实机验证）。' +
      'WebUI 的 token 属于机密：工具回传会遮掉，只有本机面板的 /pawbot-ui/napcat/status 看得到真值。',
    parameters: {
      action: {
        type: 'string',
        enum: ['status', 'start', 'qr'],
        description: 'status=查状态（默认）；start=检测+写配置+尝试启动；qr=重新取二维码。',
      },
    },
    async execute(args) {
      const action = String(args?.action || 'status')
      if (action === 'status') {
        try {
          return await napcatStatusPayload({ revealToken: false })
        } catch (e) {
          return { ok: false, error: `NapCat 自检失败：${e?.message || e}` }
        }
      }
      if (action === 'start') {
        try {
          return await napcatSetupPayload({ strictOk: true })
        } catch (e) {
          return { ok: false, error: `NapCat 启动失败：${e?.message || e}` }
        }
      }
      if (action === 'qr') {
        try {
          const r = await napcatQrPayload()
          return { ...r, webui: maskWebui(r.webui) }
        } catch (e) {
          return { ok: false, error: `取二维码失败：${e?.message || e}` }
        }
      }
      return { ok: false, error: `未知 action：${action}（只支持 status / start / qr）` }
    },
  })

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
    // 一鍵安裝 / NapCat / boot 這三組路由的實作（路由本身與 CSRF 守衛都在 lib/webui-proxy.js）
    setup: {
      status: setupStatusPayload,
      run: setupRunPayload,
    },
    napcat: {
      status: () => napcatStatusPayload({ revealToken: true }),
      setup: napcatSetupPayload,
      qr: napcatQrPayload,
    },
    boot: {
      start: bootStartPayload,
      status: bootStatusPayload,
    },
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
      'setup/status', 'setup/run',
      'napcat/status', 'napcat/setup', 'napcat/qr',
      'boot', 'boot/status',
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

  log(`已就绪：projectDir=${projectDirectory()} appDir=${appDirectory()} autostart=${cfg.autostart} restartOnCrash=${cfg.restartOnCrash}`)
}
