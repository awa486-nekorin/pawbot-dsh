"""四层记忆各自的取数逻辑。

每层只做一件事：把「这一层该给模型看的东西」取出来，附带自己的字数。
预算、顺序、拼装都不在这里 —— 那是 `assemble.py` 和 `budget.py` 的事。
这样每层能单独测，也不会再出现「谁都能往 prompt 里塞东西」。
"""

from __future__ import annotations

import time

from .. import ctxwin
from .entities import _天


class _配置覆盖:
    """把 config 包一层，只改几个键 —— ctxwin.build 只认 config.get。"""

    def __init__(self, 底, 覆盖: dict):
        self._底 = 底
        self._覆 = dict(覆盖)

    def get(self, key, default=None):
        if key in self._覆:
            return self._覆[key]
        return self._底.get(key, default)

    def __getitem__(self, key):
        return self.get(key)


def l0_window(bot, session_id: str, *, limit: int = 100, hours: float = 6.0,
              chars: int = 5000) -> tuple[str, dict]:
    """L0 在场：最近的原话。**条数 + 时间双上限**（现在是只限条数，于是 300 条盖了 14 小时）。"""
    cfg = bot.config
    rows = [r for r in bot.store.window(session_id) if (r.get("content") or "").strip()]
    if not rows:
        return "", {"lines": 0, "chars": 0, "span_hours": 0, "dropped_by_time": 0}

    ref = float(rows[-1].get("ts") or time.time())
    if hours and hours > 0:
        起点 = ref - hours * 3600
        保留 = [r for r in rows if float(r.get("ts") or ref) >= 起点]
        按时间丢 = len(rows) - len(保留)
        # 至少留 12 条，否则一安静就把刚才的话也丢了
        rows = 保留 if len(保留) >= 12 else rows[-12:]
    else:
        按时间丢 = 0

    覆盖 = {
        "chat.context_messages": max(1, int(limit)),
        "chat.context_chars": max(400, int(chars)),
        "chat.time_decay_hours": float(cfg.get("chat.time_decay_hours", 6) or 6),
    }
    文本, stats = ctxwin.build(rows, _配置覆盖(cfg, 覆盖))
    stats = dict(stats)
    stats["dropped_by_time"] = 按时间丢
    return 文本, stats


def l1_turn(bot, session_id: str, *, chars: int = 600) -> tuple[str, dict]:
    """L1 这一轮：**本轮增量**小结。v2 里它不再包含历史 —— 历史在 L2/L3。"""
    文本 = " ".join((bot.store.turn_summary(session_id) or "").split())
    if not 文本:
        return "", {"chars": 0}
    if len(文本) > chars:
        文本 = 文本[:chars].rstrip() + "…"
    return 文本, {"chars": len(文本)}


def l2_days(bot, session_id: str, entities, *, days: int = 2,
            chars: int = 1600, now: float | None = None) -> tuple[str, dict]:
    """L2 这些天：日摘要（entities 里 kind=day）+ 她自己写的日记。"""
    now = float(now or time.time())
    段: list[str] = []

    日摘要 = []
    for row in entities.load(now):
        if row.get("kind") != "day":
            continue
        if float(row.get("ts") or 0) < now - days * 86400:
            continue
        日摘要.append(row)
    日摘要.sort(key=lambda r: float(r.get("ts") or 0))
    for row in 日摘要:
        正文 = str(row.get("text") or "").strip()
        if 正文:
            # 來源標註：日摘要可能同時含私聊與群裡的事。不標出來，模型就會把
            # 私聊裡的事說成「群裡有人說」（這是本次修的一類混淆）。
            from .entities import _来源标注
            标 = _来源标注(str(row.get("source") or ""), row.get("sources"))
            段.append(f"{_天(float(row.get('ts') or now))}：{正文}{标}")

    try:
        日记 = bot.diary.recent(days) if getattr(bot, "diary", None) is not None else []
    except Exception:
        日记 = []
    已有日期 = {s.split("：", 1)[0] for s in 段}
    for item in 日记 or []:
        try:
            日期, 正文 = item
        except (TypeError, ValueError):
            continue
        if 日期 in 已有日期:
            continue
        正文 = " ".join(str(正文 or "").split())
        if 正文:
            段.append(f"{日期}（日记）：{正文}")

    if not 段:
        return "", {"chars": 0, "days": 0}
    文本 = "\n".join(段)
    if len(文本) > chars:
        文本 = 文本[-chars:]        # 保留最近几天
    return 文本, {"chars": len(文本), "days": len(段)}


def l3_entities(bot, ctx, query: str, entities, *, budget: int = 1500,
                uids=None, now: float | None = None) -> tuple[str, dict]:
    """L3 长期：按「实体优先 → 话题 → 衰减」召回。"""
    now = float(now or time.time())
    在场 = set()
    for u in (uids or []):
        if str(u or "").strip():
            在场.add(str(u))
    uid = str(getattr(ctx, "user_id", "") or "")
    if uid:
        在场.add(uid)
    gid = str(getattr(ctx, "group_id", "") or "")

    选中, stats = entities.recall(query=query, uids=在场, group_id=gid,
                                  budget=budget, now=now)
    行 = [r["_文本"] for r in 选中 if r.get("_文本")]
    if not 行:
        return "", {**stats, "chars": 0}
    文本 = "\n".join(行)
    return 文本, {**stats, "chars": len(文本)}
