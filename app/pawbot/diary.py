"""每日日記：把前一天的群聊經歷合成一篇第一人稱日記，隔天還能自然想起。

日記檔在 ``data/diary/YYYY-MM-DD.md``，一天一篇，跨群合併成「我今天」的視角。
判官掛了就用機械拼接兜底，並在檔尾標「（機械合併）」——至少不會缺一天。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta

from . import judge
from .log import log

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WEATHERS = ("晴", "多云", "阴", "小雨", "晴转多云")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MECHANICAL_MARK = "（機械合併）"


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


def _weather(day: str) -> str:
    """没有天气源，用日期算一个稳定的天气，至少同一天每篇一致。"""
    return _WEATHERS[sum(ord(ch) for ch in day) % len(_WEATHERS)]


def _group_of(row: dict) -> str:
    sid = str(row.get("session_id") or "")
    return sid[6:] if sid.startswith("group_") else ""


class Diary:
    def __init__(self, base_dir: str, config):
        self.base_dir = base_dir
        self.config = config
        self.enabled = bool(config.get("diary.enabled", True))
        directory = config.get("diary.dir", "data/diary")
        self.dir = directory if os.path.isabs(directory) else os.path.join(base_dir, directory)

    # ------------------------------------------------------------ 路径与格式
    def path_for(self, day: str) -> str:
        return os.path.join(self.dir, f"{day}.md")

    def exists(self, day: str) -> bool:
        return bool(_parse_day(day)) and os.path.exists(self.path_for(day))

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
        return (f"# {day} {WEEKDAYS[dt.weekday()]} {_weather(day)}"
                f"（心情：{self._mood(bot)}）")

    def _write(self, bot, day: str, body: str, mechanical: bool = False) -> str:
        body = " ".join(str(body or "").split())[:self._max_chars()].strip()
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
        return path

    # ------------------------------------------------------------ 素材
    def _episodes_on(self, bot, day: str) -> list[dict]:
        """当天该群（group_*）的经历，按时间排序，最多取最近 30 条。"""
        dt = _parse_day(day)
        longmem = getattr(bot, "longmem", None)
        if dt is None or longmem is None:
            return []
        start = dt.timestamp()
        end = (dt + timedelta(days=1)).timestamp()
        rows = []
        for row in getattr(longmem, "episodes", []) or []:
            if not _group_of(row):
                continue
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if start <= ts < end and str(row.get("summary") or "").strip():
                rows.append(row)
        rows.sort(key=lambda item: float(item.get("ts") or 0))
        return rows[-30:]

    @staticmethod
    def _mechanical(rows: list[dict]) -> str:
        """判官不可用时的兜底：按群把当天的摘要串成一句流水账。"""
        def short(row):
            return " ".join(str(row.get("summary") or "").split())[:60]

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
        rest = [short(row) for row in rows if short(row) and short(row) not in used]
        if rest:
            parts.append(f"有人提到{rest[0]}")
        if not parts:
            return ""
        return "今天" + "；".join(parts) + "。"

    async def _compose(self, bot, rows: list[dict], day: str) -> str:
        lines = []
        for row in rows:
            gid = _group_of(row)
            where = f"群{gid}" if gid else "私聊"
            lines.append(f"-（{where}）{' '.join(str(row.get('summary') or '').split())[:200]}")
        prompt = (
            "你在替一个 QQ 群聊机器人写她自己的日记。下面是她在这一天里看到、聊过的片段。\n"
            "写 3~6 句第一人称短日记：只写今天在群里聊了什么、自己当时的感受，"
            "不要编造没发生的事，不要写「总结」两个字，不要写人名全称，按群里的叫法来写；"
            "不要写日期和天气（开头已经有了），不要分点，不要写成报告。\n"
            "只输出 JSON：{\"diary\":\"日记正文\"}\n\n"
            "【今天的群聊片段】\n" + "\n".join(lines)
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

    # ------------------------------------------------------------ 写与读
    async def write_for(self, bot, day: str) -> str:
        """把 day 当天的群聊写成日记；没素材或已停用返回空串。"""
        if not self.enabled or _parse_day(day) is None:
            return ""
        rows = self._episodes_on(bot, day)
        if not rows:
            return ""
        body = await self._compose(bot, rows, day)
        mechanical = False
        if not body:
            body = self._mechanical(rows)
            mechanical = True
        if not body:
            return ""
        path = self._write(bot, day, body, mechanical=mechanical)
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

    def recent(self, n: int = 2) -> list[tuple[str, str]]:
        """读最近 n 天日记（跳过今天），新的在前，返回 [(day, 正文)]。"""
        n = max(0, int(n or 0))
        if not self.enabled or n == 0:
            return []
        try:
            names = [name[:-3] for name in os.listdir(self.dir) if name.endswith(".md")]
        except OSError:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        out: list[tuple[str, str]] = []
        for day in sorted((d for d in names if _parse_day(d) and d < today), reverse=True):
            try:
                with open(self.path_for(day), "r", encoding="utf-8") as f:
                    body = self._read_body(f.read())[:self._max_chars()]
            except OSError:
                continue
            if body:
                out.append((day, body))
            if len(out) >= n:
                break
        return out

    async def maybe_write_yesterday(self, bot, now: float | None = None) -> str:
        """昨天的日记还不存在、又有昨天的经历，就补写一篇。"""
        if not self.enabled:
            return ""
        current = time.time() if now is None else float(now)
        yesterday = _day_of(current - 86400)
        if self.exists(yesterday) or not self._episodes_on(bot, yesterday):
            return ""
        self.prune(current)
        return await self.write_for(bot, yesterday)

    def prune(self, now: float | None = None) -> int:
        """删掉保留期之外的日记文件，返回删除数量。"""
        current = time.time() if now is None else float(now)
        cutoff = datetime.fromtimestamp(current - self._keep_days() * 86400)
        cutoff_day = cutoff.strftime("%Y-%m-%d")
        try:
            names = [name[:-3] for name in os.listdir(self.dir) if name.endswith(".md")]
        except OSError:
            return 0
        removed = 0
        for day in names:
            if _parse_day(day) and day < cutoff_day:
                try:
                    os.remove(self.path_for(day))
                    removed += 1
                except OSError:
                    pass
        return removed
