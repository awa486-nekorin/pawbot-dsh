"""离线测试：模型把工具调用写成 DSML 文本时要解析成真调用，且不能漏到聊天里。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.llm import _finalize, _strip_dsml

SAMPLE = '''<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="search_image">
<｜｜DSML｜｜ parameter name="n" string="false">3</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="query" string="true">青髪 女の子</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="source" string="true">pixiv</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="tags" string="false">["青髪"]</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>'''


class DSMLTests(unittest.TestCase):
    def test_full_block_becomes_tool_call(self):
        message = _finalize({"role": "assistant", "content": SAMPLE})
        self.assertEqual(message["content"], "")
        call = message["tool_calls"][0]["function"]
        self.assertEqual(call["name"], "search_image")
        self.assertEqual(json.loads(call["arguments"]),
                         {"n": 3, "query": "青髪 女の子",
                          "source": "pixiv", "tags": ["青髪"]})

    def test_text_around_block_survives_without_markup(self):
        message = _finalize({"role": "assistant",
                             "content": "好啊，我找一张。\n" + SAMPLE + "\n稍等"})
        self.assertEqual(message["content"], "好啊，我找一张。\n\n稍等")
        self.assertEqual(len(message["tool_calls"]), 1)

    def test_bare_invoke_is_removed_entirely(self):
        content = ('我先看看。<|DSML|invoke name="account_action">'
                   '<|DSML|parameter name="operation" string="true">avatar</|DSML|parameter>'
                   '</|DSML|invoke>')
        message = _finalize({"role": "assistant", "content": content})
        self.assertEqual(message["content"], "我先看看。")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]),
                         {"operation": "avatar"})

    def test_structured_tool_calls_are_untouched(self):
        original = [{"id": "x", "type": "function",
                     "function": {"name": "ping", "arguments": "{}"}}]
        message = _finalize({"role": "assistant", "content": "", "tool_calls": original})
        self.assertEqual(message["tool_calls"], original)

    def test_reasoning_content_is_preserved_for_thinking_models(self):
        message = _finalize({"role": "assistant", "content": "",
                             "reasoning_content": "想一想"})
        self.assertEqual(message["reasoning_content"], "想一想")

    def test_plain_text_unchanged(self):
        self.assertEqual(_finalize({"role": "assistant", "content": "普通回复，没有标记"})["content"],
                         "普通回复，没有标记")

    def test_unclosed_markup_never_leaks(self):
        for text in ("<|DSML|calls><|DSML|invoke name=\"x\">",
                     "</|DSML|parameter> 尾巴",
                     "<||DSML|| calls> 只有开头"):
            with self.subTest(text=text):
                self.assertNotIn("DSML", _strip_dsml(text))


if __name__ == "__main__":
    unittest.main()
