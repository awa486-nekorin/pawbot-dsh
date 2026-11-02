"""
手写 WebSocket（RFC 6455）实现 —— 零第三方依赖。

为什么不用 websockets / aiocqhttp：
  1. aiocqhttp 依赖 C 扩展，pip 装的时候要现场编译，很容易失败；
  2. 自己写一遍才能看清 OneBot 反向 WS 到底在网线上传了什么。

只实现机器人场景够用的子集：
  - 服务端 handshake（接受 NapCat 这类客户端）
  - 客户端 handshake（给 tools/mock_qq.py、forward/relay 后端用，支持 wss）
  - text / binary / ping / pong / close 帧，支持分片
  - 服务端不掩码，客户端必须掩码（协议规定）

本模块只负责单条连接的帧收发与握手校验，不提供重连、事件重放或
exactly-once 语义；relay 模式的崩溃/分区数据丢失窗口见
``pawbot.relay_client`` 模块注释。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import os
import re
import ssl
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# RFC 6455 的 header name token 子集；比这更宽的名字一律拒绝，避免 CRLF/冒号注入。
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_RESERVED_CLIENT_HEADERS = frozenset({
    "host", "upgrade", "connection", "sec-websocket-key",
    "sec-websocket-version",
})

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 32 * 1024 * 1024  # 32MB，够传图片 base64 了


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept = base64(sha1(key + GUID))"""
    digest = hashlib.sha1((key + GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def _build_frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    """组装一帧。这是整个 WebSocket 协议唯一的二进制格式。"""
    header = bytearray()
    header.append(0x80 | opcode)          # FIN=1，无扩展位
    length = len(payload)
    flag = 0x80 if mask else 0x00         # 掩码位：客户端必须置 1

    if length < 126:
        header.append(flag | length)
    elif length < 65536:
        header.append(flag | 126)
        header += struct.pack("!H", length)
    else:
        header.append(flag | 127)
        header += struct.pack("!Q", length)

    if mask:
        key = os.urandom(4)
        header += key
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return bytes(header) + masked
    return bytes(header) + payload


class WSConnection:
    """一条已建立的 WebSocket 连接。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 path: str, headers: dict[str, str], is_client: bool = False):
        self.reader = reader
        self.writer = writer
        self.path = path
        self.headers = headers
        self.is_client = is_client          # 客户端发的帧要掩码
        self.closed = False

    # ---------------- 收 ----------------
    async def _read_frame(self):
        head = await self.reader.readexactly(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F

        if length == 126:
            length = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self.reader.readexactly(8))[0]

        if length > MAX_FRAME:
            raise ValueError(f"帧过大: {length}")

        mask_key = await self.reader.readexactly(4) if masked else None
        payload = await self.reader.readexactly(length) if length else b""

        if mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    async def recv(self) -> str | bytes | None:
        """收一条完整消息。返回 None 表示对端关闭。"""
        chunks: list[bytes] = []
        msg_opcode: int | None = None

        while True:
            try:
                fin, opcode, payload = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionResetError):
                self.closed = True
                return None

            if opcode == OP_PING:
                await self._write(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                await self._write(OP_CLOSE, b"")
                self.closed = True
                return None

            if opcode == OP_CONT:
                chunks.append(payload)
            else:
                msg_opcode = opcode
                chunks = [payload]

            if fin:
                data = b"".join(chunks)
                if msg_opcode == OP_BIN:
                    return data
                return data.decode("utf-8", "replace")

    # ---------------- 发 ----------------
    async def _write(self, opcode: int, payload: bytes):
        if self.closed:
            return
        try:
            self.writer.write(_build_frame(opcode, payload, mask=self.is_client))
            await self.writer.drain()
        except (ConnectionResetError, RuntimeError):
            self.closed = True

    async def send_text(self, text: str):
        await self._write(OP_TEXT, text.encode("utf-8"))

    async def ping(self):
        await self._write(OP_PING, b"")

    async def close(self):
        if not self.closed:
            await self._write(OP_CLOSE, b"")
        self.closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


async def _read_http_head(reader: asyncio.StreamReader):
    """读 HTTP 请求/响应头，返回 (起始行, headers, 残留字节)。"""
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    start = lines[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    return start, headers


async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> WSConnection:
    """作为服务端完成握手。"""
    start, headers = await _read_http_head(reader)
    parts = start.split()
    path = parts[1] if len(parts) > 1 else "/"

    if headers.get("upgrade", "").lower() != "websocket":
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        raise ValueError("不是 WebSocket 握手")

    key = headers.get("sec-websocket-key", "")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
        "\r\n"
    )
    writer.write(resp.encode("latin-1"))
    await writer.drain()
    return WSConnection(reader, writer, path, headers, is_client=False)


def default_client_ssl_context() -> ssl.SSLContext:
    """wss:// 默认使用「校验证书 + 校验主机名」的上下文，绝不静默降级。

    与 ``ssl.create_default_context()`` 等价，但单独抽出来是为了让调用方
    （以及测试）能明确看到默认安全策略，而不是散落在 connect 内部。
    """
    return ssl.create_default_context()


def _validate_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("WebSocket 路径不能为空")
    if not path.startswith("/"):
        raise ValueError("WebSocket 路径必须以 / 开头")
    for ch in path:
        if ch == " " or ch in "\r\n" or ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError("WebSocket 路径包含非法控制字符")
    return path


def _validate_host(host: str) -> str:
    if not isinstance(host, str) or not host:
        raise ValueError("WebSocket 主机名不能为空")
    for ch in host:
        if ch in "\r\n \t/" or ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError("WebSocket 主机名包含非法字符")
    return host


def _validate_extra_headers(extra_headers: dict[str, str] | None) -> dict[str, str]:
    """校验调用方追加的头，拒绝 CRLF/控制字符与保留头。

    错误信息里只写字段名/原因，不回显 header 值——调用方可能把 token
    放在 Authorization 里，异常文本不能把它带进日志。
    """
    checked: dict[str, str] = {}
    for raw_name, raw_value in (extra_headers or {}).items():
        name = str(raw_name)
        value = str(raw_value)
        if not _HEADER_NAME_RE.match(name):
            raise ValueError("WebSocket 头名称非法")
        if name.lower() in _RESERVED_CLIENT_HEADERS:
            raise ValueError(f"WebSocket 头 {name} 由协议层保留")
        if "\r" in value or "\n" in value or "\x00" in value:
            raise ValueError(f"WebSocket 头 {name} 的值包含非法控制字符")
        checked[name] = value
    return checked


def _host_header(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


def _validate_handshake(start: str, headers: dict[str, str], key: str) -> None:
    """校验服务端 101 响应；任何一项不对都 fail closed。"""
    parts = start.split()
    status = parts[1] if len(parts) > 1 else ""
    if len(parts) < 2 or parts[0] not in ("HTTP/1.1", "HTTP/1.0") or status != "101":
        raise ConnectionError(f"WebSocket 握手失败：HTTP 状态 {status or '未知'}")
    if headers.get("upgrade", "").strip().lower() != "websocket":
        raise ConnectionError("WebSocket 握手失败：Upgrade 头不正确")
    tokens = [token.strip().lower()
              for token in headers.get("connection", "").split(",")]
    if "upgrade" not in tokens:
        raise ConnectionError("WebSocket 握手失败：Connection 头不正确")
    accept = headers.get("sec-websocket-accept", "").strip()
    try:
        accepted = bool(accept) and hmac.compare_digest(accept, accept_key(key))
    except TypeError:
        accepted = False
    if not accepted:
        raise ConnectionError("WebSocket 握手失败：Sec-WebSocket-Accept 不匹配")


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    """握手失败/取消时一定关掉 socket，且不让清理异常盖住原始错误。"""
    if writer is None:
        return
    with contextlib.suppress(Exception, asyncio.CancelledError):
        writer.close()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await writer.wait_closed()


async def connect(host: str, port: int, path: str = "/ws",
                  extra_headers: dict[str, str] | None = None,
                  *, secure: bool = False,
                  ssl_context: ssl.SSLContext | None = None,
                  handshake_timeout: float = 15.0) -> WSConnection:
    """作为客户端连接（模拟器 / forward / relay backend 共用）。

    ``secure=True`` 时走 TLS（wss://），默认使用校验证书与主机名的
    ``default_client_ssl_context()``；调用方可以显式传入自己的上下文。
    握手失败、超时或被取消时都会关闭底层 socket，不会把带 token 的
    header 写进异常文本。
    """
    host = _validate_host(str(host))
    path = _validate_path(str(path))
    headers = _validate_extra_headers(extra_headers)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("WebSocket 端口非法") from None
    if not 0 < port < 65536:
        raise ValueError("WebSocket 端口超出范围")

    connect_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    context = None
    if secure:
        context = ssl_context if ssl_context is not None else default_client_ssl_context()

    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {_host_header(host, port)}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for name, value in headers.items():
        lines.append(f"{name}: {value}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_connection(
            connect_host, port, ssl=context,
            server_hostname=connect_host if context is not None else None)
        writer.write(request)
        await writer.drain()
        try:
            start, response_headers = await asyncio.wait_for(
                _read_http_head(reader), timeout=handshake_timeout)
        except asyncio.TimeoutError:
            raise ConnectionError("WebSocket 握手失败：等待响应超时") from None
        except asyncio.IncompleteReadError:
            raise ConnectionError("WebSocket 握手失败：连接被提前关闭") from None
        _validate_handshake(start, response_headers, key)
        return WSConnection(reader, writer, path, response_headers, is_client=True)
    except asyncio.CancelledError:
        await _close_writer(writer)
        raise
    except Exception:
        await _close_writer(writer)
        raise
