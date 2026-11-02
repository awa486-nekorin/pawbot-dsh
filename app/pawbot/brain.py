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

看图：`turn()` 会把 `payload['context']['images']` 里的 data URL / http 图转成 Host
附件（`images: [{media_type, data(base64)}]`）。Host 侧没有附件通道、模型不吃图、
或超过体积预算时，一律降级成旧的「本轮看不到」（system 文案也是旧的），不报错。

安全边界：
  - token 只存在于 Host 子进程环境变量和本进程内存里，不落盘、不打日志；
  - 不暴露给模型：这个模块只被 llm.py / agent.py / webui.py 内部调用；
  - 工具执行只接受「本轮登记过、且本轮 schemas 里出现过的工具名」，不接受任意名字；
  - 电脑能力默认关：只有代码生成的 `context.computer_access is True`（再加主人身份
    与 Host 配置）才可能打开；外部评论/字幕/网页文本不参与授权判定。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .log import log

URL_ENV = "PAWBOT_DSH_BRAIN_URL"
TOKEN_ENV = "PAWBOT_DSH_BRAIN_TOKEN"

# 单轮请求上限（Host 侧另有自己的超时；这里只做兜底）——留点余量给工具调用。
MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 90
# 与 Host 的 bodyBytes 上限（2MB）对齐，本地先拦，避免每轮 413 且没有诊断。
MAX_BODY = 2 * 1024 * 1024 - 64 * 1024

# 看图：把 user 消息里的 image_url 转成 Host 附件通道要的格式。
# Host 的真实 API 是 `attachments.admitPromptContent([{type:'image', mediaType, data(base64)}])`
# → `{type:'image', attachment}`（base64 必须是规范形式）。这里先按 body 预算挑图，
# 超了/坏了就少给几张并在 system 里说明，绝不因此让这一轮失败。
IMAGE_MAX_COUNT = 4
IMAGE_MAX_B64 = 900 * 1024          # 单张 base64 字符数上限
IMAGE_MAX_TOTAL_B64 = 1350 * 1024   # 一轮 base64 合计（body 2MB 还要留给 system/tools）
IMAGE_FETCH_TIMEOUT = 10
IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
# llm.py 在 dsh 分支补的那句「看不了图」：真把图送到时换成看图口径，送不到就原样保留。
_IMAGE_NOTE = re.compile(r"（对方这条消息里带了 \d+ 张图[^）]*）")


def payload_bytes(payload: dict) -> int:
    """Payload 序列化后的真实字节数（与 Host 的 body 限额同一口径）。

    只用于本地预算判断/测试测量；不会截断内容——超限时 `turn()` 会显式抛
    `PAYLOAD_TOO_LARGE`，由调用方决定降级或报错，绝不静默省略记忆块。
    """
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


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


# ── 看图：image_url → Host 附件 ───────────────────────────────────────────────

