/**
 * lib/setup.js —— 環境自檢 + 一鍵安裝（venv / pip / config），以及「一鍵」步驟狀態機。
 * ============================================================================
 * 目的：使用者裝好本插件後，不必自己開終端機——
 *   detectPython → ensureVenv → ensureDeps → ensureConfig
 * 這四步全部做成「可注入 run(cmd, args, opts)」的函數，離線單測用假 run 就能跑，
 * 不需要真的建 venv / 裝 pip / 連 docker。
 *
 * 所有外部指令一律帶 timeout，失敗一律回 `{ ok:false, … }` 結構化物件，**不 throw 到路由外**。
 *
 * run(cmd, args, opts) 契約：
 *   opts = { cwd?, env?, timeout?, maxBuffer?, onLine?(line, stream) }
 *   回傳 Promise<{ ok, code, stdout, stderr, error, timedOut, duration_ms, cmd, args }>
 *   預設用 `child_process.execFile`（見 defaultRun）；測試時換成假函數即可。
 * ============================================================================
 */
import { execFile } from 'node:child_process'
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

/** 外部指令的逾時（毫秒）。venv/pip 5 分鐘、docker/systemctl 60 秒、狀態查詢 10 秒。 */
export const TIMEOUT = {
  /** python -m venv */
  venv: 300_000,
  /** pip install */
  pip: 300_000,
  /** docker run / docker start（鏡像拉取可能更久，超時會給手動 pull 的提示） */
  docker: 60_000,
  /** systemctl start */
  service: 60_000,
  /** 狀態查詢：--version / pip show / docker ps / docker inspect … */
  probe: 10_000,
}

/** 每個 stream 最多留多少字元（避免 pip 输出把記憶體撐爆；留尾部比較有用）。 */
const MAX_CAPTURE = 400_000

function isPlainObject(v) {
  return v != null && typeof v === 'object' && !Array.isArray(v)
}

function cloneJson(v) {
  try {
    return JSON.parse(JSON.stringify(v))
  } catch {
    return v
  }
}

function capText(value) {
  const s = value == null ? '' : String(value)
  if (s.length <= MAX_CAPTURE) return s
  return `…（前面省略 ${s.length - MAX_CAPTURE} 字元）\n${s.slice(-MAX_CAPTURE)}`
}

function linesOf(value) {
  return String(value == null ? '' : value)
    .split(/\r?\n/)
    .filter((line) => line.length > 0)
}

/**
 * 預設的 run 實現：用 `child_process.execFile` 跑一支外部程式。
 * 帶 timeout / maxBuffer / windowsHide；永不 reject（錯誤一律變成 ok:false）。
 */
export function defaultRun(cmd, args = [], opts = {}) {
  const command = String(cmd)
  const argv = Array.isArray(args) ? args.map(String) : []
  const timeoutMs = Number(opts.timeout) > 0 ? Number(opts.timeout) : TIMEOUT.probe
  const startedAt = Date.now()
  return new Promise((resolveResult) => {
    let settled = false
    const finish = (result) => {
      if (settled) return
      settled = true
      resolveResult(result)
    }
    const base = { cmd: command, args: argv, duration_ms: 0 }
    try {
      execFile(
        command,
        argv,
        {
          cwd: opts.cwd || undefined,
          env: opts.env ? { ...process.env, ...opts.env } : process.env,
          timeout: timeoutMs,
          killSignal: 'SIGKILL',
          maxBuffer: Number(opts.maxBuffer) > 0 ? Number(opts.maxBuffer) : 8 * 1024 * 1024,
          windowsHide: true,
          encoding: 'utf8',
        },
        (err, stdout, stderr) => {
          const out = capText(stdout)
          const errText = capText(stderr)
          const timedOut = !!(err && (
            err.killed === true ||
            err.signal === 'SIGKILL' ||
            err.code === 'ETIMEDOUT'
          ))
          let error = ''
          if (err) {
            if (err.code === 'ENOENT') error = `找不到可执行文件：${command}`
            else if (err.code === 'EACCES') error = `没有执行权限：${command}`
            else if (timedOut) error = `超时（${Math.round(timeoutMs / 1000)} 秒）：${command} ${argv.join(' ')}`.trim()
            else error = err.message || String(err)
          }
          if (typeof opts.onLine === 'function') {
            for (const line of linesOf(out)) {
              try { opts.onLine(line, 'stdout') } catch { /* 呼叫端自己的問題，不能影響指令結果 */ }
            }
            for (const line of linesOf(errText)) {
              try { opts.onLine(line, 'stderr') } catch { /* 同上 */ }
            }
          }
          finish({
            ...base,
            ok: !err,
            code: err ? (typeof err.code === 'number' ? err.code : null) : 0,
            stdout: out,
            stderr: errText,
            error,
            timedOut,
            duration_ms: Date.now() - startedAt,
          })
        },
      )
    } catch (e) {
      finish({
        ...base,
        ok: false,
        code: null,
        stdout: '',
        stderr: '',
        error: `执行失败：${e?.message || e}`,
        timedOut: false,
        duration_ms: Date.now() - startedAt,
      })
    }
  })
}

