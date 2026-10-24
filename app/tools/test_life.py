"""时间感 / 作息 / 节假日 / 群信息缓存 / 生日。全部离线，时间用参数注入。"""

import json
import os
import re
import sys
import tempfile
import unittest
import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import groupinfo, life
from pawbot.config import DEFAULT
from plugins import life_events


# ------------------------------------------------------------------ 时间感
class NowInfoTests(unittest.TestCase):
    def test_weekday_names(self):
        expected = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        for i, name in enumerate(expected):
            info = life.now_info(date(2026, 10, 19 + i))
            self.assertEqual(info["weekday"], name)
            self.assertEqual(info["is_weekend"], i >= 5)

    def test_period_boundaries(self):
        cases = [
            ((0, 0), "凌晨", "00:00", "05:00"),
            ((4, 59), "凌晨", "00:00", "05:00"),
            ((5, 0), "早上", "05:00", "08:00"),
            ((7, 59), "早上", "05:00", "08:00"),
            ((8, 0), "上午", "08:00", "11:00"),
            ((10, 59), "上午", "08:00", "11:00"),
            ((11, 0), "中午", "11:00", "13:00"),
            ((12, 59), "中午", "11:00", "13:00"),
            ((13, 0), "下午", "13:00", "17:00"),
            ((16, 59), "下午", "13:00", "17:00"),
            ((17, 0), "傍晚", "17:00", "19:00"),
            ((18, 59), "傍晚", "17:00", "19:00"),
            ((19, 0), "晚上", "19:00", "23:00"),
            ((22, 59), "晚上", "19:00", "23:00"),
            ((23, 0), "深夜", "23:00", "24:00"),
            ((23, 59), "深夜", "23:00", "24:00"),
        ]
        for (hour, minute), name, start, end in cases:
            with self.subTest(at=f"{hour:02d}:{minute:02d}"):
                info = life.now_info(datetime(2026, 10, 21, hour, minute))
                self.assertEqual(info["period"], name)
                self.assertEqual(info["period_start"], start)
                self.assertEqual(info["period_end"], end)


# ------------------------------------------------------------------ 节假日
class HolidayTests(unittest.TestCase):
    def test_2026_holidays_and_makeup(self):
        self.assertEqual(life.holiday(date(2026, 10, 1)), "国庆节")
        self.assertEqual(life.holiday(date(2026, 10, 7)), "国庆节")
        self.assertEqual(life.holiday(date(2026, 10, 10)), "调休上班")
        self.assertEqual(life.holiday(date(2026, 1, 4)), "调休上班")
        self.assertEqual(life.holiday(date(2026, 2, 23)), "春节")
        self.assertEqual(life.holiday(date(2026, 6, 19)), "端午节")

    def test_2027_derived_schedule(self):
        self.assertEqual(life.holiday(date(2027, 2, 5)), "春节")
        self.assertEqual(life.holiday(date(2027, 2, 13)), "春节")
        self.assertEqual(life.holiday(date(2027, 2, 14)), "调休上班")
        self.assertEqual(life.holiday(date(2027, 9, 15)), "中秋节")
        self.assertEqual(life.holiday(date(2027, 10, 1)), "国庆节")

    def test_fixed_and_lunar_festivals(self):
        self.assertEqual(life.holiday(date(2027, 12, 25)), "圣诞节")
        self.assertEqual(life.holiday(date(2027, 2, 14)), "调休上班")  # 调休优先于情人节
        self.assertEqual(life.holiday(date(2027, 8, 8)), "七夕")
        self.assertIsNone(life.holiday(date(2026, 10, 11)))

    def test_weekend_is_not_a_holiday(self):
        self.assertIsNone(life.holiday(date(2026, 10, 24)))
        self.assertTrue(life.now_info(date(2026, 10, 24))["is_weekend"])


