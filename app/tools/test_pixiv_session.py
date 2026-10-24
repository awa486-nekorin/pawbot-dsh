"""Offline contract tests: fake browser targets and raw CDP transport only."""
import json
import re
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, quote, urlsplit

from pawbot import pixiv_session as ps


def artwork(iid, **changes):
    value = {"id": str(iid), "title": "blue sky", "width": 1600, "height": 1200,
             "bookmarkCount": 500, "xRestrict": 0, "userName": "artist",
             "tags": {"tags": [{"tag": "blue sky"}]},
             "urls": {"original": f"https://i.pximg.net/img-original/{iid}.png"}}
    value.update(changes)
    return value


class FakeBrowser:
    def __init__(self):
        self.targets = [{"type": "page", "url": "https://example.com/",
                         "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/other"},
                        {"type": "page", "url": "https://www.pixiv.net/",
                         "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/pixiv"}]
        self.login = {"logged_in": True, "error": ""}
        self.stages, self.details, self.calls, self.urls = [[], [], [], []], {}, [], []
        self.closed = 0
        self.error = None

    async def connect(self, host, port, path):
        assert path == "/devtools/page/pixiv"
        browser = self

        class Connection:
            async def send_text(self, text):
                request = json.loads(text)
                browser.calls.append(request)
                assert request["method"] == "Runtime.evaluate"
                expression = request["params"]["expression"]
                assert "location.origin !== 'https://www.pixiv.net'" in expression
                assert "cookie" not in expression.lower()
                if "fetch(" in expression:
                    url = json.loads(re.search(r'fetch\(("(?:[^"\\]|\\.)*")', expression)[1])
                    browser.urls.append(url)
                    if browser.error:
                        self.reply = {"id": request["id"], "result": {"exceptionDetails": {"text": browser.error}}}
                        return
                    if "/ajax/search/" in url:
                        index = sum("/ajax/search/" in u for u in browser.urls) - 1
                        body = {"illustManga": {"data": browser.stages[index]}}
                    else:
                        body = browser.details[url.rsplit("/", 1)[-1]]
                    value = {"body": body}
                else:
                    assert "meta#meta-global-data" in expression
                    assert "userData" in expression
                    value = browser.login
                self.reply = {"id": request["id"], "result": {"result": {"value": value}}}

            async def recv(self):
                return json.dumps(self.reply)

            async def close(self):
                browser.closed += 1

        return Connection()


class PixivSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser = FakeBrowser()
        self.session = ps.PixivSession()
        for item in (patch.object(ps, "_local_get", side_effect=lambda *a, **k: self.browser.targets),
                     patch.object(ps, "_connect", side_effect=self.browser.connect),
                     patch.object(ps.PixivSession, "chrome_cdp_ready", return_value=True),
                     patch.object(ps.urllib.request.OpenerDirector, "open", side_effect=AssertionError("network")),
                     patch.object(ps.subprocess, "Popen", side_effect=AssertionError("browser")),
                     patch.object(ps.subprocess, "run", side_effect=AssertionError("process"))):
            item.start()
            self.addCleanup(item.stop)

    async def test_status_and_logged_in_are_read_only(self):
        self.assertTrue((await self.session.status())["logged_in"])
        self.browser.login = {"logged_in": False, "error": ""}
        self.assertFalse(await self.session.logged_in())
        self.assertFalse(self.browser.urls)
        self.assertEqual(self.browser.closed, 2)

    async def test_only_exact_pixiv_origin_selected(self):
        valid = self.browser.targets.pop()
        for url in ("https://www.pixiv.net.evil/", "https://pixiv.net/", "http://www.pixiv.net/", "https://www.pixiv.net:444/"):
            self.browser.targets.append(dict(valid, url=url))
        state = await self.session.status()
        self.assertFalse(state["page_on_pixiv"])
        self.assertIn("no_pixiv_page", state["error"])
        self.assertFalse(self.browser.calls)

    async def test_query_two_stages_and_original_metadata(self):
        self.browser.stages[1] = [{"id": "1"}]
        self.browser.details["1"] = artwork(1)
        report = await self.session.search_report("blue sky", popular="1000")
        words = [parse_qs(urlsplit(u).query)["word"][0] for u in self.browser.urls if "/search/" in u]
        self.assertEqual(len(words), 4)          # 两阶段 × 每阶段两页
        self.assertEqual(words[0], "blue sky 1000users入り")   # 完整收藏标签，不是裸数字
        self.assertEqual(words[1], "blue sky 1000users入り")
        self.assertEqual(words[2], "blue sky")
        self.assertEqual(words[3], "blue sky")
        # 热门门槛必须同时在路径里，Pixiv 按路径关键词搜索
        paths = [urlsplit(u).path for u in self.browser.urls if "/search/" in u]
        self.assertTrue(paths[0].endswith("/" + quote("blue sky 1000users入り")))
        self.assertTrue(paths[2].endswith("/blue%20sky"))
        image = report["images"][0]
        self.assertEqual((image["width"], image["height"], image["bookmark_count"]), (1600, 1200, 500))
        self.assertIn("img-original", image["url"])
        self.assertEqual(image["tags"], "blue sky")

    async def test_filters_and_sorting(self):
        details = [artwork(1, xRestrict=1), artwork(2, title="blue sky child"),
                   artwork(3, title="blue sky furry"), artwork(4, bookmarkCount=1),
                   artwork(5, width=100), artwork(6, title="unrelated", tags={}),
                   artwork(7), artwork(8, bookmarkCount=900),
                   artwork(9, bookmarkCount=900, width=2000),
                   artwork(10, title="blue", tags={}, bookmarkCount=9000)]
        self.browser.details = {v["id"]: v for v in details}
        self.browser.stages[0] = [{"id": v["id"]} for v in details]
        report = await self.session.search_report("blue sky", n=4, popular="1000")
        # SFW 结果即使标题沾未成年词也放行（查询层只降级为 SFW，不拒绝）
        self.assertEqual([v["illust_id"] for v in report["images"]], ["9", "8", "2", "7"])
        self.assertEqual(set(report["filtered"]), {"r18", "furry", "bookmarks", "resolution", "relevance"})
        # 合格只有 4 张、不够 n*2 的挑选池，所以会继续第二阶段（不带热门门槛）补候选
        self.assertEqual(len(report["stages"]), 2)

    async def test_global_candidate_cap_and_duplicates(self):
        self.browser.stages[0] = [{"id": str(i)} for i in range(40)] * 2
        self.browser.details = {str(i): artwork(i, bookmarkCount=0) for i in range(40)}
        report = await self.session.search_report("blue sky", candidate_limit=100, popular="1000")
        self.assertEqual(report["candidates"], 30)
        self.assertEqual(sum("/ajax/illust/" in u for u in self.browser.urls), 30)
        self.assertIn("no_matching_images", report["error"])

    async def test_configurable_thresholds_and_search_wrapper(self):
        self.browser.stages[0] = [{"id": "1"}, {"id": "1"}]
        self.browser.details["1"] = artwork(1, width=10, bookmarkCount=1, title="other", tags={})
        images = await self.session.search("blue sky", popular="off", min_side=0,
                                           min_bookmarks=0, min_relevance=0)
        self.assertEqual(len(images), 1)
        self.assertEqual(len([u for u in self.browser.urls if "/search/" in u]), 2)  # 第一页不足才翻第二页

    async def test_api_errors_and_connection_cleanup(self):
        self.browser.error = "http_429"
        report = await self.session.search_report("blue sky", popular="1000")
        self.assertIn("http_429", report["error"])
        self.assertEqual(len(report["errors"]), 2)
        self.assertEqual(self.browser.closed, 3)

    async def test_anonymous_never_searches_and_invalid_request(self):
        self.browser.login = {"logged_in": False, "error": ""}
        self.assertIn("not_logged_in", (await self.session.search_report("blue sky"))["error"])
        self.assertFalse(self.browser.urls)
        self.assertIn("empty_query", (await self.session.search_report(" "))["error"])

    async def test_missing_metadata_and_unavailable_browser(self):
        self.browser.login = {"logged_in": False, "error": "login_metadata_missing"}
        self.assertIn("login_metadata_missing", (await self.session.status())["error"])
        with patch.object(ps.PixivSession, "chrome_cdp_ready", return_value=False):
            self.assertFalse((await self.session.status())["ready"])

    async def test_next_data_login_detection_supported(self):
        await self.session.status()
        expression = self.browser.calls[-1]["params"]["expression"]
        self.assertIn("__NEXT_DATA__", expression)
        self.assertIn("isLoggedIn === true", expression)

    async def test_search_autolaunches_dedicated_chrome(self):
        with patch.object(ps.PixivSession, "chrome_cdp_ready", return_value=False),                 patch.object(ps.PixivSession, "launch_chrome", return_value=True) as launch:
            self.browser.stages[0] = [{"id": "1"}]
            self.browser.details = {"1": artwork(1)}
            await self.session.search_report("blue sky", n=1, popular="off")
            launch.assert_called_once()
        with patch.object(ps.PixivSession, "launch_chrome") as launch:
            await self.session.search_report("blue sky", n=1, popular="off")
            launch.assert_not_called()          # 已经在跑就不重复启动

    async def test_record_views_only_artwork_pages(self):
        self.assertEqual(await self.session.record_views(
            ["http://www.pixiv.net/artworks/1", "https://evil.net/artworks/1",
             "https://www.pixiv.net/artworks/1?x=1", "https://www.pixiv.net/users/1"]), 0)
        self.assertFalse(self.browser.calls)

    async def test_tiers_go_from_high_to_low(self):
        self.assertEqual(ps._tiers("1000,10000 5000"), ["10000", "5000", "1000", ""])
        self.assertEqual(ps._tiers("off"), [""])
        self.assertEqual(ps._tiers("garbage"), [*ps.DEFAULT_TIERS, ""])
        # 高层有结果就不再往低层降
        self.browser.stages[0] = [{"id": str(i)} for i in range(1, 7)]
        self.browser.details = {str(i): artwork(i) for i in range(1, 7)}
        report = await self.session.search_report("blue sky", n=2, popular="10000,1000")
        self.assertEqual([s["popular"] for s in report["stages"]], ["10000"])
        word = parse_qs(urlsplit(self.browser.urls[0]).query)["word"][0]
        self.assertEqual(word, "blue sky 10000users入り")

    async def test_ai_manga_and_small_are_dropped_before_detail_requests(self):
        self.browser.stages[0] = [{"id": "1", "aiType": 2}, {"id": "2", "illustType": 1},
                                  {"id": "3", "illustType": 2},
                                  {"id": "4", "width": 300, "height": 300}, {"id": "5"}]
        self.browser.details = {"5": artwork(5)}
        report = await self.session.search_report("blue sky", n=1, popular="off")
        self.assertEqual([v["illust_id"] for v in report["images"]], ["5"])
        self.assertEqual(report["filtered"], {"ai": 1, "not_illust": 2, "resolution": 1})
        self.assertEqual([u for u in self.browser.urls if "/ajax/illust/" in u],
                         ["https://www.pixiv.net/ajax/illust/5"])

    async def test_ai_allowed_when_disabled(self):
        self.browser.stages[0] = [{"id": "1", "aiType": 2}]
        self.browser.details = {"1": artwork(1, aiType=2)}
        report = await self.session.search_report("blue sky", n=1, popular="off",
                                                  exclude_ai=False)
        self.assertEqual([v["illust_id"] for v in report["images"]], ["1"])

    async def test_bookmark_rate_breaks_ties_between_similar_counts(self):
        self.browser.stages[0] = [{"id": "1"}, {"id": "2"}]
        self.browser.details = {"1": artwork(1, bookmarkCount=1000, viewCount=20000),
                                "2": artwork(2, bookmarkCount=1000, viewCount=4000)}
        report = await self.session.search_report("blue sky", n=2, popular="off")
        self.assertEqual([v["illust_id"] for v in report["images"]], ["2", "1"])

    async def test_detail_error_is_reported(self):
        self.browser.stages[0] = [{"id": "1"}]
        report = await self.session.search_report("blue sky", popular="off")
        self.assertIn("detail 1", report["error"])


if __name__ == "__main__":
    unittest.main()
