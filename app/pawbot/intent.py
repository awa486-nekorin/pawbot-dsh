"""用「决策模型」判断主人这句话是不是在指挥 AI 去干活，以及具体干哪几件。

为什么不用正则：说话太自由（「你去评论区多看看」「刷多15个视频，顺便学学他们说话」），
脚本判不准；决策模型直接给概率，> 阈值就执行，多个动作还能同时命中。

流程：
    主人的消息 → decider.decide(每个动作一个概率 + 数量) → 概率 > 0.5 的动作全做
失败/没 key/全低概率 → 什么都不做，交给正常聊天。

配置（tools.*）：
    admin_intent_enabled   总开关（2026-11-03 起默认 true，与云端等价）
    admin_intent_threshold 概率阈值（默认 0.5，即「大于 50% 就去做」）
    admin_intent_timeout   决策模型超时（默认 12s）
决策模型本身沿用 emotion.decider_*（perplexity/pplx-decider-v1.2），
key 走 decider.resolve_key（config → 环境变量 → ~/.pawbot/decider.key）。

对外写入（发评论/弹幕/自动互动/删评论）一律转给 ``bili_scroll.action``，由本地
``bili_gate`` 校验**绑定动作+目标**的进程内凭证 + 审计（额度默认 0 = 不限）；
本模块**不直接调用 bili_extra 的写函数**。自然语言删评论（云端 delcomment）已恢复：
「删评论 BV号」→ delete_on_video、「第 2 条」→ delete_by_index、「全部」→
delete_recent(999)、否则 delete_recent(n)。凭证仍然只能由主人本人当轮消息换来；
没有 ctx / 不是主人时闸门会拒绝，删除入口只动账本里记着的、自己发的评论。

弹幕/评论的正文分两种：主人话里**明确写出来的原文**（``_payload`` 摘得到）
保留原样、不改写，成功后记账；主人没给原文则是模型成文，走
``bili_voice.compose_text``（人设 + 近期自发言 + 反公式化检查），不过就不发。
无论哪条路径，确认发出后都用 ``bili_voice.note_sent`` 记进「近期自发言」。
"""

from __future__ import annotations

import re

from . import decider, bili_gate
from .log import log

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

TASK_ORDER = ("scroll", "learn", "auto", "delcomment", "danmaku", "comment")

_STRIP_PREFIX = {
    "danmaku": re.compile(r"^\s*(?:去|帮我|给我)?\s*(?:发|扔|打)?\s*(?:个|条)?\s*弹幕[：:\s]*(.*)$", re.S),
    "comment": re.compile(r"^\s*(?:去|帮我|给我)?\s*(?:发|留)?\s*(?:个|条)?\s*评论[：:\s]*(.*)$", re.S),
}


def _cfg(bot, key: str, default=None):
    try:
        return bot.config.get("tools." + key, default)
    except Exception:
        return default


def _questions() -> dict:
    return {
        "scroll": {
            "type": "noul",
            "instructions": "主人是不是要让 AI 去 B 站刷视频 / 看视频？",
        },
        "learn": {
            "type": "noul",
            "instructions": "主人是不是要让 AI 去学评论区/别人的说话方式，记下来让自己更像真人？",
        },
        "auto": {
            "type": "noul",
            "instructions": "主人是不是要让 AI 去评论区互动（回别人的评论、发评论、逛评论区）？",
        },
        "delcomment": {
            "type": "noul",
            "instructions": "主人是不是要让 AI 删掉/撤掉她已经发出去的评论（不是发新的）？",
        },
        "danmaku": {
            "type": "noul",
            "instructions": "主人是不是要让 AI 发弹幕？",
        },
        "comment": {
            "type": "noul",
            "instructions": "主人是不是要让 AI 在视频下面发一条评论？",
        },
        "count": {
            "type": "choice",
            "instructions": "如果提到数量，要几个视频 / 看几个？按话里说的数；没提就 3。",
            "criteria": {"1": "1 个", "2": "2 个", "3": "3 个", "5": "5 个",
                         "10": "10 个", "15": "15 个", "20": "20 个",
                         "30": "30 个", "50": "50 个", "100": "100 个"},
        },
    }


def _count_from_text(text: str) -> int | None:
    """话里明确说了数字就用它（只做提取，不做判断）。"""
    m = re.search(r"(?<!\d)(\d{1,3})\s*(?:个|条|只|部)", text or "")
    if m:
        return max(1, int(m.group(1)))          # 不设上限（云端等价）
    m = re.search(r"([零一二两三四五六七八九十])\s*(?:个|条|只|部)", text or "")
    if m:
        return _CN_NUM.get(m.group(1), 3)
    return None


def _payload(action: str, text: str) -> str:
    pat = _STRIP_PREFIX.get(action)
    if not pat:
        return ""
    m = pat.match(text or "")
    if not m:
        return ""
    return m.group(1).strip().strip("：:，,。.!！~～ ")[:200]


