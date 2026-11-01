import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { readGuardConfig, GuardClient, GuardError, GuardBusy, GuardUnavailable } from '../src/guard.js';
import { validateSkill, SKILLS } from '../src/skills.js';
import { SidecarController } from '../src/controller.js';
import { createRuntime, createLineParser } from '../src/main.js';
import { config, ENV, ACTOR, TARGET, OTHER, CONNECT, payload, busyPayload, sign, syntheticGuard, syntheticBusyGuard, FakeGameAdapter, tick } from './helpers.js';

for (const [label, patch] of Object.entries({ short_secret: { PAWBOT_MC_GUARD_SECRET: 'short' }, remote: { PAWBOT_MC_GUARD_URL: 'http://example.com' }, ipv6: { PAWBOT_MC_GUARD_URL: 'http://[::1]' }, alternate_ip: { PAWBOT_MC_GUARD_URL: 'http://127.1' }, credentials: { PAWBOT_MC_GUARD_URL: 'http://user@127.0.0.1' }, query: { PAWBOT_MC_GUARD_URL: 'http://127.0.0.1?x=1' }, path: { PAWBOT_MC_GUARD_URL: 'http://127.0.0.1/other' }, targets: { PAWBOT_MC_PVP_UUIDS: '["*"]' }, actor: { PAWBOT_MC_ACTOR_UUID: '*' } })) {
  test(`config rejects ${label}`, () => assert.throws(() => readGuardConfig({ ...ENV, ...patch })));
}
test('config accepts exact endpoint or origin and literal true only', () => {
  assert.equal(readGuardConfig({ ...ENV, PAWBOT_MC_GUARD_URL: 'http://127.0.0.1:6198/v1/permit' }).guardUrl, 'http://127.0.0.1:6198');
  assert.equal(readGuardConfig({ ...ENV, PAWBOT_MC_WRITE_ENABLED: 'True' }).writeEnabled, false);
});
const pvpIds = (n) => Array.from({ length: n }, (_, i) => `dddddddd-dddd-4ddd-8ddd-${String(i).padStart(12, '0')}`);
for (const [label, patch] of Object.entries({ secret_257: { PAWBOT_MC_GUARD_SECRET: 'a'.repeat(257) }, world_id_65: { PAWBOT_MC_WORLD_ID: 'w'.repeat(65) }, pvp_17: { PAWBOT_MC_PVP_UUIDS: JSON.stringify(pvpIds(17)) } })) {
  test(`config rejects ${label} beyond the Java guard authority`, () => assert.throws(() => readGuardConfig({ ...ENV, ...patch })));
}
test('config accepts the Java guard boundary values for secret world id and PvP targets', () => {
  const cfg = readGuardConfig({ ...ENV, PAWBOT_MC_GUARD_SECRET: 'a'.repeat(256), PAWBOT_MC_WORLD_ID: 'w'.repeat(64), PAWBOT_MC_PVP_UUIDS: JSON.stringify(pvpIds(16)) });
  assert.equal(cfg.secret.length, 256); assert.equal(cfg.worldId.length, 64); assert.equal(cfg.pvpUuids.size, 16);
});