def _with_images(payload: dict) -> tuple[dict, dict]:
    """把 `payload['context']['images']` 里的图转成 Host 附件部件。

    `context.images` 是 agent.py 在这一轮真正塞进 user 消息里的图（data URL 为主，
    http 兜底）。返回 `(新的 payload, 统计)`；统计字段：
    `requested / attached / dropped / bytes / reason`。**从不抛错**：转不了就少给，
    一张都不行就和以前一样只发文字（system 里那句「看不到」原样保留）。
    """
    info = {"requested": 0, "attached": 0, "dropped": 0, "bytes": 0, "reason": ""}
    context = payload.get("context")
    urls = context.get("images") if isinstance(context, dict) else None
    if not isinstance(urls, (list, tuple)) or not urls:
        return payload, info

    wanted = [str(item) for item in urls if isinstance(item, str) and str(item).strip()]
    info["requested"] = len(wanted)
    # 不管转不转得成，`images` 都不再原样发给 Host（Host 只认 `images` 部件）。
    out = dict(payload)
    out["context"] = {key: value for key, value in context.items() if key != "images"}
    if not wanted:
        return out, info

    try:
        base = len(json.dumps({**out, "images": []}, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        base = len(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))
    budget = MAX_BODY - base - 96 * 1024
    parts: list[dict] = []
    for raw in wanted[:IMAGE_MAX_COUNT]:
        if budget <= 0:
            info["reason"] = "no-room-in-payload"
            break
        item = _image_part(raw)
        if item is None:
            # 一张坏就停：**按顺序取前缀**，附图顺序要和 user 文本里的编号对得上。
            info["reason"] = "unreadable"
            break
        media_type, data = item
        if len(data) > IMAGE_MAX_B64 or len(data) > budget:
            info["reason"] = "too-large"
            break
        parts.append({"media_type": media_type, "data": data})
        budget -= len(data) + 96
        if sum(len(part["data"]) for part in parts) >= IMAGE_MAX_TOTAL_B64:
            break
    info["dropped"] = len(wanted) - len(parts)
    if not parts:
        return out, info

    out["images"] = parts
    info["attached"] = len(parts)
    info["bytes"] = sum(len(part["data"]) for part in parts)
    if len(parts) == len(wanted):
        note = (f"（对方这条消息里带了 {len(wanted)} 张图：能看到附上的原图就以图为准，"
                "别主动描述画面；看不到就按文字回应，别假装看见了。）")
    else:
        note = (f"（对方这条消息里带了 {len(wanted)} 张图，前 {len(parts)} 张已附上，"
                "其余这轮没发：只看附上的那些，别假装看见了没发的。）")
    out["system"] = _swap_image_note(str(payload.get("system") or ""), note)
    return out, info


def _image_part(url: str) -> tuple[str, str] | None:
    """一个 image_url → `(media_type, 规范 base64)`；不认识的来源返回 None。"""
    text = str(url or "").strip()
    if not text:
        return None
    if text.startswith("data:"):
        return _data_url_part(text)
    if text.startswith(("http://", "https://")):
        raw = _fetch_image_bytes(text)
        media_type = _sniff_image(raw) if raw else ""
        if not raw or not media_type:
            return None
        return media_type, base64.b64encode(raw).decode("ascii")
    # 本地路径 / file:// 一律不读：群消息里的字符串不该变成任意文件读取。
    return None


def _data_url_part(text: str) -> tuple[str, str] | None:
    head, sep, data = text[5:].partition(",")
    if not sep or "base64" not in head.lower():
        return None
    media_type = head.split(";")[0].strip().lower()
    if media_type not in IMAGE_MEDIA_TYPES:
        return None
    try:
        raw = base64.b64decode("".join(data.split()), validate=True)
    except (ValueError, TypeError):
        return None
    if not raw:
        return None
    # 重新编码：Host 按「规范 base64」逐字校验，带换行/异形 padding 的会被拒。
    return media_type, base64.b64encode(raw).decode("ascii")


def _fetch_image_bytes(url: str) -> bytes | None:
    """http(s) 兜底取图（QQ 图的正常路径已经是 data URL）。

    群消息里的地址不该变成内网探测器：挡掉 loopback/私有地址字面量，
    限时 10s、限字节；失败只降级，不报错。
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").strip().lower()
    if not host or host == "localhost":
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback
                                or address.is_link_local or address.is_reserved
                                or address.is_unspecified):
        return None
    limit = int(IMAGE_MAX_B64 * 3 / 4)
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "PawBot/1.0 (dsh-brain image)"})
        with urllib.request.urlopen(request, timeout=IMAGE_FETCH_TIMEOUT) as response:
            raw = response.read(limit + 1)
    except Exception as error:      # 网络/证书/超时都只降级
        log.debug(f"大脑看图：取图失败 {url[:60]}（{type(error).__name__}）")
        return None
    if not raw or len(raw) > limit:
        return None
    return raw


def _sniff_image(raw: bytes) -> str:
    """按 magic bytes 认图片类型（Host 只收 png/jpeg/webp/gif）。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _missing_images_note(count: int) -> str:
    """「本轮看不到图」的唯一文案来源：llm.py 折 payload 与这里降级都用它。"""
    return (f"（对方这条消息里带了 {count} 张图；这一轮的大脑还看不了图，"
            "只按文字回应，不要假装看见了。）")


def _policy_text() -> str:
    """代码里的不可变策略正文；拿不到就返回空串。"""
    try:
        from . import policy

        return str(policy.system_policy_text() or "")
    except Exception:
        return ""


def _swap_image_note_body(system: str, note: str) -> str:
    if not system:
        return note
    if _IMAGE_NOTE.search(system):
        return _IMAGE_NOTE.sub(lambda _match: note, system, count=1)
    return system.rstrip() + "\n\n" + note


def _swap_image_note(system: str, note: str) -> str:
    """把看图说明换/补进 system，但**不破坏「策略永远最后」**。

    换图说明可能出现在策略前面；如果直接把 note 追加到字符串末尾，策略就会被
    挤到中间。这里先摘掉已有策略副本，再统一由 policy.ensure_policy_last 补回最后。
    """
    text = str(system or "")
    inert = _policy_text()
    body = text.replace(inert, "") if inert else text
    swapped = _swap_image_note_body(body.rstrip(), note)
    if not inert:
        return swapped
    try:
        from . import policy

        return policy.ensure_policy_last(swapped)
    except Exception:
        return (swapped.rstrip() + "\n\n" + inert).lstrip()


def _restore_missing_note(system: str, count: int) -> str:
    """图最终没送到（例如去图后仍超限）时，把说明换回「本轮看不到」。"""
    return _swap_image_note(system, _missing_images_note(count))


# 公开别名：其他模块（llm / 测试）按语义名取，不必依赖私有下划线。
missing_images_note = _missing_images_note


# ── 请求 ─────────────────────────────────────────────────────────────────────

def _normalized_context(payload: dict) -> dict:
    """在传输边界再收一次：`computer_access` 默认 False，且只认严格布尔 True。

    llm.py 已经会规整一次；这里再兜一次，保证任何直接调用 brain.turn 的路径
    都不会把字符串/工具参数当成电脑能力授权。其他 context 字段（images 等）
    原样保留。
    """
    out = dict(payload) if isinstance(payload, dict) else {}
    ctx = out.get("context")
    ctx = dict(ctx) if isinstance(ctx, dict) else {}
    ctx["computer_access"] = ctx.get("computer_access") is True
    if "is_owner" in ctx:
        ctx["is_owner"] = ctx.get("is_owner") is True
    out["context"] = ctx
    return out


def turn(payload: dict, *, timeout: float | int | None = None) -> dict:
    """发一轮请求给 DSH 大脑；失败抛 BrainError（带 code/status）。"""
    url, token = env_url(), env_token()
    if not url or not token:
        raise BrainError("DSH 大脑未接通（Host 没注入 brain URL/token）",
                         code="BRAIN_DISABLED")

    seconds = float(timeout or DEFAULT_TIMEOUT)
    seconds = max(1.0, min(MAX_TIMEOUT, seconds))
    outgoing, images = _with_images(_normalized_context(payload))
    body = json.dumps(outgoing, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BODY and outgoing.get("images"):
        # 图和很长的 system 同时挤进来：去掉图降级，别让整轮 413。
        log.warn(f"这一轮的图把请求顶到 {len(body)} B（上限 {MAX_BODY} B），本轮降级为不带图")
        outgoing = dict(outgoing)
        outgoing.pop("images", None)
        outgoing["system"] = _restore_missing_note(
            str(outgoing.get("system") or ""), int(images.get("requested") or 0))
        images = {**images, "attached": 0, "bytes": 0, "reason": "payload-too-large"}
        body = json.dumps(outgoing, ensure_ascii=False).encode("utf-8")
    if images.get("attached"):
        log.debug(f"大脑看图：附上 {images['attached']}/{images['requested']} 张"
                  f"（{images['bytes']} B base64）")
    elif images.get("requested"):
        log.warn(f"大脑这一轮的 {images['requested']} 张图没能附图"
                 f"（{images.get('reason') or 'unreadable'}），降级为文字")
    if len(body) > MAX_BODY:
        # 明确失败：不截断活跃期/记忆块来“凑进限额”。调用方可以决定缩小上下文后重试。
        raise BrainError(
            f"这一轮的 system/工具 schema 太大（{len(body)} B > {MAX_BODY} B），Host 会 413；"
            "已保留完整内容、没有静默截断；请减少记忆块/活跃期或工具数量",
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
        # 普通自检不给电脑能力；宿主内部的 `pawbot_brain selftest` 另有显式路径。
        "context": {"computer_access": False},
        "options": {},
    }
    return turn(payload, timeout=timeout or 60)


def announce_failure(error: Exception) -> None:
    """把失败写成一行日志（token 已在 Host 侧遮罩；这里也不打 URL）。"""
    if isinstance(error, BrainError):
        log.warn(f"DSH 大脑失败 [{error.code}/{error.status or '-'}]：{error}")
    else:
        log.warn(f"DSH 大脑失败：{type(error).__name__}: {error}")
