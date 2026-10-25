"""
萌娘百科：中文网络黑话、梗、ACG 角色/作品/术语的查询入口。

为什么单独做一个：普通搜索引擎搜「寄」只会给一堆快递，搜「草」给你植物百科。
萌娘百科对这些词的条目是「梗的出处 + 用法 + 演变」，正好是群里接话需要的。

接口（2026-10-21 实测可用）：
  · action=opensearch  给候选标题和链接，建议词也在这
  · action=query&prop=extracts&exintro=1&explaintext=1  给条目前言纯文本
注意 `action=query&list=search` 会被拒（Unauthorized API call），别用。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

API = "https://zh.moegirl.org.cn/api.php"
SITE = "https://zh.moegirl.org.cn/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MAX_RESPONSE_BYTES = 256 * 1024
MAX_TITLE_CHARS = 200
MAX_EXTRACT_CHARS = 2000


def _bounded(value, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _title(value) -> str:
    if not isinstance(value, str) or len(value) > MAX_TITLE_CHARS:
        return ""
    if any(ord(char) < 32 for char in value):
        return ""
    return value.strip()


def _page_url(title: str) -> str:
    # 用经过校验的标题生成本站链接，不信任上游返回的任意 URL。
    return SITE + urllib.parse.quote(title.replace(" ", "_"), safe="")


def _get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("萌娘百科响应超过大小上限")
        return raw.decode("utf-8", "replace")


def _api(params: dict, timeout: int = 15) -> dict:
    params = dict(params)
    params.setdefault("format", "json")
    params.setdefault("utf8", 1)
    url = API + "?" + urllib.parse.urlencode(params)
    return json.loads(_get(url, timeout=timeout))


def search_titles(query: str, limit: int = 5) -> list[dict]:
    """opensearch：返回 [{title, url}, ...]，第一条最接近。"""
    q = _title(query)
    if not q:
        return []
    limit = _bounded(limit, 5, 5)
    data = _api({"action": "opensearch", "search": q, "limit": limit})
    # 形如 ["寄", ["寄", ...], ["", ...], ["https://...", ...]]
    if not isinstance(data, list) or len(data) < 4 or not isinstance(data[1], list):
        return []
    out, seen = [], set()
    for value in data[1][:20]:
        title = _title(value)
        if not title or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        out.append({"title": title, "url": _page_url(title)})
        if len(out) >= limit:
            break
    return out


def intro(title: str, chars: int = 1200) -> dict:
    """条目前言的纯文本（MediaWiki extracts）。"""
    title = _title(title)
    if not title:
        return {}
    chars = _bounded(chars, 1200, MAX_EXTRACT_CHARS)
    data = _api({"action": "query", "prop": "extracts", "exintro": 1,
                 "explaintext": 1, "redirects": 1, "titles": title})
    query = data.get("query") if isinstance(data, dict) else None
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, dict):
        return {}
    for page in list(pages.values())[:5]:
        if not isinstance(page, dict) or "missing" in page or "invalid" in page:
            continue
        raw = page.get("extract")
        canonical = _title(page.get("title")) or title
        text = raw.strip() if isinstance(raw, str) else ""
        if text and not any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            return {"title": canonical, "text": text[:chars],
                    "truncated": len(text) > chars}
    return {}


def lookup(query: str, limit: int = 3, chars: int = 1200, *,
           book=None, allow_network: bool = False) -> dict:
    """默认只查传入的本地库；未命中也不会联网。

    独立同步脚本才可显式传 allow_network=True；普通聊天不能开启。

    远程查询只返回参考文本，不写本地库、不批量同步。返回 origin 区分
    local / moegirl，truncated 标明节选；需保存的资料由调用者审核后导入。
    """
    q = _title(query)
    if not q:
        return {"ok": False, "query": q, "error": "关键词为空或格式不合法"}
    chars = _bounded(chars, 1200, MAX_EXTRACT_CHARS)
    limit = _bounded(limit, 3, 5)
    if book is not None and book.enabled:
        local = book.match(q)[:limit]
        if local:
            item = local[0]
            text = item["meaning"]
            if item.get("usage"):
                text += " 用法：" + item["usage"]
            source = item.get("source", "")
            try:
                parsed = urllib.parse.urlsplit(source)
                url = source if parsed.scheme in ("http", "https") and parsed.netloc else ""
            except ValueError:
                url = ""
            return {"ok": True, "query": q, "title": item["name"],
                    "url": url, "extract": text[:chars], "source": source,
                    "origin": "local", "truncated": len(text) > chars,
                    "others": [it["name"] for it in local[1:]], "error": ""}
    if not allow_network:
        return {"ok": False, "query": q, "origin": "local",
                "error": "本地梗库未命中，未进行网络查询"}
    try:
        hits = search_titles(q, limit=limit)
    except Exception as e:
        return {"ok": False, "query": q, "error": f"{type(e).__name__}: {e}"}
    if not hits:
        return {"ok": False, "query": q, "error": "萌娘百科没有这个条目"}
    first = hits[0]
    try:
        page = intro(first["title"], chars=chars)
    except Exception as e:
        page = {}
        err = f"{type(e).__name__}: {e}"
    else:
        err = ""
    if not page:
        return {"ok": True, "query": q, "title": first["title"],
                "url": first["url"], "extract": "",
                "origin": "moegirl", "truncated": False,
                "others": [h["title"] for h in hits[1:]],
                "error": err or "这个条目没有可读的前言"}
    return {"ok": True, "query": q, "title": page["title"],
            "url": _page_url(page["title"]), "extract": page["text"],
            "origin": "moegirl", "truncated": page["truncated"],
            "others": [h["title"] for h in hits[1:]], "error": err}
