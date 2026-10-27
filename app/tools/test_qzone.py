"""Offline QQ Space authorization and backend receipt regressions."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.qzone import Qzone, _response_ok


class ReceiptTests(unittest.TestCase):
    def test_decoded_data_and_success_envelope(self):
        for receipt in ({}, {"tid": "t1"}, {"status": "ok", "retcode": 0, "data": {}}):
            with self.subTest(receipt=receipt):
                self.assertTrue(_response_ok(receipt))

    def test_failures_or_missing_receipts_are_not_success(self):
        for receipt in (None, True, [], {"ok": False}, {"success": False},
                        {"status": "failed"}, {"status": "async"},
                        {"retcode": 1}, {"retcode": False},
                        {"status": "ok", "data": {"success": False}}):
            with self.subTest(receipt=receipt):
                self.assertFalse(_response_ok(receipt))


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_mutation_probe_never_calls_backend(self):
        with tempfile.TemporaryDirectory() as root:
            conn = SimpleNamespace(call=AsyncMock(return_value={}))
            bot = SimpleNamespace(base_dir=root, config={}, conn_for=lambda *a: conn)
            qz = Qzone(bot)
            for name in ("post", "edit", "delete", "reply", "like", "repost"):
                self.assertFalse(await qz._探测(name))
            conn.call.assert_not_awaited()

    async def test_failed_read_probe_is_not_support(self):
        with tempfile.TemporaryDirectory() as root:
            conn = SimpleNamespace(call=AsyncMock(return_value={"status": "failed"}))
            bot = SimpleNamespace(base_dir=root, config={}, conn_for=lambda *a: conn)
            qz = Qzone(bot)
            self.assertFalse(await qz._探测("list"))
            self.assertFalse(qz.支持情况()["list"])


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"qzone.enabled": True, "qzone.enforce_limits": False,
                       "qzone.owner_ids": [7], "qzone.daily_limit": 1,
                       "qzone.actions.list": "get_qzone_msg_list",
                       "qzone.actions.comments": "get_qzone_comments"}
        self.bot = SimpleNamespace(base_dir=self.tmp.name, config=self.config)
        self.qz = Qzone(self.bot)

    def ctx(self, uid=7, gid=None):
        return SimpleNamespace(event=SimpleNamespace(
            type="message", message_type="group" if gid else "private",
            user_id=uid, group_id=gid))

    def test_owner_list_cannot_be_disabled_by_legacy_switch(self):
        self.assertFalse(self.qz.check_permission(self.ctx(uid=8))[0])
        self.assertTrue(self.qz.check_permission(self.ctx())[0])

    def test_missing_context_never_gets_owner_permissions(self):
        self.assertFalse(self.qz.check_permission(None)[0])
        self.assertFalse(self.qz.check_permission(self.ctx(uid=0))[0])

    def test_owner_cannot_override_reject_priority(self):
        self.config["whitelist.blacklist_group_ids"] = [9]
        self.config["whitelist.group_ids"] = [9]
        self.config["whitelist.user_ids"] = [7]
        self.assertFalse(self.qz.check_permission(self.ctx(gid=9))[0])

    def test_user_and_group_whitelists_are_and_not_or(self):
        self.config["whitelist.user_ids"] = [8]
        self.config["whitelist.group_ids"] = [9]
        self.assertFalse(self.qz.check_permission(self.ctx(gid=9))[0])
        self.config["whitelist.user_ids"] = [7]
        self.assertFalse(self.qz.check_permission(self.ctx(gid=10))[0])
        self.assertTrue(self.qz.check_permission(self.ctx())[0])

    def test_daily_limit_always_applies_to_writes_but_not_reads(self):
        import time
        self.qz.records.append({"day": time.strftime("%Y-%m-%d"), "ts": time.time()})
        self.assertTrue(self.qz.check_permission(self.ctx())[0])
        self.assertFalse(self.qz.check_permission(self.ctx(), mutation=True)[0])


if __name__ == "__main__":
    unittest.main()
