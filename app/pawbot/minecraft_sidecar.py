"""Supervised bounded stdio transport for the Mineflayer sidecar.

Contract (docs/MINECRAFT-PROTOCOL.md): newline-delimited JSON-RPC 2.0 over
stdin/stdout, max 65536-byte line, stdout carries protocol only. This module
owns the process: spawn, the ``ready`` handshake, bounded correlated requests,
quarantine-on-timeout, and reaping. A timed-out or cancelled mutation is never
replayed; only ``connect`` may be re-issued, and only after the process has
been restarted and a fresh guard permit obtained.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

MAX_LINE = 65536
PROTOCOL_VERSION = 1
METHODS = frozenset({"connect", "observe", "skill", "cancel", "shutdown"})
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_SKILL_TIMEOUT_MAX = 30
_CONNECT_TIMEOUT_MAX = 30
_CONNECT_KEYS = frozenset({"host", "port", "version", "username", "auth_mode",
                           "timeout_sec"})
_SKILL_KEYS = frozenset({"name", "args", "timeout_sec"})
# ``cancel`` and ``shutdown`` keep two reserved slots above the bounded normal
# queue so an emergency stop never has to wait for eight in-flight requests.
RESERVED_SLOTS = 2

# Trusted environment allowlist. QQ/LLM keys, NODE_OPTIONS and preload hooks
# must never reach the child process even if PawBot's environment holds them.
_BASE_ENV_KEYS = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA",
    "USERPROFILE", "HOME", "COMSPEC", "PATHEXT",
})
_MC_ENV_KEYS = frozenset({
    "PAWBOT_MC_GUARD_URL", "PAWBOT_MC_GUARD_SECRET", "PAWBOT_MC_WORLD_ID",
    "PAWBOT_MC_WORLD_PATH", "PAWBOT_MC_ACTOR_UUID", "PAWBOT_MC_PVP_UUIDS",
    "PAWBOT_MC_WRITE_ENABLED", "PAWBOT_MC_PVP_ENABLED", "PAWBOT_MC_AUTH_DIR",
})


class SidecarError(RuntimeError):
    """Static message only — never carry credentials, paths or raw payloads.

    ``code`` carries only the child's integer JSON-RPC error code when one was
    present (never the child's free text), so the caller can tell a signed
    snapshot-busy pause from a genuine denial without parsing a message.
    """

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def normalize_guard_url(value: object) -> str:
    """Return the loopback origin the sidecar expects.

    The Node guard accepts either the plain origin ``http://127.0.0.1:<port>``
    or the exact endpoint ``http://127.0.0.1:<port>/v1/permit``.  PawBot
    always passes the origin so both sides share one canonical form.
    """
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except (ValueError, TypeError):
        raise SidecarError("sidecar_config_invalid") from None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port
            or type(port) is not int or not 1 <= port <= 65535
            or parsed.path not in ("", "/", "/v1/permit")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.netloc not in (f"127.0.0.1:{port}",)):
        raise SidecarError("sidecar_config_invalid")
    return f"http://127.0.0.1:{port}"


def build_env(env: dict, *, base: dict | None = None) -> dict:
    """Allowlist the child environment: OS basics plus PAWBOT_MC_* only.

    Everything else in ``base`` (the parent environment by default) is dropped:
    QQ tokens, model keys, ``NODE_OPTIONS`` and ``DEBUG`` never reach the child.
    """
    source = os.environ if base is None else base
    out = {k: source[k] for k in _BASE_ENV_KEYS if k in source}
    for key in _MC_ENV_KEYS:
        value = env.get(key)
        if value is None:
            value = source.get(key)
        if value is not None:
            out[key] = str(value)
    if not out.get("PAWBOT_MC_GUARD_SECRET") or not out.get("PAWBOT_MC_ACTOR_UUID"):
        raise SidecarError("sidecar_config_invalid")
    out["PAWBOT_MC_GUARD_URL"] = normalize_guard_url(
        out.get("PAWBOT_MC_GUARD_URL") or "http://127.0.0.1:6198")
    return out


def validate_params(method: str, params: object) -> dict:
    """Bound the top-level JSON-RPC params before anything reaches the child.

    The Node side validates every skill argument schema; this host-side check
    only rejects malformed envelopes / unbounded timeouts so a broken caller
    cannot turn into an ambiguous in-flight mutation.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise SidecarError("rpc_invalid_params")
    if method in ("observe", "cancel", "shutdown"):
        if params:
            raise SidecarError("rpc_invalid_params")
        return {}
    if method == "skill":
        if set(params) != _SKILL_KEYS:
            raise SidecarError("rpc_invalid_params")
        if (not isinstance(params["name"], str) or not params["name"]
                or not isinstance(params["args"], dict)):
            raise SidecarError("rpc_invalid_params")
        timeout = params["timeout_sec"]
        if type(timeout) is not int or not 1 <= timeout <= _SKILL_TIMEOUT_MAX:
            raise SidecarError("rpc_invalid_params")
        return params
    if method == "connect":
        if set(params) != _CONNECT_KEYS:
            raise SidecarError("rpc_invalid_params")
        if (params["host"] != "127.0.0.1"
                or type(params["port"]) is not int or not 1 <= params["port"] <= 65535
                or params["version"] != "1.20.1"
                or not isinstance(params["username"], str)
                or not _USERNAME_RE.fullmatch(params["username"])
                or params["auth_mode"] not in ("microsoft", "offline")):
            raise SidecarError("rpc_invalid_params")
        timeout = params["timeout_sec"]
        if type(timeout) is not int or not 1 <= timeout <= _CONNECT_TIMEOUT_MAX:
            raise SidecarError("rpc_invalid_params")
        return params
    raise SidecarError("unknown_method")


class MinecraftSidecar:
    def __init__(self, *, env: dict, entry=None, node="node", max_pending=8,
                 ready_timeout: float = 10.0,
                 backoff_base: float = 0.5, backoff_max: float = 4.0,
                 backoff_attempts: int = 3):
        self.env = dict(env)
        self.entry = str(entry or Path(__file__).resolve().parents[1]
                         / "minecraft-sidecar" / "index.mjs")
        self.node = node
        self.max_pending = max(1, min(8, int(max_pending)))
        self.ready_timeout = max(0.1, float(ready_timeout))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(self.backoff_base, float(backoff_max))
        self.backoff_attempts = max(1, int(backoff_attempts))
        self.process: asyncio.subprocess.Process | None = None
        self.events: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.protocol_version = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._pending_methods: dict[int, str] = {}
        self._seq = 0
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock: asyncio.Lock = asyncio.Lock()
        self._start_lock: asyncio.Lock = asyncio.Lock()
        self._stopping = False
        self._ready = asyncio.Event()

    # ------------------------------------------------------------------ state
    @property
    def alive(self) -> bool:
        """True only when the process is up, the ready handshake completed,
        and no stop has been requested."""
        return (self.process is not None
                and self.process.returncode is None
                and self._ready.is_set()
                and not self._stopping)

    @property
    def pending(self) -> int:
        return len(self._pending)

    # ----------------------------------------------------------- lifecycle
    async def start(self, *, wait_ready: bool = True) -> None:
        async with self._start_lock:
            process = self.process
            if process is not None and process.returncode is None:
                if not wait_ready or self._ready.is_set():
                    return
            else:
                if process is not None:
                    await self._reap(process)   # never leak a dead child
                self._reset_process()
                self._stopping = False
                self._ready.clear()
                self.protocol_version = 0
                env = build_env(self.env)
                try:
                    self.process = await asyncio.create_subprocess_exec(
                        self.node, self.entry,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env, limit=MAX_LINE + 1,
                        cwd=str(Path(self.entry).parent))
                except (OSError, ValueError):
                    self.process = None
                    raise SidecarError("sidecar_unavailable") from None
                self._reader_task = asyncio.create_task(
                    self._read(), name="minecraft-rpc-reader")
                self._stderr_task = asyncio.create_task(
                    self._discard_stderr(), name="minecraft-stderr")
        if not wait_ready:
            return
        try:
            await asyncio.wait_for(self._ready.wait(), self.ready_timeout)
        except asyncio.TimeoutError:
            await self.stop(graceful=False)
            raise SidecarError("sidecar_not_ready") from None
        if not self.alive:
            await self.stop(graceful=False)
            raise SidecarError("sidecar_unavailable") from None

    def _reset_process(self) -> None:
        self.process = None
        self._ready.clear()
        self._reader_task = None
        self._stderr_task = None

    def _fail_pending(self, code: str) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(SidecarError(code))
        self._pending.clear()
        self._pending_methods.clear()

    def _quarantine_now(self, process) -> None:
        """Kill without awaiting; the reader task reaps and fails pending."""
        self._ready.clear()
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
        self._fail_pending("sidecar_quarantined")

    def _push_event(self, params: dict) -> None:
        try:
            if len(json.dumps(params, ensure_ascii=False).encode("utf-8")) > 4096:
                return
        except (TypeError, ValueError):
            return
        if self.events.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.events.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self.events.put_nowait(params)

    async def _discard_stderr(self) -> None:
        # Library stderr can contain auth device codes; never log or forward.
        process = self.process
        with contextlib.suppress(Exception):
            while process and process.stderr and await process.stderr.read(4096):
                pass

    async def _read(self) -> None:
        process = self.process
        abnormal = False
        try:
            while process is not None and process.stdout is not None:
                raw = await process.stdout.readline()
                if not raw:
                    abnormal = True
                    break
                if len(raw) > MAX_LINE:
                    raise SidecarError("rpc_oversized")
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise SidecarError("rpc_malformed") from None
                if not isinstance(row, dict) or row.get("jsonrpc") != "2.0":
                    raise SidecarError("rpc_malformed")
                if row.get("method") == "event":
                    event = row.get("params")
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise SidecarError("rpc_malformed")
                    if event["type"] == "ready":
                        if event.get("protocol_version") != PROTOCOL_VERSION:
                            raise SidecarError("sidecar_protocol_mismatch")
                        self.protocol_version = PROTOCOL_VERSION
                        self._ready.set()
                        continue
                    self._push_event(event)
                    continue
                request_id = row.get("id")
                if type(request_id) is not int:
                    raise SidecarError("rpc_malformed")
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue   # stale/unknown correlation id: bounded, ignored
                self._pending_methods.pop(request_id, None)
                if "error" in row:
                    error = row.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    future.set_exception(SidecarError(
                        "skill_rejected", code=code if type(code) is int else None))
                elif "result" in row:
                    future.set_result(row["result"])
                else:
                    raise SidecarError("rpc_malformed")
        except (ValueError, OSError, SidecarError, asyncio.LimitOverrunError):
            abnormal = True
        finally:
            self._ready.clear()
            self.protocol_version = 0
            self._fail_pending("sidecar_disconnected")
            if not self._stopping and process is not None:
                self._quarantine_now(process)
                await self._reap(process, grace=False)
                self._push_event({"type": "disconnected", "abnormal": abnormal})

    # ---------------------------------------------------------------- request
    async def request(self, method: str, params=None, *, timeout: float = 35) -> object:
        if method not in METHODS:
            raise SidecarError("unknown_method")
        params = validate_params(method, params)
        reserved = method in ("cancel", "shutdown")
        if self._stopping and method != "shutdown":
            raise SidecarError("sidecar_stopping")
        process = self.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise SidecarError("sidecar_unavailable")
        if method != "shutdown" and not self._ready.is_set():
            raise SidecarError("sidecar_unavailable")
        if reserved:
            if len(self._pending) >= self.max_pending + RESERVED_SLOTS:
                raise SidecarError("queue_full")
        else:
            normal = sum(1 for name in self._pending_methods.values()
                         if name not in ("cancel", "shutdown"))
            if normal >= self.max_pending:
                raise SidecarError("queue_full")
        self._seq += 1
        request_id = self._seq
        body = (json.dumps({"jsonrpc": "2.0", "id": request_id,
                            "method": method, "params": params},
                           allow_nan=False, separators=(",", ":")).encode() + b"\n")
        if len(body) > MAX_LINE:
            raise SidecarError("rpc_oversized")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._pending_methods[request_id] = method
        try:
            async with self._write_lock:
                if self.process is not process or process.stdin is None:
                    raise SidecarError("sidecar_unavailable")
                process.stdin.write(body)
                await process.stdin.drain()
            return await asyncio.wait_for(future, timeout=max(0.1, min(65, timeout)))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Quarantine: cannot prove whether a mutation executed. Kill the
            # process; the caller must re-attest, take a fresh backup, and
            # choose a fresh intention before reconnecting.
            self._quarantine_now(process)
            raise
        except (BrokenPipeError, ConnectionResetError):
            raise SidecarError("sidecar_disconnected") from None
        finally:
            self._pending.pop(request_id, None)
            self._pending_methods.pop(request_id, None)

    async def cancel(self, *, timeout: float = 2.0) -> bool:
        """Best-effort ``cancel``; quarantine flag stays with the child.

        Never replays skills. Returns True only for a correlated JSON-RPC result.
        """
        if self.process is None or self.process.returncode is not None:
            return False
        try:
            result = await self.request("cancel", {}, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except (SidecarError, asyncio.TimeoutError, OSError):
            return False
        return isinstance(result, dict)

    async def shutdown(self, *, timeout: float = 2.0) -> bool:
        """Ask the child to drain/exit; ``stop`` still reaps it afterwards."""
        if self.process is None or self.process.returncode is not None:
            return False
        try:
            result = await self.request("shutdown", {}, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except (SidecarError, asyncio.TimeoutError, OSError):
            return False
        return isinstance(result, dict)

    async def connect_with_retry(self, params: dict, prepare, *,
                                 attempts: int | None = None,
                                 timeout: float = 35) -> object:
        """Restart → prepare (cheap guard preflight) → connect, with backoff.

        ``prepare`` is an async callable invoked before every attempt. It is a non-snapshot
        preflight; the child obtains the single fresh ``connect`` permit (and therefore its
        mandatory snapshot) immediately before it opens the game socket. Only ``connect`` is
        replayed; a quarantined attempt is never retried, and a timeout never loops.
        """
        total = self.backoff_attempts if attempts is None else max(1, int(attempts))
        validate_params("connect", params)   # never restart a child for bad input
        last: Exception | None = None
        for attempt in range(total):
            if attempt:
                delay = min(self.backoff_max,
                            self.backoff_base * (2 ** (attempt - 1)))
                if delay > 0:
                    await asyncio.sleep(delay)
            if not self.alive:
                await self.stop(graceful=False)
                try:
                    await self.start()
                except SidecarError as exc:
                    last = exc
                    continue
            await prepare()   # fresh guard attestation; must not be swallowed
            try:
                return await self.request("connect", params, timeout=timeout)
            except asyncio.TimeoutError:
                raise   # request() already quarantined the child; never retry
            except SidecarError as exc:
                if str(exc) not in ("sidecar_disconnected", "sidecar_unavailable"):
                    raise
                last = exc
                await self.stop(graceful=False)
        raise last or SidecarError("sidecar_unavailable")

    # ------------------------------------------------------------------ stop
    async def _reap(self, process, *, grace: bool = True) -> None:
        """Wait/terminate/kill escalating until the child is reaped."""
        if process.returncode is not None:
            with contextlib.suppress(Exception):
                await process.wait()
            return
        if grace:
            try:
                await asyncio.wait_for(process.wait(), 3)
                return
            except asyncio.TimeoutError:
                pass
        with contextlib.suppress(ProcessLookupError, OSError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
            return
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), 2)

    async def stop(self, *, graceful: bool = True) -> None:
        self._stopping = True
        process = self.process
        tasks = (self._reader_task, self._stderr_task)
        if process is not None:
            if graceful and process.returncode is None:
                with contextlib.suppress(Exception):
                    await self.shutdown()
            await self._reap(process, grace=graceful)
        self._ready.clear()
        self.protocol_version = 0
        self._fail_pending("sidecar_stopped")
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reset_process()
