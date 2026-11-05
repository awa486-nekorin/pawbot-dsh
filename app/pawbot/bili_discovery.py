"""按来源发现 B 站视频（推荐 / 关注 / 稍后 / 收藏 / UP 投稿 / 相关 / 搜索）。

只做**发现和归一**，不判断值不值得看、不写笔记、不做任何写操作。上层拿到
``items`` 后可以自己决定交给 ``bili_scroll.run_round(candidates=...)``，也可以
在 ``ok=False`` 时显式回退到别的来源（本模块不会偷偷拿搜索冒充关注/推荐）。

来源与真实接口（均已核对本机 ``bilibili_api`` 源码）：

    recommend    homepage.get_videos(credential)            无登录也能读
    following    dynamic.get_dynamic_page_info(credential, _type=DynamicType.VIDEO)
    toview       user.get_toview_list(credential)           需登录
    favorites    favorite_list.get_video_favorite_list(...) / ...list_content
    user_videos  user.User(uid).get_videos(...)
    related      video.Video(bvid).get_related()
    search       vendored ``bili_mcp.mcp_server.bili_search``

归一字段固定为 ``bvid/title/author/play/duration/desc/url/source``。平台回包
形状会随接口和灰度变化，所以列表解析用了**有界递归**：只认字段名明确是
``bvid``（或视频链接里的 /video/BV...），绝不把 aid/cid/id 之类的任意数字
ID 当成 BV 号。
"""

from __future__ import annotations

import asyncio
import html
import json
import re

from . import bili_credentials
from .log import log

SOURCES = (
    "recommend",
    "following",
    "toview",
    "favorites",
    "user_videos",
    "related",
    "search",
)

# 这几种是“我的”数据，没凭证就明确失败，不能拿匿名推荐顶上。
_需要登录 = frozenset({"following", "toview", "favorites"})

_来源名 = {
    "recommend": "首页推荐",
    "following": "关注动态",
    "toview": "稍后再看",
    "favorites": "收藏夹",
    "user_videos": "UP 投稿",
    "related": "相关推荐",
    "search": "搜索",
}

