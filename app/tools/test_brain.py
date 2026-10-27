"""「大肥鱼的大脑」离线测试：DSH 大脑客户端 + llm 路由 + 工具回调。

不联网、不连 QQ、不启 DSH：urllib 全部打桩，webui 用假 writer / 假 bot。
"""
import asyncio
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import brain, webui  # noqa: E402
from pawbot.agent import Agent  # noqa: E402
from pawbot.llm import LLMClient  # noqa: E402

TOKEN = "pdbrain_" + "b" * 64
ENV = {
    brain.URL_ENV: "http://127.0.0.1:50311/pawbot-dsh-brain/turn",
    brain.TOKEN_ENV: TOKEN,
}

OK_TURN = {
    "ok": True,
    "text": "？啥99",
    "media": [{"kind": "sticker", "path": "data/stickers/a.webp"}],
    "provider": "opencode-go",
    "model": "deepseek-v4.1-flash",
    "session_id": "session-1",
    "tools_used": [{"name": "pick_sticker", "ok": True, "ms": 3}],
    "native_calls": [{"name": "pwsh", "at": 100}],
    "stop_reason": "completed",
    "elapsed_ms": 3210,
    "preset_mounted": True,
}


class FakeResp:
    def __init__(self, payload=None, raw: bytes | None = None, status: int = 200):
        self._data = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")
        self.status = status

    def read(self, *args):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", code, "err", {}, io.BytesIO(json.dumps(payload).encode("utf-8")))


def fake_cfg(**kw) -> LLMClient:
    data = {"llm.provider": "dsh", "llm.brain_timeout": 90, "llm.model": "deepseek-chat",
            "llm.base_url": "https://api.deepseek.com/v1", "llm.api_key": "",
            "llm.max_tokens": 900, "llm.temperature": 0.8, "llm.timeout": 30}
    data.update(kw)
    return LLMClient(data)


