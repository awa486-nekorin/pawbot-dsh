"""第三輪：辨人（尾號標籤 / 在場名單）與去掉公式化反問。"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import ctxwin
from pawbot.style import polish

CFG = {"chat.context_messages": 300, "chat.time_decay_hours": 6,
       "chat.context_chars": 12000, "chat.gap_mark_minutes": 10}


def _row(role, who, text, uid=None, ts=None):
    r = {"role": role, "who": who, "content": text, "ts": ts or time.time()}
    if uid is not None:
        r["uid"] = str(uid)
    return r


class SpeakerTests(unittest.TestCase):
    def test_self_label(self):
        self.assertEqual(ctxwin.speaker(_row("assistant", "我", "嗯")), "我（大肥鱼）")

    def test_uid_tail(self):
        self.assertEqual(ctxwin.speaker(_row("user", "阿明", "hi", 123456789)), "阿明(尾号6789)")

    def test_no_uid_marks_unknown(self):
        self.assertEqual(ctxwin.speaker(_row("user", "阿明", "hi")), "阿明(尾号未知)")

    def test_build_uses_labels(self):
        rows = [_row("user", "阿明", "早", 11112222), _row("assistant", "我", "早呀"),
                _row("user", "阿明", "我也早", 33334444)]
        text, _ = ctxwin.build(rows, CFG)
        self.assertIn("阿明(尾号2222)：早", text)
        self.assertIn("我（大肥鱼）：早呀", text)
        self.assertIn("阿明(尾号4444)：我也早", text)

    def test_present_flags_name_clash(self):
        rows = [_row("user", "阿明", "a", 11112222), _row("user", "小红", "b", 55556666),
                _row("assistant", "我", "c"), _row("user", "阿明", "d", 33334444)]
        out = ctxwin.present(rows)
        self.assertIn("阿明(尾号4444)", out)
        self.assertIn("阿明(尾号2222)", out)
        self.assertIn("小红(尾号6666)", out)
        self.assertIn("不止一个人叫这个名字", out)
        self.assertLess(out.index("尾号4444"), out.index("尾号2222"))

    def test_present_dedups_same_uid(self):
        rows = [_row("user", "阿明", "a", 11112222), _row("user", "阿明改名", "b", 11112222)]
        out = ctxwin.present(rows)
        self.assertEqual(out.count("尾号2222"), 1)
        self.assertNotIn("不止一个人", out)

    def test_build_messages_appends_present(self):
        rows = [_row("user", "阿明", "早", 11112222)]
        msgs, _ = ctxwin.build_messages(rows, CFG)
        self.assertEqual(len(msgs), 2)
        self.assertIn("在场的人", msgs[1]["content"])


class TagQuestionTests(unittest.TestCase):
    def test_drops_trailing_ni_ne(self):
        self.assertEqual(polish("今天吃了火锅，好辣。你呢？"), "今天吃了火锅，好辣。")

    def test_drops_separate_part(self):
        self.assertEqual(polish("好困啊‖你呢？"), "好困啊")

    def test_drops_dui_ba(self):
        self.assertNotIn("对吧", polish("明天下雨吧，对吧？"))

    def test_keeps_lone_question(self):
        self.assertEqual(polish("你呢？"), "你呢？")

    def test_keeps_real_question(self):
        self.assertEqual(polish("你吃饭了吗？"), "你吃饭了吗？")
        self.assertEqual(polish("你是不是傻？"), "你是不是傻？")


if __name__ == "__main__":
    unittest.main()
