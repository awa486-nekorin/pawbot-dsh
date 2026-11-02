"""
感知层：把「它收到的消息」补全成「它真正理解的消息」。

原来消息里的图片会被压成 `[图片]`，引用只有一个 id，
所以它对群里发生的很多事情其实是瞎的。这里补两件事：

  1. 引用内容 —— 用 get_msg 把被引用那条拉回来，转成
     「（回复 阿明：他昨天说要去上海）」这样的前缀
  2. 图片理解 —— 交给多模态模型描述，转成 `[图片：一只虎斑猫趴在窗台]`

两件事都会失败（NapCat 没实现 get_msg、图下载不到、模型抽风），
失败就退回原样，绝不让整条链路断掉。
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import time

from . import identity
from . import message as msg
from . import paths as layout
from . import perception
from . import speech
from . import video
from . import vision
from .log import log

#: 惰性挂在 bot 上的 FactStore（每个 bot 一个，见 ``fact_store_for_bot``）。
_FACT_STORE_ATTR = "perception_fact_store"


def fact_store_for_bot(bot):
    """每个 bot 惰性拿一个落盘的 FactStore（``state/perception/vision``）。

    只在**已经启用 state/ 布局**的 bot 上启用磁盘缓存：``paths.for_bot`` 给出
    DataPaths 且 ``state/`` 存在时，目录取 ``state_dir("perception")``；
    老测试用的裸 bot / 没迁移的数据根返回 ``None``，由 ``vision`` 使用它自己的
    纯内存默认缓存（行为与旧测试一致，也不会在仓库里造目录）。

    传进来的 ``bot.fact_store``（测试注入）优先；找不到时不在 bot 上塞任何
    与图片身份有关的数据，缓存键/值仍完全由 ``perception`` 管。
    """
    if bot is None:
        return None
    injected = getattr(bot, "fact_store", None)
    if injected is not None and callable(getattr(injected, "run", None)):
        return injected
    cached = getattr(bot, _FACT_STORE_ATTR, None)
    if cached is not None and callable(getattr(cached, "run", None)):
        return cached
    if not isinstance(getattr(bot, "paths", None), layout.DataPaths):
        return None
    try:
        data_paths = layout.for_bot(bot)
        if not os.path.isdir(data_paths.state_root):
            # state/ 还没启用：不猜旧位置，也不在这里代做迁移。
            return None
        store = perception.FactStore(
            directory=data_paths.state_dir("perception"), namespace="vision")
    except Exception as e:      # 落盘不可用只降级为纯内存，不影响看图
        log.warn(f"感知事实缓存初始化失败（本次用纯内存）：{type(e).__name__} {e}")
        return None
    try:
        setattr(bot, _FACT_STORE_ATTR, store)
    except Exception:
        pass
    return store


def _message_id_variants(message_id) -> list:
    """先按原样请求；原样不是数字时再退一步试 int（NapCat 各版本要求不一）。"""
    variants: list = [message_id]
    try:
        as_int = int(str(message_id).strip())
    except (TypeError, ValueError):
        return variants
    if as_int != message_id:
        variants.append(as_int)
    return variants


def _quoted_payload(data) -> tuple[object, dict]:
    """从 get_msg 回包里挖出消息段和发送者。

    conn.call 通常已经把 {status,retcode,data} 剥了一层，但测试/其他实现
    可能给整包；message 可能是数组、单段、CQ 字符串，也可能只给
    raw_message。挨个试，拿不到就返回 None。
    """
    if isinstance(data, list):
        return data, {}
    if not isinstance(data, dict):
        return None, {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    sender: dict = {}
    for node in (data, inner):
        if isinstance(node.get("sender"), dict):
            sender = node["sender"]
            break
    segs = None
    for node in (data, inner):
        for key in ("message", "message_chain", "content"):
            value = node.get(key)
            if value not in (None, "", [], {}):
                segs = value
                break
        if segs is not None:
            break
    if segs is None:
        raw = data.get("raw_message") or inner.get("raw_message")
        if isinstance(raw, str) and raw.strip():
            segs = raw
    return segs, sender


async def _fetch_quoted(conn, message_id) -> tuple[list[dict] | None, dict]:
    """取被引用的那条消息：返回 (消息段, 发送者)。"""
    if conn is None or not message_id:
        return None, {}
    data = None
    last_error = ""
    for candidate in _message_id_variants(message_id):
        try:
            data = await conn.call("get_msg", message_id=candidate, timeout=8)
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            log.debug(f"get_msg 失败 {message_id!r}（请求参数 {candidate!r}）：{last_error}")
            # 请求已经到了协议端还失败（超时/断连/retcode 非 0）就别再换类型重试，白等一轮
            if getattr(e, "delivered", False):
                break
    if data is None:
        log.info(f"引用消息取不到 {message_id!r}：{last_error or 'get_msg 无回包'}")
        return None, {}
    segs, sender = _quoted_payload(data)
    if segs is None:
        keys = list(data)[:6] if isinstance(data, dict) else type(data).__name__
        log.info(f"引用消息没有正文 {message_id!r}：回包字段 {keys}")
        return None, sender
    normalized = _TransportSegments(msg.normalize(segs))
    # 数组格式＝协议端按消息元素生成；CQ 字符串是重新解析的，不可信。
    normalized.transport_trusted = isinstance(segs, list)
    return normalized, sender


class _TransportSegments(list):
    """协议端回包里的段列表；``transport_trusted`` 记「是不是数组格式」。

    只有数组格式才认为本地媒体路径由协议端生成（可信）；CQ 字符串会走
    ``message.cq_to_array`` 重新解析，用户文本里的 ``[CQ:image,file=...]``
    也可能变成图片段，所以本地路径必须 fail-closed（除非配置了允许目录）。
    """

    transport_trusted = False


# ---------------------------------------------------------------- 合并转发
def _forward_nodes(data) -> list:
    """从 get_forward_msg 的回包里找出节点数组。

    NapCat / 不同实现可能把节点放在 data.messages、data.message，
    也可能直接放在顶层 messages。挨个试，拿不到就当空。
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    for node in (data, inner):
        if isinstance(node, dict):
            for key in ("messages", "message", "content"):
                value = node.get(key)
                if isinstance(value, list):
                    return value
    return []


