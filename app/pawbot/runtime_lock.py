"""运行时单实例锁协议（Python 唯一实现；Node 侧通过本文件的 CLI 复用同一算法）。

设计要点（批次 2 修复版）：

* **互斥用 OS advisory lock，不用可陈旧的 mutex 文件**：
  锁一个固定且永不删除的 guard 文件 `<dataRoot>/.locks/guard.lock`。
  Windows 用 ``msvcrt.locking``（LockFile，进程崩溃/退出时由内核自动释放），
  POSIX 用 ``fcntl.flock``。任何进程死亡都不会留下"永久卡住的锁"。
* **正式锁原子发布**：先写同目录临时文件并 fsync，再 ``link`` 到
  ``.runtime.lock``；文件系统不支持硬链接时，在 guard 互斥内用 ``rename``
  （同样是原子发布，绝不原地 ``open(wx)+write`` 留下半截 JSON）。
* **接管只认明确死亡的 owner**：活 pid / 无法判断 → 拒绝；不看 mtime、不看墙钟。
* **归档有身份核对**：归档后回读内容与接管前一致才算成功；失败尽力还原并重试。

本模块只依赖标准库，可被 ``python runtime_lock.py <op>`` 直接调用（stdin 一行
JSON，stdout 一行 JSON），也可被 ``pawbot.lifecycle`` 直接 import。
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

RUNTIME_LOCK_FILE = ".runtime.lock"
LOCK_ARCHIVE_DIR = ".locks"
LOCK_GUARD_FILE = "guard.lock"
LOCK_FIELDS = ("pid", "started_at", "host_pid", "version", "nonce")
LOCK_NONCE_BYTES = 16
GUARD_MIN_TIMEOUT = 0.05
GUARD_MAX_TIMEOUT = 60.0

# 归档/接管时需要重试的错误码（全部有 deadline，不做无界忙等）。
_RETRYABLE_LOCK_ERRNOS = {
    errno.EACCES,
    getattr(errno, "EDEADLK", -1),
    getattr(errno, "EDEADLOCK", -1),
    errno.EAGAIN,
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    errno.EINTR,
}
_UNSUPPORTED_LINK_ERRNOS = {
    errno.EPERM,
    errno.EACCES,
    errno.ENOSYS,
    errno.EXDEV,
    errno.EMLINK,
    getattr(errno, "EOPNOTSUPP", -1),
    getattr(errno, "ENOTSUP", -1),
}


class GuardUnavailable(RuntimeError):
    """OS advisory lock 不可用 / 创建 guard 失败（必须 fail-closed，不降级）。"""


class GuardTimeout(RuntimeError):
    """在 deadline 内没有拿到 guard。"""


class LockExists(RuntimeError):
    """发布正式锁时发现目标已存在（其它实例刚发布）。"""


class LockPublishError(RuntimeError):
    """创建正式锁失败。"""


# ------------------------------------------------------------------ 路径
def _root(data_root: str) -> str:
    return os.path.abspath(str(data_root or "."))


def runtime_lock_path(data_root: str) -> str:
    return os.path.join(_root(data_root), RUNTIME_LOCK_FILE)


def lock_archive_dir(data_root: str) -> str:
    return os.path.join(_root(data_root), LOCK_ARCHIVE_DIR)


def lock_guard_path(data_root: str) -> str:
    return os.path.join(lock_archive_dir(data_root), LOCK_GUARD_FILE)


def now_iso(now: Callable[[], float] | None = None) -> str:
    moment = (now or time.time)()
    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


def new_nonce() -> str:
    return secrets.token_hex(LOCK_NONCE_BYTES)


# ------------------------------------------------------------------ 进程存活
def pid_alive(pid: Any) -> bool | None:
    """true=活；false=明确不存在；None=无法判断（调用方必须 fail-closed）。"""
    if isinstance(pid, bool):
        return None
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if os.name != "nt":
        try:
            os.kill(value, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:          # ERROR_ACCESS_DENIED：存在但无权限
                return True
            if error in (87, 1168):  # 参数无效 / 进程不存在
                return False
            return None
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 —— 探测异常 = 未知，调用方 fail-closed
        return None


# ------------------------------------------------------------------ guard
class GuardLock:
    """固定 guard 文件上的 OS 排它锁；进程退出/崩溃由内核释放。"""

    def __init__(self, data_root: str, timeout: float = 5.0):
        self.path = lock_guard_path(data_root)
        self.timeout = max(GUARD_MIN_TIMEOUT, min(GUARD_MAX_TIMEOUT, float(timeout or 0)))
        self._fd: int | None = None

    def __enter__(self) -> "GuardLock":
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            self._fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise GuardUnavailable(f"打开 guard 文件失败：{exc}") from exc
        try:
            self._acquire()
            return self
        except Exception:
            self._close()
            raise

    def __exit__(self, *_exc) -> None:
        try:
            self._release()
        finally:
            self._close()

    def _acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        backoff = 0.004
        last: OSError | None = None
        while True:
            try:
                self._try_lock()
                return
            except OSError as exc:
                if exc.errno not in _RETRYABLE_LOCK_ERRNOS:
                    raise GuardUnavailable(f"OS 文件锁不可用：{exc}") from exc
                last = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GuardTimeout(
                    f"等待 guard 超时（{self.timeout:.2f}s）：{last}") from last
            time.sleep(min(backoff, remaining))
            backoff = min(0.05, backoff * 1.6)

    def _try_lock(self) -> None:
        assert self._fd is not None
        if os.name == "nt":
            import msvcrt

            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            # 退出路径不抛；fd close 也会释放锁。
            pass

    def _close(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


def guard_lock(data_root: str, timeout: float = 5.0) -> GuardLock:
    return GuardLock(data_root, timeout=timeout)


# ------------------------------------------------------------------ 锁文件
def validate_lock(lock: Any) -> tuple[bool, str]:
    if not isinstance(lock, dict):
        return False, "不是对象"
    pid = lock.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False, "pid 非法"
    if not isinstance(lock.get("started_at"), str) or not lock["started_at"].strip():
        return False, "started_at 缺失"
    host_pid = lock.get("host_pid")
    if not isinstance(host_pid, int) or isinstance(host_pid, bool) or host_pid < 0:
        return False, "host_pid 非法"
    if not isinstance(lock.get("version"), str):
        return False, "version 非法"
    nonce = lock.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 8:
        return False, "nonce 非法"
    return True, ""


def read_lock(data_root: str) -> tuple[bool, dict | None, str | None, str | None]:
    """返回 (present, lock, raw, error)；present=false 表示锁文件不存在。"""
    path = runtime_lock_path(data_root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return False, None, None, None
    except NotADirectoryError:
        return False, None, None, None
    except OSError as exc:
        return True, None, None, f"读取运行锁失败：{exc}"
    try:
        lock = json.loads(raw)
    except json.JSONDecodeError:
        return True, None, raw, "运行锁不是合法 JSON"
    valid, reason = validate_lock(lock)
    if not valid:
        return True, None, raw, f"运行锁字段非法：{reason}"
    return True, lock, raw, None


def inspect_lock(data_root: str,
                 is_alive: Callable[[Any], bool | None] | None = None) -> dict:
    probe = is_alive or pid_alive
    present, lock, _raw, error = read_lock(data_root)
    path = runtime_lock_path(data_root)
    if not present:
        return {"path": path, "present": False, "lock": None,
                "alive": None, "stale": False, "error": None}
    if lock is None:
        return {"path": path, "present": True, "lock": None,
                "alive": None, "stale": False, "error": error}
    try:
        alive = probe(lock["pid"])
    except Exception:  # noqa: BLE001 —— 探测异常 = 未知
        alive = None
    return {
        "path": path,
        "present": True,
        "lock": lock,
        "alive": alive,
        "stale": alive is False,
        "error": None if alive is not None else f"无法确认 pid={lock['pid']} 是否存活",
    }


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("写入不足")
        view = view[written:]


def publish_lock(data_root: str, text: str) -> None:
    """在 guard 内原子发布正式锁（调用方必须已持有 guard）。"""
    path = runtime_lock_path(data_root)
    tmp = f"{path}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                     | getattr(os, "O_BINARY", 0), 0o600)
        try:
            _write_all(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
            return
        except FileExistsError as exc:
            raise LockExists("正式锁已被其它实例发布") from exc
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_LINK_ERRNOS:
                raise LockPublishError(f"发布运行锁失败：{exc}") from exc
            # 文件系统不支持硬链接：guard 内目标必然已由上面检查过；用 rename 做
            # 原子发布（完整内容一次可见），绝不退回原地写。
            if os.path.lexists(path):
                raise LockExists("正式锁已被其它实例发布") from exc
            os.rename(tmp, path)
    except LockExists:
        raise
    except LockPublishError:
        raise
    except OSError as exc:
        raise LockPublishError(f"写运行锁临时文件失败：{exc}") from exc
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def archive_stale_lock(data_root: str, raw: str | None) -> tuple[bool, str]:
    """归档当前正式锁；返回 (ok, archived_path 或错误信息)。"""
    directory = lock_archive_dir(data_root)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(
        directory,
        f"stale-{int(time.time() * 1000)}-{os.getpid()}-{secrets.token_hex(3)}.lock.json")
    try:
        os.rename(runtime_lock_path(data_root), target)
    except FileNotFoundError:
        return False, "陈旧锁已被其它进程处理"
    except OSError as exc:
        return False, f"归档陈旧运行锁失败：{exc}"
    try:
        with open(target, "r", encoding="utf-8") as handle:
            archived = handle.read()
    except OSError:
        archived = None
    if raw is not None and archived != raw:
        try:
            if not os.path.lexists(runtime_lock_path(data_root)):
                os.rename(target, runtime_lock_path(data_root))
        except OSError:
            pass
        return False, "归档时锁内容已变化，重试"
    return True, target


def migration_reservation_root(data_root: str) -> str:
    """Stable sibling: unlike the data directory, this path is never renamed."""
    root = os.path.abspath(data_root)
    return os.path.join(os.path.dirname(root), f".{os.path.basename(root)}.pawbot-migration")


def _migration_conflict(data_root: str, owner_pid: int) -> dict | None:
    present, lock, _raw, error = read_lock(migration_reservation_root(data_root))
    if not present:
        return None
    if lock is None:
        return {"ok": False, "code": "LOCK_UNKNOWN", "lock": None, "archived": [],
                "message": f"迁移预约状态未知：{error}"}
    if lock["pid"] == owner_pid:  # The same Node owner reserves source and destination.
        return None
    alive = pid_alive(lock["pid"])
    if alive is False:
        return None
    return {"ok": False, "code": "LOCKED" if alive else "LOCK_UNKNOWN",
            "lock": lock, "archived": [], "message": "数据目录正在迁移，拒绝启动"}


# ------------------------------------------------------------------ 获取/释放
def acquire(data_root: str, *, owner_pid: int, host_pid: int = 0, version: str = "",
            nonce: str = "", started_at: str = "", timeout: float = 5.0,
            is_alive: Callable[[Any], bool | None] | None = None,
            probe: dict | None = None) -> dict:
    """在 guard 内完成 read/decide/archive/publish。

    返回 {"ok":bool, "code":str|None, "message":str, "lock":dict|None,
          "archived":list[str]}；不抛（GuardUnavailable/Timeout 也转成 code）。
    """
    probe = probe if isinstance(probe, dict) else None
    result: dict = {"ok": False, "code": None, "message": "", "lock": None,
                    "archived": []}
    try:
        owner = int(owner_pid)
    except (TypeError, ValueError):
        owner = 0
    if owner <= 0:
        result.update(code="LOCK_CREATE_FAILED", message=f"owner pid 非法：{owner_pid}")
        return result
    try:
        conflict = _migration_conflict(data_root, owner)
        if conflict:
            return conflict
        # Serialize the stable-marker decision with its publication. Without
        # this guard, a startup paused after its first check could recreate the
        # source directory after migration renamed it, even if later rejected.
        with guard_lock(migration_reservation_root(data_root), timeout=timeout):
            conflict = _migration_conflict(data_root, owner)
            if conflict:
                return conflict
            with guard_lock(data_root, timeout=timeout):
                return _acquire_locked(
                    data_root, owner_pid=owner, host_pid=host_pid, version=version,
                    nonce=nonce, started_at=started_at, is_alive=is_alive, probe=probe)
    except GuardTimeout as exc:
        result.update(code="LOCK_RACE", message=str(exc))
        return result
    except GuardUnavailable as exc:
        result.update(code="LOCK_GUARD_FAILED", message=str(exc))
        return result
    except Exception as exc:  # noqa: BLE001 —— 未预期失败一律 fail-closed
        result.update(code="LOCK_GUARD_FAILED",
                      message=f"抢锁异常：{type(exc).__name__}: {exc}")
        return result


def _acquire_locked(data_root: str, *, owner_pid: int, host_pid: int, version: str,
                    nonce: str, started_at: str,
                    is_alive: Callable[[Any], bool | None] | None,
                    probe: dict | None) -> dict:
    probe_fn = is_alive or pid_alive
    result: dict = {"ok": False, "code": None, "message": "", "lock": None,
                    "archived": []}
    for _attempt in range(16):
        present, lock, raw, error = read_lock(data_root)
        if present and lock is not None:
            alive = _probe_owner(lock, raw, probe_fn, probe)
            if alive is True:
                result.update(code="LOCKED", lock=lock, message=(
                    f"数据目录已被 PawBot 占用（pid={lock['pid']}，"
                    f"version={lock.get('version') or '?'}）"))
                return result
            if alive is not False:
                result.update(code="LOCK_UNKNOWN", lock=lock, message=(
                    f"无法确认运行锁持有者 pid={lock['pid']} 是否存活；"
                    "按 fail-closed 拒绝启动"))
                return result
            ok, detail = archive_stale_lock(data_root, raw)
            if ok:
                result["archived"].append(detail)
                continue
            if detail.startswith("陈旧锁已被") or detail.startswith("归档时锁内容"):
                continue
            result.update(code="LOCK_UNKNOWN", lock=lock, message=detail)
            return result
        if present and lock is None:
            result.update(code="LOCK_UNREADABLE", message=(
                f"{error or '运行锁无法解析'}；为避免双实例拒绝启动"
                f"（确认没有 PawBot 在跑后可手动删除 {runtime_lock_path(data_root)}）"))
            return result

        value = {
            "pid": int(owner_pid),
            "started_at": str(started_at or now_iso()),
            "host_pid": max(0, int(host_pid or 0)),
            "version": str(version or ""),
            "nonce": str(nonce or new_nonce()),
        }
        try:
            publish_lock(data_root, json.dumps(value, ensure_ascii=False, indent=2))
        except LockExists:
            continue
        except LockPublishError as exc:
            result.update(code="LOCK_CREATE_FAILED", message=str(exc))
            return result
        result.update(ok=True, code=None, message="", lock=value)
        return result
    result.update(code="LOCK_RACE", message="并发抢锁未完成，请重试")
    return result


def _probe_owner(lock: dict, raw: str | None,
                 probe_fn: Callable[[Any], bool | None],
                 probe: dict | None) -> bool | None:
    """先认 Node 侧对**同一份 raw** 的探查结果，再回退到本进程探测。"""
    if probe and raw is not None:
        digest = probe.get("sha256")
        if isinstance(digest, str) and digest:
            import hashlib

            actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if actual == digest:
                value = probe.get("alive")
                if value is True or value is False or value is None:
                    return value
    try:
        return probe_fn(lock.get("pid"))
    except Exception:  # noqa: BLE001 —— 探测异常 = 未知
        return None


def release(data_root: str, *, owner_pid: int, nonce: str,
            timeout: float = 5.0) -> dict:
    """在 guard 内核对 pid+nonce 后删除正式锁。"""
    result = {"ok": False, "removed": False, "code": None, "message": ""}
    try:
        with guard_lock(data_root, timeout=timeout):
            present, current, _raw, error = read_lock(data_root)
            if not present:
                result.update(ok=True, removed=False)
                return result
            if current is None:
                result.update(code="LOCK_UNREADABLE",
                              message=f"运行锁无法解析，未删除：{error}")
                return result
            if current.get("pid") != int(owner_pid) or current.get("nonce") != str(nonce or ""):
                result.update(code="NONCE_MISMATCH", message=(
                    "运行锁已不属于当前进程（pid/nonce 不匹配），拒绝释放"))
                return result
            try:
                os.unlink(runtime_lock_path(data_root))
            except FileNotFoundError:
                result.update(ok=True, removed=False)
                return result
            except OSError as exc:
                result.update(code="RELEASE_FAILED", message=f"删除运行锁失败：{exc}")
                return result
            result.update(ok=True, removed=True)
            return result
    except GuardTimeout as exc:
        result.update(code="LOCK_RACE", message=str(exc))
        return result
    except GuardUnavailable as exc:
        result.update(code="LOCK_GUARD_FAILED", message=str(exc))
        return result
    except Exception as exc:  # noqa: BLE001 —— 释放失败必须让调用方知道
        result.update(code="RELEASE_FAILED",
                      message=f"释放运行锁异常：{type(exc).__name__}: {exc}")
        return result


# ------------------------------------------------------------------ CLI（Node 侧入口）
def _cli() -> int:
    op = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        request = json.loads(sys.stdin.read() or "{}")
        if not isinstance(request, dict):
            raise ValueError("request 必须是 JSON 对象")
        data_root = str(request.get("data_root") or "")
        timeout = float(request.get("timeout_ms") or 5000) / 1000.0
        if op == "acquire":
            owner = request.get("owner") or {}
            result = acquire(
                data_root,
                owner_pid=int(owner.get("pid") or 0),
                host_pid=int(owner.get("host_pid") or 0),
                version=str(owner.get("version") or ""),
                nonce=str(owner.get("nonce") or ""),
                started_at=str(owner.get("started_at") or ""),
                timeout=timeout,
                probe=request.get("probe") if isinstance(request.get("probe"), dict) else None,
            )
        elif op == "release":
            expected = request.get("expected") or {}
            result = release(
                data_root,
                owner_pid=int(expected.get("pid") or 0),
                nonce=str(expected.get("nonce") or ""),
                timeout=timeout,
            )
        else:
            result = {"ok": False, "code": "BAD_OP", "message": f"未知操作：{op}"}
    except Exception as exc:  # noqa: BLE001 —— 结构化失败，绝不把 traceback 当协议输出
        result = {"ok": False, "code": "HELPER_ERROR",
                  "message": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
