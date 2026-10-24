/**
 * lib/napcat.js —— NapCat（QQ 登入端）編排：偵測 / 寫 OneBot 反向 WS 設定 / 啟動 / 讀 WebUI。
 * ============================================================================
 * ⚠️ 誠實標註：**本模組的自動啟動路徑（docker / systemd / binary）沒有實機驗證過**。
 *    開發這台機器沒有 docker、也沒有 NapCat，所以只有「偵測 → none → 給手動步驟」是真的跑過的；
 *    docker 的指令引數、systemd 服務名、binary 位置都照官方文件／社群慣例寫，第一次實機用請看回傳的
 *    detail / log 再判斷，不要假設它一定成功。
 *
 * 安全：WebUI 的 token 屬於機密——只由 readWebui() 取出、只經由 /pawbot-ui/napcat/status 回給
 *      本機面板；本模組不把 token / WebUI URL 寫進 log。onebot token 只寫進設定檔，也不寫 log。
 *
 * 所有外部指令都走 setup.js 的 run（預設 execFile），一律帶 timeout，失敗回結構化結果、不 throw。
 * ============================================================================
 */
import { spawn } from 'node:child_process'
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { join } from 'node:path'

import { safeRun, TIMEOUT } from './setup.js'

/** 官方 docker 鏡像（可被 Config 的 napcatImage 覆蓋）。 */
export const NAPCAT_IMAGE = 'mlikiowa/napcat-docker:latest'
/** 本插件建立的容器名（用來冪等：已存在就 docker start）。 */
export const NAPCAT_CONTAINER = 'pawbot-napcat'
/** NapCat WebUI 的容器內 / 預設外部埠。 */
export const NAPCAT_WEBUI_PORT = 6099
/** Linux 官方安裝腳本的 NapCat 根目錄。 */
export const NAPCAT_LINUX_ROOT = '/opt/QQ/resources/app/app_launcher/napcat'
/** Linux 安裝的 config 目錄（onebot11_*.json / webui.json 都在這裡）。 */
export const NAPCAT_LINUX_CONFIG_DIR = join(NAPCAT_LINUX_ROOT, 'config')
/** Linux 安裝的 webui.json 位置。 */
export const NAPCAT_LINUX_WEBUI_FILE = join(NAPCAT_LINUX_CONFIG_DIR, 'webui.json')
/** docker volume 在容器內掛載到哪裡（官方 napcat-docker 的慣例）。 */
export const NAPCAT_DOCKER_CONFIG_PATH = '/app/napcat/config'

function isPlainObject(v) {
  return v != null && typeof v === 'object' && !Array.isArray(v)
}