async def _node_body(conn, node: dict, depth: int) -> tuple[str, list[dict]]:
    """一个转发节点 → (正文, 里面的 image 段)。

    节点里再套的转发（forward 段带 id）只再展开一层，避免无休止地翻下去。
    """
    segments = node.get("message", node.get("content"))
    if isinstance(segments, str):
        return segments.strip(), []
    if isinstance(segments, dict):
        segments = [segments]
    if not isinstance(segments, list):
        return "", []
    parts: list[str] = []
    images: list[dict] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        if kind == "image":
            images.append(seg)
            parts.append("[图片]")
            continue
        if kind in ("forward", "node"):
            fid = str((seg.get("data") or {}).get("id") or "")
            if depth < 1 and fid:
                sub_text, sub_imgs = await _fetch_forward(conn, fid, depth + 1)
                images.extend(sub_imgs)
                parts.append("\n" + (sub_text or "（内层转发取不到内容）"))
            else:
                parts.append("（又套了一层转发）" if fid else "（内层转发没有 id）")
            continue
        parts.append(msg.to_plain([seg]))
    return "".join(parts).strip(), images


async def _fetch_forward(conn, forward_id, _depth: int = 0) -> tuple[str, list[dict]]:
    """把合并转发展开成「昵称：正文」，最多 30 条、总长 3000 字。

    同时收集所有节点里的 image 段（最多 9 张），交给看图链路统一转述；
    图不再像以前那样被默默丢掉。失败会写出原因，方便对着日志查。
    """
    if conn is None or not forward_id:
        return "", []
    try:
        data = await conn.call("get_forward_msg", id=str(forward_id), timeout=15)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        log.info(f"get_forward_msg 失败 {forward_id}：{reason}")
        return "", []
    lines: list[str] = []
    images: list[dict] = []
    for node in _forward_nodes(data)[:30]:
        if not isinstance(node, dict):
            continue
        # 有的实现把每个节点包成 {"type":"node","data":{...}}
        if node.get("type") == "node" and isinstance(node.get("data"), dict):
            node = node["data"]
        sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
        name = str(sender.get("card") or sender.get("nickname")
                   or sender.get("user_id") or "有人")
        # 归属只认真实 sender.user_id；昵称只用于展示，不能拿来反查是谁。
        sender_uid = identity.uid_of(sender.get("user_id"))
        body, imgs = await _node_body(conn, node, _depth)
        if imgs and len(images) < 9:
            for img in imgs[:9 - len(images)]:
                if not isinstance(img, dict):
                    continue
                copy = dict(img)
                # 记下这张转发图是谁发的；内层转发已经带上的就不覆盖
                copy.setdefault("_from", name)
                copy.setdefault("_uid", sender_uid)
                images.append(copy)
        if body:
            lines.append(f"{name}：{body}")
    if not lines and not images:
        log.info(f"转发消息没解析出正文或图 {forward_id}："
                 f"节点数 {len(_forward_nodes(data))}")
    return "\n".join(lines)[:3000].strip(), images


# ---------------------------------------------------------------- JSON 卡片
_CARD_META_KEYS = ("detail_1", "news", "detail", "music", "video", "mini_app")


def _card_from_meta(meta) -> str:
    """从 meta 里挑「标题 ｜ 描述 ｜ 链接」。B站和小程序字段名不一样。"""
    if not isinstance(meta, dict):
        return ""
    items: list[dict] = []
    for key in _CARD_META_KEYS:
        value = meta.get(key)
        if isinstance(value, dict):
            items.append(value)
    for value in meta.values():
        if isinstance(value, dict) and value not in items:
            items.append(value)
    for item in items:
        title = str(item.get("title") or "").strip()
        desc = str(item.get("desc") or "").strip()
        url = str(item.get("qqdocurl") or item.get("jumpUrl") or item.get("url")
                  or item.get("musicUrl") or "").strip()
        if not (title or desc or url):
            continue
        parts = [p for p in (title, desc if desc != title else "", url) if p]
        return " ｜ ".join(parts)
    return ""


def card_text(segments: list[dict]) -> str:
    """把 json 段（B站小卡片 / QQ 小程序）解析成一句可读的卡片内容。"""
    for seg in segments:
        if seg.get("type") != "json":
            continue
        data = seg.get("data")
        if not isinstance(data, dict):
            continue
        raw = data.get("data")
        obj = raw
        if isinstance(raw, str):
            try:
                obj = json.loads(raw)
            except Exception:
                obj = {}
        if isinstance(obj, dict):
            text = _card_from_meta(obj.get("meta"))
            if not text:
                text = str(obj.get("prompt") or "").strip()
            if text:
                return text[:600]
        # data.data 坏掉时，段上自带的 prompt 还能兜一层
        prompt = str(data.get("prompt") or "").strip()
        if prompt:
            return prompt[:600]
    return ""