async def judge(bot, text: str, context: str = "") -> dict:
    """问决策模型：这句话要她做什么？返回 {ok, hit, tasks:[{action,count,p}], ...}。"""
    if _cfg(bot, "admin_intent_enabled", True) is not True:
        return {"ok": False, "reason": "意图判断没开", "hit": False, "tasks": []}

    if not decider.resolve_key(bot.config):
        return {"ok": False, "reason": "没有决策模型 key", "hit": False, "tasks": []}

    state = "\n".join([
        "角色：大肥鱼，QQ 群里的 AI 群友；主人可以让她去 B 站干活："
        "刷视频、看评论区、学别人怎么说话、回评论、发评论、发弹幕。",
        (f"最近对话：{str(context or '')[-500:]}" if context else ""),
        f"主人说：{str(text or '')[:600]}",
    ]).strip()

    threshold = float(_cfg(bot, "admin_intent_threshold", 0.5) or 0.5)
    try:
        answers = await decider.decide(
            bot.config, state, _questions(),
            timeout=float(_cfg(bot, "admin_intent_timeout", 8) or 12))
    except decider.DeciderError as e:
        log.debug(f"意图判断失败：{e.code}")
        return {"ok": False, "reason": f"决策模型 {e.code}", "hit": False, "tasks": []}
    except Exception as e:  # noqa: BLE001
        log.warn(f"意图判断异常：{type(e).__name__}")
        return {"ok": False, "reason": type(e).__name__, "hit": False, "tasks": []}

    probs = {a: decider._noul(answers.get(a), 0.0) for a in TASK_ORDER}
    raw_count, _, _ = decider._choice(answers.get("count"), "3")
    count = _count_from_text(text) or (int(raw_count) if str(raw_count).isdigit() else 3)
    count = max(1, count)

    tasks = [{"action": a, "p": probs[a], "count": count}
             for a in TASK_ORDER if probs[a] >= threshold]
    # 「删评论」和「发评论」同时命中时，只删不发（主人不可能既要发又要删）。
    if any(t["action"] == "delcomment" for t in tasks):
        tasks = [t for t in tasks if t["action"] != "comment"]
    verdict = {
        "ok": True, "hit": bool(tasks), "tasks": tasks, "threshold": threshold,
        "probs": probs, "count": count, "text": str(text or ""),
    }
    log.debug("意图判断：" + " ".join(f"{a}={probs[a]:.2f}" for a in TASK_ORDER)
              + f" count={count} → {'/'.join(t['action'] for t in tasks) or '不执行'}")
    return verdict


