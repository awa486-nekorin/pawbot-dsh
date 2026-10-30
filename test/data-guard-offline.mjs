// 离线测试：数据守卫（版本快照 / 外部 data 目录迁移 / 联接 / 备份列表）。
// 用法：node test/data-guard-offline.mjs
import fs from 'node:fs'
import { spawnSync } from 'node:child_process'
import { syncBuiltinESMExports } from 'node:module'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as guard from '../lib/data-guard.js'
import { acquireRuntimeLock, inspectRuntimeLock } from '../lib/instance-lock.js'

const WORKER = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'runtime-lock-worker.mjs')

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
  return mkdtempSync(join(tmpdir(), `pawbot-guard-${name}-`))
}
function write(path, text) {
  mkdirSync(join(path, '..'), { recursive: true })
  writeFileSync(path, text, 'utf8')
}

console.log('== 版本快照 ==')
check('首次加载（版本标记缺失）会快照一次', () => {
  const app = tmp('app')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-old"}}')
  write(join(app, 'data', 'persona.md'), '我是大肥鱼。')
  write(join(app, 'data', 'memory', 'a.json'), '[]')
  const info = guard.prepareData({ appDir: app, version: '9.9.9' })
  assert(info.snapshot, '应该有 snapshot')
  const dir = info.snapshot
  assert(existsSync(join(dir, 'config.json')), 'config 快照')
  assert(existsSync(join(dir, 'persona.md')), 'persona 快照')
  assert(existsSync(join(dir, 'memory', 'a.json')), 'memory 快照')
  const marker = JSON.parse(readFileSync(join(app, 'data', '.data-guard.json'), 'utf8'))
  assert(marker.version === '9.9.9', 'marker 版本')
})

check('同版本重复加载不再快照', () => {
  const app = tmp('app2')
  write(join(app, 'data', 'config.json'), '{}')
  guard.prepareData({ appDir: app, version: '1.0.0' })
  const second = guard.prepareData({ appDir: app, version: '1.0.0' })
  assert(!second.snapshot, '同版本不应再快照')
  assert(guard.listBackups(join(app, 'data')).length === 1, '只有 1 份')
})

check('版本变化会新增快照，并只保留 keep 份', () => {
  const app = tmp('app3')
  write(join(app, 'data', 'config.json'), '{}')
  for (const v of ['1.0.0', '2.0.0', '3.0.0']) {
    guard.prepareData({ appDir: app, version: v, keep: 2 })
  }
  const backups = guard.listBackups(join(app, 'data'))
  assert(backups.length === 2, `应保留 2 份，实际 ${backups.length}`)
})

console.log('== 外部 data 目录 ==')
check('迁移：旧 data 复制到外部，app/data 变成联接', () => {
  const app = tmp('app4')
  const ext = tmp('ext4')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-MINE"}}')
  write(join(app, 'data', 'persona.md'), '旧人设。')
  write(join(app, 'data', 'memory', 'm1.json'), '["记忆"]')
  const info = guard.prepareData({ appDir: app, dataDir: ext, version: '1.0.0' })
  assert(info.external, '应标记为外部目录')
  assert(guard.isLink(join(app, 'data')), 'app/data 应是联接/符号链接')
  assert(existsSync(join(ext, 'config.json')), '外部有 config')
  assert(readFileSync(join(ext, 'config.json'), 'utf8').includes('sk-MINE'), '外部保留原 key')
  assert(existsSync(join(ext, 'memory', 'm1.json')), '外部有记忆')
})

check('迁移不覆盖外部已有文件（外部优先）', () => {
  const app = tmp('app5')
  const ext = tmp('ext5')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-OLD"}}')
  write(join(ext, 'config.json'), '{"llm":{"api_key":"sk-NEW"}}')
  guard.prepareData({ appDir: app, dataDir: ext, version: '1.0.0' })
  assert(readFileSync(join(ext, 'config.json'), 'utf8').includes('sk-NEW'), '外部文件不能被旧模板覆盖')
})

