"""Offline tests for bili_scroll report delivery controls (@ / 补一句)."""
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
from pawbot import bili_extra, bili_scroll, brain
from pawbot.llm import LLMClient
from pawbot.paths import DataPaths

NOTES = {"checked": 5, "notes": [
    {"title": "片子甲", "gist": "讲的甲", "url": "https://b23.tv/a"},
]}


class FakeBot:
    def __init__(self, **config):
        self.config = config
        self.sent = []
        self.calls = 0
        self.persona = SimpleNamespace(build=lambda **kw: "系统提示")
        self.llm = SimpleNamespace(chat_message=self._chat)

    async def _chat(self, messages, tools):
        self.calls += 1
        return {"content": "这条我也想看"}

    async def manual_send(self, target, message, is_group=True):
        self.sent.append((str(target), message, is_group))
        return {}


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def report(self, bot):
        await bili_scroll._report(bot, NOTES, "12:40")
        return bot.sent

    async def test_report_to_off_sends_nothing(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "off"})
        self.assertEqual(await self.report(bot), [])

    async def test_group_report_without_at_has_no_at_segment(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "group",
                         "tools.bili_scroll_group": "123456",
                         "account.owner_ids": ["1000000001"]})
        sent = await self.report(bot)
        self.assertEqual(len(sent), 1)
        target, text, is_group = sent[0]
        self.assertEqual((target, is_group), ("123456", True))
        self.assertNotIn("[CQ:at", text)
        self.assertIn("片子甲", text)

    async def test_group_report_at_owner_uses_owner_id(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "group",
                         "tools.bili_scroll_group": "123456",
                         "tools.bili_scroll_report_at": "owner",
                         "account.owner_ids": ["1000000001"]})
        text = (await self.report(bot))[0][1]
        self.assertTrue(text.startswith("[CQ:at,qq=1000000001] "))

    async def test_group_report_at_all(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "group",
                         "tools.bili_scroll_group": "123456",
                         "tools.bili_scroll_report_at": "all"})
        text = (await self.report(bot))[0][1]
        self.assertTrue(text.startswith("[CQ:at,qq=all] "))

    async def test_owner_report_never_at_even_when_configured(self):
        bot = FakeBot(**{"tools.bili_scroll_report_at": "all",
                         "account.owner_ids": ["1000000001"]})
        sent = await self.report(bot)
        self.assertEqual(sent[0][2], False)
        self.assertNotIn("[CQ:at", sent[0][1])

    async def test_comment_sends_second_message_in_her_voice(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "group",
                         "tools.bili_scroll_group": "123456",
                         "tools.bili_scroll_report_comment": True})
        sent = await self.report(bot)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1], ("123456", "这条我也想看", True))
        self.assertEqual(bot.calls, 1)

    async def test_comment_is_off_by_default(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "group",
                         "tools.bili_scroll_group": "123456"})
        self.assertEqual(len(await self.report(bot)), 1)
        self.assertEqual(bot.calls, 0)

    async def test_comment_failure_never_breaks_the_report(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "group",
                         "tools.bili_scroll_group": "123456",
                         "tools.bili_scroll_report_comment": True})

        async def boom(messages, tools):
            raise RuntimeError("llm down")
        bot.llm.chat_message = boom
        sent = await self.report(bot)
        self.assertEqual(len(sent), 1)
        self.assertIn("片子甲", sent[0][1])

    async def test_empty_model_reply_sends_no_second_message(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "group",
                         "tools.bili_scroll_group": "123456",
                         "tools.bili_scroll_report_comment": True})

        async def blank(messages, tools):
            return {"content": "   "}
        bot.llm.chat_message = blank
        self.assertEqual(len(await self.report(bot)), 1)

    async def test_no_notes_means_no_comment_call(self):
        bot = FakeBot(**{"tools.bili_scroll_report_to": "group",
                         "tools.bili_scroll_group": "123456",
                         "tools.bili_scroll_report_comment": True})
        await bili_scroll._report(bot, {"checked": 3, "notes": []}, "12:40")
        self.assertEqual(bot.calls, 0)
        self.assertEqual(len(bot.sent), 1)


