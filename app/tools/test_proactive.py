"""主动发言频率与视频分享频率：全部离线。"""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import bilibili
from pawbot.proactive import Proactive
from pawbot.topic import TopicTracker


class Cfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def make_bot(values=None):
    cfg = Cfg(values or {})
    topic = TopicTracker()
    topic.activity = lambda sid: {"level": 0.0, "per_hour": 0.0, "known": False}
    return SimpleNamespace(config=cfg, topic=topic)


class SpanTests(unittest.TestCase):
    def test_span_reads_config_instead_of_hardcoded_seconds(self):
        bot = make_bot({"proactive.idle_min": 900, "proactive.idle_max": 2400,
                        "proactive.adaptive": False})
        lo, hi = Proactive(bot)._span("group_1")
        self.assertEqual((lo, hi), (900.0, 2400.0))

    def test_floor_still_applies(self):
        bot = make_bot({"proactive.idle_min": 10, "proactive.idle_max": 20,
                        "proactive.adaptive": False, "proactive.idle_floor": 300})
        self.assertEqual(Proactive(bot)._span("group_1"), (300.0, 300.0))

    def test_busy_group_waits_longer(self):
        bot = make_bot({"proactive.idle_min": 900, "proactive.idle_max": 2400,
                        "proactive.adaptive": True,
                        "proactive.adaptive_range": [0.75, 1.5]})
        bot.topic.activity = lambda sid: {"level": 1.0, "per_hour": 60.0, "known": True}
        lo, hi = Proactive(bot)._span("group_1")
        self.assertAlmostEqual(lo, 1350.0)
        self.assertAlmostEqual(hi, 3600.0)

    def test_span_is_not_seconds_any_more(self):
        bot = make_bot({"proactive.idle_min": 900, "proactive.idle_max": 2400})
        lo, _hi = Proactive(bot)._span("group_1")
        self.assertGreater(lo, 300)


class ShouldChimeTests(unittest.TestCase):
    def setUp(self):
        self.topic = TopicTracker()
        self.cfg = {"topic.min_idle": 120, "topic.chime_cooldown": 300,
                    "topic.busy_threshold": 12}

    def test_just_spoke_blocks(self):
        self.topic.seen("group_1", "阿明", "在吗")
        ok, why = self.topic.should_chime_in("group_1", self.cfg)
        self.assertFalse(ok)
        self.assertIn("刚有人", why)

    def test_cold_group_gets_a_turn(self):
        self.topic.seen("group_1", "阿明", "在吗")
        self.topic._node("group_1")["last_human_at"] -= 9999
        ok, _why = self.topic.should_chime_in("group_1", self.cfg)
        self.assertTrue(ok)

    def test_cooldown_after_chime(self):
        self.topic.seen("group_1", "阿明", "在吗")
        self.topic._node("group_1")["last_human_at"] -= 9999
        self.topic.mark_chime("group_1")
        ok, why = self.topic.should_chime_in("group_1", {"topic.min_idle": 0,
                                                         "topic.chime_cooldown": 300})
        self.assertFalse(ok)
        self.assertIn("刚插过", why)

    def test_busy_group_is_left_alone(self):
        self.topic.seen("group_1", "阿明", "在吗")
        self.topic._node("group_1")["last_human_at"] -= 9999
        with patch.object(TopicTracker, "activity",
                          return_value={"level": 1.0, "per_hour": 1200.0}):
            ok, why = self.topic.should_chime_in("group_1", self.cfg)
        self.assertFalse(ok)
        self.assertIn("太热闹", why)


class BiliFrequencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili._shared_at.clear()
        self.addCleanup(bilibili._shared_at.clear)

    def test_gap_blocks_repeat_shares(self):
        self.assertTrue(bilibili.share_allowed("group_1", 3600))
        bilibili.mark_shared("group_1")
        self.assertFalse(bilibili.share_allowed("group_1", 3600))
        self.assertTrue(bilibili.share_allowed("group_2", 3600))   # 别的群不受影响

    def test_zero_gap_disables_the_limit(self):
        bilibili.mark_shared("group_1")
        self.assertTrue(bilibili.share_allowed("group_1", 0))

    async def test_fun_bili_falls_back_to_web_inside_gap(self):
        bot = make_bot({"proactive.bili_min_gap": 3600})
        bot.topic.solid_keywords = lambda sid, n=4: []
        pro = Proactive(bot)
        bilibili.mark_shared("group_1")
        picked = await pro._fun_bili("group_1")
        self.assertEqual(picked["kind"], "search")

    async def test_bili_chance_controls_how_often_auto_shares(self):
        bot = make_bot({"proactive.idle_activity": "auto",
                        "proactive.bili_chance": 0.0,
                        "proactive.surf_chance": 1.0,
                        "proactive.surf_min_gap_min": 0})
        bot.topic.digest = lambda sid, n=6: ""
        bot.topic.keywords = lambda sid, n=5: []
        pro = Proactive(bot)
        for _ in range(20):
            self.assertEqual((await pro._choose("group_1"))["kind"], "search")

    async def test_join_chance_unchanged(self):
        bot = make_bot({})
        bot.topic.digest = lambda sid, n=6: "他们在聊数学"
        bot.topic.keywords = lambda sid, n=5: ["数学"]
        pro = Proactive(bot)
        with patch("pawbot.proactive.random.random", return_value=0.1):
            self.assertEqual((await pro._choose("group_1"))["kind"], "join")


class ToolGapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili._shared_at.clear()
        self.addCleanup(bilibili._shared_at.clear)

    async def test_tool_refuses_second_video_in_same_group(self):
        from pawbot.tools import ToolBox

        bot = SimpleNamespace(config=Cfg({"tools.bili_min_gap": 3600}))
        box = ToolBox(bot)
        bilibili.mark_shared("group_1")
        ctx = SimpleNamespace(session_id="group_1")
        text, media = await box._t_bili_video(ctx, "")
        self.assertEqual(media, [])
        self.assertIn("刚分享过", text)


class ComposeTests(unittest.IsolatedAsyncioTestCase):
    """真的跑一遍 _compose：之前 summary 未定义的 NameError 只有走到这里才会炸。"""

    async def test_compose_builds_prompt_with_memory_and_life(self):
        import os
        import tempfile
        from unittest.mock import AsyncMock, Mock
        from pawbot.store import Store

        store = Store(os.path.join(tempfile.mkdtemp(), "s.json"))
        store.append("group_1", "user", "最近在追番", who="阿明", uid=10001)
        store.set_summary("group_1", "大家在聊番剧。")
        store.set_turn_summary("group_1", "阿明说在追番。", 1.0)
        seen = {}

        async def chat_message(messages, tools=None):
            seen["messages"] = messages
            return {"content": "我也想看"}

        bot = make_bot({"tools.enabled": False, "life.enabled": True})
        bot.store = store
        bot.persona = SimpleNamespace(build=Mock(return_value="persona"))
        bot.llm = SimpleNamespace(chat_message=chat_message)
        bot.toolbox = SimpleNamespace(schemas=Mock(return_value=[]), execute=AsyncMock())
        bot.mood = SimpleNamespace(hint=lambda: "")
        bot.topic.digest = lambda sid, n=6: "阿明：最近在追番"
        bot.longmem = SimpleNamespace(format_recall=Mock(return_value="## 你想起来的\n- 阿明爱看番"))

        text, media = await Proactive(bot)._compose(1, "group_1", {"kind": "join", "hint": "接话"})
        self.assertEqual(text, "我也想看")
        joined = "\n".join(str(m["content"]) for m in seen["messages"])
        self.assertIn("阿明说在追番", joined)
        self.assertIn("阿明爱看番", joined)
        extra = bot.persona.build.call_args.kwargs["extra"]
        self.assertIn("周", extra)            # life_hint 带星期


if __name__ == "__main__":
    unittest.main()
