"""主人用普通话下 QQ 资料修改，自动识别，不需要斜线指令。"""
import re

from pawbot.account import AccountActions, _HELP
from pawbot.plugin import on_message

__plugin_name__ = "账号"
__version__ = "2.0.0"
__description__ = "主人直接说就能改头像、昵称、签名、名片、群名、头衔"

_CAPABILITY = re.compile(r"^(?:账号(?:能力|功能)|你能改(?:什么|什麼|哪些)|你都能改(?:什么|什麼|哪些))[。！!？?]?$")


# block=False 必须保留：这个处理器要普遍地看一眼每条消息，
# 看完不拦后面的 AI 聊天（默认 block=True 会把它之后的处理器全部截断）。
@on_message(priority=85, block=False)
async def account_order(ctx):
    ev = ctx.event
    if ev.type != "message" or ev.user_id == ev.self_id or ctx.matched_command:
        return
    actions = getattr(ctx.bot, "account", None)
    if not isinstance(actions, AccountActions):
        actions = AccountActions(ctx.bot)
    try:
        # owner 白名单已移除：这条消息来自任何人都会被处理。
        # 这里保留的校验只是「必须是当前这条真实消息」，不是权限。
        text, _segments = actions._current(ctx)
    except ValueError:
        return
    if _CAPABILITY.fullmatch(text.strip()):
        await ctx.reply(_HELP)
        ctx.stop()
        return
    request = actions.natural_request(ctx, text)
    if request is None:
        return
    operation, value = request
    response, _ = await actions.execute(ctx, operation, value)
    await ctx.reply(response)
    ctx.stop()
