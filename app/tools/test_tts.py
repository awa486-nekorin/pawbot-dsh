"""QQ 原生语音：清洗、合成降级、record 段、分条与自动语音条件。全部离线打桩。"""

import asyncio
import base64
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import message as msg
from pawbot import tts
from pawbot.safety import SafetyGuard
from pawbot.tools import ToolBox

WAV = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 32


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


CFG = Cfg({
    "tts.enabled": True,
    "tts.url": "http://127.0.0.1:50021/tts",
    "tts.health_url": "http://127.0.0.1:50021/health",
    "tts.timeout": 5,
    "tts.max_chars": 0,
    "tts.chunk_chars": 180,
    "tts.auto_chance": 1.0,
    "tts.auto_max_chars": 0,
    "tts.autostart": False,
})


class FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RecordingConnection:
    def __init__(self):
        self.messages = []

    async def send_message(self, segments, **target):
        self.messages.append(copy.deepcopy(segments))
        return {"message_id": len(self.messages)}


class CleanTests(unittest.TestCase):
    def test_removes_markers_cq_and_urls(self):
        out = tts.clean_for_speech(
            "[IMG:https://x.test/a.png] 看这个 [STICKER:file:///d/s.png] "
            "[CQ:image,file=abc.jpg] https://example.com/x 好不好")
        self.assertNotIn("IMG", out)
        self.assertNotIn("STICKER", out)
        self.assertNotIn("CQ:", out)
        self.assertNotIn("http", out)
        self.assertEqual(out, "看这个 好不好")

    def test_removes_emoji_and_kaomoji(self):
        out = tts.clean_for_speech("好开心！！♥♡ (๑•̀ㅂ•́)و✧ 真的吗")
        self.assertNotIn("♥", out)
        self.assertNotIn("♡", out)
        self.assertNotIn("✧", out)
        self.assertEqual(out, "好开心！真的吗")
        # 括号里有汉字就不是颜文字，保留
        self.assertEqual(tts.clean_for_speech("（笑）这个不错"), "（笑）这个不错")

    def test_tilde_and_newline_replacements(self):
        self.assertEqual(tts.clean_for_speech("你好呀～"), "你好呀。")
        self.assertEqual(tts.clean_for_speech("好吧~"), "好吧。")
        self.assertEqual(tts.clean_for_speech("第一句。\n第二句‖第三句"),
                         "第一句。第二句，第三句")

    def test_collapses_repeated_punctuation_and_strips(self):
        self.assertEqual(tts.clean_for_speech("  真的吗？？？！！  "), "真的吗？")
        self.assertEqual(tts.clean_for_speech("，。开头去掉"), "开头去掉")
        self.assertEqual(tts.clean_for_speech("   "), "")
        self.assertEqual(tts.clean_for_speech("~~~"), "")
        self.assertEqual(tts.clean_for_speech(None), "")


class SynthTests(unittest.IsolatedAsyncioTestCase):
    async def test_synth_returns_wav(self):
        seen = {}

        def fake(req, timeout=0):
            seen["body"] = req.data.decode()
            seen["timeout"] = timeout
            return FakeResp(WAV)

        with patch("pawbot.tts.urllib.request.urlopen", fake):
            raw = await tts.synth(CFG, "晚安～")
        self.assertEqual(raw, WAV)
        self.assertIn("晚安", seen["body"])
        # 基础超时 5 秒，按字数再加一点余量
        self.assertGreaterEqual(seen["timeout"], 5)

    async def test_synth_failure_returns_none(self):
        def boom(req, timeout=0):
            raise OSError("connection refused")

        with patch("pawbot.tts.urllib.request.urlopen", boom):
            self.assertIsNone(await tts.synth(CFG, "说句话"))

    async def test_synth_skips_empty(self):
        with patch("pawbot.tts.urllib.request.urlopen",
                   side_effect=AssertionError("不该发请求")):
            self.assertIsNone(await tts.synth(CFG, "   "))

    async def test_synth_does_not_refuse_long_text(self):
        seen = {}

        def fake(req, timeout=0):
            seen["body"] = req.data.decode()
            seen["timeout"] = timeout
            return FakeResp(WAV)

        long_text = "长" * 300
        with patch("pawbot.tts.urllib.request.urlopen", fake):
            self.assertEqual(await tts.synth(CFG, long_text), WAV)
        self.assertIn("长" * 50, seen["body"])
        # 长句的等待时间按字数放宽
        self.assertGreater(seen["timeout"], 5)

    async def test_record_segment_is_base64_record(self):
        with patch("pawbot.tts.urllib.request.urlopen", lambda req, timeout=0: FakeResp(WAV)):
            seg = await tts.record_segment(CFG, "晚安")
        self.assertEqual(seg["type"], "record")
        self.assertEqual(seg["data"]["file"],
                         "base64://" + base64.b64encode(WAV).decode("ascii"))

    async def test_record_segment_none_when_synth_fails(self):
        def boom(req, timeout=0):
            raise OSError("down")

        with patch("pawbot.tts.urllib.request.urlopen", boom):
            self.assertIsNone(await tts.record_segment(CFG, "晚安"))


