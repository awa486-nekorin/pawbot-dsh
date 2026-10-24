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


def _get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _api(params: dict, timeout: int = 15) -> dict:
    params = dict(params)
    params.setdefault("format", "json")
    params.setdefault("utf8", 1)
    url = API + "?" + urllib.parse.urlencode(params)
    return json.loads(_get(url, timeout=timeout))


def search_titles(query: str, limit: int = 5) -> list[dict]:
    """opensearch：返回 [{title, url}, ...]，第一条最接近。"""
    q = (query or "").strip()
    if not q:
        return []
    data = _api({"action": "opensearch", "search": q, "limit": limit})
    # 形如 ["寄", ["寄", ...], ["", ...], ["https://...", ...]]
    if not isinstance(data, list) or len(data) < 4:
        return []
    titles, urls = data[1] or [], data[3] or []
    out = []
    for i, title in enumerate(titles):
        out.append({"title": title,
                    "url": urls[i] if i < len(urls) else
                    SITE + urllib.parse.quote(title.replace(" ", "_"))})
    return out


def intro(title: str, chars: int = 1200) -> dict:
    """条目前言的纯文本（MediaWiki extracts）。"""
    data = _api({"action": "query", "prop": "extracts", "exintro": 1,
                 "explaintext": 1, "redirects": 1, "titles": title})
    pages = ((data or {}).get("query") or {}).get("pages") or {}
    for page in pages.values():
        text = (page.get("extract") or "").strip()
        if text:
            return {"title": page.get("title") or title, "text": text[:chars],
                    "truncated": len(text) > chars}
    return {}


def lookup(query: str, limit: int = 3, chars: int = 1200) -> dict:
    """先找标题再取前言。返回 {ok, query, title, url, extract, others, error}。"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "query": q, "error": "关键词是空的"}
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
                "others": [h["title"] for h in hits[1:]],
                "error": err or "这个条目没有可读的前言"}
    return {"ok": True, "query": q, "title": page["title"],
            "url": first["url"], "extract": page["text"],
            "others": [h["title"] for h in hits[1:]], "error": err}
