"""
手写 WebSocket（RFC 6455）实现 —— 零第三方依赖。

为什么不用 websockets / aiocqhttp：
  1. aiocqhttp 依赖 C 扩展，pip 装的时候要现场编译，很容易失败；
  2. 自己写一遍才能看清 OneBot 反向 WS 到底在网线上传了什么。

只实现机器人场景够用的子集：
  - 服务端 handshake（接受 NapCat 这类客户端）
  - 客户端 handshake（给 tools/mock_qq.py 用）
  - text / binary / ping / pong / close 帧，支持分片
  - 服务端不掩码，客户端必须掩码（协议规定）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

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


async def connect(host: str, port: int, path: str = "/ws",
                  extra_headers: dict[str, str] | None = None) -> WSConnection:
    """作为客户端连接（给模拟器用）。"""
    reader, writer = await asyncio.open_connection(host, port)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for k, v in (extra_headers or {}).items():
        lines.append(f"{k}: {v}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()

    start, headers = await _read_http_head(reader)
    if "101" not in start:
        writer.close()
        raise ConnectionError(f"握手失败: {start}")
    return WSConnection(reader, writer, path, headers, is_client=True)
