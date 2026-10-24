"""
尺度过关。

    /荤        开到爱色色（默认就是 2）
    /荤 1      收着点：接荤梗、讲段子
    /荤 2      爱色色：开黄腔、撩骚、找擦边图，明说 R18 也给
    /当前尺度   看看现在几档

**没有素聊档了。** 原来的 0 档（素聊）已废除，任何 ≤0 都按 2 爱色色处理。
`/素` 命令还在，但会直接告诉她没有素聊这档。

档位是**按群**存的，A 群荤不代表 B 群也荤。
私聊里也能调，但只影响私聊自己。

谁来调都能调——这个群本来就是熟人局，尺度这事群友自己说了算。
（原来的 owner_only 门禁已按要求移除，`spice.owner_only` 这个配置项不再起作用。）
"""

from pawbot.log import log
from pawbot.plugin import on_command

__plugin_name__ = "尺度"
__version__ = "1.0.0"
__description__ = "按群调荤素档位（没有素聊档，默认爱色色）"

_LABELS = {1: "收敛点（接荤梗、讲段子）", 2: "爱色色（开黄腔、撩骚、找擦边图，明说 R18 也给）"}


def _may_control(ctx) -> bool:
    """⚠️ 尺度门禁按要求移除：现在群里任何人都能调这个群的荤素档位。

    （原来这里读的是 `whitelist.owner_ids`，而 config 里根本没有这个键，
      一旦把 spice.owner_only 设成 true 就会把所有人挡在外面——那个坑一起没了。）
    """
    return True


@on_command("荤", aliases=("开车", "荤话"), priority=5, group_only=False)
async def set_spice(ctx):
    arg = ctx.arg_text.strip()
    if arg:
        try:
            lv = int(arg)
        except ValueError:
            await ctx.reply("档位是 1/2，给个数。0 没有了，现在没素聊档。")
            ctx.stop()
            return
    else:
        lv = 2
    if not _may_control(ctx):
        await ctx.reply("这个群不是我说了算，问管理员。")
        ctx.stop()
        return
    cur = ctx.bot.spice.set(ctx.session_id, lv)
    log.info(f"尺度调整 {ctx.session_id} → {cur}（{ctx.sender_name}）")
    await ctx.reply(f"调到 {cur} 档：{_LABELS[cur]}。")
    ctx.stop()


@on_command("素", aliases=("清淡", "正经"), priority=5, group_only=False)
async def set_plain(ctx):
    await ctx.reply("没有素聊这档了，最低就是「收敛点」，默认「爱色色」。")
    ctx.stop()


@on_command("当前尺度", aliases=("尺度", "多荤"), priority=5, group_only=False)
async def show_spice(ctx):
    lv = ctx.bot.spice.get(ctx.session_id)
    await ctx.reply(f"现在 {lv} 档：{_LABELS.get(lv, _LABELS[2])}")
    ctx.stop()
