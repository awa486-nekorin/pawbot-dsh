"""主人 / 白名单专属：QQ 里直接指挥刷 B 站、学评论、互动、发弹幕、评论视频。

判断「这句话是不是在让她干活」交给**决策模型**（perplexity/pplx-decider-v1.2），
不是正则。概率 > 0.5 就执行，否则当普通聊天放行。

触发面：
- 私聊：主人的话都会过一遍决策模型
- 群里：只在她被 @ / 叫到的时候过一遍（避免把群聊全当命令）

显式指令（//刷B站 5 //学评论 //互动 //发弹幕 xxx //评论 xxx //点赞 BV号
//收藏 BV号 //投币 BV号 [1|2] //删评论 [BV号|序号|关键词|全部] //我的评论 [BV号]
//权限 //人设）仍然保留，不经过决策模型，直接执行。
2026-11-03 云端等价：
- 自然语言「删评论」已恢复（intent delcomment），按话里的 BV号 / 第 N 条 / 全部 /
  默认最近 N 条选删除入口；
- 批量删除开放（最近 / 序号 / 文本 / 整条视频），但**只删账本里记着的、自己发的**
  评论，并且仍然只认**主人本人当前消息**换来的 bili_gate 进程内凭证（动作+目标绑定）。
白名单能指挥只读/刷片/学评论，不能代发/代删。

权限 = account.owner_ids ∪ whitelist.user_ids。别人说同样的话不会被当成命令。
"""

import asyncio
import random
import re

from pawbot import bili_learn, bili_gate, bili_scroll, bili_voice, intent
from pawbot.log import log
from pawbot.plugin import on_command, on_message

__plugin_name__ = "管理员指令"
__version__ = "1.4.0"
__description__ = "主人明确指令逐动作授权（刷B站/学评论/发弹幕/评论/点赞/收藏/投币/删自己的评论，含批量）"

# ---------------------------------------------------------------- 权限


def _admins(bot) -> set[str]:
    ids = {str(x) for x in (bot.config.get("account.owner_ids") or [])}
    ids |= {str(x) for x in (bot.config.get("whitelist.user_ids") or [])}
    return ids


def _is_admin(ctx) -> bool:
    return str(ctx.user_id) in _admins(ctx.bot)


def _is_owner(ctx) -> bool:
    """只有 account.owner_ids 里的人；白名单**不算**写授权。"""
    try:
        owners = {str(x) for x in (ctx.bot.config.get("account.owner_ids") or [])}
    except Exception:
        return False
    return str(ctx.user_id) in owners


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


def _write_grant(ctx, action: str, target: str):
    """主人的当前真实消息 → **绑定动作+目标**的进程内写凭证；换不到就回 None。"""
    try:
        return bili_gate.owner_turn_grant(
            ctx.bot, ctx, action=action, target=target)
    except Exception:
        return None


def _grant_factory(ctx):
    def _make(action, target):
        return _write_grant(ctx, action, target)
    return _make


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


def _fmt_react(r: dict) -> str:
    """把这一轮的点赞/收藏/投币拼成一行（没有就返回空串）。"""
    x = (r or {}).get("reacted") or {}
    like, fav, coin = (int(x.get("like") or 0), int(x.get("fav") or 0),
                       int(x.get("coin") or 0))
    if not (like or fav or coin):
        return ""
    parts = []
    if like:
        parts.append("点赞 %d" % like)
    if fav:
        parts.append("收藏 %d" % fav)
    if coin:
        parts.append("投币 %d" % coin)
    return "我自己也动了手：" + "、".join(parts)


def _fmt_delete(r: dict) -> str:
    """删评论的结果：删了哪几条 / 一条没删就给原因（列清单也走这里）。"""
    done = r.get("deleted") or []
    if not done:
        return str(r.get("reason") or r.get("error") or "没找到要删的评论")[:800]
    lines = ["删掉 %d 条评论：" % len(done)]
    for x in done[:8]:
        lines.append("· " + str(x.get("text") or "")[:34])
    bad = r.get("failed") or []
    if bad:
        lines.append("有 %d 条没删掉：%s" % (len(bad), str(bad[0].get("error") or "")[:90]))
    return "\n".join(lines)


