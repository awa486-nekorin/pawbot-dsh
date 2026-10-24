"""记忆 v2 的测试：预算闸门 / 实体库 / 迁移去重 / 拼装。

    PYTHONIOENCODING=utf-8 py -m unittest tools.test_mem -v

全部在临时目录里跑，不碰 data/ 里的真实数据。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

from pawbot.mem import budget, layers
from pawbot.mem.assemble import build, 取配置
from pawbot.mem.entities import EntityStore
from pawbot.mem import migrate
from pawbot.mem.write import merge_day, write_turn


# ---------------------------------------------------------------- 假对象
class 假配置(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class 假Store:
    def __init__(self, rows=None, turn="", summary=""):
        self.rows = list(rows or [])
        self._turn = turn
        self._summary = summary
        self._ts = 0.0

    def window(self, sid, limit=None):
        return self.rows[-limit:] if limit else list(self.rows)

    def turn_summary(self, sid):
        return self._turn

    def turn_summary_ts(self, sid):
        return self._ts

    def set_turn_summary(self, sid, text, upto):
        self._turn = text
        self._ts = float(upto)

    def summary(self, sid):
        return self._summary


class 假日记:
    def recent(self, n=2):
        return [("2026-10-22", "今天在群里聊了很久")]


class 假Bot:
    def __init__(self, base_dir, rows=None, turn="", cfg=None):
        self.base_dir = base_dir
        self.config = 假配置({"chat.context_messages": 100, "chat.context_chars": 5000,
                              "chat.time_decay_hours": 6, "memory.engine": "v2",
                              **(cfg or {})})
        self.store = 假Store(rows, turn)
        self.diary = 假日记()
        self.entities = EntityStore(base_dir, self.config)


class 假Ctx:
    def __init__(self, uid="1000000002", gid="200000001"):
        self.session_id = "group_" + str(gid)
        self.user_id = uid
        self.group_id = gid


def 造行(n=50, 间隔秒=60, 起=None):
    起 = 起 or (time.time() - n * 间隔秒)
    rows = []
    for i in range(n):
        rows.append({"role": "user" if i % 2 == 0 else "assistant",
                     "content": f"第{i}句话，随便聊点什么" + "啊" * (i % 5),
                     "who": "群友" if i % 2 == 0 else "我",
                     "uid": "1000000002" if i % 2 == 0 else "",
                     "ts": 起 + i * 间隔秒})
    return rows


# ---------------------------------------------------------------- 预算
class 预算测试(unittest.TestCase):
    def test_单层超上限被截(self):
        出, 日志 = budget.分配({"L0": 9000, "L1": 100, "L2": 100, "L3": 100})
        self.assertEqual(出["L0"], 5000)
        self.assertTrue(any(x["原因"] == "超过单层上限" for x in 日志))

    def test_不超总预算就不砍(self):
        出, 日志 = budget.分配({"L0": 3000, "L1": 600, "L2": 1600, "L3": 1500})
        self.assertEqual(sum(出.values()), 6700)
        self.assertEqual(日志, [])

    def test_超预算先砍最远的(self):
        # 7000 的请求，总预算 6000 → 应该从 L3 开始砍
        出, 日志 = budget.分配({"L0": 4000, "L1": 600, "L2": 1600, "L3": 1500}, 总预算=6000)
        self.assertLessEqual(sum(出.values()), 6000)
        self.assertEqual(日志[0]["层"], "L3")
        self.assertEqual(出["L0"], 4000)      # 最近的没被砍

    def test_下限保护(self):
        出, _ = budget.分配({"L0": 5000, "L1": 600, "L2": 1600, "L3": 1500}, 总预算=3000)
        self.assertGreaterEqual(出["L0"], 1500)
        self.assertGreaterEqual(出["L1"], 200)
        self.assertLessEqual(sum(出.values()), 5000 + 600 + 1600 + 1500)

    def test_摘要行带砍日志(self):
        出, 日志 = budget.分配({"L0": 5000, "L1": 600, "L2": 1600, "L3": 1500}, 总预算=6000)
        行 = budget.摘要行(出, 日志)
        self.assertIn("记忆预算", 行)
        self.assertIn("砍", 行)


# ---------------------------------------------------------------- 实体库
class 实体库测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = 假配置({})
        self.库 = EntityStore(self.dir, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_同key同kind只留最新(self):
        self.库.add("uid:1", "profile", text="旧性格", ts=100)
        self.库.add("uid:1", "profile", text="新性格", ts=200)
        有效 = self.库.load()
        self.assertEqual(len(有效), 1)
        self.assertEqual(有效[0]["text"], "新性格")

    def test_一模一样的内容不重复写(self):
        self.assertTrue(self.库.add("uid:1", "profile", text="性格A", ts=100))
        self.assertIsNone(self.库.add("uid:1", "profile", text="性格A", ts=200))
        self.assertEqual(len(self.库.load()), 1)

    def test_事件key唯一(self):
        a = self.库.add_event("今天吃了火锅", ts=1000)
        b = self.库.add_event("今天吃了火锅", ts=2000)
        self.assertIsNotNone(a)
        self.assertIsNone(b, "同一天同一段话不该写两次")

    def test_召回实体优先(self):
        self.库.add("uid:1000000002", "profile", text="对工具很敏感", ts=time.time())
        self.库.add("uid:9999999999", "profile", text="完全无关的人", ts=time.time())
        选中, stats = self.库.recall(query="随便一句话", uids=["1000000002"], budget=800)
        self.assertTrue(选中)
        self.assertIn("1000000002", 选中[0]["key"])
        self.assertEqual(选中[0]["_理由"], "本人")

    def test_整条进不切半句(self):
        长 = "很长的档案" * 60
        self.库.add("uid:1", "profile", text=长, ts=time.time())
        选中, _ = self.库.recall(query="档案", uids=["1"], budget=2000)
        self.assertEqual(len(选中), 1)
        self.assertIn(长, 选中[0]["_文本"])      # 整条都在，没被切

    def test_预算不够就跳过不切(self):
        for i in range(5):
            self.库.add(f"uid:{i}", "profile", text="内容" * 100, ts=time.time())
        选中, stats = self.库.recall(query="内容", uids=[], budget=300)
        self.assertLessEqual(stats["字数"], 300)
        self.assertGreater(stats["因预算跳过"], 0)

    def test_好感不单独成行(self):
        self.库.add("uid:1", "relation", value=63.3, ts=time.time())
        选中, _ = self.库.recall(query="", uids=["1"], budget=800)
        行 = [r for r in 选中 if r["kind"] == "relation"]
        for r in 行:
            self.assertEqual(r["_文本"], "")

    def test_压实只留最新(self):
        for i in range(10):
            self.库.add("uid:1", "profile", text=f"第{i}版", ts=100 + i)
        self.assertEqual(len(self.库._raw()), 10)
        压掉 = self.库.compact()
        self.assertEqual(压掉, 9)
        self.assertEqual(len(self.库.load()), 1)


# ---------------------------------------------------------------- 迁移
class 迁移测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "data", "longmem"))
        os.makedirs(os.path.join(self.dir, "data", "people"))
        os.makedirs(os.path.join(self.dir, "data", "memory"))
        now = time.time()
        # 三条 episode：第 2 条几乎是第 1 条的重复，第 3 条是 JSON 壳的日摘要
        ep = [
            {"ts": now - 7200, "session_id": "group_1", "importance": 4,
             "summary": "群里在聊白饭账，大肥鱼欠四碗，约定路过时补"},
            {"ts": now - 7000, "session_id": "group_1", "importance": 3,
             "summary": "群里在聊白饭账，大肥鱼欠四碗，约定路过时补"},     # 重复
            {"ts": now - 3600, "session_id": "group_1", "importance": 3,
             "daily_digest": True, "summary": json.dumps({"小结": "这一天主要在聊图和账"}, ensure_ascii=False)},
        ]
        with open(os.path.join(self.dir, "data", "longmem", "episodes.jsonl"), "w", encoding="utf-8") as f:
            for r in ep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(os.path.join(self.dir, "data", "people", "1000000002.md"), "w", encoding="utf-8") as f:
            f.write("# 一碗麦西粉\n- 好感：63.3\n- 好感更新：2026-10-23T11:35:58\n"
                    "- 名字：一碗麦西粉\n- 性格：对工具敏感\n- 说话方式：短问句追问\n- 在意：信息是否准确\n")
        with open(os.path.join(self.dir, "data", "memory", "facts.md"), "w", encoding="utf-8") as f:
            f.write("# 长期记忆\n- [2026-10-23] 约定：Claude 问起就说群里硬塞的\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_迁移去重脱壳并写入(self):
        报 = migrate.run(self.dir, 假配置({}))
        self.assertEqual(报["episodes"], 1, "重复的那条应该被丢掉")
        self.assertEqual(报["episodes_去重丢掉"], 1)
        self.assertEqual(报["day"], 1)
        self.assertEqual(报["profile"], 1)
        self.assertEqual(报["relation"], 1)
        self.assertEqual(报["fact"], 1)

        库 = EntityStore(self.dir, 假配置({}))
        有效 = 库.load()
        日 = [r for r in 有效 if r["kind"] == "day"]
        self.assertTrue(日)
        self.assertFalse(日[0]["text"].startswith("{"), "JSON 壳必须脱掉")
        self.assertIn("图和账", 日[0]["text"])
        关系 = [r for r in 有效 if r["kind"] == "relation"]
        self.assertAlmostEqual(关系[0]["value"], 63.3, places=1)

    def test_迁移幂等(self):
        migrate.run(self.dir, 假配置({}))
        行数1 = len(EntityStore(self.dir, 假配置({}))._raw())
        migrate.run(self.dir, 假配置({}))
        行数2 = len(EntityStore(self.dir, 假配置({}))._raw())
        self.assertEqual(行数1, 行数2, "跑两次不该多出行")


# ---------------------------------------------------------------- 拼装
class 拼装测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bot = 假Bot(self.dir, 造行(80), turn="刚才在聊火锅")
        self.bot.entities.add("uid:1000000002", "profile",
                              text="对工具敏感，短问句追问", ts=time.time(), importance=4)
        self.bot.entities.add("promise:" + "abc12345", "promise",
                              text="欠四碗白饭", ts=time.time() - 3600, importance=5)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_四层都出且顺序是远到近(self):
        块, 统计 = build(self.bot, 假Ctx(), "白饭账还欠多少")
        self.assertGreaterEqual(len(块), 3)
        头 = [b["content"][:12] for b in 块]
        # L3 在最前，聊天记录在靠后
        self.assertTrue(any("你还记得的" in b["content"] for b in 块))
        self.assertTrue(any("这个群最近的聊天记录" in b["content"] for b in 块))
        文本 = "\n".join(b["content"] for b in 块)
        self.assertLess(文本.index("你还记得的"), 文本.index("这个群最近的聊天记录"))

    def test_总字数不超预算(self):
        _, 统计 = build(self.bot, 假Ctx(), "随便聊聊")
        self.assertLessEqual(统计["总字数"], 8700 + 400, "总字数应该被闸门压住")

    def test_长期记忆真的被召回(self):
        块, _ = build(self.bot, 假Ctx(), "白饭账还欠多少")
        文本 = "\n".join(b["content"] for b in 块)
        self.assertIn("欠四碗白饭", 文本)

    def test_窗口按时间截(self):
        # 80 条、每条间隔 60 秒 = 80 分钟，全在 6 小时内
        _, 统计 = build(self.bot, 假Ctx(), "聊聊")
        self.assertGreater(统计["L0"]["lines"], 0)
        # 改成 10 分钟窗口 → 应该只剩十几条
        self.bot.config["memory.l0_hours"] = 0.2
        _, 统计2 = build(self.bot, 假Ctx(), "聊聊")
        self.assertLess(统计2["L0"]["lines"], 统计["L0"]["lines"])


# ---------------------------------------------------------------- 写入
class 写入测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bot = 假Bot(self.dir, 造行(10))
        self.库 = self.bot.entities

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _假判官(self, 结果):
        async def ask(bot, prompt, limit):
            return 结果
        return ask

    def test_只写增量且不合并历史(self):
        import asyncio
        rows = 造行(6)
        结果 = asyncio.run(write_turn(self.bot, "group_1", rows,
                                      ask=self._假判官({"summary": "这一轮在聊火锅", "importance": 4})))
        self.assertEqual(结果["summary"], "这一轮在聊火锅")
        # 提示词里**不该**出现「已有小结」
        self.assertNotIn("已有小结", 结果["summary"])

    def test_重要度高才进事件流(self):
        import asyncio
        asyncio.run(write_turn(self.bot, "group_1", 造行(4),
                               ask=self._假判官({"summary": "闲聊", "importance": 1})))
        self.assertEqual([r for r in self.库.load() if r["kind"] == "event"], [])

        asyncio.run(write_turn(self.bot, "group_1", 造行(4),
                               ask=self._假判官({"summary": "定了个约定", "importance": 4,
                                                "promises": ["周末一起看番"]})))
        kinds = [r["kind"] for r in self.库.load()]
        self.assertIn("event", kinds)
        self.assertIn("promise", kinds)

    def test_日摘要只合并不叠加(self):
        import asyncio
        now = time.time()
        for i in range(3):
            self.库.add_event(f"第{i}件事", ts=now - 86400 + i * 60, importance=4)
        n = asyncio.run(merge_day(self.bot, self.库, time.strftime("%Y-%m-%d", time.localtime(now - 86400)),
                                  ask=self._假判官({"summary": "这一天干了三件事"})))
        self.assertEqual(n, 3)
        days = [r for r in self.库.load() if r["kind"] == "day"]
        self.assertEqual(len(days), 1)
        # 再合一次不该多出条目
        asyncio.run(merge_day(self.bot, self.库, time.strftime("%Y-%m-%d", time.localtime(now - 86400)),
                              ask=self._假判官({"summary": "又来一次"})))
        self.assertEqual(len([r for r in self.库.load() if r["kind"] == "day"]), 1)


if __name__ == "__main__":
    unittest.main()
