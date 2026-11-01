import net from 'node:net';
import { createAuthflow, AuthRequiredError } from './auth.js';
import { GuardError } from './guard.js';

const SAFE_NAME = /^[a-z][a-z0-9_:-]{0,63}$/;
const safeName = (v) => typeof v === 'string' && SAFE_NAME.test(v) ? v : 'unknown';
const position = (p) => p && [p.x, p.y, p.z].every(Number.isFinite) ? { x: p.x, y: p.y, z: p.z } : null;
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
const boundedHealth = (v) => Math.round(Math.max(0, Math.min(2048, v)) * 100) / 100;
const boundedLight = (block) => {
  const levels = [block?.light, block?.skyLight].filter((v) => Number.isInteger(v) && v >= 0 && v <= 15);
  return levels.length ? Math.max(...levels) : null;
};
const weatherOf = (bot) => {
  if (typeof bot.isRaining !== 'boolean') return null;
  if (!bot.isRaining) return 'clear';
  return Number.isFinite(bot.thunderState) && bot.thunderState > 0 ? 'thunder' : 'rain';
};
const SAFETY_EVENT_LIMIT = 16;
const hostile = new Set(['zombie', 'skeleton', 'creeper', 'spider', 'cave_spider', 'drowned', 'husk', 'stray', 'pillager', 'vindicator', 'witch', 'endermite', 'silverfish', 'slime', 'magma_cube', 'blaze', 'wither_skeleton', 'phantom', 'zoglin', 'hoglin']);
const animalFood = { cow: ['wheat'], sheep: ['wheat'], pig: ['carrot', 'potato', 'beetroot'], chicken: ['wheat_seeds', 'beetroot_seeds', 'melon_seeds', 'pumpkin_seeds'], rabbit: ['carrot', 'golden_carrot', 'dandelion'] };
const inertPlace = (name) => !/(?:tnt|fire|lava|water_bucket|flint_and_steel|spawn_egg|command|structure|jigsaw|bucket|bed|anchor|crystal|minecart|boat|rail|piston|redstone)/.test(name);
export class GameError extends Error { constructor() { super('Game operation unavailable'); } }
function check(signal) { if (signal.aborted) throw new GameError(); }
const wait = (ms, signal) => new Promise((resolve, reject) => {
  check(signal);
  const stop = () => { clearTimeout(t); signal.removeEventListener('abort', stop); reject(new GameError()); };
  const t = setTimeout(() => { signal.removeEventListener('abort', stop); resolve(); }, ms);
  signal.addEventListener('abort', stop, { once: true });
});
export class MineflayerAdapter {
  constructor(dependencies = {}) { Object.assign(this, dependencies); this.bot = null; this.connected = false; this.stopped = false; this.home = null; this.lastEffect = 0; this.events = []; this.lastHealth = null; this.safetyBot = null; }
  onDisconnect(callback) { this.disconnected = callback; }
  identity() { return this.connected ? (this.bot.player?.uuid || this.bot._client?.uuid || '').toLowerCase() : ''; }
  async connect(params, signal, beforeSocket) {
    check(signal); this.stopped = false;
    if (!this.createBot) {
      const [mf, pf, pv, vec] = await Promise.all([import('mineflayer'), import('mineflayer-pathfinder'), import('mineflayer-pvp'), import('vec3')]);
      Object.assign(this, { createBot: mf.createBot, pathfinder: pf.pathfinder, pvp: pv.plugin, Movements: pf.Movements, goals: pf.goals, Vec3: vec.Vec3 });
    }
    check(signal);
    let flow;
    if (params.auth_mode === 'microsoft') flow = await (this.createAuthflow || createAuthflow)(params.username, params.auth_dir);
    check(signal);
    const microsoftAuth = (client, options) => {
      void flow.getMinecraftJavaToken({ fetchProfile: true, fetchCertificates: true }).then(({ token, profile, certificates }) => {
        check(signal); if (this.stopped || !token || !profile?.id || !profile?.name) throw new GameError();
        const session = { accessToken: token, selectedProfile: profile, availableProfile: [profile] };
        Object.assign(client, certificates); client.session = session; client.username = profile.name;
        options.haveCredentials = true; options.accessToken = token;
        client.emit('session', session); options.connect(client);
      }).catch((error) => client.emit('error', error));
    };
    if (typeof beforeSocket !== 'function') throw new GameError();
    const socketConnect = (client) => {
      void beforeSocket().then(() => {
        check(signal); if (this.stopped) throw new GameError();
        client.setSocket((this.openSocket || net.connect)({ host: '127.0.0.1', port: params.port }));
      }).catch((error) => client.emit('error', error));
    };
    const bot = this.createBot({ host: '127.0.0.1', port: params.port, version: '1.20.1', username: params.username,
      auth: params.auth_mode === 'microsoft' ? microsoftAuth : 'offline', profilesFolder: params.auth_dir, connect: socketConnect,
      onMsaCode: () => { throw new GameError(); }, hideErrors: true, logErrors: false, respawn: false,
      checkTimeoutInterval: 10000, viewDistance: 'tiny' });
    this.bot = bot;
    bot.loadPlugin(this.pathfinder); bot.loadPlugin(this.pvp);
    this.events = []; this.lastHealth = null; this.bindSafety(bot);
    bot.on('error', () => { if (this.connected) this.disconnected?.(); });
    bot.on('end', () => { this.connected = false; this.disconnected?.(); });
    bot.on('resourcePack', () => bot.denyResourcePack());
    await new Promise((resolve, reject) => {
      const cleanup = () => { signal.removeEventListener('abort', abort); bot.removeListener('spawn', spawn); bot.removeListener('error', fail); bot.removeListener('end', fail); };
      const spawn = () => { cleanup(); resolve(); };
      const fail = (error) => { cleanup(); reject(error instanceof AuthRequiredError || error instanceof GuardError ? error : new GameError()); };
      const abort = () => { this.stop(); void this.close(); fail(); };
      bot.once('spawn', spawn); bot.once('error', fail); bot.once('end', fail); signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) abort();
    });
    check(signal); if (this.stopped) throw new GameError();
    this.connected = true;
    this.movements = new this.Movements(bot);
    Object.assign(this.movements, { canDig: false, allow1by1towers: false, allowParkour: false, allowSprinting: false, canOpenDoors: false, allowFreeMotion: false, allowEntityDetection: true, maxDropDown: 1 });
    this.movements.scafoldingBlocks = [];
    bot.pathfinder.thinkTimeout = 1000; bot.pathfinder.tickTimeout = 20; bot.pathfinder.searchRadius = 20;
    bot.pathfinder.setMovements(this.movements);
    return { uuid: this.identity() };
  }
  stop() {
    const b = this.bot; if (!b) return;
    for (const action of [() => b.pvp?.forceStop(), () => b.pathfinder?.setGoal(null), () => b.stopDigging?.(), () => b.deactivateItem?.(), () => b.clearControlStates?.()]) { try { action(); } catch {} }
  }
  async close() {
    const b = this.bot; this.stop(); this.stopped = true; this.connected = false; this.bot = null;
    this.events = []; this.lastHealth = null; this.safetyBot = null;
    if (b) { try { b.quit('Sidecar stopped'); } catch {} try { b._client?.socket?.destroy(); } catch {} }
  }
  requireBot() { if (!this.bot || !this.connected || this.stopped) throw new GameError(); return this.bot; }
  // Safe observation events: fixed names plus bounded/clamped numbers only. Chat, display names,
  // UUIDs, tokens and free text are never recorded or emitted.
  bindSafety(bot) {
    if (!bot || this.safetyBot === bot) return;
    this.safetyBot = bot;
    bot.on('health', () => { if (this.bot === bot) this.noteHealth(); });
    bot.on('death', () => { if (this.bot !== bot) return; this.lastHealth = 0; this.recordEvent({ type: 'death' }); });
    bot.on('respawn', () => { if (this.bot !== bot) return; this.lastHealth = null; this.recordEvent({ type: 'respawn' }); });
  }
  recordEvent(event) {
    if (this.events.length >= SAFETY_EVENT_LIMIT) this.events.shift();
    this.events.push(event);
  }
  noteHealth() {
    const health = Number.isFinite(this.bot?.health) ? boundedHealth(this.bot.health) : null;
    if (health === null) return;
    if (this.lastHealth !== null && health < this.lastHealth) this.recordEvent({ type: 'health_loss', from: this.lastHealth, to: health });
    this.lastHealth = health;
  }
  // Bounded scan of loaded blocks directly above the player: null when any block is unloaded, so an
  // unknown roof is omitted instead of guessed.
  roofAt(p) {
    const x = Math.floor(p.x), z = Math.floor(p.z), base = Math.floor(p.y);
    for (let dy = 2; dy <= 6; dy++) {
      const block = this.bot.blockAt(new this.Vec3(x, base + dy, z), false);
      if (!block) return null;
      if (block.boundingBox === 'block') return true;
    }
    return false;
  }
  nearby(p, max) { const b = this.requireBot(); if (!position(p) || !position(b.entity?.position) || distance(p, b.entity.position) > max) throw new GameError(); }
  loadedBlock(p, max = 4.5) { this.nearby(p, max); const b = this.bot.blockAt(new this.Vec3(p.x, p.y, p.z), false); if (!b) throw new GameError(); return b; }
  entities() {
    const out = []; let n = 0;
    for (const key in this.requireBot().entities) { if (++n > 1024) break; const e = this.bot.entities[key]; if (e && position(e.position) && distance(e.position, this.bot.entity.position) <= 16) out.push(e); }
    return out;
  }
  entity(uuid, max = 4.5) {
    const e = this.entities().find((v) => typeof v.uuid === 'string' && v.uuid.toLowerCase() === uuid);
    if (!e || e.isValid === false) throw new GameError(); this.nearby(e.position, max); return e;
  }
  items() { return this.requireBot().inventory.items().slice(0, 64); }
  item(name) { const item = this.items().find((i) => i.name === name); if (!item) throw new GameError(); return item; }
  async effect(signal, permit, fn, action = 'write', target = '') {
    check(signal); this.requireBot();
    while (Date.now() - this.lastEffect < 125) await wait(125 - (Date.now() - this.lastEffect), signal);
    await permit(action, target); check(signal); this.requireBot(); this.lastEffect = Date.now();
    return fn();
  }
  async goto(p, radius, signal, permit) {
    this.nearby(p, 16); this.loadedBlock(p, 16);
    const bot = this.requireBot();
    await this.effect(signal, permit, () => bot.pathfinder.goto(new this.goals.GoalNear(p.x, p.y, p.z, radius)));
    check(signal); this.requireBot();
  }
  async run(name, a, signal, permit) {
    const b = this.requireBot(); check(signal);
    try {
      switch (name) {
        case 'inventory': await permit('observe'); return { items: this.items().map((i) => ({ name: safeName(i.name), count: Math.min(64, Math.max(0, i.count)) })) };
        case 'move': await this.goto(a, a.radius, signal, permit); return { ok: true };
        case 'explore': await this.goto(a, 2, signal, permit); return { ok: true };
        case 'follow': {
          const target = this.entity(a.target_uuid, 16); if (target.type !== 'player') throw new GameError();
          const origin = { ...b.entity.position };
          const end = Date.now() + a.duration_sec * 1000;
          while (Date.now() < end) {
            this.nearby(origin, 16);
            const fresh = this.entity(a.target_uuid, 16);
            if (fresh !== target || fresh.type !== 'player' || distance(fresh.position, origin) > 16) throw new GameError();
            // Repeated goals stay in the skill's original loaded local area.
            await this.effect(signal, permit, () => {
              const current = this.entity(a.target_uuid, 16);
              if (current !== target || current.type !== 'player' || distance(current.position, origin) > 16) throw new GameError();
              return b.pathfinder.setGoal(new this.goals.GoalNear(current.position.x, current.position.y, current.position.z, 2));
            });
            await wait(250, signal);
          }
          b.pathfinder.setGoal(null); return { ok: true };
        }
        case 'chat': await this.effect(signal, permit, () => b.chat(a.text)); return { ok: true };
        case 'dig': {
          const block = this.loadedBlock(a); if (!b.canDigBlock(block) || ['bedrock', 'air', 'cave_air', 'void_air'].includes(block.name)) throw new GameError();
          await this.effect(signal, permit, () => { const current = this.loadedBlock(a); if (current.name !== block.name || !b.canDigBlock(current)) throw new GameError(); return b.dig(current); }); return { ok: true };
        }
        case 'collect': {
          let mined = 0; const start = { ...b.entity.position };
          const before = new Map(); for (const i of this.items()) before.set(i.type, (before.get(i.type) || 0) + i.count);
          const dropTypes = new Set();
          for (let dx = -3; dx <= 3 && mined < a.count; dx++) for (let dy = -2; dy <= 2 && mined < a.count; dy++) for (let dz = -3; dz <= 3 && mined < a.count; dz++) {
            check(signal); this.nearby(start, 4);
            const p = { x: Math.floor(start.x) + dx, y: Math.floor(start.y) + dy, z: Math.floor(start.z) + dz };
            if (distance(p, b.entity.position) > 4.5) continue;
            const block = b.blockAt(new this.Vec3(p.x, p.y, p.z), false);
            if (!block || block.name !== a.block || !b.canDigBlock(block)) continue;
            await this.effect(signal, permit, () => { const current = this.loadedBlock(p); if (current.name !== a.block || !b.canDigBlock(current)) throw new GameError(); return b.dig(current); }); mined++;
          }
          // Only loaded nearby item entities whose type matches this block's declared drops.
          const definition = b.registry.blocksByName[a.block];
          for (const type of (definition?.drops || []).slice(0, 16)) if (Number.isInteger(type)) dropTypes.add(type);
          let pickups = 0;
          for (const entity of this.entities()) {
            if (pickups >= 16 || !['item', 'item_stack'].includes(entity.name) || distance(entity.position, start) > 5 || typeof entity.getDroppedItem !== 'function') continue;
            const item = entity.getDroppedItem(); if (!item || !dropTypes.has(item.type)) continue;
            const p = position(entity.position); if (!p) continue;
            await this.goto(p, 0.5, signal, permit); pickups++;
            await wait(100, signal);
          }
          const after = new Map(); for (const i of this.items()) after.set(i.type, (after.get(i.type) || 0) + i.count);
          const received = [...dropTypes].reduce((sum, type) => sum + Math.max(0, (after.get(type) || 0) - (before.get(type) || 0)), 0);
          return { mined_blocks: mined, collected_items: received };
        }
        case 'craft': {
          const item = b.registry.itemsByName[a.item]; if (!item) throw new GameError();
          const table = b.findBlock({ matching: b.registry.blocksByName.crafting_table?.id ?? -1, maxDistance: 4, count: 1 });
          const recipes = b.recipesFor(item.id, null, 1, table || null).slice(0, 16);
          const recipe = recipes.find((r) => !r.requiresTable || table); if (!recipe) throw new GameError();
          let made = 0;
          for (let i = 0; i < a.count; i++) { await this.effect(signal, permit, () => b.craft(recipe, 1, table || null)); made += recipe.result?.count || 1; }
          return { crafted_items: made, operations: a.count };
        }
        case 'chest': {
          const block = this.loadedBlock(a); if (!['chest', 'trapped_chest', 'barrel'].includes(block.name)) throw new GameError();
          const item = b.registry.itemsByName[a.item]; if (!item) throw new GameError();
          const container = await this.effect(signal, permit, () => { const current = this.loadedBlock(a); if (current.name !== block.name) throw new GameError(); return b.openContainer(current); });
          try { await this.effect(signal, permit, () => a.operation === 'deposit' ? container.deposit(item.id, null, a.count) : container.withdraw(item.id, null, a.count)); return { transferred: a.count }; }
          finally { container.close(); }
        }
        case 'place': {
          if (!inertPlace(a.item) || !b.registry.blocksByName[a.item]) throw new GameError();
          const destination = this.loadedBlock(a); if (!['air', 'cave_air'].includes(destination.name)) throw new GameError();
          const support = this.loadedBlock({ x: a.x - a.face_x, y: a.y - a.face_y, z: a.z - a.face_z });
          if (support.boundingBox !== 'block') throw new GameError();
          await this.effect(signal, permit, () => b.equip(this.item(a.item), 'hand'));
          await this.effect(signal, permit, () => {
            const target = this.loadedBlock(a), current = this.loadedBlock(support.position);
            if (!['air', 'cave_air'].includes(target.name) || current.name !== support.name || current.boundingBox !== 'block') throw new GameError();
            return b.placeBlock(current, new this.Vec3(a.face_x, a.face_y, a.face_z));
          }); return { ok: true };
        }
        case 'farm': {
          let soil = this.loadedBlock(a); let crop = this.loadedBlock({ x: a.x, y: a.y + 1, z: a.z });
          const crops = { wheat_seeds: 'wheat', beetroot_seeds: 'beetroots', carrot: 'carrots', potato: 'potatoes' };
          if (!['farmland', 'dirt', 'grass_block'].includes(soil.name)) throw new GameError();
          if (crop.name !== 'air' && crop.name !== 'cave_air') {
            if (crop.name !== crops[a.seed]) throw new GameError();
            const age = crop.getProperties?.().age; if (age !== (a.seed === 'beetroot_seeds' ? 3 : 7)) throw new GameError();
            await this.effect(signal, permit, () => b.dig(crop));
          }
          if (soil.name !== 'farmland') {
            const hoe = this.items().find((i) => i.name.endsWith('_hoe')); if (!hoe) throw new GameError();
            await this.effect(signal, permit, () => b.equip(hoe, 'hand'));
            await this.effect(signal, permit, () => b.activateBlock(soil));
            soil = this.loadedBlock(a); if (soil.name !== 'farmland') throw new GameError();
          }
          await this.effect(signal, permit, () => b.equip(this.item(a.seed), 'hand'));
          await this.effect(signal, permit, () => b.placeBlock(soil, new this.Vec3(0, 1, 0))); return { planted: a.seed };
        }
        case 'breed': {
          const entity = this.entity(a.target_uuid); if (entity.type === 'player' || entity.type !== b.registry.entitiesByName[entity.name]?.type || !animalFood[entity.name]?.includes(a.item)) throw new GameError();
          await this.effect(signal, permit, () => b.equip(this.item(a.item), 'hand'));
          await this.effect(signal, permit, () => {
            const current = this.entity(a.target_uuid);
            if (current !== entity || current.type === 'player' || current.type !== b.registry.entitiesByName[current.name]?.type || !animalFood[current.name]?.includes(a.item)) throw new GameError();
            return b.activateEntity(current);
          }); return { ok: true };
        }
        case 'attack': {
          const entity = this.entity(a.target_uuid, 3); let playerRecord, checked = 0, playerUuidSeen = false;
          for (const name in b.players) { if (++checked > 128) break; const p = b.players[name]; if (p.uuid?.toLowerCase() === a.target_uuid) { playerUuidSeen = true; if (p.entity === entity) playerRecord = p; break; } }
          let action;
          if (entity.type === 'player') { if (!playerRecord) throw new GameError(); action = 'pvp'; }
          else if (checked <= 128 && hostile.has(entity.name) && entity.type !== 'player' && entity.type === b.registry.entitiesByName[entity.name]?.type && !playerUuidSeen) action = 'write';
          else throw new GameError();
          b.pvp.forceStop();
          await this.effect(signal, permit, () => {
            const current = this.entity(a.target_uuid, 3);
            if (current !== entity || (action === 'pvp' ? current.type !== 'player' || playerRecord.entity !== current || playerRecord.uuid?.toLowerCase() !== a.target_uuid : current.type === 'player' || current.type !== b.registry.entitiesByName[current.name]?.type || !hostile.has(current.name))) throw new GameError();
            return b.attack(current);
          }, action, action === 'pvp' ? a.target_uuid : '');
          return { attacks: 1 };
        }
        case 'eat': {
          const foods = b.registry.foodsByName || {};
          const food = this.items().find((i) => foods[i.name] && !['rotten_flesh', 'spider_eye', 'poisonous_potato', 'pufferfish', 'chorus_fruit', 'suspicious_stew'].includes(i.name));
          if (!food) throw new GameError();
          await this.effect(signal, permit, () => b.equip(food, 'hand'));
          await this.effect(signal, permit, () => b.consume()); return { ok: true };
        }
        case 'home': if (!this.home || this.home.dimension !== b.game.dimension) throw new GameError(); await this.goto(this.home, 1, signal, permit); return { ok: true };
        case 'set_home': await permit('observe'); this.home = { ...position(b.entity.position), dimension: b.game.dimension }; return { ok: true };
        case 'respawn': await this.effect(signal, permit, () => b.respawn()); return { ok: true };
        default: throw new GameError();
      }
    } finally { this.stop(); }
  }
  observe() {
    const b = this.requireBot(); this.bindSafety(b); const p = position(b.entity.position);
    this.noteHealth();
    const blocks = [];
    if (p) for (let x = -2; x <= 2 && blocks.length < 32; x++) for (let y = -1; y <= 1 && blocks.length < 32; y++) for (let z = -2; z <= 2 && blocks.length < 32; z++) {
      const block = b.blockAt(new this.Vec3(Math.floor(p.x) + x, Math.floor(p.y) + y, Math.floor(p.z) + z), false);
      if (block && block.name !== 'air') blocks.push({ name: safeName(block.name), position: position(block.position) });
    }
    // Real game facts only; unknown values are omitted rather than invented.
    const timeOfDay = b.time?.timeOfDay;
    const weather = weatherOf(b);
    const feet = p ? b.blockAt(new this.Vec3(Math.floor(p.x), Math.floor(p.y), Math.floor(p.z)), false) : null;
    const light = feet ? boundedLight(feet) : null;
    const roof = p ? this.roofAt(p) : null;
    const events = this.events.splice(0, SAFETY_EVENT_LIMIT);
    const observation = {
      health: Number.isFinite(b.health) ? Math.max(0, Math.min(2048, b.health)) : 0,
      food: Number.isFinite(b.food) ? Math.max(0, Math.min(20, b.food)) : 0,
      position: p,
      dimension: safeName(b.game.dimension),
    };
    if (Number.isInteger(timeOfDay) && timeOfDay >= 0 && timeOfDay <= 24000) observation.time_of_day = timeOfDay;
    if (weather) observation.weather = weather;
    if (light !== null) observation.light = light;
    if (roof !== null) observation.roof = roof;
    observation.inventory = this.items().map((i) => ({ name: safeName(i.name), count: Math.min(64, Math.max(0, i.count)) }));
    observation.nearby_entities = this.entities().slice(0, 32).map((e) => ({ type: safeName(e.type), name: safeName(e.name), uuid: /^[0-9a-f-]{36}$/.test(e.uuid || '') ? e.uuid : '', position: position(e.position) }));
    observation.nearby_blocks = blocks;
    observation.events = events;
    return observation;
  }
}
