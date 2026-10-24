"""Offline tests for OneBot notices and poke handling."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import event
from pawbot.onebot import ActionError
from plugins import qq_actions


class NoticeEventTests(unittest.TestCase):
    def test_group_poke_notice_has_session_and_target_properties(self):
        ev = event.parse({
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "self_id": 10001, "group_id": 77, "user_id": 20002,
            "target_id": 10001, "sender": {"nickname": "阿明"},
        })
        self.assertEqual(ev.session_id, "group_77")
        self.assertTrue(ev.is_group)
        self.assertTrue(ev.to_me)
        self.assertEqual(ev.sender_name, "阿明")
        self.assertEqual(ev.sub_type, "poke")

    def test_private_notice_has_session_property(self):
        ev = event.parse({"post_type": "notice", "notice_type": "notify",
                          "user_id": 20002, "self_id": 10001})
        self.assertEqual(ev.session_id, "private_20002")
        self.assertFalse(ev.is_group)


class PokeHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_seen = dict(qq_actions._SEEN)
        qq_actions._SEEN.clear()
        self.call = AsyncMock(return_value={})
        self.send = AsyncMock(return_value={})
        self.bot = SimpleNamespace(conn_for=lambda _self_id: SimpleNamespace(call=self.call))
        self.ev = SimpleNamespace(sub_type="poke", is_group=True, target_id=10001,
                                  group_id=77, user_id=20002)
        self.ctx = SimpleNamespace(event=self.ev, self_id=10001, bot=self.bot,
                                   session_id="group_77", send=self.send)

    def tearDown(self):
        qq_actions._SEEN.clear()
        qq_actions._SEEN.update(self.old_seen)

    async def test_poked_bot_replies_and_pokes_back_once(self):
        await qq_actions.on_poked(self.ctx)
        self.call.assert_awaited_once_with("group_poke", group_id=77,
                                           user_id=20002, timeout=8)
        self.send.assert_awaited_once()
        args, kwargs = self.send.await_args
        self.assertTrue(args[0])
        self.assertEqual(kwargs, {"group_id": 77})

    async def test_ignores_poke_targeting_other_member(self):
        self.ev.target_id = 30003
        await qq_actions.on_poked(self.ctx)
        self.call.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_debounces_repeated_pokes(self):
        await qq_actions.on_poked(self.ctx)
        await qq_actions.on_poked(self.ctx)
        self.call.assert_awaited_once()
        self.send.assert_awaited_once()

    async def test_does_not_send_reply_if_poke_back_fails(self):
        self.call.side_effect = ActionError("failed", delivered=False)
        await qq_actions.on_poked(self.ctx)
        self.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
