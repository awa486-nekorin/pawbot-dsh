"""
看图。

`deepseek-flash` 实测（2026-10-20）是**多模态的**，能吃 base64 图片。
但它同时是思考型模型：reasoning_tokens 会先吃掉一截预算，
max_tokens 给小了（<300）就会出现「content 为空、只有 reasoning」的情况。
所以这里单独给一个够大的预算，不跟聊天共用。

用法：
    desc = describe(segments, api_key, base_url, model)
返回一段「[图片：一只狸花猫...]」这样的文本，直接塞进上下文给模型当记忆用。
不识别的图返回 "[图片]"，不让上下文断掉。

缓存
----
同一张图（按**原始字节**的 SHA-256，不看 URL / 路径）只解析一次；缓存在
``pawbot.perception.FactStore`` 里，默认纯内存、有界、带 TTL、single-flight。
需要跨重启复用时，把 ``fact_store=perception.FactStore(directory=..., namespace="vision")``
传进来即可（接线建议见 ``perception.py`` 模块文档）。

需要知道「这次是不是缓存命中、能不能省掉原图」时，用新增的
``see_with_meta`` / ``describe_with_meta``；``see`` / ``describe`` 的签名与
返回类型保持原样（str），老调用方不用改。

来源边界
--------
``describe`` / ``collect_images`` 接受的是消息里的 image 段，其 ``url`` /
``path`` / ``file`` 属于不可信输入。取字节（``_load_bytes``）只允许 base64、
公网 http(s)，以及**显式许可**的本地路径：``trusted_local=True``（仅限协议端
生成的媒体）或 ``local_roots`` 允许目录。默认本地路径一律拒绝，避免「图片地址」
变成读本机文件 / 内网探测。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from . import perception
from .breaker import BREAKER
from .log import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

MAX_BYTES = 6 * 1024 * 1024        # QQ 图一般 1M 上下，卡个上限防爆
MAX_SIDE = 1280                    # 太大的图先缩，省 token
GRID_COLS = 3                      # 宫格列数：9 张正好 3x3
GRID_CELL = 400                    # 每格 400px
MAX_GRID_IMAGES = 9                # 一次最多拼 9 张

#: 兼容旧代码里 ``vision.PROMPT_VERSION`` 的读法；实际指纹见 ``_prompt_version``。
PROMPT_VERSION = perception.PROMPT_VERSION

#: 默认缓存：纯内存，不写运行态目录；测试可通过 ``vision._cache.clear()`` 清空。
_DEFAULT_STORE = perception.FactStore(namespace="vision")
_cache = _DEFAULT_STORE


def _store(fact_store=None):
    return fact_store if fact_store is not None else _DEFAULT_STORE


def _prompt_version(prompt: str) -> str:
    # 同时读两个模块的 PROMPT_VERSION：运维/测试改任意一处都能显式失效。
    version = f"{PROMPT_VERSION}|{perception.PROMPT_VERSION}"
    return perception.prompt_fingerprint(prompt, version)


def _merge_sources(sources: list[str]) -> str:
    seen = [s for s in sources if s]
    if not seen:
        return "none"
    unique = set(seen)
    if len(unique) == 1:
        return seen[0]
    return "mixed"


# ====================================================================== 取字节
# 消息段里的 ``url`` / ``path`` / ``file`` 都是**不可信输入**：群成员可以诱导协议端
# 给出任意值。旧实现把三者都直接 ``open()`` / ``urlopen()``，于是「图片地址」可以
# 变成「读本机任意文件」（还会被送去视觉模型外发），也能打内网 / loopback。
# 这里把取字节收成三条明确通路：
#
# * ``base64://``  —— 已经在消息体里的字节，按上限解码；
# * ``http(s)://`` —— 只允许公网地址（DNS 解析后逐地址校验，重定向逐跳复验）；
# * 本地路径      —— **默认拒绝**。只有调用方显式 ``trusted_local=True``（表示
#    「这段本地媒体由 OneBot 协议端生成」，见 ``perceive.enrich`` 的 provenance
#    判断）或配置/代码给出 ``local_roots`` 允许目录时才读。
_LOCAL_DENIED = ("本地图片来源默认拒绝：需要 trusted_local=True（协议端生成）"
                 "或 vision.local_roots 允许目录")
MAX_REDIRECTS = 5
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def _segment_sources(seg: dict) -> list[str]:
    """一个 image 段里可能有多个可用地址，按优先级排。"""
    data = seg.get("data") or {}
    out: list[str] = []
    for k in ("url", "path", "file"):
        v = data.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    # base64:// 开头的也是有效来源
    return out


def _public_address(address) -> bool:
    """只放行全球可路由单播地址；loopback / 私网 / 链路本地 / 保留段一律拒绝。"""
    try:
        return bool(address.is_global) and not address.is_multicast
    except Exception:
        return False


def _public_host(host: str) -> bool:
    """主机名必须是公网名字或公网 IP 字面量；内网名 / 元数据地址一律拒绝。"""
    text = str(host or "").strip().strip(".").lower()
    if not text or text == "localhost":
        return False
    if text.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        literal = ipaddress.ip_address(text)
    except ValueError:
        literal = None
    if literal is not None:
        return _public_address(literal)
    if "." not in text:
        # 单标签主机名（intranet / metadata / 反代内部名）只可能是内网目标。
        return False
    try:
        infos = socket.getaddrinfo(text, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return False
    if not infos:
        return False
    # 全部解析结果都必须是公网地址：只要有一个内网地址就拒绝（防解析到私网）。
    for info in infos:
        sockaddr = info[4] if len(info) > 4 else ()
        raw = str(sockaddr[0] if sockaddr else "").split("%")[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if not _public_address(address):
            return False
    return True


def _check_remote_url(url: str) -> str:
    """校验 http(s) 取图地址；不合法就抛 ValueError（调用方按取图失败处理）。"""
    text = str(url or "").strip()
    if not text or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise ValueError("invalid image url")
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port                     # 端口非法时这里抛 ValueError
    except ValueError:
        raise ValueError("invalid image url") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("unsupported image scheme")
    if parts.username is not None or parts.password is not None:
        raise ValueError("invalid image url")
    if not _public_host(parts.hostname or ""):
        raise ValueError("image host not public")
    return text


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """重定向逐跳复验：不跟到 file:// / 私网 / 内网地址，也不无限跳。"""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_remote_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = None


