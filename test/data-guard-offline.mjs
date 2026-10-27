// 离线测试：数据守卫（版本快照 / 外部 data 目录迁移 / 联接 / 备份列表）。
// 用法：node test/data-guard-offline.mjs
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as guard from '../lib/data-guard.js'

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
