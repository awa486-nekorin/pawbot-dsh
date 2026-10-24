"""看图链路：宫格、上下文回填、引用去占位、群信息冷却、近期图片回看。离线。"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import groupinfo, perceive, vision
from pawbot.agent import Agent
from pawbot.breaker import BREAKER
from pawbot.store import Store

URL = "data:image/jpeg;base64,AAAA"


def jpeg(size=(800, 600), color=(120, 40, 40)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def image_seg(n=0) -> dict:
    return {"type": "image", "data": {"file": f"/x/{n}.jpg"}}


class GridTests(unittest.TestCase):
    def setUp(self):
        vision._cache.clear()
        BREAKER.reset()

    def test_multi_image_is_one_grid_call(self):
        raws = [jpeg(color=(40 * i, 80, 160)) for i in range(5)]
        calls = []

        def fake(b64, key, base, model, prompt, timeout=30, budget=900,
                 effort="", retries=1):
            calls.append({"b64": b64, "prompt": prompt, "budget": budget})
            return "第1张：猫\n第2张：狗"

        with patch.object(vision, "_call_vision", side_effect=fake):
            out = vision.see(raws, "k", "https://relay/v1", "gemini-4-argon", 30)

        self.assertEqual(out, "第1张：猫\n第2张：狗")
        self.assertEqual(len(calls), 1, "多图只能调一次模型")
        self.assertEqual(calls[0]["prompt"], vision.GRID_PROMPT)

        from PIL import Image
        grid = Image.open(io.BytesIO(base64.b64decode(calls[0]["b64"])))
        self.assertEqual(grid.size, (vision.GRID_COLS * vision.GRID_CELL,
                                     ((5 + vision.GRID_COLS - 1) // vision.GRID_COLS)
                                     * vision.GRID_CELL))

    def test_same_images_are_cached(self):
        raws = [jpeg(), jpeg(color=(10, 200, 10))]
        calls = []

        def fake(b64, key, base, model, prompt, timeout=30, budget=900,
                 effort="", retries=1):
            calls.append(1)
            return "描述"

        with patch.object(vision, "_call_vision", side_effect=fake):
            vision.see(raws, "k", "https://relay/v1", "m", 30)
            vision.see(raws, "k", "https://relay/v1", "m", 30)
        self.assertEqual(len(calls), 1)

    def test_single_image_uses_normal_prompt(self):
        calls = []

        def fake(b64, key, base, model, prompt, timeout=30, budget=900,
                 effort="", retries=1):
            calls.append(prompt)
            return "一只猫"

        with patch.object(vision, "_call_vision", side_effect=fake):
            out = vision.see([jpeg()], "k", "https://relay/v1", "m", 30)
        self.assertEqual(out, "一只猫")
        self.assertEqual(calls, [vision.PROMPT])


class PreviewUrlTests(unittest.TestCase):
    def test_four_or_fewer_gives_all_originals(self):
        raws = [b"a", b"b", b"c", b"d"]
        with patch.object(vision, "data_url", side_effect=lambda r: "orig:" + r.decode()):
            urls = vision.preview_urls(raws)
        self.assertEqual(urls, ["orig:a", "orig:b", "orig:c", "orig:d"])

    def test_more_than_four_gives_grid_plus_two(self):
        raws = [b"a", b"b", b"c", b"d", b"e"]
        with patch.object(vision, "grid_data_url", return_value="GRID"), \
                patch.object(vision, "data_url", side_effect=lambda r: "orig:" + r.decode()):
            urls = vision.preview_urls(raws)
        self.assertEqual(urls, ["GRID", "orig:a", "orig:b"])

    def test_empty(self):
        self.assertEqual(vision.preview_urls([]), [])


class QuotedPlaceholderTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self):
        cfg = SimpleNamespace(get=lambda k, d=None: {}.get(k, d))
        return SimpleNamespace(config=cfg, recent_images={},
                               store=SimpleNamespace(annotate_last=lambda *a, **k: True))

    async def test_quoted_text_has_no_placeholder_run(self):
        quoted = {"data": {
            "sender": {"nickname": "阿明", "user_id": 7},
            "message": [
                {"type": "text", "data": {"text": "看上图"}},
                {"type": "image", "data": {"file": "/x/1.jpg"}},
                {"type": "image", "data": {"file": "/x/2.jpg"}},
            ],
        }}
        conn = SimpleNamespace(call=AsyncMock(return_value=quoted))
        ev = SimpleNamespace(
            type="message", reply_id="m1", message=[], self_id=99,
            is_group=True, to_me=False, plain="就是这种", session_id="group_1",
            user_id=42, image_data=[], quoted_text="", quoted_from="",
        )
        with patch.object(perceive, "_read_images", return_value=[b"a", b"b"]), \
                patch.object(perceive, "_see", return_value=("两只猫并排", [URL])):
            await perceive.enrich(self._bot(), conn, ev)

        self.assertNotIn("[图片]", ev.quoted_text)
        self.assertNotIn("[图片][图片]", ev.quoted_text)
        self.assertIn("引用了 2 张图", ev.quoted_text)
        self.assertIn("两只猫并排", ev.quoted_text)
        self.assertIn("看上图", ev.quoted_text)
        self.assertEqual(ev.quote_image_data, [URL])
        self.assertEqual(ev.quoted_from, "阿明")


class AnnotateLastTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sessions.json")
        self.store = Store(self.path)
        self.store.append("group_1", "user", "看这个[图片]", who="阿明", uid=42)
        self.store.append("group_1", "user", "没有图的句子", who="阿明", uid=42)

    def test_replaces_placeholder_on_last_user_row(self):
        ok = self.store.annotate_last("group_1", 42, "一只橘猫趴在键盘上")
        self.assertTrue(ok)
        rows = self.store.window("group_1")
        self.assertIn("[图片：一只橘猫趴在键盘上]", rows[-2]["content"])
        self.assertIn("看这个", rows[-2]["content"])
        self.assertEqual(rows[-1]["content"], "没有图的句子")

        # 原子写盘：新实例从文件里读得到同样的内容
        again = Store(self.path)
        self.assertIn("[图片：一只橘猫趴在键盘上]", again.window("group_1")[-2]["content"])

    def test_text_only_row_is_untouched(self):
        store = Store(os.path.join(self.dir, "plain.json"))
        store.append("group_1", "user", "只有文字", who="阿明", uid=42)
        self.assertFalse(store.annotate_last("group_1", 42, "一条消息"))
        self.assertEqual(store.window("group_1")[-1]["content"], "只有文字")

    def test_unknown_session_is_untouched(self):
        self.assertFalse(self.store.annotate_last("group_999", 42, "随便"))
        self.assertFalse(self.store.annotate_last("group_1", "", ""))


class _FakeConn:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.self_id = 999

    async def call(self, action, timeout=20, **params):
        self.calls.append((action, dict(params)))
        value = self.responses.get(action)
        if isinstance(value, Exception):
            raise value
        return value


class GroupInfoCooldownTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, conn):
        return SimpleNamespace(conn_for=lambda: conn, config={})

    async def test_failure_cools_down_without_calling_conn(self):
        conn = _FakeConn({"get_group_info": RuntimeError("inner_error"),
                          "get_group_member_list": [],
                          "get_group_member_info": {}})
        gi = groupinfo.GroupInfo(ttl=60)
        self.assertEqual(await gi.get(self.bot(conn), 77, now=1000.0), {})
        calls_after_fail = len(conn.calls)
        self.assertEqual(calls_after_fail, 1)

        # 冷却期内：即使 TTL 早就过了，也不再打 NapCat
        for t in (1010.0, 1500.0, 2799.0):
            self.assertEqual(await gi.get(self.bot(conn), 77, now=t), {})
        self.assertEqual(len(conn.calls), calls_after_fail)

        # 冷却结束后才会再试
        await gi.get(self.bot(conn), 77, now=1000.0 + 1801)
        self.assertGreater(len(conn.calls), calls_after_fail)

    async def test_failure_returns_stale_cache_during_cooldown(self):
        conn = _FakeConn({"get_group_info": {"group_name": "测试群", "member_count": 3},
                          "get_group_member_list": [],
                          "get_group_member_info": {"role": "member"}})
        gi = groupinfo.GroupInfo(ttl=60)
        info = await gi.get(self.bot(conn), 77, now=1000.0)
        self.assertEqual(info["name"], "测试群")

        conn.responses["get_group_info"] = RuntimeError("inner_error")
        self.assertEqual((await gi.get(self.bot(conn), 77, now=1100.0))["name"], "测试群")
        calls = len(conn.calls)
        self.assertEqual((await gi.get(self.bot(conn), 77, now=1200.0))["name"], "测试群")
        self.assertEqual(len(conn.calls), calls)


class SeeWrapperTests(unittest.IsolatedAsyncioTestCase):
    class Cfg(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    def cfg(self):
        return self.Cfg({"llm.vision_api_key": "k",
                         "llm.vision_base_url": "https://relay/v1",
                         "llm.vision_model": "gemini-4-argon",
                         "llm.vision_timeout": 30,
                         "llm.vision_fallback_model": "gemini-3.8-flash",
                         "llm.vision_fallback_timeout": 30,
                         "llm.api_key": "chat-key"})

    async def test_more_than_four_gives_grid_plus_two_originals(self):
        raws = [jpeg(color=(20 * i, 60, 120)) for i in range(5)]
        with patch.object(vision, "see", return_value="五张图的转述"):
            desc, urls = await perceive._see(raws, self.cfg())
        self.assertEqual(desc, "五张图的转述")
        self.assertEqual(len(urls), 3)
        from PIL import Image
        grid_b64 = urls[0].split(",", 1)[1]
        grid = Image.open(io.BytesIO(base64.b64decode(grid_b64)))
        self.assertEqual(grid.size, (3 * vision.GRID_CELL, 2 * vision.GRID_CELL))
        for url in urls[1:]:
            self.assertTrue(url.startswith("data:image/"))

    async def test_four_or_fewer_gives_originals_only(self):
        raws = [jpeg(), jpeg(color=(0, 0, 200))]
        with patch.object(vision, "see", return_value="两张"):
            desc, urls = await perceive._see(raws, self.cfg())
        self.assertEqual(desc, "两张")
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(u.startswith("data:image/") for u in urls))

    async def test_see_failure_still_returns_originals(self):
        raws = [jpeg()]
        with patch.object(vision, "see", side_effect=RuntimeError("boom")):
            desc, urls = await perceive._see(raws, self.cfg())
        self.assertEqual(desc, "")
        self.assertEqual(len(urls), 1)


def make_ctx(image_data=None, text_state=None):
    return SimpleNamespace(
        is_group=True, sender_name="阿明", user_id=123456,
        session_id="group_1", group_id=1, state=text_state or {},
        event=SimpleNamespace(image_data=image_data or [], to_me=False,
                              quoted_text="", image_desc="", person_card=""),
    )


class RecentImagesTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.bot = SimpleNamespace(
            config={},
            recent_images={"group_1": deque(
                [URL + "0", URL + "1", URL + "2", URL + "3"], maxlen=6)})
        self.me = {"role": "user", "who": "阿明", "uid": "123456"}

    def test_recent_images_attached_for_cue_words(self):
        msg = self.agent._user_message(make_ctx(), "上面那种图还有吗", self.me)
        blocks = msg["content"]
        urls = [b["image_url"]["url"] for b in blocks if b["type"] == "image_url"]
        self.assertEqual(urls, [URL + "1", URL + "2", URL + "3"])
        self.assertIn("这是群里刚才出现过的图", blocks[0]["text"])

    def test_no_cue_means_no_images(self):
        msg = self.agent._user_message(make_ctx(), "今天吃什么", self.me)
        self.assertIsInstance(msg["content"], str)

    def test_own_images_win_over_recent(self):
        msg = self.agent._user_message(make_ctx([URL + "own"]), "上面这种图", self.me)
        urls = [b["image_url"]["url"] for b in msg["content"] if b["type"] == "image_url"]
        self.assertEqual(urls, [URL + "own"])


class RecallHintTests(unittest.TestCase):
    def test_participation_hint_limits_recall(self):
        bot = SimpleNamespace(config={})
        ctx = SimpleNamespace(is_group=True, event=SimpleNamespace(to_me=False),
                              state={})
        hint = Agent(bot)._participation_hint(ctx)
        self.assertIn("recall_last", hint)
        self.assertIn("平时绝不用它", hint)


if __name__ == "__main__":
    unittest.main()
