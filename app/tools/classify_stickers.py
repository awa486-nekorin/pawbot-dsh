#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按画风清洗表情包库：只留二次元（动漫 / 漫画 / Q 版 / 日系插画）。

    python tools/classify_stickers.py                  # 只判定，不动文件（默认）
    python tools/classify_stickers.py --apply          # 判定 + 移走非二次元
    python tools/classify_stickers.py --limit 5        # 只判前 5 张，先看效果
    python tools/classify_stickers.py --workers 6      # 并发数，默认 4

看图模型固定 gemini-3.8-flash，走 data/config.json 里的 llm.base_url / llm.api_key
（OpenAI 兼容 /v1/chat/completions）。判定结果写到
data/stickers/classify-<时间>.json。

--apply 时非二次元图片移到 data/stickers/removed-nonanime-<时间>/（不直接删，
还能捞回来），index.json 里对应条目删掉、保留的补 "anime": true。看图失败的
unknown 一律不删，留人工确认。

注意：故意不用 pawbot/vision.py 的 describe()，那儿绑的是 llm.vision_model，
跟本脚本要用的模型不是一回事。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

MODEL = "gemini-3.8-flash"
USER_AGENT = "PawBot/1.0"
TIMEOUT = 60
RETRIES = 2
BACKOFF = 2.0
MAX_SIDE = 640
JPEG_QUALITY = 80
# gemini-3.8-flash 会在 reasoning 上烧掉一大半 token，不压 reasoning 的话
# max_tokens=200 会被截断成半截 JSON
MAX_TOKENS = 1200
REASONING_EFFORT = "minimal"

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STICKER_DIR = os.path.join(BASE, "data", "stickers")
INDEX_NAME = "index.json"
REMOVED_PREFIX = "removed-nonanime-"

_ANIME_RULES = """你是表情包画风审核员。判断这张图是不是「二次元」画风的表情包。

算二次元（anime=true）：
- 动漫 / 漫画 / Q 版 / 日系插画风格的画面
- 二次元游戏立绘、动漫角色的表情包（东方 Project、Vocaloid 这类明显是动漫角色的也算）

不算二次元（anime=false）：
- 真人照片、真人照片加字、真人合影
- 现实动物照片
- 纯色块 / 纯文字 / 简单图形加字
- 熊猫头这类「真人脸 + 简笔画身体」的合成图
- 照片拼接、影视剧截图
"""

PROMPT = _ANIME_RULES + """
只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释：
{"anime": true 或 false, "kind": "动漫插画/Q版/漫画/真人照片/照片合成/纯文字/其它", "reason": "不超过20字的中文理由"}"""

# tools/refill_stickers.py 用：一次请求同时拿画风 + 情绪
MOODS = ("开心", "无语", "嫌弃", "震惊", "疑惑", "委屈", "哭哭", "得意",
         "贴贴", "晚安", "摸鱼", "害羞", "生气", "可爱")

MOOD_PROMPT = _ANIME_RULES + """
除了画风，还要判断这张图表达的情绪 mood。mood 只能从下面这些词里选一个：
开心、无语、嫌弃、震惊、疑惑、委屈、哭哭、得意、贴贴、晚安、摸鱼、害羞、生气、可爱

只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释：
{"anime": true 或 false, "mood": "上面列表里的一个词", "kind": "动漫插画/Q版/漫画/真人照片/照片合成/纯文字/其它", "reason": "不超过20字的中文理由"}"""

KINDS = ("动漫插画", "Q版", "漫画", "真人照片", "照片合成", "纯文字", "其它")


# ---------------------------------------------------------------- 配置 / 索引

def load_llm(config_path: str | None = None) -> dict:
    """只取看图要用的 base_url / api_key，别的地方一概不碰。"""
    path = config_path or os.path.join(BASE, "data", "config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    llm = cfg.get("llm") or {}
    base_url = str(llm.get("base_url") or "").strip().rstrip("/")
    api_key = str(llm.get("api_key") or "").strip()
    if not base_url or not api_key:
        raise SystemExit("data/config.json 缺 llm.base_url 或 llm.api_key")
    return {"base_url": base_url, "api_key": api_key}


def sticker_dir(base: str) -> str:
    return os.path.join(base, "data", "stickers")


def load_index(sdir: str) -> dict:
    path = os.path.join(sdir, INDEX_NAME)
    if not os.path.exists(path):
        return {}
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


# ---------------------------------------------------------------- 看图

def image_data_url(path: str) -> str:
    """缩到最长边 640、转 JPEG 80，base64 成 data URL。动图只取第一帧。"""
    from PIL import Image

    with Image.open(path) as im:
        try:
            im.seek(0)
        except (EOFError, ValueError):
            pass
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[-1])
            im = canvas
        else:
            im = im.convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=JPEG_QUALITY)
    return ("data:image/jpeg;base64,"
            + base64.b64encode(buf.getvalue()).decode("ascii"))


