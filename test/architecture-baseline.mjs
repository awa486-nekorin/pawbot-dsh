#!/usr/bin/env node
/**
 * scripts/architecture-baseline.mjs 的离线测试。
 *
 * 覆盖：成功路径；缺文件/契约漂移；参数错误；--out 新建/不覆盖/拒写数据目录；
 * 数值表达式；DEFAULT 覆盖与重复键；端点删除/注释/False and；引号与局部变量改名；
 * 以及 /pawbot-ui CSRF 守卫的运行行为。
 *
 * 所有临时数据在 os.tmpdir() 下创建并清理。
 */

import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  rmdirSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import process from 'node:process'

import { createPawbotProxy } from '../lib/webui-proxy.js'
import { REQUIRED_FILES, REPORT_SCHEMA } from '../scripts/architecture-baseline.mjs'

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SCRIPT = join(REPO_ROOT, 'scripts', 'architecture-baseline.mjs')
const SKIP = Symbol('skip')
const CHECK_IDS = [
  'files.required',
  'package.contract',
  'entry.node.index',
  'entry.node.client',
  'entry.node.bili-watch',
  'entry.python.run',
  'entry.relative-imports',
  'routes.pawbot-ui.subpaths',
  'routes.pawbot-ui.methods',
  'bridge.tool.constants',
  'bridge.tool.allowlist-limits',
  'bridge.tool.python-client',
  'bridge.brain.protocol',
  'python.config.default',
  'python.config.source',
  'python.config.wiring',
  'python.webui.endpoints.get',
  'python.webui.endpoints.post',
  'python.webui.security-static',
]

const tempRoots = new Set()
let passed = 0
let failed = 0
let skipped = 0

function assert(condition, message) {
  if (!condition) throw new Error(message || 'assertion failed')
}

function assertEqual(actual, expected, label) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) throw new Error(`${label || 'value'} mismatch:\n  expected ${e}\n  actual   ${a}`)
}

function check(name, fn) {
  try {
    const result = fn()
    if (result === SKIP) {
      skipped += 1
      console.log(`  skip ${name}`)
    } else {
      passed += 1
      console.log(`  ok   ${name}`)
    }
  } catch (error) {
    failed += 1
    console.error(`  FAIL ${name}`)
    console.error(`       ${String(error?.stack || error?.message || error).replace(/\n/g, '\n       ')}`)
  }
}

function makeFixture() {
  const root = mkdtempSync(join(tmpdir(), 'pawbot-arch-baseline-'))
  tempRoots.add(root)
  try {
    for (const rel of REQUIRED_FILES) {
      const destination = join(root, rel)
      mkdirSync(dirname(destination), { recursive: true })
      cpSync(join(REPO_ROOT, rel), destination)
    }
    // 入口的直接相对依赖（含 lib/napcat.js、bili-watch/lib/*）也复制进来，
    // 让 relative-imports 检查在夹具里与真实仓库同构。
    cpSync(join(REPO_ROOT, 'lib'), join(root, 'lib'), { recursive: true, force: true })
    cpSync(join(REPO_ROOT, 'bili-watch', 'lib'), join(root, 'bili-watch', 'lib'), {
      recursive: true,
      force: true,
    })
    mkdirSync(join(root, 'data'), { recursive: true })
    mkdirSync(join(root, 'app', 'data'), { recursive: true })
    mkdirSync(join(root, 'reports'), { recursive: true })
    writeFileSync(join(root, 'data', 'sentinel.txt'), 'data sentinel\n', 'utf8')
    writeFileSync(join(root, 'app', 'data', 'sentinel.txt'), 'app data sentinel\n', 'utf8')
    return root
  } catch (error) {
    tempRoots.delete(root)
    rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 })
    throw error
  }
}

function dispose(root) {
  tempRoots.delete(root)
  rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 })
}

function withFixture(fn) {
  const root = makeFixture()
  try {
    return fn(root)
  } finally {
    dispose(root)
  }
}

function run(args, options = {}) {
  return spawnSync(process.execPath, [SCRIPT, ...args], {
    cwd: options.cwd || REPO_ROOT,
    encoding: 'utf8',
    windowsHide: true,
    timeout: 120000,
    env: { ...process.env, NODE_OPTIONS: '', NO_COLOR: '1' },
  })
}

function runJson(args, options = {}) {
  const result = run([...args, '--json'], options)
  try {
    return { result, report: JSON.parse(result.stdout) }
  } catch {
    throw new Error(
      `stdout 不是合法 JSON（exit=${result.status}）\nstdout: ${result.stdout}\nstderr: ${result.stderr}`,
    )
  }
}

