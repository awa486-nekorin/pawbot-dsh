import { createHmac } from 'node:crypto';
import { readGuardConfig, GuardClient, GuardBusy } from '../src/guard.js';
export const ACTOR = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
export const TARGET = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
export const OTHER = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
export const ENV = { PAWBOT_MC_GUARD_SECRET: 'synthetic-only-012345678901234567890123456789', PAWBOT_MC_WORLD_ID: 'synthetic-test-world', PAWBOT_MC_WORLD_PATH: 'C:/synthetic/world', PAWBOT_MC_ACTOR_UUID: ACTOR, PAWBOT_MC_PVP_UUIDS: JSON.stringify([TARGET]), PAWBOT_MC_WRITE_ENABLED: 'true', PAWBOT_MC_PVP_ENABLED: 'true' };
export const config = readGuardConfig(ENV);
export const CONNECT = { host: '127.0.0.1', port: 25565, version: '1.20.1', username: 'SyntheticBot', auth_mode: 'offline', timeout_sec: 3 };
export function payload(body, patch = {}) { const now = Date.now() / 1000; return { ...JSON.parse(body), world_id: config.worldId, save_path: config.worldPath, host: '127.0.0.1', port: 25565, issued_at: now, expires_at: now + 4, can_write: true, can_pvp: true, backup_id: 'synthetic-backup', ...patch }; }
/** The exact signed busy receipt shape the Java guard emits (structurally non-mutating). */
export function busyPayload(body, patch = {}) {
  const now = Date.now() / 1000;
  const request = JSON.parse(body);
  const base = {
    nonce: request.nonce, world_id: config.worldId, save_path: config.worldPath, host: '127.0.0.1', port: 25565,
    actor_uuid: config.actorUuid, action: request.action, target_uuid: request.target_uuid,
    issued_at: now, expires_at: now + 4, can_write: false, can_pvp: false, backup_id: '',
    busy: true, retry_after_ms: 250, attested_at: Math.max(1, Math.floor(now - 1)),
  };
  return { ...base, ...patch };
}
export function sign(p, { signature } = {}) { const raw = Buffer.from(typeof p === 'string' ? p : JSON.stringify(p)); return { status: 200, raw, signature: signature || createHmac('sha256', config.secret).update(raw).digest('hex') }; }
export function syntheticGuard(patch = () => ({}), cfg = config) {
  const calls = [];
  const guard = new GuardClient(cfg, { transport: async (_url, _secret, body) => { const request = JSON.parse(body); calls.push(request); const p = payload(body, patch(request)); return sign(p); } });
  guard.calls = calls; return guard;
}
/**
 * Guard that can return the real busy receipt for chosen actions. `isBusy(request, attempt)`
 * decides per call; everything else gets a normal permit.
 */
export function syntheticBusyGuard(isBusy = () => false, { patch = {}, cfg = config } = {}) {
  const calls = [];
  const guard = new GuardClient(cfg, { transport: async (_url, _secret, body) => {
    const request = JSON.parse(body); calls.push(request);
    return isBusy(request, calls.length) ? sign(busyPayload(body, patch)) : sign(payload(body));
  } });
  guard.calls = calls; return guard;
}
export { GuardBusy };
export class FakeGameAdapter {
  constructor() { this.connected = false; this.actor = ACTOR; this.calls = []; this.closeCount = 0; this.stopCount = 0; }
  onDisconnect(callback) { this.disconnect = callback; }
  async connect(_params, _signal, beforeSocket) { await beforeSocket(); this.calls.push('connect'); this.connected = true; return { uuid: this.actor }; }
  identity() { return this.connected ? this.actor : ''; }
  stop() { this.stopCount++; }
  async close() { this.connected = false; this.closeCount++; }
  async run(name, args, signal, permit) { if (signal.aborted) throw new Error(); await permit(name === 'inventory' ? 'observe' : 'write'); this.calls.push({ name, args }); return { ok: true }; }
  observe() { return { health: 17, food: 12, position: { x: 1, y: 64, z: 2 }, dimension: 'overworld', inventory: [{ name: 'dirt', count: 2 }], nearby_entities: [], nearby_blocks: [] }; }
}
export const tick = () => new Promise((r) => setImmediate(r));
