"""高可用内容副本：在服务器中继与后端之间同步 ``content/`` 子树。

本模块只负责三件事：

* :func:`build_snapshot` / :func:`parse_snapshot` / :func:`apply_snapshot`
  —— content 树的纯快照格式、校验与安全落盘（不碰网络）；
* :class:`ContentReplica` —— 用标准库 HTTP 从 ``/ha/content`` 拉取快照
  （GET）或按 lease + CAS 推送快照（PUT）；
* :class:`ContentError` —— 带错误码的失败，消息里不出现 token、URL 或 query。

协议细节见 ``docs/HA-CONTENT-PROTOCOL.md``。这里只强调边界：

* 快照**只**包含 ``content_root`` 子树。config、sessions、日志等 ``state/``
  内容永远不进来；不跟随符号链接 / Windows 目录联接，不越过根目录。
* 路径文法跨平台一致：拒绝绝对路径、``..``、反斜杠、冒号，以及 Windows 会把
  两个不同名字映射到同一个文件的情况（结尾的点 / 空格、大小写别名）。快照
  必须能被 Windows 对端原样应用，不能只在构建端「看起来合法」。
* v1 **不删除**本地文件：pull 只覆盖快照里列出的文件，快照里没有的本地
  文件原样保留。删除需要后续显式协议。
* 这是**有界的最终一致同步**，不是零丢失热迁移。所有写入按文件原子替换，
  但没有跨文件事务，也没有事件重放 / exactly-once 承诺。

大小语义（单位唯一）
--------------------

所有 ``max_bytes`` 参数都指**解码后的内容字节**，默认 32 MiB；线上传输的是
base64 JSON，必须另有 wire 预算。:func:`wire_limit_for_decoded` 用
``max_bytes * 4 // 3 + 8 MiB`` 推导 wire 上限，中继 CLI
``scripts/onebot-relay.mjs`` 用同一公式设置默认 ``maxContentBytes``。因此
「配置支持上限」只描述解码后内容；两端不会再出现一边按解码、一边按 base64
线上字节裁剪的单位错配。

数据丢失窗口（v1）
------------------

* **推送成功但响应丢失**：中继已经写入新 revision，后端没收到 ack，本地
  revision 不前进。下一次 push 会因 CAS base 过期得到 409，必须先 pull；
  不会静默覆盖对端。
* **拉取后逐文件落盘途中崩溃**：本地可能一部分文件是新 revision、一部分
  是旧 revision，而对象里的 revision 还没提交。下一次 pull 会重新应用整个
  快照并修复（快照里有且内容不同的文件都会被重写）。
* **本机在两次 push 之间崩溃**：从中继上次成功 push 到崩溃之间的本地写入
  只在本机。若本机再也起不来，对端只有上次 push 的内容——这是默认 3 秒
  同步间隔 + 一次握手带来的有界窗口，不是零丢失。
* **网络分区**：owner 的写入只落本机，对端看到的是分区前的 revision。恢复
  后由 lease/epoch 与 CAS 决定谁覆盖谁；本模块**绝不**自动把旧本地数据
  push 回去。
* **v1 不传播删除**：对端删掉的文件，在 pull 侧仍会保留，直到后续版本引入
  显式删除协议。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import http.client
import json
import math
import os
import re
import socket
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "FORMAT",
    "CONTENT_PATH",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_WIRE_BYTES",
    "wire_limit_for_decoded",
    "ContentError",
    "ContentReplica",
    "build_snapshot",
    "parse_snapshot",
    "apply_snapshot",
]

#: 快照 JSON 的 format 字段（版本 1）
FORMAT = "pawbot-ha-content-v1"

#: 中继内容端点固定路径；``ContentReplica`` 的 base_url 缺省时补上它
CONTENT_PATH = "/ha/content"

#: 单次快照最多包含多少个文件
DEFAULT_MAX_FILES = 20_000
#: 单次快照**解码后**的总字节上限（默认 32 MiB）。
#:
#: 这是本模块与中继之间唯一的“内容大小”语义：配置值始终指解码后的内容字节，
#: 不是 base64 JSON 的线上字节。线上字节由 :func:`wire_limit_for_decoded` 推导
#: （base64 的 4/3 膨胀 + JSON 元数据开销），中继侧
#: ``scripts/onebot-relay.mjs`` 用同一公式把默认 ``maxContentBytes`` 推到 wire
#: 上限，所以一个刚好 32 MiB 的**解码后**快照可以完整 roundtrip。
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
#: base64 JSON 线上字节相对解码字节固定保留的 JSON 元数据开销。
#: 与 ``lib/onebot-relay.js`` 的 body 上限语义不同：那里数的是 wire 字节。
WIRE_OVERHEAD_BYTES = 8 * 1024 * 1024

#: 本模块自己的原子写临时文件前缀。它只排除“正在写一半的本模块临时文件”，
#: 不是按扩展名 / 文件名过滤内容；content 树里其它任何普通文件都会进快照。
_TMP_PREFIX = ".pawbot-ha-tmp-"

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
#: Windows 上会指向设备而不是普通文件的保留名
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


# ---------------------------------------------------------------- 异常


class ContentError(Exception):
    """带错误码的内容同步异常；消息里不允许出现凭据或 URL。"""

    def __init__(self, code: str, message: str = "", *, http_status: int | None = None):
        self.code = str(code)
        self.message = str(message or self.code)
        self.http_status = http_status
        super().__init__(f"{self.code}: {self.message}" if self.message != self.code else self.code)

    def as_dict(self) -> dict:
        out = {"ok": False, "code": self.code, "message": self.message}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        return out


def _http_error(what: str, status: int) -> ContentError:
    """HTTP 状态 → 稳定的错误码；消息里只有动作和状态码。"""
    status = int(status)
    if 300 <= status < 400:
        return ContentError(
            "REDIRECT",
            f"{what}被重定向（HTTP {status}）：内容端点不允许重定向",
            http_status=status,
        )
    if status == 401:
        return ContentError("AUTH_FAILED", f"{what}被拒绝（HTTP 401）：检查后端 token",
                            http_status=status)
    if status == 403:
        return ContentError("FORBIDDEN", f"{what}被拒绝（HTTP 403）：当前连接不是 lease owner",
                            http_status=status)
    if status == 404:
        return ContentError("NOT_FOUND", f"{what}失败：HTTP 404", http_status=status)
    if status == 409:
        return ContentError("CONFLICT", f"{what}冲突（HTTP 409）：base revision 已过期",
                            http_status=status)
    if 400 <= status < 500:
        return ContentError("HTTP_ERROR", f"{what}失败：HTTP {status}", http_status=status)
    if status >= 500:
        return ContentError("SERVER_ERROR", f"{what}失败：服务端错误 HTTP {status}",
                            http_status=status)
    return ContentError("HTTP_ERROR", f"{what}失败：HTTP {status}", http_status=status)


# ---------------------------------------------------------------- 小工具


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _positive_int(value, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContentError("CONFIG_INVALID", f"{name} 必须是正整数")
    return value


def wire_limit_for_decoded(max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    """解码后字节上限 → 单次快照 base64 JSON（wire）字节上限。

    **公式必须与** ``scripts/onebot-relay.mjs`` 的
    ``wireLimitForDecodedBytes`` **保持一致**：中继用同一公式把默认
    ``maxContentBytes`` 设成 wire 上限。两边都以解码后内容字节为准，避免
    「Python 允许 32 MiB，但 base64 后的 44 MiB wire 被中继按 32 MiB 拒绝」
    这种单位错配。

    4/3 覆盖 base64 膨胀，固定 8 MiB 覆盖 JSON 键名 / 路径 / sha256 / 引号
    等元数据。上限仍然有界（默认约 51 MiB wire），不会无界读进内存。
    """
    max_bytes = _positive_int(max_bytes, "max_bytes", DEFAULT_MAX_BYTES)
    return max_bytes * 4 // 3 + WIRE_OVERHEAD_BYTES


#: 默认 wire 上限（由 :data:`DEFAULT_MAX_BYTES` 按同一公式推导）
DEFAULT_MAX_WIRE_BYTES = wire_limit_for_decoded(DEFAULT_MAX_BYTES)


def _wire_limit(max_bytes: int) -> int:
    """内部旧名，等价于 :func:`wire_limit_for_decoded`（保留兼容）。"""
    return wire_limit_for_decoded(max_bytes)


def _is_link(path: str) -> bool:
    """符号链接或 Windows 目录联接（junction）。判断失败按不安全处理。"""
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction is not None and isjunction(path):
            return True
    except OSError:
        return True
    return False


def _inside(root: str, path: str) -> bool:
    """path 是否仍在 root 之下（大小写按平台归一）。"""
    norm_root = os.path.normcase(os.path.abspath(root))
    norm = os.path.normcase(os.path.abspath(path))
    return norm == norm_root or norm.startswith(norm_root + os.sep)


def _canonical_json(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# ---------------------------------------------------------------- 路径校验


def _safe_relpath(raw) -> str:
    """快照里的路径必须是 posix 相对路径，且不能借 Windows 语义逃逸。

    除了 ``..`` / 绝对路径 / 反斜杠 / 冒号之外，这里还拒绝两类「同一个文件被
    两个不同快照路径指到」的 Windows 别名：

    * 结尾的点与空格：Windows 打开 ``a.`` / ``a `` 时会被规整成 ``a``，两个
      快照路径会互相覆盖（写盘成功但内容不是你以为的那个文件）；
    * 大小写别名（``Memory.md`` vs ``memory.md``）：由 :func:`_alias_key` 在
      :func:`parse_snapshot` 里统一查重，跨平台一致——Linux 上构建的快照不能
      在 Windows 对端造成覆盖。
    """
    if not isinstance(raw, str):
        raise ContentError("BAD_SNAPSHOT", "快照文件路径必须是字符串")
    if not raw or "\x00" in raw:
        raise ContentError("BAD_SNAPSHOT", "快照文件路径为空或含 NUL")
    if "\\" in raw:
        raise ContentError("BAD_SNAPSHOT", f"快照文件路径必须是 posix 相对路径：{raw!r}")
    if ":" in raw:
        # Windows 盘符与 NTFS 数据流（ADS）都靠冒号，一律拒绝
        raise ContentError("BAD_SNAPSHOT", f"快照文件路径不能含冒号：{raw!r}")
    if raw.startswith("/") or _DRIVE_RE.match(raw):
        raise ContentError("BAD_SNAPSHOT", f"快照文件路径不能是绝对路径：{raw!r}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ContentError("BAD_SNAPSHOT", f"快照文件路径不合法：{raw!r}")
    for part in parts:
        if part != part.rstrip(" ."):
            raise ContentError(
                "BAD_SNAPSHOT", f"快照文件路径的每一段都不能以点或空格结尾：{raw!r}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in part):
            raise ContentError("BAD_SNAPSHOT", f"快照文件路径含控制字符：{raw!r}")
        if part.startswith(_TMP_PREFIX):
            raise ContentError("BAD_SNAPSHOT", f"快照文件路径使用了本模块的临时文件前缀：{raw!r}")
        if part.split(".")[0].strip().lower() in _RESERVED_NAMES:
            raise ContentError("BAD_SNAPSHOT", f"快照文件路径使用了系统保留名：{raw!r}")
    return "/".join(parts)


def _alias_key(relpath: str) -> str:
    """路径的「同一个文件」判等键，跨平台一致（不依赖宿主 os.path.normcase）。

    Windows 文件名大小写不敏感，且结尾的点 / 空格会被剥掉；快照最终会落到
    Windows 对端，所以查重必须按这个语义做，否则 Linux 上通过校验的快照
    会在 Windows 上把 ``Memory.md`` 与 ``memory.md`` 写进同一个文件。

    这里用 ``casefold()``：比 NTFS 的大小写表略严格（例如 ß 与 ss 会判成别名），
    倾向是「宁可拒绝一个可疑路径，也不静默覆盖另一个文件」。
    """
    return "/".join(part.rstrip(" .").casefold() for part in relpath.split("/"))


def _prepare_root(content_root) -> tuple[str, str]:
    """返回 (调用方给的绝对路径, realpath 后的根)。根不存在就创建。"""
    raw = str(content_root or "").strip()
    if not raw:
        raise ContentError("CONFIG_INVALID", "content_root 不能为空")
    if "\x00" in raw:
        raise ContentError("CONFIG_INVALID", "content_root 含非法字符")
    raw = os.path.abspath(os.path.expanduser(raw))
    if os.path.exists(raw) and not os.path.isdir(raw):
        raise ContentError("CONFIG_INVALID", "content_root 不是目录")
    try:
        os.makedirs(raw, exist_ok=True)
    except OSError as exc:
        raise ContentError("IO_ERROR", f"建 content_root 失败：{type(exc).__name__}") from None
    root = os.path.realpath(raw)
    if not os.path.isdir(root):
        raise ContentError("IO_ERROR", "content_root 不是目录")
    return raw, root


def _target_path(root: str, rel: str) -> str:
    """把快照相对路径解析成绝对路径；任何一层是链接就拒绝，最后再查越界。"""
    parts = rel.split("/")
    current = root
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        if os.path.lexists(current):
            if _is_link(current):
                raise ContentError(
                    "UNSAFE_PATH", f"目标路径经过符号链接 / 目录联接：{rel}")
            if index < len(parts) - 1:
                if not os.path.isdir(current):
                    raise ContentError("UNSAFE_PATH", f"目标路径被非目录占用：{rel}")
            elif os.path.isdir(current):
                raise ContentError("UNSAFE_PATH", f"目标路径是目录：{rel}")
    resolved = os.path.realpath(current)
    if not _inside(root, resolved):
        raise ContentError("UNSAFE_PATH", f"目标路径越界：{rel}")
    return current


def _ensure_parent(root: str, rel: str) -> None:
    """逐级创建父目录；每一级都拒绝链接与非目录。"""
    current = root
    for part in rel.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current):
            if _is_link(current) or not os.path.isdir(current):
                raise ContentError("UNSAFE_PATH", f"目标路径经过符号链接 / 目录联接：{rel}")
            continue
        try:
            os.mkdir(current)
        except FileExistsError:
            if _is_link(current) or not os.path.isdir(current):
                raise ContentError("UNSAFE_PATH", f"目标路径经过符号链接 / 目录联接：{rel}")
        except OSError as exc:
            raise ContentError("IO_ERROR", f"建目录失败：{type(exc).__name__}") from None


def _same_bytes(path: str, data: bytes) -> bool:
    try:
        if _is_link(path) or not os.path.isfile(path):
            return False
        if os.path.getsize(path) != len(data):
            return False
        with open(path, "rb") as handle:
            return handle.read(len(data) + 1) == data
    except OSError:
        return False


def _atomic_write(path: str, data: bytes) -> None:
    """同目录临时文件 + fsync + os.replace，单文件原子替换。"""
    parent = os.path.dirname(path) or "."
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        if _is_link(path):
            raise ContentError("UNSAFE_PATH", "目标文件在写入前变成了符号链接")
        os.replace(tmp, path)
        tmp = ""
    except ContentError:
        raise
    except OSError as exc:
        raise ContentError("IO_ERROR", f"写文件失败：{type(exc).__name__}") from None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------- 快照


def build_snapshot(content_root, *, max_files: int = DEFAULT_MAX_FILES,
                   max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """把 ``content_root`` 子树打成快照 JSON（UTF-8 bytes）。

    * 只收普通文件；遇到符号链接 / 目录联接 / 特殊文件直接拒绝（fail closed）；
    * 不跟随链接、不越过根目录，不按扩展名或文件名过滤内容；
    * 文件按 posix 相对路径排序，输出是确定性的（同样内容 → 同样 bytes）；
    * 根目录不存在时返回合法的空快照（``files: []``），不创建目录。
    """
    max_files = _positive_int(max_files, "max_files", DEFAULT_MAX_FILES)
    max_bytes = _positive_int(max_bytes, "max_bytes", DEFAULT_MAX_BYTES)

    raw = os.path.abspath(os.path.expanduser(str(content_root or "").strip()))
    if not raw or not str(content_root or "").strip():
        raise ContentError("CONFIG_INVALID", "content_root 不能为空")
    if not os.path.exists(raw):
        return _canonical_json({"format": FORMAT, "files": []})
    if not os.path.isdir(raw):
        raise ContentError("CONFIG_INVALID", "content_root 不是目录")
    root = os.path.realpath(raw)

    collected: list[tuple[str, str]] = []
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ContentError("IO_ERROR", f"读 content 目录失败：{type(exc).__name__}") from None
        for entry in entries:
            rel = f"{prefix}/{entry.name}" if prefix else entry.name
            path = entry.path
            if _is_link(path):
                raise ContentError(
                    "UNSAFE_PATH", f"content 树里有符号链接 / 目录联接：{rel}")
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                raise ContentError("IO_ERROR", f"读 content 条目失败：{type(exc).__name__}") from None
            if is_dir:
                stack.append((path, rel))
            elif is_file:
                # 只排除本模块正在写一半的临时文件，不是内容过滤
                if entry.name.startswith(_TMP_PREFIX):
                    continue
                # 构建侧就用同一套路径文法校验：本机（如 Linux）能建、但对端
                # （Windows）无法安全落盘的路径（结尾点/空格、保留名、反斜杠…）
                # 必须在这里失败，而不是产出一个谁都无法应用的快照。
                _safe_relpath(rel)
                collected.append((rel, path))
            else:
                raise ContentError("UNSAFE_PATH", f"content 树里有非普通文件：{rel}")

    if len(collected) > max_files:
        raise ContentError("TOO_MANY_FILES", f"content 文件数超过上限（{max_files}）")
    collected.sort(key=lambda item: item[0])

    files: list[dict] = []
    total = 0
    for rel, path in collected:
        remaining = max_bytes - total
        if remaining <= 0:
            raise ContentError("TOO_LARGE", f"content 总大小超过上限（{max_bytes} 字节）")
        try:
            with open(path, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ContentError("UNSAFE_PATH", f"content 条目不是普通文件：{rel}")
                if info.st_size > remaining:
                    raise ContentError("TOO_LARGE", f"content 总大小超过上限（{max_bytes} 字节）")
                data = handle.read(remaining + 1)
        except ContentError:
            raise
        except OSError as exc:
            raise ContentError("IO_ERROR", f"读文件失败：{type(exc).__name__}") from None
        if len(data) > remaining:
            raise ContentError("TOO_LARGE", f"content 总大小超过上限（{max_bytes} 字节）")
        total += len(data)
        files.append({
            "path": rel,
            "data": base64.b64encode(data).decode("ascii"),
            "sha256": _sha256(data),
        })

    return _canonical_json({"format": FORMAT, "files": files})


def parse_snapshot(blob, *, max_files: int = DEFAULT_MAX_FILES,
                   max_bytes: int = DEFAULT_MAX_BYTES,
                   max_wire_bytes: int | None = None) -> list[dict]:
    """完整校验快照，返回 ``[{"path", "data"(bytes), "sha256"}]``。

    **整个快照先校验完再返回**：格式、路径、重复 / 文件目录冲突、base64、
    sha256、条目数与总字节全部通过，调用方才可以开始写盘。任何一项不合法都
    抛 :class:`ContentError`，不会返回半份结果。
    """
    max_files = _positive_int(max_files, "max_files", DEFAULT_MAX_FILES)
    max_bytes = _positive_int(max_bytes, "max_bytes", DEFAULT_MAX_BYTES)
    wire_limit = (wire_limit_for_decoded(max_bytes) if max_wire_bytes is None
                  else _positive_int(max_wire_bytes, "max_wire_bytes", 1))

    if not isinstance(blob, (bytes, bytearray)):
        raise ContentError("BAD_SNAPSHOT", "快照必须是 bytes")
    blob = bytes(blob)
    if len(blob) > wire_limit:
        raise ContentError("TOO_LARGE", f"快照线上大小超过上限（{wire_limit} 字节）")
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ContentError("BAD_SNAPSHOT", "快照不是合法 UTF-8 JSON") from None
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ContentError("BAD_SNAPSHOT", f"快照 format 不是 {FORMAT}")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise ContentError("BAD_SNAPSHOT", "快照缺少 files 数组")
    if len(raw_files) > max_files:
        raise ContentError("TOO_MANY_FILES", f"快照文件数超过上限（{max_files}）")

    entries: list[dict] = []
    seen: set[str] = set()
    total = 0
    for item in raw_files:
        if not isinstance(item, dict):
            raise ContentError("BAD_SNAPSHOT", "快照 files 条目必须是对象")
        path = _safe_relpath(item.get("path"))
        # 用跨平台的别名键查重：大小写 / 结尾点空格在 Windows 上会落到同一个
        # 文件，不能因为 Linux 对端大小写敏感就放过。
        key = _alias_key(path)
        if key in seen:
            raise ContentError("BAD_SNAPSHOT", f"快照里有重复路径（大小写/别名冲突）：{path}")
        seen.add(key)

        digest = item.get("sha256")
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise ContentError("BAD_SNAPSHOT", f"快照 sha256 不是 64 位十六进制：{path}")
        encoded = item.get("data")
        if not isinstance(encoded, str):
            raise ContentError("BAD_SNAPSHOT", f"快照 data 不是 base64 字符串：{path}")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ContentError("BAD_SNAPSHOT", f"快照 data 不是合法 base64：{path}") from None
        if len(data) > max_bytes - total:
            raise ContentError("TOO_LARGE", f"快照解码后总大小超过上限（{max_bytes} 字节）")
        total += len(data)
        if _sha256(data) != digest.lower():
            raise ContentError("BAD_SNAPSHOT", f"快照 sha256 对不上：{path}")
        entries.append({"path": path, "data": data, "sha256": digest.lower()})

    # 文件与目录不能同名：路径 A 不能同时是路径 A/B 的前缀（按跨平台别名键）
    for entry in entries:
        parts = entry["path"].split("/")
        for index in range(1, len(parts)):
            prefix = _alias_key("/".join(parts[:index]))
            if prefix in seen:
                raise ContentError(
                    "BAD_SNAPSHOT", f"快照里文件与目录冲突：{entry['path']}")
    return entries


def apply_snapshot(content_root, blob, *, max_files: int = DEFAULT_MAX_FILES,
                   max_bytes: int = DEFAULT_MAX_BYTES,
                   max_wire_bytes: int | None = None) -> dict:
    """校验并把快照落盘到 ``content_root``。

    顺序是：**先整体校验**（:func:`parse_snapshot`），**再整体预检**每个目标
    路径（越界 / 链接 / 目录冲突），全部通过后才开始逐文件写。v1 不删除任何
    文件；已存在且内容相同的文件不重写。

    返回 ``{"ok", "files", "written", "unchanged", "bytes"}``。
    """
    entries = parse_snapshot(
        blob, max_files=max_files, max_bytes=max_bytes, max_wire_bytes=max_wire_bytes)
    _raw, root = _prepare_root(content_root)

    plans: list[tuple[dict, str]] = []
    for entry in entries:
        plans.append((entry, _target_path(root, entry["path"])))

    written: list[str] = []
    unchanged: list[str] = []
    for entry, target in plans:
        _ensure_parent(root, entry["path"])
        target = _target_path(root, entry["path"])  # 建目录后再确认一次
        if _same_bytes(target, entry["data"]):
            unchanged.append(entry["path"])
            continue
        _atomic_write(target, entry["data"])
        written.append(entry["path"])

    return {
        "ok": True,
        "files": len(entries),
        "written": written,
        "unchanged": unchanged,
        "bytes": sum(len(entry["data"]) for entry in entries),
    }


# ---------------------------------------------------------------- HTTP 客户端


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """内容端点不允许重定向：拒绝 301/302/303/307/308，避免把 Bearer 带去别处。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _check_base_url(url) -> str:
    """校验 relay 地址并补成完整内容端点。

    允许两种写法（``relay_client.normalize_content_url`` 会传第一种）：

    * relay 根 / 反向代理前缀：``https://relay:6198``、``https://relay/relay``
      → 补成 ``…/ha/content``；
    * 完整端点：``https://relay:6198/ha/content``（结尾斜杠也接受）。

    只接受 http(s)，拒绝 userinfo / query / fragment；返回值是实际请求的端点。
    """
    text = str(url or "").strip()
    if not text:
        raise ContentError("CONFIG_INVALID", "内容端点 URL 为空")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise ContentError("CONFIG_INVALID", "内容端点 URL 含控制字符")
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port  # 端口非法时这里会抛 ValueError
    except ValueError:
        raise ContentError("CONFIG_INVALID", "内容端点 URL 不合法") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ContentError("CONFIG_INVALID", "内容端点必须是 http(s)://")
    if parts.username is not None or parts.password is not None:
        raise ContentError("CONFIG_INVALID", "内容端点 URL 不允许 userinfo（user:pass@）")
    if parts.query or parts.fragment:
        raise ContentError("CONFIG_INVALID", "内容端点 URL 不允许 query / fragment（token 不能放 URL）")
    if not parts.hostname:
        raise ContentError("CONFIG_INVALID", "内容端点 URL 缺少主机名")
    path = parts.path.rstrip("/")
    if not path.endswith(CONTENT_PATH):
        path = f"{path}{CONTENT_PATH}"
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))


