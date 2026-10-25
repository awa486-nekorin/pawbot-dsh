"""Offline tests for automatic Bilibili dynamic posting (default off)."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot import bili_post


class FakeBot:
    def __init__(self, root, **config):
        self.base_dir = root
        self.config = config
        self.persona = SimpleNamespace(build=lambda **kw: "系统提示")
        self.llm = SimpleNamespace(chat_message=self._chat)

    async def _chat(self, messages, tools):
        return {"content": "随手发一条，今天刷到的片子挺有意思"}


class PostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = root.name

    def bot(self, **config):
        base = {"tools.bili_post_enabled": True, "tools.bili_post_daily_limit": 1}
        return FakeBot(self.root, **{**base, **config})

    def ledger(self):
        p = Path(self.root) / "data" / "bili_post.json"
        return json.loads(p.read_text(encoding="utf-8"))["records"] if p.exists() else []

    async def post(self, bot, action, text="发一条动态", cred=True):
        with patch.object(bili_post.bili_scroll, "_bili_jct", return_value="jct" if cred else ""), \
                patch.object(bili_post.bili_scroll, "action", side_effect=action) as call:
            result = await bili_post.post_once(bot, text)
        return result, call

    async def test_default_is_off_and_never_calls_backend(self):
        bot = FakeBot(self.root)
        result, call = await self.post(bot, lambda *a, **k: {"ok": True, "data": {"success": True}})
        self.assertFalse(result["ok"])
        call.assert_not_called()
        self.assertEqual(self.ledger(), [])

    async def test_only_literal_true_enables_posting(self):
        for value in ("true", 1, "yes", None, 0):
            with self.subTest(value=value):
                bot = self.bot(**{"tools.bili_post_enabled": value})
                result, call = await self.post(bot, lambda *a, **k: {"ok": True, "data": {"success": True}})
                self.assertFalse(result["ok"])
                call.assert_not_called()

    async def test_missing_bili_jct_blocks_posting(self):
        bot = self.bot()
        result, call = await self.post(bot, lambda *a, **k: {"ok": True, "data": {"success": True}},
                                       cred=False)
        self.assertFalse(result["ok"])
        self.assertIn("bili_jct", result["reason"])
        call.assert_not_called()
        self.assertEqual(self.ledger(), [])

    async def test_success_records_receipt_and_calls_backend_once(self):
        bot = self.bot()
        result, call = await self.post(
            bot, lambda *a, **k: {"ok": True, "data": {"success": True, "data": {"dynamic_id": "777"}}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["dynamic_id"], "777")
        call.assert_called_once()
        self.assertEqual(call.call_args.args[1], "dynamic")
        self.assertEqual(call.call_args.args[2], {"text": "发一条动态"})
        rows = self.ledger()
        self.assertEqual(rows[0]["status"], "confirmed")
        self.assertEqual(rows[0]["dynamic_id"], "777")

    async def test_backend_success_false_is_not_a_receipt(self):
        bot = self.bot()
        result, call = await self.post(
            bot, lambda *a, **k: {"ok": True, "data": {"success": False, "error": "风控"}})
        self.assertFalse(result["ok"])
        self.assertIn("风控", result["reason"])
        call.assert_called_once()
        self.assertEqual(self.ledger()[0]["status"], "unconfirmed")

    async def test_daily_limit_is_enforced_from_disk(self):
        bot = self.bot(**{"tools.bili_post_daily_limit": 1})
        ok, _ = await self.post(bot, lambda *a, **k: {"ok": True, "data": {"success": True}}, text="第一条")
        self.assertTrue(ok["ok"])
        second, call = await self.post(
            bot, lambda *a, **k: {"ok": True, "data": {"success": True}}, text="第二条")
        self.assertFalse(second["ok"])
        self.assertIn("上限", second["reason"])
        call.assert_not_called()
        self.assertEqual(len(self.ledger()), 1)

    async def test_positive_daily_limit_required(self):
        for value in (0, -1, True, "abc"):
            with self.subTest(value=value):
                bot = self.bot(**{"tools.bili_post_daily_limit": value})
                result, call = await self.post(
                    bot, lambda *a, **k: {"ok": True, "data": {"success": True}})
                self.assertFalse(result["ok"])
                call.assert_not_called()

    async def test_identical_text_is_not_posted_twice_in_a_day(self):
        bot = self.bot(**{"tools.bili_post_daily_limit": 5})
        await self.post(bot, lambda *a, **k: {"ok": True, "data": {"success": True}}, text="同一句")
        again, call = await self.post(
            bot, lambda *a, **k: {"ok": True, "data": {"success": True}}, text="同一句")
        self.assertFalse(again["ok"])
        self.assertIn("一模一样", again["reason"])
        call.assert_not_called()

    async def test_empty_composition_costs_no_quota(self):
        bot = self.bot()
        result, call = await self.post(bot, lambda *a, **k: {"ok": True, "data": {"success": True}},
                                       text="   ")
        self.assertFalse(result["ok"])
        call.assert_not_called()
        self.assertEqual(self.ledger(), [])

    async def test_timeout_reserves_quota_and_marks_unconfirmed(self):
        def boom(*a, **k):
            raise TimeoutError()
        bot = self.bot()
        result, call = await self.post(bot, boom)
        self.assertFalse(result["ok"])
        call.assert_called_once()
        self.assertEqual(self.ledger()[0]["status"], "unconfirmed")
        self.assertEqual(bili_post.today_count(bot), 1)

    async def test_cancellation_is_persisted_then_reraised(self):
        def cancel(*a, **k):
            raise asyncio.CancelledError()
        bot = self.bot()
        with self.assertRaises(asyncio.CancelledError):
            await self.post(bot, cancel)
        self.assertEqual(self.ledger()[0]["status"], "unconfirmed")

    async def test_ledger_write_failure_prevents_publish(self):
        bot = self.bot()
        with patch.object(bili_post, "_save", side_effect=OSError("disk full")):
            result, call = await self.post(
                bot, lambda *a, **k: {"ok": True, "data": {"success": True}})
        self.assertFalse(result["ok"])
        call.assert_not_called()

    async def test_receipt_save_failure_does_not_report_success(self):
        bot = self.bot()
        real = bili_post._save
        calls = []

        def flaky(root, rows):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk full")
            real(root, rows)

        with patch.object(bili_post, "_save", side_effect=flaky):
            result, _ = await self.post(
                bot, lambda *a, **k: {"ok": True, "data": {"success": True}})
        self.assertFalse(result["ok"])

    async def test_compose_uses_llm_and_respects_char_cap(self):
        bot = self.bot(**{"tools.bili_post_max_chars": 20})

        async def long_chat(messages, tools):
            return {"content": "啊" * 80}

        bot.llm.chat_message = long_chat
        with patch.object(bili_post.bili_scroll, "recent_notes",
                          return_value=[{"name": "n.md"}]), \
                patch.object(bili_post.bili_scroll, "read_note", return_value="笔记正文"), \
                patch.object(bili_post.bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_post.bili_scroll, "action",
                             return_value={"ok": True, "data": {"success": True}}) as call:
            result = await bili_post.post_once(bot)
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(call.call_args.args[2]["text"]), 20)

    async def test_compose_failure_is_silent_and_costs_nothing(self):
        bot = self.bot()

        async def down(messages, tools):
            raise RuntimeError("llm down")
        bot.llm.chat_message = down
        with patch.object(bili_post.bili_scroll, "recent_notes",
                          return_value=[{"name": "n.md"}]), \
                patch.object(bili_post.bili_scroll, "read_note", return_value="笔记正文"), \
                patch.object(bili_post.bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_post.bili_scroll, "action") as call:
            result = await bili_post.post_once(bot)
        self.assertFalse(result["ok"])
        call.assert_not_called()
        self.assertEqual(self.ledger(), [])


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def test_times_parse_and_wait_math(self):
        bot = FakeBot(".", **{"tools.bili_post_times": ["12:30", " 7:05 ", "bad", "25:00"]})
        self.assertEqual(bili_post._parse_times(bot), [7 * 60 + 5, 12 * 60 + 30])
        from datetime import datetime
        self.assertAlmostEqual(
            bili_post._wait_seconds([7 * 60 + 5], datetime(2026, 1, 1, 7, 0, 0)), 5 * 60.0, places=1)

    async def test_scheduler_exits_immediately_without_times(self):
        bot = FakeBot(".", **{"tools.bili_post_times": []})
        await asyncio.wait_for(bili_post.scheduler(bot), timeout=2)


if __name__ == "__main__":
    unittest.main()