test('real guard verifies signed byte response and fresh random nonce per request', async () => {
  const guard = syntheticGuard(); await guard.permit('connect'); await guard.permit('connect');
  assert.match(guard.calls[0].nonce, /^[0-9a-f]{32}$/); assert.notEqual(guard.calls[0].nonce, guard.calls[1].nonce);
});
for (const [label, patch] of Object.entries({ nonce: { nonce: '0'.repeat(32) }, action: { action: 'write' }, actor: { actor_uuid: OTHER }, target: { target_uuid: TARGET }, world: { world_id: 'other' }, path: { save_path: 'C:/other' }, host: { host: 'example.com' }, port: { port: 0 }, future: { issued_at: Date.now() / 1000 + 10 }, expired: { expires_at: 1 }, lifetime: { expires_at: Date.now() / 1000 + 9 }, backup: { backup_id: '' } })) {
  test(`guard rejects signed mismatch: ${label}`, async () => {
    const guard = new GuardClient(config, { transport: async (_u, _s, body) => sign(payload(body, patch)) });
    await assert.rejects(guard.permit('connect'), GuardError);
  });
}
test('guard signature authentication precedes field parsing', async () => {
  const response = { status: 200, raw: Buffer.from('{"invalid JSON'), signature: '0'.repeat(64) };
  const guard = new GuardClient(config, { transport: async () => response });
  await assert.rejects(guard.permit('observe'), GuardError);
});
test('revoke receipt accepts closed or changed LAN port while action permits require an exact positive port', async () => {
  const signed = (patch) => new GuardClient(config, { transport: async (_u, _s, body) => sign(payload(body, patch)) });
  assert.equal((await signed({ port: 0 }).permit('revoke')).port, 0);
  assert.equal((await signed({ port: 25566 }).permit('revoke')).port, 25566);
  for (const action of ['observe', 'connect', 'backup']) await assert.rejects(signed({ port: 0 }).permit(action), GuardError);
  await assert.rejects(signed({ port: 65536 }).permit('revoke'), GuardError);
  await assert.rejects(signed({ port: 1.5 }).permit('revoke'), GuardError);
});
test('set_home stays keyless and bounded with no raw coordinate keys', () => {
  assert.equal(validateSkill('set_home', {}), true);
  assert.equal(validateSkill('set_home', { x: 1, y: 64, z: 2 }), false);
  assert.equal(validateSkill('set_home', { dimension: 'overworld' }), false);
  assert.equal(validateSkill('home', { x: 1 }), false);
});
test('guard denies unallowlisted PvP and disabled write before network', async () => {
  let calls = 0;
  const guard = new GuardClient({ ...config, writeEnabled: false }, { transport: async () => { calls++; throw new Error(); } });
  await assert.rejects(guard.permit('write')); await assert.rejects(guard.permit('pvp', OTHER)); assert.equal(calls, 0);
});
test('guard real loopback transport signs raw body and enforces response byte cap', async () => {
  let oversize = false;
  const server = createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    assert.equal(req.url, '/v1/permit'); assert.equal(req.headers.authorization, `Bearer ${config.secret}`);
    const signed = sign(payload(body)); res.setHeader('X-PawBot-Signature', signed.signature); res.end(oversize ? Buffer.alloc(16385) : signed.raw);
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  try { const guard = new GuardClient({ ...config, guardUrl: `http://127.0.0.1:${server.address().port}` }); await guard.permit('observe'); oversize = true; await assert.rejects(guard.permit('observe')); }
  finally { server.closeAllConnections(); await new Promise((r) => server.close(r)); }
});

actionExamples();
function actionExamples() {
  const examples = { move: { x: 1, y: 64, z: 2, radius: 1 }, follow: { target_uuid: TARGET, duration_sec: 2 }, chat: { text: 'hello' }, dig: { x: 1, y: 64, z: 2 }, collect: { block: 'dirt', count: 2 }, craft: { item: 'stick', count: 2 }, inventory: {}, chest: { x: 1, y: 64, z: 2, operation: 'deposit', item: 'dirt', count: 2 }, place: { x: 1, y: 64, z: 2, face_x: 0, face_y: 1, face_z: 0, item: 'dirt' }, farm: { x: 1, y: 64, z: 2, seed: 'wheat_seeds' }, breed: { target_uuid: TARGET, item: 'wheat' }, attack: { target_uuid: TARGET }, eat: {}, explore: { x: 1, y: 64, z: 2 }, home: {}, set_home: {}, respawn: {} };
  for (const name of SKILLS) test(`skill ${name} has exact bounded schema`, () => { assert.equal(validateSkill(name, examples[name]), true); assert.equal(validateSkill(name, { ...examples[name], arbitrary: true }), false); });
}
for (const text of ['/op abc', '  /op abc', 'hi\n/op abc', 'hello\u0000', 'x'.repeat(201)]) test('chat rejects commands or unbounded/control text', () => assert.equal(validateSkill('chat', { text }), false));
test('place rejects diagonal or zero face and counts cannot escape bounds', () => {
  assert.equal(validateSkill('place', { x: 1, y: 64, z: 1, face_x: 1, face_y: 1, face_z: 0, item: 'dirt' }), false);
  assert.equal(validateSkill('collect', { block: 'dirt', count: 17 }), false);
  assert.equal(validateSkill('move', { x: Infinity, y: 64, z: 1, radius: 1 }), false);
});

