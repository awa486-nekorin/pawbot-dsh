"""
联网找图 —— 全网搜 + Pixiv。

不靠「内置图库」（Openverse / Wikimedia 那种公共图库），
那里面根本没有真人想看的东西。改成真的去搜索引擎和画师站找。

实测（2026-10-20）能用的图源：

  搜狗    https://pic.sogou.com/pics?query=xxx
          ✅ 中文网页覆盖最广，JSON 里直接有**原图** oriPicUrl，
             还带 pic_porn_score / pic_sexy_score 成人评分
  360     https://image.so.com/i?q=xxx
          ✅ 国内老牌图库，图很多
  Wallhaven  api/v1/search   ✅ 壁纸/艺术，带 purity 分级（sfw/nsfw）
  safebooru  ✅ SFW 动漫图库
  Pixiv   https://www.pixiv.net/ajax/search/top/<词>
          ✅ 画师站，中文标签搜得准，带 xRestrict（R-18）标记

Pixiv 的坑：**i.pximg.net 强制 Referer**，没 Referer 直接 403。
所以 Pixiv 的图不能直接把 URL 丢给 QQ（NapCat 拉不到），
必须机器人自己先下载下来，再用 base64 发出去。这在 `to_segment()` 里处理。

搜狗 / 360 / Google / Bing / Yandex / DDG 都实测过：
Google 和 Bing 是 JS 渲染的抓不到，Yandex 返回 1778 字节的验证页，
DDG 要人机挑战。搜狗和 360 是国内能直连的。
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import websearch
from .log import log

UA = websearch.UA
PIXIV_REFERER = "https://www.pixiv.net/"

# 原图常常 5~20MB。以前超过 5MB 就退回 Pixiv 压过的 1200px 版本，画质很差；
# 现在原图完整下载，小的走 base64，大的落到本地文件交给同机的 NapCat 读。
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_INLINE = 3 * 1024 * 1024
IMAGE_CACHE = Path(__file__).resolve().parent.parent / "data" / "image_cache"
IMAGE_CACHE_KEEP = 60


# ====================================================================== 工具
def _clean(u: str) -> str | None:
    u = (u or "").strip().replace("&amp;", "&").replace("\\/", "/")
    u = u.replace("\\u002F", "/")
    if not u.startswith(("http://", "https://")):
        return None
    if len(u) > 1500:
        return None
    return u


def _placeholder(url: str) -> bool:
    low = url.lower()
    return any(x in low for x in (
        "placeholder", "default", "1x1", "spacer", "blank.gif",
        "pixel", "logo", "avatar", "icon", "nosource", "sprite",
    ))


def _unjson(obj: str) -> str:
    """把 JSON 里的转义还原（搜狗用 \\/ 和 \\u002F）。"""
    try:
        return json.loads(f'"{obj}"')
    except Exception:
        return obj.replace("\\/", "/").replace("\\u002F", "/").replace("&amp;", "&")


# ====================================================================== 搜狗
def search_sogou(query: str, n: int = 8, timeout: int = 20) -> list[dict]:
    """搜狗图片。返回的 url 是**原图**，不是缩略图。"""
    url = "https://pic.sogou.com/pics?" + urllib.parse.urlencode(
        {"query": query, "mode": 1, "start": 0})
    try:
        page = websearch._open(url, timeout=timeout)
    except Exception as e:
        log.warn(f"搜狗图片失败：{type(e).__name__} {e}")
        return []

    out: list[dict] = []
    for m in re.finditer(r'"oriPicUrl":"(.*?)"', page):
        u = _clean(_unjson(m.group(1)))
        if not u or _placeholder(u):
            continue
        seg = page[m.start(): m.start() + 2500]
        title = ""
        tm = re.search(r'"title":"(.*?)"', seg)
        if tm:
            title = _unjson(tm.group(1))
        page_url = ""
        pm = re.search(r'"url":"(https?:.*?)"', seg)
        if pm:
            page_url = _unjson(pm.group(1))
        porn = 0.0
        for key in ("pic_porn_score", "pic_sexy_score", "pic_vulgar_score"):
            km = re.search(rf'"{key}":([0-9.]+)', seg)
            if km:
                try:
                    porn = max(porn, float(km.group(1)))
                except Exception:
                    pass
        out.append({
            "url": u,
            "title": title[:100],
            "source": "搜狗",
            "page": page_url,
            "rating": "nsfw" if porn >= 0.35 else "sfw",
            "score": porn,
            "need_referer": False,
        })
        if len(out) >= n:
            break
    return out


# ====================================================================== 360
def search_360(query: str, n: int = 8, timeout: int = 20) -> list[dict]:
    url = "https://image.so.com/i?" + urllib.parse.urlencode({"q": query})
    try:
        page = websearch._open(url, timeout=timeout)
    except Exception as e:
        log.warn(f"360 图片失败：{type(e).__name__} {e}")
        return []

    out: list[dict] = []
    for m in re.finditer(r'"img":"(https?://[^"]+?)"', page):
        u = _clean(_unjson(m.group(1)))
        if not u or _placeholder(u):
            continue
        seg = page[m.start(): m.start() + 1500]
        title = ""
        tm = re.search(r'"title":"(.*?)"', seg)
        if tm:
            title = _unjson(tm.group(1))
        out.append({"url": u, "title": title[:100], "source": "360",
                    "page": "", "rating": "sfw", "score": 0.0,
                    "need_referer": False})
        if len(out) >= n:
            break

    if not out:
        # 退一步：直接把页面里的图片直链扒出来
        for m in re.finditer(r'(https?://[^"\'<> ]{20,300}?\.(?:jpg|jpeg|png|webp))', page):
            u = _clean(m.group(1))
            if not u or _placeholder(u):
                continue
            if "qhimg" not in u and "qhimgs" not in u:
                continue
            out.append({"url": u, "title": "", "source": "360", "page": "",
                        "rating": "sfw", "score": 0.0, "need_referer": False})
            if len(out) >= n:
                break
    return out


# ====================================================================== Wallhaven
def search_wallhaven(query: str, n: int = 8, allow_nsfw: bool = False,
                     timeout: int = 20) -> list[dict]:
    # wallhaven purity: 1=sfw 2=sfw+nsfw 3=nsfw 4=sfw+sfw
    purity = "2" if allow_nsfw else "1"
    url = "https://wallhaven.cc/api/v1/search?" + urllib.parse.urlencode({
        "q": query, "categories": "010", "purity": purity,
        "sort": "toplist", "atleast": "540x400",
    })
    try:
        data = json.loads(websearch._open(url, timeout=timeout))
    except Exception as e:
        log.warn(f"Wallhaven 失败：{type(e).__name__} {e}")
        return []
    out = []
    for it in (data.get("data") or []):
        u = _clean(it.get("path") or "")
        if not u:
            continue
        out.append({"url": u, "title": (it.get("title") or "")[:100],
                    "source": "wallhaven", "page": it.get("short_url") or "",
                    "rating": it.get("purity") or "sfw", "score": 0.0,
                    "need_referer": False})
        if len(out) >= n:
            break
    return out


# ====================================================================== Pixiv
def search_pixiv(query: str, n: int = 8, allow_r18: bool = False,
                 timeout: int = 20) -> list[dict]:
    """Pixiv 搜索。

    用的是 /ajax/search/top/<词>（免登录可用），会返回 illust / manga /
    collection 三块。带 xRestrict 的是 R-18。

    拿到的 url 是 250px 缩略图，这里直接改写成 540px 的大图。
    """
    kw = urllib.parse.quote(query)
    url = f"https://www.pixiv.net/ajax/search/top/{kw}"
    try:
        data = json.loads(websearch._open(
            url, timeout=timeout,
            headers={"Referer": PIXIV_REFERER, "Accept": "application/json"}))
    except Exception as e:
        log.warn(f"Pixiv 搜索失败：{type(e).__name__} {e}")
        return []

    body = data.get("body") or {}
    items: list[dict] = []
    for key in ("illust", "manga"):
        block = body.get(key) or {}
        for it in (block.get("data") or []):
            items.append(it)

    out: list[dict] = []
    for it in items:
        xr = int(it.get("xRestrict") or 0)
        if xr and not allow_r18:
            continue
        raw = it.get("url") or ""
        if not raw:
            continue
        iid = it.get("id")
        out.append({
            "url": _bigger_pixiv(raw),
            "thumb": raw,
            "title": (it.get("title") or "")[:100],
            "source": "pixiv",
            "page": f"https://www.pixiv.net/artworks/{iid}" if iid else "",
            "author": it.get("userName") or "",
            "tags": ", ".join((it.get("tags") or [])[:6]),
            "rating": "r18" if xr else "sfw",
            "score": 1.0 if xr else 0.0,
            "need_referer": True,
            "referer": PIXIV_REFERER,
            "illust_id": iid,
        })
        if len(out) >= n:
            break
    return out


def _bigger_pixiv(url: str) -> str:
    """把 250px 缩略图地址改成 540px 的。"""
    return re.sub(r"/c/\d+x\d+_[a-z0-9_]+/", "/c/540x540_70/", url)


def pixiv_detail(illust_id, timeout: int = 20) -> dict:
    """拿单张作品的信息（作者、原图地址、标签）。"""
    try:
        data = json.loads(websearch._open(
            f"https://www.pixiv.net/ajax/illust/{illust_id}",
            timeout=timeout,
            headers={"Referer": PIXIV_REFERER, "Accept": "application/json"}))
    except Exception as e:
        log.warn(f"Pixiv 详情失败：{type(e).__name__}")
        return {}
    b = data.get("body") or {}
    if not isinstance(b, dict):
        return {}
    urls = b.get("urls") or {}
    tags = b.get("tags") or {}
    tag_list = [t.get("tag") for t in (tags.get("tags") or []) if t.get("tag")]
    # 画质顺序：regular(1200px) > large(540) > original(可能几十MB) > 其它小图
    best = (urls.get("regular") or urls.get("large") or urls.get("original")
            or urls.get("medium") or urls.get("small") or "")
    thumb = urls.get("large") or urls.get("small") or best
    return {
        "title": b.get("title") or b.get("illustTitle") or "",
        "author": b.get("userName") or "",
        "url": best,
        "thumb": thumb,
        "tags": tag_list[:10],
        "xRestrict": b.get("xRestrict", 0),
        "page_count": b.get("pageCount", 1),
        "bookmarks": int(b.get("bookmarkCount") or 0),
        "views": int(b.get("viewCount") or 0),
        "width": int(b.get("width") or 0),
        "height": int(b.get("height") or 0),
    }


# ====================================================================== Pixiv via DeepSeek 搜索
# 专用 Chrome 不再参与搜图：先用官方 Search 找作品页，再用免登录 ajax 取详情。
# 免登录详情拿不到 R18 原图，所以 xRestrict 非 0 的条目直接丢掉。
PIXIV_ARTWORK_RE = re.compile(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)")
PIXIV_HOME_QUERY = "イラスト 10000users入り"
_PIXIV_HEADERS = {"Referer": PIXIV_REFERER, "Accept": "application/json",
                  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}
_AI_TAGS = ("ai生成", "aiイラスト", "ai-art", "aiart", "aiartwork", "生成ai", "ai画")


def _pixiv_json(url: str, timeout: int = 20) -> dict:
    return json.loads(websearch._open(url, timeout=timeout, headers=_PIXIV_HEADERS))


def _ai_tagged(tags) -> bool:
    low = " ".join(str(t) for t in (tags or [])).lower()
    return any(t in low for t in _AI_TAGS)


def _pixiv_search_page(word: str, page: int, timeout: int = 20) -> list[dict]:
    """免登录的作品搜索，按人气排序（popular_d）。一页 60 条。"""
    kw = urllib.parse.quote(word)
    url = (f"https://www.pixiv.net/ajax/search/artworks/{kw}"
           f"?word={kw}&order=popular_d&mode=all&p={page}&s_mode=s_tag&type=all&lang=zh")
    data = _pixiv_json(url, timeout)
    return (((data.get("body") or {}).get("illustManga") or {}).get("data")) or []


def _pixiv_ranking(page: int = 1, timeout: int = 20) -> list[dict]:
    """免登录的日榜，50 条，顺序就是排名。"""
    url = f"https://www.pixiv.net/ranking.php?mode=daily&content=illust&p={page}&format=json"
    data = _pixiv_json(url, timeout)
    return data.get("contents") or []


def _rank_by_popularity(items: list[dict], n: int, thresholds=(10000, 5000, 1000, 500)):
    """按收藏数排序，并且优先用够热门的那一档；不够 n 张就放宽一档。"""
    items.sort(key=lambda x: x.get("bookmarks", 0), reverse=True)
    for floor in thresholds:
        good = [x for x in items if x.get("bookmarks", 0) >= floor]
        if len(good) >= n:
            return good[:n]
    return items[:n]


def _detailed_items(cands: list[dict], config, timeout: int) -> list[dict]:
    """候选 → 取详情（1080 大图 + 收藏数 + 标签），过滤掉 R18 / AI / 详情失败的。"""
    exclude_ai = bool(config.get("tools.pixiv_exclude_ai", True))

    def one(c):
        iid = str(c.get("id") or c.get("illust_id") or "")
        try:
            d = pixiv_detail(iid, timeout)
        except Exception as e:
            log.debug(f"Pixiv 详情异常 {iid}：{type(e).__name__}")
            return None
        if not d or not d.get("url"):
            return None
        try:
            if int(d.get("xRestrict") or 0) != 0:
                return None
        except Exception:
            return None
        tags = d.get("tags") or []
        if exclude_ai and (_ai_tagged(tags) or _ai_tagged(c.get("tags"))):
            return None
        return {
            "url": d["url"],
            "thumb": d.get("thumb") or d["url"],
            "title": d.get("title") or "",
            "author": d.get("author") or "",
            "tags": " ".join(str(t) for t in tags if t),
            "rating": "sfw",
            "source": "pixiv",
            "page": f"https://www.pixiv.net/artworks/{iid}",
            "bookmarks": int(d.get("bookmarks") or 0),
            "width": int(d.get("width") or 0),
            "height": int(d.get("height") or 0),
            "need_referer": True,
            "referer": PIXIV_REFERER,
        }

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            return [x for x in pool.map(one, cands) if x]
    except Exception as e:
        log.debug(f"Pixiv 详情并发失败：{type(e).__name__}")
        return []


def search_pixiv_public(query: str, config, n: int = 8, home: bool = False) -> list[dict]:
    """免登录的 Pixiv 检索：公开搜索（按人气）或日榜。

    以前拿的是 DS 搜索索引里的作品页，候选只有几条、分类排序也乱；
    这里直接问 Pixiv 自己的接口，候选够多，再按收藏数和画幅筛。
    """
    q = (query or "").strip()
    if not q and not home:
        return []
    timeout = int(config.get("tools.image_timeout", 20) or 20)
    limit = max(6, int(config.get("tools.pixiv_candidate_limit", 30) or 30))
    min_side = int(config.get("tools.pixiv_min_side", 1000) or 0)
    exclude_ai = bool(config.get("tools.pixiv_exclude_ai", True))

    cands: list[dict] = []
    seen: set[str] = set()
    if home or not q:
        for page in (1, 2):
            try:
                rows = _pixiv_ranking(page, timeout)
            except Exception as e:
                log.debug(f"Pixiv 排行榜失败：{type(e).__name__}")
                break
            for r in rows:
                iid = str(r.get("illust_id") or "")
                if iid and iid not in seen:
                    seen.add(iid)
                    cands.append({"id": iid})
                if len(cands) >= limit:
                    break
            if len(cands) >= limit:
                break
    else:
        # 免登录的搜索接口会忽略「按人气排序」，只给最新发布。
        # 于是用 Pixiv 自己的「users入り」门槛标签把人气写进查询里：
        # 10000 → 5000 → 1000 → 裸标签，够用了就不放寬。
        tiers = [f"{q} 10000users入り", f"{q} 5000users入り",
                 f"{q} 1000users入り", q]
        for tier in tiers:
            if len(cands) >= limit:
                break
            for page in (1, 2):
                try:
                    rows = _pixiv_search_page(tier, page, timeout)
                except Exception as e:
                    log.debug(f"Pixiv 公开搜索失败（{tier[:20]} 第{page}页）：{type(e).__name__}")
                    break
                for r in rows:
                    iid = str(r.get("id") or "")
                    if not iid or iid in seen:
                        continue
                    try:
                        if int(r.get("xRestrict") or 0) != 0:
                            continue
                    except Exception:
                        continue
                    if min_side and max(int(r.get("width") or 0),
                                        int(r.get("height") or 0)) < min_side:
                        continue
                    if exclude_ai and int(r.get("aiType") or 0) == 2:
                        continue
                    seen.add(iid)
                    cands.append(r)
                    if len(cands) >= limit:
                        break
                if len(cands) >= limit or not rows:
                    break

    if not cands:
        return []
    items = _detailed_items(cands, config, timeout)
    if len(items) < max(4, n * 2):
        # 标签堆太多会被 Pixiv 当成 AND，候选只剩一两条。放宽到前两个核心标签再补一轮，
        # 不够热门档也不坚持 10000users入り。
        tokens = [t for t in q.split() if t]
        if len(tokens) >= 3:
            loose = " ".join(tokens[:2])
            extra: list[dict] = []
            for tier in (f"{loose} 1000users入り", loose):
                try:
                    rows = _pixiv_search_page(tier, 1, timeout)
                except Exception:
                    break
                for r in rows:
                    iid = str(r.get("id") or "")
                    if not iid or iid in seen:
                        continue
                    try:
                        if int(r.get("xRestrict") or 0) != 0:
                            continue
                    except Exception:
                        continue
                    if exclude_ai and int(r.get("aiType") or 0) == 2:
                        continue
                    if min_side and max(int(r.get("width") or 0),
                                        int(r.get("height") or 0)) < min_side:
                        continue
                    seen.add(iid)
                    extra.append(r)
                    if len(extra) >= 8:
                        break
                if len(extra) >= 8:
                    break
            if extra:
                more = _detailed_items(extra, config, timeout)
                if more:
                    log.info(f"Pixiv 标签放宽到「{loose}」补了 {len(more)} 张候选")
                    items.extend(more)
    return _rank_by_popularity(items, n)


def search_pixiv_ds(query: str, config, n: int = 8) -> list[dict]:
    """用 DeepSeek 官方搜索找 pixiv 作品页，再取免登录详情。"""
    q = (query or "").strip()
    n = max(1, min(8, int(n)))
    key = str(config.get("tools.search_api_key", "")
              or config.get("llm.api_key", "")).strip()
    if not key or not q:
        return []
    base = str(config.get("tools.search_base_url", "https://api.deepseek.com")
               or "https://api.deepseek.com")
    try:
        results = websearch.deepseek_search(
            f"site:pixiv.net/artworks {q}", key, base,
            count=min(20, n * 3),
            timeout=int(config.get("tools.search_timeout", 30)))
    except Exception as e:
        log.debug(f"Pixiv DS 搜索失败：{type(e).__name__}")
        return []

    ids: list[str] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        m = PIXIV_ARTWORK_RE.search(str(r.get("url") or ""))
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    if not ids:
        return []
    items = _detailed_items([{"id": iid} for iid in ids], config,
                            int(config.get("tools.image_timeout", 20) or 20))
    return _rank_by_popularity(items, n)



# ====================================================================== Pixiv 登录
# Pixiv 的免登录 ajax 搜索只给 SFW，R-18 必须登录。
# 拿到 refresh_token 就能走 app-api，R-18 和原图都能拿。
#
# 怎么拿 refresh_token（一次就够）：
#   1. 浏览器登录 pixiv.net
#   2. F12 → Application → Cookies → 找到 __refresh_token（可能没有就看
#      https://www.pixiv.net/refresh_token 这个页面的内容）
#   3. 填到配置 tools.pixiv_refresh_token
#
# 注意：这是**你自己的账号**，token 等于账号权限，别提交到 git。
# .gitignore 已经把 data/config.json 排除了。
_PIXIV_CLIENT = {
    "client_id": "MOBILE_BRAND_APP",
    "client_secret": "28c1fdd170a152438b9089acbf3b16d45d8e8cf2",
    "get_source": "MOBILE",
}
_PIXIV_APP_HEADERS = {
    "App-OS": "ios",
    "App-OS-Version": "16.6",
    "App-Version": "7.16.0",
    "Accept": "application/json",
}


def pixiv_access_token(refresh_token: str, timeout: int = 20) -> str | None:
    """用 refresh_token 换一个 access_token。失败返回 None。"""
    if not refresh_token:
        return None
    data = dict(_PIXIV_CLIENT)
    data["grant_type"] = "refresh_token"
    data["refresh_token"] = refresh_token
    body = urllib.parse.urlencode(data).encode()
    try:
        req = urllib.request.Request(
            "https://oauth.secure.pixiv.net/auth/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode())
    except Exception as e:
        log.warn(f"Pixiv 登录失败：{type(e).__name__} {e}")
        return None
    return res.get("access_token") or None


def search_pixiv_authed(query: str, n: int = 8, refresh_token: str = "",
                        allow_r18: bool = False, timeout: int = 25) -> list[dict]:
    """登录态搜 Pixiv——能拿到 R-18 和原图。"""
    token = pixiv_access_token(refresh_token, timeout)
    if not token:
        return []

    url = "https://app-api.pixiv.net/v1/search/illust?" + urllib.parse.urlencode({
        "word": query,
        "filter": "for_ios",
        "search_target": "partial_match_for_tags",
        "sort": "date_desc",
        "illust_type": "illust",
        "page_size": max(1, min(60, n * 2)),
        "oauth_token": token,
    })
    headers = dict(_PIXIV_APP_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    try:
        data = json.loads(websearch._open(url, headers=headers, timeout=timeout))
    except Exception as e:
        log.warn(f"Pixiv 登录搜索失败：{type(e).__name__} {e}")
        return []

    out: list[dict] = []
    for it in (data.get("illusts") or []):
        xr = int(it.get("x_restrict") or 0)
        if xr and not allow_r18:
            continue
        urls = it.get("image_urls") or {}
        u = _clean(urls.get("original") or urls.get("large") or urls.get("medium"))
        if not u:
            continue
        user = it.get("user") or {}
        tags = [t.get("name") for t in (it.get("tags") or []) if t.get("name")]
        out.append({
            "url": u,
            "thumb": urls.get("medium") or u,
            "title": (it.get("title") or "")[:100],
            "source": "pixiv",
            "page": f"https://www.pixiv.net/artworks/{it.get('id')}",
            "author": user.get("name") or "",
            "tags": ", ".join(tags[:6]),
            "rating": "r18" if xr else "sfw",
            "score": 1.0 if xr else 0.0,
            "need_referer": True,
            "referer": PIXIV_REFERER,
            "illust_id": it.get("id"),
        })
        if len(out) >= n:
            break
    return out


# ====================================================================== safebooru
def search_safebooru(query: str, n: int = 8, timeout: int = 20) -> list[dict]:
    url = "https://safebooru.org/index.php?" + urllib.parse.urlencode({
        "page": "dapi", "s": "post", "q": "index", "tags": query,
        "limit": max(1, n), "json": "1"})
    try:
        data = json.loads(websearch._open(url, timeout=timeout))
    except Exception as e:
        log.warn(f"safebooru 失败：{type(e).__name__} {e}")
        return []
    posts = data if isinstance(data, list) else (data.get("posts") or [])
    out = []
    for p in posts[:n]:
        u = _clean(p.get("file_url") or p.get("sample_url") or "")
        if not u:
            continue
        out.append({"url": u, "title": (p.get("tags") or "")[:100],
                    "source": "safebooru", "page": p.get("file_url") or "",
                    "rating": "sfw", "score": 0.0, "need_referer": False})
    return out


# ====================================================================== 内置图库（默认不用）
def openverse(query: str, n: int = 5, timeout: int = 15) -> list[dict]:
    """公共图库。默认不启用（用户明确说不要内置图源），留着当备胎。"""
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode({
        "q": query, "page_size": max(1, min(20, n * 2)),
        "mature": "true", "license_type": "all"})
    try:
        data = json.loads(websearch._open(url, timeout=timeout))
    except Exception:
        return []
    out = []
    for it in (data.get("results") or []):
        u = _clean(it.get("url"))
        if not u or _placeholder(u):
            continue
        out.append({"url": u, "title": (it.get("title") or "")[:100],
                    "source": "openverse", "page": it.get("foreign_landing_url") or "",
                    "rating": "sfw", "score": 0.0, "need_referer": False})
        if len(out) >= n:
            break
    return out


def wikimedia(query: str, n: int = 5, timeout: int = 15) -> list[dict]:
    url = ("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}", "gsrlimit": max(1, min(20, n * 2)),
        "gsrnamespace": 6, "prop": "imageinfo", "iiprop": "url|size",
        "iiurlwidth": 960}))
    try:
        data = json.loads(websearch._open(url, timeout=timeout))
    except Exception:
        return []
    pages = (data.get("query") or {}).get("pages") or {}
    out = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 999)):
        info = (p.get("imageinfo") or [{}])[0]
        u = _clean(info.get("thumburl") or info.get("url"))
        if not u or _placeholder(u):
            continue
        out.append({"url": u, "title": (p.get("title") or "").replace("File:", "")[:100],
                    "source": "wikimedia", "page": info.get("descriptionurl") or "",
                    "rating": "sfw", "score": 0.0, "need_referer": False})
        if len(out) >= n:
            break
    return out


# ====================================================================== 自定义图源
def custom_search(endpoint: str, query: str, n: int = 5,
                  timeout: int = 20) -> list[dict]:
    """通用外部图源。

    endpoint 里用 {query} 占位。返回体两种都认：
      · JSON 数组，或含 urls / images / data / results 字段的对象，元素里找 url / image / src
      · 纯文本，每行一个图片 URL
    """
    url = endpoint.replace("{query}", urllib.parse.quote(query))
    if "{query}" not in endpoint:
        sep = "&" if "?" in endpoint else "?"
        url = endpoint + sep + urllib.parse.urlencode({"q": query})

    body = websearch._open(url, timeout=timeout)
    out: list[dict] = []
    try:
        data = json.loads(body)
    except Exception:
        for line in body.splitlines():
            u = _clean(line.strip())
            if u and not _placeholder(u):
                out.append({"url": u, "title": "", "source": "custom", "page": "",
                            "rating": "sfw", "score": 0.0, "need_referer": False})
            if len(out) >= n:
                break
        return out

    seq: list = []
    if isinstance(data, list):
        seq = data
    elif isinstance(data, dict):
        for k in ("urls", "images", "data", "results", "list", "items"):
            v = data.get(k)
            if isinstance(v, list):
                seq = v
                break
    for it in seq:
        if isinstance(it, str):
            u = _clean(it)
            title, page = "", ""
        elif isinstance(it, dict):
            u = _clean(it.get("url") or it.get("image") or it.get("src")
                       or it.get("original") or it.get("link") or "")
            title = str(it.get("title") or it.get("title_en") or "")[:100]
            page = str(it.get("page") or it.get("source") or "")
        else:
            continue
        if u and not _placeholder(u):
            out.append({"url": u, "title": title, "source": "custom", "page": page,
                        "rating": "sfw", "score": 0.0, "need_referer": False})
        if len(out) >= n:
            break
    return out


# ====================================================================== 下载
_DL_CACHE: "dict[str, tuple[float, bytes]]" = {}
_DL_CACHE_TTL = 600
_DL_CACHE_BYTES = 160 * 1024 * 1024


def _cache_get(url: str) -> bytes | None:
    hit = _DL_CACHE.get(url)
    if hit and time.time() - hit[0] < _DL_CACHE_TTL:
        return hit[1]
    _DL_CACHE.pop(url, None)
    return None


def _cache_put(url: str, raw: bytes):
    _DL_CACHE[url] = (time.time(), raw)
    total = sum(len(v[1]) for v in _DL_CACHE.values())
    for key in sorted(_DL_CACHE, key=lambda k: _DL_CACHE[k][0]):
        if total <= _DL_CACHE_BYTES and len(_DL_CACHE) <= 24:
            break
        total -= len(_DL_CACHE.pop(key)[1])


def download(item: dict, timeout: int = 25) -> bytes | None:
    """下载完整图片；超出上限则尝试作品明确提供的下一个尺寸，不截断。

    item["_quiet"] 为真时不打每次尝试的警告，交给调用方统一报一次。
    """
    quiet = bool(item.get("_quiet"))
    urls = list(dict.fromkeys([item.get("url"), *item.get("url_candidates", [])]))
    for url in urls:
        if url and (cached := _cache_get(str(url))):
            return cached
    headers = {"User-Agent": UA, "Accept": "image/*,*/*"}
    if item.get("need_referer") and item.get("referer"):
        headers["Referer"] = item["referer"]
    for url in urls:
        if not url or not str(url).startswith(("https://", "http://")):
            continue
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read(MAX_IMAGE_BYTES + 1)
            if len(raw) > MAX_IMAGE_BYTES:
                if not quiet:
                    log.warn("图片超过下载上限，尝试作品提供的其他尺寸")
                continue
            if not raw or not (raw.startswith(b"\xff\xd8\xff")
                               or raw.startswith(b"\x89PNG\r\n\x1a\n")
                               or raw.startswith((b"GIF87a", b"GIF89a"))
                               or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")):
                continue
            _cache_put(str(url), raw)
            return raw
        except Exception as e:
            if not quiet:
                log.warn(f"下载图片失败：{type(e).__name__}")
    return None


def image_file(raw: bytes) -> str:
    """把图片交给 NapCat：小图内联 base64，大图写进本地缓存后给 file:// 路径。"""
    if len(raw) <= MAX_INLINE:
        return "base64://" + base64.b64encode(raw).decode()
    ext = {"image/png": ".png", "image/gif": ".gif",
           "image/webp": ".webp"}.get(_sniff_mime(raw), ".jpg")
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    path = IMAGE_CACHE / (hashlib.sha1(raw).hexdigest()[:20] + ext)
    if not path.exists():
        path.write_bytes(raw)
    try:
        old = sorted(IMAGE_CACHE.glob("*"), key=lambda f: f.stat().st_mtime)
        for f in old[:-IMAGE_CACHE_KEEP]:
            f.unlink(missing_ok=True)
    except OSError:
        pass
    return path.resolve().as_uri()


