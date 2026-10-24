"""梗 / 黑话知识库：匹配、注入、学习、落盘、agent/turns/工具接线。全部离线。"""
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

from pawbot import turns
from pawbot.agent import Agent
from pawbot.memes import HEADER, MAX_DETAIL, MemeBook
from pawbot.store import Store
from pawbot.tools import SCHEMAS, ToolBox


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def temp_book(**items):
    tmp = tempfile.mkdtemp()
    book = MemeBook(os.path.join(tmp, "memes.json"))
    for name, triggers, meaning, usage in items.get("rows", []):
        assert book.learn(name, triggers, meaning, usage)
    return book


class MatchTests(unittest.TestCase):
    def test_english_trigger_is_whole_word_only(self):
        book = temp_book(rows=[("曼巴", ["man"], "科比的绰号", "自嘲硬撑")])
        self.assertEqual(book.match("他在 manage 这个项目"), [])   # manager 里的 man
        self.assertEqual(book.match("that is human nature"), [])    # human 里的 man
        hit = book.match("what can i say man")
        self.assertEqual([x["name"] for x in hit], ["曼巴"])
        self.assertEqual(book.match("MAN"), [x for x in book.match("MAN") if x["name"] == "曼巴"])
        self.assertTrue(book.match("MAN"))

    def test_chinese_trigger_is_substring(self):
        book = temp_book(rows=[("牢大", ["牢大"], "科比的黑称", "慎用")])
        hit = book.match("牢大今天又活了")
        self.assertEqual(hit[0]["name"], "牢大")
        self.assertIn("牢大", hit[0]["matched"])

    def test_single_char_trigger_is_ignored(self):
        book = temp_book(rows=[("典", ["典中典", "太典了"], "经典中的经典", "")])
        self.assertEqual([x["name"] for x in book.match("太典了")], ["典"])
        hit = book.match("典中典")
        self.assertNotIn("典", hit[0]["matched"])          # 单字不参与匹配
        self.assertFalse(book.learn("孝", ["孝"], "反讽的孝", ""))
        self.assertEqual(book.match("孝"), [])

    def test_mixed_trigger_is_substring(self):
        book = temp_book(rows=[("city不city", ["city不city"], "洋气不洋气", "")])
        self.assertEqual([x["name"] for x in book.match("这穿搭city不city")],
                         ["city不city"])

    def test_sorted_by_trigger_length_then_hits(self):
        book = temp_book(rows=[
            ("只因", ["只因"], "蔡徐坤空耳", ""),
            ("只因你太美", ["只因你太美"], "完整歌词", ""),
            ("曼巴精神", ["曼巴精神"], "永不言弃", ""),
            ("曼巴运算", ["曼巴运算"], "围绕科比的运算梗", ""),
        ])
        names = [x["name"] for x in book.match("只因你太美，曼巴精神和曼巴运算")]
        self.assertEqual(names[0], "只因你太美")            # 命中词最长
        self.assertLess(names.index("曼巴精神"), names.index("只因"))
        # 同长度时 hits 高的优先
        book.bump("曼巴运算")
        names = [x["name"] for x in book.match("曼巴精神和曼巴运算")]
        self.assertEqual(names[0], "曼巴运算")

    def test_matches_are_deduplicated(self):
        book = temp_book(rows=[("哈基米", ["哈基米", "哈基米南北绿豆"], "猫", "")])
        hits = book.match("哈基米南北绿豆哈基米")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["name"], "哈基米")

    def test_blank_text_matches_nothing(self):
        book = temp_book(rows=[("哈基米", ["哈基米"], "猫", "")])
        self.assertEqual(book.match(""), [])
        self.assertEqual(book.match("   \n"), [])


