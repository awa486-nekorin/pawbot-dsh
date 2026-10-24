"""
群事件反应：把「不是发给你的消息，但发生在你眼前的事」接住。

以前撤回、禁言、管理变动、退群、精华、群文件、好友申请这些 notice
全被丢掉了，AI 完全不知道群里刚发生过什么。现在：

  1. 每件都写进会话上下文（who="系统"），下次聊天就接得上
  2. 一部分值得说话的事件按概率、带冷却地回一句
  3. 好友申请默认只记日志，不自动同意；群申请永远不自动处理

所有 notice 处理器都是 block=False：别把别人的 notice（比如 poke）拦掉，
也不要阻塞同一条 notice 的其它插件。
"""

import asyncio
import random
import time

from pawbot.log import log
from pawbot.plugin import on_notice, on_request

__plugin_name__ = "群事件"
__version__ = "1.0.0"
__description__ = "撤回、禁言、管理员、退群、精华、群文件、好友申请的反应"

RECALL_LINES = [
    "撤回也没用，我看到啦。",
    "手速挺快啊，可惜我眼睛更快。",
    "撤回了？我已经记住了，怎么办。",
    "别撤了，越撤越显得有鬼。",
    "我刚看到一半呢，你给撤了。",
    "撤吧撤吧，反正我截图了。",
    "这条消息只在群里活了三秒。",
]

BAN_LIFT_LINES = [
    "终于放我出来了，憋死我了。",
    "解禁了？我还没被关够呢。",
    "禁言解除，我又回来了。",
    "这段时间我连表情包都发不出去。",
    "行吧，我出来了，刚才谁说的我坏话。",
    "下次再禁我，我就去你空间留言。",
    "放出来了，先让我喘口气。",
]

BAN_OTHER_LINES = [
    "哎呀，{name} 被关小黑屋了。",
    "{name} 先歇会儿吧，我们等你。",
    "怎么还被禁言了，心疼 {name} 一秒。",
    "小黑屋又多一位，{name} 保重。",
    "行吧，{name} 安静一会儿也好。",
]

ADMIN_SET_LINES = [
    "哟，{name} 升官了，恭喜恭喜。",
    "{name} 当管理员了，以后罩着我点。",
    "管理+1，{name} 请客吧。",
    "{name} 现在是有身份的人了。",
    "恭喜 {name}，群里以后靠你管了。",
]

SELF_ADMIN_LINES = [
    "我当管理员了？行吧，我尽量不作恶。",
    "升官了，从今天起收保护费。",
    "管理员+1，先给自己发个红包。",
    "居然让我管群，你们胆子不小。",
    "好，我现在是管理员了，都老实点。",
]

SELF_ADMIN_OFF_LINES = [
    "管理员没了，无官一身轻。",
    "撤我管理员？行，那我以后只负责摸鱼。",
    "官帽子被摘了，正好不想管。",
    "不当管理员了，这下可以随便说话了。",
]

DECREASE_LINES = [
    "哎，又走了一个。",
    "人少了，群里都安静了。",
    "怎么突然就走了呢。",
    "走了也好，说不定是去过好日子了。",
    "又少一个说话的，有点舍不得。",
]

FRIEND_LINES = [
    "加上了，你好呀。",
    "好友通过，以后可以私聊了。",
    "你好你好，我是群里摸鱼的那个。",
    "新好友？先认识一下，有事直接说。",
    "通过了，别客气。",
]

ESSENCE_LINES = [
    "这句被加精了，含金量挺高。",
    "哟，精华+1，谁这么有眼光。",
    "被加精了，我得把这句话裱起来。",
    "精华消息，以后这就是群史了。",
    "这也能加精？行，我记下了。",
]

# 冷却时间：key 是「事件类型:群号」
_LAST: dict[str, float] = {}


# ---------------------------------------------------------------- 小工具
def _remember(ctx, sid: str, role: str, text: str, who: str):
    """写进会话上下文。失败也不能影响事件处理本身。"""
    try:
        ctx.bot.store.append(sid, role, text, who=who)
    except Exception as e:
        log.debug(f"群事件入库失败：{type(e).__name__}")


def _chance(ctx, key: str, default: float) -> bool:
    try:
        p = float(ctx.config.get(f"events.{key}", default))
    except Exception:
        p = default
    return random.random() < p


def _cooldown(ctx, key: str, cfg_name: str, default: float) -> bool:
    try:
        seconds = float(ctx.config.get(f"events.{cfg_name}", default))
    except Exception:
        seconds = default
    now = time.time()
    if seconds > 0 and now - _LAST.get(key, 0.0) < seconds:
        return False
    _LAST[key] = now
    return True


