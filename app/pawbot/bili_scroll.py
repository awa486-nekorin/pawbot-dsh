"""刷 B 站（整合自 @jojoman2024/dsh-bili-scroll 的「每日一轮 + 视频笔记」）。

它跟 `bili_watch.py` 的分工：
    bili_watch.py   只管**读**别人写好的报告（DSH 那半边在看）。
    bili_scroll.py  自己就能刷：搜/热门 → 去重 → **读字幕原文** → 判断值不值得
                    → 每条写一份 md 笔记。字幕走 `bili_mcp`（bilibili-api + SESSDATA），
                    这是原来纯 HTTP 拿不到的东西。

产物：
    data/bili_notes/<标题>-<YYMMDD>.md   一份一条，只写视频里真有的
    data/bili_scroll_seen.json           判重表（看过就记，别重复占预算）

硬约束（跟上游插件一致）：
    · 字幕没讲的**不许脑补**，拿不准就写「视频里没细说」
    · 一个 B 站号别刷太狠：每轮条数有上限，搜索之间会歇一下
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from datetime import datetime, timedelta

from .log import log

_默认 = {
    "enabled": True,
    "topic": "科技 数码 生活 冷知识",
    "per_round": 3,
    "max_chars": 12000,
    "min_play": 5000,
    "max_duration": 3600,
    "notes_dir": "data/bili_notes",
    "times": [],            # 每天几点刷一轮，如 ["12:40", "20:00"]；空 = 不定时
    "report_to": "owner",   # owner / group / off
    "report_at": "off",     # 发到群里时 @ 谁：off / owner / all
    "report_comment": False,  # 贴完清单后再用她自己的语气补一句
}

_搜过的词: dict[str, float] = {}


# ---------------------------------------------------------------- 配置 / 路径
def _cfg(bot, key, default=None):
    return bot.config.get(f"tools.bili_scroll_{key}", _默认.get(key, default))


def _base(bot) -> str:
    return getattr(bot, "base_dir", ".") or "."


def _notes_dir(bot) -> str:
    d = str(_cfg(bot, "notes_dir", _默认["notes_dir"]) or "")
    return d if os.path.isabs(d) else os.path.join(_base(bot), d)


def _seen_path(bot) -> str:
    return os.path.join(_base(bot), "data", "bili_scroll_seen.json")


def _load_seen(bot) -> dict:
    try:
        with open(_seen_path(bot), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_seen(bot, seen: dict) -> None:
    path = _seen_path(bot)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warn(f"B站判重表写入失败：{e}")


# ---------------------------------------------------------------- 凭证 / 接口
def _srv():
    """延迟导入：没装 bilibili-api 时不影响 PawBot 启动。"""
    from .bili_mcp import mcp_server as srv
    return srv


def _ensure_credential(bot) -> bool:
    """把 SESSDATA 同步到 bili_mcp 的凭证文件。没有就返回 False（还能刷，只是没字幕）。"""
    target = os.path.join(os.path.dirname(os.path.abspath(_srv().__file__)),
                          "bili_credential.json")
    sd = ""
    for key in ("tools.bili_sessdata", "bili.sessdata"):
        try:
            sd = str(bot.config.get(key, "") or "").strip()
        except Exception:
            sd = ""
        if sd:
            break
    if not sd:
        # 退到 bili-watch 共享目录里的那份（同一个人贴的 SESSDATA）
        root = ""
        try:
            root = str(bot.config.get("tools.bili_watch_dir", "") or "")
        except Exception:
            root = ""
        if not root:
            root = os.path.join(_base(bot), "pawbot-bili-watch-data")
        for cand in (os.path.join(root, "credential.json"),):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    sd = str((json.load(f) or {}).get("sessdata") or "").strip()
            except Exception:
                sd = ""
            if sd:
                break
    if not sd:
        return False
    try:
        with open(target, "r", encoding="utf-8") as f:
            cur = json.load(f)
        if str(cur.get("sessdata") or "") == sd:
            return True
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sessdata": sd, "bili_jct": "", "buvid3": "", "dedeuserid": ""}, f)
        os.replace(tmp, target)
        log.info("B站凭证已同步到 bili_mcp")
    except OSError as e:
        log.warn(f"B站凭证写入失败：{e}")
        return False
    return True


def _dur(text) -> int:
    s = str(text or "")
    if ":" in s:
        try:
            total = 0
            for part in s.split(":"):
                total = total * 60 + int(part)
            return total
        except Exception:
            return 0
    try:
        return int(float(s))
    except Exception:
        return 0


async def _search(srv, keyword: str, num: int = 12) -> list[dict]:
    """同一个词 10 分钟内不重复打（B 站会 412）。"""
    上次 = _搜过的词.get(keyword, 0)
    if time.time() - 上次 < 600:
        return []
    _搜过的词[keyword] = time.time()
    try:
        raw = await srv.bili_search(keyword=keyword, num=int(num))
        data = json.loads(raw)
    except Exception as e:
        log.warn(f"B站搜索失败（{keyword}）：{type(e).__name__} {e}")
        return []
    out = []
    for v in data.get("videos") or []:
        bvid = str(v.get("bvid") or "")
        if not bvid:
            continue
        out.append({
            "bvid": bvid,
            "title": str(v.get("title") or "").strip(),
            "author": str(v.get("author") or "").strip(),
            "play": int(v.get("play") or 0),
            "duration": _dur(v.get("duration")),
            "desc": str(v.get("description") or "").strip(),
            "url": f"https://www.bilibili.com/video/{bvid}",
        })
    return out


async def _subtitle(srv, bvid: str) -> str:
    try:
        data = json.loads(await srv.bili_subtitle(bvid=bvid))
    except Exception as e:
        log.debug(f"字幕失败 {bvid}：{type(e).__name__}")
        return ""
    return str(data.get("text") or "").strip()


async def _comments(srv, bvid: str, num: int = 8) -> list[dict]:
    try:
        data = json.loads(await srv.bili_comments(bvid=bvid, num=int(num)))
    except Exception:
        return []
    out = []
    for c in data.get("comments") or []:
        out.append({"user": str(c.get("user") or ""),
                    "content": str(c.get("content") or ""),
                    "like": int(c.get("like") or 0)})
    return out


# ---------------------------------------------------------------- 判断 / 写作
_提示头 = """你是「大肥鱼」，一个在 QQ 群里跟人聊天的 AI 群友。现在你刷到一个 B 站视频，
要判断值不值得看，并给值得的写一份笔记（给别人看，别人没看视频也能明白重点）。