def _report(action: str, r: dict) -> str:
    if not r:
        return "做完了，但没返回结果。"
    if not r.get("ok") and action not in ("danmaku", "comment", "like", "favorite",
                                          "coin", "delete_comment", "delcomment",
                                          "my_comments"):
        return "没做成：" + str(r.get("reason") or r)[:200]
    if action == "scroll":
        notes = r.get("notes") or []
        lines = [f"刷完了：检查 {r.get('checked', 0)} 个，攒下 {len(notes)} 篇笔记"]
        for n in notes[:8]:
            lines.append(f"· {str(n.get('title') or '')[:36]} — {n.get('bvid', '')}")
        extra = _fmt_react(r)
        if extra:
            lines.append(extra)
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
        if r.get("ok"):
            return "弹幕发出去了（B 站已确认）。"
        if r.get("unconfirmed"):
            return "弹幕不确定：B 站没给可验证回执，别当成已经发出去了。" + \
                   str(r.get("reason") or r.get("error") or "")[:120]
        return "弹幕没发成：" + str(r.get("reason") or r.get("error") or "")[:160]
    if action == "comment":
        if r.get("ok"):
            return "评论发出去了（B 站已确认）。"
        if r.get("unconfirmed"):
            return "评论不确定：发布后没核验到，别当成已经发出去了。" + \
                   str(r.get("reason") or r.get("error") or "")[:120]
        return "评论没发成：" + str(r.get("reason") or r.get("error") or "")[:160]
    if action in ("like", "favorite", "coin", "delete_comment"):
        名 = {"like": "点赞", "favorite": "收藏", "coin": "投币",
              "delete_comment": "删评论"}[action]
        if r.get("ok"):
            if r.get("verified") is False:
                return 名 + "做成了（B 站后端确认；读后核验没查到，只作提示）。"
            return 名 + "做成了（B 站后端已确认）。"
        if r.get("denied"):
            return 名 + "没做：" + str(r.get("error") or r.get("reason") or "")[:160]
        if r.get("unconfirmed"):
            return 名 + "不确定：没有读到可核验的证据，别当成已经做成了。" + \
                   str(r.get("error") or r.get("reason") or "")[:120]
        return 名 + "没成：" + str(r.get("error") or r.get("reason") or "")[:160]
    if action == "my_comments":
        if not r.get("ok"):
            return "查不了：" + str(r.get("error") or r.get("reason") or "")[:160]
        items = (r.get("data") or {}).get("items") or []
        if not items:
            return "这个视频底下没查到我发过的评论。"
        lines = ["我发过的评论（视频 + rpid，删的时候要带这两个）："]
        for x in items[:10]:
            lines.append("· %s rpid=%s %s" % (x.get("bvid") or "", x.get("rpid") or "",
                                              str(x.get("text") or "")[:34]))
        lines.append("要删哪条：//删评论 2 ｜ //删评论 BV1xxxxxxxx ｜ "
                     "//删评论 关键词 ｜ //删评论 全部（只删自己发的）")
        return "\n".join(lines)
    if action == "delcomment":
        return _fmt_delete(r)
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
             "delcomment": "删评论", "danmaku": "发弹幕", "comment": "发评论"}
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
            extra = _fmt_react(res)
            if extra:
                lines.append(extra)
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
                lines.append(label + "发出去了（B 站已确认）：" + str(res.get("text") or "")[:40])
            elif res.get("unconfirmed"):
                lines.append(label + "不确定：B 站没确认，别当成已经发出去了。")
            else:
                lines.append(label + "没发成：" + str(res.get("reason") or res.get("error") or "")[:120])
        elif action == "delcomment":
            lines.append(_fmt_delete(res))
        else:
            lines.append(str(res.get("reason") or res)[:120])
    return "\n".join(lines) or "做完了。"