/** 呼叫注入的 run，並把「throw / undefined / 非物件」都收斂成結構化結果。 */
export async function safeRun(run, cmd, args = [], opts = {}) {
  try {
    const r = await run(cmd, args, opts)
    if (r && typeof r === 'object') {
      return {
        ok: r.ok === true,
        code: r.code === undefined ? null : r.code,
        stdout: String(r.stdout == null ? '' : r.stdout),
        stderr: String(r.stderr == null ? '' : r.stderr),
        error: String(r.error == null ? '' : r.error),
        timedOut: r.timedOut === true,
      }
    }
    return { ok: false, code: null, stdout: '', stderr: '', error: 'run 回傳了非物件結果', timedOut: false }
  } catch (e) {
    return { ok: false, code: null, stdout: '', stderr: '', error: `run 抛错：${e?.message || e}`, timedOut: false }
  }
}

/** 版本字串解析（`Python 3.13.5` → `3.13.5`）。 */
const PY_VERSION_RE = /Python\s+(\d+\.\d+(?:\.\d+)?)/i

/**
 * 偵測可用的 Python 3。
 * 依序試：設定裡的 pythonExe（若有）→ Windows: py, python / 其他: python3, python。
 * @returns {Promise<{ok:boolean, version:string|null, exe:string|null, tried:Array, error?:string, hint?:string}>}
 */
export async function detectPython({
  pythonExe,
  run = defaultRun,
  platform = process.platform,
  timeout = TIMEOUT.probe,
} = {}) {
  const defaults = platform === 'win32' ? ['py', 'python'] : ['python3', 'python']
  const candidates = []
  const preferred = pythonExe == null ? '' : String(pythonExe).trim()
  if (preferred) candidates.push(preferred)
  for (const exe of defaults) if (!candidates.includes(exe)) candidates.push(exe)

  const tried = []
  for (const exe of candidates) {
    const r = await safeRun(run, exe, ['--version'], { timeout })
    const text = `${r.stdout}\n${r.stderr}`
    const match = text.match(PY_VERSION_RE)
    if (r.ok && match) {
      return { ok: true, version: match[1], exe, tried, cmd: `${exe} --version` }
    }
    tried.push({
      exe,
      ok: r.ok,
      error: r.error || (r.timedOut ? '逾時' : linesOf(text)[0] || '没有输出'),
    })
  }
  return {
    ok: false,
    version: null,
    exe: null,
    tried,
    error: `找不到可用的 Python 3（试过：${tried.map((t) => t.exe).join(', ')}）`,
    hint: platform === 'win32'
      ? '请安装 Python 3.10+（勾选 py launcher），或把 Config 的 pythonExe 指向 python.exe 的完整路径'
      : '请安装 python3（apt install python3 python3-venv），或把 Config 的 pythonExe 指向实际路径',
  }
}

/** venv 內各執行檔的路徑（Windows: Scripts\*, 其他: bin/*）。 */
export function venvPaths(appDir, platform = process.platform) {
  const dir = join(String(appDir || ''), '.venv')
  if (platform === 'win32') {
    return {
      dir,
      python: join(dir, 'Scripts', 'python.exe'),
      pip: join(dir, 'Scripts', 'pip.exe'),
    }
  }
  return { dir, python: join(dir, 'bin', 'python'), pip: join(dir, 'bin', 'pip') }
}

/** 既有 venv 是否可用（有 python 或有 pyvenv.cfg 就算）。 */
export function venvExists(appDir, platform = process.platform, exists = existsSync) {
  const vp = venvPaths(appDir, platform)
  return exists(vp.dir) && (exists(vp.python) || exists(join(vp.dir, 'pyvenv.cfg')))
}

/**
 * 在 `<appDir>/.venv` 建 venv；已存在就跳過。
 * 失敗（例如系統缺 ensurepip / python3-venv）回 `{ ok:false, hint }`，**不 throw**。
 */
