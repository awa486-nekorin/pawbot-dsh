"""记忆拼装门面：`build(bot, ctx, user_text)` → messages + 统计。

这是 v2 唯一的对外入口。`agent.py` 里只要判断 `memory.engine == "v2"` 就调这里，
其余一行不用改 —— 这样新旧两条路径能对着同一批数据跑出两个数字。

顺序：**远 → 近**（L3 长期在最前，L0 刚才的对话在最后，离 user 那条最近）。
理由：注意力应该落在「刚才那句」上，长期记忆是背景，不是前台。
旧实现的顺序正好相反（窗口在前、长期在后），于是模型经常拿上周的事接今天的话。
"""

from __future__ import annotations

import time

from .. import ctxwin
from ..log import log
from . import budget as 预算
from . import layers
from .entities import EntityStore

默认总预算 = 8700


def 取配置(bot) -> dict:
    cfg = bot.config
    return {
        "总预算": int(cfg.get("memory.total_chars", 默认总预算) or 默认总预算),
        "l0_limit": int(cfg.get("memory.l0_limit", 100) or 100),
        "l0_hours": float(cfg.get("memory.l0_hours", 6) or 6),
        "l0_chars": int(cfg.get("memory.l0_chars", 5000) or 5000),
        "l1_chars": int(cfg.get("memory.l1_chars", 600) or 600),
        "l2_chars": int(cfg.get("memory.l2_chars", 1600) or 1600),
        "l2_days": int(cfg.get("memory.l2_days", 2) or 2),
        "l3_chars": int(cfg.get("memory.l3_chars", 1500) or 1500),
    }


def 取实体库(bot) -> EntityStore:
    """一个 Bot 一个实例（挂在 bot 上，别每次 new）。"""
    库 = getattr(bot, "entities", None)
    if 库 is None:
        库 = EntityStore(getattr(bot, "base_dir", "."), bot.config)
        try:
            bot.entities = 库
        except Exception:
            pass
    return 库


def _按行截断(文本: str, 预算: int) -> str:
    """从**末尾**按整行取，不切半句（旧实现切半句，注入的就是碎片）。"""
    if len(文本) <= 预算:
        return 文本
    行 = 文本.splitlines()
    取: list[str] = []
    用 = 0
    for ln in reversed(行):
        if 用 + len(ln) + 1 > 预算:
            break
        取.append(ln)
        用 += len(ln) + 1
    return "\n".join(reversed(取))


_头 = {
    "L3": ("## 你还记得的（长期）\n"
           "这些是你自己攒下来的事和人，不是别人刚说的。别复述成清单，需要的时候自然带出来。\n"
           "下面这些可能来自私聊、也可能来自群裡，看每条行尾的来源标注（「私聊里说的」「群裡」等）。"
           "私聊里的事不要说成「群裡有人說」，要说「你私下跟我说过」或「对方私下跟我说过」。"),
    "L2": ("## 这些天\n"
           "按天记的，用来知道「昨天到哪了」。别把日期念出来。\n"
           "里面的事可能来自私聊、也可能来自群裡，看行内写的出处。"
           "私聊里的事不要说成「群裡有人說」，要说「你私下跟我说过」。"),
    "L1": ("## 刚才这一轮\n"
           "刚刚发生的那一小段，接着它说话。"),
}


