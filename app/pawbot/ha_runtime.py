"""高可用（A 方案）运行时生命周期控制器：内容副本 + 业务准入。

拓扑与职责边界见 ``docs/HA-ACCEPTANCE.md`` / ``docs/HA-CONTENT-PROTOCOL.md``。
本模块是**可选的**：只有 ``server.mode=relay`` 且显式构造 ``HARuntime`` 时才
生效；reverse / forward 的默认行为完全不经过这里。

它负责四件事（只碰内容与业务生命周期，不碰传输层）：

1. **接管（on_acquired）**：先排空上一段业务 → 拉取 canonical 内容 → 刷新
   内存缓存 → 返回真实 content revision。canonical 还是空的（204 / revision 0）
   时，用当前 lease 把本机内容推上去做 bootstrap；任何一步失败都返回失败，
   让 RelayClient 不发 ``ready``（fail closed）。
2. **内容同步**：``RelayClient`` 在设置 ``on_acquired`` 后会关闭自带同步循环，
   由这里的循环接管——active 时 push，standby 时 pull。owner push 收到 409 时
   先 pull canonical 对齐 CAS base，本轮不重推旧数据；pull 失败则保持显式
   冲突阻塞态，绝不静默把旧本地快照再推一次。
3. **业务准入**：on_acquired 返回后还不能开业务，必须等 ``relay.ready`` 并且
   ``relay.can_emit()`` 为真（ready_ack + upstream 都在线）才启动主动发言 /
   提醒 / B 站生产者并打开准入。relay 已 ready 但 upstream 暂时离线时继续
   等待（不 wedge），恢复后只激活一次生产者。丢租约时立即关闭准入、停生产者、
   只取消**本 runtime owner 名下**的业务任务（绝不 ``tasks.drain``，不碰传输
   重连/心跳/同步任务）。
4. **降级排空**：业务任务取消后没有在时限内结束（取消抵抗）时保持 NOT active，
   不拉取/不应用内容、也不再推送，状态里如实写明原因；下一次接管会重新尝试
   排空，仍然失败就拒绝激活。生产者启动/停止失败同样 fail closed：保留失败
   者的 bookkeeping 以便重试，不重复启动，释放/丢租约时如实报告失败。

明确不承诺：断线窗口内的内容可能没上传；已提交的外部副作用不可撤销；state
（会话、提醒、额度）不随 content 同步迁移；这不是 exactly-once 迁移。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import time
from typing import Any

from . import tasks
from .log import log

#: 运行状态（对外 status()/WebUI 只读展示）
STATE_STANDBY = "standby"        # 不是 owner：只读，不跑业务生产者
STATE_ACTIVATING = "activating"  # 已接管内容，等待 relay.ready/can_emit
STATE_ACTIVE = "active"          # 业务生产者已启动，准入打开
STATE_DEMOTING = "demoting"      # 正在停生产者 / 排空在途业务
STATE_BLOCKED = "blocked"        # fail closed：排空或内容失败，保持只读
STATE_STOPPED = "stopped"

#: 需要显式句柄管理的生产者：组件（start/stop）与定时任务（scheduler）
_COMPONENT_PRODUCERS = ("proactive", "reminder_service")
_TASK_PRODUCERS = (
    ("bili-scroll", "bili_scroll", "_bili_scroll_task"),
    ("bili-post", "bili_post", "_bili_post_task"),
)


class HARuntimeError(RuntimeError):
    """本模块自己抛出的失败（消息是固定文本，不含 URL/token）。"""


def _safe_error(exc: BaseException) -> str:
    """异常 → 不泄漏 URL/token 的短文本。

    只信任本模块自己的 ``HARuntimeError`` 和带协议错误码的
    ``ha_content.ContentError``；其它异常一律只给类型名。
    """
    if isinstance(exc, HARuntimeError):
        return str(exc) or type(exc).__name__
    cls = type(exc)
    if cls.__name__ == "ContentError" and cls.__module__.endswith(".ha_content"):
        code = str(getattr(exc, "code", "") or "")
        message = str(getattr(exc, "message", "") or "")
        if code:
            return f"{code}: {message}" if message and message != code else code
    return cls.__name__


def _is_conflict(exc: BaseException) -> bool:
    return (str(getattr(exc, "code", "") or "") in ("CONFLICT", "STALE_BASE")
            or getattr(exc, "http_status", None) == 409)


def _result_is_conflict(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(result.get("conflict")
                or result.get("status") == 409
                or str(result.get("code") or "") in ("CONFLICT", "STALE_BASE"))


def _result_revision(result: Any) -> int | None:
    """从副本返回值里取 content_revision；不可用时返回 None。"""
    if not isinstance(result, dict):
        return None
    value = result.get("content_revision")
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def _call_replica(replica: Any, name: str, *args):
    """兼容同步 / 异步副本实现，同步阻塞调用放进线程。

    真实 ``ContentReplica.pull/push`` 是阻塞 HTTP（另有 ``*_async`` 适配）；
    测试注入的假副本多为 async。优先用 ``*_async``，否则把同步方法交给
    ``asyncio.to_thread``——**绝不直接 await 同步方法的返回值**。
    """
    async_method = getattr(replica, f"{name}_async", None)
    if callable(async_method):
        result = async_method(*args)
        return await result if inspect.isawaitable(result) else result
    method = getattr(replica, name, None)
    if not callable(method):
        raise HARuntimeError(f"内容副本缺少 {name}()")
    if inspect.iscoroutinefunction(method):
        return await method(*args)
    result = await asyncio.to_thread(method, *args)
    return await result if inspect.isawaitable(result) else result


class HARuntime:
    """opt-in 的 active/passive 生命周期控制器。

    :param bot: Bot 实例（读取 persona/longmem/relations/memes/hobbies/album
        与生产者组件）
    :param relay: ``RelayClient`` 或实现同一回调契约的对象；构造时把
        ``on_acquired`` / ``on_lost`` / ``before_release`` 指向本控制器
    :param replica: 可注入的内容副本（真实 ``ContentReplica`` 或测试替身）；
        为空时按 relay 的 content_root/content_url 延迟构造
    :param sync_interval: 内容同步周期；为空时沿用 relay.sync_interval
    :param ready_timeout: 内容接管后等待 relay 进入 ready 的上限；relay 已 ready
        但 upstream/can_emit 暂未恢复时继续有界等待，恢复后自动激活
    :param producer_timeout: 单个生产者启动/停止的等待上限
    :param demote_timeout: 丢租约/释放时等待业务任务结束的上限
    """

    def __init__(
        self,
        bot,
        relay,
        *,
        replica: Any | None = None,
        content_root: str | None = None,
        sync_interval: float | None = None,
        ready_timeout: float = 60.0,
        producer_timeout: float = 3.0,
        demote_timeout: float = 2.0,
        ready_poll: float = 0.05,
        webui: Any | None = None,
        auto_attach: bool = True,
    ):
        self.bot = bot
        self.relay = relay
        self.replica = replica
        self.content_root = content_root or getattr(relay, "content_root", None)
        if sync_interval is None:
            sync_interval = getattr(relay, "sync_interval", 3.0)
        try:
            self.sync_interval = max(0.1, float(sync_interval or 3.0))
        except (TypeError, ValueError):
            self.sync_interval = 3.0
        self.ready_timeout = max(0.1, float(ready_timeout or 60.0))
        self.producer_timeout = max(0.1, float(producer_timeout or 3.0))
        self.demote_timeout = max(0.0, float(demote_timeout or 2.0))
        self.ready_poll = max(0.01, float(ready_poll or 0.05))
        self._webui = webui

        self._state = STATE_STANDBY
        self._admission = False
        self._started = False
        self._stopping = False
        self._demote_requested = False
        self._drained = True
        self._released = False

        self._content_revision = 0
        self._push_blocked = False
        # 409 后必须先 pull canonical 对齐 base；在恢复成功之前禁止任何 push。
        # 与 ``_push_blocked`` 分开保存，status 里能如实说明「为什么被阻塞」。
        self._content_conflict = False
        self._content_conflict_reason: str | None = None
        self._blocked_reason: str | None = None
        self._ready_wait_reason: str | None = None
        self._last_error: str | None = None
        self._last_error_logged: str | None = None
        self._lost_reason: str | None = None
        self._producer_notes: list[str] = []
        self._stuck_tasks: list[asyncio.Task] = []
        self._dropped_events = 0
        # 事件包装正在执行的业务事件数（不是 owned task：这些跑在传输 worker 上，
        # 不能取消；只用于在它们结束前禁止应用新内容/激活新一段业务）。
        self._inflight_events = 0
        self._events_idle = asyncio.Event()
        self._events_idle.set()

        self._producers_started = False
        self._started_components: set[str] = set()
        self._producer_tasks: dict[str, asyncio.Task] = {}
        self._bot_producer_attrs: dict[str, str] = {}

        self._sync_task: asyncio.Task | None = None
        self._ready_task: asyncio.Task | None = None
        self._state_lock = asyncio.Lock()
        self._replica_lock = asyncio.Lock()

        # 只用于取消/分组，不写日志、不进 status。
        self._business_owner = f"ha-business-{id(self):x}"
        self._previous_callbacks: dict[str, Any] = {}
        if auto_attach:
            self.attach()

    # ------------------------------------------------------------ 接线
    def attach(self) -> "HARuntime":
        """把三个生命周期回调挂到 relay 上（保留旧值以便 detach）。"""
        for attr, callback in (
            ("on_acquired", self.on_acquired),
            ("on_lost", self.on_lost),
            ("before_release", self.before_release),
        ):
            try:
                # 只在第一次挂接时保存原值；重复 start() 不覆盖它。
                if attr not in self._previous_callbacks:
                    self._previous_callbacks[attr] = getattr(self.relay, attr, None)
                setattr(self.relay, attr, callback)
            except Exception as exc:  # noqa: BLE001 - 不可写就明确报错
                log.warn(f"HA 运行时无法挂接 {attr}：{type(exc).__name__}")
        return self

    def detach(self) -> None:
        """恢复 relay 上原有的回调（stop 不自动调用，避免影响释放流程）。"""
        for attr, previous in self._previous_callbacks.items():
            try:
                if getattr(self.relay, attr, None) in (
                        self.on_acquired, self.on_lost, self.before_release):
                    setattr(self.relay, attr, previous)
            except Exception:  # noqa: BLE001
                pass
        self._previous_callbacks.clear()

    def bind_webui(self, webui) -> "HARuntime":
        """登记 WebUI：降级时要等（或取消）在途内容写入。"""
        self._webui = webui
        return self

    # ------------------------------------------------------------ 公开状态
    @property
    def mode(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._state == STATE_ACTIVE

    @property
    def content_revision(self) -> int:
        return self._content_revision

    @property
    def business_owner(self) -> str:
        return self._business_owner

    def business_scope(self):
        """给事件包装层用的 owner 作用域（``with runtime.business_scope():``）。"""
        return tasks.scope(self._business_owner)

    def _transport_ready(self) -> bool:
        """relay 已 ready 且能外发（can_emit 含 lease + ready + upstream）。"""
        can_emit = getattr(self.relay, "can_emit", None)
        if callable(can_emit):
            try:
                value = can_emit()
                if inspect.isawaitable(value):
                    return True
                return bool(value)
            except Exception:  # noqa: BLE001 - 状态判断失败按不可用处理
                return False
        ready = getattr(self.relay, "ready", None)
        if ready is not None:
            return bool(ready)
        return True

    def can_mutate(self) -> bool:
        """当前节点是否允许业务/内容写入（工具与 WebUI 的准入闸门）。"""
        return bool(
            not self._stopping
            and self._admission
            and self._state == STATE_ACTIVE
            and self._transport_ready()
        )

    def allow_business(self) -> bool:
        """:meth:`can_mutate` 的别名（业务准入）。"""
        return self.can_mutate()

    def mutation_denied_reason(self, kind: str = "") -> str | None:
        """不允许写入时返回人能看懂的原因；允许时返回 None（不含敏感信息）。"""
        if self.can_mutate():
            return None
        if self._blocked_reason:
            return f"HA {self._state}：{self._blocked_reason}"
        labels = {
            STATE_STANDBY: "当前是 standby（没有 lease），业务与内容写入关闭",
            STATE_ACTIVATING: "正在接管内容，业务准入尚未打开",
            STATE_ACTIVE: "relay 当前不可外发（ready/upstream 未就绪），拒绝写入",
            STATE_DEMOTING: "租约丢失，正在停止生产者/排空在途业务",
            STATE_BLOCKED: "HA 运行时处于 fail-closed 状态，业务与内容写入关闭",
            STATE_STOPPED: "HA 运行时已停止",
        }
        return labels.get(self._state, f"HA 运行时状态为 {self._state}，拒绝写入")

    def status(self) -> dict:
        """只读状态：不含 token / lease_id / URL 等任何凭据。"""
        lease = getattr(self.relay, "lease", None)
        return {
            "mode": self._state,
            "active": self.active,
            "admission": bool(self._admission),
            "mutable": self.can_mutate(),
            "drained": bool(self._drained),
            "content_revision": int(self._content_revision),
            "has_lease": isinstance(lease, dict),
            "relay_ready": bool(getattr(self.relay, "ready", False)),
            "can_emit": self._transport_ready(),
            "sync_interval": self.sync_interval,
            "producers_started": bool(self._producers_started),
            "producer_tasks": sorted(self._producer_tasks),
            "producer_notes": list(self._producer_notes),
            "owned_tasks": len(tasks.pending(self._business_owner)),
            "inflight_events": int(self._inflight_events),
            "dropped_events": int(self._dropped_events),
            "push_blocked": bool(self._push_blocked),
            "content_conflict": bool(self._content_conflict),
            "content_conflict_reason": self._content_conflict_reason,
            "ready_wait_reason": self._ready_wait_reason,
            "blocked_reason": self._blocked_reason,
            "lost_reason": self._lost_reason,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------ 生命周期入口
    async def start(self) -> None:
        """启动内容同步循环。生产者仍要等 relay ready/can_emit 才启动。"""
        if self._started and not self._stopping:
            return
        self._started = True
        self._stopping = False
        self.attach()
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = tasks.spawn(
                self._sync_loop(), name="ha-runtime-sync", owner=None)

    async def stop(self) -> None:
        """停止控制器：关准入、停生产者、排空业务、取消自有控制任务。

        不主动 push：最后一次有权推送属于 ``before_release``（relay.stop 会先调
        它）。stop 可重复调用。
        """
        self._stopping = True
        self._admission = False
        self._demote_requested = True
        ready, self._ready_task = self._ready_task, None
        if ready is not None and not ready.done():
            ready.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ready
        async with self._state_lock:
            notes = await self._stop_producers("stop")
            drained = await self._drain_owned("stop")
            self._producer_notes = list(notes)
            self._drained = bool(drained)
            # 生产者没停干净 / 业务没排空时不能假装「已停止」：保留 bookkeeping，
            # 让下一次接管或释放仍能重试，并把失败写进状态。
            reasons = list(notes)
            if not drained:
                reasons.append("本 owner 的业务任务没有在时限内结束")
            if reasons:
                self._state = STATE_BLOCKED
                self._blocked_reason = "停止 HA 运行时失败：" + "；".join(reasons)
                self._note_text("停止", self._blocked_reason)
            else:
                self._state = STATE_STOPPED
                self._blocked_reason = None
            self._admission = False
        sync, self._sync_task = self._sync_task, None
        if sync is not None and not sync.done():
            sync.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sync
        self._started = False

    # ------------------------------------------------------------ relay 回调
    async def on_acquired(self, client=None, lease: dict | None = None) -> int:
        """hello 之后、ready 之前的门禁：排空 → pull → 刷新缓存 → 返回 revision。"""
        if self._stopping:
            raise HARuntimeError("HA 运行时已停止，拒绝接管")
        lease = dict(lease or getattr(client, "lease", None) or {})
        self._admission = False
        self._demote_requested = False
        self._released = False
        self._push_blocked = False
        self._content_conflict = False
        self._content_conflict_reason = None
        self._ready_wait_reason = None
        async with self._state_lock:
            self._state = STATE_ACTIVATING
            self._blocked_reason = None
            self._lost_reason = None
            if not await self._quiesce_for_takeover():
                self._state = STATE_BLOCKED
                self._admission = False
                # _quiesce_for_takeover 已经写入了更具体的原因（生产者停不掉 /
                # 任务未排空）；只有没有具体原因时才用这条兜底文本。
                self._blocked_reason = self._blocked_reason or (
                    "上一次的业务任务没有在时限内结束，拒绝接管（不会拉取/应用新内容）")
                self._note_text("接管", self._blocked_reason)
                raise HARuntimeError(self._blocked_reason)
            try:
                revision = await self._acquire_content(lease)
            except asyncio.CancelledError:
                self._state = STATE_BLOCKED
                self._admission = False
                raise
            except Exception as exc:  # noqa: BLE001 - 激活失败 = 不 ready
                self._state = STATE_BLOCKED
                self._admission = False
                self._blocked_reason = f"接管内容失败：{_safe_error(exc)}"
                self._note_error("接管内容", exc)
                raise
            self._content_revision = int(revision)
            self._drained = True
            self._stuck_tasks = []
            self._arm_ready_watch(lease)
            return int(revision)

    async def on_lost(self, client=None) -> None:
        """租约丢失：立即关准入 → 停生产者 → 只取消本 owner 业务任务并等待。"""
        self._admission = False
        self._demote_requested = True
        self._ready_wait_reason = None
        self._lost_reason = "lease_lost"
        async with self._state_lock:
            if self._state == STATE_STOPPED:
                return
            self._state = STATE_DEMOTING
            self._blocked_reason = None
            self._cancel_ready_watch(exclude_current=True)
            notes = await self._stop_producers("lease_lost")
            drained = await self._drain_owned("lease_lost")
            self._producer_notes = list(notes)
            self._drained = bool(drained)
            reasons: list[str] = []
            if notes:
                # fail closed：停止失败时绝不把节点当成干净的 standby。
                reasons.append("生产者未能干净停止（" + "；".join(notes) + "）")
            if not drained:
                reasons.append(
                    "在途业务任务没有在时限内结束；不拉取/应用内容，"
                    "等待下一次接管重试排空")
            if reasons:
                self._state = STATE_BLOCKED
                self._blocked_reason = "租约丢失：" + "；".join(reasons)
                self._note_text("降级", self._blocked_reason)
            else:
                self._state = STATE_STANDBY
                self._blocked_reason = None
            self._admission = False

    async def before_release(self, client=None) -> dict:
        """主动释放前：停生产者 → 排空业务 → flush 内容 → 最后一次有权推送。

        任何一步失败都必须返回 ``ok=False``：生产者没停干净时跳过内容推送
        （它们可能还在写），pull/base 冲突未解除时也不假装推送成功。
        """
        self._admission = False
        self._demote_requested = True
        self._ready_wait_reason = None
        async with self._state_lock:
            if self._state == STATE_STOPPED:
                return {"ok": True, "drained": True, "pushed": False,
                        "blocked": False, "notes": []}
            self._state = STATE_DEMOTING
            self._blocked_reason = None
            self._cancel_ready_watch(exclude_current=True)
            notes = await self._stop_producers("release")
            producer_failed = bool(notes)
            drained = await self._drain_owned("release")
            if drained and not await self._wait_events_idle(self.demote_timeout):
                drained = False
            self._drained = bool(drained)
            pushed = False
            failure = ""
            if not drained:
                failure = "释放前在途业务任务没有在时限内结束，已跳过最后一次内容推送"
            elif producer_failed:
                failure = "释放前生产者未能干净停止，已跳过最后一次内容推送"
            else:
                self._flush_content_components()
                result = await self.push_content(reason="release")
                pushed = bool(isinstance(result, dict) and result.get("ok"))
                if not pushed:
                    code = ""
                    if isinstance(result, dict):
                        code = str(result.get("code") or "")
                    failure = ("释放前最后一次内容推送失败"
                               + (f"（{code}）" if code else ""))
            if failure:
                self._blocked_reason = failure
                self._note_text("释放", failure)
            self._released = True
            self._admission = False
            ok = bool(drained and not producer_failed and pushed)
            self._state = STATE_STANDBY if ok else STATE_BLOCKED
            self._producer_notes = list(notes)
            return {
                "ok": ok,
                "drained": bool(drained),
                "pushed": bool(pushed),
                "blocked": not ok,
                "producer_failed": bool(producer_failed),
                "error": failure or None,
                "blocked_reason": self._blocked_reason,
                "notes": list(notes),
            }

    # ------------------------------------------------------------ 事件准入
    async def handle_event(self, conn, raw: dict, original):
        """业务事件包装：非 active 直接丢弃；active 时以 owner 作用域执行。

        ``original`` 是 core 原来的 ``_on_event``。这里只包一层准入和
        ContextVar owner：事件内部 ``tasks.spawn`` 出来的任务都可以在丢租约时
        按 owner 取消，而 OneBot worker / relay 心跳等传输任务不受影响。
        """
        if self._stopping or not self.can_mutate():
            self._dropped_events += 1
            log.debug("HA 非 active，丢弃业务事件")
            return None
        self._inflight_events += 1
        self._events_idle.clear()
        try:
            with tasks.scope(self._business_owner):
                if not self.can_mutate():
                    self._dropped_events += 1
                    return None
                return await original(conn, raw)
        finally:
            self._inflight_events = max(0, self._inflight_events - 1)
            if self._inflight_events == 0:
                self._events_idle.set()

    # ------------------------------------------------------------ 内容
    def _ensure_replica(self):
        if self.replica is not None:
            return self.replica
        root = self.content_root
        if root is None:
            try:
                from .relay_client import derive_content_root  # noqa: PLC0415
                root = derive_content_root(getattr(self.bot, "config", None))
            except Exception:  # noqa: BLE001
                root = None
        if not root:
            self._note_text("内容副本", "无法确定 content 根（需要显式 content_root 或 replica）")
            return None
        url = str(getattr(self.relay, "content_url", "") or "")
        if not url:
            self._note_text("内容副本", "relay 没有 content_url，无法同步内容")
            return None
        try:
            from . import ha_content  # noqa: PLC0415 - 运行到再加载
            self.replica = ha_content.ContentReplica(
                root,
                url,
                token=str(getattr(self.relay, "access_token", "") or ""),
                timeout=float(getattr(self.relay, "sync_timeout", 10.0) or 10.0),
            )
        except Exception as exc:  # noqa: BLE001
            self._note_error("内容副本初始化", exc)
            return None
        return self.replica

    async def _acquire_content(self, lease: dict) -> int:
        """pull canonical；canonical 为空时用当前 lease bootstrap 本机内容。"""
        result = await self._pull_content()
        revision = _result_revision(result)
        if revision is None:
            raise HARuntimeError("内容副本没有返回合法的 content_revision")
        if revision == 0:
            # 中继上还没有内容（204）：把本机现有内容作为初始内容推上去。
            pushed = await self._push_content(lease, reason="bootstrap")
            revision = _result_revision(pushed)
            if revision is None:
                raise HARuntimeError("bootstrap 推送没有返回合法的 content_revision")
        self.refresh_content_caches()
        return revision

    async def _pull_content(self) -> dict:
        replica = self._ensure_replica()
        if replica is None:
            raise HARuntimeError("内容副本不可用")
        async with self._replica_lock:
            result = await _call_replica(replica, "pull")
        if not isinstance(result, dict) or not result.get("ok"):
            raise HARuntimeError("内容拉取未成功")
        return result

    async def _push_content(self, lease: dict, *, reason: str = "") -> dict:
        if self._push_blocked:
            return {"ok": False, "code": "PUSH_BLOCKED",
                    "conflict": bool(self._content_conflict),
                    "reason": self._content_conflict_reason}
        payload = self._lease_payload(lease)
        if payload is None:
            return {"ok": False, "code": "NO_LEASE"}
        replica = self._ensure_replica()
        if replica is None:
            return {"ok": False, "code": "NO_REPLICA"}
        async with self._replica_lock:
            try:
                result = await _call_replica(replica, "push", payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 冲突后不再自动覆盖远端
                if _is_conflict(exc):
                    self._mark_content_conflict(
                        "push_conflict",
                        "CAS 冲突（409）：canonical 已被推进，暂停推送；"
                        "先拉取 canonical 对齐 base（未同步的本地改动以 canonical 为准）")
                else:
                    self._note_error("内容推送", exc)
                return {"ok": False, "code": _safe_error(exc),
                        "conflict": _is_conflict(exc)}
        if not isinstance(result, dict) or not result.get("ok"):
            if _result_is_conflict(result):
                self._mark_content_conflict(
                    "push_conflict",
                    "CAS 冲突（409）：canonical 已被推进，暂停推送；"
                    "先拉取 canonical 对齐 base（未同步的本地改动以 canonical 为准）")
            else:
                self._note_text("内容推送", "推送未成功")
            return result if isinstance(result, dict) else {"ok": False}
        revision = _result_revision(result)
        if revision is not None:
            self._content_revision = revision
        self._clear_content_conflict()
        log.debug(f"HA 内容推送完成（{reason or 'sync'}，revision={self._content_revision}）")
        return result

    async def push_content(self, reason: str = "") -> dict:
        """用当前 lease 推送本机内容（release 前和周期同步都走这里）。

        处于 409 冲突阻塞态时，必须先拉取 canonical 把 CAS base 对齐；拉取失败
        就保持阻塞并如实返回，**绝不**用旧 base 重推旧本地快照。
        """
        if not self._is_lease_owner():
            return {"ok": False, "code": "NOT_OWNER"}
        lease = self._current_lease()
        if lease is None:
            return {"ok": False, "code": "NO_LEASE"}
        if self._content_conflict:
            if not await self._recover_content_conflict():
                return {"ok": False, "code": "CONTENT_CONFLICT",
                        "conflict": True,
                        "reason": self._content_conflict_reason}
            lease = self._current_lease()
            if lease is None:
                return {"ok": False, "code": "NO_LEASE"}
        return await self._push_content(lease, reason=reason)

    def _mark_content_conflict(self, reason: str, message: str) -> None:
        """409 后进入「先 pull 对齐 base」的阻塞态；不覆盖 canonical。"""
        self._content_conflict = True
        self._content_conflict_reason = reason
        self._push_blocked = True
        self._note_text("内容推送", message)

    def _clear_content_conflict(self) -> None:
        self._content_conflict = False
        self._content_conflict_reason = None
        self._push_blocked = False

    async def _recover_content_conflict(self) -> bool:
        """409 后的恢复：只 pull canonical，把 base/revision 对齐；本轮不 push。

        pull 会应用 canonical（未推送的本地改动以 canonical 为准，这一点在状态
        与日志里明确写出，不静默假装同步成功）。pull 失败时保持阻塞，下一次
        同步周期继续重试，期间绝不 push 旧数据。
        """
        if not self._content_conflict:
            return True
        try:
            result = await self._pull_content()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 保持阻塞，下一轮再试
            self._mark_content_conflict(
                "pull_conflict",
                f"内容冲突恢复失败：拉取 canonical 失败（{_safe_error(exc)}）；"
                "保持禁止推送，不重推旧数据")
            return False
        revision = _result_revision(result)
        if revision is None:
            self._mark_content_conflict(
                "pull_conflict",
                "内容冲突恢复失败：拉取没有返回合法 content_revision；"
                "保持禁止推送，不重推旧数据")
            return False
        changed = bool(result.get("changed")) or revision != self._content_revision
        self._content_revision = int(revision)
        if changed:
            self.refresh_content_caches()
        self._clear_content_conflict()
        # 明确记录「本地未同步改动以 canonical 为准」，不是静默覆盖。
        self._note_text(
            "内容推送",
            "409 后已拉取 canonical 对齐 base；未同步的本地改动以 canonical 为准，"
            "后续本地写入可以继续推送")
        return True

    def _current_lease(self) -> dict | None:
        lease = getattr(self.relay, "lease", None)
        if not isinstance(lease, dict):
            return None
        return self._lease_payload(lease)

    def _lease_payload(self, lease: dict | None) -> dict | None:
        if not isinstance(lease, dict):
            return None
        out = {}
        for key in ("lease_id", "epoch", "revision"):
            value = lease.get(key)
            if value is None:
                return None
            out[key] = value
        out["content_revision"] = int(self._content_revision)
        return out

    def _is_lease_owner(self) -> bool:
        """内容 push 只要求仍是 lease owner（不要求 upstream 在线）。"""
        return bool(getattr(self.relay, "ready", False)
                    and isinstance(getattr(self.relay, "lease", None), dict))

    async def _standby_pull(self) -> None:
        if not self._drained or self._stuck_tasks:
            return
        if self._inflight_events > 0:
            # 旧事件还在写内容：本轮不应用新快照，等它结束后的下一轮。
            return
        result = await self._pull_content()
        revision = _result_revision(result)
        if revision is None:
            return
        changed = bool(result.get("changed")) or revision != self._content_revision
        self._content_revision = revision
        if revision > 0 and changed:
            # 先刷新内存缓存再允许业务；绝不 flush 旧脏缓存覆盖刚拉下来的内容。
            self.refresh_content_caches()

    # ------------------------------------------------------------ 缓存刷新
    def refresh_content_caches(self) -> dict:
        """pull / 接管后刷新内存里的内容缓存（**只读盘，不 flush**）。"""
        refreshed: list[str] = []
        errors: list[str] = []
        bot = self.bot

        persona = getattr(bot, "persona", None)
        cache = getattr(persona, "_cache", None)
        if isinstance(cache, dict):
            try:
                cache.clear()
                refreshed.append("persona")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"persona:{type(exc).__name__}")

        longmem = getattr(bot, "longmem", None)
        loader = getattr(longmem, "load", None)
        if callable(loader) and hasattr(longmem, "episodes"):
            try:
                longmem.episodes = loader()
                refreshed.append("longmem")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"longmem:{type(exc).__name__}")

        life_store = getattr(bot, "life_store", None)
        reload_life = getattr(life_store, "reload", None)
        if callable(reload_life):
            try:
                # 只重读盘上内容；绝不把本机旧状态写回去。
                reload_life()
                refreshed.append("life_store")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"life_store:{type(exc).__name__}")

        relations = getattr(bot, "relations", None)
        loader = getattr(relations, "load", None)
        if callable(loader):
            try:
                lock = getattr(relations, "_lock", None)
                if lock is not None:
                    with lock:
                        loader()
                else:
                    loader()
                refreshed.append("relations")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"relations:{type(exc).__name__}")

        memes = getattr(bot, "memes", None)
        loader = getattr(memes, "_load", None) or getattr(memes, "load", None)
        if callable(loader):
            try:
                loader()
                # 旧内存里的 dirty 不能在新内容上再 flush 一次。
                if hasattr(memes, "_dirty"):
                    memes._dirty = False
                refreshed.append("memes")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"memes:{type(exc).__name__}")

        hobbies = getattr(bot, "hobbies", None)
        loader = getattr(hobbies, "load", None)
        if callable(loader) and hasattr(hobbies, "items"):
            try:
                hobbies.items = loader()
                refreshed.append("hobbies")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"hobbies:{type(exc).__name__}")

        album = getattr(bot, "album", None)
        loader = (getattr(album, "reload", None)
                  or getattr(album, "load", None)
                  or getattr(album, "_load", None))
        if callable(loader):
            try:
                loader()
                refreshed.append("album")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"album:{type(exc).__name__}")

        for label in errors:
            log.warn(f"HA 内容缓存刷新失败：{label}")
        return {"ok": not errors, "refreshed": refreshed, "errors": errors}

    def _flush_content_components(self) -> list[str]:
        """释放前把本机内容组件的脏数据落盘（只 flush 当前 active 数据）。"""
        flushed: list[str] = []
        for name in ("memes", "relations"):
            component = getattr(self.bot, name, None)
            flush = getattr(component, "flush", None)
            if not callable(flush):
                continue
            try:
                if flush():
                    flushed.append(name)
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"{name} 落盘", exc)
        return flushed

    # ------------------------------------------------------------ 生产者
    async def _start_producers(self) -> list[str]:
        """启动主动发言 / 提醒 / B 站生产者；返回失败说明（空 = 全部启动）。

        幂等：已经在 bookkeeping 里的组件/任务不会重复启动。启动失败不在这里
        静默吞掉——由 :meth:`_activate_business` 判定 fail closed。
        """
        notes: list[str] = []
        for name in _COMPONENT_PRODUCERS:
            if name in self._started_components:
                continue
            component = getattr(self.bot, name, None)
            start = getattr(component, "start", None)
            if not callable(start):
                notes.append(f"{name} 不可用")
                continue
            try:
                await _call_with_timeout(start, self.producer_timeout)
                self._started_components.add(name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 失败交给调用方 fail closed
                notes.append(f"{name} 启动失败：{_safe_error(exc)}")
                self._note_error(f"{name} 启动", exc)
        for label, module_name, attr in _TASK_PRODUCERS:
            existing = self._producer_tasks.get(label)
            if existing is not None and not existing.done():
                # 已有活着的同 label 生产者：绝不启动第二份。
                continue
            if existing is not None:
                # 上一份已经结束：清掉过期 bookkeeping 后再起新的。
                self._producer_tasks.pop(label, None)
                self._clear_bot_producer_attr(label, existing)
            try:
                module = importlib.import_module(f".{module_name}", __package__)
                scheduler = getattr(module, "scheduler")
                # 生产者有显式句柄；owner=None 防止被业务 owner 的取消误伤。
                task = tasks.spawn(scheduler(self.bot), name=label, owner=None)
                self._producer_tasks[label] = task
                self._bot_producer_attrs[label] = attr
                try:
                    setattr(self.bot, attr, task)
                except Exception:  # noqa: BLE001
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{label} 启动失败：{_safe_error(exc)}")
                self._note_error(f"{label} 启动", exc)
        self._producers_started = bool(self._started_components
                                       or self._producer_tasks)
        self._producer_notes = list(notes)
        return notes

    def _clear_bot_producer_attr(self, label: str, task) -> None:
        attr = self._bot_producer_attrs.pop(label, None)
        if not attr:
            return
        try:
            if task is None or getattr(self.bot, attr, None) is task:
                setattr(self.bot, attr, None)
        except Exception:  # noqa: BLE001
            pass

    async def _stop_producers(self, reason: str = "") -> list[str]:
        """取消生产者任务并停组件；返回失败/超时说明（空 = 干净停止）。

        **失败时绝不清掉 bookkeeping**：停不掉的组件/任务仍留在
        ``_started_components`` / ``_producer_tasks`` 里，下一次停止重试，且
        ``_start_producers`` 不会为它们启动第二份。
        """
        notes: list[str] = []
        for task in list(self._producer_tasks.values()):
            if task is not None and not task.done():
                task.cancel()
        running = {
            label: task for label, task in self._producer_tasks.items()
            if task is not None and not task.done()
        }
        if running:
            await asyncio.wait(list(running.values()),
                               timeout=self.producer_timeout)
        for label, task in list(self._producer_tasks.items()):
            if task is None or task.done():
                self._producer_tasks.pop(label, None)
                self._clear_bot_producer_attr(label, task)
            else:
                notes.append(f"{label} 未在时限内停止")

        for name in _COMPONENT_PRODUCERS:
            if name not in self._started_components:
                continue
            component = getattr(self.bot, name, None)
            stop = getattr(component, "stop", None)
            if not callable(stop):
                notes.append(f"{name} 不可用，无法停止")
                continue
            try:
                await _call_with_timeout(stop, self.producer_timeout)
            except asyncio.TimeoutError:
                notes.append(f"{name} 停止超时")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{name} 停止失败：{_safe_error(exc)}")
                self._note_error(f"{name} 停止", exc)
            else:
                self._started_components.discard(name)
        self._producers_started = bool(self._started_components
                                       or self._producer_tasks)
        if notes:
            log.warn(f"HA 生产者停止（{reason or 'demote'}）：" + "；".join(notes))
        return notes

    # ------------------------------------------------------------ 排空
    async def _drain_owned(self, reason: str = "") -> bool:
        """只取消本 owner 的业务任务 + 在途 WebUI 内容写入，等待有界。"""
        remaining = await tasks.cancel_owned(
            self._business_owner, timeout=self.demote_timeout)
        if remaining:
            self._stuck_tasks = list(remaining)
            self._note_text(
                "排空", f"{len(remaining)} 个业务任务取消后仍未结束（{reason}）")
            return False
        if not await self._drain_webui():
            return False
        self._stuck_tasks = []
        return True

    async def _wait_events_idle(self, timeout: float) -> bool:
        """等传输 worker 上正在执行的业务事件结束（不取消它们）。"""
        if self._inflight_events <= 0:
            return True
        try:
            await asyncio.wait_for(self._events_idle.wait(),
                                   timeout=max(0.0, float(timeout)))
        except asyncio.TimeoutError:
            self._note_text(
                "排空", f"{self._inflight_events} 个业务事件在途，未在时限内结束")
            return False
        return self._inflight_events <= 0

    async def _drain_webui(self) -> bool:
        webui = self._webui
        active = getattr(webui, "active_mutations", None)
        if not callable(active):
            return True
        try:
            current = asyncio.current_task()
            victims = [task for task in active() if task is not current and not task.done()]
        except Exception as exc:  # noqa: BLE001
            self._note_error("读取 WebUI 在途写入", exc)
            return True
        if not victims:
            return True
        for task in victims:
            task.cancel()
        _done, still = await asyncio.wait(
            victims, timeout=self.demote_timeout)
        remaining = [task for task in still if not task.done()]
        if remaining:
            self._stuck_tasks = list(remaining)
            self._note_text("排空", f"{len(remaining)} 个 WebUI 内容写入未结束")
            return False
        return True

    async def _quiesce_for_takeover(self) -> bool:
        """接管前：停掉可能残留的生产者，排空上一段业务任务与在途事件。"""
        if self._producers_started or self._producer_tasks or self._started_components:
            notes = await self._stop_producers("takeover")
            if notes:
                # 停不干净就拒绝接管：不能让旧生产者与新一轮业务并发写内容。
                self._producer_notes = list(notes)
                self._blocked_reason = (
                    "接管前生产者未能干净停止（" + "；".join(notes)
                    + "）；拒绝接管，不拉取/应用新内容")
                self._note_text("接管", self._blocked_reason)
                return False
        drained = await self._drain_owned("takeover")
        if not drained:
            return False
        if any(not task.done() for task in self._stuck_tasks):
            return False
        if not await self._wait_events_idle(self.demote_timeout):
            return False
        return True

    # ------------------------------------------------------------ ready 观察
    def _arm_ready_watch(self, lease: dict) -> None:
        epoch = str(lease.get("epoch") or "")
        lease_id = str(lease.get("lease_id") or "")
        old, self._ready_task = self._ready_task, None
        if old is not None and not old.done():
            old.cancel()
        self._ready_task = tasks.spawn(
            self._watch_ready(epoch, lease_id), name="ha-runtime-ready", owner=None)

    def _cancel_ready_watch(self, *, exclude_current: bool = False) -> None:
        task, self._ready_task = self._ready_task, None
        if task is None or task.done():
            return
        if exclude_current and task is asyncio.current_task():
            return
        task.cancel()

    def _lease_matches(self, epoch: str, lease_id: str) -> bool:
        lease = getattr(self.relay, "lease", None)
        if not isinstance(lease, dict):
            # 测试替身没有 lease 字段时以 ready 状态为准。
            return True
        if epoch and str(lease.get("epoch") or "") != epoch:
            return False
        if lease_id and str(lease.get("lease_id") or "") != lease_id:
            return False
        return True

    async def _watch_ready(self, epoch: str, lease_id: str) -> None:
        """等 relay.ready + can_emit 后启动业务；期间任何降级请求都放弃。

        ``ready_timeout`` 只约束「等 relay 进入 ready」：relay 已经 ready、但
        upstream 断开 / can_emit 为假时不能因此永久卡死。只要租约还在、状态仍是
        ACTIVATING，就继续有界轮询；upstream 恢复后自动激活（只激活一次）。
        丢租约 / stop / 重新接管都会取消本任务。
        """
        deadline = time.monotonic() + self.ready_timeout
        ready_seen = False
        while True:
            if self._stopping or self._demote_requested:
                return
            if self._state != STATE_ACTIVATING:
                return
            if not self._lease_matches(epoch, lease_id):
                self._blocked_reason = "relay lease/epoch 已变化，放弃本次激活"
                self._note_text("激活", self._blocked_reason)
                return
            if self._transport_ready():
                async with self._state_lock:
                    if (self._stopping or self._demote_requested
                            or self._state != STATE_ACTIVATING
                            or not self._lease_matches(epoch, lease_id)):
                        return
                    await self._activate_business()
                return
            relay_ready = bool(getattr(self.relay, "ready", False))
            if relay_ready:
                ready_seen = True
            now = time.monotonic()
            if now >= deadline:
                if not ready_seen:
                    self._blocked_reason = (
                        "等待 relay ready 超时，业务保持关闭（内容已接管）")
                    self._note_text("激活", self._blocked_reason)
                    return
                # relay 已 ready，只是 upstream / can_emit 还没恢复：不 wedge，
                # 继续轮询（租约/状态是边界），恢复后由同一个 watcher 激活。
                message = (
                    "relay 已 ready 但 upstream/can_emit 未就绪；继续等待，"
                    "恢复后自动激活（租约仍有效）")
                if message != self._ready_wait_reason:
                    self._ready_wait_reason = message
                    log.warn("HA 激活：" + message)
            await asyncio.sleep(self.ready_poll)

    async def _activate_business(self) -> None:
        if self._admission or self._state != STATE_ACTIVATING:
            return
        notes = await self._start_producers()
        if notes:
            # fail closed：生产者没起来就不打开准入，也不假装 active。
            self._ready_wait_reason = None
            self._blocked_reason = "业务生产者启动失败：" + "；".join(notes)
            self._note_text("激活", self._blocked_reason)
            cleanup = await self._stop_producers("activation_failed")
            self._producer_notes = list(notes)
            if cleanup:
                self._producer_notes.extend(f"激活失败清理：{note}" for note in cleanup)
            self._state = STATE_BLOCKED
            self._admission = False
            return
        self._ready_wait_reason = None
        self._producer_notes = list(notes)
        self._state = STATE_ACTIVE
        self._admission = True
        log.info("HA 业务已激活（relay ready 且可外发）")

    # ------------------------------------------------------------ 同步循环
    async def _sync_loop(self) -> None:
        try:
            while not self._stopping:
                try:
                    await asyncio.sleep(self.sync_interval)
                except asyncio.CancelledError:
                    raise
                if self._stopping:
                    return
                try:
                    await self._sync_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 下一轮再试
                    self._note_error("内容同步", exc)
        except asyncio.CancelledError:
            raise

    async def _sync_once(self) -> None:
        state = self._state
        if state == STATE_ACTIVE:
            if not self._is_lease_owner():
                return
            if self._content_conflict:
                # 409 之后本轮只 pull 对齐 base；下一轮才允许 push。
                await self._recover_content_conflict()
                return
            await self.push_content(reason="periodic")
            return
        if state != STATE_STANDBY:
            return
        # 与 on_acquired 共用状态锁：接管正在 pull/刷新时，standby 不能并发写盘。
        async with self._state_lock:
            if self._state != STATE_STANDBY:
                return
            # 取消抵抗的业务任务没排空之前，禁止拉取/应用新内容。
            if not self._drained:
                return
            if self._stuck_tasks and any(not t.done() for t in self._stuck_tasks):
                return
            await self._standby_pull()

    # ------------------------------------------------------------ 日志
    def _note_text(self, context: str, text: str) -> None:
        message = f"HA {context}：{text}"
        self._last_error = message
        if message != self._last_error_logged:
            self._last_error_logged = message
            log.warn(message)

    def _note_error(self, context: str, exc: BaseException) -> None:
        self._note_text(context, _safe_error(exc))


async def _call_with_timeout(func, timeout: float):
    """调用可能同步/异步的 start/stop，并给异步实现加等待上限。"""
    result = func()
    if inspect.isawaitable(result):
        return await asyncio.wait_for(result, timeout=max(0.1, float(timeout)))
    return result
