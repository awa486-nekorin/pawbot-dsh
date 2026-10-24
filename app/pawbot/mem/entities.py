"""L3 长期记忆：实体记忆库。

跟旧 `longmem` 的区别只有一个，但是根本性的：**这里存的是「增量」**。
旧实现把「累积后的滚动摘要」当事件追加，于是第 N 条包含前 N-1 条 —— 实测 36% 是重复。
这里同 key + 同 kind 只认最新一条，历史事件靠 `ts` 区分，不靠文本重复。

一条长这样：

    {"key":"uid:10001","kind":"profile","text":"对工具功能敏感，发现问题会直接确认",
     "ts":1792730602,"importance":3,"source":"group_123456789"}

`key` 是实体键，只有四种前缀：
    uid:<QQ号>      这个人
    group:<群号>    这个群
    promise:<主题>  一个约定（欠账、答应的事、到点要做的事）
    fact:<主题>     一条会变的事实（身高、住哪、在准备什么考试）

`kind` 决定怎么渲染、以及什么时候该被新条目顶掉：
    profile   性格/说话方式  —— 同 key 只留最新
    relation  好感数值        —— 同 key 只留最新
    promise   约定            —— 同 key 只留最新（做完了就删）
    fact      事实            —— 同 key 只留最新
    event     高重要度的事    —— key 唯一（ev:<日期>:<摘要哈希>），不去重
    group     群信息          —— 同 key 只留最新
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

from ..log import log
from ..memory import _tokens
from ..longmem import _WEAK_TOKENS

默认半衰天数 = 30.0
重要度权重 = 5.0

# 实体命中给多少分（人 > 群 > 话题）
实体权重 = {"uid": 3.0, "group": 2.0, "promise": 2.5, "fact": 2.0}


def _键前缀(key: str) -> str:
    return str(key or "").split(":", 1)[0]


def _短哈希(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:8]


def _grams(text: str, n: int = 6) -> set[str]:
    t = "".join(str(text or "").split())
    if len(t) < n:
        return set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def _像不像(a: str, b: str, 阈: float = 0.6) -> bool:
    """两条文本是不是在讲同一件事（按 6 字片段的重合比例）。

    存的时候按 key 去重（增量语义），取的时候还要按内容去重 ——
    因为旧数据里天然带着重复，而且「同一件事被记了两次」这种错以后也可能再犯。
    """
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return False
    小, 大 = (ga, gb) if len(ga) <= len(gb) else (gb, ga)
    return len(小 & 大) / len(小) >= 阈


def _天(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(ts or time.time())))


def _多久前(ts: float, now: float) -> str:
    d = max(0.0, (now - float(ts or now)) / 86400)
    if d < 1 / 24:
        return "刚才"
    if d < 1:
        return f"{int(d * 24)} 小时前"
    if d < 30:
        return f"{int(d)} 天前"
    return f"{int(d / 30)} 个月前"


def _私聊uid(source: str) -> str:
    """private_<uid> → <uid>；别的返回空串。"""
    s = str(source or "")
    return s[len("private_"):] if s.startswith("private_") else ""


def _来源标注(source: str, sources=None, 名字: str = "") -> str:
    """渲染时给一条记忆标来源：私聊 / 群里 / 私聊+群里。取不到就诚实留空，不编。"""
    s = str(source or "").strip()
    if s == "merge":
        srcs = [str(x).strip() for x in (sources or []) if str(x or "").strip()]
        if srcs:
            有私 = any(x.startswith("private_") for x in srcs)
            有群 = any(x.startswith("group_") for x in srcs)
            if 有私 and 有群:
                return "（私聊 + 群裡）"
            if 有私:
                return "（私聊）"
            if 有群:
                return "（群裡）"
        return ""      # 旧数据没有 sources，别假装知道它发生在哪
    if s.startswith("private_"):
        if 名字:
            return f"（私聊里对「{名字}」说的）"
        return "（私聊里说的）"
    if s.startswith("group_"):
        return "（群裡）"
    return ""


class EntityStore:
    """一个 jsonl 文件，读时去重（同 key+kind 取最新），写时只追加。"""

    def __init__(self, base_dir: str, config=None):
        self.base_dir = base_dir
        self.config = config
        d = "data/memory"
        if config is not None:
            d = config.get("persona.memory_dir", d) or d
        self.dir = d if os.path.isabs(d) else os.path.join(base_dir, d)
        self.path = os.path.join(self.dir, "entities.jsonl")
        self._cache: list[dict] | None = None
        self._cache_mtime = -1.0

    # ------------------------------------------------------------ 读
    def _raw(self) -> list[dict]:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self._cache, self._cache_mtime = [], -1.0
            return []
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        out: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("key") and row.get("kind"):
                        out.append(row)
        except OSError:
            out = []
        self._cache, self._cache_mtime = out, mtime
        return out

    def load(self, now: float | None = None) -> list[dict]:
        """有效条目：同 key + 同 kind 只留 ts 最新的一条。"""
        最新: dict[tuple[str, str], dict] = {}
        for row in self._raw():
            k = (str(row["key"]), str(row["kind"]))
            old = 最新.get(k)
            if old is None or float(row.get("ts") or 0) >= float(old.get("ts") or 0):
                最新[k] = row
        return sorted(最新.values(), key=lambda r: float(r.get("ts") or 0), reverse=True)

    # ------------------------------------------------------------ 写
    def add(self, key: str, kind: str, *, text: str | None = None,
            value: float | None = None, importance: int = 2,
            ts: float | None = None, source: str = "", force: bool = False,
            gist: str | None = None, points: list | None = None,
            sources: list | None = None) -> dict | None:
        """写一条。同 key+kind 的旧条目不用删 —— 读的时候自然被新的顶掉。

        force=False 时，如果同 key+kind 的最新一条内容一模一样，就不写（省得文件里堆重复）。
        """
        key = str(key or "").strip()
        kind = str(kind or "").strip()
        if not key or not kind:
            return None
        ts = float(ts or time.time())
        row = {
            "key": key,
            "kind": kind,
            "ts": ts,
            "importance": max(1, min(5, int(importance or 2))),
            "source": str(source or ""),
        }
        if text is not None:
            row["text"] = " ".join(str(text).split())[:400]
        if value is not None:
            row["value"] = float(value)
        # 渐忘用的两层：gist（一句话主干）+ points（2~3 个要点）。
        # 有了它，注入时才能按年龄降分辨率而不至于说不全；没有也能跑（退成截首句）。
        if gist is not None and str(gist).strip():
            row["gist"] = " ".join(str(gist).split())[:120]
        if points:
            净 = [" ".join(str(x).split())[:80] for x in points if str(x or "").strip()][:3]
            if 净:
                row["points"] = 净
        if sources:
            净源 = [" ".join(str(x).split())[:120] for x in sources if str(x or "").strip()]
            if 净源:
                row["sources"] = 净源

        if not force:
            for old in self.load():
                if old["key"] == key and old["kind"] == kind:
                    if old.get("text") == row.get("text") and old.get("value") == row.get("value"):
                        return None      # 一模一样，不写
                    break

        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._cache = None           # 下次读重新载
            self._cache_mtime = -1.0
        except OSError as e:
            log.warn(f"实体记忆写入失败：{type(e).__name__} {e}")
            return None
        return row

    def add_event(self, text: str, *, ts: float, importance: int = 3,
                  source: str = "", day: str | None = None,
                  gist: str | None = None, points: list | None = None) -> dict | None:
        """高重要度的事件：key 唯一（同一天同一段话不会重复写）。"""
        正文 = " ".join(str(text or "").split())
        if not 正文:
            return None
        key = f"ev:{day or _天(ts)}:{_短哈希(正文)}"
        return self.add(key, "event", text=正文, importance=importance, ts=ts,
                        source=source, gist=gist, points=points)

    def drop(self, key: str, kind: str | None = None) -> int:
        """删掉某个实体（连同它的所有 kind）。重写文件 —— 条目少，代价可接受。"""
        key = str(key or "").strip()
        rows = [r for r in self._raw() if r.get("key") != key or (kind and r.get("kind") != kind)]
        去掉 = len(self._raw()) - len(rows)
        if not 去掉:
            return 0
        self._write_all(rows)
        return 去掉

    def compact(self, keep_events: int = 400) -> int:
        """压实：同 key+kind 只留最新。返回压掉了多少行。

        ⚠ 2026-10-24 改：原来 event 超过 keep_events 是**真删**，跟「不能全忘」直接冲突
        （用户原话：「时间越久就越忘记但是又不能全忘记」）。现在改成**降分辨率**：

          · 有 `gist` 的：清掉 `text`，只留骨架 —— 省体积，但这条记忆还在，只是变粗了
          · 没 `gist` 的：**原样保留** —— 宁可占地方，也不许因为「还没提炼」就把它删掉

        也就是说，压实的对象是**字数**，不是**记忆本身**。想彻底想起来更多，`text` 还在的
        那批随时能读全文；清掉 text 的那批至少还有一句话。
        """
        有效 = self.load()
        events = [r for r in 有效 if r["kind"] == "event"]
        events.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)

        保留 = [r for r in 有效 if r["kind"] != "event"] + events[:max(0, keep_events)]
        老事件 = events[max(0, keep_events):]

        清了 = 0
        for r in 老事件:
            骨 = str(r.get("gist") or "").strip()
            if 骨 and str(r.get("text") or "").strip():
                # 只留骨架：text 清空，gist 保留，标一个记号方便以后统计
                r = dict(r)
                r.pop("text", None)
                r["压实"] = True
                保留.append(r)
                清了 += 1
            else:
                保留.append(r)      # 没骨架 —— 一条都不许丢

        保留.sort(key=lambda r: float(r.get("ts") or 0))
        原 = len(self._raw())
        self._write_all(保留)
        # 返回值口径改成「压掉多少字」更有意义，但调用方看的是行数，保持兼容
        return max(0, 原 - len(保留)) + 清了

    def _write_all(self, rows: list[dict]):
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
            self._cache = None
            self._cache_mtime = -1.0
        except OSError as e:
            log.warn(f"实体记忆压实失败：{type(e).__name__} {e}")

    # ------------------------------------------------------------ 召回
    @staticmethod
    def _score(row: dict, query_tokens: set[str], uids: set[str], group_id: str,
               now: float, half_life_days: float) -> tuple[float, str]:
        """返回 (分数, 命中的理由)。0 分 = 不要。"""
        前缀 = _键前缀(row["key"])
        分 = 0.0
        理由 = ""

        # ① 实体命中：这条讲的就是「现在在场的这个人 / 这个群」
        if 前缀 == "uid" and row["key"][4:] in uids:
            分 += 实体权重["uid"]
            理由 = "本人"
        elif 前缀 == "group" and group_id and row["key"][6:] == str(group_id):
            分 += 实体权重["group"]
            理由 = "本群"
        elif 前缀 in ("promise", "fact"):
            # 约定和事实没有实体归属，靠话题命中；但它们是「该被想起来」的那类，给一点底分
            分 += 0.0

        # ② 话题命中：只在实词上算，虚词二元组不算
        words = _tokens(str(row.get("text") or ""))
        overlap = len((query_tokens & words) - _WEAK_TOKENS)
        if overlap:
            分 += min(5.0, float(overlap))
            理由 = (理由 + "+" if 理由 else "") + f"话题{overlap}"

        if 分 <= 0:
            return 0.0, ""

        # ③ 时间衰减：半衰 30 天。约定不该衰减太快，所以 promise 用双倍半衰
        age_days = max(0.0, now - float(row.get("ts") or now)) / 86400
        半衰 = half_life_days * (2.0 if 前缀 == "promise" else 1.0)
        衰减 = 0.5 ** (age_days / max(0.01, 半衰))

        重要度 = max(1, min(5, int(row.get("importance") or 1)))
        return 分 * 衰减 * (1 + 重要度 / 重要度权重), 理由

    def recall(self, *, query: str, uids=(), group_id: str = "",
               budget: int = 1500, now: float | None = None,
               half_life_days: float | None = None,
               名字查找=None) -> tuple[list[dict], dict]:
        """按「实体优先 → 话题 → 衰减」挑条目，按预算整条取（不做字符级截断）。

        返回 (选中的条目, 统计)。`名字查找` 可选：uid → 昵称，给私聊来源标上对方名字。
        """
        now = float(now or time.time())
        半衰 = float(half_life_days if half_life_days is not None else 默认半衰天数)
        query_tokens = _tokens(str(query or ""))
        uid集 = {str(u) for u in uids if str(u or "").strip()}
        if 名字查找 is None:
            名字查找 = self._对方名字

        打分 = []
        for row in self.load():
            分, 理由 = self._score(row, query_tokens, uid集, group_id, now, 半衰)
            if 分 > 0:
                打分.append((分, 理由, row))
        打分.sort(key=lambda x: x[0], reverse=True)

        选中: list[dict] = []
        用了 = 0
        跳过 = 0
        重复 = 0
        for 分, 理由, row in 打分:
            文本 = self._render_one(row, now, 名字查找=名字查找)
            if not 文本:
                continue
            # ② 内容去重：已经选中的条目里如果有讲同一件事的，这条就不要了。
            #    否则 6 条召回里能出现 3 条一样的（旧数据实测就是这样）。
            if any(_像不像(文本, x["_文本"]) for x in 选中):
                重复 += 1
                continue
            if 用了 + len(文本) > budget:
                跳过 += 1
                continue
            选中.append({**row, "_分": round(分, 2), "_理由": 理由, "_文本": 文本})
            用了 += len(文本)
            if 用了 >= budget * 0.98:
                break

        return 选中, {
            "候选": len(打分),
            "选中": len(选中),
            "因预算跳过": 跳过,
            "因重复跳过": 重复,
            "字数": 用了,
            "预算": budget,
        }

    def _对方名字(self, uid: str) -> str:
        """从 data/people/<uid>.md 里捞私聊对方的名字；取不到返回空串。带一层缓存。"""
        uid = str(uid or "").strip()
        if not uid:
            return ""
        缓存 = getattr(self, "_名字缓存", None)
        if 缓存 is None:
            缓存 = {}
            self._名字缓存 = 缓存
        if uid in 缓存:
            return 缓存[uid]
        名 = ""
        try:
            d = "data/people"
            if self.config is not None:
                d = self.config.get("people.dir", d) or d
            d = d if os.path.isabs(d) else os.path.join(self.base_dir, d)
            with open(os.path.join(d, uid + ".md"), "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"名字[：:]\s*(.+)", text)
            if m:
                名 = m.group(1).strip()
            else:
                m = re.search(r"^#\s+(.+)$", text, re.M)
                if m:
                    名 = re.sub(r"[（(][^）)]*[）)]\s*$", "", m.group(1).strip()).strip()
        except OSError:
            名 = ""
        缓存[uid] = 名
        return 名

    @staticmethod
    def _render_one(row: dict, now: float, *, 名字查找=None) -> str:
        """一条渲染成一行。**整条进，不截半句** —— 旧实现的「尾部字符截断」就是碎片的来源。

        2026-10-24：加了一层「渐忘」——按年龄降分辨率（全文 → 骨架 → 只剩半句），
        时间说法也跟着糊（`3 天前` → `那阵子` → 不报时间）。详见 mem/decay.py。
        底线是**任何年龄都至少留一句话**：宁可说不全，不许全忘。

        2026-10-24（来源标注）：行尾按 `source` 标「私聊 / 群里 / 私聊+群里」，
        私聊来源若给得出对方名字就带上。这是给 L3 注入时防止「把私聊说成群聊」的关键。
        """
        from . import decay

        前缀 = _键前缀(row["key"])
        谁 = {
            "uid": "人",
            "group": "群",
            "promise": "约定",
            "fact": "事实",
            "event": "事",
        }.get(前缀, 前缀)
        尾号 = ""
        if 前缀 == "uid":
            尾号 = "·" + row["key"][4:][-4:]
        if row["kind"] == "relation" and row.get("value") is not None:
            return ""      # 好感不单独成行，由 tone 那一路给语气
        龄 = max(0.0, (now - float(row.get("ts") or now)) / 86400)
        正文 = decay.渲染(row, 龄).strip()
        if not 正文:
            return ""
        何时 = decay.时间措辞(龄)
        出 = f"- [{谁}{尾号}] {正文}"
        if 何时:
            出 += f"（{何时}）"
        名字 = ""
        uid = _私聊uid(str(row.get("source") or ""))
        if uid and 名字查找:
            try:
                名字 = str(名字查找(uid) or "").strip()
            except Exception:
                名字 = ""
        出 += _来源标注(str(row.get("source") or ""), row.get("sources"), 名字)
        return 出

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict:
        有效 = self.load()
        按kind: dict[str, int] = {}
        for row in 有效:
            按kind[row["kind"]] = 按kind.get(row["kind"], 0) + 1
        字数 = sum(len(str(r.get("text") or "")) for r in 有效)
        return {"条目": len(有效), "按kind": 按kind, "总字数": 字数,
                "原始行数": len(self._raw()), "文件": self.path}
