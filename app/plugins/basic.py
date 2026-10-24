"""
最简单的指令插件：ping / help / 状态。

这份文件就是插件写法的最小模板——照着改一个自己的插件就行。

    python 会自动 import 本文件，装饰器把函数登记到总线上。
"""

from pawbot import message as msg
from pawbot.plugin import on_command

__plugin_name__ = "基础指令"
__version__ = "1.0.0"
__description__ = "ping、help、状态查询"


@on_command("ping", aliases=("延迟", "在吗"), priority=5)
async def ping(ctx):
    import time
    t0 = time.time()
    data = await ctx.bot.conn_for(ctx.self_id).call("get_status", timeout=8)
    cost = int((time.time() - t0) * 1000)
    online = data.get("online", "?")
    text = f"pong · {cost}ms · online={online}"
    try:
        result = await ctx.reply(text)
        delivered = result is not None
    except Exception as e:
        delivered = bool(getattr(e, "delivered", False))
        if not delivered:
            raise
        from pawbot.log import log
        log.error("pong 已发出但等不到回执：" + str(e))

    if delivered:
        ctx.bot.store.append(ctx.session_id, "assistant", text, who="我")
        ctx.bot.topic.seen(ctx.session_id, "我", text, is_self=True)
    ctx.stop()


@on_command("help", aliases=("帮助", "菜单"), priority=5)
async def help_cmd(ctx):
    from pawbot.bus import BUS
    lines = ["可用指令："]
    seen = set()
    for h in BUS.handlers:
        if h.kind != "command":
            continue
        names = [n for n in (h.commands + h.aliases) if n not in seen]
        if not names:
            continue
        seen.update(names)
        lines.append(f"  /{names[0]}" + (f"（{'/'.join(names[1:])}）" if len(names) > 1 else ""))
    await ctx.reply("\n".join(lines))
    ctx.stop()


@on_command("状态", aliases=("status",), priority=5)
async def status_cmd(ctx):
    s = ctx.bot.status()
    await ctx.reply(
        f"在线 {len(s['connections'])} 个账号 · 已收 {s['stats']['recv']} 条 · "
        f"已发 {s['stats']['sent']} 条 · 处理器 {s['handlers']} 个"
    )
    ctx.stop()


@on_command("echo", aliases=("复读",), priority=5)
async def echo(ctx):
    text = ctx.arg_text
    if not text:
        await ctx.reply("说点什么呀。")
    else:
        await ctx.reply(msg.Text(text))
    ctx.stop()