class ReactAfterRoundTests(unittest.IsolatedAsyncioTestCase):
    """刷完一轮按云端原版调 bili_extra.react，并把结果放进 reacted / 报告。"""

    ITEM = {
        "bvid": "BV1xx411c7mD",
        "title": "片子甲",
        "author": "UP甲",
        "play": 123456,
        "duration": 120,
        "desc": "简介",
        "url": "https://www.bilibili.com/video/BV1xx411c7mD",
    }

    def _bot(self):
        bot = FakeBot(**{"tools.bili_learn_after_scroll": False,
                         "tools.bili_scroll_topic": "科技"})
        bot.base_dir = self.tmp
        bot.paths = DataPaths(root=self.tmp, base_dir=self.tmp)
        return bot

    def setUp(self):
        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        self.tmp = handle.name

    def _patches(self, react):
        judge = {"worth": True, "gist": "讲的甲", "points": ["一条"],
                 "why": "值得", "take": "还行",
                 "like": True, "fav": False, "coin": 2}
        return [
            patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()),
            patch.object(bili_scroll, "_ensure_credential", new=lambda bot: True),
            patch.object(bili_scroll, "_search",
                         new=AsyncMock(return_value=[dict(self.ITEM)])),
            patch.object(bili_scroll, "_subtitle",
                         new=AsyncMock(return_value={"text": "字幕原文",
                                                     "status": "ok"})),
            patch.object(bili_scroll, "_comments", new=AsyncMock(return_value=[])),
            patch.object(bili_scroll, "_judge", new=AsyncMock(return_value=judge)),
            patch.object(bili_scroll, "_write_note",
                         new=lambda bot, item, j, sub: os.path.join(self.tmp, "n.md")),
            patch.object(bili_scroll, "_load_seen", return_value={}),
            patch.object(bili_scroll, "_save_seen", new=lambda bot, seen: None),
            patch.object(bili_extra, "react", new=react),
        ]

    async def _run(self, react):
        bot = self._bot()
        classes = self._patches(react)
        for cls in classes:
            cls.start()
            self.addCleanup(cls.stop)
        return await bili_scroll.run_round(bot, want=1)

    async def test_round_reacts_and_reports_it(self):
        react = AsyncMock(return_value={"like": True, "fav": False, "coin": 2,
                                        "errors": []})
        out = await self._run(react)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reacted"], {"like": 1, "fav": 0, "coin": 2})
        self.assertEqual(out["notes"][0]["react"]["like"], True)
        react.assert_awaited_once()
        self.assertEqual(react.await_args.args[1], self.ITEM["bvid"])
        self.assertTrue(react.await_args.args[2]["like"])
        text = bili_scroll._fmt_report(out, "12:40")
        self.assertIn("我自己也动了手", text)
        self.assertIn("点赞 1", text)
        self.assertIn("投币 2", text)

    async def test_react_failure_does_not_drop_the_note(self):
        react = AsyncMock(side_effect=RuntimeError("boom"))
        out = await self._run(react)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["notes"]), 1)
        self.assertEqual(out["reacted"], {"like": 0, "fav": 0, "coin": 0})
        self.assertTrue(out["notes"][0]["react"]["errors"])
        # 没动成就不许在汇报里说她动了手。
        self.assertNotIn("我自己也动了手", bili_scroll._fmt_report(out, "12:40"))



class JudgeRoutingTests(unittest.IsolatedAsyncioTestCase):
    """_judge 的模型路由：真实 LLMClient 走 life_model helper，旧 Fake 走 chat。"""

    ITEM = {
        "bvid": "BV1xx411c7mD",
        "title": "片子甲",
        "author": "UP甲",
        "play": 123456,
        "duration": 120,
        "desc": "简介",
        "url": "https://www.bilibili.com/video/BV1xx411c7mD",
    }
    JUDGE = {"worth": True, "gist": "讲的甲", "points": ["一条"],
             "why": "值得", "take": "还行"}

    async def test_judge_routes_real_client_through_dsh_brain_helper(self):
        bot = FakeBot(**{"tools.bili_scroll_max_chars": 12000,
                         "llm.provider": "dsh",
                         "llm.brain_timeout": 5,
                         "llm.base_url": "https://api.example.invalid/v1",
                         "llm.api_key": ""})
        bot.llm = LLMClient(bot.config)
        payloads = []

        def fake_turn(payload, timeout=None):
            payloads.append(payload)
            return {"text": json.dumps(self.JUDGE, ensure_ascii=False)}

        with patch.object(brain, "_URL", "http://127.0.0.1:50311/turn"), \
                patch.object(brain, "_TOKEN", "pdbrain_" + "d" * 64), \
                patch.object(brain, "turn", side_effect=fake_turn):
            judge = await bili_scroll._judge(bot, self.ITEM, "字幕原文")

        self.assertEqual(judge["worth"], True)
        self.assertEqual(judge["gist"], "讲的甲")
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertRegex(payload["conv"],
                         r"^life:bili-scroll-judge:[0-9a-f]{32}$")
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["context"], {
            "autonomous": True, "native_tools": False,
            "is_owner": False, "computer_access": False})
        self.assertIn("字幕原文", payload["user"])
        # 真实客户端的旧 chat() 没有被调用（FakeBot 的 chat_message 计数保持 0）。
        self.assertEqual(bot.calls, 0)

    async def test_judge_legacy_chat_only_fake_still_works(self):
        class ChatOnly:
            def __init__(self, reply):
                self.reply = reply
                self.calls = 0

            async def chat(self, messages):
                self.calls += 1
                return self.reply

        bot = FakeBot(**{"tools.bili_scroll_max_chars": 12000})
        fake = ChatOnly(json.dumps(self.JUDGE, ensure_ascii=False))
        bot.llm = fake
        judge = await bili_scroll._judge(bot, self.ITEM, "字幕原文")
        self.assertEqual(judge["worth"], True)
        self.assertEqual(fake.calls, 1)

    async def test_judge_legacy_chat_only_fake_rejects_mock(self):
        class ChatOnly:
            async def chat(self, messages):
                return "（mock 模式，未配置 api_key）我听到你说：..."

        bot = FakeBot(**{"tools.bili_scroll_max_chars": 12000})
        bot.llm = ChatOnly()
        self.assertIsNone(await bili_scroll._judge(bot, self.ITEM, "字幕原文"))


if __name__ == "__main__":
    unittest.main()
