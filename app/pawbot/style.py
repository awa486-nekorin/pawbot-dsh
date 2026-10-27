"""Small, deterministic cleanup for model-generated QQ replies.

It only removes known mechanical tics (dash punctuation, self-quoting, "行，"
openers, meta apologies) and never rewrites meaning. URLs, CQ codes, code
spans and markdown links are shielded so they survive untouched.
"""

from __future__ import annotations

import json
import os
import re
import zlib

_PROTECTED_RE = re.compile(
    r"\[CQ:[^\]\r\n]*\]"
    r"|```[\s\S]*?```"
    r"|~~~[\s\S]*?~~~"
    r"|`[^`\r\n]*`"
    r"|https?://[^\s<>\"']+"
    r"|!\[[^\]\r\n]*\]\([^\r\n)]*\)"
    r"|\[[^\]\r\n]*\]\([^\r\n)]*\)"
    r"|<[^>\r\n]+>"
)
_SPLIT_RE = re.compile(rf"(?P<protected>{_PROTECTED_RE.pattern})|(?P<separator>‖)")
_QUOTE_PAIRS = (("“", "”"), ("「", "」"), ('"', '"'))
# 只去掉机械的开头：「行行行」「好好好」「哦，懂了」「成，」。单个「行，」「好，」是正常口语，保留。
_OPENER_RE = re.compile(
    r"^[ \t]*(?:行{2,}|好{2,}|哦\s*[,，]\s*懂了"
    r"|(?:成|懂了)[ \t]*[,，、:：!！。…]+)"
)
# 地域腔：北京大爷/东北大妈式的词换成中性说法（先长后短，避免「老铁们」被拆开）
_DIALECT = (
    ("您猜怎么着", "你猜怎么着"), ("老铁们", "大家"), ("老铁", "朋友"), ("得嘞", "好"),
    ("整一个", "来一个"), ("咋整", "怎么办"), ("贼拉", "超"), ("嘎嘎", "超"),
    ("唠嗑", "聊天"), ("埋汰", "损"), ("忽悠", "骗"), ("熊样", "样子"), ("嘚瑟", "得意"),
)
_YO_RE = re.compile(r"(^|[。！？!?，,\n])[ \t]*[喲哟][ \t]*[，,！!～~。]*[ \t]*")
# 堆儿化音：同一条里出现两次以上才去，避免误伤「女儿」「婴儿」这类正常词
_ERHUA = (("一会儿", "一会"), ("有点儿", "有点"), ("这儿", "这里"), ("那儿", "那里"),
          ("哪儿", "哪里"), ("玩儿", "玩"), ("事儿", "事"), ("劲儿", "劲"),
          ("味儿", "味"), ("样儿", "样"), ("块儿", "块"), ("点儿", "点"))
_SENTENCE_SPLIT_RE = re.compile(r"([。！？!?；;\n])")
# emoji：回复里不放彩色表情符号。♡♥★☆✧♪ 这些当符号用的留着（語C 在用）。
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 表情、旗帜、图标、符号扩充
    "\U00002B00-\U00002BFF"   # 星形、箭头
    "\U00002600-\U000027BF"   # 杂项符号
    "\U0000FE0F\U0000200D"    # 变体选择符 / 零宽连接符
    "]"
)
_EMOJI_KEEP = set("♡♥★☆✧✦♪♫")


def strip_emoji(text: str) -> str:
    """删掉 emoji，保留 ♡ 这类当语气符号的字符；顺手收掉删完留下的空格。"""
    if not text:
        return text
    out = _EMOJI_RE.sub(
        lambda m: "".join(c for c in m.group(0) if c in _EMOJI_KEEP), text)
    if out == text:
        return text
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+(?=[，。！？、,.!?；;：:])", "", out)
    return out.strip(" ")
