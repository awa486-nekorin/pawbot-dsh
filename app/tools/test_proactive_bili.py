"""主动刷 B 站：优先用真实观看报告，没报告就请求去看并老实说没看过。全部离线。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import bilibili
from pawbot import bili_watch
from pawbot.proactive import Proactive
from pawbot.store import Store


class Cfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Topic:
    def __init__(self, kws=None):
        self._kws = kws or []
        self.chimed = None

    def digest(self, session_id, n=6):
        return ""

    def keywords(self, session_id, n=5):
        return []

    def solid_keywords(self, session_id, n=4):
        return list(self._kws)

    def should_chime_in(self, session_id, config):
        return True, "冷场"

    def mark_chime(self, session_id):
        self.chimed = session_id

    def seen(self, session_id, sender, text, *, is_self=False):
        pass


def fresh_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def make_report(bvid, title="测试视频", watched_level="subtitle",
                created_at=None, **extra):
    rep = {
        "version": 1,
        "bvid": bvid,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "title": title,
        "author": "某UP主",
        "duration_sec": 120,
        "play": 10000,
        "pubdate": "2026-10-20T10:00:00Z",
        "tname": "科技",
        "watched_level": watched_level,
        "summary": f"{title}的摘要内容",
        "highlights": ["03:12 具体细节一", "05:00 具体细节二"],
        "comment_vibe": "评论区都在说好",
        "top_comments": [{"text": "前排", "like": 5}],
        "take": "这个挺有意思的",
        "share_worthy": True,
        "share_reason": "值得一看",
        "created_at": created_at or fresh_iso(),
        "source_request": None,
    }
    rep.update(extra)
    return rep


def write_report(root, rep):
    reports = os.path.join(root, "reports")
    os.makedirs(reports, exist_ok=True)
    path = os.path.join(reports, rep["bvid"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False)
    return path


FAKE_PICKS = [
    {"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
     "author": "UP", "play": 100, "tname": "科技", "pic": ""},
    {"title": "备选一", "url": "https://www.bilibili.com/video/BVtwo",
     "author": "UP2", "play": 200, "tname": "生活", "pic": ""},
    {"title": "备选二", "url": "https://www.bilibili.com/video/BVthree",
     "author": "UP3", "play": 300, "tname": "游戏", "pic": ""},
]


def make_bot(tmp, kws=None, **overrides):
    values = {
        "proactive.bili_min_gap": 0,
        "tools.bili_watch_enabled": True,
        "tools.bili_watch_dir": tmp,
    }
    values.update(overrides)
    return SimpleNamespace(config=Cfg(values), topic=_Topic(kws))


class FunBiliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili._shared_at.clear()
        self.addCleanup(bilibili._shared_at.clear)

    async def test_report_becomes_honest_bili_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = make_report("BVwatch", title="看过的好视频",
                              watched_level="subtitle")
            write_report(tmp, rep)
            bot = make_bot(tmp)
            with patch("pawbot.proactive.bilibili.pick_lively",
                       side_effect=AssertionError("有报告时不该再只看标题")):
                action = await Proactive(bot)._fun_bili("group_1")

            self.assertEqual(action["kind"], "bili")
            self.assertEqual(action["report"]["bvid"], "BVwatch")
            self.assertEqual(action["videos"], [])
            self.assertIn("观看程度", action["hint"])
            self.assertIn("subtitle", action["hint"])
            self.assertIn("真的看过", action["hint"])
            # 有报告就不该投递「去看」请求
            self.assertFalse(os.path.exists(os.path.join(tmp, "requests")))

    async def test_title_only_report_hint_does_not_claim_watched(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = make_report("BVtitle", title="只看到标题的视频",
                              watched_level="title_only")
            write_report(tmp, rep)
            action = await Proactive(make_bot(tmp))._fun_bili("group_1")

            self.assertEqual(action["report"]["bvid"], "BVtitle")
            self.assertIn("title_only", action["hint"])
            self.assertIn("别假装看过", action["hint"])
            self.assertNotIn("真的看过", action["hint"])

    async def test_no_report_requests_watch_and_stays_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp, kws=["猫咪"])
            with patch("pawbot.proactive.bilibili.pick_lively",
                       return_value=list(FAKE_PICKS)), \
                    patch("pawbot.proactive.random.random", return_value=0.0):
                action = await Proactive(bot)._fun_bili("group_1")

            self.assertEqual(action["kind"], "bili")
            self.assertNotIn("report", action)
            self.assertEqual(action["videos"][0]["title"], "热门标题")
            self.assertIn("还没看", action["hint"])
            self.assertIn("别假装看过", action["hint"])

            reqdir = os.path.join(tmp, "requests")
            reqs = [f for f in os.listdir(reqdir) if f.endswith(".json")]
            self.assertEqual(len(reqs), 1)
            with open(os.path.join(reqdir, reqs[0]), "r", encoding="utf-8") as f:
                req = json.load(f)
            self.assertEqual(req["kind"], "keyword")
            self.assertEqual(req["value"], "猫咪")
            self.assertEqual(req["session"], "group_1")

    async def test_no_keyword_requests_auto_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            with patch("pawbot.proactive.bilibili.pick_lively",
                       return_value=list(FAKE_PICKS)):
                action = await Proactive(bot)._fun_bili("group_1")

            reqdir = os.path.join(tmp, "requests")
            reqs = [f for f in os.listdir(reqdir) if f.endswith(".json")]
            self.assertEqual(len(reqs), 1)
            with open(os.path.join(reqdir, reqs[0]), "r", encoding="utf-8") as f:
                req = json.load(f)
            self.assertEqual(req["kind"], "auto")
            self.assertEqual(req["value"], "")
            self.assertIn("别假装看过", action["hint"])

    async def test_bili_watch_errors_fall_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp, kws=["猫咪"])
            with patch("pawbot.proactive.bili_watch.pick_report",
                       side_effect=RuntimeError("报告目录坏了")), \
                    patch("pawbot.proactive.bili_watch.request_watch",
                          side_effect=RuntimeError("请求目录坏了")), \
                    patch("pawbot.proactive.bilibili.pick_lively",
                          return_value=list(FAKE_PICKS)), \
                    patch("pawbot.proactive.random.random", return_value=0.0):
                action = await Proactive(bot)._fun_bili("group_1")

            self.assertEqual(action["kind"], "bili")
            self.assertEqual(action["videos"][0]["title"], "热门标题")
            self.assertIn("还没看", action["hint"])
            self.assertIn("别假装看过", action["hint"])

    async def test_disabled_skips_report_and_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVdisabled", title="不该被读"))
            bot = make_bot(tmp, **{"tools.bili_watch_enabled": False})
            with patch("pawbot.proactive.bilibili.pick_lively",
                       return_value=list(FAKE_PICKS)):
                action = await Proactive(bot)._fun_bili("group_1")

            self.assertEqual(action["kind"], "bili")
            self.assertNotIn("report", action)
            self.assertFalse(os.path.exists(os.path.join(tmp, "requests")))
            self.assertIn("别假装看过", action["hint"])


class ComposeBiliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili._shared_at.clear()
        self.addCleanup(bilibili._shared_at.clear)

    def make_pro(self, tmp, seen, **overrides):
        store = Store(os.path.join(tmp, "session.json"))
        store.append("group_1", "user", "最近在追番", who="阿明", uid=10001)

        async def chat_message(messages, tools=None):
            seen["messages"] = messages
            return {"content": "这个视频有点意思"}

        values = {
            "tools.enabled": False,
            "life.enabled": False,
            "tools.bili_watch_dir": tmp,
            "tools.bili_json_card": False,
        }
        values.update(overrides)
        bot = SimpleNamespace(
            config=Cfg(values),
            store=store,
            persona=SimpleNamespace(build=Mock(return_value="persona")),
            llm=SimpleNamespace(chat_message=chat_message),
            toolbox=SimpleNamespace(schemas=Mock(return_value=[]),
                                    execute=Mock()),
            mood=SimpleNamespace(hint=lambda: ""),
            topic=_Topic(),
        )
        return Proactive(bot)

    async def test_compose_uses_report_material_but_does_not_mark_shared(self):
        # P2-6：_compose 只负责生成，还没发出去，绝不能先记「已分享」
        with tempfile.TemporaryDirectory() as tmp:
            rep = make_report("BVcompose", title="真的看过的视频",
                              watched_level="subtitle")
            write_report(tmp, rep)
            seen = {}
            pro = self.make_pro(tmp, seen)
            text, media = await pro._compose("1", "group_1", {
                "kind": "bili", "videos": [], "report": rep, "hint": "分享",
            })

            self.assertEqual(text, "这个视频有点意思")
            joined = "\n".join(str(m["content"]) for m in seen["messages"])
            self.assertIn("你的真实观看程度", joined)
            self.assertIn("真的看过", joined)
            self.assertIn(rep["summary"], joined)
            self.assertIn("具体细节一", joined)
            self.assertIn("评论区都在说好", joined)

            # 默认不发 JSON 卡片，只发标题 + 链接
            self.assertTrue(media)
            self.assertTrue(all(s.get("type") == "text" for s in media))
            joined_media = " ".join(str(s.get("data", {}).get("text", ""))
                                    for s in media)
            self.assertIn("真的看过的视频", joined_media)
            self.assertIn("BVcompose", joined_media)

            self.assertFalse(os.path.exists(os.path.join(tmp, "shared.json")))

    async def test_compose_without_report_keeps_old_path_but_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}
            pro = self.make_pro(tmp, seen)
            v = dict(FAKE_PICKS[0])
            text, media = await pro._compose("1", "group_1", {
                "kind": "bili", "videos": [v], "hint": "分享",
            })

            self.assertEqual(text, "这个视频有点意思")
            joined = "\n".join(str(m["content"]) for m in seen["messages"])
            self.assertIn("还没看过内容", joined)
            self.assertIn("别假装看过", joined)
            self.assertIn(v["title"], joined)
            self.assertFalse(os.path.exists(os.path.join(tmp, "shared.json")))
            joined_media = " ".join(str(s.get("data", {}).get("text", ""))
                                    for s in media)
            self.assertIn(v["title"], joined_media)
            self.assertIn(v["url"], joined_media)

    async def test_card_enabled_still_sends_card_for_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}
            rep = make_report("BVcard", title="卡片视频")
            pro = self.make_pro(tmp, seen, **{"tools.bili_json_card": True})
            _text, media = await pro._compose("1", "group_1", {
                "kind": "bili", "videos": [], "report": rep, "hint": "分享",
            })

            self.assertTrue(any(s.get("type") == "json" for s in media))
            self.assertTrue(any("卡片视频" in str(s.get("data", {}).get("text", ""))
                                for s in media if s.get("type") == "text"))

    async def test_report_missing_url_skips_card_instead_of_keyerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}
            rep = make_report("BVpartial", title="字段不全的视频")
            rep.pop("url")
            pro = self.make_pro(tmp, seen, **{"tools.bili_json_card": True})
            text, media = await pro._compose("1", "group_1", {
                "kind": "bili", "videos": [], "report": rep, "hint": "分享",
            })

            self.assertEqual(text, "这个视频有点意思")
            self.assertEqual(media, [])
            joined = "\n".join(str(m["content"]) for m in seen["messages"])
            self.assertIn("你的真实观看程度", joined)


class SpeakOnceBiliTests(unittest.IsolatedAsyncioTestCase):
    """P2-6：mark_shared 必須發生在「真的送出去」之後——模型回「-」或發送失敗都不記。"""

    def setUp(self):
        bilibili._shared_at.clear()
        self.addCleanup(bilibili._shared_at.clear)

    def make_pro(self, tmp, reply="这个视频有点意思"):
        store = Store(os.path.join(tmp, "session.json"))
        store.append("group_1", "user", "最近在追番", who="阿明", uid=10001)

        async def chat_message(messages, tools=None):
            return {"content": reply}

        values = {
            "tools.enabled": False,
            "life.enabled": False,
            "tools.bili_watch_dir": tmp,
            "tools.bili_json_card": False,
            # 状态档落在 tmp，别去写仓库里的 data/**
            "proactive.state_file": os.path.join(tmp, "state.json"),
            "proactive.min_gap_min": 0,
            "proactive.daily_max": 12,
            # 固定走 B 站分支，不靠随机
            "proactive.idle_activity": "bilibili",
        }
        bot = SimpleNamespace(
            config=Cfg(values),
            store=store,
            persona=SimpleNamespace(build=Mock(return_value="persona")),
            llm=SimpleNamespace(chat_message=chat_message),
            toolbox=SimpleNamespace(schemas=Mock(return_value=[]),
                                    execute=Mock()),
            mood=SimpleNamespace(hint=lambda: ""),
            topic=_Topic(),
        )
        return Proactive(bot)

    @staticmethod
    def fake_ctx(sent, fail=False):
        class _Ctx:
            def __init__(self, event=None, bot=None, config=None):
                self.event = event
                self.bot = bot
                self.config = config

            async def send(self, payload, elapsed=0.0):
                if fail:
                    raise RuntimeError("发送失败")
                sent.append(payload)
                return {"message_id": 1}

        return _Ctx

    def patched(self, sent, fail=False):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("pawbot.proactive.Context",
                                  self.fake_ctx(sent, fail=fail)))
        stack.enter_context(patch("pawbot.proactive.tasks.spawn",
                                  lambda *a, **k: None))
        stack.enter_context(patch("pawbot.turns.update", lambda *a, **k: None))
        # 冲浪概率/插话概率固定成必然发生，别让测试靠运气
        stack.enter_context(patch("pawbot.proactive.random.random", return_value=0.0))
        return stack

    async def test_declined_reply_is_not_sent_and_not_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVdeclined", title="不想说的视频"))
            pro = self.make_pro(tmp, reply="-")
            sent = []
            with self.patched(sent):
                await pro._speak_once(1, "group_1")
            self.assertEqual(sent, [])                       # 别把「-」丢进群
            self.assertFalse(os.path.exists(os.path.join(tmp, "shared.json")))

    async def test_successful_send_marks_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVsent", title="发出去的视频"))
            pro = self.make_pro(tmp)
            sent = []
            with self.patched(sent):
                await pro._speak_once(1, "group_1")
            self.assertEqual(len(sent), 1)
            with open(os.path.join(tmp, "shared.json"), "r", encoding="utf-8") as f:
                shared = json.load(f)
            self.assertIn("BVsent", shared)
            self.assertEqual(shared["BVsent"]["session"], "group_1")

    async def test_send_failure_does_not_mark_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVfailed", title="发不出去的视频"))
            pro = self.make_pro(tmp)
            sent = []
            with self.patched(sent, fail=True):
                await pro._speak_once(1, "group_1")
            self.assertEqual(sent, [])
            self.assertFalse(os.path.exists(os.path.join(tmp, "shared.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
