"""自己的爱好看板：几部在追的番、在玩的游戏，定期上网看看有没有新进展。

``data/hobbies.json`` 长这样：

    {"items": [{"name": "葬送的芙莉莲", "kind": "anime",
                "keywords": ["芙莉莲", "Frieren"],
                "added_ts": 0, "last_checked": 0, "last_news": ""}]}

第一次运行自动写入三条默认爱好。检查结果只更新文件、返回摘要，
发不发由 proactive 决定——这里绝不自己发消息。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from difflib import SequenceMatcher

from . import websearch
from .log import log

# 默认爱好：两部近年中文圈讨论度高的番 + 一个长期在玩的游戏。
# 选它们是因为更新节奏稳定、中文资料多，搜「最新」总能搜到东西。
DEFAULT_ITEMS = (
    {"name": "葬送的芙莉莲", "kind": "anime",
     "keywords": ["芙莉莲", "Frieren", "葬送的芙莉莲"]},
    {"name": "胆大党", "kind": "anime",
     "keywords": ["胆大党", "当哒当", "Dandadan"]},
    {"name": "鸣潮", "kind": "game",
     "keywords": ["鸣潮", "Wuthering Waves"]},
)

_MAX_SUMMARY = 300


def _similar(a: str, b: str) -> float:
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _clean(text, limit: int = 0) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] if limit else text


class Hobbies:
    def __init__(self, base_dir: str, config):
        self.base_dir = base_dir
        self.config = config
        self.enabled = bool(config.get("hobbies.enabled", True))
        self.path = os.path.join(base_dir, "data", "hobbies.json")
        self.max_items = self._int("max_items", 6, 1)
        self.items: list[dict] = self.load()
        if not os.path.exists(self.path):
            self.save()

    def _int(self, key: str, default: int, minimum: int) -> int:
        try:
            return max(minimum, int(self.config.get(f"hobbies.{key}", default) or default))
        except (TypeError, ValueError):
            return default

    def check_every_hours(self) -> float:
        try:
            return max(0.1, float(self.config.get("hobbies.check_every_hours", 12) or 12))
        except (TypeError, ValueError):
            return 12.0

    # ------------------------------------------------------------ 存取
    @staticmethod
    def _normalize(raw: dict, fallback_ts: float) -> dict | None:
        name = _clean(raw.get("name"))
        if not name:
            return None
        kind = _clean(raw.get("kind")).lower()
        if kind not in ("anime", "game"):
            kind = "anime"
        words = raw.get("keywords")
        if not isinstance(words, (list, tuple)):
            words = [name]
        keywords = []
        seen = set()
        for word in words:
            word = _clean(word)[:30]
            if word and word.casefold() not in seen:
                seen.add(word.casefold())
                keywords.append(word)
        try:
            added = float(raw.get("added_ts") or fallback_ts)
        except (TypeError, ValueError):
            added = fallback_ts
        try:
            checked = float(raw.get("last_checked") or 0)
        except (TypeError, ValueError):
            checked = 0.0
        return {
            "name": name[:60],
            "kind": kind,
            "keywords": keywords[:8] or [name[:60]],
            "added_ts": added,
            "last_checked": checked,
            "last_news": _clean(raw.get("last_news"), _MAX_SUMMARY),
        }

    def load(self) -> list[dict]:
        now = time.time()
        raw_items = None
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw_items = data.get("items") if isinstance(data, dict) else data
            except Exception as e:
                log.debug(f"爱好看板读取失败：{type(e).__name__}")
        if not isinstance(raw_items, list) or not raw_items:
            raw_items = [dict(item, added_ts=now, last_checked=0, last_news="")
                         for item in DEFAULT_ITEMS]
        items = []
        for raw in raw_items:
            if isinstance(raw, dict):
                item = self._normalize(raw, now)
                if item:
                    items.append(item)
        return items[:self.max_items]

    def save(self) -> bool:
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".hobbies.", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"items": self.items[:self.max_items]}, f,
                              ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except Exception as e:
            log.warn(f"爱好看板写入失败：{type(e).__name__}")
            return False

    def add(self, name: str, kind: str = "anime", keywords=None) -> dict | None:
        """加一个爱好；同名（不分大小写）或超过 max_items 就不加。"""
        item = self._normalize({"name": name, "kind": kind, "keywords": keywords},
                               time.time())
        if not item:
            return None
        key = item["name"].casefold()
        if any(existing["name"].casefold() == key for existing in self.items):
            return None
        if len(self.items) >= self.max_items:
            return None
        self.items.append(item)
        self.save()
        return item

    # ------------------------------------------------------------ 检查
    def due_items(self, now: float | None = None) -> list[dict]:
        if not self.enabled:
            return []
        current = time.time() if now is None else float(now)
        gap = self.check_every_hours() * 3600
        due = [item for item in self.items
               if current - float(item.get("last_checked") or 0) >= gap]
        due.sort(key=lambda item: float(item.get("last_checked") or 0))
        return due

    @staticmethod
    def _summarize(results) -> str:
        pieces = []
        for row in list(results or [])[:3]:
            if not isinstance(row, dict):
                continue
            title = _clean(row.get("title"), 90)
            snippet = _clean(row.get("snippet"), 120)
            if title and snippet:
                pieces.append(f"{title}：{snippet}")
            elif title:
                pieces.append(title)
        return "；".join(pieces)[:_MAX_SUMMARY]

    async def check(self, bot, item: dict) -> str:
        """搜一次这个爱好的新进展；有新内容返回摘要（≤300 字），否则空串。"""
        if not self.enabled or not isinstance(item, dict):
            return ""
        name = _clean(item.get("name"))
        if not name:
            return ""
        query = f"{name} 最新 剧情/更新"
        result = {}
        try:
            result = await asyncio.to_thread(websearch.search, query, bot.config)
        except Exception as e:
            log.debug(f"爱好检索失败 {name}：{type(e).__name__}")
        summary = self._summarize((result or {}).get("results") if isinstance(result, dict) else None)
        item["last_checked"] = time.time()
        fresh = ""
        if summary and _similar(summary, item.get("last_news") or "") <= 0.7:
            item["last_news"] = summary
            fresh = summary
        self.save()
        return fresh
