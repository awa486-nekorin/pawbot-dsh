"""QQ 空间自主 capability 与旧手动路径的隔离回归。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.qzone import Qzone


class QzoneAutonomyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {
            "qzone.enabled": True,
            "qzone.proactive_enabled": True,
            "qzone.owner_ids": [7],
            "qzone.daily_limit": 5,
            "qzone.actions.list": "get_qzone_msg_list",
        }
        self.conn = SimpleNamespace(call=AsyncMock(return_value={
            "list": [{"tid": "t1", "content": "自己以前的说说",
                      "created_time": 100}],
            "tid": "new-tid",
        }))
        self.bot = SimpleNamespace(base_dir=self.tmp.name, config=self.config,
                                   conn_for=lambda: self.conn)
        self.qz = Qzone(self.bot)

    def ctx(self, uid=7, group_id=None):
        return SimpleNamespace(event=SimpleNamespace(
            type="message", message_type="group" if group_id else "private",
            user_id=uid, group_id=group_id))

    async def test_capability_requires_both_explicit_gates(self):
        self.assertIsNotNone(self.qz.issue_autonomy_capability("life.autonomy"))
        capability = self.qz.issue_autonomy_capability("life.autonomy")
        for key in ("qzone.enabled", "qzone.proactive_enabled"):
            with self.subTest(key=key):
                self.config[key] = False
                self.assertIsNone(self.qz.issue_autonomy_capability("life.autonomy"))
                result = await self.qz.autonomy_read(capability, limit=2)
                self.assertFalse(result["ok"])
                self.config[key] = True

    async def test_autonomy_read_does_not_claim_owner_context(self):
        capability = self.qz.issue_autonomy_capability("life.autonomy")
        result = await self.qz.autonomy_read(capability, limit=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["tid"], "t1")
        self.conn.call.assert_any_await("get_qzone_msg_list", timeout=15)

    async def test_autonomy_post_ledger_actor_is_capability_not_owner(self):
        capability = self.qz.issue_autonomy_capability("life.autonomy")
        result = await self.qz.autonomy_post(capability, "自主发一条")
        self.assertTrue(result["ok"])
        self.assertEqual(result["tid"], "new-tid")
        record = self.qz.records[-1]
        self.assertEqual(record["status"], "confirmed")
        self.assertTrue(record["actor_id"].startswith("autonomy:"))
        self.assertNotEqual(record["actor_id"], "7")
        self.conn.call.assert_awaited_with(
            "send_qzone_msg", timeout=15, content="自主发一条", images=[],
            ugc_right=1)

    async def test_unconfirmed_autonomy_write_is_not_success(self):
        self.conn.call.return_value = {"ok": False}
        capability = self.qz.issue_autonomy_capability("life.autonomy")
        result = await self.qz.autonomy_post(capability, "没人确认")
        self.assertFalse(result["ok"])
        self.assertEqual(self.qz.records[-1]["status"], "unconfirmed")

    async def test_missing_or_foreign_capability_never_touches_backend(self):
        result = await self.qz.autonomy_post(None, "不该发")
        self.assertFalse(result["ok"])
        other = Qzone(SimpleNamespace(base_dir=self.tmp.name,
                                      config=dict(self.config),
                                      conn_for=lambda: self.conn))
        foreign = other.issue_autonomy_capability("life.autonomy")
        result = await self.qz.autonomy_post(foreign, "不该发")
        self.assertFalse(result["ok"])
        self.conn.call.assert_not_awaited()

    async def test_friend_feed_like_repost_need_explicit_backend(self):
        capability = self.qz.issue_autonomy_capability("life.autonomy")
        feed = await self.qz.autonomy_feed(capability)
        self.assertFalse(feed["ok"])
        self.assertIn("qzone.actions.feed", feed["reason"])
        for name in ("like", "repost"):
            with self.subTest(name=name):
                result = await self.qz.autonomy_interact(
                    capability, name, tid="t1")
                self.assertFalse(result["ok"])
                self.assertIn(f"qzone.actions.{name}", result["reason"])
        self.conn.call.assert_not_awaited()

        self.config["qzone.actions.feed"] = "configured_feed"
        self.config["qzone.actions.like"] = "configured_like"
        self.config["qzone.actions.repost"] = "configured_repost"
        self.conn.call.return_value = {"feeds": [{"tid": "f1", "content": "好友动态"}]}
        self.assertTrue((await self.qz.autonomy_feed(capability))["ok"])
        self.assertTrue((await self.qz.autonomy_interact(capability, "like",
                                                         tid="t1"))["ok"])
        self.assertTrue((await self.qz.autonomy_interact(capability, "repost",
                                                         tid="t1", content="转")
                         )["ok"])

    async def test_autonomy_read_own_posts_with_comments(self):
        capability = self.qz.issue_autonomy_capability("life.autonomy")

        async def call(action, **kwargs):
            if action == "get_qzone_msg_list":
                return {"list": [{"tid": "t1", "content": "自己以前的说说"}]}
            if action == "get_qzone_comments":
                return {"comments": [{"commentid": "c1", "content": "朋友的留言"}],
                        "content": "留言正文"}
            return {}

        self.conn.call = AsyncMock(side_effect=call)
        result = await self.qz.autonomy_read(
            capability, limit=2, source="own", include_comments=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "own")
        self.assertEqual(result["items"][0]["comments"][0]["id"], "c1")
        self.assertEqual(result["items"][0]["comments"][0]["text"], "朋友的留言")

    async def test_autonomy_read_feed_only_when_explicitly_configured(self):
        capability = self.qz.issue_autonomy_capability("life.autonomy")
        missing = await self.qz.autonomy_read(capability, source="feed")
        self.assertFalse(missing["ok"])
        self.assertIn("qzone.actions.feed", missing["reason"])

        self.config["qzone.actions.feed"] = "configured_feed"
        self.conn.call.return_value = {"feeds": [{"tid": "f1",
                                                  "content": "好友动态"}]}
        feed = await self.qz.autonomy_read(capability, source="feed", limit=2)
        self.assertTrue(feed["ok"])
        self.assertEqual(feed["source"], "feed")
        self.assertEqual(feed["items"][0]["tid"], "f1")

    async def test_autonomy_reply_like_repost_need_tid_and_comment_id(self):
        capability = self.qz.issue_autonomy_capability("life.autonomy")
        # 配置存在但缺真实 ID：后端绝不调用。
        self.config["qzone.actions.reply"] = "configured_reply"
        self.config["qzone.actions.like"] = "configured_like"
        self.config["qzone.actions.repost"] = "configured_repost"
        for name, kwargs in (("reply", {"tid": "t1"}),
                             ("reply", {"tid": "t1", "comment_id": "c1"}),
                             ("like", {}),
                             ("repost", {})):
            with self.subTest(name=name, kwargs=kwargs):
                result = await self.qz.autonomy_interact(capability, name, **kwargs)
                self.assertFalse(result["ok"])
        self.conn.call.assert_not_awaited()

        # 配置缺失时明确跳过。
        for key in ("qzone.actions.reply", "qzone.actions.like",
                    "qzone.actions.repost"):
            self.config.pop(key)
        missing = await self.qz.autonomy_interact(
            capability, "reply", tid="t1", comment_id="c1", content="回一句")
        self.assertFalse(missing["ok"])
        self.assertIn("qzone.actions.reply", missing["reason"])
        self.conn.call.assert_not_awaited()

        self.config["qzone.actions.reply"] = "configured_reply"
        self.config["qzone.actions.like"] = "configured_like"
        reply = await self.qz.autonomy_interact(
            capability, "reply", tid="t1", comment_id="c1", content="回一句")
        self.assertTrue(reply["ok"])
        self.assertEqual(self.qz.records[-1]["action"], "reply")
        self.assertEqual(self.qz.records[-1]["actor_id"], "autonomy:life.autonomy")
        self.conn.call.assert_awaited_with(
            "configured_reply", timeout=15, tid="t1", comment_id="c1",
            content="回一句")
        liked = await self.qz.autonomy_interact(capability, "like", tid="t1")
        self.assertTrue(liked["ok"])
        self.assertEqual(self.qz.records[-1]["action"], "like")

    async def test_manual_owner_path_and_ledger_still_work(self):
        result = await self.qz.post("手动发", ctx=self.ctx())
        self.assertEqual(result["tid"], "new-tid")
        self.assertEqual(self.qz.records[-1]["actor_id"], "7")

        with self.assertRaises(RuntimeError):
            await self.qz.post("不是主人", ctx=self.ctx(uid=8))

    async def test_manual_private_read_still_requires_owner(self):
        read = await self.qz.list_own(2, ctx=self.ctx())
        self.assertTrue(read["ok"])
        denied = await self.qz.list_own(2, ctx=self.ctx(uid=8))
        self.assertFalse(denied["ok"])
        denied_group = await self.qz.list_own(2, ctx=self.ctx(group_id=9))
        self.assertFalse(denied_group["ok"])

    async def test_ledger_file_survives_restart(self):
        capability = self.qz.issue_autonomy_capability("life.autonomy")
        await self.qz.autonomy_post(capability, "重启前的说说")
        reopened = Qzone(self.bot)
        self.assertEqual(reopened.records[-1]["content"], "重启前的说说")
        self.assertEqual(reopened.today_count(), 1)
        payload = json.loads(Path(self.qz.path).read_text(encoding="utf-8"))
        self.assertEqual(payload[-1]["action"], "post")


if __name__ == "__main__":
    unittest.main()
