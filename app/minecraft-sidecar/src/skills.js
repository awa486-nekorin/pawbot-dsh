import { UUID } from './guard.js';
const id = (v) => typeof v === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(v);
const int = (v, a, b) => Number.isInteger(v) && v >= a && v <= b;
const keys = (o, expected) => Object.keys(o).length === expected.length && expected.every((k) => Object.hasOwn(o, k));
const xyz = ['x', 'y', 'z'];
const coordinates = (o, integer = true) => xyz.every((k) => Number.isFinite(o[k]) && (!integer || Number.isInteger(o[k])) && (k === 'y' ? o[k] >= -64 && o[k] <= 319 : Math.abs(o[k]) <= 29999984));
const uuid = (v) => typeof v === 'string' && UUID.test(v);
export const SKILLS = Object.freeze(['move', 'follow', 'chat', 'dig', 'collect', 'craft', 'inventory', 'chest', 'place', 'farm', 'breed', 'attack', 'eat', 'explore', 'home', 'set_home', 'respawn']);
export function validateSkill(name, a) {
  if (!a || typeof a !== 'object' || Array.isArray(a)) return false;
  switch (name) {
    case 'move': return keys(a, [...xyz, 'radius']) && coordinates(a, false) && Number.isFinite(a.radius) && a.radius >= 0.5 && a.radius <= 4;
    case 'follow': return keys(a, ['target_uuid', 'duration_sec']) && uuid(a.target_uuid) && int(a.duration_sec, 1, 30);
    case 'chat': return keys(a, ['text']) && typeof a.text === 'string' && a.text.length >= 1 && a.text.length <= 200 && !a.text.trimStart().startsWith('/') && !/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(a.text);
    case 'dig': case 'explore': return keys(a, xyz) && coordinates(a);
    case 'collect': return keys(a, ['block', 'count']) && id(a.block) && int(a.count, 1, 16);
    case 'craft': return keys(a, ['item', 'count']) && id(a.item) && int(a.count, 1, 16);
    case 'inventory': case 'eat': case 'home': case 'set_home': case 'respawn': return keys(a, []);
    case 'chest': return keys(a, [...xyz, 'operation', 'item', 'count']) && coordinates(a) && ['deposit', 'withdraw'].includes(a.operation) && id(a.item) && int(a.count, 1, 64);
    case 'place': return keys(a, [...xyz, 'face_x', 'face_y', 'face_z', 'item']) && coordinates(a) && ['face_x', 'face_y', 'face_z'].every((k) => int(a[k], -1, 1)) && Math.abs(a.face_x) + Math.abs(a.face_y) + Math.abs(a.face_z) === 1 && id(a.item);
    case 'farm': return keys(a, [...xyz, 'seed']) && coordinates(a) && ['wheat_seeds', 'beetroot_seeds', 'carrot', 'potato'].includes(a.seed);
    case 'breed': return keys(a, ['target_uuid', 'item']) && uuid(a.target_uuid) && id(a.item);
    case 'attack': return keys(a, ['target_uuid']) && uuid(a.target_uuid);
    default: return false;
  }
}