function textTail(value, max = 400) {
  const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function firstLine(value) {
  const lines = String(value == null ? '' : value).split(/\r?\n/).filter((line) => line.trim())
  return lines.length ? lines[0].trim() : ''
}

/**
 * 沒有 docker / NapCat 時給人的手動步驟。
 * 刻意**不編造**任何 curl 一鍵安裝網址——不確定的東西就說「到 GitHub Release 頁下載」。
 */
export function manualGuide() {
  return {
    docs: 'https://github.com/NapNeko/NapCatQQ',
    steps: [
      '（推薦）安裝 Docker 後重新整理面板、再按一次「一鍵」：插件會自動起 mlikiowa/napcat-docker 容器，並把 WebUI 6099 埠對外。',
      '（或）到 https://github.com/NapNeko/NapCatQQ 的 Release 頁下載 Linux 一鍵安裝腳本（napcat.sh），照該頁的說明執行；本插件不代為下載，也不提供不確定的安裝網址。',
      '（或）自行安裝後確認 /opt/QQ/resources/app/app_launcher/napcat 存在，或 PATH 裡有 napcat 指令；面板偵測到後會用 systemd（服務名 napcat）或子進程啟動。',
      'NapCat 起來後打開 WebUI（預設 http://127.0.0.1:6099/webui）用 QQ 掃碼登入；登入成功後它會反向連上 PawBot 的 ws://127.0.0.1:6199/ws。',
    ],
    note: '本机没有 Docker / NapCat，以上路径均未实机验证。',
  }
}

/** 給 status.hint 用的一句話說明。 */
export function manualHint() {
  return '这台机器没有 Docker / NapCat：请看 manual.steps 手动安装（未实机验证）'
}

/**
 * 偵測 NapCat 的執行方式（詳細版）。
 *   ① docker 可執行且 `docker ps` 通 → 'docker'
 *   ② /opt/QQ/resources/app/app_launcher/napcat 存在，或 PATH 有 `napcat` → 'binary'
 *   ③ 都沒有 → 'none'
 * @returns {Promise<{mode:'docker'|'binary'|'none', checked:Array, detail:string, path?:string}>}
 */
export async function detectNapcat({
  run,
  exists = existsSync,
  platform = process.platform,
  timeout = TIMEOUT.probe,
} = {}) {
  const checked = []
  const dockerVersion = await safeRun(run, 'docker', ['--version'], { timeout })
  checked.push({ candidate: 'docker --version', ok: dockerVersion.ok, error: dockerVersion.error || undefined })
  if (dockerVersion.ok) {
    const ps = await safeRun(run, 'docker', ['ps'], { timeout })
    checked.push({ candidate: 'docker ps', ok: ps.ok, error: ps.error || undefined })
    if (ps.ok) {
      return { mode: 'docker', checked, detail: 'docker 可用（docker ps 正常）' }
    }
  }

  const linuxRoot = !!exists(NAPCAT_LINUX_ROOT)
  const binProbe = platform === 'win32'
    ? await safeRun(run, 'where', ['napcat'], { timeout })
    : await safeRun(run, 'sh', ['-c', 'command -v napcat'], { timeout })
  checked.push({
    candidate: platform === 'win32' ? 'where napcat' : 'command -v napcat',
    ok: binProbe.ok,
    error: binProbe.error || undefined,
  })
  if (linuxRoot || binProbe.ok) {
    const path = linuxRoot ? NAPCAT_LINUX_ROOT : (firstLine(binProbe.stdout) || 'napcat')
    return {
      mode: 'binary',
      checked,
      path,
      detail: linuxRoot
        ? `找到 Linux 官方安装目录：${NAPCAT_LINUX_ROOT}`
        : `PATH 里有 napcat：${path}`,
    }
  }

  return {
    mode: 'none',
    checked,
    detail: '没有 docker、也没有 NapCat（/opt/QQ/resources/app/app_launcher/napcat 或 napcat 指令）',
  }
}

/** 只要模式的簡化版（給需要字串的呼叫端）。 */
export async function detectMode(opts = {}) {
  const r = await detectNapcat(opts)
  return r.mode
}

/**
 * NapCat 的 config 目錄（onebot11_*.json / webui.json 放這裡）。
 *   - binary（Linux 官方安裝且目錄存在）→ /opt/QQ/resources/app/app_launcher/napcat/config
 *   - 其他（含 docker）→ `<dataDir>/napcat`，也就是 docker 的
 *     `-v <dataDir>/napcat:/app/napcat/config` 在 host 端對應的目錄
 */
export function napcatConfigDir({ appDir, dataDir, mode = 'docker', exists = existsSync } = {}) {
  if (mode === 'binary' && exists(NAPCAT_LINUX_CONFIG_DIR)) return NAPCAT_LINUX_CONFIG_DIR
  const base = dataDir ? String(dataDir) : join(String(appDir || process.cwd()), 'data')
  return join(base, 'napcat')
}

/**
 * NapCat 官方格式的 OneBot 反向 WebSocket 設定（websocketClients 指回 PawBot 的 WS server）。
 * 形狀固定，離線單測會逐欄比對。
 */
export function buildOnebotConfig({ wsUrl, token = '' } = {}) {
  return {
    network: {
      httpServers: [],
      httpClients: [],
      websocketServers: [],
      websocketClients: [
        {
          name: 'pawbot',
          enable: true,
          url: String(wsUrl || ''),
          messagePostFormat: 'array',
          reportSelfMessage: false,
          reconnectInterval: 5000,
          token: String(token || ''),
          debug: false,
          heartInterval: 30000,
        },
      ],
    },
    musicSignUrl: '',
    enableLocalFile2Url: false,
    parseMultMsg: false,
  }
}

/** 數字 QQ 號才寫進檔名；未知就寫 onebot11.json。 */
function onebotFileName(qqUin) {
  const digits = String(qqUin == null ? '' : qqUin).replace(/\D/g, '')
  return digits ? `onebot11_${digits}.json` : 'onebot11.json'
}

/**
 * 寫 NapCat 的 OneBot 反向 WS 設定到 `<configDir>/onebot11_<qqUin>.json`。
 * 既有檔一定先備份成 `<file>.before-pawbot`（保留最原始的那份，不覆蓋備份）。
 * @returns {{ok:boolean, file:string, dir:string, backup:string|null, qqUin:string|null, config:object, error?:string}}
 */
export function writeOnebotConfig({
  appDir,
  dataDir,
  configDir,
  mode = 'docker',
  qqUin = '',
  wsUrl = '',
  token = '',
  exists = existsSync,
} = {}) {
  const dir = configDir
    ? String(configDir)
    : napcatConfigDir({ appDir, dataDir, mode, exists })
  if (!dir) return { ok: false, file: '', dir: '', backup: null, qqUin: null, config: null, error: '缺少 config 目录' }

  const digits = String(qqUin == null ? '' : qqUin).replace(/\D/g, '')
  const file = join(dir, onebotFileName(digits))
  const config = buildOnebotConfig({ wsUrl, token })

  let backup = null
  try {
    mkdirSync(dir, { recursive: true })
    if (exists(file)) {
      const backupPath = `${file}.before-pawbot`
      if (!exists(backupPath)) copyFileSync(file, backupPath)
      backup = backupPath
    }
  } catch (e) {
    return { ok: false, file, dir, backup: null, qqUin: digits || null, config, error: `备份/建立目录失败：${e?.message || e}` }
  }

  const text = `${JSON.stringify(config, null, 2)}\n`
  const tmp = `${file}.tmp-${process.pid}-${Date.now()}`
  try {
    writeFileSync(tmp, text, 'utf8')
    try {
      renameSync(tmp, file)
    } catch {
      writeFileSync(file, text, 'utf8')
    }
  } catch (e) {
    return { ok: false, file, dir, backup, qqUin: digits || null, config, error: `写 ${file} 失败：${e?.message || e}` }
  } finally {
    try { if (exists(tmp)) unlinkSync(tmp) } catch { /* ignore */ }
  }

  return { ok: true, file, dir, backup, qqUin: digits || null, config }
}

/**
 * 啟動 NapCat。
 *   docker：同名容器已存在就 `docker start`，否則 `docker run -d …`（冪等，不會起第二個容器）。
 *   binary：先 `systemctl start napcat`；失敗才把 `napcat` 當 detached 子進程起（記錄 pid）。
 *   none  ：回 `{ok:false, mode:'none', manual:{steps}}`，步驟裡給 Linux 官方安裝指引（不編網址）。
 */
export async function startNapcat({
  mode = 'none',
  run,
  appDir,
  dataDir,
  image = NAPCAT_IMAGE,
  webuiPort = NAPCAT_WEBUI_PORT,
  container = NAPCAT_CONTAINER,
  spawnProcess = spawn,
  log = () => {},
  exists = existsSync,
  timeout = TIMEOUT.docker,
} = {}) {
  const say = (line) => { try { log(line) } catch { /* ignore */ } }
  if (mode === 'none') {
    return {
      ok: false,
      mode: 'none',
      started: false,
      manual: manualGuide(),
      hint: manualHint(),
      error: '没有可用的 NapCat 执行方式（docker / binary 都没有）',
    }
  }

  if (mode === 'docker') {
    const base = dataDir ? String(dataDir) : join(String(appDir || process.cwd()), 'data')
    const hostConfigDir = join(base, 'napcat')
    try {
      mkdirSync(hostConfigDir, { recursive: true })
    } catch (e) {
      return { ok: false, mode, started: false, error: `建立 napcat 数据目录失败：${e?.message || e}` }
    }

    const list = await safeRun(run, 'docker', [
      'ps', '-a', '--filter', `name=^/${container}$`, '--format', '{{.Names}}',
    ], { timeout: TIMEOUT.probe })
    const existing = list.ok && String(list.stdout).split(/\r?\n/).some((name) => name.trim() === container)

    if (existing) {
      say(`NapCat 容器已存在（${container}），尝试 docker start（不会起第二个）`)
      const started = await safeRun(run, 'docker', ['start', container], { timeout })
      if (!started.ok) {
        return {
          ok: false,
          mode,
          started: false,
          already_existing: true,
          container,
          error: started.error || firstLine(started.stderr) || 'docker start 失败',
          hint: `docker start ${container} 失败：请看 \`docker logs ${container}\``,
        }
      }
      return {
        ok: true,
        mode,
        started: true,
        already_existing: true,
        container,
        detail: `已启动既有容器 ${container}`,
      }
    }

    const args = [
      'run', '-d',
      '--name', container,
      '--restart', 'unless-stopped',
      '-p', `${Number(webuiPort) || NAPCAT_WEBUI_PORT}:6099`,
      '-v', `${hostConfigDir}:${NAPCAT_DOCKER_CONFIG_PATH}`,
      '--add-host', 'host.docker.internal:host-gateway',
      '-e', 'NAPCAT_GID=0',
      '-e', 'MODE=astrbot',
      String(image || NAPCAT_IMAGE),
    ]
    say(`启动 NapCat 容器：docker ${args.join(' ')}`)
    const created = await safeRun(run, 'docker', args, { timeout })
    if (!created.ok) {
      const hint = created.timedOut
        ? `docker run 超时（${Math.round(timeout / 1000)} 秒）：镜像可能还在拉取，可先手动 docker pull ${image} 再重试`
        : `docker run 失败：可先手动 docker pull ${image}，或检查 6099 埠是否被占用、docker 是否有权限`
      return {
        ok: false,
        mode,
        started: false,
        container,
        image: String(image || NAPCAT_IMAGE),
        error: created.error || firstLine(created.stderr) || 'docker run 失败',
        hint,
        command: `docker ${args.join(' ')}`,
      }
    }
    return {
      ok: true,
      mode,
      started: true,
      container,
      image: String(image || NAPCAT_IMAGE),
      detail: `已建立并启动容器 ${container}（WebUI 埠 ${Number(webuiPort) || NAPCAT_WEBUI_PORT}）`,
      command: `docker ${args.join(' ')}`,
    }
  }

  if (mode === 'binary') {
    const svc = await safeRun(run, 'systemctl', ['start', 'napcat'], { timeout: TIMEOUT.service })
    if (svc.ok) {
      return { ok: true, mode, started: true, via: 'systemctl', detail: 'systemctl start napcat 成功' }
    }
    // systemd 不可用（沒這個服務 / 沒權限 / 非 systemd 系統）→ 子進程 fallback
    let child
    try {
      child = spawnProcess('napcat', [], { detached: true, stdio: 'ignore', windowsHide: true })
      child?.unref?.()
    } catch (e) {
      return {
        ok: false,
        mode,
        started: false,
        error: `systemctl 与子进程启动都失败：${e?.message || e}`,
        hint: 'systemctl start napcat 失败，直接执行 napcat 也失败；请手动在终端跑一次看报错',
      }
    }
    return {
      ok: true,
      mode,
      started: true,
      via: 'process',
      pid: child?.pid ?? null,
      detail: `systemctl 不可用（${textTail(svc.error || firstLine(svc.stderr) || '失败')}），已把 napcat 当子进程启动（pid=${child?.pid ?? '?'}）`,
    }
  }

  return {
    ok: false,
    mode,
    started: false,
    error: `未知的 NapCat mode：${mode}`,
    manual: manualGuide(),
    hint: manualHint(),
  }
}

/** NapCat 是否在跑（docker inspect / systemctl is-active / 記錄的 pid）。 */
export async function napcatRunning({
  mode = 'none',
  run,
  pid = null,
  container = NAPCAT_CONTAINER,
  timeout = TIMEOUT.probe,
} = {}) {
  if (mode === 'docker') {
    const r = await safeRun(run, 'docker', ['inspect', '-f', '{{.State.Running}}', container], { timeout })
    return r.ok && String(r.stdout).trim().toLowerCase() === 'true'
  }
  if (mode === 'binary') {
    const r = await safeRun(run, 'systemctl', ['is-active', 'napcat'], { timeout })
    if (r.ok && String(r.stdout).trim().toLowerCase() === 'active') return true
    if (pid) {
      try {
        process.kill(Number(pid), 0)
        return true
      } catch {
        return false
      }
    }
    return false
  }
  return false
}

/**
 * 讀 NapCat 的日誌文字（給 readWebui / qrFromLog 解析用）。
 * 注意：日誌裡可能有 WebUI token —— 這裡只回字串，呼叫端**不可以**把它寫進 log。
 */
export async function collectNapcatLogs({
  mode = 'none',
  run,
  container = NAPCAT_CONTAINER,
  timeout = TIMEOUT.probe,
} = {}) {
  if (mode === 'docker') {
    const r = await safeRun(run, 'docker', ['logs', '--tail', '300', container], { timeout })
    return `${r.stdout}\n${r.stderr}`.trim()
  }
  if (mode === 'binary') {
    const r = await safeRun(run, 'journalctl', ['-u', 'napcat', '-n', '300', '--no-pager'], { timeout })
    return r.ok ? `${r.stdout}\n${r.stderr}`.trim() : ''
  }
  return ''
}

/**
 * 從 NapCat 日誌 / webui.json 取 WebUI 的 URL 與 token。
 * 日誌格式（容忍 host/port 變化）：
 *   WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxx
 * @returns {{url:string, token:string, source:'log'|'webui.json', file?:string}|null}
 */
export function readWebui({
  mode = 'docker',
  logText = '',
  configDir,
  exists = existsSync,
  readFile = readFileSync,
} = {}) {
  const text = String(logText || '')

  const withToken = (raw) => {
    let url = String(raw || '').trim().replace(/[),.;'"]+$/, '')
    if (!url) return null
    try {
      const parsed = new URL(url)
      return { url: parsed.href, token: parsed.searchParams.get('token') || '', source: 'log' }
    } catch {
      return null
    }
  }

  // ① 官方那行：WebUi User Panel Url: http://…/webui?token=…
  const official = text.match(/WebUi\s+User\s+Panel\s+Url\s*[:：]?\s*(https?:\/\/[^\s"'<>]+)/i)
  if (official) {
    const picked = withToken(official[1])
    if (picked) return picked
  }

  // ② 寬鬆版：任何 /webui?…token=… 的 URL（日誌格式改了也還能抓到）
  const loose = text.match(/(https?:\/\/[^\s"'<>]*\/webui\?[^\s"'<>]*token=[^\s"'<>&]+[^\s"'<>]*)/i)
  if (loose) {
    const picked = withToken(loose[1])
    if (picked) return picked
  }

  // ③ webui.json（NapCat 會把 token 寫在這裡）
  const candidates = []
  if (configDir) candidates.push(join(String(configDir), 'webui.json'))
  candidates.push(NAPCAT_LINUX_WEBUI_FILE)
  for (const file of candidates) {
    if (!file || !exists(file)) continue
    try {
      const json = JSON.parse(readFile(file, 'utf8'))
      if (!isPlainObject(json) || !json.token) continue
      const rawHost = String(json.host || '127.0.0.1')
      const host = rawHost === '0.0.0.0' || rawHost === '::' || rawHost === '' ? '127.0.0.1' : rawHost
      const port = Number(json.port) || NAPCAT_WEBUI_PORT
      return {
        url: `http://${host}:${port}/webui?token=${encodeURIComponent(String(json.token))}`,
        token: String(json.token),
        source: 'webui.json',
        file,
      }
    } catch {
      /* 壞掉的 webui.json 就跳過 */
    }
  }

  return null
}

/**
 * 從日誌抓二維碼：優先 URL（https://…qrcode…），其次二維碼圖片檔案路徑。
 * @returns {{kind:'url'|'file', value:string}|null}
 */
export function qrFromLog(logText) {
  const text = String(logText || '')
  const url = text.match(/(https?:\/\/[^\s"'<>]*(?:qrcode|qr_code|qr-code|qr\.)[^\s"'<>]*)/i)
  if (url) return { kind: 'url', value: url[1].replace(/[),.;'"]+$/, '') }

  const file = text.match(/((?:[A-Za-z]:\\|\/)[^\s"'<>]*?qrcode[^\s"'<>]*?\.(?:png|jpe?g|gif))/i)
  if (file) return { kind: 'file', value: file[1] }

  const bare = text.match(/\b(qrcode[^\s"'<>]*\.(?:png|jpe?g|gif))/i)
  if (bare) return { kind: 'file', value: bare[1] }

  return null
}

/**
 * 「NapCat 已登入」最可靠的間接信號：PawBot 的 /api/status 有 connections（收到反向 WS）。
 * 純函數，容忍幾種可能的包裝形狀。
 */
export function loggedInByPawbot(pawbotStatus) {
  if (!pawbotStatus || typeof pawbotStatus !== 'object') return false
  const candidates = [
    pawbotStatus.connections,
    pawbotStatus.data?.connections,
    pawbotStatus.status?.connections,
    pawbotStatus.bot?.connections,
    pawbotStatus.onebot?.connections,
  ]
  for (const value of candidates) {
    if (Array.isArray(value)) {
      if (value.length > 0) return true
    } else if (typeof value === 'number') {
      if (value > 0) return true
    } else if (value && typeof value === 'object') {
      if (Object.keys(value).length > 0) return true
    }
  }
  return false
}

/**
 * 綜合判斷登入狀態：
 *   ① PawBot /api/status 的 connections 非空 → 最可靠（真的收到 NapCat 的 WS）
 *   ② NapCat 日誌出現登入成功的字樣 → 間接信號（未實機驗證，措辭可能随版本變）
 */
export function isLoggedIn({ pawbotStatus = null, logText = '' } = {}) {
  if (loggedInByPawbot(pawbotStatus)) return { loggedIn: true, evidence: 'pawbot-ws' }
  const text = String(logText || '')
  if (/登录成功|登入成功|Login\s*success|账号登录成功|account\s+login\s+success/i.test(text)) {
    return { loggedIn: true, evidence: 'napcat-log' }
  }
  return { loggedIn: false, evidence: null }
}
