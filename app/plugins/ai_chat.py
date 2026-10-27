"""
大模型群聊插件 —— 现在它会自己用工具了。

它做的事：
  1. 旁听普通群消息，模型自行决定加入或沉默；私聊正常回复
  2. 拼人设（data/persona.md）+ 记忆（data/memory/）+ 上下文
  3. 进 Agent：模型可以自己决定要不要 web_search / fetch_url /
     recall_memory / remember / pick_sticker / post_qzone
  4. 回复（引用 + 主动 @ 按 config.reply 走），表情包跟文字一起发
  5. 写回会话存储

改人设不用改代码：直接编辑 data/persona.md，下一句就生效。
"""

import asyncio
import random
import re
import time

from pawbot import tasks
from pawbot import tts
from pawbot import message as msg
from pawbot.tools import commit_bili_delivery, prepare_bili_delivery
from pawbot.log import log
from pawbot.plugin import on_command, on_message
from pawbot.style import cap_sentences, drop_trailing_question, polish

__plugin_name__ = "AI 群聊"
__version__ = "2.0.0"
__description__ = "会搜网、会翻记忆、会发表情包的 AI 群友"

_LOCKS: dict[str, asyncio.Lock] = {}
_PENDING: dict[str, tuple] = {}
# 攒话窗口：对方可能一句完整意思拆成几条发，先等一小会儿再交给模型
_SETTLE: dict[str, dict] = {}
# 普通群消息的批量回复：攒到几句（或等很久只有一句）再一起回
_BATCH: dict[str, dict] = {}
# 被点名时模型仍交回 <pass> / 空回复，强制保底说一句（不许沉默）
_fallback_lines = (
    "唔……在呢在呢，怎么啦？",
    "诶？你叫我呀？说嘛说嘛。",
    "呜……什么事呀，我听着呢。",
)


def _clean(text: str, wake_words: list[str], nickname: str) -> str:
    """去掉开头的唤醒词和多余 @，剩下的才是要给模型的话。"""
    t = text.strip()
    for w in [nickname, *wake_words]:
        if w and t.startswith(w):
            t = t[len(w):].lstrip("，,：: ")
    t = re.sub(r"^@\S+\s*", "", t)
    return t.strip()


def _media_kinds(media: list[dict]) -> list[str]:
    """把 media 段按去重顺序转成「图/表情包/语音」标签。"""
    kinds: list[str] = []
    for seg in media or []:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "record":
            label = "[语音]"
        elif kind == "image":
            label = "[图片]"
        elif kind == "_media":
            label = "[表情包]" if data.get("kind") == "sticker" else "[图片]"
        else:
            continue
        if label not in kinds:
            kinds.append(label)
    return kinds


def _media_note(media: list[dict]) -> str:
    """只发了图/表情包/语音、没写字时，给自己留一笔，免得下次不知道发过。"""
    kinds = _media_kinds(media)
    return "（我自己发了一条：" + "".join(kinds) + "）" if kinds else ""


def _self_media_recents(media: list[dict]) -> list[dict]:
    """把自己发出去的图/表情包整理成 recent_images 条目（self=True）。

    有 http(s) url 的可以直接给主模型看；本地产生的（base64:// / file:// /
    没 url 的表情包）只记占位，至少让下一轮知道「刚刚我自己也发了一张」。
    """
    out: list[dict] = []
    for seg in media or []:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "_media":
            is_sticker = data.get("kind") == "sticker"
            raw_url = str(data.get("url") or "")
            url = raw_url if raw_url.startswith(("http://", "https://")) else ""
            entry = {"url": url, "who": "我（大肥鱼）", "uid": "", "self": True,
                     "ts": time.time(), "desc": "",
                     "kind": "sticker" if is_sticker else "image"}
            if not url:
                entry["placeholder"] = True
            out.append(entry)
        elif kind == "image":
            raw_file = str(data.get("file") or "")
            url = raw_file if raw_file.startswith(("http://", "https://")) else ""
            entry = {"url": url, "who": "我（大肥鱼）", "uid": "", "self": True,
                     "ts": time.time(), "desc": "", "kind": "image"}
            if not url:
                entry["placeholder"] = True
            out.append(entry)
    return out


