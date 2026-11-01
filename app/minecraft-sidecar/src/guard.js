import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import http from 'node:http';
import path from 'node:path';

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// These are the Java guard's authoritative config bounds; the client must not be more permissive.
const SECRET_MIN = 32, SECRET_MAX = 256, WORLD_ID = /^[A-Za-z0-9_.-]{1,64}$/, PVP_MAX = 16;
// A busy receipt is structurally non-mutating: exactly these fields, empty backup id, both
// capabilities false. Anything else claiming to be busy is a malformed (or forged) guard response.
const BUSY_KEYS = ['nonce', 'world_id', 'save_path', 'host', 'port', 'actor_uuid', 'action', 'target_uuid', 'issued_at', 'expires_at', 'can_write', 'can_pvp', 'backup_id', 'busy', 'retry_after_ms', 'attested_at'];
export class GuardError extends Error { constructor() { super('Guard denied the operation'); this.name = 'GuardError'; } }
/** Signed busy receipt: the guard is alive and not revoked, but a snapshot owns the server thread. */
export class GuardBusy extends GuardError {
  constructor(retryAfterMs) { super('Guard is busy with a snapshot'); this.name = 'GuardBusy'; this.retryAfterMs = retryAfterMs; }
}
/** Transport failure or an unusable (5xx/timeout) answer: bounded retry, then quarantine. */
export class GuardUnavailable extends GuardError { constructor() { super('Guard unavailable'); this.name = 'GuardUnavailable'; } }
export function readGuardConfig(env = process.env) {
  let url, pvpUuids;
  const rawUrl = env.PAWBOT_MC_GUARD_URL || 'http://127.0.0.1:6198';
  if (!/^http:\/\/127\.0\.0\.1(?::[1-9][0-9]{0,4})?(?:\/v1\/permit|\/)?$/.test(rawUrl)) throw new GuardError();
  try { url = new URL(rawUrl); pvpUuids = JSON.parse(env.PAWBOT_MC_PVP_UUIDS || '[]'); } catch { throw new GuardError(); }
  const secret = env.PAWBOT_MC_GUARD_SECRET || '', worldId = env.PAWBOT_MC_WORLD_ID || '', worldPath = env.PAWBOT_MC_WORLD_PATH || '', actorUuid = (env.PAWBOT_MC_ACTOR_UUID || '').toLowerCase();
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password || !['/', '/v1/permit'].includes(url.pathname) || url.search || url.hash || secret.length < SECRET_MIN || secret.length > SECRET_MAX || /[\r\n]/.test(secret) || !WORLD_ID.test(worldId) || !(path.isAbsolute(worldPath) || path.win32.isAbsolute(worldPath)) || !UUID.test(actorUuid) || !Array.isArray(pvpUuids) || pvpUuids.length > PVP_MAX || pvpUuids.some((id) => typeof id !== 'string' || !UUID.test(id.toLowerCase()))) throw new GuardError();
  return Object.freeze({ secret, worldId, worldPath, actorUuid, pvpUuids: new Set(pvpUuids.map((id) => id.toLowerCase())), writeEnabled: env.PAWBOT_MC_WRITE_ENABLED === 'true', pvpEnabled: env.PAWBOT_MC_PVP_ENABLED === 'true', authDir: env.PAWBOT_MC_AUTH_DIR || '', guardUrl: url.origin });
}
// A literal IP, bounded response stream, no redirects, one deadline including body receipt.
export function requestPermit(url, secret, body, timeoutMs, signal) {
  return new Promise((resolve, reject) => {
    const req = http.request(new URL('/v1/permit', url), { method: 'POST', signal, headers: { Authorization: `Bearer ${secret}`, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } });
    const fail = () => { clearTimeout(timer); req.destroy(); reject(new GuardUnavailable()); };
    const timer = setTimeout(fail, timeoutMs);
    req.on('error', () => { clearTimeout(timer); reject(new GuardUnavailable()); });
    req.on('response', (res) => {
      const chunks = []; let length = 0;
      if (res.statusCode !== 200) { res.destroy(); fail(); return; }
      res.on('data', (chunk) => { length += chunk.length; if (length > 16384) { res.destroy(); fail(); } else chunks.push(chunk); });
      res.on('error', fail);
      res.on('end', () => { clearTimeout(timer); resolve({ status: res.statusCode, raw: Buffer.concat(chunks), signature: res.headers['x-pawbot-signature'] }); });
    });
    req.end(body);
  });
}
export class GuardClient {
  constructor(config, { transport = requestPermit, now = () => Date.now() } = {}) { this.config = config; this.transport = transport; this.now = now; }
  async permit(action, targetUuid = '', timeoutMs, signal) {
    if (!['connect', 'observe', 'write', 'pvp', 'backup', 'revoke'].includes(action) || ((action === 'pvp') !== Boolean(targetUuid)) || (targetUuid && !UUID.test(targetUuid))) throw new GuardError();
    if (action === 'write' && !this.config.writeEnabled) throw new GuardError();
    if (action === 'pvp' && (!this.config.pvpEnabled || !this.config.writeEnabled || !this.config.pvpUuids.has(targetUuid))) throw new GuardError();
    const nonce = randomBytes(16).toString('hex');
    const body = JSON.stringify({ nonce, actor_uuid: this.config.actorUuid, action, target_uuid: targetUuid });
    let response;
    try { response = await this.transport(this.config.guardUrl, this.config.secret, body, timeoutMs ?? (action === 'backup' ? 60000 : 10000), signal); } catch { throw new GuardUnavailable(); }
    const { raw, signature } = response;
    if (response.status !== 200 || !Buffer.isBuffer(raw) || !raw.length || raw.length > 16384 || typeof signature !== 'string' || !/^[0-9a-f]{64}$/.test(signature)) throw new GuardError();
    // Never inspect attestation fields before authenticating its exact original bytes.
    const expected = createHmac('sha256', this.config.secret).update(raw).digest();
    if (!timingSafeEqual(expected, Buffer.from(signature, 'hex'))) throw new GuardError();
    let p;
    try { p = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw)); } catch { throw new GuardError(); }
    const now = this.now() / 1000;
    // A revoke receipt echoes the guard's current LAN port: `0` when LAN is closed, or a port that
    // changed after this process connected. Revoke alone tolerates that; every action permit must
    // still report an exact integer 1..65535 (and the controller pins it to the connect port).
    const portOk = action === 'revoke' ? Number.isInteger(p?.port) && p.port >= 0 && p.port <= 65535 : Number.isInteger(p?.port) && p.port >= 1 && p.port <= 65535;
    if (!p || Array.isArray(p) || !portOk || p.nonce !== nonce || p.action !== action || p.actor_uuid !== this.config.actorUuid || p.target_uuid !== targetUuid || p.world_id !== this.config.worldId || p.save_path !== this.config.worldPath || p.host !== '127.0.0.1' || !Number.isFinite(p.issued_at) || !Number.isFinite(p.expires_at) || p.issued_at > now + 1 || p.issued_at < now - 5 || p.expires_at <= now || p.expires_at <= p.issued_at || p.expires_at > p.issued_at + 5 || typeof p.can_write !== 'boolean' || typeof p.can_pvp !== 'boolean' || typeof p.backup_id !== 'string' || p.backup_id.length > 256) throw new GuardError();
    // Optional additive busy flag. Absent means false, so every pre-busy fixture stays valid.
    const busy = Object.hasOwn(p, 'busy') ? p.busy : false;
    if (typeof busy !== 'boolean') throw new GuardError();
    if (busy) {
      // Exact shape: no extra secret/private field and no mutation-capable value may ride along.
      if (Object.keys(p).length !== BUSY_KEYS.length || !BUSY_KEYS.every((key) => Object.hasOwn(p, key))) throw new GuardError();
      if (p.backup_id !== '' || p.can_write !== false || p.can_pvp !== false) throw new GuardError();
      if (!Number.isInteger(p.retry_after_ms) || p.retry_after_ms < 50 || p.retry_after_ms > 5000) throw new GuardError();
      if (!Number.isInteger(p.attested_at) || p.attested_at <= 0 || p.attested_at > p.issued_at + 1) throw new GuardError();
      // A busy observe is a real, non-mutating observation; every mutation action is paused.
      if (action === 'observe') return Object.freeze({ ...p });
      if (action === 'revoke') throw new GuardError();
      throw new GuardBusy(p.retry_after_ms);
    }
    if (['backup', 'connect', 'write', 'pvp'].includes(action) && !/^[A-Za-z0-9_.-]{1,256}$/.test(p.backup_id)) throw new GuardError();
    if ((action === 'write' && !p.can_write) || (action === 'pvp' && (!p.can_pvp || !p.can_write))) throw new GuardError();
    return Object.freeze({ ...p });
  }
}
