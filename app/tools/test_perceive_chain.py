"""引用链与跨来源图片编号测试；假连接、假视觉，全程离线。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pawbot import perceive
from pawbot.event import MessageEvent


def text(value: str) -> dict:
    return {"type": "text", "data": {"text": value}}


def reply(message_id: str) -> dict:
    return {"type": "reply", "data": {"id": message_id}}


def image(name: str) -> dict:
    return {"type": "image", "data": {"file": name}}


def payload(name: str, *segments: dict) -> dict:
    return {"message_id": name, "message": list(segments),
            "sender": {"nickname": name, "user_id": 7}}


class FakeConn:
    def __init__(self, messages=None, error: Exception | None = None):
        self.messages = {str(k): v for k, v in (messages or {}).items()}
        self.error = error
        self.calls = []

    async def call(self, action, timeout=20, **params):
        self.calls.append((action, params))
        if action != "get_msg":
            raise AssertionError(f"测试不应调用 {action}")
        if self.error:
            raise self.error
        message_id = str(params["message_id"])
        if message_id not in self.messages:
            raise RuntimeError("消息不存在")
        return self.messages[message_id]


async def fake_read_images(segments, config):
    return [str(seg["data"]["file"]).encode("utf-8") for seg in segments]


async def fake_see(raws, config):
    name = raws[0].decode("utf-8")
    return f"描述-{name}", [f"data:image/fake;base64,{name}"]


class PerceiveChainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.annotations = []
        self.bot = SimpleNamespace(
            config={"llm.vision_max_images": 4},
            recent_images={},
            store=SimpleNamespace(
                annotate_last=lambda sid, uid, desc: self.annotations.append(desc)),
        )

    def event(self, *segments: dict, reply_id=None) -> MessageEvent:
        return MessageEvent(post_type="message", message_type="group", self_id=99,
                            group_id=1, user_id=8, message=list(segments),
                            reply_id=reply_id, plain="测试")

    async def enrich(self, ev, conn):
        with patch.object(perceive, "_read_images", new=fake_read_images), \
                patch.object(perceive, "_see", new=fake_see):
            await perceive.enrich(self.bot, conn, ev)

    async def test_nested_quote_has_two_levels_and_both_texts(self):
        conn = FakeConn({
            "A": payload("A", reply("B"), text("A 的正文")),
            "B": payload("B", text("B 的正文")),
        })
        ev = self.event(text("C 的正文"), reply_id="A")

        await self.enrich(ev, conn)

        self.assertEqual([item["depth"] for item in ev.quoted_chain], [1, 2])
        self.assertEqual([item["text"] for item in ev.quoted_chain],
                         ["A 的正文", "B 的正文"])
        self.assertIn("A 的正文", ev.quoted_text)
        self.assertIn("B 的正文", ev.quoted_text)
        self.assertIn("又引用了", ev.quoted_text)

    async def test_quote_cycle_stops_by_message_id(self):
        conn = FakeConn({
            "A": payload("A", reply("B"), text("A")),
            "B": payload("B", reply("A"), text("B")),
        })
        ev = self.event(reply_id="A")

        await self.enrich(ev, conn)

        self.assertLessEqual(len(ev.quoted_chain), 3)
        self.assertEqual(len(ev.quoted_chain), 2)
        self.assertEqual(len(conn.calls), 2)

    async def test_three_own_images_then_one_quoted_image(self):
        conn = FakeConn({"A": payload("A", image("引用1"))})
        ev = self.event(image("本条1"), image("本条2"), image("本条3"), reply_id="A")

        await self.enrich(ev, conn)

        self.assertEqual([item["idx"] for item in ev.image_items], [1, 2, 3, 4])
        self.assertEqual([item["source"] for item in ev.image_items],
                         ["本条", "本条", "本条", "引用"])
        self.assertEqual(
            ev.image_desc.splitlines(),
            ["【图1】描述-本条1", "【图2】描述-本条2",
             "【图3】描述-本条3", "【图4】描述-引用1"],
        )
        self.assertEqual(ev.quoted_chain[0]["images"], ["【图4】描述-引用1"])
        # 2026-10-25 行為變更：改傳 list，讓 store.annotate_last 逐格填描述
        # （傳 join 過的字串只會填第一格，其餘圖永远裸著）。
        self.assertEqual(self.annotations,
                         [["【图1】描述-本条1", "【图2】描述-本条2", "【图3】描述-本条3"]])

    async def test_forward_images_continue_the_same_numbering(self):
        conn = FakeConn({"A": payload("A", image("引用1"))})
        forward = {"type": "forward", "data": {"id": "F"}}
        ev = self.event(image("本条1"), forward, reply_id="A")

        with patch.object(perceive, "_fetch_forward",
                          return_value=("转发正文", [image("转发1")])):
            await self.enrich(ev, conn)

        self.assertEqual([item["source"] for item in ev.image_items],
                         ["本条", "引用", "转发"])
        self.assertEqual(ev.image_desc.splitlines(),
                         ["【图1】描述-本条1", "【图2】描述-引用1", "【图3】描述-转发1"])
        self.assertIn("【图3】描述-转发1", ev.forward_text)

    async def test_single_image_is_not_numbered(self):
        ev = self.event(image("唯一"))

        await self.enrich(ev, FakeConn())

        self.assertEqual(ev.image_desc, "描述-唯一")
        self.assertNotIn("【图", ev.image_desc)
        self.assertEqual(ev.image_items[0]["desc"], "描述-唯一")
        self.assertEqual(self.annotations, [["描述-唯一"]])

    async def test_unavailable_quote_degrades_without_exception(self):
        ev = self.event(reply_id="missing")

        await self.enrich(ev, FakeConn(error=RuntimeError("离线失败")))

        self.assertEqual(ev.quoted_text, "（他引用了一条消息，但内容取不到）")
        self.assertEqual(ev.quoted_chain, [])


if __name__ == "__main__":
    unittest.main()
