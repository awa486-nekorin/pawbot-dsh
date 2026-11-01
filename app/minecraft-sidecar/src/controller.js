import { GuardBusy, GuardError } from './guard.js';
import { validateSkill } from './skills.js';

export class RpcError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const invalid = () => new RpcError(-32602, 'Invalid parameters');
const unavailable = () => new RpcError(-32001, 'Connection unavailable');
const cancelled = () => new RpcError(-32004, 'Operation cancelled');
const guardBusy = () => new RpcError(-32008, 'Guard busy');
function aborted(signal) { if (signal.aborted) throw cancelled(); }
function raceAbort(promise, signal) {
  return new Promise((resolve, reject) => {
    const stop = () => { cleanup(); reject(cancelled()); };
    const cleanup = () => signal.removeEventListener('abort', stop);
    if (signal.aborted) return stop();
    signal.addEventListener('abort', stop, { once: true });
    Promise.resolve(promise).then((v) => { cleanup(); resolve(v); }, (e) => { cleanup(); reject(e); });
  });
}
export class SidecarController {
  constructor({ config, guard, adapter, emit = () => {} }) {
    Object.assign(this, { config, guard, adapter, emit });
    this.connected = false; this.connecting = null; this.queue = []; this.active = null;
    this.closed = false; this.quarantined = false; this.port = null; this.watchBusy = false;
    this.pausedUntil = 0;
    adapter.onDisconnect?.(() => { void this.quarantine('connection_ended'); });
  }
  async connect(params) {
    if (this.closed || this.connected || this.connecting || this.quarantined) throw unavailable();
    const expected = ['host', 'port', 'version', 'username', 'auth_mode', 'timeout_sec'];
    if (!params || Object.keys(params).length !== expected.length || !expected.every((k) => Object.hasOwn(params, k)) || params.host !== '127.0.0.1' || !Number.isInteger(params.port) || params.port < 1 || params.port > 65535 || params.version !== '1.20.1' || typeof params.username !== 'string' || !/^[A-Za-z0-9_]{1,16}$/.test(params.username) || !['microsoft', 'offline'].includes(params.auth_mode) || !Number.isInteger(params.timeout_sec) || params.timeout_sec < 1 || params.timeout_sec > 30) throw invalid();
    const ac = new AbortController(); this.connecting = ac; this.port = params.port;
    let timedOut = false;
    const deadline = Date.now() + params.timeout_sec * 1000;
    const timer = setTimeout(() => { timedOut = true; ac.abort(); }, params.timeout_sec * 1000);
    try {
      // Exactly one snapshot per connect: the guard's `connect` action performs the mandatory
      // fresh save+snapshot itself, so a separate `backup` request before it would double the copy.
      let socketAuthorized = false;
      const beforeSocket = async () => {
        aborted(ac.signal);
        const fresh = await this.permitThroughBusy('connect', '', 60000, ac.signal, deadline);
        if (fresh.port !== this.port || !fresh.backup_id) throw new GuardError();
        aborted(ac.signal); socketAuthorized = true;
      };
      const identity = await raceAbort(this.adapter.connect({ ...params, auth_dir: this.config.authDir }, ac.signal, beforeSocket), ac.signal);
      aborted(ac.signal);
      if (!socketAuthorized || identity.uuid !== this.config.actorUuid) throw new GuardError();
      this.connected = true;
      await this.checkPermit('observe', '', ac.signal);
      // The guard is the only periodic-snapshot scheduler; this process starts no backup timer.
      this.watchdog = setInterval(() => { void this.watchGuard(); }, 250); this.watchdog.unref?.();
      this.emit({ type: 'connected', uuid: this.config.actorUuid, host: '127.0.0.1', port: this.port, world_id: this.config.worldId });
      return { connected: true, uuid: this.config.actorUuid, world_id: this.config.worldId, backup_completed: true };
    } catch (error) { ac.abort(); await this.quarantine('connect_failed'); if (timedOut) throw new RpcError(-32005, 'Operation timed out'); throw error; }
    finally { clearTimeout(timer); this.connecting = null; }
  }
  /** Requests a permit, waiting out signed busy windows without spending the caller's budget. */
  async permitThroughBusy(action, target, timeoutMs, signal, deadline) {
    for (;;) {
      aborted(signal);
      try {
        return await raceAbort(this.guard.permit(action, target, timeoutMs, signal), signal);
      } catch (error) {
        if (!(error instanceof GuardBusy)) throw error;
        const remaining = deadline - Date.now();
        if (remaining <= 0) throw error;
        const wait = Math.min(Math.max(50, error.retryAfterMs || 250), remaining);
        await raceAbort(new Promise((resolve) => setTimeout(resolve, wait)), signal);
      }
    }
  }
  ensureConnected() {
    if (!this.connected || this.closed || this.quarantined || this.adapter.identity?.() !== this.config.actorUuid) throw unavailable();
  }
  async checkPermit(action, target = '', signal) {
    this.ensureConnected();
    if (signal) aborted(signal);
    const permit = await this.guard.permit(action, target, 650, signal);
    if (signal) aborted(signal);
    this.ensureConnected();
    if (permit.port !== this.port) throw new GuardError();
    return permit;
  }
  async observe() { await this.checkPermit('observe'); return this.adapter.observe(); }
  skill(request) {
    this.ensureConnected();
    if (Date.now() < this.pausedUntil) throw guardBusy();
    if (!request || Object.keys(request).length !== 3 || !validateSkill(request.name, request.args) || !Number.isInteger(request.timeout_sec) || request.timeout_sec < 1 || request.timeout_sec > 30) throw invalid();
    if (this.queue.length >= 8) throw new RpcError(-32003, 'Skill queue full');
    return new Promise((resolve, reject) => {
      const job = { ...request, resolve, reject, ac: new AbortController() };
      job.timer = setTimeout(() => {
        job.timedOut = true; job.ac.abort();
        if (this.active === job) void this.quarantine('operation_timeout');
        else { this.queue = this.queue.filter((q) => q !== job); reject(new RpcError(-32005, 'Operation timed out')); }
      }, request.timeout_sec * 1000);
      this.queue.push(job); this.emit({ type: 'queued', skill: job.name, depth: this.queue.length });
      void this.pump();
    });
  }
  async pump() {
    if (this.active || this.closed || this.quarantined || !this.connected) return;
    if (Date.now() < this.pausedUntil) return; // a busy window pauses admissions; watchdog resumes it
    const job = this.queue.shift(); if (!job) return;
    this.active = job;
    try {
      aborted(job.ac.signal);
      const checkpoint = async (action = 'write', target = '') => {
        if (action === 'pvp' && (!this.config.pvpEnabled || !this.config.writeEnabled || !this.config.pvpUuids.has(target))) throw new GuardError();
        if (action === 'write' && !this.config.writeEnabled) throw new GuardError();
        if (!['observe', 'write', 'pvp'].includes(action)) throw new GuardError();
        job.guardAction = action; job.guardTarget = target;
        await this.checkPermit(action, target, job.ac.signal);
      };
      // inventory and set_home are read-only local skills: set_home only stores in-memory coordinates
      // (adapter.js permits 'observe'), so admitting it as a write would deny a non-mutating skill.
      await checkpoint(['inventory', 'set_home'].includes(job.name) ? 'observe' : 'write');
      const value = await raceAbort(this.adapter.run(job.name, job.args, job.ac.signal, checkpoint), job.ac.signal);
      aborted(job.ac.signal); job.resolve(value); this.emit({ type: 'skill_complete', skill: job.name, ok: true });
    } catch (error) {
      if (error instanceof GuardBusy) {
        // A signed busy receipt is not a denial: stop the active action, pause and retry the next
        // choice. Never replay the interrupted mutation.
        this.adapter.stop();
        this.pause(error.retryAfterMs);
        job.reject(guardBusy());
        this.emit({ type: 'skill_complete', skill: job.name, ok: false });
      } else {
        job.reject(job.timedOut ? new RpcError(-32005, 'Operation timed out') : error);
        this.emit({ type: 'skill_complete', skill: job.name, ok: false });
        if (error instanceof GuardError) await this.quarantine('guard_unavailable');
      }
    } finally { clearTimeout(job.timer); this.active = null; void this.pump(); }
  }
  pause(retryAfterMs) {
    const wait = Number.isInteger(retryAfterMs) && retryAfterMs >= 50 && retryAfterMs <= 5000 ? retryAfterMs : 250;
    this.pausedUntil = Math.max(this.pausedUntil, Date.now() + wait);
    // Queue admission resumes when the signed busy window is over, even if the caller is idle.
    clearTimeout(this.pauseTimer);
    this.pauseTimer = setTimeout(() => { this.pauseTimer = undefined; void this.pump(); }, wait + 5);
    this.pauseTimer.unref?.();
  }
  async cancel() {
    for (const job of this.queue.splice(0)) { clearTimeout(job.timer); job.ac.abort(); job.reject(cancelled()); }
    this.connecting?.abort();
    if (this.active) { this.active.ac.abort(); await this.quarantine('operation_cancelled'); }
    else if (this.connecting) await this.quarantine('operation_cancelled');
    else this.adapter.stop();
    return { cancelled: true };
  }
  async watchGuard() {
    if (!this.connected || this.watchBusy || this.closed || this.quarantined) return;
    this.watchBusy = true;
    try {
      await this.checkPermit(this.active?.guardAction || 'observe', this.active?.guardTarget || '');
    } catch (error) {
      if (error instanceof GuardBusy) {
        // The guard is alive and doing exactly what it should: cancel the active action, stay
        // connected, and let a later tick resume. Nothing is replayed.
        this.adapter.stop();
        this.pause(error.retryAfterMs);
      } else {
        await this.quarantine('guard_unavailable');
      }
    } finally { this.watchBusy = false; }
  }
  async quarantine(reason) {
    if (this.quarantined) return;
    this.quarantined = true; this.connected = false; this.connecting?.abort();
    clearInterval(this.watchdog); clearTimeout(this.pauseTimer);
    for (const job of this.queue.splice(0)) { clearTimeout(job.timer); job.ac.abort(); job.reject(cancelled()); }
    this.active?.ac.abort(); this.adapter.stop();
    await this.adapter.close(); this.emit({ type: 'disconnected', reason });
  }
  async shutdown() {
    if (!this.closed) { this.closed = true; await this.quarantine('shutdown'); this.emit({ type: 'shutdown' }); }
    return { stopped: true };
  }
}
