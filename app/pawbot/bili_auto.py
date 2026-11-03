"""自动互动：回评论 / 发评论 / 发弹幕。

设计原则：
1. 学评论区说话的语感（bili_learn 学到的规则 + 别人的评论当样例），**不写成报告**；
2. 一律有额度：每天总互动次数上限、每种动作上限，去重表落盘；
3. 弹幕/评论只在她看过的视频（bili_notes）上做，不扫全站；
4. 理解视频靠 B 站自动字幕（文字文件），不抽帧、不看画面。
"""

from __future__ import annotations

import json
import os
import random
import re
import time

from .log import log
from . import paths as layout


SEEN_FILE = "bili_auto_seen.json"
DAILY_KEY = "daily"

_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_MD_TITLE_RE = re.compile(r"^#\s+(.+)$", re.M)


def _cfg(bot, key: str, default=None):
    try:
        return bot.config.get("tools." + key, default)
    except Exception:
        return default


def _seen_path(bot) -> str:
    """去重表是**机器状态**：放 state/，不放 content/（content 会跨机器同步）。

    云端原版用 ``os.path.dirname(bot.persona.persona_path)``，那会落进 content/；
    这里跟 bili_scroll 的判重表一样走 DataRoot 状态区。
    """
    try:
        return layout.for_bot(bot).state(SEEN_FILE)
    except Exception as e:
        log.warn(f"自动互动去重表路径解析失败（退回数据根）：{type(e).__name__}")
        try:
            return os.path.join(os.path.dirname(bot.persona.persona_path), SEEN_FILE)
        except Exception:
            return os.path.join(os.getcwd(), "data", SEEN_FILE)