# ====================================================================== 打码
def _sniff_mime(raw: bytes) -> str:
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# 「明确要 R18」的措辞。注意：「色图」「涩图」「黄图」**不算**——
# 那几个是默认档（16+/17+ 擦边），主人明确要求过：
# 平时群友说「找些色图」就给不露点但很色的，只有明说要 R18 才给 R18。
_EXPLICIT_WORDS = (
    "r18", "r-18", "r 18", "18+", "18＋", "18禁", "十八禁", "十八+",
    "无修正", "無修正", "成人向", "成年向", "成人内容",
    "露点", "露點", "全裸", "裸体", "裸體", "露胸", "露乳", "露下",
    "生殖器", "性器", "淫", "肉番",
)


# 否定：关键词前面 4 个字内出现否定字，就不算「要」
# 「没」不算否定：「有没有 18+ 的」是问句，是真的在要。
_NEG_CHARS = "不别別无無非勿休"


def wants_explicit(text: str) -> bool:
    """这句里有没有**明确**要 R18。

    「不露点」「不要 R18」「别搞 R18」这类否定不算——
    关键词前面 4 个字内出现否定字，就跳过这一次命中。
    """
    low = (text or "").lower()
    for w in _EXPLICIT_WORDS:
        for m in re.finditer(re.escape(w), low):
            left = low[max(0, m.start() - 4):m.start()]
            if not any(c in left for c in _NEG_CHARS):
                return True
    return False