# 括号里的神态/动作旁白：语C 和暧昧都不要，发送前直接去掉（两个字以上才算，
# 「（笑）」这种一个字的不动）。
_ACTION_RE = re.compile(r"（[^（）\n]{2,40}）")      # 只认全形括号，半形 ( ) 常是链接/代码
# 语C/暧昧判定：靠声音本身。必须有「喘息/爱心」这类核心标记，
# 再加至少一种「结巴 / 连续省略 / 拖音」，普通闲聊里一个「嗯……」不会误判。
_RP_CORE = re.compile(r"[♡❤]|[嗯啊呜唔咿嘤]{1,3}[…～~]{1,}")
_RP_EXTRA = (
    re.compile(r"([\u4e00-\u9fff])、\1"),                          # 不、不要  谁、谁
    re.compile(r"…+[^…]{0,8}…+[^…]{0,8}…+"),                        # 一段里三处以上省略
    re.compile(r"[～~][^～~]{0,10}[～~]"),                          # 多处拖音
)
# 语C 不按闲聊的句数规则截断，只留一个宽松的总字数上限。
ROLEPLAY_MAX_CHARS = 600
# 「我认」本身是正常说法，只有这句自我通报式的横线梗句要整句丢掉
_META_SENTENCE_RE = re.compile(r"(?:那串)?横线[ \t]*当场退役(?:了)?|当场退役(?:了)?")
# 句末抛回给对方的套话问句（只匹配整条消息最后一句）
_TAG_QUESTION_RE = re.compile(
    r"(?:^|(?<=[。！？!?…~～，,\s]))[ \t]*"
    r"(?:那|那么)?(?:你|你们)(?:呢|觉得呢|觉得怎么样|怎么看|怎么想|说呢|觉得咋样|觉得如何)"
    r"[ \t]*[？?]+[ \t]*$"
    r"|(?:^|(?<=[。！？!?…~～，,\s]))[ \t]*(?:对吧|是吧|是不是|对不对)[ \t]*[？?]+[ \t]*$"
)


def _is_roleplay(text: str) -> bool:
    """语C/暧昧：靠喘息、结巴、拖音这些声音标记判断，或者模型还是写了动作旁白。"""
    if not text:
        return False
    shielded, _ = _shield_markup(text)
    if _ACTION_RE.search(shielded):
        return True
    if not _RP_CORE.search(shielded):
        return False
    return any(pat.search(shielded) for pat in _RP_EXTRA)


def strip_actions(text: str) -> str:
    """去掉括号里的神态/动作旁白，只留说出口的话。"""
    out = _ACTION_RE.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


# 分条上限：配置 reply.max_parts（默认 3）。polish 拿不到 bot/config 对象，
# 所以直接读 data/config.json；按 mtime 缓存，配置改完下一句就生效。
_DEFAULT_MAX_PARTS = 3
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")
_max_parts_cache: tuple[float, int] | None = None


def _configured_max_parts() -> int:
    """读 reply.max_parts；文件不存在或写得不对就退回默认值。"""
    global _max_parts_cache
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        return _DEFAULT_MAX_PARTS
    if _max_parts_cache is not None and _max_parts_cache[0] == mtime:
        return _max_parts_cache[1]
    value = _DEFAULT_MAX_PARTS
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        section = raw.get("reply")
        if isinstance(section, dict) and section.get("max_parts") is not None:
            value = int(section["max_parts"])
    except Exception:
        value = _DEFAULT_MAX_PARTS
    value = max(1, value)
    _max_parts_cache = (mtime, value)
    return value


# 新规则也走同一份 data/config.json；同样按 mtime 缓存，改完下一句就生效。
_reply_cache: tuple[float, dict] | None = None


def _configured_reply() -> dict:
    """读整个 reply 段（缺省时给空 dict）；只在文件 mtime 变化时重读。"""
    global _reply_cache
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        return {}
    if _reply_cache is not None and _reply_cache[0] == mtime:
        return _reply_cache[1]
    section: dict = {}
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        s = raw.get("reply")
        if isinstance(s, dict):
            section = s
    except Exception:
        section = {}
    _reply_cache = (mtime, section)
    return section


def _configured_strip_ai() -> bool:
    v = _configured_reply().get("strip_ai_phrases")
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "off", "no", "")
    return bool(v)


def _configured_body_reaction_max() -> int:
    v = _configured_reply().get("body_reaction_max")
    if v is None:
        return 1
    try:
        return max(0, int(v))
    except Exception:
        return 1


def _configured_strip_filler_opener() -> bool:
    v = _configured_reply().get("strip_filler_opener")
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "off", "no", "")
    return bool(v)


def _configured_tone_trim() -> float:
    """句末语气词（啊/嘛/啦/呀/哦/哟）的削减比例，0~1；0 关掉。

    「吧」「吗」不在此列——它们是疑问/商量语气，删了会改意思。
    """
    v = _configured_reply().get("tone_trim")
    if v is None:
        return 0.7
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.7