class AutoVoiceTests(unittest.TestCase):
    def test_available_follows_enabled(self):
        self.assertTrue(tts.available(CFG))
        self.assertFalse(tts.available(Cfg({**CFG, "tts.enabled": False})))

    def test_conditions(self):
        rng = lambda: 0.0  # noqa: E731
        self.assertTrue(tts.should_auto_voice(CFG, "好想你", False, False, rng))
        self.assertFalse(tts.should_auto_voice(CFG, "好想你", True, False, rng))
        self.assertFalse(tts.should_auto_voice(CFG, "好想你", False, True, rng))
        self.assertFalse(tts.should_auto_voice(CFG, "看 https://a.b 吧", False, False, rng))
        self.assertFalse(tts.should_auto_voice(CFG, "`print(1)`", False, False, rng))
        self.assertTrue(tts.should_auto_voice(CFG, "字" * 200, False, False, rng))
        self.assertFalse(tts.should_auto_voice(
            Cfg({**CFG, "tts.enabled": False}), "好想你", False, False, rng))
        self.assertFalse(tts.should_auto_voice(
            Cfg({**CFG, "tts.auto_chance": 0}), "好想你", False, False, rng))

    def test_chance_roll(self):
        cfg = Cfg({**CFG, "tts.auto_chance": 0.5})
        self.assertTrue(tts.should_auto_voice(cfg, "好想你", False, False, lambda: 0.4))
        self.assertFalse(tts.should_auto_voice(cfg, "好想你", False, False, lambda: 0.6))


class VoiceRequestTests(unittest.TestCase):
    def test_positives(self):
        for text in ("用语音说句话", "发条语音听听", "念给我听", "来条语音",
                     "整段语音", "语音说一句晚安", "让我听听你的声音",
                     "换成语音回"):
            self.assertTrue(tts.voice_request(text), text)

    def test_negatives(self):
        for text in ("群里有人发语音了吗", "别发语音", "我不想用语音",
                     "发语音了吗", "刚才谁发语音了", "正常打字就行", "今天天气不错"):
            self.assertFalse(tts.voice_request(text), text)

    def test_explicit_speech_text(self):
        self.assertEqual(tts.explicit_speech_text("用语音说「晚安」"), "晚安")
        self.assertEqual(tts.explicit_speech_text("用语音说：晚安宝贝"), "晚安宝贝")
        self.assertEqual(tts.explicit_speech_text("念『今天也很想你』给我听"), "今天也很想你")
        self.assertEqual(tts.explicit_speech_text("用语音说“早点睡”"), "早点睡")
        self.assertEqual(tts.explicit_speech_text("「用语音说『晚安』」"), "晚安")
        self.assertEqual(tts.explicit_speech_text("用语音说句话"), "")
        self.assertEqual(len(tts.explicit_speech_text("念「" + "字" * 100 + "」", 10)), 10)

    def test_should_force_voice(self):
        self.assertFalse(tts.should_force_voice(
            Cfg({**CFG, "tts.enabled": False}), "发条语音"))
        self.assertTrue(tts.should_force_voice(CFG, "发条语音"))
        self.assertFalse(tts.should_force_voice(CFG, "正常说就行"))

    def test_split_for_voice(self):
        self.assertEqual(tts.split_for_voice("好想你呀", 10), ("好想你呀", ""))
        voice, rest = tts.split_for_voice("第一句。第二句。第三句。", 5)
        self.assertEqual(voice, "第一句。")
        self.assertEqual(rest, "第二句。第三句。")
        # 0 = 不限：整条都念，不留文字
        body = "第一句。" * 40
        self.assertEqual(tts.split_for_voice(body, 0), (body, ""))

    def test_chunk_for_speech(self):
        self.assertEqual(tts.chunk_for_speech("晚安"), ["晚安"])
        self.assertEqual(tts.chunk_for_speech("   "), [])
        long_text = "这是一句很长很长的话。" * 40
        chunks = tts.chunk_for_speech(long_text, 40)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 40 for c in chunks))
        self.assertEqual("".join(chunks), long_text)
        # 单句超长也要硬切，且不丢字
        hard = "啊" * 100
        pieces = tts.chunk_for_speech(hard, 30)
        self.assertTrue(all(len(c) <= 30 for c in pieces))
        self.assertEqual("".join(pieces), hard)