def is_ecchi_request(text: str) -> bool:
    """这句在要「擦边图」吗（包含色图/涩图这类默认档说法）。"""
    low = (text or "").lower()
    return any(w in low for w in (
        "色图", "涩图", "色圖", "澀圖", "黄图", "黃圖", "图", "圖",
        "找点好康", "好康", "sexy", "擦边", "擦邊", "性感",
    ))


def _needs_censor(item: dict) -> bool:
    """带成人标记的结果（搜狗评分 / Pixiv xRestrict / wallhaven purity）。"""
    return str(item.get("rating", "")).lower() in ("r18", "nsfw", "sketchy", "3", "4")


def _local_req(url: str, method: str = "GET", timeout: float = 5.0):
    """本机 CDP HTTP 请求。127.0.0.1 必须绕开系统代理，否则被接管回 502。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method=method)
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# 打码核心：先**定位**露点在哪，再盖。
#
# 之前是固定盖 30%~70% 的高度——那是瞎猜。
# 实测三张 R-18 图，露点全都在 50% 以下（甚至 50%~100% 整段），
# 固定黑带正好盖在中间的空白区，上下反而全露着。
#
# 现在改成：按行统计肤色像素，找出露点所在的横带，再盖。
# 肤色判定针对**动画肤色**（高饱和暖色）调过，能跳过白底和蓝紫色头发：
#   r>195 && g>140 && b>110 && 6<(r-g)<70 && (r-b)>12 && (g-b)<70
# 白色背景 r-g=0 → 排除；蓝发 r<g → 排除；紫衣 r<b → 排除。
#
# 脸也是肤色，所以最上面那一段（脸）会被跳过——但只在「它确实像脸」时跳：
# 位于图像上半部且高度不超过 45%。否则宁可多盖。
#
# mode:
#   bar   在 R18 部位画一条局部黑带（默认）
#   crop  裁到黑带上沿，把 R18 部位连同下方切掉
#   blur  在 R18 部位做去色马赛克
#
# 定位思路：只统计水平中段，找「肤色最密的一行」当锚点，
# 向上下扩到密度下降为止（只取紧邻这一块，不去够远处的腿），
# 高度封顶 26%——所以是一条局部小条，不是一大片。
#
# 返回 {dataUrl, detected, residual}
#   detected  检出几段露点
#   residual  打码后**仍然露着**的肤色行数（>0 说明没盖干净 → 宁可不发）
_CENSOR_EVAL = r"""
(async () => {
  const img = new Image();
  await new Promise((res, rej) => { img.onload = res;
    img.onerror = () => rej(new Error('decode failed')); img.src = %s; });
  const w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) throw new Error('empty image');
  const mode = %s;

  const isSkin = (r, g, b) =>
    r > 195 && g > 140 && b > 110 &&
    (r - g) > 6 && (r - g) < 70 &&
    (r - b) > 12 && (g - b) < 70;

  const SW = 160;
  const SH = Math.max(1, Math.round(h * SW / w));
  const sc = document.createElement('canvas');
  sc.width = SW; sc.height = SH;
  const sctx = sc.getContext('2d', { willReadFrequently: true });
  sctx.drawImage(img, 0, 0, SW, SH);
  const sd = sctx.getImageData(0, 0, SW, SH).data;

  const rows = new Array(SH).fill(0);
  const L = new Array(SH).fill(SW), R = new Array(SH).fill(0);
  for (let y = 0; y < SH; y++) {
    let n = 0, l = SW, r = 0;
    for (let x = 0; x < SW; x++) {
      const i = (y * SW + x) * 4;
      if (isSkin(sd[i], sd[i+1], sd[i+2])) {
        n++;
        if (x < l) l = x;
        if (x > r) r = x;
      }
    }
    rows[y] = n; L[y] = l; R[y] = r;
  }

  // boxIn 是视觉模型给的垂直范围 [top, bottom]（0~1）。有就用它。
  const boxIn = %s;
  let a, b, found = 0;
  if (boxIn) {
    a = Math.max(0, Math.min(SH - 2, Math.round(boxIn[0] * SH)));
    b = Math.min(SH - 1, Math.max(a + 1, Math.round(boxIn[1] * SH)));
    found = 1;
  } else {
    // 没视觉结果 → 保守：从「脸以下」的肤色区一路盖到底。
    //
    // 为什么不画窄条：实测有的图整片胸部都在画面里（甚至占满上半幅），
    // 一条 22 percent 的窄条根本盖不住。看不清的时候宁可盖多，不能漏。
    const SKIP = Math.round(SH * 0.42);
    let sum = 0, acc = 0;
    for (let y = SKIP; y < SH; y++) { sum += rows[y]; acc += rows[y] * y; }
    found = sum > (SH - SKIP) * 1.5 ? 1 : 0;
    a = SKIP;
    if (found) {
      // 从 42 percent 往下找第一段真正有肤色的行
      const thr2 = Math.max(3, Math.round(SW * 0.06));
      for (let y = SKIP; y < SH; y++) {
        if (rows[y] >= thr2) { a = Math.max(0, y - 1); break; }
      }
    } else {
      a = Math.round(SH * 0.6);      // 完全没检到 → 从六成处盖到底
    }
    b = SH - 1;
    found = 0;
  }

  // 水平范围：这条里肤色像素的实际左右跨度（不铺满整幅宽度）
  let l = SW, r = 0;
  for (let y = a; y <= b; y++) {
    if (rows[y] >= 2) { if (L[y] < l) l = L[y]; if (R[y] > r) r = R[y]; }
  }
  if (r <= l) { l = Math.round(SW * 0.30); r = Math.round(SW * 0.70); }
  const grow = Math.max(3, Math.round(SW * 0.05));
  l = Math.max(0, l - grow);
  r = Math.min(SW - 1, r + grow);
  // 宽度至少 45 percent、最多 92 percent，免得只剩一条细缝
  const minW = Math.round(SW * 0.45), maxW = Math.round(SW * 0.92);
  let bw = r - l + 1;
  if (bw < minW) {
    const mid = (l + r) / 2;
    l = Math.max(0, Math.round(mid - minW / 2));
    r = Math.min(SW - 1, l + minW - 1);
    bw = r - l + 1;
  }
  if (bw > maxW) {
    const mid = (l + r) / 2;
    l = Math.max(0, Math.round(mid - maxW / 2));
    r = Math.min(SW - 1, l + maxW - 1);
  }

  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0);

  const sx = w / SW, sy = h / SH;
  const bx0 = Math.round(l * sx), bx1 = Math.round((r + 1) * sx);
  const by0 = Math.round(a * sy), by1 = Math.round((b + 1) * sy);

  // crop：裁到黑条上沿，保留上面的画面
  if (mode === 'crop') {
    const cut = Math.max(Math.round(h * 0.2), by0);
    const out = document.createElement('canvas');
    out.width = w; out.height = Math.max(1, cut);
    out.getContext('2d').drawImage(img, 0, 0, w, cut, 0, 0, w, cut);
    return JSON.stringify({ dataUrl: out.toDataURL('image/jpeg', 0.92),
                            detected: found ? 1 : 0, residual: 0,
                            box: [bx0, by0, bx1, by1], cut: cut });
  }

  if (mode === 'blur') {
    const t = document.createElement('canvas');
    t.width = bx1 - bx0; t.height = Math.max(1, by1 - by0);
    t.getContext('2d').drawImage(img, bx0, by0, t.width, t.height, 0, 0, t.width, t.height);
    const tmp = document.createElement('canvas');
    const blk = 10;
    tmp.width = blk; tmp.height = Math.max(1, Math.round(blk * t.height / t.width));
    tmp.getContext('2d').drawImage(t, 0, 0, tmp.width, tmp.height);
    ctx.save();
    ctx.imageSmoothingEnabled = false;
    ctx.filter = 'grayscale(1) brightness(0.65)';
    ctx.drawImage(tmp, 0, 0, tmp.width, tmp.height, bx0, by0, bx1 - bx0, by1 - by0);
    ctx.restore();
  } else {
    ctx.fillStyle = '#000';
    ctx.fillRect(bx0, by0, bx1 - bx0, by1 - by0);
  }

  // 验证：黑条范围内还采得到肤色就算没盖住
  const octx = c.getContext('2d', { willReadFrequently: true });
  const od = octx.getImageData(0, 0, w, h).data;
  let residual = 0;
  for (let y = by0; y < by1; y += 2) {
    for (let x = bx0; x < bx1; x += 2) {
      const i = (y * w + x) * 4;
      if (isSkin(od[i], od[i+1], od[i+2])) residual++;
    }
  }

  return JSON.stringify({ dataUrl: c.toDataURL('image/jpeg', 0.92),
                          detected: found ? 1 : 0, residual: residual,
                          box: [bx0, by0, bx1, by1] });
})()
"""



async def _censor_via_chrome(port: int, path: str, data_url: str,
                             mode: str, box=None) -> tuple[bytes, dict]:
    from .pixiv_session import _CDP, _connect
    conn = await _connect("127.0.0.1", port, path)
    try:
        cdp = _CDP(conn)
        try:
            await cdp.call("Runtime.enable")
        except Exception:
            pass
        # mode 传字符串（"bar"/"crop"/"blur"），JS 里直接字符串比较
        expr = _CENSOR_EVAL % (json.dumps(data_url), json.dumps(mode),
                               json.dumps(list(box) if box else None))
        r = await cdp.call("Runtime.evaluate",
                           {"expression": expr, "awaitPromise": True,
                            "returnByValue": True}, timeout=40)
        out = json.loads((r.get("result") or {}).get("value") or "")
        _, _, b64 = str(out.get("dataUrl", "")).partition(",")
        raw = base64.b64decode(b64)
        if not raw:
            raise RuntimeError("canvas 输出为空")
        return raw, out
    finally:
        try:
            await conn.close()
        except Exception:
            pass


_VISION_PROMPT = """看这张插画，找出画面里**必须打码**的所有部位：
裸露的乳头/乳房、生殖器、以及任何明显性器官特写。

