"""
上下文窗口：把最近 300 条群聊，按时间远近压成一段给模型看的记录。

核心是**时间衰减加权**——越近的话越重要，越远的话越省。具体三件事：

  1. 权重   w = exp(-(now - ts) / tau)
            tau 默认 6 小时。近的权重接近 1，一天前的接近 0。
  2. 截断   按权重分档，新的话原样留着，老的话只留开头。
             长消息不会把整个窗口挤爆，而且老消息的细节本来也不重要。
  3. 取舍   总字数超预算时，从**权重最低**（最老）的开始丢，
             并在开头标一句「更早的 N 条已省略」，让模型知道自己没看全。

时间参照点用「最后一条消息的时间」而不是「现在」——群里可能一晚上没说话，
用 now 会让所有消息权重都趋零，窗口就白留了。

每一行开头都带时间戳（[HH:MM] / 跨天 [MM-DD HH:MM]），说话人用「昵称(尾号1234)」，
同名的人靠尾号分开；取不到 uid 时写成「昵称(尾号未知)」，绝不退化成裸名字。
"""

from __future__ import annotations

import math
import time

BANDS = [
    # (权重下限, 单条截断长度)
    (0.60, 220),    # 很新：基本原样
    (0.30, 110),
    (0.12, 60),
    (0.00, 40),     # 很旧：只留个印象
]


def _fmt_gap(sec: float) -> str:
    sec = int(sec)
    if sec < 90:
        return ""
    if sec < 3600:
        return f"{sec // 60} 分钟"
    if sec < 86400:
        h = sec / 3600
        return f"{h:.0f} 小时" if h >= 1.5 else f"{h:.1f} 小时"
    return f"{sec // 86400} 天"


SELF_LABEL = "我（大肥鱼）"


def _fmt_time(ts: float, ref_ts: float) -> str:
    """时间前缀：[HH:MM]，跨了天才带月日 [MM-DD HH:MM]。

    基准 = 窗口最后一条的**本地日期**（不是「现在」）。同一行里只截正文，
    绝不截这个前缀。
    """
    t = time.localtime(ts)
    r = time.localtime(ref_ts)
    same_day = (t.tm_year, t.tm_mon, t.tm_mday) == (r.tm_year, r.tm_mon, r.tm_mday)
    if same_day:
        return time.strftime("[%H:%M]", t)
    return time.strftime("[%m-%d %H:%M]", t)


def speaker(row: dict) -> str:
    """说话人标签：「昵称(尾号1234)」，自己是「我（大肥鱼）」，取不到 uid 是「昵称(尾号未知)」。

    同名的人靠尾号分开；没有 uid 就明说「尾号未知」，绝不退化成裸名字——
    裸名字等于把两个同名的人当成同一个。
    """
    if row.get("role") == "assistant":
        return SELF_LABEL
    name = " ".join(str(row.get("who") or "").split()) or "群友"
    uid = str(row.get("uid") or "").strip()
    return f"{name}(尾号{uid[-4:]})" if uid else f"{name}(尾号未知)"


def present(rows: list[dict], limit: int = 12) -> str:
    """最近几轮出现过的人，新的在前；同名不同号的会都列出来并提示各自尾号。"""
    seen: dict[str, str] = {}
    # name -> {key -> 尾号}，只为算出「同名但有不止一个尾号」的冲突
    by_name: dict[str, dict[str, str]] = {}
    for r in reversed(rows):
        if r.get("role") == "assistant":
            continue
        label = speaker(r)
        if label == SELF_LABEL:
            # 自己绝不算进在场名单，也不会被当成「同名的人」
            continue
        name = " ".join(str(r.get("who") or "").split())
        if not name:
            continue
        uid = str(r.get("uid") or "").strip()
        key = uid or name
        if key not in seen:
            seen[key] = label
            by_name.setdefault(name, {})[key] = uid[-4:] if uid else "未知"
        if len(seen) >= limit:
            break
    if not seen:
        return ""
    line = "## 这几轮在场的人\n" + "、".join(seen.values())
    clash = [n for n, keys in by_name.items() if len(keys) > 1]
    if clash:
        tails = "、".join(
            f"{n}（尾号：{'、'.join(by_name[n].values())}）" for n in clash)
        line += ("\n注意：" + "、".join(clash)
                 + " 有不止一个人叫这个名字，按尾号分清 —— " + tails + "。")
    return line + "\n（「我（大肥鱼）」是你自己。）"


