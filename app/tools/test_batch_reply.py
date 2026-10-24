"""普通群消息攒话后一起回，以及 @ / 引用的随机性。"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.agent import Agent
from pawbot.context import Context
from pawbot.event import parse
from pawbot.style import cap_sentences
from plugins import ai_chat


def make_ctx(bot, *, to_me=False, text="hello", user_id=2, nick="阿明", mid=1):
    raw = {"post_type": "message", "message_type": "group", "self_id": 1,
           "group_id": 77, "user_id": user_id, "message_id": mid,
           "message": [{"type": "text", "data": {"text": text}}],
           "sender": {"nickname": nick}}
    ev = parse(raw)
    ev.to_me = to_me
    ctx = Context(ev, bot, bot.config)
    ctx.reply = AsyncMock(return_value={"message_id": 1})
    return ctx


def make_bot(**overrides):
    config = {"bot.nickname": "大肥鱼", "bot.wake_words": [],
              "chat.settle_sec": 0.05, "chat.settle_max": 0.2,
              "chat.batch_enabled": True, "chat.batch_msgs": [2, 2],
              "chat.batch_quiet": [0.05, 0.05], "chat.batch_max_wait": [5, 5],
              "chat.batch_tick": 0.02, "tts.enabled": False}
    config.update(overrides)
    return SimpleNamespace(
        config=config, inbound={"group_77": 0},
        store=SimpleNamespace(history=Mock(return_value=[]), append=Mock()),
        topic=SimpleNamespace(seen=Mock()), _pass_rate=Mock(return_value=True),
        agent=SimpleNamespace(run=AsyncMock(return_value=("好", []))))


async def wait_for(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for d in (ai_chat._LOCKS, ai_chat._PENDING, ai_chat._SETTLE, ai_chat._BATCH):
            d.clear()

    async def test_unaddressed_messages_wait_and_answer_once(self):
        bot = make_bot()
        task = await ai_chat.ai_chat(make_ctx(bot, text="第一句"))
        await asyncio.sleep(0.1)
        self.assertEqual(bot.agent.run.await_count, 0)          # 一句不回
        await ai_chat.ai_chat(make_ctx(bot, text="第二句", user_id=3, nick="小红"))
        await ai_chat.ai_chat(make_ctx(bot, text="第三句"))
        await task
        self.assertEqual(bot.agent.run.await_count, 1)
        ctx, text = bot.agent.run.await_args.args[:2]
        self.assertEqual(text, "第三句")
        self.assertEqual(ctx.state["batch"]["count"], 3)
        self.assertEqual(ctx.state["batch"]["names"], ["阿明", "小红"])

    async def test_single_message_answers_after_max_wait(self):
        bot = make_bot(**{"chat.batch_msgs": [3, 3], "chat.batch_max_wait": [0.3, 0.3]})
        task = await ai_chat.ai_chat(make_ctx(bot, text="就这一句"))
        await asyncio.sleep(0.1)
        self.assertEqual(bot.agent.run.await_count, 0)
        await task
        self.assertEqual(bot.agent.run.await_count, 1)

    async def test_enough_messages_still_wait_for_quiet(self):
        bot = make_bot(**{"chat.batch_quiet": [0.3, 0.3]})
        task = await ai_chat.ai_chat(make_ctx(bot, text="a"))
        await ai_chat.ai_chat(make_ctx(bot, text="b"))
        await asyncio.sleep(0.15)                                 # 够数了但还没安静够
        self.assertEqual(bot.agent.run.await_count, 0)
        await task
        self.assertEqual(bot.agent.run.await_count, 1)

    async def test_addressed_message_skips_batch(self):
        bot = make_bot(**{"chat.batch_msgs": [5, 5]})
        task = await ai_chat.ai_chat(make_ctx(bot, to_me=True, text="大肥鱼在吗"))
        self.assertEqual(ai_chat._BATCH, {})
        await task
        self.assertEqual(bot.agent.run.await_count, 1)

    async def test_being_called_mid_batch_speeds_it_up(self):
        bot = make_bot(**{"chat.batch_msgs": [5, 5], "chat.batch_max_wait": [30, 30]})
        task = await ai_chat.ai_chat(make_ctx(bot, text="闲聊"))
        called = make_ctx(bot, to_me=True, text="肥鱼你说呢", user_id=3, nick="小红")
        await ai_chat.ai_chat(called)
        await asyncio.wait_for(task, 3)
        self.assertEqual(bot.agent.run.await_count, 1)
        ctx = bot.agent.run.await_args.args[0]
        self.assertTrue(ctx.state["batch"]["addressed"])
        self.assertIs(ctx.state["batch"]["target"], called)       # 回给叫它的人

    async def test_messages_during_generation_start_next_batch(self):
        bot = make_bot()
        entered, resume = asyncio.Event(), asyncio.Event()

        async def run(ctx, text, history):
            if text == "b":
                entered.set()
                await resume.wait()
            return "<pass>", []

        bot.agent.run.side_effect = run
        task = await ai_chat.ai_chat(make_ctx(bot, text="a"))
        await ai_chat.ai_chat(make_ctx(bot, text="b"))
        await asyncio.wait_for(entered.wait(), 3)
        await ai_chat.ai_chat(make_ctx(bot, text="c"))            # 生成期间进来的
        await ai_chat.ai_chat(make_ctx(bot, text="d"))
        resume.set()
        await task
        self.assertTrue(await wait_for(lambda: bot.agent.run.await_count == 2))
        self.assertEqual(bot.agent.run.await_args.args[1], "d")

    async def test_disabled_falls_back_to_settle(self):
        bot = make_bot(**{"chat.batch_enabled": False})
        task = await ai_chat.ai_chat(make_ctx(bot, text="单句"))
        self.assertEqual(ai_chat._BATCH, {})
        await task
        self.assertEqual(bot.agent.run.await_count, 1)

    async def test_reply_goes_through_target_ctx(self):
        bot = make_bot(**{"reply.at_chance_addressed": 1.0, "reply.quote_chance_addressed": 1.0})
        first = make_ctx(bot, text="闲聊", mid=10)
        called = make_ctx(bot, to_me=True, text="肥鱼", user_id=3, nick="小红", mid=11)
        task = await ai_chat.ai_chat(first)
        await ai_chat.ai_chat(called)
        await asyncio.wait_for(task, 3)
        called.reply.assert_awaited()
        first.reply.assert_not_awaited()


class ReplyStyleTests(unittest.TestCase):
    def test_chances_decide_at_and_quote(self):
        bot = make_bot()
        for p, expected in ((1.0, True), (0.0, False)):
            cfg = {"reply.at_chance": p, "reply.quote_chance": p}
            ctx = make_ctx(bot)
            ai_chat._roll_reply_style(ctx, cfg)
            self.assertIs(ctx._should_at(None), expected)
            self.assertIs(ctx._should_quote(None), expected)

    def test_override_beats_to_me_default(self):
        bot = make_bot()
        ctx = make_ctx(bot, to_me=True)
        ai_chat._roll_reply_style(ctx, {"reply.at_chance_addressed": 0.0,
                                        "reply.quote_chance_addressed": 0.0})
        self.assertFalse(ctx._should_at(None))
        self.assertFalse(ctx._should_quote(None))

    def test_always_and_off_modes_are_untouched(self):
        bot = make_bot()
        ctx = make_ctx(bot)
        ai_chat._roll_reply_style(ctx, {"reply.at_chance": 0.0, "reply.quote_chance": 0.0})
        self.assertTrue(ctx._should_at("always"))
        self.assertTrue(ctx._should_quote("always"))
        ctx.state["at_override"] = True
        self.assertFalse(ctx._should_at("off"))

    def test_both_outcomes_happen(self):
        bot = make_bot()
        seen = set()
        for _ in range(200):
            ctx = make_ctx(bot, to_me=True)
            ai_chat._roll_reply_style(ctx, {"reply.at_chance_addressed": 0.5,
                                            "reply.quote_chance_addressed": 0.5})
            seen.add((ctx._should_at(None), ctx._should_quote(None)))
        self.assertEqual(len(seen), 4)

    def test_private_chat_untouched(self):
        bot = make_bot()
        ctx = make_ctx(bot)
        ctx.event.message_type = "private"
        with patch.object(type(ctx.event), "is_group", False, create=True):
            ai_chat._roll_reply_style(ctx, {})
        self.assertNotIn("at_override", ctx.state)


class BatchHintTests(unittest.TestCase):
    def hint(self, state):
        agent = Agent.__new__(Agent)
        return Agent._batch_hint(agent, SimpleNamespace(state=state))

    def test_empty_without_batch(self):
        self.assertEqual(self.hint({}), "")

    def test_batch_lists_speakers_and_target(self):
        out = self.hint({"batch": {"count": 3, "names": ["阿明", "小红"]},
                         "reply_style": {"at": False, "quote": False, "same": False,
                                         "name": "小红", "text": "好热啊"}})
        self.assertIn("攒了 3 句", out)
        self.assertIn("阿明、小红", out)
        self.assertIn("小红 那句话下面", out)
        self.assertIn("顺口叫他的名字", out)

    def test_at_or_quote_tells_model_not_to_repeat_name(self):
        out = self.hint({"reply_style": {"at": True, "quote": True, "same": True, "name": "阿明"}})
        self.assertIn("@引用 阿明", out)
        self.assertIn("不用再重复叫他名字", out)


class LengthTests(unittest.TestCase):
    def test_roll_covers_one_to_five(self):
        seen = {ai_chat.roll_length({}) for _ in range(600)}
        self.assertEqual(seen, {1, 2, 3, 4, 5})

    def test_weights_can_pin_a_length(self):
        for _ in range(30):
            self.assertEqual(ai_chat.roll_length({"reply.length_weights": [0, 0, 0, 0, 1]}), 5)
            self.assertEqual(ai_chat.roll_length({"reply.length_weights": [1, 0, 0, 0, 0]}), 1)

    def test_bad_weights_fall_back_to_default(self):
        seen = {ai_chat.roll_length({"reply.length_weights": [0, 0, 0]}) for _ in range(600)}
        self.assertEqual(seen, {1, 2, 3, 4, 5})
        self.assertIn(ai_chat.roll_length({"reply.length_weights": "x"}), (1, 2, 3, 4, 5))

    def test_hint_carries_target_without_cramming(self):
        agent = Agent.__new__(Agent)
        out = Agent._batch_hint(agent, SimpleNamespace(state={"length_target": 5}))
        self.assertIn("大约 5 句话", out)
        self.assertIn("不要为了压句数把几层意思硬塞进一句", out)

class CapSentencesTests(unittest.TestCase):
    def test_caps_to_target(self):
        t = "现在都快夜里十一点了诶。你这是刚睡醒还是打错字了，不会连晚饭都没吃吧？"
        self.assertEqual(cap_sentences(t, 1), "现在都快夜里十一点了诶。")
        self.assertEqual(cap_sentences(t, 2), t)
        self.assertEqual(cap_sentences(t, 3), t)

    def test_ellipsis_is_not_a_sentence_end(self):
        self.assertEqual(cap_sentences("哈哈哈……笑死。真的假的？", 1), "哈哈哈……笑死。")

    def test_separator_message_counts_as_next_sentence(self):
        self.assertEqual(cap_sentences("好啊‖然后呢。再说一句。", 2), "好啊‖然后呢。")

    def test_unpunctuated_reply_kept(self):
        self.assertEqual(cap_sentences("没有标点的一句话", 1), "没有标点的一句话")

    def test_links_code_and_long_text_untouched(self):
        link = "看这个 https://a.com/x 挺好。第二句。第三句。"
        self.assertEqual(cap_sentences(link, 1), link)
        code = "试试这个 `x=1`。然后跑一下。"
        self.assertEqual(cap_sentences(code, 1), code)
        long = "解释一下。" * 60
        self.assertEqual(cap_sentences(long, 1), long)

    def test_pass_marker_and_bad_n(self):
        self.assertEqual(cap_sentences("<pass>", 1), "<pass>")
        self.assertEqual(cap_sentences("一句。两句。", 0), "一句。两句。")

    def test_closing_quote_stays_with_sentence(self):
        self.assertEqual(cap_sentences("他说「走吧。」然后呢。", 1), "他说「走吧。」")


class ExplainLengthTests(unittest.TestCase):
    def test_explain_rolls_at_least_three(self):
        seen = {ai_chat.roll_length({}, True) for _ in range(200)}
        self.assertEqual(seen, {3, 4, 5})

    def test_explain_detection(self):
        self.assertTrue(ai_chat.is_explain_request("给我讲讲为什么猫会呼噜"))
        self.assertTrue(ai_chat.is_explain_request("推荐个适合新手的番，说说好在哪"))
        self.assertFalse(ai_chat.is_explain_request("今天中午吃啥好"))

    def test_hint_says_split_instead_of_cramming(self):
        agent = Agent.__new__(Agent)
        out = Agent._batch_hint(agent, SimpleNamespace(
            state={"length_target": 3, "explain": True}))
        self.assertIn("不要用分号或逗号把几层意思硬塞进一句", out)

    async def test_explain_request_gets_three_plus_target(self):
        for d in (ai_chat._LOCKS, ai_chat._PENDING, ai_chat._SETTLE, ai_chat._BATCH):
            d.clear()
        bot = make_bot(**{"chat.settle_sec": 0, "chat.settle_max": 0})
        seen = []

        async def run(ctx, text, history):
            seen.append((ctx.state.get("explain"), ctx.state.get("length_target")))
            return "<pass>", []

        bot.agent.run.side_effect = run
        await ai_chat.ai_chat(make_ctx(bot, to_me=True, text="讲讲为什么猫会呼噜"))
        self.assertTrue(seen[0][0])
        self.assertGreaterEqual(seen[0][1], 3)


class LengthReplyTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_ceiling_is_enforced_not_target(self):
        for d in (ai_chat._LOCKS, ai_chat._PENDING, ai_chat._SETTLE, ai_chat._BATCH):
            d.clear()
        bot = make_bot(**{"chat.settle_sec": 0, "chat.settle_max": 0})
        # 目标 1 句，但内容确实有三句：不该被压成一句
        bot.agent.run.return_value = ("第一句。第二句。第三句。", [])
        ctx = make_ctx(bot, to_me=True, text="在吗")
        with patch("plugins.ai_chat.roll_length", return_value=1):
            await ai_chat.ai_chat(ctx)
        sent = ctx.reply.await_args.args[0][0]["data"]["text"]
        self.assertEqual(sent, "第一句。第二句。第三句。")

    async def test_over_ceiling_is_cut(self):
        for d in (ai_chat._LOCKS, ai_chat._PENDING, ai_chat._SETTLE, ai_chat._BATCH):
            d.clear()
        # 显式给上限，测的是「封顶机制」本身，不依赖默认值（默认值改过一次：5 → 3）
        bot = make_bot(**{"chat.settle_sec": 0, "chat.settle_max": 0,
                          "reply.length_max": 5})
        bot.agent.run.return_value = ("一。二。三。四。五。六。七。", [])
        ctx = make_ctx(bot, to_me=True, text="在吗")
        await ai_chat.ai_chat(ctx)
        self.assertEqual(ctx.reply.await_args.args[0][0]["data"]["text"], "一。二。三。四。五。")

    async def test_default_ceiling_is_three(self):
        # 2026-10-24：默认句数上限从 5 降到 3（实测中位 56 字，像总结报告）
        for d in (ai_chat._LOCKS, ai_chat._PENDING, ai_chat._SETTLE, ai_chat._BATCH):
            d.clear()
        bot = make_bot(**{"chat.settle_sec": 0, "chat.settle_max": 0})
        bot.agent.run.return_value = ("一。二。三。四。五。", [])
        ctx = make_ctx(bot, to_me=True, text="在吗")
        await ai_chat.ai_chat(ctx)
        self.assertEqual(ctx.reply.await_args.args[0][0]["data"]["text"], "一。二。三。")

    async def test_enforce_can_be_turned_off(self):
        for d in (ai_chat._LOCKS, ai_chat._PENDING, ai_chat._SETTLE, ai_chat._BATCH):
            d.clear()
        bot = make_bot(**{"chat.settle_sec": 0, "chat.settle_max": 0,
                          "reply.length_enforce": False})
        bot.agent.run.return_value = ("一。二。三。四。五。六。", [])
        ctx = make_ctx(bot, to_me=True, text="在吗")
        await ai_chat.ai_chat(ctx)
        self.assertEqual(ctx.reply.await_args.args[0][0]["data"]["text"], "一。二。三。四。五。六。")


class LengthReplyTurnVaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for d in (ai_chat._LOCKS, ai_chat._PENDING, ai_chat._SETTLE, ai_chat._BATCH):
            d.clear()

    async def test_reply_turn_sets_length_target_before_generation(self):
        bot = make_bot(**{"chat.settle_sec": 0, "chat.settle_max": 0})
        seen = []

        async def run(ctx, text, history):
            seen.append(ctx.state.get("length_target"))
            return "<pass>", []

        bot.agent.run.side_effect = run
        await ai_chat.ai_chat(make_ctx(bot, to_me=True, text="在吗"))
        self.assertEqual(len(seen), 1)
        self.assertIn(seen[0], (1, 2, 3, 4, 5))

    async def test_lengths_vary_across_turns(self):
        bot = make_bot(**{"chat.settle_sec": 0, "chat.settle_max": 0})
        seen = []

        async def run(ctx, text, history):
            seen.append(ctx.state.get("length_target"))
            return "<pass>", []

        bot.agent.run.side_effect = run
        for i in range(120):
            await ai_chat.ai_chat(make_ctx(bot, to_me=True, text=f"第{i}句"))
        self.assertEqual(set(seen), {1, 2, 3, 4, 5})


if __name__ == "__main__":
    unittest.main()