输出一个能**包住全部这些部位的最外范围**，用 0 到 1 的比例表示垂直位置
（0=图片顶部，1=图片底部）。

只输出 JSON：
{"top": 0.00, "bottom": 0.00, "what": "chest,genitalia"}

要点：
- **有多处时取最外的范围**：从最上面那处的顶部，到最下面那处的底部
- 例如胸在上、生殖器在下 → top 取胸的上沿，bottom 取生殖器的下沿
- 范围要刚好包住这些部位，不要顺带框进大头空白
- 脸、头发、手臂、腿不算
- 确实没有裸露部位才输出 {"top":0,"bottom":0,"what":"none"}"""


def censor_box_via_vision(raw: bytes, llm) -> tuple[float, float] | None:
    """让视觉模型看一眼，给出需要遮挡的垂直范围（0~1）。

    实测（deepseek-flash）：
      · 有的图很准（chest 69%~94%，两次一致）
      · 有的图直接返回空（内容被拒）
      · 有的图判断「没有裸露部位」
    所以只把**可信的结果**当准，其余交给启发式兜底。
    """
    if llm is None or not raw:
        return None
    try:
        data = base64.b64encode(raw).decode()
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": _VISION_PROMPT},
            {"type": "image_url",
             "image_url": {"url": f"data:{_sniff_mime(raw)};base64,{data}"}},
        ]}]
        text = asyncio.run(llm.chat(msgs)) or ""
    except Exception as e:
        log.warn(f"视觉定位失败：{type(e).__name__} {e}")
        return None
    m = re.search(r"\{[^}]*\}", text)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        top = float(d.get("top", 0))
        bot = float(d.get("bottom", 0))
    except Exception:
        return None
    # 只接受「合理」的范围：非空、高度 5%~70%、在画面内。
    # 模型说「没有裸露部位」也返回 None —— 这张图是 R-18 标记过的，
    # 宁可走保守兜底，不信它的「没有」。
    if not (0 <= top < bot <= 1):
        return None
    if bot - top < 0.05 or bot - top > 0.70:
        return None
    log.info(f"视觉定位：{top:.2f}~{bot:.2f}（{d.get('what')}）")
    return top, bot


def censor_image(raw: bytes, mode: str = "bar",
                 box: tuple[float, float] | None = None) -> tuple[bytes, str] | None:
    """给成人图打码。

    mode:
      bar   在**检测到的**露点横带上盖黑带（默认）
      crop  裁掉第一段露点以上的部分
      blur  在露点横带上做马赛克

    借 PawBot 专用 Chrome 的 canvas 重编码——纯标准库解不了 JPEG。
    返回 (jpeg_bytes, mime)；浏览器不可用或**没盖干净**时返回 None（宁可不发）。
    """
    from .pixiv_session import PixivSession
    sess = PixivSession()
    if not raw:
        return None
    if not sess.chrome_cdp_ready():
        log.warn("专用 Chrome 不可用，无法给成人图打码")
        return None
    mode = mode if mode in ("bar", "crop", "blur") else "bar"
    data_url = f"data:{_sniff_mime(raw)};base64," + base64.b64encode(raw).decode()
    tab = None
    try:
        # 临时开一个空白页跑 canvas（pixiv 页面有 CSP，data: 图会被拦），用完即关
        tab = _local_req(f"http://{sess.host}:{sess.port}/json/new?about:blank",
                         method="PUT")
        path = "/" + tab["webSocketDebuggerUrl"].split("/", 3)[3]
        try:
            out, info = asyncio.run(
                _censor_via_chrome(sess.port, path, data_url, mode, box))
        finally:
            try:
                _local_req(f"http://{sess.host}:{sess.port}/json/close/{tab.get('id', '')}",
                           timeout=3)
            except Exception:
                pass
    except Exception as e:
        log.warn(f"NSFW 打码失败：{type(e).__name__} {e}")
        return None
    # 没盖干净 = 露点那几段还采得到肤色，这张不能发
    residual = int(info.get("residual") or 0)
    if residual > 0:
        log.warn(f"NSFW 打码没盖干净（残留 {residual} 行），宁可不发")
        return None
    detected = int(info.get("detected") or 0)
    box = info.get("box")
    if detected == 0:
        log.info("NSFW 打码：下半身没找到肤色，用立绘常识位置兜底")
    log.info(f"NSFW 图已打码（{mode}，位置 {box}）")
    return out, "image/jpeg"


_RUNTIME_CFG: dict = {}


def config_nsfw_vision() -> bool:
    """视觉定位开关（tools.nsfw_vision，默认开）。"""
    return bool(_RUNTIME_CFG.get("nsfw_vision", True))


def to_segment(item: dict, censor: str = "bar", llm=None) -> dict | None:
    """把结果变成 OneBot 的 image 段。

    需要 Referer 的（Pixiv）必须自己下载再转 base64——
    直接把 URL 给 QQ，NapCat 拉不到，会 403。
    成人图（R-18 / nsfw）一律先打码再发；打码失败宁可不发。
    """
    if censor != "off" and _needs_censor(item):
        raw = download(item)
        # 先让视觉模型看一眼定位，拿到就按它画；拿不到走启发式
        box = None
        if raw and llm is not None:
            try:
                if config_nsfw_vision():
                    box = censor_box_via_vision(raw, llm)
            except Exception:
                box = None
        censored = censor_image(raw, censor, box) if raw else None
        if not censored:
            log.warn("成人图未能打码，这张不发")
            return None
        data, _mime = censored
        return {"type": "image",
                "data": {"file": "base64://" + base64.b64encode(data).decode()}}
    if not item.get("need_referer"):
        return {"type": "image", "data": {"file": item["url"]}}
    raw = download(item)
    if not raw:
        return None
    return {"type": "image", "data": {"file": image_file(raw)}}



def _search_via_browser(query: str, n: int, allow_r18: bool) -> list[dict]:
    """Borrow PawBot's dedicated Chrome session; skip non-owned CDP endpoints."""
    from . import pixiv_session
    sess = pixiv_session.PixivSession()
    if not sess.ensure_chrome():
        return []
    try:
        # 这段跑在线程池里（没有现成的事件循环），必须用 asyncio.run 新建一个
        return asyncio.run(sess.search(query, n, allow_r18))
    except Exception as e:
        log.warn(f"浏览器会话搜图失败：{type(e).__name__} {e}")
        return []



