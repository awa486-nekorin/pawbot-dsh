"""B 站「互动」层：发弹幕、评论视频、回复别人的评论、看收到的回复、
删自己的评论（含批量）、点赞 / 收藏 / 投币 / 按判断自动互动。

凭证复用 bili_mcp 的 bili_credential.json（bili_scroll 会把它同步好，
所以这里只读不改）。所有函数失败都往上抛，调用方自己兜。

写函数（send_danmaku / comment_video / reply_comment / like_video /
coin_video / favorite_video / delete_comment / delete_items / delete_recent /
delete_by_index / delete_by_text / delete_on_video / react）**只能由
``bili_scroll.action()`` 在 ``bili_gate`` 放行之后调用**；工具和插件都不要绕过
``action()``，否则会绕过进程内凭证、审计与额度。唯一例外是 ``react()``：
按云端原版语义，``bili_scroll`` 刷完一轮后直接调它（开关是
``tools.bili_scroll_react_*``、互动账本按视频去重、每日额度 0=不限）。

语义基准是云端线上版本（2026-11-03）：账本 ``bili_my_comments.json`` /
``bili_my_actions.json`` 记在她自己的数据根下，删评论按「最近 / 序号 / 文本 /
整条视频」都能定位；每条目标在删除前由 ``bili_scroll`` 核实作者就是凭证账户本人。

批量删除只删**账本里记着我发出去的**或**评论区里 mid 等于凭证账户**的评论；
本模块不会去猜别人的 rpid。本模块下部的 verify_* / *_receipt / comment_state /
comment_context 只做只读核验，不会发任何东西；``comment_context`` 每次调用都
现取完整评论树（顶层+楼中楼），**不缓存到磁盘**。
"""

from __future__ import annotations

import json
import os
import re
import time

from . import paths as layout
from .log import log

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def _cred():
    from .bili_mcp.mcp_server import load_credential
    cred = load_credential()
    if cred is None:
        raise RuntimeError("B 站没登录（bili_credential.json 不存在）")
    return cred


async def send_danmaku(bvid: str, text: str, progress_sec: float = 5.0) -> dict:
    """在视频里发一条弹幕（progress_sec：从第几秒飘出来）。"""
    from bilibili_api import video
    from bilibili_api.video import Danmaku

    cred = _cred()
    v = video.Video(bvid=bvid, credential=cred)
    dm = Danmaku(text=str(text)[:60], dm_time=float(max(0.0, progress_sec)))
    out = await v.send_danmaku(page_index=0, danmaku=dm)
    log.info(f"发弹幕 {bvid} @{int(progress_sec)}s：{text[:30]}")
    return out


async def comment_video(bvid: str, text: str, bot=None) -> dict:
    """在视频下面发一条评论（发成功就记进账本，方便之后删）。"""
    from bilibili_api import comment, video

    cred = _cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    aid = int(info["aid"])
    out = await comment.send_comment(
        text=text, oid=aid,
        type_=comment.CommentResourceType.VIDEO, credential=cred)
    record_my_comment(bot, "comment", aid, _rpid_of(out), text, bvid=bvid)
    log.info(f"评论视频 {bvid}：{text[:30]}")
    return out


async def reply_comment(oid: int, text: str, root: int = 0, parent: int = 0,
                        bot=None, bvid: str = "") -> dict:
    """回复某条评论（root=楼层根评论；楼中楼再给 parent）。发成功记进账本。

    发布前先看**同一个视频今天回复了几条**（``tools.bili_auto_reply_per_video_daily``，
    0=不限）：用完了就抛错、一条都不发；成功发布后记一笔到 ``reply_daily``。
    """
    from bilibili_api import comment

    cred = _cred()
    键 = _reply_key(bvid, oid)
    if bot is not None and reply_left_today(bot, 键) <= 0:
        raise RuntimeError("同一个视频一天最多回复 %d 条，这个视频（%s）今天的额度用完了"
                           % (_reply_cap(bot), 键))
    out = await comment.send_comment(
        text=text, oid=int(oid),
        type_=comment.CommentResourceType.VIDEO,
        root=int(root) or None, parent=int(parent) or None,
        credential=cred)
    _bump_reply(bot, 键)
    record_my_comment(bot, "reply", oid, _rpid_of(out), text, bvid=bvid)
    log.info(f"回复评论 oid={oid} root={root}：{text[:30]}")
    return out


