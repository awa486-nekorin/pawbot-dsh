"""梗 / 黑话知识库：平时不注入，只在当前消息或最近几条命中触发词时才想起来。

群里天天冒新梗，全塞进提示词既费 token 又会让模型每句都想玩梗。
所以这里只做一件小事：本地留一份「梗名 + 触发词 + 出处 + 用法」，
每轮拿最近几条消息碰一下触发词，命中了才把那一两条递给模型（agent.py），
没命中就当它不存在。

知识可以两个方向进来：
  · 群友当场解释了某个梗 → turns.py 的判官顺手记下来（source=群友）；
  · 模型自己查了萌娘百科/搜索后搞懂了 → 调 learn_meme 工具存下来。

文件是 data/memes.json，结构 {"items": [{name, triggers, meaning, usage,
source, learned_from, ts, hits}]}。写盘走 tmp + os.replace，读坏了按空库处理。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from functools import lru_cache

from .log import log

MIN_TRIGGER = 2          # 单字触发词一律忽略：中文单字太容易误伤
MAX_DETAIL = 120         # 每条 meaning + usage 注入时的字数上限
HEADER = ("## 可能用到的梗（只在聊到时帮你听懂；不用每句都玩梗，"
          "别解释梗，也别复述这段）")

_SPLIT_TRIGGERS = re.compile(r"[,，、;；\n]+")


def _clean(value, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def _unquote(text: str) -> str:
    """判官有时把意思整段套在引号里返回，去掉外层引号再用。"""
    t = text.strip()
    for a, b in (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"), ("『", "』")):
        if len(t) >= 2 and t.startswith(a) and t.endswith(b):
            return t[1:-1].strip()
    return t


def _clean_triggers(values) -> list[str]:
    """规整触发词：去掉单字/空串，按大小写去重。"""
    if isinstance(values, str):
        values = _SPLIT_TRIGGERS.split(values)
    elif not isinstance(values, (list, tuple, set)):
        values = []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean(value, 40).strip("\"'“”‘’")
        if len(text) < MIN_TRIGGER:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= 16:
            break
    return out


@lru_cache(maxsize=512)
def _ascii_pattern(trigger: str) -> re.Pattern:
    """英文触发词按「整词」匹配，man 不能命中 manager / human。"""
    parts = [re.escape(part) for part in trigger.casefold().split()]
    body = r"\s+".join(parts)
    return re.compile(r"(?<![a-z0-9_])" + body + r"(?![a-z0-9_])")


def _hit(haystack: str, trigger: str) -> bool:
    text = str(trigger or "").strip()
    if len(text) < MIN_TRIGGER:
        return False
    if text.isascii():
        return _ascii_pattern(text).search(haystack) is not None
    return text.casefold() in haystack


class MemeBook:
    def __init__(self, path: str, config=None, enabled: bool | None = None):
        self.path = str(path)
        self.config = config
        self._enabled = enabled
        self._lock = threading.RLock()
        self._dirty = False
        self.items: list[dict] = []
        self._load()

    # ---------------------------------------------------------- 开关
    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return bool(self._enabled)
        if self.config is None:
            return True
        try:
            return bool(self.config.get("memes.enabled", True))
        except Exception:
            return True

    # ---------------------------------------------------------- 读写
    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warn(f"梗库读取失败，按空库处理：{type(e).__name__} {e}")
            return
        rows = data.get("items") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            log.warn("梗库格式不对（items 不是列表），按空库处理")
            return
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = _clean(row.get("name"), 40)
            if not name:
                continue
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            try:
                hits = max(0, int(row.get("hits") or 0))
            except (TypeError, ValueError):
                hits = 0
            items.append({
                "name": name,
                "triggers": _clean_triggers(row.get("triggers")),
                "meaning": _clean(row.get("meaning"), 500),
                "usage": _clean(row.get("usage"), 300),
                "source": _clean(row.get("source"), 300),
                "learned_from": _clean(row.get("learned_from"), 80),
                "ts": ts,
                "hits": hits,
            })
        self.items = items

    def _write(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self.items}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def flush(self) -> bool:
        """把 hits 等脏数据落盘；没有改动就直接返回 False。"""
        with self._lock:
            if not self._dirty:
                return False
            try:
                self._write()
            except OSError as e:
                log.warn(f"梗库落盘失败：{type(e).__name__} {e}")
                return False
            self._dirty = False
            return True

    # ---------------------------------------------------------- 匹配
    def match(self, text: str) -> list[dict]:
        """返回命中的梗（按命中触发词长度降序、hits 降序），不重复。"""
        haystack = str(text or "").casefold()
        if not haystack.strip():
            return []
        with self._lock:
            found: list[tuple[dict, list[str]]] = []
            for item in self.items:
                matched = [t for t in item.get("triggers") or [] if _hit(haystack, t)]
                if matched:
                    found.append((item, matched))
            found.sort(key=lambda pair: (-max(len(t) for t in pair[1]),
                                         -int(pair[0].get("hits") or 0),
                                         str(pair[0].get("name") or "")))
            out: list[dict] = []
            seen: set[str] = set()
            for item, matched in found:
                key = str(item.get("name") or "").casefold()
                if key in seen:
                    continue
                seen.add(key)
                out.append({**item, "matched": matched})
            return out

    def inject_for(self, texts, limit: int = 3) -> str:
        """最近几条消息合起来碰一次触发词；没命中返回空串（不占上下文）。"""
        if not self.enabled:
            return ""
        if isinstance(texts, str):
            texts = [texts]
        try:
            joined = "\n".join(str(t or "") for t in (texts or []))
        except TypeError:
            joined = str(texts or "")
        if not joined.strip():
            return ""
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 3
        hits = self.match(joined)[:limit]
        if not hits:
            return ""
        lines = [HEADER]
        for item in hits:
            name = _clean(item.get("name"), 40)
            meaning = _clean(item.get("meaning"), 300)
            usage = _clean(item.get("usage"), 200)
            detail = f"{meaning}用法：{usage}" if usage else meaning
            detail = detail[:MAX_DETAIL]
            lines.append(f"- {name}：{detail}" if detail else f"- {name}")
            self.bump(name)
        return "\n".join(lines)

    # ---------------------------------------------------------- 写入
    def learn(self, name, triggers, meaning, usage="", source="群友",
              learned_from="") -> bool:
        """同名（或触发词有重叠）就合并，新的就新增。"""
        name = _unquote(_clean(name, 40))
        meaning = _unquote(_clean(meaning, 500))
        usage = _unquote(_clean(usage, 300))
        source = _clean(source, 300)
        learned_from = _clean(learned_from, 80)
        if not name or not meaning:
            return False
        trigs = _clean_triggers(triggers)
        keys = {t.casefold() for t in trigs}
        with self._lock:
            target = next((it for it in self.items
                           if str(it.get("name") or "").casefold() == name.casefold()),
                          None)
            if target is None and keys:
                target = next((it for it in self.items
                               if keys & {str(t).casefold()
                                          for t in it.get("triggers") or []}),
                              None)
            if target is None:
                if not trigs:
                    return False           # 没有可用的触发词，存了也用不上
                self.items.append({
                    "name": name,
                    "triggers": trigs,
                    "meaning": meaning,
                    "usage": usage,
                    "source": source,
                    "learned_from": learned_from,
                    "ts": time.time(),
                    "hits": 0,
                })
            else:
                existing = {str(t).casefold() for t in target.get("triggers") or []}
                for trigger in trigs:
                    if trigger.casefold() not in existing:
                        target.setdefault("triggers", []).append(trigger)
                        existing.add(trigger.casefold())
                if len(meaning) > len(str(target.get("meaning") or "")):
                    target["meaning"] = meaning
                if len(usage) > len(str(target.get("usage") or "")):
                    target["usage"] = usage
                if not target.get("source") and source:
                    target["source"] = source
                if not target.get("learned_from") and learned_from:
                    target["learned_from"] = learned_from
            self._dirty = True
            self.flush()
        return True

    def bump(self, name: str) -> bool:
        """命中注入一次 hits+1；只标脏，不马上写盘（stop 时统一 flush）。"""
        key = _clean(name).casefold()
        if not key:
            return False
        with self._lock:
            for item in self.items:
                if str(item.get("name") or "").casefold() == key:
                    item["hits"] = int(item.get("hits") or 0) + 1
                    self._dirty = True
                    return True
        return False
