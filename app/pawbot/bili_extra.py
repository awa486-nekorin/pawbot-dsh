"""B 站「互动」层：发弹幕、评论视频、回复别人的评论、看收到的回复。

凭证复用 bili_mcp 的 bili_credential.json（bili_scroll 会把它同步好，
所以这里只读不改）。所有函数失败都往上抛，调用方自己兜。
"""

from __future__ import annotations

import re

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


async def comment_video(bvid: str, text: str) -> dict:
    """在视频下面发一条评论。"""
    from bilibili_api import comment, video

    cred = _cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    out = await comment.send_comment(
        text=text, oid=info["aid"],
        type_=comment.CommentResourceType.VIDEO, credential=cred)
    log.info(f"评论视频 {bvid}：{text[:30]}")
    return out


async def reply_comment(oid: int, text: str, root: int = 0, parent: int = 0) -> dict:
    """回复某条评论（root=楼层根评论；楼中楼再给 parent）。"""
    from bilibili_api import comment

    cred = _cred()
    out = await comment.send_comment(
        text=text, oid=int(oid),
        type_=comment.CommentResourceType.VIDEO,
        root=int(root) or None, parent=int(parent) or None,
        credential=cred)
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