async def received_replies(limit: int = 20) -> list[dict]:
    """最近收到的评论回复通知，整理成好用的结构。"""
    from bilibili_api import session

    cred = _cred()
    data = await session.get_replies(credential=cred)
    out: list[dict] = []
    for it in (data.get("items") or [])[:max(1, int(limit))]:
        item = it.get("item") or {}
        user = it.get("user") or {}
        m = _BVID_RE.search(str(item.get("uri") or ""))
        out.append({
            "notify_id": str(it.get("id") or ""),
            "mid": user.get("mid"),
            "nickname": str(user.get("nickname") or ""),
            "bvid": m.group(0) if m else "",
            "aid": item.get("subject_id"),
            "root_id": item.get("root_id"),
            "source_id": item.get("source_id"),
            "text": str(item.get("root_reply_content") or item.get("title") or ""),
            "source_text": str(item.get("source_content") or ""),
            "reply_time": it.get("reply_time"),
        })
    return out


async def aid_of(bvid: str) -> int:
    """视频的 aid（评论接口用 oid）。"""
    from bilibili_api import video

    cred = _cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    return int(info["aid"])


# ---------------------------------------------------------------- 实时评论树（只读）
def _comment_time(ctime) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ctime)))
    except (OverflowError, OSError, TypeError, ValueError):
        return ""


def _comment_row(row, my_mid: str) -> dict:
    """把 B 站评论对象收窄成生成上下文要用的字段（作者/赞/时间/是否我发的）。"""
    row = row if isinstance(row, dict) else {}
    member = row.get("member") or {}
    content = row.get("content") or {}
    try:
        ctime = int(row.get("ctime") or 0)
    except (TypeError, ValueError):
        ctime = 0
    try:
        like = int(row.get("like") or 0)
    except (TypeError, ValueError):
        like = 0
    return {
        "rpid": int(row.get("rpid") or 0),
        "user": str(member.get("uname") or ""),
        "mid": str(member.get("mid") or ""),
        "like": like,
        "ctime": ctime,
        "time": _comment_time(ctime),
        "content": str(content.get("message") or ""),
        "mine": bool(my_mid) and str(member.get("mid") or "") == my_mid,
    }


async def comment_context(bvid: str, *, pages: int = 1, sub_page_size: int = 20,
                          sub_pages: int = 2) -> dict:
    """**实时**抓一个视频的完整评论树（顶层 + 楼中楼），只读。

    每次调用都重新请求 B 站，**不缓存到磁盘**（评论原文也不写任何本地文件）；
    调用方拿到后只在本次生成的提示词里用。

    返回::

        {"bvid", "aid", "fetched_at", "fetched_at_text",
         "threads": [{"rpid", "user", "mid", "like", "ctime", "time",
                      "content", "mine", "replied", "rcount",
                      "replies": [{"rpid", "user", "mid", "like", "ctime",
                                   "time", "content", "mine"}],
                      "replies_error"? }],
         "total_threads", "total_replies", "replied_threads"}

    ``replied`` = 这条楼里已经出现**凭证账户本人**的回复（拿不到凭证时一律
    False）。失败往上抛，由调用方兜（不在这里假装抓到）。
    """
    from bilibili_api import comment

    bvid = str(bvid or "").strip()
    cred = _cred()
    my_mid = str(getattr(cred, "dedeuserid", "") or "")
    aid = await _aid_of(bvid)
    if not aid:
        raise RuntimeError(f"拿不到 {bvid} 的 aid，读不了评论")
    kind = comment.CommentResourceType.VIDEO
    try:
        page_count = max(1, min(3, int(pages)))
    except (TypeError, ValueError):
        page_count = 1
    try:
        sub_size = max(1, min(50, int(sub_page_size)))
    except (TypeError, ValueError):
        sub_size = 20
    try:
        sub_page_count = max(1, min(3, int(sub_pages)))
    except (TypeError, ValueError):
        sub_page_count = 2

    threads: list[dict] = []
    for page in range(1, page_count + 1):
        data = await comment.get_comments(
            oid=int(aid), type_=kind, page_index=page, credential=cred)
        rows = (data or {}).get("replies") or []
        if not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            thread = _comment_row(row, my_mid)
            if not thread["content"]:
                continue
            try:
                thread["rcount"] = int(row.get("rcount") or 0)
            except (TypeError, ValueError):
                thread["rcount"] = 0
            replies: list[dict] = []
            seen: set[int] = set()

            def _add(reply_row) -> None:
                got = _comment_row(reply_row, my_mid)
                if not got["rpid"] or got["rpid"] in seen or not got["content"]:
                    return
                seen.add(got["rpid"])
                replies.append(got)

            # 评论列表有时自带几条楼中楼预览：先用，后面再补齐。
            for preview in row.get("replies") or []:
                _add(preview)
            if thread["rpid"] and thread["rcount"] > len(replies):
                for sub_page in range(1, sub_page_count + 1):
                    try:
                        c = comment.Comment(oid=int(aid), type_=kind,
                                            rpid=thread["rpid"], credential=cred)
                        sub = await c.get_sub_comments(
                            page_index=sub_page, page_size=sub_size)
                    except Exception as e:  # noqa: BLE001
                        thread["replies_error"] = f"{type(e).__name__}"
                        break
                    got = (sub or {}).get("replies") or []
                    if not got:
                        break
                    for reply_row in got:
                        _add(reply_row)
                    if len(got) < sub_size or len(replies) >= thread["rcount"]:
                        break
            thread["replies"] = replies
            thread["replied"] = any(reply.get("mine") for reply in replies)
            threads.append(thread)
        if len(rows) < 20:
            break

    return {
        "bvid": bvid,
        "aid": int(aid),
        "fetched_at": int(time.time()),
        "fetched_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "threads": threads,
        "total_threads": len(threads),
        "total_replies": sum(len(item.get("replies") or []) for item in threads),
        "replied_threads": sum(1 for item in threads if item.get("replied")),
    }


