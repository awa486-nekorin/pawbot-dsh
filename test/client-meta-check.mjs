/**
 * test/client-meta-check.mjs —— 檢查 client.js 的 METADATA 有沒有跟真正的 config 對上。
 * 用法：`node test/client-meta-check.mjs`
 *
 * 檢查項目：
 *   1. METADATA 裡每一個 key 路徑都真的存在於 config；
 *   2. 「常用」清單的每個 key 都有中文 label（非空、含 CJK）與 help（>= 8 字）；
 *   3. label 沒有重複（重複只警告，不讓測試紅）；
 *   4. masked_keys 對應的 key 都被標成 secret 控件，反過來也檢查；
 *   5. config 裡每個葉節點都有 metadata（沒有漏掉的鍵）；
 *   6. 需求列出的「常用」key 全部都在；
 *   7. 影響徽章、分組、下拉選單設定都合法。
 *   8. 轻量静态检查控件标签、JSON 折叠、键盘分頁、对话框、搜索防抖与状态时间；
 *      只检查语义连接，不锁定文案/完整源码，也不替代真实浏览器与读屏测试。
 *
 * 資料來源（完全離線）：
 *   - Python 子進程直接載入本倉庫 app/pawbot/config.py 的 DEFAULT。
 *   - Windows 需 py（Python 3），其他平台需 python3；禁用 bytecode 寫入。
 *   - 不連 WebUI／網路，不讀寫 app/data，不實例化會保存配置的 Config。
 *   （只印 key 與檢查摘要，不印任何設定值或 secrets）
 */
import { readFile } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { runInNewContext } from 'node:vm'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { maskSecrets } from '../lib/config-utils.js'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')

/**
 * 需求指定的「常用」key 清單（73 個）。
 *
 * ⚠️ 這是**測試檔自己的外部常數**，刻意不從 client.js / PANEL_META 取。
 * 從被測檔案取需求清單的話，把某個 key 從兩邊同時刪掉測試照樣全綠，等於沒測。
 */
const REQUIRED_COMMON = [
  'llm.model', 'llm.api_key', 'llm.reasoning_effort', 'llm.max_tokens', 'llm.timeout', 'llm.vision_model',
  'judge.model', 'emotion.enabled', 'emotion.judge_model',
  'chat.reply_mode', 'chat.reply_in_private', 'chat.context_messages', 'chat.context_chars',
  'chat.time_decay_hours', 'chat.batch_enabled', 'chat.rate_limit_per_min',
  'reply.max_parts', 'reply.quote', 'reply.at_sender', 'reply.question_rate', 'reply.strip_ai_phrases',
  'reply.body_reaction_max', 'reply.strip_filler_opener',
  'safety.enabled', 'safety.typing_cps', 'safety.min_delay', 'safety.max_delay', 'safety.stop_when_replied',
  'proactive.enabled', 'proactive.daily_max', 'proactive.idle_min', 'proactive.idle_max',
  'proactive.bili_chance', 'proactive.idle_activity', 'proactive.surf_chance',
  'memory.engine', 'memory.total_chars', 'memory.l0_hours', 'memory.l1_chars', 'memory.l2_chars',
  'memory.l3_chars', 'memes.enabled', 'diary.enabled', 'longmem.enabled', 'people.auto_profile',
  'relations.enabled',
  'tools.enabled', 'tools.max_rounds', 'tools.search_provider', 'tools.search_model', 'tools.bili_min_gap',
  'tools.bili_json_card', 'tools.image_judge', 'tools.nsfw_censor', 'tools.block_minor', 'tools.x_enabled',
  'media.sticker_chance', 'persona.sticker_fetch', 'persona.sticker_anime_only', 'video.enabled',
  'speech.enabled', 'tts.enabled', 'tts.auto_chance',
  'account.owner_ids', 'qzone.enabled', 'qzone.daily_limit', 'selfedit.enabled', 'server.port',
  'webui.port', 'whitelist.group_ids', 'whitelist.blacklist_group_ids', 'random_interject.enabled',
  'life.enabled',
]

const failures = []
const warnings = []
let passed = 0

function ok(name, detail) {
  passed += 1
  console.log('  ✔ ' + name + (detail ? ' — ' + detail : ''))
}

