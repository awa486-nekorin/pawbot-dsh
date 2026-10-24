"""离线测试：群事件反应、合并转发/卡片解析、被谈论检测。不连网。"""

import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import event, perceive
from pawbot.agent import Agent
from plugins import group_events

SELF_ID = 10001
GROUP_ID = 77
SLEEP = "plugins.group_events.asyncio.sleep"


def notice(kind, **extra):
    raw = {"post_type": "notice", "notice_type": kind, "self_id": SELF_ID,
           "group_id": GROUP_ID, "user_id": 20002,
           "sender": {"nickname": "阿明"}}
    raw.update(extra)
    return event.parse(raw)


def message(segments, **extra):
    raw = {"post_type": "message", "message_type": "group", "self_id": SELF_ID,
           "group_id": GROUP_ID, "user_id": 20002, "message_id": 55,
           "message": segments, "sender": {"nickname": "阿明"}}
    raw.update(extra)
    return event.parse(raw)


class FakeBot:
    def __init__(self, config=None):
        self.config = config if config is not None else {}
        self.store = SimpleNamespace(append=Mock())
        self.call = AsyncMock(return_value={})
        self.conn = SimpleNamespace(call=self.call)

    def conn_for(self, _self_id=None):
        return self.conn

    def rows(self):
        return [c.args for c in self.store.append.call_args_list]


def ctx_for(ev, bot=None):
    bot = bot or FakeBot()
    return SimpleNamespace(event=ev, self_id=SELF_ID, config=bot.config,
                           bot=bot, send=AsyncMock(return_value={"message_id": 1}))


async def fire(handler, ctx, chance=0.0):
    """跑一次处理器，随机数固定、睡觉跳过。"""
    with patch("random.random", return_value=chance), \
            patch(SLEEP, new=AsyncMock()):
        await handler(ctx)


class RecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        group_events._LAST.clear()

    async def test_user_recall_replies_and_records_both_lines(self):
        ctx = ctx_for(notice("group_recall", operator_id=20002))
        await fire(group_events.on_group_recall, ctx)
        ctx.send.assert_awaited_once()
        args, kwargs = ctx.send.await_args
        self.assertIn(args[0], group_events.RECALL_LINES)
        self.assertEqual(kwargs, {"group_id": GROUP_ID})
        rows = ctx.bot.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0][1], rows[0][2]), ("user", "（系统：阿明 撤回了一条消息）"))
        self.assertEqual((rows[1][1], rows[1][2]), ("assistant", args[0]))

    async def test_self_recall_is_silent_but_still_recorded(self):
        ctx = ctx_for(notice("group_recall", user_id=SELF_ID, operator_id=SELF_ID))
        await fire(group_events.on_group_recall, ctx)
        ctx.send.assert_not_awaited()
        self.assertEqual(len(ctx.bot.rows()), 1)

    async def test_admin_recalling_someone_else_is_silent(self):
        ctx = ctx_for(notice("group_recall", user_id=20002, operator_id=90000))
        await fire(group_events.on_group_recall, ctx)
        ctx.send.assert_not_awaited()
        self.assertEqual(len(ctx.bot.rows()), 1)

    async def test_chance_miss_is_silent(self):
        ctx = ctx_for(notice("group_recall", operator_id=20002))
        await fire(group_events.on_group_recall, ctx, chance=1.0)
        ctx.send.assert_not_awaited()
        self.assertEqual(len(ctx.bot.rows()), 1)

    async def test_cooldown_blocks_second_joke_in_same_group(self):
        ctx1 = ctx_for(notice("group_recall", operator_id=20002))
        await fire(group_events.on_group_recall, ctx1)
        ctx1.send.assert_awaited_once()
        ctx2 = ctx_for(notice("group_recall", user_id=30003, operator_id=30003))
        await fire(group_events.on_group_recall, ctx2)
        ctx2.send.assert_not_awaited()


class BanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        group_events._LAST.clear()

    async def test_other_banned_may_say_something(self):
        ctx = ctx_for(notice("group_ban", sub_type="ban", user_id=20002,
                             operator_id=90000, duration=600))
        await fire(group_events.on_group_ban, ctx)
        ctx.send.assert_awaited_once()
        args, kwargs = ctx.send.await_args
        self.assertIn(args[0], [x.format(name="阿明") for x in group_events.BAN_OTHER_LINES])
        self.assertEqual(kwargs, {"group_id": GROUP_ID})
        self.assertEqual(len(ctx.bot.rows()), 2)
        self.assertIn("禁言", ctx.bot.rows()[0][2])

    async def test_ban_joke_has_cooldown(self):
        ctx1 = ctx_for(notice("group_ban", sub_type="ban", user_id=20002,
                              operator_id=90000, duration=600))
        await fire(group_events.on_group_ban, ctx1)
        ctx2 = ctx_for(notice("group_ban", sub_type="ban", user_id=30003,
                              operator_id=90000, duration=600))
        await fire(group_events.on_group_ban, ctx2)
        ctx1.send.assert_awaited_once()
        ctx2.send.assert_not_awaited()

    async def test_all_mute_is_silent(self):
        ctx = ctx_for(notice("group_ban", sub_type="ban", user_id=0,
                             operator_id=90000, duration=600))
        await fire(group_events.on_group_ban, ctx)
        ctx.send.assert_not_awaited()
        self.assertEqual(len(ctx.bot.rows()), 1)
        self.assertIn("全员禁言", ctx.bot.rows()[0][2])

    async def test_self_ban_records_deadline_without_talking(self):
        ctx = ctx_for(notice("group_ban", sub_type="ban", user_id=SELF_ID,
                             operator_id=90000, duration=600))
        before = time.time()
        await fire(group_events.on_group_ban, ctx)
        ctx.send.assert_not_awaited()
        until = ctx.bot.life_state["self_banned_until"]
        self.assertGreaterEqual(until, before + 599)
        self.assertLessEqual(until, before + 601)
        self.assertEqual(len(ctx.bot.rows()), 1)

    async def test_self_lift_always_complains_and_clears_state(self):
        bot = FakeBot()
        bot.life_state = {"self_banned_until": time.time() + 600}
        ctx = ctx_for(notice("group_ban", sub_type="lift_ban", user_id=SELF_ID,
                             operator_id=90000, duration=0), bot=bot)
        await fire(group_events.on_group_ban, ctx, chance=1.0)
        ctx.send.assert_awaited_once()
        self.assertIn(ctx.send.await_args.args[0], group_events.BAN_LIFT_LINES)
        self.assertEqual(bot.life_state["self_banned_until"], 0)

    async def test_other_lift_is_silent(self):
        ctx = ctx_for(notice("group_ban", sub_type="lift_ban", user_id=20002,
                             operator_id=90000, duration=0))
        await fire(group_events.on_group_ban, ctx)
        ctx.send.assert_not_awaited()
        self.assertIn("解除", ctx.bot.rows()[0][2])


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_promoted_may_be_congratulated(self):
        ctx = ctx_for(notice("group_admin", sub_type="set", user_id=20002))
        await fire(group_events.on_group_admin, ctx)
        ctx.send.assert_awaited_once()
        self.assertIn(ctx.send.await_args.args[0],
                      [x.format(name="阿明") for x in group_events.ADMIN_SET_LINES])
        self.assertEqual(len(ctx.bot.rows()), 2)

    async def test_other_promotion_can_miss(self):
        ctx = ctx_for(notice("group_admin", sub_type="set", user_id=20002))
        await fire(group_events.on_group_admin, ctx, chance=1.0)
        ctx.send.assert_not_awaited()

    async def test_other_demoted_is_silent(self):
        ctx = ctx_for(notice("group_admin", sub_type="unset", user_id=20002))
        await fire(group_events.on_group_admin, ctx)
        ctx.send.assert_not_awaited()
        self.assertEqual(len(ctx.bot.rows()), 1)

    async def test_self_promoted_always_speaks(self):
        ctx = ctx_for(notice("group_admin", sub_type="set", user_id=SELF_ID))
        await fire(group_events.on_group_admin, ctx, chance=1.0)
        ctx.send.assert_awaited_once()
        self.assertIn(ctx.send.await_args.args[0], group_events.SELF_ADMIN_LINES)

    async def test_self_demoted_always_speaks(self):
        ctx = ctx_for(notice("group_admin", sub_type="unset", user_id=SELF_ID))
        await fire(group_events.on_group_admin, ctx, chance=1.0)
        ctx.send.assert_awaited_once()
        self.assertIn(ctx.send.await_args.args[0], group_events.SELF_ADMIN_OFF_LINES)


class DecreaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_leave_may_be_mourned(self):
        ctx = ctx_for(notice("group_decrease", sub_type="leave", user_id=20002))
        await fire(group_events.on_group_decrease, ctx)
        ctx.send.assert_awaited_once()
        self.assertIn(ctx.send.await_args.args[0], group_events.DECREASE_LINES)

    async def test_leave_can_miss(self):
        ctx = ctx_for(notice("group_decrease", sub_type="leave", user_id=20002))
        await fire(group_events.on_group_decrease, ctx, chance=1.0)
        ctx.send.assert_not_awaited()

    async def test_kick_is_silent_but_recorded(self):
        ctx = ctx_for(notice("group_decrease", sub_type="kick", user_id=20002))
        await fire(group_events.on_group_decrease, ctx)
        ctx.send.assert_not_awaited()
        self.assertIn("移出", ctx.bot.rows()[0][2])


class FriendEssenceUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_friend_add_sends_private_greeting(self):
        ev = notice("friend_add", group_id=None, user_id=20002)
        ctx = ctx_for(ev)
        await fire(group_events.on_friend_add, ctx)
        ctx.send.assert_awaited_once()
        args, kwargs = ctx.send.await_args
        self.assertIn(args[0], group_events.FRIEND_LINES)
        self.assertEqual(kwargs, {})
        self.assertEqual(ctx.bot.rows()[0][0], "private_20002")

    async def test_essence_add_may_comment(self):
        ctx = ctx_for(notice("essence", sub_type="add", user_id=20002,
                             operator_id=90000))
        await fire(group_events.on_essence, ctx)
        ctx.send.assert_awaited_once()
        self.assertIn(ctx.send.await_args.args[0], group_events.ESSENCE_LINES)
        self.assertIn("精华", ctx.bot.rows()[0][2])

    async def test_essence_removed_is_silent(self):
        ctx = ctx_for(notice("essence", sub_type="delete", user_id=20002))
        await fire(group_events.on_essence, ctx)
        ctx.send.assert_not_awaited()
        self.assertIn("移出", ctx.bot.rows()[0][2])

    async def test_group_upload_only_logs_and_records(self):
        ctx = ctx_for(notice("group_upload", user_id=20002,
                             file={"name": "报告.pdf"}))
        await fire(group_events.on_group_upload, ctx)
        ctx.send.assert_not_awaited()
        self.assertIn("报告.pdf", ctx.bot.rows()[0][2])


class RequestTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, rtype, **extra):
        raw = {"post_type": "request", "request_type": rtype, "self_id": SELF_ID,
               "user_id": 20002, "flag": "FLAG-1", "comment": "hi"}
        raw.update(extra)
        return event.parse(raw)

    async def test_friend_request_not_auto_accepted_by_default(self):
        ctx = ctx_for(self._request("friend"))
        with patch(SLEEP, new=AsyncMock()):
            await group_events.on_add_request(ctx)
        ctx.bot.call.assert_not_awaited()

    async def test_friend_request_auto_accepted_when_configured(self):
        bot = FakeBot({"events.auto_accept_friend": True})
        ctx = ctx_for(self._request("friend"), bot=bot)
        with patch(SLEEP, new=AsyncMock()):
            await group_events.on_add_request(ctx)
        ctx.bot.call.assert_awaited_once_with(
            "set_friend_add_request", flag="FLAG-1", approve=True, timeout=10)

    async def test_group_request_never_auto_accepted(self):
        bot = FakeBot({"events.auto_accept_friend": True})
        ctx = ctx_for(self._request("group", group_id=GROUP_ID), bot=bot)
        with patch(SLEEP, new=AsyncMock()):
            await group_events.on_add_request(ctx)
        ctx.bot.call.assert_not_awaited()


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    def _conn(self, payload):
        return SimpleNamespace(call=AsyncMock(return_value=payload))

    async def test_messages_key_is_expanded(self):
        conn = self._conn({"data": {"messages": [
            {"sender": {"nickname": "阿明"},
             "message": [{"type": "text", "data": {"text": "你好"}}]},
            {"sender": {"nickname": "小红"},
             "message": [{"type": "text", "data": {"text": "在吗"}}]},
        ]}})
        ev = message([{"type": "forward", "data": {"id": "f1"}}])
        await perceive.enrich(FakeBot(), conn, ev)
        self.assertIn("阿明：你好", ev.forward_text)
        self.assertIn("小红：在吗", ev.forward_text)
        conn.call.assert_awaited_once_with("get_forward_msg", id="f1", timeout=15)

    async def test_message_key_with_content_segments_is_expanded(self):
        conn = self._conn({"data": {"message": [
            {"sender": {"nickname": "阿明"},
             "content": [{"type": "text", "data": {"text": "第一句"}}]},
        ]}})
        ev = message([{"type": "forward", "data": {"id": "f2"}}])
        await perceive.enrich(FakeBot(), conn, ev)
        self.assertEqual(ev.forward_text, "阿明：第一句")

    async def test_nested_forward_is_not_expanded(self):
        conn = self._conn({"data": {"messages": [
            {"sender": {"nickname": "阿明"},
             "message": [{"type": "text", "data": {"text": "看这个"}},
                         {"type": "forward", "data": {"id": "inner"}}]},
        ]}})
        ev = message([{"type": "forward", "data": {"id": "f3"}}])
        await perceive.enrich(FakeBot(), conn, ev)
        self.assertIn("看这个", ev.forward_text)
        self.assertIn("（又套了一层转发）", ev.forward_text)

    async def test_forward_text_is_truncated(self):
        nodes = [{"sender": {"nickname": f"用户{i}"},
                  "message": [{"type": "text", "data": {"text": "x" * 80}}]}
                 for i in range(40)]
        conn = self._conn({"data": {"messages": nodes}})
        ev = message([{"type": "forward", "data": {"id": "f4"}}])
        await perceive.enrich(FakeBot(), conn, ev)
        self.assertLessEqual(len(ev.forward_text), 3000)
        self.assertLessEqual(ev.forward_text.count("\n") + 1, 30)

    async def test_forward_failure_is_silent(self):
        conn = SimpleNamespace(call=AsyncMock(side_effect=RuntimeError("boom")))
        ev = message([{"type": "forward", "data": {"id": "f5"}}])
        await perceive.enrich(FakeBot(), conn, ev)
        self.assertEqual(ev.forward_text, "")


class CardTests(unittest.IsolatedAsyncioTestCase):
    def _seg(self, payload):
        return {"type": "json", "data": {"data": json.dumps(payload, ensure_ascii=False)}}

    async def test_bilibili_detail_1_card(self):
        payload = {"app": "com.tencent.miniapp_01",
                   "meta": {"detail_1": {"title": "B站视频",
                                          "desc": "鬼畜区又出新活",
                                          "qqdocurl": "https://b23.tv/abc"}},
                   "prompt": "[QQ小程序]哔哩哔哩"}
        ev = message([self._seg(payload)])
        await perceive.enrich(FakeBot(), None, ev)
        self.assertIn("B站视频", ev.card_text)
        self.assertIn("鬼畜区又出新活", ev.card_text)
        self.assertIn("https://b23.tv/abc", ev.card_text)

    def test_news_card_uses_jump_url(self):
        payload = {"meta": {"news": {"title": "一条新闻",
                                     "desc": "摘要内容",
                                     "jumpUrl": "https://example.com/n"}}}
        text = perceive.card_text([self._seg(payload)])
        self.assertIn("一条新闻", text)
        self.assertIn("https://example.com/n", text)

    def test_prompt_is_used_when_meta_missing(self):
        payload = {"prompt": "[QQ小程序]某游戏活动"}
        text = perceive.card_text([self._seg(payload)])
        self.assertEqual(text, "[QQ小程序]某游戏活动")

    def test_broken_json_falls_back_to_segment_prompt(self):
        seg = {"type": "json", "data": {"data": "{not json", "prompt": "卡片兜底"}}
        self.assertEqual(perceive.card_text([seg]), "卡片兜底")


