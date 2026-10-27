"""
读空气：记住群里正在聊什么，判断该不该插嘴。

不是真的要「理解」话题，而是维护一小块共享状态：

  · 最近 N 条消息（谁说的、说了啥）—— 用来判断语境
  · 当前话题关键词 —— 从最近消息里抽出来的实词
  · 冷场时长 —— 多久没人说话了
  · 插话冷却 —— 它自己上次说话是什么时候

真正的价值在 `should_chime_in()`：把「随机数 < 阈值」换成
「有话题接得上 / 刚有人提到我 / 冷场很久了」这三种有理由的场合。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

from .log import log

# 话题/心情都是同一进程里的小 JSON，共用一把可重入锁就够；
# 落盘走 tmp + os.replace，读那边永远不会看到写了一半的文件。
_IO_LOCK = threading.RLock()

# 中文虚词/口头语，抽话题时过滤掉
_STOP = set("""
的 了 是 在 我 你 他 她 它 们 这 那 有 没 就 也 都 还 又 和 与 吧 啊 吗 呢
什么 怎么 为什么 一个 一下 现在 今天 可以 不是 就是 还是 但是 所以 因为
哈哈 哈哈哈 呵呵 嗯 哦 噢 唉 诶 草 笑死 真的 好像 感觉 觉得 应该
""".split())

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def _tokens(text: str) -> set[str]:
    text = _PUNCT.sub(" ", text.lower())
    out: set[str] = set()
    for w in text.split():
        if len(w) >= 2 and w not in _STOP:
            out.add(w)
        # 中文再加二字组合，粗粒度够用了
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", w):
            out.update(w[i:i + 2] for i in range(len(w) - 1))
    return {x for x in out if x not in _STOP and len(x) >= 2}


class TopicTracker:
    """每个群一份。"""

    def __init__(self, size: int = 30, path: str | None = None):
        self.size = size
        self.groups: dict[str, dict] = {}
        self.path = path
        self._dirty = 0
        if path:
            self.load()

    # ------------------------------------------------------------ 落盘
    def save(self, force: bool = False):
        """把节奏数据写盘。

        必须持久化：活跃度是「观察很久才知道」的东西，
        重启一次就归零的话，自适应永远在猜。
        """
        if not self.path:
            return
        if not force and self._dirty < 5:
            return
        import json
        import os
        with _IO_LOCK:
            snapshot = {}
            for sid, n in self.groups.items():
                snapshot[sid] = {
                    "last_human_at": n.get("last_human_at", 0),
                    "last_self_at": n.get("last_self_at", 0),
                    "last_chime_at": n.get("last_chime_at", 0),
                    "gaps": list(n.get("gaps") or []),
                    "recent": list(n.get("recent") or [])[-self.size:],
                }
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, ensure_ascii=False)
                os.replace(tmp, self.path)
                self._dirty = 0
            except Exception as e:
                # 节奏数据丢一次不致命，但别再静默：写失败至少留个线索
                log.debug(f"话题存档失败：{type(e).__name__} {e}")

    def load(self):
        import json
        import os
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for sid, v in (data or {}).items():
            if not isinstance(v, dict):
                continue
            n = self._node(sid)
            n["last_human_at"] = float(v.get("last_human_at") or 0)
            n["last_self_at"] = float(v.get("last_self_at") or 0)
            n["last_chime_at"] = float(v.get("last_chime_at") or 0)
            n["gaps"].extend(float(g) for g in (v.get("gaps") or []) if g)
            for row in (v.get("recent") or [])[-self.size:]:
                if isinstance(row, dict) and row.get("text"):
                    n["recent"].append(row)

    def _node(self, session_id: str) -> dict:
        return self.groups.setdefault(session_id, {
            "recent": deque(maxlen=self.size),
            "last_human_at": 0.0,       # 最后一次「别人」说话
            "last_self_at": 0.0,        # 最后一次「它」说话
            "last_chime_at": 0.0,       # 最后一次主动插话
            "self_said": deque(maxlen=20),
            "gaps": deque(maxlen=30),   # 相邻两条人类消息之间的间隔（秒）
        })

    def clear(self, session_id: str):
        """清除单个会话的话题与节奏状态并立即落盘。"""
        self.groups.pop(session_id, None)
        self._dirty += 1
        self.save(force=True)

    # ------------------------------------------------------------ 写入
    def seen(self, session_id: str, sender: str, text: str, *, is_self=False):
        n = self._node(session_id)
        now = time.time()
        text = (text or "").strip()
        if is_self:
            n["last_self_at"] = now
            if text:
                n["self_said"].append(text)
            return
        prev = n["last_human_at"]
        n["last_human_at"] = now
        # 记下说话间隔：这是这个群自己的呼吸节奏，比「消息总数」有用得多
        if prev and now > prev:
            gap = min(now - prev, 6 * 3600)     # 超过 6 小时的一律当成「隔了很久」
            n["gaps"].append(gap)
        if text:
            n["recent"].append({"who": sender, "text": text[:200], "ts": now})
        self._dirty += 1
        self.save()

    def mark_chime(self, session_id: str):
        self._node(session_id)["last_chime_at"] = time.time()

    # ------------------------------------------------------------ 活跃度
    def activity(self, session_id: str) -> dict:
        """这个群平时说话有多密。

        用的不是「累计消息数」（那只会随时间单调变大），
        而是**相邻消息的间隔中位数**——它代表这个群自己的呼吸节奏：

            死群     一条消息隔几小时  → 中位间隔很大
            热闹群   几十秒就一句       → 中位间隔很小

        取中位数而不是平均，是为了不被「隔夜的 6 小时」这种极端值带偏。
        """
        n = self._node(session_id)
        gaps = list(n["gaps"])
        if len(gaps) < 3:
            # 样本太少，别乱猜，给个中间值
            return {"level": 0.5, "median_gap": 0.0, "per_hour": 0.0,
                    "samples": len(gaps), "known": False}

        gaps.sort()
        median = gaps[len(gaps) // 2]
        median = max(1.0, median)
        per_hour = 3600.0 / median
        # 60 条/小时（平均一分钟一条）就算「很热闹」了
        level = max(0.0, min(1.0, per_hour / 60.0))
        return {"level": level, "median_gap": median, "per_hour": per_hour,
                "samples": len(gaps), "known": True}

    def adaptive_span(self, session_id: str, base_min: float, base_max: float,
                      low: float = 0.75, high: float = 1.5) -> tuple[float, float, dict]:
        """按群活跃度缩放冷场阈值。

        热闹群 ×1.5（有人会说话，你别急着填坑）
        死群   ×0.75（就你一个活人，可以稍微主动点）

        返回 (min, max, 活跃度信息)，信息用来打日志。
        """
        info = self.activity(session_id)
        lv = float(info["level"])
        factor = low + (high - low) * lv
        return (base_min * factor, base_max * factor, info)

    # ------------------------------------------------------------ 读取
    def idle_seconds(self, session_id: str) -> float:
        n = self._node(session_id)
        last = max(n["last_human_at"], n["last_self_at"])
        if not last:
            return 0.0
        return time.time() - last

    def keywords(self, session_id: str, limit: int = 6) -> list[str]:
        n = self._node(session_id)
        rows = list(n["recent"])
        if not rows:
            return []

        # df = 出现在几条消息里；idf 低的东西（人人都说的）不算话题
        df: dict[str, int] = {}
        weight: dict[str, float] = {}
        for i, row in enumerate(rows):
            w = 1.0 + i / max(1, len(rows))
            toks = _tokens(row["text"])
            for t in toks:
                df[t] = df.get(t, 0) + 1
                weight[t] = weight.get(t, 0.0) + w

        # 只要出现过两次以上的，或者消息本身很少时的全部
        multi = {t: s for t, s in weight.items() if df[t] >= 2}
        if len(multi) >= 3:
            pool = multi
        else:
            pool = weight

        # 太长的（整句）不是关键词；单条消息里才出现一次的，排序靠后
        def score(t: str) -> float:
            s = pool[t]
            if len(t) > 6:
                s *= 0.3
            if df[t] < 2:
                s *= 0.5
            return s

        ranked = sorted(pool, key=lambda t: -score(t))
        # 去掉被别的关键词包含的碎片（"第三题" 里有 "第三" 就留长的）
        out: list[str] = []
        for t in ranked:
            if any(t != o and t in o for o in out):
                continue
            out.append(t)
            if len(out) >= limit:
                break
        return out

    def solid_keywords(self, session_id: str, limit: int = 4) -> list[str]:
        """只返回「真的被多条消息重复提到」的词。

        短句里切出来的二字碎片（「说可」「能要」）很容易混进关键词，
        拿这种词去搜 B 站只会搜到一堆垃圾。所以搜索这种场合只用高频词，
        宁可留空（留空就去刷热门）。
        """
        n = self._node(session_id)
        rows = list(n["recent"])
        if not rows:
            return []
        df: dict[str, int] = {}
        for row in rows:
            for t in _tokens(row["text"]):
                df[t] = df.get(t, 0) + 1
        hits = [(t, c) for t, c in df.items() if c >= 2 and 2 <= len(t) <= 6]
        hits.sort(key=lambda kv: -kv[1])
        return [t for t, _ in hits[:limit]]

    def digest(self, session_id: str, limit: int = 6) -> str:
        """最近几条消息的紧凑摘要，给模型当「刚才聊到哪了」。"""
        n = self._node(session_id)
        rows = list(n["recent"])[-limit:]
        if not rows:
            return ""
        return "\n".join(f"{r['who']}：{r['text'][:80]}" for r in rows)

    def dominant_speaker(self, session_id: str) -> str:
        n = self._node(session_id)
        rows = list(n["recent"])[-10:]
        if not rows:
            return ""
        names = [r["who"] for r in rows]
        return max(set(names), key=names.count)

    # ------------------------------------------------------------ 决策
    def should_chime_in(self, session_id: str, config) -> tuple[bool, str]:
        """主动插话该不该？返回 (要不要, 理由)。

        三道闸缺一不可，少一道它就会变成一分钟一句的背景噪音：
          · 刚有人说话 → 别插
          · 刚插过 → 冷却期内闭嘴
          · 群正热闹 → 让它自己聊，别硬挤进去
        """
        n = self._node(session_id)
        now = time.time()
        idle = self.idle_seconds(session_id)
        min_idle = float(config.get("topic.min_idle", 120) or 0)
        if idle < min_idle:
            return (False, f"刚有人说过话（{int(idle)} 秒前）")
        cooldown = float(config.get("topic.chime_cooldown", 300) or 0)
        last = float(n.get("last_chime_at") or 0)
        if cooldown > 0 and last and now - last < cooldown:
            return (False, f"刚插过话（{int(now - last)} 秒前）")
        busy = float(config.get("topic.busy_threshold", 12) or 0)
        per_min = float(self.activity(session_id).get("per_hour") or 0) / 60.0
        if busy > 0 and per_min > busy:
            return (False, f"群太热闹（{per_min:.0f} 条/分）")
        kws = self.keywords(session_id)
        if kws:
            return (True, f"有话题：" + "/".join(kws[:3]))
        return (True, "冷场")


def mood_label(score: float) -> str:
    if score >= 70:
        return "心情不错"
    if score >= 40:
        return "平平"
    if score >= 20:
        return "有点困"
    return "懒得说话"


# 绑定八维情绪时用的分组，名字必须和 emotion.DIMS 一致。
# 「好奇」是中性的，两边都不算，免得问一句就当成高兴。
_POSITIVE_DIMS = ("开心", "兴奋", "感动", "得意")
_NEGATIVE_DIMS = ("烦躁", "低落")


class Mood:
    """心情值（0~100），给状态页和「今天话多话少」用的粗粒度视角。

    历史上有两套心情：这里的 `Mood` 自己漂移，`emotion.py` 的八维矩阵也在算情绪。
    聊天链路现在只认八维矩阵，所以 `Mood` 可以当它的**只读视图**：

        score = 正向维度均值 − 负向维度均值，线性映射到 0~100
                （基准 50/50 时正好 50）
        label = 原来的 mood_label(score)

    调用 `bind_emotion(emotion)` 之后，on_mentioned()/on_chatted()/save() 都是空操作，
    `Mood` 不再自己记账、也不再落盘。没绑定时行为和以前完全一样。
    """

    def __init__(self, path: str | None = None):
        self._score = 55.0
        self.updated = time.time()
        self._drift()
        self.path = path
        self._emotion = None
        if path:
            self._load()

    # ------------------------------------------------------------ 情绪视图
    def bind_emotion(self, emotion):
        """接上八维情绪矩阵；之后 score/label 都由它推导，心情不再单独存盘。"""
        self._emotion = emotion
        return self

    @property
    def bound(self) -> bool:
        return self._emotion is not None

    @property
    def score(self) -> float:
        emo = self._emotion
        if emo is None:
            return self._score
        try:
            vals = emo.values()
            pos = sum(float(vals.get(d, 50.0)) for d in _POSITIVE_DIMS) / len(_POSITIVE_DIMS)
            neg = sum(float(vals.get(d, 50.0)) for d in _NEGATIVE_DIMS) / len(_NEGATIVE_DIMS)
        except Exception:
            return self._score
        # 差值是 -100~100，线性压到 0~100；八维全在基准时就是 50
        return max(0.0, min(100.0, 50.0 + (pos - neg) / 2.0))

    @score.setter
    def score(self, value):
        # 绑定后 getter 仍从 emotion 取；这里保留写入口，未绑定的老代码照旧能用
        self._score = float(value)

    def _load(self):
        import json
        import os
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError("心情存档不是对象")
            self._score = max(0.0, min(100.0, float(d.get("score", 55))))
            self.updated = float(d.get("updated", time.time()))
        except Exception as e:
            # 坏档就当没存过：回到基线，别带着垃圾值跑
            self._score = 50.0
            self.updated = time.time()
            log.debug(f"心情存档读取失败，按默认处理：{type(e).__name__} {e}")

    def save(self):
        if self._emotion is not None:
            return          # 已经绑定八维情绪，心情不再单独落盘
        import json
        import os
        if not self.path:
            return
        with _IO_LOCK:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"score": round(self._score, 1),
                               "updated": self.updated}, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception as e:
                log.debug(f"心情存档失败：{type(e).__name__} {e}")

    def _drift(self):
        """时间越久越回落到 50 附近，避免一路飘到极端。"""
        import random
        hours = (time.time() - self.updated) / 3600
        if hours > 0.1:
            self._score += (50 - self._score) * min(0.6, hours * 0.1)
        self._score += random.uniform(-6, 6)
        self._score = max(0.0, min(100.0, self._score))
        self.updated = time.time()

    def bump(self, delta: float):
        if self._emotion is not None:
            return
        self._score = max(0.0, min(100.0, self._score + delta))
        self.updated = time.time()

    def on_chatted(self):
        """聊完一轮会稍微消耗一点，但被人搭话会涨。"""
        if self._emotion is not None:
            return
        self.bump(-2)

    def on_mentioned(self):
        if self._emotion is not None:
            return
        self.bump(+8)

    @property
    def label(self) -> str:
        return mood_label(self.score)

    def hint(self) -> str:
        """给模型的一句语气提示。"""
        if self.score >= 70:
            return "你现在心情不错，话可以多一点，愿意接梗。"
        if self.score >= 40:
            return ""
        if self.score >= 20:
            return "你现在有点困，回得短一点，别主动扯话题。"
        return "你现在懒得说话，能一个词回完就别写句子。"