def _load_seen(bot) -> dict:
    try:
        with open(_seen_path(bot), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in ("replies", "commented", "danmaku"):
                d.setdefault(k, {})
            d.setdefault(DAILY_KEY, {})
            return d
    except Exception:
        pass
    return {"replies": {}, "commented": {}, "danmaku": {}, DAILY_KEY: {}}


def _save_seen(bot, seen: dict) -> None:
    path = _seen_path(bot)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log.warn(f"自动互动去重表写入失败：{e}")


def _style_rules(bot) -> str:
    try:
        from .bili_learn import style_text
        return style_text(bot, 1200)
    except Exception:
        return ""


def _clean(text: str, limit: int = 60) -> str:
    s = str(text or "").strip()
    s = re.sub(r"^```[\s\S]*?```$", "", s)
    s = re.sub(r"^[-*•\d.、)\s]+", "", s).strip()
    s = s.strip("“”\"'「」『』 ")
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\s+", " ", s)
    # 只要前 1~2 句，防止写成小作文
    parts = re.split(r"(?<=[。！？!?~～])", s)
    s = "".join(parts[:2]).strip() or s
    return s[:limit].strip()


async def _draft(bot, task: str, context: str, examples: str = "") -> str:
    style = _style_rules(bot)
    prompt = (
        "你在 B 站刷视频，像个普通老观众一样说话。\n"
        "硬性要求：1~2 句、40 字以内；口语、带情绪（吐槽/感叹/追问都行）；"
        "不要总结视频、不要列点、不要测评腔、不要像写报告；不提 AI，不带链接、不带 @。\n"
        + (f"\n你学到的说话方式（学语感，别照抄句子）：\n{style}\n" if style else "")
        + f"\n【任务】{task}\n【上下文】\n{str(context)[:1200]}\n"
        + (f"\n【评论区里别人的话（只学语感）】\n{str(examples)[:1200]}\n" if examples else "")
        + "\n只输出你要说的话本身。"
    )
    out = await bot.llm.chat([{"role": "user", "content": prompt}])
    return _clean(out)


def _note_meta(bot, note_path: str) -> dict:
    """从笔记 markdown 里取 bvid / 标题 / gist。"""
    try:
        with open(note_path, "r", encoding="utf-8") as f:
            raw = f.read(6000)
    except OSError:
        return {}
    m = _BVID_RE.search(raw)
    title = ""
    mt = _MD_TITLE_RE.search(raw)
    if mt:
        title = mt.group(1).strip()
    gist = ""
    gi = raw.find("## 为什么值得看")
    if gi >= 0:
        gist = raw[gi:gi + 600]
    return {"bvid": m.group(0) if m else "", "title": title, "gist": gist, "raw": raw}


async def _subtitle(bot, bvid: str) -> str:
    """B 站自动生成的字幕（文字文件），优先用它理解视频。"""
    from . import bili_scroll
    try:
        srv = bili_scroll._srv()
        return await bili_scroll._subtitle(srv, bvid)
    except Exception as e:
        log.debug(f"自动互动取字幕失败 {bvid}：{type(e).__name__}")
        return ""


async def _comments_meta(bot, bvid: str, num: int = 10) -> list[dict]:
    """带 rpid 的评论列表（用来回复别人的评论）。"""
    from . import bili_scroll
    try:
        srv = bili_scroll._srv()
        rows = await bili_scroll._comments(srv, bvid, num)
    except Exception:
        return []
    return [r for r in rows if r.get("rpid")]


async def _comment_examples(bot, bvid: str, num: int = 8) -> str:
    from . import bili_scroll
    try:
        srv = bili_scroll._srv()
        rows = await bili_scroll._comments(srv, bvid, num)
    except Exception:
        return ""
    return "\n".join(f"- {r.get('content', '')[:80]}" for r in rows[:num])


async def run_round(bot, *, replies: bool | None = None, comment: bool | None = None,
                    danmaku: bool | None = None, reason: str = "") -> dict:
    """跑一轮自动互动。永不抛。"""
    if not _cfg(bot, "bili_auto_enabled", True):
        return {"ok": False, "reason": "自动互动没开（tools.bili_auto_enabled=false）"}

    from . import bili_extra

    quota = int(_cfg(bot, "bili_auto_daily_limit", 20) or 0)
    today = time.strftime("%Y-%m-%d")
    seen = _load_seen(bot)
    used = int(seen[DAILY_KEY].get(today, 0))
    if quota > 0 and used >= quota:
        return {"ok": False, "reason": f"今天互动额度用完了（{used}/{quota}）"}

    do_reply = bool(_cfg(bot, "bili_auto_reply", True)) if replies is None else replies
    do_comment = bool(_cfg(bot, "bili_auto_comment", True)) if comment is None else comment
    do_danmaku = bool(_cfg(bot, "bili_auto_danmaku", True)) if danmaku is None else danmaku

    result = {"ok": True, "reason": reason, "used": used, "quota": quota,
              "replied": [], "commented": [], "danmaku": [], "errors": []}

    def _bump():
        nonlocal used
        used += 1
        seen[DAILY_KEY][today] = used

    # ---------- 1) 回复收到的评论 ----------
    if do_reply and (quota <= 0 or used < quota):
        limit = int(_cfg(bot, "bili_auto_reply_limit", 5) or 5)
        try:
            items = await bili_extra.received_replies(limit=20)
        except Exception as e:
            items = []
            result["errors"].append(f"取回复失败：{type(e).__name__}")
        done = 0
        for it in items:
            if done >= limit or (quota > 0 and used >= quota):
                break
            nid = str(it.get("notify_id") or "")
            if not nid or nid in seen["replies"]:
                continue
            if not it.get("aid") or not it.get("root_id"):
                continue
            ctx = (f"对方（{it.get('nickname') or '网友'}）说：{it.get('text') or ''}\n"
                   f"你原来那条评论：{it.get('source_text') or ''}")
            try:
                text = await _draft(bot, "回复下面这条给你的评论（口语、别客套）", ctx)
                await bili_extra.reply_comment(int(it["aid"]), text,
                                               root=int(it["root_id"]),
                                               parent=int(it.get("source_id") or 0))
            except Exception as e:
                result["errors"].append(f"回评论失败：{type(e).__name__}: {e}")
                continue
            seen["replies"][nid] = {"ts": int(time.time()), "text": text}
            result["replied"].append(text)
            _bump()
            done += 1
        _save_seen(bot, seen)

    # ---------- 2) 在自己看过的视频下评论 ----------
    if do_comment and (quota <= 0 or used < quota):
        from . import bili_scroll
        want = int(_cfg(bot, "bili_auto_comment_limit", 2) or 2)
        notes = bili_scroll.recent_notes(bot, limit=12)
        done = 0
        for note in notes:
            if done >= want or (quota > 0 and used >= quota):
                break
            meta = _note_meta(bot, note["path"])
            bvid = meta.get("bvid")
            if not bvid or bvid in seen["commented"]:
                continue
            sub = await _subtitle(bot, bvid)
            examples = await _comment_examples(bot, bvid, 8)
            ctx = (f"视频标题：{meta.get('title') or ''}\n"
                   f"自动字幕节选：{sub[:900]}\n"
                   f"你之前的笔记：{meta.get('gist', '')[:400]}")
            # 一半概率直接回复评论区里别人的话（「评论别人的评论」），否则发顶层评论
            target = None
            chance = float(_cfg(bot, "bili_auto_reply_others_chance", 0.5) or 0.5)
            if random.random() < chance:
                try:
                    pool = await _comments_meta(bot, bvid, 10)
                except Exception:
                    pool = []
                pool = [c for c in pool if c.get("rpid") and len(str(c.get("content") or "")) >= 6]
                if pool:
                    target = random.choice(pool[:6])
            try:
                if target:
                    ctx2 = (f"视频标题：{meta.get('title') or ''}\n"
                            f"别人的评论（{target.get('user') or '网友'}）：{target.get('content')}"
                            f"\n自动字幕节选：{sub[:600]}")
                    text = await _draft(bot, "在评论区里回这条评论（像底下接话，别客套）", ctx2, examples)
                    aid = int(target.get("aid") or 0) or await bili_extra.aid_of(bvid)
                    await bili_extra.reply_comment(aid, text, root=int(target["rpid"]))
                else:
                    text = await _draft(bot, "在这个视频下面留一条你的评论", ctx, examples)
                    await bili_extra.comment_video(bvid, text)
            except Exception as e:
                result["errors"].append(f"发评论失败：{type(e).__name__}: {e}")
                continue
            seen["commented"][bvid] = {"ts": int(time.time()), "text": text,
                                       "target": str(target.get("rpid")) if target else ""}
            result["commented"].append({"bvid": bvid, "text": text})
            _bump()
            done += 1
        _save_seen(bot, seen)

    # ---------- 3) 发弹幕 ----------
    if do_danmaku and (quota <= 0 or used < quota):
        from . import bili_scroll
        want = int(_cfg(bot, "bili_auto_danmaku_limit", 2) or 2)
        notes = bili_scroll.recent_notes(bot, limit=12)
        done = 0
        for note in notes:
            if done >= want or (quota > 0 and used >= quota):
                break
            meta = _note_meta(bot, note["path"])
            bvid = meta.get("bvid")
            if not bvid:
                continue
            key = bvid + ":" + time.strftime("%Y-%m-%d")
            if key in seen["danmaku"]:
                continue
            sub = await _subtitle(bot, bvid)
            ctx = (f"视频标题：{meta.get('title') or ''}\n自动字幕节选：{sub[:600]}")
            try:
                text = await _draft(bot, "发一条很短很口语的弹幕（15 字内，别像总结）", ctx)
                if len(text) > 20:
                    text = text[:20]
                # 弹幕飘在视频前 3 分钟里，随机一点更像真人
                await bili_extra.send_danmaku(bvid, text, progress_sec=random.randint(8, 180))
            except Exception as e:
                result["errors"].append(f"发弹幕失败：{type(e).__name__}: {e}")
                continue
            seen["danmaku"][key] = {"ts": int(time.time()), "text": text}
            result["danmaku"].append({"bvid": bvid, "text": text})
            _bump()
            done += 1
        _save_seen(bot, seen)

    _save_seen(bot, seen)
    if any(result[k] for k in ("replied", "commented", "danmaku")):
        log.info(f"自动互动（{reason or '定时'}）：回 {len(result['replied'])} 条 / "
                 f"评论 {len(result['commented'])} 条 / 弹幕 {len(result['danmaku'])} 条 "
                 f"（今日 {used}/{quota or '∞'}）")
    return result


async def scheduler(bot) -> None:
    """每 bili_auto_interval_min 分钟跑一轮自动互动。"""
    import asyncio

    interval = max(10, int(_cfg(bot, "bili_auto_interval_min", 30) or 30))
    log.info(f"自动互动已启动：每 {interval} 分钟一轮（回评论/发评论/发弹幕）")
    while True:
        await asyncio.sleep(interval * 60)
        try:
            await run_round(bot, reason="定时")
        except Exception as e:
            log.warn(f"自动互动这一轮出错（已兜住）：{type(e).__name__} {e}")