# ---------------------------------------------------------------- 评论账本 / 删除
LEDGER_FILE = "bili_my_comments.json"


def _data_dir(bot) -> str:
    """账本落点：优先本地数据布局的数据根（content/state 之外），其次 persona 目录。

    这是本地架构（``pawbot.paths``）与云端原实现（``persona_path`` 同目录）的
    唯一差异：只影响文件放在哪，不影响账本格式与语义。
    """
    try:
        root = str(getattr(layout.for_bot(bot), "root", "") or "")
    except Exception:
        root = ""
    if root:
        return root
    try:
        return os.path.dirname(bot.persona.persona_path)
    except Exception:
        return os.path.join(os.getcwd(), "data")


def _ledger_path(bot) -> str:
    return os.path.join(_data_dir(bot), LEDGER_FILE)


def _load_ledger(bot) -> dict:
    try:
        with open(_ledger_path(bot), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": []}


def _save_ledger(bot, d: dict) -> None:
    path = _ledger_path(bot)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log.warn(f"评论账本写入失败：{e}")


def _rpid_of(out) -> int:
    """从 send_comment 的返回里挖出 rpid（层数不确定，递归找）。"""
    if isinstance(out, dict):
        for k in ("rpid", "rpid_str"):
            v = out.get(k)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        for k in ("data", "result"):
            v = out.get(k)
            if isinstance(v, dict):
                got = _rpid_of(v)
                if got:
                    return got
    return 0


def record_my_comment(bot, kind: str, oid: int, rpid: int, text: str,
                      bvid: str = "") -> None:
    """把「我发出去的评论」记进账本，删除时按账本找。"""
    if bot is None or not rpid:
        return
    try:
        d = _load_ledger(bot)
        d["items"].append({
            "ts": int(time.time()), "kind": str(kind or "comment"),
            "oid": int(oid), "rpid": int(rpid), "bvid": str(bvid or ""),
            "text": str(text or "")[:200], "deleted": False,
        })
        d["items"] = d["items"][-800:]
        _save_ledger(bot, d)
    except Exception as e:  # noqa: BLE001
        log.debug(f"记录评论失败：{type(e).__name__}")


def my_comments(bot, limit: int = 10, include_deleted: bool = False) -> list[dict]:
    """账本里我发过、还没删的评论（新的在前）。"""
    items = [x for x in _load_ledger(bot)["items"]
             if include_deleted or not x.get("deleted")]
    items.sort(key=lambda x: -int(x.get("ts") or 0))
    return items[:max(1, int(limit))]


async def delete_comment(oid: int, rpid: int) -> dict:
    """删掉自己发的一条评论（要 SESSDATA + bili_jct）。"""
    from bilibili_api import comment

    cred = _cred()
    c = comment.Comment(oid=int(oid), type_=comment.CommentResourceType.VIDEO,
                        rpid=int(rpid), credential=cred)
    out = await c.delete()
    log.info(f"删除评论 rpid={rpid}（oid={oid}）")
    return out if isinstance(out, dict) else {"raw": out}


async def delete_items(bot, items: list[dict]) -> dict:
    """按 rpid 逐条删；删成功的在账本里标记 deleted。"""
    done, failed = [], []
    for it in items or []:
        try:
            await delete_comment(int(it["oid"]), int(it["rpid"]))
        except Exception as e:  # noqa: BLE001
            failed.append({"rpid": it.get("rpid"),
                           "text": str(it.get("text") or "")[:40],
                           "error": f"{type(e).__name__}: {e}"})
            continue
        done.append(it)
    if done and bot is not None:
        d = _load_ledger(bot)
        ids = {str(x.get("rpid")) for x in done}
        for x in d["items"]:
            if str(x.get("rpid")) in ids:
                x["deleted"] = True
                x["deleted_ts"] = int(time.time())
        _save_ledger(bot, d)
    return {"ok": bool(done), "deleted": done, "failed": failed,
            "reason": "" if done else "一条都没删掉"}


async def delete_recent(bot, n: int = 1) -> dict:
    items = my_comments(bot, limit=max(1, int(n)))
    if not items:
        return {"ok": False, "deleted": [], "failed": [],
                "reason": "账本里没有我发过的评论（旧的评论让我按 BV 号扫一遍再删）"}
    return await delete_items(bot, items)


async def delete_by_index(bot, i: int) -> dict:
    items = my_comments(bot, limit=200)
    if not (1 <= int(i) <= len(items)):
        return {"ok": False, "deleted": [], "failed": [],
                "reason": f"只有 {len(items)} 条，没有第 {i} 条"}
    return await delete_items(bot, [items[int(i) - 1]])


async def delete_by_text(bot, text: str) -> dict:
    t = str(text or "").strip()
    items = [x for x in my_comments(bot, limit=300)
             if t and t in str(x.get("text") or "")]
    if not items:
        return {"ok": False, "deleted": [], "failed": [],
                "reason": f"没找到含「{t}」的评论"}
    return await delete_items(bot, items)


async def scan_my_comments(bvid: str, limit: int = 50) -> list[dict]:
    """只读：直接从视频评论区里找**凭证账户自己**发的评论。

    账本没记到的老评论用这个兜底；只挑 ``member.mid == 凭证账户`` 的行，
    不删、不写账本。
    """
    from bilibili_api import comment, video

    cred = _cred()
    mid = str(getattr(cred, "dedeuserid", "") or "")
    if not mid:
        raise RuntimeError("凭证里没有 dedeuserid，扫不了自己的评论")
    info = await video.Video(bvid=bvid, credential=cred).get_info()
    oid = int(info["aid"])
    rows: list[dict] = []
    page = 1
    while len(rows) < limit and page <= 6:
        data = await comment.get_comments(
            oid=oid, type_=comment.CommentResourceType.VIDEO,
            page_index=page, credential=cred)
        replies = (data or {}).get("replies") or []
        if not replies:
            break
        for r in replies:
            m = r.get("member") or {}
            if str(m.get("mid")) != mid:
                continue
            rows.append({
                "ts": int(r.get("ctime") or 0), "kind": "comment",
                "oid": oid, "rpid": int(r.get("rpid") or 0), "bvid": bvid,
                "text": str((r.get("content") or {}).get("message") or "")[:200],
                "deleted": False,
            })
        page += 1
    return rows[:limit]


async def delete_on_video(bot, bvid: str) -> dict:
    """删掉我在某个视频底下发过的全部评论：先查账本，账本没有就去扫评论区。"""
    bvid = str(bvid or "").strip()
    items = [x for x in my_comments(bot, limit=300)
             if str(x.get("bvid")) == bvid]
    if not items:
        try:
            items = await scan_my_comments(bvid, 50)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "deleted": [], "failed": [],
                    "reason": f"扫评论区失败：{type(e).__name__}: {e}"}
    if not items:
        return {"ok": False, "deleted": [], "failed": [],
                "reason": f"{bvid} 底下没找到我发的评论"}
    return await delete_items(bot, items)


