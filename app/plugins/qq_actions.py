"""
QQ 群特有的动作：撤回、拍一拍。

聊天机器人常见的一个破绽是「说错了也收不回来」——真人会说错、会撤回。
这里给它两个真实可用的手：

  /撤回        把自己这个群最后说的一条撤回来
  拍一拍 xx    拍谁（@ 或者写名字都能认）

NapCat 的接口：
  delete_msg   {message_id}
  group_poke   {group_id, user_id}
"""

import asyncio
import random
import time

from pawbot import message as msg
from pawbot.log import log
from pawbot.plugin import on_command, on_keyword, on_notice

__plugin_name__ = "群动作"
__version__ = "1.0.0"
__description__ = "撤回自己说错的话、拍一拍群友"

_SEEN: dict[str, float] = {}
_POKE_RESPONSES = (
    "哎，谁拍我！拍回来一下。",
    "收到，鱼尾巴都晃了一下。",
    "嗯？别拍上瘾啊，我也拍你一下。",
    "这下算你先动手，我记着了。",
)


@on_notice(notice_types=("notify",), priority=35, block=False)
async def on_poked(ctx):
    """QQ 原生戳一戳以 notify/poke 上报；只有被戳目标是自己时才回应。"""
    ev = ctx.event
    if getattr(ev, "sub_type", "") != "poke" or not ev.is_group:
        return
    if int(getattr(ev, "target_id", 0) or 0) != int(ctx.self_id):
        return

    key = f"{ev.group_id}:{ctx.self_id}"
    now = time.time()
    if now - _SEEN.get(key, 0) < 5:
        return
    _SEEN[key] = now

    conn = ctx.bot.conn_for(ctx.self_id)
    if conn is None:
        return
    try:
        await conn.call("group_poke", group_id=int(ev.group_id),
                        user_id=int(ev.user_id), timeout=8)
    except Exception as e:
        log.warn(f"拍一拍回应失败：{type(e).__name__} {e}")
        return

    # 拍一拍就等于被叫了一下：交给 AI 按当下语境接话，不再回固定台词
    try:
        from plugins import ai_chat
        if getattr(ctx.bot, "agent", None) is not None:
            await ai_chat.poke_turn(ctx)
            log.info(f"回应拍一拍（AI 接话）：{ev.user_id} → {ctx.self_id} @ {ev.group_id}")
            return
    except Exception as e:
        log.warn(f"拍一拍转给 AI 失败，改回固定回应：{type(e).__name__} {e}")

    await ctx.send(random.choice(_POKE_RESPONSES), group_id=int(ev.group_id))
    log.info(f"回应拍一拍：{ev.user_id} → {ctx.self_id} @ {ev.group_id}")


@on_command("撤回", aliases=("recall", "undo"), priority=5, group_only=True)
async def recall_last(ctx):
    """撤回自己在这个群说的最后一条。"""
    sid = ctx.session_id
    mid = ctx.bot.last_msg_id.get(sid)
    if not mid:
        await ctx.reply("没有能撤回的东西，我说过的都还留着。")
        ctx.stop()
        return

    conn = ctx.bot.conn_for(ctx.self_id)
    if conn is None:
        ctx.stop()
        return
    try:
        await conn.call("delete_msg", message_id=int(mid), timeout=10)
    except Exception as e:
        log.warn(f"撤回失败：{type(e).__name__} {e}")
        await ctx.reply("撤不回来了，可能已经过了时间。")
        ctx.stop()
        return

    ctx.bot.last_msg_id.pop(sid, None)
    ctx.bot.last_msg_text.pop(sid, None)
    log.info(f"已撤回 {sid} 的消息 {mid}")
    await ctx.reply("撤了。")
    ctx.stop()


@on_keyword("拍一拍", priority=40, group_only=True, block=False)
async def poke(ctx):
    """别人说「拍一拍」就拍回去；带 @ 就拍被 @ 的人。"""
    import time
    text = ctx.plain or ""
    if "拍一拍" not in text:
        return

    # 防抖：5 秒内只拍一次
    now = time.time()
    if now - _SEEN.get(ctx.session_id, 0) < 5:
        return
    _SEEN[ctx.session_id] = now

    target = None
    for seg in getattr(ctx.event, "message", []):
        if seg.get("type") == "at":
            try:
                target = int(seg["data"]["qq"])
            except Exception:
                continue
            if target and target != ctx.self_id:
                break
            target = None
    if target is None:
        target = ctx.user_id

    conn = ctx.bot.conn_for(ctx.self_id)
    if conn is None or not ctx.group_id:
        return
    try:
        await conn.call("group_poke", group_id=int(ctx.group_id),
                        user_id=int(target), timeout=8)
        log.info(f"拍一拍 {target} @ {ctx.group_id}")
    except Exception as e:
        log.warn(f"拍一拍失败：{type(e).__name__} {e}")
        return

    await ctx.reply("（拍回去了）")
    await asyncio.sleep(0)