"""自动互动：回评论 / 发评论 / 发弹幕。

设计原则：
1. 学评论区说话的语感（bili_learn 学到的规则 + 别人的评论当样例），**不写成报告**；
2. 一律有额度：每天总互动次数上限、每种动作上限，去重表落盘；
3. 弹幕/评论只在她看过的视频（bili_notes）上做，不扫全站；
4. 理解视频靠 B 站自动字幕（文字文件），不抽帧、不看画面；
5. **所有对外写都经 bili_scroll.action → bili_gate**，没有进程内凭证一条都不发；
   本模块不直接调用 bili_extra 的写函数。
6. **每一条都要像她本人说的**（`bili_voice`）：生成前注入人设 + 视频摘要 +
   该视频实时评论树（含楼中楼，只在内存里用）+ 她最近说过的话，生成后做
   反公式化检查；不过就重写一次，仍不过就放弃（宁可不发）。
7. **发出去就马上记账**：成功一条就把文本放进进程内最近发言缓存 + 动作账本
   的 ``voice`` 桶；同一轮紧接着的第二条立刻看得到它，不会等本轮结束才落盘。
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
#: 刷片自动回复（bili_scroll._autonomous_reply）发出去的自家发言记在这个桶里，
#: 好让下次生成时「最近说过什么」不会漏掉它、又重复同一句。
VOICE_KEY = "voice"

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
            if not isinstance(d.get(VOICE_KEY), list):
                d[VOICE_KEY] = []
            d.setdefault(DAILY_KEY, {})
            return d
    except Exception:
        pass
    return {"replies": {}, "commented": {}, "danmaku": {}, VOICE_KEY: [],
            DAILY_KEY: {}}


def _own_text(text, limit: int = 300) -> str:
    """账本/缓存里存的自家发言：只压空白 + 限长。

    不拆句、不去引号——存进去的必须是**实际发出去的那句**，否则下次和它算
    相似度时比较的是被改写过的文本。
    """
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _merge_voice(seen: dict, disk: dict) -> None:
    """把磁盘上刚被别的路径补记的 ``voice`` 行并回本轮账本（只加不删）。

    自动互动一轮里会多次 ``_save_seen``；刷片自动回复 / 工具写 / 主人手打
    可能在本轮中途把 voice 桶写进磁盘，这些行不在本轮的 ``seen`` 里，
    直接整体保存会把它们冲掉。
    """
    rows = [r for r in (seen.get(VOICE_KEY) or []) if isinstance(r, dict)]
    known = set()
    for row in rows:
        try:
            known.add((int(row.get("ts") or 0), str(row.get("text") or "")))
        except (TypeError, ValueError):
            continue
    for row in (disk.get(VOICE_KEY) or []):
        if not isinstance(row, dict):
            continue
        try:
            key = (int(row.get("ts") or 0), str(row.get("text") or ""))
        except (TypeError, ValueError):
            continue
        if key in known:
            continue
        rows.append(row)
        known.add(key)
    seen[VOICE_KEY] = rows[-300:]


def _save_seen(bot, seen: dict) -> None:
    path = _seen_path(bot)
    try:
        try:
            _merge_voice(seen, _load_seen(bot))
        except Exception as e:  # noqa: BLE001
            log.debug(f"合并磁盘 voice 桶失败：{type(e).__name__}")
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


async def _draft(bot, task: str, context: str, examples: str = "", *,
                 bvid: str = "", kind: str = "comment", max_chars: int = 80,
                 material: dict | None = None):
    """写一句：人设 + 视频摘要 + 实时评论上下文 + 「别说这些」+ 反公式化检查。

    返回 ``bili_voice.Draft``（str 子类）。**空串 = 放弃，调用方不许发**；
    ``.violations`` 是放弃原因。检查不过会带着更强制约重写
    （``tools.bili_voice_max_retry``，默认 1 次），仍不过就放弃。
    """
    from . import bili_voice

    return await bili_voice.compose_text(
        bot, task=task, context=context, bvid=bvid, kind=kind,
        max_chars=max_chars, examples=examples, material=material,
        style=_style_rules(bot),
        # 只做格式清理，不按上限截断：长度越界交给 check 拦（宁可不发）。
        clean=lambda raw: _clean(raw, 200))


def _reject(result: dict, kind: str, bvid: str, draft) -> None:
    """记录一条「因为反公式化检查没过而放弃」——不静默、也不发。"""
    reasons = list(getattr(draft, "violations", ()) or ())
    entry = {
        "kind": str(kind),
        "bvid": str(bvid or ""),
        "attempts": int(getattr(draft, "attempts", 0) or 0),
        "reason": ("；".join(reasons)[:300] if reasons
                   else "没写出合格的一句（模型空/失败）"),
    }
    result.setdefault("voice_rejected", []).append(entry)
    log.info(f"B 站{kind}放弃（{bvid or '-'}）：{entry['reason'][:160]}")


def _mark_rejected(seen: dict, bucket: str, key: str, kind: str, bvid: str,
                   draft) -> None:
    """把「放弃」写进去重表：本轮/以后都不再对同一条硬发（避免反复烧模型）。

    只记拒绝原因（口头禅/开头/相似度），不记评论区里别人的原文。
    """
    key = str(key or "")
    if not key:
        return
    reasons = list(getattr(draft, "violations", ()) or ())
    seen.setdefault(bucket, {})[key] = {
        "ts": int(time.time()),
        "rejected": True,
        "kind": str(kind),
        "bvid": str(bvid or ""),
        "reason": ("；".join(reasons)[:200] if reasons else "没写出合格的一句"),
    }


def note_own_text(bot, kind: str, bvid: str, text: str, *,
                  seen: dict | None = None) -> None:
    """记一条**已经发出去**的自家发言：本进程缓存 + 动作账本的 voice 桶。

    bili_auto 自己的回复/评论/弹幕已经写进 replies/commented/danmaku；
    voice 桶另外收所有路径（刷片自动回复、工具写、主人手打）发出去的，
    这样「最近说过什么」不会漏掉、也不会在同一轮里重复。

    ``seen`` 传进来时直接写进这份本轮账本并立刻落盘（调用方持有的一轮账本），
    避免本轮结束的整体保存把中途补记的行冲掉；不传就现读、追加、保存。
    """
    if bot is None:
        return
    text = _own_text(text)
    if not text:
        return
    try:
        from . import bili_voice
        bili_voice.remember_own(bot, text, kind=kind, bvid=bvid)
    except Exception as e:  # noqa: BLE001
        log.debug(f"进程内补记自家发言失败：{type(e).__name__} {e}")
    try:
        target = seen if isinstance(seen, dict) else _load_seen(bot)
        rows = target.get(VOICE_KEY)
        if not isinstance(rows, list):
            rows = []
        rows.append({"ts": int(time.time()), "kind": str(kind or "reply"),
                     "bvid": str(bvid or ""), "text": text})
        target[VOICE_KEY] = rows[-300:]
        _save_seen(bot, target)
    except Exception as e:  # noqa: BLE001
        log.debug(f"补记自家发言失败：{type(e).__name__}")


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
        info = await bili_scroll._subtitle(srv, bvid)
        # bili_scroll._subtitle 返回 {text,status,...}；调用方只想要正文。
        # 旧实现直接把 dict 当字符串切片，云端自动互动每轮 KeyError。
        if isinstance(info, dict):
            return str(info.get("text") or "")
        return str(info or "")
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


def _grant_for(grant, grant_factory, action: str, args: dict):
    """每次写之前取一枚**绑定动作+目标**的凭证。

    优先用工厂（定时任务/主人消息各自签发）；工厂换不到就回 None，这次不写。
    凭证绑定目标 = bili_gate.target_key(action, args)，不能拿去写别的视频。
    """
    from . import bili_gate

    if callable(grant_factory):
        try:
            return grant_factory(action, bili_gate.target_key(action, args))
        except Exception:
            return None
    return grant


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def run_round(bot, *, replies: bool | None = None, comment: bool | None = None,
                    danmaku: bool | None = None, reason: str = "",
                    grant=None, grant_factory=None) -> dict:
    """跑一轮自动互动。永不抛。

    **所有对外写入都经 bili_scroll.action → bili_gate**：本函数不直接调用
    bili_extra 的评论/回复/弹幕写函数。没有进程内凭证、闸门没开、额度用完时，
    一条都不会发出去。读（收到的回复/字幕/评论）仍然可以直接调。
    云端等价：``bili_auto_daily_limit`` / 三个 per-round limit 的 **0 = 不限**。
    """
    if _cfg(bot, "bili_auto_enabled", False) is not True:
        return {"ok": False, "reason": "自动互动没开（tools.bili_auto_enabled 要是字面 true）"}

    # 云端等价：0 = 不限；负数才是配置错。
    quota = _as_int(_cfg(bot, "bili_auto_daily_limit", 0), 0)
    if quota < 0:
        return {"ok": False,
                "reason": "自动互动额度不能是负数（tools.bili_auto_daily_limit；0 = 不限）"}
    if grant is None and not callable(grant_factory):
        return {"ok": False,
                "reason": "自动互动没有进程内写授权（bili_gate 凭证）；这一轮不发任何东西"}

    do_reply = (_cfg(bot, "bili_auto_reply", False) is True) if replies is None else bool(replies)
    do_comment = (_cfg(bot, "bili_auto_comment", False) is True) if comment is None else bool(comment)
    do_danmaku = (_cfg(bot, "bili_auto_danmaku", False) is True) if danmaku is None else bool(danmaku)
    if not (do_reply or do_comment or do_danmaku):
        return {"ok": False, "reason": "自动互动的三种动作都没开"}

    from . import bili_extra, bili_scroll, bili_voice

    today = time.strftime("%Y-%m-%d")
    seen = _load_seen(bot)
    used = int(seen[DAILY_KEY].get(today, 0))
    if quota > 0 and used >= quota:
        return {"ok": False, "reason": f"今天互动额度用完了（{used}/{quota}）"}

    result = {"ok": True, "reason": reason, "used": used, "quota": quota,
              "replied": [], "commented": [], "danmaku": [], "errors": [],
              # 因为「这个视频今天回复额度用完了」而没发的视频（键去重，只用于日志）。
              "skipped_quota": [],
              # 2026-11-02 之前的通知：不统计、不回。
              "skipped_old": [],
              # 没过反公式化检查而放弃的条目（宁可不发）。
              "voice_rejected": []}
    _满 = set()          # 这一轮已经确认额度用尽的回复键，免得反复读账本

    def _skip_reply(键: str) -> None:
        _满.add(键)
        if 键 not in result["skipped_quota"]:
            result["skipped_quota"].append(键)

    def _bump():
        nonlocal used
        used += 1
        seen[DAILY_KEY][today] = used

    # ---------- 1) 回复收到的评论 ----------
    if do_reply and (quota <= 0 or used < quota):
        # 0 = 不限（云端等价）；扫多少条由 tools.bili_auto_reply_scan 决定。
        limit = _as_int(_cfg(bot, "bili_auto_reply_limit", 0), 0)
        limit = limit if limit > 0 else 10 ** 9
        扫描 = max(1, min(200, _as_int(_cfg(bot, "bili_auto_reply_scan", 50), 50)))
        try:
            items = await bili_extra.received_replies(limit=扫描)
        except Exception as e:
            items = []
            result["errors"].append(f"取回复失败：{type(e).__name__}")
        done = 0
        截止 = bili_voice.cutoff_ts()
        for it in items:
            if done >= limit or (quota > 0 and used >= quota):
                break
            nid = str(it.get("notify_id") or "")
            if not nid or nid in seen["replies"]:
                continue
            # 2026-11-02 之前的回复是主人自己发的：不统计、也不回。
            # 拿不到 reply_time 的通知同样不回（没法证明是之后的）。
            if _as_int(it.get("reply_time"), 0) < 截止:
                result["skipped_old"].append(nid or "?")
                continue
            bvid = str(it.get("bvid") or "")
            root = _as_int(it.get("root_id"), 0)
            rpid = _as_int(it.get("source_id"), 0) or root
            if not bvid or not rpid:
                continue
            # 同一个视频一天最多回几条（tools.bili_auto_reply_per_video_daily；
            # 0=不限）：额度用完就不起草、不发，只记一次跳过。
            键 = bili_extra._reply_key(bvid, _as_int(it.get("aid"), 0))
            if 键 in _满 or bili_extra.reply_left_today(bot, 键) <= 0:
                _skip_reply(键)
                continue
            ctx = (f"对方（{it.get('nickname') or '网友'}）说：{it.get('text') or ''}\n"
                   f"你原来那条评论：{it.get('source_text') or ''}")
            try:
                text = await _draft(bot, "回复下面这条给你的评论（口语、别客套）", ctx,
                                    bvid=bvid, kind="reply", max_chars=60)
            except Exception as e:
                result["errors"].append(f"回评论草稿失败：{type(e).__name__}: {e}")
                continue
            if not text:
                _reject(result, "reply", bvid, text)
                _mark_rejected(seen, "replies", nid, "reply", bvid, text)
                continue
            args = {"bvid": bvid, "text": text, "rpid": rpid}
            if root and root != rpid:
                args["root"] = root
            结果 = await bili_scroll.action(bot, "reply", args,
                                            grant=_grant_for(grant, grant_factory,
                                                             "reply", args))
            if 结果.get("ok"):
                seen["replies"][nid] = {"ts": int(time.time()), "text": text,
                                        "bvid": bvid}
                result["replied"].append(text)
                # 本地回复走 vendored bili_reply，不经 bili_extra.reply_comment，
                # 所以成功后在这里补记一笔每视频每日额度（reply_comment 自己那条
                # 路径也会记，同一次发布只会走其中一边）。
                bili_extra.note_reply_sent(bot, bvid)
                # 成功后立刻进「本进程内最近发言」+ voice 桶：同一轮紧接着的
                # 下一条生成/检查马上看得到这句，不会等到本轮结束才落盘。
                note_own_text(bot, "reply", bvid, text, seen=seen)
                _bump()
                done += 1
                continue
            result["errors"].append(f"回评论没成：{结果.get('error') or '未知原因'}")
            if 结果.get("unconfirmed"):
                # 可能已经发出去了：记账但不重试，避免重复回复同一条通知。
                seen["replies"][nid] = {"ts": int(time.time()), "text": text,
                                        "bvid": bvid, "unconfirmed": True}
            if 结果.get("denied"):
                break
        _save_seen(bot, seen)

    # ---------- 2) 在自己看过的视频下评论 ----------
    if do_comment and (quota <= 0 or used < quota):
        want = _as_int(_cfg(bot, "bili_auto_comment_limit", 0), 0)
        want = want if want > 0 else 10 ** 9
        扫描 = max(1, min(200, _as_int(_cfg(bot, "bili_auto_notes_scan", 50), 50)))
        notes = bili_scroll.recent_notes(bot, limit=扫描)
        done = 0
        for note in notes:
            if done >= want or (quota > 0 and used >= quota):
                break
            meta = _note_meta(bot, note["path"])
            bvid = meta.get("bvid")
            if not bvid or bvid in seen["commented"]:
                continue
            sub = await _subtitle(bot, bvid)
            # 视频摘要用标题+笔记 gist+字幕（首次生成后缓存到 content/bili_summaries/）；
            # 评论区原文由 _draft 每次现抓，这里不再单独拉一份样例。
            material = {"title": meta.get("title") or "",
                        "desc": meta.get("gist") or "", "subtitle": sub}
            ctx = (f"视频标题：{meta.get('title') or ''}\n"
                   f"自动字幕节选：{sub[:900]}\n"
                   f"你之前的笔记：{meta.get('gist', '')[:400]}")
            # 一半概率直接回复评论区里别人的话，否则发顶层评论（都要过闸门）
            target = None
            chance = float(_cfg(bot, "bili_auto_reply_others_chance", 0.5) or 0.5)
            if random.random() < chance:
                键 = bili_extra._reply_key(bvid, 0)
                if 键 in _满 or bili_extra.reply_left_today(bot, 键) <= 0:
                    # 这个视频今天回复额度满了：不回复，退成发顶层评论（顶层评论
                    # 不占「每视频回复额度」），并且记一次跳过，不静默。
                    _skip_reply(键)
                else:
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
                    text = await _draft(bot, "在评论区里回这条评论（像底下接话，别客套）",
                                        ctx2, bvid=bvid, kind="reply", max_chars=60,
                                        material=material)
                else:
                    text = await _draft(bot, "在这个视频下面留一条你的评论", ctx,
                                        bvid=bvid, kind="comment", max_chars=80,
                                        material=material)
                if not text:
                    _reject(result, "reply" if target else "comment", bvid, text)
                    _mark_rejected(seen, "commented", bvid,
                                   "reply" if target else "comment", bvid, text)
                    continue
                if target:
                    args = {"bvid": bvid, "text": text, "rpid": int(target["rpid"])}
                    结果 = await bili_scroll.action(bot, "reply", args,
                                                    grant=_grant_for(grant, grant_factory,
                                                                     "reply", args))
                else:
                    args = {"bvid": bvid, "text": text}
                    结果 = await bili_scroll.action(bot, "comment", args,
                                                    grant=_grant_for(grant, grant_factory,
                                                                     "comment", args))
            except Exception as e:
                result["errors"].append(f"发评论失败：{type(e).__name__}: {e}")
                continue
            if 结果.get("ok"):
                if target:
                    bili_extra.note_reply_sent(bot, bvid)
                seen["commented"][bvid] = {"ts": int(time.time()), "text": text,
                                           "target": str(target.get("rpid")) if target else ""}
                # 回别人的评论是「回复」，不是「顶层评论」：分开计数，汇报才不
                # 会把回评论说成发了条评论（内容形状也与回复段一致，都是文本）。
                if target:
                    result["replied"].append(text)
                else:
                    result["commented"].append({"bvid": bvid, "text": text})
                note_own_text(bot, "reply" if target else "comment", bvid, text,
                              seen=seen)
                _bump()
                done += 1
                continue
            result["errors"].append(f"发评论没成：{结果.get('error') or '未知原因'}")
            if 结果.get("unconfirmed"):
                seen["commented"][bvid] = {"ts": int(time.time()), "text": text,
                                           "unconfirmed": True}
            if 结果.get("denied"):
                break
        _save_seen(bot, seen)

    # ---------- 3) 发弹幕 ----------
    if do_danmaku and (quota <= 0 or used < quota):
        want = _as_int(_cfg(bot, "bili_auto_danmaku_limit", 0), 0)
        want = want if want > 0 else 10 ** 9
        扫描 = max(1, min(200, _as_int(_cfg(bot, "bili_auto_notes_scan", 50), 50)))
        notes = bili_scroll.recent_notes(bot, limit=扫描)
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
            material = {"title": meta.get("title") or "",
                        "desc": meta.get("gist") or "", "subtitle": sub}
            try:
                # 弹幕也要过同一套人设/上下文/反公式化检查（不许绕开）；
                # 上限交给 check，不在这里静默截断。
                text = await _draft(bot, "发一条很短很口语的弹幕（20 字内，别像总结）", ctx,
                                    bvid=bvid, kind="danmaku", max_chars=25,
                                    material=material)
                if not text:
                    _reject(result, "danmaku", bvid, text)
                    _mark_rejected(seen, "danmaku", key, "danmaku", bvid, text)
                    continue
                # 弹幕飘在视频前 3 分钟里，随机一点更像真人
                args = {"bvid": bvid, "text": text,
                        "progress": random.randint(8, 180)}
                结果 = await bili_scroll.action(
                    bot, "send_danmaku", args,
                    grant=_grant_for(grant, grant_factory,
                                     "send_danmaku", args))
            except Exception as e:
                result["errors"].append(f"发弹幕失败：{type(e).__name__}: {e}")
                continue
            if 结果.get("ok"):
                seen["danmaku"][key] = {"ts": int(time.time()), "text": text}
                result["danmaku"].append({"bvid": bvid, "text": text})
                note_own_text(bot, "danmaku", bvid, text, seen=seen)
                _bump()
                done += 1
                continue
            result["errors"].append(f"发弹幕没成：{结果.get('error') or '未知原因'}")
            if 结果.get("unconfirmed"):
                seen["danmaku"][key] = {"ts": int(time.time()), "text": text,
                                        "unconfirmed": True}
            if 结果.get("denied"):
                break
        _save_seen(bot, seen)

    _save_seen(bot, seen)
    if any(result[k] for k in ("replied", "commented", "danmaku", "voice_rejected",
                               "skipped_old")):
        跳过 = len(result["skipped_quota"])
        尾 = ""
        if 跳过:
            上限 = bili_extra._reply_cap(bot)
            尾 = (f"｜{跳过} 个视频今天回复已满 {上限} 条，跳过" if 上限 > 0
                  else f"｜{跳过} 个视频今天回复已满，跳过")
        过期 = len(result["skipped_old"])
        if 过期:
            尾 += f"｜{过期} 条 2026-11-02 之前的通知跳过"
        放弃 = len(result["voice_rejected"])
        if 放弃:
            尾 += f"｜{放弃} 条没过反公式化检查，没发"
        log.info(f"自动互动（{reason or '定时'}）：回 {len(result['replied'])} 条 / "
                 f"评论 {len(result['commented'])} 条 / 弹幕 {len(result['danmaku'])} 条 "
                 f"（今日 {used}/{quota or '∞'}）" + 尾)
    return result


async def scheduler(bot) -> None:
    """每 bili_auto_interval_min 分钟跑一轮自动互动。"""
    import asyncio

    from . import bili_gate

    interval = max(10, int(_cfg(bot, "bili_auto_interval_min", 30) or 30))
    # 受控定时任务的能力：每次写之前换一枚**绑定动作+目标**的一次性写凭证；
    # 模型/参数碰不到它。投币/删除不在 scheduler 可授权的动作里，换不到凭证。
    capability = bili_gate.register_scheduler("bili-auto")
    log.info(f"自动互动已启动：每 {interval} 分钟一轮（回评论/发评论/发弹幕，全部过写闸门）")
    while True:
        await asyncio.sleep(interval * 60)
        try:
            await run_round(
                bot, reason="定时",
                grant_factory=lambda action, target: bili_gate.scheduler_grant(
                    capability, action=action, target=target))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warn(f"自动互动这一轮出错（已兜住）：{type(e).__name__} {e}")
