/**
 * test/setup-offline.mjs —— 一鍵安裝 + NapCat 編排的離線單測（不建 venv、不裝 pip、不碰 docker）。
 * 用法：`node test/setup-offline.mjs`
 * 全綠 → exit 0；有紅 → exit 1。
 *
 * 覆蓋：
 *   detectPython（成功 / 第一個失敗第二個成功 / 全失敗 / 指定 pythonExe）
 *   ensureVenv（已存在跳過 / 缺 ensurepip 不 throw）
 *   ensureDeps（已裝跳過 / 安裝成功）
 *   ensureConfig（補齊不覆蓋 / 從 .example 複製 / 最小模板 / 壞 JSON 不動）
 *   writeOnebotConfig（JSON 形狀 + 備份 + 檔名）
 *   readWebui（日誌正則 + webui.json）、qrFromLog、loggedInByPawbot / isLoggedIn
 *   detectNapcat / detectMode（docker / binary / none）
 *   startNapcat（docker run / docker start 冪等 / none 給手動步驟）
 *   runEnvSetup 完整流程（假 run 建出假 venv）、setupState 各 phase
 *   boot 四步狀態機（成功 / napcat skip / env 失敗全 skip / 重複 start 不重跑）
 *   createSetupJob 非阻塞 + 進度
 *   /pawbot-ui 新路由的接線（注入假 deps，含 CSRF content-type 檢查與 501 降級）
 *
 * ⚠️ 這裡全部是假 run / 假 deps；docker、systemd、NapCat 實機掃碼都沒有驗證。
 */
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  createBootOrchestrator,
  createSetupJob,
  detectPython,
  ensureConfig,
  ensureDeps,
  ensureVenv,
  runEnvSetup,
  setupState,
} from '../lib/setup.js'
import {
  NAPCAT_CONTAINER,
  NAPCAT_IMAGE,
  detectMode,
  detectNapcat,
  isLoggedIn,
  loggedInByPawbot,
  manualGuide,
  napcatConfigDir,
  qrFromLog,
  readWebui,
  startNapcat,
  writeOnebotConfig,
} from '../lib/napcat.js'
import { createPawbotProxy } from '../lib/webui-proxy.js'

let passed = 0
let failed = 0
async function t(name, fn) {
  try {
    await fn()
    passed += 1
    console.log(`  ✔ ${name}`)
  } catch (e) {
    failed += 1
    console.log(`  ✘ ${name}`)
    const msg = String((e && e.message) || e).split('\n').join('\n      ')
    console.log(`      ${msg}`)
  }
}

/** 假 run：handler 回傳 `{ ok, code, stdout, stderr, error }`；undefined 視為 unexpected。 */
function fakeRun(handler) {
  const calls = []
  const run = async (cmd, args = [], opts = {}) => {
    const call = { cmd, args: [...args], opts }
    calls.push(call)
    const r = (await handler(cmd, args, opts)) ?? { ok: false, error: `unexpected: ${cmd} ${args.join(' ')}` }
    const result = {
      ok: r.ok === true,
      code: r.code === undefined ? (r.ok === true ? 0 : null) : r.code,
      stdout: String(r.stdout == null ? '' : r.stdout),
      stderr: String(r.stderr == null ? '' : r.stderr),
      error: String(r.error == null ? '' : r.error),
      timedOut: r.timedOut === true,
    }
    // 模擬 defaultRun 的 onLine 契約：假 run 也要逐行回呼，否則「輸出逐行 onLog」測不到
    if (typeof opts.onLine === 'function') {
      for (const line of result.stdout.split(/\r?\n/).filter(Boolean)) opts.onLine(line, 'stdout')
      for (const line of result.stderr.split(/\r?\n/).filter(Boolean)) opts.onLine(line, 'stderr')
    }
    return result
  }
  run.calls = calls
  return run
}

const tmpRoots = []
function tempDir(label) {
  const dir = mkdtempSync(join(tmpdir(), `pawbot-${label}-`))
  tmpRoots.push(dir)
  return dir
}

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('== detectPython（假 run） ==')

await t('py 成功 → {ok:true, version, exe:"py"}，不再試 python', async () => {
  const run = fakeRun((cmd) => {
    if (cmd === 'py') return { ok: true, stdout: 'Python 3.13.5\r\n' }
    return { ok: true, stdout: 'Python 3.11.9' }
  })
  const r = await detectPython({ run, platform: 'win32' })
  assert.equal(r.ok, true)
  assert.equal(r.version, '3.13.5')
  assert.equal(r.exe, 'py')
  assert.equal(run.calls.length, 1, 'py 成功就不該再試 python')
})

await t('py 失敗、python 成功 → 改用 python，tried 記錄 py', async () => {
  const run = fakeRun((cmd) => {
    if (cmd === 'py') return { ok: false, error: '找不到可执行文件：py' }
    return { ok: true, stdout: 'Python 3.12.4' }
  })
  const r = await detectPython({ run, platform: 'win32' })
  assert.equal(r.ok, true)
  assert.equal(r.exe, 'python')
  assert.equal(r.version, '3.12.4')
  assert.equal(r.tried.length, 1)
  assert.equal(r.tried[0].exe, 'py')
})

await t('全部失敗 → ok:false，tried 兩個，hint 不為空', async () => {
  const run = fakeRun(() => ({ ok: false, stderr: 'Python was not found' }))
  const r = await detectPython({ run, platform: 'win32' })
  assert.equal(r.ok, false)
  assert.equal(r.exe, null)
  assert.equal(r.version, null)
  assert.equal(r.tried.length, 2)
  assert.ok(r.hint && r.hint.length > 0)
  assert.match(r.error, /py, python/)
})

await t('Linux 依序試 python3 → python', async () => {
  const run = fakeRun((cmd) => (cmd === 'python3' ? { ok: true, stdout: 'Python 3.11.2' } : { ok: false }))
  const r = await detectPython({ run, platform: 'linux' })
  assert.equal(r.ok, true)
  assert.equal(r.exe, 'python3')
  assert.equal(run.calls[0].cmd, 'python3')
})

