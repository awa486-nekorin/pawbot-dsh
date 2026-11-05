"""Offline behaviour tests: per-action B站 write authorization (2026-11-03).

不联网、不发 QQ、不用真实凭证。所有后端调用都是 AsyncMock（假传输），
状态文件写在 TemporaryDirectory 里，所以 `app/data` 完全不参与。

覆盖用户明确要求（2026-11-03「完整云端策略」）的行为：
- 写闸门与每个动作**默认全开**（云端等价），但仍然是可关的总开关；
- 非主人（含白名单）换不到写凭证、显式指令也不代发/代删；
- 授权**绑定动作和目标**：换动作/换目标都不行；过期、重复使用都不行；
- 额度 **0 = 不限**；负数 / 布尔 / 字符串仍是配置错；
- 批量删除（最近/序号/文本/整条视频）与 react **可达**，但只走主人凭证 + 审计；
- 删除他人评论 → 拒绝，且不碰删除后端；作者查不清也不删；
- 回执语义与云端一致：后端成功即成功，读后核验只作附加信息（``verified``），
  但后端失败 / 抛异常仍然绝不报成功；
- 正常授权（主人当轮 + 明确指令）单次成功；
- 定时任务换不到投币/删除/点赞/收藏/react 的凭证；自然语言「互动」不会隐式授权。
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

from pawbot import bili_auto, bili_extra, bili_gate, bili_scroll, intent, tools as tools_mod
from pawbot.paths import DataPaths
from plugins import admin_cmds

BVID = "BV1xx411c7mD"
BVID2 = "BV1yy411c7mE"
OWNER = "10001"
OTHER = "999"
MY_MID = "4242"


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


def make_bot(tmp, tools=None, owners=(OWNER,), whitelist=(OTHER,)):
    cfg = {
        "account": {"owner_ids": list(owners)},
        "whitelist": {"user_ids": list(whitelist)},
        "tools": dict(tools or {}),
    }
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    return SimpleNamespace(config=Cfg(cfg), base_dir=tmp,
                           paths=DataPaths(root=tmp, base_dir=tmp))


def make_ctx(bot, uid=OWNER, arg_text=""):
    event = SimpleNamespace(post_type="message", user_id=uid, type="message",
                            is_group=False, to_me=True, plain="")
    return SimpleNamespace(
        bot=bot, event=event, user_id=uid,
        reply=AsyncMock(return_value={}), stop=Mock(),
        arg_text=arg_text, raw_text="", matched_command="",
        state={}, session_id="private_1", self_id=0, group_id=None,
    )


def owner_grant(bot, action, args, uid=OWNER):
    return bili_gate.owner_turn_grant(
        bot, make_ctx(bot, uid=uid), action=action,
        target=bili_gate.target_key(action, args))


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bili_gate.reset_for_tests()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(bili_gate.reset_for_tests)

    def _bot(self, **tools):
        return make_bot(self.dir, tools)


class DefaultOnTests(Base):
    """2026-11-03：默认与云端等价地全开（闸门仍可关）。"""

    def test_new_actions_are_on_by_default(self):
        bot = self._bot()
        cases = (
            ("like", {"bvid": BVID}),
            ("favorite", {"bvid": BVID}),
            ("coin", {"bvid": BVID, "num": 1}),
            ("delete_comment", {"bvid": BVID, "rpid": 12345}),
            ("delete_recent", {"n": 3}),
            ("delete_by_index", {"index": 1}),
            ("delete_by_text", {"text": "旧话"}),
            ("delete_on_video", {"bvid": BVID}),
            ("react", {"bvid": BVID,
                       "judge": {"like": True, "fav": False, "coin": 0}}),
        )
        for action, args in cases:
            with self.subTest(action=action):
                out = bili_gate.guard(bot, action, dict(args),
                                      grant=owner_grant(bot, action, args))
                self.assertTrue(out["ok"], out)

    def test_default_config_declares_all_switches_true(self):
        import runpy
        defaults = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "pawbot" / "config.py"),
            run_name="__config_offline__")["DEFAULT"]["tools"]
        for key in ("bili_write_enabled", "bili_write_like", "bili_write_favorite",
                    "bili_write_coin", "bili_write_delete_comment",
                    "bili_write_delete_recent", "bili_write_delete_by_index",
                    "bili_write_delete_by_text", "bili_write_delete_on_video",
                    "bili_write_react", "bili_write_scheduler"):
            self.assertIs(defaults[key], True, key)
        # 额度默认空对象 = 全部不限（云端等价）。
        self.assertEqual(defaults["bili_write_quota"], {})

    async def test_master_switch_off_still_blocks_backend(self):
        bot = self._bot(bili_write_enabled=False)
        backend = AsyncMock(return_value={"code": 0})
        args = {"bvid": BVID}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "like_video", new=backend), \
                patch.object(bili_extra, "favorite_video", new=backend), \
                patch.object(bili_extra, "coin_video", new=backend):
            out = await bili_scroll.action(
                bot, "like", dict(args), grant=owner_grant(bot, "like", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("denied"), out)
        self.assertIn("没开", out["error"])
        backend.assert_not_awaited()


class NonOwnerTests(Base):
    def test_whitelist_cannot_mint_a_grant(self):
        bot = self._bot(bili_write_like=True)
        self.assertIsNone(owner_grant(bot, "like", {"bvid": BVID}, uid=OTHER))

    async def test_whitelist_explicit_like_command_does_not_post(self):
        bot = self._bot(bili_write_enabled=True, bili_write_like=True)
        ctx = make_ctx(bot, uid=OTHER, arg_text=BVID)
        action = AsyncMock(return_value={"ok": True})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await admin_cmds.cmd_like(ctx)
        self.assertIn("只能由主人", ctx.reply.await_args.args[0])
        action.assert_not_awaited()

    async def test_whitelist_coin_and_delete_commands_do_not_post(self):
        bot = self._bot(bili_write_enabled=True, bili_write_coin=True,
                        bili_write_delete_comment=True)
        with patch.object(admin_cmds.bili_scroll, "action",
                          new=AsyncMock(return_value={"ok": True})) as action:
            await admin_cmds.cmd_coin(make_ctx(bot, uid=OTHER, arg_text=BVID))
            await admin_cmds.cmd_del_comment(
                make_ctx(bot, uid=OTHER, arg_text=f"{BVID} 123456"))
        action.assert_not_awaited()


class GrantBindingTests(Base):
    def _bot(self, **tools):
        base = dict(bili_write_enabled=True, bili_write_like=True,
                    bili_write_favorite=True, bili_write_coin=True,
                    bili_write_delete_comment=True)
        base.update(tools)
        return super()._bot(**base)

    def test_wrong_action_is_rejected(self):
        bot = self._bot()
        grant = owner_grant(bot, "like", {"bvid": BVID})
        out = bili_gate.guard(bot, "favorite", {"bvid": BVID}, grant=grant)
        self.assertFalse(out["ok"])
        self.assertIn("绑定的动作", out["error"])

    def test_wrong_target_is_rejected(self):
        bot = self._bot()
        grant = owner_grant(bot, "like", {"bvid": BVID})
        out = bili_gate.guard(bot, "like", {"bvid": BVID2}, grant=grant)
        self.assertFalse(out["ok"])
        self.assertIn("绑定的目标", out["error"])

    def test_expired_grant_is_rejected(self):
        bot = self._bot()
        grant = owner_grant(bot, "like", {"bvid": BVID})
        bili_gate._GRANTS[grant._nonce]["expires"] = 0
        out = bili_gate.guard(bot, "like", {"bvid": BVID}, grant=grant)
        self.assertFalse(out["ok"])
        self.assertIn("凭证", out["error"])

    def test_reused_grant_is_rejected(self):
        bot = self._bot(bili_write_quota={"like": 3})
        grant = owner_grant(bot, "like", {"bvid": BVID})
        first = bili_gate.guard(bot, "like", {"bvid": BVID}, grant=grant)
        self.assertTrue(first["ok"], first)
        second = bili_gate.guard(bot, "like", {"bvid": BVID}, grant=grant)
        self.assertFalse(second["ok"])
        self.assertIn("凭证", second["error"])


class QuotaTests(Base):
    def test_negative_bool_and_string_quotas_are_config_errors(self):
        for bad in (-1, True, False, "5", 1.5):
            with self.subTest(quota=bad):
                bot = make_bot(self.dir, {
                    "bili_write_enabled": True, "bili_write_like": True,
                    "bili_write_quota": {"like": bad},
                })
                out = bili_gate.guard(
                    bot, "like", {"bvid": BVID},
                    grant=owner_grant(bot, "like", {"bvid": BVID}))
                self.assertFalse(out["ok"], out)
                self.assertIn("不限", out["error"])

    def test_zero_quota_means_unlimited(self):
        bot = make_bot(self.dir, {
            "bili_write_enabled": True, "bili_write_like": True,
            "bili_write_quota": {"like": 0},
        })
        for bv in (BVID, BVID2, "BV1zz411c7mF"):
            args = {"bvid": bv}
            out = bili_gate.guard(bot, "like", dict(args),
                                  grant=owner_grant(bot, "like", args))
            self.assertTrue(out["ok"], out)
            settled = bili_gate.settle(bot, out["reservation"],
                                       {"ok": True, "data": {"code": 0}})
            self.assertEqual(settled["status"], "confirmed")
        self.assertEqual(bili_gate.status(bot)["used"]["like"], 3)

    def test_daily_quota_is_enforced_per_action(self):
        bot = make_bot(self.dir, {
            "bili_write_enabled": True, "bili_write_like": True,
            "bili_write_quota": {"like": 1},
        })
        args = {"bvid": BVID}
        first = bili_gate.guard(bot, "like", args,
                                grant=owner_grant(bot, "like", args))
        self.assertTrue(first["ok"], first)
        bili_gate.settle(bot, first["reservation"],
                         {"ok": True, "data": {}}, verified=True)
        args2 = {"bvid": BVID2}
        second = bili_gate.guard(bot, "like", args2,
                                 grant=owner_grant(bot, "like", args2))
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "quota_exhausted")


class ReactionActionTests(Base):
    def _bot(self, **tools):
        base = dict(bili_write_enabled=True, bili_write_like=True,
                    bili_write_favorite=True, bili_write_coin=True,
                    bili_write_quota={"like": 2, "favorite": 1, "coin": 1})
        base.update(tools)
        return super()._bot(**base)

    async def test_like_success_and_verification_is_informational(self):
        bot = self._bot()
        args = {"bvid": BVID}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "like_video",
                             new=AsyncMock(return_value={"code": 0})) as backend, \
                patch.object(bili_extra, "verify_like",
                             new=AsyncMock(return_value=True)) as verify:
            out = await bili_scroll.action(bot, "like", args,
                                           grant=owner_grant(bot, "like", args))
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), True)
        backend.assert_awaited_once_with(BVID)
        verify.assert_awaited_once_with(BVID)

    async def test_like_unverified_is_still_success_but_noted(self):
        bot = self._bot()
        args = {"bvid": BVID}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "like_video",
                             new=AsyncMock(return_value={"code": 0})), \
                patch.object(bili_extra, "verify_like",
                             new=AsyncMock(return_value=False)):
            out = await bili_scroll.action(bot, "like", args,
                                           grant=owner_grant(bot, "like", args))
        # 云端语义：后端成功即成功；读后核验只是附加信息。
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), False)
        self.assertEqual(bili_gate.status(bot)["used"]["like"], 1)
        # 成功过一次的同目标仍然去重（发布类动作不变）。
        again_args = {"bvid": BVID}
        again = bili_gate.guard(
            bot, "like", again_args, grant=owner_grant(bot, "like", again_args))
        self.assertFalse(again["ok"])
        self.assertEqual(again["code"], "dedupe")

    async def test_like_backend_error_is_unconfirmed(self):
        bot = self._bot()
        args = {"bvid": BVID}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "like_video",
                             new=AsyncMock(return_value={"code": -101,
                                                         "message": "账号未登录"})):
            out = await bili_scroll.action(bot, "like", args,
                                           grant=owner_grant(bot, "like", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("unconfirmed"), out)
        self.assertIn("错误码", out["error"])
        self.assertEqual(bili_gate.status(bot)["used"]["like"], 1)

    async def test_network_error_is_unconfirmed_and_consumes_quota(self):
        bot = self._bot()
        args = {"bvid": BVID}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "like_video",
                             new=AsyncMock(side_effect=TimeoutError("timeout"))):
            out = await bili_scroll.action(bot, "like", args,
                                           grant=owner_grant(bot, "like", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("unconfirmed"), out)
        self.assertEqual(bili_gate.status(bot)["used"]["like"], 1)

    async def test_favorite_success_requires_verification(self):
        bot = self._bot()
        args = {"bvid": BVID}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "favorite_video",
                             new=AsyncMock(return_value={"code": 0})) as backend, \
                patch.object(bili_extra, "verify_favorite",
                             new=AsyncMock(return_value=True)):
            out = await bili_scroll.action(bot, "favorite", args,
                                           grant=owner_grant(bot, "favorite", args))
        self.assertTrue(out["ok"], out)
        backend.assert_awaited_once()
        self.assertEqual(backend.await_args.args[1], BVID)

    async def test_coin_success_requires_delta_not_absolute_count(self):
        bot = self._bot()
        args = {"bvid": BVID, "num": 1}
        # 已有 1 个币；投完变 2 才算这次真的投进去了（核验仍要按增量）。
        verify = AsyncMock(side_effect=[1, 2])
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "coin_video",
                             new=AsyncMock(return_value={"code": 0})) as backend, \
                patch.object(bili_extra, "verify_coin", new=verify):
            out = await bili_scroll.action(bot, "coin", args,
                                           grant=owner_grant(bot, "coin", args))
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), True)
        backend.assert_awaited_once_with(BVID, 1)

    async def test_coin_without_delta_is_still_success_with_verified_false(self):
        bot = self._bot()
        args = {"bvid": BVID, "num": 1}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "coin_video",
                             new=AsyncMock(return_value={"code": 0})), \
                patch.object(bili_extra, "verify_coin",
                             new=AsyncMock(side_effect=[1, 1])):
            out = await bili_scroll.action(bot, "coin", args,
                                           grant=owner_grant(bot, "coin", args))
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), False)

    async def test_coin_pre_read_failure_does_not_spend(self):
        bot = self._bot()
        args = {"bvid": BVID, "num": 1}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "coin_video",
                             new=AsyncMock(return_value={"code": 0})) as backend, \
                patch.object(bili_extra, "verify_coin",
                             new=AsyncMock(return_value=None)):
            out = await bili_scroll.action(bot, "coin", args,
                                           grant=owner_grant(bot, "coin", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("unconfirmed"), out)
        backend.assert_not_awaited()


class DeleteCommentTests(Base):
    def _bot(self, **tools):
        base = dict(bili_write_enabled=True, bili_write_delete_comment=True,
                    bili_write_quota={"delete_comment": 1})
        base.update(tools)
        return super()._bot(**base)

    async def test_single_own_comment_is_deleted_once(self):
        bot = self._bot()
        args = {"bvid": BVID, "rpid": 123456}
        state = AsyncMock(side_effect=[
            {"state": "present", "mid": MY_MID, "oid": 777, "text": "hi"},
            {"state": "gone", "oid": 777},
        ])
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "my_mid", return_value=MY_MID), \
                patch.object(bili_extra, "comment_state", new=state), \
                patch.object(bili_extra, "delete_comment",
                             new=AsyncMock(return_value={"code": 0})) as backend:
            out = await bili_scroll.action(
                bot, "delete_comment", args,
                grant=owner_grant(bot, "delete_comment", args))
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), True)
        backend.assert_awaited_once_with(777, 123456)
        self.assertEqual(state.await_count, 2)

    async def test_other_author_is_refused_before_backend(self):
        bot = self._bot()
        args = {"bvid": BVID, "rpid": 123456}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "my_mid", return_value=MY_MID), \
                patch.object(bili_extra, "comment_state",
                             new=AsyncMock(return_value={
                                 "state": "present", "mid": "8888",
                                 "oid": 777, "text": "别人的评论"})), \
                patch.object(bili_extra, "delete_comment",
                             new=AsyncMock(return_value={"code": 0})) as backend:
            out = await bili_scroll.action(
                bot, "delete_comment", args,
                grant=owner_grant(bot, "delete_comment", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("denied"), out)
        self.assertIn("不是凭证账户", out["error"])
        backend.assert_not_awaited()
        # 没预留、没扣额度：拒绝别人评论不该消耗今天的删除额度。
        self.assertEqual(bili_gate.status(bot)["used"]["delete_comment"], 0)

    async def test_unknown_author_refuses_without_delete(self):
        bot = self._bot()
        args = {"bvid": BVID, "rpid": 123456}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "my_mid", return_value=MY_MID), \
                patch.object(bili_extra, "comment_state",
                             new=AsyncMock(return_value={
                                 "state": "unknown", "reason": "timeout"})), \
                patch.object(bili_extra, "delete_comment",
                             new=AsyncMock(return_value={"code": 0})) as backend:
            out = await bili_scroll.action(
                bot, "delete_comment", args,
                grant=owner_grant(bot, "delete_comment", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("denied"), out)
        backend.assert_not_awaited()

    async def test_delete_without_identity_is_refused(self):
        bot = self._bot()
        args = {"bvid": BVID, "rpid": 123456}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "my_mid", return_value=""), \
                patch.object(bili_extra, "delete_comment",
                             new=AsyncMock(return_value={"code": 0})) as backend:
            out = await bili_scroll.action(
                bot, "delete_comment", args,
                grant=owner_grant(bot, "delete_comment", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("denied"), out)
        backend.assert_not_awaited()

    async def test_delete_succeeds_even_if_read_back_misses(self):
        bot = self._bot()
        args = {"bvid": BVID, "rpid": 123456}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "my_mid", return_value=MY_MID), \
                patch.object(bili_extra, "comment_state", new=AsyncMock(side_effect=[
                    {"state": "present", "mid": MY_MID, "oid": 777, "text": "hi"},
                    {"state": "unknown", "reason": "timeout"},
                ])), \
                patch.object(bili_extra, "delete_comment",
                             new=AsyncMock(return_value={"code": 0})):
            out = await bili_scroll.action(
                bot, "delete_comment", args,
                grant=owner_grant(bot, "delete_comment", args))
        self.assertTrue(out["ok"], out)
        self.assertIs(out.get("verified"), False)
        self.assertEqual(bili_gate.status(bot)["used"]["delete_comment"], 1)

    async def test_delete_backend_error_is_unconfirmed(self):
        bot = self._bot()
        args = {"bvid": BVID, "rpid": 123456}
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "my_mid", return_value=MY_MID), \
                patch.object(bili_extra, "comment_state",
                             new=AsyncMock(return_value={
                                 "state": "present", "mid": MY_MID,
                                 "oid": 777, "text": "hi"})), \
                patch.object(bili_extra, "delete_comment",
                             new=AsyncMock(side_effect=RuntimeError("denied"))):
            out = await bili_scroll.action(
                bot, "delete_comment", args,
                grant=owner_grant(bot, "delete_comment", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("unconfirmed"), out)


class SchedulerScopeTests(Base):
    def test_scheduler_cannot_authorize_coin_or_delete(self):
        cap = bili_gate.register_scheduler("offline-test")
        for action in ("coin", "delete_comment", "delete_recent", "delete_on_video",
                       "like", "favorite", "react"):
            with self.subTest(action=action):
                self.assertIsNone(bili_gate.scheduler_grant(
                    cap, action=action, target=BVID))
        self.assertIsNotNone(bili_gate.scheduler_grant(
            cap, action="reply", target=f"{BVID}|1"))

    def test_scheduler_switch_can_still_close_scheduler_writes(self):
        cap = bili_gate.register_scheduler("offline-test-2")
        args = {"bvid": BVID, "text": "hi", "rpid": 1}
        grant = bili_gate.scheduler_grant(
            cap, action="reply", target=bili_gate.target_key("reply", args))
        # 默认（bili_write_scheduler 不填）按字面 True 放行。
        bot = self._bot(bili_write_enabled=True, bili_write_reply=True)
        self.assertTrue(bili_gate.guard(bot, "reply", dict(args), grant=grant)["ok"])
        # 显式关掉就仍然拦得住（闸门是可关的总开关）。
        closed = make_bot(self.dir, {
            "bili_write_enabled": True, "bili_write_reply": True,
            "bili_write_scheduler": False,
        })
        grant2 = bili_gate.scheduler_grant(
            cap, action="reply", target=bili_gate.target_key("reply", args))
        out = bili_gate.guard(closed, "reply", dict(args), grant=grant2)
        self.assertFalse(out["ok"])
        self.assertIn("bili_write_scheduler", out["error"])


class NoImplicitAuthorizationTests(Base):
    async def test_auto_interaction_never_uses_coin_or_delete(self):
        bot = make_bot(self.dir, {
            "bili_auto_enabled": True, "bili_auto_reply": True,
            "bili_auto_comment": True, "bili_auto_danmaku": True,
            "bili_auto_daily_limit": 20,
        })
        item = {"notify_id": "n1", "bvid": BVID, "root_id": 1,
                "source_id": 2, "nickname": "网友", "text": "hi",
                "source_text": "yo", "reply_time": int(time.time())}
        calls = []

        async def fake_action(bot_, name, args=None, grant=None):
            calls.append(name)
            return {"ok": True, "data": {}}

        with patch.object(bili_auto, "_draft", new=AsyncMock(return_value="好的")), \
                patch.object(bili_extra, "received_replies",
                             new=AsyncMock(return_value=[item])), \
                patch.object(bili_scroll, "recent_notes", return_value=[]), \
                patch.object(bili_scroll, "action", new=fake_action):
            await bili_auto.run_round(
                bot, grant_factory=lambda action, target: object())
        self.assertTrue(calls)
        self.assertFalse({"coin", "delete_comment", "like", "favorite"} & set(calls),
                         calls)

    async def test_natural_language_delete_without_owner_ctx_is_denied(self):
        bot = self._bot(bili_jct="jct")
        delete_recent = AsyncMock(return_value={"ok": True, "deleted": [],
                                                "failed": [], "reason": ""})
        with patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "delete_recent", new=delete_recent):
            out = await intent.run(
                bot, {"text": "把评论删了", "count": 1,
                      "tasks": [{"action": "delcomment", "count": 1}]})
        result = out["tasks"][0]["result"]
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("denied"), result)
        self.assertIn("凭证", result["error"])
        delete_recent.assert_not_awaited()

    def test_model_tool_cannot_see_coin_or_delete(self):
        allowed = tools_mod._bili_allowed_actions()
        self.assertNotIn("coin", allowed)
        self.assertNotIn("delete_comment", allowed)
        for name in ("like", "favorite", "my_comments"):
            self.assertIn(name, allowed)

    async def test_model_like_call_binds_action_and_target(self):
        tb = object.__new__(tools_mod.ToolBox)
        tb.bot = SimpleNamespace()
        tb._is_owner = lambda ctx: True
        tb._current_request_text = lambda ctx: "帮我给这个视频点赞"
        tb._external_tainted = lambda ctx: False
        seen = {}

        def fake_grant(bot, ctx, *, action, target, budget=1):
            seen.update({"action": action, "target": target})
            return object()

        action_mock = AsyncMock(return_value={"ok": True, "data": {}})
        with patch.object(tools_mod.bili_gate, "owner_turn_grant",
                          new=fake_grant), \
                patch.object(tools_mod.bili_scroll, "action", new=action_mock), \
                patch.object(tools_mod.bili_gate, "READ_ACTIONS", frozenset()):
            out, _ = await tb._t_bili_action(None, "like", {"bvid": BVID})
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual(seen, {"action": "like", "target": BVID})
        action_mock.assert_awaited_once()


class AdminCommandTests(Base):
    def _bot(self, **tools):
        base = dict(bili_write_enabled=True, bili_write_like=True,
                    bili_write_coin=True, bili_write_delete_comment=True)
        base.update(tools)
        return super()._bot(**base)

    async def _run_cmd(self, cmd, ctx):
        async def fake_bg(ctx_, factory, on_done):
            await factory()

        with patch.object(admin_cmds, "_bg", new=fake_bg):
            await cmd(ctx)

    async def test_owner_coin_command_mints_bound_grant(self):
        bot = self._bot()
        ctx = make_ctx(bot, arg_text=f"{BVID} 2")
        action = AsyncMock(return_value={"ok": True, "data": {}})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_coin, ctx)
        action.assert_awaited_once()
        args = action.await_args.args
        self.assertEqual(args[1], "coin")
        self.assertEqual(args[2], {"bvid": BVID, "num": 2})
        grant = action.await_args.kwargs.get("grant")
        self.assertIsInstance(grant, bili_gate.WriteGrant)
        self.assertEqual(grant.action, "coin")
        self.assertEqual(grant.target, BVID)

    async def test_delete_without_arg_lists_my_comments(self):
        bot = self._bot()
        ctx = make_ctx(bot, arg_text="")
        results = {}

        async def fake_bg(ctx_, factory, on_done):
            results["r"] = await factory()
            results["text"] = on_done(results["r"])

        with patch.object(bili_extra, "my_comments",
                          return_value=[{"text": "旧话", "bvid": BVID, "rpid": 1}]), \
                patch.object(admin_cmds, "_bg", new=fake_bg):
            await admin_cmds.cmd_del_comment(ctx)
        self.assertIn("我发过的评论", results["text"])
        self.assertIn("旧话", results["text"])
        self.assertIn("//删评论 全部", results["text"])
        ctx.reply.assert_awaited()

    async def test_delete_all_maps_to_delete_recent(self):
        bot = self._bot()
        action = AsyncMock(return_value={"ok": True, "deleted": [], "failed": [],
                                         "reason": ""})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_del_comment,
                                make_ctx(bot, arg_text="全部"))
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "delete_recent")
        self.assertEqual(action.await_args.args[2], {"n": 999})
        grant = action.await_args.kwargs.get("grant")
        self.assertIsInstance(grant, bili_gate.WriteGrant)
        self.assertEqual(grant.action, "delete_recent")
        self.assertEqual(grant.target, "recent|999")

    async def test_delete_index_maps_to_delete_by_index(self):
        bot = self._bot()
        action = AsyncMock(return_value={"ok": True, "deleted": [], "failed": [],
                                         "reason": ""})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_del_comment,
                                make_ctx(bot, arg_text="2"))
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "delete_by_index")
        self.assertEqual(action.await_args.args[2], {"index": 2})
        grant = action.await_args.kwargs.get("grant")
        self.assertEqual(grant.target, "index|2")

    async def test_delete_video_maps_to_delete_on_video(self):
        bot = self._bot()
        action = AsyncMock(return_value={"ok": True, "deleted": [], "failed": [],
                                         "reason": ""})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_del_comment,
                                make_ctx(bot, arg_text=BVID))
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "delete_on_video")
        self.assertEqual(action.await_args.args[2], {"bvid": BVID})
        grant = action.await_args.kwargs.get("grant")
        self.assertEqual(grant.action, "delete_on_video")
        self.assertEqual(grant.target, BVID)

    async def test_delete_text_maps_to_delete_by_text(self):
        bot = self._bot()
        action = AsyncMock(return_value={"ok": True, "deleted": [], "failed": [],
                                         "reason": ""})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_del_comment,
                                make_ctx(bot, arg_text="旧话"))
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "delete_by_text")
        self.assertEqual(action.await_args.args[2], {"text": "旧话"})
        grant = action.await_args.kwargs.get("grant")
        self.assertEqual(
            grant.target,
            bili_gate.target_key("delete_by_text", {"text": "旧话"}))

    async def test_whitelist_cannot_delete(self):
        bot = self._bot()
        ctx = make_ctx(bot, uid=OTHER, arg_text="全部")
        action = AsyncMock(return_value={"ok": True})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await admin_cmds.cmd_del_comment(ctx)
        self.assertIn("只能由主人本人", ctx.reply.await_args.args[0])
        action.assert_not_awaited()

    async def test_my_comments_is_a_read_without_grant(self):
        bot = self._bot()
        ctx = make_ctx(bot, arg_text=BVID)
        action = AsyncMock(return_value={"ok": True,
                                         "data": {"count": 0, "items": []}})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_my_comments, ctx)
        action.assert_awaited_once_with(
            bot, "my_comments", {"bvid": BVID, "limit": 20})


class BatchAndReactTests(Base):
    """云端等价：批量删评论与 react 可达；0 额度不拦截；闸门关掉仍拦截。"""

    def _bot(self, **tools):
        base = dict(bili_write_enabled=True)
        base.update(tools)
        return super()._bot(**base)

    async def test_batch_delete_reaches_backend_with_owner_grant(self):
        bot = self._bot()
        args = {"n": 3}
        backend = AsyncMock(return_value={
            "ok": True, "deleted": [{"rpid": 1, "text": "a"}], "failed": []})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "delete_recent", new=backend):
            out = await bili_scroll.action(
                bot, "delete_recent", dict(args),
                grant=owner_grant(bot, "delete_recent", args))
        self.assertTrue(out["ok"], out)
        backend.assert_awaited_once_with(bot, 3)
        self.assertEqual(bili_gate.status(bot)["used"]["delete_recent"], 1)

    async def test_batch_delete_nothing_deleted_is_not_success(self):
        bot = self._bot()
        args = {"n": 1}
        backend = AsyncMock(return_value={
            "ok": False, "deleted": [], "failed": [], "reason": "一条都没删掉"})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "delete_recent", new=backend):
            out = await bili_scroll.action(
                bot, "delete_recent", dict(args),
                grant=owner_grant(bot, "delete_recent", args))
        self.assertFalse(out["ok"], out)
        self.assertIn("一条都没删掉", out["error"])

    async def test_react_reaches_backend_with_owner_grant(self):
        bot = self._bot()
        args = {"bvid": BVID,
                "judge": {"like": True, "fav": False, "coin": 0}}
        backend = AsyncMock(return_value={"like": True, "fav": False, "coin": 0,
                                          "errors": []})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "react", new=backend):
            out = await bili_scroll.action(
                bot, "react", dict(args), grant=owner_grant(bot, "react", args))
        self.assertTrue(out["ok"], out)
        backend.assert_awaited_once()
        self.assertEqual(backend.await_args.args[1], BVID)
        self.assertTrue(backend.await_args.args[2]["like"])

    async def test_react_all_failed_is_not_success(self):
        bot = self._bot()
        args = {"bvid": BVID, "judge": {"like": True, "coin": 1}}
        backend = AsyncMock(return_value={"like": False, "fav": False, "coin": 0,
                                          "errors": ["点赞 RuntimeError: boom"]})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "react", new=backend):
            out = await bili_scroll.action(
                bot, "react", dict(args), grant=owner_grant(bot, "react", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("unconfirmed"), out)

    async def test_gate_switch_off_still_blocks_batch_delete(self):
        bot = self._bot(bili_write_enabled=False)
        args = {"n": 2}
        backend = AsyncMock(return_value={"ok": True, "deleted": [1], "failed": []})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "delete_recent", new=backend):
            out = await bili_scroll.action(
                bot, "delete_recent", dict(args),
                grant=owner_grant(bot, "delete_recent", args))
        self.assertFalse(out["ok"], out)
        self.assertTrue(out.get("denied"), out)
        self.assertIn("没开", out["error"])
        backend.assert_not_awaited()

    async def test_zero_quota_does_not_block_batch_delete(self):
        bot = self._bot(bili_write_quota={"delete_recent": 0})
        backend = AsyncMock(return_value={"ok": True, "deleted": [1], "failed": []})
        with patch.object(bili_scroll, "_bili_jct", return_value="jct"), \
                patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()), \
                patch.object(bili_extra, "delete_recent", new=backend):
            for n in (1, 2, 3):
                args = {"n": n}
                out = await bili_scroll.action(
                    bot, "delete_recent", dict(args),
                    grant=owner_grant(bot, "delete_recent", args))
                self.assertTrue(out["ok"], out)
        self.assertEqual(backend.await_count, 3)


if __name__ == "__main__":
    unittest.main()
