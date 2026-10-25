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
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .breaker import BREAKER
from .log import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

MAX_BYTES = 6 * 1024 * 1024        # QQ 图一般 1M 上下，卡个上限防爆
MAX_SIDE = 1280                    # 太大的图先缩，省 token
GRID_COLS = 3                      # 宫格列数：9 张正好 3x3
GRID_CELL = 400                    # 每格 400px
MAX_GRID_IMAGES = 9                # 一次最多拼 9 张
_cache: dict[str, str] = {}        # 同一张图（按 url/路径）只解析一次


# ====================================================================== 取字节
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


def _load_bytes(src: str, timeout: int = 20) -> bytes | None:
    if src.startswith("base64://"):
        try:
            return base64.b64decode(src[9:])
        except Exception:
            return None
    if src.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(src, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(MAX_BYTES)
        except Exception as e:
            log.warn(f"取图失败 {src[:60]}：{type(e).__name__}")
            return None
    # 本地路径（NapCat 有时给 file:// 或者裸路径）
    p = src[7:] if src.startswith("file://") else src
    try:
        if os.path.isfile(p) and os.path.getsize(p) <= MAX_BYTES:
            with open(p, "rb") as f:
                return f.read()
    except Exception:
        pass
    return None


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


PROMPT = (
    "把这张图尽可能详细地转述成文字，让没看过图的人也能在脑子里还原它。"
    "要覆盖：画面主体是什么（几个人/什么东西）、动作与姿态、表情、服装与配饰、"
    "发型与配色、构图与视角、背景与环境、光线和画风。"
    "图里出现的文字、对话气泡、水印、界面字样要照抄原文。"
    "能认出的角色、作品、梗可以点名，但不确定时要写「像是…」而不是断言。"
    "不要评价好坏，不要客套，不要省略细节；用三到八句中文写。"
)


def collect_images(segments: list[dict]) -> list[str]:
    """把这条消息里的图读出来，缩好，变成 data URL（给主模型直接看）。

    只读本地/下载 + 缩图，不打模型，几毫秒级；转述失败也不影响原图传达。
    """
    urls: list[str] = []
    for seg in segments:
        if seg.get("type") != "image":
            continue
        raw = None
        for src in _segment_sources(seg):
            raw = _load_bytes(src)
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


def describe(segments: list[dict], api_key: str, base_url: str, model: str,
             timeout: int = 20, effort: str = "", fallback: str = "",
             fallback_timeout: int = 20):
    """把一条消息里所有图片变成文字描述。返回 "[图片：...]" 拼接串。

    主模型（比如 gemini-4-argon）不稳时自动退到 fallback 模型；
    两边都有超时上限，宁可这段描述没有，也不把回覆拖死。
    """
    if not api_key:
        return ""
    chain = [(model, timeout)]
    if fallback and fallback != model:
        chain.append((fallback, fallback_timeout))
    parts: list[str] = []
    for seg in segments:
        if seg.get("type") != "image":
            continue

        key = json.dumps(seg.get("data") or {}, sort_keys=True, ensure_ascii=False)
        if key in _cache:
            parts.append(_cache[key])
            continue

        raw = None
        for src in _segment_sources(seg):
            raw = _load_bytes(src)
            if raw:
                break
        if not raw:
            parts.append("[图片]")
            continue

        shrunk = _shrink(raw)
        if not shrunk:
            parts.append("[图片]")
            continue
        b64 = base64.b64encode(shrunk[0]).decode()

        desc = ""
        for name, secs in chain:
            if BREAKER.is_open(name):
                log.warn(f"看图模型 {name} 熔断中，跳过")
                continue
            try:
                desc = _call_vision(b64, api_key, base_url, name, PROMPT, secs,
                                    effort=effort, retries=0)
            except Exception as e:
                BREAKER.record_failure(name)
                log.warn(f"看图失败（{name}）：{type(e).__name__}")
                desc = ""
            else:
                BREAKER.record_success(name)
            if desc:
                break

        text = f"[图片：{desc}]" if desc else "[图片]"
        if len(_cache) > 200:          # 别让缓存无限长
            _cache.clear()
        _cache[key] = text
        parts.append(text)

    return "".join(parts)


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


def see(raws: list[bytes], api_key: str, base_url: str, model: str,
        timeout: int = 30, effort: str = "", fallback: str = "",
        fallback_timeout: int = 30, fallback_api_key: str = "",
        mode: str = "") -> str:
    """一次看完一串图，返回纯描述文本（不带 [图片：] 包装）。

    mode="sticker" 时改用 STICKER_PROMPT：只判断表情包表达什么意思，不描述画面内容。

    1 张：直接转述。
    多张：用 Pillow 拼成 3 列宫格、左上角标序号，只调一次模型，让它逐格转述；
          这样 15 张图也只有一次往返，不会逐张串行超时。
    缓存键是「每张图 sha1 串接」，同样的图不重复花钱。
    """
    raws = [r for r in (raws or []) if r][:MAX_GRID_IMAGES]
    if not raws or not api_key:
        return ""

    digest = (str(mode or "") + "|" +
              "".join(hashlib.sha1(r).hexdigest() for r in raws))
    if digest in _cache:
        return _cache[digest]

    if len(raws) == 1:
        shrunk = _shrink(raws[0])
        if not shrunk:
            return ""
        b64 = base64.b64encode(shrunk[0]).decode()
        if str(mode or "") == "sticker":
            prompt = STICKER_PROMPT
            # 思考型模型（deepseek-flash）先烧 reasoning：400 实测不够，
            # 会白走两轮"翻倍重试"，直接给 900
            budget = 900
        else:
            prompt = PROMPT
            budget = 900
    else:
        grid = _grid_bytes(raws)
        if not grid:
            log.warn("宫格拼不出来（Pillow 不可用或图都坏了），跳过转述")
            return ""
        b64 = base64.b64encode(grid).decode()
        prompt = GRID_PROMPT
        budget = 2400                # 9 格要写全，预算给到 _call_vision 的上限

    chain: list[tuple[str, int, str]] = [(model, timeout, api_key)]
    if fallback and fallback != model:
        # 备用模型可能跟主模型不同渠道：没单独给 key 就沿用主 key
        chain.append((fallback, fallback_timeout, fallback_api_key or api_key))

    desc = ""
    for name, secs, key in chain:
        if not name:
            continue
        if BREAKER.is_open(name):
            log.warn(f"看图模型 {name} 熔断中，跳过")
            continue
        try:
            desc = _call_vision(b64, key, base_url, name, prompt, secs,
                                budget=budget, effort=effort, retries=0)
        except Exception as e:
            BREAKER.record_failure(name)
            log.warn(f"看图失败（{name}）：{type(e).__name__} {e}")
            desc = ""
        else:
            BREAKER.record_success(name)
        if desc:
            break

    if desc:
        if len(_cache) > 200:          # 别让缓存无限长
            _cache.clear()
        _cache[digest] = desc
    return desc


def clear_cache():
    _cache.clear()