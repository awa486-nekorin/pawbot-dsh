"""关系网 + 记忆接口的离线测试：临时目录、假 ctx / 假 bot，不连 QQ、不起服务。"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.relations import Relations
from pawbot.webui import WebUI
from plugins import relations as plugin


class Cfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.summaries = {}
        self.turns = {}
        self.calls = []

    def window(self, session_id, limit=None):
        rows = list(self.rows)
        return rows[-int(limit):] if limit else rows

    def summary(self, session_id):
        return self.summaries.get(session_id, "")

    def turn_summary(self, session_id):
        return self.turns.get(session_id, "")

    def set_turn_summary(self, session_id, text, upto):
        self.calls.append((session_id, text, upto))
        self.turns[session_id] = text


def make_rel(tmp, **extra):
    values = dict(extra)
    return Relations(tmp, Cfg(values))


class PairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rel = make_rel(self.tmp.name)

    def test_pair_key_is_sorted_and_score_accumulates(self):
        self.assertTrue(self.rel.note_interaction(20001, 10001, "reply", group_id=7))
        row = self.rel.data["pairs"]["10001|20001"]
        self.assertEqual(row["a"], "10001")
        self.assertEqual(row["b"], "20001")
        self.assertAlmostEqual(row["score"], 1.0)
        self.assertGreater(row["last_ts"], 0)

        self.rel.note_interaction(10001, 20001, "mention", group_id=7)
        self.rel.note_interaction(20001, 10001, "poke", group_id=7)
        row = self.rel.data["pairs"]["10001|20001"]
        self.assertAlmostEqual(row["score"], 3.0)
        self.assertEqual(row["kinds"], {"reply": 1, "mention": 1, "poke": 1})

    def test_invalid_interactions_are_ignored(self):
        self.assertFalse(self.rel.note_interaction(1, 1, "reply"))
        self.assertFalse(self.rel.note_interaction(1, 2, "unknown"))
        self.assertFalse(self.rel.note_interaction("", 2, "reply"))
        self.assertEqual(self.rel.data["pairs"], {})

    def test_top_partners_sorted_and_limited(self):
        for _ in range(3):
            self.rel.note_interaction(1, 3, "reply")     # 3.0
        for _ in range(2):
            self.rel.note_interaction(1, 2, "poke")      # 2.4
        partners = self.rel.top_partners(1)
        self.assertEqual([uid for uid, _ in partners], ["3", "2"])
        self.assertEqual(len(self.rel.top_partners(1, limit=1)), 1)
        self.assertEqual(self.rel.top_partners(1, limit=0), [])

    def test_top_partners_can_be_scoped_to_one_group(self):
        for _ in range(2):
            self.rel.note_interaction(1, 2, "poke", group_id=7)   # 群 7：2.4
        for _ in range(3):
            self.rel.note_interaction(1, 3, "reply", group_id=8)  # 群 8：3.0
        self.assertEqual([u for u, _ in self.rel.top_partners(1, group_id=7)], ["2"])
        self.assertEqual([u for u, _ in self.rel.top_partners(1, group_id=8)], ["3"])
        self.assertEqual([u for u, _ in self.rel.top_partners(1)], ["3", "2"])


class HintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rel = make_rel(self.tmp.name)

    def names(self, uid):
        return {"2": "阿明", "3": "阿亮"}.get(str(uid), "")

    def test_weak_relation_gets_no_hint(self):
        self.rel.note_interaction(1, 2, "reply")          # 1.0
        self.rel.note_interaction(1, 2, "mention")        # 1.8
        self.assertEqual(self.rel.pair_hint(1, self.names), "")
        self.rel.note_interaction(1, 2, "poke")           # 3.0
        self.assertIn("阿明", self.rel.pair_hint(1, self.names))

    def test_hint_uses_qq_when_name_unknown(self):
        for _ in range(3):
            self.rel.note_interaction(1, 2, "reply")      # 3.0
        hint = self.rel.pair_hint(1, lambda uid: "")
        self.assertIn("2", hint)
        self.assertNotIn("阿明", hint)

    def test_second_partner_only_listed_when_strong_enough(self):
        for _ in range(3):
            self.rel.note_interaction(1, 2, "reply")      # 3.0
        self.rel.note_interaction(1, 3, "reply")          # 1.0
        hint = self.rel.pair_hint(1, self.names)
        self.assertIn("阿明", hint)
        self.assertNotIn("阿亮", hint)

    def test_min_score_config_and_disable(self):
        for _ in range(3):
            self.rel.note_interaction(1, 2, "reply")
        self.rel.flush()
        strict = make_rel(self.tmp.name, **{"relations.min_score": 5})
        # 同一个文件：strict 会重新载入，确认分数还在
        self.assertEqual(strict.pair_hint(1, self.names), "")
        off = make_rel(self.tmp.name, **{"relations.enabled": False})
        self.assertEqual(off.pair_hint(1, self.names), "")
        self.assertFalse(off.note_interaction(1, 2, "reply"))


class GroupMetaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rel = make_rel(self.tmp.name)

    def test_set_and_get(self):
        self.assertTrue(self.rel.set_group_meta(7, 10001, [10002, "10003", {"uid": 10004}]))
        meta = self.rel.group_meta(7)
        self.assertEqual(meta["owner"], 10001)
        self.assertEqual(meta["admins"], [10002, 10003, 10004])
        self.assertGreater(meta["updated"], 0)

    def test_unchanged_meta_does_not_mark_dirty(self):
        self.assertTrue(self.rel.set_group_meta(7, 1, [2]))
        self.assertFalse(self.rel.set_group_meta(7, 1, [2]))
        self.assertTrue(self.rel.set_group_meta(7, 1, [3]))

    def test_missing_group_and_empty_meta(self):
        self.assertIsNone(self.rel.group_meta(99))
        self.assertFalse(self.rel.set_group_meta(7, 0, []))
        self.assertFalse(self.rel.set_group_meta("", 1, [2]))

    def test_owner_is_not_duplicated_in_admins(self):
        self.assertTrue(self.rel.set_group_meta(7, 1, [1, 2]))
        self.assertEqual(self.rel.group_meta(7)["admins"], [2])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rel = make_rel(self.tmp.name)

    def test_dirty_then_flush_then_reload(self):
        self.rel.note_interaction(1, 2, "reply")
        self.rel.remember_name(2, "阿明")
        self.assertFalse(os.path.exists(self.rel.path))      # 没有每条都写盘
        self.assertTrue(self.rel.flush())
        self.assertTrue(os.path.exists(self.rel.path))
        self.assertFalse(self.rel.flush())                    # 不脏就不写

        again = make_rel(self.tmp.name)
        row = again.data["pairs"]["1|2"]
        self.assertAlmostEqual(row["score"], 1.0)
        self.assertEqual(again.data["names"]["2"], "阿明")

    def test_force_flush_writes_even_without_changes(self):
        self.assertTrue(self.rel.flush(force=True))
        self.assertTrue(os.path.exists(self.rel.path))

    def test_corrupt_json_is_tolerated(self):
        os.makedirs(os.path.dirname(self.rel.path), exist_ok=True)
        with open(self.rel.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        again = make_rel(self.tmp.name)
        self.assertEqual(again.data["pairs"], {})
        self.assertEqual(again.top_partners(1), [])

    def test_group_summary_uses_names_and_stays_in_group(self):
        self.rel.remember_name(111, "阿明")
        self.rel.remember_name(222, "阿亮")
        self.rel.remember_name(333, "阿花")
        self.rel.remember_name(444, "阿草")
        for _ in range(3):
            self.rel.note_interaction(111, 222, "reply", group_id=7)
        for _ in range(3):
            self.rel.note_interaction(333, 444, "reply", group_id=8)
        text7 = self.rel.summarize_group(7)
        self.assertIn("阿明", text7)
        self.assertIn("阿亮", text7)
        self.assertNotIn("阿花", text7)
        self.assertIn("阿花", self.rel.summarize_group(8))
        self.assertEqual(self.rel.summarize_group(9), "")


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patcher = patch.object(Relations, "start_autosave", lambda self: None)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.bot = SimpleNamespace(
            base_dir=self.tmp.name,
            config=Cfg(),
            store=FakeStore([
                {"role": "user", "who": "阿明", "uid": "10001", "content": "早"},
                {"role": "user", "who": "阿亮", "uid": "20001", "content": "早"},
            ]),
        )

    def ctx(self, ev):
        return SimpleNamespace(event=ev, bot=self.bot, config=self.bot.config,
                               self_id=99999, group_id=getattr(ev, "group_id", None))

    async def test_reply_maps_quoted_name_back_to_uid(self):
        ev = SimpleNamespace(user_id=20001, group_id=7, is_group=True,
                             session_id="group_7", reply_id="42",
                             quoted_from="阿明", sender_name="阿亮",
                             message=[{"type": "text", "data": {"text": "你说啥"}}])
        await plugin.note_message(self.ctx(ev))
        row = self.bot.relations.data["pairs"]["10001|20001"]
        self.assertEqual(row["kinds"]["reply"], 1)
        self.assertEqual(self.bot.relations.data["names"]["20001"], "阿亮")

    async def test_mention_ignores_all_self_and_bot(self):
        ev = SimpleNamespace(user_id=20001, group_id=7, is_group=True,
                             session_id="group_7", reply_id=None, quoted_from="",
                             sender_name="阿亮",
                             message=[{"type": "at", "data": {"qq": "all"}},
                                      {"type": "at", "data": {"qq": 20001}},
                                      {"type": "at", "data": {"qq": 99999}},
                                      {"type": "at", "data": {"qq": 30003}}])
        await plugin.note_message(self.ctx(ev))
        pairs = self.bot.relations.data["pairs"]
        self.assertEqual(list(pairs), ["20001|30003"])
        self.assertEqual(pairs["20001|30003"]["kinds"]["mention"], 1)

    async def test_poke_notice_uses_target_id(self):
        ev = SimpleNamespace(sub_type="poke", notice_type="notify", user_id=20001,
                             target_id=30003, group_id=7,
                             raw_notice={"user_id": 20001, "target_id": 30003})
        await plugin.note_poke(self.ctx(ev))
        row = self.bot.relations.data["pairs"]["20001|30003"]
        self.assertEqual(row["kinds"]["poke"], 1)

    async def test_poke_notice_falls_back_to_raw_user_id(self):
        ev = SimpleNamespace(sub_type="poke", notice_type="notify", user_id=20001,
                             target_id=0, group_id=7,
                             raw_notice={"user_id": 30003})
        await plugin.note_poke(self.ctx(ev))
        self.assertEqual(self.bot.relations.data["pairs"]["20001|30003"]["kinds"]["poke"], 1)

    async def test_poke_involving_the_bot_is_skipped(self):
        ev = SimpleNamespace(sub_type="poke", notice_type="notify", user_id=99999,
                             target_id=20001, group_id=7,
                             raw_notice={"user_id": 99999, "target_id": 20001})
        await plugin.note_poke(self.ctx(ev))
        self.assertEqual(self.bot.relations.data["pairs"], {})

    async def test_group_meta_copied_from_groupinfo_cache(self):
        class FakeGI:
            def cached(self, gid):
                return {"name": "测试群", "owner_id": 10001,
                        "admins": [{"uid": 10002, "name": "阿明"}]}

        ev = SimpleNamespace(user_id=20001, group_id=7, is_group=True,
                             session_id="group_7", reply_id=None, quoted_from="",
                             sender_name="阿亮", message=[])
        with patch("pawbot.groupinfo.for_bot", return_value=FakeGI()):
            await plugin.note_message(self.ctx(ev))
        meta = self.bot.relations.group_meta(7)
        self.assertEqual(meta["owner"], 10001)
        self.assertEqual(meta["admins"], [10002])


class SenseHintTests(unittest.TestCase):
    def setUp(self):
        from pawbot.agent import Agent
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rel = make_rel(self.tmp.name)
        self.bot = SimpleNamespace(
            base_dir=self.tmp.name,
            config=Cfg({"topic.enabled": False}),
            store=FakeStore([{"role": "user", "who": "阿明", "uid": "2", "content": "早"}]),
            relations=self.rel,
        )
        self.agent = Agent(self.bot)
        self.ev = SimpleNamespace(user_id="1")
        self.ctx = SimpleNamespace(event=self.ev, session_id="group_7",
                                   is_group=True, group_id=7, sender_name="阿亮")

    def test_relation_hint_appears_only_after_enough_interactions(self):
        self.assertNotIn("关系印象", self.agent._sense_hint(self.ctx))
        for _ in range(3):
            self.rel.note_interaction(1, 2, "reply", group_id=7)
        hint = self.agent._sense_hint(self.ctx)
        self.assertIn("关系印象", hint)
        self.assertIn("阿明", hint)


class FakeWriter:
    def __init__(self):
        self.buf = b""

    def write(self, data):
        self.buf += data

    async def drain(self):
        pass

def parse_response(writer):
    head, _, body = writer.buf.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n")[0].decode("latin-1").split(" ")[1])
    return status, json.loads(body.decode("utf-8"))


class WebUIMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bot = SimpleNamespace(
            base_dir=self.tmp.name,
            config=Cfg(),
            store=FakeStore(),
            people=SimpleNamespace(all_people=lambda: [
                {"user_id": "10001", "name": "阿明", "affinity_label": "挺熟",
                 "sections": {"性格": "爱吐槽"}}]),
        )
        self.web = WebUI(self.bot, root=str(Path(__file__).resolve().parents[1] / "web"))

    async def test_memory_returns_200_without_longmem_reminders_diary(self):
        writer = FakeWriter()
        await self.web._route_get(writer, "/api/memory", {"session_id": ["group_1"]})
        status, payload = parse_response(writer)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["episodes"], [])
        self.assertEqual(data["reminders"], [])
        self.assertEqual(data["diary"], [])
        self.assertEqual(data["people"][0]["name"], "阿明")

    async def test_memory_collects_optional_modules(self):
        self.bot.store.summaries["group_1"] = "更早的摘要"
        self.bot.store.turns["group_1"] = "刚才在聊考试"
        self.bot.longmem = SimpleNamespace(episodes=[
            {"ts": i, "session_id": "group_1", "summary": f"经历{i}", "importance": 2}
            for i in range(25)
        ])
        self.bot.reminders = SimpleNamespace(list_pending=lambda: [{"text": "明天带伞"}])
        today = time.strftime("%Y-%m-%d")
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        diary_dir = os.path.join(self.tmp.name, "data", "diary")
        os.makedirs(diary_dir, exist_ok=True)
        Path(os.path.join(diary_dir, today + ".md")).write_text("今天的日记", encoding="utf-8")
        self.bot.diary = SimpleNamespace(recent=lambda n: [
            (yesterday, "昨天的日记")])   # diary.recent 的真实形状

        data = self.web._memory_data("group_1")
        self.assertEqual(data["summary"], "更早的摘要")
        self.assertEqual(data["turn_summary"], "刚才在聊考试")
        self.assertEqual(len(data["episodes"]), 20)
        self.assertEqual(data["episodes"][0]["summary"], "经历24")   # 最新在前
        self.assertEqual(data["reminders"], [{"text": "明天带伞"}])
        self.assertEqual(data["diary"][0]["day"], today)
        self.assertIn("今天的日记", data["diary"][0]["text"])
        self.assertIn("昨天的日记", [row["text"] for row in data["diary"]])

    async def test_forget_turn_summary_via_post(self):
        writer = FakeWriter()
        body = json.dumps({"session_id": "group_1", "what": "turn_summary"}).encode()
        await self.web._route_post(writer, "/api/memory/forget", body)
        status, payload = parse_response(writer)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.bot.store.turns["group_1"], "")
        self.assertEqual(len(self.bot.store.calls), 1)

    async def test_forget_episodes_without_drop_session_returns_501(self):
        writer = FakeWriter()
        body = json.dumps({"session_id": "group_1", "what": "episodes"}).encode()
        await self.web._route_post(writer, "/api/memory/forget", body)
        status, payload = parse_response(writer)
        self.assertEqual(status, 501)
        self.assertFalse(payload["ok"])

    async def test_forget_episodes_uses_drop_session_when_available(self):
        dropped = []
        self.bot.longmem = SimpleNamespace(drop_session=lambda sid: dropped.append(sid) or 3)
        status, payload = await self.web._forget_memory({"session_id": "group_1",
                                                         "what": "episodes"})
        self.assertEqual(status, 200)
        self.assertEqual(dropped, ["group_1"])
        self.assertEqual(payload["data"]["removed"], 3)

    async def test_forget_episodes_supports_async_drop_session(self):
        async def drop(sid):
            return 2

        self.bot.longmem = SimpleNamespace(drop_session=drop)
        status, payload = await self.web._forget_memory({"session_id": "group_1",
                                                         "what": "episodes"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["removed"], 2)

    async def test_forget_diary_deletes_only_inside_diary_dir(self):
        today = time.strftime("%Y-%m-%d")
        diary_dir = os.path.join(self.tmp.name, "data", "diary")
        os.makedirs(diary_dir, exist_ok=True)
        path = os.path.join(diary_dir, today + ".md")
        Path(path).write_text("今天的日记", encoding="utf-8")
        outside = os.path.join(self.tmp.name, "data", "secret.md")
        Path(outside).write_text("别删我", encoding="utf-8")

        status, payload = await self.web._forget_memory({"what": "diary", "day": today})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["removed"])
        self.assertFalse(os.path.exists(path))

        status, _ = await self.web._forget_memory({"what": "diary", "day": "../../secret"})
        self.assertEqual(status, 400)
        self.assertTrue(os.path.exists(outside))

    async def test_unknown_forget_target_is_rejected(self):
        status, payload = await self.web._forget_memory({"session_id": "g", "what": "everything"})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
