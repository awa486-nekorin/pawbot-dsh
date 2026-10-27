/**
 * 大肥鱼大脑桥（lib/dsh-brain.js）的离线测试。
 *
 * 不启 DSH、不联网、不写盘：只测纯函数 + 路由的鉴权/限额/协议分支。
 * 真正的 Agent 轮次由 pawbot_brain(selftest) 在运行中的 DSH 里验证（见 docs）。
 */
import { EventEmitter } from 'node:events'
import assert from 'node:assert/strict'
import {
  BRAIN_LIMITS,
  BRAIN_TURN_PATH,
  COMPUTER_TOOL_NAMES,
  computerToolGrant,
  createBrainBridge,
  DEFAULT_NATIVE_TOOLS,
  isLoopback,
  nativePresetRows,
  nativeToolNames,
  oneLine,
  openAiToolToDefinition,
  summarizeSession,
} from '../lib/dsh-brain.js'

let passed = 0
let failed = 0
const ok = (name, extra = '') => {
  passed += 1
  console.log(`  ✔ ${name}${extra ? ' — ' + extra : ''}`)
}
const bad = (name, detail) => {
  failed += 1
  console.log(`  ✘ ${name} — ${detail}`)
}
function check(name, run) {
  try {
    const result = run()
    if (result === true || result === undefined) ok(name)
    else bad(name, typeof result === 'string' ? result : 'assertion failed')
  } catch (error) {
    bad(name, error.message)
  }
}
async function checkAsync(name, run) {
  try {
    const result = await run()
    if (result === true || result === undefined) ok(name)
    else bad(name, typeof result === 'string' ? result : 'assertion failed')
  } catch (error) {
    bad(name, error.message)
  }
}

console.log('== dsh-brain 純函數 ==')

check('isLoopback 只認 127/::1', () => {
  assert.equal(isLoopback('127.0.0.1'), true)
  assert.equal(isLoopback('::1'), true)
  assert.equal(isLoopback('::ffff:127.0.0.1'), true)
  assert.equal(isLoopback('192.168.1.4'), false)
  assert.equal(isLoopback('::ffff:10.0.0.1'), false)
  assert.equal(isLoopback(undefined), false)
  return true
})

check('oneLine 壓成一行並截斷', () => {
  assert.equal(oneLine(new Error('a\nb\tc'), 100), 'Error: a b c')
  assert.equal(oneLine('x'.repeat(50), 10).length, 10)
  return true
})

check('summarizeSession 取最後一條非空 assistant 文本', () => {
  const events = [
    { type: 'turn/start' },
    { type: 'assistant/message', data: { message: { content: [] } } },        // 工具步：空
    { type: 'tool/result' },
    { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '第一句' }] } } },
    { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '   ' }] } } }, // 空白不算
    { type: 'assistant/message', data: { message: { content: [
      { type: 'text', text: '？啥99' }, { type: 'reasoning', text: '内部推理' },
    ] } } },
    { type: 'turn/end', data: { reason: { kind: 'completed' } } },
  ]
  const session = { seq: events.length, eventAt: (seq) => events[seq] }
  const outcome = summarizeSession(session, 0)
  assert.equal(outcome.text, '？啥99')
  assert.equal(outcome.reason.kind, 'completed')
  return true
})

check('summarizeSession 跳過 turn/start 之前的事件', () => {
  const events = [
    { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '上一轮' }] } } },
    { type: 'turn/start' },
    { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '这一轮' }] } } },
  ]
  const session = { seq: events.length, eventAt: (seq) => events[seq] }
  assert.equal(summarizeSession(session, 0).text, '这一轮')
  return true
})

