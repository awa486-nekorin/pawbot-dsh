"""
滚动对话小結：每回完一轮，把「上一份小结 + 这一轮」合成一份新的小结。

为什么另做一份、而不复用 store.summary：
  · store.summary 只在 300 条窗口溢出、archive 攒满 40 条时才压缩——
    活跃群里一晚上未必攒够，等于模型其实没有长期记忆；
  · 这份小结每轮都更新，覆盖的是「刚才这一轮」，下一轮接着在上面叠。

它只会作为 system 提示注入，**永远不发给群**。谁也别把它当聊天内容复述。

用判官模型来做（便宜、快）；判官不可用时退回主模型。
"""

from __future__ import annotations

import asyncio
import time

from . import ctxwin, judge
from .log import log
from .memory import _tokens

# 同一会话的两次小结别并发跑：否则两边都拿同一批消息，重复计一次
_locks: dict[str, asyncio.Lock] = {}

_PROMPT = (
    "你在给一个 QQ 群里的 AI 群友维护一份「对话记忆」。"
    "把【已有小结】和【这一轮新对话】合并成一份新的小结，供它下一轮接话用。\n"
    "必须保留：正在聊什么、谁持什么观点或偏好、有没有约定、还没解决的问题、"
    "这一轮的情绪走向（谁不高兴、谁在撒娇）。\n"
    "另给出 3~12 个能代表本轮的关键词，并按 1~5 给重要性评分。\n"
    "可以丢掉：寒暄、重复、表情包、纯语气词。\n"
    "要求：第三人称陈述，像日记，不要分点、不要标题、不要写「总结」二字；"
    "最多 {limit} 字；只输出 JSON。\n"
    "认人：对话里「昵称(尾号1234)」的尾号才是身份，同名不同尾号是不同的人；"
    "小结里提到人时写成「昵称(尾号1234)」，别把两个人的话、观点合到一个人头上。"
    "「我（大肥鱼）」是这个 AI 群友自己，小结里称「大肥鱼」。\n"
    "注意：这份摘要不会展示给任何人，所以不要写「请告诉我」「如果需要」这类回应性的话。\n\n"
    "【已有小结】\n{prev}\n\n"
    "【这一轮新对话】\n{turn}"
)


def _render(rows: list[dict], max_chars: int = 2500) -> str:
    """渲染给判官看的对话。**每行都带时间**：判官要判「谁先说的、谁在接谁」，
    没有时间就只能猜，猜错就把两个人的话合到一个人头上（写进小结就改不回来了）。

    截断按**整行**从尾部取（和 mem/write.py 的 `_渲染` 同一套），
    不按字元切——按字元切会把行首的 `[HH:MM] 谁：` 前缀削掉，判官就认不出谁说的。
    """
    rows = [r for r in rows if str(r.get("content") or "").strip()]
    # 基准日取最后一条，跟 ctxwin（模型看到的上下文）同一套口径，两边不会打架
    ref = float(rows[-1].get("ts") or time.time()) if rows else time.time()
    lines = []
    for r in rows:
        content = str(r.get("content") or "").strip()
        if not content:
            continue
        who = ctxwin.speaker(r) if (r.get("role") == "assistant" or r.get("who")) else "对方"
        stamp = ctxwin._fmt_time(float(r.get("ts") or ref), ref)
        lines.append(f"{stamp} {who}：{content[:200]}")
    if not lines:
        return ""
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full
    # 从尾部整行取，别把时间/说话人前缀切成半行
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:
        # 一行都塞不下时至少留最后一行完整的（宁可超字数，也不切出无前缀的半行）
        return lines[-1]
    return "\n".join(reversed(kept))


async def _ask(bot, prompt: str, limit: int
               ) -> tuple[str, list[str], int, list[str], list[dict]]:
    """优先判官（deepseek-flash，快且便宜），失败再用主模型。"""
    try:
        verdict = await judge.judge_json(
            bot.config,
            prompt + f'\n\n只输出 JSON：{{"summary":"不超过{limit}字的小结","keywords":["关键词"],'
                     f'"importance":2,"promises":["用户明确要求记住或到点提醒的事"],'
                     f'"memes":[{{"name":"梗名","triggers":["触发词"],"meaning":"出处和意思"}}]}}；'
                     "importance 为 1 到 5 的整数；没有约定就给空数组。"
                     "memes 只在【这一轮】里群友明确解释了某个梗/黑话/缩写的出处或意思时才填，"
                     "把对方说的意思概括进去；没人解释、只是用了梗，或者是你自己猜的，一律给空数组。",
            timeout=float(bot.config.get("chat.turn_summary_timeout", 20)))
        if isinstance(verdict, dict):
            text = str(verdict.get("summary") or "").strip()
            if text:
                words = verdict.get("keywords") or []
                importance = int(verdict.get("importance") or 2)
                promises = verdict.get("promises") or []
                if not isinstance(promises, (list, tuple)):
                    promises = [promises]
                promises = [str(p).strip() for p in promises if str(p).strip()]
                memes = verdict.get("memes") or []
                if not isinstance(memes, (list, tuple)):
                    memes = [memes]
                memes = [m for m in memes if isinstance(m, dict)][:6]
                return (text, [str(word) for word in words if str(word).strip()],
                        max(1, min(5, importance)), promises[:8], memes)
    except Exception as e:
        log.debug(f"小結判官不可用：{type(e).__name__}")
    try:
        text = (await bot.llm.chat([{"role": "user", "content": prompt}]) or "").strip()
        return text, sorted(_tokens(text)), 2, [], []
    except Exception as e:
        log.debug(f"小結主模型失败：{type(e).__name__}")
        return "", [], 2, [], []


