"""
文字转语音：把一句话交给本地的飞鱼（CosyVoice）服务合成 wav，
再作为 QQ 原生语音（record 段）发出去。

协议：
    GET  127.0.0.1:50021/health  → {"ok": true}
    POST 127.0.0.1:50021/tts     → audio/wav

这里只管「文字 → record 段」：
  · clean_for_speech 把回复文字洗成适合朗读的一两句
  · synth 负责网络调用，失败就回 None，绝不抛给聊天链路
  · should_auto_voice 是自动语音的纯条件判断（方便单测）
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import media
from . import message as msg
from .log import log

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "enabled": True,
    "url": "http://127.0.0.1:50021/tts",
    "health_url": "http://127.0.0.1:50021/health",
    "timeout": 60,
    "max_chars": 0,          # 0 = 不限：长了就分段念，不截断
    "auto_chance": 0.12,
    "auto_max_chars": 0,     # 0 = 不限
    "chunk_chars": 180,      # 一条语音最多念多少字，多出来接着发下一条
    "provider": "local",     # local = 本地 CosyVoice；cloud = 阿里云 Voice Studio
    "cloud_url": "wss://convai-platform.aliyuncs.com/api-ws/v1/inference",
    "cloud_key": "",
    "cloud_voice": "",       # 音色 ID（UUID），控制台「音色管理」里复制
    "cloud_format": "pcm",
    "cloud_sample_rate": 24000,
    "autostart": True,
    # 本地 CosyVoice 启动命令，例如
    # ["/path/to/CosyVoice/.venv/bin/python", "/path/to/CosyVoice/tools/feiyu_server.py"]。
    "server_cmd": [],
}

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`]*`")

# emoji、装饰符号：整段删掉，别让 TTS 念出「笑脸」「爱心」
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 表情、旗、麻将、补充符号
    "\U00002600-\U000027BF"   # 杂项符号、装饰、♡♥
    "\U00002B00-\U00002BFF"   # 箭头、星
    "\U0000FE0F\U0000200D\U000020E3"
    "\U00003030\U000030FB"    # 〰 ・
    "]"
)
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
# 纯符号的括号组（颜文字），含汉字/字母/数字的括号（比如「（笑）」）保留
_KAOMOJI_GROUP_RE = re.compile(r"[（(][^（）()A-Za-z0-9\u4e00-\u9fff]*[)）]")
_KAOMOJI_CHARS = set("ωΩ･°∀´｀＾^＿_＞＜><｡゜ﾟ☆★♪♫●○◆◇■□▲△▼▽๑•̀ㅂ́و✧꒰꒱乁ノヮ")
_PUNCT = "，。！？、,.!?；;：:"
_PUNCT_RUN_RE = re.compile(r"[，。！？、,.!?；;：:]{2,}")

# ---------------------------------------------------------------- 被要求发语音
# 只在「具体地要她开口」时命中；群里随口提到「语音」不算。
# 第一个模式后面允许的语气尾巴有白名单，像「发语音了吗」这种问别人有没有发的不算。
_VOICE_REQUEST_RES = (
    re.compile(
        r"(?<![不別别没沒])(?:發|发|來|来|整|錄|录|搞)\s*(?:條|条|段)?\s*(?:語音|语音)"
        r"(?=$|[\s，,。.!！?？~～、；;:：]|[吧呗嘛啊呀哦咯啦呢哈嘞]"
        r"|听听|聽聽|给我|給我|念|唸|说|說|回)"),
    re.compile(r"(?:用|換成|换成|換|换|改成|改為|改为)\s*(?:語音|语音)\s*(?:說|说|回|發|发|念|唸)"),
    re.compile(r"(?:語音|语音)\s*(?:說|说|回|念|唸|唱)\s*一句"),
    re.compile(r"(?:說|说)\s*(?:句|几句|幾句)?\s*(?:話|话)?\s*(?:給我聽|给我听)"),
    re.compile(r"(?:念|唸)\s*(?:給我聽|给我听|給我|给我)"),
    re.compile(r"(?:讀|读)出(?:來|来)"),
    re.compile(r"唱一句"),
    re.compile(r"(?:讓|让|給|给)?\s*我?\s*看?\s*(?:聽聽|听听)\s*你的?\s*(?:聲音|声音)"),
    re.compile(r"你的?\s*(?:聲音|声音)\s*好?好?(?:聽|听)"),
)

_QUOTE_RES = (
    re.compile(r"「([^」]{1,1000})」"),
    re.compile(r"『([^』]{1,1000})』"),
    re.compile(r"“([^”]{1,1000})”"),
    re.compile(r"\"([^\"]{1,1000})\""),
    re.compile(r"'([^']{1,1000})'"),
)
_SAY_TAIL_RE = re.compile(r"(?:說|说|念|唸)\s*[：:]\s*(.+)$", re.S)
_SENTENCE_END = "。！？!?…；;."


def voice_request(text) -> bool:
    """这条消息是不是在明确要求「用语音说/发条语音」。"""
    raw = str(text or "")
    if not raw.strip():
        return False
    return any(rx.search(raw) for rx in _VOICE_REQUEST_RES)


def explicit_speech_text(text, max_chars: int | None = None) -> str:
    """对方点明要念的内容：引号里的，或「说：/念：」后面那段；没有就回空串。"""
    raw = str(text or "")
    if not raw.strip():
        return ""
    limit = DEFAULTS["max_chars"] if max_chars is None else int(max_chars)
    best: tuple[int, str] | None = None
    for rx in _QUOTE_RES:
        for m in rx.finditer(raw):
            body = m.group(1).strip()
            # 引号里若还套着引号、或本身就是那句要求，就不是要念的内容
            if not body or re.search(r"[「」『』“”\"']", body):
                continue
            if re.search(r"(?:語音|语音)", body):
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), body)
    pick = best[1] if best is not None else None
    if pick is None:
        m = _SAY_TAIL_RE.search(raw)
        if m:
            body = m.group(1).strip().strip("「」『』“”\"' ")
            if body and not re.search(r"(?:語音|语音)", body):
                pick = body
    if pick is None:
        return ""
    return pick[:limit] if limit > 0 else pick


def should_force_voice(config, text) -> bool:
    """对方明确要语音：不受 auto_chance 概率限制。"""
    return available(config) and voice_request(text)


def split_for_voice(text, max_chars: int) -> tuple[str, str]:
    """整条文字转语音时的切分：返回 (要念的话, 剩下的文字)。

    max_chars 为 0（默认）时不限制：整条都念，剩下的文字为空。
    配了正数才按老办法在句末切一刀，剩下的照常打字发。
    """
    clean = clean_for_speech(text)
    if not clean:
        return "", ""
    limit = int(max_chars or 0)
    if limit <= 0 or len(clean) <= limit:
        return clean, ""
    head = clean[:limit]
    cut = -1
    for ch in _SENTENCE_END:
        cut = max(cut, head.rfind(ch))
    if cut > 0:
        return head[:cut + 1], clean[cut + 1:].strip()
    return head, clean[limit:].strip()


def chunk_for_speech(text, size: int = 180) -> list[str]:
    """把要念的话切成每段 size 字以内，尽量在句末断开。

    QQ 一条语音有长度上限，太长就分成连续几条语音发，而不是截断或者不念。
    """
    clean = clean_for_speech(text)
    if not clean:
        return []
    limit = max(20, int(size or 180))
    if len(clean) <= limit:
        return [clean]
    chunks: list[str] = []
    buf = ""
    for piece in re.findall(r"[^。！？!?…；;]+[。！？!?…；;]*", clean):
        while len(piece) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        if buf and len(buf) + len(piece) > limit:
            chunks.append(buf)
            buf = piece
        else:
            buf += piece
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def _one_punct(match) -> str:
    """一串标点压成一个：问号 > 感叹号 > 句号 > 逗号。"""
    run = match.group(0)
    for ch, repl in (("？", "？"), ("?", "？"), ("！", "！"), ("!", "！"),
                     ("。", "。"), (".", "。")):
        if ch in run:
            return repl
    return "，"


def _cfg(config, key, default=None):
    try:
        return config.get(f"tts.{key}", DEFAULTS.get(key, default))
    except Exception:
        return DEFAULTS.get(key, default)


def clean_for_speech(text) -> str:
    """把回复文字洗成适合朗读的一句：去标记、CQ 码、网址、表情和颜文字。"""
    out = str(text or "")
    if not out:
        return ""
    out = media.MARKER_RE.sub("", out)
    out = msg.CQ_RE.sub("", out)
    out = _URL_RE.sub("", out)
    out = _CODE_RE.sub("", out)
    out = _ZERO_WIDTH_RE.sub("", out)
    out = _KAOMOJI_GROUP_RE.sub("", out)
    out = _EMOJI_RE.sub("", out)
    out = "".join(ch for ch in out if ch not in _KAOMOJI_CHARS)
    # 波浪号是拖音，朗读时换成句号；‖ 和换行是分条/断行，读成逗号
    out = out.replace("～", "。").replace("~", "。")
    out = re.sub(r"[‖\r\n]+", "，", out)
    out = re.sub(r"\s+", " ", out)
    # 重复/混排标点压成一个；句首的标点去掉
    out = _PUNCT_RUN_RE.sub(_one_punct, out)
    out = re.sub(r"(?<=[，。！？、,.!?；;：:])\s+", "", out)
    out = re.sub(r"\s+(?=[，。！？、,.!?；;：:])", "", out)
    out = out.strip().lstrip(_PUNCT + " ").strip()
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", out):
        return ""
    return out


def _post_tts(url: str, text: str, timeout: float) -> bytes:
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "PawBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


async def _cloud_synth(config, text: str, timeout: float) -> bytes | None:
    """阿里云 Voice Studio 实时语音合成（WebSocket）。

    协议：run-task → task-started → continue-task(text) → finish-task，
    音频以二进制帧回来，直到 task-finished。format=wav 时服务端直接给带头的
    WAV，省得自己拼。
    """
    try:
        import websockets
    except Exception:
        log.warn("[tts] 云端合成需要 websockets 库（pip install websockets）")
        return None
    url = str(_cfg(config, "cloud_url", "") or DEFAULTS["cloud_url"])
    key = str(_cfg(config, "cloud_key", "") or "").strip()
    voice = str(_cfg(config, "cloud_voice", "") or "").strip()
    if not key or not voice:
        log.debug("[tts] 云端合成缺 key 或音色 ID")
        return None
    fmt = str(_cfg(config, "cloud_format", "wav") or "wav")
    try:
        rate = int(_cfg(config, "cloud_sample_rate", 24000) or 24000)
    except (TypeError, ValueError):
        rate = 24000
    task_id = str(uuid.uuid4())

    async def run() -> bytes | None:
        import websockets as _ws
        chunks: list[bytes] = []
        async with _ws.connect(url, additional_headers={"Authorization": "Bearer " + key},
                               max_size=None, open_timeout=15) as ws:
            await ws.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {"parameters": {"voice": voice, "format": fmt,
                                           "sample_rate": rate},
                            "input": {}}}, ensure_ascii=False))
            started = False
            while True:
                msg = await ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    chunks.append(bytes(msg))
                    continue
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                event = str((data.get("header") or {}).get("event") or "")
                if event == "task-started":
                    started = True
                    await ws.send(json.dumps({
                        "header": {"action": "continue-task", "task_id": task_id,
                                   "streaming": "duplex"},
                        "payload": {"input": {"text": text}}}, ensure_ascii=False))
                    await ws.send(json.dumps({
                        "header": {"action": "finish-task", "task_id": task_id,
                                   "streaming": "duplex"},
                        "payload": {"input": {}}}, ensure_ascii=False))
                elif event == "task-finished":
                    break
                elif event == "task-failed":
                    err = (data.get("payload") or {}).get("error") or {}
                    log.warn(f"[tts] 云端合成失败：{err.get('code')} {err.get('message')}")
                    return None
                elif not started and event in ("task-failed",):
                    return None
        if not chunks:
            return None
        raw = b"".join(chunks)
        if raw[:4] == b"RIFF":
            # wav 模式下服务端每帧都带自己的 44 字节头，直接拼接会得到一堆坏头，
            # 这里把每段的数据块抠出来再套一次头。
            raw = _strip_wav_headers(raw)
        if raw[:4] != b"RIFF":
            raw = _wrap_pcm(raw, rate)
        return raw

    try:
        return await asyncio.wait_for(run(), timeout=max(10.0, timeout))
    except asyncio.TimeoutError:
        log.warn("[tts] 云端合成超时")
        return None
    except Exception as e:
        log.warn(f"[tts] 云端合成失败：{type(e).__name__} {e}")
        return None


def _strip_wav_headers(raw: bytes) -> bytes:
    """把多段 WAV 拼起来时去掉各自的头，只留 PCM 数据。"""
    out = bytearray()
    i = 0
    n = len(raw)
    while i + 12 <= n:
        if raw[i:i + 4] != b"RIFF":
            # 已经不是 RIFF 了，后面全是裸数据
            out += raw[i:]
            break
        # 找 data 块
        j = i + 12
        data = None
        while j + 8 <= n:
            cid = raw[j:j + 4]
            size = int.from_bytes(raw[j + 4:j + 8], "little")
            body = raw[j + 8:j + 8 + size]
            if cid == b"data":
                data = body
                break
            j += 8 + size + (size & 1)
        if data is None:
            break
        out += data
        i = j + 8 + len(data) + (len(data) & 1)
    return bytes(out)


def _wrap_pcm(pcm: bytes, rate: int) -> bytes:
    """裸 PCM（int16 单声道）套 44 字节 WAV 头。"""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate or 24000))
        w.writeframes(pcm)
    return buf.getvalue()


async def synth(config, text) -> bytes | None:
    """合成 wav；清理后为空或任何失败都回 None（只记一行 warn）。

    不按长度拒绝：长的由 record_segments 事先切段，这里只负责念一段。
    provider=cloud 时走阿里云 Voice Studio，本地服务只当备胎。
    """
    clean = clean_for_speech(text)
    if not clean:
        return None
    try:
        base = float(_cfg(config, "timeout", 60) or 60)
    except (TypeError, ValueError):
        base = 60.0
    # 合成本身大约 1 字 0.3 秒（RTF≈1），给三倍余量，长句子才不至于等不到
    timeout = base + len(clean) * 3.0
    provider = str(_cfg(config, "provider", "local") or "local").strip().lower()
    if provider == "cloud":
        raw = await _cloud_synth(config, clean, timeout)
        if raw:
            return raw
        log.debug("[tts] 云端不可用，这一条退回本地")
    url = str(_cfg(config, "url", "") or DEFAULTS["url"])
    try:
        raw = await asyncio.to_thread(_post_tts, url, clean, timeout)
    except Exception as e:
        log.warn(f"[tts] 合成失败：{type(e).__name__} {e}")
        # 服务被杀/崩了：静默再拉一次（有冷却），这一条退回打字
        if isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError)):
            try:
                asyncio.get_running_loop().create_task(revive(config))
            except RuntimeError:
                pass
        return None
    if not raw:
        log.warn("[tts] 合成失败：空音频")
        return None
    return raw


def to_record(raw: bytes) -> dict:
    return msg.Record("base64://" + base64.b64encode(raw).decode("ascii"))


async def record_segments(config, text) -> list[dict]:
    """整段合成，长了就切成连续几条语音；全部失败回空列表。"""
    chunks = chunk_for_speech(text, _cfg(config, "chunk_chars", 180))
    out: list[dict] = []
    for chunk in chunks:
        raw = await synth(config, chunk)
        if raw:
            out.append(to_record(raw))
    if chunks and not out:
        log.warn("[tts] 语音合成全部失败，退回打字")
    elif len(out) < len(chunks):
        log.warn(f"[tts] 语音只合成 {len(out)}/{len(chunks)} 段")
    return out


async def record_segment(config, text) -> dict | None:
    segs = await record_segments(config, text)
    return segs[0] if segs else None


def available(config) -> bool:
    try:
        return bool(_cfg(config, "enabled", True))
    except Exception:
        return False


def should_auto_voice(config, text, has_media=False, used_speak=False,
                      rng=random.random) -> bool:
    """这一轮要不要把文字整条换成语音（纯判断，网络调用在外部做）。"""
    if not available(config):
        return False
    if has_media or used_speak:
        return False
    raw = str(text or "")
    if not raw.strip():
        return False
    if _URL_RE.search(raw) or _CODE_RE.search(raw):
        return False
    try:
        chance = float(_cfg(config, "auto_chance", 0.12))
    except (TypeError, ValueError):
        chance = float(DEFAULTS["auto_chance"])
    if chance <= 0:
        return False
    if not clean_for_speech(raw):
        return False
    if chance >= 1.0:
        return True
    return rng() < chance


# ---------------------------------------------------------------- 自启动
_autostart_tried = False
_autostart_lock: asyncio.Lock | None = None
_last_spawn_at = 0.0
REVIVE_COOLDOWN = 300.0


def _health_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
        return bool(isinstance(data, dict) and data.get("ok"))
    except Exception:
        return False


def _spawn_server(config) -> subprocess.Popen:
    cmd = _cfg(config, "server_cmd", DEFAULTS["server_cmd"])
    if isinstance(cmd, str):
        cmd = [cmd]
    cmd = [str(x) for x in (cmd or []) if str(x).strip()]
    if not cmd:
        raise ValueError("tts.server_cmd 为空")
    log_path = PROJECT_ROOT / "data" / "tts_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as out:
        return subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, **kwargs)


async def ensure_server(config) -> None:
    """tts.autostart 为真且 /health 不通时，分离进程拉起一次 TTS 服务。"""
    global _autostart_tried, _autostart_lock
    try:
        if not bool(_cfg(config, "autostart", True)):
            return
    except Exception:
        return
    if _autostart_tried:
        return
    if _autostart_lock is None:
        _autostart_lock = asyncio.Lock()
    async with _autostart_lock:
        if _autostart_tried:
            return
        health = str(_cfg(config, "health_url", "") or DEFAULTS["health_url"])
        if await asyncio.to_thread(_health_ok, health):
            _autostart_tried = True
            return
        _autostart_tried = True
        await revive(config)


async def revive(config) -> bool:
    """服务挂了就再拉一次（带冷却），供合成失败时兜底。返回是否发起了重启。"""
    global _last_spawn_at
    try:
        if not bool(_cfg(config, "autostart", True)):
            return False
    except Exception:
        return False
    health = str(_cfg(config, "health_url", "") or DEFAULTS["health_url"])
    if await asyncio.to_thread(_health_ok, health):
        return False
    now = time.monotonic()
    if now - _last_spawn_at < REVIVE_COOLDOWN:
        return False
    _last_spawn_at = now
    try:
        proc = await asyncio.to_thread(_spawn_server, config)
    except Exception as e:
        log.warn(f"[tts] 起服务失败：{type(e).__name__} {e}")
        return False
    log.info(f"[tts] 语音服务不在了，重新拉起 pid={getattr(proc, 'pid', '?')}"
             "（约 60 秒后就绪）")
    return True
