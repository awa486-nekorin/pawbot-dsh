"""Regression: 事实边界、简短表达和草稿收紧必须真的进入 prompt。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import draft, policy
from pawbot.paths import DataPaths
from pawbot.persona import Persona


class StyleGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = DataPaths(root=self.tmp.name, base_dir=self.tmp.name)
        self.paths.ensure_layout()
        self.persona = Persona(self.tmp.name, {}, paths=self.paths)
        Path(self.persona.persona_path).write_text(
            "# 测试人设\n你说话短，像群友随手回消息，嘴硬心软。\n", encoding="utf-8")

    def test_reality_boundary_is_injected_and_policy_stays_last(self):
        text = self.persona.build()
        self.assertIn("说话底线（代码固定", text)
        self.assertIn("刚在听歌", text)
        self.assertIn("被问「在吗」就回「在」", text)
        self.assertIn("不要解释上一句的用意", text)
        self.assertIn("8～25 字", text)
        self.assertTrue(text.rstrip().endswith(policy.system_policy_text().rstrip()))

    def test_draft_prompt_demands_one_short_sentence(self):
        self.assertIn("最多 40 字", draft._DRAFT_TASK)
        self.assertIn("不要自我辩护", draft._DRAFT_TASK)
        self.assertIn("可以带喵", draft._DRAFT_TASK)
        self.assertLessEqual(draft.DEFAULT_MAX_CHARS, 120)
        self.assertEqual(draft._max_chars({"chat.chat_draft_max_chars": 300}),
                         draft.MAX_DRAFT_CHARS)

    def test_cleanup_only_strips_catgirl_words_and_actions(self):
        prompt = draft._CLEANUP_SYSTEM
        self.assertIn("猫娘特有词汇", prompt)
        self.assertIn("括号里的动作", prompt)
        self.assertIn("一个字都不要改", prompt)
        self.assertIn("不要改写成中性平实", prompt)

    def test_code_strip_removes_cat_marks_and_actions_only(self):
        self.assertEqual(
            draft._strip_catgirl("（耳朵一耷拉）喵…其实没真在听啦，糊弄过去了喵。"),
            "其实没真在听啦，糊弄过去了。")
        self.assertEqual(
            draft._strip_catgirl("喵？没在听呀～你想听什么喵？"),
            "没在听呀～你想听什么？")
        self.assertEqual(
            draft._strip_catgirl("打拳累了就趴会儿，歇够再收拾～（甩甩尾巴）"),
            "打拳累了就趴会儿，歇够再收拾～")

    def test_final_block_forbids_expanding_explanations(self):
        block = draft.final_block("接住对方，别解释背景")
        self.assertIn("别展开草稿里的解释和背景", block)
        self.assertIn("不要自我辩护", block)


class DraftFactGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_draft_carries_fact_guard(self):
        class FakeLLM:
            def __init__(self):
                self.messages = []

            async def chat_message(self, messages, tools=None, **kwargs):
                self.messages = messages
                return {"content": "没在听，你听错了"}

        cfg = {"chat.chat_draft_max_chars": 120,
               "chat.chat_draft_persona": "你是一只猫娘。"}
        llm = FakeLLM()
        bot = SimpleNamespace(config=cfg, llm=llm)
        ctx = SimpleNamespace(sender_name="对方")
        text = await draft.content_draft(bot, ctx, "你还在听歌？", [], config=cfg)
        self.assertEqual(text, "没在听，你听错了")
        system = "\n".join(str(m.get("content") or "") for m in llm.messages
                           if m.get("role") == "system")
        self.assertIn("不要编造现实活动", system)
        self.assertIn("不要继续编", system)
        self.assertIn("你是一只猫娘", system)


if __name__ == "__main__":
    unittest.main()
