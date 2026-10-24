"""日記與 agent 注入測試；全程離線，判官全部打樁。"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.diary import Diary


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def ts_on(day: str, hour: int = 12) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour).timestamp()


def episode(day: str, sid: str = "group_100", summary: str = "群裡聊了芙莉蓮",
            hour: int = 12) -> dict:
    return {"ts": ts_on(day, hour), "session_id": sid, "participants": ["1"],
            "summary": summary, "keywords": ["芙莉蓮"], "importance": 2}


def fake_bot(config, episodes=None, mood_label="平平"):
    return SimpleNamespace(
        config=config,
        longmem=SimpleNamespace(episodes=list(episodes or [])),
        mood=SimpleNamespace(label=mood_label),
    )


class DiaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Cfg({"diary.enabled": True, "diary.keep_days": 60,
                           "diary.max_chars": 500})
        self.diary = Diary(self.tmp.name, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_write_for_creates_day_file_with_header(self):
        bot = fake_bot(self.config, [episode("2026-10-21")])
        reply = "今天群裡一直在聊芙莉蓮，我也跟著看了兩集，感覺時間過得很快。有人說週末要一起打遊戲，我其實有點期待。晚上大家又扯到吃的，我又餓了。"
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": reply})) as judge:
            body = await self.diary.write_for(bot, "2026-10-21")
        self.assertEqual(body, reply)
        path = self.diary.path_for("2026-10-21")
        self.assertEqual(os.path.normpath(path),
                         os.path.normpath(os.path.join(self.tmp.name, "data", "diary",
                                                       "2026-10-21.md")))
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        first = text.splitlines()[0]
        self.assertTrue(first.startswith("# 2026-10-21 周"))
        self.assertIn("（心情：平平）", first)
        self.assertIn(reply, text)
        self.assertNotIn("（機械合併）", text)
        judge.assert_awaited_once()

    async def test_maybe_write_yesterday_only_writes_yesterday(self):
        now = time.time()
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        bot = fake_bot(self.config, [episode(yesterday, summary="昨天聊了膽大黨"),
                                    episode(today, summary="今天聊了鳴潮")])
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": "昨天群裡在聊膽大黨，我在旁邊看得很開心。"})) as judge:
            await self.diary.maybe_write_yesterday(bot, now=now)
        self.assertTrue(self.diary.exists(yesterday))
        self.assertFalse(self.diary.exists(today))
        judge.assert_awaited_once()
        # 已經寫過就不再重寫、也不再問判官
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": "不該被呼叫"})) as judge2:
            await self.diary.maybe_write_yesterday(bot, now=now)
        judge2.assert_not_awaited()

    async def test_no_episodes_writes_nothing(self):
        bot = fake_bot(self.config, [])
        with patch("pawbot.diary.judge.judge_json", AsyncMock()) as judge:
            self.assertEqual(await self.diary.write_for(bot, "2026-10-21"), "")
            self.assertEqual(await self.diary.maybe_write_yesterday(bot), "")
        judge.assert_not_awaited()
        self.assertFalse(os.path.exists(self.diary.dir))

    async def test_judge_failure_uses_mechanical_fallback(self):
        bot = fake_bot(self.config, [
            episode("2026-10-21", "group_100", "大家在聊芙莉蓮的新一集", hour=10),
            episode("2026-10-21", "group_200", "有人提到週末要去爬山", hour=15),
        ])
        with patch("pawbot.diary.judge.judge_json", AsyncMock(return_value=None)):
            await self.diary.write_for(bot, "2026-10-21")
        with open(self.diary.path_for("2026-10-21"), "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("今天在群 100 聊了", text)
        self.assertIn("有人提到", text)
        self.assertTrue(text.rstrip().endswith("（機械合併）"))

    async def test_private_episodes_are_ignored(self):
        bot = fake_bot(self.config, [
            episode("2026-10-21", "private_1", "私聊內容不該進日記"),
            episode("2026-10-21", "group_100", "群裡聊了鳴潮"),
        ])
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": "今天群裡在聊鳴潮，我聽得挺起勁。"})) as judge:
            await self.diary.write_for(bot, "2026-10-21")
        prompt = judge.await_args_list[0].args[1]
        self.assertIn("群裡聊了鳴潮", prompt)
        self.assertNotIn("私聊內容不該進日記", prompt)

    async def test_at_most_thirty_episodes_in_prompt(self):
        rows = [episode("2026-10-21", "group_100", f"第{i}段閒聊", hour=1 + i % 20)
                for i in range(35)]
        bot = fake_bot(self.config, rows)
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": "今天群裡聊了好多事，我記不太清。"})) as judge:
            await self.diary.write_for(bot, "2026-10-21")
        prompt = judge.await_args_list[0].args[1]
        self.assertEqual(prompt.count("-（群100）"), 30)

    async def test_recent_reads_newest_first_and_skips_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        older = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        bot = fake_bot(self.config, [])
        self.diary._write(bot, older, "前天的日記")
        self.diary._write(bot, yesterday, "昨天的日記")
        self.diary._write(bot, today, "今天的日記")
        self.assertEqual(self.diary.recent(2),
                         [(yesterday, "昨天的日記"), (older, "前天的日記")])
        self.assertEqual(self.diary.recent(1), [(yesterday, "昨天的日記")])
        self.assertEqual(self.diary.recent(0), [])

    async def test_prune_removes_files_outside_keep_days(self):
        old_day = (datetime.now() - timedelta(days=61)).strftime("%Y-%m-%d")
        fresh_day = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        bot = fake_bot(self.config, [])
        self.diary._write(bot, old_day, "很舊的日記")
        self.diary._write(bot, fresh_day, "還算新的日記")
        self.assertEqual(self.diary.prune(), 1)
        self.assertFalse(self.diary.exists(old_day))
        self.assertTrue(self.diary.exists(fresh_day))


class AgentDiaryInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_diary_injected_before_longmem_and_user(self):
        from pawbot.agent import Agent
        from pawbot.store import Store

        tmp = tempfile.TemporaryDirectory()
        try:
            store = Store(os.path.join(tmp.name, "sessions.json"))
            store.append("group_1", "user", "在聊什麼", who="阿明", uid="100")
            store.set_turn_summary("group_1", "阿明剛問在聊什麼。", time.time())
            seen = {}

            async def chat_message(messages, tools=None):
                seen["messages"] = messages
                return {"content": "隨便聊聊"}

            diary = SimpleNamespace(recent=Mock(return_value=[
                ("2026-10-20", "昨天群裡聊了芙莉蓮。"),
                ("2026-10-19", "前天群裡在聊鳴潮。"),
            ]))
            longmem = SimpleNamespace(format_recall=Mock(return_value=(
                "## 你想起来的（内部记忆）\n- 喜歡海邊（在 QQ 群 2）")))
            bot = SimpleNamespace(
                config=Cfg({"tools.enabled": False, "topic.enabled": False,
                            "chat.context_messages": 300, "chat.context_chars": 12000,
                            "longmem.top_k": 6}),
                store=store, longmem=longmem, diary=diary,
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
                                  event=SimpleNamespace(plain="在聊什麼"),
                                  state={}, matched_command=None)
            await agent.run(ctx, "在聊什麼", [])
            messages = seen["messages"]
            diary_idx = next(i for i, msg in enumerate(messages)
                             if "你前两天写的日记" in msg["content"])
            long_idx = next(i for i, msg in enumerate(messages)
                            if "你想起来的（内部记忆）" in msg["content"])
            self.assertLess(diary_idx, long_idx)
            self.assertEqual(messages[diary_idx]["role"], "system")
            self.assertIn("2026-10-20", messages[diary_idx]["content"])
            self.assertIn("2026-10-19", messages[diary_idx]["content"])
            self.assertLess(long_idx, len(messages) - 1)
            self.assertEqual(messages[-1]["role"], "user")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
