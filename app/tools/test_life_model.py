"""生活后台共用模型入口的离线测试。

用到真实 ``LLMClient``（provider=dsh）+ 真实 payload 折叠，只把 ``brain.turn``
换成替身来替代网络。断言三件事：
  · 自定义 life:<purpose>:uuid 轮次确实改道 DSH 大脑，且每次唯一；
  · payload 不带任何工具，context 明确 autonomous/非主人/无电脑能力/无原生工具，
    user 仍是调用方给的真实材料，没有被伪造；
  · mock / 空 / 回显 / 失败都不会被当成真实生活内容返回。
另外覆盖非 DSH 客户端的旧 chat_message(messages, None) 兼容路径。
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import brain, life_model  # noqa: E402
from pawbot.llm import LLMClient, MOCK_NOTICE  # noqa: E402


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def make_client(**overrides) -> LLMClient:
    data = {
        "llm.provider": "dsh",
        "llm.brain_timeout": 30,
        "llm.model": "deepseek-chat",
        "llm.base_url": "https://api.example.invalid/v1",
        "llm.api_key": "",
        "llm.max_tokens": 900,
        "llm.temperature": 0.8,
        "llm.timeout": 30,
    }
    data.update(overrides)
    return LLMClient(Cfg(data))


class BrainRouteTests(unittest.IsolatedAsyncioTestCase):
    """真实 LLMClient + brain.turn 替身：只替换网络，不替换路由。"""

    def setUp(self):
        self.url = patch.object(brain, "_URL",
                                "http://127.0.0.1:50311/pawbot-dsh-brain/turn").start()
        self.token = patch.object(brain, "_TOKEN", "pdbrain_" + "d" * 64).start()
        self.addCleanup(patch.stopall)
        self.payloads = []
        self.reply = '{"action":"skip","reason":"现在不想动"}'

    def fake_turn(self, payload, timeout=None):
        self.payloads.append(payload)
        return {"ok": True, "text": self.reply, "provider": "p",
                "model": "m", "session_id": "s"}

    async def call(self, client, purpose="plan", reasoning_effort="low"):
        bot = SimpleNamespace(llm=client)
        messages = [
            {"role": "system", "content": "## 她现在的状态\n安静，刚看完书。"},
            {"role": "user", "content": "今天下雨了，想看看窗外。"},
        ]
        with patch.object(brain, "turn", side_effect=self.fake_turn):
            text = await life_model.chat(
                bot, messages, purpose=purpose, reasoning_effort=reasoning_effort)
        return text, messages

    async def test_unique_life_conv_and_brain_payload_shape(self):
        client = make_client()
        text, messages = await self.call(client, purpose="plan")
        self.assertEqual(text, self.reply)
        self.assertEqual(len(self.payloads), 1)
        payload = self.payloads[0]

        # 轮次必须是自定义的 life:<purpose>:uuid，不能伪装成群聊/主人会话。
        self.assertRegex(payload["conv"], r"^life:plan:[0-9a-f]{32}$")
        # 不带任何工具 schema：原生工具和 PawBot 工具都不该出现。
        self.assertEqual(payload["tools"], [])
        # 结构化权限标记：自主轮次 + 非主人 + 无电脑能力 + 无原生工具。
        self.assertEqual(payload["context"], {
            "autonomous": True,
            "native_tools": False,
            "is_owner": False,
            "computer_access": False,
        })
        # 不伪造用户：user 就是调用方给的那条材料，上下文里也没有 sender 身份。
        self.assertEqual(payload["user"], messages[-1]["content"])
        self.assertNotIn("sender_name", payload["context"])
        self.assertNotIn("user_id", payload["context"])
        self.assertIn("安静，刚看完书。", payload["system"])
        # 单次思考强度带到 Host 选项，不影响其他后台轮次。
        self.assertEqual(payload["options"]["reasoningEffort"], "low")
        self.assertIsInstance(payload["options"]["timeoutMs"], int)

    async def test_each_call_gets_a_fresh_conv(self):
        client = make_client()
        await self.call(client, purpose="plan")
        await self.call(client, purpose="plan")
        self.assertEqual(len(self.payloads), 2)
        self.assertNotEqual(self.payloads[0]["conv"], self.payloads[1]["conv"])

    async def test_unknown_effort_is_not_forwarded(self):
        client = make_client()
        await self.call(client, purpose="reflect", reasoning_effort="off")
        self.assertNotIn("reasoningEffort", self.payloads[0]["options"])

    async def test_purpose_is_sanitized_for_conv(self):
        client = make_client()
        text, _ = await self.call(client, purpose="Plan / Round 2")
        self.assertEqual(text, self.reply)
        self.assertRegex(self.payloads[0]["conv"], r"^life:plan-round-2:[0-9a-f]{32}$")

    async def test_dsh_without_bridge_or_direct_key_is_rejected(self):
        client = make_client()
        with patch.object(brain, "enabled", return_value=False):
            with self.assertRaises(life_model.LifeModelError) as caught:
                await life_model.chat(
                    SimpleNamespace(llm=client),
                    [{"role": "user", "content": "随便说点什么"}])
        self.assertIn("大脑桥", str(caught.exception))


class DirectCompatTests(unittest.IsolatedAsyncioTestCase):
    """非 DSH 客户端保持老调用形状，只是额外带 reasoning_effort。"""

    async def test_calls_chat_message_without_brain_context(self):
        class FakeLLM:
            def __init__(self):
                self.calls = []

            def provider(self):
                return "openai"

            async def chat_message(self, messages, tools=None,
                                   reasoning_effort=None):
                self.calls.append((messages, tools, reasoning_effort))
                return {"content": "我刚把书放回架子上。"}

        client = FakeLLM()
        messages = [{"role": "system", "content": "人设"},
                    {"role": "user", "content": "在干嘛"}]
        text = await life_model.chat(SimpleNamespace(llm=client), messages)
        self.assertEqual(text, "我刚把书放回架子上。")
        self.assertEqual(len(client.calls), 1)
        sent, tools, effort = client.calls[0]
        self.assertEqual(sent, messages)
        self.assertIsNone(tools)
        self.assertEqual(effort, "low")

    async def test_legacy_client_without_effort_kwarg_still_works(self):
        class LegacyLLM:
            def __init__(self):
                self.calls = []

            def provider(self):
                return "openai"

            async def chat_message(self, messages, tools=None):
                self.calls.append((messages, tools))
                return {"content": "嗯，刚泡了茶。"}

        client = LegacyLLM()
        text = await life_model.chat(
            SimpleNamespace(llm=client),
            [{"role": "user", "content": "在吗"}], reasoning_effort="medium")
        self.assertEqual(text, "嗯，刚泡了茶。")
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(client.calls[0][1])


class RejectBadOutputTests(unittest.IsolatedAsyncioTestCase):
    """mock / 空 / 回显 / 故障都不能变成生活经历。"""

    def bot(self, content):
        class FakeLLM:
            def provider(self):
                return "openai"

            async def chat_message(self, messages, tools=None, **kwargs):
                return {"content": content}

        return SimpleNamespace(llm=FakeLLM())

    async def assert_rejected(self, content, fragment):
        with self.assertRaises(life_model.LifeModelError) as caught:
            await life_model.chat(self.bot(content),
                                  [{"role": "user", "content": "真实材料"}])
        self.assertIn(fragment, str(caught.exception))

    async def test_mock_text_is_rejected(self):
        await self.assert_rejected(MOCK_NOTICE + "我听到你说：真实材料", "mock")

    async def test_empty_text_is_rejected(self):
        await self.assert_rejected("   \n", "空")

    async def test_echoed_input_is_rejected(self):
        await self.assert_rejected("真实材料", "回显")

    async def test_client_failure_is_wrapped(self):
        class BrokenLLM:
            def provider(self):
                return "openai"

            async def chat_message(self, messages, tools=None, **kwargs):
                raise RuntimeError("relay down")

        with self.assertRaises(life_model.LifeModelError) as caught:
            await life_model.chat(SimpleNamespace(llm=BrokenLLM()),
                                  [{"role": "user", "content": "真实材料"}])
        self.assertIn("relay down", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)


if __name__ == "__main__":
    unittest.main()
