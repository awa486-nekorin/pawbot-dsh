"""离线测试 QQ 分条；不连接 QQ、模型或真实数据目录。"""

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import message as msg
from pawbot.safety import SafetyGuard


class RecordingConnection:
    def __init__(self, bot=None):
        self.messages = []
        self.targets = []
        self.bot = bot

    async def send_message(self, segments, **target):
        self.messages.append(copy.deepcopy(segments))
        self.targets.append(target)
        if self.bot:
            self.bot.inbound["test"] = self.bot.inbound.get("test", 0) + 1
        return {"message_id": len(self.messages)}


class MessageSplitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(config={"safety.burst": 3,
                                           "safety.react_delay": [0, 0]},
                                   inbound={})
        self.guard = SafetyGuard(self.bot)
        self.sleep_patch = patch("pawbot.safety.asyncio.sleep", new_callable=AsyncMock)
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def texts(self, parts):
        return [msg.to_plain(part) for part in parts]

    def test_marker_and_empty_chunks(self):
        parts = self.guard._split([msg.Text("‖第一句。‖‖第二句！‖")])
        self.assertEqual(self.texts(parts), ["第一句。", "第二句！"])
        self.assertEqual(self.guard._split([msg.Text("‖‖ \n‖")]), [])
        self.assertEqual(self.guard._split([]), [])

    def test_no_marker_preserves_long_text_code_and_newlines(self):
        text = "第一句。\n" * 100 + "a || b"
        self.assertEqual(self.texts(self.guard._split([msg.Text(text)])), [text])

    def test_quote_and_mention_only_on_first_message(self):
        parts = self.guard._split([
            msg.Reply("1"), msg.At("2"), msg.Text(" "),
            msg.Text("甲。‖乙。‖丙。")])
        self.assertEqual([s["type"] for s in parts[0]], ["reply", "at", "text", "text"])
        self.assertEqual([s["type"] for s in parts[1]], ["text"])
        self.assertEqual([s["type"] for s in parts[2]], ["text"])

    def test_images_and_multiple_text_segments_keep_order(self):
        image = {"type": "image", "data": {"file": "local-test.png"}}
        source = [msg.Text("甲‖乙"), image, msg.Text("丙‖丁")]
        before = copy.deepcopy(source)
        parts = self.guard._split(source)
        self.assertEqual(self.texts(parts), ["甲", "乙", "丙，丁", "[图片]"])
        self.assertEqual(parts[3], [image])
        self.assertEqual(source, before)

    def test_overflow_merges_without_losing_text(self):
        for burst, count in [(1, 1), (2, 2), (3, 3), (8, 3)]:
            with self.subTest(burst=burst):
                self.bot.config["safety.burst"] = burst
                parts = self.guard._split([msg.Text("一‖二‖三‖四‖五")])
                self.assertEqual(len(parts), count)
                # 超出的部分并进最后一条：已收句直接接，没收句补逗号，不塞换行
                joined = "".join(self.texts(parts)).replace("\n", "").replace("，", "")
                self.assertEqual(joined, "一二三四五")

    async def test_send_stops_remaining_parts_after_two_inbound_messages(self):
        connection = RecordingConnection(self.bot)
        async def fake_send(segments, **target):
            connection.messages.append(copy.deepcopy(segments))
            self.bot.inbound["test"] = self.bot.inbound.get("test", 0) + 2
            return {"message_id": len(connection.messages)}
        connection.send_message = fake_send
        # 規格變更：新增两条消息后，分段回复的剩余部分视为过时并丢弃。
        await self.guard.send(connection, "test", [msg.Text("甲‖乙‖丙")], group_id=42)
        self.assertEqual(self.texts(connection.messages), ["甲"])


    async def test_disabled_safety_still_splits_private_messages(self):
        self.bot.config["safety.enabled"] = False
        connection = RecordingConnection()
        await self.guard.send(connection, "test", [msg.Text("甲‖乙")], user_id=7)
        self.assertEqual(self.texts(connection.messages), ["甲", "乙"])
        self.assertEqual(connection.targets, [{"user_id": 7}] * 2)

    async def test_each_part_waits_for_its_own_typing_time(self):
        events = []

        async def fake_sleep(delay):
            events.append(("sleep", delay))

        connection = RecordingConnection()
        original_send = connection.send_message

        async def fake_send(segments, **target):
            events.append(("send", msg.to_plain(segments)))
            return await original_send(segments, **target)

        self.sleep.side_effect = fake_sleep
        connection.send_message = fake_send
        self.guard._think_delay = lambda part: len(msg.to_plain(part)) / 10
        self.guard._gap_delay = lambda: 0.9
        await self.guard.send(connection, "test", [msg.Text("甲乙‖丙丁戊己")], group_id=42)
        self.assertEqual(events, [("sleep", 0.2), ("send", "甲乙"),
                                  ("sleep", 0.9), ("sleep", 0.4),
                                  ("send", "丙丁戊己")])

    def test_typing_delay_tracks_length_and_caps_long_text(self):
        self.bot.config.update({"safety.typing_cps": 10, "safety.min_delay": 0.6,
                                "safety.max_delay": 4})
        with patch("pawbot.safety.random.uniform", return_value=1):
            self.assertEqual(self.guard._think_delay([msg.Text("甲" * 10)]), 1)
            self.assertEqual(self.guard._think_delay([msg.Text("甲" * 20)]), 2)
            self.assertEqual(self.guard._think_delay([msg.Text("甲" * 100)]), 4)
            self.assertEqual(self.guard._think_delay([]), 0.6)

    async def test_disabled_safety_keeps_typing_delays(self):
        self.bot.config["safety.enabled"] = False
        self.guard._think_delay = lambda part: 0.6
        self.guard._gap_delay = lambda: 1.0
        await self.guard.send(RecordingConnection(), "test", [msg.Text("甲‖乙")], user_id=7)
        self.assertEqual([c.args[0] for c in self.sleep.await_args_list], [0.6, 1.0, 0.6])

    async def test_rate_limit_and_marker_only_send_nothing(self):
        connection = RecordingConnection()
        self.assertIsNone(await self.guard.send(connection, "empty", [msg.Text("‖‖")], group_id=42))
        # 新规则：一次回复 1 个 token，所以把这一分钟的上限压到 3 再塞满
        self.bot.config["safety.per_minute"] = 3
        self.guard._bucket["test"] = [__import__("time").time()] * 3
        self.assertIsNone(await self.guard.send(connection, "test", [msg.Text("甲‖乙")], group_id=42))
        self.assertEqual(connection.messages, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
