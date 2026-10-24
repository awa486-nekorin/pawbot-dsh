"""
判官模型：一个更便宜、更快的「二次判断」通道。

跟主聊天模型分开配（judge.*）；没配的键就从 llm.* 继承连接信息。
采样参数用判官自己的默认值（低温、低思考、短预算）——它只做 JSON
小判断，比如「这条消息让她心情怎么变」，不需要会聊天。

    data = await judge_json(config, prompt, timeout=4)
    if data is None: ...          # 任何失败都返回 None，调用方自己兜底

key 的优先级：环境变量 PAWBOT_JUDGE_API_KEY → judge.api_key → llm.api_key。
这里从不打印 key，日志里也只记异常类型。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

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
