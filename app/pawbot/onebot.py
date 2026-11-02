"""
OneBot v11 协议层：反向 WebSocket 服务端 / 外拨中继客户端 + 动作调用。

两种拓扑共用同一条连接语义：

反向（server，旧默认）：
┌──────────────┐   ws://127.0.0.1:6199/ws   ┌──────────────┐
│  NapCatQQ    │ ─────────────────────────► │   PawBot     │
│ (协议端)      │  X-Self-ID / X-Client-Role │ (本文件)      │
└──────────────┘ ◄───────────────────────── └──────────────┘

外拨（client，server.mode=forward）：
┌──────────────┐   ws://relay/ws            ┌──────────────┐
│   PawBot     │ ─────────────────────────► │ OneBot 中继   │
│ (本文件)      │  Authorization: Bearer …   │ (协议端)      │
└──────────────┘ ◄───────────────────────── └──────────────┘

      事件上报：{"post_type":"message", ...}
      动作调用：{"action":"send_group_msg","params":{...},"echo":"uuid"}
      动作回执：{"status":"ok","retcode":0,"data":{...},"echo":"uuid"}

关键点：反向 WS 里 NapCat 是「客户端」，我们是「服务端」——
所以端口在我们这边开，AstrBot 的 6199 也是这么来的。
forward 模式反过来：我们主动外拨，不在本地监听任何端口。
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import time
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from . import tasks, wsproto
from .log import log


class ActionError(RuntimeError):
    """动作调用失败。

    delivered=True 表示「请求已经交给协议端了，只是回执没等到」——
    消息多半已经发出去，只是我们不知道结果。上层要据此决定
    这句要不要算进「我说过的话」。
    """

    def __init__(self, msg: str, delivered: bool = False):
        super().__init__(msg)
        self.delivered = delivered


class Connection:
    """一条 OneBot 连接 = 一个 QQ 号（self_id）的通道。"""

    def __init__(self, ws: wsproto.WSConnection, server: "OneBotEndpoint"):
        self.ws = ws
        self.server = server
        self.self_id = int(ws.headers.get("x-self-id") or 0)
        self.role = ws.headers.get("x-client-role", "Universal")
        self.pending: dict[str, asyncio.Future] = {}
        self.alive_at = time.time()
        self.last_heartbeat = 0
        self.connected_at = time.time()
        self.ready = False          # 收到 lifecycle connect 才为 True
        self.nickname = ""
        # 事件队列：收包循环只管「收」，业务逻辑在 worker 里排队跑。
        # 不这么做的话，插件里一调 API 就会和收包循环互相等死——
        # 回执要由收包循环读取，而收包循环正 await 着插件。
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.worker: asyncio.Task | None = None

    @property
    def label(self) -> str:
        return f"{self.nickname or '?'}({self.self_id or '?'})"

    async def call(self, action: str, timeout: float = 20, **params) -> dict:
        """调用 OneBot API 并等待回执（靠 echo 字段配对）。

        断线 / guard 拒绝时 fail closed：直接失败，不往已关闭的连接上写，
        也不让调用方误以为消息已经发出（delivered=False）。
        """
        if self.ws.closed:
            raise ActionError(f"{action} 未发送：连接已断开", delivered=False)
        allow = getattr(self.server, "_allow_send", None)
        if allow is not None and not await allow(self):
            raise ActionError(f"{action} 未发送：transport guard 拒绝", delivered=False)

        echo = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[echo] = fut
        payload = {"action": action, "params": params, "echo": echo}
        log.debug(f"→ {action} {json.dumps(params, ensure_ascii=False)[:200]}")
        sent = False
        try:
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
            sent = True                 # 出了这一步，协议端就该收到请求了
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise ActionError(f"{action} 超时（{timeout}s），NapCat 没回执",
                              delivered=sent)
        except asyncio.CancelledError:
            # 连接断了 / 任务被取消：请求已经发出去了，只是等不到回执
            raise ActionError(f"{action} 等回执时连接中断", delivered=sent)
        except ActionError:
            raise
        except Exception as e:
            if sent:
                raise ActionError(f"{action} 等回执失败：{e}", delivered=True)
            raise
        finally:
            self.pending.pop(echo, None)

    async def send_message(self, message, *, group_id: int | None = None,
                           user_id: int | None = None,
                           auto_escape: bool = False) -> dict:
        if group_id:
            return await self.call("send_group_msg", group_id=group_id,
                                   message=message, auto_escape=auto_escape)
        return await self.call("send_private_msg", user_id=user_id,
                               message=message, auto_escape=auto_escape)

    async def typing(self, *, group_id: int | None = None,
                     user_id: int | None = None) -> None:
        """给协议端发「正在输入」。纯装饰，任何失败都吞掉。

        set_input_status 不是 OneBot v11 标准动作，NapCat 各版本支持的参数
        不完全一样；协议端不回执时 call 会一直等到超时，所以只等 3 秒，
        而且绝不让它影响真正的发送。
        """
        params = {"event_type": 1, "user_id": int(user_id or 0)}
        if group_id:
            params["group_id"] = int(group_id)
        try:
            await self.call("set_input_status", timeout=3, **params)
        except Exception as e:
            log.debug(f"set_input_status 失败（忽略）：{type(e).__name__} {e}")

    async def close(self):
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ActionError("连接已关闭"))
        self.pending.clear()
        if self.worker and not self.worker.done():
            self.worker.cancel()
        await self.ws.close()


class OneBotEndpoint:
    """反向服务端与外拨客户端共用的连接登记、事件 worker 与发送前 guard。"""

    mode = ""

    def __init__(self, on_event: Callable[[Connection, dict], Awaitable[None]] | None = None):
        self.on_event = on_event
        self.connections: dict[int, Connection] = {}
        # 未来 lease/fencing 的 transport 层前置钩子：
        #   guard(conn) -> bool | Awaitable[bool]
        # 默认 None = 不改变旧行为；返回 False 时 call() 直接失败且不发送。
        # 这里只提供接缝，不做任何 lease 判断（由上层模块负责）。
        self.send_guard: Callable[[Connection], Any] | None = None
        self._seq = 0

    async def _allow_send(self, conn: Connection) -> bool:
        """transport 层发送前置检查；默认放行。"""
        guard = self.send_guard
        if guard is None:
            return True
        try:
            verdict = guard(conn)
            if inspect.isawaitable(verdict):
                verdict = await verdict
            return bool(verdict)
        except Exception as e:
            log.warn(f"transport send guard 异常，按拒绝发送处理：{type(e).__name__} {e}")
            return False

    def get(self, self_id: int | None = None) -> Connection | None:
        alive = [c for c in self.connections.values() if not c.ws.closed]
        if self_id:
            for c in alive:
                if c.self_id == self_id:
                    return c
        if len(alive) == 1:
            return alive[0]
        return alive[0] if alive else None

    async def _dispatch(self, conn: Connection, data: dict):
        # 1) 动作回执：有 echo 且无 post_type
        if "echo" in data and "post_type" not in data:
            echo = str(data["echo"])
            fut = conn.pending.get(echo)
            if fut and not fut.done():
                if data.get("status") == "failed" or int(data.get("retcode") or 0) != 0:
                    fut.set_exception(ActionError(
                        f"retcode={data.get('retcode')} {data.get('message') or data.get('wording') or ''}",
                        delivered=False))
                else:
                    fut.set_result(data.get("data") or {})
            return

        post_type = data.get("post_type")

        # 2) 心跳 / 生命周期
        if post_type == "meta_event":
            meta = data.get("meta_event_type")
            if meta == "heartbeat":
                conn.last_heartbeat = int(data.get("time") or 0)
                conn.alive_at = time.time()
                return
            if meta == "lifecycle":
                sub = data.get("sub_type")
                log.info(f"协议端生命周期事件：{sub}")
                if sub == "connect":
                    conn.ready = True
                    if not conn.self_id and data.get("self_id"):
                        conn.self_id = int(data["self_id"])
                        self._register(conn)
                    tasks.spawn(self._probe_login(conn), name="onebot-login-probe")
                return

        # 3) 其他业务事件 —— 丢进队列，由 worker 串行处理
        if post_type:
            try:
                conn.inbox.put_nowait(data)
            except asyncio.QueueFull:
                log.warn("事件队列满了，丢弃一条（插件处理太慢？）")

    async def _worker(self, conn: Connection):
        """串行消费事件：保证同一连接内消息有序，同时不阻塞收包。"""
        while True:
            try:
                data = await conn.inbox.get()
            except asyncio.CancelledError:
                return
            if self.on_event:
                try:
                    await self.on_event(conn, data)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    log.error(f"事件处理异常：{type(e).__name__} {e}")
            conn.inbox.task_done()

    def _register(self, conn: Connection):
        """按 self_id 登记，并清掉这条连接可能占用的临时 key（连上时 self_id 还未知）。"""
        for k, v in list(self.connections.items()):
            if v is conn and k != conn.self_id:
                self.connections.pop(k, None)
        self.connections[conn.self_id] = conn

    async def _probe_login(self, conn: Connection):
        """连上后主动问一下昵称，方便日志里认人。"""
        try:
            info = await conn.call("get_login_info", timeout=8)
            conn.nickname = str(info.get("nickname") or "")
            if info.get("user_id"):
                conn.self_id = int(info["user_id"])
                self._register(conn)
            log.info(f"登录账号确认：{conn.label}")
        except Exception:
            pass


async def _close_quietly(conn: Connection) -> None:
    """关闭连接，吞掉清理阶段由任务取消引发的 CancelledError/IO 异常。

    stop() 会取消重连任务；任务可能正好停在 ``conn.close()`` 里等待
    ``wait_closed()``，此时再次 await 会拿到已被取消的 waiter。清理本身
    不应让停机失败，因此把连接标记为 closed 后继续。
    """
    try:
        await conn.close()
    except asyncio.CancelledError:
        conn.ws.closed = True
    except Exception:
        conn.ws.closed = True


class OneBotServer(OneBotEndpoint):
    """监听反向 WS 端口，把事件交给 on_event 回调。"""

    mode = "reverse"

    def __init__(self, host: str, port: int, path: str = "/ws",
                 access_token: str = "",
                 on_event: Callable[[Connection, dict], Awaitable[None]] | None = None):
        super().__init__(on_event=on_event)
        self.host = host
        self.port = port
        self.path = path
        self.access_token = access_token
        self._server: asyncio.AbstractServer | None = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info(f"OneBot 反向 WebSocket 已监听 ws://{self.host}:{self.port}{self.path}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self.connections.values()):
            await conn.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            ws = await wsproto.accept(reader, writer)
        except Exception as e:
            log.warn(f"握手失败 {peer}: {e}")
            return

        # server.access_token 留空则不校验；配置后按 OneBot 惯例接受
        # Authorization: Bearer <token> 或 ws://.../ws?access_token=<token>。
        request = urlsplit(ws.path)
        if self.access_token:
            auth = ws.headers.get("authorization", "")
            bearer = auth[7:] if auth[:7].lower() == "bearer " else ""
            query_token = parse_qs(request.query).get("access_token", [""])[0]
            expected = str(self.access_token).encode("utf-8")
            valid = any(hmac.compare_digest(candidate.encode("utf-8"), expected)
                        for candidate in (bearer, query_token) if candidate)
            if not valid:
                log.warn(f"拒绝连接 {peer}：Token 不匹配")
                await ws.close()
                return

        if self.path and self.path != "/" and request.path.rstrip("/") != self.path.rstrip("/"):
            log.warn(f"路径不匹配 {request.path}，期望 {self.path}（仍然接受）")

        conn = Connection(ws, self)
        self._seq += 1
        if conn.self_id:
            old = self.connections.get(conn.self_id)
            if old is not None and old is not conn and not old.ws.closed:
                log.warn(f"账号 {conn.self_id} 重复连接，先关掉旧的那条")
                await old.close()
        self._register(conn)
        log.info(f"协议端已连接 {peer} self_id={conn.self_id or '未知'} "
                 f"role={conn.role} path={request.path}")

        # Keep workers visible through asynchronous disconnect cleanup, even after
        # _handle removes their connection from the active connection map.
        conn.worker = tasks.spawn(self._worker(conn), name="onebot-worker")

        try:
            while True:
                text = await ws.recv()
                if text is None:
                    break
                if isinstance(text, bytes):
                    text = text.decode("utf-8", "replace")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    log.warn(f"收到非 JSON 数据：{text[:120]}")
                    continue
                conn.alive_at = time.time()
                await self._dispatch(conn, data)
        except Exception as e:
            log.error(f"连接异常 {peer}: {type(e).__name__} {e}")
        finally:
            log.info(f"协议端断开 {conn.label}")
            await conn.close()
            for k, v in list(self.connections.items()):
                if v is conn:
                    self.connections.pop(k, None)


class OneBotClient(OneBotEndpoint):
    """主动外拨 OneBot 中继（forward 模式），断线自动重连且 fail closed。

    与 OneBotServer 共用 Connection / 动作回执 / 事件 worker / 心跳与
    lifecycle 语义；区别只在于握手方向、重连循环和「重连后不重复投递
    同一条已收到事件」的去重表。lease/fencing 不在这里判断，只预留
    ``send_guard`` 钩子。
    """

    mode = "forward"

    def __init__(self, host: str, port: int, path: str = "/ws",
                 access_token: str = "",
                 on_event: Callable[[Connection, dict], Awaitable[None]] | None = None,
                 reconnect_initial: float = 1.0,
                 reconnect_max: float = 30.0,
                 connect_timeout: float = 10.0,
                 dedup_size: int = 1024):
        super().__init__(on_event=on_event)
        self.host = host
        self.port = int(port)
        self.path = path or "/ws"
        self.access_token = str(access_token or "")
        self.reconnect_initial = max(0.01, float(reconnect_initial))
        self.reconnect_max = max(self.reconnect_initial, float(reconnect_max))
        self.connect_timeout = float(connect_timeout) if connect_timeout else None
        self.dedup_size = max(1, int(dedup_size))
        self._runner: asyncio.Task | None = None
        self._stopping = False
        # 重连后中继可能重放断线前的事件；按事件身份去重，避免同一条业务
        # 事件被 on_event 处理两次。meta_event（心跳/lifecycle）不参与去重。
        self._delivered: OrderedDict[str, None] = OrderedDict()

    @classmethod
    def from_config(cls, config, *, on_event=None) -> "OneBotClient":
        """从 Config（或任何有 get(dotted, default) 的对象）构造。"""
        host = str(config.get("server.forward_host", "127.0.0.1") or "127.0.0.1")
        port = int(config.get("server.forward_port", 6199) or 6199)
        path = str(config.get("server.forward_path", "/ws") or "/ws")
        url = str(config.get("server.forward_url", "") or "").strip()
        if url:
            host, port, path = parse_forward_url(url)
        return cls(
            host=host,
            port=port,
            path=path,
            # forward 与 reverse 共用同一个 token 键：外拨时作为 Bearer 发出。
            access_token=str(config.get("server.access_token", "") or ""),
            on_event=on_event,
        )

    def _auth_headers(self) -> dict[str, str]:
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}"}

    async def start(self):
        """启动后台外拨循环；不绑定任何本地端口。"""
        if self._runner is not None and not self._runner.done():
            return
        self._stopping = False
        self._runner = tasks.spawn(self._run(), name="onebot-forward")

    async def stop(self):
        """取消后台重连/收包任务并关闭当前连接（可重复调用）。"""
        self._stopping = True
        runner, self._runner = self._runner, None
        if runner is not None and not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001 - 停机不应因清理异常中断
                log.debug(f"OneBot 中继停止时清理异常：{type(e).__name__} {e}")
        for conn in list(self.connections.values()):
            await _close_quietly(conn)
        self.connections.clear()

    async def _run(self):
        backoff = self.reconnect_initial
        while not self._stopping:
            try:
                connect = wsproto.connect(self.host, self.port, self.path,
                                          extra_headers=self._auth_headers())
                if self.connect_timeout:
                    ws = await asyncio.wait_for(connect, timeout=self.connect_timeout)
                else:
                    ws = await connect
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 连接失败要退避重试
                log.warn(f"连接 OneBot 中继失败 {self.host}:{self.port}："
                         f"{type(e).__name__} {e}；{backoff:g}s 后重试")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max)
                continue

            backoff = self.reconnect_initial
            conn = Connection(ws, self)
            if conn.self_id:
                old = self.connections.get(conn.self_id)
                if old is not None and old is not conn and not old.ws.closed:
                    log.warn(f"账号 {conn.self_id} 重复连接，先关掉旧的那条")
                    await old.close()
            self._register(conn)
            conn.worker = tasks.spawn(self._worker(conn), name="onebot-forward-worker")
            log.info(f"已外拨连接 OneBot 中继 {self.host}:{self.port}")

            try:
                await self._receive(conn)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 单条连接异常不应终止重连循环
                log.error(f"OneBot 中继连接异常：{type(e).__name__} {e}")
            finally:
                log.info(f"OneBot 中继断开 {conn.label}")
                await _close_quietly(conn)
                for k, v in list(self.connections.items()):
                    if v is conn:
                        self.connections.pop(k, None)

            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max)

    async def _receive(self, conn: Connection):
        while True:
            text = await conn.ws.recv()
            if text is None:
                return
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                log.warn(f"收到非 JSON 数据：{text[:120]}")
                continue
            conn.alive_at = time.time()
            await self._dispatch(conn, data)

    async def _dispatch(self, conn: Connection, data: dict):
        key = self._delivery_key(data)
        if key is not None:
            if key in self._delivered:
                log.debug("忽略中继重连后重复投递的事件")
                return
            self._delivered[key] = None
            while len(self._delivered) > self.dedup_size:
                self._delivered.popitem(last=False)
        await super()._dispatch(conn, data)

    @staticmethod
    def _delivery_key(data: dict) -> str | None:
        """给可去重的业务事件一个稳定身份；meta/回执返回 None（不去重）。"""
        if not isinstance(data, dict):
            return None
        if "echo" in data and "post_type" not in data:
            return None
        post_type = data.get("post_type")
        if not post_type or post_type == "meta_event":
            return None
        if post_type in ("message", "message_sent"):
            # 消息只在有 message_id 时去重：没有 id 的极端情况下宁可重复，
            # 也不要把同一秒内的两条真实消息误判成重放。
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


def parse_forward_url(url: str) -> tuple[str, int, str]:
    """把 ``ws://host:port/path`` 拆成 wsproto.connect 需要的三元组。

    只支持明文 ws://：wsproto 不做 TLS，遇到 wss:// 直接报配置错误，
    而不是悄悄降级成明文把 token 发出去。
    """
    parts = urlsplit(url)
    if parts.scheme != "ws":
        raise ValueError("server.forward_url 只支持 ws://（wsproto 不提供 TLS）")
    host = parts.hostname or ""
    if not host:
        raise ValueError("server.forward_url 缺少主机名")
    port = int(parts.port or 80)
    path = parts.path or "/ws"
    if parts.query:
        path = f"{path}?{parts.query}"
    return host, port, path