check('openAiToolToDefinition 保留 schema 並渲染文本', () => {
  const def = openAiToolToDefinition({
    type: 'function',
    function: {
      name: 'web_search',
      description: '搜一下',
      parameters: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] },
    },
  }, async () => ({ text: 'hi' }), (value) => value.text)
  assert.equal(def.name, 'web_search')
  assert.equal(def.description, '搜一下')
  assert.equal(def.parameters.required[0], 'query')
  const blocks = def.output.render({}, { text: '结果' })
  assert.deepEqual(blocks, [{ type: 'text', text: '结果' }])
  return true
})

check('nativePresetRows 按工具族映射，空集合不掛東西', () => {
  const rows = nativePresetRows(['pwsh', 'read', 'glob', 'web_search', 'unknown'])
  const ids = rows.map((row) => row.id)
  assert.ok(ids.includes('tool-pwsh') || ids.includes('tool-bash'))
  assert.ok(ids.includes('tool-fs'))
  assert.ok(ids.includes('tool-fs-search'))
  assert.ok(ids.includes('tool-web'))
  assert.deepEqual(nativePresetRows([]), [])
  assert.deepEqual(nativePresetRows(['unknown']), [])
  return true
})

check('白名單常量自洽：默认电脑工具都在 preset 工具名里', () => {
  for (const name of DEFAULT_NATIVE_TOOLS) assert.ok(COMPUTER_TOOL_NAMES.includes(name), name)
  assert.equal(BRAIN_TURN_PATH, '/pawbot-dsh-brain/turn')
  assert.ok(BRAIN_LIMITS.bodyBytes >= 1024 * 1024)
  return true
})

check('nativeToolNames 跟着配置走，不是常量全集', () => {
  const all = nativeToolNames(DEFAULT_NATIVE_TOOLS)
  for (const name of DEFAULT_NATIVE_TOOLS) assert.ok(all.includes(name) || name === 'pwsh' || name === 'bash', name)
  const onlyRead = nativeToolNames(['read'])
  assert.deepEqual(onlyRead, ['read', 'write', 'edit'])  // dsh-tool-fs 一行注册三个 fs 工具
  assert.deepEqual(nativeToolNames([]), [])
  assert.deepEqual(nativeToolNames(['unknown']), [])
  return true
})

check('computerToolGrant：默认只给主人（群友轮次一个都不给）', () => {
  const guest = computerToolGrant(DEFAULT_NATIVE_TOOLS, { ownerOnly: true, isOwner: false })
  assert.deepEqual(guest.granted, [])
  assert.deepEqual(guest.declared, [])
  assert.ok(guest.reason.includes('owner-only'))
  const owner = computerToolGrant(DEFAULT_NATIVE_TOOLS, { ownerOnly: true, isOwner: true })
  assert.ok(owner.granted.includes('pwsh') || owner.granted.includes('bash'))
  const open = computerToolGrant(['read'], { ownerOnly: false, isOwner: false })
  assert.deepEqual(open.declared, ['read'])
  assert.deepEqual(open.granted, ['read', 'write', 'edit'])
  const none = computerToolGrant([], { ownerOnly: false, isOwner: true })
  assert.deepEqual(none.granted, [])
  return true
})

// ── 路由：鉴权 / 限额 / 协议 ───────────────────────────────────────────────

function makeReq({ method = 'POST', headers = {}, body = '', remote = '127.0.0.1' } = {}) {
  const emitter = new EventEmitter()
  const req = Object.assign(emitter, {
    method,
    headers,
    socket: { remoteAddress: remote },
    pause() {},
  })
  setImmediate(() => {
    if (body) emitter.emit('data', Buffer.from(body, 'utf8'))
    emitter.emit('end')
  })
  return req
}

function makeRes() {
  const res = {
    statusCode: 0,
    headers: null,
    body: '',
    destroyed: false,
    writableEnded: false,
    writeHead(status, headers) {
      res.statusCode = status
      res.headers = headers
    },
    end(body) {
      res.body = body || ''
      res.writableEnded = true
    },
  }
  return res
}

