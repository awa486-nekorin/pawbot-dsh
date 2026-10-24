"""约定提醒：时间解析、待办存储、到点发送、工具文案，全部离线。"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import message as msg           # noqa: E402
from pawbot import turns                   # noqa: E402
from pawbot.reminders import (ReminderService, ReminderStore,  # noqa: E402
                              parse_when)
from pawbot.store import Store             # noqa: E402
from pawbot.tools import ToolBox           # noqa: E402


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


NOW = datetime(2026, 10, 21, 10, 0)        # 星期三


def at(text, now=NOW):
    ts, desc = parse_when(text, now)
    return (datetime.fromtimestamp(ts) if ts else None), desc


# ====================================================================== 时间解析
class ParseWhenTests(unittest.TestCase):
    CASES = [
        ("明天下午三點", datetime(2026, 10, 22, 15, 0)),
        ("明天下午三点", datetime(2026, 10, 22, 15, 0)),
        ("週五早上八點", datetime(2026, 10, 23, 8, 0)),
        ("周五早上8点", datetime(2026, 10, 23, 8, 0)),
        ("3天後", datetime(2026, 10, 24, 10, 0)),
        ("三天后", datetime(2026, 10, 24, 10, 0)),
        ("兩小時後", datetime(2026, 10, 21, 12, 0)),
        ("半小时后", datetime(2026, 10, 21, 10, 30)),
        ("一週後", datetime(2026, 10, 28, 10, 0)),
        ("晚上八點半", datetime(2026, 10, 21, 20, 30)),
        ("10月25號", datetime(2026, 10, 25, 9, 0)),
        ("10/25", datetime(2026, 10, 25, 9, 0)),
        ("下週一", datetime(2026, 10, 26, 9, 0)),
        ("星期一", datetime(2026, 10, 26, 9, 0)),
        ("下下週三", datetime(2026, 11, 4, 9, 0)),
        ("凌晨一點", datetime(2026, 10, 22, 1, 0)),
        ("早上八點", datetime(2026, 10, 22, 8, 0)),      # 今天已过 → 明天
        ("十三點", datetime(2026, 10, 21, 13, 0)),
        ("晚上", datetime(2026, 10, 21, 20, 0)),
        ("中午", datetime(2026, 10, 21, 12, 0)),
        ("下午", datetime(2026, 10, 21, 15, 0)),
        ("三點半", datetime(2026, 10, 22, 3, 30)),
        ("9:30", datetime(2026, 10, 22, 9, 30)),
        ("明天", datetime(2026, 10, 22, 9, 0)),
        ("23点", datetime(2026, 10, 21, 23, 0)),
        ("三点十五分", datetime(2026, 10, 22, 3, 15)),
        ("10月25號 3:30", datetime(2026, 10, 25, 3, 30)),
        ("下午3:30", datetime(2026, 10, 21, 15, 30)),
        ("下午3点半", datetime(2026, 10, 21, 15, 30)),
        ("晚上八点一刻", datetime(2026, 10, 21, 20, 15)),
        ("下个礼拜三", datetime(2026, 10, 28, 9, 0)),
        ("今晚八点", datetime(2026, 10, 21, 20, 0)),
        ("明晚八点", datetime(2026, 10, 22, 20, 0)),
        ("明早七点", datetime(2026, 10, 22, 7, 0)),
    ]

    def test_common_phrases(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                value, _desc = at(text)
                self.assertEqual(value, expected)

    def test_month_day_crosses_year(self):
        value, _desc = at("10月25號", datetime(2026, 12, 26, 10, 0))
        self.assertEqual(value, datetime(2027, 10, 25, 9, 0))

    def test_time_only_rolls_to_tomorrow_when_passed(self):
        value, desc = at("下午三點", datetime(2026, 10, 21, 16, 0))
        self.assertEqual(value, datetime(2026, 10, 22, 15, 0))
        self.assertEqual(desc, "明天 15:00")

    def test_weekday_today_but_passed_moves_to_next_week(self):
        # 2026-10-23 是周五，10 点说「周五」→ 下周五
        value, _desc = at("週五", datetime(2026, 10, 23, 10, 0))
        self.assertEqual(value, datetime(2026, 10, 30, 9, 0))

    def test_description_is_human_readable(self):
        _value, desc = at("明天下午三點")
        self.assertEqual(desc, "明天 15:00")
        _value, desc = at("10月25號")
        self.assertEqual(desc, "10月25日 09:00")

    def test_invalid_time_like_values_are_rejected(self):
        value, why = at("25點")
        self.assertIsNone(value)
        self.assertIn("点", why)
        value, why = at("13月")
        self.assertIsNone(value)
        self.assertIn("月", why)
        value, _why = at("2月30號")
        self.assertIsNone(value)

    def test_no_time_word(self):
        value, why = at("提醒我帶學生證")
        self.assertIsNone(value)
        self.assertEqual(why, "没说什么时候")

    def test_one_dian_as_a_bit_is_not_a_time(self):
        value, _why = at("提醒我一點小事")
        self.assertIsNone(value)


# ====================================================================== 待办存储
class ReminderStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "reminders.json")

    def test_add_list_due(self):
        store = ReminderStore(self.path)
        late = store.add(session_id="group_1", group_id=11, user_id=22,
                         who="阿明", what="交作業", due_ts=200.0)
        early = store.add(session_id="private_22", group_id=None, user_id=22,
                          who="阿明", what="帶學生證", due_ts=100.0)
        self.assertNotEqual(late["id"], early["id"])
        self.assertEqual([x["id"] for x in store.list_pending()],
                         [early["id"], late["id"]])
        self.assertEqual([x["id"] for x in store.due(150.0)], [early["id"]])
        self.assertEqual(len(store.due(50.0)), 0)
        self.assertIsNone(early["group_id"])
        self.assertEqual(early["user_id"], 22)

    def test_persists_across_restart(self):
        store = ReminderStore(self.path)
        item = store.add(session_id="group_1", group_id=11, user_id=22,
                         who="阿明", what="帶學生證", due_ts=100.0)
        again = ReminderStore(self.path)
        pending = again.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], item["id"])
        self.assertEqual(pending[0]["what"], "帶學生證")
        self.assertEqual(pending[0]["group_id"], 11)

    def test_mark_notified_and_done(self):
        store = ReminderStore(self.path)
        a = store.add(session_id="s", user_id=1, what="甲", due_ts=10.0)
        b = store.add(session_id="s", user_id=1, what="乙", due_ts=10.0)
        self.assertTrue(store.mark_notified(a["id"], ts=5.0))
        self.assertTrue(store.mark_done(b["id"]))
        self.assertEqual(store.list_pending(), [])
        self.assertEqual(store.items[0]["notified_ts"], 5.0)
        self.assertTrue(store.items[1]["done"])

    def test_snooze_moves_due_time(self):
        store = ReminderStore(self.path)
        item = store.add(session_id="s", user_id=1, what="甲", due_ts=10.0)
        self.assertTrue(store.snooze(item["id"], 60.0, now=1000.0))
        self.assertEqual(store.items[0]["due_ts"], 1060.0)
        self.assertEqual(len(store.list_pending()), 1)

    def test_remove(self):
        store = ReminderStore(self.path)
        item = store.add(session_id="s", user_id=1, what="甲", due_ts=10.0)
        self.assertTrue(store.remove(item["id"]))
        self.assertFalse(store.remove(item["id"]))
        self.assertEqual(store.list_pending(), [])

    def test_corrupt_file_does_not_explode(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken")
        store = ReminderStore(self.path)
        self.assertEqual(store.list_pending(), [])
        store.add(session_id="s", user_id=1, what="甲", due_ts=10.0)
        self.assertEqual(len(ReminderStore(self.path).list_pending()), 1)


# ====================================================================== 发送
class FakeConn:
    """假协议端：记录 send_group_msg / send_private_msg 的参数。"""

    def __init__(self, fail_times=0):
        self.calls = []
        self.fail_times = fail_times

    async def call(self, action, timeout=20, **params):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("协议端断了")
        self.calls.append({"action": action, "params": params})
        return {"message_id": len(self.calls)}

    async def send_message(self, message, *, group_id=None, user_id=None,
                           auto_escape=False):
        if group_id:
            return await self.call("send_group_msg", group_id=group_id,
                                   message=message)
        return await self.call("send_private_msg", user_id=user_id,
                               message=message)


class FakeSafety:
    async def send(self, conn, session_key, segments, *, group_id=None,
                   user_id=None, elapsed=0.0):
        return await conn.send_message(segments, group_id=group_id,
                                       user_id=user_id)


class FakeHistory:
    def __init__(self):
        self.rows = []

    def append(self, session_id, role, content, who="", **kw):
        self.rows.append({"session_id": session_id, "role": role,
                          "content": content, "who": who})


def make_bot(tmp, *, cfg=None, conn=None):
    bot = SimpleNamespace(
        config=Cfg(cfg or {}),
        reminders=ReminderStore(os.path.join(tmp, "reminders.json")),
        safety=FakeSafety(),
        store=FakeHistory(),
        stats={"sent": 0},
        last_msg_id={},
        last_msg_text={},
    )
    bot.conn_for = lambda self_id=None: conn
    return bot


class ReminderServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.now = time.time()

    async def test_group_reminder_ats_the_right_qq(self):
        conn = FakeConn()
        bot = make_bot(self.tmp, cfg={"reminders.quiet_hours": []}, conn=conn)
        bot.reminders.add(session_id="group_123", group_id=123, user_id=456,
                          who="阿明", what="帶學生證", due_ts=self.now - 5)
        service = ReminderService(bot)

        sent = await service.tick(self.now)

        self.assertEqual(sent, 1)
        self.assertEqual(len(conn.calls), 1)
        call = conn.calls[0]
        self.assertEqual(call["action"], "send_group_msg")
        self.assertEqual(call["params"]["group_id"], 123)
        segments = call["params"]["message"]
        self.assertEqual(segments[0], {"type": "at", "data": {"qq": "456"}})
        self.assertIn("你让我提醒你：帶學生證", msg.to_plain(segments))
        # 发完就算通知过，待办清空；会话里留一条「我提醒了他」
        self.assertEqual(bot.reminders.list_pending(), [])
        self.assertTrue(bot.reminders.items[0]["notified_ts"])
        self.assertEqual(len(bot.store.rows), 1)
        self.assertEqual(bot.store.rows[0]["session_id"], "group_123")
        self.assertIn("（提醒他：帶學生證）", bot.store.rows[0]["content"])

    async def test_private_reminder_has_no_at(self):
        conn = FakeConn()
        bot = make_bot(self.tmp, cfg={"reminders.quiet_hours": []}, conn=conn)
        bot.reminders.add(session_id="private_456", group_id=None, user_id=456,
                          who="阿明", what="交作業", due_ts=self.now - 5)
        service = ReminderService(bot)

        await service.tick(self.now)

        call = conn.calls[0]
        self.assertEqual(call["action"], "send_private_msg")
        self.assertEqual(call["params"]["user_id"], 456)
        self.assertFalse(any(s.get("type") == "at"
                             for s in call["params"]["message"]))
        self.assertIn("你让我提醒你：交作業", msg.to_plain(call["params"]["message"]))
        self.assertEqual(bot.store.rows[0]["session_id"], "private_456")

    async def test_not_due_yet_is_skipped(self):
        conn = FakeConn()
        bot = make_bot(self.tmp, cfg={"reminders.quiet_hours": []}, conn=conn)
        bot.reminders.add(session_id="group_1", group_id=1, user_id=2,
                          who="阿明", what="晚点", due_ts=self.now + 300)
        service = ReminderService(bot)
        self.assertEqual(await service.tick(self.now), 0)
        self.assertEqual(conn.calls, [])

    async def test_quiet_hours_postpone_to_nine(self):
        conn = FakeConn()
        bot = make_bot(self.tmp, cfg={"reminders.quiet_hours": [0, 9]}, conn=conn)
        quiet_now = datetime(2026, 10, 21, 2, 0).timestamp()
        bot.reminders.add(session_id="group_1", group_id=1, user_id=2,
                          who="阿明", what="帶學生證", due_ts=quiet_now - 1)
        service = ReminderService(bot)

        self.assertEqual(await service.tick(quiet_now), 0)
        self.assertEqual(conn.calls, [])
        target = datetime(2026, 10, 21, 9, 0).timestamp()
        self.assertAlmostEqual(bot.reminders.items[0]["due_ts"], target, places=3)
        self.assertFalse(bot.reminders.items[0]["done"])

        # 9 点一到就发
        self.assertEqual(await service.tick(target), 1)
        self.assertEqual(conn.calls[0]["action"], "send_group_msg")

    async def test_failure_retries_then_gives_up(self):
        conn = FakeConn(fail_times=99)
        bot = make_bot(self.tmp, cfg={"reminders.quiet_hours": []}, conn=conn)
        bot.reminders.add(session_id="group_1", group_id=1, user_id=2,
                          who="阿明", what="帶學生證", due_ts=self.now - 1)
        service = ReminderService(bot)

        self.assertEqual(await service.tick(self.now), 0)
        self.assertEqual(conn.calls, [])
        self.assertFalse(bot.reminders.items[0]["done"])
        self.assertGreaterEqual(bot.reminders.items[0]["due_ts"], self.now + 59)

        self.assertEqual(await service.tick(self.now + 61), 0)
        self.assertFalse(bot.reminders.items[0]["done"])

        self.assertEqual(await service.tick(self.now + 122), 0)
        self.assertTrue(bot.reminders.items[0]["done"])
        self.assertEqual(bot.reminders.list_pending(), [])

    async def test_one_failure_does_not_block_the_next(self):
        conn = FakeConn(fail_times=1)
        bot = make_bot(self.tmp, cfg={"reminders.quiet_hours": []}, conn=conn)
        bot.reminders.add(session_id="group_1", group_id=1, user_id=2,
                          who="甲", what="第一件", due_ts=self.now - 20)
        bot.reminders.add(session_id="group_1", group_id=1, user_id=3,
                          who="乙", what="第二件", due_ts=self.now - 10)
        service = ReminderService(bot)

        self.assertEqual(await service.tick(self.now), 1)
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(conn.calls[0]["params"]["message"][0]["data"]["qq"], "3")
        self.assertEqual(len(bot.reminders.list_pending()), 1)

    async def test_start_and_stop(self):
        bot = make_bot(self.tmp, cfg={"reminders.quiet_hours": []}, conn=FakeConn())
        service = ReminderService(bot)
        await service.start()
        await asyncio.sleep(0.01)
        self.assertTrue(service.task is not None)
        await service.stop()
        self.assertIsNone(service.task)
        # 没启动过就 stop 也不炸
        await service.stop()


# ====================================================================== 工具
class FakeMemory:
    def __init__(self):
        self.rows = []

    def remember(self, text, kind="fact"):
        self.rows.append((text, kind))
        return True


def make_toolbox(tmp):
    bot = SimpleNamespace(
        config=Cfg({}),
        reminders=ReminderStore(os.path.join(tmp, "reminders.json")),
        memory=FakeMemory(),
        qzone=SimpleNamespace(enabled=False),
    )
    return ToolBox(bot), bot


def fake_ctx(group=True):
    return SimpleNamespace(
        event=SimpleNamespace(user_id=456),
        session_id="group_123" if group else "private_456",
        sender_name="阿明",
        is_group=group,
        group_id=123 if group else None,
    )


class RemindToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.toolbox, self.bot = make_toolbox(self.tmp)

    def test_tool_registered(self):
        self.assertIn("remind", self.toolbox.available())

    async def test_success_reply_and_store(self):
        text, media = await self.toolbox.execute(
            fake_ctx(), "remind", {"when": "明天下午三點", "what": "帶學生證"})
        self.assertEqual(media, [])
        self.assertEqual(text, "好，记下了：明天 15:00 提醒你 帶學生證")
        pending = self.bot.reminders.list_pending()
        self.assertEqual(len(pending), 1)
        item = pending[0]
        self.assertEqual(item["session_id"], "group_123")
        self.assertEqual(item["group_id"], 123)
        self.assertEqual(item["user_id"], 456)
        self.assertEqual(item["who"], "阿明")
        self.assertEqual(item["what"], "帶學生證")
        self.assertGreater(item["due_ts"], time.time())
        # 约定同时写进长期记忆
        self.assertEqual(len(self.bot.memory.rows), 1)
        line, kind = self.bot.memory.rows[0]
        self.assertEqual(kind, "fact")
        self.assertIn("阿明 约好", line)
        self.assertIn("提醒：帶學生證", line)

    async def test_private_reminder_has_no_group(self):
        await self.toolbox.execute(
            fake_ctx(group=False), "remind", {"when": "晚上八點", "what": "交作業"})
        item = self.bot.reminders.list_pending()[0]
        self.assertIsNone(item["group_id"])
        self.assertEqual(item["session_id"], "private_456")

    async def test_unparsable_time_asks_again(self):
        text, _media = await self.toolbox.execute(
            fake_ctx(), "remind", {"when": "有空的时候", "what": "帶學生證"})
        self.assertEqual(text, "没听懂是什么时候，换个说法（比如「明天下午三点」）")
        self.assertEqual(self.bot.reminders.list_pending(), [])
        self.assertEqual(self.bot.memory.rows, [])


# ====================================================================== 约定进记忆
class PromiseMemoryTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "s.json"), max_messages=300)
        memory = FakeMemory()
        bot = SimpleNamespace(
            config=Cfg({"chat.turn_summary": True}),
            store=store,
            llm=SimpleNamespace(chat=AsyncMock(return_value="主模型小结")),
            memory=memory,
        )
        bot.store.append("group_1", "user", "周五记得提醒我交作业", who="阿明")
        return bot, memory

    async def test_promises_go_into_long_term_memory(self):
        bot, memory = self.make_bot()
        verdict = {"summary": "阿明让周五提醒他交作业。",
                   "promises": ["周五提醒阿明交作业"]}
        with patch.object(turns.judge, "judge_json", AsyncMock(return_value=verdict)):
            out = await turns.update(bot, "group_1")
        self.assertTrue(out)
        self.assertEqual(memory.rows, [("约定：周五提醒阿明交作业", "fact")])

    async def test_old_judge_output_without_promises(self):
        bot, memory = self.make_bot()
        with patch.object(turns.judge, "judge_json",
                          AsyncMock(return_value={"summary": "阿明提了个约定。"})):
            out = await turns.update(bot, "group_1")
        self.assertTrue(out)
        self.assertEqual(memory.rows, [])

    async def test_prompt_asks_for_promises(self):
        bot, _memory = self.make_bot()
        mock = AsyncMock(return_value={"summary": "打了个招呼。"})
        with patch.object(turns.judge, "judge_json", mock):
            await turns.update(bot, "group_1")
        prompt = mock.await_args.args[1]
        self.assertIn("promises", prompt)


if __name__ == "__main__":
    unittest.main()


class DeliveryPathTests(unittest.IsolatedAsyncioTestCase):
    """提醒不经过拟人化层：不该有打字延迟，也不该被「手滑」改字。"""

    async def test_deliver_bypasses_safety_layer(self):
        import time as _t
        from pawbot import reminders as rm

        class Conn:
            def __init__(self):
                self.calls = []

            async def send_message(self, segments, **target):
                self.calls.append((segments, target))
                return {"message_id": 1}

        class Bot:
            def __init__(self):
                self.config = SimpleNamespace(get=lambda k, d=None: d)
                self.sent_via_ctx = 0

            def conn_for(self, self_id=None):
                return conn

        conn = Conn()
        bot = Bot()
        svc = rm.ReminderService(bot)
        await svc._deliver({"what": "帶學生證", "user_id": 10001,
                            "group_id": 42, "session_id": "group_42"})
        self.assertEqual(len(conn.calls), 1)
        segments, target = conn.calls[0]
        self.assertEqual(target, {"group_id": 42})
        self.assertEqual(segments[0]["type"], "at")
        self.assertIn("帶學生證", segments[1]["data"]["text"])

    async def test_tick_caps_the_batch(self):
        import time as _t
        from pawbot import reminders as rm

        class Store:
            def __init__(self, n):
                self.items = [{"id": f"r{i}", "what": f"事{i}", "due_ts": 0,
                               "user_id": 1, "group_id": 2, "session_id": "group_2",
                               "done": False, "notified_ts": 0} for i in range(n)]
                self.notified = []

            def due(self, now):
                return [x for x in self.items if not x["done"]]

            def mark_notified(self, item_id, ts=None):
                self.notified.append(item_id)
                for x in self.items:
                    if x["id"] == item_id:
                        x["done"] = True
                return True

            def snooze(self, *a, **k):
                return True

        class Bot:
            def __init__(self, n):
                self.config = SimpleNamespace(
                    get=lambda k, d=None: [] if k == "reminders.quiet_hours" else d)
                self.reminders = Store(n)
                self.store = None
                self.conn = None

            def conn_for(self, self_id=None):
                return None

        bot = Bot(10)
        svc = rm.ReminderService(bot)
        svc._deliver = AsyncMock()          # 不真的发
        sent = await svc.tick(now=1e10)
        self.assertEqual(sent, 3)           # 默认每轮最多 3 条