# 聊天记录块的开头说明。mem/assemble.py（四层记忆 L0）也 import 这段，保证两边口径一致。
L0_HEADER = (
    "## 这个群最近的聊天记录（从旧到新）\n"
    "每行开头的方括号是这条消息的时间，先看它排先后；跨了天才带月日。\n"
    "名字后面括号里的尾号才是身份：同名但尾号不同，就是不同的人；"
    "写「尾号未知」的说明没拿到 QQ 号，只能按昵称认，别硬猜是谁。\n"
    "开头写「我（大肥鱼）」的行全是你自己发出去的消息，不是任何群友说的；"
    "别人在话里叫你「大肥鱼」，不等于那句话是你说的，别把两边弄混。\n"
    "不要把两行不同人的话拼成一句来引用；引号里的内容只属于那一行的说话人。\n"
    "看语气和话题用，不要逐条回应，也不要引用下面的话来证明你记得。"
)


def weight(age_sec: float, tau_sec: float) -> float:
    if tau_sec <= 0:
        return 1.0
    return math.exp(-max(0.0, age_sec) / tau_sec)


def build(rows: list[dict], config) -> tuple[str, dict]:
    """返回 (给模型看的聊天记录, 统计信息)。

    rows 里每条要有 role / content / who / ts。
    """
    limit = int(config.get("chat.context_messages", 300))
    tau_h = float(config.get("chat.time_decay_hours", 6))
    tau = max(60.0, tau_h * 3600)
    budget = int(config.get("chat.context_chars", 12000))
    gap_mark_min = float(config.get("chat.gap_mark_minutes", 10)) * 60

    rows = [r for r in rows if (r.get("content") or "").strip()]
    total_in = len(rows)
    rows = rows[-limit:] if limit > 0 else rows
    if not rows:
        return "", {"lines": 0, "dropped": 0, "chars": 0, "span_hours": 0}

    ref = float(rows[-1].get("ts") or time.time())

    # ---------- 先算权重和截断长度（截断只作用于正文，时间前缀和人名绝不动）
    items = []
    for r in rows:
        ts = float(r.get("ts") or ref)
        w = weight(ref - ts, tau)
        cap = BANDS[-1][1]
        for floor, c in BANDS:
            if w >= floor:
                cap = c
                break
        text = " ".join(str(r.get("content") or "").split())
        if len(text) > cap:
            text = text[:cap].rstrip() + "…"
        items.append({"ts": ts, "w": w, "who": speaker(r),
                      "time": _fmt_time(ts, ref),
                      "text": text,
                      "self": r.get("role") == "assistant"})

    # ---------- 超预算就从最老的开始丢
    def render(seq) -> tuple[str, int]:
        lines: list[str] = []
        prev_ts = None
        for it in seq:
            if prev_ts is not None:
                gap = it["ts"] - prev_ts
                # 只有间隔真的够久才标一句，不然 300 条全是标记，撑爆预算
                if gap >= gap_mark_min:
                    g = _fmt_gap(gap)
                    if g:
                        lines.append(f"——（隔了 {g}）——")
            prev_ts = it["ts"]
            lines.append(f"{it['time']} {it['who']}：{it['text']}")
        body = "\n".join(lines)
        return body, len(body)

    body, size = render(items)
    dropped = 0
    while size > budget and len(items) > 12:
        # 每次丢最老的一条（权重最低的那端）
        items.pop(0)
        dropped += 1
        body, size = render(items)

    head = ""
    if dropped:
        head = f"（更早的 {dropped} 条已经省略，下面是接着聊的部分）\n"

    span_h = (items[-1]["ts"] - items[0]["ts"]) / 3600 if len(items) > 1 else 0
    stats = {
        "lines": len(items),
        "total_in_window": total_in,
        "dropped": dropped,
        "chars": size,
        "span_hours": round(span_h, 1),
        "newest_weight": round(items[-1]["w"], 2),
        "oldest_weight": round(items[0]["w"], 2),
    }
    return head + body, stats


def build_messages(rows: list[dict], config, current: dict | None = None
                   ) -> tuple[list[dict], dict]:
    """直接产出可塞进 messages 的 system 块。current 是正在说话的那条（不进窗口，只进在场名单）。"""
    text, stats = build(rows, config)
    if not text:
        return [], stats
    out = [{"role": "system", "content": L0_HEADER + "\n" + text}]
    who = present(rows[-40:] + ([current] if current else []))
    if who:
        out.append({"role": "system", "content": who})
    return out, stats
