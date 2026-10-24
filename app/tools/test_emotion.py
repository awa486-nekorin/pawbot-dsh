"""情绪矩阵离线测试：不联网、不读 data/config.json，时间与判官全部打桩。"""

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.emotion import DIMS, Emotion          # noqa: E402
from pawbot.judge import JudgeLLM, judge_enabled, judge_json   # noqa: E402


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
        # 不让宿主机上的真 key 影响 key 优先级测试
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PAWBOT_JUDGE_API_KEY", None)
        self.off = FakeConfig({"judge": {"enabled": False}})


class HeuristicTests(Base):
    def test_praise_raises_happy_and_proud(self):
        e = Emotion(None, self.off)
        with mock.patch("pawbot.emotion._hour", return_value=15):
            deltas = e.heuristic_deltas("你好可爱，真厉害", addressed=True)
        self.assertGreater(deltas["开心"], 0)
        self.assertGreater(deltas["得意"], 0)
        e.apply(deltas)

        self.assertIn(e.blend()["dominant"], ("开心", "得意"))
        hint = e.hint()
        self.assertTrue("活泼" in hint or "傲娇" in hint, hint)

    def test_repeated_insults_dominate_annoyance(self):
        e = Emotion(None, self.off)
        with mock.patch("pawbot.emotion._hour", return_value=15):
            for _ in range(5):
                asyncio.run(e.update("你个傻X人机", config=self.off))
        self.assertEqual(e.blend()["dominant"], "烦躁")
        hint = e.hint()
        self.assertIn("短句", hint)
        self.assertIn("不攻击外貌/家庭", hint)


    def test_repeated_praise_moves_a_visible_amount(self):
        e = Emotion(None, self.off)
        with mock.patch("pawbot.emotion._hour", return_value=15):
            for _ in range(3):
                e.apply(e.heuristic_deltas("你好可爱，好棒", addressed=False))
        self.assertGreaterEqual(e.values()["开心"], 70)
        self.assertGreaterEqual(e.values()["得意"], 65)


class ValueTests(Base):
    def test_single_step_cap_and_bounds(self):
        e = Emotion(None, self.off)
        applied = e.apply({"开心": 80, "乱写的": 10, "兴奋": "多"})
        self.assertEqual(applied, {"开心": 15})
        self.assertAlmostEqual(e.values()["开心"], 65.0, places=2)

        for _ in range(10):
            e.apply({"兴奋": 15})
        self.assertEqual(e.values()["兴奋"], 100.0)

        for _ in range(10):
            e.apply({"低落": -15})
        self.assertEqual(e.values()["低落"], 0.0)

        for name, value in e.values().items():
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 100.0, name)

    def test_turn_regresses_without_overshoot(self):
        e = Emotion(None, self.off)
        e._values["开心"] = 60.0
        e._values["低落"] = 49.0
        e.turn()
        self.assertAlmostEqual(e.values()["开心"], 58.0, places=2)
        self.assertEqual(e.values()["低落"], 50.0)

        e2 = Emotion(None, FakeConfig({"emotion": {"turn_regress": 3}}))
        e2._values["好奇"] = 60.0
        e2.turn()
        self.assertAlmostEqual(e2.values()["好奇"], 57.0, places=2)

    def test_long_idle_returns_to_baseline(self):
        base = 1_700_000_000.0
        with mock.patch("pawbot.emotion._now", return_value=base):
            e = Emotion(None, self.off)
            e.apply({d: 15 for d in DIMS})
            e.apply({d: 15 for d in DIMS})
            self.assertTrue(all(abs(v - 50) > 20 for v in e.values().values()))

        with mock.patch("pawbot.emotion._now", return_value=base + 12 * 3600):
            e.decay()
            vals = e.values()
        for dim in DIMS:
            self.assertAlmostEqual(vals[dim], 50.0, delta=2.0, msg=dim)

    def test_hint_has_no_numbers_and_calm_is_neutral(self):
        calm = Emotion(None, self.off).hint()
        self.assertIn("平常", calm)

        cases = [{"开心": 15}, {"烦躁": 15}, {"低落": 15}, {"亲昵": 15},
                 {"疲惫": 15}, {"好奇": 15}, {"得意": 15}, {"兴奋": 15},
                 {"开心": 15, "亲昵": 9}, {d: 15 for d in DIMS}]
        for deltas in cases:
            e = Emotion(None, self.off)
            e.apply(deltas)
            hint = e.hint()
            self.assertIsNone(re.search(r"\d", hint), (deltas, hint))
            self.assertNotIn("分", hint, (deltas, hint))


