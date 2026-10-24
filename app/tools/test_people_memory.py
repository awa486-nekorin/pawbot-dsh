"""群友记忆修复的回归测试。

覆盖 2026-10-24 修的五个问题：
  1. 档案按行截断、标签行优先（原来按字符从尾部切，性格被挤掉、首行是半句）
  2. 提炼失败后退避（原来失败一次就每条消息烧一次模型）
  3. 样本按 uid 挑（原来按昵称，同名串味、改名失忆）
  4. 使唤不扣好感，反而加一点（她靠帮上忙获得成就感）
  5. 日增上限落盘（原来重启就清零，上限形同虚设）

    PYTHONIOENCODING=utf-8 py -m unittest tools.test_people_memory -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest

from pawbot.people import PersonBook, HELP_BONUS


class 假配置(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def 建库(目录, **cfg):
    return PersonBook(目录, 假配置({"people.dir": 目录, **cfg}))


class 档案截断测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.库 = 建库(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_长档案保留性格且不切半句(self):
        self.库.set_section("1", "小明", "性格", "爱抬杠，但心软")
        for i in range(40):
            self.库.remember("1", "小明", f"第{i}件琐事：今天聊了游戏{i}")
        卡 = self.库.card("1", max_chars=600)
        行们 = 卡.splitlines()
        self.assertTrue(行们)
        self.assertIn("性格", 卡, "标签行必须保住")
        # 每一行都必须是完整的一行（不能出现「6件琐事：」这种半句）
        for ln in 行们:
            self.assertTrue(ln.startswith("- "), f"出现半行了：{ln!r}")
            self.assertIn("：", ln)

    def test_给模型那份也不出现碎片(self):
        self.库.set_section("1", "小明", "说话方式", "短句多")
        for i in range(40):
            self.库.remember("1", "小明", f"琐事{i}：内容{i}")
        卡 = self.库.card_for_prompt("1", max_chars=400)
        self.assertTrue(卡)
        for ln in 卡.splitlines():
            self.assertTrue(ln.startswith("- "), f"碎行：{ln!r}")

    def test_短档案原样返回(self):
        self.库.set_section("1", "小明", "性格", "安静")
        卡 = self.库.card("1")
        self.assertIn("性格：安静", 卡)

    def test_名字不带标题括注(self):
        self.库.remember("1", "一碗麦西粉", "爱吃白饭")
        # 模拟标题带括注的旧档案
        with open(self.库.path("1"), "w", encoding="utf-8") as f:
            f.write("# 一碗麦西粉(你的主人)\n- 性格：爱问问题\n")
        self.assertEqual(self.库._name_line(self.库.path("1")), "一碗麦西粉")


class 好感方向测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_使唤不掉好感(self):
        self.库 = 建库(self.dir)
        干活 = ["帮我搜一下这个", "来点图", "快点", "再来一张", "给我发一下", "怎么还没好",
                "继续发", "快点啊", "再搜搜", "好了没", "发图", "整点好玩的"]
        for t in 干活:
            self.库.note_interaction("1", "主人", t)
        好 = self.库.affinity("1")
        self.assertGreaterEqual(好, 50.0, f"被使唤不该掉好感，实际 {好}")
        self.assertLessEqual(好, 100.0)

    def test_帮忙确实加了一点(self):
        库 = 建库(self.dir)
        库.note_interaction("1", "主人", "帮我搜一下这个")
        self.assertGreater(库.affinity("1"), 50.0, "被找去帮忙应该加好感")

    def test_同一串请求只加一次(self):
        库 = 建库(self.dir)
        for _ in range(8):
            库.note_interaction("1", "主人", "帮我看看这个")
        好 = 库.affinity("1")
        self.assertLess(好, 54.0, f"刷屏不该刷出好感（日上限 10），实际 {好}")

    def test_骂人仍然扣分(self):
        库 = 建库(self.dir)
        for _ in range(4):
            库.note_interaction("1", "某人", "傻逼")
        self.assertLess(库.affinity("1"), 50.0, "被骂还是要掉")

    def test_语气不再劝她嫌烦(self):
        库 = 建库(self.dir)
        库.note_interaction("1", "主人", "帮我搜一下")
        语气 = 库.affinity_tone("1")
        self.assertNotIn("嫌烦", 语气)
        self.assertNotIn("不用好声好气", 语气)


class 提炼退避测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_失败不会每条消息都重试(self):
        class 坏LLM:
            次 = 0

            async def chat(self, msgs):
                坏LLM.次 += 1
                return "我看不出来"        # 解析不出任何字段

        库 = 建库(self.dir)
        库.config["people.update_cooldown"] = 1800

        async def 跑():
            触发 = 0
            for i in range(60):
                if 库.observe("222"):
                    触发 += 1
                    await 库.update_profile(坏LLM(), "222", "阿强",
                                            [f"样本{i}"] * 10)
            return 触发

        触发 = asyncio.run(跑())
        self.assertLessEqual(触发, 3, f"60 条消息触发了 {触发} 次，退避没生效")

    def test_样本不足也算试过(self):
        库 = 建库(self.dir)
        库.config["people.update_cooldown"] = 1800
        asyncio.run(库.update_profile(object(), "333", "阿明", ["只有一句"]))
        self.assertIn("333", 库._last_attempt)


class 样本按uid测试(unittest.TestCase):
    def test_同名不同号不串味(self):
        rows = [
            {"role": "user", "who": "管理员", "uid": "111", "content": "甲的发言"},
            {"role": "user", "who": "管理员", "uid": "222", "content": "乙的发言"},
            {"role": "assistant", "who": "我", "uid": "", "content": "她自己的话"},
        ]
        甲的 = PersonBook.pick_samples(rows, "111", "管理员")
        self.assertEqual(甲的, ["甲的发言"])
        乙的 = PersonBook.pick_samples(rows, "222", "管理员")
        self.assertEqual(乙的, ["乙的发言"])

    def test_改名后仍取得到(self):
        rows = [{"role": "user", "who": "旧名字", "uid": "111", "content": "改名前说的话"}]
        self.assertEqual(PersonBook.pick_samples(rows, "111", "新名字"), ["改名前说的话"])

    def test_老记录没uid时按昵称兜底(self):
        rows = [{"role": "user", "who": "阿明", "content": "老记录"}]
        self.assertEqual(PersonBook.pick_samples(rows, "999", "阿明"), ["老记录"])

    def test_不把她说的话当样本(self):
        rows = [{"role": "assistant", "who": "我", "uid": "111", "content": "她自己说的"}]
        self.assertEqual(PersonBook.pick_samples(rows, "111", "我"), [])


class 日增落盘测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_重启后日上限仍然生效(self):
        甜 = ["谢谢", "你好可爱", "厉害", "抱抱", "辛苦啦"] * 9
        库 = 建库(self.dir, **{"people.affinity_daily_gain": 10})
        for t in 甜:
            库.note_interaction("1", "甜", t)
        第一次 = 库.affinity("1")
        self.assertLessEqual(第一次, 60.5, f"日上限没生效：{第一次}")

        # 模拟重启：新实例从同一目录起来
        库2 = 建库(self.dir, **{"people.affinity_daily_gain": 10})
        for t in 甜[:5]:
            库2.note_interaction("1", "甜", t)
        第二次 = 库2.affinity("1")
        self.assertLess(第二次, 第一次 + 5, f"重启把日上限绕过了：{第一次} → {第二次}")


if __name__ == "__main__":
    unittest.main()
