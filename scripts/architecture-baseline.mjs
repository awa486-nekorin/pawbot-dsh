#!/usr/bin/env node
/**
 * PawBot DSH 架构基线检查器（方案 A / 第 1 批次：基线与契约冻结）
 *
 * 只读检查；不联网、不执行 Python、不启动 PawBot、不写业务数据。
 * 检查关键文件、package 契约、Node/Python 入口、旧 /pawbot-ui 路由、
 * 只读桥/大脑桥端点、config.py 的 DEFAULT 来源、WebUI 关键端点。
 *
 * 用法：
 *   node scripts/architecture-baseline.mjs [--root <dir>] [--json] [--out <new.json>] [--verbose]
 *
 * 退出码：0 全通过；1 契约检查失败；2 参数/根目录/--out 安全校验/写入失败。
 */

import {
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  lstatSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import process from 'node:process'

export const REPORT_SCHEMA = 'pawbot-dsh/architecture-baseline@1'

export const REQUIRED_FILES = Object.freeze([
  'package.json',
  'cordis.patch.yml',
  'index.js',
  'client.js',
  'lib/webui-proxy.js',
  'lib/dsh-tool-bridge.js',
  'lib/dsh-brain.js',
  'lib/hostdeps.js',
  'lib/config-utils.js',
  'lib/setup.js',
  'lib/data-guard.js',
  'app/run.py',
  'app/requirements.txt',
  'app/pawbot/core.py',
  'app/pawbot/config.py',
  'app/pawbot/webui.py',
  'app/pawbot/dsh_bridge.py',
  'app/pawbot/brain.py',
  'bili-watch/index.js',
  'bili-watch/selfcheck.mjs',
  'bili-watch/locale/en.json',
  'bili-watch/locale/zh.json',
  'bili-watch/icon.svg',
  'locale/en.json',
  'locale/zh.json',
  'icon.svg',
  'scripts/architecture-baseline.mjs',
  'test/architecture-baseline.mjs',
  'docs/ARCHITECTURE-BASELINE.md',
])

const EXPECTED_EXPORTS = Object.freeze({
  '.': './index.js',
  './client': './client.js',
  './bili-watch': './bili-watch/index.js',
  './bili-watch/locale/*.json': './bili-watch/locale/*.json',
  './bili-watch/icon': './bili-watch/icon.svg',
  './locale/*.json': './locale/*.json',
  './icon': './icon.svg',
  './package.json': './package.json',
})

const EXPECTED_ENGINES = Object.freeze({
  dsh: '>=0.1.7-rc.1',
  node: '>=22',
})

const EXPECTED_DSH = Object.freeze({
  bundle: { patch: './cordis.patch.yml' },
  client: {
    platform: 'web',
    inject: [
      '@deepseek-ai/dsh-client-runtime',
      '@deepseek-ai/dsh-client-ui-settings',
      '@deepseek-ai/dsh-client-ui-settings-plugins',
    ],
  },
})

const PAWBOT_UI_SUBPATHS = Object.freeze([
  'status', 'config', 'persona', 'reload-plugins', 'plugins/reload',
  'send', 'restart', 'stop', 'context', 'memory', 'life',
  'setup/status', 'setup/run',
  'napcat/status', 'napcat/setup', 'napcat/qr',
  'boot', 'boot/status',
])

const PAWBOT_UI_METHOD_PAIRS = Object.freeze([
  'GET boot/status', 'GET config', 'GET context', 'GET life', 'GET memory',
  'GET napcat/status', 'GET persona', 'GET setup/status', 'GET status',
  'GET sync/scp/status',
  'POST boot', 'POST config', 'POST napcat/qr', 'POST napcat/setup',
  'POST persona', 'POST plugins/reload', 'POST reload-plugins',
  'POST restart', 'POST send', 'POST setup/run', 'POST stop',
  'POST sync/scp/pull', 'POST sync/scp/push',
])

const CONFIG_SECTIONS = Object.freeze([
  'store', 'longmem', 'memory', 'diary', 'hobbies', 'reminders', 'memes',
  'server', 'webui', 'bot', 'chat', 'spice', 'people', 'relations', 'reply',
  'slang', 'persona', 'media', 'account', 'tools', 'speech', 'video', 'tts',
  'qzone', 'selfedit', 'album', 'llm', 'judge', 'libido', 'emotion',
  'whitelist', 'random_interject', 'topic', 'life', 'proactive', 'events',
  'safety', 'sync',
])

const WEBUI_GET_ENDPOINTS = Object.freeze([
  '/api/status', '/api/brain/status', '/api/config', '/api/plugins',
  '/api/events', '/api/logs', '/api/sessions', '/api/people',
  '/api/memory', '/api/context', '/api/models', '/api/files',
  '/api/file', '/api/stickers',
])

const WEBUI_POST_ENDPOINTS = Object.freeze([
  '/api/brain/tool', '/api/config', '/api/plugins/reload', '/api/plugins/toggle',
  '/api/models/save', '/api/models/delete', '/api/models/apply',
  '/api/context/window', '/api/send', '/api/llm/test', '/api/session/clear',
  '/api/memory/forget', '/api/file',
])

const USAGE = `PawBot DSH 架构基线检查器

用法：
  node scripts/architecture-baseline.mjs [--root <dir>] [--json] [--out <new.json>] [--verbose]

参数：
  --root <dir>   仓库根目录；默认当前工作目录。以 - 开头的路径请用 --root=<dir>
  --json         把完整 JSON 报告写到 stdout（只输出 JSON）
  --out <file>   新建 .json 报告；父目录必须已存在
                 拒绝覆盖任何已有文件、叶子符号链接、特殊文件、<root>/data 与 <root>/app/data
  --verbose      人类可读输出中显示每个检查的 expected/actual
  --help, -h     显示本帮助

退出码：
  0  全部通过
  1  架构契约检查失败
  2  参数错误、根目录不存在、--out 不安全或报告写入失败

安全：
  - 只读检查；不带 --out 时不写任何文件；
  - --out 同时校验词法路径、父目录/目标的 realpath、根目录链接和外部数据 junction；
  - 采用硬链接发布，目标已存在时原子失败，绝不覆盖。`

class UsageError extends Error {
  constructor(message) {
    super(message)
    this.name = 'UsageError'
  }
}

class CheckFailure extends Error {
  constructor(message, expected = null, actual = null) {
    super(message)
    this.name = 'CheckFailure'
    this.expected = expected
    this.actual = actual
  }
}

function deepEqual(a, b) {
  if (Object.is(a, b)) return true
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    return a.every((item, index) => deepEqual(item, b[index]))
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const ak = Object.keys(a).sort()
    const bk = Object.keys(b).sort()
    if (ak.length !== bk.length) return false
    return ak.every((key, index) => key === bk[index] && deepEqual(a[key], b[key]))
  }
  return false
}