铁规则：只能写下面材料里真有的东西。字幕没讲到的，就写「视频里没细说」，绝不脑补。

只输出 JSON（不要解释、不要代码块）：
{"worth": true 或 false, "gist": "一两句说清这条在讲什么",
 "points": ["3~6 条关键点：方法/数字/结论/反直觉之处"],
 "why": "为什么值得看，两三句，给谁看、能拿走什么",
 "take": "你自己的一句话吐槽或评价，可以尖刻"}

材料如下：
"""


async def _judge(bot, item: dict, sub: str) -> dict | None:
    sub_text = sub[: int(_cfg(bot, "max_chars", 12000) or 12000)]
    prompt = (_提示头
              + f"【标题】{item['title']}\n"
              + f"【UP】{item['author']}\n"
              + f"【播放】{item['play']}\n"
              + f"【简介】{item['desc'][:400]}\n"
              + "【字幕原文（可能有截断）】\n"
              + (sub_text or "（没拿到字幕）"))
    try:
        reply = await bot.llm.chat([{"role": "user", "content": prompt}])
    except Exception as e:
        log.warn(f"B站判断失败：{type(e).__name__} {e}")
        return None
    正文 = str(reply or "").strip()
    正文 = re.sub(r"^```(?:json)?|```$", "", 正文, flags=re.M).strip()
    try:
        return json.loads(正文)
    except Exception:
        log.warn("B站判断返回的不是 JSON，跳过这条")
        return None


_非法 = re.compile(r'[\\/:*?"<>|\r\n]+')


def _note_path(bot, item: dict) -> str:
    name = _非法.sub("-", item["title"]).strip("- ")[:60] or item["bvid"]
    stamp = time.strftime("%y%m%d")
    return os.path.join(_notes_dir(bot), f"{name}-{stamp}.md")


def _write_note(bot, item: dict, judge: dict, sub: str) -> str:
    path = _note_path(bot, item)
    lines = [
        f"# {item['title']}",
        "",
        f"> {judge.get('gist') or ''}",
        "",
        f"- **UP**：{item['author']}",
        f"- **链接**：{item['url']}",
        f"- **播放**：{item['play']}",
        f"- **时长**：{item['duration']} 秒",
        f"- **字幕**：{'有，' + str(len(sub)) + ' 字' if sub else '没拿到'}",
        "",
        "## 讲了什么",
        "",
        str(judge.get("gist") or "（没写）"),
        "",
        "## 关键点",
        "",
    ]
    for p in (judge.get("points") or [])[:6]:
        lines.append(f"- {p}")
    lines += ["", "## 为什么值得看", "", str(judge.get("why") or "（没写）"), "",
              "## 我的一句话", "", str(judge.get("take") or "（没写）"), ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ---------------------------------------------------------------- 对外
def recent_notes(bot, limit: int = 10) -> list[dict]:
    d = _notes_dir(bot)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".md")]
    except OSError:
        return []
    items = []
    for n in names:
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        items.append({"name": n, "path": p, "mtime": st.st_mtime, "size": st.st_size})
    items.sort(key=lambda x: -x["mtime"])
    return items[:max(1, int(limit))]


def read_note(bot, name: str) -> str:
    d = os.path.abspath(_notes_dir(bot))
    p = os.path.abspath(os.path.join(d, os.path.basename(str(name or ""))))
    if not p.startswith(d) or not os.path.isfile(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()[:6000]
    except OSError:
        return ""


async def run_round(bot, *, topic: str = "", want: int | None = None) -> dict:
    """刷一轮：搜 → 去重 → 读字幕 → 判断 → 写笔记。返回摘要，永不抛。"""
    if not _cfg(bot, "enabled", True):
        return {"ok": False, "reason": "刷 B 站没开（tools.bili_scroll_enabled=false）"}
    目标 = int(want or _cfg(bot, "per_round", 3) or 3)
    目标 = max(1, min(6, 目标))
    try:
        srv = _srv()
    except Exception as e:
        return {"ok": False, "reason": f"bilibili-api 没装好：{type(e).__name__}: {e}"}

    有凭证 = False
    try:
        有凭证 = _ensure_credential(bot)
    except Exception:
        有凭证 = False

    seen = _load_seen(bot)
    关键词 = [w for w in re.split(r"[\s,，、]+", str(topic or _cfg(bot, "topic", ""))) if w]
    候选: list[dict] = []
    for kw in 关键词[:4]:
        got = await _search(srv, kw, num=12)
        if got:
            候选.extend(got)
        await asyncio.sleep(1.2)          # 别把 B 站搜接口打急

    最小播放 = int(_cfg(bot, "min_play", 5000) or 0)
    最长 = int(_cfg(bot, "max_duration", 3600) or 0)
    去重表: dict[str, dict] = {}
    for v in 候选:
        if v["bvid"] in seen or v["bvid"] in 去重表:
            continue
        if 最小播放 and v["play"] < 最小播放:
            continue
        if 最长 and v["duration"] and v["duration"] > 最长:
            continue
        去重表[v["bvid"]] = v
    跳过 = len(候选) - len(去重表)

    笔记: list[dict] = []
    检查 = 0
    for v in list(去重表.values()):
        if len(笔记) >= 目标:
            break
        检查 += 1
        sub = await _subtitle(srv, v["bvid"]) if 有凭证 else ""
        if not sub and not v["desc"]:
            seen[v["bvid"]] = {"ts": int(time.time()), "title": v["title"], "why": "没字幕也没简介"}
            continue
        judge = await _judge(bot, v, sub)
        seen[v["bvid"]] = {"ts": int(time.time()), "title": v["title"],
                           "worth": bool((judge or {}).get("worth"))}
        if not judge or not judge.get("worth"):
            continue
        try:
            path = _write_note(bot, v, judge, sub)
        except OSError as e:
            log.warn(f"B站笔记写入失败：{e}")
            continue
        笔记.append({
            "bvid": v["bvid"], "title": v["title"], "author": v["author"],
            "url": v["url"], "gist": str(judge.get("gist") or "")[:120],
            "note": os.path.basename(path), "subtitle_chars": len(sub),
        })

    _save_seen(bot, seen)
    log.info(f"刷B站一轮：候选 {len(候选)} → 去重后 {len(去重表)} → 检查 {检查} → 笔记 {len(笔记)}")
    return {"ok": True, "credential": 有凭证, "candidates": len(候选),
            "skipped_seen": 跳过, "checked": 检查, "notes": 笔记}


# ---------------------------------------------------------------- 全功能路由
# 插件那 28 个能力全在这张表里，由 bili_action 一个工具转发，
# 不给她开 20 多个独立工具（工具表太胖会吃掉上下文）。
_动作表 = {
    "search": "bili_search",
    "video_info": "bili_video_info",
    "subtitle": "bili_subtitle",
    "comments": "bili_comments",
    "danmaku": "bili_danmaku",
    "crawl": "bili_crawl",
    "hot": "bili_hot_videos",
    "buzzwords": "bili_hot_buzzwords",
    "weekly": "bili_weekly_hot",
    "rank": "bili_rank",
    "zones": "bili_video_zones",
    "user_info": "bili_user_info",
    "search_user": "bili_search_user",
    "user_videos": "bili_user_videos",
    "favorites": "bili_favorite_lists",
    "favorite_content": "bili_favorite_content",
    "unread": "bili_unread_messages",
    "received_replies": "bili_received_replies",
    "received_at": "bili_received_at_and_likes",
    # 写操作（要 bili_jct）
    "reply": "bili_reply",
    "dynamic": "bili_send_dynamic",
    "opus": "bili_send_opus",
    "upload_video": "bili_upload_video",
    "upload_video_multi": "bili_upload_video_multi",
    "send_message": "bili_send_message",
}

_写动作 = {"reply", "dynamic", "opus", "upload_video", "upload_video_multi", "send_message"}


def _cred_file() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(_srv().__file__)),
                        "bili_credential.json")


def _bili_jct(bot) -> str:
    """写操作要的 CSRF token。先看 config，再看凭证文件。"""
    try:
        v = str(bot.config.get("tools.bili_jct", "") or "").strip()
    except Exception:
        v = ""
    if v:
        return v
    try:
        with open(_cred_file(), "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("bili_jct") or "").strip()
    except Exception:
        return ""


def actions() -> list[str]:
    return sorted(_动作表)


async def action(bot, name: str, args: dict | None = None) -> dict:
    """统一入口：把动作名 + 参数转给 vendored 的 bilibili-mcp。

    返回 {ok, action, data} 或 {ok: false, error, hint}。永不抛。
    """
    名 = str(name or "").strip().lower()
    if 名 not in _动作表:
        return {"ok": False, "error": f"没有这个动作：{name}",
                "hint": "可用： " + ", ".join(actions())}
    try:
        srv = _srv()
    except Exception as e:
        return {"ok": False, "error": f"bilibili-api 没装好：{type(e).__name__}: {e}"}

    if 名 in _写动作:
        if not _bili_jct(bot):
            return {"ok": False, "action": 名,
                    "error": "写 B 站（评论/动态/投稿/私信）需要 bili_jct，现在只有 SESSDATA。",
                    "hint": "把浏览器 cookie 里的 bili_jct 也贴给主人配置项 tools.bili_jct，"
                            "或者先在 B 站网页随便做一次操作让它刷新，再复制 bili_jct。"}

    fn = getattr(srv, _动作表[名], None)
    if fn is None:
        return {"ok": False, "error": f"vendored 服务里找不到 {_动作表[名]}"}

    # 只传它认得的参数，多传的丢掉（不同版本签名会有出入）
    try:
        允许 = set(inspect.signature(fn).parameters.keys())
    except (TypeError, ValueError):
        允许 = set()
    清理 = {k: v for k, v in (args or {}).items() if not 允许 or k in 允许}
    try:
        raw = await fn(**清理)
    except Exception as e:
        log.warn(f"B站动作 {名} 失败：{type(e).__name__} {e}")
        return {"ok": False, "action": 名, "error": f"{type(e).__name__}: {e}"}

    try:
        data = json.loads(raw)
    except Exception:
        data = str(raw or "")[:4000]
    if 名 == "subtitle" and isinstance(data, dict):
        data["text"] = str(data.get("text") or "")[:6000]
    return {"ok": True, "action": 名, "data": data}


# ---------------------------------------------------------------- 定时刷
def _parse_times(bot) -> list[int]:
    raw = _cfg(bot, "times", []) or []
    out = set()
    for t in raw:
        m = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$", str(t))
        if m:
            时, 分 = int(m.group(1)), int(m.group(2))
            if 0 <= 时 < 24 and 0 <= 分 < 60:
                out.add(时 * 60 + 分)
    return sorted(out)


def _wait_seconds(times: list[int], now: datetime | None = None) -> float:
    现在 = now or datetime.now()
    当前 = 现在.hour * 60 + 现在.minute + 现在.second / 60.0
    for t in times:
        if t > 当前:
            return max(5.0, (t - 当前) * 60.0)
    明天第一个 = times[0]
    return max(5.0, (1440 - 当前 + 明天第一个) * 60.0)


def _fmt_report(结果: dict, 时: str = "") -> str:
    笔记 = 结果.get("notes") or []
    if not 笔记:
        return (f"刷了一轮 B 站{'（' + 时 + '）' if 时 else ''}："
                f"翻到 {结果.get('checked', 0)} 条，没挑出值得单独记的。")
    行 = [f"刷完一轮 B 站{'（' + 时 + '）' if 时 else ''}：挑出 {len(笔记)} 条"]
    for i, n in enumerate(笔记, 1):
        行.append(f"{i}. {n['title']}\n   {n['gist']}\n   {n['url']}")
    return "\n".join(行)


async def _report(bot, 结果: dict, 时: str = "") -> None:
    if str(_cfg(bot, "report_to", "owner") or "").lower() in ("off", "none", ""):
        return
    文本 = _fmt_report(结果, 时)
    try:
        owners = [str(x) for x in (bot.config.get("account.owner_ids", []) or []) if str(x).strip()]
    except Exception:
        owners = []
    try:
        if str(_cfg(bot, "report_to", "owner")).lower() == "group":
            群 = str(_cfg(bot, "group", "") or "").strip()
            if 群:
                await bot.manual_send(群, _at_prefix(bot, owners) + 文本, True)
                await _comment(bot, 群, True, 结果)
                return
        if owners:
            await bot.manual_send(owners[0], 文本, False)
            await _comment(bot, owners[0], False, 结果)
    except Exception as e:
        log.warn(f"B站刷完汇报发不出去：{type(e).__name__} {e}")


def _at_prefix(bot, owners: list[str]) -> str:
    """群里汇报时按 tools.bili_scroll_report_at 决定要不要 @ 人。"""
    谁 = str(_cfg(bot, "report_at", "off") or "off").strip().lower()
    if 谁 in ("all", "everyone"):
        return "[CQ:at,qq=all] "
    if 谁 in ("owner", "master") and owners:
        return f"[CQ:at,qq={owners[0]}] "
    return ""


async def _comment(bot, 目标: str, 是群: bool, 结果: dict) -> None:
    """tools.bili_scroll_report_comment：贴完清单再用她自己的语气补一句。

    失败一律静默跳过——汇报本身已经发出去了，不能因为补一句而报错。
    """
    if not _cfg(bot, "report_comment", False):
        return
    笔记 = 结果.get("notes") or []
    if not 笔记:
        return
    标题 = "、".join(str(n.get("title") or "").strip() for n in 笔记[:3])
    想 = (f"你刚刷完 B 站，挑出这些：《{标题}》。"
          "用你自己的语气补一句感想或勾人的话，一两句，别复述标题、别列清单、别用 emoji。")
    try:
        try:
            系统 = bot.persona.build(group_id=int(目标)) if 是群 else bot.persona.build()
        except Exception:
            系统 = bot.persona.build()
        回复 = await asyncio.wait_for(
            bot.llm.chat_message([{"role": "system", "content": 系统},
                                  {"role": "user", "content": 想}], None),
            timeout=45)
        话 = str((回复 or {}).get("content") or "").strip()
        if not 话:
            return
        await bot.manual_send(目标, 话[:200], 是群)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warn(f"B站汇报补一句失败（已跳过）：{type(e).__name__} {e}")


async def scheduler(bot) -> None:
    """每天按 tools.bili_scroll_times 刷一轮，刷完私聊汇报。"""
    times = _parse_times(bot)
    if not times:
        return
    log.info("B站定时刷已启动：%s" % "、".join(f"{t // 60:02d}:{t % 60:02d}" for t in times))
    while True:
        await asyncio.sleep(_wait_seconds(times))
        try:
            现在 = datetime.now()
            时 = f"{现在.hour:02d}:{现在.minute:02d}"
            结果 = await run_round(bot)
            await _report(bot, 结果, 时)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warn(f"定时刷 B 站这一轮出错（已兜住）：{type(e).__name__} {e}")
        # 同一分钟内别触发第二次
        await asyncio.sleep(65)
