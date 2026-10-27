"""自动发 B 站动态（默认关闭）。

对外发布，所以规则比读接口严：
  1. `tools.bili_post_enabled` 必须是字面 True 才动；
  2. 必须有 `bili_jct`（B 站写操作要 CSRF），只有 SESSDATA 一律不发；
  3. 每天最多 `tools.bili_post_daily_limit` 条，正整数字面校验，额度从磁盘账本读；
  4. 先持久化「pending」预留额度，再**只调用一次**后端；回执必须 `success is True` 才算成功；
  5. 超时/取消都按「未确认」记，额度照扣（宁可少发，不可重复发）；
  6. 同一天不重复发同一段文字。

内容来源：`source=scroll` 用最近的刷片笔记，`source=topic` 按 `topic` 现写。
写完的账本在 `data/bili_post.json`，只留最近 300 条，但绝不丢掉当天的记录。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime

from . import bili_scroll
from .log import log

_默认 = {
    "enabled": False,
    "daily_limit": 1,
    "times": [],            # 每天几点发，如 ["12:30"]；空 = 不定时
    "source": "scroll",     # scroll=用刷片笔记 / topic=按主题现写
    "topic": "",
    "max_chars": 180,
}

_锁 = asyncio.Lock()


def _cfg(bot, key, default=None):
    return bot.config.get(f"tools.bili_post_{key}", _默认.get(key, default))


def _base(bot) -> str:
    return getattr(bot, "base_dir", ".") or "."


def _path(bot) -> str:
    return os.path.join(_base(bot), "data", "bili_post.json")


def _load(bot) -> list[dict]:
    try:
        with open(_path(bot), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warn(f"B 站动态账本读取失败，按空账本处理：{type(e).__name__}")
        raise
    rows = data.get("records") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("动态账本格式不对")
    return [r for r in rows if isinstance(r, dict) and r.get("day")]


def _save(bot, rows: list[dict]) -> None:
    path = _path(bot)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    保留 = [r for r in rows if r.get("day") == today]
    旧 = [r for r in rows if r.get("day") != today][-300:]
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"records": 旧 + 保留}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def enabled(bot) -> bool:
    """字面 True 才算开：字符串 "true"、1 之类一律不开。"""
    return _cfg(bot, "enabled", False) is True


def _daily_limit(bot) -> int:
    raw = _cfg(bot, "daily_limit", 1)
    if isinstance(raw, bool):
        raise ValueError("daily_limit 不能是布尔")
    limit = int(raw)
    if limit <= 0:
        raise ValueError("daily_limit 必须是正整数")
    return limit


def today_count(bot) -> int:
    day = time.strftime("%Y-%m-%d")
    return sum(1 for r in _load(bot) if r.get("day") == day)


def has_credential(bot) -> bool:
    try:
        return bool(bili_scroll._bili_jct(bot))
    except Exception:
        return False


def _parse_times(bot) -> list[int]:
    raw = _cfg(bot, "times", []) or []
    out = set()
    for t in raw:
        m = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$", str(t))
        if m:
            时, 分 = int(m.group(1)), int(m.group(2))
            if 0 <= 时 < 24 and 0 <= 分 < 60:
                out.add(时 * 60 + 分)
    return sorted(out)


def _wait_seconds(times: list[int], now: datetime | None = None) -> float:
    现在 = now or datetime.now()
    当前 = 现在.hour * 60 + 现在.minute + 现在.second / 60.0
    for t in times:
        if t > 当前:
            return max(5.0, (t - 当前) * 60.0)
    return max(5.0, (1440 - 当前 + times[0]) * 60.0)


# ---------------------------------------------------------------- 内容
def _material(bot) -> str:
    """给模型看的素材：刷片笔记或指定主题。"""
    if str(_cfg(bot, "source", "scroll")).lower() == "topic":
        return f"主题：{str(_cfg(bot, 'topic', '') or '').strip()}"
    笔记 = bili_scroll.recent_notes(bot, 1)
    if not 笔记:
        return ""
    try:
        body = bili_scroll.read_note(bot, 笔记[0]["name"])
    except Exception:
        return ""
    return f"你最近刷到的片子笔记：\n{body}"


async def compose(bot) -> str:
    """让她用自己的语气写一条动态；写不出来就返回空串。"""
    素材 = _material(bot).strip()
    if not 素材:
        return ""
    上限 = max(20, int(_cfg(bot, "max_chars", 180) or 180))
    想 = (f"{素材}\n\n"
          f"用你自己的语气发一条 B 站动态说说这件事，不超过 {上限} 字。"
          "不要列表、不要标题、不要复述原文，不要 emoji，不要提「素材」「根据」。"
          "就像随手发的一条日常感想。")
    try:
        系统 = bot.persona.build()
    except Exception:
        系统 = ""
    try:
        回复 = await asyncio.wait_for(
            bot.llm.chat_message([{"role": "system", "content": 系统},
                                  {"role": "user", "content": 想}], None),
            timeout=60)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warn(f"B 站动态写不出来（已跳过这一轮）：{type(e).__name__} {e}")
        return ""
    话 = str((回复 or {}).get("content") or "").strip()
    话 = re.sub(r"\s+", " ", 话)
    return 话[:上限] if 话 else ""


# ---------------------------------------------------------------- 发布
async def post_once(bot, text: str | None = None) -> dict:
    """发一条动态。返回 {ok, reason} / {ok: True, dynamic_id}。永不抛（取消除外）。"""
    async with _锁:
        if not enabled(bot):
            return {"ok": False, "reason": "自动发动态没开（tools.bili_post_enabled=false）"}
        if not has_credential(bot):
            return {"ok": False,
                    "reason": "没有 bili_jct，发不了动态（只有 SESSDATA 只能读）",
                    "hint": "把浏览器 cookie 里的 bili_jct 填到 tools.bili_jct"}
        try:
            limit = _daily_limit(bot)
            rows = _load(bot)
        except Exception as e:
            return {"ok": False, "reason": f"动态账本或额度配置不可用：{type(e).__name__}"}
        正文 = (text if text is not None else await compose(bot)).strip()
        if not 正文:
            return {"ok": False, "reason": "这一轮没写出内容"}
        if not text:
            上限 = max(20, int(_cfg(bot, "max_chars", 180) or 180))
            正文 = 正文[:上限]
        day = time.strftime("%Y-%m-%d")
        今天 = [r for r in rows if r.get("day") == day]
        if len(今天) >= limit:
            return {"ok": False, "reason": f"今天已经发到上限（tools.bili_post_daily_limit={limit}）"}
        if any(r.get("status") == "confirmed" and r.get("text") == 正文 for r in 今天):
            return {"ok": False, "reason": "今天已经发过一模一样的内容，跳过"}

        record = {"day": day, "ts": time.time(), "status": "pending",
                  "text": 正文[:500], "source": str(_cfg(bot, "source", "scroll")),
                  "dynamic_id": ""}
        rows.append(record)
        try:
            _save(bot, rows)
        except Exception as e:
            return {"ok": False, "reason": f"额度账本写不进去，未发布：{type(e).__name__}"}

        取消 = None
        try:
            结果 = await bili_scroll.action(bot, "dynamic", {"text": 正文})
            数据 = 结果.get("data") if isinstance(结果, dict) else None
            成功 = bool(结果.get("ok")) and isinstance(数据, dict) and 数据.get("success") is True
            if 成功:
                record["status"] = "confirmed"
                内 = 数据.get("data") if isinstance(数据.get("data"), dict) else {}
                record["dynamic_id"] = str(
                    内.get("dynamic_id") or 内.get("dyn_id") or 内.get("id") or "")
                回复 = {"ok": True, "dynamic_id": record["dynamic_id"], "text": 正文}
            else:
                record["status"] = "unconfirmed"
                错 = (数据 or {}).get("error") if isinstance(数据, dict) else (结果 or {}).get("error")
                回复 = {"ok": False,
                        "reason": f"B 站没确认发布成功：{错 or '未知原因'}；本次已计入额度"}
        except asyncio.CancelledError as e:
            取消 = e
            record["status"] = "unconfirmed"
            回复 = {"ok": False, "reason": "发布被取消，结果未确认；本次已计入额度"}
        except Exception as e:
            record["status"] = "unconfirmed"
            回复 = {"ok": False,
                    "reason": f"发布未确认：{type(e).__name__} {e}；本次已计入额度"}
        try:
            _save(bot, rows)
        except Exception as e:
            log.warn(f"B 站动态回执保存失败，禁止后续自动发布：{type(e).__name__}")
            if 取消 is None:
                回复 = {"ok": False, "reason": "回执已返回但账本保存失败，结果不再视为已确认"}
        if 取消 is not None:
            raise 取消
        return 回复


# ---------------------------------------------------------------- 定时
async def scheduler(bot) -> None:
    """每天按 tools.bili_post_times 发一条；没配时间就自己退掉。"""
    times = _parse_times(bot)
    if not times:
        return
    log.info("B站自动发动态已启动：%s" % "、".join(f"{t // 60:02d}:{t % 60:02d}" for t in times))
    while True:
        await asyncio.sleep(_wait_seconds(times))
        try:
            if not enabled(bot):
                continue
            结果 = await post_once(bot)
            if 结果.get("ok"):
                log.info(f"自动发了条 B 站动态 dynamic_id={结果.get('dynamic_id') or '未知'}")
            else:
                log.info(f"这一轮没发 B 站动态：{结果.get('reason')}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warn(f"自动发 B 站动态这一轮出错（已兜住）：{type(e).__name__} {e}")
        await asyncio.sleep(65)
