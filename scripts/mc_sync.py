#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MC 模式一键同步：进 MC 前拉云端 → 本地跑；结束后推回云端。

用法（Windows 可直接双击同目录的 .bat）：
    py -3.13 scripts/mc_sync.py --action enter    # 拉云端到本地（启动本地 PawBot 前）
    py -3.13 scripts/mc_sync.py --action exit     # 本地推回云端（停止本地 PawBot 后）
    py -3.13 scripts/mc_sync.py --action status   # 只看状态（不改数据）
    py -3.13 scripts/mc_sync.py --action test     # 测云端连通性

约束（故意保守）：
- enter 前本地 PawBot 必须已停止（检测 data/.runtime.lock 的 pid），否则拒绝，避免拉到一半被运行中的 Bot 覆盖；
- exit 前本地 PawBot 必须已停止，避免把半写状态推上云；
- pull 前自动把本地内容备份到 <dataRoot>/backups/pre-pull-<时间>.tar.gz；
- 不打印 password/token/passphrase；不在命令行接收密钥。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

APP_ENV = os.environ.get("PAWBOT_APP_DIR", "")


def app_dir(root: Path) -> Path:
    if APP_ENV:
        p = Path(APP_ENV).expanduser().resolve()
        if (p / "run.py").is_file():
            return p
        if (p / "app" / "run.py").is_file():
            return p / "app"
    if (root / "app" / "run.py").is_file():
        return root / "app"
    if (root / "run.py").is_file():
        return root
    return root / "app"


def config_path(base: Path) -> Path:
    for candidate in (base / "data" / "state" / "config.json",
                      base / "data" / "config.json"):
        if candidate.is_file():
            return candidate
    return base / "data" / "state" / "config.json"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return True  # 无法确认就按“可能在跑”处理，保守拒绝
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running(base: Path) -> tuple[bool, dict]:
    lock = base / "data" / ".runtime.lock"
    if not lock.is_file():
        return False, {}
    try:
        data = json.loads(lock.read_text(encoding="utf-8-sig"))
    except Exception:
        return True, {"pid": 0, "raw": True}
    pid = int(data.get("pid") or 0)
    return pid_alive(pid), data


def data_root(base: Path) -> Path:
    data = base / "data"
    if data.exists():
        return Path(os.path.realpath(data))
    return data


def backup_before_pull(base: Path) -> Path | None:
    root = data_root(base)
    backup_dir = root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"pre-pull-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    content = root / "content"
    state = root / "state"
    with tarfile.open(out, "w:gz") as tar:
        if content.is_dir():
            tar.add(content, arcname="content")
        for name in ("config.json", "sessions.json", "emotion.json", "mood.json",
                     "topic.json", "proactive_state.json", "plugin_state.json"):
            f = state / name
            if f.is_file():
                tar.add(f, arcname=f"state/{name}")
    return out


def load(config_file: Path):
    from pawbot.config import Config  # noqa: WPS433
    return Config(str(config_file))


def show_status(config, base: Path) -> int:
    from pawbot import cloudsync
    info = cloudsync.status(config, str(base))
    print("PawBot 云端同步状态")
    print(f"  配置        : {config.path}")
    print(f"  已启用      : {info['enabled']}（手动 push/pull 不受此限制）")
    print(f"  provider    : {info['provider']}（{info['provider_label']}）")
    print(f"  目标        : {info['url'] or '(未设置)'}")
    print(f"  加密        : {info['encrypted']}")
    print(f"  包含        : persona={info['include']['persona']} memory={info['include']['memory']} "
          f"config={info['include']['config']} stickers={info['include']['stickers']}")
    print(f"  自动拉取    : {info['auto_pull_on_start']}")
    print(f"  上次 push   : {info['last_push'] or '-'}")
    print(f"  上次 pull   : {info['last_pull'] or '-'}")
    print(f"  凭据        : username={info['username_set']} password={info['password_set']} "
          f"token={info['token_set']} passphrase={info['passphrase_set']}")
    busy, lock = running(base)
    print(f"  本地 PawBot : {'运行中 pid=' + str(lock.get('pid')) if busy else '未运行'}")
    if info.get("warning"):
        print(f"  [!] {info['warning']}")
    if not info["ready"]:
        print("  [!] provider url 未配置，push/pull 无法进行")
    return 0


def action_enter(config, base: Path) -> int:
    from pawbot import cloudsync
    busy, lock = running(base)
    if busy:
        print(f"拒绝拉取：本地 PawBot 仍在运行（pid={lock.get('pid')}）。")
        print("请先在 DSH 面板/状态页停止 PawBot，再重新双击“进入 MC 模式”。")
        return 2
    backup = backup_before_pull(base)
    if backup:
        print(f"已备份本地数据：{backup}")
    try:
        result = cloudsync.pull(config, str(base), dry=False,
                                overwrite_config=False)
    except cloudsync.SyncError as error:
        print(f"拉取失败：{error.code} {error}")
        return 1
    print(f"已拉取云端数据：文件 {result.get('files')}，写入 {len(result.get('applied') or [])}，"
          f"跳过 {len(result.get('skipped') or [])}，加密={result.get('encrypted')}")
    if result.get("backup_dir"):
        print(f"pull 前备份：{result['backup_dir']}")
    print("完成。现在可以启动本地 PawBot（DSH）并开 MC。")
    return 0


def action_exit(config, base: Path) -> int:
    from pawbot import cloudsync
    busy, lock = running(base)
    if busy:
        print(f"拒绝推送：本地 PawBot 仍在运行（pid={lock.get('pid')}）。")
        print("请先停止本地 PawBot（并退出 MC/存档），再重新双击“结束 MC 模式”。")
        return 2
    try:
        result = cloudsync.push(config, str(base), dry=False)
    except cloudsync.SyncError as error:
        print(f"推送失败：{error.code} {error}")
        return 1
    print(f"已推送到云端：文件 {result.get('files')}，大小 {result.get('bytes')} B，"
          f"加密={result.get('encrypted')}，sha256={str(result.get('sha256'))[:16]}…")
    print(f"云端时间戳：{result.get('last_push')}")
    print("完成。现在可以启动云端 PawBot；若它设了 auto_pull_on_start，会自动拉取这份数据。")
    return 0


def action_test(config, base: Path) -> int:
    from pawbot import cloudsync
    try:
        result = cloudsync.test_connection(config, str(base))
    except cloudsync.SyncError as error:
        print(f"连接失败：{error.code} {error}")
        return 1
    print(f"连接成功：{json.dumps(result, ensure_ascii=False)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", choices=["enter", "exit", "status", "test"], required=True)
    ap.add_argument("--root", default="", help="部署根（含 app/）；默认脚本上级")
    args = ap.parse_args()
    root = Path(args.root).expanduser().resolve() if args.root else \
        Path(__file__).resolve().parent.parent
    base = app_dir(root)
    cfg_path = config_path(base)
    if not cfg_path.is_file():
        print(f"找不到配置：{cfg_path}")
        return 2
    # 让 pawbot 包可导入：base 就是 app 目录
    sys.path.insert(0, str(base))
    config = load(cfg_path)
    if args.action == "status":
        return show_status(config, base)
    if args.action == "test":
        return action_test(config, base)
    if args.action == "enter":
        return action_enter(config, base)
    return action_exit(config, base)


if __name__ == "__main__":
    sys.exit(main())
