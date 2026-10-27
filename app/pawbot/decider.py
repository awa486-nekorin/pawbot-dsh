#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""类 JEV 的决策模型客户端（OpenRouter `/alpha/decisions`）。

用途：把「心情矩阵」从关键词/普通 LLM 判官改成概率化决策模型：
  state = 当前情绪 + 上下文 + 新消息；
  questions = {是否明显变化(noul), 主导情绪(choice), 强度(score)}；
  answers 直接给概率/置信度，比让聊天模型输出 JSON 稳。

配置（emotion.decider_*，都在 config.py 的 DEFAULT 里）：
  enabled / model / base_url / api_key / timeout / min_shift / weight

key 解析顺序：
  1. config emotion.decider_api_key
  2. 环境变量 PAWBOT_DECIDER_KEY / OPENROUTER_API_KEY
  3. ~/.dsh/.credentials.yaml 里的 PAWBOT_DECIDER_KEY / OPENROUTER_API_KEY
不把 key 写进仓库、不打印 key、日志只记错误类型。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "perplexity/pplx-decider-v1.2"
DEFAULT_TIMEOUT = 12.0
DEFAULT_MIN_SHIFT = 0.5
DEFAULT_WEIGHT = 1.0

EMOTIONS = ("开心", "兴奋", "惊讶", "烦躁", "低落", "感动", "得意", "害羞")
NEGATIVE = {"烦躁", "低落"}
POSITIVE = {"开心", "兴奋", "感动", "得意", "害羞"}

_CRED_PATHS = (
    Path.home() / ".dsh" / ".credentials.yaml",
    Path(os.environ.get("DSH_HOME", "")) / ".credentials.yaml" if os.environ.get("DSH_HOME") else None,
)

# 专用 key 文件（在仓库/插件目录之外，升级不会碰）：~/.pawbot/decider.key
_KEY_FILES = (
    Path.home() / ".pawbot" / "decider.key",
    Path.home() / ".pawbot" / "decider_key.txt",
)


class DeciderError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _cfg(config, key: str, default=None):
    try:
        value = config.get(key, default)
    except TypeError:
        # 兼容只支持 get(key) 的假 config
        try:
            value = config.get(key)
        except Exception:
            value = default
    except Exception:
        value = default
    return default if value is None else value


def _credential(name: str) -> str:
    for path in _CRED_PATHS:
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = re.search(rf"^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", text, re.M)
        if match:
            value = match.group(1).strip().strip('"').strip("'")
            if value:
                return value
    return ""


def resolve_key(config) -> str:
    key = str(_cfg(config, "emotion.decider_api_key", "") or "").strip()
    if key:
        return key
    for env in ("PAWBOT_DECIDER_KEY", "OPENROUTER_API_KEY", "OPENROUTER_DECISION_API_KEY"):
        value = os.environ.get(env, "").strip()
        if value:
            return value
    explicit = os.environ.get("PAWBOT_DECIDER_KEY_FILE", "").strip()
    key_files = ([Path(explicit)] if explicit else []) + list(_KEY_FILES)
    for path in key_files:
        try:
            value = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if value:
            return value
    for name in ("PAWBOT_DECIDER_KEY", "OPENROUTER_API_KEY", "OPENROUTER_DECISION_API_KEY"):
        value = _credential(name)
        if value:
            return value
    return ""