# ---------------------------------------------------------------- 点赞 / 收藏 / 投币
ACTION_FILE = "bili_my_actions.json"
FAV_FOLDER_TITLE = "大肥鱼觉得好的"


def _actions_path(bot) -> str:
    return os.path.join(_data_dir(bot), ACTION_FILE)


def _load_actions(bot) -> dict:
    try:
        with open(_actions_path(bot), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in ("liked", "faved", "coined", "daily", "reply_daily"):
                d.setdefault(k, {})
            return d
    except Exception:
        pass
    return {"liked": {}, "faved": {}, "coined": {}, "daily": {}, "reply_daily": {}}


def _save_actions(bot, d: dict) -> None:
    path = _actions_path(bot)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log.warn(f"互动账本写入失败：{e}")


async def coin_balance() -> int:
    """她自己还有多少硬币。"""
    from bilibili_api import user

    return int(await user.get_self_coins(credential=_cred()))


async def like_video(bvid: str) -> dict:
    """给视频点个赞。

    回执**不当唯一证据**：调用方可以再 ``verify_like()`` 读后核验（现在是附加
    信息，不把「后端成功」降级成未确认）。
    """
    from bilibili_api import video

    out = await video.Video(bvid=str(bvid), credential=_cred()).like(True)
    log.info(f"点赞 {bvid}")
    return out if isinstance(out, dict) else {"raw": out}


async def coin_video(bvid: str, num: int = 1) -> dict:
    """投 1 或 2 个硬币。

    显式 ``like=False``：投币的授权**不包含点赞**，不顺手改动账号的点赞状态。
    """
    from bilibili_api import video

    n = 2 if int(num) >= 2 else 1
    out = await video.Video(bvid=str(bvid), credential=_cred()).pay_coin(
        num=n, like=False)
    log.info(f"投币 {bvid} ×{n}")
    return out if isinstance(out, dict) else {"raw": out}


async def _fav_folder_id(bot=None, *, create: bool = False) -> int:
    """找「大肥鱼觉得好的」收藏夹；``create=False`` 时绝不新建（核验路径用）。

    云端原实现把找到的 folder_id 缓存进互动账本；有 bot 时同样复用缓存，
    没有 bot（只读核验）就现查，绝不新建。
    """
    from bilibili_api import favorite_list

    acts = _load_actions(bot) if bot is not None else None
    fid = int((acts or {}).get("folder_id") or 0)
    cred = _cred()
    mid = int(getattr(cred, "dedeuserid", 0) or 0)

    async def _find() -> int:
        data = await favorite_list.get_video_favorite_list(mid, credential=cred)
        for it in (data or {}).get("list") or []:
            if str(it.get("title") or "") == FAV_FOLDER_TITLE:
                return int(it.get("id") or 0)
        return 0

    if not fid:
        fid = await _find()
    if fid or not create:
        return fid
    made = await favorite_list.create_video_favorite_list(
        title=FAV_FOLDER_TITLE,
        introduction="大肥鱼刷到觉得特别好、以后还想再看的视频",
        private=False, credential=cred)
    for key in ("id", "fid"):
        if isinstance(made, dict) and made.get(key):
            try:
                fid = int(made[key])
                break
            except (TypeError, ValueError):
                fid = 0
    if not fid:
        fid = await _find()
    if fid and acts is not None:
        acts["folder_id"] = fid
        _save_actions(bot, acts)
    return fid


async def favorite_video(bot, bvid: str) -> dict:
    """收藏到「大肥鱼觉得好的」；没有这个收藏夹才新建（同一次授权内）。"""
    from bilibili_api import video

    fid = await _fav_folder_id(bot, create=True)
    if not fid:
        raise RuntimeError("没找到也建不了收藏夹")
    out = await video.Video(bvid=str(bvid), credential=_cred()).set_favorite(
        add_media_ids=[fid])
    log.info(f"收藏 {bvid} → {FAV_FOLDER_TITLE}")
    return out if isinstance(out, dict) else {"raw": out}


async def react(bot, bvid: str, judge: dict) -> dict:
    """按她自己的判断决定点赞 / 收藏 / 投币。

    规则（云端原版语义）：每个视频只做一次（账本去重）、可配每日额度（0=不限）、
    投币前先看余额，不够就不投。永不抛。
    """
    out = {"like": False, "fav": False, "coin": 0, "errors": []}
    if bot is None or not bvid or not isinstance(judge, dict):
        return out
    want = {"like": bool(judge.get("like")), "fav": bool(judge.get("fav")),
            "coin": int(judge.get("coin") or 0)}
    if not (want["like"] or want["fav"] or want["coin"]):
        return out

    bvid = str(bvid)
    acts = _load_actions(bot)
    today = time.strftime("%Y-%m-%d")
    day = acts["daily"].setdefault(today, {"like": 0, "fav": 0, "coin": 0})

    def _on(kind: str) -> bool:
        return bool(bot.config.get(f"tools.bili_scroll_react_{kind}", True))

    def _left(kind: str) -> bool:
        cap = int(bot.config.get(f"tools.bili_scroll_react_{kind}_daily", 0) or 0)
        return cap <= 0 or int(day.get(kind) or 0) < cap

    if want["like"] and _on("like") and bvid not in acts["liked"] and _left("like"):
        try:
            await like_video(bvid)
            if await verify_like(bvid):
                acts["liked"][bvid] = int(time.time())
                day["like"] = int(day.get("like") or 0) + 1
                out["like"] = True
            else:
                out["errors"].append("点赞：B站那边没点上（复核未通过）")
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"点赞 {type(e).__name__}: {e}")

    if want["fav"] and _on("fav") and bvid not in acts["faved"] and _left("fav"):
        try:
            await favorite_video(bot, bvid)
            if await verify_favorite(bvid):
                acts["faved"][bvid] = int(time.time())
                day["fav"] = int(day.get("fav") or 0) + 1
                out["fav"] = True
            else:
                out["errors"].append("收藏：收藏夹里没见到（复核未通过）")
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"收藏 {type(e).__name__}: {e}")

    if want["coin"] > 0 and _on("coin") and bvid not in acts["coined"] and _left("coin"):
        n = 2 if want["coin"] >= 2 else 1
        try:
            bal = await coin_balance()
        except Exception:  # noqa: BLE001
            bal = 999
        if bal < n:
            out["errors"].append(f"硬币不够（余额 {bal}）")
        else:
            try:
                await coin_video(bvid, n)
                got = await verify_coin(bvid)
                if got is not None and got >= n:
                    acts["coined"][bvid] = {"num": got, "ts": int(time.time())}
                    day["coin"] = int(day.get("coin") or 0) + got
                    out["coin"] = got
                else:
                    out["errors"].append(
                        "投币：B站显示只投了 %s 个（复核未通过）" % got)
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"投币 {type(e).__name__}: {e}")

    _save_actions(bot, acts)
    if out["errors"]:
        log.warn("对 %s 的互动没全成：%s" % (bvid, "；".join(out["errors"])[:200]))
    return out


