"""主动上网冲浪的节流：概率 + 每群最短间隔，没过就接话或不开口。"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot.proactive import Proactive


class _Topic:
    def __init__(self, digest="", kws=None):
        self._digest = digest
        self._kws = kws or []

    def digest(self, session_id, n=6):
        return self._digest

    def keywords(self, session_id, n=5):
        return self._kws

    def solid_keywords(self, session_id, n=4):
        return []


def make_pro(topic=None, **overrides):
    values = {"proactive.surf_chance": 1.0, "proactive.surf_min_gap_min": 120,
              "proactive.idle_activity": "search"}
    values.update(overrides)
    bot = SimpleNamespace(
        config=SimpleNamespace(get=lambda key, default=None: values.get(key, default)),
        topic=topic or _Topic(),
    )
    return Proactive(bot)


class SurfGateTests(unittest.IsolatedAsyncioTestCase):
    def test_chance_zero_never_surfs(self):
        pro = make_pro(**{"proactive.surf_chance": 0})
        self.assertFalse(any(pro._surf_allowed("group_1") for _ in range(50)))

    def test_min_gap_blocks_second_surf(self):
        pro = make_pro()
        self.assertTrue(pro._surf_allowed("group_1"))
        self.assertFalse(pro._surf_allowed("group_1"))
        self.assertTrue(pro._surf_allowed("group_2"))     # 间隔按群算

    def test_gap_expires(self):
        pro = make_pro()
        self.assertTrue(pro._surf_allowed("group_1"))
        pro._last_surf["group_1"] = time.time() - 121 * 60
        self.assertTrue(pro._surf_allowed("group_1"))

    def test_failed_roll_does_not_start_cooldown(self):
        pro = make_pro(**{"proactive.surf_chance": 0})
        pro._surf_allowed("group_1")
        self.assertNotIn("group_1", pro._last_surf)

    async def test_choose_surfs_when_allowed(self):
        pro = make_pro(topic=_Topic())
        with patch("pawbot.proactive.random.random", return_value=0.99):
            action = await pro._choose("group_1")
        self.assertEqual(action["kind"], "search")

    async def test_denied_with_topic_joins(self):
        pro = make_pro(topic=_Topic("阿明：今天好热", ["好热"]),
                       **{"proactive.surf_chance": 0})
        with patch("pawbot.proactive.random.random", return_value=0.99):
            action = await pro._choose("group_1")
        self.assertEqual(action["kind"], "join")

    async def test_denied_without_topic_skips(self):
        pro = make_pro(**{"proactive.surf_chance": 0})
        action = await pro._choose("group_1")
        self.assertEqual(action["kind"], "skip")

    async def test_skip_action_makes_no_compose_call(self):
        pro = make_pro()
        pro._quota_ok = lambda sid: (True, "")
        pro.bot.topic.should_chime_in = lambda sid, cfg: (True, "ok")
        pro._choose = AsyncMock(return_value={"kind": "skip", "hint": "不说"})
        pro._compose = AsyncMock()
        await pro._speak_once(1, "group_1")
        pro._compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