function makeBridge(overrides = {}) {
  const ctx = {
    get: () => undefined,
    tools: { schemas: () => [] },
  }
  return createBrainBridge({
    ctx,
    config: { brainEnabled: true, brainSandbox: 'workspace-write', ...overrides },
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    pawbotBase: 'http://127.0.0.1:6185',
    hostOrigin: 'http://127.0.0.1:50311',
    workDir: () => undefined,
  })
}

const jsonHeaders = (token) => ({
  'content-type': 'application/json',
  authorization: `Bearer ${token}`,
})

console.log('\n== 大腦橋路由（鉴权/限额/协议） ==')

await checkAsync('关掉时 503 BRAIN_DISABLED', async () => {
  const bridge = makeBridge({ brainEnabled: false })
  const res = makeRes()
  await bridge.dispatchTurn(makeReq(), res)
  assert.equal(res.statusCode, 503)
  assert.equal(JSON.parse(res.body).error.code, 'BRAIN_DISABLED')
  return true
})

await checkAsync('非 loopback 403', async () => {
  const bridge = makeBridge()
  const token = getToken(bridge)
  const res = makeRes()
  await bridge.dispatchTurn(makeReq({ headers: jsonHeaders(token), remote: '10.0.0.5' }), res)
  assert.equal(res.statusCode, 403)
  return true
})

await checkAsync('缺 token / 错 token 都 403', async () => {
  const bridge = makeBridge()
  getToken(bridge)
  for (const headers of [
    { 'content-type': 'application/json' },
    { 'content-type': 'application/json', authorization: 'Bearer pdbrain_' + 'a'.repeat(64) },
  ]) {
    const res = makeRes()
    await bridge.dispatchTurn(makeReq({ headers }), res)
    assert.equal(res.statusCode, 403)
  }
  return true
})

await checkAsync('带 Origin 的请求 403（不是浏览器 API）', async () => {
  const bridge = makeBridge()
  const token = getToken(bridge)
  const res = makeRes()
  await bridge.dispatchTurn(makeReq({ headers: { ...jsonHeaders(token), origin: 'https://evil' } }), res)
  assert.equal(res.statusCode, 403)
  return true
})

await checkAsync('GET 405、错 Content-Type 415、超大 body 413、坏 JSON 400', async () => {
  const bridge = makeBridge()
  const token = getToken(bridge)

  let res = makeRes()
  await bridge.dispatchTurn(makeReq({ method: 'GET', headers: jsonHeaders(token) }), res)
  assert.equal(res.statusCode, 405, 'GET')

  res = makeRes()
  await bridge.dispatchTurn(makeReq({ headers: { ...jsonHeaders(token), 'content-type': 'text/plain' } }), res)
  assert.equal(res.statusCode, 415, 'content-type')

  res = makeRes()
  await bridge.dispatchTurn(makeReq({ headers: { ...jsonHeaders(token), 'content-length': String(BRAIN_LIMITS.bodyBytes + 1) } }), res)
  assert.equal(res.statusCode, 413, 'content-length')

  res = makeRes()
  await bridge.dispatchTurn(makeReq({ headers: jsonHeaders(token), body: 'not-json' }), res)
  assert.equal(res.statusCode, 400, 'invalid json')
  return true
})

await checkAsync('合法请求但缺 system/user → 502（结构错误，不瞎跑）', async () => {
  const bridge = makeBridge()
  const token = getToken(bridge)
  const res = makeRes()
  await bridge.dispatchTurn(makeReq({
    headers: jsonHeaders(token),
    body: JSON.stringify({ conv: 'group_1', system: '', user: '', tools: [] }),
  }), res)
  assert.equal(res.statusCode, 502)
  const payload = JSON.parse(res.body)
  assert.equal(payload.ok, false)
  assert.ok(['INVALID_REQUEST', 'NO_MODEL', 'BRAIN_UNAVAILABLE'].includes(payload.error.code), payload.error.code)
  return true
})

