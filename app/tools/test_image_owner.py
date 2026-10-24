"""图片归属：recent_images 新/旧格式兼容、自己发的图 self=True、
「刚才那张图」逐张带归属、本条/引用/转发三来源措辞、annotate_last 贴对行。离线。"""
import os
import sys
import tempfile
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import perceive
from pawbot.agent import Agent
from pawbot.store import Store
from plugins import ai_chat

URL = "data:image/jpeg;base64,AAAA"


def make_ctx(image_items=None, **event_attrs):
    values = {"to_me": False, "quoted_text": "", "quoted_from": "",
              "image_desc": "", "image_data": [], "quote_image_data": [],
              "forward_image_data": [], "image_items": image_items or [],
              "quoted_is_self": False, "person_card": "", "forward_text": "",
              "card_text": "", "talked_about": False, "voice_text": "",
              "video_desc": ""}
    values.update(event_attrs)
    ev = SimpleNamespace(**values)
    return SimpleNamespace(is_group=True, sender_name="阿明", user_id=123456,
                           session_id="group_1", group_id=1, state={}, event=ev)


class PushRecentTests(unittest.TestCase):
    def _bot(self):
        return SimpleNamespace(recent_images={})

    def test_bare_urls_are_wrapped_into_dicts(self):
        bot = self._bot()
        perceive._push_recent(bot, "g1", [URL + "a", URL + "b"])
        dq = bot.recent_images["g1"]
        self.assertEqual(len(dq), 2)
        self.assertTrue(all(isinstance(x, dict) for x in dq))
        self.assertEqual(dq[0]["url"], URL + "a")
        self.assertFalse(dq[0]["self"])
        self.assertEqual(dq[0]["who"], "")
        self.assertEqual(dq[0]["uid"], "")

    def test_dict_entries_preserved_with_attribution(self):
        bot = self._bot()
        perceive._push_recent(bot, "g1", [
            {"url": URL + "m", "who": "阿明", "uid": "1234", "self": False,
             "ts": 1.0, "desc": "一只猫"},
        ])
        dq = bot.recent_images["g1"]
        self.assertEqual(dq[0]["who"], "阿明")
        self.assertEqual(dq[0]["uid"], "1234")
        self.assertEqual(dq[0]["desc"], "一只猫")
        self.assertEqual(dq[0]["ts"], 1.0)

    def test_mixed_old_and_new(self):
        bot = self._bot()
        perceive._push_recent(bot, "g1", [URL + "old",
            {"url": URL + "new", "who": "小红", "self": False}])
        dq = bot.recent_images["g1"]
        self.assertEqual([x["url"] for x in dq], [URL + "old", URL + "new"])

    def test_placeholder_without_url_still_recorded(self):
        bot = self._bot()
        perceive._push_recent(bot, "g1", [
            {"url": "", "who": "我（大肥鱼）", "self": True, "kind": "sticker"}])
        self.assertEqual(len(bot.recent_images["g1"]), 1)
        self.assertTrue(bot.recent_images["g1"][0]["self"])


class SelfMediaRecentsTests(unittest.TestCase):
    def test_http_image_keeps_url(self):
        recents = ai_chat._self_media_recents([
            {"type": "image", "data": {"file": "https://x/a.jpg"}}])
        self.assertEqual(len(recents), 1)
        self.assertTrue(recents[0]["self"])
        self.assertEqual(recents[0]["who"], "我（大肥鱼）")
        self.assertEqual(recents[0]["url"], "https://x/a.jpg")

    def test_local_or_base64_image_uses_placeholder(self):
        recents = ai_chat._self_media_recents([
            {"type": "image", "data": {"file": "base64://abcd"}}])
        self.assertEqual(recents[0]["url"], "")
        self.assertTrue(recents[0]["placeholder"])
        self.assertTrue(recents[0]["self"])

    def test_sticker_marks_kind(self):
        recents = ai_chat._self_media_recents([
            {"type": "_media", "data": {"kind": "sticker", "url": "https://s/s.png"}}])
        self.assertEqual(recents[0]["kind"], "sticker")
        self.assertEqual(recents[0]["url"], "https://s/s.png")

    def test_media_note_distinguishes_sticker_from_image(self):
        self.assertEqual(
            ai_chat._media_note([{"type": "_media", "data": {"kind": "sticker"}}]),
            "（我自己发了一条：[表情包]）")
        self.assertEqual(
            ai_chat._media_note([{"type": "image", "data": {}}]),
            "（我自己发了一条：[图片]）")


