"""Pixiv 主页/排行榜来源、画质评判、空查询路由、[IMG:] 标记和按情绪挑表情包。全部离线。"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import imagesearch
from pawbot import pixiv_session as ps
from pawbot.stickers import StickerBook
from pawbot.tools import ToolBox

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def detail(iid, **changes):
    value = {"id": str(iid), "title": f"作品{iid}", "width": 2000, "height": 1500,
             "bookmarkCount": 3000, "viewCount": 20000, "xRestrict": 0, "aiType": 1,
             "userName": "画师", "tags": {"tags": [{"tag": "オリジナル"}]},
             "urls": {"original": f"https://i.pximg.net/img-original/{iid}.png"}}
    value.update(changes)
    return value


def ranking_item(iid, **changes):
    value = {"illust_id": iid, "illust_type": "0", "width": 2000, "height": 1500,
             "url": f"https://i.pximg.net/c/480x960/{iid}.jpg", "user_name": "画师", "rank": iid,
             "illust_content_type": {"furry": False, "lo": False}}
    value.update(changes)
    return value


class HomeReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = ps.PixivSession()
        self.details = {}
        self.raw = {}
        for item in (patch.object(ps.PixivSession, "status", AsyncMock(return_value={
                         "ready": True, "page_on_pixiv": True, "logged_in": True, "error": ""})),
                     patch.object(ps.PixivSession, "_fetch_raw", side_effect=self._raw),
                     patch.object(ps.PixivSession, "_fetch_json", side_effect=self._json)):
            item.start()
            self.addCleanup(item.stop)

    async def _raw(self, url):
        for key, value in self.raw.items():
            if key in url:
                return value
        raise RuntimeError("http_404")

    async def _json(self, url):
        return self.details[url.rsplit("/", 1)[-1]]

    async def test_ranking_top_n_then_filters(self):
        items = [ranking_item(i) for i in range(1, 31)]
        items[0]["illust_content_type"]["furry"] = True          # Pixiv 自带的福瑞标记
        items[1]["width"] = 400                                   # 低清
        items[2]["illust_type"] = "1"                             # 漫画
        self.raw["ranking.php"] = {"contents": items}
        self.details = {str(i): detail(i) for i in range(1, 31)}
        self.details["4"] = detail(4, bookmarkCount=10)           # 收藏太少
        self.details["5"] = detail(5, aiType=2)                   # AI 生成
        report = await self.session.home_report(3, source="daily", top=20, variety=False)
        ids = [x["illust_id"] for x in report["images"]]
        self.assertEqual(len(ids), 3)
        self.assertTrue(set(ids).isdisjoint({"1", "2", "3", "4", "5"}))
        self.assertTrue(all(int(i) <= 20 for i in ids))           # 只看前 20
        self.assertEqual(report["filtered"], {"furry": 1, "resolution": 1, "not_illust": 1,
                                              "bookmarks": 1, "ai": 1})
        self.assertEqual(report["query"], "今日排行")
        self.assertTrue(all(x["thumb"].startswith("https://i.pximg.net/c/480x960/") for x in report["images"]))

    async def test_recommend_source_uses_thumbnails(self):
        thumbs = [{"id": str(i), "illustType": 0, "aiType": 1, "width": 1800, "height": 1800,
                   "url": f"https://i.pximg.net/c/250x250/{i}.jpg"} for i in (7, 8, 9)]
        self.raw["top/illust"] = {"body": {"thumbnails": {"illust": thumbs},
                                           "page": {"recommend": {"ids": ["8", "9", "404"]}}}}
        self.details = {str(i): detail(i) for i in (7, 8, 9)}
        report = await self.session.home_report(2, source="recommend", variety=False)
        self.assertEqual(sorted(x["illust_id"] for x in report["images"]), ["8", "9"])
        self.assertEqual(report["query"], "主页推荐")

    async def test_not_logged_in_reports_error(self):
        ps.PixivSession.status.return_value = {"ready": True, "page_on_pixiv": True,
                                               "logged_in": False, "error": ""}
        report = await self.session.home_report(2, source="daily")
        self.assertIn("not_logged_in", report["error"])
        self.assertEqual(report["images"], [])


class JudgeImagesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.items = [{"thumb": f"https://i.pximg.net/t/{i}.jpg", "page": f"p{i}"} for i in range(4)]
        patch.object(imagesearch, "download", return_value=PNG).start()
        self.addCleanup(patch.stopall)

    async def test_low_scores_dropped_and_sorted(self):
        # 画质评判现在走看图通道（_vision_judge_sync），不走 judge.*
        scores = iter([(8, ""), (3, "崩坏"), (9, ""), None])
        with patch.object(imagesearch, "_vision_judge_sync",
                          side_effect=lambda *a, **k: next(scores)):
            kept = await imagesearch.judge_images(list(self.items), {})
        self.assertEqual([x["page"] for x in kept], ["p2", "p0", "p3"])  # 3 分淘汰；没打分的保留
        self.assertEqual(kept[0]["judge_score"], 9)

    async def test_judge_unavailable_keeps_metadata_order(self):
        with patch.object(imagesearch, "_vision_judge_sync", return_value=None):
            kept = await imagesearch.judge_images(list(self.items), {})
        self.assertEqual(kept, self.items)

    async def test_disabled_by_config(self):
        with patch("pawbot.judge.judge_json", AsyncMock()) as judge:
            kept = await imagesearch.judge_images(list(self.items), _Cfg({"tools.image_judge": False}))
        judge.assert_not_called()
        self.assertEqual(kept, self.items)


class _Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class RoutingTests(unittest.TestCase):
    def test_empty_query_routes_to_home(self):
        with patch.object(ps.PixivSession, "home_report", AsyncMock(return_value={
                "provider": "pixiv(浏览器)", "images": [], "error": ""})) as home, \
                patch.object(ps.PixivSession, "search_report", AsyncMock()) as search:
            imagesearch.image_search("", _Cfg(), 2, "auto")
        home.assert_awaited_once()
        search.assert_not_called()

    def test_tag_query_uses_search_sorted_by_tiers(self):
        with patch.object(ps.PixivSession, "search_report", AsyncMock(return_value={
                "provider": "pixiv(浏览器)", "images": [], "error": ""})) as search:
            imagesearch.image_search("青髪 女の子", _Cfg(), 2, "pixiv")
        kwargs = search.await_args.kwargs
        self.assertEqual(kwargs["popular"], "10000,5000,1000")
        self.assertGreaterEqual(kwargs["n"], 4)                  # 多拿几张给评判挑


class AutoLaunchTests(unittest.TestCase):
    def test_missing_browser_is_reported_after_failed_launch(self):
        with patch.object(ps.PixivSession, "chrome_cdp_ready", return_value=False),                 patch.object(ps.PixivSession, "launch_chrome", return_value=False):
            out = imagesearch.image_search("风景", _Cfg(), 1, "pixiv")
        self.assertEqual(out["images"], [])
        self.assertIn("专用 Chrome", out["error"])


class ToolMarkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = _Cfg({"persona.stickers_dir": self.tmp.name, "persona.sticker_fetch": False})
        self.stickers = StickerBook(self.tmp.name, config)
        self.bot = SimpleNamespace(config=config, spice=SimpleNamespace(get=lambda sid: 0),
                                   llm=object(), stickers=self.stickers,
                                   emotion=SimpleNamespace(blend=lambda: {"dominant": "烦躁"}))
        self.toolbox = ToolBox(self.bot)
        self.ctx = SimpleNamespace(session_id="group_1", raw_text="来点图", state={},
                                   event=SimpleNamespace(plain="来点图"))

    async def test_search_image_returns_img_markers_not_raw_segments(self):
        items = [{"url": "https://i.pximg.net/img-original/1.png", "need_referer": True,
                  "referer": imagesearch.PIXIV_REFERER, "rating": "sfw", "source": "pixiv",
                  "page": "https://www.pixiv.net/artworks/1", "title": "一"}]
        with patch.object(imagesearch, "image_search", return_value={"images": items, "provider": "pixiv"}) as search, \
                patch.object(imagesearch, "download", return_value=PNG):
            text, segs = await self.toolbox._t_search_image(self.ctx)
        self.assertEqual(search.call_args.args[3], "home")          # 没主题 → 主页
        self.assertEqual(segs, [{"type": "text", "data": {"text": "[IMG:https://i.pximg.net/img-original/1.png]"}}])
        self.assertIn("单独一条消息", text)

    async def test_failed_download_tries_next_url_then_skips(self):
        items = [{"url": "https://a/1.png", "url_candidates": ["https://a/1-large.png"], "rating": "sfw"}]
        with patch.object(imagesearch, "image_search", return_value={"images": items, "provider": "x"}), \
                patch.object(imagesearch, "download", side_effect=[None, PNG]):
            _text, segs = await self.toolbox._t_search_image(self.ctx, "猫")
        self.assertEqual(segs[0]["data"]["text"], "[IMG:https://a/1-large.png]")

    async def test_pick_sticker_follows_mood_and_returns_marker(self):
        self.stickers.save(PNG, "无语猫", tags=["无语"])
        self.stickers.save(PNG.replace(b"0", b"1"), "开心狗", tags=["开心"])
        # 默认 chance 只有 0.12 会随机拒发，这里把骰子固定成「放行」
        with patch("pawbot.media.random.random", return_value=0.0):
            _text, segs = await self.toolbox._t_pick_sticker(self.ctx)   # 不给 query，按「烦躁」挑
        marker = segs[0]["data"]["text"]
        self.assertTrue(marker.startswith("[STICKER:file:///"))
        self.assertIn("%E6%97%A0%E8%AF%AD%E7%8C%AB", marker)          # 文件名「无语猫」（URI 编码）
        self.assertEqual(self.stickers.index()["无语猫.png"]["uses"], 1)

    async def test_pick_sticker_respects_pacing(self):
        self.stickers.save(PNG, "无语猫", tags=["无语"])
        # 「烦躁」会触发情绪绕闸（发癫可以主动发），这条测的是随机闸，换成不绕闸的情绪
        self.bot.emotion = SimpleNamespace(
            blend=lambda: {"dominant": "惊讶", "intensity": "轻微"})
        ledger = SimpleNamespace(sticker_allowed=lambda sid: False)
        with patch("pawbot.media.ledger_for", return_value=ledger, create=True):
            text, segs = await self.toolbox._t_pick_sticker(self.ctx, "无语")
        self.assertEqual(segs, [])
        self.assertIn("别发", text)

    async def test_preference_pool_prefers_used_stickers(self):
        self.stickers.save(PNG, "常用", tags=["开心"])
        self.stickers.save(PNG.replace(b"0", b"2"), "冷门", tags=["开心"])
        idx = self.stickers.index()
        idx["常用.png"]["uses"] = 20
        self.stickers.save_index(idx)
        picks = [os.path.basename(self.stickers.pick("开心")) for _ in range(200)]
        self.assertGreater(picks.count("常用.png"), picks.count("冷门.png") * 2)


if __name__ == "__main__":
    unittest.main()
