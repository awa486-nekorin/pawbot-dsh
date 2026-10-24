#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""严格二次元复检：把表情包库里「已标 anime:true」的图按更严的标准重判一遍。

上一轮 tools/classify_stickers.py 的口径太松（Q 版 / 卡通插画都算），结果
真人偶像的 Q 版化画像、粗糙手绘梗图也混进来了。这个脚本只处理现有
index.json 里 anime=true 的条目，用一套更严的 prompt 重新判定：
角色必须是虚构的动漫/漫画/游戏/轻小说角色，或明显的日系插画/同人图。

    python tools/strict_anime_pass.py               # 只判定、只出表格（默认）
    python tools/strict_anime_pass.py --apply       # 搬走 false、更新 index.json
    python tools/strict_anime_pass.py --limit 5     # 只判前 5 张，先看效果
    python tools/strict_anime_pass.py --only-unchecked --apply
                                                    # 只复核还没 strict 标记的
                                                    # （补图后用）

看图模型、凭据、缩图参数沿用 pawbot/animecheck.py —— 但那边的判定规则不动。
失败（超时 / 429、5xx 重试用尽 / 回复解析不出）一律 unknown；unknown 不删，
留在库里等人工看。

--apply 时 anime=false 的图移到 data/stickers/removed-nonanime-strict-<时间>/
（不直接删，可逆），index.json 删掉对应条目；留下的条目保持 anime:true，
desc 后面追加「｜strict: <kind>」方便日后追溯（已有 strict 标记就不重复加）。
结果报告写到 data/stickers/strict-pass-<时间>.json（dry 不写任何文件）。
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
TIMEOUT = 40
BACKOFF = 1.0
MAX_SIDE = 640
JPEG_QUALITY = 80
# gemini-3.8-flash 的思考 token 会吃掉额度，MAX_TOKENS 压太小会被截成半截 JSON
MAX_TOKENS = 1200
REASONING_EFFORT = "minimal"
DEFAULT_WORKERS = 3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_NAME = "index.json"
REMOVED_PREFIX = "removed-nonanime-strict-"
REPORT_PREFIX = "strict-pass-"
STRICT_MARK = "｜strict: "

STRICT_PROMPT = """判斷這張圖是不是「日系二次元」：角色必須是虛構的動漫/漫畫/遊戲/輕小說角色，
或明顯的日系插畫/同人圖。
以下一律算 false：
- 真人照片、真人照片加字、真人視頻截圖
- 現實人物（明星、網紅、運動員）的 Q 版化/卡通化畫像
- 美式/歐美卡通（貓和老鼠、辛普森等）截圖
- 熊貓頭、真人臉合成、貼吧滑稽
- 粗糙手繪線稿、簡筆畫表情包、純色塊加字、emoji 改圖
- 實物照片（玩偶、手辦、周邊的實拍）
只輸出 JSON：{"anime": true/false, "kind": "日系插畫/動漫角色/Q版/真人Q版化/美式卡通/照片/簡筆畫/實物/其它", "reason": "不超过20字"}
不要 markdown 代码块、不要任何解释。"""

KINDS = ("日系插畫", "動漫角色", "Q版", "真人Q版化", "美式卡通",
         "照片", "簡筆畫", "實物", "其它")


# ---------------------------------------------------------------- 配置 / 索引

def load_llm(config_path: str | None = None) -> dict:
    """只取看图要用的 base_url / api_key，绝不打印 key。"""
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


def write_json_atomic(path: str, obj, indent: int | None = 2) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
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
        if s in ("true", "yes", "1", "是", "日系二次元"):
            return True
        if s in ("false", "no", "0", "否", "不是"):
            return False
    return None


def parse_strict(text: str) -> dict | None:
    """从回复里挖 JSON，只认 anime 字段。容忍 ```json 围栏和前后废话。"""
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
        kind = str(obj.get("kind") or "").strip().replace("\n", " ") or "其它"
        reason = str(obj.get("reason") or "").strip().replace("\n", " ")
        return {"anime": anime, "kind": kind[:12], "reason": reason[:20]}
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


def judge_one(path: str, cfg: dict, timeout: int = TIMEOUT) -> dict:
    """判一张。只有 429 / 5xx 重试一次；超时和坏回复直接 unknown。"""
    try:
        data_url = image_data_url(path)
    except Exception as exc:
        return _unknown("读图失败", f"{type(exc).__name__}: {exc}")

    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": STRICT_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }

    for attempt in range(2):
        try:
            obj = _post_json(cfg, payload, timeout=timeout)
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or (500 <= exc.code < 600
                                            and exc.code not in (501, 505))
            if retryable and attempt == 0:
                time.sleep(BACKOFF)
                continue
            return _unknown("请求失败", f"HTTP {exc.code}")
        except Exception as exc:                        # 超时 / 断网 / 坏 JSON
            return _unknown("请求失败", f"{type(exc).__name__}: {exc}")
        parsed = parse_strict(_reply_text(obj))
        if parsed is None:
            return _unknown("模型回复无法解析")
        parsed["status"] = "ok"
        return parsed
    return _unknown("请求失败", "重试用尽")


