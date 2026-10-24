"""
联网搜索 + 网页正文抓取。

三种搜索后端，按配置选：

  deepseek  ← 用 DeepSeek 自己的 web_search（Responses API，需要 v4-flash）
  bing      ← 本地抓 HTML，不需要 key，国内可直连（默认）
  duckduckgo← 本地抓 HTML，境外网络更稳
  none      ← 关掉

说明：DeepSeek 的联网搜索只在 **Responses API** 上开放，而且只有
`deepseek-v4-flash` 支持、只吃文本、无状态。普通 chat/completions 没有这个能力，
所以要单独发一次请求。
"""

from __future__ import annotations

import base64
import gzip
import html as html_mod
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .log import log
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# ====================================================================== 基础 HTTP
def _open(url: str, *, headers: dict | None = None, data: bytes | None = None,
          timeout: int = 20) -> str:
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "replace")


def _strip_html(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?s)<[^>]+>", " ", fragment)
    fragment = html_mod.unescape(fragment)
    return re.sub(r"\s+", " ", fragment).strip()


# ====================================================================== 官方 Search / Fetch
# DeepSeek 官方的网页搜索与抓取是独立 REST 接口，不经过任何对话模型：
#   POST https://api.deepseek.com/v1/tools/search
#   POST https://api.deepseek.com/v1/tools/fetch
# 结果带 rerank_score（0~3，≥1.5 优质）、authority（0~4）、date。
# 实测 2026-10-21：现有 key 可用，能拿到当天新闻。
def _post_json(url: str, payload: dict, api_key: str, timeout: int = 30) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    text = _open(url, data=body, timeout=timeout, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    return json.loads(text) if text.strip() else {}


def deepseek_search(query: str, api_key: str,
                    base_url: str = "https://api.deepseek.com",
                    count: int = 5, need_content: bool = False,
                    ori_question: str = "", timeout: int = 30) -> list[dict]:
    """DeepSeek 官方 Search。返回统一后的 [{title,url,snippet,...}]。"""
    payload = {"query": query, "count": max(1, min(50, int(count)))}
    if need_content:
        payload["need_content"] = True
    if ori_question:
        payload["ori_question"] = ori_question[:500]
    data = _post_json(base_url.rstrip("/") + "/v1/tools/search",
                      payload, api_key, timeout=timeout)
    out = []
    for item in (data.get("results") or []):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        out.append({
            "title": item.get("title") or item.get("site_name") or item["url"],
            "url": item["url"],
            "snippet": item.get("snippet") or item.get("summary") or "",
            "summary": item.get("summary") or "",
            "content": item.get("content") or "",
            "site": item.get("site_name") or "",
            "date": item.get("date") or "",
            "score": item.get("rerank_score"),
            "authority": item.get("authority"),
        })
    return out


def deepseek_fetch(url: str, api_key: str,
                   base_url: str = "https://api.deepseek.com",
                   timeout: int = 60) -> dict:
    """DeepSeek 官方 Fetch：返回 {ok,title,text,error}。"""
    try:
        data = _post_json(base_url.rstrip("/") + "/v1/tools/fetch",
                          {"url": url}, api_key, timeout=timeout)
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}
    content = (data.get("content") or "").strip()
    if not content:
        return {"ok": False, "url": url, "error": "没取到正文"}
    return {"ok": True, "url": url, "title": data.get("title") or "",
            "text": content, "provider": "deepseek-fetch"}


def fetch_smart(url: str, config) -> dict:
    """先走官方 Fetch，失败再退回本地抓 HTML。"""
    key = _api_key(config)
    if key:
        base = _search_base(config)
        out = deepseek_fetch(url, key, base,
                             timeout=int(config.get("tools.timeout", 90)))
        if out.get("ok"):
            out["text"] = out["text"][:3000]
            return out
        log.debug(f"官方 fetch 失败（{out.get('error')}），改用本地抓取")
    return fetch(url, 3000)


# ====================================================================== X / 推特
# 只借用官方 Search 找链接、官方 Fetch 读正文，不碰 x.com 的登录态。
_X_RESERVED = {
    "search", "home", "explore", "i", "hashtag", "intent", "share", "status",
    "settings", "notifications", "messages", "login", "signup", "logout",
    "compose", "jobs", "tos", "privacy", "download",
}
_X_HOSTS = {"x.com", "www.x.com", "mobile.x.com",
            "twitter.com", "www.twitter.com", "mobile.twitter.com"}


