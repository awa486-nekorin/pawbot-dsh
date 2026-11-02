"""高可用 OneBot relay 后端客户端（server.mode=relay）。

拓扑（A 方案，服务器中继唯一权威）::

    NapCat ──/onebot/reverse──► relay ◄──/onebot/backend── PawBot(local / server)
                                  │
                                  └── HTTP /ha/content（内容快照）

本文件只实现「后端」这一侧：外拨 WS、hello/lease、heartbeat、ready 门禁、
内容副本同步与生命周期回调。中继（lease 权威）由 Node 侧实现，Python 端
不参与 fencing 判定——relay 说 fence 就立刻停手。

关键约束（与中继契约逐条对应）
--------------------------------
* 业务控制帧以 ``$relay`` 字段标识，且 ``$relay`` 必须排在 JSON 第一位；
  控制帧永远不会进入业务事件去重/分发路径。
* hello 之后**不是** ready：必须先拉取/应用内容，再发 ``ready``，收到
  ``ready_ack`` 才允许收发业务。握手完成但未 ready 时 ``get()`` 返回
  ``None``，``can_emit()`` 为假，所有外发动作 fail closed。
* lease 的有效期只按本机单调时钟（``time.monotonic``）从 ack 推算，
  不信任墙上时钟；heartbeat_ack 续期，超时/fenced 立即失效。
* ``can_emit`` 同时要求：自己的 lease 仍有效、已 ready、upstream 在线、
  连接仍是当前连接。``get()`` 会隐藏所有非活动连接。
* 新 claim 会立即 fence 旧 owner（中继侧）；旧连接上的 echo 只回旧连接，
  新连接不会收到旧 owner 的 echo（每个 Connection 自己维护 pending）。中继还会
  在旧 generation 的 echo key 过期前拒绝复用（`error: echo_reused`），所以复用
  echo key 的动作会失败而不是收到别人的回执。
* 内容副本只在 lease owner 时 push；standby / warm standby 只 pull。
  拉取遇到 409 冲突时保留本地数据，**不会**自动把旧本地数据推回去。
* push 撞上 409（canonical 已被推进）后进入冲突阻塞态：下一轮**先 pull**
  canonical 对齐 CAS base，成功之后才恢复 push；pull 也失败就保持阻塞，并通过
  ``content_blocked`` / ``content_blocked_reason`` 暴露，绝不重推旧本地数据。
* ready 的门禁是双向的：中继要求 ready 里的 content_revision 等于它当前的
  canonical revision，客户端要求 ready_ack 原样回显自己刚发出的值。不一致时
  中继回 ``error: content_stale``，客户端重新 pull 后用新 revision 重试（有界），
  绝不把旧 revision 当成同步完成。

数据丢失窗口（重要，不要过度承诺）
----------------------------------
这是「有界最终同步」，不是零丢失实时迁移：

* 最后一次成功 push 之后到进程崩溃/断网之间的内容改动会丢（有界于
  ``server.relay_sync_interval`` 加上一次 push 的耗时）。
* 网络分区时旧 owner 可能仍以为 lease 有效（本地单调时钟未超时），
  中继会在自己的 TTL 到期后 fence 它；这段时间内的外发会被中继丢弃，
  调用方可能看到「已发送但没回执」。
* 中继不重放事件，也不提供 exactly-once；重连后只保证「从当前时刻
  继续」，断线期间的事件就是丢了。
* 内容快照只覆盖 content 子树（人设/记忆/日记等），不含 config/state；
  state 与 NapCat 会话状态不在迁移范围内。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import random
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from . import tasks, wsproto
from .log import log
from .onebot import Connection, OneBotEndpoint, _close_quietly

BACKEND_PATH = "/onebot/backend"
CONTENT_PATH = "/ha/content"

PRIORITIES = ("local", "server")
DEFAULT_PRIORITY = "server"

# 握手/激活阶段等待控制帧的上限（内容同步本身另有 sync_timeout）。
DEFAULT_HANDSHAKE_TIMEOUT = 10.0

# 一次连接尝试里 ready 的最多发送次数：中继发现 content revision 在
# hello→pull→ready 之间前进了会回 content_stale，客户端重新 pull 后重试。
READY_MAX_ATTEMPTS = 3


# ---------------------------------------------------------------- URL / 配置


def parse_relay_url(url: str) -> tuple[str, int, str, bool]:
    """把 ``ws(s)://host:port/path`` 拆成 (host, port, path, secure)。

    与 ``onebot.parse_forward_url`` 不同，这里允许 wss://（relay 跨机部署
    默认应走 TLS）。缺省路径是 ``/onebot/backend``。
    """
    parts = urlsplit(str(url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in ("ws", "wss"):
        raise ValueError("server.forward_url 只支持 ws:// 或 wss://")
    host = parts.hostname or ""
    if not host:
        raise ValueError("server.forward_url 缺少主机名")
    secure = scheme == "wss"
    port = int(parts.port or (443 if secure else 80))
    path = parts.path or BACKEND_PATH
    if parts.query:
        path = f"{path}?{parts.query}"
    return host, port, path, secure


def derive_content_url(ws_url: str) -> str:
    """从 forward WS URL 推导 HTTP(S) relay 根地址（不含 /ha/content）。"""
    parts = urlsplit(str(ws_url or "").strip())
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme.lower())
    if not scheme or not parts.netloc:
        return ""
    return urlunsplit((scheme, parts.netloc, "", "", ""))


def derive_content_url_from_host(host: str, port: int, secure: bool) -> str:
    scheme = "https" if secure else "http"
    host = str(host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}"


def normalize_content_url(value: str) -> str:
    """把显式 relay_content_url 归一成 ContentReplica 的 base_url。

    允许用户写成 ``https://relay:6198``、``https://relay:6198/`` 或
    ``https://relay:6198/ha/content``；统一去掉结尾斜杠与 ``/ha/content``，
    由 ha_content 自己拼端点。带反向代理前缀（``/relay``）时保留前缀。
    """
    parts = urlsplit(str(value or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("server.relay_content_url 必须是 http(s):// 地址")
    path = parts.path.rstrip("/")
    if path.endswith(CONTENT_PATH):
        path = path[: -len(CONTENT_PATH)].rstrip("/")
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def derive_content_root(config: Any) -> str | None:
    """从 Config.path 推导 content 根：``<data>/state/config.json`` → ``<data>/content``。

    兼容尚未迁移的旧位置 ``<data>/config.json`` → ``<data>/content``。
    推导不出来时返回 None（调用方可以显式传 content_root 或 replica）。
    """
    path = getattr(config, "path", None)
    if not path:
        return None
    try:
        state_dir = os.path.dirname(os.path.abspath(str(path)))
        if os.path.basename(state_dir).lower() == "state":
            data_dir = os.path.dirname(state_dir)
        else:
            data_dir = state_dir
        return os.path.join(data_dir, "content")
    except (OSError, ValueError):
        return None


def advance_backoff(current: float, initial: float, maximum: float,
                    outcome: str) -> float:
    """下一次重连的基础等待时间（纯函数，便于测试）。

    * ``outcome == "ready"``：成功连上过，回到最快重连档位。
    * 其他（连接失败 / hello_reject / 激活失败）：在现有基础上翻倍，
      但**不重置**到最快档位——standby 被拒绝时不能退化成高频重试。
    """
    initial = max(0.01, float(initial))
    maximum = max(initial, float(maximum))
    if outcome == "ready":
        return initial
    return min(max(initial, float(current) * 2.0), maximum)


# ---------------------------------------------------------------- 内部状态


class _Attempt:
    """一次 WS 连接尝试的控制帧等待状态（不跨连接复用）。"""

    def __init__(self, conn: Connection):
        loop = asyncio.get_running_loop()
        self.conn = conn
        self.hello: asyncio.Future = loop.create_future()
        self.ready_ack: asyncio.Future = loop.create_future()
        self.closed = asyncio.Event()
        self.lease: dict | None = None
        self.heartbeat_ms = 0
        self.ttl_ms = 0
        self.became_ready = False
        self.released = False
        # 最近一次发出去的 ready 里的 content_revision；ready_ack 必须原样回显。
        self.ready_content_revision: int | None = None


def _resolve(fut: asyncio.Future, value) -> None:
    if not fut.done():
        fut.set_result(value)


# ---------------------------------------------------------------- RelayClient


class RelayClient(OneBotEndpoint):
    """server.mode=relay 的后端传输：lease + ready 门禁 + 内容副本。"""

    mode = "relay"

    def __init__(
        self,
        host: str,
        port: int,
        path: str = BACKEND_PATH,
        access_token: str = "",
        *,
        priority: str = DEFAULT_PRIORITY,
        backend_id: str = "",
        on_event: Callable[[Connection, dict], Awaitable[None]] | None = None,
        on_acquired: Callable[["RelayClient", dict], Any] | None = None,
        on_lost: Callable[["RelayClient"], Any] | None = None,
        before_release: Callable[["RelayClient"], Any] | None = None,
        replica: Any | None = None,
        content_root: str | None = None,
        content_url: str = "",
        require_content: bool = True,
        sync_interval: float = 3.0,
        sync_timeout: float = 10.0,
        secure: bool = False,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
        connect_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        dedup_size: int = 1024,
    ):
        super().__init__(on_event=on_event)
        self.host = str(host)
        self.port = int(port)
        self.path = str(path or BACKEND_PATH)
        self.access_token = str(access_token or "")
        self.secure = bool(secure)
        self.priority = self._normalize_priority(priority)
        configured_id = str(backend_id or "").strip()
        if len(configured_id) > 64:
            log.warn("server.relay_backend_id 超过 relay 允许的 64 字符，已截断")
            configured_id = configured_id[:64]
        # 保持配置语义：留空就发空字符串（relay 允许），不擅自改写成随机 id。
        self.backend_id = configured_id

        self.on_acquired = on_acquired
        self.on_lost = on_lost
        self.before_release = before_release
        self.replica = replica
        self.content_root = content_root
        self.content_url = str(content_url or "")
        self.require_content = bool(require_content)
        self.sync_interval = max(0.1, float(sync_interval or 3.0))
        self.sync_timeout = max(0.1, float(sync_timeout or 10.0))

        self.reconnect_initial = max(0.01, float(reconnect_initial or 1.0))
        self.reconnect_max = max(self.reconnect_initial,
                                 float(reconnect_max or 30.0))
        self.connect_timeout = (float(connect_timeout)
                                if connect_timeout else None)
        self.dedup_size = max(1, int(dedup_size))

        self._runner: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._stopping = False
        self._conn: Connection | None = None
        self._attempt: _Attempt | None = None
        self._replica_obj: Any | None = None

        # lease / ready / upstream 状态：全部由控制帧驱动。
        self.lease: dict | None = None
        self.lease_id: str | None = None
        self.epoch: str | None = None
        self.revision = 0
        self.content_revision = 0
        self.upstream = False
        self.ready = False
        self._lease_deadline: float | None = None
        self._lost_notified = False
        # 内容 CAS 冲突后的阻塞态：先 pull canonical 更新 base，本轮不 push；
        # 拉取也失败就保持阻塞（fail closed），并且绝不回推旧本地数据。
        self._content_conflict = False
        self._content_conflict_reason: str | None = None
        # 业务事件去重（与 forward 一致）；控制帧在 _dispatch 里先行返回，
        # 永远不会经过这张表。
        self._delivered: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------ 构造

    @staticmethod
    def _normalize_priority(value: Any) -> str:
        priority = str(value or DEFAULT_PRIORITY).strip().lower()
        if priority not in PRIORITIES:
            log.warn(f"未知 server.relay_priority={priority!r}，按 {DEFAULT_PRIORITY} 处理")
            return DEFAULT_PRIORITY
        return priority

    @classmethod
    def from_config(cls, config, *, on_event=None, on_acquired=None,
                    on_lost=None, before_release=None, replica=None,
                    content_root=None) -> "RelayClient":
        """从 Config（或任何有 get(dotted, default) 的对象）构造。

        WS 目标仍使用既有 ``server.forward_*`` / ``server.access_token`` 键；
        relay 专用键是 ``server.relay_*``。``forward_url`` 为空时回落到
        host/port/path，且把 forward 的历史默认路径 ``/ws`` 映射为
        relay 的后端端点 ``/onebot/backend``。
        """
        url = str(config.get("server.forward_url", "") or "").strip()
        if url:
            host, port, path, secure = parse_relay_url(url)
        else:
            host = str(config.get("server.forward_host", "127.0.0.1") or "127.0.0.1")
            port = int(config.get("server.forward_port", 6199) or 6199)
            path = str(config.get("server.forward_path", "") or "").strip()
            if not path or path.rstrip("/") == "/ws":
                path = BACKEND_PATH
            secure = False

        content_url = str(config.get("server.relay_content_url", "") or "").strip()
        if content_url:
            content_url = normalize_content_url(content_url)
        elif url:
            content_url = derive_content_url(url)
        else:
            content_url = derive_content_url_from_host(host, port, secure)

        if content_root is None:
            content_root = derive_content_root(config)

        return cls(
            host=host,
            port=port,
            path=path,
            access_token=str(config.get("server.access_token", "") or ""),
            priority=config.get("server.relay_priority", DEFAULT_PRIORITY),
            backend_id=str(config.get("server.relay_backend_id", "") or ""),
            on_event=on_event,
            on_acquired=on_acquired,
            on_lost=on_lost,
            before_release=before_release,
            replica=replica,
            content_root=content_root,
            content_url=content_url,
            require_content=bool(config.get("server.relay_require_content", True)),
            sync_interval=float(config.get("server.relay_sync_interval", 3.0) or 3.0),
            sync_timeout=float(config.get("server.relay_sync_timeout", 10.0) or 10.0),
            secure=secure,
        )

    # ------------------------------------------------------------ 公开状态

    def _is_lease_owner(self) -> bool:
        """当前是否仍是有效 lease owner（不要求 upstream）。"""
        conn = self._conn
        return bool(
            self.ready
            and self.lease_id
            and conn is not None
            and not conn.ws.closed
            and not self._lease_expired()
        )

    def can_emit(self) -> bool:
        """只有「自己的 lease 有效 + 已 ready + upstream 在线」才允许外发。"""
        return bool(self._is_lease_owner() and self.upstream)

    @property
    def content_blocked(self) -> bool:
        """内容是否处于冲突阻塞态（需要先 pull 恢复，不能推送本地旧数据）。"""
        return bool(self._content_conflict)

    @property
    def content_blocked_reason(self) -> str | None:
        """阻塞原因（``push_conflict`` / ``pull_conflict``）；未阻塞为 None。"""
        return self._content_conflict_reason

    def _mark_content_conflict(self, reason: str, message: str) -> None:
        if not self._content_conflict:
            log.warn(message)
        self._content_conflict = True
        self._content_conflict_reason = reason

    def _clear_content_conflict(self) -> None:
        self._content_conflict = False
        self._content_conflict_reason = None

    def get(self, self_id: int | None = None) -> Connection | None:
        """隐藏所有非活动连接：standby / 初始化 / lease 过期时返回 None。"""
        if not self.can_emit():
            return None
        conn = self._conn
        if conn is None:
            return None
        # 与 OneBotEndpoint.get 兼容：self_id 还未知（0）时仍返回唯一活动连接。
        if self_id and conn.self_id and conn.self_id != self_id:
            return None
        return conn

    async def _allow_send(self, conn: Connection) -> bool:
        """Connection.call 在真正写 socket 前会再问一次，确保 fence 后不发出。"""
        if conn is not self._conn or not self.can_emit():
            return False
        return await super()._allow_send(conn)

    # ------------------------------------------------------------ 生命周期

    async def start(self):
        """启动后台外拨循环（不监听本地端口）与内容同步循环。"""
        if self._runner is not None and not self._runner.done():
            return
        self._stopping = False
        self._lost_notified = False
        self._runner = tasks.spawn(self._run(), name="onebot-relay")
        if self._sync_enabled() and (self._sync_task is None or self._sync_task.done()):
            self._sync_task = tasks.spawn(self._sync_loop(), name="onebot-relay-sync")

    async def stop(self):
        """先 flush 内容并 bye，再关闭连接；可重复调用。"""
        self._stopping = True
        conn, lease = self._conn, self.lease
        if (conn is not None and lease is not None
                and not conn.ws.closed and not self._lease_expired()):
            # 主动释放不是「丢 lease」：不要触发 on_lost。
            self._lost_notified = True
            try:
                await self._flush_before_release()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 停机清理不能中断
                log.warn(f"relay 释放前内容 flush 失败：{type(e).__name__} {e}")
            try:
                if not self._lease_expired():
                    await self._send_control(conn, self._control(
                        "bye", lease_id=lease["lease_id"], epoch=lease["epoch"],
                        revision=lease["revision"]))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 中继可能已断开
                log.debug(f"relay bye 发送失败（忽略）：{type(e).__name__} {e}")
            attempt = self._attempt
            if attempt is not None:
                attempt.released = True

        runner, self._runner = self._runner, None
        if runner is not None and not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001 - 停机不应因清理异常中断
                log.debug(f"relay 停止时清理异常：{type(e).__name__} {e}")

        sync, self._sync_task = self._sync_task, None
        if sync is not None and not sync.done():
            sync.cancel()
            try:
                await sync
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                log.debug(f"relay 同步停止时清理异常：{type(e).__name__} {e}")

        for conn in list(self.connections.values()):
            await _close_quietly(conn)
        self.connections.clear()
        if self._conn is not None:
            await _close_quietly(self._conn)
            self._conn = None
        self._attempt = None
        self._reset_lease()
        # 只关闭自己创建的副本；注入的 replica 归调用方所有。
        replica_obj, self._replica_obj = self._replica_obj, None
        if replica_obj is not None:
            close = getattr(replica_obj, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:  # noqa: BLE001 - 关闭失败不影响停机
                    log.debug(f"relay 内容副本关闭失败：{type(e).__name__} {e}")

    # ------------------------------------------------------------ 重连循环

    async def _run(self):
        backoff = self.reconnect_initial
        while not self._stopping:
            outcome = await self._run_attempt()
            if self._stopping:
                break
            if outcome == "ready":
                # 成功 ready 过：断线重连回到最快档位。
                backoff = self.reconnect_initial
            delay = self._jitter(backoff)
            log.debug(f"relay 将在 {delay:.2f}s 后重连（outcome={outcome}）")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            backoff = advance_backoff(backoff, self.reconnect_initial,
                                      self.reconnect_max, outcome)

    def _jitter(self, base: float) -> float:
        """有界抖动：50%~100% 的基础等待，避免多后端同时重连。"""
        base = max(0.01, float(base))
        return random.uniform(base * 0.5, base)

    async def _run_attempt(self) -> str:
        """一次完整尝试：connect → hello → 内容 → ready → 收包直到断开。

        返回值：``ready``（曾 ready 后断开）/ ``rejected``（hello_reject）/
        ``failed``（连接或激活失败）/ ``stopped``。
        """
        conn: Connection | None = None
        attempt: _Attempt | None = None
        reader: asyncio.Task | None = None
        heartbeat: asyncio.Task | None = None
        try:
            try:
                ws = await self._open()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 连接失败要退避重试
                log.warn(f"连接 relay 失败 {self.host}:{self.port}："
                         f"{type(e).__name__} {e}")
                return "failed"

            conn = Connection(ws, self)
            attempt = _Attempt(conn)
            self._attempt = attempt
            self._conn = conn
            conn.worker = tasks.spawn(self._worker(conn), name="onebot-relay-worker")
            reader = tasks.spawn(self._reader(attempt), name="onebot-relay-reader")

            await self._send_control(conn, self._control(
                "hello", priority=self.priority, backend_id=self.backend_id))
            hello = await self._wait(attempt.hello)
            if hello is None:
                log.warn("relay hello 无响应（超时或连接关闭）")
                return "failed"
            if hello.get("$relay") != "hello_ack":
                code = str(hello.get("code") or "unknown")
                log.warn(f"relay 拒绝 backend hello（code={code}）")
                return "rejected"

            lease = attempt.lease
            if lease is None:
                log.warn("relay hello_ack 缺少有效 lease，按失败重试")
                return "failed"

            heartbeat = tasks.spawn(self._heartbeat_loop(attempt),
                                    name="onebot-relay-heartbeat")

            content_revision = await self._activate(lease)
            if content_revision is None:
                log.warn("relay 激活失败（内容未就绪），不发送 ready")
                return "failed"

            ready = None
            for index in range(READY_MAX_ATTEMPTS):
                if self._stopping or attempt.closed.is_set() or self._lease_expired():
                    log.warn("relay 激活期间连接/lease 已失效，不发送 ready")
                    return "failed"
                self.content_revision = int(content_revision)
                current = self.lease or lease
                ready = await self._send_ready(
                    attempt, conn, current, int(content_revision))
                if ready is None:
                    log.warn("relay ready_ack 无响应（超时或连接关闭）")
                    return "failed"
                if ready.get("$relay") == "ready_ack":
                    break
                code = str(ready.get("code") or "")
                if code != "content_stale":
                    log.warn(f"relay 拒绝 ready（code={code or 'unknown'}），按失败重试")
                    return "failed"
                if index + 1 >= READY_MAX_ATTEMPTS:
                    log.warn("relay 内容 revision 持续变化，放弃本轮 ready（重连后重新拉取）")
                    return "failed"
                # hello → pull → ready 之间中继上的 canonical revision 前进了
                # （例如旧 owner 的在途 PUT 恰好提交）。重新 pull 拿到新 base 后
                # 再发 ready；绝不把旧 revision 当成同步完成。
                log.info("relay 内容 revision 已前进，重新拉取后重试 ready")
                refreshed = await self._activate(lease)
                if refreshed is None:
                    log.warn("relay 重新拉取内容失败，不发送 ready")
                    return "failed"
                content_revision = refreshed
            if ready is None or ready.get("$relay") != "ready_ack":
                return "failed"

            log.info(f"relay backend 已就绪（priority={self.priority} "
                     f"backend_id={self.backend_id}）")
            await attempt.closed.wait()
            return "ready"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 单次尝试异常不应终止重连
            log.warn(f"relay 连接异常：{type(e).__name__} {e}")
            return "failed"
        finally:
            if heartbeat is not None and not heartbeat.done():
                heartbeat.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await heartbeat
            if self._attempt is attempt:
                self._attempt = None

            # ready 之后掉线 = lease 丢失；初始化阶段失败不算生命周期丢 lease。
            if (attempt is not None and attempt.became_ready
                    and not attempt.released and not self._lost_notified
                    and not self._stopping):
                await self._notify_lost("connection_lost")

            if reader is not None and not reader.done():
                reader.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await reader

            self.ready = False
            self.upstream = False
            self._reset_lease()
            if conn is not None:
                for key, value in list(self.connections.items()):
                    if value is conn:
                        self.connections.pop(key, None)
                await _close_quietly(conn)
            if self._conn is conn:
                self._conn = None

    async def _open(self):
        connect = wsproto.connect(
            self.host, self.port, self.path,
            extra_headers=self._auth_headers(), secure=self.secure)
        if self.connect_timeout:
            return await asyncio.wait_for(connect, self.connect_timeout)
        return await connect

    def _auth_headers(self) -> dict[str, str]:
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}"}

    # ------------------------------------------------------------ 收包 / 控制

    async def _reader(self, attempt: _Attempt):
        conn = attempt.conn
        try:
            while True:
                text = await conn.ws.recv()
                if text is None:
                    break
                if isinstance(text, bytes):
                    text = text.decode("utf-8", "replace")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    log.warn(f"relay 收到非 JSON 数据：{text[:120]}")
                    continue
                if not isinstance(data, dict):
                    continue
                conn.alive_at = time.time()
                await self._dispatch(conn, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 收包异常交给重连循环
            log.warn(f"relay 收包异常：{type(e).__name__} {e}")
        finally:
            attempt.closed.set()
            # 让 hello/ready 的等待立即失败，而不是等到超时。
            _resolve(attempt.hello, None)
            _resolve(attempt.ready_ack, None)

    async def _dispatch(self, conn: Connection, data: dict):
        # 控制帧优先且绝不进入业务事件去重/分发。
        if "$relay" in data:
            await self._handle_control(conn, data)
            return

        if self._lease_expired():
            await self._fail_lease("lease_expired")
            return

        if conn is not self._conn or not self.ready:
            log.debug("忽略非活动 relay 连接的业务帧")
            return

        # 动作回执交给 Connection.pending 配对；旧连接的 echo 不会进新连接。
        if "echo" in data and "post_type" not in data:
            await super()._dispatch(conn, data)
            return
        if data.get("post_type"):
            key = self._delivery_key(data)
            if key is not None:
                if key in self._delivered:
                    log.debug("忽略 relay 重复投递的业务事件")
                    return
                self._delivered[key] = None
                while len(self._delivered) > self.dedup_size:
                    self._delivered.popitem(last=False)
            await super()._dispatch(conn, data)

    @staticmethod
    def _delivery_key(data: dict) -> str | None:
        """业务事件的稳定身份；meta/回执返回 None（不去重）。"""
        post_type = data.get("post_type")
        if not post_type or post_type == "meta_event":
            return None
        if post_type in ("message", "message_sent"):
            message_id = data.get("message_id")
            if message_id in (None, ""):
                return None
            return f"message:{data.get('self_id')}:{message_id}"
        fields = ("post_type", "notice_type", "request_type", "sub_type", "self_id",
                  "group_id", "user_id", "operator_id", "target_id", "time", "flag")
        identity = {name: data.get(name) for name in fields
                    if data.get(name) not in (None, "")}
        if not identity:
            return None
        return "event:" + json.dumps(identity, sort_keys=True, ensure_ascii=False)

    async def _handle_control(self, conn: Connection, data: dict):
        kind = str(data.get("$relay") or "")
        attempt = self._attempt
        if attempt is None or conn is not attempt.conn:
            log.debug(f"忽略旧 relay 连接的控制帧：{kind}")
            return

        if kind == "hello_ack":
            lease_id = str(data.get("lease_id") or "")
            epoch = str(data.get("epoch") or "")
            try:
                revision = int(data.get("revision") or 0)
                heartbeat_ms = int(data.get("heartbeat_interval_ms") or 0)
                ttl_ms = int(data.get("lease_ttl_ms") or 0)
                content_revision = int(data.get("content_revision") or 0)
            except (TypeError, ValueError):
                log.warn("relay hello_ack 字段不是整数，按失败处理")
                _resolve(attempt.hello, data)
                return
            if (not lease_id or not epoch or heartbeat_ms <= 0 or ttl_ms <= 0
                    or revision < 0 or content_revision < 0):
                log.warn("relay hello_ack 字段不完整，按失败处理")
                _resolve(attempt.hello, data)
                return
            lease = {
                "lease_id": lease_id,
                "epoch": epoch,
                "revision": revision,
                "content_revision": content_revision,
            }
            self.lease = lease
            self.lease_id = lease_id
            self.epoch = epoch
            self.revision = revision
            self.upstream = bool(data.get("upstream"))
            self._lease_deadline = time.monotonic() + ttl_ms / 1000.0
            attempt.lease = dict(lease)
            attempt.heartbeat_ms = heartbeat_ms
            attempt.ttl_ms = ttl_ms
            _resolve(attempt.hello, data)
            return

        if kind == "hello_reject":
            _resolve(attempt.hello, data)
            return

        if kind == "ready_ack":
            lease = self.lease or {}
            if (str(data.get("lease_id") or "") != str(lease.get("lease_id") or "")
                    or str(data.get("epoch") or "") != str(lease.get("epoch") or "")):
                log.warn("忽略 lease/epoch 不匹配的 relay ready_ack")
                _resolve(attempt.ready_ack,
                         {"$relay": "error", "code": "stale_ready_ack"})
                return
            if data.get("revision") is not None:
                try:
                    ack_revision = int(data.get("revision") or 0)
                except (TypeError, ValueError):
                    log.warn("忽略 revision 非整数的 relay ready_ack")
                    _resolve(attempt.ready_ack,
                             {"$relay": "error", "code": "invalid_ready_ack"})
                    return
                if ack_revision != int(lease.get("revision") or 0):
                    log.warn("忽略 revision 不匹配的 relay ready_ack")
                    _resolve(attempt.ready_ack,
                             {"$relay": "error", "code": "stale_ready_ack"})
                    return
            if not data.get("ready"):
                log.warn("relay ready_ack 未确认 ready")
                _resolve(attempt.ready_ack,
                         {"$relay": "error", "code": "invalid_ready_ack"})
                return
            # content revision 必须原样回显我们刚发出去的值。否则「已同步」的
            # 内容 barrier 就是假的（中继上的 canonical 可能已经不是我们读到的
            # 那一版），宁可 fail closed 重连重拉。
            ack_content = data.get("content_revision")
            if ack_content is None:
                log.warn("relay ready_ack 没有回显 content_revision，拒绝进入 ready")
                _resolve(attempt.ready_ack,
                         {"$relay": "error", "code": "invalid_ready_ack"})
                return
            try:
                ack_content_value = int(ack_content)
            except (TypeError, ValueError):
                log.warn("忽略 content_revision 非整数的 relay ready_ack")
                _resolve(attempt.ready_ack,
                         {"$relay": "error", "code": "invalid_ready_ack"})
                return
            sent_content = attempt.ready_content_revision
            if sent_content is None or ack_content_value != int(sent_content):
                log.warn("relay ready_ack 的 content_revision 与我们发送的不一致，"
                         "拒绝进入 ready")
                _resolve(attempt.ready_ack,
                         {"$relay": "error", "code": "content_revision_mismatch"})
                return
            # 先发布状态，避免紧跟着到达的缓存 lifecycle-connect 被丢掉。
            self._lease_deadline = time.monotonic() + attempt.ttl_ms / 1000.0
            self.ready = True
            self._lost_notified = False
            attempt.became_ready = True
            self._register(conn)
            _resolve(attempt.ready_ack, data)
            return

        if kind == "heartbeat_ack":
            lease = self.lease
            if lease is None:
                return
            if (str(data.get("lease_id") or "") != str(lease.get("lease_id") or "")
                    or str(data.get("epoch") or "") != str(lease.get("epoch") or "")):
                log.debug("忽略 lease/epoch 不匹配的 relay heartbeat_ack")
                return
            self._lease_deadline = time.monotonic() + attempt.ttl_ms / 1000.0
            if data.get("revision") is not None:
                try:
                    self.revision = int(data["revision"])
                    lease["revision"] = self.revision
                    if attempt.lease is not None:
                        attempt.lease["revision"] = self.revision
                except (TypeError, ValueError):
                    pass
            return

        if kind == "upstream_state":
            self.upstream = bool(data.get("connected"))
            log.info(f"relay upstream {'已连接' if self.upstream else '已断开'}")
            return

        if kind == "fenced":
            reason = str(data.get("reason") or "")
            await self._fail_lease(f"fenced:{reason}")
            return

        if kind == "error":
            code = str(data.get("code") or "")
            log.warn(f"relay 错误：{code or 'unknown'}")
            # content_stale 只会是对我们刚发出的 ready 的拒绝：交给 ready 等待方
            # 决定「重新 pull 后重试」还是失败，绝不能当成 ready。
            if code == "content_stale":
                _resolve(attempt.ready_ack, data)
                return
            # upstream_unavailable / echo_reused 只是暂时不能转发这一帧，lease
            # 仍然有效；只有 not_owner / fenced 才说明当前连接已经不是 owner。
            if code in ("not_owner", "fenced"):
                await self._fail_lease(f"error:{code}")
            return

        if kind == "bye":
            log.info("relay 已释放 backend 连接")
            await self._fail_lease("relay_bye")
            return

        log.debug(f"忽略未知 relay 控制帧：{kind!r}")

    async def _heartbeat_loop(self, attempt: _Attempt):
        interval = max(0.05, attempt.heartbeat_ms / 1000.0)
        next_send = time.monotonic() + interval
        try:
            while not attempt.closed.is_set():
                now = time.monotonic()
                if self._lease_expired():
                    await self._fail_lease("heartbeat_timeout")
                    return
                deadline = self._lease_deadline or (now + interval)
                # 在「该发心跳」和「lease 到期」两个时刻中较早者醒来，
                # 这样即使 ttl 比心跳间隔还短，也能在 ttl 附近 fail closed。
                wake = min(next_send, deadline) - now
                await asyncio.sleep(max(0.01, wake))
                if attempt.closed.is_set() or self._attempt is not attempt:
                    return
                if self._lease_expired():
                    await self._fail_lease("heartbeat_timeout")
                    return
                if time.monotonic() < next_send:
                    continue
                lease = self.lease
                if lease is None:
                    return
                try:
                    await self._send_control(attempt.conn, self._control(
                        "heartbeat", lease_id=lease["lease_id"],
                        epoch=lease["epoch"], revision=lease["revision"]))
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 交给重连循环
                    log.warn(f"relay 心跳发送失败：{type(e).__name__} {e}")
                    return
                next_send = time.monotonic() + interval
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------ lease 失效

    def _lease_expired(self) -> bool:
        deadline = self._lease_deadline
        return bool(self.lease_id) and deadline is not None and time.monotonic() >= deadline

    async def _fail_lease(self, reason: str):
        """fenced / 超时：立即 fail closed，暂停生命周期并关闭连接。"""
        await self._notify_lost(reason)
        attempt = self._attempt
        if attempt is not None:
            await _close_quietly(attempt.conn)

    async def _notify_lost(self, reason: str):
        if self._lost_notified:
            return
        self._lost_notified = True
        self.ready = False
        log.warn(f"relay lease 丢失：{reason}")
        callback = self.on_lost
        try:
            if callback is not None:
                result = callback(self)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 回调失败不能拖垮重连
            log.warn(f"relay on_lost 回调失败：{type(e).__name__} {e}")
        finally:
            # 回调之后再清 lease，避免把旧 lease_id 暴露给后续状态/日志。
            self.lease = None
            self.lease_id = None
            self.epoch = None
            self._lease_deadline = None
            self.ready = False

    def _reset_lease(self):
        self.lease = None
        self.lease_id = None
        self.epoch = None
        self.revision = 0
        self._lease_deadline = None
        self.ready = False

    # ------------------------------------------------------------ 内容副本

    def _sync_enabled(self) -> bool:
        if self.on_acquired is not None:
            # 有 on_acquired 时内容由回调负责（它返回 content_revision）。
            return False
        if self.replica is not None:
            return True
        return bool(self.require_content and self.content_url)

    def _ensure_replica(self):
        """延迟到运行时才 import/实例化 ha_content。

        ha_content 由独立任务提供；这里不在模块导入期依赖它，缺失时
        明确失败（激活失败 → 不发 ready），而不是悄悄跳过内容同步。
        """
        if self.replica is not None:
            return self.replica
        if self._replica_obj is not None:
            return self._replica_obj
        if not self.content_url:
            log.warn("relay 内容同步未配置 relay_content_url，无法激活")
            return None
        if not self.content_root:
            log.warn("relay 内容根未知（需要 Config.path 或显式 content_root），无法激活")
            return None
        try:
            from . import ha_content  # noqa: PLC0415 - 运行时按需加载
        except Exception as e:  # noqa: BLE001 - 模块缺失/导入失败都按不可用
            log.warn(f"relay 内容模块不可用：{type(e).__name__} {e}")
            return None
        try:
            self._replica_obj = ha_content.ContentReplica(
                self.content_root, self.content_url,
                token=self.access_token, timeout=self.sync_timeout)
        except Exception as e:  # noqa: BLE001
            log.warn(f"relay 内容副本初始化失败：{type(e).__name__} {e}")
            return None
        return self._replica_obj

    async def _activate(self, lease: dict) -> int | None:
        """hello 之后、ready 之前的门禁：返回 content_revision 或失败 None。"""
        self._clear_content_conflict()
        if self.on_acquired is not None:
            try:
                value = self.on_acquired(self, dict(lease))
                if inspect.isawaitable(value):
                    value = await value
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 回调失败 = 不 ready
                log.warn(f"relay on_acquired 失败：{type(e).__name__} {e}")
                return None
            if value is None or value is False:
                return None
            try:
                revision = int(value)
            except (TypeError, ValueError):
                log.warn("relay on_acquired 未返回整数 content_revision")
                return None
            return revision if revision >= 0 else None

        if not self.require_content:
            return int(lease.get("content_revision") or 0)

        replica = self._ensure_replica()
        if replica is None:
            return None
        try:
            result = await self._replica_call(replica, "pull", "pull_async")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 拉取失败 = 不 ready
            if self._is_conflict(e):
                self._mark_content_conflict(
                    "pull_conflict",
                    "relay 内容拉取冲突（409）：不自动推送本地旧数据，不发送 ready")
            else:
                log.warn(f"relay 内容拉取失败：{type(e).__name__} {e}")
            return None
        if not isinstance(result, dict) or not result.get("ok"):
            if self._result_is_conflict(result):
                self._mark_content_conflict(
                    "pull_conflict",
                    "relay 内容拉取冲突（409）：不自动推送本地旧数据，不发送 ready")
            else:
                log.warn("relay 内容拉取未成功，不发送 ready")
            return None
        try:
            revision = int(result.get("content_revision") or 0)
        except (TypeError, ValueError):
            return None
        return revision if revision >= 0 else None

    @staticmethod
    async def _replica_call(replica: Any, sync_name: str, async_name: str,
                            *args):
        """兼容同步 / 异步副本实现。

        真实 ``ha_content.ContentReplica`` 的 pull/push 是阻塞 HTTP，并另外
        提供 ``*_async`` 适配；测试注入的假副本通常是 async。两种都支持，
        有 ``*_async`` 时优先用它，避免阻塞事件循环。
        """
        async_method = getattr(replica, async_name, None)
        if callable(async_method):
            result = async_method(*args)
            return await result if inspect.isawaitable(result) else result
        result = getattr(replica, sync_name)(*args)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _is_conflict(error: BaseException) -> bool:
        code = str(getattr(error, "code", "") or "")
        return code in ("CONFLICT", "STALE_BASE") or getattr(error, "http_status", None) == 409

    @staticmethod
    def _result_is_conflict(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        return bool(result.get("conflict")
                    or result.get("status") == 409
                    or str(result.get("code") or "") in ("CONFLICT", "STALE_BASE"))

    def _lease_payload(self) -> dict | None:
        lease = self.lease
        if lease is None or not self.lease_id:
            return None
        return {
            "lease_id": lease["lease_id"],
            "epoch": lease["epoch"],
            "revision": lease["revision"],
            "content_revision": self.content_revision,
        }

    async def _sync_loop(self):
        try:
            while not self._stopping:
                await asyncio.sleep(self.sync_interval)
                if self._stopping:
                    return
                try:
                    if self._is_lease_owner():
                        if self._content_conflict:
                            # 409 之后：先 pull canonical 把 base 对齐，本轮不 push。
                            await self._recover_content_conflict()
                        else:
                            await self._push_content()
                    else:
                        await self._pull_standby()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 同步失败下一轮再试
                    log.warn(f"relay 内容同步失败：{type(e).__name__} {e}")
        except asyncio.CancelledError:
            raise

    async def _push_content(self):
        """只有 lease owner 才 push；409 冲突绝不自动覆盖远端。"""
        if self._content_conflict:
            log.debug("relay 内容 CAS 冲突未解决，跳过 push（不自动重推旧数据）")
            return
        replica = self._ensure_replica()
        lease = self._lease_payload()
        if replica is None or lease is None:
            return
        try:
            result = await self._replica_call(replica, "push", "push_async", lease)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 下一轮先恢复再说
            if self._is_conflict(e):
                self._mark_content_conflict(
                    "push_conflict",
                    "relay 内容推送冲突（409）：canonical 已被推进，暂停推送；"
                    "下一轮先 pull 对齐 base（未推送的本地改动以 canonical 为准）")
            else:
                log.warn(f"relay 内容推送失败：{type(e).__name__} {e}")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            if self._result_is_conflict(result):
                self._mark_content_conflict(
                    "push_conflict",
                    "relay 内容推送冲突（409）：canonical 已被推进，暂停推送；"
                    "下一轮先 pull 对齐 base（未推送的本地改动以 canonical 为准）")
            else:
                log.warn("relay 内容推送未成功")
            return
        if result.get("content_revision") is not None:
            try:
                self.content_revision = int(result["content_revision"])
            except (TypeError, ValueError):
                pass

    async def _recover_content_conflict(self) -> bool:
        """409 之后的恢复：只 pull canonical，把 CAS base 对齐，**本轮不 push**。

        为什么不能直接用旧 base 重推：中继上的 canonical 已经被别人推进过，
        重推旧本地数据等于用过期版本覆盖 canonical。这里先 pull（拿到新 base
        并应用 canonical 内容），下一次同步周期才允许 push——那时 push 用的是
        刚对齐的 base，CAS 会保护并发写入。

        pull 也失败（网络 / 再次 409 / 校验失败）时保持阻塞并暴露原因：
        ``content_blocked`` / ``content_blocked_reason``，绝不假装同步成功。
        """
        replica = self._ensure_replica()
        if replica is None:
            self._mark_content_conflict(
                "replica_unavailable", "relay 内容冲突无法恢复：内容副本不可用")
            return False
        log.info("relay 内容 CAS 冲突恢复：先拉取 canonical 更新 base（本轮不推送）")
        try:
            result = await self._replica_call(replica, "pull", "pull_async")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 保持阻塞，下一轮再试
            if self._is_conflict(e):
                self._mark_content_conflict(
                    "pull_conflict",
                    "relay 内容冲突未解除（pull 409）：保持阻塞，不推送本地旧数据")
            else:
                log.warn(f"relay 冲突恢复拉取失败：{type(e).__name__} {e}")
            return False
        if not isinstance(result, dict) or not result.get("ok"):
            if self._result_is_conflict(result):
                self._mark_content_conflict(
                    "pull_conflict",
                    "relay 内容冲突未解除（pull 409）：保持阻塞，不推送本地旧数据")
            else:
                log.warn("relay 冲突恢复拉取未成功，保持阻塞")
            return False
        revision = result.get("content_revision")
        if revision is not None:
            try:
                self.content_revision = int(revision)
            except (TypeError, ValueError):
                pass
        if self._content_conflict:
            log.info(f"relay 内容冲突已通过 pull 解除（base revision={self.content_revision}）")
        self._clear_content_conflict()
        return True

    async def _pull_standby(self):
        """standby / warm standby 只 pull；拉取冲突时绝不回推旧本地数据。"""
        replica = self._ensure_replica()
        if replica is None:
            return
        try:
            result = await self._replica_call(replica, "pull", "pull_async")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 下一轮再试
            if self._is_conflict(e):
                self._mark_content_conflict(
                    "pull_conflict", "relay 内容拉取冲突（409）：不自动推送本地旧数据")
            else:
                log.warn(f"relay 内容拉取失败：{type(e).__name__} {e}")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            if self._result_is_conflict(result):
                self._mark_content_conflict(
                    "pull_conflict", "relay 内容拉取冲突（409）：不自动推送本地旧数据")
            else:
                log.warn("relay 内容拉取未成功")
            return
        self._clear_content_conflict()
        if result.get("content_revision") is not None:
            try:
                self.content_revision = int(result["content_revision"])
            except (TypeError, ValueError):
                pass

    async def _flush_before_release(self):
        """bye 之前把 owner 的内容 flush 掉；回调优先，否则直接 push。"""
        callback = self.before_release
        if callback is not None:
            result = callback(self)
            if inspect.isawaitable(result):
                await result
            return
        if self.on_acquired is not None:
            # 内容由 on_acquired 回调负责；没有 before_release 时不擅自 push。
            return
        if self._content_conflict:
            # 冲突阻塞态下释放：不推旧本地数据（那会覆盖 canonical），如实报告。
            log.warn(f"relay 内容处于冲突阻塞态（{self._content_conflict_reason or 'unknown'}）"
                     "：释放前不推送旧本地数据；需先 pull 恢复")
            return
        if self._is_lease_owner():
            await self._push_content()

    # ------------------------------------------------------------ 发送控制帧

    async def _send_control(self, conn: Connection, payload: dict):
        if conn.ws.closed:
            raise ConnectionError("relay 连接已关闭")
        # json.dumps 保序：$relay 一定在第一位（中继按前缀识别控制帧）。
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        log.debug(f"relay → {payload.get('$relay')}")
        await conn.ws.send_text(text)

    @staticmethod
    def _control(kind: str, **fields) -> dict:
        payload: dict[str, Any] = {"$relay": kind}
        payload.update(fields)
        return payload

    async def _send_ready(self, attempt: _Attempt, conn: Connection, lease: dict,
                          content_revision: int):
        """发 ready 并等这一次的结果（ready_ack 或 error 控制帧）。"""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        attempt.ready_ack = future
        attempt.ready_content_revision = int(content_revision)
        await self._send_control(conn, self._control(
            "ready",
            lease_id=lease["lease_id"],
            epoch=lease["epoch"],
            revision=lease["revision"],
            content_revision=int(content_revision),
        ))
        return await self._wait(future)

    # ------------------------------------------------------------ 等待

    async def _wait(self, future: asyncio.Future):
        timeout = self.connect_timeout or DEFAULT_HANDSHAKE_TIMEOUT
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
