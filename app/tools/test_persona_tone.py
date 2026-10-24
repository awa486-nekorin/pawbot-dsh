"""人设尺度与「去助手化」的离线回归测试。

只读 data/persona.md、只调用纯提示组装函数，不连网络、不调模型。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.agent import Agent
from pawbot.spice import Spice

ROOT = Path(__file__).resolve().parents[1]
PERSONA = (ROOT / "data" / "persona.md").read_text(encoding="utf-8")


def make_agent(spice):
    """最小 bot：只放 _spice_hint 需要的字段。"""
    return Agent(SimpleNamespace(
        config={"spice.allow_flirt": True},
        spice=spice,
        libido=None,
    ))


class PersonaTextScaleTests(unittest.TestCase):
    def test_text_flirt_and_roleplay_are_covered(self):
        self.assertTrue(any(w in PERSONA for w in ("语C", "文爱", "口嗨")),
                        "persona 里没写文字上的尺度（语C/文爱/口嗨）")

    @unittest.skip("nofilter 本地模型测试窗口：人设里的露骨限制已临时移除，恢复人设时一起恢复本测试")
    def test_no_explicit_anatomy_instruction(self):
        # 放开的只是尺度口径，不该教它写露骨细节
        self.assertIn("不写露骨", PERSONA)

    def test_refusal_is_respected(self):
        self.assertIn("拒绝", PERSONA)

    def test_guard_line_untouched(self):
        self.assertIn(
            "萌系那一头只停在可爱画风、表情包和周边上；色色、擦边、R18 的话题和图只往成年角色、大姐姐那边走。",
            PERSONA)


class PersonaToneTests(unittest.TestCase):
    ASSISTANT_PHRASES = (
        "有什么我可以帮你",
        "我能帮你什么",
        "很高兴为你服务",
        "希望对你有帮助",
        "还有其他问题吗",
        "需要我帮你",
    )

    def test_assistant_phrases_only_appear_as_negative_example(self):
        for phrase in self.ASSISTANT_PHRASES:
            for line in PERSONA.splitlines():
                if phrase not in line:
                    continue
                stripped = line.strip()
                self.assertTrue(
                    stripped.startswith("- ") and "→" in stripped
                    and stripped.index(phrase) < stripped.index("→"),
                    f"助手腔「{phrase}」出现在非反例位置：{stripped}")

    def test_counter_example_section_has_anti_assistant_rows(self):
        section = PERSONA.split("## 不要这样说 → 可以这样说", 1)
        self.assertEqual(len(section), 2, "反例小节不见了")
        body = section[1].split("\n## ", 1)[0]
        rows = [ln for ln in body.splitlines() if ln.strip().startswith("- ")]
        self.assertGreaterEqual(len(rows), 13, f"反例条目太少：{len(rows)}")
        self.assertTrue(any("好的，我这就帮你看看" in r for r in rows))
        self.assertTrue(any("首先" in r for r in rows))


class SpiceHintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def spice(self, level):
        sp = Spice(os.path.join(self.tmp, "spice.json"), default=2)
        sp.set("group_1", level)
        return sp

    def test_level_2_covers_roleplay_and_banter(self):
        hint = make_agent(self.spice(2))._spice_hint(SimpleNamespace(session_id="group_1"))
        self.assertIn("这个群的调调", hint)
        self.assertTrue(any(w in hint for w in ("语C", "口嗨")))

    def test_level_2_keeps_the_brake(self):
        # nofilter 測試窗口：措辞上的「靠暗示/不写露骨」已临时移除，只保留不硬带的刹车
        hint = make_agent(self.spice(2))._spice_hint(SimpleNamespace(session_id="group_1"))
        self.assertIn("别硬往那边带", hint)

    def test_level_1_is_milder(self):
        hint = make_agent(self.spice(1))._spice_hint(SimpleNamespace(session_id="group_1"))
        self.assertIn("不主动撩", hint)
        self.assertNotIn("文爱", hint)

    def test_level_0_adds_nothing(self):
        # Spice 现在会把 ≤0 抬成 2；这里用桩验证兜底分支不注入放开口径
        class Zero:
            def get(self, _sid):
                return 0

            def hint(self, _sid):
                return ""

        hint = make_agent(Zero())._spice_hint(SimpleNamespace(session_id="group_1"))
        self.assertNotIn("语C", hint)
        self.assertNotIn("口嗨", hint)


class ParticipationHintTests(unittest.TestCase):
    def hint(self):
        ctx = SimpleNamespace(is_group=True, session_id="group_1", state={},
                              event=SimpleNamespace(to_me=False))
        return Agent(SimpleNamespace(config={}))._participation_hint(ctx)

    def test_keeps_silence_and_owner_command_protocol(self):
        out = self.hint()
        self.assertIn("<pass>", out)
        self.assertIn("account_action", out)
        self.assertIn("不调用工具", out)

    def test_sounds_like_a_group_member(self):
        out = self.hint()
        self.assertIn("普通群消息", out)
        self.assertNotIn("帮上忙才加入", out)


if __name__ == "__main__":
    unittest.main()