def _api_key(config) -> str:
    return str(config.get("tools.search_api_key", "")
               or config.get("llm.api_key", "")).strip()


def _search_base(config) -> str:
    return str(config.get("tools.search_base_url", "https://api.deepseek.com")
               or "https://api.deepseek.com")


def _is_x_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    return host in _X_HOSTS


def _x_url(url: str) -> str:
    """twitter.com / mobile 版统一成 https://x.com/<path>，并去掉查询串。"""
    u = (url or "").strip()
    try:
        parts = urllib.parse.urlparse(u)
    except Exception:
        return u
    if (parts.hostname or "").lower() not in _X_HOSTS:
        return u
    return "https://x.com" + (parts.path or "").rstrip("/")


def x_search(query: str, config, kind: str = "posts", count: int = 8) -> list[dict]:
    """搜 X 上的帖子或账号。只用官方 Search 的结果，不登录 x.com。"""
    q = (query or "").strip()
    key = _api_key(config)
    count = max(1, int(count))
    if not key or not q:
        return []
    base = _search_base(config)
    timeout = int(config.get("tools.search_timeout", 30))
    per = min(20, max(count * 2, 10))

    def _ds(term: str) -> list[dict]:
        try:
            return deepseek_search(term, key, base, count=per, timeout=timeout)
        except Exception as e:
            log.debug(f"X 搜索失败（{type(e).__name__}）")
            return []

    if str(kind or "").lower() == "people":
        rows = _ds(f"site:x.com {q}")
    else:
        rows = _ds(f"site:x.com {q}") + _ds(f"site:twitter.com {q}")

    out: list[dict] = []
    seen: set[str] = set()
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        url = _x_url(str(r.get("url") or ""))
        if url in seen:
            continue
        if str(kind or "").lower() == "people":
            m = re.match(r"^https://x\.com/([A-Za-z0-9_]+)/?$", url)
            if not m:
                continue
            handle = m.group(1)
            if handle.lower() in _X_RESERVED:
                continue
            seen.add(url)
            out.append({
                "url": url,
                "handle": "@" + handle,
                "name": r.get("title") or "",
                "bio": r.get("summary") or r.get("snippet") or "",
            })
        else:
            m = re.search(r"x\.com/([A-Za-z0-9_]+)/status/(\d+)", url)
            if not m:
                continue
            seen.add(url)
            out.append({
                "url": url,
                "author": "@" + m.group(1),
                "text": r.get("summary") or r.get("snippet") or "",
                "date": r.get("date") or "",
                "title": r.get("title") or "",
            })
        if len(out) >= count:
            break
    return out[:count]


def x_read(url: str, config) -> dict:
    """读一条 X 帖子或某人主页的正文（官方 Fetch，最多 3000 字）。"""
    raw = (url or "").strip()
    if not _is_x_url(raw):
        return {"error": "只能读 x.com 的链接"}
    u = _x_url(raw)
    key = _api_key(config)
    if not key:
        return {"error": "没有 api_key"}
    out = deepseek_fetch(u, key, _search_base(config),
                         timeout=int(config.get("tools.timeout", 90)))
    if not out.get("ok"):
        return {"error": out.get("error") or "没取到正文"}
    text = str(out.get("text") or "")[:3000]
    if not text:
        return {"error": "没取到正文"}
    return {"ok": True, "url": u, "title": out.get("title") or "", "text": text}


# ====================================================================== 搜索
def search_bing(query: str, n: int = 5) -> list[dict]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "count": n + 3, "setlang": "zh-CN"})
    page = _open(url)

    results = []
    for chunk in page.split('class="b_algo"')[1:]:
        chunk = chunk[:8000]
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        link = _unwrap_bing(html_mod.unescape(m.group(1)))
        title = _strip_html(m.group(2))
        snip = ""
        p = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        if p:
            snip = _strip_html(p.group(1))
        if title and link:
            results.append({"title": title, "url": link, "snippet": snip})
        if len(results) >= n:
            break
    return results


def _unwrap_bing(url: str) -> str:
    """Bing 的结果链接是跳转壳，真地址藏在 u=a1<base64url> 里。"""
    if "bing.com/ck/a" not in url:
        return url
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        u = (q.get("u") or [""])[0]
        if u.startswith("a1"):
            u = u[2:]
        u += "=" * (-len(u) % 4)
        return base64.urlsafe_b64decode(u).decode("utf-8", "ignore") or url
    except Exception:
        return url


