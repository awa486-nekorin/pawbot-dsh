"""稳定性回归：档案写入加锁/原子化、后台任务登记、心情视图。

全部离线、只用临时目录；不读项目的 data/。
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import tasks
from pawbot.emotion import Emotion
from pawbot.people import PersonBook
from pawbot.topic import Mood, TopicTracker, mood_label


class Cfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class AtomicPeopleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "people")
        self.book = PersonBook(self.tmp.name, Cfg({"people.dir": self.dir}))

    def test_concurrent_bumps_keep_file_parseable(self):
        """50 个线程同时给同一个人加分：文件不能损坏，也不能互相覆盖。"""
        errors = []

        def worker(i):
            try:
                self.book.bump_affinity(10001, "阿明", 0.5)
            except Exception as e:                  # pragma: no cover - 出错才走到
                errors.append(f"{i}: {type(e).__name__} {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        path = self.book.path(10001)
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertTrue(lines and lines[0].startswith("# "), text[:80])
        self.assertEqual(sum(1 for ln in lines if ln.strip().startswith("- 好感：")), 1)
        self.assertEqual(sum(1 for ln in lines if ln.strip().startswith("- 好感更新：")), 1)
        self.assertEqual(
            [fn for fn in os.listdir(self.dir) if fn.endswith(".tmp")], [])

        value = self.book.affinity(10001)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0)
        # 50 * 0.5 从 50 起步 = 75，一步都没丢
        self.assertAlmostEqual(value, 75.0, delta=0.01)

    def test_atomic_write_failure_leaves_original_intact(self):
        """os.replace 之前抛错：原档一个字节都不变，临时文件不留。"""
        self.book.bump_affinity(10001, "阿明", 10.0)
        path = self.book.path(10001)
        before = Path(path).read_text(encoding="utf-8")

        with mock.patch("os.replace", side_effect=OSError("模拟磁盘错误")):
            with self.assertRaises(OSError):
                self.book.bump_affinity(10001, "阿明", 10.0)

        self.assertEqual(Path(path).read_text(encoding="utf-8"), before)
        self.assertEqual(
            [fn for fn in os.listdir(self.dir) if fn.endswith(".tmp")], [])
        self.assertAlmostEqual(self.book.affinity(10001), 60.0, delta=0.01)

    def test_remember_and_section_writes_are_atomic(self):
        self.assertTrue(self.book.remember(10002, "阿亮", "爱熬夜"))
        self.assertTrue(self.book.set_section(10002, "阿亮", "性格", "话少"))
        # 重复记同一句不再写第二遍
        self.assertFalse(self.book.remember(10002, "阿亮", "爱熬夜"))
        text = Path(self.book.path(10002)).read_text(encoding="utf-8")
        self.assertEqual(text.count("- 爱熬夜"), 1)
        self.assertEqual(text.count("- 性格：话少"), 1)
        self.assertEqual(
            [fn for fn in os.listdir(self.dir) if fn.endswith(".tmp")], [])


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for task in tasks.pending():
            task.cancel()
        await asyncio.sleep(0)

    async def test_spawn_logs_exception_and_forgets_reference(self):
        async def boom():
            await asyncio.sleep(0)
            raise ValueError("炸了")

        with mock.patch("pawbot.tasks.log") as fake_log:
            task = tasks.spawn(boom(), name="boom")
            self.assertIn(task, tasks.pending())
            await asyncio.sleep(0.05)

        self.assertEqual(tasks.pending(), [])
        self.assertTrue(fake_log.warn.called)
        msg = fake_log.warn.call_args.args[0]
        self.assertIn("后台任务 boom 失败", msg)
        self.assertIn("ValueError", msg)

    async def test_cancel_does_not_log(self):
        async def sleepy():
            await asyncio.sleep(5)

        with mock.patch("pawbot.tasks.log") as fake_log:
            task = tasks.spawn(sleepy(), name="cancel_me")
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0.05)

        self.assertFalse(fake_log.warn.called)
        self.assertEqual(tasks.pending(), [])

    async def test_drain_waits_for_remaining_tasks(self):
        done = []

        async def slow():
            await asyncio.sleep(0.15)
            done.append("ok")

        tasks.spawn(slow(), name="slow")
        self.assertEqual(done, [])
        await tasks.drain(timeout=5)
        self.assertEqual(done, ["ok"])
        self.assertEqual(tasks.pending(), [])


class MoodViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_bound_mood_tracks_emotion(self):
        emo = Emotion(None, None)
        mood = Mood().bind_emotion(emo)
        self.assertAlmostEqual(mood.score, 50.0, places=6)

        emo.apply({"开心": 15, "兴奋": 15, "感动": 15, "得意": 15})
        high = mood.score
        self.assertGreater(high, 53.0)
        self.assertEqual(mood.label, mood_label(high))

        emo.apply({"开心": -15, "兴奋": -15, "感动": -15, "得意": -15})
        self.assertLess(mood.score, high)

        chill = Emotion(None, None)
        low_mood = Mood().bind_emotion(chill)
        chill.apply({"烦躁": 15, "低落": 15, "害羞": 15})
        self.assertLess(low_mood.score, 45.0)

    def test_bound_mood_is_read_only_and_does_not_save(self):
        path = os.path.join(self.tmp.name, "mood.json")
        emo = Emotion(None, None)
        mood = Mood(path).bind_emotion(emo)
        before = mood.score
        mood.on_mentioned()
        mood.on_chatted()
        mood.save()
        self.assertEqual(mood.score, before)
        self.assertFalse(os.path.exists(path))

    def test_unbound_mood_behaves_as_before(self):
        mood = Mood()
        mood.score = 55
        mood.on_mentioned()
        self.assertAlmostEqual(mood.score, 63.0)
        mood.score = 10
        self.assertIn("懒得", mood.hint())
        mood.score = 80
        self.assertEqual(mood.label, "心情不错")

        path = os.path.join(self.tmp.name, "mood.json")
        saved = Mood(path)
        saved.score = 42.0
        saved.save()
        again = Mood(path)
        self.assertAlmostEqual(again.score, 42.0, delta=0.01)

    def test_mood_load_tolerates_corrupt_file(self):
        path = os.path.join(self.tmp.name, "mood.json")
        Path(path).write_text("{不是 json", encoding="utf-8")
        mood = Mood(path)
        self.assertGreaterEqual(mood.score, 0.0)
        self.assertLessEqual(mood.score, 100.0)


class TopicSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_save_writes_valid_json_atomically(self):
        path = os.path.join(self.tmp.name, "topic.json")
        tracker = TopicTracker(5, path)
        tracker.seen("g1", "阿明", "今天数学考试好难")
        tracker.save(force=True)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertIn("g1", data)
        self.assertEqual(
            [fn for fn in os.listdir(self.tmp.name) if fn.endswith(".tmp")], [])

    def test_save_failure_is_logged_not_raised(self):
        path = os.path.join(self.tmp.name, "topic.json")
        tracker = TopicTracker(5, path)
        tracker.seen("g1", "阿明", "随便聊聊")
        with mock.patch("pawbot.topic.log") as fake_log:
            with mock.patch("builtins.open", side_effect=OSError("磁盘满了")):
                tracker.save(force=True)          # 不抛
        self.assertTrue(fake_log.debug.called)


if __name__ == "__main__":
    unittest.main()
