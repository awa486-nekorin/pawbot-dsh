"""愛好與插件節流測試；websearch 全部打樁，不碰網路。"""

import asyncio
import importlib.util
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

from pawbot import tasks
from pawbot.hobbies import Hobbies

ROOT = Path(__file__).resolve().parents[1]


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def search_results(*rows):
    return {"provider": "test", "results": [
        {"title": title, "url": "https://example.com", "snippet": snippet}
        for title, snippet in rows]}


def load_plugin():
    path = ROOT / "plugins" / "hobbies.py"
    spec = importlib.util.spec_from_file_location("pawbot_plugin_hobbies_undertest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HobbiesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Cfg({"hobbies.enabled": True,
                           "hobbies.check_every_hours": 12,
                           "hobbies.max_items": 6})
        self.book = Hobbies(self.tmp.name, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_created_on_first_run(self):
        path = os.path.join(self.tmp.name, "data", "hobbies.json")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data["items"]
        self.assertEqual(len(items), 3)
        kinds = [item["kind"] for item in items]
        self.assertEqual(kinds.count("anime"), 2)
        self.assertEqual(kinds.count("game"), 1)
        for item in items:
            self.assertTrue(item["name"])
            self.assertTrue(item["keywords"])
            self.assertEqual(item["last_checked"], 0)
            self.assertEqual(item["last_news"], "")
        again = Hobbies(self.tmp.name, self.config)
        self.assertEqual([item["name"] for item in again.items],
                         [item["name"] for item in items])

    async def test_check_summarizes_three_results_and_dedupes(self):
        item = self.book.items[0]
        payload = search_results(
            ("很長" * 120, "很長" * 120),
            ("標題二", "摘要二"),
            ("標題三", "摘要三"),
            ("標題四", "摘要四"),
        )
        bot = SimpleNamespace(config=self.config)
        with patch("pawbot.hobbies.websearch.search", Mock(return_value=payload)) as search:
            fresh = await self.book.check(bot, item)
        self.assertTrue(fresh)
        self.assertLessEqual(len(fresh), 300)
        self.assertIn("標題二", fresh)
        self.assertNotIn("標題四", fresh)
        self.assertEqual(search.call_args.args[0], f"{item['name']} 最新 剧情/更新")
        self.assertGreater(item["last_checked"], 0)
        reloaded = Hobbies(self.tmp.name, self.config)
        self.assertEqual(reloaded.items[0]["last_news"], fresh)
        # 同樣的結果再查一次：相似度 1.0 > 0.7，視為沒有新進展
        with patch("pawbot.hobbies.websearch.search", Mock(return_value=payload)):
            again = await self.book.check(bot, item)
        self.assertEqual(again, "")

    async def test_check_returns_news_when_content_changed(self):
        item = self.book.items[0]
        item["last_news"] = "舊聞：完全不同的內容"
        bot = SimpleNamespace(config=self.config)
        with patch("pawbot.hobbies.websearch.search",
                   Mock(return_value=search_results(("新消息", "官方放出新預告")))):
            fresh = await self.book.check(bot, item)
        self.assertIn("新消息", fresh)

    async def test_check_failure_still_marks_checked(self):
        item = self.book.items[0]
        bot = SimpleNamespace(config=self.config)
        with patch("pawbot.hobbies.websearch.search",
                   Mock(return_value={"provider": "test", "results": [], "error": "boom"})):
            self.assertEqual(await self.book.check(bot, item), "")
        self.assertGreater(item["last_checked"], 0)

    async def test_due_items_respect_interval(self):
        now = time.time()
        self.book.items[0]["last_checked"] = now - 13 * 3600
        self.book.items[1]["last_checked"] = now - 3600
        self.book.items[2]["last_checked"] = 0
        due = self.book.due_items(now)
        self.assertEqual([item["name"] for item in due],
                         [self.book.items[2]["name"], self.book.items[0]["name"]])

        slow = Hobbies(self.tmp.name, Cfg({**self.config,
                                           "hobbies.check_every_hours": 24}))
        slow.items[0]["last_checked"] = now - 13 * 3600
        slow.items[1]["last_checked"] = now - 3600
        slow.items[2]["last_checked"] = 0
        self.assertEqual([item["name"] for item in slow.due_items(now)],
                         [slow.items[2]["name"]])

        self.book.enabled = False
        self.assertEqual(self.book.due_items(now), [])


class HobbiesPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        rest = tasks.pending()
        for task in rest:
            task.cancel()
        if rest:
            await asyncio.gather(*rest, return_exceptions=True)

    async def test_tick_does_not_block_on_diary(self):
        module = load_plugin()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = {"n": 0}

        async def slow_diary(bot):
            calls["n"] += 1
            started.set()
            await release.wait()

        diary = SimpleNamespace(enabled=True, maybe_write_yesterday=slow_diary)
        book = SimpleNamespace(enabled=True, due_items=Mock(return_value=[]))
        bot = SimpleNamespace(diary=diary, hobbies=book)
        ctx = SimpleNamespace(bot=bot)

        await asyncio.wait_for(module.diary_and_hobbies(ctx), timeout=0.5)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        for _ in range(50):
            if book.due_items.call_count:
                break
            await asyncio.sleep(0.01)
        # 第二次 tick 在 10 分鐘節流內，不該再排一次
        await asyncio.wait_for(module.diary_and_hobbies(ctx), timeout=0.5)
        await asyncio.sleep(0.02)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(book.due_items.call_count, 1)
        release.set()
        await asyncio.sleep(0.05)

    async def test_tick_collects_hobby_news_into_pending(self):
        module = load_plugin()
        item = {"name": "鳴潮", "kind": "game"}
        book = SimpleNamespace(enabled=True, due_items=Mock(return_value=[item]),
                               check=AsyncMock(return_value="3.2 版本前瞻已公佈"))
        bot = SimpleNamespace(diary=SimpleNamespace(enabled=False), hobbies=book)

        await asyncio.wait_for(
            module.diary_and_hobbies(SimpleNamespace(bot=bot)), timeout=0.5)
        for _ in range(50):
            if getattr(bot, "pending_hobby_news", None):
                break
            await asyncio.sleep(0.01)
        pending = bot.pending_hobby_news
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["name"], "鳴潮")
        self.assertEqual(pending[0]["kind"], "game")
        self.assertEqual(pending[0]["text"], "3.2 版本前瞻已公佈")
        self.assertIn("ts", pending[0])
        book.check.assert_awaited_once_with(bot, item)

    async def test_tick_skips_when_hobbies_disabled(self):
        module = load_plugin()
        book = SimpleNamespace(enabled=False, due_items=Mock(return_value=[]))
        bot = SimpleNamespace(diary=SimpleNamespace(enabled=False), hobbies=book)
        await module.diary_and_hobbies(SimpleNamespace(bot=bot))
        await asyncio.sleep(0.02)
        book.due_items.assert_not_called()


if __name__ == "__main__":
    unittest.main()