check('插件重装（app/data 又变回真目录）后仍用外部数据，不丢 key', () => {
  const app = tmp('app6')
  const ext = tmp('ext6')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-FIRST"}}')
  guard.prepareData({ appDir: app, dataDir: ext, version: '1.0.0' })
  // 模拟升级把 app 整个替换：删掉 app/data（联接本身），重新放一个模板 data
  rmSync(join(app, 'data'), { recursive: true, force: true })
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":""}}')
  write(join(app, 'data', 'persona.md'), '模板人设。')
  const info = guard.prepareData({ appDir: app, dataDir: ext, version: '2.0.0' })
  assert(info.external, '仍应是外部目录')
  const cfg = readFileSync(join(ext, 'config.json'), 'utf8')
  assert(cfg.includes('sk-FIRST'), `外部 key 应保留，实际 ${cfg}`)
  assert(guard.isLink(join(app, 'data')), 'app/data 应重建为联接')
})

check('外部目录下版本快照也落在外部', () => {
  const app = tmp('app7')
  const ext = tmp('ext7')
  write(join(app, 'data', 'persona.md'), 'v1')
  guard.prepareData({ appDir: app, dataDir: ext, version: '1.0.0' })
  const info = guard.prepareData({ appDir: app, dataDir: ext, version: '2.0.0' })
  assert(info.snapshot && info.snapshot.startsWith(ext), '快照应在外部目录里')
})


function makeDirLink(linkPath, target) {
  try {
    mkdirSync(join(linkPath, '..'), { recursive: true })
    if (process.platform === 'win32') {
      spawnSync('cmd.exe', ['/c', 'mklink', '/J', linkPath, target],
        { windowsHide: true, encoding: 'utf8' })
    } else {
      fs.symlinkSync(target, linkPath, 'dir')
    }
    return existsSync(linkPath)
      && fs.realpathSync(linkPath) === fs.realpathSync(target)
  } catch {
    return false
  }
}

function withBrokenRename(appData, run) {
  const original = fs.renameSync
  fs.renameSync = (src, dst) => {
    if (resolve(src) === resolve(appData)) {
      const error = new Error('EBUSY: simulated locked directory')
      error.code = 'EBUSY'
      throw error
    }
    return original(src, dst)
  }
  syncBuiltinESMExports()
  try {
    return run()
  } finally {
    fs.renameSync = original
    syncBuiltinESMExports()
  }
}

/** RACE-1：在源目录真正 rename 之前，注入「另一个实例刚发布活运行锁」。 */
function withInjectedRename(appData, inject, run) {
  const original = fs.renameSync
  fs.renameSync = (src, dst) => {
    if (resolve(src) === resolve(appData)) inject(src, dst)
    return original(src, dst)
  }
  syncBuiltinESMExports()
  try {
    return run()
  } finally {
    fs.renameSync = original
    syncBuiltinESMExports()
  }
}

/** 在第一次 copyFileSync 时执行观察/注入（copy 是迁移最长的竞态窗口）。 */
function withCopyHook(hook, run) {
  const original = fs.copyFileSync
  let first = true
  fs.copyFileSync = (src, dst) => {
    if (first) {
      first = false
      hook(src, dst)
    }
    return original(src, dst)
  }
  syncBuiltinESMExports()
  try {
    return run()
  } finally {
    fs.copyFileSync = original
    syncBuiltinESMExports()
  }
}

function liveLock(nonce = 'c'.repeat(32), version = 'racer') {
  return {
    pid: process.pid,
    started_at: new Date().toISOString(),
    host_pid: 0,
    version,
    nonce,
  }
}

function lockAt(root) {
  return inspectRuntimeLock(root)
}

function lockHelperReady() {
  const tries = process.platform === 'win32'
    ? [['py', ['-3.13']], ['py', ['-3']], ['python', []]]
    : [['python3', []], ['python', []]]
  for (const [exe, args] of tries) {
    const r = spawnSync(exe, [...args, '-c', 'import sys; print(sys.executable)'], {
      encoding: 'utf8', windowsHide: true, timeout: 15000,
    })
    if (r.status === 0 && String(r.stdout || '').trim()) return true
  }
  return false
}

function migratedLeftovers(app) {
  return fs.readdirSync(app).filter((n) => n.startsWith('data.migrated-'))
}

console.log('== D1：迁移失败不得删除原数据 ==')