function checkById(report, id) {
  return report.checks.find((item) => item.id === id)
}

function assertCheckOk(report, id) {
  const item = checkById(report, id)
  assert(item, `报告缺少检查项 ${id}`)
  assert(item.ok, `检查项 ${id} 应通过：${JSON.stringify(item)}`)
  return item
}

function assertCheckFailed(report, id) {
  const item = checkById(report, id)
  assert(item, `报告缺少检查项 ${id}`)
  assert(!item.ok, `检查项 ${id} 应失败：${JSON.stringify(item)}`)
  return item
}

function assertDataUntouched(root) {
  assertEqual(readFileSync(join(root, 'data', 'sentinel.txt'), 'utf8'), 'data sentinel\n', 'data sentinel')
  assertEqual(
    readFileSync(join(root, 'app', 'data', 'sentinel.txt'), 'utf8'),
    'app data sentinel\n',
    'app/data sentinel',
  )
  assertEqual(readdirSync(join(root, 'data')).sort(), ['sentinel.txt'], 'data 目录文件')
  assertEqual(readdirSync(join(root, 'app', 'data')).sort(), ['sentinel.txt'], 'app/data 目录文件')
}

function write(root, rel, text) {
  const path = join(root, rel)
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, text, 'utf8')
  return path
}

function read(root, rel) {
  return readFileSync(join(root, rel), 'utf8')
}

function replaceIn(root, rel, from, to) {
  const text = read(root, rel)
  if (from instanceof RegExp) {
    from.lastIndex = 0
    assert(from.test(text), `${rel} 不匹配待替换表达式：${from}`)
    from.lastIndex = 0
  } else {
    assert(text.includes(from), `${rel} 不包含待替换片段：${from}`)
  }
  const next = text.replace(from, to)
  assert(next !== text, `${rel} 替换没有产生变化`)
  write(root, rel, next)
  return next
}

function assertSentinels(root) {
  assertEqual(readFileSync(join(root, 'data', 'sentinel.txt'), 'utf8'), 'data sentinel\n', 'data sentinel')
  assertEqual(
    readFileSync(join(root, 'app', 'data', 'sentinel.txt'), 'utf8'),
    'app data sentinel\n',
    'app/data sentinel',
  )
}

process.on('exit', () => {
  for (const root of tempRoots) {
    try {
      rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 })
    } catch {
      /* 退出清理失败不改变测试结果 */
    }
  }
})

function makeRes() {
  const state = { status: 0, headers: null, body: '' }
  return {
    state,
    writeHead(status, headers) {
      state.status = status
      state.headers = headers
    },
    end(body) {
      state.body = body === undefined ? '' : String(body)
    },
  }
}

function makeProxy(overrides = {}) {
  let restartCalls = 0
  let fetchCalls = 0
  const proxy = createPawbotProxy({
    pawbotFetch: async () => {
      fetchCalls += 1
      return { status: 200, payload: { ok: true, data: {} } }
    },
    processInfo: () => ({ running: false }),
    restart: async () => {
      restartCalls += 1
      return { ok: true }
    },
    getOrigin: () => 'http://127.0.0.1:1234',
    log: () => {},
    ...overrides,
  })
  return { proxy, calls: () => ({ restartCalls, fetchCalls }) }
}

console.log('== 成功路径 ==')

check('--help 退出 0 并列出参数/安全说明', () => {
  const result = run(['--help'])
  assertEqual(result.status, 0, 'exit code')
  for (const needle of ['--root', '--json', '--out', '--verbose', '新建 .json', '拒绝覆盖', '退出码']) {
    assert(result.stdout.includes(needle), `--help 缺少 ${needle}`)
  }
  assertEqual(result.stderr, '', 'stderr')
})

check('--json：19/19 通过、退出 0、数据目录不被写', () => {
  withFixture((root) => {
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 0, 'exit code')
    assertEqual(result.stderr, '', 'stderr')
    assertEqual(report.schema, REPORT_SCHEMA, 'schema')
    assertEqual(report.root, resolve(root), 'root')
    assertEqual(report.ok, true, 'ok')
    assertEqual(report.summary, { total: 19, passed: 19, failed: 0 }, 'summary')
    assertEqual(report.checks.map((item) => item.id), CHECK_IDS, 'check ids')
    for (const id of CHECK_IDS) assertCheckOk(report, id)
    assertDataUntouched(root)
  })
})