def search_360(query: str, n: int = 5) -> list[dict]:
    """360 搜索的网页结果。

    实测（2026-10-21）：Bing 的 HTML 已经没有 b_algo 结构、DDG 的 html 端点直接给空壳，
    国内可直连又还能解析的就剩 360。真地址藏在 data-mdurl 里，href 是跳转壳。
    """
    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
    page = _open(url)

    results = []
    for chunk in page.split('class="res-title')[1:]:
        chunk = chunk[:4000]
        open_tag = re.search(r"<a\s[^>]*>", chunk, re.S)
        body = re.search(r"<a\s[^>]*>(.*?)</a>", chunk, re.S)
        if not open_tag or not body:
            continue
        open_tag = open_tag.group(0)
        real = re.search(r'data-mdurl="([^"]+)"', open_tag)
        href = re.search(r'href="([^"]+)"', open_tag)
        link = html_mod.unescape(real.group(1)) if real else (
            html_mod.unescape(href.group(1)) if href else "")
        # 高亮标签是行内标签，去掉时不能留空格，否则「科技新闻」会变成「科技 新闻」
        title = _strip_html(re.sub(r"</?em[^>]*>", "", body.group(1)))
        if not title or not link:
            continue
        snip = ""
        p = re.search(r'<p class="res-desc"[^>]*>(.*?)</p>', chunk, re.S)
        if p:
            snip = _strip_html(re.sub(r"</?em[^>]*>", "", p.group(1)))
        if any(x["title"] == title for x in results):
            continue
        results.append({"title": title, "url": link, "snippet": snip})
        if len(results) >= n:
            break
    return results


def search_local(query: str, n: int = 5) -> list[dict]:
    """不花模型、不调 v4-pro 的本地搜索：360 → Bing → DDG，谁通就用谁。"""
    for name, fn in (("360", search_360), ("bing", search_bing),
                     ("ddg", search_duckduckgo)):
        try:
            out = fn(query, n)
        except Exception as e:
            log.debug(f"本地搜索 {name} 失败：{type(e).__name__}")
            continue
        if out:
            log.debug(f"本地搜索用 {name}，{len(out)} 条")
            return out
    return []


def search_duckduckgo(query: str, n: int = 5) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    page = _open(url)

    results = []
    blocks = re.split(r'class="result__body"', page)
    for chunk in blocks[1:]:
        chunk = chunk[:5000]
        a = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not a:
            continue
        link = html_mod.unescape(a.group(1))
        if "uddg=" in link:                     # DDG 也会套一层跳转
            q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
            link = (q.get("uddg") or [link])[0]
        title = _strip_html(a.group(2))
        snip = ""
        s = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', chunk, re.S)
        if s:
            snip = _strip_html(s.group(1))
        results.append({"title": title, "url": link, "snippet": snip})
        if len(results) >= n:
            break
    return results


def search_deepseek(query: str, api_key: str, base_url: str,
                    model: str = "deepseek-v4-flash", timeout: int = 90,
                    max_output_tokens: int = 800,
                    reasoning_effort: str = "") -> dict:
    """调用 DeepSeek Responses API 的 web_search，让模型自己搜完再回答。

    实测（2026-10-20 / 10-21）：
      · deepseek-v4-pro   → 真的会发 web_search_call，能拿到当天信息（本项目不用）
      · deepseek-v4-flash → 参数照收，0 次搜索、返回空；上层会退回本地抓取
    默认模型是 flash，所以这条路线实际上依赖「空答案 → 本地搜索」的降级。
    """
    payload = {
        "model": model,
        "input": query,
        "tools": [{"type": "web_search"}],
        "max_output_tokens": max_output_tokens,
    }
    # 搜索这一步也走思考型模型；实测这几个字段服务端都收（2026-10-21）
    effort = str(reasoning_effort or "").strip().lower()
    if effort in ("low", "medium", "high"):
        payload["reasoning_effort"] = effort
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    text = _open(base_url.rstrip("/") + "/responses", data=body, timeout=timeout,
                 headers={"Content-Type": "application/json",
                          "Authorization": f"Bearer {api_key}"})
    data = json.loads(text)

    # 优先用便捷字段；没有就遍历 output 里的 output_text
    answer = data.get("output_text") or ""
    if not answer:
        for item in data.get("output", []) or []:
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    answer += c["text"]

    # 数一下它到底搜了几次——0 次说明这个模型没执行搜索
    queries: list[str] = []
    for item in data.get("output", []) or []:
        if item.get("type") == "web_search_call":
            action = item.get("action") or {}
            for q in (action.get("queries") or []):
                if "ws_call_id" not in str(q):
                    queries.append(str(q))

    return {
        "answer": answer.strip(),
        "searched": len(queries),
        "queries": queries,
        "status": data.get("status"),
        "model": data.get("model", model),
    }


