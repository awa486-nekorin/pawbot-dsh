"""用「决策模型」判断主人这句话是不是在指挥 AI 去干活，以及具体干哪几件。

为什么不用正则：说话太自由（「你去评论区多看看」「刷多15个视频，顺便学学他们说话」），
脚本判不准；决策模型直接给概率，> 阈值就执行，多个动作还能同时命中。

流程：
    主人的消息 → decider.decide(每个动作一个概率 + 数量) → 概率 > 0.5 的动作全做
失败/没 key/全低概率 → 什么都不做，交给正常聊天。

配置（tools.*）：
    admin_intent_enabled   总开关（默认 true）
    admin_intent_threshold 概率阈值（默认 0.5，即「大于 50% 就去做」）
    admin_intent_timeout   决策模型超时（默认 12s）
决策模型本身沿用 emotion.decider_*（perplexity/pplx-decider-v1.2），
key 走 decider.resolve_key（config → 环境变量 → ~/.pawbot/decider.key）。
"""

from __future__ import annotations

import re

from . import decider
from .log import log

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

TASK_ORDER = ("scroll", "learn", "auto", "danmaku", "comment")

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
                         "10": "10 个", "15": "15 个", "20": "20 个"},
        },
    }


def _count_from_text(text: str) -> int | None:
    """话里明确说了数字就用它（只做提取，不做判断）。"""
    m = re.search(r"(?<!\d)(\d{1,2})\s*(?:个|条|只|部)", text or "")
    if m:
        return max(1, min(20, int(m.group(1))))
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
    if not bool(_cfg(bot, "admin_intent_enabled", True)):
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
    count = max(1, min(20, count))

    tasks = [{"action": a, "p": probs[a], "count": count}
             for a in TASK_ORDER if probs[a] >= threshold]
    verdict = {
        "ok": True, "hit": bool(tasks), "tasks": tasks, "threshold": threshold,
        "probs": probs, "count": count, "text": str(text or ""),
    }
    log.debug("意图判断：" + " ".join(f"{a}={probs[a]:.2f}" for a in TASK_ORDER)
              + f" count={count} → {'/'.join(t['action'] for t in tasks) or '不执行'}")
    return verdict


async def run(bot, verdict: dict, reason: str = "QQ 命令") -> dict:
    """按决策结果依次执行动作，合并结果。永不抛。"""
    results = []
    count = int(verdict.get("count") or 3)
    text = str(verdict.get("text") or "")

    from . import bili_learn, bili_scroll

    def _last_bvid() -> str:
        for n in bili_scroll.recent_notes(bot, limit=5):
            try:
                with open(n["path"], "r", encoding="utf-8") as f:
                    m = re.search(r"BV[0-9A-Za-z]{10}", f.read(3000))
            except OSError:
                continue
            if m:
                return m.group(0)
        return ""

    for task in verdict.get("tasks") or []:
        action = task.get("action")
        n = int(task.get("count") or count)
        try:
            if action == "scroll":
                r = await bili_scroll.run_round(bot, want=n)
            elif action == "learn":
                r = await bili_learn.run_round(bot, videos=max(1, min(6, n // 2 or 3)),
                                               reason=reason)
            elif action == "auto":
                from . import bili_auto
                r = await bili_auto.run_round(bot, reason=reason)
            elif action == "danmaku":
                from . import bili_extra
                bv = _last_bvid()
                payload = _payload("danmaku", text) or "好耶[doge]"
                if not bv:
                    r = {"ok": False, "reason": "还没刷过 B 站，没有可发弹幕的视频"}
                else:
                    import random
                    r = await bili_extra.send_danmaku(bv, payload[:60],
                                                     progress_sec=random.randint(8, 180))
                    r = {"ok": True, "data": r, "text": payload}
            elif action == "comment":
                from . import bili_extra
                bv = _last_bvid()
                payload = _payload("comment", text)
                if not bv:
                    r = {"ok": False, "reason": "还没刷过 B 站，没有可评论的视频"}
                elif not payload:
                    r = {"ok": False, "reason": "没看出来要评论什么"}
                else:
                    r = await bili_extra.comment_video(bv, payload)
                    r = {"ok": True, "data": r, "text": payload}
            else:
                r = {"ok": False, "reason": f"未知动作：{action}"}
        except Exception as e:
            r = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        results.append({"action": action, "count": n, "result": r})

    return {"ok": True, "tasks": results}
