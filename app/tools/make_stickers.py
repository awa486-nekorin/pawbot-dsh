#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成几张占位表情包（纯标准库，不装 Pillow）。

【已不使用】2026-10-21 起表情包改为从斗图网抓真图入库（见 pawbot/doutu.py、
tools/fetch_stickers.py），本脚本生成的自画占位图已从 data/stickers/ 删除。
脚本保留，仅作离线占位图生成工具，不再随启动流程运行。

只是为了让你在没有素材时也能跑通「挑图 → 发送」这条链路。
真要用，把你自己收的表情包丢进 data/stickers/，然后在 index.json 里打标签。
"""

import json
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "stickers")
SIZE = 240


def png(width: int, height: int, pixel) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # 每行开头一个 filter byte
        for x in range(width):
            raw.extend(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def make(name: str, bg: tuple, fg: tuple, mood: str):
    """画一张很简的猫脸：耳朵 + 眼睛 + 嘴。mood 决定眼睛形状。"""
    def px(x: int, y: int):
        cx, cy = SIZE / 2, SIZE / 2 + 10
        r = SIZE * 0.33

        # 脸
        if (x - cx) ** 2 + (y - cy) ** 2 < r ** 2:
            col = fg
        else:
            col = bg

        # 耳朵（两个三角形）
        for ex in (cx - r * 0.72, cx + r * 0.72):
            top = cy - r * 1.55
            base = cy - r * 0.45
            half = r * 0.5
            if base > y > top:
                t = (y - top) / (base - top)
                if abs(x - ex) < half * t:
                    col = fg

        # 眼睛
        for ex in (cx - r * 0.38, cx + r * 0.38):
            if mood == "happy":
                # 弯眼（用两条细横线拼）
                if abs(y - (cy - 6)) < 4 and abs(x - ex) < 16:
                    col = bg
            else:
                if (x - ex) ** 2 + (y - cy + 4) ** 2 < 100:
                    col = bg

        # 嘴：一个小小的 v
        if mood != "blank":
            if abs(y - (cy + 30)) < 3 and abs(x - cx) < 18:
                col = bg
        return col

    data = png(SIZE, SIZE, lambda x, y: bytes(px(x, y)))
    path = os.path.join(OUT, name)
    with open(path, "wb") as f:
        f.write(data)
    return name


def main():
    os.makedirs(OUT, exist_ok=True)
    index = {
        "开心.png": {"tags": ["开心", "笑", "好耶", "高兴"], "desc": "眯眼笑的猫脸"},
        "无语.png": {"tags": ["无语", "尴尬", "沉默", "服了"], "desc": "面无表情的猫脸"},
        "猫.png": {"tags": ["猫", "卖萌", "可爱", "在吗"], "desc": "普通猫脸"},
        "晚安.png": {"tags": ["晚安", "困", "睡觉", "深夜"], "desc": "深夜的猫脸"},
    }

    make("开心.png", (253, 230, 138), (120, 90, 20), "happy")
    make("无语.png", (209, 213, 219), (100, 105, 115), "blank")
    make("猫.png", (167, 243, 208), (20, 90, 70), "normal")
    make("晚安.png", (30, 41, 59), (226, 232, 240), "normal")

    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"已生成 {len(index)} 张占位表情包 → {OUT}")


if __name__ == "__main__":
    main()
