"""语音理解：转写请求格式、失败降级、perceive 接线。全部离线打桩。"""
import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import perceive, speech

MP3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 64


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


CFG = Cfg({"speech.enabled": True, "speech.timeout": 60, "speech.max_chars": 300,
           "llm.vision_api_key": "vkey", "llm.vision_base_url": "https://relay/v1",
           "llm.vision_model": "gemini-3.8-flash", "llm.reasoning_effort": "medium"})


class FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TranscribeTests(unittest.TestCase):
    def test_payload_uses_audio_and_vision_credentials(self):
        seen = {}

        def fake(req, timeout=0):
            seen["body"] = json.loads(req.data.decode())
            seen["headers"] = dict(req.headers)
            return FakeResp(json.dumps({"choices": [{"message": {"content": "明天下午三点集合"}}]}).encode())

        with patch("pawbot.speech.urllib.request.urlopen", fake):
            out = speech.transcribe(MP3, CFG)
        self.assertEqual(out, "明天下午三点集合")
        content = seen["body"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "input_audio")
        self.assertEqual(content[0]["input_audio"]["format"], "mp3")
        self.assertEqual(base64.b64decode(content[0]["input_audio"]["data"]), MP3)
        self.assertEqual(seen["body"]["reasoning_effort"], "medium")
        self.assertEqual(seen["body"]["model"], "gemini-3.8-flash")
        # 中转渠道的 Cloudflare 会拦没有 UA 的请求
        self.assertIn("user-agent", {k.lower(): v for k, v in seen["headers"].items()})

    def test_http_error_returns_empty(self):
        import urllib.error
        def boom(req, timeout=0):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with patch("pawbot.speech.urllib.request.urlopen", boom):
            self.assertEqual(speech.transcribe(MP3, CFG), "")

    def test_empty_audio_skips_request(self):
        with patch("pawbot.speech.urllib.request.urlopen") as urlopen:
            self.assertEqual(speech.transcribe(b"", CFG), "")
        urlopen.assert_not_called()

    def test_long_result_is_truncated(self):
        body = json.dumps({"choices": [{"message": {"content": "啊" * 900}}]}).encode()
        with patch("pawbot.speech.urllib.request.urlopen", lambda req, timeout=0: FakeResp(body)):
            self.assertLessEqual(len(speech.transcribe(MP3, CFG)), 300)


class RecordFetchTests(unittest.IsolatedAsyncioTestCase):
    def test_get_record_base64(self):
        conn = SimpleNamespace(call=self._call(return_value={
            "file": "base64://" + base64.b64encode(MP3).decode()}))
        raw = asyncio.run(perceive._record_bytes(conn, {"data": {"file": "a.amr"}}, CFG))
        self.assertEqual(raw, MP3)

    def test_get_record_local_path(self):
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "v.mp3")
        with open(path, "wb") as f:
            f.write(MP3)
        conn = SimpleNamespace(call=self._call(return_value={"file": path}))
        raw = asyncio.run(perceive._record_bytes(conn, {"data": {"file": "a.amr"}}, CFG))
        self.assertEqual(raw, MP3)

    def test_falls_back_to_url_when_call_fails(self):
        async def boom(**kw):
            raise RuntimeError("no")
        seg = {"data": {"file": "a.amr", "url": ""}}
        raw = asyncio.run(perceive._record_bytes(SimpleNamespace(call=boom), seg, CFG))
        self.assertIsNone(raw)

    @staticmethod
    def _call(return_value):
        async def call(action, **params):
            assert action == "get_record"
            assert params.get("out_format") == "mp3"
            return return_value
        return call


class EnrichTests(unittest.IsolatedAsyncioTestCase):
    async def test_enrich_sets_voice_text(self):
        ev = SimpleNamespace(type="message", message=[{"type": "record", "data": {"file": "a.amr"}}],
                             plain="", reply_id=None, self_id=1, is_group=True,
                             to_me=False, quoted_text="", image_desc="", forward_text="",
                             card_text="", voice_text="", talked_about=False)
        bot = SimpleNamespace(config=CFG, store=SimpleNamespace(window=lambda *a, **k: []))
        with patch.object(perceive, "_record_bytes", return_value=MP3), \
             patch.object(perceive.speech, "transcribe", return_value="今晚八点开黑"):
            await perceive.enrich(bot, None, ev)
        self.assertEqual(ev.voice_text, "今晚八点开黑")

    async def test_disabled_speech_skips(self):
        ev = SimpleNamespace(type="message", message=[{"type": "record", "data": {"file": "a.amr"}}],
                             plain="", reply_id=None, self_id=1, is_group=True,
                             to_me=False, quoted_text="", image_desc="", forward_text="",
                             card_text="", voice_text="", talked_about=False)
        cfg = Cfg({"speech.enabled": False})
        bot = SimpleNamespace(config=cfg, store=SimpleNamespace(window=lambda *a, **k: []))
        with patch.object(perceive, "_record_bytes") as rb:
            await perceive.enrich(bot, None, ev)
        rb.assert_not_called()
        self.assertEqual(ev.voice_text, "")


if __name__ == "__main__":
    unittest.main()
