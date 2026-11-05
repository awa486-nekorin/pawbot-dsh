"""离线测试：B 站多来源发现 + 刷片管线接入。

平台回包按本机 bilibili_api 的真实解包形态模拟（Api.result 已把 data/result
字段拆出来），只 patch 网络边界，不 mock 归一函数或 run_round 的去重/字幕/
判断/笔记逻辑。全程不读运行态 data，不联网。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot import bili_credentials, bili_discovery, bili_extra, bili_scroll
from pawbot.paths import DataPaths

try:  # 本机 app/.venv 已装；系统解释器没装时只跳过平台回包用例，不拖垮 discovery。
    from bilibili_api import dynamic, favorite_list, homepage, user, video
    _HAS_BILI_API = True
except Exception:  # pragma: no cover - 取决于运行解释器
    dynamic = favorite_list = homepage = user = video = None
    _HAS_BILI_API = False

BVID_R = "BV1xx411c7mD"
BVID_F = "BV1yy411c7mE"
BVID_T = "BV1zz411c7mF"
BVID_V = "BV1aa411c7mG"
BVID_REL = "BV1bb411c7mH"
BVID_S = "BV1cc411c7mJ"


class FakeBot:
    def __init__(self, **config):
        self.config = config


@unittest.skipUnless(_HAS_BILI_API, "本机解释器没装 bilibili_api")
class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, **config):
        return FakeBot(**config)

    def creds(self):
        return patch.object(
            bili_credentials, "load",
            return_value={"sessdata": "fake-sess", "bili_jct": "fake-jct",
                          "dedeuserid": "12345"})

    async def test_recommend_anonymous_normalizes_feed(self):
        raw = {
            "item": [
                {"id": 123456, "title": "推荐标题", "bvid": BVID_R,
                 "owner": {"name": "UP甲"}, "stat": {"view": 12345},
                 "duration": 125, "desc": "推荐简介"},
                {"id": 999, "title": "不是视频的广告"},
            ]
        }
        get_videos = AsyncMock(return_value=raw)
        with patch.object(bili_credentials, "load", return_value={}), \
                patch.object(homepage, "get_videos", new=get_videos):
            out = await bili_discovery.discover(self.bot(), "recommend", limit=5)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["source"], "recommend")
        self.assertEqual(len(out["items"]), 1)
        item = out["items"][0]
        self.assertEqual(item["bvid"], BVID_R)
        self.assertEqual(item["title"], "推荐标题")
        self.assertEqual(item["author"], "UP甲")
        self.assertEqual(item["play"], 12345)
        self.assertEqual(item["duration"], 125)
        self.assertEqual(item["desc"], "推荐简介")
        self.assertEqual(item["url"], f"https://www.bilibili.com/video/{BVID_R}")
        self.assertEqual(item["source"], "recommend")
        # 没登录也能读推荐。
        self.assertIsNone(get_videos.await_args.kwargs["credential"])

    async def test_following_uses_dynamic_video_and_module_author(self):
        raw = {
            "items": [{
                "type": "DYNAMIC_TYPE_AV",
                "modules": {
                    "module_author": {"name": "动态UP"},
                    "module_dynamic": {
                        "major": {"type": "MAJOR_TYPE_ARCHIVE", "archive": {
                            "bvid": BVID_F, "title": "动态视频",
                            "duration_text": "03:21", "desc": "动态简介",
                            "stat": {"play": "987"},
                        }}
                    },
                },
            }]
        }
        page = AsyncMock(return_value=raw)
        with self.creds(), \
                patch.object(dynamic, "get_dynamic_page_info", new=page):
            out = await bili_discovery.discover(self.bot(), "following")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["source"], "following")
        item = out["items"][0]
        self.assertEqual(item["bvid"], BVID_F)
        self.assertEqual(item["author"], "动态UP")
        self.assertEqual(item["duration"], 201)
        self.assertEqual(item["play"], 987)
        self.assertEqual(item["source"], "following")
        self.assertEqual(page.await_args.kwargs["_type"],
                         dynamic.DynamicType.VIDEO)

    async def test_toview_parses_list_and_requires_login(self):
        raw = {"count": 1, "list": [{
            "bvid": BVID_T, "title": "稍后视频",
            "owner": {"name": "UP稍后"}, "stat": {"view": 321},
            "duration": 65, "desc": "稍后简介",
        }]}
        listing = AsyncMock(return_value=raw)
        with self.creds(), \
                patch.object(user, "get_toview_list", new=listing):
            out = await bili_discovery.discover(self.bot(), "toview")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["items"][0]["author"], "UP稍后")
        self.assertEqual(out["items"][0]["source"], "toview")

        listing.reset_mock()
        with patch.object(bili_credentials, "load", return_value={}), \
                patch.object(user, "get_toview_list", new=listing):
            denied = await bili_discovery.discover(self.bot(), "toview")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["items"], [])
        self.assertIn("登录", denied["reason"])
        listing.assert_not_awaited()

    async def test_following_without_credential_has_clear_reason(self):
        call = AsyncMock()
        with patch.object(bili_credentials, "load", return_value={}), \
                patch.object(dynamic, "get_dynamic_page_info", new=call):
            out = await bili_discovery.discover(self.bot(), "following")
        self.assertFalse(out["ok"])
        self.assertIn("关注动态", out["reason"])
        self.assertIn("登录", out["reason"])
        call.assert_not_awaited()

    async def test_favorites_explicit_media_id_skips_list_lookup(self):
        raw = {"medias": [{
            "bvid": BVID_V, "title": "收藏视频",
            "upper": {"name": "UP收藏"},
            "cnt_info": {"play": 456}, "duration": 88, "intro": "收藏简介",
        }]}
        content = AsyncMock(return_value=raw)
        lists = AsyncMock()
        with self.creds(), \
                patch.object(favorite_list, "get_video_favorite_list", new=lists), \
                patch.object(favorite_list, "get_video_favorite_list_content",
                             new=content):
            out = await bili_discovery.discover(
                self.bot(), "favorites", media_id=42)
        self.assertTrue(out["ok"], out)
        item = out["items"][0]
        self.assertEqual((item["author"], item["play"], item["duration"],
                          item["desc"]), ("UP收藏", 456, 88, "收藏简介"))
        lists.assert_not_awaited()
        content.assert_awaited_once_with(media_id=42, page=1, credential=ANY)

    async def test_favorites_auto_selects_existing_folder(self):
        content = AsyncMock(return_value={"medias": [{
            "bvid": BVID_V, "title": "收藏视频",
            "upper": {"name": "UP收藏"}, "cnt_info": {"play": 1},
            "duration": 88,
        }]})
        lists = AsyncMock(return_value={
            "list": [{"id": 7, "title": "默认收藏夹", "media_count": 3}]})
        with self.creds(), \
                patch.object(favorite_list, "get_video_favorite_list", new=lists), \
                patch.object(favorite_list, "get_video_favorite_list_content",
                             new=content):
            out = await bili_discovery.discover(self.bot(), "favorites")
        self.assertTrue(out["ok"], out)
        lists.assert_awaited_once()
        content.assert_awaited_once_with(media_id=7, page=1, credential=ANY)

    async def test_favorites_without_credential_does_not_read_any_folder(self):
        content = AsyncMock()
        with patch.object(bili_credentials, "load", return_value={}), \
                patch.object(favorite_list, "get_video_favorite_list_content",
                             new=content):
            out = await bili_discovery.discover(
                self.bot(), "favorites", media_id=42)
        self.assertFalse(out["ok"])
        self.assertIn("登录", out["reason"])
        content.assert_not_awaited()

    async def test_user_videos_uses_plan_uid_and_vlist_shape(self):
        raw = {"list": {"vlist": [{
            "bvid": BVID_V, "title": "投稿视频", "author": "UP投稿",
            "play": 777, "length": "12:03", "description": "投稿简介",
        }]}}
        made = []

        class FakeUser:
            def __init__(self, uid, credential=None):
                self.uid = uid
                made.append(self)
                self.credential = credential

            async def get_videos(self, **kwargs):
                self.kwargs = kwargs
                return raw

        with self.creds(), patch.object(user, "User", new=FakeUser):
            out = await bili_discovery.discover(
                self.bot(), "user_videos", uid=888, limit=9)
        self.assertTrue(out["ok"], out)
        self.assertEqual(made[0].uid, 888)
        self.assertEqual(made[0].kwargs["ps"], 9)
        item = out["items"][0]
        self.assertEqual(item["author"], "UP投稿")
        self.assertEqual(item["duration"], 723)
        self.assertEqual(item["desc"], "投稿简介")

    async def test_user_videos_falls_back_to_configured_uid(self):
        made = []

        class FakeUser:
            def __init__(self, uid, credential=None):
                self.uid = uid
                made.append(self)

            async def get_videos(self, **kwargs):
                return {"list": {"vlist": []}}

        with patch.object(bili_credentials, "load", return_value={}), \
                patch.object(user, "User", new=FakeUser):
            out = await bili_discovery.discover(
                self.bot(**{"tools.bili_uid": "456"}), "user_videos")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["items"], [])
        self.assertEqual(made[0].uid, 456)

    async def test_user_videos_without_uid_has_clear_reason(self):
        with patch.object(bili_credentials, "load", return_value={}):
            out = await bili_discovery.discover(
                self.bot(), "user_videos", uid=0)
        self.assertFalse(out["ok"])
        self.assertIn("uid", out["reason"])

    async def test_related_parses_list_and_requires_bvid(self):
        raw = [{
            "bvid": BVID_REL, "title": "相关视频",
            "owner": {"name": "相关UP"}, "stat": {"view": 55},
            "duration": 90, "desc": "相关简介",
        }]
        related = AsyncMock(return_value=raw)
        with patch.object(video.Video, "get_related", new=related):
            out = await bili_discovery.discover(
                self.bot(), "related", bvid=BVID_R)
        self.assertTrue(out["ok"], out)
        item = out["items"][0]
        self.assertEqual((item["bvid"], item["author"], item["play"],
                          item["duration"]), (BVID_REL, "相关UP", 55, 90))
        self.assertEqual(item["source"], "related")

        related.reset_mock()
        with patch.object(video.Video, "get_related", new=related):
            denied = await bili_discovery.discover(
                self.bot(), "related", bvid="123456")
        self.assertFalse(denied["ok"])
        self.assertIn("bvid", denied["reason"])
        related.assert_not_awaited()

    async def test_search_parses_vendored_json_and_marks_search(self):
        raw = json.dumps({"videos": [{
            "bvid": BVID_S, "title": "搜索视频", "author": "UP搜索",
            "play": 888, "duration": "01:02", "description": "搜索简介",
        }]}, ensure_ascii=False)
        search = AsyncMock(return_value=raw)
        with patch.object(bili_discovery, "_search_raw", new=search):
            out = await bili_discovery.discover(
                self.bot(), "search", query="冷知识", limit=3)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["source"], "search")
        self.assertEqual(out["items"][0]["source"], "search")
        self.assertEqual(out["items"][0]["duration"], 62)
        search.assert_awaited_once_with("冷知识", 3)

    async def test_empty_result_is_ok_not_failure(self):
        with patch.object(bili_credentials, "load", return_value={}), \
                patch.object(homepage, "get_videos",
                             new=AsyncMock(return_value={"item": []})):
            out = await bili_discovery.discover(self.bot(), "recommend")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["items"], [])
        self.assertTrue(out["reason"])

    async def test_api_failure_is_reported_not_faked(self):
        with patch.object(bili_credentials, "load", return_value={}), \
                patch.object(homepage, "get_videos",
                             new=AsyncMock(side_effect=RuntimeError("boom"))):
            out = await bili_discovery.discover(self.bot(), "recommend")
        self.assertFalse(out["ok"])
        self.assertEqual(out["items"], [])
        self.assertIn("首页推荐读取失败", out["reason"])

    async def test_unknown_source_is_rejected(self):
        out = await bili_discovery.discover(self.bot(), "nope")
        self.assertFalse(out["ok"])
        self.assertIn("不支持的来源", out["reason"])


ITEM = {
    "bvid": BVID_R, "title": "片子甲", "author": "UP甲",
    "play": 123456, "duration": 120, "desc": "简介甲",
    "url": f"https://www.bilibili.com/video/{BVID_R}",
}


class ScrollPipelineTests(unittest.IsolatedAsyncioTestCase):
    """run_round：候选接入、来源保留、字幕级别、旧搜索路径。"""

    def setUp(self):
        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        self.tmp = handle.name

    def _bot(self):
        bot = FakeBot(**{
            "tools.bili_scroll_min_play": 0,
            "tools.bili_scroll_max_duration": 0,
            "tools.bili_learn_after_scroll": False,
        })
        bot.base_dir = self.tmp
        bot.paths = DataPaths(root=self.tmp, base_dir=self.tmp)
        return bot

    def _pipeline(self, *, subtitle=""):
        judge = {"worth": True, "gist": "讲的甲", "points": ["一条"],
                 "why": "值得", "take": "还行"}
        self.search = AsyncMock(return_value=[dict(ITEM)])
        self.discover = AsyncMock(return_value={
            "ok": True, "source": "following", "items": [dict(ITEM)],
            "reason": "",
        })
        self.write_note = Mock(
            return_value=os.path.join(self.tmp, "note.md"))
        patches = [
            patch.object(bili_scroll, "_srv", return_value=SimpleNamespace()),
            patch.object(bili_scroll, "_ensure_credential", new=lambda bot: True),
            patch.object(bili_scroll, "_search", new=self.search),
            patch.object(bili_discovery, "discover", new=self.discover),
            patch.object(bili_scroll, "_subtitle",
                         new=AsyncMock(return_value={
                             "text": subtitle, "status": "ok" if subtitle else "none",
                             "language": "zh-CN" if subtitle else "",
                             "generated": bool(subtitle),
                         })),
            patch.object(bili_scroll, "_comments", new=AsyncMock(return_value=[])),
            patch.object(bili_scroll, "_judge", new=AsyncMock(return_value=judge)),
            patch.object(bili_scroll, "_write_note", new=self.write_note),
            patch.object(bili_scroll, "_load_seen", return_value={}),
            patch.object(bili_scroll, "_save_seen", new=lambda bot, seen: None),
            patch.object(bili_extra, "react", new=AsyncMock(return_value={
                "like": False, "fav": False, "coin": 0, "errors": []})),
        ]
        for ctx in patches:
            ctx.start()
            self.addCleanup(ctx.stop)

    async def test_candidates_skip_search_and_keep_source(self):
        self._pipeline()
        item = dict(ITEM, source="following")
        out = await bili_scroll.run_round(
            self._bot(), want=1, candidates=[item], source="following")
        self.assertTrue(out["ok"], out)
        self.search.assert_not_awaited()
        self.discover.assert_not_awaited()
        note = out["notes"][0]
        self.assertEqual(note["source"], "following")
        self.assertEqual(note["watched_level"], "desc_comments")

    async def test_candidate_own_source_beats_default_search_label(self):
        self._pipeline()
        item = dict(ITEM, source="recommend")
        out = await bili_scroll.run_round(
            self._bot(), want=1, candidates=[item])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["notes"][0]["source"], "recommend")

    async def test_following_source_uses_discover_not_search(self):
        self._pipeline()
        item = dict(ITEM, source="following")
        self.discover.return_value = {
            "ok": True, "source": "following", "items": [item], "reason": ""}
        out = await bili_scroll.run_round(
            self._bot(), want=1, source="following")
        self.assertTrue(out["ok"], out)
        self.search.assert_not_awaited()
        self.discover.assert_awaited_once()
        self.assertEqual(self.discover.await_args.args[1], "following")
        self.assertEqual(out["notes"][0]["source"], "following")

    async def test_discover_failure_returns_reason_without_fake_note(self):
        self._pipeline()
        self.discover.return_value = {
            "ok": False, "source": "toview", "items": [],
            "reason": "稍后再看需要登录 B 站：没有可用凭据",
        }
        out = await bili_scroll.run_round(
            self._bot(), want=1, source="toview")
        self.assertFalse(out["ok"])
        self.assertIn("登录", out["reason"])
        self.assertNotIn("notes", out)
        self.search.assert_not_awaited()
        self.write_note.assert_not_called()

    async def test_subtitle_is_recorded_as_subtitle_level(self):
        self._pipeline(subtitle="字幕原文")
        out = await bili_scroll.run_round(
            self._bot(), want=1,
            candidates=[dict(ITEM, source="toview")], source="toview")
        self.assertTrue(out["ok"], out)
        note = out["notes"][0]
        self.assertEqual(note["watched_level"], "subtitle")
        self.assertEqual(note["subtitle_status"], "ok")
        self.assertGreater(note["subtitle_chars"], 0)
        self.assertEqual(note["source"], "toview")

    async def test_old_search_path_is_unchanged(self):
        self._pipeline()
        out = await bili_scroll.run_round(self._bot(), want=1)
        self.assertTrue(out["ok"], out)
        self.search.assert_awaited()
        self.discover.assert_not_awaited()
        self.assertEqual(out["notes"][0]["source"], "search")


if __name__ == "__main__":
    unittest.main()
