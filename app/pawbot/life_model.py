"""生活/自主后台的共用模型入口。

背景：``LLMClient`` 只有在调用方传入 ``brain_conv`` 时才会把这一轮交给
``llm.provider=dsh`` 的 DSH 大脑；自主规划/反应这类后台调用以前没传，
在只配了 DSH、没有直连 ``api_key`` 的部署里会落到 mock，规划永远失败。

这里集中提供 :func:`chat`：

* ``provider=dsh`` 且 DSH 大脑桥真的接通时，用**每次唯一**的
  ``life:<purpose>:<uuid>`` 轮次改道大脑，并明确标记这是自主轮次：
  非主人、无电脑能力、无原生工具；不传任何工具 schema，也不伪造用户身份。
* 其他客户端保持原来的 ``chat_message(messages, None)`` 路径，只是把单次
  ``reasoning_effort`` 一起带过去（老客户端不接受该 kwarg 时自动退回旧调用）。
* 无论走哪条路，mock 文案、空回复、模型回显（把输入原样当输出）和调用失败
  一律抛 :class:`LifeModelError`，绝不把占位/回声当成真实经历写进生活库。

本轮只落 API；后台调用方（autonomy 等）稍后接入。
"""

from __future__ import annotations

import inspect
import re
import uuid

from . import brain
from .llm import is_mock_text


class LifeModelError(RuntimeError):
    """生活后台这一轮没有拿到可用的真实文本。"""


_PURPOSE_RE = re.compile(r"[^a-z0-9_-]+")
_PURPOSE_MAX = 40


def _purpose_slug(purpose) -> str:
    """把调用方给的 purpose 收成 conv 里的稳定片段（不含分隔符/控制字符）。"""
    text = str(purpose or "").strip().lower()
    text = _PURPOSE_RE.sub("-", text).strip("-_")
    return (text or "life")[:_PURPOSE_MAX]


def _provider(llm) -> str:
    getter = getattr(llm, "provider", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "").strip().lower()
    except Exception:
        return ""


def _brain_ready(llm) -> bool:
    """只有 provider=dsh **且** Host 真的注入了大脑入口/token 时才改道。"""
    if _provider(llm) != "dsh":
        return False
    try:
        return bool(brain.enabled())
    except Exception:
        return False


def _direct_available(llm) -> bool:
    check = getattr(llm, "chat_available", None)
    if not callable(check):
        return True
    try:
        return bool(check())
    except Exception:
        return False


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("text", "input_text"):
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return ""


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _validate(content, messages: list[dict]) -> str:
    """拒绝 mock / 空 / 回显；返回可直接交给 JSON 解析的正文。"""
    text = _text_of(content).strip()
    if not text:
        raise LifeModelError("生活模型返回空内容，不能当成真实经历")
    if is_mock_text(text):
        raise LifeModelError("生活模型落到了 mock/占位输出，不能当成真实经历")
    probe = _norm(text)
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        user_text = _text_of(message.get("content")).strip()
        if user_text and _norm(user_text) == probe:
            raise LifeModelError("生活模型只是回显了输入，不能当成真实经历")
    return text


def _accepts_reasoning_effort(call) -> bool:
    try:
        parameters = inspect.signature(call).parameters
    except (TypeError, ValueError):
        return True
    if "reasoning_effort" in parameters:
        return True
    return any(item.kind is inspect.Parameter.VAR_KEYWORD
               for item in parameters.values())


async def _call_direct(llm, messages: list[dict], reasoning_effort: str) -> dict:
    """老路径：不带 brain_conv；支持思考强度的客户端才传该 kwarg。"""
    call = getattr(llm, "chat_message", None)
    if not callable(call):
        raise LifeModelError("当前客户端没有 chat_message，无法生成生活内容")
    if _accepts_reasoning_effort(call):
        return await call(messages, None, reasoning_effort=reasoning_effort)
    # 测试/自定义客户端可能只有 (messages, tools) 两个参数。
    # 生产里的 LLMClient 支持 reasoning_effort，不会走到这里。
    return await call(messages, None)


async def chat(bot, messages: list[dict], *, purpose: str = "life",
               reasoning_effort: str = "low") -> str:
    """用当前客户端生成一段真实的生活后台文本。

    ``messages`` 必须是调用方自己拼的正常 OpenAI 消息；本函数不追加系统提示、
    不伪造 user、也不传任何工具。返回值是去掉首尾空白后的正文；失败、mock、
    空串或回显输入都会抛 :class:`LifeModelError`。
    """
    llm = getattr(bot, "llm", None)
    if llm is None:
        raise LifeModelError("当前 bot 没有可用的模型客户端")
    items = [message for message in (messages or []) if isinstance(message, dict)]
    if not items:
        raise LifeModelError("生活模型调用缺少 messages")

    effort = str(reasoning_effort or "").strip().lower()

    if _brain_ready(llm):
        slug = _purpose_slug(purpose)
        conv = f"life:{slug}:{uuid.uuid4().hex}"
        meta = {
            "autonomous": True,
            "native_tools": False,
            "is_owner": False,
            "computer_access": False,
        }
        try:
            call = llm.chat_message
            kwargs = {"brain_conv": conv, "brain_meta": meta}
            if _accepts_reasoning_effort(call):
                kwargs["reasoning_effort"] = effort
            message = await call(items, None, **kwargs)
        except LifeModelError:
            raise
        except Exception as error:
            raise LifeModelError(
                f"生活模型（DSH 大脑）调用失败：{type(error).__name__}: {error}") from error
    else:
        # 配了 dsh 但桥没接通、也没有直连 key：直连只会拿到 mock。
        # 这里提前说清楚，不把占位文案带进生活库。
        if _provider(llm) == "dsh" and not _direct_available(llm):
            raise LifeModelError(
                "llm.provider=dsh，但 DSH 大脑桥未接通，也没有可回退的直连 API")
        try:
            message = await _call_direct(llm, items, effort)
        except LifeModelError:
            raise
        except Exception as error:
            raise LifeModelError(
                f"生活模型调用失败：{type(error).__name__}: {error}") from error

    content = message.get("content") if isinstance(message, dict) else message
    return _validate(content, items)
