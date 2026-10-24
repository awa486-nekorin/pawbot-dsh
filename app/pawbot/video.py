"""视频感知：把 QQ 收到的视频抽帧，走视觉转述链，让大肥鱼能「看」视频。

NapCat 收视频段时 data.file 常见是缓存文件名（xx.mp4），要用 get_file
换绝对路径；少数情况直接给 file:/// 路径。拿到本地文件后用 ffmpeg
均匀抽几帧（走 stdout 管道，不留临时文件），再把帧交给 vision 链转述。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .log import log

# 优先环境变量（装好 ffmpeg 后可设 PAWBOT_FFMPEG），其次 PATH，最后按名字调。
_DEFAULT_FFMPEG = "ffmpeg"
_DEFAULT_FFPROBE = "ffprobe"
_FRAME_W = 640          # 抽帧宽度，够视觉模型看清内容又不至于太重
_FRAME_Q = 4            # mjpeg 质量（1-31，越小越清晰）


def _which(name: str, env: str, fallback: str) -> str:
    import shutil
    cand = os.environ.get(env, "").strip()
    if cand and os.path.isfile(cand):
        return cand
    found = shutil.which(name)
    return found or fallback


def _bin(name: str) -> str:
    if name == "ffmpeg":
        return _which("ffmpeg", "PAWBOT_FFMPEG", _DEFAULT_FFMPEG)
    return _which("ffprobe", "PAWBOT_FFPROBE", _DEFAULT_FFPROBE)


def _probe_millis(path: str) -> int | None:
    """ffprobe 取视频时长（毫秒），失败返回 None。"""
    try:
        out = subprocess.run(
            [_bin("ffprobe"), "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=15)
        dur = float(out.stdout.decode("utf-8", "replace").strip())
        return int(dur * 1000) if dur > 0 else None
    except Exception as e:
        log.debug(f"ffprobe 失败：{type(e).__name__} {e}")
        return None


def _grab_frame(path: str, at_ms: int) -> bytes | None:
    """在 at_ms 这一帧导出 JPEG 字节。"""
    try:
        proc = subprocess.run(
            [_bin("ffmpeg"), "-v", "error", "-y", "-ss", f"{at_ms / 1000:.3f}",
             "-i", path, "-frames:v", "1",
             "-vf", f"scale={_FRAME_W}:-2", "-q:v", str(_FRAME_Q),
             "-f", "image2pipe", "-c:v", "mjpeg", "-"],
            capture_output=True, timeout=20)
        if proc.returncode != 0 or not proc.stdout:
            log.debug(f"抽帧失败 at={at_ms}ms：{proc.stderr.decode('utf-8', 'replace')[-120:]}")
            return None
        return proc.stdout
    except Exception as e:
        log.debug(f"抽帧异常：{type(e).__name__} {e}")
        return None


def extract_frames(path: str, n: int = 3) -> list[bytes]:
    """均匀抽最多 n 帧（1-6），失败自动降级到首帧。返回 JPEG 字节列表。"""
    if not path or not os.path.isfile(path):
        return []
    n = max(1, min(6, int(n or 3)))
    ms = _probe_millis(path)
    out: list[bytes] = []
    if ms is not None and ms > 500:
        step = ms // n
        points = [step * (i + 1) for i in range(n)]
        # 视频开头可能是黑场/字幕，从 15% 开始抽
        points = [max(150, int(p * 0.15)) for p in points] if n > 1 else [ms // 2]
        for t in points:
            frame = _grab_frame(path, t)
            if frame:
                out.append(frame)
            if len(out) >= n:
                break
    if not out:
        first = _grab_frame(path, 0)
        if first:
            out.append(first)
    return out


def local_path(seg: dict, cache_file: str | None = None) -> str | None:
    """从 video 段里解出本地路径。cache_file 是 NapCat get_file 的返回路径。"""
    data = seg.get("data") or {}
    f = str(data.get("file") or "")
    if cache_file:
        cache_file = str(cache_file)
        if cache_file.startswith("file:///"):
            cache_file = cache_file[len("file:///"):]
        if os.path.isfile(cache_file):
            return cache_file
    if f.startswith("file:///"):
        p = f[len("file:///"):]
        return p if os.path.isfile(p) else None
    if f.startswith("base64://"):
        return None
    if os.path.isabs(f) and os.path.isfile(f):
        return f
    # 只剩缓存文件名的情况：调用方负责先用 get_file 换绝对路径
    return None
