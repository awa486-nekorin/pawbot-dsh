#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SCP 一键同步：Windows 本机 <-> 阿里云 ECS，只同步 content/。

用法：
    # Windows（本机）
    py -3.13 scripts/scp_sync.py --mode remote --action pull  --root "D:\\WorkBuddy\\pawbot-dsh"
    py -3.13 scripts/scp_sync.py --mode remote --action push  --root "D:\\WorkBuddy\\pawbot-dsh"
    py -3.13 scripts/scp_sync.py --mode remote --action status --root "D:\\WorkBuddy\\pawbot-dsh"

    # ECS（云端，同步包就在本机目录）
    python3 scripts/scp_sync.py --mode local --action pull  --root /opt/pawbot-dsh
    python3 scripts/scp_sync.py --mode local --action push  --root /opt/pawbot-dsh

动作含义：
    pull  = 把同步包套用到本机 data/content（拉取/导入）
    push  = 把本机 data/content 打包成同步包（推回/导出）
    status= 只读状态
    test  = 测试远端目录可写

配置：scripts/scp-sync.json（同目录；不存在时读 scp-sync.example.json 并报错）。
只同步 content/；config.json、sessions、browser 缓存、密钥都不动。
运行中的 PawBot 会拒绝同步（data/.runtime.lock 的 pid 仍存活）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

DEFAULT_BUNDLE = "pawbot-content.tgz"


# --------------------------------------------------------------------- 配置
def load_config(path: Path) -> dict:
    if not path.is_file():
        example = path.with_name("scp-sync.example.json")
        if example.is_file():
            raise SystemExit(
                f"缺少 {path.name}。请复制 {example.name} 为 {path.name}，"
                "填 host / user / key / dir / data_dir 后再运行。")
        raise SystemExit(f"缺少配置文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as error:
        raise SystemExit(f"配置文件不是合法 JSON：{error}")
    remote = data.setdefault("remote", {})
    local = data.setdefault("local", {})
    data["bundle"] = str(data.get("bundle") or DEFAULT_BUNDLE)
    remote.setdefault("port", 22)
    remote.setdefault("user", "")
    remote.setdefault("key", "")
    remote.setdefault("dir", "/srv/pawbot-sync")
    local.setdefault("data_dir", "")
    local.setdefault("bundle_dir", "/srv/pawbot-sync")
    data["keep_backups"] = int(data.get("keep_backups") or 5)
    return data


def resolve_data_dir(root: Path, cfg: dict) -> Path:
    configured = str(cfg["local"].get("data_dir") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured)).resolve()
    root = root.resolve()
    for candidate in (root / "app" / "data", root / "data"):
        if candidate.is_dir():
            return Path(os.path.realpath(candidate))
    return Path(os.path.realpath(root / "app" / "data"))


def resolve_bundle_path(cfg: dict, mode: str) -> Path:
    if mode == "remote":
        return Path("/") / cfg["bundle"]  # 仅用于展示；实际走 scp
    return Path(os.path.expanduser(str(cfg["local"]["bundle_dir"]))) / cfg["bundle"]


# --------------------------------------------------------------------- 运行锁
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running(data: Path) -> tuple[bool, int]:
    lock = data / ".runtime.lock"
    if not lock.is_file():
        return False, 0
    try:
        info = json.loads(lock.read_text(encoding="utf-8-sig"))
    except Exception:
        return True, 0
    pid = int(info.get("pid") or 0)
    return pid_alive(pid), pid


def require_stopped(data: Path) -> None:
    busy, pid = running(data)
    if busy:
        raise SystemExit(
            f"PawBot 还在运行（pid={pid or '?'}），拒绝同步。\n"
            "请先在 DSH/服务面板停止 PawBot，再重新点按钮。")


# --------------------------------------------------------------------- 打包
def count_files(path: Path) -> tuple[int, int]:
    files = size = 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return files, size


def make_bundle(data: Path, out: Path) -> tuple[int, int, str]:
    content = data / "content"
    if not content.is_dir():
        raise SystemExit(f"找不到 content 目录：{content}")
    with tarfile.open(out, "w:gz") as tar:
        tar.add(content, arcname="content")
    files, size = count_files(content)
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    return files, size, sha


def backup_content(data: Path, keep: int) -> Path | None:
    content = data / "content"
    if not content.is_dir():
        return None
    backup_dir = data / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"pre-pull-{time.strftime('%Y%m%d-%H%M%S')}.tgz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(content, arcname="content")
    backups = sorted(backup_dir.glob("pre-pull-*.tgz"))
    for old in backups[:-max(1, keep)]:
        try:
            old.unlink()
        except OSError:
            pass
    return out