# ---------------------------------------------------------------- 回执提取 / 只读核验
# 这些只做「发布之后回头看一眼」，不新增任何写动作。回执语义与云端一致：
# 后端返回成功即算成功；核验结果只作附加信息，不把成功降级成「不确定」。
def _first_id(out, keys, depth: int = 0) -> int:
    """从接口返回里递归挖出第一个正整数 id（rpid / dmid）。找不到回 0。"""
    if depth > 4:
        return 0
    if isinstance(out, dict):
        for key in keys:
            value = out.get(key)
            if value:
                try:
                    got = int(value)
                except (TypeError, ValueError):
                    got = 0
                if got > 0:
                    return got
        for key in ("data", "result"):
            value = out.get(key)
            if isinstance(value, (dict, list)):
                got = _first_id(value, keys, depth + 1)
                if got:
                    return got
    elif isinstance(out, list):
        for item in out[:8]:
            got = _first_id(item, keys, depth + 1)
            if got:
                return got
    return 0


def comment_receipt(out) -> dict:
    """把 send_comment 的返回整理成 bili_gate 认的回执形状。

    云端语义：没拿到 rpid 就不算「评论已发出」——回执里不带可验证证据时，
    闸门会记未确认，而不是把一句 ok 当成发布成功。
    """
    rpid = _first_id(out, ("rpid", "rpid_str"))
    if rpid:
        return {"success": True, "rpid": rpid, "rpid_str": str(rpid)}
    return {"success": True}