export async function ensureVenv({
  appDir,
  pythonExe,
  run = defaultRun,
  onLog = () => {},
  platform = process.platform,
  exists = existsSync,
  timeout = TIMEOUT.venv,
} = {}) {
  const dir = String(appDir || '')
  if (!dir) return { ok: false, error: '缺少 appDir' }
  const vp = venvPaths(dir, platform)
  const say = (line) => { try { onLog(line) } catch { /* 記錄失敗不影響安裝 */ } }

  if (venvExists(dir, platform, exists)) {
    say(`venv 已存在，跳過建立：${vp.dir}`)
    return { ok: true, skipped: true, dir: vp.dir, python: vp.python, pip: vp.pip }
  }

  const exe = pythonExe == null ? '' : String(pythonExe).trim()
  if (!exe) {
    return {
      ok: false,
      skipped: false,
      dir: vp.dir,
      error: '沒有可用的 Python 可執行檔，無法建立 venv',
      hint: '先安裝 Python 3.10+（Linux 建議 apt install python3 python3-venv）',
    }
  }

  say(`建立 venv：${exe} -m venv .venv（cwd=${dir}）`)
  const r = await safeRun(run, exe, ['-m', 'venv', '.venv'], { cwd: dir, timeout, onLine: say })

  if (!r.ok || !exists(vp.python)) {
    const blob = `${r.stdout}\n${r.stderr}\n${r.error}`
    const missingVenv = /ensurepip|No module named venv|python3-venv|venv is not available/i.test(blob)
    const hint = missingVenv
      ? '系統缺少 venv/ensurepip：請安裝 python3-venv（Debian/Ubuntu：apt install python3-venv；Fedora：dnf install python3-pip），或改用系統 pip'
      : '建立 venv 失敗：請確認 Python 安裝完整（含 venv/ensurepip）；也可以放棄 venv，改用系統 pip 安裝（把 pythonExe 指向系統 python 並手動 pip install -r requirements.txt）'
    say(`建立 venv 失敗：${r.error || r.stderr || r.stdout || '未知原因'}`)
    return {
      ok: false,
      skipped: false,
      dir: vp.dir,
      python: vp.python,
      pip: vp.pip,
      error: r.error || (missingVenv ? '缺少 venv 模組' : 'venv 沒有產生 python 執行檔'),
      hint,
      timedOut: r.timedOut,
      output: linesOf(`${r.stdout}\n${r.stderr}`).slice(-20),
    }
  }

  say(`venv 建立完成：${vp.python}`)
  return { ok: true, skipped: false, created: true, dir: vp.dir, python: vp.python, pip: vp.pip }
}

/** 從 `pip show` 的輸出抓套件名（小寫）。 */
export function parsePipShowNames(stdout) {
  const names = []
  const re = /^Name:\s*(\S+)/gim
  let m
  while ((m = re.exec(String(stdout || ''))) !== null) names.push(String(m[1]).toLowerCase())
  return names
}

/** 用 venv 的 pip（沒有 pip.exe 就 `python -m pip`）查 pillow / websockets 是否已裝。 */
async function probePipPackages({ appDir, run, platform, exists, timeout = TIMEOUT.probe }) {
  const vp = venvPaths(appDir, platform)
  if (!exists(vp.python)) {
    return { checked: false, pillow: false, websockets: false, names: [] }
  }
  const pipCmd = exists(vp.pip) ? [vp.pip] : [vp.python, '-m', 'pip']
  const r = await safeRun(run, pipCmd[0], [...pipCmd.slice(1), 'show', 'pillow', 'websockets'], { timeout })
  const names = parsePipShowNames(r.stdout)
  return {
    checked: true,
    pillow: names.includes('pillow'),
    websockets: names.includes('websockets'),
    names,
  }
}

/**
 * 用 venv 的 pip 裝 `app/requirements.txt`；pillow + websockets 都已裝就跳過。
 * 一律加 `--no-input --disable-pip-version-check`，輸出逐行 onLog()。
 */
