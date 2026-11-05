"""记忆预算闸门。

现在的 `chat.context_chars` 只管窗口，管不住「窗口 + 摘要 + 日记 + 记忆 + 档案」加起来的
总量 —— 于是四层各要各的，没人管总账。这一层就是那个总账。

规矩两条：
  1. **单层上限**：每层先被自己的上限截一次（L0 5000 / L1 600 / L2 1600 / L3 1500）
  2. **总预算**：加起来超了，按「先砍最远的」顺序砍 —— L3 → L2 → L1 → L0，
     但每层留一个下限（L0 至少 1500，否则连「刚才那句」都看不到）

砍了什么必须记下来（`砍` 列表），否则线上出问题时不知道是谁把预算吃掉的。
"""

from __future__ import annotations

# 层的顺序就是「时间尺度从近到远」：L0 秒级、L1 分钟级、L2 天级、L3 周月级
层们 = ("L0", "L1", "L2", "L3")

默认单层上限 = {
    "L0": 5000,   # 最近的原话
    "L1": 600,    # 本轮增量小结
    "L2": 1600,   # 近两天的日摘要 + 日记
    "L3": 1500,   # 实体记忆（人 / 群 / 约定 / 事实）
}

# 砍到不能再砍为止（再砍就没意义了）
默认下限 = {
    "L0": 1500,   # 至少能看到刚才那几句
    "L1": 200,
    "L2": 0,
    "L3": 300,
}

默认总预算 = 8700

# 砍的顺序：远的先砍。L0 最后砍 —— 她要回的是「刚才那句」，不是「上周那句」
砍序 = ("L3", "L2", "L1", "L0")

# 活跃期（shortterm）是**旁路层**：它不参与上面这套「偏好预算」，只受自己的
# 硬容量（默认 128000 字）约束。放在这里只是让调用方必须显式走旁路参数，
# 免得以后有人把活跃期塞进 请求 里被静默砍掉。
旁路层 = ("active",)


def 分配带旁路(请求: dict, 旁路请求: dict | None = None,
               总预算: int | None = None, 上限: dict | None = None,
               下限: dict | None = None) -> tuple[dict, dict, list[dict]]:
    """正常四层走预算，旁路层原样返回。返回 (分配, 旁路分配, 砍日志)。"""
    分配结果, 日志 = 分配(请求, 总预算=总预算, 上限=上限, 下限=下限)
    旁路 = {str(k): max(0, int(v or 0)) for k, v in (旁路请求 or {}).items()
            if k in 旁路层}
    return 分配结果, 旁路, 日志


def 单层截断(请求: dict, 上限: dict | None = None) -> tuple[dict, list[dict]]:
    """先按单层上限截一次。返回 (截后, 日志)。"""
    上限 = 上限 or 默认单层上限
    出 = {}
    日志 = []
    for 层 in 层们:
        想 = max(0, int(请求.get(层, 0) or 0))
        顶 = max(0, int(上限.get(层, 想)))
        if 想 > 顶:
            出[层] = 顶
            日志.append({"层": 层, "原因": "超过单层上限", "从": 想, "到": 顶})
        else:
            出[层] = 想
    return 出, 日志


def 分配(请求: dict, 总预算: int | None = None,
         上限: dict | None = None, 下限: dict | None = None) -> tuple[dict, list[dict]]:
    """算出每层实际能占多少字。返回 (分配, 砍日志)。

    纯函数，不读配置也不碰文件 —— 这样才测得住。
    """
    总预算 = int(总预算 if 总预算 is not None else 默认总预算)
    下限 = 下限 or 默认下限

    出, 日志 = 单层截断(请求, 上限)
    总量 = sum(出.values())
    if 总量 <= 总预算:
        return 出, 日志

    需要砍 = 总量 - 总预算
    for 层 in 砍序:
        if 需要砍 <= 0:
            break
        现在 = 出[层]
        底 = max(0, int(下限.get(层, 0)))
        可砍 = max(0, 现在 - 底)
        if 可砍 <= 0:
            continue
        砍 = min(可砍, 需要砍)
        出[层] = 现在 - 砍
        需要砍 -= 砍
        日志.append({"层": 层, "原因": "总预算不够", "从": 现在, "到": 出[层]})

    return 出, 日志


