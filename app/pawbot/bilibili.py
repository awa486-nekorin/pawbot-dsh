"""
B 站：刷视频、找视频、分享到群里。

接口实测（2026-10-20，全部免登录、免 key）：
  · /x/web-interface/ranking/v2   全站排行榜  ✅
  · /x/web-interface/popular      综合热门    ✅
  · /x/web-interface/search/type  关键词搜索  ⚠️ 有时要 wbi 签名，失败就降级到热门

发到群里的消息用 OneBot 的 `json` 卡片段，QQ 会自动展开成视频卡片。
NapCat 支持 `{"type":"json","data":{"data":"<json字符串>"}}`。
如果卡片发不出去，调用方要能退回「标题 + 链接」纯文本。
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse

from . import websearch
from .log import log

# 每个群上一次分享视频的时间。主动刷视频和工具调用的分享共用这一份，
# 免得「刚主动发过一个」又被叫去发一个。
_shared_at: dict[str, float] = {}


def share_allowed(session_id: str, gap: float) -> bool:
    if gap <= 0:
        return True
    last = _shared_at.get(session_id, 0.0)
    return not last or time.time() - last >= gap


def mark_shared(session_id: str) -> None:
    _shared_at[session_id] = time.time()

API = "https://api.bilibili.com"
HEADERS = {"Referer": "https://www.bilibili.com/",
           "User-Agent": websearch.UA,
           "Accept": "application/json"}


def _get(path: str, params: dict | None = None, timeout: int = 15) -> dict | None:
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        data = json.loads(websearch._open(url, headers=HEADERS, timeout=timeout))
    except Exception as e:
        log.warn(f"B站接口失败 {path}：{type(e).__name__} {e}")
        return None
    if data.get("code") != 0:
        log.warn(f"B站接口 code={data.get('code')} msg={data.get('message')} {path}")
        return None
    return data.get("data") or {}


def _clean_item(v: dict) -> dict | None:
    bvid = v.get("bvid") or ""
    if not bvid:
        # 排行榜老接口可能只给 aid
        aid = v.get("aid")
        bvid = f"av{aid}" if aid else ""
    title = (v.get("title") or "").strip()
    if not bvid or not title:
        return None
    pic = v.get("pic") or ""
    if pic.startswith("//"):
        pic = "https:" + pic
    elif pic.startswith("http://"):
        pic = "https://" + pic[7:]
    owner = v.get("owner") or {}
    stat = v.get("stat") or {}
    return {
        "bvid": bvid,
        "title": title.replace("<em class=\"keyword\">", "").replace("</em>", ""),
        "author": owner.get("name") or v.get("author") or "",
        "url": f"https://www.bilibili.com/video/{bvid}",
        "pic": pic,
        "play": stat.get("view") or v.get("play") or 0,
        "desc": (v.get("desc") or "")[:160],
        "duration": v.get("duration") or 0,
        "tname": v.get("tname") or "",
    }


def ranking(n: int = 10) -> list[dict]:
    """全站排行榜。"""
    d = _get("/x/web-interface/ranking/v2", {"rid": 0, "type": "all"})
    if not d:
        return []
    out = [x for x in (_clean_item(v) for v in (d.get("list") or [])) if x]
    return out[:n]


def popular(n: int = 10) -> list[dict]:
    """综合热门。"""
    d = _get("/x/web-interface/popular", {"ps": min(20, max(1, n)), "pn": 1})
    if not d:
        return []
    return [x for x in (_clean_item(v) for v in (d.get("list") or [])) if x][:n]


def search(keyword: str, n: int = 5) -> list[dict]:
    """关键词搜视频。接口不稳就降级到热门里筛。"""
    d = _get("/x/web-interface/search/type",
             {"search_type": "video", "keyword": keyword, "page": 1}, timeout=20)
    if d:
        raw = d.get("result") or []
        out = [x for x in (_clean_item(v) for v in raw) if x]
        if out:
            return out[:n]

    # 降级：从热门里按关键词粗筛
    log.info(f"B站搜索降级到热门筛选：{keyword}")
    hits = [v for v in popular(50) if keyword.lower() in
            (v["title"] + v["desc"] + v["tname"]).lower()]
    return hits[:n]


def pick_lively(n: int = 1, keyword: str | None = None) -> list[dict]:
    """随机挑几个「有话可聊」的视频。keyword 为空就从热门里随机。

    冷场自救时用：不要排行榜前几名（太像新闻稿），要那种有人气的、好吐槽的。
    """
    pool: list[dict] = []
    if keyword:
        pool = search(keyword, 12) or popular(20)
    else:
        pool = popular(20) or ranking(20)
    if not pool:
        return []
    # 太长的（>1 小时）不推，群里没耐心看
    pool = [v for v in pool if not v.get("duration") or _dur(v["duration"]) <= 3600]
    if not pool:
        return []
    random.shuffle(pool)
    return pool[:n]


def _dur(v) -> int:
    """B站 duration 有 'mm:ss' 和秒数两种格式。"""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v)
    if ":" in s:
        try:
            parts = [int(x) for x in s.split(":")]
            total = 0
            for p in parts:
                total = total * 60 + p
            return total
        except Exception:
            return 0
    try:
        return int(s)
    except Exception:
        return 0


def card_enabled(config=None) -> bool:
    """要不要发 JSON 卡片。**默认不发。**

    新版手机 QQ 对 `com.tencent.structmsg` 卡片经常直接显示
    「发送者版本过低，无法展示该内容」——发出去就是一条坏消息，
    还会把真正有用的纯文本链接挤到后面。
    纯文本 + 链接腾讯自己会展开成缩略卡片，反而稳。

    想强制用卡片：config 里设 tools.bili_json_card = true。
    """
    try:
        return bool(config.get("tools.bili_json_card", False)) if config is not None else False
    except Exception:
        return False


def card(v: dict) -> dict:
    """做成 QQ 的 json 卡片段。新版客户端可能展示不了，见 card_enabled。"""
    payload = {
        "app": "com.tencent.structmsg",
        "desc": "新闻",
        "view": "news",
        "ver": "0.0.0.1",
        "prompt": "[分享] " + v["title"][:40],
        "meta": {
            "news": {
                "title": v["title"][:60],
                "desc": (v.get("author") or "") + (
                    f" · {_fmt_play(v['play'])}播放" if v.get("play") else ""),
                "jumpUrl": v["url"],
                "preview": v.get("pic") or "",
                "tag": "哔哩哔哩",
            }
        },
    }
    return {"type": "json", "data": {
        "data": json.dumps(payload, ensure_ascii=False)}}


def _fmt_play(n) -> str:
    try:
        n = int(n)
    except Exception:
        return "?"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def brief(v: dict) -> str:
    """给模型看的一行摘要。"""
    bits = [v["title"]]
    if v.get("author"):
        bits.append(f"UP：{v['author']}")
    if v.get("play"):
        bits.append(f"{_fmt_play(v['play'])}播放")
    if v.get("tname"):
        bits.append(v["tname"])
    return "｜".join(bits) + f"\n{v['url']}"