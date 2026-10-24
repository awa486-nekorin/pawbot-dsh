"""
记忆区：可读、可写、可检索的纯文本记忆。

    data/memory/facts.md        事实（"主人在准备数学考试"）
    data/memory/slang.md        网络用词（"绝绝子 = 太……了"）
    data/memory/impression.md   对群友的印象（"阿明：爱问技术问题"）

检索方式很朴素：按字/词重叠打分。够用，而且你打开文件就能看懂它在记什么。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from types import SimpleNamespace

from .persona import append_line

FILES = {
    "fact": ("facts.md", "# 长期记忆\n"),
    "slang": ("slang.md", "# 网络用词 / 梗\n"),
    "impression": ("impression.md", "# 对群友的印象\n"),
    "people": ("people.md", "# 人物卡\n"),
}


class Memory:
    def __init__(self, base_dir: str, config):
        self.base_dir = base_dir
        self.config = config
        self.longmem = None
        d = config.get("persona.memory_dir", "data/memory")
        self.dir = d if os.path.isabs(d) else os.path.join(base_dir, d)

    def path(self, kind: str) -> str:
        name, _ = FILES.get(kind, FILES["fact"])
        return os.path.join(self.dir, name)

    # ---------------------------------------------------------- 写
    def remember(self, text: str, kind: str = "fact") -> bool:
        text = text.strip().replace("\n", " ")
        if not text:
            return False
        _, header = FILES.get(kind, FILES["fact"])
        stamp = time.strftime("%Y-%m-%d")
        return append_line(self.path(kind), f"- [{stamp}] {text}", header)

    # ---------------------------------------------------------- 读 / 检索
    def _lines(self, kind: str) -> list[str]:
        p = self.path(kind)
        if not os.path.exists(p):
            return []
        with open(p, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip().startswith(("-", "*"))]

    def recall(self, query: str, kind: str | None = None, limit: int = 5) -> list[str]:
        """按词重叠与日期衰减打分，同分时较新的条目优先。"""
        if self.longmem is not None:
            entries = self.longmem.recall_for(SimpleNamespace(user_id=0, session_id=""),
                                               query, k=limit, fact_kind=kind)
            return [f"[{entry.get('kind') or 'episode'}] {entry['text']}"
                    for entry in entries[:limit]]
        tokens = _tokens(query)
        if not tokens:
            return []
        kinds = [kind] if kind else list(FILES)
        now = time.time()
        scored: list[tuple[float, float, str]] = []

        for k in kinds:
            for index, line in enumerate(self._lines(k)):
                body = re.sub(r"^[-*]\s*(\[[^\]]+\]\s*)?", "", line)
                overlap = len(tokens & _tokens(body))
                if not overlap:
                    continue
                stamp = re.match(r"^[-*]\s*\[(\d{4}-\d{2}-\d{2})\]", line)
                ts = 0.0
                if stamp:
                    try:
                        ts = datetime.strptime(stamp.group(1), "%Y-%m-%d").timestamp()
                    except ValueError:
                        pass
                age_days = max(0.0, now - ts) / 86400 if ts else 0.0
                score = overlap * (0.5 ** (age_days / 7.0))
                scored.append((score, ts or index, f"[{k}] {body}"))

        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [line for _, _, line in scored[:limit]]

    def all_lines(self, kind: str) -> list[str]:
        return self._lines(kind)

    # ---------------------------------------------------------- 人物卡
    def person(self, name: str) -> list[str]:
        """查某人的卡片条目（按名字前缀匹配）。"""
        name = (name or "").strip()
        if not name:
            return []
        out = []
        for line in self._lines("people"):
            body = re.sub(r"^[-*]\s*(\[[^\]]+\]\s*)?", "", line)
            if body.startswith(name + "：") or body.startswith(name + ":"):
                out.append(body.split("：", 1)[-1].split(":", 1)[-1].strip())
        return out

    def note_person(self, name: str, note: str) -> bool:
        """写成「阿明：爱问技术问题」这种一行一条的卡片。"""
        name = (name or "").strip()
        note = (note or "").strip().replace("\n", " ")
        if not name or not note:
            return False
        return self.remember(f"{name}：{note}", "people")


def _tokens(text: str) -> set[str]:
    text = text.strip()
    if not text:
        return set()
    out: set[str] = set()
    # 英文/数字词
    out.update(w.lower() for w in re.findall(r"[A-Za-z0-9_]{2,}", text))
    # 中文按二字组
    han = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in han:
        if len(run) <= 2:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out