async function connected(t, adapter = new FakeGameAdapter(), guard = syntheticGuard()) {
  const controller = new SidecarController({ config, guard, adapter }); t.after(() => controller.shutdown());
  await controller.connect(CONNECT); return { controller, adapter, guard };
}
test('connect obtains exactly one snapshot-bearing connect permit before the socket', async (t) => {
  const { controller, adapter, guard } = await connected(t);
  assert.equal(controller.connected, true);
  assert.deepEqual(guard.calls.slice(0, 2).map((c) => c.action), ['connect', 'observe']);
  assert.equal(guard.calls.filter((c) => c.action === 'backup').length, 0,
    'the guard connect action already performs the mandatory fresh snapshot');
  assert.equal(adapter.calls[0], 'connect');
  const result = await controller.observe(); assert.equal(result.health, 17);
});
test('connect denies mismatched guard port without game connection', async () => {
  const adapter = new FakeGameAdapter(); const controller = new SidecarController({ config, adapter, guard: syntheticGuard(() => ({ port: 25566 })) });
  await assert.rejects(controller.connect(CONNECT)); assert.equal(adapter.calls.length, 0); assert.equal(controller.quarantined, true);
});
test('spawn actor mismatch disconnects and quarantines', async () => {
  const adapter = new FakeGameAdapter(); adapter.actor = OTHER;
  const controller = new SidecarController({ config, adapter, guard: syntheticGuard() });
  await assert.rejects(controller.connect(CONNECT)); assert.equal(adapter.connected, false); assert.equal(controller.quarantined, true);
});
test('guard loss cancels ongoing action and closes under one second', async (t) => {
  let deny = false;
  const guard = syntheticGuard(() => deny ? { world_id: 'revoked' } : {});
  const adapter = new FakeGameAdapter(); adapter.run = () => new Promise(() => {});
  const { controller } = await connected(t, adapter, guard);
  const task = controller.skill({ name: 'chat', args: { text: 'hi' }, timeout_sec: 30 }); const rejected = assert.rejects(task);
  await tick(); const before = Date.now(); deny = true;
  while (!controller.quarantined && Date.now() - before < 1200) await new Promise((r) => setTimeout(r, 20));
  await rejected; assert.equal(adapter.connected, false); assert.ok(Date.now() - before < 1000);
});
test('queue has one active plus eight waiting, cancel rejects all and closes', async (t) => {
  const adapter = new FakeGameAdapter(); adapter.run = () => new Promise(() => {});
  const { controller } = await connected(t, adapter);
  const jobs = []; const req = { name: 'chat', args: { text: 'hi' }, timeout_sec: 30 };
  for (let i = 0; i < 9; i++) jobs.push(controller.skill(req));
  const all = Promise.allSettled(jobs);
  assert.throws(() => controller.skill(req), /queue full/i); await controller.cancel();
  const results = await all; assert.equal(results.filter((r) => r.status === 'rejected').length, 9); assert.equal(adapter.connected, false);
});
test('timeout closes a noncooperative operation and clears queue', async (t) => {
  const adapter = new FakeGameAdapter(); adapter.run = () => new Promise(() => {});
  const { controller } = await connected(t, adapter);
  await assert.rejects(controller.skill({ name: 'eat', args: {}, timeout_sec: 1 }), (error) => error.code === -32005);
  assert.equal(adapter.connected, false); assert.equal(controller.quarantined, true);
});
test('fresh permit validates port before mutation and changed actor denies observe', async (t) => {
  const { controller, adapter } = await connected(t);
  adapter.actor = OTHER; await assert.rejects(controller.observe()); assert.equal(adapter.calls.length, 1);
});
test('protocol static errors do not echo adapter exception text', async () => {
  const lines = []; const adapter = new FakeGameAdapter(); adapter.connect = async () => { throw new Error('synthetic-private-detail'); };
  const runtime = createRuntime({ config, adapter, guard: syntheticGuard(), write: (v) => lines.push(v) });
  await runtime.dispatch({ jsonrpc: '2.0', id: 1, method: 'connect', params: CONNECT });
  assert.equal(lines.at(-1).error.message, 'Operation failed'); assert.doesNotMatch(JSON.stringify(lines), /synthetic-private-detail/);
});
test('byte parser bounds oversized fragmented lines and resumes at next newline', () => {
  const lines = [], errors = []; const parser = createLineParser((v) => lines.push(v), () => errors.push(true));
  parser(Buffer.from('x'.repeat(60000))); parser(Buffer.from('x'.repeat(10000))); parser(Buffer.from('ignored\n{"id":1}\n'));
  assert.equal(errors.length, 1); assert.deepEqual(lines, ['{"id":1}']);
});