export async function ensureDeps({
  appDir,
  run = defaultRun,
  onLog = () => {},
  platform = process.platform,
  exists = existsSync,
  force = false,
  venvPip,
  timeout = TIMEOUT.pip,
} = {}) {
  const dir = String(appDir || '')
  if (!dir) return { ok: false, error: '缺少 appDir' }
  const say = (line) => { try { onLog(line) } catch { /* ignore */ } }
  const vp = venvPaths(dir, platform)

  if (!exists(vp.python)) {
    return {
      ok: false,
      error: 'venv 不存在，無法安裝依賴',
      hint: '先跑「環境安裝」建立 app/.venv（Python 3.10+）',
      deps: { pillow: false, websockets: false },
    }
  }

  const requirements = join(dir, 'requirements.txt')
  if (!exists(requirements)) {
    return {
      ok: false,
      error: `requirements.txt 不存在：${requirements}`,
      hint: 'PawBot 源碼不完整（app/ 由安裝包提供，應含 requirements.txt）',
      deps: { pillow: false, websockets: false },
    }
  }

  const pipExe = venvPip && exists(String(venvPip)) ? String(venvPip) : ''
  const pipCmd = pipExe ? [pipExe] : (exists(vp.pip) ? [vp.pip] : [vp.python, '-m', 'pip'])

  if (!force) {
    const probe = await probePipPackages({ appDir: dir, run, platform, exists })
    if (probe.checked && probe.pillow && probe.websockets) {
      say('依赖已安装（pillow / websockets 都在），跳過 pip install')
      return {
        ok: true,
        skipped: true,
        deps: { pillow: true, websockets: true },
        python: vp.python,
        pip: pipCmd[0],
      }
    }
  }

  say(`安装依赖：${pipCmd.join(' ')} install -r requirements.txt（可能要几分钟）`)
  const r = await safeRun(
    run,
    pipCmd[0],
    [...pipCmd.slice(1), 'install', '-r', requirements, '--no-input', '--disable-pip-version-check'],
    { cwd: dir, timeout, onLine: say },
  )

  if (!r.ok) {
    const tail = linesOf(`${r.stdout}\n${r.stderr}`).slice(-20)
    const hint = r.timedOut
      ? `pip install 超时（${Math.round(timeout / 1000)} 秒）：请检查网络 / 镜像源后重试`
      : 'pip 安装失败：请看 log 尾部的错误；常见原因是网络、缺少编译工具、requirements.txt 写错'
    say(`安装依赖失败：${r.error || '见上面的 pip 输出'}`)
    return {
      ok: false,
      error: r.error || 'pip install 失败',
      hint,
      timedOut: r.timedOut,
      output: tail,
      deps: { pillow: false, websockets: false },
    }
  }

  // 不重跑 probe（多一次 pip show 會慢）；pip exit 0 就認為裝完。
  say('依赖安装完成')
  return {
    ok: true,
    skipped: false,
    deps: { pillow: true, websockets: true },
    python: vp.python,
    pip: pipCmd[0],
    output: linesOf(`${r.stdout}\n${r.stderr}`).slice(-20),
  }
}

/** 沒有 .example 時寫出的最小可用 PawBot config（只補缺、不覆蓋既有值）。 */
export const MINIMAL_CONFIG = {
  llm: {
    base_url: 'https://api.deepseek.com',
    api_key: '',
    model: 'deepseek-chat',
    temperature: 0.8,
    max_tokens: 2000,
    timeout: 60,
  },
  memory: {
    engine: 'builtin',
    total_chars: 12000,
    l0_hours: 12,
  },
  tools: {
    enabled: true,
    max_rounds: 5,
  },
  server: {
    host: '0.0.0.0',
    port: 6199,
    path: '/ws',
    access_token: '',
  },
  webui: {
    host: '127.0.0.1',
    port: 6185,
    token: '',
  },
}

/** 補齊缺的鍵（既有值一律不動；值型別不對的區段視為缺）。回傳是否有改動。 */
function fillMissing(target, defaults, added, prefix = '') {
  let changed = false
  for (const [key, defValue] of Object.entries(defaults)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (!(key in target)) {
      target[key] = cloneJson(defValue)
      added.push(path)
      changed = true
      continue
    }
    if (isPlainObject(defValue)) {
      if (!isPlainObject(target[key])) {
        target[key] = cloneJson(defValue)
        added.push(path)
        changed = true
      } else if (fillMissing(target[key], defValue, added, path)) {
        changed = true
      }
    }
  }
  return changed
}

function writeJsonAtomic(file, value) {
  const text = `${JSON.stringify(value, null, 2)}\n`
  const tmp = `${file}.tmp-${process.pid}-${Date.now()}`
  writeFileSync(tmp, text, 'utf8')
  try {
    renameSync(tmp, file)
  } catch {
    // 跨裝置 / Windows 被佔用：退回直接寫
    writeFileSync(file, text, 'utf8')
  } finally {
    try { if (existsSync(tmp)) unlinkSync(tmp) } catch { /* 清不掉就算了 */ }
  }
}

/** 檢查 config 是否有關鍵區段（llm / memory / tools）。 */
export function validateConfigObject(config) {
  const required = ['llm', 'memory', 'tools']
  const missing = required.filter((key) => !isPlainObject(config?.[key]))
  return { valid: missing.length === 0, missing }
}

/**
 * 準備 `app/data/config.json`：
 *   - 不存在 → 從 `config.json.example` 複製；沒有 .example 就寫最小模板（再補齊）。
 *   - 存在但缺 llm / memory / tools → 補預設，**不覆蓋任何既有值**。
 *   - 一律確保 `data/{people,diary,memory}` 存在。
 *   - onebotToken 非空 → 寫進 `server.access_token`（與 NapCat 的 token 保持一致）。
 */
