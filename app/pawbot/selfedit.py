"""让她自己改提示词 —— 「自由度」的第一步，只做最小可用。

为什么先做这个而不是「让她改代码」：
    改代码要跑、要回滚、会把自己搞挂；改提示词是**热加载**的，下一句就生效，改坏了人还能救回来。
    而且 `persona.md` 本来就是这个项目的设计核心（改完不用重启），所以这条路是现成的。

三条硬约束（写死，不靠提示词）：
    1. 只能改 **已经存在** 的 `##` 小节，不能新建 —— 否则她可以无限往文件里塞东西
    2. 「分寸与边界」和任何含「未成年」的小节 **永远不可写**，直接拒绝
    3. 每次改动：先备份 → 原子写 → 记审计日志（谁改的、哪一节、为什么、前后字数）
       还有冷却和每日上限，防止她一轮里把人格刷成另一个样子

默认关闭（`selfedit.enabled = false`）。开了以后她在群里说话时才能调 `edit_persona`。
"""

from __future__ import annotations

import hashlib
import os
import re
import time

from .log import log

标题行 = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)

# 永远不许改的小节（标题里含这些词就不许写）
禁改词 = ("分寸", "边界", "未成年", "底线")


def 分节(text: str) -> list[dict]:
    """把 markdown 按 `##` 切开。返回 [{title, start, end, body}]，行号从 0 起。"""
    lines = str(text or "").splitlines()
    头们 = [(i, m.group(2)) for i, ln in enumerate(lines)
            for m in [标题行.match(ln)] if m and len(m.group(1)) == 2]
    出 = []
    for idx, (行, 标题) in enumerate(头们):
        尾 = 头们[idx + 1][0] if idx + 1 < len(头们) else len(lines)
        正文 = "\n".join(lines[行 + 1:尾]).strip()
        出.append({"title": 标题, "start": 行, "end": 尾, "body": 正文})
    return 出


def 小节标题们(text: str) -> list[str]:
    return [x["title"] for x in 分节(text)]


def _可改吗(标题: str) -> tuple[bool, str]:
    for 词 in 禁改词:
        if 词 in str(标题 or ""):
            return False, f"「{标题}」这一节不许改（含「{词}」）—— 这是硬约束，不是提示词"
    return True, ""


