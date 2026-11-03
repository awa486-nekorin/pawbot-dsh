"""主人 / 白名单专属：QQ 里直接指挥刷 B 站、学评论、互动、发弹幕、评论视频。

判断「这句话是不是在让她干活」交给**决策模型**（perplexity/pplx-decider-v1.2），
不是正则。概率 > 0.5 就执行，否则当普通聊天放行。

触发面：
- 私聊：主人的话都会过一遍决策模型
- 群里：只在她被 @ / 叫到的时候过一遍（避免把群聊全当命令）

显式指令（/刷5 /学评论 /互动 /发弹幕 xxx /评论 xxx /权限 /人设）仍然保留，
不经过决策模型，直接执行。

权限 = account.owner_ids ∪ whitelist.user_ids。别人说同样的话不会被当成命令。
"""

import asyncio
import random
import re

from pawbot import bili_learn, bili_scroll, intent
from pawbot.log import log
from pawbot.plugin import on_command, on_message

__plugin_name__ = "管理员指令"
__version__ = "1.2.0"
__description__ = "主人的话交给决策模型判断，>50% 就去执行（刷B站/学评论/互动/发弹幕/评论）"

# ---------------------------------------------------------------- 权限


def _admins(bot) -> set[str]:
    ids = {str(x) for x in (bot.config.get("account.owner_ids") or [])}
    ids |= {str(x) for x in (bot.config.get("whitelist.user_ids") or [])}
    return ids


def _is_admin(ctx) -> bool:
    return str(ctx.user_id) in _admins(ctx.bot)


# ---------------------------------------------------------------- 共用


async def _send(ctx, text: str):
    try:
        await ctx.reply(text)
    except Exception as e:
        log.warn(f"管理员指令回复失败：{type(e).__name__} {e}")


def _bvid_from(text: str) -> str:
    m = re.search(r"BV[0-9A-Za-z]{10}", str(text or ""))
    return m.group(0) if m else ""


def _last_bvid(bot) -> str:
    try:
        for n in bili_scroll.recent_notes(bot, limit=5):
            try:
                with open(n["path"], "r", encoding="utf-8") as f:
                    bv = _bvid_from(f.read(3000))
            except OSError:
                continue
            if bv:
                return bv
    except Exception:
        pass
    return ""


async def _bg(ctx, coro_factory, on_done):
    bot, self_id = ctx.bot, ctx.self_id
    group_id, session_id, uid = ctx.group_id, ctx.session_id, ctx.user_id

    async def _runner():
        try:
            result = await coro_factory()
        except Exception as e:
            log.warn(f"后台任务失败：{type(e).__name__} {e}")
            result = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        try:
            text = on_done(result)
            if not text:
                return
            conn = bot.conn_for(self_id)
            if conn is None:
                return
            await bot.safety.send(conn, session_id,
                                  [{"type": "text", "data": {"text": text}}],
                                  group_id=group_id, user_id=None if group_id else uid)
        except Exception as e:
            log.warn(f"后台汇报失败：{type(e).__name__} {e}")

    asyncio.create_task(_runner())


def _report(action: str, r: dict) -> str:
    if not r:
        return "做完了，但没返回结果。"
    if not r.get("ok") and action not in ("danmaku", "comment"):
        return "没做成：" + str(r.get("reason") or r)[:200]
    if action == "scroll":
        notes = r.get("notes") or []
        lines = [f"刷完了：检查 {r.get('checked', 0)} 个，攒下 {len(notes)} 篇笔记"]
        for n in notes[:8]:
            lines.append(f"· {str(n.get('title') or '')[:36]} — {n.get('bvid', '')}")
        return "\n".join(lines)
    if action == "learn":
        return (f"学完了：{len(r.get('videos') or [])} 个视频 / {r.get('comments', 0)} 条评论 "
                f"→ 新学 {r.get('learned', 0)} 条，累计 {r.get('total', 0)} 条。")
    if action == "auto":
        lines = [f"这轮：回 {len(r.get('replied') or [])} 条 / 评论 {len(r.get('commented') or [])} 条 / "
                 f"弹幕 {len(r.get('danmaku') or [])} 条（今日 {r.get('used')}/{r.get('quota') or '∞'}）"]
        for x in (r.get("replied") or [])[:5]:
            lines.append("· 回：" + str(x)[:40])
        for x in (r.get("commented") or [])[:5]:
            lines.append("· 评：" + str(x.get("text") or "")[:40])
        for x in (r.get("danmaku") or [])[:5]:
            lines.append("· 弹幕：" + str(x.get("text") or "")[:40])
        return "\n".join(lines)
    if action == "danmaku":
        return "弹幕发出去了。" if r.get("ok") else "弹幕没发成：" + str(r.get("reason") or "")[:160]
    if action == "comment":
        return "评论发出去了。" if r.get("ok") else "评论没发成：" + str(r.get("reason") or "")[:160]
    return str(r)[:200]