class InjectTests(unittest.TestCase):
    def test_disabled_book_never_injects(self):
        tmp = tempfile.mkdtemp()
        book = MemeBook(os.path.join(tmp, "memes.json"),
                        Cfg({"memes.enabled": False}))
        book.learn("牢大", ["牢大"], "科比的黑称", "慎用")
        self.assertEqual(book.inject_for(["牢大来了"]), "")

    def test_no_hit_returns_empty_string(self):
        book = temp_book(rows=[("哈基米", ["哈基米"], "猫", "")])
        self.assertEqual(book.inject_for(["今天吃什么", "随便"]), "")
        self.assertEqual(book.items[0]["hits"], 0)

    def test_hit_builds_header_and_bullets(self):
        book = temp_book(rows=[("牢大", ["牢大"], "科比的黑称", "慎用，容易冒犯人")])
        hint = book.inject_for(["牢大怎么来的", "别乱说"])
        self.assertTrue(hint.startswith(HEADER))
        self.assertIn("- 牢大：科比的黑称", hint)
        self.assertIn("用法：慎用", hint)
        self.assertEqual(book.items[0]["hits"], 1)

    def test_meaning_and_usage_are_capped(self):
        book = temp_book(rows=[("长梗", ["长梗"], "意" * 300, "用" * 300)])
        hint = book.inject_for(["长梗"])
        bullet = hint.splitlines()[1]
        detail = bullet.split("：", 1)[1]
        self.assertLessEqual(len(detail), MAX_DETAIL)
        self.assertNotIn("用" * (MAX_DETAIL + 1), hint)

    def test_limit_keeps_only_top_n(self):
        book = temp_book(rows=[
            ("哈基米", ["哈基米"], "猫", ""),
            ("赢麻了", ["赢麻了"], "赢到麻木", ""),
            ("破防", ["破防"], "心理防线被击穿", ""),
            ("典中典", ["典中典"], "经典中的经典", ""),
        ])
        hint = book.inject_for(["哈基米赢麻了破防了典中典"], 2)
        self.assertEqual(len(hint.splitlines()), 3)          # 标题 + 2 条
        self.assertEqual(book.items[0]["hits"] + book.items[1]["hits"]
                         + book.items[2]["hits"] + book.items[3]["hits"], 2)

    def test_accepts_a_plain_string(self):
        book = temp_book(rows=[("哈基米", ["哈基米"], "猫", "")])
        self.assertIn("哈基米", book.inject_for("哈基米"))


class LearnTests(unittest.TestCase):
    def test_new_meme_is_added_and_persisted(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "memes.json")
        book = MemeBook(path)
        self.assertTrue(book.learn("哈基米", ["哈基米"],
                                   "《赛马娘》蜂蜜之歌", "喊猫"))
        again = MemeBook(path)
        self.assertEqual(again.items[0]["name"], "哈基米")
        self.assertEqual(again.items[0]["source"], "群友")
        self.assertEqual(again.items[0]["learned_from"], "")

    def test_same_name_merges_triggers_and_keeps_longer_meaning(self):
        book = temp_book(rows=[("牢大", ["牢大"], "短解释", "旧用法")])
        book.bump("牢大")
        self.assertTrue(book.learn("牢大", ["科比", "牢大"],
                                   "这是一个更长的解释，说明牢X的来源。", "慎用"))
        self.assertEqual(len(book.items), 1)
        item = book.items[0]
        self.assertEqual({t.casefold() for t in item["triggers"]},
                         {"牢大", "科比"})
        self.assertIn("更长的解释", item["meaning"])
        self.assertEqual(item["usage"], "旧用法")             # 旧的更长/同长时保留
        self.assertEqual(item["hits"], 1)                    # hits 不被覆盖

    def test_trigger_overlap_merges_into_existing_name(self):
        book = temp_book(rows=[("哈基米", ["哈基米"], "猫", "")])
        self.assertTrue(book.learn("蜂蜜之歌", ["哈基米", "哈基米南北绿豆"],
                                   "《赛马娘》里东海帝皇哼的蜂蜜之歌，后来指猫。", ""))
        self.assertEqual(len(book.items), 1)
        self.assertEqual(book.items[0]["name"], "哈基米")
        self.assertIn("哈基米南北绿豆", book.items[0]["triggers"])

    def test_name_merge_is_case_insensitive(self):
        book = temp_book(rows=[("ikun", ["ikun"], "蔡徐坤粉丝", "")])
        book.learn("IKUN", ["爱坤"], "蔡徐坤粉丝的称呼，谐音爱坤。", "")
        self.assertEqual(len(book.items), 1)
        self.assertIn("爱坤", book.items[0]["triggers"])

    def test_invalid_input_returns_false(self):
        book = temp_book()
        self.assertFalse(book.learn("", ["哈基米"], "猫", ""))
        self.assertFalse(book.learn("哈基米", ["哈基米"], "", ""))
        self.assertFalse(book.learn("哈基米", ["猫"], "猫", ""))   # 单字全被过滤

    def test_learned_from_is_kept(self):
        book = temp_book()
        book.learn("哈基米", ["哈基米"], "猫", "", source="群友",
                   learned_from="group_123")
        self.assertEqual(book.items[0]["learned_from"], "group_123")
        book.learn("哈基米", ["哈基米南北绿豆"], "更长的解释，蜂蜜之歌。", "",
                   learned_from="group_456")
        self.assertEqual(book.items[0]["learned_from"], "group_123")


