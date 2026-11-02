"""表达层的「情绪 → 语气参数」纯函数。

这一层只把**不可变的情绪快照**（``emotion.Emotion.snapshot()``）加上
「这条消息跟她有多相关」翻成几个轻量参数：

    energy            语气有多活、起伏有多快（0 安静 ~ 1 活跃）
    warmth            语气有多暖、多愿意贴过去（0 疏 ~ 1 暖）
    softness          语气有多软、多收着（0 硬 ~ 1 软）
    rhythm            说话节奏：brisk / steady / slow
    length_allowance  允许的长度倍数（0.75~1.25，只是允许，不是要求）

它**不产出任何台词、情绪词或标点指令**：不要求她说「我难过」，也不要求
用「！！」喊出来。怎么把这些参数落成句子，仍由人设与主模型决定；这里只给
一个轻量的语气坐标。所有计算是纯函数、无 IO、无模型调用、无随机数。

给后续 ``ai_chat`` / ``proactive`` 接线用：

    from .affect import plan_for
    plan = plan_for(bot, ctx)          # 永远不抛异常，拿不到情绪时给中性计划
    plan.energy, plan.rhythm, ...      # 只读的冻结对象

纯函数入口（测试/离线评估直接用）：

    plan(snapshot, relevance=1.0, question=False, serious=False, roleplay=False)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .emotion import neutral_snapshot

RHYTHM_BRISK = "brisk"
RHYTHM_STEADY = "steady"
RHYTHM_SLOW = "slow"

# 长度允许的上下限：表达层只能在这个范围里收放，不能把「情绪」当无穷长理由。
MIN_LENGTH = 0.75
MAX_LENGTH = 1.25

_QUESTION_END_RE = re.compile(r"[？?]\s*$")
_QUESTION_WORD_RE = re.compile(r"(?:吗|呢|么)\s*[？?]?\s*$")


@dataclass(frozen=True)
class AffectPlan:
    """一份只读的表达计划。字段都是标量/枚举，没有任何可执行文本。"""

    energy: float
    warmth: float
    softness: float
    rhythm: str
    length_allowance: float

    @property
    def active(self) -> bool:
        """语气是否明显偏离中性；轻微偏离不值得在提示里多写一句。"""
        return (abs(self.energy - 0.5) >= 0.12
                or abs(self.warmth - 0.5) >= 0.12
                or abs(self.softness - 0.5) >= 0.12)


@dataclass(frozen=True)
class ContextSignals:
    """从上下文抽出的、与情绪刺激无关的轻量信号。"""

    relevance: float = 1.0
    question: bool = False
    serious: bool = False
    roleplay: bool = False


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _value(values: Mapping, dim: str) -> float:
    raw = values.get(dim) if isinstance(values, Mapping) else None
    try:
        return _clamp01((float(raw) - 50.0) / 50.0)
    except (TypeError, ValueError):
        return 0.0


def _avg(*items: float) -> float:
    return sum(items) / len(items)


def _snapshot_values(snapshot) -> Mapping:
    if snapshot is None:
        return {}
    values = getattr(snapshot, "values", snapshot)
    return values if isinstance(values, Mapping) else {}


def plan(snapshot, *, relevance: float = 1.0, question: bool = False,
         serious: bool = False, roleplay: bool = False) -> AffectPlan:
    """把情绪快照 + 相关度/上下文折成一份 AffectPlan（纯函数）。

    ``relevance``：0=完全旁听，1=直接冲她来。相关度越低，语气越往中性收、
    允许的长度越短；情绪本身仍会被读到，只是不外放。
    """
    values = _snapshot_values(snapshot)
    try:
        relevance = _clamp01(float(relevance))
    except (TypeError, ValueError):
        relevance = 1.0

    joy = _value(values, "开心")
    excited = _value(values, "兴奋")
    surprised = _value(values, "惊讶")
    annoyed = _value(values, "烦躁")
    low = _value(values, "低落")
    moved = _value(values, "感动")
    proud = _value(values, "得意")
    shy = _value(values, "害羞")

    # 原始坐标：只由情绪偏离决定，不看标点、不看随机数。
    raw_energy = (0.5
                  + 0.34 * _avg(excited, joy, proud)
                  + 0.18 * surprised
                  + 0.10 * annoyed
                  - 0.32 * low
                  - 0.06 * moved
                  - 0.04 * shy)
    raw_warmth = (0.5
                  + 0.30 * moved
                  + 0.18 * shy
                  + 0.10 * joy
                  - 0.20 * annoyed
                  - 0.04 * low)
    raw_softness = (0.5
                    + 0.30 * moved
                    + 0.22 * shy
                    + 0.20 * low
                    - 0.26 * annoyed
                    - 0.08 * excited
                    - 0.04 * proud)

    # 相关度把语气往中性拉：旁听别人的对话时，她可以心里有感觉，但表达收着。
    pull = 0.55 + 0.45 * relevance
    energy = _clamp01(0.5 + (raw_energy - 0.5) * pull)
    warmth = _clamp01(0.5 + (raw_warmth - 0.5) * pull)
    softness = _clamp01(0.5 + (raw_softness - 0.5) * pull)

    if energy >= 0.62:
        rhythm = RHYTHM_BRISK
    elif energy <= 0.38:
        rhythm = RHYTHM_SLOW
    else:
        rhythm = RHYTHM_STEADY

    length = (1.0
              + 0.28 * (energy - 0.5)
              - 0.06 * (softness - 0.5)
              - 0.18 * (1.0 - relevance))
    if question:
        length += 0.08          # 真问题允许说清楚，不为了短把答案砍掉
    if serious:
        length += 0.10          # 正事/纠错允许把话说完整
    if roleplay:
        length += 0.12          # 对戏本来就不按闲聊句数收
    length = max(MIN_LENGTH, min(MAX_LENGTH, length))

    return AffectPlan(energy=round(energy, 3),
                      warmth=round(warmth, 3),
                      softness=round(softness, 3),
                      rhythm=rhythm,
                      length_allowance=round(length, 3))


def signals_from_ctx(ctx) -> ContextSignals:
    """从聊天 ctx 里读「相不相关/是不是问题/是不是正事/是不是对戏」。

    只读属性，不联网、不落盘、不调用模型。ctx 缺字段时全部按中性处理。
    """
    if ctx is None:
        return ContextSignals()
    state = getattr(ctx, "state", None)
    state = state if isinstance(state, Mapping) else {}
    addressed = bool(getattr(ctx, "to_me", False)
                     or getattr(ctx, "matched_command", None)
                     or state.get("wake")
                     or state.get("addressed"))
    raw = getattr(ctx, "raw_text", None)
    if raw is None:
        event = getattr(ctx, "event", None)
        raw = getattr(event, "plain", "") if event is not None else ""
    text = str(raw or "").strip()
    question = bool(_QUESTION_END_RE.search(text) or _QUESTION_WORD_RE.search(text))
    return ContextSignals(
        relevance=1.0 if addressed else 0.35,
        question=question,
        serious=bool(state.get("serious") or state.get("factual")),
        roleplay=bool(state.get("roleplay") or state.get("rp")),
    )


def plan_for(bot, ctx=None) -> AffectPlan:
    """给 ``ai_chat`` / ``proactive`` 的便捷入口：拿 bot 的情绪快照 + ctx 信号。

    永不抛异常：没有 emotion、快照坏了、ctx 缺字段 → 返回中性计划。
    """
    try:
        emotion = getattr(bot, "emotion", None)
        snapshot = (emotion.snapshot()
                    if emotion is not None and hasattr(emotion, "snapshot")
                    else neutral_snapshot())
        signals = signals_from_ctx(ctx)
        return plan(snapshot, relevance=signals.relevance,
                    question=signals.question, serious=signals.serious,
                    roleplay=signals.roleplay)
    except Exception:
        return plan(None)


__all__ = [
    "AffectPlan",
    "ContextSignals",
    "MIN_LENGTH",
    "MAX_LENGTH",
    "RHYTHM_BRISK",
    "RHYTHM_SLOW",
    "RHYTHM_STEADY",
    "plan",
    "plan_for",
    "signals_from_ctx",
]
