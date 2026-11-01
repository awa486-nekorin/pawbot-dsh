import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { MineflayerAdapter } from '../src/adapter.js';
import { ACTOR, TARGET, OTHER, ENV, config, CONNECT, syntheticGuard } from './helpers.js';
import { SidecarController } from '../src/controller.js';
import { AuthRequiredError } from '../src/auth.js';
import { GuardError, readGuardConfig } from '../src/guard.js';
import { validateSkill } from '../src/skills.js';

class Vec3 {
  constructor(x, y, z) { Object.assign(this, { x, y, z }); }
}
class Movements { constructor() {} }
class GoalNear { constructor(x, y, z, radius) { Object.assign(this, { x, y, z, radius }); } }
function game() {
  const bot = new EventEmitter(); const effects = [], controls = []; const blocks = new Map();
  const itemNames = ['dirt', 'oak_planks', 'stick', 'wheat_seeds', 'wheat', 'apple', 'iron_hoe'];
  bot.player = { uuid: ACTOR }; bot.health = 13; bot.food = 8; bot.game = { dimension: 'overworld' };
  bot.entity = { position: new Vec3(0, 64, 0) };
  bot.entities = {}; bot.players = {};
  bot.inventory = { items: () => itemNames.map((name, index) => ({ name, count: 32, type: index + 1 })) };
  bot.registry = { itemsByName: Object.fromEntries(itemNames.map((name, i) => [name, { id: i + 1 }])), blocksByName: { dirt: { id: 1 }, crafting_table: { id: 58 }, tnt: { id: 46 } }, foodsByName: { apple: { foodPoints: 4 } }, entitiesByName: { zombie: { type: 'hostile' }, cow: { type: 'animal' }, player: { type: 'player' } } };
  const makeBlock = (name, p, props = {}) => ({ name, type: name === 'air' ? 0 : 1, position: new Vec3(p.x, p.y, p.z), boundingBox: name === 'air' ? 'empty' : 'block', light: 15, skyLight: 15, getProperties: () => props });
  const setBlock = (name, p, props) => blocks.set(`${p.x},${p.y},${p.z}`, makeBlock(name, p, props));
  const getBlock = (p) => blocks.get(`${p.x},${p.y},${p.z}`) || makeBlock('air', p);
  bot.blockAt = getBlock; bot.canDigBlock = (b) => b.name !== 'air';
  bot.dig = async (b) => { effects.push(['dig', b.name]); setBlock('air', b.position); };
  bot.chat = (text) => effects.push(['chat', text]); bot.equip = async (item) => effects.push(['equip', item.name]);
  bot.craft = async (_recipe, count) => effects.push(['craft', count]); bot.recipesFor = () => [{ result: { count: 4 }, requiresTable: false }];
  bot.findBlock = () => null;
  bot.openContainer = async () => { effects.push(['open']); return { deposit: async (_id, _m, count) => effects.push(['deposit', count]), withdraw: async (_id, _m, count) => effects.push(['withdraw', count]), close: () => effects.push(['close']) }; };
  bot.placeBlock = async (block, face) => { effects.push(['place', block.name]); setBlock('wheat', { x: block.position.x + face.x, y: block.position.y + face.y, z: block.position.z + face.z }); };
  bot.activateBlock = async (block) => { effects.push(['activateBlock', block.name]); setBlock('farmland', block.position); };
  bot.activateEntity = async (entity) => effects.push(['breed', entity.name]);
  bot.attack = (entity) => effects.push(['attack', entity.uuid]); bot.consume = async () => effects.push(['consume']); bot.respawn = () => effects.push(['respawn']);
  bot.pathfinder = { setMovements: () => {}, goto: async (goal) => effects.push(['goto', goal]), setGoal: (goal) => goal ? effects.push(['goal', goal]) : controls.push(['goal', null]) };
  bot.pvp = { forceStop: () => controls.push(['pvp_stop']) };
  bot.stopDigging = () => controls.push(['stop_dig']); bot.deactivateItem = () => controls.push(['deactivate']); bot.clearControlStates = () => controls.push(['clear_controls']);
  bot.quit = () => effects.push(['quit']); bot.loadPlugin = () => {};
  const adapter = new MineflayerAdapter({ Vec3, Movements, goals: { GoalNear } });
  adapter.bot = bot; adapter.connected = true;
  let permits = [];
  const permit = async (action = 'write', target = '') => { permits.push({ action, target }); };
  const run = async (name, args) => adapter.run(name, args, new AbortController().signal, permit);
  return { bot, adapter, effects, controls, setBlock, run, permits, permit };
}