await checkAsync('status 路由只認 loopback，回包不含 token', async () => {
  const bridge = makeBridge()
  getToken(bridge)
  let res = makeRes()
  await bridge.dispatchStatus(makeReq({ method: 'GET', remote: '10.0.0.5' }), res)
  assert.equal(res.statusCode, 403)

  res = makeRes()
  await bridge.dispatchStatus(makeReq({ method: 'GET' }), res)
  assert.equal(res.statusCode, 200)
  const payload = JSON.parse(res.body)
  assert.equal(payload.enabled, true)
  assert.equal(payload.token_present, true)
  assert.ok(!JSON.stringify(payload).includes('pdbrain_'), 'status 泄漏了 token')
  return true
})

await checkAsync('envForChild 注入 URL/token，且不把 token 写进 status/日志', async () => {
  const bridge = makeBridge()
  const env = bridge.envForChild({ KEEP: '1' })
  assert.equal(env.KEEP, '1')
  assert.match(env.PAWBOT_DSH_BRAIN_TOKEN, /^pdbrain_[a-f0-9]{64}$/)
  // 子进程要打的是 DSH WebServer 的 origin（hostOrigin），不是 PawBot 自己的 6185
  assert.equal(env.PAWBOT_DSH_BRAIN_URL, 'http://127.0.0.1:50311' + BRAIN_TURN_PATH)
  const off = makeBridge({ brainEnabled: false }).envForChild({ KEEP: '1' })
  assert.equal(off.PAWBOT_DSH_BRAIN_TOKEN, undefined)
  assert.equal(off.KEEP, '1')
  return true
})

// ── runTurn：注入宿主模块 + 假 agents（不启 DSH，专门锁回归） ──────────────
// 之前 15 条测试一条都没进过 runTurn，导致一个 TDZ（在 turnState 声明前写字段）
// 能让所有真实轮次 502 却测试全绿。这里补上真正执行 runTurn 的路径。

const fakeModules = {
  createUserMessage: (input) => ({ id: 'u1', role: 'user', content: input.content, source: input.source }),
  personaPrefixSection: 'deployment:persona-prefix',
  installModelSelection: () => {},
  setSandboxMode: () => {},
  setApprovalPolicy: () => {},
}

function makeTurnHarness({ ownerOnly = true, nativeTools = DEFAULT_NATIVE_TOOLS, hang = false, stopReason = 'completed', text = '好的' } = {}) {
  const records = { created: [], mounted: [], sections: [], registered: [], restricts: [], guards: [], cancelled: 0 }
  const agent = {
    id: 'session-fake',
    status: 'idle',
    session: {
      id: 'session-fake',
      seq: 0,
      _events: [],
      eventAt(seq) { return this._events[seq] },
    },
    followup() {
      this.session._events.push({ type: 'turn/start' })
      this.session._events.push({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text }] } } })
      this.session._events.push({ type: 'turn/end', data: { reason: { kind: stopReason } } })
      this.session.seq = this.session._events.length
    },
    async whenIdle() {
      if (hang) return new Promise(() => {})   // 模拟工具卡死
    },
    cancel() { records.cancelled += 1 },
  }
  const agentCtx = {
    systemPrompt: {
      getSectionOrder: () => 0,
      section: (section) => { records.sections.push(section); return () => {} },
    },
    tools: {
      register: (definition) => { records.registered.push(definition); return () => {} },
      restrict: (filter) => { records.restricts.push(filter); return () => {} },
      guard: (guard) => { records.guards.push(guard); return () => {} },
      schemas: () => [],
    },
  }
  const ctx = {
    get(name) {
      if (name === 'agents') {
        return {
          create: async (options) => {
            records.created.push(options)
            await options.setup(agentCtx, agent)
            return { agent, dispose: async () => {} }
          },
        }
      }
      if (name === 'agentDefaultModel') return { currentSelection: () => ({ provider: 'p', model: 'm' }) }
      if (name === 'agentPresets') {
        return {
          register: async () => (async () => {}),
          mount: async (_ctx, id) => { records.mounted.push(id) },
          resolve: async () => ({}),
        }
      }
      return undefined
    },
    tools: { schemas: () => [] },
    approval: { setPolicy: () => {} },
  }
  const bridge = createBrainBridge({
    ctx,
    config: { brainEnabled: true, brainNativeTools: nativeTools, brainOwnerOnly: ownerOnly, brainSandbox: 'workspace-write' },
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    pawbotBase: 'http://127.0.0.1:6185',
    hostOrigin: 'http://127.0.0.1:50311',
    workDir: () => undefined,
    modules: fakeModules,
  })
  return { bridge, records, agent }
}

