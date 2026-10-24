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
 *
 * 資料來源：
 *   - 優先打 PawBot WebUI：http://127.0.0.1:6185/api/config
 *   - 6185 沒開才退回讀 D:/WorkBuddy/2026-10-20-00-14-25/pawbot/data/config.json
 *   （只讀 key 與遮罩結果，不印任何設定值或 secrets）
 */
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { maskSecrets } from '../lib/config-utils.js'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const FALLBACK_CONFIG = 'D:/WorkBuddy/2026-10-20-00-14-25/pawbot/data/config.json'

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
  const url = 'http://127.0.0.1:6185/api/config'
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(3000) })
    if (!response.ok) throw new Error('HTTP ' + response.status)
    const json = await response.json()
    const config = json && json.data !== undefined ? json.data : json
    if (!isPlainObject(config)) throw new Error('回應不是 config 物件')
    return { config, source: url + '（PawBot WebUI 在線）' }
  } catch (error) {
    const config = JSON.parse(await readFile(FALLBACK_CONFIG, 'utf8'))
    if (!isPlainObject(config)) throw new Error('config.json 不是物件')
    return {
      config,
      source: FALLBACK_CONFIG + '（6185 不可用：' + String((error && error.message) || error) + '）',
    }
  }
}

console.log('== PawBot 面板 METADATA 檢查 ==')

// 載入 client.js：Node 分支只會把 PANEL_META 掛到 globalThis，不會碰 DOM / React。
await import(pathToFileURL(path.join(ROOT, 'client.js')).href)
const meta = globalThis.__PAWBOT_PANEL_META__
if (!meta || !meta.metadata || !Array.isArray(meta.common) || !Array.isArray(meta.advanced)) {
  console.error('✘ client.js 沒有暴露 __PAWBOT_PANEL_META__（Node 分支）')
  process.exit(1)
}

const { config, source } = await loadConfig()
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
