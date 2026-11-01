import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createAuthflow, canonicalAuthDir, AuthRequiredError } from '../src/auth.js';

class FakeAuthflow {
  constructor(username, cache, options, callback) { Object.assign(this, { username, cache, options, callback }); this.msa = { authDeviceCode: async () => { throw new Error('should be disabled'); } }; }
}
async function temporary(t) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'mc-sidecar-synthetic-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true })); return fs.realpath(dir);
}
test('runtime auth uses confined cache and refuses any device flow or callback output', async (t) => {
  const dir = await temporary(t); let displays = 0;
  const flow = await createAuthflow('SyntheticBot', dir, { Authflow: FakeAuthflow, Titles: { MinecraftNintendoSwitch: 'synthetic' }, display: () => displays++ });
  await assert.rejects(flow.msa.authDeviceCode(), AuthRequiredError);
  assert.throws(() => flow.callback({ user_code: 'NOT-SHOWN' }), AuthRequiredError); assert.equal(displays, 0);
  const cache = flow.cache({ cacheName: 'live' }); await cache.setCached({ synthetic: 'test-data-only' });
  assert.deepEqual(await cache.getCached(), { synthetic: 'test-data-only' });
  const entries = await fs.readdir(dir); assert.equal(entries.length, 1); assert.match(entries[0], /^[0-9a-f]{24}_live\.json$/);
  assert.throws(() => flow.cache({ cacheName: '../escape' }));
});
test('installed Authflow constructor remains noninteractive with a synthetic empty local cache', async (t) => {
  const dir = await temporary(t); const flow = await createAuthflow('SyntheticBot', dir);
  await assert.rejects(flow.msa.authDeviceCode(), AuthRequiredError); assert.deepEqual(await fs.readdir(dir), []);
});
test('only explicitly interactive auth callback receives local device code', async (t) => {
  const dir = await temporary(t); const codes = [];
  const flow = await createAuthflow('SyntheticBot', dir, { interactive: true, Authflow: FakeAuthflow, Titles: { MinecraftNintendoSwitch: 'synthetic' }, display: (code) => codes.push(code.user_code) });
  flow.callback({ user_code: 'SYNTHETIC' }); assert.deepEqual(codes, ['SYNTHETIC']);
});
test('auth dir rejects missing runtime dir, relative path, and linked cache file', async (t) => {
  const dir = await temporary(t);
  await assert.rejects(canonicalAuthDir('relative')); await assert.rejects(canonicalAuthDir(path.join(dir, 'missing')));
  const flow = await createAuthflow('SyntheticBot', dir, { Authflow: FakeAuthflow, Titles: { MinecraftNintendoSwitch: 'synthetic' } });
  const cache = flow.cache({ cacheName: 'live' }); await cache.setCached({ a: 1 });
  const [file] = await fs.readdir(dir); const target = path.join(dir, 'target'); await fs.writeFile(target, '{}');
  try { await fs.unlink(path.join(dir, file)); await fs.symlink(target, path.join(dir, file)); }
  catch (error) { if (error.code === 'EPERM') { t.diagnostic('Windows symlink privilege unavailable; directory link rejection is separately enforced by lstat.'); return; } throw error; }
  await assert.rejects(cache.setCached({ a: 2 }));
});
