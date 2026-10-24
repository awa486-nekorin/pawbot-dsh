"""熔断器：连续失败就跳过主模型，冷却后半开试探。全程离线 + 假时钟。"""
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.breaker import BREAKER, Breaker
from pawbot.llm import LLMClient
from pawbot import vision

KEY = "sk-test-SECRET-do-not-leak"
PRIMARY = "gemini-4-argon"
FALLBACK = "deepseek-v4.1-flash"


class Clock:
    """假时钟：monotonic 完全由测试推进。"""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += float(secs)


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reply(content="好的", finish="stop"):
    return json.dumps({"choices": [{"message": {"role": "assistant",
                                                "content": content},
                                    "finish_reason": finish}]}).encode()


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def base_cfg(**extra):
    cfg = Cfg({
        "llm.base_url": "https://relay.example/v1",
        "llm.api_key": KEY,
        "llm.model": PRIMARY,
        "llm.max_tokens": 600,
        "llm.temperature": 0.8,
        "llm.timeout": 120,
        "llm.first_timeout": 20,
        "llm.reasoning_effort": "medium",
        "llm.fallback_model": FALLBACK,
        "llm.breaker_threshold": 2,
        "llm.breaker_cooldown": 300,
    })
    cfg.update(extra)
    return cfg


MESSAGES = [{"role": "user", "content": "hi"}]


class BreakerCase(unittest.TestCase):
    def setUp(self):
        BREAKER.reset()
        self.clock = Clock()
        self._tp = patch("pawbot.breaker.time.monotonic", self.clock)
        self._tp.start()
        self.addCleanup(self._tp.stop)
        self.addCleanup(BREAKER.reset)