check('默认 cwd 可用', () => {
  withFixture((root) => {
    const { result, report } = runJson([], { cwd: root })
    assertEqual(result.status, 0, 'exit code')
    assertEqual(report.root, resolve(root), 'root')
    assertEqual(report.ok, true, 'ok')
  })
})

check('人类输出与 --verbose', () => {
  withFixture((root) => {
    const plain = run(['--root', root])
    assertEqual(plain.status, 0, 'plain exit')
    assert(plain.stdout.includes('结果: PASS'), '缺少 PASS 汇总')
    assert(plain.stdout.includes('[PASS] files.required'), '缺少检查行')
    assert(!plain.stdout.includes('expected:'), '非 verbose 不应输出 expected')
    const verbose = run(['--root', root, '--verbose'])
    assertEqual(verbose.status, 0, 'verbose exit')
    assert(verbose.stdout.includes('expected:') && verbose.stdout.includes('actual:'), 'verbose 缺少 expected/actual')
  })
})

check('--json --out 新建报告，内容与 stdout 一致', () => {
  withFixture((root) => {
    const out = join(root, 'reports', 'baseline-report.json')
    const result = run(['--root', root, '--json', '--out', out])
    assertEqual(result.status, 0, 'exit code')
    assert(existsSync(out), '报告不存在')
    assertEqual(JSON.parse(readFileSync(out, 'utf8')), JSON.parse(result.stdout), '文件与 stdout')
    assert(!result.stdout.includes('PawBot DSH 架构基线检查'), 'stdout 混入人类输出')
    assertDataUntouched(root)
  })
})

check('--root= / --out= 等号形式可用', () => {
  withFixture((root) => {
    const out = join(root, 'reports', 'baseline-equals.json')
    const result = run([`--root=${root}`, `--out=${out}`])
    assertEqual(result.status, 0, 'exit code')
    assert(existsSync(out), '报告不存在')
    assert(result.stdout.includes('报告已写入:'), '人类输出缺少报告路径')
  })
})

console.log('\n== --out 保守策略 ==')

check('拒绝覆盖已有普通文件，内容不变', () => {
  withFixture((root) => {
    const target = join(root, 'reports', 'existing.json')
    writeFileSync(target, 'sentinel-report\n', 'utf8')
    const result = run(['--root', root, '--out', target])
    assertEqual(result.status, 2, 'exit code')
    assert(result.stderr.includes('拒绝覆盖已有文件'), `stderr=${result.stderr}`)
    assertEqual(readFileSync(target, 'utf8'), 'sentinel-report\n', '已有文件内容')
    assertDataUntouched(root)
  })
})

check('package.json 目标始终拒绝（存在/缺失/reports 内）', () => {
  withFixture((root) => {
    const before = read(root, 'package.json')
    const existing = run(['--root', root, '--out', join(root, 'package.json')])
    assertEqual(existing.status, 2, 'existing exit')
    assert(existing.stderr.includes('package.json'), `stderr=${existing.stderr}`)
    assertEqual(read(root, 'package.json'), before, 'package.json 内容')

    rmSync(join(root, 'package.json'))
    const missing = run(['--root', root, '--out', join(root, 'package.json')])
    assertEqual(missing.status, 2, 'missing exit')
    assert(missing.stderr.includes('package.json'), `stderr=${missing.stderr}`)
    assert(!existsSync(join(root, 'package.json')), '缺失的 package.json 不应被创建')

    const inReports = run(['--root', root, '--out', join(root, 'reports', 'package.json')])
    assertEqual(inReports.status, 2, 'reports/package.json exit')
    assert(inReports.stderr.includes('package.json'), `stderr=${inReports.stderr}`)
    assert(!existsSync(join(root, 'reports', 'package.json')), 'reports/package.json 不应被创建')
  })
})

check('root 内只允许 reports/ 下的新报告', () => {
  withFixture((root) => {
    const direct = join(root, 'not-reports.json')
    const first = run(['--root', root, '--out', direct])
    assertEqual(first.status, 2, 'direct exit')
    assert(first.stderr.includes('<root>/reports'), `stderr=${first.stderr}`)
    assert(!existsSync(direct), 'root 下非 reports 目标不应被创建')

    const traversal = join(root, 'reports', '..', 'not-reports.json')
    const second = run(['--root', root, '--out', traversal])
    assertEqual(second.status, 2, 'traversal exit')
    assert(!existsSync(direct), '.. 回溯不应写入 root')
  })
})

