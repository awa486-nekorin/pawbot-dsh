"""Offline whitelist matrix; never initializes Bot or reads runtime config."""

import sys
import unittest
from copy import deepcopy
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.core import Bot
from pawbot.event import parse


class WhitelistTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot.__new__(Bot)
        self.bot.config = {}

    def configure(self, *, users=(), groups=(), blocked=(), owners=()):
        self.bot.config = {
            "whitelist.user_ids": list(users),
            "whitelist.group_ids": list(groups),
            "whitelist.blacklist_group_ids": list(blocked),
            "account.owner_ids": list(owners),
        }

    def event(self, *, uid=20002, gid=None, post_type="message"):
        raw = {
            "post_type": post_type,
            "self_id": 10001,
            "user_id": uid,
            "message_type": "private" if gid is None else "group",
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }
        if gid is not None:
            raw["group_id"] = gid
        return parse(raw)

    def test_missing_whitelist_settings_allow_private_and_group(self):
        for gid in (None, 77):
            with self.subTest(gid=gid):
                self.assertTrue(self.bot._pass_filter(self.event(gid=gid)))

    def test_empty_lists_leave_each_dimension_unrestricted(self):
        self.configure()
        for uid, gid in product((20002, 30003), (None, 77, 88)):
            with self.subTest(uid=uid, gid=gid):
                self.assertTrue(self.bot._pass_filter(self.event(uid=uid, gid=gid)))

    def test_user_only_filter_applies_to_private_and_every_group(self):
        self.configure(users=[20002])
        for uid, gid in product((20002, 30003), (None, 77, 88)):
            with self.subTest(uid=uid, gid=gid):
                self.assertEqual(self.bot._pass_filter(self.event(uid=uid, gid=gid)),
                                 uid == 20002)

    def test_group_only_filter_leaves_users_and_private_messages_unrestricted(self):
        self.configure(groups=[77])
        for uid, gid in product((20002, 30003), (None, 77, 88)):
            with self.subTest(uid=uid, gid=gid):
                self.assertEqual(self.bot._pass_filter(self.event(uid=uid, gid=gid)),
                                 gid in (None, 77))

    def test_both_filters_require_user_and_group_match(self):
        self.configure(users=[20002], groups=[77])
        for uid, gid, allowed in (
            (20002, 77, True),
            (20002, 88, False),
            (30003, 77, False),
            (30003, 88, False),
            (20002, None, True),
            (30003, None, False),
        ):
            with self.subTest(uid=uid, gid=gid):
                self.assertEqual(self.bot._pass_filter(self.event(uid=uid, gid=gid)),
                                 allowed)

    def test_blacklist_wins_with_empty_or_matching_whitelists(self):
        for users, groups in product(([], [20002]), ([], [77])):
            with self.subTest(users=users, groups=groups):
                self.configure(users=users, groups=groups, blocked=[77])
                self.assertFalse(self.bot._pass_filter(self.event(gid=77)))
                self.assertTrue(self.bot._pass_filter(self.event()))

    def test_blacklist_only_rejects_the_matching_group(self):
        self.configure(blocked=[77])
        self.assertFalse(self.bot._pass_filter(self.event(gid=77)))
        self.assertTrue(self.bot._pass_filter(self.event(gid=88)))
        self.assertTrue(self.bot._pass_filter(self.event()))

    def test_string_and_integer_config_ids_are_equivalent(self):
        for user, group, blocked in product((20002, "20002"),
                                             (77, "77"), (88, "88")):
            with self.subTest(user=user, group=group, blocked=blocked):
                self.configure(users=[user], groups=[group, blocked], blocked=[blocked])
                self.assertTrue(self.bot._pass_filter(self.event(gid=77)))
                self.assertFalse(self.bot._pass_filter(self.event(gid=88)))
                self.assertFalse(self.bot._pass_filter(self.event(uid=30003, gid=77)))

    def test_non_message_events_keep_the_existing_bypass(self):
        self.configure(users=[30003], groups=[88], blocked=[77])
        for post_type, gid in product(("notice", "request", "meta_event",
                                       "message_sent", "unknown"), (None, 77)):
            with self.subTest(post_type=post_type, gid=gid):
                self.assertTrue(self.bot._pass_filter(self.event(gid=gid, post_type=post_type)))

    def test_owner_status_does_not_bypass_message_filters(self):
        for owners in ([], [20002], ["20002"]):
            with self.subTest(owners=owners):
                self.configure(users=[30003], owners=owners)
                self.assertFalse(self.bot._pass_filter(self.event()))
                self.assertFalse(self.bot._pass_filter(self.event(gid=77)))
                self.configure(groups=[88], owners=owners)
                self.assertFalse(self.bot._pass_filter(self.event(gid=77)))
                self.configure(users=[20002], groups=[77], blocked=[77], owners=owners)
                self.assertFalse(self.bot._pass_filter(self.event(gid=77)))
                self.configure(users=[20002], groups=[77], owners=owners)
                self.assertTrue(self.bot._pass_filter(self.event(gid=77)))

    def test_filter_does_not_modify_configured_lists(self):
        self.configure(users=["20002", 30003], groups=["77", 88], blocked=[88],
                       owners=[20002])
        before = deepcopy(self.bot.config)
        for uid, gid in product((20002, 40004), (None, 77, 88)):
            self.bot._pass_filter(self.event(uid=uid, gid=gid))
        self.assertEqual(self.bot.config, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