async def _say(ctx, text: str, *, group_id, sid: str) -> bool:
    """先装一会儿人，再发；发成功才记进「我说过的话」。"""
    await asyncio.sleep(random.uniform(1.0, 3.0))
    try:
        if group_id:
            await ctx.send(text, group_id=int(group_id))
        else:
            await ctx.send(text)
    except Exception as e:
        log.warn(f"群事件发言失败：{type(e).__name__} {e}")
        return False
    _remember(ctx, sid, "assistant", text, "我")
    return True


def _label(ev) -> str:
    """notice 里的当事人怎么称呼：优先昵称，其次 QQ 号。"""
    try:
        sender = (getattr(ev, "raw_notice", {}) or {}).get("sender") or {}
    except Exception:
        sender = {}
    if isinstance(sender, dict):
        name = sender.get("card") or sender.get("nickname")
        if name:
            return str(name)
    uid = int(getattr(ev, "user_id", 0) or 0)
    return str(uid) if uid else "有人"


def _duration(seconds) -> str:
    try:
        seconds = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        seconds = 0
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _set_banned(bot, until: float):
    """记下「自己被禁言到什么时候」。life_state 由别的模块共用，没有就建。"""
    state = getattr(bot, "life_state", None)
    if state is None:
        state = {}
        try:
            setattr(bot, "life_state", state)
        except Exception:
            return
    try:
        if isinstance(state, dict):
            state["self_banned_until"] = float(until)
        else:
            setattr(state, "self_banned_until", float(until))
    except Exception as e:
        log.debug(f"禁言状态记录失败：{type(e).__name__}")