// ---------------------------------------------------------------- busy snapshot contract

test('signed busy receipt is a non-mutating observe, never a mutation permit', async () => {
  const guard = new GuardClient(config, { transport: async (_u, _s, body) => sign(busyPayload(body)) });
  for (const action of ['connect', 'backup', 'write']) {
    await assert.rejects(guard.permit(action), GuardBusy);
  }
  await assert.rejects(guard.permit('pvp', TARGET), GuardBusy);
  const row = await guard.permit('observe');
  assert.equal(row.busy, true);
  assert.equal(row.backup_id, '');
  assert.equal(row.can_write, false);
  assert.equal(row.can_pvp, false);
});

test('forged or malformed busy receipts fail closed and extra private fields are refused', async () => {
  for (const patch of [{ can_write: true }, { can_pvp: true }, { backup_id: 'backup-1' }, { retry_after_ms: 0 },
    { retry_after_ms: 6000 }, { retry_after_ms: 250.5 }, { attested_at: 0 }, { attested_at: 1e15 }, { busy: 'true' }, { secret: 'leak' }]) {
    const guard = new GuardClient(config, { transport: async (_u, _s, body) => sign(busyPayload(body, patch)) });
    await assert.rejects(guard.permit('observe'), GuardError);
  }
  // A busy receipt forged without the secret is rejected on the signature, before any field is read.
  const forged = new GuardClient(config, { transport: async (_u, _s, body) => sign(busyPayload(body), { signature: '0'.repeat(64) }) });
  await assert.rejects(forged.permit('observe'), GuardError);
});

test('a busy checkpoint pauses and cancels the action instead of quarantining', async (t) => {
  let busy = true;
  const guard = syntheticBusyGuard((request) => busy && request.action === 'write');
  const adapter = new FakeGameAdapter();
  const controller = new SidecarController({ config, guard, adapter }); t.after(() => controller.shutdown());
  await controller.connect(CONNECT);
  await assert.rejects(controller.skill({ name: 'chat', args: { text: 'hi' }, timeout_sec: 5 }), (error) => error.code === -32008);
  assert.equal(controller.quarantined, false);
  assert.equal(adapter.connected, true);
  assert.ok(adapter.stopCount >= 1);
  busy = false;
  await new Promise((r) => setTimeout(r, 350));
  assert.deepEqual(await controller.skill({ name: 'chat', args: { text: 'hi' }, timeout_sec: 5 }), { ok: true });
});

