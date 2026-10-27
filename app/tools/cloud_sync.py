#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""云端共享/同步 CLI。

    py tools/cloud_sync.py status
    py tools/cloud_sync.py test
    py tools/cloud_sync.py push [--dry]
    py tools/cloud_sync.py pull [--dry] [--overwrite-config]

配置在 data/config.json 的 "sync" 段（见 docs/CLOUD-SYNC.md）。
输出里不会打印 password / token / passphrase。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import cloudsync                 # noqa: E402
from pawbot.config import Config             # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROVIDER_LABELS = {"local": "本机/挂载目录", "webdav": "WebDAV", "http": "自建 HTTP 端点"}


def _size(num: int) -> str:
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _brief(items, limit: int = 8) -> str:
    items = list(items or [])
    if not items:
        return "无"
    head = "、".join(str(x) for x in items[:limit])
    if len(items) > limit:
        return f"{head} …（共 {len(items)} 项）"
    return head


def _yes(value) -> str:
    return "是" if value else "否"


def _load(args) -> Config:
    base = os.path.abspath(args.base)
    return Config(os.path.join(base, "data", "config.json"))


def _provider_text(name: str) -> str:
    return f"{name}（{PROVIDER_LABELS.get(name, name)}）"


def cmd_status(args) -> int:
    info = cloudsync.status(_load(args), os.path.abspath(args.base))
    print(f"云同步：{'已启用' if info['enabled'] else '未启用'}")
    print(f"  服务商    {_provider_text(info['provider'])}")
    print(f"  目标      {info['url'] or '（未配置 sync.url）'}")
    if info.get("remote_dir"):
        print(f"  远端目录  {info['remote_dir']}")
    print(f"  可加密    {_yes(info['encrypted'])}"
          + (f"（{info['encryption']}）" if info["encrypted"] else "（未设置 passphrase）"))
    include = info["include"]
    print("  包含      "
          f"人设={_yes(include['persona'])} 记忆={_yes(include['memory'])} "
          f"配置={_yes(include['config'])} 表情包={_yes(include['stickers'])}")
    print(f"  冲突策略  "
          f"{'覆盖 config.json' if info['overwrite_config'] else '不覆盖，只写 data/config.synced.json'}")
    print(f"  启动拉取  {_yes(info['auto_pull_on_start'])}")
    print(f"  上次上传  {info['last_push'] or '—'}")
    print(f"  上次拉取  {info['last_pull'] or '—'}")
    print(f"  远端文件  {info['remote_name']}")
    if info.get("warning") == "CONFIG_REQUIRES_ENCRYPTION":
        print("  [!] 包含 config 但没设 passphrase：push 会被拒绝（fail-closed）")
    if not info["ready"]:
        print("  [!] 还没填 sync.url，push/pull 跑不起来")
    return 0


def cmd_test(args) -> int:
    base = os.path.abspath(args.base)
    info = cloudsync.test_connection(_load(args), base)
    print(f"[OK] 连接成功：{_provider_text(info['provider'])}"
          f"（{info['duration_ms']} ms）")
    print(f"  {info['detail']}")
    return 0


def cmd_push(args) -> int:
    base = os.path.abspath(args.base)
    result = cloudsync.push(_load(args), base, dry=args.dry)
    title = "上传完成（试运行，未上传）" if result["dry_run"] else "上传完成"
    print(f"[OK] {title}")
    print(f"  目标      {_provider_text(result['provider'])} · {result['remote_name']}")
    print(f"  大小      {_size(result['bytes'])} · 文件 {result['files']} 个 · "
          f"加密 {('是（' + result['encryption'] + '）') if result['encrypted'] else '否'} · "
          f"耗时 {result['duration_ms']} ms")
    print(f"  摘要      sha256 {result['sha256'][:16]}…")
    if not result["dry_run"]:
        print(f"  记录      last_push = {result['last_push'] or '—'}")
    return 0