# ---------------------------------------------------------------- 主流程

def is_checked(entry) -> bool:
    """这条已经过严格复核了吗？（desc 里有 strict 标记）"""
    return isinstance(entry, dict) and STRICT_MARK in str(entry.get("desc") or "")


def judge_all(base: str, cfg: dict, workers: int = DEFAULT_WORKERS,
              limit: int | None = None, timeout: int = TIMEOUT,
              index: dict | None = None,
              only_unchecked: bool = False) -> dict:
    """只判 index 里 anime=true 且磁盘上存在的图。

    only_unchecked=True 时再跳过一次已带 strict 标记的条目——补图 / 人工改过
    之后只复核新的，别让边界图反复翻面。
    """
    sdir = sticker_dir(base)
    idx = index if index is not None else load_index(sdir)

    def wanted(entry) -> bool:
        if not (isinstance(entry, dict) and entry.get("anime") is True):
            return False
        return not (only_unchecked and is_checked(entry))

    names = [name for name, entry in idx.items()
             if wanted(entry) and os.path.isfile(os.path.join(sdir, name))]
    missing = [name for name, entry in idx.items()
               if wanted(entry) and not os.path.isfile(os.path.join(sdir, name))]
    if limit is not None:
        names = names[:max(0, int(limit))]

    results: dict[str, dict] = {}
    lock = threading.Lock()
    if names:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            futures = {
                pool.submit(judge_one, os.path.join(sdir, name), cfg, timeout): name
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

    ordered = {name: results[name] for name in idx if name in results}
    counts = {
        "total": len(ordered),
        "anime": sum(1 for r in ordered.values() if r.get("anime") is True),
        "nonanime": sum(1 for r in ordered.values() if r.get("anime") is False),
        "unknown": sum(1 for r in ordered.values() if r.get("anime") is None),
    }
    return {"results": ordered, "counts": counts, "missing_files": missing}


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
        rows.append((flag, name, str(res.get("kind") or "?"),
                     str(res.get("reason") or "")))
    if not rows:
        print("（没有可判定的图片）")
        return
    header = ("状态", "文件", "kind", "理由")
    widths = [max(_display_width(row[i]) for row in rows + [header]) for i in range(4)]
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
        print("index 标了 anime 但磁盘上没有（跳过）：" + "、".join(report["missing_files"]))


def apply_removals(base: str, report: dict, stamp: str) -> tuple[str, list[str]]:
    """搬走 false，更新 index.json。unknown 原样保留。"""
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
            shutil.move(src, os.path.join(out_dir, name))
            moved.append(name)

    idx = load_index(sdir)
    moved_set = set(moved)
    new_idx = {}
    for name, entry in idx.items():
        if name in moved_set:
            continue
        item = dict(entry) if isinstance(entry, dict) else {"desc": str(entry)}
        res = report["results"].get(name)
        if res is not None and res.get("anime") is True:
            item["anime"] = True
            kind = str(res.get("kind") or "").strip()
            desc = str(item.get("desc") or "")
            if kind and "strict:" not in desc:
                item["desc"] = desc + STRICT_MARK + kind if desc else "strict: " + kind
        new_idx[name] = item
    if new_idx or idx:
        write_json_atomic(os.path.join(sdir, INDEX_NAME), new_idx)
    return out_dir, moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="严格二次元复检：只留虚构动漫角色/日系插画，--apply 时搬走其余")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true",
                      help="只判定不动文件（默认）")
    mode.add_argument("--apply", action="store_true",
                      help="移动 nonanime 图片并更新 index.json")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多判定几张（按 index 顺序）")
    parser.add_argument("--only-unchecked", action="store_true",
                        help="只判 desc 里还没有 strict 标记的条目（补图后复核用）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"并发请求数，默认 {DEFAULT_WORKERS}")
    parser.add_argument("--timeout", type=int, default=TIMEOUT,
                        help=f"单张超时秒数，默认 {TIMEOUT}")
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

    report = judge_all(base, cfg, workers=args.workers, limit=args.limit,
                       timeout=args.timeout,
                       only_unchecked=args.only_unchecked)
    print_table(report)
    print_summary(report)

    if args.apply:
        out_dir, moved = apply_removals(base, report, stamp)
        payload = {
            "created": stamp,
            "model": MODEL,
            "mode": "apply",
            "only_unchecked": args.only_unchecked,
            "prompt": STRICT_PROMPT,
            "counts": report["counts"],
            "missing_files": report["missing_files"],
            "removed_dir": out_dir,
            "moved": moved,
            "results": report["results"],
        }
        out_path = os.path.join(sdir, REPORT_PREFIX + stamp + ".json")
        write_json_atomic(out_path, payload)

        idx = load_index(sdir)
        kept = [n for n in idx if n not in set(moved)]
        print(f"\n已移动 {len(moved)} 张到：{out_dir if moved else '（没有需要移动的）'}")
        print(f"保留 {len(kept)} 条；index 现有 {len(idx)} 条")
        print(f"判定结果：{out_path}")
    else:
        print("\n（dry run：没有动任何文件；确认无误后加 --apply）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
