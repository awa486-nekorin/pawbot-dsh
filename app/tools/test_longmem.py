"""分層長期記憶與延遲存儲測試；全程離線並打樁模型。"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.longmem import LongMemory
from pawbot.store import Store


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def fake_bot(config=None):
    return SimpleNamespace(config=Cfg(config or {}),
                           llm=SimpleNamespace(chat=AsyncMock(return_value="壓縮摘要")))


class LongMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Cfg({"longmem.dir": "memory", "longmem.enabled": True,
                           "longmem.daily_merge_threshold": 12,
                           "longmem.keep_days": 30, "longmem.max_episodes": 5000,
                           "longmem.half_life_days": 7, "longmem.max_chars": 900})
        self.bot = fake_bot(self.config)
        self.longmem = LongMemory(self.tmp.name, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_episode_write_and_reload(self):
        now = time.time()
        await self.longmem.add_episode(self.bot, ts=now, session_id="group_100",
                                       participants=["123"], summary="喜欢旅行和海边",
                                       keywords=["旅行", "海边"], importance=4)
        loaded = LongMemory(self.tmp.name, self.config)
        self.assertEqual(len(loaded.episodes), 1)
        self.assertEqual(loaded.episodes[0]["participants"], ["123"])
        self.assertEqual(loaded.episodes[0]["importance"], 4)

    async def test_recall_ranks_person_recency_and_importance(self):
        now = time.time()
        self.longmem.episodes = [
            {"ts": now, "session_id": "group_2", "participants": ["123"],
             "summary": "喜欢旅行", "keywords": ["旅行"], "importance": 2},
            {"ts": now, "session_id": "group_3", "participants": ["999"],
             "summary": "喜欢旅行", "keywords": ["旅行"], "importance": 2},
            {"ts": now - 14 * 86400, "session_id": "group_4", "participants": ["123"],
             "summary": "喜欢旅行海边", "keywords": ["旅行"], "importance": 2},
            {"ts": now, "session_id": "group_5", "participants": ["888"],
             "summary": "旅行与徒步", "keywords": ["旅行"], "importance": 5},
        ]
        ctx = SimpleNamespace(user_id=123, session_id="private_123")
        entries = self.longmem.recall_for(ctx, "旅行", k=6)
        self.assertGreater(entries[0]["score"], entries[1]["score"])
        self.assertEqual(entries[0]["source"], "在 QQ 群 2")
        recent = self.longmem._episode_score(self.longmem.episodes[0], {"旅行"}, now,
                                             7, "0", "")
        older = self.longmem._episode_score(self.longmem.episodes[2], {"旅行"}, now,
                                            7, "0", "")
        important = self.longmem._episode_score(self.longmem.episodes[3], {"旅行"}, now,
                                                7, "0", "")
        self.assertGreater(recent, older)
        self.assertGreater(important, recent)

    async def test_cross_session_recall_labels_source(self):
        await self.longmem.add_episode(self.bot, ts=time.time(), session_id="group_777",
                                       participants=["123"], summary="阿明喜欢海边旅行",
                                       keywords=["旅行"], importance=3)
        ctx = SimpleNamespace(user_id=123, session_id="private_123")
        formatted = self.longmem.format_recall(ctx, "旅行")
        self.assertIn("在 QQ 群 777", formatted)
        self.assertIn("别把别的群的私事说出来", formatted)

    async def test_daily_merge_and_capacity_limit(self):
        cfg = Cfg({**self.config, "longmem.daily_merge_threshold": 2,
                   "longmem.max_episodes": 20})
        memory = LongMemory(self.tmp.name, cfg)
        with patch("pawbot.longmem.judge.judge_json",
                   AsyncMock(return_value={"summary": "这天聊过旅行与海边"})) as judge:
            for i in range(3):
                await memory.add_episode(self.bot, ts=time.time() + i, session_id="group_1",
                                         participants=["9"], summary=f"讨论旅行第{i}次",
                                         keywords=["旅行"], importance=2)
        self.assertEqual(len(memory.episodes), 2)
        self.assertTrue(any(row.get("daily_digest") for row in memory.episodes))
        judge.assert_awaited()

        cap_cfg = Cfg({**self.config, "longmem.daily_merge_threshold": 100,
                       "longmem.max_episodes": 2})
        capped = LongMemory(self.tmp.name, cap_cfg)
        for i in range(4):
            await capped.add_episode(self.bot, ts=time.time() + i, session_id=f"group_{i}",
                                     participants=[], summary=f"唯一事实{i}",
                                     keywords=[f"item{i}"], importance=2)
        self.assertEqual(len(capped.episodes), 2)
        self.assertEqual(len(LongMemory(self.tmp.name, cap_cfg).episodes), 2)

    async def test_agent_injects_longmem_before_turn_summary_and_user(self):
        from pawbot.agent import Agent

        tmp = tempfile.TemporaryDirectory()
        try:
            store = Store(os.path.join(tmp.name, "sessions.json"))
            store.append("group_1", "user", "旅行怎么样", who="阿明", uid="100")
            store.set_turn_summary("group_1", "阿明聊过旅行。", time.time())
            seen = {}

            async def chat_message(messages, tools=None):
                seen["messages"] = messages
                return {"content": "还不错"}

            longmem = SimpleNamespace(format_recall=Mock(return_value=(
                "## 你想起来的（内部记忆）\n- 喜欢海边（在 QQ 群 2）")))
            bot = SimpleNamespace(
                config=Cfg({"tools.enabled": False, "topic.enabled": False,
                            "chat.context_messages": 300, "chat.context_chars": 12000,
                            "longmem.top_k": 6}),
                store=store, longmem=longmem,
                persona=SimpleNamespace(build=Mock(return_value="persona")),
                toolbox=SimpleNamespace(schemas=Mock(return_value=[]), execute=AsyncMock()),
                llm=SimpleNamespace(chat_message=chat_message),
                stickers=SimpleNamespace(files=Mock(return_value=[])),
                topic=SimpleNamespace(), mood=SimpleNamespace(hint=lambda: ""),
            )
            agent = Agent(bot)
            agent._sense_hint = lambda ctx: ""
            agent._life_hint = lambda ctx: ""
            agent._mood_hint = lambda ctx: ""
            agent._spice_hint = lambda ctx: ""
            agent._tool_hint = lambda on: ""
            ctx = SimpleNamespace(group_id=1, sender_name="阿明", session_id="group_1",
                                  user_id=100, is_group=True,
                                  event=SimpleNamespace(plain="旅行怎么样"),
                                  state={}, matched_command=None)
            await agent.run(ctx, "旅行怎么样", [])
            messages = seen["messages"]
            long_idx = next(i for i, msg in enumerate(messages)
                            if "你想起来的（内部记忆）" in msg["content"])
            turn_idx = next(i for i, msg in enumerate(messages)
                            if "你记得的刚才这几轮" in msg["content"])
            self.assertLess(long_idx, turn_idx)
            self.assertLess(turn_idx, len(messages) - 1)
            self.assertEqual(messages[-1]["role"], "user")
        finally:
            tmp.cleanup()


class StoreTests(unittest.TestCase):
    def test_delayed_flush_and_archive_overflow_keeps_rough_summary(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "sessions.json")
            store = Store(path, max_messages=1)
            store.append("group_1", "user", "unique-entry-000", who="阿明")
            self.assertFalse(os.path.exists(path))
            with patch("pawbot.store.log.warn") as warning:
                for i in range(1, 100):
                    store.append("group_1", "user", f"unique-entry-{i:03d}", who="阿明")
            self.assertEqual(store.archive_size("group_1"), 80)
            self.assertIn("未壓縮的較早內容，已粗略合併", store.summary("group_1"))
            self.assertIn("unique-entry-000", store.summary("group_1"))
            warning.assert_called()
            self.assertTrue(store.flush())
            self.assertTrue(os.path.exists(path))
            self.assertFalse(store.dirty)
        finally:
            tmp.cleanup()

    def test_archive_overflow_also_compacts_on_restore(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = Store(os.path.join(tmp.name, "sessions.json"), max_messages=1)
            for i in range(82):
                store.append("group_1", "user", f"msg{i}", who="甲")
            rows = store.take_archive("group_1")
            self.assertLessEqual(len(rows), 80)
            store.restore_archive("group_1", [{"role": "user", "content": f"extra{i}", "who": "乙"}
                                               for i in range(100)])
            self.assertEqual(store.archive_size("group_1"), 80)
            self.assertIn("未壓縮的較早內容，已粗略合併", store.summary("group_1"))
        finally:
            tmp.cleanup()

    def test_bot_stop_forces_store_flush(self):
        from pawbot.core import Bot

        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "sessions.json")
            store = Store(path)
            store.append("group_1", "user", "未落盘")
            bot = Bot.__new__(Bot)
            bot.store = store
            bot._store_flush_task = None
            bot.proactive = SimpleNamespace(stop=AsyncMock())
            bot.mood = SimpleNamespace(save=Mock())
            bot.topic = SimpleNamespace(save=Mock())
            bot.server = SimpleNamespace(stop=AsyncMock())
            asyncio.run(bot.stop())
            self.assertTrue(os.path.exists(path))
            self.assertEqual(Store(path).history("group_1")[0]["content"], "未落盘")
        finally:
            tmp.cleanup()

    def test_compression_is_serialized_per_session(self):
        from pawbot.core import Bot

        async def run():
            bot = Bot.__new__(Bot)
            bot._compress_locks = {}
            rows = [{"role": "user", "content": "旧内容", "who": "阿明"}]

            class FakeStore:
                data = {"group_1": {}}
                available = True
                calls = 0

                def needs_compress(self, sid):
                    return self.available

                def take_archive(self, sid):
                    self.available = False
                    return rows

                def summary(self, sid):
                    return ""

                def turn_summary(self, sid):
                    return "最近小结"

                def set_summary(self, sid, text):
                    self.result = text

                def restore_archive(self, sid, rows):
                    self.available = True

            store = FakeStore()
            bot.store = store
            entered = asyncio.Event()
            release = asyncio.Event()
            prompts = []

            async def compress(messages):
                store.calls += 1
                prompts.extend(messages)
                entered.set()
                await release.wait()
                return "壓縮後摘要"

            bot.llm = SimpleNamespace(chat=compress)
            task1 = asyncio.create_task(bot._maybe_compress("group_1"))
            await entered.wait()
            task2 = asyncio.create_task(bot._maybe_compress("group_1"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(task1, task2)
            self.assertEqual(store.calls, 1)
            self.assertIn("最近小结", prompts[0]["content"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
