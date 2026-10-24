"""
随机插话 —— 让机器人不像个「只在被 @ 时才活过来」的门铃。

策略：记最近 20 条群消息，按概率决定要不要接一句。
冷却时间防止刷屏，命中概率建议 0.01 ~ 0.05。
开关在 WebUI 的 random_interject 里。
"""

import asyncio
import random
import time
from collections import defaultdict, deque

from pawbot.plugin import on_message
from pawbot.style import polish

__plugin_name__ = "随机插话"
__version__ = "1.0.0"
__description__ = "按概率接话，带冷却"

RECENT: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))
LAST_SPEAK: dict[int, float] = {}
BUSY: set[int] = set()


@on_message(priority=95)
async def interject(ctx):
    cfg = ctx.config
    ev = ctx.event
    if ctx.state.get("group_observe"):
        return
    if not ev.is_group or not cfg.get("random_interject.enabled", False):
        return
    if ctx.matched_command:
        return

    gid = ev.group_id
    RECENT[gid].append(f"{ctx.sender_name}: {ctx.plain}")

    if gid in BUSY:
        return
    if time.time() - LAST_SPEAK.get(gid, 0) < float(cfg.get("random_interject.cooldown_sec", 120)):
        return
    if random.random() > float(cfg.get("random_interject.probability", 0.02)):
        return

    BUSY.add(gid)
    try:
        await asyncio.sleep(random.uniform(1.2, 3.0))    # 假装在打字
        transcript = "\n".join(list(RECENT[gid])[-8:])
        prompt = (
            "下面是 QQ 群最近几条聊天记录。如果你有自然的、简短的话想说就接一句，"
            "一般不超过 25 个字，用聊天的口吻；如果没什么可说的，只回复「<pass>」。\n\n"
            + transcript
        )
        system = ctx.bot.persona.build(
            group_id=gid, sender="",
            extra="## 现在\n"
                  "群里刚聊到下面这些，你刷到了，想随口接一句。"
                  "不用回答任何人，当群友发一条。没意思就别开口。")
        raw = (await ctx.bot.llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ])).strip()
        answer = polish(raw)

        if not answer or "<pass>" in answer or len(answer) > 80:
            return
        await ctx.send(answer, group_id=gid)
        LAST_SPEAK[gid] = time.time()
        from pawbot.log import log
        log.info(f"随机插话 → 群{gid}：{answer}")
    except Exception as e:
        from pawbot.log import log
        log.debug(f"插话失败（忽略）：{e}")
    finally:
        BUSY.discard(gid)
