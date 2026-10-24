#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给表情包库补一批「只要二次元」的图。

    python tools/refill_stickers.py                 # 默认关键词，入库上限 60
    python tools/refill_stickers.py --dry           # 只判定，不写文件
    python tools/refill_stickers.py --limit 70      # 总入库上限
    python tools/refill_stickers.py --keywords "疑惑 动漫,生气 动漫"   # 追加关键词

流程：斗图网搜索 → 下载 → gemini-3.8-flash 判画风 + 情绪（复用
tools/classify_stickers.classify_sticker）→ 只有 anime=true 才入 StickerBook。
被判定为非二次元 / unknown 的图只写进临时目录，用完即删，不会留在 data/stickers。

文件名 二次元_<sha1前6>.<ext>；index 条目补 anime:true。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot import doutu                                    # noqa: E402
from pawbot.stickers import StickerBook, StickerError      # noqa: E402
from tools.classify_stickers import (                       # noqa: E402
    MOODS, classify_sticker, load_llm,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK_CFG = {
    "persona.stickers_dir": "data/stickers",
    "persona.stickers_max": 400,
    "persona.sticker_fetch": True,
}
NAME_PREFIX = "二次元_"
PER_KEYWORD = 4          # 每个关键词目标入库张数（需求 3–5）
SEARCH_LIMIT = 12        # 每个关键词最多看几张搜索结果
DEFAULT_LIMIT = 60

# 关键词 → 兜底情绪（模型给的 mood 不在 MOODS 里时用它）
KEYWORD_MOOD = {
    "二次元表情包": "可爱",
    "动漫表情包": "可爱",
    "Q版表情包": "可爱",
    "猫娘": "可爱",
    "兽耳": "可爱",
    "萌系": "可爱",
    "可爱表情包": "可爱",
    "动漫女孩": "可爱",
    "日系表情包": "可爱",
    "摸鱼": "摸鱼",
    "贴贴": "贴贴",
    "无语 动漫": "无语",
    "震惊 动漫": "震惊",
    "嫌弃 动漫": "嫌弃",
    "委屈 动漫": "委屈",
    "晚安 动漫": "晚安",
    # 以下不是默认词，用于补覆盖缺口（--keywords 时也能直接写中文关键词）
    "疑惑 动漫": "疑惑",
    "问号 动漫": "疑惑",
    "生气 动漫": "生气",
    "愤怒 动漫": "生气",
    "开心 动漫": "开心",
    "害羞 动漫": "害羞",
    "得意 动漫": "得意",
    "傲娇 动漫": "得意",
    "哭哭 动漫": "哭哭",
    "震惊 颜艺": "震惊",
}

DEFAULT_KEYWORDS = [
    "二次元表情包", "动漫表情包", "Q版表情包", "猫娘", "兽耳", "萌系",
    "可爱表情包", "动漫女孩", "日系表情包", "摸鱼", "贴贴",
    "无语 动漫", "震惊 动漫", "嫌弃 动漫", "委屈 动漫", "晚安 动漫",
]

# 主情绪 → 近义标签（入库时补 1–2 个，pick 时按标签重叠打分）
MOOD_EXTRAS = {
    "开心": ["笑死", "高兴"],
    "无语": ["嫌弃", "沉默"],
    "嫌弃": ["无语", "鄙视"],
    "震惊": ["吃惊", "离谱"],
    "疑惑": ["问号", "好奇"],
    "委屈": ["难过", "哭哭"],
    "哭哭": ["委屈", "难过"],
    "得意": ["嚣张", "开心"],
    "贴贴": ["亲昵", "可爱"],
    "晚安": ["困", "睡觉"],
    "摸鱼": ["躺平", "疲惫"],
    "害羞": ["脸红", "可爱"],
    "生气": ["愤怒", "冒火"],
    "可爱": ["萌系", "软萌"],
}

# 关键词里出现这些词时用它兜底（--keywords 传默认表之外的关键词也能用）
GUESS_WORDS = (
    ("无语", "无语"), ("嫌弃", "嫌弃"), ("震惊", "震惊"), ("疑惑", "疑惑"),
    ("问号", "疑惑"), ("委屈", "委屈"), ("哭", "哭哭"), ("开心", "开心"),
    ("笑", "开心"), ("贴贴", "贴贴"), ("晚安", "晚安"), ("摸鱼", "摸鱼"),
    ("害羞", "害羞"), ("生气", "生气"), ("愤怒", "生气"), ("可爱", "可爱"),
    ("得意", "得意"), ("傲娇", "得意"),
)


def mood_for_keyword(keyword: str) -> str:
    """模型 mood 不可用时的兜底：先查表，再按关键词里的词猜。"""
    kw = str(keyword or "").strip()
    if kw in KEYWORD_MOOD:
        return KEYWORD_MOOD[kw]
    for word, mood in GUESS_WORDS:
        if word in kw:
            return mood
    return "可爱"


def tags_for(mood: str) -> list[str]:
    tags = [mood]
    for extra in MOOD_EXTRAS.get(mood, []):
        if extra not in tags:
            tags.append(extra)
        if len(tags) >= 3:
            break
    return tags


def _norm_mood(value: str, keyword: str) -> str:
    mood = str(value or "").strip()
    return mood if mood in MOODS else mood_for_keyword(keyword)


def _name_taken(book: StickerBook, stem: str) -> bool:
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        if os.path.exists(os.path.join(book.dir, stem + ext)):
            return True
    return False


def _mark_anime(book: StickerBook, fname: str) -> None:
    idx = book.index()
    meta = idx.get(fname)
    if meta is None or meta.get("anime") is True:
        return
    meta["anime"] = True
    idx[fname] = meta
    book.save_index(idx)


def _print_coverage(book: StickerBook) -> None:
    idx = book.index()
    counts = {m: 0 for m in MOODS}
    for meta in idx.values():
        for tag in (meta.get("tags") or []):
            if tag in counts:
                counts[tag] += 1
    print("情绪标签覆盖：" + " / ".join(f"{m} {counts[m]}" for m in MOODS))


def run(base: str, cfg: dict, keywords: list[str], per_keyword: int = PER_KEYWORD,
        limit: int = DEFAULT_LIMIT, dry: bool = False) -> dict:
    """抓取 + 判定 + 入库。返回统计（测试也用它）。"""
    book = StickerBook(base, BOOK_CFG)
    known_names = set(book.files())
    known_sha1 = {str(m.get("sha1") or "") for m in book.index().values()}
    known_sha1.discard("")
    seen: set[str] = set()
    total_saved = 0
    stats: dict[str, dict] = {}

    try:
        target = max(1, int(per_keyword))
    except (TypeError, ValueError):
        target = PER_KEYWORD
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = DEFAULT_LIMIT

    with tempfile.TemporaryDirectory(prefix="pawbot-refill-") as tmpdir:
        for keyword in keywords:
            kw = str(keyword or "").strip()
            if not kw:
                continue
            stat = {"found": 0, "got": 0, "saved": 0, "dropped": 0, "reasons": {}}
            stats[kw] = stat

            def drop(reason: str) -> None:
                stat["dropped"] += 1
                stat["reasons"][reason] = stat["reasons"].get(reason, 0) + 1

            urls = []
            try:
                urls = doutu.search(kw, SEARCH_LIMIT) or []
            except Exception as exc:                    # 搜索失败不拖垮整批
                print(f"[{kw}] 搜索失败（{type(exc).__name__}）：跳过")
            stat["found"] = len(urls)

            for url in urls:
                if total_saved >= cap:
                    break
                if stat["saved"] >= target:
                    break
                try:
                    raw = doutu.download(url)
                except Exception:                       # doutu 自己吞异常，兜底
                    raw = None
                if not raw:
                    drop("下载失败")
                    continue
                stat["got"] += 1

                digest = hashlib.sha1(raw).hexdigest()
                if digest in seen:
                    drop("重复")
                    continue
                seen.add(digest)
                if digest in known_sha1:        # 库里已有：不浪费一次看图
                    drop("重复")
                    continue

                stem = NAME_PREFIX + digest[:6]
                # 判定前先写临时文件；分类器只看图，不看路径
                tmp_path = os.path.join(tmpdir, stem + ".img")
                with open(tmp_path, "wb") as f:
                    f.write(raw)
                try:
                    verdict = classify_sticker(tmp_path, cfg)
                except Exception as exc:
                    verdict = {"anime": None, "mood": "",
                               "error": f"{type(exc).__name__}: {exc}"}
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

                if verdict.get("anime") is not True:
                    if verdict.get("anime") is False:
                        drop("非二次元")
                    else:
                        drop("判定失败")
                    continue

                mood = _norm_mood(verdict.get("mood"), kw)
                tags = tags_for(mood)
                desc = f"斗图网：{kw}（二次元）"

                if digest in known_sha1:
                    drop("重复")
                    continue
                if _name_taken(book, stem):
                    drop("重名")
                    continue

                if dry:
                    known_sha1.add(digest)
                    stat["saved"] += 1
                    total_saved += 1
                    print(f"  [dry] 可入库 {stem}（{mood}）")
                    continue

                try:
                    fname = book.save(raw, stem, tags=tags, desc=desc)
                except StickerError as exc:
                    drop("入库失败")
                    print(f"  入库失败 {stem}：{exc}")
                    continue
                except Exception as exc:                # 磁盘 / 索引异常
                    drop("入库失败")
                    print(f"  入库失败 {stem}：{type(exc).__name__}: {exc}")
                    continue

                _mark_anime(book, fname)
                known_sha1.add(digest)
                new = fname not in known_names
                known_names.add(fname)
                if new:
                    stat["saved"] += 1
                    total_saved += 1
                    print(f"  入库 {fname}（{mood}）")
                else:
                    drop("重复")

            reasons = "，".join(f"{k} {v}" for k, v in stat["reasons"].items())
            tail = f"（{reasons}）" if reasons else ""
            print(f"[{kw}] 搜索 {stat['found']} 张，抓到 {stat['got']}，"
                  f"入库 {stat['saved']}，丢弃 {stat['dropped']}{tail}")
            if total_saved >= cap:
                print(f"已达到上限 {cap} 张，停止后续关键词")
                break

    if dry:
        print(f"\n[dry] 可入库 {total_saved} 张，未写任何文件；库现有 {len(book.files())} 张")
    else:
        print(f"\n合计入库 {total_saved} 张，库现有 {len(book.files())} 张")
        _print_coverage(book)
    return {"saved": total_saved, "stats": stats, "total": len(book.files())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从斗图网补二次元表情包（只有判定 anime=true 的才入库）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"本次总入库上限，默认 {DEFAULT_LIMIT}")
    parser.add_argument("--per-keyword", type=int, default=PER_KEYWORD,
                        help=f"每个关键词目标入库张数，默认 {PER_KEYWORD}")
    parser.add_argument("--dry", action="store_true", help="只判定，不写入")
    parser.add_argument("--keywords", default="",
                        help="追加关键词，逗号分隔（默认 16 个之外再抓）")
    parser.add_argument("--only-keywords", action="store_true",
                        help="只用 --keywords 给的关键词，不跑默认 16 个")
    parser.add_argument("--base", default=BASE, help="项目根目录（测试用）")
    parser.add_argument("--config", default=None,
                        help="config.json 路径（测试用，默认 <base>/data/config.json）")
    args = parser.parse_args(argv)

    base = os.path.abspath(args.base)
    keywords = [] if args.only_keywords else list(DEFAULT_KEYWORDS)
    for extra in str(args.keywords or "").replace("，", ",").split(","):
        extra = extra.strip()
        if extra and extra not in keywords:
            keywords.append(extra)
    if not keywords:
        print("没有关键词可抓（--only-keywords 需要配 --keywords）")
        return 2
    if not os.path.isdir(os.path.join(base, "data", "stickers")):
        print(f"找不到表情包目录：{os.path.join(base, 'data', 'stickers')}")
        return 2

    cfg = load_llm(args.config or os.path.join(base, "data", "config.json"))
    print(f"关键词 {len(keywords)} 个，目标每词 {args.per_keyword} 张，"
          f"本次上限 {args.limit} 张{'（dry run）' if args.dry else ''}")
    try:
        run(base, cfg, keywords, per_keyword=args.per_keyword,
            limit=args.limit, dry=args.dry)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
