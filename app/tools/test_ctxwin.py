"""上下文窗口 A 侧：时间戳 + 认人。

覆盖：每行带时间戳、同日/跨日格式、uid 缺失标签、截断不切前缀、
同名不同尾号、自己不算进同名冲突、L0_HEADER 存在且被 build_messages 使用。
"""
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import ctxwin

CFG = {"chat.context_messages": 300, "chat.time_decay_hours": 6,
       "chat.context_chars": 12000, "chat.gap_mark_minutes": 10}


def _row(role, who, text, uid=None, ts=None):
    r = {"role": role, "who": who, "content": text, "ts": ts or time.time()}
    if uid is not None:
        r["uid"] = str(uid)
    return r


def _ts(y, mo, d, h, mi):
    """本地时间的 epoch 秒。"""
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


_TIME_RE = re.compile(r"^\[\d{2}:\d{2}\] ")
_DATE_TIME_RE = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\] ")


class TimeStampTests(unittest.TestCase):
    def test_every_line_has_timestamp(self):
        rows = [_row("user", "阿明", "早", 11112222, _ts(2026, 10, 24, 21, 0)),
                _row("assistant", "我", "早呀", None, _ts(2026, 10, 24, 21, 1)),
                _row("user", "小红", "来了", 55556666, _ts(2026, 10, 24, 21, 2))]
        cfg = dict(CFG, **{"chat.gap_mark_minutes": 9999})  # 别让间隔标记混进来
        text, _ = ctxwin.build(rows, cfg)
        for ln in text.splitlines():
            if ln.startswith("——"):
                continue
            self.assertTrue(_TIME_RE.match(ln) or _DATE_TIME_RE.match(ln),
                            f"这行没有时间前缀：{ln!r}")

    def test_same_day_format(self):
        ref = _ts(2026, 10, 24, 23, 0)
        rows = [_row("user", "阿明", "a", 11112222, _ts(2026, 10, 24, 21, 35))]
        text, _ = ctxwin.build(rows, dict(CFG, **{"chat.gap_mark_minutes": 9999}))
        self.assertIn("[21:35] 阿明(尾号2222)：a", text)
        self.assertNotIn("10-24", text)

    def test_cross_day_format(self):
        rows = [_row("user", "阿明", "昨晚的", 11112222, _ts(2026, 10, 23, 23, 50)),
                _row("user", "小红", "今天的", 55556666, _ts(2026, 10, 24, 9, 5))]
        text, _ = ctxwin.build(rows, dict(CFG, **{"chat.gap_mark_minutes": 9999}))
        self.assertIn("[10-23 23:50] 阿明(尾号2222)：昨晚的", text)
        self.assertIn("[09:05] 小红(尾号6666)：今天的", text)


class SpeakerTests(unittest.TestCase):
    def test_uid_missing_marks_unknown(self):
        self.assertEqual(ctxwin.speaker(_row("user", "阿明", "hi")), "阿明(尾号未知)")

    def test_self_label_kept(self):
        self.assertEqual(ctxwin.speaker(_row("assistant", "我", "嗯")), "我（大肥鱼）")

    def test_uid_tail_kept(self):
        self.assertEqual(ctxwin.speaker(_row("user", "阿明", "hi", 123456789)),
                         "阿明(尾号6789)")


class TruncationTests(unittest.TestCase):
    def test_truncation_never_cuts_prefix(self):
        # 20+ 小时前且跨天 → 权重远低于 0.12，单条 cap=40，正文被砍，但前缀必须完整
        long_text = "很长的正文" * 100
        rows = [_row("user", "阿明", long_text, 11112222, _ts(2026, 10, 23, 1, 0)),
                _row("assistant", "我", "哦", None, _ts(2026, 10, 24, 21, 0))]
        text, _ = ctxwin.build(rows, dict(CFG, **{"chat.gap_mark_minutes": 9999}))
        lines = [ln for ln in text.splitlines() if not ln.startswith("——")]
        first = lines[0]
        self.assertTrue(first.startswith("[10-23 01:00] 阿明(尾号2222)："), first[:80])
        self.assertIn("尾号2222", first)
        # 正文确实被截断（短于原文，且带省略号）
        self.assertLess(len(first.split("：", 1)[1]), 60)
        self.assertTrue(first.rstrip().endswith("…"))


class PresentTests(unittest.TestCase):
    def test_same_name_different_tail_flag_and_list_tails(self):
        rows = [_row("user", "阿明", "a", 11112222), _row("user", "小红", "b", 55556666),
                _row("assistant", "我", "c"), _row("user", "阿明", "d", 33334444)]
        out = ctxwin.present(rows)
        self.assertIn("阿明(尾号4444)", out)
        self.assertIn("阿明(尾号2222)", out)
        self.assertIn("不止一个人叫这个名字", out)
        self.assertIn("尾号：4444、2222", out)

    def test_self_not_counted_as_clash(self):
        # 有个群友昵称恰好也叫「大肥鱼」，机器人自己是「我（大肥鱼）」——
        # 只能算一个人，绝不能误报成「有两个大肥鱼」。
        rows = [_row("user", "大肥鱼", "a", 11112222),
                _row("assistant", "我", "b")]
        out = ctxwin.present(rows)
        self.assertIn("大肥鱼(尾号2222)", out)
        self.assertNotIn("不止一个人", out)
        self.assertNotIn("我（大肥鱼）(尾号", out)

    def test_uid_missing_shows_unknown_in_present(self):
        rows = [_row("user", "阿明", "a")]
        out = ctxwin.present(rows)
        self.assertIn("阿明(尾号未知)", out)


class HeaderTests(unittest.TestCase):
    def test_L0_HEADER_exists(self):
        self.assertIsInstance(ctxwin.L0_HEADER, str)
        self.assertTrue(ctxwin.L0_HEADER.strip())

    def test_L0_HEADER_covers_required_points(self):
        h = ctxwin.L0_HEADER
        for phrase in ("方括号", "时间", "尾号", "同名", "我（大肥鱼）",
                       "你自己发出去", "不要逐条回应", "引号"):
            self.assertIn(phrase, h)

    def test_build_messages_uses_L0_HEADER(self):
        rows = [_row("user", "阿明", "早", 11112222, _ts(2026, 10, 24, 21, 0))]
        msgs, _ = ctxwin.build_messages(rows, CFG)
        self.assertIsInstance(msgs, list)
        self.assertIsInstance(msgs[0], dict)
        self.assertIn(ctxwin.L0_HEADER, msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