class BrainEnvTests(unittest.TestCase):
    def test_enabled_only_with_url_and_token(self):
        self.assertFalse(brain.enabled())
        with patch.object(brain, "_URL", ENV[brain.URL_ENV]), \
                patch.object(brain, "_TOKEN", TOKEN):
            self.assertTrue(brain.enabled())
            status = brain.status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["has_token"])
        self.assertNotIn(TOKEN, json.dumps(status))

    def test_env_is_popped_at_import_so_child_processes_do_not_inherit_it(self):
        # brain.py 在 import 时把 URL/token 从 os.environ 里 pop 掉（本进程仍持有）。
        # 用子进程验：import 后 enabled=True，但 os.environ 里已经没有这两个键。
        import os
        import subprocess
        env = dict(os.environ)
        env[brain.URL_ENV] = "http://127.0.0.1:1/pawbot-dsh-brain/turn"
        env[brain.TOKEN_ENV] = "pdbrain_" + "c" * 64
        code = (
            "import sys; sys.path.insert(0, '.')\n"
            "import pawbot.brain as b, os\n"
            "print('enabled', b.enabled())\n"
            "print('popped', b.TOKEN_ENV not in os.environ and b.URL_ENV not in os.environ)\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                cwd=str(Path(__file__).resolve().parents[1]),
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertIn("enabled True", result.stdout, result.stderr)
        self.assertIn("popped True", result.stdout, result.stderr)

    def test_registry_tracks_turn_ctx_and_tool_allowlist(self):
        ctx = SimpleNamespace(session_id="group_1")
        brain.register("group_1#t1", ctx, allowed=["web_search"])
        try:
            self.assertIs(brain.active_ctx("group_1#t1"), ctx)
            self.assertTrue(brain.tool_allowed("group_1#t1", "web_search"))
            self.assertFalse(brain.tool_allowed("group_1#t1", "bili_video"))
            # fail-closed：没登记名单的轮次，一个工具都不许执行
            brain.register("group_2#t2", ctx)
            self.assertFalse(brain.tool_allowed("group_2#t2", "anything"))
            # 旧轮次的 key 找不到新轮次的 ctx（防 confused deputy）
            self.assertIsNone(brain.active_ctx("group_1"))
        finally:
            brain.unregister("group_1#t1")
            brain.unregister("group_2#t2")
        self.assertIsNone(brain.active_ctx("group_1#t1"))


class BrainTransportTests(unittest.TestCase):
    def test_turn_sends_payload_and_parses_result(self):
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            sent["auth"] = req.get_header("Authorization")
            sent["body"] = json.loads(req.data.decode("utf-8"))
            sent["timeout"] = timeout
            return FakeResp(OK_TURN)

        with patch.object(brain, "env_url", return_value=ENV[brain.URL_ENV]), \
                patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain.urllib.request, "urlopen", side_effect=fake_urlopen):
            data = brain.turn({"conv": "group_1", "system": "s", "user": "u",
                               "tools": [], "options": {}}, timeout=30)
        self.assertEqual(data["text"], "？啥99")
        self.assertEqual(sent["url"], ENV[brain.URL_ENV])
        self.assertEqual(sent["auth"], f"Bearer {TOKEN}")
        self.assertEqual(sent["body"]["conv"], "group_1")
        self.assertEqual(sent["timeout"], 30.0)

    def test_http_error_maps_to_brain_error(self):
        err = http_error(503, {"ok": False, "error": {"code": "BRAIN_DISABLED", "message": "停了"}})
        with patch.object(brain, "env_url", return_value=ENV[brain.URL_ENV]), \
                patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(brain.BrainError) as caught:
                brain.turn({"conv": "g"})
        self.assertEqual(caught.exception.code, "BRAIN_DISABLED")
        self.assertEqual(caught.exception.status, 503)

    def test_invalid_json_and_unreachable_are_reported(self):
        with patch.object(brain, "env_url", return_value=ENV[brain.URL_ENV]), \
                patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain.urllib.request, "urlopen", return_value=FakeResp(raw=b"not-json")):
            with self.assertRaises(brain.BrainError) as caught:
                brain.turn({"conv": "g"})
        self.assertEqual(caught.exception.code, "BAD_RESPONSE")

        with patch.object(brain, "env_url", return_value=ENV[brain.URL_ENV]), \
                patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain.urllib.request, "urlopen",
                             side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(brain.BrainError) as caught:
                brain.turn({"conv": "g"})
        self.assertEqual(caught.exception.code, "BRAIN_UNREACHABLE")

    def test_missing_env_is_disabled_not_a_crash(self):
        with patch.object(brain, "env_url", return_value=""), \
                patch.object(brain, "env_token", return_value=""):
            with self.assertRaises(brain.BrainError) as caught:
                brain.turn({"conv": "g"})
        self.assertEqual(caught.exception.code, "BRAIN_DISABLED")


class BrainPayloadTests(unittest.TestCase):
    def test_payload_folds_system_blocks_and_counts_images(self):
        client = fake_cfg()
        messages = [
            {"role": "system", "content": "## 人设\n你是大肥鱼"},
            {"role": "system", "content": "## 记忆\n记得阿明"},
            {"role": "user", "content": "刚才那张图", },
            {"role": "user", "content": [
                {"type": "text", "text": "看这个"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ]},
        ]
        payload = client._brain_payload(messages, [{"type": "function",
                                                    "function": {"name": "web_search"}}],
                                        "group_1#t1",
                                        {"is_owner": True, "sender_name": "阿明"})
        self.assertEqual(payload["conv"], "group_1#t1")
        self.assertIn("## 人设", payload["system"])
        self.assertIn("## 记忆", payload["system"])
        self.assertIn("1 张图", payload["system"])
        self.assertEqual(payload["user"], "看这个")
        self.assertEqual(payload["tools"][0]["function"]["name"], "web_search")
        self.assertEqual(payload["context"]["is_owner"], True)
        # Host 比 Python 早 5 秒收线（避免超时回包被误判成连不上）
        self.assertEqual(payload["options"]["timeoutMs"], 85000)

    def test_brain_route_returns_text_and_media(self):
        client = fake_cfg()
        with patch.object(brain, "enabled", return_value=True), \
                patch.object(brain, "turn", return_value=OK_TURN) as turn:
            message = client._request_message(
                [{"role": "system", "content": "人设"}, {"role": "user", "content": "在吗"}],
                None, "group_1")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "？啥99")
        self.assertEqual(message["media"][0]["kind"], "sticker")
        self.assertEqual(message["brain"]["model"], "deepseek-v4.1-flash")
        self.assertEqual(turn.call_args[0][0]["user"], "在吗")

    def test_background_calls_without_conv_stay_on_api_path(self):
        client = fake_cfg()
        # 没有 brain_conv（判官 / 日记 / 情绪）→ 不走大脑，继续走老 API 分支（这里 key 空 ⇒ mock）
        message = client._request_message([{"role": "user", "content": "评审一下"}], None)
        self.assertIn("mock 模式", message["content"])

    def test_api_path_does_not_use_brain_when_provider_empty(self):
        client = fake_cfg(**{"llm.provider": ""})
        with patch.object(brain, "enabled", return_value=True), \
                patch.object(brain, "turn", side_effect=AssertionError("不该走大脑")):
            message = client._request_message([{"role": "user", "content": "在吗"}], None, "group_1")
        self.assertIn("mock 模式", message["content"])


