"""Offline tests for the fixed read-only DSH bridge client."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pawbot import dsh_bridge
from pawbot.tools import ToolBox

TOKEN = "pbdsh_" + "a" * 64
ENV = {
    dsh_bridge.URL_ENV: "http://127.0.0.1:51225" + dsh_bridge.PATH,
    dsh_bridge.TOKEN_ENV: TOKEN,
}
STATUS = {
    "ok": True, "queue_length": 2, "inflight_watches": 1,
    "watched_today": 4, "daily_limit": 10, "auto_stock": True,
    "has_credential": True, "credential_source": "file",
    "title": "private", "token": "secret",
}


class BridgeTests(unittest.TestCase):
    def test_only_exact_loopback_http_endpoints_are_configured(self):
        self.assertTrue(dsh_bridge.is_configured(ENV))
        for url in (
            "http://localhost:51225" + dsh_bridge.PATH,
            "http://192.168.1.4:51225" + dsh_bridge.PATH,
            "http://127.0.0.1:51225/other",
            "http://127.0.0.1:0" + dsh_bridge.PATH,
            "http://user@127.0.0.1:51225" + dsh_bridge.PATH,
        ):
            with self.subTest(url=url):
                self.assertFalse(dsh_bridge.is_configured({**ENV, dsh_bridge.URL_ENV: url}))
        self.assertFalse(dsh_bridge.is_configured({}))
        self.assertFalse(dsh_bridge.is_configured({**ENV, dsh_bridge.TOKEN_ENV: "short"}))

    def test_request_is_fixed_and_result_is_projected(self):
        response = SimpleNamespace(status=200, getheader=lambda name, default="": "application/json",
                                    read=lambda size: json.dumps({
                                        "ok": True, "name": "bili_watch_status", "value": STATUS,
                                    }).encode())
        connection = SimpleNamespace(
            request=lambda *args, **kwargs: requests.append((args, kwargs)),
            getresponse=lambda: response,
            close=lambda: None,
        )
        requests = []
        with patch.object(dsh_bridge.http.client, "HTTPConnection", return_value=connection) as http:
            result = dsh_bridge.bili_watch_status(ENV)
        http.assert_called_once_with("127.0.0.1", 51225, timeout=2)
        self.assertEqual(result, {
            "ok": True, "queue_length": 2, "inflight_watches": 1,
            "watched_today": 4, "daily_limit": 10, "auto_stock": True,
            "has_credential": True, "credential_source": "file",
        })
        self.assertEqual(requests[0][0], ("POST", dsh_bridge.PATH))
        self.assertEqual(json.loads(requests[0][1]["body"]), {
            "name": "bili_watch_status", "arguments": {},
        })
        self.assertEqual(requests[0][1]["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(connection.timeout if hasattr(connection, "timeout") else 2, 2)

    def test_http_errors_invalid_payload_and_missing_env_are_safe(self):
        self.assertFalse(dsh_bridge.bili_watch_status({})["ok"])
        for status, body in (
            (503, b"{}"),
            (200, b"not-json"),
            (200, b'{"ok":true,"name":"other","value":{"ok":true}}'),
            (200, b'{"ok":true,"name":"bili_watch_status","value":{"ok":false}}'),
            (200, b"x" * (dsh_bridge.MAX_RESPONSE_BYTES + 1)),
        ):
            connection = SimpleNamespace(
                request=lambda *a, **kw: None,
                getresponse=lambda: SimpleNamespace(status=status,
                                                     getheader=lambda name, default="": "application/json",
                                                     read=lambda limit: body[:limit]),
                close=lambda: None,
            )
            with patch.object(dsh_bridge.http.client, "HTTPConnection", return_value=connection):
                self.assertFalse(dsh_bridge.bili_watch_status(ENV)["ok"])


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def box(self):
        return ToolBox(SimpleNamespace(config={}, qzone=SimpleNamespace(enabled=False)))

    async def test_status_tool_is_hidden_without_env_and_formats_only_projected_fields(self):
        box = self.box()
        with patch.dict("os.environ", {}, clear=True):
            self.assertNotIn("bili_watch_status", box.available())
            text, media = await box.execute(None, "bili_watch_status", {})
            self.assertIn("不可用", text)
            self.assertEqual(media, [])
        with patch.object(dsh_bridge, "bili_watch_status", return_value={
            "ok": True, "queue_length": 2, "has_credential": True,
        }):
            with patch.dict("os.environ", ENV, clear=True):
                self.assertIn("bili_watch_status", box.available())
                text, media = await box.execute(None, "bili_watch_status", {})
        self.assertIn("队列长度：2", text)
        self.assertIn("已配置凭据：True", text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(media, [])

    async def test_unavailable_bridge_never_leaks_transport_error(self):
        box = self.box()
        with patch.dict("os.environ", ENV, clear=True), patch.object(
                dsh_bridge, "bili_watch_status", return_value={"ok": False, "error": "secret URL"}):
            text, _ = await box.execute(None, "bili_watch_status", {})
        self.assertEqual(text, "B 站观看服务状态暂时不可用。")
        self.assertNotIn("secret URL", text)


if __name__ == "__main__":
    unittest.main()