@on_message(priority=90)
async def ai_chat(ctx):
    cfg = ctx.config
    ev = ctx.event

    # ---- 1. 普通群消息也旁听，由模型决定是否开口
    if ev.type != "message" or ev.user_id == ev.self_id or ctx.matched_command:
        return
    if ev.is_group:
        if cfg.get("chat.reply_mode", "at_or_wake") == "never":
            return
    elif not cfg.get("chat.reply_in_private", True):
        return
    ctx.state["group_observe"] = ev.is_group

    text = _clean(ctx.plain, cfg.get("bot.wake_words", []), cfg.get("bot.nickname", ""))
    # 只发了一张图、没配字：把视觉描述当成它听到的话
    if not text:
        desc = getattr(ev, "image_desc", "")
        if desc:
            text = "（不说话，只发了张图）" + desc
    if not text:
        return

    # ---- 2. 攒话：等对方把这一段话说完（最多等 settle_max 秒）
    #
    # 这一步必须放到后台任务里：OneBot 的读循环是逐条 await 事件的，
    # ai_chat 一 await 睡觉，后面那半句话根本进不来，攒话就永远攒不到。
    key = ctx.session_id
    settle = float(cfg.get("chat.settle_sec", 1.6))
    settle_max = float(cfg.get("chat.settle_max", 4.5))
    if settle <= 0 or settle_max <= 0:
        return await _converse(key, ctx, text)

    # 群里没人叫它的普通消息：不是来一句回一句，而是攒几句、或者等很久都只有一句，再一起回。
    # 被 @ / 叫名字的走下面原来的短攒话；已经在攒的批次里再来的消息一律并进去。
    addressed = bool(getattr(ev, "to_me", False) or ctx.state.get("wake"))
    batch = _BATCH.get(key)
    settle_state = _SETTLE.get(key)
    if ev.is_group and cfg.get("chat.batch_enabled", True):
        in_batch = batch is not None and batch["running"]
        in_settle = bool(settle_state and settle_state["running"])
        if in_batch or (not addressed and not in_settle):
            return _batch_add(key, ctx, text, addressed, cfg)

    state = _SETTLE.setdefault(key, {"seq": 0, "ctx": ctx, "text": text,
                                     "first": 0.0, "running": False})
    state["seq"] += 1
    state["ctx"], state["text"] = ctx, text
    if state["running"]:
        log.debug(f"等待对方说完，先并入下一条：{key}")
        return None
    if not state["first"]:
        state["first"] = time.monotonic()
    state["running"] = True
    return tasks.spawn(_settle_then_talk(key, settle, settle_max), name="ai_chat_settle")


def _name_for_uid(bot, session_id: str, uid, fallback: str = "") -> str:
    """从最近聊天里把这个 QQ 号的昵称找出来；找不到就用传进来的，最后退回号码。"""
    try:
        for row in reversed(bot.store.window(session_id, 120)):
            if str(row.get("uid") or "") == str(uid):
                who = " ".join(str(row.get("who") or "").split())
                if who:
                    return who
    except Exception:
        pass
    return fallback or str(uid)


async def poke_turn(ctx):
    """被拍一拍 = 被点名。走被叫的快速路径，回给拍它的人。"""
    key = ctx.session_id
    if not key:
        return None
    ctx.state["wake"] = True
    ctx.state["poked"] = True
    fallback = str(getattr(ctx, "sender_name", "") or "")
    if fallback.isdigit():
        fallback = ""
    name = _name_for_uid(ctx.bot, key, ctx.user_id, fallback)
    text = f"（{name}拍了拍你，意思跟 @ 你一下差不多，在等你接话）"
    try:
        ctx.bot.store.append(key, "user", text, who=name, uid=ctx.user_id)
        ctx.bot.topic.seen(key, name, text)
    except Exception as e:
        log.debug(f"记录拍一拍失败：{type(e).__name__}")
    return await _converse(key, ctx, text)


def _pair(cfg, key: str, default: tuple) -> tuple[float, float]:
    """读 [低, 高] 区间配置；写成单个数字就当固定值。"""
    raw = cfg.get(key, default)
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except Exception:
        try:
            lo = hi = float(raw)
        except Exception:
            lo, hi = float(default[0]), float(default[1])
    return (lo, hi) if lo <= hi else (hi, lo)


