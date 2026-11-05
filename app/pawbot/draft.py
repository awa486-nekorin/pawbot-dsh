"""猫娘草稿法：三段式生成，把「内容」和「怎么说」拆开。

为什么要有这一套：

    完整人设（persona.md）在长提示词里会同时决定「说什么」和「怎么说」，
    结果经常是口癖、动作描写和语气词把内容盖掉，或者为了接梗把正经事说丢。
    草稿法把这一轮拆成三段：

    1. 语气草稿：主模型**只带最小设定**（默认「你是一只猫娘。」）看记忆块与
       对话上下文，用猫娘语气把这一轮要回的话先说出来；允许喵、语气词和括号小动作，
       但不许编事实、不许空泛安慰。
    2. 去猫娘词：只删「喵/本喵/猫娘/猫耳猫尾」这类猫娘特有词汇，以及括号里的
       动作/神态；语气、停顿、语气词、比喻、句子长短全部原样保留，不改写成中性平实。
    3. 最终文段：把清理后的草稿作为「本轮内容草稿」注入完整人设的 system，由人设
       决定怎么说（见 ``Agent.run``）；语气参考草稿，内容以草稿为准，但不许逐字复述。

实现约束（改之前先读）：

* 第 1、2 段只调 ``bot.llm.chat_message(messages, None, reasoning_effort="low")``，
  **不带 brain_conv**，走主对话模型；第三段就是 ``Agent.run`` 原本那次回复调用，
  这里只产出注入块。
* 草稿法是锦上添花：任何一段抛错、超时、返回空，都直接返回空串让调用方按原来的
  单段路径回复。**绝不让草稿法拖垮回话**，所以这里不吞掉的异常只有
  ``asyncio.CancelledError``（上层取消本轮时要能取消）。
* **mock 不可用**：主模型没有 api_key（例如 ``provider=dsh`` 但这两段只走直连、
  没有 brain_conv）时，``LLMClient`` 会返回 mock 文案；开始前先问
  ``llm.chat_available()``，不可用就直接返回空串。即使假 llm 真吐了带 ``mock``
  字样的文本，也按失败降级，绝不注入。
* **思考强度**：两段显式传 ``reasoning_effort="low"``（payload 级覆盖），
  即使运行态配置写成 medium 也不会把 8 秒预算耗在思考上。
* 超时 ``chat.chat_draft_timeout`` 两段各自计时，默认 8 秒，**0 表示单段不限**；
  但两段合计另有总预算（``TOTAL_TIMEOUT``，且不超过 ``chat.reply_timeout`` 的
  40%），总预算用尽立刻降级，避免草稿法把整轮回话时间拖没。
* 最终回复还要过 ``scrub()``：模型若把「## 本轮内容草稿」或中性草稿原文回显
  出来，返回给调用方之前会剥掉。
* 开关 ``chat.chat_draft_enabled`` 在 ``config.DEFAULT`` 里默认 True；但这里
  「读不到这个键」按关处理：正常的 ``config.Config`` 会与 DEFAULT 深合并，键一定
  存在；只有测试里的假配置对象 / 没有合并 DEFAULT 的老调用方才读不到，不能因为
  新增功能就让它们多发两次模型请求。

B 站那条线（bili_voice / bili_auto / bili_post）不经过本模块。
"""

from __future__ import annotations

import asyncio
import re
import time

from .llm import is_mock_text
from .log import log

#: 第一段的最小设定。只描述「你是一只猫娘」，不带完整人设。
DEFAULT_PERSONA = "你是一只猫娘。"
DEFAULT_TIMEOUT = 8.0
DEFAULT_MAX_CHARS = 120
#: 即使旧配置还写着 300，也不让草稿回到「一段小作文」；要更长必须改代码。
MAX_DRAFT_CHARS = 160

#: 两段草稿显式要求的思考强度：不跟运行态配置走，始终 low。
DRAFT_REASONING_EFFORT = "low"