check('只允许 .json、父目录必须存在', () => {
  withFixture((root) => {
    const nonJson = run(['--root', root, '--out', join(root, 'reports', 'report.txt')])
    assertEqual(nonJson.status, 2, 'non-json exit')
    assert(nonJson.stderr.includes('只允许新建 .json'), `stderr=${nonJson.stderr}`)
    assert(!existsSync(join(root, 'reports', 'report.txt')), '不应创建 .txt')

    const missingParent = run(['--root', root, '--out', join(root, 'reports', 'missing', 'report.json')])
    assertEqual(missingParent.status, 2, 'missing parent exit')
    assert(missingParent.stderr.includes('父目录不存在'), `stderr=${missingParent.stderr}`)
    assert(!existsSync(join(root, 'reports', 'missing')), '不应创建父目录')
  })
})

check('--out --json 不被吞掉，按缺值报错', () => {
  withFixture((root) => {
    const result = run(['--root', root, '--out', '--json'])
    assertEqual(result.status, 2, 'exit code')
    assert(result.stderr.includes('--out 需要一个文件参数'), `stderr=${result.stderr}`)
    assertEqual(result.stdout, '', 'stdout 不应输出 JSON 报告')
  })
})

check('同一路径第二次运行被拒绝，报告不被覆盖', () => {
  withFixture((root) => {
    const out = join(root, 'reports', 'once.json')
    const first = run(['--root', root, '--json', '--out', out])
    assertEqual(first.status, 0, 'first exit')
    const before = readFileSync(out, 'utf8')
    const second = run(['--root', root, '--json', '--out', out])
    assertEqual(second.status, 2, 'second exit')
    assert(second.stderr.includes('拒绝覆盖已有文件'), `stderr=${second.stderr}`)
    assertEqual(readFileSync(out, 'utf8'), before, '报告内容')
  })
})

console.log('\n== 负例：缺文件 / 契约漂移 ==')

check('缺关键文件 → files.required 失败', () => {
  withFixture((root) => {
    rmSync(join(root, 'app', 'pawbot', 'webui.py'))
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'files.required')
    assert(String(item.actual).includes('app/pawbot/webui.py'), `actual=${item.actual}`)
  })
})

check('入口相对依赖 hostdeps 缺失 → relative-imports 失败', () => {
  withFixture((root) => {
    rmSync(join(root, 'lib', 'hostdeps.js'))
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'entry.relative-imports')
    assert(item.actual.missing.includes('index.js -> ./lib/hostdeps.js'), `missing=${JSON.stringify(item.actual.missing)}`)
    assert(item.actual.checked.includes('bili-watch/index.js -> ./lib/hostdeps.js'), 'bili-watch hostdeps 应被检查')
    assert(!item.actual.missing.includes('bili-watch/index.js -> ./lib/hostdeps.js'), 'bili-watch/lib/hostdeps.js 仍然存在')
  })
})

check('package engines 漂移 → package.contract 失败', () => {
  withFixture((root) => {
    const pkg = JSON.parse(read(root, 'package.json'))
    pkg.engines.node = '>=18'
    write(root, 'package.json', `${JSON.stringify(pkg, null, 2)}\n`)
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'package.contract')
    assertEqual(item.expected.node, '>=22', 'expected node')
    assertEqual(item.actual.node, '>=18', 'actual node')
  })
})

check('旧 /pawbot-ui 子路径漂移 → subpaths 失败', () => {
  withFixture((root) => {
    replaceIn(root, 'index.js', /'boot',\s*'boot\/status',/, "'boot',")
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'routes.pawbot-ui.subpaths')
    assert(!item.actual.includes('boot/status'), `actual=${JSON.stringify(item.actual)}`)
  })
})

check('代理 import 单双引号/空白变化仍通过', () => {
  withFixture((root) => {
    replaceIn(
      root,
      'index.js',
      "import { createPawbotProxy } from './lib/webui-proxy.js'",
      'import {createPawbotProxy} from "./lib/webui-proxy.js"',
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 0, 'exit code')
    assertCheckOk(report, 'routes.pawbot-ui.subpaths')
  })
})

check('只读桥路径漂移 → tool.constants 失败', () => {
  withFixture((root) => {
    replaceIn(
      root,
      'app/pawbot/dsh_bridge.py',
      'PATH = "/pawbot-dsh-bridge/execute"',
      'PATH = "/pawbot-dsh-bridge/run"',
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'bridge.tool.constants')
    assertEqual(item.actual.pyPath, '/pawbot-dsh-bridge/run', 'actual pyPath')
  })
})