def _check_token(token) -> str:
    if token is None:
        return ""
    if not isinstance(token, str):
        raise ContentError("CONFIG_INVALID", "token 必须是字符串")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in token):
        raise ContentError("CONFIG_INVALID", "token 含控制字符")
    return token


def _lease_headers(lease) -> dict:
    if not isinstance(lease, dict):
        raise ContentError("LEASE_INVALID", "lease 必须是对象")
    headers: dict[str, str] = {}
    for key, header in (("lease_id", "X-Relay-Lease"), ("epoch", "X-Relay-Epoch")):
        value = lease.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ContentError("LEASE_INVALID", f"lease 缺少 {key}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ContentError("LEASE_INVALID", f"lease.{key} 含控制字符")
        headers[header] = value
    revision = lease.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ContentError("LEASE_INVALID", "lease.revision 必须是非负整数")
    headers["X-Relay-Revision"] = str(revision)
    return headers


def _revision_header(headers) -> int:
    raw = headers.get("X-Content-Revision")
    if raw is None:
        raise ContentError("BAD_RESPONSE", "内容响应缺少 X-Content-Revision")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ContentError("BAD_RESPONSE", "X-Content-Revision 不是整数") from None
    if value < 0:
        raise ContentError("BAD_RESPONSE", "X-Content-Revision 不能为负")
    return value


def _read_response(response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(65536, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ContentError("TOO_LARGE", "内容响应超过快照大小上限")
        chunks.append(chunk)
    return b"".join(chunks)


class ContentReplica:
    """一个后端的内容副本：GET 拉取、PUT 按 lease + CAS 推送。

    :param content_root: 本机 ``content/`` 目录（只读写这棵子树）
    :param base_url: relay 根地址（如 ``http://127.0.0.1:6198``）或完整内容端点
        （``…/ha/content``）；缺省时自动补 ``/ha/content``，只接受 http(s)，
        拒绝 userinfo / query / fragment
    :param token: 后端角色 Bearer token（空字符串表示不带 Authorization）
    :param timeout: 单次 HTTP 超时（秒）
    :param max_files / max_bytes: 快照条目数与**解码后**总字节上限
    :param max_wire_bytes: 响应体读取上限（wire / base64 JSON 字节）；默认按
        ``max_bytes`` 用 :func:`wire_limit_for_decoded` 推导

    实例属性 ``revision``（别名 ``content_revision``）保存最近一次成功 pull
    或 push 得到的 canonical content revision；push 用它作为 CAS base。

    线程安全：pull / push 在实例内串行（同一对象不会并发读改写 revision）。
    多个实例指向同一 ``content_root`` 时由调用方自行串行化。
    """

    def __init__(self, content_root, base_url, token: str = "", timeout: float = 10.0,
                 *, max_files: int = DEFAULT_MAX_FILES,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 max_wire_bytes: int | None = None):
        root = str(content_root or "").strip()
        if not root:
            raise ContentError("CONFIG_INVALID", "content_root 不能为空")
        if "\x00" in root:
            raise ContentError("CONFIG_INVALID", "content_root 含非法字符")
        self.content_root = os.path.abspath(os.path.expanduser(root))
        self.base_url = _check_base_url(base_url)
        self._token = _check_token(token)
        try:
            self._timeout = float(timeout)
        except (TypeError, ValueError):
            raise ContentError("CONFIG_INVALID", "timeout 必须是正数") from None
        if not math.isfinite(self._timeout) or self._timeout <= 0:
            raise ContentError("CONFIG_INVALID", "timeout 必须是正数")
        self._max_files = _positive_int(max_files, "max_files", DEFAULT_MAX_FILES)
        self._max_bytes = _positive_int(max_bytes, "max_bytes", DEFAULT_MAX_BYTES)
        self._max_wire = (wire_limit_for_decoded(self._max_bytes) if max_wire_bytes is None
                          else _positive_int(max_wire_bytes, "max_wire_bytes", 1))
        self.revision = 0
        self._lock = threading.RLock()
        # 显式关掉环境代理，并禁止任何重定向：Authorization 只发给这个端点
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    # ------------------------------------------------------------ 公共状态
    @property
    def content_revision(self) -> int:
        """最近一次成功 pull / push 得到的 content revision。"""
        return self.revision

    def close(self) -> None:
        with self._lock:
            try:
                self._opener.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响调用方
                pass

    # ------------------------------------------------------------ 请求
    def _base_headers(self) -> dict:
        headers = {"User-Agent": "PawBot-HA/1", "Accept": "application/octet-stream"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(self, method: str, *, data: bytes | None = None,
                 headers: dict | None = None, what: str):
        request = urllib.request.Request(
            self.base_url, data=data, headers=headers or {}, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = int(response.status)
                meta = response.headers
                body = _read_response(response, self._max_wire)
                return status, meta, body
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
            raise _http_error(what, status) from None
        except ContentError:
            raise
        except (TimeoutError, socket.timeout):
            raise ContentError("TIMEOUT", f"{what}超时（{self._timeout:g} 秒）") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise ContentError("TIMEOUT", f"{what}超时（{self._timeout:g} 秒）") from None
            raise ContentError(
                "NETWORK", f"{what}失败：连不上内容端点（{type(reason).__name__}）") from None
        except http.client.HTTPException as exc:
            raise ContentError(
                "NETWORK", f"{what}失败：连接异常（{type(exc).__name__}）") from None
        except OSError as exc:
            raise ContentError("NETWORK", f"{what}失败：{type(exc).__name__}") from None

    # ------------------------------------------------------------ pull
    def pull(self) -> dict:
        """拉取并应用中继上的 canonical 快照。

        成功返回 ``{"ok": True, "content_revision": int, "changed": bool}``；
        204 表示中继上还没有内容（revision 0），不改动本地文件。失败抛
        :class:`ContentError`，且不会提交新的本地 revision。
        """
        with self._lock:
            status, headers, body = self._request(
                "GET", headers=self._base_headers(), what="拉取内容")
            if status == 204:
                self.revision = 0
                return {"ok": True, "content_revision": 0, "changed": False}
            if status != 200:
                raise _http_error("拉取内容", status)

            revision = _revision_header(headers)
            digest = str(headers.get("X-Content-SHA256") or "").strip().lower()
            if not _HEX64.fullmatch(digest):
                raise ContentError("BAD_RESPONSE", "内容响应缺少合法的 X-Content-SHA256")
            if _sha256(body) != digest:
                raise ContentError("BAD_RESPONSE", "内容响应 sha256 校验失败")

            result = apply_snapshot(
                self.content_root, body,
                max_files=self._max_files, max_bytes=self._max_bytes,
                max_wire_bytes=self._max_wire,
            )
            previous = self.revision
            self.revision = revision
            changed = bool(result["written"]) or revision != previous
            return {"ok": True, "content_revision": revision, "changed": changed}

    # ------------------------------------------------------------ push
    def push(self, lease: dict) -> dict:
        """按当前 lease 把本机快照 CAS 推给中继。

        ``lease`` 必须含 ``lease_id`` / ``epoch`` / ``revision``；base 使用本
        对象最近一次 pull / push 得到的 revision。409（base 过期 / 不是 owner）
        会抛 ``CONFLICT``，**不会**自动重试或把旧本地数据再推一次。
        """
        with self._lock:
            lease_headers = _lease_headers(lease)
            blob = build_snapshot(
                self.content_root, max_files=self._max_files, max_bytes=self._max_bytes)
            base = self.revision
            headers = self._base_headers()
            headers["Content-Type"] = "application/octet-stream"
            headers.update(lease_headers)
            headers["X-Content-Base-Revision"] = str(base)

            status, _resp_headers, body = self._request(
                "PUT", data=blob, headers=headers, what="推送内容")
            if status != 200:
                raise _http_error("推送内容", status)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ContentError("BAD_RESPONSE", "推送响应不是合法 JSON") from None
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ContentError("BAD_RESPONSE", "推送响应没有 ok:true")
            revision = payload.get("content_revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise ContentError("BAD_RESPONSE", "推送响应 content_revision 不合法")
            digest = payload.get("sha256")
            if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
                raise ContentError("BAD_RESPONSE", "推送响应缺少合法 sha256")
            if digest.lower() != _sha256(blob):
                raise ContentError("BAD_RESPONSE", "推送响应 sha256 与发送内容不一致")

            self.revision = revision
            return {"ok": True, "content_revision": revision, "changed": revision != base}

    # ------------------------------------------------------------ 异步适配
    async def pull_async(self) -> dict:
        """:meth:`pull` 的 asyncio 适配（在线程里跑阻塞 HTTP）。"""
        return await asyncio.to_thread(self.pull)

    async def push_async(self, lease: dict) -> dict:
        """:meth:`push` 的 asyncio 适配（在线程里跑阻塞 HTTP）。"""
        return await asyncio.to_thread(self.push, lease)
