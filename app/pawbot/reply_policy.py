"""回复清洗策略：把 ``reply.*`` 配置收成一个显式、可注入的冻结对象。

``style.polish(text, policy=...)`` 拿到这个对象后**不再读固定路径的
``data/config.json``**，也不会因为测试环境缺文件而退回旧行为。这样后续
``ai_chat`` / ``proactive`` 接线时只要把 ``policy_for(bot)`` 传进去即可，
离线测试也能用 ``ReplyPolicy(...)`` 直接构造。

这里不新增 ``config.py`` 的 DEFAULT 键：只读已经存在的 ``reply.max_parts`` /
``reply.strip_ai_phrases`` / ``reply.body_reaction_max`` /
``reply.strip_filler_opener`` / ``reply.tone_trim`` / ``reply.space_to_comma`` /
``reply.period_soften``；语义保护开关
（``reply.semantic_guard`` 等）是可选的，没配就按「开」处理。

和旧行为的关系：

    ReplyPolicy.from_config(bot.config)   新语义（保护真问句/立场/语气词）
    ReplyPolicy.legacy()                  旧语义（保持既有输出不变）

``style.polish`` 不传 policy 时仍走旧的配置读取，输出语义不变。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _lookup(config, dotted: str, default=None):
    """先按 ``config.get("reply.x")`` 读，再支持嵌套 dict / 普通对象属性。"""
    if config is None:
        return default
    try:
        if hasattr(config, "get"):
            value = config.get(dotted)
            if value is not None:
                return value
    except Exception:
        pass
    node = config
    for part in dotted.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            try:
                node = getattr(node, part)
            except Exception:
                return default
    return node


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "off", "no", "")
    return bool(value)


def _as_int(value, default: int, *, minimum: int = 1) -> int:
    if value is None:
        return default
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _as_ratio(value, default: float) -> float:
    if value is None:
        return default
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ReplyPolicy:
    """一份只读的回复清洗策略；字段与 ``style.polish`` 的开关一一对应。"""

    max_parts: int = 3
    strip_ai_phrases: bool = True
    body_reaction_max: int = 1
    strip_filler_opener: bool = True
    space_to_comma: bool = False
    period_soften: bool = False
    tone_trim: float = 0.7
    semantic_guard: bool = True
    preserve_questions: bool = True
    preserve_stance: bool = True
    preserve_particles: bool = True
    protect_literals: bool = True

    @classmethod
    def legacy(cls) -> "ReplyPolicy":
        """旧语义：关闭全部语义保护，用于对比/回滚测试。"""
        return cls(semantic_guard=False,
                   preserve_questions=False,
                   preserve_stance=False,
                   preserve_particles=False,
                   protect_literals=False)

    @classmethod
    def from_config(cls, config) -> "ReplyPolicy":
        """从配置对象/嵌套 dict 构造；不读文件、不做 IO。"""
        guard = _as_bool(_lookup(config, "reply.semantic_guard"), True)
        return cls(
            max_parts=_as_int(_lookup(config, "reply.max_parts"), 3, minimum=1),
            strip_ai_phrases=_as_bool(
                _lookup(config, "reply.strip_ai_phrases"), True),
            body_reaction_max=_as_int(
                _lookup(config, "reply.body_reaction_max"), 1, minimum=0),
            strip_filler_opener=_as_bool(
                _lookup(config, "reply.strip_filler_opener"), True),
            space_to_comma=_as_bool(
                _lookup(config, "reply.space_to_comma"), False),
            period_soften=_as_bool(
                _lookup(config, "reply.period_soften"), False),
            tone_trim=_as_ratio(_lookup(config, "reply.tone_trim"), 0.7),
            semantic_guard=guard,
            preserve_questions=_as_bool(
                _lookup(config, "reply.preserve_questions"), guard),
            preserve_stance=_as_bool(
                _lookup(config, "reply.preserve_stance"), guard),
            preserve_particles=_as_bool(
                _lookup(config, "reply.preserve_particles"), guard),
            protect_literals=_as_bool(
                _lookup(config, "reply.protect_literals"), guard),
        )


def policy_for(bot) -> ReplyPolicy:
    """给后续 ``ai_chat`` / ``proactive`` 接线用：从 ``bot.config`` 取策略。"""
    return ReplyPolicy.from_config(getattr(bot, "config", None))


__all__ = ["ReplyPolicy", "policy_for"]