# ---------------------------------------------------------------- 被谈论
_ABOUT_BOT_RE = re.compile(
    r"这(?:个)?(?:机器人|bot|AI|人工智能)"
    r"|她(?:在|又|怎么|是不是)",
    re.IGNORECASE,
)


def talks_about_bot(text: str, nicknames) -> bool:
    """没叫它，但话头明显在说它。

    只认两类信号：昵称/唤醒词出现在句子里；或「这机器人 / 这个 AI /
    她(在|又|怎么|是不是…)」这种明确指代。刻意不把「机器人」三个字本身
    当信号——「我自己是个机器人」「我在写机器人作业」都不算在说它。

    边界：「她在家」这种也可能在说真人，单看一句话无法区分；
    这里选择宁可按「可能是在说它」处理（只影响提示词，不会自动唤醒）。
    """
    body = str(text or "").strip()
    if not body:
        return False
    for name in nicknames or ():
        name = str(name or "").strip()
        if name and name in body:
            return True
    return bool(_ABOUT_BOT_RE.search(body))


def _nicknames(bot) -> list[str]:
    cfg = getattr(bot, "config", None)
    if cfg is None:
        return []
    try:
        names = [cfg.get("bot.nickname", "")]
        names.extend(list(cfg.get("bot.wake_words", []) or []))
    except Exception:
        return []
    return [str(n) for n in names if str(n or "").strip()]


def _configured_local_roots(config) -> tuple[str, ...]:
    """配置/代码批准的本地图片目录（``vision.local_roots``）。

    默认空：图片段里的本地路径一律拒绝。只填「协议端/运维明确批准」的媒体
    缓存目录；不要填数据根、人设、state 或凭证所在目录 —— 那等于把任意文件
    读取又放回来了。
    """
    try:
        raw = config.get("vision.local_roots", ())
    except Exception:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    roots = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            roots.append(os.path.expanduser(text))
    return tuple(roots)


def _event_transport_trusts_local(ev) -> bool:
    """这条事件的本地媒体路径是否由 OneBot 协议端生成（可信）。

    只看 ``ev.raw``：``message`` 是**数组**（OneBot 数组格式，NapCat 由消息元素
    生成）才可信；``message`` 是 CQ 字符串时 ``message.cq_to_array`` 会把用户
    文本里的 ``[CQ:image,file=...]`` 解析成图片段 —— 那是不可信输入。拿不到原始
    事件（``ev.raw`` 缺失/形状不对）同样按不可信处理（fail-closed）。

    注意：这只决定「本地路径能不能读」，**不**放松远程 URL 的校验 —— 即使事件
    来自协议端，段里伪造的 ``url`` 仍然只允许公网 http(s)。
    """
    raw = getattr(ev, "raw", None)
    if not isinstance(raw, dict):
        return False
    if raw.get("post_type") not in ("message", "message_sent"):
        return False
    return isinstance(raw.get("message"), list)


def _forward_local_trusted(forward_segments: list[dict]) -> bool:
    """合并转发的图只能来自节点里的数组消息（``_node_body`` 只收这种）。

    ``_node_body`` 遇到字符串正文直接返回、不收集图片段，所以拿到图就说明
    协议端给的是数组消息。
    """
    return bool(forward_segments)


async def _record_bytes(conn, seg: dict, config) -> bytes | None:
    """拿一段语音的音频字节。优先让 NapCat 转成 mp3，其次直接下载。"""
    data = seg.get("data") or {}
    name = str(data.get("file") or "")
    out = None
    if conn is not None and name:
        try:
            out = await conn.call("get_record", file=name, out_format="mp3", timeout=20)
        except Exception as e:
            log.debug(f"get_record 失败：{type(e).__name__}")
    # 回执可能是 {"file": "C:\\...\\x.mp3"} 或 {"file": "base64://..."} 或直接字符串
    path = ""
    if isinstance(out, dict):
        inner = out.get("data") if isinstance(out.get("data"), dict) else {}
        path = str(out.get("file") or inner.get("file") or "")
        if not path and isinstance(out.get("base64"), str):
            path = "base64://" + out["base64"]
    elif isinstance(out, str):
        path = out
    if path.startswith("base64://"):
        import base64
        try:
            return base64.b64decode(path[9:])
        except Exception:
            return None
    if path:
        try:
            if path.startswith(("http://", "https://")):
                import urllib.request
                req = urllib.request.Request(path, headers={"User-Agent": vision.UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read(8 * 1024 * 1024)
            import os
            local = path[7:] if path.startswith("file://") else path
            if os.path.isfile(local):
                with open(local, "rb") as f:
                    return f.read(8 * 1024 * 1024)
        except Exception as e:
            log.debug(f"取语音失败：{type(e).__name__}")
    url = str(data.get("url") or "")
    if url.startswith(("http://", "https://")):
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": vision.UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read(8 * 1024 * 1024)
        except Exception as e:
            log.debug(f"下载语音失败：{type(e).__name__}")
    return None


async def _transcribe_record(bot, conn, seg: dict) -> str:
    if not bot.config.get("speech.enabled", True):
        return ""
    loop = asyncio.get_running_loop()
    raw = await _record_bytes(conn, seg, bot.config)
    if not raw:
        return ""
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: speech.transcribe(raw, bot.config)),
            timeout=float(bot.config.get("speech.timeout", 60) or 60) + 5)
    except Exception as e:
        log.warn(f"语音转写超时/失败：{type(e).__name__}")
        return ""