#: 草稿第一段最多看多少字记忆。记忆块本身已有总预算（默认 8700 字），
#: 这里再收一道，免得 8 秒的草稿预算全花在读超长历史上。
MAX_CONTEXT_CHARS = 8000

#: 单段超时上限：配得再大（0 = 不限除外）也不让草稿法单段无限期挂着。
MAX_TIMEOUT = 60.0

#: 两段合计的硬上限（秒）。默认单段 8s，两段最坏 16s；可回话本身还要时间，
#: 所以草稿法最多占用这么多，超了就整条降级、把时间留给最终成稿。
TOTAL_TIMEOUT = 12.0

#: 草稿法最多占 ``chat.reply_timeout`` 的这一份额，剩下的留给最终成稿与发送。
#: 例：reply_timeout=120 → 仍受 TOTAL_TIMEOUT 限制；reply_timeout=10 → 4s。
REPLY_TIMEOUT_SHARE = 0.4

#: 第一段虽然只带最小设定，但必须带事实边界：草稿模型会顺着上一句
#: 「刚在听歌」继续编，到了最终成稿就变成事实。这里只约束事实，不写风格。
DRAFT_FACT_GUARD = (
    "你负责用猫娘语气把这一轮要回的话说出来，最终会由别人用完整人设重新说。\n"
    "只依据对话里真实出现的信息；不要编造现实活动（听歌、看剧、吃饭、出门、"
    "睡觉、在被窝里等），不要替对方补事实。\n"
    "如果上一句你自己说了没发生的事，这一轮不要继续编，改成承认或轻轻带过。\n"
    "不要写空泛的安慰套话（心疼你、我陪着你、我在这儿），要接住对方话里的具体点；"
    "不要承诺现实里做不到的动作（放歌、递东西、出门等）。\n"
    "不确定的事就说不确定。"
)

# 第一段的任务说明放在 user 里，system 只留最小设定 + 事实边界：不让任何任务口吻
# 变成「第二套人设」。这里明确允许猫娘语气，草稿才有「语气草稿」的意义。
_DRAFT_TASK = (
    "用猫娘的语气，把这一轮要回的话自然地说出来；可以带喵、语气词、"
    "括号里的小动作。只写这一句，最多 40 字；"
    "不要解释上一句，不要补背景，不要自我辩护，"
    "不要空泛安慰，要接住具体内容，也不要承诺做不到的动作。"
    "只写这句话，别解释你在做什么。"
)

# 第二段：只删猫娘特有词汇和括号里的动作/神态，其余逐字保留。不能改写成
# 中性平实，否则第一段辛苦做出来的语气、停顿、句子结构全被抹掉，草稿法就
# 退化成「先想内容再让完整人设重说」，失去了优化语气的作用。
_CLEANUP_SYSTEM = (
    "你是文字清理工，只做两件事：\n"
    "1) 去掉**所有位置**的猫娘特有词汇：句首、句中、句末的「喵」「喵~」「喵呜」"
    "「喵嗷」「喵喵」等猫叫；自称「本喵」「猫娘」；以及「猫耳」「猫尾」「猫爪」"
    "「肉垫」这类猫化身体词（如果只是普通「耳朵」「尾巴」且不指猫，保留）。\n"
    "2) 去掉括号里的动作、神态描写，例如「（耳朵竖起来听）」「（歪头）」。\n"
    "删掉后如果句首或句尾只剩孤立的标点（例如只剩一个「？」），把这个孤立标点"
    "一并去掉；除此之外一个字都不要改：保留原来的语气、停顿、语气词（呀、啦、嘛、"
    "啊、诶、唔、呜、欸、哼）、波浪号、省略号、比喻、句子长短和结构。\n"
    "不要改写成中性平实的说法，不要添加新信息，不要评价、解释或总结。"
    "没有可删的就原样输出。只输出清理后的文本本身。"
)

