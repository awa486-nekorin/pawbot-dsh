"""滚动对话小結：合并、截断、跳过条件、注入位置。全部离线。"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import turns
from pawbot.store import Store


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def make_bot(cfg=None):
    tmp = tempfile.mkdtemp()
    store = Store(os.path.join(tmp, "s.json"), max_messages=300)
    return SimpleNamespace(config=Cfg(cfg or {}), store=store,
                           llm=SimpleNamespace(chat=AsyncMock(return_value="主模型小结")))


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = make_bot({"chat.turn_summary": True})
        self.sid = "group_1"

    def seed(self, *pairs):
        import time
        for who, text, role in pairs:
            self.bot.store.append(self.sid, role, text, who=who, ts=time.time())

    async def test_merges_previous_summary_with_this_turn(self):
        self.seed(("阿明", "今天好累", "user"))
        self.bot.store.set_turn_summary(self.sid, "阿明在抱怨累。", 1.0)
        self.seed(("阿明", "帮我找首歌", "user"), ("我", "找吧，想听什么", "assistant"))
        mock = AsyncMock(return_value={"summary": "阿明先抱怨累，又要找歌，我答应了。"})
        with patch.object(turns.judge, "judge_json", mock):
            out = await turns.update(self.bot, self.sid)
        self.assertIn("找歌", out)
        self.assertEqual(self.bot.store.turn_summary(self.sid), out)
        # 提示词里确实带上了上一份小结
        self.assertIn("阿明在抱怨累", mock.await_args.args[1])

    async def test_cursor_moves_so_the_same_turn_is_not_summarized_twice(self):
        self.seed(("阿明", "你好", "user"))
        with patch.object(turns.judge, "judge_json",
                          AsyncMock(return_value={"summary": "打了个招呼。"})) as judge:
            await turns.update(self.bot, self.sid)
            await turns.update(self.bot, self.sid)
        self.assertEqual(judge.await_count, 1)

    async def test_skips_when_nothing_new(self):
        with patch.object(turns.judge, "judge_json", AsyncMock()) as judge:
            self.assertEqual(await turns.update(self.bot, self.sid), "")
        judge.assert_not_called()

    async def test_falls_back_to_main_llm(self):
        self.seed(("阿明", "你好", "user"))
        with patch.object(turns.judge, "judge_json", AsyncMock(return_value=None)):
            out = await turns.update(self.bot, self.sid)
        self.assertEqual(out, "主模型小结")
        self.bot.llm.chat.assert_awaited_once()

    async def test_judge_exception_falls_back(self):
        self.seed(("阿明", "你好", "user"))
        with patch.object(turns.judge, "judge_json", AsyncMock(side_effect=RuntimeError("x"))):
            out = await turns.update(self.bot, self.sid)
        self.assertEqual(out, "主模型小结")

    async def test_long_output_is_capped(self):
        self.seed(("阿明", "你好", "user"))
        long = "很长的内容" * 500
        with patch.object(turns.judge, "judge_json", AsyncMock(return_value={"summary": long})):
            out = await turns.update(self.bot, self.sid)
        self.assertLessEqual(len(out), 600)          # limit 300 × 2

    async def test_disabled_by_config(self):
        self.bot.config["chat.turn_summary"] = False
        self.seed(("阿明", "你好", "user"))
        with patch.object(turns.judge, "judge_json", AsyncMock()) as judge:
            self.assertEqual(await turns.update(self.bot, self.sid), "")
        judge.assert_not_called()

    async def test_prompt_forbids_it_from_appearing_in_chat(self):
        self.seed(("阿明", "你好", "user"))
        mock = AsyncMock(return_value={"summary": "打了招呼。"})
        with patch.object(turns.judge, "judge_json", mock):
            await turns.update(self.bot, self.sid)
        prompt = mock.await_args.args[1]
        self.assertIn("不会展示给任何人", prompt)
        self.assertIn("只输出 JSON", prompt)


class StoreTests(unittest.TestCase):
    def test_turn_summary_persists(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.json")
        store = Store(path)
        store.set_turn_summary("s", "小结内容", 1234.0)
        store.flush()
        again = Store(path)
        self.assertEqual(again.turn_summary("s"), "小结内容")
        self.assertEqual(again.turn_summary_ts("s"), 1234.0)

    def test_clear_drops_turn_summary(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "s.json"))
        store.set_turn_summary("s", "x", 1.0)
        store.clear("s")
        self.assertEqual(store.turn_summary("s"), "")


class PromptInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_summary_sits_at_the_end_before_user_message(self):
        from pawbot.agent import Agent
        import time

        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "s.json"))
        store.append("group_1", "user", "在吗", who="阿明")
        store.append("group_1", "assistant", "在", who="我")
        store.append("group_1", "user", "刚才说到哪了", who="阿明")
        store.set_turn_summary("group_1", "阿明问过在不在，我答了。", time.time())

        seen = {}

        async def chat_message(messages, tools=None):
            seen["messages"] = messages
            return {"content": "说你在呢"}

        bot = SimpleNamespace(
            config=Cfg({"tools.enabled": False, "topic.enabled": False,
                        "chat.context_messages": 300, "chat.context_chars": 12000}),
            store=store,
            persona=SimpleNamespace(build=Mock(return_value="persona")),
            toolbox=SimpleNamespace(schemas=Mock(return_value=[]), execute=AsyncMock()),
            llm=SimpleNamespace(chat_message=chat_message),
            stickers=SimpleNamespace(files=Mock(return_value=[])),
            topic=SimpleNamespace(), mood=SimpleNamespace(hint=lambda: ""),
        )
        agent = Agent(bot)
        agent._sense_hint = lambda ctx: ""
        agent._mood_hint = lambda ctx: ""
        agent._spice_hint = lambda ctx: ""
        agent._tool_hint = lambda on: ""
        ctx = SimpleNamespace(group_id=1, sender_name="阿明", session_id="group_1",
                              is_group=True, event=SimpleNamespace(plain="刚才说到哪了"),
                              state={}, matched_command=None)
        await agent.run(ctx, "刚才说到哪了", store.history("group_1")[:-1])

        msgs = seen["messages"]
        idx = [i for i, m in enumerate(msgs)
               if m["role"] == "system" and "你记得的刚才这几轮" in m["content"]]
        self.assertEqual(len(idx), 1)
        self.assertLess(idx[0], len(msgs) - 1)                  # 不是最后一条
        self.assertEqual(msgs[-1]["role"], "user")              # 最后是当前这句话
        self.assertIn("阿明问过在不在", msgs[idx[0]]["content"])


if __name__ == "__main__":
    unittest.main()
