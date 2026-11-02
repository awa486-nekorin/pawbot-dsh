"""
判官模型：一个更便宜、更快的「二次判断」通道。

跟主聊天模型分开配（judge.*）；没配的键就从 llm.* 继承连接信息。
采样参数用判官自己的默认值（低温、低思考、短预算）——它只做 JSON
小判断，比如「这条消息让她心情怎么变」，不需要会聊天。

    data = await judge_json(config, prompt, timeout=4)
    if data is None: ...          # 任何失败都返回 None，调用方自己兜底

另有一条可选的决策模型路径（judge.decider_*，默认关）：

    data = await judge_quality_json(config, 文字线索, timeout=8)

它只回答「这张图值不值得发 / 画质哪一档」这类可枚举判断——决策模型是纯文本
模型，看不到图，所以不能替代需要看图的描述。关闭、没 key、超时、HTTP/解析
失败都会无感退回 judge_json，绝不向业务抛错。

key 的优先级：环境变量 PAWBOT_JUDGE_API_KEY → judge.api_key → llm.api_key。
决策模型单独用 judge.decider_api_key → PAWBOT_JUDGE_DECIDER_KEY /
OPENROUTER_API_KEY（详见 _resolve_decider_key）。这里从不打印 key，日志里也只记
错误码/异常类型。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from . import decider
from .llm import LLMClient
from .log import log

_MISSING = object()

# 判官自己的采样默认值：比聊天更冷、更省、更快。
# 只影响这几个键；model / base_url 之类还是优先继承 llm.*。
_JUDGE_TUNE = {
    "reasoning_effort": "low",
    "temperature": 0.2,
    "max_tokens": 600,
    "timeout": 20,
}


def _resolve_key(config) -> str:
    """按优先级找 key，只返回字符串，绝不打印。"""
    env = os.environ.get("PAWBOT_JUDGE_API_KEY", "")
    if isinstance(env, str) and env.strip():
        return env.strip()
    for scope in ("judge", "llm"):
        try:
            val = config.get(f"{scope}.api_key", "")
        except Exception:
            val = ""
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _raw(config, dotted: str):
    """读一个配置项；不存在 / None / 空串都算没配。"""
    try:
        val = config.get(dotted, _MISSING)
    except Exception:
        return _MISSING
    if val is None or (isinstance(val, str) and not val.strip()):
        return _MISSING
    return val


class JudgeLLM(LLMClient):
    """判官专用客户端：judge.* 优先，其次判官默认值，最后 llm.*。"""

    def _cfg(self, key, default=None):
        if key == "api_key":
            return _resolve_key(self.config)
        val = _raw(self.config, f"judge.{key}")
        if val is _MISSING:
            # 采样参数不跟聊天共用（否则会拿到 0.8 / 900 / medium）
            if key in _JUDGE_TUNE:
                return _JUDGE_TUNE[key]
            val = _raw(self.config, f"llm.{key}")
        return default if val is _MISSING else val


def _sniff_mime(raw: bytes) -> str:
    """按魔数认图片格式，认不出就按 jpeg 发。"""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"GIF8":
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _first_json(text) -> dict | None:
    """从回复里抠第一个 JSON 对象，容忍 ```json 围栏和前后废话。"""
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def judge_enabled(config) -> bool:
    """judge.enabled（默认开）+ 至少有一个可用 key。"""
    try:
        if not config.get("judge.enabled", True):
            return False
    except Exception:
        return False
    return bool(_resolve_key(config))


_JSON_RULE = (
    "\n\n输出格式（硬性要求，违反这次判断就作废）："
    "回复里必须有 JSON，第一个非空白字符是 { ，最后一个非空白字符是 } ，"
    "能直接 json.loads 解析。不要 ```json 代码块、不要前后缀、不要解释、不要语气词，"
    "也不要先想一段再给 JSON。没有内容可判也要输出 {}。"
)


async def judge_json(config, prompt: str, *, image: bytes | None = None,
                     timeout: float = 8.0) -> dict | None:
    """问一次判官，拿回一个 JSON 对象；任何失败都返回 None。"""
    try:
        if not _resolve_key(config):
            log.debug("判官没有可用 key，跳过")
            return None

        content: object = str(prompt or "") + _JSON_RULE
        if image:
            mime = _sniff_mime(image)
            b64 = base64.b64encode(image).decode("ascii")
            content = [
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]
        messages = [{"role": "user", "content": content}]

        client = JudgeLLM(config)
        reply = await asyncio.wait_for(client.chat(messages),
                                       timeout=max(0.1, float(timeout)))
        data = _first_json(reply)
        if data is None:
            log.debug("判官回复里没有 JSON，按失败处理")
        return data
    except asyncio.TimeoutError:
        log.debug(f"判官超时（{timeout}s），跳过")
        return None
    except Exception as e:                      # 网络、解析、配置，一律吞掉
        log.warn(f"判官调用失败：{type(e).__name__}")
        return None


