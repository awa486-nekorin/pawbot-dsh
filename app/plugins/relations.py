"""关系网记录：谁回复谁、谁 @ 谁、谁戳谁，顺手记住群主和管理。

不做任何网络请求，也不改 core：
  * 引用 → 从 store 最近窗口里按昵称找回 QQ 号；
  * @ → 直接读消息里的 at 段；
  * 戳一戳 → 读 notice 的 target_id，字段缺失时去 raw 里兜底；
  * 群主/管理 → 只读 groupinfo 已有的缓存，没缓存就等下一轮。

所有处理器都是 block=False，并且整段 try/except：记录关系是顺手的事，
不能因为它出错就耽误正常聊天。
"""

from pawbot import groupinfo
from pawbot import relations as relations_mod
from pawbot.log import log
from pawbot.plugin import on_message, on_notice

__plugin_name__ = "关系网"
__version__ = "1.0.0"
__description__ = "记录谁回复谁、谁 @ 谁、谁戳谁，并缓存群主/管理"

WINDOW = 300


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _first_id(*values) -> str:
    for value in values:
        uid = _digits(value)
        if uid:
            return uid
    return ""


def _quoted_uid(bot, session_id: str, quoted_from: str, user_id) -> str:
    """把「被引用者的昵称」映射回 QQ 号。

    只看 store 最近窗口里带 uid 的真人消息；同名时取最近一条，
    跳过说话人自己（自己引用自己不算互动）。
    """
    target = " ".join(str(quoted_from or "").split())
    if not target:
        return ""
    try:
        rows = bot.store.window(session_id, WINDOW)
    except Exception:
        return ""
    me = _digits(user_id)
    for row in reversed(list(rows or [])):
        if not isinstance(row, dict) or row.get("role") == "assistant":
            continue
        if " ".join(str(row.get("who") or "").split()) != target:
            continue
        uid = _digits(row.get("uid"))
        if uid and uid != me:
            return uid
    return ""


def _group_meta(rel, bot, gid) -> None:
    """只读缓存，绝不在这里打协议请求。"""
    try:
        info = groupinfo.for_bot(bot).cached(gid)
    except Exception:
        return
    if not isinstance(info, dict) or not info:
        return
    admins = []
    for item in info.get("admins") or []:
        admins.append(item.get("uid") if isinstance(item, dict) else item)
    rel.set_group_meta(gid, info.get("owner_id") or 0, admins)


@on_message(priority=70, block=False)
async def note_message(ctx):
    try:
        ev = ctx.event
        bot = ctx.bot
        rel = relations_mod.for_bot(bot)
        if not rel.enabled():
            return
        uid = getattr(ev, "user_id", 0)
        if not _digits(uid):
            return
        is_group = bool(getattr(ev, "is_group", False))
        gid = getattr(ev, "group_id", None) if is_group else None

        # 顺手记群名片：只存称呼，不存聊天内容
        try:
            rel.remember_name(uid, getattr(ev, "sender_name", ""))
        except Exception as e:
            log.debug(f"关系网记名字失败：{type(e).__name__}")

        # 1) 引用：谁回应了谁
        if getattr(ev, "reply_id", None) and getattr(ev, "quoted_from", ""):
            other = _quoted_uid(bot, getattr(ev, "session_id", ""),
                                getattr(ev, "quoted_from", ""), uid)
            if other:
                rel.note_interaction(uid, other, "reply", group_id=gid)

        # 2) @：消息里的 at 段；跳过 @ 全体、@ 自己、@ 机器人
        self_id = _digits(getattr(ctx, "self_id", 0) or getattr(ev, "self_id", 0))
        seen: set[str] = set()
        for seg in getattr(ev, "message", []) or []:
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            target = _digits(data.get("qq"))
            if (not target or target == self_id or target == _digits(uid)
                    or target in seen):
                continue
            seen.add(target)
            rel.note_interaction(uid, target, "mention", group_id=gid)

        # 3) 群主 / 管理：复用 groupinfo 缓存
        if gid:
            _group_meta(rel, bot, gid)
    except Exception as e:
        log.debug(f"关系网记录失败：{type(e).__name__} {e}")


@on_notice(notice_types=("notify",), priority=70, block=False)
async def note_poke(ctx):
    try:
        ev = ctx.event
        if str(getattr(ev, "sub_type", "")) != "poke":
            return
        rel = relations_mod.for_bot(ctx.bot)
        if not rel.enabled():
            return
        raw = getattr(ev, "raw_notice", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        # 谁戳的：NapCat 一般放 user_id；别的实现可能是 sender_id / operator_id
        actor = _first_id(getattr(ev, "user_id", 0), raw.get("user_id"),
                          raw.get("sender_id"), raw.get("operator_id"))
        # 被戳的：target_id 优先；完全没有时退回 raw 的 target_id / user_id
        target = _first_id(getattr(ev, "target_id", 0), raw.get("target_id"),
                           raw.get("receiver_id"), raw.get("target"))
        if not target:
            fallback = _first_id(raw.get("target_id"), raw.get("user_id"))
            if fallback and fallback != actor:
                target = fallback
        if not actor or not target or actor == target:
            return
        self_id = _digits(getattr(ctx, "self_id", 0))
        if self_id and (actor == self_id or target == self_id):
            # 跟机器人自己的互动不算「群友之间」的关系
            return
        rel.note_interaction(actor, target, "poke",
                             group_id=getattr(ev, "group_id", None))
    except Exception as e:
        log.debug(f"关系网记录戳一戳失败：{type(e).__name__} {e}")
