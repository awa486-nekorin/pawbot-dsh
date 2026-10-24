"""日志：控制台打印 + 环形缓冲 + 实时推给 WebUI（SSE）。"""

from __future__ import annotations

import asyncio
import time
from collections import deque

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


class Logger:
    def __init__(self, maxlen: int = 800):
        self.buffer: deque[dict] = deque(maxlen=maxlen)
        self.subscribers: list[asyncio.Queue] = []
        self.level = "debug"
        self.loop: asyncio.AbstractEventLoop | None = None
        self._file = None

    def to_file(self, path: str):
        """把日志同时追加到文件。

        以前只有启动时手动 `> data/run.log` 重定向才有日志文件，
        双击 start.bat 启动就什么都没有，事后完全没法排查。
        """
        try:
            import os
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._file = open(path, "a", encoding="utf-8", buffering=1)
        except Exception:
            self._file = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def _emit(self, level: str, msg: str, **fields):
        if LEVELS.get(level, 20) < LEVELS.get(self.level, 10):
            return
        rec = {
            "ts": time.strftime("%H:%M:%S", time.localtime()),
            "level": level,
            "msg": msg,
            **fields,
        }
        self.buffer.append(rec)

        if self._file is not None:
            try:
                self._file.write(f"[{rec['ts']}][{level.upper():5}] {msg}\n")
            except Exception:
                self._file = None

        color = {"debug": "\033[90m", "info": "\033[36m",
                 "warn": "\033[33m", "error": "\033[31m"}.get(level, "")
        print(f"{color}[{rec['ts']}][{level.upper():5}]\033[0m {msg}", flush=True)

        # asyncio.Queue 不是线程安全的：LLM 在线程池里、WebUI 在循环里，
        # 所以不在循环线程时，得把投递动作丢回循环去做。
        def _push():
            for q in list(self.subscribers):
                try:
                    q.put_nowait(rec)
                except asyncio.QueueFull:
                    pass

        try:
            asyncio.get_running_loop()
            _push()
        except RuntimeError:
            if self.loop and not self.loop.is_closed():
                try:
                    self.loop.call_soon_threadsafe(_push)
                except RuntimeError:
                    pass

    def debug(self, msg: str, **f):
        self._emit("debug", msg, **f)

    def info(self, msg: str, **f):
        self._emit("info", msg, **f)

    def warn(self, msg: str, **f):
        self._emit("warn", msg, **f)

    def error(self, msg: str, **f):
        self._emit("error", msg, **f)


log = Logger()