# ====================================================================== 抓正文
def fetch(url: str, max_chars: int = 3000) -> dict:
    """抓一个网页，去标签取正文。返回 {title, url, text, ok, error}"""
    try:
        page = _open(url, timeout=25)
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}

    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    title = _strip_html(m.group(1)) if m else ""

    body = page
    b = re.search(r"(?is)<(article|main)[^>]*>(.*?)</\1>", page)
    if b:
        body = b.group(2)

    text = _strip_html(body)
    return {"ok": True, "url": url, "title": title, "text": text[:max_chars],
            "truncated": len(text) > max_chars}


# ====================================================================== 统一入口
def search(query: str, config) -> dict:
    """按配置选后端。返回 {provider, answer, results, error}"""
    provider = str(config.get("tools.search_provider", "bing")).lower()

    if provider == "none":
        return {"provider": "none", "results": [], "error": "搜索已关闭"}

    if provider in ("local", "so360", "sogou"):
        # 不经过大模型，也就不存在 v4-pro 的调用
        try:
            results = search_local(query, 5)
        except Exception as e:
            return {"provider": "local", "results": [], "error": f"{type(e).__name__}: {e}"}
        if not results:
            return {"provider": "local", "results": [],
                    "error": "360/Bing/DDG 都没解析出结果"}
        return {"provider": "local/360", "results": results}

    if provider == "deepseek":
        # 官方 Search 接口：不经过对话模型，也就不存在 v4-pro
        key = str(config.get("tools.search_api_key", "")
                  or config.get("llm.api_key", "")).strip()
        if not key:
            return {"provider": "deepseek", "results": [], "error": "没有 api_key"}
        base = str(config.get("tools.search_base_url", "https://api.deepseek.com"))
        try:
            results = deepseek_search(
                query, key, base, count=int(config.get("tools.search_count", 5)),
                ori_question=str(config.get("tools.search_ori_question", "")),
                timeout=int(config.get("tools.search_timeout", 30)))
        except Exception as e:
            results, err = [], f"{type(e).__name__}: {e}"
        else:
            err = ""
        if results:
            return {"provider": "deepseek/search", "results": results}
        # 接口挂了或没结果 → 本地抓取兜底，别让机器人哑掉
        local = search_local(query)
        return {"provider": "deepseek→local(兜底)", "results": local,
                "error": err or "官方搜索没有结果"}

    if provider == "deepseek_responses":
        key = str(config.get("tools.search_api_key", "")
                  or config.get("llm.api_key", "")).strip()
        if not key:
            return {"provider": "deepseek", "results": [], "error": "没有 api_key"}
        try:
            out = search_deepseek(
                query, key,
                base_url=str(config.get("tools.search_base_url", "https://api.deepseek.com")),
                model=str(config.get("tools.search_model", "deepseek-v4-flash")),
                timeout=int(config.get("tools.timeout", 90)),
                reasoning_effort=str(config.get("tools.search_reasoning_effort", "medium")
                                     or ""),
            )
            if out["answer"]:
                return {"provider": f"deepseek/{out['model']}",
                        "answer": out["answer"],
                        "searched": out["searched"],
                        "results": []}
            # 没答案（或模型没真的搜）→ 退回本地抓取，别让机器人哑掉
            return {"provider": "deepseek→local(空答案降级)",
                    "results": search_local(query),
                    "error": f"模型返回空（web_search 调用 {out['searched']} 次）"}
        except Exception as e:
            try:
                return {"provider": "deepseek→local(异常降级)", "results": search_local(query),
                        "error": f"{type(e).__name__}: {e}"}
            except Exception as e2:
                return {"provider": "deepseek", "results": [], "error": f"{e} / 降级失败 {e2}"}

    try:
        if provider == "duckduckgo":
            return {"provider": "duckduckgo", "results": search_duckduckgo(query)}
        if provider == "bing":
            return {"provider": "bing", "results": search_bing(query)}
        return {"provider": "local", "results": search_local(query)}
    except Exception as e:
        return {"provider": provider, "results": [], "error": f"{type(e).__name__}: {e}"}
