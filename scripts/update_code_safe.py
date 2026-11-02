#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安全升级 PawBot 程式：只换代码，不碰外部数据（人设/记忆/设定/密钥）。

前置条件（必须满足，否则拒绝执行）：
    py -3.13 scripts/data_separation.py --mode verify   # update_safe=true

用法：
    # 1) 先看计划（默认 dry-run）
    py -3.13 scripts/update_code_safe.py --package dist/pawbot-server-no-mc-*.tar.gz
    # 2) 真正升级
    py -3.13 scripts/update_code_safe.py --package dist/pawbot-server-no-mc-*.tar.gz --apply
    # 3) 重启 DSH/PawBot 后验证
    py -3.13 scripts/data_separation.py --mode verify

保证：
- 不删除、不覆盖 app/data（目录联接）及其指向的外部数据；
- 不动 app/.venv、node_modules、.git、dist；
- 升级前自动快照 content/ + state 关键文件到 <dataRoot>/backups/pre-update-*.tar.gz；
- 升级前后比对 persona.md / config.json 的 SHA-256，不一致即报错。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data_separation as ds  # noqa: E402

ROOT_FILES = ["index.js", "client.js", "package.json", "cordis.patch.yml", "icon.svg",
              "LICENSE", "README.md", "DEPLOY.md", ".gitignore", "AGENTS.md"]
ROOT_DIRS = ["lib", "locale", "bili-watch", "docs", "scripts"]
APP_FILES = ["run.py", "requirements.txt", "requirements-bili.txt", "ARCHITECTURE.md",
             "README.upstream.md", "使用指南.md", "对话测试窗口.bat"]
APP_DIRS = ["pawbot", "plugins", "web"]
FORBIDDEN = {"app/data", "app/.venv", "app/.data.pawbot-migration", "node_modules",
             ".git", "dist", "__pycache__"}
SNAPSHOT_STATE = ["config.json", "sessions.json", "emotion.json", "mood.json",
                  "topic.json", "proactive_state.json", "plugin_state.json",
                  "libido.json", "media_ledger.json", "chat_window.json"]


def snapshot_data(data_root: Path, stamp: str) -> Path | None:
    backups = data_root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    out = backups / f"pre-update-{stamp}.tar.gz"
    content = data_root / "content"
    state = data_root / "state"
    with tarfile.open(out, "w:gz") as tar:
        if content.is_dir():
            tar.add(content, arcname="content")
        if state.is_dir():
            for name in SNAPSHOT_STATE:
                f = state / name
                if f.is_file():
                    tar.add(f, arcname=f"state/{name}")
    return out


def prepare_source(package: Path, temp: Path) -> Path:
    if package.is_dir():
        return package
    if not package.is_file():
        raise SystemExit(f"package 不存在：{package}")
    if package.suffixes[-2:] == [".tar", ".gz"] or package.suffix == ".gz":
        with tarfile.open(package) as tar:
            tar.extractall(temp)
        entries = [p for p in temp.iterdir()]
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return temp
    raise SystemExit(f"暂不支持的包格式：{package}（请用 .tar.gz）")


def copy_code(src: Path, root: Path) -> list[str]:
    changed = []
    for name in ROOT_FILES:
        s = src / name
        if s.is_file():
            shutil.copy2(s, root / name)
            changed.append(name)
    for name in ROOT_DIRS:
        s = src / name
        if s.is_dir():
            d = root / name
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
            changed.append(name + "/")
    app_src = src / "app"
    app_dst = root / "app"
    if app_src.is_dir() and app_dst.is_dir():
        for name in APP_FILES:
            s = app_src / name
            if s.is_file():
                shutil.copy2(s, app_dst / name)
                changed.append("app/" + name)
        for name in APP_DIRS:
            s = app_src / name
            if s.is_dir():
                d = app_dst / name
                if d.exists():
                    shutil.rmtree(d)
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
                changed.append("app/" + name + "/")
    return changed


