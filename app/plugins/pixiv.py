"""
Pixiv 登录（借 PawBot 专用 Chrome 会话，不用 token）。

指令：
    /pixiv登录        开专用 Chrome、等你登录
    /pixiv登录 状态   看现在能不能用
    /pixiv登录 解除   把浏览器会话断开（下次重新登）

专用浏览器配置与日常 Edge / Chrome 隔离，首次使用需重新登录 Google 和 Pixiv。
"""

import asyncio

from pawbot.log import log
from pawbot.plugin import on_command

__plugin_name__ = "Pixiv"
__version__ = "2.0.0"
__description__ = "借 PawBot 专用 Chrome 会话搜 Pixiv（R-18 和原图都能拿）"


def _sess():
    from pawbot.pixiv_session import PixivSession
    return PixivSession()


@on_command("pixiv登录", aliases=("pixiv登入", "pixiv"), priority=5)
async def pixiv_login(ctx):
    arg = ctx.arg_text.strip()
    sess = _sess()

    if arg in ("状态", "status"):
        status = await sess.status()
        lines = ["PawBot 专用 Pixiv 会话状态：",
                 f"专用 Chrome：{'运行中' if status.get('ready') else '不可用'}",
                 f"当前页面：{'Pixiv' if status.get('page_on_pixiv') else '非 Pixiv / 无页面'}",
                 f"登录状态：{'已确认' if status.get('logged_in') else '未确认'}",
                 f"搜索方式：{status.get('history_mode', 'unknown')}"]
        if status.get("error"):
            lines.append(f"诊断：{status['error']}")
        await ctx.reply("\n".join(lines))
        ctx.stop()
        return

    if arg in ("解除", "解绑", "off", "退出"):
        ctx.bot.config.set("tools.pixiv_use_browser", False)
        ctx.bot.config.set("tools.pixiv_sessid", "")
        ctx.bot.config.set("tools.pixiv_refresh_token", "")
        ctx.bot.config.save()
        await ctx.reply("行，之后只走免登录的 SFW 路径。")
        ctx.stop()
        return

    # --- 粘了一串东西进来：当成凭据试
    if arg:
        from pawbot import imagesearch
        value = arg.strip()

        if imagesearch.pixiv_access_token(value):
            ctx.bot.config.set("tools.pixiv_refresh_token", value)
            ctx.bot.config.save()
            log.info("Pixiv 已绑定（refresh_token）")
            await ctx.reply("好了，已经绑上。以后找图会走你的号，R-18 也能拿（发出去前会自动打码）。")
            ctx.stop()
            return

        import asyncio as _aio
        loop = _aio.get_event_loop()
        probe = await loop.run_in_executor(
            None, lambda: imagesearch.search_pixiv_sessid("AI", 12, value, True))
        r18 = [x for x in probe if x.get("rating") == "r18"]
        if r18:
            ctx.bot.config.set("tools.pixiv_sessid", value)
            ctx.bot.config.save()
            log.info("Pixiv 已绑定（SESSID）")
            await ctx.reply(f"成了！用你这个号能搜到 R-18（试搜命中 {len(r18)} 张）。\n"
                            f"以后找图就走你的号了。")
        else:
            await ctx.reply("这串两种都不认：\n"
                            "  · 当 refresh_token：换不出 access_token\n"
                            "  · 当 SESSID：也搜不到 R-18（可能过期了或复制错了）\n"
                            "再取一次试试。")
        ctx.stop()
        return

    if not sess.chrome_cdp_ready():
        if sess.cdp_alive():
            await ctx.reply("9222 已被其他浏览器会话占用；为避免干扰你的浏览器，我不会接管它。请关闭占用 9222 的调试实例后再试。")
            ctx.stop()
            return
        await ctx.reply("我会启动 PawBot 专用 Chrome（不会关闭现有 Edge/Chrome）。首次使用需要在新窗口重新登录 Google 和 Pixiv。")
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None, sess.launch_chrome, "https://www.pixiv.net/")
        if not ok:
            await ctx.reply("专用 Chrome 启动失败。请确认安装了 Google Chrome，且本机 9222 端口可用。")
            ctx.stop()
            return
        await asyncio.sleep(4)

    await sess.open_login("https://www.pixiv.net/")
    await ctx.reply("PawBot 专用 Chrome 已打开。请在该窗口登录 Pixiv（Google 账号也可以）；现有浏览器的登录不会复制过去。\n"
                    "登录后会自动检测。Chrome 保持开启，机器人才能借用这个会话。")

    # 后台等，别占着对话
    async def _wait():
        try:
            ok = await sess.wait_login(300)
            if ok:
                ctx.bot.config.set("tools.pixiv_use_browser", True)
                ctx.bot.config.save()
                log.info("Pixiv 浏览器会话已就绪")
            else:
                log.info("Pixiv 登录超时")
        except Exception as e:
            log.warn(f"等待 Pixiv 登录出错：{type(e).__name__} {e}")

    asyncio.create_task(_wait())
    ctx.stop()
