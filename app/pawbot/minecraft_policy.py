"""Fail-closed authorization for Minecraft actions.

A world permit is an attestation from the local integrated-server guard, not a
Minecraft protocol fact. Callers must obtain it from a trusted guard adapter;
plain Mineflayer connection state is never sufficient authorization.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass

from .identity import uid_of

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# The Java guard refuses more than 16 exact PvP targets; the host policy uses
# the same bound so a config the clients accepted cannot suddenly widen here.
MAX_PVP_TARGETS = 16


@dataclass(frozen=True)
class WorldPermit:
    world_id: str
    verified: bool
    issued_at: float
    can_write: bool = False
    can_pvp: bool = False


def normalize_player_uuid(value: object) -> str:
    """Return a canonical UUID, or empty for malformed/unknown identities."""
    text = str(value or "").strip()
    if not _UUID_RE.fullmatch(text):
        return ""
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return ""


def owner_identity_matches(config, player_uuid: object) -> bool:
    """Require an exact local Minecraft UUID and complete numeric QQ UID binding."""
    configured_uuid = normalize_player_uuid(
        config.get("minecraft.owner_player_uuid", ""))
    owner_uid = uid_of(config.get("minecraft.owner_qq_uid", ""))
    observed_uuid = normalize_player_uuid(player_uuid)
    return bool(configured_uuid and owner_uid and observed_uuid == configured_uuid)


def explicit_mapping_pairs(configured: object) -> frozenset[tuple[str, str]]:
    """Canonical explicit UUID <-> QQ UID links from ``owner_mappings``.

    Exactly one side of an entry must be an exact UUID and the other a complete
    numeric QQ UID.  A nickname, a partial number, a UUID<->UUID / UID<->UID
    entry or any unparseable entry is not a link: it can neither verify nor
    contradict an identity.  Duplicate/ambiguous links are returned as-is so
    each caller can fail closed on them; nothing is inferred from a name.
    """
    if not isinstance(configured, dict):
        return frozenset()
    pairs = set()
    for key, value in configured.items():
        key_uuid = normalize_player_uuid(key)
        value_uuid = normalize_player_uuid(value)
        key_uid = "" if key_uuid else uid_of(key)
        value_uid = "" if value_uuid else uid_of(value)
        if key_uuid and value_uid:
            pairs.add((key_uuid, value_uid))
        elif value_uuid and key_uid:
            pairs.add((value_uuid, key_uid))
    return frozenset(pairs)


def owner_human_identity(config) -> str:
    """Canonical owner-human UUID, or "" when absent/invalid/conflicting.

    This is the **single** owner-human contract for the whole runtime; the
    resident delegates to it instead of re-implementing the rules.  The optional
    ``minecraft.owner_human_uuid`` is accepted only when all hold:

    1. it is exactly one canonical UUID (case/uppercase spellings normalize to
       the same identity; a nickname, QQ number or any other inference is not);
    2. it differs from ``minecraft.owner_player_uuid``, which is the bot actor
       and never the owner human;
    3. every explicit local ``owner_mappings`` link touching it agrees with the
       complete numeric ``minecraft.owner_qq_uid``, and that QQ UID is not
       explicitly linked to a different UUID (ambiguous links included).

    Any conflict means "no verified owner-human identity", so every caller stays
    fail-closed.  Mappings may veto an inconsistent identity, but they can never
    grant the owner role on their own: nothing is inferred from QQ names,
    nicknames or UID suffixes.
    """
    human = normalize_player_uuid(config.get("minecraft.owner_human_uuid", ""))
    if not human:
        return ""
    bot_actor = normalize_player_uuid(
        config.get("minecraft.owner_player_uuid", ""))
    if human == bot_actor:
        return ""
    owner_uid = uid_of(config.get("minecraft.owner_qq_uid", ""))
    pairs = explicit_mapping_pairs(config.get("minecraft.owner_mappings", {}))
    human_uids = {uid for mapped_uuid, uid in pairs if mapped_uuid == human}
    # One avatar explicitly linked to two QQ UIDs is a conflicting local mapping.
    if len(human_uids) > 1:
        return ""
    if not owner_uid:
        return human
    if human_uids and owner_uid not in human_uids:
        return ""
    owner_uuids = {mapped_uuid for mapped_uuid, uid in pairs if uid == owner_uid}
    # One owner QQ UID explicitly linked to two UUIDs is also a conflict.
    if len(owner_uuids) > 1:
        return ""
    if owner_uuids and human not in owner_uuids:
        return ""
    return human


def owner_human_matches(config, player_uuid: object) -> bool:
    """True only for the exact configured owner-human UUID (never the bot actor)."""
    configured = owner_human_identity(config)
    observed = normalize_player_uuid(player_uuid)
    return bool(configured and observed == configured)


def allowed_pvp_targets(configured: object) -> frozenset[str]:
    """Normalize a local exact-target list; wildcards and malformed IDs vanish."""
    if not isinstance(configured, (list, tuple, set, frozenset)):
        return frozenset()
    targets = frozenset(value for item in configured
                        if (value := normalize_player_uuid(item)))
    # More than the guard's hard limit is a configuration error: deny all PvP
    # rather than silently keeping a subset the operator did not audit.
    return targets if len(targets) <= MAX_PVP_TARGETS else frozenset()


def authorize_action(
    config,
    permit: WorldPermit | None,
    *,
    action: str,
    target_uuid: object = "",
    player_uuid: object = "",
    now: float | None = None,
) -> bool:
    """Check config, fresh guard attestation, dedicated world, capability and target.

    Supported actions are ``write`` and ``pvp``. Every unknown action is denied.
    The timestamp is injectable to keep expiry behavior deterministic in tests.
    """
    if action not in {"write", "pvp"}:
        return False
    if config.get("minecraft.enabled", False) is not True:
        return False
    if config.get("minecraft.guard_required", True) is not True:
        return False
    if not owner_identity_matches(config, player_uuid):
        return False
    if permit is None or permit.verified is not True:
        return False
    expected_world = str(config.get("minecraft.test_world_id", "") or "").strip()
    if not expected_world or not permit.world_id:
        return False
    if permit.world_id != expected_world:
        return False
    try:
        issued_at = float(permit.issued_at)
        configured_age = float(config.get("minecraft.permit_max_age_sec", 30))
        current = time.time() if now is None else float(now)
    except (TypeError, ValueError, OverflowError):
        return False
    # The guard attestation itself may live at most 5 seconds
    # (docs/MINECRAFT-PROTOCOL.md); the local config can only shorten that.
    max_age = min(5.0, configured_age) if configured_age > 0 else 0.0
    if (not all(math.isfinite(value) for value in (issued_at, max_age, current))
            or max_age <= 0 or issued_at > current or current - issued_at > max_age):
        return False
    if action == "write":
        return (config.get("minecraft.write_enabled", False) is True
                and permit.can_write is True)
    # PvP is strictly narrower than write: the sidecar also gates it on write.
    if not (config.get("minecraft.pvp_enabled", False) is True
            and config.get("minecraft.write_enabled", False) is True
            and permit.can_pvp is True and permit.can_write is True):
        return False
    target = normalize_player_uuid(target_uuid)
    actor = normalize_player_uuid(player_uuid)
    configured_actor = normalize_player_uuid(
        config.get("minecraft.owner_player_uuid", ""))
    if (not target or target in {actor, configured_actor}):
        return False   # unknown identity, or the bot's own actor
    return target in allowed_pvp_targets(
        config.get("minecraft.pvp_target_uuids", []))
