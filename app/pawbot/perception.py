"""感知事实缓存：按图片**原始字节**缓存「这张图是什么」。

为什么单独一层
--------------
``vision.py`` 以前按 ``segment.data``（URL / 路径）缓存，URL 一变就重新花钱；
失败结果（``"[图片]"``）也会被永久写成成功缓存。这一层把身份改成图片
原始字节的 SHA-256，并给缓存加上边界、TTL、single-flight 和可选的磁盘持久化。

约束
----
* 缓存键只包含视觉识别所需参数：图片字节摘要、模型、base_url、推理强度、
  提示词版本/指纹、看图模式、命名空间；**绝不包含 API key 或任何凭证**。
* 缓存值只保存模型给出的视觉事实文本；**不保存发送者、会话、群号、URL** 等身份信息。
* 失败、空文本、熔断跳过都不会进入成功缓存；下次会重新尝试。
* 默认 ``FactStore()`` 是纯内存的；只有显式传 ``directory=`` 才会落盘。
  测试与新功能开发应使用临时目录，绝不要把运行态目录写进仓库。

接线示例（由协调器/``perceive`` 在后续批次接入，本模块自身不改业务路由）::

    from pawbot import paths, perception, vision

    data_paths = paths.for_bot(bot)
    fact_store = perception.FactStore(
        directory=data_paths.state_dir("perception"),
        namespace="vision",
        max_entries=512,
        max_bytes=8 * 1024 * 1024,
        ttl=30 * 24 * 3600,
    )

    result = vision.see_with_meta(
        raws, api_key, base_url, model,
        timeout, effort, fallback, fallback_timeout, fallback_api_key,
        mode=mode, fact_store=fact_store,
    )
    if result.omit_original_image:
        # 事实描述已经足够，主模型不必再看一遍原图（省 token）
        urls = []
    else:
        urls = vision.preview_urls(raws)
    desc = result.text

磁盘布局（``directory`` 是存储根，``namespace`` 是子目录）::

    <directory>/<namespace>/<sha256(cache_key)>.json
    {"schema": 1, "namespace": "vision", "value": "<视觉事实文本>",
     "created_at": 0.0, "expires_at": 0.0}

同一份数据根下不同 ``namespace`` 互不可见；不同数据根用不同 ``directory``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .log import log

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "FactStore",
    "PerceptionResult",
    "PROMPT_VERSION",
    "STORE_SCHEMA",
    "cache_key",
    "image_digest",
    "image_digests",
    "normalize_base_url",
    "prompt_fingerprint",
    "should_omit_original",
]

#: 提示词版本：改了 ``vision.PROMPT`` / ``STICKER_PROMPT`` / ``GRID_PROMPT``
#: 的语义就把它 +1。``vision`` 还会把提示词正文的短指纹一起放进键里，
#: 所以就算忘了改这里，提示词变了也不会复用旧事实。
PROMPT_VERSION = "vision-facts-v1"

STORE_SCHEMA = 1
DEFAULT_MAX_ENTRIES = 512
DEFAULT_MAX_BYTES = 8 * 1024 * 1024       # 8 MiB
DEFAULT_TTL_SECONDS = 30 * 24 * 3600      # 30 天

_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def image_digest(raw) -> str:
    """图片原始字节的 SHA-256（URL/路径不参与身份）。"""
    if raw is None:
        raise ValueError("image bytes required")
    return hashlib.sha256(bytes(raw)).hexdigest()


def image_digests(raws: Iterable) -> list[str]:
    return [image_digest(raw) for raw in (raws or [])]


def normalize_base_url(base_url: str) -> str:
    """把 base_url 规整成「主机 + 路径」，并去掉 userinfo/query/fragment。

    * 不把可能带凭证的 URL 片段写进缓存键或磁盘；
    * 同一中转地址末尾多个 ``/`` 不产生不同键。
    """
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw.rstrip("/")
    if not parts.scheme:
        return raw.rstrip("/")
    netloc = parts.netloc.rsplit("@", 1)[-1]          # 去掉 user:password@
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc.lower(), parts.path.rstrip("/"), "", "")
    )


def prompt_fingerprint(prompt: str, version: str = PROMPT_VERSION) -> str:
    """提示词版本 + 正文指纹；正文一改，缓存键立刻变化。"""
    body = str(prompt or "").encode("utf-8")
    return f"{str(version or '').strip()}:{hashlib.sha256(body).hexdigest()[:16]}"


def cache_key(image_digests, *, model: str = "", base_url: str = "",
              effort: str = "", prompt_version: str = PROMPT_VERSION,
              mode: str = "", namespace: str = "") -> str:
    """构造稳定缓存键。

    只接收视觉事实相关参数；调用方不得把 API key / 发送者 / 会话塞进来。
    """
    payload = {
        "schema": STORE_SCHEMA,
        "images": [str(d) for d in (image_digests or [])],
        "model": str(model or "").strip(),
        "base_url": normalize_base_url(base_url),
        "effort": str(effort or "").strip().lower(),
        "prompt_version": str(prompt_version or "").strip(),
        "mode": str(mode or "").strip().lower(),
        "namespace": str(namespace or "").strip(),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class PerceptionResult:
    """一次看图结果的附加元数据；不改变 ``vision.see/describe`` 的返回类型。"""

    text: str = ""
    key: str = ""
    keys: tuple[str, ...] = ()
    cached: bool = False
    source: str = "none"          # memory / disk / model / coalesced / mixed / none
    mode: str = ""
    image_count: int = 0
    omit_original_image: bool = False

    def __bool__(self) -> bool:
        return bool(self.text)

    @property
    def visual_facts(self) -> str:
        return self.text

    @property
    def omit_original(self) -> bool:
        """``omit_original_image`` 的短别名。"""
        return self.omit_original_image

    @property
    def from_cache(self) -> bool:
        return self.cached

    @property
    def cache_hit(self) -> bool:
        return self.cached


def should_omit_original(result) -> bool:
    """调用方辅助：缓存命中且事实描述足够时，可不再把原图喂给主模型。"""
    return bool(getattr(result, "omit_original_image", False))


@dataclass
class _Entry:
    value: str
    size: int
    created_at: float
    expires_at: float
    source: str = "memory"


@dataclass
class _Flight:
    done: threading.Event = field(default_factory=threading.Event)
    value: str | None = None
    error: BaseException | None = None


def _safe_namespace(namespace: str) -> str:
    raw = str(namespace or "").strip()
    safe = _NAME_RE.sub("_", raw).strip("._-")
    return (safe or "default")[:64]


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


class FactStore:
    """线程安全、有界的感知事实缓存。

    ``directory=None`` 时纯内存；传目录后按 ``namespace`` 落盘，重启可复用。
    写入磁盘失败（只读盘、磁盘满、权限问题）只降级为内存缓存，不影响看图。
    """

    def __init__(self, directory=None, max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 ttl: float = DEFAULT_TTL_SECONDS,
                 namespace: str = "vision"):
        raw_directory = "" if directory is None else str(os.fspath(directory)).strip()
        self.directory = (os.path.abspath(os.path.expanduser(raw_directory))
                          if raw_directory else None)
        self.namespace = _safe_namespace(namespace)
        self.max_entries = max(0, int(max_entries))
        self.max_bytes = max(0, int(max_bytes))
        self.ttl = max(0.0, float(ttl))
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0
        self._inflight: dict[str, _Flight] = {}
        self._last_error = ""
        self._disk_dir = (os.path.join(self.directory, self.namespace)
                          if self.directory else None)

    # ------------------------------------------------------------ 基础状态
    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key) -> bool:
        return self.get(key) is not None

    @staticmethod
    def _now(now) -> float:
        return time.time() if now is None else float(now)

    def _expired(self, expires_at: float, now: float) -> bool:
        return bool(expires_at) and now >= expires_at

    # ------------------------------------------------------------ 内存 LRU
    def _evict_locked(self) -> None:
        while self._entries and (len(self._entries) > self.max_entries
                                 or self._bytes > self.max_bytes):
            _key, old = self._entries.popitem(last=False)
            self._bytes -= old.size

    def _remember_locked(self, key: str, entry: _Entry) -> None:
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= old.size
        self._entries[key] = entry
        self._bytes += entry.size
        self._evict_locked()

    def _memory_lookup_locked(self, key: str, now: float) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._expired(entry.expires_at, now):
            self._entries.pop(key, None)
            self._bytes -= entry.size
            return None
        entry.source = "memory"
        self._entries.move_to_end(key)
        return entry

    # ------------------------------------------------------------ 磁盘
    def _file_name(self, key: str) -> str:
        return hashlib.sha256(str(key).encode("utf-8")).hexdigest() + ".json"

    def _path_locked(self, key: str) -> str:
        assert self._disk_dir is not None
        return os.path.join(self._disk_dir, self._file_name(key))

    def _unlink(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def _disk_lookup_locked(self, key: str, now: float) -> _Entry | None:
        if not self._disk_dir:
            return None
        path = self._path_locked(key)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:
            self._last_error = f"read {type(exc).__name__}: {exc}"
            self._unlink(path)
            return None
        if not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA:
            self._unlink(path)
            return None
        value = data.get("value")
        if not isinstance(value, str) or not value.strip():
            self._unlink(path)
            return None
        try:
            expires_at = float(data.get("expires_at") or 0.0)
            created_at = float(data.get("created_at") or 0.0)
        except (TypeError, ValueError):
            expires_at, created_at = 0.0, 0.0
        if self._expired(expires_at, now):
            self._unlink(path)
            return None
        size = _utf8_size(value)
        if (self.max_entries <= 0 or self.max_bytes <= 0
                or size > self.max_bytes):
            self._unlink(path)
            return None
        entry = _Entry(value=value, size=size, created_at=created_at,
                       expires_at=expires_at, source="disk")
        self._remember_locked(key, entry)
        return entry

    def _disk_files_locked(self) -> list[tuple[float, int, str]]:
        if not self._disk_dir:
            return []
        try:
            entries = list(os.scandir(self._disk_dir))
        except OSError:
            return []
        files: list[tuple[float, int, str]] = []
        for item in entries:
            try:
                if not item.is_file() or item.name.startswith(".tmp-"):
                    continue
                if not item.name.endswith(".json"):
                    continue
                stat = item.stat()
            except OSError:
                continue
            files.append((float(stat.st_mtime), int(stat.st_size), item.path))
        return files

    def _prune_disk_locked(self) -> None:
        files = self._disk_files_locked()
        if not files:
            return
        total = sum(size for _mtime, size, _path in files)
        files.sort(key=lambda item: (item[0], item[2]))
        while files and (len(files) > self.max_entries or total > self.max_bytes):
            _mtime, size, path = files.pop(0)
            self._unlink(path)
            total -= size

    def _write_disk_locked(self, key: str, entry: _Entry) -> None:
        if not self._disk_dir:
            return
        os.makedirs(self._disk_dir, exist_ok=True)
        path = self._path_locked(key)
        body = {
            "schema": STORE_SCHEMA,
            "namespace": self.namespace,
            "value": entry.value,
            "created_at": round(entry.created_at, 6),
            "expires_at": round(entry.expires_at, 6),
        }
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json",
                                   dir=self._disk_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except BaseException:
            self._unlink(tmp)
            raise

    # ------------------------------------------------------------ 读写
    def get_entry(self, key: str, *, now=None) -> _Entry | None:
        key = str(key)
        stamp = self._now(now)
        with self._lock:
            entry = self._memory_lookup_locked(key, stamp)
            if entry is not None:
                return entry
            return self._disk_lookup_locked(key, stamp)

    def get(self, key: str, *, now=None) -> str | None:
        entry = self.get_entry(key, now=now)
        return entry.value if entry is not None else None

    def put(self, key: str, value: str, *, now=None) -> bool:
        """写成功缓存。空值 / 超上限 / 非字符串一律拒绝，不写盘。"""
        if not isinstance(value, str) or not value.strip():
            return False
        size = _utf8_size(value)
        if (self.max_entries <= 0 or self.max_bytes <= 0
                or size > self.max_bytes):
            return False
        stamp = self._now(now)
        expires_at = (stamp + self.ttl) if self.ttl > 0 else 0.0
        entry = _Entry(value=value, size=size, created_at=stamp,
                       expires_at=expires_at, source="memory")
        with self._lock:
            self._remember_locked(str(key), entry)
            if self._disk_dir:
                try:
                    self._write_disk_locked(str(key), entry)
                    self._prune_disk_locked()
                except Exception as exc:
                    message = f"感知缓存写盘失败（{type(exc).__name__}: {exc}）"
                    if message != self._last_error:
                        log.warn(message)
                    self._last_error = message
        return True

    def set(self, key: str, value: str, *, now=None) -> bool:
        """``put`` 的别名，便于调用方按常规缓存命名使用。"""
        return self.put(key, value, now=now)

    def run(self, key: str, factory: Callable[[], str | None], *,
            now=None) -> tuple[str | None, bool, str]:
        """缓存穿透保护：同一 key 并发时只让一个线程执行 ``factory``。

        返回 ``(value, cached, source)``。``factory`` 返回空值/None 视为失败：
        不会写缓存，但同一批次等待者会复用这次失败结果，避免同 key 打爆模型。
        """
        key = str(key)
        now_arg = now
        while True:
            stamp = self._now(now_arg)
            with self._lock:
                entry = self._memory_lookup_locked(key, stamp)
                if entry is not None:
                    return entry.value, True, entry.source
                entry = self._disk_lookup_locked(key, stamp)
                if entry is not None:
                    return entry.value, True, "disk"
                flight = self._inflight.get(key)
                if flight is None:
                    flight = _Flight()
                    self._inflight[key] = flight
                    owner = True
                else:
                    owner = False

            if owner:
                value: str | None = None
                try:
                    value = factory()
                    if value is not None and not isinstance(value, str):
                        value = str(value)
                except BaseException as exc:       # 让等待者拿到同一失败
                    flight.error = exc
                    with self._lock:
                        self._inflight.pop(key, None)
                    flight.done.set()
                    raise
                cached = False
                try:
                    if value:
                        # 显式传 now 的测试用固定时钟；生产里从模型返回后再取当前时间。
                        put_now = stamp if now_arg is not None else self._now(None)
                        cached = self.put(key, value, now=put_now)
                finally:
                    flight.value = value or None
                    with self._lock:
                        self._inflight.pop(key, None)
                    flight.done.set()
                return value or None, False, "model"

            # 等待者：等 owner 完成；成功则复用内存/磁盘事实，失败则不缓存失败。
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            stamp = self._now(now_arg)
            with self._lock:
                entry = self._memory_lookup_locked(key, stamp)
            if entry is not None:
                return entry.value, True, "coalesced"
            if flight.value:
                return flight.value, False, "coalesced"
            return None, False, "coalesced"

    def get_or_compute(self, key: str, factory: Callable[[], str | None], *,
                       now=None) -> tuple[str | None, bool, str]:
        """``run`` 的别名：读缓存，未命中时 single-flight 执行 ``factory``。"""
        return self.run(key, factory, now=now)

    def clear(self) -> None:
        """清空当前 namespace 的内存与磁盘条目。"""
        with self._lock:
            self._entries.clear()
            self._bytes = 0
            for _mtime, _size, path in self._disk_files_locked():
                self._unlink(path)

    def stats(self) -> dict:
        with self._lock:
            files = self._disk_files_locked()
            return {
                "namespace": self.namespace,
                "directory": self.directory,
                "disk_dir": self._disk_dir,
                "entries": len(self._entries),
                "bytes": self._bytes,
                "disk_entries": len(files),
                "disk_bytes": sum(size for _mtime, size, _path in files),
                "inflight": len(self._inflight),
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "ttl": self.ttl,
                "last_error": self._last_error,
            }
