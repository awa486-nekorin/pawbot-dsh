"""决策模型（类 JEV）测试：请求格式 / 响应解析 / 情绪映射 / key 解析 / 失败降级。

    py -m unittest tools.test_decider -q

全程离线：用本机 http.server 假装 OpenRouter decisions 端点。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import decider                              # noqa: E402
from pawbot.config import DEFAULT                       # noqa: E402

GOOD_ANSWERS = {
    "shift": {"type": "noul", "noul": 0.91},
    "emotion": {
        "type": "choice",
        "choice": "低落",
        "confidence": 0.86,
        "probabilities": {"低落": 0.86, "开心": 0.05, "none": 0.09},
    },
    "intensity": {"type": "score", "score": 0.82, "legend": {"0": "轻微", "1": "明显", "2": "强烈"}},
}


class Cfg(dict):
    def __init__(self, emotion=None):
        super().__init__(copy.deepcopy(DEFAULT))
        self["emotion"].update(emotion or {})

    def get(self, key, default=None):
        node = self
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key, value):
        parts = str(key).split(".")
        node = self
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value


class FakeDecisions(BaseHTTPRequestHandler):
    requests: list[dict] = []
    response = {"answers": GOOD_ANSWERS, "model": "perplexity/pplx-decider-v1.2", "id": "gen-test"}
    status = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "body": body,
        })
        if type(self).status != 200:
            self.send_response(type(self).status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"nope"}}')
            return
        payload = json.dumps(type(self).response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class DeciderBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDecisions)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/alpha/decisions"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeDecisions.requests = []
        FakeDecisions.response = {"answers": GOOD_ANSWERS}
        FakeDecisions.status = 200
        self.cfg = Cfg({
            "decider_enabled": True,
            "decider_model": "perplexity/pplx-decider-v1.2",
            "decider_base_url": self.url,
            "decider_api_key": "test-key",
        })

    def run_async(self, coro):
        return asyncio.run(coro)


class RequestTests(DeciderBase):
    def test_request_shape_and_auth(self):
        answers = self.run_async(decider.decide(self.cfg, "她被人夸可爱", decider.mood_questions()))
        self.assertEqual(answers["emotion"]["choice"], "低落")
        req = FakeDecisions.requests[0]
        self.assertEqual(req["path"], "/alpha/decisions")
        self.assertEqual(req["auth"], "Bearer test-key")
        self.assertEqual(req["body"]["model"], "perplexity/pplx-decider-v1.2")
        self.assertIn("她被人夸可爱", req["body"]["state"])
        q = req["body"]["questions"]
        self.assertEqual(q["shift"]["type"], "noul")
        self.assertIn("低落", q["emotion"]["criteria"])
        self.assertEqual(q["intensity"]["type"], "score")
        self.assertEqual(len(q["intensity"]["criteria"]), 3)

    def test_http_error_raises_code_without_key(self):
        FakeDecisions.status = 500
        with self.assertRaises(decider.DeciderError) as ctx:
            self.run_async(decider.decide(self.cfg, "x", decider.mood_questions()))
        self.assertEqual(ctx.exception.code, "HTTP_500")
        self.assertNotIn("test-key", str(ctx.exception))

    def test_missing_key_raises(self):
        cfg = Cfg({"decider_base_url": self.url})
        old_files, old_cred = decider._KEY_FILES, decider._CRED_PATHS
        old_env = {k: os.environ.pop(k, None) for k in ("PAWBOT_DECIDER_KEY", "OPENROUTER_API_KEY", "PAWBOT_DECIDER_KEY_FILE")}
        decider._KEY_FILES = ()
        decider._CRED_PATHS = ()
        try:
            with self.assertRaises(decider.DeciderError) as ctx:
                self.run_async(decider.decide(cfg, "x", decider.mood_questions()))
            self.assertEqual(ctx.exception.code, "NO_KEY")
        finally:
            decider._KEY_FILES = old_files
            decider._CRED_PATHS = old_cred
            for k, v in old_env.items():
                if v is not None:
                    os.environ[k] = v


class MappingTests(unittest.TestCase):
    def test_negative_choice(self):
        deltas = decider.deltas_from_answers(GOOD_ANSWERS)
        self.assertGreater(deltas["低落"], 5)
        self.assertLess(deltas.get("开心", 0), 0)

    def test_positive_choice(self):
        answers = copy.deepcopy(GOOD_ANSWERS)
        answers["emotion"]["choice"] = "感动"
        answers["intensity"]["score"] = 1.0
        deltas = decider.deltas_from_answers(answers, weight=1.0)
        self.assertGreaterEqual(deltas["感动"], 10)
        self.assertLess(deltas.get("烦躁", 0), 0)

    def test_low_shift_skips(self):
        answers = copy.deepcopy(GOOD_ANSWERS)
        answers["shift"] = {"type": "noul", "noul": 0.2}
        self.assertEqual(decider.deltas_from_answers(answers), {})

    def test_none_choice_skips(self):
        answers = copy.deepcopy(GOOD_ANSWERS)
        answers["emotion"]["choice"] = "none"
        self.assertEqual(decider.deltas_from_answers(answers), {})

    def test_weight_scales(self):
        low = decider.deltas_from_answers(GOOD_ANSWERS, weight=0.5)
        high = decider.deltas_from_answers(GOOD_ANSWERS, weight=1.5)
        self.assertGreater(high["低落"], low["低落"])

    def test_legend_or_probabilities_fallback(self):
        # 没有 score 时用 probabilities 期望
        answer = {"probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}}
        self.assertAlmostEqual(decider._score(answer), 1.0, places=3)


class MoodIntegrationTests(DeciderBase):
    def test_disabled_returns_none(self):
        cfg = Cfg({"decider_enabled": False, "decider_base_url": self.url, "decider_api_key": "k"})
        out = self.run_async(decider.decide_mood(cfg, "x"))
        self.assertIsNone(out)
        self.assertEqual(FakeDecisions.requests, [])

    def test_enabled_returns_deltas(self):
        out = self.run_async(decider.decide_mood(self.cfg, "群友：今天好累啊", "上下文", True))
        self.assertIsNotNone(out)
        self.assertGreater(out["低落"], 0)

    def test_failure_returns_none_not_raise(self):
        FakeDecisions.status = 500
        out = self.run_async(decider.decide_mood(self.cfg, "x"))
        self.assertIsNone(out)


class KeyResolutionTests(unittest.TestCase):
    def test_config_key_wins(self):
        cfg = Cfg({"decider_api_key": "from-config"})
        self.assertEqual(decider.resolve_key(cfg), "from-config")

    def test_env_key(self):
        cfg = Cfg({"decider_api_key": ""})
        old = os.environ.get("PAWBOT_DECIDER_KEY")
        os.environ["PAWBOT_DECIDER_KEY"] = "from-env"
        try:
            self.assertEqual(decider.resolve_key(cfg), "from-env")
        finally:
            if old is None:
                os.environ.pop("PAWBOT_DECIDER_KEY", None)
            else:
                os.environ["PAWBOT_DECIDER_KEY"] = old

    def test_key_file(self):
        cfg = Cfg({"decider_api_key": ""})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decider.key"
            path.write_text("from-file\n", encoding="utf-8")
            old = os.environ.get("PAWBOT_DECIDER_KEY_FILE")
            os.environ["PAWBOT_DECIDER_KEY_FILE"] = str(path)
            try:
                self.assertEqual(decider.resolve_key(cfg), "from-file")
            finally:
                if old is None:
                    os.environ.pop("PAWBOT_DECIDER_KEY_FILE", None)
                else:
                    os.environ["PAWBOT_DECIDER_KEY_FILE"] = old

    def test_no_key(self):
        cfg = Cfg({"decider_api_key": ""})
        old_env = {k: os.environ.pop(k, None) for k in ("PAWBOT_DECIDER_KEY", "OPENROUTER_API_KEY")}
        old_file = os.environ.pop("PAWBOT_DECIDER_KEY_FILE", None)
        old_paths = decider._KEY_FILES
        decider._KEY_FILES = ()
        try:
            # 把 credentials 路径也临时置空，避免读到开发机上的真 key
            old_cred = decider._CRED_PATHS
            decider._CRED_PATHS = ()
            try:
                self.assertEqual(decider.resolve_key(cfg), "")
            finally:
                decider._CRED_PATHS = old_cred
        finally:
            decider._KEY_FILES = old_paths
            for k, v in old_env.items():
                if v is not None:
                    os.environ[k] = v
            if old_file is not None:
                os.environ["PAWBOT_DECIDER_KEY_FILE"] = old_file


if __name__ == "__main__":
    unittest.main(verbosity=2)
