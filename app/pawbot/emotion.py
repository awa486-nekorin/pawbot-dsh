"""
八维情绪矩阵：让「大肥鱼」有自己的心情，而不是每条都同一个语气。

八个维度（顺序固定）：开心 兴奋 惊讶 烦躁 低落 感动 得意 害羞，
各自 0~100，基准 50。设计上有三层：

    衰减    时间久了所有情绪往 50 回落（指数退避，半衰期默认 90 分钟），
            隔夜再聊天基本就是平常状态。
    回落    每回一条消息，各维度再往 50 挪 2 点，避免情绪一直挂在高位。
    更新    优先让判官模型看上下文给「±15 以内的增减」，判官不可用/
            失败时用关键词启发式兜底。单步最多 ±15，数值夹在 0~100。

`blend()` 把八个数字压成「主导 + 次要 + 强度」，`hint()` 再翻成给
系统提示的语气指引——只描述语气，不让模型把情绪或分数说出来。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import time

from . import judge
from .llm import LLMClient
from .log import log

DIMS = ("开心", "兴奋", "惊讶", "烦躁", "低落", "感动", "得意", "害羞")
BASE = 50.0
# 每个维度自己的基准，目前都是 50；性欲是独立模块（pawbot/libido.py），不混进情绪。
BASE_BY_DIM = {d: BASE for d in DIMS}

# 高值时的语气风格。注意：这些文案会被塞进系统提示，不能带数字。
_STYLE = {
    "开心": "活泼、话多一点、愿意接梗",
    "兴奋": "积极、语气上扬、主动一点",
    "惊讶": "会惊讶一下、追问一句、语气有起伏",
    "烦躁": "短句、克制、不耐烦",
    "低落": "话少、语气软、不想多说",
    "感动": "心里软、想多陪一会儿、说话温一点",
    "得意": "小傲娇、小炫耀",
    "害羞": "扭捏、脸红、话少但软",
}
_STRENGTH = {"轻微": "淡淡的", "明显": "比较明显", "强烈": "很明显、藏不太住"}
_SOOTHE = "别把情绪直接说出来或报数，体现在语气里。"
# 烦躁时的红线：可以凶，但不能越界；和 SOOTHE 一样是三句内的收尾。
_RAGE = ("被骂或反复挑衅可以凶回去怼一句，但不攻击外貌/家庭/身份、"
         "不骂无关的人、不连续骂，情绪别直接说出来或报数，体现在语气里。")

# 情绪判官自己的采样默认值：低温、低思考、只要一段 JSON。
# 没配 emotion.judge_* 时才用这些；model / base_url 之类才回落到 llm.*。
# max_tokens 给到 1200：中转上的 Gemini 会先把思考写进正文，400 的短预算
# 实测约三分之一的回复被截断在半截 JSON 上（5~7 秒一次，重试会顶穿超时）。
_EMOTION_TUNE = {
    "reasoning_effort": "low",
    "temperature": 0.2,
    "max_tokens": 1200,
}
# 配置里的键名：emotion.judge_effort 对应客户端要的 reasoning_effort
_CFG_ALIAS = {"reasoning_effort": "effort"}


class _EmotionJudge(LLMClient):
    """情绪判官专用客户端：emotion.judge_* 优先，没配才继承 llm.*。

    和 JudgeLLM 同一套写法，只是作用域换成 emotion.*：api_key 留空就用
    llm.api_key，不另存一份。这里从不打印 key。
    """

    def _cfg(self, key, default=None):
        if key == "api_key":
            val = judge._raw(self.config, "emotion.judge_api_key")
            if val is not judge._MISSING:
                return str(val).strip()
            return LLMClient._cfg(self, "api_key", default)
        name = _CFG_ALIAS.get(key, key)
        # 情绪判官不跨模型备援：要求就是 gemini-3.8-flash，失败就落关键词兜底，
        # 不要悄悄换成聊天备用模型（那是另一家的判断口径）。
        if key == "fallback_model":
            return ""
        val = judge._raw(self.config, f"emotion.judge_{name}")
        if val is judge._MISSING:
            if key in _EMOTION_TUNE:
                return _EMOTION_TUNE[key]
            val = judge._raw(self.config, f"llm.{key}")
        return default if val is judge._MISSING else val


def _now() -> float:
    return time.time()


def _hour() -> int:
    return time.localtime().tm_hour


def _is_num(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return math.isfinite(float(v))


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


class Emotion:
    def __init__(self, path: str | None, config=None):
        self.path = path or None
        self.config = config
        self.lock = threading.RLock()
        self._values = {d: BASE_BY_DIM[d] for d in DIMS}
        self._updated = _now()
        self.load()

    # ------------------------------------------------------------ 存取
    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            vals = raw.get("values") if isinstance(raw, dict) else None
            if isinstance(vals, dict):
                for dim in DIMS:
                    if _is_num(vals.get(dim)):
                        self._values[dim] = _clamp(float(vals[dim]), 0.0, 100.0)
                    else:
                        self._values[dim] = BASE_BY_DIM[dim]
            ts = raw.get("updated") if isinstance(raw, dict) else None
            if _is_num(ts) and ts > 0:
                self._updated = float(ts)
        except Exception:
            # 文件坏了就当没存过，别让机器人起不来
            self._values = {d: BASE_BY_DIM[d] for d in DIMS}
            self._updated = _now()

    def save(self):
        if not self.path:
            return
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({
                        "values": {d: round(self._values[d], 2) for d in DIMS},
                        "updated": self._updated,
                    }, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception as e:
                log.warn(f"情绪存档失败：{type(e).__name__}")

    def _num_cfg(self, key: str, default: float) -> float:
        if self.config is None:
            return default
        try:
            val = self.config.get(key, default)
        except Exception:
            return default
        return float(val) if _is_num(val) else default

    # ------------------------------------------------------------ 时间
    def decay(self, now: float | None = None):
        """指数回归各自基准：v = base + (v-base) * 0.5 ** (分钟 / 半衰期)。"""
        with self.lock:
            now = _now() if now is None else float(now)
            minutes = (now - self._updated) / 60.0
            if minutes > 0:
                half = self._num_cfg("emotion.half_life_min", 90.0)
                if half <= 0:
                    half = 240.0
                factor = 0.5 ** (minutes / half)
                for dim in DIMS:
                    base = BASE_BY_DIM[dim]
                    # 保留两位：既够用，也避免「刚好到阈值」被浮点误差吃掉
                    self._values[dim] = round(_clamp(
                        base + (self._values[dim] - base) * factor, 0.0, 100.0), 2)
            self._updated = now

    def turn(self):
        """每回一条消息，各维度往自己的基准退一小步，不越过基准。"""
        step = abs(self._num_cfg("emotion.turn_regress", 2.0))
        with self.lock:
            for dim in DIMS:
                base = BASE_BY_DIM[dim]
                v = self._values[dim]
                if v > base:
                    self._values[dim] = max(base, v - step)
                elif v < base:
                    self._values[dim] = min(base, v + step)
            self._updated = _now()

    # ------------------------------------------------------------ 数值
    def apply(self, deltas: dict) -> dict:
        """加一组增减：未知维度/非数字忽略，单步 ±max_step，结果夹 0~100。"""
        applied: dict = {}
        if not isinstance(deltas, dict):
            return applied
        max_step = abs(self._num_cfg("emotion.max_step", 15.0))
        with self.lock:
            self.decay()
            for dim, delta in deltas.items():
                if dim not in DIMS or not _is_num(delta):
                    continue
                step = _clamp(float(delta), -max_step, max_step)
                if not step:
                    continue
                self._values[dim] = round(
                    _clamp(self._values[dim] + step, 0.0, 100.0), 2)
                applied[dim] = int(step) if step.is_integer() else round(step, 2)
        return applied

    def values(self) -> dict:
        """先按时间衰减，再给一份拷贝。"""
        self.decay()
        with self.lock:
            return {d: self._values[d] for d in DIMS}

    def reset(self):
        with self.lock:
            self._values = {d: BASE_BY_DIM[d] for d in DIMS}
            self._updated = _now()
        self.save()

    # ------------------------------------------------------------ 解读
    def blend(self, with_values: bool = False) -> dict:
        """主导/次要情绪：只看高于 50 的偏离，低值只代表「没有」。

        with_values=True 时附带当前八维数值（给后台页面看，不进提示词）。
        """
        vals = self.values()
        if with_values:
            return dict(self._blend(vals), values={k: round(v, 1) for k, v in vals.items()})
        return self._blend(vals)

    def _blend(self, vals: dict) -> dict:
        ranked = sorted(DIMS, key=lambda d: vals[d], reverse=True)
        dom = ranked[0]
        dd = vals[dom] - BASE
        if dd < 8:
            return {"dominant": None, "secondary": None,
                    "weights": (), "intensity": None}
        intensity = "轻微" if dd < 15 else ("明显" if dd < 30 else "强烈")
        sec = ranked[1]
        sd = vals[sec] - BASE
        if sd >= 8:
            w_dom, w_sec = dd ** 1.5, sd ** 1.5
            total = w_dom + w_sec
            weights = (round(w_dom / total, 2), round(w_sec / total, 2))
            if weights[1] >= 0.25:
                return {"dominant": dom, "secondary": sec,
                        "weights": weights, "intensity": intensity}
        return {"dominant": dom, "secondary": None,
                "weights": (1.0,), "intensity": intensity}

    def hint(self) -> str:
        """给系统提示的语气指引：不超过三句，不含数字和分数。"""
        state = self.blend()
        dom = state["dominant"]
        if not dom:
            return "状态平常，正常聊。" + _SOOTHE
        sec = state["secondary"]
        first = f"主要是{dom}"
        if sec:
            first += f"，带一点{sec}"
        first += f"，{_STRENGTH[state['intensity']]}。"
        body = "语气上" + _STYLE[dom]
        if sec:
            body += "；再掺一点" + _STYLE[sec]
        body += "。"
        return first + body + (_RAGE if dom == "烦躁" else _SOOTHE)

    # ------------------------------------------------------------ 兜底
    def heuristic_deltas(self, text: str, *, addressed: bool) -> dict:
        """关键词兜底：只抓几个最常见的刺激，别做成大词表。"""
        low = str(text or "").lower()
        out: dict = {}

        def bump(dim: str, delta: int):
            out[dim] = out.get(dim, 0) + delta

        if any(k in low for k in ("可爱", "厉害", "好棒", "喜欢你", "真聪明", "夸")):
            bump("开心", 12)
            bump("得意", 9)
        if any(k in low for k in ("傻", "笨", "蠢", "滚", "垃圾", "废物", "sb", "人机", "闭嘴")):
            bump("烦躁", 14)
            bump("低落", 5)
            bump("开心", -7)
        if any(k in low for k in ("抱抱", "贴贴", "摸摸", "想你", "亲亲")):
            bump("感动", 13)
            bump("开心", 5)
        if any(k in low for k in ("色", "涩", "骚", "撩", "想要", "硬了", "湿了", "裸", "脱",
                                  "亲一下", "咬一口", "腿", "胸")):
            bump("害羞", 5)
            bump("兴奋", 4)
        if any(k in low for k in ("?", "？", "为什么", "怎么")):
            bump("惊讶", 5)
        if any(k in low for k in ("!!", "！！", "卧槽", "牛")):
            bump("兴奋", 6)
        if 1 <= _hour() <= 6:
            bump("低落", 4)
        if addressed:
            bump("惊讶", 2)
        return {k: v for k, v in out.items() if v}

    # ------------------------------------------------------------ 更新
    def _judge_prompt(self, text: str, context: str, addressed: bool) -> str:
        vals = self.values()
        current = "，".join(f"{d} {int(round(vals[d]))}" for d in DIMS)
        lines = [str(context or "").strip()]
        ctx = "\n".join(" ".join(line.split()) for line in lines[0].splitlines()[-4:])[:600]
        who = "这条消息在叫她" if addressed else "这条消息不一定是在叫她"
        parts = [
            "你是「大肥鱼」的情绪判官。她是二次元少女群友，不是助手，"
            "心情随聊天自然起伏：被夸会开心/得意，被骂会烦躁，被撒娇会感动，"
            "无聊或深夜聊天会害羞。\n"
            f"当前情绪（基准五十）：{current}",
        ]
        if ctx:
            parts.append(f"最近上下文：\n{ctx}")
        parts.append(f"新消息（{who}）：{str(text or '')[:500]}")
        parts.append(
            "判断这条消息让她各维度变化多少：键只能用"
            + "、".join(DIMS)
            + "，值是整数，范围 -15 到 15，只写有变化的维度，没有变化输出 {}。"
            "幅度要拉开：明显的夸奖、辱骂、撒娇、撩拨给 8 到 15；"
            "有情绪但不强烈给 4 到 7；普通闲聊给 0 到 2。不要所有情况都给 1 到 3，"
            "那样情绪永远没起伏。")
        parts.append(
            "输出格式（硬性要求，违反这次判断就作废）：回复里必须只有 JSON，"
            "第一个非空白字符是 { ，最后一个非空白字符是 } ，能直接 json.loads 解析；"
            "不要解释、不要代码块、不要前后缀、不要先写一段话再给 JSON，"
            "没有变化也输出 {}。格式就长这样："
            '{"开心": 9, "感动": 6, "烦躁": -4}')
        return "\n".join(parts)

    @staticmethod
    def _clean_deltas(data) -> dict | None:
        if not isinstance(data, dict):
            return None
        return {k: v for k, v in data.items() if k in DIMS and _is_num(v)}

    def _judge_model(self, config) -> str:
        """emotion.judge_model；留空表示还是走旧的 judge.* 通道。"""
        try:
            return str(config.get("emotion.judge_model", "") or "").strip()
        except Exception:
            return ""

    async def _judge_via_gemini(self, config, prompt: str,
                                timeout: float) -> dict | None:
        """用 emotion.judge_model（中转上的 Gemini）判一次；任何失败返回 None。

        判官只能问一次：中转一次要 5~7 秒，emotion.judge_timeout 只有 10 秒，
        重试基本都会超时，不如直接把预算给足、失败就交给关键词兜底。
        """
        try:
            client = _EmotionJudge(config)
            if not str(client._cfg("model", "") or "").strip():
                return None
            if not str(client._cfg("api_key", "") or "").strip():
                log.debug("情绪判官没有可用 key，跳过")
                return None
            reply = await asyncio.wait_for(
                client.chat([{"role": "user", "content": prompt}]),
                timeout=max(0.1, float(timeout)))
            data = judge._first_json(reply)
            if data is None:
                log.debug("情绪判官回复里没有 JSON，按失败处理")
            return data
        except asyncio.TimeoutError:
            log.debug(f"情绪判官超时（{timeout}s），跳过")
            return None
        except Exception as e:                      # 网络、配置、解析，一律吞掉
            log.warn(f"情绪判官调用失败：{type(e).__name__}")
            return None

    async def update(self, text: str, *, config, context: str = "",
                     addressed: bool = False, use_judge: bool = True) -> dict:
        """收一条消息，更新情绪并落盘，返回实际生效的增减。永不抛异常。"""
        try:
            if config is not None:
                self.config = config
            self.decay()

            deltas = None
            if use_judge and config is not None:
                # ① 决策模型（类 JEV，概率化）：开着就优先用它判「变不变/变什么/多强」
                try:
                    from . import decider
                    if decider.enabled(config):
                        decided = await decider.decide_mood(
                            config, text, context, addressed, self.values())
                        if decided is not None:
                            deltas = decided
                            log.debug(f"决策模型情绪：{deltas}")
                except Exception as e:
                    log.debug(f"决策模型异常：{type(e).__name__}")
                # ② 没开/失败：退回原来的判官（judge_model 或 judge.* 通道）
                if deltas is None:
                    timeout = self._num_cfg("emotion.judge_timeout", 10.0)
                    data = None
                    try:
                        if self._judge_model(config):
                            # 配了 judge_model：走中转上的 Gemini，不碰 judge.* 通道
                            data = await self._judge_via_gemini(
                                config, self._judge_prompt(text, context, addressed),
                                timeout)
                        elif judge.judge_enabled(config):
                            data = await judge.judge_json(
                                config, self._judge_prompt(text, context, addressed),
                                timeout=timeout)
                    except Exception as e:
                        log.debug(f"判官异常：{type(e).__name__}")
                        data = None
                    deltas = self._clean_deltas(data)
            heuristic = self.heuristic_deltas(text, addressed=addressed)
            if deltas is None or not deltas:
                deltas = heuristic
            elif heuristic:
                strong = max(abs(v) for v in heuristic.values())
                weak = max((abs(v) for v in deltas.values()), default=0)
                if strong >= 6 and weak < 3:
                    # 判官给得太保守，但关键词信号很明显：以关键词为准
                    deltas = dict(deltas)
                    deltas.update(heuristic)

            applied = self.apply(deltas)
            self.save()
            return applied
        except Exception as e:
            log.warn(f"情绪更新失败：{type(e).__name__}")
            return {}
