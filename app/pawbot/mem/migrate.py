"""把旧数据搬进新实体库。**幂等**：跑几次结果一样。

搬什么：
    longmem/episodes.jsonl  → kind=day（daily_digest 那批）/ kind=event
    people/<uid>.md         → kind=profile + kind=relation（好感）
    memory/facts.md         → kind=fact（解析 [日期] 前缀）

不搬什么：
    memory/impression.md    0 字节，没内容
    memory/slang.md         那是「网络用词理解」，不是实体；v2 改成命中才查，不每轮注入
    sessions.json           只有 116 字的摘要，而且 L2 会自己长出来

⚠ 搬 episodes 时会**去重**，而且顺序很关键：
   旧数据里每一条都是「累积摘要」（第 N 条包含前 N-1 条），相邻重复最高 85.7%。
   所以按天分组、**从新到旧**遍历，新的先占位；旧的新增片段不足 45% 就整条丢掉。
   （按时间正序去重是错的 —— 那样留下的是最早、最不完整的那条。）
"""

from __future__ import annotations

import json
import os
import re

from .entities import EntityStore, _天, _短哈希

去重阈值 = 0.60      # 新增片段占比低于这个，说明这条基本是重复
片段长 = 6
每天最多事件 = 8     # 一天塞太多事件没有意义，L2 的日摘要会接手


def _grams(text: str) -> set[str]:
    t = "".join(str(text or "").split())
    if len(t) < 片段长:
        return set()
    return {t[i:i + 片段长] for i in range(len(t) - 片段长 + 1)}


def _脱壳(text: str) -> str:
    """旧数据里 7/23 条的 summary 是 `{"小结":"…"}` 或 `{"summary":"…"}` 这种 JSON 壳。"""
    t = str(text or "").strip()
    if not t.startswith("{"):
        return t
    try:
        j = json.loads(t)
    except json.JSONDecodeError:
        return t
    if not isinstance(j, dict):
        return t
    for k in ("小结", "summary", "摘要", "text"):
        v = j.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return t


def _读jsonl(path: str) -> list[dict]:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        pass
    return out


def _读档案(path: str) -> dict:
    """解析 people/<uid>.md：名字 / 性格 / 说话方式 / 在意 / 好感。"""
    出: dict = {"性格": [], "说话方式": [], "在意": [], "名字": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            正文 = f.read()
    except OSError:
        return 出
    for line in 正文.splitlines():
        line = line.strip()
        m = re.match(r"^[-*]\s*(性格|说话方式|在意|名字|好感|好感更新)\s*[：:]\s*(.+)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "好感":
            try:
                出["好感"] = float(v)
            except ValueError:
                pass
        elif k == "名字":
            出["名字"] = v
        elif k in ("性格", "说话方式", "在意"):
            出[k].append(v)
    return 出


def run(base_dir: str, config=None, *, dry_run: bool = False) -> dict:
    """执行迁移。返回报告（没写盘时 dry_run=True）。"""
    库 = EntityStore(base_dir, config)
    报告 = {"episodes": 0, "episodes_去重丢掉": 0, "day": 0, "profile": 0,
            "relation": 0, "fact": 0, "跳过": [], "dry_run": dry_run}

    def 写(key, kind, **kw):
        if dry_run:
            return {"key": key, "kind": kind}
        return 库.add(key, kind, **kw)

    # ---------- ① episodes ----------
    # 按天分组、从新到旧处理：新的先占位，旧的只在「确实带来新内容」时才留。
    # 旧数据是累积摘要，正序去重会留下最早最不完整的那条，所以顺序不能反。
    ep_path = os.path.join(base_dir, "data", "longmem", "episodes.jsonl")
    by_day: dict[str, list[dict]] = {}
    for row in _读jsonl(ep_path):
        正文 = _脱壳(row.get("summary"))
        if not 正文:
            报告["跳过"].append("空 episode")
            continue
        ts = float(row.get("ts") or 0)
        day = str(row.get("day") or _天(ts))
        by_day.setdefault(day, []).append({**row, "_正文": 正文, "_ts": ts})

    for day, rows in sorted(by_day.items()):
        rows.sort(key=lambda r: r["_ts"], reverse=True)      # 新的先来
        已见: set[str] = set()
        这天事件 = 0
        for row in rows:
            正文 = row["_正文"]
            重要度 = max(1, min(5, int(row.get("importance") or 2)))
            段 = _grams(正文)
            新增比例 = 1.0 if not 段 else len(段 - 已见) / len(段)

            if row.get("daily_digest"):
                写(f"day:{day}", "day", text=正文, importance=max(3, 重要度),
                   ts=row["_ts"], source=str(row.get("session_id") or ""))
                报告["day"] += 1
            elif 这天事件 >= 每天最多事件:
                报告["episodes_去重丢掉"] += 1
                continue
            elif 新增比例 < 去重阈值:
                报告["episodes_去重丢掉"] += 1
                continue
            else:
                写(f"ev:{day}:{_短哈希(正文)}", "event", text=正文, importance=重要度,
                   ts=row["_ts"], source=str(row.get("session_id") or ""))
                报告["episodes"] += 1
                这天事件 += 1
            已见 |= 段

    # ---------- ② people/<uid>.md ----------
    people_dir = os.path.join(base_dir, "data", "people")
    try:
        文件们 = [n for n in os.listdir(people_dir) if n.endswith(".md")]
    except OSError:
        文件们 = []
    for fn in sorted(文件们):
        uid = fn[:-3]
        if not uid.isdigit():
            continue
        档 = _读档案(os.path.join(people_dir, fn))
        段 = []
        if 档["性格"]:
            段.append("性格：" + "；".join(档["性格"][:3]))
        if 档["说话方式"]:
            段.append("说话方式：" + "；".join(档["说话方式"][:3]))
        if 档["在意"]:
            段.append("在意：" + "；".join(档["在意"][:3]))
        if 段:
            写(f"uid:{uid}", "profile", text="｜".join(段), importance=4,
               ts=0, source="people.md")
            报告["profile"] += 1
        if "好感" in 档:
            写(f"uid:{uid}", "relation", value=档["好感"], importance=3,
               ts=0, source="people.md")
            报告["relation"] += 1

    # ---------- ③ memory/facts.md ----------
    facts_path = os.path.join(base_dir, "data", "memory", "facts.md")
    try:
        with open(facts_path, "r", encoding="utf-8") as f:
            行们 = f.read().splitlines()
    except OSError:
        行们 = []
    for line in 行们:
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        正文 = re.sub(r"^[-*]\s*", "", line)
        ts = 0.0
        m = re.match(r"^\[(\d{4}-\d{2}-\d{2})\]\s*(.+)$", 正文)
        if m:
            import time as _t
            try:
                ts = _t.mktime(_t.strptime(m.group(1), "%Y-%m-%d"))
            except ValueError:
                ts = 0.0
            正文 = m.group(2)
        正文 = 正文.strip()
        if not 正文:
            continue
        写(f"fact:{_短哈希(正文)}", "fact", text=正文, importance=4, ts=ts, source="facts.md")
        报告["fact"] += 1

    if not dry_run:
        报告["库统计"] = 库.stats()
    return 报告