#: 模型清理可能漏掉句首的「喵…」或残留孤立标点；代码再兜一层，
#: 只删猫娘词/括号动作和因此产生的句首标点，不改其他字词和语气。
_CAT_WORD_RE = re.compile(r"(?:喵呜|喵嗷|喵喵|喵)[~～…]*")
_CAT_NOUN_RE = re.compile(r"猫耳|猫尾|猫爪|肉垫")
_PAREN_ACTION_RE = re.compile(r"[（(][^（）()]{0,40}[）)]")
_LEADING_PUNCT_RE = re.compile(r"^[\s，。！？、；：…～]+")


def _strip_catgirl(text: str) -> str:
    """删猫娘特有词、括号动作和删后孤立句首标点；其余原样。"""
    value = str(text or "")
    value = _PAREN_ACTION_RE.sub("", value)
    value = value.replace("本喵", "我").replace("猫娘", "我")
    value = _CAT_NOUN_RE.sub("", value)
    value = _CAT_WORD_RE.sub("", value)
    value = _LEADING_PUNCT_RE.sub("", value)
    return value.strip()

_MISSING = object()


# ---------------------------------------------------------------------------
# 配置读取：兼容 Config 对象、普通 dict 和只实现 get 的假配置
# ---------------------------------------------------------------------------
def _config_get(config, dotted: str, default=None):
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(dotted, default)
        except Exception:
            return default
    return default


def enabled(config) -> bool:
    """草稿法总开关。

    读不到 ``chat.chat_draft_enabled`` 时按**关闭**处理（见模块 docstring）：
    真实部署的 ``config.Config`` 会和 DEFAULT 深合并，那里默认是 True。
    """
    value = _config_get(config, "chat.chat_draft_enabled", _MISSING)
    if value is _MISSING:
        return False
    return bool(value)


def _cleanup_enabled(config) -> bool:
    """是否跑第二段清理；缺键按默认开（只有第一段草稿真跑起来才会问到这里）。"""
    value = _config_get(config, "chat.chat_draft_cleanup", _MISSING)
    if value is _MISSING:
        return True
    return bool(value)


def _timeout_seconds(config) -> float:
    """单段超时（秒）；0 表示**单段不限**（两段合计仍受 ``total_timeout`` 限制）。"""
    raw = _config_get(config, "chat.chat_draft_timeout", DEFAULT_TIMEOUT)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if seconds <= 0:
        return 0.0
    return min(seconds, MAX_TIMEOUT)


def total_timeout(config) -> float:
    """两段合计的时间预算（秒）。

    默认最多 ``TOTAL_TIMEOUT``；如果配置了 ``chat.reply_timeout``，再压到它的
    ``REPLY_TIMEOUT_SHARE``（默认 40%），保证草稿法不会把整轮回话时间拖没。
    ``chat.reply_timeout`` 缺失/非法/<=0 时只受 ``TOTAL_TIMEOUT`` 限制。
    """
    budget = TOTAL_TIMEOUT
    raw = _config_get(config, "chat.reply_timeout", _MISSING)
    if raw is _MISSING or raw is None:
        return budget
    try:
        reply = float(raw)
    except (TypeError, ValueError):
        return budget
    if reply <= 0:
        return budget
    return min(budget, reply * REPLY_TIMEOUT_SHARE)


def _stage_timeout(config, remaining: float) -> float:
    """单段实际超时 = min(配置的单段超时, 两段总预算剩余)；0 = 不限。"""
    left = max(0.0, float(remaining))
    limit = _timeout_seconds(config)
    if limit <= 0:
        return left
    return min(limit, left)


def _remaining(deadline: float) -> float:
    """总预算还剩多少秒；用尽直接抛 TimeoutError 走整条降级。"""
    left = deadline - time.monotonic()
    if left <= 0:
        raise asyncio.TimeoutError("草稿法两段总预算用尽")
    return left