async def update(bot, session_id: str) -> str:
    """合并上一份小结和这一轮新对话。返回新的小结（失败返回空串）。

    `memory.engine = "v2"` 时改走 `mem.write.write_turn`：
    只写「这一轮新增」，不再把上一份小结喂进去（那是 36% 重复的根）。
    """
    lock = _locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        if str(bot.config.get("memory.engine", "legacy")) == "v2":
            try:
                return await _update_v2(bot, session_id)
            except Exception as e:
                log.warn(f"小结 v2 失败，退回旧路径：{type(e).__name__} {e}")
        return await _update(bot, session_id)


async def _update_v2(bot, session_id: str) -> str:
    from .mem import write as 记忆写
    cfg = bot.config
    if not cfg.get("chat.turn_summary", True):
        return ""
    store = bot.store
    if session_id not in getattr(store, "data", {}):
        return ""
    since = store.turn_summary_ts(session_id)
    rows = [r for r in store.window(session_id)
            if float(r.get("ts") or 0) > since]
    if len(rows) < int(cfg.get("chat.turn_summary_min_rows", 1) or 1):
        return ""
    结果 = await 记忆写.write_turn(bot, session_id, rows[-40:])
    return str(结果.get("summary") or "")


async def _update(bot, session_id: str) -> str:
    cfg = bot.config
    if not cfg.get("chat.turn_summary", True):
        return ""
    store = bot.store
    if session_id not in getattr(store, "data", {}):
        return ""
    since = store.turn_summary_ts(session_id)
    rows = [r for r in store.window(session_id)
            if float(r.get("ts") or 0) > since]
    min_rows = int(cfg.get("chat.turn_summary_min_rows", 1) or 1)
    if len(rows) < min_rows:
        return ""
    # 一轮里可能堆了几十条，取最后一段够了
    rows = rows[-40:]

    limit = int(cfg.get("chat.turn_summary_chars", 300) or 300)
    prev = store.turn_summary(session_id) or "（还没有）"
    prompt = _PROMPT.format(limit=limit, prev=prev, turn=_render(rows))
    text, keywords, importance, promises, memes = await _ask(bot, prompt, limit)
    text = " ".join(text.split())
    if not text:
        return ""
    # 用户明确要求记住或到点提醒的事，单独落进长期记忆（旧判官没有 promises 也没关系）
    memory = getattr(bot, "memory", None)
    for promise in promises:
        if memory is None:
            break
        try:
            memory.remember(f"约定：{promise}", "fact")
        except Exception as e:
            log.warn(f"约定写入记忆失败：{type(e).__name__} {e}")
    # 群友这一轮明确解释过的梗，顺手存进梗库（旧判官没有 memes 也没关系）
    book = getattr(bot, "memes", None)
    if book is not None:
        for item in memes:
            try:
                book.learn(
                    str(item.get("name") or ""),
                    item.get("triggers") or [],
                    str(item.get("meaning") or ""),
                    str(item.get("usage") or ""),
                    source="群友",
                    learned_from=str(session_id),
                )
            except Exception as e:
                log.warn(f"梗写入失败：{type(e).__name__} {e}")
    if len(text) > limit * 2:          # 模型没守字数，粗暴截一下，别让提示词越来越胖
        text = text[:limit * 2]
    upto = float(rows[-1].get("ts") or time.time())
    store.set_turn_summary(session_id, text, upto)
    longmem = getattr(bot, "longmem", None)
    if longmem is not None:
        participants = sorted({str(row.get("uid")) for row in rows
                               if row.get("role") != "assistant" and row.get("uid")})
        try:
            await longmem.add_episode(bot, ts=upto, session_id=session_id,
                                      participants=participants, summary=text,
                                      keywords=keywords, importance=importance)
        except Exception as e:
            log.warn(f"長期記憶寫入失敗：{type(e).__name__} {e}")
    log.info(f"对话小結 {session_id}：{text[:60]}")
    return text
