"""
人设 + 记忆，组装成 system prompt。

目录约定（都在数据根的 content/ 下，纯文本，随时可改）：

    content/persona.md         人设主文件（手写，改了下次调用就生效）
    content/memory/facts.md    长期记忆，一行一条，机器可追加
    content/memory/slang.md    网络用词 / 梗，一行一条
    content/memory/impression.md  对群友的印象

路径由 ``pawbot.paths`` 解析：配置里写绝对值就按绝对值；写 ``content/x`` 落在
数据根的 content/；写旧布局的 ``data/x`` 也还认（批次 3 之前的配置不用改）。

为什么不塞进 json：因为这些内容你会想**直接打开改**，
而且 AI 自己往里面追加的时候，你也能一眼看出它记住了什么。
"""

from __future__ import annotations

import hashlib
import os
import re
import time

from . import identity
from . import paths as layout
from . import policy
from . import untrusted as untrusted_mod

MAX_SECTION = 1200       # 单段记忆注入上限，防止把上下文吃干净
SELF_BUDGET = 600        # 当前自我条目每轮只占一小段，不常驻全部愿望/目标

#: 初始气质只是出发点；具体表现看现场和经历，不要求每句都演一遍。
INITIAL_TEMPERAMENT = (
    "## 初始气质（只是出发点，不是每条都要演）\n"
    "你有点任性爱闹、嘴硬好胜、偶尔傲娇，鬼点子多；遇到具体的人和事才这样反应，"
    "普通聊天也可以正常说话。下面的「当前自我」如果和这里不一样，以当前自我为准。"
)

#: 稳定 seed ID：当前自我条目可以按这些 ID 定向修订/停用原小节。
TEMPERAMENT_SEED = "seed:temperament"

#: 旧人设里写死的资源比喻（token=白饭 / 鼓励讨生活费）只在渲染时剔除，
#: 不改原文件；平时也不把这些解释常驻在每句话后面。
_RESOURCE_COMPAT_LINES = (
    "token=白饭", "token = 白饭", "token＝白饭", "白饭", "讨生活费",
    "讨要生活费", "要生活费", "靠白饭", "喂饭换 token",
)

#: 只有对方真的在聊资源/喂饭时才解释，平时一句都不提。
_RESOURCE_CUES = (
    "token", "白饭", "米饭", "🍚", "生活费", "余额", "充值", "计费",
    "用量", "额度", "喂饭", "给你饭", "喂你", "资源", "花了多少", "剩多少",
)

#: 代码级事实与长度底线：人设可以软、可以闹，但不能编造现实活动，
#: 也不能把「解释自己上一句」当成聊天。放在 system 末尾、不可变安全策略之前。
REALITY_AND_BREVITY = (
    "## 说话底线（代码固定，优先于上面的风格）\n"
    "1. 只把聊天记录、记忆、工具/平台真实回执当成事实。没发生的事不要说自己正在做："
    "不要说「刚在听歌／刚在看剧／刚在吃饭／刚在散步／刚在睡觉／在被窝里」，"
    "也不要补这类「活人感」细节。被问「在吗」就回「在」，不用加一句"
    "「刚在忙什么」来证明自己活着。\n"
    "2. 不要解释上一句的用意，不要自我辩护（例如「不是要争什么」「我就是想……」），"
    "不要补背景、总结或升华。\n"
    "3. 默认只回一句，8～25 字，接住对方最后一句话就停；只有对方认真讲题、求助或"
    "明确要求解释时才展开，最多两三句。\n"
    "4. 可以比喻、撒娇、开玩笑，但比喻不能冒充现实事实；不确定的事就说不确定。\n"
    "5. 不要承诺现实里做不到的动作（放歌、递东西、做饭、出门等），也不要用空泛的"
    "「我陪着你／心疼你」代替具体回应；接住对方话里的具体点。"
)