def _split_parts(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for match in _SPLIT_RE.finditer(text):
        if match.group("separator"):
            parts.append(text[start:match.start()])
            start = match.end()
    parts.append(text[start:])
    return parts


def _unwrap_outer_quotes(part: str) -> str:
    """模型把自己的台词整个用引号括起来念时，去掉这层引号。"""
    part = part.strip()
    changed = True
    while changed:
        changed = False
        for left, right in _QUOTE_PAIRS:
            if len(part) >= 2 and part.startswith(left) and part.endswith(right):
                part = part[len(left):-len(right)].strip()
                changed = True
                break
    return part


def _shield_markup(text: str) -> tuple[str, dict[str, str]]:
    nonce = "\x00PAWBOT"
    while nonce in text:
        nonce += "_"
    protected: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        marker = f"{nonce}{len(protected)}\x00"
        protected[marker] = match.group(0)
        return marker

    return _PROTECTED_RE.sub(replace, text), protected


def _drop_meta_sentences(text: str) -> str:
    """去掉「横线当场退役」这类自我通报的整句，连它后面的标点一起。"""
    chunks = _SENTENCE_SPLIT_RE.split(text)
    out: list[str] = []
    skip_next = False
    for index, chunk in enumerate(chunks):
        if skip_next:
            skip_next = False
            continue
        if index % 2 == 0 and _META_SENTENCE_RE.fullmatch(chunk.strip()):
            skip_next = True
            continue
        out.append(chunk)
    return "".join(out)


def _neutralize_dialect(text: str) -> str:
    for old, new in _DIALECT:
        text = text.replace(old, new)
    text = _YO_RE.sub(lambda m: m.group(1), text)
    if sum(text.count(word) for word, _ in _ERHUA) >= 2:
        for word, plain in _ERHUA:
            text = text.replace(word, plain)
    return text


def _clean_punctuation(text: str) -> str:
    text = re.sub(r"[ \t]*[,，]+[ \t]*", "，", text)   # 「、」是顿号，别改成逗号
    text = re.sub(r"[ \t]*，+[ \t]*([。！？!?；;])", r"\1", text)
    text = re.sub(r"([。！？!?；;])(?:[ \t]*[。！？!?；;])+", r"\1", text)
    text = re.sub(r"(^|[。！？!?；;])[ \t]*，", r"\1", text)
    text = re.sub(r"\n[ \t]*(?:\n[ \t]*){2,}", "\n\n", text)
    return text


def _drop_tag_question(parts: list[str]) -> list[str]:
    """去掉「说一句 + 你呢？」式的公式收尾；整条回复只有这一问时保留。"""
    if not parts:
        return parts
    last = parts[-1]
    match = _TAG_QUESTION_RE.search(last)
    if not match:
        return parts
    head = last[:match.start()].rstrip(" \t，,、")
    if head:
        if head[-1] not in "。！？!?…~～":
            head += "。" if not re.search(r"[嘛啦呀哦诶唔哼呢吧]$", head) else ""
        return parts[:-1] + [head]
    if len(parts) > 1:
        return parts[:-1]
    return parts


_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


def _drop_markdown_emphasis(part: str) -> str:
    """群里不显示 markdown：**加粗** 的两颗星会原样露出来，去掉但保留文字。"""
    return _BOLD_RE.sub(r"\1", part)


def _polish_part(part: str) -> str:
    part = _unwrap_outer_quotes(part)
    visible, protected = _shield_markup(part)

    opener = _OPENER_RE.match(visible)
    if opener and visible[opener.end():].strip():
        visible = visible[opener.end():].lstrip(" \t,，、:：!！。…")

    visible = _drop_meta_sentences(visible)
    visible = _neutralize_dialect(visible)
    visible = re.sub(r"(?:——|—)+", "，", visible)
    visible = re.sub(r"(?<=\S)[ \t]+/[ \t]+(?=\S)", "，", visible)
    visible = _clean_punctuation(visible).strip(" \t\r\n,，、")

    visible = strip_emoji(visible)
    for marker, original in protected.items():
        visible = visible.replace(marker, original)
    return _drop_markdown_emphasis(visible)


_SENT_TOKEN_RE = re.compile(
    r"[^。！？!?‖\n]*[。！？!?]+[…」』”\"）)]*|[^。！？!?‖\n]+|‖|\n")


def cap_sentences(text: str, n: int, max_chars: int = 200) -> str:
    """闲聊回复最多留前 n 句。代码、链接、CQ 码、过长的解释原样放过，只截聊天。"""
    if not text or n < 1 or "<pass>" in text:
        return text
    if _is_roleplay(text):
        # 语C 不按句数截，只留一个宽松的总字数上限
        if len(text) <= ROLEPLAY_MAX_CHARS:
            return text
        return text[:ROLEPLAY_MAX_CHARS].rstrip(" \t\r\n，,、") + "……"
    if len(text) > max_chars:
        return text
    if _PROTECTED_RE.search(text):
        return text
    kept: list[str] = []
    count = 0
    for tok in _SENT_TOKEN_RE.findall(text):
        if tok in ("‖", "\n"):
            if count >= n:
                break
            kept.append(tok)
            continue
        if count >= n:
            break
        kept.append(tok)
        count += 1
    out = "".join(kept).strip(" \t\r\n‖")
    return out or text


_FOLLOWUP_RE = re.compile(
    r"(?:老实|老實)?交代|快说|快說|说说|說說|讲讲|講講|说来听听|說來聽聽|展开说说|详细说说|"
    r"別裝|别装|到底|怎么想|怎麼想|想啥|说来|講講|讲来|坦白|快跟我|跟我说说|跟我讲讲")


def _is_followup(sentence: str) -> bool:
    """这句是不是「追问式收尾」：问号结尾，或者短句里塞了催人交代的话。"""
    s = sentence.strip()
    if not s:
        return False
    if s.endswith(("？", "?")):
        return True
    if len(s) <= 14 and _FOLLOWUP_RE.search(s):
        return True
    return len(s) <= 8 and s[-1] in "嘛呢吧呀啊"


def _tail_is_prompt(tail: str) -> bool:
    """逗号后面这一小截是不是在催人交代/追问，而不是真在问一件正事。"""
    t = tail.strip()
    if not t:
        return False
    if _FOLLOWUP_RE.search(t):
        return True
    if t.startswith(("你", "咱", "快", "来", "说说", "讲讲")):
        return True
    return len(t) <= 10


def drop_trailing_question(text: str, max_chars: int = 200) -> str:
    """把最后那截追问去掉，让回复落在陈述上。整条只剩这一问时保留，免得无话可说。"""
    if not text or "<pass>" in text or len(text) > max_chars:
        return text
    if _is_roleplay(text):
        return text
    if _PROTECTED_RE.search(text):
        return text
    parts = _split_parts(text)
    idx = next((i for i in range(len(parts) - 1, -1, -1) if parts[i].strip()), -1)
    if idx < 0:
        return text
    chunk = parts[idx]
    sentences = [t for t in _SENT_TOKEN_RE.findall(chunk) if t.strip()]
    if not sentences or not _is_followup(sentences[-1]):
        return text
    if len(sentences) >= 2:
        kept_sentences = sentences[:-1]
    else:
        # 只有一句时，从最后一个逗号切开：前半句是真内容，后半句多是「快跟我讲讲嘛」
        last = sentences[-1].strip()
        body = last[:-1] if last.endswith(("？", "?")) else last
        cut = max(body.rfind("，"), body.rfind(","))
        tail = body[cut + 1:] if cut > 0 else ""
        if cut <= 0 or not tail.strip() or not _tail_is_prompt(tail):
            return text
        kept_sentences = [body[:cut].rstrip() + "。"]
    kept = "".join(kept_sentences).rstrip(" \t，,、")
    if kept and kept[-1] not in "。！？!?…~～":
        kept += "。"
    rest = [p for p in parts[:idx] + [kept] if p.strip()]
    out = "‖".join(rest)
    return out if out.strip() else text


# 模型偷懒用换行代替 ‖ 时，把段落换行当成「另起一条」。
# protected 分支保证网址/代码/CQ 码里的换行不被当成段落分隔。
_NEWLINE_RE = re.compile(
    rf"(?P<protected>{_PROTECTED_RE.pattern})|(?P<break>\n(?:[ \t]*\n)*)")
# 条列清单（- / * / 1.）本来就要换行，整段原样保留，不拆条
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*•·][ \t]+|\d{1,3}[.)、][ \t]+)", re.MULTILINE)


