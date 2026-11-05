"""
记忆区：可读、可写、可检索的纯文本记忆。

    content/memory/facts.md        事实（"主人在准备数学考试"）
    content/memory/slang.md        网络用词（"绝绝子 = 太……了"）
    content/memory/impression.md   对群友的印象（"阿明：爱问技术问题"）

检索方式很朴素：按字/词重叠打分。够用，而且你打开文件就能看懂它在记什么。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime
from types import SimpleNamespace

from . import paths as layout
from .persona import append_line

FILES = {
    "fact": ("facts.md", "# 长期记忆\n"),
    "slang": ("slang.md", "# 网络用词 / 梗\n"),
    "impression": ("impression.md", "# 对群友的印象\n"),
    "people": ("people.md", "# 人物卡\n"),
}


class Memory:
    def __init__(self, base_dir: str, config, *, paths=None):
        self.base_dir = base_dir
        self.config = config
        self.paths = paths if paths is not None else layout.resolve(base_dir, config)
        self.longmem = None
        # core 在 LifeStore 构造后挂上；没有时保持旧纯文本行为。
        self.life_store = None
        self.dir = self.paths.setting(self.config.get("persona.memory_dir", ""),
                                      kind="content", default="memory")

    def path(self, kind: str) -> str:
        name, _ = FILES.get(kind, FILES["fact"])
        return os.path.join(self.dir, name)

    # ---------------------------------------------------------- 写
    @staticmethod
    def _subject_for(kind: str, text: str) -> str:
        """旧文本没有主体字段：人物/印象按「名字：内容」拆，其余才落到 self。"""
        body = re.sub(r"^[-*]\s*(\[[^\]]+\]\s*)?", "", str(text or "")).strip()
        if kind in ("people", "impression"):
            head = body.split("：", 1)[0].split(":", 1)[0].strip()
            if head and len(head) <= 24 and head != body:
                return f"person:{head}"
        # 「阿明 约好……」这类明确以人名开头的约定也归到那个人；普通事实才 self。
        m = re.match(r"^([\u4e00-\u9fffA-Za-z0-9_\-]{1,16})(?:约好|说|喜欢|讨厌|在|要)", body)
        if m and m.group(1) not in {"约定", "记住", "今天", "昨天", "明天", "主人"}:
            return f"person:{m.group(1)}"
        return "self"

    @staticmethod
    def _life_kind(kind: str) -> str:
        # 旧四类都归到语义记忆；程序/情景经验由 life_store 自己的入口写。
        return "semantic"

    def remember(self, text: str, kind: str = "fact", *,
                 subject: str | None = None,
                 source_ids=None, confirmed: bool = False,
                 confidence: float = 0.6, salience: int = 3) -> bool:
        """优先写 life_store（带主体/来源/候选状态），旧文件只在无 store 时兜底。"""
        text = str(text or "").strip().replace("\n", " ")
        if not text:
            return False
        store = getattr(self, "life_store", None)
        if store is not None:
            try:
                store.remember(
                    text,
                    kind=self._life_kind(kind),
                    subject=subject or self._subject_for(kind, text),
                    source_ids=source_ids,
                    confidence=confidence,
                    salience=salience,
                    confirmed=confirmed,
                )
                return True
            except Exception as e:
                # 写生活库失败时不要静默丢掉：退回旧纯文本路径，保证还记下来。
                from .log import log
                log.warn(f"生活记忆写入失败，退回旧记忆文件：{type(e).__name__} {e}")
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

    @staticmethod
    def _stable_legacy_id(kind: str, body: str) -> str:
        digest = hashlib.sha1(f"{kind}:{body}".encode("utf-8")).hexdigest()[:16]
        return f"legacy:{kind}:{digest}"

    def _migrate_legacy_line(self, kind: str, line: str) -> None:
        """把命中的旧条目作为低置信候选导入 life_store（稳定来源 ID、按主体）。"""
        store = getattr(self, "life_store", None)
        if store is None:
            return
        body = re.sub(r"^[-*]\s*(\[[^\]]+\]\s*)?", "", line).strip()
        if not body:
            return
        source_id = self._stable_legacy_id(kind, body)
        try:
            experience = store.record_experience(
                source_id, body, platform="legacy", kind="legacy_import",
                confidence=0.3, salience=2)
            store.remember(
                body, kind=self._life_kind(kind),
                subject=self._subject_for(kind, line),
                source_ids=[experience.get("id") or source_id],
                confidence=0.3, salience=2, confirmed=False)
        except Exception:
            # 迁移失败不影响本轮检索；旧文本仍然会作为命中返回。
            pass

    def _legacy_scored(self, query: str, kinds: list[str]) -> list[tuple[float, int, str, str]]:
        tokens = _tokens(query)
        if not tokens:
            return []
        now = time.time()
        scored: list[tuple[float, float, str, str]] = []
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
                scored.append((score, ts or index, k, body))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [(score, ts, k, body) for score, ts, k, body in scored]

    @staticmethod
    def _legacy_row_tag(row: dict) -> str:
        """迁移来的旧条目仍标明「未验证」，但只在它当前有效时才会出现。"""
        roots = [str(x) for x in (row.get("evidence_roots") or [])]
        roots += [str(x) for x in (row.get("source_ids") or [])]
        if any(root.startswith("legacy:") for root in roots):
            return "旧记忆·未验证"
        if row.get("status") == "candidate":
            return "候选·待确认"
        return str(row.get("status") or "memory")

    def _life_recall_rows(self, query: str, limit: int) -> list[dict]:
        store = getattr(self, "life_store", None)
        if store is None:
            return []
        try:
            return list(store.recall(query, limit=limit) or [])
        except Exception:
            return []

    def recall(self, query: str, kind: str | None = None, limit: int = 5) -> list[str]:
        """先查 life_store；旧文本只按 query 迁移后再走同一召回，绝不直接回灌旧原文。

        ``superseded / conflicted / retired`` 的旧事实只留在 LifeStore 的状态里，
        不再作为「事实」重新出现在这里。没有 LifeStore 时才保留旧纯文本兜底。
        """
        kinds = [kind] if kind else list(FILES)
        count = max(1, int(limit or 5))
        out: list[str] = []
        seen: set[str] = set()

        def add_rows(rows) -> None:
            for row in rows:
                if len(out) >= count:
                    break
                body = str(row.get("text") or "").strip()
                key = re.sub(r"\s+", "", body).casefold()
                if not body or key in seen:
                    continue
                seen.add(key)
                tag = self._legacy_row_tag(row)
                out.append(f"[{row.get('id')}|{tag}|{row.get('subject')}] {body}")

        store = getattr(self, "life_store", None)
        if store is not None:
            # 1) 先拿当前有效召回。
            add_rows(self._life_recall_rows(query, count))

            # 2) 再把本轮 query 命中的旧文本按稳定 ID 迁移到 LifeStore。
            #    迁移本身不增加证据，也不改变旧条目的冲突/退役状态。
            legacy_hits = self._legacy_scored(query, kinds)
            if legacy_hits:
                for _score, _ts, k, body in legacy_hits[:20]:
                    self._migrate_legacy_line(k, body)
                # 3) 迁移后重新召回；只有仍然有效的条目能进入事实上下文。
                add_rows(self._life_recall_rows(query, count))
            return out[:count]

        # 没有 LifeStore：保持旧的纯文本 + longmem 兜底。
        for _score, _ts, k, body in self._legacy_scored(query, kinds):
            if len(out) >= count:
                break
            key = re.sub(r"\s+", "", body).casefold()
            if key in seen:
                continue
            seen.add(key)
            legacy_id = self._stable_legacy_id(
                k, re.sub(r"^[-*]\s*(\[[^\]]+\]\s*)?", "", body).strip())
            out.append(f"[{legacy_id}|旧记忆·未验证] [{k}] {body}")
        if self.longmem is not None and not out:
            entries = self.longmem.recall_for(SimpleNamespace(user_id=0, session_id=""),
                                              query, k=count, fact_kind=kind)
            return [f"[{entry.get('kind') or 'episode'}] {entry['text']}"
                    for entry in entries[:count]]
        return out[:count]


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
