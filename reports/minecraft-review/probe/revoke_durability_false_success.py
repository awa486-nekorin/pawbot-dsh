"""Reviewer-only probe: a non-durable guard revoke can be re-reported as success.

Run from the repository root:

    py -3.13 reports\\minecraft-review\\probe\\revoke_durability_false_success.py

The probe uses a real ``GuardClient`` + real ``Bot.minecraft_emergency_stop``
against a synthetic loopback endpoint that reproduces the exact status contract
of the Java guard after a non-durable revoke:

  * first authenticated ``revoke``  -> 503 unsigned ``unavailable`` (no marker)
  * every later authenticated ``revoke`` -> 409 ``revoked`` (in-memory latch)

The second emergency stop then reports ``ok: true`` with
``revoke_receipt: "already_revoked"`` even though no durable marker exists, so a
process restart would re-arm the guard. No real game/world/auth is touched.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))

from pawbot.core import Bot  # noqa: E402

SECRET = "z" * 40
ACTOR = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORLD = "test-world"


class Cfg:
    def __init__(self, values):
        self.values = values

    def get(self, dotted, default=None):
        node = self.values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def bind(bot, *names):
    for name in names:
        raw = inspect.getattr_static(Bot, name)
        if isinstance(raw, staticmethod):
            setattr(bot, name, raw.__func__)
        else:
            setattr(bot, name, types.MethodType(getattr(Bot, name), bot))
    return bot


class NonDurableGuard:
    """Exact post-failure status contract of PermitService.revoke()."""

    def __init__(self):
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                self.rfile.read(length)
                owner.requests += 1
                if owner.requests == 1:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"unavailable")
                else:
                    self.send_response(409)
                    self.end_headers()
                    self.wfile.write(b"revoked")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/permit"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


async def _noop():
    return None


def main():
    world = tempfile.mkdtemp(prefix="pawbot-durability-world-")
    guard = NonDurableGuard()
    try:
        os.environ["PAWBOT_MC_GUARD_SECRET"] = SECRET
        os.environ["PAWBOT_MC_GUARD_URL"] = guard.url
        config = Cfg({"minecraft": {
            "enabled": True,
            "port": 0,
            "owner_qq_uid": "123456789",
            "owner_player_uuid": ACTOR,
            "test_world_id": WORLD,
            "world_path": world,
            "guard_required": True,
        }})
        bot = SimpleNamespace(
            config=config, minecraft_resident=None, _minecraft_estop=False,
            _minecraft_lock=asyncio.Lock())
        bind(bot, "minecraft_emergency_stop", "_minecraft_stop_session",
             "_minecraft_guard_revoke", "_minecraft_guard_settings",
             "_minecraft_lifecycle_lock")

        first = asyncio.run(bot.minecraft_emergency_stop(source="probe-1"))
        second = asyncio.run(bot.minecraft_emergency_stop(source="probe-2"))
        print("first =", json.dumps(first, ensure_ascii=False, sort_keys=True))
        print("second =", json.dumps(second, ensure_ascii=False, sort_keys=True))
        print("no_resident_guard_requests =", guard.requests)

        # Same failure, resident path: _minecraft_stop_session retries the
        # revoke after stopping the resident, so one single emergency-stop call
        # can turn the 503 into a 409 "already revoked" success.
        from pawbot.minecraft_guard import GuardClient, GuardSettings  # noqa: E402

        resident_guard = NonDurableGuard()
        try:
            os.environ["PAWBOT_MC_GUARD_URL"] = resident_guard.url
            resident_settings = GuardSettings(
                resident_guard.url, SECRET, WORLD, world, ACTOR, 25565)
            resident_bot = SimpleNamespace(
                config=config, minecraft_resident=None, _minecraft_estop=False,
                _minecraft_lock=asyncio.Lock())
            bind(resident_bot, "minecraft_emergency_stop", "_minecraft_stop_session",
                 "_minecraft_guard_revoke", "_minecraft_guard_settings",
                 "_minecraft_lifecycle_lock")
            resident = SimpleNamespace(
                guard=GuardClient(resident_settings),
                sidecar=SimpleNamespace(process=None, alive=False),
                estop=_noop,
            )
            resident_bot.minecraft_resident = resident
            single = asyncio.run(resident_bot.minecraft_emergency_stop(source="probe-resident"))
            print("resident_single_call =",
                  json.dumps(single, ensure_ascii=False, sort_keys=True))
            print("resident_guard_requests =", resident_guard.requests)
            if single.get("ok") is True:
                print("FALSE_SUCCESS: one resident stop reports ok without a durable revoke")
                return 1
        finally:
            resident_guard.close()

        if second.get("ok") is True:
            print("FALSE_SUCCESS: second stop reports ok without any durable revoke")
            return 1
        print("no false success")
        return 0
    finally:
        guard.close()


if __name__ == "__main__":
    sys.exit(main())
