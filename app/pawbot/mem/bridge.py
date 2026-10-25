"""跨会话记忆桥 —— 让私聊里能想起「群里刚才在聊什么」。

为什么需要这一层：
    L0 只取**本会话**的原话，L1/L2 也只按本会话的 session_id 取。
    于是私聊时她完全不知道群里刚发生的事，反过来群里也接不上私聊的话头。
    longmem 的 episode 虽然是跨会话的，但要「关键词撞上」才召回，
    接不住「刚才群里说的那个」这种指着刚才的话。

方向（默认单向，防说漏）：
    memory.cross_session = "to_private"（默认）  群 → 私聊
    memory.cross_session = "both"                双向（私聊内容也会进群，慎用）
    memory.cross_session = "off"                 关掉

其它配置：
    memory.cross_hours  （默认 6）   只看最近几小时
    memory.cross_lines  （默认 12）  最多几条原话
    memory.cross_chars  （默认 700） 整块字数上限
"""

from __future__ import annotations

import time

_头_群到私聊 = (
    "## 群里刚才在聊什么（内部记忆，不是这个人私下跟你说的）\n"
    "这是你自己在群里看到的，用来接话、别露馅；不要复述成「我听说」。\n"
    "别人在群里的私事不要原样说给这个人听，点到为止。\n"
)

_头_私聊到群 = (
    "## 刚才有人私下跟你说的（内部记忆，群里别说漏）\n"
    "这些是私聊里的内容，群里聊天时只用来理解对方，不要当众复述。\n"
)

_默认 = {"mode": "to_private", "hours": 6.0, "lines": 12, "chars": 700}


def _cfg(bot, key, default=None):
    return bot.config.get(f"memory.cross_{key}", default)


def _数字(bot, key, 默认):
    try:
        return type(默认)(_cfg(bot, key, 默认) or 默认)
    except (TypeError, ValueError):
        return 默认


def _rows(bot, sid: str, limit: int = 300) -> list[dict]:
    try:
        return bot.store.window(sid, limit) or []
    except Exception:
        return []


def _挑群(bot, uid: str) -> tuple[str, list[dict]] | None:
    """选一个群：优先「当前这个人在里面最近说过话」的，其次最近更新的。"""
    数据 = getattr(getattr(bot, "store", None), "data", None) or {}
    try:
        会话们 = list(数据.keys())
    except Exception:
        return None
    最好 = None
    最好分 = -1.0
    for sid in 会话们:
        if not str(sid).startswith("group_"):
            continue
        行 = _rows(bot, str(sid))
        if not 行:
            continue
        时间 = float(行[-1].get("ts") or 0)
        他在 = False
        if uid:
            他在 = any(str(r.get("uid") or "") == uid for r in 行[-80:])
        分 = (2.0 if 他在 else 0.0) + 时间 / 1e12
        if 分 > 最好分:
            最好分 = 分
            最好 = (str(sid), 行)
    return 最好


def _挑私聊(bot) -> tuple[str, list[dict]] | None:
    数据 = getattr(getattr(bot, "store", None), "data", None) or {}
    try:
        会话们 = [str(s) for s in 数据.keys() if str(s).startswith("private_")]
    except Exception:
        return None
    最好 = None
    最好时间 = -1.0
    for sid in 会话们:
        行 = _rows(bot, sid)
        if not 行:
            continue
        时间 = float(行[-1].get("ts") or 0)
        if 时间 > 最好时间:
            最好时间 = 时间
            最好 = (sid, 行)
    return 最好


def _截(text: str, n: int = 70) -> str:
    平 = " ".join(str(text or "").split())
    return 平 if len(平) <= n else 平[:n] + "…"


def _拼行(行: list[dict], *, 条数: int, 起点: float, 自称: str) -> list[str]:
    出: list[str] = []
    for r in 行:
        if float(r.get("ts") or 0) < 起点:
            continue
        正文 = _截(r.get("content") or "")
        if not 正文:
            continue
        if r.get("role") == "assistant":
            谁 = 自称 or "我"
        else:
            谁 = _截(r.get("who") or "", 12) or "有人"
        出.append(f"{谁}：{正文}")
    return 出[-条数:]


def build(bot, ctx, *, now: float | None = None) -> str:
    """返回要注入的 system 文本；不该注入时返回空串。永不多抛。"""
    try:
        return _build(bot, ctx, now=now)
    except Exception:
        return ""


def _build(bot, ctx, *, now: float | None = None) -> str:
    mode = str(_cfg(bot, "mode", _默认["mode"]) or "").strip().lower()
    if mode in ("", "off", "none", "false", "0", "no"):
        return ""
    现在 = float(now or time.time())
    小时 = _数字(bot, "hours", _默认["hours"])
    条数 = int(_数字(bot, "lines", _默认["lines"]))
    预算 = int(_数字(bot, "chars", _默认["chars"]))
    if 小时 <= 0 or 条数 <= 0 or 预算 <= 0:
        return ""
    起点 = 现在 - 小时 * 3600
    自称 = ""
    try:
        自称 = str(bot.config.get("bot.nickname", "") or "")
    except Exception:
        自称 = ""

    if getattr(ctx, "is_group", False):
        if mode != "both":
            return ""
        选 = _挑私聊(bot)
        if not 选:
            return ""
        _, 行 = 选
        文本 = "\n".join(_拼行(行, 条数=条数, 起点=起点, 自称=自称))
        if not 文本:
            return ""
        return (_头_私聊到群 + 文本)[:预算]

    uid = str(getattr(ctx, "user_id", "") or "")
    选 = _挑群(bot, uid)
    if not 选:
        return ""
    群号, 行 = 选
    文本 = "\n".join(_拼行(行, 条数=条数, 起点=起点, 自称=自称))
    if not 文本:
        return ""
    尾巴 = f"\n（出自群 {群号[6:]}；只是背景，别复述，也别当成这个人私下跟你说的。）"
    return (_头_群到私聊 + 文本 + 尾巴)[:预算]
