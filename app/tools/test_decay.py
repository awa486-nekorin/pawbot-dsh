"""渐忘（按龄降分辨率）的测试。

核心断言只有一条：**任何年龄都不会渲染成空。**
其余都是围绕这条的边界检查。
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot.mem import decay
from pawbot.mem.entities import EntityStore


class 取档(unittest.TestCase):
    def test_四档边界(self):
        self.assertEqual(decay.取档(0), "清晰")
        self.assertEqual(decay.取档(7), "清晰")
        self.assertEqual(decay.取档(7.1), "略糊")
        self.assertEqual(decay.取档(30), "略糊")
        self.assertEqual(decay.取档(30.1), "骨架")
        self.assertEqual(decay.取档(180), "骨架")
        self.assertEqual(decay.取档(180.1), "影影绰绰")
        self.assertEqual(decay.取档(3650), "影影绰绰")

    def test_重要度把阈值拉长三倍(self):
        # 普通 8 天已经掉到「略糊」，重要（imp4）还在「清晰」
        self.assertEqual(decay.取档(8, 3), "略糊")
        self.assertEqual(decay.取档(8, 4), "清晰")
        self.assertEqual(decay.取档(21, 4), "清晰")
        self.assertEqual(decay.取档(22, 4), "略糊")
        # 90 天：普通已经「骨架」，重要还在「略糊」
        self.assertEqual(decay.取档(90, 3), "骨架")
        self.assertEqual(decay.取档(90, 4), "略糊")

    def test_负数年龄当零(self):
        self.assertEqual(decay.取档(-5), "清晰")


class 渲染(unittest.TestCase):
    条 = {
        "text": "群里仍以图片闲聊为主，炸金美墨(3313)发蓝发大蝴蝶结抱吉他的糊图，"
                "大肥鱼觉得呆得可爱，并延续对其黑丝和腰臀的调侃。一碗麦西粉问的卡片打不开。",
        "gist": "炸金美墨发了张糊图，大肥鱼觉得呆得可爱",
        "points": ["一碗麦西粉问的卡片打不开"],
        "importance": 2,
    }

    def test_清晰给全文(self):
        self.assertEqual(decay.渲染(self.条, 3), self.条["text"])

    def test_略糊是骨架加一个要点(self):
        出 = decay.渲染(self.条, 15)
        self.assertIn("炸金美墨发了张糊图", 出)
        self.assertIn("卡片打不开", 出)
        self.assertNotIn("黑丝", 出)          # 次要细节掉了

    def test_骨架只剩一句(self):
        出 = decay.渲染(self.条, 60)
        self.assertEqual(出, "炸金美墨发了张糊图，大肥鱼觉得呆得可爱")

    def test_影影绰绰再掉一层(self):
        出 = decay.渲染(self.条, 400)
        self.assertTrue(出.startswith("（过去挺久了"))
        self.assertIn("炸金美墨发了张糊图", 出)
        self.assertNotIn("呆得可爱", 出)      # 连评价都掉了，只剩「发生过什么」

    def test_没有骨架也不为空_退成截首句(self):
        无 = {"text": self.条["text"], "importance": 2}
        出 = decay.渲染(无, 400)
        self.assertTrue(出.strip())
        self.assertIn("群里仍以图片闲聊为主", 出)   # 截的是第一句，没猜主干

    def test_压实过只剩骨架的也能渲染(self):
        只骨 = {"gist": "炸金美墨发了张糊图", "importance": 2, "压实": True}
        self.assertEqual(decay.渲染(只骨, 1), "炸金美墨发了张糊图")
        self.assertEqual(decay.渲染(只骨, 400), "（过去挺久了，细节记不清）炸金美墨发了张糊图")

    def test_空记录给空(self):
        self.assertEqual(decay.渲染({}, 400), "")

    def test_任何年龄都不为空(self):
        """最重要的一条：从刚发生到十年后，一条都不许渲染成空。"""
        样本们 = [
            self.条,
            {"text": "约定：输者请吃白饭", "importance": 4},
            {"text": "她说她怕黑", "gist": "她说她怕黑", "importance": 5},
            {"text": "a", "importance": 1},
            {"text": "很长的一段" * 100, "importance": 3},
        ]
        for 条 in 样本们:
            for 龄 in (0, 1, 7, 7.5, 30, 31, 90, 180, 181, 365, 366, 1000, 3650):
                出 = decay.渲染(条, 龄)
                self.assertTrue(出.strip(),
                                f"龄={龄} 条={条} 渲染成了空")


class 时间措辞(unittest.TestCase):
    def test_越久越含糊(self):
        self.assertIn("天前", decay.时间措辞(3))
        self.assertEqual(decay.时间措辞(15), "那阵子")
        self.assertEqual(decay.时间措辞(60), "几个月前")
        self.assertEqual(decay.时间措辞(200), "")      # 最老那档不报时间
        self.assertEqual(decay.时间措辞(400), "")

    def test_刚发生给精确小时(self):
        self.assertEqual(decay.时间措辞(0.01), "刚才")
        self.assertIn("小时", decay.时间措辞(0.3))


class 抹细节(unittest.TestCase):
    def test_时间换成那阵子(self):
        self.assertEqual(decay.抹时间("昨天说要给我看图"), "那阵子说要给我看图")
        self.assertEqual(decay.抹时间("09-17 那件事"), "那阵子 那件事")
        self.assertEqual(decay.抹时间("2026-09-17 那件事"), "那阵子 那件事")

    def test_括注去掉(self):
        self.assertEqual(decay.抹细节("炸金美墨(3313)发了图"), "炸金美墨发了图")

    def test_程度副词去掉(self):
        self.assertEqual(decay.抹细节("非常可爱"), "可爱")


class 压实不丢记忆(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "data", "memory"), exist_ok=True)
        self.库 = EntityStore(self.dir)
        now = time.time()
        # 20 条：前 10 条有骨架，后 10 条没骨架
        for i in range(20):
            行 = {"key": f"ev:d:{i}", "kind": "event", "ts": now - i * 86400 * 5,
                  "importance": 2, "text": f"第{i}件事的完整经过，写得很长很长" * 3}
            if i < 10:
                行["gist"] = f"第{i}件事的骨架"
            with open(self.库.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(行, ensure_ascii=False) + "\n")
        self.库._cache = None
        self.库._cache_mtime = -1.0

    def test_压实后条数不减(self):
        前 = len(self.库.load())
        self.库.compact(keep_events=5)
        后 = self.库.load()
        self.assertEqual(len(后), 前, "压实不许减少记忆条数")

    def test_有骨架的只清text_骨架还在(self):
        self.库.compact(keep_events=5)
        行们 = self.库.load()
        有骨 = [r for r in 行们 if str(r.get("gist") or "").startswith("第")]
        self.assertTrue(有骨)
        被清 = [r for r in 有骨 if not str(r.get("text") or "").strip()]
        self.assertTrue(被清, "超过 keep_events 且有骨架的，text 应该被清掉")
        for r in 被清:
            self.assertTrue(r.get("gist"))

    def test_没骨架的一条都不许丢(self):
        self.库.compact(keep_events=5)
        行们 = self.库.load()
        无骨 = [r for r in 行们 if not str(r.get("gist") or "").strip()]
        self.assertEqual(len(无骨), 10, "没骨架的必须原样保留，不许删")
        for r in 无骨:
            self.assertTrue(str(r.get("text") or "").strip())

    def test_压实后每条都还能渲染(self):
        self.库.compact(keep_events=5)
        now = time.time()
        for r in self.库.load():
            出 = self.库._render_one(r, now)
            self.assertTrue(出.strip(), f"压实后这条渲染成空了：{r}")


class 需要补骨架(unittest.TestCase):
    def test_刚发生的不补(self):
        self.assertFalse(decay.需要补骨架({"text": "x"}, 3))

    def test_要降档了才补(self):
        self.assertTrue(decay.需要补骨架({"text": "x"}, 60))

    def test_已有骨架的不补(self):
        self.assertFalse(decay.需要补骨架({"text": "x", "gist": "y"}, 60))

    def test_没正文的不补(self):
        self.assertFalse(decay.需要补骨架({}, 60))


class 提示词(unittest.TestCase):
    def test_占位符被替换(self):
        出 = decay.骨架提示("原文在这里")
        self.assertIn("原文在这里", 出)
        self.assertNotIn("{正文}", 出)

    def test_要求不许编(self):
        出 = decay.骨架提示("x")
        self.assertIn("不许编", 出)
        self.assertIn("JSON", 出)


if __name__ == "__main__":
    unittest.main()
