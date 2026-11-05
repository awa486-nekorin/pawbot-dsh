"""每日日記：把前一天的實際經歷合成一篇第一人稱日記，想回憶時再按需查。

日記檔在 ``content/diary/YYYY-MM-DD.md``，一天一篇，跨群、跨平台合併成
「我今天」的視角。判官掛了就用機械拼接兜底，並在檔尾標「（機械合併）」——
至少不會缺一天。

日記不再預設塞進每輪上下文：``recent`` 只保留給面板兼容；模型要回憶時用
``search`` 找命中片段，再用 ``read`` 讀必要段落。每篇的索引 / 原文 / 經歷定位
放在 ``content/diary/.index/``，舊日記歸檔到 ``content/diary/archive/``，
兩者都不改變既有 ``diary.dir`` 路徑。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta

from . import judge
from . import paths as layout
from .log import log

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MECHANICAL_MARK = "（機械合併）"
_INDEX_DIR = ".index"
_ARCHIVE_DIR = "archive"
_SNIPPET_CHARS = 160


def _parse_day(day: str) -> datetime | None:
    day = str(day or "")
    if not _DAY_RE.match(day):
        return None
    try:
        return datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def _group_of(row: dict) -> str:
    sid = str(row.get("session_id") or "")
    return sid[6:] if sid.startswith("group_") else ""


def _clean_text(value) -> str:
    return " ".join(str(value or "").split())


def _unique(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _day_bound(value) -> str:
    """把搜索的 start / end 规整成 ``YYYY-MM-DD``；认不出就当没给。"""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            return _day_of(value)
        except (TypeError, ValueError, OSError):
            return ""
    text = str(value).strip()
    if _parse_day(text):
        return text
    head = text[:10]
    if _parse_day(head):
        return head
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _people_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = re.split(r"[,，、;；\s]+", value)
    else:
        try:
            values = list(value)
        except TypeError:
            values = [value]
    return _unique(values)


def _people_match(have, wanted: list[str]) -> bool:
    if not wanted:
        return True
    hay = {str(item or "").strip().casefold() for item in (have or [])}
    hay.discard("")
    for item in wanted:
        needle = str(item or "").strip().casefold()
        if not needle:
            continue
        for value in hay:
            if needle == value or needle in value or value in needle:
                return True
    return False


def _terms(query: str) -> list[str]:
    text = _clean_text(query).casefold()
    if not text:
        return []
    parts = [part for part in re.split(r"[,，、;；:：/\\|]+", text) if part.strip()]
    return _unique(part.strip() for part in parts)


def _relevance(text: str, query: str) -> int:
    """粗略关键词相关度；0 表示不认为命中，避免把無關日記帶出來。"""
    hay = str(text or "").casefold()
    needle = _clean_text(query).casefold()
    if not needle:
        return 1
    if needle in hay:
        return 100 + hay.count(needle)
    terms = _terms(query)
    score = 0
    matched = 0
    for term in terms:
        if len(term) < 2:
            continue
        count = hay.count(term)
        if count:
            matched += 1
            score += min(count, 5) * 10
    if matched:
        return score
    # 中文自然問句不一定和日記用同一個整詞（「那次聊芙莉蓮」）。
    # 用雙字片段做兜底，但要求至少一半命中，避免只碰到一個常用詞。
    chunks: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", needle):
        chunks.extend(run[i:i + 2] for i in range(len(run) - 1))
    if chunks:
        hits = sum(1 for chunk in chunks if chunk in hay)
        if hits >= 2 and hits * 2 >= len(chunks):
            return hits
    return 0


def _snippet(text: str, query: str, limit: int = _SNIPPET_CHARS) -> str:
    body = _clean_text(text)
    if len(body) <= limit:
        return body
    needle = _clean_text(query).casefold()
    pos = body.casefold().find(needle) if needle else -1
    if pos < 0:
        return body[:max(1, limit - 1)].rstrip() + "…"
    start = max(0, pos - limit // 3)
    end = min(len(body), start + limit)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return prefix + body[start:end].rstrip() + suffix


class Diary:
    def __init__(self, base_dir: str, config, *, paths=None):
        self.base_dir = base_dir
        self.config = config
        self.paths = paths if paths is not None else layout.resolve(base_dir, config)
        self.enabled = bool(config.get("diary.enabled", True))
        self.dir = self.paths.setting(config.get("diary.dir", ""),
                                      kind="content", default="diary")

    # ------------------------------------------------------------ 路徑與格式
    def path_for(self, day: str) -> str:
        return os.path.join(self.dir, f"{day}.md")

    def _index_dir(self) -> str:
        return os.path.join(self.dir, _INDEX_DIR)

    def _index_path(self, day: str) -> str:
        return os.path.join(self._index_dir(), f"{day}.json")

    def _archive_dir(self) -> str:
        return os.path.join(self.dir, _ARCHIVE_DIR)

    def _archive_path(self, day: str) -> str:
        return os.path.join(self._archive_dir(), f"{day}.md")

    def _archive_index_path(self, day: str) -> str:
        return os.path.join(self._archive_dir(), f"{day}.json")

    def exists(self, day: str) -> bool:
        """這一天是否有日記；歸檔的也算存在（archive 透明）。"""
        if not _parse_day(day):
            return False
        return (os.path.exists(self.path_for(day))
                or os.path.exists(self._archive_path(day)))

    def _max_chars(self) -> int:
        try:
            return max(60, int(self.config.get("diary.max_chars", 500) or 500))
        except (TypeError, ValueError):
            return 500

    def _keep_days(self) -> int:
        try:
            return max(1, int(self.config.get("diary.keep_days", 60) or 60))
        except (TypeError, ValueError):
            return 60

    @staticmethod
    def _mood(bot) -> str:
        label = ""
        try:
            mood = getattr(bot, "mood", None)
            if mood is not None:
                label = str(getattr(mood, "label", "") or "")
            if not label:
                emotion = getattr(bot, "emotion", None)
                if emotion is not None:
                    label = str((emotion.blend() or {}).get("dominant") or "")
        except Exception:
            label = ""
        label = label.replace("心情", "").strip()
        return (label or "平平")[:8]

    def _header(self, bot, day: str) -> str:
        dt = _parse_day(day) or datetime.now()
        # 沒有真實天氣源就不寫天氣：以前按日期雜湊一個「晴」是編的。
        return f"# {day} {WEEKDAYS[dt.weekday()]}（心情：{self._mood(bot)}）"

    @staticmethod
    def _write_json(path: str, payload: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: str) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _read_text(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    @staticmethod
    def _stamp(now=None) -> str:
        moment = time.time() if now is None else float(now)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment))

    def _write(self, bot, day: str, body: str, mechanical: bool = False, *,
               experience_ids=None, people=None, topics=None, sources=None) -> str:
        body = _clean_text(body)[:self._max_chars()].strip()
        if not body:
            return ""
        text = self._header(bot, day) + "\n\n" + body
        if mechanical:
            text += "\n\n" + _MECHANICAL_MARK
        text += "\n"
        os.makedirs(self.dir, exist_ok=True)
        path = self.path_for(day)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

        ids = _unique(experience_ids)
        sidecar = {
            "version": 1,
            "id": day,
            "day": day,
            "written_at": time.time(),
            "text": body,
            "experience_ids": ids,
            "people": _unique(people),
            "topics": _unique(topics),
            "sources": list(sources or [])[:20],
            "mechanical": bool(mechanical),
        }
        try:
            self._write_json(self._index_path(day), sidecar)
        except OSError as e:
            log.debug(f"日記索引寫入失敗：{type(e).__name__}")
        return path

    def _append(self, bot, day: str, body: str, mechanical: bool = False, *,
                experience_ids=None, people=None, topics=None, sources=None,
                now=None) -> str:
        """帶時間補記：保住舊正文與舊索引，只追加新素材。"""
        body = _clean_text(body)[:self._max_chars()].strip()
        if not body:
            return ""
        active = os.path.exists(self.path_for(day))
        path = self.path_for(day) if active else self._archive_path(day)
        index_path = self._index_path(day) if active else self._archive_index_path(day)
        old_file = self._read_text(path)
        if not old_file:
            return self._write(bot, day, body, mechanical=mechanical,
                               experience_ids=experience_ids, people=people,
                               topics=topics, sources=sources)
        existing = self._read_json(index_path) or {}
        stamp = self._stamp(now)
        addition = f"## 补记（{stamp}）\n\n{body}"
        if mechanical:
            addition += "\n\n" + _MECHANICAL_MARK
        text = old_file.rstrip() + "\n\n" + addition + "\n"
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError as e:
            log.debug(f"日記補記失敗：{day} {type(e).__name__}")
            return ""

        old_text = _clean_text(existing.get("text"))
        if not old_text:
            old_text = _clean_text(self._read_body(old_file))
        note = f"补记（{stamp}）：{body}"
        sidecar = self._merge_meta(existing, {
            "version": existing.get("version") or 1,
            "id": day,
            "day": day,
            "written_at": time.time(),
            "text": note,
            "experience_ids": _unique(experience_ids),
            "people": _unique(people),
            "topics": _unique(topics),
            "sources": list(sources or []),
            "mechanical": bool(mechanical),
        })
        sidecar["text"] = (old_text + " " + note).strip()
        sidecar["id"] = day
        sidecar["day"] = day
        try:
            self._write_json(index_path, sidecar)
        except OSError as e:
            log.debug(f"日記索引寫入失敗：{type(e).__name__}")
        return path

    def _new_rows(self, rows: list[dict], meta: dict) -> list[dict]:
        """只挑索引裡還沒有的素材；同一份素材重跑不重複補記。"""
        existing_ids = {str(value or "").strip()
                        for value in meta.get("experience_ids") or []
                        if str(value or "").strip()}
        existing_roots = {str(source.get("root_source_id") or "").strip()
                          for source in meta.get("sources") or []
                          if isinstance(source, dict)}
        existing_roots.discard("")
        existing_sources = {self._source_signature(source)
                            for source in meta.get("sources") or []
                            if isinstance(source, dict)}
        fresh: list[dict] = []
        for row in rows:
            root = str(row.get("root_source_id") or "").strip()
            if root and root in existing_roots:
                continue
            ids = {str(row.get(key) or "").strip()
                   for key in ("root_source_id", "experience_id", "id")}
            ids.discard("")
            if ids & existing_ids:
                continue
            if not ids:
                signature = self._source_signature(row)
                if signature and signature in existing_sources:
                    continue
            fresh.append(row)
        return fresh

    # ------------------------------------------------------------ 素材
    @staticmethod
    def _row_id(row: dict) -> str:
        for key in ("experience_id", "id", "root_source_id"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _row_people(row: dict) -> list[str]:
        values = row.get("people")
        if not values:
            values = row.get("participants")
        return _people_list(values)

    @staticmethod
    def _row_topics(row: dict) -> list[str]:
        values = row.get("topics")
        if not values:
            values = row.get("keywords")
        if isinstance(values, str):
            return _people_list(values)
        return _unique(values or [])

    @staticmethod
    def _row_signature(row: dict, origin: str = "") -> str:
        """沒有 ID 時才用完整來源指紋；摘要相同但來源不同不能合併。"""
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        return "\x1f".join([
            str(origin or ""),
            str(row.get("platform") or ""),
            str(row.get("session_id") or ""),
            f"{ts:.3f}",
            _clean_text(row.get("summary")),
            "\x1e".join(Diary._row_people(row)),
            "\x1e".join(Diary._row_topics(row)),
        ])

    @classmethod
    def _row_keys(cls, row: dict, origin: str = "") -> set[str]:
        """一條經歷的一組真實來源別名：同根 / 同 ID 都算同一來源。"""
        keys: set[str] = set()
        root = str(row.get("root_source_id") or "").strip()
        if root:
            keys.add("root:" + root)
        for key in ("experience_id", "id"):
            value = str(row.get(key) or "").strip()
            if value:
                keys.add("id:" + value)
        if not keys:
            signature = cls._row_signature(row, origin)
            if signature:
                keys.add("sig:" + signature)
        return keys

    @staticmethod
    def _source_signature(source: dict) -> str:
        try:
            ts = float(source.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        return "\x1f".join([
            str(source.get("platform") or ""),
            str(source.get("session_id") or ""),
            f"{ts:.3f}",
            _clean_text(source.get("summary")),
        ])

    @classmethod
    def _source_key(cls, source: dict) -> str:
        if not isinstance(source, dict):
            return ""
        root = str(source.get("root_source_id") or "").strip()
        if root:
            return "root:" + root
        source_id = str(source.get("id") or "").strip()
        if source_id:
            return "id:" + source_id
        signature = cls._source_signature(source)
        return "sig:" + signature if signature else ""

    @classmethod
    def _merge_sources(cls, *groups) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for group in groups:
            for source in group or []:
                if not isinstance(source, dict):
                    continue
                key = cls._source_key(source)
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(dict(source))
        return out

    @classmethod
    def _merge_meta(cls, older: dict | None, newer: dict | None) -> dict:
        """合併兩份索引，保留舊正文 / 經歷 / 人物；新索引只補不能蓋。"""
        older = dict(older or {})
        newer = dict(newer or {})
        if not older:
            return newer
        out = dict(older)
        for key in ("experience_ids", "people", "topics"):
            out[key] = _unique(list(older.get(key) or []) + list(newer.get(key) or []))
        out["sources"] = cls._merge_sources(older.get("sources"), newer.get("sources"))
        old_text = _clean_text(older.get("text"))
        new_text = _clean_text(newer.get("text"))
        if new_text and new_text not in old_text:
            out["text"] = (old_text + " " + new_text).strip()
        else:
            out["text"] = old_text
        out["mechanical"] = bool(older.get("mechanical") or newer.get("mechanical"))
        try:
            out["written_at"] = max(float(older.get("written_at") or 0),
                                    float(newer.get("written_at") or 0))
        except (TypeError, ValueError):
            out["written_at"] = older.get("written_at")
        try:
            out["version"] = max(int(older.get("version") or 1),
                                 int(newer.get("version") or 1))
        except (TypeError, ValueError):
            out["version"] = older.get("version") or 1
        out["id"] = str(older.get("id") or newer.get("id") or "")
        out["day"] = str(older.get("day") or newer.get("day") or "")
        return out

    @staticmethod
    def _source_label(row: dict) -> str:
        sid = str(row.get("session_id") or "")
        if sid.startswith("group_"):
            return "群" + sid[len("group_"):]
        if sid.startswith("private_"):
            return "私聊"
        platform = str(row.get("platform") or "").strip()
        labels = {
            "bili": "B站", "bilibili": "B站",
            "qzone": "空間", "web": "網上", "website": "網上",
            "minecraft": "MC", "mc": "MC",
            "local": "本地",
        }
        return labels.get(platform.casefold(), platform or "本地")

    def _episodes_on(self, bot, day: str) -> list[dict]:
        """當天素材：舊群聊情節 + life_store 的跨平台經歷，按真实来源去重。"""
        dt = _parse_day(day)
        if dt is None:
            return []
        start = dt.timestamp()
        end = (dt + timedelta(days=1)).timestamp()
        rows: list[dict] = []
        seen_keys: set[str] = set()

        def add(row: dict, origin: str) -> None:
            if not isinstance(row, dict):
                return
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                return
            if not (start <= ts < end):
                return
            summary = _clean_text(row.get("summary"))
            if not summary:
                return
            key = self._row_keys(row, origin)
            if key & seen_keys:
                return
            item = dict(row)
            item["_origin"] = origin
            seen_keys.update(key)
            rows.append(item)

        longmem = getattr(bot, "longmem", None)
        if longmem is not None:
            for row in getattr(longmem, "episodes", []) or []:
                # 舊路徑只取群聊；私聊不進日記（與舊行為一致）。
                if not _group_of(row):
                    continue
                add(row, "longmem")

        life_store = getattr(bot, "life_store", None)
        recent = getattr(life_store, "recent_experiences", None)
        if callable(recent):
            try:
                items = recent(100, start=start, end=end)
            except Exception as e:
                items = []
                log.debug(f"日記讀取生活經歷失敗：{type(e).__name__}")
            for row in items or []:
                add(row, "life")

        rows.sort(key=lambda item: float(item.get("ts") or 0))
        return rows[-30:]

    @staticmethod
    def _mechanical(rows: list[dict]) -> str:
        """判官不可用時的兜底：按群 / 平台把當天摘要串成一句流水賬。"""
        def short(row):
            return _clean_text(row.get("summary"))[:60]

        groups: list[str] = []
        for row in rows:
            gid = _group_of(row)
            if gid and gid not in groups:
                groups.append(gid)
        parts: list[str] = []
        used: set[str] = set()
        for gid in groups[:2]:
            texts = [short(row) for row in rows if _group_of(row) == gid and short(row)][:2]
            if not texts:
                continue
            used.update(texts)
            parts.append(f"在群 {gid} 聊了" + "、".join(texts))
        rest = [row for row in rows if short(row) and short(row) not in used]
        if rest:
            first = rest[0]
            if _group_of(first):
                parts.append(f"有人提到{short(first)}")
            else:
                parts.append(f"在{Diary._source_label(first)}經歷了「{short(first)}」")
        if not parts:
            return ""
        return "今天" + "；".join(parts) + "。"

    async def _compose(self, bot, rows: list[dict], day: str) -> str:
        lines = []
        for row in rows:
            where = self._source_label(row)
            summary = _clean_text(row.get("summary"))[:200]
            reaction = _clean_text(row.get("reaction"))[:120]
            extra = f"（我當時：{reaction}）" if reaction and reaction != summary else ""
            lines.append(f"-（{where}）{summary}{extra}")
        prompt = (
            "你在替一个 QQ 群聊机器人写她自己的日记。下面是她在这一天里看到、聊过、"
            "在各平台做过的片段。\n"
            "写 3~6 句第一人称短日记：只写今天实际发生的事、自己当时的感受，"
            "不要编造没发生的事，不要写「总结」两个字，不要写人名全称，按群里的叫法来写；"
            "不要写日期和天气（开头已经有了），不要分点，不要写成报告。\n"
            "只输出 JSON：{\"diary\":\"日记正文\"}\n\n"
            "【今天的片段】\n" + "\n".join(lines)
        )
        text = ""
        try:
            result = await judge.judge_json(bot.config, prompt, timeout=30)
            if isinstance(result, dict):
                text = str(result.get("diary") or result.get("text") or "").strip()
        except Exception as e:
            log.debug(f"日记判官不可用：{type(e).__name__}")
        text = text.strip().strip("“”\"'「」").replace(_MECHANICAL_MARK, "")
        text = " ".join(part.strip() for part in text.splitlines() if part.strip())
        if len(text) < 10:
            return ""
        return text[:self._max_chars()]

    # ------------------------------------------------------------ 寫與讀
    async def write_for(self, bot, day: str) -> str:
        """把 day 當天的經歷寫成日記；沒素材或已停用返回空串。

        已存在的日記（含 archive 裡的）默認不重寫；只有索引裡還沒有的
        新素材才以帶時間的補記追加，舊正文 / 舊經歷 / 舊人物一律保留。
        """
        if not self.enabled or _parse_day(day) is None:
            return ""
        rows = self._episodes_on(bot, day)
        if self.exists(day):
            meta = self._load_meta(day)
            old_text = str((meta or {}).get("text") or self._entry_text(day) or "")
            has_index = (os.path.exists(self._index_path(day))
                         or os.path.exists(self._archive_index_path(day)))
            if meta is None or not has_index:
                return old_text
            fresh = self._new_rows(rows, meta)
            if not fresh:
                return old_text
            body = await self._compose(bot, fresh, day)
            mechanical = False
            if not body:
                body = self._mechanical(fresh)
                mechanical = True
            if not body:
                return old_text

            ids: list[str] = []
            people: list[str] = []
            topics: list[str] = []
            sources: list[dict] = []
            for row in fresh:
                rid = self._row_id(row)
                if rid:
                    ids.append(rid)
                people.extend(self._row_people(row))
                topics.extend(self._row_topics(row))
                sources.append({
                    "id": rid,
                    "root_source_id": str(row.get("root_source_id") or ""),
                    "platform": str(row.get("platform") or ""),
                    "session_id": str(row.get("session_id") or ""),
                    "ts": float(row.get("ts") or 0),
                    "summary": _clean_text(row.get("summary"))[:200],
                })
            path = self._append(bot, day, body, mechanical=mechanical,
                                experience_ids=ids, people=people, topics=topics,
                                sources=sources)
            if path:
                log.info(f"日记已补记：{day}{'（機械合併）' if mechanical else ''}")
            return body

        if not rows:
            return ""
        body = await self._compose(bot, rows, day)
        mechanical = False
        if not body:
            body = self._mechanical(rows)
            mechanical = True
        if not body:
            return ""

        ids: list[str] = []
        people: list[str] = []
        topics: list[str] = []
        sources: list[dict] = []
        for row in rows:
            rid = self._row_id(row)
            if rid:
                ids.append(rid)
            people.extend(self._row_people(row))
            topics.extend(self._row_topics(row))
            sources.append({
                "id": rid,
                "platform": str(row.get("platform") or ""),
                "session_id": str(row.get("session_id") or ""),
                "ts": float(row.get("ts") or 0),
                "summary": _clean_text(row.get("summary"))[:200],
            })

        path = self._write(bot, day, body, mechanical=mechanical,
                           experience_ids=ids, people=people, topics=topics,
                           sources=sources)
        if path:
            log.info(f"日记已写：{day}{'（機械合併）' if mechanical else ''}")
        return body

    @staticmethod
    def _read_body(text: str) -> str:
        lines = []
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped == _MECHANICAL_MARK:
                continue
            if stripped.startswith("# "):
                continue
            lines.append(stripped)
        return " ".join(lines)

    def _entry_text(self, day: str) -> str:
        for path in (self.path_for(day), self._archive_path(day)):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return self._read_body(f.read())
            except OSError:
                continue
        return ""

    def _load_meta(self, day: str) -> dict | None:
        """讀索引；沒有索引的舊日記也照樣能搜，只是缺經歷定位。"""
        if not _parse_day(day):
            return None
        meta = self._read_json(self._index_path(day))
        if meta is None:
            meta = self._read_json(self._archive_index_path(day))
        text = self._entry_text(day)
        if not text and meta is not None:
            text = _clean_text(meta.get("text"))
        active = os.path.exists(self.path_for(day))
        archived = os.path.exists(self._archive_path(day))
        if meta is None and not text and not active and not archived:
            return None
        meta = dict(meta or {})
        meta["id"] = str(meta.get("id") or day)
        meta["day"] = day
        meta["text"] = text or _clean_text(meta.get("text"))
        meta["experience_ids"] = _unique(meta.get("experience_ids") or [])
        meta["people"] = _unique(meta.get("people") or [])
        meta["topics"] = _unique(meta.get("topics") or [])
        meta["archived"] = bool(archived and not active)
        return meta

    def _available_days(self) -> list[str]:
        days: set[str] = set()
        for folder in (self.dir, self._archive_dir()):
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".md"):
                    continue
                day = name[:-3]
                if _parse_day(day):
                    days.add(day)
        return sorted(days)

    def search(self, query: str, *, start="", end="", people=None,
               limit: int = 5) -> list[dict]:
        """按主題 / 人物 / 時間找日記，只返回命中片段，不返回全文。"""
        if not self.enabled:
            return []
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        limit = max(0, min(limit, 50))
        if limit == 0:
            return []
        start_day = _day_bound(start)
        end_day = _day_bound(end)
        wanted = _people_list(people)
        query = str(query or "").strip()

        hits: list[tuple[int, str, dict]] = []
        for day in self._available_days():
            if start_day and day < start_day:
                continue
            if end_day and day > end_day:
                continue
            meta = self._load_meta(day)
            if meta is None:
                continue
            if wanted and not _people_match(meta.get("people"), wanted):
                continue
            body = meta.get("text") or ""
            haystack = " ".join([
                body,
                " ".join(meta.get("people") or []),
                " ".join(meta.get("topics") or []),
            ])
            score = _relevance(haystack, query)
            if score <= 0:
                continue
            hits.append((score, day, {
                "id": day,
                "day": day,
                "snippet": _snippet(body, query),
                "experience_ids": list(meta.get("experience_ids") or []),
            }))

        hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in hits[:limit]]

    def read(self, entry_id: str, *, offset: int = 0, limit: int = 1800) -> dict:
        """按日記 ID 讀原文；可用 offset / limit 繼續往後翻。"""
        raw = str(entry_id or "").strip()
        if raw.startswith("diary:"):
            raw = raw[len("diary:"):].strip()
        day = raw.split("#", 1)[0].strip()
        empty = {
            "ok": False,
            "text": "",
            "next_offset": None,
            "truncated": False,
            "day": day if _parse_day(day) else "",
            "experience_ids": [],
        }
        if not self.enabled or not _parse_day(day):
            return empty
        meta = self._load_meta(day)
        if meta is None:
            return empty
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(limit or 1800)
        except (TypeError, ValueError):
            limit = 1800
        limit = max(1, min(limit, 100000))
        text = meta.get("text") or ""
        chunk = text[offset:offset + limit]
        truncated = offset + len(chunk) < len(text)
        return {
            "ok": True,
            "text": chunk,
            "next_offset": (offset + len(chunk)) if truncated else None,
            "truncated": truncated,
            "day": day,
            "experience_ids": list(meta.get("experience_ids") or []),
        }

    def recent(self, n: int = 2) -> list[tuple[str, str]]:
        """讀最近 n 天日記（跳過今天），新的在前。保留給舊面板兼容。"""
        n = max(0, int(n or 0))
        if not self.enabled or n == 0:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        out: list[tuple[str, str]] = []
        for day in sorted((d for d in self._available_days() if d < today), reverse=True):
            body = self._entry_text(day)[:self._max_chars()]
            if body:
                out.append((day, body))
            if len(out) >= n:
                break
        return out

    async def maybe_write_yesterday(self, bot, now: float | None = None) -> str:
        """昨天的日記還不存在、又有昨天的經歷，就補寫一篇。"""
        if not self.enabled:
            return ""
        current = time.time() if now is None else float(now)
        yesterday = _day_of(current - 86400)
        if self.exists(yesterday) or not self._episodes_on(bot, yesterday):
            return ""
        self.prune(current)
        return await self.write_for(bot, yesterday)

    def _archive_day(self, day: str) -> bool:
        source = self.path_for(day)
        if not os.path.exists(source):
            return False
        archive = self._archive_path(day)
        source_text = self._read_text(source)
        archive_text = self._read_text(archive)
        try:
            os.makedirs(self._archive_dir(), exist_ok=True)
            if archive_text:
                # 歸檔目標已存在：合併成一份帶時間的補記，絕不覆蓋舊檔。
                if not source_text:
                    return False
                body = self._read_body(source_text)
                merged = archive_text.rstrip()
                if body:
                    merged += f"\n\n## 归档补记（{self._stamp()}）\n\n{body}"
                merged += "\n"
                tmp = archive + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(merged)
                os.replace(tmp, archive)
            else:
                os.replace(source, archive)
        except OSError as e:
            log.debug(f"日記歸檔失敗：{day} {type(e).__name__}")
            return False

        active_index = self._index_path(day)
        archive_index = self._archive_index_path(day)
        source_meta = self._read_json(active_index)
        archive_meta = self._read_json(archive_index)
        if archive_meta is None and archive_text:
            archive_meta = {"text": _clean_text(self._read_body(archive_text))}
        if source_meta is not None or archive_meta is not None:
            merged_meta = self._merge_meta(archive_meta, source_meta)
            merged_meta["id"] = day
            merged_meta["day"] = day
            if not merged_meta.get("text"):
                merged_meta["text"] = _clean_text(
                    self._read_body(self._read_text(archive)))
            try:
                self._write_json(archive_index, merged_meta)
            except OSError as e:
                log.debug(f"日記索引歸檔失敗：{day} {type(e).__name__}")
                # 索引沒合併成功就留下原索引，至少不丟出處。
                return True

        for path in (source, active_index):
            if not os.path.exists(path):
                continue
            try:
                os.remove(path)
            except OSError as e:
                log.debug(f"日記歸檔清理失敗：{type(e).__name__}")
        return True

    def prune(self, now: float | None = None) -> int:
        """把保留期之外的日記和索引移到 archive/，返回歸檔篇數。

        只移動日記文本與索引，不刪除任何經歷；目錄不存在時也不創建。
        """
        current = time.time() if now is None else float(now)
        cutoff = datetime.fromtimestamp(current - self._keep_days() * 86400)
        cutoff_day = cutoff.strftime("%Y-%m-%d")
        try:
            names = [name[:-3] for name in os.listdir(self.dir) if name.endswith(".md")]
        except OSError:
            return 0
        archived = 0
        for day in sorted(names):
            if _parse_day(day) and day < cutoff_day and self._archive_day(day):
                archived += 1
        return archived
