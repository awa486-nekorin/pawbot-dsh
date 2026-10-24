"""离线测试：分条（polish / safety._split）与引用消息补全（perceive）。

不连 QQ、不调模型、不读写真实配置外的任何文件。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import event as ev_mod
from pawbot import message as msg
from pawbot import perceive
from pawbot.safety import SafetyGuard
from pawbot.style import polish


# ---------------------------------------------------------------- polish
class PolishSplitTests(unittest.TestCase):
    def test_three_parts_stay_three(self):
        self.assertEqual(polish("一。‖二。‖三。"), "一。‖二。‖三。")

    def test_four_parts_collapse_into_three_without_newlines(self):
        result = polish("一。‖二。‖三。‖四。", max_parts=3)
        self.assertEqual(result, "一。‖二。‖三。四。")
        self.assertEqual(result.count("‖"), 2)
        self.assertNotIn("\n", result)

    def test_paragraph_newlines_become_split_marks(self):
        self.assertEqual(polish("一。\n二。"), "一。‖二。")
        self.assertEqual(polish("一。\n\n二。"), "一。‖二。")
        result = polish("一。\n二。\n三。\n四。")
        self.assertEqual(result.count("‖"), 2)
        self.assertNotIn("\n", result)

    def test_protected_newlines_are_kept(self):
        code = "看这个：\n```\nprint(1)\nprint(2)\n```"
        self.assertIn("\n", polish(code))
        url = "见 https://example.com/a?x=1\n后面这句"
        self.assertEqual(polish(url), "见 https://example.com/a?x=1‖后面这句")
        for source in ("- 第一\n- 第二\n- 第三",
                       "* 甲\n* 乙",
                       "1. 先这样\n2. 再那样"):
            with self.subTest(source=source):
                self.assertEqual(polish(source), source)
                self.assertNotIn("‖", polish(source))

    def test_pass_is_untouched(self):
        for source in ("<pass>", " <pass> ", "开头。\n<pass>"):
            with self.subTest(source=source):
                self.assertEqual(polish(source), source)

    def test_max_parts_parameter(self):
        self.assertEqual(polish("一。‖二。‖三。", max_parts=2), "一。‖二。三。")
        self.assertEqual(polish("一。‖二。‖三。", max_parts=9), "一。‖二。‖三。")


# ---------------------------------------------------------------- safety._split
class SplitTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.bot = SimpleNamespace(config={"safety.burst": 3}, inbound={})
        self.guard = SafetyGuard(self.bot)

    def test_quote_and_at_only_on_first_part(self):
        parts = self.guard._split([
            msg.Reply("1"), msg.At("2"), msg.Text(" 甲 \n‖\n 乙 \n")])
        self.assertEqual([s["type"] for s in parts[0]], ["reply", "at", "text"])
        self.assertEqual([s["type"] for s in parts[1]], ["text"])
        for part in parts[1:]:
            self.assertFalse(any(s["type"] in ("reply", "at") for s in part))

    def test_each_part_is_stripped(self):
        parts = self.guard._split([msg.Text("  甲  ‖\n\n乙\n‖\t丙\t")])
        texts = [s["data"]["text"] for part in parts for s in part
                 if s["type"] == "text"]
        self.assertEqual(texts, ["甲", "乙", "丙"])

    def test_overflow_merge_has_no_newline(self):
        self.bot.config["safety.burst"] = 2
        parts = self.guard._split([msg.Text("一‖二‖三")])
        self.assertEqual(len(parts), 2)
        self.assertNotIn("\n", msg.to_plain(parts[-1]))


# ---------------------------------------------------------------- 引用
class FakeConfig:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)


class FakeConn:
    """冒充 onebot.Connection：call 返回已经剥过外层的 data（真实行为）。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def call(self, action, **params):
        self.calls.append((action, params))
        if isinstance(self.reply, Exception):
            raise self.reply
        if callable(self.reply):
            return self.reply(**params)
        return self.reply


INNER = {
    "message_id": 645288937,
    "message": [
        {"type": "text", "data": {"text": "我昨天说要去上海"}},
    ],
    "raw_message": "我昨天说要去上海",
    "sender": {"user_id": 1, "nickname": "大肥鱼"},
}


def _event(text="你说啥", reply_id="645288937"):
    raw = {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "time": 1, "self_id": 1, "message_id": 2, "group_id": 9, "user_id": 7,
        "message": [{"type": "reply", "data": {"id": reply_id}},
                    {"type": "text", "data": {"text": text}}],
        "raw_message": f"[CQ:reply,id={reply_id}] {text}",
        "sender": {"user_id": 7, "nickname": "阿明"},
    }
    return ev_mod.parse(raw)


