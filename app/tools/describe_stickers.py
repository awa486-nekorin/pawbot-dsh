#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给表情包库补「真实描述」：desc + mood + desc_source。

    python tools/describe_stickers.py                  # 只打印新旧描述（默认 dry run）
    python tools/describe_stickers.py --limit 2        # 先看两张的效果
    python tools/describe_stickers.py --apply          # 真的写回 index.json（先自动备份）

视觉通道优先级：
  ① 显式参数 --base / --key / --model（OpenAI 兼容的 /chat/completions，图片用 base64 data URL）
  ② 环境变量 TOGEMINI_API_KEY，或 DSH 凭证文件里的同名项
     （默认 C:\\Users\\Administrator\\.dsh\\.credentials.yaml，可用 DSH_CREDENTIALS_FILE 改）
     base 默认 https://totokens.cc/v1，model 默认 gemini-4-argon-medium
  ③ 都没有（或某张图看失败）：用标签 + 情绪拼一句人话，desc_source 标 tags

写回时只动 index.json（先备份成 index.json.bak-<时间>），绝不删改图片文件。
同一张图已经有人工/视觉描述（desc_source=vision 且不像「斗图网：xxx」）不会重复覆盖，
除非这次也是视觉结果。

提示词要求和 tools/classify_stickers.py 共用同一套情绪词表（classify_stickers.MOODS）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from pawbot import stickers as sb  # noqa: E402

try:                                      # 作为脚本跑：tools/ 在 sys.path 里
    import classify_stickers as ck
except ImportError:                       # 作为 -m tools.describe_stickers 跑
    from tools import classify_stickers as ck

STICKER_DIR = os.path.join(BASE, "data", "stickers")
INDEX_NAME = "index.json"
MAX_DESC = sb.DESC_MAX_CHARS
FAKE_PREFIX = "斗图网："               # 老的假描述，一律换掉
IMAGE_EXTS = sb.EXTS