class BreakerStateTests(BreakerCase):
    def test_two_consecutive_failures_open(self):
        BREAKER.record_failure("m")
        self.assertFalse(BREAKER.is_open("m"))
        BREAKER.record_failure("m")
        self.assertTrue(BREAKER.is_open("m"))

    def test_success_clears_counter(self):
        BREAKER.record_failure("m")
        BREAKER.record_success("m")
        self.assertEqual(BREAKER.count("m"), 0)
        BREAKER.record_failure("m")
        self.assertFalse(BREAKER.is_open("m"), "成功之后计数要重新开始")

    def test_success_closes_open_breaker(self):
        BREAKER.record_failure("m")
        BREAKER.record_failure("m")
        self.assertTrue(BREAKER.is_open("m"))
        BREAKER.record_success("m")
        self.assertFalse(BREAKER.is_open("m"))
        self.assertEqual(BREAKER.count("m"), 0)

    def test_threshold_and_cooldown_are_configurable(self):
        BREAKER.configure(1, 30)
        BREAKER.record_failure("m")
        self.assertTrue(BREAKER.is_open("m"))
        self.clock.advance(31)
        self.assertFalse(BREAKER.is_open("m"))

    def test_per_call_threshold_override(self):
        BREAKER.record_failure("m", threshold=1)
        self.assertTrue(BREAKER.is_open("m", cooldown=10))
        self.clock.advance(11)
        self.assertFalse(BREAKER.is_open("m", cooldown=10))

    def test_different_models_are_independent(self):
        BREAKER.record_failure("a")
        BREAKER.record_failure("a")
        self.assertTrue(BREAKER.is_open("a"))
        self.assertFalse(BREAKER.is_open("b"))

    def test_reset(self):
        BREAKER.record_failure("m")
        BREAKER.record_failure("m")
        BREAKER.reset()
        self.assertFalse(BREAKER.is_open("m"))
        self.assertEqual(BREAKER.count("m"), 0)

    def test_half_open_allows_single_probe_then_success_closes(self):
        BREAKER.record_failure("m")
        BREAKER.record_failure("m")
        self.clock.advance(300)
        self.assertFalse(BREAKER.is_open("m"), "冷却到期应放行一次试探")
        self.assertTrue(BREAKER.is_open("m"), "试探还没回来时不该放第二个进去")
        BREAKER.record_success("m")
        self.assertFalse(BREAKER.is_open("m"))

    def test_half_open_failure_recools(self):
        BREAKER.record_failure("m")
        BREAKER.record_failure("m")
        self.clock.advance(300)
        self.assertFalse(BREAKER.is_open("m"))     # 试探放行
        BREAKER.record_failure("m")                # 试探失败
        self.assertTrue(BREAKER.is_open("m"))      # 再冷却一轮
        self.clock.advance(299)
        self.assertTrue(BREAKER.is_open("m"))
        self.clock.advance(1)
        self.assertFalse(BREAKER.is_open("m"))     # 新一轮放行试探

    def test_stuck_probe_expires(self):
        BREAKER.record_failure("m")
        BREAKER.record_failure("m")
        self.clock.advance(300)
        self.assertFalse(BREAKER.is_open("m"))     # 放行试探，但没人回结果
        self.clock.advance(299)
        self.assertTrue(BREAKER.is_open("m"))
        self.clock.advance(1)
        self.assertFalse(BREAKER.is_open("m"), "试探卡死一个冷却周期后要能重试")

    def test_thread_safe_counting(self):
        def hit():
            for _ in range(10):
                BREAKER.record_failure("m")

        threads = [threading.Thread(target=hit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(BREAKER.count("m"), 40)
        self.assertTrue(BREAKER.is_open("m"))


class LLMBreakerTests(BreakerCase):
    def test_open_breaker_skips_primary(self):
        calls = []

        def fake_open(req, timeout=0):
            body = json.loads(req.data.decode())
            calls.append((body["model"], timeout))
            if body["model"] == PRIMARY:
                raise TimeoutError("The read operation timed out")
            return FakeResp(reply("备援回复"))

        client = LLMClient(base_cfg())
        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            self.assertEqual(client._request_message(MESSAGES)["content"], "备援回复")
            self.assertEqual([c[0] for c in calls], [PRIMARY, FALLBACK])
            self.assertEqual(calls[0][1], 20, "主模型只等 first_timeout")

            # 第二次：主模型再超时，连续失败 2 次 → 熔断打开
            calls.clear()
            self.assertEqual(client._request_message(MESSAGES)["content"], "备援回复")
            self.assertEqual([c[0] for c in calls], [PRIMARY, FALLBACK])
            self.assertTrue(BREAKER.is_open(PRIMARY))

            # 第三次：主模型熔断中，直接走备援，一次都不打主模型
            calls.clear()
            self.assertEqual(client._request_message(MESSAGES)["content"], "备援回复")
            self.assertEqual([c[0] for c in calls], [FALLBACK],
                             "熔断期间不该再请求主模型")
            self.assertEqual(calls[0][1], 120, "备援用完整 timeout")

    def test_primary_success_after_failure_keeps_breaker_closed(self):
        seq = [TimeoutError("timed out"), reply("备援"), reply("主模型恢复")]

        def fake_open(req, timeout=0):
            act = seq.pop(0)
            if isinstance(act, Exception):
                raise act
            return FakeResp(act)

        client = LLMClient(base_cfg())
        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            client._request_message(MESSAGES)      # 主超时 → 备援成功
            self.assertEqual(client._request_message(MESSAGES)["content"], "主模型恢复")
        self.assertEqual(BREAKER.count(PRIMARY), 0)
        self.assertFalse(BREAKER.is_open(PRIMARY))

    def test_breaker_threshold_read_from_config(self):
        calls = []

        def fake_open(req, timeout=0):
            body = json.loads(req.data.decode())
            calls.append(body["model"])
            if body["model"] == PRIMARY:
                raise TimeoutError("timed out")
            return FakeResp(reply("ok"))

        client = LLMClient(base_cfg(**{"llm.breaker_threshold": 1}))
        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            client._request_message(MESSAGES)      # 1 次失败就熔断
            calls.clear()
            client._request_message(MESSAGES)
        self.assertEqual(calls, [FALLBACK], "threshold=1 时第二次应直接走备援")

    def test_fallback_failure_also_counts(self):
        def fake_open(req, timeout=0):
            raise TimeoutError("timed out")

        client = LLMClient(base_cfg())
        with patch("pawbot.llm.urllib.request.urlopen", fake_open):
            for _ in range(2):
                with self.assertRaises(Exception):
                    client._request_message(MESSAGES)
        self.assertTrue(BREAKER.is_open(PRIMARY))
        self.assertTrue(BREAKER.is_open(FALLBACK))


class VisionBreakerTests(BreakerCase):
    def setUp(self):
        super().setUp()
        vision._cache.clear()
        self.addCleanup(vision._cache.clear)

    def _describe(self, model=PRIMARY, fallback=FALLBACK):
        seg = {"type": "image", "data": {"file": "/x/a.jpg"}}
        calls = []

        def fake(b64, key, base, name, prompt, timeout=20, budget=900,
                 effort="", retries=1):
            calls.append(name)
            if name == PRIMARY:
                raise TimeoutError("slow")
            return "备援转述"

        with patch.object(vision, "_load_bytes", return_value=b"raw"), \
                patch.object(vision, "_shrink", return_value=(b"shrunk", "jpeg")), \
                patch.object(vision, "_call_vision", side_effect=fake):
            out = vision.describe([seg], "k", "https://relay/v1", model, 12,
                                  "", fallback, 22)
        return out, calls

    def test_two_vision_failures_open_breaker(self):
        for _ in range(2):
            vision._cache.clear()
            self._describe(fallback="")
        self.assertTrue(BREAKER.is_open(PRIMARY))

    def test_vision_skips_open_model(self):
        BREAKER.record_failure(PRIMARY)
        BREAKER.record_failure(PRIMARY)
        self.assertTrue(BREAKER.is_open(PRIMARY))

        out, calls = self._describe()
        self.assertIn("备援转述", out)
        self.assertEqual(calls, [FALLBACK], "熔断中的看图模型要被跳过")

    def test_vision_success_resets_failures(self):
        BREAKER.record_failure(PRIMARY)
        seg = {"type": "image", "data": {"file": "/x/b.jpg"}}
        with patch.object(vision, "_load_bytes", return_value=b"raw"), \
                patch.object(vision, "_shrink", return_value=(b"shrunk", "jpeg")), \
                patch.object(vision, "_call_vision", return_value="主模型转述"):
            out = vision.describe([seg], "k", "https://relay/v1", PRIMARY, 12,
                                  "", FALLBACK, 22)
        self.assertIn("主模型转述", out)
        self.assertEqual(BREAKER.count(PRIMARY), 0)


if __name__ == "__main__":
    unittest.main()