class VisionFallbackTests(unittest.TestCase):
    def _agent(self, items):
        agent = Agent.__new__(Agent)
        agent.bot = SimpleNamespace(config={},
                                    recent_images={"group_1": deque(items, maxlen=6)})
        return agent

    def _cue(self, agent):
        return agent._user_message(make_ctx(), "上面那张图是啥", {"who": "阿明", "uid": "123456"})

    def test_fallback_lists_owner_per_image(self):
        agent = self._agent([
            {"url": URL + "0", "who": "阿明", "uid": "1111222233334444", "self": False, "desc": "一只猫"},
            {"url": URL + "1", "who": "我（大肥鱼）", "self": True, "desc": ""},
        ])
        msg = self._cue(agent)
        blocks = msg["content"]
        urls = [b["image_url"]["url"] for b in blocks if b["type"] == "image_url"]
        self.assertEqual(urls, [URL + "0", URL + "1"])
        text = blocks[0]["text"]
        self.assertIn("1. 阿明(尾号4444) 发的：一只猫", text)
        self.assertIn("2. 我（大肥鱼） 自己发的", text)
        self.assertIn("别把别人的图当成自己发的", text)

    def test_old_bare_string_deque_still_works(self):
        agent = self._agent([URL + "0", URL + "1", URL + "2"])
        msg = self._cue(agent)
        blocks = msg["content"]
        urls = [b["image_url"]["url"] for b in blocks if b["type"] == "image_url"]
        self.assertEqual(urls, [URL + "0", URL + "1", URL + "2"])
        self.assertIn("群里某人发的", blocks[0]["text"])


class SenseHintSourceTests(unittest.TestCase):
    def _hint(self, **attrs):
        bot = SimpleNamespace(config={"topic.enabled": False})
        return Agent(bot)._sense_hint(make_ctx(**attrs))

    def test_three_sources_have_distinct_wording(self):
        hint = self._hint(
            image_items=[
                {"source": "本条", "desc": "一只猫"},
                {"source": "引用", "desc": "一只狗"},
                {"source": "转发", "desc": "一张表格"},
            ],
            image_data=[URL + "own"], quote_image_data=[URL + "q"],
            forward_image_data=[URL + "f"], quoted_from="一碗麦西粉")
        self.assertIn("这条消息的图是 阿明 发的", hint)
        self.assertIn("被引用那条里的图是 一碗麦西粉 发的，不是 阿明 的", hint)
        self.assertIn("转发内容里的图来自被转发的人，不是 阿明 自己发的", hint)
        self.assertIn("一只猫", hint)
        self.assertIn("一只狗", hint)
        self.assertIn("一张表格", hint)

    def test_quoted_self_image_is_flagged_as_own(self):
        hint = self._hint(
            image_items=[{"source": "引用", "desc": "我自己发的"}],
            quote_image_data=[URL + "q"], quoted_is_self=True)
        self.assertIn("被引用那条里的图是**你自己**发的图", hint)


class AnnotateOwnerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "s.json"))

    def test_two_images_from_same_uid_go_to_own_rows(self):
        # 模拟真实流程：发一条 → 描述一条 → 再发一条 → 再描述
        self.store.append("g", "user", "看这个[图片]", who="阿明", uid=42)
        self.assertTrue(self.store.annotate_last("g", 42, "第一张"))
        self.store.append("g", "user", "再看这个[图片]", who="阿明", uid=42)
        self.assertTrue(self.store.annotate_last("g", 42, "第二张"))
        self.store.append("g", "user", "补充一句", who="阿明", uid=42)
        rows = self.store.window("g")
        self.assertIn("[图片：第一张]", rows[0]["content"])
        self.assertIn("[图片：第二张]", rows[1]["content"])
        self.assertEqual(rows[2]["content"], "补充一句")

    def test_other_uid_row_is_never_touched(self):
        self.store.append("g", "user", "别人的[图片]", who="小红", uid=99)
        self.store.append("g", "user", "我的[图片]", who="阿明", uid=42)
        self.assertTrue(self.store.annotate_last("g", 42, "我的描述"))
        rows = self.store.window("g")
        self.assertEqual(rows[0]["content"], "别人的[图片]")
        self.assertIn("[图片：我的描述]", rows[1]["content"])

    def test_already_described_row_is_skipped(self):
        self.store.append("g", "user", "看这个[图片]", who="阿明", uid=42)
        self.store.append("g", "user", "又一张[图片]", who="阿明", uid=42)
        self.assertTrue(self.store.annotate_last("g", 42, "第一次"))
        self.assertTrue(self.store.annotate_last("g", 42, "第二次"))
        rows = self.store.window("g")
        # 第一次贴到最近的那条图行，第二次贴到更早的那条
        self.assertIn("[图片：第一次]", rows[1]["content"])
        self.assertIn("[图片：第二次]", rows[0]["content"])
        # 都描述完了，再来一次就不该乱贴
        self.assertFalse(self.store.annotate_last("g", 42, "第三次"))


class EnrichAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_items_carry_from_and_self(self):
        bot = SimpleNamespace(config={"llm.vision_max_images": 4}, recent_images={},
                              store=SimpleNamespace(annotate_last=lambda *a, **k: True))
        ev = SimpleNamespace(type="message", message=[{"type": "image", "data": {"file": "x.jpg"}}],
                             reply_id=None, self_id=99, is_group=True, to_me=False,
                             plain="看图", session_id="group_1", user_id=42,
                             sender_name="阿明", sender={"nickname": "阿明"},
                             image_data=[], quoted_text="", quoted_from="")
        def fake_details(segments, config, budget=4):
            if not segments:
                return []
            return [{"desc": "一只猫", "urls": [URL]}]

        with patch.object(perceive, "_image_details", side_effect=fake_details):
            await perceive.enrich(bot, None, ev)
        self.assertEqual(len(ev.image_items), 1)
        item = ev.image_items[0]
        self.assertEqual(item["source"], "本条")
        self.assertEqual(item["from"], "阿明")
        self.assertFalse(item["self"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