_RECENT_MAX = 6                     # 每个会话留最近几张图，供「上面那种图」回看


def _is_sticker(seg: dict) -> bool:
    """判断这个 image 段是不是表情包 / 梗图。

    NapCat 会在 data 里带 sub_type=1（表情包）或者 summary="[动画表情]"；
    有的版本还会带 emoji_id / emoji_package_id。任一条命中就算表情包。
    """
    if not isinstance(seg, dict):
        return False
    data = seg.get("data") or {}
    sub = str(data.get("sub_type") or data.get("subType") or "").strip()
    if sub in ("1", "sticker", "emoji"):
        return True
    summary = str(data.get("summary") or "")
    if "表情" in summary:
        return True
    if str(data.get("emoji_id") or "").strip():
        return True
    return False


class _ImageSegments(list):
    """图片段列表 + 本地来源许可。

    ``vision._load_bytes`` 的 ``local_roots`` / ``trusted_local`` 跟着列表走，
    这样 ``_image_bytes_tagged`` 的签名保持「只吃一个段列表」不变（老测试/桩
    不用改），来源许可也不会因为漏传参数而静默放开。
    """

    local_roots: tuple = ()
    trusted_local = False


def _with_source_policy(segments, local_roots, trusted_local) -> _ImageSegments:
    marked = _ImageSegments(segments or [])
    marked.local_roots = tuple(local_roots or ())
    marked.trusted_local = bool(trusted_local)
    return marked


def _image_bytes(segments: list[dict]) -> list[bytes]:
    """读出图片段的原始字节（下载 / 本地路径 / base64 都走 vision 那套）。"""
    return [raw for raw, _sticker in _image_bytes_tagged(segments)]


def _image_bytes_tagged(segments: list[dict]) -> list[tuple[bytes, bool]]:
    """同上，但每张图带回「是不是表情包」，供看图时选不同的提示词。

    本地来源许可从 ``_ImageSegments`` 上读；普通 list 没有就按「不可信、无允许
    目录」处理（本地路径 fail-closed）。
    """
    local_roots = getattr(segments, "local_roots", ())
    trusted_local = bool(getattr(segments, "trusted_local", False))
    out: list[tuple[bytes, bool]] = []
    for seg in segments:
        if not isinstance(seg, dict) or seg.get("type") != "image":
            continue
        for src in vision._segment_sources(seg):
            raw = vision._load_bytes(src, local_roots=local_roots,
                                     trusted_local=trusted_local)
            if raw:
                out.append((raw, _is_sticker(seg)))
                break
    return out


async def _read_images(segments: list[dict], config, *, local_roots=None,
                       trusted_local: bool = False) -> list[bytes]:
    loop = asyncio.get_running_loop()
    marked = _with_source_policy(segments, local_roots, trusted_local)
    try:
        return await loop.run_in_executor(None, _image_bytes, marked)
    except Exception as e:
        log.debug(f"取原图失败：{type(e).__name__}")
        return []


async def _preview_urls(raws: list[bytes]) -> list[str]:
    """给主模型看的原图 / 宫格预览；失败只回空，不影响转述。"""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, vision.preview_urls, raws)
    except Exception as e:
        log.debug(f"拼预览图失败：{type(e).__name__}")
        return []


async def _see(raws: list[bytes], config, mode: str = "",
               fact_store=None) -> tuple[str, list[str]]:
    """一次看完这串图，返回 (转述, 给主模型看的 data URL)。

    多张图由 vision 拼成宫格只打一次模型，不再逐张串行；
    图数 >4 时主模型拿到的是「宫格 + 前 2 张原图」。
    整体有硬超时：宁可没有转述，也不把回覆拖死。

    ``fact_store`` 只在生产路径传进来（``fact_store_for_bot`` 惰性建的
    ``state/perception`` 缓存）。命中缓存且事实足够时 ``omit_original_image``
    为真，这里就**不把原图再塞回主模型**，但保留缓存里的事实描述；没命中
    的新图仍然照旧附图，绝不假装缓存看过一张没见过的图。不传 ``fact_store``
    时走原来的 ``vision.see`` 路径，老测试/老调用方行为不变。
    """
    raws = [r for r in (raws or []) if r][:vision.MAX_GRID_IMAGES]
    if not raws:
        return "", []

    # 看图可以走另一家（主模型换渠道时，这里仍用原来那家的 key）
    key = str(config.get("llm.vision_api_key", "")
              or config.get("llm.api_key", "")).strip()
    if not key:
        return "", await _preview_urls(raws)
    base = str(config.get("llm.vision_base_url", "")
               or config.get("llm.base_url", ""))
    model = str(config.get("llm.vision_model", "") or config.get("llm.model", ""))
    effort = str(config.get("llm.vision_reasoning_effort", "")
                 or config.get("llm.reasoning_effort", ""))
    budget = int(config.get("llm.vision_timeout", 30) or 30)
    fallback = str(config.get("llm.vision_fallback_model", "") or "")
    fallback_budget = int(config.get("llm.vision_fallback_timeout", 30) or 30)
    # 备用模型可能跟主模型不是同一个渠道：没单独配 key 就用聊天链的 key
    fallback_key = str(config.get("llm.api_key", "") or "").strip()
    total = budget + (fallback_budget if fallback else 0) + 10

    loop = asyncio.get_running_loop()
    see_meta = getattr(vision, "see_with_meta", None)
    if fact_store is not None and callable(see_meta):
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: see_meta(
                        raws, key, base, model, budget, effort,
                        fallback, fallback_budget, fallback_key,
                        mode=mode, fact_store=fact_store)),
                timeout=total)
        except Exception as e:
            log.warn(f"看图超时/失败：{type(e).__name__} {e}")
            return "", await _preview_urls(raws)
        desc = str(getattr(result, "text", "") or "")
        cache_hit = bool(getattr(result, "cache_hit", False))
        omit = bool(getattr(result, "omit_original_image", False))
        if cache_hit and omit:
            return desc, []
        return desc, await _preview_urls(raws)

    # 老路径：没有 per-bot FactStore（老测试 / 裸 bot）时保持原样。
    urls = await _preview_urls(raws)
    try:
        desc = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: vision.see(raws, key, base, model, budget, effort,
                                         fallback, fallback_budget, fallback_key,
                                         mode=mode)),
            timeout=total)
        return str(desc or ""), urls
    except Exception as e:
        log.warn(f"看图超时/失败：{type(e).__name__} {e}")
        return "", urls


