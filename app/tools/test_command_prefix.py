"""Offline tests for the `//` command-prefix rule (owner request 2026-11-03).

Covers the real `Bot._prepare_text` path: only `//`-prefixed text with a known
command counts as a command; unmatched `//` text is stripped for chat; bare
`/`-prefixed text is no longer a command. No network, no Bot startup.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import core
from pawbot.bus import Handler

CONFIG = {
    "bot.command_prefixes": ["//"],
    "bot.allow_no_prefix": False,
}


class FakeBus:
    def __init__(self):
        self.handlers = [Handler(fn=lambda: None, kind="command",
                                 module="test", name="刷B站",
                                 commands=("刷B站",))]


class CommandPrefixTest(unittest.TestCase):
    def setUp(self):
        self._saved = core.BUS.handlers
        core.BUS.handlers = FakeBus().handlers
        self.bot = object.__new__(core.Bot)
        self.bot.config = dict(CONFIG)
        self.bot._is_wake = lambda ev: False

    def tearDown(self):
        core.BUS.handlers = self._saved

    def _run(self, text):
        ctx = SimpleNamespace(state={})
        ev = SimpleNamespace(plain=text)
        core.Bot._prepare_text(self.bot, ctx, ev)
        return ctx

    def test_double_slash_known_command(self):
        ctx = self._run("//刷B站 3")
        self.assertEqual(ctx.matched_command, "刷B站")
        self.assertEqual(ctx.raw_text, "3")

    def test_double_slash_unknown_is_chat_without_prefix(self):
        ctx = self._run("//今天吃什么")
        self.assertEqual(ctx.matched_command, "")
        self.assertEqual(ctx.raw_text, "今天吃什么")

    def test_single_slash_is_not_a_command(self):
        ctx = self._run("/刷B站 3")
        self.assertEqual(ctx.matched_command, "")
        self.assertEqual(ctx.raw_text, "/刷B站 3")

    def test_bare_text_is_not_a_command(self):
        ctx = self._run("刷B站 3")
        self.assertEqual(ctx.matched_command, "")
        self.assertEqual(ctx.raw_text, "刷B站 3")


class HelpPrefixTest(unittest.TestCase):
    """帮助文案必须展示真实前缀；写死 `/` 会让用户照着重发一条普通聊天。"""

    def setUp(self):
        from plugins import basic

        self.basic = basic
        self._saved = core.BUS.handlers
        core.BUS.handlers = [
            Handler(fn=lambda: None, kind="command", module="test", name="ping",
                    commands=("ping",), aliases=("延迟",)),
            Handler(fn=lambda: None, kind="command", module="test", name="help",
                    commands=("help",), aliases=("帮助",)),
        ]

    def tearDown(self):
        core.BUS.handlers = self._saved

    def _run(self, prefixes):
        replies = []

        class Cfg:
            def get(self, key, default=None):
                if key == "bot.command_prefixes":
                    return prefixes
                return default

        async def reply(text):
            replies.append(text)

        ctx = SimpleNamespace(
            bot=SimpleNamespace(config=Cfg()),
            reply=reply,
            stop=lambda: None,
        )
        asyncio.run(self.basic.help_cmd(ctx))
        return "\n".join(replies)

    def test_help_uses_double_slash_by_default(self):
        out = self._run(["//"])
        self.assertIn("都以 // 开头", out)
        self.assertIn("//ping（延迟）", out)
        self.assertIn("//help（帮助）", out)
        # 旧前缀不能作为「照着发」的样子出现
        self.assertNotIn("  /ping", out)
        self.assertNotIn("  /help", out)

    def test_help_follows_configured_prefix(self):
        out = self._run(["!"])
        self.assertIn("!ping（延迟）", out)
        self.assertNotIn("//ping", out)

    def test_help_survives_bad_prefix_config(self):
        out = self._run(None)
        self.assertIn("//ping（延迟）", out)


if __name__ == "__main__":
    unittest.main()
