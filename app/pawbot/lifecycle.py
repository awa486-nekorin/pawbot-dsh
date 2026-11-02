"""运行时生命周期（批次 2）：数据根解析、单实例锁、控制面状态、停机 flush 与事件闸门。

这个模块是「批次 2 的接缝」，不改 app/pawbot 的业务语义：
  * DataRoot：优先 `PAWBOT_DATA_DIR`，否则 `<BASE_DIR>/data`；外部数据根在
    `<BASE_DIR>/data` 不存在时建立目录联接，保证 Python 看到的 `base_dir/data`
    与 Host 解析出的数据根是同一个真实路径；不一致就 fail-closed，绝不双端分裂。
  * RuntimeLock：`<dataRoot>/.runtime.lock`，JSON 字段与 Node 侧
    `lib/instance-lock.js` 完全一致；运行锁由**真实 Python 进程**持有，
    Host 不持锁（py.exe launcher 的 pid 与 python.exe 不同）。
  * RuntimeLifecycle：给 WebUI 控制面提供 ready/shutdown，负责停事件、取消进行中
    任务、真实 flush Store 并校验结果、延迟发出停机信号；run.py 负责最终清理与退出码。

安全：控制 token 只从环境变量读，不写日志、不进回包；本模块不打印 token。
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import os
import secrets
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from . import runtime_lock

# 退出码语义（run.py / Host 共同约定）
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DATA_ROOT = 2
EXIT_LOCKED = 3
EXIT_LOCK_UNKNOWN = 4
EXIT_CONTROL_SHUTDOWN = 10

DATA_ROOT_ENV = "PAWBOT_DATA_DIR"
CONTROL_TOKEN_ENV = "PAWBOT_CONTROL_TOKEN"
HOST_PID_ENV = "PAWBOT_HOST_PID"
RUNTIME_VERSION_ENV = "PAWBOT_RUNTIME_VERSION"

RUNTIME_LOCK_FILE = runtime_lock.RUNTIME_LOCK_FILE
LOCK_ARCHIVE_DIR = runtime_lock.LOCK_ARCHIVE_DIR
LOCK_GUARD_FILE = runtime_lock.LOCK_GUARD_FILE
LOCK_FIELDS = runtime_lock.LOCK_FIELDS
LOCK_NONCE_BYTES = runtime_lock.LOCK_NONCE_BYTES


class DataRootError(RuntimeError):
    """数据根无法安全确定（不一致 / 无法建立联接）。"""


class RuntimeLocked(RuntimeError):
    """运行锁被一个活着的进程持有。"""

    def __init__(self, message: str, lock: dict | None = None):
        super().__init__(message)
        self.lock = lock or {}


class RuntimeLockUnknown(RuntimeError):
    """无法判断运行锁状态；按 fail-closed 拒绝。"""

    def __init__(self, message: str, lock: dict | None = None):
        super().__init__(message)
        self.lock = lock or {}


@dataclass(frozen=True)
class DataRoot:
    root: str
    app_data: str
    source: str = "default"
    external: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def paths(self) -> dict[str, str]:
        root = self.root
        return {
            "root": root,
            "config": os.path.join(root, "config.json"),
            "persona": os.path.join(root, "persona.md"),
            "memory": os.path.join(root, "memory"),
            "backups": os.path.join(root, "backups"),
            "brain": os.path.join(root, "brain"),
            "napcat": os.path.join(root, "napcat"),
            "logs": os.path.join(root, "logs"),
            "authz": os.path.join(root, "authz"),
            "sessions": os.path.join(root, "sessions"),
            "tmp": os.path.join(root, "tmp"),
            "runtime_lock": runtime_lock_path(root),
        }


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(str(path)))


def _is_link(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        return bool(getattr(os.path, "isjunction", lambda _p: False)(path))
    except OSError:
        return False


def _create_dir_alias(link_path: str, target: str) -> None:
    """在 link_path 建一个指向 target 的目录联接（Windows /J，POSIX symlink）。"""
    parent = os.path.dirname(link_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    os.makedirs(target, exist_ok=True)
    if os.path.lexists(link_path):
        raise DataRootError(f"{link_path} 已存在，不能再建立数据根联接")
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", link_path, target],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DataRootError(f"建立目录联接失败：{type(exc).__name__}: {exc}") from exc
        if result.returncode != 0 or not os.path.exists(link_path):
            detail = (result.stderr or result.stdout or "").strip()
            raise DataRootError(f"建立目录联接失败（{link_path} → {target}）：{detail}")
        return
    try:
        os.symlink(target, link_path, target_is_directory=True)
    except OSError as exc:
        raise DataRootError(f"建立目录符号链接失败（{link_path} → {target}）：{exc}") from exc


def resolve_data_root(base_dir: str, env: dict | None = None) -> DataRoot:
    """解析 Python 侧数据根，并保证 `<base>/data` 实际指向同一个根。"""
    env = os.environ if env is None else env
    base = os.path.abspath(str(base_dir))
    app_data = os.path.join(base, "data")
    raw = str(env.get(DATA_ROOT_ENV, "") or "").strip()
    if not raw:
        return DataRoot(root=app_data, app_data=app_data, source="default")
    requested = os.path.abspath(os.path.expanduser(raw))
    if _real(requested) == _real(app_data):
        return DataRoot(root=app_data, app_data=app_data, source="env")
    actual_root, actual_data = os.path.normcase(_real(requested)), os.path.normcase(_real(app_data))
    try:
        common = os.path.commonpath((actual_root, actual_data))
    except ValueError:  # Different Windows drives cannot overlap.
        common = None
    if common in (actual_root, actual_data):
        raise DataRootError("数据根与 app/data 存在祖先或后代关系，拒绝建立循环联接")
    if _is_link(app_data):
        target = _real(app_data)
        raise DataRootError(
            f"{DATA_ROOT_ENV}={requested} 与 {app_data} 的实际联接目标 {target} 不一致；"
            "为避免双端分裂，拒绝启动。请先修正数据目录或目录联接。"
        )
    if os.path.exists(app_data):
        raise DataRootError(
            f"{DATA_ROOT_ENV}={requested} 与 {app_data} 不一致，且后者是真实目录；"
            "为避免双端分裂，拒绝启动。请用 DSH 数据守卫迁移，或手动把 app/data "
            "换成指向该数据根的目录联接。"
        )
    _create_dir_alias(app_data, requested)
    if _real(app_data) != _real(requested):
        raise DataRootError(f"目录联接已建立但解析结果不一致：{app_data} → {_real(app_data)}")
    return DataRoot(
        root=requested, app_data=app_data, source="env", external=True,
        warnings=(f"已为外部数据根建立目录联接：{app_data} → {requested}",),
    )


# ---------------------------------------------------------------- 进程存活 / 运行锁
# 互斥算法只有一份：app/pawbot/runtime_lock.py（OS advisory lock 锁固定 guard
# 文件；正式锁用 temp+link/rename 原子发布）。这里只做生命周期语义适配。
def pid_alive(pid) -> bool | None:
    return runtime_lock.pid_alive(pid)


def runtime_lock_path(data_root: str) -> str:
    return runtime_lock.runtime_lock_path(data_root)


def lock_archive_dir(data_root: str) -> str:
    return runtime_lock.lock_archive_dir(data_root)


def lock_guard_path(data_root: str) -> str:
    return runtime_lock.lock_guard_path(data_root)


def guard_lock(data_root: str, timeout: float = 5.0):
    """公开 guard 入口：同一 guard 可跨 Node helper / Python 进程互斥。"""
    return runtime_lock.guard_lock(data_root, timeout=timeout)


def read_runtime_lock(data_root: str) -> dict | None:
    """只读读取；锁不存在或损坏返回 None。"""
    return runtime_lock.read_lock(data_root)[1]


def inspect_runtime_lock(data_root: str,
                         is_alive: Callable[[int], bool | None] | None = None) -> dict:
    return runtime_lock.inspect_lock(data_root, is_alive=is_alive)


def _host_pid_value(host_pid: int) -> int:
    try:
        value = int(host_pid or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def acquire_runtime_lock(
    data_root: str,
    *,
    host_pid: int = 0,
    version: str = "",
    nonce: str | None = None,
    is_alive: Callable[[int], bool | None] | None = None,
    timeout: float = 5.0,
) -> dict:
    """获取运行锁；活 pid → RuntimeLocked，未知/损坏 → RuntimeLockUnknown。"""
    result = runtime_lock.acquire(
        os.path.abspath(str(data_root or ".")),
        owner_pid=os.getpid(),
        host_pid=_host_pid_value(host_pid),
        version=str(version or ""),
        nonce=str(nonce or runtime_lock.new_nonce()),
        started_at=runtime_lock.now_iso(),
        timeout=timeout,
        is_alive=is_alive,
    )
    if result.get("ok"):
        return result["lock"]
    code = str(result.get("code") or "")
    message = str(result.get("message") or "抢锁失败")
    lock = result.get("lock")
    if code == "LOCKED":
        raise RuntimeLocked(message, lock)
    raise RuntimeLockUnknown(message, lock)


def release_runtime_lock(data_root: str, lock: dict | None) -> bool:
    """释放运行锁；核对 pid+nonce。锁不存在视为已释放成功。"""
    expected = lock or {}
    try:
        expected_pid = int(expected.get("pid") or 0)
    except (TypeError, ValueError):
        expected_pid = 0
    if expected_pid != os.getpid():
        return False
    result = runtime_lock.release(
        os.path.abspath(str(data_root or ".")),
        owner_pid=expected_pid,
        nonce=str(expected.get("nonce") or ""),
        timeout=3.0,
    )
    return bool(result.get("ok"))


# ---------------------------------------------------------------- 控制面
def control_token_from_env(env: dict | None = None) -> str:
    """控制 token 只来自 env；空/空白 = 控制面关闭。"""
    env = os.environ if env is None else env
    return str(env.get(CONTROL_TOKEN_ENV, "") or "").strip()


def take_control_token(env: dict | None = None) -> str:
    """取出控制 token 并立刻从 env 删除，避免业务子进程继承（参考 brain.py）。"""
    source = os.environ if env is None else env
    token = control_token_from_env(source)
    try:
        source.pop(CONTROL_TOKEN_ENV, None)
    except Exception:  # noqa: BLE001 —— pop 失败不影响启动
        pass
    return token


def peer_is_loopback(peername: Any) -> bool:
    """真实 loopback 判定（只看 socket peer，不看 Header/查询参数）。"""
    if not peername or not isinstance(peername, (tuple, list)) or not peername:
        return False
    host = str(peername[0] or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_loopback)


class RuntimeLifecycle:
    """把数据根、运行锁、控制面状态和停机顺序收在一个生命周期对象里。"""

    def __init__(self, *, data_root: str, lock: dict | None = None,
                 control_token: str = "", version: str = "", host_pid: int = 0,
                 exit_delay: float = 0.2):
        self.data_root = os.path.abspath(str(data_root))
        self.lock = lock
        self.control_token = str(control_token or "")
        self.version = str(version or "")
        self.host_pid = int(host_pid or 0)
        self.exit_delay = max(0.0, float(exit_delay))
        self.accepting = True
        self.shutting_down = False
        self.exit_code = EXIT_OK
        self.stop_event = asyncio.Event()
        self._bot: Any = None
        self._webui: Any = None
        self._original_on_event: Any = None
        self._shutdown_mutex = asyncio.Lock()
        self._shutdown_result: dict | None = None
        self._exit_scheduled = False
        self._cleanup_done = False
        # relay 模式：request_shutdown 已经按 core 既有顺序停过 Bot（含传输释放），
        # run.py 的兜底收尾不得再 stop 一次（避免重复 before_release/bye）。
        self._bot_stopped = False

    # -- 接线
    def bind_bot(self, bot: Any) -> "RuntimeLifecycle":
        self._bot = bot
        return self

    def bind_webui(self, webui: Any) -> "RuntimeLifecycle":
        """登记 WebUI：停机开始时先让它拒绝新变更，并把在途请求交给取消/等待。"""
        self._webui = webui
        return self

    @property
    def bot_stopped(self) -> bool:
        """relay 停机是否已由 request_shutdown 按 core 顺序停过 Bot。"""
        return bool(self._bot_stopped)

    def install_event_gate(self, bot: Any | None = None) -> bool:
        """停机开始后不再处理新事件（OneBot worker 仍可正常结束旧事件）。"""
        target = bot if bot is not None else self._bot
        server = getattr(target, "server", None)
        original = getattr(server, "on_event", None)
        if not callable(original):
            return False
        self._original_on_event = original

        async def _gated(conn, data):
            if not self.accepting:
                return None
            return await original(conn, data)

        server.on_event = _gated
        return True

    # -- 状态
    @property
    def store(self):
        return getattr(self._bot, "store", None) if self._bot is not None else None

    def store_dirty(self) -> bool:
        store = self.store
        if store is None:
            return True
        return bool(getattr(store, "dirty", False))

    def probe_db_writable(self) -> tuple[bool, str]:
        """真的写探针验证「数据根 tmp/ + Store 目标」都可写、且 Store 可被替换。

        只写 tmp/ 不够：Store 文件本身只读（或目录不允许替换）时，停机 flush 会失败，
        ready 必须提前说 false，而不是等 shutdown 才暴露。
        """
        if not os.path.isdir(self.data_root):
            return False, "数据根不存在"
        tmp_dir = os.path.join(self.data_root, "tmp")
        path = ""
        try:
            os.makedirs(tmp_dir, exist_ok=True)
            path = os.path.join(
                tmp_dir, f".ready-probe-{os.getpid()}-{secrets.token_hex(4)}")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("ok")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return self._probe_store_replaceable()

    def _probe_store_replaceable(self) -> tuple[bool, str]:
        """Store 文件存在 → 必须能以写方式打开；目标目录必须允许 replace。"""
        store = self.store
        store_path = str(getattr(store, "path", "") or "")
        if not store_path:
            return True, ""
        store_path = os.path.abspath(store_path)
        parent = os.path.dirname(store_path) or "."
        src = ""
        dst = ""
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            return False, f"Store 目录不存在且无法创建：{type(exc).__name__}: {exc}"
        try:
            if os.path.exists(store_path):
                if not os.path.isfile(store_path):
                    return False, "Store 目标不是普通文件"
                # 只以 O_WRONLY 打开再关闭，不截断、不写入；只读文件在 Windows 会 EACCES。
                fd = os.open(store_path, os.O_WRONLY)
                os.close(fd)
            else:
                # 目标还不存在：至少验证同目录可创建。
                probe = os.path.join(
                    parent, f".ready-store-probe-{os.getpid()}-{secrets.token_hex(4)}")
                with open(probe, "w", encoding="utf-8") as handle:
                    handle.write("ok")
                os.unlink(probe)
            # 验 replace 语义（flush 用 os.replace 原子替换 Store）。
            tag = f"{os.getpid()}-{secrets.token_hex(4)}"
            src = os.path.join(parent, f".ready-replace-src-{tag}")
            dst = os.path.join(parent, f".ready-replace-dst-{tag}")
            with open(src, "w", encoding="utf-8") as handle:
                handle.write("ok")
            with open(dst, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.replace(src, dst)
            return True, ""
        except OSError as exc:
            return False, f"Store 不可替换：{type(exc).__name__}: {exc}"
        finally:
            for item in (src, dst):
                if item:
                    try:
                        os.unlink(item)
                    except OSError:
                        pass

    def ready_state(self) -> dict:
        # db_writable 始终如实探测（即使正在停机）——ready 才是「接不接新活」的判定。
        db_ok, db_error = self.probe_db_writable()
        store = self.store
        dirty = self.store_dirty()
        ready = bool(self.accepting and not self.shutting_down and db_ok and store is not None)
        payload = {
            "ok": True,
            "ready": ready,
            "db_writable": bool(db_ok),
            "store_dirty": dirty,
            "shutting_down": bool(self.shutting_down),
            "pid": os.getpid(),
        }
        if not db_ok and db_error:
            payload["db_error"] = db_error
        return payload

    # -- 停机
    def flush_store(self) -> dict:
        """真实 flush Store 并验证：dirty 清零、文件存在且 JSON 可解析。"""
        store = self.store
        if store is None:
            return {"ok": False, "flushed": False, "error": "store 不可用"}
        try:
            store.flush(force=True)
            if bool(getattr(store, "dirty", False)):
                return {"ok": False, "flushed": False, "error": "flush 后 Store 仍标记为 dirty"}
            path = str(getattr(store, "path", "") or "")
            if not path or not os.path.isfile(path):
                return {"ok": False, "flushed": False, "error": "flush 后 Store 文件不存在"}
            with open(path, "r", encoding="utf-8") as handle:
                json.load(handle)
            return {"ok": True, "flushed": True, "error": None}
        except Exception as exc:  # noqa: BLE001 —— 任何失败都必须如实返回
            return {"ok": False, "flushed": False, "error": f"{type(exc).__name__}: {exc}"}

    async def _begin_webui_draining(self) -> tuple[bool, list[str]]:
        """停机开始：让 WebUI 立刻拒绝新的变更类请求（控制面重复 ack 仍可用）。"""
        ok = True
        notes: list[str] = []
        webui = self._webui
        if webui is not None:
            try:
                begin = getattr(webui, "begin_draining", None)
                if callable(begin):
                    begin()
            except Exception as exc:  # noqa: BLE001
                ok = False
                notes.append(f"WebUI 停止接收新变更失败：{type(exc).__name__}: {exc}")
        return ok, notes

    async def _stop_background_threads(self, timeout: float = 3.0) -> tuple[bool, list[str]]:
        """停掉仍会写 Store 的同步线程；线程没停就如实失败（不能发成功 ack）。"""
        ok = True
        notes: list[str] = []
        bot = self._bot
        if bot is None:
            return ok, notes
        syncer = getattr(bot, "realtime_sync", None)
        if syncer is not None:
            try:
                await asyncio.to_thread(syncer.stop, timeout)
                if syncer.is_alive():
                    ok = False
                    notes.append("实时同步线程未在时限内停止")
            except Exception as exc:  # noqa: BLE001
                ok = False
                notes.append(f"实时同步停止失败：{type(exc).__name__}: {exc}")
        auto_pull = getattr(bot, "_auto_pull_thread", None)
        if auto_pull is not None:
            try:
                await asyncio.to_thread(auto_pull.join, timeout)
                if auto_pull.is_alive():
                    ok = False
                    notes.append("启动同步线程未在时限内停止")
            except Exception as exc:  # noqa: BLE001
                ok = False
                notes.append(f"启动同步等待失败：{type(exc).__name__}: {exc}")
        return ok, notes

    async def _quiesce_producers(self, timeout: float = 3.0) -> tuple[bool, list[str]]:
        """legacy 停机：停机 ack 之前先停掉会继续写 Store / 发外网的周期生产者。

        - WebUI 立刻进入 draining（拒绝新的变更类请求）；
        - OneBot 监听与连接关闭，不再接收新事件；
        - 取消已知的周期任务（store flush / B 站调度），停 proactive / reminders。
        任何一步失败都如实记录，ack 不能当成功。
        """
        ok, notes = await self._begin_webui_draining()
        bot = self._bot
        if bot is None:
            return ok, notes
        server = getattr(bot, "server", None)
        stop_server = getattr(server, "stop", None)
        if callable(stop_server):
            try:
                await asyncio.wait_for(stop_server(), timeout=timeout)
            except asyncio.TimeoutError:
                ok = False
                notes.append("关闭 OneBot 监听超时")
            except Exception as exc:  # noqa: BLE001
                ok = False
                notes.append(f"关闭 OneBot 监听失败：{type(exc).__name__}: {exc}")
        loop_tasks = []
        for name in ("_store_flush_task", "_bili_scroll_task", "_bili_post_task"):
            task = getattr(bot, name, None)
            if isinstance(task, asyncio.Task) and not task.done():
                loop_tasks.append(task)
        for task in loop_tasks:
            task.cancel()
        if loop_tasks:
            try:
                await asyncio.wait(loop_tasks, timeout=max(0.1,float(timeout)))
            except Exception:  # noqa: BLE001
                pass
            if any(not task.done() for task in loop_tasks):
                ok = False
                notes.append("周期任务未在时限内停止")
        for name in ("proactive", "reminder_service"):
            component = getattr(bot, name, None)
            stop_component = getattr(component, "stop", None)
            if not callable(stop_component):
                continue
            try:
                await asyncio.wait_for(stop_component(), timeout=timeout)
            except asyncio.TimeoutError:
                ok = False
                notes.append(f"{name} 停止超时")
            except Exception as exc:  # noqa: BLE001
                ok = False
                notes.append(f"{name} 停止失败：{type(exc).__name__}: {exc}")
        thread_ok, thread_notes = await self._stop_background_threads(timeout)
        ok = ok and thread_ok
        notes.extend(thread_notes)
        return ok, notes

    def _relay_transport(self) -> Any | None:
        """识别 relay 模式传输；legacy（reverse/forward）一律返回 None，行为不变。

        判定用 core 已有的公开契约 ``server.mode == "relay"`` 且挂了
        ``bot.ha_runtime``，不依赖具体类，也不碰 core/relay_client。
        """
        bot = self._bot
        if bot is None or getattr(bot, "ha_runtime", None) is None:
            return None
        server = getattr(bot, "server", None)
        if server is None:
            return None
        if str(getattr(server, "mode", "") or "").lower() == "relay":
            return server
        return None

    async def _release_relay_transport(self, server: Any) -> tuple[bool, list[str]]:
        """relay 模式：在取消任何任务之前，按 core 既有顺序释放传输。

        ``Bot.stop``（relay 分支）会先关业务入口、再 ``RelayClient.stop``：
        ``before_release``（停生产者/排空/flush 内容/最后一次有权推送）→ bye →
        ``HARuntime.stop`` → 本机 state 落盘。这里复用同一条顺序，并临时观察
        ``before_release`` 的返回值，把「最终推送失败」如实带进 ack/退出码。
        """
        bot = self._bot
        notes: list[str] = []
        captured: dict[str, Any] = {}
        original = getattr(server, "before_release", None)
        wrapped = False
        if callable(original):
            async def _recording(*args, **kwargs):
                try:
                    result = original(*args, **kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                except BaseException as exc:  # noqa: BLE001 - 记录后照常抛出
                    captured["error"] = exc
                    raise
                captured["result"] = result
                return result
            try:
                server.before_release = _recording
                wrapped = True
            except Exception as exc:  # noqa: BLE001 - 只读替身也允许，但不能假装看到了结果
                notes.append(f"无法观察 relay 释放结果：{type(exc).__name__}: {exc}")
        ok = True
        try:
            stopper = getattr(bot, "stop", None) if bot is not None else None
            if callable(stopper):
                await stopper()
                self._bot_stopped = True
            else:
                stop_server = getattr(server, "stop", None)
                if not callable(stop_server):
                    return False, notes + ["relay 传输没有可用的 stop()"]
                await stop_server()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 释放失败必须进 ack，而不是静默
            ok = False
            notes.append(f"停止 relay 传输失败：{type(exc).__name__}: {exc}")
        finally:
            if wrapped:
                try:
                    server.before_release = original
                except Exception:  # noqa: BLE001
                    pass
        error = captured.get("error")
        result = captured.get("result")
        if error is not None:
            ok = False
            notes.append(
                f"relay 释放前内容 flush 失败：{type(error).__name__}: {error}")
        elif isinstance(result, dict) and not result.get("ok"):
            detail = result.get("error") or result.get("code") or "未排空或推送未成功"
            ok = False
            notes.append(f"relay 释放前内容 flush/push 未成功：{detail}")
        return ok, notes

    def _collect_inflight(self) -> list[asyncio.Task]:
        """收集当前所有可写/外发的在途任务（不取消、不等待）。

        必须在停 OneBot/停止周期生产者**之前**先快照一次：server.stop() 会清空
        connections，断开后再去找 worker 就找不到了（S1 回归）。
        """
        current = asyncio.current_task()
        victims: list[asyncio.Task] = []
        try:
            from . import tasks as tasks_mod
            victims.extend(task for task in tasks_mod.pending()
                           if task is not current and not task.done())
        except Exception:  # noqa: BLE001
            pass
        server = getattr(self._bot, "server", None)
        connections = getattr(server, "connections", {}) if server is not None else {}
        try:
            for conn in list(connections.values()):
                worker = getattr(conn, "worker", None)
                if worker is not None and worker is not current and not worker.done():
                    victims.append(worker)
        except Exception:  # noqa: BLE001
            pass
        webui = self._webui
        active_requests = getattr(webui, "active_requests", None) if webui is not None else None
        if callable(active_requests):
            try:
                victims.extend(task for task in active_requests()
                               if task is not current and not task.done())
            except Exception:  # noqa: BLE001
                pass
        for name in ("_store_flush_task", "_bili_scroll_task", "_bili_post_task"):
            task = getattr(self._bot, name, None)
            if isinstance(task, asyncio.Task) and task is not current and not task.done():
                victims.append(task)
        seen: set[int] = set()
        unique: list[asyncio.Task] = []
        for task in victims:
            if id(task) in seen:
                continue
            seen.add(id(task))
            unique.append(task)
        return unique

    async def _cancel_inflight(self, timeout: float = 2.0,
                               extra: list[asyncio.Task] | None = None) -> list[asyncio.Task]:
        """取消并等待进行中的任务；返回超时后仍未结束的任务（非空 = 不能发成功 ack）。"""
        current = asyncio.current_task()
        victims: list[asyncio.Task] = [
            task for task in (extra or [])
            if task is not current and not task.done()
        ]
        victims.extend(self._collect_inflight())
        seen: set[int] = set()
        unique: list[asyncio.Task] = []
        for task in victims:
            if id(task) in seen:
                continue
            seen.add(id(task))
            unique.append(task)
        for task in unique:
            try:
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        if not unique:
            return []
        try:
            _done, pending = await asyncio.wait(unique, timeout=max(0.1, float(timeout)))
        except Exception:  # noqa: BLE001
            return [task for task in unique if not task.done()]
        return [task for task in pending if not task.done()]

    def _schedule_exit(self) -> None:
        if self._exit_scheduled:
            return
        self._exit_scheduled = True
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(self.exit_delay, self.stop_event.set)
        except RuntimeError:
            self.stop_event.set()

    async def request_shutdown(self) -> dict:
        """控制面 shutdown：停新事件/新变更 → 等待在途任务收尾 → 真实 flush → 延迟退出。

        ack 只有在「所有可写任务确实结束 + flush 成功」时才是 ok/flushed:true；
        取消后仍有 pending、或周期生产者停不下来，就失败（WebUI 回 500，退出码非 0/10）。
        """
        async with self._shutdown_mutex:
            if self._shutdown_result is not None:
                return {**self._shutdown_result, "already_shutting_down": True}
            self.accepting = False
            self.shutting_down = True
            # 先快照在途任务：停 OneBot/周期任务会清掉 connections，之后再收集就漏了。
            early = self._collect_inflight()
            relay = self._relay_transport()
            if relay is not None:
                # relay 模式：必须先按 core 既有顺序释放传输（Bot.stop → RelayClient.stop
                # → before_release：停生产者/排空业务/flush 内容/最后一次有权推送 → bye），
                # 之后才允许取消任何剩余任务。先取消 relay runner/heartbeat 会清 lease，
                # 让最后一次授权推送被跳过；这里也不做全局 drain。
                ok, notes = await self._begin_webui_draining()
                thread_ok, thread_notes = await self._stop_background_threads()
                release_ok, release_notes = await self._release_relay_transport(relay)
                producer_ok = bool(ok and thread_ok and release_ok)
                notes = [*notes, *thread_notes, *release_notes]
            else:
                producer_ok, notes = await self._quiesce_producers()
            pending = await self._cancel_inflight(extra=early)
            result = self.flush_store()
            ok = bool(result.get("ok")) and producer_ok and not pending
            error = result.get("error")
            if pending:
                error = f"{len(pending)} 个任务在取消后仍未结束，拒绝发成功 ack"
            elif not producer_ok:
                error = "；".join(notes) or "周期任务未安全停止"
            payload = {
                "ok": ok,
                "flushed": bool(result.get("flushed")) and ok,
                "error": None if ok else error,
                "pending_tasks": len(pending),
            }
            self.exit_code = EXIT_CONTROL_SHUTDOWN if ok else EXIT_ERROR
            self._shutdown_result = dict(payload)
            self._schedule_exit()
            return {**payload, "already_shutting_down": False}

    async def wait_stopped(self) -> None:
        await self.stop_event.wait()

    def signal_stop(self, exit_code: int | None = None) -> None:
        """本地信号（Ctrl+C / SIGTERM）触发；不覆盖控制面已经定好的退出码。"""
        if exit_code is not None and self.exit_code == EXIT_OK:
            self.exit_code = int(exit_code)
        self.stop_event.set()

    # -- 退出兜底
    def cleanup(self) -> dict:
        """atexit / finally 兜底：再 force flush 一次并释放锁；幂等、绝不抛。"""
        if self._cleanup_done:
            return {"ok": True, "flushed": False, "skipped": True}
        self._cleanup_done = True
        result = {"ok": True, "flushed": False, "released": False, "error": None}
        try:
            if self.store is not None:
                flushed = self.flush_store()
                result = {**flushed, "released": False, "skipped": False}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "flushed": False, "released": False,
                      "error": f"{type(exc).__name__}: {exc}"}
        try:
            if self.lock is not None:
                released = bool(release_runtime_lock(self.data_root, self.lock))
                result["released"] = released
                if released:
                    self.lock = None
                else:
                    result["ok"] = False
                    result["error"] = result.get("error") or "释放运行锁失败"
            else:
                result["released"] = True
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["released"] = False
            result["error"] = result.get("error") or f"释放运行锁异常：{type(exc).__name__}: {exc}"
        return result