def _split_newlines(text: str) -> list[str]:
    """把闲聊回复里段落之间的换行换成 ‖；受保护内容和清单保持原样。"""
    if "\n" not in text:
        return [text]
    if "```" in text or "~~~" in text or _LIST_ITEM_RE.search(text):
        return [text]
    pieces: list[str] = []
    start = 0
    for match in _NEWLINE_RE.finditer(text):
        if not match.group("break"):
            continue
        pieces.append(text[start:match.start()])
        start = match.end()
    pieces.append(text[start:])
    return [piece.strip() for piece in pieces if piece.strip()]


def _polish_roleplay_part(part: str) -> str:
    """语C 里：去掉括号神态旁白，其余引号、结巴、喘息、句末追问原样留着，换行接成一行。"""
    part = strip_emoji(strip_actions(part.strip()))
    if "\n" not in part:
        return _drop_markdown_emphasis(part)
    pieces: list[str] = []
    start = 0
    for match in _NEWLINE_RE.finditer(part):
        if not match.group("break"):
            continue
        pieces.append(part[start:match.start()])
        start = match.end()
    pieces.append(part[start:])
    joined = "".join(piece.strip() for piece in pieces if piece.strip())
    return _drop_markdown_emphasis(joined)


def _join_inline(left: str, right: str) -> str:
    """把两条并成一条：前一条已收句就直接接，否则补一个逗号。绝不塞换行。"""
    if not left:
        return right
    if not right:
        return left
    if left[-1] in "。！？!?…~～；;：:，,":
        return left + right
    return left + "，" + right


