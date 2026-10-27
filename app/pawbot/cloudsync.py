"""云端共享/同步：人设 + 记忆 + 配置（含 API key）打包上云。

设计要点（详细说明见 docs/CLOUD-SYNC.md）：

- 适配器可插拔：``local``（本机/挂载目录）、``webdav``（坚果云/Nextcloud）、
  ``http``（自建端点，POST 上传 / GET 下载 + Bearer）。
- Bundle 是 ``tar.gz``，内含 ``persona.md``、``memory/…``、``config.json``、
  ``manifest.json``（版本、时间、逐条 sha256、pawbot 版本）。
- ``sync.passphrase`` 非空就加密（AES-256-GCM + PBKDF2-HMAC-SHA256）；
  未加密时**禁止**上传 config.json（fail-closed，防止把 API key 明文丢上云）。
- pull 默认不覆盖 ``data/config.json``，只写 ``data/config.synced.json`` 并给 diff；
  只有 ``overwrite_config=true`` 才覆盖，覆盖前先备份到 ``data/backup-sync-<时间>/``。
- 返回值和日志里不出现 password / token / passphrase 的值；异常消息会先做脱敏。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import re
import shutil
import socket
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__ as PAWBOT_VERSION
from .config import DEFAULT
from .log import log

# ---------------------------------------------------------------- 常量

SUPPORTED_PROVIDERS = ("local", "webdav", "http")

BUNDLE_FORMAT = "pawbot-sync"
BUNDLE_FORMAT_VERSION = 1
PLAIN_NAME = "pawbot-sync.tar.gz"
ENCRYPTED_NAME = "pawbot-sync.tar.gz.enc"
MANIFEST_NAME = "manifest.json"
CONFIG_TARGET = "config.json"
PERSONA_TARGET = "persona.md"
CONFIG_SYNCED = "config.synced.json"

ENCRYPTION_ALGO = "AES-256-GCM"
ENCRYPTION_KDF = "PBKDF2-HMAC-SHA256"

_MAGIC = b"PWBSYNC\x01"           # 8 字节：格式 + 版本
_SALT_LEN = 16
_NONCE_LEN = 12
_KDF_ITERATIONS = 200_000
_HTTP_TIMEOUT = 30.0

# 记忆相关的 data/ 相对目录（只同步这些，data/brain、data/browser 永不进包）
_MEMORY_DIR_KEYS = (
    ("persona.memory_dir", "data/memory", "memory"),
    ("people.dir", "data/people", "people"),
    ("longmem.dir", "data/longmem", "longmem"),
    ("diary.dir", "data/diary", "diary"),
)
_MEMORY_FILES = ("sessions.json", "relations.json", "media_ledger.json", "memes.json")

_EXCLUDED_DIRS = {"brain", "browser", "backup", "backup-sync", "napcat",
                  "__pycache__", ".git", "image_cache", "album"}
_MAX_ENTRIES = 50_000
_MAX_TOTAL_BYTES = 512 * 1024 * 1024

_CRYPTO_BACKEND: str | None = None
_CRYPTO_CHECKED = False


# ---------------------------------------------------------------- 异常

class SyncError(Exception):
    """带错误码的同步异常；消息里不允许出现凭据。"""

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


def scrub(text: Any, config=None) -> str:
    """把 sync 段里的凭据从任意文本里擦掉（日志/错误兜底用）。"""
    s = str(text)
    try:
        sec = sync_section(config) if config is not None else {}
    except Exception:
        sec = {}
    for key in ("password", "token", "passphrase"):
        value = str(sec.get(key) or "")
        if len(value) >= 3:
            s = s.replace(value, "***")
    return s


# ---------------------------------------------------------------- 配置

def sync_section(config) -> dict:
    """取合并了默认值的 sync 段（不修改传入对象）。"""
    defaults = copy.deepcopy(DEFAULT.get("sync", {}))

    if isinstance(config, dict):
        raw = config.get("sync")
        if not isinstance(raw, dict):
            raw = config if "provider" in config else {}
    else:
        try:
            raw = config.get("sync", {})
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

    merged = defaults
    merged.update(raw)

    for key in ("include_persona", "include_memory", "include_config",
                "include_stickers", "overwrite_config", "auto_pull_on_start", "enabled"):
        merged[key] = _as_bool(merged.get(key), _as_bool(defaults.get(key, False)))
    for key in ("provider", "url", "username", "password", "token", "remote_dir",
                "passphrase", "last_push", "last_pull"):
        merged[key] = "" if merged.get(key) is None else str(merged.get(key))
    merged["provider"] = (merged["provider"].strip() or "local").lower()
    merged["remote_dir"] = merged["remote_dir"].strip() or "pawbot-sync"
    return merged


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y", "是")
    return bool(value)


def _info_get(config, dotted: str, default=None):
    if isinstance(config, dict):
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
    try:
        return config.get(dotted, default)
    except Exception:
        return default


def _touch(config, key: str, value: str) -> None:
    """尽力写回 sync.last_push / sync.last_pull；失败不影响主流程。"""
    try:
        if isinstance(config, dict):
            sec = config.setdefault("sync", {})
            if isinstance(sec, dict):
                sec[key] = value
            return
        setter = getattr(config, "set", None)
        if callable(setter):
            setter(f"sync.{key}", value)
    except Exception as exc:  # noqa: BLE001 - 只是时间戳，写不进去无所谓
        log.warn(f"云同步：更新时间戳失败（{type(exc).__name__}）")


# ---------------------------------------------------------------- 加密

def _detect_crypto() -> str:
    global _CRYPTO_BACKEND, _CRYPTO_CHECKED
    if _CRYPTO_CHECKED and _CRYPTO_BACKEND:
        return _CRYPTO_BACKEND
    backend = None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        backend = "cryptography"
    except Exception:
        try:
            from Crypto.Cipher import AES  # noqa: F401
            backend = "pycryptodome"
        except Exception:
            backend = None
    _CRYPTO_BACKEND = backend
    _CRYPTO_CHECKED = True
    if not backend:
        raise SyncError(
            "CRYPTO_UNAVAILABLE",
            "设置了 passphrase 但没有加密库：请 pip install cryptography（或 pycryptodome）",
        )
    return backend


def crypto_available() -> bool:
    try:
        _detect_crypto()
        return True
    except SyncError:
        return False


def is_encrypted(blob: bytes) -> bool:
    return bytes(blob[:len(_MAGIC)]) == _MAGIC


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                               _KDF_ITERATIONS, dklen=32)


def _encrypt(plain: bytes, passphrase: str) -> bytes:
    backend = _detect_crypto()
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(passphrase, salt)
    if backend == "cryptography":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        ciphertext = AESGCM(key).encrypt(nonce, plain, None)
    else:
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        body, tag = cipher.encrypt_and_digest(plain)
        ciphertext = body + tag
    return _MAGIC + salt + nonce + ciphertext


def _decrypt(blob: bytes, passphrase: str) -> bytes:
    backend = _detect_crypto()
    if not is_encrypted(blob):
        raise SyncError("BAD_BUNDLE", "这不是 PawBot 加密同步包（文件头不对）")
    header = len(_MAGIC) + _SALT_LEN + _NONCE_LEN
    if len(blob) <= header:
        raise SyncError("BAD_BUNDLE", "同步包被截断")
    salt = blob[len(_MAGIC):len(_MAGIC) + _SALT_LEN]
    nonce = blob[len(_MAGIC) + _SALT_LEN:header]
    ciphertext = blob[header:]
    key = _derive_key(passphrase, salt)
    try:
        if backend == "cryptography":
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            return AESGCM(key).decrypt(nonce, ciphertext, None)
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        if len(ciphertext) < 16:
            raise ValueError("ciphertext too short")
        return cipher.decrypt_and_verify(ciphertext[:-16], ciphertext[-16:])
    except SyncError:
        raise
    except Exception:
        raise SyncError("BAD_PASSPHRASE", "解密失败：passphrase 不对，或同步包损坏") from None


# ---------------------------------------------------------------- 路径与文件

def _safe_relpath(name: str) -> str:
    """校验一个相对路径；有穿越/绝对路径/空段就抛 PATH_TRAVERSAL。"""
    raw = str(name or "")
    if not raw or "\x00" in raw:
        raise SyncError("PATH_TRAVERSAL", "同步包里有空路径或非法字符")
    norm = raw.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or norm.startswith("~"):
        raise SyncError("PATH_TRAVERSAL", f"同步包里有绝对路径：{raw}")
    parts = norm.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SyncError("PATH_TRAVERSAL", f"同步包里有可疑路径：{raw}")
    return "/".join(parts)


def _data_dir(base_dir: str) -> str:
    return os.path.abspath(os.path.join(base_dir, "data"))


def _apply_path(base_dir: str, target: str) -> str:
    """data 相对目标 → 绝对路径；再兜一层越界检查。"""
    target = _safe_relpath(target)
    data = _data_dir(base_dir)
    dest = os.path.abspath(os.path.join(data, *target.split("/")))
    if dest != data and not dest.startswith(data + os.sep):
        raise SyncError("PATH_TRAVERSAL", f"目标路径越界：{target}")
    return dest


def _resolve_data_dir(config, base_dir: str, dotted: str, default: str) -> tuple[str, str]:
    """配置里的目录 → (绝对路径, data 相对路径)。只允许落在 data/ 内。"""
    raw = str(_info_get(config, dotted, default) or default)
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    path = os.path.abspath(path)
    data = _data_dir(base_dir)
    if path == data:
        return path, ""
    if path.startswith(data + os.sep):
        return path, os.path.relpath(path, data).replace(os.sep, "/")
    default_rel = default.replace("data" + os.sep, "").replace("data/", "").replace("\\", "/")
    return os.path.join(data, *default_rel.split("/")), default_rel


def _persona_source(config, base_dir: str) -> tuple[str, str]:
    """人设源文件 → (绝对路径, data 相对目标)。target 固定 persona.md，逻辑上只有一份。"""
    raw = str(_info_get(config, "persona.file", "data/persona.md") or "data/persona.md")
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    path = os.path.abspath(path)
    data = _data_dir(base_dir)
    if path.startswith(data + os.sep):
        rel = os.path.relpath(path, data).replace(os.sep, "/")
    else:
        rel = PERSONA_TARGET
    return path, (PERSONA_TARGET if rel.startswith("..") else rel)


def _junk_file(name: str) -> bool:
    low = name.lower()
    if name in (".DS_Store", "Thumbs.db", "desktop.ini", ".gitkeep"):
        return True
    if low.endswith((".tmp", ".log", ".pyc", ".bak")) or ".bak-" in low:
        return True
    if low.startswith("~$") or name.endswith("~"):
        return True
    return False


def _walk_files(root: str):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _EXCLUDED_DIRS and not d.startswith("backup-sync") and d != "__pycache__"
        )
        for name in sorted(filenames):
            if _junk_file(name):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
            except OSError:
                continue
            yield path


def _bundle_path(target: str, used: set[str]) -> str:
    if target.startswith("memory/"):
        candidate = "memory/" + target[len("memory/"):]
    else:
        candidate = "memory/" + target
    if candidate in used:
        candidate = "memory/_data/" + target
    if candidate in used:
        raise SyncError("BUNDLE_CONFLICT", f"同步包内路径重复：{target}")
    used.add(candidate)
    return candidate


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_local() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


# ---------------------------------------------------------------- Manifest / Bundle

def _collect_entries(section: dict, config, base_dir: str) -> dict[str, tuple[bytes, str]]:
    """返回 bundle 路径 → (内容, data 相对目标)。"""
    data_dir = _data_dir(base_dir)
    used: set[str] = set()
    payloads: dict[str, tuple[bytes, str]] = {}

    def add(abs_path: str, target: str, bundle_path: str | None = None) -> None:
        try:
            with open(abs_path, "rb") as handle:
                blob = handle.read()
        except OSError:
            return
        path = bundle_path or _bundle_path(target, used)
        payloads[path] = (blob, target)

    persona_path, _persona_rel = _persona_source(config, base_dir)
    if section["include_persona"]:
        add(persona_path, "persona.md", PERSONA_TARGET)

    if section["include_memory"]:
        for dotted, default, kind in _MEMORY_DIR_KEYS:
            root, rel = _resolve_data_dir(config, base_dir, dotted, default)
            if not rel:
                continue
            for abs_path in _walk_files(root):
                child = os.path.relpath(abs_path, root).replace(os.sep, "/")
                target = f"{rel}/{child}"
                add(abs_path, target)
        for name in _MEMORY_FILES:
            abs_path = os.path.join(data_dir, name)
            if os.path.isfile(abs_path) and not os.path.islink(abs_path):
                add(abs_path, name)

    if section["include_stickers"]:
        root = os.path.join(data_dir, "stickers")
        for abs_path in _walk_files(root):
            child = os.path.relpath(abs_path, root).replace(os.sep, "/")
            target = f"stickers/{child}"
            add(abs_path, target, f"stickers/{child}")

    if section["include_config"]:
        config_path = os.path.join(data_dir, "config.json")
        if os.path.isfile(config_path):
            add(config_path, CONFIG_TARGET, CONFIG_TARGET)

    return payloads


def _build_bundle(section: dict, config, base_dir: str) -> tuple[bytes, dict, int]:
    """生成（必要时加密的）bundle 字节 + manifest。"""
    passphrase = str(section.get("passphrase") or "")
    if section["include_config"] and not passphrase:
        raise SyncError(
            "CONFIG_REQUIRES_ENCRYPTION",
            "include_config=true 但没设置 sync.passphrase：配置里有 API key，拒绝明文上传",
        )

    payloads = _collect_entries(section, config, base_dir)
    entries = []
    for path in sorted(payloads):
        blob, target = payloads[path]
        entries.append({
            "path": path,
            "target": target,
            "size": len(blob),
            "sha256": _sha256(blob),
        })

    manifest = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "pawbot_version": PAWBOT_VERSION,
        "created_at": _now_iso(),
        "encrypted": bool(passphrase),
        "encryption": ENCRYPTION_ALGO if passphrase else "",
        "includes": {
            "persona": section["include_persona"],
            "memory": section["include_memory"],
            "config": section["include_config"],
            "stickers": section["include_stickers"],
        },
        "entry_count": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "entries": entries,
    }

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for entry in entries:
            blob = payloads[entry["path"]][0]
            info = tarfile.TarInfo(entry["path"])
            info.size = len(blob)
            info.mtime = int(time.time())
            info.mode = 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = "pawbot"
            tar.addfile(info, io.BytesIO(blob))
        raw_manifest = json.dumps(manifest, ensure_ascii=False, indent=2,
                                  sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(raw_manifest)
        info.mtime = int(time.time())
        info.mode = 0o600
        info.uid = info.gid = 0
        info.uname = info.gname = "pawbot"
        tar.addfile(info, io.BytesIO(raw_manifest))

    bundle = buffer.getvalue()
    if passphrase:
        bundle = _encrypt(bundle, passphrase)
    return bundle, manifest, len(buffer.getvalue())


def _read_bundle(raw: bytes, passphrase: str) -> tuple[dict, dict[str, tuple[str, bytes]]]:
    """解密 + 解包 + 校验。返回 (manifest, {bundle路径: (target, bytes)})。"""
    plain = _decrypt(raw, passphrase) if is_encrypted(raw) else raw

    if plain[:2] != b"\x1f\x8b":
        raise SyncError("BAD_BUNDLE", "同步包不是 gzip 数据")
    try:
        tar = tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise SyncError("BAD_BUNDLE", f"打不开同步包：{type(exc).__name__}") from None

    files: dict[str, bytes] = {}
    total = 0
    with tar:
        members = tar.getmembers()
        if len(members) > _MAX_ENTRIES:
            raise SyncError("BAD_BUNDLE", f"同步包条目太多：{len(members)}")
        for member in members:
            name = _safe_relpath(member.name)
            if member.issym() or member.islnk():
                raise SyncError("PATH_TRAVERSAL", f"同步包里不允许链接：{name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise SyncError("BAD_BUNDLE", f"同步包里有非普通文件：{name}")
            if member.size > _MAX_TOTAL_BYTES:
                raise SyncError("BAD_BUNDLE", "同步包里有超大文件")
            total += member.size
            if total > _MAX_TOTAL_BYTES:
                raise SyncError("BAD_BUNDLE", "同步包解压后太大")
            extracted = tar.extractfile(member)
            if extracted is None:
                raise SyncError("BAD_BUNDLE", f"读不出包内文件：{name}")
            if name in files:
                raise SyncError("MANIFEST_INVALID", f"同步包里有重名文件：{name}")
            files[name] = extracted.read()

    raw_manifest = files.pop(MANIFEST_NAME, None)
    if raw_manifest is None:
        raise SyncError("MANIFEST_INVALID", "同步包里没有 manifest.json")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except Exception:
        raise SyncError("MANIFEST_INVALID", "manifest.json 不是合法 JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
        raise SyncError("MANIFEST_INVALID", "manifest.json 格式不对")
    try:
        version = int(manifest.get("format_version") or 0)
    except Exception:
        version = 0
    if version < 1 or version > BUNDLE_FORMAT_VERSION:
        raise SyncError("MANIFEST_INVALID", f"不支持的同步包版本：{manifest.get('format_version')}")

    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        raise SyncError("MANIFEST_INVALID", "manifest.json 缺少 entries")

    checked: dict[str, tuple[str, bytes]] = {}
    seen_targets: set[str] = set()
    for item in raw_entries:
        if not isinstance(item, dict):
            raise SyncError("MANIFEST_INVALID", "manifest.json 的条目格式不对")
        path = _safe_relpath(item.get("path"))
        target = _safe_relpath(item.get("target"))
        if target in seen_targets:
            raise SyncError("MANIFEST_INVALID", f"manifest 里有重复目标：{target}")
        seen_targets.add(target)
        blob = files.get(path)
        if blob is None:
            raise SyncError("MANIFEST_INVALID", f"manifest 声明的文件不在包里：{path}")
        if int(item.get("size", -1)) != len(blob):
            raise SyncError("MANIFEST_MISMATCH", f"文件大小对不上：{path}")
        if str(item.get("sha256", "")) != _sha256(blob):
            raise SyncError("MANIFEST_MISMATCH", f"文件 sha256 对不上：{path}")
        checked[path] = (target, blob)

    extra = set(files) - set(checked)
    if extra:
        raise SyncError("MANIFEST_INVALID", f"包里有 manifest 未声明的文件：{sorted(extra)[0]}")
    return manifest, checked


# ---------------------------------------------------------------- 适配器

class Adapter:
    name = "base"

    def upload(self, data: bytes, name: str) -> None:
        raise NotImplementedError

    def download(self, name: str) -> bytes:
        raise NotImplementedError

    def test(self) -> dict:
        raise NotImplementedError


class LocalAdapter(Adapter):
    """本机目录 / 挂载盘。原子写：.tmp → os.replace。"""

    name = "local"

    def __init__(self, root: str):
        root = os.path.abspath(os.path.expanduser(str(root)))
        if not root:
            raise SyncError("CONFIG_INVALID", "local 需要 sync.url（目录路径）")
        self.root = root

    def _path(self, name: str) -> str:
        safe = _safe_relpath(name)
        if "/" in safe:
            raise SyncError("CONFIG_INVALID", f"远端文件名不合法：{name}")
        return os.path.join(self.root, safe)

    def upload(self, data: bytes, name: str) -> None:
        path = self._path(name)
        try:
            os.makedirs(self.root, exist_ok=True)
            _atomic_write(path, data)
        except OSError as exc:
            raise SyncError("IO_ERROR", f"写本地同步目录失败：{type(exc).__name__}") from None

    def download(self, name: str) -> bytes:
        path = self._path(name)
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            raise SyncError("NOT_FOUND", f"本地同步目录里没有 {name}") from None
        except OSError as exc:
            raise SyncError("IO_ERROR", f"读本地同步目录失败：{type(exc).__name__}") from None

    def test(self) -> dict:
        try:
            os.makedirs(self.root, exist_ok=True)
            probe = os.path.join(self.root, f".pawbot-probe-{os.getpid()}")
            with open(probe, "wb") as handle:
                handle.write(b"ok")
            os.remove(probe)
        except OSError as exc:
            raise SyncError("IO_ERROR", f"本地同步目录不可写：{type(exc).__name__}") from None
        return {"ok": True, "detail": f"目录可读写：{self.root}"}


def _redact_url(url: str) -> str:
    """去掉 URL 里的 user:pass 和 query（query 里常带 token）。"""
    try:
        parts = urllib.parse.urlsplit(str(url))
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return ""


def _http_sync_error(what: str, status: int) -> SyncError:
    if status in (401, 403):
        return SyncError("AUTH_FAILED",
                         f"{what}被拒绝（HTTP {status}）：检查用户名/密码或 token",
                         http_status=status)
    if status == 404:
        return SyncError("NOT_FOUND", f"{what}失败：HTTP 404", http_status=status)
    if 400 <= status < 500:
        return SyncError("HTTP_4XX", f"{what}失败：HTTP {status}", http_status=status)
    if status >= 500:
        return SyncError("HTTP_5XX", f"{what}失败：服务端报错 HTTP {status}", http_status=status)
    return SyncError("HTTP_ERROR", f"{what}失败：HTTP {status}", http_status=status)


def _http_request(method: str, url: str, data: bytes | None = None,
                  headers: dict | None = None, timeout: float = _HTTP_TIMEOUT,
                  what: str = "请求") -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        raise _http_sync_error(what, int(exc.code)) from None
    except (socket.timeout, TimeoutError):
        raise SyncError("TIMEOUT", f"{what}超时（{int(timeout)} 秒）") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise SyncError("TIMEOUT", f"{what}超时（{int(timeout)} 秒）") from None
        raise SyncError("NETWORK", f"{what}失败：连不上服务器（{type(reason).__name__}）") from None
    except OSError as exc:
        raise SyncError("NETWORK", f"{what}失败：{type(exc).__name__}") from None


class HttpAdapter(Adapter):
    """自建端点：POST 上传 / GET 下载，可选 Bearer。

    ``sync.url`` 有两种写法：
      - 完整端点（推荐）：``https://example.com/pawbot-sync``，会用 ``?name=`` 传文件名；
      - 模板：``https://example.com/pawbot-sync/{name}``。
    """

    name = "http"

    def __init__(self, url: str, token: str = "", timeout: float = _HTTP_TIMEOUT):
        url = str(url or "").strip()
        if not url:
            raise SyncError("CONFIG_INVALID", "http provider 需要 sync.url（完整端点）")
        if not url.startswith(("http://", "https://")):
            raise SyncError("CONFIG_INVALID", "http provider 的 sync.url 必须是 http(s):// 开头")
        self.url = url
        self.token = str(token or "")
        self.timeout = timeout

    def _url_for(self, name: str) -> str:
        quoted = urllib.parse.quote(name, safe="")
        if "{name}" in self.url:
            return self.url.replace("{name}", quoted)
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}name={quoted}"

    def _headers(self, upload: bool) -> dict:
        headers = {"User-Agent": f"PawBot/{PAWBOT_VERSION}",
                   "Accept": "application/octet-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if upload:
            headers["Content-Type"] = "application/octet-stream"
        return headers

    def upload(self, data: bytes, name: str) -> None:
        status, _ = _http_request("POST", self._url_for(name), data=data,
                                  headers=self._headers(True), timeout=self.timeout,
                                  what="上传同步包")
        if status >= 400:
            raise _http_sync_error("上传同步包", status)

    def download(self, name: str) -> bytes:
        status, body = _http_request("GET", self._url_for(name),
                                     headers=self._headers(False), timeout=self.timeout,
                                     what="下载同步包")
        if status >= 400:
            raise _http_sync_error("下载同步包", status)
        return body

    def test(self) -> dict:
        probe = "__pawbot_selftest__"
        status, _ = _http_request("GET", self._url_for(probe),
                                  headers=self._headers(False), timeout=self.timeout,
                                  what="探测端点")
        if status in (200, 201, 204, 404):
            return {"ok": True, "detail": f"端点可达（HTTP {status}）"}
        raise _http_sync_error("探测端点", status)


class WebdavAdapter(Adapter):
    """WebDAV（坚果云 / Nextcloud / 群晖）：PUT / GET / MKCOL / PROPFIND。"""

    name = "webdav"

    def __init__(self, url: str, username: str = "", password: str = "",
                 remote_dir: str = "pawbot-sync", timeout: float = _HTTP_TIMEOUT):
        url = str(url or "").strip()
        if not url:
            raise SyncError("CONFIG_INVALID", "webdav provider 需要 sync.url（目录 URL）")
        if not url.startswith(("http://", "https://")):
            raise SyncError("CONFIG_INVALID", "webdav 的 sync.url 必须是 http(s):// 开头")
        self.base = url.rstrip("/")
        self.username = str(username or "")
        self.password = str(password or "")
        self.remote_dir = "/".join(
            part for part in str(remote_dir or "pawbot-sync").replace("\\", "/").split("/")
            if part and part not in (".", "..")
        ) or "pawbot-sync"
        self.timeout = timeout
        self._dir_ready = False

    def _headers(self, upload: bool) -> dict:
        headers = {"User-Agent": f"PawBot/{PAWBOT_VERSION}"}
        if self.username:
            raw = f"{self.username}:{self.password}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        if upload:
            headers["Content-Type"] = "application/octet-stream"
        return headers

    @staticmethod
    def _encode_path(path: str) -> str:
        return "/".join(urllib.parse.quote(part, safe="") for part in path.split("/") if part)

    def _dir_url(self) -> str:
        return f"{self.base}/{self._encode_path(self.remote_dir)}"

    def _file_url(self, name: str) -> str:
        safe = _safe_relpath(name)
        if "/" in safe:
            raise SyncError("CONFIG_INVALID", f"远端文件名不合法：{name}")
        return f"{self._dir_url()}/{urllib.parse.quote(safe, safe='')}"

    def _ensure_dir(self) -> None:
        if self._dir_ready:
            return
        parts = self.remote_dir.split("/")
        for index in range(1, len(parts) + 1):
            sub = "/".join(parts[:index])
            url = f"{self.base}/{self._encode_path(sub)}"
            try:
                status, _ = _http_request("MKCOL", url, headers=self._headers(False),
                                          timeout=self.timeout, what="创建远端目录")
            except SyncError as exc:
                # 已存在（405）或并发创建（409）都不算错
                if exc.http_status in (405, 409):
                    continue
                raise
            if status not in (200, 201, 204, 301, 302, 405, 409):
                raise _http_sync_error("创建远端目录", status)
        self._dir_ready = True

    def upload(self, data: bytes, name: str) -> None:
        self._ensure_dir()
        status, _ = _http_request("PUT", self._file_url(name), data=data,
                                  headers=self._headers(True), timeout=self.timeout,
                                  what="上传同步包")
        if status >= 400:
            raise _http_sync_error("上传同步包", status)

    def download(self, name: str) -> bytes:
        status, body = _http_request("GET", self._file_url(name),
                                     headers=self._headers(False), timeout=self.timeout,
                                     what="下载同步包")
        if status >= 400:
            raise _http_sync_error("下载同步包", status)
        return body

    def test(self) -> dict:
        self._ensure_dir()
        request_headers = self._headers(False)
        request_headers["Depth"] = "0"
        status, _ = _http_request("PROPFIND", self._dir_url(), headers=request_headers,
                                  timeout=self.timeout, what="查询远端目录")
        if status in (200, 207):
            return {"ok": True, "detail": f"远端目录可用（HTTP {status}）"}
        raise _http_sync_error("查询远端目录", status)


def make_adapter(section: dict, base_dir: str | None = None) -> Adapter:
    provider = str(section.get("provider") or "local").strip().lower()
    url = str(section.get("url") or "").strip()
    if provider == "local":
        if not url:
            raise SyncError("CONFIG_INVALID", "local 需要 sync.url（目录路径）")
        if base_dir and not os.path.isabs(os.path.expanduser(url)):
            url = os.path.join(base_dir, url)
        return LocalAdapter(url)
    if provider == "webdav":
        return WebdavAdapter(url, username=section.get("username", ""),
                             password=section.get("password", ""),
                             remote_dir=section.get("remote_dir", "pawbot-sync"))
    if provider == "http":
        return HttpAdapter(url, token=section.get("token", ""))
    raise SyncError("UNKNOWN_PROVIDER",
                    f"不支持的 provider：{provider}（可选 {' / '.join(SUPPORTED_PROVIDERS)}）")


# ---------------------------------------------------------------- diff / 备份

def _flatten(node: Any, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    if not isinstance(node, dict):
        out[prefix or ""] = json.dumps(node, ensure_ascii=False, sort_keys=True)
        return out
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            _flatten(value, dotted, out)
        else:
            out[dotted] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return out


def _config_diff(local: dict, remote: dict) -> dict:
    left = _flatten(local if isinstance(local, dict) else {})
    right = _flatten(remote if isinstance(remote, dict) else {})
    changed = sorted(k for k in set(left) & set(right) if left[k] != right[k])
    added = sorted(set(right) - set(left))
    removed = sorted(set(left) - set(right))
    return {
        "changed": changed,
        "added": added,
        "removed": removed,
        "count": len(changed) + len(added) + len(removed),
    }


def _backup_dir(base_dir: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    root = os.path.join(_data_dir(base_dir), f"backup-sync-{stamp}")
    candidate = root
    index = 2
    while os.path.exists(candidate):
        candidate = f"{root}-{index}"
        index += 1
    return candidate


def _backup_files(base_dir: str, targets: list[str]) -> tuple[str, list[str]]:
    """覆盖前备份（保持 data 相对路径）；返回 (备份目录, 备份了哪些)。"""
    existing = [target for target in targets if os.path.isfile(_apply_path(base_dir, target))]
    if not existing:
        return "", []
    backup = _backup_dir(base_dir)
    saved: list[str] = []
    data = _data_dir(base_dir)
    for target in sorted(set(existing)):
        source = _apply_path(base_dir, target)
        relative = os.path.relpath(source, data).replace(os.sep, "/")
        destination = os.path.join(backup, *relative.split("/"))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
        saved.append(relative)
    return backup, saved


# ---------------------------------------------------------------- 高层 API

def push(config, base_dir: str, dry: bool = False) -> dict:
    """打包并上传。dry=True 只打包、报摘要，不写远端、不动时间戳。"""
    started = time.perf_counter()
    section = sync_section(config)
    adapter = make_adapter(section, base_dir)
    bundle, manifest, plain_size = _build_bundle(section, config, base_dir)
    encrypted = bool(section.get("passphrase") or "")
    remote_name = ENCRYPTED_NAME if encrypted else PLAIN_NAME
    digest = _sha256(bundle)

    if not dry:
        adapter.upload(bundle, remote_name)
        stamp = _now_local()
        _touch(config, "last_push", stamp)

    elapsed = int((time.perf_counter() - started) * 1000)
    result = {
        "ok": True,
        "dry_run": bool(dry),
        "provider": adapter.name,
        "remote_name": remote_name,
        "bytes": len(bundle),
        "bundle_bytes": plain_size,
        "sha256": digest,
        "encrypted": encrypted,
        "encryption": ENCRYPTION_ALGO if encrypted else "",
        "files": manifest.get("entry_count", 0),
        "includes": manifest.get("includes", {}),
        "last_push": "" if dry else _read_last(config, "last_push"),
        "duration_ms": elapsed,
    }
    log.info(
        f"云同步上传{'（试运行）' if dry else '完成'}：provider={adapter.name} "
        f"文件={result['files']} 大小={len(bundle)}B 加密={'是' if encrypted else '否'} "
        f"耗时={elapsed}ms"
    )
    return result


def _read_last(config, key: str) -> str:
    try:
        return str(sync_section(config).get(key) or "")
    except Exception:
        return ""


def pull(config, base_dir: str, dry: bool = False,
         overwrite_config: bool | None = None) -> dict:
    """下载并应用。默认不动 data/config.json，只写 config.synced.json。"""
    started = time.perf_counter()
    section = sync_section(config)
    adapter = make_adapter(section, base_dir)
    passphrase = str(section.get("passphrase") or "")

    raw, remote_name = _download(adapter, passphrase)
    manifest, entries = _read_bundle(raw, passphrase)

    include_persona = section["include_persona"]
    include_memory = section["include_memory"]
    include_config = section["include_config"]
    include_stickers = section["include_stickers"]

    persona_target = _persona_source(config, base_dir)[1]

    writes: list[tuple[str, bytes]] = []
    applied: list[str] = []
    skipped: list[str] = []
    remote_config = None

    for path, (target, blob) in sorted(entries.items()):
        category = _target_category(target)
        if category == "config":
            if not include_config:
                skipped.append(target)
                continue
            try:
                remote_config = json.loads(blob.decode("utf-8-sig"))
            except Exception:
                raise SyncError("BAD_BUNDLE", "同步包里的 config.json 不是合法 JSON") from None
            if not isinstance(remote_config, dict):
                raise SyncError("BAD_BUNDLE", "同步包里的 config.json 不是对象")
            continue
        if category == "persona" and not include_persona:
            skipped.append(target)
            continue
        if category == "memory" and not include_memory:
            skipped.append(target)
            continue
        if category == "stickers" and not include_stickers:
            skipped.append(target)
            continue

        # 人设按本机的 persona.file 落盘（跨机器路径可以不同）
        apply_target = persona_target if category == "persona" else target
        writes.append((apply_target, blob))

    # 内容没变的文件不写、不备份，避免每拉一次就攒一个备份目录
    unchanged: list[str] = []
    pending: list[tuple[str, bytes]] = []
    for target, blob in writes:
        try:
            with open(_apply_path(base_dir, target), "rb") as handle:
                current = handle.read()
        except OSError:
            current = None
        if current == blob:
            unchanged.append(target)
        else:
            pending.append((target, blob))
    writes = pending

    config_result = {
        "synced_path": "",
        "overwritten": False,
        "applied_config": False,
        "backup_dir": "",
        "diff": {},
    }

    local_config_path = os.path.join(_data_dir(base_dir), "config.json")
    local_config = _read_json(local_config_path, {}) or {}

    if remote_config is not None:
        config_result["diff"] = _config_diff(
            local_config if isinstance(local_config, dict) else {}, remote_config
        )

    overwrite = (section["overwrite_config"] if overwrite_config is None
                 else bool(overwrite_config))
    if remote_config is not None and overwrite and include_config:
        merged = copy.deepcopy(remote_config)
        if isinstance(local_config, dict) and isinstance(local_config.get("sync"), dict):
            merged["sync"] = copy.deepcopy(local_config["sync"])
        config_result["_merged"] = merged  # 内部用，返回前删掉

    target_paths = [target for target, _ in writes]
    if remote_config is not None and include_config and overwrite:
        target_paths.append(CONFIG_TARGET)

    backup_path = ""
    backed_up: list[str] = []
    if not dry:
        backup_path, backed_up = _backup_files(base_dir, target_paths)

    if not dry:
        for target, blob in writes:
            _atomic_write(_apply_path(base_dir, target), blob)
            applied.append(target)

        if remote_config is not None and include_config:
            synced_path = os.path.join(_data_dir(base_dir), CONFIG_SYNCED)
            payload = json.dumps(remote_config, ensure_ascii=False, indent=2).encode("utf-8")
            _atomic_write(synced_path, payload)
            config_result["synced_path"] = f"data/{CONFIG_SYNCED}"

            if overwrite:
                merged = config_result.pop("_merged", None)
                if merged is None:
                    merged = copy.deepcopy(remote_config)
                config_path = os.path.join(_data_dir(base_dir), "config.json")
                _atomic_write(config_path,
                              json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8"))
                config_result["overwritten"] = True
                config_result["applied_config"] = True
                applied.append(CONFIG_TARGET)
    else:
        applied = [target for target, _ in writes]
        if remote_config is not None and include_config:
            config_result["synced_path"] = f"data/{CONFIG_SYNCED}（试运行，未写）"
            if overwrite:
                applied.append(CONFIG_TARGET)

    config_result.pop("_merged", None)
    if backup_path:
        config_result["backup_dir"] = os.path.relpath(backup_path, base_dir).replace(os.sep, "/")
        config_result["backed_up"] = backed_up

    elapsed = int((time.perf_counter() - started) * 1000)
    if not dry:
        stamp = _now_local()
        _touch(config, "last_pull", stamp)

    result = {
        "ok": True,
        "dry_run": bool(dry),
        "provider": adapter.name,
        "remote_name": remote_name,
        "bytes": len(raw),
        "encrypted": is_encrypted(raw),
        "encryption": ENCRYPTION_ALGO if is_encrypted(raw) else "",
        "files": manifest.get("entry_count", len(entries)),
        "applied": applied,
        "unchanged": unchanged,
        "skipped": skipped,
        "manifest_created_at": manifest.get("created_at", ""),
        "pawbot_version": manifest.get("pawbot_version", ""),
        "config": config_result,
        "backup_dir": config_result["backup_dir"],
        "last_pull": "" if dry else _read_last(config, "last_pull"),
        "duration_ms": elapsed,
    }
    log.info(
        f"云同步拉取{'（试运行）' if dry else '完成'}：provider={adapter.name} "
        f"文件={result['files']} 写入={len(applied)} 加密={result['encrypted']} "
        f"耗时={elapsed}ms"
    )
    return result


def _download(adapter: Adapter, passphrase: str) -> tuple[bytes, str]:
    wants_encrypted = bool(passphrase)
    primary = ENCRYPTED_NAME if wants_encrypted else PLAIN_NAME
    alternate = PLAIN_NAME if wants_encrypted else ENCRYPTED_NAME

    try:
        return adapter.download(primary), primary
    except SyncError as exc:
        if exc.code != "NOT_FOUND":
            raise

    try:
        raw = adapter.download(alternate)
    except SyncError as exc:
        if exc.code == "NOT_FOUND":
            raise SyncError("NOT_FOUND", "远端还没有同步包，先 push 一次") from None
        raise

    if wants_encrypted:
        raise SyncError(
            "ENCRYPTION_MISMATCH",
            "本机设置了 passphrase，但远端是明文包；拒绝拉取（防止被降级）",
        )
    raise SyncError("PASSPHRASE_REQUIRED", "远端是加密包，请先配置 sync.passphrase")


def _target_category(target: str) -> str:
    if target == CONFIG_TARGET:
        return "config"
    if target == PERSONA_TARGET or ("/" not in target and target.endswith(".md")):
        return "persona"
    if target.startswith("stickers/"):
        return "stickers"
    for root in ("memory/", "people/", "longmem/", "diary/", "sessions/"):
        if target.startswith(root):
            return "memory"
    if target in _MEMORY_FILES:
        return "memory"
    raise SyncError("BAD_BUNDLE", f"同步包里有未知路径：{target}")


def status(config, base_dir: str | None = None) -> dict:
    """只读状态；不含任何密码/token/passphrase 的值。"""
    section = sync_section(config)
    provider = section["provider"]
    url = section["url"]
    local_url = ""
    remote_url = ""
    if provider == "local" and url:
        path = os.path.expanduser(url)
        if base_dir and not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        local_url = os.path.abspath(path)
    else:
        remote_url = _redact_url(url)

    warning = ""
    if section["include_config"] and not section["passphrase"]:
        warning = "CONFIG_REQUIRES_ENCRYPTION"

    return {
        "enabled": section["enabled"],
        "provider": provider,
        "provider_label": {"local": "本机/挂载目录", "webdav": "WebDAV",
                           "http": "自建 HTTP 端点"}.get(provider, provider),
        "url": local_url or remote_url,
        "remote_dir": section["remote_dir"] if provider == "webdav" else "",
        "ready": bool(url),
        "encrypted": bool(section["passphrase"]),
        "encryption": ENCRYPTION_ALGO if section["passphrase"] else "",
        "include": {
            "persona": section["include_persona"],
            "memory": section["include_memory"],
            "config": section["include_config"],
            "stickers": section["include_stickers"],
        },
        "overwrite_config": section["overwrite_config"],
        "auto_pull_on_start": section["auto_pull_on_start"],
        "remote_name": ENCRYPTED_NAME if section["passphrase"] else PLAIN_NAME,
        "last_push": section["last_push"],
        "last_pull": section["last_pull"],
        "username_set": bool(section["username"]),
        "password_set": bool(section["password"]),
        "token_set": bool(section["token"]),
        "passphrase_set": bool(section["passphrase"]),
        "warning": warning,
        "supported_providers": list(SUPPORTED_PROVIDERS),
    }


def test_connection(config, base_dir: str | None = None) -> dict:
    """探测远端；失败抛 SyncError（消息已脱敏）。"""
    section = sync_section(config)
    adapter = make_adapter(section, base_dir)
    started = time.perf_counter()
    detail = adapter.test() or {}
    return {
        "ok": True,
        "provider": adapter.name,
        "detail": str(detail.get("detail", "连接成功")),
        "encrypted": bool(section["passphrase"]),
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


# ---------------------------------------------------------------- 启动钩子

def _auto_pull_worker(config, base_dir: str) -> None:
    try:
        result = pull(config, base_dir)
        log.info(
            f"云同步启动拉取完成：文件={result.get('files', 0)} "
            f"写入={len(result.get('applied', []))} "
            f"加密={'是' if result.get('encrypted') else '否'}"
        )
    except SyncError as exc:
        log.warn(f"云同步启动拉取失败（不影响启动）：{exc.code} {exc.message}")
    except Exception as exc:  # noqa: BLE001 - 启动钩子绝不能把主程序带崩
        log.warn(f"云同步启动拉取失败（不影响启动）：{type(exc).__name__} {scrub(exc, config)}")


def start_auto_pull(config, base_dir: str) -> threading.Thread | None:
    """sync.enabled && sync.auto_pull_on_start 时，后台线程拉一次；失败只告警。"""
    try:
        section = sync_section(config)
    except Exception:
        return None
    if not section["enabled"] or not section["auto_pull_on_start"]:
        return None

    thread = threading.Thread(
        target=_auto_pull_worker,
        args=(config, base_dir),
        name="cloudsync-auto-pull",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "SyncError",
    "SUPPORTED_PROVIDERS",
    "PLAIN_NAME",
    "ENCRYPTED_NAME",
    "ENCRYPTION_ALGO",
    "sync_section",
    "make_adapter",
    "crypto_available",
    "is_encrypted",
    "scrub",
    "push",
    "pull",
    "status",
    "test_connection",
    "start_auto_pull",
]