class QuoteAndTalkTests(unittest.IsolatedAsyncioTestCase):
    async def _quote(self, uid, text="我是大肥鱼"):
        conn = SimpleNamespace(call=AsyncMock(return_value={
            "data": {"message": [{"type": "text", "data": {"text": text}}],
                     "sender": {"nickname": "大肥鱼", "user_id": uid}},
        }))
        ev = message([{"type": "reply", "data": {"id": "99"}},
                      {"type": "text", "data": {"text": "你说啥"}}])
        await perceive.enrich(FakeBot(), conn, ev)
        return ev

    async def test_quoting_self_sets_flag(self):
        ev = await self._quote(SELF_ID)
        self.assertTrue(ev.quoted_is_self)
        self.assertEqual(ev.quoted_text, "我是大肥鱼")
        self.assertEqual(ev.quoted_from, "大肥鱼")

    async def test_quoting_other_does_not_set_flag(self):
        ev = await self._quote(20002)
        self.assertFalse(ev.quoted_is_self)

    def test_talks_about_bot_hits(self):
        names = ["大肥鱼", "肥鱼"]
        for text in ("这机器人又在偷懒", "这个AI怎么回事", "这bot是不是坏了",
                     "她是不是没睡醒", "她在吗", "肥鱼呢", "大肥鱼的尾巴"):
            self.assertTrue(perceive.talks_about_bot(text, names), text)

    def test_talks_about_bot_avoids_obvious_false_positives(self):
        names = ["大肥鱼", "肥鱼"]
        for text in ("我自己是个机器人", "她今天去哪了", "今天天气不错",
                     "我在写机器人作业", ""):
            self.assertFalse(perceive.talks_about_bot(text, names), text)

    async def test_enrich_marks_talked_about(self):
        bot = FakeBot({"bot.nickname": "大肥鱼", "bot.wake_words": ["肥鱼"]})
        ev = message([{"type": "text", "data": {"text": "这机器人又在偷懒"}}])
        await perceive.enrich(bot, None, ev)
        self.assertTrue(ev.talked_about)

    async def test_enrich_does_not_mark_plain_chat(self):
        bot = FakeBot({"bot.nickname": "大肥鱼", "bot.wake_words": ["肥鱼"]})
        ev = message([{"type": "text", "data": {"text": "今天中午吃什么"}}])
        await perceive.enrich(bot, None, ev)
        self.assertFalse(ev.talked_about)


class SenseHintTests(unittest.TestCase):
    def _hint(self, **attrs):
        values = {"to_me": False, "quoted_text": "", "quoted_from": "",
                  "image_desc": "", "person_card": "", "forward_text": "",
                  "card_text": "", "talked_about": False}
        values.update(attrs)
        ev = SimpleNamespace(**values)
        ctx = SimpleNamespace(sender_name="阿明", is_group=True,
                              session_id="group_77", state={}, event=ev)
        bot = SimpleNamespace(config={"topic.enabled": False})
        return Agent(bot)._sense_hint(ctx)

    def test_forward_and_card_and_talked_hints(self):
        hint = self._hint(forward_text="阿明：你好\n小红：在吗",
                          card_text="标题 ｜ 描述 ｜ https://x",
                          talked_about=True)
        self.assertIn("对方转发了一段聊天记录", hint)
        self.assertIn("阿明：你好", hint)
        self.assertIn("对方分享了一个卡片", hint)
        self.assertIn("好像在说你", hint)
        self.assertIn("<pass>", hint)

    def test_plain_message_has_no_sense_hint(self):
        self.assertEqual(self._hint(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