def _merge_parts(parts: list[str], limit: int) -> list[str]:
    """超过上限的部分并到最后一条：直接相接，必要时补逗号，不用 \\n。"""
    limit = max(1, int(limit))
    if len(parts) <= limit:
        return parts
    merged = ""
    for piece in parts[limit - 1:]:
        merged = _join_inline(merged, piece)
    return parts[:limit - 1] + ([merged] if merged else [])


# ---------------------------------------------------------------- AI 味固定句（A 类）
# 去掉「我认 / 我在XX / 不是…而是…」这类把状态当旁白播报的固定句。
# 只动整句：句子恰好是那句宣稱才删，「我认这个颜色」「我在这里等你」这种正常叙述不碰。
_SELF_NARRATION = (
    "我在这里", "我在這裡",
    "我一直在这", "我一直在這",
    "我稳稳地接住你", "我稳稳接住你", "我穩穩地接住你",
    "我接住你的情绪", "我接住你的情緒",
    "我只是嘴上逞强", "我只是嘴上逞強",
    "我会一直陪着你", "我會一直陪著你",
)
_ACK_DECL = ("我认", "我认了", "我认错")
_TRAIL_TAIL_PUNCT_RE = re.compile(r"^(.*?)([。！？!?…~～；;，,、]+)$")
_TONE_TRIM_RE = re.compile(r"(?P<ch>[啊嘛啦呀哦哟])(?=[，。！？!?…～~\s]|$)")
_TRAIL_TONE_RE = re.compile(r"[呢嘛啦呀啊哦噢诶唔哼哟]+$")
# 这些词里的尾字不是语气词，别删（干嘛 / 哎呀 / 哎啊）
_TONE_PROTECT_PREV = {"嘛": "干做为怎啥", "呀": "哎唉", "啊": "哎唉"}
_TONE_KEEP_TAGS = ("<pass>",)


def _trim_tone_particles(text: str, rate: float) -> str:
    """按比例削句末语气词；确定性哈希，同一条回复重跑结果一致。

    只动「啊嘛啦呀哦哟」的句末/停顿前位置；呢/吧/吗一律不碰；
    「干嘛」「哎呀」这类词保留；整句只剩语气词时不动；
    「你呢？」这种极短问句保留，否则会变成「你？」。
    哈希种子用「去掉所有语气词后的正文 + 这个语气词」，保证多轮 polish 幂等。
    """
    if not text or rate <= 0:
        return text
    if any(tag in text for tag in _TONE_KEEP_TAGS):
        return text
    body_len = len(re.sub(r"[‖\s]", "", text))
    if body_len <= 2:
        return text
    if body_len <= 4 and re.search(r"[？?]$", text.strip()):
        return text
    base = re.sub(r"[啊嘛呢啦呀哦哟]", "", text)

    def repl(match: re.Match) -> str:
        ch = match.group("ch")
        pos = match.start()
        prev = text[pos - 1] if pos > 0 else ""
        if prev and prev in _TONE_PROTECT_PREV.get(ch, ""):
            return ch
        if rate >= 1.0:
            return ""
        if zlib.crc32((base + ch).encode("utf-8")) % 100 < int(rate * 100):
            return ""
        return ch

    return _TONE_TRIM_RE.sub(repl, text)