# ------------------------------------------------------------------ 作息
class ScheduleTests(unittest.TestCase):
    def test_day_parts(self):
        def key(hour, minute=0, day=21):
            return life.schedule_state(datetime(2026, 10, day, hour, minute))["key"]

        self.assertEqual(key(3), "sleep")
        self.assertEqual(key(7, 30), "wake")
        self.assertEqual(key(9), "morning")
        self.assertEqual(key(12), "noon")
        self.assertEqual(key(14, 30), "sleepy")
        self.assertEqual(key(17), "afternoon")
        self.assertEqual(key(19), "evening")
        self.assertEqual(key(21), "night")
        self.assertEqual(key(23, 45), "latenight")

    def test_weekend_shift_pushes_schedule_back(self):
        weekday = life.schedule_state(datetime(2026, 10, 21, 8, 0))
        saturday = life.schedule_state(datetime(2026, 10, 24, 8, 0))
        self.assertEqual(weekday["key"], "wake")
        self.assertEqual(saturday["key"], "sleep")      # 08:00 - 1.5h = 06:30
        self.assertTrue(saturday["quiet"])
        self.assertAlmostEqual(saturday["shift_hours"], 1.5)
        # 10:00 周六 = 生效时间 08:30，正好进入「上午精神」
        self.assertEqual(life.schedule_state(datetime(2026, 10, 24, 10, 0))["key"],
                         "morning")

    def test_shift_can_be_disabled(self):
        cfg = {"life.weekend_shift_hours": 0}
        state = life.schedule_state(datetime(2026, 10, 24, 8, 0), cfg)
        self.assertEqual(state["key"], "wake")

    def test_makeup_saturday_is_not_a_late_morning(self):
        self.assertEqual(life.holiday(date(2026, 10, 10)), "调休上班")
        state = life.schedule_state(datetime(2026, 10, 10, 8, 0))
        self.assertEqual(state["key"], "wake")
        self.assertEqual(state["shift_hours"], 0)

    def test_description_is_stable_within_the_same_block(self):
        a = life.schedule_state(datetime(2026, 10, 21, 10, 5))
        b = life.schedule_state(datetime(2026, 10, 21, 10, 55))
        self.assertEqual(a["activity"], b["activity"])
        self.assertEqual(a["spirit"], b["spirit"])

    def test_quiet_and_proactive_factor(self):
        self.assertTrue(life.quiet_now(datetime(2026, 10, 21, 3, 0)))
        self.assertFalse(life.quiet_now(datetime(2026, 10, 21, 10, 0)))
        self.assertGreaterEqual(life.proactive_factor(datetime(2026, 10, 21, 3, 0)), 100)
        self.assertEqual(life.proactive_factor(datetime(2026, 10, 21, 9, 0)), 0.7)
        self.assertEqual(life.proactive_factor(datetime(2026, 10, 21, 14, 30)), 1.5)

    def test_enabled_switch(self):
        cfg = {"life.enabled": False}
        self.assertEqual(life.life_hint(t=datetime(2026, 10, 21, 10, 0), config=cfg), "")


# ------------------------------------------------------------------ 提示词
class LifeHintTests(unittest.TestCase):
    def test_hint_has_day_state_and_rules_but_no_clock(self):
        hint = life.life_hint(t=datetime(2026, 10, 21, 10, 0))
        self.assertIn("周三", hint)
        self.assertIn("上午", hint)
        self.assertIn("你现在大概在", hint)
        self.assertIn("不要报时", hint)
        self.assertIn("不要每句都提", hint)
        self.assertIsNone(re.search(r"\d{1,2}:\d{2}", hint))

    def test_holiday_sentence(self):
        hint = life.life_hint(t=datetime(2026, 10, 1, 10, 0))
        self.assertIn("周四", hint)
        self.assertIn("国庆节", hint)
        self.assertIn("别像客服群发祝福", hint)

    def test_makeup_workday_is_not_a_blessing(self):
        hint = life.life_hint(t=datetime(2026, 10, 10, 10, 0))
        self.assertIn("调休上班", hint)
        self.assertNotIn("群发祝福", hint)

    def test_sleep_hint_tells_her_to_stay_asleep(self):
        hint = life.life_hint(t=datetime(2026, 10, 21, 3, 0))
        self.assertIn("睡着了", hint)
        self.assertIn("只有被直接叫到", hint)

    def test_group_description_is_appended(self):
        info = {"name": "测试群", "member_count": 42, "owner_name": "老王",
                "admins": [{"uid": 2, "name": "阿明"}], "self_role": "admin"}
        hint = life.life_hint(t=datetime(2026, 10, 21, 10, 0), group_info=info)
        self.assertIn("你在「测试群」里", hint)
        self.assertIn("你自己是管理员", hint)


# ------------------------------------------------------------------ 群感知
def _conn_responses():
    return {
        "get_group_info": {"status": "ok", "data": {
            "group_id": 77, "group_name": "测试群", "member_count": 42}},
        "get_group_member_list": {"status": "ok", "data": [
            {"user_id": 1, "nickname": "老王", "role": "owner"},
            {"user_id": 2, "nickname": "阿明", "card": "阿明", "role": "admin"},
            {"user_id": 3, "nickname": "小红", "role": "admin"},
            {"user_id": 4, "nickname": "路人", "role": "member"},
        ]},
        "get_group_member_info": {"status": "ok", "data": {
            "user_id": 999, "role": "member"}},
    }


class FakeConn:
    def __init__(self, responses=None):
        self.self_id = 999
        self.calls = []
        self.responses = responses if responses is not None else _conn_responses()

    async def call(self, action, timeout=20, **params):
        self.calls.append((action, dict(params)))
        value = self.responses.get(action)
        if isinstance(value, Exception):
            raise value
        return value


class GroupInfoTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, conn):
        return SimpleNamespace(conn_for=lambda: conn, config={})

    async def test_fetch_filters_members_and_caches_by_ttl(self):
        conn = FakeConn()
        gi = groupinfo.GroupInfo(ttl=1800)
        info = await gi.get(self.bot(conn), 77, now=1000.0)
        self.assertEqual(info["name"], "测试群")
        self.assertEqual(info["member_count"], 42)
        self.assertEqual(info["owner_name"], "老王")
        self.assertEqual(info["admins"], [{"uid": 2, "name": "阿明"},
                                          {"uid": 3, "name": "小红"}])
        self.assertEqual(info["self_role"], "member")
        self.assertEqual([c[0] for c in conn.calls],
                         ["get_group_info", "get_group_member_list",
                          "get_group_member_info"])

        await gi.get(self.bot(conn), 77, now=1000.0 + 1799)
        self.assertEqual(len(conn.calls), 3)          # TTL 内不再请求

        await gi.get(self.bot(conn), 77, now=1000.0 + 1801)
        self.assertEqual(len(conn.calls), 6)          # 过期后重新拉

    async def test_failure_returns_stale_cache(self):
        conn = FakeConn()
        gi = groupinfo.GroupInfo(ttl=60)
        await gi.get(self.bot(conn), 77, now=1000.0)
        conn.responses["get_group_info"] = RuntimeError("boom")
        info = await gi.get(self.bot(conn), 77, now=5000.0)
        self.assertEqual(info["name"], "测试群")      # 旧缓存
        self.assertEqual(conn.calls[-1][0], "get_group_info")

    async def test_failure_without_cache_returns_empty(self):
        conn = FakeConn({})
        conn.responses["get_group_info"] = RuntimeError("boom")
        gi = groupinfo.GroupInfo(ttl=60)
        self.assertEqual(await gi.get(self.bot(conn), 88, now=1000.0), {})

    async def test_partial_failure_is_not_cached(self):
        conn = FakeConn()
        conn.responses["get_group_member_list"] = RuntimeError("boom")
        gi = groupinfo.GroupInfo(ttl=60)
        self.assertEqual(await gi.get(self.bot(conn), 77, now=1000.0), {})
        self.assertIsNone(gi.cached(77))

    async def test_no_connection_returns_empty(self):
        gi = groupinfo.GroupInfo(ttl=60)
        bot = SimpleNamespace(conn_for=lambda: None, config={})
        self.assertEqual(await gi.get(bot, 88, now=1000.0), {})
        self.assertIsNone(gi.cached(88))

    async def test_for_bot_attaches_instance(self):
        bot = SimpleNamespace(config={"life.groupinfo_ttl": 120})
        gi = groupinfo.for_bot(bot)
        self.assertIs(gi, groupinfo.for_bot(bot))
        self.assertEqual(gi.ttl, 120)

    def test_describe_format(self):
        desc = groupinfo.describe({
            "name": "测试群", "member_count": 42,
            "owner_name": "老王",
            "admins": [{"uid": 2, "name": "阿明"}, {"uid": 3, "name": "小红"}],
            "self_role": "member",
        })
        self.assertEqual(desc, "你在「测试群」里（42 人），群主是 老王，"
                               "管理有 阿明、小红；你自己是普通成员。")
        self.assertEqual(groupinfo.describe({}), "")


# ------------------------------------------------------------------ 生日
class BirthdayParseTests(unittest.TestCase):
    def test_self_birthday_forms(self):
        for text in ("我生日是10月21号", "我的生日 10.21", "我 10/21 生日",
                     "我是10月21号生日", "我生日10-21", "我生日：10月21日",
                     "我生日是2026年10月21日"):
            with self.subTest(text=text):
                parsed = life_events.parse_birthday(text)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["md"], "10-21")

    def test_other_people_are_ignored(self):
        for text in ("他生日是10月21号", "我妈生日10月21号", "我妈妈生日10月21号",
                     "他 10/21 生日", "今天10月21号"):
            with self.subTest(text=text):
                self.assertIsNone(life_events.parse_birthday(text))

    def test_invalid_date_is_ignored(self):
        self.assertIsNone(life_events.parse_birthday("我生日是13月40号"))
        self.assertIsNone(life_events.parse_birthday("我今天生日"))


class FakeCtx:
    def __init__(self, bot, *, group_id=77, user_id=2, sender_name="阿明", plain=""):
        self.bot = bot
        self.group_id = group_id
        self.user_id = user_id
        self.sender_name = sender_name
        self.plain = plain
        self.is_group = True
        self.sent = []

    async def send(self, message, **kwargs):
        self.sent.append((message, kwargs))
        return {"message_id": len(self.sent)}


class BirthdayWishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pawbot-birthday-")
        self.addCleanup(self.tmp.cleanup)
        self.bot = SimpleNamespace(
            base_dir=self.tmp.name,
            config={"life.birthday_file": "data/birthdays.json"},
            store=SimpleNamespace(append=Mock()),
        )

    def write(self, data):
        path = life_events.birthday_path(self.bot)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    async def test_wishes_only_once_per_day_and_records_it(self):
        self.write({"123": {"md": "10-21", "name": "阿明", "groups": [77]}})
        ctx = FakeCtx(self.bot)
        sent = await life_events.check_birthdays(ctx, now=datetime(2026, 10, 21, 10, 0))
        self.assertEqual(sent, 1)
        self.assertEqual(len(ctx.sent), 1)
        segments, kwargs = ctx.sent[0]
        self.assertEqual(kwargs.get("group_id"), 77)
        self.assertEqual(segments[0], {"type": "at", "data": {"qq": "123"}})
        self.assertIn("生日快乐", segments[1]["data"]["text"])
        self.assertEqual(self.bot.store.append.call_count, 1)

        again = await life_events.check_birthdays(ctx, now=datetime(2026, 10, 21, 15, 0))
        self.assertEqual(again, 0)
        self.assertEqual(len(ctx.sent), 1)
        data = life_events.load_birthdays(self.bot)
        self.assertEqual(data["wished"]["123"], "2026-10-21")

    async def test_outside_window_does_not_wish(self):
        self.write({"123": {"md": "10-21", "name": "阿明", "groups": [77]}})
        ctx = FakeCtx(self.bot)
        self.assertEqual(await life_events.check_birthdays(
            ctx, now=datetime(2026, 10, 21, 8, 59)), 0)
        self.assertEqual(await life_events.check_birthdays(
            ctx, now=datetime(2026, 10, 21, 22, 0)), 0)
        self.assertEqual(len(ctx.sent), 0)
        # 时段内再检查，仍然会补上（失败/时段外不写 wished）
        self.assertEqual(await life_events.check_birthdays(
            ctx, now=datetime(2026, 10, 21, 9, 0)), 1)

    async def test_other_groups_are_not_wished(self):
        self.write({"123": {"md": "10-21", "name": "阿明", "groups": [88]}})
        ctx = FakeCtx(self.bot, group_id=77)
        self.assertEqual(await life_events.check_birthdays(
            ctx, now=datetime(2026, 10, 21, 10, 0)), 0)

    async def test_remember_only_self_and_keeps_groups(self):
        ctx = FakeCtx(self.bot, user_id=123, plain="我生日是10月21号")
        self.assertTrue(life_events.remember_birthday(ctx))
        data = life_events.load_birthdays(self.bot)
        self.assertEqual(data["123"]["md"], "10-21")
        self.assertEqual(data["123"]["groups"], [77])

        other = FakeCtx(self.bot, user_id=456, plain="他生日是10月22号")
        self.assertFalse(life_events.remember_birthday(other))
        self.assertNotIn("456", life_events.load_birthdays(self.bot))


class LifeEventsHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_group_info_refreshes_in_background(self):
        with tempfile.TemporaryDirectory(prefix="pawbot-life-") as tmp:
            conn = FakeConn()
            bot = SimpleNamespace(base_dir=tmp, config={},
                                  store=SimpleNamespace(append=Mock()),
                                  conn_for=lambda: conn)
            ctx = FakeCtx(bot)
            gi = groupinfo.for_bot(bot)
            self.assertFalse(gi.is_fresh(77))

            await life_events.life_events(ctx)
            self.assertFalse(gi.is_fresh(77))       # 后台刷新，没阻塞消息
            for _ in range(100):
                await asyncio.sleep(0.01)
                if gi.cached(77):
                    break
            self.assertEqual(gi.cached(77)["name"], "测试群")

            calls = len(conn.calls)
            await life_events.life_events(ctx)      # 缓存新了，不再请求
            await asyncio.sleep(0.05)
            self.assertEqual(len(conn.calls), calls)


# ------------------------------------------------------------------ 配置
class LifeConfigTests(unittest.TestCase):
    def test_default_config_has_life_section(self):
        cfg = DEFAULT.get("life") or {}
        self.assertTrue(cfg.get("enabled"))
        self.assertEqual(cfg.get("groupinfo_ttl"), 1800)
        self.assertEqual(cfg.get("birthday_wish_hours"), [9, 22])
        keys = [b.get("key") for b in cfg.get("schedule") or []]
        self.assertIn("sleep", keys)
        self.assertIn("latenight", keys)
        self.assertTrue(any(b.get("quiet") for b in cfg["schedule"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