# 「高人氣」搜法（主人教的，实测有效）：
# 在標籤後面**空一格加數字**（1000 / 10000 / 000 / 0000），
# 因為 `s_mode=s_tag` 是**標籤部分匹配**，`1000` 會命中
# `1000users入り` 這類標籤（Pixiv 上作品火了會被讀者打上這種「收藏數門檻」標籤）。
#
# 實測（`/ajax/search/artworks/`，收藏數取自 `/ajax/illust/<id>`）：
#   `猫`        → 0~165 收藏（全是剛發的新圖）
#   `猫 1000`   → 1008~4150 收藏  ✅ 12279 條
#   `猫 10000`  → 10000~27518 收藏 ✅ 1318 條
#
# **端點很關鍵**：必須用 `/ajax/search/artworks/`（作品搜尋）。
# 用 `/ajax/search/top/`（聚合搜尋）語義不同，同一個
# `猫 1000users入り` 前者回 10848 條、後者只回 4 條。
POPULAR_TAGS = ("100", "300", "500", "1000", "5000", "10000", "50000", "100000")


def _popular_suffix(popular: str) -> str:
    """把配置值規整成可用的數字；off/空 → 空串。"""
    pv = str(popular or "").strip()
    if not pv or pv.lower() in ("off", "0", "none", "false"):
        return ""
    digits = pv.strip("0") or "0"
    if pv in POPULAR_TAGS or digits in POPULAR_TAGS:
        return pv
    return "1000"          # 不認識的門檻就當 1000