def danmaku_receipt(out) -> dict:
    """把 send_danmaku 的返回整理成闸门认的回执（dmid 才算证据）。"""
    dmid = _first_id(out, ("dmid", "dmid_str", "dm_id", "dm_id_str"))
    if dmid:
        return {"success": True, "dmid": dmid, "dmid_str": str(dmid)}
    return {"success": True}


async def _aid_of(bvid: str) -> int:
    from bilibili_api import video

    info = await video.Video(bvid=str(bvid), credential=_cred()).get_info()
    return int((info or {}).get("aid") or 0)


# ---------------------------------------------------------------- 身份 / 我的评论（只读）
def my_mid() -> str:
    """凭证账户自己的 mid。空串 = 无法核对身份，删除必须拒绝。"""
    return str(getattr(_cred(), "dedeuserid", "") or "").strip()


async def comment_state(bvid: str, rpid) -> dict:
    """查一条顶层评论现在的状态（只读，用于删除前核实身份 + 删除后复核）。

    返回 ``{"state": "present"|"gone"|"unknown", "mid", "oid", "text", "reason"}``：
    - present：查到了，``mid`` 是评论作者的 mid；
    - gone：翻完（页数/条数用尽）确实没有这条；
    - unknown：拿不到 aid / 网络异常 / rpid 不合法——**不能据此说成功或失败**。
    """
    try:
        target = str(int(rpid))
        if not target or target == "0":
            return {"state": "unknown", "reason": "rpid 不合法"}
    except (TypeError, ValueError):
        return {"state": "unknown", "reason": "rpid 不合法"}
    try:
        from bilibili_api import comment

        cred = _cred()
        aid = await _aid_of(bvid)
        if not aid:
            return {"state": "unknown", "reason": "拿不到 aid"}
        kind = comment.CommentResourceType.VIDEO
        for page in (1, 2, 3):
            data = await comment.get_comments(
                oid=aid, type_=kind, page_index=page, credential=cred)
            rows = (data or {}).get("replies") or []
            for row in rows:
                if str(row.get("rpid")) != target:
                    continue
                member = row.get("member") or {}
                return {
                    "state": "present",
                    "mid": str(member.get("mid") or ""),
                    "oid": aid,
                    "text": str((row.get("content") or {}).get("message") or ""),
                }
            if len(rows) < 20:
                break
        return {"state": "gone", "oid": aid}
    except Exception as e:  # noqa: BLE001
        log.debug(f"查评论状态失败 {bvid}/{rpid}：{type(e).__name__}")
        return {"state": "unknown", "reason": f"{type(e).__name__}: {e}"}


