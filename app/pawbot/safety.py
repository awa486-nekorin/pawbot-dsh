"""QQ 发送层：按 ‖ 分条，控制每会话发送额度、静默时段与可选沉默。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque
from weakref import WeakValueDictionary

from . import media
from . import message as msg
from .log import log

# 模型留的「另起一条消息」标记：‖。发送前按它切分，符号本身不发出去
SPLIT_MARK_RE = re.compile(r"‖+")

# 溢出合并时判断前一段是否已经收句：收句就直接接，没收句补一个逗号（绝不换行）
_SENTENCE_END = "。！？!?…~～；;：:，,"


def _overflow_join(segments: list[dict]) -> str:
    """把下一段并进本条时用什么相连。"""
    if not segments:
        return ""
    last = segments[-1]
    if last.get("type") != "text":
        return ""
    text = str((last.get("data") or {}).get("text", "")).rstrip()
    return "" if text.endswith(tuple(_SENTENCE_END)) else "，"

# ---------------------------------------------------------------- 手滑
# 小表：正确写法 → 手滑写法。只收常见的两个字的混淆（的/得、在/再、那/哪、
# 已/以、做/作这类），改完句子仍然读得通；每条消息最多动一处。
_TYPO_TABLE = (
    ("已经", "以经"),   # 已 → 以
    ("觉得", "觉的"),   # 得 → 的
    ("真的", "真得"),   # 的 → 得
    ("当作", "当做"),   # 作 → 做
    ("再说", "在说"),   # 再 → 在
)

# 这些区域里一个字都不许动：网址、@、引号/书名号里的引用。
# （表里全是汉字词，本来就落不进数字和英文串里）
_TYPO_MASK_RE = re.compile(
    r"https?://\S+"
    r"|@\S+"
    r"|「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’"
    r"|\"[^\"]*\"|'[^']*'"
    r"|```[\s\S]*?```|`[^`]*`"
)


def typo_plan(text: str, blocked: frozenset[str] | set[str] = frozenset()):
    """挑一处可以安全手滑的地方，返回 (错句, 正确词) 或 None。

    只处理 ≥ 8 字的纯文字；引号里、@、网址里的内容不碰；blocked 里的词
    （人设高频词/昵称）也不碰。
    """
    if len(text.strip()) < 8:
        return None
    masked = list(text)
    for m in _TYPO_MASK_RE.finditer(text):
        for i in range(m.start(), m.end()):
            masked[i] = "\x00"
    masked = "".join(masked)

    hits: list[tuple[int, str, str]] = []
    for right, wrong in _TYPO_TABLE:
        if right in blocked:
            continue
        start = masked.find(right)
        while start >= 0:
            hits.append((start, right, wrong))
            start = masked.find(right, start + len(right))
    if not hits:
        return None
    pos, right, wrong = random.choice(sorted(hits))
    return text[:pos] + wrong + text[pos + len(right):], right


def _typo_fix_text(right: str) -> str:
    """更正那一句。短、口语、不带解释性括号，像人顺手补的一句。"""
    return f"打错了，是「{right}」"




class SafetyGuard:
    def __init__(self, bot):
        self.bot = bot
        self._bucket: dict[str, deque] = {}
        self._locks = WeakValueDictionary()

    # ---------------------------------------------------------- 配置快捷读取
    def _c(self, key, default=None):
        return self.bot.config.get(f"safety.{key}", default)

    @property
    def enabled(self) -> bool:
        return bool(self._c("enabled", True))

    @property
    def typing_indicator(self) -> bool:
        return bool(self._c("typing_indicator", True))

    async def _typing(self, conn, group_id, user_id) -> None:
        """发一次「正在输入」。假 conn / 老协议端没有这个方法就安静跳过。"""
        fn = getattr(conn, "typing", None)
        if fn is None:
            return
        try:
            await fn(group_id=group_id, user_id=user_id)
        except Exception as exc:
            log.debug(f"[safety] typing 失败（忽略）：{type(exc).__name__} {exc}")

    def _typo_blocked(self) -> set[str]:
        """人设里的高频词不要拿来手滑；至少把昵称挡掉。"""
        try:
            name = str(self.bot.config.get("bot.nickname", "") or "").strip()
        except Exception:
            name = ""
        return {name} if name else set()

    def _plan_typo(self, parts):
        """这条要不要手滑一次；返回 (段下标, 错句, 正确词) 或 None。"""
        if len(parts) != 1:
            return None
        text_idx = None
        for j, seg in enumerate(parts[0]):
            kind = seg.get("type")
            if kind == "text":
                if text_idx is not None:     # 多段文字 = 不纯，别碰
                    return None
                text_idx = j
            elif kind not in ("reply", "at"):
                return None
        if text_idx is None:
            return None
        chance = float(self._c("typo_chance", 0.03))
        # 被直接 @ / 引用回复时减半：重要对话里少出岔子
        addressed = any(s.get("type") in ("reply", "at") for s in parts[0])
        if addressed:
            chance *= 0.5
        if chance <= 0 or random.random() >= chance:
            return None
        seg = parts[0][text_idx]
        data = seg.get("data")
        if not isinstance(data, dict):
            return None
        plan = typo_plan(str(data.get("text", "")), self._typo_blocked())
        if plan is None:
            return None
        wrong_text, right = plan
        return text_idx, wrong_text, right

    async def _send_typo_fix(self, conn, right, group_id, user_id) -> None:
        """补一句更正。直送，不走分段/限流；失败就当没这回事。"""
        try:
            lo, hi = self._c("typo_fix_delay", [1.2, 2.4])
            await asyncio.sleep(random.uniform(float(lo), float(hi)))
            await self._raw(conn, [msg.Text(_typo_fix_text(right))], group_id, user_id)
        except Exception as exc:
            log.debug(f"[safety] 更正没发出去（忽略）：{type(exc).__name__} {exc}")

    # ---------------------------------------------------------- 主入口
    async def send(self, conn, session_key: str, segments: list[dict], *,
                   group_id: int | None = None, user_id: int | None = None,
                   elapsed: float = 0.0) -> dict | None:
        """替 Context 发消息。策略性静默返回 None，其余返回第一段回执。"""
        parts = self._split(segments)
        if not parts:
            return None
        if not self.enabled:
            return await self._send_parts(conn, parts, session_key, group_id, user_id, elapsed)

        # 发送节制只剩一层硬闸：每会话滑动窗口（safety.per_minute / per_hour）。
        # 一次回复不管拆成几条 QQ 消息都只算 1 个 token（媒体在 _deliver 里各算 1），
        # 所以不会被 ‖ 分条数卡死。被 @ / 私聊找上门的回复同样过这道闸——
        # 防小号被风控优先于有问必答。超限直接不发、返回 None。
        # 静默时段（_in_quiet_hours）仍留给主动引擎判断，这里不拦。
        if not self._take_token(session_key, 1):
            log.info(f"[safety] 限速丢弃 {session_key}")
            return None
        return await self._send_parts(conn, parts, session_key, group_id, user_id, elapsed)

    @staticmethod
    def _is_media(part):
        return len(part) == 1 and part[0].get("type") in ("image", "_media")

    @staticmethod
    def _is_voice(part):
        """只含一个 record 段的一条消息：原生语音，不走图片账本。"""
        return len(part) == 1 and part[0].get("type") == "record"

    async def _send_parts(self, conn, parts: list[list[dict]], session_key: str,
                          group_id: int | None, user_id: int | None,
                          elapsed: float = 0.0) -> dict | None:
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        async with lock:
            return await self._deliver(conn, parts, session_key, group_id, user_id, elapsed)

    async def _deliver(self, conn, parts, session_key, group_id, user_id, elapsed):
        result = None
        delivered = False
        sticker_sent = False
        stop_text = False
        ledger = None
        sent_media = []
        inbound = self._inbound(session_key)
        typo_fix = None
        typo = self._plan_typo(parts)
        if typo is not None:
            text_idx, wrong_text, right = typo
            parts[0][text_idx]["data"]["text"] = wrong_text
            typo_fix = right
        typing_done = False
        reacted = False
        try:
            for i, part in enumerate(parts):
                if self._is_media(part):
                    if ledger is None:
                        ledger = media.ledger_for(self.bot)
                    segment = part[0]
                    existing_image = segment.get("type") == "image"
                    if existing_image:
                        file = str((segment.get("data") or {}).get("file") or "")
                        ref = media.MediaRef("img", file)
                        resolved = segment
                    else:
                        data = segment.get("data") or {}
                        ref = media.MediaRef(data.get("kind", "img"),
                                             str(data.get("url") or data.get("file") or ""))
                    if ref.kind == "sticker" and sticker_sent:
                        log.debug("[media] 每次回复最多一张 sticker")
                        continue
                    if not existing_image:
                        resolved = await media.resolve(ref, session=session_key, ledger=ledger)
                    if resolved is None:
                        continue
                    # 图/表情包也要「点开、选一张、发出去」的时间；如果它是这次
                    # 回复最先发出去的东西，就用第一段的反应延迟。
                    if not reacted:
                        pause = self._react_delay()
                        reacted = True
                    elif delivered:
                        pause = random.uniform(1.0, 2.5)
                    else:
                        pause = 0.0
                    if pause > 0:
                        await asyncio.sleep(pause)
                    # 每条媒体单独占 1 个 token（回复本身已在 send() 里扣过 1 个）
                    if self.enabled and not self._take_token(session_key, 1):
                        log.info(f"[safety] 限速丢弃媒体 {session_key}")
                        continue
                    receipt = None
                    media_delivered = False
                    for attempt in range(2):
                        try:
                            receipt = await self._raw(conn, [resolved], group_id, user_id)
                            media_delivered = True
                            break
                        except Exception as exc:
                            # 回执超时不等于没发出，重试会让群里收到两张。
                            if getattr(exc, "delivered", False):
                                media._warn(ref, "send unconfirmed")
                                receipt = {}
                                media_delivered = True
                                break
                            if attempt == 0:
                                await asyncio.sleep(0.25)
                            else:
                                media._warn(ref, f"send {type(exc).__name__}")
                    if not media_delivered:
                        continue
                    if not delivered:
                        result = receipt if receipt is not None else {}
                    delivered = True
                    if ref.kind == "img" and not existing_image:
                        ledger.remember(session_key, ref.kind, ref.url, ref.digest)
                    if not existing_image:
                        sent_media.append(ref)
                    if ref.kind == "sticker":
                        sticker_sent = True
                    continue

                if stop_text or (i > 0 and not self._is_voice(part)
                                 and self._inbound(session_key) - inbound >= 2):
                    continue
                delay = self._think_delay(part)
                if self._is_voice(part):
                    # 语音不是「打字」，按一次录音/上传的时间算
                    delay = random.uniform(2.0, 4.0)
                if i > 0:
                    gap = min(3.0, max(0.0, self._gap_delay()))
                    await asyncio.sleep(gap)
                    delay = min(delay, 3.0 - gap)
                else:
                    # 「看到消息 → 反应过来」的时间，加在打字延迟之前。
                    # 生成花掉的时间不再抵扣（elapsed 参数保留，由 subtract_elapsed
                    # 决定要不要恢复旧的抵扣行为），否则模型越慢回复得越快，太像机器。
                    if not reacted:
                        react = self._react_delay()
                        if react > 0:
                            await asyncio.sleep(react)
                        reacted = True
                    if elapsed > 0 and self._c("subtract_elapsed", False):
                        limit = float(self._c("max_delay", 12.0))
                        if elapsed >= limit:
                            delay = 0.0
                        else:
                            delay = max(0.3, min(delay, limit) - elapsed)
                if (not typing_done and delay > 0.4 and self.typing_indicator
                        and any(s.get("type") == "text"
                                and str(s.get("data", {}).get("text", "")).strip()
                                for s in part)):
                    # 一条回复只亮一次「正在输入」，而且要在真正等待之前
                    await self._typing(conn, group_id, user_id)
                    typing_done = True
                if delay > 0:
                    await asyncio.sleep(delay)
                if (i > 0 and not self._is_voice(part)
                        and self._inbound(session_key) - inbound >= 2):
                    continue
                try:
                    data = await self._raw(conn, part, group_id, user_id)
                    if not delivered:
                        result = data if data is not None else {}
                    delivered = True
                except Exception as exc:
                    log.error(f"[safety] 分段发送失败（第 {i+1}/{len(parts)} 段）：{exc}")
                    if getattr(exc, "delivered", False):
                        delivered = True
                    if i == 0:
                        raise
                    stop_text = True
                if typo_fix:
                    right, typo_fix = typo_fix, None
                    await self._send_typo_fix(conn, right, group_id, user_id)
        finally:
            if delivered:
                if ledger is None:
                    ledger = media.ledger_for(self.bot)
                ledger.turn(session_key)
                # 用本次回复的轮数记账，避免把贴纸所在的这轮算作冷却了一轮。
                for ref in sent_media:
                    if ref.kind == "sticker":
                        ledger.remember(session_key, ref.kind, ref.url, ref.digest)
                if sticker_sent:
                    ledger.mark_sticker(session_key)
        return result

    async def _raw(self, conn, segments, group_id, user_id):
        if group_id:
            return await conn.send_message(segments, group_id=int(group_id))
        return await conn.send_message(segments, user_id=int(user_id or 0))

    # ---------------------------------------------------------- 拆句
    def _split(self, segments: list[dict]) -> list[list[dict]]:
        """按 ‖ 切分 QQ 消息，引用与 @ 只放在第一条。"""
        if not segments:
            return []

        body = list(segments)
        head: list[dict] = []
        while body and body[0].get("type") in ("reply", "at"):
            head.append(body.pop(0))
            if (body and body[0].get("type") == "text"
                    and not str(body[0].get("data", {}).get("text", "")).strip()):
                head.append(body.pop(0))

        parts: list[list[dict]] = []
        current: list[dict] = []
        images: list[list[dict]] = []
        records: list[list[dict]] = []
        stickers: list[list[dict]] = []
        for segment in body:
            if segment.get("type") == "image":
                if current:
                    parts.append(current)
                    current = []
                images.append([segment])
                continue
            if segment.get("type") == "record":
                # 语音必须单独成一条：和文字/@ 混在一起 NapCat 会丢段
                if current:
                    parts.append(current)
                    current = []
                records.append([segment])
                continue
            if segment.get("type") != "text":
                current.append(segment)
                continue
            text, refs = media.extract(str(segment.get("data", {}).get("text", "")))
            for ref in refs:
                target = stickers if ref.kind == "sticker" else images
                target.append([{"type": "_media", "data": {"kind": ref.kind, "url": ref.url}}])
            chunks = SPLIT_MARK_RE.split(text)
            for i, chunk in enumerate(chunks):
                if i and current:
                    parts.append(current)
                    current = []
                # 每条消息的首尾空白/换行都去掉，别把空行带进 QQ 消息里
                if chunk.strip():
                    current.append(msg.Text(chunk.strip()))
        if current:
            parts.append(current)

        cap = min(3, max(1, int(self._c("burst", 3))))
        if len(parts) > cap:
            # 多出来的部分并进最后一条：已收句就直接接，否则补逗号；不塞换行
            tail: list[dict] = []
            for part in parts[cap - 1:]:
                if tail:
                    marker = _overflow_join(tail)
                    if marker:
                        tail.append(msg.Text(marker))
                tail.extend(part)
            parts = parts[:cap - 1] + [tail]
        if parts:
            parts[0] = head + parts[0]
        return parts + images + records + stickers

    # ---------------------------------------------------------- 时间
    def _think_delay(self, segments: list[dict]) -> float:
        """模拟「看到消息 → 想 → 打字」。safety.no_delay 打开时直接归零。"""
        if self._c("no_delay", False):
            return 0.0
        text = "".join(str(s.get("data", {}).get("text", "")) for s in segments
                       if s.get("type") == "text")
        cps = max(1.0, float(self._c("typing_cps", 4.0)))
        lo = float(self._c("min_delay", 1.2))
        hi = float(self._c("max_delay", 12.0))

        delay = (len(text) / cps) * random.uniform(0.75, 1.4)
        if delay > hi:
            # 超出上限时也别固定成 hi，否则长消息永远一个时长，反而像机器
            delay = hi * random.uniform(0.85, 1.0)
        delay = max(delay, lo * random.uniform(0.7, 1.25))
        return max(0.3, delay)

    def _inbound(self, session_key: str) -> int:
        """这个会话收到过多少条别人的消息（Core 里维护）。"""
        try:
            return int(self.bot.inbound.get(session_key, 0))
        except Exception:
            return 0

    def _gap_delay(self) -> float:
        if self._c("no_delay", False):
            return 0.0
        lo, hi = self._c("split_gap", [0.8, 2.5])
        return random.uniform(float(lo), float(hi))

    def _in_quiet_hours(self) -> bool:
        window = self._c("quiet_hours", [])
        if not window or len(window) != 2:
            return False
        start, end = int(window[0]), int(window[1])
        if start == end:
            return False
        hour = time.localtime().tm_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end        # 跨零点，如 23 → 7

    # ---------------------------------------------------------- 限速
    def _react_delay(self) -> float:
        """看到消息到「反应过来」要多久。每次回复的第一段只算一次。"""
        if self._c("no_delay", False):
            return 0.0
        span = self._c("react_delay", [3.0, 8.0])
        try:
            lo, hi = float(span[0]), float(span[1])
        except Exception:
            lo, hi = 3.0, 8.0
        if hi < lo:
            lo, hi = hi, lo
        return random.uniform(max(0.0, lo), max(0.0, hi))

    def _take_token(self, key: str, n: int = 1) -> bool:
        """每会话滑动窗口：per_minute 条/分钟、per_hour 条/小时。

        一次回复只扣 1 个 token（不论拆成几条），每条媒体另扣 1 个。
        per_minute / per_hour 配 0（或负数）表示不限制那一档。
        """
        now = time.time()
        window = self._bucket.get(key)
        if not isinstance(window, deque):
            window = deque(window or ())
            self._bucket[key] = window
        cutoff = now - 3600.0
        while window and window[0] < cutoff:
            window.popleft()

        per_minute = int(self._c("per_minute", 6) or 0)
        per_hour = int(self._c("per_hour", 60) or 0)
        if per_minute > 0:
            recent = sum(1 for t in window if now - t < 60.0)
            if recent + n > per_minute:
                return False
        if per_hour > 0 and len(window) + n > per_hour:
            return False
        for _ in range(max(1, n)):
            window.append(now)
        return True