def 摘要行(分配结果: dict, 日志: list[dict]) -> str:
    """一行日志，写进 log.info。"""
    各层 = " ".join(f"{k}={v}" for k, v in 分配结果.items())
    总 = sum(分配结果.values())
    砍 = ""
    if 日志:
        砍 = "｜砍：" + "，".join(
            f"{x['层']} {x['从']}→{x['到']}（{x['原因']}）" for x in 日志)
    return f"记忆预算 {总}：{各层}{砍}"


# ---------------------------------------------------------------- 最终总预算
#
# 四层闸门只算 L0~L3；活跃期、跨会话桥、梗、在场名单、生活上下文都是旁路块，
# 各要各的额度，加起来仍然可能把上下文吃穿。下面的函数在 **Agent 交给草稿之前**
# 对「所有记忆 system 块」做最后一次统一计数：
#   · 当前工作记忆（活跃期 / L0 / L1）优先保留；
#   · 只从整行边界裁剪，绝不切半句、半截 QQ 号；
#   · 被整块省略的层会有明确标记，不静默消失；
#   · 当前新 message 和不可变 policy 不在这份列表里，因此不会被它裁掉。
_块优先级 = {
    "active": 0,
    "l0": 1,
    "l1": 2,
    "life": 3,
    "present": 3,
    "other": 3,
    "l3": 4,
    "l2": 5,
    "bridge": 6,
    "meme": 7,
}
_块中文名 = {
    "active": "当前对话", "l0": "最近记录", "l1": "刚才这一轮",
    "life": "生活记忆", "present": "在场名单", "other": "其他记忆",
    "l3": "长期记忆", "l2": "这几天", "bridge": "跨会话",
    "meme": "梗", "unknown": "记忆",
}


def 记忆块类型(text: str) -> str:
    """按现有块 header 识别记忆层；不认识的一律当普通记忆。"""
    body = str(text or "").lstrip()
    if body.startswith("## 这一段对话的完整记录"):
        return "active"
    if body.startswith(("## 这个群最近的聊天记录", "## 你们俩最近的私聊记录")):
        return "l0"
    if body.startswith("## 刚才这一轮"):
        return "l1"
    if body.startswith("## 你还记得的（长期）"):
        return "l3"
    if body.startswith("## 这些天"):
        return "l2"
    if body.startswith(("## 群里刚才在聊什么", "## 刚才有人私下跟你说的")):
        return "bridge"
    if body.startswith("## 可能用到的梗"):
        return "meme"
    if body.startswith("## 这几轮在场的人"):
        return "present"
    if body.startswith(("## 相关的生活记忆与目标", "## 相关的真实经历")):
        return "life"
    return "other"


def _按行保留末尾(text: str, limit: int, marker: str) -> str:
    """从末尾保留**完整行**；放不下的旧行直接丢，并在末尾写清楚省略。"""
    if limit <= 0:
        return ""
    text = str(text or "")
    if len(text) <= limit:
        return text
    marker = marker if len(marker) <= max(0, limit - 1) else "（记忆已省略）"[:max(0, limit - 1)]
    if not marker:
        return ""
    available = max(0, limit - len(marker) - 1)
    lines = text.splitlines()
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + (1 if kept else 0)
        if used + cost > available:
            # 当前行整行放不下：跳过它和更旧的内容，不切半行。
            break
        kept.append(line)
        used += cost
    body = "\n".join(reversed(kept)).strip()
    out = (body + "\n" if body else "") + marker
    if len(out) > limit:
        # 极端小预算下只保留标记本身，仍然不切正文半行。
        out = marker[:limit]
    return out


def 按行预算(text: str, 预算: int, *, 标记: str = "（内容过长，已省略部分）") -> str:
    """单块文本的整行预算（生活上下文等非 message 块也可用）。"""
    try:
        limit = max(0, int(预算))
    except (TypeError, ValueError):
        limit = 0
    return _按行保留末尾(text, limit, 标记)


