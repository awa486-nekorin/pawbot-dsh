"""L1 / L2 的写入：这是 v2 修的那个根问题。

旧实现（`turns.py`）把「上一份小结」喂给判官，产出一份**累积摘要**，
然后同时做两件事：① 覆盖滚动小结（对）② 追加到事件流（错）。

累积摘要进事件流 = 第 N 条包含前 N-1 条。实测 23 条里 6-gram 重复 36%，相邻最高 85.7%。

v2 把两件事拆开：
    L1 滚动小结  ← 只写「这一轮新增了什么」，覆盖，不含历史
    事件流       ← 只写「这一轮新增里值得单独记住的」（重要性 ≥3 或带约定），key 唯一
    日摘要       ← 跨天时把当天的事件合并成一条 kind=day

于是「重复」从设计上就不可能出现：L1 不含历史，事件 key 唯一，日摘要只合并不叠加。
"""

from __future__ import annotations

import time

from .. import judge
from ..log import log
from .entities import EntityStore, _短哈希, _天

# 只总结「这一轮」，**不给上一份小结** —— 少给一个输入，就少一份重复
# 私聊和群聊不是一回事：私聊（session_id 前缀 private_）里发生的事，绝不能被写成群聊发生的。
_提示通用 = (
    "用第三人称写一段小结，供 AI 群友下一轮接话用。\n"
    "**只写这一小段里发生的事**，不要写「之前」「一直」「继续」这类承接词，"
    "也不要复述以前聊过的内容 —— 以前的事另有记录，这里只记新的。\n"
    "必须保留：这一小段在聊什么、谁说了什么观点或偏好、有没有新约定、有没有没解决的问题、情绪走向。\n"
    "可以丢掉：寒暄、重复、表情包、纯语气词。\n"
    "要求：最多 {limit} 字；第三人称；不要分点、不要标题、不要写「总结」二字；只输出 JSON。\n"
    "JSON 里要有这些字段：\n"
    "  summary    上面那段小结（不超过 {limit} 字）\n"
    "  gist       一句话主干：「谁 + 做了什么 + 结果」，**不超过 40 字**，"
    "**不要时间词**（不写「今天」「周三」「9月17日」）。以后这条记忆变旧了只剩这一句，所以它必须自己站得住\n"
    "  points     2~3 个次要细节，每条不超过 30 字；没有就给 []\n"
    "  importance 1~5（1=随口提过，5=约定/关系这种要一直记得的）\n"
    "  keywords   3~8 个关键词\n"
    "  promises   这一小段里新出现的约定；没有就给 []\n"
    "认人：「昵称(尾号1234)」的尾号才是身份，同名不同尾号是不同的人。"
    "「我（大肥鱼）」是这个 AI 群友自己，小结里称「大肥鱼」。\n\n"
    "【这一小段对话】\n{turn}"
)

_PROMPT = {
    "群": "下面是 QQ 群里刚刚发生的这一小段对话。\n" + _提示通用,
    "私": ("下面是「你」和某个人刚才的一段**私聊**。在场只有你（大肥鱼）和对方，没有别的人。\n"
           "小结**必须写清这是私聊里说的**：summary 开头带「（私聊）」或写「对方私下跟我说…」；"
           "绝不要写成「群里有人说…」。\n" + _提示通用),
}


def _选提示(session_id: str) -> str:
    return _PROMPT["私"] if str(session_id or "").startswith("private_") else _PROMPT["群"]


def _渲染(rows: list[dict], max_chars: int = 2500) -> str:
    """判官材料：一行 = [HH:MM] 谁：正文（跨日才带月日）。

    时间基准 = 窗口最后一条的日期（不是「现在」）；截断按整行从尾部取，
    保证时间前缀和说话人标签不被字数截断吃掉。
    """
    from .. import ctxwin
    行 = []
    if not rows:
        return ""
    基准 = float(rows[-1].get("ts") or time.time())
    基准日 = time.localtime(基准)
    for r in rows:
        正文 = str(r.get("content") or "").strip()
        if not 正文:
            continue
        谁 = ctxwin.speaker(r) if (r.get("role") == "assistant" or r.get("who")) else "对方"
        ts = float(r.get("ts") or 基准)
        lt = time.localtime(ts)
        前缀 = time.strftime("%H:%M", lt)
        if lt.tm_yday != 基准日.tm_yday or lt.tm_year != 基准日.tm_year:
            前缀 = time.strftime("%m-%d %H:%M", lt)
        行.append(f"[{前缀}] {谁}：{正文[:200]}")
    if not 行:
        return ""
    全文 = "\n".join(行)
    if len(全文) <= max_chars:
        return 全文
    # 从尾部整行取，别把时间前缀切成两半
    取: list[str] = []
    用 = 0
    for ln in reversed(行):
        if 用 + len(ln) + 1 > max_chars:
            break
        取.append(ln)
        用 += len(ln) + 1
    return "\n".join(reversed(取))