def _reply_text(obj) -> str:
    try:
        content = obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "1", "是", "动漫", "二次元"):
            return True
        if s in ("false", "no", "0", "否", "不是", "非二次元"):
            return False
    return None


def parse_reply(text: str) -> dict | None:
    """从模型回复里挖出 JSON；容忍 ```json 围栏和前后废话。"""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    candidates = []
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if fence:
        candidates.append(fence.group(1))
    left, right = s.find("{"), s.rfind("}")
    if left >= 0 and right > left:
        candidates.append(s[left:right + 1])
    candidates.append(s)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        anime = _as_bool(obj.get("anime"))
        if anime is None:
            continue
        kind = str(obj.get("kind") or "").strip() or "其它"
        reason = str(obj.get("reason") or "").strip().replace("\n", " ")
        return {"anime": anime, "kind": kind[:12], "reason": reason[:40]}
    return None


def _post_json(cfg: dict, payload: dict, timeout: int = TIMEOUT) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def _unknown(reason: str, error: str = "") -> dict:
    out = {"anime": None, "kind": "unknown", "reason": reason, "status": "unknown"}
    if error:
        out["error"] = error[:160]
    return out


def parse_mood_reply(text: str) -> dict | None:
    """parse_reply 的加强版：多认一个 mood 字段（原样保留，合法性交给调用方）。"""
    parsed = parse_reply(text)
    if parsed is None:
        return None
    mood = ""
    left = text.find("{")
    right = text.rfind("}")
    raw = text[left:right + 1] if left >= 0 and right > left else text
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = {}
    if isinstance(obj, dict):
        mood = str(obj.get("mood") or "").strip()
    parsed["mood"] = mood[:12]
    return parsed


def _ask(path: str, cfg: dict, prompt: str, parser, timeout: int = TIMEOUT) -> dict:
    """读图 → 发一次（带重试）→ 交给 parser 解析。失败返回 unknown。"""
    try:
        data_url = image_data_url(path)
    except Exception as exc:                            # 坏图 / 不支持格式
        return _unknown("读图失败", f"{type(exc).__name__}: {exc}")

    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }

    last = "未知错误"
    for attempt in range(RETRIES + 1):
        try:
            obj = _post_json(cfg, payload, timeout=timeout)
            parsed = parser(_reply_text(obj))
            if parsed is None:
                last = "模型回复无法解析"
                payload["max_tokens"] = min(4000, int(payload["max_tokens"]) * 2)
                if attempt < RETRIES:
                    time.sleep(BACKOFF)
                    continue
                return _unknown(last)
            parsed["status"] = "ok"
            return parsed
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            retryable = exc.code == 429 or exc.code >= 500
            if not retryable or attempt >= RETRIES:
                return _unknown("请求失败", last)
        except Exception as exc:                        # 超时 / 断网 / 坏 JSON
            last = f"{type(exc).__name__}: {exc}"
            if attempt >= RETRIES:
                return _unknown("请求失败", last)
        time.sleep(BACKOFF)
    return _unknown("请求失败", last)


def classify_one(path: str, cfg: dict, timeout: int = TIMEOUT) -> dict:
    return _ask(path, cfg, PROMPT, parse_reply, timeout=timeout)


def classify_sticker(path: str, cfg: dict, timeout: int = TIMEOUT) -> dict:
    """单图分类（画风 + mood），给 refill_stickers 用。

    返回 {"anime": True/False/None, "mood": str, "kind": ..., "reason": ..., "status": ...}。
    mood 可能是空串或集合外的词，由调用方兜底。
    """
    out = _ask(path, cfg, MOOD_PROMPT, parse_mood_reply, timeout=timeout)
    out.setdefault("mood", "")
    return out


# ---------------------------------------------------------------- 主流程

def _disk_files(sdir: str) -> set[str]:
    if not os.path.isdir(sdir):
        return set()
    return {name for name in os.listdir(sdir)
            if name != INDEX_NAME and not name.lower().endswith(".json")
            and os.path.isfile(os.path.join(sdir, name))}


def judge_all(base: str, cfg: dict, workers: int = 4,
              limit: int | None = None, index: dict | None = None) -> dict:
    sdir = sticker_dir(base)
    idx = index if index is not None else load_index(sdir)
    on_disk = _disk_files(sdir)

    names = [name for name in idx if name in on_disk]
    missing = [name for name in idx if name not in on_disk]
    untracked = sorted(name for name in on_disk if name not in idx)
    if limit is not None:
        names = names[:limit]

    results: dict[str, dict] = {}
    lock = threading.Lock()
    if names:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            futures = {
                pool.submit(classify_one, os.path.join(sdir, name), cfg): name
                for name in names
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:                # 兜底，不该发生
                    res = _unknown("判定异常", f"{type(exc).__name__}: {exc}")
                with lock:
                    results[name] = res

    # 按 index 顺序排，输出稳定
    ordered = {name: results[name] for name in idx if name in results}
    counts = {
        "total": len(ordered),
        "anime": sum(1 for r in ordered.values() if r.get("anime") is True),
        "nonanime": sum(1 for r in ordered.values() if r.get("anime") is False),
        "unknown": sum(1 for r in ordered.values() if r.get("anime") is None),
    }
    return {
        "results": ordered,
        "counts": counts,
        "missing_files": missing,
        "untracked_files": untracked,
    }


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _display_width(text))