test('real adapter observes changing health inventory and loaded blocks without NBT/chat', async () => {
  const g = game(); g.setBlock('dirt', { x: 0, y: 63, z: 0 });
  g.bot.entities[1] = { type: 'hostile', name: 'zombie', uuid: OTHER, position: new Vec3(1, 64, 0), nbt: { syntheticPrivate: true } };
  const p = g.adapter.observe(); assert.equal(p.health, 13); assert.equal(p.food, 8); assert.equal(p.inventory[0].name, 'dirt');
  assert.ok(p.nearby_blocks.some((b) => b.name === 'dirt')); assert.equal(p.nearby_entities[0].name, 'zombie'); assert.doesNotMatch(JSON.stringify(p), /nbt|syntheticPrivate/);
  g.bot.health = 7; assert.equal(g.adapter.observe().health, 7);
});
test('real observe derives bounded game time weather light and roof from loaded state', () => {
  const g = game(); g.bot.time = { timeOfDay: 13000 }; g.bot.isRaining = true; g.bot.thunderState = 0;
  g.setBlock('dirt', { x: 0, y: 66, z: 0 });
  const p = g.adapter.observe();
  assert.equal(p.time_of_day, 13000); assert.equal(p.weather, 'rain'); assert.equal(p.light, 15); assert.equal(p.roof, true);
  g.bot.thunderState = 1; assert.equal(g.adapter.observe().weather, 'thunder');
  g.bot.isRaining = false; g.bot.thunderState = 0; assert.equal(g.adapter.observe().weather, 'clear');
  g.bot.time = { timeOfDay: 30000 }; assert.equal('time_of_day' in g.adapter.observe(), false);
});
test('real observe omits unknown time weather light and roof instead of inventing values', () => {
  const g = game(); delete g.bot.time; g.bot.isRaining = 'synthetic'; g.bot.blockAt = () => null;
  const p = g.adapter.observe();
  assert.equal('time_of_day' in p, false); assert.equal('weather' in p, false); assert.equal('light' in p, false); assert.equal('roof' in p, false);
  assert.deepEqual(Object.keys(p).filter((k) => !['health', 'food', 'position', 'dimension', 'inventory', 'nearby_entities', 'nearby_blocks', 'events'].includes(k)), []);
  g.bot.isRaining = true; assert.equal('weather' in g.adapter.observe(), true);
});
test('real observe exposes no display names private fields or player chat from loaded objects', () => {
  const g = game(); g.bot.entities[1] = { type: 'player', name: 'player', uuid: TARGET, username: 'SyntheticName', displayName: 'SyntheticName', position: new Vec3(1, 64, 0), nbt: { syntheticPrivate: true }, metadata: { syntheticPrivate: true } };
  g.bot.emit('chat', { username: 'SyntheticName', message: 'private synthetic chat' });
  const p = g.adapter.observe();
  assert.doesNotMatch(JSON.stringify(p), /SyntheticName|private synthetic chat|syntheticPrivate/);
  assert.equal(p.nearby_entities[0].uuid, TARGET);
  assert.deepEqual(Object.keys(p.nearby_entities[0]).filter((k) => !['type', 'name', 'uuid', 'position', 'distance'].includes(k)), []);
});
test('real adapter records bounded health-loss death and respawn observations without chat or identifiers', async () => {
  const g = connectionGame(); await g.adapter.connect(CONNECT, new AbortController().signal, async () => {});
  g.bot.health = 20; g.bot.emit('health');
  g.bot.health = 12; g.bot.emit('health');
  g.bot.emit('death'); g.bot.health = 0; g.bot.emit('health');
  g.bot.emit('respawn'); g.bot.health = 20; g.bot.emit('health');
  g.bot.emit('chat', { username: 'SyntheticName', message: 'private synthetic chat' });
  const p = g.adapter.observe();
  assert.deepEqual(p.events.map((e) => e.type), ['health_loss', 'death', 'respawn']);
  assert.deepEqual(p.events[0], { type: 'health_loss', from: 20, to: 12 });
  assert.doesNotMatch(JSON.stringify(p.events), /SyntheticName|private synthetic chat|[0-9a-f]{8}-/);
  assert.deepEqual(g.adapter.observe().events, []);
  for (let i = 0; i < 40; i++) { g.bot.health = 20 - (i % 10); g.bot.emit('health'); }
  const bounded = g.adapter.observe().events; assert.ok(bounded.length <= 16); assert.ok(bounded.every((e) => e.type === 'health_loss'));
  await g.adapter.close();
});
for (const name of ['move', 'explore']) test(`real ${name} uses bounded pathfinder goal`, async () => {
  const g = game(); await g.run(name, { x: 2, y: 64, z: 0, radius: 1 }); assert.ok(g.effects.some((e) => e[0] === 'goto')); assert.equal(g.permits[0].action, 'write');
  await assert.rejects(g.run(name, { x: 100, y: 64, z: 0, radius: 1 }));
});
test('real follow repeatedly goals local player for bounded duration and stops', async () => {
  const g = game(); const e = { type: 'player', uuid: TARGET, position: new Vec3(2, 64, 0) }; g.bot.entities[1] = e;
  await g.run('follow', { target_uuid: TARGET, duration_sec: 1 });
  assert.ok(g.effects.some((e) => e[0] === 'goal' && e[1] instanceof GoalNear)); assert.ok(g.controls.some((e) => e[0] === 'goal' && e[1] === null));
});
test('real chat and dig guard before effects', async () => {
  const g = game(); await g.run('chat', { text: 'hi' }); g.setBlock('dirt', { x: 1, y: 64, z: 0 });
  await g.run('dig', { x: 1, y: 64, z: 0 }); assert.deepEqual(g.effects.filter((e) => ['chat', 'dig'].includes(e[0])), [['chat', 'hi'], ['dig', 'dirt']]); assert.equal(g.permits.length, 2);
});
test('real collect scans local loaded blocks and guards each bounded dig', async () => {
  const g = game(); g.setBlock('dirt', { x: 1, y: 64, z: 0 }); g.setBlock('dirt', { x: 2, y: 64, z: 0 });
  const r = await g.run('collect', { block: 'dirt', count: 2 }); assert.equal(r.mined_blocks, 2); assert.equal(g.permits.length, 2);
});
test('real collect approaches matching loaded drops and reports observed inventory increase', async () => {
  const g = game(); g.setBlock('dirt', { x: 1, y: 64, z: 0 }); g.bot.registry.blocksByName.dirt.drops = [1];
  let count = 0; const original = g.bot.inventory.items;
  g.bot.inventory.items = () => original().map((i) => i.type === 1 ? { ...i, count: i.count + count } : i);
  const dig = g.bot.dig; g.bot.dig = async (block) => { await dig(block); g.bot.entities[1] = { type: 'other', name: 'item', position: new Vec3(1, 64, 0), getDroppedItem: () => ({ type: 1 }) }; };
  g.bot.pathfinder.goto = async () => { count++; delete g.bot.entities[1]; };
  const r = await g.run('collect', { block: 'dirt', count: 1 }); assert.equal(r.mined_blocks, 1); assert.equal(r.collected_items, 1);
});
test('real craft loops bounded recipe operations with permit for each', async () => {
  const g = game(); const r = await g.run('craft', { item: 'stick', count: 2 }); assert.equal(r.crafted_items, 8); assert.equal(g.effects.filter((e) => e[0] === 'craft').length, 2); assert.equal(g.permits.length, 2);
});
test('real inventory returns bounded names/counts without item metadata', async () => {
  const g = game(); const r = await g.run('inventory', {}); assert.equal(r.items.length, 7); assert.equal(g.permits[0].action, 'observe'); assert.deepEqual(Object.keys(r.items[0]), ['name', 'count']);
});
for (const operation of ['deposit', 'withdraw']) test(`real chest ${operation} opens transfers closes with permits`, async () => {
  const g = game(); g.setBlock('chest', { x: 1, y: 64, z: 0 }); await g.run('chest', { x: 1, y: 64, z: 0, operation, item: 'dirt', count: 4 });
  assert.deepEqual(g.effects.map((e) => e[0]), ['open', operation, 'close']); assert.equal(g.permits.length, 2);
});
test('real place guards equip and placement against loaded support', async () => {
  const g = game(); g.setBlock('dirt', { x: 1, y: 63, z: 0 }); await g.run('place', { x: 1, y: 64, z: 0, face_x: 0, face_y: 1, face_z: 0, item: 'dirt' });
  assert.deepEqual(g.effects.map((e) => e[0]), ['equip', 'place']); assert.equal(g.permits.length, 2);
  await assert.rejects(g.run('place', { x: 1, y: 64, z: 0, face_x: 0, face_y: 1, face_z: 0, item: 'tnt' }));
});
test('real farm tills and plants with permit at every mutation', async () => {
  const g = game(); g.setBlock('dirt', { x: 1, y: 63, z: 0 }); await g.run('farm', { x: 1, y: 63, z: 0, seed: 'wheat_seeds' });
  assert.deepEqual(g.effects.map((e) => e[0]), ['equip', 'activateBlock', 'equip', 'place']); assert.equal(g.permits.length, 4);
});
test('real farm cross-contract uses the soil block coordinate and rejects the player-feet coordinate', async () => {
  const g = game(); g.setBlock('dirt', { x: 1, y: 63, z: 0 });
  const soil = { x: 1, y: 63, z: 0, seed: 'wheat_seeds' }, feet = { x: 1, y: 64, z: 0, seed: 'wheat_seeds' };
  assert.equal(validateSkill('farm', soil), true); assert.equal(validateSkill('farm', feet), true);
  await g.run('farm', soil); assert.ok(g.effects.some((e) => e[0] === 'place'));
  await assert.rejects(g.adapter.run('farm', feet, new AbortController().signal, g.permit));
});
test('real breed rejects player and uses approved animal food', async () => {
  const g = game(); g.bot.entities[1] = { type: 'animal', name: 'cow', uuid: OTHER, position: new Vec3(1, 64, 0) };
  await g.run('breed', { target_uuid: OTHER, item: 'wheat' }); assert.deepEqual(g.effects.map((e) => e[0]), ['equip', 'breed']);
  g.bot.entities[1].type = 'player'; await assert.rejects(g.run('breed', { target_uuid: OTHER, item: 'wheat' }));
});
test('real attack known player requires target-specific PvP permit; hostile uses write', async () => {
  const g = game(); const e = { type: 'player', name: 'player', uuid: TARGET, position: new Vec3(1, 64, 0) }; g.bot.entities[1] = e;
  await assert.rejects(g.run('attack', { target_uuid: TARGET }));
  g.bot.players.Synthetic = { uuid: TARGET, entity: e }; await g.run('attack', { target_uuid: TARGET });
  assert.deepEqual(g.permits.at(-1), { action: 'pvp', target: TARGET });
  g.bot.entities = { 2: { type: 'hostile', name: 'zombie', uuid: OTHER, position: new Vec3(1, 64, 0) } };
  await g.run('attack', { target_uuid: OTHER }); assert.deepEqual(g.permits.at(-1), { action: 'write', target: '' });
});
test('real adapter and real guard controller deny player UUID outside local allowlist', async (t) => {
  const g = game(); const e = { type: 'player', uuid: OTHER, position: new Vec3(1, 64, 0) }; g.bot.entities[1] = e; g.bot.players.Synthetic = { uuid: OTHER, entity: e };
  const controller = new SidecarController({ config, guard: syntheticGuard(), adapter: g.adapter }); controller.connected = true; controller.port = 25565;
  t.after(() => controller.shutdown()); await assert.rejects(controller.skill({ name: 'attack', args: { target_uuid: OTHER }, timeout_sec: 2 }));
  assert.equal(g.effects.filter((e) => e[0] === 'attack').length, 0);
});
test('target entity changing from mob to player during permit cannot receive a write-permitted attack', async () => {
  const g = game(); const entity = { type: 'hostile', name: 'zombie', uuid: OTHER, position: new Vec3(1, 64, 0) }; g.bot.entities[1] = entity;
  await assert.rejects(g.adapter.run('attack', { target_uuid: OTHER }, new AbortController().signal, async () => { entity.type = 'player'; }));
  assert.equal(g.effects.filter((e) => e[0] === 'attack').length, 0); assert.ok(g.controls.some((e) => e[0] === 'clear_controls'));
});
test('follow losing target clears active path goal on failure', async () => {
  const g = game(); const entity = { type: 'player', uuid: TARGET, position: new Vec3(2, 64, 0) }; g.bot.entities[1] = entity;
  let calls = 0;
  await assert.rejects(g.adapter.run('follow', { target_uuid: TARGET, duration_sec: 1 }, new AbortController().signal, async () => { if (++calls === 2) delete g.bot.entities[1]; }));
  assert.ok(g.controls.some((e) => e[0] === 'goal' && e[1] === null));
});
test('real eat selects safe available food; home/set_home and respawn execute bounded actions', async () => {
  const g = game(); await g.run('eat', {}); assert.deepEqual(g.effects.slice(0, 2).map((e) => e[0]), ['equip', 'consume']);
  await g.run('set_home', {}); await g.run('home', {}); await g.run('respawn', {}); assert.ok(g.effects.some((e) => e[0] === 'goto')); assert.ok(g.effects.some((e) => e[0] === 'respawn'));
});
test('controller authorizes set_home with an observe permit and keeps write skills denied when write is off', async (t) => {
  const g = game(); const noWrite = readGuardConfig({ ...ENV, PAWBOT_MC_WRITE_ENABLED: 'false' }); const actions = [];
  const originalRun = g.adapter.run.bind(g.adapter);
  g.adapter.run = (name, args, signal, permit) => originalRun(name, args, signal, async (action = 'write', target = '') => { actions.push({ action, target }); return permit(action, target); });
  const controller = new SidecarController({ config: noWrite, guard: syntheticGuard(() => ({}), noWrite), adapter: g.adapter });
  controller.connected = true; controller.port = 25565; t.after(() => controller.shutdown());
  assert.deepEqual(await controller.skill({ name: 'set_home', args: {}, timeout_sec: 2 }), { ok: true });
  assert.deepEqual(actions, [{ action: 'observe', target: '' }]);
  assert.deepEqual(g.adapter.home, { x: 0, y: 64, z: 0, dimension: 'overworld' });
  await assert.rejects(controller.skill({ name: 'chat', args: { text: 'hi' }, timeout_sec: 2 }), (error) => error instanceof GuardError);
  assert.equal(g.effects.filter((e) => e[0] === 'chat').length, 0);
});
test('stop immediately stops pathfinder dig controls pvp and held item', () => {
  const g = game(); g.adapter.stop(); assert.deepEqual(g.controls.map((e) => e[0]), ['pvp_stop', 'goal', 'stop_dig', 'deactivate', 'clear_controls']); assert.equal(g.adapter.observe().health, 13);
});
test('permit failure after equip prevents late placement', async () => {
  const g = game(); g.setBlock('dirt', { x: 1, y: 63, z: 0 }); let calls = 0;
  await assert.rejects(g.adapter.run('place', { x: 1, y: 64, z: 0, face_x: 0, face_y: 1, face_z: 0, item: 'dirt' }, new AbortController().signal, async () => { if (++calls === 2) throw new Error('revoked'); }));
  assert.equal(g.effects.filter((e) => e[0] === 'place').length, 0);
});
test('minimum action spacing admits no faster than eight effects per second', async () => {
  const g = game(); const timestamps = []; g.bot.chat = () => timestamps.push(Date.now());
  await g.run('chat', { text: 'one' }); await g.run('chat', { text: 'two' }); await g.run('chat', { text: 'three' });
  assert.ok(timestamps[1] - timestamps[0] >= 120); assert.ok(timestamps[2] - timestamps[1] >= 120);
});
function connectionGame({ flow } = {}) {
  const g = game(); const order = [];
  const client = new EventEmitter(); client.setSocket = () => { order.push('socket'); queueMicrotask(() => g.bot.emit('spawn')); };
  client.socket = { destroy: () => order.push('destroy') }; g.bot._client = client;
  const adapter = new MineflayerAdapter({ Vec3, Movements, goals: { GoalNear }, pathfinder: () => {}, pvp: () => {},
    createAuthflow: async () => flow,
    openSocket: (options) => { assert.deepEqual(options, { host: '127.0.0.1', port: 25565 }); order.push('open'); return {}; },
    createBot: (options) => { queueMicrotask(() => { if (typeof options.auth === 'function') options.auth(client, options); else options.connect(client); }); return g.bot; },
  });
  client.on('error', (error) => g.bot.emit('error', error));
  return { ...g, adapter, order };
}
test('real offline adapter rechecks guard immediately before opening literal loopback socket', async () => {
  const g = connectionGame();
  const identity = await g.adapter.connect(CONNECT, new AbortController().signal, async () => { g.order.push('permit'); });
  assert.equal(identity.uuid, ACTOR); assert.deepEqual(g.order.slice(0, 3), ['permit', 'open', 'socket']);
  assert.equal(g.adapter.movements.canDig, false); assert.equal(g.adapter.movements.allow1by1towers, false); assert.deepEqual(g.adapter.movements.scafoldingBlocks, []);
  await g.adapter.close();
});
test('real Microsoft adapter checks guard after cached token and before socket, with no fallback', async () => {
  let release;
  const flow = { getMinecraftJavaToken: () => new Promise((resolve) => { release = resolve; }) };
  const g = connectionGame({ flow });
  const pending = g.adapter.connect({ ...CONNECT, auth_mode: 'microsoft', auth_dir: 'synthetic' }, new AbortController().signal, async () => { g.order.push('permit'); });
  await new Promise((r) => setImmediate(r)); assert.deepEqual(g.order, []);
  release({ token: 'synthetic-only', profile: { id: ACTOR.replaceAll('-', ''), name: 'SyntheticBot' } });
  const identity = await pending; assert.equal(identity.uuid, ACTOR); assert.deepEqual(g.order.slice(0, 3), ['permit', 'open', 'socket']); await g.adapter.close();
});
test('real Microsoft adapter missing cache returns setup error without socket or fallback', async () => {
  const g = connectionGame({ flow: { getMinecraftJavaToken: async () => { throw new AuthRequiredError(); } } });
  await assert.rejects(g.adapter.connect({ ...CONNECT, auth_mode: 'microsoft', auth_dir: 'synthetic' }, new AbortController().signal, async () => {}), AuthRequiredError);
  assert.deepEqual(g.order, []); await g.adapter.close();
});
test('real adapter denied pre-socket guard opens no socket', async () => {
  const g = connectionGame(); await assert.rejects(g.adapter.connect(CONNECT, new AbortController().signal, async () => { throw new GuardError(); }), GuardError);
  assert.deepEqual(g.order, []); await g.adapter.close();
});
test('real adapter cancellation during cached authentication cannot open a late socket', async () => {
  let release; const flow = { getMinecraftJavaToken: () => new Promise((resolve) => { release = resolve; }) };
  const g = connectionGame({ flow }); const ac = new AbortController();
  const pending = g.adapter.connect({ ...CONNECT, auth_mode: 'microsoft', auth_dir: 'synthetic' }, ac.signal, async () => {}); const failure = assert.rejects(pending);
  await new Promise((r) => setImmediate(r)); ac.abort(); await failure;
  release({ token: 'synthetic', profile: { id: ACTOR.replaceAll('-', ''), name: 'SyntheticBot' } });
  await new Promise((r) => setImmediate(r)); assert.equal(g.order.includes('open'), false);
});
test('dependency imports and actual registry include Minecraft1.20.1 and pinned plugin APIs', async () => {
  const [mf, pf, pv, data, auth, uuid] = await Promise.all([import('mineflayer'), import('mineflayer-pathfinder'), import('mineflayer-pvp'), import('minecraft-data'), import('prismarine-auth'), import('uuid')]);
  assert.equal(typeof mf.createBot, 'function'); assert.equal(typeof pf.pathfinder, 'function'); assert.equal(typeof pv.plugin, 'function'); assert.equal(data.default('1.20.1').version.minecraftVersion, '1.20.1'); assert.equal(data.default('1.20.1').entitiesByName.zombie.type, 'hostile'); assert.equal(data.default('1.20.1').entitiesByName.cow.type, 'animal'); assert.equal(typeof auth.Authflow, 'function'); assert.match(uuid.v4(), /^[0-9a-f-]{36}$/);
});
