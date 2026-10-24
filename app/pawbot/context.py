"""上下文对象 ctx —— 插件作者唯一需要打交道的东西。"""

from __future__ import annotations

import re

from . import message as msg


class Context:
    def __init__(self, event, bot, config, raw_text: str = "", matched_command: str = ""):
        self.event = event
        self.bot = bot
        self.config = config
        self.raw_text = raw_text
        self.matched_command = matched_command
        self.matched: re.Match | None = None
        self.state: dict = {}          # 同一事件内共享的临时状态
        self.stopped = False

    # ------- 便捷属性 -------
    @property
    def is_group(self) -> bool:
        return getattr(self.event, "is_group", False)

    @property
    def group_id(self) -> int | None:
        return getattr(self.event, "group_id", None)

    @property
    def user_id(self) -> int:
        return getattr(self.event, "user_id", 0)

    @property
    def self_id(self) -> int:
        return getattr(self.event, "self_id", 0)

    @property
    def plain(self) -> str:
        return getattr(self.event, "plain", "")

    @property
    def session_id(self) -> str:
        return getattr(self.event, "session_id", "")

    @property
    def sender_name(self) -> str:
        return getattr(self.event, "sender_name", "")

    # ------- 参数解析 -------
    @property
    def args(self) -> list[str]:
        """指令后面的参数，按空白切分。"""
        text = self.raw_text
        if self.matched_command:
            idx = text.find(self.matched_command)
            if idx >= 0:
                text = text[idx + len(self.matched_command):]
        return text.split()

    @property
    def arg_text(self) -> str:
        text = self.raw_text
        if self.matched_command:
            idx = text.find(self.matched_command)
            if idx >= 0:
                text = text[idx + len(self.matched_command):]
        return text.strip()

    # ------- 发消息 -------
    def _should_at(self, at) -> bool:
        mode = at if at is not None else self.config.get("reply.at_sender", "auto")
        if mode is True:
            mode = "always"
        if mode is False or mode == "off":
            return False
        if not self.is_group:
            return False
        if mode == "always":
            return True
        override = self.state.get("at_override")
        if override is not None:
            return bool(override)
        # auto：别人直接跟它说话（@ 或指令）才 @ 回去，否则像刷屏
        return bool(getattr(self.event, "to_me", False) or self.matched_command)

    def _should_quote(self, quote) -> bool:
        mode = quote if quote is not None else self.config.get("reply.quote", "auto")
        if mode is True:
            mode = "always"
        if mode is False or mode == "off":
            return False
        if not getattr(self.event, "message_id", 0):
            return False
        if mode == "always":
            return True
        override = self.state.get("quote_override")
        if override is not None:
            return bool(override) and not self.matched_command
        # auto：群里回话才引用；指令回复不引用，看着更利落
        return self.is_group and not self.matched_command

    async def reply(self, message, *, quote=None, at=None, elapsed: float = 0.0):
        """回复当前消息。引用和 @ 由 config.reply 决定，也可以调用时临时指定。

        组装顺序固定为：引用段 → @段 + 空格 → 正文。
        """
        body = msg.normalize(message)
        head: list[dict] = []

        if self._should_quote(quote):
            head.append(msg.Reply(self.event.message_id))

        if self._should_at(at):
            head.extend([msg.At(self.user_id), msg.Text(" ")])

        return await self.send(head + body, elapsed=elapsed)

    def at(self, user_id) -> dict:
        """给插件用的：构造一个 @ 段。"""
        return msg.At(user_id)

    async def send(self, message, *, group_id=None, user_id=None, elapsed: float = 0.0):
        segments = msg.normalize(message)
        conn = self.bot.conn_for(self.self_id)
        if conn is None:
            raise RuntimeError("没有可用的协议端连接")
        target_group = group_id if group_id else (self.group_id if self.is_group else None)
        target_user = user_id if user_id else (None if self.is_group else self.user_id)
        try:
            result = await self.bot.safety.send(conn, self.session_id, segments,
                                                group_id=target_group, user_id=target_user,
                                                elapsed=elapsed)
        except Exception as e:
            if getattr(e, "delivered", False):
                self.bot.stats["sent"] = self.bot.stats.get("sent", 0) + 1
            raise
        if result is not None:
            self.bot.stats["sent"] = self.bot.stats.get("sent", 0) + 1
            # 记住自己最后一条的 message_id，撤回要用
            try:
                mid = (result or {}).get("message_id") or \
                      ((result or {}).get("data") or {}).get("message_id")
                if mid:
                    self.bot.last_msg_id[self.session_id] = int(mid)
                    self.bot.last_msg_text[self.session_id] = msg.to_plain(segments)
            except Exception:
                pass
        return result
    def stop(self):
        """阻断后续处理器（比如指令处理完就不该再让 LLM 接话）。"""
        self.stopped = True