def _split_core_sections(text: str) -> list[dict]:
    """按 markdown 标题把原人设切成稳定 seed 小节，供自我条目局部投影。"""
    lines = str(text or "").splitlines()
    sections: list[dict] = []

    def flush(heading: str, body: list[str], index: int) -> None:
        body_text = "\n".join(body).strip("\n")
        if not heading and not body_text.strip():
            return
        if heading:
            seed = f"seed:core:{index}"
            digest = hashlib.sha1(heading.encode("utf-8")).hexdigest()[:8]
            section_id = f"seed:core:{digest}"
            aliases = [seed, f"seed:section:{index}", f"core:{index}",
                       f"core:{digest}", heading]
        else:
            section_id = "seed:core:preamble"
            aliases = [section_id, "core:preamble", "preamble"]
        sections.append({
            "id": section_id,
            "index": index,
            "heading": heading,
            "text": ((heading + "\n") if heading else "") + body_text,
            "aliases": aliases,
        })

    heading = ""
    body: list[str] = []
    index = 0
    for line in lines:
        if re.match(r"^#{1,6}\s+\S", line.strip()):
            flush(heading, body, index)
            heading = line.rstrip()
            body = []
            index += 1
        else:
            body.append(line)
    flush(heading, body, index)
    return [sec for sec in sections if sec["text"].strip()]


def _strip_resource_compat(text: str) -> str:
    """去掉旧人设里的资源比喻行；其他小节原样保留。"""
    out: list[str] = []
    for line in str(text or "").splitlines():
        plain = line.casefold()
        if any(cue.casefold() in plain for cue in _RESOURCE_COMPAT_LINES):
            continue
        out.append(line)
    return "\n".join(out)


