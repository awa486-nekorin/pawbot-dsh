"""Offline privacy/authorization regressions for proactive Space actions."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.proactive import Proactive


def schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_composition_never_reads_private_space(self):
        for name in ("qzone_list", "qzone_comments"):
            with self.subTest(name=name):
                seen = []
                async def chat(messages, tools=None):
                    seen.append((list(messages), tools))
                    if len(seen) == 1:
                        return {"tool_calls": [{"id": "q1", "function": {"name": name, "arguments": '{}'}}]}
                    return {"content": "普通群聊回复"}
                execute = AsyncMock(return_value=("SECRET FEED", []))
                bot = SimpleNamespace(
                    config={"tools.enabled": True, "tools.max_rounds": 1, "life.enabled": False},
                    topic=SimpleNamespace(digest=lambda *a: ""),
                    mood=SimpleNamespace(hint=lambda: ""),
                    persona=SimpleNamespace(build=lambda **kw: kw["extra"]),
                    store=SimpleNamespace(summary=lambda *a: "", turn_summary=lambda *a: "", window=lambda *a: []),
                    llm=SimpleNamespace(chat_message=chat),
                    toolbox=SimpleNamespace(schemas=lambda: [schema(name), schema("web_search")], execute=execute),
                )
                text, media = await Proactive(bot)._compose(1, "group_1", {"kind": "join", "hint": ""})
                self.assertEqual(text, "普通群聊回复")
                self.assertEqual(media, [])
                self.assertNotIn(name, [s["function"]["name"] for s in seen[0][1]])
                execute.assert_not_awaited()
                self.assertNotIn("SECRET FEED", str(seen))
                self.assertIn("不能把私人空间", str(seen[1][0]))


class ProactiveGateTests(unittest.IsolatedAsyncioTestCase):
    async def compose(self, name, config=None, after_schema=None):
        seen = []
        cfg = {"tools.enabled": True, "tools.max_rounds": 1, "life.enabled": False,
               "qzone.enabled": True, **(config or {})}
        async def chat(messages, tools=None):
            seen.append((list(messages), tools))
            if len(seen) == 1:
                if after_schema:
                    after_schema(cfg)
                return {"tool_calls": [{"id": "q1", "function": {"name": name, "arguments": '{}'}}]}
            return {"content": "done"}
        execute = AsyncMock(return_value=("real result", []))
        bot = SimpleNamespace(
            config=cfg, topic=SimpleNamespace(digest=lambda *a: ""),
            mood=SimpleNamespace(hint=lambda: ""), persona=SimpleNamespace(build=lambda **kw: kw["extra"]),
            store=SimpleNamespace(summary=lambda *a: "", turn_summary=lambda *a: "", window=lambda *a: []),
            llm=SimpleNamespace(chat_message=chat),
            toolbox=SimpleNamespace(schemas=lambda: [schema(name), schema("web_search")], execute=execute))
        await Proactive(bot)._compose(1, "group_1", {"kind": "join", "hint": ""})
        return seen, execute

    async def test_default_off_hides_and_rejects_every_space_call(self):
        for name in ("post_qzone", "qzone_edit", "qzone_delete", "qzone_reply",
                     "qzone_like", "qzone_repost", "qzone_future_action", "qzone_list", "qzone_comments"):
            with self.subTest(name=name):
                seen, execute = await self.compose(name)
                self.assertNotIn(name, [s["function"]["name"] for s in seen[0][1]])
                execute.assert_not_awaited()

    async def test_gate_requires_true_and_existing_feature_gate(self):
        for config in ({"qzone.proactive_enabled": "true"},
                       {"qzone.proactive_enabled": 1},
                       {"qzone.proactive_enabled": True, "qzone.enabled": False}):
            seen, execute = await self.compose("post_qzone", config)
            execute.assert_not_awaited()

    async def test_explicit_gate_does_not_invent_verified_actor(self):
        seen, execute = await self.compose("post_qzone", {"qzone.proactive_enabled": True})
        self.assertIn("post_qzone", [s["function"]["name"] for s in seen[0][1]])
        execute.assert_awaited_once_with(None, "post_qzone", {})

    async def test_disabling_gate_after_schema_still_rejects(self):
        seen, execute = await self.compose("post_qzone", {"qzone.proactive_enabled": True},
                                          lambda cfg: cfg.update({"qzone.proactive_enabled": False}))
        execute.assert_not_awaited()

    async def test_private_group_reads_stay_blocked_when_opted_in(self):
        for name in ("qzone_list", "qzone_comments"):
            seen, execute = await self.compose(name, {"qzone.proactive_enabled": True})
            execute.assert_not_awaited()
            self.assertNotIn(name, [s["function"]["name"] for s in seen[0][1]])

    async def test_non_qzone_tools_unchanged(self):
        _, execute = await self.compose("web_search")
        execute.assert_awaited_once_with(None, "web_search", {})


if __name__ == "__main__":
    unittest.main()