export function ensureConfig({ appDir, onebotToken = '', exists = existsSync } = {}) {
  const dir = String(appDir || '')
  if (!dir) return { ok: false, error: '缺少 appDir' }
  const dataDir = join(dir, 'data')
  const configPath = join(dataDir, 'config.json')
  const examplePath = join(dataDir, 'config.json.example')
  const dirs = ['people', 'diary', 'memory'].map((name) => join(dataDir, name))

  try {
    mkdirSync(dataDir, { recursive: true })
    for (const d of dirs) mkdirSync(d, { recursive: true })
  } catch (e) {
    return { ok: false, error: `建立 data 目录失败：${e?.message || e}`, data_dir: dataDir }
  }

  let created = false
  let fromExample = false
  try {
    if (!exists(configPath)) {
      if (exists(examplePath)) {
        copyFileSync(examplePath, configPath)
        fromExample = true
      } else {
        writeJsonAtomic(configPath, MINIMAL_CONFIG)
      }
      created = true
    }
  } catch (e) {
    return { ok: false, error: `建立 config.json 失败：${e?.message || e}`, config_path: configPath, data_dir: dataDir }
  }

  let config
  try {
    config = JSON.parse(readFileSync(configPath, 'utf8'))
  } catch (e) {
    return {
      ok: false,
      config_path: configPath,
      data_dir: dataDir,
      created,
      from_example: fromExample,
      error: `config.json 不是合法 JSON：${e?.message || e}`,
      hint: '请先修好（或移走）这个文件；外挂不会擅自覆盖坏掉的设定',
    }
  }
  if (!isPlainObject(config)) {
    return {
      ok: false,
      config_path: configPath,
      data_dir: dataDir,
      created,
      from_example: fromExample,
      error: 'config.json 顶层不是 JSON 对象',
      hint: '请先修好（或移走）这个文件；外挂不会擅自覆盖坏掉的设定',
    }
  }

  const before = JSON.stringify(config)
  const added = []
  fillMissing(config, MINIMAL_CONFIG, added)

  let tokenUpdated = false
  if (onebotToken) {
    if (!isPlainObject(config.server)) config.server = {}
    if (config.server.access_token !== onebotToken) {
      config.server.access_token = onebotToken
      tokenUpdated = true
    }
  }

  const changed = JSON.stringify(config) !== before
  if (changed) {
    try {
      writeJsonAtomic(configPath, config)
    } catch (e) {
      return {
        ok: false,
        error: `写 config.json 失败：${e?.message || e}`,
        added,
        token_updated: tokenUpdated,
        config_path: configPath,
        data_dir: dataDir,
      }
    }
  }

  return {
    ok: true,
    created,
    from_example: fromExample,
    changed,
    added,
    token_updated: tokenUpdated,
    dirs,
    config_path: configPath,
    data_dir: dataDir,
  }
}

/**
 * 「環境」這一步的完整流程：偵測 Python → venv → 依賴 → config。
 * 任何一步失敗都回結構化結果（含 phase / hint），不 throw。
 */
