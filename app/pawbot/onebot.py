"""
OneBot v11 协议层：反向 WebSocket 服务端 + 动作调用。

┌──────────────┐   ws://127.0.0.1:6199/ws   ┌──────────────┐
│  NapCatQQ    │ ─────────────────────────► │   PawBot     │
│ (协议端)      │  X-Self-ID / X-Client-Role │ (本文件)      │
└──────────────┘ ◄───────────────────────── └──────────────┘
      事件上报：{"post_type":"message", ...}
      动作调用：{"action":"send_group_msg","params":{...},"echo":"uuid"}
      动作回执：{"status":"ok","retcode":0,"data":{...},"echo":"uuid"}

关键点：反向 WS 里 NapCat 是「客户端」，我们是「服务端」——
所以端口在我们这边开，AstrBot 的 6199 也是这么来的。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from . import wsproto
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

    def __init__(self, ws: wsproto.WSConnection, server: "OneBotServer"):
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
        """调用 OneBot API 并等待回执（靠 echo 字段配对）。"""
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


class OneBotServer:
    """监听反向 WS 端口，把事件交给 on_event 回调。"""

    def __init__(self, host: str, port: int, path: str = "/ws",
                 access_token: str = "",
                 on_event: Callable[[Connection, dict], Awaitable[None]] | None = None):
        self.host = host
        self.port = port
        self.path = path
        self.access_token = access_token
        self.on_event = on_event
        self.connections: dict[int, Connection] = {}
        self._server: asyncio.AbstractServer | None = None
        self._seq = 0

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info(f"OneBot 反向 WebSocket 已监听 ws://{self.host}:{self.port}{self.path}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self.connections.values()):
            await conn.close()

    def get(self, self_id: int | None = None) -> Connection | None:
        alive = [c for c in self.connections.values() if not c.ws.closed]
        if self_id:
            for c in alive:
                if c.self_id == self_id:
                    return c
        if len(alive) == 1:
            return alive[0]
        return alive[0] if alive else None

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

        conn.worker = asyncio.create_task(self._worker(conn))

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
                    asyncio.create_task(self._probe_login(conn))
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
