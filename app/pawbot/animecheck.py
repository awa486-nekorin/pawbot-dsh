"""自动抓图前的画风门槛：这一张到底是二次元，还是真人照片/照片合成？

斗图网（doutu）的搜索结果什么都有，真人群里常见的熊猫头、真人表情包、
影视截图混进来以后，表情包库就脏了。所以 fetch_from_doutu 在 save() 之前
先把图丢给看图模型判一次，只放行二次元。

模型固定 gemini-3.8-flash（可用 persona.anime_check_model 覆盖），走
data/config.json 里的 llm.base_url / llm.api_key（OpenAI 兼容
/v1/chat/completions）。判定标准跟 tools/classify_stickers.py 一致。

约定：
    True  = 二次元，可以入库
    False = 明确不是二次元，不要
    None  = 判不了（没配置 / 超时 / 429、5xx 重试用尽 / 回复解析不出）
           调用方应当按「不放行」处理，别让没判过的图混进库

结果按 sha1 缓存在 <stickers_dir>/anime_cache.json（原子写，最多 500 条），
同一张图反复抓到不会重复打模型。缓存只存 True/False，None 不进缓存——
临时故障下次还能重试。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

MODEL = "gemini-3.8-flash"
USER_AGENT = "PawBot/1.0"
MAX_SIDE = 640
JPEG_QUALITY = 80
# gemini-3.8-flash 的思考 token 会吃掉不少额度，max_tokens 太小会被截成半截 JSON
MAX_TOKENS = 1200
REASONING_EFFORT = "minimal"
RETRY_BACKOFF = 1.0
CACHE_NAME = "anime_cache.json"
CACHE_MAX = 500

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ANIME_RULES = """你是表情包画风审核员。判断这张图是不是「二次元」画风的表情包。

算二次元（anime=true）：
- 动漫 / 漫画 / Q 版 / 日系插画风格的画面
- 二次元游戏立绘、动漫角色的表情包（东方 Project、Vocaloid 这类明显是动漫角色的也算）

不算二次元（anime=false）：
- 真人照片、真人照片加字、真人合影
- 现实动物照片
- 纯色块 / 纯文字 / 简单图形加字
- 熊猫头这类「真人脸 + 简笔画身体」的合成图
- 照片拼接、影视剧截图
"""

PROMPT = _ANIME_RULES + """
只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释：
{"anime": true 或 false, "kind": "动漫插画/Q版/漫画/真人照片/照片合成/纯文字/其它", "reason": "不超过20字的中文理由"}"""

# sha1 -> bool。进程内缓存 + 按 stickers_dir 懒加载/落盘。
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, bool] = {}
_CACHE_PATH: str | None = None


# ---------------------------------------------------------------- 配置

def _llm(config) -> tuple[str, str] | None:
    """取中转的 base_url / api_key。缺一个就算没配，返回 None（绝不打印 key）。"""
    try:
        base = str(config.get("llm.base_url", "") or "").strip().rstrip("/")
        key = str(config.get("llm.api_key", "") or "").strip()
    except Exception:
        return None
    if not base or not key:
        return None
    return base, key


def configured(config) -> bool:
    """看图通道是否配好。没配的时候调用方没法判定，只能另想办法。"""
    return _llm(config) is not None


def _model_name(config) -> str:
    try:
        name = str(config.get("persona.anime_check_model", "") or "").strip()
    except Exception:
        name = ""
    return name or MODEL


def _stickers_dir(config) -> str:
    try:
        d = str(config.get("persona.stickers_dir", "") or "").strip()
    except Exception:
        d = ""
    if not d:
        d = "data/stickers"
    if not os.path.isabs(d):
        d = os.path.join(_ROOT, d)
    return d


def cache_path(config) -> str:
    return os.path.join(_stickers_dir(config), CACHE_NAME)


# ---------------------------------------------------------------- 看图

def _sniff_data_mime(raw: bytes) -> str | None:
    """缩图失败时用原图直发，靠魔数给个 MIME。"""
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _data_url(raw: bytes) -> str | None:
    """缩到最长边 640、转 JPEG 80 再 base64；PIL 解不开就原图直发。

    原图直发是为了「解码失败不等于画风不明」：斗图网的图偶有奇形怪状的
    JPEG，交给模型看一眼比直接判 unknown 更有用。真正不是图片的返回 None。
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            try:
                im.seek(0)                      # 动图只取第一帧
            except (EOFError, ValueError):
                pass
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[-1])
                im = canvas
            else:
                im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=JPEG_QUALITY)
            payload = buf.getvalue()
        return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")
    except Exception:
        mime = _sniff_data_mime(raw)
        if not mime:
            return None
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _reply_text(obj) -> str:
    try:
        content = obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "1", "是", "动漫", "二次元"):
            return True
        if s in ("false", "no", "0", "否", "不是", "非二次元"):
            return False
    return None