_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_视频链接_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10})(?:[/?#]|$)")
_最大递归深度 = 6


class _来源错误(Exception):
    """参数/前置条件不满足；消息可以直接作为 reason 返回。"""


def _fail(source: str, reason: str) -> dict:
    return {"ok": False, "source": source, "items": [], "reason": str(reason)}


def _ok(source: str, items: list[dict]) -> dict:
    return {
        "ok": True,
        "source": source,
        "items": items,
        "reason": "" if items else "没有返回可用的视频",
    }


def _limit(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 12
    return max(1, min(50, n))


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _bvid(value) -> str:
    """只接受真正的 BV 号，或明确的 /video/BV... 链接。其它 ID 一律不认。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if _BVID_RE.fullmatch(text):
        return text
    match = _视频链接_RE.search(text)
    return match.group(1) if match else ""


def _int(value) -> int:
    if isinstance(value, bool):
        return 0
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    try:
        if text.endswith("万"):
            return max(0, int(float(text[:-1]) * 10000))
        if text.endswith("亿"):
            return max(0, int(float(text[:-1]) * 100000000))
        return max(0, int(float(text)))
    except (TypeError, ValueError):
        return 0


def _duration(value) -> int:
    """统一成秒。接口里可能是 123、"12:03" 或 "03:21"。"""
    text = _text(value)
    if not text:
        return 0
    if ":" in text:
        try:
            total = 0
            for part in text.split(":"):
                total = total * 60 + int(part)
            return max(0, total)
        except (TypeError, ValueError):
            return 0
    return _int(text)


def _title(node: dict) -> str:
    text = _text(node.get("title") or node.get("archive_title"))
    if not text:
        return ""
    # 搜索接口有时把关键词包在 <em> 里；只清这一种标记，别把标题里的
    # 正常尖括号内容一起吃掉。
    text = re.sub(r"</?em\b[^>]*>", "", text)
    return html.unescape(text).strip()


def _author(node: dict) -> str:
    """从视频节点或其明确的上层作者容器里取 UP 名。"""
    if not isinstance(node, dict):
        return ""
    for key in ("author", "up_name", "uname"):
        name = _text(node.get(key))
        if name:
            return name
    for key in ("owner", "upper", "module_author"):
        sub = node.get(key)
        if isinstance(sub, dict):
            name = _text(sub.get("name") or sub.get("uname"))
            if name:
                return name
    modules = node.get("modules")
    if isinstance(modules, dict):
        author = modules.get("module_author")
        if isinstance(author, dict):
            name = _text(author.get("name") or author.get("uname"))
            if name:
                return name
    return ""


def _play(node: dict) -> int:
    if not isinstance(node, dict):
        return 0
    for key in ("play", "view", "view_count", "vv"):
        value = _int(node.get(key))
        if value:
            return value
    for key in ("stat", "cnt_info"):
        sub = node.get(key)
        if isinstance(sub, dict):
            for field in ("view", "play", "vv", "view_count"):
                value = _int(sub.get(field))
                if value:
                    return value
    return 0


def _desc(node: dict) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("desc", "description", "intro", "summary"):
        text = _text(node.get(key))
        if text:
            return text
    return ""


def _video_from_url(value) -> str:
    text = _text(value)
    if not text:
        return ""
    return _bvid(text)


def _walk_video_nodes(node, *, depth: int = 0, context: tuple = ()):
    """有界递归找“明确带 bvid 的视频节点”。

    ``context`` 是祖先字典，用来补动态卡片里 ``module_author.name`` 这类
    外层作者信息；不会拿祖先里的任意数字 ID 当 bvid。
    """
    if depth > _最大递归深度:
        return
    if isinstance(node, dict):
        bvid = _bvid(node.get("bvid")) or _bvid(node.get("bv_id"))
        if not bvid:
            for key in ("uri", "arcurl", "jump_url", "url"):
                bvid = _video_from_url(node.get(key))
                if bvid:
                    break
        if bvid:
            yield node, context
        for value in node.values():
            if isinstance(value, (dict, list, tuple)):
                yield from _walk_video_nodes(
                    value, depth=depth + 1, context=context + (node,))
    elif isinstance(node, (list, tuple)):
        for value in node:
            if isinstance(value, (dict, list, tuple)):
                yield from _walk_video_nodes(
                    value, depth=depth + 1, context=context)


def _normalize(node: dict, context: tuple, source: str) -> dict | None:
    bvid = _bvid(node.get("bvid")) or _bvid(node.get("bv_id"))
    if not bvid:
        for key in ("uri", "arcurl", "jump_url", "url"):
            bvid = _video_from_url(node.get(key))
            if bvid:
                break
    if not bvid:
        return None

    author = _author(node)
    if not author:
        for parent in reversed(context):
            author = _author(parent)
            if author:
                break

    play = _play(node)
    if not play:
        for parent in reversed(context):
            play = _play(parent)
            if play:
                break

    title = _title(node)
    desc = _desc(node)
    duration = _duration(
        node.get("duration") or node.get("duration_text")
        or node.get("length"))
    return {
        "bvid": bvid,
        "title": title,
        "author": author,
        "play": play,
        "duration": duration,
        "desc": desc,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "source": source,
    }


def _collect(raw, source: str, limit: int) -> list[dict]:
    """从任意层级的平台回包里按顺序挑出视频，去重后截断到 limit。"""
    out: list[dict] = []
    seen: set[str] = set()
    for node, context in _walk_video_nodes(raw):
        item = _normalize(node, context, source)
        if item is None or item["bvid"] in seen:
            continue
        seen.add(item["bvid"])
        out.append(item)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 凭证 / UID
def _credential(bot):
    """bot 自己的凭证入口；只在内存里构造 Credential，绝不打印任何字段值。"""
    data = bili_credentials.load(bot) or {}
    if not isinstance(data, dict) or not _text(data.get("sessdata")):
        return None
    from bilibili_api import Credential

    return Credential(
        sessdata=_text(data.get("sessdata")),
        bili_jct=_text(data.get("bili_jct")),
        buvid3=_text(data.get("buvid3")),
        dedeuserid=_text(data.get("dedeuserid")),
    )


def _config_uid(bot) -> int:
    for key in ("tools.bili_uid", "bili.uid", "bili.user_uid",
                "tools.bili_user_uid", "tools.bili_scroll_uid"):
        try:
            raw = bot.config.get(key, "")
        except Exception:
            raw = ""
        uid = _int(raw)
        if uid > 0:
            return uid
    return 0


def _uid(bot, cred) -> int:
    uid = _config_uid(bot)
    if uid > 0:
        return uid
    try:
        return _int(getattr(cred, "dedeuserid", 0))
    except Exception:
        return 0


# ---------------------------------------------------------------- 各来源
async def _search_raw(query: str, num: int):
    """vendored 搜索的原始返回（通常是 JSON 字符串）。"""
    from .bili_mcp import mcp_server as srv

    return await srv.bili_search(keyword=query, num=int(num), order="totalrank")


async def _favorite_media_id(bot, cred, uid: int) -> int:
    from bilibili_api import favorite_list

    data = await favorite_list.get_video_favorite_list(uid=uid, credential=cred)
    rows = data.get("list") if isinstance(data, dict) else data
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        media_id = _int(row.get("id") or row.get("fid"))
        if media_id > 0:
            return media_id
    return 0


async def _fetch(bot, source: str, *, query: str, uid: int, media_id: int,
                 bvid: str, limit: int, cred):
    if source == "recommend":
        from bilibili_api import homepage

        return await homepage.get_videos(credential=cred)

    if source == "following":
        from bilibili_api import dynamic

        return await dynamic.get_dynamic_page_info(
            credential=cred, _type=dynamic.DynamicType.VIDEO, pn=1)

    if source == "toview":
        from bilibili_api import user

        return await user.get_toview_list(credential=cred)

    if source == "favorites":
        from bilibili_api import favorite_list

        if media_id <= 0:
            owner = _uid(bot, cred)
            if owner <= 0:
                raise _来源错误("收藏夹需要 uid：plan 没指定，配置和凭证里也没有")
            media_id = await _favorite_media_id(bot, cred, owner)
            if media_id <= 0:
                raise _来源错误("现有收藏列表里没有可用的收藏夹")
        return await favorite_list.get_video_favorite_list_content(
            media_id=int(media_id), page=1, credential=cred)

    if source == "user_videos":
        from bilibili_api import user

        target = _int(uid) or _uid(bot, cred)
        if target <= 0:
            raise _来源错误("UP 投稿需要 uid：plan 没指定，配置和凭证里也没有")
        client = user.User(uid=target, credential=cred)
        return await client.get_videos(
            pn=1, ps=max(1, min(50, int(limit))))

    if source == "related":
        from bilibili_api import video

        target = _bvid(bvid)
        if not target:
            raise _来源错误("相关推荐需要有效的 bvid")
        client = video.Video(bvid=target, credential=cred)
        return await client.get_related()

    if source == "search":
        keyword = _text(query)
        if not keyword:
            raise _来源错误("搜索需要关键词 query")
        raw = await _search_raw(keyword, limit)
        if isinstance(raw, (dict, list)):
            return raw
        return json.loads(raw)

    raise _来源错误(f"不支持的来源：{source}")


# ---------------------------------------------------------------- 对外
async def discover(bot, source, *, query: str = "", uid: int = 0,
                   media_id: int = 0, bvid: str = "",
                   limit: int = 12) -> dict:
    """按来源取一批视频并归一。

    返回 ``{ok, source, items, reason}``，永不抛（取消除外）。``ok=False`` 时
    ``reason`` 说明是没凭证、参数不满足还是接口失败；上层可以据此**显式**
    回退，不要拿别的来源冒充当前来源。
    """
    name = _text(source).lower()
    if name not in SOURCES:
        return _fail(name, f"不支持的来源：{source or '（空）'}")
    count = _limit(limit)
    label = _来源名.get(name, name)

    try:
        cred = _credential(bot)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warn(f"B站发现初始化凭证失败：{type(e).__name__}")
        return _fail(name, f"B站接口初始化失败：{type(e).__name__}")

    if name in _需要登录 and cred is None:
        return _fail(name, f"{label}需要登录 B 站：没有可用凭据")

    if name == "favorites" and cred is None:
        # 显式 media_id 理论上可匿名读，但“用哪个收藏夹”属于账号数据；
        # 统一要求登录，别悄悄读成别人的收藏夹。
        return _fail(name, f"{label}需要登录 B 站：没有可用凭据")

    try:
        raw = await _fetch(
            bot, name, query=query, uid=_int(uid),
            media_id=_int(media_id), bvid=_text(bvid), limit=count,
            cred=cred)
    except asyncio.CancelledError:
        raise
    except _来源错误 as e:
        return _fail(name, str(e))
    except Exception as e:  # noqa: BLE001
        log.warn(f"B站发现失败（{name}）：{type(e).__name__}")
        return _fail(name, f"{label}读取失败：{type(e).__name__}: {str(e)[:160]}")

    return _ok(name, _collect(raw, name, count))