async def _默认问(bot, prompt: str, limit: int) -> dict:
    return await judge.judge_json(bot.config, prompt,
                                  timeout=float(bot.config.get("chat.turn_summary_timeout", 20)))


async def write_turn(bot, session_id: str, rows: list[dict], *,
                     ask=None, now: float | None = None) -> dict:
    """写一轮。返回 {summary, keywords, importance, promises, 写了什么}。

    `ask` 可注入（测试用假判官），签名 `(bot, prompt, limit) -> dict`。
    """
    cfg = bot.config
    now = float(now or time.time())
    rows = [r for r in rows if (r.get("content") or "").strip()][-40:]
    if not rows:
        return {"summary": "", "跳过": "这一轮没有内容"}

    limit = int(cfg.get("memory.l1_chars", 600) or 600)
    prompt = _选提示(session_id).format(limit=limit, turn=_渲染(rows))
    try:
        结果 = await (ask or _默认问)(bot, prompt, limit)
    except Exception as e:
        log.warn(f"本轮小结失败：{type(e).__name__} {e}")
        return {"summary": "", "跳过": "判官不可用"}

    正文 = " ".join(str((结果 or {}).get("summary") or "").split())
    if not 正文:
        return {"summary": "", "跳过": "判官没给出小结"}
    if len(正文) > limit * 2:
        正文 = 正文[: limit * 2]

    重要度 = max(1, min(5, int((结果 or {}).get("importance") or 2)))
    主干 = " ".join(str((结果 or {}).get("gist") or "").split())[:120]
    要点 = [str(x).strip() for x in ((结果 or {}).get("points") or [])
            if str(x or "").strip()][:3]
    关键词 = [str(w).strip() for w in ((结果 or {}).get("keywords") or []) if str(w).strip()][:12]
    约定 = [str(p).strip() for p in ((结果 or {}).get("promises") or []) if str(p).strip()][:8]
    upto = float(rows[-1].get("ts") or now)

    # ① L1：覆盖。**不含历史**，所以下一轮看到的就是「刚才那一轮」
    bot.store.set_turn_summary(session_id, 正文, upto)

    库 = getattr(bot, "entities", None) or EntityStore(getattr(bot, "base_dir", "."), cfg)
    try:
        bot.entities = 库
    except Exception:
        pass

    写了 = {"L1": len(正文)}

    # ② 事件流：只收「值得单独记住的」，key 唯一 → 不会重复
    if 重要度 >= int(cfg.get("memory.event_min_importance", 3) or 3):
        行 = 库.add_event(正文, ts=upto, importance=重要度, source=session_id,
                         gist=主干 or None, points=要点 or None)
        if 行:
            写了["event"] = 1

    # ③ 约定：key 用主题的哈希，同一个约定反复提到只留最新一条
    for 项 in 约定:
        key = "promise:" + _短哈希(项)
        if 库.add(key, "promise", text=项, importance=5, ts=upto, source=session_id):
            写了["promise"] = 写了.get("promise", 0) + 1

    # ③' 梗 / 黑话：群友这一轮明确解释过才记（跟旧路径同一个规矩）
    书 = getattr(bot, "memes", None)
    if 书 is not None:
        for item in ((结果 or {}).get("memes") or []):
            if not isinstance(item, dict):
                continue
            try:
                书.learn(str(item.get("name") or ""), item.get("triggers") or [],
                         str(item.get("meaning") or ""), str(item.get("usage") or ""),
                         source="群友", learned_from=str(session_id))
                写了["meme"] = 写了.get("meme", 0) + 1
            except Exception as e:
                log.debug(f"梗写入失败：{type(e).__name__}")

    # ④ 跨天了就把昨天的事件合并成一条日摘要（只合并，不叠加）
    try:
        if cfg.get("memory.auto_day_merge", True):
            合并 = await merge_day(bot, 库, _天(upto - 86400), ask=ask, now=now)
            if 合并:
                写了["day"] = 合并
    except Exception as e:
        log.debug(f"日摘要合并跳过：{type(e).__name__}")

    log.info(f"本轮小结 v2 {session_id}：{正文[:50]}｜重要度 {重要度}｜写了 {写了}")
    return {"summary": 正文, "keywords": 关键词, "importance": 重要度,
            "promises": 约定, "写了": 写了}