check('Host 只读桥 exact 注册缺失 → tool.constants 失败', () => {
  withFixture((root) => {
    replaceIn(
      root,
      'index.js',
      "ctx.webServer.register({ kind: 'exact', path: BRIDGE_PATH, handler: bridge.dispatch })",
      '',
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'bridge.tool.constants')
    assertEqual(item.actual.hostExact, false, 'hostExact')
  })
})

check('大脑桥 turn 路由改成 prefix → brain.protocol 失败', () => {
  withFixture((root) => {
    replaceIn(
      root,
      'index.js',
      "ctx.webServer.register({ kind: 'exact', path: BRAIN_TURN_PATH, handler: brain.dispatchTurn })",
      "ctx.webServer.register({ kind: 'prefix', path: BRAIN_TURN_PATH, handler: brain.dispatchTurn })",
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'bridge.brain.protocol')
    assertEqual(item.actual.host['turn exact register'], false, 'turn exact')
  })
})

check('大脑桥 status 路由改成 prefix → brain.protocol 失败', () => {
  withFixture((root) => {
    replaceIn(
      root,
      'index.js',
      "ctx.webServer.register({ kind: 'exact', path: BRAIN_STATUS_PATH, handler: brain.dispatchStatus })",
      "ctx.webServer.register({ kind: 'prefix', path: BRAIN_STATUS_PATH, handler: brain.dispatchStatus })",
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'bridge.brain.protocol')
    assertEqual(item.actual.host['status exact register'], false, 'status exact')
  })
})

console.log('\n== 数值表达式 / DEFAULT / 端点解析 ==')

check('JS 限额写成 1024 * 1024 → 按值失败', () => {
  withFixture((root) => {
    replaceIn(root, 'lib/dsh-tool-bridge.js', 'responseBytes: 1024', 'responseBytes: 1024 * 1024')
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'bridge.tool.allowlist-limits')
    assertEqual(item.actual.responseBytes, 1048576, '实际值')
  })
})

check('Python 限额写成 1024 * 1024 → 按值失败', () => {
  withFixture((root) => {
    replaceIn(
      root,
      'app/pawbot/dsh_bridge.py',
      'MAX_RESPONSE_BYTES = 1024',
      'MAX_RESPONSE_BYTES = 1024 * 1024',
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'bridge.tool.allowlist-limits')
    assertEqual(item.actual.MAX_RESPONSE_BYTES, 1048576, '实际值')
  })
})

check('等价数值表达式 2 * 512 仍通过', () => {
  withFixture((root) => {
    replaceIn(root, 'lib/dsh-tool-bridge.js', 'responseBytes: 1024', 'responseBytes: 2 * 512')
    replaceIn(
      root,
      'app/pawbot/dsh_bridge.py',
      'MAX_RESPONSE_BYTES = 1024',
      'MAX_RESPONSE_BYTES = 2 * 512',
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 0, 'exit code')
    assertCheckOk(report, 'bridge.tool.allowlist-limits')
  })
})

check('DEFAULT 后续赋值 → config.default 失败', () => {
  withFixture((root) => {
    const source = `${read(root, 'app/pawbot/config.py')}\nDEFAULT["webui"] = None\n`
    write(root, 'app/pawbot/config.py', source)
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.config.default')
    assertEqual(item.actual.mutations, 1, 'mutations')
  })
})