async def _describe_images(segments: list[dict], config, *, local_roots=None,
                           trusted_local: bool = False) -> tuple[str, list[str]]:
    """先取原图（快、不依赖模型），再尝试详细转述（有超时上限）。"""
    raws = await _read_images(segments, config, local_roots=local_roots,
                              trusted_local=trusted_local)
    if not raws:
        return "", []
    return await _see(raws, config)


async def _image_details(segments: list[dict], config,
                         describe_budget: int, fact_store=None, *,
                         local_roots=None,
                         trusted_local: bool = False) -> list[dict]:
    """逐张转述，保留每张图自己的 data URL；描述预算之外的图仍保留原图。

    表情包（sub_type=1 / summary 含「表情」）走 sticker 模式：只要「表达什么意思」，
    不要画面描述 —— 群里没人想看「一只橘猫举着牌子写着…」这种转述。

    ``fact_store`` 是可选的生产缓存（``state/perception``）；不传时与旧行为一致。
    ``local_roots`` / ``trusted_local`` 是本地图片的显式来源许可（见
    ``vision._load_bytes``），不传时本地路径一律拒绝。
    每张 detail 同时带回 ``from`` / ``uid``（转发图取自节点发送者），供上层
    统一编号时保留归属；这些归属字段只存在于内存事件里，不写进事实缓存。
    """
    if not segments:
        return []
    image_segments = [seg for seg in segments
                      if isinstance(seg, dict) and seg.get("type") == "image"]
    loop = asyncio.get_running_loop()
    marked = _with_source_policy(segments, local_roots, trusted_local)
    tagged = await loop.run_in_executor(None, _image_bytes_tagged, marked)
    details: list[dict] = []
    for pos, (raw, sticker) in enumerate(tagged[:vision.MAX_GRID_IMAGES]):
        source = image_segments[pos] if pos < len(image_segments) else {}
        src_from = str(source.get("_from") or "")
        src_uid = identity.uid_of(source.get("_uid"))
        if pos < describe_budget:
            see_kwargs = {"fact_store": fact_store} if fact_store is not None else {}
            desc, urls = await _see([raw], config,
                                    mode="sticker" if sticker else "",
                                    **see_kwargs)
        else:
            desc = ""
            try:
                urls = await loop.run_in_executor(None, vision.preview_urls, [raw])
            except Exception as e:
                log.debug(f"生成图片预览失败：{type(e).__name__}")
                urls = []
        details.append({"desc": str(desc or "").strip(), "urls": urls,
                        "sticker": bool(sticker), "from": src_from,
                        "uid": src_uid})
    return details


async def _fetch_quote_chain(conn, reply_id, self_id) -> list[dict]:
    """最多取三层引用；message_id 统一成字符串去重，循环时立即停止。"""
    chain: list[dict] = []
    seen: set[str] = set()
    next_id = reply_id
    for depth in range(1, 4):
        if not next_id:
            break
        key = str(next_id).strip()
        if not key or key in seen:
            break
        seen.add(key)
        segs, sender = await _fetch_quoted(conn, next_id)
        if not segs:
            break
        who = str(sender.get("card") or sender.get("nickname") or "")
        plain = _drop_placeholders(msg.to_plain(segs, self_id=self_id))
        # 归属只认真实 sender.user_id（QQ 号），不按昵称反查/硬猜。
        chain.append({"depth": depth, "from": who,
                      "uid": identity.uid_of(sender.get("user_id")),
                      "text": plain, "segments": segs, "sender": sender})
        next_id = msg.find_reply_id(segs)
    return chain