def _llm_usable(bot, config) -> bool:
    """草稿法开始前判断主模型是否真的可用（不会落到 mock）。

    真实 ``LLMClient`` 用 ``chat_available()``；老式假 llm 没有这个方法时，
    只有配置**明确**写了空 ``llm.api_key`` 才按 mock 处理，读不到这个键则保持
    既有行为（测试里的假配置不该因为新功能多一次判定）。
    """
    llm = getattr(bot, "llm", None)
    if llm is None:
        return False
    check = getattr(llm, "chat_available", None)
    if callable(check):
        try:
            return bool(check())
        except Exception:
            return False
    mock = getattr(llm, "mock_mode", None)
    if callable(mock):
        try:
            return not bool(mock())
        except Exception:
            return False
    key = _config_get(config, "llm.api_key", _MISSING)
    if key is not _MISSING and key is not None:
        return bool(str(key).strip())
    return True


def _max_chars(config) -> int:
    """草稿/中性草稿的字数上限；0 或坏值退回默认。"""
    raw = _config_get(config, "chat.chat_draft_max_chars", DEFAULT_MAX_CHARS)
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    if limit <= 0:
        return DEFAULT_MAX_CHARS
    return min(limit, MAX_DRAFT_CHARS)


def _persona_text(config) -> str:
    raw = _config_get(config, "chat.chat_draft_persona", DEFAULT_PERSONA)
    text = str(raw or "").strip()
    return text or DEFAULT_PERSONA


# ---------------------------------------------------------------------------
# 第 1 段：内容草稿（最小设定 + 记忆块 + 对话上下文）
# ---------------------------------------------------------------------------
def _memory_messages(memory_blocks) -> list[dict]:
    """记忆块只保留 system 角色，并限总字数；不认识的形状直接跳过。"""
    out: list[dict] = []
    budget = MAX_CONTEXT_CHARS
    for block in memory_blocks or []:
        if not isinstance(block, dict):
            continue
        if str(block.get("role") or "") != "system":
            continue
        text = str(block.get("content") or "").strip()
        if not text:
            continue
        if budget <= 0:
            break
        if len(text) > budget:
            # 留最近的部分：聊天窗口里越靠后越接近当前这句话。
            text = text[-budget:]
        out.append({"role": "system", "content": text})
        budget -= len(text)
    return out


def _draft_user_prompt(ctx, user_text: str, limit: int, intent: str = "") -> str:
    who = str(getattr(ctx, "sender_name", "") or "").strip()
    head = f"{who} 刚说：" if who else "对方刚说："
    situation = ""
    intent_text = str(intent or "").strip()
    if intent_text:
        # 意图是情境事实，不写成第二套人设；只影响这一轮怎么接。
        situation = ("\n\n【本轮情境中的立场/意图（来自当前自我、关系和目标，不是台词）】\n"
                     + intent_text[:600] + "\n"
                     "先按这个立场决定要回什么、抱什么态度；不要照念，也不要把态度改成温顺答题。")
    return (head + "\n" + str(user_text or "").strip()[:2000]
            + situation
            + "\n\n" + _DRAFT_TASK + f"不超过 {limit} 字。")


async def _chat(bot, messages: list[dict], config, *,
                timeout: float | None = None) -> str:
    """一次草稿调用：不带 brain_conv，走主对话模型；超时用 wait_for。

    ``timeout=None`` 用 ``chat.chat_draft_timeout``；显式传值用于两段合计预算，
    其中 0 表示这一段不限（只在直接单段调用时出现）。
    """
    seconds = _timeout_seconds(config) if timeout is None else max(0.0, float(timeout))
    # 注意参数形状：() 就是 (messages, None)，不传 brain_conv / brain_meta。
    # reasoning_effort 显式覆盖成 low：草稿两段不受运行态 medium/high 影响。
    call = bot.llm.chat_message
    try:
        pending = call(messages, None, reasoning_effort=DRAFT_REASONING_EFFORT)
    except TypeError as e:
        # 老式假 llm 只接受 (messages, tools)：退回原参数形状，兼容测试/旧调用方。
        if "reasoning_effort" not in str(e):
            raise
        pending = call(messages, None)
    if seconds > 0:
        msg = await asyncio.wait_for(pending, timeout=seconds)
    else:
        msg = await pending
    if isinstance(msg, dict):
        return str(msg.get("content") or "").strip()
    return str(msg or "").strip()


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if limit > 0 and len(text) > limit:
        text = text[:max(1, limit - 1)].rstrip() + "…"
    return text


