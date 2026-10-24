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
            out = moegirl.lookup("寄")
        self.assertTrue(out["ok"])
        self.assertEqual(out["title"], "永远的神")
        self.assertIn("永远滴神", out["extract"])
        self.assertIn("寄叶二号B型", out["others"])

    def test_lookup_empty_query(self):
        self.assertFalse(moegirl.lookup("  ")["ok"])

    def test_lookup_without_hits(self):
        empty = json.dumps(["abc", [], [], []])
        with patch.object(moegirl, "_get", router(opensearch=empty)):
            out = moegirl.lookup("abc")
        self.assertFalse(out["ok"])
        self.assertIn("没有这个条目", out["error"])

    def test_lookup_survives_network_error(self):
        def boom(url, timeout=15):
            raise OSError("connection reset")
        with patch.object(moegirl, "_get", boom):
            out = moegirl.lookup("寄")
        self.assertFalse(out["ok"])
        self.assertIn("OSError", out["error"])

    def test_entry_without_intro_still_returns_link(self):
        with patch.object(moegirl, "_get", router(extract=json.dumps({"query": {"pages": {}}}))):
            out = moegirl.lookup("寄")
        self.assertTrue(out["ok"])
        self.assertEqual(out["extract"], "")
        self.assertTrue(out["url"])


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
        self.assertIn("萌娘百科", text)
        self.assertIn("moegirl", text)

    def test_agent_hint_mentions_moegirl(self):
        src = Path(__file__).resolve().parents[1].joinpath("pawbot", "agent.py").read_text(encoding="utf-8")
        self.assertIn("moegirl", src)
        self.assertIn("萌娘百科", src)


if __name__ == "__main__":
    unittest.main()
