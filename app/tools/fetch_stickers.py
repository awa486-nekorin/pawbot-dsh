#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一次性批量入库：从斗图网（doutupk.com）按标签抓表情包进 data/stickers/。

    python tools/fetch_stickers.py

每个标签抓 3 张，文件名 <标签>_<sha1前6>.<扩展名>；主标签之外再补 1-2 个
近义情绪标签。图片内容看不到，标签只能按搜索关键词推断，desc 统一写来源。
重复运行是安全的：按 sha1 去重，已存在的图只会合并标签。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pawbot.stickers import StickerBook  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PER_TAG = 3

# 主标签 -> 近义情绪标签（pick 时按标签重叠打分，多打几个更容易被挑中）
TAGS = {
    "熊猫头": ["搞笑", "无语"],
    "牢大": ["嘲讽", "搞笑"],
    "坤坤": ["搞笑", "嘲讽"],
    "阴阳怪气": ["嘲讽", "嫌弃"],
    "无语": ["嫌弃", "沉默"],
    "震惊": ["疑惑", "离谱"],
    "委屈": ["难过", "哭哭"],
    "得意": ["嚣张", "开心"],
    "嘲讽": ["嫌弃", "阴阳怪气"],
    "哭哭": ["委屈", "难过"],
    "摸鱼": ["躺平", "疲惫"],
    "晚安": ["困", "睡觉"],
    "开心": ["笑死", "高兴"],
    "嫌弃": ["无语", "鄙视"],
    "疑惑": ["问号", "好奇"],
    "笑死": ["开心", "搞笑"],
    "破防": ["委屈", "难过"],
    "贴贴": ["亲昵", "可爱"],
    "溜了": ["跑路", "搞笑"],
    "你礼貌吗": ["无语", "嫌弃"],
}


def main() -> int:
    book = StickerBook(BASE, {
        "persona.stickers_dir": "data/stickers",
        "persona.stickers_max": 400,
        "persona.sticker_fetch": True,
    })
    known = set(book.files())
    total_new = 0
    total_miss = 0

    for tag, extras in TAGS.items():
        saved = book.fetch_from_doutu(tag, PER_TAG, extra_tags=extras)
        new = [name for name in saved if name not in known]
        known.update(saved)
        miss = max(0, PER_TAG - len(saved))
        total_new += len(new)
        total_miss += miss
        listed = ", ".join(saved) if saved else "无"
        print(f"[{tag}] 新入库 {len(new)} / 目标 {PER_TAG}  {listed}")
        time.sleep(0.4)                       # 别把人家站刷太狠

    print(f"\n合计：新入库 {total_new} 张，没抓到 {total_miss} 张，"
          f"库现有 {len(book.files())} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
