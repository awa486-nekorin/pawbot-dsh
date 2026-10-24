"""
语音理解：把 QQ 语音转成文字，再交给聊天模型。

QQ 的语音是 SILK 格式，本机没有解码器；走 NapCat 的 `get_record`
（`out_format=mp3`）让协议端先转好，再把 mp3 交给 Gemini 的音频输入。
实测（2026-10-21）：同一段中文语音，wav 6.3 秒、mp3 4.2 秒就能逐字转写。

这里只管「音频字节 → 文字」。取音频、调用时机都在 perceive。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from .log import log

_PROMPT = "把这段语音逐字转写成文字，只输出原话。听不清就说「听不清」，不要猜。"


def _cfg(config, key, default=None):
    try:
        return config.get(f"speech.{key}", default)
    except Exception:
        return default


def _creds(config) -> tuple[str, str, str]:
    """语音也走多模态那套凭据；没单独配就跟看图一致。"""
    key = str(_cfg(config, "api_key", "") or config.get("llm.vision_api_key", "")
              or config.get("llm.api_key", "")).strip()
    base = str(_cfg(config, "base_url", "") or config.get("llm.vision_base_url", "")
               or config.get("llm.base_url", ""))
    model = str(_cfg(config, "model", "") or config.get("llm.vision_model", "")
                or config.get("llm.model", ""))
    return key, base, model


def transcribe(raw: bytes, config, *, fmt: str = "mp3", timeout: int = 60) -> str:
    """把音频字节转成文字；任何失败都回空串，不影响正常聊天。"""
    if not raw:
        return ""
    key, base, model = _creds(config)
    if not key or not base:
        return ""
    b64 = base64.b64encode(raw).decode("ascii")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
            {"type": "text", "text": _PROMPT},
        ]}],
        "max_tokens": int(_cfg(config, "max_tokens", 400) or 400),
        "temperature": 0.0,
    }
    effort = str(_cfg(config, "reasoning_effort", "")
                 or config.get("llm.reasoning_effort", "")).strip().lower()
    if effort in ("low", "medium", "high"):
        payload["reasoning_effort"] = effort
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 # 中转渠道的 Cloudflare 会拦没有 UA 的请求
                 "User-Agent": "PawBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warn(f"语音转写失败 HTTP {e.code}")
        return ""
    except Exception as e:
        log.warn(f"语音转写失败：{type(e).__name__} {e}")
        return ""
    try:
        text = (data["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""
    text = " ".join(text.split())
    limit = int(_cfg(config, "max_chars", 300) or 300)
    return text[:limit]
