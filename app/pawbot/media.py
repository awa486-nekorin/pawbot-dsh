"""回复里的图片协议、离线去重账本与安全解析。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from . import imagesearch
from .log import log

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ROOTS = (PROJECT_ROOT / "data" / "stickers",
                 PROJECT_ROOT / "data" / "image_cache")
MAX_STICKER_BYTES = 5 * 1024 * 1024
MARKER_RE = re.compile(r"\[(IMG|STICKER)\s*[:：]\s*((?:https?://|file:///)[^\]\r\n]+)\]", re.I)
_DEFAULT_LEDGER = None
# 配置缺失或写坏时的表情包节奏：发完下一轮就允许，且不是每轮必发。
DEFAULT_STICKER_GAP = (1, 1)
DEFAULT_STICKER_CHANCE = 0.95
_FALLBACK_CONFIG = None
_FALLBACK_LOADED = False


def _sticker_gap(config) -> tuple[int, int]:
    raw = None
    try:
        raw = config.get("media.sticker_gap") if config is not None else None
    except Exception:
        raw = None
    try:
        lo, hi = int(raw[0]), int(raw[1])
        if lo < 0 or hi < lo:
            raise ValueError
        return lo, hi
    except Exception:
        return DEFAULT_STICKER_GAP


def _sticker_chance(config) -> float:
    try:
        value = float(config.get("media.sticker_chance"))
        if value != value or not 0.0 <= value <= 1.0:  # NaN 或越界都回退
            raise ValueError
        return value
    except Exception:
        return DEFAULT_STICKER_CHANCE


def _fallback_config():
    """没有 bot 时也尽量读 data/config.json，让 media.* 覆盖生效。"""
    global _FALLBACK_CONFIG, _FALLBACK_LOADED
    if not _FALLBACK_LOADED:
        _FALLBACK_LOADED = True
        try:
            from .config import Config
            _FALLBACK_CONFIG = Config(str(PROJECT_ROOT / "data" / "config.json"))
        except Exception:
            _FALLBACK_CONFIG = None
    return _FALLBACK_CONFIG


@dataclass
class MediaRef:
    kind: str
    url: str
    digest: str = ""


def extract(text: str) -> tuple[str, list[MediaRef]]:
    refs = []

    def remove(match):
        refs.append(MediaRef(match[1].lower(), match[2].strip()))
        return ""

    clean = MARKER_RE.sub(remove, text)
    return (clean.strip() if refs else clean), refs


class MediaLedger:
    def __init__(self, path, config=None):
        self.path = Path(path) if path is not None else None
        self.config = config
        self.sessions: dict = {}
        # 同一轮内可能被 tools / resolve / safety 多次询问，掷骰结果要一致，
        # 否则挑好的表情包会在发给模型之后又被判掉。
        self._rolls: dict = {}
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            sessions = data.get("sessions", {})
            if isinstance(sessions, dict):
                for key, state in list(sessions.items())[-500:]:
                    if not isinstance(state, dict):
                        continue
                    self.sessions[str(key)] = {
                        "turn": max(0, int(state.get("turn", 0))),
                        "next_sticker": max(0, int(state.get("next_sticker", 0))),
                        "img": self._entries(state.get("img")),
                        "sticker": self._entries(state.get("sticker")),
                    }
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @staticmethod
    def _entries(entries):
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries[-300:] if isinstance(entry, dict)
                and isinstance(entry.get("turn", 0), int)
                and isinstance(entry.get("url", ""), str)
                and isinstance(entry.get("digest", ""), str)]

    def _session(self, session):
        key = str(session)
        state = self.sessions.pop(key, None)
        if state is None:
            state = {"turn": 0, "next_sticker": 0, "img": [], "sticker": []}
        self.sessions[key] = state
        while len(self.sessions) > 500:
            self.sessions.pop(next(iter(self.sessions)))
        return state

    def _save(self):
        if self.path is None:
            return
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, delete=False) as f:
                tmp = f.name
                json.dump({"sessions": self.sessions}, f, ensure_ascii=False,
                          separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            log.debug("[media] 去重账本暂未落盘")
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _image_window(self) -> int:
        """同一张图隔了多少轮之后可以重发。原来永不过期，导致搜图老是「搜了不發」。"""
        try:
            return max(1, int(self.config.get("media.image_repeat_turns", 30) or 30))
        except (TypeError, ValueError, AttributeError):
            return 30

    def seen_image(self, session, url=None, digest=None, within: int | None = None) -> bool:
        state = self._session(session)
        span = self._image_window() if within is None else max(1, int(within))
        return any(state["turn"] - entry.get("turn", 0) < span
                   and ((url and entry.get("url") == url)
                        or (digest and entry.get("digest") == digest))
                   for entry in state["img"])

    def recent_image_urls(self, session, within: int | None = None) -> set[str]:
        """近期发过的图片 URL，搜图时用来避开重复，换一批新图。"""
        state = self._session(session)
        span = self._image_window() if within is None else max(1, int(within))
        return {str(e.get("url") or "") for e in state["img"]
                if state["turn"] - e.get("turn", 0) < span and e.get("url")}

    def seen_sticker(self, session, url=None, digest=None, within=8) -> bool:
        state = self._session(session)
        return any(state["turn"] - entry.get("turn", 0) < within
                   and ((url and entry.get("url") == url)
                        or (digest and entry.get("digest") == digest))
                   for entry in state["sticker"])

    def remember(self, session, kind, url, digest):
        state = self._session(session)
        entries = state[kind]
        entries.append({"url": url, "digest": digest, "turn": state["turn"]})
        del entries[:-300]
        self._save()

    def turn(self, session) -> int:
        """一次回复实际送达后调用一次，返回新的轮数。"""
        state = self._session(session)
        state["turn"] += 1
        self._save()
        return state["turn"]

    def _sticker_now_allowed(self, key, state) -> bool:
        """间隔到点后再按 media.sticker_chance 掷一次骰，同一轮复用结果。"""
        chance = _sticker_chance(self.config)
        if chance >= 1.0:
            return True
        if chance <= 0.0:
            return False
        cached = self._rolls.get(key)
        if cached and cached[0] == state["turn"]:
            return cached[1]
        allowed = random.random() < chance
        self._rolls[key] = (state["turn"], allowed)
        while len(self._rolls) > 500:
            self._rolls.pop(next(iter(self._rolls)))
        return allowed

    def sticker_allowed(self, session) -> bool:
        """表情包节奏：间隔（media.sticker_gap）到了，再按概率（media.sticker_chance）掷骰。

        默认 [1, 1] + 0.95：发完下一轮就重新有资格，几乎每条回复都能带一张，
        但不是死板地每轮必发。每轮最多一张仍由 safety 层控制。
        """
        state = self._session(session)
        if state["turn"] < int(state.get("next_sticker", 0) or 0):
            return False
        return self._sticker_now_allowed(str(session), state)

    def mark_sticker(self, session):
        state = self._session(session)
        lo, hi = _sticker_gap(self.config)
        state["next_sticker"] = state["turn"] + random.randint(lo, hi)
        self._rolls.pop(str(session), None)
        self._save()


def ledger_for(bot) -> MediaLedger:
    global _DEFAULT_LEDGER
    ledger = getattr(bot, "media_ledger", None)
    if ledger is None:
        root = getattr(bot, "base_dir", None)
        config = getattr(bot, "config", None)
        if root and Path(root).resolve() == PROJECT_ROOT.resolve():
            if _DEFAULT_LEDGER is None:
                _DEFAULT_LEDGER = MediaLedger(PROJECT_ROOT / "data" / "media_ledger.json",
                                              config=config)
            ledger = _DEFAULT_LEDGER
        else:
            ledger = MediaLedger(Path(root) / "data" / "media_ledger.json" if root else None,
                                 config=config)
        bot.media_ledger = ledger
    return ledger


def sticker_allowed(session, *, bot=None, ledger=None) -> bool:
    global _DEFAULT_LEDGER
    if ledger is None and bot is not None:
        ledger = ledger_for(bot)
    if ledger is None:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = MediaLedger(PROJECT_ROOT / "data" / "media_ledger.json",
                                          config=_fallback_config())
        ledger = _DEFAULT_LEDGER
    return ledger.sticker_allowed(session)


def _host(url):
    try:
        return (urlsplit(url).hostname or "local")[:100]
    except ValueError:
        return "invalid-host"


def _warn(ref, reason):
    # 不把带令牌的查询参数、文件路径或协议端异常原文写进日志。
    log.warn(f"[media] {ref.kind} {reason} host={_host(ref.url)}")


def _validate(raw, kind):
    limit = MAX_STICKER_BYTES if kind == "sticker" else imagesearch.MAX_IMAGE_BYTES
    if not raw:
        raise ValueError("empty image")
    if len(raw) > limit:
        raise ValueError("image too large")
    if not (raw.startswith(b"\xff\xd8\xff")
            or raw.startswith(b"\x89PNG\r\n\x1a\n")
            or raw.startswith((b"GIF87a", b"GIF89a"))
            or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")):
        raise ValueError("invalid image")
    imagesearch._sniff_mime(raw)
    return raw


def _local_bytes(url, kind):
    parsed = urlsplit(url)
    if parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("local path denied")
    path = Path(url2pathname(parsed.path)).resolve()
    roots = (Path(root).resolve() for root in ALLOWED_ROOTS)
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("local path denied")
    limit = MAX_STICKER_BYTES if kind == "sticker" else imagesearch.MAX_IMAGE_BYTES
    with path.open("rb") as f:
        return _validate(f.read(limit + 1), kind)


def _load(ref):
    if ref.url.startswith("file:///"):
        return _local_bytes(ref.url, ref.kind)
    if ref.url.startswith("base64://"):
        limit = MAX_STICKER_BYTES if ref.kind == "sticker" else imagesearch.MAX_IMAGE_BYTES
        if len(ref.url) > (limit + 2) // 3 * 4 + 9:
            raise ValueError("image too large")
        return _validate(base64.b64decode(ref.url[9:], validate=True), ref.kind)
    if not ref.url.startswith(("https://", "http://")):
        raise ValueError("unsupported image source")
    parsed = urlsplit(ref.url)
    if not parsed.hostname:
        raise ValueError("invalid image URL")
    return _validate(imagesearch.download({
        "url": ref.url, "need_referer": ref.url.startswith("https://i.pximg.net/"),
        "referer": "https://www.pixiv.net/", "_quiet": True,
    }), ref.kind)


async def resolve(ref: MediaRef, *, session, ledger: MediaLedger) -> dict | None:
    """解析但不记为已发送；成功后 ref.digest 可供发送层提交账本。"""
    seen = ledger.seen_sticker if ref.kind == "sticker" else ledger.seen_image
    if seen(session, url=ref.url):
        log.debug(f"[media] {ref.kind} URL 重复 host={_host(ref.url)}")
        return None
    if ref.kind == "sticker" and not ledger.sticker_allowed(session):
        log.debug("[media] sticker 尚未到发送间隔")
        return None
    reason = "resolve failed"
    for attempt in range(2):
        try:
            raw = await asyncio.to_thread(_load, ref)
            ref.digest = hashlib.sha1(raw).hexdigest()
            if seen(session, digest=ref.digest):
                log.debug(f"[media] {ref.kind} 内容重复 host={_host(ref.url)}")
                return None
            file = await asyncio.to_thread(imagesearch.image_file, raw)
            return {"type": "image", "data": {"file": file}}
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "empty image", "image too large", "invalid image", "local path denied",
                "unsupported image source", "invalid image URL",
            } else type(exc).__name__
            if attempt == 0:
                await asyncio.sleep(0.25)
    _warn(ref, reason)
    return None