await t('設定 pythonExe 優先試，且可用完整路徑', async () => {
  const exe = 'C:/Python313/python.exe'
  const run = fakeRun((cmd) => (cmd === exe ? { ok: true, stdout: 'Python 3.13.1' } : { ok: false }))
  const r = await detectPython({ run, platform: 'win32', pythonExe: exe })
  assert.equal(r.ok, true)
  assert.equal(r.exe, exe)
  assert.equal(run.calls.length, 1)
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== ensureVenv（假 run + 假檔案系統目錄） ==')

await t('venv 已存在 → 跳過建立，完全不跑指令', async () => {
  const appDir = tempDir('venv-exists')
  mkdirSync(join(appDir, '.venv', 'bin'), { recursive: true })
  writeFileSync(join(appDir, '.venv', 'bin', 'python'), '')
  const run = fakeRun(() => ({ ok: true }))
  const r = await ensureVenv({ appDir, run, platform: 'linux', pythonExe: 'python3' })
  assert.equal(r.ok, true)
  assert.equal(r.skipped, true)
  assert.equal(run.calls.length, 0)
  assert.ok(r.python.endsWith(join('.venv', 'bin', 'python')))
})

await t('缺 ensurepip → ok:false + hint 提到 python3-venv，不 throw', async () => {
  const appDir = tempDir('venv-fail')
  const run = fakeRun(() => ({
    ok: false,
    stderr: 'Error: Command ... ensurepip is not available. On Debian/Ubuntu systems, you need to install the python3-venv package.',
  }))
  const r = await ensureVenv({ appDir, run, platform: 'linux', pythonExe: 'python3' })
  assert.equal(r.ok, false)
  assert.equal(r.skipped, false)
  assert.match(r.hint, /python3-venv/)
})

await t('run 直接 throw 也不會讓 ensureVenv 抛出去', async () => {
  const appDir = tempDir('venv-throw')
  const run = async () => {
    throw new Error('boom')
  }
  const r = await ensureVenv({ appDir, run, platform: 'linux', pythonExe: 'python3' })
  assert.equal(r.ok, false)
  assert.match(r.error, /boom|run 抛错/)
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== ensureDeps（假 run） ==')

function makeVenv(appDir) {
  mkdirSync(join(appDir, '.venv', 'bin'), { recursive: true })
  writeFileSync(join(appDir, '.venv', 'bin', 'python'), '')
  writeFileSync(join(appDir, 'requirements.txt'), 'pillow>=10.0\nwebsockets>=12.0\n')
}

await t('pillow + websockets 都已裝 → 跳過 pip install', async () => {
  const appDir = tempDir('deps-skip')
  makeVenv(appDir)
  const run = fakeRun((cmd, args) => {
    if (args.includes('show')) {
      return { ok: true, stdout: 'Name: pillow\nVersion: 10.0.0\n---\nName: websockets\nVersion: 12.0\n' }
    }
    return { ok: false, error: '不該跑到這裡' }
  })
  const r = await ensureDeps({ appDir, run, platform: 'linux' })
  assert.equal(r.ok, true)
  assert.equal(r.skipped, true)
  assert.deepEqual(r.deps, { pillow: true, websockets: true })
  assert.ok(!run.calls.some((c) => c.args.includes('install')), '已裝就不該 pip install')
})

await t('缺套件 → pip install -r requirements.txt --no-input --disable-pip-version-check', async () => {
  const appDir = tempDir('deps-install')
  makeVenv(appDir)
  const run = fakeRun((cmd, args) => {
    if (args.includes('show')) return { ok: true, stdout: 'Name: pillow\nVersion: 10.0.0\n' }
    if (args.includes('install')) return { ok: true, stdout: 'Successfully installed websockets-12.0\n' }
    return { ok: false, error: 'unexpected' }
  })
  const logs = []
  const r = await ensureDeps({ appDir, run, platform: 'linux', onLog: (line) => logs.push(line) })
  assert.equal(r.ok, true)
  assert.equal(r.skipped, false)
  const install = run.calls.find((c) => c.args.includes('install'))
  assert.ok(install, '應該要跑 pip install')
  assert.ok(install.args.includes('-r'))
  assert.ok(install.args.includes('--no-input'))
  assert.ok(install.args.includes('--disable-pip-version-check'))
  assert.equal(install.opts.cwd, appDir)
  assert.ok(logs.some((line) => /安装依赖/.test(line)), 'onLog 要收到安装訊息')
  assert.ok(logs.some((line) => /Successfully installed/.test(line)), 'pip 輸出要逐行 onLog')
})

await t('requirements.txt 不存在 → 結構化錯誤，不跑 pip', async () => {
  const appDir = tempDir('deps-noreq')
  mkdirSync(join(appDir, '.venv', 'bin'), { recursive: true })
  writeFileSync(join(appDir, '.venv', 'bin', 'python'), '')
  const run = fakeRun(() => ({ ok: true }))
  const r = await ensureDeps({ appDir, run, platform: 'linux' })
  assert.equal(r.ok, false)
  assert.match(r.error, /requirements\.txt 不存在/)
  assert.equal(run.calls.length, 0)
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== ensureConfig（真實檔案系統的暫存目錄） ==')

await t('既有 config 缺 memory/tools → 補齊但不覆蓋既有值', () => {
  const appDir = tempDir('cfg-fill')
  mkdirSync(join(appDir, 'data'), { recursive: true })
  const original = {
    llm: { api_key: 'sk-keep-me', model: 'my-model', temperature: 0.3 },
    server: { port: 7000, access_token: 'keep-token' },
  }
  writeFileSync(join(appDir, 'data', 'config.json'), JSON.stringify(original, null, 2))
  const r = ensureConfig({ appDir })
  assert.equal(r.ok, true)
  assert.equal(r.created, false)
  const after = JSON.parse(readFileSync(join(appDir, 'data', 'config.json'), 'utf8'))
  // 既有值不能被蓋掉
  assert.equal(after.llm.api_key, 'sk-keep-me')
  assert.equal(after.llm.model, 'my-model')
  assert.equal(after.llm.temperature, 0.3)
  assert.equal(after.server.port, 7000)
  assert.equal(after.server.access_token, 'keep-token')
  // 缺的區段/鍵補齊
  assert.ok(after.memory && typeof after.memory === 'object')
  assert.ok(after.tools && typeof after.tools === 'object')
  assert.equal(after.llm.base_url, 'https://api.deepseek.com')
  assert.ok(r.added.includes('memory'))
  assert.ok(r.added.includes('tools'))
  assert.ok(r.added.includes('llm.base_url'))
  // data 子目錄
  for (const name of ['people', 'diary', 'memory']) {
    assert.ok(existsSync(join(appDir, 'data', name)), `應該建立 data/${name}`)
  }
})

await t('沒有 config.json、有 .example → 先複製再補齊', () => {
  const appDir = tempDir('cfg-example')
  mkdirSync(join(appDir, 'data'), { recursive: true })
  writeFileSync(join(appDir, 'data', 'config.json.example'), JSON.stringify({ llm: { model: 'from-example' }, server: { port: 6199 } }))
  const r = ensureConfig({ appDir })
  assert.equal(r.ok, true)
  assert.equal(r.created, true)
  assert.equal(r.from_example, true)
  const after = JSON.parse(readFileSync(join(appDir, 'data', 'config.json'), 'utf8'))
  assert.equal(after.llm.model, 'from-example')
  assert.ok(after.memory && after.tools)
})

await t('沒有 config、沒有 .example → 寫最小可用模板（含 llm/memory/tools/server/webui）', () => {
  const appDir = tempDir('cfg-min')
  const r = ensureConfig({ appDir })
  assert.equal(r.ok, true)
  assert.equal(r.created, true)
  assert.equal(r.from_example, false)
  const after = JSON.parse(readFileSync(join(appDir, 'data', 'config.json'), 'utf8'))
  for (const key of ['llm', 'memory', 'tools', 'server', 'webui']) assert.ok(after[key], `最小模板應含 ${key}`)
  assert.equal(after.server.path, '/ws')
  assert.equal(after.server.port, 6199)
})

await t('onebotToken 非空 → 寫進 server.access_token（且回報 token_updated）', () => {
  const appDir = tempDir('cfg-token')
  mkdirSync(join(appDir, 'data'), { recursive: true })
  writeFileSync(join(appDir, 'data', 'config.json'), JSON.stringify({ llm: {}, server: { port: 6199, access_token: '' } }))
  const r = ensureConfig({ appDir, onebotToken: 'nap-token-1' })
  assert.equal(r.ok, true)
  assert.equal(r.token_updated, true)
  const after = JSON.parse(readFileSync(join(appDir, 'data', 'config.json'), 'utf8'))
  assert.equal(after.server.access_token, 'nap-token-1')
})

await t('壞掉的 JSON → ok:false，原檔不被覆蓋', () => {
  const appDir = tempDir('cfg-broken')
  mkdirSync(join(appDir, 'data'), { recursive: true })
  const path = join(appDir, 'data', 'config.json')
  writeFileSync(path, '{ this is not json')
  const r = ensureConfig({ appDir })
  assert.equal(r.ok, false)
  assert.match(r.error, /不是合法 JSON/)
  assert.equal(readFileSync(path, 'utf8'), '{ this is not json')
})

await t('真實 app/data/config.json 模板：ensureConfig 一個字元都不改', () => {
  const real = new URL('../app/data/config.json', import.meta.url)
  // app/ 由安裝包提供；若這份 checkout 還沒有，就跳過（不讓測試紅）
  if (!existsSync(real)) return
  const appDir = tempDir('cfg-real')
  mkdirSync(join(appDir, 'data'), { recursive: true })
  const srcText = readFileSync(real, 'utf8')
  writeFileSync(join(appDir, 'data', 'config.json'), srcText)
  const r = ensureConfig({ appDir })
  assert.equal(r.ok, true)
  assert.equal(r.changed, false)
  assert.deepEqual(r.added, [])
  assert.equal(readFileSync(join(appDir, 'data', 'config.json'), 'utf8'), srcText)
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== runEnvSetup 完整流程（假 run 造出假 venv） ==')

await t('run.py + 假 python + 假 pip → phase ready，四步都 ok', async () => {
  const appDir = tempDir('env-full')
  writeFileSync(join(appDir, 'run.py'), '# fake\n')
  writeFileSync(join(appDir, 'requirements.txt'), 'pillow>=10.0\nwebsockets>=12.0\n')
  const run = fakeRun((cmd, args) => {
    if (args.length === 1 && args[0] === '--version') {
      if (cmd === 'python3') return { ok: true, stdout: 'Python 3.12.4\n' }
      return { ok: false, stderr: 'command not found' }
    }
    if (args.join(' ') === '-m venv .venv') {
      mkdirSync(join(appDir, '.venv', 'bin'), { recursive: true })
      writeFileSync(join(appDir, '.venv', 'bin', 'python'), '')
      return { ok: true, stdout: 'venv created' }
    }
    if (args.includes('show')) return { ok: false, stdout: 'WARNING: Package(s) not found: pillow\n' }
    if (args.includes('install')) return { ok: true, stdout: 'Successfully installed pillow-10.0.0 websockets-12.0\n' }
    return { ok: false, error: `unexpected: ${cmd} ${args.join(' ')}` }
  })
  const progress = []
  const logs = []
  const r = await runEnvSetup({
    appDir,
    run,
    platform: 'linux',
    onLog: (line) => logs.push(line),
    onProgress: (pct, msg) => progress.push([pct, msg]),
  })
  assert.equal(r.ok, true, r.hint || r.error || '')
  assert.equal(r.phase, 'ready')
  assert.equal(r.python.version, '3.12.4')
  assert.equal(r.venv.skipped, false)
  assert.equal(r.deps.skipped, false)
  assert.ok(r.config.ok)
  assert.deepEqual(r.steps.map((s) => s.state), ['ok', 'ok', 'ok', 'ok', 'ok'])
  assert.ok(existsSync(join(appDir, 'data', 'config.json')))
  assert.ok(progress.length >= 3)
  assert.equal(progress[progress.length - 1][0], 100)
  assert.ok(logs.some((line) => /找到 Python 3\.12\.4/.test(line)))
})

await t('run.py 不存在 → phase no-app，一個指令都不跑', async () => {
  const appDir = tempDir('env-noapp')
  const run = fakeRun(() => ({ ok: true, stdout: 'Python 3.12.0' }))
  const r = await runEnvSetup({ appDir, run, platform: 'linux' })
  assert.equal(r.ok, false)
  assert.equal(r.phase, 'no-app')
  assert.equal(run.calls.length, 0)
})

await t('沒有 Python → phase no-python，帶 hint', async () => {
  const appDir = tempDir('env-nopy')
  writeFileSync(join(appDir, 'run.py'), '# fake\n')
  const run = fakeRun(() => ({ ok: false, error: 'not found' }))
  const r = await runEnvSetup({ appDir, run, platform: 'linux' })
  assert.equal(r.ok, false)
  assert.equal(r.phase, 'no-python')
  assert.ok(r.hint && r.hint.length > 0)
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== setupState（唯讀自檢） ==')

await t('空目錄 → phase no-app，progress 0，log 只留最後 60 行', async () => {
  const appDir = tempDir('state-empty')
  const run = fakeRun(() => ({ ok: false, error: 'not found' }))
  const logs = Array.from({ length: 100 }, (_, i) => `line-${i}`)
  const st = await setupState({ appDir, run, platform: 'linux', logs })
  assert.equal(st.appPresent, false)
  assert.equal(st.phase, 'no-app')
  assert.equal(st.progress, 0)
  assert.equal(st.log.length, 60)
  assert.deepEqual(st.log[59], { stream: 'stdout', line: 'line-99' })
  assert.equal(st.venv.exists, false)
  assert.equal(st.deps.ok, false)
  assert.equal(st.config.valid, false)
})

await t('venv + 依賴 + config 都齊 → phase ready progress 100', async () => {
  const appDir = tempDir('state-ready')
  writeFileSync(join(appDir, 'run.py'), '# fake\n')
  mkdirSync(join(appDir, '.venv', 'bin'), { recursive: true })
  writeFileSync(join(appDir, '.venv', 'bin', 'python'), '')
  mkdirSync(join(appDir, 'data'), { recursive: true })
  writeFileSync(join(appDir, 'data', 'config.json'), JSON.stringify({ llm: {}, memory: {}, tools: {} }))
  const run = fakeRun((cmd, args) => {
    if (args.length === 1 && args[0] === '--version') return { ok: true, stdout: 'Python 3.12.1' }
    if (args.includes('show')) return { ok: true, stdout: 'Name: pillow\n---\nName: websockets\n' }
    return { ok: false }
  })
  const st = await setupState({ appDir, run, platform: 'linux' })
  assert.equal(st.appPresent, true)
  assert.equal(st.python.ok, true)
  assert.equal(st.venv.exists, true)
  assert.deepEqual(st.deps.pillow, true)
  assert.deepEqual(st.deps.websockets, true)
  assert.equal(st.deps.ok, true)
  assert.equal(st.config.exists, true)
  assert.equal(st.config.valid, true)
  assert.equal(st.phase, 'ready')
  assert.equal(st.progress, 100)
})

await t('boot 進行中時 setupState 的 phase/progress 以 boot 為準', async () => {
  const appDir = tempDir('state-boot')
  const run = fakeRun(() => ({ ok: false }))
  const st = await setupState({
    appDir,
    run,
    platform: 'linux',
    boot: { running: true, phase: 'running', progress: 40, message: '一键进行中' },
  })
  assert.equal(st.phase, 'running')
  assert.equal(st.progress, 40)
  assert.equal(st.message, '一键进行中')
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== writeOnebotConfig（JSON 形狀 + 備份） ==')

await t('產出的 JSON 形狀完全符合 NapCat 反向 WS 格式', () => {
  const dir = tempDir('onebot-shape')
  const wsUrl = 'ws://127.0.0.1:6199/ws'
  const r = writeOnebotConfig({ configDir: join(dir, 'napcat'), qqUin: '123456789', wsUrl, token: 'tok-1' })
  assert.equal(r.ok, true, r.error || '')
  assert.equal(r.file, join(dir, 'napcat', 'onebot11_123456789.json'))
  assert.equal(r.qqUin, '123456789')
  const json = JSON.parse(readFileSync(r.file, 'utf8'))
  assert.deepEqual(Object.keys(json).sort(), ['enableLocalFile2Url', 'musicSignUrl', 'network', 'parseMultMsg'])
  assert.equal(json.musicSignUrl, '')
  assert.equal(json.enableLocalFile2Url, false)
  assert.equal(json.parseMultMsg, false)
  const net = json.network
  assert.deepEqual(net.httpServers, [])
  assert.deepEqual(net.httpClients, [])
  assert.deepEqual(net.websocketServers, [])
  assert.equal(net.websocketClients.length, 1)
  const client = net.websocketClients[0]
  assert.deepEqual(client, {
    name: 'pawbot',
    enable: true,
    url: wsUrl,
    messagePostFormat: 'array',
    reportSelfMessage: false,
    reconnectInterval: 5000,
    token: 'tok-1',
    debug: false,
    heartInterval: 30000,
  })
})

await t('既有檔先備份成 .before-pawbot，且不覆蓋最早那份備份', () => {
  const dir = tempDir('onebot-backup')
  const configDir = join(dir, 'napcat')
  mkdirSync(configDir, { recursive: true })
  const file = join(configDir, 'onebot11_42.json')
  writeFileSync(file, '{"old":"original"}')
  const first = writeOnebotConfig({ configDir, qqUin: '42', wsUrl: 'ws://a', token: '' })
  assert.equal(first.ok, true)
  assert.equal(first.backup, `${file}.before-pawbot`)
  assert.equal(readFileSync(first.backup, 'utf8'), '{"old":"original"}')
  const second = writeOnebotConfig({ configDir, qqUin: '42', wsUrl: 'ws://b', token: '' })
  assert.equal(second.ok, true)
  assert.equal(readFileSync(second.backup, 'utf8'), '{"old":"original"}', '備份要保留最原始內容')
  const now = JSON.parse(readFileSync(file, 'utf8'))
  assert.equal(now.network.websocketClients[0].url, 'ws://b')
})

await t('qqUin 未知 → 寫 onebot11.json', () => {
  const dir = tempDir('onebot-nouq')
  const r = writeOnebotConfig({ configDir: join(dir, 'napcat'), qqUin: '', wsUrl: 'ws://x', token: '' })
  assert.equal(r.ok, true)
  assert.equal(r.file, join(dir, 'napcat', 'onebot11.json'))
})

await t('napcatConfigDir：binary 用官方目錄、docker 用 data/napcat', () => {
  assert.equal(napcatConfigDir({ mode: 'docker', dataDir: '/srv/pawbot/data' }), join('/srv/pawbot/data', 'napcat'))
  const binaryDir = napcatConfigDir({ mode: 'binary', dataDir: '/srv/pawbot/data' })
  assert.ok(binaryDir.endsWith('napcat'), 'binary 應回 NapCat 的 config 目錄')
  assert.ok(!binaryDir.includes('/srv/pawbot/data'), 'binary 不該落在 PawBot data 下')
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== readWebui / qrFromLog / loggedInByPawbot ==')

await t('日誌正則：WebUi User Panel Url（host/port 可變）', () => {
  const a = readWebui({ logText: '[NapCat] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=abc123\n' })
  assert.ok(a)
  assert.equal(a.token, 'abc123')
  assert.match(a.url, /\/webui\?token=abc123/)
  assert.equal(a.source, 'log')

  const b = readWebui({ logText: 'WebUi User Panel Url: http://0.0.0.0:7000/webui?token=tok-2' })
  assert.ok(b)
  assert.equal(b.token, 'tok-2')
  assert.match(b.url, /0\.0\.0\.0:7000/)
})

await t('日誌只有寬鬆的 /webui?token= 也能抓', () => {
  const r = readWebui({ logText: 'open https://example.test:8443/webui?token=loose-token now' })
  assert.ok(r)
  assert.equal(r.token, 'loose-token')
})

await t('沒有日誌 → 讀 webui.json（host 0.0.0.0 換成 127.0.0.1）', () => {
  const dir = tempDir('webui-json')
  const configDir = join(dir, 'napcat')
  mkdirSync(configDir, { recursive: true })
  writeFileSync(join(configDir, 'webui.json'), JSON.stringify({ host: '0.0.0.0', port: 6099, token: 'webui-tok' }))
  const r = readWebui({ configDir })
  assert.ok(r)
  assert.equal(r.source, 'webui.json')
  assert.equal(r.token, 'webui-tok')
  assert.equal(r.url, 'http://127.0.0.1:6099/webui?token=webui-tok')
})

await t('什麼都沒有 → null', () => {
  assert.equal(readWebui({ logText: 'nothing useful', configDir: tempDir('webui-none') }), null)
})

await t('qrFromLog：二維碼 URL / 檔案路徑 / 抓不到', () => {
  assert.deepEqual(
    qrFromLog('please scan https://api.qrserver.com/v1/create-qr-code/?data=x'),
    { kind: 'url', value: 'https://api.qrserver.com/v1/create-qr-code/?data=x' },
  )
  assert.deepEqual(
    qrFromLog('二维码已保存到 /app/napcat/cache/qrcode.png'),
    { kind: 'file', value: '/app/napcat/cache/qrcode.png' },
  )
  assert.deepEqual(qrFromLog('no code here'), null)
})

await t('loggedInByPawbot：connections 非空才算已登入（多種形狀）', () => {
  assert.equal(loggedInByPawbot(null), false)
  assert.equal(loggedInByPawbot({}), false)
  assert.equal(loggedInByPawbot({ connections: [] }), false)
  assert.equal(loggedInByPawbot({ connections: [{ self_id: 1 }] }), true)
  assert.equal(loggedInByPawbot({ connections: 1 }), true)
  assert.equal(loggedInByPawbot({ data: { connections: [{ self_id: 2 }] } }), true)
  assert.equal(loggedInByPawbot({ connections: { 123: {} } }), true)
})

await t('isLoggedIn：PawBot WS 最可靠，其次是日誌字樣', () => {
  assert.deepEqual(isLoggedIn({ pawbotStatus: { connections: [{}] }, logText: '' }), {
    loggedIn: true,
    evidence: 'pawbot-ws',
  })
  assert.equal(isLoggedIn({ pawbotStatus: { connections: [] }, logText: '登录成功' }).evidence, 'napcat-log')
  assert.equal(isLoggedIn({ pawbotStatus: {}, logText: 'nothing' }).loggedIn, false)
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== detectNapcat / startNapcat（假 run，未實機） ==')

await t('docker --version + docker ps 都通 → mode docker', async () => {
  const run = fakeRun((cmd, args) => {
    if (cmd === 'docker' && args[0] === '--version') return { ok: true, stdout: 'Docker version 27.0.0' }
    if (cmd === 'docker' && args[0] === 'ps') return { ok: true, stdout: 'CONTAINER ID' }
    return { ok: false }
  })
  const r = await detectNapcat({ run, platform: 'linux', exists: () => false })
  assert.equal(r.mode, 'docker')
  assert.equal(await detectMode({ run, platform: 'linux', exists: () => false }), 'docker')
})

await t('沒有 docker，但 PATH 有 napcat → mode binary', async () => {
  const run = fakeRun((cmd, args) => {
    if (cmd === 'docker') return { ok: false, error: '找不到可执行文件：docker' }
    if (cmd === 'sh' && args[0] === '-c') return { ok: true, stdout: '/usr/local/bin/napcat\n' }
    return { ok: false }
  })
  const r = await detectNapcat({ run, platform: 'linux', exists: () => false })
  assert.equal(r.mode, 'binary')
  assert.equal(r.path, '/usr/local/bin/napcat')
})

await t('docker / napcat 都沒有 → mode none', async () => {
  const run = fakeRun(() => ({ ok: false }))
  const r = await detectNapcat({ run, platform: 'win32', exists: () => false })
  assert.equal(r.mode, 'none')
  assert.equal(await detectMode({ run, platform: 'win32', exists: () => false }), 'none')
})

await t('none 時的手動步驟誠實：有 GitHub Release 指引、不編造 curl 網址', () => {
  const guide = manualGuide()
  assert.ok(guide.steps.length >= 3)
  assert.ok(guide.steps.some((step) => step.includes('https://github.com/NapNeko/NapCatQQ')))
  assert.ok(!guide.steps.some((step) => /curl\s+-o\s+\S+napcat\.sh\s+https?:/.test(step)), '不該編造一鍵腳本網址')
})

await t('docker：無同名容器 → docker run 帶齊參數', async () => {
  const dir = tempDir('docker-run')
  const run = fakeRun((cmd, args) => {
    if (args[0] === 'ps') return { ok: true, stdout: '\n' }
    if (args[0] === 'run') return { ok: true, stdout: 'container-id\n' }
    return { ok: false }
  })
  const r = await startNapcat({ mode: 'docker', run, dataDir: dir, image: NAPCAT_IMAGE, webuiPort: 6099 })
  assert.equal(r.ok, true)
  assert.equal(r.started, true)
  const call = run.calls.find((c) => c.args[0] === 'run')
  assert.ok(call)
  const args = call.args
  for (const expected of [
    '--name', NAPCAT_CONTAINER, '--restart', 'unless-stopped', '-p', '6099:6099',
    '-e', 'NAPCAT_GID=0', '-e', 'MODE=astrbot', NAPCAT_IMAGE,
  ]) {
    assert.ok(args.includes(expected), `docker run 應含 ${expected}`)
  }
  assert.equal(args[3], NAPCAT_CONTAINER)
  assert.ok(String(args[args.indexOf('-v') + 1]).endsWith(`:${'/app/napcat/config'}`))
})

await t('docker：同名容器已存在 → docker start，不重跑 docker run（冪等）', async () => {
  const dir = tempDir('docker-start')
  const run = fakeRun((cmd, args) => {
    if (args[0] === 'ps' && args.includes('-a')) return { ok: true, stdout: `${NAPCAT_CONTAINER}\n` }
    if (args[0] === 'start') return { ok: true, stdout: NAPCAT_CONTAINER }
    return { ok: false, error: '不該跑 docker run' }
  })
  const r = await startNapcat({ mode: 'docker', run, dataDir: dir })
  assert.equal(r.ok, true)
  assert.equal(r.already_existing, true)
  assert.ok(!run.calls.some((c) => c.args[0] === 'run'), '已存在容器就不該 docker run')
  assert.ok(run.calls.some((c) => c.args[0] === 'start'))
})

await t('none → ok:false + mode none + manual.steps（不 throw）', async () => {
  const r = await startNapcat({ mode: 'none', run: fakeRun(() => ({ ok: false })) })
  assert.equal(r.ok, false)
  assert.equal(r.mode, 'none')
  assert.equal(r.started, false)
  assert.ok(Array.isArray(r.manual.steps) && r.manual.steps.length > 0)
})

await t('binary：systemctl 失敗 → 用子進程 fallback 並記錄 pid', async () => {
  const run = fakeRun((cmd) => {
    if (cmd === 'systemctl') return { ok: false, error: 'System has not been booted with systemd' }
    return { ok: false }
  })
  let spawned = null
  const spawnProcess = (cmd, args, opts) => {
    spawned = { cmd, args, opts }
    return { pid: 4321, unref() {} }
  }
  const r = await startNapcat({ mode: 'binary', run, spawnProcess })
  assert.equal(r.ok, true)
  assert.equal(r.via, 'process')
  assert.equal(r.pid, 4321)
  assert.equal(spawned.cmd, 'napcat')
  assert.equal(spawned.opts.detached, true)
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== boot 四步狀態機（假 deps） ==')

await t('完整四步：順序 env → napcat → pawbot → ready，done 且 progress 100', async () => {
  const order = []
  const boot = createBootOrchestrator({
    env: async () => { order.push('env'); return { ok: true, detail: 'env ok' } },
    napcat: async () => { order.push('napcat'); return { ok: true, detail: 'napcat ok' } },
    pawbot: async () => { order.push('pawbot'); return { ok: true, detail: 'pawbot ok' } },
    ready: async () => { order.push('ready'); return { ok: true, detail: 'ready ok' } },
  })
  const start = boot.start()
  assert.equal(start.started, true)
  const fin = await boot.waitForDone(5000)
  assert.deepEqual(order, ['env', 'napcat', 'pawbot', 'ready'])
  assert.equal(fin.done, true)
  assert.equal(fin.running, false)
  assert.equal(fin.phase, 'done')
  assert.equal(fin.progress, 100)
  assert.deepEqual(fin.steps.map((s) => s.id), ['env', 'napcat', 'pawbot', 'ready'])
  assert.deepEqual(fin.steps.map((s) => s.state), ['ok', 'ok', 'ok', 'ok'])
  assert.ok(fin.steps.every((s) => typeof s.name === 'string' && s.name.length > 0))
})

await t('napcat 回 skip（mode none）→ 該步 skip、其餘照跑、整體仍 done', async () => {
  const boot = createBootOrchestrator({
    env: async () => ({ ok: true }),
    napcat: async () => ({ ok: false, skip: true, mode: 'none', hint: '没有 docker' }),
    pawbot: async () => ({ ok: true }),
    ready: async () => ({ ok: true }),
  })
  boot.start()
  const fin = await boot.waitForDone(5000)
  assert.equal(fin.phase, 'done')
  assert.equal(fin.done, true)
  assert.deepEqual(fin.steps.map((s) => s.state), ['ok', 'skip', 'ok', 'ok'])
  assert.match(fin.steps[1].detail, /没有 docker/)
})

await t('env 失敗 → 其餘三步 skip、phase failed、done 仍是 true', async () => {
  const order = []
  const boot = createBootOrchestrator({
    env: async () => { order.push('env'); return { ok: false, hint: '没有 Python 3' } },
    napcat: async () => { order.push('napcat'); return { ok: true } },
    pawbot: async () => { order.push('pawbot'); return { ok: true } },
    ready: async () => { order.push('ready'); return { ok: true } },
  })
  boot.start()
  const fin = await boot.waitForDone(5000)
  assert.deepEqual(order, ['env'])
  assert.equal(fin.phase, 'failed')
  assert.equal(fin.done, true)
  assert.equal(fin.steps[0].state, 'fail')
  assert.deepEqual(fin.steps.slice(1).map((s) => s.state), ['skip', 'skip', 'skip'])
  assert.match(fin.steps[1].detail, /前一步/)
  assert.match(fin.error, /Python/)
})

await t('pawbot 失敗 → ready skip（不會白等）', async () => {
  const order = []
  const boot = createBootOrchestrator({
    env: async () => ({ ok: true }),
    napcat: async () => ({ ok: false, skip: true }),
    pawbot: async () => { order.push('pawbot'); return { ok: false, error: 'spawn 失败' } },
    ready: async () => { order.push('ready'); return { ok: true } },
  })
  boot.start()
  const fin = await boot.waitForDone(5000)
  assert.deepEqual(order, ['pawbot'])
  assert.equal(fin.steps[2].state, 'fail')
  assert.equal(fin.steps[3].state, 'skip')
  assert.equal(fin.phase, 'failed')
})

await t('重複 start：執行中回 started:false / already_running:true，不會跑第二套', async () => {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  let envRuns = 0
  const boot = createBootOrchestrator({
    env: async () => { envRuns += 1; await gate; return { ok: true } },
    napcat: async () => ({ ok: true }),
    pawbot: async () => ({ ok: true }),
    ready: async () => ({ ok: true }),
  })
  const first = boot.start()
  assert.equal(first.started, true)
  assert.equal(boot.status().steps[0].state, 'running')
  const second = boot.start()
  assert.equal(second.started, false)
  assert.equal(second.already_running, true)
  release()
  const fin = await boot.waitForDone(5000)
  assert.equal(envRuns, 1)
  assert.equal(fin.phase, 'done')
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== createSetupJob（非阻塞安裝 job） ==')

await t('start 立刻回 started，完成後 progress 100 / result 保留', async () => {
  let release
  const gate = new Promise((resolve) => { release = resolve })
  const job = createSetupJob({
    runEnv: async ({ onProgress }) => {
      onProgress(55, '安装依赖中')
      await gate
      return { ok: true, phase: 'ready' }
    },
  })
  const r = job.start()
  assert.equal(r.started, true)
  const mid = job.status()
  assert.equal(mid.running, true)
  assert.equal(mid.progress, 55)
  assert.equal(mid.message, '安装依赖中')
  release()
  const fin = await job.waitForDone(5000)
  assert.equal(fin.running, false)
  assert.equal(fin.progress, 100)
  assert.equal(fin.result.phase, 'ready')
  assert.equal(fin.error, null)
})

await t('失敗時 error 有值、progress 不假裝 100', async () => {
  const job = createSetupJob({
    runEnv: async ({ onProgress }) => {
      onProgress(25, '建 venv')
      return { ok: false, phase: 'venv-failed', hint: '装 python3-venv' }
    },
  })
  job.start()
  const fin = await job.waitForDone(5000)
  assert.equal(fin.running, false)
  assert.match(fin.error, /python3-venv/)
  assert.notEqual(fin.progress, 100)
})

/* ══════════════════════════════════════════════════════════════════════════ */
console.log('\n== /pawbot-ui 新路由接線（假 deps + CSRF 守衛） ==')

const SAME_ORIGIN = 'http://127.0.0.1:61025'

function mockReq({ method = 'GET', url = '/pawbot-ui/setup/status', headers = {}, body } = {}) {
  const req = new EventEmitter()
  req.method = method
  req.url = url
  req.headers = headers
  req.complete = false
  req.resume = () => {}
  if (body !== undefined) {
    setImmediate(() => {
      if (req.complete) return
      req.complete = true
      req.emit('data', Buffer.from(typeof body === 'string' ? body : JSON.stringify(body)))
      req.emit('end')
    })
  }
  return req
}

function mockRes() {
  const res = { statusCode: null, body: '', finished: false }
  res.writeHead = (status) => { res.statusCode = status }
  res.end = (chunk) => {
    if (chunk !== undefined) res.body += String(chunk)
    res.finished = true
  }
  res.json = () => JSON.parse(res.body)
  return res
}

function proxyWith(extra = {}) {
  return createPawbotProxy({
    pawbotFetch: async () => ({ ok: false, http_status: 404, data: null, error: 'not used' }),
    processInfo: () => ({ running: false }),
    restart: async () => ({ ok: true }),
    getOrigin: () => SAME_ORIGIN,
    ...extra,
  })
}

await t('GET /pawbot-ui/setup/status → 注入的 setup.status()', async () => {
  let called = 0
  const { dispatch } = proxyWith({
    setup: {
      status: async () => { called += 1; return { phase: 'ready', progress: 100, appPresent: true } },
      run: async () => ({ started: true }),
    },
  })
  const res = mockRes()
  await dispatch(mockReq({ url: '/pawbot-ui/setup/status' }), res)
  assert.equal(res.statusCode, 200)
  const body = res.json()
  assert.equal(body.ok, true)
  assert.equal(body.phase, 'ready')
  assert.equal(body.progress, 100)
  assert.equal(called, 1)
})

await t('POST /pawbot-ui/setup/run 帶 force → 傳給 setup.run', async () => {
  const seen = []
  const { dispatch } = proxyWith({
    setup: {
      status: async () => ({}),
      run: async (opts) => { seen.push(opts); return { started: true } },
    },
  })
  const res = mockRes()
  await dispatch(
    mockReq({
      method: 'POST',
      url: '/pawbot-ui/setup/run',
      headers: { origin: SAME_ORIGIN, 'content-type': 'application/json' },
      body: { force: true },
    }),
    res,
  )
  assert.equal(res.statusCode, 200)
  assert.equal(res.json().ok, true)
  assert.deepEqual(seen, [{ force: true }])
})

await t('POST /pawbot-ui/setup/run 沒有 content-type: application/json → 415（沿用 CSRF 守衛）', async () => {
  let called = 0
  const { dispatch } = proxyWith({
    setup: { status: async () => ({}), run: async () => { called += 1; return { started: true } } },
  })
  const res = mockRes()
  await dispatch(
    mockReq({
      method: 'POST',
      url: '/pawbot-ui/setup/run',
      headers: { origin: SAME_ORIGIN, 'content-type': 'text/plain' },
      body: {},
    }),
    res,
  )
  assert.equal(res.statusCode, 415)
  assert.equal(called, 0)
})

await t('GET /pawbot-ui/napcat/status 與 POST /pawbot-ui/napcat/qr', async () => {
  const { dispatch } = proxyWith({
    napcat: {
      status: async () => ({ mode: 'docker', running: true, webui: { url: 'http://x/webui?token=t', token: 't' }, qr: null, loggedIn: false }),
      setup: async () => ({ mode: 'docker', started: true }),
      qr: async () => ({ qr: { kind: 'url', value: 'https://qr' }, webui: null }),
    },
  })
  const sres = mockRes()
  await dispatch(mockReq({ url: '/pawbot-ui/napcat/status' }), sres)
  assert.equal(sres.statusCode, 200)
  assert.equal(sres.json().mode, 'docker')
  assert.equal(sres.json().webui.token, 't')

  const qres = mockRes()
  await dispatch(
    mockReq({
      method: 'POST',
      url: '/pawbot-ui/napcat/qr',
      headers: { origin: SAME_ORIGIN, 'content-type': 'application/json' },
      body: {},
    }),
    qres,
  )
  assert.equal(qres.statusCode, 200)
  assert.equal(qres.json().qr.kind, 'url')
})

await t('POST /pawbot-ui/napcat/setup：mode none 也回 200（ok:true + started:false + manual）', async () => {
  const { dispatch } = proxyWith({
    napcat: {
      status: async () => ({}),
      setup: async () => ({
        ok: true,
        mode: 'none',
        started: false,
        manual: { steps: ['a'] },
        hint: 'no docker',
        error: 'no docker',
      }),
      qr: async () => ({}),
    },
  })
  const res = mockRes()
  await dispatch(
    mockReq({
      method: 'POST',
      url: '/pawbot-ui/napcat/setup',
      headers: { origin: SAME_ORIGIN, 'content-type': 'application/json' },
      body: {},
    }),
    res,
  )
  assert.equal(res.statusCode, 200)
  const body = res.json()
  // 面板的 api() 會把 ok:false 當成拋錯；這裡必須是 true，並用 started:false 表達「沒啟動」
  assert.equal(body.ok, true)
  assert.equal(body.mode, 'none')
  assert.equal(body.started, false)
  assert.deepEqual(body.manual.steps, ['a'])
})

await t('POST /pawbot-ui/boot 與 GET /pawbot-ui/boot/status', async () => {
  let forceSeen = null
  const steps = [{ id: 'env', name: 'env', state: 'pending', detail: '' }]
  const { dispatch } = proxyWith({
    boot: {
      start: async ({ force }) => { forceSeen = force; return { started: true, phase: 'running', progress: 0, steps } },
      status: async () => ({ phase: 'running', progress: 25, steps, done: false }),
    },
  })
  const bres = mockRes()
  await dispatch(
    mockReq({
      method: 'POST',
      url: '/pawbot-ui/boot',
      headers: { origin: SAME_ORIGIN, 'content-type': 'application/json' },
      body: { force: true },
    }),
    bres,
  )
  assert.equal(bres.statusCode, 200)
  assert.equal(bres.json().started, true)
  assert.equal(forceSeen, true)

  const sres = mockRes()
  await dispatch(mockReq({ url: '/pawbot-ui/boot/status' }), sres)
  assert.equal(sres.statusCode, 200)
  assert.equal(sres.json().phase, 'running')
  assert.equal(sres.json().progress, 25)
})

await t('沒有注入對應 deps → 501（不假裝成功）', async () => {
  const { dispatch } = proxyWith()
  const res = mockRes()
  await dispatch(mockReq({ url: '/pawbot-ui/napcat/status' }), res)
  assert.equal(res.statusCode, 501)
  assert.equal(res.json().ok, false)
})

await t('跨來源 POST /pawbot-ui/boot → 403（新路由同樣受 CSRF 守衛保護）', async () => {
  let called = 0
  const { dispatch } = proxyWith({
    boot: { start: async () => { called += 1; return { started: true } }, status: async () => ({}) },
  })
  const res = mockRes()
  await dispatch(
    mockReq({
      method: 'POST',
      url: '/pawbot-ui/boot',
      headers: { origin: 'http://evil.example', 'sec-fetch-site': 'cross-site', 'content-type': 'application/json' },
      body: {},
    }),
    res,
  )
  assert.equal(res.statusCode, 403)
  assert.equal(called, 0)
})

/* ══════════════════════════════════════════════════════════════════════════ */
for (const dir of tmpRoots) {
  try { rmSync(dir, { recursive: true, force: true }) } catch { /* Windows 上被佔用就留著 */ }
}

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
