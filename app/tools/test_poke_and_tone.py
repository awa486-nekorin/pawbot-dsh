"""拍一拍等于被点名、工作意见要正经、追问式收尾要降频。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.agent import Agent
from pawbot.context import Context
from pawbot.event import parse
from pawbot.style import drop_trailing_question
from plugins import ai_chat


def group_ctx(bot, *, uid=2, nick="阿明", text="hello", to_me=False):
    raw = {"post_type": "message", "message_type": "group", "self_id": 1,
           "group_id": 77, "user_id": uid, "message_id": 5,
           "message": [{"type": "text", "data": {"text": text}}],
           "sender": {"nickname": nick}}
    ev = parse(raw)
    ev.to_me = to_me
    ctx = Context(ev, bot, bot.config)
    ctx.reply = AsyncMock(return_value={"message_id": 1})
    return ctx


def poke_ctx(bot, *, uid=2):
    ev = parse({"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                "self_id": 1, "group_id": 77, "user_id": uid, "target_id": 1})
    ctx = Context(ev, bot, bot.config)
    ctx.reply = AsyncMock(return_value={"message_id": 1})
    return ctx


def make_bot(**overrides):
    cfg = {"bot.nickname": "大肥鱼", "bot.wake_words": [], "account.owner_ids": ["9"],
           "chat.settle_sec": 0, "chat.settle_max": 0, "chat.batch_enabled": True}
    cfg.update(overrides)
    return SimpleNamespace(
        config=cfg, inbound={"group_77": 0},
        store=SimpleNamespace(history=lambda k: [],
                              window=lambda k, n=120: [
                                  {"role": "user", "who": "阿明", "uid": "2", "content": "hi"}],
                              append=Mock()),
        topic=SimpleNamespace(seen=lambda *a, **k: None),
        _pass_rate=lambda ev: True,
        agent=SimpleNamespace(run=AsyncMock(return_value=("好", []))))


class PokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for d in (ai_chat._LOCKS, ai_chat._PENDING, ai_chat._SETTLE, ai_chat._BATCH):
            d.clear()

    async def test_poke_goes_through_ai_as_addressed(self):
        bot = make_bot()
        ctx = poke_ctx(bot)
        await ai_chat.poke_turn(ctx)
        self.assertEqual(bot.agent.run.await_count, 1)
        args = bot.agent.run.await_args.args
        self.assertEqual(args[0], ctx)
        self.assertIn("拍了拍你", args[1])
        self.assertTrue(bot.agent.run.await_args.args[0].state.get("wake"))

    async def test_poke_uses_recent_nickname(self):
        bot = make_bot()
        ctx = poke_ctx(bot)
        ctx.state["poked"] = False
        await ai_chat.poke_turn(ctx)
        self.assertIn("阿明拍了拍你", bot.agent.run.await_args.args[1])

    async def test_poke_records_into_history(self):
        bot = make_bot()
        await ai_chat.poke_turn(poke_ctx(bot))
        bot.store.append.assert_called()
        first = bot.store.append.call_args_list[0].args
        self.assertEqual(first[1], "user")
        self.assertIn("拍了拍你", first[2])

    async def test_poke_gets_addressed_at_chance(self):
        bot = make_bot()
        ctx = poke_ctx(bot)
        ai_chat._roll_reply_style(ctx, {"reply.at_chance_addressed": 1.0})
        self.assertTrue(ctx._should_at(None))


class WorkFeedbackHintTests(unittest.TestCase):
    def hint(self, *, uid=9, text="你回复不要每次都用问句，改一下"):
        agent = Agent.__new__(Agent)
        agent.bot = SimpleNamespace(config={"account.owner_ids": ["9"]})
        ctx = SimpleNamespace(user_id=uid, plain=text)
        return Agent._work_feedback_hint(agent, ctx)

    def test_owner_feedback_gets_serious_hint(self):
        out = self.hint()
        self.assertIn("提意见", out)
        self.assertIn("嬉皮笑脸", out)
        self.assertIn("打岔", out)

    def test_other_members_get_nothing(self):
        self.assertEqual(self.hint(uid=2), "")

    def test_owner_small_talk_gets_nothing(self):
        self.assertEqual(self.hint(text="在吗"), "")

    def test_no_owners_configured(self):
        agent = Agent.__new__(Agent)
        agent.bot = SimpleNamespace(config={})
        self.assertEqual(Agent._work_feedback_hint(
            agent, SimpleNamespace(user_id=9, plain="不要这样")), "")


class QuestionRateTests(unittest.TestCase):
    def test_no_question_roll_strips_followup(self):
        bot = make_bot()
        ctx = group_ctx(bot, to_me=True)
        with patch("plugins.ai_chat.random.random", return_value=0.99):
            ai_chat._roll_reply_style(ctx, {"reply.question_rate": 0.3})
        self.assertFalse(ctx.state["reply_question"])

    def test_question_roll_allows_followup(self):
        bot = make_bot()
        ctx = group_ctx(bot, to_me=True)
        with patch("plugins.ai_chat.random.random", return_value=0.0):
            ai_chat._roll_reply_style(ctx, {"reply.question_rate": 0.3})
        self.assertTrue(ctx.state["reply_question"])

    def test_hint_tells_model_to_stop(self):
        agent = Agent.__new__(Agent)
        out = Agent._batch_hint(agent, SimpleNamespace(state={"reply_question": False}))
        self.assertIn("不要用问句或追问收尾", out)
        out = Agent._batch_hint(agent, SimpleNamespace(state={"reply_question": True}))
        self.assertIn("最多一个", out)


class DropTrailingQuestionTests(unittest.TestCase):
    def test_drops_tail_chase_clause(self):
        self.assertEqual(
            drop_trailing_question("都夜里十一点啦怎么还在吃中午饭，你这是刚睡醒还是过着哪国时差呀？"),
            "都夜里十一点啦怎么还在吃中午饭。")

    def test_drops_standalone_followup_sentence(self):
        self.assertEqual(drop_trailing_question("肯定是海蓝色呀。快跟我讲讲嘛"), "肯定是海蓝色呀。")
        self.assertEqual(drop_trailing_question("第一句。老实交代"), "第一句。")

    def test_keeps_statement_ending(self):
        t = "好家伙，原来在挑新手机啊。你看中哪台了？是旧的电池顶不住，还是卡得受不了啦。"
        self.assertEqual(drop_trailing_question(t), t)

    def test_keeps_lone_question(self):
        self.assertEqual(drop_trailing_question("就一句问话？"), "就一句问话？")

    def test_keeps_links(self):
        t = "看这个 https://a.com/x？真的？"
        self.assertEqual(drop_trailing_question(t), t)


if __name__ == "__main__":
    unittest.main()
