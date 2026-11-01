"""Offline tests for fail-closed Minecraft action policy."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.config import DEFAULT  # noqa: E402
from pawbot.minecraft_policy import (  # noqa: E402
    MAX_PVP_TARGETS,
    WorldPermit,
    allowed_pvp_targets,
    authorize_action,
    explicit_mapping_pairs,
    owner_human_identity,
    owner_human_matches,
    owner_identity_matches,
)


WORLD = "test-world-01"
ALLOWED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OWNER_UUID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class Config(dict):
    def get(self, dotted, default=None):
        node = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class MinecraftPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(copy.deepcopy(DEFAULT))
        self.config["minecraft"].update({
            "enabled": True,
            "test_world_id": WORLD,
            "owner_qq_uid": "123456789",
            "owner_player_uuid": OWNER_UUID,
            "write_enabled": True,
            "pvp_enabled": True,
            "pvp_target_uuids": [ALLOWED],
        })
        self.permit = WorldPermit(WORLD, True, 100.0, can_write=True, can_pvp=True)

    def allowed(self, action, permit=None, **kwargs):
        return authorize_action(self.config, self.permit if permit is None else permit,
                                action=action, player_uuid=OWNER_UUID,
                                now=103.0, **kwargs)

    def test_defaults_keep_integration_and_mutations_disabled(self):
        self.assertFalse(DEFAULT["minecraft"]["enabled"])
        self.assertTrue(DEFAULT["minecraft"]["guard_required"])
        self.assertFalse(DEFAULT["minecraft"]["write_enabled"])
        self.assertFalse(DEFAULT["minecraft"]["pvp_enabled"])
        self.assertEqual(DEFAULT["minecraft"]["pvp_target_uuids"], [])
        # Guard authority: no longer than 1800s (30 min) between snapshots.
        self.assertLessEqual(DEFAULT["minecraft"]["backup_interval_min"], 30)
        self.assertGreaterEqual(DEFAULT["minecraft"]["backup_interval_min"], 1)
        self.assertEqual(DEFAULT["minecraft"]["port"], 0)
        self.assertEqual(DEFAULT["minecraft"]["host"], "127.0.0.1")
        # Optional keys the resident actually reads must be registered with
        # safe defaults; remote decisions and in-game chat stay off.
        self.assertEqual(DEFAULT["minecraft"]["tick_interval_sec"], 3)
        self.assertEqual(DEFAULT["minecraft"]["decision_interval_sec"], 60)
        self.assertFalse(DEFAULT["minecraft"]["decision_remote_enabled"])
        self.assertEqual(DEFAULT["minecraft"]["decision_cost_usd_per_call"], 0)
        self.assertEqual(DEFAULT["minecraft"]["reconnect_attempts"], 3)
        self.assertEqual(DEFAULT["minecraft"]["reconnect_backoff_sec"], 1)
        self.assertFalse(DEFAULT["minecraft"]["game_chat_enabled"])
        self.assertEqual(DEFAULT["minecraft"]["owner_human_uuid"], "")
        # The owner human is optional and deliberately distinct from the bot
        # actor; this key must not alias owner_player_uuid.
        self.assertIn("owner_human_uuid", DEFAULT["minecraft"])

    def test_allows_write_only_with_matching_fresh_guard_permit(self):
        self.assertTrue(self.allowed("write"))
        self.assertFalse(self.allowed("write", WorldPermit(WORLD, True, 0, can_write=True)))
        self.assertFalse(self.allowed("write", WorldPermit("other", True, 100, can_write=True)))
        self.assertFalse(self.allowed("write", WorldPermit(WORLD, False, 100, can_write=True)))
        self.assertFalse(self.allowed("write", WorldPermit(WORLD, True, 100)))

    def test_pvp_requires_exact_configured_player_uuid(self):
        self.assertTrue(self.allowed("pvp", target_uuid=ALLOWED))
        self.assertFalse(self.allowed("pvp", target_uuid=OTHER))
        self.assertFalse(self.allowed("pvp", target_uuid="*"))
        self.config["minecraft"]["pvp_target_uuids"] = ["*"]
        self.assertFalse(self.allowed("pvp", target_uuid=ALLOWED))

    def test_pvp_is_never_wider_than_write(self):
        # Turning write off must also close PvP: the sidecar gates PvP on write.
        self.config["minecraft"]["write_enabled"] = False
        self.assertFalse(self.allowed("pvp", target_uuid=ALLOWED))
        self.config["minecraft"]["write_enabled"] = True
        self.assertFalse(self.allowed(
            "pvp", WorldPermit(WORLD, True, 100, can_write=False, can_pvp=True),
            target_uuid=ALLOWED))

    def test_pvp_target_cannot_be_the_actor_itself(self):
        self.config["minecraft"]["pvp_target_uuids"] = [OWNER_UUID]
        self.assertFalse(self.allowed("pvp", target_uuid=OWNER_UUID))
        self.assertFalse(self.allowed("pvp", target_uuid=OWNER_UUID.upper()))
        self.assertFalse(self.allowed("pvp", target_uuid=""))
        self.assertFalse(self.allowed("pvp", target_uuid=None))

    def test_disabled_expired_future_and_unknown_actions_fail_closed(self):
        self.config["minecraft"]["enabled"] = False
        self.assertFalse(self.allowed("write"))
        self.config["minecraft"]["enabled"] = True
        self.assertFalse(self.allowed("write", WorldPermit(WORLD, True, 79, can_write=True)))
        self.assertFalse(self.allowed("write", WorldPermit(WORLD, True, 111, can_write=True)))
        self.assertFalse(self.allowed("write", WorldPermit(WORLD, True, float("nan"), can_write=True)))
        self.assertFalse(self.allowed("admin"))

    def test_mismatched_or_missing_owner_identity_denies_actions(self):
        self.assertFalse(authorize_action(
            self.config, self.permit, action="write", player_uuid=OTHER, now=103.0))
        self.assertFalse(authorize_action(
            self.config, self.permit, action="write", player_uuid="", now=103.0))
        self.config["minecraft"]["owner_qq_uid"] = "Owner123456789"
        self.assertFalse(self.allowed("write"))

    def test_guard_is_mandatory_even_if_config_attempts_to_disable_requirement(self):
        self.config["minecraft"]["guard_required"] = False
        self.assertFalse(self.allowed("write"))

    def test_permit_age_is_capped_at_the_guard_attestation_lifetime(self):
        # A generous local config can never widen the 5-second guard lifetime.
        self.config["minecraft"]["permit_max_age_sec"] = 3600
        self.assertTrue(authorize_action(
            self.config, self.permit, action="write",
            player_uuid=OWNER_UUID, now=104.0))
        self.assertFalse(authorize_action(
            self.config, self.permit, action="write",
            player_uuid=OWNER_UUID, now=106.0))
        self.config["minecraft"]["permit_max_age_sec"] = 0
        self.assertFalse(self.allowed("write"))

    def test_string_values_never_enable_capabilities(self):
        self.config["minecraft"]["write_enabled"] = "true"
        self.assertFalse(self.allowed("write"))
        self.config["minecraft"]["write_enabled"] = True
        self.config["minecraft"]["pvp_enabled"] = "true"
        self.assertFalse(self.allowed("pvp", target_uuid=ALLOWED))
        self.config["minecraft"]["pvp_enabled"] = True
        self.assertFalse(self.allowed(
            "write", WorldPermit(WORLD, "true", 100, can_write=True)))

    def test_owner_binding_requires_full_numeric_uid_and_exact_player_uuid(self):
        self.config["minecraft"]["owner_qq_uid"] = "123456789"
        self.config["minecraft"]["owner_player_uuid"] = ALLOWED
        self.assertTrue(owner_identity_matches(self.config, ALLOWED.upper()))
        self.assertFalse(owner_identity_matches(self.config, OTHER))
        self.config["minecraft"]["owner_qq_uid"] = "owner123456789"
        self.assertFalse(owner_identity_matches(self.config, ALLOWED))
        self.config["minecraft"]["owner_qq_uid"] = "１２３４５６７８９"
        self.assertFalse(owner_identity_matches(self.config, ALLOWED))
        self.config["minecraft"]["owner_qq_uid"] = "123456789"
        self.config["minecraft"]["owner_player_uuid"] = ""
        self.assertFalse(owner_identity_matches(self.config, ALLOWED))

    def test_target_config_rejects_non_lists_and_malformed_uuids(self):
        self.assertEqual(allowed_pvp_targets("*"), frozenset())
        self.assertEqual(allowed_pvp_targets(["not-a-uuid", "*"]), frozenset())
        self.assertEqual(allowed_pvp_targets([ALLOWED.upper()]), frozenset({ALLOWED}))

    def test_pvp_target_bound_matches_the_guard_limit(self):
        def uuid_for(index):
            return f"{index:08x}-0000-4000-8000-000000000000"

        targets = [uuid_for(index) for index in range(MAX_PVP_TARGETS)]
        self.assertEqual(len(allowed_pvp_targets(targets)), MAX_PVP_TARGETS)
        self.assertEqual(allowed_pvp_targets(targets + [uuid_for(99)]), frozenset(),
                         "more than the guard limit must deny all PvP, not keep a subset")
        # Duplicates of the same distinct identity still count once.
        self.assertEqual(len(allowed_pvp_targets(targets * 2)), MAX_PVP_TARGETS)


class OwnerHumanIdentityTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(copy.deepcopy(DEFAULT))
        self.config["minecraft"].update({
            "owner_qq_uid": "123456789",
            "owner_player_uuid": OWNER_UUID,
            "owner_mappings": {},
        })

    def set_human(self, value):
        self.config["minecraft"]["owner_human_uuid"] = value

    def test_absent_or_malformed_owner_human_stays_unverified(self):
        self.assertEqual(owner_human_identity(self.config), "")
        for value in ("", "not-a-uuid", "*", 123):
            with self.subTest(value=value):
                self.set_human(value)
                self.assertEqual(owner_human_identity(self.config), "")
                self.assertFalse(owner_human_matches(self.config, OTHER))

    def test_exact_and_case_insensitive_uuid_normalizes(self):
        self.set_human(OTHER.upper())
        self.assertEqual(owner_human_identity(self.config), OTHER)
        self.assertTrue(owner_human_matches(self.config, OTHER.upper()))
        self.assertFalse(owner_human_matches(self.config, OWNER_UUID))

    def test_owner_human_must_not_be_the_bot_actor(self):
        self.set_human(OWNER_UUID)
        self.assertEqual(owner_human_identity(self.config), "")
        self.assertFalse(owner_human_matches(self.config, OWNER_UUID))

    def test_owner_mappings_conflicts_reject_the_identity(self):
        self.set_human(OTHER)
        self.config["minecraft"]["owner_mappings"] = {"123456789": OTHER}
        self.assertEqual(owner_human_identity(self.config), OTHER,
                         "an explicit link to the configured owner QQ is consistent")
        self.config["minecraft"]["owner_mappings"] = {"987654321": OTHER}
        self.assertEqual(owner_human_identity(self.config), "",
                         "a link to a different complete QQ UID is a conflict")
        self.config["minecraft"]["owner_mappings"] = {"123456789": ALLOWED}
        self.assertEqual(owner_human_identity(self.config), "",
                         "the owner QQ linked to another UUID is a conflict")
        self.config["minecraft"]["owner_mappings"] = {"malformed": OTHER}
        self.assertEqual(owner_human_identity(self.config), OTHER,
                         "a malformed mapping entry is not a verified link")

    def test_owner_human_is_always_distinct_from_the_bot_actor(self):
        self.config["minecraft"]["owner_player_uuid"] = OTHER.upper()
        self.set_human(OTHER)
        self.assertEqual(owner_human_identity(self.config), "",
                         "uppercase spellings normalize to the same actor")
        self.assertFalse(owner_human_matches(self.config, OTHER))

    def test_mapping_pairs_are_explicit_complete_uuid_uid_links_only(self):
        pairs = explicit_mapping_pairs({
            "123456789": OTHER,
            ALLOWED.upper(): "uid:987654321",
            "uid:555555555": OTHER.upper(),
            "not-a-uuid": "not-a-uid",
            OTHER: ALLOWED,                    # UUID <-> UUID is not a link
            "owner123456789": OTHER,           # a name is never a QQ UID
        })
        self.assertEqual(pairs, frozenset({
            (OTHER, "123456789"),
            (ALLOWED, "987654321"),
            (OTHER, "555555555"),
        }))
        for malformed in (None, "", "*", ["123456789", OTHER], 123):
            with self.subTest(malformed=malformed):
                self.assertEqual(explicit_mapping_pairs(malformed), frozenset())

    def test_every_mapping_conflict_fails_closed_but_never_grants(self):
        self.set_human(OTHER)
        # Consistent explicit links (or shapes that are not links at all).
        consistent = [
            {},
            {"123456789": OTHER},
            {"123456789": OTHER.upper()},
            {"uid:123456789": OTHER},
            {OTHER.upper(): "123456789"},
            {"malformed": OTHER},
            {OTHER: ALLOWED},
            "not-a-mapping",
        ]
        for mappings in consistent:
            with self.subTest(mappings=mappings):
                self.config["minecraft"]["owner_mappings"] = mappings
                self.assertEqual(owner_human_identity(self.config), OTHER)
        # Any contradiction, including ambiguous links, is unverified.
        conflicting = [
            {"987654321": OTHER},                               # human -> another UID
            {"123456789": ALLOWED},                             # owner UID -> another UUID
            {OTHER: "987654321"},                               # same conflict, key order
            {OTHER: "123456789", OTHER.upper(): "987654321"},   # one avatar, two UIDs
            {OTHER: "123456789", ALLOWED: "123456789"},         # one QQ UID, two UUIDs
        ]
        for mappings in conflicting:
            with self.subTest(mappings=mappings):
                self.config["minecraft"]["owner_mappings"] = mappings
                self.assertEqual(owner_human_identity(self.config), "",
                                 "conflicting explicit mappings must not verify")

    def test_a_mapping_alone_never_grants_the_owner_human_identity(self):
        self.config["minecraft"]["owner_mappings"] = {"123456789": OTHER}
        self.assertEqual(owner_human_identity(self.config), "")
        self.assertFalse(owner_human_matches(self.config, OTHER))


if __name__ == "__main__":
    unittest.main()
