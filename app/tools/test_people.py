"""群友档案：好感加减、衰减、语气与档案读写。全部离线、临时目录。"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.people import PersonBook


class Cfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class AffinityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = PersonBook(self.tmp.name, Cfg({"people.dir": os.path.join(self.tmp.name, "people")}))

    def test_new_person_starts_neutral(self):
        self.assertEqual(self.book.affinity(10001), 50.0)
        self.assertIn("一般", self.book.affinity_tone(10001))

    def test_praise_rises_insult_falls_faster(self):
        up = self.book.note_interaction(10001, "阿明", "你太可爱了，谢谢你！", addressed=True)
        self.assertGreater(up, 50)
        down = self.book.note_interaction(10001, "阿明", "你个傻X，滚", addressed=True)
        self.assertLess(down, up)
        self.assertLess(down, 50)          # 骂一句掉得比夸一句多

    def test_repeated_insults_drop_and_streak_compounds(self):
        first = self.book.note_interaction(10001, "阿明", "闭嘴", addressed=True)
        self.assertLess(first, 45)                     # 一句就掉得看得见
        for _ in range(3):
            last = self.book.note_interaction(10001, "阿明", "闭嘴，烦死了")
        self.assertLess(last, 20)                      # 连续嘴臭加成，掉得更快
        self.assertLess(last, first - 20)

    def test_ordinary_addressing_does_not_cancel_insults(self):
        for _ in range(5):
            self.book.note_interaction(10001, "阿明", "你好", addressed=True)
        warm = self.book.affinity(10001)
        self.assertLess(warm, 60)
        cold = self.book.note_interaction(10001, "阿明", "滚吧", addressed=True)
        self.assertLess(cold, warm - 4)

    def test_affectionate_teasing_does_not_lower_affinity(self):
        start = self.book.affinity(10001)
        warm = self.book.note_interaction(10001, "阿明", "笨蛋啦，抱抱", addressed=True)
        self.assertGreater(warm, start)
        # 光一句「笨蛋」不确定语气，至少不能当骂人扣分
        flat = self.book.note_interaction(10001, "阿明", "笨蛋", addressed=True)
        self.assertGreaterEqual(flat, warm - 0.1)
        # 夹着真骂人的词才照常扣
        bad = self.book.note_interaction(10001, "阿明", "笨蛋，滚吧", addressed=True)
        self.assertLess(bad, flat - 4)

    def test_ordinary_chat_warms_only_to_soft_cap(self):
        for _ in range(200):
            self.book.note_interaction(10001, "阿明", "今天吃什么")
        self.assertLessEqual(self.book.affinity(10001), 58.1)

    def test_values_are_clamped(self):
        self.assertEqual(self.book.bump_affinity(10001, "阿明", 999), 100.0)
        self.assertEqual(self.book.bump_affinity(10001, "阿明", -999), 0.0)

    def test_decay_returns_toward_neutral(self):
        self.book.bump_affinity(10001, "阿明", 40)          # 90
        path = self.book.path(10001)
        text = Path(path).read_text(encoding="utf-8")
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 14 * 86400))
        text = "\n".join(("- 好感更新：" + old) if line.startswith("- 好感更新") else line
                         for line in text.splitlines()) + "\n"
        Path(path).write_text(text, encoding="utf-8")
        self.book._cache.clear()
        self.assertAlmostEqual(self.book.affinity(10001), 70.0, delta=1.0)

    def test_affinity_is_hidden_from_prompt_card(self):
        self.book.set_section(10001, "阿明", "性格", "嘴硬但靠谱")
        self.book.bump_affinity(10001, "阿明", 30)
        card = self.book.card(10001)
        self.assertIn("嘴硬但靠谱", card)
        self.assertNotIn("好感", card)
        prompt = self.book.card_for_prompt(10001)
        self.assertNotIn("80", prompt)
        tone = self.book.affinity_tone(10001)
        self.assertNotRegex(tone, r"\d")

    def test_tone_labels_are_monotonic(self):
        for value, word in ((20, "不太想搭理"), (40, "保留"), (55, "一般"),
                            (70, "挺熟"), (90, "亲近")):
            with self.subTest(value=value):
                self.book.bump_affinity(10001, "阿明", 0)
                self.book._write_affinity(10001, "阿明", float(value))
                self.assertIn(word, self.book.affinity_tone(10001))

    def test_persistence_and_all_people_listing(self):
        self.book.set_section(10001, "阿明", "性格", "爱吐槽")
        self.book.bump_affinity(10001, "阿明", 20)
        again = PersonBook(self.tmp.name, Cfg({"people.dir": os.path.join(self.tmp.name, "people")}))
        self.assertAlmostEqual(again.affinity(10001), 70.0, delta=0.5)
        people = again.all_people()
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["user_id"], "10001")
        self.assertEqual(people[0]["sections"]["性格"], "爱吐槽")
        self.assertEqual(people[0]["affinity_label"], "挺熟")

    def test_owner_has_no_special_baseline(self):
        book = PersonBook(self.tmp.name, Cfg({"people.dir": self.book.dir,
                                             "account.owner_ids": [10001]}))
        self.assertEqual(book.affinity(10001), 50.0)
        self.assertEqual(book.affinity(10002), 50.0)

    def test_nagging_does_not_lower_affinity(self):
        """2026-10-24 改口径：她是很粘人、从帮上忙里获得成就感的角色，
        **被使唤不掉好感**（原来是一直催就扣 1.5~6，实测 12 句正常请求把好感打到 11.4）。"""
        start = self.book.affinity(10001)
        self.book.note_interaction(10001, "阿明", "给我搜张图", addressed=True)
        self.book.note_interaction(10001, "阿明", "快点", addressed=True)
        mid = self.book.affinity(10001)
        self.assertGreaterEqual(mid, start)
        for _ in range(3):
            last = self.book.note_interaction(10001, "阿明", "怎么还没好，快发图")
        self.assertGreaterEqual(last, mid)                 # 一直催也不掉
        self.assertNotIn("嫌烦", self.book.affinity_tone(10001))
        self.assertNotRegex(self.book.affinity_tone(10001), r"\d")

    def test_requests_do_not_drain_affinity(self):
        """要她干活不会把她越推越远 —— 这跟「嘴甜能缓和」是两件事。"""
        for _ in range(3):
            self.book.note_interaction(10001, "阿明", "再发一张")
        before = self.book.affinity(10001)
        after = self.book.note_interaction(10001, "阿明", "可爱的肥鱼再发一张嘛")
        self.assertGreaterEqual(after, before)

    def test_praise_spam_hits_daily_cap(self):
        for _ in range(50):
            self.book.note_interaction(10001, "阿明", "你好可爱，谢谢，喜欢你", addressed=True)
        self.assertLessEqual(self.book.affinity(10001), 60.5)

    def test_gains_shrink_near_the_top(self):
        self.book._write_affinity(10001, "阿明", 95.0)
        up = self.book.note_interaction(10001, "阿明", "谢谢你")
        self.assertLess(up - 95.0, 1.0)

    def test_spamming_addresses_gives_bonus_once(self):
        for _ in range(10):
            self.book.note_interaction(10001, "阿明", "在吗", addressed=True)
        self.assertLess(self.book.affinity(10001), 51.6)

    def test_neutral_phrases_not_counted_as_cold(self):
        for text in ("这个我还没用过", "没用到", "再来一局"):
            with self.subTest(text=text):
                self.assertGreaterEqual(self.book.note_interaction(10002, "阿亮", text), 50.0)

    def test_corrupt_file_is_tolerated(self):
        os.makedirs(os.path.join(self.tmp.name, "people"), exist_ok=True)
        Path(self.book.path(10002)).write_text("- 好感：不是数字\n", encoding="utf-8")
        self.assertEqual(self.book.affinity(10002), 50.0)




class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "people")

    def book(self, **extra):
        values = {"people.dir": self.dir}
        values.update(extra)
        return PersonBook(self.tmp.name, Cfg(values))

    def test_observe_triggers_at_threshold_then_cools_down(self):
        book = self.book(**{"people.update_every": 3, "people.update_cooldown": 3600})
        self.assertFalse(book.observe(1))
        self.assertFalse(book.observe(1))
        self.assertTrue(book.observe(1))
        book.mark_updated(1)
        self.assertFalse(book.observe(1))          # 冷却期没过

    async def test_update_profile_writes_personality_sections(self):
        class FakeLLM:
            async def chat(self, messages):
                return "性格：嘴硬但会照顾人\n说话方式：短句，爱反问\n在意：别被人当小孩"

        book = self.book()
        ok = await book.update_profile(FakeLLM(), 10001, "阿明",
                                       ["今天好累", "谁说我不会", "你先吃", "别管我", "不用了"])
        self.assertTrue(ok)
        people = book.all_people()
        self.assertEqual(people[0]["sections"]["性格"], "嘴硬但会照顾人")
        self.assertIn("说话方式", people[0]["sections"])
        self.assertNotIn("好感", people[0]["sections"])

    async def test_update_profile_needs_enough_samples(self):
        class Boom:
            async def chat(self, messages):
                raise AssertionError("样本不够时不该调模型")

        self.assertFalse(await self.book().update_profile(Boom(), 1, "阿明", ["只有一句"]))


if __name__ == "__main__":
    unittest.main()