# 只处理这三种明确形式：不是A，而是B / 不是A，是B / 并不是A，而是B。
# A、B 里不许再出现逗号/顿号，抽不干净就整句不动，宁可不改也不改错意思。
_NOT_BUT_RE = re.compile(
    r"^(?:并不是|不是)(?P<a>[^，,。！？!?；;、]{1,12})[，,](?:而是|是)"
    r"(?P<b>[^，,。！？!?；;、]{1,40})$"
)
# 只有 A 是「状态/感受词」时才敢丢掉「不是A」那半句。
# 为什么不能一律丢：「不是我的错，而是你的错」丢掉 A 就变成认错，
# 「不是不行，而是需要时间」丢掉 A 就变成否定能力——那是改意思，不是清语气。
# 「不是懒，而是累」→「只是累」这种才是同一个意思，A 恰好是个状态词。
_NOT_BUT_STATES = (
    "懒", "累", "生气", "难过", "伤心", "忙", "饿", "渴", "困", "想睡",
    "害羞", "讨厌", "怕", "紧张", "开心", "高兴", "烦", "无聊",
    "孤独", "寂寞", "嫉妒", "崩溃", "委屈", "尴尬", "慌",
)
# 状态词前常见的程度副词：「不是太累，而是……」
_NOT_BUT_DEGREES = ("很", "太", "特别", "真的", "有点", "有些", "挺", "超", "非常", "好")


def _is_state_word(a: str) -> bool:
    """A 是不是纯粹的状态/感受词（前面至多挂一个程度副词，后面至多一个语气词）。"""
    s = _TRAIL_TONE_RE.sub("", a.strip())
    if s.endswith("了"):
        s = s[:-1]
    if s in _NOT_BUT_STATES:
        return True
    return any(s.startswith(d) and s[len(d):] in _NOT_BUT_STATES
               for d in _NOT_BUT_DEGREES)


def _split_tail(s: str) -> tuple[str, str]:
    m = _TRAIL_TAIL_PUNCT_RE.match(s)
    if m:
        return m.group(1), m.group(2)
    return s, ""


def _rewrite_not_but(bare: str) -> str | None:
    m = _NOT_BUT_RE.fullmatch(bare)
    if not m:
        return None
    if not _is_state_word(m.group("a")):
        # A 不是状态/感受词：丢掉它就会改意思（否认、拒绝、归属都被吃掉），整句不动。
        return None
    b = m.group("b").strip()
    if not b:
        return None
    # 保守改法：A 已确认是状态词，丢掉「不是A」这半句，只留 B。
    # 衔接词用「只是」而不是「其实」——「不是懒，而是累」→「只是累」是自然中文，
    # 「其实是累」不是。B 本身已经带转折词时就不再加，免得「只是其实…」。
    if b.startswith(("只是", "就", "其实", "反而", "因为")):
        return b
    return "只是" + b


def _clean_ai_sentence(raw: str) -> str | None:
    """对单句做 A 类清理：返回 None=整句删除；返回字符串=替换后的句子。"""
    s = raw.strip()
    if not s:
        return raw
    body, tail = _split_tail(s)
    bare = body.strip()
    if not bare:
        return raw
    if bare in _ACK_DECL:
        return None
    # 开场「我认，…」：剥掉开场白，留后面真正要说的
    for opener in _ACK_DECL:
        for comma in ("，", ","):
            if bare.startswith(opener + comma):
                rest = bare[len(opener) + len(comma):].lstrip(" \t，,、")
                return (rest + (tail or "")) if rest else None
    # 自我旁白式宣稱（整句恰是那句，或句末只多了个语气词）
    if _TRAIL_TONE_RE.sub("", bare).strip() in _SELF_NARRATION:
        return None
    rewritten = _rewrite_not_but(bare)
    if rewritten is not None:
        return rewritten + (tail or "")
    return raw


def _strip_ai_phrases(part: str) -> str:
    """A 类：删掉自我旁白固定句。删空了就退回原句，别让消息变空。"""
    chunks = _SENTENCE_SPLIT_RE.split(part)
    out: list[str] = []
    skip_next = False
    changed = False
    for index, chunk in enumerate(chunks):
        if skip_next:
            skip_next = False
            continue
        if index % 2 == 0:
            new = _clean_ai_sentence(chunk)
            if new is None:
                skip_next = True
                changed = True
                continue
            if new != chunk:
                changed = True
                out.append(new)
            else:
                out.append(chunk)
        else:
            out.append(chunk)
    if not changed:
        return part
    result = "".join(out).strip()
    return result or part


