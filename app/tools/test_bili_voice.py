"""离线测试：B 站发言的人味守门（bili_voice）+ 实时评论树 + 摘要缓存。

全部离线：模型用 FakeLLM，B 站接口用 AsyncMock，数据根是临时目录。
覆盖需求里的硬场景：
- 生成文本命中近期开头 → 重试一次；重试仍命中 → 放弃且后端零调用；
- 「草」当天超限被拦；相似度超阈值被拦；AI 腔/长度越界被拦；
- 评论上下文每次实时获取（两次调用两次请求、不落盘）；
- 视频摘要第二次命中缓存、不覆盖已存在的文件；
- 2026-11-02 之前的通知（以及没有 reply_time 的）被跳过；
- 弹幕与视频顶层评论同样走 check；
- bili_scroll 的刷片自动回复不绕过 check。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import (bili_auto, bili_extra, bili_gate, bili_post, bili_scroll,
                    bili_voice, intent, tools as tools_mod)
from pawbot.paths import DataPaths
from plugins import admin_cmds

BVID = "BV1xx411c7mD"
BVID2 = "BV1yy411c7mE"
RECENT = int(time.time())
OWNER = "10001"


class Cfg:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        cur = self.data
        for part in str(key).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur


class FakeLLM:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else ""


class FakePersona:
    def __init__(self, text="大肥鱼：反应快、嘴硬、说话不客套。"):
        self.text = text

    def core(self):
        return self.text


def make_bot(tmp, tools=None, replies=(), persona=True):
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "content"), exist_ok=True)
    return SimpleNamespace(
        config=Cfg({"account": {"owner_ids": [OWNER]}, "tools": dict(tools or {})}),
        base_dir=tmp,
        paths=DataPaths(root=tmp, base_dir=tmp),
        llm=FakeLLM(replies),
        persona=FakePersona() if persona else None,
    )


def write_ledger(bot, rows):
    path = bili_extra._ledger_path(bot)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"items": rows}, f, ensure_ascii=False)


def own_row(text, *, ts=RECENT, bvid=BVID, kind="comment", rpid=1):
    return {"ts": int(ts), "kind": kind, "oid": 9, "rpid": rpid,
            "bvid": bvid, "text": text, "deleted": False}


def notification(nid="n1", bvid=BVID, reply_time=RECENT, **extra):
    item = {"notify_id": nid, "bvid": bvid, "root_id": 111, "source_id": 222,
            "nickname": "网友", "text": "你这句挺有意思", "source_text": "原来那条",
            "reply_time": int(reply_time)}
    item.update(extra)
    return item


def make_ctx(bot, uid=OWNER, arg_text="", plain=""):
    event = SimpleNamespace(post_type="message", user_id=uid, type="message",
                            is_group=False, to_me=True, plain=plain)
    return SimpleNamespace(
        bot=bot, event=event, user_id=uid,
        reply=AsyncMock(return_value={}), stop=Mock(),
        arg_text=arg_text, raw_text="", matched_command="",
        state={}, session_id="private_1", self_id=0, group_id=None,
    )


class VoiceTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 进程内最近发言缓存按数据根分桶；这里每个测试都是新的临时目录，
        # 再显式清一次，避免测试之间串味。
        bili_voice.reset_session_own()
        bili_gate.reset_for_tests()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(bili_voice.reset_session_own)
        self.addCleanup(bili_gate.reset_for_tests)

    def _no_material(self):
        """摘要/评论上下文都别真的去请求 B 站。"""
        return [
            patch.object(bili_voice, "video_summary",
                         new=AsyncMock(return_value="")),
            patch.object(bili_extra, "comment_context",
                         new=AsyncMock(return_value={"threads": []})),
        ]

    async def _run_with(self, bot, items, action):
        patches = self._no_material()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch.object(bili_extra, "received_replies",
                          new=AsyncMock(return_value=items)), \
                patch.object(bili_scroll, "action", new=action):
            return await bili_auto.run_round(
                bot, grant_factory=lambda action, target: object())


class ComposeCheckTests(VoiceTestBase):
    async def test_banned_opener_triggers_one_retry_then_sends(self):
        bot = make_bot(self.dir, {
            "bili_auto_enabled": True, "bili_auto_reply": True,
            "bili_auto_daily_limit": 0, "bili_auto_reply_limit": 0,
        }, replies=["笑死，这个防御也太草了", "这防御音游还能这么玩"])
        write_ledger(bot, [own_row("笑死，这音游也太草了", ts=RECENT - 60)])
        action = AsyncMock(return_value={"ok": True, "data": {}})

        out = await self._run_with(bot, [notification()], action)

        self.assertEqual(len(bot.llm.calls), 2)
        self.assertEqual(out["replied"], ["这防御音游还能这么玩"])
        self.assertEqual(out["voice_rejected"], [])
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[1], "reply")
        self.assertEqual(action.await_args.args[2]["text"], "这防御音游还能这么玩")

    async def test_retry_still_banned_gives_up_with_zero_backend_calls(self):
        bot = make_bot(self.dir, {
            "bili_auto_enabled": True, "bili_auto_reply": True,
            "bili_auto_daily_limit": 0, "bili_auto_reply_limit": 0,
        }, replies=["笑死，这个音游草得离谱", "笑死，这防御太草了"])
        action = AsyncMock(return_value={"ok": True, "data": {}})

        out = await self._run_with(bot, [notification()], action)

        self.assertEqual(len(bot.llm.calls), 2)
        self.assertEqual(out["replied"], [])
        action.assert_not_awaited()
        self.assertEqual(len(out["voice_rejected"]), 1)
        self.assertEqual(out["voice_rejected"][0]["kind"], "reply")
        self.assertIn("笑死", out["voice_rejected"][0]["reason"])
        rejected = bili_auto._load_seen(bot)["replies"]["n1"]
        self.assertTrue(rejected.get("rejected"))
        self.assertIn("笑死", rejected.get("reason", ""))

    async def test_cao_daily_cap_blocks_before_sending(self):
        bot = make_bot(self.dir, {
            "bili_auto_enabled": True, "bili_auto_reply": True,
            "bili_auto_daily_limit": 0, "bili_auto_reply_limit": 0,
        }, replies=["草，这防御也太真实了", "这防御也太真实了"])
        write_ledger(bot, [own_row("草，这也能过", ts=RECENT - 30)])

        cons = bili_voice.constraints(bot)
        self.assertEqual(cons["cao_used"], 1)
        self.assertEqual(cons["cao_cap"], 1)
        self.assertIn("草", cons["banned_phrases"])
        reasons = bili_voice.check("草，这也太真实了", bot, BVID, cons=cons)
        self.assertTrue(any("草" in reason for reason in reasons), reasons)
        # 今天还没用满时也不许一条里塞两个「草」
        fresh = bili_voice.constraints(make_bot(self.dir))
        self.assertTrue(any("草" in reason for reason in
                            bili_voice.check("草，草，这也行", bot, BVID, cons=fresh)))

        action = AsyncMock(return_value={"ok": True, "data": {}})
        out = await self._run_with(bot, [notification()], action)
        action.assert_awaited_once()
        self.assertEqual(out["replied"], ["这防御也太真实了"])

    async def test_similarity_over_threshold_is_rejected(self):
        bot = make_bot(self.dir, {"bili_voice_similarity": 0.6},
                       replies=["按住鼠标苟活啊", "换个说法这音游是真难"])
        write_ledger(bot, [own_row("按住鼠标苟活", ts=RECENT - 20)])

        reasons = bili_voice.check("按住鼠标苟活啊", bot, BVID)
        self.assertTrue(any("太像" in reason for reason in reasons), reasons)
        self.assertTrue(any("同视频历史" in reason for reason in reasons), reasons)

        action = AsyncMock(return_value={"ok": True, "data": {}})
        bot.config.data["tools"].update({
            "bili_auto_enabled": True, "bili_auto_reply": True,
            "bili_auto_daily_limit": 0, "bili_auto_reply_limit": 0,
        })
        out = await self._run_with(bot, [notification()], action)
        action.assert_awaited_once()
        self.assertEqual(out["replied"], ["换个说法这音游是真难"])

    def test_ai_tone_and_length_are_rejected(self):
        bot = make_bot(self.dir)
        reasons = bili_voice.check("总的来说这个视频告诉我们，首先不要上头。", bot, BVID)
        self.assertTrue(any("AI 腔" in reason for reason in reasons), reasons)
        self.assertTrue(any("太长" in reason
                            for reason in bili_voice.check("啊" * 200, bot, BVID)))
        self.assertTrue(any("太短" in reason
                            for reason in bili_voice.check("好", bot, BVID)))

    def test_cao_daily_cap_counts_beyond_recent_n(self):
        bot = make_bot(self.dir, {"bili_voice_recent_n": 1})
        write_ledger(bot, [own_row("草，今天这个真离谱", ts=RECENT - 120),
                           own_row("后面又说了一句", ts=RECENT - 10)])
        cons = bili_voice.constraints(bot)
        self.assertEqual(len(cons["rows"]), 1)          # 开头/口头禅只取最近 1 条
        self.assertEqual(cons["cao_used"], 1)           # 但「草」按当天全部发言算
        self.assertIn("草", cons["banned_phrases"])

    def test_constraints_use_recent_openers_and_truncate_persona(self):
        bot = make_bot(self.dir)
        write_ledger(bot, [own_row("好家伙，这也行", ts=RECENT - 10),
                           own_row("草，可以的", ts=RECENT - 5)])
        cons = bili_voice.constraints(bot)
        self.assertTrue(any(opener.startswith("好家伙") for opener in cons["openers"]))
        self.assertIn("草", cons["banned_phrases"])
        self.assertIn("本次写作硬约束", cons["prompt"])
        self.assertLessEqual(len(bili_voice.persona_block(bot, limit=8)), 9)


class VoiceSwitchTests(VoiceTestBase):
    """``tools.bili_voice_enabled`` 关掉 = 三种检查路径都不查；记账/去重照旧。"""

    def test_disabled_switch_skips_tools_and_post_checks(self):
        bot = make_bot(self.dir, {"bili_voice_enabled": False})
        # 账本里先放一条会命中「笑死」的发言：开关开着时这里一定会被拦。
        write_ledger(bot, [own_row("笑死，这波操作也太真实了", ts=RECENT - 10)])
        self.assertEqual(tools_mod._bili_voice_reasons(
            bot, "笑死，这波操作也太真实了", {"bvid": BVID}, "reply"), [])
        self.assertEqual(bili_post._voice_violations(
            bot, "笑死，这波操作也太真实了", 120), [])

    async def test_disabled_switch_uses_legacy_compose_path(self):
        bot = make_bot(self.dir, {"bili_voice_enabled": False},
                       replies=["笑死，这波操作也太真实了"])
        draft = await bili_voice.compose_text(
            bot, task="回一条", bvid=BVID, max_chars=80)
        self.assertEqual(str(draft), "笑死，这波操作也太真实了")
        self.assertEqual(len(bot.llm.calls), 1)
        self.assertEqual(len(bot.llm.calls[0]), 1)          # 旧路径只有一条 user 消息
        self.assertIn("你在 B 站刷视频", bot.llm.calls[0][0]["content"])
        self.assertNotIn("你的人设", bot.llm.calls[0][0]["content"])

    async def test_disabled_switch_still_records_successful_sent_text(self):
        bot = make_bot(self.dir, {"bili_voice_enabled": False})
        tb = object.__new__(tools_mod.ToolBox)
        tb.bot = bot
        tb._is_owner = lambda ctx: True
        tb._current_request_text = lambda ctx: "帮我回一条评论"
        tb._external_tainted = lambda ctx: False
        action = AsyncMock(return_value={"ok": True, "data": {"success": True}})
        with patch.object(tools_mod.bili_gate, "owner_turn_grant",
                          return_value=object()), \
                patch.object(tools_mod.bili_scroll, "action", new=action):
            out, _ = await tb._t_bili_action(
                None, "reply",
                {"bvid": BVID, "rpid": 111, "text": "笑死，这波操作也太真实了"})
        self.assertTrue(out.startswith("✅"), out)
        action.assert_awaited_once()
        # 不检查 ≠ 不记账：确认发出后仍进「近期自发言」，后续去重照旧生效。
        self.assertIn("笑死，这波操作也太真实了",
                      [row["text"] for row in bili_voice.recent_own(bot)])


class CommentContextTests(VoiceTestBase):
    async def test_draft_fetches_comment_context_on_every_call(self):
        bot = make_bot(self.dir, replies=["这防御也太稳了", "这波手速确实快"])
        ctx = AsyncMock(return_value={"threads": []})
        with patch.object(bili_extra, "comment_context", new=ctx), \
                patch.object(bili_voice, "video_summary",
                             new=AsyncMock(return_value="")):
            first = await bili_auto._draft(bot, "回复", "对方说：稳",
                                           bvid=BVID, kind="reply", max_chars=60)
            second = await bili_auto._draft(bot, "回复", "对方说：稳",
                                            bvid=BVID, kind="reply", max_chars=60)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(ctx.await_count, 2)

    async def test_prompt_injects_persona_summary_context_recent_and_constraints(self):
        bot = make_bot(self.dir, replies=["这防御也太稳了"])
        write_ledger(bot, [own_row("笑死，这个音游防御可还行", ts=RECENT - 10)])
        context = {"threads": [{
            "rpid": 111, "user": "网友A", "like": 3, "ctime": RECENT,
            "time": "2026-11-04 08:00", "content": "这防御稳",
            "mine": False, "replied": False, "replies": [],
        }]}
        with patch.object(bili_extra, "comment_context",
                          new=AsyncMock(return_value=context)), \
                patch.object(bili_voice, "video_summary",
                             new=AsyncMock(return_value="UP 在演示音游防御。")):
            raw = await bili_auto._draft(bot, "回复", "对方说：稳",
                                         bvid=BVID, kind="reply", max_chars=60)
        self.assertTrue(raw)
        system = bot.llm.calls[0][0]["content"]
        user = bot.llm.calls[0][1]["content"]
        self.assertIn("大肥鱼：反应快", system)          # 人设 block
        self.assertIn("本次写作硬约束", system)         # 反公式化约束
        self.assertIn("UP 在演示音游防御。", user)      # 视频摘要
        self.assertIn("网友A", user)                    # 实时评论上下文
        self.assertIn("你最近自己说过的话", user)        # 「别说这些」
        self.assertIn("笑死，这个音游防御可还行", user)

    async def test_live_own_comment_blocks_same_video_repeat_without_local_history(self):
        bot = make_bot(self.dir, replies=["按住鼠标苟活啊", "这鼠标点得比上班还认真"])
        context = {"bvid": BVID, "threads": [{
            "rpid": 111, "user": "大肥鱼", "like": 2, "ctime": RECENT,
            "time": "2026-11-04 08:00", "content": "按住鼠标苟活",
            "mine": True, "replied": False, "replies": [],
        }]}
        with patch.object(bili_extra, "comment_context",
                          new=AsyncMock(return_value=context)), \
                patch.object(bili_voice, "video_summary",
                             new=AsyncMock(return_value="")):
            draft = await bili_auto._draft(bot, "回复", "对方说：稳",
                                           bvid=BVID, kind="reply", max_chars=60)

        self.assertEqual(str(draft), "这鼠标点得比上班还认真")
        self.assertEqual(len(bot.llm.calls), 2)     # 第一句撞同视频历史 → 重写
        self.assertEqual(bili_voice.own_from_context(context)[0]["text"], "按住鼠标苟活")
        self.assertEqual(bili_voice.recent_own(bot, 12), [])   # 但没写本地

    async def test_context_is_fetched_live_every_time_and_never_cached(self):
        make_bot(self.dir)  # 只建 temp 数据根
        top = {"rpid": 111, "like": 7, "ctime": RECENT, "rcount": 1,
               "member": {"mid": "42", "uname": "网友A"},
               "content": {"message": "这游戏真难"}}
        sub = {"rpid": 222, "like": 1, "ctime": RECENT,
               "member": {"mid": "999", "uname": "大肥鱼"},
               "content": {"message": "确实"}}

        class FakeComment:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def get_sub_comments(self, page_index=1, page_size=10):
                return {"replies": [sub]}

        get_comments = AsyncMock(return_value={"replies": [top]})

        class FakeCommentResourceType:
            VIDEO = 1

        fake_comment = SimpleNamespace(
            CommentResourceType=FakeCommentResourceType,
            get_comments=get_comments,
            Comment=FakeComment,
        )
        # 这个用例 mock 了 B 站后端，不需要真的装 bilibili_api：
        # 进程内给 ``from bilibili_api import comment`` 一个假包即可。
        with patch.object(bili_extra, "_cred",
                          return_value=SimpleNamespace(dedeuserid="999")), \
                patch.object(bili_extra, "_aid_of",
                             new=AsyncMock(return_value=123)), \
                patch.dict(sys.modules, {
                    "bilibili_api": SimpleNamespace(comment=fake_comment),
                    "bilibili_api.comment": fake_comment,
                }):
            first = await bili_extra.comment_context(BVID)
            second = await bili_extra.comment_context(BVID)

        self.assertEqual(get_comments.await_count, 2)
        thread = first["threads"][0]
        self.assertEqual(thread["user"], "网友A")
        self.assertEqual(thread["like"], 7)
        self.assertFalse(thread["mine"])
        self.assertTrue(thread["replied"])          # 楼中楼里有凭证账户本人
        self.assertEqual(thread["replies"][0]["user"], "大肥鱼")
        self.assertTrue(thread["replies"][0]["mine"])
        self.assertEqual(second["total_threads"], 1)
        files = [str(path) for path in Path(self.dir).rglob("*") if path.is_file()]
        self.assertEqual(files, [])                 # 评论原文不落盘


class SummaryCacheTests(VoiceTestBase):
    async def test_summary_is_generated_once_then_read_from_cache(self):
        bot = make_bot(self.dir, replies=[
            "这游戏难得很。主播靠背板过关。最后说别轻易入坑。"])
        material = {"title": "测试视频", "desc": "一个音游"}

        first = await bili_voice.video_summary(bot, BVID, material=material)
        self.assertTrue(first)
        self.assertEqual(len(bot.llm.calls), 1)
        path = bili_voice.summary_path(bot, BVID)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(Path(path).parent.name, "bili_summaries")

        second = await bili_voice.video_summary(bot, BVID, material=material)
        self.assertEqual(second, first)
        self.assertEqual(len(bot.llm.calls), 1)     # 第二次不再调模型

    async def test_existing_summary_is_never_overwritten(self):
        bot = make_bot(self.dir, replies=["新写的摘要。第二句。"])
        path = bili_voice.summary_path(bot, BVID)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("手写的旧摘要")

        got = await bili_voice.video_summary(bot, BVID, material={"title": "标题"})
        self.assertEqual(got, "手写的旧摘要")
        self.assertEqual(bot.llm.calls, [])
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "手写的旧摘要")


class NotificationCutoffTests(VoiceTestBase):
    async def test_notifications_before_cutoff_or_without_time_are_skipped(self):
        bot = make_bot(self.dir, {
            "bili_auto_enabled": True, "bili_auto_reply": True,
            "bili_auto_daily_limit": 0, "bili_auto_reply_limit": 0,
        }, replies=["这防御音游还能这么玩"])
        old = notification("old", BVID2, reply_time=bili_voice.cutoff_ts() - 1)
        missing = notification("missing", BVID2)
        missing.pop("reply_time")
        fresh = notification("fresh", BVID, reply_time=bili_voice.cutoff_ts() + 10)
        action = AsyncMock(return_value={"ok": True, "data": {}})

        out = await self._run_with(bot, [old, missing, fresh], action)

        self.assertEqual(sorted(out["skipped_old"]), ["missing", "old"])
        self.assertEqual(len(bot.llm.calls), 1)      # 只给新通知起草
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[2]["bvid"], BVID)
        self.assertEqual(out["replied"], ["这防御音游还能这么玩"])


class AllWritePathsGoThroughCheckTests(VoiceTestBase):
    def _note(self):
        path = os.path.join(self.dir, "note.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 测试视频\n{BVID}\n")
        return [{"path": path}]

    async def test_danmaku_goes_through_check(self):
        bot = make_bot(self.dir, {
            "bili_auto_enabled": True, "bili_auto_reply": False,
            "bili_auto_comment": False, "bili_auto_danmaku": True,
            "bili_auto_daily_limit": 0, "bili_auto_danmaku_limit": 1,
        }, replies=["笑死，这音游", "笑死，绷不住了"])
        action = AsyncMock(return_value={"ok": True, "data": {}})

        patches = self._no_material()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch.object(bili_auto, "_subtitle", new=AsyncMock(return_value="")), \
                patch.object(bili_scroll, "recent_notes", return_value=self._note()), \
                patch.object(bili_scroll, "action", new=action):
            out = await bili_auto.run_round(
                bot, grant_factory=lambda action, target: object())

        action.assert_not_awaited()
        self.assertEqual(out["danmaku"], [])
        self.assertEqual(len(out["voice_rejected"]), 1)
        self.assertEqual(out["voice_rejected"][0]["kind"], "danmaku")
        self.assertEqual(len(bot.llm.calls), 2)

    async def test_top_level_comment_goes_through_check(self):
        bot = make_bot(self.dir, {
            "bili_auto_enabled": True, "bili_auto_reply": False,
            "bili_auto_comment": True, "bili_auto_danmaku": False,
            "bili_auto_daily_limit": 0, "bili_auto_comment_limit": 1,
            "bili_auto_reply_others_chance": 0,
        }, replies=["笑死，这音游", "笑死，绷不住了"])
        action = AsyncMock(return_value={"ok": True, "data": {}})

        patches = self._no_material()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch.object(bili_auto, "_subtitle", new=AsyncMock(return_value="")), \
                patch.object(bili_scroll, "recent_notes", return_value=self._note()), \
                patch.object(bili_scroll, "action", new=action):
            out = await bili_auto.run_round(
                bot, grant_factory=lambda action, target: object())

        action.assert_not_awaited()
        self.assertEqual(out["commented"], [])
        self.assertEqual(len(out["voice_rejected"]), 1)
        self.assertEqual(out["voice_rejected"][0]["kind"], "comment")

    async def test_scroll_autonomous_reply_does_not_bypass_check(self):
        bot = make_bot(self.dir, {"bili_scroll_autoreply": True},
                       replies=["笑死，这音游", "笑死，绷不住了"])
        item = {"bvid": BVID, "title": "测试视频"}
        comments = [{"rpid": 111, "user": "网友", "content": "这关卡也太难了"}]
        action = AsyncMock(return_value={"ok": True, "data": {}})

        patches = self._no_material()
        for item_patch in patches:
            item_patch.start()
            self.addCleanup(item_patch.stop)
        with patch.object(bili_scroll, "action", new=action):
            out = await bili_scroll._autonomous_reply(
                bot, item, comments, grant=object())

        action.assert_not_awaited()
        self.assertFalse(out["ok"])
        self.assertEqual(out["tried"], 1)
        self.assertIn("反公式化", out["items"][0]["reason"])
        self.assertEqual(len(bot.llm.calls), 2)


class SameRoundDedupeTests(VoiceTestBase):
    """同一轮连发：第二条生成时第一条还没落盘，也必须马上去重。"""

    async def test_second_similar_text_in_same_round_is_rejected(self):
        bot = make_bot(self.dir, {
            "bili_auto_enabled": True, "bili_auto_reply": True,
            "bili_auto_daily_limit": 0, "bili_auto_reply_limit": 0,
            "bili_voice_max_retry": 0, "bili_voice_similarity": 0.7,
        }, replies=["这防御也太真实了", "我玩过，这防御也太真实了"])
        items = [notification("n1", BVID), notification("n2", BVID2)]
        action = AsyncMock(return_value={"ok": True, "data": {}})

        out = await self._run_with(bot, items, action)

        # 第一条发出去；第二条和它太像，同一轮里就被拦住，不再烧一次后端。
        action.assert_awaited_once()
        self.assertEqual(out["replied"], ["这防御也太真实了"])
        self.assertEqual(len(out["voice_rejected"]), 1)
        self.assertIn("太像", out["voice_rejected"][0]["reason"])
        # 第一条已经同时进了本进程缓存和动作账本的 voice 桶。
        self.assertIn("这防御也太真实了",
                      [row["text"] for row in bili_voice.recent_own(bot)])
        self.assertIn("这防御也太真实了",
                      [row["text"] for row in
                       (bili_auto._load_seen(bot).get(bili_auto.VOICE_KEY) or [])])


class ModelWrittenToolTextGoesThroughVoiceTests(VoiceTestBase):
    """tools.bili_action 的 reply/dynamic：模型自己写的正文不许绕过检查。"""

    def _toolbox(self, bot):
        tb = object.__new__(tools_mod.ToolBox)
        tb.bot = bot
        # 这里只验证「正文检查 + 记账」；主人/当前请求/污染判定由别的测试锁。
        tb._is_owner = lambda ctx: True
        tb._current_request_text = lambda ctx: "帮我回一条评论，再发个动态"
        tb._external_tainted = lambda ctx: False
        return tb

    async def _run_tool(self, bot, action, args):
        tb = self._toolbox(bot)
        fake = AsyncMock(return_value={"ok": True, "data": {"success": True}})
        with patch.object(tools_mod.bili_gate, "owner_turn_grant",
                          return_value=object()), \
                patch.object(tools_mod.bili_scroll, "action", new=fake):
            out, _ = await tb._t_bili_action(None, action, args)
        return out, fake

    async def test_reply_rejected_by_voice_never_calls_backend(self):
        bot = make_bot(self.dir)
        out, action = await self._run_tool(
            bot, "reply",
            {"bvid": BVID, "rpid": 111, "text": "笑死，这波操作也太真实了"})
        self.assertIn("没过", out)
        self.assertIn("笑死", out)
        action.assert_not_awaited()
        self.assertEqual(bili_voice.recent_own(bot), [])   # 失败不记账

    async def test_dynamic_rejected_by_voice_never_calls_backend(self):
        bot = make_bot(self.dir)
        out, action = await self._run_tool(
            bot, "dynamic",
            {"text": "总的来说，这个视频告诉我们首先要冷静。"})
        self.assertIn("没过", out)
        self.assertIn("AI 腔", out)
        action.assert_not_awaited()
        self.assertEqual(bili_voice.recent_own(bot), [])

    async def test_successful_model_reply_is_recorded_and_visible(self):
        bot = make_bot(self.dir)
        out, action = await self._run_tool(
            bot, "reply",
            {"bvid": BVID, "rpid": 111, "text": "这防御也太稳了吧"})
        self.assertTrue(out.startswith("✅"), out)
        action.assert_awaited_once()
        self.assertIn("这防御也太稳了吧",
                      [row["text"] for row in bili_voice.recent_own(bot)])
        # 同一进程里紧接着再写一句一样的，立刻会被拦（缓存 + 账本都见到了）。
        self.assertTrue(bili_voice.check("这防御也太稳了吧", bot, BVID))

    async def test_successful_dynamic_is_recorded(self):
        bot = make_bot(self.dir)
        out, action = await self._run_tool(
            bot, "dynamic", {"text": "今天这个片子看得挺开心"})
        self.assertTrue(out.startswith("✅"), out)
        action.assert_awaited_once()
        self.assertIn("今天这个片子看得挺开心",
                      [row["text"] for row in bili_voice.recent_own(bot)])


class IntentWriteGoesThroughVoiceTests(VoiceTestBase):
    """自然语言派活：模型成文要过检查；主人原文保留但也要记账。"""

    def _note(self):
        path = os.path.join(self.dir, "note.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 测试视频\n{BVID}\n")
        return [{"path": path}]

    async def test_generated_danmaku_blocked_by_voice_never_sends(self):
        bot = make_bot(self.dir, {"bili_voice_max_retry": 0},
                       replies=["笑死，这波操作"])
        ctx = make_ctx(bot)
        action = AsyncMock(return_value={"ok": True, "data": {}})
        patches = self._no_material()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch.object(bili_scroll, "recent_notes", return_value=self._note()), \
                patch.object(bili_scroll, "action", new=action):
            out = await intent.run(
                bot, {"text": "去发个弹幕", "count": 1,
                      "tasks": [{"action": "danmaku", "count": 1}]}, ctx=ctx)

        res = out["tasks"][0]["result"]
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("voice_rejected"))
        self.assertIn("反公式化", res["reason"])
        action.assert_not_awaited()
        self.assertEqual(bili_voice.recent_own(bot), [])

    async def test_generated_danmaku_that_passes_is_sent_and_recorded(self):
        bot = make_bot(self.dir, {"bili_voice_max_retry": 0},
                       replies=["这波操作有点稳"])
        ctx = make_ctx(bot)
        action = AsyncMock(return_value={"ok": True,
                                         "data": {"success": True, "dmid": "9"}})
        patches = self._no_material()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch.object(bili_scroll, "recent_notes", return_value=self._note()), \
                patch.object(bili_scroll, "action", new=action):
            out = await intent.run(
                bot, {"text": "去发个弹幕", "count": 1,
                      "tasks": [{"action": "danmaku", "count": 1}]}, ctx=ctx)

        res = out["tasks"][0]["result"]
        self.assertTrue(res["ok"], res)
        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[2]["text"], "这波操作有点稳")
        self.assertIn("这波操作有点稳",
                      [row["text"] for row in bili_voice.recent_own(bot)])

    async def test_owner_written_danmaku_is_kept_and_recorded(self):
        bot = make_bot(self.dir)
        ctx = make_ctx(bot)
        action = AsyncMock(return_value={"ok": True,
                                         "data": {"success": True, "dmid": "9"}})
        with patch.object(bili_scroll, "recent_notes", return_value=self._note()), \
                patch.object(bili_scroll, "action", new=action):
            out = await intent.run(
                bot, {"text": "发弹幕 笑死这波", "count": 1,
                      "tasks": [{"action": "danmaku", "count": 1}]}, ctx=ctx)

        res = out["tasks"][0]["result"]
        self.assertTrue(res["ok"], res)
        # 主人给的原文一字不改（即使带「笑死」也不改写/不拦截）。
        self.assertEqual(action.await_args.args[2]["text"], "笑死这波")
        self.assertIn("笑死这波",
                      [row["text"] for row in bili_voice.recent_own(bot)])

    async def test_owner_written_danmaku_records_the_truncated_sent_text(self):
        bot = make_bot(self.dir)
        ctx = make_ctx(bot)
        action = AsyncMock(return_value={"ok": True,
                                         "data": {"success": True, "dmid": "9"}})
        long_text = "这波" + "啊" * 80        # 82 字，超过弹幕 60 字硬上限
        sent = long_text[:60]
        with patch.object(bili_scroll, "recent_notes", return_value=self._note()), \
                patch.object(bili_scroll, "action", new=action):
            out = await intent.run(
                bot, {"text": f"发弹幕 {long_text}", "count": 1,
                      "tasks": [{"action": "danmaku", "count": 1}]}, ctx=ctx)

        res = out["tasks"][0]["result"]
        self.assertTrue(res["ok"], res)
        # 实际发出去的是截断后的那段；返回与记账必须都是同一段，不能记全文。
        self.assertEqual(action.await_args.args[2]["text"], sent)
        self.assertEqual(res["text"], sent)
        self.assertEqual([row["text"] for row in bili_voice.recent_own(bot)], [sent])


class OwnerTypedCommandTextIsRecordedTests(VoiceTestBase):
    """//评论 //发弹幕 是主人手打原文：不改写，成功后进「近期自发言」。"""

    async def _run_cmd(self, cmd, ctx):
        async def fake_bg(ctx_, factory, on_done):
            await factory()
        with patch.object(admin_cmds, "_bg", new=fake_bg):
            await cmd(ctx)

    async def test_owner_comment_text_is_not_rewritten_but_recorded(self):
        bot = make_bot(self.dir, {"bili_write_enabled": True,
                                  "bili_write_comment": True})
        ctx = make_ctx(bot, arg_text=f"{BVID} 笑死，这也太真实了")
        action = AsyncMock(return_value={"ok": True,
                                         "data": {"success": True, "rpid": 1}})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_comment, ctx)

        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[2]["text"], "笑死，这也太真实了")
        self.assertIn("笑死，这也太真实了",
                      [row["text"] for row in bili_voice.recent_own(bot)])

    async def test_owner_danmaku_is_not_rewritten_but_recorded(self):
        bot = make_bot(self.dir, {"bili_write_enabled": True,
                                  "bili_write_danmaku": True})
        ctx = make_ctx(bot, arg_text=f"{BVID} 草，这也行")
        action = AsyncMock(return_value={"ok": True,
                                         "data": {"success": True, "dmid": "9"}})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_danmaku, ctx)

        action.assert_awaited_once()
        self.assertEqual(action.await_args.args[2]["text"], "草，这也行")
        self.assertIn("草，这也行",
                      [row["text"] for row in bili_voice.recent_own(bot)])

    async def test_failed_owner_write_is_not_recorded(self):
        bot = make_bot(self.dir, {"bili_write_enabled": True,
                                  "bili_write_comment": True})
        ctx = make_ctx(bot, arg_text=f"{BVID} 这句没发出去")
        action = AsyncMock(return_value={"ok": False, "error": "B 站没确认"})
        with patch.object(admin_cmds.bili_scroll, "action", new=action):
            await self._run_cmd(admin_cmds.cmd_comment, ctx)

        action.assert_awaited_once()
        self.assertEqual(bili_voice.recent_own(bot), [])


if __name__ == "__main__":
    unittest.main()
