"""Offline media protocol tests. No network, bot connection, or real data writes."""

import asyncio
import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import media, message as msg
from pawbot.safety import SafetyGuard


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 24


class Cfg:
    """最小配置替身：键用 media__sticker_gap 这种写法传进来。"""

    def __init__(self, **kw):
        self.kw = {k.replace("__", "."): v for k, v in kw.items()}

    def get(self, key, default=None):
        return self.kw.get(key, default)


class Conn:
    def __init__(self, fail=0):
        self.messages = []
        self.fail = fail

    async def send_message(self, segments, **target):
        self.messages.append(copy.deepcopy(segments))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("local test failure")
        return {"message_id": len(self.messages)}


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bot = SimpleNamespace(base_dir=str(self.root), config={"safety.enabled": False},
                                   inbound={})
        self.guard = SafetyGuard(self.bot)
        self.ledger = media.ledger_for(self.bot)
        self.sleep = patch("pawbot.safety.asyncio.sleep", new_callable=AsyncMock).start()
        patch("pawbot.media.asyncio.sleep", new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.tmp.cleanup)

    def test_extract(self):
        clean, refs = media.extract("前 [IMG:https://a/1.jpg] [sticker：https://b/s.gif] 后 [x:y]")
        self.assertEqual(clean, "前   后 [x:y]")
        self.assertEqual([(r.kind, r.url) for r in refs], [
            ("img", "https://a/1.jpg"), ("sticker", "https://b/s.gif")])
        self.assertEqual(media.extract("[IMG 不是标记] [IMG:url]")[0], "[IMG 不是标记] [IMG:url]")

    async def test_text_then_image_then_sticker_are_separate(self):
        async def load(ref, **kwargs):
            ref.digest = hashlib.sha1(ref.url.encode()).hexdigest()
            return {"type": "image", "data": {"file": ref.url}}
        with patch("pawbot.media.resolve", side_effect=load):
            conn = Conn()
            result = await self.guard.send(conn, "s", [msg.Text(
                "看这张[IMG:https://a/1.jpg]‖还有[STICKER:https://b/s.gif]")], group_id=1)
        self.assertIsNotNone(result)
        self.assertEqual([msg.to_plain(x) for x in conn.messages[:2]], ["看这张", "还有"])
        self.assertEqual([x[0]["type"] for x in conn.messages[2:]], ["image", "image"])
        self.assertEqual([x[0]["data"]["file"] for x in conn.messages[2:]],
                         ["https://a/1.jpg", "https://b/s.gif"])
        self.assertNotIn("[IMG:", repr(conn.messages))

    async def test_existing_image_segment_is_alone(self):
        image = {"type": "image", "data": {"file": "base64://" + __import__("base64").b64encode(PNG).decode()}}
        async def resolve(ref, **kwargs):
            return {"type": "image", "data": {"file": ref.url}}
        conn = Conn()
        with patch("pawbot.media.resolve", side_effect=resolve):
            await self.guard.send(conn, "s", [msg.Text("甲"), image, msg.Text("乙")], group_id=1)
        self.assertEqual(conn.messages[0], [msg.Text("甲")])
        self.assertEqual(conn.messages[1], [msg.Text("乙")])
        self.assertEqual(conn.messages[2][0]["type"], "image")

    async def test_download_failure_retries_warns_once_and_continues(self):
        with patch("pawbot.media.imagesearch.download", return_value=None) as dl, \
             patch("pawbot.media.log.warn") as warn, \
             patch("pawbot.media.imagesearch.image_file", return_value="b64"):
            first = await media.resolve(media.MediaRef("img", "https://bad.example/a?secret=x"),
                                        session="s", ledger=self.ledger)
            self.assertIsNone(first)
            self.assertEqual(dl.call_count, 2)
            self.assertEqual(warn.call_count, 1)
            self.assertNotIn("secret", warn.call_args.args[0])

    async def test_send_failure_retries_once_and_skips(self):
        async def load(ref, **kwargs):
            ref.digest = "d"
            return {"type": "image", "data": {"file": "b64"}}
        conn = Conn(fail=2)
        with patch("pawbot.media.resolve", side_effect=load), patch("pawbot.media.log.warn") as warn:
            result = await self.guard.send(conn, "s", [msg.Text("[IMG:https://x/p]")], group_id=1)
        self.assertIsNone(result)
        self.assertEqual(len(conn.messages), 2)
        self.assertEqual(warn.call_count, 1)

    def test_dedupe_and_turns(self):
        ledger = self.ledger
        ledger.remember("s", "img", "u1", "h1")
        self.assertTrue(ledger.seen_image("s", url="u1"))
        self.assertTrue(ledger.seen_image("s", digest="h1"))
        ledger.remember("s", "img", "u2", "h1")
        self.assertTrue(ledger.seen_image("s", url="u2"))
        ledger.remember("s", "sticker", "s1", "z1")
        self.assertTrue(ledger.seen_sticker("s", url="s1", within=8))
        for _ in range(8):
            ledger.turn("s")
        self.assertFalse(ledger.seen_sticker("s", url="s1", within=8))

    def test_sticker_pacing(self):
        # [1,1] + chance 1.0：发完下一轮就允许，连续两轮都能带一张
        ledger = media.MediaLedger(None, config=Cfg(media__sticker_gap=[1, 1],
                                                    media__sticker_chance=1.0))
        self.assertTrue(ledger.sticker_allowed("s"))
        with patch("pawbot.media.random.randint", return_value=1) as randint:
            ledger.mark_sticker("s")
        randint.assert_called_once_with(1, 1)
        self.assertFalse(ledger.sticker_allowed("s"))
        ledger.turn("s")
        self.assertTrue(ledger.sticker_allowed("s"))
        ledger.mark_sticker("s")
        self.assertFalse(ledger.sticker_allowed("s"))
        ledger.turn("s")
        self.assertTrue(ledger.sticker_allowed("s"))

    def test_sticker_gap_two(self):
        ledger = media.MediaLedger(None, config=Cfg(media__sticker_gap=[2, 2],
                                                    media__sticker_chance=1.0))
        ledger.mark_sticker("s")          # turn 0 → next_sticker 2
        ledger.turn("s")
        self.assertFalse(ledger.sticker_allowed("s"))
        ledger.turn("s")
        self.assertTrue(ledger.sticker_allowed("s"))

    def test_sticker_chance_zero_never(self):
        ledger = media.MediaLedger(None, config=Cfg(media__sticker_gap=[1, 1],
                                                    media__sticker_chance=0.0))
        for _ in range(3):
            self.assertFalse(ledger.sticker_allowed("s"))
            ledger.mark_sticker("s")
            ledger.turn("s")

    def test_sticker_chance_rolls_once_per_turn(self):
        # tools / resolve / safety 同一轮会重复询问，掷骰结果必须一致
        ledger = media.MediaLedger(None, config=Cfg(media__sticker_gap=[1, 1],
                                                    media__sticker_chance=0.95))
        with patch("pawbot.media.random.random", return_value=0.99) as roll:
            self.assertFalse(ledger.sticker_allowed("s"))
            self.assertFalse(ledger.sticker_allowed("s"))
        self.assertEqual(roll.call_count, 1)
        ledger.turn("s")
        with patch("pawbot.media.random.random", return_value=0.1):
            self.assertTrue(ledger.sticker_allowed("s"))

    def test_sticker_config_fallback(self):
        # 配置缺失或写坏：[1,1] + 0.95
        for config in (None, Cfg(media__sticker_gap="oops", media__sticker_chance="bad"),
                       Cfg(media__sticker_gap=[3, 1], media__sticker_chance=2)):
            ledger = media.MediaLedger(None, config=config)
            with patch("pawbot.media.random.randint") as randint, \
                 patch("pawbot.media.random.random", return_value=0.0):
                ledger.mark_sticker("s")
                randint.assert_called_once_with(1, 1)
                ledger.turn("s")
                self.assertTrue(ledger.sticker_allowed("s"))
        self.bot.media_ledger = None
        self.bot.config = Cfg(media__sticker_gap=[1, 1], media__sticker_chance=1.0)
        self.assertIs(media.ledger_for(self.bot).config, self.bot.config)

    async def test_local_file_containment_and_validation(self):
        stickers = self.root / "data" / "stickers"
        stickers.mkdir(parents=True)
        inside = stickers / "a.png"
        inside.write_bytes(PNG)
        with patch.object(media, "ALLOWED_ROOTS", (stickers, self.root / "cache")):
            ref = media.MediaRef("sticker", inside.as_uri())
            self.assertEqual(await asyncio.to_thread(media._load, ref), PNG)
            outside = self.root / "outside.png"
            outside.write_bytes(PNG)
            with self.assertRaisesRegex(ValueError, "denied"):
                await asyncio.to_thread(media._load, media.MediaRef("sticker", outside.as_uri()))

    async def test_media_only_and_rate_limit_counts_media_once(self):
        async def load(ref, **kwargs):
            ref.digest = hashlib.sha1(ref.url.encode()).hexdigest()
            return {"type": "image", "data": {"file": ref.url}}
        with patch("pawbot.media.resolve", side_effect=load):
            conn = Conn()
            self.assertIsNotNone(await self.guard.send(conn, "media", [msg.Text(
                "[IMG:https://a/1][IMG:https://a/2][IMG:https://a/3]")], group_id=1))
            self.assertEqual(len(conn.messages), 3)
            self.bot.config.update({"safety.enabled": True, "safety.burst": 3})
            self.guard._bucket.clear()
            conn = Conn()
            result = await self.guard.send(conn, "mix", [msg.Text(
                "a‖b[IMG:https://b/1][IMG:https://b/2][IMG:https://b/3]")], group_id=1)
            self.assertIsNotNone(result)
            self.assertEqual(len(conn.messages), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