_CONFIRM = {
    "scroll": "行，我去刷。",
    "learn": "行，我去翻评论区学学。",
    "auto": "行，我去评论区转转。",
    "danmaku": "行，发个弹幕。",
    "comment": "行，去评论一条。",
}


def _confirm_tasks(tasks) -> str:
    names = {"scroll": "刷B站", "learn": "学评论", "auto": "评论区互动",
             "danmaku": "发弹幕", "comment": "发评论"}
    parts = []
    for x in tasks:
        action = x.get("action")
        label = names.get(action, action)
        if action in ("scroll", "learn"):
            label += " %s 个" % x.get("count", 3)
        parts.append(label)
    return "行，" + " + ".join(parts) + "。"


def _report_tasks(r: dict) -> str:
    inner = r if isinstance(r, dict) else {}
    lines = []
    for task in (inner.get("tasks") or []):
        action = task.get("action")
        res = task.get("result") or {}
        if action == "scroll":
            notes = res.get("notes") or []
            lines.append("刷完了：检查 %s 个，攒下 %s 篇笔记" % (res.get("checked", 0), len(notes)))
            for n in notes[:5]:
                lines.append("· %s — %s" % (str(n.get("title") or "")[:30], n.get("bvid", "")))
        elif action == "learn":
            lines.append("学完了：%s 个视频 / %s 条评论 → 新学 %s 条（累计 %s）" % (
                len(res.get("videos") or []), res.get("comments", 0),
                res.get("learned", 0), res.get("total", 0)))
        elif action == "auto":
            lines.append("互动：回 %s / 评论 %s / 弹幕 %s（今日 %s/%s）" % (
                len(res.get("replied") or []), len(res.get("commented") or []),
                len(res.get("danmaku") or []), res.get("used"), res.get("quota") or "∞"))
        elif action in ("danmaku", "comment"):
            label = "弹幕" if action == "danmaku" else "评论"
            if res.get("ok"):
                lines.append(label + "发出去了：" + str(res.get("text") or "")[:40])
            else:
                lines.append(label + "没发成：" + str(res.get("reason") or "")[:120])
        else:
            lines.append(str(res.get("reason") or res)[:120])
    return "\n".join(lines) or "做完了。"


# ---------------------------------------------------------------- 决策模型入口


@on_message(priority=8)
async def admin_natural_command(ctx):
    """主人的话先过决策模型：是不是在指挥她干活？是就去做。"""
    if not _is_admin(ctx):
        return
    if getattr(ctx, "matched_command", ""):      # 显式指令交给 command 处理器
        return
    ev = ctx.event
    if getattr(ev, "type", "") != "message":
        return
    text = str(getattr(ev, "plain", "") or "").strip()
    if not text or len(text) > 200:
        return
    # 群里只在她被叫到时判断，别把群聊都当命令
    if getattr(ev, "is_group", False) and not getattr(ev, "to_me", False):
        return

    context = ""
    try:
        rows = ctx.bot.store.window(ctx.session_id, 6)
        context = "\n".join(f"{r.get('who', '')}: {str(r.get('content') or '')[:80]}"
                            for r in rows[-6:])
    except Exception:
        pass

    verdict = await intent.judge(ctx.bot, text, context)
    if not verdict.get("hit"):
        return      # 不是命令 → 正常聊天流程继续

    tasks = verdict.get("tasks") or []
    await _send(ctx, _confirm_tasks(tasks))
    ctx.stop()

    bot = ctx.bot
    verdict_copy = dict(verdict)

    async def _run():
        return await intent.run(bot, verdict_copy, reason="QQ 指挥")

    await _bg(ctx, _run, _report_tasks)


# ---------------------------------------------------------------- 显式指令


