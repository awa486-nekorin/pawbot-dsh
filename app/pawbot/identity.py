"""稳定身份：完整 QQ 号标签、昵称清洗、同一 uid 改名识别。

聊天记录会被拼进 system 提示词，所以「谁说的」不能只靠一个昵称字符串：

  * 只写尾号会把尾号相同的两个人并成一个人（QQ 号后四位撞车很常见）；
  * 群名片可以随时改，改完如果按名字认人，同一个人就变成两个；
  * 昵称里带换行/引号/「系统」字样时，拼出来的记录行可以被伪造成另一条记录。

这里统一提供这些工具：

    uid_of(value)                  OneBot 的 int / 纯数字 str / None → 纯数字字符串（空 = 未知）
    sanitize_name(value)           控制字符、换行压平，限长，括号/冒号全角化
    label(row)                     「昵称(uid:123456789)」或「昵称(uid未知)」，自己是固定自称
    identities(rows)               按 uid 归并，记录当前昵称与曾用名
    utterance_text(row)            这条记录「实际说出口的文字」（不含附件转述）
    attachments_of(row)            这条记录的附件元数据（结构化，和正文分开）
    record(row, time_text=...)     给活跃期记录用的结构化字段（JSON 友好）

**记录渲染契约**（legacy / v2 共用，见 `shortterm.render`）：

    {"time": "...", "role": "user", "uid": "123456789", "name": "阿明",
     "text": "看这个", "attachments": [
         {"type": "image", "desc": "一只猫", "uid": "123456789",
          "name": "阿明", "source": "annotation", "legacy": false}]}

`text` 只放说话人真正打出来的字；`[图片：…]` 这类视觉转述是附件元数据，不能
被当成他说的话。新版 `Store.annotate_last` 会把转述写成 `attachments` 并保留
`original_content`；旧数据里已经写进正文的 `[图片：…]` 只在**没有新版标记**时
按 legacy 解析，并标记 `legacy=true`，不会反过来改写新版用户亲手打出的字面量。

注意：这里只做「认人」，不改 `ctxwin.speaker` 的旧输出 —— 旧窗口的
「昵称(尾号1234)」仍是兼容格式。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SELF_LABEL = "我（大肥鱼）"
FALLBACK_NAME = "群友"
UNKNOWN_UID_TEXT = "uid未知"
NAME_LIMIT = 40

# 名字里的结构符号全角化：即使群名片写成「阿明(uid:123)」也拼不出第二个身份标签。
_SPOOF_MAP = str.maketrans({
    "(": "（", ")": "）", ":": "：",
    "[": "［", "]": "］", "<": "＜", ">": "＞",
    "{": "｛", "}": "｝", "`": "｀",
    '"': "＂", "'": "＇",
})

# 这些昵称会和「我自己」混淆：加「群友·」前缀，避免模型把群友的话当成自己说的。
_SELF_ALIASES = {
    "我", "大肥鱼", "机器人", "bot", "assistant", "system", "user",
    "管理员", "助手", "主人",
}


_LEGACY_UID_LABEL = re.compile(r"(?i)uid\s*:\s*([0-9]+)")


def uid_of(value) -> str:
    """OneBot 的 uid → 十进制字符串；空 / 非纯数字 / 0 视为未知。

    只接受**整个**值都是 ASCII 数字（或本来就是整数）的情况：

        uid_of(123)          -> "123"
        uid_of(" 123 ")      -> "123"
        uid_of("1234567890") -> "1234567890"
        uid_of("attacker123")-> ""       # 不能从任意字符串里抠数字
        uid_of("123abc")     -> ""
        uid_of("１２３")      -> ""       # 全角数字也不是 QQ 号

    兼容旧的显式标签 `uid:123`（现有测试/旧调用会传），但它只认这个固定前缀；
    任意字母、昵称、别名都不会被抽成 uid，避免「attacker123 → 123」这种把
    无关字符串当成授权对象的路径。

    `0` 是 NapCat 缺字段时的占位，不是真实 QQ 号；保留它会让所有缺字段的人
    都变成「同一个人」。前导零按整数规范化（"00123" → "123"）。
    """
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value > 0 else ""
    if isinstance(value, float):
        if value.is_integer() and value > 0:
            return str(int(value))
        return ""
    text = str(value if value is not None else "").strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return ""
    # 只放宽一个明确的旧标签前缀；不做「从字符串里提取数字」。
    match = _LEGACY_UID_LABEL.fullmatch(text)
    if match:
        text = match.group(1)
    if not text.isascii() or not text.isdigit():
        return ""
    try:
        number = int(text)
    except ValueError:
        return ""
    return str(number) if number > 0 else ""


def sanitize_name(value, *, limit: int = NAME_LIMIT) -> str:
    """把群名片洗成**单行、限长、不能伪造标签**的名字。

    空名字返回空串，由调用方决定回退成「群友」还是「未知」。
    """
    text = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    # 换行、制表、零宽字符、段落分隔都压成空格；不保留任何能「另起一行」的东西。
    text = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")
        else ch
        for ch in text
    )
    text = " ".join(text.split())
    text = text.translate(_SPOOF_MAP).strip()
    if not text:
        return ""
    if SELF_LABEL in text or text.casefold() in _SELF_ALIASES:
        text = "群友·" + text
    if len(text) > max(1, int(limit)):
        text = text[:max(1, int(limit))].rstrip() + "…"
    return text


def display_name(row: dict) -> str:
    """这一行说话人的展示名；自己是固定自称，空名字回退「群友」。"""
    if isinstance(row, dict) and row.get("role") == "assistant":
        return SELF_LABEL
    name = sanitize_name(row.get("who") if isinstance(row, dict) else "")
    return name or FALLBACK_NAME


def label(row: dict) -> str:
    """一行记录的说话人标签：**完整 uid**，不是只看尾号。"""
    if not isinstance(row, dict):
        return f"{FALLBACK_NAME}({UNKNOWN_UID_TEXT})"
    if row.get("role") == "assistant":
        return SELF_LABEL
    name = display_name(row)
    uid = uid_of(row.get("uid"))
    return f"{name}(uid:{uid})" if uid else f"{name}({UNKNOWN_UID_TEXT})"


def _ts(row: dict) -> float:
    try:
        return float((row or {}).get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Identity:
    """一个 QQ 号在这段记录里的身份快照。"""

    uid: str
    name: str                     # 当前（最近一次）昵称
    aliases: tuple[str, ...] = ()  # 曾用名，按出现顺序去重，不含当前昵称
    first_ts: float = 0.0
    last_ts: float = 0.0
    count: int = 0

    @property
    def renamed(self) -> bool:
        return bool(self.aliases)

    @property
    def label(self) -> str:
        return f"{self.name}(uid:{self.uid})"


def identities(rows) -> list[Identity]:
    """按 uid 归并说话人；同 uid 改名字只算一个人，并把旧名字记进曾用名。

    没有 uid 的行**不**归并：昵称相同也可能不是同一个人，不能猜。
    """
    seen: dict[str, dict] = {}
    order: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("role") == "assistant":
            continue
        uid = uid_of(row.get("uid"))
        if not uid:
            continue
        name = display_name(row)
        ts = _ts(row)
        rec = seen.get(uid)
        if rec is None:
            rec = {"uid": uid, "name": name, "aliases": [], "first_ts": ts,
                   "last_ts": ts, "count": 0}
            seen[uid] = rec
            order.append(uid)
        else:
            if name and name != rec["name"]:
                old = rec["name"]
                if old and old != name and old not in rec["aliases"]:
                    rec["aliases"].append(old)
                rec["name"] = name
            if ts:
                rec["first_ts"] = ts if not rec["first_ts"] else min(rec["first_ts"], ts)
                rec["last_ts"] = max(rec["last_ts"], ts)
        rec["count"] += 1

    出: list[Identity] = []
    for uid in order:
        rec = seen[uid]
        aliases = tuple(a for a in rec["aliases"] if a and a != rec["name"])
        出.append(Identity(uid=rec["uid"], name=rec["name"], aliases=aliases,
                          first_ts=rec["first_ts"], last_ts=rec["last_ts"],
                          count=rec["count"]))
    return 出


def identity_lines(idents) -> str:
    """给提示词看的身份表：完整 uid + 当前昵称 + 曾用名。"""
    行: list[str] = []
    for it in idents or []:
        if not isinstance(it, Identity):
            continue
        line = f"- uid {it.uid}：现在叫「{it.name}」"
        if it.aliases:
            line += "；曾用名：" + "、".join(f"「{a}」" for a in it.aliases)
            line += "（同一个人，按 uid 认，别当成两个人）"
        行.append(line)
    return "\n".join(行)


def unknown_name_lines(rows, *, limit: int = 12) -> str:
    """没有 uid 的说话人：明确标成「uid未知」，并说明不能按昵称合并。"""
    names: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("role") == "assistant":
            continue
        if uid_of(row.get("uid")):
            continue
        name = display_name(row)
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    picked = names[:max(0, int(limit or 0))]
    行 = [f"- uid未知：「{n}」（没拿到 QQ 号，不能确认和同名的人是不是同一个）"
          for n in picked]
    if len(names) > len(picked):
        行.append(f"- 另有 {len(names) - len(picked)} 个没拿到 QQ 号的昵称未列出。")
    return "\n".join(行)


def uids_for_name(rows, name, *, exclude=()) -> list[str]:
    """窗口里某个昵称对应的所有已知 uid（去重、保持出现顺序）。

    返回长度 > 1 就代表「同名但无法确定是谁」——调用方必须放弃按名字反查，
    绝不能取最近一条硬猜。
    """
    target = sanitize_name(name)
    if not target:
        return []
    skip = {uid_of(x) for x in (exclude or ()) if uid_of(x)}
    出: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("role") == "assistant":
            continue
        uid = uid_of(row.get("uid"))
        if not uid or uid in skip:
            continue
        if sanitize_name(row.get("who")) != target:
            continue
        if uid not in 出:
            出.append(uid)
    return 出


#: 图片转述/占位符：`[图片]`、`[图片：描述]`、`[图片:描述]`。
_IMAGE_TOKEN_RE = re.compile(r"\[图片(?:[：:]([^\]]*))?\]")
_BARE_IMAGE_RE = re.compile(r"\[图片\]")
_IMAGE_SOURCES = frozenset({"annotation", "placeholder", "legacy_inline"})


def _one_line(value) -> str:
    return " ".join(str(value if value is not None else "").split())


def image_attachment(row: dict, desc, *, source: str = "annotation",
                     uid=None, legacy: bool = False) -> dict:
    """构造一条附件元数据；uid 是**发这张图的人**（完整 QQ 号）。"""
    row = row if isinstance(row, dict) else {}
    source_uid = uid_of(uid if uid is not None else row.get("uid"))
    if row.get("role") == "assistant":
        name = SELF_LABEL
    else:
        name = sanitize_name(row.get("who")) or display_name(row)
    return {
        "type": "image",
        "desc": _one_line(desc),
        # `uid` 是主字段；`source_uid` 是同一事实的显式别名，方便调用方按
        # 「附件的来源」读取，不依赖说话人行上的 uid。
        "uid": source_uid or None,
        "source_uid": source_uid or None,
        "name": name,
        "source": str(source or "annotation"),
        "legacy": bool(legacy),
    }


def _normalize_attachment(item, row: dict, *, fallback_source: str) -> dict:
    if isinstance(item, dict):
        desc = item.get("desc", "")
        source = item.get("source") or fallback_source
        uid = item.get("uid", item.get("source_uid"))
        legacy = bool(item.get("legacy"))
        out = image_attachment(row, desc, source=str(source), uid=uid, legacy=legacy)
        kind = _one_line(item.get("type") or item.get("kind") or "image")
        if kind:
            out["type"] = kind
        name = item.get("name")
        if name:
            out["name"] = sanitize_name(name) or out["name"]
        return out
    return image_attachment(row, item, source=fallback_source)


def _legacy_split(text: str) -> tuple[str, list[dict]]:
    """旧数据兼容：把已经写进正文的 `[图片：…]` / `[图片]` 拆成附件。

    只在行**没有**新版 `attachments` 标记时调用。描述会带 `legacy=true`，
    表示这是从旧正文里解析出来的兼容信息，不是新版写入的结构化附件。
    """
    raw = str(text or "")
    if not _IMAGE_TOKEN_RE.search(raw):
        return raw, []
    out: list[dict] = []
    for match in _IMAGE_TOKEN_RE.finditer(raw):
        # row 在调用处再补；这里只保留描述和来源，避免正则阶段依赖身份。
        out.append({"desc": _one_line(match.group(1) or ""),
                    "source": "legacy_inline", "legacy": True})
    clean = _IMAGE_TOKEN_RE.sub("", raw)
    clean = re.sub(r"[ \t]{2,}", " ", clean).strip()
    return clean, out


def _strip_bare_placeholders(text: str) -> tuple[str, int]:
    """新版行：只去掉裸的 `[图片]`（真图占位），保留用户手打的 `[图片：…]`。"""
    raw = str(text or "")
    count = len(_BARE_IMAGE_RE.findall(raw))
    if not count:
        return raw, 0
    clean = _BARE_IMAGE_RE.sub("", raw)
    clean = re.sub(r"[ \t]{2,}", " ", clean).strip()
    return clean, count


def attachments_of(row: dict) -> list[dict]:
    """这一行的附件元数据（结构化，和 `text` 分开）。

    * 新版行（有 `attachments` 键）：以 Store 写入的元数据为准；裸 `[图片]`
      补成空描述的占位附件。`annotate_last` 已经把 `original_content` 和
      转述都存好时，不再从正文里猜。
    * 旧版行（没有 `attachments` 键）：把旧 `[图片：…]` 解析成 `legacy`
      附件，保证升级后历史也按附件语义渲染。
    * 没有附件就是空列表。
    """
    if not isinstance(row, dict):
        return []
    if "attachments" not in row:
        _clean, legacy = _legacy_split(str(row.get("content") or ""))
        return [
            image_attachment(row, item["desc"], source=item["source"],
                             uid=row.get("uid"), legacy=True)
            for item in legacy
        ]

    meta = row.get("attachments")
    out: list[dict] = []
    if isinstance(meta, list):
        for item in meta:
            if isinstance(item, (dict, str)):
                out.append(_normalize_attachment(item, row,
                                                 fallback_source="annotation"))
    # 新版行如果已经由 legacy/Agent 路径把正文里的标记删掉了，就不要再从
    # 元数据里重复注入同一张图；Agent legacy 会单独生成附件块。
    if (out and isinstance(row.get("original_content"), str)
            and not _IMAGE_TOKEN_RE.search(str(row.get("content") or ""))):
        return []
    if not out:
        base = (row.get("original_content")
                if isinstance(row.get("original_content"), str)
                else row.get("content"))
        _clean, placeholders = _strip_bare_placeholders(str(base or ""))
        for _ in range(placeholders):
            out.append(image_attachment(row, "", source="placeholder",
                                        uid=row.get("uid")))
    return out


def typed_inline_tokens(row: dict) -> list[str]:
    """新版行正文里**没有对应附件元数据**的 `[图片：…]`：用户手打的字面量。

    旧版行（没有 `attachments` 键）返回空列表：那些标记按 legacy 转述处理，
    不能反过来当成「用户手打」。
    """
    if not isinstance(row, dict) or "attachments" not in row:
        return []
    meta = row.get("attachments")
    descs = set()
    if isinstance(meta, list):
        for item in meta:
            if isinstance(item, dict):
                desc = _one_line(item.get("desc"))
                if desc:
                    descs.add(desc)
    out: list[str] = []
    for match in _IMAGE_TOKEN_RE.finditer(str(row.get("content") or "")):
        if match.group(1) is None:
            continue
        if _one_line(match.group(1)) not in descs:
            out.append(match.group(0))
    return out


def utterance_text(row: dict) -> str:
    """这条记录里「人真正说出口/打出来的文字」，不含图片转述。

    新版行优先用 `original_content`（`annotate_last` 在补描述前保存的原文），
    只去掉真图占位 `[图片]`，**不**去掉用户手打的 `[图片：…]` 字面量；旧版行
    才把 `[图片：…]` 当兼容附件拆掉。
    """
    if not isinstance(row, dict):
        return ""
    override = row.get("_text_override")
    if isinstance(override, str):
        return override
    if "attachments" not in row:
        clean, _legacy = _legacy_split(str(row.get("content") or ""))
        return clean

    original = row.get("original_content")
    base = original if isinstance(original, str) else str(row.get("content") or "")
    text, placeholders = _strip_bare_placeholders(base)
    # 手工构造的新版行（没有 original_content）如果元数据描述仍出现在正文里，
    # 去掉那些描述的标记；有 original_content 时正文里的字面量是用户打的，保留。
    if not isinstance(original, str) and isinstance(row.get("attachments"), list):
        descs = {_one_line(item.get("desc")) for item in row["attachments"]
                 if isinstance(item, dict) and _one_line(item.get("desc"))}
        if descs:
            text = _IMAGE_TOKEN_RE.sub(
                lambda m: "" if _one_line(m.group(1) or "") in descs else m.group(0),
                text)
            if _IMAGE_TOKEN_RE.search(str(row.get("content") or "")):
                text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text


def record(row: dict, *, time_text: str) -> dict:
    """一条记录的结构化身份字段（JSON 序列化友好）。

    `uid` 为 null 表示没拿到 QQ 号；`role=assistant` 表示这条是机器人自己发的。
    `text` 是实际说出口的文字，`attachments` 是图片等附件元数据；正文里的
    换行/引号/伪造 JSON 都只是字段值，不会变成结构里的另一条记录。
    """
    row = row if isinstance(row, dict) else {}
    text = utterance_text(row)
    attachments = attachments_of(row)
    if row.get("role") == "assistant":
        return {"time": str(time_text), "role": "assistant", "uid": None,
                "name": SELF_LABEL, "text": text, "attachments": attachments}
    uid = uid_of(row.get("uid"))
    return {"time": str(time_text), "role": "user", "uid": uid or None,
            "name": display_name(row), "text": text,
            "attachments": attachments}