def pixiv_search_url(query: str, allow_r18: bool,
                     popular: str = "") -> str:
    """Pixiv 作品搜尋的 URL。

    mode=all|safe 管分級（mode=r18 是「只要 R-18」，不是「允許」，所以允許時用 all）；
    s_mode=s_tag 是標籤部分匹配 —— 高人氣數字就是靠它起作用的。

    popular: 傳 "1000" 就在關鍵詞後面空一格加 1000，收窄到高人氣作品。
    """
    word = query
    pv = _popular_suffix(popular)
    if pv:
        word = f"{query} {pv}"
    kw = urllib.parse.quote(word)
    mode = "all" if allow_r18 else "safe"
    return (f"https://www.pixiv.net/ajax/search/artworks/{kw}"
            f"?word={kw}&order=date_d&mode={mode}&p=1&s_mode=s_tag&type=all")


def search_pixiv_sessid(query: str, n: int = 8, sessid: str = "",
                        allow_r18: bool = False, timeout: int = 20) -> list[dict]:
    """直接带 SESSID cookie 搜 Pixiv。

    这是最省事的一条路：不用重启浏览器、不用调试端口，
    主人从自己的浏览器里把 SESSID 的值复制出来就行。
    （SESSID 是 HttpOnly，但 DevTools → Application → Cookies 里能看到）
    """
    if not sessid:
        return []
    kw = urllib.parse.quote(query)
    url = pixiv_search_url(query, allow_r18)
    try:
        data = json.loads(websearch._open(
            url, timeout=timeout,
            headers={"Cookie": f"SESSID={sessid}",
                     "Referer": PIXIV_REFERER,
                     "Accept": "application/json"}))
    except Exception as e:
        log.warn(f"Pixiv(SESSID) 搜索失败：{type(e).__name__} {e}")
        return []

    body = data.get("body") or {}
    items: list[dict] = []
    for key in ("illust", "manga"):
        items += ((body.get(key) or {}).get("data") or [])

    out: list[dict] = []
    for it in items:
        xr = int(it.get("xRestrict") or 0)
        if xr and not allow_r18:
            continue
        raw = it.get("url") or ""
        if not raw:
            continue
        iid = it.get("id")
        out.append({
            "url": _bigger_pixiv(raw),
            "thumb": raw,
            "title": (it.get("title") or "")[:100],
            "source": "pixiv",
            "page": f"https://www.pixiv.net/artworks/{iid}" if iid else "",
            "author": it.get("userName") or "",
            "tags": ", ".join((it.get("tags") or [])[:6]),
            "rating": "r18" if xr else "sfw",
            "score": 1.0 if xr else 0.0,
            "need_referer": True,
            "referer": PIXIV_REFERER,
            "illust_id": iid,
        })
        if len(out) >= n:
            break
    return out


