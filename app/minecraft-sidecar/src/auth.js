import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomBytes } from 'node:crypto';

export class AuthRequiredError extends Error { constructor() { super('Local authentication setup required'); } }
const cacheNames = new Set(['msal', 'live', 'sisu', 'xbl', 'bed', 'mca', 'mcs', 'pfb']);
export async function canonicalAuthDir(value, { create = false } = {}) {
  if (!value || !path.isAbsolute(value) || value.startsWith('\\\\')) throw new AuthRequiredError();
  const resolved = path.resolve(value);
  let current = path.parse(resolved).root;
  const parts = resolved.slice(current.length).split(path.sep).filter(Boolean);
  for (const part of parts) {
    current = path.join(current, part);
    try { const stat = await fs.lstat(current); if (stat.isSymbolicLink() || !stat.isDirectory()) throw new AuthRequiredError(); }
    catch (e) { if (e.code !== 'ENOENT' || !create) throw new AuthRequiredError(); await fs.mkdir(current, { mode: 0o700 }); }
  }
  if (await fs.realpath(resolved) !== resolved) throw new AuthRequiredError();
  return resolved;
}
export async function createAuthflow(username, authDir, { interactive = false, display = () => {}, Authflow, Titles } = {}) {
  const dir = await canonicalAuthDir(authDir, { create: interactive });
  if (!Authflow) { const api = (await import('prismarine-auth')).default; Authflow = api.Authflow; Titles = api.Titles; }
  const cache = ({ cacheName }) => {
    if (!cacheNames.has(cacheName)) throw new AuthRequiredError();
    const filename = `${createHash('sha256').update(username).digest('hex').slice(0, 24)}_${cacheName}.json`;
    const location = path.join(dir, filename); let value;
    const read = async () => {
      await canonicalAuthDir(dir);
      try { const stat = await fs.lstat(location); if (stat.isSymbolicLink() || !stat.isFile() || stat.size > 1048576) throw new AuthRequiredError(); return JSON.parse(await fs.readFile(location, 'utf8')); }
      catch (error) { if (error.code === 'ENOENT') return {}; throw new AuthRequiredError(); }
    };
    const set = async (data) => {
      await canonicalAuthDir(dir);
      try { const stat = await fs.lstat(location); if (stat.isSymbolicLink() || !stat.isFile()) throw new AuthRequiredError(); } catch (e) { if (e.code !== 'ENOENT') throw e; }
      const text = JSON.stringify(data); if (Buffer.byteLength(text) > 1048576) throw new AuthRequiredError();
      const tmp = path.join(dir, `.${randomBytes(16).toString('hex')}.tmp`);
      await fs.writeFile(tmp, text, { flag: 'wx', mode: 0o600 });
      await fs.rename(tmp, location); value = data;
    };
    return { getCached: async () => value ??= await read(), setCached: set, setCachedPartial: async (data) => set({ ...(value ??= await read()), ...data }), reset: async () => { await set({}); return {}; } };
  };
  const flow = new Authflow(username, cache, { authTitle: Titles.MinecraftNintendoSwitch, deviceType: 'Nintendo', flow: 'live' }, (code) => {
    if (!interactive) throw new AuthRequiredError();
    // Only a human-invoked terminal setup process receives this callback.
    display(code);
  });
  if (!interactive) {
    // This also prevents even requesting a fresh device code in the protocol process.
    flow.msa.authDeviceCode = async () => { throw new AuthRequiredError(); };
  }
  return flow;
}
