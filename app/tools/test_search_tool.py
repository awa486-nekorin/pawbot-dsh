"""搜索工具候选重试、动态标签和过滤的离线测试。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot.tools import ToolBox


class SearchToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(config={}, spice=SimpleNamespace(get=lambda sid: 0), llm=object())
        self.toolbox = ToolBox(self.bot)
        self.ctx = SimpleNamespace(session_id='group_77', raw_text='找张pixiv上的风景插画',
                                   event=SimpleNamespace(plain='找张pixiv上的风景插画'),
                                   state={})

    async def test_dynamic_tags_and_next_candidate_after_download_failure(self):
        items = [{'url': 'https://example.com/a', 'rating': 'sfw', 'title': '一'},
                 {'url': 'https://example.com/b', 'rating': 'sfw', 'title': '二',
                  'page': 'https://www.pixiv.net/artworks/2', 'width': 1200, 'height': 1600,
                  'bookmark_count': 1000}]
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 16
        segment = {'type': 'text', 'data': {'text': '[IMG:https://example.com/b]'}}
        with patch('pawbot.tools.imagesearch.image_search', return_value={'images': items, 'provider': 'pixiv(浏览器)'}) as search, \
             patch('pawbot.tools.imagesearch.download', side_effect=[None, png]) as download:
            text, media = await self.toolbox._t_search_image(
                self.ctx, '风景插画', tags=['風景', '青空'], source='pixiv')
        # pixiv 是标签 AND 搜索，只传标签；中文描述放进去会搜不到
        self.assertEqual(search.call_args.args[0], '風景 青空')
        self.assertEqual(search.call_args.args[3], 'pixiv')
        self.assertGreater(search.call_args.args[2], 1)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(media, [segment])
        self.assertIn('artworks/2', text)
        self.assertNotIn('无标题', text)

    async def test_original_furry_request_cannot_be_laundered_through_tags(self):
        self.ctx.event.plain = '搜福瑞图片'
        with patch('pawbot.tools.imagesearch.image_search') as search:
            text, media = await self.toolbox._t_search_image(
                self.ctx, '插画', source='pixiv', tags=['風景'])
        search.assert_not_called()
        self.assertEqual(media, [])
        self.assertIn('福瑞', text)

    async def test_session_error_is_visible_and_not_replaced(self):
        with patch('pawbot.tools.imagesearch.image_search', return_value={
                'images': [], 'error': '未登录，请 /pixiv登录'}) as search:
            text, media = await self.toolbox._t_search_image(self.ctx, '風景', source='pixiv')
        self.assertIn('未登录', text)
        self.assertEqual(media, [])
        self.assertEqual(search.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class LocalSearchTests(unittest.TestCase):
    """360 结果解析与 local 路由：全部离线打桩。"""

    PAGE = (
        '<html><body>'
        '<li class="res-list"><h3 class="res-title ">'
        '<a href="https://www.so.com/link?m=abc" data-mdurl="https://news.example.com/a">'
        '<em>科技</em>新闻一</a></h3>'
        '<p class="res-desc"><span>6天前 - </span>第一条摘要</p></li>'
        '<li class="res-list"><h3 class="res-title ">'
        '<a href="https://www.so.com/link?m=def" data-mdurl="https://news.example.com/b">'
        '新闻二</a></h3><p class="res-desc">第二条摘要</p></li>'
        '</body></html>'
    )

    def test_parses_360_results_with_real_urls(self):
        from pawbot import websearch
        with patch.object(websearch, "_open", return_value=self.PAGE):
            out = websearch.search_360("科技新闻", 5)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "科技新闻一")       # em 标签不留空格
        self.assertEqual(out[0]["url"], "https://news.example.com/a")
        self.assertIn("第一条摘要", out[0]["snippet"])

    def test_provider_local_never_touches_an_llm(self):
        from pawbot import websearch
        with patch.object(websearch, "search_local",
                          return_value=[{"title": "t", "url": "u", "snippet": "s"}]) as local, \
             patch.object(websearch, "search_deepseek") as llm:
            out = websearch.search("科技新闻", {"tools.search_provider": "local"})
        local.assert_called_once()
        llm.assert_not_called()
        self.assertEqual(out["results"][0]["title"], "t")

    def test_local_falls_back_to_next_engine(self):
        from pawbot import websearch
        with patch.object(websearch, "search_360", return_value=[]), \
             patch.object(websearch, "search_bing",
                          return_value=[{"title": "b", "url": "u", "snippet": ""}]):
            out = websearch.search_local("x", 3)
        self.assertEqual(out[0]["title"], "b")

    def test_search_model_default_is_flash(self):
        from pawbot import websearch
        import inspect
        sig = inspect.signature(websearch.search_deepseek)
        self.assertIn("flash", sig.parameters["model"].default)


class OfficialDeepSeekTests(unittest.TestCase):
    """官方 /v1/tools/search 与 /v1/tools/fetch：不经模型。"""

    PAYLOAD = {"results": [
        {"rerank_score": 2.4, "title": "新闻一", "url": "https://a.example/1",
         "snippet": "摘要一", "summary": "长摘要", "site_name": "站点",
         "date": "2026-10-20T16:00:00+00:00", "authority": 4},
        {"title": "", "url": "https://a.example/2", "snippet": "",
         "site_name": "站点二", "authority": -1},
        {"title": "没有链接", "snippet": "x"},
    ]}

    def test_maps_results_and_drops_entries_without_url(self):
        from pawbot import websearch
        with patch.object(websearch, "_post_json", return_value=self.PAYLOAD) as post:
            out = websearch.deepseek_search("新闻", "key", count=5)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "新闻一")
        self.assertEqual(out[0]["authority"], 4)
        self.assertEqual(out[0]["date"][:10], "2026-10-20")
        self.assertEqual(out[1]["title"], "站点二")      # 没标题用站点名
        self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/v1/tools/search")
        self.assertNotIn("need_content", post.call_args.args[1])

    def test_count_is_clamped(self):
        from pawbot import websearch
        with patch.object(websearch, "_post_json", return_value={"results": []}) as post:
            websearch.deepseek_search("x", "key", count=999)
        self.assertEqual(post.call_args.args[1]["count"], 50)

    def test_provider_deepseek_uses_official_search(self):
        from pawbot import websearch
        cfg = {"tools.search_provider": "deepseek", "tools.search_api_key": "key"}
        with patch.object(websearch, "deepseek_search",
                          return_value=[{"title": "t", "url": "u", "snippet": "s"}]) as official, \
             patch.object(websearch, "search_deepseek") as llm:
            out = websearch.search("x", cfg)
        official.assert_called_once()
        llm.assert_not_called()
        self.assertEqual(out["provider"], "deepseek/search")

    def test_official_failure_falls_back_to_local(self):
        from pawbot import websearch
        cfg = {"tools.search_provider": "deepseek", "tools.search_api_key": "key"}
        with patch.object(websearch, "deepseek_search", side_effect=OSError("boom")), \
             patch.object(websearch, "search_local",
                          return_value=[{"title": "local", "url": "u", "snippet": ""}]):
            out = websearch.search("x", cfg)
        self.assertEqual(out["provider"], "deepseek→local(兜底)")
        self.assertEqual(out["results"][0]["title"], "local")

    def test_fetch_smart_prefers_official_then_local(self):
        from pawbot import websearch
        cfg = {"tools.search_api_key": "key"}
        with patch.object(websearch, "deepseek_fetch",
                          return_value={"ok": True, "title": "T", "text": "正文"}) as official, \
             patch.object(websearch, "fetch") as local:
            out = websearch.fetch_smart("https://a.example", cfg)
        official.assert_called_once()
        local.assert_not_called()
        self.assertEqual(out["title"], "T")

        with patch.object(websearch, "deepseek_fetch",
                          return_value={"ok": False, "error": "HTTP 404"}), \
             patch.object(websearch, "fetch",
                          return_value={"ok": True, "title": "L", "text": "本地"}):
            out = websearch.fetch_smart("https://a.example", cfg)
        self.assertEqual(out["title"], "L")

    def test_fetch_smart_without_key_uses_local(self):
        from pawbot import websearch
        with patch.object(websearch, "deepseek_fetch") as official, \
             patch.object(websearch, "fetch",
                          return_value={"ok": True, "title": "L", "text": "x"}):
            websearch.fetch_smart("https://a.example", {})
        official.assert_not_called()
