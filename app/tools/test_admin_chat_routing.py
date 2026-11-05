"""Regression: owner messages must fall through to ai_chat when not a command."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.bus import BUS, Bus, Handler
from plugins import admin_cmds, ai_chat  # noqa: F401  (registration side effects)


class NaturalCommandRoutingTests(unittest.TestCase):
    def test_natural_command_handler_does_not_block_chat(self):
        rows = [h for h in BUS.handlers if h.name == "admin_natural_command"]
        self.assertTrue(rows, "admin_natural_command 没注册")
        self.assertFalse(rows[0].block,
                         "admin_natural_command 不能阻断后续 ai_chat；真命令应自己 ctx.stop()")

    def test_chat_handler_is_registered_after_natural_command(self):
        names = [(h.name, h.priority, h.block) for h in BUS.handlers]
        self.assertIn("ai_chat", [row[0] for row in names])


class _Ctx:
    def __init__(self, event):
        self.event = event
        self.stopped = False
        self.state = {}
        self.matched_command = ""
        self.session_id = "group_1"

    def stop(self):
        self.stopped = True


class BusFallthroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_blocking_handler_lets_chat_run(self):
        bus = Bus()
        order = []

        async def first(ctx):
            order.append("natural")
            ctx.state["judged"] = True

        async def chat(ctx):
            order.append("chat")

        bus.register(Handler(fn=first, kind="message", priority=8, block=False))
        bus.register(Handler(fn=chat, kind="message", priority=90))
        event = SimpleNamespace(type="message", is_group=True, to_me=True,
                                role="owner", self_id=1, user_id=2)
        ctx = _Ctx(event)
        ran = await bus.dispatch(ctx)
        self.assertEqual(order, ["natural", "chat"])
        self.assertEqual(ran, 2)
        self.assertTrue(ctx.state["judged"])

    async def test_command_can_stop_fallthrough(self):
        bus = Bus()
        order = []

        async def command(ctx):
            order.append("command")
            ctx.stop()

        async def chat(ctx):
            order.append("chat")

        bus.register(Handler(fn=command, kind="message", priority=8, block=False))
        bus.register(Handler(fn=chat, kind="message", priority=90))
        event = SimpleNamespace(type="message", is_group=True, to_me=True,
                                role="owner", self_id=1, user_id=2)
        ctx = _Ctx(event)
        await bus.dispatch(ctx)
        self.assertEqual(order, ["command"])


if __name__ == "__main__":
    unittest.main()