class JudgePathTests(Base):
    def config(self):
        return FakeConfig({
            "judge": {"enabled": True, "api_key": "test-key"},
            "emotion": {"judge_timeout": 1.0},
        })

    def test_invalid_fields_are_dropped(self):
        cfg = self.config()
        e = Emotion(None, cfg)
        fake = mock.AsyncMock(
            return_value={"烦躁": 12, "unknown": 5, "开心": "x"})
        with mock.patch("pawbot.judge.judge_json", new=fake):
            applied = asyncio.run(e.update("随便说点", config=cfg))
        self.assertEqual(applied, {"烦躁": 12})
        self.assertAlmostEqual(e.values()["烦躁"], 62.0, delta=0.5)
        self.assertAlmostEqual(e.values()["开心"], 50.0, delta=0.5)
        fake.assert_awaited_once()

    def test_none_falls_back_to_heuristic(self):
        cfg = self.config()
        e = Emotion(None, cfg)
        with mock.patch("pawbot.emotion._hour", return_value=15), \
                mock.patch("pawbot.judge.judge_json",
                           new=mock.AsyncMock(return_value=None)):
            applied = asyncio.run(e.update("你个傻X人机", config=cfg))
        self.assertIn("烦躁", applied)
        self.assertGreater(applied["烦躁"], 0)

    def test_judge_exception_never_raises(self):
        cfg = self.config()
        e = Emotion(None, cfg)
        with mock.patch("pawbot.emotion._hour", return_value=15), \
                mock.patch("pawbot.judge.judge_json",
                           new=mock.AsyncMock(side_effect=RuntimeError("boom"))):
            applied = asyncio.run(e.update("你好", config=cfg))
        self.assertIsInstance(applied, dict)


class PersistenceTests(Base):
    def test_round_trip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "emotion.json")
            base = 1_700_000_000.0
            with mock.patch("pawbot.emotion._now", return_value=base):
                e = Emotion(path, self.off)
                e.apply({"开心": 10})
                e.save()

                raw = json.loads(Path(path).read_text(encoding="utf-8"))
                self.assertEqual(raw["values"]["开心"], 60.0)
                self.assertEqual(raw["updated"], base)
                self.assertFalse(os.path.exists(path + ".tmp"))

                again = Emotion(path, self.off)
                self.assertAlmostEqual(again.values()["开心"], 60.0, places=3)

            Path(path).write_text("{坏掉的 json", encoding="utf-8")
            broken = Emotion(path, self.off)
            self.assertTrue(all(v == 50.0 for v in broken.values().values()))

            Path(path).write_text(json.dumps({"values": {"开心": 999}}),
                                  encoding="utf-8")
            weird = Emotion(path, self.off)
            self.assertEqual(weird.values()["开心"], 100.0)
            self.assertEqual(weird.values()["低落"], 50.0)


class JudgeClientTests(Base):
    def test_key_resolution_order(self):
        base = {"llm": {"api_key": "llm-key", "model": "chat-m",
                        "base_url": "https://chat.example/v1"}}
        chat_client = JudgeLLM(FakeConfig(base))
        self.assertEqual(chat_client._cfg("api_key"), "llm-key")
        self.assertEqual(chat_client._cfg("model"), "chat-m")
        with mock.patch.dict(os.environ,
                             {"PAWBOT_JUDGE_API_KEY": "env-key"}):
            self.assertEqual(chat_client._cfg("api_key"), "env-key")

        rich = FakeConfig({**base, "judge": {
            "api_key": "judge-key", "model": "judge-m",
            "base_url": "https://judge.example/v1"}})
        judge_client = JudgeLLM(rich)
        self.assertEqual(judge_client._cfg("api_key"), "judge-key")
        self.assertEqual(judge_client._cfg("model"), "judge-m")
        self.assertEqual(judge_client._cfg("base_url"),
                         "https://judge.example/v1")

    def test_tuning_defaults(self):
        client = JudgeLLM(FakeConfig({}))
        self.assertEqual(client._cfg("temperature"), 0.2)
        self.assertEqual(client._cfg("max_tokens"), 600)
        self.assertEqual(client._cfg("timeout"), 20)
        self.assertEqual(client._cfg("reasoning_effort"), "low")

        payload = client._payload([{"role": "user", "content": "hi"}])
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["max_tokens"], 600)
        self.assertEqual(payload["reasoning_effort"], "low")

    def test_judge_enabled(self):
        self.assertTrue(judge_enabled(FakeConfig({"judge": {"api_key": "k"}})))
        self.assertTrue(judge_enabled(FakeConfig({"llm": {"api_key": "k"}})))
        self.assertFalse(judge_enabled(FakeConfig({"judge": {"enabled": True}})))
        self.assertFalse(judge_enabled(FakeConfig(
            {"judge": {"enabled": False, "api_key": "k"}})))


class JudgeJsonTests(Base):
    def test_parses_fenced_json_and_sends_png(self):
        cfg = FakeConfig({"judge": {"api_key": "test-key"}})
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4
        fake = mock.AsyncMock(return_value="好的：\n```json\n{\"开心\": 3}\n```")
        with mock.patch.object(JudgeLLM, "chat", new=fake):
            out = asyncio.run(judge_json(cfg, "看图判断", image=png, timeout=1))
        self.assertEqual(out, {"开心": 3})

        content = fake.call_args.args[0][0]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(content[0]["image_url"]["url"]
                        .startswith("data:image/png;base64,"))
        self.assertEqual(content[1], {"type": "text", "text": "看图判断"})

    def test_failure_returns_none(self):
        cfg = FakeConfig({"judge": {"api_key": "test-key"}})
        with mock.patch.object(JudgeLLM, "chat",
                               new=mock.AsyncMock(return_value="不是 JSON")):
            self.assertIsNone(asyncio.run(judge_json(cfg, "hi", timeout=1)))
        with mock.patch.object(JudgeLLM, "chat",
                               new=mock.AsyncMock(side_effect=RuntimeError("x"))):
            self.assertIsNone(asyncio.run(judge_json(cfg, "hi", timeout=1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
