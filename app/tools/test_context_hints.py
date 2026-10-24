"""上下文提示：引用谁的消息、群里最近聊的条数。离线。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.agent import Agent


def ctx(sender="主人", quoted="", quoted_from="", is_group=True):
    return SimpleNamespace(
        sender_name=sender, is_group=is_group, session_id="group_1", state={},
        event=SimpleNamespace(to_me=False, quoted_text=quoted, quoted_from=quoted_from,
                              image_desc="", person_card=""),
        bot=None)


class ToolHintTests(unittest.TestCase):
    def test_uncertain_facts_must_be_searched(self):
        bot = SimpleNamespace(config={}, stickers=SimpleNamespace(files=lambda: []),
                              qzone=SimpleNamespace(enabled=False), spice=SimpleNamespace())
        hint = Agent(bot)._tool_hint(True)
        self.assertIn("不确定就先搜", hint)
        self.assertIn("不要用好像、大概、应该", hint)
        self.assertIn("查不到就说没查到", hint)


class SenseHintTests(unittest.TestCase):
    def setUp(self):
        self.digest = Mock(return_value="阿明：在吗\n小红：在")
        self.bot = SimpleNamespace(config={"topic.enabled": True, "topic.digest_lines": 12},
                                   topic=SimpleNamespace(digest=self.digest))
        self.agent = Agent(self.bot)

    def test_quote_names_who_quoted_and_whose_message(self):
        hint = self.agent._sense_hint(ctx(sender="主人", quoted="周末去爬山吗",
                                          quoted_from="一碗麦西粉"))
        self.assertIn("主人 引用了 一碗麦西粉 的消息", hint)
        self.assertIn("周末去爬山吗", hint)
        self.assertIn("不要当成别人说的", hint)

    def test_recent_group_chat_uses_configured_line_count(self):
        hint = self.agent._sense_hint(ctx())
        self.digest.assert_called_once_with("group_1", 12)
        self.assertIn("群里刚才聊的", hint)
        self.assertIn("阿明：在吗", hint)

    def test_private_chat_has_no_group_digest(self):
        hint = self.agent._sense_hint(ctx(is_group=False))
        self.digest.assert_not_called()
        self.assertNotIn("群里刚才聊的", hint)


if __name__ == "__main__":
    unittest.main()
