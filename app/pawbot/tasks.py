"""后台任务登记处：不丢引用、异常有日志、停机前能等一等。

`asyncio.create_task(...)` 直接丢掉返回值有三个坑：

  1. 事件循环只保留任务的弱引用，任务跑着跑着可能被垃圾回收；
  2. 任务里抛的异常没人接，会被记成「Task exception was never retrieved」，
     既不在日志里，也看不到上下文；
  3. 停机时没地方等它们，写到一半的状态就丢了。

这里用模块级 set 留强引用，任务结束时自动移除；异常统一打一条 warn
（CancelledError 是正常取消，不记）。`drain()` 给 stop() 用。
"""

from __future__ import annotations

import asyncio

from .log import log

_TASKS: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task, name: str) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warn(f"后台任务 {name} 失败：{type(exc).__name__}: {exc}")


def spawn(coro, *, name: str = "") -> asyncio.Task:
    """把协程丢到后台跑，引用留住，异常记日志。"""
    task = asyncio.create_task(coro, name=name or None)
    label = name or getattr(coro, "__name__", "") or repr(coro)
    _TASKS.add(task)
    task.add_done_callback(lambda t: _on_done(t, label))
    return task


def pending() -> list[asyncio.Task]:
    """还没跑完的后台任务（快照，测试和排查用）。"""
    return [t for t in _TASKS if not t.done()]


async def drain(timeout: float = 5.0) -> None:
    """等剩余后台任务收尾；超时的取消掉，别让进程卡在退出上。"""
    rest = pending()
    if not rest:
        return
    _, still = await asyncio.wait(rest, timeout=timeout)
    for task in still:
        task.cancel()
    if still:
        await asyncio.wait(still, timeout=1.0)