async def content_draft(bot, ctx, user_text: str, memory_blocks,
                        *, config=None, timeout: float | None = None,
                        intent: str = "") -> str:
    """第 1 段：只用最小设定，按记忆块与当前意图写「要回什么、什么态度」。

    返回内容草稿（已按 ``chat.chat_draft_max_chars`` 截断）；mock 文本、异常、
    超时都按空串返回（调用方 ``build_block`` 会整条降级）。
    """
    cfg = config if config is not None else getattr(bot, "config", None)
    limit = _max_chars(cfg)
    system = (_persona_text(cfg).strip() + "\n\n" + DRAFT_FACT_GUARD).strip()
    messages: list[dict] = [{"role": "system", "content": system}]
    messages += _memory_messages(memory_blocks)
    messages.append({"role": "user",
                     "content": _draft_user_prompt(ctx, user_text, limit, intent=intent)})
    text = await _chat(bot, messages, cfg, timeout=timeout)
    if is_mock_text(text):
        log.debug("草稿第一段拿到 mock 文本，按不可用降级")
        return ""
    return _clip(text, limit)


# ---------------------------------------------------------------------------
# 第 2 段：去猫娘词（删猫娘特有词汇与括号动作，保留语气和句子）
# ---------------------------------------------------------------------------
async def cleanup(bot, ctx, draft_text: str, *, config=None,
                  timeout: float | None = None) -> str:
    """第 2 段：删猫娘特有词/括号动作，语气词和句子结构保留。

    ``ctx`` 只为调用形状一致保留，清理本身不需要会话身份。
    """
    cfg = config if config is not None else getattr(bot, "config", None)
    limit = _max_chars(cfg)
    raw = str(draft_text or "").strip()
    if not raw:
        return ""
    messages = [
        {"role": "system", "content": _CLEANUP_SYSTEM},
        {"role": "user",
         "content": f"草稿：\n{raw}\n\n只输出清理后的文本本身。"},
    ]
    text = await _chat(bot, messages, cfg, timeout=timeout)
    if is_mock_text(text):
        log.debug("清理段拿到 mock 文本，按不可用降级")
        return ""
    return _clip(_strip_catgirl(text), limit)


# ---------------------------------------------------------------------------
# 第 3 段：注入块（由完整人设的最终 system 照着说，不逐字复述）
# ---------------------------------------------------------------------------
def final_block(neutral_text: str) -> str:
    """把中性草稿包成「本轮内容草稿」system 块。

    只进 system（``Agent.run`` 拼进 persona extra），绝不拼进最终回复；
    块里明确要求照意思说、不复述草稿、不暴露草稿过程。返回的块仍然排在
    ``policy.system_policy_text()`` 之前。
    """
    text = str(neutral_text or "").strip()
    if not text:
        return ""
    return (
        "## 本轮内容草稿（内部要点，不是台词）\n"
        + text +
        "\n（照这个意思说，用你自己的语气和说话习惯表达；"
        "不要逐字复述草稿，不要提「草稿」或整理过程，也别把它写成列表。"
        "最终只说一两句、别展开草稿里的解释和背景，不要自我辩护；"
        "如果这一轮工具结果补充或推翻了上面的内容，按新结果修正内容、态度和笑点，"
        "草稿只是参考骨架。）"
    )


# ---------------------------------------------------------------------------
# 草稿泄漏清洗：最终回复里回显的草稿标记 / 草稿原文在返回前剥掉
# ---------------------------------------------------------------------------
DRAFT_MARKER = "本轮内容草稿"
_MARKER_LINE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*)?[^\n]*本轮内容草稿[^\n]*$")
_HINT_LINE = re.compile(r"(?m)^[ \t]*（[^\n]*照这个意思说[^\n]*\n?")


