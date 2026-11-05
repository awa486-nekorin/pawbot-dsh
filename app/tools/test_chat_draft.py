"""猫娘草稿法：三段顺序 / prompt 内容 / 降级 / 开关 / 思考强度默认值。

全部离线：模型调用是本文件里的 FakeLLM，不联网、不读 app/data、不碰 B 站链路。
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import draft, llm, policy
from pawbot.agent import Agent
from pawbot.config import DEFAULT, Config
from pawbot.llm import LLMClient

FULL_PERSONA_MARK = "大肥鱼完整人设：贪吃、爱摸鱼、嘴上不饶人。"
POLICY_TEXT = policy.system_policy_text().strip()


class Cfg(dict):
    """最小配置对象：缺键时返回调用方给的默认值（与既有测试里的假配置一致）。"""

    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeLLM:
    """按顺序吐出预设回复，并记录每次调用的 messages / tools / 覆盖参数。"""

    def __init__(self, responses, *, available=True):
        self.responses = list(responses)
        self.calls = []
        self.available = available

    def chat_available(self):
        return self.available

    async def chat_message(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("模型被多调用了一次：草稿法不该发额外请求")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return await item()
        return {"content": item}


def _build_persona(*, group_id=None, sender="", extra=""):
    """模拟 Persona.build：完整人设 + extra（草稿块就在这里）+ 不可变策略收尾。"""
    parts = [FULL_PERSONA_MARK, "## 说话方式\n喵。"]
    if extra:
        parts.append(extra)
    parts.append(policy.system_policy_text().strip())
    return policy.ensure_policy_last("\n\n".join(parts))


def make_bot(responses, *, overrides=None, available=True):
    config = Cfg({
        "tools.enabled": False,
        "topic.enabled": False,
        "spice.allow_flirt": False,
        "chat.context_messages": 0,
        "chat.context_chars": 12000,
        "chat.chat_draft_enabled": True,
    })
    if overrides:
        config.update(overrides)
    llm = FakeLLM(responses, available=available)
    bot = SimpleNamespace(
        config=config,
        llm=llm,
        persona=SimpleNamespace(build=Mock(side_effect=_build_persona)),
        toolbox=SimpleNamespace(schemas=Mock(return_value=[])),
        store=SimpleNamespace(summary=Mock(return_value=""),
                              turn_summary=Mock(return_value=""),
                              window=Mock(return_value=[])),
        stickers=SimpleNamespace(files=Mock(return_value=[])),
        topic=SimpleNamespace(), mood=SimpleNamespace(hint=lambda: ""),
    )
    return bot, llm


def make_ctx():
    return SimpleNamespace(group_id=1, sender_name="阿明", user_id=100,
                           session_id="group_1", is_group=True,
                           event=SimpleNamespace(plain="今天吃啥"),
                           state={}, matched_command=None)


def make_agent(bot):
    agent = Agent(bot)
    for name in ("_participation_hint", "_identity_hint", "_sense_hint",
                 "_life_hint", "_mood_hint", "_sticker_hint", "_spice_hint",
                 "_drafting_hint", "_tool_hint"):
        setattr(agent, name, lambda *args: "")
    return agent


def system_of(call) -> str:
    return str(call["messages"][0]["content"])


class FirstStagePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_stage_prompt_is_minimal_persona_and_includes_memory(self):
        """第一段 system 只有最小设定，不含完整人设；记忆块与当前这句话都带进去。"""
        bot, llm = make_bot(["草稿"])
        memory = [
            {"role": "system", "content": "## 你记得的事\n- 阿明爱吃面"},
            # 非 system 形状的块不进草稿上下文
            {"role": "assistant", "content": "（assistant 形状的块不要带进去）"},
        ]
        out = await draft.content_draft(bot, make_ctx(), "今天吃啥", memory,
                                        config=bot.config)
        self.assertEqual(out, "草稿")
        self.assertEqual(len(llm.calls), 1)
        msgs = llm.calls[0]["messages"]
        self.assertTrue(msgs[0]["content"].startswith(draft.DEFAULT_PERSONA))
        self.assertIn("不要编造现实活动", msgs[0]["content"])
        self.assertNotIn(FULL_PERSONA_MARK, str(msgs[0]["content"]))
        joined = "\n".join(str(m.get("content")) for m in msgs)
        self.assertIn("阿明爱吃面", joined)
        self.assertIn("今天吃啥", joined)
        self.assertNotIn("assistant 形状", joined)
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertIsNone(llm.calls[0]["tools"])          # 不带工具、不带 brain_conv

    async def test_second_stage_prompt_strips_catgirl_words_and_actions(self):
        """第二段只删猫娘特有词汇与括号动作，语气词和句子结构必须保留。"""
        bot, llm = make_bot(["问对方吃饭了没有，语气轻松。"])
        out = await draft.cleanup(bot, make_ctx(), "喵呜～（耳朵抖）先问她吃没吃",
                                  config=bot.config)
        self.assertEqual(out, "问对方吃饭了没有，语气轻松。")
        self.assertEqual(len(llm.calls), 1)
        system = system_of(llm.calls[0])
        for word in ("猫娘特有词汇", "括号里的动作", "一个字都不要改",
                     "语气词", "不要改写成中性平实"):
            self.assertIn(word, system)
        user_text = str(llm.calls[0]["messages"][-1]["content"])
        self.assertIn("喵呜～（耳朵抖）先问她吃没吃", user_text)
        self.assertEqual(llm.calls[0]["messages"][-1]["role"], "user")

    async def test_empty_first_draft_degrades_without_cleanup_call(self):
        bot, llm = make_bot([""])
        block = await draft.build_block(bot, make_ctx(), "今天吃啥", [],
                                        config=bot.config)
        self.assertEqual(block, "")
        self.assertEqual(len(llm.calls), 1)               # 第二段不再调用


class ThreeStageOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_stage_order_and_final_system_injection(self):
        """草稿 → 清理 → 完整人设成稿：顺序、prompt、注入位置一次核对。"""
        neutral = "问对方吃饭了没有，语气轻松。"
        bot, llm = make_bot(["先问她吃没吃，语气轻松", neutral, "那我就随便问问啦。"])
        text, media = await make_agent(bot).run(make_ctx(), "今天吃啥", [])

        self.assertEqual((text, media), ("那我就随便问问啦。", []))
        self.assertEqual(len(llm.calls), 3)               # 两段草稿 + 最终成稿

        # 第一段：最小设定 + 事实边界，不含完整人设
        self.assertTrue(system_of(llm.calls[0]).startswith(draft.DEFAULT_PERSONA))
        self.assertIn("不要编造现实活动", system_of(llm.calls[0]))
        self.assertNotIn(FULL_PERSONA_MARK, system_of(llm.calls[0]))
        # 第二段：清理要求 + 第一段草稿
        self.assertIn("猫娘特有词汇", system_of(llm.calls[1]))
        self.assertIn("先问她吃没吃",
                      str(llm.calls[1]["messages"][-1]["content"]))
        # 第三段：完整人设 system 里带中性草稿，不带原始草稿
        final_system = system_of(llm.calls[2])
        self.assertIn(FULL_PERSONA_MARK, final_system)
        self.assertIn("本轮内容草稿", final_system)
        self.assertIn(neutral, final_system)
        self.assertNotIn("先问她吃没吃", final_system)
        # 草稿块在不可变策略之前；策略仍恰好一份且在最后（不破坏 _ensure_policy_last）
        self.assertTrue(final_system.rstrip().endswith(POLICY_TEXT))
        self.assertLess(final_system.index("本轮内容草稿"),
                        final_system.index(POLICY_TEXT))
        self.assertEqual(final_system.count(POLICY_TEXT), 1)
        # 草稿只进 system：任何 user 消息里都没有
        for message in llm.calls[2]["messages"]:
            if message["role"] == "user":
                self.assertNotIn("本轮内容草稿", str(message.get("content")))
        self.assertEqual(llm.calls[2]["messages"][-1]["role"], "user")

    async def test_draft_block_stays_before_policy_with_memory_blocks(self):
        """有记忆块时 _ensure_policy_last 会把策略挪到最后一条 system；草稿仍在策略之前。"""
        neutral = "问对方吃饭了没有。"
        bot, llm = make_bot(["先问她吃没吃", neutral, "吃了吗？"])
        agent = make_agent(bot)
        agent._legacy_context = lambda *args: [
            {"role": "system", "content": "## 记忆块\n- 阿明爱吃面"}]
        await agent.run(make_ctx(), "今天吃啥", [])

        messages = llm.calls[-1]["messages"]
        systems = [m for m in messages if m["role"] == "system"]
        self.assertEqual(len(systems), 2)                 # persona + 记忆块
        self.assertIn("本轮内容草稿", str(systems[0]["content"]))
        self.assertNotIn(POLICY_TEXT, str(systems[0]["content"]))
        self.assertTrue(str(systems[-1]["content"]).rstrip().endswith(POLICY_TEXT))
        joined = "\n".join(str(m["content"]) for m in systems)
        self.assertLess(joined.index("本轮内容草稿"), joined.index(POLICY_TEXT))
        self.assertEqual(joined.count(POLICY_TEXT), 1)

    async def test_switch_off_calls_model_once(self):
        """chat_draft_enabled=False：完全跳过草稿法，只调用一次模型。"""
        bot, llm = make_bot(["只回一次。"], overrides={"chat.chat_draft_enabled": False})
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(text, "只回一次。")
        self.assertEqual(len(llm.calls), 1)
        self.assertNotIn("本轮内容草稿", system_of(llm.calls[0]))

    async def test_cleanup_off_uses_first_draft_directly(self):
        """chat_draft_cleanup=False：跳过第二段，直接把第一段草稿当本轮要点。"""
        raw = "要回她：嗯嗯，可以。"
        bot, llm = make_bot([raw, "好的。"], overrides={"chat.chat_draft_cleanup": False})
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(text, "好的。")
        self.assertEqual(len(llm.calls), 2)               # 没有清理那一次
        self.assertIn(raw, system_of(llm.calls[1]))

    async def test_draft_does_not_leak_into_reply(self):
        """最终回复是模型成稿，草稿 / 草稿块标记都不会被拼进发出的正文。"""
        bot, llm = make_bot(["喵呜～（耳朵抖）先问她吃没吃",
                             "问对方吃饭了没有。", "嗯，吃了吗？"])
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(text, "嗯，吃了吗？")
        for leak in ("喵呜", "耳朵抖", "问对方吃饭了没有", "本轮内容草稿", "草稿"):
            self.assertNotIn(leak, text)


class DegradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_stage_error_degrades_to_single_call(self):
        """第一段抛错：跳过草稿法，按原单段路径照常回复。"""
        bot, llm = make_bot([RuntimeError("boom"), "照常回复。"])
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(text, "照常回复。")
        self.assertEqual(len(llm.calls), 2)               # 失败的第一段 + 最终成稿
        self.assertNotIn("本轮内容草稿", system_of(llm.calls[-1]))

    async def test_second_stage_error_degrades_to_single_call(self):
        """第二段抛错：同样整条降级，不拿原始草稿硬凑。"""
        bot, llm = make_bot(["先问她吃没吃", RuntimeError("boom"), "照常回复。"])
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(text, "照常回复。")
        self.assertEqual(len(llm.calls), 3)               # 第一段 + 失败的清理 + 最终成稿
        self.assertNotIn("本轮内容草稿", system_of(llm.calls[-1]))
        self.assertNotIn("先问她吃没吃", system_of(llm.calls[-1]))

    async def test_timeout_degrades_to_single_call(self):
        """第一段超时：立刻跳过草稿法，不把回话拖住。"""
        async def slow():
            await asyncio.sleep(0.5)
            return {"content": "慢草稿"}

        bot, llm = make_bot([slow, "照常回复。"],
                            overrides={"chat.chat_draft_timeout": 0.02})
        started = time.monotonic()
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        elapsed = time.monotonic() - started
        self.assertEqual(text, "照常回复。")
        self.assertLess(elapsed, 0.35)                    # 没等那 0.5 秒
        self.assertEqual(len(llm.calls), 2)               # 被取消的第一段 + 最终成稿
        self.assertNotIn("本轮内容草稿", system_of(llm.calls[-1]))


class MockGuardTests(unittest.IsolatedAsyncioTestCase):
    """mock / 不可用时草稿法必须完全不跑，更不能把 mock 文案注入 system。"""

    async def test_unavailable_llm_skips_draft_and_calls_model_once(self):
        bot, llm = make_bot(["照常回复。"], available=False)
        block = await draft.build_block(bot, make_ctx(), "今天吃啥", [],
                                        config=bot.config)
        self.assertEqual(block, "")
        self.assertEqual(llm.calls, [])                   # 可用性判定先拦住，一次草稿都不发
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(text, "照常回复。")
        self.assertEqual(len(llm.calls), 1)               # 只有最终成稿那一次
        self.assertNotIn("mock", system_of(llm.calls[0]).casefold())
        self.assertNotIn("本轮内容草稿", system_of(llm.calls[0]))

    async def test_real_client_without_api_key_never_sends_draft_requests(self):
        """真实 LLMClient（无 api_key）自己就报告不可用：草稿法一次请求都不发。"""
        cfg = Cfg({
            "chat.chat_draft_enabled": True,
            "chat.chat_draft_timeout": 8,
            "llm.base_url": "https://relay.example/v1",
            "llm.api_key": "",
        })
        client = LLMClient(cfg)
        self.assertFalse(client.chat_available())
        from unittest.mock import patch
        with patch.object(client, "chat_message",
                          side_effect=AssertionError("mock 模式不该发草稿请求")):
            bot = SimpleNamespace(config=cfg, llm=client)
            block = await draft.build_block(bot, make_ctx(), "今天吃啥", [], config=cfg)
        self.assertEqual(block, "")

    async def test_mock_text_from_llm_is_never_injected(self):
        mock_line = f"{llm.MOCK_NOTICE}我听到你说：今天吃啥"
        bot, fake = make_bot([mock_line])
        block = await draft.build_block(bot, make_ctx(), "今天吃啥", [],
                                        config=bot.config)
        self.assertEqual(block, "")
        self.assertEqual(len(fake.calls), 1)              # 拿到 mock 后第二段不再调用

        bot, fake = make_bot([mock_line, "照常回复。"])
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(text, "照常回复。")
        self.assertEqual(len(fake.calls), 2)              # 第一段被丢弃 + 最终成稿
        self.assertNotIn("mock", system_of(fake.calls[-1]).casefold())
        self.assertNotIn("本轮内容草稿", system_of(fake.calls[-1]))


class ReasoningEffortTests(unittest.IsolatedAsyncioTestCase):
    """两段草稿即使运行态配置写 medium，也必须显式按 low 发出。"""

    async def test_both_draft_stages_request_low_even_if_config_is_medium(self):
        bot, fake = make_bot(["先问她吃没吃", "问对方吃饭了没有。", "嗯，吃了吗？"],
                             overrides={"llm.reasoning_effort": "medium"})
        await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(len(fake.calls), 3)
        for call in fake.calls[:2]:
            self.assertEqual(call["kwargs"].get("reasoning_effort"), "low")
        # 第三段是完整人设成稿，不强行改参数（仍按运行态配置）
        self.assertNotIn("reasoning_effort", fake.calls[2]["kwargs"])

    async def test_real_payloads_carry_low_for_both_stages(self):
        cfg = Cfg({
            "chat.chat_draft_enabled": True,
            "chat.chat_draft_timeout": 8,
            "chat.chat_draft_max_chars": 300,
            "chat.chat_draft_cleanup": True,
            "chat.chat_draft_persona": draft.DEFAULT_PERSONA,
            "llm.base_url": "https://relay.example/v1",
            "llm.api_key": "test-key",
            "llm.model": "chat-model",
            "llm.reasoning_effort": "medium",
        })
        client = LLMClient(cfg)
        seen = []

        def fake_post(url, payload, key, timeout):
            seen.append(payload)
            return {"choices": [{"message": {"role": "assistant",
                                             "content": "草稿" if len(seen) == 1 else "中性"},
                                 "finish_reason": "stop"}]}

        from unittest.mock import patch
        with patch.object(client, "_post", side_effect=fake_post):
            bot = SimpleNamespace(config=cfg, llm=client)
            block = await draft.build_block(bot, make_ctx(), "今天吃啥", [], config=cfg)
        self.assertIn("中性", block)
        self.assertEqual(len(seen), 2)
        self.assertEqual([p["reasoning_effort"] for p in seen], ["low", "low"])


class ScrubTests(unittest.IsolatedAsyncioTestCase):
    """模型回显草稿块 / 中性草稿时，最终回复里不能带出去。"""

    async def test_marker_and_full_neutral_draft_are_scrubbed(self):
        neutral = "问对方吃饭了没有，语气轻松。"
        block = draft.final_block(neutral)
        bot, fake = make_bot(["先问她吃没吃", neutral, block + "\n嗯，吃了吗？"])
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        self.assertEqual(text, "嗯，吃了吗？")
        for leak in ("本轮内容草稿", "照这个意思说", neutral):
            self.assertNotIn(leak, text)

    def test_scrub_unit(self):
        neutral = "问对方吃饭了没有。"
        block = draft.final_block(neutral)
        self.assertEqual(draft.scrub(block + "\n嗯，吃了吗？", draft_block=block),
                         "嗯，吃了吗？")
        # 模型只复述中性草稿（没有标记块）也要删掉
        self.assertEqual(draft.scrub("嗯。\n" + neutral, draft_block=block), "嗯。")
        self.assertEqual(draft.scrub("  "), "")
        # 没有块可参照时也要把标记行与提示行剥掉，绝不留下「本轮内容草稿」
        cleaned = draft.scrub(
            "嗯。\n## 本轮内容草稿（内部要点，不是台词）\n" + neutral
            + "\n（照这个意思说，用你自己的语气）\n吃了吗？")
        self.assertNotIn("本轮内容草稿", cleaned)
        self.assertNotIn("照这个意思说", cleaned)
        self.assertNotIn(neutral, cleaned)


class TimeoutBudgetTests(unittest.IsolatedAsyncioTestCase):
    """chat_draft_timeout=0 = 单段不限；两段合计仍受总预算与 reply_timeout 约束。"""

    def test_zero_means_unlimited_per_stage(self):
        self.assertEqual(draft._timeout_seconds(
            Cfg({"chat.chat_draft_timeout": 0})), 0.0)
        self.assertEqual(draft._timeout_seconds(
            Cfg({"chat.chat_draft_timeout": -5})), 0.0)

    def test_total_budget_is_bounded(self):
        self.assertEqual(draft.total_timeout(Cfg({})), draft.TOTAL_TIMEOUT)
        self.assertLess(draft.TOTAL_TIMEOUT, 2 * draft.DEFAULT_TIMEOUT)
        for reply in (5, 10, 120):
            with self.subTest(reply=reply):
                budget = draft.total_timeout(Cfg({"chat.reply_timeout": reply}))
                self.assertGreater(budget, 0)
                self.assertLessEqual(budget, reply)
                self.assertLessEqual(budget, draft.TOTAL_TIMEOUT)

    async def test_unlimited_stage_still_degrades_within_reply_timeout(self):
        async def slow():
            await asyncio.sleep(0.5)
            return {"content": "慢草稿"}

        bot, fake = make_bot([slow, "照常回复。"], overrides={
            "chat.chat_draft_timeout": 0,          # 单段不限
            "chat.reply_timeout": 0.2,             # 总预算 0.08s
        })
        started = time.monotonic()
        text, _ = await make_agent(bot).run(make_ctx(), "今天吃啥", [])
        elapsed = time.monotonic() - started
        self.assertEqual(text, "照常回复。")
        self.assertLess(elapsed, 0.2)
        self.assertEqual(len(fake.calls), 2)               # 被取消的第一段 + 最终成稿
        self.assertNotIn("本轮内容草稿", system_of(fake.calls[-1]))


class FinalBlockTests(unittest.TestCase):
    def test_final_block_requires_following_meaning_without_repeating(self):
        block = draft.final_block("问对方吃饭了没有。")
        self.assertIn("本轮内容草稿", block)
        self.assertIn("问对方吃饭了没有。", block)
        self.assertIn("不要逐字复述", block)
        self.assertIn("草稿", block)
        self.assertNotIn(POLICY_TEXT, block)
        self.assertEqual(draft.final_block("  "), "")


class ConfigDefaultTests(unittest.TestCase):
    def test_reasoning_effort_default_is_low(self):
        self.assertEqual(DEFAULT["llm"]["reasoning_effort"], "low")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "config.json"))
            self.assertEqual(cfg.get("llm.reasoning_effort"), "low")
            payload = LLMClient(cfg)._payload(
                [{"role": "user", "content": "hi"}])
            self.assertEqual(payload["reasoning_effort"], "low")

    def test_chat_draft_defaults(self):
        chat = DEFAULT["chat"]
        self.assertIs(chat["chat_draft_enabled"], False)
        self.assertEqual(chat["chat_draft_persona"], "你是一只猫娘。")
        self.assertIs(chat["chat_draft_cleanup"], True)
        self.assertEqual(chat["chat_draft_timeout"], 8)
        self.assertEqual(chat["chat_draft_max_chars"], 120)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "config.json"))
            self.assertFalse(draft.enabled(cfg))
            self.assertEqual(cfg.get("chat.chat_draft_persona"), "你是一只猫娘。")
            self.assertEqual(cfg.get("chat.chat_draft_timeout"), 8)

    def test_partial_config_without_key_stays_off(self):
        """没和 DEFAULT 合并的假配置（老调用方/测试）不发额外模型请求。"""
        self.assertFalse(draft.enabled({}))
        self.assertFalse(draft.enabled(Cfg({})))


if __name__ == "__main__":
    unittest.main()