def _batch_add(key: str, ctx, text: str, addressed: bool, cfg):
    now = time.monotonic()
    st = _BATCH.get(key)
    start = st is None or not st["running"]
    if start:
        mlo, mhi = _pair(cfg, "chat.batch_msgs", (2, 4))
        qlo, qhi = _pair(cfg, "chat.batch_quiet", (6, 14))
        wlo, whi = _pair(cfg, "chat.batch_max_wait", (60, 150))
        st = {"items": [], "first": now, "last": now, "running": True,
              "addressed": False, "addressed_at": 0.0,
              "need": random.randint(int(mlo), max(int(mlo), int(mhi))),
              "quiet": random.uniform(qlo, qhi),
              "max_wait": random.uniform(wlo, whi)}
        _BATCH[key] = st
    st["items"].append((ctx, text, addressed))
    st["last"] = now
    if addressed and not st["addressed"]:
        st["addressed"], st["addressed_at"] = True, now
    if start:
        log.debug(f"开始攒话：{key}（凑 {st['need']} 句 / 静 {st['quiet']:.0f}s / 最多等 {st['max_wait']:.0f}s）")
        return tasks.spawn(_batch_then_talk(key, st, cfg), name="ai_chat_batch")
    return None


async def _batch_then_talk(key: str, st: dict, cfg):
    tick = max(0.05, float(cfg.get("chat.batch_tick", 1.0)))
    settle = float(cfg.get("chat.settle_sec", 1.6))
    settle_max = float(cfg.get("chat.settle_max", 4.5))
    try:
        while True:
            await asyncio.sleep(tick)
            now = time.monotonic()
            quiet_for = now - st["last"]
            if st["addressed"]:
                # 中途有人叫它了：按被叫的节奏尽快回
                if quiet_for >= settle or now - st["addressed_at"] >= settle_max:
                    break
                continue
            if len(st["items"]) >= st["need"] and quiet_for >= st["quiet"]:
                break
            if now - st["first"] >= st["max_wait"]:
                break
    finally:
        st["running"] = False
        if _BATCH.get(key) is st:
            _BATCH.pop(key, None)
    items = st["items"]
    if not items:
        return
    try:
        await _fire_batch(key, items, st["addressed"])
    except Exception as e:
        log.error(f"攒话回复失败：{type(e).__name__} {e}")


async def _fire_batch(key: str, items: list, addressed: bool):
    ctx, text, _ = items[-1]
    names: list[str] = []
    for c, _t, _a in items:
        n = " ".join(str(getattr(c, "sender_name", "") or "").split())
        if n and n not in names:
            names.append(n)
    hot = [it for it in items if it[2]]
    pick = hot[-1] if hot else random.choice(items[-3:])
    ctx.state["batch"] = {"count": len(items), "names": names, "addressed": addressed,
                          "target": pick[0], "target_text": pick[1]}
    log.info(f"攒话结束 {key}：{len(items)} 句 · {'、'.join(names[:5])}")
    await _converse(key, ctx, text)


_EXPLAIN_CUES = ("为什么", "为啥", "讲讲", "解释", "说清楚", "原理", "区别", "怎么", "如何",
                 "推荐", "说说", "介绍", "分析", "教我", "对比", "讲讲", "是什么")


def is_explain_request(text: str) -> bool:
    """在解释、讲题、推荐理由这类内容多的请求上，不压句数。"""
    t = str(text or "")
    return any(c in t for c in _EXPLAIN_CUES)


def roll_length(cfg, explain: bool = False) -> int:
    """这条回复写几句。闲聊 1~5 句随机；解释/推荐类至少 3 句，免得几层意思塞进一句。"""
    raw = cfg.get("reply.length_weights", [3, 3, 2, 1, 1])
    try:
        weights = [max(0.0, float(x)) for x in list(raw)[:5]]
    except Exception:
        weights = [3.0, 3.0, 2.0, 1.0, 1.0]
    if not weights or sum(weights) <= 0:
        weights = [3.0, 3.0, 2.0, 1.0, 1.0]
    if explain:
        weights = [0.0, 0.0] + weights[2:]
        if sum(weights) <= 0:
            weights = [0.0, 0.0, 1.0, 1.0, 1.0]
    return random.choices(range(1, len(weights) + 1), weights=weights)[0]