def apply_bundle(bundle: Path, data: Path, keep: int) -> tuple[int, str]:
    if not bundle.is_file():
        raise SystemExit(f"同步包不存在：{bundle}")
    tmp = Path(tempfile.mkdtemp(prefix="pawbot-scp-pull-"))
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            names = tar.getnames()
            if not any(n == "content" or n.startswith("content/") for n in names):
                raise SystemExit("同步包里没有 content/，拒绝导入")
            try:
                tar.extractall(tmp, filter="data")  # Python 3.12+
            except TypeError:
                tar.extractall(tmp)
        new_content = tmp / "content"
        if not new_content.is_dir():
            raise SystemExit("同步包解压后没有 content/，拒绝导入")
        backup = backup_content(data, keep)
        current = data / "content"
        trash = data / f"content.old-{time.strftime('%Y%m%d-%H%M%S')}"
        if current.exists():
            shutil.move(str(current), str(trash))
        try:
            shutil.move(str(new_content), str(current))
        except Exception:
            if not current.exists() and trash.exists():
                shutil.move(str(trash), str(current))
            raise
        if trash.exists():
            shutil.rmtree(trash, ignore_errors=True)
        files, _size = count_files(current)
        sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
        note = f"；已备份到 {backup}" if backup else ""
        return files, f"导入完成：{files} 个文件，包 sha256={sha[:16]}…{note}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------- 远端
def _target(remote: dict) -> str:
    user = str(remote.get("user") or "").strip()
    host = str(remote.get("host") or "").strip()
    if not host:
        raise SystemExit("配置里 remote.host 为空：请填 ECS IP 或 ~/.ssh/config 的 Host 别名")
    return f"{user}@{host}" if user else host


def _remote_os(remote: dict) -> str:
    return str(remote.get("os") or "linux").strip().lower()


def _win_path(path: str) -> str:
    return str(path or "").replace("/", "\\")


def _ssh_base(remote: dict) -> list[str]:
    cmd = ["ssh", "-p", str(remote.get("port") or 22)]
    key = str(remote.get("key") or "").strip()
    if key:
        cmd += ["-i", os.path.expanduser(key)]
    cmd += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    return cmd + [_target(remote)]


def _scp_base(remote: dict) -> list[str]:
    cmd = ["scp", "-P", str(remote.get("port") or 22)]
    key = str(remote.get("key") or "").strip()
    if key:
        cmd += ["-i", os.path.expanduser(key)]
    cmd += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    return cmd


def _remote_path(remote: dict, name: str) -> str:
    return f"{str(remote.get('dir') or '/srv/pawbot-sync').rstrip('/')}/{name}"