async def _see_video(bot, conn, seg: dict, config) -> str:
    """抽视频帧 → 视觉转述。任何一步失败都回空串，绝不让链路断掉。"""
    try:
        n = int(config.get("video.frames", 3) or 3)
    except (TypeError, ValueError):
        n = 3
    # 先试 NapCat get_file 换绝对路径（缓存文件名常见）
    cache_file = ""
    data = seg.get("data") or {}
    name = str(data.get("file") or "")
    if conn is not None and name and not name.startswith("file:///"):
        try:
            out = await conn.call("get_file", file=name, timeout=40)
            if isinstance(out, dict):
                inner = out.get("data") if isinstance(out.get("data"), dict) else {}
                cache_file = str(out.get("file") or inner.get("file") or "")
        except Exception as e:
            log.debug(f"get_file 短视频失败：{type(e).__name__}")
    path = video.local_path(seg, cache_file)
    if not path:
        log.debug(f"视频没有本地路径：{name[:40]!r}")
        return ""
    loop = asyncio.get_running_loop()
    try:
        frames = await asyncio.wait_for(
            loop.run_in_executor(None, video.extract_frames, path, n),
            timeout=40)
    except Exception as e:
        log.debug(f"视频抽帧失败：{type(e).__name__}")
        return ""
    if not frames:
        return ""
    try:
        desc, _ = await asyncio.wait_for(_see(frames, config), timeout=60)
    except Exception as e:
        log.warn(f"视频转述失败：{type(e).__name__}")
        return ""
    return str(desc or "").strip()


def _recent_deque(bot, session_id):
    """这个会话最近出现过的图（仅内存）。bot.recent_images[sid] = deque(maxlen=6)。"""
    reg = getattr(bot, "recent_images", None)
    if not isinstance(reg, dict):
        reg = {}
        try:
            setattr(bot, "recent_images", reg)
        except Exception:
            return collections.deque(maxlen=_RECENT_MAX)
    dq = reg.get(session_id)
    if not isinstance(dq, collections.deque):
        dq = collections.deque(maxlen=_RECENT_MAX)
        reg[session_id] = dq
    return dq


def _push_recent(bot, session_id, items) -> None:
    """把最近出现的图塞进会话 deque（maxlen=6）。

    兼容两种输入：
      - 裸 URL 字符串（旧格式），包成 self=False、who/uid 留空的 dict；
      - 带归属的 dict：{"url","who","uid","self","ts","desc"}。
    没有真正 url 的（本地产生的图 / 表情包）允许用 ``kind`` / ``data_url`` /
    ``placeholder`` 占位，至少让下一轮知道「刚刚出现过一张图」。
    """
    if not session_id or not items:
        return
    try:
        now = time.time()
        packed: list[dict] = []
        for it in items:
            if not it:
                continue
            if isinstance(it, str):
                entry = {"url": it, "who": "", "uid": "", "self": False,
                         "ts": now, "desc": ""}
            elif isinstance(it, dict):
                entry = dict(it)
                entry.setdefault("url", "")
                entry.setdefault("who", "")
                entry.setdefault("uid", "")
                entry.setdefault("self", False)
                entry.setdefault("ts", now)
                entry.setdefault("desc", "")
            else:
                continue
            # 有真实 url，或带了一个能识别的占位（本地图/表情包），才记
            if entry.get("url") or entry.get("data_url") or entry.get("kind") \
                    or entry.get("placeholder"):
                packed.append(entry)
        if packed:
            _recent_deque(bot, session_id).extend(packed)
    except Exception as e:
        log.debug(f"记录近期图片失败：{type(e).__name__}")


def _recent_entries(images, who: str = "", uid: str = "", self: bool = False) -> list[dict]:
    """把 _image_details 的结果整理成 recent_images 的归属条目。"""
    entries: list[dict] = []
    for item in images:
        desc = str(item.get("desc") or "").strip()
        urls = [u for u in (item.get("urls") or []) if u]
        if not urls:
            # 没有可给模型看的 url，仍记一个占位，让下一轮知道出现过一张图
            entries.append({"url": "", "who": who, "uid": str(uid or ""),
                            "self": bool(self), "ts": time.time(),
                            "desc": desc, "placeholder": True})
            continue
        for url in urls:
            entries.append({"url": url, "who": who, "uid": str(uid or ""),
                            "self": bool(self), "ts": time.time(), "desc": desc})
    return entries


_IMAGE_PLACEHOLDER_RE = re.compile(r"\s*\[图片\]")


def _drop_placeholders(text: str) -> str:
    """去掉正文里的 `[图片]` 占位——图另有详细转述，别让模型以为只有几个字。"""
    return " ".join(_IMAGE_PLACEHOLDER_RE.sub("", str(text or "")).split())


def _quoted_body(item: dict) -> str:
    bits = [item["text"]] if item["text"] else []
    count = len(item["image_segments"])
    if count:
        descriptions = [d["display_desc"] for d in item["images"] if d["display_desc"]]
        bits.append(f"（引用了 {count} 张图：{'；'.join(descriptions)}）" if descriptions
                    else f"（引用了 {count} 张图，没能看清）")
    return " ".join(bits).strip()


