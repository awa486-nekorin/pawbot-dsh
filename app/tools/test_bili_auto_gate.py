"""Offline tests: auto interaction / QQ intent / admin commands must use bili_gate.

No network, no QQ, no real credential. These tests pin:
- bili_auto never calls bili_extra write functions directly;
- 0 = unlimited for the daily/per-round limits (cloud parity);
- QQ natural-language deletion is restored, but without an owner message it is
  denied by the gate before any backend call;
- whitelist-only (non-owner) users cannot make the bot post or delete;
- owner commands mint a process-internal grant, and failed writes are not
  reported as success.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import bili_auto, bili_extra, bili_gate, bili_scroll, intent
from pawbot.paths import DataPaths
from plugins import admin_cmds

BVID = "BV1xx411c7mD"
OWNER = "10001"


class Cfg:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        cur = self.data
        for part in str(key).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur


def make_bot(tmp, tools=None, owners=(OWNER,), whitelist=()):
    cfg = {
        "account": {"owner_ids": list(owners)},
        "whitelist": {"user_ids": list(whitelist)},
        "tools": dict(tools or {}),
    }
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    return SimpleNamespace(
        config=Cfg(cfg),
        base_dir=tmp,
        paths=DataPaths(root=tmp, base_dir=tmp),
    )


def make_ctx(bot, uid=OWNER, post_type="message", **extra):
    event = SimpleNamespace(post_type=post_type, user_id=uid, type="message",
                            is_group=False, to_me=True, plain="")
    ctx = SimpleNamespace(
        bot=bot,
        event=event,
        user_id=uid,
        reply=AsyncMock(return_value={}),
        stop=Mock(),
        arg_text="",
        raw_text="",
        matched_command="",
        state={},
        session_id="private_1",
        self_id=0,
        group_id=None,
    )
    for key, value in extra.items():
        setattr(ctx, key, value)
    return ctx


class AutoGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bili_gate.reset_for_tests()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(bili_gate.reset_for_tests)

    def _bot(self, **tools):
        return make_bot(self.dir, tools)

    async def test_disabled_auto_makes_no_calls(self):
        bot = self._bot()
        action = AsyncMock()
        received = AsyncMock(return_value=[])
        with patch.object(bili_scroll, "action", new=action), \
                patch.object(bili_extra, "received_replies", new=received):
            out = await bili_auto.run_round(bot)
        self.assertFalse(out["ok"])
        action.assert_not_awaited()
        received.assert_not_awaited()

    async def test_no_grant_returns_before_any_read(self):
        bot = self._bot(bili_auto_enabled=True, bili_auto_reply=True,
                        bili_auto_daily_limit=20)
        received = AsyncMock(return_value=[])
        with patch.object(bili_extra, "received_replies", new=received):
            out = await bili_auto.run_round(bot)
        self.assertFalse(out["ok"])
        self.assertIn("授权", out["reason"])
        received.assert_not_awaited()

    async def test_daily_limit_zero_means_unlimited(self):
        bot = self._bot(bili_auto_enabled=True, bili_auto_reply=True,
                        bili_auto_daily_limit=0)
        received = AsyncMock(return_value=[])
        with patch.object(bili_extra, "received_replies", new=received):
            out = await bili_auto.run_round(
                bot, grant_factory=lambda action, target: object())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["quota"], 0)
        self.assertEqual(out["used"], 0)
        # 0 不是「不发」，而是「不限」：这一轮照常读了收到的回复。
        received.assert_awaited_once()

    async def test_zero_round_limit_means_unlimited(self):
        bot = self._bot(bili_auto_enabled=True, bili_auto_reply=True,
                        bili_auto_daily_limit=0, bili_auto_reply_limit=0,
                        bili_auto_reply_scan=50)
        item = {"notify_id": "n0", "bvid": BVID, "root_id": 111,
                "source_id": 222, "nickname": "网友", "text": "hi",
                "source_text": "yo", "reply_time": int(time.time())}
        action = AsyncMock(return_value={"ok": True, "data": {}})
        with patch.object(bili_auto, "_draft", new=AsyncMock(return_value="好的")), \
                patch.object(bili_extra, "received_replies",
                             new=AsyncMock(return_value=[item])) as received, \
                patch.object(bili_scroll, "action", new=action):
            out = await bili_auto.run_round(
                bot, grant_factory=lambda action, target: object())
        self.assertTrue(out["ok"], out)
        # 扫描条数走 tools.bili_auto_reply_scan。
        received.assert_awaited_once_with(limit=50)
        action.assert_awaited_once()

    async def test_writes_use_action_and_never_call_bili_extra(self):
        bot = self._bot(bili_auto_enabled=True, bili_auto_reply=True,
                        bili_auto_comment=True, bili_auto_danmaku=True,
                        bili_auto_daily_limit=20, bili_auto_reply_limit=1,
                        bili_auto_comment_limit=1, bili_auto_danmaku_limit=1,
                        bili_auto_reply_others_chance=0)
        item = {"notify_id": "n1", "bvid": BVID, "root_id": 111,
                "source_id": 222, "nickname": "网友", "text": "hi",
                "source_text": "yo", "reply_time": int(time.time())}
        action = AsyncMock(return_value={"ok": True, "data": {}})
        write_reply = Mock()
        write_comment = Mock()
        write_danmaku = Mock()
        with patch.object(bili_auto, "_draft", new=AsyncMock(return_value="好的")), \
                patch.object(bili_extra, "received_replies",
                             new=AsyncMock(return_value=[item])), \
                patch.object(bili_scroll, "recent_notes", return_value=[]), \
                patch.object(bili_scroll, "action", new=action), \
                patch.object(bili_extra, "reply_comment", new=write_reply), \
                patch.object(bili_extra, "comment_video", new=write_comment), \
                patch.object(bili_extra, "send_danmaku", new=write_danmaku):
            out = await bili_auto.run_round(
                bot, grant_factory=lambda action, target: ("grant", action, target))
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "reply")
        # 每次写都换一枚**绑定动作+目标**的凭证，不是一枚万能凭证。
        grant = action.await_args.kwargs.get("grant")
        self.assertEqual(grant, ("grant", "reply", f"{BVID}|222"))
        write_reply.assert_not_called()
        write_comment.assert_not_called()
        write_danmaku.assert_not_called()
        self.assertEqual(out["replied"], ["好的"])
        seen = bili_auto._load_seen(bot)
        self.assertIn("n1", seen["replies"])

    async def test_unconfirmed_is_not_reported_as_done(self):
        bot = self._bot(bili_auto_enabled=True, bili_auto_reply=True,
                        bili_auto_daily_limit=20, bili_auto_reply_limit=1)
        item = {"notify_id": "n2", "bvid": BVID, "root_id": 111,
                "source_id": 222, "nickname": "网友", "text": "hi",
                "source_text": "yo", "reply_time": int(time.time())}
        action = AsyncMock(return_value={"ok": False, "unconfirmed": True,
                                         "error": "B 站没确认"})
        with patch.object(bili_auto, "_draft", new=AsyncMock(return_value="好的")), \
                patch.object(bili_extra, "received_replies",
                             new=AsyncMock(return_value=[item])), \
                patch.object(bili_scroll, "action", new=action):
            out = await bili_auto.run_round(
                bot, grant_factory=lambda action, target: object())
        self.assertEqual(out["replied"], [])
        self.assertTrue(out["errors"])
        seen = bili_auto._load_seen(bot)
        self.assertTrue(seen["replies"]["n2"].get("unconfirmed"))


class IntentGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bili_gate.reset_for_tests()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(bili_gate.reset_for_tests)

    def _bot(self, **tools):
        return make_bot(self.dir, tools)

    def _note(self):
        path = os.path.join(self.dir, "note.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 测试视频\n{BVID}\n")
        return [{"path": path}]

    async def test_auto_without_ctx_does_not_read_or_write(self):
        bot = self._bot(bili_auto_enabled=True, bili_auto_reply=True,
                        bili_auto_daily_limit=20)
        received = AsyncMock(return_value=[])
        with patch.object(bili_extra, "received_replies", new=received):
            out = await intent.run(bot, {"text": "去互动", "count": 1,
                                         "tasks": [{"action": "auto", "count": 1}]})
        result = out["tasks"][0]["result"]
        self.assertFalse(result["ok"])
        self.assertIn("授权", result["reason"])
        received.assert_not_awaited()

    async def test_delcomment_without_owner_ctx_is_denied_before_backend(self):
        bot = self._bot(bili_jct="jct")
        # 云端等价：批量删除函数已经移植进来。
        for name in ("delete_recent", "delete_on_video", "delete_by_index",
                     "delete_by_text"):
            self.assertTrue(callable(getattr(bili_extra, name, None)), name)
        delete_recent = AsyncMock(return_value={"ok": True, "deleted": [],
                                                "failed": [], "reason": ""})
        with patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "delete_recent", new=delete_recent):
            out = await intent.run(bot, {"text": "删掉我的评论", "count": 1,
                                         "tasks": [{"action": "delcomment",
                                                    "count": 1}]})
        result = out["tasks"][0]["result"]
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("denied"), result)
        self.assertIn("凭证", result["error"])
        delete_recent.assert_not_awaited()

    async def test_comment_without_ctx_hits_gate_not_bili_extra(self):
        bot = self._bot(bili_jct="jct")
        backend = Mock()
        with patch.object(bili_scroll, "recent_notes", return_value=self._note()), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "comment_video", new=backend):
            out = await intent.run(bot, {"text": "评论 笑死", "count": 1,
                                         "tasks": [{"action": "comment", "count": 1}]})
        result = out["tasks"][0]["result"]
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("denied"), result)
        backend.assert_not_called()

    async def test_send_danmaku_without_ctx_hits_gate_not_bili_extra(self):
        bot = self._bot(bili_jct="jct")
        backend = Mock()
        with patch.object(bili_scroll, "recent_notes", return_value=self._note()), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "send_danmaku", new=backend):
            out = await intent.run(bot, {"text": "发弹幕 好耶", "count": 1,
                                         "tasks": [{"action": "danmaku", "count": 1}]})
        result = out["tasks"][0]["result"]
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("denied"), result)
        backend.assert_not_called()


class AdminGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bili_gate.reset_for_tests()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(bili_gate.reset_for_tests)

    def _bot(self, **tools):
        return make_bot(self.dir, tools, owners=(OWNER,), whitelist=("999",))

    async def test_whitelist_only_user_cannot_comment(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True)
        ctx = make_ctx(bot, uid="999", arg_text=f"{BVID} 笑死")
        action = AsyncMock(return_value={"ok": True})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await admin_cmds.cmd_comment(ctx)
        ctx.reply.assert_awaited()
        self.assertIn("只能由主人", ctx.reply.await_args.args[0])
        action.assert_not_awaited()
        ctx.stop.assert_called()

    async def test_whitelist_only_user_cannot_send_danmaku(self):
        bot = self._bot(bili_write_enabled=True, bili_write_danmaku=True)
        ctx = make_ctx(bot, uid="999", arg_text=f"{BVID} 好耶")
        action = AsyncMock(return_value={"ok": True})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await admin_cmds.cmd_danmaku(ctx)
        ctx.reply.assert_awaited()
        self.assertIn("只能由主人", ctx.reply.await_args.args[0])
        action.assert_not_awaited()

    async def test_owner_comment_uses_process_grant(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True)
        ctx = make_ctx(bot, uid=OWNER, arg_text=f"{BVID} 笑死")
        action = AsyncMock(return_value={"ok": True, "data": {}})

        async def fake_bg(ctx_, factory, on_done):
            await factory()

        with patch.object(admin_cmds.bili_scroll, "action", new=action), \
                patch.object(admin_cmds, "_bg", new=fake_bg):
            await admin_cmds.cmd_comment(ctx)
        action.assert_awaited_once()
        grant = action.await_args.kwargs.get("grant")
        self.assertIsInstance(grant, bili_gate.WriteGrant)
        self.assertEqual(grant.source, "owner_turn")

    async def test_natural_delcomment_confirms_and_runs(self):
        bot = self._bot()
        ctx = make_ctx(bot, uid=OWNER)
        ctx.event.plain = "把评论删了"
        verdict = {"ok": True, "hit": True, "count": 1,
                   "tasks": [{"action": "delcomment", "count": 1}]}
        action = AsyncMock(return_value={"ok": True, "deleted": [], "failed": [],
                                         "reason": ""})

        async def fake_bg(ctx_, factory, on_done):
            await factory()

        with patch.object(admin_cmds.intent, "judge",
                          new=AsyncMock(return_value=verdict)), \
                patch.object(admin_cmds.bili_scroll, "action", new=action), \
                patch.object(admin_cmds, "_bg", new=fake_bg):
            await admin_cmds.admin_natural_command(ctx)
        ctx.reply.assert_awaited()
        self.assertIn("删评论", ctx.reply.await_args.args[0])
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "delete_recent")
        self.assertEqual(action.await_args.args[2], {"n": 1})
        grant = action.await_args.kwargs.get("grant")
        self.assertIsInstance(grant, bili_gate.WriteGrant)
        self.assertEqual(grant.action, "delete_recent")
        ctx.stop.assert_called()



class SubtitleShapeTests(unittest.IsolatedAsyncioTestCase):
    """bili_scroll._subtitle 返回 dict；自动互动必须取 text 而不是把 dict 切片。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bot = make_bot(self.tmp.name)

    async def test_subtitle_helper_extracts_text_from_dict(self):
        info = {"text": "字幕正文", "status": "ok", "language": "zh"}
        with patch.object(bili_scroll, "_srv", return_value=object()), \
                patch.object(bili_scroll, "_subtitle",
                             new=AsyncMock(return_value=info)):
            self.assertEqual(await bili_auto._subtitle(self.bot, BVID), "字幕正文")

    async def test_subtitle_helper_keeps_plain_string_shape(self):
        with patch.object(bili_scroll, "_srv", return_value=object()), \
                patch.object(bili_scroll, "_subtitle",
                             new=AsyncMock(return_value="旧形状正文")):
            self.assertEqual(await bili_auto._subtitle(self.bot, BVID), "旧形状正文")

    async def test_subtitle_helper_degrades_to_empty(self):
        with patch.object(bili_scroll, "_srv", side_effect=RuntimeError("boom")):
            self.assertEqual(await bili_auto._subtitle(self.bot, BVID), "")


if __name__ == "__main__":
    unittest.main()