check('rename 注入 EBUSY：保留原目录并回退，brain/kept.txt 与原 config 都在', () => {
  const app = tmp('busy')
  const ext = tmp('busy-ext')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-KEEP"}}')
  write(join(app, 'data', 'brain', 'kept.txt'), 'keep-me')
  const appData = join(app, 'data')
  const info = withBrokenRename(appData, () => guard.prepareData({ appDir: app, dataDir: ext }))
  assert(info.migrated === false, `rename 失败不得报告 migrated=true：${JSON.stringify(info)}`)
  assert(!guard.isLink(appData), 'app/data 仍应是真目录')
  assert(readFileSync(join(appData, 'config.json'), 'utf8').includes('sk-KEEP'), '原 config 不能丢')
  assert(readFileSync(join(appData, 'brain', 'kept.txt'), 'utf8') === 'keep-me',
    'brain/kept.txt 必须留在原目录（旧实现会同 rm 一起丢）')
  assert(info.warnings.some((w) => w.includes('改名失败')), `warnings=${JSON.stringify(info.warnings)}`)
  const leftovers = fs.readdirSync(app).filter((n) => n.startsWith('data.migrated-'))
  assert(leftovers.length === 0, `不应出现改名后的留档：${leftovers}`)
})

check('rename 失败时外部已有文件优先保留（不覆盖 key）', () => {
  const app = tmp('busy-priority')
  const ext = tmp('busy-priority-ext')
  write(join(app, 'data', 'config.json'), '{"llm":{"api_key":"sk-OLD"}}')
  write(join(ext, 'config.json'), '{"llm":{"api_key":"sk-NEW"}}')
  const appData = join(app, 'data')
  const info = withBrokenRename(appData, () => guard.prepareData({ appDir: app, dataDir: ext }))
  assert(info.migrated === false, JSON.stringify(info))
  assert(readFileSync(join(ext, 'config.json'), 'utf8').includes('sk-NEW'), '外部已有文件优先')
  assert(readFileSync(join(appData, 'config.json'), 'utf8').includes('sk-OLD'), '原文件不丢')
})

check('dataDir 是 app/data 的后代：拒绝迁移，不自我递归复制', () => {
  const app = tmp('descendant')
  const appData = join(app, 'data')
  const nested = join(appData, 'nested-ext')
  write(join(appData, 'config.json'), '{"a":1}')
  const info = guard.prepareData({ appDir: app, dataDir: nested })
  assert(info.migrated === false, JSON.stringify(info))
  assert(!guard.isLink(appData), '不得把 app/data 换成联接')
  assert(!existsSync(nested), '不应在 app/data 里创建目标目录')
  assert(readFileSync(join(appData, 'config.json'), 'utf8') === '{"a":1}', '数据不变')
  assert(info.warnings.some((w) => w.includes('包含关系')), `warnings=${JSON.stringify(info.warnings)}`)
})

check('dataDir 是 app/data 后代的 junction 别名：realpath 后仍拒绝迁移', () => {
  const app = tmp('junction-descendant')
  const appData = join(app, 'data')
  const inner = join(appData, 'memory')
  mkdirSync(inner, { recursive: true })
  write(join(appData, 'config.json'), '{"a":1}')
  write(join(inner, 'kept.json'), '[]')
  const aliasParent = tmp('junction-desc-parent')
  const alias = join(aliasParent, 'alias')
  if (!makeDirLink(alias, inner)) {
    console.log('  ↷ 无法创建目录联接，跳过 junction 后代用例')
    return
  }
  const info = guard.prepareData({ appDir: app, dataDir: alias })
  assert(info.migrated === false, JSON.stringify(info))
  assert(!guard.isLink(appData), '不得把 app/data 换成联接')
  assert(readFileSync(join(appData, 'config.json'), 'utf8') === '{"a":1}', '数据不变')
  assert(readFileSync(join(inner, 'kept.json'), 'utf8') === '[]', '后代数据不变')
  assert(info.warnings.some((w) => w.includes('包含关系')), `warnings=${JSON.stringify(info.warnings)}`)
})

check('dataDir 是 app/data 的祖先：拒绝迁移，不形成循环联接', () => {
  const app = tmp('ancestor')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{"a":1}')
  const info = guard.prepareData({ appDir: app, dataDir: app })
  assert(info.migrated === false, JSON.stringify(info))
  assert(!guard.isLink(appData), '不得把 app/data 换成指向 app/ 的循环联接')
  assert(readFileSync(join(appData, 'config.json'), 'utf8') === '{"a":1}', '数据不变')
  assert(info.warnings.some((w) => w.includes('包含关系')), `warnings=${JSON.stringify(info.warnings)}`)
})

