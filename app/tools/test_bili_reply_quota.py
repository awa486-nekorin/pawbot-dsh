"""Offline tests: 每视频每天回复额度（2026-11-03 生产热修 replycap 移植）。

No network, no real B 站 / QQ, no credential, no app/data: every bot gets its own
temporary data root and ``bilibili_api.comment.send_comment`` is an AsyncMock, so a
"publish" here never leaves the process.

Pinned semantics:
- 同一个视频一天最多 ``tools.bili_auto_reply_per_video_daily`` 条（默认 5，0=不限）；
- 额度用完 → **发布前**抛错，后端 ``send_comment`` 一次都不调；
- 超限在自动互动里记成 ``skipped_quota``（日志带数量），不是静默吞掉；
- 计键按视频（bvid，拿不到才退回 oid），不同视频互不影响；计数按自然日，次日重置。
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

from pawbot import bili_auto, bili_extra, bili_scroll
from pawbot.paths import DataPaths

BVID = "BV1xx411c7mD"
VID2 = "BV1yy411c7mE"


class Cfg:
    """最小的 bot.config：按 ``a.b.c`` 逐段取。"""

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


def make_bot(tmp, tools=None):
    cfg = {"account": {"owner_ids": ["10001"]}, "tools": dict(tools or {})}
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    return SimpleNamespace(
        config=Cfg(cfg),
        base_dir=tmp,
        paths=DataPaths(root=tmp, base_dir=tmp),
    )


class ReplyQuotaTests(unittest.IsolatedAsyncioTestCase):
    """bili_extra 的账本 + reply_comment 发布前检查。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def bot(self, **tools):
        return make_bot(self.dir, tools)

    def fill(self, bot, bvid, n):
        for _ in range(int(n)):
            bili_extra._bump_reply(bot, bili_extra._reply_key(bvid))

    def yesterday(self):
        return time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))

    async def publish(self, bot, *, bvid=BVID, oid=1001, text="回一句"):
        send = AsyncMock(return_value={"rpid": 9})
        with patch.object(bili_extra, "_cred", return_value=object()), \
                patch("bilibili_api.comment.send_comment", new=send):
            out = await bili_extra.reply_comment(oid, text, bot=bot, bvid=bvid)
        return out, send

    async def test_under_cap_publishes_and_counts(self):
        bot = self.bot()                       # 没配就是默认 5
        self.assertEqual(bili_extra._reply_cap(bot), 5)
        self.assertEqual(bili_extra.reply_left_today(bot, BVID), 5)

        out, send = await self.publish(bot)

        self.assertEqual(out, {"rpid": 9})
        send.assert_awaited_once()
        # 键按视频：同一视频发过一条就记在这个视频头上。
        self.assertEqual(bili_extra.reply_used_today(bot, BVID), 1)
        self.assertEqual(bili_extra.reply_left_today(bot, BVID), 4)

    async def test_over_cap_is_rejected_before_backend_call(self):
        bot = self.bot(bili_auto_reply_per_video_daily=2)
        self.fill(bot, BVID, 2)
        send = AsyncMock(return_value={"rpid": 9})
        with patch.object(bili_extra, "_cred", return_value=object()), \
                patch("bilibili_api.comment.send_comment", new=send):
            with self.assertRaises(RuntimeError) as ctx:
                await bili_extra.reply_comment(1001, "再回一句", bot=bot, bvid=BVID)

        self.assertIn("额度用完", str(ctx.exception))
        # 关键：额度不够时**一次后端调用都没有**，也还没记第二条。
        send.assert_not_awaited()
        self.assertEqual(bili_extra.reply_used_today(bot, BVID), 2)

    async def test_zero_means_unlimited(self):
        bot = self.bot(bili_auto_reply_per_video_daily=0)
        self.fill(bot, BVID, 50)
        self.assertGreater(bili_extra.reply_left_today(bot, BVID), 0)

        _, send = await self.publish(bot)

        send.assert_awaited_once()
        self.assertEqual(bili_extra.reply_used_today(bot, BVID), 51)

    async def test_videos_do_not_share_quota(self):
        bot = self.bot(bili_auto_reply_per_video_daily=2)
        self.fill(bot, VID2, 2)
        self.assertEqual(bili_extra.reply_left_today(bot, VID2), 0)

        _, send = await self.publish(bot, bvid=BVID)
        send.assert_awaited_once()
        self.assertEqual(bili_extra.reply_used_today(bot, BVID), 1)

        send2 = AsyncMock(return_value={"rpid": 9})
        with patch.object(bili_extra, "_cred", return_value=object()), \
                patch("bilibili_api.comment.send_comment", new=send2):
            with self.assertRaises(RuntimeError):
                await bili_extra.reply_comment(2002, "回另一条", bot=bot, bvid=VID2)
        send2.assert_not_awaited()
        self.assertEqual(bili_extra.reply_used_today(bot, VID2), 2)

    async def test_next_day_resets(self):
        bot = self.bot(bili_auto_reply_per_video_daily=1)
        acts = bili_extra._load_actions(bot)
        acts["reply_daily"][self.yesterday()] = {BVID: 1}
        bili_extra._save_actions(bot, acts)

        # 昨天用满了，今天从 0 开始。
        self.assertEqual(bili_extra.reply_used_today(bot, BVID), 0)
        self.assertEqual(bili_extra.reply_left_today(bot, BVID), 1)
        _, send = await self.publish(bot)

        send.assert_awaited_once()
        self.assertEqual(bili_extra.reply_used_today(bot, BVID), 1)
        # 昨天的记录不被今天覆盖（只影响日志/清理，不参与今天的判定）。
        self.assertEqual(
            bili_extra._load_actions(bot)["reply_daily"][self.yesterday()][BVID], 1)

    async def test_key_falls_back_to_oid_without_bvid(self):
        self.assertEqual(bili_extra._reply_key(BVID, 123), BVID)
        self.assertEqual(bili_extra._reply_key("", 456), "oid:456")

        bot = self.bot(bili_auto_reply_per_video_daily=1)
        await self.publish(bot, bvid="", oid=456)
        self.assertEqual(bili_extra.reply_used_today(bot, "oid:456"), 1)
        with self.assertRaises(RuntimeError):
            await self.publish(bot, bvid="", oid=456)

    async def test_ledger_initialises_reply_daily_bucket(self):
        bot = self.bot()
        # 全新账本
        self.assertEqual(bili_extra._load_actions(bot)["reply_daily"], {})
        # 老账本（没有 reply_daily）读出来也要自动补上这个桶。
        path = bili_extra._actions_path(bot)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"liked": {}, "faved": {}, "coined": {}, "daily": {}}')
        acts = bili_extra._load_actions(bot)
        self.assertIn("reply_daily", acts)
        self.assertEqual(acts["reply_daily"], {})