class QuoteFetchTests(unittest.IsolatedAsyncioTestCase):
    def _plain(self, segs):
        return msg.to_plain(segs)

    async def test_envelope_with_data(self):
        conn = FakeConn({"status": "ok", "retcode": 0, "data": INNER})
        segs, sender = await perceive._fetch_quoted(conn, "645288937")
        self.assertEqual(self._plain(segs), "我昨天说要去上海")
        self.assertEqual(sender.get("nickname"), "大肥鱼")

    async def test_already_unwrapped_data(self):
        conn = FakeConn(dict(INNER))
        segs, sender = await perceive._fetch_quoted(conn, "645288937")
        self.assertEqual(self._plain(segs), "我昨天说要去上海")
        self.assertEqual(sender.get("user_id"), 1)

    async def test_message_as_cq_string(self):
        conn = FakeConn({"message": "[CQ:at,qq=1]早", "sender": {"nickname": "甲"}})
        segs, _sender = await perceive._fetch_quoted(conn, "42")
        self.assertEqual(self._plain(segs), "@1 早")

    async def test_message_as_array(self):
        conn = FakeConn({"message": [{"type": "text", "data": {"text": "数组形态"}}]})
        segs, _sender = await perceive._fetch_quoted(conn, "42")
        self.assertEqual(self._plain(segs), "数组形态")

    async def test_raw_message_fallback(self):
        conn = FakeConn({"raw_message": "[CQ:text]没给 message 字段",
                         "sender": {"nickname": "甲"}})
        segs, _sender = await perceive._fetch_quoted(conn, "42")
        self.assertEqual(self._plain(segs), "没给 message 字段")

    async def test_retries_with_int_when_original_id_fails(self):
        def strict(message_id=None, **kw):
            if not isinstance(message_id, int):
                raise ValueError("message_id 必须是整数")
            return dict(INNER)
        conn = FakeConn(strict)
        segs, _sender = await perceive._fetch_quoted(conn, "645288937")
        self.assertEqual(self._plain(segs), "我昨天说要去上海")
        self.assertEqual(len(conn.calls), 2)

    async def test_failure_returns_none_and_logs_info(self):
        conn = FakeConn(RuntimeError("boom"))
        with patch("pawbot.perceive.log.info") as info:
            segs, sender = await perceive._fetch_quoted(conn, "42")
        self.assertIsNone(segs)
        self.assertEqual(sender, {})
        self.assertTrue(info.called)


class QuoteEnrichTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self):
        from types import SimpleNamespace
        return SimpleNamespace(config=FakeConfig({"llm.api_key": ""}))

    async def test_quoted_text_and_from_are_filled(self):
        ev = _event()
        await perceive.enrich(self._bot(), FakeConn(dict(INNER)), ev)
        self.assertEqual(ev.reply_id, "645288937")
        self.assertEqual(ev.quoted_text, "我昨天说要去上海")
        self.assertEqual(ev.quoted_from, "大肥鱼")
        self.assertTrue(ev.quoted_is_self)

    async def test_unavailable_quote_still_tells_the_model(self):
        ev = _event()
        with patch("pawbot.perceive.log.info"):
            await perceive.enrich(self._bot(), FakeConn(RuntimeError("boom")), ev)
        self.assertIn("取不到", ev.quoted_text)

    async def test_empty_payload_still_tells_the_model(self):
        ev = _event()
        with patch("pawbot.perceive.log.info"):
            await perceive.enrich(self._bot(), FakeConn({}), ev)
        self.assertIn("取不到", ev.quoted_text)


class ReplyIdTests(unittest.TestCase):
    def test_missing_id_is_none_not_string_none(self):
        self.assertIsNone(msg.find_reply_id([{"type": "reply", "data": {}}]))
        self.assertIsNone(msg.find_reply_id([{"type": "reply", "data": {"id": ""}}]))
        self.assertIsNone(msg.find_reply_id([]))

    def test_alternate_and_numeric_ids(self):
        self.assertEqual(msg.find_reply_id([{"type": "reply", "data": {"id": "645288937"}}]),
                         "645288937")
        self.assertEqual(msg.find_reply_id([{"type": "reply", "data": {"id": -123}}]), "-123")
        self.assertEqual(msg.find_reply_id([{"type": "reply",
                                             "data": {"message_id": "77"}}]), "77")


if __name__ == "__main__":
    unittest.main(verbosity=2)
