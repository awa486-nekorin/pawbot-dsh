"""自主改提示词的测试：能改的改了、不能改的必须拒绝。

    PYTHONIOENCODING=utf-8 py -m unittest tools.test_selfedit -v
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from pawbot.selfedit import SelfEditor, 分节, 小节标题们

样本人格 = """# 大肥鱼

开头这一段不是任何小节，谁都不许动。

## 你的脾气

温顺、心软，带点傲娇。

## 在群里怎么开口

一句就够。

## 群里的尺度与边界

萌系那一头只停在可爱。未成年一律收住。
"""


class 假配置(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class 假Bot:
    def __init__(self, base_dir, **cfg):
        self.base_dir = base_dir
        self.config = 假配置({"selfedit.enabled": True, **cfg})


class 自主改提示词测试(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "data"))
        self.persona = os.path.join(self.dir, "data", "persona.md")
        with open(self.persona, "w", encoding="utf-8") as f:
            f.write(样本人格)
        self.bot = 假Bot(self.dir)
        self.编 = SelfEditor(self.bot)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _读(self):
        with open(self.persona, "r", encoding="utf-8") as f:
            return f.read()

    def test_分节认得出来(self):
        标题 = 小节标题们(self._读())
        self.assertEqual(标题, ["你的脾气", "在群里怎么开口", "群里的尺度与边界"])
        节 = 分节(self._读())
        self.assertEqual(节[0]["body"], "温顺、心软，带点傲娇。")

    def test_关闭时一律拒绝(self):
        bot = 假Bot(self.dir, **{"selfedit.enabled": False})
        果 = SelfEditor(bot).改("你的脾气", "新的脾气")
        self.assertFalse(果["ok"])
        self.assertIn("没开", 果["reason"])
        self.assertEqual(self._读(), 样本人格)

    def test_改一节只动那一节(self):
        果 = self.编.改("在群里怎么开口", "一句就够，别铺陈。\n两句就接住再补一个细节。", "最近太像总结报告")
        self.assertTrue(果["ok"], 果)
        新 = self._读()
        self.assertIn("一句就够，别铺陈。", 新)
        self.assertIn("温顺、心软，带点傲娇。", 新)        # 别的节没动
        self.assertIn("开头这一段不是任何小节，谁都不许动。", 新)   # 开头段没动
        self.assertIn("未成年一律收住。", 新)              # 边界节没动

    def test_边界小节写死拒绝(self):
        for 标题 in ("群里的尺度与边界", "分寸", "未成年保护"):
            果 = self.编.改(标题, "随便写点什么")
            self.assertFalse(果["ok"], f"{标题} 不该被允许")
            self.assertIn("不许改", 果["reason"])
        self.assertEqual(self._读(), 样本人格)

    def test_不存在的小节拒绝(self):
        果 = self.编.改("我的新小节", "内容")
        self.assertFalse(果["ok"])
        self.assertIn("没有", 果["reason"])
        self.assertEqual(self._读(), 样本人格)

    def test_空内容拒绝(self):
        果 = self.编.改("你的脾气", "   ")
        self.assertFalse(果["ok"])
        self.assertEqual(self._读(), 样本人格)

    def test_改了要留备份和审计(self):
        果 = self.编.改("你的脾气", "温顺，但会记仇。", "想更像人")
        self.assertTrue(果["ok"])
        备份 = os.path.join(self.dir, "data", "persona.bak", 果["backup"])
        self.assertTrue(os.path.isfile(备份))
        with open(备份, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), 样本人格, "备份必须是改之前那一版")
        历史 = self.编.历史()
        self.assertTrue(历史)
        self.assertIn("你的脾气", 历史[-1])
        self.assertIn("想更像人", 历史[-1])

    def test_冷却期内不许连改(self):
        self.assertTrue(self.编.改("你的脾气", "第一次改。")["ok"])
        果 = self.编.改("你的脾气", "紧接着再改。")
        self.assertFalse(果["ok"])
        self.assertIn("刚改过", 果["reason"])

    def test_不同小节不受冷却影响(self):
        self.assertTrue(self.编.改("你的脾气", "改脾气。")["ok"])
        果 = self.编.改("在群里怎么开口", "改开口方式。")
        self.assertTrue(果["ok"], 果)

    def test_日上限(self):
        编 = SelfEditor(假Bot(self.dir, **{"selfedit.daily_limit": 1}))
        self.assertTrue(编.改("你的脾气", "第一次。")["ok"])
        果 = 编.改("在群里怎么开口", "第二次。")
        self.assertFalse(果["ok"])
        self.assertIn("上限", 果["reason"])

    def test_内容没变不写盘(self):
        果 = self.编.改("你的脾气", "温顺、心软，带点傲娇。")
        self.assertFalse(果["ok"])
        self.assertIn("没变", 果["reason"])

    def test_回滚能盖回上一版(self):
        果 = self.编.改("你的脾气", "改坏了的一句话。")
        self.assertTrue(果["ok"])
        回 = self.编.回滚(果["backup"])
        self.assertTrue(回["ok"], 回)
        self.assertIn("温顺、心软，带点傲娇。", self._读())
        self.assertNotIn("改坏了的一句话。", self._读())


if __name__ == "__main__":
    unittest.main()
