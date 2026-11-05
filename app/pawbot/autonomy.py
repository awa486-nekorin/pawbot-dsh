"""自主生活：独立的活动时钟、安静执行与分享候选协调。

这一层不启动任何常驻循环。``Proactive._tick`` 在每个轮询周期最前面调用
:meth:`Autonomy.tick`，所以即使群很热闹、协议端暂时没连上，或当前处于安静
时段，她仍然有机会自己安静地做一件事。

边界：

* 只有到了自己持久化的 ``next_at`` 才会有一次机会；首次等待 2–4 小时。
* 重启时错过的活动不补发，中断的 ``running`` 标记按 abandoned 恢复。
* 一次 tick 最多执行一个动作：search / bili / qzone_read / qzone_post /
  reflect / skip。没有真实资料时不写经历、不伪造成功。
* 活动本身是静默的；只有 ``Proactive`` 在群聊里主动分享时才产生对外消息。
* 分享候选有时效、可过期、可以一直不发；群/全局冷却落盘。
* QQ 空间读写必须走 ``Qzone`` 的自主运行时 capability，不冒充主人。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
import random
import re
import tempfile
import threading
import time

from . import bili_scroll
from . import life_model
from . import untrusted
from . import websearch
from .log import log

STATE_VERSION = 1
STATE_NAME = "autonomy.json"
ACTIONS = ("search", "bili", "qzone_read", "qzone_interact", "qzone_post",
           "reflect", "skip")
_SELF_PATCH_OPS = {"add", "revise", "retire", "delete", "merge"}
_GOAL_PATCH_OPS = {"add", "revise", "complete", "pause", "abandon", "delete"}
_MAX_HISTORY = 24
_MAX_CANDIDATES = 24
_MAX_ROLE_CHARS = 3600
_MAX_PLAN_CONTEXT_CHARS = 9000
_SELF_TOP_FIELDS = {"text", "content", "title", "summary", "category",
                    "valid_scope", "expected_other_version", "into",
                    "into_id", "merge_into", "merge_target", "target",
                    "source_id", "from", "other_id"}
_STOP_BIGRAMS = {
    "的是", "一个", "这个", "那个", "什么", "怎么", "就是", "不是",
    "我们", "你们", "他们", "可以", "然后", "还是", "已经", "自己",
}


# --------------------------------------------------------------------- 工具
def _one_line(value, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _number(config, key: str, default: float) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return value if value >= 0 else float(default)


def _flag(config, key: str, default: bool = True) -> bool:
    try:
        value = config.get(key, default)
    except Exception:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no", "off", "none"}
    return value is not False


def _clip(text, limit: int) -> str:
    """保留换行的有界文本，不把多段提示压成一行。"""
    value = str(text or "").strip()
    return value[:max(0, int(limit))]


def _accepts_kwarg(fn, name: str) -> bool:
    """旧版 run_round 没有 candidates/source 时保持兼容，不靠 TypeError 猜副作用。"""
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _json_object(text) -> dict:
    """从模型回复里取第一个 JSON 对象；失败明确抛错，不猜内容。"""
    if isinstance(text, dict):
        return text
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("模型没有返回内容")
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型 JSON 不是对象")
    return data


def _source_hash(*parts) -> str:
    joined = "\n".join(str(p or "") for p in parts)
    return hashlib.sha1(joined.encode("utf-8", "replace")).hexdigest()[:20]


def _tokens(text) -> set[str]:
    """用于候选相关性：英文词 + 中文二元组。"""
    raw = str(text or "").lower()
    out: set[str] = set()
    for chunk in re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+", raw):
        if chunk.isascii():
            if chunk not in _STOP_BIGRAMS:
                out.add(chunk)
            continue
        if len(chunk) <= 2:
            out.add(chunk)
            continue
        out.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return {item for item in out if item not in _STOP_BIGRAMS}


def _overlap(query: str, text: str) -> int:
    return len(_tokens(query) & _tokens(text))


class Autonomy:
    """一个账号唯一的自主活动协调器。"""

    def __init__(self, bot, *, now=None, planner=None, rng=None):
        self.bot = bot
        self._now = now if callable(now) else None
        self._planner = planner if callable(planner) else None
        self._rng = rng if rng is not None else random
        self._state: dict | None = None
        self._state_lock = threading.RLock()
        self._tick_lock: asyncio.Lock | None = None
        self._tick_loop = None
        self._qzone_capability = None
        self._state_path = self._resolve_state_path()

    # ------------------------------------------------------------ 路径 / 配置
    def _resolve_state_path(self) -> str:
        paths = getattr(self.bot, "paths", None)
        try:
            if paths is not None and hasattr(paths, "state"):
                return str(paths.state(STATE_NAME))
        except Exception:
            pass
        try:
            from . import paths as layout
            return str(layout.for_bot(self.bot).state(STATE_NAME))
        except Exception:
            root = str(getattr(self.bot, "base_dir", ".") or ".")
            return os.path.join(root, "data", "state", STATE_NAME)

    def _cfg(self, key, default=None):
        try:
            return self.bot.config.get(f"life.{key}", default)
        except Exception:
            return default

    def _evolution_enabled(self) -> bool:
        """目标/自我条目是否允许被经历缓慢推动。关掉时不写、也不喂给规划。"""
        return _flag(self.bot.config, "life.evolution_enabled", True)

    def _growth_enabled(self) -> bool:
        """领域了解/实践/技能快照是否启用。关掉时不计养成，也不编造进度。"""
        return _flag(self.bot.config, "life.growth_enabled", True)

    @property
    def enabled(self) -> bool:
        value = self._cfg("autonomy_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"", "false", "0", "no", "off", "none"}
        return value is not False

    def _reasoning_effort(self) -> str:
        try:
            value = str(self.bot.config.get("llm.reasoning_effort", "low") or "low").lower()
        except Exception:
            value = "low"
        return value if value in {"low", "medium", "high"} else "low"

    def _clock(self, now=None) -> float:
        if now is not None:
            return float(now)
        if self._now is not None:
            return float(self._now())
        return time.time()

    def _interval(self) -> float:
        lo = _number(self.bot.config, "life.activity_min_hours", 2.0) * 3600.0
        hi = _number(self.bot.config, "life.activity_max_hours", 4.0) * 3600.0
        if hi < lo:
            lo, hi = hi, lo
        if hi <= 0:
            return 3600.0
        try:
            return float(self._rng.uniform(lo, hi))
        except Exception:
            return (lo + hi) / 2.0

    def _backoff(self, failures: int) -> float:
        try:
            hours = max(0.5, min(4.0, 0.5 * (2 ** max(0, int(failures) - 1))))
        except Exception:
            hours = 1.0
        return hours * 3600.0

    # ------------------------------------------------------------ 持久化
    @property
    def state_path(self) -> str:
        return self._state_path

    @staticmethod
    def _empty_state(now: float) -> dict:
        return {
            "version": STATE_VERSION,
            "created_at": now,
            "updated_at": now,
            "next_at": 0.0,
            "running": False,
            "failure_count": 0,
            "last_activity_at": 0.0,
            "last_action": "",
            "last_status": "",
            "last_reason": "",
            "activity": {},
            "last_shared": {},
            "last_conversation": {},
            "last_global_share": 0.0,
            "last_qzone_share": 0.0,
            "qzone_items": [],
            "candidates": [],
            "history": [],
        }

    def _load(self, now: float) -> dict:
        if self._state is not None:
            return self._state
        state = self._empty_state(now)
        dirty = False
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
                raise ValueError("unknown autonomy state")
            for key, default in state.items():
                value = raw.get(key, default)
                if isinstance(default, dict) and not isinstance(value, dict):
                    value = default
                if isinstance(default, list) and not isinstance(value, list):
                    value = default
                state[key] = value
        except FileNotFoundError:
            # 第一次运行：把随机等待的 next_at 落盘，重启不能当成“已到期补发”。
            dirty = True
        except Exception as exc:
            log.warn(f"自主活动状态读取失败，按新状态处理：{type(exc).__name__} {exc}")
            state = self._empty_state(now)
            dirty = True

        # 重启恢复：不补发、不重复执行。中断的 running 明确记为 abandoned。
        activity = state.get("activity") if isinstance(state.get("activity"), dict) else {}
        if state.get("running") or activity.get("status") == "running":
            activity["status"] = "abandoned"
            activity["reason"] = "运行中重启，结果未确认，不补发"
            activity["finished_at"] = now
            state["activity"] = activity
            state["running"] = False
            state["last_status"] = "abandoned"
            state["last_reason"] = "重启恢复"
            state["next_at"] = now + self._interval()
            dirty = True
        elif float(state.get("next_at") or 0) <= now:
            # 离线期间错过的那次机会直接作废，不集中补做。
            state["next_at"] = now + self._interval()
            dirty = True
        if not state.get("next_at"):
            state["next_at"] = now + self._interval()
            dirty = True
        self._state = state
        if dirty:
            self._save()
        return state

    def _save(self) -> None:
        state = self._state
        if state is None:
            return
        state["version"] = STATE_VERSION
        state["updated_at"] = self._clock()
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")) + "\n"
        directory = os.path.dirname(self._state_path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".autonomy-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self._state_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            log.warn(f"自主活动状态落盘失败：{type(exc).__name__} {exc}")

    # ------------------------------------------------------------ 公开快照
    def snapshot(self) -> dict:
        now = self._clock()
        with self._state_lock:
            state = copy.deepcopy(self._load(now))
        nxt = float(state.get("next_at") or 0)
        candidates = [item for item in state.get("candidates", [])
                      if isinstance(item, dict) and float(item.get("expires_at") or 0) > now]
        return {
            "enabled": self.enabled,
            "path": self._state_path,
            "running": bool(state.get("running")),
            "next_at": nxt,
            "next_in_sec": max(0, int(nxt - now)) if nxt else None,
            "last_activity_at": float(state.get("last_activity_at") or 0),
            "last_action": str(state.get("last_action") or ""),
            "last_status": str(state.get("last_status") or ""),
            "last_reason": str(state.get("last_reason") or ""),
            "failure_count": int(state.get("failure_count") or 0),
            "activity": copy.deepcopy(state.get("activity") or {}),
            "share": {
                "last_global_share": float(state.get("last_global_share") or 0),
                "last_qzone_share": float(state.get("last_qzone_share") or 0),
                "last_shared": copy.deepcopy(state.get("last_shared") or {}),
                "candidates": len(candidates),
            },
            "history": copy.deepcopy((state.get("history") or [])[-5:]),
        }

    # ------------------------------------------------------------ 分享协调
    def share_candidate(self, session_id: str, query: str = "") -> dict | None:
        """返回与当前话题相关、尚未在本会话发过的候选；没有就 None。"""
        if not self.enabled:
            return None
        now = self._clock()
        session = _one_line(session_id, 120)
        if not session:
            return None
        text = _one_line(query, 500)
        if not text:
            try:
                text = _one_line(self.bot.topic.digest(session, 6), 500)
            except Exception:
                text = ""
        if not text:
            # 没有话题时不用旧候选硬找存在感。
            return None
        with self._state_lock:
            state = self._load(now)
            rows = []
            for item in state.get("candidates", []):
                if not isinstance(item, dict):
                    continue
                if float(item.get("expires_at") or 0) <= now:
                    continue
                if isinstance(item.get("shared"), dict) and item["shared"].get(session):
                    continue
                body = " ".join([
                    str(item.get("summary") or ""),
                    str(item.get("share_point") or ""),
                    " ".join(str(x) for x in (item.get("topics") or [])),
                ])
                score = _overlap(text, body)
                if score <= 0:
                    continue
                rows.append((score, float(item.get("created_at") or 0), item))
            if not rows:
                return None
            rows.sort(key=lambda pair: (-pair[0], -pair[1], str(pair[2].get("experience_id"))))
            return copy.deepcopy(rows[0][2])

    def can_share(self, session_id: str, *, now=None) -> bool:
        """统一对外表达冷却：本会话 + 全局 + 最近刚聊过。

        直接回复不经过这里，所以普通对话照常；这里只决定要不要**另起**话题。
        时间统一按调用时刻（一次活动结束时再排下次），不会用开始时间提前放行。
        """
        ts = self._clock(now)
        session = _one_line(session_id, 120)
        if not session:
            return False
        with self._state_lock:
            state = self._load(ts)
            gap = _number(self.bot.config, "life.share_gap_hours", 3.0) * 3600.0
            global_gap = _number(self.bot.config, "life.global_share_gap_hours", 1.0) * 3600.0
            last = float((state.get("last_shared") or {}).get(session) or 0)
            if gap > 0 and last and ts - last < gap:
                return False
            last_all = float(state.get("last_global_share") or 0)
            if global_gap > 0 and last_all and ts - last_all < global_gap:
                return False
            # 刚和这个会话聊过就先别提旧事另开话题；这个信号由真实对话路径
            # 调 note_conversation 落盘，避免把主动分享当成又一次闲聊。
            recent_gap = _number(
                self.bot.config, "life.recent_conversation_gap_min", 15.0) * 60.0
            last_conv = float((state.get("last_conversation") or {}).get(session) or 0)
            if recent_gap > 0 and last_conv and ts - last_conv < recent_gap:
                return False
            return True

    def note_shared(self, session_id: str, experience_id: str = "", *, now=None) -> None:
        """成功发出后才计数；失败/模型反悔不能提前消耗冷却。"""
        ts = self._clock(now)
        session = _one_line(session_id, 120)
        if not session:
            return
        with self._state_lock:
            state = self._load(ts)
            state.setdefault("last_shared", {})[session] = ts
            state["last_global_share"] = ts
            exp = _one_line(experience_id, 120)
            if exp:
                for item in state.get("candidates", []):
                    if isinstance(item, dict) and item.get("experience_id") == exp:
                        item.setdefault("shared", {})[session] = ts
                        item["last_shared_at"] = ts
            self._prune_share_locked(state, ts)
            self._save()

    def note_conversation(self, session_id: str, *, now=None) -> None:
        """记录最近一次普通交流，作为主动开口前的“刚聊过”信号。"""
        ts = self._clock(now)
        session = _one_line(session_id, 120)
        if not session:
            return
        with self._state_lock:
            state = self._load(ts)
            state.setdefault("last_conversation", {})[session] = ts
            cutoff = ts - 7 * 86400
            state["last_conversation"] = {
                key: value for key, value in state["last_conversation"].items()
                if float(value or 0) >= cutoff
            }
            self._save()

    def _prune_share_locked(self, state: dict, now: float) -> None:
        cutoff = now - 30 * 86400
        state["last_shared"] = {
            key: value for key, value in (state.get("last_shared") or {}).items()
            if float(value or 0) >= cutoff
        }
        live = []
        for item in state.get("candidates", []):
            if not isinstance(item, dict):
                continue
            if float(item.get("expires_at") or 0) <= now:
                continue
            live.append(item)
        state["candidates"] = live[-_MAX_CANDIDATES:]

    # ------------------------------------------------------------ 时钟准入
    def _ha_allowed(self) -> bool:
        runtime = getattr(self.bot, "ha_runtime", None)
        if runtime is None:
            return True
        check = getattr(runtime, "can_mutate", None)
        if not callable(check):
            return True
        try:
            return bool(check())
        except Exception:
            return False

    def _tick_lock_for_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._tick_lock is None or self._tick_loop is not loop:
            self._tick_lock = asyncio.Lock()
            self._tick_loop = loop
        return self._tick_lock

    async def tick(self, *, now=None) -> dict:
        """到点后执行一次有限动作；其余时间立即返回。"""
        ts = self._clock(now)
        if not self.enabled:
            return {"ok": False, "reason": "autonomy-disabled"}
        if not self._ha_allowed():
            return {"ok": False, "reason": "ha-not-admitted"}
        async with self._tick_lock_for_loop():
            with self._state_lock:
                state = self._load(ts)
                if state.get("running"):
                    return {"ok": False, "reason": "already-running"}
                next_at = float(state.get("next_at") or 0)
                if next_at and ts < next_at:
                    return {"ok": False, "reason": "not-due", "next_at": next_at}
            return await self._run_due(ts)

    async def _run_due(self, ts: float) -> dict:
        with self._state_lock:
            state = self._load(ts)
            state["running"] = True
            state["activity"] = {
                "id": f"act_{_source_hash(ts, getattr(self._rng, 'random', random.random)())[:16]}",
                "status": "running",
                "started_at": ts,
                "motivation": "",
                "steps": [],
                "next_at": 0.0,
            }
            self._save()

        plan: dict = {"action": "skip"}
        status, reason, failure = "skipped", "", False
        try:
            plan = await self._plan(ts)
            action = self._normalize_action(plan.get("action"))
            if action == "skip":
                reason = _one_line(plan.get("reason") or "这次不想动", 200)
            elif action not in self._available_actions(ts):
                reason = f"动作当前不可用：{action}"
            else:
                evidence = await self._execute(action, plan, ts)
                if evidence.get("skip"):
                    reason = _one_line(evidence.get("reason") or "没有实际资料", 200)
                elif not evidence.get("ok"):
                    status, failure = "failed", True
                    reason = _one_line(evidence.get("reason") or "平台没有确认结果", 300)
                else:
                    done = await self._commit(ts, plan, evidence)
                    status = "completed" if done else "failed"
                    reason = "" if done else "结果未形成可记录的的经历"
                    failure = not done
        except asyncio.CancelledError:
            self._finish(ts, plan, "abandoned", "任务被取消，结果未确认", failure=False)
            raise
        except Exception as exc:
            status, failure = "failed", True
            reason = f"{type(exc).__name__}: {exc}"
            log.warn(f"自主活动失败（已退避）：{reason[:200]}")
        self._finish(ts, plan, status, reason, failure=failure)
        return {"ok": status == "completed", "status": status, "action":
                self._normalize_action(plan.get("action")), "reason": reason}

    def _finish(self, ts: float, plan: dict, status: str, reason: str,
                *, failure: bool) -> None:
        # 下一次活动从**本次结束时刻**算起：慢搜索/读页面花掉的几分钟不能变成
        # 立刻补跑下一个机会。
        end = max(float(ts), self._clock())
        with self._state_lock:
            state = self._load(end)
            failures = int(state.get("failure_count") or 0)
            if failure:
                failures += 1
            elif status in {"completed", "skipped"}:
                failures = 0
            state["failure_count"] = failures
            state["running"] = False
            state["last_activity_at"] = end
            state["last_action"] = self._normalize_action(plan.get("action"))
            state["last_status"] = status
            state["last_reason"] = _one_line(reason, 300)
            if failure:
                state["next_at"] = end + self._backoff(failures)
            else:
                state["next_at"] = end + self._interval()
            activity = state.get("activity") if isinstance(state.get("activity"), dict) else {}
            activity.update({
                "status": status,
                "reason": _one_line(reason, 300),
                "finished_at": end,
                "next_at": state["next_at"],
            })
            state["activity"] = activity
            state.setdefault("history", []).append({
                "at": end,
                "action": state["last_action"],
                "status": status,
                "reason": _one_line(reason, 200),
            })
            state["history"] = state["history"][-_MAX_HISTORY:]
            self._save()

    # ------------------------------------------------------------ 规划
    @staticmethod
    def _normalize_action(value) -> str:
        action = str(value or "").strip().lower()
        return action if action in ACTIONS else "skip"

    def _qzone_gates(self) -> bool:
        try:
            return (self.bot.config.get("qzone.enabled", False) is True
                    and self.bot.config.get("qzone.proactive_enabled", False) is True
                    and getattr(self.bot, "qzone", None) is not None)
        except Exception:
            return False

    def _qzone_ready(self, ts: float) -> bool:
        if not self._qzone_gates():
            return False
        with self._state_lock:
            state = self._load(ts)
            last = float(state.get("last_qzone_share") or 0)
        gap = _number(self.bot.config, "life.qzone_gap_hours", 8.0) * 3600.0
        return not (gap > 0 and last and ts - last < gap)

    def _qzone_cap(self):
        """同一 Qzone 运行时只签发一个 capability，避免长期活动累积对象。"""
        qzone = getattr(self.bot, "qzone", None)
        if qzone is None:
            return None
        cached = self._qzone_capability
        if cached is not None and getattr(cached, "_qzone", None) is qzone:
            return cached
        issue = getattr(qzone, "issue_autonomy_capability", None)
        if not callable(issue):
            return None
        self._qzone_capability = issue("life.autonomy")
        return self._qzone_capability

    def _available_actions(self, ts: float) -> set[str]:
        available = {"reflect", "skip"}
        try:
            if str(self.bot.config.get("tools.search_provider", "bing")).lower() != "none":
                available.add("search")
        except Exception:
            available.add("search")
        try:
            if self.bot.config.get("tools.bili_scroll_enabled", True):
                available.add("bili")
        except Exception:
            available.add("bili")
        if self._qzone_gates():
            available.add("qzone_read")
            if self._qzone_configured_actions():
                available.add("qzone_interact")
            if self._qzone_ready(ts):
                available.add("qzone_post")
        return available

    def _qzone_configured_actions(self) -> list[str]:
        """只看显式配置的真实后端动作，不替用户猜 like/repost 接口名。"""
        out = []
        for name in ("reply", "like", "repost"):
            try:
                value = str(self.bot.config.get(f"qzone.actions.{name}", "") or "").strip()
            except Exception:
                value = ""
            if value:
                out.append(name)
        return out

    def _qzone_sources(self) -> list[str]:
        sources = ["own"]
        try:
            if str(self.bot.config.get("qzone.actions.feed", "") or "").strip():
                sources.append("feed")
        except Exception:
            pass
        return sources

    def _hobbies(self, limit: int = 6) -> list[dict]:
        hobbies = getattr(self.bot, "hobbies", None)
        rows = getattr(hobbies, "items", None)
        if not isinstance(rows, (list, tuple)):
            return []
        out = []
        for row in rows[:max(0, int(limit))]:
            if not isinstance(row, dict):
                continue
            name = _one_line(row.get("name"), 60)
            if not name:
                continue
            out.append({
                "name": name,
                "kind": _one_line(row.get("kind"), 20),
                "last_news": _one_line(row.get("last_news"), 200),
            })
        return out

    def _skills(self, limit: int = 8) -> list[dict]:
        """真实养成快照：领域了解与实际练习分开，不把读过一次说成熟练。"""
        if not self._growth_enabled():
            return []
        store = self._store()
        reader = getattr(store, "growth_snapshot", None) if store is not None else None
        if not callable(reader):
            return []
        try:
            snapshot = reader() or {}
        except Exception:
            return []
        rows = snapshot.get("skills") if isinstance(snapshot, dict) else []
        out = []
        for row in (rows or [])[:max(0, int(limit))]:
            if not isinstance(row, dict):
                continue
            topic = _one_line(row.get("topic") or row.get("name"), 60)
            if not topic:
                continue
            out.append({
                "topic": topic,
                "understanding_count": int(row.get("understanding_count") or 0),
                "practice_count": int(row.get("practice_count") or 0),
                "stage": _one_line(row.get("stage") or row.get("stage_reason"), 120),
            })
        return out

    def _persona_context(self, extra: str = "", limit: int = _MAX_ROLE_CHARS) -> str:
        """用 bot.persona.build 生成角色上下文；失败只退回空串，不换通用系统提示。"""
        persona = getattr(self.bot, "persona", None)
        build = getattr(persona, "build", None)
        if not callable(build):
            return ""
        text = ""
        try:
            text = build(group_id=None, sender="", extra=extra)
        except TypeError:
            try:
                text = build(extra=extra)
            except Exception:
                text = ""
        except Exception:
            text = ""
        text = _clip(text, limit).rstrip()

        # build() 可能很长；上面按 head 截断会把「学来的说话方式」切掉。
        # 自主活动/空间发布也吃这一份角色上下文，所以截断后单独把风格段补回。
        style_block = ""
        style_fn = getattr(persona, "style_block", None)
        if callable(style_fn):
            try:
                style_block = str(style_fn(1200) or "").strip()
            except Exception:
                style_block = ""
        if style_block and "学来的说话方式" not in text:
            room = max(600, int(limit) - len(style_block))
            text = _clip(text, room).rstrip()
            text = (text + "\n\n" + style_block).strip()
        return text

    def _plan_status(self, *, hobbies, skills, goals, self_entries,
                     recent) -> str:
        lines: list[str] = ["## 你现在的状态（自主时间，不是群聊回复）"]
        if hobbies:
            lines.append("爱好：" + "、".join(
                f"{row['name']}（{row['kind'] or '兴趣'}）" for row in hobbies))
        if skills:
            lines.append("养成：" + "；".join(
                f"{row['topic']} 了解{row['understanding_count']}次/"
                f"练习{row['practice_count']}次"
                for row in skills[:6]))
        if goals:
            lines.append("手头目标：" + "；".join(
                f"[{row['id']}] {row['text']}（v{row['version']}，{row['status']}，"
                f"下一步：{row['next_step'] or '未定'}）"
                for row in goals[:6]))
        if self_entries:
            lines.append("当前自我：" + "；".join(
                f"[{row['id']}] {row['text']}" for row in self_entries[:6]))
        if recent:
            lines.append("最近真实经历：" + "；".join(
                _one_line(row.get("summary"), 120) for row in recent[:4]))
        if not self._evolution_enabled():
            lines.append("（养成演变已关闭：可以做事和记录经历，但不要改目标/自我条目。）")
        if not self._growth_enabled():
            lines.append("（成长记账已关闭：不要声称技能或练习进度变化。）")
        return "\n".join(lines)[:1800]

    def _plan_context(self, ts: float) -> dict:
        store = self._store()
        recent, goals, self_entries = [], [], []
        evolution = self._evolution_enabled()
        if store is not None:
            try:
                recent = store.recent_experiences(8)
            except Exception:
                recent = []
            if evolution:
                try:
                    goals = store.goals(active_only=False)[:6]
                except Exception:
                    goals = []
                try:
                    self_entries = store.self_entries()[:8]
                except Exception:
                    self_entries = []
        hobbies = self._hobbies()
        skills = self._skills()
        recent_rows = [
            {"id": row.get("id"), "summary": row.get("summary"),
             "reaction": row.get("reaction"), "topics": row.get("topics"),
             "platform": row.get("platform"), "ts": row.get("ts")}
            for row in recent
        ]
        goal_rows = [
            {"id": row.get("id"), "type": row.get("type"), "text": row.get("text"),
             "status": row.get("status"), "version": int(row.get("version") or 1),
             "progress": row.get("progress"), "next_step": row.get("next_step")}
            for row in goals
        ]
        self_rows = [
            {"id": row.get("id"), "category": row.get("category"),
             "version": int(row.get("version") or 1), "text": row.get("text")}
            for row in self_entries
        ]
        status = self._plan_status(
            hobbies=hobbies, skills=skills, goals=goal_rows,
            self_entries=self_rows, recent=recent_rows)
        role = self._persona_context(status)
        with self._state_lock:
            state = self._load(ts)
            qzone_items = copy.deepcopy(state.get("qzone_items") or [])
        return {
            "now": ts,
            "role": role,
            "role_context": role,
            "persona": role,
            "evolution_enabled": evolution,
            "growth_enabled": self._growth_enabled(),
            "available_actions": sorted(self._available_actions(ts)),
            "hobbies": hobbies,
            "skills": skills,
            "recent_experiences": recent_rows,
            "goals": goal_rows,
            "self_entries": self_rows,
            "qzone_sources": self._qzone_sources(),
            "qzone_interact": self._qzone_configured_actions(),
            "qzone_items": qzone_items[-8:],
        }

    async def _plan(self, ts: float) -> dict:
        context = self._plan_context(ts)
        if self._planner is not None:
            value = self._planner(context)
            if inspect.isawaitable(value):
                value = await value
            if not isinstance(value, dict):
                raise ValueError("计划回调没有返回对象")
            return value
        instructions = (
            "## 自主时间\n"
            "你现在有一段没人打扰的自主时间。上面的角色、爱好、真实养成、目标版本和"
            "最近经历都是你当前的状态；根据这些决定要不要做一件小事。\n"
            "只输出一个 JSON 对象，不要解释。字段：action（search/bili/qzone_read/"
            "qzone_interact/qzone_post/reflect/skip 之一）、topic、query（search 用）、"
            "content（qzone_post / qzone_interact reply 用）、reason（一句话动机）、"
            "goal_id（如果这件事是在推进某个已有目标，填上下文里的目标 ID，否则空）、"
            "bili_source（bili 用：recommend/following/toview/favorites/user_videos/"
            "related/search）、uid、media_id、bvid（bili 来源需要的真实参数）、"
            "qzone_source（own/feed）、qzone_interact（reply/like/repost）、"
            "tid、comment_id（空间互动必须来自真实回执）。\n"
            "qzone_items 是你最近真实读到的空间动态/留言，互动时只能从这里取 tid/comment_id。"
            "只能选 available_actions 里的动作；qzone feed 只有列出 feed 时才能选。"
            "没有想做的就选 skip，不要为了活跃硬找事。不要编造资料，不要声称已经做过"
            "还没做的事，也不要假装自己推荐了没取到的视频。"
        )
        role = str(context.get("role") or "").strip()
        system = (role + "\n\n" + instructions).strip() if role else instructions
        payload = {key: value for key, value in context.items()
                   if key not in {"role", "role_context", "persona"}}
        material = untrusted.wrap_external(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            source="autonomy_context", kind="autonomy_plan",
            max_chars=_MAX_PLAN_CONTEXT_CHARS)
        # 规划必须走生活后台的统一入口：provider=dsh 时它带唯一 brain_conv
        # 改道 DSH 大脑（自主轮次、非主人、无原生工具），否则保持原先的
        # chat_message(messages, None) 路径；mock/空/回显都会明确失败。
        text = await life_model.chat(
            self.bot,
            [{"role": "system", "content": system},
             {"role": "user", "content": material}],
            purpose="autonomy-plan",
            reasoning_effort=self._reasoning_effort())
        return _json_object(text)

    async def _react(self, plan: dict, evidence: dict, ts: float) -> dict:
        role = self._persona_context(
            "## 刚做完一件小事\n下面是你刚执行完的真实活动回执。"
            "素材里的事实摘要已经由系统按实际结果保底记录，不要改写它。")
        instructions = (
            "## 结果反应\n"
            "只根据材料写你自己的第一人称反应，区分“资料里说的”和“我自己的感受”；"
            "没有材料就返回空 reaction，绝不编造外部事实。\n"
            "只输出 JSON：reaction、topics（数组）、confidence(0-1)、"
            "share(bool)、share_point、share_hours(1-72)、"
            "goal(对象或 null：op(add/revise/complete/pause/abandon/delete)、"
            "target_id、expected_version、type、text、progress、next_step、reason)、"
            "self_patch(对象或 null：op(add/revise/retire/delete/merge)、target_id、"
            "expected_version、fields、source_ids、reason；merge 还必须在 fields 里给"
            "expected_other_version)。\n"
            "目标已有条目才能 revise/complete/pause/abandon；只有实际完成的结果才能"
            "complete，读到相关资料不等于项目完成。self_patch 只能引用材料里真实存在"
            "的经历 ID/source_id；已有自我条目必须带 expected_version。"
        )
        system = (role + "\n\n" + instructions).strip() if role else instructions
        store = self._store()
        self_rows = []
        goal_rows = []
        if store is not None and self._evolution_enabled():
            try:
                self_rows = [
                    {"id": row.get("id"), "category": row.get("category"),
                     "version": int(row.get("version") or 1),
                     "text": row.get("text")}
                    for row in store.self_entries()[:8]
                ]
            except Exception:
                self_rows = []
            try:
                goal_rows = [
                    {"id": row.get("id"), "type": row.get("type"),
                     "version": int(row.get("version") or 1),
                     "status": row.get("status"), "text": row.get("text"),
                     "progress": row.get("progress"),
                     "next_step": row.get("next_step")}
                    for row in store.goals(active_only=False)[:6]
                ]
            except Exception:
                goal_rows = []
        payload = {
            "plan": {key: plan.get(key) for key in
                     ("action", "topic", "query", "goal_id", "bili_source")},
            "source_id": evidence.get("source_id") or "",
            "materials": copy.deepcopy(evidence.get("materials") or {}),
            "fallback_summary": _one_line(evidence.get("summary"), 1200),
            "self_entries": self_rows,
            "goals": goal_rows,
        }
        material = untrusted.wrap_external(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            source="autonomy_activity", kind="autonomy_result", max_chars=9000)
        try:
            text = await life_model.chat(
                self.bot,
                [{"role": "system", "content": system},
                 {"role": "user", "content": material}],
                purpose="autonomy-react",
                reasoning_effort=self._reasoning_effort())
            return _json_object(text)
        except Exception as exc:
            log.debug(f"自主活动结果反应失败（保留真实经历，不生成分享）："
                      f"{type(exc).__name__} {exc}")
            return {}

    # ------------------------------------------------------------ 执行
    async def _run_sync(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args))

    async def _execute(self, action: str, plan: dict, ts: float) -> dict:
        if action == "search":
            return await self._do_search(plan, ts)
        if action == "bili":
            return await self._do_bili(plan, ts)
        if action == "qzone_read":
            return await self._do_qzone_read(plan, ts)
        if action == "qzone_interact":
            return await self._do_qzone_interact(plan, ts)
        if action == "qzone_post":
            return await self._do_qzone_post(plan, ts)
        if action == "reflect":
            return await self._do_reflect(plan, ts)
        return {"ok": True, "skip": True, "reason": "这次不做"}

    async def _do_search(self, plan: dict, ts: float) -> dict:
        query = _one_line(plan.get("query") or plan.get("topic"), 200)
        if not query:
            return {"ok": False, "reason": "计划里没有具体搜索问题"}
        try:
            data = await self._run_sync(websearch.search, query, self.bot.config)
        except Exception as exc:
            return {"ok": False, "reason": f"搜索失败：{type(exc).__name__} {exc}"}
        if not isinstance(data, dict):
            return {"ok": False, "reason": "搜索没有返回可用结果"}
        results = [row for row in (data.get("results") or []) if isinstance(row, dict)]
        answer = _one_line(data.get("answer"), 1200)
        if not results and not answer:
            return {"ok": False, "reason": _one_line(data.get("error") or "搜索没有结果", 200)}
        rows = []
        for row in results[:3]:
            rows.append({
                "title": _one_line(row.get("title"), 160),
                "url": _one_line(row.get("url"), 500),
                "snippet": _one_line(row.get("snippet") or row.get("desc"), 300),
            })
        first_url = next((row["url"] for row in rows if row["url"]), "")
        page = {}
        if first_url:
            try:
                page = await self._run_sync(websearch.fetch, first_url, 1800)
            except Exception as exc:
                page = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not isinstance(page, dict):
                page = {}
        read_text = _one_line(page.get("text"), 1500) if page.get("ok") else ""
        titles = "；".join(
            filter(None, [f"{row['title']} {row['snippet']}".strip() for row in rows]))
        summary = f"搜了「{query}」。"
        if answer:
            summary += f"搜索摘要：{answer[:500]}。"
        if titles:
            summary += f"结果：{titles[:600]}。"
        if read_text:
            summary += f"读到的正文：{read_text[:700]}。"
        if not (answer or titles or read_text):
            return {"ok": False, "reason": "搜索没有可用于记录的实际内容"}
        if first_url:
            summary += f"（来源：{first_url}）"
        evidence = {
            "ok": True,
            "platform": "web",
            "source_id": f"web:{_source_hash(query, first_url, titles)}",
            "summary": _one_line(summary, 1600),
            "observed_level": "page_text" if read_text else "search_results",
            "practice_mode": "understanding",
            "practice_topic": _one_line(plan.get("topic") or query, 80),
            "topics": [_one_line(plan.get("topic") or query, 80)],
            "materials": {"query": query, "answer": answer,
                          "results": rows, "page": {
                              key: page.get(key) for key in ("ok", "title", "url", "text", "error")
                          } if page else {}},
        }
        return evidence

    async def _discover_bili(self, plan: dict) -> tuple[dict, str]:
        """惰性接新的 bili_discovery；接口没落盘时返回明确失败，不假装取到视频。"""
        source = _one_line(plan.get("bili_source") or "search", 40).lower()
        allowed = {"recommend", "following", "toview", "favorites",
                   "user_videos", "related", "search"}
        if source not in allowed:
            source = "search"
        query = _one_line(plan.get("query") or plan.get("topic"), 200)
        try:
            from . import bili_discovery
        except Exception as exc:
            return ({"ok": False, "source": source, "items": [],
                     "reason": f"B 站来源接口不可用：{type(exc).__name__}"}, source)
        discover = getattr(bili_discovery, "discover", None)
        if not callable(discover):
            return ({"ok": False, "source": source, "items": [],
                     "reason": "B 站来源接口缺少 discover"}, source)
        try:
            value = discover(
                self.bot, source, query=query,
                uid=_safe_int(plan.get("uid"), 0),
                media_id=_safe_int(plan.get("media_id"), 0),
                bvid=_one_line(plan.get("bvid"), 40),
                limit=12)
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            return ({"ok": False, "source": source, "items": [],
                     "reason": f"取 B 站候选失败：{type(exc).__name__} {exc}"}, source)
        if not isinstance(value, dict):
            return ({"ok": False, "source": source, "items": [],
                     "reason": "B 站来源没有返回对象"}, source)
        data = dict(value)
        data["source"] = _one_line(data.get("source") or source, 40)
        rows = [row for row in (data.get("items") or []) if isinstance(row, dict)]
        data["items"] = rows
        return data, data["source"]

    async def _do_bili(self, plan: dict, ts: float) -> dict:
        discovered, source = await self._discover_bili(plan)
        if not discovered.get("ok") or not discovered.get("items"):
            reason = _one_line(discovered.get("reason") or "这轮 B 站没有真实候选", 300)
            # 明确退回普通搜索，并在记录里保留这次回退；不谎称推荐了视频。
            fallback_plan = {
                "query": _one_line(plan.get("query") or plan.get("topic"), 200)
                or "今天有什么新鲜事",
                "topic": _one_line(plan.get("topic"), 80),
            }
            evidence = await self._do_search(fallback_plan, ts)
            if evidence.get("ok"):
                materials = evidence.setdefault("materials", {})
                materials["fallback_from"] = "bilibili"
                materials["bili_source"] = source
                materials["fallback_reason"] = reason
                evidence["practice_topic"] = evidence.get("practice_topic") or \
                    _one_line(plan.get("topic"), 80)
            return evidence
        topic = _one_line(plan.get("query") or plan.get("topic"), 200)
        kwargs = {"topic": topic or "", "want": 1}
        if _accepts_kwarg(bili_scroll.run_round, "candidates"):
            kwargs["candidates"] = discovered["items"]
        if _accepts_kwarg(bili_scroll.run_round, "source"):
            kwargs["source"] = source
        try:
            result = await bili_scroll.run_round(self.bot, **kwargs)
        except Exception as exc:
            return {"ok": False, "reason": f"刷 B 站失败：{type(exc).__name__} {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (result or {}).get("reason") if isinstance(result, dict) else ""
            return {"ok": False, "reason": _one_line(reason or "刷 B 站没有完成", 300)}
        notes = [row for row in (result.get("notes") or []) if isinstance(row, dict)]
        if not notes:
            return {"ok": True, "skip": True, "reason": "这轮没有写出真实笔记"}
        note = notes[0]
        bvid = _one_line(note.get("bvid"), 40)
        title = _one_line(note.get("title"), 200)
        gist = _one_line(note.get("gist"), 600)
        url = _one_line(note.get("url"), 500)
        level = _one_line(note.get("watched_level"), 40) or "title_only"
        if not bvid:
            return {"ok": False, "reason": "B 站回执没有视频 ID，结果未确认"}
        summary = f"刷 B 站（来源 {source}）看到《{title}》（{url}）。{gist}"
        return {
            "ok": True,
            "platform": "bilibili",
            "source_id": f"bilibili:{bvid}",
            "summary": _one_line(summary, 1600),
            "observed_level": level,
            "practice_mode": "understanding",
            "practice_topic": _one_line(plan.get("topic") or title, 80),
            "topics": [_one_line(plan.get("topic") or title, 80)],
            "materials": {"notes": notes[:3], "subtitle": result.get("subtitle") or {},
                          "reacted": result.get("reacted") or {},
                          "bili_source": source,
                          "candidates": [{key: row.get(key) for key in
                                          ("bvid", "title", "author", "url", "play",
                                           "duration")}
                                         for row in discovered["items"][:6]]},
        }

    async def _do_qzone_read(self, plan: dict, ts: float) -> dict:
        qzone = getattr(self.bot, "qzone", None)
        if qzone is None or not self._qzone_gates():
            return {"ok": False, "reason": "空间自主读取未启用"}
        source = _one_line(plan.get("qzone_source") or plan.get("source") or "own",
                           20).lower()
        if source not in {"own", "feed"}:
            source = "own"
        if source == "feed" and "feed" not in self._qzone_sources():
            return {"ok": False,
                    "reason": "未配置已确认的 qzone.actions.feed 好友动态接口"}
        capability = self._qzone_cap()
        if capability is None:
            return {"ok": False, "reason": "空间自主能力不可用"}
        try:
            result = await qzone.autonomy_read(
                capability, limit=5, source=source, include_comments=True)
        except Exception as exc:
            return {"ok": False, "reason": f"读空间失败：{type(exc).__name__} {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "reason": _one_line(
                (result or {}).get("reason") or "空间没有确认读取成功", 300)}
        items = [row for row in (result.get("items") or []) if isinstance(row, dict)]
        if not items:
            return {"ok": True, "skip": True,
                    "reason": "空间的自己动态暂时没有可读内容" if source == "own"
                    else "好友空间暂时没有可读动态"}
        lines = []
        tids = []
        for row in items[:3]:
            tid = _one_line(row.get("tid"), 80)
            text = _one_line(row.get("content"), 400)
            if tid:
                tids.append(tid)
            if text:
                lines.append(text)
            comments = [item for item in (row.get("comments") or [])
                        if isinstance(item, dict)]
            for comment in comments[:2]:
                ctext = _one_line(comment.get("text"), 200)
                if ctext:
                    lines.append("留言：" + ctext)
        if not lines:
            return {"ok": True, "skip": True, "reason": "空间回执没有正文"}
        if source == "feed":
            summary = "翻了空间好友动态：" + "；".join(lines)
            level = "friend_feed"
            practice_topic = "QQ 空间好友动态"
        else:
            summary = "翻了自己的空间近况（含留言）：" + "；".join(lines)
            level = "space_text"
            practice_topic = "QQ 空间近况"
        self._remember_qzone_items(ts, source, items)
        return {
            "ok": True,
            "platform": "qzone",
            "source_id": f"qzone:{source}:{_source_hash(','.join(tids), *lines)}",
            "summary": _one_line(summary, 1600),
            "observed_level": level,
            "practice_mode": "understanding",
            "practice_topic": practice_topic,
            "topics": ["QQ空间"],
            "materials": {"source": source, "items": items[:5]},
        }

    async def _do_qzone_interact(self, plan: dict, ts: float) -> dict:
        qzone = getattr(self.bot, "qzone", None)
        if qzone is None or not self._qzone_gates():
            return {"ok": False, "reason": "空间自主互动未启用"}
        name = _one_line(plan.get("qzone_interact") or plan.get("interact")
                         or plan.get("interact_name"), 20).lower()
        tid = _one_line(plan.get("tid"), 120)
        comment_id = _one_line(plan.get("comment_id"), 120)
        content = _one_line(plan.get("content") or plan.get("text"), 1000)
        if not tid:
            return {"ok": False, "reason": "空间互动缺少真实 tid，未调用后端"}
        if name == "reply" and not comment_id:
            return {"ok": False, "reason": "回复留言缺少真实 comment_id，未调用后端"}
        if name == "reply" and not content:
            return {"ok": False, "reason": "回复留言缺少正文"}
        capability = self._qzone_cap()
        if capability is None:
            return {"ok": False, "reason": "空间自主能力不可用"}
        try:
            result = await qzone.autonomy_interact(
                capability, name, tid=tid, comment_id=comment_id, content=content)
        except Exception as exc:
            return {"ok": False, "reason": f"空间互动失败：{type(exc).__name__} {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "reason": _one_line(
                (result or {}).get("reason") or "空间没有确认互动成功", 300)}
        if name == "reply":
            summary = f"在空间回复了一条留言：{content}"
            action_word = "reply"
        elif name == "repost":
            summary = "转发了一条空间动态" + (f"：{content}" if content else "")
            action_word = "repost"
        else:
            summary = "给一条空间动态点了赞"
            action_word = "like"
        return {
            "ok": True,
            "platform": "qzone",
            "source_id": f"qzone:{action_word}:{_source_hash(tid, comment_id)}",
            "summary": _one_line(summary, 600),
            "observed_level": "space_action",
            "practice_mode": "",
            "practice_topic": "",
            "topics": ["QQ空间"],
            "materials": {"interact": action_word, "tid": tid,
                          "comment_id": comment_id, "content": content,
                          "receipt": result.get("data") or {}},
        }

    def _remember_qzone_items(self, ts: float, source: str, items: list[dict]) -> None:
        """保存最近真实读到的动态/留言，供下一次规划互动时取真实 ID。"""
        rows = []
        for row in (items or [])[:6]:
            if not isinstance(row, dict):
                continue
            tid = _one_line(row.get("tid"), 120)
            if not tid:
                continue
            comments = []
            for comment in (row.get("comments") or [])[:6]:
                if not isinstance(comment, dict):
                    continue
                cid = _one_line(comment.get("id") or comment.get("comment_id"), 120)
                if not cid:
                    continue
                comments.append({
                    "id": cid,
                    "nick": _one_line(comment.get("nick"), 80),
                    "text": _one_line(comment.get("text"), 300),
                })
            rows.append({
                "source": source,
                "tid": tid,
                "content": _one_line(row.get("content"), 400),
                "comments": comments,
                "read_at": max(float(ts), self._clock()),
            })
        if not rows:
            return
        with self._state_lock:
            state = self._load(max(float(ts), self._clock()))
            existing = [row for row in (state.get("qzone_items") or [])
                        if isinstance(row, dict)
                        and (row.get("source"), row.get("tid")) not in
                        {(item["source"], item["tid"]) for item in rows}]
            state["qzone_items"] = (existing + rows)[-8:]
            self._save()

    async def _do_qzone_post(self, plan: dict, ts: float) -> dict:
        qzone = getattr(self.bot, "qzone", None)
        if qzone is None or not self._qzone_gates():
            return {"ok": False, "reason": "空间自主发布未启用"}
        if not self._qzone_ready(ts):
            return {"ok": False, "reason": "空间发布冷却未到"}
        content = _one_line(plan.get("content") or plan.get("text"), 1000)
        if not content:
            return {"ok": False, "reason": "计划里没有可发布的正文"}
        capability = self._qzone_cap()
        if capability is None:
            return {"ok": False, "reason": "空间自主能力不可用"}
        try:
            result = await qzone.autonomy_post(capability, content)
        except Exception as exc:
            return {"ok": False, "reason": f"空间发布失败：{type(exc).__name__} {exc}"}
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "reason": _one_line(
                (result or {}).get("reason") or "空间没有确认发布成功", 300)}
        tid = _one_line(result.get("tid"), 80)
        done_at = max(float(ts), self._clock())
        with self._state_lock:
            state = self._load(done_at)
            state["last_qzone_share"] = done_at
            self._save()
        return {
            "ok": True,
            "platform": "qzone",
            "source_id": f"qzone:post:{tid or _source_hash(content)}",
            "summary": f"自己在空间发了：{content}",
            "observed_level": "post",
            "practice_mode": "",
            "practice_topic": "",
            "topics": ["QQ空间"],
            "goal_completed": True,
            "materials": {"content": content, "tid": tid,
                          "receipt": result.get("data") or {}},
        }

    async def _do_reflect(self, plan: dict, ts: float) -> dict:
        store = self._store()
        if store is None:
            return {"ok": False, "reason": "没有生活状态可反思"}
        try:
            recent = store.recent_experiences(8)
        except Exception as exc:
            return {"ok": False, "reason": f"读经历失败：{type(exc).__name__} {exc}"}
        recent = [row for row in recent if isinstance(row, dict)]
        if not recent:
            return {"ok": True, "skip": True, "reason": "还没有可依据的真实经历"}
        self_rows, goal_rows = [], []
        if self._evolution_enabled():
            try:
                self_rows = [
                    {"id": row.get("id"), "category": row.get("category"),
                     "version": int(row.get("version") or 1),
                     "text": row.get("text")}
                    for row in store.self_entries()[:8]
                ]
            except Exception:
                self_rows = []
            try:
                goal_rows = [
                    {"id": row.get("id"), "type": row.get("type"),
                     "version": int(row.get("version") or 1),
                     "status": row.get("status"), "text": row.get("text"),
                     "progress": row.get("progress"),
                     "next_step": row.get("next_step")}
                    for row in store.goals(active_only=False)[:6]
                ]
            except Exception:
                goal_rows = []
        return {
            "ok": True,
            "platform": "local",
            "source_id": "",
            "summary": "",
            "observed_level": "",
            "practice_mode": "",
            "practice_topic": "",
            "topics": [],
            "reflect": True,
            "materials": {"experiences": [
                {"id": row.get("id"), "source_id": row.get("root_source_id"),
                 "summary": row.get("summary"), "reaction": row.get("reaction"),
                 "topics": row.get("topics")}
                for row in recent
            ], "self_entries": self_rows, "goals": goal_rows,
                "skills": self._skills()},
        }

    # ------------------------------------------------------------ 落库
    def _store(self):
        store = getattr(self.bot, "life_store", None)
        return store if store is not None else None

    async def _commit(self, ts: float, plan: dict, evidence: dict) -> bool:
        if evidence.get("reflect"):
            reaction = await self._react(plan, evidence, ts)
            allowed = set()
            for row in (evidence.get("materials") or {}).get("experiences", []):
                if not isinstance(row, dict):
                    continue
                for key in ("id", "source_id"):
                    if row.get(key):
                        allowed.add(str(row[key]))
            self._apply_self_patch(reaction, ts, allowed)
            return True

        store = self._store()
        if store is None:
            return False
        reaction = await self._react(plan, evidence, ts)
        # 事实部分只认实际执行回执；模型写的 summary 不再覆盖外部事实。
        summary = _one_line(evidence.get("summary"), 1600)
        if not summary:
            return False
        goal_id = self._plan_goal_id(plan)
        if not goal_id:
            goal = reaction.get("goal")
            if isinstance(goal, dict):
                goal_id = self._valid_goal_id(
                    goal.get("target_id") or goal.get("id"))
        try:
            row = store.record_experience(
                evidence.get("source_id") or f"autonomy:{_source_hash(ts)}",
                summary,
                platform=_one_line(evidence.get("platform") or "local", 40),
                topics=evidence.get("topics") or [],
                reaction=_one_line(reaction.get("reaction"), 800),
                observed_level=_one_line(evidence.get("observed_level"), 80),
                goal_id=goal_id,
                kind="episode",
                confidence=self._confidence(reaction.get("confidence")),
                salience=4 if reaction.get("share") is True else 3,
                now=ts,
            )
        except Exception as exc:
            log.warn(f"自主活动经历写入失败：{type(exc).__name__} {exc}")
            return False
        experience_id = str(row.get("id") or "")
        if not experience_id:
            return False
        fresh = abs(float(row.get("ts") or 0) - ts) < 0.001

        practice_mode = str(evidence.get("practice_mode") or "")
        topic = _one_line(evidence.get("practice_topic"), 80)
        if self._growth_enabled() and practice_mode in {"understanding", "practice"} \
                and topic:
            try:
                store.practice(topic, experience_id=experience_id, mode=practice_mode,
                               outcome="observed", now=ts)
            except Exception as exc:
                log.debug(f"自主活动养成记录失败（不影响经历）：{type(exc).__name__} {exc}")

        if fresh and self._evolution_enabled():
            self._apply_goal(reaction, experience_id, ts, goal_id=goal_id,
                             evidence=evidence)
            self._apply_self_patch(
                reaction, ts,
                {experience_id, str(evidence.get("source_id") or "")} - {""})

        if reaction.get("share") is True:
            self._add_candidate(ts, evidence, reaction, summary, experience_id, row)
        return True

    def _plan_goal_id(self, plan: dict) -> str:
        """计划里给的目标 ID 必须真实存在，才写进经历。"""
        return self._valid_goal_id(plan.get("goal_id"))

    def _valid_goal_id(self, value) -> str:
        wanted = _one_line(value, 120)
        if not wanted:
            return ""
        store = self._store()
        if store is None:
            return ""
        try:
            rows = store.goals(active_only=False)
        except Exception:
            return ""
        return wanted if any(str(row.get("id") or "") == wanted for row in rows) else ""

    @staticmethod
    def _confidence(value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.6
        return max(0.0, min(1.0, number))

    def _apply_goal(self, reaction: dict, experience_id: str, ts: float, *,
                    goal_id: str = "", evidence: dict | None = None) -> None:
        """按 op 更新已有目标；阅读只补 progress，不能把项目当完成。"""
        goal = reaction.get("goal")
        if not isinstance(goal, dict):
            return
        store = self._store()
        if store is None:
            return
        op = _one_line(goal.get("op") or goal.get("operation"), 20).lower()
        if op == "update":
            op = "revise"
        if op not in _GOAL_PATCH_OPS:
            op = "revise" if goal.get("target_id") or goal_id else "add"
        target_id = _one_line(goal.get("target_id") or goal.get("id") or goal_id, 120)
        text = _one_line(goal.get("text") or goal.get("title"), 300)
        evidence = evidence if isinstance(evidence, dict) else {}
        facts = _one_line(evidence.get("summary"), 300)
        progress = _one_line(goal.get("progress"), 300)
        next_step = _one_line(goal.get("next_step"), 300)
        reason = _one_line(goal.get("reason") or "自主活动的真实经历", 300)
        source_ids = [experience_id] if experience_id else []

        rows = []
        try:
            rows = [row for row in store.goals(active_only=False)
                    if isinstance(row, dict)]
        except Exception:
            rows = []
        target = next((row for row in rows
                       if str(row.get("id") or "") == target_id), None)
        if target is None:
            if op != "add":
                log.debug(f"自主目标更新跳过：目标不存在 {target_id}")
                return
            if not text:
                return
            fields = {
                "type": _one_line(goal.get("type") or "task", 20),
                "text": text,
                "next_step": next_step,
                "progress": progress or facts,
            }
            patch_op = "add"
            expected = None
        else:
            current = int(target.get("version") or 1)
            wanted = goal.get("expected_version")
            if wanted is not None:
                try:
                    if int(wanted) != current:
                        log.debug("自主目标更新跳过：版本已变化")
                        return
                except (TypeError, ValueError):
                    return
            expected = current
            fields = {}
            if text:
                fields["text"] = text
            if goal.get("type"):
                fields["type"] = _one_line(goal.get("type"), 20)
            if progress or facts:
                fields["progress"] = progress or facts
            if next_step:
                fields["next_step"] = next_step
            patch_op = op
            if patch_op == "add":
                # 反应里写 add 但目标已存在时，按局部修订处理，不制造重复目标。
                patch_op = "revise"
            if op == "complete" and not (
                    evidence.get("goal_completed") is True
                    or evidence.get("practice_mode") == "practice"):
                # 读到资料、看过视频都不是“做完了”；降级为一次有依据的进展。
                patch_op = "revise"
                fields.setdefault("progress", facts or "这次只更了解了一点")
            elif op == "abandon" and not reason:
                fields["progress"] = fields.get("progress") or facts
            if patch_op in {"complete", "pause", "abandon", "delete"}:
                fields = {key: value for key, value in fields.items()
                          if key in {"text", "type", "progress", "next_step"}}
        try:
            result = store.patch_goal(
                patch_op, target_id=target_id if patch_op != "add" else "",
                expected_version=expected, fields=fields, reason=reason,
                source_ids=source_ids, now=ts)
            if not result.get("ok"):
                log.debug(f"自主目标未更新：{result.get('error')}")
        except Exception as exc:
            log.debug(f"自主目标更新失败：{type(exc).__name__} {exc}")

    def _apply_self_patch(self, reaction: dict, ts: float,
                          allowed_sources: set[str]) -> None:
        """只有带真实经历依据的局部补丁才落到 life_store。"""
        if not self._evolution_enabled():
            return
        patch = reaction.get("self_patch")
        if not isinstance(patch, dict):
            return
        op = _one_line(patch.get("op") or patch.get("operation"), 20).lower()
        if op not in _SELF_PATCH_OPS:
            return
        raw_sources = patch.get("source_ids")
        if not isinstance(raw_sources, list):
            return
        sources = [str(item) for item in raw_sources if str(item) in allowed_sources]
        if not sources:
            return
        fields = patch.get("fields") if isinstance(patch.get("fields"), dict) else {}
        if not fields:
            fields = {key: value for key, value in patch.items()
                      if key in _SELF_TOP_FIELDS}
        if not fields and op not in {"retire", "delete"}:
            return
        store = self._store()
        if store is None:
            return
        target_id = _one_line(patch.get("target_id") or patch.get("id"), 120)
        if op != "add" and not target_id:
            return
        expected = patch.get("expected_version")
        if op != "add" and expected is None:
            # 签名要求已有条目必须带版本；缺了直接不写，绝不拿旧快照覆盖。
            log.debug("自主自我修订跳过：缺少 expected_version")
            return
        try:
            result = store.patch_self(
                op, target_id=target_id,
                expected_version=expected,
                expected_other_version=patch.get("expected_other_version"),
                fields=fields,
                reason=_one_line(patch.get("reason") or "基于实际经历的自主修订", 400),
                source_ids=sources, now=ts)
            if not result.get("ok"):
                log.debug(f"自主自我修订未应用：{result.get('error')}")
        except Exception as exc:
            log.debug(f"自主自我修订失败：{type(exc).__name__} {exc}")

    def _add_candidate(self, ts: float, evidence: dict, reaction: dict,
                       summary: str, experience_id: str, row: dict) -> None:
        try:
            hours = float(reaction.get("share_hours") or 24)
        except (TypeError, ValueError):
            hours = 24.0
        hours = max(1.0, min(72.0, hours))
        point = _one_line(reaction.get("share_point") or reaction.get("reaction"), 600)
        topics = evidence.get("topics") or []
        if not isinstance(topics, list):
            topics = [str(topics)]
        topics = [_one_line(item, 80) for item in topics if _one_line(item, 80)]
        if not topics:
            topics = [_one_line(row.get("topics") and row.get("topics")[0] or "", 80)]
        with self._state_lock:
            state = self._load(ts)
            items = [item for item in state.get("candidates", [])
                     if isinstance(item, dict) and item.get("experience_id") != experience_id]
            items.append({
                "experience_id": experience_id,
                "summary": _one_line(summary, 1000),
                "share_point": point,
                "topics": topics[:8],
                "platform": _one_line(evidence.get("platform"), 40),
                "created_at": ts,
                "expires_at": ts + hours * 3600.0,
                "shared": {},
            })
            state["candidates"] = items[-_MAX_CANDIDATES:]
            self._save()
