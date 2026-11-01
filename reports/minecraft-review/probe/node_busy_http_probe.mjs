// Reviewer-only probe: real loopback HTTP busy receipt -> Node controller.
//
// Run from the repository root:
//   node reports/minecraft-review/probe/node_busy_http_probe.mjs
//
// The HTTP server returns the exact 16-field signed busy receipt over a real
// loopback socket, then a real 409 `revoked`. It verifies the Node sidecar
// keeps the connection and stops the active action on busy (code -32008), and
// quarantines on a real revoke in under one second. No game, credential or
// repository file other than this probe is touched.
import http from 'node:http';
import { createHmac } from 'node:crypto';
import { readGuardConfig, GuardClient } from '../../../app/minecraft-sidecar/src/guard.js';
import { SidecarController } from '../../../app/minecraft-sidecar/src/controller.js';

const ENV = {
  PAWBOT_MC_GUARD_SECRET: 'synthetic-only-012345678901234567890123456789',
  PAWBOT_MC_WORLD_ID: 'synthetic-test-world',
  PAWBOT_MC_WORLD_PATH: 'C:/synthetic/world',
  PAWBOT_MC_ACTOR_UUID: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  PAWBOT_MC_PVP_UUIDS: '[]',
  PAWBOT_MC_WRITE_ENABLED: 'true',
  PAWBOT_MC_PVP_ENABLED: 'false',
};
Object.assign(process.env, ENV);
const config = readGuardConfig(process.env);
if (config.writeEnabled !== true) throw new Error('config');

let mode = 'busy';
const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', () => {
    if (mode === 'revoked') {
      res.writeHead(409, { 'content-type': 'text/plain' });
      res.end('revoked');
      return;
    }
    const request = JSON.parse(body);
    const now = Date.now() / 1000;
    const payload = {
      nonce: request.nonce, world_id: config.worldId, save_path: config.worldPath,
      host: '127.0.0.1', port: 25565, actor_uuid: config.actorUuid,
      action: request.action, target_uuid: request.target_uuid,
      issued_at: now, expires_at: now + 4,
      can_write: mode !== 'busy', can_pvp: false, backup_id: mode === 'busy' ? '' : 'synthetic-backup',
    };
    if (mode === 'busy') {
      Object.assign(payload, {
        busy: true, retry_after_ms: 250,
        attested_at: Math.max(1, Math.floor(now - 1)),
      });
    }
    const raw = Buffer.from(JSON.stringify(payload));
    const signature = createHmac('sha256', config.secret).update(raw).digest('hex');
    res.writeHead(200, { 'content-type': 'application/json', 'x-pawbot-signature': signature });
    res.end(raw);
  });
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
// readGuardConfig freezes the object; build an equivalent mutable config for the probe.
const liveConfig = { ...config, guardUrl: `http://127.0.0.1:${server.address().port}` };

class FakeGameAdapter {
  constructor() { this.connected = true; this.stopCount = 0; this.closeCount = 0; }
  onDisconnect() {}
  identity() { return liveConfig.actorUuid; }
  stop() { this.stopCount++; }
  async close() { this.connected = false; this.closeCount++; }
  async run() { return { ok: true }; }
  observe() { return { health: 17 }; }
}

const adapter = new FakeGameAdapter();
const controller = new SidecarController({
  config: liveConfig, guard: new GuardClient(liveConfig), adapter,
});
controller.connected = true;
controller.port = 25565;

const failures = [];
try {
  // A busy observe is a valid observation, not a denial.
  const observation = await controller.observe();
  console.log('busy_observe_health=' + observation.health
    + ' quarantined=' + controller.quarantined + ' connected=' + controller.connected);
  if (observation.health !== 17 || controller.quarantined || !controller.connected) failures.push('busy_observe');

  // A busy write checkpoint must stop the active action, keep the connection and answer -32008.
  const job = controller.skill({ name: 'chat', args: { text: 'hi' }, timeout_sec: 5 }).catch((error) => error);
  await new Promise((resolve) => setTimeout(resolve, 150));
  const error = await job;
  console.log('busy_skill_code=' + error.code + ' stopCount=' + adapter.stopCount
    + ' quarantined=' + controller.quarantined + ' connected=' + controller.connected);
  if (error.code !== -32008 || adapter.stopCount < 1 || controller.quarantined || !controller.connected) {
    failures.push('busy_skill');
  }

  // A real 409 revoked must be detected by the 250 ms watchdog in under one second.
  mode = 'revoked';
  const before = Date.now();
  while (!controller.quarantined && Date.now() - before < 1200) await controller.watchGuard();
  const elapsed = Date.now() - before;
  console.log('revoke_detect_ms=' + elapsed + ' quarantined=' + controller.quarantined);
  if (!controller.quarantined || elapsed >= 1000) failures.push('revoke_detect');
} finally {
  await controller.shutdown();
  await new Promise((resolve) => server.close(resolve));
}

console.log(failures.length ? 'FAILURES=' + failures.join(',') : 'PROBE PASSED');
process.exitCode = failures.length ? 1 : 0;
