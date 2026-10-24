"""日记与爱好——借群消息当心跳，每 10 分钟最多跑一次后台检查。

只做两件事，且都不阻塞当前消息：

1. 昨天的日记还没写、又有昨天的经历，就丢后台补一篇；
2. 到点的爱好逐项搜一下新进展，结果先放进 ``bot.pending_hobby_news``，
   由 proactive 之后自己挑时机说，插件绝不直接发群消息。
"""

import time

from pawbot import tasks
from pawbot.log import log
from pawbot.plugin import on_message

__plugin_name__ = "日记与爱好"
__version__ = "1.0.0"
__description__ = "每天补写昨天的日记；定期查追番/游戏新进展"

DIARY_INTERVAL = 600
HOBBY_INTERVAL = 600

_last_diary = 0.0
_last_hobby = 0.0


@on_message(priority=70, block=False)
async def diary_and_hobbies(ctx):
    global _last_diary, _last_hobby
    bot = ctx.bot
    now = time.time()

    if now - _last_diary >= DIARY_INTERVAL:
        _last_diary = now
        diary = getattr(bot, "diary", None)
        if diary is not None and getattr(diary, "enabled", True):
            tasks.spawn(diary.maybe_write_yesterday(bot), name="diary")

    if now - _last_hobby >= HOBBY_INTERVAL:
        _last_hobby = now
        book = getattr(bot, "hobbies", None)
        if book is not None and getattr(book, "enabled", True):
            tasks.spawn(_check_due(bot, book), name="hobbies")


async def _check_due(bot, book) -> list[dict]:
    """逐项检查到点的爱好，把新进展追加到 bot.pending_hobby_news。"""
    try:
        items = book.due_items()
    except Exception as e:
        log.debug(f"爱好到期检查失败：{type(e).__name__}")
        return []
    fresh = []
    for item in items:
        try:
            summary = await book.check(bot, item)
        except Exception as e:
            log.debug(f"爱好检查失败：{type(e).__name__}")
            continue
        if not summary:
            continue
        pending = getattr(bot, "pending_hobby_news", None)
        if not isinstance(pending, list):
            pending = []
            bot.pending_hobby_news = pending
        pending.append({
            "name": str(item.get("name") or ""),
            "kind": str(item.get("kind") or ""),
            "text": summary,
            "ts": time.time(),
        })
        fresh.append(pending[-1])
        log.info(f"爱好有新进展：{item.get('name')}")
    return fresh
