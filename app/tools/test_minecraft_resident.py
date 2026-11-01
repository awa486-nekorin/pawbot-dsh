"""Offline tests for the autonomous Minecraft resident.

Everything here uses temporary directories, deterministic clocks and in-process
fakes for the guard/sidecar.  No game, world, credential, browser or network is
touched.  The tests assert real behavior: persisted state, guard ordering,
budget reservation, intent-only skills, failure handling and a synthetic
accelerated 60-minute acceptance run.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.config import DEFAULT  # noqa: E402
from pawbot.emotion import Emotion  # noqa: E402
from pawbot.minecraft_guard import GuardBusy, GuardDenied  # noqa: E402
from pawbot.minecraft_policy import WorldPermit, owner_human_identity  # noqa: E402
from pawbot.minecraft_sidecar import SidecarError  # noqa: E402
from pawbot import minecraft_resident as mr  # noqa: E402


ACTOR = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OWNER_HUMAN = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
WORLD = "test-world-01"
OWNER_UID = "123456789"
LAN_PORT = 25566


class Config(dict):
    """Dotted config getter compatible with the resident/decider callers."""

    def get(self, dotted, default=None):
        if not isinstance(dotted, str) or "." not in dotted:
            return super().get(dotted, default)
        node = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class Clock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakePaths:
    def __init__(self, root):
        self.root = Path(root)

    def minecraft_content(self, name=""):
        path = self.root / "content" / "minecraft"
        return str(path / name if name else path)

    def minecraft_state(self, name=""):
        path = self.root / "state" / "minecraft"
        return str(path / name if name else path)


class FakePersona:
    def core(self):
        return "测试用共享人设：慢慢生活，不假装真人。"


class FakeLongMemory:
    def __init__(self):
        self.episodes = []

    async def add_episode(self, bot, **kwargs):
        self.episodes.append(kwargs)


class FakeSettings:
    def __init__(self, actor, world, port, world_path):
        self.actor_uuid = actor
        self.world_id = world
        self.port = port
        self.world_path = world_path

    def validate(self):
        return None


class FakeGuard:
    def __init__(self, clock, *, actor=ACTOR, world=WORLD, port=LAN_PORT):
        self.clock = clock
        self.settings = FakeSettings(actor, world, port,
                                     os.path.join(tempfile.gettempdir(), "synthetic-test-world"))
        self.actions = []
        self.rows = []
        self.deny = set()
        self.unavailable = False
        self.busy = set()
        self.busy_left = {}
        self.busy_observe = False

    async def request(self, action, target_uuid=""):
        self.actions.append((action, target_uuid))
        if self.busy_left.get(action, 0) > 0:
            self.busy_left[action] -= 1
            raise GuardBusy(50)
        if self.unavailable or action in self.deny:
            raise GuardDenied("guard_unavailable" if self.unavailable else "guard_denied")
        if action in self.busy:
            raise GuardBusy(50)
        if action == "observe" and self.busy_observe:
            now = self.clock()
            row = {
                "nonce": "0" * 32, "world_id": self.settings.world_id,
                "save_path": self.settings.world_path, "host": "127.0.0.1",
                "port": self.settings.port, "actor_uuid": self.settings.actor_uuid,
                "action": action, "target_uuid": target_uuid,
                "issued_at": now, "expires_at": now + 5,
                "can_write": False, "can_pvp": False, "backup_id": "",
                "busy": True, "retry_after_ms": 50, "attested_at": int(now),
            }
            self.rows.append(row)
            return dict(row)
        now = self.clock()
        row = {
            "nonce": "0" * 32, "world_id": self.settings.world_id,
            "save_path": self.settings.world_path, "host": "127.0.0.1",
            "port": self.settings.port, "actor_uuid": self.settings.actor_uuid,
            "action": action, "target_uuid": target_uuid,
            "issued_at": now, "expires_at": now + 5,
            "can_write": True, "can_pvp": True, "backup_id": "backup-1",
        }
        self.rows.append(row)
        return dict(row)

    @staticmethod
    def policy_permit(row):
        return WorldPermit(row["world_id"], True, row["issued_at"],
                           can_write=row["can_write"], can_pvp=row["can_pvp"])


class FakeSidecar:
    def __init__(self, clock, observation):
        self.clock = clock
        self.observation = observation
        self.alive = False
        self.starts = 0
        self.stops = []
        self.calls = []
        self.skills = []
        self.connect_params = {}
        self.connect_prepares = 0
        self.fail_connects = 0
        self.observe_error = None
        self.skill_error = None
        self.start_error = None
        self.omit_backup_completed = False

    async def start(self):
        self.starts += 1
        if self.start_error:
            raise self.start_error
        self.alive = True

    async def stop(self, graceful=True):
        self.stops.append(bool(graceful))
        self.alive = False

    async def connect_with_retry(self, params, prepare, *, attempts=1, timeout=35):
        self.connect_params = dict(params)
        self.connect_prepares += 1
        await prepare()
        if self.fail_connects > 0:
            self.fail_connects -= 1
            self.alive = False
            raise SidecarError("sidecar_disconnected")
        self.alive = True
        result = {"connected": True, "uuid": params.get("_actor", ACTOR),
                  "world_id": WORLD}
        if not self.omit_backup_completed:
            result["backup_completed"] = True
        return result

    async def request(self, method, params=None, timeout=35):
        self.calls.append((method, params, timeout))
        if method == "observe":
            if self.observe_error is not None:
                raise self.observe_error
            return copy.deepcopy(self.observation)
        if method == "skill":
            if self.skill_error is not None:
                raise self.skill_error
            self.skills.append(dict(params or {}))
            return {"ok": True}
        if method == "connect":
            self.alive = True
            return {"connected": True, "uuid": ACTOR, "world_id": WORLD,
                    "backup_completed": True}
        return {}


_SOIL_BLOCKS = frozenset({"farmland", "dirt", "grass_block"})
_CROPS = {"wheat_seeds": "wheat", "beetroot_seeds": "beetroots",
          "carrot": "carrots", "potato": "potatoes"}
_HOSTILES = frozenset({"zombie", "skeleton", "creeper", "spider"})
_INERT_DENY = re.compile(
    r"(?:tnt|fire|lava|water_bucket|flint_and_steel|spawn_egg|command|structure|"
    r"jigsaw|bucket|bed|anchor|crystal|minecart|boat|rail|piston|redstone)")


class ContractSidecar:
    """Contract-faithful stand-in for the real Node sidecar.

    It reproduces the *preconditions* the resident depends on in
    ``app/minecraft-sidecar/src/adapter.js`` and ``controller.js``: an
    observe-only ``set_home``, ``home`` refusing until a set_home was accepted,
    farm soil/crop/hoe/seed rules, reachability limits for move, placement
    support rules, and exact PvP targets.  Every skill the real sidecar (or an
    integrated server) would refuse is recorded in ``rejected`` and raised as a
    sidecar failure, so an "accept everything" mock cannot hide a contract
    break.
    """

    def __init__(self, clock, guard=None, *, players=(), start_tod=6000,
                 weather=(), roof=False, entities=()):
        self.clock = clock
        self.guard = guard
        self.config = None
        self.alive = False
        self.starts = 0
        self.stops = []
        self.calls = []
        self.skills = []
        self.rejected = []
        self.planted = []
        self.attacks = []
        self.crops = {}
        self.home = None
        self.fail_set_home = False
        self.set_home_guard_actions = []
        self.dimension = "minecraft:overworld"
        self.position = [0.0, 64.0, 0.0]
        self.start = float(clock())
        self.start_tod = int(start_tod)
        self.players = list(players)
        self.entities = [dict(row) for row in entities]
        self.weather = tuple(weather)
        self.roof = bool(roof)
        self.health = 20.0
        self.food = 18.0
        self.inventory = {"dirt": 16, "wooden_hoe": 1, "wheat_seeds": 16,
                          "bread": 24, "cobblestone": 8}
        self.blocks = {}
        for x in range(-40, 41):
            for z in range(-40, 41):
                self.blocks[(x, 62, z)] = "stone"
                self.blocks[(x, 63, z)] = "dirt"

    # ---- process lifecycle -------------------------------------------------
    async def start(self):
        self.starts += 1
        self.alive = True

    async def stop(self, graceful=True):
        self.stops.append(bool(graceful))
        self.alive = False

    async def connect_with_retry(self, params, prepare, *, attempts=1, timeout=35):
        await prepare()
        self.alive = True
        return {"connected": True, "uuid": ACTOR, "world_id": WORLD,
                "backup_completed": True}

    # ---- environment -------------------------------------------------------
    @property
    def elapsed(self):
        return max(0.0, float(self.clock()) - self.start)

    @property
    def time_of_day(self):
        return int((self.start_tod + self.elapsed * 20) % 24000)

    @property
    def weather_at(self):
        current = "clear"
        for start, end, name in self.weather:
            if start <= self.elapsed < end:
                current = name
        return current

    @property
    def light(self):
        hour = (self.time_of_day / 1000.0 + 6.0) % 24
        return 15 if 6 <= hour < 18 else 4

    def _player_cell(self):
        return (int(math.floor(self.position[0])),
                int(math.floor(self.position[1])),
                int(math.floor(self.position[2])))

    def _block(self, x, y, z):
        return self.blocks.get((int(x), int(y), int(z)), "air")

    def _set(self, x, y, z, name):
        key = (int(x), int(y), int(z))
        if name == "air":
            self.blocks.pop(key, None)
        else:
            self.blocks[key] = name

    def _within(self, x, y, z, limit):
        return math.dist((float(x), float(y), float(z)),
                         (self.position[0], self.position[1], self.position[2])) <= limit

    def _deny(self, skill, reason):
        self.rejected.append({"skill": skill, "reason": reason,
                              "args": copy.deepcopy(getattr(self, "_current_args", None))})
        raise SidecarError(reason)

    def _visible_players(self):
        rows = [copy.deepcopy(row) for row in self.entities
                if row.get("type") == "player"]
        present = {row.get("uuid") for row in rows}
        for index, uuid in enumerate(self.players):
            if uuid in present:
                continue
            offset = 1.5 if index % 2 == 0 else -1.5
            rows.append({
                "type": "player", "name": "Someone", "uuid": uuid,
                "position": {"x": round(self.position[0] + offset, 2),
                             "y": self.position[1],
                             "z": round(self.position[2] + offset, 2)}})
        return rows

    def observation(self):
        px, py, pz = self._player_cell()
        blocks = []
        for dx in range(-2, 3):
            for dy in range(-1, 2):
                for dz in range(-2, 3):
                    if len(blocks) >= 32:
                        break
                    key = (px + dx, py + dy, pz + dz)
                    name = self.blocks.get(key)
                    if name:
                        blocks.append({"name": name,
                                       "position": {"x": key[0], "y": key[1],
                                                    "z": key[2]}})
        entities = self._visible_players() + [
            copy.deepcopy(row) for row in self.entities
            if row.get("type") != "player"]
        return {
            "health": self.health, "food": self.food,
            "position": {"x": self.position[0], "y": self.position[1],
                         "z": self.position[2]},
            "dimension": self.dimension,
            "inventory": [{"name": name, "count": count}
                          for name, count in self.inventory.items() if count > 0][:64],
            "nearby_entities": entities[:32],
            "nearby_blocks": blocks,
            "time_of_day": self.time_of_day,
            "weather": self.weather_at,
            "light": self.light,
            "roof": self.roof,
            "events": [{"type": "weather_change", "at": 0}],
        }

    # ---- json-rpc ----------------------------------------------------------
    async def request(self, method, params=None, timeout=35):
        self.calls.append((method, params, timeout))
        if method == "observe":
            return copy.deepcopy(self.observation())
        if method == "skill":
            params = dict(params or {})
            name, args = params.get("name"), params.get("args")
            self._current_args = args
            self.skills.append({"name": name, "args": copy.deepcopy(args)})
            if not mr.validate_skill(name, args):
                self._deny(name, "invalid_args")
            target = args.get("target_uuid") if name == "attack" else None
            player_uuids = {row.get("uuid") for row in self._visible_players()}
            if name in {"set_home", "inventory"}:
                expected = "observe"
            elif name == "attack" and target in player_uuids:
                expected = "pvp"
            else:
                expected = "write"
            action = self.guard.actions[-1][0] if (
                self.guard is not None and self.guard.actions) else ""
            if action != expected:
                self._deny(name, "guard_action_" + str(action))
            self._run_skill(name, args)
            return {"ok": True}
        if method == "connect":
            self.alive = True
            return {"connected": True, "uuid": ACTOR, "world_id": WORLD,
                    "backup_completed": True}
        return {}

    def _run_skill(self, name, args):
        if name == "set_home":
            if self.fail_set_home:
                self._deny(name, "set_home_rejected")
            if self.guard is not None and self.guard.actions:
                self.set_home_guard_actions.append(self.guard.actions[-1][0])
            if self._block(*self._player_cell()) != "air":
                self._deny(name, "feet_blocked")
            self.home = {"x": round(self.position[0], 3),
                         "y": round(self.position[1], 3),
                         "z": round(self.position[2], 3),
                         "dimension": self.dimension}
            return
        if name == "home":
            if self.home is None:
                self._deny(name, "home_not_set")
            if self.home["dimension"] != self.dimension:
                self._deny(name, "home_dimension")
            if not self._within(self.home["x"], self.home["y"], self.home["z"], 16):
                self._deny(name, "home_too_far")
            self.position = [float(self.home["x"]), float(self.home["y"]),
                             float(self.home["z"])]
            return
        if name in {"move", "explore"}:
            x, y, z = float(args["x"]), float(args["y"]), float(args["z"])
            # The adapter uses GoalNear with a radius, so a solid target cell is
            # legal as long as the destination is loaded and nearby; the bot
            # simply stands next to it.
            if not self._within(x, y, z, 16):
                self._deny(name, "target_too_far")
            if self._block(x, y, z) == "air" and self._block(x, y - 1, z) != "air":
                self.position = [x, y, z]
            return
        if name == "collect":
            # The adapter scans the loaded 7x5x7 area for the named block.  A
            # synthetic dirt supply is seeded around the standing position so
            # repeated building projects have something real to mine.
            px, py, pz = self._player_cell()
            for dx, dz in ((1, -1), (0, 1), (-1, 1), (1, -2)):
                cell = (px + dx, py, pz + dz)
                if cell not in self.blocks:
                    self.blocks[cell] = args["block"]
            mined = 0
            for dx in range(-3, 4):
                for dy in range(-2, 3):
                    for dz in range(-3, 4):
                        if mined >= args["count"]:
                            break
                        cell = (px + dx, py + dy, pz + dz)
                        if self.blocks.get(cell) != args["block"]:
                            continue
                        if not self._within(*cell, 4.5):
                            continue
                        # the synthetic support layer is never mined
                        if cell[1] <= 63:
                            continue
                        self._set(*cell, "air")
                        mined += 1
            self.inventory[args["block"]] = self.inventory.get(args["block"], 0) + mined
            return
        if name == "dig":
            cell = (args["x"], args["y"], args["z"])
            block = self.blocks.get(cell)
            if not block or block == "bedrock":
                self._deny(name, "no_block")
            self._set(*cell, "air")
            if block in self.inventory:
                self.inventory[block] += 1
            return
        if name == "place":
            x, y, z = args["x"], args["y"], args["z"]
            item = args["item"]
            if _INERT_DENY.search(item):
                self._deny(name, "unsafe_item")
            if self._block(x, y, z) != "air":
                self._deny(name, "destination_occupied")
            if (x, y, z) == self._player_cell():
                self._deny(name, "destination_is_player")
            support = self._block(x - args["face_x"], y - args["face_y"],
                                  z - args["face_z"])
            if support == "air":
                self._deny(name, "no_support")
            if self.inventory.get(item, 0) <= 0:
                self._deny(name, "missing_item")
            self.inventory[item] -= 1
            self._set(x, y, z, item)
            return
        if name == "craft":
            if args["item"] not in {"stick", "planks", "oak_planks"}:
                self._deny(name, "unknown_recipe")
            self.inventory[args["item"]] = (
                self.inventory.get(args["item"], 0) + args["count"])
            return
        if name == "farm":
            x, y, z = args["x"], args["y"], args["z"]
            soil = self._block(x, y, z)
            if soil not in _SOIL_BLOCKS:
                self._deny(name, "not_soil")
            if not self._within(x, y, z, 4.5):
                self._deny(name, "soil_too_far")
            crop_name = _CROPS[args["seed"]]
            above = self._block(x, y + 1, z)
            if above not in {"air", crop_name}:
                self._deny(name, "crop_blocked")
            if above == crop_name:
                if self.crops.get((x, y + 1, z), 0) != 7:
                    self._deny(name, "crop_immature")
                self._set(x, y + 1, z, "air")
            if soil != "farmland":
                if self.inventory.get("wooden_hoe", 0) <= 0:
                    self._deny(name, "missing_hoe")
                self._set(x, y, z, "farmland")
            if self.inventory.get(args["seed"], 0) <= 0:
                self._deny(name, "missing_seed")
            if (x, y + 1, z) == self._player_cell():
                self._deny(name, "crop_is_player")
            self.inventory[args["seed"]] -= 1
            self._set(x, y + 1, z, crop_name)
            self.crops[(x, y + 1, z)] = 0
            self.planted.append((x, y, z))
            return
        if name == "breed":
            if not any(row.get("uuid") == args["target_uuid"]
                       for row in self.entities if row.get("type") != "player"):
                self._deny(name, "no_animal")
            return
        if name == "eat":
            if self.inventory.get("bread", 0) <= 0:
                self._deny(name, "no_food")
            self.inventory["bread"] -= 1
            self.food = min(20.0, self.food + 4)
            return
        if name == "follow":
            matches = [row for row in self._visible_players()
                       if row.get("uuid") == args["target_uuid"]]
            if not matches:
                self._deny(name, "no_target")
            position = matches[0].get("position") or {}
            if not self._within(position.get("x", 0), position.get("y", 0),
                                position.get("z", 0), 16):
                self._deny(name, "follow_too_far")
            return
        if name == "attack":
            target = args["target_uuid"]
            players = [row for row in self._visible_players()
                       if row.get("uuid") == target]
            hostiles = [row for row in self.entities
                        if row.get("uuid") == target and row.get("type") != "player"]
            if players:
                position = players[0].get("position") or {}
                if not self._within(position.get("x", 0), position.get("y", 0),
                                    position.get("z", 0), 3):
                    self._deny(name, "attack_too_far")
                if self.config is None:
                    self._deny(name, "pvp_disabled")
                target_list = {mr._safe_uuid(uuid) for uuid in
                               (self.config.get("minecraft.pvp_target_uuids", []) or [])}
                if not (self.config.get("minecraft.pvp_enabled", False) is True
                        and self.config.get("minecraft.write_enabled", False) is True
                        and target in target_list):
                    self._deny(name, "pvp_not_allowed")
                self.attacks.append(target)
                return
            if hostiles:
                if self.config is None or self.config.get(
                        "minecraft.write_enabled", False) is not True:
                    self._deny(name, "write_disabled")
                self.attacks.append(target)
                return
            self._deny(name, "unknown_target")
        if name == "respawn":
            self.position = [0.0, 64.0, 0.0]
            self.health, self.food = 20.0, 20.0
            return
        if name == "inventory":
            return
        if name == "chat":
            if self.config is None or self.config.get(
                    "minecraft.game_chat_enabled", False) is not True:
                self._deny(name, "chat_disabled")
            return
        if name == "chest":
            self._deny(name, "no_container")
        self._deny(name, "unsupported_skill")


def observation(*, health=20, food=18, position=None, players=(), inventory=None,
                blocks=None, dimension="minecraft:overworld", **env):
    row = {
        "health": health, "food": food,
        "position": position or {"x": 0.0, "y": 64.0, "z": 0.0},
        "dimension": dimension, "inventory": list(inventory or []),
        "nearby_entities": [], "nearby_blocks": list(blocks or []),
    }
    for item in players:
        row["nearby_entities"].append({
            "type": "player", "name": "Someone", "uuid": item,
            "position": {"x": 2.0, "y": 64.0, "z": 2.0},
        })
    row.update(env)
    return row


class ResidentBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.guard = FakeGuard(self.clock)
        self.obs = observation()
        self.sidecar = FakeSidecar(self.clock, self.obs)
        self.config = Config(copy.deepcopy(DEFAULT))
        self.config["minecraft"].update({
            "enabled": True,
            "host": "127.0.0.1",
            "port": LAN_PORT,
            "version": "1.20.1",
            "username": "SyntheticBot",
            "auth_mode": "offline",
            "connect_timeout_sec": 15,
            "owner_qq_uid": OWNER_UID,
            "owner_player_uuid": ACTOR,
            "test_world_id": WORLD,
            "world_path": os.path.join(self.root, "synthetic-world"),
            "write_enabled": True,
            "pvp_enabled": False,
            "pvp_target_uuids": [],
            "backup_interval_min": 30,
            "decision_enabled": False,
        })
        self.longmem = FakeLongMemory()
        self.bot = SimpleNamespace(
            config=self.config,
            paths=FakePaths(self.root),
            persona=FakePersona(),
            emotion=Emotion(str(self.root / "emotion.json")),
            longmem=self.longmem,
            base_dir=str(self.root),
        )
        self.resident = self.make_resident()

    def make_resident(self, *, config=None, sidecar=None, guard=None):
        return mr.MinecraftResident(
            self.bot if config is None else SimpleNamespace(
                config=config, paths=FakePaths(self.root), persona=FakePersona(),
                emotion=Emotion(str(self.root / "emotion.json")),
                longmem=self.longmem, base_dir=str(self.root)),
            sidecar=sidecar or self.sidecar, guard=guard or self.guard,
            clock=self.clock)

    def state_file(self):
        return self.root / "content" / "minecraft" / "resident.json"

    def budget_file(self):
        return self.root / "state" / "minecraft" / "budget.json"

    def _seed_project(self):
        self.resident.state.data["projects"] = [{
            "id": "p1", "kind": "shelter", "title": "搭一个能躲雨的小屋",
            "goal": "test", "status": "active", "step": 0, "progress": 0,
            "steps": self.resident._project_steps("shelter", {"x": 0, "y": 64, "z": 0}),
            "anchor": {"x": 0, "y": 64, "z": 0}, "created_at": self.clock(),
            "last_acted": 0, "failures": 0,
        }]
        return self.resident.state.data["projects"][0]


class PureValidationTests(unittest.TestCase):
    def test_defaults_stay_disabled_remote_off_and_backup_bounded(self):
        default = DEFAULT["minecraft"]
        self.assertFalse(default["enabled"])
        self.assertFalse(default["write_enabled"])
        self.assertFalse(default["pvp_enabled"])
        self.assertFalse(default["decision_enabled"])
        self.assertEqual(default["port"], 0)
        self.assertLessEqual(default["backup_interval_min"], 30)
        self.assertGreaterEqual(default["backup_interval_min"], 1)
        self.assertEqual(default["pvp_target_uuids"], [])

    def test_skill_schemas_reject_unknown_skills_extra_keys_and_commands(self):
        self.assertFalse(mr.validate_skill("eval", {}))
        self.assertFalse(mr.validate_skill("shell", {}))
        self.assertFalse(mr.validate_skill("chat", {"text": "/op @a"}))
        self.assertFalse(mr.validate_skill("chat", {"text": "hello\nworld"}))
        self.assertTrue(mr.validate_skill("chat", {"text": "hello"}))
        self.assertFalse(mr.validate_skill("move", {"x": 0, "y": 64, "z": 0,
                                                    "radius": 1, "extra": 1}))
        self.assertFalse(mr.validate_skill("move", {"x": 0, "y": 64, "z": 0,
                                                    "radius": 9}))
        self.assertTrue(mr.validate_skill("move", {"x": 0, "y": 64.5, "z": 0,
                                                   "radius": 1}))
        self.assertFalse(mr.validate_skill("collect", {"block": "dirt", "count": 0}))
        self.assertFalse(mr.validate_skill("place", {"x": 0, "y": 64, "z": 0,
                                                     "face_x": 1, "face_y": 1,
                                                     "face_z": 0, "item": "dirt"}))
        self.assertFalse(mr.validate_skill("attack", {"target_uuid": "*"}))
        self.assertTrue(mr.validate_skill("attack", {"target_uuid": OTHER_1}))
        digit_uuid = "12345678-1234-4234-8234-123456789012"
        self.assertTrue(mr.validate_skill("follow", {
            "target_uuid": digit_uuid, "duration_sec": 5}))
        self.assertEqual(mr._clean_steps([{
            "skill": "follow", "args": {"target_uuid": digit_uuid, "duration_sec": 5}}]),
            [{"skill": "follow", "args": {"target_uuid": digit_uuid, "duration_sec": 5}}])

    def test_safe_observation_accepts_real_sidecar_fields_and_drops_unknowns(self):
        row = {
            "health": 18.5, "food": 17, "position": {"x": 1, "y": 64, "z": 2},
            "dimension": "minecraft:overworld",
            "inventory": [{"name": "dirt", "count": 4}],
            "nearby_entities": [{"type": "player", "name": "Nick",
                                 "uuid": OTHER_1,
                                 "position": {"x": 2, "y": 64, "z": 2}}],
            "nearby_blocks": [{"name": "grass_block",
                               "position": {"x": 0, "y": 63, "z": 0}}],
            "raw_nbt": {"secret": "must not survive"},
            "world_path": "C:\\original\\world",
        }
        safe = mr.safe_observation(row)
        self.assertEqual(safe["health"], 18.5)
        self.assertEqual(safe["food"], 17)
        self.assertEqual(safe["inventory"], [{"name": "dirt", "count": 4}])
        self.assertNotIn("raw_nbt", safe)
        self.assertNotIn("world_path", safe)
        self.assertEqual(safe["nearby_entities"][0]["distance"], 1.0)
        with self.assertRaises(SidecarError):
            mr.safe_observation({"health": True, "food": 20})
        with self.assertRaises(SidecarError):
            mr.safe_observation({"health": 20})
        with self.assertRaises(SidecarError):
            mr.safe_observation([])

    def test_nearby_players_require_exact_uuid_and_never_use_names(self):
        row = observation(players=[OTHER_1.upper()])
        row["nearby_entities"].append({"type": "player", "name": "NoUuid",
                                       "position": {"x": 0, "y": 64, "z": 0}})
        self.assertEqual([item["uuid"] for item in mr.nearby_players(row)], [OTHER_1])

    def test_guard_url_origin_and_secret_text_are_normalized(self):
        self.assertEqual(mr._guard_endpoint("http://127.0.0.1:6198"),
                         "http://127.0.0.1:6198/v1/permit")
        self.assertEqual(mr._guard_endpoint("http://127.0.0.1:6198/"),
                         "http://127.0.0.1:6198/v1/permit")
        self.assertEqual(mr._guard_endpoint("http://127.0.0.1:6198/v1/permit"),
                         "http://127.0.0.1:6198/v1/permit")
        self.assertEqual(mr._safe_text("/home/owner/world 12345"), "[path] [id]")


class LifecycleTests(ResidentBase):
    def test_disabled_and_strict_boolean_do_not_start_sidecar(self):
        self.config["minecraft"]["enabled"] = False
        self.assertFalse(asyncio.run(self.resident.start()))
        self.assertEqual(self.sidecar.starts, 0)
        self.config["minecraft"]["enabled"] = "true"
        self.assertFalse(asyncio.run(self.resident.start()))
        self.assertEqual(self.sidecar.starts, 0)

    def test_start_requires_owner_binding_and_exact_integer_connect_config(self):
        self.config["minecraft"]["owner_qq_uid"] = ""
        with self.assertRaises(GuardDenied) as ctx:
            asyncio.run(self.resident.start())
        self.assertEqual(str(ctx.exception), "owner_identity_unbound")
        self.assertEqual(self.sidecar.starts, 0)

        self.config["minecraft"]["owner_qq_uid"] = OWNER_UID
        self.config["minecraft"]["connect_timeout_sec"] = 15.5
        with self.assertRaises(GuardDenied) as ctx:
            asyncio.run(self.resident.start())
        self.assertEqual(str(ctx.exception), "connect_config_invalid")
        self.assertEqual(self.sidecar.starts, 0)

        self.config["minecraft"]["connect_timeout_sec"] = 15
        self.config["minecraft"]["port"] = 0
        with self.assertRaises(GuardDenied) as ctx:
            asyncio.run(self.resident.start())
        self.assertEqual(str(ctx.exception), "connect_config_invalid")

    def test_sidecar_env_flags_are_literal_true_only(self):
        self.config["minecraft"]["write_enabled"] = "true"
        self.config["minecraft"]["pvp_enabled"] = 1
        env = self.resident._sidecar_env()
        self.assertEqual(env["PAWBOT_MC_WRITE_ENABLED"], "false")
        self.assertEqual(env["PAWBOT_MC_PVP_ENABLED"], "false")
        self.config["minecraft"]["write_enabled"] = True
        self.config["minecraft"]["pvp_enabled"] = True
        env = self.resident._sidecar_env()
        self.assertEqual(env["PAWBOT_MC_WRITE_ENABLED"], "true")
        self.assertEqual(env["PAWBOT_MC_PVP_ENABLED"], "true")
        self.assertEqual(json.loads(env["PAWBOT_MC_PVP_UUIDS"]), [])

    def test_start_uses_one_non_snapshot_preflight_and_binds_actor(self):
        asyncio.run(self._start_and_stop())
        self.assertEqual(self.sidecar.starts, 1)
        actions = [action for action, _target in self.guard.actions]
        # The child's single guard `connect` already performs the mandatory fresh snapshot; the
        # resident must not add a `backup` or a second `connect` (double/triple copy per connect).
        self.assertEqual(actions.count("backup"), 0)
        self.assertEqual(actions.count("connect"), 0)
        self.assertEqual(actions[0], "observe")
        self.assertEqual(self.sidecar.connect_params["host"], "127.0.0.1")
        self.assertEqual(self.sidecar.connect_params["port"], LAN_PORT)
        self.assertEqual(self.sidecar.connect_params["version"], "1.20.1")
        self.assertIs(type(self.sidecar.connect_params["timeout_sec"]), int)
        binding = json.loads((self.root / "state" / "minecraft" / "bindings.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(binding["actor_uuid"], ACTOR)
        self.assertEqual(binding["world_id"], WORLD)
        self.assertNotIn("owner_qq_uid", binding)
        self.assertNotIn("world_path", binding)
        self.assertIn("revoke", actions)
        self.assertTrue(self.sidecar.stops)

    def test_connect_requires_the_child_to_report_a_completed_backup(self):
        self.sidecar.omit_backup_completed = True

        async def scenario():
            await self.resident.start()

        with self.assertRaises(Exception):
            asyncio.run(scenario())

    async def _start_and_stop(self):
        await self.resident.start()
        await self.resident.stop()

    def test_estop_latches_and_core_facing_stop_is_safe(self):
        async def scenario():
            await self.resident.start()
            return await self.resident.estop()

        result = asyncio.run(scenario())
        self.assertTrue(result["latched"])
        self.assertTrue(self.resident.latched)
        self.assertIn("revoke", [action for action, _target in self.guard.actions])
        self.assertTrue(self.sidecar.stops)
        with self.assertRaises(GuardDenied) as ctx:
            asyncio.run(self.resident.start())
        self.assertEqual(str(ctx.exception), "resident_latched")


class GuardAndSkillTests(ResidentBase):
    def test_observe_and_write_are_guarded_with_backup_interval_cap(self):
        self.config["minecraft"]["backup_interval_min"] = 60
        self.assertEqual(self.resident._backup_interval_seconds(), 30 * 60)
        self.sidecar.alive = True
        self.resident._home_seeded = True   # home seeding has its own tests
        self.resident._last_backup = self.clock()
        self.resident._last_decision = self.clock() - 120
        self.obs["food"] = 3
        asyncio.run(self.resident.tick())
        actions = [action for action, _target in self.guard.actions]
        self.assertEqual(actions, ["observe", "write"])
        self.assertEqual([call[0] for call in self.sidecar.calls], ["observe", "skill"])
        self.assertEqual(self.sidecar.skills[0]["name"], "eat")
        self.assertIs(type(self.sidecar.skills[0]["timeout_sec"]), int)

    def test_no_periodic_backup_request_is_made_by_the_resident(self):
        # The Java guard is the single backup scheduler; a due legacy interval must not add a
        # second snapshot stream from this process.
        self.sidecar.alive = True
        self.resident._home_seeded = True   # home seeding has its own tests
        self.resident._last_backup = self.clock() - 1801
        self.resident._last_decision = self.clock()
        asyncio.run(self.resident.tick())
        actions = [action for action, _target in self.guard.actions]
        self.assertNotIn("backup", actions)
        self.assertEqual(actions[0], "observe")
        self.assertFalse(hasattr(self.resident, "_backup_if_due"))
        # The compatibility key still validates to the shared ceiling, advisory only.
        self.config["minecraft"]["backup_interval_min"] = 60
        self.assertEqual(self.resident._backup_interval_seconds(), 30 * 60)

    def test_child_busy_error_code_is_a_bounded_pause(self):
        # The child may hit a snapshot at its own checkpoint; its -32008 must reach the resident as
        # the same bounded guard_busy pause, not as a skill failure.
        self.sidecar.skill_error = SidecarError("skill_rejected", code=-32008)
        self.resident._home_seeded = True
        self.resident.state.data["drives"]["energy"] = .1
        self.obs["food"] = 3
        decision = asyncio.run(self.resident.decide(self.obs))
        decision.intent = "satisfy_drive"
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome["executed"])
        self.assertEqual(outcome["reason"], "guard_busy")
        self.assertEqual(self.resident.state.data["counters"]["failures"], 0)
        self.assertGreaterEqual(self.resident._idle_cooldown, 1)

    def test_unavailable_guard_still_latches_the_resident(self):
        # A real guard/transport failure keeps the fail-closed quarantine; only a *signed busy*
        # receipt is a pause.
        async def scenario():
            self.resident._started = True
            self.resident.sidecar.alive = True
            self.resident._stop.clear()
            self.guard.unavailable = True
            await self.resident._run()

        asyncio.run(scenario())
        self.assertTrue(self.resident.latched)
        self.assertTrue(self.sidecar.stops)

    def test_guard_busy_is_a_bounded_pause_not_a_failure(self):
        self.resident._home_seeded = True
        self.resident.state.data["drives"]["energy"] = .1
        self.obs["food"] = 3
        decision = asyncio.run(self.resident.decide(self.obs))
        decision.intent = "satisfy_drive"
        self.guard.busy.add("write")
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome["executed"])
        self.assertEqual(outcome["reason"], "guard_busy")
        self.assertEqual(self.sidecar.skills, [])
        self.assertEqual(self.resident.state.data["counters"]["failures"], 0)
        self.assertGreaterEqual(self.resident._idle_cooldown, 1)

    def test_connect_preflight_tolerates_a_busy_observe(self):
        # A busy observe is a valid non-mutating answer, so the resident's cheap preflight cannot
        # turn a snapshot window into a failed connection; the child retries its connect permit.
        self.guard.busy_observe = True

        async def scenario():
            await self.resident.start()
            await self.resident.stop()

        asyncio.run(scenario())
        self.assertEqual(self.sidecar.starts, 1)
        self.assertEqual([action for action, _target in self.guard.actions].count("backup"), 0)
        self.assertEqual([action for action, _target in self.guard.actions].count("connect"), 0)

    def test_one_denied_skill_does_not_kill_loop_and_next_attempt_can_succeed(self):
        project = self._seed_project()
        self.resident._last_decision = self.clock() - 30
        self.guard.deny.add("write")
        decision = asyncio.run(self.resident.decide(self.obs))
        decision.intent = "continue_project"
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertTrue(outcome["executed"])
        self.assertFalse(outcome["ok"])
        self.assertEqual(self.sidecar.skills, [])
        self.assertEqual(self.resident.state.data["counters"]["failures"], 1)
        self.guard.deny.clear()
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertTrue(outcome["ok"])
        self.assertEqual(len(self.sidecar.skills), 1)
        self.assertEqual(project["step"], 1)

    def test_unknown_or_invalid_project_skill_is_never_executed(self):
        self.resident.state.data["projects"] = [{
            "id": "p1", "kind": "shelter", "title": "x", "status": "active",
            "step": 0, "progress": 0,
            "steps": [{"skill": "shell", "args": {}}],
        }]
        decision = mr.Decision("continue_project")
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertEqual(outcome["reason"], "no_valid_skill")
        self.assertEqual(self.sidecar.skills, [])
        self.resident.state.data["projects"][0]["steps"] = [
            {"skill": "chat", "args": {"text": "/kill @e"}}]
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertEqual(outcome["reason"], "no_valid_skill")
        self.assertEqual(self.sidecar.skills, [])

    def test_game_chat_slash_is_denied_even_if_a_project_asks_for_it(self):
        self.resident.state.data["projects"] = [{
            "id": "p1", "kind": "shelter", "title": "x", "status": "active",
            "step": 0, "progress": 0,
            "steps": [{"skill": "chat", "args": {"text": "/op"}}],
        }]
        decision = mr.Decision("continue_project")
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertEqual(outcome["reason"], "no_valid_skill")
        self.assertEqual(self.sidecar.skills, [])

    def test_repeat_and_failure_loops_are_bounded(self):
        self.resident.state.data["drives"]["hoarding_urge"] = .9
        args = {"block": "dirt", "count": 3}
        signature = f"collect:{self.resident._sig_args(args)}"
        self.resident._action_log = [
            {"sig": signature, "at": self.clock(), "ok": True} for _ in range(3)]
        self.sidecar.alive = True
        decision = mr.Decision("satisfy_drive")
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertEqual(outcome["reason"], "repeat_blocked")
        self.assertEqual(self.sidecar.skills, [])
        self.assertGreaterEqual(self.resident._idle_cooldown, 2)

        self._seed_project()
        for _ in range(3):
            self.resident._record_failure("collect", "test")
        self.assertGreaterEqual(self.resident._idle_cooldown, 2)
        self.assertEqual(self.resident.state.data["projects"][0]["status"], "paused")
        result = asyncio.run(self.resident._cycle(self.obs))
        self.assertEqual(result["reason"], "cooldown")

    def _seed_project(self):
        self.resident.state.data["projects"] = [{
            "id": "p1", "kind": "shelter", "title": "搭一个能躲雨的小屋",
            "goal": "test", "status": "active", "step": 0, "progress": 0,
            "steps": self.resident._project_steps("shelter", {"x": 0, "y": 64, "z": 0}),
            "anchor": {"x": 0, "y": 64, "z": 0}, "created_at": self.clock(),
            "last_acted": 0, "failures": 0,
        }]
        return self.resident.state.data["projects"][0]


class MindTests(ResidentBase):
    def test_workspace_is_three_to_five_items_and_perception_does_not_act(self):
        impulse = self.resident.impulse_engine.propose(
            self.obs, self.resident.state, self.resident._affect(), self.resident.workspace)
        ranked = self.resident.planner.rank(
            self.obs, self.resident.state, self.resident._affect(),
            self.resident.workspace, impulse)
        workspace = self.resident._build_workspace(self.obs, impulse, ranked)
        self.assertGreaterEqual(len(workspace), 3)
        self.assertLessEqual(len(workspace), 5)
        self.assertEqual(self.sidecar.calls, [])
        for item in workspace.as_context():
            self.assertNotIn("owner_qq_uid", item)
            self.assertNotIn("world_path", item)

    def test_y_zero_is_a_valid_anchor_not_replaced_by_default(self):
        self.resident._ensure_project(observation(position={"x": 1, "y": 0, "z": 2}))
        project = self.resident.state.data["projects"][0]
        self.assertEqual(project["anchor"]["y"], 0)
        self.assertEqual(project["steps"][0]["args"]["y"], 0.0)

    def test_fallback_decision_is_deterministic_and_intent_only(self):
        first = asyncio.run(self.resident.decide(self.obs))
        second = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(first.intent, second.intent)
        self.assertIn(first.intent, mr.INTENTS)
        self.assertEqual(self.resident.jev.can_decide, False)
        self.assertNotIn("private_thought", first.safe_log())
        self.assertEqual(first.skill, "")
        self.assertTrue(first.private_thought)  # in-memory only

    def test_remote_budget_reserves_before_call_even_on_error_and_invalid(self):
        self.config["minecraft"].update({
            "decision_enabled": True,
            "decision_remote_enabled": True,
            "decision_cost_usd_per_call": 0.01,
            "decision_budget_usd_per_day": 0.05,
            "max_decisions_per_day": 5,
        })
        captured = {}

        async def fake_decide(config, state, questions, *, timeout=None):
            captured["state"] = state
            captured["questions"] = questions
            return {"intent": {"choice": "shell"}, "confidence": {"score": 0.9}}

        with mock.patch.object(mr.decider, "decide", fake_decide):
            decision = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(self.resident.budget.data["attempts"], 1)
        self.assertAlmostEqual(self.resident.budget.data["spent_usd"], 0.01)
        self.assertIn(decision.source, {"drive", "fallback", "opportunity", "social"})
        self.assertNotIn("private_thought", captured["state"])
        self.assertNotIn("owner_qq_uid", json.dumps(captured["state"]))
        self.assertNotIn("world_path", json.dumps(captured["state"]))
        self.assertGreaterEqual(len(captured["state"]["workspace"]), 3)
        self.assertLessEqual(len(captured["state"]["workspace"]), 5)
        self.assertEqual(set(captured["questions"]["intent"]["criteria"]), set(mr.INTENTS))

        async def fake_timeout(config, state, questions, *, timeout=None):
            raise TimeoutError("synthetic")

        with mock.patch.object(mr.decider, "decide", fake_timeout):
            decision = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(self.resident.budget.data["attempts"], 2)
        self.assertAlmostEqual(self.resident.budget.data["spent_usd"], 0.02)
        self.assertNotEqual(decision.source, "jev")

        async def fake_valid(config, state, questions, *, timeout=None):
            captured["budget_before"] = json.loads(
                self.budget_file().read_text(encoding="utf-8"))["attempts"]
            return {"intent": {"choice": "explore"}, "confidence": {"score": 0.9}}

        with mock.patch.object(mr.decider, "decide", fake_valid):
            decision = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(captured["budget_before"], 3)
        self.assertEqual(self.resident.budget.data["attempts"], 3)
        self.assertEqual(decision.intent, "explore")
        self.assertEqual(decision.source, "jev")
        self.assertEqual(self.sidecar.calls, [])
        persisted = json.loads(self.budget_file().read_text(encoding="utf-8"))
        self.assertEqual(persisted["attempts"], 3)

    def test_remote_requires_configured_cost_and_budget_exhaustion_falls_back(self):
        self.config["minecraft"].update({
            "decision_enabled": True,
            "decision_remote_enabled": True,
            "decision_budget_usd_per_day": 0.05,
            "max_decisions_per_day": 5,
        })
        self.resident.config["minecraft"].pop("decision_cost_usd_per_call", None)
        self.assertFalse(self.resident.jev.can_decide)
        with mock.patch.object(mr.decider, "decide",
                               mock.AsyncMock(return_value={"intent": {"choice": "rest"}})) as fake:
            decision = asyncio.run(self.resident.decide(self.obs))
        fake.assert_not_awaited()
        self.assertEqual(self.resident.budget.data["attempts"], 0)
        self.assertNotEqual(decision.source, "jev")

        self.config["minecraft"]["decision_cost_usd_per_call"] = 0.03
        with mock.patch.object(mr.decider, "decide",
                               mock.AsyncMock(return_value={"intent": {"choice": "rest"},
                                                            "confidence": {"score": .5}})) as fake:
            first = asyncio.run(self.resident.decide(self.obs))
            second = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(fake.await_count, 1)  # .03 + .03 > .05: never called twice
        self.assertEqual(self.resident.budget.data["attempts"], 1)
        self.assertEqual(first.source, "jev")
        self.assertNotEqual(second.source, "jev")

    def test_arbiter_can_hesitate_and_survival_overrides(self):
        deliberate = mr.Candidate("continue_project", .50, "project", "继续")
        impulse = [mr.Candidate("explore", .48, "opportunity", "机会", urgency=.2)]
        personality = dict(self.resident.state.data["personality"])
        personality.update({"caution": .95, "stubbornness": .10})
        decision = self.resident.arbiter.resolve(
            deliberate, impulse, [deliberate], personality,
            {"fear": .8, "warmth": 0}, mr.Appraisal())
        self.assertEqual(decision.intent, "idle")
        self.assertGreaterEqual(decision.conflict, .7)

        survival = [mr.Candidate("recover", .99, "survival", "危险", urgency=1.0)]
        decision = self.resident.arbiter.resolve(
            deliberate, survival, [deliberate], personality, {}, mr.Appraisal())
        self.assertEqual(decision.intent, "recover")
        self.assertEqual(decision.source, "survival")

        self.obs["health"] = 2
        self.sidecar.alive = True
        self.resident._last_decision = self.clock() - 30
        decision = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(decision.intent, "recover")
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertEqual(outcome["skill"], "respawn")
        self.assertEqual(self.sidecar.skills[0]["name"], "respawn")

    def test_appraisal_updates_shared_emotion_and_residue_decays(self):
        threat = observation(health=6, food=5)
        before = self.bot.emotion.values()["烦躁"]
        deltas = asyncio.run(self.resident._appraise(threat, force=True))
        self.assertIn("烦躁", deltas)
        self.assertGreater(self.bot.emotion.values()["烦躁"], before)
        fear = self.resident.state.data["affect_residue"]["fear"]
        self.assertGreater(fear, 0)
        asyncio.run(self.resident._appraise(threat, force=True))
        self.assertGreater(self.resident.state.data["affect_residue"]["fear"], fear)

        happy_before = self.bot.emotion.values()["开心"]
        asyncio.run(self.resident._appraise(
            observation(), force=True,
            outcome={"executed": True, "ok": True, "skill": "place"}))
        self.assertGreater(self.bot.emotion.values()["开心"], happy_before)

    def test_social_identity_keeps_duplicate_names_separate_and_owner_is_exact_uuid(self):
        twins = observation(players=[OTHER_1, OTHER_2])
        for entity in twins["nearby_entities"]:
            entity["name"] = "SameName"
        self.resident._update_social(twins)
        players = self.resident.state.data["players"]
        self.assertEqual(len(players), 2)
        self.assertTrue(all("name" not in row for row in players))
        self.assertTrue(all(row["role"] == "player" for row in players))

        owner = observation(players=[ACTOR])
        owner["nearby_entities"][0]["name"] = "NotTheOwnerName"
        self.resident._update_social(owner)
        owner_rows = [row for row in self.resident.state.data["players"]
                      if row["uuid"] == ACTOR]
        # The configured UUID is the bot actor, not the owner human's avatar.
        self.assertEqual(owner_rows[0]["role"], "self")
        self.assertFalse(owner_rows[0]["explicit_link"])

        context = self.resident._context(owner)
        self.assertTrue(context["self_model"]["social_self"]["actor_present"])
        self.assertFalse(context["self_model"]["social_self"]["owner_present"])
        encoded = json.dumps(context)
        self.assertNotIn(OWNER_UID, encoded)
        self.assertNotIn("world_path", encoded)
        self.assertTrue(all("uuid" not in row for row in context["social_models"]))

        # A cross-channel link exists only for an explicit local UUID -> UID
        # mapping, and the UID itself never reaches state or model context.
        self.config["minecraft"]["owner_mappings"] = {OTHER_1: OWNER_UID}
        self.resident._update_social(twins)
        mapped = [row for row in self.resident.state.data["players"]
                  if row["uuid"] == OTHER_1][0]
        self.assertTrue(mapped["explicit_link"])
        self.assertNotIn(OWNER_UID, json.dumps(self.resident.state.data))
        self.assertNotIn("qq", json.dumps(self.resident._context(owner)).lower())

    def test_persona_and_long_memory_are_shared_actual_contracts(self):
        self.resident._last_episode = 0
        decision = mr.Decision("continue_project", rationale="local")
        asyncio.run(self.resident._reflect(
            decision, self.obs, {"executed": True, "ok": True, "skill": "place"}))
        self.assertGreaterEqual(len(self.longmem.episodes), 1)
        episode = self.longmem.episodes[-1]
        self.assertEqual(episode["source"], "minecraft")
        self.assertTrue(str(episode["session_id"]).startswith("minecraft_"))
        self.assertNotIn(OWNER_UID, json.dumps(episode, ensure_ascii=False))
        self.assertNotIn(str(self.root), json.dumps(episode, ensure_ascii=False))
        context = self.resident._context(self.obs)
        self.assertIn("测试用共享人设", context["self_model"]["persona"])


class PersistenceAndAcceptanceTests(ResidentBase):
    def test_promise_text_is_sanitized_and_state_has_no_private_leak(self):
        title = "C:\\Users\\secret\\world 1234567890 见一面"
        promise_id = self.resident.add_promise(title)
        self.assertTrue(promise_id)
        text = self.resident.state.data["promises"][-1]["title"]
        self.assertNotIn("C:\\Users", text)
        self.assertNotIn("1234567890", text)
        self.resident.state.save()
        raw = self.state_file().read_text(encoding="utf-8")
        self.assertNotIn(OWNER_UID, raw)
        self.assertNotIn("C:\\Users\\secret", raw)
        self.assertNotIn("private_thought", raw)
        self.assertNotIn("owner_qq_uid", raw)

    def test_legacy_state_cannot_smuggle_paths_ids_or_private_thoughts(self):
        self.state_file().parent.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(json.dumps({
            "self_narrative": "C:\\original\\world 123456789",
            "recent_actions": ["C:\\original\\world 123456789"],
            "journal": [{"at": 1, "text": "/home/secret/world 99999999"}],
            "projects": [{"id": "p1", "status": "active", "title": "C:\\secret 88888888",
                          "steps": [{"skill": "chat", "args": {"text": "C:\\secret 77777777"}}]}],
            "private_thought": "do not persist",
            "owner_qq_uid": OWNER_UID,
            "world_path": "C:\\original\\world",
        }, ensure_ascii=False), encoding="utf-8")
        state = mr.ResidentState(self.state_file(), clock=self.clock)
        state.save()
        raw = self.state_file().read_text(encoding="utf-8")
        for forbidden in ("C:\\original", "C:\\secret", "99999999", "123456789",
                          "private_thought", "owner_qq_uid", "world_path"):
            self.assertNotIn(forbidden, raw)

    def test_project_and_promise_progress_survive_reload(self):
        self._seed_project()
        self.resident.add_promise("回家看看", steps=[{"skill": "home", "args": {}}])
        self.resident._last_decision = self.clock() - 30
        decision = asyncio.run(self.resident.decide(self.obs))
        decision.intent = "continue_project"
        outcome = asyncio.run(self.resident._execute(decision, self.obs))
        self.assertTrue(outcome["ok"])
        asyncio.run(self.resident._reflect(decision, self.obs, outcome))
        self.resident.state.save()
        reloaded = mr.ResidentState(self.state_file(), clock=self.clock)
        self.assertEqual(reloaded.data["projects"][0]["step"], 1)
        self.assertTrue(reloaded.data["promises"])
        self.assertEqual(reloaded.data["promises"][0]["steps"][0]["skill"], "home")
        self.assertTrue((self.root / "content" / "minecraft" / "diary").exists())

    def test_paused_project_can_resume_later(self):
        project = self._seed_project()
        project.update({"status": "paused", "failures": 2, "step": 3,
                        "last_acted": self.clock() - 301})
        self.resident._ensure_project(self.obs)
        self.assertEqual(project["status"], "active")
        self.assertEqual(project["failures"], 0)
        self.assertEqual(project["step"], 2)

    def test_sixty_minute_accelerated_autonomy_is_meaningful_and_bounded(self):
        # Contract-realistic child + real tick() path: home must be seeded,
        # farm coordinates must hit real soil, and rain must pull the resident
        # indoors.  The fake refuses anything the Node sidecar/server would.
        self.config["minecraft"].update({
            "decision_interval_sec": 30,
            "owner_human_uuid": OWNER_HUMAN,
        })
        contract = ContractSidecar(
            self.clock, self.guard,
            players=[OWNER_HUMAN, OTHER_1], start_tod=6000,
            weather=((0, 600, "clear"), (600, 1200, "rain"),
                     (1200, 1500, "thunder"), (1500, 3600, "clear")))
        contract.config = self.config
        self.resident = self.make_resident(sidecar=contract)
        self.resident.state.data["personality"].update(
            {"industriousness": .8, "curiosity": .7, "sociability": .7,
             "caution": .5, "attachment": .8})
        self.resident.state.data["drives"].update(
            {"energy": .9, "boredom": .2, "loneliness": .3, "wanderlust": .4})
        self.resident._last_decision = self.clock()
        outcomes = []

        async def run_hour():
            for _ in range(120):
                self.clock.advance(30)
                result = await self.resident.tick()
                outcomes.append(result["outcome"])

        asyncio.run(run_hour())
        state = self.resident.state.data
        skills = [row["name"] for row in contract.skills]
        self.assertEqual(contract.rejected, [])
        self.assertTrue(self.resident._home_seeded)
        self.assertEqual(contract.set_home_guard_actions, ["observe"])
        self.assertIn("home", skills)
        self.assertGreaterEqual(len(state["projects"]), 2)
        self.assertGreaterEqual(len({project.get("kind") for project in state["projects"]}), 2)
        self.assertGreaterEqual(state["counters"]["projects_completed"], 1)
        self.assertGreaterEqual(state["counters"]["decisions"], 50)
        self.assertLessEqual(state["counters"]["decisions"], 120)
        self.assertTrue(state["promises"])
        self.assertGreaterEqual(len(state["journal"]), 10)
        self.assertTrue(self.longmem.episodes)
        self.assertGreaterEqual(len(contract.skills), 20)
        allowed = {skill for values in mr.SKILLS.values() for skill in values}
        allowed.add("set_home")   # internal observe-only home seed
        self.assertTrue(set(skills) <= allowed)
        self.assertNotIn("inventory", set(skills))
        self.assertNotIn("chat", set(skills))
        self.assertNotIn("attack", set(skills))
        self.assertGreaterEqual(len(contract.planted), 1)
        self.assertLessEqual(state["counters"]["failures"], 10)
        self.assertGreaterEqual(len(self.resident.workspace), 3)
        self.assertLessEqual(len(self.resident.workspace), 5)
        roles = {row["uuid"]: row["role"] for row in state["players"]}
        self.assertEqual(roles.get(OWNER_HUMAN), "owner")
        self.assertEqual(roles.get(OTHER_1), "player")
        raw = self.state_file().read_text(encoding="utf-8")
        self.assertNotIn(OWNER_UID, raw)
        self.assertNotIn(str(self.root), raw)
        self.assertNotIn("private_thought", raw)
        diary_files = list((self.root / "content" / "minecraft" / "diary").glob("*.md"))
        self.assertTrue(diary_files)
        diary = "\n".join(path.read_text(encoding="utf-8") for path in diary_files)
        self.assertNotIn(OWNER_UID, diary)
        self.assertNotIn(str(self.root), diary)
        # Rain without a roof must have produced at least one home/shelter pull.
        self.assertTrue(any("rest" in str(outcome.get("reason", "")) or
                            outcome.get("skill") in {"home", "move"}
                            for outcome in outcomes))

    def test_cancellation_and_exceptional_exit_revoke_and_stop_child(self):
        async def scenario():
            await self.resident.start()
            await self.resident.stop()

        asyncio.run(scenario())
        self.assertIn("revoke", [action for action, _target in self.guard.actions])
        self.assertTrue(self.sidecar.stops)
        self.assertFalse(self.resident.started)

        self.resident._latched = False
        self.resident._started = True
        self.resident._stop.clear()
        self.resident.sidecar.alive = True
        self.resident.sidecar.observe_error = RuntimeError("synthetic")
        asyncio.run(self.resident._run())
        self.assertTrue(self.resident.latched)
        self.assertTrue(self.sidecar.stops)
        self.assertIn("revoke", [action for action, _target in self.guard.actions])

    def test_reconnect_uses_bounded_backoff_and_resumes(self):
        sleeper_calls = []

        async def sleeper(seconds):
            sleeper_calls.append(seconds)

        self.resident._sleep = sleeper
        self.sidecar.alive = False
        self.sidecar.fail_connects = 2
        ok = asyncio.run(self.resident._reconnect())
        self.assertTrue(ok)
        self.assertEqual(len(sleeper_calls), 3)
        self.assertTrue(all(seconds >= 0.05 for seconds in sleeper_calls))
        self.assertEqual(self.sidecar.connect_prepares, 3)
        self.assertEqual(self.resident._reconnect_attempts, 3)
        # The child owns the single connect permit+snapshot; the resident adds no snapshot requests.
        actions = [action for action, _target in self.guard.actions]
        self.assertEqual(actions.count("backup"), 0)
        self.assertEqual(actions.count("connect"), 0)
        self.assertGreaterEqual(actions.count("observe"), 3)
        self.assertTrue(self.sidecar.alive)

    def test_module_has_no_arbitrary_execution_surface(self):
        source = Path(mr.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "shell=True", "eval(", "exec("):
            self.assertNotIn(forbidden, source)


class ContractResidentBase(ResidentBase):
    """Resident backed by the contract-faithful child and a distinct owner."""

    def setUp(self):
        super().setUp()
        self.config["minecraft"]["owner_human_uuid"] = OWNER_HUMAN
        self.contract = ContractSidecar(self.clock, self.guard, players=[],
                                        start_tod=6000)
        self.contract.config = self.config
        self.resident = self.make_resident(sidecar=self.contract)

    def quiet_rest_state(self):
        """No active project/promise and a low-energy rest pull."""
        self.resident.state.data["drives"].update(
            {"energy": .1, "boredom": .2, "loneliness": .1,
             "creative_tension": .2, "hoarding_urge": .1,
             "care_urge": .1, "wanderlust": .1})
        self.resident.state.data["projects"] = [{
            "id": "p9", "kind": "shelter", "title": "暂停的项目",
            "status": "paused", "step": 0, "progress": 0, "steps": [],
            "anchor": {"x": 0, "y": 64, "z": 0}, "created_at": 0,
            "last_acted": self.clock(), "failures": 2,
        }]
        self.resident.state.data["places"] = [{
            "kind": "home", "x": 0.0, "y": 64.0, "z": 0.0,
            "dimension": "minecraft:overworld", "visits": 1,
            "last_seen": self.clock(),
        }]
        self.resident._last_decision = self.clock() - 120


class HomeSeedContractTests(ContractResidentBase):
    def test_home_rejected_until_set_home_seeds_persisted_home(self):
        self.quiet_rest_state()
        self.contract.alive = True
        result = asyncio.run(self.resident.tick())
        names = [row["name"] for row in self.contract.skills]
        # The real child refuses `home` until set_home was accepted; the seed
        # therefore has to come first in the same verified tick.
        self.assertIn("set_home", names)
        self.assertIn("home", names)
        self.assertLess(names.index("set_home"), names.index("home"))
        self.assertTrue(result["home_seed"]["seeded"])
        self.assertEqual(self.contract.set_home_guard_actions, ["observe"])
        self.assertEqual(result["outcome"]["skill"], "home")
        self.assertTrue(result["outcome"]["ok"])
        self.assertTrue(self.resident._home_seeded)

    def test_home_seed_failure_is_never_reported_as_success(self):
        self.quiet_rest_state()
        self.contract.alive = True
        self.contract.fail_set_home = True
        result = asyncio.run(self.resident.tick())
        self.assertFalse(result["home_seed"]["seeded"])
        self.assertEqual(result["home_seed"]["reason"], "call_failed")
        self.assertFalse(self.resident._home_seeded)
        names = [row["name"] for row in self.contract.skills]
        self.assertEqual(names, ["set_home"])   # home is never even attempted
        self.assertNotEqual(result["outcome"].get("skill"), "home")

        for _ in range(4):
            self.clock.advance(30)
            asyncio.run(self.resident.tick())
        names = [row["name"] for row in self.contract.skills]
        self.assertEqual(names.count("set_home"), mr.MAX_HOME_SEED_ATTEMPTS)
        self.assertNotIn("home", names)
        self.assertFalse(self.resident._home_seeded)

    def test_reconnect_loses_sidecar_home_and_reseeds_before_home(self):
        self.resident._home_seeded = True
        self.contract.home = {"x": 0.0, "y": 64.0, "z": 0.0,
                              "dimension": "minecraft:overworld"}
        self.resident._last_decision = self.clock() - 30

        async def sleeper(_seconds):
            return None

        self.resident._sleep = sleeper
        self.contract.alive = False
        self.contract.home = None          # a fresh child process has no home
        ok = asyncio.run(self.resident._reconnect())
        self.assertTrue(ok)
        self.assertFalse(self.resident._home_seeded)
        self.contract.skills.clear()
        self.contract.alive = True
        self.quiet_rest_state()
        result = asyncio.run(self.resident.tick())
        names = [row["name"] for row in self.contract.skills]
        self.assertEqual(names[-2:], ["set_home", "home"])
        self.assertTrue(result["home_seed"]["seeded"])
        self.assertTrue(result["outcome"]["ok"])

    def test_home_place_must_be_canonical_numbers_not_a_name(self):
        self.state_file().parent.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(json.dumps({
            "places": [
                {"kind": "home", "x": "0", "y": "64", "z": "0",
                 "dimension": "minecraft:overworld"},
                {"kind": "home", "x": 0, "y": 64, "z": 0,
                 "dimension": "minecraft:overworld", "name": "Owner"},
                {"kind": "home", "x": 0, "y": 64, "z": 0, "dimension": ""},
            ],
        }), encoding="utf-8")
        reloaded = mr.ResidentState(self.state_file(), clock=self.clock)
        self.assertEqual(len(reloaded.data["places"]), 1)
        kept = reloaded.data["places"][0]
        self.assertEqual((kept["x"], kept["y"], kept["z"]), (0, 64, 0))
        self.assertNotIn("name", kept)
        point = mr._place_row({"kind": "home", "x": 1.5, "y": 64, "z": -2,
                               "dimension": "minecraft:overworld",
                               "name": "Owner"})
        self.assertEqual((point["x"], point["y"], point["z"]), (1.5, 64, -2))
        self.assertNotIn("name", point)
        self.assertEqual(mr._place_row({"kind": "home", "x": "1", "y": 64,
                                        "z": 0}), {})
        self.assertEqual(mr._place_row({"kind": "home", "x": 1, "y": 64,
                                        "z": 0, "dimension": ""}), {})

    def test_home_is_not_seeded_while_far_from_the_persisted_home(self):
        self.quiet_rest_state()
        self.resident.state.data["places"][0].update({"x": 200.0, "z": 200.0})
        self.contract.alive = True
        result = asyncio.run(self.resident.tick())
        self.assertFalse(result["home_seed"]["seeded"])
        self.assertEqual(result["home_seed"]["reason"], "approach_home")
        names = [row["name"] for row in self.contract.skills]
        self.assertEqual(names, ["move"])     # walking home, never re-pointing
        self.assertNotIn("set_home", names)
        self.assertNotIn("home", names)


class FarmContractTests(ContractResidentBase):
    def test_farm_soil_comes_from_loaded_ground_not_the_player_feet(self):
        self.resident.state.data["drives"]["care_urge"] = .5
        self.resident.state.data["next_project_id"] = 2   # slot 2 -> farm
        obs = observation(
            position={"x": 0.0, "y": 64.0, "z": 0.0},
            blocks=[
                {"name": "dirt", "position": {"x": 0, "y": 63, "z": 0}},
                {"name": "dirt", "position": {"x": 1, "y": 63, "z": 0}},
                {"name": "stone", "position": {"x": 0, "y": 62, "z": 0}},
                {"name": "wheat", "position": {"x": -1, "y": 63, "z": 0}},
            ])
        self.resident._ensure_project(obs)
        project = self.resident.state.data["projects"][-1]
        self.assertEqual(project["kind"], "farm")
        self.assertEqual(project["steps"][0]["skill"], "move")
        farm_steps = [row for row in project["steps"] if row["skill"] == "farm"]
        self.assertTrue(farm_steps)
        soil = {(0, 63, 0), (1, 63, 0)}
        for step in farm_steps:
            args = step["args"]
            self.assertIn((args["x"], args["y"], args["z"]), soil)
            self.assertNotEqual(args["y"], 64)   # 64 is the player's feet: air
            self.assertEqual(args["seed"], "wheat_seeds")
        # the move step stands clear of the crop column
        move = project["steps"][0]["args"]
        self.assertNotEqual((move["x"], move["z"]), (0.0, 0.0))

    def test_farm_sequence_satisfies_the_real_child_contract(self):
        self.contract.position = [0.0, 64.0, 0.0]
        self.resident.state.data["drives"]["care_urge"] = .5
        self.resident.state.data["next_project_id"] = 2
        obs = mr.safe_observation(self.contract.observation())
        self.resident._ensure_project(obs)
        project = self.resident.state.data["projects"][-1]
        self.assertEqual(project["kind"], "farm")
        decision = mr.Decision("continue_project")
        outcomes = []
        for _ in range(len(project["steps"])):
            outcomes.append(asyncio.run(self.resident._execute(decision, obs)))
        self.assertEqual(self.contract.rejected, [])
        self.assertTrue(all(row["ok"] for row in outcomes))
        self.assertEqual(len(self.contract.planted), 2)
        self.assertEqual(project["status"], "done")

    def test_opposite_corner_soil_stays_within_the_child_reach_from_stand(self):
        # Regression: with soils at opposite corners of the observed area the
        # old fixed stand picked (2, 64, 0), leaving (-2, 63, -2) at 4.583
        # blocks, which the real adapter refuses (> 4.5).  Every generated
        # `farm` coordinate must be reachable from the single `move` target.
        obs = observation(blocks=[
            {"name": "dirt", "position": {"x": 2, "y": 63, "z": 2}},
            {"name": "dirt", "position": {"x": -2, "y": 63, "z": -2}},
        ])
        steps = self.resident._project_steps(
            "farm", {"x": 0, "y": 64, "z": 0}, obs)
        move = next(row["args"] for row in steps if row["skill"] == "move")
        farm_args = [row["args"] for row in steps if row["skill"] == "farm"]
        self.assertEqual({(args["x"], args["y"], args["z"]) for args in farm_args},
                         {(2, 63, 2), (-2, 63, -2)})
        for args in farm_args:
            distance = math.dist(
                (move["x"], move["y"], move["z"]),
                (args["x"], args["y"], args["z"]))
            self.assertLessEqual(distance, 4.5)

    def test_immature_crop_is_skipped_and_bad_soil_is_not_claimed_as_success(self):
        obs = observation(blocks=[
            {"name": "dirt", "position": {"x": 0, "y": 63, "z": 0}},
            {"name": "wheat", "position": {"x": 0, "y": 64, "z": 0}},
            {"name": "dirt", "position": {"x": 1, "y": 63, "z": 0}},
        ])
        steps = self.resident._project_steps("farm", {"x": 0, "y": 64, "z": 0}, obs)
        farm_cells = {(row["args"]["x"], row["args"]["y"], row["args"]["z"])
                      for row in steps if row["skill"] == "farm"}
        self.assertNotIn((0, 63, 0), farm_cells)   # immature crop above: skip
        self.assertIn((1, 63, 0), farm_cells)

        # No usable soil in the observation -> no farm step at all.  The
        # resident must not invent a y-1 coordinate on gravel/stone: the real
        # child refuses that cell as `not_soil` 100% of the time, so the
        # project would only accumulate predictable failures.
        barren = observation(
            position={"x": 0.0, "y": 64.0, "z": 0.0},
            blocks=[{"name": "gravel", "position": {"x": 0, "y": 63, "z": 0}}])
        self.assertEqual(
            self.resident._project_steps("farm", {"x": 0, "y": 64, "z": 0}, barren),
            [])
        # A direct farm step on that bad soil is still refused by the child
        # contract and never reported as a successful planting.
        self.contract.blocks[(0, 63, 0)] = "gravel"
        self.contract.position = [0.0, 64.0, 0.0]
        direct = {"skill": "farm", "args": {"x": 0, "y": 63, "z": 0,
                                            "seed": "wheat_seeds"}}
        self.resident.state.data["projects"] = [{
            "id": "p1", "kind": "farm", "title": "x", "status": "active",
            "step": 0, "progress": 0,
            "steps": [direct],
            "anchor": {"x": 0, "y": 64, "z": 0}, "created_at": self.clock(),
            "last_acted": 0, "failures": 0,
        }]
        outcome = asyncio.run(self.resident._execute(
            mr.Decision("continue_project"), barren))
        self.assertFalse(outcome["ok"])
        self.assertEqual(self.contract.planted, [])
        self.assertTrue(any(row["reason"] == "not_soil"
                            for row in self.contract.rejected))

    def test_barren_ground_falls_back_to_a_shelter_project(self):
        # Regression: `_farm_soil` used to invent a y-1 block "unknown" cell on
        # stone/gravel/air, so `_project_steps("farm")` was never empty and the
        # documented shelter fallback in `_ensure_project` was dead code.
        for blocks in (
            [{"name": "stone", "position": {"x": 0, "y": 63, "z": 0}}],
            [{"name": "gravel", "position": {"x": 0, "y": 63, "z": 0}}],
            [],
        ):
            with self.subTest(ground=[row["name"] for row in blocks]):
                self.resident.state.data["projects"] = []
                self.resident.state.data["drives"]["care_urge"] = .5
                self.resident.state.data["next_project_id"] = 2   # slot 2 -> farm
                obs = observation(
                    position={"x": 0.0, "y": 64.0, "z": 0.0}, blocks=blocks)
                self.resident._ensure_project(obs)
                project = self.resident.state.data["projects"][-1]
                self.assertEqual(project["kind"], "shelter")
                self.assertTrue(project["steps"])
                self.assertFalse(any(row["skill"] == "farm"
                                     for row in project["steps"]))
                for row in project["steps"]:
                    self.assertTrue(mr.validate_skill(row["skill"], row["args"]),
                                    row)


class OwnerIdentityTests(ResidentBase):
    def test_owner_role_comes_only_from_the_exact_owner_human_uuid(self):
        self.config["minecraft"]["owner_human_uuid"] = OWNER_HUMAN
        obs = observation(players=[ACTOR, OWNER_HUMAN, OTHER_1])
        for entity in obs["nearby_entities"]:
            entity["name"] = "Owner"      # a nickname never grants the role
        self.resident._update_social(obs)
        roles = {row["uuid"]: row for row in self.resident.state.data["players"]}
        self.assertEqual(roles[ACTOR]["role"], "self")
        self.assertEqual(roles[OWNER_HUMAN]["role"], "owner")
        self.assertEqual(roles[OTHER_1]["role"], "player")
        self.assertGreaterEqual(roles[OWNER_HUMAN]["trust"], .7)
        self.assertLess(roles[OTHER_1]["trust"], .5)
        context = self.resident._context(obs)
        self.assertTrue(context["self_model"]["social_self"]["owner_present"])
        encoded = json.dumps(context, ensure_ascii=False)
        self.assertNotIn(OWNER_HUMAN, encoded)
        self.assertNotIn(OWNER_UID, encoded)
        self.assertNotIn("qq", encoded.lower())
        self.assertTrue(all("uuid" not in row for row in context["social_models"]))

        # Removing the owner field demotes the very same player to "player".
        self.config["minecraft"]["owner_human_uuid"] = ""
        self.resident._update_social(obs)
        roles = {row["uuid"]: row for row in self.resident.state.data["players"]}
        self.assertEqual(roles[OWNER_HUMAN]["role"], "player")
        context = self.resident._context(obs)
        self.assertFalse(context["self_model"]["social_self"]["owner_present"])

    def test_owner_field_equal_to_the_bot_actor_is_rejected(self):
        self.config["minecraft"]["owner_human_uuid"] = ACTOR
        self.assertEqual(self.resident._owner_human_uuid(), "")
        obs = observation(players=[ACTOR])
        self.resident._update_social(obs)
        self.assertEqual(self.resident.state.data["players"][0]["role"], "self")
        self.assertFalse(self.resident._context(obs)["self_model"]["social_self"]
                         ["owner_present"])

    def test_cross_channel_mapping_never_grants_owner_role_or_duplicates(self):
        self.config["minecraft"]["owner_human_uuid"] = OWNER_HUMAN
        self.config["minecraft"]["owner_mappings"] = {
            OTHER_1: OWNER_UID, OWNER_UID: OTHER_2}
        obs = observation(players=[OTHER_1, OTHER_2])
        self.resident._update_social(obs)
        rows = {row["uuid"]: row for row in self.resident.state.data["players"]}
        # the same QQ UID points at two UUIDs -> conflicting, both unlinked
        self.assertFalse(rows[OTHER_1]["explicit_link"])
        self.assertFalse(rows[OTHER_2]["explicit_link"])
        self.assertEqual(rows[OTHER_1]["role"], "player")
        raw = json.dumps(self.resident.state.data, ensure_ascii=False)
        self.assertNotIn(OWNER_UID, raw)

        self.config["minecraft"]["owner_mappings"] = {OTHER_1: OWNER_UID}
        self.resident._update_social(obs)
        rows = {row["uuid"]: row for row in self.resident.state.data["players"]}
        self.assertTrue(rows[OTHER_1]["explicit_link"])
        self.assertEqual(rows[OTHER_1]["role"], "player")   # link != owner
        self.assertEqual(rows[OTHER_2]["role"], "player")

    def test_owner_role_and_trust_survive_restart(self):
        self.config["minecraft"]["owner_human_uuid"] = OWNER_HUMAN
        self.resident._update_social(observation(players=[OWNER_HUMAN]))
        self.resident.state.save()
        reloaded = mr.ResidentState(self.state_file(), clock=self.clock)
        row = reloaded.data["players"][0]
        self.assertEqual(row["uuid"], OWNER_HUMAN)
        self.assertEqual(row["role"], "owner")
        self.assertGreaterEqual(row["trust"], .7)

    def test_resident_uses_the_single_canonical_owner_human_contract(self):
        # G8 regression: the resident must agree with minecraft_policy on every
        # owner_mappings shape instead of applying a weaker rule of its own.
        self.config["minecraft"]["owner_human_uuid"] = OWNER_HUMAN
        cases = [
            ({}, True),
            ({OWNER_UID: OWNER_HUMAN}, True),                      # explicit link agrees
            ({OWNER_HUMAN: OWNER_UID}, True),                      # key order
            ({"987654321": OWNER_HUMAN}, False),                   # human -> other UID
            ({OWNER_UID: OTHER_1}, False),                         # owner UID -> other UUID
            ({OWNER_HUMAN: OWNER_UID,
              OWNER_HUMAN.upper(): "987654321"}, False),           # one avatar, two UIDs
            ({OWNER_HUMAN: OWNER_UID,
              OTHER_1: OWNER_UID}, False),                         # one UID, two UUIDs
        ]
        obs = observation(players=[OWNER_HUMAN])
        for mappings, verified in cases:
            with self.subTest(mappings=mappings):
                self.config["minecraft"]["owner_mappings"] = mappings
                expected = OWNER_HUMAN if verified else ""
                self.assertEqual(owner_human_identity(self.config), expected)
                self.assertEqual(self.resident._owner_human_uuid(), expected)
                self.resident.state.data["players"] = []
                self.resident._update_social(obs)
                row = self.resident.state.data["players"][0]
                self.assertEqual(row["role"], "owner" if verified else "player")
                self.assertEqual(
                    self.resident._context(obs)["self_model"]["social_self"]
                    ["owner_present"], verified)

    def test_owner_mappings_never_grant_the_owner_role_on_their_own(self):
        self.config["minecraft"]["owner_human_uuid"] = ""
        self.config["minecraft"]["owner_mappings"] = {OWNER_UID: OWNER_HUMAN}
        obs = observation(players=[ACTOR, OWNER_HUMAN])
        self.assertEqual(self.resident._owner_human_uuid(), "")
        self.resident._update_social(obs)
        rows = {row["uuid"]: row for row in self.resident.state.data["players"]}
        self.assertEqual(rows[OWNER_HUMAN]["role"], "player")
        self.assertTrue(rows[OWNER_HUMAN]["explicit_link"])   # a link, not a role
        self.assertEqual(rows[ACTOR]["role"], "self")
        self.assertFalse(self.resident._context(obs)["self_model"]["social_self"]
                         ["owner_present"])


class SuggestionTests(ResidentBase):
    def setUp(self):
        super().setUp()
        self.config["minecraft"]["owner_human_uuid"] = OWNER_HUMAN
        self.resident._update_social(
            observation(players=[OWNER_HUMAN, OTHER_1]))
        self.resident.state.data["drives"].update({"energy": .8})

    def test_dispositions_are_bounded_and_never_touch_guard_or_child(self):
        self.config["minecraft"]["owner_human_uuid"] = OWNER_HUMAN
        owner = self.resident.consider_suggestion(OWNER_HUMAN, "过来一起待一会儿")
        self.assertEqual(owner["status"], "accepted")
        self.assertEqual(owner["hint"], "socialize")

        other = self.resident.consider_suggestion(OTHER_1, "去那边看看")
        self.assertEqual(other["status"], "accepted")
        self.assertEqual(other["hint"], "explore")

        vague = self.resident.consider_suggestion(OTHER_1, "嗯……")
        self.assertEqual(vague["status"], "negotiating")
        self.assertTrue(vague["counter"])

        self.resident._idle_cooldown = 2
        busy = self.resident.consider_suggestion(OTHER_1, "过来一起待一会儿")
        self.assertEqual(busy["status"], "deferred")
        self.assertGreater(busy["due_at"], 0)
        self.resident._idle_cooldown = 0

        refused = self.resident.consider_suggestion(OTHER_1, "/op @a")
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["reason"], "not_allowed")
        self.assertEqual(self.resident.consider_suggestion(
            OTHER_1, "帮我去攻击他")["status"], "refused")
        self.assertEqual(self.resident.consider_suggestion(
            OTHER_1, "打他")["status"], "refused")
        self.assertEqual(self.resident.consider_suggestion(
            OTHER_1, "跟我来 /tp 1 2 3")["status"], "refused")
        self.assertEqual(self.resident.consider_suggestion(
            OTHER_2, "过来")["reason"], "unknown_player")
        self.assertEqual(self.resident.consider_suggestion(
            ACTOR, "过来")["reason"], "self")
        self.config["minecraft"]["enabled"] = False
        self.assertEqual(self.resident.consider_suggestion(
            OTHER_1, "过来")["reason"], "disabled")
        self.config["minecraft"]["enabled"] = True

        # A suggestion is data, never a skill or an authorization grant.
        self.assertEqual(self.guard.actions, [])
        self.assertEqual(self.sidecar.calls, [])
        for row in self.resident.state.data["suggestions"]:
            self.assertNotIn("skill", row)
            self.assertNotIn("args", row)
            self.assertNotIn(OWNER_UID, json.dumps(row, ensure_ascii=False))
        self.assertLessEqual(len(self.resident.state.data["suggestions"]),
                             mr.MAX_SUGGESTIONS)

    def test_accepted_suggestion_biases_the_decision_but_not_authority(self):
        self.resident.state.data["drives"].update(
            {"energy": .35, "boredom": .2, "loneliness": .2,
             "creative_tension": .3, "hoarding_urge": .2,
             "care_urge": .2, "wanderlust": .3})
        before = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(before.intent, "satisfy_drive")
        accepted = self.resident.consider_suggestion(OTHER_1, "去那边看看")
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(self.resident._suggestion_hints(), [("explore", .45)])
        after = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(after.intent, "explore")
        # None of that produced write/PvP capability.
        self.assertNotIn("attack", mr.SKILLS["explore"])
        self.assertEqual(self.sidecar.calls, [])

    def test_suggestions_are_persisted_without_qq_ids_or_commands(self):
        self.resident.consider_suggestion(OTHER_1, "一起去看看那边")
        self.resident.state.save()
        raw = self.state_file().read_text(encoding="utf-8")
        self.assertNotIn(OWNER_UID, raw)
        self.assertNotIn("/op", raw)
        reloaded = mr.ResidentState(self.state_file(), clock=self.clock)
        self.assertEqual(reloaded.data["suggestions"][0]["status"], "accepted")
        self.assertEqual(reloaded.data["suggestions"][0]["hint"], "socialize")
        self.assertEqual(reloaded.data["next_suggestion_id"], 2)


class EnvironmentTests(ResidentBase):
    def test_observation_environment_fields_are_schema_bounded(self):
        row = observation(
            time_of_day=13000, weather="rain", light=4, roof=False,
            events=[{"type": "weather_change", "at": 5},
                    {"type": "player_near", "at": 3}])
        safe = mr.safe_observation(row)
        self.assertEqual(safe["time_of_day"], 13000)
        self.assertEqual(safe["weather"], "rain")
        self.assertEqual(safe["light"], 4)
        self.assertIs(safe["roof"], False)
        self.assertEqual(len(safe["events"]), 2)
        self.assertNotIn("raw", json.dumps(safe))

        bad = mr.safe_observation(observation(
            time_of_day=999999, weather="snow", light=99, roof="true",
            events=[{"type": "Bad/Type", "at": 1}] * 20))
        for key in ("time_of_day", "weather", "light", "roof", "events"):
            self.assertNotIn(key, bad)
        huge = observation(events=[{"type": f"ev_{i}", "at": i}
                                   for i in range(30)])
        self.assertEqual(len(mr.safe_observation(huge)["events"]),
                         mr.MAX_OBSERVATION_EVENTS)

    def test_game_clock_and_environment_use_in_game_time(self):
        noon = mr.game_clock({"time_of_day": 6000})
        self.assertEqual(noon["phase"], "day")
        self.assertEqual(noon["hour"], 12.0)
        night = mr.game_clock({"time_of_day": 14000})
        self.assertEqual(night["phase"], "night")
        self.assertEqual(mr.game_clock({"time_of_day": 999999}), None)

        exposed = mr.environment({"weather": "thunder", "roof": False,
                                  "light": 4, "time_of_day": 14000})
        sheltered = mr.environment({"weather": "thunder", "roof": True,
                                    "light": 12, "time_of_day": 6000})
        clear = mr.environment({"weather": "clear", "roof": False,
                                "time_of_day": 6000})
        self.assertLess(exposed["comfort"], clear["comfort"])
        self.assertGreater(sheltered["comfort"], exposed["comfort"])
        self.assertTrue(exposed["storms"] and exposed["dark"])
        self.assertFalse(sheltered["dark"])
        self.assertTrue(sheltered["sheltered"])

    def test_weather_and_roof_change_appraisal_and_residue(self):
        exposed = observation(weather="thunder", roof=False, light=4,
                              time_of_day=14000)
        sheltered = observation(weather="thunder", roof=True, light=12,
                                time_of_day=6000)
        clear = observation(time_of_day=6000)
        self.assertLess(mr.Appraiser().appraise(exposed, self.resident.state)
                        .goal_congruence,
                        mr.Appraiser().appraise(clear, self.resident.state)
                        .goal_congruence)
        self.assertGreater(mr.Appraiser().appraise(sheltered, self.resident.state)
                           .goal_congruence,
                           mr.Appraiser().appraise(exposed, self.resident.state)
                           .goal_congruence)

        fear_before = self.resident.state.data["affect_residue"]["fear"]
        asyncio.run(self.resident._appraise(exposed, force=True))
        self.assertGreater(self.resident.state.data["affect_residue"]["fear"],
                           fear_before)
        warmth_before = self.resident.state.data["affect_residue"]["warmth"]
        asyncio.run(self.resident._appraise(sheltered, force=True))
        self.assertGreater(self.resident.state.data["affect_residue"]["warmth"],
                           warmth_before)

    def test_return_home_habit_reads_game_time_not_the_wall_clock(self):
        self.resident.state.data["habits"]["return_home"] = {
            "count": 2, "last_at": self.clock()}
        daytime = observation(time_of_day=1000)     # 07:00 in game
        with mock.patch.object(mr.time, "localtime",
                               return_value=SimpleNamespace(tm_hour=23)):
            candidates = self.resident._propose(daytime)
        self.assertFalse(any(row.intent == "rest" and row.source == "habit"
                             for row in candidates))

        night = observation(time_of_day=14000)      # 20:00 in game
        with mock.patch.object(mr.time, "localtime",
                               return_value=SimpleNamespace(tm_hour=7)):
            candidates = self.resident._propose(night)
        self.assertTrue(any(row.intent == "rest" and row.source == "habit"
                            for row in candidates))

    def test_successful_actions_form_semantic_habits_across_threshold(self):
        # No habit count is seeded here: the resident has to learn these keys
        # from the same _success path an accepted skill runs through.
        self.resident.state.data["habits"].clear()
        self.resident.state.data["projects"] = [{
            "id": "p1", "kind": "farm", "title": "照看那块田",
            "goal": "test", "status": "active", "step": 0, "progress": 0,
            "steps": [{"skill": "move", "args": {
                "x": 0.0, "y": 64.0, "z": 0.0, "radius": 1.0}}
                for _ in range(3)],
            "anchor": {"x": 0, "y": 64, "z": 0}, "created_at": self.clock(),
            "last_acted": 0, "failures": 0,
        }]
        self.resident.state.data["players"] = [{
            "uuid": OTHER_1, "role": "player", "familiarity": .7,
            "trust": .6, "closeness": .5, "boundary": False,
            "last_seen": self.clock(), "shared_history_count": 2,
            "explicit_link": False,
        }]
        familiar = observation(players=[OTHER_1])

        def succeed():
            self.resident._success("rest", "home", {},
                                   {"project_id": "", "promise_id": ""}, familiar)
            self.resident._success("continue_project", "farm", {},
                                   {"project_id": "p1", "promise_id": ""}, familiar)
            self.resident._success("socialize", "follow", {"target_uuid": OTHER_1},
                                   {"project_id": "", "promise_id": ""}, familiar)

        def habit_candidates(observation_row):
            return [(row.intent, row.project_id)
                    for row in self.resident._propose(observation_row)
                    if row.source == "habit"]

        night = observation(players=[OTHER_1], time_of_day=14000)   # 20:00 in game
        succeed()
        habits = self.resident.state.data["habits"]
        self.assertEqual(habits["return_home"]["count"], 1)
        self.assertEqual(habits["tend_farm"]["count"], 1)
        self.assertEqual(habits["greet_known"]["count"], 1)
        self.assertEqual(habit_candidates(night), [])

        succeed()
        self.assertEqual(habits["return_home"]["count"], 2)
        self.assertEqual(habits["tend_farm"]["count"], 2)
        self.assertEqual(habits["greet_known"]["count"], 2)
        learned = habit_candidates(night)
        self.assertIn(("rest", ""), learned)
        self.assertIn(("continue_project", "p1"), learned)
        self.assertIn(("socialize", ""), learned)
        # The same context guards still apply: daytime removes the late-night
        # habit pull even though the count has crossed the threshold.
        daytime = observation(players=[OTHER_1], time_of_day=1000)  # 07:00 in game
        self.assertFalse(any(intent == "rest"
                             for intent, _project in habit_candidates(daytime)))

    def test_rain_without_roof_pulls_indoors_and_context_is_safe(self):
        outside = observation(weather="rain", roof=False)
        sheltered = observation(weather="rain", roof=True)
        self.assertTrue(any(row.intent == "rest" and row.source == "weather"
                            for row in self.resident._propose(outside)))
        self.assertFalse(any(row.intent == "rest" and row.source == "weather"
                             for row in self.resident._propose(sheltered)))
        context = self.resident._context(outside)
        facts = context["world_facts"]["environment"]
        self.assertEqual(facts["weather"], "rain")
        self.assertIs(facts["roof"], False)
        self.assertIn("recent_events", context["world_facts"])
        encoded = json.dumps(context, ensure_ascii=False)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(OWNER_UID, encoded)


class PvpAttackTests(ContractResidentBase):
    def _threat_entities(self, player_uuid):
        return [{"type": "player", "name": "Someone", "uuid": player_uuid,
                 "position": {"x": 1.0, "y": 64.0, "z": 1.0}},
                {"type": "mob", "name": "zombie", "uuid": "",
                 "position": {"x": 2.0, "y": 64.0, "z": 0.0}}]

    def test_only_the_exact_configured_target_can_be_attacked(self):
        self.config["minecraft"].update({"pvp_enabled": True,
                                         "write_enabled": True,
                                         "pvp_target_uuids": [OTHER_1]})
        self.contract.entities = self._threat_entities(OTHER_1)
        obs = mr.safe_observation(self.contract.observation())
        decision = asyncio.run(self.resident.decide(obs))
        self.assertEqual(decision.intent, "recover")
        outcome = asyncio.run(self.resident._execute(decision, obs))
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["skill"], "attack")
        self.assertEqual(self.contract.attacks, [OTHER_1])
        self.assertIn(("pvp", OTHER_1), self.guard.actions)
        self.assertNotIn("attack", mr.SKILLS["continue_project"])
        self.assertIn("attack", mr.SKILLS["recover"])

    def test_unknown_player_and_actor_are_never_spontaneous_targets(self):
        self.config["minecraft"].update({"pvp_enabled": True,
                                         "write_enabled": True,
                                         "pvp_target_uuids": [OTHER_1]})
        for uuid in (OTHER_2, ACTOR):
            self.contract.entities = self._threat_entities(uuid)
            obs = mr.safe_observation(self.contract.observation())
            decision = asyncio.run(self.resident.decide(obs))
            outcome = asyncio.run(self.resident._execute(decision, obs))
            self.assertNotEqual(outcome.get("skill"), "attack")
            self.assertEqual(self.contract.attacks, [])
        self.assertNotIn("pvp", [action for action, _t in self.guard.actions])

    def test_strict_flags_are_required_and_double_guarded(self):
        self.config["minecraft"].update({"pvp_enabled": True,
                                         "write_enabled": True,
                                         "pvp_target_uuids": [OTHER_1]})
        # pvp capability off -> preflight denial, no guard/child call
        self.config["minecraft"]["pvp_enabled"] = False
        result = asyncio.run(self.resident._invoke_skill(
            "attack", {"target_uuid": OTHER_1}))
        self.assertFalse(result["ok"])
        self.assertTrue(result["preflight"])
        self.assertEqual(result["reason"], "pvp_denied")
        # write capability off also closes PvP (strict literal True only)
        self.config["minecraft"].update({"pvp_enabled": True,
                                         "write_enabled": False})
        result = asyncio.run(self.resident._invoke_skill(
            "attack", {"target_uuid": OTHER_1}))
        self.assertEqual(result["reason"], "pvp_denied")
        self.assertNotIn("pvp", [action for action, _t in self.guard.actions])
        # an exact permit path still refuses a non-configured target
        self.config["minecraft"].update({"write_enabled": True,
                                         "pvp_enabled": True})
        self.guard.deny.add("pvp")
        result = asyncio.run(self.resident._invoke_skill(
            "attack", {"target_uuid": OTHER_2}))
        self.assertEqual(result["reason"], "pvp_denied")
        self.assertEqual(self.contract.attacks, [])


class SharedMemoryRestartBudgetTests(ContractResidentBase):
    def test_state_memory_and_owner_role_survive_restart(self):
        self.config["minecraft"].update({"pvp_target_uuids": []})
        self.contract.players = [OWNER_HUMAN, OTHER_1]
        self.resident._last_decision = self.clock()
        for _ in range(8):
            self.clock.advance(30)
            asyncio.run(self.resident.tick())
        self.resident.consider_suggestion(OWNER_HUMAN, "过来一起待一会儿")
        self.resident.add_promise("天亮后看看那块田",
                                  steps=[{"skill": "eat", "args": {}}])
        self.resident.state.save()
        episodes = len(self.longmem.episodes)
        projects = len(self.resident.state.data["projects"])

        restarted = self.make_resident(sidecar=self.contract)
        self.assertEqual(len(self.longmem.episodes), episodes)
        self.assertEqual(len(restarted.state.data["projects"]), projects)
        self.assertTrue(restarted.state.data["promises"])
        self.assertEqual(restarted.state.data["suggestions"][0]["status"],
                         "accepted")
        roles = {row["uuid"]: row["role"]
                 for row in restarted.state.data["players"]}
        self.assertEqual(roles.get(OWNER_HUMAN), "owner")
        self.assertEqual(roles.get(OTHER_1), "player")
        self.assertTrue(self.longmem.episodes)
        self.assertTrue(all(row.get("source") == "minecraft"
                            for row in self.longmem.episodes))

    def test_failed_remote_decisions_stay_reserved_across_restart(self):
        self.config["minecraft"].update({
            "decision_enabled": True,
            "decision_remote_enabled": True,
            "decision_cost_usd_per_call": 0.02,
            "decision_budget_usd_per_day": 0.05,
            "max_decisions_per_day": 5,
        })

        async def boom(config, state, questions, *, timeout=None):
            raise TimeoutError("synthetic")

        with mock.patch.object(mr.decider, "decide", boom):
            first = asyncio.run(self.resident.decide(self.obs))
        self.assertNotEqual(first.source, "jev")
        self.assertEqual(self.resident.budget.data["attempts"], 1)
        with mock.patch.object(mr.decider, "decide",
                               mock.AsyncMock(return_value={
                                   "intent": {"choice": "rest"},
                                   "confidence": {"score": .5}})):
            second = asyncio.run(self.resident.decide(self.obs))
        self.assertEqual(second.source, "jev")   # second call fits the budget
        self.assertEqual(self.resident.budget.data["attempts"], 2)
        persisted = json.loads(self.budget_file().read_text(encoding="utf-8"))
        self.assertEqual(persisted["attempts"], 2)

        restarted = self.make_resident(sidecar=self.contract)
        self.assertEqual(restarted.budget.data["attempts"], 2)
        with mock.patch.object(mr.decider, "decide",
                               mock.AsyncMock(return_value={
                                   "intent": {"choice": "rest"},
                                   "confidence": {"score": .5}})) as fake:
            third = asyncio.run(restarted.decide(self.obs))
        fake.assert_not_awaited()      # 3 * .02 > .05: no refund, no call
        self.assertEqual(restarted.budget.data["attempts"], 2)
        self.assertNotEqual(third.source, "jev")


if __name__ == "__main__":
    unittest.main()
