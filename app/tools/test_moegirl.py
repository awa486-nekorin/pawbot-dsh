"""萌娘百科查询：解析、降级与工具接线。全部离线（HTTP 打桩）。"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import moegirl
from pawbot.tools import ToolBox

OPENSEARCH = json.dumps(
    ["寄", ["寄", "寄叶二号B型"], ["", ""],
     ["https://zh.moegirl.org.cn/%E5%AF%84",
      "https://zh.moegirl.org.cn/%E5%AF%84%E5%8F%B6%E4%BA%8C%E5%8F%B7B%E5%9E%8B"]],
    ensure_ascii=False)
EXTRACT = json.dumps(
    {"batchcomplete": "", "query": {"redirects": [{"from": "Yyds", "to": "永远的神"}],
                                    "pages": {"1": {"pageid": 1, "ns": 0, "title": "永远的神",
                                                    "extract": "永远滴神出自英雄联盟S9总决赛。"}}}},
    ensure_ascii=False)


def router(opensearch=OPENSEARCH, extract=EXTRACT):
    def _get(url, timeout=15):
        if "action=opensearch" in url:
            return opensearch
        if "prop=extracts" in url:
            return extract
        return "{}"
    return _get


class LookupTests(unittest.TestCase):
    def test_search_titles_parses_opensearch(self):
        with patch.object(moegirl, "_get", router()):
            hits = moegirl.search_titles("寄")
        self.assertEqual(hits[0]["title"], "寄")
        self.assertTrue(hits[0]["url"].startswith("https://zh.moegirl.org.cn/"))
        self.assertEqual(hits[1]["title"], "寄叶二号B型")

    def test_intro_uses_redirect_target(self):
        with patch.object(moegirl, "_get", router()):
            page = moegirl.intro("Yyds")
        self.assertEqual(page["title"], "永远的神")
        self.assertIn("永远滴神", page["text"])

    def test_lookup_returns_extract_and_alternatives(self):
        with patch.object(moegirl, "_get", router()):
            out = moegirl.lookup("寄", allow_network=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["title"], "永远的神")
        self.assertIn("永远滴神", out["extract"])
        self.assertIn("寄叶二号B型", out["others"])

    def test_lookup_empty_query(self):
        self.assertFalse(moegirl.lookup("  ")["ok"])

    def test_lookup_without_hits(self):
        empty = json.dumps(["abc", [], [], []])
        with patch.object(moegirl, "_get", router(opensearch=empty)):
            out = moegirl.lookup("abc", allow_network=True)
        self.assertFalse(out["ok"])
        self.assertIn("没有这个条目", out["error"])

    def test_lookup_survives_network_error(self):
        def boom(url, timeout=15):
            raise OSError("connection reset")
        with patch.object(moegirl, "_get", boom):
            out = moegirl.lookup("寄", allow_network=True)
        self.assertFalse(out["ok"])
        self.assertIn("OSError", out["error"])

    def test_entry_without_intro_still_returns_link(self):
        with patch.object(moegirl, "_get", router(extract=json.dumps({"query": {"pages": {}}}))):
            out = moegirl.lookup("寄", allow_network=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["extract"], "")
        self.assertTrue(out["url"])


class ValidationTests(unittest.TestCase):
    def test_malformed_search_shapes_are_ignored(self):
        for value in ({}, ["q", "not a list", [], []],
                      ["q", [None, {}, 7, "x\x00y"], [], []]):
            with self.subTest(value=value), patch.object(moegirl, "_api", return_value=value):
                self.assertEqual(moegirl.search_titles("测试"), [])

    def test_untrusted_urls_are_not_forwarded(self):
        value = ["q", ["测试", "测试"], [], ["javascript:alert(1)", "https://evil.test"]]
        with patch.object(moegirl, "_api", return_value=value):
            hits = moegirl.search_titles("测试")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["url"], moegirl._page_url("测试"))

    def test_malformed_extract_shapes_are_ignored(self):
        for value in ([], {"query": []}, {"query": {"pages": []}},
                      {"query": {"pages": {"1": {"extract": {"text": "bad"}}}}},
                      {"query": {"pages": {"1": None}}}):
            with self.subTest(value=value), patch.object(moegirl, "_api", return_value=value):
                self.assertEqual(moegirl.intro("测试"), {})

    def test_extract_bound_and_redirect_source_are_preserved(self):
        with patch.object(moegirl, "_get", router()):
            out = moegirl.lookup("yyds", chars=4, allow_network=True)
        self.assertEqual(len(out["extract"]), 4)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["origin"], "moegirl")
        self.assertEqual(out["url"], moegirl._page_url(out["title"]))

    def test_invalid_query_never_fetches(self):
        with patch.object(moegirl, "_api") as api:
            for query in (None, {}, 7, "x" * 201):
                self.assertFalse(moegirl.lookup(query)["ok"])
            api.assert_not_called()

    def test_local_first_lookup_never_fetches_or_mutates(self):
        import tempfile
        from pawbot.memes import MemeBook
        with tempfile.TemporaryDirectory() as tmp:
            book = MemeBook(str(Path(tmp, "memes.json")))
            book.learn("测试梗", ["测试梗"], "离线解释")
            before = Path(book.path).read_bytes()
            with patch.object(moegirl, "_get", side_effect=AssertionError("network")):
                out = moegirl.lookup("测试梗", book=book)
                miss = moegirl.lookup("另一个梗", book=book, allow_network=False)
            self.assertTrue(out["ok"])
            self.assertEqual(out["origin"], "local")
            self.assertEqual(out["source"], "群友")
            self.assertEqual(out["url"], "")
            self.assertFalse(miss["ok"])
            self.assertEqual(Path(book.path).read_bytes(), before)

    def test_http_response_size_is_bounded(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"x" * (moegirl.MAX_RESPONSE_BYTES + 1)
        with patch.object(moegirl.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(ValueError):
                moegirl._get(moegirl.API)
        response.read.assert_called_once_with(moegirl.MAX_RESPONSE_BYTES + 1)


class ToolWiringTests(unittest.IsolatedAsyncioTestCase):
    def box(self):
        bot = SimpleNamespace(config={}, qzone=SimpleNamespace(enabled=False))
        return ToolBox(bot)

    def test_tool_is_offered_to_the_model(self):
        names = [s["function"]["name"] for s in self.box().schemas()]
        self.assertIn("moegirl", names)

    async def test_tool_output_tells_it_not_to_recite(self):
        box = self.box()
        with patch.object(moegirl, "lookup",
                          return_value={"ok": True, "title": "永远的神",
                                        "url": "https://zh.moegirl.org.cn/Yyds",
                                        "extract": "永远滴神出自英雄联盟S9总决赛。",
                                        "others": ["Uzi"], "error": ""}):
            text, media = await box.execute(None, "moegirl", {"query": "yyds"})
        self.assertIn("永远的神", text)
        self.assertIn("别复述", text)
        self.assertEqual(media, [])

    async def test_tool_says_so_when_nothing_found(self):
        box = self.box()
        with patch.object(moegirl, "lookup",
                          return_value={"ok": False, "error": "萌娘百科没有这个条目"}):
            text, _ = await box.execute(None, "moegirl", {"query": "瞎编的词"})
        self.assertIn("没查到", text)
        self.assertIn("别硬猜", text)


class PromptRuleTests(unittest.TestCase):
    def test_persona_tells_it_to_check_moegirl(self):
        text = Path(__file__).resolve().parents[1].joinpath("data", "persona.md").read_text(encoding="utf-8")
        # v3 + 本地知识改造：人设只许她查「本地梗库」，并明确 moegirl 不联网；
        # 不许再写「先查萌娘百科」那种会把聊天路径带向联网的说法。
        self.assertIn("本地梗库", text)
        self.assertIn("moegirl", text)
        self.assertIn("不联网", text)
        self.assertNotIn("先查萌娘百科", text)

    def test_agent_hint_mentions_moegirl(self):
        src = Path(__file__).resolve().parents[1].joinpath("pawbot", "agent.py").read_text(encoding="utf-8")
        self.assertIn("moegirl", src)
        self.assertIn("萌娘百科", src)


if __name__ == "__main__":
    unittest.main()
