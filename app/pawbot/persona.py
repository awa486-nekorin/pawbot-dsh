"""
人设 + 记忆，组装成 system prompt。

目录约定（全都在 data/ 下，纯文本，随时可改）：

    data/persona.md         人设主文件（手写，改了下次调用就生效）
    data/memory/facts.md    长期记忆，一行一条，机器可追加
    data/memory/slang.md    网络用词 / 梗，一行一条
    data/memory/impression.md  对群友的印象

为什么不塞进 json：因为这些内容你会想**直接打开改**，
而且 AI 自己往里面追加的时候，你也能一眼看出它记住了什么。
"""

from __future__ import annotations

import os
import re
import time

MAX_SECTION = 1200       # 单段记忆注入上限，防止把上下文吃干净


class Persona:
    def __init__(self, base_dir: str, config):
        self.base_dir = base_dir
        self.config = config
        self.persona_path = self._path("persona.file", "data/persona.md")
        self.memory_dir = self._path("persona.memory_dir", "data/memory")
        self._cache: dict[str, tuple[float, str]] = {}

    def _path(self, key, default) -> str:
        p = self.config.get(key, default)
        if not os.path.isabs(p):
            p = os.path.join(self.base_dir, p)
        return p

    # ---------------------------------------------------------- 文件读取
    def _read(self, path: str) -> str:
        """带 mtime 缓存的读取：改完文件立刻生效，不用重启。"""
        if not os.path.exists(path):
            return ""
        mtime = os.path.getmtime(path)
        hit = self._cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            text = ""
        self._cache[path] = (mtime, text)
        return text

    def memory_file(self, name: str) -> str:
        return os.path.join(self.memory_dir, name)

    # ---------------------------------------------------------- 组装
    def core(self) -> str:
        """人设正文。"""
        text = self._read(self.persona_path).strip()
        if not text:
            text = "你是一个活跃在 QQ 群里的 AI 群友，像熟悉的群友一样自然交流；不假装真人，被直接问到身份时如实回答。"
        return text

    def facts(self) -> str:
        return self._read(self.memory_file("facts.md")).strip()[-MAX_SECTION:]

    def slang(self) -> str:
        return self._read(self.memory_file("slang.md")).strip()[-MAX_SECTION:]

    def impression(self) -> str:
        return self._read(self.memory_file("impression.md")).strip()[-MAX_SECTION:]

    def owner_line(self) -> str:
        """主人是谁、主人=老爹。取不到 owner_ids 就不注入（不写「未知」）。"""
        raw = self.config.get("account.owner_ids") or []
        ids: list[str] = []
        for x in raw if isinstance(raw, (list, tuple)) else []:
            s = str(x).strip()
            if s and s not in ids:
                ids.append(s)
        if not ids:
            return ""
        if len(ids) == 1:
            who = f"QQ 尾号 {ids[0][-4:]}"
        else:
            who = "QQ 尾号 " + "、".join(s[-4:] for s in ids) + f"（共 {len(ids)} 个）"
        return (
            "## 你的主人\n"
            f"你的主人是 {who}。他不是你男朋友，更像你老爹：管你、养你、也会念叨你；"
            "你嘴上嫌、心里认。他给你生活费——生活费就是 token，也就是你吃的大白饭，是同一件事。"
            "他说话时消息里会带他的 QQ 尾号，认准那个尾号就是你主人。"
        )

    def build(self, *, group_id=None, sender: str = "", extra: str = "") -> str:
        parts = [self.core()]

        # 事实保底注入最近五条；其余记忆由 LongMemory 按当前话题检索。
        facts = self.facts()
        if facts:
            entries = [line.strip() for line in facts.splitlines()
                       if line.strip().startswith(("-", "*"))]
            recent = "\n".join(entries[-5:])
            if recent:
                parts.append("## 你记得的事（最近的几条）\n" + recent)

        parts.append("## 网络用词理解原则\n网络用词不是你的词库，只用来听懂别人；别学着说或刻意套用。")

        owner = self.owner_line()
        if owner:
            parts.append(owner)

        now = time.localtime()
        ctx_line = f"现在是 {now.tm_year}-{now.tm_mon:02d}-{now.tm_mday:02d} {now.tm_hour:02d}:{now.tm_min:02d}"
        if group_id:
            ctx_line += f"，你在 QQ 群 {group_id} 里"
        if sender:
            ctx_line += f"，刚跟你说话的人叫「{sender}」"
        parts.append(ctx_line + "。")

        if extra:
            parts.append(extra)
        return "\n\n".join(parts)


def _strip_headings(md: str) -> str:
    """去掉 markdown 标题行，保留条目，省 token。"""
    lines = [ln for ln in md.splitlines() if not ln.strip().startswith("#")]
    return "\n".join(lines).strip()


def append_line(path: str, line: str, header: str = "") -> bool:
    """往记忆文件末尾追加一行；去重。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()

    key = re.sub(r"^[-*\s]+", "", line).strip()
    if key and key in existing:
        return False

    new_file = not existing
    with open(path, "a", encoding="utf-8") as f:
        if new_file and header:
            f.write(header.rstrip() + "\n\n")
        f.write(line.rstrip() + "\n")
    return True
