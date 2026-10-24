"""
OneBot 11 消息段模型。

协议里一条消息就是一个「消息段数组」：
    [{"type":"at","data":{"qq":"123"}}, {"type":"text","data":{"text":" 你好"}}]

这里做三件事：
  1. 数组 ↔ CQ 码 互转
  2. 数组 → 人类可读纯文本（给 LLM 看、给关键词匹配用）
  3. 提供 At / Text / Image 这类小构造函数，写插件时不用手拼 dict
"""

from __future__ import annotations

import re
from typing import Any

CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^\]]*)?)\]")

# 这些段在「纯文本」里的占位符，方便 LLM 明白发生了什么
PLACEHOLDER = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "face": "[表情]",
    "forward": "[聊天记录]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
    "share": "[链接]",
    "location": "[位置]",
}


def Text(text: str) -> dict:
    return {"type": "text", "data": {"text": str(text)}}


def At(qq: int | str) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}


def Image(file: str) -> dict:
    return {"type": "image", "data": {"file": file}}


def Record(file: str) -> dict:
    """QQ 原生语音段。file 用 base64://<wav base64>，协议端自己转 silk。"""
    return {"type": "record", "data": {"file": file}}


def Reply(message_id: int | str) -> dict:
    return {"type": "reply", "data": {"id": str(message_id)}}


def Face(id: int | str) -> dict:
    return {"type": "face", "data": {"id": str(id)}}


def normalize(message: Any) -> list[dict]:
    """把 API 允许的三种形态（字符串 / 单段 / 数组）统一成数组。"""
    if message is None:
        return []
    if isinstance(message, str):
        return cq_to_array(message)
    if isinstance(message, dict):
        return [message]
    if isinstance(message, list):
        return [m if isinstance(m, dict) else Text(str(m)) for m in message]
    return [Text(str(message))]


def array_to_cq(segments: list[dict]) -> str:
    out = []
    for seg in segments:
        t = seg.get("type", "text")
        data = seg.get("data") or {}
        if t == "text":
            raw = str(data.get("text", ""))
            raw = raw.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
            out.append(raw)
        else:
            params = ",".join(f"{k}={v}" for k, v in data.items())
            out.append(f"[CQ:{t}{',' + params if params else ''}]")
    return "".join(out)


def cq_to_array(cq: str) -> list[dict]:
    segments: list[dict] = []
    pos = 0
    for m in CQ_RE.finditer(cq):
        if m.start() > pos:
            segments.append(Text(_unescape(cq[pos:m.start()])))
        data: dict[str, str] = {}
        body = m.group(2).lstrip(",")
        if body:
            for pair in body.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    data[k] = _unescape(v)
        segments.append({"type": m.group(1), "data": data})
        pos = m.end()
    if pos < len(cq):
        segments.append(Text(_unescape(cq[pos:])))
    return segments


def _unescape(s: str) -> str:
    return (s.replace("&#91;", "[").replace("&#93;", "]")
             .replace("&#44;", ",").replace("&amp;", "&"))


def to_plain(segments: list[dict], self_id: int | None = None) -> str:
    """转成给人类/LLM 看的纯文本。"""
    parts = []
    for seg in segments:
        t = seg.get("type", "text")
        data = seg.get("data") or {}
        if t == "text":
            parts.append(str(data.get("text", "")))
        elif t == "at":
            qq = str(data.get("qq", ""))
            if self_id and qq == str(self_id):
                parts.append("")
            elif qq == "all":
                parts.append("@全体成员 ")
            else:
                parts.append(f"@{qq} ")
        elif t == "reply":
            continue
        else:
            parts.append(PLACEHOLDER.get(t, f"[{t}]"))
    return "".join(parts).strip()


def is_at_me(segments: list[dict], self_id: int) -> bool:
    return any(s.get("type") == "at" and str((s.get("data") or {}).get("qq")) == str(self_id)
               for s in segments)


def find_reply_id(segments: list[dict]) -> str | None:
    """被引用那条的 message_id。

    NapCat / 不同实现把 id 放在 data.id，也有放 message_id 的；id 可能是
    字符串、负数或超大整数。取不到就返回 None——早期版本会 str(None) 得到
    字符串 "None"，下游拿它当 id 去调 get_msg 必然失败，日志里只剩一句
    debug，引用就这样静默丢了。
    """
    for seg in segments:
        if seg.get("type") != "reply":
            continue
        data = seg.get("data") or {}
        if not isinstance(data, dict):
            continue
        for key in ("id", "message_id", "reply_id"):
            value = data.get(key)
            if value is None or str(value).strip() == "":
                continue
            text = str(value).strip()
            return None if text == "None" else text
        return None
    return None