# ====================================================================== 底线
# 未成年与校园 / 幼态指向词。福瑞是另一条独立排除规则，不混进这个词表。
_MINOR_TERMS = (
    "未成年", "未成年人", "未满18", "未满十八", "18岁以下", "十八岁以下",
    "低龄", "年幼", "儿童", "孩童", "小孩", "小孩子", "幼童", "幼儿",
    "幼女", "幼男", "幼态", "幼小", "童颜", "萝莉", "萝利", "正太",
    "小学生", "初中生", "中学生", "高中生", "高校生", "女子高生", "男子高生",
    "学生妹", "学生弟", "校服", "学生服", "学生制服", "校园", "幼儿园", "幼稚园",
    "minor", "underage", "child", "children", "kid", "kids",
    "loli", "lolita", "shota", "little girl", "little boy",
    "young girl", "young boy", "schoolgirl", "schoolboy", "school uniform",
    "elementary school", "middle school", "high school",
    "ロリ", "ショタ", "こども", "子供", "幼児",
)


def mentions_minor(text: str) -> bool:
    t = (text or "").casefold()
    return any(term in t for term in _MINOR_TERMS)


def mentions_furry(text: str) -> bool:
    """福瑞是独立的用户排除项，不与未成年人分类混用。"""
    t = (text or "").casefold()
    return bool(re.search(r"福[瑞睿]|兽人|獸人|兽设|獸設|ケモノ|獣人|獣化|"
                          r"(?<![a-z])(?:furry|furries|kemono|anthro)(?![a-z])", t))


def _allowed(item: dict, allow_r18: bool) -> bool:
    """福瑞与未成年相关结果始终排除，其他非 SFW 结果受 R18 开关限制。"""
    metadata = f"{item.get('tags', '')} {item.get('title', '')}"
    if mentions_furry(metadata) or mentions_minor(metadata):
        return False
    if item.get("rating") == "sfw":
        return True
    return allow_r18


# ====================================================================== 画质评判
_JUDGE_PROMPT = (
    "你在给一张插画缩略图打美术质量分（1-10）。看构图、线条、上色、完成度、人体比例和画面干净程度。"
    "粗糙草稿、低清糊图、AI 崩坏（手指/五官错乱、细节融成一团）、梗图截图、纯文字、拼贴、广告都给低分；"
    "画得好、好看、愿意发给朋友看的给高分。只输出 JSON：{\"score\": 整数, \"reason\": \"不超过10个字\"}")


def _vision_judge_sync(raw: bytes, config, timeout: float) -> tuple[int, str] | None:
    """画质评判走看图通道（llm.vision_*），不走 judge.*。

    judge 通道现在可能是纯文本模型（Mercury / DeepSeek），塞图片进去必然失败，
    表现就是「画质评判没给出结果 → 按收藏数排序」，搜出来的图跟着变难看。
    """
    try:
        from . import vision
        key = str(config.get("llm.vision_api_key", "")
                  or config.get("llm.api_key", "")).strip()
        base = str(config.get("llm.vision_base_url", "")
                   or config.get("llm.base_url", ""))
        model = str(config.get("llm.vision_model", "") or config.get("llm.model", ""))
        effort = str(config.get("llm.vision_reasoning_effort", "") or "low")
    except Exception:
        return None
    if not key or not base or not model:
        return None
    try:
        shrunk = vision._shrink(raw)
        tiny = shrunk[0] if shrunk else raw
        b64 = base64.b64encode(tiny).decode("ascii")
        text = vision._call_vision(b64, key, base, model, _JUDGE_PROMPT,
                                   timeout=int(max(5, timeout)), budget=300,
                                   effort=effort, retries=0)
    except Exception as e:
        log.debug(f"画质评判调用失败：{type(e).__name__}")
        return None
    from .judge import _first_json
    data = _first_json(text)
    try:
        score = int(data["score"])
    except Exception:
        return None
    return max(1, min(10, score)), str((data or {}).get("reason") or "")[:20]


async def judge_images(items: list[dict], config) -> list[dict]:
    """用看图模型给缩略图打分，丢掉低于 tools.image_judge_min 的。

    评判不可用或全部失败时原样返回（按元数据排序），不因为评判故障而没图可发。
    每项写回 judge_score / judge_reason，供日志和排序。
    """
    if not items or not config.get("tools.image_judge", True):
        return items
    floor = float(config.get("tools.image_judge_min", 6))
    timeout = float(config.get("tools.image_judge_timeout", 15))

    sem = asyncio.Semaphore(3)

    async def rate(item):
        thumb = item.get("thumb") or item.get("url")
        if not thumb:
            return None
        raw = await asyncio.to_thread(download, {
            "url": thumb, "need_referer": str(thumb).startswith("https://i.pximg.net/"),
            "referer": PIXIV_REFERER}, 15)
        if not raw:
            return None
        async with sem:
            return await asyncio.to_thread(_vision_judge_sync, raw, config, timeout)

    results = await asyncio.gather(*(rate(x) for x in items), return_exceptions=True)
    rated = [(x, r) for x, r in zip(items, results) if isinstance(r, tuple)]
    if not rated:
        log.debug("画质评判没给出结果，按收藏/尺寸排序")
        return items
    # 整批都低于门槛时别把结果清空：挑最好的两张兜底，宁可次一点也别「搜了不發」
    if all(r[0] < floor for _, r in rated):
        best = max(r[0] for _, r in rated)
        if best >= 4:
            fallback = sorted(rated, key=lambda p: p[1][0], reverse=True)[:2]
            for x, r in fallback:
                x["judge_score"], x["judge_reason"] = r
            log.info(f"画质整体偏低（最高 {best}），兜底发 {len(fallback)} 张，不空手")
            return [x for x, _ in fallback]
        log.info(f"画质评判全部低于门槛（最高 {best}），这轮不硬发")
        return []
    kept = []
    for item, result in zip(items, results):
        if isinstance(result, tuple):
            item["judge_score"], item["judge_reason"] = result
            if result[0] < floor:
                log.info(f"画质评判淘汰 {item.get('page') or item.get('url')}：{result[0]} {result[1]}")
                continue
        kept.append(item)
    kept.sort(key=lambda x: x.get("judge_score", floor), reverse=True)
    return kept


# ====================================================================== 统一入口
def _drop_seen(images: list[dict], avoid: set | None, n: int) -> list[dict]:
    """把最近发过的图剔掉；全被剔掉时退回原列表，别一条都不给。"""
    if not avoid:
        return images[:n]
    def same(x):
        for u in [x.get("url"), *(x.get("url_candidates") or [])]:
            if isinstance(u, str) and u in avoid:
                return True
        return False
    kept = [x for x in images if not same(x)]
    return (kept or images)[:n]


