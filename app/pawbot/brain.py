"""
DSH 大脑：把「大肥鱼的一轮对话」交给 DSH 进程内的 Agent（可以用电脑工具）。
================================================================================
原来的链路是 `PawBot → 外部 /chat/completions`。开 `llm.provider = "dsh"` 之后变成：

    PawBot（Python）
      └─ POST {PAWBOT_DSH_BRAIN_URL}（Host 插件注入的 loopback 地址 + Bearer token）
            └─ DSH Host：创建进程内 Agent（complete 人设 + 电脑工具 + PawBot 工具代理）
                  ├─ 电脑工具：pwsh / read / write / edit / glob / grep / web_search / web_fetch
                  └─ PawBot 工具：回调本进程 `POST /api/brain/tool`（用真实 ctx 执行）
            └─ 回 { ok, text, media, provider, model, tools_used, ... }

这个模块只做三件事：
  1. 从环境变量取 Host 注入的入口与 token（没注入 = 没开 DSH 大脑 → 上层回退直连 API）；
  2. 发一轮请求、把错误归一成 BrainError（上层能读懂为什么失败）；
  3. 维护「当前轮次 → ctx」的登记表：Host 回调执行 PawBot 工具时，用真 ctx 跑
     （工具的权限、群/私聊、主人判断都依赖它，不能拿假 ctx 顶）。

安全边界：
  - token 只存在于 Host 子进程环境变量和本进程内存里，不落盘、不打日志；
  - 不暴露给模型：这个模块只被 llm.py / agent.py / webui.py 内部调用；
  - 工具执行只接受「本轮登记过、且本轮 schemas 里出现过的工具名」，不接受任意名字。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .log import log

URL_ENV = "PAWBOT_DSH_BRAIN_URL"
TOKEN_ENV = "PAWBOT_DSH_BRAIN_TOKEN"

# 单轮请求上限（Host 侧另有自己的超时；这里只做兜底）——留点余量给工具调用。
MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 90
# 与 Host 的 bodyBytes 上限（2MB）对齐，本地先拦，避免每轮 413 且没有诊断。
MAX_BODY = 2 * 1024 * 1024 - 64 * 1024


class BrainError(RuntimeError):
    """DSH 大脑不可用/这一轮失败。上层据此决定回退直连 API 还是如实报错。"""

    def __init__(self, message: str, *, code: str = "BRAIN_ERROR", status: int = 0,
                 detail: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail

    def as_dict(self) -> dict:
        return {"code": self.code, "status": self.status, "message": str(self),
                **({"detail": self.detail} if self.detail else {})}


# ── 环境 ─────────────────────────────────────────────────────────────────────
# 注意：URL/token 在 import 时就取出来并从 os.environ 里删掉。
# PawBot 之后还会 spawn TTS / MCP / ffmpeg 等子进程，不 pop 的话它们会继承这个 token；
# Linux 上同用户还能从 /proc/<pid>/environ 读到。删掉后本进程仍持有（模块级变量）。
_URL = str(os.environ.pop(URL_ENV, "") or "").strip()
_TOKEN = str(os.environ.pop(TOKEN_ENV, "") or "").strip()


def env_url() -> str:
    return _URL


def env_token() -> str:
    return _TOKEN


def enabled() -> bool:
    return bool(_URL and _TOKEN)


def status() -> dict:
    """给面板/诊断用：不含 token 值。"""
    return {
        "enabled": enabled(),
        "has_url": bool(_URL),
        "has_token": bool(_TOKEN),
        "active_turns": len(_ACTIVE),
    }


# ── 当前轮次登记表（Host 回调执行工具时用） ──────────────────────────────────

_ACTIVE: dict[str, object] = {}
_ALLOWED: dict[str, set[str]] = {}


def register(conv: str, ctx, allowed: set[str] | list[str] | None = None) -> None:
    """登记「这一轮由哪个 ctx 负责执行 PawBot 工具」。"""
    if not conv:
        return
    _ACTIVE[conv] = ctx
    if allowed is not None:
        _ALLOWED[conv] = {str(x) for x in allowed}


def unregister(conv: str) -> None:
    _ACTIVE.pop(conv, None)
    _ALLOWED.pop(conv, None)


def active_ctx(conv: str):
    return _ACTIVE.get(conv)


def tool_allowed(conv: str, name: str) -> bool:
    """fail-closed：只有本轮明确登记过、且工具名在本轮 schemas 里，才允许执行。"""
    allowed = _ALLOWED.get(conv)
    if allowed is None:
        return False
    return str(name) in allowed


# ── 请求 ─────────────────────────────────────────────────────────────────────

def turn(payload: dict, *, timeout: float | int | None = None) -> dict:
    """发一轮请求给 DSH 大脑；失败抛 BrainError（带 code/status）。"""
    url, token = env_url(), env_token()
    if not url or not token:
        raise BrainError("DSH 大脑未接通（Host 没注入 brain URL/token）",
                         code="BRAIN_DISABLED")

    seconds = float(timeout or DEFAULT_TIMEOUT)
    seconds = max(1.0, min(MAX_TIMEOUT, seconds))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BODY:
        raise BrainError(
            f"这一轮的 system/工具 schema 太大（{len(body)} B > {MAX_BODY} B），Host 会 413；"
            "请减少记忆块或工具数量",
            code="PAYLOAD_TOO_LARGE")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "PawBot/1.0 (dsh-brain)",
    })
    try:
        with urllib.request.urlopen(req, timeout=seconds) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        code, message, detail = _error_of(raw, f"HTTP {e.code}")
        raise BrainError(f"DSH 大脑拒绝请求：{message}", code=code, status=e.code,
                         detail=detail) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise BrainError(f"DSH 大脑连不上（{reason}）", code="BRAIN_UNREACHABLE") from e

    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        raise BrainError("DSH 大脑回包不是 JSON", code="BAD_RESPONSE",
                         detail=raw[:300]) from e
    if not isinstance(data, dict):
        raise BrainError("DSH 大脑回包不是 object", code="BAD_RESPONSE")
    if data.get("ok") is not True:
        code, message, detail = _error_of(raw, "unknown")
        raise BrainError(f"DSH 大脑这一轮失败：{message}", code=code, detail=detail)
    return data


def _error_of(raw: str, fallback: str) -> tuple[str, str, str]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return (str(err.get("code") or fallback),
                str(err.get("message") or err.get("code") or fallback),
                "")
    return fallback, fallback, raw[:300]


def selftest(prompt: str = "", system: str = "", timeout: float | int | None = None) -> dict:
    """一轮「不经过 QQ」的自检（给 /api/llm/test 与面板用）。"""
    payload = {
        "conv": "pawbot-selftest",
        "system": system or "你是一个用一句话回答的中文助手。",
        "user": prompt or "用一句话说明你现在在正常工作。",
        "tools": [],
        "options": {},
    }
    return turn(payload, timeout=timeout or 60)


def announce_failure(error: Exception) -> None:
    """把失败写成一行日志（token 已在 Host 侧遮罩；这里也不打 URL）。"""
    if isinstance(error, BrainError):
        log.warn(f"DSH 大脑失败 [{error.code}/{error.status or '-'}]：{error}")
    else:
        log.warn(f"DSH 大脑失败：{type(error).__name__}: {error}")
