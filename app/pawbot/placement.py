#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LEGACY / STANDALONE 放置租约测试夹具——**不是运行时权威，不要接线**。

状态（2026-10-31）：本模块只被 ``app/tools/test_placement.py`` 使用，配合测试里
的假 HTTP arbiter 验证「HTTP JSON lease 客户端」的 fail-closed 语义（抢占、过期、
旧 revision、超时、心跳、日志脱敏）。它**没有**被 ``core.py`` /
``relay_client.py`` / ``ha_runtime.py`` / ``lib/onebot-relay.js`` 导入，也不在
``server.mode=relay`` 的生产组合里。

A 方案真正的运行时权威是：

* ``lib/onebot-relay.js``：唯一中继，WS lease/fencing/ready 与 ``/ha/content``；
* ``app/pawbot/relay_client.py``：后端 WS 客户端与内容 barrier；
* ``app/pawbot/ha_runtime.py``：业务接管/准入/内容同步生命周期。

不要把本模块接进运行时，也不要把这里的 HTTP ``/placement`` 协议当成中继协议。
下面保留的是该独立 fixture 自身的协议说明，仅用于理解测试。

------------------------------------------------------------

这里的「放置」只回答一件事：**现在应该由哪台机器上的 PawBot 发消息？**

  · 本地 PawBot 在线时优先；
  · 本地掉线（租约过期 / 主动释放）后，服务器上的 PawBot 可以接管；
  · 同一 arbiter 下同一时刻只有一个 holder；其他实例是 passive，不能外发。
    旧 holder 的租约被接管或到期后，客户端本地检查会 fail-closed；这是 fixture
    自身的行为，不代表 ``lib/onebot-relay.js`` 的真实转发/echo 语义。

为什么要有 fencing token
------------------------
租约过期不等于旧实例立刻知道。旧实例可能还在发最后一条消息，新实例已经接管。
所以 arbiter 每次授权都给出单调递增的 ``revision`` / ``fencing_token``；
调用方在真正外发前用 :meth:`LeaseClient.guard_emit` 做一次本地检查。本模块
**不解决分布式共识**：它只保证「拿不到有效租约就一定不发」，以及在客户端
拒绝回退的 revision / fencing token，避免旧租约复活。真正的仲裁由 arbiter 负责。

它不是本机 ``.runtime.lock``
---------------------------
``.runtime.lock`` 解决「同一台机器上不能跑两个 PawBot」；本模块解决
「两台机器谁对外发言」。两者不能互相替代，也不要拿 lease 去当本机互斥锁。

协议（仅该 fixture 的 HTTP + JSON 假 arbiter；不是 relay 协议）
--------------------------------------------------------------
所有请求/响应都是 JSON 对象；``base_url`` 例如 ``http://127.0.0.1:6200/placement``。

``POST <base>/claim``
  请求: ``{"instance_id", "priority": "local"|"server", "ttl", "known_revision"}``
  成功: ``{"ok": true, "role": "active"|"passive",
           "lease_id": "...", "revision": N, "fencing_token": N,
           "ttl": seconds, "expires_at": unix_seconds,
           "holder": "instance_id", "priority": "..."}``
  ``role=passive`` 时 ``lease_id`` 可以为空；**优先级由 arbiter 决定**，
  客户端传的 ``priority`` 只是提示，不能据此自认为 active。

``POST <base>/renew``
  请求: ``{"instance_id", "lease_id", "revision"}``
  成功: 同 claim 的 active 响应（``lease_id`` 必须与请求一致）。
  租约已被别人接管 / 过期: ``409``。

``POST <base>/release``
  请求: ``{"instance_id", "lease_id", "revision"}``
  成功: ``{"ok": true, "released": true}``；重复释放 / 已经不是持有者时，
  arbiter 可以回 ``200`` + ``released:false`` 或 ``404/409``，客户端一律按
  「本地已释放」处理（幂等）。

``GET <base>/status?instance_id=...``
  只读查询当前 holder，不得改变租约。响应至少含 ``holder`` / ``revision`` /
  ``fencing_token`` / ``ttl`` / ``expires_at``。

