"""自动抓图的二次元门槛（pawbot/animecheck.py）离线测试。

不联网：urllib.request.urlopen 和 doutu.search/download 全部 patch 掉，
表情包库放在临时目录，判定缓存也就落在临时目录里。
"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import animecheck  # noqa: E402
from pawbot.stickers import StickerBook  # noqa: E402

# 能过魔数判断、但 PIL 解不开的假图：走「原图直发」那条兜底分支
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 4000
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 4000


class FakeResp:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(anime) -> bytes:
    """包一层 OpenAI 兼容的回复；content 里是模型该输出的 JSON。"""
    content = json.dumps({"anime": anime, "kind": "Q版", "reason": "测试"})
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://relay.example/v1/chat/completions",
                                  code, "boom", None, None)


def make_book(base: str, anime_only: bool = True):
    cfg = {
        "persona.stickers_dir": os.path.join(base, "stickers"),
        "persona.stickers_max": 200,
        "persona.sticker_fetch": True,
        "persona.sticker_anime_only": anime_only,
        "persona.anime_check_model": "gemini-3.8-flash",
        "llm.base_url": "https://relay.example/v1",
        "llm.api_key": "test-key-not-real",
    }
    return cfg, StickerBook(base, cfg)


class AnimeGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg, self.book = make_book(self.tmp.name)
        animecheck._CACHE.clear()
        animecheck._CACHE_PATH = None

    def tearDown(self):
        animecheck._CACHE.clear()
        animecheck._CACHE_PATH = None
        self.tmp.cleanup()

    def _fetch(self, raw=PNG, **urlopen_kwargs):
        with patch("pawbot.doutu.search", return_value=["http://x/1.jpg"]), \
             patch("pawbot.doutu.download", return_value=raw), \
             patch("urllib.request.urlopen", **urlopen_kwargs) as op:
            names = self.book.fetch_from_doutu("开心", 1)
        return names, op

    # ------------------------------------------------------------ 门槛
    def test_false_is_not_saved(self):
        names, op = self._fetch(return_value=FakeResp(reply(False)))
        self.assertEqual(names, [])
        self.assertEqual(self.book.files(), [])
        self.assertEqual(self.book.index(), {})
        self.assertEqual(op.call_count, 1)

    def test_none_is_not_saved(self):
        names, op = self._fetch(side_effect=http_error(501))
        self.assertEqual(names, [])
        self.assertEqual(self.book.files(), [])
        self.assertEqual(op.call_count, 1)          # 501 不重试

    def test_timeout_or_garbage_is_not_saved(self):
        names, _ = self._fetch(side_effect=TimeoutError("slow"))
        self.assertEqual(names, [])
        names, _ = self._fetch(return_value=FakeResp(b"<html>not json</html>"))
        self.assertEqual(names, [])
        self.assertEqual(self.book.files(), [])

    def test_retry_once_on_5xx_then_give_up(self):
        names, op = self._fetch(side_effect=http_error(503))
        self.assertEqual(names, [])
        self.assertEqual(op.call_count, 2)          # 429/5xx 最多重试 1 次

    def test_retry_once_on_5xx_then_succeed(self):
        calls = []

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise http_error(500)
            return FakeResp(reply(True))

        names, _ = self._fetch(side_effect=flaky)
        self.assertEqual(len(names), 1)
        self.assertEqual(len(calls), 2)

    def test_true_is_saved_and_marked(self):
        names, _ = self._fetch(return_value=FakeResp(reply(True)))
        self.assertEqual(len(names), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.book.dir, names[0])))
        self.assertIs(self.book.index()[names[0]]["anime"], True)

    # ------------------------------------------------------------ 缓存
    def test_same_sha1_hits_cache(self):
        names, op = self._fetch(return_value=FakeResp(reply(True)))
        self.assertEqual(len(names), 1)
        self.assertEqual(op.call_count, 1)

        cache_file = os.path.join(self.book.dir, animecheck.CACHE_NAME)
        self.assertTrue(os.path.isfile(cache_file))
        with open(cache_file, encoding="utf-8") as f:
            cached = json.load(f)
        self.assertEqual(list(cached.values()), [True])

        # 第二次：不同 URL、同样字节 → 直接吃缓存，不再打模型
        names2, op2 = self._fetch(return_value=AssertionError("不该再打模型"))
        self.assertEqual(names2, names)
        self.assertEqual(op2.call_count, 0)

        # 模拟进程重启（内存缓存清空）→ 命中磁盘缓存
        animecheck._CACHE.clear()
        animecheck._CACHE_PATH = None
        with patch("urllib.request.urlopen",
                   side_effect=AssertionError("不该再打模型")) as op3:
            self.assertIs(animecheck.is_anime_cached(PNG, self.cfg), True)
        self.assertEqual(op3.call_count, 0)

    def test_corrupt_cache_file_is_tolerated(self):
        cache_file = os.path.join(self.book.dir, animecheck.CACHE_NAME)
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        names, op = self._fetch(return_value=FakeResp(reply(True)))
        self.assertEqual(len(names), 1)
        self.assertEqual(op.call_count, 1)
        with open(cache_file, encoding="utf-8") as f:
            self.assertEqual(list(json.load(f).values()), [True])

    # ------------------------------------------------------------ 开关
    def test_switch_off_skips_check_entirely(self):
        cfg, book = make_book(os.path.join(self.tmp.name, "off"), anime_only=False)
        with patch("pawbot.doutu.search", return_value=["http://x/1.jpg"]), \
             patch("pawbot.doutu.download", return_value=PNG), \
             patch("urllib.request.urlopen",
                   side_effect=AssertionError("关掉门槛就不该打模型")) as op:
            names = book.fetch_from_doutu("开心", 1)
        self.assertEqual(len(names), 1)
        self.assertEqual(op.call_count, 0)
        self.assertNotIn("anime", book.index()[names[0]])

    def test_jpeg_bytes_are_accepted_too(self):
        names, _ = self._fetch(return_value=FakeResp(reply(True)), raw=JPEG)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".jpg"))


class ParseTests(unittest.TestCase):
    def test_fenced_and_noisy_replies(self):
        self.assertIs(animecheck.parse_anime('```json\n{"anime": true}\n```'), True)
        self.assertIs(animecheck.parse_anime('判定结果：{"anime": false, "kind": "真人照片"} 完'), False)
        self.assertIsNone(animecheck.parse_anime("我觉得是二次元"))
        self.assertIsNone(animecheck.parse_anime(""))

    def test_missing_credentials_means_unknown(self):
        self.assertIsNone(animecheck.is_anime(PNG, {}))
        self.assertFalse(animecheck.configured({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