test('watchdog busy keeps the connection and stops the active action', async (t) => {
  let busy = false;
  const guard = syntheticBusyGuard((request) => busy && request.action === 'write');
  const adapter = new FakeGameAdapter();
  let release;
  adapter.run = async (_name, _args, _signal, permit) => { await permit('write'); return new Promise((resolve) => { release = resolve; }); };
  const controller = new SidecarController({ config, guard, adapter }); t.after(() => controller.shutdown());
  await controller.connect(CONNECT);
  const job = controller.skill({ name: 'chat', args: { text: 'hi' }, timeout_sec: 10 });
  await tick();
  await new Promise((r) => setTimeout(r, 100));
  busy = true;
  await new Promise((r) => setTimeout(r, 800)); // several 250 ms watchdogs
  assert.equal(controller.quarantined, false);
  assert.equal(adapter.connected, true);
  assert.ok(adapter.stopCount >= 1);
  release({ ok: true });
  assert.deepEqual(await job, { ok: true });
});

test('a real revoke after a busy window still closes under one second', async (t) => {
  let mode = 'ok';
  const guard = new GuardClient(config, { transport: async (_u, _s, body) => {
    const request = JSON.parse(body);
    if (mode === 'busy' && request.action === 'observe') return sign(busyPayload(body));
    if (mode === 'revoked') return sign(payload(body, { world_id: 'revoked' }));
    return sign(payload(body));
  } });
  const adapter = new FakeGameAdapter();
  const controller = new SidecarController({ config, guard, adapter }); t.after(() => controller.shutdown());
  await controller.connect(CONNECT);
  mode = 'busy';
  await new Promise((r) => setTimeout(r, 700));
  assert.equal(controller.quarantined, false);
  assert.equal(adapter.connected, true);
  mode = 'revoked';
  const before = Date.now();
  while (!controller.quarantined && Date.now() - before < 1200) await new Promise((r) => setTimeout(r, 20));
  assert.equal(controller.quarantined, true);
  assert.ok(Date.now() - before < 1000);
});

test('connect retries a signed busy window within its own budget', async (t) => {
  const guard = syntheticBusyGuard((request, attempt) => request.action === 'connect' && attempt <= 3);
  const adapter = new FakeGameAdapter();
  const controller = new SidecarController({ config, guard, adapter }); t.after(() => controller.shutdown());
  assert.equal((await controller.connect(CONNECT)).connected, true);
  assert.ok(guard.calls.filter((call) => call.action === 'connect').length >= 4);
  assert.equal(guard.calls.filter((call) => call.action === 'backup').length, 0);
  assert.equal(adapter.connected, true);
});

test('a busy observe is a valid non-mutating observation', async (t) => {
  const guard = syntheticBusyGuard((request) => request.action === 'observe');
  const adapter = new FakeGameAdapter();
  const controller = new SidecarController({ config, guard, adapter }); t.after(() => controller.shutdown());
  await controller.connect(CONNECT);
  assert.equal((await controller.observe()).health, 17);
});

test('the guard is the only periodic snapshot scheduler', async (t) => {
  const guard = syntheticGuard();
  const adapter = new FakeGameAdapter();
  const controller = new SidecarController({ config, guard, adapter }); t.after(() => controller.shutdown());
  await controller.connect(CONNECT);
  assert.equal(controller.backupTimer, undefined);
  assert.equal(controller.periodicBackup, undefined);
  assert.equal(guard.calls.filter((call) => call.action === 'backup').length, 0);
  const before = guard.calls.length;
  await controller.observe();
  await new Promise((r) => setTimeout(r, 400));
  assert.ok(guard.calls.slice(before).every((call) => call.action !== 'backup'), 'no client-side backup timer');
});

test('a transport failure is unavailable, never busy, and quarantines fail-closed', async (t) => {
  let offline = false;
  const guard = new GuardClient(config, { transport: async (_u, _s, body) => {
    if (offline) throw new Error('synthetic transport failure');
    return sign(payload(body));
  } });
  const adapter = new FakeGameAdapter();
  const controller = new SidecarController({ config, guard, adapter }); t.after(() => controller.shutdown());
  await controller.connect(CONNECT);
  offline = true;
  await assert.rejects(controller.skill({ name: 'chat', args: { text: 'hi' }, timeout_sec: 5 }),
    (error) => error instanceof GuardUnavailable && !(error instanceof GuardBusy));
  assert.equal(controller.quarantined, true);
});