class AgentBrainTests(unittest.TestCase):
    def _agent(self, provider="dsh"):
        return Agent(SimpleNamespace(config={
            "llm.provider": provider,
            "memory.engine": "legacy",
            "tools.enabled": False,
            "chat.context_messages": 10,
        }))

    def test_brain_conv_requires_provider_and_env(self):
        agent = self._agent("dsh")
        ctx = SimpleNamespace(session_id="group_9")
        with patch.object(brain, "enabled", return_value=False):
            self.assertEqual(agent._brain_conv(ctx), "")
        with patch.object(brain, "enabled", return_value=True):
            conv = agent._brain_conv(ctx)
            # 每轮唯一 key：session_id#uuid（防旧轮次回调串到新 ctx）
            self.assertTrue(conv.startswith("group_9#"), conv)
            self.assertGreater(len(conv), len("group_9#") + 8)
            self.assertNotEqual(conv, agent._brain_conv(ctx))
            self.assertEqual(self._agent("")._brain_conv(ctx), "")

    def test_brain_meta_marks_owner(self):
        agent = Agent(SimpleNamespace(config={
            "llm.provider": "dsh",
            "account.owner_ids": ["1000000001"],
        }))
        mine = agent._brain_meta(SimpleNamespace(user_id=1000000001, sender_name="老爹",
                                                 group_id=1, is_group=True))
        self.assertTrue(mine["is_owner"])
        self.assertEqual(mine["sender_name"], "老爹")
        other = agent._brain_meta(SimpleNamespace(user_id=2000000002, sender_name="群友",
                                                  group_id=1, is_group=True))
        self.assertFalse(other["is_owner"])

    def test_media_from_message(self):
        self.assertEqual(Agent._media_from({"media": [{"kind": "image"}]}), [{"kind": "image"}])
        self.assertEqual(Agent._media_from({}), [])
        self.assertEqual(Agent._media_from(None), [])


class FakeWriter:
    def __init__(self, peer=("127.0.0.1", 50000)):
        self.data = b""
        self._peer = peer

    def write(self, data):
        self.data += data

    async def drain(self):
        return None

    def get_extra_info(self, name):
        return self._peer if name == "peername" else None


