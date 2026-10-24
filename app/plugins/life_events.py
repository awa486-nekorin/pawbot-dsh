"""生活事件：群信息后台刷新 + 生日记录/生日祝福。

这个插件只做两件事，都不改别人负责的模块：

1. 群消息进来时，如果 GroupInfo 缓存过期，就丢一个后台 task 去刷新
   （``asyncio.create_task``，不阻塞消息处理，也不影响 AI 回话）。
2. 生日：
   - 只识别「本人说自己生日」的句子，写进 ``data/birthdays.json``；
   - 当天第一次在某群收到消息时，如果群里有群友过生日、还没祝过、
     且时间在 ``life.birthday_wish_hours`` 内，就 @ 他发一句。

选「每条消息顺手检查」而不是「每 10 分钟后台轮询」的原因：
插件会被 WebUI 热重载，后台 task 容易变成没人管的孤儿；而群消息本身就是
天然的心跳，检查一次只是读一个小 JSON，代价可以忽略。过了 22:00 不补祝，
第二天也不会补（wished 记的是当天日期）。
"""

import asyncio
import json
import os
import random
import re
import tempfile
import threading
from datetime import date, datetime, time as dtime

from pawbot import groupinfo
from pawbot import message as msg
from pawbot.log import log
from pawbot.plugin import on_message

__plugin_name__ = "生活事件"
__version__ = "1.0.0"
__description__ = "群信息后台刷新 + 生日记录与祝福"

# 后台刷新 task 的引用：不 retain 的话可能被 GC，完成后从集合里移除。
_TASKS: set[asyncio.Task] = set()
# 同一个群同时只刷一次，别让连发的消息拉起一堆重复请求。
_REFRESHING: set[int] = set()
# birthdays.json 的读-改-写锁（线程锁，插件里都是同步文件操作）。
_BIRTHDAY_LOCK = threading.RLock()

WISHES = [
    "生日快乐！今天你最大，想要什么自己说。",
    "生日快乐呀，又长一岁，别太得意。",
    "生日快乐！蛋糕分我一块就行。",
    "生日快乐，今天允许你许一个不靠谱的愿望。",
    "生日快乐！希望你今年少点糟心事，多点好吃的。",
    "生日快乐，别熬夜了……就今天听我一句。",
    "生日快乐！新的一岁也要好好吃饭。",
]

# 只认「我 / 我的生日」。刻意不匹配「他生日」「我妈生日」这种第三人称。
_MD = (r"(?:\d{4}\s*年)?\s*(?P<month>\d{1,2})\s*(?:月|[./\-])\s*"
       r"(?P<day>\d{1,2})\s*[日号]?")
_BIRTHDAY_PATTERNS = (
    # 我生日是10月21号 / 我的生日 10.21 / 我生日：10/21
    re.compile(r"我(?:的)?生日[是为在：:，,\s]*" + _MD),
    # 我 10/21 生日 / 我是10月21号生日 / 我10月21日过生日
    re.compile(r"我(?:是|在)?\s*" + _MD + r"\s*(?:那天)?(?:的)?(?:过)?生日"),
)


def _cfg(bot, key: str, default):
    try:
        val = bot.config.get(f"life.{key}", default)
    except Exception:
        return default
    return default if val is None else val


def _base_dir(bot) -> str:
    base = getattr(bot, "base_dir", "") or ""
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return base


def birthday_path(bot) -> str:
    path = str(_cfg(bot, "birthday_file", "data/birthdays.json"))
    if not os.path.isabs(path):
        path = os.path.join(_base_dir(bot), path)
    return path


def _as_dt(value=None) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, dtime(0, 0))
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    return datetime.now()


# ---------------------------------------------------------------- 生日表
def load_birthdays(bot) -> dict:
    path = birthday_path(bot)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.debug(f"生日表读取失败：{type(e).__name__} {e}")
        return {}


def save_birthdays(bot, data: dict):
    """原子写：临时文件 + os.replace，别留半截 JSON。"""
    path = birthday_path(bot)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".birthdays.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_birthday(text: str) -> dict | None:
    """从一句话里提取「我」的生日，返回 {"md": "10-21", "month": 10, "day": 21}。"""
    text = str(text or "")
    if not text or "生日" not in text:
        return None
    for pattern in _BIRTHDAY_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            month, day = int(m.group("month")), int(m.group("day"))
        except (TypeError, ValueError):
            continue
        if 1 <= month <= 12 and 1 <= day <= 31:
            return {"md": f"{month:02d}-{day:02d}", "month": month, "day": day}
    return None


def _groups_of(info) -> list[int]:
    out = []
    if isinstance(info, dict):
        for g in info.get("groups") or []:
            try:
                out.append(int(g))
            except (TypeError, ValueError):
                continue
    return out


def remember_birthday(ctx) -> bool:
    """本人说自己生日时写进表里；不是本人说的返回 False。"""
    parsed = parse_birthday(getattr(ctx, "plain", ""))
    if not parsed:
        return False
    gid = _safe_int(getattr(ctx, "group_id", 0))
    qq = str(getattr(ctx, "user_id", "") or "")
    if not qq:
        return False
    with _BIRTHDAY_LOCK:
        data = load_birthdays(ctx.bot)
        old = data.get(qq)
        old = old if isinstance(old, dict) else {}
        groups = set(_groups_of(old))
        if gid:
            groups.add(gid)
        data[qq] = {
            "md": parsed["md"],
            "name": str(getattr(ctx, "sender_name", "") or old.get("name") or qq),
            "groups": sorted(groups),
        }
        try:
            save_birthdays(ctx.bot, data)
        except Exception as e:
            log.warn(f"生日表写入失败：{type(e).__name__} {e}")
            return False
    log.info(f"记下生日 {qq}（{data[qq]['name']}）：{parsed['md']}")
    return True


