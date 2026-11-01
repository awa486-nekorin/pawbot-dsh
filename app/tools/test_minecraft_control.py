"""Emergency-stop route and CLI tests: real loopback HTTP, synthetic secrets.

Covers the narrow authenticated control route (independent of the panel token
and the model), the honest result mapping in ``minecraft_control``, the direct
signed guard-revoke fallback, and the CLI's no-secret-argv/log behaviour.
No resident, game, world, runtime config or persona is touched.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import minecraft_control  # noqa: E402
from pawbot.core import Bot  # noqa: E402
from pawbot.minecraft_control import (  # noqa: E402
    CONTROL_TOKEN_ENV,
    main,
    revoke_direct,
    run_stop,
    settings_from_env,
    stop_via_control,
)
from pawbot.minecraft_guard import (  # noqa: E402
    GuardClient,
    GuardDenied,
    GuardSettings,
    sign,
)
from pawbot.webui import CONTROL_MAX_BODY, WebUI  # noqa: E402

TOKEN = "control-token-for-tests"
PANEL_TOKEN = "panel-token-for-tests"
SECRET = "g" * 40
ACTOR = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORLD = "test-world"
LAN_PORT = 32123
FULL = {"ok": True, "latched": True, "revoked": True, "resident_stopped": True,
        "sidecar_reaped": True, "writes_drained": True, "session": "resident"}


class FakeGuard:
    """Minimal signed loopback guard good enough for a ``revoke`` receipt."""

    def __init__(self, world_path):
        self.world_path = world_path
        self.mode = "ok"
        self.port_value = LAN_PORT
        self.already_revoked = False
        self.revoke_failures = 0
        self.sticky_after_success = False
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.requests += 1
                if owner.already_revoked:
                    # Same static answer the real guard returns after its
                    # sticky in-memory latch is set.
                    self.send_response(409)
                    self.end_headers()
                    self.wfile.write(b"revoked")
                    return
                if owner.revoke_failures:
                    # The real guard answers an unsigned 503 when it cannot
                    # persist the revocation marker, but its in-memory latch is
                    # already set, so every later request is a sticky 409.
                    owner.revoke_failures -= 1
                    owner.already_revoked = True
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"unavailable")
                    return
                if owner.mode == "deny":
                    self.send_response(403)
                    self.end_headers()
                    return
                request = json.loads(body)
                if owner.sticky_after_success and request["action"] == "revoke":
                    # A real durable revoke makes every later request a 409.
                    owner.already_revoked = True
                now = time.time()
                row = {"nonce": request["nonce"], "world_id": WORLD,
                       "save_path": owner.world_path, "host": "127.0.0.1",
                       "port": owner.port_value, "actor_uuid": request["actor_uuid"],
                       "action": request["action"],
                       "target_uuid": request["target_uuid"],
                       "issued_at": now, "expires_at": now + 5,
                       "can_write": False, "can_pvp": False, "backup_id": ""}
                raw = json.dumps(row, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-PawBot-Signature", sign(SECRET, raw))
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()

    @property
    def env(self):
        return {
            "PAWBOT_MC_GUARD_URL": f"http://127.0.0.1:{self.port}/v1/permit",
            "PAWBOT_MC_GUARD_SECRET": SECRET,
            "PAWBOT_MC_WORLD_ID": WORLD,
            "PAWBOT_MC_WORLD_PATH": self.world_path,
            "PAWBOT_MC_ACTOR_UUID": ACTOR,
            "PAWBOT_MC_LAN_PORT": str(LAN_PORT),
        }

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class FakeControlServer:
    """Loopback control endpoint with a configurable answer."""

    def __init__(self):
        self.status = 200
        self.payload = dict(FULL)
        self.raw = None
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.requests.append((self.path, dict(self.headers), body))
                if owner.raw is not None:
                    data = owner.raw
                else:
                    data = json.dumps(owner.payload).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class ControlTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pawbot-mc-control-")
        self.addCleanup(self.tmp.cleanup)
        self.world = os.path.realpath(os.path.join(self.tmp.name, "disposable-world"))
        os.makedirs(self.world)
        self.guard = FakeGuard(self.world)
        self.addCleanup(self.guard.close)
        self.control = FakeControlServer()
        self.addCleanup(self.control.close)


class DirectCliTests(ControlTestCase):
    def test_full_control_success_is_reported_truthfully(self):
        result = stop_via_control(self.control.port, TOKEN)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["path"], "control")
        self.assertTrue(result["revoked"] and result["resident_stopped"])
        method, headers, _body = self.control.requests[0]
        self.assertEqual(method, "/api/control/minecraft-stop")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)

    def test_session_none_without_revoke_is_not_success(self):
        self.control.payload = {"ok": True, "latched": True, "revoked": False,
                                "resident_stopped": True, "sidecar_reaped": True,
                                "writes_drained": True, "session": "none"}
        result = stop_via_control(self.control.port, TOKEN)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"], "control_incomplete")
        self.assertFalse(result["revoked"])

    def test_partial_control_is_incomplete_and_merges_with_direct_revoke(self):
        self.control.payload = {"ok": False, "latched": True, "revoked": False,
                                "resident_stopped": True, "sidecar_reaped": True,
                                "writes_drained": True, "session": "resident"}
        env = {CONTROL_TOKEN_ENV: TOKEN, **self.guard.env}
        merged = run_stop(self.control.port, env=env)
        self.assertTrue(merged["ok"], merged)
        self.assertEqual(merged["path"], "control+guard_direct")
        self.assertTrue(merged["revoked"] and merged["sidecar_reaped"])
        self.assertEqual(merged["control_error"], "control_incomplete")

    def test_control_rejection_falls_back_to_signed_guard_revoke(self):
        self.control.status = 401
        self.control.payload = {"ok": False, "error": "unauthorized"}
        env = {CONTROL_TOKEN_ENV: TOKEN, **self.guard.env}
        result = run_stop(self.control.port, env=env)
        self.assertFalse(result["ok"])
        self.assertEqual(result["control_error"], "control_rejected")
        self.assertTrue(result["revoked"], result)
        self.assertFalse(result["latched"], "guard-only revoke cannot latch PawBot")
        self.assertFalse(result["sidecar_reaped"])

    def test_unreachable_control_falls_back_and_stays_honest(self):
        probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = probe.server_address[1]
        probe.server_close()
        env = {CONTROL_TOKEN_ENV: TOKEN, **self.guard.env}
        result = run_stop(port, env=env)
        self.assertFalse(result["ok"])
        self.assertIn(result["control_error"],
                      ("control_unreachable", "control_invalid_response"))
        self.assertTrue(result["revoked"])
        self.assertFalse(result["resident_stopped"])

    def test_invalid_control_body_is_not_trusted(self):
        self.control.raw = b"not json at all"
        result = stop_via_control(self.control.port, TOKEN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "control_invalid_response")

    def test_missing_token_never_touches_the_network(self):
        env = dict(self.guard.env)
        result = run_stop(self.control.port, env=env)
        self.assertFalse(result["ok"])
        self.assertEqual(result["control_error"], "no_control_token")
        self.assertEqual(self.control.requests, [])
        self.assertTrue(result["revoked"])

    def test_guard_denial_and_bad_settings_are_static_errors(self):
        self.guard.mode = "deny"
        denied = revoke_direct(self.guard.env)
        self.assertFalse(denied["ok"])
        self.assertFalse(denied["revoked"])
        self.assertEqual(denied["error"], "guard_denied")
        result = run_stop(self.control.port,
                          env={"PAWBOT_MC_GUARD_SECRET": SECRET,
                               "PAWBOT_MC_LAN_PORT": "not-a-port"})
        self.assertFalse(result["ok"])
        self.assertFalse(result["revoked"])
        self.assertEqual(result["error"], "invalid_guard_settings")

    def test_direct_revoke_accepts_documented_origin_url(self):
        env = dict(self.guard.env)
        env["PAWBOT_MC_GUARD_URL"] = f"http://127.0.0.1:{self.guard.port}"
        result = revoke_direct(env)
        self.assertTrue(result["revoked"], result)
        self.assertFalse(result["ok"], "guard-only revoke cannot claim a full stop")

    def test_direct_revoke_accepts_zero_or_changed_lan_port_receipt(self):
        for port in (0, 45555):
            with self.subTest(port=port):
                self.guard.port_value = port
                result = revoke_direct(self.guard.env)
                self.assertTrue(result["revoked"], result)
                self.assertEqual(result["revoke_receipt"], "signed")
                self.assertEqual(result["error"], "resident_not_reaped")

    def test_direct_revoke_already_revoked_is_safe_but_not_a_durable_success(self):
        self.guard.already_revoked = True
        result = revoke_direct(self.guard.env)
        self.assertFalse(result["ok"], "a guard-only fallback never claims a full stop")
        self.assertFalse(
            result["revoked"],
            "an unsigned 409 proves the in-memory latch only, never durability")
        self.assertTrue(result["already_revoked"])
        self.assertFalse(result["revoke_durable"])
        self.assertEqual(result["revoke_receipt"], "already_revoked")
        self.assertEqual(result["error"], "revoke_not_durable")

    def test_direct_revoke_signed_receipt_is_durable_evidence(self):
        result = revoke_direct(self.guard.env)
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["revoked"], result)
        self.assertTrue(result["revoke_durable"])
        self.assertEqual(result["revoke_receipt"], "signed")

    def test_control_already_revoked_without_durable_marker_is_not_success(self):
        self.control.payload = {**FULL, "revoke_receipt": "already_revoked",
                                "revoke_durable": False}
        result = stop_via_control(self.control.port, TOKEN)
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["revoke_durable"])
        self.assertEqual(result["revoke_receipt"], "already_revoked")
        self.assertEqual(result["error"], "control_incomplete")

    def test_control_already_revoked_with_a_verified_durable_marker_is_valid(self):
        # A responder that can prove the marker is durable keeps the
        # already-revoked receipt semantics: still a confirmed stop.
        self.control.payload = {**FULL, "revoke_receipt": "already_revoked",
                                "revoke_durable": True}
        result = stop_via_control(self.control.port, TOKEN)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["revoke_durable"])
        self.assertEqual(result["revoke_receipt"], "already_revoked")

    def test_control_partial_plus_already_revoked_guard_is_not_merged_success(self):
        self.control.payload = {"ok": False, "latched": True, "revoked": False,
                                "resident_stopped": True, "sidecar_reaped": True,
                                "writes_drained": True, "session": "resident"}
        self.guard.already_revoked = True
        env = {CONTROL_TOKEN_ENV: TOKEN, **self.guard.env}
        result = run_stop(self.control.port, env=env)
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["revoked"], result)
        self.assertTrue(result["already_revoked"], result)
        self.assertEqual(result["path"], "guard_direct")
        self.assertEqual(result["error"], "revoke_not_durable")

    def test_direct_revoke_without_secret_is_honest_noop(self):
        result = revoke_direct({})
        self.assertFalse(result["ok"])
        self.assertFalse(result["revoked"])
        self.assertTrue(result["noop"])
        self.assertFalse(result["guard_configured"])
        self.assertEqual(result["revoke_skipped"], "no_guard_configured")
        self.assertEqual(result["error"], "guard_not_configured")

    def test_configured_secret_with_invalid_settings_is_not_not_configured(self):
        base = dict(self.guard.env)
        cases = {
            "port0_bad_actor": {"PAWBOT_MC_LAN_PORT": "0",
                                "PAWBOT_MC_ACTOR_UUID": "not-a-uuid"},
            "bad_path": {"PAWBOT_MC_WORLD_PATH": "relative-world"},
            "bad_world_id": {"PAWBOT_MC_WORLD_ID": "bad world id"},
            "stale_endpoint": {"PAWBOT_MC_GUARD_URL":
                               "http://127.0.0.1:1/v1/permit",
                               "PAWBOT_MC_LAN_PORT": "0"},
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                env = {**base, **changes}
                result = revoke_direct(env)
                self.assertFalse(result["ok"], result)
                self.assertFalse(result["revoked"], result)
                self.assertTrue(result["guard_configured"], result)
                self.assertNotIn("revoke_skipped", result)
                self.assertNotIn("noop", result)
                expected = ("guard_unavailable" if name == "stale_endpoint"
                            else "invalid_guard_settings")
                self.assertEqual(result["error"], expected, result)

    def test_cli_without_secret_never_claims_a_revoke(self):
        env = {"PAWBOT_WEBUI_PORT": "9"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out):
            code = main(["stop"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["revoked"])
        self.assertEqual(payload["revoke_skipped"], "no_guard_configured")

    def test_cli_does_not_accept_control_session_none_without_revoke(self):
        self.control.payload = {"ok": True, "latched": True, "revoked": False,
                                "resident_stopped": True, "sidecar_reaped": True,
                                "writes_drained": True, "session": "none"}
        env = {CONTROL_TOKEN_ENV: TOKEN,
               "PAWBOT_WEBUI_PORT": str(self.control.port)}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out):
            code = main(["stop"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["revoked"])
        self.assertEqual(payload["control_error"], "control_incomplete")

    def test_settings_from_env_never_raises_on_bad_port(self):
        settings = settings_from_env({"PAWBOT_MC_LAN_PORT": "oops"})
        self.assertEqual(settings.port, 0)

    def test_cli_prints_json_without_the_token_and_returns_success(self):
        env = {CONTROL_TOKEN_ENV: TOKEN, "PAWBOT_WEBUI_PORT": str(self.control.port),
               **self.guard.env}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(out):
            code = main(["stop"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(SECRET, out.getvalue())

    def test_cli_failure_exits_non_zero_without_traceback_or_token(self):
        env = {CONTROL_TOKEN_ENV: TOKEN, "PAWBOT_WEBUI_PORT": "9", **self.guard.env}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(out):
            code = main(["stop"])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())

    def test_cli_rejects_invalid_port(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["stop", "--webui-port", "70000"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error"], "invalid_webui_port")


class _Cfg:
    def __init__(self, values):
        self.values = values

    def get(self, dotted, default=None):
        node = self.values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _bind(bot, *names):
    for name in names:
        raw = inspect.getattr_static(Bot, name)
        if isinstance(raw, staticmethod):
            setattr(bot, name, raw.__func__)
        else:
            setattr(bot, name, types.MethodType(getattr(Bot, name), bot))
    return bot


class RealLoopbackEmergencyStopTests(unittest.IsolatedAsyncioTestCase):
    """Actual loopback route + CLI, real core/GuardClient, synthetic guard."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pawbot-mc-estop-")
        self.addCleanup(self.tmp.cleanup)
        self.world = os.path.realpath(os.path.join(self.tmp.name, "disposable-world"))
        os.makedirs(self.world)
        self.guard = FakeGuard(self.world)
        self.addCleanup(self.guard.close)
        self.guard.port_value = 0  # LAN closed: revoke receipt must carry 0
        config = _Cfg({"minecraft": {
            "enabled": True, "port": 0, "test_world_id": WORLD,
            "world_path": self.world, "owner_player_uuid": ACTOR,
            "guard_url": f"http://127.0.0.1:{self.guard.port}/v1/permit",
        }})
        self.bot = SimpleNamespace(
            config=config, minecraft_resident=None, _minecraft_estop=False,
            _minecraft_lock=asyncio.Lock())
        _bind(self.bot, "minecraft_emergency_stop", "_minecraft_guard_settings",
              "_minecraft_guard_revoke", "_minecraft_stop_session",
              "_minecraft_lifecycle_lock")
        self.web = WebUI(self.bot, host="127.0.0.1", port=0,
                         token=PANEL_TOKEN, control_token=TOKEN)
        await self.web.start()
        self.port = self.web._server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self.web.stop)

    def env(self):
        return {
            # Empty env override: the no-resident path must fall back to the
            # configured minecraft.guard_url (S2).
            "PAWBOT_MC_GUARD_URL": "",
            "PAWBOT_MC_GUARD_SECRET": SECRET,
            "PAWBOT_MC_WORLD_ID": WORLD,
            "PAWBOT_MC_WORLD_PATH": self.world,
            "PAWBOT_MC_ACTOR_UUID": ACTOR,
            "PAWBOT_MC_LAN_PORT": "0",
            "PAWBOT_CONTROL_TOKEN": TOKEN,
            "PAWBOT_WEBUI_PORT": str(self.port),
        }

    async def test_route_and_cli_confirm_the_real_revoke_receipt(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env(), clear=False), \
                contextlib.redirect_stdout(out):
            code = await asyncio.to_thread(main, ["stop"])
        self.assertTrue(out.getvalue(), f"CLI printed nothing (exit={code})")
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        payload = json.loads(lines[-1])
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["revoked"], payload)
        self.assertTrue(payload["revoke_durable"], payload)
        self.assertEqual(payload["session"], "none")
        self.assertEqual(payload["revoke_receipt"], "signed")
        self.assertEqual(self.guard.requests, 1)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(SECRET, out.getvalue())

    def _resident(self):
        """Resident with a real guard client, a reaped sidecar and a no-op estop."""
        settings = GuardSettings(
            f"http://127.0.0.1:{self.guard.port}/v1/permit", SECRET, WORLD,
            self.world, ACTOR, LAN_PORT)

        async def estop():
            return None

        return SimpleNamespace(
            guard=GuardClient(settings),
            sidecar=SimpleNamespace(process=None, alive=False),
            estop=estop)

    async def test_non_durable_503_then_409_is_never_a_successful_stop(self):
        # 1st revoke: unsigned 503 (marker not persisted); retry: sticky 409.
        # Everything else is confirmed, so only durability decides ok.
        self.guard.revoke_failures = 1
        self.bot.minecraft_resident = self._resident()
        result = await self.bot.minecraft_emergency_stop(source="non-durable")
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["revoked"], result)
        self.assertFalse(result["revoke_durable"], result)
        self.assertTrue(result["already_revoked"], result)
        self.assertEqual(result["revoke_receipt"], "already_revoked")
        self.assertEqual(result["revoke_error"], "already_revoked_unverified")
        self.assertTrue(result["resident_stopped"])
        self.assertTrue(result["sidecar_reaped"])
        self.assertTrue(result["writes_drained"])
        self.assertTrue(result["latched"])
        self.assertEqual(self.guard.requests, 2,
                         "one retry after the 503, then the sticky 409")

    async def test_fresh_already_revoked_without_durable_evidence_is_not_success(self):
        self.guard.already_revoked = True
        with mock.patch.dict(os.environ, self.env(), clear=False):
            result = await self.bot.minecraft_emergency_stop(source="fresh-409")
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["revoked"], result)
        self.assertFalse(result["revoke_durable"], result)
        self.assertTrue(result["already_revoked"], result)
        self.assertEqual(result["revoke_receipt"], "already_revoked")
        self.assertEqual(result["error"], "already_revoked_unverified")

    async def test_durable_receipt_makes_a_later_already_revoked_stop_valid(self):
        # The signed receipt proves the marker is durable; the same process may
        # then honour a later 409 as a valid already-revoked receipt.
        self.guard.sticky_after_success = True
        with mock.patch.dict(os.environ, self.env(), clear=False):
            first = await self.bot.minecraft_emergency_stop(source="first")
            self.assertTrue(first["ok"], first)
            self.assertTrue(first["revoke_durable"], first)
            self.assertEqual(first["revoke_receipt"], "signed")
            second = await self.bot.minecraft_emergency_stop(source="second")
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["already_revoked"], second)
        self.assertTrue(second["revoke_durable"], second)
        self.assertEqual(second["revoke_receipt"], "already_revoked")


class WebUIRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        async def emergency_stop(*, source="control"):
            self.calls.append(source)
            return dict(FULL)

        self.bot = SimpleNamespace(minecraft_emergency_stop=emergency_stop)
        self.web = WebUI(self.bot, host="127.0.0.1", port=0,
                         token=PANEL_TOKEN, control_token=TOKEN)
        await self.web.start()
        self.port = self.web._server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self.web.stop)

    async def request(self, method, path, *, token=None, body=b"{}",
                      content_length=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        length = len(body) if content_length is None else content_length
        headers = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1",
                   "Connection: close"]
        if token:
            headers.append("Authorization: Bearer " + token)
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {length}")
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode() + body)
        await writer.drain()
        try:
            raw = await asyncio.wait_for(reader.read(), 10)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if not raw:
            return None, {}
        status = int(raw.split(b" ", 2)[1])
        payload = raw.split(b"\r\n\r\n", 1)[1]
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, {}

    async def test_route_requires_the_independent_control_token(self):
        status, payload = await self.request("POST", "/api/control/minecraft-stop")
        self.assertEqual((status, payload.get("error")), (401, "unauthorized"))
        panel = PANEL_TOKEN
        status, payload = await self.request("POST", "/api/control/minecraft-stop",
                                             token=panel)
        self.assertEqual(status, 401)
        self.assertEqual(self.calls, [])

    async def test_route_is_post_only_and_control_token_only(self):
        status, _payload = await self.request("GET", "/api/control/minecraft-stop",
                                              token=TOKEN)
        self.assertEqual(status, 405)
        status, payload = await self.request("POST", "/api/control/minecraft-stop",
                                             token=TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.calls, ["control"])

    async def test_route_rejects_oversized_body_before_reading_it(self):
        status, payload = await self.request(
            "POST", "/api/control/minecraft-stop", token=TOKEN, body=b"",
            content_length=CONTROL_MAX_BODY + 1)
        self.assertEqual(status, 413)
        self.assertEqual(self.calls, [])

    async def test_route_is_disabled_when_control_token_is_empty(self):
        self.web.control_token = ""
        status, payload = await self.request("POST", "/api/control/minecraft-stop",
                                             token=TOKEN)
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "control_disabled")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
