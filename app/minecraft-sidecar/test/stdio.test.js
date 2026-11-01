import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { ENV, config } from './helpers.js';

const cwd = new URL('..', import.meta.url);
function childHarness(t, entry = 'index.mjs', args = []) {
  const child = spawn(process.execPath, [entry, ...args], { cwd, env: { SystemRoot: process.env.SystemRoot, PATH: process.env.PATH, ...ENV }, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  let stdout = '', stderr = '';
  child.stdout.on('data', (v) => { stdout += v; }); child.stderr.on('data', (v) => { stderr += v; });
  t.after(() => { if (child.exitCode === null) child.kill(); });
  const until = async (predicate, timeout = 4000) => {
    const end = Date.now() + timeout;
    while (!predicate(stdout) && Date.now() < end) await new Promise((r) => setTimeout(r, 10));
    assert.ok(predicate(stdout), `child condition timed out; stderr=${stderr}`);
  };
  return { child, until, output: () => stdout, diagnostics: () => stderr };
}
test('child emits ready then correlated static JSON-RPC errors with real newlines', async (t) => {
  const h = childHarness(t); await h.until((s) => s.includes('"ready"\n') || s.includes('"ready"'));
  h.child.stdin.write('{broken}\n');
  h.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'unknown', params: {} })}\n`);
  h.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'observe', params: {} })}\n`);
  await h.until((s) => s.includes('"id":2'));
  const lines = h.output().trim().split('\n').map(JSON.parse);
  assert.equal(lines[0].params.type, 'ready');
  assert.ok(lines.some((l) => l.error?.code === -32700));
  assert.equal(lines.find((l) => l.id === 1).error.code, -32601);
  assert.equal(lines.find((l) => l.id === 2).error.code, -32001);
  assert.equal(h.diagnostics(), '');
  h.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 3, method: 'shutdown', params: {} })}\n`);
  await h.until((s) => s.includes('"id":3'));
  if (h.child.exitCode === null) await once(h.child, 'exit');
  assert.equal(h.child.exitCode, 0);
});
test('child drops oversized chunked line and processes next request', async (t) => {
  const h = childHarness(t); await h.until((s) => s.includes('"ready"'));
  h.child.stdin.write('x'.repeat(60000)); h.child.stdin.write('y'.repeat(10000));
  h.child.stdin.write(`\n${JSON.stringify({ jsonrpc: '2.0', id: 8, method: 'unknown', params: {} })}\n`);
  await h.until((s) => s.includes('"id":8'));
  assert.equal(h.output().trim().split('\n').filter((s) => JSON.parse(s).error?.code === -32600).length, 1);
  assert.doesNotMatch(h.output(), /xxxxx|yyyyy/);
});
test('auth CLI refuses non-interactive invocation before auth loading', async (t) => {
  const h = childHarness(t, 'auth.mjs', ['SyntheticBot']);
  if (h.child.exitCode === null) await once(h.child, 'exit');
  assert.equal(h.child.exitCode, 2); assert.equal(h.output(), '');
  assert.match(h.diagnostics(), /interactive terminal/); assert.doesNotMatch(h.diagnostics(), /token|device.?code|https?:/i);
});
test('invalid trusted config never emits path/secret or protocol ready', async (t) => {
  const child = spawn(process.execPath, ['index.mjs'], { cwd, env: { ...ENV, PAWBOT_MC_GUARD_URL: 'https://remote.invalid' }, stdio: ['pipe', 'pipe', 'pipe'] });
  t.after(() => { if (child.exitCode === null) child.kill(); });
  let out = '', err = ''; child.stdout.on('data', (s) => { out += s; }); child.stderr.on('data', (s) => { err += s; });
  await once(child, 'exit'); assert.equal(child.exitCode, 2); assert.equal(out, ''); assert.equal(err, 'Minecraft sidecar configuration invalid\n');
  assert.doesNotMatch(err, new RegExp(config.secret));
});
