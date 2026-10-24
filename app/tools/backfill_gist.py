"""给已有的记忆补「骨架」（gist + points）。

## 为什么需要这一步

`mem/decay.py` 的渐忘逻辑是：有 `gist` 的按年龄降分辨率（全文 → 骨架 → 只剩半句），
没有 `gist` 的只能退成「截首句」——说不全，但不会说错。

新写入的记忆自带 `gist`（`write.py` 里跟小结同一个调用要来的，不多花钱）。
但**历史数据没有**，所以要跑一次这个脚本。不跑也能用，只是老记忆降到「骨架」档时
会比较笨（截的是第一句，不一定是最重要那句）。

## 用法

    py tools/backfill_gist.py --dry-run     # 只看看有多少条要补，不调模型
    py tools/backfill_gist.py               # 真跑（会先备份）
    py tools/backfill_gist.py --limit 5     # 只补 5 条试试味道

## 它会做什么

  1. 读 `data/memory/entities.jsonl`
  2. 挑出「没有 gist 但有正文」的（`decay.需要补骨架`）
  3. 逐条问模型：压成一句话主干 + 2~3 个要点
  4. **只加字段，不动 `text`** —— 原文永远留着，随时能「想起来更多」
  5. 写回前备份成 `entities.before-gist-<时间>.jsonl`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import judge
from pawbot.config import Config
from pawbot.log import log
from pawbot.mem import decay
from pawbot.mem.entities import EntityStore


async def 补一条(cfg, row: dict) -> tuple[str, list]:
    正文 = str(row.get("text") or "").strip()
    if not 正文:
        return "", []
    结果 = await judge.judge_json(cfg, decay.骨架提示(正文), timeout=30)
    if not isinstance(结果, dict):
        return "", []
    gist = " ".join(str(结果.get("gist") or "").split())[:120]
    points = [" ".join(str(x).split())[:80]
              for x in (结果.get("points") or []) if str(x or "").strip()][:3]
    return gist, points


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不调模型不写盘")
    ap.add_argument("--limit", type=int, default=0, help="最多补几条（0 = 全部）")
    ap.add_argument("--base", default=".", help="项目根目录")
    args = ap.parse_args()

    base = os.path.abspath(args.base)
    cfg = Config(os.path.join(base, "data", "config.json"))
    库 = EntityStore(base, cfg)
    全部 = 库.load()
    now = time.time()

    待补 = []
    for r in 全部:
        龄 = max(0.0, (now - float(r.get("ts") or now)) / 86400)
        if decay.需要补骨架(r, 龄):
            待补.append(r)

    print(f"库里 {len(全部)} 条，需要补骨架 {len(待补)} 条")
    if args.limit:
        待补 = 待补[: args.limit]
        print(f"  按 --limit 只处理前 {len(待补)} 条")
    if not 待补:
        print("没有要补的。")
        return
    if args.dry_run:
        for r in 待补[:10]:
            print(f"  [{r.get('kind')}] {str(r.get('text') or '')[:50]}…")
        if len(待补) > 10:
            print(f"  ……还有 {len(待补) - 10} 条")
        print("\n（--dry-run：没有调模型，也没有写盘）")
        return

    # 备份
    if os.path.exists(库.path):
        戳 = time.strftime("%Y%m%d-%H%M%S")
        备份 = os.path.join(库.dir, f"entities.before-gist-{戳}.jsonl")
        shutil.copy2(库.path, 备份)
        print(f"已备份 → {备份}")

    成 = 0
    败 = 0
    for i, r in enumerate(待补, 1):
        gist, points = await 补一条(cfg, r)
        if gist:
            r["gist"] = gist
            if points:
                r["points"] = points
            成 += 1
            print(f"  {i}/{len(待补)} ✓ {gist[:44]}")
        else:
            败 += 1
            print(f"  {i}/{len(待补)} ✗ 模型没给出来，这条保持原样（会走截首句兜底）")

    # 写回：load() 已经按 key+kind 去过重，写回去顺便把被顶掉的旧行清掉
    库._write_all(全部)
    print(f"\n完成：补上 {成} 条，失败 {败} 条。原文一个字没动。")

    # 抽样验证
    print("\n抽 3 条看效果：")
    for r in 全部:
        if not str(r.get("gist") or "").strip():
            continue
        for 龄 in (60, 400):
            出 = decay.渲染(r, 龄)
            print(f"  {龄:>3}天 | {出[:70]}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