async def enrich(bot, conn, ev) -> None:
    """给事件补上引用链、统一编号的图片描述及其他感知字段。"""
    if ev.type != "message":
        return

    ev.quoted_chain = []
    ev.image_items = []
    # 每次感知都从「没有 uid」开始：取不到真实 sender.user_id 时宁可留空，
    # 也不能让上一次事件/昵称反查的 uid 串到这一条引用上。
    ev.quoted_uid = ""
    ev.quoted_is_self = False
    # 生产路径用 per-bot 的 FactStore（state/perception）；裸 bot / 老测试为 None，
    # 继续走 vision 的纯内存默认缓存，测试行为不变。
    fact_store = fact_store_for_bot(bot)

    # 先把消息取回来，图片稍后按「本条 → 引用 → 转发」统一处理。
    quote_chain = []
    if getattr(ev, "reply_id", None):
        quote_chain = await _fetch_quote_chain(
            conn, ev.reply_id, getattr(ev, "self_id", 0))
        for item in quote_chain:
            item["image_segments"] = [s for s in item["segments"]
                                      if s.get("type") == "image"]
            item["images"] = []
        if quote_chain:
            sender = quote_chain[0]["sender"]
            ev.quoted_from = quote_chain[0]["from"]
            quoted_uid = quote_chain[0].get("uid") or identity.uid_of(
                sender.get("user_id"))
            ev.quoted_uid = quoted_uid
            if quoted_uid:
                ev.quoted_is_self = quoted_uid == identity.uid_of(
                    getattr(ev, "self_id", 0))

    forward_id = ""
    for seg in ev.message:
        if seg.get("type") == "forward":
            forward_id = str((seg.get("data") or {}).get("id") or "")
            if forward_id:
                break
    forward_text = ""
    forward_segments: list[dict] = []
    if forward_id:
        forward_text, forward_segments = await _fetch_forward(conn, forward_id)

    # ---------- JSON 卡片（B站 / 小程序） ----------
    if any(seg.get("type") == "json" for seg in ev.message):
        text = card_text(ev.message)
        if text:
            ev.card_text = text
            log.debug(f"卡片内容：{text[:60]}")

    # ---------- 语音 ----------
    records = [s for s in ev.message if s.get("type") == "record"]
    if records:
        text = await _transcribe_record(bot, conn, records[0])
        if text:
            ev.voice_text = text
            log.info(f"语音转写：{text[:60]}")

    # ---------- 视频（抽帧转述） ----------
    videos = [s for s in ev.message if s.get("type") == "video"]
    if videos and bot.config.get("video.enabled", True):
        desc = await _see_video(bot, conn, videos[0], bot.config)
        if desc:
            ev.video_desc = desc
            log.info(f"看视频：{desc[:80]}")

    # ---------- 有没有在说它（已经 @ 它就不用再提示了） ----------
    if (getattr(ev, "is_group", False)
            and not getattr(ev, "to_me", False)
            and talks_about_bot(getattr(ev, "plain", ""), _nicknames(bot))):
        ev.talked_about = True

    # ---------- 图片：统一编号，但旧的原图字段仍按来源分别保留 ----------
    # 本地图片路径默认不可信（见 vision._load_bytes）：只有 OneBot 数组格式
    # 事件/回包里的媒体才带 trusted_local 许可；CQ 字符串可能被用户文本伪造，
    # 只能靠配置的 vision.local_roots 允许目录放行。
    local_roots = _configured_local_roots(bot.config)
    transport_trusts_local = _event_transport_trusts_local(ev)
    detail_kwargs: dict = {}
    if fact_store is not None:
        detail_kwargs["fact_store"] = fact_store
    if local_roots:
        detail_kwargs["local_roots"] = local_roots
    own_kwargs = dict(detail_kwargs)
    if transport_trusts_local:
        own_kwargs["trusted_local"] = True
    own_segments = [s for s in ev.message if s.get("type") == "image"]
    own_images = await _image_details(own_segments, bot.config, 4, **own_kwargs)
    describe_left = max(0, 4 - len(own_images))
    for item in quote_chain:
        quote_kwargs = dict(detail_kwargs)
        if getattr(item.get("segments"), "transport_trusted", False):
            quote_kwargs["trusted_local"] = True
        item["images"] = await _image_details(
            item["image_segments"], bot.config, describe_left, **quote_kwargs)
        describe_left = max(0, describe_left - len(item["images"]))
    forward_kwargs = dict(detail_kwargs)
    if _forward_local_trusted(forward_segments):
        forward_kwargs["trusted_local"] = True
    forward_images = await _image_details(
        forward_segments, bot.config, describe_left, **forward_kwargs)

    own_from = getattr(ev, "sender_name", "") or ""
    own_uid = identity.uid_of(getattr(ev, "user_id", ""))
    own_self = bool(own_uid) and own_uid == identity.uid_of(
        getattr(ev, "self_id", 0))

    groups = [("本条", own_images, own_from, own_uid, own_self)]
    for qitem in quote_chain:
        qsender = qitem.get("sender") or {}
        qui = qitem.get("uid") or identity.uid_of(qsender.get("user_id"))
        groups.append(("引用", qitem["images"], qitem.get("from", "") or "",
                       qui, bool(qui) and qui == identity.uid_of(
                           getattr(ev, "self_id", 0))))
    groups.append(("转发", forward_images, "", "", False))
    total = sum(len(images) for _source, images, _from, _uid, _self in groups)
    idx = 0
    for source, images, src_from, src_uid, src_self in groups:
        for item in images:
            idx += 1
            item["idx"] = idx
            item["source"] = source
            # 谁发的：本条=sender，引用=quoted_from，转发=节点里的原发送者。
            # 昵称只用于展示，归属以真实 uid 为准。
            item["from"] = (src_from or str(item.get("from") or "")
                            or str(item.get("_from") or "") or "")
            item["uid"] = (src_uid or identity.uid_of(item.get("uid"))
                           or identity.uid_of(item.get("_uid")))
            item["self"] = bool(src_self)
            item["data_url"] = item["urls"][0] if item["urls"] else ""
            desc = item["desc"]
            item["display_desc"] = f"【图{idx}】{desc}" if total > 1 and desc else desc
            ev.image_items.append({"idx": idx, "desc": item["display_desc"],
                                   "source": source, "data_url": item["data_url"],
                                   "from": item["from"], "uid": item["uid"],
                                   "self": item["self"],
                                   "sticker": bool(item.get("sticker"))})

    own_urls = [url for item in own_images for url in item["urls"] if url]
    if own_urls:
        ev.image_data = own_urls[:int(bot.config.get("llm.vision_max_images", 4) or 4)]
    if own_images:
        # 缓存命中时 urls 为空，仍然记一条带描述的占位：下一轮「刚才那张图」
        # 还能按 uid + 描述回看，但不会把同一张原图反复塞回主模型。
        _push_recent(bot, ev.session_id,
                     _recent_entries(own_images, own_from, own_uid, own_self))
    if quote_chain:
        quote_urls = []
        for item in quote_chain[0]["images"]:
            for url in item["urls"]:
                if url and url not in quote_urls:
                    quote_urls.append(url)
        if quote_urls:
            ev.quote_image_data = quote_urls
        # 最近图：每一层引用都带上「谁发的」——引用的图不是本条 sender 的
        for qitem in quote_chain:
            qfrom = qitem.get("from", "") or ""
            qsender = qitem.get("sender") or {}
            qui = qitem.get("uid") or identity.uid_of(qsender.get("user_id"))
            q_self = bool(qui) and qui == identity.uid_of(
                getattr(ev, "self_id", 0))
            _push_recent(bot, ev.session_id,
                         _recent_entries(qitem["images"], qfrom, qui, q_self))
    forward_urls = [url for item in forward_images for url in item["urls"] if url]
    if forward_urls:
        ev.forward_image_data = forward_urls
    fwd_entries: list[dict] = []
    for item in forward_images:
        ffrom = str(item.get("from") or item.get("_from") or "") \
            or "转发内容里的人"
        fwd_entries.extend(_recent_entries(
            [item], ffrom, item.get("uid") or identity.uid_of(item.get("_uid")),
            False))
    if fwd_entries:
        _push_recent(bot, ev.session_id, fwd_entries)

    descriptions = [item["desc"] for item in ev.image_items if item["desc"]]
    if descriptions:
        ev.image_desc = "\n".join(descriptions)
        log.info(f"看图：{ev.image_desc[:80]}")
    # 傳 list 而不是 join 成一個字串：一條訊息帶多張圖時，store.annotate_last
    # 才能逐格填描述（傳 str 只會填第一格，其餘格子永遠裸著）。
    own_desc = [item["display_desc"] for item in own_images
                if item["display_desc"] and item["display_desc"] != "[图片]"]
    if own_desc:
        annotate = getattr(getattr(bot, "store", None), "annotate_last", None)
        if callable(annotate):
            try:
                annotate(ev.session_id, getattr(ev, "user_id", ""), own_desc)
            except Exception as e:
                log.debug(f"回填图片描述失败：{type(e).__name__}")

    # ---------- 引用文本：首层保持旧格式，更深层追加为嵌套链 ----------
    if getattr(ev, "reply_id", None):
        if quote_chain:
            bodies = [_quoted_body(item) for item in quote_chain]
            ev.quoted_text = bodies[0]
            for pos in range(1, len(bodies)):
                previous = quote_chain[pos - 1]["from"] or "他"
                current = quote_chain[pos]["from"] or "有人"
                ev.quoted_text += f"（{previous} 又引用了{current}：{bodies[pos]}）"
            ev.quoted_text = ev.quoted_text[:1200]
            if not ev.quoted_text:
                ev.quoted_text = "（他引用了一条消息，但内容取不到）"
            log.info(f"引用内容：{ev.quoted_text[:60]}")
        else:
            ev.quoted_text = "（他引用了一条消息，但内容取不到）"
            ev.quoted_from = ""
            log.info(f"引用消息内容取不到 {ev.reply_id!r}：get_msg 未返回消息段")
        ev.quoted_chain = [{"depth": item["depth"], "from": item["from"],
                            "uid": item.get("uid") or identity.uid_of(
                                (item.get("sender") or {}).get("user_id")),
                            "text": item["text"],
                            "images": [d["display_desc"] for d in item["images"]
                                       if d["display_desc"]]}
                           for item in quote_chain]

    # ---------- 合并转发文本 ----------
    if forward_id:
        parts: list[str] = []
        if forward_text:
            parts.append(forward_text)
        forward_desc = "；".join(item["display_desc"] for item in forward_images
                                 if item["display_desc"])
        if forward_desc:
            parts.append(f"（转发的图：{forward_desc}）")
        elif forward_segments:
            parts.append(f"（转发了 {len(forward_segments)} 张图，没能看清）")
        if parts:
            ev.forward_text = "\n".join(parts)[:4000]
            log.debug(f"转发内容：{len(ev.forward_text)} 字 / {len(forward_segments)} 张图")


def render(ev) -> str:
    """把感知结果拼成给模型看的一段话（附件形式，不污染原始文本）。"""
    bits: list[str] = []
    quoted = getattr(ev, "quoted_text", "")
    if quoted:
        bits.append(f"（他引用了这条消息：{quoted}）")
    img = getattr(ev, "image_desc", "")
    if img:
        bits.append(img)
    video_desc = getattr(ev, "video_desc", "")
    if video_desc:
        bits.append(f"（这条消息带视频：{video_desc}）")
    return "".join(bits)