"""Offline Space mutation authorization, quotas and receipt regressions."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot.qzone import Qzone


def actor(uid=123, gid=None):
    return SimpleNamespace(event=SimpleNamespace(
        user_id=uid, group_id=gid, type="message",
        message_type="group" if gid else "private"))


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.conn = SimpleNamespace(call=AsyncMock(return_value={"tid": "t1"}))
        self.config = {"qzone.enabled": True,
                       "qzone.enforce_limits": False, "qzone.owner_ids": [123],
                       "qzone.daily_limit": 3,
                       "qzone.actions.edit": "configured_edit_adapter",
                       "qzone.actions.delete": "configured_delete_adapter",
                       "qzone.actions.reply": "configured_reply_adapter"}
        self.bot = SimpleNamespace(base_dir=self.root.name, config=self.config, conn_for=lambda: self.conn)
        self.qz = Qzone(self.bot)
        self.ctx = actor()

    async def assert_denied(self, ctx):
        with self.assertRaises(RuntimeError):
            await self.qz.post("text", ctx=ctx)
        for result in (await self.qz.edit("t", "text", ctx=ctx),
                       await self.qz.delete("t", ctx=ctx),
                       await self.qz.reply("t", "c", "text", ctx=ctx)):
            self.assertFalse(result["ok"])
        self.conn.call.assert_not_awaited()
        self.assertEqual(self.qz.records, [])

    async def test_missing_zero_and_unlisted_actors_cannot_write(self):
        for ctx in (None, SimpleNamespace(user_id=123), actor(0), actor(456), actor(-1)):
            with self.subTest(ctx=ctx):
                await self.assert_denied(ctx)

    async def test_omitted_context_fails_closed_even_after_preflight(self):
        self.assertTrue(self.qz.check_permission(self.ctx, mutation=True)[0])
        with self.assertRaises(RuntimeError):
            await self.qz.post("text")
        self.assertFalse((await self.qz.reply("t", "c", "text"))["ok"])
        self.conn.call.assert_not_awaited()

    async def test_missing_nonmessage_and_nondecimal_actors_cannot_write(self):
        for context in (None, SimpleNamespace(user_id=123), actor(0), actor(-1),
                        actor("+123"),
                        SimpleNamespace(event=SimpleNamespace(type="notice", user_id=123))):
            with self.subTest(context=context):
                await self.assert_denied(context)

    async def test_feature_gate_is_retained(self):
        self.config["qzone.enabled"] = False
        await self.assert_denied(self.ctx)
        self.assertFalse((await self.qz.list_own(ctx=self.ctx))["ok"])

    async def test_explicit_owners_required_despite_legacy_limit_bypass(self):
        for owners in ([], None, "123", [456]):
            self.config["qzone.owner_ids"] = owners
            await self.assert_denied(self.ctx)

    async def test_reject_priority_and_whitelist_and_semantics(self):
        self.config.update({"whitelist.user_ids": [123], "whitelist.group_ids": [9],
                            "whitelist.blacklist_group_ids": [9]})
        await self.assert_denied(actor(123, 9))
        self.config["whitelist.blacklist_group_ids"] = []
        await self.assert_denied(actor(123, 8))
        self.config["whitelist.user_ids"] = [456]
        await self.assert_denied(actor(123, 9))

    async def test_invalid_daily_limits_cannot_disable_security(self):
        for value in (0, -1, None, True, "bad", 1.5, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.config["qzone.daily_limit"] = value
                await self.assert_denied(self.ctx)
        self.config["qzone.daily_limit"] = 3

    async def test_passive_reads_work_when_daily_write_quota_is_exhausted(self):
        self.config["qzone.daily_limit"] = 1
        self.qz.records = [{"day": time.strftime("%Y-%m-%d"), "ts": time.time()}]
        self.assertFalse(self.qz.check_permission(self.ctx, mutation=True)[0])
        self.assertTrue((await self.qz.list_own(ctx=self.ctx))["ok"])
        self.assertTrue((await self.qz.comments("t", ctx=self.ctx))["ok"])

    async def test_group_notice_and_missing_context_remain_private(self):
        contexts = (
            None,
            actor(123, 9),
            SimpleNamespace(event=SimpleNamespace(
                user_id=123, type="notice", message_type="private")),
        )
        for ctx in contexts:
            self.assertFalse((await self.qz.list_own(ctx=ctx))["ok"])
            self.assertFalse((await self.qz.comments("t", ctx=ctx))["ok"])
        self.conn.call.assert_not_awaited()

    async def test_passive_reply_with_actor_remains_available(self):
        result = await self.qz.reply("t", "c", "hello", ctx=self.ctx)
        self.assertTrue(result["ok"])
        self.conn.call.assert_awaited_once_with("configured_reply_adapter", timeout=15,
                                                tid="t", comment_id="c", content="hello")
        self.assertEqual(self.qz.today_count(), 1)

    async def test_all_mutations_use_one_authorized_call_and_receipt(self):
        for method, args, action in (("post", ("hello",), "send_qzone_msg"),
                                     ("edit", ("t", "new"), "configured_edit_adapter"),
                                     ("delete", ("t",), "configured_delete_adapter"),
                                     ("reply", ("t", "c", "hi"), "configured_reply_adapter")):
            self.qz.records = []
            self.conn.call.reset_mock()
            result = await getattr(self.qz, method)(*args, ctx=self.ctx)
            self.assertTrue(result.get("ok", True))
            self.conn.call.assert_awaited_once()
            self.assertEqual(self.conn.call.await_args.args, (action,))
            self.assertEqual(self.qz.records[0]["status"], "confirmed")

    async def test_unconfirmed_receipts_never_report_success(self):
        for receipt in (None, True, [], {"status": "failed"}, {"data": {"success": False}}):
            self.conn.call.return_value = receipt
            self.qz.records = []
            with self.assertRaises(RuntimeError):
                await self.qz.post("hello", ctx=self.ctx)
            self.assertEqual(self.qz.records[0]["status"], "unconfirmed")
            self.qz.records = []
            self.assertFalse((await self.qz.reply("t", "c", "hi", ctx=self.ctx))["ok"])

    async def test_no_guessed_mutation_actions_or_like_repost(self):
        for name in ("edit", "delete", "reply"):
            self.config.pop(f"qzone.actions.{name}")
            result = await self.qz._mutate(name, {}, ctx=self.ctx)
            self.assertFalse(result["ok"])
        for name in ("like", "repost"):
            self.config[f"qzone.actions.{name}"] = "do_not_call"
            self.assertFalse((await self.qz._mutate(name, {}, ctx=self.ctx))["ok"])
        self.conn.call.assert_not_awaited()

    async def test_daily_quota_and_reload(self):
        self.config["qzone.daily_limit"] = 2
        await self.qz.post("one", ctx=self.ctx)
        self.assertTrue((await self.qz.edit("t", "two", ctx=self.ctx))["ok"])
        self.assertFalse(Qzone(self.bot).check_permission(self.ctx, mutation=True)[0])
        self.assertEqual(self.conn.call.await_count, 2)

    async def test_concurrent_calls_cannot_race_quota(self):
        self.config["qzone.daily_limit"] = 1
        results = await asyncio.gather(*(self.qz.reply("t", "c", "hi", ctx=self.ctx) for _ in range(8)))
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.conn.call.assert_awaited_once()

    async def test_uncertain_failure_and_cancellation_reserve_quota(self):
        self.config["qzone.daily_limit"] = 1
        for exc in (TimeoutError(), asyncio.CancelledError()):
            self.qz.records = []
            self.conn.call.side_effect = exc
            if isinstance(exc, asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await self.qz.reply("t", "c", "hi", ctx=self.ctx)
            else:
                self.assertFalse((await self.qz.reply("t", "c", "hi", ctx=self.ctx))["ok"])
            self.assertEqual(Qzone(self.bot).today_count(), 1)
            self.assertEqual(Qzone(self.bot).records[0]["status"], "unconfirmed")
            self.assertFalse(Qzone(self.bot).check_permission(self.ctx, mutation=True)[0])

    async def test_final_receipt_save_failure_does_not_report_success(self):
        save = self.qz._save
        calls = 0
        def fail_second_save():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk failure")
            save()
        with patch.object(self.qz, "_save", side_effect=fail_second_save):
            result = await self.qz.reply("t", "c", "hello", ctx=self.ctx)
        self.assertFalse(result["ok"])
        self.conn.call.assert_awaited_once()
        self.assertFalse(self.qz.check_permission(self.ctx, mutation=True)[0])
        self.assertEqual(Qzone(self.bot).records[0]["status"], "pending")

    async def test_comments_use_one_complete_authorized_request(self):
        self.conn.call.return_value = {"status": "ok", "data": {"comments": []}}
        self.assertTrue((await self.qz.comments("t1", ctx=self.ctx))["ok"])
        self.conn.call.assert_awaited_once_with("get_qzone_comments", tid="t1", timeout=15)

    async def test_ledger_write_failure_prevents_dispatch(self):
        with patch.object(self.qz, "_save", side_effect=OSError("disk failure")):
            self.assertFalse((await self.qz.delete("t", ctx=self.ctx))["ok"])
        self.conn.call.assert_not_awaited()
        self.assertFalse(self.qz.check_permission(self.ctx, mutation=True)[0])

    async def test_corrupt_ledger_fails_closed(self):
        path = Path(self.qz.path)
        path.parent.mkdir(exist_ok=True)
        for text in ("not json", "{}", '[{"day": "today", "ts": "bad"}]'):
            path.write_text(text, encoding="utf-8")
            qz = Qzone(self.bot)
            self.assertFalse((await qz.delete("t", ctx=self.ctx))["ok"])
        self.conn.call.assert_not_awaited()

    async def test_daily_ledger_never_truncates_current_quota(self):
        self.qz.records = [{"day": time.strftime("%Y-%m-%d"), "ts": time.time()}] * 250
        self.qz._save()
        self.assertEqual(Qzone(self.bot).today_count(), 250)


if __name__ == "__main__":
    unittest.main()
