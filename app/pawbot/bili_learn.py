"""从 B 站评论里学「怎么说话」，写成一份可注入 system prompt 的风格笔记。

数据流：
    bili_scroll 刷到的笔记（每篇带 BV 号）
        → 抓该视频的评论区
        → 让模型提炼「说话方式」（语气 / 句式 / 口头禅 / 标点习惯 / 情绪表达）
        → 合并进 data/memory/style_learned.md（去重、限长）
        → persona.build() 每轮注入，下一句就生效

只学「形式」不学「内容」：提示词里明确禁止概括评论内容、禁止带 URL。
"""

from __future__ import annotations

import os
import re

from .log import log

STYLE_FILE = "style_learned.md"
MAX_STYLE_CHARS = 6000      # 文件总长上限
MAX_ITEMS = 80              # 最多保留多少条
INJECT_CHARS = 1600         # 每轮真正注入 prompt 的长度

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s*")


def _cfg(bot, key: str, default=None):
    try:
        return bot.config.get("tools." + key, default)
    except Exception:
        return default


def style_path(bot) -> str:
    return bot.persona.memory_file(STYLE_FILE)


def style_text(bot, limit: int = INJECT_CHARS) -> str:
    """取学到的风格（尾部 limit 字符），给 persona 注入用。"""
    try:
        with open(style_path(bot), "r", encoding="utf-8") as f:
            return f.read().strip()[-max(200, int(limit)):]
    except OSError:
        return ""


def _bvid_of(raw: str) -> str:
    m = _BVID_RE.search(str(raw or ""))
    return m.group(0) if m else ""


def _parse_items(text: str) -> list[str]:
    out: list[str] = []
    for line in str(text or "").splitlines():
        if line.strip().startswith("#"):
            continue
        s = _ITEM_RE.sub("", line.strip()).strip()
        s = re.sub(r"\s+", " ", s)
        if len(s) >= 6 and "http" not in s.lower():
            out.append(s[:160])
    return out


def _merge(bot, items: list[str]) -> int:
    """把新条目合并进风格文件：去重、旧的保留、只留尾部 MAX_ITEMS 条。"""
    path = style_path(bot)
    old: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = _parse_items(f.read())
    except OSError:
        old = []

    merged: list[str] = []
    seen: set[str] = set()
    for it in old + list(items):
        key = re.sub(r"\W+", "", it).lower()[:24]
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(it)
    merged = merged[-MAX_ITEMS:]

    body = "# 学来的说话方式（来自 B 站评论，只学语气句式，别照搬内容）\n" + \
           "\n".join("- " + x for x in merged)
    body = body[:MAX_STYLE_CHARS]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body + "\n")
    os.replace(tmp, path)
    log.info(f"说话风格已更新：{len(merged)} 条 → {path}")
    return len(merged)


async def run_round(bot, *, videos: int | None = None, comments: int | None = None,
                    reason: str = "") -> dict:
    """学一轮：抓最近笔记里视频的评论 → 提炼风格 → 写文件。永不抛。"""
    if not _cfg(bot, "bili_learn_enabled", True):
        return {"ok": False, "reason": "学评论没开（tools.bili_learn_enabled=false）"}

    want_videos = max(1, min(6, int(videos or _cfg(bot, "bili_learn_videos", 3) or 3)))
    want_comments = max(5, min(50, int(comments or _cfg(bot, "bili_learn_comments", 20) or 20)))

    from . import bili_scroll          # 延迟导入，避免循环依赖

    try:
        srv = bili_scroll._srv()
    except Exception as e:
        return {"ok": False, "reason": f"bilibili-api 没装好：{type(e).__name__}: {e}"}

    notes = bili_scroll.recent_notes(bot, limit=want_videos)
    used: list[str] = []
    rows: list[str] = []
    for note in notes:
        try:
            with open(note["path"], "r", encoding="utf-8") as f:
                raw = f.read(4000)
        except OSError:
            continue
        bvid = _bvid_of(raw)
        if not bvid:
            continue
        try:
            got = await bili_scroll._comments(srv, bvid, want_comments)
        except Exception as e:
            log.warn(f"学评论抓 {bvid} 的评论失败：{type(e).__name__} {e}")
            continue
        used.append(bvid)
        for c in got or []:
            text = str(c).replace("\n", " ").strip()
            if len(text) >= 4:
                rows.append(text[:200])

    if not rows:
        return {"ok": False, "reason": "没有可学的评论（先刷一轮 B 站攒点笔记）"}

    sample = "\n".join("- " + t for t in rows[:80])
    prompt = (
        "下面是 B 站视频评论区的真实网友评论。只分析他们**怎么说话**：语气、句式、口头禅、"
        "标点习惯、情绪表达方式，提炼 8~12 条能让「AI 在 QQ 群里聊天更像真人」的具体规则。\n"
        "要求：\n"
        "1) 每条一行，以「- 」开头，不超过 30 字；\n"
        "2) 不要概括评论内容、不要复述具体事件、不要出现任何 URL 或人名；\n"
        "3) 写可执行的说法（例如「多用短句，逗号连写，少用句号」）。\n\n"
        "评论：\n" + sample
    )
    try:
        out = await bot.llm.chat([{"role": "user", "content": prompt}])
    except Exception as e:
        return {"ok": False, "reason": f"提炼失败：{type(e).__name__}: {e}"}

    items = _parse_items(out)[:12]
    if not items:
        return {"ok": False, "reason": "模型没给出可用的风格条目"}

    total = _merge(bot, items)
    log.info(f"学评论一轮（{reason or '手动'}）：{len(used)} 个视频 {len(rows)} 条评论 "
             f"→ 新学 {len(items)} 条，累计 {total} 条")
    return {"ok": True, "videos": used, "comments": len(rows),
            "learned": len(items), "total": total, "style": style_text(bot)}
