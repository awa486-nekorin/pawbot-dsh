"""I14 不完美感：正在输入状态 + 偶尔手滑一处再更正。全部离线，用假 conn。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import message as msg
from pawbot.safety import SafetyGuard, typo_plan

ALLOWED_PAIRS = {
    ("已经", "以经"),   # 已/以
    ("觉得", "觉的"),   # 得/的
    ("真的", "真得"),   # 的/得
    ("当作", "当做"),   # 作/做
    ("再说", "在说"),   # 再/在
    ("哪里", "那里"),   # 哪/那
}


class Conn:
    """假协议端：记下每条消息和每次 typing 调用。"""

    def __init__(self, typing_error=None):
        self.messages = []
        self.typing_calls = []
        self.typing_error = typing_error

    async def send_message(self, segments, **target):
        self.messages.append(segments)
        return {"message_id": len(self.messages)}

    async def typing(self, *, group_id=None, user_id=None):
        self.typing_calls.append({"group_id": group_id, "user_id": user_id})
        if self.typing_error:
            raise self.typing_error


def bot_with(**overrides):
    config = {"safety.enabled": True,
              "safety.typing_cps": 10,
              "safety.min_delay": 0.9,
              "safety.max_delay": 4.0,
              # 这些用例只测 typing / 手滑，关掉第一段的反应延迟
              "safety.react_delay": [0, 0],
              "safety.split_gap": [1.0, 1.0],
              "safety.typo_chance": 0.0,      # 默认关掉手滑，各测试自己开
              "safety.typo_fix_delay": [0.01, 0.02],
              "bot.nickname": "大肥鱼"}
    config.update(overrides)
    return SimpleNamespace(config=config, inbound={"s": 0})


class RecordingConn:
    """只记录 call 的假 Connection，用来测协议层的 typing()。"""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def call(self, action, timeout=20, **params):
        self.calls.append({"action": action, "timeout": timeout, "params": params})
        if self.error:
            raise self.error
        return {"status": "ok"}


class ConnectionTypingTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_params_and_short_timeout(self):
        from pawbot.onebot import Connection
        conn = RecordingConn()
        await Connection.typing(conn, group_id=42, user_id=7)
        self.assertEqual(conn.calls[0]["action"], "set_input_status")
        self.assertEqual(conn.calls[0]["timeout"], 3)
        self.assertEqual(conn.calls[0]["params"],
                         {"event_type": 1, "user_id": 7, "group_id": 42})

    async def test_failure_never_raises(self):
        from pawbot.onebot import Connection
        conn = RecordingConn(error=RuntimeError("NapCat 没回执"))
        await Connection.typing(conn, user_id=7)      # 不应抛
        self.assertEqual(conn.calls[0]["params"], {"event_type": 1, "user_id": 7})


class TypingIndicatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = bot_with()
        self.guard = SafetyGuard(self.bot)
        self.sleep = patch("pawbot.safety.asyncio.sleep",
                           new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def test_calls_typing_before_real_delay_in_group(self):
        self.guard._think_delay = lambda part: 1.0
        conn = Conn()
        result = await self.guard.send(conn, "s", [msg.Text("你好世界")],
                                       group_id=42, user_id=7)
        self.assertIsNotNone(result)
        self.assertEqual(conn.typing_calls, [{"group_id": 42, "user_id": 7}])
        self.assertEqual(len(conn.messages), 1)
        # typing 要在 sleep 之前：先亮状态，再等
        self.assertEqual(self.sleep.await_args_list[0].args[0], 1.0)

    async def test_no_typing_when_delay_is_tiny(self):
        self.guard._think_delay = lambda part: 0.2
        conn = Conn()
        await self.guard.send(conn, "s", [msg.Text("你好世界")], group_id=42)
        self.assertEqual(conn.typing_calls, [])
        self.assertEqual(len(conn.messages), 1)

    async def test_no_typing_without_text(self):
        self.guard._think_delay = lambda part: 1.0
        conn = Conn()
        await self.guard.send(conn, "s", [{"type": "face", "data": {"id": "1"}}],
                              group_id=42)
        self.assertEqual(conn.typing_calls, [])
        self.assertEqual(len(conn.messages), 1)

    async def test_typing_failure_does_not_block_sending(self):
        self.guard._think_delay = lambda part: 1.0
        conn = Conn(typing_error=RuntimeError("NapCat 不认这个动作"))
        result = await self.guard.send(conn, "s", [msg.Text("你好世界")],
                                       group_id=42, user_id=7)
        self.assertIsNotNone(result)
        self.assertEqual(len(conn.typing_calls), 1)
        self.assertEqual(len(conn.messages), 1)

    async def test_switch_off_means_no_call(self):
        self.bot.config["safety.typing_indicator"] = False
        self.guard._think_delay = lambda part: 1.0
        conn = Conn()
        await self.guard.send(conn, "s", [msg.Text("你好世界")], group_id=42)
        self.assertEqual(conn.typing_calls, [])
        self.assertEqual(len(conn.messages), 1)

    async def test_typing_not_repeated_for_every_part(self):
        self.guard._think_delay = lambda part: 1.0
        self.guard._gap_delay = lambda: 1.0
        conn = Conn()
        await self.guard.send(conn, "s", [msg.Text("第一句话‖第二句话")], group_id=42)
        self.assertEqual(len(conn.typing_calls), 1)
        self.assertEqual(len(conn.messages), 2)


class TypoPlanTests(unittest.TestCase):
    def test_short_text_never_typos(self):
        self.assertIsNone(typo_plan("真的吗"))

    def test_quotes_and_urls_are_untouched(self):
        self.assertIsNone(typo_plan("「已经」是引用 https://x.test/已经"))

    def test_code_spans_are_untouched(self):
        self.assertIsNone(typo_plan("他说 `已经` 够了"))
        self.assertIsNone(typo_plan("看这段 ```已经够了```"))

    def test_picks_a_known_pair(self):
        plan = typo_plan("我觉得这个事情很复杂")
        self.assertIsNotNone(plan)
        wrong_text, right = plan
        self.assertEqual(right, "觉得")
        self.assertEqual(wrong_text, "我觉的这个事情很复杂")
        self.assertIn((right, "觉的"), ALLOWED_PAIRS)

    def test_blocked_words_are_skipped(self):
        self.assertIsNone(typo_plan("我觉得这个事情很复杂", {"觉得"}))


class TypoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = bot_with()
        self.guard = SafetyGuard(self.bot)
        self.sleep = patch("pawbot.safety.asyncio.sleep",
                           new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def _send(self, text, *, group_id=1, user_id=2, addressed=False):
        segs = [msg.At(2), msg.Text(text)] if addressed else [msg.Text(text)]
        conn = Conn()
        result = await self.guard.send(conn, "s", segs, group_id=group_id, user_id=user_id)
        return conn, result

    async def test_chance_zero_never_changes_text(self):
        self.bot.config["safety.typo_chance"] = 0.0
        with patch("random.random", return_value=0.0):
            conn, _ = await self._send("我觉得这个事情很复杂")
        self.assertEqual(len(conn.messages), 1)
        self.assertEqual(conn.messages[0][-1]["data"]["text"], "我觉得这个事情很复杂")

    async def test_chance_one_changes_exactly_one_place_and_fixes_it(self):
        self.bot.config["safety.typo_chance"] = 1.0
        with patch("random.random", return_value=0.0):
            conn, result = await self._send("我觉得这个事情很复杂")
        self.assertIsNotNone(result)
        self.assertEqual(len(conn.messages), 2)
        wrong = conn.messages[0][-1]["data"]["text"]
        right = "我觉得这个事情很复杂"
        self.assertEqual(wrong, "我觉的这个事情很复杂")
        diff = [(a, b) for a, b in zip(wrong, right) if a != b]
        self.assertEqual(diff, [("的", "得")])          # 只动一处
        self.assertIn(("觉得", "觉的"), ALLOWED_PAIRS)
        fix = conn.messages[1][0]["data"]["text"]
        self.assertEqual(fix, "打错了，是「觉得」")
        self.assertLessEqual(len(fix), 12)
        self.assertNotIn("（", fix)
        self.assertNotIn("纠正", fix)

    async def test_short_message_is_left_alone(self):
        self.bot.config["safety.typo_chance"] = 1.0
        with patch("random.random", return_value=0.0):
            conn, _ = await self._send("真的吗")
        self.assertEqual(len(conn.messages), 1)
        self.assertEqual(conn.messages[0][0]["data"]["text"], "真的吗")

    async def test_url_digits_and_quotes_are_preserved(self):
        self.bot.config["safety.typo_chance"] = 1.0
        text = "已经3次了 https://x.test/有 还有「已经」"
        with patch("random.random", return_value=0.0):
            conn, _ = await self._send(text)
        self.assertEqual(len(conn.messages), 2)
        wrong = conn.messages[0][0]["data"]["text"]
        self.assertIn("3次", wrong)
        self.assertIn("https://x.test/有", wrong)
        self.assertIn("「已经」", wrong)
        self.assertNotIn("「以经」", wrong)
        self.assertEqual(wrong.replace("以经", "已经"), text)

    async def test_addressed_message_halves_the_chance(self):
        self.bot.config["safety.typo_chance"] = 1.0
        with patch("random.random", return_value=0.6):   # 0.6 < 1.0，但 > 0.5
            conn, _ = await self._send("我觉得这个事情很复杂")
            self.assertEqual(conn.messages[0][-1]["data"]["text"], "我觉的这个事情很复杂")
            conn2, _ = await self._send("我觉得这个事情很复杂", addressed=True)
            self.assertEqual(len(conn2.messages), 1)
            self.assertEqual(conn2.messages[0][-1]["data"]["text"],
                             "我觉得这个事情很复杂")

    async def test_multi_part_never_typos(self):
        self.bot.config["safety.typo_chance"] = 1.0
        with patch("random.random", return_value=0.0):
            conn, _ = await self._send("我觉得这个‖事情很复杂")
        self.assertEqual(len(conn.messages), 2)
        self.assertEqual([m[0]["data"]["text"] for m in conn.messages],
                         ["我觉得这个", "事情很复杂"])

    async def test_fix_failure_is_swallowed(self):
        self.bot.config["safety.typo_chance"] = 1.0
        calls = {"n": 0}

        class FlakyConn(Conn):
            async def send_message(self, segments, **target):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("更正发不出去")
                return await super().send_message(segments, **target)

        with patch("random.random", return_value=0.0):
            conn = FlakyConn()
            result = await self.guard.send(conn, "s", [msg.Text("我觉得这个事情很复杂")],
                                           group_id=1, user_id=2)
        self.assertIsNotNone(result)
        self.assertEqual(conn.messages[0][-1]["data"]["text"], "我觉的这个事情很复杂")


if __name__ == "__main__":
    unittest.main(verbosity=2)
