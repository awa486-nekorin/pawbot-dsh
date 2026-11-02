"""Autonomous resident loop sharing PawBot's persona, affect and long-memory stores.

This module is deliberately source/offline only.  It never opens a world, never
reads a credential and never invents a game action: every side effect goes
through :mod:`pawbot.minecraft_guard` (authenticated loopback attestation) and
the pre-implemented skill registry in the Node sidecar.

Design notes
------------
* A bounded global workspace (3-5 items) is rebuilt from perception, drives,
  affect, projects, promises, habits and social presence.  Environmental
  stimuli compete for attention; they never call a skill directly.
* ``ImpulseEngine`` is the fast, deterministic system.  ``JevAdapter`` is the
  slow, optional intent-only model.  ``DeterministicPlanner`` is the always
  available fallback.  ``Arbiter`` resolves conflict, including hesitation.
* All high-level choices are *intents* (see :data:`INTENTS`).  Skills and their
  arguments come only from an allow-listed schema; model output can never name
  a skill or supply executable code.
* Remote decisions are off unless the operator explicitly enables them *and*
  configures an upper-bound per-call cost.  A call attempt and its reserved
  cost are persisted before the request, and are not refunded on error,
  timeout or invalid output.
* No private thought, raw QQ UID or world-save path is persisted, logged or
  placed in the model context.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import decider, tasks
from .log import log
from .minecraft_guard import GuardBusy, GuardClient, GuardDenied, GuardSettings
from .minecraft_policy import (
    WorldPermit,
    allowed_pvp_targets,
    authorize_action,
    explicit_mapping_pairs,
    normalize_player_uuid,
    owner_human_identity,
    owner_identity_matches,
)
from .minecraft_sidecar import MinecraftSidecar, SidecarError

# Keep these tuples stable/compatible with the earlier skeleton.
INTENTS = ("continue_project", "fulfill_promise", "satisfy_drive", "socialize",
           "explore", "rest", "idle")
# Survival reflection is internal; a remote decision may never select it.
INTERNAL_INTENTS = INTENTS + ("recover",)

SKILLS = {
    "continue_project": {"move", "dig", "collect", "craft", "place", "farm", "home", "eat", "explore"},
    "fulfill_promise": {"move", "place", "craft", "chat", "follow", "home"},
    "satisfy_drive": {"move", "collect", "craft", "place", "farm", "eat", "home", "explore", "follow"},
    "socialize": {"chat", "follow", "move"},
    "explore": {"move", "explore", "home"},
    "rest": {"home", "eat"},
    "recover": {"eat", "respawn", "home", "attack"},
    "idle": set(),
}

WORKSPACE_CAPACITY = 4
WORKSPACE_MIN = 3
WORKSPACE_MAX = 5
SKILL_TIMEOUT_SEC = 30
OBSERVE_TIMEOUT_SEC = 8
MAX_BACKUP_INTERVAL_MIN = 30
MAX_CONNECT_TIMEOUT_SEC = 30
MAX_SKILLS_MEMORY = 64
MAX_JOURNAL = 120
MAX_PLAYERS = 64
MAX_PROJECTS = 8
MAX_PROMISES = 32
MAX_PLACES = 32
MAX_ACTIONS = 24
MAX_DIARY_BYTES = 256 * 1024
# Observation extensions supplied by the Node sidecar (weather/time/light/roof
# and a bounded safe-event list).  Everything below is schema-bounded: an
# out-of-range or unknown value is dropped, never echoed into state/model.
MAX_OBSERVATION_EVENTS = 8
MAX_SUGGESTIONS = 8
HOME_SEED_TOLERANCE = 3.0      # seed only while standing at the persisted home
HOME_RETURN_RADIUS = 12.0      # the sidecar's `home` skill is a <=16 block goto
# A persisted home beyond this range is not walked to: the `home` skill is a
# short goto, so a long approach would burn the whole skill budget and end in a
# timeout. Staying honestly unseeded is better than an abortive march.
HOME_APPROACH_MAX_DISTANCE = 48.0
MAX_HOME_SEED_ATTEMPTS = 3     # then stay honestly unseeded until reconnect
HOME_SEED_RETRY_COOLDOWN = 600.0  # ...or until this many seconds have passed
_SOIL_BLOCKS = frozenset({"farmland", "dirt", "grass_block"})
# The Node adapter refuses a `farm` soil further than 4.5 blocks from the bot
# (adapter.js `loadedBlock(p, 4.5)`); generated steps must stay inside it.
FARM_SOIL_REACH = 4.5
_GAME_TICKS_PER_HOUR = 1000.0
_GAME_DAY_START_HOUR = 6.0
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SUGGESTION_STATUSES = ("accepted", "deferred", "refused", "negotiating")
_SUGGESTION_HINTS = (
    (("过来", "来这", "跟我", "一起", "陪我", "come", "follow", "here"), "socialize"),
    (("看看", "探索", "那边", "远处", "explore", "look", "around"), "explore"),
    (("休息", "睡", "歇", "回去", "回家", "rest", "sleep", "home"), "rest"),
    (("帮忙", "整理", "收拾", "做点", "build", "help", "farm", "种"), "satisfy_drive"),
)
_FORBIDDEN_REQUEST_RE = re.compile(
    r"(?i)(?<![a-z])(attack|pvp|kill|op|give|tp|setblock|fill|summon|execute|"
    r"command|sudo|gamemode|effect)(?![a-z])")
_FORBIDDEN_REQUEST_WORDS = ("攻击", "杀掉", "杀了", "杀死", "杀人", "打他", "打她",
                            "打你", "砍他", "砍她")

_HOSTILE_NAMES = frozenset({
    "zombie", "husk", "drowned", "skeleton", "stray", "creeper", "spider",
    "cave_spider", "enderman", "witch", "pillager", "vindicator", "evoker",
    "ravager", "phantom", "slime", "magma_cube", "blaze", "ghast", "piglin",
    "piglin_brute", "hoglin", "zoglin", "warden", "vex", "silverfish",
    "endermite", "guardian", "elder_guardian", "shulker", "breeze",
})
_ITEM_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WORLD_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_DIMENSION_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[^\s/\"']+/)+[^\s/\"']+")
_LONG_NUMBER_RE = re.compile(r"(?<![0-9])(?:[0-9]{5,})(?![0-9])")
_URL_RE = re.compile(r"(?i)https?://\S+")


def _guard_endpoint(value) -> str:
    """Normalize an accepted loopback guard URL to the exact permit endpoint."""
    raw = str(value or "").strip()
    if not raw:
        return "http://127.0.0.1:6198/v1/permit"
    if raw.endswith("/v1/permit"):
        return raw
    if raw.endswith("/"):
        raw = raw[:-1]
    return raw + "/v1/permit"


# ---------------------------------------------------------------------------
# safe primitives
# ---------------------------------------------------------------------------
def _cfg(config, key, default=None):
    """Read a dotted ``minecraft.*`` config value without trusting its type."""
    try:
        value = config.get("minecraft." + key, default)
    except TypeError:
        try:
            value = config.get(key)
        except Exception:
            value = default
    except Exception:
        value = default
    return default if value is None else value


def _one_line(value, limit: int = 240) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:max(0, int(limit))]


def _safe_text(value, limit: int = 160) -> str:
    """Bounded, de-identified text for state/log/model context.

    This is not a general redactor; it is a last line of defence so long
    numeric identifiers (QQ UIDs), Windows/UNC paths and URLs cannot leak into
    persisted resident state or a model prompt.
    """
    text = _one_line(value, limit * 2)
    text = _PATH_RE.sub("[path]", text)
    text = _POSIX_PATH_RE.sub("[path]", text)
    text = _URL_RE.sub("[url]", text)
    text = _LONG_NUMBER_RE.sub("[id]", text)
    return text[:limit]


def _finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


def _num(value, default: float, *, low=None, high=None) -> float:
    number = _finite(value)
    if number is None:
        number = float(default)
    if low is not None:
        number = max(float(low), number)
    if high is not None:
        number = min(float(high), number)
    return number


def _int(value, default: int, *, low=None, high=None) -> int:
    if type(value) is not int:
        value = int(default)
    if low is not None:
        value = max(int(low), value)
    if high is not None:
        value = min(int(high), value)
    return value


def _strict_true(value) -> bool:
    return value is True


def _valid_uuid(value) -> str:
    return normalize_player_uuid(value)


def _distance(left: dict, right: dict):
    try:
        return math.dist(
            (float(left["x"]), float(left["y"]), float(left["z"])),
            (float(right["x"]), float(right["y"]), float(right["z"])),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _position(value):
    if not isinstance(value, dict):
        return None
    x, y, z = (_finite(value.get(key)) for key in ("x", "y", "z"))
    if x is None or y is None or z is None:
        return None
    if not (-64 <= y <= 319) or abs(x) > 29999984 or abs(z) > 29999984:
        return None
    return {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3)}


def _canonical_coord(value):
    """One canonical numeric coordinate, or ``None``.

    Numeric only: booleans, strings, NaN/infinity and out-of-world values are
    rejected so a persisted place can never be built from a nickname, a path or
    a JSON string that merely looks like a number.
    """
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if abs(value) > 29999984:
        return None
    number = round(float(value), 3)
    return 0.0 if number == 0 else number


def _canonical_point(value):
    """A bounded ``{x, y, z}`` point from numeric fields only."""
    if not isinstance(value, dict):
        return None
    x = _canonical_coord(value.get("x"))
    y = _canonical_coord(value.get("y"))
    z = _canonical_coord(value.get("z"))
    if x is None or y is None or z is None or not -64 <= y <= 319:
        return None
    return {"x": x, "y": y, "z": z}


def _coord(value, *, integer=True):
    number = _finite(value)
    if number is None or abs(number) > 29999984:
        return None
    if integer:
        if not number.is_integer():
            return None
        return int(number)
    return number


def _y_coord(value):
    number = _finite(value)
    if number is None or not -64 <= number <= 319:
        return None
    if not number.is_integer():
        return None
    return int(number)


def _item_name(value) -> str:
    text = str(value or "").strip().lower()
    return text if _ITEM_RE.fullmatch(text) else ""


def _safe_dimension(value) -> str:
    text = str(value or "").strip().lower()
    return text if _DIMENSION_RE.fullmatch(text) else ""


def _safe_uuid(value) -> str:
    return normalize_player_uuid(value)


# ---------------------------------------------------------------------------
# observation contract (matches app/minecraft-sidecar/src/adapter.js)
# ---------------------------------------------------------------------------
def safe_observation(row: dict) -> dict:
    """Validate and bound the real sidecar observe payload.

    Only the fields the sidecar actually emits are trusted.  Unknown keys are
    ignored rather than persisted.  Player display names are intentionally
    discarded: identity is keyed by exact UUID only, never by nickname.
    """
    if not isinstance(row, dict):
        raise SidecarError("invalid_observation")
    health = _finite(row.get("health"))
    food = _finite(row.get("food"))
    if health is None or food is None:
        raise SidecarError("invalid_observation")
    out = {
        "health": _clamp(health, 0.0, 2048.0),
        "food": _clamp(food, 0.0, 20.0),
        "position": _position(row.get("position")),
        "dimension": _safe_dimension(row.get("dimension")),
        "inventory": [],
        "nearby_entities": [],
        "nearby_blocks": [],
    }
    inventory = row.get("inventory")
    if isinstance(inventory, list):
        for item in inventory[:MAX_SKILLS_MEMORY]:
            if not isinstance(item, dict):
                continue
            name = _item_name(item.get("name"))
            count = item.get("count")
            if not name or type(count) is not int:
                continue
            out["inventory"].append({"name": name, "count": int(_clamp(count, 0, 64))})
    entities = row.get("nearby_entities")
    if isinstance(entities, list):
        for entity in entities[:32]:
            if not isinstance(entity, dict):
                continue
            kind = _one_line(entity.get("type"), 32).lower()
            name = _item_name(entity.get("name"))
            position = _position(entity.get("position"))
            if not kind or not name or position is None:
                continue
            record = {"type": kind, "name": name, "position": position}
            uuid = _safe_uuid(entity.get("uuid"))
            if uuid:
                record["uuid"] = uuid
            if out["position"] is not None:
                distance = _distance(out["position"], position)
                if distance is not None:
                    record["distance"] = round(distance, 2)
            out["nearby_entities"].append(record)
    blocks = row.get("nearby_blocks")
    if isinstance(blocks, list):
        for block in blocks[:32]:
            if not isinstance(block, dict):
                continue
            name = _item_name(block.get("name"))
            position = _position(block.get("position"))
            if not name or position is None:
                continue
            record = {"name": name, "position": position}
            if out["position"] is not None:
                distance = _distance(out["position"], position)
                if distance is not None:
                    record["distance"] = round(distance, 2)
            out["nearby_blocks"].append(record)
    if "time_of_day" in row:
        value = row.get("time_of_day")
        if type(value) is int and 0 <= value <= 24000:
            out["time_of_day"] = value
    if "weather" in row:
        weather = str(row.get("weather") or "").lower()
        if weather in {"clear", "rain", "thunder"}:
            out["weather"] = weather
    if "light" in row:
        value = row.get("light")
        if type(value) is int and 0 <= value <= 15:
            out["light"] = value
    if "roof" in row:
        value = row.get("roof")
        if type(value) is bool:
            out["roof"] = value
    events = row.get("events")
    if isinstance(events, list):
        safe_events = []
        for item in events[:MAX_OBSERVATION_EVENTS]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("type") or "").strip().lower()
            if not _EVENT_TYPE_RE.fullmatch(name):
                continue
            record = {"type": name}
            at = _finite(item.get("at"))
            if at is not None and at >= 0:
                record["at"] = round(at, 3)
            safe_events.append(record)
        if safe_events:
            out["events"] = safe_events
    return out


def game_clock(observation) -> dict | None:
    """In-game clock derived from ``time_of_day`` only (never wall-clock)."""
    if not isinstance(observation, dict):
        return None
    ticks = observation.get("time_of_day")
    if type(ticks) is not int or not 0 <= ticks <= 24000:
        return None
    hour = (ticks / _GAME_TICKS_PER_HOUR + _GAME_DAY_START_HOUR) % 24.0
    if 6 <= hour < 18:
        phase = "day"
    elif 18 <= hour < 20:
        phase = "evening"
    elif hour >= 20 or hour < 4:
        phase = "night"
    else:
        phase = "dawn"
    return {"time_of_day": ticks, "hour": round(hour, 2), "phase": phase}


def environment(observation) -> dict:
    """Bounded weather/time/light/roof comfort model from the observation.

    Habits, appraisal and comfort read this instead of the host wall clock, so
    an accelerated/offline run and a real night behave the same way.  Unknown
    fields stay unknown; nothing is invented.
    """
    observation = observation if isinstance(observation, dict) else {}
    clock = game_clock(observation)
    weather = observation.get("weather")
    if weather not in {"clear", "rain", "thunder"}:
        weather = ""
    light = observation.get("light")
    if type(light) is not int or not 0 <= light <= 15:
        light = None
    roof = observation.get("roof")
    if type(roof) is not bool:
        roof = None
    phase = clock["phase"] if clock else ""
    hour = clock["hour"] if clock else None
    storms = weather in {"rain", "thunder"}
    dark = phase in {"evening", "night"} or (light is not None and light <= 4)
    comfort = 0.5
    if weather == "rain":
        comfort += 0.12 if roof is True else -0.18
    elif weather == "thunder":
        comfort += 0.08 if roof is True else -0.30
    if dark:
        comfort -= 0.05 if roof is True else 0.12
    if light is not None and light <= 4 and roof is not True:
        comfort -= 0.08
    return {
        "time_of_day": observation.get("time_of_day")
        if type(observation.get("time_of_day")) is int else None,
        "game_hour": hour,
        "phase": phase,
        "weather": weather,
        "light": light,
        "roof": roof,
        "storms": storms,
        "dark": bool(dark),
        "sheltered": roof is True,
        "comfort": round(_clamp(comfort, 0.0, 1.0), 3),
    }


def nearby_players(observation: dict) -> list[dict]:
    """Players with an exact UUID; names are never used as identity."""
    result = []
    for entity in observation.get("nearby_entities", []) or []:
        if not isinstance(entity, dict) or entity.get("type") != "player":
            continue
        uuid = _safe_uuid(entity.get("uuid"))
        if not uuid:
            continue
        result.append({"uuid": uuid, "position": entity.get("position"),
                       "distance": entity.get("distance")})
    return result


def _threat_count(observation: dict) -> int:
    count = 0
    for entity in observation.get("nearby_entities", []) or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").lower()
        distance = _finite(entity.get("distance"))
        if name in _HOSTILE_NAMES and (distance is None or distance <= 10):
            count += 1
    return count


# ---------------------------------------------------------------------------
# skill registry validation (mirrors app/minecraft-sidecar/src/skills.js)
# ---------------------------------------------------------------------------
def validate_skill(name: str, args) -> bool:
    """Exact predefined skill schemas.  Unknown skills and extra keys fail."""
    if not isinstance(name, str) or not isinstance(args, dict):
        return False
    keys = set(args)

    def exact(expected):
        return keys == set(expected)

    def coords(integer=True):
        if integer:
            return (all(_coord(args.get(key), integer=True) is not None
                        for key in ("x", "y", "z"))
                    and _y_coord(args.get("y")) is not None)
        values = [_coord(args.get(key), integer=False) for key in ("x", "y", "z")]
        y = _finite(args.get("y"))
        return (all(value is not None for value in values)
                and y is not None and -64 <= y <= 319)

    if name == "move":
        return (exact({"x", "y", "z", "radius"}) and coords(False)
                and _finite(args.get("radius")) is not None
                and 0.5 <= float(args["radius"]) <= 4.0)
    if name == "follow":
        return (exact({"target_uuid", "duration_sec"})
                and bool(_safe_uuid(args.get("target_uuid")))
                and type(args.get("duration_sec")) is int
                and 1 <= args["duration_sec"] <= 30)
    if name == "chat":
        text = args.get("text")
        return (exact({"text"}) and isinstance(text, str)
                and 1 <= len(text) <= 200
                and not text.lstrip().startswith("/")
                and not re.search(r"[\u0000-\u001f\u007f-\u009f\u2028\u2029]", text))
    if name in {"dig", "explore"}:
        return exact({"x", "y", "z"}) and coords(True)
    if name == "collect":
        return (exact({"block", "count"}) and bool(_item_name(args.get("block")))
                and type(args.get("count")) is int and 1 <= args["count"] <= 16)
    if name == "craft":
        return (exact({"item", "count"}) and bool(_item_name(args.get("item")))
                and type(args.get("count")) is int and 1 <= args["count"] <= 16)
    if name in {"inventory", "eat", "home", "set_home", "respawn"}:
        return exact(set())
    if name == "chest":
        return (exact({"x", "y", "z", "operation", "item", "count"}) and coords(True)
                and args.get("operation") in {"deposit", "withdraw"}
                and bool(_item_name(args.get("item")))
                and type(args.get("count")) is int and 1 <= args["count"] <= 64)
    if name == "place":
        if not (exact({"x", "y", "z", "face_x", "face_y", "face_z", "item"})
                and coords(True) and bool(_item_name(args.get("item")))):
            return False
        faces = [args.get(key) for key in ("face_x", "face_y", "face_z")]
        return (all(type(value) is int and -1 <= value <= 1 for value in faces)
                and abs(faces[0]) + abs(faces[1]) + abs(faces[2]) == 1)
    if name == "farm":
        return (exact({"x", "y", "z", "seed"}) and coords(True)
                and args.get("seed") in {"wheat_seeds", "beetroot_seeds", "carrot", "potato"})
    if name == "breed":
        return (exact({"target_uuid", "item"}) and bool(_safe_uuid(args.get("target_uuid")))
                and bool(_item_name(args.get("item"))))
    if name == "attack":
        return exact({"target_uuid"}) and bool(_safe_uuid(args.get("target_uuid")))
    return False


# ---------------------------------------------------------------------------
# persisted state
# ---------------------------------------------------------------------------
_PERSONALITY_DEFAULTS = {
    "curiosity": .65, "industriousness": .55, "orderliness": .5,
    "sociability": .5, "mischief": .35, "caution": .65,
    "attachment": .5, "stubbornness": .4, "attention_span": .6,
    "expressiveness": .55,
}
_DRIVE_DEFAULTS = {
    "energy": .8, "boredom": .2, "loneliness": .2,
    "creative_tension": .3, "hoarding_urge": .2,
    "care_urge": .2, "wanderlust": .3,
}
_PERSONALITY_KEYS = tuple(_PERSONALITY_DEFAULTS)
_DRIVE_KEYS = tuple(_DRIVE_DEFAULTS)
_PROJECT_KEYS = ("id", "kind", "title", "goal", "status", "progress", "step",
                 "steps", "anchor", "created_at", "last_acted", "failures")
_PROMISE_KEYS = ("id", "title", "status", "created_at", "due_at", "steps",
                 "step", "attempts", "player_uuid", "last_acted", "fulfilled_at")
_PLACE_KEYS = ("kind", "x", "y", "z", "dimension", "visits", "last_seen")
_PLAYER_KEYS = ("uuid", "role", "familiarity", "trust", "closeness",
                "boundary", "last_seen", "shared_history_count", "explicit_link")
_SUGGESTION_KEYS = ("id", "player_uuid", "role", "text", "status", "hint",
                    "created_at", "due_at", "expires_at", "reason", "counter")
_HABIT_KEYS = ("count", "last_at")
# Successful skills that back one semantic habit.  ImpulseEngine.propose()
# reads these names when it adds habit candidates, so recording only the raw
# skill name would leave those candidates unreachable.
_HABIT_KEY_FOR_SKILL = {"home": "return_home", "farm": "tend_farm",
                        "follow": "greet_known", "chat": "greet_known"}


def _sanitize_args(args) -> dict:
    out = {}
    if not isinstance(args, dict):
        return out
    for key, value in args.items():
        if not isinstance(key, str) or len(key) > 64:
            continue
        if isinstance(value, str):
            if key.endswith("_uuid"):
                out[key] = _safe_uuid(value)
            elif key in {"block", "item", "seed"}:
                out[key] = value[:64]
            else:
                out[key] = _safe_text(value, 200)
        elif type(value) in (int, float):
            out[key] = value
    return out


def _clean_steps(value, limit=8):
    if not isinstance(value, list):
        return []
    out = []
    for step in value[-limit:]:
        if not isinstance(step, dict):
            continue
        skill = step.get("skill")
        args = _sanitize_args(step.get("args"))
        if isinstance(skill, str) and validate_skill(skill, args):
            out.append({"skill": skill, "args": args})
    return out


def _clean_row(row, keys):
    if not isinstance(row, dict):
        return {}
    return {key: row[key] for key in keys if key in row}


def _project_row(row):
    out = _clean_row(row, _PROJECT_KEYS)
    if not out:
        return {}
    out["steps"] = _clean_steps(out.get("steps"))
    for key in ("id", "kind", "title", "goal"):
        if key in out:
            out[key] = _safe_text(out[key], 160)
    return out


def _promise_row(row):
    out = _clean_row(row, _PROMISE_KEYS)
    if not out:
        return {}
    out["steps"] = _clean_steps(out.get("steps"))
    out["title"] = _safe_text(out.get("title"), 160)
    out["id"] = _safe_text(out.get("id"), 64)
    if "player_uuid" in out:
        out["player_uuid"] = _safe_uuid(out.get("player_uuid"))
    return out


def _place_row(row):
    out = _clean_row(row, _PLACE_KEYS)
    if not out:
        return {}
    out["kind"] = _safe_text(out.get("kind"), 24)
    out["dimension"] = _safe_dimension(out.get("dimension"))
    # A place is only ever a canonical numeric point: a legacy/poisoned row
    # carrying a nickname, a string coordinate or a path is dropped whole.
    point = _canonical_point(out)
    if not out["kind"] or point is None:
        return {}
    if out["kind"] == "home" and not out["dimension"]:
        return {}
    out.update(point)
    return out


def _player_row(row):
    out = _clean_row(row, _PLAYER_KEYS)
    if not out:
        return {}
    out["uuid"] = _safe_uuid(out.get("uuid"))
    out["role"] = _safe_text(out.get("role"), 24) or "player"
    return out if out["uuid"] else {}


def _suggestion_row(row):
    out = _clean_row(row, _SUGGESTION_KEYS)
    if not out:
        return {}
    out["id"] = _safe_text(out.get("id"), 32)
    out["player_uuid"] = _safe_uuid(out.get("player_uuid"))
    out["role"] = _safe_text(out.get("role"), 24) or "player"
    out["text"] = _safe_text(out.get("text"), 160)
    out["status"] = out.get("status") if out.get("status") in _SUGGESTION_STATUSES else ""
    hint = out.get("hint")
    out["hint"] = hint if hint in INTENTS else ""
    out["reason"] = _safe_text(out.get("reason"), 32)
    out["counter"] = _safe_text(out.get("counter"), 160)
    out["created_at"] = _num(out.get("created_at"), 0, low=0)
    out["due_at"] = _num(out.get("due_at"), 0, low=0)
    out["expires_at"] = _num(out.get("expires_at"), 0, low=0)
    if not out["id"] or not out["player_uuid"] or not out["status"] or not out["text"]:
        return {}
    return out


class ResidentState:
    """Bounded persisted self narrative, drives, projects, promises and people."""

    VERSION = 2

    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.data = self._load()

    def _load(self):
        try:
            row = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            row = {}
        if not isinstance(row, dict):
            row = {}
        data = {
            "version": self.VERSION,
            "self_narrative": _safe_text(row.get("self_narrative"),
                                        500) or "我正在慢慢熟悉这个世界。",
            "personality": self._numbers(row.get("personality"), _PERSONALITY_DEFAULTS),
            "drives": self._numbers(row.get("drives"), _DRIVE_DEFAULTS),
            "projects": [item for item in (_project_row(row)
                                           for row in self._rows(
                                               row.get("projects"), MAX_PROJECTS,
                                               _PROJECT_KEYS)) if item],
            "promises": [item for item in (_promise_row(row)
                                           for row in self._rows(
                                               row.get("promises"), MAX_PROMISES,
                                               _PROMISE_KEYS)) if item],
            "places": [item for item in (_place_row(row)
                                         for row in self._rows(
                                             row.get("places"), MAX_PLACES,
                                             _PLACE_KEYS)) if item],
            "players": [item for item in (_player_row(row)
                                          for row in self._rows(
                                              row.get("players"), MAX_PLAYERS,
                                              _PLAYER_KEYS)) if item],
            "suggestions": [item for item in (_suggestion_row(row)
                                              for row in self._rows(
                                                  row.get("suggestions"), MAX_SUGGESTIONS,
                                                  _SUGGESTION_KEYS)) if item],
            "habits": self._habits(row.get("habits")),
            "journal": self._journal(row.get("journal")),
            "reflection": _safe_text(row.get("reflection"), 500),
            "affect_residue": self._residue(row.get("affect_residue")),
            "last_action": _safe_text(row.get("last_action"), 64),
            "recent_actions": [_safe_text(x, 100) for x in row.get("recent_actions", [])[-16:]
                               if isinstance(x, str)] if isinstance(row.get("recent_actions"), list) else [],
            "counters": self._counters(row.get("counters")),
            "last_tick": _num(row.get("last_tick"), self.clock(), low=0),
            "updated_at": _num(row.get("updated_at"), 0, low=0),
            "next_project_id": max(1, _int(row.get("next_project_id"), 1, low=1)),
            "next_promise_id": max(1, _int(row.get("next_promise_id"), 1, low=1)),
            "next_suggestion_id": max(1, _int(row.get("next_suggestion_id"), 1, low=1)),
            "last_promise_at": _num(row.get("last_promise_at"), 0, low=0),
            "last_project_complete_at": _num(
                row.get("last_project_complete_at"), 0, low=0),
            "daily_calls": max(0, _int(row.get("daily_calls"), 0)),
            "call_day": str(row.get("call_day") or "")[:10],
        }
        # Explicitly drop legacy/forbidden fields if an older file carried them.
        for forbidden in ("private_thought", "private_thoughts", "owner_qq_uid",
                          "world_path", "save_path", "appraisal"):
            data.pop(forbidden, None)
        return data

    @staticmethod
    def _numbers(value, defaults):
        result = dict(defaults)
        if isinstance(value, dict):
            for key in result:
                try:
                    result[key] = max(0.0, min(1.0, float(value[key])))
                except (KeyError, TypeError, ValueError):
                    pass
        return result

    @staticmethod
    def _rows(value, limit, keys):
        if not isinstance(value, list):
            return []
        out = []
        for item in value[-limit:]:
            row = _clean_row(item, keys)
            if row:
                out.append(row)
        return out

    @staticmethod
    def _habits(value):
        result = {}
        if isinstance(value, dict):
            for key, item in list(value.items())[-16:]:
                name = _safe_text(key, 32)
                item = _clean_row(item, _HABIT_KEYS)
                if not name or not item:
                    continue
                result[name] = {
                    "count": max(0, _int(item.get("count"), 0, high=100000)),
                    "last_at": _num(item.get("last_at"), 0, low=0),
                }
        return result

    @staticmethod
    def _journal(value):
        if not isinstance(value, list):
            return []
        out = []
        for item in value[-MAX_JOURNAL:]:
            if not isinstance(item, dict):
                continue
            text = _safe_text(item.get("text"), 220)
            if text:
                out.append({"at": _num(item.get("at"), 0, low=0), "text": text})
        return out

    @staticmethod
    def _residue(value):
        value = value if isinstance(value, dict) else {}
        return {"fear": _num(value.get("fear"), 0, low=0, high=1),
                "warmth": _num(value.get("warmth"), 0, low=0, high=1),
                "updated_at": _num(value.get("updated_at"), 0, low=0)}

    @staticmethod
    def _counters(value):
        value = value if isinstance(value, dict) else {}
        return {"cycles": max(0, _int(value.get("cycles"), 0)),
                "decisions": max(0, _int(value.get("decisions"), 0)),
                "skills": max(0, _int(value.get("skills"), 0)),
                "failures": max(0, _int(value.get("failures"), 0)),
                "projects_completed": max(0, _int(value.get("projects_completed"), 0)),
                "promises_fulfilled": max(0, _int(value.get("promises_fulfilled"), 0))}

    def serializable(self):
        """Only allow-listed fields ever reach disk."""
        data = self.data
        return {
            "version": self.VERSION,
            "self_narrative": _safe_text(data.get("self_narrative"), 500),
            "personality": dict(data["personality"]),
            "drives": dict(data["drives"]),
            "projects": [_project_row(row) for row in data["projects"]],
            "promises": [_promise_row(row) for row in data["promises"]],
            "places": [_place_row(row) for row in data["places"]],
            "players": [_player_row(row) for row in data["players"]],
            "suggestions": [_suggestion_row(row) for row in data["suggestions"]],
            "habits": {key: _clean_row(value, _HABIT_KEYS) for key, value in data["habits"].items()},
            "journal": [
                {"at": _num(item.get("at"), 0, low=0),
                 "text": _safe_text(item.get("text"), 220)}
                for item in data["journal"][-MAX_JOURNAL:] if isinstance(item, dict)
            ],
            "reflection": _safe_text(data.get("reflection"), 500),
            "affect_residue": dict(data["affect_residue"]),
            "last_action": _safe_text(data.get("last_action"), 64),
            "recent_actions": [_safe_text(x, 100) for x in data["recent_actions"][-16:]],
            "counters": dict(data["counters"]),
            "last_tick": _num(data.get("last_tick"), 0, low=0),
            "updated_at": _num(data.get("updated_at"), 0, low=0),
            "next_project_id": max(1, _int(data.get("next_project_id"), 1, low=1)),
            "next_promise_id": max(1, _int(data.get("next_promise_id"), 1, low=1)),
            "next_suggestion_id": max(1, _int(data.get("next_suggestion_id"), 1, low=1)),
            "last_promise_at": _num(data.get("last_promise_at"), 0, low=0),
            "last_project_complete_at": _num(
                data.get("last_project_complete_at"), 0, low=0),
            "daily_calls": max(0, _int(data.get("daily_calls"), 0)),
            "call_day": str(data.get("call_day") or "")[:10],
        }

    def save(self):
        self.data["updated_at"] = self.clock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.serializable(), ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)


class BindingStore:
    """Machine-local actor/world binding.  Never stores QQ UID or save path."""

    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        try:
            row = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            row = {}
        self.data = row if isinstance(row, dict) else {}

    def bind(self, actor_uuid: str, world_id: str) -> None:
        actor = _safe_uuid(actor_uuid)
        if not actor or not _WORLD_ID_RE.fullmatch(str(world_id or "")):
            raise GuardDenied("binding_invalid")
        existing_actor = _safe_uuid(self.data.get("actor_uuid"))
        existing_world = str(self.data.get("world_id") or "")
        if existing_actor and existing_actor != actor:
            raise GuardDenied("binding_mismatch")
        if existing_world and existing_world != world_id:
            raise GuardDenied("binding_mismatch")
        self.data = {"version": 1, "actor_uuid": actor, "world_id": str(world_id),
                     "owner_bound": True, "bound_at": self.clock()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)


class DecisionBudget:
    """Persisted per-day remote budget.  Reservation happens before the call."""

    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        try:
            row = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            row = {}
        row = row if isinstance(row, dict) else {}
        self.data = {
            "version": 1,
            "day": str(row.get("day") or "")[:10],
            "attempts": max(0, _int(row.get("attempts"), 0)),
            "spent_usd": max(0.0, _num(row.get("spent_usd"), 0)),
            "last_call_at": max(0.0, _num(row.get("last_call_at"), 0)),
        }

    def _roll(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.data.get("day") != day:
            self.data.update({"day": day, "attempts": 0, "spent_usd": 0.0})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    def reserve(self, *, now: float, cost_upper: float, daily_limit: float,
                max_attempts: int) -> bool:
        """Persist the attempt *before* the remote call; never refund."""
        if (type(cost_upper) not in (int, float) or type(daily_limit) not in (int, float)
                or type(max_attempts) is not int):
            return False
        cost = float(cost_upper)
        limit = float(daily_limit)
        if (not math.isfinite(cost) or not math.isfinite(limit)
                or cost <= 0 or limit <= 0 or max_attempts < 1):
            return False
        self._roll(now)
        if self.data["attempts"] >= max_attempts:
            return False
        if self.data["spent_usd"] + cost > limit + 1e-9:
            return False
        previous = (self.data["attempts"], self.data["spent_usd"],
                    self.data["last_call_at"])
        self.data["attempts"] += 1
        self.data["spent_usd"] = round(self.data["spent_usd"] + cost, 6)
        self.data["last_call_at"] = float(now)
        try:
            self.save()
        except OSError:
            self.data["attempts"], self.data["spent_usd"], self.data["last_call_at"] = previous
            return False
        return True


# ---------------------------------------------------------------------------
# workspace / candidate / decision value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkspaceItem:
    kind: str
    label: str
    salience: float
    source: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "label": _safe_text(self.label, 80),
                "salience": round(self.salience, 3), "source": self.source}


class GlobalWorkspace:
    """Capacity-limited attention set: 3-5 items, never omniscient."""

    def __init__(self, capacity: int = WORKSPACE_CAPACITY):
        self.capacity = _int(capacity, WORKSPACE_CAPACITY,
                             low=WORKSPACE_MIN, high=WORKSPACE_MAX)
        self._items: list[WorkspaceItem] = []

    def reset(self) -> None:
        self._items = []

    def submit(self, kind: str, label: str, salience: float, source: str) -> None:
        item = WorkspaceItem(str(kind)[:24], _safe_text(label, 80),
                             _clamp(_num(salience, 0, low=0, high=1), 0.0, 1.0),
                             str(source)[:24])
        self._items.append(item)

    def contents(self) -> list[WorkspaceItem]:
        # Stable sort by descending salience, then insertion order.
        ranked = sorted(enumerate(self._items), key=lambda pair: (-pair[1].salience, pair[0]))
        return [item for _index, item in ranked[: self.capacity]]

    def as_context(self) -> list[dict]:
        return [item.as_dict() for item in self.contents()]

    def __len__(self) -> int:
        return len(self.contents())


@dataclass(frozen=True)
class Candidate:
    intent: str
    score: float
    source: str
    reason: str
    urgency: float = 0.0
    confidence: float = 0.0
    project_id: str = ""
    promise_id: str = ""

    def alternative(self) -> dict:
        return {"intent": self.intent, "score": round(_clamp(self.score, 0, 1), 3),
                "source": self.source, "reason": _safe_text(self.reason, 80)}


@dataclass
class Decision:
    intent: str
    alternatives: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    conflict: float = 0.0
    emotion_delta: dict = field(default_factory=dict)
    private_thought: str = ""
    public_utterance: str = ""
    rationale: str = ""
    expected_duration: int = 30
    interruptibility: float = .8
    source: str = "fallback"
    skill: str = ""
    args: dict = field(default_factory=dict)
    project_id: str = ""
    promise_id: str = ""

    def safe_log(self) -> dict:
        """Structured log fields: never the private thought or raw model text."""
        return {"intent": self.intent, "source": self.source,
                "confidence": round(self.confidence, 2),
                "conflict": round(self.conflict, 2),
                "alternatives": [
                    {"intent": row.get("intent"), "score": row.get("score"),
                     "source": row.get("source")}
                    for row in self.alternatives[:4]
                ],
                "rationale": _safe_text(self.rationale, 120),
                "skill": self.skill, "expected_duration": self.expected_duration}


@dataclass(frozen=True)
class Appraisal:
    goal_congruence: float = 0.0
    novelty: float = 0.0
    controllability: float = 0.0
    agency: str = "world"
    relevance: float = 0.0
    threat: bool = False
    social: bool = False
    success: bool = False
    failure: bool = False


# ---------------------------------------------------------------------------
# appraisal / drives / workspace construction
# ---------------------------------------------------------------------------
class Appraiser:
    """Event -> appraisal dimensions -> emotion deltas + residue."""

    EMOTIONS = ("开心", "兴奋", "惊讶", "烦躁", "低落", "感动", "得意", "害羞")

    def appraise(self, observation: dict, state: ResidentState, outcome=None) -> Appraisal:
        threat = _threat_count(observation) > 0
        health, food = observation["health"], observation["food"]
        body_bad = health <= 8 or food <= 7
        players = nearby_players(observation)
        env = environment(observation)
        exposed = env["storms"] and env["sheltered"] is not True
        uncomfortable = env["comfort"] < 0.35
        disrupted = exposed or uncomfortable
        events = observation.get("events")
        event_count = len(events) if isinstance(events, list) else 0
        goal_congruence = (
            -0.6 if body_bad or threat else
            (-0.25 if disrupted else
             (0.55 if outcome and outcome.get("ok") else
              (-0.4 if outcome and outcome.get("executed") else 0.05))))
        controllability = (
            -0.5 if threat or body_bad else
            (-0.2 if disrupted else
             (0.45 if outcome and outcome.get("ok") else 0.1)))
        novelty = (0.6 if players else (0.35 if threat else 0.1))
        if event_count:
            novelty = min(0.8, novelty + 0.15)
        return Appraisal(
            goal_congruence=goal_congruence,
            novelty=novelty,
            controllability=controllability,
            agency="world" if threat or body_bad else (
                "self" if outcome and outcome.get("ok") else "world"),
            relevance=(0.7 if players else (0.5 if threat else 0.2)),
            threat=threat or body_bad,
            social=bool(players),
            success=bool(outcome and outcome.get("ok")),
            failure=bool(outcome and outcome.get("executed") and not outcome.get("ok")),
        )

    def deltas(self, appraisal: Appraisal, personality: dict, residue: dict) -> dict:
        out: dict[str, float] = {}
        if appraisal.threat:
            out["烦躁"] = 4 + 2 * residue.get("fear", 0)
            out["低落"] = 2
            out["惊讶"] = 2
        if appraisal.failure:
            out["低落"] = out.get("低落", 0) + 3
            out["烦躁"] = out.get("烦躁", 0) + 2
        if appraisal.success:
            out["开心"] = out.get("开心", 0) + 4
            out["得意"] = out.get("得意", 0) + 2 + personality.get("industriousness", .5)
        if appraisal.novelty > 0.25 and personality.get("curiosity", .5) > 0.4:
            out["兴奋"] = out.get("兴奋", 0) + 3
            out["惊讶"] = out.get("惊讶", 0) + 2
        if appraisal.social:
            out["感动"] = out.get("感动", 0) + 3
            out["害羞"] = out.get("害羞", 0) + 1
        if appraisal.controllability < -0.2 and personality.get("caution", .5) > 0.5:
            out["烦躁"] = out.get("烦躁", 0) + 2
        return {key: int(round(max(-15.0, min(15.0, value))))
                for key, value in out.items() if value}


# ---------------------------------------------------------------------------
# fast / slow / fallback / arbiter systems
# ---------------------------------------------------------------------------
class ImpulseEngine:
    """Fast, deterministic candidates; never calls a model."""

    def propose(self, observation: dict, state: ResidentState,
                affect: dict, workspace: GlobalWorkspace,
                hints=None) -> list[Candidate]:
        drives = state.data["drives"]
        personality = state.data["personality"]
        candidates: list[Candidate] = []
        players = nearby_players(observation)
        known = self._known_players(players, state)
        habits = state.data["habits"]
        env = environment(observation)

        if observation["health"] <= 4:
            candidates.append(Candidate("recover", .99, "survival",
                                        "身体撑不住了", urgency=1.0))
        elif observation["food"] <= 5:
            candidates.append(Candidate("recover", .92, "survival",
                                        "肚子饿得难受", urgency=.9))
        elif _threat_count(observation) > 0:
            candidates.append(Candidate("recover", .88, "survival",
                                        "附近有危险", urgency=.85))

        project = self._first_project(state)
        if project:
            candidates.append(Candidate(
                "continue_project", .64 + .1 * personality.get("industriousness", .5)
                + .08 * personality.get("orderliness", .5), "project",
                "手上的东西还没做完", project_id=str(project.get("id", ""))))
        promise = self._due_promise(state)
        if promise:
            candidates.append(Candidate(
                "fulfill_promise", .60 + .12 * personality.get("attachment", .5),
                "promise", "答应过的事不能一直拖着",
                promise_id=str(promise.get("id", ""))))

        strongest = max(_DRIVE_KEYS, key=lambda key: drives.get(key, 0))
        pressure = drives.get(strongest, 0)
        if pressure >= .45:
            candidates.append(Candidate("satisfy_drive", .32 + .28 * pressure,
                                        "drive", f"{strongest} 在催",
                                        urgency=pressure * .5))

        if known and drives.get("loneliness", 0) + personality.get("sociability", 0) > .8:
            candidates.append(Candidate("socialize", .30 + .2 * personality.get("sociability", .5),
                                        "social", "附近有认识的人"))
        if habits.get("greet_known", {}).get("count", 0) >= 2 and known:
            candidates.append(Candidate("socialize", .46, "habit",
                                        "见到熟人会先靠近"))
        if habits.get("tend_farm", {}).get("count", 0) >= 2 and project \
                and project.get("kind") == "farm":
            candidates.append(Candidate("continue_project", .58, "habit",
                                        "习惯去看看那块田",
                                        project_id=str(project.get("id", ""))))
        # Evening/night is read from the in-game clock, never the host clock; an
        # unknown game time cannot manufacture a "it is late" habit.
        late = env["phase"] in {"evening", "night"}
        if habits.get("return_home", {}).get("count", 0) >= 2 \
                and (late or drives.get("energy", 1) < .35):
            candidates.append(Candidate("rest", .40, "habit", "天晚了，想回家"))
        # Rain/thunder outside pushes the same "go inside" pull; a known roof
        # keeps it out of the ranking entirely.
        if env["storms"] and env["sheltered"] is not True:
            candidates.append(Candidate("rest", .52, "weather", "外面在下雨，想进屋"))

        # Opportunity is deliberately the weakest source (rank 5, not rank 1).
        if personality.get("curiosity", .5) > .55 and drives.get("wanderlust", 0) > .35:
            candidates.append(Candidate("explore", .26, "opportunity",
                                        "远处好像有点意思", urgency=.15))
        # A suggestion accepted from a known player (see consider_suggestion)
        # is a bounded social input, never a skill or an authorization.
        for intent, score in hints or []:
            if intent in INTENTS:
                candidates.append(Candidate(str(intent),
                                            _clamp(_num(score, 0, low=0, high=1), 0, 1),
                                            "suggestion", "有人提过这件事",
                                            urgency=.1))
        if not candidates:
            candidates.append(Candidate("idle", .10, "fallback", "暂时什么都不想做"))
        return candidates

    @staticmethod
    def _known_players(players, state):
        known = {row.get("uuid") for row in state.data["players"]
                 if row.get("uuid") and row.get("role") != "self"
                 and (row.get("shared_history_count", 0) or
                      row.get("familiarity", 0) > .55)}
        return [row for row in players if row["uuid"] in known]

    @staticmethod
    def _first_project(state):
        for project in state.data["projects"]:
            if project.get("status") == "active" and project.get("steps"):
                return project
        return None

    @staticmethod
    def _due_promise(state):
        now = _num(state.data.get("last_tick"), time.time(), low=0) or time.time()
        for promise in state.data["promises"]:
            if promise.get("status") != "active":
                continue
            due = _num(promise.get("due_at"), 0, low=0)
            if not due or due <= now:
                return promise
        return None


class DeterministicPlanner:
    """Always-available fallback scoring (requirements 5.4)."""

    def rank(self, observation: dict, state: ResidentState, affect: dict,
             workspace: GlobalWorkspace, impulse: list[Candidate]) -> list[Candidate]:
        drives = state.data["drives"]
        personality = state.data["personality"]
        scores: dict[str, Candidate] = {}

        def add(intent, score, source, reason, **kwargs):
            score = _clamp(score, 0.0, 1.0)
            current = scores.get(intent)
            if current is None or score > current.score:
                scores[intent] = Candidate(intent, score, source, reason, **kwargs)

        project = ImpulseEngine._first_project(state)
        if project:
            commitment = .30 + .30 * personality.get("industriousness", .5)
            fit = .10 * personality.get("orderliness", .5)
            add("continue_project", .55 + commitment + fit - .02,
                "project", "把已经开始的事接着做", project_id=str(project.get("id", "")))
        promise = ImpulseEngine._due_promise(state)
        if promise:
            age = max(0.0, (_num(state.data.get("last_tick"), time.time(), low=0)
                            or time.time())
                      - _num(promise.get("created_at"), 0, low=0))
            add("fulfill_promise", .62 + .20 * personality.get("attachment", .5)
                + min(.20, age / 600.0 * .20), "promise", "兑现承诺",
                promise_id=str(promise.get("id", "")))

        strongest = max(_DRIVE_KEYS, key=lambda key: drives.get(key, 0))
        add("satisfy_drive", .20 + .55 * drives.get(strongest, 0),
            "drive", f"{strongest} 的压力")

        players = nearby_players(observation)
        known = ImpulseEngine._known_players(players, state)
        social_pull = (.25 + .35 * personality.get("sociability", .5)
                       + .2 * drives.get("loneliness", 0)
                       + .15 * state.data["affect_residue"].get("warmth", 0))
        add("socialize", social_pull if known else social_pull * .35,
            "social", "想和认识的人待一会儿")

        wander = (.18 + .35 * drives.get("wanderlust", 0) * personality.get("curiosity", .5))
        add("explore", min(wander, .34), "opportunity", "环境机会（权重封顶）")

        add("rest", .10 + .35 * (1 - drives.get("energy", .5))
            + .08 * personality.get("caution", .5), "drive", "需要缓一缓")
        add("idle", .06 + .04 * (1 - personality.get("attention_span", .5)),
            "fallback", "发呆也是一种选择")

        # Survival is not part of ordinary scoring; it overrides in Arbiter.
        for candidate in impulse:
            if candidate.source == "survival":
                scores[candidate.intent] = candidate
            elif candidate.intent not in scores:
                scores[candidate.intent] = candidate
            elif candidate.score > scores[candidate.intent].score:
                scores[candidate.intent] = candidate
        return sorted(scores.values(), key=lambda item: (-item.score, item.intent))


class JevAdapter:
    """Slow intent-only decision adapter.  Budget reservation precedes calls."""

    def __init__(self, resident: "MinecraftResident"):
        self.resident = resident

    def _settings(self):
        config = self.resident.config
        if _cfg(config, "decision_enabled", False) is not True:
            return None
        if _cfg(config, "decision_remote_enabled", True) is not True:
            return None
        cost = _finite(_cfg(config, "decision_cost_usd_per_call",
                            _cfg(config, "decision_cost_usd", None)))
        limit = _finite(_cfg(config, "decision_budget_usd_per_day", 0))
        max_attempts = _cfg(config, "max_decisions_per_day", None)
        if cost is None or cost <= 0 or limit is None or limit <= 0:
            return None
        if type(max_attempts) is not int or max_attempts < 1:
            return None
        timeout = _num(_cfg(config, "decision_timeout_sec", 12), 12, low=5, high=30)
        return {"cost": float(cost), "limit": float(limit),
                "max_attempts": int(max_attempts), "timeout": timeout}

    @property
    def can_decide(self) -> bool:
        return self._settings() is not None

    async def decide(self, context: dict):
        settings = self._settings()
        if settings is None:
            return None
        now = self.resident.clock()
        if not self.resident.budget.reserve(
                now=now, cost_upper=settings["cost"], daily_limit=settings["limit"],
                max_attempts=settings["max_attempts"]):
            return None
        questions = {
            "intent": {
                "type": "choice",
                "instructions": "只选择此刻的高层生活意图；只选给定候选，不输出技能或代码。",
                "criteria": {key: key for key in INTENTS},
            },
            "confidence": {
                "type": "score",
                "instructions": "这个选择有多确定？",
                "criteria": ["low", "medium", "high"],
            },
        }
        try:
            answers = await decider.decide(
                self.resident.config, context, questions, timeout=settings["timeout"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reserve stays consumed
            log.warn(f"Minecraft deliberate decision fallback: {type(exc).__name__}")
            return None
        if not isinstance(answers, dict):
            return None
        answer = answers.get("intent")
        candidate = answer.get("choice") if isinstance(answer, dict) else ""
        if candidate not in INTENTS:
            return None
        confidence = decider._score(answer, .5)
        return Candidate(str(candidate), .78 + .22 * confidence, "jev",
                         "审慎判断", confidence=confidence)


class Arbiter:
    """Resolve impulse vs deliberation, including hesitation/partial action."""
    def resolve(self, deliberate: Candidate | None, impulse: list[Candidate],
                alternatives: list[Candidate], personality: dict,
                residue: dict, appraisal: Appraisal) -> Decision:
        survival = next((item for item in impulse if item.source == "survival"), None)
        if survival and survival.urgency >= .8:
            chosen = survival
            conflict = 1.0
            rationale = "先保命，别的事等会儿再说。"
        else:
            if deliberate is None:
                deliberate = alternatives[0] if alternatives else Candidate(
                    "idle", .1, "fallback", "没事可做")
            fast = max((item for item in impulse if item.source != "survival"),
                       key=lambda item: item.score, default=None)
            conflict = 0.0
            if fast is not None and fast.intent != deliberate.intent:
                scale = max(deliberate.score, fast.score, 0.1)
                conflict = _clamp(1.0 - abs(deliberate.score - fast.score) / scale,
                                  0.0, 1.0)
            caution = personality.get("caution", .5) + .3 * residue.get("fear", 0)
            if (fast is not None and fast.intent != deliberate.intent
                    and conflict >= .72 and caution >= .6
                    and personality.get("stubbornness", .5) < .8):
                chosen = Candidate("idle", deliberate.score * .8, "arbiter",
                                   "两边都想要，先犹豫一下")
                rationale = "我拿不准，先站一会儿再说。"
            else:
                chosen = deliberate
                rationale = "想了想，还是这样更像我。"
                if fast is not None and fast.intent != deliberate.intent \
                        and fast.urgency > .7 and personality.get("stubbornness", .5) < .45:
                    chosen = fast
                    rationale = "本来想好了，可心里那股劲更急。"

        ranked = sorted({item.intent: item for item in (alternatives + impulse)}.values(),
                        key=lambda item: (-item.score, item.intent))
        return Decision(
            intent=chosen.intent,
            alternatives=[item.alternative() for item in ranked[:4]],
            confidence=_clamp(chosen.confidence or chosen.score, 0, 1),
            conflict=round(conflict, 2),
            emotion_delta={},
            private_thought=self._private_thought(chosen),
            rationale=rationale,
            expected_duration=30 if chosen.intent in {"idle", "rest"} else 60,
            interruptibility=.9 if conflict >= .7 else .7,
            source=chosen.source,
            project_id=chosen.project_id,
            promise_id=chosen.promise_id,
        )

    @staticmethod
    def _private_thought(candidate: Candidate) -> str:
        thoughts = {
            "continue_project": "手上这件事还没完，我想再推进一步。",
            "fulfill_promise": "答应过的事，老拖着心里不踏实。",
            "satisfy_drive": "心里有股说不上来的劲，得做点什么。",
            "socialize": "有人在旁边的话，我想靠近一点。",
            "explore": "远处好像有什么，值得走一趟。",
            "rest": "有点累了，先缓一缓。",
            "recover": "现在不安全，先顾好自己。",
            "idle": "这一刻什么都不太想干。",
        }
        return thoughts.get(candidate.intent, "")


# ---------------------------------------------------------------------------
# resident
# ---------------------------------------------------------------------------
class MinecraftResident:
    """Manage one explicitly registered world resident and its bounded mind."""

    def __init__(self, bot, *, sidecar=None, guard=None, clock=time.time):
        self.bot = bot
        self.config = bot.config
        self.clock = clock
        self.sidecar = sidecar or MinecraftSidecar(env=self._sidecar_env())
        self.guard = guard or GuardClient(self._guard_settings())
        self.state = ResidentState(bot.paths.minecraft_content("resident.json"), clock=clock)
        self.budget = DecisionBudget(bot.paths.minecraft_state("budget.json"), clock=clock)
        self.bindings = BindingStore(bot.paths.minecraft_state("bindings.json"), clock=clock)
        self.workspace = GlobalWorkspace(
            self.state.data.get("workspace_capacity", WORKSPACE_CAPACITY))
        self.appraiser = Appraiser()
        self.impulse_engine = ImpulseEngine()
        self.planner = DeterministicPlanner()
        self.jev = JevAdapter(self)
        self.arbiter = Arbiter()
        self._task = None
        self._stop = asyncio.Event()
        self._started = False
        self._latched = False
        self._last_observe = 0.0
        self._last_decision = 0.0
        self._last_backup = 0.0
        self._last_appraisal = 0.0
        self._last_episode = 0.0
        self._last_appraisal_result: Appraisal | None = None
        self._last_emotion_delta: dict = {}
        self._last_outcome: dict | None = None
        self._failure_streak = 0
        self._idle_cooldown = 0
        self._action_log: list[dict] = []
        self._reconnect_attempts = 0
        # Node's `home` is process-only, so this session-local flag gates every
        # home skill until the child accepted a `set_home` seed.
        self._home_seeded = False
        self._home_seed_failures = 0
        self._home_seed_reason = ""
        self._home_seed_last_failure = 0.0

    # ------------------------------------------------------------ config
    def _guard_settings(self):
        settings = GuardSettings(
            _guard_endpoint(os.environ.get(
                "PAWBOT_MC_GUARD_URL",
                str(_cfg(self.config, "guard_url", "http://127.0.0.1:6198/v1/permit")))),
            os.environ.get("PAWBOT_MC_GUARD_SECRET", ""),
            str(_cfg(self.config, "test_world_id", "") or ""),
            str(_cfg(self.config, "world_path", "") or ""),
            str(_cfg(self.config, "owner_player_uuid", "") or ""),
            _int(_cfg(self.config, "port", 0), 0, low=0, high=65535))
        settings.validate()
        return settings

    def _sidecar_env(self):
        env = {
            "PAWBOT_MC_GUARD_URL": _guard_endpoint(os.environ.get(
                "PAWBOT_MC_GUARD_URL",
                str(_cfg(self.config, "guard_url", "http://127.0.0.1:6198/v1/permit")))),
            "PAWBOT_MC_GUARD_SECRET": os.environ.get("PAWBOT_MC_GUARD_SECRET", ""),
            "PAWBOT_MC_WORLD_ID": str(_cfg(self.config, "test_world_id", "") or ""),
            "PAWBOT_MC_WORLD_PATH": str(_cfg(self.config, "world_path", "") or ""),
            "PAWBOT_MC_ACTOR_UUID": str(_cfg(self.config, "owner_player_uuid", "") or ""),
            "PAWBOT_MC_PVP_UUIDS": json.dumps(sorted(allowed_pvp_targets(
                _cfg(self.config, "pvp_target_uuids", [])))),
            # Only literal Python True enables a child flag.
            "PAWBOT_MC_WRITE_ENABLED": "true" if _cfg(
                self.config, "write_enabled", False) is True else "false",
            "PAWBOT_MC_PVP_ENABLED": "true" if _cfg(
                self.config, "pvp_enabled", False) is True else "false",
            "PAWBOT_MC_AUTH_DIR": self.bot.paths.minecraft_state("auth"),
        }
        return env

    def _backup_interval_seconds(self) -> int:
        """Advisory only: the Java guard's ``backup_interval_sec`` is the sole schedule.

        The key is kept for config compatibility and to show the owner the shared ceiling; the
        resident never asks for a periodic ``backup`` action.
        """
        raw = _cfg(self.config, "backup_interval_min", 30)
        minutes = _int(raw, 30, low=1, high=MAX_BACKUP_INTERVAL_MIN)
        return minutes * 60

    def _decision_interval_seconds(self) -> int:
        raw = _cfg(self.config, "decision_interval_sec", 30)
        return _int(raw, 30, low=30, high=90)

    def _tick_interval_seconds(self) -> float:
        raw = _cfg(self.config, "tick_interval_sec", 3)
        return _num(raw, 3.0, low=0.5, high=10.0)

    def _reconnect_attempts_allowed(self) -> int:
        return _int(_cfg(self.config, "reconnect_attempts", 3), 3, low=1, high=6)

    def _connect_params(self) -> dict:
        config = self.config
        host = str(_cfg(config, "host", "127.0.0.1") or "")
        port = _cfg(config, "port", 0)
        version = str(_cfg(config, "version", "1.20.1") or "")
        username = str(_cfg(config, "username", "") or "")
        auth_mode = str(_cfg(config, "auth_mode", "") or "")
        raw_timeout = _cfg(config, "connect_timeout_sec", 15)
        if (host != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535
                or version != "1.20.1" or not _USERNAME_RE.fullmatch(username)
                or auth_mode not in {"microsoft", "offline"}
                or type(raw_timeout) is not int
                or not 1 <= raw_timeout <= MAX_CONNECT_TIMEOUT_SEC):
            raise GuardDenied("connect_config_invalid")
        return {"host": host, "port": port, "version": version,
                "username": username, "auth_mode": auth_mode,
                "timeout_sec": int(raw_timeout)}

    def _actor_uuid(self) -> str:
        return _safe_uuid(_cfg(self.config, "owner_player_uuid", ""))

    def _owner_human_uuid(self) -> str:
        """The owner human's own avatar, or empty.

        Delegates to the single canonical contract in
        :func:`pawbot.minecraft_policy.owner_human_identity`; the resident must
        never re-implement a second, weaker rule (G8).  That contract accepts
        only the exact normalized ``minecraft.owner_human_uuid``, rejects a
        value equal to the bot actor and rejects any conflict with an explicit
        ``owner_mappings`` link to a different complete QQ UID.  A nickname, a
        QQ UID suffix or an inferred match can never promote a player to owner.
        """
        return owner_human_identity(self.config)

    def _mapping_links(self) -> dict:
        """Explicit local UUID <-> QQ UID links, minus ambiguous entries.

        Pair parsing comes from the same canonical contract
        (:func:`pawbot.minecraft_policy.explicit_mapping_pairs`).  A UUID mapped
        to more than one QQ UID, or one QQ UID mapped to more than one UUID, is
        a duplicate/conflicting mapping: neither side is linked.  Only the
        boolean link is ever persisted or shown to a model, and it never grants
        the owner role by itself.
        """
        by_uuid: dict[str, set] = {}
        by_uid: dict[str, set] = {}
        for uuid, uid in explicit_mapping_pairs(
                _cfg(self.config, "owner_mappings", {})):
            by_uuid.setdefault(uuid, set()).add(uid)
            by_uid.setdefault(uid, set()).add(uuid)
        links = {}
        for uuid, uids in by_uuid.items():
            if len(uids) != 1:
                continue
            uid = next(iter(uids))
            if by_uid.get(uid) != {uuid}:
                continue
            links[uuid] = True
        return links

    def _explicitly_mapped(self, player_uuid: str) -> bool:
        uuid = _safe_uuid(player_uuid)
        return bool(uuid) and uuid in self._mapping_links()

    def _role_for(self, player_uuid: str) -> str:
        """Role from the exact configured UUIDs only, never from a name."""
        uuid = _safe_uuid(player_uuid)
        if not uuid:
            return "player"
        if uuid == self._actor_uuid():
            return "self"
        if uuid == self._owner_human_uuid():
            return "owner"
        return "player"

    def _owner_present(self, players) -> bool:
        owner = self._owner_human_uuid()
        return bool(owner) and any(row.get("uuid") == owner for row in players or [])

    @staticmethod
    def _role_baseline(role: str) -> dict:
        if role == "self":
            return {"familiarity": .5, "trust": .5, "closeness": .5, "boundary": .3}
        if role == "owner":
            return {"familiarity": .6, "trust": .7, "closeness": .5, "boundary": .4}
        return {"familiarity": .1, "trust": .25, "closeness": .1, "boundary": .5}

    def _session_id(self) -> str:
        world = str(getattr(getattr(self.guard, "settings", None), "world_id", "") or "")
        if not _WORLD_ID_RE.fullmatch(world):
            world = "disposable"
        return "minecraft_" + world[:64]

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        if _cfg(self.config, "enabled", False) is not True:
            return False
        if self._latched or getattr(self.bot, "_minecraft_estop", False) is True:
            raise GuardDenied("resident_latched")
        if self._started:
            return True
        actor = self._actor_uuid()
        if not actor or not owner_identity_matches(self.config, actor):
            raise GuardDenied("owner_identity_unbound")
        params = self._connect_params()
        settings = getattr(self.guard, "settings", None)
        if settings is not None and hasattr(settings, "validate"):
            settings.validate()
        world_id = str(getattr(settings, "world_id", "") or "") or str(
            _cfg(self.config, "test_world_id", "") or "")
        if not _WORLD_ID_RE.fullmatch(world_id):
            raise GuardDenied("invalid_guard_settings")
        self.bindings.bind(actor, world_id)
        try:
            await self.sidecar.start()
        except Exception:
            with contextlib.suppress(Exception):
                await self.sidecar.stop(graceful=False)
            raise
        try:
            await self._connect(params, attempts=1)
        except BaseException:
            with contextlib.suppress(Exception):
                await self.sidecar.stop(graceful=False)
            raise
        self._stop.clear()
        self._started = True
        self._task = tasks.spawn(self._run(), name="minecraft-resident", owner="minecraft")
        log.info("Minecraft resident connected to guard-verified registered world")
        return True

    async def _connect(self, params: dict, *, attempts: int = 1):
        async def prepare():
            actor = self._actor_uuid()
            if not owner_identity_matches(self.config, actor):
                raise GuardDenied("owner_identity_unbound")
            # Cheap, non-snapshot preflight: the child obtains the single authoritative `connect`
            # permit (which itself performs the mandatory fresh save+snapshot) immediately before it
            # opens the game socket. Asking for `backup` here as well would copy the world two or
            # three times per connect for no security gain.
            await self.guard.request("observe")

        connect_with_retry = getattr(self.sidecar, "connect_with_retry", None)
        timeout = max(1, int(params["timeout_sec"])) + 5
        if callable(connect_with_retry):
            result = await connect_with_retry(params, prepare, attempts=attempts,
                                              timeout=timeout)
        else:
            await prepare()
            result = await self.sidecar.request("connect", params, timeout=timeout)
        if (not isinstance(result, dict) or result.get("connected") is not True
                or result.get("backup_completed") is not True):
            raise SidecarError("connect_rejected")
        expected_actor = _safe_uuid(getattr(getattr(self.guard, "settings", None),
                                           "actor_uuid", "")) or self._actor_uuid()
        if _safe_uuid(result.get("uuid")) != expected_actor:
            raise SidecarError("actor_mismatch")
        expected_world = str(getattr(getattr(self.guard, "settings", None),
                                     "world_id", "") or "") or str(
            _cfg(self.config, "test_world_id", "") or "")
        if expected_world and result.get("world_id") != expected_world:
            raise SidecarError("world_mismatch")
        self._last_backup = self.clock()
        # A (re)connect starts a fresh Node process/adapter: its process-only
        # `home` is gone, so the next verified observation must re-seed.
        # The failure budget deliberately survives reconnects: resetting it here
        # would let one unreachable home re-trigger the same abortive approach
        # after every reconnect.
        self._home_seeded = False
        return result

    async def stop(self):
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        revoked = False
        if self._started:
            with contextlib.suppress(Exception):
                await self.guard.request("revoke")
                revoked = True
            with contextlib.suppress(Exception):
                await self.sidecar.stop(graceful=True)
            self._started = False
        else:
            with contextlib.suppress(Exception):
                await self.sidecar.stop(graceful=False)
        with contextlib.suppress(OSError, ValueError):
            self.state.save()
        return {"stopped": True, "revoked": revoked}

    async def estop(self):
        """Host-level emergency stop; latches until the PawBot process restarts."""
        self._latched = True
        result = await self.stop()
        result["latched"] = True
        return result

    async def emergency_stop(self):
        return await self.estop()

    @property
    def latched(self) -> bool:
        return self._latched

    @property
    def started(self) -> bool:
        return self._started

    async def _safe_fail(self, exc: BaseException):
        # Static fields only: no exception text (it can contain paths/URLs).
        log.warn(f"Minecraft resident loop stopped safely: {type(exc).__name__}")
        with contextlib.suppress(Exception):
            await self.guard.request("revoke")
        with contextlib.suppress(Exception):
            await self.sidecar.stop(graceful=False)
        self._started = False
        self._latched = True
        with contextlib.suppress(OSError, ValueError):
            self.state.save()

    async def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    if not self._sidecar_alive():
                        if not await self._reconnect():
                            await self._sleep(5.0)
                            continue
                    await self.tick()
                    await self._sleep(self._tick_interval_seconds())
                except asyncio.CancelledError:
                    raise
                except GuardBusy:
                    # Never treat a signed busy window as a loop-fatal guard failure.
                    await self._sleep(1.0)
                except GuardDenied:
                    await self._safe_fail(GuardDenied("guard_denied"))
                    return
                except SidecarError as exc:
                    log.warn(f"Minecraft sidecar unavailable: {type(exc).__name__}")
                    with contextlib.suppress(Exception):
                        await self.sidecar.stop(graceful=False)
                    if not await self._reconnect():
                        await self._sleep(5.0)
                except Exception as exc:  # noqa: BLE001 - fail closed, never crash core
                    await self._safe_fail(exc)
                    return
        except asyncio.CancelledError:
            raise

    def _sidecar_alive(self) -> bool:
        return bool(getattr(self.sidecar, "alive", True))

    async def _reconnect(self) -> bool:
        params = self._connect_params()
        attempts = self._reconnect_attempts_allowed()
        base = _num(_cfg(self.config, "reconnect_backoff_sec", 1.0), 1.0, low=0.05, high=30)
        for attempt in range(attempts):
            self._reconnect_attempts += 1
            await self._sleep(min(30.0, base * (2 ** attempt)))
            if self._stop.is_set():
                return False
            try:
                await self._connect(params, attempts=1)
                log.info("Minecraft sidecar reconnected under a fresh guard permit")
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warn(f"Minecraft reconnect attempt failed: {type(exc).__name__}")
                with contextlib.suppress(Exception):
                    await self.sidecar.stop(graceful=False)
        return False

    async def _sleep(self, seconds: float):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, float(seconds)))
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------ transport
    async def tick(self):
        """One observe -> appraise -> decide -> act cycle (test-injectable)."""
        # No periodic snapshot request here: the Java guard owns the single backup schedule.
        await self.guard.request("observe")
        observed = await self.sidecar.request("observe", {}, timeout=OBSERVE_TIMEOUT_SEC)
        safe = safe_observation(observed)
        self._last_observe = self.clock()
        # Seed Node's process-only home before any intent can select `home`.
        home_seed = await self._ensure_home_seed(safe)
        outcome = await self._cycle(safe)
        return {"observation": safe, "outcome": outcome, "home_seed": home_seed}

    # ------------------------------------------------------------ home seeding
    def _home_point(self):
        place = self._home()
        if not place:
            return None
        point = _canonical_point(place)
        dimension = _safe_dimension(place.get("dimension"))
        if point is None or not dimension:
            return None
        point["dimension"] = dimension
        return point

    def _home_seed_exhausted(self) -> bool:
        """True while a failed seed must not hammer the child.

        A reconnect resets the counter; otherwise a long quiet period allows a
        fresh, still-bounded set of attempts instead of disabling home forever.
        """
        if self._home_seed_failures < MAX_HOME_SEED_ATTEMPTS:
            return False
        if self.clock() - self._home_seed_last_failure >= HOME_SEED_RETRY_COOLDOWN:
            self._home_seed_failures = 0
            self._home_seed_reason = ""
            return False
        return True

    async def _ensure_home_seed(self, observation):
        """Give Node an explicit home, from the persisted place only.

        The child keeps `home` in process memory, so it must be (re)seeded
        after the first verified observation and after every reconnect.  The
        seed itself is read-only (Node's `set_home` uses an ``observe``
        permit), and success is only reported after the child accepted it.
        """
        if self._home_seeded:
            return None
        target = self._home_point()
        if target is None:
            # No persisted home yet: the first valid observation defines it.
            self._remember_home(observation)
            target = self._home_point()
        if target is None:
            return None
        position = _canonical_point(observation.get("position"))
        if position is None:
            return {"seeded": False, "reason": "no_position"}
        dimension = _safe_dimension(observation.get("dimension"))
        if dimension and dimension != target["dimension"]:
            self._home_seed_reason = "home_dimension_mismatch"
            return {"seeded": False, "reason": self._home_seed_reason}
        distance = _distance(position, target)
        if distance is None:
            return {"seeded": False, "reason": "no_position"}
        if distance > HOME_SEED_TOLERANCE:
            # Only walk back to the persisted home; never re-point `home` at
            # wherever the bot happens to be standing.  The approach is a
            # normal guarded write, and the next observation seeds.
            if _cfg(self.config, "write_enabled", False) is not True:
                self._home_seed_reason = "home_far_write_disabled"
                return {"seeded": False, "reason": self._home_seed_reason,
                        "distance": round(distance, 2)}
            if self._home_seed_exhausted():
                return {"seeded": False, "reason": self._home_seed_reason or
                        "home_seed_attempts_exhausted"}
            if distance > HOME_APPROACH_MAX_DISTANCE:
                # Bounded refusal: a long approach cannot finish inside one skill
                # budget, and a doomed call would only cost a timeout.
                self._home_seed_failures += 1
                self._home_seed_last_failure = self.clock()
                self._home_seed_reason = "home_too_far"
                log.warn(f"Minecraft home approach skipped: home_too_far "
                         f"distance={round(distance, 1)}")
                return {"seeded": False, "reason": self._home_seed_reason,
                        "distance": round(distance, 2)}
            skill, args = self._approach_step(position, target)
            if not skill:
                return {"seeded": False, "reason": "home_approach_unavailable"}
            result = await self._invoke_skill(skill, args)
            if result.get("ok") is not True:
                self._home_seed_failures += 1
                self._home_seed_last_failure = self.clock()
                self._home_seed_reason = str(result.get("reason") or "home_approach_failed")
                log.warn(f"Minecraft home approach failed: {self._home_seed_reason}")
                return {"seeded": False, "reason": self._home_seed_reason,
                        "distance": round(distance, 2)}
            return {"seeded": False, "reason": "approach_home",
                    "distance": round(distance, 2)}
        if self._home_seed_exhausted():
            return {"seeded": False, "reason": "home_seed_attempts_exhausted"}
        result = await self._invoke_skill("set_home", {}, action="observe")
        if result.get("ok") is not True:
            self._home_seed_failures += 1
            self._home_seed_last_failure = self.clock()
            self._home_seed_reason = str(result.get("reason") or "home_seed_failed")
            self._action_log.append({"sig": "set_home:seed_failed", "ok": False,
                                     "at": self.clock()})
            self._action_log = self._action_log[-MAX_ACTIONS:]
            log.warn(f"Minecraft home seed failed: {self._home_seed_reason}")
            return {"seeded": False, "reason": self._home_seed_reason,
                    "distance": round(distance, 2)}
        self._home_seeded = True
        self._home_seed_failures = 0
        self._home_seed_reason = ""
        self._home_seed_last_failure = 0.0
        return {"seeded": True, "skill": "set_home",
                "distance": round(distance, 2)}

    async def _cycle(self, observation: dict):
        state = self.state.data
        now = self.clock()
        dt = _clamp(now - _num(state.get("last_tick"), now, low=0), 0.0, 300.0)
        state["last_tick"] = now
        self._drift(dt)
        self._update_social(observation)
        self._update_places(observation)
        self._ensure_project(observation)
        self._ensure_promise(observation)
        await self._appraise(observation)

        candidates = self._propose(observation, self.workspace)
        ranked = self.planner.rank(observation, self.state, self._affect(),
                                   self.workspace, candidates)
        self._build_workspace(observation, candidates, ranked)

        state["counters"]["cycles"] += 1
        if self._idle_cooldown > 0:
            self._idle_cooldown -= 1
            state["recent_actions"].append("cooldown")
            self.state.save()
            return {"executed": False, "reason": "cooldown", "skill": ""}
        if not self._due_decision(observation):
            self.state.save()
            return {"executed": False, "reason": "not_due", "skill": ""}

        decision = await self.decide(observation)
        self._last_decision = now
        state["counters"]["decisions"] += 1
        log.info("Minecraft decision", **decision.safe_log())
        outcome = await self._execute(decision, observation)
        self._last_outcome = outcome
        log.info("Minecraft action", intent=decision.intent, skill=decision.skill,
                 ok=outcome.get("ok") is True, executed=outcome.get("executed") is True,
                 reason=_safe_text(outcome.get("reason"), 48),
                 project_id=decision.project_id, promise_id=decision.promise_id)
        if outcome.get("executed"):
            await self._appraise(observation, outcome=outcome)
        await self._reflect(decision, observation, outcome)
        self.state.save()
        return outcome

    # ------------------------------------------------------------ suggestions
    def _propose(self, observation, workspace=None):
        return self.impulse_engine.propose(
            observation, self.state, self._affect(),
            workspace if workspace is not None else self.workspace,
            hints=self._suggestion_hints())

    def _suggestion_hints(self):
        """At most two unexpired accepted suggestions, as (intent, score)."""
        now = self.clock()
        hints = []
        for row in reversed(self.state.data.get("suggestions", [])):
            if len(hints) >= 2:
                break
            if row.get("status") != "accepted":
                continue
            if _num(row.get("expires_at"), 0, low=0) <= now:
                continue
            hint = row.get("hint")
            if hint in INTENTS:
                # The weight follows the *current* exact owner field, never a
                # stored role that might predate a config change.
                owner = self._role_for(row.get("player_uuid")) == "owner"
                hints.append((hint, .55 if owner else .45))
        return hints

    def consider_suggestion(self, player_uuid, text, *, now=None):
        """Process a player's request as a bounded, read-only suggestion.

        This is the only intake for "someone asked the resident to do
        something".  It returns a disposition (accepted / deferred / refused /
        negotiating) and never calls the guard or the sidecar, never produces a
        skill/args pair and therefore never grants write or PvP authority: an
        accepted hint is still a plain preference that must pass `_execute`'s
        normal guard + policy checks.  Slash commands, control characters and
        adversarial wording are refused outright; unknown players stay
        separate and cannot steer the resident.
        """
        moment = self.clock() if now is None else _num(now, self.clock(), low=0)
        if _cfg(self.config, "enabled", False) is not True:
            return self._suggestion_reply("refused", "disabled")
        uuid = _safe_uuid(player_uuid)
        if not uuid:
            return self._suggestion_reply("refused", "unknown_player")
        if uuid == self._actor_uuid():
            return self._suggestion_reply("refused", "self")
        known = {row.get("uuid") for row in self.state.data["players"]
                 if row.get("uuid")}
        if uuid not in known:
            return self._suggestion_reply("refused", "unknown_player")
        if not isinstance(text, str):
            return self._suggestion_reply("refused", "invalid_text")
        clean = _one_line(text, 200)
        if not clean:
            return self._suggestion_reply("refused", "empty")
        if (re.search(r"[\u0000-\u001f\u007f-\u009f\u2028\u2029]", clean)
                or "/" in clean or "\\" in clean
                or _FORBIDDEN_REQUEST_RE.search(clean)
                or any(word in clean for word in _FORBIDDEN_REQUEST_WORDS)):
            return self._suggestion_reply("refused", "not_allowed")
        role = self._role_for(uuid)
        lowered = clean.lower()
        hint = ""
        for words, intent in _SUGGESTION_HINTS:
            if any(word in lowered for word in words):
                hint = intent
                break
        if not hint:
            return self._store_suggestion(
                uuid, role, clean, "negotiating", reason="unclear",
                counter="你想让我做什么？说得具体一点。", moment=moment)
        hot = (self._idle_cooldown > 0
               or self.state.data["drives"].get("energy", 1) < .25)
        busy = bool(ImpulseEngine._first_project(self.state))
        if role != "owner" and (hot or busy):
            return self._store_suggestion(
                uuid, role, clean, "deferred", reason="busy", hint=hint,
                counter="我手上还有事，等一会儿。", moment=moment)
        return self._store_suggestion(uuid, role, clean, "accepted",
                                      reason="ok", hint=hint, moment=moment)

    @staticmethod
    def _suggestion_reply(status, reason, *, hint="", counter="", row=None):
        row = row or {}
        return {"status": status, "reason": reason, "hint": hint,
                "counter": counter, "suggestion_id": row.get("id", ""),
                "expires_at": row.get("expires_at", 0) or 0,
                "due_at": row.get("due_at", 0) or 0}

    def _store_suggestion(self, uuid, role, text, status, *, reason="", hint="",
                          counter="", moment=None):
        moment = self.clock() if moment is None else moment
        index = _int(self.state.data.get("next_suggestion_id"), 1, low=1)
        self.state.data["next_suggestion_id"] = index + 1
        row = {
            "id": f"s{index}", "player_uuid": uuid, "role": role,
            "text": _safe_text(text, 160), "status": status, "hint": hint,
            "created_at": moment,
            "due_at": (moment + 120 if status == "deferred" else 0),
            "expires_at": (moment + 180 if status == "accepted" else 0),
            "reason": reason, "counter": _safe_text(counter, 160),
        }
        self.state.data["suggestions"].append(row)
        del self.state.data["suggestions"][:-MAX_SUGGESTIONS]
        self.state.save()
        return self._suggestion_reply(status, reason, hint=hint,
                                      counter=row["counter"], row=row)

    # ------------------------------------------------------------ home helpers
    def _remember_home(self, observation):
        position = _canonical_point(observation.get("position"))
        dimension = _safe_dimension(observation.get("dimension"))
        if position is None or not dimension:
            return None
        home = {"kind": "home", "x": position["x"], "y": position["y"],
                "z": position["z"], "dimension": dimension, "visits": 1,
                "last_seen": self.clock()}
        self.state.data["places"].append(home)
        del self.state.data["places"][:-MAX_PLACES]
        return home

    def _home_step(self, observation):
        """A home skill only once the child accepted `set_home` this session."""
        home = self._home_point()
        if home is None:
            return "", {}
        position = _canonical_point(observation.get("position"))
        if position is None:
            return "", {}
        dimension = _safe_dimension(observation.get("dimension"))
        if dimension and dimension != home["dimension"]:
            return "", {}
        distance = _distance(position, home)
        if self._home_seeded and (distance is None or distance <= HOME_RETURN_RADIUS):
            return "home", {}
        if _cfg(self.config, "write_enabled", False) is not True:
            return "", {}
        return self._approach_step(position, home)

    @staticmethod
    def _approach_step(position, home):
        distance = _distance(position, home)
        if not distance or distance <= 0:
            return "", {}
        ratio = min(1.0, 10.0 / distance)
        x = int(round(position["x"] + (home["x"] - position["x"]) * ratio))
        z = int(round(position["z"] + (home["z"] - position["z"]) * ratio))
        args = {"x": float(x), "y": float(position["y"]), "z": float(z),
                "radius": 1.0}
        if (x, z) == (int(round(position["x"])), int(round(position["z"]))):
            return "", {}
        return ("move", args) if validate_skill("move", args) else ("", {})

    def _drift(self, dt: float):
        drives = self.state.data["drives"]
        if dt <= 0:
            return
        drives["energy"] = _clamp(drives["energy"] - 0.00012 * dt, 0, 1)
        drives["boredom"] = _clamp(drives["boredom"] + 0.00020 * dt, 0, 1)
        drives["loneliness"] = _clamp(drives["loneliness"] + 0.00010 * dt, 0, 1)
        drives["creative_tension"] = _clamp(drives["creative_tension"] + 0.00008 * dt, 0, 1)
        residue = self.state.data["affect_residue"]
        factor = 0.5 ** (dt / 600.0)
        residue["fear"] = _clamp(residue.get("fear", 0) * factor, 0, 1)
        residue["warmth"] = _clamp(residue.get("warmth", 0) * factor, 0, 1)
        residue["updated_at"] = self.clock()

    def _affect(self) -> dict:
        emotion = getattr(self.bot, "emotion", None)
        snapshot = None
        if emotion is not None:
            with contextlib.suppress(Exception):
                snapshot = emotion.snapshot()
        values = {}
        if snapshot is not None:
            with contextlib.suppress(Exception):
                values = {str(key): round(float(value), 1)
                          for key, value in snapshot.values.items()}
        return {
            "dominant": getattr(snapshot, "dominant", None),
            "secondary": getattr(snapshot, "secondary", None),
            "intensity": getattr(snapshot, "intensity", None),
            "values": values,
            "residue": {"fear": round(self.state.data["affect_residue"].get("fear", 0), 2),
                        "warmth": round(self.state.data["affect_residue"].get("warmth", 0), 2)},
        }

    # ------------------------------------------------------------ appraisal
    async def _appraise(self, observation, *, force: bool = False, outcome=None):
        now = self.clock()
        if not force and now - self._last_appraisal < 10:
            return self._last_emotion_delta
        result = self.appraiser.appraise(observation, self.state, outcome=outcome)
        deltas = self.appraiser.deltas(result, self.state.data["personality"],
                                       self.state.data["affect_residue"])
        emotion = getattr(self.bot, "emotion", None)
        if emotion is not None and deltas:
            with contextlib.suppress(Exception):
                emotion.apply(deltas)
        residue = self.state.data["affect_residue"]
        if result.threat:
            residue["fear"] = _clamp(residue.get("fear", 0) + 0.45, 0, 1)
            residue["warmth"] = _clamp(residue.get("warmth", 0) - 0.05, 0, 1)
        if result.social:
            residue["warmth"] = _clamp(residue.get("warmth", 0) + 0.15, 0, 1)
        if result.failure:
            residue["fear"] = _clamp(residue.get("fear", 0) + 0.10, 0, 1)
        # Weather/comfort comes from the in-game observation, not the host clock.
        env = environment(observation)
        if env["storms"] and env["sheltered"] is True:
            residue["warmth"] = _clamp(residue.get("warmth", 0) + 0.06, 0, 1)
        elif env["storms"]:
            residue["fear"] = _clamp(residue.get("fear", 0) + 0.08, 0, 1)
        residue["updated_at"] = now
        self._last_appraisal = now
        self._last_appraisal_result = result
        self._last_emotion_delta = dict(deltas)
        return deltas

    def _due_decision(self, observation) -> bool:
        return self.clock() - self._last_decision >= self._decision_interval_seconds()

    # ------------------------------------------------------------ attention
    def _build_workspace(self, observation, impulse, ranked):
        workspace = self.workspace
        workspace.reset()
        drives = self.state.data["drives"]
        personality = self.state.data["personality"]
        appraisal = self._last_appraisal_result or Appraisal()
        affect = self._affect()

        health, food = observation["health"], observation["food"]
        body_salience = 0.85 if health <= 8 or food <= 7 else 0.45
        workspace.submit("perception", f"身体 {int(health)}/{int(food)}",
                         body_salience + 0.1 * (1 - min(health, 20) / 20), "perception")
        strongest = max(_DRIVE_KEYS, key=lambda key: drives.get(key, 0))
        workspace.submit("drive", strongest, 0.35 + .55 * drives.get(strongest, 0), "drive")
        if affect.get("dominant"):
            workspace.submit("affect", f"{affect['dominant']} {affect.get('intensity') or ''}",
                             0.45 + .25 * (1 if affect.get("intensity") else 0), "affect")
        else:
            workspace.submit("self", self.state.data["self_narrative"], .30, "self")

        project = ImpulseEngine._first_project(self.state)
        if project:
            workspace.submit("project", str(project.get("title", "")),
                             0.55 + .3 * personality.get("industriousness", .5), "project")
        promise = ImpulseEngine._due_promise(self.state)
        if promise:
            workspace.submit("promise", str(promise.get("title", "")),
                             0.60 + .2 * personality.get("attachment", .5), "promise")
        players = nearby_players(observation)
        known = ImpulseEngine._known_players(players, self.state)
        if players:
            workspace.submit("social", "附近有人" if not known else "附近有认识的人",
                             0.40 + .2 * appraisal.social, "social")
        if _threat_count(observation) > 0:
            workspace.submit("threat", "附近有敌对生物", .95, "perception")
        habit = next((key for key, row in self.state.data["habits"].items()
                      if row.get("count", 0) >= 2), "")
        if habit:
            workspace.submit("habit", habit, .38, "habit")
        env = environment(observation)
        if env["weather"] or env["phase"] or env["roof"] is not None:
            label = " ".join(part for part in (
                env["phase"], env["weather"],
                "有遮挡" if env["roof"] is True else "") if part) or "环境状态"
            workspace.submit("environment", label,
                             0.42 + (0.15 if env["comfort"] < 0.35 else 0),
                             "perception")
        for candidate in ranked[:3]:
            workspace.submit("intent", candidate.intent, candidate.score, candidate.source)
        # Guarantee the 3-5 item contract even on a quiet world.
        while len(workspace) < WORKSPACE_MIN:
            workspace.submit("self", "这一刻的注意力", .20, "self")
        return workspace

    # ------------------------------------------------------------ decision
    async def decide(self, observation, *, workspace=None, allow_remote=True):
        if workspace is None:
            impulse = self._propose(observation)
            ranked = self.planner.rank(observation, self.state, self._affect(),
                                       self.workspace, impulse)
            surface = self._build_workspace(observation, impulse, ranked)
        else:
            surface = workspace
        impulse = self._propose(observation, surface)
        ranked = self.planner.rank(observation, self.state, self._affect(), surface, impulse)
        # Build the shared context even when the remote model is off: the same
        # bounded self-model/workspace/affect representation is used by local
        # fallback decisions and remains stable if a model is enabled later.
        context = self._context(observation, surface)
        deliberate = None
        if allow_remote and self.jev.can_decide:
            deliberate = await self.jev.decide(context)
        if deliberate is None:
            deliberate = ranked[0] if ranked else Candidate("idle", .1, "fallback", "发呆")
        decision = self.arbiter.resolve(deliberate, impulse, ranked,
                                        self.state.data["personality"],
                                        self.state.data["affect_residue"],
                                        self._last_appraisal_result or Appraisal())
        decision.emotion_delta = dict(self._last_emotion_delta)
        return decision

    def _context(self, observation, workspace=None):
        """Model context: no private thought, QQ UID, save path or raw exception."""
        persona = getattr(self.bot, "persona", None)
        persona_text = ""
        if persona is not None:
            with contextlib.suppress(Exception):
                persona_text = _safe_text(persona.core(), 300)
        players = nearby_players(observation)
        state = self.state.data
        project = ImpulseEngine._first_project(self.state)
        promise = ImpulseEngine._due_promise(self.state)
        surface = workspace if workspace is not None else self.workspace
        world_facts = {
            "health": round(observation["health"], 1),
            "food": round(observation["food"], 1),
            "position": observation.get("position"),
            "dimension": observation.get("dimension"),
            "inventory": observation.get("inventory", [])[:64],
            "nearby_entities": [
                {"type": row.get("type"), "distance": row.get("distance")}
                for row in observation.get("nearby_entities", [])[:32]
            ],
            "nearby_blocks": [
                {"name": row.get("name"), "distance": row.get("distance")}
                for row in observation.get("nearby_blocks", [])[:32]
            ],
        }
        if "time_of_day" in observation:
            world_facts["time_of_day"] = observation["time_of_day"]
        if "weather" in observation:
            world_facts["weather"] = observation["weather"]
        env = environment(observation)
        world_facts["environment"] = {
            "phase": env["phase"],
            "game_hour": env["game_hour"],
            "weather": env["weather"],
            "light": env["light"],
            "roof": env["roof"],
            "comfort": env["comfort"],
        }
        events = observation.get("events")
        world_facts["recent_events"] = [
            {"type": row.get("type")} for row in (events if isinstance(events, list) else [])
            if isinstance(row, dict) and row.get("type")
        ][:MAX_OBSERVATION_EVENTS]
        affordances = {skill for skills in SKILLS.values() for skill in skills}
        # `set_home` is an internal seed (observe-only), never an affordance.
        affordances.discard("set_home")
        if _cfg(self.config, "game_chat_enabled", False) is not True:
            affordances.discard("chat")
        if (_cfg(self.config, "pvp_enabled", False) is not True
                or _cfg(self.config, "write_enabled", False) is not True):
            affordances.discard("attack")
        if _cfg(self.config, "write_enabled", False) is not True:
            affordances -= {"dig", "collect", "craft", "place", "farm", "chest",
                            "breed", "eat", "respawn", "follow",
                            "move", "explore"}
        # `home` is only real while the child's process has an accepted home.
        if not self._home_seeded:
            affordances.discard("home")
        return {
            "self_model": {
                "narrative": state["self_narrative"],
                "persona": persona_text,
                "focus": {
                    "project": project.get("title") if project else "",
                    "promise": promise.get("title") if promise else "",
                },
                "body": {"health": world_facts["health"], "food": world_facts["food"]},
                "social_self": {"known_players": len(
                    ImpulseEngine._known_players(players, self.state)),
                    "actor_present": any(row["uuid"] == self._actor_uuid()
                                         for row in players),
                    "owner_present": self._owner_present(players)},
            },
            "affect": self._affect(),
            "workspace": surface.as_context() if hasattr(surface, "as_context") else [],
            "drives": dict(state["drives"]),
            "personality": dict(state["personality"]),
            "active_projects": [
                {"title": row.get("title"), "goal": row.get("goal"),
                 "progress": row.get("progress", 0), "status": row.get("status")}
                for row in state["projects"] if row.get("status") == "active"
            ][:4],
            "memory": {
                "places": [{"kind": row.get("kind"), "visits": row.get("visits", 0)}
                           for row in state["places"][:8]],
                "promises": [row.get("title") for row in state["promises"]
                             if row.get("status") == "active"][:4],
                "journal": [row.get("text") for row in state["journal"][-3:]],
            },
            "social_models": [
                {"role": row.get("role"), "explicit_link": bool(row.get("explicit_link")),
                 "familiarity": round(float(row.get("familiarity", 0)), 2),
                 "trust": round(float(row.get("trust", 0)), 2),
                 "closeness": round(float(row.get("closeness", 0)), 2)}
                for row in state["players"][-8:]
            ],
            "world_facts": world_facts,
            "affordances": sorted(affordances),
            "recent_actions": list(state["recent_actions"][-8:]),
        }

    # ------------------------------------------------------------ skill choice
    def _select_skill(self, intent: str, observation: dict):
        state = self.state.data
        if intent == "continue_project":
            project = ImpulseEngine._first_project(self.state)
            if not project:
                return "", {}, {"project_id": "", "promise_id": ""}
            step = self._step_for(project, observation, kind="project")
            return step[0], step[1], {"project_id": str(project.get("id", "")),
                                      "promise_id": ""}
        if intent == "fulfill_promise":
            promise = ImpulseEngine._due_promise(self.state)
            if not promise:
                return "", {}, {"project_id": "", "promise_id": ""}
            step = self._step_for(promise, observation, kind="promise")
            return step[0], step[1], {"project_id": "",
                                      "promise_id": str(promise.get("id", ""))}
        if intent == "satisfy_drive":
            skill, args = self._drive_skill(observation)
            return skill, args, {"project_id": "", "promise_id": ""}
        if intent == "socialize":
            known = ImpulseEngine._known_players(nearby_players(observation), self.state)
            if known:
                owner = self._owner_human_uuid()
                chosen = next((row for row in known if row["uuid"] == owner), known[0])
                return "follow", {"target_uuid": chosen["uuid"],
                                  "duration_sec": 5}, {"project_id": "", "promise_id": ""}
            return "", {}, {"project_id": "", "promise_id": ""}
        if intent == "explore":
            position = observation.get("position")
            if position:
                return "explore", self._offset_coords(position, 10, 10), {
                    "project_id": "", "promise_id": ""}
            return "", {}, {"project_id": "", "promise_id": ""}
        if intent == "rest":
            if observation["food"] < 15:
                return "eat", {}, {"project_id": "", "promise_id": ""}
            skill, args = self._home_step(observation)
            return skill, args, {"project_id": "", "promise_id": ""}
        if intent == "recover":
            if observation["health"] <= 4:
                return "respawn", {}, {"project_id": "", "promise_id": ""}
            if observation["food"] < 10:
                return "eat", {}, {"project_id": "", "promise_id": ""}
            # PvP is only ever the *exact* locally configured target, and only
            # with both strict flags on: no unknown player is ever selected.
            target = self._pvp_target_nearby(observation)
            if target:
                return "attack", {"target_uuid": target}, {
                    "project_id": "", "promise_id": ""}
            skill, args = self._home_step(observation)
            return skill, args, {"project_id": "", "promise_id": ""}
        if intent == "idle":
            return "", {}, {"project_id": "", "promise_id": ""}
        return "", {}, {"project_id": "", "promise_id": ""}

    def _pvp_target_nearby(self, observation) -> str:
        """The configured PvP target inside the child's 3-block attack range."""
        if _cfg(self.config, "pvp_enabled", False) is not True:
            return ""
        if _cfg(self.config, "write_enabled", False) is not True:
            return ""
        allowed = allowed_pvp_targets(_cfg(self.config, "pvp_target_uuids", []))
        actor = self._actor_uuid()
        if not allowed:
            return ""
        best, best_distance = "", None
        for row in nearby_players(observation):
            uuid = row.get("uuid")
            if not uuid or uuid == actor or uuid not in allowed:
                continue
            distance = _finite(row.get("distance"))
            if distance is None:
                distance = _distance(observation.get("position"), row.get("position"))
            if distance is None or distance > 3.0:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = uuid, distance
        return best

    def _step_for(self, row, observation, *, kind):
        steps = row.get("steps") if isinstance(row.get("steps"), list) else []
        index = _int(row.get("step"), 0, low=0)
        if index >= len(steps):
            steps = self._project_steps(
                str(row.get("kind", "")),
                row.get("anchor") or observation.get("position"), observation)
            row["steps"] = steps
            row["step"] = index = 0
        if not steps:
            return "", {}
        step = steps[min(index, len(steps) - 1)]
        skill = str(step.get("skill") or "")
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        if skill == "home":
            # A stored `home` step is only executable after seeding; otherwise
            # it degrades to walking back toward the persisted home.
            return self._home_step(observation)
        if not validate_skill(skill, args):
            return "", {}
        return skill, dict(args)

    def add_promise(self, title, *, due_at=0, player_uuid="", steps=None) -> str:
        """Register an explicit local promise without exposing a model tool.

        ``title`` is sanitized (paths/long numeric identifiers are redacted).
        Unknown skills or invalid arguments are dropped; the promise cannot
        widen the skill allow-list.
        """
        text = _safe_text(title, 160)
        if not text:
            return ""
        target = _safe_uuid(player_uuid)
        clean = _clean_steps(steps or [], limit=6)
        if not clean:
            if target:
                clean = [{"skill": "follow",
                          "args": {"target_uuid": target, "duration_sec": 5}},
                         {"skill": "home", "args": {}}]
            else:
                clean = [{"skill": "home", "args": {}}, {"skill": "eat", "args": {}}]
        index = _int(self.state.data.get("next_promise_id"), 1, low=1)
        self.state.data["next_promise_id"] = index + 1
        self.state.data["promises"].append({
            "id": f"q{index}", "title": text, "status": "active",
            "created_at": self.clock(), "due_at": _num(due_at, 0, low=0),
            "step": 0, "attempts": 0, "player_uuid": target, "steps": clean,
        })
        del self.state.data["promises"][:-MAX_PROMISES]
        return f"q{index}"

    def _drive_skill(self, observation):
        drives = self.state.data["drives"]
        inventory = observation.get("inventory", [])
        position = observation.get("position")
        if drives.get("energy", 1) < .35:
            if observation["food"] < 18:
                return "eat", {}
            return self._home_step(observation)
        if drives.get("hoarding_urge", 0) > .6:
            return "collect", {"block": "dirt", "count": 3}
        if drives.get("creative_tension", 0) > .6:
            block = self._inventory_block(inventory)
            if block and position:
                return "place", {"x": int(position["x"] + 1), "y": int(position["y"]),
                                 "z": int(position["z"]), "face_x": 0, "face_y": 1,
                                 "face_z": 0, "item": block}
            return "collect", {"block": "dirt", "count": 2}
        if drives.get("wanderlust", 0) > .6 or drives.get("boredom", 0) > .65:
            if position:
                return "explore", self._offset_coords(position, 8, 8)
            return "", {}
        if drives.get("loneliness", 0) > .6:
            known = ImpulseEngine._known_players(nearby_players(observation), self.state)
            if known:
                return "follow", {"target_uuid": known[0]["uuid"], "duration_sec": 5}
        if drives.get("care_urge", 0) > .6:
            if observation["food"] < 15:
                return "eat", {}
            return self._home_step(observation)
        if drives.get("energy", 1) < .5:
            if observation["food"] < 15:
                return "eat", {}
            return self._home_step(observation)
        return "collect", {"block": "dirt", "count": 2}

    @staticmethod
    def _inventory_block(inventory):
        for item in inventory or []:
            name = _item_name(item.get("name")) if isinstance(item, dict) else ""
            if name and (name.endswith("planks") or name.endswith("cobblestone")
                         or name in {"dirt", "stone", "sand", "gravel", "oak_log"}):
                if item.get("count", 0) > 0:
                    return name
        return ""

    @staticmethod
    def _offset_coords(position, dx, dz):
        x = int(_clamp(round(position["x"]) + dx, -29999984, 29999984))
        z = int(_clamp(round(position["z"]) + dz, -29999984, 29999984))
        y = _y_coord(position["y"])
        return {"x": x, "y": 64 if y is None else y, "z": z}

    def _home(self):
        for place in self.state.data["places"]:
            if place.get("kind") == "home":
                return place
        return None

    # ------------------------------------------------------------ execution
    async def _execute(self, decision, observation):
        if decision.intent not in INTERNAL_INTENTS:
            return {"executed": False, "ok": False, "skill": "",
                    "reason": "invalid_intent"}
        skill, args, refs = self._select_skill(decision.intent, observation)
        decision.skill, decision.args = skill, dict(args or {})
        decision.project_id = refs.get("project_id", "")
        decision.promise_id = refs.get("promise_id", "")
        if not skill:
            self._reward_idle(decision.intent)
            return {"executed": False, "ok": True, "skill": "",
                    "reason": "no_valid_skill", **refs}
        if skill not in SKILLS.get(decision.intent, set()):
            self._record_failure(skill, "skill_not_allowed")
            return {"executed": False, "ok": False, "skill": skill,
                    "reason": "skill_not_allowed", **refs}
        if not validate_skill(skill, args):
            self._record_failure(skill, "invalid_args")
            return {"executed": False, "ok": False, "skill": skill,
                    "reason": "invalid_args", **refs}
        if skill == "chat" and _cfg(self.config, "game_chat_enabled", False) is not True:
            self._record_failure(skill, "game_chat_disabled")
            return {"executed": False, "ok": False, "skill": skill,
                    "reason": "game_chat_disabled", **refs}
        if self._repeat_blocked(decision.intent, skill, args):
            self._idle_cooldown = max(self._idle_cooldown, 2)
            self.state.data["drives"]["boredom"] = _clamp(
                self.state.data["drives"]["boredom"] + .08, 0, 1)
            return {"executed": False, "ok": False, "skill": skill,
                    "reason": "repeat_blocked", **refs}

        result = await self._invoke_skill(skill, args)
        if result.get("ok") is not True:
            reason = str(result.get("reason"))
            # A closed capability is a boundary, not a failed attempt: it must
            # not inflate the failure counter or pause an in-flight project.
            if reason not in {"write_disabled", "pvp_denied", "game_chat_disabled",
                              "invalid_action", "invalid_args", "guard_busy"}:
                self._record_failure(skill, reason)
            if result.get("preflight"):
                return {"executed": False, "ok": False, "skill": skill,
                        "reason": reason, **refs}
            return {"executed": True, "ok": False, "skill": skill,
                    "reason": "call_failed", **refs}
        self._success(decision.intent, skill, args, refs, observation)
        return {"executed": True, "ok": True, "skill": skill, "reason": "ok",
                **refs}

    async def _invoke_skill(self, skill, args, *, action=None):
        """Guard + policy + child call for one validated skill.

        Shared by normal intent execution and by the read-only home seed so
        both paths obey the same strict flags and permit rules.  ``preflight``
        marks a denial that happened before any guard/child call.
        """
        if not validate_skill(skill, args):
            return {"ok": False, "reason": "invalid_args", "preflight": True}
        if action is None:
            if skill in {"inventory", "set_home"}:
                action = "observe"
            elif skill == "attack":
                action = "pvp"
            else:
                action = "write"
        if action not in {"observe", "write", "pvp"}:
            return {"ok": False, "reason": "invalid_action", "preflight": True}
        if skill == "chat" and _cfg(self.config, "game_chat_enabled", False) is not True:
            return {"ok": False, "reason": "game_chat_disabled", "preflight": True}
        if action == "write" and _cfg(self.config, "write_enabled", False) is not True:
            return {"ok": False, "reason": "write_disabled", "preflight": True}
        target = ""
        if action == "pvp":
            target = _safe_uuid(args.get("target_uuid"))
            actor_now = self._actor_uuid()
            # Double guard: strict local flags *and* an exact configured target
            # that is not the bot itself.  Unknown players are never eligible.
            if (_cfg(self.config, "pvp_enabled", False) is not True
                    or _cfg(self.config, "write_enabled", False) is not True
                    or not target or target == actor_now
                    or target not in allowed_pvp_targets(
                        _cfg(self.config, "pvp_target_uuids", []))):
                return {"ok": False, "reason": "pvp_denied", "preflight": True}
        actor = self._actor_uuid()
        if not owner_identity_matches(self.config, actor):
            return {"ok": False, "reason": "owner_unbound", "preflight": True}
        try:
            permit = await self.guard.request(action, target)
            if action in {"write", "pvp"}:
                policy_permit = (self.guard.policy_permit(permit)
                                 if hasattr(self.guard, "policy_permit")
                                 else WorldPermit(
                                     str(permit.get("world_id") or
                                         _cfg(self.config, "test_world_id", "") or ""), True,
                                     _num(permit.get("issued_at"), self.clock(), low=0),
                                     can_write=permit.get("can_write") is True,
                                     can_pvp=permit.get("can_pvp") is True))
                if not authorize_action(
                        self.config, policy_permit, action=action,
                        target_uuid=target, player_uuid=actor, now=self.clock()):
                    return {"ok": False, "reason": "authorization_denied",
                            "preflight": True}
            await self.sidecar.request(
                "skill", {"name": skill, "args": args, "timeout_sec": SKILL_TIMEOUT_SEC},
                timeout=SKILL_TIMEOUT_SEC + 5)
        except asyncio.CancelledError:
            raise
        except GuardBusy:
            # Bounded pause, not a failure: do not record a failure streak or replay the action.
            self._idle_cooldown = max(self._idle_cooldown, 1)
            return {"ok": False, "reason": "guard_busy", "preflight": True}
        except SidecarError as exc:
            # The child's signed snapshot-busy checkpoint arrives as JSON-RPC -32008; treat it as
            # the same bounded pause, not as a skill denial.
            if getattr(exc, "code", None) == -32008:
                self._idle_cooldown = max(self._idle_cooldown, 1)
                return {"ok": False, "reason": "guard_busy", "preflight": True}
            return {"ok": False, "reason": "call_failed", "preflight": False}
        except (GuardDenied, OSError, TimeoutError):
            # One denied/failed skill never kills the loop; the failure is
            # recorded and drives change so the next choice differs.
            return {"ok": False, "reason": "call_failed", "preflight": False}
        return {"ok": True, "reason": "ok", "preflight": False}

    def _record_failure(self, skill, reason):
        self._failure_streak += 1
        self.state.data["counters"]["failures"] += 1
        drives = self.state.data["drives"]
        drives["boredom"] = _clamp(drives["boredom"] + .04, 0, 1)
        drives["creative_tension"] = _clamp(drives["creative_tension"] + .03, 0, 1)
        self._action_log.append({"sig": f"{skill}:{reason}", "ok": False,
                                 "at": self.clock()})
        self._action_log = self._action_log[-MAX_ACTIONS:]
        self._bump_habit("failed_" + str(skill)[:20])
        self._pause_current_project()
        if self._failure_streak >= 3:
            self._idle_cooldown = max(self._idle_cooldown, 2)
            self._failure_streak = 0

    def _success(self, intent, skill, args, refs, observation):
        self._failure_streak = 0
        self.state.data["counters"]["skills"] += 1
        drives = self.state.data["drives"]
        if skill in {"collect", "dig"}:
            drives["hoarding_urge"] = _clamp(drives["hoarding_urge"] - .12, 0, 1)
            drives["creative_tension"] = _clamp(drives["creative_tension"] + .06, 0, 1)
            drives["energy"] = _clamp(drives["energy"] - .02, 0, 1)
        elif skill in {"place", "craft"}:
            drives["creative_tension"] = _clamp(drives["creative_tension"] - .18, 0, 1)
            drives["hoarding_urge"] = _clamp(drives["hoarding_urge"] - .05, 0, 1)
            drives["energy"] = _clamp(drives["energy"] - .02, 0, 1)
        elif skill in {"explore", "move"}:
            drives["wanderlust"] = _clamp(drives["wanderlust"] - .12, 0, 1)
            drives["boredom"] = _clamp(drives["boredom"] - .12, 0, 1)
            drives["energy"] = _clamp(drives["energy"] - .03, 0, 1)
        elif skill in {"follow", "chat"}:
            drives["loneliness"] = _clamp(drives["loneliness"] - .16, 0, 1)
            drives["energy"] = _clamp(drives["energy"] - .02, 0, 1)
        elif skill == "eat":
            drives["energy"] = _clamp(drives["energy"] + .15, 0, 1)
        elif skill in {"home", "respawn"}:
            drives["energy"] = _clamp(drives["energy"] + .08, 0, 1)
            drives["boredom"] = _clamp(drives["boredom"] + .03, 0, 1)
        elif skill in {"farm", "breed"}:
            drives["care_urge"] = _clamp(drives["care_urge"] + .12, 0, 1)
            drives["energy"] = _clamp(drives["energy"] - .02, 0, 1)
        self._slow_personality(intent, skill)
        if skill == "follow":
            self._note_interaction(args.get("target_uuid", ""))
        self._bump_habit(_HABIT_KEY_FOR_SKILL.get(skill, skill))
        label = f"{intent}:{skill}"
        self.state.data["last_action"] = label
        self.state.data["recent_actions"] = (
            self.state.data["recent_actions"] + [label])[-16:]
        self._action_log.append({"sig": f"{skill}:{self._sig_args(args)}", "ok": True,
                                 "at": self.clock()})
        self._action_log = self._action_log[-MAX_ACTIONS:]
        self._advance_plan(refs, observation)

    def _slow_personality(self, intent, skill):
        personality = self.state.data["personality"]
        step = .002
        if intent == "continue_project":
            personality["industriousness"] = _clamp(
                personality["industriousness"] + step, 0, 1)
        elif intent in {"explore"}:
            personality["curiosity"] = _clamp(personality["curiosity"] + step, 0, 1)
        elif intent in {"socialize", "fulfill_promise"}:
            personality["attachment"] = _clamp(personality["attachment"] + step, 0, 1)
        elif skill in {"home", "farm"}:
            personality["orderliness"] = _clamp(personality["orderliness"] + step, 0, 1)

    def _advance_plan(self, refs, observation):
        project_id = refs.get("project_id", "")
        if project_id:
            for project in self.state.data["projects"]:
                if str(project.get("id")) != project_id:
                    continue
                project["step"] = _int(project.get("step"), 0, low=0) + 1
                project["progress"] = max(_int(project.get("progress"), 0),
                                           project["step"])
                project["last_acted"] = self.clock()
                project["failures"] = 0
                if project["step"] >= len(project.get("steps") or []):
                    project["status"] = "done"
                    self.state.data["counters"]["projects_completed"] += 1
                    self.state.data["last_project_complete_at"] = self.clock()
                    self._narrative_for_project(project)
                break
        promise_id = refs.get("promise_id", "")
        if promise_id:
            for promise in self.state.data["promises"]:
                if str(promise.get("id")) != promise_id:
                    continue
                promise["step"] = _int(promise.get("step"), 0, low=0) + 1
                promise["last_acted"] = self.clock()
                if promise["step"] >= len(promise.get("steps") or []):
                    promise["status"] = "fulfilled"
                    promise["fulfilled_at"] = self.clock()
                    self.state.data["counters"]["promises_fulfilled"] += 1
                break

    # ------------------------------------------------------------ state updates
    def _update_social(self, observation):
        now = self.clock()
        players = self.state.data["players"]
        linked = self._mapping_links()
        seen = {}
        for row in nearby_players(observation):
            uuid = row["uuid"]
            seen[uuid] = row
            role = self._role_for(uuid)
            baseline = self._role_baseline(role)
            record = next((item for item in players if item.get("uuid") == uuid), None)
            if record is None:
                record = {
                    "uuid": uuid, "role": role,
                    "familiarity": baseline["familiarity"],
                    "trust": baseline["trust"],
                    "closeness": baseline["closeness"],
                    "boundary": baseline["boundary"],
                    "last_seen": now, "shared_history_count": 0,
                    "explicit_link": uuid in linked,
                }
                players.append(record)
            else:
                record["explicit_link"] = uuid in linked
                record["familiarity"] = _clamp(
                    float(record.get("familiarity", 0)) + .03, 0, 1)
            record["last_seen"] = now
            # Role/trust come only from the canonical owner-human contract
            # (`minecraft_policy.owner_human_identity`); `owner_mappings` just
            # records a cross-channel link and never grants the role.
            record["role"] = role
            if role == "owner":
                record["trust"] = max(float(record.get("trust", 0)),
                                      baseline["trust"])
                record["familiarity"] = max(float(record.get("familiarity", 0)),
                                            baseline["familiarity"])
        del players[:-MAX_PLAYERS]
        for record in players:
            if record.get("uuid") not in seen and now - _num(record.get("last_seen"), now) > 3600:
                record["closeness"] = _clamp(float(record.get("closeness", 0)) * .98, 0, 1)

    def _update_places(self, observation):
        position = observation.get("position")
        if position is None:
            return
        places = self.state.data["places"]
        home = next((row for row in places if row.get("kind") == "home"), None)
        if home is None:
            self._remember_home(observation)
        else:
            distance = _distance(position, home)
            if distance is not None and distance <= 4:
                home["visits"] = _int(home.get("visits"), 0) + 1
                home["last_seen"] = self.clock()
        if _threat_count(observation) > 0:
            danger = {"kind": "danger", "x": round(position["x"], 3),
                      "y": round(position["y"], 3), "z": round(position["z"], 3),
                      "dimension": _safe_dimension(observation.get("dimension")),
                      "visits": 1, "last_seen": self.clock()}
            places.append(danger)
        del places[:-MAX_PLACES]

    def _ensure_project(self, observation):
        active = [row for row in self.state.data["projects"]
                  if row.get("status") == "active"]
        if active:
            return
        # A project paused by repeated failures is not abandoned forever: after
        # a quiet period it may be resumed with a fresh attempt budget.
        paused = [row for row in self.state.data["projects"]
                  if row.get("status") == "paused"]
        if paused:
            candidate = min(paused, key=lambda row: _num(row.get("last_acted"), 0, low=0))
            if self.clock() - _num(candidate.get("last_acted"), 0, low=0) >= 300:
                candidate["status"] = "active"
                candidate["failures"] = 0
                candidate["step"] = max(0, _int(candidate.get("step"), 0) - 1)
            return
        if len(self.state.data["projects"]) >= MAX_PROJECTS:
            return
        # After finishing a project, leave a short window for promises/social
        # follow-through instead of immediately starting another project.
        if (any(row.get("status") == "active" for row in self.state.data["promises"])
                and self.clock() - _num(
                    self.state.data.get("last_project_complete_at"), 0, low=0) < 180):
            return
        position = observation.get("position")
        if position is None:
            return
        personality = self.state.data["personality"]
        index = _int(self.state.data.get("next_project_id"), 1, low=1)
        # A small deterministic rotation keeps a long life from repeating the
        # same building project; personality still colours the choice.
        slot = index % 3
        if slot == 0 and personality.get("curiosity", 0) >= .4:
            kind = "explore"
        elif slot == 2 and (self.state.data["drives"].get("care_urge", 0) >= .3
                            or personality.get("orderliness", 0) >= .45):
            kind = "farm"
        else:
            kind = "shelter"
        titles = {"shelter": "搭一个能躲雨的小屋", "farm": "开一小块田",
                  "explore": "去看清远处那片坡"}
        goals = {"shelter": "把落脚的地方收拾得像家", "farm": "种下能吃的东西",
                 "explore": "把附近的边界画进脑子里"}
        self.state.data["next_project_id"] = index + 1
        anchor = {"x": int(round(position["x"])), "y": _y_coord(position["y"]),
                  "z": int(round(position["z"]))}
        if anchor["y"] is None:
            anchor["y"] = 64
        steps = self._project_steps(kind, anchor, observation)
        if not steps and kind == "farm":
            # No plantable, loaded soil in sight: fall back instead of emitting
            # farm coordinates the child would reject.
            kind = "shelter"
            steps = self._project_steps(kind, anchor, observation)
        self.state.data["projects"].append({
            "id": f"p{index}", "kind": kind, "title": titles[kind], "goal": goals[kind],
            "status": "active", "progress": 0, "step": 0,
            "steps": steps, "anchor": anchor,
            "created_at": self.clock(), "last_acted": 0, "failures": 0,
        })
        del self.state.data["projects"][:-MAX_PROJECTS]

    def _farm_soil(self, observation, anchor):
        """Plantable soil from the loaded observation, or nothing at all.

        The child's `farm` wants the soil block (farmland/dirt/grass_block) at
        (x, y, z) and the crop at y+1, within 4.5 blocks of the player.  Only
        blocks the observation actually reports can be planted: a guessed y-1
        cell may be stone/gravel/air, which the child refuses as `not_soil`.
        Preference order: an observed soil block exactly one below the feet,
        within two blocks; columns that already carry a crop/other block are
        skipped so an immature crop is never re-planted.  No observed soil
        means no farm step, so `_ensure_project` can fall back to shelter
        instead of accumulating predictable child refusals.
        """
        observation = observation if isinstance(observation, dict) else {}
        position = _canonical_point(observation.get("position")) or _canonical_point(anchor)
        if position is None:
            return []
        fx = int(math.floor(position["x"]))
        fy = int(math.floor(position["y"]))
        fz = int(math.floor(position["z"]))
        blocks = observation.get("nearby_blocks")
        occupied = set()
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            point = _canonical_point(block.get("position"))
            if point is None:
                continue
            occupied.add((int(math.floor(point["x"])),
                          int(math.floor(point["y"])),
                          int(math.floor(point["z"]))))
        soils = {}
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            name = _item_name(block.get("name"))
            if name not in _SOIL_BLOCKS:
                continue
            point = _canonical_point(block.get("position"))
            if point is None:
                continue
            sx = int(math.floor(point["x"]))
            sy = int(math.floor(point["y"]))
            sz = int(math.floor(point["z"]))
            if sy != fy - 1:
                continue
            if max(abs(sx - fx), abs(sz - fz)) > 2:
                continue
            if (sx, sy + 1, sz) in occupied:
                continue
            soils[(sx, sy, sz)] = name
        ordered = sorted(soils, key=lambda cell: (
            abs(cell[0] - fx) + abs(cell[2] - fz),
            cell[0] - fx, cell[2] - fz, cell[1]))
        if ordered:
            return [{"x": x, "y": y, "z": z, "block": soils[(x, y, z)]}
                    for x, y, z in ordered[:2]]
        return []

    def _project_steps(self, kind, anchor, observation=None):
        anchor = anchor if isinstance(anchor, dict) else {}
        x = int(anchor.get("x", 0)) if _coord(anchor.get("x")) is not None else 0
        raw_y = _y_coord(anchor.get("y"))
        y = 64 if raw_y is None else raw_y
        z = int(anchor.get("z", 0)) if _coord(anchor.get("z")) is not None else 0
        if kind == "farm":
            soils = self._farm_soil(observation, anchor)
            if not soils:
                return []
            position = (_canonical_point((observation or {}).get("position"))
                        or _canonical_point(anchor) or {"x": float(x), "y": float(y),
                                                        "z": float(z)})
            occupied = set()
            for block in (observation or {}).get("nearby_blocks") or []:
                if not isinstance(block, dict):
                    continue
                point = _canonical_point(block.get("position"))
                if point is not None:
                    occupied.add((int(math.floor(point["x"])),
                                  int(math.floor(point["y"])),
                                  int(math.floor(point["z"]))))
            soil_columns = {(row["x"], row["z"]) for row in soils}
            px = int(math.floor(position["x"]))
            py = int(math.floor(position["y"]))
            pz = int(math.floor(position["z"]))
            # The single `move` must leave every following `farm` soil inside
            # the child's reach.  Pick the legal stand with the smallest worst
            # distance (preference order breaks ties) instead of a fixed offset,
            # then drop any soil the best stand still cannot reach.
            stands = []
            for dx, dz in ((2, 2), (2, 0), (0, 2), (-2, 2), (2, -2),
                           (-2, 0), (0, -2), (-2, -2)):
                cell = (px + dx, py, pz + dz)
                if cell in occupied or (cell[0], cell[2]) in soil_columns:
                    continue
                distances = [
                    math.dist((float(cell[0]), float(cell[1]), float(cell[2])),
                              (float(soil["x"]), float(soil["y"]), float(soil["z"])))
                    for soil in soils]
                stands.append((max(distances), len(stands), cell, distances))
            if not stands:
                return []
            _worst, _order, stand, distances = min(
                stands, key=lambda row: (row[0], row[1]))
            soils = [soil for soil, distance in zip(soils, distances)
                     if distance <= FARM_SOIL_REACH]
            if not soils:
                return []
            steps = [{"skill": "move", "args": {
                "x": float(stand[0]), "y": float(py),
                "z": float(stand[2]), "radius": 1.0}}]
            for soil in soils:
                steps.append({"skill": "farm", "args": {
                    "x": soil["x"], "y": soil["y"], "z": soil["z"],
                    "seed": "wheat_seeds"}})
            steps.append({"skill": "eat", "args": {}})
            return steps
        if kind == "explore":
            return [
                {"skill": "explore", "args": {
                    "x": int(_clamp(x + 10, -29999984, 29999984)), "y": y,
                    "z": int(_clamp(z + 10, -29999984, 29999984))}},
                {"skill": "explore", "args": {
                    "x": int(_clamp(x + 20, -29999984, 29999984)), "y": y,
                    "z": int(_clamp(z + 5, -29999984, 29999984))}},
                {"skill": "home", "args": {}},
            ]
        upper_y = min(319, y + 1)
        occupied = set()
        for block in (observation or {}).get("nearby_blocks") or []:
            if not isinstance(block, dict):
                continue
            point = _canonical_point(block.get("position"))
            if point is not None:
                occupied.add((int(math.floor(point["x"])),
                              int(math.floor(point["y"])),
                              int(math.floor(point["z"]))))
        move_cell = (x + 2, y, z + 2)
        if move_cell in occupied:
            move_cell = (x, y, z)
        site = None
        for dx, dz in ((1, 1), (1, 0), (0, 1), (0, -1), (-1, 0), (-1, -1),
                       (1, -1), (-1, 1), (0, 0), (2, 0), (0, 2), (-2, 0),
                       (0, -2)):
            if (dx, dz) == (2, 2):
                continue
            cx, cz = x + dx, z + dz
            if (cx, y, cz) in occupied or (cx, y + 1, cz) in occupied:
                continue
            site = (cx, cz)
            break
        if site is None:
            site = (x + 1, z + 1)   # nothing free observed; the child refuses honestly
        return [
            # Stand clear of the build cell first: placing a block in one's own
            # feet cell is refused by the server.
            {"skill": "move", "args": {"x": float(move_cell[0]), "y": float(y),
                                       "z": float(move_cell[2]), "radius": 1.0}},
            {"skill": "collect", "args": {"block": "dirt", "count": 4}},
            {"skill": "place", "args": {"x": site[0], "y": y, "z": site[1],
                                        "face_x": 0, "face_y": 1, "face_z": 0, "item": "dirt"}},
            {"skill": "place", "args": {"x": site[0], "y": upper_y, "z": site[1],
                                        "face_x": 0, "face_y": 1, "face_z": 0, "item": "dirt"}},
            {"skill": "eat", "args": {}},
        ]

    def _ensure_promise(self, observation):
        active = [row for row in self.state.data["promises"]
                  if row.get("status") == "active"]
        if active or len(self.state.data["promises"]) >= MAX_PROMISES:
            return
        known = ImpulseEngine._known_players(nearby_players(observation), self.state)
        if not known:
            return
        if self.state.data["personality"].get("sociability", .5) < .45:
            return
        # Promises are not a metronome: a new autonomous promise waits a while,
        # so an active project is not starved by a freshly recreated promise.
        if self.clock() - _num(self.state.data.get("last_promise_at"), 0, low=0) < 300:
            return
        target = known[0]["uuid"]
        index = _int(self.state.data.get("next_promise_id"), 1, low=1)
        self.state.data["next_promise_id"] = index + 1
        self.state.data["last_promise_at"] = self.clock()
        self.state.data["promises"].append({
            "id": f"q{index}",
            "title": "下次见到那个玩家时跟着走一小段",
            "status": "active", "created_at": self.clock(), "due_at": 0,
            "step": 0, "attempts": 0, "player_uuid": target,
            "steps": [
                {"skill": "follow", "args": {"target_uuid": target, "duration_sec": 5}},
                {"skill": "move", "args": {"x": float((observation.get("position") or {}).get("x", 0)),
                                           "y": float((observation.get("position") or {}).get("y", 64)),
                                           "z": float((observation.get("position") or {}).get("z", 0)),
                                           "radius": 1.0}},
            ],
        })

    def _note_interaction(self, player_uuid):
        uuid = _safe_uuid(player_uuid)
        for record in self.state.data["players"]:
            if record.get("uuid") == uuid:
                record["closeness"] = _clamp(float(record.get("closeness", 0)) + .05, 0, 1)
                record["trust"] = _clamp(float(record.get("trust", 0)) + .03, 0, 1)
                record["shared_history_count"] = _int(
                    record.get("shared_history_count"), 0) + 1
                return

    def _bump_habit(self, name):
        key = _safe_text(name, 32)
        if not key:
            return
        habits = self.state.data["habits"]
        record = habits.get(key) or {"count": 0, "last_at": 0}
        record["count"] = min(100000, _int(record.get("count"), 0) + 1)
        record["last_at"] = self.clock()
        habits[key] = record
        if len(habits) > 16:
            oldest = sorted(habits.items(), key=lambda pair: pair[1].get("last_at", 0))[0][0]
            habits.pop(oldest, None)

    def _repeat_blocked(self, intent, skill, args) -> bool:
        signature = f"{skill}:{self._sig_args(args)}"
        recent = [row for row in self._action_log[-4:]
                  if row.get("sig") == signature and self.clock() - row.get("at", 0) < 600]
        return len(recent) >= 3

    @staticmethod
    def _sig_args(args):
        try:
            return hashlib.sha1(json.dumps(args, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()[:16]
        except (TypeError, ValueError):
            return "invalid"

    def _reward_idle(self, intent):
        drives = self.state.data["drives"]
        if intent in {"idle", "rest"}:
            drives["energy"] = _clamp(drives["energy"] + .02, 0, 1)
            drives["boredom"] = _clamp(drives["boredom"] + .01, 0, 1)

    def _pause_current_project(self):
        for project in self.state.data["projects"]:
            if project.get("status") == "active":
                project["failures"] = _int(project.get("failures"), 0) + 1
                project["last_acted"] = self.clock()
                if project["failures"] >= 2:
                    project["status"] = "paused"
                break

    # ------------------------------------------------------------ reflection
    async def _reflect(self, decision, observation, outcome=None):
        outcome = outcome or {}
        skill = outcome.get("skill", "")
        executed = outcome.get("executed") is True
        ok = outcome.get("ok") is True
        if executed and ok:
            phrases = {
                "continue_project": f"我又往前做了一步（{_safe_text(skill, 24)}）。",
                "fulfill_promise": "答应过的事做了一点，心里轻了些。",
                "satisfy_drive": "顺着心里那股劲做了点事，舒服多了。",
                "socialize": "和旁边的人待了一会儿，没那么闷了。",
                "explore": "多看清了一点周围，脚下更熟了。",
                "rest": "停下来歇了一会儿，缓过来了。",
                "recover": "先把自己安顿好，别的等会儿再说。",
            }
            entry = phrases.get(decision.intent, "做了点小事。")
        elif outcome.get("reason") in {"write_disabled", "game_chat_disabled",
                                       "authorization_denied", "owner_unbound"}:
            entry = "这次被边界拦住了，我记下这里不能乱来。"
        elif executed and not ok:
            entry = "这次没成，先记着，换个法子再来。"
        elif outcome.get("reason") == "repeat_blocked":
            entry = "一直在做同一件事，我停下来喘口气。"
        else:
            entry = "这次只是看着，没急着动手。"

        project_completed = any(
            outcome.get("project_id") == str(row.get("id")) and row.get("status") == "done"
            for row in self.state.data["projects"])
        if project_completed:
            entry = "手上的项目做完了，我把它记进日记里。"

        entry = _safe_text(entry, 220)
        stamp = time.strftime("%H:%M", time.localtime(self.clock()))
        self.state.data["journal"].append({"at": self.clock(),
                                           "text": f"{stamp} {entry}"})
        del self.state.data["journal"][:-MAX_JOURNAL]
        self.state.data["reflection"] = entry
        self._write_diary(stamp, entry)
        self._update_narrative(decision, outcome)
        milestone = project_completed or outcome.get("promise_id") and any(
            row.get("status") == "fulfilled" and str(row.get("id")) == outcome.get("promise_id")
            for row in self.state.data["promises"])
        await self._record_episode(entry, milestone=bool(milestone))

    def _narrative_for_project(self, project):
        title = _safe_text(project.get("title"), 60)
        self.state.data["self_narrative"] = _safe_text(
            f"我把「{title}」做完了，这里又多了一点我的痕迹。", 500)

    def _update_narrative(self, decision, outcome):
        personality = self.state.data["personality"]
        trait = max(_PERSONALITY_KEYS, key=lambda key: personality.get(key, 0))
        project = next((row for row in self.state.data["projects"]
                        if row.get("status") == "active"), None)
        promise = next((row for row in self.state.data["promises"]
                        if row.get("status") == "active"), None)
        if project:
            text = f"我还在慢慢做「{_safe_text(project.get('title'), 60)}」。"
        elif promise:
            text = "我答应过一件事，还记着。"
        elif trait == "curiosity":
            text = "我想知道这片地方还藏着什么。"
        elif trait == "industriousness":
            text = "我喜欢把东西一点点做出来。"
        else:
            text = "我在慢慢把这里当成能待的地方。"
        self.state.data["self_narrative"] = _safe_text(text, 500)

    def _write_diary(self, stamp, entry):
        try:
            root = Path(self.bot.paths.minecraft_content("diary"))
            root.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
            path = root / f"{day}.md"
            if path.exists() and path.stat().st_size > MAX_DIARY_BYTES:
                return
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"- {stamp} {_safe_text(entry, 220)}\n")
        except (OSError, ValueError):
            # A diary write must never stop the resident or leak a path.
            pass

    async def _record_episode(self, summary, *, milestone: bool = False):
        now = self.clock()
        if not milestone and now - self._last_episode < 60:
            return
        longmem = getattr(self.bot, "longmem", None)
        add_episode = getattr(longmem, "add_episode", None)
        if not callable(add_episode):
            return
        participants = [row.get("uuid") for row in self.state.data["players"][-8:]
                        if row.get("uuid")]
        # Only pass keywords this LongMemory build actually accepts: the live
        # implementation has no source= parameter and TypeError'd every write.
        extra = {}
        try:
            if "source" in inspect.signature(add_episode).parameters:
                extra["source"] = "minecraft"
        except (TypeError, ValueError):
            extra = {}
        try:
            await add_episode(
                self.bot, ts=now, session_id=self._session_id(),
                participants=participants, summary=_safe_text(summary, 300),
                keywords=["minecraft", "自主", "项目"], importance=4 if milestone else 2,
                **extra)
            self._last_episode = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never break the loop for memory
            log.warn(f"Minecraft long-memory write skipped: {type(exc).__name__}")


# Readable aliases used by the protocol/design vocabulary.
Jev = JevAdapter
FallbackPlanner = DeterministicPlanner
Workspace = GlobalWorkspace