def snapshot_code(root: Path, data_root: Path, stamp: str) -> Path | None:
    """升级前留存旧代码（不含 app/data、.venv、node_modules），便于回滚。"""
    out_dir = data_root / "backups" / f"pre-update-code-{stamp}"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in ROOT_FILES + ROOT_DIRS:
            src = root / name
            if src.is_file():
                shutil.copy2(src, out_dir / name)
            elif src.is_dir():
                shutil.copytree(src, out_dir / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
        app_out = out_dir / "app"
        app_out.mkdir(exist_ok=True)
        for name in APP_FILES + APP_DIRS:
            src = root / "app" / name
            if src.is_file():
                shutil.copy2(src, app_out / name)
            elif src.is_dir():
                shutil.copytree(src, app_out / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"))
        return out_dir
    except OSError as error:
        print(f"WARN: 旧代码快照失败（继续升级）：{error}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True)
    ap.add_argument("--root", default="", help="部署根（含 app/）；默认 scripts 的上一级")
    ap.add_argument("--app-dir", default="", help="兼容旧参数：app 目录（含 run.py）")
    ap.add_argument("--profile-config", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.app_dir:
        app_dir = ds.resolve_app_dir(args.app_dir)
        root = app_dir.parent
    else:
        root = Path(args.root).expanduser().resolve() if args.root else \
            Path(__file__).resolve().parent.parent
        app_dir = root / "app"
    profile = ds.find_profile_config(args.profile_config)
    info = ds.describe(root, app_dir, profile)
    print("升级前检查")
    print(f"  数据根            : {info['effective_data_root']}")
    print(f"  已外置            : {info['external']}")
    print(f"  更新安全          : {info['update_safe']}")
    print(f"  persona sha256    : {info['persona_sha256'][:16] or '-'}")
    print(f"  config  sha256    : {info['config_sha256'][:16] or '-'}")
    if not info["update_safe"]:
        print("\n拒绝升级：数据尚未外置。请先按 docs/DATA-SEPARATION.md 完成 dataDir 设置并重启迁移。")
        return 2

    package = Path(args.package).expanduser().resolve()
    print(f"\n升级包            : {package}")
    if not args.apply:
        print("dry-run：未写入任何文件。确认后加 --apply。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    data_root = Path(info["effective_data_root"])
    snap = snapshot_data(data_root, stamp)
    if snap:
        print(f"已快照数据        : {snap}")
    code_snap = snapshot_code(root, data_root, stamp)
    if code_snap:
        print(f"已快照旧代码      : {code_snap}")

    temp = Path(tempfile.mkdtemp(prefix="pawbot-update-"))
    try:
        src = prepare_source(package, temp)
        changed = copy_code(src, root)
        print(f"已更新 {len(changed)} 项：{', '.join(changed[:12])}" +
              (" …" if len(changed) > 12 else ""))
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    after = ds.describe(root, app_dir, profile)
    print("\n升级后检查")
    print(f"  数据根            : {after['effective_data_root']}")
    print(f"  app/data 是联接   : {after['app_data_is_link']}")
    print(f"  联接目标          : {after['app_data_target']}")
    print(f"  persona sha256    : {after['persona_sha256'][:16] or '-'}")
    print(f"  config  sha256    : {after['config_sha256'][:16] or '-'}")
    ok = (after["app_data_is_link"]
          and os.path.normcase(after["app_data_target"]) == os.path.normcase(info["app_data_target"])
          and after["persona_sha256"] == info["persona_sha256"]
          and after["config_sha256"] == info["config_sha256"])
    if not ok:
        print("\nERROR: 升级后数据校验失败（联接或人设/设定哈希变化）。")
        if snap:
            print(f"数据快照在：{snap}；请停止服务后人工核对/回滚。")
        return 3
    print("\nOK：程式已更新，数据未变。")
    print("下一步：重启 DSH（或让插件重载 PawBot），然后运行：")
    print("  py -3.13 scripts/data_separation.py --mode verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