@on_command("刷B站", aliases=("刷b站", "刷b站视频", "刷"), priority=6)
async def cmd_scroll(ctx):
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    arg = ctx.arg_text.strip()
    m = re.search(r"\d+", arg)
    want = max(1, min(20, int(m.group(0)))) if m else 3
    topic = re.sub(r"\d+", "", arg).strip()
    await _send(ctx, f"去刷 {want} 个" + (f"（主题：{topic}）" if topic else "") + "，刷完汇报。")
    ctx.stop()
    await _bg(ctx, lambda: bili_scroll.run_round(ctx.bot, topic=topic, want=want),
              lambda r: _report("scroll", r))


@on_command("学评论", aliases=("学说话", "学评论区", "学一下"), priority=6)
async def cmd_learn(ctx):
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    m = re.search(r"\d+", ctx.arg_text)
    videos = max(1, min(6, int(m.group(0)))) if m else 3
    await _send(ctx, f"去翻 {videos} 个视频的评论区学一下。")
    ctx.stop()
    await _bg(ctx, lambda: bili_learn.run_round(ctx.bot, videos=videos, reason="QQ 指挥"),
              lambda r: _report("learn", r))


@on_command("互动", aliases=("自动互动", "去互动", "刷评论"), priority=6)
async def cmd_auto(ctx):
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    await _send(ctx, "去评论区转转，回几条、发条弹幕。")
    ctx.stop()

    async def _run():
        from pawbot import bili_auto
        return await bili_auto.run_round(ctx.bot, reason="QQ 指挥")

    await _bg(ctx, _run, lambda r: _report("auto", r))


@on_command("发弹幕", aliases=("弹幕",), priority=6)
async def cmd_danmaku(ctx):
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    arg = ctx.arg_text.strip()
    bvid = _bvid_from(arg) or _last_bvid(ctx.bot)
    text = re.sub(r"BV[0-9A-Za-z]{10}", "", arg).strip() or "好耶[doge]"
    if not bvid:
        await _send(ctx, "还没刷过 B 站，先 /刷3 攒点视频再发弹幕。")
        ctx.stop()
        return
    await _send(ctx, f"发到 {bvid}：{text[:40]}")
    ctx.stop()

    async def _run():
        from pawbot import bili_extra
        return await bili_extra.send_danmaku(bvid, text[:60], progress_sec=float(random.randint(8, 180)))

    await _bg(ctx, _run, lambda r: _report("danmaku", r))


@on_command("评论", aliases=("发评论",), priority=6)
async def cmd_comment(ctx):
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    arg = ctx.arg_text.strip()
    bvid = _bvid_from(arg) or _last_bvid(ctx.bot)
    text = re.sub(r"BV[0-9A-Za-z]{10}", "", arg).strip()
    if not text:
        await _send(ctx, "评论内容呢？例如：评论 这个视频笑死")
        ctx.stop()
        return
    if not bvid:
        await _send(ctx, "还没刷过 B 站，先 /刷3 攒点视频再评论。")
        ctx.stop()
        return
    await _send(ctx, f"评到 {bvid}：{text[:40]}")
    ctx.stop()

    async def _run():
        from pawbot import bili_extra
        return await bili_extra.comment_video(bvid, text[:200])

    await _bg(ctx, _run, lambda r: _report("comment", r))


@on_command("权限", aliases=("whoami",), priority=6)
async def cmd_perm(ctx):
    bot = ctx.bot
    owners = [str(x) for x in (bot.config.get("account.owner_ids") or [])]
    white = [str(x) for x in (bot.config.get("whitelist.user_ids") or [])]
    uid = str(ctx.user_id)
    role = "主人" if uid in owners else ("白名单管理员" if uid in white else "普通成员")
    await _send(ctx,
                f"你是：{role}\n"
                f"主人：{', '.join(o[-4:] for o in owners) or '（未配置）'}\n"
                f"白名单：{', '.join(w[-4:] for w in white) or '（空，所有人可聊）'}\n"
                f"你的尾号：{uid[-4:]}\n"
                "直接说人话就行：刷三个视频 / 学一下评论区说话 / 去评论区互动 / 发弹幕 好耶 / 评论 笑死")
    ctx.stop()


@on_command("人设", aliases=("人设是什么",), priority=6)
async def cmd_persona(ctx):
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能看。")
        ctx.stop()
        return
    try:
        from pawbot.selfedit import 小节标题们
        with open(ctx.bot.persona.persona_path, "r", encoding="utf-8") as f:
            titles = 小节标题们(f.read())
    except Exception as e:
        await _send(ctx, f"读人设失败：{type(e).__name__}: {e}")
        ctx.stop()
        return
    await _send(ctx, "人设小节：" + "、".join(titles[:20]))
    ctx.stop()