def _roll_reply_style(ctx, cfg):
    """这条回复要不要 @、要不要引用，随机定；回给攒话里的哪一位也在这里定。"""
    ev = ctx.event
    if not ev.is_group:
        return
    batch = ctx.state.get("batch") or {}
    target = batch.get("target") or ctx
    addressed = bool(batch.get("addressed") or getattr(ev, "to_me", False)
                     or ctx.state.get("wake"))
    if addressed:
        at_p = float(cfg.get("reply.at_chance_addressed", 0.5))
        q_p = float(cfg.get("reply.quote_chance_addressed", 0.6))
    else:
        at_p = float(cfg.get("reply.at_chance", 0.08))
        q_p = float(cfg.get("reply.quote_chance_batch" if batch.get("count", 1) > 1
                            else "reply.quote_chance", 0.4 if batch.get("count", 1) > 1 else 0.3))
    at, quote = random.random() < at_p, random.random() < q_p
    rate = float(cfg.get("reply.question_rate", 0.3))
    ctx.state["reply_question"] = random.random() < rate
    target.state["at_override"], target.state["quote_override"] = at, quote
    ctx.state["reply_target"] = target
    ctx.state["reply_style"] = {
        "at": at, "quote": quote, "same": target is ctx,
        "name": " ".join(str(getattr(target, "sender_name", "") or "").split()),
        "text": batch.get("target_text", "") if target is not ctx else ""}


async def _settle_then_talk(key: str, settle: float, settle_max: float):
    """等对方把话说完，再走正常的一轮对话。异常只记日志，别丢进事件循环。"""
    state = _SETTLE.setdefault(key, {"seq": 0, "ctx": None, "text": "",
                                     "first": 0.0, "running": False})
    try:
        while True:
            seq = state["seq"]
            elapsed = time.monotonic() - state["first"]
            wait = min(settle, max(0.0, settle_max - elapsed))
            if wait > 0:
                await asyncio.sleep(wait)
            if state["seq"] != seq and time.monotonic() - state["first"] < settle_max:
                continue
            break
        ctx, text = state["ctx"], state["text"]
        if ctx is None or not text:
            return
        # 先放开攒话状态再开聊：生成和打字期间新来的消息要能开下一轮，不能被吞掉
        state["running"] = False
        state["ctx"], state["text"], state["first"] = None, "", 0.0
        await _converse(key, ctx, text)
    except Exception as e:
        log.error(f"攒话回复失败：{type(e).__name__} {e}")
    finally:
        state["running"] = False
        state["ctx"], state["text"], state["first"] = None, "", 0.0


async def _converse(key: str, ctx, text: str):
    """会话锁 + 合并排队消息，真正的对话轮次都在这里。"""
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    if lock.locked():
        _PENDING[key] = (ctx, text)
        log.debug(f"AI 上一条还没收尾，合并到最新消息：{key}")
        return
    async with lock:
        try:
            while True:
                await _reply_turn(ctx, text)
                pending = _PENDING.pop(key, None)
                if pending is None:
                    break
                ctx, text = pending
        finally:
            _PENDING.pop(key, None)


