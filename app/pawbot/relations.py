"""关系网：谁跟谁互动多、每个群的群主和管理。

    data/relations.json
    {
      "pairs": {
        "10001|10002": {"a": "10001", "b": "10002", "score": 3.0,
                        "last_ts": 1760000000.0,
                        "kinds": {"reply": 2, "mention": 1, "poke": 0}}
      },
      "groups": {"123456": {"owner": 10001, "admins": [10002], "updated": 1760000000}},
      "group_pairs": {"123456": {"10001|10002": 3.0}},
      "names": {"10001": "阿明"}
    }

``pairs`` / ``groups`` 是按需求定的主结构：

  * ``pairs`` 的 key 是**字符串排序后**的两个 QQ 号，保证 A→B 和 B→A
    落到同一条；``a`` / ``b`` 也按同样顺序存。
  * ``groups`` 只缓存群主/管理，刷新频率由 groupinfo 那边控制。

``group_pairs`` 和 ``names`` 是附加索引，不改变上面两项的语义：

  * ``group_pairs[gid][pair_key]`` 只累加**这个群里**发生的互动，
    这样 ``summarize_group(gid)`` 不会把别的群的私事带出来。
  * ``names`` 存最近见到的群名片，只给提示词称呼用，不存任何内容。

写盘策略与 store 一样：只在内存里改 + 打脏标记，``flush()`` 才真正落盘。
``for_bot()`` 会挂一个守护线程按 ``relations.flush_interval`` 定期 flush，
插件热重载也复用同一个实例，不会越开越多。
"""

from __future__ import annotations

import atexit
import json
import os
import re
import threading
import time

from .log import log
from .people import _atomic_write, _lock_for

_KIND_SCORE = {"reply": 1.0, "mention": 0.8, "poke": 1.2}
_KINDS = ("reply", "mention", "poke")


def _norm_uid(value) -> str:
    """QQ 号统一成纯数字字符串；空 / 非数字返回空串。"""
    return re.sub(r"\D", "", str(value or ""))


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pair_key(a: str, b: str) -> str:
    """字符串排序，保证同一条关系只有一个 key。"""
    return f"{a}|{b}" if a <= b else f"{b}|{a}"


