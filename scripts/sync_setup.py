#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安全配置 PawBot 云端同步（人设/记忆/可选 config），密钥只从环境变量读。

用法：
    # 看当前状态（只读）
    py -3.13 scripts/sync_setup.py --mode status

    # 配置近实时同步（密钥走环境变量，不出现在命令行）
    set PAWBOT_SYNC_USERNAME=you@example.com
    set PAWBOT_SYNC_PASSWORD=应用密码
    set PAWBOT_SYNC_PASSPHRASE=长口令
    py -3.13 scripts/sync_setup.py --mode apply --provider webdav ^
        --url https://dav.jianguoyun.com/dav/ --remote-dir pawbot-sync ^
        --realtime --auto-pull

说明：
- 只改 <dataRoot>/state/config.json 的 "sync" 段，先备份；
- 不改 key/人设/记忆文件；include_config=true 时必须有 passphrase；
- 同步线程在 Bot 启动时读取配置，改完需重启 PawBot/DSH 才生效；
- 详细机制见 docs/SYNC-SETUP.md。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data_separation as ds  # noqa: E402

ENV_USER = "PAWBOT_SYNC_USERNAME"
ENV_PASS = "PAWBOT_SYNC_PASSWORD"
ENV_TOKEN = "PAWBOT_SYNC_TOKEN"
ENV_PASSPHRASE = "PAWBOT_SYNC_PASSPHRASE"


def config_path(root: Path) -> Path:
    app = root / "app"
    data = (Path(os.path.realpath(app / "data")) if (app / "data").exists() else app / "data")
    for candidate in (data / "state" / "config.json", data / "config.json",
                      app / "data" / "state" / "config.json", app / "data" / "config.json"):
        if candidate.is_file():
            return candidate
    return data / "state" / "config.json"


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except Exception as error:
        raise SystemExit(f"读取配置失败：{error}")


def status(path: Path, data: dict, json_out: bool) -> int:
    sync = data.get("sync") if isinstance(data.get("sync"), dict) else {}
    info = {
        "config": str(path),
        "enabled": bool(sync.get("enabled")),
        "mode": str(sync.get("mode") or "manual"),
        "provider": str(sync.get("provider") or "local"),
        "url": str(sync.get("url") or ""),
        "remote_dir": str(sync.get("remote_dir") or ""),
        "username_set": bool(sync.get("username")),
        "password_set": bool(sync.get("password")),
        "token_set": bool(sync.get("token")),
        "passphrase_set": bool(sync.get("passphrase")),
        "include_persona": bool(sync.get("include_persona", True)),
        "include_memory": bool(sync.get("include_memory", True)),
        "include_config": bool(sync.get("include_config", False)),
        "include_stickers": bool(sync.get("include_stickers", False)),
        "auto_pull_on_start": bool(sync.get("auto_pull_on_start", False)),
        "conflict_policy": str(sync.get("conflict_policy") or "keep_both"),
        "poll_sec": sync.get("poll_sec"),
        "debounce_sec": sync.get("debounce_sec"),
        "remote_poll_sec": sync.get("remote_poll_sec"),
        "last_push": sync.get("last_push") or "",
        "last_pull": sync.get("last_pull") or "",
        "config_requires_encryption": bool(sync.get("include_config")) and not bool(sync.get("passphrase")),
    }
    if json_out:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print("PawBot 云端同步状态")
    print(f"  配置文件      : {info['config']}")
    print(f"  启用          : {info['enabled']}")
    print(f"  模式          : {info['mode']}（realtime=近实时）")
    print(f"  provider      : {info['provider']}")
    print(f"  url           : {info['url'] or '(未设置)'}")
    print(f"  remote_dir    : {info['remote_dir'] or '-'}")
    print(f"  凭据          : username={info['username_set']} password={info['password_set']} "
          f"token={info['token_set']} passphrase={info['passphrase_set']}")
    print(f"  同步内容      : persona={info['include_persona']} memory={info['include_memory']} "
          f"config={info['include_config']} stickers={info['include_stickers']}")
    print(f"  自动拉取      : {info['auto_pull_on_start']}")
    print(f"  冲突策略      : {info['conflict_policy']}")
    print(f"  轮询/防抖/远端: {info['poll_sec']}s / {info['debounce_sec']}s / {info['remote_poll_sec']}s")
    print(f"  上次 push/pull: {info['last_push'] or '-'} / {info['last_pull'] or '-'}")
    if info["config_requires_encryption"]:
        print("  [!] include_config=true 但没有 passphrase：push 会被拒绝（fail-closed）")
    print()
    if info["enabled"] and info["mode"] == "realtime":
        print("结论：近实时同步已配置；记得只让一边接 QQ/写数据。")
    else:
        print("结论：未启用近实时同步。可用 --mode apply --realtime 配置。")
    return 0


