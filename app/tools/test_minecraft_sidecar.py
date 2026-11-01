"""Offline transport tests for the supervised sidecar process.

Every test spawns a real temporary child process (``sys.executable`` + a
synthetic stdio JSON-RPC script) and talks to it through the real
``MinecraftSidecar`` pipes. No Node module, Minecraft, world, auth cache,
runtime data or network endpoint is used; the synthetic guard URL is never
contacted by the child.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import uuid
from contextlib import suppress
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.minecraft_guard import GuardDenied  # noqa: E402
from pawbot.minecraft_sidecar import (  # noqa: E402
    MAX_LINE,
    MinecraftSidecar,
    SidecarError,
    build_env,
    normalize_guard_url,
)

SECRET = "s" * 40
ACTOR = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LAN_PORT = 32123
CONNECT = {"host": "127.0.0.1", "port": 12345, "version": "1.20.1",
           "username": "SyntheticBot", "auth_mode": "offline", "timeout_sec": 1}

# A synthetic child implementing the documented stdio JSON-RPC contract. It
# records every request method in LOG (so "never replay" is observable) and can
# dump its environment (so the allowlist is observable). MODE selects one
# misbehaviour per test.
_FAKE = r'''
import json, os, sys, threading, time

MODE = __MODE__
LOG = __LOG__
DUMP = __DUMP__


def send(obj):
    sys.stdout.buffer.write(
        (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def log(method, params):
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"method": method, "params": params},
                                    sort_keys=True) + "\n")
    except OSError:
        pass


if MODE == "no_ready":
    time.sleep(30)
    sys.exit(0)
send({"jsonrpc": "2.0", "method": "event",
      "params": {"type": "ready", "protocol_version": 2 if MODE == "bad_version" else 1}})
if MODE == "stderr_noise":
    sys.stderr.write("device code must never be logged\n")
    sys.stderr.flush()
if MODE == "spam_events":
    for index in range(120):
        send({"jsonrpc": "2.0", "method": "event",
              "params": {"type": "noise", "n": index}})
try:
    with open(DUMP, "w", encoding="utf-8") as handle:
        handle.write("\n".join(sorted("%s=%s" % (key, value)
                                      for key, value in os.environ.items())))
except OSError:
    pass

while True:
    raw = sys.stdin.buffer.readline()
    if not raw:
        break
    try:
        message = json.loads(raw)
    except ValueError:
        continue
    method = message.get("method")
    mid = message.get("id")
    params = message.get("params")
    log(method, params)
    if MODE == "malformed" and method == "observe":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        continue
    if MODE == "oversize" and method == "observe":
        sys.stdout.buffer.write(b" " * 70000 + b"\n")
        sys.stdout.buffer.flush()
        continue
    if MODE == "unsolicited" and method == "observe":
        send({"jsonrpc": "2.0", "id": 999999, "result": {"stale": True}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"health": 20}})
        continue
    if MODE == "hang" and method in ("observe", "skill"):
        threading.Thread(target=time.sleep, args=(10,), daemon=True).start()
        continue
    if method == "connect":
        if MODE == "crash_connect_once":
            marker = LOG + ".crashed"
            if not os.path.exists(marker):
                open(marker, "w").close()
                os._exit(3)
        if MODE == "hang_connect":
            threading.Thread(target=time.sleep, args=(10,), daemon=True).start()
            continue
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"connected": True, "uuid": "actor", "world_id": "world",
                         "backup_completed": True}})
        send({"jsonrpc": "2.0", "method": "event",
              "params": {"type": "connected", "uuid": "actor"}})
    elif method == "observe":
        try:
            with open(DUMP, encoding="utf-8") as handle:
                env_text = handle.read()
        except OSError:
            env_text = ""
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"health": 20, "env": env_text}})
    elif method == "skill":
        send({"jsonrpc": "2.0", "id": mid, "result": {"done": True}})
    elif method == "cancel":
        send({"jsonrpc": "2.0", "id": mid, "result": {"cancelled": True}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopped": True}})
        break
    else:
        send({"jsonrpc": "2.0", "id": mid,
              "error": {"code": -32601, "message": "Method not found"}})
'''


def _read_log(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except OSError:
        return []


class SidecarTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pawbot-sidecar-test-")
        self.sidecars: list[MinecraftSidecar] = []

    async def asyncTearDown(self):
        for sidecar in self.sidecars:
            with suppress(Exception):
                await sidecar.stop(graceful=False)
        self.tmp.cleanup()

    def _fake(self, mode: str) -> tuple[str, str, str]:
        stem = f"fake_{mode}_{uuid.uuid4().hex[:8]}"
        script = os.path.join(self.tmp.name, stem + ".py")
        dump = os.path.join(self.tmp.name, stem + ".env")
        log = os.path.join(self.tmp.name, stem + ".log")
        source = (_FAKE.replace("__MODE__", repr(mode))
                  .replace("__LOG__", repr(log)).replace("__DUMP__", repr(dump)))
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(source)
        return script, dump, log

    def _make(self, mode: str, **kwargs) -> tuple[MinecraftSidecar, str, str]:
        script, dump, log = self._fake(mode)
        env = {
            "PAWBOT_MC_GUARD_URL": f"http://127.0.0.1:{LAN_PORT}/v1/permit",
            "PAWBOT_MC_GUARD_SECRET": SECRET,
            "PAWBOT_MC_WORLD_ID": "test-world",
            "PAWBOT_MC_WORLD_PATH": self.tmp.name,
            "PAWBOT_MC_ACTOR_UUID": ACTOR,
            "PAWBOT_MC_PVP_UUIDS": "[]",
            "PAWBOT_MC_WRITE_ENABLED": "false",
            "PAWBOT_MC_PVP_ENABLED": "false",
            "PAWBOT_MC_AUTH_DIR": self.tmp.name,
        }
        sidecar = MinecraftSidecar(env=env, entry=script, node=sys.executable,
                                   **kwargs)
        self.sidecars.append(sidecar)
        return sidecar, dump, log

    async def _wait(self, predicate, timeout: float = 4.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return bool(predicate())

    # ------------------------------------------------------------- handshake
    async def test_start_waits_for_ready_protocol_one(self):
        sidecar, _dump, _log = self._make("normal")
        await sidecar.start()
        self.assertTrue(sidecar.alive)
        self.assertEqual(sidecar.protocol_version, 1)
        self.assertTrue(sidecar.events.empty(), "ready must not enter the event queue")

    async def test_wrong_protocol_version_is_rejected(self):
        sidecar, _dump, _log = self._make("bad_version", ready_timeout=2)
        with self.assertRaises(SidecarError):
            await sidecar.start()
        self.assertFalse(sidecar.alive)
        self.assertIsNone(sidecar.process)

    async def test_missing_ready_times_out_and_reaps(self):
        sidecar, _dump, _log = self._make("no_ready", ready_timeout=0.3)
        with self.assertRaises(SidecarError) as ctx:
            await sidecar.start()
        self.assertEqual(str(ctx.exception), "sidecar_not_ready")
        self.assertIsNone(sidecar.process)

    async def test_missing_executable_is_a_static_error(self):
        sidecar, _dump, _log = self._make("normal")
        sidecar.node = "pawbot-no-such-node-binary-da39a3"
        with self.assertRaises(SidecarError) as ctx:
            await sidecar.start()
        self.assertEqual(str(ctx.exception), "sidecar_unavailable")
        self.assertIsNone(sidecar.process)

    # ------------------------------------------------------ requests & events
    async def test_requests_are_correlated_and_events_are_forwarded(self):
        sidecar, _dump, _log = self._make("normal")
        await sidecar.start()
        observed, skilled = await asyncio.gather(
            sidecar.request("observe", {}, timeout=5),
            sidecar.request("skill", {"name": "move", "args": {}, "timeout_sec": 1},
                            timeout=5))
        self.assertEqual(observed["health"], 20)
        self.assertTrue(skilled["done"])
        connected_result = await sidecar.request(
            "connect", {"host": "127.0.0.1", "port": 12345, "version": "1.20.1",
                        "username": "SyntheticBot", "auth_mode": "offline",
                        "timeout_sec": 1}, timeout=5)
        self.assertTrue(connected_result["connected"])
        connected = await asyncio.wait_for(sidecar.events.get(), 2)
        self.assertEqual(connected["type"], "connected")
        self.assertTrue(sidecar.events.empty())

    async def test_unknown_correlation_ids_are_ignored(self):
        sidecar, _dump, _log = self._make("unsolicited")
        await sidecar.start()
        self.assertEqual((await sidecar.request("observe", {}, timeout=5))["health"], 20)

    async def test_queue_is_bounded_and_cancel_keeps_a_reserved_slot(self):
        sidecar, _dump, _log = self._make("hang", max_pending=8)
        await sidecar.start()
        waiting = [asyncio.create_task(sidecar.request("observe", {}, timeout=30))
                   for _ in range(8)]
        try:
            await self._wait(lambda: sidecar.pending >= 8)
            with self.assertRaises(SidecarError) as ctx:
                await sidecar.request("skill", {"name": "move", "args": {},
                                                "timeout_sec": 1}, timeout=5)
            self.assertEqual(str(ctx.exception), "queue_full")
            self.assertTrue(await sidecar.cancel(timeout=3),
                            "cancel must be admitted through a reserved slot")
        finally:
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)

    async def test_events_are_bounded(self):
        sidecar, _dump, _log = self._make("spam_events")
        await sidecar.start()
        await self._wait(lambda: sidecar.events.qsize() >= 64, timeout=4)
        self.assertLessEqual(sidecar.events.qsize(), 64)

    # --------------------------------------------------- quarantine / replay
    async def test_timeout_quarantines_reaps_and_never_replays(self):
        sidecar, _dump, log = self._make("hang")
        await sidecar.start()
        with self.assertRaises(asyncio.TimeoutError):
            await sidecar.request("skill", {"name": "move", "args": {},
                                            "timeout_sec": 1}, timeout=0.3)
        reaped = await self._wait(
            lambda: sidecar.process is not None and sidecar.process.returncode is not None)
        self.assertTrue(reaped, "a quarantined child must be killed and reaped")
        self.assertFalse(sidecar.alive)
        self.assertEqual(sidecar.pending, 0)
        with self.assertRaises(SidecarError):
            await sidecar.request("skill", {"name": "move", "args": {},
                                            "timeout_sec": 1}, timeout=1)
        self.assertEqual([row["method"] for row in _read_log(log)], ["skill"],
                         "a timed-out mutation must never be replayed")

    async def test_malformed_output_quarantines(self):
        sidecar, _dump, _log = self._make("malformed")
        await sidecar.start()
        with self.assertRaises(SidecarError):
            await sidecar.request("observe", {}, timeout=3)
        self.assertTrue(await self._wait(lambda: not sidecar.alive))

    async def test_oversized_output_quarantines(self):
        sidecar, _dump, _log = self._make("oversize")
        await sidecar.start()
        with self.assertRaises(SidecarError):
            await sidecar.request("observe", {}, timeout=3)
        self.assertTrue(await self._wait(lambda: not sidecar.alive))
        self.assertGreater(MAX_LINE, 4096)

    # ------------------------------------------------------------ allowlist
    async def test_child_environment_is_allowlisted(self):
        sidecar, _dump, _log = self._make("normal")
        poisoned = {"NODE_OPTIONS": "--require evil", "QQ_TOKEN": "qq-secret",
                    "OPENROUTER_API_KEY": "llm-secret", "DEBUG": "*"}
        with mock.patch.dict(os.environ, poisoned):
            await sidecar.start()
        env_text = (await sidecar.request("observe", {}, timeout=5))["env"]
        lines = set(env_text.splitlines())
        keys = {line.split("=", 1)[0] for line in lines if "=" in line}
        self.assertFalse(keys & set(poisoned), sorted(keys & set(poisoned)))
        self.assertIn("PAWBOT_MC_GUARD_URL=http://127.0.0.1:%d" % LAN_PORT, lines)
        self.assertIn("PAWBOT_MC_GUARD_SECRET=" + SECRET, lines)
        self.assertIn("PAWBOT_MC_WORLD_ID=test-world", lines)
        self.assertIn("PAWBOT_MC_WRITE_ENABLED=false", lines)

    def test_build_env_rejects_bad_urls_and_missing_identity(self):
        base = {}
        ok = build_env({"PAWBOT_MC_GUARD_SECRET": SECRET,
                        "PAWBOT_MC_ACTOR_UUID": ACTOR,
                        "PAWBOT_MC_GUARD_URL": "http://127.0.0.1:6198/v1/permit"},
                       base=base)
        self.assertEqual(ok["PAWBOT_MC_GUARD_URL"], "http://127.0.0.1:6198")
        self.assertEqual(normalize_guard_url("http://127.0.0.1:6198/"),
                         "http://127.0.0.1:6198")
        for url in ("http://localhost:6198", "https://127.0.0.1:6198",
                    "http://user:pw@127.0.0.1:6198", "http://127.0.0.1:6198/x",
                    "http://127.0.0.1:6198?q=1", "http://127.0.0.1:6198#f",
                    "http://[::1]:6198", "http://127.0.0.1", "",
                    "http://127.0.0.1:99999"):
            with self.subTest(url=url), self.assertRaises(SidecarError):
                normalize_guard_url(url)
        for env in ({"PAWBOT_MC_ACTOR_UUID": ACTOR},
                    {"PAWBOT_MC_GUARD_SECRET": SECRET}):
            with self.subTest(env=sorted(env)), self.assertRaises(SidecarError):
                build_env(env, base=base)

    # -------------------------------------------------------------- shutdown
    async def test_stop_sends_shutdown_reaps_and_is_idempotent(self):
        sidecar, _dump, log = self._make("normal")
        await sidecar.start()
        process = sidecar.process
        await sidecar.stop(graceful=True)
        self.assertIsNotNone(process)
        self.assertIsNotNone(process.returncode, "child must be reaped")
        self.assertIsNone(sidecar.process)
        self.assertFalse(sidecar.alive)
        self.assertIn("shutdown", [row["method"] for row in _read_log(log)])
        await sidecar.stop(graceful=True)   # idempotent

    async def test_cancel_without_a_process_is_false(self):
        sidecar, _dump, _log = self._make("normal")
        self.assertFalse(await sidecar.cancel())
        self.assertFalse(await sidecar.shutdown())

    # ----------------------------------------------------------- reconnect
    async def test_connect_with_retry_restarts_and_only_replays_connect(self):
        sidecar, _dump, log = self._make(
            "crash_connect_once", backoff_base=0.01, backoff_max=0.02,
            backoff_attempts=3)
        prepared = []

        async def prepare():
            prepared.append(True)

        result = await sidecar.connect_with_retry(CONNECT, prepare, attempts=3,
                                                  timeout=5)
        self.assertTrue(result["connected"])
        self.assertEqual(len(prepared), 2, "every connect attempt needs a fresh permit")
        self.assertEqual([row["method"] for row in _read_log(log)],
                         ["connect", "connect"])

    async def test_connect_with_retry_propagates_guard_denial(self):
        sidecar, _dump, log = self._make("normal", backoff_attempts=3)
        calls = []

        async def prepare():
            calls.append(True)
            raise GuardDenied("guard_denied")

        with self.assertRaises(GuardDenied):
            await sidecar.connect_with_retry(CONNECT, prepare, attempts=3, timeout=3)
        self.assertEqual(len(calls), 1)
        self.assertEqual([row for row in _read_log(log) if row["method"] == "connect"], [])

    async def test_connect_timeout_is_never_retried(self):
        sidecar, _dump, log = self._make("hang_connect")
        await sidecar.start()
        prepared = []

        async def prepare():
            prepared.append(True)

        with self.assertRaises(asyncio.TimeoutError):
            await sidecar.connect_with_retry(CONNECT, prepare, attempts=3, timeout=0.3)
        self.assertEqual(len(prepared), 1)
        self.assertEqual([row["method"] for row in _read_log(log)], ["connect"])

    async def test_invalid_params_are_rejected_before_the_child_sees_them(self):
        sidecar, _dump, log = self._make("normal")
        await sidecar.start()
        bad = [
            ("skill", {"name": "move", "args": {}, "timeout_sec": 31}),
            ("skill", {"name": "move", "args": {}, "timeout_sec": True}),
            ("skill", {"name": "move", "args": [], "timeout_sec": 1}),
            ("skill", {"name": "", "args": {}, "timeout_sec": 1}),
            ("skill", {"args": {}, "timeout_sec": 1}),
            ("observe", {"extra": 1}),
            ("connect", {**CONNECT, "host": "example.com"}),
            ("connect", {**CONNECT, "port": "12345"}),
            ("connect", {**CONNECT, "version": "1.20.2"}),
            ("connect", {**CONNECT, "auth_mode": "auto"}),
            ("connect", {**CONNECT, "username": "bad name"}),
            ("connect", {**CONNECT, "username": "x" * 17}),
            ("connect", {**CONNECT, "timeout_sec": 31}),
            ("connect", {"host": "127.0.0.1"}),
        ]
        for method, params in bad:
            with self.subTest(method=method, params=params), \
                    self.assertRaises(SidecarError) as ctx:
                await sidecar.request(method, params, timeout=2)
            self.assertEqual(str(ctx.exception), "rpc_invalid_params")
        self.assertEqual(_read_log(log), [])

    async def test_stderr_noise_never_becomes_an_event(self):
        sidecar, _dump, _log = self._make("stderr_noise")
        await sidecar.start()
        await asyncio.sleep(0.05)
        self.assertTrue(sidecar.events.empty())


if __name__ == "__main__":
    unittest.main()
