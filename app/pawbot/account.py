"""Owner-only account actions shared by commands and model tools.

Integration: bot.account = AccountActions(bot); await bot.account.execute(ctx,
operation, value='', target_user_id=None) -> (text, []). No configuration I/O.
Only the current raw message supplies intent, values, images and title targets.
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

from .message import normalize
from .onebot import ActionError

SUBCOMMANDS = {
    "能力": "capability", "资料": "profile", "資料": "profile",
    "头像": "avatar", "頭像": "avatar", "昵称": "nickname", "暱稱": "nickname",
    "签名": "signature", "簽名": "signature", "名片": "group_card",
    "群名": "group_name", "头衔": "special_title", "頭銜": "special_title",
}
_NAMES = {
    "avatar": r"(?:头像|頭像)", "nickname": r"(?:昵称|暱稱|昵稱|名字)",
    "signature": r"(?:签名|簽名)", "group_card": r"(?:群名片|名片)",
    "group_name": r"(?:群名|群名称|群名稱)",
    "special_title": r"(?:专属头衔|專屬頭銜|头衔|頭銜)",
}
_LIMITS = {"nickname": 36, "signature": 200, "group_card": 60,
           "group_name": 60, "special_title": 18}
_HELP = ("我能改自己的头像、昵称（名字）、个性签名、当前群名片；有群权限时还能改群名和专属头衔。"
         "群名要管理员或群主，头衔要群主才能改。个人标签没有可用的接口，做不了；"
         "也不会退群、删好友或拉黑。\n"
         "直接用普通话说就行，不需要指令：\n"
         "· 把昵称改成小蓝鱼 / 名字改成小蓝鱼\n"
         "· 帮我改签名 游来游去\n"
         "· 把头像换成 <HTTPS 图片地址>（或直接发图说“换成这张图”）\n"
         "· 把群名片改成 塘主 / 把群名改成 鱼塘 / 给我头衔 塘主\n"
         "只认 account.owner_ids 里主人本人的话，别人说的不会执行。")
_VERB = r"(?:修改|更改|更换|更換|设置|設置|设定|設定|改|换|換)"
_CURRENT_IMAGE = ("这张图片", "這張圖片", "这张图", "這張圖", "这张", "這張")


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return int(text) if re.fullmatch(r"[1-9][0-9]{0,19}", text) else None


def _literal(value):
    value = value.strip()
    for left, right in (("\"", "\""), ("'", "'"), ("“", "”"), ("「", "」"), ("『", "』")):
        if len(value) >= 2 and value.startswith(left) and value.endswith(right):
            return value[1:-1]
    return value


def _url(value, *, image=False):
    if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\s\\\x00-\x1f]", value):
        return False
    try:
        url = urlsplit(value)
        if url.scheme not in (("https", "http") if image else ("https",)):
            return False
        if not url.hostname or url.username or url.password or url.fragment:
            return False
        if url.port not in (None, 80, 443):
            return False
        host = url.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return bool(re.fullmatch(r"[a-z0-9.-]+", host))
    except ValueError:
        return False


class AccountActions:
    def __init__(self, bot):
        self.bot = bot

    def _current(self, ctx):
        ev = ctx.event
        raw = getattr(ev, "raw", None)
        if not isinstance(raw, dict) or raw.get("post_type") != "message" or ev.type != "message":
            raise ValueError("只接受主人当前原始消息的直接请求。")
        for key in ("self_id", "user_id"):
            if not _id(raw.get(key)) or _id(raw.get(key)) != _id(getattr(ctx, key, None)):
                raise ValueError("原始事件身份不一致，拒绝账号操作。")
        if ctx.user_id == ctx.self_id:
            raise ValueError("不接受机器人自己的消息。")
        if (raw.get("message_type") == "group") != bool(ctx.is_group):
            raise ValueError("原始事件会话类型不一致。")
        if ctx.is_group and (not _id(ctx.group_id) or _id(raw.get("group_id")) != _id(ctx.group_id)):
            raise ValueError("原始事件群号不一致。")
        owners = self.bot.config.get("account.owner_ids", [])
        if not isinstance(owners, (list, tuple)) or not owners:
            raise ValueError("未设置 account.owner_ids，拒绝账号操作。")
        if _id(ctx.user_id) not in {_id(owner) for owner in owners if _id(owner)}:
            raise ValueError("只有 account.owner_ids 中的主人可以操作账号。")
        # 下面的校验不是权限，是正确性前提：指令必须来自当前这条真实消息，
        # 不能是翻出来的历史消息或伪造事件。
        segments = normalize(raw.get("message"))
        if not segments and raw.get("raw_message"):
            segments = normalize(raw["raw_message"])
        text = "".join(str(s.get("data", {}).get("text", ""))
                       for s in segments if s.get("type") == "text").strip()
        return text, segments

    def command_request(self, ctx, text, segments):
        """Parse only a verified account command in the current raw event."""
        matched = getattr(ctx, "matched_command", "")
        if matched not in ("账号", "帳號"):
            return None
        prefixes = self.bot.config.get("bot.command_prefixes", ["/"])
        prefixes = prefixes if isinstance(prefixes, (tuple, list)) else ["/"]
        starts = [p + matched for p in prefixes if isinstance(p, str) and p]
        if self.bot.config.get("bot.allow_no_prefix", True):
            starts.append(matched)
        tail = None
        for start in starts:
            if text == start or (text.startswith(start) and text[len(start):len(start) + 1].isspace()):
                tail = text[len(start):].strip()
                break
        if tail is None:
            raise ValueError("指令必须直接出现在当前原始消息开头。")
        parts = tail.split(maxsplit=1)
        operation = SUBCOMMANDS.get(parts[0]) if parts else "capability"
        if operation is None:
            raise ValueError(_HELP)
        value = parts[1].strip() if len(parts) > 1 else ""
        target = None
        if operation == "special_title":
            parts = value.split(maxsplit=1)
            if parts and _id(parts[0]):
                target = _id(parts[0])
                value = parts[1] if len(parts) > 1 else ""
            mentions = self._targets(segments, ctx.self_id)
            if mentions:
                if len(mentions) != 1 or (target and target != mentions[0]):
                    raise ValueError("请只指定一个头衔目标。")
                target = mentions[0]
        elif self._targets(segments, ctx.self_id):
            raise ValueError("此操作只修改机器人自己的资料，不能指定其他成员。")
        return operation, _literal(value), target

    @staticmethod
    def _targets(segments, self_id):
        targets = []
        for segment in segments:
            if segment.get("type") != "at":
                continue
            qq = segment.get("data", {}).get("qq")
            if str(qq) == str(self_id):
                continue
            target = _id(qq)
            if target is None:
                raise ValueError("目标必须是当前消息中明确的一位 QQ 成员，不能是全体成员。")
            targets.append(target)
        return targets

    def _natural(self, ctx, operation, text):
        # 主人名单已在 _current 里校验过，这里只认这一条的原文：
        # 普通说话即可，不要求 @、唤醒词或指令前缀；但必须是完整一句修改请求，
        # 引文、否定、提问和复述别人的话都不算。
        words = self.bot.config.get("bot.wake_words", [])
        nickname = self.bot.config.get("bot.nickname", "")
        names = [w for w in (list(words) if isinstance(words, (list, tuple)) else []) + [nickname]
                 if isinstance(w, str) and w]
        for name in sorted(names, key=len, reverse=True):
            if text.startswith(name):
                text = text[len(name):].lstrip(" ,，:：")
                break
        unquoted = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'|“[^”\n]*”|「[^」\n]*」|『[^』\n]*』', "", text)
        if re.search(r"不要|別|别|不用|不必|不需要|无需|無需|禁止|取消|勿|不能|不许|不許|别执行|先别|先別", unquoted):
            raise ValueError("当前消息含否定或取消指示，拒绝修改。")
        if operation not in ("capability", "profile") and re.search(r"[?？]$", text):
            raise ValueError("请直接提出修改请求，询问不会执行修改。")
        # Anchored grammar deliberately refuses reported, hypothetical, quoted and negative commands.
        # Only value quotes are unwrapped, never quoted instructions or enriched ctx.plain/history.
        text = re.sub(r"^(?:(?:请|請|帮我|幫我|麻烦你|麻煩你|现在|現在|立刻)[ ,，]*)+", "", text)
        if operation in ("capability", "profile"):
            noun = r"(?:账号|帳號)?(?:能力|功能)" if operation == "capability" else r"(?:账号|帳號|个人|個人)?(?:资料|資料)"
            if re.fullmatch(r"(?:查看|查询|查詢|读取|讀取|显示|顯示|看看)(?:你的|自己(?:的)?|本人(?:的)?)?" + noun + r"[。！!？?]?", text):
                return ""
            raise ValueError("当前消息没有直接查询此项账号信息。")
        noun = _NAMES[operation]
        subject = r"(?:你的|自己的|机器人(?:的)?|機器人(?:的)?)?"
        patterns = (
            rf"(?:把|将|將)?{subject}{noun}{_VERB}(?:一下)?(?:成|为|為)?[ :：]*(?P<value>.+)",
            rf"{_VERB}(?:一下)?{subject}{noun}(?:成|为|為)?[ :：]*(?P<value>.+)",
        )
        if operation == "special_title":
            # 「给我头衔 塘主」是没有改/换动词的说法
            found = re.fullmatch(
                rf"(?:给我|給我|给个|給個)(?:一个|一個|个|個)?{noun}[ :：]*(?P<value>.+)",
                text, re.DOTALL)
            if found:
                return _literal(found["value"])
        for pattern in patterns:
            found = re.fullmatch(pattern, text, re.DOTALL)
            if found:
                value = _literal(found["value"])
                # 「你的名字改成什么了」「名字改了吗」不是指令，别把疑问词写成昵称
                if re.match(r"^(?:什么|什麼|啥|哪|多少|几|幾|谁|誰|了|吗|嗎|呢|吧|嘛)", value):
                    raise ValueError("这是问句或语气词，不执行修改。")
                return value
        raise ValueError("这条不像是要修改资料；否定、引文、提问和模型自称授权都不会执行。")

    def natural_request(self, ctx, text):
        """Identify a plain-language owner order, or None when this is just chat."""
        for operation in ("avatar", "nickname", "signature", "group_card",
                          "group_name", "special_title"):
            try:
                value = self._natural(ctx, operation, text)
            except ValueError:
                continue
            if operation == "avatar" and value in _CURRENT_IMAGE:
                value = ""
            return operation, value
        return None

    @staticmethod
    def _receipt(result):
        # Connection.call normally returns unwrapped data. Also reject explicit failure
        # envelopes and UserApi result codes (profile and longnick return these directly).
        if not isinstance(result, dict):
            raise ValueError("回执格式不明，无法确认操作结果。")
        if isinstance(result, dict):
            if result.get("status", "ok") != "ok" or result.get("retcode", 0) not in (0, "0"):
                raise ValueError("协议端回执未确认成功。")
            if result.get("result", 0) not in (0, "0"):
                raise ValueError("QQ 回执报告操作失败。")
            if "data" in result and isinstance(result["data"], dict):
                AccountActions._receipt(result["data"])
        return result or {}

    async def execute(self, ctx, operation, value="", target_user_id=None) -> tuple[str, list]:
        """Allowed operations: capability, profile, avatar, nickname, signature,
        group_card (alias card), group_name, special_title. Targets only for title.
        Empty values are accepted only for capability/profile or current-event avatar.
        """
        modifying = False
        try:
            if not isinstance(operation, str) or not isinstance(value, str):
                raise ValueError("operation 和 value 必须是字符串。")
            operation = {"card": "group_card", "capabilities": "capability", "read_profile": "profile"}.get(operation, operation)
            if operation not in (*_NAMES, "capability", "profile"):
                raise ValueError("不支持此账号操作；个人标签暂无已确认接口。")
            text, segments = self._current(ctx)
            supplied_target = _id(target_user_id) if target_user_id is not None else None
            if target_user_id is not None and not supplied_target:
                raise ValueError("目标 QQ 号必须是正整数字符串或整数。")
            if operation != "special_title" and target_user_id is not None:
                raise ValueError("只有专属头衔允许指定目标成员。")
            request = self.command_request(ctx, text, segments)
            if request:
                requested_op, requested_value, requested_target = request
                if requested_op != operation or value != requested_value:
                    raise ValueError("工具操作或值与当前原始指令不一致。")
                target = requested_target or ctx.self_id
                if supplied_target is not None and supplied_target != target:
                    raise ValueError("目标与当前原始指令不一致。")
            else:
                # 本轮 search_image 现找的图：地址本来就不在原话里，直接放行
                searched = ctx.state.get("avatar_ok_urls") or set()
                if (operation == "avatar" and value and _url(value)
                        and value in searched):
                    requested_value = value
                    target = ctx.self_id
                else:
                    requested_value = self._natural(ctx, operation, text)
                    target = ctx.self_id
                if operation == "special_title":
                    # Natural requests may target an explicit raw @mention or leading QQ number.
                    parts = requested_value.split(maxsplit=1)
                    if parts and _id(parts[0]):
                        target = _id(parts[0])
                        requested_value = _literal(parts[1]) if len(parts) > 1 else ""
                    mentions = self._targets(segments, ctx.self_id)
                    if mentions:
                        if len(mentions) != 1 or (target != ctx.self_id and target != mentions[0]):
                            raise ValueError("请只指定一个头衔目标。")
                        target = mentions[0]
                    if supplied_target is not None and supplied_target != target:
                        raise ValueError("目标成员不在当前原始请求中。")
                if operation == "avatar" and requested_value in _CURRENT_IMAGE:
                    requested_value = ""
                if value != requested_value:
                    raise ValueError("修改值必须与当前原始请求完全一致。")
            if operation in ("capability", "profile") and value:
                raise ValueError("查询操作不接受修改值。")
            if operation == "capability":
                return _HELP, []
            if operation in _LIMITS:
                if not value.strip() or len(value) > _LIMITS[operation] or any(
                    unicodedata.category(c) in ("Cc", "Cs") for c in value):
                    raise ValueError(f"内容须为非空文本，最多 {_LIMITS[operation]} 字，不能包含控制字符。")
            group_id = _id(ctx.group_id) if ctx.is_group else None
            if operation in ("group_card", "group_name", "special_title") and not group_id:
                raise ValueError("此操作只能在当前群使用。")
            avatar = value
            if operation == "avatar":
                images = [s.get("data", {}).get("url") for s in segments if s.get("type") == "image"]
                images = [url for url in images if _url(url, image=True)]
                if not value:
                    if len(images) != 1:
                        raise ValueError("请附上本条消息中的一张图片，或明确提供 HTTPS URL。")
                    avatar = images[0]
                else:
                    # 本轮的 search_image 结果可以直接拿来当头像（模型现场挑的图）
                    found = ctx.state.get("avatar_ok_urls") or set()
                    if not _url(value) or (value not in text and value not in found):
                        raise ValueError("头像只接受原文明确的 HTTPS URL、当前消息附图，"
                                         "或本轮刚搜到的图片；禁止本机路径。")
            conn = self.bot.conn_for(ctx.self_id)
            if conn is None or _id(getattr(conn, "self_id", None)) != _id(ctx.self_id):
                raise ValueError("没有此 QQ 账号的协议端连接。")
            if operation == "profile":
                data = self._receipt(await conn.call("get_stranger_info", user_id=ctx.self_id, no_cache=True))
                return (f"QQ：{ctx.self_id}\n昵称：{data.get('nickname', '未返回')}\n"
                        f"签名：{data.get('long_nick', data.get('longNick', '未返回'))}", [])
            if operation in ("group_name", "special_title"):
                info = self._receipt(await conn.call("get_group_member_info", group_id=group_id,
                                                    user_id=ctx.self_id, no_cache=True))
                allowed = ("owner", "admin") if operation == "group_name" else ("owner",)
                if info.get("role") not in allowed:
                    raise ValueError("机器人群权限不足：群名需要管理员或群主，专属头衔需要群主。")
            if operation == "avatar":
                if avatar.startswith("https://i.pximg.net/"):
                    # Pixiv 图床要 Referer，NapCat 自己拉会 403，这里先下好原图再交给它
                    from . import imagesearch
                    raw = imagesearch.download({"url": avatar, "need_referer": True,
                                                "referer": "https://www.pixiv.net/"})
                    if not raw:
                        raise ValueError("这张 Pixiv 图没下载成功，头像没有改。")
                    avatar = imagesearch.image_file(raw)
                action, params = "set_qq_avatar", {"file": avatar}
            elif operation == "nickname":
                action, params = "set_qq_profile", {"nickname": value}
            elif operation == "signature":
                action, params = "set_self_longnick", {"longNick": value}
            elif operation == "group_card":
                action, params = "set_group_card", {"group_id": group_id, "user_id": ctx.self_id, "card": value}
            elif operation == "group_name":
                action, params = "set_group_name", {"group_id": group_id, "group_name": value}
            else:
                action, params = "set_group_special_title", {"group_id": group_id, "user_id": target, "special_title": value}
            modifying = True
            self._receipt(await conn.call(action, **params))
            return "改好了。", []
        except ActionError as exc:
            if exc.delivered and modifying:
                return "请求已送出，但没有收到确定回执，修改结果不确定；请检查 QQ，不自动重试。", []
            return "账号操作失败，协议端未确认成功。", []
        except ValueError as exc:
            return str(exc), []
        except Exception:
            return "账号操作异常，无法确认结果；请检查 QQ 后再决定是否重试。", []