class PersistenceTests(unittest.TestCase):
    def test_bump_is_dirty_until_flush(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "memes.json")
        book = MemeBook(path)
        book.learn("哈基米", ["哈基米"], "猫", "")
        self.assertEqual(MemeBook(path).items[0]["hits"], 0)
        self.assertTrue(book.bump("哈基米"))
        self.assertTrue(book.bump("哈基米"))
        self.assertFalse(book.bump("不存在"))
        self.assertEqual(MemeBook(path).items[0]["hits"], 0)   # 还没落盘
        self.assertTrue(book.flush())
        self.assertEqual(MemeBook(path).items[0]["hits"], 2)
        self.assertFalse(book.flush())                         # 没改动

    def test_corrupt_file_is_treated_as_empty(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "memes.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        book = MemeBook(path)
        self.assertEqual(book.items, [])
        self.assertTrue(book.learn("哈基米", ["哈基米"], "猫", ""))
        self.assertEqual(len(MemeBook(path).items), 1)

    def test_wrong_shape_is_treated_as_empty(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "memes.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"items": "oops"}, f)
        self.assertEqual(MemeBook(path).items, [])

    def test_file_layout(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "memes.json")
        book = MemeBook(path)
        book.learn("哈基米", ["哈基米"], "猫", "喊猫")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIsInstance(data["items"], list)
        row = data["items"][0]
        for key in ("name", "triggers", "meaning", "usage", "source",
                    "learned_from", "ts", "hits"):
            self.assertIn(key, row)


def make_agent_bot(book, store):
    async def chat_message(messages, tools=None):
        chat_message.seen = messages
        return {"content": "嗯"}

    chat_message.seen = None
    bot = SimpleNamespace(
        config=Cfg({"tools.enabled": False, "topic.enabled": False,
                    "chat.context_messages": 300, "chat.context_chars": 12000}),
        store=store,
        memes=book,
        persona=SimpleNamespace(build=Mock(return_value="persona")),
        toolbox=SimpleNamespace(schemas=Mock(return_value=[]), execute=AsyncMock()),
        llm=SimpleNamespace(chat_message=chat_message),
        stickers=SimpleNamespace(files=Mock(return_value=[])),
        topic=SimpleNamespace(), mood=SimpleNamespace(hint=lambda: ""),
    )
    agent = Agent(bot)
    agent._sense_hint = lambda ctx: ""
    agent._mood_hint = lambda ctx: ""
    agent._spice_hint = lambda ctx: ""
    agent._tool_hint = lambda on: ""
    return agent, chat_message


def agent_ctx(text):
    return SimpleNamespace(group_id=1, sender_name="阿明", session_id="group_1",
                           is_group=True, event=SimpleNamespace(plain=text),
                           state={}, matched_command=None)


class AgentInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, book, text):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "s.json"))
        store.append("group_1", "user", text, who="阿明")
        agent, chat_message = make_agent_bot(book, store)
        await agent.run(agent_ctx(text), text, store.history("group_1")[:-1])
        return chat_message.seen

    async def test_meme_hint_appears_only_on_trigger(self):
        book = temp_book(rows=[("牢大", ["牢大"], "科比的黑称", "慎用")])
        msgs = await self._run(book, "牢大这梗哪来的")
        hints = [m for m in msgs if m["role"] == "system"
                 and "可能用到的梗" in str(m.get("content") or "")]
        self.assertEqual(len(hints), 1)
        self.assertIn("- 牢大：", hints[0]["content"])
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertLess(msgs.index(hints[0]), len(msgs) - 1)

    async def test_no_hint_when_nothing_matches(self):
        book = temp_book(rows=[("牢大", ["牢大"], "科比的黑称", "慎用")])
        msgs = await self._run(book, "今天晚饭吃什么")
        self.assertFalse([m for m in msgs
                          if "可能用到的梗" in str(m.get("content") or "")])

    async def test_recent_rows_also_trigger(self):
        book = temp_book(rows=[("哈基米", ["哈基米"], "猫", "")])
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "s.json"))
        store.append("group_1", "user", "哈基米哈基米", who="阿明")
        store.append("group_1", "user", "这是啥意思", who="阿明")
        agent, chat_message = make_agent_bot(book, store)
        await agent.run(agent_ctx("这是啥意思"), "这是啥意思",
                        store.history("group_1")[:-1])
        self.assertIn("哈基米", "\n".join(
            str(m.get("content") or "") for m in chat_message.seen
            if m["role"] == "system"))


def make_turn_bot(book):
    tmp = tempfile.mkdtemp()
    store = Store(os.path.join(tmp, "s.json"), max_messages=300)
    return SimpleNamespace(config=Cfg({"chat.turn_summary": True}), store=store,
                           memes=book,
                           llm=SimpleNamespace(chat=AsyncMock(return_value="主模型小结")))


class TurnsLearnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.book = temp_book()
        self.bot = make_turn_bot(self.book)
        self.bot.store.append("group_1", "user", "哈基米是猫的意思", who="阿明",
                              ts=time.time())

    async def test_judge_memes_are_saved(self):
        verdict = {"summary": "阿明解释了哈基米。",
                   "memes": [{"name": "哈基米", "triggers": ["哈基米"],
                              "meaning": "《赛马娘》蜂蜜之歌，后来指猫。"}]}
        with patch.object(turns.judge, "judge_json", AsyncMock(return_value=verdict)) as judge:
            await turns.update(self.bot, "group_1")
        self.assertEqual(len(self.book.items), 1)
        item = self.book.items[0]
        self.assertEqual(item["name"], "哈基米")
        self.assertEqual(item["source"], "群友")
        self.assertEqual(item["learned_from"], "group_1")
        self.assertIn("memes", judge.await_args.args[1])

    async def test_old_judge_format_without_memes_does_not_crash(self):
        with patch.object(turns.judge, "judge_json",
                          AsyncMock(return_value={"summary": "闲聊。"})):
            out = await turns.update(self.bot, "group_1")
        self.assertEqual(out, "闲聊。")
        self.assertEqual(self.book.items, [])

    async def test_malformed_memes_field_is_ignored(self):
        with patch.object(turns.judge, "judge_json",
                          AsyncMock(return_value={"summary": "闲聊。", "memes": "oops"})):
            self.assertEqual(await turns.update(self.bot, "group_1"), "闲聊。")
        self.assertEqual(self.book.items, [])


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def box(self, book=None):
        bot = SimpleNamespace(config={}, qzone=SimpleNamespace(enabled=False),
                              memes=book)
        return ToolBox(bot)

    def test_tool_is_offered_and_has_schema(self):
        book = temp_book()
        names = [s["function"]["name"] for s in self.box(book).schemas()]
        self.assertIn("learn_meme", names)
        props = SCHEMAS["learn_meme"]["function"]["parameters"]["properties"]
        self.assertEqual(set(props), {"name", "triggers", "meaning", "usage"})
        self.assertIn("learn_meme", SCHEMAS["moegirl"]["function"]["description"])

    async def test_tool_saves_meme(self):
        book = temp_book()
        box = self.box(book)
        ctx = SimpleNamespace(session_id="group_9")
        text, media = await box.execute(ctx, "learn_meme", {
            "name": "哈基米", "triggers": ["哈基米"],
            "meaning": "《赛马娘》蜂蜜之歌，后来指猫。", "usage": "喊猫"})
        self.assertIn("记住了", text)
        self.assertEqual(media, [])
        self.assertEqual(book.items[0]["learned_from"], "group_9")
        self.assertIn("哈基米", book.inject_for(["哈基米"]))

    async def test_tool_rejects_useless_triggers(self):
        book = temp_book()
        box = self.box(book)
        text, _ = await box.execute(None, "learn_meme", {
            "name": "孝", "triggers": ["孝"], "meaning": "反讽"})
        self.assertIn("记不下来", text)
        self.assertEqual(book.items, [])

    def test_tool_is_hidden_without_a_book(self):
        names = [s["function"]["name"] for s in self.box().schemas()]
        self.assertNotIn("learn_meme", names)


class SeedDataTests(unittest.TestCase):
    PATH = Path(__file__).resolve().parents[1].joinpath("data", "memes.json")

    @unittest.skipUnless(PATH.exists(), "还没跑 tools/seed_memes.py")
    def test_seeded_book_is_usable(self):
        data = json.loads(self.PATH.read_text(encoding="utf-8"))
        rows = data["items"]
        self.assertGreaterEqual(len(rows), 25)
        names = {row["name"] for row in rows}
        for name in ("牢大", "曼巴", "坤坤", "ikun", "小黑子", "鸡你太美",
                     "只因", "练习时长两年半", "你干嘛哎哟", "尊嘟假嘟",
                     "绝绝子", "yyds", "破防", "下头", "泰裤辣", "依托答辩",
                     "遥遥领先", "city不city", "哈基米", "电子榨菜", "显眼包",
                     "我嘞个豆", "赢麻了", "典", "孝"):
            self.assertIn(name, names)
        for row in rows:
            self.assertTrue(row["meaning"].strip(), row["name"])
            self.assertTrue(row["triggers"], row["name"])
            # 预置条目有网页出处；从群友那里学来的条目 source 写「群友」
            self.assertTrue(row["source"].startswith("http") or row["source"] == "群友",
                            row["name"])
            self.assertTrue(all(len(t) >= 2 for t in row["triggers"]), row["name"])
        book = MemeBook(str(self.PATH))
        man = book.match("man")
        self.assertIn("曼巴", [x["name"] for x in man])
        self.assertEqual(book.match("manager"), [])
        self.assertTrue(book.inject_for(["典中典"]))


if __name__ == "__main__":
    unittest.main()
