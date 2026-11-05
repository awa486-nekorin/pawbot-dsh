"""日記按需檢索 / 歸檔 / 跨平台素材測試。

全部使用 tempfile 真實存取；只有判官與生命狀態來源用假對象，
檢索與持久化本身不打樁。

    py -3.13 -m unittest tools.test_diary_recall -q
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.diary import Diary
from pawbot.mem import layers


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def ts_on(day: str, hour: int = 12) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour).timestamp()


def day_before(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


class FakeLifeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls: list[tuple] = []

    def recent_experiences(self, limit=10, *, start=None, end=None):
        self.calls.append((limit, start, end))
        return [dict(row) for row in self.rows]


class DiaryRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Cfg({"diary.enabled": True, "diary.keep_days": 60,
                           "diary.max_chars": 2000})
        self.diary = Diary(self.tmp.name, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    async def write(self, bot, day: str, body: str) -> str:
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": body})):
            return await self.diary.write_for(bot, day)

    async def test_search_returns_snippet_and_read_paginates(self):
        day = "2026-10-21"
        body = ("今天在群裡聊到潮汐，大家都說漲潮時海邊很好看。"
                "我又想起上次沒看完的那部紀錄片，於是記下了幾個想查的問題。"
                "晚上有人問我週末要不要一起去看海，我說再看看，其實有點想去。"
                "後來又聊到吃的，話題就歪掉了。") * 4
        bot = SimpleNamespace(config=self.config, mood=SimpleNamespace(label="平平"),
                              longmem=SimpleNamespace(episodes=[]),
                              life_store=FakeLifeStore([
                                  {"id": "exp-sea", "ts": ts_on(day, 9),
                                   "platform": "local", "summary": "聊到潮汐"},
                              ]))
        await self.write(bot, day, body)

        hits = self.diary.search("潮汐")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(set(hit), {"id", "day", "snippet", "experience_ids"})
        self.assertEqual(hit["id"], day)
        self.assertEqual(hit["day"], day)
        self.assertLessEqual(len(hit["snippet"]), 161)
        self.assertNotEqual(hit["snippet"], body)
        self.assertIn("潮汐", hit["snippet"])

        first = self.diary.read(hit["id"], offset=0, limit=40)
        self.assertTrue(first["ok"])
        self.assertEqual(first["day"], day)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_offset"], 40)
        self.assertEqual(len(first["text"]), 40)
        self.assertEqual(set(first), {"ok", "text", "next_offset", "truncated",
                                      "day", "experience_ids"})

        second = self.diary.read(hit["id"], offset=first["next_offset"], limit=40)
        self.assertTrue(second["ok"])
        self.assertEqual(second["text"], self.diary.read(hit["id"])["text"][40:80])

        whole = self.diary.read(hit["id"], limit=100000)
        self.assertFalse(whole["truncated"])
        self.assertIsNone(whole["next_offset"])
        self.assertIn("潮汐", whole["text"])

        missing = self.diary.read("1999-01-01")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["text"], "")

    async def test_unrelated_query_does_not_hit(self):
        day = "2026-10-20"
        bot = SimpleNamespace(config=self.config, longmem=SimpleNamespace(episodes=[]),
                              life_store=FakeLifeStore([
                                  {"id": "exp-anime", "ts": ts_on(day, 9),
                                   "platform": "local", "summary": "聊了芙莉蓮"},
                              ]))
        await self.write(bot, day, "今天群裡一直在聊芙莉蓮的新一集，我跟著看了兩集。")
        self.assertEqual(self.diary.search("量子力學"), [])
        self.assertEqual(self.diary.search("完全不存在的主題"), [])
        self.assertEqual(self.diary.search("芙莉蓮")[0]["day"], day)

    async def test_filters_by_date_and_people(self):
        first = "2026-10-18"
        second = "2026-10-19"
        life = FakeLifeStore([
            {"id": "exp-a", "ts": ts_on(first, 10), "platform": "local",
             "summary": "和阿明聊了潮汐的路線", "people": ["阿明"],
             "topics": ["潮汐"]},
            {"id": "exp-b", "ts": ts_on(second, 10), "platform": "local",
             "summary": "和小紅聊了潮汐的紀錄片", "people": ["小紅"],
             "topics": ["潮汐"]},
        ])
        bot = SimpleNamespace(config=self.config, life_store=life,
                              longmem=SimpleNamespace(episodes=[]))
        await self.write(bot, first, "今天和阿明聊了潮汐的路線。")
        await self.write(bot, second, "今天和小紅聊了潮汐的紀錄片。")

        by_person = self.diary.search("潮汐", people=["阿明"])
        self.assertEqual([row["day"] for row in by_person], [first])
        by_date = self.diary.search("潮汐", start=second, end=second)
        self.assertEqual([row["day"] for row in by_date], [second])
        combo = self.diary.search("潮汐", start=first, end=second, people="小紅")
        self.assertEqual([row["day"] for row in combo], [second])
        self.assertEqual(self.diary.search("阿明")[0]["day"], first)

    async def test_archive_is_searchable_and_readable_after_prune(self):
        old_day = day_before(61)
        fresh_day = day_before(2)
        life = FakeLifeStore([
            {"id": "exp-old", "ts": ts_on(old_day, 9), "platform": "bili",
             "summary": "在B站看了很舊的潮汐紀錄片", "topics": ["潮汐"]},
            {"id": "exp-fresh", "ts": ts_on(fresh_day, 9), "platform": "bili",
             "summary": "在B站看了新的潮汐紀錄片", "topics": ["潮汐"]},
        ])
        bot = SimpleNamespace(config=self.config, life_store=life,
                              longmem=SimpleNamespace(episodes=[]))
        await self.write(bot, old_day, "很舊的潮汐紀錄片，我當時覺得很好看。")
        await self.write(bot, fresh_day, "新的潮汐紀錄片，也不錯。")

        self.assertEqual(self.diary.prune(), 1)
        self.assertFalse(os.path.exists(self.diary.path_for(old_day)))
        archive_md = os.path.join(self.diary.dir, "archive", f"{old_day}.md")
        archive_json = os.path.join(self.diary.dir, "archive", f"{old_day}.json")
        self.assertTrue(os.path.exists(archive_md))
        self.assertTrue(os.path.exists(archive_json))
        self.assertTrue(self.diary.exists(old_day))

        hit = self.diary.search("很舊")
        self.assertEqual(hit[0]["day"], old_day)
        self.assertEqual(hit[0]["experience_ids"], ["exp-old"])
        read = self.diary.read(hit[0]["id"])
        self.assertTrue(read["ok"])
        self.assertIn("很舊的潮汐紀錄片", read["text"])
        self.assertEqual(read["experience_ids"], ["exp-old"])

    async def test_cross_platform_generation_without_longmem(self):
        day = "2026-10-22"
        life = FakeLifeStore([
            {"id": "exp-bili", "root_source_id": "root-1", "ts": ts_on(day, 9),
             "platform": "bili", "summary": "在B站看了潮汐紀錄片",
             "reaction": "覺得很有意思", "people": ["阿明"], "topics": ["潮汐"]},
            {"id": "exp-qzone", "ts": ts_on(day, 15), "platform": "qzone",
             "summary": "在空間發了潮汐筆記", "people": [], "topics": ["潮汐"]},
            {"id": "exp-bili", "ts": ts_on(day, 9), "platform": "bili",
             "summary": "在B站看了潮汐紀錄片", "people": ["阿明"]},   # 重複經歷
        ])
        bot = SimpleNamespace(config=self.config, life_store=life,
                              mood=SimpleNamespace(label="開心"))
        reply = "今天在B站看了潮汐紀錄片，覺得很有意思，後來又在空間寫了筆記。"
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": reply})) as judge:
            body = await self.diary.write_for(bot, day)

        self.assertEqual(body, reply)
        self.assertTrue(os.path.exists(self.diary.path_for(day)))
        prompt = judge.await_args_list[0].args[1]
        self.assertIn("在B站看了潮汐紀錄片", prompt)
        self.assertIn("在空間發了潮汐筆記", prompt)
        self.assertEqual(prompt.count("在B站看了潮汐紀錄片"), 1)
        self.assertIn("（B站）", prompt)
        self.assertIn("（空間）", prompt)

        self.assertEqual(len(life.calls), 1)
        limit, start, end = life.calls[0]
        self.assertEqual(limit, 100)
        self.assertEqual(start, ts_on(day, 0))
        self.assertEqual(end, ts_on(day, 0) + 86400)

        with open(os.path.join(self.diary.dir, ".index", f"{day}.json"),
                  "r", encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["text"], reply)
        self.assertEqual(sidecar["experience_ids"], ["exp-bili", "exp-qzone"])
        self.assertEqual(sidecar["people"], ["阿明"])
        self.assertEqual(sidecar["topics"], ["潮汐"])
        self.assertEqual(len(sidecar["sources"]), 2)

        hits = self.diary.search("潮汐", people=["阿明"])
        self.assertEqual(hits[0]["id"], day)
        self.assertEqual(hits[0]["experience_ids"], ["exp-bili", "exp-qzone"])

    async def test_same_summary_keeps_independent_sources_and_people(self):
        day = "2026-10-24"
        life = FakeLifeStore([
            {"id": "exp-a", "ts": ts_on(day, 9), "platform": "bili",
             "summary": "都聊了潮汐", "people": ["阿明"], "topics": ["潮汐", "海"]},
            {"id": "exp-b", "ts": ts_on(day, 15), "platform": "qzone",
             "summary": "都聊了潮汐", "people": ["小紅"], "topics": ["潮汐", "紀錄片"]},
        ])
        bot = SimpleNamespace(config=self.config, life_store=life,
                              longmem=SimpleNamespace(episodes=[]))
        await self.write(bot, day, "今天在不同地方都聊到潮汐，聽到的說法不太一樣。")

        with open(os.path.join(self.diary.dir, ".index", f"{day}.json"),
                  "r", encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["experience_ids"], ["exp-a", "exp-b"])
        self.assertEqual(sidecar["people"], ["阿明", "小紅"])
        self.assertEqual(sidecar["topics"], ["潮汐", "海", "紀錄片"])
        self.assertEqual(len(sidecar["sources"]), 2)
        self.assertEqual({source["platform"] for source in sidecar["sources"]},
                         {"bili", "qzone"})
        self.assertEqual({source["id"] for source in sidecar["sources"]},
                         {"exp-a", "exp-b"})

        self.assertEqual(self.diary.search("潮汐", people=["阿明"])[0]["day"], day)
        self.assertEqual(self.diary.search("潮汐", people=["小紅"])[0]["day"], day)
        read = self.diary.read(day)
        self.assertEqual(read["experience_ids"], ["exp-a", "exp-b"])

    async def test_existing_diary_is_not_rewritten_and_supplement_keeps_old(self):
        day = "2026-10-25"
        life = FakeLifeStore([
            {"id": "exp-old", "ts": ts_on(day, 9), "platform": "local",
             "summary": "和舊朋友聊了潮汐", "people": ["阿明"], "topics": ["潮汐"]},
        ])
        bot = SimpleNamespace(config=self.config, life_store=life,
                              longmem=SimpleNamespace(episodes=[]))
        await self.write(bot, day, "舊正文：和舊朋友聊潮汐。")
        path = self.diary.path_for(day)
        old_file = Path(path).read_text(encoding="utf-8")
        with open(os.path.join(self.diary.dir, ".index", f"{day}.json"),
                  "r", encoding="utf-8") as f:
            old_sidecar = json.load(f)

        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": "不該重寫的正文"})) as judge:
            repeated = await self.diary.write_for(bot, day)
        judge.assert_not_awaited()
        self.assertEqual(repeated, "舊正文：和舊朋友聊潮汐。")
        self.assertEqual(Path(path).read_text(encoding="utf-8"), old_file)
        with open(os.path.join(self.diary.dir, ".index", f"{day}.json"),
                  "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), old_sidecar)

        life.rows.append({
            "id": "exp-new", "ts": ts_on(day, 18), "platform": "qzone",
            "summary": "和小紅聊了潮汐紀錄片", "people": ["小紅"],
            "topics": ["潮汐", "紀錄片"],
        })
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": "補記正文：後來和小紅聊了紀錄片。"})):
            await self.diary.write_for(bot, day)

        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("舊正文：和舊朋友聊潮汐。", text)
        self.assertIn("補記正文：後來和小紅聊了紀錄片。", text)
        self.assertIn("## 补记（", text)
        self.assertEqual(text.count("舊正文：和舊朋友聊潮汐。"), 1)
        with open(os.path.join(self.diary.dir, ".index", f"{day}.json"),
                  "r", encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["experience_ids"], ["exp-old", "exp-new"])
        self.assertEqual(sidecar["people"], ["阿明", "小紅"])
        self.assertEqual(sidecar["topics"], ["潮汐", "紀錄片"])
        self.assertEqual(len(sidecar["sources"]), 2)
        self.assertIn("舊正文：和舊朋友聊潮汐。", sidecar["text"])
        self.assertIn("補記正文：後來和小紅聊了紀錄片。", sidecar["text"])

        first = self.diary.read(day, offset=0, limit=10)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_offset"], 10)
        whole = self.diary.read(day, limit=100000)
        self.assertEqual(first["text"] + self.diary.read(
            day, offset=10, limit=100000)["text"], whole["text"])
        self.assertIn("舊正文", whole["text"])
        self.assertIn("補記正文", whole["text"])
        self.assertEqual(self.diary.search("潮汐", people=["小紅"])[0]["day"], day)
        self.assertEqual(self.diary.search("潮汐", people=["阿明"])[0]["day"], day)

        with patch("pawbot.diary.judge.judge_json", AsyncMock()) as judge:
            await self.diary.write_for(bot, day)
        judge.assert_not_awaited()
        self.assertEqual(Path(path).read_text(encoding="utf-8"), text)

    async def test_archive_never_overwrites_preserved_text_or_index(self):
        old_day = day_before(61)
        life = FakeLifeStore([
            {"id": "exp-old", "ts": ts_on(old_day, 9), "platform": "bili",
             "summary": "很舊的潮汐紀錄片", "people": ["阿明"], "topics": ["潮汐"]},
        ])
        bot = SimpleNamespace(config=self.config, life_store=life,
                              longmem=SimpleNamespace(episodes=[]))
        await self.write(bot, old_day, "A 的舊正文：很舊的潮汐紀錄片。")
        self.assertEqual(self.diary.prune(), 1)
        archive = os.path.join(self.diary.dir, "archive", f"{old_day}.md")
        archive_index = os.path.join(self.diary.dir, "archive", f"{old_day}.json")
        archived_file = Path(archive).read_text(encoding="utf-8")
        with open(archive_index, "r", encoding="utf-8") as f:
            archived_meta = json.load(f)

        # 已歸檔的日期再生成時，默認不重寫，也不在 active 目錄造出第二篇。
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": "不該生成的 B 正文"})) as judge:
            await self.diary.write_for(bot, old_day)
        judge.assert_not_awaited()
        self.assertFalse(os.path.exists(self.diary.path_for(old_day)))
        self.assertEqual(Path(archive).read_text(encoding="utf-8"), archived_file)
        with open(archive_index, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), archived_meta)

        # 真正的新素材只追加到歸檔，舊正文 / 舊經歷 / 舊人物都保留。
        life.rows.append({
            "id": "exp-new", "ts": ts_on(old_day, 18), "platform": "qzone",
            "summary": "後來和小紅聊了舊紀錄片", "people": ["小紅"],
            "topics": ["紀錄片"],
        })
        with patch("pawbot.diary.judge.judge_json",
                   AsyncMock(return_value={"diary": "補記：後來和小紅聊了舊紀錄片。"})):
            await self.diary.write_for(bot, old_day)
        text = Path(archive).read_text(encoding="utf-8")
        self.assertIn("A 的舊正文：很舊的潮汐紀錄片。", text)
        self.assertIn("補記：後來和小紅聊了舊紀錄片。", text)
        self.assertFalse(os.path.exists(self.diary.path_for(old_day)))
        with open(archive_index, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["experience_ids"], ["exp-old", "exp-new"])
        self.assertEqual(sidecar["people"], ["阿明", "小紅"])
        self.assertEqual(self.diary.search("潮汐", people=["阿明"])[0]["day"], old_day)
        self.assertEqual(self.diary.search("潮汐", people=["小紅"])[0]["day"], old_day)
        read = self.diary.read(old_day)
        self.assertIn("A 的舊正文", read["text"])
        self.assertIn("補記：後來和小紅聊了舊紀錄片。", read["text"])
        self.assertEqual(read["experience_ids"], ["exp-old", "exp-new"])

        # 防禦性回歸：若同一天又出現 active 檔案，第二次歸檔必須合併而非覆蓋。
        self.diary._write(bot, old_day, "B 的正文：另一篇同一天日記。",
                          experience_ids=["exp-b"], people=["小紅"])
        self.assertEqual(self.diary.prune(), 1)
        merged = Path(archive).read_text(encoding="utf-8")
        self.assertIn("A 的舊正文：很舊的潮汐紀錄片。", merged)
        self.assertIn("補記：後來和小紅聊了舊紀錄片。", merged)
        self.assertIn("B 的正文：另一篇同一天日記。", merged)
        self.assertEqual(merged.count("A 的舊正文"), 1)
        with open(archive_index, "r", encoding="utf-8") as f:
            merged_meta = json.load(f)
        self.assertEqual(merged_meta["experience_ids"], ["exp-old", "exp-new", "exp-b"])
        self.assertEqual(merged_meta["people"], ["阿明", "小紅"])
        self.assertIn("A 的舊正文", merged_meta["text"])
        self.assertIn("B 的正文", merged_meta["text"])
        self.assertIn("B 的正文", self.diary.read(old_day)["text"])

    async def test_missing_dir_is_not_created_by_search_read_or_prune(self):
        fresh = Diary(self.tmp.name, self.config)
        self.assertFalse(os.path.exists(fresh.dir))
        self.assertEqual(fresh.search("任何主題"), [])
        self.assertEqual(fresh.prune(), 0)
        self.assertFalse(fresh.read("2026-10-21")["ok"])
        self.assertFalse(os.path.exists(fresh.dir))


class FakeEntities:
    def __init__(self, rows):
        self.rows = list(rows)

    def load(self, now):
        return list(self.rows)


class DiaryStub:
    def __init__(self):
        self.called = False

    def recent(self, n=2):
        self.called = True
        return [("2026-10-21", "不該自動注入的日記正文")]


class L2DiaryTests(unittest.TestCase):
    def test_l2_keeps_day_summary_but_not_diary(self):
        now = time.time()
        entities = FakeEntities([
            {"kind": "day", "ts": now - 3600, "text": "昨天在群裡聊了潮汐",
             "source": "group_1"},
        ])
        diary = DiaryStub()
        bot = SimpleNamespace(diary=diary, config=Cfg({}))
        text, stats = layers.l2_days(bot, "group_1", entities, days=2, now=now)
        self.assertIn("潮汐", text)
        self.assertNotIn("不該自動注入的日記正文", text)
        self.assertFalse(diary.called)
        self.assertEqual(stats["chars"], len(text))


if __name__ == "__main__":
    unittest.main()
