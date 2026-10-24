"""群聊旁听与沉默协议的离线回归测试，不读取真实聊天或调用模型。"""

import sys
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.agent import Agent
from pawbot.bus import Bus, Handler
from pawbot.context import Context
from pawbot.core import Bot
from pawbot.event import parse
from plugins import ai_chat, group_tools, random_interject


def raw_message(**values):
    raw = {"post_type": "message", "message_type": "group", "self_id": 1,
           "group_id": 77, "user_id": 2, "message_id": 123,
           "message": [{"type": "text", "data": {"text": "今天这个游戏更新挺有意思"}}],
           "sender": {"nickname": "阿明"}}
    raw.update(values)
    return raw


class GroupObserveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_chat._LOCKS.clear()
        ai_chat._SETTLE.clear()
        ai_chat._PENDING.clear()
        self.config = {"bot.nickname": "大肥鱼", "bot.wake_words": ["大肥鱼"],
                       "random_interject.enabled": True,
                       "chat.settle_sec": 0, "chat.settle_max": 0}
        self.bot = SimpleNamespace(
            config=self.config,
            agent=SimpleNamespace(run=AsyncMock(return_value=("<pass>", []))),
            store=SimpleNamespace(history=Mock(return_value=[]), append=Mock()),
            topic=SimpleNamespace(seen=Mock()), _pass_rate=Mock(return_value=True))
        self.ctx = Context(parse(raw_message()), self.bot, self.config)
        self.ctx.state["wake"] = False
        self.ctx.reply = AsyncMock(return_value={"message_id": 5})

    async def test_non_wake_message_is_considered_and_pass_is_silent(self):
        await ai_chat.ai_chat(self.ctx)
        self.bot.agent.run.assert_awaited_once()
        self.ctx.reply.assert_not_awaited()
        self.bot.store.append.assert_not_called()
        self.bot.topic.seen.assert_not_called()
        self.bot._pass_rate.assert_not_called()
        self.assertNotIn("ai_spoke", self.ctx.state)

    async def test_interested_reply_is_sent_and_recorded_once(self):
        self.bot.agent.run.return_value = ("那个新地图确实不错。", [])
        await ai_chat.ai_chat(self.ctx)
        self.ctx.reply.assert_awaited_once()
        self.bot.store.append.assert_called_once_with(
            "group_77", "assistant", "那个新地图确实不错。", who="我")
        self.bot._pass_rate.assert_called_once()
        self.assertTrue(self.ctx.state["ai_spoke"])

    async def test_pass_with_prepared_media_sends_media_only(self):
        # 只发一张表情包/图、不配字是正常的「反应」；<pass> 在这里表示不发文字
        self.ctx.event.to_me = True
        self.ctx.state["wake"] = True
        media = [{"type": "text", "data": {"text": "[STICKER:file:///x.png]"}}]
        self.bot.agent.run.return_value = (" <pass> \n", list(media))
        await ai_chat.ai_chat(self.ctx)
        self.ctx.reply.assert_awaited_once()
        self.assertEqual(self.ctx.reply.await_args.args[0], media)
        self.bot.store.append.assert_not_called()

    async def test_command_self_message_and_never_are_excluded(self):
        self.ctx.matched_command = "ping"
        await ai_chat.ai_chat(self.ctx)
        self.ctx.matched_command = ""
        self.ctx.event.user_id = 1
        await ai_chat.ai_chat(self.ctx)
        self.ctx.event.user_id = 2
        self.config["chat.reply_mode"] = "never"
        await ai_chat.ai_chat(self.ctx)
        self.bot.agent.run.assert_not_awaited()

    async def test_empty_message_does_not_force_a_reply(self):
        self.ctx.event.plain = ""
        await ai_chat.ai_chat(self.ctx)
        self.bot.agent.run.assert_not_awaited()
        self.ctx.reply.assert_not_awaited()

    async def test_legacy_plugins_do_not_bypass_group_decision(self):
        self.ctx.state["group_observe"] = True
        self.ctx.event.plain = "早安"
        await group_tools.morning(self.ctx)
        self.ctx.event.plain = "晚安"
        await group_tools.night(self.ctx)
        await random_interject.interject(self.ctx)
        self.ctx.reply.assert_not_awaited()

    async def test_suppressed_send_does_not_record_assistant(self):
        self.bot.agent.run.return_value = ("聊一句。", [])
        self.ctx.reply.return_value = None
        await ai_chat.ai_chat(self.ctx)
        self.bot.store.append.assert_not_called()
        self.assertNotIn("ai_spoke", self.ctx.state)

    def test_observer_prompt_also_exists_when_tools_are_off(self):
        hint = Agent(self.bot)._participation_hint(self.ctx)
        self.assertIn("<pass>", hint)
        self.assertIn("不调用工具", hint)
        self.assertIn("普通群消息", hint)


class CoreObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_ingests_non_wake_message_and_runs_ai(self):
        bot = object.__new__(Bot)
        bot.config = {"bot.wake_words": ["大肥鱼"], "chat.settle_sec": 0, "chat.settle_max": 0}
        bot.stats = {"recv": 0}
        bot.inbound = {}
        bot.recent_events = deque()
        bot._rate = {}
        bot.store = SimpleNamespace(append=Mock(), history=Mock(return_value=[]))
        bot.topic = SimpleNamespace(seen=Mock())
        bot.mood = SimpleNamespace(save=Mock(), on_chatted=Mock(), on_mentioned=Mock())
        bot._recall_person = AsyncMock()
        bot._maybe_compress = AsyncMock()
        bot.agent = SimpleNamespace(run=AsyncMock(return_value=("<pass>", [])))
        bus = Bus()
        bus.register(Handler(fn=group_tools.morning, kind="keyword", keyword="早安", priority=40, block=False))
        bus.register(Handler(fn=ai_chat.ai_chat, kind="message", priority=90))
        with patch("pawbot.core.BUS", bus), patch("pawbot.core.perceive.enrich", new_callable=AsyncMock) as enrich:
            await bot._on_event(None, raw_message(message=[{"type": "text", "data": {"text": "早安"}}]))
            bot.agent.run.assert_awaited_once()
            enrich.assert_awaited_once()
            bot.store.append.assert_called_once()
            bot.mood.save.assert_not_called()
            await bot._on_event(None, raw_message(user_id=1))
            self.assertEqual(bot.agent.run.await_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