class SpeakToolTests(unittest.TestCase):
    def _bot(self, cfg):
        return SimpleNamespace(
            config=cfg, memes=None, account=None,
            qzone=SimpleNamespace(enabled=False),
            toolbox=None)

    def test_speak_hidden_when_disabled(self):
        tool = ToolBox(self._bot(Cfg({**CFG, "tts.enabled": False})))
        names = tool.available()
        self.assertNotIn("speak", names)
        self.assertEqual([s["function"]["name"] for s in tool.schemas()].count("speak"), 0)

    def test_speak_listed_when_enabled(self):
        tool = ToolBox(self._bot(CFG))
        self.assertIn("speak", tool.available())
        self.assertIn("speak", [s["function"]["name"] for s in tool.schemas()])


class SpeakToolAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_speak_returns_record_once_per_turn(self):
        ctx = SimpleNamespace(state={}, session_id="g1")
        bot = SimpleNamespace(config=CFG, memes=None, account=None,
                              qzone=SimpleNamespace(enabled=False))
        tool = ToolBox(bot)
        seg = msg.Record("base64://AAA")
        with patch("pawbot.tools.tts.record_segments",
                   new=AsyncMock(return_value=[seg])):
            text, extra = await tool.execute(ctx, "speak", {"text": "晚安"})
            self.assertIn("语音已经发出去了", text)
            self.assertEqual(extra, [seg])
            text2, extra2 = await tool.execute(ctx, "speak", {"text": "再说一句"})
            self.assertEqual(extra2, [])
        self.assertIn("已经发过语音", text2)

    async def test_speak_failure_text(self):
        ctx = SimpleNamespace(state={}, session_id="g1")
        bot = SimpleNamespace(config=CFG, memes=None, account=None,
                              qzone=SimpleNamespace(enabled=False))
        tool = ToolBox(bot)
        with patch("pawbot.tools.tts.record_segments", new=AsyncMock(return_value=[])):
            text, extra = await tool.execute(ctx, "speak", {"text": "晚安"})
        self.assertEqual(extra, [])
        self.assertIn("没录成", text)


class SplitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(config={"safety.burst": 3,
                                           "safety.react_delay": [0, 0],
                                           "safety.min_delay": 0.0,
                                           "safety.typing_cps": 1000},
                                   inbound={})
        self.guard = SafetyGuard(self.bot)
        self.sleep_patch = patch("pawbot.safety.asyncio.sleep", new_callable=AsyncMock)
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_record_gets_its_own_part(self):
        record = msg.Record("base64://AAA")
        parts = self.guard._split([msg.Text("先说一句"), record, msg.Text("再补一句")])
        self.assertEqual(len(parts), 3)
        self.assertEqual([s["type"] for s in parts[0]], ["text"])
        self.assertEqual([s["type"] for s in parts[1]], ["text"])
        self.assertEqual(parts[2], [record])
        self.assertEqual(len(parts), 3)

    def test_record_alone_is_unchanged(self):
        record = msg.Record("base64://AAA")
        parts = self.guard._split([record])
        self.assertEqual(parts, [[record]])

    async def test_send_parts_delivers_record(self):
        record = msg.Record("base64://AAA")
        conn = RecordingConnection()
        await self.guard._send_parts(conn, [[record]], "sess", None, 12345)
        self.assertEqual(conn.messages, [[record]])


if __name__ == "__main__":
    unittest.main()