def cmd_pull(args) -> int:
    base = os.path.abspath(args.base)
    overwrite = True if args.overwrite_config else None
    result = cloudsync.pull(_load(args), base, dry=args.dry, overwrite_config=overwrite)
    title = "拉取完成（试运行，未写盘）" if result["dry_run"] else "拉取完成"
    print(f"[OK] {title}")
    print(f"  来源      {_provider_text(result['provider'])} · {result['remote_name']}")
    print(f"  大小      {_size(result['bytes'])} · 包内文件 {result['files']} 个 · "
          f"加密 {('是（' + result['encryption'] + '）') if result['encrypted'] else '否'} · "
          f"耗时 {result['duration_ms']} ms")
    print(f"  写入      {len(result['applied'])} 个：{_brief(result['applied'])}")
    if result.get("unchanged"):
        print(f"  未变      {len(result['unchanged'])} 个（内容一致，不重写）")
    if result["skipped"]:
        print(f"  跳过      {len(result['skipped'])} 个（本机 include_* 关闭）："
              f"{_brief(result['skipped'])}")

    config = result.get("config") or {}
    if config.get("synced_path"):
        print(f"  配置      {config['synced_path']}")
    if config.get("overwritten"):
        print("  配置      config.json 已被覆盖（覆盖前已备份）")
    elif config.get("synced_path"):
        print("  配置      config.json 未动（默认不覆盖）")
    diff = config.get("diff") or {}
    if diff.get("count"):
        print(f"  差异      {diff['count']} 项（只列 key）："
              f"{_brief(diff.get('changed', []) + diff.get('added', []) + diff.get('removed', []))}")
    if result.get("backup_dir"):
        print(f"  备份      {result['backup_dir']}")
    if not result["dry_run"]:
        print(f"  记录      last_pull = {result['last_pull'] or '—'}")
    return 0


def cmd_watch(args) -> int:
    """前台跑实时同步，Ctrl+C 退出；用来观察「本地改了→多久上传」和冲突。"""
    import time as _time
    from pawbot import sync_realtime

    cfg = _load(args)
    section = cloudsync.sync_section(cfg)
    if not section.get("enabled"):
        print("[!] sync.enabled=false：实时同步不会上传。先在面板/配置里打开。")
    base = os.path.abspath(args.base)

    def on_event(kind, text):
        print(f"[{_time.strftime('%H:%M:%S')}] {kind}: {text}")

    syncer = sync_realtime.RealtimeSyncer(
        cfg, base,
        poll_sec=args.interval or None,
        remote_poll_sec=args.remote_interval or None,
        debounce_sec=args.debounce or None,
        on_event=on_event,
    )
    print(f"实时同步：本地 {syncer.poll_sec}s / 防抖 {syncer.debounce_sec}s / 远端 {syncer.remote_poll_sec}s"
          "（Ctrl+C 退出）")
    syncer.start()
    try:
        syncer.join()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停…")
    finally:
        syncer.stop()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PawBot 云端共享/同步")
    parser.add_argument("--base", default=BASE_DIR, help="PawBot 项目根目录（默认脚本上一级）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="看配置与上次同步时间（不联网）")
    sub.add_parser("test", help="探测远端是否可达/凭据是否正确")

    push_parser = sub.add_parser("push", help="打包上传")
    push_parser.add_argument("--dry", action="store_true", help="只打包看摘要，不写远端")

    pull_parser = sub.add_parser("pull", help="下载并应用")
    pull_parser.add_argument("--dry", action="store_true", help="只下载校验看差异，不写盘")
    pull_parser.add_argument("--overwrite-config", action="store_true",
                             help="允许覆盖 data/config.json（会先备份）")

    watch_parser = sub.add_parser("watch", help="前台跑实时同步（Ctrl+C 退出）")
    watch_parser.add_argument("--interval", type=float, default=0, help="本地轮询秒数（默认读配置）")
    watch_parser.add_argument("--remote-interval", type=float, default=0, dest="remote_interval",
                              help="远端轮询秒数（默认读配置）")
    watch_parser.add_argument("--debounce", type=float, default=0, help="防抖秒数（默认读配置）")

    args = parser.parse_args(argv)
    base = os.path.abspath(args.base)
    if not os.path.isdir(os.path.join(base, "data")):
        print(f"[FAIL] {base} 里没有 data/ - 这应该是 PawBot 项目根目录")
        return 1

    handlers = {"status": cmd_status, "test": cmd_test,
                "push": cmd_push, "pull": cmd_pull, "watch": cmd_watch}

    def _safe_config():
        try:
            return _load(args)
        except Exception:  # noqa: BLE001 - 报错路径上读配置失败也不能再崩
            return None

    try:
        return handlers[args.cmd](args)
    except cloudsync.SyncError as exc:
        print(f"[FAIL] [{exc.code}] {cloudsync.scrub(exc.message, _safe_config())}")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 最后一道兜底
        print(f"[FAIL] {type(exc).__name__}: {cloudsync.scrub(exc, _safe_config())}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
