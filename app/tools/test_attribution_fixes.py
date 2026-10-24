"""2026-10-25 修复的回归测试：P0-1 / P1-2 / P1-3 / P1-4 / P1-5 / P1-6。

覆盖：polish 不静默变空、跨 ‖ 的身体反应额度、`不是A而是B` 状态词白名单、
turns._render 按整行截尾、annotate_last 的归属/多图逐格、
_user_message 的图片编号与真实附图对齐。全部离线。
"""
import os
import re
import sys
import tempfile
import time
import unittest
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import turns
from pawbot.agent import Agent
from pawbot.store import Store
from pawbot.style import polish

URL = "data:image/jpeg;base64,AAAA"


class PolishNotEmptyTests(unittest.TestCase):
    """P0-1：清理完是空串时必须退回原始输入（被 @ 时不能一声不吭）。"""

    def test_known_empty_cases_fall_back_to_original(self):
        for src in ("（脸红）", "（别过脸去）", "😀😀",
                    "我在这里。我会一直陪着你。"):
            with self.subTest(source=src):
                self.assertEqual(polish(src), src)

    def test_separator_only_is_still_empty(self):
        self.assertEqual(polish("‖‖"), "")


class CrossPartReactionTests(unittest.TestCase):
    """P1-2：额度按整条回覆算，单条/多条 결과一致。"""

    def test_single_part_and_split_parts_agree(self):
        self.assertEqual(polish("我脸红了。‖心跳好快。尾巴在摇。"), "我脸红了。")
        self.assertEqual(polish("我脸红了。‖心跳好快。‖尾巴在摇。"), "我脸红了。")

    def test_part_with_text_keeps_text(self):
        self.assertEqual(polish("我脸红了。‖心跳好快。这题选 B。"),
                         "我脸红了。‖这题选 B。")


class NotButWhitelistTests(unittest.TestCase):
    """P1-3：A 不是状态/感受词时保留原句，别改意思。"""

    def test_denials_and_objects_are_kept(self):
        for src in ("不是我的错，而是你的错", "不是不行，是需要时间",
                    "不是要你道歉，而是要你改"):
            with self.subTest(source=src):
                self.assertEqual(polish(src), src)

    def test_state_words_are_rewritten(self):
        self.assertEqual(polish("不是懒，而是累"), "只是累")
        self.assertEqual(polish("不是太累，而是真困了。"), "只是真困了。")


class RenderTruncationTests(unittest.TestCase):
    """P1-4：按整行从尾取，不能切出没有 [HH:MM] 前缀的半行。"""

    def _rows(self):
        now = time.time()
        return [
            {"role": "user", "who": "小红", "uid": "6666", "ts": now,
             "content": "今天的回覆" * 5},
            {"role": "assistant", "ts": now, "content": "刚才说的那句"},
        ]

    def test_no_prefixless_half_line(self):
        out = turns._render(self._rows(), 30)
        self.assertTrue(out)
        for line in out.splitlines():
            self.assertRegex(line, r"^\[\d{2}:\d{2}\] ")

    def test_tail_lines_are_whole(self):
        now = time.time()
        rows = [{"role": "user", "who": "阿明", "uid": "1234", "ts": now,
                 "content": f"第{i}句话" * 3} for i in range(5)]
        out = turns._render(rows, 120)
        self.assertTrue(out)
        for line in out.splitlines():
            self.assertRegex(line, r"^\[\d{2}:\d{2}\] ")
        self.assertLessEqual(len(out.splitlines()), 5)

    def test_short_input_is_untouched(self):
        now = time.time()
        rows = [{"role": "user", "who": "阿明", "uid": "1234", "ts": now,
                 "content": "你好"}]
        self.assertIn("你好", turns._render(rows))