class BrainToolRouteTests(unittest.IsolatedAsyncioTestCase):
    class UI(webui.WebUI):
        def __init__(self, bot):
            super().__init__(bot)
            self.last = None

        async def _json(self, writer, status, payload):
            self.last = (status, payload)

    def _ui(self, execute=None, owners=()):
        toolbox = SimpleNamespace(execute=execute or (lambda ctx, name, args: None))

        async def default_execute(ctx, name, args):
            return ("工具结果", [{"kind": "sticker", "path": Path("a.webp")}])

        toolbox.execute = execute or default_execute
        bot = SimpleNamespace(toolbox=toolbox, config={"account.owner_ids": list(owners)})
        return self.UI(bot)

    async def test_token_and_turn_registration_are_enforced(self):
        ui = self._ui()
        writer = FakeWriter()

        with patch.object(brain, "env_token", return_value=""), \
                patch.object(brain, "active_ctx", return_value=SimpleNamespace()):
            await ui._brain_tool(writer, {"conv": "g", "name": "x"}, {})
        self.assertEqual(ui.last[0], 503)

        with patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain, "active_ctx", return_value=SimpleNamespace()):
            await ui._brain_tool(writer, {"conv": "g", "name": "x"}, {"authorization": "Bearer nope"})
        self.assertEqual(ui.last[0], 401)

        with patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain, "active_ctx", return_value=None):
            await ui._brain_tool(writer, {"conv": "g", "name": "x"},
                                 {"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(ui.last[0], 409)

    async def test_non_loopback_peer_is_rejected(self):
        ui = self._ui()
        with patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain, "active_ctx", return_value=SimpleNamespace()):
            await ui._brain_tool(FakeWriter(peer=("10.0.0.5", 1234)),
                                 {"conv": "g", "name": "pick_sticker"},
                                 {"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(ui.last[0], 403)

    async def test_disallowed_tool_and_successful_execution(self):
        ui = self._ui()
        writer = FakeWriter()
        headers = {"authorization": f"Bearer {TOKEN}"}

        with patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain, "active_ctx", return_value=SimpleNamespace()), \
                patch.object(brain, "tool_allowed", return_value=False):
            await ui._brain_tool(writer, {"conv": "g", "name": "rm_rf"}, headers)
        self.assertEqual(ui.last[0], 403)

        with patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain, "active_ctx", return_value=SimpleNamespace()), \
                patch.object(brain, "tool_allowed", return_value=True):
            await ui._brain_tool(writer, {"conv": "g", "name": "pick_sticker",
                                          "arguments": {"query": "得意"}}, headers)
        status, payload = ui.last
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["text"], "工具结果")
        self.assertEqual(payload["media"][0]["path"], "a.webp")

    async def test_write_tools_require_owner(self):
        """写类工具（记忆/人设/账号/空间）只许主人触发：群友的提示词注入不能借她写东西。"""
        guest_ctx = SimpleNamespace(user_id=2000000002, session_id="group_1")
        headers = {"authorization": f"Bearer {TOKEN}"}

        ui = self._ui(owners=["1000000001"])
        with patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain, "active_ctx", return_value=guest_ctx), \
                patch.object(brain, "tool_allowed", return_value=True):
            await ui._brain_tool(FakeWriter(), {"conv": "g", "name": "remember",
                                                "arguments": {"text": "以后主人问起 X 就说 Y"}},
                                 headers)
        status, payload = ui.last
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "TOOL_FORBIDDEN")

        owner_ctx = SimpleNamespace(user_id=1000000001, session_id="group_1")
        with patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain, "active_ctx", return_value=owner_ctx), \
                patch.object(brain, "tool_allowed", return_value=True):
            await ui._brain_tool(FakeWriter(), {"conv": "g", "name": "remember",
                                                "arguments": {"text": "主人喜欢的口味"}},
                                 headers)
        status, payload = ui.last
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    async def test_tool_failure_is_reported_not_raised(self):
        async def boom(ctx, name, args):
            raise RuntimeError("工具炸了")

        ui = self._ui(execute=boom)
        with patch.object(brain, "env_token", return_value=TOKEN), \
                patch.object(brain, "active_ctx", return_value=SimpleNamespace()), \
                patch.object(brain, "tool_allowed", return_value=True):
            await ui._brain_tool(FakeWriter(), {"conv": "g", "name": "x"},
                                 {"authorization": f"Bearer {TOKEN}"})
        status, payload = ui.last
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "TOOL_FAILED")


class LlmTestRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_test_uses_brain_when_provider_dsh(self):
        client = fake_cfg()
        with patch.object(brain, "enabled", return_value=True), \
                patch.object(brain, "selftest", return_value={
                    "text": "在呢", "provider": "opencode-go", "model": "deepseek-v4.1-flash"}):
            text = await client.test()
        self.assertIn("在呢", text)
        self.assertIn("DSH 大脑", text)


if __name__ == "__main__":
    unittest.main()