class Relations:
    def __init__(self, base_dir: str, config=None):
        self.base_dir = base_dir or "."
        self.config = config
        self.path = os.path.join(self.base_dir, "data", "relations.json")
        self._lock = threading.RLock()
        self.dirty = False
        self.data: dict = {"pairs": {}, "groups": {}, "group_pairs": {}, "names": {}}
        self._thread: threading.Thread | None = None
        self.load()

    # ------------------------------------------------------------ 配置
    def _cfg(self, key: str, default):
        cfg = self.config
        if cfg is None:
            return default
        try:
            value = cfg.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def enabled(self) -> bool:
        return bool(self._cfg("relations.enabled", True))

    def min_score(self) -> float:
        return _as_float(self._cfg("relations.min_score", 3)) or 3.0

    # ------------------------------------------------------------ 读写
    def load(self):
        raw = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                log.warn(f"关系网载入失败：{type(e).__name__} {e}")
                raw = {}
        with self._lock:
            self.data = self._normalize(raw)
            self.dirty = False

    @staticmethod
    def _normalize(raw) -> dict:
        out = {"pairs": {}, "groups": {}, "group_pairs": {}, "names": {}}
        if not isinstance(raw, dict):
            return out

        pairs = raw.get("pairs")
        if isinstance(pairs, dict):
            for key, row in pairs.items():
                if not isinstance(row, dict):
                    continue
                a = _norm_uid(row.get("a"))
                b = _norm_uid(row.get("b"))
                if (not a or not b) and "|" in str(key):
                    left, _, right = str(key).partition("|")
                    a, b = _norm_uid(left), _norm_uid(right)
                if not a or not b or a == b:
                    continue
                kinds = row.get("kinds") if isinstance(row.get("kinds"), dict) else {}
                clean_kinds = {k: max(0, _as_int(kinds.get(k))) for k in _KINDS}
                pair_key = _pair_key(a, b)
                out["pairs"][pair_key] = {
                    "a": min(a, b),
                    "b": max(a, b),
                    "score": max(0.0, _as_float(row.get("score"))),
                    "last_ts": _as_float(row.get("last_ts")),
                    "kinds": clean_kinds,
                }

        groups = raw.get("groups")
        if isinstance(groups, dict):
            for gid, row in groups.items():
                gid_s = _norm_uid(gid)
                if not gid_s or not isinstance(row, dict):
                    continue
                admins = []
                for item in row.get("admins") or []:
                    uid = _norm_uid(item.get("uid") if isinstance(item, dict) else item)
                    if uid and int(uid) not in admins:
                        admins.append(int(uid))
                out["groups"][gid_s] = {
                    "owner": _as_int(row.get("owner")),
                    "admins": admins,
                    "updated": _as_int(row.get("updated")),
                }

        group_pairs = raw.get("group_pairs")
        if isinstance(group_pairs, dict):
            for gid, rows in group_pairs.items():
                gid_s = _norm_uid(gid)
                if not gid_s or not isinstance(rows, dict):
                    continue
                clean = {}
                for key, score in rows.items():
                    left, _, right = str(key).partition("|")
                    a, b = _norm_uid(left), _norm_uid(right)
                    if not a or not b or a == b:
                        continue
                    clean[_pair_key(a, b)] = max(0.0, _as_float(score))
                out["group_pairs"][gid_s] = clean

        names = raw.get("names")
        if isinstance(names, dict):
            for uid, name in names.items():
                uid_s = _norm_uid(uid)
                text = " ".join(str(name or "").split())[:40]
                if uid_s and text and text != uid_s:
                    out["names"][uid_s] = text
        return out

    def flush(self, force: bool = False) -> bool:
        """把有变更的数据原子写盘；没有变更就直接返回 False。"""
        with _lock_for(self.path):
            with self._lock:
                if not force and not self.dirty:
                    return False
                payload = json.dumps(self.data, ensure_ascii=False, indent=2)
                _atomic_write(self.path, payload + "\n")
                self.dirty = False
                return True

    def save(self):
        """flush 的兼容别名。"""
        return self.flush(force=True)

    # ------------------------------------------------------------ 记录
    def remember_name(self, uid, name) -> bool:
        """只记群名片，不记内容；同名不同人由 uid 区分。"""
        uid_s = _norm_uid(uid)
        text = " ".join(str(name or "").split())[:40]
        if not uid_s or not text or text == uid_s:
            return False
        with self._lock:
            if self.data["names"].get(uid_s) == text:
                return False
            self.data["names"][uid_s] = text
            self.dirty = True
            return True

    def note_interaction(self, a, b, kind: str, group_id=None) -> bool:
        """记一次互动。kind 只能是 reply / mention / poke。

        group_id 可选：给了就同时累加这个群的分，给 summarize_group 用。
        """
        if not self.enabled() or kind not in _KIND_SCORE:
            return False
        ua, ub = _norm_uid(a), _norm_uid(b)
        if not ua or not ub or ua == ub:
            return False
        delta = _KIND_SCORE[kind]
        key = _pair_key(ua, ub)
        gid = _norm_uid(group_id)
        now = time.time()
        with self._lock:
            row = self.data["pairs"].get(key)
            if row is None:
                row = {"a": key.partition("|")[0], "b": key.partition("|")[2],
                       "score": 0.0, "last_ts": 0.0,
                       "kinds": {k: 0 for k in _KINDS}}
                self.data["pairs"][key] = row
            row["score"] = round(_as_float(row.get("score")) + delta, 3)
            row["last_ts"] = now
            kinds = row.setdefault("kinds", {})
            for name in _KINDS:
                kinds.setdefault(name, 0)
            kinds[kind] = _as_int(kinds.get(kind)) + 1
            if gid:
                scores = self.data["group_pairs"].setdefault(gid, {})
                scores[key] = round(_as_float(scores.get(key)) + delta, 3)
            self.dirty = True
            return True

    # ------------------------------------------------------------ 查询
    def top_partners(self, uid, limit: int = 3, group_id=None) -> list[tuple[str, float]]:
        """按互动分排序的 [(对方 uid, score)]。"""
        uid_s = _norm_uid(uid)
        if not uid_s:
            return []
        gid = _norm_uid(group_id)
        rows: list[tuple[str, float]] = []
        with self._lock:
            if gid:
                scores = dict(self.data["group_pairs"].get(gid) or {})
                for key, score in scores.items():
                    a, _, b = key.partition("|")
                    if a == uid_s:
                        rows.append((b, _as_float(score)))
                    elif b == uid_s:
                        rows.append((a, _as_float(score)))
            else:
                for key, row in self.data["pairs"].items():
                    a, _, b = key.partition("|")
                    if a == uid_s:
                        rows.append((b, _as_float(row.get("score"))))
                    elif b == uid_s:
                        rows.append((a, _as_float(row.get("score"))))
        rows.sort(key=lambda item: (-item[1], item[0]))
        return rows[:max(0, int(limit or 0))]

    def pair_hint(self, uid, name_of, group_id=None) -> str:
        """给提示词的一句话：这个人跟谁互动最多。

        只说「谁和谁互动多」，不猜关系性质。top1 不到 min_score 就整句不返回，
        免得刚聊一句的人被说成「最常」。
        """
        if not self.enabled():
            return ""
        partners = self.top_partners(uid, 2, group_id=group_id)
        if not partners or partners[0][1] < self.min_score():
            return ""

        def _name(other: str) -> str:
            text = ""
            if callable(name_of):
                try:
                    text = str(name_of(other) or "").strip()
                except Exception:
                    text = ""
            if not text:
                with self._lock:
                    text = str(self.data["names"].get(other) or "")
            return " ".join(text.split())[:40] if text else other

        first = _name(partners[0][0])
        text = f"他跟 {first} 互动最多"
        if len(partners) > 1 and partners[1][1] >= self.min_score():
            text += f"，跟 {_name(partners[1][0])} 互动次之"
        return text

    def summarize_group(self, gid, limit: int = 5) -> str:
        """这个群里互动最密的几对（含名字），给群感知提示用。"""
        if not self.enabled():
            return ""
        gid_s = _norm_uid(gid)
        if not gid_s:
            return ""
        with self._lock:
            scores = dict(self.data["group_pairs"].get(gid_s) or {})
            names = dict(self.data["names"])
        threshold = self.min_score()
        rows = sorted(((key, score) for key, score in scores.items() if score >= threshold),
                      key=lambda item: (-item[1], item[0]))
        if not rows:
            return ""
        pairs = []
        for key, _score in rows[:max(1, int(limit or 5))]:
            a, _, b = key.partition("|")
            pairs.append((names.get(a) or a, names.get(b) or b))
        head = f"群里 {pairs[0][0]} 和 {pairs[0][1]} 互动最多"
        rest = [f"{a} 和 {b}" for a, b in pairs[1:]]
        if rest:
            head += "，" + "、".join(rest) + " 也常互动"
        return head + "。"

    # ------------------------------------------------------------ 群主 / 管理
    def set_group_meta(self, gid, owner, admins) -> bool:
        """由插件从 groupinfo 缓存同步；内容没变就不打脏标记。"""
        gid_s = _norm_uid(gid)
        if not gid_s:
            return False
        owner_i = _as_int(owner)
        clean = []
        for item in admins or []:
            uid = _as_int(_norm_uid(item.get("uid") if isinstance(item, dict) else item))
            if uid and uid != owner_i and uid not in clean:
                clean.append(uid)
        if not owner_i and not clean:
            return False
        with self._lock:
            old = self.data["groups"].get(gid_s) or {}
            old_admins = [_as_int(x) for x in old.get("admins") or []]
            if _as_int(old.get("owner")) == owner_i and old_admins == clean:
                return False
            self.data["groups"][gid_s] = {
                "owner": owner_i,
                "admins": clean,
                "updated": int(time.time()),
            }
            self.dirty = True
            return True

    def group_meta(self, gid) -> dict | None:
        gid_s = _norm_uid(gid)
        if not gid_s:
            return None
        with self._lock:
            row = self.data["groups"].get(gid_s)
            if not isinstance(row, dict):
                return None
            return {
                "owner": _as_int(row.get("owner")),
                "admins": [_as_int(x) for x in row.get("admins") or []],
                "updated": _as_int(row.get("updated")),
            }

    # ------------------------------------------------------------ 后台落盘
    def start_autosave(self):
        if self._thread is not None and self._thread.is_alive():
            return
        # 正常退出时补一次落盘；硬杀进程最多丢一个 flush_interval
        try:
            atexit.register(self.flush)
        except Exception:
            pass
        thread = threading.Thread(target=self._autosave_loop,
                                  name="relations-flush", daemon=True)
        self._thread = thread
        thread.start()

    def _autosave_loop(self):
        while True:
            try:
                interval = _as_float(self._cfg("relations.flush_interval", 60)) or 60.0
            except Exception:
                interval = 60.0
            time.sleep(max(1.0, interval))
            try:
                self.flush()
            except Exception as e:
                log.debug(f"关系网落盘失败：{type(e).__name__} {e}")


def for_bot(bot) -> Relations:
    """懒建立挂在 Bot 上的实例（不改 core.py）。插件热重载复用同一个。"""
    rel = getattr(bot, "relations", None)
    if isinstance(rel, Relations):
        return rel
    rel = Relations(getattr(bot, "base_dir", "") or ".",
                    getattr(bot, "config", None))
    try:
        setattr(bot, "relations", rel)
    except Exception:
        pass
    rel.start_autosave()
    return rel
