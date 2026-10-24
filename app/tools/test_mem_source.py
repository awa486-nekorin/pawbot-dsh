"""记忆 v2 的来源标注（C：私聊记忆带进群聊时不能变成「群里有人说的」）。

覆盖：
  1. 私聊 session 的小结提示词用私聊措辞、群聊用群聊措辞；
  2. merge_day 产出的 day 条目保留 sources；
  3. _render_one 对 private / group / merge 三种 source 分别标对；
  4. assemble 产出的 L2/L3 header 含来源提示；
  5. 判官材料 _渲染 带时间前缀，且截断不吞时间前缀。

    cd app
    $env:PYTHONPATH='.'; $env:PYTHONIOENCODING='utf-8'; py tools/test_mem_source.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot.mem.assemble import build, _头
from pawbot.mem.entities import EntityStore
from pawbot.mem.write import merge_day, write_turn, _渲染


# ---------------------------------------------------------------- 假对象
class 假配置(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class 假Store:
    def __init__(self, rows=None, turn=""):
        self.rows = list(rows or [])
        self._turn = turn

    def window(self, sid, limit=None):
        return self.rows[-limit:] if limit else list(self.rows)

    def turn_summary(self, sid):
        return self._turn

    def set_turn_summary(self, sid, text, upto):
        self._turn = text


class 假日记:
    def recent(self, n=2):
        return []


class 假Bot:
    def __init__(self, base_dir, rows=None, turn="", cfg=None):
        self.base_dir = base_dir
        self.config = 假配置({
            "chat.context_messages": 100, "chat.context_chars": 5000,
            "chat.time_decay_hours": 6, "memory.engine": "v2",
            "memory.l1_chars": 600, "memory.event_min_importance": 3,
            "memory.auto_day_merge": True,
            **(cfg or {}),
        })
        self.store = 假Store(rows, turn)
        self.diary = 假日记()
        self.entities = EntityStore(base_dir, self.config)
        self.memes = None


class 假Ctx:
    def __init__(self, uid="1000000002", gid="200000001"):
        self.session_id = "group_" + str(gid)
        self.user_id = uid
        self.group_id = gid


# ---------------------------------------------------------------- 提示词分岔
class 提示词分岔测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bot = 假Bot(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _造行(self):
        now = time.time()
        return [
            {"role": "user", "content": "我喜欢蓝色", "who": "阿明", "uid": "1000000002", "ts": now - 60},
            {"role": "assistant", "content": "蓝色好看", "who": "我", "uid": "", "ts": now},
        ]

    async def _跑(self, sid):
        captured = {}

        async def ask(bot, prompt, limit):
            captured["prompt"] = prompt
            return {"summary": "（私聊）他私下说喜欢蓝色", "importance": 3}

        await write_turn(self.bot, sid, self._造行(), ask=ask)
        return captured["prompt"]

    def test_私聊session用私聊措辞(self):
        p = asyncio.run(self._跑("private_1000000002"))
        self.assertIn("私聊", p)
        self.assertIn("私下跟我说", p)
        self.assertNotIn("群里刚刚发生", p)

    def test_群聊session用群聊措辞(self):
        p = asyncio.run(self._跑("group_200000001"))
        self.assertIn("群里刚刚发生", p)
        self.assertNotIn("私下跟我说", p)


# ---------------------------------------------------------------- 判官材料带时间
class 渲染测试(unittest.TestCase):
    def test_带时间前缀且截断不吞前缀(self):
        t1 = time.mktime(time.strptime("2026-10-23 20:00:00", "%Y-%m-%d %H:%M:%S"))
        t2 = time.mktime(time.strptime("2026-10-24 21:35:00", "%Y-%m-%d %H:%M:%S"))
        rows = [
            {"role": "user", "content": "昨天的话", "who": "阿明", "uid": "1234", "ts": t1},
            {"role": "assistant", "content": "今天的话", "who": "我", "uid": "", "ts": t2},
        ]
        出 = _渲染(rows)
        # 跨日带月日，同日只写 HH:MM
        self.assertIn("[10-23 20:00] 阿明(尾号1234)：昨天的话", 出)
        self.assertIn("[21:35] 我（大肥鱼）：今天的话", 出)
        # 截断按整行从尾部取：剩下的行前缀必须完整
        短 = _渲染(rows, max_chars=25)
        self.assertIn("[21:35] 我（大肥鱼）", 短)
        self.assertNotIn("昨天的话", 短)


# ---------------------------------------------------------------- merge_day 保留 sources
class 日摘要来源测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bot = 假Bot(self.dir)
        self.库 = self.bot.entities

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_day条目保留sources且提示词分组(self):
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
        self.库.add_event("他私下说喜欢蓝色", ts=now - 86400, importance=4,
                          source="private_1000000002")
        self.库.add_event("群里在聊火锅", ts=now - 86400 + 60, importance=4,
                          source="group_200000001")

        captured = {}

        async def ask(bot, prompt, limit):
            captured["prompt"] = prompt
            return {"summary": "白天群里聊火锅，夜里私聊说喜欢蓝色"}

        n = asyncio.run(merge_day(self.bot, self.库, day, ask=ask))
        self.assertEqual(n, 2)
        days = [r for r in self.库.load() if r["kind"] == "day"]
        self.assertEqual(len(days), 1)
        self.assertIn("sources", days[0])
        self.assertIn("private_1000000002", days[0]["sources"])
        self.assertIn("group_200000001", days[0]["sources"])
        # 提示词按来源分组，别把私聊并成群聊
        self.assertIn("私聊", captured["prompt"])
        self.assertIn("群里", captured["prompt"])


# ---------------------------------------------------------------- _render_one 三种来源
class 渲染来源测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.库 = EntityStore(self.dir, 假配置({}))
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_群聊来源(self):
        出 = EntityStore._render_one(
            {"key": "ev:d:1", "kind": "event", "text": "群里聊火锅", "ts": self.now,
             "importance": 3, "source": "group_200000001"}, self.now)
        self.assertIn("（群裡）", 出)

    def test_私聊来源无名(self):
        出 = EntityStore._render_one(
            {"key": "ev:d:2", "kind": "event", "text": "他说喜欢蓝色", "ts": self.now,
             "importance": 3, "source": "private_1000000002"}, self.now)
        self.assertIn("（私聊里说的）", 出)

    def test_私聊来源带名字(self):
        出 = EntityStore._render_one(
            {"key": "ev:d:3", "kind": "event", "text": "他说喜欢蓝色", "ts": self.now,
             "importance": 3, "source": "private_1000000002"}, self.now,
            名字查找=lambda uid: "一碗麦西粉")
        self.assertIn("一碗麦西粉", 出)
        self.assertIn("私聊", 出)

    def test_merge来源按sources(self):
        出 = EntityStore._render_one(
            {"key": "day:2026-10-23", "kind": "day", "text": "这一天……", "ts": self.now,
             "importance": 3, "source": "merge",
             "sources": ["private_1000000002", "group_200000001"]}, self.now)
        self.assertIn("（私聊 + 群裡）", 出)

    def test_merge来源只有私聊(self):
        出 = EntityStore._render_one(
            {"key": "day:2026-10-23", "kind": "day", "text": "这一天……", "ts": self.now,
             "importance": 3, "source": "merge",
             "sources": ["private_1000000002"]}, self.now)
        self.assertIn("（私聊）", 出)
        self.assertNotIn("+", 出)


# ---------------------------------------------------------------- assemble header
class 拼装头测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        now = time.time()
        rows = [
            {"role": "user", "content": "在吗", "who": "阿明", "uid": "1000000002", "ts": now - 120},
            {"role": "assistant", "content": "在的", "who": "我", "uid": "", "ts": now},
        ]
        self.bot = 假Bot(self.dir, rows=rows, turn="")
        # 让 L2/L3 真的有内容：一条日摘要 + 一条本人档案
        self.bot.entities.add("uid:1000000002", "profile",
                              text="对工具敏感", ts=now, importance=4)
        self.bot.entities.add("day:" + time.strftime("%Y-%m-%d", time.localtime(now)),
                              "day", text="这一天聊了火锅", ts=now, importance=3,
                              source="merge", sources=["group_200000001"])

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_L2L3头含来源提示(self):
        self.assertIn("私聊", _头["L2"])
        self.assertIn("群裡", _头["L2"])
        self.assertIn("私聊", _头["L3"])
        self.assertIn("群裡", _头["L3"])

    def test_build产出的头含来源提示(self):
        块, _ = build(self.bot, 假Ctx(), "对工具敏感")
        文本 = "\n".join(b["content"] for b in 块)
        # L0 的 header（A 的 L0_HEADER 或内联回退）要讲清楚「我（大肥鱼）」是自己发的
        self.assertTrue(any("我（大肥鱼）" in b["content"] for b in 块))
        # L2/L3 的 header 要讲「私聊里的事别说成群里有人说」
        self.assertIn("私聊里的事不要说成", 文本)


if __name__ == "__main__":
    unittest.main()