def base_url(config) -> str:
    return str(_cfg(config, "emotion.decider_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).strip()


def model(config) -> str:
    return str(_cfg(config, "emotion.decider_model", DEFAULT_MODEL) or DEFAULT_MODEL).strip()


def enabled(config) -> bool:
    return bool(_cfg(config, "emotion.decider_enabled", False))


def _num(config, key: str, default: float) -> float:
    try:
        return float(_cfg(config, key, default))
    except (TypeError, ValueError):
        return default


def _post(url: str, key: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "HTTP-Referer": "http://127.0.0.1",
            "X-Title": "PawBot",
            "User-Agent": "PawBot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise DeciderError(f"HTTP_{exc.code}", detail) from None
    except urllib.error.URLError as exc:
        raise DeciderError("UNREACHABLE", str(getattr(exc, "reason", exc))) from None
    except TimeoutError:
        raise DeciderError("TIMEOUT") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise DeciderError("BAD_JSON", raw[:200]) from None
    if not isinstance(data, dict):
        raise DeciderError("BAD_RESPONSE", "response is not an object")
    return data


async def decide(config, state, questions, *, timeout: float | None = None) -> dict:
    """调一次决策模型，返回 answers（原样结构）。失败抛 DeciderError。"""
    key = resolve_key(config)
    if not key:
        raise DeciderError("NO_KEY")
    payload = {"model": model(config), "state": state, "questions": questions}
    to = float(timeout if timeout is not None else _num(config, "emotion.decider_timeout", DEFAULT_TIMEOUT))
    data = await asyncio.to_thread(_post, base_url(config), key, payload, to)
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise DeciderError("BAD_RESPONSE", "response has no answers")
    return answers


def _choice(answer, default: str = "") -> tuple[str, float, dict]:
    if not isinstance(answer, dict):
        return default, 0.0, {}
    choice = str(answer.get("choice") or default)
    try:
        confidence = float(answer.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    probs = answer.get("probabilities") if isinstance(answer.get("probabilities"), dict) else {}
    return choice, confidence, probs


def _noul(answer, default: float = 0.0) -> float:
    if isinstance(answer, dict):
        try:
            return float(answer.get("noul"))
        except (TypeError, ValueError):
            pass
    try:
        return float(answer)
    except (TypeError, ValueError):
        return default


def _score(answer, default: float = 0.0) -> float:
    if isinstance(answer, dict):
        if "score" in answer:
            try:
                return max(0.0, min(1.0, float(answer.get("score"))))
            except (TypeError, ValueError):
                pass
        probs = answer.get("probabilities")
        if isinstance(probs, dict) and probs:
            # 没有 score 时按概率分布求期望（legend 的键通常是 0..n）
            total = 0.0
            weight = 0.0
            for k, v in probs.items():
                try:
                    total += float(k) * float(v)
                    weight += float(v)
                except (TypeError, ValueError):
                    continue
            if weight:
                return max(0.0, min(1.0, total / weight))
    try:
        return max(0.0, min(1.0, float(answer)))
    except (TypeError, ValueError):
        return default


def mood_questions() -> dict:
    return {
        "shift": {
            "type": "noul",
            "instructions": "这条消息会不会让她的情绪产生明显变化（不是普通寒暄）？",
        },
        "emotion": {
            "type": "choice",
            "instructions": "她最可能变成哪种主导情绪？",
            "criteria": {
                **{name: name for name in EMOTIONS},
                "none": "没有明显情绪变化",
            },
        },
        "intensity": {
            "type": "score",
            "instructions": "如果情绪有变化，强度大概多强？",
            "criteria": ["轻微", "明显", "强烈"],
        },
    }


def deltas_from_answers(answers: dict, *, current: dict | None = None,
                        weight: float = DEFAULT_WEIGHT,
                        min_shift: float = DEFAULT_MIN_SHIFT) -> dict:
    """把 answers 映射成情绪增减（键给 emotion.DIMS 用，值 -15..15）。

    shift 低于阈值 / choice=none 时返回 {}（交给关键词兜底）。
    """
    shift = _noul(answers.get("shift"), 0.0)
    choice, confidence, probs = _choice(answers.get("emotion"), "")
    intensity = _score(answers.get("intensity"), 0.5)
    if shift < min_shift or not choice or choice == "none":
        return {}
    if choice not in EMOTIONS:
        return {}
    # 基础幅度：强度 0.66 左右（明显）时约 10；强烈约 13~15；轻微 5~7
    base = max(4.0, min(15.0, 4.0 + intensity * 11.0)) * max(0.1, weight)
    # 置信度太低就收一点，别让一次模糊判断把矩阵拉爆
    base *= max(0.5, min(1.0, confidence or 0.6))
    value = int(round(base))
    deltas: dict[str, int] = {choice: value}
    if choice in NEGATIVE:
        for other in ("开心", "得意"):
            deltas[other] = -int(round(value * 0.5))
    elif choice in POSITIVE:
        for other in ("烦躁", "低落"):
            deltas[other] = -int(round(value * 0.4))
        if choice in ("开心", "得意"):
            deltas["感动"] = int(round(value * 0.3))
    # 其他维度轻微回落，避免长期全高
    for dim in EMOTIONS:
        if dim not in deltas:
            deltas[dim] = -1
    return {k: v for k, v in deltas.items() if v}


def build_state(text: str, context: str, addressed: bool, values: dict | None) -> str:
    current = "，".join(f"{dim} {int(round(float((values or {}).get(dim, 50))))}" for dim in EMOTIONS)
    lines = [
        "角色：大肥鱼，二次元少女群友（不是助手），心情会随聊天自然起伏。",
        f"当前情绪（基准 50）：{current}",
    ]
    ctx = "\n".join(" ".join(line.split()) for line in str(context or "").splitlines()[-4:])[:600]
    if ctx:
        lines.append(f"最近上下文：\n{ctx}")
    who = "这条消息在叫她/冲她来" if addressed else "这条消息不一定是在叫她"
    lines.append(f"新消息（{who}）：{str(text or '')[:500]}")
    return "\n".join(lines)


async def decide_mood(config, text: str, context: str = "", addressed: bool = False,
                      values: dict | None = None) -> dict | None:
    """给 emotion.update 用：成功返回 deltas；未启用/失败返回 None（走兜底）。"""
    if not enabled(config):
        return None
    try:
        answers = await decide(
            config,
            build_state(text, context, addressed, values),
            mood_questions(),
            timeout=_num(config, "emotion.decider_timeout", DEFAULT_TIMEOUT),
        )
    except DeciderError as exc:
        _log_debug(f"决策模型失败：{exc.code}")
        return None
    except Exception as exc:  # noqa: BLE001
        _log_debug(f"决策模型异常：{type(exc).__name__}")
        return None
    return deltas_from_answers(
        answers,
        current=values,
        weight=_num(config, "emotion.decider_weight", DEFAULT_WEIGHT),
        min_shift=_num(config, "emotion.decider_min_shift", DEFAULT_MIN_SHIFT),
    )


def _log_debug(message: str) -> None:
    try:
        from .log import log

        log.debug(message)
    except Exception:
        pass
