"""主聊天模型备援：超时/5xx 换模型重发，400 不动，日志不泄 key。"""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.breaker import BREAKER
from pawbot.llm import LLMClient, LLMError

KEY = "sk-test-SECRET-do-not-leak"
URL = "https://relay.example/v1/chat/completions"


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def base_cfg(**extra):
    cfg = Cfg({
        "llm.base_url": "https://relay.example/v1",
        "llm.api_key": KEY,
        "llm.model": "gemini-4-argon",
        "llm.max_tokens": 600,
        "llm.temperature": 0.8,
        "llm.timeout": 120,
        "llm.first_timeout": 40,
        "llm.reasoning_effort": "medium",
        "llm.fallback_model": "deepseek-v4.1-flash",
    })
    cfg.update(extra)
    return cfg


def reply(content="好的", finish="stop"):
    return json.dumps({"choices": [{"message": {"role": "assistant",
                                                "content": content},
                                    "finish_reason": finish}]}).encode()


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=b'{"error":"x"}'):
    import io
    return urllib.error.HTTPError(URL, code, "err", {}, io.BytesIO(body))


TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
MESSAGES = [{"role": "user", "content": "hi"}]


class FallbackTests(unittest.TestCase):
    def setUp(self):
        BREAKER.reset()          # 熔断器是全局状态，别让上一个用例影响下一个

    def test_timeout_switches_to_fallback(self):
        seen = {}
        actions = [TimeoutError("The read operation timed out"), reply("备援到了")]

        def fake_open(req, timeout=0):
            body = json.loads(req.data.decode())
            seen.setdefault("calls", []).append(
                {"model": body["model"], "timeout": timeout,
                 "effort": body.get("reasoning_effort"),
                 "tools": "tools" in body})
            act = actions[len(seen["calls"]) - 1]
            if isinstance(act, Exception):
                raise act
            return FakeResp(act)

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            out = LLMClient(base_cfg())._request_message(MESSAGES, TOOLS)

        self.assertEqual(out["content"], "备援到了")
        self.assertEqual([c["model"] for c in seen["calls"]],
                         ["gemini-4-argon", "deepseek-v4.1-flash"])
        # 主模型用 first_timeout，备援用 timeout
        self.assertEqual(seen["calls"][0]["timeout"], 40)
        self.assertEqual(seen["calls"][1]["timeout"], 120)
        # 同一份 payload：effort 和 tools 都要照带
        self.assertEqual(seen["calls"][1]["effort"], "medium")
        self.assertTrue(seen["calls"][1]["tools"])

    def test_503_switches_to_fallback(self):
        models = []

        def fake_open(req, timeout=0):
            body = json.loads(req.data.decode())
            models.append(body["model"])
            if len(models) == 1:
                raise http_error(503, b'{"error":{"message":"upstream pool_limit"}}')
            return FakeResp(reply("收到"))

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            out = LLMClient(base_cfg())._request_message(MESSAGES, TOOLS)

        self.assertEqual(out["content"], "收到")
        self.assertEqual(models, ["gemini-4-argon", "deepseek-v4.1-flash"])

    def test_429_switches_to_fallback(self):
        models = []

        def fake_open(req, timeout=0):
            models.append(json.loads(req.data.decode())["model"])
            if len(models) == 1:
                raise http_error(429)
            return FakeResp(reply("限流也切"))

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            out = LLMClient(base_cfg())._request_message(MESSAGES)
        self.assertEqual(out["content"], "限流也切")
        self.assertEqual(len(models), 2)

    def test_400_does_not_switch(self):
        models = []

        def fake_open(req, timeout=0):
            models.append(json.loads(req.data.decode())["model"])
            raise http_error(400, b'{"error":"bad request"}')

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            with self.assertRaises(LLMError) as ctx:
                LLMClient(base_cfg())._request_message(MESSAGES, TOOLS)

        self.assertEqual(len(models), 1, "400 是请求本身有问题，不该换模型重发")
        self.assertIn("400", str(ctx.exception))

    def test_no_fallback_raises_llm_error(self):
        models = []

        def fake_open(req, timeout=0):
            models.append(json.loads(req.data.decode())["model"])
            raise TimeoutError("timed out")

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            with self.assertRaises(LLMError):
                LLMClient(base_cfg(**{"llm.fallback_model": ""})) \
                    ._request_message(MESSAGES)
        self.assertEqual(len(models), 1)

    def test_fallback_same_as_primary_is_ignored(self):
        models = []

        def fake_open(req, timeout=0):
            models.append(json.loads(req.data.decode())["model"])
            raise TimeoutError("timed out")

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            with self.assertRaises(LLMError):
                LLMClient(base_cfg(**{"llm.fallback_model": "gemini-4-argon"})) \
                    ._request_message(MESSAGES)
        self.assertEqual(len(models), 1, "备援和主模型同名时不该重发")

    def test_length_doubles_budget(self):
        budgets = []

        def fake_open(req, timeout=0):
            body = json.loads(req.data.decode())
            budgets.append(body["max_tokens"])
            if len(budgets) == 1:
                return FakeResp(reply("", "length"))
            return FakeResp(reply("加预算后成功"))

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            out = LLMClient(base_cfg())._request_message(MESSAGES, TOOLS)
        self.assertEqual(out["content"], "加预算后成功")
        self.assertEqual(budgets, [600, 1200])

    def test_length_doubling_applies_to_fallback(self):
        seen = []
        seq = [TimeoutError("timeout"),
               reply("", "length"),
               reply("备援加预算成功")]

        def fake_open(req, timeout=0):
            body = json.loads(req.data.decode())
            seen.append((body["model"], body["max_tokens"]))
            act = seq[len(seen) - 1]
            if isinstance(act, Exception):
                raise act
            return FakeResp(act)

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            out = LLMClient(base_cfg())._request_message(MESSAGES, TOOLS)
        self.assertEqual(out["content"], "备援加预算成功")
        self.assertEqual(seen, [("gemini-4-argon", 600),
                                ("deepseek-v4.1-flash", 600),
                                ("deepseek-v4.1-flash", 1200)])

    def test_first_timeout_zero_uses_timeout(self):
        timeouts = []

        def fake_open(req, timeout=0):
            body = json.loads(req.data.decode())
            timeouts.append(timeout)
            if len(timeouts) == 1:
                return FakeResp(reply("", "length"))
            return FakeResp(reply("ok"))

        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            LLMClient(base_cfg(**{"llm.first_timeout": 0})) \
                ._request_message(MESSAGES)
        self.assertEqual(timeouts, [120, 120])

    def test_log_never_contains_api_key(self):
        import io

        def fake_open(req, timeout=0):
            raise TimeoutError("timed out")

        buf = io.StringIO()
        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            from pawbot.log import log
            with patch.object(log, "_emit",
                              lambda level, msg, **f: buf.write(str(msg) + "\n")):
                with self.assertRaises(LLMError):
                    LLMClient(base_cfg())._request_message(MESSAGES)
        text = buf.getvalue()
        self.assertIn("gemini-4-argon", text)
        self.assertIn("deepseek-v4.1-flash", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("SECRET", text)


if __name__ == "__main__":
    unittest.main()
