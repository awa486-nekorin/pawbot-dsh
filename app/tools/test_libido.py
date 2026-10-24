"""性欲值：加减、回落、标签与提示。全部离线、临时目录。"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import libido as lb


class Cfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class LibidoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "libido.json")

    def make(self, **cfg):
        return lb.Libido(self.path, Cfg({"libido.half_life_min": 180, **cfg}))

    def test_flirt_raises_and_plain_chat_does_not(self):
        x = self.make()
        base = x.value
        self.assertEqual(x.note("今天天气不错", spice=2), base)
        up = x.note("想你了，抱抱，亲亲", spice=2, addressed=True)
        self.assertGreater(up, base + 10)

    def test_spice_zero_never_changes(self):
        x = self.make()
        self.assertEqual(x.note("想你了，抱抱", spice=0), x.value)

    def test_dirty_talk_down_and_cap(self):
        x = self.make()
        x.bump(60)
        down = x.note("别闹，说正经的，作业还没写", spice=2)
        self.assertLess(down, 80)
        self.assertLessEqual(x.bump(999), 100)
        self.assertGreaterEqual(x.bump(-999), 0)

    def test_decay_returns_to_base(self):
        x = self.make()
        x.bump(50)                      # 70
        x.updated = time.time() - 180 * 60
        x.decay()
        self.assertAlmostEqual(x.value, 45.0, delta=1.0)   # 70 → 20 + 50*0.5

    def test_labels_and_hint_thresholds(self):
        x = self.make()
        x.value = 24
        self.assertEqual(x.label(), "平常")
        self.assertEqual(x.hint(2), "")
        x.value = 40
        self.assertEqual(x.label(), "心痒")
        self.assertIn("心痒", x.hint(2))
        x.value = 60
        self.assertIn("被撩到", x.label())
        self.assertIn("撩回去", x.hint(2))
        x.value = 85
        self.assertIn("上头", x.label())
        self.assertIn("撒娇", x.hint(2))

    def test_persistence(self):
        x = self.make()
        x.bump(30)
        x.save()
        again = lb.Libido(self.path, Cfg({}))
        self.assertAlmostEqual(again.value, 50.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