check('dataDir 是指向 app/data 的 junction（词法不同、实际同目录）：拒绝自迁移', () => {
  const app = tmp('junction-alias')
  const appData = join(app, 'data')
  const aliasParent = tmp('junction-parent')
  const alias = join(aliasParent, 'alias')
  write(join(appData, 'config.json'), '{"a":1}')
  if (!makeDirLink(alias, appData)) {
    console.log('  ↷ 无法创建目录联接，跳过 junction 别名用例')
    return
  }
  const info = guard.prepareData({ appDir: app, dataDir: alias })
  assert(info.migrated === false, JSON.stringify(info))
  assert(!guard.isLink(appData), '不得把 app/data 换成联接')
  assert(readFileSync(join(appData, 'config.json'), 'utf8') === '{"a":1}', '数据不变')
  assert(!existsSync(join(app, 'data.migrated')), '不得生成迁移留档')
})

console.log('== RACE-1：迁移与实例启动之间的互斥（预约源/目标运行锁 + 两次复检） ==')

check('改名时被发布活运行锁：回滚迁移，原数据/外部副本/别人的锁都保留', () => {
  const app = tmp('race-rename')
  const ext = tmp('race-rename-ext')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{"llm":{"api_key":"sk-RACE"}}')
  write(join(appData, 'brain', 'kept.txt'), 'keep-me')
  const racer = liveLock('e'.repeat(32))
  const info = withInjectedRename(appData, () => {
    writeFileSync(join(appData, '.runtime.lock'), JSON.stringify(racer), 'utf8')
  }, () => guard.prepareData({ appDir: app, dataDir: ext }))
  assert(info.migrated === false, `改名时出现活锁不得报告 migrated=true：${JSON.stringify(info)}`)
  assert(!guard.isLink(appData), 'app/data 仍应是真目录（不得把活锁随目录搬走）')
  assert(readFileSync(join(appData, 'config.json'), 'utf8').includes('sk-RACE'), '原 config 不能丢')
  assert(readFileSync(join(appData, 'brain', 'kept.txt'), 'utf8') === 'keep-me', '原 brain 数据不能丢')
  const after = JSON.parse(readFileSync(join(appData, '.runtime.lock'), 'utf8'))
  assert(after.nonce === racer.nonce, '竞争者的活锁不得被删除/替换')
  assert(existsSync(join(ext, 'config.json')), '外部已复制副本应保留（失败不删数据）')
  assert(migratedLeftovers(app).length === 0, `回滚后不应留下改名目录：${migratedLeftovers(app)}`)
  assert(info.warnings.some((w) => w.includes('运行锁')), `warnings=${JSON.stringify(info.warnings)}`)
})

check('复制期间被发布活运行锁：改名前复检失败，不 rename、不建联接', () => {
  const app = tmp('race-copy')
  const ext = tmp('race-copy-ext')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{"a":1}')
  write(join(appData, 'brain', 'kept.txt'), 'keep-me')
  const racer = liveLock('d'.repeat(32))
  const info = withCopyHook(() => {
    writeFileSync(join(appData, '.runtime.lock'), JSON.stringify(racer), 'utf8')
  }, () => guard.prepareData({ appDir: app, dataDir: ext }))
  assert(info.migrated === false, `复制期间出现活锁不得迁移：${JSON.stringify(info)}`)
  assert(!guard.isLink(appData), '不得把 app/data 换成联接')
  assert(readFileSync(join(appData, 'brain', 'kept.txt'), 'utf8') === 'keep-me', '原数据不能丢')
  const after = JSON.parse(readFileSync(join(appData, '.runtime.lock'), 'utf8'))
  assert(after.nonce === racer.nonce, '复检失败后不得删除/替换别人的锁')
  assert(existsSync(join(ext, 'config.json')), '外部副本已保留')
  assert(migratedLeftovers(app).length === 0, '未 rename，不应出现留档目录')
  assert(info.warnings.some((w) => w.includes('改名前复检失败')), `warnings=${JSON.stringify(info.warnings)}`)
})