def build(bot, ctx, user_text: str, *, now: float | None = None) -> tuple[list[dict], dict]:
    """返回 (要拼进 messages 的 system 块列表, 统计)。"""
    now = float(now or time.time())
    cfg = 取配置(bot)
    sid = getattr(ctx, "session_id", "") or ""
    库 = 取实体库(bot)

    # ---------- 第一遍：各层按自己的单层上限取 ----------
    l0文本, l0统计 = layers.l0_window(bot, sid, limit=cfg["l0_limit"],
                                      hours=cfg["l0_hours"], chars=cfg["l0_chars"])
    l1文本, l1统计 = layers.l1_turn(bot, sid, chars=cfg["l1_chars"])
    l2文本, l2统计 = layers.l2_days(bot, sid, 库, days=cfg["l2_days"],
                                    chars=cfg["l2_chars"], now=now)

    在场 = []
    try:
        for r in bot.store.window(sid)[-40:]:
            uid = str(r.get("uid") or "").strip()
            if uid and r.get("role") != "assistant":
                在场.append(uid)
        if getattr(ctx, "user_id", None):
            在场.append(str(ctx.user_id))
    except Exception:
        pass

    l3文本, l3统计 = layers.l3_entities(bot, ctx, user_text, 库,
                                        budget=cfg["l3_chars"], uids=在场, now=now)

    # ---------- 闸门 ----------
    请求 = {"L0": len(l0文本), "L1": len(l1文本), "L2": len(l2文本), "L3": len(l3文本)}
    分配, 砍 = 预算.分配(请求, 总预算=cfg["总预算"],
                         上限={"L0": cfg["l0_chars"], "L1": cfg["l1_chars"],
                               "L2": cfg["l2_chars"], "L3": cfg["l3_chars"]})

    # ---------- 第二遍：按分配结果重取/截断 ----------
    if 分配["L0"] < len(l0文本):
        l0文本, l0统计 = layers.l0_window(bot, sid, limit=cfg["l0_limit"],
                                          hours=cfg["l0_hours"], chars=分配["L0"])
    if 分配["L1"] < len(l1文本):
        l1文本 = _按行截断(l1文本, 分配["L1"])
    if 分配["L2"] < len(l2文本):
        l2文本 = _按行截断(l2文本, 分配["L2"])
    if 分配["L3"] < len(l3文本):
        l3文本, l3统计 = layers.l3_entities(bot, ctx, user_text, 库,
                                            budget=分配["L3"], uids=在场, now=now)

    # ---------- 拼 ----------
    块: list[dict] = []
    if l3文本.strip():
        块.append({"role": "system", "content": _头["L3"] + "\n" + l3文本.strip()})
    if l2文本.strip():
        块.append({"role": "system", "content": _头["L2"] + "\n" + l2文本.strip()})
    if l1文本.strip():
        块.append({"role": "system", "content": _头["L1"] + "\n" + l1文本.strip()})
    if l0文本.strip():
        # L0 的 header 归 A 统一维护（ctxwin.L0_HEADER）；A 还没加时退回这份内联文字。
        头l0 = getattr(ctxwin, "L0_HEADER", None) or (
            "## 这个群最近的聊天记录（从旧到新）\n"
            "看语气和话题用，不要逐条回应，也不要引用下面的话来证明你记得。"
            "括号里是 QQ 尾号，按尾号认人。"
            "开头写「我（大肥鱼）」的行全是**你自己发出去的消息**，不是别人说的。\n")
        # ⚠️ L0_HEADER 結尾沒有換行，這裡必須自己補，否則第一條記錄會黏在說明最後一行上。
        块.append({"role": "system",
                   "content": 头l0.rstrip() + "\n" + l0文本.strip()})

    # 梗 / 黑话：只在最近几条或这一句命中触发词时才注入，平时不占预算。
    # 它不属于四层记忆（不是「发生过的事」），所以单独一块、单独计数。
    梗文本 = ""
    try:
        书 = getattr(bot, "memes", None)
        if 书 is not None:
            最近 = [str(r.get("content") or "") for r in bot.store.window(sid)[-6:]] + [user_text]
            梗文本 = 书.inject_for(最近, int(bot.config.get("memes.inject_limit", 3))) or ""
    except Exception:
        梗文本 = ""
    if 梗文本.strip():
        块.append({"role": "system", "content": 梗文本.strip()})

    try:
        名单 = ctxwin.present(bot.store.window(sid)[-40:])
    except Exception:
        名单 = ""
    if 名单:
        块.append({"role": "system", "content": 名单})

    统计 = {
        "engine": "v2",
        "各层字数": {"L0": len(l0文本), "L1": len(l1文本), "L2": len(l2文本), "L3": len(l3文本)},
        "分配": 分配,
        "请求": 请求,
        "梗库": len(梗文本),
        "总字数": sum(len(m["content"]) for m in 块),
        "砍": 砍,
        "L0": l0统计,
        "L1": l1统计,
        "L2": l2统计,
        "L3": l3统计,
    }
    log.debug(预算.摘要行(分配, 砍))
    return 块, 统计