class RoundSkipTests(unittest.IsolatedAsyncioTestCase):
    """bili_auto.run_round：超限不算错误、计入 skipped_quota、日志带数量。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def bot(self, **tools):
        base = dict(bili_auto_enabled=True, bili_auto_reply=True,
                    bili_auto_comment=False, bili_auto_danmaku=False,
                    bili_auto_daily_limit=0, bili_auto_reply_limit=0)
        base.update(tools)
        return make_bot(self.dir, base)

    def note(self):
        path = os.path.join(self.dir, "note.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 测试视频\n" + BVID + "\n")
        return [{"path": path}]

    def item(self, nid, bvid=BVID):
        return {"notify_id": nid, "bvid": bvid, "root_id": 111,
                "source_id": 222, "nickname": "网友", "text": "hi",
                "source_text": "yo", "reply_time": int(time.time())}

    async def test_full_quota_is_skipped_not_replied(self):
        bot = self.bot(bili_auto_reply_per_video_daily=1)
        for _ in range(1):
            bili_extra._bump_reply(bot, bili_extra._reply_key(BVID))
        action = AsyncMock(return_value={"ok": True, "data": {}})
        fake_log = Mock()
        with patch.object(bili_auto, "_draft", new=AsyncMock(return_value="好的")), \
                patch.object(bili_extra, "received_replies",
                             new=AsyncMock(return_value=[self.item("n1"),
                                                         self.item("n2", VID2)])), \
                patch.object(bili_scroll, "action", new=action), \
                patch.object(bili_auto.log, "info", new=fake_log):
            out = await bili_auto.run_round(
                bot, reason="定时", grant_factory=lambda action, target: object())

        self.assertTrue(out["ok"], out)
        # 满额的那个视频一个写动作都不发，另一个视频照常回。
        self.assertEqual(len(out["skipped_quota"]), 1)
        self.assertEqual(out["skipped_quota"], [BVID])
        self.assertEqual(out["replied"], ["好的"])
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "reply")
        self.assertEqual(action.await_args.args[2]["bvid"], VID2)
        # 不是「静默跳过」：一轮结束的日志里带着数量。
        self.assertTrue(fake_log.called)
        行 = fake_log.call_args.args[0]
        self.assertIn("1 个视频今天回复已满 1 条，跳过", 行)

    async def test_successful_reply_bumps_every_video_quota(self):
        bot = self.bot(bili_auto_reply_per_video_daily=5)
        action = AsyncMock(return_value={"ok": True, "data": {}})
        with patch.object(bili_auto, "_draft", new=AsyncMock(return_value="好的")), \
                patch.object(bili_extra, "received_replies",
                             new=AsyncMock(return_value=[self.item("n3")])), \
                patch.object(bili_scroll, "action", new=action):
            out = await bili_auto.run_round(
                bot, grant_factory=lambda action, target: object())

        self.assertEqual(out["replied"], ["好的"])
        self.assertEqual(out["skipped_quota"], [])
        # 本地回复走 vendored bili_reply，成功后由 run_round 补记这一笔。
        self.assertEqual(bili_extra.reply_used_today(bot, BVID), 1)
        self.assertEqual(bili_extra.reply_left_today(bot, BVID), 4)

    async def test_comment_round_falls_back_to_top_level_comment(self):
        bot = self.bot(bili_auto_reply=False, bili_auto_comment=True,
                       bili_auto_reply_per_video_daily=1,
                       bili_auto_reply_others_chance=1,
                       bili_auto_comment_limit=1)
        bili_extra._bump_reply(bot, bili_extra._reply_key(BVID))
        action = AsyncMock(return_value={"ok": True, "data": {}})
        with patch.object(bili_auto, "_note_meta", return_value={"bvid": BVID,
                                                                 "title": "t"}):
            with patch.object(bili_auto, "_subtitle", new=AsyncMock(return_value="")), \
                    patch.object(bili_auto, "_draft",
                                 new=AsyncMock(return_value="自己说一句")), \
                    patch.object(bili_scroll, "recent_notes",
                                 return_value=self.note()), \
                    patch.object(bili_scroll, "action", new=action):
                out = await bili_auto.run_round(
                    bot, grant_factory=lambda action, target: object())

        # 回复额度满了 → 不回评论，改成发顶层评论；跳过照样记一笔。
        self.assertEqual(out["skipped_quota"], [BVID])
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "comment")
        self.assertEqual(len(out["commented"]), 1)

    async def test_reply_to_someone_counts_as_replied_not_commented(self):
        bot = self.bot(bili_auto_reply=False, bili_auto_comment=True,
                       bili_auto_reply_others_chance=1,
                       bili_auto_comment_limit=1)
        action = AsyncMock(return_value={"ok": True, "data": {}})
        pool = [{"rpid": 333, "user": "网友", "content": "这关卡也太难了"}]
        with patch.object(bili_auto, "_note_meta", return_value={"bvid": BVID,
                                                                 "title": "t"}):
            with patch.object(bili_auto, "_subtitle", new=AsyncMock(return_value="")), \
                    patch.object(bili_auto, "_comments_meta",
                                 new=AsyncMock(return_value=pool)), \
                    patch.object(bili_auto, "_draft",
                                 new=AsyncMock(return_value="确实难")), \
                    patch.object(bili_scroll, "recent_notes",
                                 return_value=self.note()), \
                    patch.object(bili_scroll, "action", new=action):
                out = await bili_auto.run_round(
                    bot, grant_factory=lambda action, target: object())

        # 回的是别人的评论 → 计入 replied（文本），不是顶层评论 commented。
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "reply")
        self.assertEqual(out["replied"], ["确实难"])
        self.assertEqual(out["commented"], [])


if __name__ == "__main__":
    unittest.main()
