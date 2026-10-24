"""情绪判官改走中转模型（Gemini）的离线测试：不联网、不读 data/config.json。"""

import asyncio
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import emotion as emotion_mod                      # noqa: E402
from pawbot.emotion import Emotion, _EmotionJudge              # noqa: E402
from pawbot.llm import LLMError                                # noqa: E402
from pawbot.log import log                                     # noqa: E402

RELAY = "https://9527.codes/v1"
CHAT_RELAY = "https://chat.example/v1"
KEY = "sk-unit-test-placeholder"


class FakeConfig:
    """只有 .get(dotted, default)，跟真 Config 的读法一致。"""

    def __init__(self, data=None):
        self.data = data or {}

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PAWBOT_JUDGE_API_KEY", None)
        log.buffer.clear()
        # 固定成白天，避免深夜的「疲惫」混进启发式
        hour = mock.patch("pawbot.emotion._hour", return_value=15)
        hour.start()
        self.addCleanup(hour.stop)

    def llm_cfg(self, **emotion_over):
        emotion = {
            "judge_model": "gemini-3.8-flash",
            "judge_base_url": RELAY,
            "judge_timeout": 10,
            "judge_effort": "low",
        }
        emotion.update(emotion_over)
        return FakeConfig({
            "llm": {"base_url": CHAT_RELAY, "api_key": KEY,
                    "model": "chat-model", "temperature": 0.8,
                    "max_tokens": 900, "reasoning_effort": "medium"},
            "emotion": emotion,
        })

    def run_update(self, cfg, reply='{"开心": 3}', text="嗯", **kw):
        """跑一次 Emotion.update，_post 打桩；返回 (emotion, applied, sent)。"""
        sent = []

        def fake_post(url, payload, key, timeout):
            sent.append({"url": url, "payload": payload,
                         "key": key, "timeout": timeout})
            if isinstance(reply, BaseException):
                raise reply
            return {"choices": [{"message": {"role": "assistant",
                                             "content": reply},
                                 "finish_reason": "stop"}]}

        e = Emotion(None, cfg)
        with mock.patch.object(emotion_mod._EmotionJudge, "_post",
                               side_effect=fake_post):
            applied = asyncio.run(e.update(text, config=cfg, **kw))
        return e, applied, sent


class RoutingTests(Base):
    def test_judge_model_sends_relay_model(self):
        cfg = self.llm_cfg()
        _, applied, sent = self.run_update(cfg)
        self.assertEqual(applied, {"开心": 3})
        self.assertEqual(len(sent), 1)
        req = sent[0]
        self.assertTrue(req["url"].startswith(RELAY), req["url"])
        self.assertEqual(req["url"], RELAY + "/chat/completions")
        self.assertEqual(req["payload"]["model"], "gemini-3.8-flash")
        self.assertEqual(req["payload"]["reasoning_effort"], "low")
        self.assertEqual(req["payload"]["temperature"], 0.2)
        self.assertEqual(req["payload"]["max_tokens"], 1200)
        self.assertEqual(req["timeout"], 10)
        # key 走 llm.api_key，不在 emotion 段落里重复存一份
        self.assertEqual(req["key"], KEY)

    def test_emotion_key_override_still_allowed(self):
        cfg = self.llm_cfg(judge_api_key="judge-only-key")
        _, _, sent = self.run_update(cfg)
        self.assertEqual(sent[0]["key"], "judge-only-key")

    def test_empty_judge_model_keeps_old_channel(self):
        cfg = FakeConfig({
            "llm": {"base_url": CHAT_RELAY, "api_key": KEY, "model": "chat-model"},
            "judge": {"enabled": True, "api_key": "judge-key"},
            "emotion": {"judge_model": "", "judge_timeout": 10},
        })
        fake = mock.AsyncMock(return_value={"烦躁": 9})
        with mock.patch("pawbot.emotion.judge.judge_json", new=fake), \
                mock.patch.object(emotion_mod._EmotionJudge, "_post") as post:
            applied = asyncio.run(Emotion(None, cfg).update("随便", config=cfg))
        self.assertEqual(applied, {"烦躁": 9})
        fake.assert_awaited_once()
        post.assert_not_called()

    def test_only_valid_dims_are_applied(self):
        cfg = self.llm_cfg()
        reply = '好的：\n```json\n{"开心": 3, "不存在": 99, "兴奋": "多"}\n```'
        _, applied, _ = self.run_update(cfg, reply=reply)
        self.assertEqual(applied, {"开心": 3})

    def test_tuning_defaults_without_config(self):
        client = _EmotionJudge(FakeConfig({}))
        self.assertEqual(client._cfg("temperature"), 0.2)
        self.assertEqual(client._cfg("max_tokens"), 1200)
        self.assertEqual(client._cfg("reasoning_effort"), "low")
        self.assertEqual(client._cfg("api_key", ""), "")


class FallbackTests(Base):
    def test_non_json_reply_falls_back_to_heuristic(self):
        cfg = self.llm_cfg()
        _, applied, sent = self.run_update(cfg, reply="我觉得不太像 JSON 呢",
                                           text="你个笨蛋")
        self.assertEqual(len(sent), 1)
        self.assertEqual(applied.get("烦躁"), 14)     # 关键词兜底生效

    def test_judge_error_falls_back_to_heuristic(self):
        cfg = self.llm_cfg()
        out = io.StringIO()
        with redirect_stdout(out):
            _, applied, sent = self.run_update(
                cfg, reply=LLMError(f"boom {KEY}"), text="你个笨蛋")
        self.assertEqual(len(sent), 1)
        self.assertEqual(applied.get("烦躁"), 14)
        # 日志和输出里都不能出现 key
        text = out.getvalue() + "".join(r.get("msg", "") for r in log.buffer)
        self.assertNotIn(KEY, text)

    def test_timeout_falls_back_to_heuristic(self):
        cfg = self.llm_cfg()
        with mock.patch.object(emotion_mod._EmotionJudge, "chat",
                               new=mock.AsyncMock(side_effect=asyncio.TimeoutError)):
            applied = asyncio.run(Emotion(None, cfg).update("你个笨蛋", config=cfg))
        self.assertEqual(applied.get("烦躁"), 14)


if __name__ == "__main__":
    unittest.main(verbosity=2)
