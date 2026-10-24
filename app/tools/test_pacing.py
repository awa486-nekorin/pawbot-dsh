"""Offline pacing, coalescing, and tool-budget regressions."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.agent import Agent
from pawbot.context import Context
from pawbot.event import parse
from pawbot.safety import SafetyGuard
from plugins import ai_chat


def make_ctx(bot, *, to_me=False, text="hello"):
    raw = {"post_type": "message", "message_type": "group", "self_id": 1,
           "group_id": 77, "user_id": 2, "message_id": 1,
           "message": [{"type": "text", "data": {"text": text}}],
           "sender": {"nickname": "阿明"}}
    ev = parse(raw)
    ev.to_me = to_me
    ctx = Context(ev, bot, bot.config)
    ctx.reply = AsyncMock(return_value={"message_id": 1})
    return ctx


class AiChatPacingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_chat._LOCKS.clear()
        ai_chat._PENDING.clear()
        ai_chat._SETTLE.clear()
        self.bot = SimpleNamespace(
            config={"bot.nickname": "大肥鱼", "bot.wake_words": [],
                    "chat.settle_sec": 0, "chat.settle_max": 0,
                    "tts.enabled": False},
            inbound={"group_77": 0},
            store=SimpleNamespace(history=Mock(return_value=[]), append=Mock()),
            topic=SimpleNamespace(seen=Mock()), _pass_rate=Mock(return_value=True),
            agent=SimpleNamespace(run=AsyncMock(return_value=("ok", []))))
        self.ctx = make_ctx(self.bot)

    async def test_locked_messages_coalesce_to_one_latest_followup(self):
        entered, resume = __import__("asyncio").Event(), __import__("asyncio").Event()
        async def run(ctx, text, history):
            if text == "hello":
                entered.set()
                await resume.wait()
            return "ok", []
        self.bot.agent.run.side_effect = run
        task = __import__("asyncio").create_task(ai_chat.ai_chat(self.ctx))
        await entered.wait()
        for text in ("second", "third", "latest"):
            ctx = make_ctx(self.bot, text=text)
            await ai_chat.ai_chat(ctx)
        resume.set()
        await task
        self.assertEqual(self.bot.agent.run.await_count, 2)
        self.assertEqual(self.bot.agent.run.await_args_list[-1].args[1], "latest")

    async def _arrivals_case(self, *, addressed, media, count, should_send, pending=False):
        async def run(ctx, text, history):
            if text == "hello":
                self.bot.inbound["group_77"] = count
                if pending:
                    # 真的还有一条更新的消息排队等他回答
                    ai_chat._PENDING["group_77"] = (make_ctx(self.bot, text="newer"), "newer")
                return "answer", ([{"type": "image"}] if media else [])
            return "<pass>", []
        self.bot.agent.run.side_effect = run
        ctx = make_ctx(self.bot, to_me=addressed)
        await ai_chat.ai_chat(ctx)
        self.assertEqual(ctx.reply.await_count, int(should_send))

    async def test_stale_answer_discarded_after_two_arrivals(self):
        await self._arrivals_case(addressed=False, media=False, count=2,
                                  should_send=False, pending=True)
        self.assertEqual(self.bot.agent.run.await_count, 2)   # 只在最后那条上追一次

    async def test_two_arrivals_without_pending_still_send(self):
        # 两条指令插进来不会产生 pending，旧回答不该被吞
        await self._arrivals_case(addressed=False, media=False, count=2, should_send=True)

    async def test_messages_inside_settle_window_are_batched(self):
        self.bot.config.update({"chat.settle_sec": 0.08, "chat.settle_max": 0.3})
        seen = []

        async def run(ctx, text, history):
            seen.append(text)
            return "<pass>", []

        self.bot.agent.run.side_effect = run
        first = await ai_chat.ai_chat(self.ctx)      # 后台攒话任务
        await asyncio.sleep(0.02)
        for text in ("第二句", "第三句"):
            await ai_chat.ai_chat(make_ctx(self.bot, text=text))
        await first
        self.assertEqual(seen, ["第三句"])      # 只答最后一条，前面已在历史里
        self.assertEqual(self.bot.agent.run.await_count, 1)

    async def test_answer_is_style_polished_before_send(self):
        self.bot.config["reply.length_enforce"] = False     # 这条只验风格清理，不验句数截断
        self.bot.agent.run.return_value = ("怎么能这样——就这样了‖行行行，我认", [])
        await ai_chat.ai_chat(self.ctx)
        sent = self.ctx.reply.await_args.args[0][0]["data"]["text"]
        self.assertEqual(sent, "怎么能这样，就这样了‖我认")

    async def test_addressed_answer_survives_new_arrivals(self):
        await self._arrivals_case(addressed=True, media=False, count=2, should_send=True)


class SafetyPacingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 旧路径：生成耗时抵扣、无反应延迟。新行为默认不抵扣，这里显式切回旧路径验证。
        self.bot = SimpleNamespace(config={"safety.enabled": False,
                                           "safety.max_delay": 6,
                                           "safety.subtract_elapsed": True,
                                           "safety.react_delay": [0, 0]},
                                   inbound={"s": 0})
        self.guard = SafetyGuard(self.bot)
        self.sleep = patch("pawbot.safety.asyncio.sleep", new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def test_first_delay_subtracts_generation_elapsed(self):
        self.guard._think_delay = lambda part: 5
        conn = SimpleNamespace(send_message=AsyncMock(return_value={"ok": 1}))
        await self.guard.send(conn, "s", [{"type": "text", "data": {"text": "x"}}],
                              group_id=3, elapsed=4)
        self.assertEqual(self.sleep.await_args_list[0].args[0], 1)

    async def test_remaining_parts_drop_after_two_new_messages(self):
        async def send(segments, **kw):
            self.bot.inbound["s"] = 2
            return {"ok": 1}
        conn = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        await self.guard.send(conn, "s", [{"type": "text", "data": {"text": "a‖b‖c"}}], group_id=3)
        self.assertEqual(conn.send_message.await_count, 1)


class ToolBudgetTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, responses, *, budget=35):
        config = {"tools.enabled": True, "tools.max_rounds": 8,
                  "tools.time_budget": budget, "chat.context_messages": 0,
                  "topic.enabled": False, "spice.allow_flirt": False}
        calls = [{"id": str(i), "function": {"name": "web_search", "arguments": '{"q":"x%d"}' % i}}
                 for i in range(4)]
        toolbox = SimpleNamespace(schemas=Mock(return_value=[{}]), execute=AsyncMock(return_value=("result", [])))
        llm = SimpleNamespace(chat_message=AsyncMock(side_effect=responses))
        bot = SimpleNamespace(config=config, toolbox=toolbox, llm=llm,
            persona=SimpleNamespace(build=Mock(return_value="persona")),
            store=SimpleNamespace(summary=Mock(return_value=""),
                                  turn_summary=Mock(return_value=""),
                                  window=Mock(return_value=[])),
            topic=SimpleNamespace(), mood=SimpleNamespace(), spice=SimpleNamespace(), stickers=SimpleNamespace(files=Mock(return_value=[])))
        agent = Agent(bot)
        agent._participation_hint = lambda ctx: ""
        agent._sense_hint = lambda ctx: ""
        agent._mood_hint = lambda ctx: ""
        agent._spice_hint = lambda ctx: ""
        agent._tool_hint = lambda on: ""
        ctx = SimpleNamespace(group_id=1, sender_name="x", session_id="s", is_group=False)
        return agent, bot, toolbox, ctx, calls

    async def test_budget_stops_tool_rounds(self):
        async def first(*args):
            import time
            time.sleep(0.02)
            return {"tool_calls": [{"id": "1", "function": {"name": "other", "arguments": "{}"}}]}
        agent, bot, toolbox, ctx, calls = self.make_agent([None, {"content": "done"}], budget=0.005)
        llm = bot.llm
        llm.chat_message.side_effect = [
            {"tool_calls": [{"id": "1", "function": {"name": "other", "arguments": "{}"}}]},
            {"content": "done"}]
        import time
        with patch("pawbot.agent.time.monotonic", side_effect=[0, 0, 0, 1]):
            result = await agent.run(ctx, "question", [])
        self.assertEqual(result[0], "done")
        self.assertEqual(toolbox.execute.await_count, 1)
        self.assertIsNone(bot.llm.chat_message.await_args_list[-1].args[1])

    async def test_web_search_stops_after_two_calls(self):
        seq = [
            {"tool_calls": [{"id": "1", "function": {"name": "web_search", "arguments": '{"query":"cat"}'}}]},
            {"tool_calls": [{"id": "2", "function": {"name": "web_search", "arguments": '{"query":"weather in tokyo today"}'}}]},
            {"tool_calls": [{"id": "3", "function": {"name": "web_search", "arguments": '{"query":"historical population census results for 2020"}'}}]},
            {"content": "done"},
        ]
        agent, bot, toolbox, ctx, _ = self.make_agent(seq)
        result = await agent.run(ctx, "question", [])
        self.assertEqual(result[0], "done")
        self.assertEqual(toolbox.execute.await_count, 2)
        self.assertIsNone(bot.llm.chat_message.await_args_list[-1].args[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class GenerationCreditTests(unittest.IsolatedAsyncioTestCase):
    """模型生成耗时要从打字延迟里扣：等得久就直接发，别再拖。"""

    def setUp(self):
        self.bot = SimpleNamespace(config={"safety.enabled": True,
                                           "safety.typing_cps": 10,
                                           "safety.min_delay": 0.6,
                                           "safety.max_delay": 4.0,
                                           # 本类专门覆盖旧的「抵扣生成耗时」路径
                                           "safety.subtract_elapsed": True,
                                           "safety.react_delay": [0, 0]},
                                   inbound={"s": 0})
        self.guard = SafetyGuard(self.bot)
        self.sleep = patch("pawbot.safety.asyncio.sleep",
                           new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    def conn(self):
        return SimpleNamespace(send_message=AsyncMock(return_value={"ok": 1}))

    async def test_slow_generation_skips_typing_delay(self):
        # 生成已经花了 6 秒，超过 max_delay=4：不再模拟打字，直接发
        await self.guard.send(self.conn(), "s", [{"type": "text", "data": {"text": "你好"}}],
                              group_id=1, elapsed=6.0)
        self.assertEqual(self.sleep.await_count, 0)

    async def test_medium_generation_subtracts_elapsed(self):
        # 打字按 4 字/10cps=0.4s，但生成已等 2 秒 → 剩下只睡 0.3 下限
        await self.guard.send(self.conn(), "s", [{"type": "text", "data": {"text": "你好"}}],
                              group_id=1, elapsed=2.0)
        self.assertEqual(self.sleep.await_count, 1)
        self.assertEqual(self.sleep.await_args.args[0], 0.3)

    async def test_fast_generation_still_typing_delay(self):
        # 生成 0.5 秒，打字 0.4s → 全额扣完还剩 0.3 下限
        await self.guard.send(self.conn(), "s", [{"type": "text", "data": {"text": "你好"}}],
                              group_id=1, elapsed=0.5)
        self.assertEqual(self.sleep.await_args.args[0], 0.3)

    async def test_multi_part_keeps_gaps_but_first_has_no_think(self):
        self.guard._gap_delay = lambda: 1.0
        await self.guard.send(self.conn(), "s",
                              [{"type": "text", "data": {"text": "甲‖乙"}}],
                              group_id=1, elapsed=10.0)
        delays = [c.args[0] for c in self.sleep.await_args_list]
        # 第一段不睡（生成已经等够了）；第二段才睡分条间隔 + 自己的打字时间
        self.assertEqual(len(delays), 2)
        self.assertEqual(delays[0], 1.0)
        self.assertLessEqual(delays[1], 2.0)

    async def test_no_elapsed_keeps_old_behaviour(self):
        await self.guard.send(self.conn(), "s", [{"type": "text", "data": {"text": "你好"}}],
                              group_id=1)
        self.assertEqual(self.sleep.await_count, 1)
        self.assertGreater(self.sleep.await_args.args[0], 0.3)