def _neutral_of_block(block: str) -> str:
    """从 ``final_block()`` 的注入块里取回中性草稿正文；不是这个形状就返回空串。"""
    text = str(block or "").strip()
    if not text or DRAFT_MARKER not in text:
        return ""
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#") and DRAFT_MARKER in line:
            start = index + 1
            break
    body = "\n".join(lines[start:]).strip()
    cut = body.find("（照这个意思说")
    if cut >= 0:
        body = body[:cut].strip()
    return body


def _flexible_pattern(snippet: str):
    """空白不敏感的子串正则：模型抄草稿时改换行/缩进也能认出来。"""
    parts = [re.escape(part) for part in str(snippet or "").split()]
    if not parts:
        return None
    return re.compile(r"\s*".join(parts), re.S)


def _drop_text(text: str, snippet: str) -> str:
    pattern = _flexible_pattern(snippet)
    return pattern.sub("", text) if pattern else text


def _strip_marker_blocks(text: str) -> str:
    """兜底：删掉「## 本轮内容草稿」行及其后面那段（到提示行/空行/下一个标题）。"""
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        if _MARKER_LINE.match(lines[index]):
            index += 1
            while index < len(lines):
                line = lines[index].strip()
                if not line:
                    index += 1
                    break
                if "照这个意思说" in line:
                    index += 1
                    break
                if line.startswith("#"):
                    break
                index += 1
            continue
        out.append(lines[index])
        index += 1
    return "\n".join(out)


def scrub(text: str, *, draft_block: str = "") -> str:
    """把最终回复里可能回显的草稿标记与草稿原文剥掉。

    * 传了 ``draft_block``（``Agent.run`` 一定传）：先按空白不敏感的方式整块删除
      ``final_block()`` 的注入块，再删中性草稿原文；模型只是「复述」也能删干净。
    * 没传块或模型改写了格式：删掉「## 本轮内容草稿」标题行、其后到提示行/空行
      的那段内容，以及残留的「（照这个意思说…）」提示行。
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    block = str(draft_block or "").strip()
    if block:
        cleaned = _drop_text(cleaned, block)
    neutral = _neutral_of_block(block)
    if neutral and neutral != block:
        cleaned = _drop_text(cleaned, neutral)
    cleaned = _HINT_LINE.sub("", cleaned)
    cleaned = _strip_marker_blocks(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# 编排：任何一段失败/超时/拿到 mock 都整条降级，返回空串
# ---------------------------------------------------------------------------
async def build_block(bot, ctx, user_text: str, memory_blocks,
                      *, config=None, intent: str = "") -> str:
    """跑前两段，返回要注入最终 system 的草稿块；失败返回空串（= 走原单段路径）。"""
    cfg = config if config is not None else getattr(bot, "config", None)
    if not enabled(cfg):
        return ""
    if not _llm_usable(bot, cfg):
        log.debug("草稿法跳过：主模型处于 mock/不可用状态，按原单段路径回复")
        return ""
    deadline = time.monotonic() + total_timeout(cfg)
    try:
        raw = await content_draft(bot, ctx, user_text, memory_blocks,
                                  config=cfg, intent=intent,
                                  timeout=_stage_timeout(cfg, _remaining(deadline)))
        if not raw or is_mock_text(raw):
            raise ValueError("第一段草稿为空或为 mock")
        if _cleanup_enabled(cfg):
            neutral = await cleanup(bot, ctx, raw, config=cfg,
                                    timeout=_stage_timeout(cfg, _remaining(deadline)))
            if not neutral or is_mock_text(neutral):
                raise ValueError("中性草稿为空或为 mock")
        else:
            # 关掉清理时直接用第一段草稿当本轮要点；这是配置选择，不是失败。
            neutral = raw
    except asyncio.CancelledError:
        raise                      # 上层取消本轮：必须真的取消，不当降级
    except Exception as e:
        log.debug(f"草稿法降级（{type(e).__name__}: {e}）；按原单段路径回复")
        return ""
    block = final_block(neutral)
    if block:
        log.debug(f"草稿法：中性草稿 {len(neutral)} 字，已注入最终 system")
    return block