# ---------------------------------------------------------------- 决策模型入口


@on_message(priority=8, block=False)
async def admin_natural_command(ctx):
    """主人的话先过决策模型：是不是在指挥她干活？是就去做。

    block=False 是必须的：不是命令时要让后面的 ai_chat 继续接话；
    真命令的分支会自己 ctx.stop()，不需要靠阻断整条消息来拦住聊天。
    """
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
    if not tasks:
        ctx.stop()
        return
    await _send(ctx, _confirm_tasks(tasks))
    ctx.stop()

    bot = ctx.bot
    verdict_copy = dict(verdict)

    async def _run():
        return await intent.run(bot, verdict_copy, reason="QQ 指挥", ctx=ctx)

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
    if not _is_owner(ctx):
        await _send(ctx, "自动互动只能由主人本人的当前消息发起；"
                        "白名单/转发/定时之外没有写授权。")
        ctx.stop()
        return
    await _send(ctx, "去评论区转转，回几条、发条弹幕（每一步都过写闸门）。")
    ctx.stop()

    async def _run():
        from pawbot import bili_auto
        return await bili_auto.run_round(ctx.bot, reason="QQ 指挥",
                                         grant_factory=_grant_factory(ctx))

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
        await _send(ctx, "还没刷过 B 站，先 //刷B站 3 攒点视频再发弹幕。")
        ctx.stop()
        return
    grant = _write_grant(ctx, "send_danmaku", bvid)
    if grant is None:
        await _send(ctx, "发弹幕只能由主人本人的当前消息发起；白名单/转发不算写授权。")
        ctx.stop()
        return
    await _send(ctx, f"发到 {bvid}：{text[:40]}")
    ctx.stop()

    async def _run():
        r = await bili_scroll.action(
            ctx.bot, "send_danmaku",
            {"bvid": bvid, "text": text[:60],
             "progress": float(random.randint(8, 180))},
            grant=grant)
        # 主人手打的原文不改写；确认发出后才进「近期自发言」（失败不记）。
        bili_voice.note_sent(ctx.bot, r, kind="danmaku", bvid=bvid, text=text[:60])
        return r

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
        await _send(ctx, "评论内容呢？例如：//评论 这个视频笑死")
        ctx.stop()
        return
    if not bvid:
        await _send(ctx, "还没刷过 B 站，先 //刷B站 3 攒点视频再评论。")
        ctx.stop()
        return
    grant = _write_grant(ctx, "comment", bvid)
    if grant is None:
        await _send(ctx, "发评论只能由主人本人的当前消息发起；白名单/转发不算写授权。")
        ctx.stop()
        return
    await _send(ctx, f"评到 {bvid}：{text[:40]}")
    ctx.stop()

    async def _run():
        r = await bili_scroll.action(
            ctx.bot, "comment", {"bvid": bvid, "text": text[:200]}, grant=grant)
        # 主人手打的原文不改写；确认发出后才进「近期自发言」（失败不记）。
        bili_voice.note_sent(ctx.bot, r, kind="comment", bvid=bvid,
                             text=text[:200])
        return r

    await _bg(ctx, _run, lambda r: _report("comment", r))


def _target_bvid(ctx) -> str:
    """指令里给的 BV 号；没给就用最近一篇笔记的视频。"""
    return _bvid_from(ctx.arg_text) or _last_bvid(ctx.bot)


