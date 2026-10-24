"""熔断器：某个模型连续失败后，冷却期内直接跳过它，别再每轮空等超时。

聊天和看图都跑在线程池里（LLMClient / vision.describe），所以状态用
threading.Lock 保护。

状态机（按模型名分别记）：
    关闭   —— 正常调用；连续失败达 threshold 次 → 打开
    打开   —— 冷却 cooldown 秒；期间 is_open() 返回 True，调用方直接走备援
    半开   —— 冷却到期后放行一次试探请求：
               成功 → 关闭并清零；失败 → 重新冷却一轮

模块级单例 BREAKER 给 llm / vision 共用；测试里用 reset() 清干净。
"""

from __future__ import annotations

import threading
import time

from .log import log

DEFAULT_THRESHOLD = 2
DEFAULT_COOLDOWN = 300.0


class Breaker:
    def __init__(self, threshold: int = DEFAULT_THRESHOLD,
                 cooldown: float = DEFAULT_COOLDOWN, clock=None):
        self._lock = threading.Lock()
        self._threshold = int(threshold)
        self._cooldown = float(cooldown)
        self._clock = clock            # 注入假时钟用；None = time.monotonic()
        self._fails: dict[str, int] = {}      # 连续失败计数
        self._opened: dict[str, float] = {}   # 熔断开始的时间点
        self._probing: dict[str, float] = {}  # 半开试探的放行时间（防卡死）

    # ------------------------------------------------------------------ 内部
    def _now(self) -> float:
        clock = self._clock
        return clock() if clock is not None else time.monotonic()

    def configure(self, threshold=None, cooldown=None) -> None:
        """按配置更新默认阈值；单次调用也可以直接传参覆盖。"""
        with self._lock:
            if threshold is not None:
                self._threshold = max(1, int(threshold))
            if cooldown is not None:
                self._cooldown = max(0.0, float(cooldown))

    # ------------------------------------------------------------------ 查询
    def is_open(self, model: str, cooldown: float | None = None) -> bool:
        """True = 别打这个模型，直接走备援。

        冷却到期后第一次调用返回 False（放行一次试探），
        试探结束前（record_success / record_failure 之前）其余调用仍返回 True。
        """
        model = str(model or "")
        if not model:
            return False
        secs = self._cooldown if cooldown is None else max(0.0, float(cooldown))
        now = self._now()
        with self._lock:
            opened = self._opened.get(model)
            if opened is None:
                return False
            if now - opened < secs:
                return True
            started = self._probing.get(model)
            # 上一次试探还没回来（比如卡在网络上）：再等一个冷却周期，
            # 免得并发请求全都打到半死的模型上。
            if started is not None and now - started < secs:
                return True
            self._probing[model] = now
            return False

    def count(self, model: str) -> int:
        with self._lock:
            return self._fails.get(str(model or ""), 0)

    # ------------------------------------------------------------------ 记录
    def record_failure(self, model: str, threshold: int | None = None,
                       cooldown: float | None = None) -> bool:
        """记一次失败；返回这次是否把熔断打开了。"""
        model = str(model or "")
        if not model:
            return False
        limit = self._threshold if threshold is None else max(1, int(threshold))
        secs = self._cooldown if cooldown is None else max(0.0, float(cooldown))
        opened_now = False
        again = False
        with self._lock:
            self._probing.pop(model, None)
            self._fails[model] = self._fails.get(model, 0) + 1
            fails = self._fails[model]
            if model in self._opened:
                # 半开试探失败（或并发下又失败一次）：再冷却一轮
                self._opened[model] = self._now()
                again = True
            elif fails >= limit:
                self._opened[model] = self._now()
                opened_now = True
        if again:
            log.warn(f"模型 {model} 试探失败，再熔断 {int(secs)}s，期间直接用备援")
        elif opened_now:
            log.warn(f"模型 {model} 连续失败 {fails} 次，熔断 {int(secs)}s，"
                     f"期间直接用备援")
        return opened_now or again

    def record_success(self, model: str) -> None:
        """成功：清零计数；如果之前是熔断状态就关闭。"""
        model = str(model or "")
        if not model:
            return
        with self._lock:
            was_open = model in self._opened
            self._fails.pop(model, None)
            self._opened.pop(model, None)
            self._probing.pop(model, None)
        if was_open:
            log.info(f"模型 {model} 恢复")

    # ------------------------------------------------------------------ 测试
    def reset(self) -> None:
        with self._lock:
            self._fails.clear()
            self._opened.clear()
            self._probing.clear()
            self._threshold = DEFAULT_THRESHOLD
            self._cooldown = DEFAULT_COOLDOWN


BREAKER = Breaker()