export async function runEnvSetup({
  appDir,
  pythonExe,
  run = defaultRun,
  onLog = () => {},
  onProgress = () => {},
  platform = process.platform,
  onebotToken = '',
  force = false,
  exists = existsSync,
} = {}) {
  const dir = String(appDir || '')
  const steps = []
  const add = (id, name, state, detail) => steps.push({ id, name, state, detail: String(detail || '') })
  const say = (line) => { try { onLog(line) } catch { /* ignore */ } }
  const progress = (pct, message) => {
    try { onProgress(pct, message) } catch { /* ignore */ }
    if (message) say(message)
  }

  if (!dir) {
    return { ok: false, phase: 'no-app-dir', error: '缺少 appDir', hint: 'Config 的 appDir 没有设定', steps }
  }
  if (!exists(join(dir, 'run.py'))) {
    const msg = `PawBot 源碼不在 ${dir}（找不到 run.py）`
    add('app', 'PawBot 源碼', 'fail', msg)
    say(msg)
    return {
      ok: false,
      phase: 'no-app',
      error: msg,
      hint: 'app/ 由安裝包提供（應含 run.py、requirements.txt、data/）；请确认 appDir 设定',
      steps,
    }
  }
  add('app', 'PawBot 源碼', 'ok', `run.py 存在（${dir}）`)

  progress(10, '检测 Python……')
  const python = await detectPython({ pythonExe, run, platform })
  if (!python.ok) {
    add('python', 'Python', 'fail', python.error || '找不到 Python 3')
    say(`检测 Python 失败：${python.error || ''}`)
    return { ok: false, phase: 'no-python', error: python.error, hint: python.hint, python, steps }
  }
  add('python', 'Python', 'ok', `${python.exe} → Python ${python.version}`)
  say(`找到 Python ${python.version}（${python.exe}）`)

  progress(25, `建立 venv（${python.exe} -m venv .venv）……`)
  const venv = await ensureVenv({ appDir: dir, pythonExe: python.exe, run, onLog: say, platform, exists })
  if (!venv.ok) {
    add('venv', 'venv', 'fail', venv.error || '建立 venv 失败')
    return { ok: false, phase: 'venv-failed', error: venv.error, hint: venv.hint, python, venv, steps }
  }
  add('venv', 'venv', 'ok', venv.skipped ? '已存在（跳过建立）' : `已建立：${venv.python}`)

  progress(50, '安装依赖（pip install，可能要几分钟）……')
  const deps = await ensureDeps({ appDir: dir, run, onLog: say, platform, exists, force, venvPip: venv.pip })
  if (!deps.ok) {
    add('deps', '依赖', 'fail', deps.error || '安装依赖失败')
    return { ok: false, phase: 'deps-failed', error: deps.error, hint: deps.hint, python, venv, deps, steps }
  }
  add('deps', '依赖', 'ok', deps.skipped ? 'pillow / websockets 已安装（跳过）' : 'requirements.txt 安装完成')

  progress(85, '准备 config.json 与 data 目录……')
  const config = ensureConfig({ appDir: dir, onebotToken, exists })
  if (!config.ok) {
    add('config', '配置', 'fail', config.error || '准备配置失败')
    return { ok: false, phase: 'config-failed', error: config.error, hint: config.hint, python, venv, deps, config, steps }
  }
  add(
    'config',
    '配置',
    'ok',
    config.created
      ? (config.from_example ? '已从 config.json.example 建立' : '已建立最小 config.json')
      : (config.changed ? `已补齐：${config.added.join(', ') || 'server.access_token'}` : '已存在（无改动）'),
  )

  progress(100, '环境就绪')
  return {
    ok: true,
    phase: 'ready',
    app_dir: dir,
    python,
    venv,
    deps,
    config,
    steps,
  }
}

/**
 * 唯讀自檢（給 GET /pawbot-ui/setup/status 用）。
 * 不建立任何東西；只跑 `--version` / `pip show` 這類查詢（各有 timeout）。
 */
export async function setupState({
  appDir,
  pythonExe,
  run = defaultRun,
  platform = process.platform,
  onebotToken = '',
  logs = [],
  boot = null,
  exists = existsSync,
} = {}) {
  const dir = String(appDir || '')
  const appPresent = !!dir && exists(join(dir, 'run.py'))
  const python = await detectPython({ pythonExe, run, platform })
  const vp = venvPaths(dir, platform)
  const hasVenv = !!dir && venvExists(dir, platform, exists)
  const probe = hasVenv
    ? await probePipPackages({ appDir: dir, run, platform, exists })
    : { checked: false, pillow: false, websockets: false, names: [] }
  const deps = {
    pillow: probe.pillow,
    websockets: probe.websockets,
    ok: !!(probe.checked && probe.pillow && probe.websockets),
    checked: probe.checked,
  }

  const configPath = join(dir, 'data', 'config.json')
  const configExists = !!dir && exists(configPath)
  let configValid = false
  let configMissing = ['llm', 'memory', 'tools']
  let configError = ''
  if (configExists) {
    try {
      const parsed = JSON.parse(readFileSync(configPath, 'utf8'))
      const v = validateConfigObject(parsed)
      configValid = v.valid
      configMissing = v.missing
    } catch (e) {
      configError = `config.json 不是合法 JSON：${e?.message || e}`
    }
  }

  let phase
  let progressValue
  let message
  if (!appPresent) {
    phase = 'no-app'
    progressValue = 0
    message = `PawBot 源碼不在 ${dir || '（未設定 appDir）'}（找不到 run.py）`
  } else if (!python.ok) {
    phase = 'no-python'
    progressValue = 5
    message = '找不到可用的 Python 3'
  } else if (!hasVenv) {
    phase = 'need-venv'
    progressValue = 20
    message = '尚未建立 venv（app/.venv）'
  } else if (!deps.ok) {
    phase = 'need-deps'
    progressValue = 45
    message = '依赖尚未安装完成（pillow / websockets）'
  } else if (!configExists || !configValid) {
    phase = 'need-config'
    progressValue = 75
    message = configError || `config.json 缺少关键区段：${configMissing.join(', ')}`
  } else {
    phase = 'ready'
    progressValue = 100
    message = '环境就绪'
  }

  // 一鍵流程進行中時，phase/progress 以流程為準（面板輪詢才看得到進展）
  if (boot && typeof boot === 'object' && boot.running) {
    phase = boot.phase || 'running'
    progressValue = Number.isFinite(boot.progress) ? boot.progress : progressValue
    message = boot.message || message
  }

  // log 只回最後 60 行。元素一律是 `{ stream, line }`：
  // 面板的日誌窗是按物件讀 `entry.line`（字串會被丟掉、顯示成「还没有日志」）。
  const logTail = (Array.isArray(logs) ? logs : []).slice(-60).map((entry) => {
    if (entry && typeof entry === 'object' && entry.line !== undefined && entry.line !== null) {
      return { stream: String(entry.stream || 'stdout'), line: String(entry.line) }
    }
    return { stream: 'stdout', line: String(entry == null ? '' : entry) }
  })

  return {
    appDir: dir,
    app_dir: dir,
    appPresent,
    app_present: appPresent,
    python,
    venv: {
      exists: hasVenv,
      path: vp.dir,
      python: vp.python,
      pip: vp.pip,
    },
    deps,
    config: {
      exists: configExists,
      valid: configValid,
      path: configPath,
      missing: configMissing,
      error: configError || null,
    },
    phase,
    progress: progressValue,
    message,
    log: logTail,
    // 只回「有沒有設」不回路徑以外的東西；token 本身絕不出現在這裡
    onebot_token_set: !!String(onebotToken || ''),
  }
}

