// 测试夹具：在独立进程里抢运行锁，验证原子并发（不参与任何业务逻辑）。
// 用法：node runtime-lock-worker.mjs <dataRoot> <tag> <startAtEpochMs> <holdMs>
import { acquireRuntimeLock, releaseRuntimeLock } from '../../lib/instance-lock.js'

const [dataRoot, tag, startAtRaw, holdMsRaw] = process.argv.slice(2)
const startAt = Number(startAtRaw) || 0
const holdMs = Math.max(0, Number(holdMsRaw) || 0)
const sleep = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms)

while (Date.now() < startAt) sleep(5)

const result = acquireRuntimeLock({ dataRoot, hostPid: process.pid, version: 'worker' })
const payload = {
  tag,
  ok: result.ok === true,
  code: result.code || null,
  nonce: result.lock?.nonce || null,
  pid: process.pid,
  archived: Array.isArray(result.staleArchived) ? result.staleArchived.length : 0,
  message: result.message || '',
}
process.stdout.write(JSON.stringify(payload) + '\n')
if (result.ok) {
  sleep(holdMs)
  releaseRuntimeLock({ dataRoot, nonce: result.lock.nonce })
}
process.exit(result.ok ? 0 : 2)
