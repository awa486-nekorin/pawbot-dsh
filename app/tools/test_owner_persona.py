"""主人身份注入：persona.owner_line + agent._owner_hint。离线。

只调纯函数，不连网络、不调模型；owner_ids 全部用桩注入，不读真实 data/config.json。
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.agent import Agent
from pawbot.persona import Persona

ROOT = Path(__file__).resolve().parents[1]


class FakeConfig:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)


class OwnerLineTests(unittest.TestCase):
    def _persona(self, owners):
        return Persona(str(ROOT), FakeConfig({"account.owner_ids": owners}))

    def test_single_owner_lists_tail_and_dad_role(self):
        line = self._persona(["1000000001"]).owner_line()
        self.assertIn("0001", line)
        self.assertIn("老爹", line)
        self.assertIn("生活费", line)
        self.assertIn("token", line)
        self.assertIn("大白饭", line)
        self.assertIn("不是", line)
        self.assertNotIn("未知", line)

    def test_multiple_owners_all_listed(self):
        line = self._persona(["1000000001", "1000000002"]).owner_line()
        self.assertIn("0001", line)
        self.assertIn("0002", line)

    def test_no_owner_ids_injects_nothing(self):
        self.assertEqual(self._persona([]).owner_line(), "")
        self.assertEqual(self._persona(None).owner_line(), "")

    def test_build_includes_owner_line_when_present(self):
        out = self._persona(["1000000001"]).build(group_id=1, sender="阿明")
        self.assertIn("## 你的主人", out)
        self.assertIn("0001", out)

    def test_build_without_owner_has_no_owner_section(self):
        out = self._persona([]).build(group_id=1, sender="阿明")
        self.assertNotIn("## 你的主人", out)


class OwnerHintTests(unittest.TestCase):
    def _agent(self, owners):
        return Agent(SimpleNamespace(config=FakeConfig({"account.owner_ids": owners})))

    def test_owner_message_gets_hint(self):
        hint = self._agent(["1000000001"])._owner_hint(
            SimpleNamespace(user_id="1000000001"))
        self.assertIn("这条是你主人说的", hint)
        self.assertIn("老爹", hint)
        self.assertIn("生活费", hint)
        self.assertIn("token", hint)
        self.assertIn("大白饭", hint)
        self.assertIn("撒娇", hint)
        self.assertIn("男朋友", hint)

    def test_non_owner_gets_nothing(self):
        hint = self._agent(["1000000001"])._owner_hint(SimpleNamespace(user_id="123456"))
        self.assertEqual(hint, "")

    def test_no_owners_configured(self):
        hint = self._agent([])._owner_hint(SimpleNamespace(user_id="1000000001"))
        self.assertEqual(hint, "")

    def test_sense_hint_injects_owner_line_for_owner_only(self):
        bot = SimpleNamespace(config=FakeConfig(
            {"account.owner_ids": ["1000000001"], "topic.enabled": False}))
        agent = Agent(bot)

        def mk(user_id):
            return SimpleNamespace(
                sender_name="主人", is_group=True, session_id="g1", state={},
                user_id=user_id, event=SimpleNamespace())

        self.assertIn("这条是你主人说的", agent._sense_hint(mk("1000000001")))
        self.assertEqual(agent._sense_hint(mk("123456")), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