/* ────────────────────────────────────────────────────────────────────────────
 * 「一鍵」四步狀態機（env → napcat → pawbot → ready）
 * ──────────────────────────────────────────────────────────────────────────── */

/** 一鍵流程固定四步（順序即執行順序）。 */
export const BOOT_STEPS = [
  { id: 'env', name: '环境准备（Python / venv / 依赖 / 配置）' },
  { id: 'napcat', name: 'NapCat（检测 / 写 OneBot 配置 / 启动）' },
  { id: 'pawbot', name: '启动 PawBot 子进程' },
  { id: 'ready', name: '等待 PawBot /api/status 可达' },
]

const TERMINAL = new Set(['ok', 'fail', 'skip'])

function stepDetail(value, max = 500) {
  const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/**
 * 建一個一鍵流程編排器。
 *
 * deps = {
 *   env:    async ({ force }) => ({ ok, detail?, hint?, error? }),
 *   napcat: async ({ force }) => ({ ok, skip?, detail?, hint?, manual?, mode? }),
 *   pawbot: async ({ force }) => ({ ok, detail?, error? }),
 *   ready:  async ({ force }) => ({ ok, detail?, error? }),
 * }
 * 回傳 { start, status, waitForDone }；start 非阻塞，狀態用 status() 輪詢。
 */
export function createBootOrchestrator({ env, napcat, pawbot, ready, log = () => {}, now = Date.now } = {}) {
  const deps = { env, napcat, pawbot, ready }
  let state = freshState()
  let promise = null
  let session = 0

  function freshState() {
    return {
      running: false,
      phase: 'idle',
      progress: 0,
      message: '尚未开始',
      started_at: 0,
      finished_at: 0,
      error: '',
      steps: BOOT_STEPS.map((step) => ({ id: step.id, name: step.name, state: 'pending', detail: '' })),
    }
  }

  function recalc() {
    const terminalCount = state.steps.filter((step) => TERMINAL.has(step.state)).length
    const hasRunning = state.steps.some((step) => step.state === 'running')
    const allDone = terminalCount === state.steps.length
    state.progress = allDone ? 100 : Math.min(95, terminalCount * 25 + (hasRunning ? 5 : 0))
  }

  function snapshot() {
    const steps = state.steps.map((step) => ({ ...step }))
    const done = !state.running && (state.phase === 'done' || state.phase === 'failed')
    return {
      ok: true,
      running: state.running,
      phase: state.phase,
      progress: state.progress,
      message: state.message,
      started_at: state.started_at || null,
      finished_at: state.finished_at || null,
      done,
      error: state.error || null,
      steps,
    }
  }

  async function runStep(step, ctx) {
    step.state = 'running'
    step.detail = ''
    recalc()
    let result
    try {
      const fn = deps[step.id]
      result = typeof fn === 'function' ? await fn(ctx) : { ok: false, error: `没有注入 ${step.id} 步骤` }
    } catch (e) {
      result = { ok: false, error: `步骤抛错：${e?.message || e}` }
    }
    const r = result && typeof result === 'object' ? result : {}
    if (r.skip === true || r.skipped === true) {
      step.state = 'skip'
      step.detail = stepDetail(r.detail || r.hint || r.message || '已跳过')
    } else if (r.ok === false) {
      step.state = 'fail'
      step.detail = stepDetail(r.detail || r.hint || r.error || '失败')
    } else {
      step.state = 'ok'
      step.detail = stepDetail(r.detail || r.message || '完成')
    }
    recalc()
    return r
  }

  async function execute(currentSession, force) {
    const ctx = { force: !!force }
    try {
      const envRes = await runStep(state.steps[0], ctx)
      const envFailed = state.steps[0].state === 'fail'
      if (envFailed && !state.error) state.error = envRes?.hint || envRes?.error || '环境准备失败'

      if (envFailed) {
        for (const step of state.steps.slice(1)) {
          step.state = 'skip'
          step.detail = '前一步（env）失败，已跳过'
        }
      } else {
        // NapCat 失败不阻止 PawBot 启动（没有 QQ 连接也能先把机器跑起来）
        await runStep(state.steps[1], ctx)
        await runStep(state.steps[2], ctx)
        if (state.steps[2].state === 'fail') {
          if (!state.error) state.error = state.steps[2].detail || 'PawBot 启动失败'
          state.steps[3].state = 'skip'
          state.steps[3].detail = 'PawBot 未启动，已跳过'
        } else {
          const readyRes = await runStep(state.steps[3], ctx)
          if (state.steps[3].state === 'fail' && !state.error) {
            state.error = readyRes?.hint || readyRes?.error || state.steps[3].detail || '等待就绪失败'
          }
        }
      }
    } catch (e) {
      if (!state.error) state.error = `一键流程异常：${e?.message || e}`
    } finally {
      if (currentSession === session) {
        recalc()
        state.running = false
        state.finished_at = now()
        const failed = state.steps.some((step) => step.state === 'fail')
        state.phase = failed ? 'failed' : 'done'
        state.progress = 100
        state.message = failed
          ? `一键流程结束（有失败步骤）：${state.error || '请看 steps'}`
          : '一键完成：PawBot 已就绪'
        log(state.message)
      }
    }
  }

  const start = ({ force = false } = {}) => {
    if (state.running) {
      return { ok: true, started: false, already_running: true, status: snapshot() }
    }
    session += 1
    const currentSession = session
    state = {
      ...freshState(),
      running: true,
      phase: 'running',
      progress: 0,
      message: '一键开始：env → napcat → pawbot → ready',
      started_at: now(),
    }
    log('一键流程开始（env → napcat → pawbot → ready）')
    promise = execute(currentSession, force === true)
    return { ok: true, started: true, status: snapshot() }
  }

  const status = () => snapshot()

  const waitForDone = async (timeoutMs = 120_000) => {
    const deadline = now() + Math.max(0, Number(timeoutMs) || 0)
    while (state.running && now() < deadline) {
      // eslint-disable-next-line no-await-in-loop
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    return snapshot()
  }

  return { start, status, waitForDone, get promise() { return promise } }
}

/**
 * 「只跑環境安裝」的非阻塞 job（POST /pawbot-ui/setup/run 用）。
 * runEnv({ force, onProgress }) 由呼叫端注入（通常是 runEnvSetup 的封裝）。
 */
export function createSetupJob({ runEnv, log = () => {}, now = Date.now } = {}) {
  let state = {
    running: false,
    started_at: 0,
    finished_at: 0,
    progress: 0,
    message: '尚未开始',
    result: null,
    error: '',
  }

  const status = () => ({
    running: state.running,
    started_at: state.started_at || null,
    finished_at: state.finished_at || null,
    progress: state.progress,
    message: state.message,
    result: state.result,
    error: state.error || null,
  })

  const start = ({ force = false } = {}) => {
    if (state.running) {
      return { ok: true, started: false, already_running: true, progress: state.progress, message: state.message }
    }
    state = {
      running: true,
      started_at: now(),
      finished_at: 0,
      progress: 5,
      message: '开始安装……',
      result: null,
      error: '',
    }
    const onProgress = (pct, message) => {
      if (!state.running) return
      const value = Number(pct)
      if (Number.isFinite(value)) state.progress = Math.max(0, Math.min(100, Math.round(value)))
      if (message) state.message = String(message)
    }
    const runner = (async () => {
      try {
        const result = await runEnv({ force: force === true, onProgress })
        state.result = result && typeof result === 'object' ? result : { ok: false, error: '安装步骤没有回传结果' }
        if (!state.result.ok) state.error = state.result.hint || state.result.error || '安装失败'
        else state.progress = 100
      } catch (e) {
        state.error = `安装异常：${e?.message || e}`
      } finally {
        state.running = false
        state.finished_at = now()
        state.message = state.error ? `安装失败：${state.error}` : '安装完成'
        log(state.message)
      }
    })()
    return { ok: true, started: true, promise: runner }
  }

  const waitForDone = async (timeoutMs = 120_000) => {
    const deadline = now() + Math.max(0, Number(timeoutMs) || 0)
    while (state.running && now() < deadline) {
      // eslint-disable-next-line no-await-in-loop
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    return status()
  }

  return { start, status, waitForDone }
}
