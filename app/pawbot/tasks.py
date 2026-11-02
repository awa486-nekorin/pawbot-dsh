"""后台任务登记处：不丢引用、异常有日志、停机前能等一等。

`asyncio.create_task(...)` 直接丢掉返回值有三个坑：

  1. 事件循环只保留任务的弱引用，任务跑着跑着可能被垃圾回收；
  2. 任务里抛的异常没人接，会被记成「Task exception was never retrieved」，
     既不在日志里，也看不到上下文；
  3. 停机时没地方等它们，写到一半的状态就丢了。

这里用模块级 set 留强引用，任务结束时自动移除；异常统一打一条 warn
（CancelledError 是正常取消，不记）。`drain()` 给 stop() 用。

任务归属（HA 批次新增，向后兼容）
---------------------------------
高可用模式不能让「丢租约」顺手取消传输层的重连/心跳任务，所以业务任务需要
能按 owner 分组取消。默认行为完全不变：不传 owner 时任务照旧登记在全局表里，
`pending()` / `drain()` 仍看全部任务。

* :func:`scope` / :func:`set_owner` / :func:`reset_owner` —— ContextVar 作用域，
  在事件包装层套一层后，里面所有 ``tasks.spawn`` 自动继承 owner；
* :func:`spawn(..., owner=...)` —— 显式指定（覆盖继承值）；
* :func:`pending(owner=...)` / :func:`owned` —— 只看某组任务；
* :func:`cancel_owned` —— 只取消某组任务，返回超时后仍未结束的（调用方据此
  fail closed，而不是假装已经排空）。
"""

from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from typing import Iterator

from .log import log

_TASKS: set[asyncio.Task] = set()

# 当前业务作用域的 owner（默认 None = 未分组，与旧行为一致）。
_CURRENT_OWNER: ContextVar[str | None] = ContextVar("pawbot-task-owner", default=None)
# 每个任务创建时的 owner 快照；任务结束时移除。
_TASK_OWNER: dict[asyncio.Task, str | None] = {}

# 内部哨兵：区分「没传 owner」（继承当前作用域）和「显式传 None」。
_INHERIT = object()
_ALL = object()


def _on_done(task: asyncio.Task, name: str) -> None:
    _TASKS.discard(task)
    _TASK_OWNER.pop(task, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warn(f"后台任务 {name} 失败：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- owner 作用域


def current_owner() -> str | None:
    """当前上下文继承的任务 owner；没有作用域时是 None。"""
    return _CURRENT_OWNER.get()


def set_owner(owner: str | None):
    """设置当前上下文的 owner，返回给 :func:`reset_owner` 的 token。"""
    return _CURRENT_OWNER.set(None if owner is None else str(owner))


def reset_owner(token) -> None:
    _CURRENT_OWNER.reset(token)


@contextlib.contextmanager
def scope(owner: str) -> Iterator[None]:
    """在 ``with`` 块里创建的任务都归 ``owner`` 所有（嵌套可恢复）。"""
    token = _CURRENT_OWNER.set(str(owner))
    try:
        yield
    finally:
        _CURRENT_OWNER.reset(token)


def owner_of(task: asyncio.Task) -> str | None:
    """任务创建时记录的 owner；未知任务返回 None。"""
    return _TASK_OWNER.get(task)


# ---------------------------------------------------------------- 登记 / 查询


def spawn(coro, *, name: str = "", owner=_INHERIT) -> asyncio.Task:
    """把协程丢到后台跑，引用留住，异常记日志。

    ``owner`` 不传时继承 :func:`scope` 设置的当前 owner；显式传 ``None``
    则强制不分组。返回的 Task 仍登记在全局表里（停机 ``drain`` 能看到）。
    """
    if owner is _INHERIT:
        tag = _CURRENT_OWNER.get()
    else:
        tag = None if owner is None else str(owner)
    task = asyncio.create_task(coro, name=name or None)
    label = name or getattr(coro, "__name__", "") or repr(coro)
    _TASKS.add(task)
    _TASK_OWNER[task] = tag
    task.add_done_callback(lambda t: _on_done(t, label))
    return task


def pending(owner=_ALL) -> list[asyncio.Task]:
    """还没跑完的后台任务（快照，测试和排查用）。

    不传 ``owner`` 返回全部（与旧版一致）；传了就只返回该 owner 的任务
    （``owner=None`` 表示未分组的任务）。
    """
    if owner is _ALL:
        return [t for t in _TASKS if not t.done()]
    tag = None if owner is None else str(owner)
    return [t for t in _TASKS if not t.done() and _TASK_OWNER.get(t) == tag]


def owned(owner: str) -> list[asyncio.Task]:
    """``pending(owner=...)`` 的别名，读起来更清楚。"""
    return pending(owner)


async def cancel_owned(owner: str, timeout: float = 2.0) -> list[asyncio.Task]:
    """只取消 ``owner`` 名下的任务，最多等 ``timeout`` 秒。

    返回超时后仍未结束的任务；非空表示「排空失败」，调用方必须 fail closed
    （不能补一次内容推送、不能重新激活业务）。

    刻意不碰其它任务：传输层重连、心跳、内容同步等任务不属于业务 owner，
    不会被这里取消。
    """
    tag = str(owner)
    current = asyncio.current_task()
    victims = [t for t in pending(tag) if t is not current and not t.done()]
    if not victims:
        return []
    for task in victims:
        try:
            task.cancel()
        except Exception:  # noqa: BLE001 - 取消失败留给超时判定
            pass
    _done, still = await asyncio.wait(victims, timeout=max(0.0, float(timeout)))
    return [task for task in still if not task.done()]


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
