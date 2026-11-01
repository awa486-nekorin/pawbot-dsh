import { readGuardConfig, GuardClient, GuardError } from './guard.js';
import { MineflayerAdapter } from './adapter.js';
import { SidecarController, RpcError } from './controller.js';
import { AuthRequiredError } from './auth.js';

export const MAX_LINE = 65536;
const errors = Object.freeze({
  '-32700': 'Parse error', '-32600': 'Invalid request', '-32601': 'Method not found', '-32602': 'Invalid parameters', '-32603': 'Operation failed',
  '-32001': 'Connection unavailable', '-32002': 'Guard denied the operation', '-32003': 'Skill queue full', '-32004': 'Operation cancelled', '-32005': 'Operation timed out', '-32006': 'Local authentication setup required', '-32007': 'Request capacity exceeded', '-32008': 'Guard busy',
});
const errorBody = (code) => ({ code, message: errors[code] });
const plain = (v) => v && typeof v === 'object' && !Array.isArray(v);
export function createRuntime({ config, guard, adapter, write }) {
  const emit = (params) => { if (Buffer.byteLength(JSON.stringify(params)) <= 4096) write({ jsonrpc: '2.0', method: 'event', params }); };
  const controller = new SidecarController({ config, guard, adapter, emit });
  const inflight = new Set(); let stopping = false;
  async function dispatch(message) {
    if (!plain(message) || message.jsonrpc !== '2.0' || !Number.isSafeInteger(message.id) || message.id <= 0 || typeof message.method !== 'string' || !plain(message.params) || Object.keys(message).length !== 4) {
      write({ jsonrpc: '2.0', id: null, error: errorBody(-32600) }); return;
    }
    const { id, method, params } = message;
    if (inflight.has(id) || stopping) { write({ jsonrpc: '2.0', id, error: errorBody(-32600) }); return; }
    if (inflight.size >= 18 || (inflight.size >= 16 && !['cancel', 'shutdown'].includes(method))) { write({ jsonrpc: '2.0', id, error: errorBody(-32007) }); return; }
    inflight.add(id);
    try {
      let result;
      if (method === 'connect') result = await controller.connect(params);
      else if (method === 'skill') result = await controller.skill(params);
      else if (['observe', 'cancel', 'shutdown'].includes(method)) {
        if (Object.keys(params).length) throw new RpcError(-32602, errors[-32602]);
        if (method === 'observe') result = await controller.observe();
        if (method === 'cancel') result = await controller.cancel();
        if (method === 'shutdown') { stopping = true; result = await controller.shutdown(); }
      } else throw new RpcError(-32601, errors[-32601]);
      if (Buffer.byteLength(JSON.stringify(result)) > 32768) throw new Error();
      write({ jsonrpc: '2.0', id, result });
    } catch (error) {
      const code = error instanceof GuardError ? -32002 : error instanceof AuthRequiredError ? -32006 : error instanceof RpcError && Object.hasOwn(errors, error.code) ? error.code : -32603;
      write({ jsonrpc: '2.0', id, error: errorBody(code) });
    } finally { inflight.delete(id); }
    return stopping;
  }
  return { controller, dispatch, get stopping() { return stopping; } };
}
// Byte parser drops the whole oversized line, including subsequent chunks; bounded memory.
export function createLineParser(onLine, onError) {
  let parts = [], bytes = 0, dropping = false;
  return (chunk) => {
    let start = 0;
    for (let i = 0; i <= chunk.length; i++) {
      if (i !== chunk.length && chunk[i] !== 10) continue;
      const segment = chunk.subarray(start, i); start = i + 1;
      if (!dropping) {
        bytes += segment.length;
        if (bytes > MAX_LINE) { parts = []; bytes = 0; dropping = true; onError(); }
        else if (segment.length) parts.push(segment);
      }
      if (i < chunk.length) {
        if (!dropping) {
          try { onLine(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(parts)).replace(/\r$/, '')); } catch { onError(); }
        }
        parts = []; bytes = 0; dropping = false;
      }
    }
  };
}
export async function runStdio() {
  const output = process.stdout.write.bind(process.stdout), diagnostic = process.stderr.write.bind(process.stderr);
  // A separate writer owns protocol stdout. Libraries may not print even asynchronously.
  const discard = (_data, encoding, callback) => { (typeof encoding === 'function' ? encoding : callback)?.(); return true; };
  process.stdout.write = discard; process.stderr.write = discard;
  console.log = console.info = console.warn = console.error = console.debug = () => {};
  delete process.env.DEBUG; delete process.env.NODE_DEBUG;
  let config;
  try { config = readGuardConfig(); } catch { diagnostic('Minecraft sidecar configuration invalid\n'); process.exitCode = 2; return; }
  let ending = false;
  const send = (message) => {
    if (ending) return;
    if (process.stdout.writableLength > 262144) { void finish(); return; }
    output(`${JSON.stringify(message)}\n`);
  };
  const runtime = createRuntime({ config, guard: new GuardClient(config), adapter: new MineflayerAdapter(), write: send });
  const finish = async () => {
    if (ending) return; ending = true; process.stdin.pause();
    await runtime.controller.shutdown();
    output('', () => process.exit(0));
  };
  const parser = createLineParser((line) => {
    if (ending) return;
    let message;
    try { message = JSON.parse(line); } catch { send({ jsonrpc: '2.0', id: null, error: errorBody(-32700) }); return; }
    void runtime.dispatch(message).then((stop) => { if (stop) void finish(); });
  }, () => send({ jsonrpc: '2.0', id: null, error: errorBody(-32600) }));
  process.stdin.on('data', parser); process.stdin.on('end', () => { void finish(); });
  process.on('SIGTERM', () => { void finish(); }); process.on('SIGINT', () => { void finish(); });
  process.on('uncaughtException', () => { diagnostic('Minecraft sidecar stopped\n'); void finish(); });
  process.on('unhandledRejection', () => { diagnostic('Minecraft sidecar stopped\n'); void finish(); });
  output(`${JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'ready', protocol_version: 1 } })}\n`);
}
