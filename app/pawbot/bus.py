"""
事件总线 + 处理器注册表。

AstrBot 里这条链路叫 pipeline（九级流水线），我们砍到够用的四级：
    限流 → 白名单 → 唤醒判定 → 处理器分发

每个 Handler 是一个 async 函数，签名 async def h(ctx)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .log import log

HandlerFn = Callable[[object], Awaitable[None]]

KIND_ORDER = {
    "command": 0,      # 指令最优先
    "regex": 1,
    "keyword": 2,
    "message": 3,      # 兜底的自然语言
    "notice": 4,
    "request": 5,
}


@dataclass
class Handler:
    fn: HandlerFn
    kind: str
    module: str = ""
    name: str = ""
    priority: int = 50
    enabled: bool = True

    # 匹配条件
    commands: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    keyword: str = ""
    pattern: re.Pattern | None = None
    notice_types: tuple[str, ...] = ()

    # 行为
    require_at: bool = False
    group_only: bool = False
    private_only: bool = False
    admin_only: bool = False
    block: bool = True          # 命中后是否阻断后续处理器

    def __post_init__(self):
        if not self.name:
            self.name = getattr(self.fn, "__name__", "handler")


class Bus:
    def __init__(self):
        self.handlers: list[Handler] = []

    def register(self, h: Handler):
        self.handlers.append(h)
        self.handlers.sort(key=lambda x: (x.priority, KIND_ORDER.get(x.kind, 9)))

    def remove_module(self, module: str) -> int:
        before = len(self.handlers)
        self.handlers = [h for h in self.handlers if h.module != module]
        return before - len(self.handlers)

    def by_module(self, module: str) -> list[Handler]:
        return [h for h in self.handlers if h.module == module]

    async def dispatch(self, ctx) -> int:
        """依次跑处理器，返回执行数量。"""
        ran = 0
        for h in list(self.handlers):
            if not h.enabled or ctx.stopped:
                continue
            if not _match(h, ctx):
                continue
            ran += 1
            log.debug(f"分发 → {h.kind}:{h.module}.{h.name}")
            try:
                await h.fn(ctx)
            except Exception as e:
                import traceback
                log.error(f"处理器 {h.module}.{h.name} 出错：{type(e).__name__} {e}")
                traceback.print_exc()
            if h.block:
                break
        return ran


def _match(h: Handler, ctx) -> bool:
    ev = ctx.event

    if h.kind in ("notice",):
        if ev.type != "notice":
            return False
        return not h.notice_types or getattr(ev, "notice_type", "") in h.notice_types

    if h.kind == "request":
        return ev.type == "request"

    # 以下都要求是消息事件
    if ev.type != "message":
        return False
    if h.group_only and not ev.is_group:
        return False
    if h.private_only and ev.is_group:
        return False
    if h.require_at and not ev.to_me:
        return False
    if h.admin_only and ev.role not in ("owner", "admin"):
        return False

    if h.kind == "command":
        return ctx.matched_command in h.commands + h.aliases
    if h.kind == "keyword":
        return h.keyword in ev.plain
    if h.kind == "regex":
        return bool(h.pattern and h.pattern.search(ev.plain))
    if h.kind == "message":
        return True
    return False


BUS = Bus()
