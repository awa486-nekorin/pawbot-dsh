"""离线测试：语C / 暧昧的清理规则。

语C 不写括号里的神态动作：模型就算写了也在发送前去掉，只留说出口的话
（拟声词、喘息、结巴、拖音）。语C 时不拆条、不去引号、不删结巴、不剪追问；
普通闲聊的清理照旧。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.style import (
    ROLEPLAY_MAX_CHARS,
    _is_roleplay,
    cap_sentences,
    drop_trailing_question,
    polish,
    strip_actions,
)

# 模型老毛病：仍然写了括号旁白
WITH_ACTIONS = ("（用力扭动身体，绳子发出吱嘎声）哼！区区人类竟敢把本小姐绑起来！"
                "（别过脸去，语气倔强）我才不会认错呢！")
WITH_ACTIONS_CLEAN = "哼！区区人类竟敢把本小姐绑起来！我才不会认错呢！"

# 想要的写法：只靠声音
VOICE = "呜……不、不要挠……哈、哈哈……你这个坏人……停、停下啦～"
MOAN = "嗯……啊……♡ 不、不行了……"


class DetectionTests(unittest.TestCase):
    def test_voice_markers_count_as_roleplay(self):
        self.assertTrue(_is_roleplay(VOICE))
        self.assertTrue(_is_roleplay(MOAN))

    def test_leftover_action_also_counts(self):
        self.assertTrue(_is_roleplay(WITH_ACTIONS))

    def test_normal_chat_does_not(self):
        for text in ("（笑）好呀", "今天吃了火锅，好辣。", "嗯……行吧。", "", "（）"):
            with self.subTest(text=text):
                self.assertFalse(_is_roleplay(text))


class StripActionsTests(unittest.TestCase):
    def test_strips_parenthetical_actions(self):
        self.assertEqual(strip_actions(WITH_ACTIONS), WITH_ACTIONS_CLEAN)

    def test_keeps_single_char_and_voice(self):
        self.assertEqual(strip_actions("（笑）好呀"), "（笑）好呀")
        self.assertEqual(strip_actions(VOICE), VOICE)


class RoleplayPolishTests(unittest.TestCase):
    def test_actions_removed_on_send(self):
        out = polish(WITH_ACTIONS)
        self.assertEqual(out, WITH_ACTIONS_CLEAN)
        self.assertNotIn("（", out)

    def test_voice_survives_untouched(self):
        self.assertEqual(polish(VOICE), VOICE)
        self.assertEqual(polish(MOAN), MOAN)

    def test_newlines_joined_not_split(self):
        out = polish(VOICE + "\n" + MOAN)
        self.assertNotIn("\n", out)
        self.assertNotIn("‖", out)
        self.assertEqual(out, VOICE + MOAN)

    def test_trailing_question_kept(self):
        source = "谁、谁是肥鱼啊……你说呢？"
        self.assertEqual(polish(source), source)

    def test_idempotent(self):
        for source in (VOICE, MOAN, WITH_ACTIONS):
            with self.subTest(source=source):
                first = polish(source)
                self.assertEqual(polish(first), first)


class RoleplayCapsTests(unittest.TestCase):
    def test_cap_sentences_keeps_whole_roleplay(self):
        self.assertEqual(cap_sentences(VOICE, 1), VOICE)
        long_sample = VOICE * 4
        self.assertGreater(len(long_sample), 100)
        self.assertLessEqual(len(long_sample), ROLEPLAY_MAX_CHARS)
        self.assertEqual(cap_sentences(long_sample, 1), long_sample)

    def test_roleplay_hard_limit(self):
        huge = VOICE * 40
        out = cap_sentences(huge, 1)
        self.assertLessEqual(len(out), ROLEPLAY_MAX_CHARS + 2)

    def test_drop_trailing_question_leaves_roleplay_alone(self):
        text = "呜……不、不要……你快说嘛？"
        self.assertEqual(drop_trailing_question(text), text)


class NormalChatStillCleanedTests(unittest.TestCase):
    def test_newline_still_becomes_split_mark(self):
        self.assertEqual(polish("一。\n二。"), "一。‖二。")

    def test_cap_sentences_still_caps(self):
        text = "现在都快夜里十一点了诶。你这是刚睡醒还是打错字了，不会连晚饭都没吃吧？"
        self.assertEqual(cap_sentences(text, 1), "现在都快夜里十一点了诶。")

    def test_drop_trailing_question_still_drops(self):
        self.assertEqual(drop_trailing_question("肯定是海蓝色呀。快跟我讲讲嘛"), "肯定是海蓝色呀。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
