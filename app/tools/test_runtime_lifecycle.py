"""批次 2 离线验收：DataRoot / 单实例锁 / 运行时生命周期 / WebUI 控制面 / 真实 run.py。

覆盖：
  * 数据根优先级、联接、失败 fail-closed；
  * 单实例锁格式、活 pid 拒绝、未知 fail-closed、stale 归档接管、nonce 释放；
  * RuntimeLifecycle：真实 Store 写入 → shutdown → flush → 可重读；事件闸门；取消进行中任务；
  * GET /api/control/ready、POST /api/control/shutdown：loopback + 独立 Bearer、禁用/拒绝/flush 失败；
  * 真实 run.py 子进程：ready → shutdown → 退出码 10 → 重读数据；第二实例拒绝且不写业务数据；
    强杀残留锁 → 下次启动 stale 接管。

不联网、不发 QQ、不调用 LLM；全部使用临时目录与临时端口。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from pawbot import lifecycle, tasks  # noqa: E402
from pawbot.store import Store  # noqa: E402
from pawbot.webui import WebUI  # noqa: E402

TOKEN = "test-control-" + "a" * 32
PANEL = "test-panel-token"


def real(path) -> str:
    return os.path.realpath(os.path.abspath(str(path)))


def is_link(path) -> bool:
    try:
        if os.path.islink(path):
            return True
        return bool(getattr(os.path, "isjunction", lambda _p: False)(path))
    except OSError:
        return False


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)


def http_call(port: int, method: str, path: str, token: str | None = None,
              body=None, timeout: float = 5.0):
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        return response.status, payload
    finally:
        conn.close()


class DataRootResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pawbot-py-root-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_root_is_app_data(self):
        info = lifecycle.resolve_data_root(self.tmp, env={})
        self.assertEqual(real(info.root), real(os.path.join(self.tmp, "data")))
        self.assertEqual(info.source, "default")
        self.assertFalse(info.external)

    def test_env_root_wins_and_clean_install_gets_alias(self):
        external = os.path.join(self.tmp, "external")
        info = lifecycle.resolve_data_root(self.tmp, env={lifecycle.DATA_ROOT_ENV: external})
        self.assertEqual(real(info.root), real(external))
        self.assertTrue(info.external)
        self.assertTrue(os.path.isdir(external))
        self.assertTrue(is_link(os.path.join(self.tmp, "data")), "app/data 应成为联接")
        self.assertEqual(real(os.path.join(self.tmp, "data")), real(external))

    def test_env_mismatch_with_real_app_data_fails_closed(self):
        app_data = os.path.join(self.tmp, "data")
        write_json(os.path.join(app_data, "config.json"), {"llm": {"api_key": "sk-KEEP"}})
        external = os.path.join(self.tmp, "external")
        with self.assertRaises(lifecycle.DataRootError) as caught:
            lifecycle.resolve_data_root(self.tmp, env={lifecycle.DATA_ROOT_ENV: external})
        self.assertIn("data", str(caught.exception))
        self.assertFalse(is_link(app_data), "失败时不得把真目录换成联接")
        self.assertEqual(read_json(os.path.join(app_data, "config.json"))["llm"]["api_key"], "sk-KEEP")
        self.assertFalse(os.path.exists(external), "失败时不应悄悄建外部目录")

    def test_env_external_junction_is_accepted_after_alias_exists(self):
        external = os.path.join(self.tmp, "external-junction")
        first = lifecycle.resolve_data_root(
            self.tmp, env={lifecycle.DATA_ROOT_ENV: external})
        self.assertEqual(real(first.root), real(external))
        # Host 迁移成功后 app/data 是指向外的联接；第二次解析（run.py 收到 env）必须
        # 接受这个联接，而不是因为「真实目录不一致」拒绝启动。
        second = lifecycle.resolve_data_root(
            self.tmp, env={lifecycle.DATA_ROOT_ENV: external})
        self.assertEqual(real(second.root), real(external))
        self.assertEqual(real(lifecycle.runtime_lock_path(second.root)),
                         real(os.path.join(external, lifecycle.RUNTIME_LOCK_FILE)))

    def test_env_equal_to_app_data_is_accepted(self):
        info = lifecycle.resolve_data_root(
            self.tmp, env={lifecycle.DATA_ROOT_ENV: os.path.join(self.tmp, "data")})
        self.assertEqual(real(info.root), real(os.path.join(self.tmp, "data")))
        self.assertFalse(info.external)

    def test_env_overlapping_root_is_rejected_without_alias(self):
        app_data = os.path.join(self.tmp, "data")
        candidates = [self.tmp, os.path.dirname(self.tmp), os.path.join(app_data, "nested")]
        if os.name == "nt":
            candidates.append(os.path.join(self.tmp.upper(), "DATA", "nested"))
        for requested in candidates:
            with self.subTest(requested=requested):
                with self.assertRaises(lifecycle.DataRootError):
                    lifecycle.resolve_data_root(self.tmp, env={lifecycle.DATA_ROOT_ENV: requested})
                self.assertFalse(os.path.lexists(app_data))

    def test_control_token_requires_non_empty_env_value(self):
        self.assertEqual(lifecycle.control_token_from_env({}), "")
        self.assertEqual(lifecycle.control_token_from_env({lifecycle.CONTROL_TOKEN_ENV: "   "}), "")
        self.assertEqual(
            lifecycle.control_token_from_env({lifecycle.CONTROL_TOKEN_ENV: " tok "}), "tok")

    def test_peer_is_loopback(self):
        for peer in (("127.0.0.1", 1), ("127.0.0.2", 1), ("::1", 1, 0, 0),
                     ("::ffff:127.0.0.1", 1, 0, 0)):
            self.assertTrue(lifecycle.peer_is_loopback(peer), peer)
        for peer in (("192.168.1.4", 1), ("10.0.0.1", 1), ("::ffff:10.0.0.1", 1, 0, 0),
                     ("2001:db8::1", 1, 0, 0), None, (), ("not-an-ip", 1)):
            self.assertFalse(lifecycle.peer_is_loopback(peer), peer)


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pawbot-py-lock-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_lock_format_and_release(self):
        lock = lifecycle.acquire_runtime_lock(
            self.root, host_pid=os.getpid(), version="1.2.3")
        self.assertEqual(sorted(lock.keys()),
                         ["host_pid", "nonce", "pid", "started_at", "version"])
        self.assertEqual(lock["pid"], os.getpid())
        self.assertEqual(lock["host_pid"], os.getpid())
        self.assertEqual(lock["version"], "1.2.3")
        self.assertRegex(lock["nonce"], r"^[a-f0-9]{32}$")
        self.assertTrue(lifecycle.release_runtime_lock(self.root, lock))
        self.assertFalse(os.path.exists(lifecycle.runtime_lock_path(self.root)))

    def test_live_pid_is_rejected(self):
        first = lifecycle.acquire_runtime_lock(self.root, version="first")
        with self.assertRaises(lifecycle.RuntimeLocked) as caught:
            lifecycle.acquire_runtime_lock(self.root, version="second")
        self.assertIn(str(os.getpid()), str(caught.exception))
        self.assertEqual(lifecycle.read_runtime_lock(self.root)["nonce"], first["nonce"])
        lifecycle.release_runtime_lock(self.root, first)

    def test_dead_pid_is_archived_and_taken_over(self):
        dead = {"pid": 999999, "started_at": "2026-01-01T00:00:00+00:00",
                "host_pid": 123, "version": "crashed", "nonce": "b" * 32}
        os.makedirs(self.root, exist_ok=True)
        write_json(lifecycle.runtime_lock_path(self.root), dead)
        lock = lifecycle.acquire_runtime_lock(self.root, version="new")
        self.assertNotEqual(lock["nonce"], dead["nonce"])
        archive_dir = os.path.join(self.root, lifecycle.LOCK_ARCHIVE_DIR)
        archived = [name for name in os.listdir(archive_dir) if name.startswith("stale-")]
        self.assertTrue(archived, "stale 锁应归档")
        self.assertEqual(read_json(os.path.join(archive_dir, archived[0]))["nonce"], dead["nonce"])
        self.assertTrue(os.path.exists(lifecycle.lock_guard_path(self.root)), "guard 文件不应被删除")
        self.assertFalse(os.path.exists(os.path.join(archive_dir, "takeover.lock")),
                         "不应再创建可陈旧的 takeover.lock")
        lifecycle.release_runtime_lock(self.root, lock)

    def test_unknown_liveness_fails_closed(self):
        dead = {"pid": 999999, "started_at": "2026-01-01T00:00:00+00:00",
                "host_pid": 0, "version": "x", "nonce": "c" * 32}
        os.makedirs(self.root, exist_ok=True)
        write_json(lifecycle.runtime_lock_path(self.root), dead)
        with self.assertRaises(lifecycle.RuntimeLockUnknown):
            lifecycle.acquire_runtime_lock(self.root, is_alive=lambda _pid: None)
        self.assertEqual(read_json(lifecycle.runtime_lock_path(self.root))["nonce"], dead["nonce"])

    def test_corrupt_lock_fails_closed(self):
        os.makedirs(self.root, exist_ok=True)
        with open(lifecycle.runtime_lock_path(self.root), "w", encoding="utf-8") as handle:
            handle.write("not-json")
        with self.assertRaises(lifecycle.RuntimeLockUnknown):
            lifecycle.acquire_runtime_lock(self.root)
        with open(lifecycle.runtime_lock_path(self.root), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "not-json")

    def test_release_checks_nonce(self):
        lock = lifecycle.acquire_runtime_lock(self.root)
        self.assertFalse(lifecycle.release_runtime_lock(self.root, {**lock, "nonce": "f" * 32}))
        self.assertTrue(os.path.exists(lifecycle.runtime_lock_path(self.root)))
        self.assertTrue(lifecycle.release_runtime_lock(self.root, lock))

    def test_pid_alive_probe(self):
        self.assertTrue(lifecycle.pid_alive(os.getpid()))
        self.assertFalse(lifecycle.pid_alive(999999))
        self.assertIsNone(lifecycle.pid_alive(0))

    def test_inspect(self):
        lock = lifecycle.acquire_runtime_lock(self.root)
        info = lifecycle.inspect_runtime_lock(self.root)
        self.assertTrue(info["present"])
        self.assertTrue(info["alive"])
        self.assertFalse(info["stale"])
        self.assertEqual(info["lock"]["nonce"], lock["nonce"])
        lifecycle.release_runtime_lock(self.root, lock)


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.mkdtemp(prefix="pawbot-py-life-")
        self.events = []

        async def on_event(_conn, _data):
            self.events.append(_data)

        self.store = Store(os.path.join(self.root, "sessions.json"))
        self.bot = SimpleNamespace(
            store=self.store,
            base_dir=self.root,
            server=SimpleNamespace(on_event=on_event, connections={}),
        )
        self.lock = lifecycle.acquire_runtime_lock(self.root, version="test")
        self.life = lifecycle.RuntimeLifecycle(
            data_root=self.root, lock=self.lock, control_token=TOKEN)
        self.life.bind_bot(self.bot)
        self.life.install_event_gate(self.bot)

    async def asyncTearDown(self):
        try:
            self.life.cleanup()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)

    async def test_ready_reflects_db_and_store_dirty(self):
        state = self.life.ready_state()
        self.assertTrue(state["ready"])
        self.assertTrue(state["db_writable"])
        self.assertFalse(state["store_dirty"])
        self.assertFalse(state["shutting_down"])
        self.store.append("s1", "user", "hello")
        state = self.life.ready_state()
        self.assertTrue(state["store_dirty"])

    async def test_event_gate_drops_events_after_shutdown_begins(self):
        await self.bot.server.on_event(None, {"n": 1})
        self.assertEqual(len(self.events), 1)
        result = await self.life.request_shutdown()
        self.assertTrue(result["ok"])
        await asyncio.wait_for(self.life.wait_stopped(), 2)
        await self.bot.server.on_event(None, {"n": 2})
        self.assertEqual(len(self.events), 1, "停机后不得再处理新事件")
        self.assertFalse(self.life.ready_state()["ready"])

    async def test_shutdown_flushes_real_store_and_flushes_are_readable(self):
        self.store.append("s1", "user", "记住这句话")
        self.assertTrue(self.store.dirty)
        result = await self.life.request_shutdown()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["flushed"], result)
        self.assertFalse(self.store.dirty)
        await asyncio.wait_for(self.life.wait_stopped(), 2)
        payload = read_json(self.store.path)
        self.assertEqual(payload["s1"]["messages"][-1]["content"], "记住这句话")
        second = await self.life.request_shutdown()
        self.assertTrue(second.get("already_shutting_down"), second)

    async def test_shutdown_cancels_inflight_tasks(self):
        slow = tasks.spawn(asyncio.sleep(30), name="slow")
        worker = asyncio.create_task(asyncio.sleep(30))
        conn = SimpleNamespace(worker=worker)
        self.bot.server.connections = {1: conn}
        result = await self.life.request_shutdown()
        self.assertTrue(result["ok"])
        await asyncio.sleep(0.05)
        self.assertTrue(slow.done(), "登记的后台任务应被取消")
        self.assertTrue(slow.cancelled())
        self.assertTrue(worker.done(), "连接 worker 应被取消")
        self.assertTrue(worker.cancelled())

    async def test_flush_failure_is_not_reported_as_success(self):
        def boom(*_args, **_kwargs):
            raise OSError("disk full")
        self.store.flush = boom
        result = await self.life.request_shutdown()
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["flushed"])
        self.assertIn("disk full", result["error"])
        self.assertNotEqual(self.life.exit_code, lifecycle.EXIT_CONTROL_SHUTDOWN)
        await asyncio.wait_for(self.life.wait_stopped(), 2)
        self.assertFalse(self.life.ready_state()["ready"])

    async def test_unwritable_store_dir_blocks_ready(self):
        os.makedirs(os.path.join(self.root, "tmp"), exist_ok=True)
        os.rmdir(os.path.join(self.root, "tmp"))
        with open(os.path.join(self.root, "tmp"), "w", encoding="utf-8") as handle:
            handle.write("not-a-dir")
        state = self.life.ready_state()
        self.assertFalse(state["db_writable"])
        self.assertFalse(state["ready"])

    async def test_cleanup_releases_lock(self):
        self.life.cleanup()
        self.assertFalse(os.path.exists(lifecycle.runtime_lock_path(self.root)))
        self.life.cleanup()  # 幂等


class _FakeWriter:
    def __init__(self, peer):
        self.peer = peer
        self.data = b""

    def get_extra_info(self, name, default=None):
        return self.peer if name == "peername" else default

    def write(self, data):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None

    def response(self):
        head, _, body = self.data.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        return status, json.loads(body.decode("utf-8"))


class _FakeReader:
    """只给 _control 的直接单元测试用；被调用即失败（这些负例应在读 body 前返回）。"""

    async def readexactly(self, _length):
        raise AssertionError("负例不应读取 body")


class WebUIControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pawbot-py-webui-")
        self.harness = None

    async def asyncTearDown(self):
        if self.harness:
            webui, life = self.harness
            await webui.stop()
            life.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def start(self, control_token=TOKEN, panel_token=PANEL):
        root = os.path.join(self.tmp, "data")
        os.makedirs(root, exist_ok=True)

        async def on_event(_conn, _data):
            return None

        self.store = Store(os.path.join(root, "sessions.json"))
        self.bot = SimpleNamespace(
            store=self.store,
            base_dir=root,
            server=SimpleNamespace(on_event=on_event, connections={}),
        )
        lock = lifecycle.acquire_runtime_lock(root, version="webui-test")
        life = lifecycle.RuntimeLifecycle(
            data_root=root, lock=lock, control_token=control_token)
        life.bind_bot(self.bot)
        life.install_event_gate(self.bot)
        webui = WebUI(self.bot, host="127.0.0.1", port=0, token=panel_token,
                      control_token=control_token, lifecycle=life)
        await webui.start()
        port = int(webui._server.sockets[0].getsockname()[1])
        self.webui = webui
        self.life = life
        self.port = port
        self.harness = (webui, life)

    async def request(self, method, path, token=None, body=None):
        return await asyncio.to_thread(
            http_call, self.port, method, path, token, body)

    async def test_control_disabled_without_token(self):
        await self.start(control_token="")
        status, payload = await self.request("GET", "/api/control/ready")
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload.get("error"), "control_disabled")
        status, payload = await self.request("POST", "/api/control/shutdown", token=PANEL)
        self.assertEqual(status, 403, payload)
        self.assertFalse(self.life.shutting_down)

    async def test_auth_requires_non_empty_control_bearer_only(self):
        await self.start()
        status, _ = await self.request("GET", "/api/control/ready")
        self.assertEqual(status, 401)
        status, _ = await self.request("GET", "/api/control/ready", token=PANEL)
        self.assertEqual(status, 401, "面板 token 不能当控制 token")
        status, _ = await self.request("GET", f"/api/control/ready?token={TOKEN}")
        self.assertEqual(status, 401, "查询参数不能认证")
        status, _ = await self.request("GET", "/api/control/ready", token="wrong")
        self.assertEqual(status, 401)
        status, payload = await self.request("GET", "/api/control/ready", token=TOKEN)
        self.assertEqual(status, 200, payload)
        self.assertNotIn("token", json.dumps(payload).lower())

    async def test_ready_payload_is_real(self):
        await self.start()
        status, payload = await self.request("GET", "/api/control/ready", token=TOKEN)
        self.assertEqual(status, 200, payload)
        for key in ("ready", "db_writable", "store_dirty"):
            self.assertIn(key, payload)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["db_writable"])
        self.assertFalse(payload["store_dirty"])
        self.store.append("s1", "user", "dirty")
        status, payload = await self.request("GET", "/api/control/ready", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(payload["store_dirty"])

    async def test_shutdown_flushes_store_then_ready_turns_false(self):
        await self.start()
        self.store.append("s1", "assistant", "要落盘的话")
        status, payload = await self.request(
            "POST", "/api/control/shutdown", token=TOKEN, body={"reason": "test"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("ok"), True, payload)
        self.assertEqual(payload.get("flushed"), True, payload)
        await asyncio.wait_for(self.life.wait_stopped(), 2)
        saved = read_json(os.path.join(self.tmp, "data", "sessions.json"))
        self.assertEqual(saved["s1"]["messages"][-1]["content"], "要落盘的话")
        status, payload = await self.request("GET", "/api/control/ready", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertFalse(payload["ready"])
        self.assertTrue(payload["shutting_down"])

    async def test_flush_failure_returns_500_not_fake_success(self):
        await self.start()
        self.store.append("s1", "user", "写不进去")

        def boom(*_args, **_kwargs):
            raise OSError("disk full")
        self.store.flush = boom
        status, payload = await self.request("POST", "/api/control/shutdown", token=TOKEN)
        self.assertEqual(status, 500, payload)
        self.assertFalse(payload.get("ok"), payload)
        self.assertFalse(payload.get("flushed"), payload)
        self.assertIn("disk full", payload.get("error", ""))
        await asyncio.wait_for(self.life.wait_stopped(), 2)
        self.assertNotEqual(self.life.exit_code, lifecycle.EXIT_CONTROL_SHUTDOWN)

    async def test_remote_peer_rejected_before_token_check(self):
        await self.start()
        writer = _FakeWriter(("192.168.1.9", 43210))
        await self.webui._control(
            writer, _FakeReader(), "POST", "/api/control/shutdown",
            {"authorization": f"Bearer {TOKEN}"})
        status, payload = writer.response()
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload.get("error"), "forbidden_peer")
        self.assertFalse(self.life.shutting_down)

    async def test_unknown_control_path_and_method(self):
        await self.start()
        status, _ = await self.request("GET", "/api/control/nope", token=TOKEN)
        self.assertEqual(status, 404)
        status, _ = await self.request("GET", "/api/control/shutdown", token=TOKEN)
        self.assertEqual(status, 405)
        status, _ = await self.request("POST", "/api/control/ready", token=TOKEN)
        self.assertEqual(status, 405)


    async def _open(self):
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def _close(self, writer):
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def test_control_auth_rejects_before_reading_declared_body(self):
        """鉴权前不得无期限等 body：错误 token 必须立刻 401。"""
        await self.start()
        reader, writer = await self._open()
        try:
            writer.write(
                b"POST /api/control/shutdown HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer wrong-token\r\n"
                b"Content-Length: 1000\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.read(4096), timeout=3)
            self.assertIn(b" 401 ", head)
            self.assertFalse(self.life.shutting_down)
        finally:
            await self._close(writer)

    async def test_control_body_read_has_timeout_after_auth(self):
        """鉴权通过后也不无期限等 body：stalled body 必须超时回 4xx。"""
        await self.start()
        reader, writer = await self._open()
        started = time.monotonic()
        try:
            writer.write(
                b"POST /api/control/shutdown HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode("ascii")
                + b"Content-Length: 10\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.read(4096), timeout=6)
            elapsed = time.monotonic() - started
            status = int(head.split(b" ")[1])
            self.assertIn(status, (400, 408), head[:200])
            self.assertLess(elapsed, 5.5, f"body 读取必须在有时限内失败（{elapsed:.2f}s）")
            self.assertFalse(self.life.shutting_down, "body 不完整时不能执行 shutdown")
        finally:
            await self._close(writer)

    async def test_control_oversized_body_rejected_before_read(self):
        await self.start()
        reader, writer = await self._open()
        try:
            writer.write(
                b"POST /api/control/shutdown HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode("ascii")
                + b"Content-Length: 1000000\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.read(4096), timeout=3)
            self.assertIn(b" 413 ", head)
            self.assertFalse(self.life.shutting_down)
        finally:
            await self._close(writer)


class RealRunPyTests(unittest.TestCase):
    """真实 run.py 子进程：验证控制面接线、退出码、锁与 Store 真正的落盘。"""

    PY_LAUNCHER = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pawbot-runpy-")
        cls.app = os.path.join(cls.tmp, "app")
        os.makedirs(cls.app)
        shutil.copy2(APP_DIR / "run.py", os.path.join(cls.app, "run.py"))
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
        for name in ("pawbot", "plugins", "web"):
            shutil.copytree(APP_DIR / name, os.path.join(cls.app, name), ignore=ignore)
        cls.procs = []

    @classmethod
    def tearDownClass(cls):
        for proc in cls.procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(10)
                except Exception:
                    pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.data = os.path.join(self.app, "data")
        shutil.rmtree(self.data, ignore_errors=True)
        os.makedirs(self.data)
        self.server_port = free_port()
        self.webui_port = free_port()
        self.panel_token = PANEL
        self.control_token = TOKEN
        write_json(os.path.join(self.data, "config.json"), {
            "server": {"host": "127.0.0.1", "port": self.server_port,
                       "path": "/ws", "access_token": ""},
            "webui": {"host": "127.0.0.1", "port": self.webui_port,
                      "token": self.panel_token},
            "tts": {"enabled": False},
            "proactive": {"enabled": False},
            "reminders": {"enabled": False},
            "sync": {"enabled": False},
        })
        write_json(os.path.join(self.data, "sessions.json"), {
            "s1": {"messages": [{"role": "user", "content": "旧会话", "who": "主人",
                                  "ts": 1.0}],
                   "archive": [], "summary": "", "updated": 1},
        })
        self.lock_path = lifecycle.runtime_lock_path(self.data)
        self.proc = None

    def tearDown(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                http_call(self.webui_port, "POST", "/api/control/shutdown",
                          token=self.control_token, timeout=5)
            except Exception:
                pass
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)

    def _env(self, with_control=True):
        env = os.environ.copy()
        env["PYTHONPATH"] = self.app
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # T1：真实子进程不接受运行环境里可能存在的用户数据根 / DSH bridge 凭证；
        # 显式指向本测试的临时 dataDir，且清掉所有 bridge URL/token。
        for key in ("PAWBOT_DSH_BRIDGE_URL", "PAWBOT_DSH_BRIDGE_TOKEN",
                    "PAWBOT_DSH_BRAIN_URL", "PAWBOT_DSH_BRAIN_TOKEN",
                    "PAWBOT_PYTHON_EXE"):
            env.pop(key, None)
        env[lifecycle.DATA_ROOT_ENV] = self.data
        env["PAWBOT_HOST_PID"] = str(os.getpid())
        env["PAWBOT_RUNTIME_VERSION"] = "test"
        if with_control:
            env[lifecycle.CONTROL_TOKEN_ENV] = self.control_token
        else:
            env.pop(lifecycle.CONTROL_TOKEN_ENV, None)
        return env

    def _command(self, use_launcher=False):
        if use_launcher:
            launcher = shutil.which("py")
            if launcher:
                return [launcher, "-3.13", "run.py"]
        return [sys.executable, "run.py"]

    def _spawn(self, use_launcher=False, with_control=True):
        env = self._env(with_control=with_control)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            self._command(use_launcher), cwd=self.app, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", **kwargs)
        self.proc = proc
        self.procs.append(proc)
        return proc

    def _output(self, proc):
        try:
            out, err = proc.communicate(timeout=1)
        except Exception:
            return "<output unavailable>"
        return (out or "") + "\n" + (err or "")

    def _wait_ready(self, proc, timeout=40):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if proc.poll() is not None:
                self.fail(f"run.py 提前退出 code={proc.returncode}\n{self._output(proc)}")
            try:
                status, payload = http_call(
                    self.webui_port, "GET", "/api/control/ready",
                    token=self.control_token, timeout=2)
                if status == 200 and payload and payload.get("ready"):
                    return payload
                last = (status, payload)
            except (OSError, http.client.HTTPException) as exc:
                last = repr(exc)
            time.sleep(0.1)
        self.fail(f"等待 /api/control/ready 超时：{last}\n{self._output(proc) if proc.poll() is not None else ''}")

    def test_shutdown_flushes_real_store_and_exits_10(self):
        proc = self._spawn()
        self._wait_ready(proc)
        lock = read_json(self.lock_path)
        self.assertEqual(sorted(lock.keys()),
                         ["host_pid", "nonce", "pid", "started_at", "version"])
        self.assertEqual(lock["host_pid"], os.getpid())
        self.assertEqual(lock["version"], "test")

        # 通过真实 WebUI 写 Store（clear 会标 dirty，但不会立即落盘）。
        status, payload = http_call(
            self.webui_port, "POST", "/api/session/clear",
            token=self.panel_token, body={"session_id": "s1"})
        self.assertEqual(status, 200, payload)

        status, payload = http_call(
            self.webui_port, "POST", "/api/control/shutdown",
            token=self.control_token, body={"reason": "acceptance"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"), payload)
        self.assertTrue(payload.get("flushed"), payload)

        code = proc.wait(20)
        self.assertEqual(code, lifecycle.EXIT_CONTROL_SHUTDOWN,
                         f"exit={code}\n{self._output(proc)}")
        self.assertFalse(os.path.exists(self.lock_path), "正常退出必须释放锁")
        saved = read_json(os.path.join(self.data, "sessions.json"))
        self.assertNotIn("s1", saved, "shutdown 应把被清除后的 Store 真正落盘")

    def test_check_mode_exits_zero_and_releases_lock(self):
        env = self._env()
        result = subprocess.run(
            self._command() + ["--check"], cwd=self.app, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90)
        self.assertEqual(result.returncode, 0,
                         f"check exit={result.returncode}\n{result.stdout}\n{result.stderr}")
        self.assertFalse(os.path.exists(self.lock_path), "--check 正常退出也要释放锁")

    def test_py_launcher_pid_differs_from_lock_pid(self):
        launcher = shutil.which("py")
        if not launcher:
            self.skipTest("没有 py launcher")
        proc = self._spawn(use_launcher=True)
        self._wait_ready(proc)
        lock = read_json(self.lock_path)
        self.assertNotEqual(lock["pid"], proc.pid,
                            "py.exe launcher 的 pid 不应等于实际 Python 的 pid")
        self.assertTrue(lifecycle.pid_alive(lock["pid"]))
        status, payload = http_call(
            self.webui_port, "POST", "/api/control/shutdown",
            token=self.control_token, body={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(proc.wait(20), lifecycle.EXIT_CONTROL_SHUTDOWN)

    def test_second_instance_rejected_without_business_writes(self):
        proc = self._spawn()
        self._wait_ready(proc)
        lock_before = read_json(self.lock_path)
        config_before = Path(self.data, "config.json").read_bytes()
        sessions_before = Path(self.data, "sessions.json").read_bytes()

        second = subprocess.run(
            self._command(), cwd=self.app, env=self._env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=40)
        self.assertEqual(second.returncode, lifecycle.EXIT_LOCKED,
                         f"second exit={second.returncode}\n{second.stdout}\n{second.stderr}")
        self.assertIn("单实例", (second.stdout or "") + (second.stderr or ""))
        lock_after = read_json(self.lock_path)
        self.assertEqual(lock_after["nonce"], lock_before["nonce"], "第二个实例不得改锁")
        self.assertEqual(Path(self.data, "config.json").read_bytes(), config_before)
        self.assertEqual(Path(self.data, "sessions.json").read_bytes(), sessions_before,
                         "第二个实例不得写业务数据")

        status, payload = http_call(
            self.webui_port, "POST", "/api/control/shutdown",
            token=self.control_token, body={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(proc.wait(20), lifecycle.EXIT_CONTROL_SHUTDOWN)

    def test_force_kill_leaves_stale_lock_and_next_start_takes_over(self):
        proc = self._spawn()
        self._wait_ready(proc)
        first = read_json(self.lock_path)
        proc.kill()  # 等价 taskkill /F：atexit 不会执行
        proc.wait(15)
        self.assertTrue(os.path.exists(self.lock_path), "强杀后锁应残留")
        self.assertEqual(read_json(self.lock_path)["nonce"], first["nonce"])

        second = self._spawn()
        self._wait_ready(second)
        taken = read_json(self.lock_path)
        self.assertNotEqual(taken["nonce"], first["nonce"], "新实例应接管并替换锁")
        archive_dir = os.path.join(self.data, lifecycle.LOCK_ARCHIVE_DIR)
        self.assertTrue(os.path.isdir(archive_dir), "应归档 stale 锁")
        archived = [name for name in os.listdir(archive_dir) if name.startswith("stale-")]
        self.assertTrue(archived)
        self.assertEqual(read_json(os.path.join(archive_dir, archived[0]))["nonce"], first["nonce"])
        self.assertTrue(os.path.exists(lifecycle.lock_guard_path(self.data)),
                        "guard 文件必须保留")

        status, payload = http_call(
            self.webui_port, "POST", "/api/control/shutdown",
            token=self.control_token, body={})
        self.assertEqual(status, 200, payload)
        self.assertEqual(second.wait(20), lifecycle.EXIT_CONTROL_SHUTDOWN)
        self.assertFalse(os.path.exists(self.lock_path))



class RuntimeLockRegressionTests(unittest.TestCase):
    """L1–L6 负例：不靠墙钟、活 owner 不被抢、guard 有界、崩溃可恢复。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pawbot-py-lock2-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, lock):
        os.makedirs(self.root, exist_ok=True)
        write_json(lifecycle.runtime_lock_path(self.root), lock)

    def _dead(self, **overrides):
        lock = {"pid": 999999, "started_at": "2026-01-01T00:00:00+00:00",
                "host_pid": 0, "version": "crashed", "nonce": "d" * 32}
        lock.update(overrides)
        return lock

    def test_live_owner_with_ancient_timestamp_is_never_stolen(self):
        ancient = self._dead(pid=os.getpid(),
                             started_at="2020-01-01T00:00:00+00:00",
                             nonce="a" * 32)
        self._write(ancient)
        with self.assertRaises(lifecycle.RuntimeLocked):
            lifecycle.acquire_runtime_lock(self.root, timeout=0.2)
        self.assertEqual(read_json(lifecycle.runtime_lock_path(self.root))["nonce"], ancient["nonce"])
        archive = os.path.join(self.root, lifecycle.LOCK_ARCHIVE_DIR)
        stale = [n for n in os.listdir(archive) if n.startswith("stale-")]
        self.assertEqual(stale, [], "活 owner 的锁不得被归档")

    def test_unknown_owner_with_ancient_timestamp_stays_fail_closed(self):
        ancient = self._dead(started_at="2020-01-01T00:00:00+00:00")
        self._write(ancient)
        with self.assertRaises(lifecycle.RuntimeLockUnknown):
            lifecycle.acquire_runtime_lock(self.root, is_alive=lambda _pid: None, timeout=0.2)
        self.assertEqual(read_json(lifecycle.runtime_lock_path(self.root))["nonce"], ancient["nonce"])

    def test_dead_owner_with_future_timestamp_is_still_taken_over(self):
        future = self._dead(started_at="2035-01-01T00:00:00+00:00")
        self._write(future)
        lock = lifecycle.acquire_runtime_lock(self.root, version="new", timeout=2)
        self.assertNotEqual(lock["nonce"], future["nonce"])
        lifecycle.release_runtime_lock(self.root, lock)

    def test_crash_temp_without_official_lock_does_not_block(self):
        os.makedirs(self.root, exist_ok=True)
        with open(lifecycle.runtime_lock_path(self.root) + ".tmp-1-deadbeef", "w",
                  encoding="utf-8") as handle:
            handle.write('{"pid": 12')
        lock = lifecycle.acquire_runtime_lock(self.root, version="after-crash", timeout=2)
        self.assertEqual(lock["pid"], os.getpid())
        self.assertEqual(read_json(lifecycle.runtime_lock_path(self.root))["nonce"], lock["nonce"])
        lifecycle.release_runtime_lock(self.root, lock)

    def _holder_script(self, body: str) -> str:
        return ("import sys, time\n"
                f"sys.path.insert(0, {str(APP_DIR)!r})\n"
                "from pawbot import lifecycle\n"
                "root = sys.argv[1]\n" + body)

    def _clean_env(self):
        env = os.environ.copy()
        for key in (lifecycle.DATA_ROOT_ENV, lifecycle.CONTROL_TOKEN_ENV,
                    "PAWBOT_DSH_BRIDGE_TOKEN", "PAWBOT_DSH_BRIDGE_URL",
                    "PAWBOT_DSH_BRAIN_TOKEN", "PAWBOT_DSH_BRAIN_URL"):
            env.pop(key, None)
        env["PYTHONPATH"] = str(APP_DIR)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def test_guard_contention_is_bounded_and_released_on_kill(self):
        script = self._holder_script(
            "with lifecycle.guard_lock(root, timeout=10):\n"
            "    print('guard', flush=True)\n"
            "    time.sleep(30)\n")
        proc = subprocess.Popen([sys.executable, "-c", script, self.root],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                env=self._clean_env())
        def close_holder():
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self.addCleanup(close_holder)
        self.assertEqual(proc.stdout.readline().strip(), "guard")
        started = time.monotonic()
        with self.assertRaises(lifecycle.RuntimeLockUnknown) as caught:
            lifecycle.acquire_runtime_lock(self.root, timeout=0.2)
        elapsed = time.monotonic() - started
        self.assertIn("超时", str(caught.exception))
        self.assertLess(elapsed, 4.0, "guard 争用必须有界，不允许忙等")
        self.assertFalse(os.path.exists(lifecycle.runtime_lock_path(self.root)))
        proc.kill()
        proc.wait(10)
        lock = lifecycle.acquire_runtime_lock(self.root, timeout=3)
        lifecycle.release_runtime_lock(self.root, lock)

    def test_concurrent_stale_takeover_has_exactly_one_winner(self):
        self._write(self._dead())
        script = self._holder_script(
            "try:\n"
            "    lock = lifecycle.acquire_runtime_lock(root, timeout=5)\n"
            "except lifecycle.RuntimeLocked:\n"
            "    print('LOCKED', flush=True)\n"
            "    sys.exit(0)\n"
            "except lifecycle.RuntimeLockUnknown as exc:\n"
            "    print('UNKNOWN ' + str(exc), flush=True)\n"
            "    sys.exit(0)\n"
            "print('WIN ' + lock['nonce'], flush=True)\n"
            "time.sleep(2.0)\n"
            "lifecycle.release_runtime_lock(root, lock)\n")
        procs = [subprocess.Popen([sys.executable, "-c", script, self.root],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace",
                                  env=self._clean_env())
                 for _ in range(4)]
        outputs = []
        try:
            for proc in procs:
                out, err = proc.communicate(timeout=30)
                outputs.append((out or "").strip())
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(10)
        winners = [line for out in outputs for line in out.splitlines() if line.startswith("WIN ")]
        others = [line for out in outputs for line in out.splitlines()
                  if not line.startswith("WIN ")]
        self.assertEqual(len(winners), 1, f"outputs={outputs}")
        for line in others:
            self.assertTrue(line.startswith("LOCKED"), f"loser={line} outputs={outputs}")
        archive = os.path.join(self.root, lifecycle.LOCK_ARCHIVE_DIR)
        stale = [n for n in os.listdir(archive) if n.startswith("stale-")]
        self.assertEqual(len(stale), 1, f"旧 stale 只能归档一次：{stale}")
        self.assertFalse(os.path.exists(lifecycle.runtime_lock_path(self.root)))

    def test_live_stable_migration_reservation_blocks_before_root_creation(self):
        target = os.path.join(self.root, "fresh-data")
        reservation = lifecycle.runtime_lock.migration_reservation_root(target)
        lock = {**self._dead(), "pid": os.getppid()}
        write_json(os.path.join(reservation, lifecycle.RUNTIME_LOCK_FILE), lock)
        result = lifecycle.runtime_lock.acquire(target, owner_pid=os.getpid())
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "LOCKED")
        self.assertFalse(os.path.exists(target), "rejected startup must not recreate the migration gap")

    def test_corrupt_stable_migration_reservation_is_failclosed(self):
        target = os.path.join(self.root, "fresh-data")
        reservation = lifecycle.runtime_lock.migration_reservation_root(target)
        os.makedirs(reservation)
        with open(os.path.join(reservation, lifecycle.RUNTIME_LOCK_FILE), "w", encoding="utf-8") as handle:
            handle.write("{")
        result = lifecycle.runtime_lock.acquire(target, owner_pid=os.getpid())
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "LOCK_UNKNOWN")
        self.assertFalse(os.path.exists(target))

    def test_dead_stable_migration_reservation_does_not_permanently_block(self):
        target = os.path.join(self.root, "fresh-data")
        reservation = lifecycle.runtime_lock.migration_reservation_root(target)
        write_json(os.path.join(reservation, lifecycle.RUNTIME_LOCK_FILE), self._dead())
        lock = lifecycle.acquire_runtime_lock(target)
        self.assertEqual(lock["pid"], os.getpid())
        self.assertTrue(lifecycle.release_runtime_lock(target, lock))

    def test_release_without_nonce_does_not_delete(self):
        lock = lifecycle.acquire_runtime_lock(self.root, timeout=2)
        self.assertFalse(lifecycle.release_runtime_lock(self.root, {**lock, "nonce": "f" * 32}))
        self.assertTrue(os.path.exists(lifecycle.runtime_lock_path(self.root)))
        self.assertTrue(lifecycle.release_runtime_lock(self.root, lock))


