"""
斗图网（doutupk.com）表情包抓取。

搜索页 https://www.doutupk.com/search?keyword=<关键词> 里每张图都是
    <img ... data-original="http://img.doutupk.com/production/uploads/image/....jpg" ...>
（同一张图的 data-backup 是重复项，这里只解析 data-original）。

图片站防盗链，下载必须带 UA 和 Referer，否则 403。
全流程超时 15 秒、任何异常都不外抛，失败一律返回 None / []——
表情包是锦上添花，不能因为它把聊天链路拖死。
"""

from __future__ import annotations

import gzip
import html as html_mod
import re
import urllib.parse
import urllib.request

from .log import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = "https://www.doutupk.com/"
TIMEOUT = 15
MAX_BYTES = 3 * 1024 * 1024
MIN_BYTES = 2 * 1024

IMG_RE = re.compile(r'''data-original\s*=\s*["']([^"']+)["']''', re.I)
EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp)(?:[?#]|$)", re.I)


def _open(url: str, timeout: int = TIMEOUT, limit: int = MAX_BYTES) -> bytes:
    """带表情包站要求的头读一个 URL，最多读 limit+1 字节。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": REFERER,
        "Accept": "image/*,text/html,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(limit + 1)
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw


def _is_image(raw: bytes) -> bool:
    return bool(raw) and (
        raw.startswith(b"\xff\xd8\xff")
        or raw.startswith(b"\x89PNG\r\n\x1a\n")
        or raw.startswith((b"GIF87a", b"GIF89a"))
        or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
    )


def _fetch_page(https_url: str, http_url: str) -> str:
    """先试 https，失败再退回 http（斗图网两套都能开，个别网络只通一边）。"""
    for url in (https_url, http_url):
        try:
            raw = _open(url)
            return raw.decode("utf-8", "replace")
        except Exception as e:
            log.debug(f"[doutu] 打开搜索页失败（{type(e).__name__}）：{url.split('?')[0]}")
    return ""


def search(keyword: str, limit: int = 20) -> list[str]:
    """搜「keyword」，返回页面里去重后的图片 URL（只留 png/jpg/gif/webp）。"""
    kw = str(keyword or "").strip()
    if not kw:
        return []
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        n = 20
    quoted = urllib.parse.quote(kw)
    page = _fetch_page(
        "https://www.doutupk.com/search?keyword=" + quoted,
        "http://www.doutupk.com/search?keyword=" + quoted,
    )
    if not page:
        log.debug(f"[doutu] 搜索「{kw}」没拿到页面")
        return []

    out: list[str] = []
    seen: set[str] = set()
    for m in IMG_RE.finditer(page):
        url = html_mod.unescape(m.group(1).strip()).replace("\\/", "/")
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith(("http://", "https://")):
            continue
        if not EXT_RE.search(url) or url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= n:
            break
    if not out:
        log.debug(f"[doutu] 搜索「{kw}」页面里没有图片")
    return out


def download(url: str, max_bytes: int = MAX_BYTES, timeout: int = TIMEOUT) -> bytes | None:
    """下载一张图并校验魔数/大小。3MB 以上、2KB 以下、非图片都丢弃。"""
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        return None
    try:
        limit = max(MIN_BYTES, int(max_bytes))
    except (TypeError, ValueError):
        limit = MAX_BYTES

    candidates = [u]
    if u.startswith("http://"):
        candidates.insert(0, "https://" + u[len("http://"):])
    for cand in candidates:
        try:
            raw = _open(cand, timeout=timeout, limit=limit)
        except Exception as e:
            log.debug(f"[doutu] 下载失败（{type(e).__name__}）：{cand.split('?')[0]}")
            continue
        if not raw or len(raw) < MIN_BYTES or len(raw) > limit:
            continue
        if not _is_image(raw):
            continue
        return raw
    return None