def _gid(ev) -> int:
    try:
        return int(getattr(ev, "group_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 撤回
@on_notice(notice_types=("group_recall",), priority=30, block=False)
async def on_group_recall(ctx):
    ev = ctx.event
    gid = _gid(ev)
    if not gid:
        return
    author = int(getattr(ev, "user_id", 0) or 0)
    operator = int(getattr(ev, "operator_id", 0) or 0)
    self_id = int(getattr(ctx, "self_id", 0) or 0)
    sid = f"group_{gid}"
    _remember(ctx, sid, "user", f"（系统：{_label(ev)} 撤回了一条消息）", "系统")

    # 自己撤回的不调侃自己；管理员撤别人的是管理动作，也不插嘴
    if author == self_id or operator == self_id:
        return
    if operator and operator != author:
        return
    if not _chance(ctx, "recall_reply_chance", 0.25):
        return
    if not _cooldown(ctx, f"recall:{gid}", "recall_cooldown", 600):
        return
    await _say(ctx, random.choice(RECALL_LINES), group_id=gid, sid=sid)


# ---------------------------------------------------------------- 禁言
@on_notice(notice_types=("group_ban",), priority=30, block=False)
async def on_group_ban(ctx):
    ev = ctx.event
    gid = _gid(ev)
    if not gid:
        return
    target = int(getattr(ev, "user_id", 0) or 0)
    operator = int(getattr(ev, "operator_id", 0) or 0)
    self_id = int(getattr(ctx, "self_id", 0) or 0)
    raw = getattr(ev, "raw_notice", {}) or {}
    try:
        duration = int(raw.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    lifted = str(getattr(ev, "sub_type", "")) == "lift_ban" or duration <= 0
    sid = f"group_{gid}"
    op = str(operator) if operator else "管理员"

    if target == self_id:
        if lifted:
            _set_banned(ctx.bot, 0)
            _remember(ctx, sid, "user", "（系统：你的禁言解除了）", "系统")
            await _say(ctx, random.choice(BAN_LIFT_LINES),
                       group_id=gid, sid=sid)
        else:
            _set_banned(ctx.bot, time.time() + duration)
            _remember(ctx, sid, "user",
                      f"（系统：你被 {op} 禁言了 {_duration(duration)}）", "系统")
        return

    if target == 0:
        # 全员禁言：不评价
        text = ("（系统：全员禁言解除了）" if lifted
                else f"（系统：{op} 开启了全员禁言）")
        _remember(ctx, sid, "user", text, "系统")
        return

    name = _label(ev)
    if lifted:
        _remember(ctx, sid, "user", f"（系统：{name} 的禁言解除了）", "系统")
        return
    _remember(ctx, sid, "user",
              f"（系统：{name} 被 {op} 禁言了 {_duration(duration)}）", "系统")
    if not _chance(ctx, "ban_reply_chance", 0.2):
        return
    if not _cooldown(ctx, f"ban:{gid}", "ban_cooldown", 600):
        return
    await _say(ctx, random.choice(BAN_OTHER_LINES).format(name=name),
               group_id=gid, sid=sid)


# ---------------------------------------------------------------- 管理员
@on_notice(notice_types=("group_admin",), priority=30, block=False)
async def on_group_admin(ctx):
    ev = ctx.event
    gid = _gid(ev)
    if not gid:
        return
    target = int(getattr(ev, "user_id", 0) or 0)
    self_id = int(getattr(ctx, "self_id", 0) or 0)
    promoted = str(getattr(ev, "sub_type", "")) == "set"
    sid = f"group_{gid}"
    name = _label(ev)
    _remember(ctx, sid, "user",
              f"（系统：{name} {'被设为管理员' if promoted else '的管理员被取消了'}）",
              "系统")

    if target == self_id:
        lines = SELF_ADMIN_LINES if promoted else SELF_ADMIN_OFF_LINES
        await _say(ctx, random.choice(lines), group_id=gid, sid=sid)
        return
    if not promoted:
        return
    if not _chance(ctx, "admin_reply_chance", 0.5):
        return
    await _say(ctx, random.choice(ADMIN_SET_LINES).format(name=name),
               group_id=gid, sid=sid)


# ---------------------------------------------------------------- 退群 / 被踢
@on_notice(notice_types=("group_decrease",), priority=30, block=False)
async def on_group_decrease(ctx):
    ev = ctx.event
    gid = _gid(ev)
    if not gid:
        return
    kicked = str(getattr(ev, "sub_type", "")) == "kick"
    sid = f"group_{gid}"
    name = _label(ev)
    _remember(ctx, sid, "user",
              f"（系统：{name} {'被管理员移出了群聊' if kicked else '退出了群聊'}）",
              "系统")
    if kicked:
        return
    if not _chance(ctx, "decrease_reply_chance", 0.3):
        return
    await _say(ctx, random.choice(DECREASE_LINES), group_id=gid, sid=sid)


# ---------------------------------------------------------------- 加好友
@on_notice(notice_types=("friend_add",), priority=30, block=False)
async def on_friend_add(ctx):
    ev = ctx.event
    uid = int(getattr(ev, "user_id", 0) or 0)
    if not uid:
        return
    sid = f"private_{uid}"
    _remember(ctx, sid, "user", f"（系统：{uid} 加你为好友）", "系统")
    await _say(ctx, random.choice(FRIEND_LINES), group_id=None, sid=sid)


# ---------------------------------------------------------------- 精华消息
@on_notice(notice_types=("essence",), priority=30, block=False)
async def on_essence(ctx):
    ev = ctx.event
    gid = _gid(ev)
    if not gid:
        return
    sub = str(getattr(ev, "sub_type", "") or "")
    sid = f"group_{gid}"
    raw = getattr(ev, "raw_notice", {}) or {}
    author = int(raw.get("sender_id") or raw.get("user_id") or 0)
    name = str(author) if author else "有人"
    if sub not in ("", "add", "set"):
        _remember(ctx, sid, "user", "（系统：一条消息被移出了精华）", "系统")
        return
    _remember(ctx, sid, "user", f"（系统：{name} 的消息被设为了精华）", "系统")
    if not _chance(ctx, "essence_reply_chance", 0.5):
        return
    await _say(ctx, random.choice(ESSENCE_LINES), group_id=gid, sid=sid)


# ---------------------------------------------------------------- 群文件
@on_notice(notice_types=("group_upload",), priority=30, block=False)
async def on_group_upload(ctx):
    ev = ctx.event
    gid = _gid(ev)
    if not gid:
        return
    raw = getattr(ev, "raw_notice", {}) or {}
    file = raw.get("file") if isinstance(raw.get("file"), dict) else {}
    name = str(file.get("name") or "文件")
    log.info(f"群文件上传：群{gid} {_label(ev)} → {name}")
    _remember(ctx, f"group_{gid}", "user",
              f"（系统：{_label(ev)} 上传了群文件「{name}」）", "系统")


# ---------------------------------------------------------------- 申请
@on_request(priority=25, block=False)
async def on_add_request(ctx):
    """好友 / 入群申请：默认只记录，不替主人点头。

    auto_accept_friend 只对好友申请生效；入群申请无论配置如何都不自动同意。
    """
    ev = ctx.event
    rtype = str(getattr(ev, "request_type", "") or "")
    comment = str(getattr(ev, "comment", "") or "")[:60]
    if rtype == "friend":
        log.info(f"收到好友申请：{ev.user_id}（{comment}）")
        if not _chance_bool(ctx.config, "events.auto_accept_friend", False):
            return
        conn = ctx.bot.conn_for(ctx.self_id)
        if conn is None:
            log.warn("好友申请自动同意失败：没有在线的协议端")
            return
        try:
            await conn.call("set_friend_add_request", flag=ev.flag,
                            approve=True, timeout=10)
            log.info(f"已自动同意好友申请：{ev.user_id}")
        except Exception as e:
            log.warn(f"自动同意好友申请失败：{type(e).__name__} {e}")
        return
    if rtype == "group":
        log.info(f"收到入群申请：群{getattr(ev, 'group_id', None)} 用户{ev.user_id}"
                 f"（{comment}），不自动处理")
        return
    log.info(f"收到请求：{rtype} 用户{ev.user_id}（{comment}）")


def _chance_bool(config, key: str, default: bool) -> bool:
    try:
        return bool(config.get(key, default))
    except Exception:
        return default
