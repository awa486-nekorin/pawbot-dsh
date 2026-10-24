"""
尺度控制：这个群现在能聊多荤。

不是一把开关，是两档，而且**按群分开**——A 群能开黄腔，不代表 B 群也行。

    1  收敛点  能接荤梗、讲荤段子、开轻微黄腔
    2  爱色色  能开黄腔、撩骚、暧昧互动、找擦边的图，R18 明说也给

**没有素聊档了。** 原来的 0 档（素聊）已废除：任何 ≤0 的值一律按 2「爱色色」处理。
这是主人的决定——不要素聊群，她就是爱色色。

档位存在 `data/spice.json`，群里用 `/荤` `/荤 2` `/当前尺度` 调。
（`/素` 还在，但现在没有素聊可调，会直接告诉她这件事。）

另外提供 `joke()`：单独调一次模型、专门用「讲荤段子」的人设来写，
比指望聊天模型自己临场发挥稳定得多——要段子的时候就是要段子，
不该等它自己决定给不给。
"""

from __future__ import annotations

import json
import os
import random
import time

LEVEL_NAMES = {1: "收敛点", 2: "爱色色"}

# 各档位给模型的说明
HINTS = {
    1: "这个群能接荤梗、讲荤段子、开玩笑式地开点黄腔。"
       "对方起哄你就接，要段子就给段子，别突然装纯洁也别说教。",
    2: "这个群放得开，你本来就爱色色。可以开黄腔、撩骚、暧昧互动，"
       "对方要擦边的图也能找，明说要 R18 就给。该撩就撩，别扭捏，"
       "但也别每句都往荤了带——那是另一种别扭。",
}

JOKE_PROMPT = """你在一个熟人群里讲荤段子。要求：

- 直接给段子正文，不要任何前缀、免责声明、括号说明
- 短。一到三句，能在群里发出去的那种
- 要损、要自然、最好跟群里的语境能对上
- 不要写成完整故事，不要教育人，不要说「仅供娱乐」
- 中文口语化，标点正常

现在讲一个{topic}的荤段子。"""


class Spice:
    def __init__(self, path: str, default: int = 0):
        self.path = path
        self.default = int(default)
        self.data: dict[str, int] = {}
        self._load()

    # ------------------------------------------------------------ 存取
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k, v in (d or {}).items():
                try:
                    self.data[k] = max(0, min(2, int(v)))
                except Exception:
                    continue
        except Exception:
            pass

    def save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def get(self, session_id: str) -> int:
        """当前档位。**0 档已废除**：读到 ≤0 一律当作 2「爱色色」。"""
        v = int(self.data.get(session_id, self.default))
        return 2 if v <= 0 else v

    def set(self, session_id: str, level: int) -> int:
        """设档位。**≤0 会被抬到 2**——没有素聊档可设。"""
        level = int(level)
        level = 2 if level <= 0 else min(2, level)
        self.data[session_id] = level
        self.save()
        return level

    def all(self) -> dict:
        return dict(self.data)

    # ------------------------------------------------------------ 提示
    def hint(self, session_id: str) -> str:
        lv = self.get(session_id)
        return HINTS.get(lv, HINTS[2])

    def label(self, session_id: str) -> str:
        return LEVEL_NAMES.get(self.get(session_id), "爱色色")


# --------------------------------------------------------------------- 荤段子
_TOPICS = ["日常的", "打游戏的", "熬夜的", "考试的", "吃饭的", "挤地铁的", "开会的"]


async def joke(llm, level: int = 2, topic: str = "") -> str:
    """专门写一条荤段子。没有素聊档，最小也是 1（收敛点）。"""
    if int(level) <= 0:
        level = 1
    topic = (topic or "").strip() or random.choice(_TOPICS)
    prompt = JOKE_PROMPT.format(topic=topic)
    try:
        text = await llm.chat([
            {"role": "system", "content": "你是个混群的，说话没正形，但有点分寸。"},
            {"role": "user", "content": prompt},
        ])
    except Exception:
        return ""
    text = (text or "").strip()
    # 模型偶尔会带前缀，剥掉
    for pre in ("段子：", "荤段子：", "来一个：", "答案："):
        if text.startswith(pre):
            text = text[len(pre):].strip()
    return text
