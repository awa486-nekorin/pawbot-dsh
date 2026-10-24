"""约定提醒：把「明天提醒我带学生证」变成真的到点 @ 他。

两块东西：

  · ReminderStore —— 一份 JSON 待办（``data/reminders.json``），
    RLock + 临时文件 + ``os.replace`` 原子落盘，重启后还在；
  · ReminderService —— 后台每 poll_sec 秒查一次到点，用 Context 发消息，
    失败 60 秒后重试，连续 3 次放弃，半夜不吵人。

时间解析（``parse_when``）故意只认中文口语里常见的那几种写法，
认不出来就老实返回 None——宁可让模型问一句，也不自己猜时间。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, time as dtime, timedelta

from . import message as msg
from . import tasks
from .context import Context
from .log import log

# ====================================================================== 时间解析
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "兩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_NUM = "零〇一二两兩三四五六七八九十百"
_NUM = rf"(?:\d{{1,3}}|[{_CN_NUM}]{{1,4}})"

_FULLWIDTH = str.maketrans("０１２３４５６７８９：／　", "0123456789:/ ")

# 相对时间：3天後 / 两小时后 / 半小時後 / 一週後
_REL_RE = re.compile(
    rf"(半(?:个|個)?|{_NUM})\s*(?:(?:个|個)\s*)?"
    r"(秒钟|秒鐘|秒|分钟|分鐘|分|小时|小時|钟头|鐘頭|天|日|周|週|星期|礼拜|禮拜|个月|個月)"
    r"\s*(?:之?[后後]|以后|以後)"
)

_UNIT_SECONDS = {
    "秒": 1, "秒钟": 1, "秒鐘": 1,
    "分": 60, "分钟": 60, "分鐘": 60,
    "小时": 3600, "小時": 3600, "钟头": 3600, "鐘頭": 3600,
    "天": 86400, "日": 86400,
    "周": 604800, "週": 604800, "星期": 604800,
    "礼拜": 604800, "禮拜": 604800,
    "个月": 2592000, "個月": 2592000,
}

# 绝对日期：今天 / 明天 / 后天 / 大后天
_DATE_WORD_RE = re.compile("大后天|大後天|后天|後天|明天|明日|今天|今日")
_DATE_WORD_OFFSET = {
    "今天": 0, "今日": 0,
    "明天": 1, "明日": 1,
    "后天": 2, "後天": 2,
    "大后天": 3, "大後天": 3,
}

# 10月25號 / 十月二十五号
_MD_RE = re.compile(rf"({_NUM})\s*月\s*({_NUM})\s*[号號日]")
# 10/25
_MD_SLASH_RE = re.compile(r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/])")
# 只有月份（13月 → 无效）
_MONTH_RE = re.compile(rf"({_NUM})\s*月")

# 週五 / 星期五 / 下週一 / 下个礼拜三 / 下下週三
_WEEKDAY_RE = re.compile(
    r"(?:(下下|下个|下個|下)\s*)?(?:周|週|星期|礼拜|禮拜)\s*([一二三四五六日天])")
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 時刻：15:30 / 下午三點半 / 晚上八点
_COLON_RE = re.compile(r"(\d{1,2})\s*:\s*(\d{1,2})")
_PERIOD = "凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|晚间|晚間|夜里|夜裡|深夜"
_POINT_RE = re.compile(
    rf"(?:({_PERIOD})\s*)?({_NUM})\s*[点點]\s*(半|[一二三]刻|(?:{_NUM})\s*分)?"
)
_PERIOD_ONLY_RE = re.compile(_PERIOD)
_PERIOD_DEFAULT = {
    "凌晨": (0, 0),
    "早上": (8, 0),
    "早晨": (7, 0),
    "上午": (9, 0),
    "中午": (12, 0),
    "下午": (15, 0),
    "傍晚": (18, 0),
    "晚上": (20, 0),
    "晚间": (20, 0),
    "晚間": (20, 0),
    "夜里": (23, 0),
    "夜裡": (23, 0),
    "深夜": (23, 0),
}
_NIGHT = {"傍晚", "晚上", "晚间", "晚間", "夜里", "夜裡", "深夜"}


def _to_int(token) -> int | None:
    """中文/阿拉伯数字 → int。支持 三、十、十五、二十三、一百零五。"""
    text = str(token or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "百" in text:
        head, _, tail = text.partition("百")
        base = _to_int(head) if head else 1
        if base is None:
            return None
        rest = _to_int(tail) if tail else 0
        return None if rest is None else base * 100 + rest
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _to_int(head) if head else 1
        ones = _to_int(tail) if tail else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(text) == 1 and text in _CN_DIGITS:
        return _CN_DIGITS[text]
    return None


def _as_dt(value) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value))
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        return datetime.fromtimestamp(float(ts()))
    return datetime.now()


def _period_hour(period: str, hour: int) -> int:
    """把「下午三点」的 3 变成 15；只处理 1~11 点，12 点按中午算。"""
    if not period:
        return hour
    if period == "凌晨":
        return 0 if hour == 12 else hour
    if period == "中午":
        return hour + 12 if 1 <= hour < 12 else hour
    if period == "下午":
        return hour + 12 if 1 <= hour < 12 else hour
    if period in _NIGHT:
        if hour == 12:
            return 0
        return hour + 12 if 1 <= hour < 12 else hour
    # 早上 / 上午 / 早晨
    return hour


def _describe(due: datetime, now: datetime) -> str:
    delta = (due.date() - now.date()).days
    if delta == 0:
        label = "今天"
    elif delta == 1:
        label = "明天"
    elif delta == 2:
        label = "后天"
    else:
        label = f"{due.month}月{due.day}日"
    return f"{label} {due.hour:02d}:{due.minute:02d}"


def _month_day(month: int, day: int, now: datetime) -> datetime | None:
    """今年的这个日期；已经过了就顺延到明年。日期不存在返回 None。"""
    for year in (now.year, now.year + 1):
        try:
            value = datetime(year, month, day)
        except ValueError:
            continue
        if value.date() >= now.date():
            return value
    return None


def parse_when(text, now=None) -> tuple[float | None, str]:
    """把一句口语时间解析成 (时间戳, 中文说明)。

    认不出来返回 (None, 原因)。第二个值同时也是给用户看的确认文案。
    """
    raw = str(text or "").translate(_FULLWIDTH)
    if not raw.strip():
        return None, "没说什么时候"
    # 口语缩写先展开，否则「明晚八点」会被当成早上八点
    for short, full in (("今晚", "今天晚上"), ("明晚", "明天晚上"),
                        ("今早", "今天早上"), ("明早", "明天早上"),
                        ("明儿", "明天"), ("明兒", "明天")):
        raw = raw.replace(short, full)
    current = _as_dt(now)

    # ---- 相对时间：直接算，不再看日期
    rel = _REL_RE.search(raw)
    if rel:
        amount_token, unit = rel.group(1), rel.group(2)
        if amount_token.startswith("半"):
            amount = 0.5
        else:
            value = _to_int(amount_token)
            amount = float(value) if value is not None else None
        seconds = _UNIT_SECONDS.get(unit, 0)
        if amount is None or seconds <= 0:
            return None, f"看不懂这个时长：{rel.group(0)}"
        due = current + timedelta(seconds=amount * seconds)
        return due.timestamp(), _describe(due, current)

    # ---- 绝对日期
    date_value: datetime | None = None
    weekday_explicit = False
    word = _DATE_WORD_RE.search(raw)
    if word:
        offset = _DATE_WORD_OFFSET.get(word.group(0), 0)
        date_value = datetime.combine(current.date() + timedelta(days=offset), dtime(0, 0))
    else:
        md = _MD_RE.search(raw) or _MD_SLASH_RE.search(raw)
        if md:
            month, day = _to_int(md.group(1)), _to_int(md.group(2))
            if month is None or not 1 <= month <= 12:
                return None, f"月份不对：{md.group(0)}"
            if day is None or not 1 <= day <= 31:
                return None, f"日期不对：{md.group(0)}"
            date_value = _month_day(month, day, current)
            if date_value is None:
                return None, f"没有这一天：{md.group(0)}"
        else:
            month_only = _MONTH_RE.search(raw)
            if month_only:
                month = _to_int(month_only.group(1))
                if month is None or not 1 <= month <= 12:
                    return None, f"月份不对：{month_only.group(0)}"
                return None, "只说月份，没说哪天"
            week = _WEEKDAY_RE.search(raw)
            if week:
                prefix = week.group(1) or ""
                target_idx = _WEEKDAY_INDEX[week.group(2)]
                today_idx = current.weekday()
                if prefix == "下" or prefix in ("下个", "下個"):
                    days = 7 - today_idx + target_idx
                elif prefix == "下下":
                    days = 14 - today_idx + target_idx
                else:
                    days = (target_idx - today_idx) % 7
                date_value = datetime.combine(
                    current.date() + timedelta(days=days), dtime(0, 0))
                weekday_explicit = True

    # ---- 时刻
    hour = minute = None
    period = ""
    colon = _COLON_RE.search(raw)
    if colon:
        hour, minute = int(colon.group(1)), int(colon.group(2))
        # 「下午3:30」里的时段：取冒号前面最后一个时段词
        periods = _PERIOD_ONLY_RE.findall(raw[:colon.start()])
        if periods:
            period = periods[-1]
        if hour > 23:
            return None, f"点数不对：{hour}点"
        if minute > 59:
            return None, f"分钟不对：{minute}分"
    else:
        for point in _POINT_RE.finditer(raw):
            period = point.group(1) or ""
            value = _to_int(point.group(2))
            if value is None:
                continue
            minutes = 0
            minute_token = point.group(3)
            if minute_token:
                token = minute_token.strip()
                if token == "半":
                    minutes = 30
                elif token.endswith("刻"):
                    quarter = _to_int(token[:-1])
                    if quarter is None:
                        continue
                    minutes = quarter * 15
                else:
                    parsed = _to_int(re.sub(r"[分\s]", "", token))
                    if parsed is None:
                        continue
                    minutes = parsed
            # 「一点小事」里的「一点」不是时间：没日期、没时段、没分钟时不当真
            if (value == 1 and not period and not minute_token
                    and date_value is None and point.group(2) == "一"):
                continue
            hour, minute = value, minutes
            break
        if hour is None:
            bare = _PERIOD_ONLY_RE.search(raw)
            if bare:
                period = bare.group(0)
                hour, minute = _PERIOD_DEFAULT.get(period, (9, 0))

    if hour is not None:
        if hour > 23:
            return None, f"点数不对：{hour}点"
        if minute > 59:
            return None, f"分钟不对：{minute}分"
        hour = _period_hour(period, hour)
        if not 0 <= hour <= 23:
            return None, f"点数不对：{hour}点"

    if date_value is None and hour is None:
        return None, "没说什么时候"

    if hour is None:
        hour, minute = 9, 0

    base_day = date_value.date() if date_value is not None else current.date()
    due = datetime.combine(base_day, dtime(hour, minute))
    if date_value is None:
        # 只给时刻：今天已经过了就顺延到明天
        if due <= current:
            due += timedelta(days=1)
    elif weekday_explicit and due <= current:
        # 「周五」说了但今天就是周五、点已经过了 → 下周
        due += timedelta(days=7)
    return due.timestamp(), _describe(due, current)


# ====================================================================== 待办存储
class ReminderStore:
    """``data/reminders.json``：``{"items": [...], "seq": n}``。

    所有读写都在 RLock 里，落盘走「同目录临时文件 + os.replace」，
    半截写坏的文件不会出现在正式路径上。
    """

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.items: list[dict] = []
        self.seq = 0
        self._load()

    # ---------------------------------------------------------------- 读写
    @staticmethod
    def _normalize(raw: dict) -> dict | None:
        if not isinstance(raw, dict):
            return None
        what = str(raw.get("what") or "").strip()
        if not what:
            return None
        try:
            due = float(raw.get("due_ts") or 0)
        except (TypeError, ValueError):
            return None
        if due <= 0:
            return None
        group_id = raw.get("group_id")
        try:
            group_id = int(group_id) if group_id not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            group_id = None
        try:
            user_id = int(raw.get("user_id") or 0)
        except (TypeError, ValueError):
            user_id = 0
        try:
            created = float(raw.get("created_ts") or due)
        except (TypeError, ValueError):
            created = due
        try:
            notified = float(raw.get("notified_ts") or 0)
        except (TypeError, ValueError):
            notified = 0.0
        return {
            "id": str(raw.get("id") or ""),
            "session_id": str(raw.get("session_id") or ""),
            "group_id": group_id,
            "user_id": user_id,
            "who": str(raw.get("who") or ""),
            "what": what,
            "due_ts": due,
            "created_ts": created,
            "done": bool(raw.get("done")),
            "notified_ts": notified,
        }

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warn(f"提醒文件读取失败：{type(e).__name__} {e}")
            return
        rows = data.get("items") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return
        seen = set()
        for raw in rows:
            item = self._normalize(raw)
            if item is None:
                continue
            if not item["id"] or item["id"] in seen:
                item["id"] = f"r{len(self.items) + 1}"
            seen.add(item["id"])
            self.items.append(item)
        try:
            self.seq = max(int((data or {}).get("seq") or 0), len(self.items))
        except (TypeError, ValueError, AttributeError):
            self.seq = len(self.items)

    def _save(self):
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        payload = {"items": self.items, "seq": self.seq}
        fd, tmp = tempfile.mkstemp(prefix=".reminders.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- 增删改查
    def add(self, *, session_id: str, group_id=None, user_id=0, who: str = "",
            what: str, due_ts: float) -> dict:
        with self.lock:
            self.seq += 1
            item = {
                "id": f"r{self.seq}",
                "session_id": str(session_id or ""),
                "group_id": int(group_id) if group_id else None,
                "user_id": int(user_id or 0),
                "who": str(who or ""),
                "what": str(what or "").strip(),
                "due_ts": float(due_ts),
                "created_ts": time.time(),
                "done": False,
                "notified_ts": 0.0,
            }
            self.items.append(item)
            self._save()
            return dict(item)

    def list_pending(self) -> list[dict]:
        with self.lock:
            rows = [dict(x) for x in self.items
                    if not x.get("done") and not x.get("notified_ts")]
        rows.sort(key=lambda x: float(x.get("due_ts") or 0))
        return rows

    def due(self, now: float) -> list[dict]:
        current = float(now)
        with self.lock:
            rows = [dict(x) for x in self.items
                    if not x.get("done") and not x.get("notified_ts")
                    and float(x.get("due_ts") or 0) <= current]
        rows.sort(key=lambda x: float(x.get("due_ts") or 0))
        return rows

    def mark_done(self, item_id: str) -> bool:
        with self.lock:
            for item in self.items:
                if item["id"] == str(item_id):
                    item["done"] = True
                    self._save()
                    return True
        return False

    def mark_notified(self, item_id: str, ts: float | None = None) -> bool:
        with self.lock:
            for item in self.items:
                if item["id"] == str(item_id):
                    item["notified_ts"] = float(ts if ts is not None else time.time())
                    item["done"] = True
                    self._save()
                    return True
        return False

    def snooze(self, item_id: str, sec: float, now: float | None = None) -> bool:
        base = float(now if now is not None else time.time())
        with self.lock:
            for item in self.items:
                if item["id"] == str(item_id):
                    item["due_ts"] = base + max(0.0, float(sec))
                    item["done"] = False
                    item["notified_ts"] = 0.0
                    self._save()
                    return True
        return False

    def remove(self, item_id: str) -> bool:
        with self.lock:
            before = len(self.items)
            self.items = [x for x in self.items if x["id"] != str(item_id)]
            if len(self.items) == before:
                return False
            self._save()
            return True


# ====================================================================== 发送
class _FakeEvent:
    """给 Context 用的最小事件壳：只为发一条提醒，不参与流水线。"""

    def __init__(self, item: dict):
        self.post_type = "message"
        self.type = "message"
        self.message_id = 0
        self.user_id = int(item.get("user_id") or 0)
        self.group_id = item.get("group_id")
        self.self_id = 0
        self.message: list[dict] = []
        self.plain = ""
        self.raw = {}
        self.to_me = False
        self.reply_id = None
        self.sender = {}
        self.time = int(time.time())
        self._session_id = str(item.get("session_id") or "")
        self._who = str(item.get("who") or "")

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)

    @property
    def session_id(self) -> str:
        if self._session_id:
            return self._session_id
        if self.group_id:
            return f"group_{self.group_id}"
        return f"private_{self.user_id}"

    @property
    def sender_name(self) -> str:
        return self._who


class ReminderService:
    """后台轮询到点提醒。停机用 Event 立刻叫醒，最多等 5 秒。"""

    MAX_ATTEMPTS = 3
    RETRY_SEC = 60.0

    def __init__(self, bot):
        self.bot = bot
        self.task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._attempts: dict[str, int] = {}

    # ---------------------------------------------------------------- 配置
    def _cfg(self, key, default=None):
        try:
            return self.bot.config.get(f"reminders.{key}", default)
        except Exception:
            return default

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    def poll_sec(self) -> float:
        try:
            return max(1.0, float(self._cfg("poll_sec", 30) or 30))
        except (TypeError, ValueError):
            return 30.0

    def quiet_hours(self) -> tuple[int, int] | None:
        window = self._cfg("quiet_hours", [0, 9])
        if not window or not isinstance(window, (list, tuple)) or len(window) != 2:
            return None
        try:
            start, end = int(window[0]), int(window[1])
        except (TypeError, ValueError):
            return None
        if not (0 <= start <= 24 and 0 <= end <= 24) or start == end:
            return None
        return start, end

    # ---------------------------------------------------------------- 生命周期
    async def start(self):
        if not self.enabled:
            return
        if self.task is not None and not self.task.done():
            return
        self._stop = asyncio.Event()
        self.task = tasks.spawn(self._loop(), name="reminders")

    async def stop(self):
        event, task = self._stop, self.task
        self._stop, self.task = None, None
        if event is not None:
            event.set()
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            task.cancel()
            try:
                await task
            except Exception:
                pass

    async def _loop(self):
        while self._stop is not None and not self._stop.is_set():
            try:
                await self.tick()
            except Exception as e:
                log.warn(f"提醒轮询失败：{type(e).__name__} {e}")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_sec())
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------------- 主逻辑
    async def tick(self, now: float | None = None) -> int:
        """处理一批到点提醒，返回成功发出几条。单条失败不影响其他条。"""
        if not self.enabled:
            return 0
        store = getattr(self.bot, "reminders", None)
        if store is None:
            return 0
        current = float(now if now is not None else time.time())
        sent = 0
        # 停机一天回来时可能积压一堆，一次只发几条，别把群刷屏
        cap = max(1, int(self._cfg("max_per_tick", 3) or 3))
        for item in store.due(current)[:cap]:
            item_id = str(item.get("id") or "")
            try:
                if self._in_quiet(current):
                    target = self._quiet_target(current)
                    store.snooze(item_id, max(1.0, target - current), now=current)
                    log.debug(f"提醒顺延到 {time.strftime('%H:%M', time.localtime(target))}：{item.get('what')}")
                    continue
                await self._deliver(item)
                store.mark_notified(item_id, ts=current)
                self._attempts.pop(item_id, None)
                self._remember_sent(item)
                sent += 1
            except Exception as e:
                try:
                    self._on_fail(store, item_id, e)
                except Exception as inner:
                    log.warn(f"提醒失败处理异常 {item_id}：{type(inner).__name__} {inner}")
        return sent

    async def _deliver(self, item: dict):
        """提醒不经过拟人化发送层：到点就说，别拖打字延迟，更不能把提醒正文打错。"""
        conn = self.bot.conn_for()
        if conn is None:
            raise RuntimeError("协议端没连接")
        what = str(item.get("what") or "")
        user_id = int(item.get("user_id") or 0)
        group_id = item.get("group_id")
        if group_id:
            segments = [msg.At(user_id), msg.Text(f" 你让我提醒你：{what}")]
            await conn.send_message(segments, group_id=int(group_id))
        else:
            segments = [msg.Text(f"你让我提醒你：{what}")]
            await conn.send_message(segments, user_id=user_id)

    def _remember_sent(self, item: dict):
        what = str(item.get("what") or "")
        session_id = str(item.get("session_id") or "")
        log.info(f"提醒已发 {session_id or item.get('user_id')}：{what[:40]}")
        store = getattr(self.bot, "store", None)
        if store is None or not session_id:
            return
        try:
            store.append(session_id, "assistant", f"（提醒他：{what}）", who="我")
        except Exception as e:
            log.debug(f"提醒写回会话失败：{type(e).__name__}")

    def _on_fail(self, store, item_id: str, error: Exception):
        attempt = self._attempts.get(item_id, 0) + 1
        self._attempts[item_id] = attempt
        what = next((x.get("what") for x in getattr(store, "items", [])
                     if str(x.get("id")) == item_id), "")
        log.warn(f"提醒发送失败 {item_id}（第 {attempt}/{self.MAX_ATTEMPTS} 次）："
                 f"{type(error).__name__} {error}")
        try:
            if attempt >= self.MAX_ATTEMPTS:
                store.mark_done(item_id)
                self._attempts.pop(item_id, None)
                log.warn(f"提醒放弃 {item_id}：连续失败 {self.MAX_ATTEMPTS} 次（{what}）")
            else:
                store.snooze(item_id, self.RETRY_SEC)
        except Exception as e:
            log.warn(f"提醒重试登记失败 {item_id}：{type(e).__name__} {e}")

    # ---------------------------------------------------------------- 静默时段
    def _in_quiet(self, now: float) -> bool:
        window = self.quiet_hours()
        if window is None:
            return False
        start, end = window
        hour = datetime.fromtimestamp(now).hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end        # 跨零点，如 23 → 9

    def _quiet_target(self, now: float) -> float:
        window = self.quiet_hours()
        if window is None:
            return now
        end = window[1] % 24
        current = datetime.fromtimestamp(now)
        target = current.replace(hour=end, minute=0, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target.timestamp()
