// 回归：client.js 必须把 PawBot 的 epoch 秒当秒处理，不能直接 new Date(秒)。
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, '..', 'client.js'), 'utf8')
const start = source.indexOf('function toEpochMs')
const end = source.indexOf('function countOf')
assert.ok(start > 0 && end > start, 'client.js 里找不到时间格式化函数')

const code = source.slice(start, end)
const api = vm.runInNewContext(
  code + '\n;({ toEpochMs, fmtTime, fmtRelative, fmtWhen })',
  { Date, Number, Math, String, JSON },
)

const seconds = 1793806907 // 2026-11-04 23:41:47 Asia/Shanghai 附近
const millis = seconds * 1000

assert.equal(api.toEpochMs(seconds), millis, '秒应转成毫秒')
assert.equal(api.toEpochMs(millis), millis, '毫秒不应再乘 1000')
assert.equal(api.toEpochMs(''), 0)

const absolute = api.fmtTime(seconds)
assert.match(absolute, /2026/, `秒级时间应显示 2026，实际：${absolute}`)
assert.doesNotMatch(absolute, /1970/, `秒级时间不能显示 1970，实际：${absolute}`)
assert.equal(api.fmtTime(millis), absolute, '秒和毫秒输入应得到同一显示')

const rel = api.fmtRelative(seconds, seconds - 3600)
assert.equal(rel, '1 小时后', `相对时间应以秒为基准，实际：${rel}`)

const when = api.fmtWhen(seconds)
assert.match(when, /2026/, `fmtWhen 应显示 2026，实际：${when}`)
assert.doesNotMatch(when, /1970/, `fmtWhen 不能显示 1970，实际：${when}`)

console.log('life-time-format: ok', { absolute, rel, when })