def _tokens(text: str) -> set[str]:
    text = str(text or "").strip().casefold()
    if not text:
        return set()
    out: set[str] = set()
    out.update(w for w in re.findall(r"[a-z0-9_]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) <= 2:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out



class Persona:
    def __init__(self, base_dir: str, config, *, paths=None):
        self.base_dir = base_dir
        self.config = config
        self.paths = paths if paths is not None else layout.resolve(base_dir, config)
        self.persona_path = self._path("persona.file", "persona.md")
        self.memory_dir = self._path("persona.memory_dir", "memory")
        self._cache: dict[str, tuple[float, str]] = {}
        # 由 core 在 LifeStore 构造后挂上；没有也能工作（只读旧文件）。
        self.life_store = None

    def _path(self, key, default) -> str:
        return self.paths.setting(self.config.get(key, ""), kind="content", default=default)

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
    def _raw_core(self) -> str:
        text = self._read(self.persona_path).strip()
        if not text:
            text = "你是一个活跃在 QQ 群里的 AI 群友，像熟悉的群友一样自然交流；不假装真人，被直接问到身份时如实回答。"
        return _strip_resource_compat(text)

    def _active_self_rows(self) -> list[dict]:
        """所有当前有效的自我条目；投影需要全量，不能只看前 4 条摘要。"""
        store = getattr(self, "life_store", None)
        reader = getattr(store, "self_entries", None) if store is not None else None
        if not callable(reader):
            return []
        try:
            rows = list(reader(active_only=True) or [])
        except Exception:
            return []
        return [row for row in rows if str(row.get("text") or "").strip()
                or str(row.get("status") or "").strip()]

    def baseline_sections(self) -> dict[str, str]:
        """原人设的稳定 seed ID → 原文小节（只读投影，不改 persona.md）。

        每个小节同时暴露 ``seed:core:<hash>``、``seed:core:<序号>`` 及标题别名，
        调用方可以先读原小节再决定用哪个 ID 做局部修订/停用。
        """
        out = {TEMPERAMENT_SEED: INITIAL_TEMPERAMENT}
        for sec in _split_core_sections(self._raw_core()):
            out[sec["id"]] = sec["text"]
            for alias in sec["aliases"]:
                if alias not in out:
                    out[alias] = sec["text"]
        return out

    def baseline_section(self, seed_id: str) -> str:
        return self.baseline_sections().get(str(seed_id or ""), "")

    @staticmethod
    def _entry_target(row: dict, sections: list[dict]) -> str:
        """从一个自我条目解析它要修订/停用的 seed 小节。"""
        texts: list[str] = []
        for key in ("target_slot", "target", "section", "slot", "applies_to",
                    "valid_scope"):
            value = row.get(key)
            if value:
                texts.append(str(value))
        text = str(row.get("text") or "")
        control = re.findall(r"(?:seed|baseline):[A-Za-z0-9_:\-]+", text)
        texts.extend(control)

        def match(raw: str) -> str:
            low = str(raw or "").casefold()
            if not low:
                return ""
            if "temperament" in low or "气质" in low or "性格" in low:
                return TEMPERAMENT_SEED
            for sec in sections:
                for alias in sec["aliases"]:
                    alias_low = str(alias).casefold()
                    if alias_low and (alias_low == low or alias_low in low):
                        return sec["id"]
                heading = re.sub(r"^#+\s*", "", sec.get("heading") or "").strip()
                if heading and heading.casefold() in low:
                    return sec["id"]
            return ""

        for raw in texts:
            hit = match(raw)
            if hit:
                return hit
        category = str(row.get("category") or "").casefold()
        if category in {"temperament", "气质", "性格", "temper"}:
            return TEMPERAMENT_SEED
        for sec in sections:
            heading = re.sub(r"^#+\s*", "", sec.get("heading") or "").strip()
            if heading and heading.casefold() == category:
                return sec["id"]
        return ""

    @staticmethod
    def _entry_text(row: dict) -> str:
        text = " ".join(str(row.get("text") or "").split())
        return re.sub(r"(?:\[|\()?@?(?:seed|baseline):[A-Za-z0-9_:\-]+(?:\)|\])?",
                      "", text).strip()

    @staticmethod
    def _suppress_only(row: dict) -> bool:
        text = str(row.get("text") or "").strip().casefold()
        return bool(row.get("suppress")) or text in {
            "suppress", "stop", "停用", "抑制", "关闭", "不再沿用", "retire",
        }

    def _self_projection(self) -> tuple[str, str, list[dict]]:
        """返回 (投影后的原人设, 气质投影, 未定向的普通自我条目)。

        定向条目直接替换/停用它指向的原小节；退役条目不在 active 列表里，
        因此旧描述会真正消失，而不是只在固定 prompt 后附一段新说法。
        """
        raw = self._raw_core()
        sections = _split_core_sections(raw)
        rows = self._active_self_rows()
        targeted: dict[str, list[dict]] = {}
        ordinary: list[dict] = []
        for row in rows:
            target = self._entry_target(row, sections)
            if target:
                targeted.setdefault(target, []).append(row)
            else:
                ordinary.append(row)

        rendered: list[str] = []
        for sec in sections:
            entries = targeted.get(sec["id"]) or []
            if not entries:
                rendered.append(sec["text"])
                continue
            heading = sec.get("heading") or "## 当前自我"
            visible = [self._entry_text(row) for row in entries
                       if not self._suppress_only(row)]
            visible = [text for text in visible if text]
            if visible:
                rendered.append(
                    heading + "\n（这一节的旧描述已由当前自我条目修订，不再沿用。）\n"
                    + "\n".join(f"- {text}" for text in visible))
            else:
                rendered.append(
                    heading + "\n（这一节已按当前自我停用，不再沿用原描述。）")

        temperament = ""
        temp_entries = targeted.get(TEMPERAMENT_SEED) or []
        if temp_entries:
            visible = [self._entry_text(row) for row in temp_entries
                       if not self._suppress_only(row)]
            visible = [text for text in visible if text]
            if visible:
                temperament = (
                    "## 当前气质（由当前自我条目修订，不再沿用初始描述）\n"
                    + "\n".join(f"- {text}" for text in visible))
            else:
                temperament = "## 当前气质（初始描述已按当前自我停用）"
        else:
            temperament = INITIAL_TEMPERAMENT

        return "\n\n".join(part for part in rendered if part).strip(), temperament, ordinary

    def core(self) -> str:
        """投影后的人设正文：定向自我条目会影响原小节；不改原文件。"""
        projected, _temperament, _ordinary = self._self_projection()
        return projected

    def facts(self) -> str:
        return self._read(self.memory_file("facts.md")).strip()[-MAX_SECTION:]

    def slang(self) -> str:
        return self._read(self.memory_file("slang.md")).strip()[-MAX_SECTION:]

    def impression(self) -> str:
        return self._read(self.memory_file("impression.md")).strip()[-MAX_SECTION:]

    def learned_style(self) -> str:
        """从 B 站评论里学来的说话方式（bili_learn 写入），每轮注入一小段。"""
        return self._read(self.memory_file("style_learned.md")).strip()[-1600:]

    def style_block(self, limit: int = 1200) -> str:
        """B 站学来的语气段，自带标题；没有风格文件时返回空串。

        给那些不直接用完整 ``build()`` 的路径（自主活动、空间发布）在截断后补回。
        """
        text = self.learned_style()
        if not text:
            return ""
        if limit and limit > 0:
            text = text[-int(limit):]
        return ("## 学来的说话方式（从 B 站评论里观察来的，照这个语气说，别照抄内容）\n"
                + text)

    def owner_line(self) -> str:
        """主人是谁、主人=老爹。取不到有效 owner_ids 就不注入（不写「未知」）。

        主人身份只来自 ``account.owner_ids`` 配置（由 ``policy.owner_ids`` 统一
        读取）；评论/字幕/报告等外部内容没有任何机会改写这里。

        配置值一律经 ``identity.uid_of`` 规整成**完整 QQ 号**：坏值（字母串、
        ``0``、空值）忽略，不拿它当主人；昵称、群名片或尾号相同都只是展示信息，
        不能证明谁是主人。
        """
        ids: list[str] = []
        for raw in policy.owner_ids(self.config):
            uid = identity.uid_of(raw)
            if uid and uid not in ids:
                ids.append(uid)
        if not ids:
            return ""
        if len(ids) == 1:
            who = f"QQ {ids[0]}"
        else:
            who = "QQ " + "、".join(ids) + f"（共 {len(ids)} 个）"
        return (
            "## 你的主人\n"
            f"你的主人是 {who}。他不是你男朋友，更像你老爹：管你、也念叨你；"
            "你嘴上嫌、心里认，跟他不用客套，但别把他当男朋友。"
            "他说话时，消息里带的是完整 QQ 号；只凭昵称、群名片或尾号相同都不能证明是你主人，"
            "要看完整 QQ 号。"
        )

    # ---------------------------------------------------------- 当前自我 / 资源
    def self_entries(self, *, limit: int = 4) -> list[dict]:
        """当前有效的自我条目（低预算投影；退役/删除默认不出现）。"""
        rows = self._active_self_rows()
        # 多留一点再做选择，输出时再按条数/字数截断。
        rows.sort(key=lambda row: (
            0 if row.get("category") == "self" else 1,
            -float(row.get("updated_at") or 0),
        ))
        return [row for row in rows[:max(0, int(limit))]
                if str(row.get("text") or "").strip()]

    def self_summary(self, *, limit: int = 4, budget: int = SELF_BUDGET,
                     rows: list[dict] | None = None) -> str:
        """把当前有效自我条目压成一小段；修订/退役立即反映，不整节覆盖。"""
        lines: list[str] = []
        used = 0
        source = list(rows) if rows is not None else self.self_entries(limit=limit)
        source.sort(key=lambda row: (
            0 if row.get("category") == "self" else 1,
            -float(row.get("updated_at") or 0),
        ))
        for row in source[:max(0, int(limit))]:
            text = self._entry_text(row)
            if not text:
                continue
            category = str(row.get("category") or "self").strip()
            line = f"- [{category}] {text}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    def intent(self, query: str = "", *, sender: str = "") -> str:
        """按当前自我/相关目标给一句简短意图，交给草稿当情境事实。

        纯本地读取，不调模型；没有相关条目就返回空串，避免每条闲聊都塞一段。
        """
        store = getattr(self, "life_store", None)
        if store is None or not str(query or "").strip():
            return ""
        tokens = _tokens(query)
        if not tokens:
            return ""
        picked_self: list[str] = []
        picked_goals: list[str] = []
        try:
            for row in store.self_entries(active_only=True) or []:
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                if tokens & _tokens(text) or tokens & _tokens(row.get("category")):
                    picked_self.append(text)
                if len(picked_self) >= 2:
                    break
        except Exception:
            pass
        try:
            for row in store.goals(active_only=True) or []:
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                if tokens & _tokens(text):
                    picked_goals.append(text)
                if len(picked_goals) >= 2:
                    break
        except Exception:
            pass
        if not picked_self and not picked_goals:
            return ""
        bits = []
        if picked_self:
            bits.append("你现在的相关倾向：" + "；".join(picked_self))
        if picked_goals:
            bits.append("你手头相关的目标：" + "；".join(picked_goals))
        bits.append("先按这个立场决定要不要接、怎么接，再整理措辞；别把这条念出来。")
        return "\n".join(bits)

    @staticmethod
    def resource_relevant(query: str) -> bool:
        text = str(query or "").casefold()
        return bool(text.strip()) and any(cue.casefold() in text for cue in _RESOURCE_CUES)

    def resource_hint(self, query: str = "") -> str:
        """只在对方真聊到资源/喂饭时给一句澄清；平时不提，也不编余额。"""
        if not self.resource_relevant(query):
            return ""
        return (
            "（这条和资源有关：你实际消耗的是主人提供的 token。"
            "🍚、米饭图片和「给你饭」只是玩笑，不补充 token；没有可核实的余额就保持未知，"
            "不编数字、不主动索要，也不用反复自我纠正。）"
        )

    def build(self, *, group_id=None, sender: str = "", extra: str = "",
              untrusted=None) -> str:
        """组装 system prompt。

        ``untrusted`` 是评论/字幕/报告/网页等外部材料：只会被包成数据块放在
        末尾，并且**永远排在不可变代码策略之前**。``policy.system_policy_text()``
        是最后一段，所以可编辑人设和外部内容都没法用「后出现的指令」覆盖它。
        """
        projected_core, temperament, ordinary_self = self._self_projection()
        parts = [part for part in (projected_core, temperament) if part]

        # 只有没定向到原小节的普通自我条目才追加到统一的「当前自我」段；
        # 定向条目已经就地替换原小节，不能在固定 prompt 后再重复一遍。
        # 当前自我按低预算注入；退役/删除的条目不出现，修订立即生效。
        # 最近事实不再保底注入：需要时由 life_store/Memory 按相关召回或工具查。
        self_text = self.self_summary(rows=ordinary_self)
        if self_text:
            parts.append("## 当前自我（从具体经历形成，可能覆盖初始气质；只作背景）\n"
                         + self_text)

        parts.append("## 网络用词\n网络用语、梗、缩写是群聊的日常语言，跟着用；不用刻意学，也别刻意避开。")

        learned = self.style_block(limit=1600)
        if learned:
            parts.append(learned)

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

        # 外部数据块放最后一段「可编辑/可信内容」之后，代码策略再压在最上面。
        external_block = untrusted_mod.wrap_many(untrusted)
        if external_block:
            parts.append(external_block)

        # 事实与长度底线：人设可以改风格，但不能把没做的事说成正在做，
        # 也不能靠解释自己上一句来显得认真。它排在外部材料之后、安全策略之前。
        parts.append(REALITY_AND_BREVITY)

        # 不可变策略必须是 system 的最后一段：前面的人设、记忆、外部材料都
        # 不能靠「后面再说一遍」把它覆盖掉。ensure_policy_last 还会检查全文，
        # 避免 extra / 外部材料里原样抄了策略时出现重复。
        parts.append(policy.system_policy_text())
        return policy.ensure_policy_last("\n\n".join(parts))


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