# ---------------------------------------------------------------- 身体反应频率上限（B 类）
_BODY_WORDS = (
    "脸红", "脸热", "脸颊发烫", "脸都红", "脸红红",
    "耳朵", "耳根", "耳尖", "耳垂",
    "尾巴", "尾鳍", "尾巴尖",
    "心跳", "心口",
    "眼睛亮", "眼睛一亮", "眼睛都亮",
    "眼神躲", "眼神飘", "眼神闪", "眼神软", "眼神亮",
    "呼吸乱", "呼吸急", "呼吸重", "呼吸一滞",
    "喉头", "喉结", "喉咙发紧", "喉咙发干",
    "下意识", "不自觉",
)
_OTHER_PERSON = ("你", "他", "她", "它", "他们", "她们", "它们", "你们", "妳")
_SELF_PRONOUN = ("我", "人家", "咱")
# 在描述图片/表情包而不是说自己身体时，不当成自身反应
_BODY_OBJECTS = ("表情包", "贴图", "贴纸", "图片", "照片", "头像", "壁纸", "截图", "梗图")


def _is_body_reaction(sentence: str) -> bool:
    s = sentence.strip()
    if not s:
        return False
    if any(q in s for q in ("「", "“", '"')):
        return False
    if any(k in s for k in _BODY_OBJECTS):
        return False
    if not any(k in s for k in _BODY_WORDS):
        return False
    # 有别的对象代词、又没有自称，判定为在说别人，不算「本人反应」
    if any(p in s for p in _OTHER_PERSON) and not any(p in s for p in _SELF_PRONOUN):
        return False
    return True


def _count_body_reaction(part: str) -> int:
    chunks = _SENTENCE_SPLIT_RE.split(part)
    return len([i for i in range(0, len(chunks), 2)
                if chunks[i].strip() and _is_body_reaction(chunks[i])])


def _has_non_reaction_content(part: str) -> bool:
    """这一条里除了自身反应句，还有没有别的内容。"""
    chunks = _SENTENCE_SPLIT_RE.split(part)
    return any(chunks[i].strip() and not _is_body_reaction(chunks[i])
               for i in range(0, len(chunks), 2))


def _cap_body_reaction_reply(parts: list[str], max_n: int) -> list[str]:
    """**整条回覆**（含所有 ‖ 分条）合计最多留 max_n 句自身反应。

    用户的话是「不要每次说完话都说一下自己的脸/尾巴」——那是按一整次回覆算的，
    不是按单条消息算。按单条算的话，一条回覆拆成三条就有三句，等于没管住。
    所以先按「整条回覆」的总反应句数分配额度，再逐条处理：额度还有就留，
    额度用完时——这条只剩反应句就整条不发，这条还有正文就只削掉反应句。
    不拆条、拆条两条路径现在走同一套账，结果一致。
    """
    if max_n < 0:
        return parts
    used = 0
    out: list[str] = []
    for p in parts:
        count = _count_body_reaction(p)
        if count == 0:
            out.append(p)
            continue
        remaining = max(0, max_n - used)
        if remaining <= 0:
            if not _has_non_reaction_content(p):
                # 整条都是反应句、额度已用完 → 这一条没有别的内容，直接不发。
                # （不能走 _cap_body_reaction(p, 0) 的 trim 分支：它为了不掏空
                #   单条会硬留一句，跨条累计就又超额度了。）
                continue
            out.append(_cap_body_reaction(p, 0))
            continue
        if count <= remaining:
            out.append(p)
            used += count
            continue
        capped = _cap_body_reaction(p, remaining)
        out.append(capped)
        used += _count_body_reaction(capped)
    if out:
        return out
    # 整次回覆全是反应句：至少留第一句，别把回复削空（和单条时的保底一致）。
    first = _cap_body_reaction(parts[0], 0) if parts else ""
    return [first] if first else parts[:1]


