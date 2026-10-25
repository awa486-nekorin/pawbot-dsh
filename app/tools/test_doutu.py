"""斗图网抓取 + 表情包入库的离线测试（不联网，全部 patch 掉 _open）。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import doutu  # noqa: E402
from pawbot.stickers import StickerBook  # noqa: E402

BASE = Path(__file__).resolve().parents[1]
STICKER_DIR = BASE / "data" / "stickers"

PAGE = b"""<html><body>
<img class="lazy" data-original="http://img.doutupk.com/a.jpg" data-backup="http://img.doutupk.com/a.jpg">
<img class="lazy" data-original="http://img.doutupk.com/a.jpg">
<img class="lazy" data-original="//img.doutupk.com/b.png">
<img class="lazy" data-original="http://img.doutupk.com/c.gif?x=1">
<img class="lazy" data-original="http://img.doutupk.com/d.webp">
<img class="lazy" data-original="http://img.doutupk.com/e.txt">
</body></html>"""

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 4000
PNGDATA = b"\x89PNG\r\n\x1a\n" + b"p" * 4000


def make_book(base: str) -> StickerBook:
    return StickerBook(base, {"persona.stickers_dir": os.path.join(base, "stickers"),
                              "persona.stickers_max": 400,
                              "persona.sticker_fetch": True})


class SearchTests(unittest.TestCase):
    def test_parses_dedupes_and_filters(self):
        urls = []
        with patch("pawbot.doutu._open", side_effect=lambda url, **kw: PAGE) as op:
            urls = doutu.search("熊猫头", limit=20)
        self.assertEqual(op.call_args.args[0],
                         "https://www.doutupk.com/search?keyword=" + doutu.urllib.parse.quote("熊猫头"))
        self.assertEqual(urls, [
            "http://img.doutupk.com/a.jpg",
            "https://img.doutupk.com/b.png",
            "http://img.doutupk.com/c.gif?x=1",
            "http://img.doutupk.com/d.webp",
        ])

    def test_limit(self):
        with patch("pawbot.doutu._open", return_value=PAGE):
            self.assertEqual(len(doutu.search("开心", limit=2)), 2)

    def test_https_then_http_fallback(self):
        calls = []

        def fake_open(url, **kw):
            calls.append(url)
            if url.startswith("https://"):
                raise ConnectionResetError("tls")
            return PAGE

        with patch("pawbot.doutu._open", side_effect=fake_open):
            urls = doutu.search("无语")
        self.assertTrue(urls)
        self.assertEqual(calls[0][:8], "https://")
        self.assertEqual(calls[1][:7], "http://")

    def test_failure_returns_empty(self):
        with patch("pawbot.doutu._open", side_effect=OSError("down")):
            self.assertEqual(doutu.search("震惊"), [])
        self.assertEqual(doutu.search(""), [])

    def test_page_without_images(self):
        with patch("pawbot.doutu._open", return_value=b"<html>nothing</html>"):
            self.assertEqual(doutu.search("贴贴"), [])


class DownloadTests(unittest.TestCase):
    def test_accepts_image_and_upgrades_http_to_https(self):
        calls = []

        def fake_open(url, **kw):
            calls.append(url)
            if url.startswith("https://"):
                raise OSError("no tls")
            return JPEG

        with patch("pawbot.doutu._open", side_effect=fake_open):
            raw = doutu.download("http://img.doutupk.com/a.jpg")
        self.assertEqual(raw, JPEG)
        self.assertEqual(calls[0], "https://img.doutupk.com/a.jpg")
        self.assertEqual(calls[1], "http://img.doutupk.com/a.jpg")

    def test_rejects_non_image(self):
        with patch("pawbot.doutu._open", return_value=b"<html>" + b"x" * 4000):
            self.assertIsNone(doutu.download("https://img.doutupk.com/a.jpg"))

    def test_rejects_too_small_and_too_big(self):
        with patch("pawbot.doutu._open", return_value=b"\xff\xd8\xff" + b"x" * 100):
            self.assertIsNone(doutu.download("https://img.doutupk.com/small.jpg"))
        with patch("pawbot.doutu._open", return_value=JPEG + b"x" * 5000):
            self.assertIsNone(doutu.download("https://img.doutupk.com/big.jpg",
                                             max_bytes=4096))

    def test_bad_url_and_exceptions(self):
        self.assertIsNone(doutu.download("file:///etc/passwd"))
        with patch("pawbot.doutu._open", side_effect=TimeoutError()):
            self.assertIsNone(doutu.download("https://img.doutupk.com/a.jpg"))


class FetchFromDoutuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = make_book(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_saves_with_tag_desc_and_hashed_name(self):
        with patch("pawbot.doutu.search", return_value=["http://x/1.jpg"]), \
             patch("pawbot.doutu.download", return_value=PNGDATA):
            names = self.book.fetch_from_doutu("开心", 1, extra_tags=["笑死"])
        self.assertEqual(len(names), 1)
        name = names[0]
        self.assertTrue(name.startswith("开心_"), name)
        self.assertTrue(name.endswith(".png"), name)
        self.assertEqual(len(name.split("_")[1].split(".")[0]), 6)
        meta = self.book.index()[name]
        self.assertIn("开心", meta["tags"])
        self.assertIn("笑死", meta["tags"])
        self.assertEqual(meta["desc"], "斗图网：开心")
        self.assertEqual(len(meta["sha1"]), 40)
        self.assertTrue(os.path.isfile(os.path.join(self.book.dir, name)))

    def test_same_image_in_one_page_saved_once(self):
        with patch("pawbot.doutu.search", return_value=["http://x/1.jpg", "http://x/2.jpg"]), \
             patch("pawbot.doutu.download", return_value=PNGDATA):
            names = self.book.fetch_from_doutu("无语", 2)
        self.assertEqual(len(names), 1)
        self.assertEqual(len(self.book.files()), 1)

    def test_sha1_dedupe_across_runs(self):
        with patch("pawbot.doutu.search", return_value=["http://x/1.jpg"]), \
             patch("pawbot.doutu.download", return_value=PNGDATA):
            first = self.book.fetch_from_doutu("晚安", 1)
        with patch("pawbot.doutu.search", return_value=["http://x/9.jpg"]), \
             patch("pawbot.doutu.download", return_value=PNGDATA):
            second = self.book.fetch_from_doutu("困", 1)
        self.assertEqual(first, second)
        self.assertEqual(len(self.book.files()), 1)
        self.assertIn("困", self.book.index()[first[0]]["tags"])

    def test_download_failures_and_empty_search(self):
        with patch("pawbot.doutu.search", return_value=[]):
            self.assertEqual(self.book.fetch_from_doutu("震惊", 1), [])
        with patch("pawbot.doutu.search", return_value=["http://x/1.jpg"]), \
             patch("pawbot.doutu.download", return_value=None):
            self.assertEqual(self.book.fetch_from_doutu("震惊", 1), [])
        self.assertEqual(self.book.files(), [])


class PickOrFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = make_book(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_existing_library_never_hits_network(self):
        self.book.save(PNGDATA, "开心", ["开心"])
        with patch("pawbot.doutu.search", side_effect=AssertionError("不该联网")), \
             patch.object(self.book, "fetch_from_doutu",
                          side_effect=AssertionError("不该抓图")):
            path = await self.book.pick_or_fetch("开心")
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("开心.png"))

    async def test_missing_tag_fetches_then_returns(self):
        with patch("pawbot.doutu.search", return_value=["http://x/1.jpg"]) as search, \
             patch("pawbot.doutu.download", return_value=JPEG) as dl:
            path = await self.book.pick_or_fetch("破防")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        name = os.path.basename(path)
        self.assertIn("破防", self.book.index()[name]["tags"])
        self.assertEqual(search.call_count, 1)
        self.assertEqual(dl.call_count, 1)

    async def test_empty_query_does_not_network(self):
        with patch("pawbot.doutu.search", side_effect=AssertionError("不该联网")):
            self.assertIsNone(await self.book.pick_or_fetch(""))

    async def test_fetch_disabled_stays_offline(self):
        self.book.fetch_enabled = False
        with patch("pawbot.doutu.search", side_effect=AssertionError("不该联网")):
            self.assertIsNone(await self.book.pick_or_fetch("牢大"))


class IndexHygieneTests(unittest.TestCase):
    def test_no_index_entry_without_file(self):
        index_path = STICKER_DIR / "index.json"
        if not index_path.exists():
            self.skipTest("没有真实表情包库")
        idx = json.loads(index_path.read_text(encoding="utf-8"))
        files = {f for f in os.listdir(STICKER_DIR)
                 if os.path.splitext(f)[1].lower() in
                 {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}}
        self.assertEqual(sorted(set(idx) - files), [])

    def test_self_drawn_placeholders_are_gone(self):
        # 仓库里不带运行时数据（app/data 只在部署时生成），没有表情包库就跳过，
        # 而不是报 FileNotFoundError —— 这条测的是「库里的脏条目」，前提是库存在。
        index_path = STICKER_DIR / "index.json"
        if not index_path.exists():
            self.skipTest("没有真实表情包库")
        idx = json.loads(index_path.read_text(encoding="utf-8"))
        for name in ("开心.png", "无语.png", "晚安.png", "猫.png", "我要看小萝莉.jpg"):
            self.assertNotIn(name, idx)
            self.assertFalse((STICKER_DIR / name).exists(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