check('DEFAULT 重复键 webui: None → config.default 失败', () => {
  withFixture((root) => {
    replaceIn(root, 'app/pawbot/config.py', /DEFAULT = \{\r?\n/, '$&    "webui": None,\n')
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.config.default')
    assert(String(item.error).includes('重复键'), `error=${item.error}`)
  })
})

check('第二次 DEFAULT = {} 赋值 → config.default 失败', () => {
  withFixture((root) => {
    const source = `${read(root, 'app/pawbot/config.py')}\nDEFAULT = {}\n`
    write(root, 'app/pawbot/config.py', source)
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.config.default')
    assertEqual(item.actual.assignments, 2, 'assignments')
  })
})

check('DEFAULT.minecraft 删除未登记键 → config.default 失败（整段冻结）', () => {
  withFixture((root) => {
    replaceIn(root, 'app/pawbot/config.py',
      /^        "game_chat_enabled": False,[^\r\n]*\r?\n/m, '')
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.config.default')
    assert(String(item.error).includes('键集合'), `error=${item.error}`)
  })
})

check('DEFAULT.minecraft 默认值漂移 → config.default 失败（整段冻结）', () => {
  withFixture((root) => {
    replaceIn(root, 'app/pawbot/config.py',
      '"tick_interval_sec": 3', '"tick_interval_sec": 4')
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.config.default')
    assert(String(item.error).includes('tick_interval_sec'), `error=${item.error}`)
  })
})

check('DEFAULT.minecraft 集合默认值非空 → config.default 失败（整段冻结）', () => {
  withFixture((root) => {
    replaceIn(root, 'app/pawbot/config.py',
      '"pvp_target_uuids": []',
      '"pvp_target_uuids": ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]')
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.config.default')
    assert(String(item.error).includes('pvp_target_uuids'), `error=${item.error}`)
  })
})

check('端点被注释/False and 吞掉 → POST 端点检查失败', () => {
  withFixture((root) => {
    replaceIn(
      root,
      'app/pawbot/webui.py',
      '        if path == "/api/send":',
      '        # if path == "/api/ghost":\n        if False and path == "/api/send":',
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.webui.endpoints.post')
    assert(item.actual.missing.includes('/api/send'), `missing=${JSON.stringify(item.actual.missing)}`)
    assert(!item.actual.paths.includes('/api/ghost'), '注释里的旧路径不应被当成端点')
  })
})

check('单双引号 + json.load/merge 同步改名 + 额外 deepcopy 仍通过', () => {
  withFixture((root) => {
    const replacements = [
      ['"server": {', "'server': {"],
      ['"webui": {', "'webui': {"],
      ['"host": "0.0.0.0"', "'host': '0.0.0.0'"],
      ['"port": 6199', "'port': 6199"],
      ['"path": "/ws"', "'path': '/ws'"],
      ['"access_token": ""', "'access_token': ''"],
      ['"host": "127.0.0.1"', "'host': '127.0.0.1'"],
      ['"port": 6185', "'port': 6185"],
      ['"token": ""', "'token': ''"],
      ['disk = json.load(f)', 'payload = json.load(f)'],
      ['_deep_merge(DEFAULT, disk)', '_deep_merge(DEFAULT, payload)'],
    ]
    let source = read(root, 'app/pawbot/config.py')
    for (const [from, to] of replacements) {
      assert(source.includes(from), `config.py 不包含待替换片段：${from}`)
      source = source.replace(from, to)
    }
    source += '\n_EXTRA_DEFAULT_COPY = deepcopy(DEFAULT)\n'
    write(root, 'app/pawbot/config.py', source)
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 0, 'exit code')
    assertCheckOk(report, 'python.config.default')
    assertCheckOk(report, 'python.config.source')
  })
})

check('只改 json.load 变量、不同步 _deep_merge → config.source 失败', () => {
  withFixture((root) => {
    replaceIn(root, 'app/pawbot/config.py', 'disk = json.load(f)', 'payload = json.load(f)')
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.config.source')
    assertEqual(item.actual.jsonLoadVars, ['payload'], 'load vars')
    assertEqual(item.actual.diskMergeVars, ['disk'], 'merge vars')
    assertEqual(item.actual.linkedVars, [], 'linked vars')
  })
})

check('注释伪合并 + 非合并赋值 → config.source 失败', () => {
  withFixture((root) => {
    replaceIn(
      root,
      'app/pawbot/config.py',
      'self.data = _deep_merge(DEFAULT, disk)',
      'self.data = disk  # self.data = _deep_merge(DEFAULT, disk)',
    )
    const { result, report } = runJson(['--root', root])
    assertEqual(result.status, 1, 'exit code')
    const item = assertCheckFailed(report, 'python.config.source')
    assertEqual(item.actual.diskMergeVars, [], '注释伪合并不应计入')
    assertEqual(item.actual.linkedVars, [], 'link vars')
  })
})

console.log('\n== 参数错误 ==')

check('未知参数 / 缺值 / 不存在根目录 → exit 2', () => {
  const unknown = run(['--definitely-unknown'])
  assertEqual(unknown.status, 2, 'unknown exit')
  assert(unknown.stderr.includes('未知参数'), 'unknown stderr')

  const rootMissing = run(['--root'])
  assertEqual(rootMissing.status, 2, 'root missing exit')
  assert(rootMissing.stderr.includes('--root 需要一个目录参数'), 'root missing stderr')

  const outMissing = run(['--out'])
  assertEqual(outMissing.status, 2, 'out missing exit')
  assert(outMissing.stderr.includes('--out 需要一个文件参数'), 'out missing stderr')

  const outDash = run(['--out', '-'])
  assertEqual(outDash.status, 2, 'out dash exit')
  assert(outDash.stderr.includes('不支持'), 'out dash stderr')

  const absent = join(tmpdir(), `pawbot-arch-absent-${process.pid}-${Date.now()}`)
  assert(!existsSync(absent), '测试目录意外存在')
  const badRoot = run(['--root', absent])
  assertEqual(badRoot.status, 2, 'bad root exit')
  assert(badRoot.stderr.includes('--root 不存在'), 'bad root stderr')
})

console.log('\n== --out 数据目录拒写 ==')

check('data / app/data / 嵌套 / .. 回溯拒绝且不创建报告', () => {
  withFixture((root) => {
    mkdirSync(join(root, 'data', 'nested'), { recursive: true })
    const targets = [
      join(root, 'data', 'report.json'),
      join(root, 'app', 'data', 'report.json'),
      join(root, 'data', 'nested', 'report.json'),
      join(root, 'app', 'data', '..', 'data', 'report.json'),
    ]
    for (const target of targets) {
      const result = run(['--root', root, '--json', '--out', target])
      assertEqual(result.status, 2, `exit for ${target}`)
      assert(!existsSync(target), `不应创建 ${target}`)
      assert(
        result.stderr.includes('拒绝写入报告') || result.stderr.includes('父目录不存在'),
        `stderr=${result.stderr}`,
      )
      assert(!result.stdout.includes('"schema"'), '拒写时不应输出 JSON 报告')
    }
    assertEqual(readdirSync(join(root, 'data', 'nested')), [], 'nested 目录应保持为空')
    assertSentinels(root)
  })
})

check('--out 指向已有 data 目录也拒绝', () => {
  withFixture((root) => {
    const result = run(['--root', root, '--out', join(root, 'data')])
    assertEqual(result.status, 2, 'exit code')
    assert(result.stderr.includes('--out 指向目录'), `stderr=${result.stderr}`)
    assertDataUntouched(root)
  })
})

check('父目录 junction 指向 data → 按 realpath 拒绝', () => {
  withFixture((root) => {
    const link = join(root, 'linked-data')
    try {
      symlinkSync(join(root, 'data'), link, 'junction')
    } catch {
      return SKIP
    }
    try {
      const result = run(['--root', root, '--out', join(link, 'report.json')])
      assertEqual(result.status, 2, 'exit code')
      assert(result.stderr.includes('拒绝写入报告'), `stderr=${result.stderr}`)
      assert(!existsSync(join(root, 'data', 'report.json')), '不应写入真实 data')
      assertDataUntouched(root)
    } finally {
      rmdirSync(link)
    }
    return undefined
  })
})

check('外部 app/data junction → 按真实数据根拒绝', () => {
  withFixture((root) => {
    const external = mkdtempSync(join(tmpdir(), 'pawbot-arch-external-'))
    tempRoots.add(external)
    writeFileSync(join(external, 'sentinel.txt'), 'external sentinel\n', 'utf8')
    rmSync(join(root, 'app', 'data'), { recursive: true, force: true })
    try {
      symlinkSync(external, join(root, 'app', 'data'), 'junction')
    } catch {
      tempRoots.delete(external)
      rmSync(external, { recursive: true, force: true })
      return SKIP
    }
    try {
      const result = run(['--root', root, '--out', join(external, 'report.json')])
      assertEqual(result.status, 2, 'exit code')
      assert(result.stderr.includes('拒绝写入报告'), `stderr=${result.stderr}`)
      assert(!existsSync(join(external, 'report.json')), '外部数据目录不应出现报告')
      assertEqual(readFileSync(join(external, 'sentinel.txt'), 'utf8'), 'external sentinel\n', '外部哨兵')
    } finally {
      rmdirSync(join(root, 'app', 'data'))
    }
    return undefined
  })
})

check('root 本身是 junction 链接 → 仍按真实数据根拒绝', () => {
  withFixture((root) => {
    const linkParent = mkdtempSync(join(tmpdir(), 'pawbot-arch-rootlink-'))
    tempRoots.add(linkParent)
    const link = join(linkParent, 'repo-link')
    try {
      symlinkSync(root, link, 'junction')
    } catch {
      tempRoots.delete(linkParent)
      rmSync(linkParent, { recursive: true, force: true })
      return SKIP
    }
    try {
      const result = run(['--root', link, '--out', join(link, 'data', 'report.json')])
      assertEqual(result.status, 2, 'exit code')
      assert(result.stderr.includes('拒绝写入报告'), `stderr=${result.stderr}`)
      assert(!existsSync(join(root, 'data', 'report.json')), '真实 data 不应出现报告')
      assertDataUntouched(root)
    } finally {
      rmdirSync(link)
    }
    return undefined
  })
})

check('叶子 file symlink → 拒绝；平台不支持创建时 skip', () => {
  withFixture((root) => {
    const link = join(root, 'reports', 'leaf-report.json')
    try {
      symlinkSync(join(root, 'data', 'sentinel.txt'), link, 'file')
    } catch {
      return SKIP
    }
    try {
      const result = run(['--root', root, '--out', link])
      assertEqual(result.status, 2, 'exit code')
      assert(result.stderr.includes('叶子符号链接'), `stderr=${result.stderr}`)
      assertEqual(readFileSync(join(root, 'data', 'sentinel.txt'), 'utf8'), 'data sentinel\n', 'sentinel')
    } finally {
      unlinkSync(link)
    }
    return undefined
  })
})

console.log('\n== 运行行为：/pawbot-ui CSRF 守卫 ==')

async function runBehaviorChecks() {
  const cases = []
  cases.push(['跨源 POST 被 403 拒绝且不调用 action', async () => {
    const { proxy, calls } = makeProxy()
    const res = makeRes()
    await proxy.dispatch({
      url: '/pawbot-ui/restart',
      method: 'POST',
      headers: {
        origin: 'http://evil.example',
        'sec-fetch-site': 'cross-site',
        'content-type': 'application/json',
      },
    }, res)
    assertEqual(res.state.status, 403, 'status')
    assert(res.state.body.includes('cross-origin denied'), `body=${res.state.body}`)
    assertEqual(calls().restartCalls, 0, 'restart calls')
  }])

  cases.push(['同源 POST 但 content-type 非 JSON → 415', async () => {
    const { proxy, calls } = makeProxy()
    const res = makeRes()
    await proxy.dispatch({
      url: '/pawbot-ui/restart',
      method: 'POST',
      headers: {
        origin: 'http://127.0.0.1:1234',
        'sec-fetch-site': 'same-origin',
        'content-type': 'text/plain',
      },
    }, res)
    assertEqual(res.state.status, 415, 'status')
    assertEqual(calls().restartCalls, 0, 'restart calls')
  }])

  cases.push(['同源 POST + JSON → 200 且调用 action', async () => {
    const { proxy, calls } = makeProxy()
    const res = makeRes()
    await proxy.dispatch({
      url: '/pawbot-ui/restart',
      method: 'POST',
      headers: {
        origin: 'http://127.0.0.1:1234',
        'sec-fetch-site': 'same-origin',
        'content-type': 'application/json',
      },
    }, res)
    assertEqual(res.state.status, 200, 'status')
    assertEqual(calls().restartCalls, 1, 'restart calls')
  }])

  cases.push(['跨源 GET Referer → 403 且不访问 PawBot', async () => {
    const { proxy, calls } = makeProxy()
    const res = makeRes()
    await proxy.dispatch({
      url: '/pawbot-ui/status',
      method: 'GET',
      headers: { referer: 'http://evil.example/' },
    }, res)
    assertEqual(res.state.status, 403, 'status')
    assertEqual(calls().fetchCalls, 0, 'fetch calls')
  }])

  cases.push(['拿不到 Host origin 时非 GET fail-closed → 403', async () => {
    const { proxy, calls } = makeProxy({ getOrigin: () => null })
    const res = makeRes()
    await proxy.dispatch({
      url: '/pawbot-ui/restart',
      method: 'POST',
      headers: {
        'sec-fetch-site': 'same-origin',
        'content-type': 'application/json',
      },
    }, res)
    assertEqual(res.state.status, 403, 'status')
    assertEqual(calls().restartCalls, 0, 'restart calls')
  }])

  for (const [name, fn] of cases) {
    try {
      await fn()
      passed += 1
      console.log(`  ok   ${name}`)
    } catch (error) {
      failed += 1
      console.error(`  FAIL ${name}`)
      console.error(`       ${String(error?.stack || error?.message || error).replace(/\n/g, '\n       ')}`)
    }
  }
}

await runBehaviorChecks()

console.log(`\n${passed} passed, ${failed} failed, ${skipped} skipped`)
process.exit(failed ? 1 : 0)
