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
        self.assertIn("不是", line)
        self.assertNotIn("未知", line)
        # 资源关系不再写进常驻主人提示：不吃白饭、不讨生活费。
        for stale in ("生活费", "token", "大白饭", "白饭", "讨"):
            self.assertNotIn(stale, line)

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
        # v3 人设自带一节通用的「你的主人」（不写死号码）；没配 owner_ids 时，
        # 不能注入「你的主人是 …」这行具体身份，也不能出现具体尾号。
        self.assertNotIn("你的主人是", out)
        self.assertNotIn("0001", out)

    def test_build_uses_current_self_entries_not_recent_facts(self):
        """当前自我条目进入 prompt；旧 facts 不再按最近日期保底全灌。"""
        persona = self._persona([])
        persona.facts = lambda: "- 很旧的无关事实：上周吃了面"
        persona.life_store = SimpleNamespace(self_entries=lambda **_: [{
            "id": "self-1", "version": 2, "category": "preference",
            "text": "被具体经历改过：现在更喜欢先查清楚再接话",
        }])
        out = persona.build(group_id=1, sender="阿明")
        self.assertIn("现在更喜欢先查清楚再接话", out)
        self.assertNotIn("很旧的无关事实", out)
        self.assertIn("初始气质", out)

    def test_intent_only_when_relevant_and_uses_self_goal(self):
        persona = self._persona([])
        persona.life_store = SimpleNamespace(
            self_entries=lambda **_: [{
                "id": "self-1", "version": 1, "category": "habit",
                "text": "说好要自己先查机械臂资料"}],
            goals=lambda **_: [{
                "id": "goal-1", "version": 1, "status": "active",
                "text": "把扫地机器人改成坐骑"}],
        )
        self.assertEqual(persona.intent("今天天气不错"), "")
        intent = persona.intent("你那个机械臂到底查清楚没有")
        self.assertIn("机械臂", intent)

    def test_resource_hint_only_on_relevant_input(self):
        persona = self._persona([])
        self.assertEqual(persona.resource_hint("今天吃啥"), "")
        self.assertEqual(persona.resource_hint("你好呀"), "")
        hint = persona.resource_hint("你的 token 还剩多少")
        self.assertIn("token", hint)
        self.assertIn("🍚", hint)
        self.assertIn("不补充", hint)
        self.assertIn("保持未知", hint)


class OwnerHintTests(unittest.TestCase):
    def _agent(self, owners):
        return Agent(SimpleNamespace(config=FakeConfig({"account.owner_ids": owners})))

    def test_owner_message_gets_hint(self):
        hint = self._agent(["1000000001"])._owner_hint(
            SimpleNamespace(user_id="1000000001"))
        self.assertIn("这条是你主人说的", hint)
        self.assertIn("老爹", hint)
        self.assertIn("男朋友", hint)
        for stale in ("生活费", "token", "大白饭", "白饭", "讨"):
            self.assertNotIn(stale, hint)

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
