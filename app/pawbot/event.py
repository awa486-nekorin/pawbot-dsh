"""把 OneBot 原始 JSON 事件，翻译成统一的事件对象（对应 AstrBot 里的 AstrMessageEvent）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import message as msg


@dataclass
class Event:
    post_type: str                     # message / notice / request / meta_event
    self_id: int = 0
    raw: dict = field(default_factory=dict)
    time: int = field(default_factory=lambda: int(time.time()))

    @property
    def type(self) -> str:
        return self.post_type


@dataclass
class MessageEvent(Event):
    message_type: str = "private"      # group / private
    sub_type: str = "normal"
    message_id: int = 0
    user_id: int = 0
    group_id: int | None = None
    message: list[dict] = field(default_factory=list)
    raw_message: str = ""
    sender: dict = field(default_factory=dict)
    plain: str = ""                    # 去掉 @/表情 后的纯文本
    to_me: bool = False                # 是否 @ 了机器人
    reply_id: str | None = None
    quoted_text: str = ""              # 感知层补：被引用那条的正文
    quoted_from: str = ""              # 感知层补：被引用那条是谁发的
    quoted_is_self: bool = False       # 感知层补：引用的那条是不是自己发的
    quoted_chain: list[dict] = field(default_factory=list)  # 感知层补：最多三层引用
    image_desc: str = ""               # 感知层补：本消息图片的视觉描述
    image_items: list[dict] = field(default_factory=list)   # 感知层补：统一编号的图片
    image_data: list = field(default_factory=list)  # 感知层补：原图 data URL（给主模型看）
    person_card: str = ""              # 感知层补：说话人的档案
    forward_text: str = ""             # 感知层补：合并转发的展开内容
    card_text: str = ""                # 感知层补：JSON 卡片（B站/小程序）的正文
    talked_about: bool = False         # 感知层补：没叫它，但话头在说它
    voice_text: str = ""               # 感知层补：语音转写出来的原话
    video_desc: str = ""               # 感知层补：视频抽帧后的视觉描述

    @property
    def is_group(self) -> bool:
        return self.message_type == "group"

    @property
    def session_id(self) -> str:
        return f"group_{self.group_id}" if self.is_group else f"private_{self.user_id}"

    @property
    def sender_name(self) -> str:
        return (self.sender.get("card") or self.sender.get("nickname")
                or str(self.user_id))

    @property
    def role(self) -> str:
        return self.sender.get("role", "member")


@dataclass
class NoticeEvent(Event):
    notice_type: str = ""
    group_id: int | None = None
    user_id: int = 0
    operator_id: int = 0
    target_id: int = 0
    raw_notice: dict = field(default_factory=dict)
    sub_type: str = ""

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)

    @property
    def session_id(self) -> str:
        return f"group_{self.group_id}" if self.is_group else f"private_{self.user_id or self.operator_id}"

    @property
    def sender_name(self) -> str:
        sender = self.raw_notice.get("sender") or {}
        return str(sender.get("card") or sender.get("nickname")
                   or self.user_id or self.operator_id)

    @property
    def to_me(self) -> bool:
        return bool(self.self_id and self.target_id == self.self_id)


@dataclass
class RequestEvent(Event):
    request_type: str = ""
    group_id: int | None = None
    user_id: int = 0
    comment: str = ""
    flag: str = ""


def parse(raw: dict) -> Event | None:
    """入口：Raw OneBot JSON → Event。"""
    post_type = raw.get("post_type")
    base = {
        "post_type": post_type or "unknown",
        "self_id": int(raw.get("self_id") or 0),
        "raw": raw,
        "time": int(raw.get("time") or time.time()),
    }

    if post_type == "message" or post_type == "message_sent":
        segments = msg.normalize(raw.get("message"))
        self_id = base["self_id"]
        plain = msg.to_plain(segments, self_id=self_id)
        is_group = raw.get("message_type") == "group"
        return MessageEvent(
            **base,
            message_type=raw.get("message_type", "private"),
            sub_type=raw.get("sub_type", "normal"),
            message_id=int(raw.get("message_id") or 0),
            user_id=int(raw.get("user_id") or 0),
            group_id=int(raw["group_id"]) if is_group and raw.get("group_id") else None,
            message=segments,
            raw_message=str(raw.get("raw_message") or ""),
            sender=raw.get("sender") or {},
            plain=plain,
            to_me=bool(is_group and msg.is_at_me(segments, self_id)) or not is_group,
            reply_id=msg.find_reply_id(segments),
        )

    if post_type == "notice":
        return NoticeEvent(
            **base,
            notice_type=raw.get("notice_type", ""),
            sub_type=str(raw.get("sub_type") or ""),
            group_id=int(raw["group_id"]) if raw.get("group_id") else None,
            user_id=int(raw.get("user_id") or 0),
            operator_id=int(raw.get("operator_id") or 0),
            target_id=int(raw.get("target_id") or 0),
            raw_notice=raw,
        )

    if post_type == "request":
        return RequestEvent(
            **base,
            request_type=raw.get("request_type", ""),
            group_id=int(raw["group_id"]) if raw.get("group_id") else None,
            user_id=int(raw.get("user_id") or 0),
            comment=str(raw.get("comment") or ""),
            flag=str(raw.get("flag") or ""),
        )

    if post_type == "meta_event":
        return Event(**base)

    return Event(**base)
