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
  keep_both（默认）：本地改过 + 远端也变了 → 远端包存到 data/sync-conflict-<时间>/，本机不动；
  local：本地优先，忽略远端这次变化（只记日志）；
  remote：远端优先，直接 pull（persona/记忆会覆盖；config 仍按 overwrite_config 决定）。
首次运行（本机还没有 .sync-state.json）默认 keep_both：宁可让你确认，也不悄悄覆盖本机人设。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from . import cloudsync
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


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, "data", STATE_NAME)


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


def _write_conflict(base_dir: str, raw: bytes, manifest: dict) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    out_dir = os.path.join(base_dir, "data", f"sync-conflict-{stamp}")
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
        self.state_path = _state_path(base_dir)
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
                out_dir = _write_conflict(self.base_dir, raw, manifest)
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
                out_dir = _write_conflict(self.base_dir, raw, manifest)
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
