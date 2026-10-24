"""表情包收藏：StickerBook.save 和 save_sticker 工具的离线测试。

全部写临时目录，不碰真的 data/stickers。
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot.stickers import StickerBook, StickerError  # noqa: E402
from pawbot.tools import ToolBox  # noqa: E402

# 1x1 PNG 和一段最小 JPEG 头，够过魔数判断
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg==")
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"jpeg-body" + b"\xff\xd9"

CUR_URL = "https://multimedia.nt.qq.com.cn/download/cur.png"
QUOTED_URL = "https://multimedia.nt.qq.com.cn/download/quoted.png"
SEARCH_URL = "https://i.pximg.net/img-master/img/2026/01/01/test_p0_master1200.jpg"


def png(seed: int = 0) -> bytes:
    return PNG + bytes([seed])


def make_book(base: str, max_count: int = 200) -> StickerBook:
    cfg = {"persona.stickers_dir": os.path.join(base, "stickers"),
           "persona.stickers_max": max_count}
    return StickerBook(base, cfg)


class StickerSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = make_book(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_png_writes_file_and_index(self):
        fname = self.book.save(png(1), "开心", ["开心", "笑"], "眯眼笑的猫脸")
        self.assertEqual(fname, "开心.png")
        self.assertTrue(os.path.exists(os.path.join(self.book.dir, fname)))
        with open(self.book.index_path, encoding="utf-8") as f:
            idx = json.load(f)
        self.assertEqual(idx[fname]["tags"], ["开心", "笑"])
        self.assertEqual(idx[fname]["desc"], "眯眼笑的猫脸")
        self.assertEqual(len(idx[fname]["sha1"]), 40)
        self.assertFalse([f for f in os.listdir(self.book.dir) if f.endswith(".tmp")])

    def test_jpeg_gets_sniffed_extension(self):
        fname = self.book.save(JPEG, "震惊")
        self.assertEqual(fname, "震惊.jpg")
        self.assertTrue(os.path.isfile(os.path.join(self.book.dir, fname)))

    def test_reject_non_image_and_oversize(self):
        with self.assertRaises(StickerError):
            self.book.save(b"not an image at all", "假图", ["无语"])
        big = PNG + b"x" * (5 * 1024 * 1024)
        with self.assertRaises(StickerError):
            self.book.save(big, "大图")
        self.assertEqual(self.book.files(), [])

    def test_identical_bytes_are_deduped_and_tags_merged(self):
        first = self.book.save(png(2), "甲", ["猫"], "第一张")
        second = self.book.save(png(2), "乙", ["可爱"])
        self.assertEqual(first, second)
        self.assertEqual(len(self.book.files()), 1)
        meta = self.book.index()[first]
        self.assertEqual(meta["tags"], ["猫", "可爱"])
        self.assertEqual(meta["desc"], "第一张")

    def test_filename_sanitization(self):
        cases = [
            ("../x", png(3), [], "x.png"),
            ("a/b", png(4), [], "b.png"),
            ("c\\d", png(5), [], "d.png"),
            ("开心😀", png(6), [], "开心.png"),
            ("..", png(7), ["晚安"], "晚安.png"),
        ]
        for name, raw, tags, want in cases:
            fname = self.book.save(raw, name, tags)
            self.assertEqual(fname, want)
            self.assertNotIn("/", fname)
            self.assertNotIn("\\", fname)
            self.assertNotIn("..", fname)

    def test_cap_refuses_instead_of_deleting(self):
        self.book = make_book(self.tmp.name, max_count=2)
        a = self.book.save(png(10), "一")
        b = self.book.save(png(11), "二")
        with self.assertRaises(StickerError) as cm:
            self.book.save(png(12), "三")
        self.assertIn("上限", str(cm.exception))
        self.assertEqual(self.book.files(), sorted([a, b]))
        # 满了也不耽误去重：同一张图直接认旧的
        self.assertEqual(self.book.save(png(10), "重复"), a)

    def test_pick_matches_saved_tags_including_single_cjk_char(self):
        self.book.save(png(20), "随便", ["无语", "服了"])
        self.book.save(png(21), "别的", ["开心"])
        self.book.save(png(22), "猫猫", ["猫"])
        self.assertEqual(os.path.basename(self.book.pick("无语")), "随便.png")
        self.assertEqual(os.path.basename(self.book.pick("他发了个无语的表情")), "随便.png")
        self.assertEqual(os.path.basename(self.book.pick("猫")), "猫猫.png")


class SaveStickerToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = make_book(self.tmp.name)
        self.bot = SimpleNamespace(config={}, stickers=self.book,
                                   account=None,
                                   qzone=SimpleNamespace(enabled=False),
                                   conn_for=Mock(return_value=None))
        self.toolbox = ToolBox(self.bot)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, message=None, reply_id=None, state=None):
        ev = SimpleNamespace(message=message or [], reply_id=reply_id, self_id=10001)
        return SimpleNamespace(event=ev, state=state if state is not None else {},
                               session_id="group_1", self_id=10001)

    def test_tool_registered_even_with_empty_library(self):
        names = self.toolbox.available()
        self.assertIn("save_sticker", names)
        # 库空也保留 pick_sticker：它会去网上找一张存进库
        self.assertIn("pick_sticker", names)
        self.assertIsNotNone(
            next(s for s in self.toolbox.schemas()
                 if s["function"]["name"] == "save_sticker"))

    async def test_current_message_image_wins_over_quote(self):
        ctx = self.ctx(message=[
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"url": CUR_URL, "file": "abc.image"}},
        ], reply_id="4242")
        with patch("pawbot.tools.imagesearch.download", return_value=png(30)) as dl:
            text, media = await self.toolbox._t_save_sticker(ctx, "震惊", ["震惊"])
        self.assertEqual(media, [])
        self.assertTrue(text.startswith("存好了：震惊.png"), text)
        self.assertIn("（标签：震惊）", text)
        item = dl.call_args.args[0]
        self.assertEqual(item["url"], CUR_URL)
        self.assertFalse(item["need_referer"])
        self.bot.conn_for.assert_not_called()
        self.assertIn("震惊.png", self.book.files())

    async def test_quoted_image_is_used_when_current_has_none(self):
        class FakeConn:
            def __init__(self, segs):
                self.segs = segs
                self.calls = []

            async def call(self, action, **kw):
                self.calls.append(action)
                return {"message": self.segs}

        conn = FakeConn([{"type": "image", "data": {"url": QUOTED_URL}}])
        self.bot.conn_for = Mock(return_value=conn)
        ctx = self.ctx(message=[{"type": "text", "data": {"text": "这个好笑"}}],
                       reply_id="4242")
        with patch("pawbot.tools.imagesearch.download", return_value=png(31)) as dl:
            text, media = await self.toolbox._t_save_sticker(ctx, "好笑", ["开心"])
        self.assertEqual(media, [])
        self.assertIn("存好了", text)
        self.assertEqual(dl.call_args.args[0]["url"], QUOTED_URL)
        self.assertIn("get_msg", conn.calls)

    async def test_quote_fetch_failure_is_graceful(self):
        self.bot.conn_for = Mock(side_effect=RuntimeError("没连上"))
        ctx = self.ctx(reply_id="4242")
        with patch("pawbot.tools.imagesearch.download") as dl:
            text, media = await self.toolbox._t_save_sticker(ctx, "无图")
        self.assertEqual(media, [])
        self.assertIn("没找到", text)
        dl.assert_not_called()

    async def test_search_url_used_and_pixiv_referer_set(self):
        ctx = self.ctx(state={"avatar_ok_urls": {SEARCH_URL}})
        with patch("pawbot.tools.imagesearch.download", return_value=png(32)) as dl:
            text, media = await self.toolbox._t_save_sticker(
                ctx, "猫猫", ["猫"], source="search")
        self.assertEqual(media, [])
        self.assertIn("存好了", text)
        item = dl.call_args.args[0]
        self.assertEqual(item["url"], SEARCH_URL)
        self.assertTrue(item["need_referer"])
        self.assertEqual(item["referer"], "https://www.pixiv.net/")

    async def test_no_image_returns_clear_reason(self):
        ctx = self.ctx(state={"avatar_ok_urls": set()})
        with patch("pawbot.tools.imagesearch.download") as dl:
            text, media = await self.toolbox._t_save_sticker(ctx, "空")
        self.assertEqual(media, [])
        self.assertIn("没找到", text)
        dl.assert_not_called()

    async def test_download_failure_returns_reason_and_saves_nothing(self):
        ctx = self.ctx(message=[{"type": "image", "data": {"url": CUR_URL}}])
        with patch("pawbot.tools.imagesearch.download", return_value=None):
            text, media = await self.toolbox._t_save_sticker(ctx, "失败")
        self.assertEqual(media, [])
        self.assertIn("没存成", text)
        self.assertEqual(self.book.files(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
