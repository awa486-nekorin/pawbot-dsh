"""
性欲值：一个独立于 8 维情绪的状态，0~100。

它只在「允许荤」的群里起作用。**现在已经没有素聊档了**（spice 恒 ≥ 1），
所以它在每个群里都会更新。数值本身不会进聊天，也不会被说出来。

涨：被撩、暧昧、亲密表达、擦边话题；掉：时间（向基线回落）、正常闲聊。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from .log import log

BASE = 20.0

_UP_RE = re.compile(
    r"想你|想要你|抱抱|抱我|贴贴|亲亲|亲一口|摸头|摸摸|撩|色色|涩涩|擦边|擦邊|"
    r"性感|好色|馋你|心动|心動|喜欢你|喜欢你|勾引|暧昧|曖昧|晚安吻|"
    r"(?<![A-Za-z])(?:sexy|horny|kiss|hug)(?![A-Za-z])", re.I)
_DOWN_RE = re.compile(r"别闹|正经|素的|素聊|工作|作业|考试|代码|bug|报错")


class Libido:
    """性欲值：只在 spice ≥ 1 的群里影响语气。"""

    def __init__(self, path: str | None = None, config=None):
        self.path = path
        self.config = config
        self.lock = threading.RLock()
        self.value = BASE
        self.updated = time.time()
        if path:
            self._load()
        self.decay()

    # ------------------------------------------------------------ 存取
    def _cfg(self, key, default):
        try:
            return self.config.get(key, default) if self.config is not None else default
        except Exception:
            return default

    def _load(self):
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.value = max(0.0, min(100.0, float(data.get("value", BASE))))
            self.updated = float(data.get("updated", time.time()))
        except Exception:
            pass

    def save(self):
        if not self.path:
            return
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"value": round(self.value, 1), "updated": self.updated},
                              f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception as e:
                log.debug(f"性欲值保存失败：{type(e).__name__}")

    # ------------------------------------------------------------ 变化
    def decay(self, now: float | None = None):
        """时间越久越回到基线。"""
        now = time.time() if now is None else now
        minutes = max(0.0, (now - self.updated) / 60)
        half = max(1.0, float(self._cfg("libido.half_life_min", 180)))
        self.value = BASE + (self.value - BASE) * (0.5 ** (minutes / half))
        self.updated = now
        self.value = max(0.0, min(100.0, self.value))

    def bump(self, delta: float) -> float:
        self.value = max(0.0, min(100.0, self.value + float(delta)))
        self.updated = time.time()
        return self.value

    def note(self, text: str, *, spice: int = 0, addressed: bool = False) -> float:
        """一条消息带来的变化。"""
        if int(spice) <= 0:
            return self.value
        self.decay()
        text = (text or "").strip()
        up = len(_UP_RE.findall(text))
        down = len(_DOWN_RE.findall(text))
        delta = min(16.0, up * 8.0) - min(8.0, down * 4.0)
        if addressed and up:
            delta += 3.0             # 直接冲她来的撩，反应更明显
        step = float(self._cfg("libido.max_step", 16))
        delta = max(-step, min(step, delta))
        if abs(delta) < 0.01:
            return self.value
        return self.bump(delta)

    # ------------------------------------------------------------ 提示
    def label(self) -> str:
        v = self.value
        if v >= 75:
            return "很上头"
        if v >= 55:
            return "被撩到了"
        if v >= 30:
            return "心痒"
        return "平常"

    def hint(self, spice: int) -> str:
        """给模型的语气提示；数值永不出现。"""
        if int(spice) <= 0:
            return ""
        self.decay()
        v = self.value
        if v < 30:
            return ""
        if v < 55:
            return ("你现在有点心痒，被撩到的时候会比平时主动一点，"
                    "可以接回去、可以嘴硬，但别超过这个群允许的尺度，也别主动把话题往床上带。")
        if v < 75:
            return ("你现在明显被撩到了，语气黏一点、会主动撩回去，"
                    "但始终守住这个群允许的尺度；对方踩刹车就立刻收。")
        return ("你现在很上头，想黏人、想被哄，会撒娇也会说点露骨的玩笑。"
                "仍然只看这个群允许到哪一步，不给没同意的人施压，也不越级。"
                "别把「想要」挂嘴边说个没完。")
