// 离线测试：统一数据根（优先级 / 统一子路径 / 迁移复用 / 迁移失败回退 / 联接跟随）。
// 用法：node test/data-root-offline.mjs
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { realpathSync } from 'node:fs'
import fs from 'node:fs'
import { syncBuiltinESMExports } from 'node:module'
import * as guard from '../lib/data-guard.js'
import {
  DATA_ROOT_ENV,
  DATA_ROOT_SUBPATHS,
  dataPaths,
  ensureDataLayout,
  resolveDataRoot,
} from '../lib/data-root.js'

const inheritedPython = process.env.PAWBOT_PYTHON_EXE
delete process.env.PAWBOT_PYTHON_EXE
process.on('exit', () => {
  if (inheritedPython === undefined) delete process.env.PAWBOT_PYTHON_EXE
  else process.env.PAWBOT_PYTHON_EXE = inheritedPython
})

let passed = 0
let failed = 0
function check(name, run) {
  try {
    run()
    passed += 1
    console.log(`  ✔ ${name}`)
  } catch (error) {
    failed += 1
    console.error(`  ✘ ${name}\n      ${error?.message || error}`)
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed')
}
function tmp(name) {
  return mkdtempSync(join(tmpdir(), `pawbot-root-${name}-`))
}
function write(path, text) {
  mkdirSync(join(path, '..'), { recursive: true })
  writeFileSync(path, text, 'utf8')
}
function real(path) {
  try {
    return realpathSync(path)
  } catch {
    return resolve(path)
  }
}

console.log('== 优先级：Config > PAWBOT_DATA_DIR > app/data ==')

check('默认数据根 = <appDir>/data，source=default', () => {
  const app = tmp('default')
  const info = resolveDataRoot({ appDir: app, env: {} })
  assert(info.dataRoot === resolve(join(app, 'data')), `dataRoot=${info.dataRoot}`)
  assert(info.source === 'default', `source=${info.source}`)
  assert(info.external === false, '默认不应是外部目录')
  assert(info.requested === '', `requested=${info.requested}`)
  assert(!existsSync(join(app, 'data', 'memory')), 'resolve 不应急切创建子目录')
  assert(!existsSync(join(app, 'data', 'tmp')), 'resolve 不应急切创建 tmp')
})

check('Config 显式 dataDir 优先于环境变量', () => {
  const app = tmp('cfg')
  const fromConfig = tmp('cfg-config')
  const fromEnv = join(tmp('cfg-env-parent'), 'env-not-chosen')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-ORIGINAL"}}')
  const info = resolveDataRoot({
    appDir: app,
    configDataDir: fromConfig,
    env: { [DATA_ROOT_ENV]: fromEnv },
  })
  assert(info.source === 'config', `source=${info.source}`)
  assert(real(info.dataRoot) === real(fromConfig), `dataRoot=${info.dataRoot}`)
  assert(info.external === true, '应标记外部目录')
  assert(info.migrated === true, '应发生迁移')
  assert(guard.isLink(join(app, 'data')), 'app/data 应成为联接')
  assert(readFileSync(join(fromConfig, 'config.json'), 'utf8').includes('sk-ORIGINAL'), '数据已复制')
  assert(!existsSync(fromEnv), '未选中的环境变量目录不应被创建')
})

check('Config 空白时回退 PAWBOT_DATA_DIR', () => {
  const app = tmp('env')
  const ext = tmp('env-ext')
  write(join(app, 'data', 'persona.md'), '旧人设')
  const info = resolveDataRoot({
    appDir: app,
    configDataDir: '   ',
    env: { [DATA_ROOT_ENV]: ` ${ext} ` },
  })
  assert(info.source === 'env', `source=${info.source}`)
  assert(real(info.dataRoot) === real(ext), `dataRoot=${info.dataRoot}`)
  assert(readFileSync(join(ext, 'persona.md'), 'utf8') === '旧人设', '应迁移人设')
})

check('dataGuard 关闭时忽略 Config/环境变量，仍用 app/data', () => {
  const app = tmp('disabled')
  const ext = tmp('disabled-ext')
  const info = resolveDataRoot({
    appDir: app,
    configDataDir: ext,
    env: { [DATA_ROOT_ENV]: ext },
    enabled: false,
  })
  assert(info.dataRoot === resolve(join(app, 'data')), `dataRoot=${info.dataRoot}`)
  assert(info.external === false, '关闭时不应外部化')
  assert(!existsSync(join(app, 'data')), '关闭时不应迁移/建联接')
})

console.log('== 统一子路径 ==')

check('dataPaths 暴露固定子路径且都在根下', () => {
  const root = tmp('paths')
  const p = dataPaths(root)
  const expected = {
    config: 'config.json',
    persona: 'persona.md',
    memory: 'memory',
    backups: 'backups',
    brain: 'brain',
    napcat: 'napcat',
    logs: 'logs',
    authz: 'authz',
    sessions: 'sessions',
    tmp: 'tmp',
    runtimeLock: '.runtime.lock',
  }
  assert(JSON.stringify(DATA_ROOT_SUBPATHS) === JSON.stringify(expected), 'DATA_ROOT_SUBPATHS 契约漂移')
  for (const [name, rel] of Object.entries(expected)) {
    assert(p[name] === join(resolve(root), rel), `${name}=${p[name]} 应为 ${join(root, rel)}`)
  }
  assert(p.root === resolve(root), `root=${p.root}`)
})

check('ensureDataLayout 只建目录、幂等', () => {
  const root = tmp('layout')
  const first = ensureDataLayout(root)
  assert(first.created.length === 8, `首次创建 ${first.created.length} 个目录`)
  for (const name of ['memory', 'backups', 'brain', 'napcat', 'logs', 'authz', 'sessions', 'tmp']) {
    assert(existsSync(join(root, name)), `${name} 应存在`)
  }
  assert(!existsSync(join(root, 'config.json')), '不应凭空创建 config.json')
  assert(!existsSync(join(root, 'persona.md')), '不应凭空创建 persona.md')
  const second = ensureDataLayout(root)
  assert(second.created.length === 0, `第二次 created=${second.created.length}`)
})

console.log('== 迁移复用 / 失败回退 ==')

check('迁移后 DataRoot 指向外部，app/data 保留联接', () => {
  const app = tmp('migrate')
  const ext = tmp('migrate-ext')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-MINE"}}')
  write(join(app, 'data', 'memory', 'm1.json'), '["记忆"]')
  const info = resolveDataRoot({ appDir: app, configDataDir: ext, env: {} })
  assert(info.dataRoot === resolve(ext), `dataRoot=${info.dataRoot}`)
  assert(info.external && info.migrated, '应 external+migrated')
  assert(guard.isLink(join(app, 'data')), 'app/data 应是联接')
  assert(readFileSync(join(ext, 'memory', 'm1.json'), 'utf8') === '["记忆"]', '记忆已迁移')
})

check('干净安装：app/data 与外部目录都不存在 → 建外部目录并联接', () => {
  const app = tmp('clean-install')
  const ext = join(tmp('clean-install-parent'), 'new-external')
  assert(!existsSync(join(app, 'data')), '前提：app/data 不存在')
  assert(!existsSync(ext), '前提：外部目录不存在')
  const info = resolveDataRoot({ appDir: app, configDataDir: ext, env: {} })
  assert(info.migrated === true, JSON.stringify(info))
  assert(info.external === true, JSON.stringify(info))
  assert(real(info.dataRoot) === real(ext), `dataRoot=${info.dataRoot}`)
  assert(guard.isLink(join(app, 'data')), 'app/data 应成为联接')
  assert(existsSync(ext), '外部目录应被创建')
})

check('迁移失败（外部路径是文件）→ 回退 app/data、保留原数据、给出 warnings', () => {
  const app = tmp('fail')
  const externalFile = join(tmp('fail-ext'), 'not-a-dir')
  write(externalFile, 'x')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-KEEP"}}')
  write(join(app, 'data', 'persona.md'), '人设保留')
  const info = resolveDataRoot({ appDir: app, configDataDir: externalFile, env: {} })
  assert(real(info.dataRoot) === real(join(app, 'data')), `迁移失败应用 app/data，实际 ${info.dataRoot}`)
  assert(info.external === false, '失败时不应标记外部')
  assert(info.warnings.length > 0, '失败必须有 warnings')
  assert(!guard.isLink(join(app, 'data')), '失败时 app/data 仍是真目录')
  assert(readFileSync(join(app, 'data', 'config.json'), 'utf8').includes('sk-KEEP'), '原数据不能丢')
  assert(readFileSync(join(app, 'data', 'persona.md'), 'utf8') === '人设保留', '人设不能丢')
})

check('app/data 已联接到别的目录 → 跟随实际联接目标，不按 Config 分裂', () => {
  const app = tmp('link-other')
  const actual = tmp('link-actual')
  const wanted = tmp('link-wanted')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-ACTUAL"}}')
  const prepared = guard.prepareData({ appDir: app, dataDir: actual })
  assert(prepared.external, '准备阶段应迁移到 actual')
  const info = resolveDataRoot({ appDir: app, configDataDir: wanted, env: {} })
  assert(real(info.dataRoot) === real(actual), `应跟随实际联接 ${actual}，实际 ${info.dataRoot}`)
  assert(info.external === true, '跟随联接时应是外部目录')
  assert(info.warnings.some((w) => w.includes('不一致')), `warnings=${JSON.stringify(info.warnings)}`)
  assert(!guard.isLink(join(actual, 'data')), '不应擅自改动实际目标')
})

check('Config dataDir 恰好等于 app/data 时不做自迁移', () => {
  const app = tmp('self')
  write(join(app, 'data', 'config.json'), '{"a":1}')
  const info = resolveDataRoot({
    appDir: app,
    configDataDir: join(app, 'data'),
    env: {},
  })
  assert(real(info.dataRoot) === real(join(app, 'data')), `dataRoot=${info.dataRoot}`)
  assert(info.migrated === false, '不应迁移')
  assert(!guard.isLink(join(app, 'data')), '不应把 app/data 变成联接')
  assert(readFileSync(join(app, 'data', 'config.json'), 'utf8') === '{"a":1}', '数据不变')
})


console.log('== 活运行锁期间不得迁移/写快照 ==')

check('活运行锁（旧时间戳）阻止迁移与版本快照，原数据与锁不变', () => {
  const app = tmp('live-lock')
  const ext = join(tmp('live-lock-ext-parent'), 'ext-not-created')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{"llm":{"api_key":"sk-LIVE"}}')
  write(join(appData, 'persona.md'), '活人设')
  const lockPath = join(appData, '.runtime.lock')
  const lock = {
    pid: process.pid,
    started_at: '2020-01-01T00:00:00.000Z',
    host_pid: 0,
    version: 'running',
    nonce: 'a'.repeat(32),
  }
  writeFileSync(lockPath, JSON.stringify(lock), 'utf8')
  const before = readFileSync(lockPath, 'utf8')
  const info = resolveDataRoot({ appDir: app, configDataDir: ext, env: {}, version: '9.9.9' })
  assert(real(info.dataRoot) === real(appData), `活锁根不得被迁走：${info.dataRoot}`)
  assert(info.external === false && info.migrated === false, JSON.stringify(info))
  assert(!guard.isLink(appData), 'app/data 仍应是真目录')
  assert(!existsSync(join(appData, 'backups')), '活锁期间不得写版本快照')
  assert(!existsSync(ext), '活锁期间不应创建/迁移外部目录')
  assert(readFileSync(lockPath, 'utf8') === before, '运行锁内容不得被改动/擦除')
  assert(info.warnings.some((w) => w.includes('运行锁')), `warnings=${JSON.stringify(info.warnings)}`)
})

check('明确死亡的 stale 锁不阻止迁移', () => {
  const app = tmp('stale-lock')
  const ext = tmp('stale-lock-ext')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{"llm":{"api_key":"sk-STALE"}}')
  writeFileSync(join(appData, '.runtime.lock'), JSON.stringify({
    pid: 999999,
    started_at: '2020-01-01T00:00:00.000Z',
    host_pid: 0,
    version: 'crashed',
    nonce: 'b'.repeat(32),
  }), 'utf8')
  const info = resolveDataRoot({ appDir: app, configDataDir: ext, env: {} })
  assert(info.migrated === true, JSON.stringify(info))
  assert(guard.isLink(appData), 'stale 锁不应阻止迁移成功')
  assert(readFileSync(join(ext, 'config.json'), 'utf8').includes('sk-STALE'), '数据已迁移')
})

console.log('== RACE-1：改名窗口出现活运行锁（端到端 resolveDataRoot） ==')

check('改名时被发布活运行锁 → 回退 app/data、活锁不被搬走、外部副本保留', () => {
  const app = tmp('race-root')
  const ext = tmp('race-root-ext')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{"llm":{"api_key":"sk-RACE"}}')
  write(join(appData, 'brain', 'kept.txt'), 'keep-me')
  const racer = {
    pid: process.pid,
    started_at: new Date().toISOString(),
    host_pid: 0,
    version: 'racer',
    nonce: 'e'.repeat(32),
  }
  const original = fs.renameSync
  fs.renameSync = (src, dst) => {
    if (resolve(src) === resolve(appData)) {
      writeFileSync(join(appData, '.runtime.lock'), JSON.stringify(racer), 'utf8')
    }
    return original(src, dst)
  }
  syncBuiltinESMExports()
  let info
  try {
    info = resolveDataRoot({ appDir: app, configDataDir: ext, env: {} })
  } finally {
    fs.renameSync = original
    syncBuiltinESMExports()
  }
  assert(info.migrated === false, `活锁随目录搬走时不得报告 migrated=true：${JSON.stringify(info)}`)
  assert(info.external === false, `活锁随目录搬走时不得 external=true：${JSON.stringify(info)}`)
  assert(real(info.dataRoot) === real(appData), `dataRoot 应回退 app/data，实际 ${info.dataRoot}`)
  assert(!guard.isLink(appData), 'app/data 应恢复为真目录')
  assert(readFileSync(join(appData, 'config.json'), 'utf8').includes('sk-RACE'), '原 config 不能丢')
  assert(readFileSync(join(appData, 'brain', 'kept.txt'), 'utf8') === 'keep-me', '原 brain 数据不能丢')
  const after = JSON.parse(readFileSync(join(appData, '.runtime.lock'), 'utf8'))
  assert(after.nonce === racer.nonce, '竞争者的活锁不得被删除/替换')
  assert(existsSync(join(ext, 'config.json')), '外部已复制副本应保留（失败不删数据）')
  assert(info.warnings.some((w) => w.includes('运行锁')), `warnings=${JSON.stringify(info.warnings)}`)
})

console.log('== 版本快照兼容 ==')

check('resolveDataRoot 传入 version 仍写版本快照与 marker', () => {
  const app = tmp('snap')
  write(join(app, 'data', 'config.json'), '{}')
  write(join(app, 'data', 'persona.md'), '人设')
  const info = resolveDataRoot({ appDir: app, env: {}, version: '9.9.9', keep: 3 })
  assert(info.snapshot && existsSync(info.snapshot), `snapshot=${info.snapshot}`)
  assert(existsSync(join(info.snapshot, 'config.json')), '快照含 config')
  const marker = JSON.parse(readFileSync(join(app, 'data', '.data-guard.json'), 'utf8'))
  assert(marker.version === '9.9.9', `marker=${marker.version}`)
})

check('旧 prepareData 签名保持不变（迁移 + 快照字段齐全）', () => {
  const app = tmp('compat')
  const ext = tmp('compat-ext')
  write(join(app, 'data', 'config.json'), '{}')
  const info = guard.prepareData({ appDir: app, dataDir: ext, version: '1.2.3', keep: 2 })
  for (const key of ['enabled', 'dataDir', 'external', 'migrated', 'snapshot', 'warnings', 'version']) {
    assert(key in info, `prepareData 缺少字段 ${key}`)
  }
  assert(info.external === true && info.migrated === true, '旧签名迁移行为不变')
  assert(info.version === '1.2.3', 'version 回传不变')
  assert(info.snapshot && info.snapshot.startsWith(ext), '快照落在外部目录')
})

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