function fmt(value) {
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function assertEqual(actual, expected, label) {
  if (!deepEqual(actual, expected)) {
    throw new CheckFailure(`${label} 与冻结契约不一致`, expected, actual)
  }
}

function assertTrue(condition, message, expected = true, actual = false) {
  if (!condition) throw new CheckFailure(message, expected, actual)
}

function sorted(values) {
  return [...values].sort()
}

function assertSameSet(actual, expected, label) {
  const a = sorted(actual)
  const e = sorted(expected)
  if (!deepEqual(a, e)) throw new CheckFailure(`${label} 与冻结契约不一致`, e, a)
  return a
}

function isFile(path) {
  try {
    return statSync(path).isFile()
  } catch {
    return false
  }
}

function isDirectory(path) {
  try {
    return statSync(path).isDirectory()
  } catch {
    return false
  }
}

function realPath(path) {
  // Windows junction 只有 realpath.native 会解析到真实目标。
  return (realpathSync.native || realpathSync)(path)
}

function canonicalizeForWrite(target) {
  const absolute = resolve(target)
  if (existsSync(absolute)) {
    try {
      return realPath(absolute)
    } catch {
      return absolute
    }
  }
  const tail = []
  let cursor = absolute
  while (!existsSync(cursor)) {
    const parent = dirname(cursor)
    tail.unshift(basename(cursor))
    if (parent === cursor) break
    cursor = parent
  }
  let base = cursor
  try {
    base = realPath(cursor)
  } catch {
    base = cursor
  }
  return join(base, ...tail)
}

function normalizePathForCompare(path) {
  const absolute = resolve(path)
  return process.platform === 'win32' ? absolute.toLowerCase() : absolute
}

function isInsidePath(child, parent) {
  const c = normalizePathForCompare(child)
  const p = normalizePathForCompare(parent)
  const rel = relative(p, c)
  return rel === '' || (!rel.startsWith(`..${sep}`) && rel !== '..' && !isAbsolute(rel))
}

function validateOutPath(root, outValue) {
  if (!outValue) throw new UsageError('--out 需要一个文件路径')
  if (outValue === '-') {
    throw new UsageError('--out 不支持 "-"；需要 stdout 时请使用 --json')
  }
  const target = resolve(outValue)
  if (basename(target).toLowerCase() === 'package.json') {
    throw new UsageError(`拒绝把 package.json 作为报告目标：${target}`)
  }
  let leaf = null
  try {
    leaf = lstatSync(target)
  } catch (error) {
    if (error?.code !== 'ENOENT') {
      throw new UsageError(`无法检查 --out 目标：${target}（${error?.code || error?.message}）`)
    }
  }
  if (leaf) {
    if (leaf.isSymbolicLink()) {
      throw new UsageError(`拒绝 --out 叶子符号链接：${target}`)
    }
    if (leaf.isDirectory()) {
      throw new UsageError(`--out 指向目录，拒绝写入：${target}`)
    }
    if (!leaf.isFile()) {
      throw new UsageError(`--out 指向特殊文件，拒绝写入：${target}`)
    }
    throw new UsageError(`拒绝覆盖已有文件：${target}；请指定新的 .json 文件名`)
  }
  if (!basename(target).toLowerCase().endsWith('.json')) {
    throw new UsageError(`--out 只允许新建 .json 报告：${target}`)
  }

  const parent = dirname(target)
  let parentStat = null
  try {
    parentStat = statSync(parent)
  } catch (error) {
    throw new UsageError(`--out 父目录不存在或不可访问：${parent}（${error?.code || error?.message}）`)
  }
  if (!parentStat.isDirectory()) {
    throw new UsageError(`--out 父路径不是目录：${parent}`)
  }
  let canonicalParent
  try {
    canonicalParent = realPath(parent)
  } catch (error) {
    throw new UsageError(`无法解析 --out 父目录：${parent}（${error?.code || error?.message}）`)
  }

  const lexicalTarget = resolve(target)
  const canonicalTarget = join(canonicalParent, basename(target))
  const lexicalParent = resolve(parent)
  const rootReal = canonicalizeForWrite(root)
  const forbidden = []
  for (const base of new Set([resolve(root), rootReal])) {
    for (const rel of ['data', join('app', 'data')]) {
      const lexical = join(base, rel)
      forbidden.push({ lexical, canonical: canonicalizeForWrite(lexical) })
    }
  }

  for (const dir of forbidden) {
    const hit = [
      isInsidePath(lexicalTarget, dir.lexical),
      isInsidePath(lexicalParent, dir.lexical),
      isInsidePath(canonicalTarget, dir.canonical),
      isInsidePath(canonicalParent, dir.canonical),
    ].some(Boolean)
    if (hit) {
      throw new UsageError(
        `拒绝写入报告：--out 不能位于 <root>/data 或 <root>/app/data 内；` +
        `收到 ${target}，父目录真实路径 ${canonicalParent}`,
      )
    }
  }

  const rootLexical = resolve(root)
  const reportsLexical = join(rootLexical, 'reports')
  const reportsCanonical = canonicalizeForWrite(reportsLexical)
  const insideRoot = [
    isInsidePath(lexicalTarget, rootLexical),
    isInsidePath(lexicalParent, rootLexical),
    isInsidePath(canonicalTarget, rootReal),
    isInsidePath(canonicalParent, rootReal),
  ].some(Boolean)
  if (insideRoot) {
    const allowedByReports = [
      isInsidePath(lexicalTarget, reportsLexical),
      isInsidePath(lexicalParent, reportsLexical),
      isInsidePath(canonicalTarget, reportsCanonical),
      isInsidePath(canonicalParent, reportsCanonical),
      isInsidePath(reportsCanonical, rootReal),
    ].every(Boolean)
    if (!allowedByReports) {
      throw new UsageError(
        `root 内只允许写入 <root>/reports/ 下的新 .json 报告：${target}（父目录真实路径 ${canonicalParent}）`,
      )
    }
  }
  return target
}

function writeJsonReport(outPath, report) {
  const text = `${JSON.stringify(report, null, 2)}\n`
  const dir = dirname(outPath)
  const tempPath = join(
    dir,
    `.${basename(outPath)}.tmp-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  )
  let fd = null
  let published = false
  try {
    fd = openSync(tempPath, 'wx', 0o600)
    writeFileSync(fd, text, 'utf8')
    fsyncSync(fd)
    closeSync(fd)
    fd = null
    // linkSync 原子地要求目标不存在；目标已存在则 EEXIST，绝不覆盖。
    linkSync(tempPath, outPath)
    published = true
    try {
      unlinkSync(tempPath)
    } catch {
      /* 已发布的报告不受临时文件清理失败影响 */
    }
  } catch (error) {
    if (fd !== null) {
      try {
        closeSync(fd)
      } catch {
        /* 忽略清理失败 */
      }
    }
    if (!published) {
      try {
        unlinkSync(tempPath)
      } catch {
        /* 忽略清理失败 */
      }
    }
    if (error?.code === 'EEXIST') {
      throw new UsageError(`拒绝覆盖已有文件：${outPath}`)
    }
    throw new UsageError(`报告写入失败：${outPath}（${error?.code || error?.message || error}）`)
  }
}

function readText(root, rel) {
  const full = join(root, rel)
  let st
  try {
    st = statSync(full)
  } catch (error) {
    throw new CheckFailure(
      `缺少关键文件：${rel}`,
      `${rel} 存在且可读`,
      `${rel} 不存在（${error?.code || error?.message || 'stat failed'}）`,
    )
  }
  if (!st.isFile()) {
    throw new CheckFailure(
      `关键路径不是普通文件：${rel}`,
      `${rel} 是普通文件`,
      st.isDirectory() ? 'directory' : 'not a regular file',
    )
  }
  try {
    return readFileSync(full, 'utf8')
  } catch (error) {
    throw new CheckFailure(
      `无法读取关键文件：${rel}`,
      `${rel} 可按 UTF-8 读取`,
      error?.code || error?.message || 'read failed',
    )
  }
}

function parsePackageJson(root) {
  const raw = readText(root, 'package.json')
  try {
    return JSON.parse(raw.replace(/^﻿/, ''))
  } catch (error) {
    throw new CheckFailure('package.json 不是合法 JSON', 'valid JSON', error?.message || String(error))
  }
}

function extractBalanced(source, startIndex, openChar, closeChar, hashComments = false) {
  if (startIndex < 0 || source[startIndex] !== openChar) {
    throw new CheckFailure(
      `无法定位 ${openChar}${closeChar} 块`,
      `source[${startIndex}] === ${openChar}`,
      startIndex < 0 ? 'marker not found' : `found ${fmt(source[startIndex])}`,
    )
  }
  let depth = 0
  let quote = null
  let escaped = false
  let lineComment = false
  let blockComment = false
  for (let i = startIndex; i < source.length; i += 1) {
    const ch = source[i]
    const next = source[i + 1]
    if (lineComment) {
      if (ch === '\n') lineComment = false
      continue
    }
    if (blockComment) {
      if (ch === '*' && next === '/') {
        blockComment = false
        i += 1
      }
      continue
    }
    if (quote) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === quote) quote = null
      continue
    }
    if (ch === '/' && next === '/') {
      lineComment = true
      i += 1
      continue
    }
    if (ch === '/' && next === '*') {
      blockComment = true
      i += 1
      continue
    }
    if (hashComments && ch === '#') {
      lineComment = true
      continue
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      quote = ch
      continue
    }
    if (ch === openChar) depth += 1
    if (ch === closeChar) {
      depth -= 1
      if (depth === 0) return source.slice(startIndex, i + 1)
    }
  }
  throw new CheckFailure(`${openChar}${closeChar} 块没有闭合`, 'balanced block', 'unterminated')
}

function sliceBetween(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker)
  if (start < 0) throw new CheckFailure(`找不到代码段起点：${startMarker}`, startMarker, 'missing')
  const end = source.indexOf(endMarker, start + startMarker.length)
  if (end < 0) throw new CheckFailure(`找不到代码段终点：${endMarker}`, endMarker, 'missing')
  return source.slice(start, end)
}

function extractExportedString(source, name) {
  const re = new RegExp(`export\\s+const\\s+${name}\\s*=\\s*(['"])([^'"]*)\\1`)
  const match = re.exec(source)
  return match ? match[2] : null
}

function extractArrayAssignment(source, name, exported = false) {
  const prefix = exported ? 'export\\s+const\\s+' : '(?:const|let|var)\\s+'
  const re = new RegExp(`${prefix}${name}\\s*=\\s*(?:Object\\.freeze\\()?\\s*\\[`)
  const match = re.exec(source)
  if (!match) return null
  const openIndex = match.index + match[0].length - 1
  const block = extractBalanced(source, openIndex, '[', ']')
  return [...block.matchAll(/(['"])(.*?)\1/g)].map((item) => item[2])
}

function stripPythonLineComment(line) {
  let quote = null
  let escaped = false
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i]
    if (quote) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === quote) quote = null
      continue
    }
    if (ch === '#') return line.slice(0, i)
    if (ch === '"' || ch === "'") quote = ch
  }
  return line
}

function stripPythonComments(source) {
  return source.split(/\r?\n/).map(stripPythonLineComment).join('\n')
}

function tokenizeArithmetic(expression) {
  const tokens = []
  let i = 0
  while (i < expression.length) {
    const ch = expression[i]
    if (/\s/.test(ch)) {
      i += 1
      continue
    }
    if (/[0-9.]/.test(ch)) {
      let j = i
      while (j < expression.length && /[0-9._]/.test(expression[j])) j += 1
      const value = Number(expression.slice(i, j).replace(/_/g, ''))
      if (!Number.isFinite(value)) return null
      tokens.push({ type: 'number', value })
      i = j
      continue
    }
    if ((ch === '*' || ch === '/') && expression[i + 1] === ch) {
      tokens.push({ type: 'operator', value: ch + ch })
      i += 2
      continue
    }
    if ('+-*/%()'.includes(ch)) {
      tokens.push({ type: 'operator', value: ch })
      i += 1
      continue
    }
    return null
  }
  return tokens
}

/** 仅支持数字常量的 + - * / // % ** 与括号；不能解析就返回 null，让检查失败。 */
function evaluateArithmetic(expression) {
  const tokens = tokenizeArithmetic(expression)
  if (!tokens || tokens.length === 0) return null
  let pos = 0
  const peek = () => tokens[pos]
  const take = () => tokens[pos++]

  function parseExpression() {
    let value = parseTerm()
    while (value !== null && peek()?.type === 'operator' && (peek().value === '+' || peek().value === '-')) {
      const op = take().value
      const right = parseTerm()
      if (right === null) return null
      value = op === '+' ? value + right : value - right
    }
    return value
  }

  function parseTerm() {
    let value = parsePower()
    while (
      value !== null &&
      peek()?.type === 'operator' &&
      ['*', '/', '//', '%'].includes(peek().value)
    ) {
      const op = take().value
      const right = parsePower()
      if (right === null) return null
      if ((op === '/' || op === '//' || op === '%') && right === 0) return null
      if (op === '*') value *= right
      else if (op === '/') value /= right
      else if (op === '//') value = Math.floor(value / right)
      else value %= right
    }
    return value
  }

  function parsePower() {
    let value = parseUnary()
    if (value !== null && peek()?.type === 'operator' && peek().value === '**') {
      take()
      const exponent = parsePower()
      if (exponent === null) return null
      value = value ** exponent
    }
    return value
  }

  function parseUnary() {
    const token = peek()
    if (token?.type === 'operator' && (token.value === '+' || token.value === '-')) {
      take()
      const value = parseUnary()
      if (value === null) return null
      return token.value === '-' ? -value : value
    }
    return parsePrimary()
  }

  function parsePrimary() {
    const token = peek()
    if (!token) return null
    if (token.type === 'number') {
      take()
      return token.value
    }
    if (token.type === 'operator' && token.value === '(') {
      take()
      const value = parseExpression()
      if (value === null || peek()?.value !== ')') return null
      take()
      return value
    }
    return null
  }

  const value = parseExpression()
  if (value === null || pos !== tokens.length || !Number.isFinite(value)) return null
  return value
}

function extractObjectNumbers(source, name) {
  const re = new RegExp(`${name}\\s*=\\s*(?:Object\\.freeze\\()?\\s*\\{`)
  const match = re.exec(source)
  if (!match) return null
  const openIndex = match.index + match[0].length - 1
  const block = extractBalanced(source, openIndex, '{', '}')
  const values = {}
  let cursor = 1
  while (cursor < block.length - 1) {
    const keyRe = /\s*([A-Za-z_$][\w$]*)\s*:/g
    keyRe.lastIndex = cursor
    const keyMatch = keyRe.exec(block)
    if (!keyMatch) break
    const valueStart = keyRe.lastIndex
    let depth = 0
    let quote = null
    let escaped = false
    let end = valueStart
    for (; end < block.length - 1; end += 1) {
      const ch = block[end]
      if (quote) {
        if (escaped) escaped = false
        else if (ch === '\\') escaped = true
        else if (ch === quote) quote = null
        continue
      }
      if (ch === '"' || ch === "'" || ch === '`') {
        quote = ch
        continue
      }
      if (ch === '(' || ch === '[' || ch === '{') depth += 1
      else if (ch === ')' || ch === ']' || ch === '}') depth -= 1
      else if (ch === ',' && depth === 0) break
    }
    values[keyMatch[1]] = evaluateArithmetic(block.slice(valueStart, end).trim())
    cursor = end + 1
  }
  return values
}

function extractPythonString(source, name) {
  const match = new RegExp(`^\\s*${name}\\s*=\\s*(['"])(.*?)\\1`, 'm').exec(source)
  return match ? match[2] : null
}

function extractPythonNumber(source, name) {
  const match = new RegExp(`^\\s*${name}\\s*=\\s*(.+)$`, 'm').exec(source)
  if (!match) return null
  return evaluateArithmetic(stripPythonLineComment(match[1]).trim())
}

function extractDefaultBlock(source) {
  const marker = 'DEFAULT = {'
  const markerIndex = source.indexOf(marker)
  if (markerIndex < 0) throw new CheckFailure('config.py 没有 DEFAULT = {', marker, 'missing')
  const openIndex = source.indexOf('{', markerIndex)
  return extractBalanced(source, openIndex, '{', '}', true)
}

function extractTopLevelConfigEntries(block) {
  const entries = []
  for (const line of block.split(/\r?\n/)) {
    const match = /^ {4}(['"])([a-z_]+)\1\s*:\s*(.*)$/.exec(line)
    if (!match) continue
    entries.push({ key: match[2], dict: match[3].trim().startsWith('{') })
  }
  return entries
}

function extractDefaultAssignments(source) {
  const code = stripPythonComments(source)
  return {
    assignments: (code.match(/^\s*DEFAULT\s*=/gm) || []).length,
    mutations: (code.match(/^\s*DEFAULT\s*(?:\[|\.)/gm) || []).length,
  }
}

function extractPythonSectionBlock(defaultBlock, section) {
  const match = new RegExp(`(['"])${section}\\1\\s*:\\s*\\{`).exec(defaultBlock)
  if (!match) {
    throw new CheckFailure(`DEFAULT 缺少配置段 ${section}`, `"${section}": {`, 'missing')
  }
  const openIndex = match.index + match[0].length - 1
  return extractBalanced(defaultBlock, openIndex, '{', '}', true)
}

function parseSimplePythonObject(block) {
  const out = {}
  const code = stripPythonComments(block)
  const re = /(['"])([a-z_]+)\1\s*:\s*(?:(['"])(.*?)\3|(-?\d+(?:\.\d+)?)|(True|False))/g
  for (const match of code.matchAll(re)) {
    if (match[3] !== undefined) out[match[2]] = match[4]
    else if (match[5] !== undefined) out[match[2]] = Number(match[5])
    else out[match[2]] = match[6] === 'True'
  }
  return out
}

function extractPathComparisons(block) {
  const paths = new Set()
  for (const rawLine of block.split(/\r?\n/)) {
    const code = stripPythonLineComment(rawLine).trim()
    if (!code.startsWith('if ') && !code.startsWith('elif ')) continue
    const condition = code.replace(/^(?:if|elif)\s+/, '')
    if (condition.startsWith('path ==')) {
      const match = /^path\s*==\s*['"]([^'"]+)['"]/.exec(condition)
      if (match) paths.add(match[1])
    } else if (condition.startsWith('path in')) {
      const match = /^path\s+in\s*\(([^)]*)\)/.exec(condition)
      if (match) for (const item of match[1].matchAll(/['"]([^'"]+)['"]/g)) paths.add(item[1])
    }
  }
  return [...paths].sort()
}

function markerResults(source, markers) {
  const actual = {}
  const missing = []
  for (const marker of markers) {
    const ok = typeof marker.test === 'function'
      ? marker.test(source)
      : source.includes(marker.text)
    actual[marker.label] = ok
    if (!ok) missing.push(marker.label)
  }
  return {
    ok: missing.length === 0,
    expected: markers.map((marker) => marker.label),
    actual,
    ...(missing.length ? { detail: `缺少标记：${missing.join(', ')}` } : {}),
  }
}

function extractRegisterObjects(source) {
  const objects = []
  for (const match of source.matchAll(/ctx\.webServer\.register\(\s*\{([\s\S]*?)\}\s*\)/g)) {
    objects.push(match[1])
  }
  return objects
}

function registerHasExact(objects, expected) {
  return objects.some((object) => {
    const kind = /kind\s*:\s*(['"])([^'"]+)\1/.exec(object)?.[2]
    const pathMatch = /path\s*:\s*(?:([A-Za-z_$][\w$]*)|(['"])([^'"]*)\2)/.exec(object)
    const path = pathMatch ? (pathMatch[1] ?? pathMatch[3]) : null
    const handler = /handler\s*:\s*([A-Za-z_$][\w$.]*)/.exec(object)?.[1]
    return kind === expected.kind && path === expected.path && handler === expected.handler
  })
}

function wildcardTargetExists(root, target) {
  const normalized = target.replace(/^\.\//, '')
  const star = normalized.indexOf('*')
  if (star < 0) return isFile(join(root, normalized))
  const prefix = normalized.slice(0, star)
  const suffix = normalized.slice(star + 1)
  const slash = prefix.lastIndexOf('/')
  const dirRel = slash >= 0 ? prefix.slice(0, slash) : '.'
  const filePrefix = slash >= 0 ? prefix.slice(slash + 1) : prefix
  const dir = join(root, dirRel)
  if (!isDirectory(dir)) return false
  return readdirSync(dir).some((name) => name.startsWith(filePrefix) && name.endsWith(suffix))
}

function extractRelativeImports(source) {
  const specs = new Set()
  for (const match of source.matchAll(/\bfrom\s+(['"])(\.\.?\/[^'"]+)\1/g)) specs.add(match[2])
  for (const match of source.matchAll(/\bimport\s+(['"])(\.\.?\/[^'"]+)\1/g)) specs.add(match[2])
  return [...specs]
}

function moduleTargetExists(root, entryRel, spec) {
  const target = resolve(dirname(join(root, entryRel)), spec)
  if (isFile(target)) return true
  if (isDirectory(target) && isFile(join(target, 'index.js'))) return true
  return false
}

function recordCheck(checks, id, category, title, fn, options = {}) {
  try {
    const result = fn() || {}
    const check = { id, category, title, ok: result.ok !== false }
    if ('expected' in result) check.expected = result.expected
    if ('actual' in result) check.actual = result.actual
    if (result.detail) check.detail = result.detail
    if (options.staticOnly) {
      check.mode = 'static'
      check.limitation = options.limitation || '静态源码观测，不验证运行时行为。'
    }
    checks.push(check)
  } catch (error) {
    const check = { id, category, title, ok: false }
    if (error instanceof CheckFailure) {
      if (error.expected !== null) check.expected = error.expected
      if (error.actual !== null) check.actual = error.actual
    }
    check.error = error?.message || String(error)
    if (options.staticOnly) {
      check.mode = 'static'
      check.limitation = options.limitation || '静态源码观测，不验证运行时行为。'
    }
    checks.push(check)
  }
}

export function runBaseline(rootInput) {
  const root = resolve(rootInput)
  const checks = []
  const started = Date.now()

  recordCheck(checks, 'files.required', 'files', '关键文件齐全', () => {
    const missing = REQUIRED_FILES.filter((rel) => !isFile(join(root, rel)))
    return {
      ok: missing.length === 0,
      expected: `${REQUIRED_FILES.length} 个关键文件存在且为普通文件`,
      actual: missing.length === 0
        ? `${REQUIRED_FILES.length}/${REQUIRED_FILES.length}`
        : `缺少 ${missing.length} 个：${missing.join(', ')}`,
    }
  })

  recordCheck(checks, 'package.contract', 'package', 'package 身份 / engines / exports / dsh', () => {
    const pkg = parsePackageJson(root)
    const identity = { name: pkg.name, type: pkg.type }
    assertEqual(identity, { name: '@local/pawbot', type: 'module' }, 'package identity')
    assertEqual(pkg.engines, EXPECTED_ENGINES, 'package engines')
    assertEqual(pkg.exports, EXPECTED_EXPORTS, 'package exports')
    const missingTargets = Object.values(EXPECTED_EXPORTS).filter(
      (target) => !wildcardTargetExists(root, target),
    )
    if (missingTargets.length) {
      throw new CheckFailure('package exports 的目标文件缺失', Object.values(EXPECTED_EXPORTS), missingTargets)
    }
    assertEqual(pkg.dsh, EXPECTED_DSH, 'package dsh')
    assertTrue(isFile(join(root, 'cordis.patch.yml')), 'dsh.bundle.patch 目标不存在', 'cordis.patch.yml 存在', 'missing')
    return {
      ok: true,
      expected: { identity, engines: EXPECTED_ENGINES, exports: EXPECTED_EXPORTS, dsh: EXPECTED_DSH },
      actual: { identity, engines: pkg.engines, exports: pkg.exports, dsh: pkg.dsh },
    }
  })

  recordCheck(checks, 'entry.node.index', 'entry', 'Node 主入口 index.js', () => {
    const source = readText(root, 'index.js')
    const actual = {
      name: extractExportedString(source, 'name'),
      inject: extractArrayAssignment(source, 'inject', true),
      apply: /export\s+function\s+apply\s*\(/.test(source),
      config: /export\s+const\s+Config\s*=/.test(source),
    }
    assertEqual(
      { name: actual.name, inject: actual.inject },
      { name: 'pawbot', inject: ['tools', 'webServer'] },
      'index.js name/inject',
    )
    for (const key of ['apply', 'config']) {
      assertTrue(actual[key], `index.js 缺少 ${key} 契约`, true, actual[key])
    }
    return { ok: true, expected: { name: 'pawbot', inject: ['tools', 'webServer'], apply: true, config: true }, actual }
  })

  recordCheck(checks, 'entry.node.client', 'entry', '浏览器客户端 client.js', () => {
    const source = readText(root, 'client.js')
    return markerResults(source, [
      { label: 'ModuleLoader', text: 'window.__ModuleLoader__.load({' },
      { label: 'plugin id', test: (text) => /id:\s*'@local\/pawbot'/.test(text) },
      { label: 'api base', text: "const API = '/pawbot-ui'" },
    ])
  })

  recordCheck(checks, 'entry.node.bili-watch', 'entry', 'bili-watch 入口 bili-watch/index.js', () => {
    const source = readText(root, 'bili-watch/index.js')
    const actual = {
      name: extractExportedString(source, 'name'),
      inject: extractArrayAssignment(source, 'inject', true),
      apply: /export\s+function\s+apply\s*\(/.test(source),
      config: /export\s+const\s+Config\s*=/.test(source),
    }
    assertEqual(
      { name: actual.name, inject: actual.inject },
      { name: 'pawbot-bili-watch', inject: ['tools', 'llm'] },
      'bili-watch name/inject',
    )
    for (const key of ['apply', 'config']) {
      assertTrue(actual[key], `bili-watch/index.js 缺少 ${key} 契约`, true, actual[key])
    }
    return {
      ok: true,
      expected: { name: 'pawbot-bili-watch', inject: ['tools', 'llm'], apply: true, config: true },
      actual,
    }
  })

  recordCheck(checks, 'entry.python.run', 'entry', 'Python 入口 app/run.py', () => {
    const source = readText(root, 'app/run.py')
    return markerResults(source, [
      { label: 'import Bot', text: 'from pawbot.core import Bot' },
      { label: 'import WebUI', text: 'from pawbot.webui import WebUI' },
      { label: 'BASE_DIR', text: 'BASE_DIR =' },
      { label: 'Bot(BASE_DIR)', text: 'bot = Bot(BASE_DIR)' },
      { label: 'main', text: 'async def main(args):' },
      { label: 'main guard', text: 'if __name__ == "__main__":' },
      { label: 'asyncio run', text: 'asyncio.run(main(parse_args()))' },
    ])
  })

  recordCheck(checks, 'entry.relative-imports', 'entry', '入口直接相对依赖可解析', () => {
    const missing = []
    const checked = []
    for (const entry of ['index.js', 'bili-watch/index.js']) {
      for (const spec of extractRelativeImports(readText(root, entry))) {
        const label = `${entry} -> ${spec}`
        checked.push(label)
        if (!moduleTargetExists(root, entry, spec)) missing.push(label)
      }
    }
    return {
      ok: missing.length === 0,
      expected: 'index.js 与 bili-watch/index.js 的相对导入目标全部存在',
      actual: { checked, missing },
      ...(missing.length ? { detail: `缺少：${missing.join(', ')}` } : {}),
    }
  })

  recordCheck(checks, 'routes.pawbot-ui.subpaths', 'routes', '旧 /pawbot-ui 注册与 exact 子路径', () => {
    const source = readText(root, 'index.js')
    const registration = markerResults(source, [
      {
        label: 'proxy import',
        test: (text) => /import\s*\{\s*createPawbotProxy\s*\}\s*from\s*(['"])\.\/lib\/webui-proxy\.js\1/.test(text),
      },
      { label: 'prefix /pawbot-ui', text: "add({ kind: 'prefix', path: '/pawbot-ui' })" },
      { label: 'prefix /pawbot-ui/', text: "add({ kind: 'prefix', path: '/pawbot-ui/' })" },
      { label: 'exact template', text: 'path: `/pawbot-ui/${p}`' },
    ])
    if (!registration.ok) return registration
    const marker = 'for (const p of ['
    const markerIndex = source.indexOf(marker)
    if (markerIndex < 0) throw new CheckFailure('index.js 缺少 /pawbot-ui 子路径数组', marker, 'missing')
    const block = extractBalanced(source, source.indexOf('[', markerIndex), '[', ']')
    const values = [...block.matchAll(/(['"])([^'"]+)\1/g)].map((match) => match[2])
    assertTrue(new Set(values).size === values.length, '/pawbot-ui 子路径数组存在重复项', '无重复', values)
    return {
      ok: true,
      expected: { registration: registration.expected, subpaths: sorted(PAWBOT_UI_SUBPATHS) },
      actual: {
        registration: registration.actual,
        subpaths: assertSameSet(values, PAWBOT_UI_SUBPATHS, '/pawbot-ui 子路径'),
      },
    }
  })

  recordCheck(checks, 'routes.pawbot-ui.methods', 'routes', '旧 /pawbot-ui 方法与子路径', () => {
    const source = readText(root, 'lib/webui-proxy.js')
    const pairs = new Set()
    for (const match of source.matchAll(
      /method\s*===\s*['"](GET|POST)['"]\s*&&\s*sub\s*===\s*['"]([^'"]+)['"]/g,
    )) pairs.add(`${match[1]} ${match[2]}`)
    for (const match of source.matchAll(
      /method\s*===\s*['"](GET|POST)['"]\s*&&\s*\(\s*sub\s*===\s*['"]([^'"]+)['"]\s*\|\|\s*sub\s*===\s*['"]([^'"]+)['"]\s*\)/g,
    )) {
      pairs.add(`${match[1]} ${match[2]}`)
      pairs.add(`${match[1]} ${match[3]}`)
    }
    const actual = sorted(pairs)
    assertEqual(actual, sorted(PAWBOT_UI_METHOD_PAIRS), '/pawbot-ui method/sub')
    assertTrue(source.includes('未知的 /pawbot-ui 子路径'), '代理缺少未知子路径 404 分支', '未知子路径 404', 'missing')
    return { ok: true, expected: sorted(PAWBOT_UI_METHOD_PAIRS), actual }
  })

  recordCheck(checks, 'bridge.tool.constants', 'bridge', '只读桥常量与 Host exact 注册', () => {
    const js = readText(root, 'lib/dsh-tool-bridge.js')
    const py = readText(root, 'app/pawbot/dsh_bridge.py')
    const index = readText(root, 'index.js')
    const actual = {
      jsPath: extractExportedString(js, 'BRIDGE_PATH'),
      jsUrlEnv: extractExportedString(js, 'BRIDGE_URL_ENV'),
      jsTokenEnv: extractExportedString(js, 'BRIDGE_TOKEN_ENV'),
      pyPath: extractPythonString(py, 'PATH'),
      pyUrlEnv: extractPythonString(py, 'URL_ENV'),
      pyTokenEnv: extractPythonString(py, 'TOKEN_ENV'),
      jsTokenShape: /`pbdsh_\$\{randomBytes\(32\)\.toString\('hex'\)\}`/.test(js),
      pyTokenRegex: py.includes('pbdsh_[a-f0-9]{64}'),
      jsLoopback: js.includes('export function isLoopback('),
      pyLoopback: py.includes('address.is_loopback'),
      hostImport: index.includes(
        "import { BRIDGE_PATH, createToolBridge, redactBridgeText, stripBridgeEnv } from './lib/dsh-tool-bridge.js'",
      ),
      hostExact: index.includes(
        "ctx.webServer.register({ kind: 'exact', path: BRIDGE_PATH, handler: bridge.dispatch })",
      ),
      hostGated: index.includes('cfg.dshBridgeEnabled ? createToolBridge({'),
    }
    const expected = {
      jsPath: '/pawbot-dsh-bridge/execute',
      jsUrlEnv: 'PAWBOT_DSH_BRIDGE_URL',
      jsTokenEnv: 'PAWBOT_DSH_BRIDGE_TOKEN',
      pyPath: '/pawbot-dsh-bridge/execute',
      pyUrlEnv: 'PAWBOT_DSH_BRIDGE_URL',
      pyTokenEnv: 'PAWBOT_DSH_BRIDGE_TOKEN',
      jsTokenShape: true,
      pyTokenRegex: true,
      jsLoopback: true,
      pyLoopback: true,
      hostImport: true,
      hostExact: true,
      hostGated: true,
    }
    assertEqual(actual, expected, '只读桥常量/Host 注册')
    return { ok: true, expected, actual }
  })

  recordCheck(checks, 'bridge.tool.allowlist-limits', 'bridge', '只读桥白名单与限额', () => {
    const js = readText(root, 'lib/dsh-tool-bridge.js')
    const py = readText(root, 'app/pawbot/dsh_bridge.py')
    const jsTools = extractArrayAssignment(js, 'BRIDGE_TOOLS')
    assertEqual(jsTools, ['bili_watch_status'], 'BRIDGE_TOOLS')
    assertTrue(py.includes('"name": "bili_watch_status"'), 'Python 只读桥没有请求 bili_watch_status', true, false)
    assertTrue(
      py.includes('payload.get("name") != "bili_watch_status"'),
      'Python 只读桥没有校验响应 name',
      true,
      false,
    )
    const jsLimits = extractObjectNumbers(js, 'BRIDGE_LIMITS')
    const expectedJs = {
      bodyBytes: 1024,
      responseBytes: 1024,
      requestsPerMinute: 30,
      concurrency: 2,
      timeoutMs: 2000,
    }
    assertEqual(jsLimits, expectedJs, 'BRIDGE_LIMITS')
    const pyLimits = {
      MAX_RESPONSE_BYTES: extractPythonNumber(py, 'MAX_RESPONSE_BYTES'),
      TIMEOUT_SECONDS: extractPythonNumber(py, 'TIMEOUT_SECONDS'),
    }
    const expectedPy = { MAX_RESPONSE_BYTES: 1024, TIMEOUT_SECONDS: 2 }
    assertEqual(pyLimits, expectedPy, 'Python 只读桥限额')
    return {
      ok: true,
      expected: { jsTools: ['bili_watch_status'], jsLimits: expectedJs, pyLimits: expectedPy },
      actual: { jsTools, jsLimits, pyLimits },
    }
  })

  recordCheck(checks, 'bridge.tool.python-client', 'bridge', 'Python 只读桥客户端协议', () => {
    const py = readText(root, 'app/pawbot/dsh_bridge.py')
    const js = readText(root, 'lib/dsh-tool-bridge.js')
    const pyMarkers = markerResults(py, [
      { label: 'endpoint path exact', text: 'parsed.path != PATH' },
      { label: 'loopback only', text: 'address.is_loopback' },
      { label: 'POST PATH', text: 'connection.request("POST", PATH' },
      { label: 'bearer auth', text: '"Authorization": f"Bearer {token}"' },
      { label: 'json content type', text: '"Content-Type": "application/json"' },
      { label: 'response limit', text: 'MAX_RESPONSE_BYTES + 1' },
    ])
    const jsMarkers = markerResults(js, [
      { label: 'strip env', text: 'stripBridgeEnv(base)' },
      { label: 'loopback host', text: 'isLoopback(host)' },
      { label: 'inject url', text: 'env[BRIDGE_URL_ENV] = url.href' },
      { label: 'inject token', text: 'env[BRIDGE_TOKEN_ENV] = token' },
    ])
    return {
      ok: pyMarkers.ok && jsMarkers.ok,
      expected: { python: pyMarkers.expected, host: jsMarkers.expected },
      actual: { python: pyMarkers.actual, host: jsMarkers.actual },
      ...(pyMarkers.ok && jsMarkers.ok
        ? {}
        : { detail: [pyMarkers.detail, jsMarkers.detail].filter(Boolean).join('；') }),
    }
  })

  recordCheck(checks, 'bridge.brain.protocol', 'bridge', '大脑桥路径 / 客户端 / Host 注册', () => {
    const js = readText(root, 'lib/dsh-brain.js')
    const py = readText(root, 'app/pawbot/brain.py')
    const index = readText(root, 'index.js')
    const constants = {
      jsTurnPath: extractExportedString(js, 'BRAIN_TURN_PATH'),
      jsStatusPath: extractExportedString(js, 'BRAIN_STATUS_PATH'),
      jsUrlEnv: extractExportedString(js, 'BRAIN_URL_ENV'),
      jsTokenEnv: extractExportedString(js, 'BRAIN_TOKEN_ENV'),
      pyUrlEnv: extractPythonString(py, 'URL_ENV'),
      pyTokenEnv: extractPythonString(py, 'TOKEN_ENV'),
    }
    const expectedConstants = {
      jsTurnPath: '/pawbot-dsh-brain/turn',
      jsStatusPath: '/pawbot-dsh-brain/status',
      jsUrlEnv: 'PAWBOT_DSH_BRAIN_URL',
      jsTokenEnv: 'PAWBOT_DSH_BRAIN_TOKEN',
      pyUrlEnv: 'PAWBOT_DSH_BRAIN_URL',
      pyTokenEnv: 'PAWBOT_DSH_BRAIN_TOKEN',
    }
    assertEqual(constants, expectedConstants, '大脑桥常量')
    const client = markerResults(py, [
      { label: 'POST', text: 'urllib.request.Request(url, data=body, method="POST"' },
      { label: 'bearer', text: '"Authorization": f"Bearer {token}"' },
      { label: 'json', text: '"Content-Type": "application/json"' },
    ])
    const registerObjects = extractRegisterObjects(index)
    const host = markerResults(index, [
      {
        label: 'brain import',
        text: 'import { BRAIN_TURN_PATH, BRAIN_STATUS_PATH, BRAIN_URL_ENV, BRAIN_TOKEN_ENV, createBrainBridge',
      },
      { label: 'brain factory', text: 'const brain = createBrainBridge({' },
      {
        label: 'turn exact register',
        test: () => registerHasExact(registerObjects, {
          kind: 'exact',
          path: 'BRAIN_TURN_PATH',
          handler: 'brain.dispatchTurn',
        }),
      },
      {
        label: 'status exact register',
        test: () => registerHasExact(registerObjects, {
          kind: 'exact',
          path: 'BRAIN_STATUS_PATH',
          handler: 'brain.dispatchStatus',
        }),
      },
    ])
    return {
      ok: client.ok && host.ok,
      expected: { constants: expectedConstants, client: client.expected, host: host.expected },
      actual: { constants, client: client.actual, host: host.actual },
      ...(client.ok && host.ok ? {} : { detail: [client.detail, host.detail].filter(Boolean).join('；') }),
    }
  })

  recordCheck(checks, 'python.config.default', 'python', 'config DEFAULT 段 / 默认值 / 单一赋值来源', () => {
    const source = readText(root, 'app/pawbot/config.py')
    const block = extractDefaultBlock(source)
    const entries = extractTopLevelConfigEntries(block)
    const keys = entries.map((entry) => entry.key)
    assertTrue(new Set(keys).size === keys.length, 'DEFAULT 顶层配置段存在重复键', '无重复', keys)
    const nonDict = entries.filter((entry) => !entry.dict).map((entry) => entry.key)
    assertEqual(nonDict, [], 'DEFAULT 顶层配置段必须都是 dict')
    assertSameSet(keys, CONFIG_SECTIONS, 'DEFAULT 顶层配置段')
    const assignments = extractDefaultAssignments(source)
    assertEqual(assignments, { assignments: 1, mutations: 0 }, 'DEFAULT 必须只有一次字典赋值且无后续修改')

    const server = parseSimplePythonObject(extractPythonSectionBlock(block, 'server'))
    const webui = parseSimplePythonObject(extractPythonSectionBlock(block, 'webui'))
    const defaults = {
      server: { host: '0.0.0.0', port: 6199, path: '/ws', access_token: '' },
      webui: { host: '127.0.0.1', port: 6185, token: '' },
    }
    for (const [section, actualSection] of [['server', server], ['webui', webui]]) {
      for (const [key, value] of Object.entries(defaults[section])) {
        assertEqual({ [key]: actualSection[key] }, { [key]: value }, `DEFAULT.${section}.${key}`)
      }
    }
    return {
      ok: true,
      expected: { keys: sorted(CONFIG_SECTIONS), assignments: 1, mutations: 0, defaults },
      actual: {
        keys: sorted(keys),
        nonDict,
        ...assignments,
        defaults: {
          server: Object.fromEntries(Object.keys(defaults.server).map((key) => [key, server[key]])),
          webui: Object.fromEntries(Object.keys(defaults.webui).map((key) => [key, webui[key]])),
        },
      },
    }
  })

  recordCheck(checks, 'python.config.source', 'python', 'DEFAULT 是运行态配置的来源', () => {
    const source = stripPythonComments(readText(root, 'app/pawbot/config.py'))
    const loadVars = [...source.matchAll(
      /^\s*([A-Za-z_]\w*)\s*=\s*json\.load\s*\(\s*[^)\r\n]*\)/gm,
    )].map((match) => match[1])
    const mergeVars = [...source.matchAll(
      /self\.data\s*=\s*_deep_merge\s*\(\s*DEFAULT\s*,\s*([A-Za-z_]\w*)\s*,?\s*\)/g,
    )].map((match) => match[1])
    const linkedVars = mergeVars.filter((name) => loadVars.includes(name))
    const actual = {
      deepcopyAssignment: /self\.data\s*=\s*deepcopy\s*\(\s*DEFAULT\s*\)/.test(source),
      jsonLoadVars: loadVars,
      diskMergeVars: mergeVars,
      linkedVars,
      patchMergeAssignment: /self\.data\s*=\s*_deep_merge\s*\(\s*self\.data\s*,\s*[A-Za-z_]\w*\s*,?\s*\)/.test(source),
      deepMergeDef: /def\s+_deep_merge\s*\(/.test(source),
      getter: /def\s+get\s*\(\s*self/.test(source),
      setter: /def\s+set\s*\(\s*self/.test(source),
    }
    assertTrue(actual.deepcopyAssignment, '缺少 self.data = deepcopy(DEFAULT)', true, actual)
    assertTrue(loadVars.length > 0, '缺少 <变量> = json.load(...) 绑定', true, actual)
    assertTrue(actual.linkedVars.length > 0, 'json.load 变量与 _deep_merge(DEFAULT, 同变量) 未形成链路', true, actual)
    for (const key of ['patchMergeAssignment', 'deepMergeDef', 'getter', 'setter']) {
      assertTrue(actual[key], `缺少 ${key}`, true, actual[key])
    }
    return {
      ok: true,
      expected: {
        deepcopyAssignment: true,
        jsonLoadVars: 'non-empty',
        diskMergeVars: 'same variable as json.load',
        linkedVars: 'non-empty',
        patchMergeAssignment: true,
        deepMergeDef: true,
        getter: true,
        setter: true,
      },
      actual,
    }
  }, {
    staticOnly: true,
    limitation: '静态源码观测；不执行 Config，只校验 json.load 绑定变量与 self.data = _deep_merge(DEFAULT, 同变量) 的文本对应关系。',
  })

  recordCheck(checks, 'python.config.wiring', 'python', 'config.py / core.py / run.py 接线', () => {
    const source = `${readText(root, 'app/pawbot/core.py')}\n${readText(root, 'app/run.py')}`
    return markerResults(source, [
      {
        label: 'Config(data/config.json)',
        test: (text) => /Config\s*\(\s*os\.path\.join\(\s*base_dir\s*,\s*['"]data['"]\s*,\s*['"]config\.json['"]\s*\)\s*\)/.test(text),
      },
      { label: 'webui host', test: (text) => /host\s*=\s*bot\.config\.get\(\s*['"]webui\.host['"]\s*,\s*['"]127\.0\.0\.1['"]\s*\)/.test(text) },
      { label: 'webui port', test: (text) => /port\s*=\s*int\(\s*bot\.config\.get\(\s*['"]webui\.port['"]\s*,\s*6185\s*\)\s*\)/.test(text) },
      { label: 'webui token', test: (text) => /token\s*=\s*bot\.config\.get\(\s*['"]webui\.token['"]\s*,\s*['"]['"]\s*\)/.test(text) },
      { label: 'Bot(BASE_DIR)', text: 'bot = Bot(BASE_DIR)' },
    ])
  })

  recordCheck(checks, 'python.webui.endpoints.get', 'webui', 'WebUI 关键 GET 端点', () => {
    const source = readText(root, 'app/pawbot/webui.py')
    const block = sliceBetween(source, 'async def _route_get', 'async def _route_post')
    const paths = extractPathComparisons(block)
    const missing = WEBUI_GET_ENDPOINTS.filter((endpoint) => !paths.includes(endpoint))
    const actual = {
      paths,
      indexRoute: block.includes('path in ("/", "/index.html")'),
      stickerRoute: block.includes('path.startswith("/stickers/")'),
      streamRoute: source.includes('if path == "/api/stream" and method == "GET":'),
      missing,
    }
    if (missing.length) throw new CheckFailure('WebUI GET 端点缺失', sorted(WEBUI_GET_ENDPOINTS), actual)
    for (const key of ['indexRoute', 'stickerRoute', 'streamRoute']) {
      assertTrue(actual[key], `WebUI GET 缺少 ${key}`, true, actual[key])
    }
    return { ok: true, expected: sorted(WEBUI_GET_ENDPOINTS), actual }
  })

  recordCheck(checks, 'python.webui.endpoints.post', 'webui', 'WebUI 关键 POST 端点', () => {
    const source = readText(root, 'app/pawbot/webui.py')
    const block = sliceBetween(source, 'async def _route_post', 'async def _sse')
    const paths = extractPathComparisons(block)
    const missing = WEBUI_POST_ENDPOINTS.filter((endpoint) => !paths.includes(endpoint))
    const actual = {
      paths,
      brainToolDispatch: block.includes('await self._brain_tool(writer, payload, headers or {})'),
      unknown404: block.includes('await self._json(writer, 404, {"ok": False, "error": "not found"})'),
      missing,
    }
    if (missing.length) throw new CheckFailure('WebUI POST 端点缺失', sorted(WEBUI_POST_ENDPOINTS), actual)
    for (const key of ['brainToolDispatch', 'unknown404']) {
      assertTrue(actual[key], `WebUI POST 缺少 ${key}`, true, actual[key])
    }
    return { ok: true, expected: sorted(WEBUI_POST_ENDPOINTS), actual }
  })

  recordCheck(checks, 'python.webui.security-static', 'webui', 'WebUI 安全边界（静态观测）', () => {
    const source = readText(root, 'app/pawbot/webui.py')
    return markerResults(source, [
      { label: 'max body', text: 'MAX_HTTP_BODY = 4 * 1024 * 1024' },
      { label: 'token check', text: 'if self.token and path != "/api/brain/tool":' },
      { label: 'compare_digest', text: 'hmac.compare_digest' },
      { label: 'brain tool handler', text: 'async def _brain_tool(' },
      { label: 'brain token', text: 'expected = brain.env_token()' },
      { label: 'loopback peers', text: 'if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):' },
      { label: 'active ctx', text: 'ctx = brain.active_ctx(conv)' },
      { label: 'allowed tool', text: 'if not brain.tool_allowed(conv, name):' },
      { label: 'sensitive owner', text: 'if name in SENSITIVE_BRAIN_TOOLS and not self._ctx_is_owner(ctx):' },
    ])
  }, {
    staticOnly: true,
    limitation: '静态源码观测；不启动 WebUI，不验证运行时拒绝行为。运行行为由测试套件中的 CSRF 用例覆盖。',
  })

  const failed = checks.filter((check) => !check.ok).length
  return {
    schema: REPORT_SCHEMA,
    generated_at: new Date().toISOString(),
    root,
    node: process.version,
    platform: process.platform,
    duration_ms: Date.now() - started,
    ok: failed === 0,
    summary: { total: checks.length, passed: checks.length - failed, failed },
    checks,
  }
}

function requireValue(argv, index, flag, what) {
  const value = argv[index + 1]
  if (value === undefined || value === '' || (value.startsWith('-') && value !== '-')) {
    throw new UsageError(`${flag} 需要${what}；以 - 开头的值请用 ${flag}=<值>`)
  }
  return value
}

function parseArgs(argv) {
  const options = { root: null, json: false, out: null, verbose: false, help: false }
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === '--help' || arg === '-h') {
      options.help = true
      continue
    }
    if (arg === '--json') {
      options.json = true
      continue
    }
    if (arg === '--verbose' || arg === '-v') {
      options.verbose = true
      continue
    }
    if (arg === '--root') {
      options.root = requireValue(argv, i, '--root', '一个目录参数')
      i += 1
      continue
    }
    if (arg.startsWith('--root=')) {
      const value = arg.slice('--root='.length)
      if (!value) throw new UsageError('--root 需要一个目录参数')
      options.root = value
      continue
    }
    if (arg === '--out') {
      options.out = requireValue(argv, i, '--out', '一个文件参数')
      i += 1
      continue
    }
    if (arg.startsWith('--out=')) {
      const value = arg.slice('--out='.length)
      if (!value) throw new UsageError('--out 需要一个文件参数')
      options.out = value
      continue
    }
    throw new UsageError(`未知参数：${arg}`)
  }
  return options
}

function resolveRoot(value) {
  const root = resolve(value)
  if (!isDirectory(root)) throw new UsageError(`--root 不存在或不是目录：${root}`)
  return root
}

function printValue(value) {
  if (value === undefined || value === null) return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function printHuman(report, options, outPath) {
  console.log('PawBot DSH 架构基线检查')
  console.log(`root: ${report.root}`)
  console.log(`node: ${report.node}`)
  console.log(`schema: ${report.schema}`)
  console.log('')
  for (const check of report.checks) {
    console.log(`[${check.ok ? 'PASS' : 'FAIL'}] ${check.id} — ${check.title}`)
    if (!check.ok || options.verbose) {
      if (check.expected !== undefined) console.log(`       expected: ${printValue(check.expected)}`)
      if (check.actual !== undefined) console.log(`       actual:   ${printValue(check.actual)}`)
      if (check.detail) console.log(`       detail:   ${check.detail}`)
      if (check.error) console.log(`       error:    ${check.error}`)
      if (check.limitation) console.log(`       limitation: ${check.limitation}`)
    }
  }
  console.log('')
  console.log(
    `结果: ${report.ok ? 'PASS' : 'FAIL'} — ` +
    `${report.summary.passed} passed, ${report.summary.failed} failed, ` +
    `${report.summary.total} total`,
  )
  if (outPath) console.log(`报告已写入: ${outPath}`)
}

export function main(argv = process.argv.slice(2)) {
  let options
  try {
    options = parseArgs(argv)
  } catch (error) {
    if (error instanceof UsageError) {
      console.error(`错误: ${error.message}`)
      console.error('')
      console.error(USAGE)
      return 2
    }
    throw error
  }

  if (options.help) {
    console.log(USAGE)
    return 0
  }

  let root
  try {
    root = resolveRoot(options.root || process.cwd())
  } catch (error) {
    if (error instanceof UsageError) {
      console.error(`错误: ${error.message}`)
      return 2
    }
    throw error
  }

  let outPath = null
  try {
    if (options.out) outPath = validateOutPath(root, options.out)
  } catch (error) {
    if (error instanceof UsageError) {
      console.error(`错误: ${error.message}`)
      return 2
    }
    throw error
  }

  const report = runBaseline(root)

  if (outPath) {
    try {
      writeJsonReport(outPath, report)
    } catch (error) {
      if (error instanceof UsageError) {
        console.error(`错误: ${error.message}`)
        return 2
      }
      throw error
    }
  }

  if (options.json) process.stdout.write(`${JSON.stringify(report, null, 2)}\n`)
  else printHuman(report, options, outPath)

  return report.ok ? 0 : 1
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : ''
const thisPath = resolve(fileURLToPath(import.meta.url))
const samePath = process.platform === 'win32'
  ? invokedPath.toLowerCase() === thisPath.toLowerCase()
  : invokedPath === thisPath

if (samePath) {
  try {
    process.exitCode = main(process.argv.slice(2))
  } catch (error) {
    console.error(`未预期错误: ${error?.stack || error?.message || error}`)
    process.exitCode = 2
  }
}