check('复制期间源/目标运行锁均由本进程预约，第二个实例被拒绝；结束后释放', () => {
  if (!lockHelperReady()) {
    console.log('  ↷ 无可用 Python helper，跳过预约锁跨进程用例')
    return
  }
  const app = tmp('race-reserve')
  const ext = join(tmp('race-reserve-parent'), 'external')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{"a":1}')
  let observed = null
  let blocked = null
  const info = withCopyHook(() => {
    observed = {
      source: lockAt(appData),
      target: lockAt(ext),
      worker: spawnSync(process.execPath, [WORKER, appData, 'during-copy', '0', '0'], {
        encoding: 'utf8',
        windowsHide: true,
        timeout: 30000,
      }),
    }
  }, () => guard.prepareData({ appDir: app, dataDir: ext }))
  assert(info.migrated === true, `正常迁移应成功：${JSON.stringify(info)}`)
  assert(observed?.source?.present === true && observed?.target?.present === true,
    `复制期间源/目标都应有运行锁：${JSON.stringify(observed)}`)
  assert(observed.source.lock?.pid === process.pid, `源锁 owner=${observed.source.lock?.pid}`)
  assert(observed.target.lock?.pid === process.pid, `目标锁 owner=${observed.target.lock?.pid}`)
  assert(observed.source.lock?.version === 'data-guard:migrate-source',
    `源锁 version=${observed.source.lock?.version}`)
  assert(observed.target.lock?.version === 'data-guard:migrate-target',
    `目标锁 version=${observed.target.lock?.version}`)
  const lines = String(observed.worker?.stdout || '').trim().split(/\r?\n/).filter(Boolean)
  try {
    blocked = JSON.parse(lines[lines.length - 1] || 'null')
  } catch {
    blocked = null
  }
  assert(blocked?.ok === false && blocked?.code === 'LOCKED',
    `第二个实例应得到 LOCKED：stdout=${observed.worker?.stdout} stderr=${observed.worker?.stderr}`)
  assert(!existsSync(join(ext, '.runtime.lock')), '迁移结束后目标根不得残留运行锁')
  assert(!existsSync(join(appData, '.runtime.lock')), '迁移结束后联接根不得残留运行锁')
  const old = migratedLeftovers(app)[0]
  assert(old, '应保留源目录留档')
  assert(!existsSync(join(app, old, '.runtime.lock')),
    '源锁必须用原 nonce 在改名后的新路径（留档目录）释放，不能留在旧目录里')
})

check('迁移复制不携带源运行锁与 guard 归档（陈旧锁留在源留档里）', () => {
  const app = tmp('race-carry')
  const ext = tmp('race-carry-ext')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{}')
  mkdirSync(join(appData, '.locks'), { recursive: true })
  writeFileSync(join(appData, '.runtime.lock'), JSON.stringify({
    pid: 999999,
    started_at: '2020-01-01T00:00:00.000Z',
    host_pid: 0,
    version: 'crashed',
    nonce: 'f'.repeat(32),
  }), 'utf8')
  writeFileSync(join(appData, '.locks', 'stale-manual.lock.json'), '{}', 'utf8')
  const info = guard.prepareData({ appDir: app, dataDir: ext })
  assert(info.migrated === true, JSON.stringify(info))
  assert(!existsSync(join(ext, '.runtime.lock')), '外部目录不得带上源运行锁')
  const extLocks = existsSync(join(ext, '.locks')) ? fs.readdirSync(join(ext, '.locks')) : []
  assert(extLocks.filter((n) => n.startsWith('stale-')).length === 0,
    `外部目录不得带上源锁归档：${JSON.stringify(extLocks)}`)
  const old = migratedLeftovers(app)[0]
  assert(old, '应保留源目录留档')
  assert(readFileSync(join(app, old, 'config.json'), 'utf8') === '{}', '源留档数据完好')
})

console.log('== 版本快照与实例启动之间的互斥（预约运行锁） ==')

