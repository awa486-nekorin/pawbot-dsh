"""Offline tests for the local B站 write gate (2026-11-03 cloud policy).

These tests never touch the network: no B站 request, no QQ, no real credential.
They pin the gate semantics after the 2026-11-03「完整云端策略」decision:

- 没有凭证 / 闸门关掉 / 非主人 三类情况都不能碰到后端；
- 授权凭证**绑定动作和目标**：换动作、换目标都用不了；
- 回执没有可验证 id 时只能算 unconfirmed，不能算成功；
- 回执成功即成功：读后核验只作附加信息（``verified``），不把成功降级；
- 批量删除与「一键点赞+收藏+投币」现在可达，但同样要主人凭证 + 审计 + 参数校验；
- 额度 0 = 不限；负数 / 布尔 / 字符串仍是配置错。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import bili_extra, bili_gate, bili_scroll
from pawbot.paths import DataPaths

BVID = "BV1xx411c7mD"
OWNER = "10001"


class Cfg:
    """Minimal dotted-key config stand-in (same contract as pawbot.config.Config)."""

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


def make_ctx(bot, uid=OWNER, post_type="message"):
    return SimpleNamespace(
        bot=bot,
        event=SimpleNamespace(post_type=post_type, user_id=uid),
    )


def owner_grant(bot, action, args):
    """主人的一条真实消息 → 绑定这个动作+目标的凭证（测试统一走这里）。"""
    return bili_gate.owner_turn_grant(
        bot, make_ctx(bot), action=action,
        target=bili_gate.target_key(action, args))


class GateTests(unittest.TestCase):
    def setUp(self):
        bili_gate.reset_for_tests()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(bili_gate.reset_for_tests)

    def _bot(self, **tools):
        return make_bot(self.dir, tools)

    def test_only_owner_message_can_mint_a_bounded_grant(self):
        bot = self._bot()
        grant = owner_grant(bot, "comment", {"bvid": BVID})
        self.assertIsNotNone(grant)
        self.assertEqual(grant.action, "comment")
        self.assertEqual(grant.target, BVID)
        self.assertIsNone(bili_gate.owner_turn_grant(
            bot, make_ctx(bot, uid="999"), action="comment", target=BVID))
        self.assertIsNone(bili_gate.owner_turn_grant(
            bot, {"event": {"post_type": "message"}}, action="comment", target=BVID))
        self.assertIsNone(bili_gate.owner_turn_grant(
            bot, "owner", action="comment", target=BVID))
        self.assertIsNone(bili_gate.owner_turn_grant(
            bot, None, action="comment", target=BVID))
        notice = make_ctx(bot)
        notice.event.post_type = "notice"
        self.assertIsNone(bili_gate.owner_turn_grant(
            bot, notice, action="comment", target=BVID))
        # 没绑定动作/目标、或动作根本不是写动作 → 换不到凭证。
        self.assertIsNone(bili_gate.owner_turn_grant(
            bot, make_ctx(bot), action="", target=BVID))
        self.assertIsNone(bili_gate.owner_turn_grant(
            bot, make_ctx(bot), action="comment", target=""))
        self.assertIsNone(bili_gate.owner_turn_grant(
            bot, make_ctx(bot), action="search", target=BVID))

    def test_gate_off_denies_comment_even_with_grant(self):
        bot = self._bot(bili_write_enabled=False)
        args = {"bvid": BVID, "text": "hi"}
        out = bili_gate.guard(bot, "comment", args,
                              grant=owner_grant(bot, "comment", args))
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "not_authorized")
        self.assertIn("没开", out["error"])

    def test_kind_switch_is_required(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=False)
        args = {"bvid": BVID, "text": "hi"}
        out = bili_gate.guard(bot, "comment", args,
                              grant=owner_grant(bot, "comment", args))
        self.assertFalse(out["ok"])
        self.assertIn("bili_write_comment", out["error"])

    def test_danmaku_denial_names_the_real_config_key(self):
        # 发弹幕的配置键是 tools.bili_write_danmaku；拒绝文案写
        # tools.bili_write_send_danmaku 会让人照着改也永远改不对。
        bot = self._bot(bili_write_enabled=True, bili_write_danmaku=False)
        args = {"bvid": BVID, "text": "hi"}
        out = bili_gate.guard(bot, "send_danmaku", args,
                              grant=owner_grant(bot, "send_danmaku", args))
        self.assertFalse(out["ok"])
        self.assertIn("tools.bili_write_danmaku", out["error"])
        self.assertNotIn("tools.bili_write_send_danmaku", out["error"])

    def test_no_grant_is_denied(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True)
        out = bili_gate.guard(bot, "comment", {"bvid": BVID, "text": "hi"})
        self.assertFalse(out["ok"])
        self.assertIn("凭证", out["error"])

    def test_grant_is_bound_to_one_action(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True,
                        bili_write_like=True)
        comment_args = {"bvid": BVID, "text": "hi"}
        grant = owner_grant(bot, "comment", comment_args)
        out = bili_gate.guard(bot, "like", {"bvid": BVID}, grant=grant)
        self.assertFalse(out["ok"])
        self.assertIn("绑定的动作", out["error"])
        # 这不是额度/配置问题：凭证本身就不能跨动作复用。
        self.assertEqual(out["code"], "not_authorized")

    def test_grant_is_bound_to_one_target(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True)
        grant = owner_grant(bot, "comment", {"bvid": BVID})
        out = bili_gate.guard(bot, "comment",
                              {"bvid": "BV1yy411c7mE", "text": "hi"}, grant=grant)
        self.assertFalse(out["ok"])
        self.assertIn("绑定的目标", out["error"])

    def test_grant_is_single_use_and_expires(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True,
                        bili_write_quota={"comment": 5})
        args = {"bvid": BVID, "text": "hi"}
        grant = owner_grant(bot, "comment", args)
        first = bili_gate.guard(bot, "comment", {"bvid": BVID, "text": "hi"},
                                grant=grant)
        self.assertTrue(first["ok"], first)
        again = bili_gate.guard(bot, "comment", {"bvid": BVID, "text": "hi"},
                                grant=grant)
        self.assertFalse(again["ok"])
        self.assertIn("凭证", again["error"])

        expired = owner_grant(bot, "comment", args)
        rec = bili_gate._GRANTS[expired._nonce]
        rec["expires"] = 0
        out = bili_gate.guard(bot, "comment", {"bvid": BVID, "text": "hi"},
                              grant=expired)
        self.assertFalse(out["ok"])
        self.assertIn("凭证", out["error"])

    def test_quota_and_dedupe(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True,
                        bili_write_quota={"comment": 2})
        first_args = {"bvid": BVID, "text": "hi"}
        first = bili_gate.guard(
            bot, "comment", first_args, grant=owner_grant(bot, "comment", first_args))
        self.assertTrue(first["ok"], first)
        ok = bili_gate.settle(bot, first["reservation"],
                              {"ok": True,
                               "data": {"success": True, "rpid": 555, "rpid_str": "555"}})
        self.assertEqual(ok["status"], "confirmed")
        again_args = {"bvid": BVID, "text": "hi"}
        again = bili_gate.guard(
            bot, "comment", again_args, grant=owner_grant(bot, "comment", again_args))
        self.assertFalse(again["ok"])
        self.assertEqual(again["code"], "dedupe")
        second_args = {"bvid": BVID, "text": "别的"}
        second = bili_gate.guard(
            bot, "comment", second_args, grant=owner_grant(bot, "comment", second_args))
        self.assertTrue(second["ok"], second)
        third_args = {"bvid": BVID, "text": "第三个"}
        other = bili_gate.guard(
            bot, "comment", third_args, grant=owner_grant(bot, "comment", third_args))
        self.assertFalse(other["ok"])
        self.assertEqual(other["code"], "quota_exhausted")

    def test_quota_zero_means_unlimited(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True,
                        bili_write_quota={"comment": 0})
        for idx, text in enumerate(("一", "二", "三"), 1):
            args = {"bvid": BVID, "text": text}
            out = bili_gate.guard(bot, "comment", dict(args),
                                  grant=owner_grant(bot, "comment", args))
            self.assertTrue(out["ok"], out)
            settled = bili_gate.settle(
                bot, out["reservation"],
                {"ok": True,
                 "data": {"success": True, "rpid": 500 + idx}})
            self.assertEqual(settled["status"], "confirmed")
        self.assertEqual(bili_gate.status(bot)["used"]["comment"], 3)

    def test_receipt_without_evidence_is_unconfirmed(self):
        bot = self._bot(bili_write_enabled=True, bili_write_reply=True)
        args = {"bvid": BVID, "text": "hi", "rpid": 1}
        out = bili_gate.guard(bot, "reply", args,
                              grant=owner_grant(bot, "reply", args))
        self.assertTrue(out["ok"], out)
        settled = bili_gate.settle(bot, out["reservation"],
                                   {"ok": True, "data": {"success": True}})
        self.assertEqual(settled["status"], "unconfirmed")
        self.assertIn("rpid", settled["reason"])

    def test_downgrade_demotes_confirmed_to_unconfirmed(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True,
                        bili_write_quota={"comment": 2})
        args = {"bvid": BVID, "text": "hi"}
        out = bili_gate.guard(bot, "comment", args,
                              grant=owner_grant(bot, "comment", args))
        bili_gate.settle(bot, out["reservation"],
                         {"ok": True, "data": {"success": True, "rpid": 777}})
        down = bili_gate.downgrade(bot, out["reservation"], "复核没找到")
        self.assertEqual(down["status"], "unconfirmed")
        again_args = {"bvid": BVID, "text": "hi"}
        again = bili_gate.guard(
            bot, "comment", again_args, grant=owner_grant(bot, "comment", again_args))
        self.assertFalse(again["ok"])
        self.assertEqual(again["code"], "dedupe_unconfirmed")

    def test_batch_and_aggregate_actions_need_owner_grant_not_rejection(self):
        bot = self._bot()
        cases = (
            ("delete_recent", {"n": 2}),
            ("delete_by_index", {"index": 1}),
            ("delete_by_text", {"text": "旧话"}),
            ("delete_on_video", {"bvid": BVID}),
            ("react", {"bvid": BVID, "judge": {"like": True}}),
        )
        for name, args in cases:
            with self.subTest(name=name):
                self.assertEqual(bili_gate.refusal(name), "")
                self.assertIn(name, bili_gate.WRITE_ACTIONS)
                # 没凭证仍然不行：拒绝的是「没授权」，不是「动作不开放」。
                denied = bili_gate.guard(bot, name, dict(args))
                self.assertFalse(denied["ok"])
                self.assertIn("凭证", denied["error"])
                ok = bili_gate.guard(
                    bot, name, dict(args), grant=owner_grant(bot, name, args))
                self.assertTrue(ok["ok"], ok)

    def test_delete_comment_requires_explicit_positive_rpid(self):
        bot = self._bot(bili_write_enabled=True, bili_write_delete_comment=True)
        cases = ({}, {"bvid": BVID}, {"bvid": BVID, "rpid": 0},
                 {"bvid": BVID, "rpid": -3}, {"bvid": BVID, "rpid": True},
                 {"bvid": BVID, "rpid": "最近"})
        for args in cases:
            with self.subTest(args=args):
                out = bili_gate.guard(bot, "delete_comment", dict(args),
                                      grant=owner_grant(bot, "delete_comment",
                                                        {"bvid": BVID, "rpid": 1}))
                self.assertFalse(out["ok"])
                # 目标不明确时报的是 bvid 或 rpid 的错，绝不悄悄选一个目标。
                self.assertTrue("rpid" in out["error"] or "bvid" in out["error"],
                                out["error"])

    def test_comment_extra_args_and_danmaku_length_are_rejected(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True,
                        bili_write_danmaku=True)
        args = {"bvid": BVID, "text": "hi", "images": ["x"]}
        # 授权目标只按 bvid 算；多余参数在参数形状检查里被拒。
        out = bili_gate.guard(
            bot, "comment", args,
            grant=bili_gate.owner_turn_grant(
                bot, make_ctx(bot), action="comment", target=BVID))
        self.assertFalse(out["ok"])
        self.assertIn("images", out["error"])
        long_text = "弹" * 61
        args2 = {"bvid": BVID, "text": long_text}
        out = bili_gate.guard(
            bot, "send_danmaku", args2,
            grant=owner_grant(bot, "send_danmaku", args2))
        self.assertFalse(out["ok"])
        self.assertIn("60", out["error"])


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bili_gate.reset_for_tests()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(bili_gate.reset_for_tests)

    def _bot(self, **tools):
        return make_bot(self.dir, tools)

    async def test_no_bili_jct_never_calls_backend(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True)
        args = {"bvid": BVID, "text": "hi"}
        backend = AsyncMock(return_value={"rpid": 1})
        with patch.object(bili_scroll, "_bili_jct", return_value=""), \
                patch.object(bili_extra, "comment_video", new=backend):
            out = await bili_scroll.action(
                bot, "comment", args, grant=owner_grant(bot, "comment", args))
        self.assertFalse(out["ok"])
        self.assertIn("bili_jct", out["error"])
        backend.assert_not_awaited()

    async def test_no_grant_never_calls_backend(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True)
        backend = AsyncMock(return_value={"rpid": 1})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "comment_video", new=backend):
            out = await bili_scroll.action(bot, "comment", {"bvid": BVID, "text": "hi"})
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("denied"), out)
        backend.assert_not_awaited()

    async def test_comment_missing_post_check_is_still_success(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True,
                        bili_write_quota={"comment": 2})
        args = {"bvid": BVID, "text": "hi"}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "comment_video",
                             new=AsyncMock(return_value={"rpid": 555})), \
                patch.object(bili_extra, "verify_comment",
                             new=AsyncMock(return_value=False)):
            out = await bili_scroll.action(
                bot, "comment", args, grant=owner_grant(bot, "comment", args))
        # 云端语义：后端返回成功（带 rpid）即成功；核验只作附加信息。
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), False)
        # 闸门记录 confirmed ⇒ 同样的内容不能当天再发一次。
        again_args = {"bvid": BVID, "text": "hi"}
        again = bili_gate.guard(
            bot, "comment", again_args,
            grant=owner_grant(bot, "comment", again_args))
        self.assertFalse(again["ok"])
        self.assertEqual(again["code"], "dedupe")

    async def test_comment_success_does_not_require_post_check(self):
        bot = self._bot(bili_write_enabled=True, bili_write_comment=True)
        args = {"bvid": BVID, "text": "hi"}
        verify = AsyncMock(return_value=True)
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "comment_video",
                             new=AsyncMock(return_value={"rpid": 555})), \
                patch.object(bili_extra, "verify_comment", new=verify):
            out = await bili_scroll.action(
                bot, "comment", args, grant=owner_grant(bot, "comment", args))
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), True)
        verify.assert_awaited_once()

    async def test_danmaku_without_dmid_is_unconfirmed(self):
        bot = self._bot(bili_write_enabled=True, bili_write_danmaku=True)
        args = {"bvid": BVID, "text": "好耶"}
        verify = AsyncMock(return_value=True)
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "send_danmaku",
                             new=AsyncMock(return_value={"code": 0})), \
                patch.object(bili_extra, "verify_danmaku", new=verify):
            out = await bili_scroll.action(
                bot, "send_danmaku", args,
                grant=owner_grant(bot, "send_danmaku", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("unconfirmed"), out)
        verify.assert_not_awaited()

    async def test_danmaku_success_needs_dmid_verification_is_informational(self):
        bot = self._bot(bili_write_enabled=True, bili_write_danmaku=True)
        args = {"bvid": BVID, "text": "好耶"}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "send_danmaku",
                             new=AsyncMock(return_value={"dmid": 777})), \
                patch.object(bili_extra, "verify_danmaku",
                             new=AsyncMock(return_value=True)):
            out = await bili_scroll.action(
                bot, "send_danmaku", args,
                grant=owner_grant(bot, "send_danmaku", args))
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), True)

    async def test_danmaku_progress_zero_is_kept(self):
        # progress=0 是合法的「开头就飘」；不能因为 `or 5.0` 被改成第 5 秒。
        bot = self._bot(bili_write_enabled=True, bili_write_danmaku=True)
        args = {"bvid": BVID, "text": "好耶", "progress": 0}
        backend = AsyncMock(return_value={"dmid": 777})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "send_danmaku", new=backend), \
                patch.object(bili_extra, "verify_danmaku",
                             new=AsyncMock(return_value=True)):
            out = await bili_scroll.action(
                bot, "send_danmaku", args,
                grant=owner_grant(bot, "send_danmaku", args))
        self.assertTrue(out["ok"], out)
        backend.assert_awaited_once_with(BVID, "好耶", 0.0)

    async def test_danmaku_default_progress_when_absent(self):
        bot = self._bot(bili_write_enabled=True, bili_write_danmaku=True)
        args = {"bvid": BVID, "text": "好耶"}
        backend = AsyncMock(return_value={"dmid": 778})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "send_danmaku", new=backend), \
                patch.object(bili_extra, "verify_danmaku",
                             new=AsyncMock(return_value=True)):
            out = await bili_scroll.action(
                bot, "send_danmaku", args,
                grant=owner_grant(bot, "send_danmaku", args))
        self.assertTrue(out["ok"], out)
        backend.assert_awaited_once_with(BVID, "好耶", 5.0)

    async def test_read_danmaku_is_not_treated_as_a_write(self):
        bot = self._bot()
        srv = SimpleNamespace(bili_danmaku=AsyncMock(
            return_value='{"count": 0, "danmakus": []}'))
        with patch.object(bili_scroll, "_bili_jct", return_value=""), \
                patch.object(bili_scroll, "_srv", return_value=srv):
            out = await bili_scroll.action(bot, "danmaku", {"bvid": BVID})
        self.assertTrue(out["ok"], out)
        srv.bili_danmaku.assert_awaited_once()

    async def test_batch_delete_needs_grant_then_reaches_bili_extra(self):
        bot = self._bot(bili_write_enabled=True, bili_write_delete_recent=True)
        backend = AsyncMock(return_value={"ok": True, "deleted": [1], "failed": []})
        args = {"n": 2}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "delete_recent", new=backend):
            denied = await bili_scroll.action(bot, "delete_recent", dict(args))
            self.assertFalse(denied["ok"])
            self.assertTrue(denied.get("denied"), denied)
            backend.assert_not_awaited()
            ok = await bili_scroll.action(
                bot, "delete_recent", dict(args),
                grant=owner_grant(bot, "delete_recent", args))
        self.assertTrue(ok["ok"], ok)
        backend.assert_awaited_once_with(bot, 2)

    async def test_react_needs_grant_then_reaches_bili_extra(self):
        bot = self._bot(bili_write_enabled=True, bili_write_react=True)
        backend = AsyncMock(return_value={"like": True, "fav": False, "coin": 0,
                                          "errors": []})
        args = {"bvid": BVID,
                "judge": {"like": True, "fav": False, "coin": 0}}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "react", new=backend):
            denied = await bili_scroll.action(bot, "react", dict(args))
            self.assertFalse(denied["ok"])
            self.assertIn("凭证", denied["error"])
            backend.assert_not_awaited()
            ok = await bili_scroll.action(
                bot, "react", dict(args), grant=owner_grant(bot, "react", args))
        self.assertTrue(ok["ok"], ok)
        backend.assert_awaited_once()

    async def test_verify_helpers_fail_closed_without_credential(self):
        with patch.object(bili_extra, "_cred", side_effect=RuntimeError("no cred")):
            self.assertFalse(await bili_extra.verify_comment(BVID, 555))
            self.assertFalse(await bili_extra.verify_danmaku(BVID, "好耶"))
            self.assertFalse(await bili_extra.verify_like(BVID))
            self.assertIsNone(await bili_extra.verify_coin(BVID))
            self.assertFalse(await bili_extra.verify_favorite(BVID))
            self.assertEqual((await bili_extra.comment_state(BVID, 1))["state"],
                             "unknown")
        self.assertEqual(bili_extra.comment_receipt({"rpid": 5}),
                         {"success": True, "rpid": 5, "rpid_str": "5"})
        self.assertEqual(bili_extra.comment_receipt({"code": 0}),
                         {"success": True})

    def test_batch_delete_helpers_and_react_exist(self):
        # 云端等价：批量删除 / 账本 / react 的实现在 bili_extra 里可达。
        for name in ("delete_recent", "delete_on_video", "delete_by_index",
                     "delete_by_text", "delete_items", "my_comments",
                     "record_my_comment", "scan_my_comments", "react",
                     "coin_balance", "delete_comment"):
            self.assertTrue(callable(getattr(bili_extra, name, None)), name)



if __name__ == "__main__":
    unittest.main()
