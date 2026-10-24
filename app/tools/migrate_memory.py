#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把旧记忆搬进 v2 的实体库。

    py tools/migrate_memory.py --dry-run     # 只看会搬什么，不写盘
    py tools/migrate_memory.py               # 真搬（幂等，跑几次结果一样）

搬：longmem/episodes.jsonl → 事件 / 日摘要（**从新到旧去重**，旧的累积摘要会被丢掉）
    people/<uid>.md        → 人物档案 + 好感
    memory/facts.md        → 长期事实

不搬：impression.md（0 字节）、slang.md（不是实体，改成命中才查）、sessions.json（L2 会自己长）

搬完不会自动启用 v2。要启用就把 data/config.json 里
    "memory": {"engine": "v2"}
打开，然后重启。想退回就改回 "legacy"，一个字都不用动代码。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot.config import Config          # noqa: E402
from pawbot.mem import migrate            # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="把旧记忆搬进 v2 实体库")
    p.add_argument("--base", default=".", help="pawbot 项目根目录（默认当前目录）")
    p.add_argument("--dry-run", action="store_true", help="只看报告，不写盘")
    args = p.parse_args()

    base = os.path.abspath(args.base)
    if not os.path.isdir(os.path.join(base, "data")):
        print(f"✗ {base} 里没有 data/ —— 这应该是 pawbot 项目根目录")
        return 1

    cfg = Config(os.path.join(base, "data", "config.json"))
    报 = migrate.run(base, cfg, dry_run=args.dry_run)

    模式 = "试运行（没写盘）" if args.dry_run else "已写入"
    print(f"迁移{模式}：{base}")
    print(f"  事件        {报['episodes']:4} 条")
    print(f"  日摘要      {报['day']:4} 条")
    print(f"  人物档案    {报['profile']:4} 条")
    print(f"  好感        {报['relation']:4} 条")
    print(f"  长期事实    {报['fact']:4} 条")
    if 报["episodes_去重丢掉"]:
        print(f"  去重丢掉    {报['episodes_去重丢掉']:4} 条"
              f"（旧的累积摘要，内容已经被更新的那条包含）")
    if 报["跳过"]:
        print(f"  跳过        {len(报['跳过'])} 条：{'、'.join(报['跳过'][:3])}")

    if not args.dry_run and 报.get("库统计"):
        统 = 报["库统计"]
        print(f"\n实体库：{统['条目']} 条 / {统['总字数']} 字 → {统['文件']}")
        print(f"  按类型：{统['按kind']}")
        print("\n下一步：把 data/config.json 的 memory.engine 改成 \"v2\"，重启 PawBot。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