async def run(bot, verdict: dict, reason: str = "QQ 命令", ctx=None) -> dict:
    """按决策结果依次执行动作，合并结果。永不抛。

    ``ctx`` 是主人这条真实消息的上下文；所有对外写只用它换 ``bili_gate`` 的
    进程内凭证。没有 ctx / 不是主人本人时，写动作会被闸门拒绝，不会裸调后端。
    """
    results = []
    count = max(1, int(verdict.get("count") or 3))
    text = str(verdict.get("text") or "")

    from . import bili_learn, bili_scroll

    def _grant(action: str, args: dict):
        """主人这条真实消息 → 一枚**只对这个动作+目标有效**的写凭证。"""
        if ctx is None:
            return None
        try:
            return bili_gate.owner_turn_grant(
                bot, ctx, action=action, target=bili_gate.target_key(action, args))
        except Exception:
            return None

    def _last_video() -> tuple[str, str]:
        """最近一篇刷片笔记的视频：BV 号 + 标题（标题只当上下文，不脑补内容）。"""
        for n in bili_scroll.recent_notes(bot, limit=5):
            try:
                with open(n["path"], "r", encoding="utf-8") as f:
                    raw = f.read(3000)
            except OSError:
                continue
            m = re.search(r"BV[0-9A-Za-z]{10}", raw)
            if not m:
                continue
            mt = re.search(r"^#\s+(.+)$", raw, re.M)
            return m.group(0), (mt.group(1).strip() if mt else "")
        return "", ""

    async def _compose(action_name: str, bvid: str, title: str) -> tuple[str, list[str]]:
        """主人没给原文时才让她自己写；写完必须过 bili_voice 的同一套检查。

        返回 ``(文本, 放弃原因)``；文本为空 = 放弃，调用方**不许发**。
        """
        from . import bili_auto, bili_voice
        if action_name == "danmaku":
            task = "发一条很短很口语的弹幕（20 字内，别像总结）"
            limit = 25
        else:
            task = "在这个视频下面留一条你的评论（像普通观众，别客套）"
            limit = 80
        context = f"视频标题：{title[:80]}" if title else ""
        try:
            draft = await bili_voice.compose_text(
                bot, task=task, context=context, bvid=bvid,
                kind=action_name, max_chars=limit,
                style=bili_auto._style_rules(bot))
        except Exception as e:  # noqa: BLE001
            log.warn(f"自然语言派活写不出{action_name}：{type(e).__name__}")
            return "", [f"生成失败（{type(e).__name__}）"]
        if str(draft or ""):
            return str(draft), []
        reasons = list(getattr(draft, "violations", ()) or ())
        return "", (reasons or ["没写出合格的一句"])

    def _note(result: dict, kind: str, bvid: str, text: str) -> None:
        """确认发出后才记账（失败/未确认不记）：下一轮「近期自发言」看得到。"""
        if not (isinstance(result, dict) and result.get("ok") is True):
            return
        try:
            from . import bili_voice
            bili_voice.note_sent(bot, result, kind=kind, bvid=bvid, text=text)
        except Exception as e:  # noqa: BLE001
            log.debug(f"记录已发{kind}失败：{type(e).__name__}")

    async def _write(action_name: str, bvid: str, payload: str):
        """发一条弹幕/评论；成功就记账。主人给的原文也走这里，但不改写。

        弹幕后端硬上限 60 字（与 ``//发弹幕`` 一致）：**实际发出去的是截断后的
        那段**，所以记账和返回给调用方的 ``text`` 也必须用同一段，不能记全文。
        """
        import random

        if action_name == "danmaku":
            发出 = str(payload or "")[:60]
            args = {"bvid": bvid, "text": 发出,
                    "progress": random.randint(8, 180)}
            r = await bili_scroll.action(
                bot, "send_danmaku", args, grant=_grant("send_danmaku", args))
            _note(r, action_name, bvid, 发出)
            return {**r, "text": 发出}
        args = {"bvid": bvid, "text": payload[:200]}
        r = await bili_scroll.action(
            bot, "comment", args, grant=_grant("comment", args))
        _note(r, action_name, bvid, payload)
        return {**r, "text": payload}

    for task in verdict.get("tasks") or []:
        action = task.get("action")
        n = max(1, int(task.get("count") or count))
        try:
            if action == "scroll":
                r = await bili_scroll.run_round(bot, want=n)
            elif action == "learn":
                r = await bili_learn.run_round(bot, videos=max(1, min(6, n // 2 or 3)),
                                               reason=reason)
            elif action == "auto":
                from . import bili_auto
                r = await bili_auto.run_round(
                    bot, reason=reason,
                    grant_factory=(_grant if ctx is not None else None))
            elif action == "delcomment":
                # 云端原版语义：按话里的目标选删除入口。仍然走 bili_scroll.action
                # → bili_gate：只有主人本人当轮消息换来的凭证才放行，批量入口
                # 只删账本里记着的 / 评论区里 mid 是自己的评论。
                txt = str(text or "")
                bv = re.search(r"BV[0-9A-Za-z]{10}", txt)
                idx = re.search(r"第\s*(\d{1,3})\s*[条个]", txt)
                if bv:
                    删除动作, 删除参数 = "delete_on_video", {"bvid": bv.group(0)}
                elif idx:
                    删除动作, 删除参数 = "delete_by_index", {"index": int(idx.group(1))}
                elif any(k in txt for k in ("全部", "所有", "都删", "全删", "清理")):
                    删除动作, 删除参数 = "delete_recent", {"n": 999}
                else:
                    删除动作, 删除参数 = "delete_recent", {"n": max(1, n)}
                r = await bili_scroll.action(
                    bot, 删除动作, 删除参数,
                    grant=_grant(删除动作, 删除参数))
            elif action == "danmaku":
                bv, title = _last_video()
                if not bv:
                    r = {"ok": False, "reason": "还没刷过 B 站，没有可发弹幕的视频"}
                else:
                    # 主人话里明确写了弹幕原文（``_payload`` 能摘出来）：保留原文、
                    # 不改写；没给原文才是模型成文，必须过同一套人味检查。
                    payload = _payload("danmaku", text)
                    if payload:
                        r = await _write("danmaku", bv, payload)
                    else:
                        payload, reasons = await _compose("danmaku", bv, title)
                        if payload:
                            r = await _write("danmaku", bv, payload)
                        else:
                            r = {"ok": False, "voice_rejected": reasons,
                                 "reason": "没写出合格的弹幕（反公式化检查没过），没发"}
            elif action == "comment":
                bv, title = _last_video()
                if not bv:
                    r = {"ok": False, "reason": "还没刷过 B 站，没有可评论的视频"}
                else:
                    payload = _payload("comment", text)
                    if payload:
                        r = await _write("comment", bv, payload)
                    else:
                        payload, reasons = await _compose("comment", bv, title)
                        if payload:
                            r = await _write("comment", bv, payload)
                        else:
                            r = {"ok": False, "voice_rejected": reasons,
                                 "reason": "没写出合格的评论（反公式化检查没过），没发"}
            else:
                r = {"ok": False, "reason": f"未知动作：{action}"}
        except Exception as e:
            r = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        results.append({"action": action, "count": n, "result": r})

    return {"ok": True, "tasks": results}
