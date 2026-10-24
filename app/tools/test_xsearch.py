"""X 工具 + Pixiv(DeepSeek) 路线的离线测试。全部打桩，不联网。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import imagesearch, websearch
from pawbot.tools import SCHEMAS, ToolBox


class Cfg:
    """按点号键取值的小配置，和 Config.get 的用法一致。"""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)


DS = {"tools.search_api_key": "key", "tools.search_base_url": "https://api.deepseek.com"}


def hit(url, **kw):
    row = {"title": "", "url": url, "snippet": "", "summary": "",
           "date": "", "content": "", "site": "", "score": None, "authority": None}
    row.update(kw)
    return row


def pixiv_item(url="https://i.pximg.net/img-original/a.png", **kw):
    row = {"url": url, "thumb": url, "title": "作品", "author": "画师",
           "tags": "オリジナル", "rating": "sfw", "source": "pixiv",
           "page": "https://www.pixiv.net/artworks/1", "need_referer": True,
           "referer": imagesearch.PIXIV_REFERER}
    row.update(kw)
    return row


class XSearchTests(unittest.TestCase):
    def test_posts_keep_status_links_normalize_and_dedupe(self):
        def fake(term, key, base, **kw):
            if "twitter.com" in term:
                return [hit("https://twitter.com/Baz/status/789", summary="三"),
                        hit("https://x.com/Foo/status/123?s=20", summary="重复")]
            return [hit("https://x.com/Foo/status/123", summary="一", date="2026-10-20T10:00:00Z"),
                    hit("https://twitter.com/Bar/status/456", snippet="二"),
                    hit("https://x.com/Foo", title="主页不是帖子"),
                    hit("https://x.com/Foo/status/123", summary="重复")]

        with patch.object(websearch, "deepseek_search", side_effect=fake) as ds:
            rows = websearch.x_search("DeepSeek", Cfg(DS))
        self.assertEqual([r["url"] for r in rows],
                         ["https://x.com/Foo/status/123",
                          "https://x.com/Bar/status/456",
                          "https://x.com/Baz/status/789"])
        self.assertEqual([r["author"] for r in rows], ["@Foo", "@Bar", "@Baz"])
        self.assertEqual(rows[0]["text"], "一")
        self.assertEqual(rows[1]["text"], "二")          # summary 空时用 snippet
        self.assertEqual(rows[0]["date"][:10], "2026-10-20")
        self.assertEqual(ds.call_count, 2)
        self.assertIn("site:x.com", ds.call_args_list[0].args[0])
        self.assertIn("site:twitter.com", ds.call_args_list[1].args[0])

    def test_people_only_single_segment_profiles_and_no_reserved(self):
        rows_in = [
            hit("https://x.com/alice", title="Alice (@alice) / X", summary="画师"),
            hit("https://twitter.com/bob", title="Bob (@bob) / X", snippet="简介"),
            hit("https://x.com/search?q=123", title="搜索"),
            hit("https://x.com/alice/status/1", title="帖子"),
            hit("https://x.com/hashtag/tag", title="话题"),
            hit("https://x.com/i", title="i"),
            hit("https://x.com/explore", title="探索"),
            hit("https://x.com/intent/follow", title="关注"),
            hit("https://x.com/share", title="分享"),
            hit("https://x.com/status/1", title="状态页"),
            hit("https://twitter.com/alice", title="Alice 重复"),
        ]
        with patch.object(websearch, "deepseek_search", return_value=rows_in) as ds:
            rows = websearch.x_search("alice", Cfg(DS), kind="people")
        self.assertEqual([r["url"] for r in rows],
                         ["https://x.com/alice", "https://x.com/bob"])
        self.assertEqual(rows[0]["handle"], "@alice")
        self.assertEqual(rows[0]["name"], "Alice (@alice) / X")
        self.assertEqual(rows[0]["bio"], "画师")
        self.assertEqual(rows[1]["bio"], "简介")
        self.assertEqual(ds.call_count, 1)

    def test_count_is_applied(self):
        with patch.object(websearch, "deepseek_search",
                          return_value=[hit("https://x.com/a/status/1"),
                                        hit("https://x.com/b/status/2")]):
            rows = websearch.x_search("x", Cfg(DS), count=1)
        self.assertEqual(len(rows), 1)

    def test_no_api_key_returns_empty(self):
        with patch.object(websearch, "deepseek_search") as ds:
            self.assertEqual(websearch.x_search("x", Cfg({})), [])
        ds.assert_not_called()


class XReadTests(unittest.TestCase):
    def test_rejects_non_x_links(self):
        with patch.object(websearch, "deepseek_fetch") as fetch:
            out = websearch.x_read("https://example.com/status/1", Cfg(DS))
        self.assertEqual(out, {"error": "只能读 x.com 的链接"})
        fetch.assert_not_called()

    def test_twitter_link_is_normalized_and_truncated(self):
        with patch.object(websearch, "deepseek_fetch",
                          return_value={"ok": True, "title": "标题", "text": "字" * 5000}) as fetch:
            out = websearch.x_read("https://twitter.com/Foo/status/123", Cfg(DS))
        self.assertEqual(fetch.call_args.args[0], "https://x.com/Foo/status/123")
        self.assertEqual(len(out["text"]), 3000)
        self.assertEqual(out["url"], "https://x.com/Foo/status/123")
        self.assertEqual(out["title"], "标题")

    def test_fetch_failure_returns_error(self):
        with patch.object(websearch, "deepseek_fetch",
                          return_value={"ok": False, "error": "HTTP 404"}):
            out = websearch.x_read("https://x.com/Foo/status/123", Cfg(DS))
        self.assertEqual(out, {"error": "HTTP 404"})


class PixivDeepSeekTests(unittest.TestCase):
    def test_extracts_ids_dedupes_and_drops_r18_or_urlless(self):
        results = [
            hit("https://www.pixiv.net/artworks/111"),
            hit("https://www.pixiv.net/en/artworks/222"),
            hit("https://www.pixiv.net/artworks/111"),       # 重复
            hit("https://www.pixiv.net/artworks/333"),       # 详情没 url
            hit("https://www.pixiv.net/artworks/444"),       # R-18
            hit("https://www.pixiv.net/artworks/"),          # 没 ID
            hit("https://example.com/artworks/999"),         # 不是 pixiv
        ]
        details = {
            "111": {"title": "一", "author": "A", "url": "https://i.pximg.net/1.png",
                    "tags": ["オリジナル", "女の子"], "xRestrict": 0},
            "222": {"title": "二", "author": "B", "url": "https://i.pximg.net/2.png",
                    "tags": [], "xRestrict": 0},
            "333": {"title": "三", "author": "C", "url": "", "tags": [], "xRestrict": 0},
            "444": {"title": "四", "author": "D", "url": "https://i.pximg.net/4.png",
                    "tags": [], "xRestrict": 1},
            "999": {"title": "九", "author": "E", "url": "https://i.pximg.net/9.png",
                    "tags": [], "xRestrict": 0},
        }
        with patch.object(imagesearch.websearch, "deepseek_search",
                          return_value=results) as ds, \
                patch.object(imagesearch, "pixiv_detail",
                             side_effect=lambda iid, *a: details.get(iid, {})) as detail:
            out = imagesearch.search_pixiv_ds("初音ミク", Cfg(DS), 4)
        self.assertEqual([x["page"] for x in out],
                         ["https://www.pixiv.net/artworks/111",
                          "https://www.pixiv.net/artworks/222"])
        self.assertEqual(ds.call_args.args[0], "site:pixiv.net/artworks 初音ミク")
        self.assertEqual(ds.call_args.kwargs["count"], 12)   # min(20, 4*3)
        self.assertEqual(detail.call_count, 4)               # 999 没被请求
        first = out[0]
        self.assertEqual(first["thumb"], "https://i.pximg.net/1.png")
        self.assertEqual(first["tags"], "オリジナル 女の子")
        self.assertEqual(first["rating"], "sfw")
        self.assertTrue(first["need_referer"])
        self.assertEqual(first["referer"], imagesearch.PIXIV_REFERER)

    def test_stops_at_n(self):
        results = [hit(f"https://www.pixiv.net/artworks/{i}") for i in (1, 2, 3)]
        with patch.object(imagesearch.websearch, "deepseek_search", return_value=results), \
                patch.object(imagesearch, "pixiv_detail",
                             side_effect=lambda iid, *a: {"url": f"https://i.pximg.net/{iid}.png",
                                                      "title": iid, "author": "",
                                                      "tags": [], "xRestrict": 0}):
            out = imagesearch.search_pixiv_ds("x", Cfg(DS), 2)
        self.assertEqual(len(out), 2)

    def test_empty_api_key_returns_empty_without_search(self):
        with patch.object(imagesearch.websearch, "deepseek_search") as ds:
            self.assertEqual(imagesearch.search_pixiv_ds("x", Cfg({}), 4), [])
        ds.assert_not_called()

    def test_detail_exception_is_skipped(self):
        results = [hit("https://www.pixiv.net/artworks/1"),
                   hit("https://www.pixiv.net/artworks/2")]

        def boom(iid, *args):
            if iid == "1":
                raise RuntimeError("boom")
            return {"url": "https://i.pximg.net/2.png", "title": "二", "author": "",
                    "tags": [], "xRestrict": 0}

        with patch.object(imagesearch.websearch, "deepseek_search", return_value=results), \
                patch.object(imagesearch, "pixiv_detail", side_effect=boom):
            out = imagesearch.search_pixiv_ds("x", Cfg(DS), 4)
        self.assertEqual([x["page"] for x in out], ["https://www.pixiv.net/artworks/2"])


class PixivRoutingTests(unittest.TestCase):
    def cfg(self, **extra):
        data = {"tools.pixiv_use_browser": False, "tools.image_judge": False}
        data.update(extra)
        return Cfg(data)

    def test_pixiv_source_uses_public_search(self):
        item = pixiv_item()
        with patch.object(imagesearch, "search_pixiv_public", return_value=[item]) as pub:
            out = imagesearch.image_search("风景", self.cfg(), 2, "pixiv")
        self.assertEqual(out["provider"], "pixiv(公开)")
        self.assertEqual(out["images"], [item])
        self.assertEqual(out["error"], "")
        self.assertEqual(pub.call_args.args[0], "风景")
        self.assertGreaterEqual(pub.call_args.args[2], 2)

    def test_falls_back_to_ds_when_public_returns_nothing(self):
        with patch.object(imagesearch, "search_pixiv_public", return_value=[]), \
                patch.object(imagesearch, "search_pixiv_ds",
                             return_value=[pixiv_item()]) as ds:
            out = imagesearch.image_search("风景", self.cfg(), 2, "pixiv")
        self.assertEqual(out["provider"], "pixiv(公开)")
        self.assertEqual(len(out["images"]), 1)
        ds.assert_called_once()

    def test_home_uses_configured_home_query(self):
        with patch.object(imagesearch, "search_pixiv_public",
                          return_value=[pixiv_item()]) as pub:
            out = imagesearch.image_search("", self.cfg(
                **{"tools.pixiv_home_query": "自定义主页词"}), 2, "home")
        self.assertEqual(pub.call_args.args[0], "自定义主页词")
        self.assertTrue(pub.call_args.kwargs.get("home"))
        self.assertEqual(out["provider"], "pixiv(公开)")

    def test_home_query_has_default(self):
        with patch.object(imagesearch, "search_pixiv_public",
                          return_value=[pixiv_item()]) as pub:
            imagesearch.image_search("", self.cfg(), 2, "home")
        self.assertEqual(pub.call_args.args[0], imagesearch.PIXIV_HOME_QUERY)

    def test_furry_results_are_still_filtered(self):
        furry = pixiv_item(tags="furry オリジナル", title="兽人")
        clean = pixiv_item(url="https://i.pximg.net/img-original/b.png", tags="風景")
        with patch.object(imagesearch, "search_pixiv_public", return_value=[furry, clean]):
            out = imagesearch.image_search("風景", self.cfg(), 2, "pixiv")
        self.assertEqual(out["images"], [clean])

    def test_empty_result_reports_error_not_exception(self):
        with patch.object(imagesearch, "search_pixiv_public", return_value=[]), \
                patch.object(imagesearch, "search_pixiv_ds", return_value=[]):
            out = imagesearch.image_search("風景", self.cfg(), 2, "pixiv")
        self.assertEqual(out["images"], [])
        self.assertIn("pixiv", out["provider"])
        self.assertTrue(out["error"])


class ToolBoxTests(unittest.IsolatedAsyncioTestCase):
    def box(self, cfg=None):
        bot = SimpleNamespace(config=cfg or Cfg({}), qzone=SimpleNamespace(enabled=False))
        return ToolBox(bot)

    def test_x_tools_are_available_by_default(self):
        names = self.box().available()
        self.assertIn("x_search", names)
        self.assertIn("x_read", names)
        self.assertIn("x_search", SCHEMAS)
        self.assertIn("x_read", SCHEMAS)

    def test_x_tools_can_be_switched_off(self):
        box = self.box(Cfg({"tools.x_enabled": False}))
        self.assertNotIn("x_search", box.available())
        self.assertNotIn("x_read", box.available())
        names = [s["function"]["name"] for s in box.schemas()]
        self.assertNotIn("x_search", names)

    async def test_posts_are_formatted_as_numbered_list(self):
        rows = [{"url": "https://x.com/foo/status/1", "author": "@foo",
                 "text": "字" * 200, "date": "2026-10-20T00:00:00Z", "title": "t"},
                {"url": "https://x.com/bar/status/2", "author": "@bar",
                 "text": "短", "date": "", "title": "t"}]
        with patch.object(websearch, "x_search", return_value=rows) as search:
            text, media = await self.box()._t_x_search(None, "DeepSeek")
        self.assertEqual(media, [])
        self.assertIn("1. @foo 2026-10-20：", text)
        self.assertIn("字" * 120, text)
        self.assertNotIn("字" * 121, text)
        self.assertIn("https://x.com/foo/status/1", text)
        self.assertIn("2. @bar：短", text)          # 没日期就不留空档
        self.assertEqual(search.call_args.kwargs["kind"], "posts")

    async def test_people_are_formatted_with_handle_and_bio(self):
        rows = [{"url": "https://x.com/alice", "handle": "@alice",
                 "name": "Alice", "bio": "简" * 100}]
        with patch.object(websearch, "x_search", return_value=rows) as search:
            text, _ = await self.box()._t_x_search(None, "alice", kind="people")
        self.assertIn("1. Alice @alice：", text)
        self.assertIn("简" * 80, text)
        self.assertNotIn("简" * 81, text)
        self.assertIn("https://x.com/alice", text)
        self.assertEqual(search.call_args.kwargs["kind"], "people")

    async def test_no_results_message(self):
        with patch.object(websearch, "x_search", return_value=[]):
            text, media = await self.box()._t_x_search(None, "什么也没有")
        self.assertEqual(text, "X 上没搜到相关内容")
        self.assertEqual(media, [])

    async def test_x_read_formats_and_reports_errors(self):
        with patch.object(websearch, "x_read",
                          return_value={"ok": True, "url": "https://x.com/a/status/1",
                                        "title": "帖子", "text": "正文"}):
            text, _ = await self.box()._t_x_read(None, "https://x.com/a/status/1")
        self.assertIn("《帖子》https://x.com/a/status/1", text)
        self.assertIn("正文", text)
        with patch.object(websearch, "x_read", return_value={"error": "只能读 x.com 的链接"}):
            text, _ = await self.box()._t_x_read(None, "https://example.com")
        self.assertIn("只能读 x.com 的链接", text)


if __name__ == "__main__":
    unittest.main()