async function runOne(harness, body) {
  const token = getToken(harness.bridge)
  const res = makeRes()
  await harness.bridge.dispatchTurn(makeReq({
    headers: jsonHeaders(token),
    body: JSON.stringify({
      conv: 'group_1#turn1', system: '你是大肥鱼', user: '在吗', tools: [], ...body,
    }),
  }), res)
  return { status: res.statusCode, payload: JSON.parse(res.body) }
}

await checkAsync('runTurn：群友轮次不给电脑工具（不挂 preset），只跑 PawBot 工具', async () => {
  const harness = makeTurnHarness({ ownerOnly: true })
  const { status, payload } = await runOne(harness, { context: { is_owner: false } })
  assert.equal(status, 200)
  assert.equal(payload.text, '好的')
  assert.deepEqual(payload.isolation.computer_tools, [])
  assert.equal(harness.records.mounted.length, 0, '群友轮次不应挂 preset')
  assert.equal(harness.records.sections.length, 1)
  assert.equal(harness.records.sections[0].complete, true)
  // 人设段文本 = PawBot 的 system（没电脑工具时不追加安全边界）
  assert.equal(harness.records.sections[0].text(), '你是大肥鱼')
  return true
})

await checkAsync('runTurn：主人轮次挂 preset、放开电脑工具、并注入安全边界', async () => {
  const harness = makeTurnHarness({ ownerOnly: true })
  const { status, payload } = await runOne(harness, { context: { is_owner: true } })
  assert.equal(status, 200)
  assert.ok(harness.records.mounted.includes('pawbot-brain'))
  assert.ok(payload.isolation.computer_tools.includes('pwsh') || payload.isolation.computer_tools.includes('bash'))
  const sectionText = harness.records.sections[0].text()
  assert.ok(sectionText.includes('你是大肥鱼'))
  assert.ok(sectionText.includes('安全边界'), '有电脑工具时应追加安全边界')
  // guard 必须真的拦名单外的工具
  const deny = harness.records.guards[0]({ name: 'subagent' })
  assert.equal(typeof deny, 'string')
  const allow = harness.records.guards[0]({ name: payload.isolation.computer_tools.find((n) => n === 'pwsh' || n === 'bash') })
  assert.equal(allow, undefined)
  return true
})

await checkAsync('runTurn：中断/超时不能把残句当成功回给群', async () => {
  const interrupted = makeTurnHarness({ stopReason: 'aborted', text: '我看看…' })
  let result = await runOne(interrupted, { context: { is_owner: false } })
  assert.equal(result.status, 502)
  assert.equal(result.payload.error.code, 'INTERRUPTED')

  const hung = makeTurnHarness({ hang: true })
  result = await runOne(hung, { context: { is_owner: false }, options: { timeoutMs: 80 } })
  assert.equal(result.status, 504)
  assert.equal(result.payload.error.code, 'TIMEOUT')
  assert.ok(hung.records.cancelled > 0, '超时后必须 cancel Agent')
  return true
})


function getToken(bridge) {
  const env = bridge.envForChild({})
  const token = env.PAWBOT_DSH_BRAIN_TOKEN
  assert.match(String(token), /^pdbrain_[a-f0-9]{64}$/)
  return token
}

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed === 0 ? 0 : 1)