def _touch_group(ctx) -> None:
    """已知生日的人在某个群说过话，就把这个群加进他的 groups。"""
    qq = str(getattr(ctx, "user_id", "") or "")
    gid = _safe_int(getattr(ctx, "group_id", 0))
    if not qq or not gid:
        return
    with _BIRTHDAY_LOCK:
        data = load_birthdays(ctx.bot)
        info = data.get(qq)
        if not isinstance(info, dict):
            return
        groups = set(_groups_of(info))
        if gid in groups:
            return
        groups.add(gid)
        info["groups"] = sorted(groups)
        data[qq] = info
        try:
            save_birthdays(ctx.bot, data)
        except Exception as e:
            log.debug(f"生日群列表更新失败：{type(e).__name__} {e}")


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _wish_hours(bot) -> tuple[int, int]:
    raw = _cfg(bot, "birthday_wish_hours", [9, 22])
    try:
        start, end = int(raw[0]), int(raw[1])
    except Exception:
        start, end = 9, 22
    return max(0, min(23, start)), max(0, min(24, end))


async def check_birthdays(ctx, now=None) -> int:
    """今天该祝的祝一遍，返回这次发了几条。只发一次（wished 按 QQ 记日期）。"""
    if not bool(_cfg(ctx.bot, "birthday_wish", True)):
        return 0
    if not getattr(ctx, "is_group", False):
        return 0
    gid = _safe_int(getattr(ctx, "group_id", 0))
    if not gid:
        return 0

    dt = _as_dt(now)
    start, end = _wish_hours(ctx.bot)
    if not (start <= dt.hour < end):
        return 0

    today = dt.strftime("%Y-%m-%d")
    md = dt.strftime("%m-%d")
    data = load_birthdays(ctx.bot)
    candidates = []
    for qq, info in data.items():
        if not str(qq).isdigit() or not isinstance(info, dict):
            continue
        if str(info.get("md") or "") != md:
            continue
        if gid not in _groups_of(info):
            continue
        candidates.append(str(qq))

    sent = 0
    for qq in sorted(candidates):
        if str((data.get("wished") or {}).get(qq) or "") == today:
            continue
        text = random.choice(WISHES)
        try:
            result = await ctx.send([msg.At(qq), msg.Text(" " + text)], group_id=gid)
        except Exception as e:
            log.warn(f"生日祝福发送失败 {qq}：{type(e).__name__} {e}")
            continue
        if result is None:
            # 被安全层限流/静默吞掉了：不算祝过，下次消息再试
            log.debug(f"生日祝福被安全层拦下，稍后再试：{qq}")
            continue
        # 发送成功才记；失败的话下次消息还会重试
        with _BIRTHDAY_LOCK:
            fresh = load_birthdays(ctx.bot)
            wished = fresh.get("wished")
            if not isinstance(wished, dict):
                wished = {}
            wished[qq] = today
            fresh["wished"] = wished
            try:
                save_birthdays(ctx.bot, fresh)
            except Exception as e:
                log.warn(f"生日祝福记录写入失败：{type(e).__name__} {e}")
        try:
            ctx.bot.store.append(f"group_{gid}", "assistant", text, who="我")
        except Exception as e:
            log.debug(f"生日祝福入库失败：{type(e).__name__} {e}")
        log.info(f"生日祝福 → 群{gid} QQ{qq}：{text}")
        sent += 1
    return sent


# ---------------------------------------------------------------- 群信息刷新
def _spawn_refresh(bot, gid: int) -> None:
    if gid in _REFRESHING:
        return
    _REFRESHING.add(gid)

    async def runner():
        try:
            await groupinfo.for_bot(bot).get(bot, gid)
        except Exception as e:      # get() 自己不会抛，这里只是兜底
            log.debug(f"群信息后台刷新异常 {gid}：{type(e).__name__} {e}")
        finally:
            _REFRESHING.discard(gid)

    try:
        task = asyncio.create_task(runner())
    except RuntimeError:
        _REFRESHING.discard(gid)
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


# ---------------------------------------------------------------- 入口
@on_message(priority=80, block=False)
async def life_events(ctx):
    bot = ctx.bot
    if not bool(_cfg(bot, "enabled", True)):
        return
    if not getattr(ctx, "is_group", False):
        return
    gid = _safe_int(getattr(ctx, "group_id", 0))
    if not gid:
        return

    # 1) 群信息过期就后台刷，不阻塞这条消息
    try:
        if not groupinfo.for_bot(bot).is_fresh(gid):
            _spawn_refresh(bot, gid)
    except Exception as e:
        log.debug(f"群信息缓存检查失败：{type(e).__name__} {e}")

    # 2) 生日：先看这句是不是本人报生日，不是的话只补充「出现的群」
    try:
        if not remember_birthday(ctx):
            _touch_group(ctx)
    except Exception as e:
        log.debug(f"生日记录失败：{type(e).__name__} {e}")

    # 3) 今天该祝的祝一次
    try:
        await check_birthdays(ctx)
    except Exception as e:
        log.warn(f"生日检查失败：{type(e).__name__} {e}")