check('版本快照复制期间预约数据根运行锁，第二个实例被拒绝；结束后释放', () => {
  if (!lockHelperReady()) {
    console.log('  ↷ 无可用 Python helper，跳过快照预约锁用例')
    return
  }
  const app = tmp('snapshot-reserve')
  const dataDir = join(app, 'data')
  write(join(dataDir, 'config.json'), '{}')
  write(join(dataDir, 'persona.md'), '人设')
  let observed = null
  let contender = null
  const info = withCopyHook((src, dst) => {
    if (!String(dst).includes('plugin-update-')) return
    observed = lockAt(dataDir)
    contender = acquireRuntimeLock({ dataRoot: dataDir, timeoutMs: 2000 })
  }, () => guard.prepareData({ appDir: app, version: '9.9.9' }))
  assert(info.snapshot && existsSync(info.snapshot), `应完成快照：${JSON.stringify(info)}`)
  assert(observed?.present === true && observed?.lock?.pid === process.pid,
    `快照复制期间应由本进程持锁：${JSON.stringify(observed)}`)
  assert(String(observed.lock?.version || '').startsWith('data-guard:snapshot'),
    `快照锁 version=${observed.lock?.version}`)
  assert(contender?.ok === false && contender?.code === 'LOCKED',
    `快照期间第二实例应 LOCKED：${JSON.stringify(contender)}`)
  assert(!existsSync(join(dataDir, '.runtime.lock')), '快照结束后必须释放运行锁')
  const marker = JSON.parse(readFileSync(join(dataDir, '.data-guard.json'), 'utf8'))
  assert(marker.version === '9.9.9', `marker=${marker.version}`)
})

check('rename 到建联间隙仍由稳定路径预约拒绝第二实例', () => {
  const app = tmp('stable-gap')
  const ext = tmp('stable-gap-ext')
  const appData = join(app, 'data')
  write(join(appData, 'config.json'), '{}')
  const original = fs.renameSync
  let contender = null
  fs.renameSync = (src, dst) => {
    const value = original(src, dst)
    if (resolve(src) === resolve(appData)) {
      const child = spawnSync(process.execPath, [WORKER, appData, 'during-gap', '0', '0'], {
        encoding: 'utf8', windowsHide: true, timeout: 30000,
      })
      contender = JSON.parse(String(child.stdout).trim().split(/\r?\n/).pop())
    }
    return value
  }
  syncBuiltinESMExports()
  let info
  try {
    info = guard.prepareData({ appDir: app, dataDir: ext })
  } finally {
    fs.renameSync = original
    syncBuiltinESMExports()
  }
  assert(contender?.ok === false && contender.code === 'LOCKED', JSON.stringify(contender))
  assert(info.migrated === true, JSON.stringify(info))
})

check('helper 不可用时不迁移、不快照、不复制原配置', () => {
  const app = tmp('missing-helper')
  const ext = tmp('missing-helper-ext')
  write(join(app, 'data', 'config.json'), '{}')
  const previous = process.env.PAWBOT_PYTHON_EXE
  process.env.PAWBOT_PYTHON_EXE = join(app, 'missing-python.exe')
  let info
  try {
    info = guard.prepareData({ appDir: app, dataDir: ext, version: 'helper-fail' })
  } finally {
    if (previous === undefined) delete process.env.PAWBOT_PYTHON_EXE
    else process.env.PAWBOT_PYTHON_EXE = previous
  }
  assert(info.migrated === false && !info.snapshot, JSON.stringify(info))
  assert(!existsSync(join(ext, 'config.json')), 'helper失败不能复制数据')
  assert(!existsSync(join(app, 'data', '.data-guard.json')), 'helper失败不能写版本标记')
})

console.log('== 手动备份 ==')
check('createBackup/listBackups/describe', () => {
  const app = tmp('app8')
  write(join(app, 'data', 'config.json'), '{"a":1}')
  write(join(app, 'data', 'persona.md'), '人设')
  const r = guard.createBackup(join(app, 'data'), 'manual-test')
  assert(r.ok && r.files === 2, `备份文件数 ${r.files}`)
  assert(guard.listBackups(join(app, 'data')).length === 1, '列表 1 份')
  const d = guard.describe(join(app, 'data'))
  assert(d.has_config && d.has_persona, 'describe 标记文件存在')
  assert(JSON.stringify(d).indexOf('sk-') < 0, 'describe 不泄露内容')
})

check('guard 关闭时不动任何文件', () => {
  const app = tmp('app9')
  write(join(app, 'data', 'config.json'), '{}')
  const info = guard.prepareData({ appDir: app, dataDir: tmp('ext9'), version: '1.0.0', enabled: false })
  assert(info.enabled === false)
  assert(!existsSync(join(app, 'data', '.data-guard.json')), '不应写 marker')
})

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