# ---------------------------------------------------------------- 唔/嗯 当固定开场
# 实测 133 条真实回覆里，「唔…」开头 75 条 = 56%——这不是口癖，这是模板。
# 做法：只在整条回覆**以语气词+省略号开头**时处理，且按内容哈希抽掉一半
# （同一句话结果固定，幂等；另一半保留软糯感，不至于全变硬）。
_FILLER_OPENER_RE = re.compile(r"^\s*(?:唔|嗯|诶|欸|啊|噢|哦)[ \t]*[…\.．。]{1,3}[ \t]*")
_FILLER_KEEP_MOD = 2      # 哈希 % 2 == 0 → 削掉开头


def _strip_filler_opener(text: str) -> str:
    m = _FILLER_OPENER_RE.match(text or "")
    if not m:
        return text
    rest = text[m.end():].lstrip()
    if len(rest) < 6:          # 削完就没什么了，别削
        return text
    if zlib.crc32(rest.encode("utf-8")) % _FILLER_KEEP_MOD != 0:
        return text
    return rest


def _cap_body_reaction(part: str, max_n: int) -> str:
    """单条消息里自身身体/表情反应句最多留 max_n 句；句尾装饰短句优先删。"""
    chunks = _SENTENCE_SPLIT_RE.split(part)
    reaction_idx = [i for i in range(0, len(chunks), 2)
                    if chunks[i].strip() and _is_body_reaction(chunks[i])]
    if not reaction_idx:
        return part
    has_other_content = any(
        chunks[i].strip() and not _is_body_reaction(chunks[i])
        for i in range(0, len(chunks), 2))
    if max_n <= 0:
        keep = 1 if not has_other_content else 0
    else:
        keep = max_n
    if len(reaction_idx) <= keep:
        return part
    excess = len(reaction_idx) - keep
    # 句尾优先：删除靠后的那些反应句（连同它们后面的标点一起）
    skip = set()
    for idx in reaction_idx[-excess:]:
        skip.add(idx)
        skip.add(idx + 1)
    out = []
    for i, c in enumerate(chunks):
        if i in skip:
            continue
        out.append(c)
    result = "".join(out).strip()
    return result or part


def polish(text: str, max_parts: int | None = None) -> str:
    """Tidy common voice artifacts without interpreting the reply's meaning.

    max_parts 不给时读配置 reply.max_parts（默认 3）。超出的部分并到最后一条，
    绝不为了凑条数把几条消息用换行重新粘成一条。
    """
    if not text or "<pass>" in text:
        return text

    limit = _configured_max_parts() if max_parts is None else max(1, int(max_parts))
    if _is_roleplay(text):
        # 语C：不拆换行、不去引号、不删结巴、不剪追问，整段留在一条里
        parts = [piece for piece in (_polish_roleplay_part(raw)
                                     for raw in _split_parts(text)) if piece]
        parts = _merge_parts(parts, limit)
        return _non_empty("‖".join(parts), text)

    parts: list[str] = []
    for raw in _split_parts(text):
        # 先整段清理（引号/开场白要对整段判断），再按换行分条
        for piece in _split_newlines(_polish_part(raw)):
            if piece:
                parts.append(piece)
    if _configured_strip_ai():
        parts = [_strip_ai_phrases(p) for p in parts]
    parts = _cap_body_reaction_reply(parts, _configured_body_reaction_max())
    if _configured_strip_filler_opener() and parts:
        # 只整条回覆的第一条，削不空（_strip_filler_opener 自己有长度保护）
        parts[0] = _strip_filler_opener(parts[0])
    parts = [p for p in parts if p]
    parts = _drop_tag_question(parts)
    # 语气词削减放在「呢/吧 问句」处理之后：先判断这是不是「你呢？」这种标签问句，
    # 否则把「呢」削掉会让问句识别失效，反而多留一句。
    tone_trim = _configured_tone_trim()
    if tone_trim > 0:
        parts = [_trim_tone_particles(p, tone_trim) for p in parts]
    parts = _merge_parts(parts, limit)
    return _non_empty("‖".join(part for part in parts if part), text)


def _non_empty(cleaned: str, original: str) -> str:
    """最後一道保護：清理完不能是空字串，否則被 @ 時 bot 會一聲不吭。

    `plugins/ai_chat.py` 是先 polish 再看要不要保底；拿到空字串就直接靜默 return。
    所以這裡退回原始輸入（strip 過的）——宁可按原文发一句，也别装死。
    只有「原文本来就全是 ‖ / 空白」这种没有内容的输入才允许回空串，
    免得把分隔符本身当一句话发出去。
    """
    if cleaned:
        return cleaned
    source = str(original or "").strip()
    if source.strip("‖ \t\r\n"):
        return source
    return ""