function fail(name, detail) {
  failures.push({ name, detail })
  console.log('  ✘ ' + name + (detail ? '\n      ' + String(detail).split('\n').join('\n      ') : ''))
}

function warn(name, detail) {
  warnings.push({ name, detail })
  console.log('  ! ' + name + (detail ? '\n      ' + String(detail).split('\n').join('\n      ') : ''))
}

function getPath(object, key) {
  let cur = object
  for (const part of String(key).split('.')) {
    if (cur === null || typeof cur !== 'object') return undefined
    cur = cur[part]
  }
  return cur
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function hasCJK(text) {
  return /[\u3400-\u9fff]/.test(String(text || ''))
}

function charLength(text) {
  return Array.from(String(text || '')).length
}

function collectConfigKeys(config) {
  const keys = []
  for (const [section, value] of Object.entries(config)) {
    if (isPlainObject(value)) {
      for (const leaf of Object.keys(value)) keys.push(section + '.' + leaf)
    } else {
      keys.push(section)
    }
  }
  return keys
}

async function loadConfig() {
  const executable = process.platform === 'win32' ? 'py' : 'python3'
  const script = [
    'import json, runpy, sys',
    // Load this file directly, not the package or Config constructor (which can save).
    'defaults = runpy.run_path(sys.argv[1])["DEFAULT"]',
    'print(json.dumps(defaults, ensure_ascii=True))',
  ].join('\n')
  let stdout
  try {
    ;({ stdout } = await promisify(execFile)(executable, [
      ...(process.platform === 'win32' ? ['-3'] : []),
      '-I', '-B', '-c', script, path.join(ROOT, 'app', 'pawbot', 'config.py'),
    ], { cwd: ROOT, encoding: 'utf8', timeout: 15000, maxBuffer: 1024 * 1024, windowsHide: true }))
  } catch (error) {
    throw new Error('无法离线加载 config.py DEFAULT；请安装可用的 ' + executable +
      '（Python 3）。不回退到网络或运行时配置。原因：' + String(error.code || error.message))
  }
  const config = JSON.parse(stdout)
  if (!isPlainObject(config)) throw new Error('config.py DEFAULT 不是物件')
  return { config, source: 'app/pawbot/config.py DEFAULT（离线；' + executable + '；不读取运行时配置）' }
}

console.log('== PawBot 面板 METADATA 檢查 ==')

// 載入 client.js：Node 分支只會把 PANEL_META 掛到 globalThis，不會碰 DOM / React。
await import(pathToFileURL(path.join(ROOT, 'client.js')).href)
const meta = globalThis.__PAWBOT_PANEL_META__
if (!meta || !meta.metadata || !Array.isArray(meta.common) || !Array.isArray(meta.advanced)) {
  console.error('✘ client.js 沒有暴露 __PAWBOT_PANEL_META__（Node 分支）')
  process.exit(1)
}

// Static smoke checks: inspect semantic props/relationships, not copy, full snapshots,
// or exact formatting. These complement metadata checks, not browser/AT testing.
const clientSource = await readFile(path.join(ROOT, 'client.js'), 'utf8')
function componentSource(name) {
  const start = new RegExp(String.raw`^([ \t]*)function ${name}\s*\([^\n]*\)\s*\{`, 'm').exec(clientSource)
  if (!start) throw new Error('Missing component: ' + name)
  const end = clientSource.indexOf('\n' + start[1] + '}', start.index + start[0].length)
  if (end < 0) throw new Error('Missing component boundary: ' + name)
  return clientSource.slice(start.index, end)
}
function wiringCheck(name, run) {
  try {
    const result = run()
    if (result === true) ok(name)
    else fail(name, typeof result === 'string' ? result : 'Accessible wiring contract is missing')
  } catch (error) {
    fail(name, error.message)
  }
}
const prop = (name, value) => new RegExp(String.raw`(?:["']${name}["']|\b${name})\s*:\s*${value}(?=\s*[,}])`)

wiringCheck('字段标签连接实际控件，包含滑杆和 JSON', () => {
  const field = componentSource('Field')
  if (!prop('htmlFor', 'controlId').test(field) || !prop('id', 'labelId').test(field)) return false
  for (const name of ['NumberControl', 'SelectControl', 'RatioControl', 'SecretControl', 'JsonControl']) {
    const call = new RegExp(String.raw`h\(${name},\s*\{([^}]+)\}`).exec(field)
    if (!call || !/\bcontrolId\b/.test(call[1]) || !/\blabelId\b/.test(call[1])) return name + ' loses the field ids'
    const control = componentSource(name)
    if (!prop('id', 'controlId').test(control) || !prop('aria-labelledby', 'labelId').test(control)) return name + ' loses its accessible identity'
  }
  return true
})
wiringCheck('所有原生编辑控件都有名称或对应的可见标签', () => {
  const controls = [...clientSource.matchAll(/h\(['"](input|select|textarea)['"],\s*\{([\s\S]*?)\}/g)]
  if (!controls.length) return 'No controls checked'
  const unnamed = controls.filter(([, , props]) => {
    if (/['"]aria-(?:label|labelledby)['"]\s*:/.test(props)) return false
    const id = /\bid\s*:\s*['"]([^'"]+)['"]/.exec(props)?.[1]
    return !id || !new RegExp(String.raw`htmlFor\s*:\s*['"]${id}['"]`).test(clientSource)
  })
  return unnamed.length ? unnamed.map(([match]) => match.split('\n')[0]).join(', ') : true
})
wiringCheck('JSON 折叠按钮、编辑区和错误提示具有独立关联', () => {
  const json = componentSource('JsonControl')
  const button = /h\(['"]button['"],\s*\{([\s\S]*?)\}/.exec(json)?.[1] || ''
  const editor = /h\(['"]textarea['"],\s*\{([\s\S]*?)\}/.exec(json)?.[1] || ''
  return prop('id', 'controlId').test(button) && prop('aria-expanded', 'open').test(button) &&
    prop('aria-controls', 'regionId').test(button) && prop('id', 'regionId').test(json) &&
    prop('hidden', '!open').test(json) && prop('id', 'editorId').test(editor) &&
    prop('aria-labelledby', 'labelId').test(editor) && /aria-invalid/.test(editor) &&
    /aria-describedby[^\n]+errorId/.test(editor) && prop('id', 'errorId').test(json)
})
wiringCheck('开关使用名称和布尔 ARIA 状态', () => {
  const control = componentSource('Switch')
  return prop('role', `["']switch["']`).test(control) &&
    prop('id', 'controlId').test(control) && prop('aria-labelledby', 'labelId').test(control) &&
    prop('aria-checked', String.raw`Boolean\(on\)`).test(control)
})
wiringCheck('标签页有双向 ARIA 关联和单一活动面板', () => {
  const panel = componentSource('Panel')
  return /role\s*:\s*['"]tablist['"]/.test(panel) && /role\s*:\s*['"]tab['"]/.test(panel) &&
    /role\s*:\s*['"]tabpanel['"]/.test(panel) && /aria-controls[^\n]+pb-panel-/.test(panel) &&
    /aria-labelledby[^\n]+pb-tab-/.test(panel) && /aria-selected[^\n]+tab\s*===\s*id/.test(panel) &&
    /tabIndex\s*:\s*tab\s*===\s*id\s*\?\s*0\s*:\s*-1/.test(panel) &&
    /hidden\s*:\s*tab\s*!==\s*id/.test(panel) &&
    ['setup', 'status', 'config', 'persona'].every((id) => new RegExp(String.raw`panelProps\(["']${id}["']\)`).test(panel))
})
wiringCheck('标签页支持方向键与首尾键，并移动焦点', () => {
  const panel = componentSource('Panel')
  return ['ArrowRight', 'ArrowLeft', 'Home', 'End'].every((key) => panel.includes(key)) &&
    /preventDefault\(/.test(panel) && /\.focus\(/.test(panel) && /focus\s*=\s*true/.test(panel)
})
wiringCheck('可聚焦面板和对话框具有可见焦点样式', () => {
  const rules = [...clientSource.matchAll(/([^{}]+):focus-visible\s*\{([^}]+)\}/g)]
  return rules.some(([, selector, body]) =>
    /\[tabindex\]|\[data-pb-tabpanel\]/.test(selector) && /box-shadow|outline\s*:[^;]*solid/.test(body))
})
wiringCheck('确认框限制焦点、处理 Escape 并清理恢复', () => {
  const dialog = componentSource('ConfirmDialog')
  return /aria-modal/.test(dialog) && /aria-labelledby/.test(dialog) &&
    /activeElement/.test(dialog) && /querySelectorAll/.test(dialog) &&
    /shiftKey/.test(dialog) && /Escape/.test(dialog) && /callbacksRef\.current\.busy/.test(dialog) &&
    /card\.focus\(/.test(dialog) && /focusable\.includes\(document\.activeElement\)/.test(dialog) &&
    /addEventListener\(['"]focusin['"]/.test(dialog) && /removeEventListener\(['"]focusin['"]/.test(dialog) &&
    /removeEventListener\(['"]keydown['"]/.test(dialog) && /previous\.isConnected/.test(dialog) && /previous\.focus\(/.test(dialog)
})
wiringCheck('搜索输入立即更新草稿，筛选防抖且取消旧计时器', () => {
  const config = componentSource('ConfigTab')
  const delay = /setTimeout\(\(\)\s*=>\s*setSearch\(searchDraft\),\s*(\d+)\)/.exec(config)
  return /value\s*:\s*searchDraft/.test(config) && /setSearchDraft\(event\.target\.value\)/.test(config) &&
    Boolean(delay && Number(delay[1]) >= 100 && Number(delay[1]) <= 500) &&
    /return\s*\(\)\s*=>\s*window\.clearTimeout\(/.test(config) && /\[searchDraft\]/.test(config)
})
wiringCheck('状态首次失败不伪报停机，并标记成功响应时间', () => {
  const status = componentSource('StatusTab')
  return /!st\s*\?/.test(status) && /lastSuccessAt/.test(status) &&
    (status.match(/setLastSuccessAt\(Date\.now\(\)\)/g) || []).length === 2 &&
    /dateTime\s*:\s*new Date\(lastSuccessAt\)\.toISOString\(\)/.test(status)
})

const { config, source } = await loadConfig()
wiringCheck('常用清单保持 73 项，全部 metadata 为 73 + 233 = 306', () =>
  REQUIRED_COMMON.length === 73 && meta.common.length === 73 && meta.advanced.length === 233 &&
  meta.common.every((item) => REQUIRED_COMMON.includes(item.key)))
wiringCheck('QQ 空间主动权限保留在高级设置且默认关闭', () =>
  meta.metadata['qzone.proactive_enabled']?.advanced === true && config.qzone.proactive_enabled === false)
wiringCheck('每日空间额度保留正整数要求且没有虚构上限', () => {
  const item = meta.metadata['qzone.daily_limit']
  const boundsFor = runInNewContext('(' + componentSource('boundsFor') + '\n})', Object.create(null), { timeout: 1000 })
  for (const current of [3, 101, 100001]) {
    const bounds = boundsFor(item, current)
    if (bounds.min !== 1 || bounds.step !== 1 || bounds.max !== undefined) return false
  }
  return item.max === null && boundsFor({ key: 'example', max: 0 }, 3).max === 0 &&
    boundsFor({ key: 'example', max: 42 }, 3).max === 42 &&
    boundsFor({ key: 'example' }, 3).max === 100000 &&
    boundsFor({ key: 'server.port' }, 3).max === 65535 &&
    prop('max', 'bounds.max').test(componentSource('NumberControl'))
})
const configKeys = collectConfigKeys(config)
const allMeta = meta.common.concat(meta.advanced)
const allLabels = new Map()
const allKeys = new Map()
const masked = maskSecrets(config)
const maskedKeys = masked.maskedKeys

console.log('  config 來源：' + source)
console.log('  config 區段：' + Object.keys(config).length + '，葉節點：' + configKeys.length)
console.log('  metadata：常用 ' + meta.common.length + ' + 進階 ' + meta.advanced.length + ' = ' + allMeta.length)
console.log('  masked_keys：' + maskedKeys.length + ' 個')
console.log('')

// 0. key 不重複
for (const item of allMeta) {
  if (allKeys.has(item.key)) warn('metadata key 重複', item.key)
  else allKeys.set(item.key, true)
}
if (allKeys.size === allMeta.length) ok('metadata key 沒有重複', allMeta.length + ' 個')
else fail('metadata key 有重複', '請看上面的警告')

// 1. METADATA 每個 key 都存在於 config
{
  const missing = allMeta.filter((item) => getPath(config, item.key) === undefined).map((item) => item.key)
  if (missing.length) fail('METADATA 的 key 都真的存在於 config', missing.join('\n'))
  else ok('METADATA 的 key 都真的存在於 config', allMeta.length + '/' + allMeta.length)
}

// 2. 常用 key 的 label / help
{
  const badLabels = meta.common.filter((item) => !item.label || !hasCJK(item.label))
  const shortHelp = meta.common.filter((item) => charLength(String(item.help || '').replace(/\s/g, '')) < 8)
  if (badLabels.length) {
    fail('常用欄位都有中文 label', badLabels.map((item) => item.key + ' → ' + JSON.stringify(item.label)).join('\n'))
  } else {
    ok('常用欄位都有中文 label', meta.common.length + '/' + meta.common.length)
  }
  if (shortHelp.length) {
    fail('常用欄位 help ≥ 8 字', shortHelp.map((item) => item.key + ' → ' + charLength(item.help || '') + ' 字').join('\n'))
  } else {
    ok('常用欄位 help ≥ 8 字', meta.common.length + '/' + meta.common.length)
  }
}

// 2b. 進階欄位也要有中文 label / help / 區段標題
{
  const badLabels = meta.advanced.filter((item) => !item.label || !hasCJK(item.label))
  const shortHelp = meta.advanced.filter((item) => charLength(String(item.help || '').replace(/\s/g, '')) < 8)
  const badSections = meta.advanced.filter((item) => !meta.sectionLabels[item.section])
  if (badLabels.length) {
    fail('進階欄位也有中文 label', badLabels.map((item) => item.key + ' → ' + JSON.stringify(item.label)).join('\n'))
  } else {
    ok('進階欄位也有中文 label', meta.advanced.length + '/' + meta.advanced.length)
  }
  if (shortHelp.length) {
    fail('進階欄位 help ≥ 8 字', shortHelp.map((item) => item.key).join('\n'))
  } else {
    ok('進階欄位 help ≥ 8 字', meta.advanced.length + '/' + meta.advanced.length)
  }
  if (badSections.length) fail('進階欄位都有中文區段標題', badSections.map((item) => item.key).join('\n'))
  else ok('進階欄位都有中文區段標題', Object.keys(meta.sectionLabels).length + ' 個區段')
}

// 3. label 重複（只警告）
for (const item of allMeta) {
  if (allLabels.has(item.label)) {
    const previous = allLabels.get(item.label)
    warn('label 重複，可能造成混淆', item.label + '：' + previous + ' / ' + item.key)
  } else {
    allLabels.set(item.label, item.key)
  }
}
if (allLabels.size === allMeta.length) ok('沒有重複的 label', allMeta.length + ' 個標籤都唯一')

// 4. masked_keys ↔ secret 控件
{
  const notSecret = maskedKeys.filter((key) => {
    const item = meta.metadata[key]
    return !item || item.control !== 'secret'
  })
  // 空字串的 secret 現在不遮罩、不列入 masked_keys（面板會顯示「未設定」），
  // 所以「secret 控件都必須在 masked_keys」只對真的有值的鍵成立。
  const secretControls = allMeta.filter((item) => item.control === 'secret')
  const secretWithValue = secretControls.filter((item) => {
    const raw = getPath(config, item.key)
    return !(raw === '' || raw === null || raw === undefined)
  })
  const secretNotMasked = secretWithValue
    .filter((item) => maskedKeys.indexOf(item.key) < 0)
    .map((item) => item.key)
  if (notSecret.length) fail('masked_keys 都被標成 secret 控件', notSecret.join('\n'))
  else ok('masked_keys 都被標成 secret 控件', maskedKeys.length + '/' + maskedKeys.length)
  if (secretNotMasked.length) fail('有值的 secret 控件都在 masked_keys 裡', secretNotMasked.join('\n'))
  else {
    ok(
      '有值的 secret 控件都在 masked_keys 裡',
      secretWithValue.length + ' 個（另有 ' + (secretControls.length - secretWithValue.length) + ' 個空值會顯示「未設定」）',
    )
  }
}

// 5. config 每個葉節點都有 metadata
{
  const uncovered = configKeys.filter((key) => !meta.metadata[key])
  if (uncovered.length) fail('config 每個 key 都有 metadata（沒有漏）', uncovered.join('\n'))
  else ok('config 每個 key 都有 metadata（沒有漏）', configKeys.length + '/' + configKeys.length)
}

// 6. 需求指定的常用 key 全部都在（清單在測試檔內，不取自 client.js）
{
  const missing = REQUIRED_COMMON.filter((key) => !meta.metadata[key])
  const notCommon = REQUIRED_COMMON.filter((key) => {
    const item = meta.metadata[key]
    return !item || item.advanced
  })
  if (missing.length) fail('需求列出的常用 key 都在 METADATA', missing.join('\n'))
  else ok('需求列出的常用 key 都在 METADATA', REQUIRED_COMMON.length + ' 個')
  if (notCommon.length) fail('需求列出的 key 都落在「常用」', notCommon.join('\n'))
  else ok('需求列出的 key 都落在「常用」', REQUIRED_COMMON.length + ' 個')
}

// 7. 影響徽章、分組、下拉選單
{
  const badImpact = allMeta.filter((item) => (
    !Array.isArray(item.impact) || !item.impact.length ||
    item.impact.some((code) => !meta.impacts[code])
  ))
  if (badImpact.length) fail('每個欄位都有合法的影響徽章', badImpact.map((item) => item.key).join('\n'))
  else ok('每個欄位都有合法的影響徽章', allMeta.length + '/' + allMeta.length)

  const groupIds = new Set(meta.groups.map((group) => group.id))
  const badGroup = allMeta.filter((item) => (
    item.advanced ? item.group !== 'advanced' : !groupIds.has(item.group)
  ))
  if (badGroup.length) fail('每個欄位都落在合法分組', badGroup.map((item) => item.key + ' → ' + item.group).join('\n'))
  else ok('每個欄位都落在合法分組', meta.groups.length + ' 個分組')

  const badSelect = allMeta.filter((item) => (
    item.control === 'select' && (!Array.isArray(item.options) || item.options.length === 0)
  ))
  if (badSelect.length) fail('下拉選單都有選項', badSelect.map((item) => item.key).join('\n'))
  else ok('下拉選單都有選項', allMeta.filter((item) => item.control === 'select').length + ' 個')
}

// 8. help 写「旧版／旧字段／兼容」的字段，徽章必须含 compat
{
  const compatWord = /旧版|旧字段|兼容/
  const hits = allMeta.filter((item) => compatWord.test(String(item.help || '')))
  const badCompat = hits
    .filter((item) => item.impact.indexOf('compat') < 0)
    .map((item) => item.key)
  if (badCompat.length) fail('help 提到旧版/旧字段/兼容的字段都标了 compat 徽章', badCompat.join('\n'))
  else if (hits.length < 1) {
    // 面板文字若再被改写（例如繁体→简体），正则会一个都命中不到而「假绿」。
    // 命中 0 个直接 fail：这条规则必须真的检查到东西。
    fail(
      'help 提到旧版/旧字段/兼容的字段都标了 compat 徽章',
      'compat 规则可能失效：命中 0 个（help 文字被改过？正则应同步更新）',
    )
  } else {
    ok('help 提到旧版/旧字段/兼容的字段都标了 compat 徽章', hits.length + ' 个')
  }
}

// 9. 徽章說 restart、help 卻說「立即生效」＝自相矛盾
{
  const badRestart = allMeta
    .filter((item) => item.impact.indexOf('restart') >= 0)
    .filter((item) => /立即生效/.test(String(item.help || '')))
    .map((item) => item.key)
  if (badRestart.length) fail('restart 徽章的 help 沒有說「立即生效」', badRestart.join('\n'))
  else ok('restart 徽章的 help 沒有說「立即生效」', meta.advanced.concat(meta.common).filter((item) => item.impact.indexOf('restart') >= 0).length + ' 個')
}

console.log('')
console.log('== 結果 ==')
console.log('  ' + passed + ' passed, ' + failures.length + ' failed, ' + warnings.length + ' warning(s)')
if (warnings.length) {
  console.log('  警告：')
  for (const warning of warnings) console.log('    - ' + warning.name + '：' + warning.detail)
}
if (failures.length) {
  console.log('  失敗：')
  for (const failure of failures) console.log('    - ' + failure.name)
  process.exitCode = 1
}
