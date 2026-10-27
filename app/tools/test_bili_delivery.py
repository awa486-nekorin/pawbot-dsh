"""Offline Bilibili tracking tests at the final ai_chat QQ delivery boundary."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import bilibili, bili_watch, message as msg
from pawbot.tools import ToolBox, _bili_delivery_segments
from plugins import ai_chat


class DeliveryError(RuntimeError):
    def __init__(self, delivered):
        super().__init__("receipt unavailable")
        self.delivered = delivered


class BiliDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = tmp.name
        bilibili._shared_at.clear()
        self.addCleanup(bilibili._shared_at.clear)
        self.cfg = {
            "tools.bili_watch_dir": self.directory,
            "tools.bili_watch_enabled": False,
            "tools.bili_min_gap": 0,
            "tools.bili_json_card": False,
            "chat.no_silence": False,
            "reply.length_enforce": False,
        }
        self.bot = SimpleNamespace(
            config=self.cfg, agent=SimpleNamespace(run=AsyncMock()),
            store=SimpleNamespace(history=Mock(return_value=[]), append=Mock()),
            topic=SimpleNamespace(seen=Mock()), inbound={},
            _pass_rate=Mock(return_value=True), emotion=None)
        self.ctx = SimpleNamespace(
            config=self.cfg, bot=self.bot, state={}, session_id="group_1",
            event=SimpleNamespace(is_group=True, to_me=False),
            reply=AsyncMock(return_value={}), send=AsyncMock(return_value={}))
        for target in ("_roll_reply_style",):
            p = patch.object(ai_chat, target)
            p.start()
            self.addCleanup(p.stop)
        for target in ("should_force_voice", "should_auto_voice"):
            p = patch.object(ai_chat.tts, target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        # Close the summary coroutine rather than scheduling background work.
        p = patch.object(ai_chat.tasks, "spawn", side_effect=lambda coro, **kw: coro.close())
        p.start()
        self.addCleanup(p.stop)

    def video(self, bvid="BVselected"):
        return {"bvid": bvid, "title": "测试视频",
                "url": f"https://www.bilibili.com/video/{bvid}",
                "author": "UP", "play": 100, "tname": "科技", "pic": ""}

    async def select(self, bvid="BVselected", report=False):
        video = self.video(bvid)
        self.cfg["tools.bili_watch_enabled"] = report
        with patch.object(bilibili, "pick_lively", return_value=[video]), \
                patch.object(bili_watch, "pick_report", return_value=video), \
                patch.object(bili_watch, "format_for_model", return_value="看过的报告"):
            _, segments = await ToolBox(self.bot)._t_bili_video(self.ctx)
        self.assert_unshared()
        return segments

    def assert_unshared(self):
        self.assertFalse(os.path.exists(os.path.join(self.directory, "shared.json")))
        self.assertTrue(bilibili.share_allowed("group_1", 3600))

    def shared(self):
        path = os.path.join(self.directory, "shared.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    async def reply(self, media, answer="看看这个视频。"):
        self.bot.agent.run.return_value = (answer, media)
        await ai_chat._reply_turn(self.ctx, "找个视频")

    async def test_selection_only_never_marks_report_or_title(self):
        await self.select(report=True)
        await self.select(report=False)
        self.ctx.reply.assert_not_awaited()
        self.assert_unshared()

    async def test_successful_reply_commits_and_sends_actual_clean_segments(self):
        segments = await self.select(report=True)
        async def delivered(payload, **kwargs):
            self.assert_unshared()
            self.assertIn(msg.Text("测试视频\nhttps://www.bilibili.com/video/BVselected"), payload)
            self.assertTrue(all("_bili_share" not in seg for seg in payload))
            return {}  # Empty receipt is success, unlike None.
        self.ctx.reply.side_effect = delivered
        await self.reply(segments)
        self.assertEqual(self.shared()["BVselected"]["session"], "group_1")
        self.assertFalse(bilibili.share_allowed("group_1", 3600))

    async def test_none_receipt_does_not_commit(self):
        segments = await self.select()
        self.ctx.reply.return_value = None
        await self.reply(segments)
        self.assert_unshared()

    async def test_exception_delivered_true_commits(self):
        segments = await self.select()
        self.ctx.reply.side_effect = DeliveryError(True)
        await self.reply(segments)
        self.assertEqual(set(self.shared()), {"BVselected"})
        self.assertFalse(bilibili.share_allowed("group_1", 3600))

    async def test_exception_delivered_false_does_not_commit(self):
        segments = await self.select()
        self.ctx.reply.side_effect = DeliveryError(False)
        await self.reply(segments)
        self.assert_unshared()

    async def test_plain_send_exception_does_not_commit(self):
        segments = await self.select()
        self.ctx.reply.side_effect = RuntimeError("offline")
        await self.reply(segments)
        self.assert_unshared()

    async def test_no_actual_segment_never_commits_selected_video(self):
        await self.select()
        await self.reply([], answer="只说一下这个标题。")
        self.ctx.reply.assert_awaited_once()
        self.assert_unshared()

    async def test_only_final_subset_is_shared(self):
        omitted = await self.select("BVomitted")
        selected = await self.select("BVincluded")
        await self.reply(copy.deepcopy(selected))
        self.assertEqual(set(self.shared()), {"BVincluded"})
        payload = self.ctx.reply.await_args.args[0]
        self.assertNotIn(omitted[0]["data"], [seg["data"] for seg in payload])

    async def test_multiple_delivered_videos_each_commit(self):
        first = await self.select("BVfirst")
        second = await self.select("BVsecond")
        await self.reply(first + second)
        self.assertEqual(set(self.shared()), {"BVfirst", "BVsecond"})

    async def test_reply_target_session_receives_shared_state(self):
        segments = await self.select()
        target = SimpleNamespace(session_id="group_target",
                                 reply=AsyncMock(return_value={}))
        self.ctx.state["reply_target"] = target
        await self.reply(segments)
        self.ctx.reply.assert_not_awaited()
        target.reply.assert_awaited_once()
        self.assertEqual(self.shared()["BVselected"]["session"], "group_target")
        self.assertTrue(bilibili.share_allowed("group_1", 3600))
        self.assertFalse(bilibili.share_allowed("group_target", 3600))

    async def test_single_dict_media_is_normalized_not_iterated_as_keys(self):
        segments = await self.select()
        await self.reply(segments[0])
        self.assertEqual(set(self.shared()), {"BVselected"})
        self.assertTrue(all(isinstance(seg, dict) for seg in self.ctx.reply.await_args.args[0]))

    async def test_replaced_segment_with_stale_metadata_does_not_commit(self):
        segments = await self.select()
        segments[0]["data"]["text"] = "这个视频就不发了。"
        await self.reply(segments)
        self.assert_unshared()

    async def test_decline_and_empty_output_do_not_send_bili_or_commit(self):
        for answer in ("<pass>", "", "   "):
            with self.subTest(answer=answer):
                segments = await self.select()
                await self.reply(segments, answer)
                self.assert_unshared()
                self.ctx.reply.assert_not_awaited()

    async def test_cleanup_empty_output_does_not_share(self):
        segments = await self.select()
        with patch.object(ai_chat, "polish", return_value=""):
            await self.reply(segments)
        self.ctx.reply.assert_not_awaited()
        self.assert_unshared()

    async def test_addressed_decline_fallback_does_not_share(self):
        segments = await self.select()
        self.cfg["chat.no_silence"] = True
        self.ctx.event.to_me = True
        await self.reply(segments, "<pass>")
        self.ctx.reply.assert_awaited_once()
        self.assert_unshared()
        self.assertNotIn("bilibili", str(self.ctx.reply.await_args.args[0]))

    async def test_decline_preserves_unrelated_media_without_sharing(self):
        segments = await self.select()
        await self.reply(segments + [msg.Record("file://voice.wav")], "<pass>")
        self.assertEqual(self.ctx.reply.await_args.args[0], [msg.Record("file://voice.wav")])
        self.assert_unshared()

    async def test_rate_suppression_does_not_commit(self):
        segments = await self.select()
        self.bot._pass_rate.return_value = False
        await self.reply(segments)
        self.ctx.reply.assert_not_awaited()
        self.assert_unshared()

    async def test_card_and_text_commit_same_video_once(self):
        self.cfg["tools.bili_json_card"] = True
        segments = await self.select()
        self.assertEqual([s["type"] for s in segments], ["json", "text"])
        with patch.object(bili_watch, "mark_shared", wraps=bili_watch.mark_shared) as mark:
            await self.reply(segments)
        mark.assert_called_once_with("BVselected", "group_1", dir=self.directory)

    async def test_no_report_segments_means_no_commit(self):
        self.cfg["tools.bili_watch_enabled"] = True
        with patch.object(bili_watch, "pick_report", return_value={"bvid": "BVmissing"}), \
                patch.object(bili_watch, "format_for_model", return_value="报告"):
            _, segments = await ToolBox(self.bot)._t_bili_video(self.ctx)
        self.assertEqual(segments, [])
        await self.reply(segments)
        self.assert_unshared()

    async def test_voice_send_uses_same_delivery_outcome_rules(self):
        for outcome, expected in (({}, True), (None, False),
                                  (DeliveryError(True), True), (DeliveryError(False), False)):
            with self.subTest(outcome=outcome):
                segments = _bili_delivery_segments(
                    [msg.Text("测试视频\nhttps://www.bilibili.com/video/BVvoice")],
                    "BVvoice", self.directory)
                self.ctx.state["force_voice"] = True
                self.ctx.send.reset_mock()
                self.ctx.send.side_effect = outcome if isinstance(outcome, Exception) else None
                self.ctx.send.return_value = outcome
                with patch.object(ai_chat.tts, "record_segments",
                                  new=AsyncMock(return_value=[msg.Record("voice")])), \
                        patch.object(ai_chat.tts, "split_for_voice", return_value=("朗读内容", "")), \
                        patch.object(bili_watch, "mark_shared") as mark, \
                        patch.object(bilibili, "mark_shared") as cooldown:
                    await self.reply(segments)
                self.ctx.send.assert_awaited_once()
                self.ctx.reply.assert_not_awaited()
                self.assertEqual(mark.called, expected)
                self.assertEqual(cooldown.called, expected)


if __name__ == "__main__":
    unittest.main()