async def _reply_turn(ctx, text):
    cfg, ev = ctx.config, ctx.event
    key = ctx.session_id
    history = ctx.bot.store.history(key)
    t0 = time.time()
    started = time.monotonic()
    inbound = getattr(ctx.bot, "inbound", {}).get(key, 0)
    timeout = float(cfg.get("chat.reply_timeout", 120))
    log.debug(f"AI 处理 {key} ← {text[:40]!r}")
    _roll_reply_style(ctx, cfg)
    explain = is_explain_request(text)
    ctx.state["explain"] = explain
    # 对方明确要表情包：让 pick_sticker 不受「随机低频」闸限制（被邀请 vs 自己顺手发）
    if re.search(r"表情包|斗图|(?:发|来|整|搞).{0,4}表情|(?:发|来)张图", str(text or "")):
        ctx.state["_sticker_requested"] = True
    ctx.state["length_target"] = roll_length(cfg, explain)
    # 对方点名要语音：这条无论如何都回语音（不受 auto_chance 限制）
    if tts.should_force_voice(cfg, text):
        ctx.state["force_voice"] = True
        spoken = tts.explicit_speech_text(text, cfg.get("tts.max_chars", 80))
        if spoken:
            ctx.state["force_voice_text"] = spoken
        log.info(f"对方要求语音回复 {key}：{spoken[:20]!r}" if spoken
                 else f"对方要求语音回复 {key}")
    log.debug(f"句数目标 {key}：{ctx.state['length_target']}{'（解释类）' if explain else ''}")

    try:
        # 模型或某个工具卡住时不能把这个会话永久锁死
        answer, media = await asyncio.wait_for(
            ctx.bot.agent.run(ctx, text, history), timeout=timeout)
    except asyncio.TimeoutError:
        log.warn(f"AI 超过 {timeout:.0f}s 没完成，放弃这条并解锁：{key}")
        return
    media = msg.normalize(media)
    if not answer.strip() or answer.strip().casefold() == "<pass>":
        # A selected video is not consent to share after the model declines.
        # Preserve the existing image/sticker-only reaction behavior.
        media, _ = prepare_bili_delivery(media, include=False)
    log.debug(f"AI 完成 {key} · {time.time() - t0:.1f}s · "
              f"{answer[:30]!r} · 媒体 {len(media)}")

    addressed = bool(getattr(ev, "to_me", False) or ctx.state.get("wake")
                     or (ctx.state.get("batch") or {}).get("addressed"))
    no_silence = bool(cfg.get("chat.no_silence", True))
    # 被点名时不允许沉默：模型想 pass 也被拦下来，换一句保底应付
    if answer.strip().casefold() == "<pass>":
        if not media and not ctx.state.get("force_voice"):
            if no_silence and addressed:
                answer = random.choice(_fallback_lines)
                log.debug(f"被点名但 AI 交回 <pass>，强制保底回应：{key}")
            else:
                log.debug(f"AI 旁听后选择沉默：{key}")
                return
        else:
            # 只发一张表情包/图、不配字，是正常的「反应」；别把已经挑好的媒体丢掉
            answer = ""
    if not answer and not media and not ctx.state.get("force_voice"):
        if no_silence and addressed:
            answer = random.choice(_fallback_lines)
            log.debug(f"被点名但 AI 空回复，强制保底回应：{key}")
        else:
            log.debug(f"AI 空回复（不发送）：{key}")
            return
    arrivals = getattr(ctx.bot, "inbound", {}).get(key, 0) - inbound
    # 只有确实有更新的消息在排队时才作废，否则（比如两条指令插进来）会把回复吞掉
    if arrivals >= 2 and key in _PENDING and not media and not addressed:
        log.debug(f"AI 上下文已过时，留给最新消息：{key}")
        return
    if ev.is_group and not ctx.bot._pass_rate(ev):
        log.debug(f"AI 已决定加入，但达到群发送限额：{key}")
        return
    # 破折号、自我引號、「行，」開頭這類人機味在發送前直接清掉
    polished = polish(answer)
    if polished != answer:
        log.debug(f"风格清理 {key}：{answer[:30]!r} → {polished[:30]!r}")
    answer = polished
    if not ctx.state.get("reply_question") and answer:
        shorter = drop_trailing_question(answer)
        if shorter != answer:
            log.debug(f"去掉追问收尾 {key}：{answer[-20:]!r} → {shorter[-20:]!r}")
            answer = shorter
    if answer and cfg.get("reply.length_enforce", True):
        # 只封顶不硬塞：内容多就让它分成几句自然写
        # ⭐ 2026-10-24：默认上限 5 → 3（实测中位 56 字、平均 2.4 句，读起来像总结报告）
        ceiling = int(cfg.get("reply.length_max", 3))
        capped = cap_sentences(answer, ceiling, max_chars=320)
        if capped != answer:
            log.debug(f"句数截到 {ceiling} 句 {key}：{answer[:30]!r} → {capped[:30]!r}")
            answer = capped
    if not answer.strip():
        media, _ = prepare_bili_delivery(media, include=False)
    if not answer and not media:
        log.debug(f"AI 回覆清理後為空：{key}")
        return
    if len(answer) > 900:
        answer = answer[:900] + "……"

    # 语音：先看「对方点名要语音」，再看概率自动转。不按字数拒绝，
    # 说得多就分成连续几条语音发出去。
    max_chars = int(cfg.get("tts.max_chars", 0) or 0)
    force_text = str(ctx.state.get("force_voice_text") or "")
    records: list[dict] = []
    text_out = answer
    if ctx.state.get("force_voice") and not ctx.state.get("_spoke"):
        if force_text:
            # 对方点明了要念的内容：念那段，模型的话照常打字发
            records = await tts.record_segments(cfg, force_text)
        elif answer:
            voice_text, rest = tts.split_for_voice(answer, max_chars)
            if voice_text:
                records = await tts.record_segments(cfg, voice_text)
            if records:
                text_out = rest
    elif answer and tts.should_auto_voice(cfg, answer, has_media=bool(media),
                                          used_speak=bool(ctx.state.get("_spoke"))):
        records = await tts.record_segments(cfg, answer)
        if records:
            text_out = ""
    if records:
        media = list(records) + list(media)
        log.info(f"语音回复 {key} · {len(answer)} 字 / {len(records)} 条 · "
                 f"{'强制' if ctx.state.get('force_voice') else '自动'}")

    payload = []
    if text_out:
        payload.append({"type": "text", "data": {"text": text_out}})
    media, bili_pending = prepare_bili_delivery(media)
    payload.extend(media)
    if not payload:
        return

    try:
        replier = ctx.state.get("reply_target") or ctx
        if records and not text_out:
            # 语音必须单独一条，且不能带 @/引用头
            result = await replier.send(payload, elapsed=time.monotonic() - started)
        else:
            result = await replier.reply(payload, elapsed=time.monotonic() - started)
        delivered = result is not None
    except Exception as e:
        # delivered=True 表示请求已经递给协议端，只是回执没等到（断连 / 超时）。
        # 这种情况话其实已经说出去了，必须算进「我说过的话」。
        delivered = bool(getattr(e, "delivered", False))
        log.error(("已发出但等不到回执：" if delivered else "发送失败：") + str(e))

    if delivered and bili_pending:
        commit_bili_delivery(bili_pending, replier.session_id)

    # 用户消息已经在 core 里全量入库了，这里只记自己说的。
    # 漏记一条的代价很大：模型下一句会以为自己没答过，于是回头再补一遍，
    # 表现出来就是「每句都在翻旧话题」。只发图/表情包/语音也要记一笔，
    # 不然她根本不知道那条是自己发的。
    # 自己发出去的图/表情包也进 recent_images，标记 self=True，免得下一轮
    # 提到「刚才那张图」时被当成别人发的。
    if delivered and media:
        try:
            from pawbot import perceive
            recents = _self_media_recents(media)
            if recents:
                perceive._push_recent(ctx.bot, key, recents)
        except Exception as e:
            log.debug(f"记录自己发的图失败：{type(e).__name__}")
    media_note = _media_note(media)
    self_note = answer or media_note
    if answer and media and media_note:
        # 说了话又带了图/表情包/语音：把「附了什么」补进自己的记录，别下次只当纯文字
        self_note = answer + "（并附了：" + "".join(_media_kinds(media)) + "）"
    if self_note and delivered:
        ctx.bot.store.append(key, "assistant", self_note, who="我")
        # 自己说过的话也要记进话题，不然会重复自己
        ctx.bot.topic.seen(key, "我", self_note, is_self=True)
    if not delivered:
        return
    ctx.state["ai_spoke"] = True
    emotion = getattr(ctx.bot, "emotion", None)
    if emotion is not None:
        emotion.turn()
        emotion.save()

    log.info(f"AI 回复 {key} · {len(answer)} 字 · {(time.time()-t0)*1000:.0f}ms")

    # 这一轮结束后更新滚动小结（后台做，不拖慢回复，也不发给任何人）
    try:
        from pawbot import turns
        tasks.spawn(turns.update(ctx.bot, key), name="turns_update")
    except Exception as e:
        log.debug(f"小結调度失败：{type(e).__name__}")


@on_command("清空", aliases=("重置", "forget"), priority=5)
async def clear_memory(ctx):
    clear_session = getattr(ctx.bot, "clear_session", None)
    if clear_session:
        clear_session(ctx.session_id)
    else:
        ctx.bot.store.clear(ctx.session_id)
        ctx.bot.topic.groups.pop(ctx.session_id, None)
        ctx.bot.topic.save(force=True)
        for state_name in ("inbound", "last_msg_id", "last_msg_text"):
            getattr(ctx.bot, state_name, {}).pop(ctx.session_id, None)
    await ctx.reply("这段对话我忘掉了。")
    ctx.stop()


@on_command("记住", priority=5)
async def remember_cmd(ctx):
    text = ctx.arg_text
    if not text:
        await ctx.reply("要记什么？说清楚点。")
        return
    ok = ctx.bot.memory.remember(text, "fact")
    await ctx.reply("记下了。" if ok else "这条已经在记忆里了。")
    ctx.stop()