@on_command("点赞", aliases=("给个赞",), priority=6)
async def cmd_like(ctx):
    """//点赞 [BV号]：给一个视频点赞（一次一个目标，读后核验才算成功）。"""
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    bvid = _target_bvid(ctx)
    if not bvid:
        await _send(ctx, "还没刷过 B 站，先 //刷B站 3 攒点视频再点赞。")
        ctx.stop()
        return
    grant = _write_grant(ctx, "like", bvid)
    if grant is None:
        await _send(ctx, "点赞只能由主人本人的当前消息发起；白名单/转发不算写授权。")
        ctx.stop()
        return
    await _send(ctx, f"去给 {bvid} 点赞。")
    ctx.stop()
    await _bg(ctx,
              lambda: bili_scroll.action(ctx.bot, "like", {"bvid": bvid}, grant=grant),
              lambda r: _report("like", r))


@on_command("收藏", aliases=("加收藏",), priority=6)
async def cmd_favorite(ctx):
    """//收藏 [BV号]：收藏一个视频（一次一个目标，读后核验才算成功）。"""
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    bvid = _target_bvid(ctx)
    if not bvid:
        await _send(ctx, "还没刷过 B 站，先 //刷B站 3 攒点视频再收藏。")
        ctx.stop()
        return
    grant = _write_grant(ctx, "favorite", bvid)
    if grant is None:
        await _send(ctx, "收藏只能由主人本人的当前消息发起；白名单/转发不算写授权。")
        ctx.stop()
        return
    await _send(ctx, f"去收藏 {bvid}。")
    ctx.stop()
    await _bg(ctx,
              lambda: bili_scroll.action(ctx.bot, "favorite", {"bvid": bvid}, grant=grant),
              lambda r: _report("favorite", r))


@on_command("投币", aliases=("硬币",), priority=6)
async def cmd_coin(ctx):
    """//投币 [BV号] [1|2]：投 1 或 2 个硬币；只按这条指令授权，不顺手点赞。"""
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    bvid = _target_bvid(ctx)
    if not bvid:
        await _send(ctx, "还没刷过 B 站，先 //刷B站 3 攒点视频再投币。")
        ctx.stop()
        return
    rest = re.sub(r"BV[0-9A-Za-z]{10}", "", ctx.arg_text)
    m = re.search(r"(?<!\d)([12])(?!\d)", rest)
    num = 2 if m and m.group(1) == "2" else 1
    grant = _write_grant(ctx, "coin", bvid)
    if grant is None:
        await _send(ctx, "投币只能由主人本人的当前消息发起；白名单/转发不算写授权。")
        ctx.stop()
        return
    await _send(ctx, f"去给 {bvid} 投 {num} 个硬币（不顺手点赞）。")
    ctx.stop()
    await _bg(ctx,
              lambda: bili_scroll.action(
                  ctx.bot, "coin", {"bvid": bvid, "num": num}, grant=grant),
              lambda r: _report("coin", r))


@on_command("我的评论", aliases=("我发过的评论",), priority=6)
async def cmd_my_comments(ctx):
    """//我的评论 [BV号]：只读列出凭证账户在这个视频下发过的评论（带 rpid）。"""
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    bvid = _target_bvid(ctx)
    if not bvid:
        await _send(ctx, "还没刷过 B 站，先 //刷B站 3 攒点视频，或者带上 BV 号。")
        ctx.stop()
        return
    await _send(ctx, f"去看看我在 {bvid} 底下发过的评论…")
    ctx.stop()
    await _bg(ctx,
              lambda: bili_scroll.action(
                  ctx.bot, "my_comments", {"bvid": bvid, "limit": 20}),
              lambda r: _report("my_comments", r))


async def _scan_recent_comments(bot, videos: int = 3, per: int = 20) -> list:
    """账本空的时候，去最近几篇笔记的视频评论区里扫自己发过的评论。"""
    from pawbot import bili_extra

    out = []
    for note in bili_scroll.recent_notes(bot, limit=videos):
        try:
            with open(note["path"], "r", encoding="utf-8") as f:
                bv = _bvid_from(f.read(3000))
        except OSError:
            continue
        if not bv:
            continue
        try:
            out.extend(await bili_extra.scan_my_comments(bv, per))
        except Exception as e:
            log.debug(f"扫我的评论失败 {bv}：{type(e).__name__}")
    return out