_合并提示 = (
    "下面是同一天里若干段对话小结，按「发生在哪里」分组、组内按时间排。"
    "把它们合成一条「这一天发生了什么」的记录。\n"
    "必须保留：重要事实、新约定、人物关系变化、没解决的事，"
    "**以及每一件事发生在哪里**（哪个群、还是和谁私聊）。"
    "私聊里发生的事，要写成「对方私下跟我说…」，不要写成「群里有人说…」。\n"
    "可以丢掉：重复出现的闲聊主题、语气。\n"
    "要求：第三人称，最多 {limit} 字，只输出 JSON：{{\"summary\":\"……\"}}。\n\n{body}"
)


def _来源标签(source: str) -> str:
    s = str(source or "").strip()
    if s.startswith("private_"):
        return "私聊"
    if s.startswith("group_"):
        return "群里"
    return "来源未标注"


async def merge_day(bot, 库: EntityStore, day: str, *, ask=None,
                    now: float | None = None) -> int:
    """把 `day` 那天的事件合并成一条 kind=day。已经有就不动。返回合并了几条。"""
    now = float(now or time.time())
    for row in 库.load(now):
        if row.get("kind") == "day" and row.get("key") == f"day:{day}":
            return 0

    事件 = [r for r in 库.load(now)
            if r.get("kind") == "event" and _天(float(r.get("ts") or 0)) == day]
    if not 事件:
        return 0
    事件.sort(key=lambda r: float(r.get("ts") or 0))

    limit = int(bot.config.get("memory.l2_day_chars", 800) or 800)
    # 按 source 分组：私聊和群聊不能搅成一段，组内仍按时间排
    分组: dict[str, list] = {}
    for r in 事件:
        分组.setdefault(str(r.get("source") or ""), []).append(r)
    段 = []
    for src in sorted(分组):
        rs = sorted(分组[src], key=lambda r: float(r.get("ts") or 0))
        标题 = _来源标签(src)
        lines = [f"- {str(r.get('text') or '')[:200]}" for r in rs[-40:]]
        段.append(f"【{标题}】\n" + "\n".join(lines))
    body = "\n\n".join(段)
    prompt = _合并提示.format(limit=limit, body=body)
    try:
        结果 = await (ask or _默认问)(bot, prompt, limit)
    except Exception as e:
        log.debug(f"日摘要判官不可用：{type(e).__name__}")
        结果 = {}
    正文 = " ".join(str((结果 or {}).get("summary") or "").split())
    if not 正文:
        # 判官不行就机械拼接 —— 宁可难看，也别丢这一天；带上来源，别把私聊并成群聊
        碎 = []
        for r in 事件:
            处 = _来源标签(str(r.get("source") or ""))
            处 = "" if 处 == "来源未标注" else 处
            碎.append(f"{处 + '：' if 处 else ''}{str(r.get('text') or '')[:120]}")
        正文 = "；".join(碎)[:limit]
    if not 正文:
        return 0

    # 日摘要的骨架：优先用判官给的；没给就从当天已有事件里挑一条最长的顶
    # （它比事件活得更久，所以更需要能降档，否则老了会变成一坨说不清的全文）
    主干 = " ".join(str((结果 or {}).get("gist") or "").split())[:120]
    if not 主干:
        带骨 = [r for r in 事件 if str(r.get("gist") or "").strip()]
        if 带骨:
            主干 = str(带骨[-1]["gist"]).strip()[:120]
        else:
            主干 = 正文.split("；")[0][:60]
    要点 = [str(x).strip() for x in ((结果 or {}).get("points") or [])
            if str(x or "").strip()][:3]

    # 保留「涉及哪些 source」：日摘要渲染时靠它标注私聊/群聊（旧数据没有就为空，不崩）
    sources = sorted({str(r.get("source") or "").strip() for r in 事件
                      if str(r.get("source") or "").strip()})

    库.add(f"day:{day}", "day", text=正文[:limit], importance=3,
           gist=主干 or None, points=要点 or None,
           ts=float(事件[-1].get("ts") or now), source="merge",
           sources=sources or None)
    log.info(f"日摘要已合并 {day}：{len(事件)} 条事件（{len(sources)} 个来源）→ {len(正文)} 字")
    return len(事件)