def print_table(report: dict) -> None:
    rows = []
    for name, res in report["results"].items():
        if res.get("anime") is True:
            flag = "保留"
        elif res.get("anime") is False:
            flag = "删除"
        else:
            flag = "待定"
        rows.append((flag, str(res.get("kind") or "?"), name,
                     str(res.get("reason") or "")))
    if not rows:
        print("（没有可判定的图片）")
        return
    widths = [max(_display_width(row[i]) for row in rows + [("状态", "kind", "文件", "理由")])
              for i in range(4)]
    header = ("状态", "kind", "文件", "理由")
    print("  ".join(_pad(header[i], widths[i]) for i in range(4)))
    print("  ".join("-" * widths[i] for i in range(4)))
    for row in rows:
        print("  ".join(_pad(row[i], widths[i]) for i in range(4)))


def print_summary(report: dict) -> None:
    c = report["counts"]
    print(f"\n判定 {c['total']} 张：保留 {c['anime']} 张，"
          f"删除 {c['nonanime']} 张，unknown {c['unknown']} 张")
    unknown = [name for name, r in report["results"].items() if r.get("anime") is None]
    if unknown:
        print("unknown（没删，留人工确认）：" + "、".join(unknown))
    if report["missing_files"]:
        print("index 里有、磁盘上没有（跳过）：" + "、".join(report["missing_files"]))
    if report["untracked_files"]:
        print("磁盘上有、index 里没有（未判定）：" + "、".join(report["untracked_files"]))


def apply_removals(base: str, report: dict, stamp: str) -> tuple[str, list[str]]:
    sdir = sticker_dir(base)
    removed = [name for name, r in report["results"].items()
               if r.get("anime") is False]
    out_dir = os.path.join(sdir, REMOVED_PREFIX + stamp)

    moved = []
    if removed:
        os.makedirs(out_dir, exist_ok=True)
        for name in removed:
            src = os.path.join(sdir, name)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(out_dir, name)
            shutil.move(src, dst)
            moved.append(name)

    idx = load_index(sdir)
    moved_set = set(moved)
    new_idx = {}
    for name, entry in idx.items():
        if name in moved_set:
            continue
        item = dict(entry) if isinstance(entry, dict) else {"desc": str(entry)}
        # 留下的补 anime:true；unknown 不标，等人工确认过再说
        if name not in report["results"] or report["results"][name].get("anime") is True:
            item["anime"] = True
        new_idx[name] = item
    if new_idx or idx:
        write_json_atomic(os.path.join(sdir, INDEX_NAME), new_idx)
    return out_dir, moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按画风筛表情包：只留二次元，--apply 时把其余移到 removed-nonanime-*")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true",
                      help="只判定不动文件（默认）")
    mode.add_argument("--apply", action="store_true",
                      help="移动非二次元图片并更新 index.json")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多判定几张（按 index 顺序）")
    parser.add_argument("--workers", type=int, default=4,
                        help="并发请求数，默认 4")
    parser.add_argument("--base", default=BASE,
                        help="项目根目录（测试用，默认本仓库）")
    parser.add_argument("--config", default=None,
                        help="config.json 路径（测试用，默认 <base>/data/config.json）")
    args = parser.parse_args(argv)

    base = os.path.abspath(args.base)
    sdir = sticker_dir(base)
    if not os.path.isdir(sdir):
        print(f"找不到表情包目录：{sdir}")
        return 2
    cfg = load_llm(args.config or os.path.join(base, "data", "config.json"))
    stamp = time.strftime("%Y%m%d-%H%M%S")

    report = judge_all(base, cfg, workers=args.workers, limit=args.limit)
    print_table(report)
    print_summary(report)

    payload = {
        "created": stamp,
        "model": MODEL,
        "mode": "apply" if args.apply else "dry",
        "limit": args.limit,
        "counts": report["counts"],
        "missing_files": report["missing_files"],
        "untracked_files": report["untracked_files"],
        "results": report["results"],
    }
    out_path = os.path.join(sdir, f"classify-{stamp}.json")
    write_json_atomic(out_path, payload)
    print(f"\n判定结果：{out_path}")

    if args.apply:
        out_dir, moved = apply_removals(base, report, stamp)
        print(f"已移动 {len(moved)} 张到：{out_dir}")
        on_disk = _disk_files(sdir)
        idx = load_index(sdir)
        print(f"清理后 index {len(idx)} 条 / 磁盘 {len(on_disk)} 个文件")
    else:
        print("（dry run：没有动任何文件；确认无误后加 --apply）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
