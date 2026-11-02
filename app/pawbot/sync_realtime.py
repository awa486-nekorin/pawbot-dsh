#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""近实时云同步（可选）：本地文件变化 → 防抖后 push；远端 manifest 变化 → pull / 冲突保留。

和 cloudsync.py 的关系：
  · bundle、加密、解包、备份、冲突策略全部复用 cloudsync，这里只做「什么时候同步」。
  · 不是 Syncthing 那种秒级双向文件系统：默认 2 秒轮询本地、3 秒防抖、30 秒看一次远端；
    这是「够实时」和「不要把 PawBot 写记忆变成上传风暴」之间的折中。

触发条件（config）
  sync.enabled=true 且 sync.mode="realtime" 时，Bot.start() 会拉起这个后台线程。

冲突策略（sync.conflict_policy）
  keep_both（默认）：本地改过 + 远端也变了 → 远端包存到 <DataRoot>/state/sync-conflict-<时间>/，本机不动；
  local：本地优先，忽略远端这次变化（只记日志）；
  remote：远端优先，直接 pull（persona/记忆会覆盖；config 仍按 overwrite_config 决定）。
首次运行（本机还没有 state/.sync-state.json）默认 keep_both：宁可让你确认，也不悄悄覆盖本机人设。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from . import cloudsync
from . import paths as layout
from .log import log

MANIFEST_NAME = "pawbot-sync.manifest.json"
STATE_NAME = ".sync-state.json"
DEFAULT_POLL_SEC = 2.0
DEFAULT_DEBOUNCE_SEC = 3.0
DEFAULT_REMOTE_POLL_SEC = 30.0
MIN_POLL_SEC = 0.2


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _safe_float(value, default: float, floor: float = MIN_POLL_SEC) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out >= floor else default


def _section(config) -> dict:
    return cloudsync.sync_section(config)


def _state_path(base_dir: str, config=None) -> str:
    return layout.resolve(base_dir, config).state(STATE_NAME)


def _load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warn(f"实时同步：状态写不进去（{type(exc).__name__}）")


def local_fingerprint(config, base_dir: str) -> str:
    """把当前要同步的文件内容算成一个指纹（只读，不改任何文件）。"""
    section = _section(config)
    try:
        entries = cloudsync._collect_entries(section, config, base_dir)
    except Exception:
        return ""
    digest = hashlib.sha256()
    for target in sorted(entries):
        blob = entries[target][0]
        digest.update(target.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(blob).digest())
    return digest.hexdigest()


