"""
群管理小插件：入群欢迎、关键词自动回复。

演示 on_notice 和 on_keyword 两种写法。
"""

import asyncio
import random

from pawbot import message as msg
from pawbot.plugin import on_keyword, on_notice

__plugin_name__ = "群互动"
__version__ = "1.0.0"
__description__ = "入群欢迎 + 关键词应答"

WELCOME = [
    "来了只新猫。{name}，自己找地方趴着吧。",
    "{name} 你好哦。群规在公告里，别踩雷。",
    "嗯，{name} 到了。有问题就直接问。",
]


@on_notice(notice_types=("group_increase",), priority=10)
async def welcome(ctx):
    ev = ctx.event
    await asyncio.sleep(random.uniform(0.8, 2.0))   # 别像机器人一样秒回
    text = random.choice(WELCOME).format(name=f"@{ev.user_id}")
    await ctx.send([msg.At(ev.user_id), msg.Text(" " + text)],
                   group_id=ev.group_id)


@on_keyword("早安", priority=40, block=False)
async def morning(ctx):
    if ctx.state.get("group_observe"):
        return
    # 只对「就一句早安」的短消息应答；长句子让给 AI 群聊插件
    if len(ctx.plain) > 6:
        return
    await ctx.reply("早。今天也别熬夜到三点。")
    ctx.stop()


@on_keyword("晚安", priority=40, block=False)
async def night(ctx):
    if ctx.state.get("group_observe"):
        return
    if len(ctx.plain) > 6:
        return
    hour = __import__("time").localtime().tm_hour
    if hour >= 2 or hour < 5:
        await ctx.reply("这个点才睡，你是猫头鹰吗。")
    else:
        await ctx.reply("晚安，做个好梦。")
    ctx.stop()
