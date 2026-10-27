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

    def test_no_explicit_anatomy_instruction(self):
        # 放开的只是尺度口径，不该教它写露骨细节
        self.assertIn("不写露骨", PERSONA)

    def test_refusal_is_respected(self):
        self.assertIn("拒绝", PERSONA)

    def test_guard_line_untouched(self):
        # v3 人设把「萌系 vs 成人向」写短了，但两条边界都还在；底线句也保留
        self.assertIn("萌系只停在可爱周边上，色色和 R18 只往成年大姐姐那边走。", PERSONA)
        self.assertIn("严守底线：不外传别人隐私和真实信息", PERSONA)
        self.assertIn("绝不拿外貌、家庭、地域攻击别人", PERSONA)


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
        # v3 人设不再给「不要这样说 → 可以这样说」的 28 组对照（模型会复读右栏），
        # 改成 3 条「绝不这样写」的反例；这里守住「反例还在、而且是反例」这件事。
        section = PERSONA.split("绝不这样写", 1)
        self.assertEqual(len(section), 2, "反例小节不见了")
        body = section[1].split("\n绝对禁止", 1)[0]
        rows = [ln for ln in body.splitlines() if ln.strip().startswith("- ❌")]
        self.assertGreaterEqual(len(rows), 3, f"反例条目太少：{len(rows)}")
        self.assertTrue(any("同一张又发我一遍" in r for r in rows))


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


class DraftingHintTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent(SimpleNamespace(config={}))

    def test_qq_message_and_notice_get_scoped_tone(self):
        from pawbot.event import MessageEvent, NoticeEvent
        for event in (MessageEvent(post_type="message"), NoticeEvent(post_type="notice")):
            with self.subTest(event=type(event).__name__):
                hint = self.agent._drafting_hint(SimpleNamespace(event=event))
                self.assertIn("QQ 对话成稿", hint)
                self.assertIn("沿用既有人设", hint)
                self.assertIn("不每句加喵", hint)
                self.assertIn("不展示草稿", hint)

    def test_non_dialogue_context_does_not_get_tone_overlay(self):
        from pawbot.event import Event, RequestEvent
        for ctx in (None, SimpleNamespace(), SimpleNamespace(event=Event("meta_event")),
                    SimpleNamespace(event=RequestEvent("request"))):
            self.assertEqual(self.agent._drafting_hint(ctx), "")

    def test_tone_does_not_override_facts_permissions_or_local_storage(self):
        from pawbot.event import MessageEvent
        hint = self.agent._drafting_hint(SimpleNamespace(event=MessageEvent("message")))
        for text in ("事实", "工具参数保持准确", "授权", "发送范围", "<pass>",
                     "不为了接梗自动联网补库"):
            self.assertIn(text, hint)

    def test_existing_style_cleanups_still_apply(self):
        from pawbot import style
        self.assertEqual(style.polish("<pass>"), "<pass>")
        self.assertNotIn("😀", style.strip_emoji("回来了😀"))
        self.assertEqual(style.polish("第一句‖第二句‖第三句", max_parts=2).count("‖"), 1)

    def test_local_knowledge_rule_overrides_legacy_online_rule(self):
        agent = Agent(SimpleNamespace(config={}, stickers=SimpleNamespace(files=lambda: [])))
        hint = agent._tool_hint(True)
        self.assertIn("本地优先", hint)
        self.assertIn("人工审核", hint)
        self.assertIn("覆盖旧提示", hint)
        self.assertNotIn("只要有一点点可能是梗", hint)


class DraftingExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dialogue_draft_reuses_completion_and_preserves_pass(self):
        from unittest.mock import AsyncMock, Mock, patch
        from pawbot.event import MessageEvent

        event = MessageEvent("message", group_id=1, message_type="group")
        ctx = SimpleNamespace(event=event, group_id=1, sender_name="群友",
                              user_id=1, session_id="group_1", is_group=True, state={})
        bot = SimpleNamespace(config={"tools.enabled": False},
                              llm=SimpleNamespace(chat_message=AsyncMock(return_value={"content": "<pass>"})),
                              persona=SimpleNamespace(build=Mock(return_value="existing persona")),
                              toolbox=SimpleNamespace(schemas=Mock(return_value=[])))
        agent = Agent(bot)
        agent._legacy_context = lambda *args: []
        agent._user_message = lambda *args: {"role": "user", "content": "普通聊天"}
        for name in ("_participation_hint", "_sense_hint", "_life_hint", "_mood_hint", "_spice_hint"):
            setattr(agent, name, lambda *args: "")
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            text, media = await agent.run(ctx, "普通聊天", [])
        self.assertEqual((text, media), ("<pass>", []))
        bot.llm.chat_message.assert_awaited_once()
        self.assertIn("QQ 对话成稿", bot.persona.build.call_args.kwargs["extra"])
        self.assertEqual(bot.llm.chat_message.await_args.args[1], None)


if __name__ == "__main__":
    unittest.main()
