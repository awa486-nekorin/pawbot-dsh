"""
OpenAI 兼容的大模型客户端（urllib 实现，不装 openai 包）。

能直接对接：DeepSeek / 通义 / 智谱 / Kimi / Ollama / LM Studio / OneAPI ……
只要是 /v1/chat/completions 这一套，改 base_url + model 就行。

没有填 api_key 时进入 mock 模式：会返回固定话术，方便离线调通整条链路。
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import urllib.error
import urllib.request

from .breaker import BREAKER
from .log import log

# DeepSeek 系模型有时不返回结构化的 tool_calls，而是把调用写成 DSML 文本
# （<|DSML|invoke name="...">…）。直接发出去就会变成一串乱码，所以解析成
# 正常工具调用；解析不了的部分也从正文里剥掉，绝不让标记漏到聊天里。
_DSML_BAR = r"[|｜]{1,2}\s*DSML\s*[|｜]{1,2}"
_DSML_CALLS = re.compile(rf"<\s*{_DSML_BAR}\s*(?:tool_)?calls\s*>", re.I)
_DSML_END_CALLS = re.compile(rf"<\s*/\s*{_DSML_BAR}\s*(?:tool_)?calls\s*>", re.I)
_DSML_INVOKE = re.compile(rf"<\s*{_DSML_BAR}\s*invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>", re.I)
_DSML_END_INVOKE = re.compile(rf"<\s*/\s*{_DSML_BAR}\s*invoke\s*>", re.I)
_DSML_PARAM = re.compile(
    rf"<\s*{_DSML_BAR}\s*parameter\s+name\s*=\s*[\"']([^\"']+)[\"']"
    rf"(?:\s+string\s*=\s*[\"'](true|false)[\"'])?\s*>(.*?)"
    rf"<\s*/\s*{_DSML_BAR}\s*parameter\s*>", re.I | re.S)
_DSML_TAG = re.compile(rf"<\s*/?\s*{_DSML_BAR}[^>]*>", re.I)


def _strip_dsml(text: str) -> str:
    """去掉残留的 DSML 标记，避免整段乱码被发进群。"""
    if not isinstance(text, str) or "DSML" not in text:
        return text
    start = _DSML_CALLS.search(text)
    if start:
        end = _DSML_END_CALLS.search(text, start.end())
        text = text[:start.start()] + (text[end.end():] if end else "")
    # 没有外层 calls 的裸 invoke：整段连参数正文一起去掉
    while True:
        invoke = _DSML_INVOKE.search(text)
        if not invoke:
            break
        end = _DSML_END_INVOKE.search(text, invoke.end())
        text = text[:invoke.start()] + (text[end.end():] if end else "")
    return _DSML_TAG.sub("", text).strip()


def _parse_dsml(content: str):
    """把 DSML 文本解析成 OpenAI 风格的 tool_calls；返回 (剩余文本, calls)。"""
    if not isinstance(content, str) or "DSML" not in content:
        return content, []
    calls = []
    for match in _DSML_INVOKE.finditer(content):
        name = match.group(1)
        end = _DSML_END_INVOKE.search(content, match.end())
        block = content[match.end():end.start() if end else len(content)]
        args = {}
        for param in _DSML_PARAM.finditer(block):
            key, is_string, raw = param.group(1), param.group(2), param.group(3).strip()
            if (is_string or "").lower() == "true":
                args[key] = raw
                continue
            try:
                args[key] = json.loads(raw)
            except (TypeError, ValueError):
                args[key] = raw
        calls.append({
            "id": f"dsml_{len(calls)}",
            "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return _strip_dsml(content), calls


def _finalize(message: dict) -> dict:
    """结构化的 tool_calls 优先；文本形式的 DSML 调用也认。"""
    if message.get("tool_calls"):
        message["content"] = _strip_dsml(message.get("content") or "")
        return message
    content = message.get("content") or ""
    clean, calls = _parse_dsml(content)
    if calls:
        message["content"] = clean
        message["tool_calls"] = calls
    elif clean != content:
        message["content"] = clean
    return message


class LLMError(RuntimeError):
    pass


# 这些状态码是「换一家再试」的信号（中转限流 / 上游失联）；
# 400、401、422 之类是自己请求写错了，换个模型也一样，不该重发。
_FALLBACK_CODES = (429, 500, 502, 503, 504)


class _Transient(Exception):
    """内部用：可切换备援的失败，带一句人话原因。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LLMClient:
    def __init__(self, config):
        self.config = config

    def _cfg(self, key, default=None):
        return self.config.get(f"llm.{key}", default)

    def _payload(self, messages: list[dict], tools: list[dict] | None = None,
                 budget: int | None = None, model: str | None = None) -> dict:
        payload = {
            "model": model or self._cfg("model", "deepseek-chat"),
            "messages": messages,
            "temperature": float(self._cfg("temperature", 0.8)),
            "max_tokens": int(budget or self._cfg("max_tokens", 900)),
            "stream": False,
        }
        # 思考型模型（deepseek-flash 等）的思考强度：low / medium / high，
        # 留空就交给服务端默认，不认识的写法一律忽略。
        effort = str(self._cfg("reasoning_effort", "medium") or "").strip().lower()
        if effort in ("low", "medium", "high"):
            payload["reasoning_effort"] = effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _post(self, url: str, payload: dict, key: str, timeout: int) -> dict:
        """发一次请求。超时和 429/5xx 归为 _Transient（可以换备援），其余是 LLMError。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "PawBot/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            msg = f"HTTP {e.code}: {detail}"
            if e.code in _FALLBACK_CODES:
                raise _Transient(msg) from e
            raise LLMError(msg) from e
        except (socket.timeout, TimeoutError) as e:
            raise _Transient(f"超时 {timeout}s") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise _Transient(f"超时 {timeout}s") from e
            raise LLMError(f"网络错误（境外 API 记得开代理）：{e.reason}") from e

    def _request(self, messages: list[dict], tools: list[dict] | None = None) -> str:
        msg = self._request_message(messages, tools)
        content = msg.get("content") or ""
        if msg.get("tool_calls"):
            # 不该发生：调用方没要工具却返回了工具调用
            return content
        return content

    def _request_message(self, messages: list[dict],
                         tools: list[dict] | None = None) -> dict:
        """完整返回 assistant message，含可能的 tool_calls。

        主模型先用 llm.first_timeout 等着；超时或 429/5xx 时如果配了
        llm.fallback_model，就立刻用同一份 payload 换模型重发。
        """
        base = str(self._cfg("base_url", "")).rstrip("/")
        key = str(self._cfg("api_key", "")).strip()
        timeout = int(self._cfg("timeout", 60))

        if not base:
            raise LLMError("没有配置 llm.base_url")

        if not key:
            # 离线 mock：让链路能跑通，正式用请填 api_key
            last = messages[-1].get("content") if messages else ""
            if isinstance(last, list):
                last = " ".join(str(x.get("text", "")) for x in last)
            return {"role": "assistant",
                    "content": f"（mock 模式，未配置 api_key）我听到你说：{str(last)[:60]}"}

        url = base + "/chat/completions"
        budget = int(self._cfg("max_tokens", 900))

        primary = str(self._cfg("model", "deepseek-chat"))
        fallback = str(self._cfg("fallback_model", "") or "").strip()
        if fallback == primary:
            fallback = ""
        # first_timeout=0 表示不分主备，沿用 llm.timeout
        first_timeout = int(self._cfg("first_timeout", 0) or 0)
        if first_timeout <= 0:
            first_timeout = timeout

        # 连续失败达 threshold 次就熔断 cooldown 秒；期间不再空等主模型
        BREAKER.configure(int(self._cfg("breaker_threshold", 2) or 2),
                          float(self._cfg("breaker_cooldown", 300) or 300))

        model, model_timeout = primary, first_timeout
        # 主模型还在冷却期：直接走备援，省掉每轮 first_timeout 的空等
        if fallback and BREAKER.is_open(primary):
            log.warn(f"聊天模型 {primary} 熔断中，直接用备援 {fallback}")
            model, model_timeout = fallback, timeout

        # deepseek-flash 这类思考型模型：reasoning_tokens 会先吃掉预算，
        # 预算不够时 finish_reason=length 而 content 是空串。
        # 遇到这种情况就翻倍重试，最多两次——比直接把空回复发到群里强。
        for attempt in range(3):
            payload = self._payload(messages, tools, budget, model=model)
            try:
                data = self._post(url, payload, key, model_timeout)
            except _Transient as e:
                BREAKER.record_failure(model)
                if not fallback or model == fallback:
                    raise LLMError(e.reason) from e
                log.warn(f"聊天模型 {primary} 失败（{e.reason}），改用 {fallback}")
                model, model_timeout = fallback, timeout
                payload = self._payload(messages, tools, budget, model=model)
                try:
                    data = self._post(url, payload, key, model_timeout)
                except _Transient as e2:
                    BREAKER.record_failure(model)
                    raise LLMError(f"备援模型 {fallback} 也失败（{e2.reason}）") from e2

            try:
                message = data["choices"][0]["message"]
                finish = data["choices"][0].get("finish_reason")
            except (KeyError, IndexError):
                raise LLMError(f"返回格式意外：{json.dumps(data, ensure_ascii=False)[:300]}")

            BREAKER.record_success(model)
            message.setdefault("role", "assistant")
            message = _finalize(message)
            has_text = bool((message.get("content") or "").strip())
            has_tools = bool(message.get("tool_calls"))
            if has_text or has_tools or finish != "length":
                return message
            budget *= 2                      # 被思考吃光，加预算再来
            if budget > 4000:
                break
        return _finalize(message)


    async def chat(self, messages: list[dict]) -> str:
        """在线程池里跑阻塞请求，别堵住事件循环。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._request, messages)

    async def chat_message(self, messages: list[dict],
                           tools: list[dict] | None = None) -> dict:
        """要工具的那种调用：返回带 tool_calls 的完整 message。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._request_message, messages, tools)

    def build_messages(self, user_text: str, history: list[dict],
                       system_prompt: str | None = None,
                       extra_system: str | None = None) -> list[dict]:
        sys_prompt = system_prompt or self._cfg("system_prompt", "你是一个 QQ 群友。")
        if extra_system:
            sys_prompt = sys_prompt + "\n\n" + extra_system
        return [{"role": "system", "content": sys_prompt}, *history,
                {"role": "user", "content": user_text}]

    async def test(self) -> str:
        return await self.chat([{"role": "user", "content": "用一句话确认你在工作。"}])