class AnnotateLastFixTests(unittest.TestCase):
    """P1-5：uid 空不贴、多图逐格、他人行不动。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "s.json"))

    def _content(self, idx=0):
        return self.store.window("g")[idx]["content"]

    def test_empty_uid_is_never_attributed(self):
        self.store.append("g", "user", "看这个[图片]", who="小红", uid=99)
        self.assertFalse(self.store.annotate_last("g", "", "阿明刚发的图"))
        self.assertFalse(self.store.annotate_last("g", None, "阿明刚发的图"))
        self.assertEqual(self._content(), "看这个[图片]")

    def test_multi_image_filled_cell_by_cell(self):
        self.store.append("g", "user", "看[图片][图片][图片]", who="阿明", uid=42)
        self.assertTrue(self.store.annotate_last("g", 42, ["第一张", "第二张", "第三张"]))
        self.assertEqual(self._content(),
                         "看[图片：第一张][图片：第二张][图片：第三张]")
        # 三格都填完了，再调用不能乱贴，也不该 False 之后又改内容
        self.assertFalse(self.store.annotate_last("g", 42, ["第四张"]))
        self.assertEqual(self._content(),
                         "看[图片：第一张][图片：第二张][图片：第三张]")

    def test_str_fills_first_cell_then_next_call_fills_next(self):
        self.store.append("g", "user", "[图片][图片]", who="阿明", uid=42)
        self.assertTrue(self.store.annotate_last("g", 42, "第一张"))
        self.assertEqual(self._content(), "[图片：第一张][图片]")
        self.assertTrue(self.store.annotate_last("g", 42, "第二张"))
        self.assertEqual(self._content(), "[图片：第一张][图片：第二张]")

    def test_extra_descriptions_appended_with_semicolon(self):
        self.store.append("g", "user", "[图片]", who="阿明", uid=42)
        self.assertTrue(self.store.annotate_last("g", 42, ["一", "二", "三"]))
        self.assertEqual(self._content(), "[图片：一]；二；三")

    def test_other_person_row_is_untouched(self):
        self.store.append("g", "user", "别人的[图片]", who="小红", uid=99)
        self.store.append("g", "user", "我的[图片]", who="阿明", uid=42)
        self.assertTrue(self.store.annotate_last("g", 42, "我的描述"))
        self.assertEqual(self.store.window("g")[0]["content"], "别人的[图片]")
        self.assertEqual(self.store.window("g")[1]["content"],
                         "我的[图片：我的描述]")


class UserMessageNumberingTests(unittest.TestCase):
    """P1-6：没有 url 的图不占编号，编号数量必须等于真实附图数量。"""

    def _agent(self, items):
        agent = Agent.__new__(Agent)
        agent.bot = SimpleNamespace(config={},
                                    recent_images={"group_1": deque(items, maxlen=6)})
        return agent

    def _ctx(self):
        ev = SimpleNamespace(to_me=False, quoted_text="", quoted_from="",
                             image_desc="", image_data=[], quote_image_data=[],
                             forward_image_data=[], image_items=[],
                             quoted_is_self=False, person_card="", forward_text="",
                             card_text="", talked_about=False, voice_text="",
                             video_desc="")
        return SimpleNamespace(is_group=True, sender_name="阿明", user_id=123456,
                               session_id="group_1", group_id=1, state={}, event=ev)

    def _message(self, items):
        agent = self._agent(items)
        return agent._user_message(self._ctx(), "上面那张图是啥",
                                   {"who": "阿明", "uid": "123456"})

    def test_placeholder_without_url_does_not_shift_numbering(self):
        msg = self._message([
            {"url": URL + "cat", "who": "阿明", "uid": "1111", "desc": "猫"},
            {"url": "", "who": "我（大肥鱼）", "self": True,
             "kind": "sticker", "desc": ""},
            {"url": URL + "dog", "who": "小红", "uid": "6666", "desc": "狗"},
        ])
        blocks = msg["content"]
        urls = [b["image_url"]["url"] for b in blocks if b["type"] == "image_url"]
        # 只附了两张真图：猫（阿明）、狗（小红）
        self.assertEqual(urls, [URL + "cat", URL + "dog"])
        text = blocks[0]["text"]
        numbered = [ln for ln in text.splitlines() if re.match(r"^\d+\.\s", ln)]
        self.assertEqual(len(numbered), len(urls))
        self.assertEqual(numbered[0], "1. 阿明(尾号1111) 发的：猫")
        self.assertEqual(numbered[1], "2. 小红(尾号6666) 发的：狗")
        # 没有原图的那张另起一句，不混进编号
        self.assertIn("还有一张（没附原图）：表情包", text)

    def test_all_placeholders_attach_nothing(self):
        msg = self._message([
            {"url": "", "who": "我（大肥鱼）", "self": True,
             "kind": "sticker", "desc": ""},
        ])
        self.assertIsInstance(msg["content"], str)
        self.assertIn("下面没附原图", msg["content"])
        self.assertFalse([ln for ln in msg["content"].splitlines()
                          if re.match(r"^\d+\.\s", ln)])
        self.assertIn("还有一张（没附原图）：表情包", msg["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