class ControlTokenEnvTests(unittest.TestCase):
    def test_take_control_token_pops_env(self):
        env = {lifecycle.CONTROL_TOKEN_ENV: "  tok-value  "}
        self.assertEqual(lifecycle.take_control_token(env), "tok-value")
        self.assertNotIn(lifecycle.CONTROL_TOKEN_ENV, env)

    def test_take_control_token_missing_is_empty(self):
        env = {}
        self.assertEqual(lifecycle.take_control_token(env), "")


def _load_runpy():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pawbot_run_module_for_tests", APP_DIR / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MainExitCodeTests(unittest.TestCase):
    """R1：main 的退出码必须在 finally 全部完成后再决定。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pawbot-main-")
        self.runpy = _load_runpy()
        self.holder = {}
        self.log_patch = mock.patch.object(self.runpy.log, "to_file")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_main(self, *, check, auto_shutdown=False, fail_stop_flush=False,
                  stop_raises=False, fail_cleanup_release=False):
        root = self.root
        holder = self.holder

        class TestLifecycle(lifecycle.RuntimeLifecycle):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["life"] = self

            async def wait_stopped(self):
                if auto_shutdown:
                    await self.request_shutdown()
                await super().wait_stopped()

        class FakeServer:
            def __init__(self):
                self.on_event = None
                self.connections = {}
                self.port = 6199
                self.path = "/ws"

        class FakeBot:
            def __init__(self, base_dir):
                self.base_dir = base_dir
                self.server = FakeServer()
                self.config = SimpleNamespace(
                    data={"webui": {"port": 0}}, get=lambda *_a, **_k: 0)
                self.store = Store(os.path.join(root, "sessions.json"))

            async def start(self):
                return None

            async def stop(self):
                if stop_raises:
                    raise OSError("stop failed")
                if fail_stop_flush:
                    def boom(*_args, **_kwargs):
                        raise OSError("disk full")
                    self.store.flush = boom

        class FakeWebUI:
            def __init__(self, *_args, **_kwargs):
                pass

            async def start(self):
                return None

            async def stop(self):
                return None

        info = lifecycle.DataRoot(root=root, app_data=os.path.join(root, "data"),
                                  source="test")
        patches = [
            mock.patch.object(self.runpy, "resolve_data_root", lambda _base: info),
            mock.patch.object(self.runpy, "RuntimeLifecycle", TestLifecycle),
            mock.patch.object(self.runpy, "Bot", FakeBot),
            mock.patch.object(self.runpy, "WebUI", FakeWebUI),
        ]
        if fail_cleanup_release:
            patches.append(mock.patch.object(
                lifecycle, "release_runtime_lock", lambda *_a, **_k: False))
        env_patch = mock.patch.dict(
            os.environ, {lifecycle.CONTROL_TOKEN_ENV: "main-test-control-token"})
        try:
            for item in patches:
                item.start()
            env_patch.start()
            code = asyncio.run(self.runpy.main(
                SimpleNamespace(check=check, port=None, webui=None)))
            env_left = os.environ.get(lifecycle.CONTROL_TOKEN_ENV)
        finally:
            env_patch.stop()
            for item in reversed(patches):
                item.stop()
        return code, holder, env_left

    def test_control_shutdown_flush_failure_after_ack_must_not_return_10(self):
        code, _holder, _env = self._run_main(
            check=False, auto_shutdown=True, fail_stop_flush=True)
        self.assertNotEqual(code, 0, "最终 flush 失败不能返回 0")
        self.assertNotEqual(code, lifecycle.EXIT_CONTROL_SHUTDOWN,
                            "最终 flush 失败不能伪报成功控制停机（10）")
        self.assertEqual(code, lifecycle.EXIT_ERROR)

    def test_control_shutdown_stop_failure_must_not_return_10(self):
        code, _holder, _env = self._run_main(
            check=False, auto_shutdown=True, stop_raises=True)
        self.assertNotIn(code, (0, lifecycle.EXIT_CONTROL_SHUTDOWN))

    def test_check_flush_failure_must_not_return_0(self):
        code, _holder, _env = self._run_main(
            check=True, fail_stop_flush=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, lifecycle.EXIT_ERROR)

    def test_cleanup_release_failure_must_not_return_0(self):
        code, _holder, _env = self._run_main(
            check=True, fail_cleanup_release=True)
        self.assertNotEqual(code, 0, "cleanup 释放锁失败不能返回 0")

    def test_control_shutdown_success_returns_10(self):
        code, holder, _env = self._run_main(check=False, auto_shutdown=True)
        self.assertEqual(code, lifecycle.EXIT_CONTROL_SHUTDOWN)
        self.assertEqual(holder["life"].exit_code, lifecycle.EXIT_CONTROL_SHUTDOWN)

    def test_control_token_is_removed_from_env_before_business_code(self):
        code, _holder, env_left = self._run_main(check=True)
        self.assertEqual(code, lifecycle.EXIT_OK)
        self.assertIsNone(env_left, "控制 token 必须从 env 删除，避免业务子进程继承")


if __name__ == "__main__":
    unittest.main()