安全与失败语义
--------------
* ``lease_id`` 必须由 arbiter 用 CSPRNG 生成（本模块提供 :func:`new_lease_id`），
  客户端拒绝短于 :data:`MIN_LEASE_ID_LEN` 的 id。
* token 只放在 ``Authorization: Bearer`` 头里；**日志和异常消息不出现 token /
  lease_id**。
* 所有 HTTP 请求都有 timeout，重试次数有上限；401/403 不重试。
* claim/renew 失败（含超时）后状态立即变 :attr:`LeaseState.LOST`，``can_emit()``
  返回 False，``guard_emit()`` 抛 :class:`EmitNotAllowed`。
* 409、旧 revision、旧 lease_id 都按 fail-closed 处理，不会拿旧租约继续发。
"""

from __future__ import annotations

import enum
import hmac
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .log import log

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_BACKOFF",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TTL",
    "EmitNotAllowed",
    "LeaseClient",
    "LeaseState",
    "MIN_LEASE_ID_LEN",
    "PlacementError",
    "PlacementLease",
    "ROLE_ACTIVE",
    "ROLE_PASSIVE",
    "new_lease_id",
]

ROLE_ACTIVE = "active"
ROLE_PASSIVE = "passive"

DEFAULT_TTL = 30.0
DEFAULT_TIMEOUT = 3.0
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_BACKOFF = 0.2
MAX_RESPONSE_BYTES = 64 * 1024
MIN_LEASE_ID_LEN = 16


class LeaseState(str, enum.Enum):
    """客户端看到的租约状态。

    ``ACTIVE`` 才可能允许外发；其余状态一律不能发。``LOST`` 是 fail-closed
    的终点：超时、409、401、旧 revision、本地 TTL 过期都会落到这里。
    """

    UNKNOWN = "unknown"     # 还没 claim 过
    ACTIVE = "active"       # 持有有效 active 租约
    PASSIVE = "passive"     # 能连上 arbiter，但当前 holder 是别人
    LOST = "lost"           # 租约丢失/请求失败/被接管：禁止外发
    RELEASED = "released"   # 自己释放了（或重复释放）

    def __str__(self) -> str:
        return self.value


class PlacementError(RuntimeError):
    """带错误码的放置错误；消息里不允许出现 token / lease_id。"""

    def __init__(self, code: str, message: str = "", *, status: int | None = None):
        self.code = str(code or "error")
        self.message = str(message or self.code)
        self.status = status
        super().__init__(f"{self.code}: {self.message}" if self.message != self.code else self.code)

    def as_dict(self) -> dict:
        out = {"ok": False, "code": self.code, "message": self.message}
        if self.status is not None:
            out["status"] = int(self.status)
        return out


class EmitNotAllowed(PlacementError):
    """没有有效租约却试图外发时抛出（fail-closed）。"""

    def __init__(self, message: str = "没有有效的 placement lease"):
        super().__init__("no_lease", message)


def new_lease_id() -> str:
    """生成一个不可预测的 lease_id（arbiter 端使用；客户端只校验长度）。

    16 字节 CSPRNG → 32 个十六进制字符。不要用时间戳、自增数、实例名拼 lease_id：
    可预测的 id 会让旧实例/攻击者更容易冒充 holder。
    """
    return secrets.token_hex(16)


def _as_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise PlacementError("protocol", f"{name} 不能是 bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    raise PlacementError("protocol", f"{name} 不是整数")


def _as_float(value, default: float, name: str = "value") -> float:
    if isinstance(value, bool) or value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise PlacementError("protocol", f"{name} 不是数字") from None


def _new_instance_id() -> str:
    host = ""
    try:
        host = socket.gethostname()
    except OSError:
        host = "host"
    return f"{host or 'host'}-{os.getpid()}-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class PlacementLease:
    """一份 arbiter 授予的 active 租约（不可变快照）。

    ``revision`` 与 ``fencing_token`` 都由 arbiter 单调递增；调用方只能使用
    最新的 lease 对象。``expires_at`` 是 arbiter 时钟的 Unix 秒，客户端用本地
    时钟做保守检查（早一点停比晚一点停安全）。
    """

    lease_id: str
    revision: int
    fencing_token: int
    role: str
    ttl: float
    expires_at: float
    instance_id: str
    priority: str = ""
    holder: str = ""

    @property
    def active(self) -> bool:
        return self.role == ROLE_ACTIVE and bool(self.lease_id)

    def remaining(self, now: float | None = None) -> float:
        """还剩多少秒（可能为负）；``now`` 默认本地时间。"""
        return float(self.expires_at) - (time.time() if now is None else float(now))

    def valid(self, now: float | None = None, *, margin: float = 0.0) -> bool:
        """是否仍可外发：active + 有 lease_id + 未过期（留 ``margin`` 秒余量）。"""
        return self.active and self.remaining(now) > float(margin)

    def __repr__(self) -> str:  # 不暴露 lease_id，避免被顺手打进日志
        return (
            f"PlacementLease(role={self.role!r}, instance_id={self.instance_id!r}, "
            f"revision={self.revision}, fencing_token={self.fencing_token}, "
            f"ttl={self.ttl!r}, expires_at={self.expires_at!r}, lease_id=***)"
        )


class LeaseClient:
    """向 HTTP JSON arbiter 申请 / 续租 / 释放 placement lease 的同步客户端（fixture）。

    本类只服务于 ``app/tools/test_placement.py`` 的独立 HTTP lease 回归；它不是
    A 方案运行时使用的租约实现，不会被生产代码导入。下面的调用示例只说明该
    fixture 的用法，不代表 ``server.mode=relay`` 的真实接线。

    典型用法（仅限该 fixture 的假 arbiter）::

        client = LeaseClient("http://127.0.0.1:6200/placement",
                             priority="local", ttl=30)
        if client.claim() is not None:       # arbiter 说本机是 active
            client.start()                   # 后台按 ttl/3 续租
        ...
        client.guard_emit()                  # 真正发消息前再检查一次
        ...
        client.stop()                        # 停心跳并释放租约

    ``priority`` 只是提示：arbiter 可以回 ``passive``，客户端绝不会因为自己标了
    ``local`` 就跳过 arbiter 的裁决。``lease`` / ``state`` / ``fencing_token``
    都是只读快照。

    线程安全：``claim`` / ``renew`` / ``release`` / ``status`` 之间用操作锁串行化；
    ``can_emit`` 只读短锁，不会被网络请求卡住。
    """

    def __init__(
        self,
        arbiter_url: str,
        *,
        instance_id: str = "",
        priority: str = "local",
        ttl: float = DEFAULT_TTL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        heartbeat_interval: float | None = None,
        token: str = "",
        clock=time.time,
    ):
        base = str(arbiter_url or "").strip().rstrip("/")
        if not base:
            raise PlacementError("config", "arbiter_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise PlacementError("config", "arbiter_url 必须以 http:// 或 https:// 开头")
        self._base = base
        self._instance_id = str(instance_id or "").strip() or _new_instance_id()
        self._priority = str(priority or "local").strip().lower() or "local"
        self._ttl = max(0.1, float(ttl))
        self._timeout = max(0.05, float(timeout))
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = max(0.0, float(retry_backoff))
        self._heartbeat_interval = self._normalize_interval(heartbeat_interval)
        self._token = str(token or "")
        self._clock = clock if callable(clock) else time.time

        self._lock = threading.RLock()
        self._op_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._state = LeaseState.UNKNOWN
        self._lease: PlacementLease | None = None
        self._last_error = ""
        self._last_detail = ""
        self._last_lease_id = ""
        self._seen_revision = -1
        self._seen_fence = -1

    # ------------------------------------------------------------ 只读快照
    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def priority(self) -> str:
        return self._priority

    @property
    def state(self) -> LeaseState:
        with self._lock:
            return self._state

    @property
    def lease(self) -> PlacementLease | None:
        with self._lock:
            return self._lease

    @property
    def lease_id(self) -> str:
        """当前租约 id（没有则为空串）。只读；不要把它写进日志。"""
        with self._lock:
            return self._lease.lease_id if self._lease else ""

    @property
    def fencing_token(self) -> int:
        with self._lock:
            return int(self._lease.fencing_token) if self._lease else -1

    @property
    def fence(self) -> int:
        """:attr:`fencing_token` 的短别名（中继转发时可用作 X-PawBot-Fence）。"""
        return self.fencing_token

    @property
    def revision(self) -> int:
        with self._lock:
            return int(self._lease.revision) if self._lease else -1

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def last_detail(self) -> str:
        with self._lock:
            return self._scrub(self._last_detail)

    @property
    def heartbeat_interval(self) -> float:
        return self._heartbeat_interval

    def __repr__(self) -> str:  # token / lease_id 都不出现在 repr 里
        with self._lock:
            state = self._state.value
        return (f"LeaseClient(instance_id={self._instance_id!r}, "
                f"priority={self._priority!r}, state={state!r})")

    # ------------------------------------------------------------ 仲裁操作
    def acquire(self) -> PlacementLease | None:
        """:meth:`claim` 的别名，方便 A 中继 / B 直连的调用方按 acquire 命名。"""
        return self.claim()

    def claim(self) -> PlacementLease | None:
        """向 arbiter 申请 active。

        返回有效 :class:`PlacementLease` 表示本机成为 active；返回 ``None``
        表示 arbiter 判为 passive 或请求失败（失败时 ``state == LOST``）。
        已持有有效租约时直接返回当前租约，不重复请求。
        """
        with self._op_lock:
            with self._lock:
                if self._state is LeaseState.ACTIVE:
                    self._expire_if_needed()
                    if self._state is LeaseState.ACTIVE and self._lease is not None:
                        return self._lease

            payload = {
                "instance_id": self._instance_id,
                "priority": self._priority,
                "ttl": self._ttl,
            }
            if self._seen_revision >= 0:
                payload["known_revision"] = self._seen_revision

            status, data, code = self._request("POST", "claim", payload)
            if status is None:
                self._fail(self._map_transport(code), "claim 请求失败")
                return None
            if status in (200, 201):
                with self._lock:
                    try:
                        lease = self._accept(data)
                    except PlacementError as exc:
                        self._fail(exc.code, exc.message)
                        return None
                    if lease is None:
                        self._lease = None
                        self._state = LeaseState.PASSIVE
                        self._last_error = ""
                        self._last_detail = ""
                        return None
                    self._lease = lease
                    self._state = LeaseState.ACTIVE
                    self._last_error = ""
                    self._last_detail = ""
                log.info(
                    f"placement: 取得 active 租约 instance={self._instance_id} "
                    f"revision={lease.revision} fence={lease.fencing_token}"
                )
                return lease

            self._fail(self._map_http(status), "claim 被拒绝")
            return None

    def renew(self) -> bool:
        """续租。只有当前确实持有 active 租约时才发请求。

        成功返回 True；409/401/超时/旧 revision 等失败都会让状态立即变
        ``LOST`` 并返回 False（fail-closed）。passive 状态下调用只返回 False，
        不会误把自己提升成 active。
        """
        with self._op_lock:
            with self._lock:
                if self._state is LeaseState.PASSIVE:
                    return False
                if self._state is not LeaseState.ACTIVE or self._lease is None:
                    return False
                if self._expire_if_needed():
                    return False
                lease = self._lease

            payload = {
                "instance_id": self._instance_id,
                "lease_id": lease.lease_id,
                "revision": lease.revision,
            }
            status, data, code = self._request("POST", "renew", payload)
            with self._lock:
                # 请求在飞的时候可能已经被 release / stop：不要复活旧租约。
                if (self._state is not LeaseState.ACTIVE or self._lease is None
                        or self._lease.lease_id != lease.lease_id):
                    return False
                if status is None:
                    self._fail(self._map_transport(code), "renew 请求失败")
                    return False
                if status in (200, 201):
                    try:
                        fresh = self._accept(data, expected_lease_id=lease.lease_id)
                    except PlacementError as exc:
                        self._fail(exc.code, exc.message)
                        return False
                    if fresh is None:
                        # arbiter 在 renew 上返回 passive 属于协议错误；宁可不发。
                        self._fail("protocol", "renew 返回了 passive")
                        return False
                    self._lease = fresh
                    self._last_error = ""
                    self._last_detail = ""
                    return True
                self._fail(self._map_http(status), "renew 被拒绝")
                return False

    def release(self) -> dict:
        """释放租约（幂等，不抛异常）。

        本地状态**立刻**变 ``RELEASED``，即使网络失败也不会再允许外发。
        返回 ``{"ok", "released", "duplicate", "code", "local_released"}``；
        重复 release / 已经不是 holder 时按成功处理（``duplicate=True``）。
        """
        with self._op_lock:
            with self._lock:
                lease = self._lease
                self._lease = None
                self._state = LeaseState.RELEASED
                self._last_error = ""
                self._last_detail = ""
                if lease is None:
                    return {
                        "ok": True, "released": True, "duplicate": True,
                        "code": "already_released", "local_released": True,
                    }

            payload = {
                "instance_id": self._instance_id,
                "lease_id": lease.lease_id,
                "revision": lease.revision,
            }
            status, data, code = self._request("POST", "release", payload)
            if status is None:
                return {
                    "ok": False, "released": False, "duplicate": False,
                    "code": self._map_transport(code), "local_released": True,
                }
            if status in (200, 201):
                duplicate = bool(data.get("duplicate")) or data.get("released") is False
                return {
                    "ok": True, "released": True, "duplicate": duplicate,
                    "code": "already_released" if duplicate else "ok",
                    "local_released": True,
                }
            if status in (404, 409):
                return {
                    "ok": True, "released": True, "duplicate": True,
                    "code": "already_released", "local_released": True,
                }
            return {
                "ok": False, "released": False, "duplicate": False,
                "code": self._map_http(status), "local_released": True,
            }

    def status(self) -> dict | None:
        """只读查询 arbiter 当前 holder；失败返回 None，**不改变**本地租约状态。"""
        path = "status?" + urllib.parse.urlencode({"instance_id": self._instance_id})
        with self._op_lock:
            code_status, data, _code = self._request("GET", path, None, retries=0)
        if code_status in (200, 201) and isinstance(data, dict):
            return data
        return None

    # ------------------------------------------------------------ 发消息闸门
    def can_emit(self) -> bool:
        """现在能不能对外发消息：必须持有未过期的 active 租约。

        这是本地最后一道闸；调用开销很小，不会发网络请求。租约到期会被立即
        标成 ``LOST``（fail-closed）。
        """
        with self._lock:
            if self._state is not LeaseState.ACTIVE:
                return False
            if self._expire_if_needed():
                return False
            lease = self._lease
            if lease is None or not lease.active:
                return False
            if self._clock() >= float(lease.expires_at):
                self._fail("expired", "lease TTL 到期")
                return False
            return True

    def guard_emit(self, *, action: str = "send") -> bool:
        """外发前调用；没有有效租约就抛 :class:`EmitNotAllowed`，有效则返回 True。

        建议放在真正的 OneBot ``send_*`` 之前（以及中继转发的入口），而不是
        只在取得租约时检查一次：旧 holder 可能在租约过期后还持有旧对象。
        """
        if not self.can_emit():
            raise EmitNotAllowed(f"拒绝{action}：没有有效的 placement lease（fail-closed）")
        return True

    # ------------------------------------------------------------ 心跳线程
    def start(self) -> "LeaseClient":
        """启动后台续租线程（daemon）。建议先 :meth:`claim` 成功再 start。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"pawbot-lease-{self._instance_id[:24]}",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, *, release: bool = True) -> dict | None:
        """停止心跳线程；默认顺带释放租约。

        返回 :meth:`release` 的结果；``release=False`` 时返回 None，租约会在
        TTL 到期后自然失效（此时 ``can_emit`` 仍按本地时钟兜底）。
        """
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._join_timeout())
        if release:
            return self.release()
        return None

    def close(self, *, release: bool = True) -> dict | None:
        """``stop`` 的别名，方便 with 语句 / 清理路径。"""
        return self.stop(release=release)

    def __enter__(self) -> "LeaseClient":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_interval):
            with self._lock:
                if self._state is not LeaseState.ACTIVE:
                    continue
            try:
                ok = self.renew()
            except Exception as exc:  # noqa: BLE001 - 心跳线程不能把进程打挂
                self._fail("internal", f"心跳异常（{type(exc).__name__}）")
                return
            if not ok:
                return

    def _join_timeout(self) -> float:
        return max(1.0, self._timeout * (self._max_retries + 1) + 1.0)

    def _normalize_interval(self, value) -> float:
        if value is None:
            interval = self._ttl / 3.0
        else:
            try:
                interval = float(value)
            except (TypeError, ValueError):
                interval = self._ttl / 3.0
        return max(0.05, min(interval, max(0.05, self._ttl / 2.0)))

    # ------------------------------------------------------------ 协议解析
    def _accept(self, data: dict, *, expected_lease_id: str | None = None) -> PlacementLease | None:
        """把 arbiter 的 200 响应转成 lease / None（passive）。失败抛 PlacementError。"""
        if not isinstance(data, dict):
            raise PlacementError("protocol", "arbiter 响应不是 JSON 对象")
        if data.get("ok") is False:
            code = self._scrub(str(data.get("code") or "rejected"))[:80] or "rejected"
            raise PlacementError(code, "arbiter 拒绝了请求")

        role = str(data.get("role") or "").strip().lower()
        if not role:
            if data.get("active") is True:
                role = ROLE_ACTIVE
            elif data.get("active") is False:
                role = ROLE_PASSIVE

        if role == ROLE_PASSIVE:
            # passive 没有租约；revision/fencing 只在响应确实提供时用于单调检查。
            if "revision" in data and "fencing_token" in data:
                self._observe(_as_int(data.get("revision"), "revision"),
                              _as_int(data.get("fencing_token"), "fencing_token"))
            return None
        if role != ROLE_ACTIVE:
            raise PlacementError("protocol", f"未知 role：{role!r}")

        lease_id = str(data.get("lease_id") or "")
        if len(lease_id) < MIN_LEASE_ID_LEN:
            raise PlacementError("protocol", "arbiter 返回的 lease_id 太短或为空")
        if expected_lease_id is not None and not hmac.compare_digest(
                lease_id.encode("utf-8"), expected_lease_id.encode("utf-8")):
            raise PlacementError("protocol", "renew 返回的 lease_id 与当前租约不一致")

        revision = _as_int(data.get("revision"), "revision")
        fence = _as_int(data.get("fencing_token", revision), "fencing_token")
        self._observe(revision, fence)

        ttl = _as_float(data.get("ttl"), self._ttl, "ttl")
        if ttl <= 0:
            raise PlacementError("protocol", "arbiter 返回的 ttl 必须大于 0")
        expires = _as_float(data.get("expires_at"), self._clock() + ttl, "expires_at")
        if expires <= 0:
            expires = self._clock() + ttl

        holder = str(data.get("holder") or data.get("instance_id") or self._instance_id)
        self._last_lease_id = lease_id
        return PlacementLease(
            lease_id=lease_id,
            revision=revision,
            fencing_token=fence,
            role=ROLE_ACTIVE,
            ttl=ttl,
            expires_at=expires,
            instance_id=str(data.get("instance_id") or self._instance_id),
            priority=str(data.get("priority") or self._priority),
            holder=holder,
        )

    def _observe(self, revision: int, fence: int) -> None:
        """单调检查：revision / fencing_token 只能前进，不能后退。"""
        if revision < 0 or fence < 0:
            raise PlacementError("protocol", "revision / fencing_token 不能为负")
        if revision < self._seen_revision or fence < self._seen_fence:
            raise PlacementError(
                "stale_revision",
                f"arbiter 返回旧版本（revision={revision}, fence={fence}），拒绝复活旧租约",
            )
        self._seen_revision = max(self._seen_revision, revision)
        self._seen_fence = max(self._seen_fence, fence)

    # ------------------------------------------------------------ 状态迁移
    def _expire_if_needed(self) -> bool:
        """本地 TTL 检查；过期了就立即 LOST。调用方必须已持有 ``_lock``。"""
        lease = self._lease
        if self._state is not LeaseState.ACTIVE or lease is None:
            return False
        if self._clock() >= float(lease.expires_at):
            self._fail("expired", "lease TTL 到期")
            return True
        return False

    def _fail(self, code: str, detail: str = "") -> None:
        """统一的 fail-closed：清掉租约、状态变 LOST、只记录不含秘密的原因。"""
        with self._lock:
            was_active = self._state is LeaseState.ACTIVE
            self._lease = None
            self._state = LeaseState.LOST
            self._last_error = self._scrub(str(code or "error"))[:80] or "error"
            self._last_detail = self._scrub(detail)
            error = self._last_error
        if was_active or error in ("auth_failed", "stale_revision"):
            log.warn(f"placement: 租约失效（{error}），已 fail-closed")
        else:
            log.debug(f"placement: 未取得租约（{error}）")

    def _scrub(self, text: str) -> str:
        """防御性脱敏：token / 曾见过的 lease_id 不进入日志或返回消息。"""
        out = str(text or "")
        if self._token:
            out = out.replace(self._token, "***")
        if self._last_lease_id:
            out = out.replace(self._last_lease_id, "***")
        return out

    @staticmethod
    def _map_transport(code: str) -> str:
        if code in ("timeout", "network", "stopped"):
            return code
        return "network"

    @staticmethod
    def _map_http(status: int) -> str:
        if status in (401, 403):
            return "auth_failed"
        if status == 409:
            return "conflict"
        if status == 404:
            return "not_found"
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "server_error"
        return f"http_{int(status)}"

    # ------------------------------------------------------------ HTTP
    def _url(self, path: str) -> str:
        return f"{self._base}/{str(path).lstrip('/')}"

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "PawBot-placement/1",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> tuple[int | None, dict, str]:
        """发一个 JSON 请求。返回 ``(status|None, data, code)``。

        ``status is None`` 表示传输层失败（超时/连不上）；重试只针对传输失败、
        429 和 5xx，401/403/409 等不重试。重试次数最多 ``max_retries + 1`` 次。
        """
        attempts = (self._max_retries if retries is None else max(0, int(retries))) + 1
        timeout = self._timeout if timeout is None else max(0.05, float(timeout))
        last_status: int | None = None
        last_data: dict = {}
        last_code = "network"
        for attempt in range(attempts):
            status, data, code = self._request_once(method, path, payload, timeout)
            last_status, last_data = status, data
            last_code = code if status is None else f"http_{int(status)}"
            if status is not None and status < 500 and status != 429:
                return status, data, code
            if attempt + 1 >= attempts:
                break
            delay = min(self._retry_backoff * (2 ** attempt), 1.0)
            if delay > 0 and self._stop.wait(delay):
                return None, {}, "stopped"
        return last_status, last_data, last_code

    def _request_once(
        self, method: str, path: str, payload: dict | None, timeout: float
    ) -> tuple[int | None, dict, str]:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._url(path), data=body, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except Exception:  # noqa: BLE001 - 错误响应体读不到就按空处理
                raw = b""
        except (socket.timeout, TimeoutError):
            return None, {}, "timeout"
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return None, {}, "timeout"
            return None, {}, "network"
        except (OSError, ValueError):
            return None, {}, "network"

        if len(raw) > MAX_RESPONSE_BYTES:
            return status, {}, "protocol"
        if not raw:
            return status, {}, "ok"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, {}, "protocol"
        if not isinstance(data, dict):
            return status, {}, "protocol"
        return status, data, "ok"