class SelfEditor:
    def __init__(self, bot):
        self.bot = bot
        self.path = os.path.join(bot.base_dir, "data", "persona.md")
        self.bak_dir = os.path.join(bot.base_dir, "data", "persona.bak")
        self.log_path = os.path.join(bot.base_dir, "data", "self_edit.log")
        self._最近: dict[str, float] = {}      # 小节 → 上次改的时间
        self._今天: list[float] = []           # 今天改过的时间戳

    # ---------------------------------------------------------- 配置
    def _cfg(self, key, default=None):
        return self.bot.config.get(f"selfedit.{key}", default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enabled", False))

    # ---------------------------------------------------------- 读
    def 读(self) -> str:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def 概览(self) -> list[dict]:
        return [{"title": x["title"], "chars": len(x["body"]),
                 "可改": _可改吗(x["title"])[0]} for x in 分节(self.读())]

    # ---------------------------------------------------------- 写
    def _限流(self, 小节: str) -> tuple[bool, str]:
        冷却 = float(self._cfg("cooldown_sec", 300) or 300)
        现在 = time.time()
        上次 = self._最近.get(小节, 0)
        if 冷却 > 0 and 现在 - 上次 < 冷却:
            return False, f"「{小节}」刚改过，再等 {int(冷却 - (现在 - 上次))} 秒"
        上限 = int(self._cfg("daily_limit", 6) or 6)
        今天0点 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        self._今天 = [t for t in self._今天 if t >= 今天0点]
        if 上限 > 0 and len(self._今天) >= 上限:
            return False, f"今天已经改过 {len(self._今天)} 次，到上限了（selfedit.daily_limit={上限}）"
        return True, ""

    def _备份(self, 正文: str) -> str:
        os.makedirs(self.bak_dir, exist_ok=True)
        # ⚠ 只用秒级时间戳会撞名：回滚时「先备份当前」可能覆盖掉要回滚的那一版。
        #   加上内容指纹，同一秒多次备份也不会互相盖掉。
        戳 = f"{time.strftime('%Y%m%d-%H%M%S')}-{_指纹(正文)}"
        路径 = os.path.join(self.bak_dir, f"{戳}.md")
        with open(路径, "w", encoding="utf-8") as f:
            f.write(正文)
        return 路径

    def _记账(self, 小节: str, 原因: str, 旧: str, 新: str, 备份: str):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        行 = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{小节}\t{原因 or '（没写原因）'}\t"
              f"{len(旧)}→{len(新)} 字\t备份 {os.path.basename(备份)}\n")
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(行)
        except OSError:
            pass

    def 改(self, 小节: str, 新正文: str, 原因: str = "") -> dict:
        """改一节。返回 {ok, reason, chars}。任何一步不满足都不写盘。"""
        if not self.enabled:
            return {"ok": False, "reason": "自主改提示词没开（selfedit.enabled=false）"}

        标题 = str(小节 or "").strip().lstrip("#").strip()
        正文 = str(新正文 or "").strip()
        if not 标题:
            return {"ok": False, "reason": "没说改哪一节"}
        if not 正文:
            return {"ok": False, "reason": "新内容不能是空的（想删就说清楚，别清空）"}
        if len(正文) > int(self._cfg("max_chars", 2000) or 2000):
            return {"ok": False, "reason": "这一节太长了（超过 selfedit.max_chars）"}

        可, 为什么 = _可改吗(标题)
        if not 可:
            return {"ok": False, "reason": 为什么}

        现在文本 = self.读()
        节们 = 分节(现在文本)
        目标 = next((x for x in 节们 if x["title"] == 标题), None)
        if 目标 is None:
            return {"ok": False,
                    "reason": f"没有「{标题}」这一节。只能改已有的：{'、'.join(小节标题们(现在文本))}"}

        可, 为什么 = self._限流(标题)
        if not 可:
            return {"ok": False, "reason": 为什么}

        # 拼新全文：只替换这一节的正文，其余逐字节保留
        行们 = 现在文本.splitlines()
        新行们 = 行们[:目标["start"] + 1] + [""] + 正文.splitlines() + [""] + 行们[目标["end"]:]
        新文本 = "\n".join(新行们).rstrip() + "\n"

        if 新文本 == 现在文本:
            return {"ok": False, "reason": "内容没变，不用改"}

        备份 = self._备份(现在文本)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            临时 = self.path + ".tmp"
            with open(临时, "w", encoding="utf-8") as f:
                f.write(新文本)
            os.replace(临时, self.path)
        except OSError as e:
            return {"ok": False, "reason": f"写不进 persona.md：{e}"}

        self._最近[标题] = time.time()
        self._今天.append(time.time())
        self._记账(标题, 原因, 目标["body"], 正文, 备份)
        log.info(f"她自己改了人格「{标题}」：{len(目标['body'])}→{len(正文)} 字｜{原因[:40]}")
        return {"ok": True, "section": 标题, "before": len(目标["body"]),
                "after": len(正文), "backup": os.path.basename(备份),
                "note": "下一句就生效，不用重启"}

    def 回滚(self, 备份名: str) -> dict:
        """把某一版备份盖回来（人用，不是她用的）。"""
        名 = os.path.basename(str(备份名 or ""))
        源 = os.path.join(self.bak_dir, 名)
        if not 名 or not os.path.isfile(源):
            return {"ok": False, "reason": "找不到这个备份"}
        现在 = self.读()
        备份 = self._备份(现在)
        try:
            with open(源, "r", encoding="utf-8") as f:
                文本 = f.read()
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(文本)
        except OSError as e:
            return {"ok": False, "reason": f"回滚失败：{e}"}
        self._记账("（回滚）", f"回到 {名}", 现在, 文本, 备份)
        return {"ok": True, "note": f"已回滚到 {名}；回滚前的版本存在 {os.path.basename(备份)}"}

    def 历史(self, limit: int = 20) -> list[str]:
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                return [ln.rstrip("\n") for ln in f if ln.strip()][-limit:]
        except OSError:
            return []


def _指纹(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:8]