# ====================================================================== 决策模型档位
# 决策模型（decider.py）是纯文本模型：只给它文字线索，只问「值得发吗 / 画质
# 哪一档」这类可枚举问题。要描述画面内容必须走 judge_json 的视觉通道，不要用这里。

_JUDGE_DECIDER_ENVS = ("PAWBOT_JUDGE_DECIDER_KEY", "OPENROUTER_API_KEY")
# judge 专用的 key 文件（放在仓库外，升级不碰）；不共用 emotion 的 decider.key。
_JUDGE_DECIDER_KEY_FILES = (
    Path.home() / ".pawbot" / "judge-decider.key",
    Path.home() / ".pawbot" / "judge_decider_key.txt",
)

DEFAULT_DECIDER_TIMEOUT = 8.0
DEFAULT_DECIDER_MIN_WORTH = 0.5

# 档位 → 判官分数区间（1-10）。默认门槛 tools.image_judge_min=6，
# 所以 low/reject 会被淘汰，只有 high/ok 能留下。
QUALITY_TIERS = {
    "high": (8, 10),
    "ok": (6, 7),
    "low": (4, 5),
    "reject": (1, 3),
}
_TIER_LABELS = {
    "high": "画质好",
    "ok": "能发",
    "low": "画质偏低",
    "reject": "不建议发",
}


class _DeciderConfig:
    """把 judge.decider_* 投影成 decider.decide() 认识的 emotion.decider_*。

    decider.py 的请求/鉴权/answers 解析已有测试覆盖，这里只换前缀，
    不复制它的 HTTP 实现；是否走这条路由 judge_decider_enabled 先判断，
    所以 emotion.decider_enabled 恒为 True。
    """

    def __init__(self, config, key: str) -> None:
        self._config = config
        self._key = key

    def get(self, dotted: str, default=None):
        if dotted == "emotion.decider_enabled":
            return True
        if dotted == "emotion.decider_api_key":
            return self._key
        prefix = "emotion.decider_"
        if isinstance(dotted, str) and dotted.startswith(prefix):
            val = _raw(self._config, f"judge.decider_{dotted[len(prefix):]}")
            return default if val is _MISSING else val
        return default


def judge_decider_enabled(config) -> bool:
    """judge.decider_enabled（默认关）；开了但没 key 时调用方仍会退回判官。"""
    val = _raw(config, "judge.decider_enabled")
    return bool(val) if val is not _MISSING else False


