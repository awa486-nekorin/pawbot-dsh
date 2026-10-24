"""离线测试拟人化发送延迟、发送限速与主动发言收敛（不发真实请求）。"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.proactive import Proactive
from pawbot.safety import SafetyGuard


def text(t: str) -> dict:
    return {"type": "text", "data": {"text": t}}


def make_guard(**overrides):
    cfg = {"safety.enabled": True,
           "safety.react_delay": [0, 0],
           "safety.typing_cps": 10,
           "safety.min_delay": 0.6,
           "safety.max_delay": 12.0,
           "safety.per_minute": 6,
           "safety.per_hour": 60}
    cfg.update(overrides)
    bot = SimpleNamespace(config=cfg, inbound={"s": 0})
    return SafetyGuard(bot), bot


def make_conn():
    return SimpleNamespace(send_message=AsyncMock(return_value={"ok": 1}))


def sleep_args(mock) -> list[float]:
    return [c.args[0] for c in mock.await_args_list]


class ReactionDelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sleep = patch("pawbot.safety.asyncio.sleep", new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def test_first_part_reacts_then_types_without_generation_credit(self):
        guard, _bot = make_guard(**{"safety.react_delay": [2.0, 2.0]})
        guard._think_delay = lambda part: 5.0
        await guard.send(make_conn(), "s", [text("在吗")], group_id=1, elapsed=4.0)
        # 先反应 2 秒，再老老实实打 5 秒；生成花的 4 秒不再抵扣
        self.assertEqual(sleep_args(self.sleep), [2.0, 5.0])

    async def test_subtract_elapsed_true_restores_old_credit(self):
        guard, _bot = make_guard(**{"safety.react_delay": [0, 0],
                                    "safety.subtract_elapsed": True})
        guard._think_delay = lambda part: 5.0
        await guard.send(make_conn(), "s", [text("在吗")], group_id=1, elapsed=4.0)
        self.assertEqual(sleep_args(self.sleep), [1.0])

    async def test_split_parts_keep_gap_and_typing(self):
        guard, _bot = make_guard(**{"safety.react_delay": [2.0, 2.0]})
        guard._think_delay = lambda part: 0.8
        guard._gap_delay = lambda: 1.0
        await guard.send(make_conn(), "s", [text("甲‖乙")], group_id=1, elapsed=99.0)
        # 第一段：反应 2 + 打字 0.8；第二段：分条间隔 1 + 自己的打字 0.8
        self.assertEqual(sleep_args(self.sleep), [2.0, 0.8, 1.0, 0.8])


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sleep = patch("pawbot.safety.asyncio.sleep", new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def test_seventh_reply_in_a_minute_is_dropped(self):
        guard, _bot = make_guard()
        conn = make_conn()
        for _ in range(6):
            self.assertIsNotNone(
                await guard.send(conn, "s", [text("在吗")], group_id=1))
        self.assertIsNone(await guard.send(conn, "s", [text("在吗")], group_id=1))
        self.assertEqual(conn.send_message.await_count, 6)

    async def test_per_hour_cap(self):
        guard, _bot = make_guard(**{"safety.per_minute": 100,
                                    "safety.per_hour": 3})
        conn = make_conn()
        for _ in range(3):
            self.assertIsNotNone(
                await guard.send(conn, "s", [text("在吗")], group_id=1))
        self.assertIsNone(await guard.send(conn, "s", [text("在吗")], group_id=1))
        self.assertEqual(conn.send_message.await_count, 3)

    async def test_multi_part_reply_costs_only_one_token(self):
        guard, _bot = make_guard()
        conn = make_conn()
        # 一次回复拆成 3 条也只算 1 个 token
        self.assertIsNotNone(
            await guard.send(conn, "s", [text("一‖二‖三")], group_id=1))
        for _ in range(5):
            self.assertIsNotNone(
                await guard.send(conn, "s", [text("四")], group_id=1))
        self.assertIsNone(await guard.send(conn, "s", [text("五")], group_id=1))
        self.assertEqual(conn.send_message.await_count, 8)

    async def test_private_sessions_are_limited_too(self):
        guard, _bot = make_guard(**{"safety.per_minute": 1})
        conn = make_conn()
        self.assertIsNotNone(
            await guard.send(conn, "private_7", [text("在吗")], user_id=7))
        self.assertIsNone(
            await guard.send(conn, "private_7", [text("在吗")], user_id=7))


class _FakeContext:
    sink: list = []
    receipt = {"message_id": 1}

    def __init__(self, **kwargs):
        pass

    async def send(self, payload, *, elapsed=0.0):
        _FakeContext.sink.append(payload)
        return _FakeContext.receipt


class _FakeTopic:
    def __init__(self):
        self.groups = {"group_1": {}}

    def should_chime_in(self, session_id, config):
        return True, "冷场"

    def mark_chime(self, session_id):
        pass

    def seen(self, *args, **kwargs):
        pass

    def digest(self, session_id, n=6):
        return ""

    def keywords(self, session_id, n=5):
        return []


def make_pro_bot(state_path: str, **overrides):
    values = {"proactive.daily_max": 12,
              "proactive.min_gap_min": 0,
              "proactive.state_file": state_path,
              "proactive.bili_min_gap": 3600}
    values.update(overrides)
    return SimpleNamespace(
        config=SimpleNamespace(get=lambda key, default=None: values.get(key, default)),
        topic=_FakeTopic(),
        store=SimpleNamespace(append=Mock()),
    )


class ProactiveQuotaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pawbot-proactive-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = os.path.join(self.tmp, "proactive_state.json")
        _FakeContext.sink = []
        _FakeContext.receipt = {"message_id": 1}

    def make_pro(self, bot=None, **overrides):
        bot = bot or make_pro_bot(self.state_path)
        if overrides:
            base = bot.config.get
            merged = dict(overrides)
            bot.config.get = lambda key, default=None: merged.get(
                key, base(key, default))
        pro = Proactive(bot)
        pro._choose = AsyncMock(return_value={"kind": "join", "hint": "接话"})
        pro._compose = AsyncMock(return_value=("我也想说", []))
        return pro

    async def speak(self, pro):
        async def close(coro, name=None):
            coro.close()
        with patch("pawbot.proactive.Context", _FakeContext), \
                patch("pawbot.style.polish", side_effect=lambda t: t), \
                patch("pawbot.proactive.tasks.spawn", side_effect=close):
            await pro._speak_once(1, "group_1")

    def read_state(self):
        with open(self.state_path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def test_daily_max_blocks_further_speaking(self):
        pro = self.make_pro(**{"proactive.daily_max": 2})
        for _ in range(3):
            await self.speak(pro)
        self.assertEqual(len(_FakeContext.sink), 2)
        state = self.read_state()
        self.assertEqual(state["counts"]["group_1"], 2)
        self.assertEqual(state["date"], time.strftime("%Y-%m-%d", time.localtime()))

    async def test_min_gap_blocks_and_expires(self):
        pro = self.make_pro(**{"proactive.daily_max": 12,
                               "proactive.min_gap_min": 20})
        await self.speak(pro)
        self.assertEqual(len(_FakeContext.sink), 1)
        await self.speak(pro)                       # 20 分钟内第二次 → 跳过
        self.assertEqual(len(_FakeContext.sink), 1)

        state = self.read_state()
        state["last"]["group_1"] = time.time() - 21 * 60
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        pro2 = self.make_pro(**{"proactive.daily_max": 12,
                                "proactive.min_gap_min": 20})
        await self.speak(pro2)
        self.assertEqual(len(_FakeContext.sink), 2)

    async def test_count_resets_on_new_local_day_but_gap_survives(self):
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"date": yesterday,
                       "counts": {"group_1": 99},
                       "last": {"group_1": time.time() - 3600}}, f)
        pro = self.make_pro(**{"proactive.daily_max": 1,
                               "proactive.min_gap_min": 20})
        await self.speak(pro)
        self.assertEqual(len(_FakeContext.sink), 1)
        state = self.read_state()
        self.assertEqual(state["date"], time.strftime("%Y-%m-%d", time.localtime()))
        self.assertEqual(state["counts"]["group_1"], 1)

    async def test_count_persists_across_instances(self):
        pro = self.make_pro(**{"proactive.daily_max": 1})
        await self.speak(pro)
        self.assertEqual(len(_FakeContext.sink), 1)
        pro2 = self.make_pro(**{"proactive.daily_max": 1})
        await self.speak(pro2)
        self.assertEqual(len(_FakeContext.sink), 1)
        self.assertEqual(self.read_state()["counts"]["group_1"], 1)

    async def test_only_successful_sends_are_counted(self):
        _FakeContext.receipt = None
        pro = self.make_pro(**{"proactive.daily_max": 5})
        await self.speak(pro)
        self.assertEqual(pro._load_state()["counts"].get("group_1", 0), 0)
        _FakeContext.receipt = {"message_id": 1}
        await self.speak(pro)
        self.assertEqual(self.read_state()["counts"]["group_1"], 1)

    async def test_broken_state_file_is_ignored(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        pro = self.make_pro(**{"proactive.daily_max": 1})
        await self.speak(pro)
        self.assertEqual(len(_FakeContext.sink), 1)
        self.assertEqual(self.read_state()["counts"]["group_1"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
