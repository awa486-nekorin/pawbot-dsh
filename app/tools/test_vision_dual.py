"""多模态双通道：Gemini 详细转述 + 原图一起给主模型看。离线。"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import perceive, vision
from pawbot.agent import Agent
from pawbot.breaker import BREAKER

URL = "data:image/jpeg;base64,AAAA"


class VisionKeepTests(unittest.TestCase):
    def setUp(self):
        vision._cache.clear()
        BREAKER.reset()          # 熔断器是全局状态，别让上一个用例影响下一个

    def test_collect_images_returns_data_urls(self):
        seg = {"type": "image", "data": {"file": "/x/a.jpg"}}
        with patch.object(vision, "_load_bytes", return_value=b"raw"), \
                patch.object(vision, "_shrink", return_value=(b"shrunk", "jpeg")):
            urls = vision.collect_images([seg])
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("data:image/jpeg;base64,"))

    def test_describe_falls_back_to_second_model(self):
        calls = []

        def fake(b64, key, base, model, prompt, timeout=20, budget=900,
                 effort="", retries=1):
            calls.append(model)
            if model == "gemini-4-argon":
                raise TimeoutError("slow")
            return "备用模型写的转述"

        seg = {"type": "image", "data": {"file": "/x/a.jpg"}}
        with patch.object(vision, "_load_bytes", return_value=b"raw"), \
                patch.object(vision, "_shrink", return_value=(b"shrunk", "jpeg")), \
                patch.object(vision, "_call_vision", side_effect=fake):
            out = vision.describe([seg], "k", "https://relay/v1", "gemini-4-argon",
                                  20, "", "gemini-3.8-flash", 20)
        self.assertIn("备用模型写的转述", out)
        self.assertEqual(calls, ["gemini-4-argon", "gemini-3.8-flash"])

    def test_describe_gives_up_without_fallback(self):
        seg = {"type": "image", "data": {"file": "/x/a.jpg"}}
        with patch.object(vision, "_load_bytes", return_value=b"raw"), \
                patch.object(vision, "_shrink", return_value=(b"shrunk", "jpeg")), \
                patch.object(vision, "_call_vision", side_effect=TimeoutError("slow")):
            out = vision.describe([seg], "k", "https://relay/v1", "gemini-4-argon", 20)
        self.assertEqual(out, "[图片]")

    def test_without_keep_returns_string(self):
        seg = {"type": "image", "data": {"file": "/x/a.jpg"}}
        with patch.object(vision, "_load_bytes", return_value=b"raw"), \
                patch.object(vision, "_shrink", return_value=(b"shrunk", "jpeg")), \
                patch.object(vision, "_call_vision", return_value="描述"):
            out = vision.describe([seg], "k", "https://relay/v1", "m", 30)
        self.assertIsInstance(out, str)

    def test_no_key_returns_empty(self):
        self.assertEqual(vision.describe([{"type": "image", "data": {}}], "",
                                         "", "", 5), "")

    def test_prompt_asks_for_detail(self):
        self.assertIn("尽可能详细", vision.PROMPT)
        self.assertIn("照抄原文", vision.PROMPT)


class PerceiveTests(unittest.TestCase):
    def test_describe_images_passes_urls_through(self):
        cfg = SimpleNamespace(get=lambda k, d=None: {
            "llm.vision_api_key": "k", "llm.vision_base_url": "https://relay/v1",
            "llm.vision_model": "gemini-4-argon", "llm.vision_timeout": 30,
        }.get(k, d))
        with patch.object(perceive, "_image_bytes", return_value=[b"raw"]), \
                patch.object(vision, "preview_urls", return_value=[URL]), \
                patch.object(vision, "see", return_value="转述"):
            import asyncio
            text, urls = asyncio.run(perceive._describe_images(
                [{"type": "image", "data": {}}], cfg))
        self.assertEqual(text, "转述")
        self.assertEqual(urls, [URL])

    def test_image_is_kept_when_transcription_fails(self):
        cfg = SimpleNamespace(get=lambda k, d=None: {
            "llm.vision_api_key": "k", "llm.vision_base_url": "https://relay/v1",
            "llm.vision_model": "gemini-4-argon", "llm.vision_timeout": 30,
        }.get(k, d))
        with patch.object(perceive, "_image_bytes", return_value=[b"raw"]), \
                patch.object(vision, "preview_urls", return_value=[URL]), \
                patch.object(vision, "see", side_effect=RuntimeError("503")):
            import asyncio
            text, urls = asyncio.run(perceive._describe_images(
                [{"type": "image", "data": {}}], cfg))
        self.assertEqual(text, "")
        self.assertEqual(urls, [URL])       # 转述失败，原图照样给模型


def make_ctx(image_data=None, is_group=True):
    return SimpleNamespace(
        is_group=is_group, sender_name="阿明", user_id=123456,
        session_id="group_1", group_id=1, state={},
        event=SimpleNamespace(image_data=image_data or [], to_me=False,
                              quoted_text="", image_desc="", person_card=""))


class UserMessageTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent.__new__(Agent)
        self.agent.bot = SimpleNamespace(config={})
        self.me = {"role": "user", "who": "阿明", "uid": "123456"}

    def test_text_only(self):
        msg = self.agent._user_message(make_ctx(), "在吗", self.me)
        self.assertEqual(msg["content"], "阿明(尾号3456)：在吗")

    def test_image_is_attached_with_text(self):
        msg = self.agent._user_message(make_ctx([URL]), "这图啥意思", self.me)
        blocks = msg["content"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("这图啥意思", blocks[0]["text"])
        self.assertEqual(blocks[1], {"type": "image_url",
                                     "image_url": {"url": URL}})

    def test_at_most_four_images(self):
        urls = [URL + str(i) for i in range(5)]
        msg = self.agent._user_message(make_ctx(urls), "五张", self.me)
        self.assertEqual(sum(1 for b in msg["content"] if b["type"] == "image_url"), 4)

    def test_private_chat_text_has_no_name_prefix(self):
        msg = self.agent._user_message(make_ctx(is_group=False), "在吗", self.me)
        self.assertEqual(msg["content"], "在吗")


class SenseHintTests(unittest.TestCase):
    def test_hint_tells_model_to_check_image_first(self):
        bot = SimpleNamespace(config={})
        agent = Agent(bot)
        agent.bot.topic = SimpleNamespace(digest=lambda *a, **k: "")
        ctx = make_ctx([URL])
        ctx.event.image_desc = "一只猫"
        ctx.event.quoted_text = ""
        hint = agent._sense_hint(ctx)
        self.assertIn("原图也一并发给你了", hint)
        self.assertIn("以原图为准", hint)


if __name__ == "__main__":
    unittest.main()
