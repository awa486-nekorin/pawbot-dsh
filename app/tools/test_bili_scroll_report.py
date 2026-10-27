"""Offline tests for bili_scroll report delivery controls (@ / 补一句)."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot import bili_scroll

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


if __name__ == "__main__":
    unittest.main()