def image_search(query: str, config, n: int = 5, source: str = "auto",
                 spice: int = 0, allow_r18: bool | None = None,
                 avoid: set | None = None) -> dict:
    """找图。

    source: auto（先全网再 Pixiv） / web（只搜搜索引擎） / pixiv（只搜 Pixiv）
    spice: 当前群尺度，0=只要 SFW，1+=允许 R18
    """
    query = (query or "").strip()
    src = (source or "auto").lower()
    if not query and src in ("auto", "pixiv", "home"):
        src = "home"
    if not query and src != "home":
        return {"provider": "-", "images": [], "error": "关键词是空的"}
    if mentions_furry(query):
        return {"provider": "-", "images": [], "error": "查询包含已排除的福瑞标签"}

    # allow_r18 由调用方明确给；没给就按档位推断（兼容旧调用）
    if allow_r18 is None:
        allow_r18 = int(spice) >= 1
    allow_r18 = bool(allow_r18) and int(spice) >= 1   # 0 档永远不给 R18

    minor_query = mentions_minor(query)
    # 未成年 + 性化请求：不搜。这条不是「降级」，也不受配置放宽影响。
    if minor_query and (wants_explicit(query) or is_ecchi_request(query)):
        return {"provider": "-", "images": [],
                "error": "未成年或幼态角色与性化内容不能一起搜索"}

    # tools.block_minor=true（默认）直接拒绝并说明原因；显式关闭时也只降级为 SFW。
    if minor_query and config.get("tools.block_minor", True):
        return {"provider": "-", "images": [],
                "error": "查询涉及未成年或幼态角色，已按 tools.block_minor 拒绝搜图"}
    if minor_query:
        allow_r18 = False
        log.info("查询涉及未成年相关说法，已强制降级为 SFW")

    log.info(f"找图分级：{'允许 R18' if allow_r18 else '只要 SFW（擦边自然会有）'}")
    n = max(1, min(8, int(n)))
    if src in ("pixiv", "home"):
        from . import pixiv_session
        # 多拿几张给评判挑，评判再淘汰丑图（候选太少就只能盲选）
        pool = min(12, max(n + 4, n * 3))
        if not config.get("tools.pixiv_use_browser", True):
            # 免登录路线：Pixiv 自己的检索（按人气 / 日榜）→ 详情取 1080 大图。
            # 拿不到才退回 DS 搜作品页（索引薄，只当兜底）。
            q = query
            if src == "home":
                q = str(config.get("tools.pixiv_home_query", PIXIV_HOME_QUERY)
                        or "").strip() or PIXIV_HOME_QUERY
            imgs = search_pixiv_public(q, config, pool, home=(src == "home"))
            if not imgs:
                imgs = search_pixiv_ds(q, config, pool)
            images = [x for x in imgs if _allowed(x, allow_r18)]
            # 只评判收藏最高的几张：评判是逐张调视觉模型，全部评判会拖到 40 秒以上
            judged = asyncio.run(judge_images(images[:max(4, n)], config))
            if not imgs:
                err = "Pixiv 没搜到作品（检索无结果或作品详情取不到）"
            elif not judged:
                err = "候选都被画质评判淘汰了，换个标签或回主页再找"
            elif allow_r18:
                err = "Pixiv 免登录只给全年齡作品；R18 没拿到"
            else:
                err = ""
            marks = [x.get("bookmarks", 0) for x in judged[:3]]
            log.info(f"Pixiv 找图（公开）：候选 {len(imgs)} → 通过 {len(judged)}"
                     f"{'，收藏 ' + '/'.join(str(m) for m in marks) if marks else ''}")
            return {"provider": "pixiv(公开)", "images": _drop_seen(judged, avoid, n), "error": err}
        sess = pixiv_session.PixivSession()
        common = dict(min_bookmarks=int(config.get("tools.pixiv_min_bookmarks", 500)),
                      min_side=int(config.get("tools.pixiv_min_side", 1000)),
                      exclude_ai=bool(config.get("tools.pixiv_exclude_ai", True)), variety=True)

        async def run():
            if not sess.chrome_cdp_ready():
                # 專用 Chrome 被關掉時自己拉起來一次，別讓搜圖整個失效
                if config.get("tools.pixiv_auto_launch", True) and sess.launch_chrome():
                    for _ in range(20):
                        await asyncio.sleep(0.5)
                        if sess.chrome_cdp_ready():
                            break
                    log.info("Pixiv 专用 Chrome 已自动启动")
                if not sess.chrome_cdp_ready():
                    return {"provider": "pixiv(浏览器)", "images": [],
                            "error": "专用 Chrome 没在运行，自动启动也没成功；请运行 /pixiv登录 检查"}
            if src == "home":
                report = await sess.home_report(
                    pool, allow_r18, source=str(config.get("tools.pixiv_home_source", "auto")),
                    top=int(config.get("tools.pixiv_home_top", 20)), **common)
            else:
                report = await sess.search_report(
                    query, n=pool, allow_r18=allow_r18,
                    popular=str(config.get("tools.pixiv_popular", "10000,5000,1000") or ""),
                    candidate_limit=int(config.get("tools.pixiv_candidate_limit", 30)), **common)
            images = [x for x in report.get("images", []) if _allowed(x, allow_r18)]
            judged = await judge_images(images[:max(4, n)], config)
            if images and not judged:
                report["error"] = "候选都被画质评判淘汰了，换个标签或回主页再找"
            report["images"] = _drop_seen(judged, avoid, n)
            return report

        try:
            return asyncio.run(run())
        except Exception as e:
            return {"provider": "pixiv(浏览器)", "images": [],
                    "error": f"专用会话搜索异常：{type(e).__name__}，请检查 /pixiv登录 状态"}

    engines: list[tuple[str, object]] = []
    if src in ("auto", "web"):
        engines.append(("搜狗", lambda: search_sogou(query, n)))
        engines.append(("360", lambda: search_360(query, n)))
        engines.append(("wallhaven", lambda: search_wallhaven(query, n, allow_r18)))
    if src in ("auto", "pixiv"):
        if not config.get("tools.pixiv_use_browser", True):
            engines.append(("pixiv(DS)", lambda: search_pixiv_ds(query, config, n)))
        sid = str(config.get("tools.pixiv_sessid", "") or "").strip()
        if sid:
            engines.append(("pixiv(SESSID)", lambda: search_pixiv_sessid(
                query, n, sid, allow_r18)))
        rt = str(config.get("tools.pixiv_refresh_token", "") or "").strip()
        if rt:
            # 登录态优先：能拿 R-18 和原图
            engines.append(("pixiv(登录)", lambda: search_pixiv_authed(
                query, n, rt, allow_r18)))
        # 借 PawBot 专用 Chrome 会话（CDP）；未运行或不是专用配置就跳过。
        if config.get("tools.pixiv_use_browser", True):
            engines.append(("pixiv(浏览器)", lambda: _search_via_browser(
                query, n, allow_r18)))
        if not config.get("tools.pixiv_use_browser", True):
            engines.append(("pixiv(公开)", lambda: search_pixiv_public(
                query, config, n)))
        engines.append(("pixiv", lambda: search_pixiv(query, n, allow_r18)))
    if src == "booru":
        engines.append(("safebooru", lambda: search_safebooru(query, n)))

    if config.get("tools.image_stock_fallback", False):
        engines.append(("openverse", lambda: openverse(query, n)))
        engines.append(("wikimedia", lambda: wikimedia(query, n)))

    custom_ep = str(config.get("tools.image_custom_endpoint", "") or "").strip()
    if custom_ep:
        engines.append(("custom", lambda: custom_search(custom_ep, query, n)))

    errors: list[str] = []
    for name, fn in engines:
        try:
            imgs = fn()
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
            log.warn(f"图源 {name} 失败：{type(e).__name__} {e}")
            continue
        # 过滤后这个图源没剩下的，就换下一个图源，不能把被筛掉的原样发出去
        imgs = [x for x in (imgs or []) if _allowed(x, allow_r18)]
        if imgs:
            log.info(f"图源 {name} 命中「{query}」→ {len(imgs)} 张")
            return {"provider": name, "images": _drop_seen(imgs, avoid, n),
                    "error": "; ".join(errors)}

    return {"provider": "无", "images": [],
            "error": "; ".join(errors) or "所有图源都没结果"}
