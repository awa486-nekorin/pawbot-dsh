"""跨会话经历记忆：逐轮追加、按日合并与相关度检索。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

from . import judge
from .log import log
from .memory import _tokens

# 中文二元组切分会切出大量「什么」「意思」「你们」这种虚词片段，
# 拿它们打分会让任何一句问话都撞上一堆无关的梗
_WEAK_TOKENS = frozenset("""
什么 意思 啥意 是啥 你们 我们 他们 她们 这个 那个 怎么 为什 一下 一个 是不 不是
可以 还是 就是 但是 然后 因为 所以 如果 的话 一点 有点 没有 知道 觉得 现在 今天
说的 说什 帮我 给我 我的 你的 这是 那是 好的 好吧 吗吗 了吗 呢呢
""".split())


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts or time.time())).strftime("%Y-%m-%d")


def _source(session_id: str) -> str:
    sid = str(session_id or "")
    if sid.startswith("group_"):
        return f"在 QQ 群 {sid[6:]}"
    if sid.startswith("private_"):
        return "私聊"
    return f"在會話 {sid}" if sid else "在其他會話"


def _clean_keywords(values, text: str = "") -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        values = []
    out = []
    seen = set()
    for value in list(values) + sorted(_tokens(text)):
        word = " ".join(str(value or "").split()).strip()[:30]
        key = word.casefold()
        if word and key not in seen:
            seen.add(key)
            out.append(word)
        if len(out) >= 16:
            break
    return out


class LongMemory:
    def __init__(self, base_dir: str, config, memory=None):
        self.base_dir = base_dir
        self.config = config
        self.memory = memory
        self.enabled = bool(config.get("longmem.enabled", True))
        if memory is not None:
            memory.longmem = self
        directory = config.get("longmem.dir", "data/longmem")
        self.dir = directory if os.path.isabs(directory) else os.path.join(base_dir, directory)
        self.path = os.path.join(self.dir, "episodes.jsonl")
        self.episodes: list[dict] = []
        self._lock = asyncio.Lock()
        self.episodes = self.load()

    def _cfg(self, key: str, default):
        return self.config.get(f"longmem.{key}", default)

    def load(self) -> list[dict]:
        rows = []
        if not os.path.exists(self.path):
            return rows
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(row, dict) and row.get("summary"):
                        rows.append(row)
        except OSError as e:
            log.warn(f"長期記憶載入失敗：{type(e).__name__}")
        cap = max(1, int(self._cfg("max_episodes", 5000) or 5000))
        return rows[-cap:]

    def drop_session(self, session_id: str) -> int:
        """忘掉某个会话的全部经历（WebUI「清空长期经历」用），返回删了几条。"""
        sid = str(session_id or "")
        if not sid:
            return 0
        keep = [row for row in self.episodes
                if str(row.get("session_id") or "") != sid]
        removed = len(self.episodes) - len(keep)
        if removed:
            self.episodes = keep
            self._write_all()
            log.info(f"長期記憶已清空 {sid}：{removed} 条")
        return removed

    def _write_all(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for row in self.episodes:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, self.path)

    def _append(self, row: dict):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.episodes.append(row)

    @staticmethod
    def _mechanical(rows: list[dict]) -> str:
        pieces = []
        for row in rows:
            text = " ".join(str(row.get("summary") or "").split())
            if text:
                pieces.append(text[:160])
        return "；".join(pieces)[:700]

    async def _daily_digest(self, bot, rows: list[dict], day: str) -> dict:
        summaries = [str(row.get("summary") or "") for row in rows if row.get("summary")]
        joined = "\n".join(summaries)
        text = ""
        try:
            result = await judge.judge_json(
                bot.config,
                "把同一天较早的对话经历合成一条日记摘要，保留重要事实、约定、人物和情绪，不要重复。"
                "只输出 JSON：{\"summary\":\"摘要\"}\n\n" + joined,
                timeout=float(bot.config.get("chat.turn_summary_timeout", 20)))
            if isinstance(result, dict):
                text = str(result.get("summary") or "").strip()
        except Exception as e:
            log.debug(f"日记摘要判官不可用：{type(e).__name__}")
        if not text:
            text = self._mechanical(rows)
        participants = sorted({str(uid) for row in rows
                               for uid in row.get("participants", []) if str(uid)})
        keywords = _clean_keywords([word for row in rows
                                    for word in row.get("keywords", [])], text)
        return {
            "ts": min(float(row.get("ts") or time.time()) for row in rows),
            "session_id": str(rows[0].get("session_id") or ""),
            "participants": participants,
            "summary": text[:700],
            "keywords": keywords,
            "importance": max([int(row.get("importance") or 1) for row in rows] or [1]),
            "day": day,
            "daily_digest": True,
        }

    async def _maintain(self, bot):
        now = time.time()
        threshold = max(2, int(self._cfg("daily_merge_threshold", 12) or 12))
        keep_days = max(1, int(self._cfg("keep_days", 30) or 30))
        changed = False

        # 保留期外只保留每個 session/day 的日摘要。
        cutoff = now - keep_days * 86400
        groups: dict[tuple[str, str], list[dict]] = {}
        fresh = []
        for row in self.episodes:
            day = str(row.get("day") or _day(float(row.get("ts") or now)))
            ts = float(row.get("ts") or now)
            if ts < cutoff:
                groups.setdefault((str(row.get("session_id") or ""), day), []).append(row)
                changed = True
            else:
                fresh.append(row)
        for (sid, day), rows in groups.items():
            if len(rows) == 1 and rows[0].get("daily_digest"):
                fresh.append(rows[0])
                continue
            digest = await self._daily_digest(bot, rows, day)
            digest["session_id"] = sid
            fresh.append(digest)
        self.episodes = fresh

        # 同一會話同一天超過門檻時，將較早內容併成一條日摘要。
        groups = {}
        for row in self.episodes:
            day = str(row.get("day") or _day(float(row.get("ts") or now)))
            groups.setdefault((str(row.get("session_id") or ""), day), []).append(row)
        for (sid, day), rows in groups.items():
            if len(rows) <= threshold:
                continue
            rows.sort(key=lambda item: float(item.get("ts") or 0))
            digests = [row for row in rows if row.get("daily_digest")]
            regular = [row for row in rows if not row.get("daily_digest")]
            merge_count = len(regular) - max(0, threshold - len(digests)) + 1
            if merge_count <= 0:
                continue
            earlier = regular[:merge_count]
            digest_rows = digests + earlier
            digest = await self._daily_digest(bot, digest_rows, day)
            digest["session_id"] = sid
            remaining = regular[merge_count:]
            group_rows = set(id(row) for row in rows)
            self.episodes = [row for row in self.episodes if id(row) not in group_rows]
            self.episodes.extend([digest, *remaining])
            changed = True

        cap = max(1, int(self._cfg("max_episodes", 5000) or 5000))
        if len(self.episodes) > cap:
            self.episodes.sort(key=lambda row: float(row.get("ts") or 0))
            self.episodes = self.episodes[-cap:]
            changed = True
        if changed:
            self.episodes.sort(key=lambda row: float(row.get("ts") or 0))
            self._write_all()

    async def add_episode(self, bot, *, ts: float, session_id: str,
                          participants: list[str], summary: str,
                          keywords: list[str] | None = None, importance: int = 2):
        if not self._cfg("enabled", True) or not summary.strip():
            return
        row = {
            "ts": float(ts or time.time()),
            "session_id": str(session_id),
            "participants": sorted({str(uid) for uid in participants if str(uid)}),
            "summary": str(summary).strip()[:700],
            "keywords": _clean_keywords(keywords, summary),
            "importance": max(1, min(5, int(importance or 2))),
            "day": _day(float(ts or time.time())),
        }
        async with self._lock:
            self._append(row)
            await self._maintain(bot)

    async def maintain(self, bot):
        """啟動時清理過期逐輪記憶，不建立額外事件。"""
        if not self.enabled or not self._cfg("enabled", True):
            return
        async with self._lock:
            await self._maintain(bot)

    @staticmethod
    def _episode_score(row: dict, query_tokens: set[str], now: float,
                       half_life_days: float, user_id: str, session_id: str) -> float:
        words = set(str(w) for w in row.get("keywords", [])) | _tokens(str(row.get("summary") or ""))
        overlap = len((query_tokens & words) - _WEAK_TOKENS)
        if not overlap:
            return 0.0
        age_days = max(0.0, now - float(row.get("ts") or now)) / 86400
        decay = 0.5 ** (age_days / max(0.01, half_life_days))
        importance = max(1, min(5, int(row.get("importance") or 1)))
        score = overlap * decay * (1 + importance / 5)
        if user_id and user_id in {str(uid) for uid in row.get("participants", [])}:
            score += 1.5
        if session_id and str(row.get("session_id") or "") == session_id:
            score += 0.5
        return score

    def _fact_candidates(self, query_tokens: set[str], now: float, half_life_days: float,
                         kind: str | None = None):
        if self.memory is None:
            return []
        from .memory import FILES
        candidates = []
        for item_kind in FILES:
            if kind and item_kind != kind:
                continue
            for line in self.memory.all_lines(item_kind):
                body = line.lstrip("-* ").strip()
                parsed = None
                if body.startswith("[") and "]" in body:
                    date_text = body[1:body.index("]")]
                    try:
                        parsed = datetime.strptime(date_text, "%Y-%m-%d").timestamp()
                        body = body[body.index("]") + 1:].strip()
                    except ValueError:
                        pass
                age_days = max(0.0, now - parsed) / 86400 if parsed else 0.0
                decay = 0.5 ** (age_days / max(0.01, half_life_days))
                hits = query_tokens & _tokens(body)
                # 只撞上一个「意思」「什么」这种虚词二元组不算相关；
                # 但查询本身就是一个实词（例如「熬夜」）时，一个命中就够
                overlap = len(hits - _WEAK_TOKENS)
                need = 1 if len(query_tokens - _WEAK_TOKENS) <= 2 else 2
                if overlap >= need:
                    score = overlap * decay * (1 + 2 / 5)
                    candidates.append({"score": score, "text": body,
                                       "source": f"长期资料/{item_kind}", "key": f"fact:{item_kind}:{body}",
                                       "kind": item_kind})
        return candidates

    def recall_for(self, ctx, text: str, k: int = 6,
                   fact_kind: str | None = None) -> list[dict]:
        """按話題、人物與時間挑選跨會話經歷及純文字事實記憶。"""
        if not self.enabled or not self._cfg("enabled", True):
            return []
        query_tokens = _tokens(text)
        if not query_tokens:
            return []
        now = time.time()
        half_life = float(self._cfg("half_life_days", 7) or 7)
        user_id = str(getattr(ctx, "user_id", "") or "")
        session_id = str(getattr(ctx, "session_id", "") or "")
        candidates = self._fact_candidates(query_tokens, now, half_life, fact_kind)
        if not fact_kind:
            for row in self.episodes:
                score = self._episode_score(row, query_tokens, now, half_life, user_id, session_id)
                if score:
                    sid = str(row.get("session_id") or "")
                    text_value = str(row.get("summary") or "").strip()
                    key = "episode:" + " ".join(text_value.split()).casefold()
                    candidates.append({"score": score, "text": text_value,
                                       "source": _source(sid), "key": key,
                                       "kind": "episode"})
        candidates.sort(key=lambda item: (-item["score"], item["key"]))
        # 保底：話題完全對不上時，至少撈一條跟這個人相關的經歷。
        # 真人也不是只有被問到才想起朋友的事，聊著聊著會自己冒出來。
        if not fact_kind and user_id and len(candidates) < max(1, int(k or 6)):
            picked_keys = {c["key"] for c in candidates}
            for row in sorted(self.episodes,
                              key=lambda r: float(r.get("ts") or 0), reverse=True):
                if str(row.get("session_id") or "") == session_id:
                    continue
                if user_id not in {str(uid) for uid in row.get("participants", [])}:
                    continue
                text_value = str(row.get("summary") or "").strip()
                key = "episode:" + " ".join(text_value.split()).casefold()
                if not text_value or key in picked_keys:
                    continue
                candidates.append({"score": 0.01, "text": text_value,
                                   "source": _source(str(row.get("session_id") or "")),
                                   "key": key, "kind": "episode"})
                break
        picked = []
        seen = set()
        max_chars = max(1, int(self._cfg("max_chars", 900) or 900))
        budget = max_chars
        for item in candidates:
            if item["key"] in seen:
                continue
            seen.add(item["key"])
            line = f"{item['text']}（{item['source']}）"
            if len(line) > budget:
                line = line[:budget]
            if not line:
                break
            picked.append({**item, "text": line})
            budget -= len(line) + 1
            if len(picked) >= max(1, int(k or self._cfg("top_k", 6))):
                break
            if budget <= 0:
                break
        return picked

    def format_recall(self, ctx, text: str, k: int = 6) -> str:
        entries = self.recall_for(ctx, text, k)
        if not entries:
            return ""
        lines = ["## 你想起来的（内部记忆，不要复述来源、不要说「我记得档案里…」）"]
        lines.extend(f"- {entry['text']}" for entry in entries)
        lines.append("别把别的群的私事说出来，只用来理解对方。")
        return "\n".join(lines)