def 最终记忆预算(块列表, 总预算: int, *,
                 标记: str = "（记忆预算不足，已省略部分记忆块）",
                 保底: dict | None = None) -> tuple[list[dict], dict]:
    """统一 memory-only 总预算。返回 (按原顺序保留的块, 统计)。

    ``块列表`` 是 OpenAI 形状的 message dict（只处理 role=system 的内容）。
    当前工作记忆（active/L0/L1）优先；预算不足时先整块省略低优先级的桥/梗/
    长期层，再从末尾按整行裁剪。所有省略都会在保留下来的文本里留标记。
    """
    try:
        original_budget = max(0, int(总预算))
    except (TypeError, ValueError):
        original_budget = 0
    budget = original_budget
    records = []
    passthrough: list[tuple[int, dict]] = []
    for index, block in enumerate(list(块列表 or [])):
        if not isinstance(block, dict):
            continue
        if block.get("role") != "system":
            # 当前 user / tool 回执不属于 memory-only 预算，原样保留不乱裁。
            passthrough.append((index, dict(block)))
            continue
        content = str(block.get("content") or "")
        kind = 记忆块类型(content)
        records.append({
            "index": index,
            "block": block,
            "content": content,
            "kind": kind,
            "priority": _块优先级.get(kind, 3),
            "omitted": False,
            "truncated": False,
        })
    original = sum(len(rec["content"]) for rec in records)
    info = {
        "预算": budget,
        "原字数": original,
        "总字数": original,
        "省略": [],
        "截断": [],
        "块": {rec["kind"]: len(rec["content"]) for rec in records},
    }
    if not records or original <= budget:
        return [dict(block) for block in (块列表 or [])], info

    marker = str(标记 or "（记忆已省略）")
    marker_reserved = False
    total = original

    # 1) 低优先级旁路层先整块省略（life/present 与 other 也属于可选背景）。
    order = sorted(records, key=lambda rec: (-rec["priority"], -rec["index"]))
    for rec in order:
        if total <= budget:
            break
        if rec["priority"] < 3:
            continue
        if not marker_reserved:
            # 给全局省略标记留位置；标记只写一次。
            if budget > len(marker) + 1:
                budget = budget - len(marker) - 1
                marker_reserved = True
        total -= len(rec["content"])
        rec["omitted"] = True
        info["省略"].append(rec["kind"])

    # 2) L1/L0/active：仍超就从各自末尾按整行裁；优先裁低优先的 L1。
    for rec in sorted(records, key=lambda item: (-item["priority"], -item["index"])):
        if total <= budget:
            break
        if rec["omitted"] or rec["priority"] > 2:
            continue
        need = total - budget
        target = max(0, len(rec["content"]) - need)
        new_text = _按行保留末尾(
            rec["content"], target,
            "（较早内容已按记忆预算省略，当前对话和完整 QQ 号保留）")
        if len(new_text) >= len(rec["content"]):
            continue
        total -= len(rec["content"]) - len(new_text)
        rec["content"] = new_text
        rec["truncated"] = True
        info["截断"].append(rec["kind"])

    # 3) 极端情况：所有可裁的都裁完仍超，最后一层也按整行压到预算内。
    for rec in records:
        if total <= budget:
            break
        if rec["omitted"]:
            continue
        need = total - budget
        target = max(0, len(rec["content"]) - need)
        new_text = _按行保留末尾(
            rec["content"], target, "（记忆预算不足，已省略较早内容）")
        if new_text == rec["content"]:
            continue
        total -= len(rec["content"]) - len(new_text)
        rec["content"] = new_text
        rec["truncated"] = True
        if rec["kind"] not in info["截断"]:
            info["截断"].append(rec["kind"])

    kept: list[tuple[int, dict]] = []
    for rec in records:
        if rec["omitted"]:
            continue
        block = dict(rec["block"])
        block["content"] = rec["content"]
        kept.append((rec["index"], block))

    if info["省略"]:
        note = marker
        used = sum(len(str(b.get("content") or "")) for _i, b in kept)
        if used + len(note) <= original_budget:
            kept.append((len(records) + 1, {"role": "system", "content": note}))
        elif not kept and original_budget >= len(note):
            kept = [(len(records) + 1, {"role": "system", "content": note})]

    # 保留原顺序（新加的省略标记放最后）。
    kept.extend(passthrough)
    kept.sort(key=lambda item: item[0])
    kept_blocks = [block for _index, block in kept]
    info["总字数"] = sum(
        len(str(block.get("content") or "")) for block in kept_blocks
        if block.get("role") == "system")
    info["块"] = {rec["kind"]: len(rec["content"]) for rec in records if not rec["omitted"]}
    return kept_blocks, info


# 便于调用方按语义选名字；同一个实现。
记忆总预算 = 最终记忆预算
fit_memory_blocks = 最终记忆预算