def remote_test(remote: dict) -> str:
    path = str(remote.get("dir") or "/srv/pawbot-sync").rstrip("/")
    if _remote_os(remote) == "windows":
        # Windows 端用 cmd 建目录；失败也不阻断，scp 会给出真实错误
        win = _win_path(path)
        cmd = _ssh_base(remote) + [f'cmd /c if not exist "{win}" mkdir "{win}"']
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception:
            pass
        return path
    cmd = _ssh_base(remote) + [
        f"mkdir -p {shlex.quote(path)} && test -w {shlex.quote(path)} && echo OK"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise SystemExit(f"远端目录不可写：{result.stderr.strip()[:300]}")
    return path


def remote_fetch(remote: dict, bundle: str, dest: Path) -> None:
    src = f"{_target(remote)}:{_remote_path(remote, bundle)}"
    result = subprocess.run(_scp_base(remote) + [src, str(dest)],
                            capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise SystemExit(
            f"拉取失败：{result.stderr.strip()[:300]}\n"
            "请确认 ECS 上已经有同步包（先在另一边点一次“推回”）。")


def remote_upload(remote: dict, bundle: str, local: Path) -> None:
    dir_path = str(remote.get("dir") or "/srv/pawbot-sync").rstrip("/")
    if _remote_os(remote) == "windows":
        # Windows OpenSSH 的远端 shell 不是 POSIX，直接 scp 到最终文件，避免 mv/引号问题
        dst = f"{_target(remote)}:{dir_path}/{bundle}"
        result = subprocess.run(_scp_base(remote) + [str(local), dst],
                                capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise SystemExit(f"上传失败：{result.stderr.strip()[:300]}")
        return
    tmp_name = bundle + ".tmp"
    dst = f"{_target(remote)}:{dir_path}/{tmp_name}"
    result = subprocess.run(_scp_base(remote) + [str(local), dst],
                            capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise SystemExit(f"上传失败：{result.stderr.strip()[:300]}")
    cmd = _ssh_base(remote) + [
        f"mv -f {shlex.quote(dir_path + '/' + tmp_name)} "
        f"{shlex.quote(dir_path + '/' + bundle)}"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise SystemExit(f"远端改名失败：{result.stderr.strip()[:300]}")


def remote_stat(remote: dict, bundle: str) -> str:
    if _remote_os(remote) == "windows":
        return "（Windows 远端，状态略；以 scp 是否成功为准）"
    path = _remote_path(remote, bundle)
    cmd = _ssh_base(remote) + [
        f"if [ -f {shlex.quote(path)} ]; then "
        f"stat -c '%s %y' {shlex.quote(path)}; else echo MISSING; fi"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return f"无法读取（{result.stderr.strip()[:120]}）"
    return result.stdout.strip()


# --------------------------------------------------------------------- 动作
def action_pull(cfg: dict, mode: str, data: Path) -> int:
    require_stopped(data)
    bundle_name = cfg["bundle"]
    if mode == "remote":
        tmp = Path(tempfile.mkdtemp(prefix="pawbot-scp-")) / bundle_name
        try:
            remote_fetch(cfg["remote"], bundle_name, tmp)
            files, message = apply_bundle(tmp, data, cfg["keep_backups"])
            print(message)
            return 0
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
    bundle = resolve_bundle_path(cfg, mode)
    files, message = apply_bundle(bundle, data, cfg["keep_backups"])
    print(message)
    return 0


def action_push(cfg: dict, mode: str, data: Path) -> int:
    require_stopped(data)
    bundle_name = cfg["bundle"]
    tmp = Path(tempfile.mkdtemp(prefix="pawbot-scp-")) / bundle_name
    try:
        files, content_size, sha = make_bundle(data, tmp)
        bundle_size = tmp.stat().st_size
        if mode == "remote":
            remote_test(cfg["remote"])
            remote_upload(cfg["remote"], bundle_name, tmp)
            where = _remote_path(cfg["remote"], bundle_name)
        else:
            bundle_dir = Path(os.path.expanduser(str(cfg["local"]["bundle_dir"])))
            bundle_dir.mkdir(parents=True, exist_ok=True)
            dst = bundle_dir / bundle_name
            shutil.copy2(tmp, dst)
            where = str(dst)
        print(f"推送完成：content {files} 个文件 / {content_size} B；"
              f"包 {bundle_size} B，sha256={sha[:16]}…")
        print(f"位置：{where}")
        return 0
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def action_status(cfg: dict, mode: str, data: Path) -> int:
    busy, pid = running(data)
    files, size = count_files(data / "content")
    print("PawBot 数据同步状态")
    print(f"  模式        : {'远端 SCP（Windows 本机）' if mode == 'remote' else '本机目录（ECS）'}")
    print(f"  数据根      : {data}")
    print(f"  本地 content: {files} 个文件，{size} B")
    print(f"  本地 PawBot : {'运行中 pid=' + str(pid) if busy else '未运行'}")
    if mode == "remote":
        remote = cfg["remote"]
        host = str(remote.get("host") or "").strip() or "(未填 remote.host)"
        print(f"  远端        : {host}:{str(remote.get('dir'))}")
        try:
            print(f"  远端同步包  : {remote_stat(remote, cfg['bundle'])}")
        except SystemExit as error:
            print(f"  远端同步包  : {error}")
    else:
        bundle = resolve_bundle_path(cfg, mode)
        if bundle.is_file():
            sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
            print(f"  同步包      : {bundle}")
            print(f"  大小/时间   : {bundle.stat().st_size} B / "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(bundle.stat().st_mtime))}")
            print(f"  sha256      : {sha[:16]}…")
        else:
            print(f"  同步包      : 不存在（{bundle}）")
    return 0


def action_test(cfg: dict, mode: str) -> int:
    if mode == "remote":
        path = remote_test(cfg["remote"])
        print(f"SSH/SCP 正常，远端目录可写：{path}")
    else:
        bundle_dir = Path(os.path.expanduser(str(cfg["local"]["bundle_dir"])))
        bundle_dir.mkdir(parents=True, exist_ok=True)
        probe = bundle_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"本机目录可写：{bundle_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["remote", "local"], required=True,
                    help="remote=Windows 通过 scp 连 ECS；local=ECS 本机目录")
    ap.add_argument("--action", choices=["pull", "push", "status", "test"], required=True)
    ap.add_argument("--root", default="", help="部署根（含 app/）；默认脚本上级")
    ap.add_argument("--config", default="", help="配置文件；默认 scripts/scp-sync.json")
    args = ap.parse_args()
    script_dir = Path(__file__).resolve().parent
    config_path = Path(args.config) if args.config else script_dir / "scp-sync.json"
    cfg = load_config(config_path)
    root = Path(args.root).expanduser().resolve() if args.root else script_dir.parent
    data = resolve_data_dir(root, cfg)
    if args.action == "status":
        return action_status(cfg, args.mode, data)
    if args.action == "test":
        return action_test(cfg, args.mode)
    if args.action == "pull":
        return action_pull(cfg, args.mode, data)
    return action_push(cfg, args.mode, data)


if __name__ == "__main__":
    sys.exit(main())