def apply(path: Path, args, data: dict) -> int:
    sync = dict(data.get("sync") or {})
    if args.provider not in {"local", "webdav", "http"}:
        raise SystemExit("provider 必须是 local / webdav / http")
    if args.provider in {"webdav", "http"} and not args.url:
        raise SystemExit("webdav/http 必须给 --url")
    sync.update({
        "enabled": True,
        "provider": args.provider,
        "url": args.url or "",
        "remote_dir": args.remote_dir,
        "include_persona": not args.no_persona,
        "include_memory": not args.no_memory,
        "include_config": bool(args.include_config),
        "include_stickers": bool(args.include_stickers),
        "auto_pull_on_start": bool(args.auto_pull),
        "mode": "realtime" if args.realtime else "manual",
        "poll_sec": args.poll,
        "debounce_sec": args.debounce,
        "remote_poll_sec": args.remote_poll,
        "conflict_policy": args.conflict_policy,
        "first_run": args.first_run,
        "overwrite_config": bool(args.overwrite_config),
    })
    username = os.environ.get(ENV_USER, "")
    password = os.environ.get(ENV_PASS, "")
    token = os.environ.get(ENV_TOKEN, "")
    passphrase = os.environ.get(ENV_PASSPHRASE, "")
    # 只有显式提供才覆盖，避免把已有凭据清空
    if username:
        sync["username"] = username
    if password:
        sync["password"] = password
    if token:
        sync["token"] = token
    if passphrase:
        sync["passphrase"] = passphrase
    if sync.get("include_config") and not sync.get("passphrase"):
        raise SystemExit("include_config=true 时必须设置环境变量 PAWBOT_SYNC_PASSPHRASE")
    if args.provider == "webdav" and not (sync.get("username") and sync.get("password")):
        raise SystemExit("webdav 需要 PAWBOT_SYNC_USERNAME / PAWBOT_SYNC_PASSWORD")
    if args.provider == "http" and not (sync.get("token") or passphrase):
        print("WARN: http 未设置 token；确认你的端点允许匿名")

    data["sync"] = sync
    backup = path.with_name(path.name + ".bak-sync-" + time.strftime("%Y%m%d-%H%M%S"))
    if path.is_file():
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    print(f"已写入 {path}")
    if backup.exists():
        print(f"原配置备份：{backup}")
    print("注意：同步线程在 Bot 启动时读取配置，请重启 PawBot/DSH 生效。")
    print("凭据只从环境变量读取，未打印；配置里已保存（明文，注意文件权限）。")
    return status(path, data, False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["status", "apply"], default="status")
    ap.add_argument("--root", default="", help="部署根（含 app/）")
    ap.add_argument("--provider", choices=["local", "webdav", "http"], default="local")
    ap.add_argument("--url", default="")
    ap.add_argument("--remote-dir", default="pawbot-sync")
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--auto-pull", action="store_true")
    ap.add_argument("--include-config", action="store_true")
    ap.add_argument("--include-stickers", action="store_true")
    ap.add_argument("--no-persona", action="store_true")
    ap.add_argument("--no-memory", action="store_true")
    ap.add_argument("--overwrite-config", action="store_true")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--debounce", type=float, default=3.0)
    ap.add_argument("--remote-poll", type=float, default=30.0)
    ap.add_argument("--conflict-policy", choices=["keep_both", "local", "remote"], default="keep_both")
    ap.add_argument("--first-run", choices=["keep_both", "pull"], default="keep_both")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).expanduser().resolve() if args.root else \
        Path(__file__).resolve().parent.parent
    path = config_path(root)
    data = load(path)
    if args.mode == "status":
        return status(path, data, args.json)
    return apply(path, args, data)


if __name__ == "__main__":
    sys.exit(main())