def _remote_manifest(adapter) -> dict | None:
    """远端没有 manifest（老版本/还没 push 过）时返回 None，调用方按「没变化」处理。"""
    try:
        raw = adapter.download(MANIFEST_NAME)
    except Exception:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_conflict(base_dir: str, raw: bytes, manifest: dict, config=None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    out_dir = layout.resolve(base_dir, config).state(f"sync-conflict-{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "remote.bundle.bin"), "wb") as fh:
        fh.write(raw)
    with open(os.path.join(out_dir, "remote.manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    note = os.path.join(out_dir, "README.txt")
    with open(note, "w", encoding="utf-8") as fh:
        fh.write(
            "远端和本机都改过，所以没有自动覆盖。\n"
            "确认远端更好：tools/cloud_sync.py pull --overwrite-config\n"
            "确认本机更好：tools/cloud_sync.py push（然后再把本目录删掉）\n"
        )
    return out_dir


class RealtimeSyncer(threading.Thread):
    """后台线程：本地防抖 push + 远端轮询 pull/冲突。"""

    def __init__(self, config, base_dir: str, *,
                 poll_sec: float | None = None,
                 debounce_sec: float | None = None,
                 remote_poll_sec: float | None = None,
                 on_event=None):
        super().__init__(name="pawbot-realtime-sync", daemon=True)
        self.config = config
        self.base_dir = base_dir
        section = _section(config)
        self.poll_sec = _safe_float(poll_sec if poll_sec is not None else section.get("poll_sec"),
                                    DEFAULT_POLL_SEC)
        self.debounce_sec = _safe_float(debounce_sec if debounce_sec is not None else section.get("debounce_sec"),
                                        DEFAULT_DEBOUNCE_SEC, floor=0.0)
        self.remote_poll_sec = _safe_float(remote_poll_sec if remote_poll_sec is not None else section.get("remote_poll_sec"),
                                           DEFAULT_REMOTE_POLL_SEC)
        self.state_path = _state_path(base_dir, config)
        self.state = _load_state(self.state_path)
        self._stop_event = threading.Event()
        self._pending_fp = ""
        self._pending_since = 0.0
        self._on_event = on_event
        self._last_remote_check = 0.0
        self._last_error = ""

    # ---------------------------------------------------------------- 状态
    def _emit(self, kind: str, text: str) -> None:
        try:
            if callable(self._on_event):
                self._on_event(kind, text)
        except Exception:
            pass

    def _persist(self) -> None:
        self.state["updated_at"] = _now_iso()
        _save_state(self.state_path, self.state)

    def _mark_pushed(self, result: dict, fp: str) -> None:
        self.state["local_fp"] = fp
        self.state["bundle_sha"] = str(result.get("sha256") or "")
        self.state["last_push"] = _now_iso()
        self.state.pop("conflict_sha", None)
        self._persist()

    # ---------------------------------------------------------------- 本地
    def _push_once(self, fp: str) -> None:
        section = _section(self.config)
        adapter = cloudsync.make_adapter(section, self.base_dir)
        result = cloudsync.push(self.config, self.base_dir)
        manifest = {
            "format": "pawbot-sync-manifest",
            "format_version": 1,
            "created_at": _now_iso(),
            "sha256": str(result.get("sha256") or ""),
            "bundle_name": str(result.get("remote_name") or ""),
            "bytes": int(result.get("bytes") or 0),
            "files": int(result.get("files") or 0),
            "encrypted": bool(result.get("encrypted")),
        }
        adapter.upload(json.dumps(manifest, ensure_ascii=False).encode("utf-8"), MANIFEST_NAME)
        self._mark_pushed(result, fp)
        log.info(f"实时同步：已上传（{result.get('files', 0)} 个文件，{result.get('bytes', 0)}B）")
        self._emit("push", f"已上传 {result.get('files', 0)} 个文件")

    def tick_local(self, *, now: float | None = None) -> bool:
        """返回 True 表示这次真的 push 了。"""
        now = time.monotonic() if now is None else now
        section = _section(self.config)
        if not section.get("enabled"):
            return False
        fp = local_fingerprint(self.config, self.base_dir)
        if not fp:
            return False
        if fp == self.state.get("local_fp"):
            self._pending_fp = ""
            return False
        if fp != self._pending_fp:
            self._pending_fp = fp
            self._pending_since = now
            return False
        if now - self._pending_since < self.debounce_sec:
            return False
        try:
            self._push_once(fp)
            return True
        except cloudsync.SyncError as exc:
            self._warn(f"实时同步上传失败（{exc.code}）")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"实时同步上传异常（{type(exc).__name__}）")
        self._pending_since = now  # 失败后等下一轮再试
        return False

    # ---------------------------------------------------------------- 远端
    def tick_remote(self, *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        if self._last_remote_check and now - self._last_remote_check < self.remote_poll_sec:
            return {"checked": False}
        self._last_remote_check = now
        section = _section(self.config)
        if not section.get("enabled"):
            return {"checked": False}
        try:
            adapter = cloudsync.make_adapter(section, self.base_dir)
            manifest = _remote_manifest(adapter)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"实时同步远端检查失败（{type(exc).__name__}）")
            return {"checked": False}
        if not manifest:
            return {"checked": False}
        remote_sha = str(manifest.get("sha256") or "")
        if not remote_sha or remote_sha == self.state.get("bundle_sha"):
            return {"checked": True, "changed": False}
        if self.state.get("conflict_sha") == remote_sha:
            return {"checked": True, "changed": False, "conflict": True}

        fp = local_fingerprint(self.config, self.base_dir)
        local_changed = bool(self.state.get("local_fp")) and fp != self.state.get("local_fp")
        first_run = not self.state.get("bundle_sha")
        policy = str(section.get("conflict_policy") or "keep_both").strip().lower()

        try:
            if first_run and str(section.get("first_run") or "keep_both").lower() != "pull":
                raw = adapter.download(str(manifest.get("bundle_name") or cloudsync.PLAIN_NAME))
                out_dir = _write_conflict(self.base_dir, raw, manifest, self.config)
                self.state["bundle_sha"] = remote_sha
                self.state["conflict_sha"] = remote_sha
                self._persist()
                log.warn(f"实时同步：首次发现远端数据，已存到 {out_dir}，未覆盖本机")
                self._emit("conflict", f"首次同步：远端数据已存到 {out_dir}")
                return {"checked": True, "first_run": True, "conflict": True, "dir": out_dir}
            if local_changed and policy == "local":
                self.state["bundle_sha"] = remote_sha
                self._persist()
                log.info("实时同步：本地优先，忽略远端这次变化")
                return {"checked": True, "ignored": True}
            if local_changed and policy == "keep_both":
                raw = adapter.download(str(manifest.get("bundle_name") or cloudsync.PLAIN_NAME))
                out_dir = _write_conflict(self.base_dir, raw, manifest, self.config)
                self.state["bundle_sha"] = remote_sha
                self.state["conflict_sha"] = remote_sha
                self._persist()
                log.warn(f"实时同步：两边都改过，远端包存到 {out_dir}（本机未动）")
                self._emit("conflict", f"冲突包存到 {out_dir}")
                return {"checked": True, "conflict": True, "dir": out_dir}

            result = cloudsync.pull(self.config, self.base_dir)
            self.state["bundle_sha"] = remote_sha
            self.state["local_fp"] = local_fingerprint(self.config, self.base_dir)
            self.state["last_pull"] = _now_iso()
            self.state.pop("conflict_sha", None)
            self._persist()
            log.info(f"实时同步：已拉取（{result.get('files', 0)} 个文件）")
            self._emit("pull", f"已拉取 {result.get('files', 0)} 个文件")
            return {"checked": True, "pulled": True, "result": result}
        except cloudsync.SyncError as exc:
            self._warn(f"实时同步拉取失败（{exc.code}）")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"实时同步拉取异常（{type(exc).__name__}）")
        return {"checked": True, "error": True}

    def _warn(self, text: str) -> None:
        self._last_error = text
        log.warn(text)
        self._emit("error", text)

    # ---------------------------------------------------------------- 线程
    def run(self) -> None:  # pragma: no cover - 线程循环本身由 start/stop 测试覆盖
        while not self._stop_event.wait(self.poll_sec):
            try:
                self.tick_local()
            except Exception as exc:  # noqa: BLE001
                self._warn(f"实时同步本地检查异常（{type(exc).__name__}）")
            try:
                self.tick_remote()
            except Exception as exc:  # noqa: BLE001
                self._warn(f"实时同步远端检查异常（{type(exc).__name__}）")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)


# ---------------------------------------------------------------- 内容同步屏障
# 跨机器 active/passive 切换时用：切 active 之前、释放租约之前，尽量让 content/
# 先同步到一个确定的点。state/ 是「只属于这台机器」的，绝不参与这个屏障。
#
# 注意：RealtimeSyncer 目前没有可靠的「立刻 flush / 等上传完成」hook
# （tick_local 受防抖影响，返回 False 不代表已经同步）。所以默认构造出来的
# 屏障是 no-op，会明确返回 status="skipped" + synced=False；不要把它当成
# 「已经同步」。需要真实屏障时注入 adapter / flush / wait 回调。

BARRIER_CONTENT = "content"
BARRIER_STATE = "state"

_HOOK_FLUSH_NAMES = ("flush_content", "barrier_flush", "flush")
_HOOK_WAIT_NAMES = ("wait_content", "barrier_wait", "wait_idle", "wait")


class SyncBarrierAdapter:
    """可注入的内容同步适配器协议。

    只允许操作 ``content/``（人设、记忆、人物档案、日记、长期经历、表情包、
    梗库、关系网等）；``state/``（配置、会话、同步状态、日志等）不参与跨机
    同步，也不应该交给这两个 hook。

    hook 的返回值约定：

    * ``{"status": "synced"|"partial"|"skipped"|"failed", "synced": bool,
      "detail": "..."}`` —— 推荐，屏障会原样汇总；
    * ``True`` / ``False`` —— 分别表示已同步 / 没做事；
    * ``None`` 或不返回 —— 按 ``skipped`` 处理（不假装同步过）。

    ``timeout`` 是建议预算，由 hook 自己遵守；同步 hook 是阻塞调用，屏障不会
    为了超时另起线程（避免留下仍在写文件的幽灵任务）。
    """

    name = "base"

    def flush_content(self, *, timeout: float = 5.0) -> dict:
        """把 content/ 的待推变化尽量推出去；没做事就返回 skipped。"""
        raise NotImplementedError

    def wait_content(self, *, timeout: float = 5.0) -> dict:
        """等待在途的 content 同步结束；没做事就返回 skipped。"""
        raise NotImplementedError


class NoopSyncBarrierAdapter(SyncBarrierAdapter):
    """明确的 no-op：什么都不做，返回 skipped（绝不谎报已同步）。"""

    name = "noop"

    def flush_content(self, *, timeout: float = 5.0) -> dict:
        return {"status": "skipped", "synced": False,
                "detail": "没有可用的 content flush hook"}

    def wait_content(self, *, timeout: float = 5.0) -> dict:
        return {"status": "skipped", "synced": False,
                "detail": "没有可用的 content wait hook"}


def _normalize_barrier_result(raw, source: str) -> dict:
    """把 hook 的返回值规整成统一结果；无法确认的一律算 skipped。"""
    if isinstance(raw, dict):
        status = str(raw.get("status") or "").strip().lower()
        synced = raw.get("synced")
        if synced is None:
            synced = status == "synced"
        synced = bool(synced)
        if status not in ("synced", "partial", "skipped", "failed"):
            status = "synced" if synced else "skipped"
        if status == "synced" and not synced:
            status = "partial"
        return {"status": status, "synced": synced, "source": source,
                "detail": str(raw.get("detail") or "")}
    if raw is True:
        return {"status": "synced", "synced": True, "source": source, "detail": ""}
    if raw is False or raw is None:
        return {"status": "skipped", "synced": False, "source": source,
                "detail": "hook 没有返回可确认的同步结果"}
    return {"status": "skipped", "synced": False, "source": source,
            "detail": "hook 返回值类型无法识别"}


class ContentSyncBarrier:
    """跨机 active/passive 切换前的「只同步 content/」屏障。

    用法::

        barrier = ContentSyncBarrier(syncer)          # 没有可靠 hook → skipped
        # 或
        barrier = ContentSyncBarrier(adapter=my_adapter)
        result = barrier.before_claim()              # 接管发消息之前
        if result["status"] != "synced":
            ...                                      # 由调用方决定是否 fail-closed

    :meth:`before_claim` 先 ``wait_content`` 再 ``flush_content``：确认在途同步
    收敛、并把本机 content 的待推变化推出去，避免带着旧内容接管。
    :meth:`before_release` 先 flush 再 wait：确保自己的 content 已经交出去，
    下一个 holder 能拉到。

    返回 dict 的关键字段：

    * ``status``：``synced`` / ``partial`` / ``skipped`` / ``failed``；
    * ``synced``：只有 ``status == "synced"`` 才为 True；
    * ``layers``：恒为 ``["content"]``，``state_synced`` 恒为 False；
    * ``steps`` / ``flush`` / ``wait``：每个 hook 的独立结果，便于排查；
    * ``detail`` / ``duration_ms``：给人看的说明和耗时。

    这个类**本身不读写任何文件**，也不会调用 RealtimeSyncer 的轮询方法；
    它只通过注入的 hook 操作 content/。没有 hook 时返回 ``skipped``。
    """

    def __init__(self, syncer=None, *, adapter=None,
                 flush=None, wait=None, timeout: float = 5.0):
        self.syncer = syncer
        self.adapter = adapter
        self.timeout = _safe_float(timeout, 5.0, floor=0.05)
        self._flush_hook, self._flush_source = self._pick(
            flush, adapter, "flush_content", _HOOK_FLUSH_NAMES)
        self._wait_hook, self._wait_source = self._pick(
            wait, adapter, "wait_content", _HOOK_WAIT_NAMES)

    @staticmethod
    def _find_hook(obj, names: tuple[str, ...]):
        for name in names:
            try:
                hook = getattr(obj, name, None)
            except Exception:  # noqa: BLE001 - 假对象 / 属性代理都可能抛
                continue
            if callable(hook):
                return hook, name
        return None, ""

    def _pick(self, explicit, adapter, adapter_name: str,
              syncer_names: tuple[str, ...]):
        """优先级：显式回调 > adapter > syncer 鸭子类型 hook > no-op。"""
        if callable(explicit):
            return explicit, "callable"
        hook, name = self._find_hook(adapter, (adapter_name,))
        if hook is not None:
            label = str(getattr(adapter, "name", "") or type(adapter).__name__)
            return hook, f"adapter:{label}"
        hook, name = self._find_hook(self.syncer, syncer_names)
        if hook is not None:
            return hook, f"syncer:{name}"
        return None, "noop"

    @property
    def hooks(self) -> dict:
        """当前实际使用的 hook 来源（不含任何凭据，可安全进日志）。"""
        return {"flush": self._flush_source, "wait": self._wait_source}

    @property
    def is_noop(self) -> bool:
        return (self._flush_source in ("noop", "adapter:noop")
                and self._wait_source in ("noop", "adapter:noop"))

    def before_claim(self, *, timeout: float | None = None) -> dict:
        """接管 active 之前调用：先等 content 收敛，再推本机 content 变化。"""
        return self._run("before_claim", timeout)

    def before_release(self, *, timeout: float | None = None) -> dict:
        """释放 active 租约之前调用：先推 content 变化，再等它确认完成。"""
        return self._run("before_release", timeout)

    def _run(self, phase: str, timeout: float | None) -> dict:
        budget = self.timeout if timeout is None else _safe_float(
            timeout, self.timeout, floor=0.01)
        started = time.monotonic()
        if phase == "before_claim":
            plan = (("wait", self._wait_hook, self._wait_source),
                    ("flush", self._flush_hook, self._flush_source))
        else:
            plan = (("flush", self._flush_hook, self._flush_source),
                    ("wait", self._wait_hook, self._wait_source))

        steps: list[tuple[str, dict]] = []
        for name, hook, source in plan:
            steps.append((name, self._invoke(hook, source, budget)))

        results = dict(steps)
        statuses = [item["status"] for item in results.values()]
        if "failed" in statuses:
            status = "failed"
        elif statuses and all(item == "synced" for item in statuses):
            status = "synced"
        elif any(item in ("synced", "partial") for item in statuses):
            status = "partial"
        else:
            status = "skipped"

        details = [f"{name}: {item['detail']}" for name, item in steps
                   if item.get("detail")]
        return {
            "phase": phase,
            "status": status,
            "synced": status == "synced",
            "skipped": status == "skipped",
            "partial": status == "partial",
            "failed": status == "failed",
            "layers": [BARRIER_CONTENT],
            "excluded_layers": [BARRIER_STATE],
            "state_synced": False,
            "content_only": True,
            "order": [name for name, _ in steps],
            "steps": results,
            "flush": results.get("flush", {}),
            "wait": results.get("wait", {}),
            "detail": "；".join(details),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    def _invoke(self, hook, source: str, timeout: float) -> dict:
        if hook is None:
            return {"status": "skipped", "synced": False, "source": "noop",
                    "detail": "没有可用的内容同步 hook"}
        try:
            raw = hook(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 屏障失败要如实上报，不吞掉
            return {"status": "failed", "synced": False, "source": source,
                    "detail": f"hook 异常：{type(exc).__name__}"}
        return _normalize_barrier_result(raw, source)


def start_realtime(config, base_dir: str, **kwargs) -> RealtimeSyncer | None:
    """按配置决定要不要起实时线程；没开/没启用返回 None。"""
    try:
        section = _section(config)
    except Exception:
        return None
    if not section.get("enabled"):
        return None
    if str(section.get("mode") or "manual").strip().lower() != "realtime":
        return None
    try:
        syncer = RealtimeSyncer(config, base_dir, **kwargs)
        syncer.start()
        log.info(
            f"实时同步已启动：本地轮询 {syncer.poll_sec}s / 防抖 {syncer.debounce_sec}s / "
            f"远端 {syncer.remote_poll_sec}s"
        )
        return syncer
    except Exception as exc:  # noqa: BLE001
        log.warn(f"实时同步启动失败（不影响运行）：{type(exc).__name__} {exc}")
        return None