def parse_anime(text: str) -> bool | None:
    """从回复里挖 JSON，只取 anime 字段。容忍 ```json 围栏和前后废话。"""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    candidates = []
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if fence:
        candidates.append(fence.group(1))
    left, right = s.find("{"), s.rfind("}")
    if left >= 0 and right > left:
        candidates.append(s[left:right + 1])
    candidates.append(s)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        verdict = _as_bool(obj.get("anime"))
        if verdict is not None:
            return verdict
    return None


def _post_json(base: str, key: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def is_anime(raw: bytes, config, timeout: int = 30) -> bool | None:
    """单次判定。True / False / None（没配置、超时、429 或 5xx 重试用尽、解析不出）。

    只有 429 / 5xx 重试一次；501 这类「服务端不支持」立刻放弃，别白等。
    """
    if not raw:
        return None
    creds = _llm(config)
    if creds is None:
        return None
    data_url = _data_url(raw)
    if data_url is None:
        return None

    payload = {
        "model": _model_name(config),
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }

    for attempt in range(2):
        try:
            obj = _post_json(creds[0], creds[1], payload, timeout)
        except urllib.error.HTTPError as exc:
            # 501（未实现）/505（版本不支持）这种重试也没用，直接放弃
            retryable = exc.code == 429 or (500 <= exc.code < 600
                                            and exc.code not in (501, 505))
            if retryable and attempt == 0:
                time.sleep(RETRY_BACKOFF)
                continue
            return None
        except Exception:                       # 超时 / 断网 / 坏 JSON
            return None
        return parse_anime(_reply_text(obj))
    return None


# ---------------------------------------------------------------- 缓存

def _load_cache(path: str) -> None:
    """按路径懒加载；坏档当空缓存，不影响抓图。"""
    global _CACHE_PATH
    if _CACHE_PATH == path:
        return
    _CACHE.clear()
    _CACHE_PATH = path
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for digest, verdict in data.items():
        if isinstance(digest, str) and isinstance(verdict, bool):
            _CACHE[digest] = verdict


def _save_cache() -> None:
    if not _CACHE_PATH:
        return
    try:
        while len(_CACHE) > CACHE_MAX:          # 超量先丢最早的
            _CACHE.pop(next(iter(_CACHE)))
        path = _CACHE_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        pass


def _read_raw(raw_or_path) -> bytes | None:
    if isinstance(raw_or_path, (bytes, bytearray)):
        return bytes(raw_or_path)
    try:
        with open(str(raw_or_path), "rb") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def is_anime_cached(raw_or_path, config, timeout: int = 30) -> bool | None:
    """先查 sha1 缓存，miss 才打看图模型。只有 True/False 会落缓存。"""
    raw = _read_raw(raw_or_path)
    if not raw:
        return None
    digest = hashlib.sha1(raw).hexdigest()
    path = cache_path(config)

    with _CACHE_LOCK:
        _load_cache(path)
        if digest in _CACHE:
            return _CACHE[digest]

    verdict = is_anime(raw, config, timeout=timeout)
    if verdict is None:
        return None

    with _CACHE_LOCK:
        _load_cache(path)
        _CACHE[digest] = verdict
        _save_cache()
    return verdict