def _cfg_num(config, dotted: str, default: float) -> float:
    val = _raw(config, dotted)
    if val is _MISSING:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _resolve_decider_key(config) -> str:
    """决策模型 key，优先级与 decider.py 一致：

    面板 judge.decider_api_key → 环境变量 PAWBOT_JUDGE_DECIDER_KEY /
    OPENROUTER_API_KEY → judge 专用 key 文件 → ~/.dsh/.credentials.yaml
    （凭证文件复用 decider.py 的那一套）。只返回字符串，绝不打印。
    """
    panel = _raw(config, "judge.decider_api_key")
    if panel is not _MISSING:
        return str(panel).strip()
    for env_name in _JUDGE_DECIDER_ENVS:
        value = os.environ.get(env_name, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    explicit = os.environ.get("PAWBOT_JUDGE_DECIDER_KEY_FILE", "").strip()
    for path in ([Path(explicit)] if explicit else []) + list(_JUDGE_DECIDER_KEY_FILES):
        try:
            value = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if value:
            return value
    for name in _JUDGE_DECIDER_ENVS:
        value = decider._credential(name)
        if value:
            return value
    return ""


def quality_questions() -> dict:
    """画质判官的决策问题：只问文字线索能答的可枚举判断。"""
    return {
        "worth": {
            "type": "noul",
            "instructions": "只看这些文字线索（来源/标签/尺寸/说明），这张图值得发到群里吗？",
        },
        "tier": {
            "type": "choice",
            "instructions": "按文字线索，这张图的画质大概属于哪一档？",
            "criteria": {
                "high": "画质好，愿意发给朋友",
                "ok": "能看，可以发",
                "low": "糊/崩/截图/纯文字，勉强能看",
                "reject": "不该发（广告/梗图截图/明显崩坏）",
            },
        },
        "polish": {
            "type": "score",
            "instructions": "如果一定要打个美术分，大概多高？",
            "criteria": ["差", "一般", "能发", "很好"],
        },
    }


def build_quality_state(prompt: str) -> str:
    """决策模型的 state：纯文字，并明确告诉它看不到画面。"""
    clue = " ".join(str(prompt or "").split())[:800]
    return "\n".join([
        "角色：给 QQ 群挑图的大肥鱼，只做「发不发 / 画质档位」的判断。",
        "你只拿到文字线索，看不到画面；不要假装看过图，也不要描述画面内容。",
        f"线索：{clue}",
    ])


def quality_from_answers(answers, *,
                         min_worth: float = DEFAULT_DECIDER_MIN_WORTH) -> dict | None:
    """把决策模型 answers 映射成判官结果；认不出档位时返回 None（交给 LLM）。

    | 决策 | 值 | 判官 score | reason |
    |---|---|---|---|
    | tier | high | 8-10（按 polish 在区间内插值） | 决策:画质好 |
    | tier | ok | 6-7 | 决策:能发 |
    | tier | low | 4-5 | 决策:画质偏低 |
    | tier | reject | 1-3 | 决策:不建议发 |
    | worth < min_worth | 任意档 | 压到 ≤3 | 决策:不值得发 |
    """
    if not isinstance(answers, dict):
        return None
    tier, _confidence, _probs = decider._choice(answers.get("tier"), "")
    if tier not in QUALITY_TIERS:
        return None
    worth = decider._noul(answers.get("worth"), 1.0)
    polish = decider._score(answers.get("polish"), 0.5)
    lo, hi = QUALITY_TIERS[tier]
    score = int(round(lo + max(0.0, min(1.0, polish)) * (hi - lo)))
    gated = worth < min_worth
    if gated:
        score = min(score, 3)
    label = _TIER_LABELS[tier]
    if gated and tier != "reject":
        label = "不值得发"
    return {
        "score": max(1, min(10, score)),
        "reason": f"决策:{label}"[:20],
        "tier": tier,
        "worth": round(worth, 3),
        "source": "decider",
    }


async def judge_quality_json(config, prompt: str, *, image: bytes | None = None,
                             timeout: float = 8.0) -> dict | None:
    """画质判官：决策模型优先，任何失败都无感退回 judge_json（视觉判官）。

    决策模型只吃文字线索，做「值得发吗 / 哪一档」这类可枚举判断；它看不到图，
    所以 image 只在退回判官时使用，绝不塞给决策模型。返回
    {"score": 1..10, "reason", "tier", "worth", "source": "decider"}，
    与 judge_json 的 {"score", "reason"} 兼容；退回时原样返回 judge_json 的结果。
    """
    if judge_decider_enabled(config):
        try:
            key = _resolve_decider_key(config)
        except Exception as exc:                  # noqa: BLE001
            key = ""
            log.warn(f"画质决策模型 key 解析失败（{type(exc).__name__}），退回判官")
        if key:
            try:
                caller_timeout = float(timeout)
            except (TypeError, ValueError):
                caller_timeout = 8.0
            wait = max(1.0, min(max(0.1, caller_timeout),
                                max(1.0, _cfg_num(config, "judge.decider_timeout",
                                                  DEFAULT_DECIDER_TIMEOUT))))
            try:
                answers = await asyncio.wait_for(
                    decider.decide(
                        _DeciderConfig(config, key),
                        build_quality_state(prompt),
                        quality_questions(),
                        timeout=wait,
                    ),
                    timeout=wait,
                )
                data = quality_from_answers(
                    answers,
                    min_worth=_cfg_num(config, "judge.decider_min_worth",
                                       DEFAULT_DECIDER_MIN_WORTH),
                )
            except asyncio.TimeoutError:
                log.debug(f"画质决策模型超时（{wait:.0f}s），退回判官")
            except decider.DeciderError as exc:
                log.debug(f"画质决策模型失败（{exc.code}），退回判官")
            except Exception as exc:              # noqa: BLE001
                log.warn(f"画质决策模型异常（{type(exc).__name__}），退回判官")
            else:
                if data is not None:
                    return data
                log.debug("画质决策模型没给出可用档位，退回判官")
    return await judge_json(config, prompt, image=image, timeout=timeout)
