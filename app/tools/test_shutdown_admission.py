"""Shutdown must finish work before success and reject new mutating HTTP work.

These independent regressions use real Store + loopback HTTP, temporary roots,
and synthetic tokens. They never start Bot, QQ, a browser, or a model request.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pawbot import onebot, tasks
from pawbot.lifecycle import RuntimeLifecycle
from pawbot.store import Store
from pawbot.webui import WebUI


class ShutdownAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pawbot-shutdown-admission-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "sessions.json"))
        self.store.append("s", "user", "must remain unless admitted work clears it")
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()

        async def manual_send(*_args):
            self.started.set()
            try:
                await self.release.wait()
                self.store.append("s", "assistant", "late external send")
            finally:
                self.finished.set()
            return {}

        async def stop():
            return None

        self.bot = SimpleNamespace(
            store=self.store,
            base_dir=self.tmp.name,
            server=SimpleNamespace(connections={}),
            clear_session=self.store.clear,
            manual_send=manual_send,
            stop=stop,
        )
        self.life = RuntimeLifecycle(data_root=self.tmp.name, exit_delay=60)
        self.life.bind_bot(self.bot)
        self.web = WebUI(self.bot, host="127.0.0.1", port=0,
                         control_token="admission-control-test", lifecycle=self.life)
        bind_webui = getattr(self.life, "bind_webui", None)
        if callable(bind_webui):
            bind_webui(self.web)
        await self.web.start()
        self.port = self.web._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.release.set()
        await self.web.stop()
        self.life.cleanup()

    async def post(self, path, payload):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        body = json.dumps(payload).encode()
        writer.write((f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                      "Connection: close\r\n\r\n").encode() + body)
        await writer.drain()
        try:
            response = await asyncio.wait_for(reader.read(), 5)
            return int(response.split(b" ", 2)[1]) if response else None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @unittest.skipUnless(os.name == "nt", "Windows read-only replacement semantics")
    async def test_ready_rejects_read_only_store_destination(self):
        import stat

        self.store.flush(force=True)
        try:
            os.chmod(self.store.path, stat.S_IREAD)
            state = self.life.ready_state()
            self.assertFalse(state["db_writable"],
                             "writable tmp/ is insufficient when Store replacement is forbidden")
            self.assertFalse(state["ready"])
        finally:
            os.chmod(self.store.path, stat.S_IWRITE | stat.S_IREAD)

    async def test_successful_ack_has_no_pending_cancellation_resistant_writer(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def writer():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await release.wait()
            self.store.append("s", "assistant", "after ack")

        task = tasks.spawn(writer(), name="admission-regression-writer")
        try:
            await started.wait()
            ack = await asyncio.wait_for(self.life.request_shutdown(), 6)
            self.assertFalse(ack.get("ok") and not task.done(),
                             "success ack cannot precede completion of a Store writer")
        finally:
            release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)

    async def test_successful_ack_does_not_lose_disconnected_onebot_worker(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await release.wait()
            self.store.append("s", "assistant", "late disconnected worker")

        task = asyncio.create_task(worker())

        async def close_connections():
            task.cancel()
            self.bot.server.connections.clear()
            await asyncio.sleep(0)

        self.bot.server.connections = {1: SimpleNamespace(worker=task)}
        self.bot.server.stop = close_connections
        try:
            await started.wait()
            ack = await asyncio.wait_for(self.life.request_shutdown(), 6)
            self.assertFalse(ack.get("ok") and not task.done(),
                             "disconnect must not hide a still-running worker from shutdown")
        finally:
            release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)

    async def test_shutdown_tracks_worker_disconnected_before_shutdown(self):
        started = asyncio.Event()
        release = asyncio.Event()
        worker = None

        async def on_event(_conn, _data):
            nonlocal worker
            worker = asyncio.current_task()
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue  # Simulate a library finishing asynchronous cancellation cleanup.
            self.store.append("s", "assistant", "disconnected worker cleanup")
            raise asyncio.CancelledError

        received = False

        async def recv():
            nonlocal received
            if not received:
                received = True
                return json.dumps({"post_type": "message"})
            await started.wait()
            return None

        async def close():
            return None

        ws = SimpleNamespace(headers={"x-self-id": "23"}, path="/ws",
                             closed=False, recv=recv, close=close)
        server = onebot.OneBotServer("127.0.0.1", 0, on_event=on_event)
        self.bot.server = server
        writer = SimpleNamespace(get_extra_info=lambda _name: ("127.0.0.1", 0))
        try:
            with mock.patch.object(onebot.wsproto, "accept", new=mock.AsyncMock(return_value=ws)):
                await server._handle(None, writer)
            await asyncio.sleep(0)
            self.assertEqual(server.connections, {})
            self.assertIsNotNone(worker)
            self.assertFalse(worker.done())
            ack = await asyncio.wait_for(self.life.request_shutdown(), 6)
            self.assertFalse(ack.get("ok") and not worker.done(),
                             "disconnect before shutdown must not hide a still-writing worker")
        finally:
            release.set()
            if worker is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 1)

    async def test_successful_ack_cannot_leave_sync_writer_thread_alive(self):
        import threading
        release = threading.Event()
        started = threading.Event()

        def write_after_release():
            started.set()
            release.wait()
            self.store.append("s", "assistant", "late sync thread")

        thread = threading.Thread(target=write_after_release, daemon=True)
        self.bot._auto_pull_thread = thread
        thread.start()
        try:
            await asyncio.to_thread(started.wait, 1)
            ack = await asyncio.wait_for(self.life.request_shutdown(), 6)
            self.assertFalse(ack.get("ok") and thread.is_alive(),
                             "successful ack cannot leave a background sync writer alive")
        finally:
            release.set()
            await asyncio.to_thread(thread.join, 1)

    async def test_shutdown_rejects_new_session_mutation(self):
        ack = await asyncio.wait_for(self.life.request_shutdown(), 6)
        self.assertTrue(ack.get("ok"), ack)
        before = self.store.history("s")
        try:
            status = await self.post("/api/session/clear", {"session_id": "s"})
        except (ConnectionError, OSError):
            status = None  # Closing the listener is also a valid admission gate.
        self.assertEqual(self.store.history("s"), before,
                         "a post-shutdown handler must not mutate Store")
        self.assertIn(status, (None, 409, 503))

    async def test_successful_ack_waits_for_inflight_panel_send(self):
        request = asyncio.create_task(self.post(
            "/api/send", {"target": "synthetic-target", "message": "never sent"}))
        try:
            await asyncio.wait_for(self.started.wait(), 3)
            ack = await asyncio.wait_for(self.life.request_shutdown(), 6)
            self.assertFalse(ack.get("ok") and not self.finished.is_set(),
                             "success ack cannot leave an HTTP send running")
        finally:
            self.release.set()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(request, 2)


if __name__ == "__main__":
    unittest.main()
