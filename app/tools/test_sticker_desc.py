"""表情包 2.0 的离线测试：描述元数据 / 按描述挑图 / 情绪绕闸 / 低频默认值。

全部写临时目录、全部不联网：
- 「看图」用注入的假 VISION_DESCRIBER（模拟视觉通道成功），失败分支不注入；
- 表情包库都是临时目录里的假 PNG 文件。
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import config as config_mod  # noqa: E402
from pawbot import media, stickers  # noqa: E402
from pawbot.stickers import StickerBook  # noqa: E402
from pawbot.tools import ToolBox  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg==")


def png(seed: int = 0) -> bytes:
    return PNG + bytes([seed])


def make_config(base: str, **extra) -> dict:
    cfg = {"persona.stickers_dir": os.path.join(base, "stickers"),
           "persona.stickers_max": 100,
           "persona.sticker_fetch": False}
    cfg.update(extra)
    return cfg


def make_book(base: str, **extra) -> StickerBook:
    return StickerBook(base, make_config(base, **extra))


def write_index(book: StickerBook, entries: dict) -> None:
    """按给定 meta 造索引 + 对应假图片文件（不走 save，方便只放 desc）。"""
    for name in entries:
        with open(os.path.join(book.dir, name), "wb") as f:
            f.write(png(len(entries)))
    book.save_index(entries)


class DescMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = make_book(self.tmp.name)

    def test_vision_desc_and_mood_are_written(self):
        calls = []

        def fake_vision(raw):
            calls.append(raw)
            return {"desc": "猫娘叉腰得意，配字『就这』", "mood": "得意",
                    "tags": ["猫娘", "得意"]}

        with patch.object(stickers, "VISION_DESCRIBER", fake_vision):
            fname = self.book.save(png(1), "叉腰", tags=["得意"])
        self.assertEqual(len(calls), 1)
        meta = self.book.index()[fname]
        self.assertEqual(meta["desc"], "猫娘叉腰得意，配字『就这』")
        self.assertEqual(meta["mood"], "得意")
        self.assertEqual(meta["desc_source"], "vision")
        self.assertEqual(meta["tags"], ["得意"])          # 人工标签不被视觉标签冲掉
        self.assertEqual(len(meta["sha1"]), 40)

    def test_vision_failure_falls_back_to_readable_tags_desc(self):
        def broken(raw):
            raise RuntimeError("看图挂了吧")

        with patch.object(stickers, "VISION_DESCRIBER", broken):
            fname = self.book.save(png(2), "摆烂", tags=["困", "躺平", "摆烂"])
        meta = self.book.index()[fname]
        self.assertEqual(meta["desc"], "困得躺平摆烂的样子")
        self.assertEqual(meta["desc_source"], "tags")
        self.assertEqual(meta["mood"], "摸鱼")
        self.assertNotIn("斗图网", meta["desc"])

    def test_no_channel_uses_tags_desc(self):
        fname = self.book.save(png(3), "开心", tags=["开心"])
        meta = self.book.index()[fname]
        self.assertEqual(meta["desc"], "开心的样子")
        self.assertEqual(meta["desc_source"], "tags")
        self.assertNotIn("斗图网", meta["desc"])
        self.assertEqual(meta["mood"], "开心")

    def test_desc_from_tags_examples(self):
        self.assertEqual(stickers.desc_from_tags(["困", "躺平", "摆烂"]),
                         "困得躺平摆烂的样子")
        self.assertEqual(stickers.desc_from_tags(["开心"]), "开心的样子")
        self.assertEqual(stickers.desc_from_tags(["无语", "服了"]), "无语、服了的样子")
        self.assertEqual(stickers.desc_from_tags([], "委屈"), "委屈的样子")
        self.assertEqual(stickers.desc_from_tags([]), "一张表情包")
        for tags in (["开心"], ["困", "躺平"], ["摸鱼"]):
            self.assertNotIn("斗图网：", stickers.desc_from_tags(tags))

    def test_saving_same_image_keeps_desc_and_does_not_rewrite_with_fake(self):
        first = self.book.save(png(4), "甲", ["猫"], desc="眯眼笑的猫脸")
        self.assertEqual(self.book.index()[first]["desc"], "眯眼笑的猫脸")
        self.assertEqual(self.book.index()[first]["desc_source"], "vision")
        # 同图再存（没给 desc、也没有看图通道）：保留原来的真描述
        second = self.book.save(png(4), "乙", ["可爱"])
        self.assertEqual(first, second)
        meta = self.book.index()[first]
        self.assertEqual(meta["desc"], "眯眼笑的猫脸")
        self.assertEqual(meta["tags"], ["猫", "可爱"])

    def test_fake_doutu_desc_is_replaced(self):
        fname = self.book.save(png(5), "旧", ["无语"])
        idx = self.book.index()
        idx[fname]["desc"] = "斗图网：无语"
        self.book.save_index(idx)
        self.book.save(png(5), "再存", ["无语"])
        meta = self.book.index()[fname]
        self.assertEqual(meta["desc"], "无语的样子")
        self.assertNotIn("斗图网", meta["desc"])

    def test_read_credential_env_wins(self):
        with patch.dict(os.environ, {"TOGEMINI_API_KEY": "  sk-test  "}):
            self.assertEqual(stickers.read_credential(), "sk-test")


class PickWithMetaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = make_book(self.tmp.name)

    def test_matches_only_by_desc(self):
        write_index(self.book, {
            "a.png": {"desc": "猫娘叉腰得意，配字『就这』", "sha1": "a" * 40},
            "b.png": {"desc": "熊猫头笑得在地上打滚", "sha1": "b" * 40},
        })
        hit = self.book.pick_with_meta("叉腰得意")
        self.assertIsNotNone(hit)
        path, meta = hit
        self.assertEqual(os.path.basename(path), "a.png")
        self.assertEqual(meta["name"], "a.png")
        self.assertIn("叉腰", meta["desc"])
        # pick() 是它的薄封装，行为一致
        self.assertEqual(self.book.pick("叉腰得意"), path)

    def test_matches_by_mood_alone(self):
        write_index(self.book, {
            "a.png": {"desc": "猫娘叉腰", "mood": "得意", "sha1": "a" * 40},
            "b.png": {"desc": "熊猫头", "mood": "无语", "sha1": "b" * 40},
        })
        path, meta = self.book.pick_with_meta("得意")
        self.assertEqual(os.path.basename(path), "a.png")
        self.assertEqual(meta["mood"], "得意")

    def test_tags_still_outrank_desc(self):
        write_index(self.book, {
            "tag.png": {"tags": ["无语"], "desc": "普通的猫", "sha1": "a" * 40},
            "desc.png": {"desc": "无语得翻白眼", "sha1": "b" * 40},
        })
        path, _meta = self.book.pick_with_meta("无语")
        self.assertEqual(os.path.basename(path), "tag.png")

    def test_empty_library_and_miss_return_none(self):
        self.assertIsNone(self.book.pick_with_meta("无语"))
        write_index(self.book, {"a.png": {"desc": "猫娘", "tags": ["猫"]}})
        self.assertIsNone(self.book.pick_with_meta("牢大坤坤"))
        # 空 query 仍然随机挑一张（老行为）
        hit = self.book.pick_with_meta("")
        self.assertIsNotNone(hit)

    def test_top_meta_lists_desc_and_tags_without_paths(self):
        write_index(self.book, {
            "a.png": {"desc": "猫娘叉腰得意", "tags": ["得意", "猫娘"]},
            "b.png": {"desc": "熊猫头无语", "tags": ["无语"]},
        })
        rows = self.book.top_meta("得意", limit=8)
        self.assertEqual(len(rows), 2)
        name, meta = rows[0]
        self.assertEqual(name, "a.png")
        self.assertEqual(meta["desc"], "猫娘叉腰得意")
        self.assertNotIn(".png", meta["desc"])


class PickStickerToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = make_book(self.tmp.name)
        self.book.save(png(10), "无语", tags=["无语"])
        self.book.save(png(11), "兴奋猫", tags=["兴奋"])
        self.blend = {"dominant": "惊讶", "secondary": None,
                      "weights": (1.0,), "intensity": "轻微"}
        self.bot = SimpleNamespace(
            config=make_config(self.tmp.name),
            stickers=self.book,
            qzone=SimpleNamespace(enabled=False),
            emotion=SimpleNamespace(blend=lambda: dict(self.blend)),
        )
        self.toolbox = ToolBox(self.bot)

    def ctx(self):
        return SimpleNamespace(session_id="group_1", state={},
                               event=SimpleNamespace(plain=""))

    def _ledger(self, allowed):
        class FakeLedger:
            def __init__(self):
                self.allowed = allowed
                self.forced = 0

            def sticker_allowed(self, sid):
                return self.allowed

            def force_sticker(self, sid):
                self.forced += 1
                self.allowed = True
                return True

        return FakeLedger()

    async def test_rejects_when_random_gate_closed(self):
        ledger = self._ledger(False)
        ctx = self.ctx()
        with patch("pawbot.media.ledger_for", return_value=ledger):
            text, segs = await self.toolbox._t_pick_sticker(ctx, "无语")
        self.assertEqual(segs, [])
        self.assertIn("别发", text)
        self.assertEqual(ledger.forced, 0)
        self.assertEqual(ctx.state.get("_picked"), [])

    async def test_strong_emotion_bypasses_random_gate(self):
        self.blend = {"dominant": "兴奋", "secondary": None,
                      "weights": (1.0,), "intensity": "明显"}
        ledger = self._ledger(False)
        ctx = self.ctx()
        with patch("pawbot.media.ledger_for", return_value=ledger):
            text, segs = await self.toolbox._t_pick_sticker(ctx, "")
        self.assertEqual(ledger.forced, 1)             # 真的绕过了随机闸
        self.assertEqual(len(segs), 1)
        self.assertTrue(segs[0]["data"]["text"].startswith("[STICKER:file:///"))
        self.assertIn("描述：兴奋的样子", text)          # 回包带选中的描述
        self.assertEqual(len(ctx.state["_picked"]), 1)

    async def test_intensity_strong_bypasses_too(self):
        self.blend = {"dominant": "低落", "secondary": None,
                      "weights": (1.0,), "intensity": "强烈"}
        ledger = self._ledger(False)
        with patch("pawbot.media.ledger_for", return_value=ledger):
            _text, segs = await self.toolbox._t_pick_sticker(self.ctx(), "无语")
        self.assertEqual(ledger.forced, 1)
        self.assertEqual(len(segs), 1)

    async def test_only_one_sticker_per_turn(self):
        self.blend = {"dominant": "兴奋", "secondary": None,
                      "weights": (1.0,), "intensity": "强烈"}
        ledger = self._ledger(True)
        ctx = self.ctx()
        with patch("pawbot.media.ledger_for", return_value=ledger):
            _text1, segs1 = await self.toolbox._t_pick_sticker(ctx, "兴奋")
            text2, segs2 = await self.toolbox._t_pick_sticker(ctx, "无语")
        self.assertEqual(len(segs1), 1)
        self.assertEqual(segs2, [])
        self.assertIn("已经带过一张", text2)
        self.assertEqual(len(ctx.state["_picked"]), 1)

    async def test_reply_carries_desc_of_picked_sticker(self):
        ledger = self._ledger(True)
        with patch("pawbot.media.ledger_for", return_value=ledger):
            text, _segs = await self.toolbox._t_pick_sticker(self.ctx(), "无语")
        self.assertIn("描述：无语的样子", text)
        self.assertIn("会作为单独一条消息发出", text)

    async def test_schema_talks_about_description(self):
        pick = next(s for s in self.toolbox.schemas()
                    if s["function"]["name"] == "pick_sticker")
        self.assertIn("画面/情绪描述", pick["function"]["description"])
        self.assertIn("画面/情绪", pick["function"]["parameters"]["properties"]["query"]["description"])
        self.assertIn("默认少发", pick["function"]["description"])
        save = next(s for s in self.toolbox.schemas()
                    if s["function"]["name"] == "save_sticker")
        self.assertIn("画面描述", save["function"]["parameters"]["properties"]["desc"]["description"])


class StickerDefaultsTests(unittest.TestCase):
    def test_defaults_are_low_frequency(self):
        media_cfg = config_mod.DEFAULT["media"]
        self.assertEqual(media_cfg["sticker_chance"], 0.12)
        self.assertEqual(media_cfg["sticker_gap"], [3, 6])
        self.assertEqual(media_cfg["sticker_force_intensity"], "强烈")
        self.assertEqual(media_cfg["sticker_force_dominant"], ["兴奋", "烦躁", "感动"])
        self.assertEqual(media.DEFAULT_STICKER_CHANCE, 0.12)
        self.assertEqual(media.DEFAULT_STICKER_GAP, (3, 6))

    def test_forced_sticker_only_lasts_this_turn(self):
        cfg = {"media.sticker_gap": [3, 6], "media.sticker_chance": 0.0}
        ledger = media.MediaLedger(None, config=cfg)
        self.assertFalse(ledger.sticker_allowed("s"))
        ledger.force_sticker("s")
        self.assertTrue(ledger.sticker_allowed("s"))
        ledger.turn("s")
        self.assertFalse(ledger.sticker_allowed("s"))
        ledger.mark_sticker("s")
        self.assertFalse(ledger.sticker_allowed("s"))


class BypassRuleTests(unittest.TestCase):
    def test_force_conditions(self):
        cfg = {"media.sticker_force_intensity": "强烈",
               "media.sticker_force_dominant": ["兴奋", "烦躁", "感动"]}
        tool = ToolBox(SimpleNamespace(config=cfg))
        strong = SimpleNamespace(blend=lambda: {"dominant": "低落", "intensity": "强烈"})
        excited = SimpleNamespace(blend=lambda: {"dominant": "兴奋", "intensity": "轻微"})
        calm = SimpleNamespace(blend=lambda: {"dominant": "惊讶", "intensity": "轻微"})
        none = SimpleNamespace(blend=lambda: {"dominant": None, "intensity": None})
        self.assertTrue(tool._sticker_bypass(cfg, strong))
        self.assertTrue(tool._sticker_bypass(cfg, excited))
        self.assertFalse(tool._sticker_bypass(cfg, calm))
        self.assertFalse(tool._sticker_bypass(cfg, none))
        self.assertFalse(tool._sticker_bypass(cfg, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