def load_index(sdir: str) -> dict:
    path = os.path.join(sdir, INDEX_NAME)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backup_index(sdir: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    src = os.path.join(sdir, INDEX_NAME)
    dst = f"{src}.bak-{stamp}"
    shutil.copy2(src, dst)
    return dst


def resolve_channel(args) -> dict | None:
    """按优先级找看图通道；找不到返回 None（只用标签兜底）。"""
    base = str(args.base or "").strip().rstrip("/")
    key = str(args.key or "").strip()
    model = str(args.model or "").strip()
    if key:
        return {"base_url": base or sb.DESC_BASE,
                "api_key": key,
                "model": model or sb.DESC_MODEL,
                "source": "explicit"}
    key = sb.read_credential()
    if key:
        return {"base_url": base or sb.DESC_BASE,
                "api_key": key,
                "model": model or sb.DESC_MODEL,
                "source": "togemini"}
    return None


def describe_one(path: str, tags: list[str], channel: dict | None,
                 timeout: float) -> dict:
    """给一张图定 desc / mood / desc_source。看图失败就退回标签描述。"""
    mood_hint = sb.mood_from_tags(tags)
    info = None
    if channel is not None:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            raw = b""
        if raw:
            info = sb.vision_describe(raw, base_url=channel["base_url"],
                                      api_key=channel["api_key"],
                                      model=channel["model"], timeout=timeout,
                                      prompt=sb.DESC_PROMPT)
    if info:
        return {"desc": info["desc"],
                "mood": info.get("mood") or mood_hint,
                # 视觉给的新标签只在原来没标签时用，别把人工标签冲掉
                "tags": list(tags) or list(info.get("tags") or [])[:8],
                "source": "vision"}
    return {"desc": sb.desc_from_tags(tags, mood_hint),
            "mood": mood_hint,
            "tags": list(tags),
            "source": "tags"}


def _should_replace(meta: dict, result: dict) -> bool:
    old = str(meta.get("desc") or "").strip()
    if not old or old.startswith(FAKE_PREFIX):
        return True
    if result["source"] == "vision":
        return old != result["desc"]
    # 标签兜底不覆盖已有的真描述（可能是人工写的 / 上次视觉的结果）
    return str(meta.get("desc_source") or "") not in ("vision",)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="给表情包库补真实描述（desc/mood/desc_source），默认 dry run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true", help="只打印，不写 index（默认）")
    mode.add_argument("--apply", action="store_true", help="真的写回 index.json（先备份）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理几张（按 index 顺序）")
    parser.add_argument("--base", default="", help="看图通道 base URL（留空用默认）")
    parser.add_argument("--key", default="", help="看图通道 api key（留空读 TOGEMINI_API_KEY）")
    parser.add_argument("--model", default="", help=f"看图模型（默认 {sb.DESC_MODEL}）")
    parser.add_argument("--timeout", type=float, default=60.0, help="单张等待上限（秒）")
    parser.add_argument("--workers", type=int, default=2, help="并发数，默认 2")
    parser.add_argument("--dir", default="", help="表情包目录（默认 <repo>/data/stickers）")
    args = parser.parse_args(argv)

    sdir = os.path.abspath(args.dir) if args.dir else STICKER_DIR
    index_path = os.path.join(sdir, INDEX_NAME)
    if not os.path.isfile(index_path):
        print(f"找不到索引：{index_path}")
        return 2
    index = load_index(sdir)
    # 只处理 index 里有、磁盘上真的存在的图
    names = [n for n in index
             if os.path.splitext(n)[1].lower() in IMAGE_EXTS
             and os.path.isfile(os.path.join(sdir, n))]
    missing = [n for n in index if n not in names]
    if args.limit is not None:
        names = names[:max(0, int(args.limit))]
    if not names:
        print("没有可处理的图片。")
        return 0

    channel = resolve_channel(args)
    if channel is None:
        print("视觉通道：无（没有 key，全部用标签拼描述）")
    else:
        print(f"视觉通道：{channel['source']} model={channel['model']} "
              f"base={channel['base_url']}")
    if ck.MOODS != sb.MOODS:                  # 两边词表必须一致，不一致先提醒
        print(f"⚠ 情绪词表和 classify_stickers.MOODS 不一致：{ck.MOODS} vs {sb.MOODS}")

    print(f"待处理 {len(names)} 张（目录：{sdir}）")
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as pool:
        futures = {
            pool.submit(describe_one, os.path.join(sdir, name),
                        list((index.get(name) or {}).get("tags") or []),
                        channel, args.timeout): name
            for name in names
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as exc:          # 兜底，不该发生
                results[name] = {"desc": "", "mood": "", "tags": [],
                                 "source": "tags",
                                 "error": f"{type(exc).__name__}: {exc}"}

    counts = {"vision": 0, "tags": 0}
    for name in names:
        result = results[name]
        old = str((index.get(name) or {}).get("desc") or "")
        counts[result["source"]] = counts.get(result["source"], 0) + 1
        print(f"\n[{result['source']}] {name}")
        print(f"  旧：{old or '（空）'}")
        print(f"  新：{result['desc']}")
        print(f"  mood：{result['mood'] or '（未定）'}  tags：{'、'.join(result['tags']) or '（无）'}")
        if result.get("error"):
            print(f"  ⚠ {result['error']}")
    print(f"\ndesc_source：vision {counts.get('vision', 0)} / tags {counts.get('tags', 0)}")

    if missing:
        print("index 里有、磁盘上没有（跳过）：" + "、".join(missing))

    if not args.apply:
        print("\n（dry run：没有写任何文件；确认无误后加 --apply）")
        return 0

    bak = backup_index(sdir)
    updated = 0
    for name in names:
        result = results[name]
        if not result["desc"]:
            continue
        meta = dict(index.get(name) or {})
        if not _should_replace(meta, result):
            continue
        meta["desc"] = result["desc"][:MAX_DESC]
        if result.get("mood"):
            meta["mood"] = result["mood"]
        elif not meta.get("mood"):
            meta["mood"] = sb.mood_from_tags(meta.get("tags") or [])
        meta["desc_source"] = result["source"]
        index[name] = meta
        updated += 1
    write_json_atomic(index_path, index)
    print(f"已写回 {updated} 条到 {index_path}")
    print(f"备份：{bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