async def verify_comment(bvid: str, rpid, root: int = 0) -> bool:
    """去评论区里找这条评论到底在不在。root=0 查顶层，root!=0 查楼中楼。

    任何异常 / 拿不到 aid / 没翻到都回 False（调用方按未确认处理）。
    """
    try:
        target = str(int(rpid))
        if not target or target == "0":
            return False
        from bilibili_api import comment

        cred = _cred()
        aid = await _aid_of(bvid)
        if not aid:
            return False
        kind = comment.CommentResourceType.VIDEO
        if int(root or 0) > 0:
            c = comment.Comment(oid=aid, type_=kind, rpid=int(root), credential=cred)
            data = await c.get_sub_comments(page_index=1, page_size=50)
            rows = (data or {}).get("replies") or []
            if any(str(r.get("rpid")) == target for r in rows):
                return True
        # 顶层评论可能被新评论挤到后面：最多翻 3 页，找不到就老实说没确认。
        for page in (1, 2, 3):
            data = await comment.get_comments(
                oid=aid, type_=kind, page_index=page, credential=cred)
            rows = (data or {}).get("replies") or []
            if any(str(r.get("rpid")) == target for r in rows):
                return True
            if len(rows) < 20:
                break
        return False
    except Exception as e:  # noqa: BLE001
        log.debug(f"复核评论失败 {bvid}/{rpid}：{type(e).__name__}")
        return False


def _norm_text(text) -> str:
    return re.sub(r"\s+", "", str(text or ""))


async def verify_danmaku(bvid: str, text: str) -> bool:
    """去视频弹幕列表里找这条弹幕。找不到 / 出错都回 False（按未确认处理）。"""
    try:
        target = _norm_text(text)
        if not target:
            return False
        from bilibili_api import video

        rows = await video.Video(bvid=str(bvid), credential=_cred()).get_danmakus(
            page_index=0)
        return any(_norm_text(getattr(d, "text", "")) == target
                   for d in (rows or [])[:1000])
    except Exception as e:  # noqa: BLE001
        log.debug(f"复核弹幕失败 {bvid}：{type(e).__name__}")
        return False


# ---------------------------------------------------------------- 点赞/收藏/投币 读后核验
# 这三类动作的接口回执不可信（云端原实现就是「接口说 ok，实际没点上」）：
# 真的去查一遍账号状态才叫 verified。现在它只是**附加信息**：后端返回成功
# 就算成功，核验结果不再把成功降级（2026-11-03 云端等价语义）。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _cookie_header() -> str:
    cred = _cred()
    return "SESSDATA=%s; bili_jct=%s; buvid3=%s; DedeUserID=%s" % (
        cred.sessdata, cred.bili_jct, getattr(cred, "buvid3", ""), cred.dedeuserid)