def _opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(_CheckedRedirect())
    return _OPENER


def _open_remote(req: urllib.request.Request, timeout: int) -> bytes:
    """真正发请求（单独一层，测试可以只 patch 这里，不碰网络）。"""
    with _opener().open(req, timeout=timeout) as response:
        return response.read(MAX_BYTES + 1)


def _fetch_remote(url: str, timeout: int) -> bytes | None:
    try:
        _check_remote_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        data = _open_remote(req, timeout)
    except Exception as e:
        # 不写完整 URL：可能带 token；只留主机名。
        log.warn(f"取图失败 {_host_of(url) or 'invalid-url'}：{type(e).__name__}")
        return None
    if not data:
        return None
    if len(data) > MAX_BYTES:
        log.warn(f"取图失败 {_host_of(url) or 'invalid-url'}：超过 {MAX_BYTES} 字节")
        return None
    return data


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "")[:120]
    except ValueError:
        return ""


def _local_path(src: str) -> str | None:
    """把 file:// / 裸路径解析成本地路径；其它 scheme、UNC、相对路径都不认。"""
    text = str(src or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("file:"):
        if not lowered.startswith("file://"):
            return None
        try:
            parts = urllib.parse.urlsplit(text)
        except ValueError:
            return None
        if parts.scheme.lower() != "file" or parts.query or parts.fragment:
            return None
        if parts.netloc and parts.netloc.lower() not in ("", "localhost"):
            return None                # file://host/share 是网络共享
        return urllib.request.url2pathname(parts.path) or None
    if "://" in text or text.startswith("//") or text.startswith("\\\\"):
        return None                    # 其它 scheme / UNC 路径
    return text


def _normalized_roots(local_roots) -> tuple[str, ...]:
    out: list[str] = []
    for root in local_roots or ():
        if not isinstance(root, str) or not root.strip():
            continue
        try:
            real = os.path.realpath(os.path.abspath(os.path.expanduser(root.strip())))
        except OSError:
            continue
        out.append(os.path.normcase(real))
    return tuple(dict.fromkeys(out))


def _inside_roots(path: str, roots) -> bool:
    target = os.path.normcase(path)
    for root in roots:
        try:
            if os.path.commonpath((target, root)) == root:
                return True
        except ValueError:             # 不同盘符
            continue
    return False


def _load_local(src: str, *, local_roots, trusted_local: bool) -> bytes | None:
    roots = () if trusted_local else _normalized_roots(local_roots)
    if not trusted_local and not roots:
        log.debug(_LOCAL_DENIED)
        return None
    path = _local_path(src)
    if not path or not os.path.isabs(path):
        return None
    try:
        real = os.path.realpath(path)
        if roots and not _inside_roots(real, roots):
            return None
        if not os.path.isfile(real) or os.path.getsize(real) > MAX_BYTES:
            return None
        with open(real, "rb") as f:
            return f.read(MAX_BYTES)
    except OSError:
        return None


def _decode_inline(src: str) -> bytes | None:
    payload = src[9:]
    if len(payload) > ((MAX_BYTES + 2) // 3) * 4 + 4:
        return None
    try:
        raw = base64.b64decode(payload)
    except Exception:
        return None
    return raw if raw and len(raw) <= MAX_BYTES else None


def _load_bytes(src: str, timeout: int = 20, *, local_roots=None,
                trusted_local: bool = False) -> bytes | None:
    """按来源边界取图字节；取不到返回 None（不抛、不影响主链路）。

    ``trusted_local=True`` 只能由「已确认来源是 OneBot 协议端生成的本地媒体」
    的调用方传（``perceive.enrich``）；**绝不能**用模型输出 / 消息文本直接置真。
    ``local_roots`` 是配置或代码给出的允许目录，只对本地路径生效。
    """
    text = str(src or "")
    if not text:
        return None
    if text.startswith("base64://"):
        return _decode_inline(text)
    if text.lower().startswith(("http://", "https://")):
        return _fetch_remote(text, timeout)
    return _load_local(text, local_roots=local_roots,
                       trusted_local=trusted_local)


def _shrink(raw: bytes) -> tuple[bytes, str] | None:
    """有 Pillow 就缩一下；没有就原样用（flash 接受大图，只是费 token）。"""
    try:
        from PIL import Image          # type: ignore
        import io
    except Exception:
        return (raw, "jpeg")           # 无 Pillow：直传，赌尺寸不大

    try:
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        if max(im.size) > MAX_SIDE:
            im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        return (buf.getvalue(), "jpeg")
    except Exception:
        return (raw, "jpeg")


def data_url(raw: bytes) -> str:
    """单张原图 → 缩好的 data URL（给主模型直接看）。"""
    shrunk = _shrink(raw)
    if not shrunk:
        return ""
    return f"data:image/{shrunk[1]};base64," + base64.b64encode(shrunk[0]).decode()


# ====================================================================== 宫格
def _grid_bytes(raws: list[bytes]) -> bytes | None:
    """把多张图拼成 3 列宫格，每格左上角画序号 1..N。Pillow 不在就返回 None。"""
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont      # type: ignore
    except Exception:
        return None

    cells = []
    for raw in raws[:MAX_GRID_IMAGES]:
        try:
            im = Image.open(io.BytesIO(raw))
            im.seek(0)                  # 动图只取第一帧
            im = im.convert("RGB")
            im.thumbnail((GRID_CELL, GRID_CELL))
        except Exception:
            continue
        cells.append(im)
    if not cells:
        return None

    rows = (len(cells) + GRID_COLS - 1) // GRID_COLS
    canvas = Image.new("RGB", (GRID_COLS * GRID_CELL, rows * GRID_CELL), (20, 20, 20))
    draw = ImageDraw.Draw(canvas)
    font = None
    try:
        font = ImageFont.load_default(size=28)
    except Exception:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

    for i, im in enumerate(cells):
        row, col = divmod(i, GRID_COLS)
        ox, oy = col * GRID_CELL, row * GRID_CELL
        canvas.paste(im, (ox + (GRID_CELL - im.width) // 2,
                          oy + (GRID_CELL - im.height) // 2))
        # 序号画在左上角，带黑描边，深色浅色底都看得清
        draw.text((ox + 8, oy + 6), str(i + 1), fill=(255, 235, 0), font=font,
                  stroke_width=3, stroke_fill=(0, 0, 0))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def grid_data_url(raws: list[bytes]) -> str:
    """多张图拼成宫格后的 data URL；拼不出来返回空串。"""
    grid = _grid_bytes([r for r in (raws or []) if r])
    if not grid:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(grid).decode()


def preview_urls(raws: list[bytes]) -> list[str]:
    """给主模型看的图：≤4 张给全部原图，>4 张给「宫格 + 前 2 张原图」。"""
    raws = [r for r in (raws or []) if r]
    if not raws:
        return []
    if len(raws) <= 4:
        return [u for u in (data_url(r) for r in raws) if u]
    out = []
    grid = grid_data_url(raws)
    if grid:
        out.append(grid)
    out.extend(u for u in (data_url(r) for r in raws[:2]) if u)
    return out


# ====================================================================== 调模型
def _call_vision(b64: str, api_key: str, base_url: str, model: str,
                 prompt: str, timeout: int = 20, budget: int = 900,
                 effort: str = "", retries: int = 1) -> str:
    """调一次视觉接口。

    ⚠️ deepseek-flash 是思考型模型，实测描述一张普通照片要烧 750 个
    reasoning tokens，budget 给 400 时 finish_reason=length、content 直接是空串。
    所以这里：预算不够（length）或 content 空 → 翻倍重试一次，最多到 2400。
    """
    effort = str(effort or "").strip().lower()
    while budget <= 2400:
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
            "max_tokens": budget,
            "temperature": 0.3,
        }
        # 看图也是思考型模型，不显式给强度就吃服务端默认值
        if effort in ("low", "medium", "high"):
            payload["reasoning_effort"] = effort
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions", data=body,
            # 必须带 User-Agent：中转渠道的 Cloudflare 会拦掉 Python 默认 UA（403 1010）
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}",
                     "User-Agent": "PawBot/1.0"})
        # 中转站偶发 502/503/429：重试几次再放弃，别因为一次抖动就丢掉整段描述
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt >= retries:
                    raise
                log.warn(f"看图 HTTP {e.code}，重试 {attempt + 1}/{retries}")
                time.sleep(1.5 * (attempt + 1))
            except urllib.error.URLError as e:
                if attempt >= retries:
                    raise
                log.warn(f"看图网络错误 {type(e.reason).__name__}，重试 {attempt + 1}/{retries}")
                time.sleep(1.5 * (attempt + 1))

        choice = data["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        if text:
            return text
        if choice.get("finish_reason") != "length":
            return ""                   # 不是预算问题，别瞎重试
        log.warn(f"看图 budget={budget} 被思考吃光，翻倍重试")
        budget *= 2
    return ""


def _call_chain(b64: str, chain: list[tuple[str, int, str]], base_url: str,
                prompt: str, budget: int, effort: str) -> str:
    """按主模型 → 备援链调一次视觉接口；失败只影响本次，不写成功缓存。"""
    desc = ""
    for name, secs, api_key in chain:
        if not name:
            continue
        if BREAKER.is_open(name):
            log.warn(f"看图模型 {name} 熔断中，跳过")
            continue
        try:
            desc = _call_vision(b64, api_key, base_url, name, prompt, secs,
                                budget=budget, effort=effort, retries=0)
        except Exception as e:
            BREAKER.record_failure(name)
            log.warn(f"看图失败（{name}）：{type(e).__name__} {e}")
            desc = ""
        else:
            BREAKER.record_success(name)
        # 占位符不是视觉事实，别把它写成永久成功缓存。
        desc = (desc or "").strip()
        if desc in ("[图片]", "[图片：]", "[图片:]"):
            desc = ""
        if desc:
            break
    return desc


PROMPT = (
    "把这张图尽可能详细地转述成文字，让没看过图的人也能在脑子里还原它。"
    "要覆盖：画面主体是什么（几个人/什么东西）、动作与姿态、表情、服装与配饰、"
    "发型与配色、构图与视角、背景与环境、光线和画风。"
    "图里出现的文字、对话气泡、水印、界面字样要照抄原文。"
    "能认出的角色、作品、梗可以点名，但不确定时要写「像是…」而不是断言。"
    "不要评价好坏，不要客套，不要省略细节；用三到八句中文写。"
)


def collect_images(segments: list[dict], *, local_roots=None,
                   trusted_local: bool = False) -> list[str]:
    """把这条消息里的图读出来，缩好，变成 data URL（给主模型直接看）。

    只读本地/下载 + 缩图，不打模型，几毫秒级；转述失败也不影响原图传达。
    来源边界见 ``_load_bytes``：本地路径默认拒绝，需要 ``local_roots`` 或
    ``trusted_local``（协议端生成的媒体）才读。
    """
    urls: list[str] = []
    for seg in segments:
        if seg.get("type") != "image":
            continue
        raw = None
        for src in _segment_sources(seg):
            raw = _load_bytes(src, local_roots=local_roots,
                              trusted_local=trusted_local)
            if raw:
                break
        if not raw:
            continue
        shrunk = _shrink(raw)
        if not shrunk:
            continue
        b64 = base64.b64encode(shrunk[0]).decode()
        urls.append(f"data:image/{shrunk[1]};base64,{b64}")
    return urls


def _describe_segment(seg: dict, chain: list[tuple[str, int, str]],
                      base_url: str, effort: str, store, *,
                      local_roots=None,
                      trusted_local: bool = False) -> tuple[str, bool, str, str]:
    """转述单张图，返回 ``(事实文本, 是否命中, 来源, 缓存键)``。"""
    raw = None
    for src in _segment_sources(seg):
        raw = _load_bytes(src, local_roots=local_roots,
                          trusted_local=trusted_local)
        if raw:
            break
    if not raw:
        return "", False, "none", ""

    key = perception.cache_key(
        [perception.image_digest(raw)],
        model=chain[0][0] if chain else "",
        base_url=base_url,
        effort=effort,
        prompt_version=_prompt_version(PROMPT),
        mode="",
        namespace=getattr(store, "namespace", ""),
    )

    def factory(raw=raw) -> str | None:
        try:
            shrunk = _shrink(raw)
            if not shrunk:
                return None
            b64 = base64.b64encode(shrunk[0]).decode()
        except Exception as e:
            log.warn(f"缩图失败：{type(e).__name__}")
            return None
        return _call_chain(b64, chain, base_url, PROMPT, 900, effort) or None

    value, cached, source = store.run(key, factory)
    return (str(value or ""), bool(cached), source, key)


def describe_with_meta(segments: list[dict], api_key: str, base_url: str, model: str,
                       timeout: int = 20, effort: str = "", fallback: str = "",
                       fallback_timeout: int = 20, *,
                       fact_store=None, local_roots=None,
                       trusted_local: bool = False) -> perception.PerceptionResult:
    """``describe`` 的带元数据版本；缓存命中时 ``omit_original_image`` 为真。

    ``local_roots`` / ``trusted_local`` 只影响「图片来源能不能读」，不影响缓存键
    （缓存仍按原始字节的 SHA-256），命中时也不会把原图再外发。
    """
    store = _store(fact_store)
    if not api_key:
        return perception.PerceptionResult(text="", source="none")

    chain: list[tuple[str, int, str]] = [(model, timeout, api_key)]
    if fallback and fallback != model:
        chain.append((fallback, fallback_timeout, api_key))

    parts: list[str] = []
    keys: list[str] = []
    sources: list[str] = []
    image_count = 0
    all_cached = True

    for seg in segments:
        if not isinstance(seg, dict) or seg.get("type") != "image":
            continue
        image_count += 1
        desc, cached, source, key = _describe_segment(
            seg, chain, base_url, effort, store, local_roots=local_roots,
            trusted_local=trusted_local)
        if key:
            keys.append(key)
        sources.append(source)
        if not cached:
            all_cached = False
        if desc:
            parts.append(f"[图片：{desc}]")
        else:
            parts.append("[图片]")

    text = "".join(parts)
    cached = bool(image_count) and all_cached
    return perception.PerceptionResult(
        text=text,
        key=keys[0] if len(keys) == 1 else "",
        keys=tuple(keys),
        cached=cached,
        source=_merge_sources(sources),
        image_count=image_count,
        omit_original_image=cached and bool(text),
    )


def describe(segments: list[dict], api_key: str, base_url: str, model: str,
             timeout: int = 20, effort: str = "", fallback: str = "",
             fallback_timeout: int = 20, *, fact_store=None, local_roots=None,
             trusted_local: bool = False):
    """把一条消息里所有图片变成文字描述。返回 "[图片：...]" 拼接串。

    主模型（比如 gemini-4-argon）不稳时自动退到 fallback 模型；
    两边都有超时上限，宁可这段描述没有，也不把回覆拖死。

    缓存按图片原始字节的 SHA-256；失败或 ``[图片]`` 不会进入成功缓存。
    ``local_roots`` / ``trusted_local`` 是本地图片的显式来源许可（见
    ``_load_bytes``），默认本地路径一律拒绝。
    """
    return describe_with_meta(
        segments, api_key, base_url, model, timeout, effort, fallback,
        fallback_timeout, fact_store=fact_store, local_roots=local_roots,
        trusted_local=trusted_local,
    ).text


STICKER_PROMPT = (
    "这是一张聊天里的表情包 / 梗图。**不要描述画面内容**：不要说画了什么角色、什么颜色、"
    "什么构图、有什么文字排版。只需要判断它表达什么意思或情绪。"
    "输出格式固定为「表情包：X」，X 是一到三个词的语义标签（例如「无语」「在笑你」「敷衍」"
    "「震惊」「委屈巴巴」）。如果它明显是在接某个具体的人或场面，最多再用半句话说明。"
    "总共不超过 20 个字，不要客套，不要解释。"
)

GRID_PROMPT = (
    "这张图是把多张图片拼在一起的宫格，每格左上角有一个黄色序号。"
    "请按序号逐格转述，每格单独一段，并以「第1张：」「第2张：」这样的开头标出是第几张。"
    "每格都要写清楚：画面主体是什么（几个人/什么东西）、动作与姿态、表情、服装与配饰、"
    "发型与配色、构图与视角、背景与环境、光线和画风。"
    "图里出现的文字、对话气泡、水印、界面字样要照抄原文。"
    "能认出的角色、作品、梗可以点名，但不确定时要写「像是…」而不是断言。"
    "不要评价好坏，不要客套，不要省略细节；每格三到六句中文，全部写全。"
)


def see_with_meta(raws: list[bytes], api_key: str, base_url: str, model: str,
                  timeout: int = 30, effort: str = "", fallback: str = "",
                  fallback_timeout: int = 30, fallback_api_key: str = "",
                  mode: str = "", *,
                  fact_store=None) -> perception.PerceptionResult:
    """``see`` 的带元数据版本。

    1 张：直接转述。
    多张：用 Pillow 拼成 3 列宫格、左上角标序号，只调一次模型，让它逐格转述；
          这样 15 张图也只有一次往返，不会逐张串行超时。
    缓存键是「每张图原始字节的 sha256 串接 + 模型 + base_url + effort + 提示词指纹 + mode」。
    """
    store = _store(fact_store)
    raws = [r for r in (raws or []) if r][:MAX_GRID_IMAGES]
    if not raws or not api_key:
        return perception.PerceptionResult(
            text="", mode=str(mode or ""), image_count=len(raws), source="none")

    single = len(raws) == 1
    if single:
        if str(mode or "") == "sticker":
            prompt, budget = STICKER_PROMPT, 900
        else:
            prompt, budget = PROMPT, 900
    else:
        prompt, budget = GRID_PROMPT, 2400

    key = perception.cache_key(
        [perception.image_digest(raw) for raw in raws],
        model=model,
        base_url=base_url,
        effort=effort,
        prompt_version=_prompt_version(prompt),
        mode=mode,
        namespace=getattr(store, "namespace", ""),
    )

    chain: list[tuple[str, int, str]] = [(model, timeout, api_key)]
    if fallback and fallback != model:
        # 备用模型可能跟主模型不同渠道：没单独给 key 就沿用主 key
        chain.append((fallback, fallback_timeout, fallback_api_key or api_key))

    def factory() -> str | None:
        try:
            if single:
                shrunk = _shrink(raws[0])
                if not shrunk:
                    return None
                b64 = base64.b64encode(shrunk[0]).decode()
            else:
                grid = _grid_bytes(raws)
                if not grid:
                    log.warn("宫格拼不出来（Pillow 不可用或图都坏了），跳过转述")
                    return None
                b64 = base64.b64encode(grid).decode()
        except Exception as e:
            log.warn(f"准备看图输入失败：{type(e).__name__}")
            return None
        return _call_chain(b64, chain, base_url, prompt, budget, effort) or None

    value, cached, source = store.run(key, factory)
    text = str(value or "")
    return perception.PerceptionResult(
        text=text,
        key=key,
        keys=(key,),
        cached=bool(cached),
        source=source,
        mode=str(mode or ""),
        image_count=len(raws),
        # 表情包转述只是语义标签，不足以代替原图；事实描述才建议省掉原图。
        omit_original_image=bool(text) and bool(cached)
        and str(mode or "") != "sticker",
    )


def see(raws: list[bytes], api_key: str, base_url: str, model: str,
        timeout: int = 30, effort: str = "", fallback: str = "",
        fallback_timeout: int = 30, fallback_api_key: str = "",
        mode: str = "", *, fact_store=None) -> str:
    """一次看完一串图，返回纯描述文本（不带 [图片：] 包装）。

    兼容旧签名；需要缓存命中/省原图元数据时用 ``see_with_meta``。
    """
    return see_with_meta(
        raws, api_key, base_url, model, timeout, effort, fallback,
        fallback_timeout, fallback_api_key, mode=mode, fact_store=fact_store,
    ).text


def clear_cache(fact_store=None):
    """清空默认缓存（或指定的 ``fact_store``）。"""
    _store(fact_store).clear()
