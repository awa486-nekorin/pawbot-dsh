"""思考强度参数：只接受 low/medium/high，其它写法不入 payload。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.llm import LLMClient


class FakeConfig:
    def __init__(self, effort):
        self.effort = effort

    def get(self, key, default=None):
        if key == "llm.reasoning_effort":
            return self.effort if self.effort is not None else default
        return default


class ReasoningEffortTests(unittest.TestCase):
    def payload(self, effort):
        return LLMClient(FakeConfig(effort))._payload(
            [{"role": "user", "content": "hi"}])

    def test_levels_are_forwarded(self):
        for value, expected in (("low", "low"), ("medium", "medium"),
                                ("high", "high"), ("HIGH", "high"), (" medium ", "medium")):
            with self.subTest(value=value):
                self.assertEqual(self.payload(value)["reasoning_effort"], expected)

    def test_unknown_or_empty_is_omitted(self):
        for value in ("", "off", "disabled", "garbage"):
            with self.subTest(value=value):
                self.assertNotIn("reasoning_effort", self.payload(value))

    def test_default_is_medium(self):
        self.assertEqual(self.payload(None)["reasoning_effort"], "medium")

    def test_tools_payload_keeps_effort(self):
        payload = LLMClient(FakeConfig("medium"))._payload(
            [{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        self.assertEqual(payload["reasoning_effort"], "medium")
        self.assertEqual(payload["tool_choice"], "auto")


class OtherCallPathsTests(unittest.TestCase):
    """看图、联网搜索也走思考型模型，强度不能漏。"""

    def test_vision_payload_carries_effort(self):
        import json
        from unittest.mock import patch
        from pawbot import vision

        seen = {}

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "猫"},
                                                "finish_reason": "stop"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def boom(req, timeout=0):
            seen["body"] = json.loads(req.data.decode())
            return FakeResp()

        with patch("pawbot.vision.urllib.request.urlopen", boom):
            out = vision._call_vision("AAAA", "key", "http://x", "deepseek-flash",
                                      "讲图", effort="high")
        self.assertEqual(out, "猫")
        self.assertEqual(seen["body"]["reasoning_effort"], "high")

    def test_vision_omits_unknown_effort(self):
        import json
        from unittest.mock import patch
        from pawbot import vision

        seen = {}

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "猫"},
                                                "finish_reason": "stop"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def boom(req, timeout=0):
            seen["body"] = json.loads(req.data.decode())
            return FakeResp()

        with patch("pawbot.vision.urllib.request.urlopen", boom):
            vision._call_vision("AAAA", "key", "http://x", "m", "讲图", effort="off")
        self.assertNotIn("reasoning_effort", seen["body"])

    def test_search_payload_carries_effort(self):
        import json
        from unittest.mock import patch
        from pawbot import websearch

        seen = {}

        def fake_open(url, **kw):
            seen["body"] = json.loads(kw["data"].decode())
            return json.dumps({"output_text": "答案", "output": []})

        with patch.object(websearch, "_open", fake_open):
            out = websearch.search_deepseek("今天新闻", "key", "http://x",
                                            reasoning_effort="medium")
        self.assertEqual(seen["body"]["reasoning_effort"], "medium")
        self.assertEqual(out["answer"], "答案")

    def test_search_omits_unknown_effort(self):
        import json
        from unittest.mock import patch
        from pawbot import websearch

        seen = {}

        def fake_open(url, **kw):
            seen["body"] = json.loads(kw["data"].decode())
            return json.dumps({"output_text": "答案", "output": []})

        with patch.object(websearch, "_open", fake_open):
            websearch.search_deepseek("今天新闻", "key", "http://x",
                                      reasoning_effort="disabled")
        self.assertNotIn("reasoning_effort", seen["body"])

    def test_search_reads_config_effort(self):
        import json
        from unittest.mock import patch
        from pawbot import websearch

        seen = {}

        def fake_open(url, **kw):
            seen["body"] = json.loads(kw["data"].decode())
            return json.dumps({"output_text": "答案", "output": []})

        class Cfg:
            def get(self, key, default=None):
                values = {"tools.search_provider": "deepseek_responses",
                          "llm.api_key": "key",
                          "tools.search_reasoning_effort": "high",
                          "tools.search_base_url": "http://x"}
                return values.get(key, default)

        with patch.object(websearch, "_open", fake_open):
            websearch.search("今天新闻", Cfg())
        self.assertEqual(seen["body"]["reasoning_effort"], "high")


class VisionRoutingTests(unittest.IsolatedAsyncioTestCase):
    """会话走中转、看图仍走 DeepSeek 时，凭据不能串。"""

    class Cfg(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    def run_describe(self, cfg):
        from unittest.mock import patch
        from pawbot import perceive, vision

        seen = {}

        def fake(raws, key, base, model, timeout=30, effort="", *args, **kw):
            seen.update(key=key, base=base, model=model, effort=effort)
            return ""

        with patch.object(perceive, "_image_bytes", return_value=[b"raw"]), \
                patch.object(vision, "see", fake):
            import asyncio
            asyncio.run(perceive._describe_images([{"type": "image", "data": {}}], cfg))
        return seen

    def test_vision_uses_its_own_credentials(self):
        seen = self.run_describe(self.Cfg({
            "llm.api_key": "chat-key", "llm.base_url": "https://relay/v1",
            "llm.model": "gemini-3.8-flash",
            "llm.vision_api_key": "deepseek-key",
            "llm.vision_base_url": "https://api.deepseek.com/v1",
            "llm.vision_model": "deepseek-flash",
            "llm.reasoning_effort": "medium",
        }))
        self.assertEqual(seen["key"], "deepseek-key")
        self.assertEqual(seen["base"], "https://api.deepseek.com/v1")
        self.assertEqual(seen["model"], "deepseek-flash")
        self.assertEqual(seen["effort"], "medium")

    def test_vision_falls_back_to_chat_credentials(self):
        seen = self.run_describe(self.Cfg({
            "llm.api_key": "chat-key", "llm.base_url": "https://relay/v1",
            "llm.model": "gemini-3.8-flash",
            "llm.reasoning_effort": "high",
        }))
        self.assertEqual(seen["key"], "chat-key")
        self.assertEqual(seen["base"], "https://relay/v1")
        self.assertEqual(seen["model"], "gemini-3.8-flash")
        self.assertEqual(seen["effort"], "high")


if __name__ == "__main__":
    unittest.main()