def _http_json(url: str) -> dict:
    """只读 GET（带上凭证 cookie）。失败往上抛，调用方按未确认处理。"""
    import urllib.request

    req = urllib.request.Request(url, headers={
        "Cookie": _cookie_header(), "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


async def verify_like(bvid: str) -> bool:
    """去 B 站查这条到底点上赞没有（不信接口的一句 ok）。"""
    try:
        aid = await _aid_of(bvid)
        if not aid:
            return False
        d = _http_json(
            "https://api.bilibili.com/x/web-interface/archive/has/like?aid=%s" % aid)
        return int(d.get("data") or 0) == 1
    except Exception as e:  # noqa: BLE001
        log.debug(f"复核点赞失败 {bvid}：{type(e).__name__}")
        return False


async def verify_coin(bvid: str) -> int | None:
    """这条上实际投了几个币；**查不到回 None**（不能当成 0 或成功）。"""
    try:
        aid = await _aid_of(bvid)
        if not aid:
            return None
        d = _http_json(
            "https://api.bilibili.com/x/web-interface/archive/coins?aid=%s" % aid)
        return int(((d or {}).get("data") or {}).get("multiply") or 0)
    except Exception as e:  # noqa: BLE001
        log.debug(f"复核投币失败 {bvid}：{type(e).__name__}")
        return None


async def verify_favorite(bvid: str) -> bool:
    """收藏夹里到底有没有这条（B 站收藏接口回的 success_num 不可信）。

    只用 ``create=False`` 查：核验路径绝不新建收藏夹（那是另一次写）。
    """
    try:
        from bilibili_api import favorite_list

        fid = await _fav_folder_id(None, create=False)
        if not fid:
            return False
        data = await favorite_list.FavoriteList(
            media_id=fid, credential=_cred()).get_content()
        return any(str(m.get("bvid")) == str(bvid)
                   for m in ((data or {}).get("medias") or []))
    except Exception as e:  # noqa: BLE001
        log.debug(f"复核收藏失败 {bvid}：{type(e).__name__}")
        return False


# ---------------------------------------------------------------- 每视频每天回复上限
# 2026-11-03 生产热修（replycap）移植：同一个视频一天最多回复
# ``tools.bili_auto_reply_per_video_daily`` 条（默认 5，0=不限），按自然日计，
# 记在互动账本的 ``reply_daily`` 桶里：{日期: {视频键: 已回条数}}，只留最近 7 天。
# 视频键 = bvid（拿不到 bvid 时退回 ``oid:<aid>``），所以同一视频的不同评论楼层
# 共用一份额度；不同视频互不影响。
def _reply_cap(bot) -> int:
    """一个视频一天能回几条（0 或负数 = 不限）。配置读不到按默认 5。"""
    try:
        return int(bot.config.get("tools.bili_auto_reply_per_video_daily", 5) or 0)
    except Exception:
        return 5


def _reply_key(bvid: str = "", oid: int = 0) -> str:
    """回复额度的计键：优先 bvid，没有 bvid 才退回 oid。"""
    return str(bvid or "").strip() or ("oid:%s" % int(oid or 0))


def reply_used_today(bot, key: str) -> int:
    """这个视频今天已经回复了几条。"""
    if bot is None:
        return 0
    acts = _load_actions(bot)
    return int(((acts.get("reply_daily") or {}).get(time.strftime("%Y-%m-%d")) or {}).get(str(key), 0))


def reply_left_today(bot, key: str) -> int:
    """这个视频今天还能回几条（cap<=0 表示不限，给一个足够大的数）。"""
    cap = _reply_cap(bot)
    if cap <= 0:
        return 10 ** 9
    return max(0, cap - reply_used_today(bot, key))


def _bump_reply(bot, key: str) -> None:
    """记一条「已经回复成功」。一次发布只能记一次（见 note_reply_sent）。"""
    if bot is None:
        return
    acts = _load_actions(bot)
    today = time.strftime("%Y-%m-%d")
    d = acts.setdefault("reply_daily", {})
    keep_after = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    for k in list(d):          # 只留最近 7 天
        if str(k) < keep_after:
            d.pop(k, None)
    d.setdefault(today, {})
    d[today][str(key)] = int(d[today].get(str(key), 0)) + 1
    _save_actions(bot, acts)


def note_reply_sent(bot, bvid: str = "", oid: int = 0) -> None:
    """一条回复**确认发出**后记一笔（本地适配点）。

    ``reply_comment()`` 自己发布、自己记；本地 ``bili_scroll.action("reply")``
    走的是 vendored ``bili_reply``，不经过 ``reply_comment()``，所以由调用方
    （``bili_auto``）在回执确认后调这里补记。**同一次发布只调其中一处**，
    否则会重复计数。
    """
    _bump_reply(bot, _reply_key(bvid, oid))