@on_command("删评论", aliases=("删除评论", "删掉评论", "撤评论", "删我评论"), priority=6)
async def cmd_del_comment(ctx):
    """删掉她自己在 B 站发过的评论（云端原版用法）。

    用法：
        //删评论            列出最近发过的评论
        //删评论 2          删列表里的第 2 条
        //删评论 BV1xxxxxxxx  删这个视频底下她发的全部评论
        //删评论 关键词      删内容里带这个词的评论
        //删评论 全部        删掉账本里最近发过的所有评论

    删除仍然只认主人本人当前消息换来的 bili_gate 凭证（动作+目标绑定），
    而且只动账本/评论区里属于凭证账户自己的评论。
    """
    if not _is_admin(ctx):
        await _send(ctx, "这个只有主人或白名单里的人能指挥。")
        ctx.stop()
        return
    if not _is_owner(ctx):
        await _send(ctx, "删评论只能由主人本人的当前消息发起；"
                         "白名单/转发/定时之外没有删除授权。")
        ctx.stop()
        return
    arg = ctx.arg_text.strip()
    bot = ctx.bot

    # 不带参数 = 只读列清单：不换凭证、不删任何东西。
    if not arg:
        await _send(ctx, "去看看发过的评论…")
        ctx.stop()

        async def _list():
            from pawbot import bili_extra
            items = bili_extra.my_comments(bot, limit=10)
            if not items:
                items = await _scan_recent_comments(bot)
            if not items:
                return {"ok": False, "deleted": [], "failed": [],
                        "reason": "我这边没有发过评论的记录"
                                  "（账本空，最近笔记的视频里也没扫到）"}
            lines = ["我发过的评论（新的在前）："]
            for i, x in enumerate(items[:10], 1):
                lines.append("%d. %s（%s）" % (i, str(x.get("text") or "")[:34],
                                              x.get("bvid") or "回复"))
            lines.append("要删哪条：//删评论 2 ｜ //删评论 BV1xxxxxxxx ｜ "
                         "//删评论 关键词 ｜ //删评论 全部")
            return {"ok": True, "deleted": [], "failed": [],
                    "reason": "\n".join(lines)}

        await _bg(ctx, _list, lambda r: _report("delcomment", r))
        return

    if arg in ("全部", "所有", "都删", "全删", "清理"):
        名, 参 = "delete_recent", {"n": 999}
    elif arg.isdigit():
        名, 参 = "delete_by_index", {"index": int(arg)}
    else:
        bv = _bvid_from(arg)
        名, 参 = (("delete_on_video", {"bvid": bv}) if bv
                  else ("delete_by_text", {"text": arg}))
    grant = _write_grant(ctx, 名, bili_gate.target_key(名, 参))
    if grant is None:
        await _send(ctx, "删评论只能由主人本人的当前消息发起；白名单/转发不算删除授权。")
        ctx.stop()
        return
    await _send(ctx, f"去删自己发过的评论（{名}）。")
    ctx.stop()
    await _bg(ctx,
              lambda: bili_scroll.action(bot, 名, 参, grant=grant),
              lambda r: _report("delcomment", r))


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
                "指令要 // 开头：//刷B站 5 ｜ //学评论 3 ｜ //互动 ｜ //发弹幕 好耶 ｜ "
                "//评论 笑死 ｜ //点赞 BV号 ｜ //收藏 BV号 ｜ //投币 BV号 [1|2] ｜ "
                "//我的评论 [BV号] ｜ //删评论 2 ｜ //删评论 BV号 ｜ //删评论 关键词 ｜ "
                "//删评论 全部 ｜ //权限\n"
                "删除只动账本里你自己发过的评论；每次公开写仍要过闸门（主人本人凭证+审计）。"
                "不带 // 的话一律当聊天，不会自动去干活。")
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
