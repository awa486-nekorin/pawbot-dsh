"""
主动行为引擎：没人说话的时候，它自己找点事干。

这就是「群友」和「问答机器人」的分界线——真人不会等别人 @ 才开口。

一轮 tick 的逻辑：

    群里多久没人说话了？
        │
        ├─ 还没到点（阈值是随机滚动的，不是固定值）→ 什么都不做
        │
        └─ 到点了
             │
             ├─ 刚才有话题残留 → 接着那个话题说一句
             ├─ 没有话题       → 上网找乐子 / 刷 B 站，挑一条分享
             └─ 心情太差       → 这次跳过（真人也有一天不想说话）

几个必须守住的边界：
  · 只在配置允许的群里活跃（proactive.groups 为空 = 不主动）
  · 静默时段不说话
  · 每次说完重置一个较长冷却，绝不刷屏
  · 一轮最多说一次，说失败就退避
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time

from . import bilibili
from . import bili_watch
from . import life
from . import tasks
from .context import Context
from .log import log


# 模型回「-」表示這次不想說。各種破折號都算，別把字面量「-」丟進群。
_DECLINE_CHARS = set("-－—–─")


def _declined(text) -> bool:
    """整條回覆只是一串破折號（典型就是「-」）= 這次不想說。"""
    t = str(text or "").strip()
    if not t:
        return False
    t = t.strip("\"'“”‘’「」『』（）() \t\r\n")
    if not t:
        return False
    return all(ch in _DECLINE_CHARS for ch in t)


class _FakeEvent:
    """让主动发言也能复用 Context 的发送 / 拟人化逻辑。"""

    def __init__(self, bot, group_id: int):
        self.post_type = "message"
        self.type = "message"
        self.message_type = "group"
        self.sub_type = "normal"
        self.message_id = 0
        self.user_id = 0
        self.group_id = group_id
        self.self_id = 0
        self.message: list[dict] = []
        self.plain = ""
        self.raw = {}
        self.to_me = False
        self.reply_id = None
        self.quoted_text = ""
        self.image_desc = ""
        self.sender = {}
        self.time = int(time.time())

    @property
    def is_group(self) -> bool:
        return True

    @property
    def session_id(self) -> str:
        return f"group_{self.group_id}"

    @property
    def sender_name(self) -> str:
        return ""


class Proactive:
    def __init__(self, bot):
        self.bot = bot
        self.task: asyncio.Task | None = None
        # 每个群下一次「到点」的时间，随机滚动，避免整点刷屏的感觉
        self._next: dict[str, float] = {}
        self._fail_streak: dict[str, int] = {}
        self._last_span: dict[str, int] = {}   # 上次算出的阈值（分钟），用来抑制重复日志
        # 每日次数 / 最短间隔的状态，懒加载后一直留在内存里
        self._state: dict | None = None
        self._last_surf: dict[str, float] = {}   # 每个群上次主动上网冲浪的时间

    # ------------------------------------------------------------ 配置
    def _cfg(self, k, d=None):
        return self.bot.config.get(f"proactive.{k}", d)

    def _qzone_blocked(self, name: str) -> bool:
        is_qzone = name == "post_qzone" or name.startswith("qzone_")
        if not is_qzone:
            return False
        return not (self.bot.config.get("qzone.enabled", False) is True
                    and self.bot.config.get("qzone.proactive_enabled", False) is True)

    # ------------------------------------------------------------ 每日额度状态
    def _state_path(self) -> str:
        raw = str(self._cfg("state_file", "") or "").strip()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if raw and os.path.isabs(raw):
            return raw
        return os.path.join(root, raw) if raw else os.path.join(root, "data", "proactive_state.json")

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _load_state(self) -> dict:
        """读 data/proactive_state.json；坏档/缺档一律当作空状态，绝不让主动引擎挂掉。"""
        if self._state is not None:
            return self._state
        state = {"date": "", "counts": {}, "last": {}}
        try:
            with open(self._state_path(), "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                state["date"] = str(raw.get("date") or "")
                counts = raw.get("counts")
                if isinstance(counts, dict):
                    for k, v in counts.items():
                        try:
                            state["counts"][str(k)] = int(v)
                        except (TypeError, ValueError):
                            continue
                last = raw.get("last")
                if isinstance(last, dict):
                    for k, v in last.items():
                        try:
                            state["last"][str(k)] = float(v)
                        except (TypeError, ValueError):
                            continue
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug(f"主动发言状态读取失败（按空状态处理）：{type(exc).__name__} {exc}")
        self._state = state
        return state

    def _save_state(self) -> None:
        if self._state is None:
            return
        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            log.debug(f"主动发言状态落盘失败（忽略）：{type(exc).__name__} {exc}")

    def _roll_day(self, state: dict) -> None:
        """跨本地日期只清次数；上次发言时间保留，最短间隔跨零点仍然有效。"""
        today = self._today()
        if state.get("date") != today:
            state["date"] = today
            state["counts"] = {}

    def _quota_ok(self, session_id: str) -> tuple[bool, str]:
        state = self._load_state()
        self._roll_day(state)
        daily_max = int(self._cfg("daily_max", 12) or 0)
        count = int(state["counts"].get(session_id, 0) or 0)
        if daily_max > 0 and count >= daily_max:
            return False, f"今天已主动说过 {count} 次"
        gap_min = float(self._cfg("min_gap_min", 20) or 0)
        last_at = float(state["last"].get(session_id, 0) or 0)
        if gap_min > 0 and last_at and time.time() - last_at < gap_min * 60:
            return False, f"距上次主动发言不到 {gap_min:g} 分钟"
        return True, ""

    def _note_spoken(self, session_id: str) -> None:
        """成功发出去一次才计数；失败 / 被限速丢掉的不算。"""
        state = self._load_state()
        self._roll_day(state)
        state["counts"][session_id] = int(state["counts"].get(session_id, 0) or 0) + 1
        state["last"][session_id] = time.time()
        self._save_state()

    @property
    def enabled(self) -> bool:
        if not self._cfg("enabled", False):
            return False
        return bool(self._groups())

    def _groups(self) -> list[int]:
        """配置里写死的群。留空 = 用它实际收到过消息的群（更省心）。"""
        raw = self._cfg("groups", []) or []
        out = []
        for g in raw:
            try:
                out.append(int(g))
            except Exception:
                continue
        if out:
            return out
        if str(self._cfg("auto_groups", "true")).lower() in ("false", "0", "no"):
            return []
        # 从话题追踪里拿它真的待着的群
        for sid in list(self.bot.topic.groups):
            if sid.startswith("group_"):
                try:
                    out.append(int(sid[6:]))
                except Exception:
                    continue
        return out

    def _span(self, session_id: str | None = None) -> tuple[float, float]:
        """冷场阈值：读 proactive.idle_min/idle_max，再按群活跃度缩放。

        这两条线是「多久没人才开口」的主闸门，别写死成几十秒——
        群一安静就每半分钟说一句，那不是群友，是背景噪音。
        """
        lo = float(self._cfg("idle_min", 900))
        hi = float(self._cfg("idle_max", 2400))
        if hi < lo:
            hi = lo
        floor = float(self._cfg("idle_floor", 300))
        lo, hi = max(lo, floor), max(hi, floor)
        if session_id and self._cfg("adaptive", True):
            low, high = self._cfg("adaptive_range", [0.75, 1.5])
            lo, hi, _info = self.bot.topic.adaptive_span(
                session_id, lo, hi, float(low), float(high))
            lo, hi = max(floor, lo), max(floor, hi)
        return (lo, hi)

    def _roll(self, session_id: str) -> float:
        lo, hi = self._span(session_id)
        gap = random.uniform(lo, hi)
        # 作息：上午、晚上更爱冒泡，犯困、熬夜时懒得开口
        if self.bot.config.get("life.enabled", True):
            gap *= max(0.5, min(2.5, life.proactive_factor()))
        # 连着失败就退避，别在服务端抽风时疯狂重试
        streak = self._fail_streak.get(session_id, 0)
        if streak:
            gap *= min(6.0, 1.5 ** streak)
        return time.time() + gap

    # ------------------------------------------------------------ 生命周期
    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._loop(), name="proactive")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None

    async def _loop(self):
        await asyncio.sleep(20)                   # 启动后先安静一会
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"主动行为异常：{type(e).__name__} {e}")
            # 每 20 秒看一次表，够用了，不用更密
            await asyncio.sleep(20)

    # ------------------------------------------------------------ 观测
    def snapshot(self) -> list[dict]:
        """给 WebUI 看的：每个群现在什么状态、下次打算什么时候开口。"""
        out: list[dict] = []
        now = time.time()
        for gid in self._groups():
            sid = f"group_{gid}"
            lo, hi = self._span(sid)
            info = self.bot.topic.activity(sid)
            nxt = self._next.get(sid, 0)
            out.append({
                "group": gid,
                "idle_sec": int(self.bot.topic.idle_seconds(sid)),
                "per_hour": round(float(info.get("per_hour") or 0), 1),
                "median_gap_sec": int(info.get("median_gap") or 0),
                "level": round(float(info.get("level") or 0), 2),
                "known": bool(info.get("known")),
                "span_min": [round(lo / 60), round(hi / 60)],
                "next_in_sec": max(0, int(nxt - now)) if nxt else None,
                "fail_streak": self._fail_streak.get(sid, 0),
            })
        return out

    # ------------------------------------------------------------ 主逻辑
    async def _tick(self):
        if not self.enabled:
            return
        if self.bot.conn_for() is None:            # 协议端没连上，别空转
            return
        if self.bot.safety._in_quiet_hours():
            return
        if self.bot.config.get("life.enabled", True) and life.quiet_now():
            return                                  # 睡着了，没人叫就不说话

        for gid in self._groups():
            sid = f"group_{gid}"
            now = time.time()
            if sid not in self._next:
                self._next[sid] = self._roll(sid)

            # 有人说的话就顺延——冷场才需要它救。门槛也用自适应后的下限
            idle = self.bot.topic.idle_seconds(sid)
            lo, _hi = self._span(sid)
            if idle < lo:
                continue

            if now < self._next[sid]:
                continue

            await self._speak_once(gid, sid)
            self._next[sid] = self._roll(sid)

    async def _speak_once(self, group_id: int, session_id: str):
        # 先看今天的额度和最短间隔：一个群一天最多说几次、两次至少隔多久。
        # 超了就走人，连生成都不做。
        quota_ok, quota_why = self._quota_ok(session_id)
        if not quota_ok:
            log.debug(f"主动发言跳过（{quota_why}）：{session_id}")
            return

        ok, why = self.bot.topic.should_chime_in(
            session_id, self.bot.config)
        if not ok:
            log.debug(f"主动发言跳过（{why}）：{session_id}")
            return

        action = await self._choose(session_id)
        if action["kind"] == "skip":
            log.debug(f"主动发言跳过（{action['hint']}）：{session_id}")
            return
        log.info(f"主动发言 [{why}] → {action['kind']}：{action['hint'][:50]}")
        started = time.monotonic()

        try:
            text, media = await self._compose(group_id, session_id, action)
        except Exception as e:
            log.error(f"主动发言生成失败：{type(e).__name__} {e}")
            self._fail_streak[session_id] = self._fail_streak.get(session_id, 0) + 1
            return

        # 模型回「-」= 这次不想说。以前会把这句字面量丢进群，还会顺手把视频
        # 记成「已分享」——其实什么都没发出去（P2-6）。判断放在 polish 前，
        # 免得 polish 先把它清成空字串，落到「有 media 就照发」那条路。
        if _declined(text):
            log.debug(f"主动发言跳过（模型回「-」，不算说过也不算分享过）：{session_id}")
            return

        from .style import polish
        text = polish(text)

        if not text.strip() and not media:
            self._fail_streak[session_id] = self._fail_streak.get(session_id, 0) + 1
            return

        ev = _FakeEvent(self.bot, group_id)
        ctx = Context(event=ev, bot=self.bot, config=self.bot.config)
        payload = list(media) + ([{"type": "text", "data": {"text": text}}] if text else [])
        try:
            result = await ctx.send(payload, elapsed=time.monotonic() - started)
        except Exception as e:
            log.error(f"主动发言发送失败：{type(e).__name__} {e}")
            self._fail_streak[session_id] = self._fail_streak.get(session_id, 0) + 1
            return
        if result is None:
            return

        self._note_spoken(session_id)
        self._fail_streak[session_id] = 0
        if action["kind"] == "bili":
            bilibili.mark_shared(session_id)
            self._mark_bili_shared(session_id, action)
        self.bot.topic.mark_chime(session_id)
        self.bot.topic.seen(session_id, "我", text, is_self=True)
        self.bot.store.append(session_id, "assistant", text, who="我")
        try:
            from . import turns
            tasks.spawn(turns.update(self.bot, session_id), name="turn-summary")
        except Exception as e:
            log.debug(f"主动发言后小結调度失败：{type(e).__name__}")

    def _mark_bili_shared(self, session_id: str, action: dict) -> None:
        """发送成功之后才把报告里的 bvid 记进 shared.json。

        插件靠 shared.json 过滤「已推荐过的」，所以这一步不能提前做：
        模型回「-」或发送失败时这条片其实没出现在群里，记了就被永久消耗。
        """
        report = action.get("report")
        if not isinstance(report, dict):
            return
        bvid = str(report.get("bvid") or "").strip()
        if not bvid:
            return
        try:
            bili_watch.mark_shared(bvid, session_id, dir=self._bili_watch_dir())
        except Exception as exc:
            log.warn(f"标记 B 站视频已分享失败：{type(exc).__name__} {exc}")

    # ------------------------------------------------------------ 选什么
    async def _choose(self, session_id: str) -> dict:
        """决定这一轮是「接话题」还是「找乐子」。"""
        digest = self.bot.topic.digest(session_id, 6)
        kws = self.bot.topic.keywords(session_id, 5)

        # 有话题残留，而且不是太久以前 → 接着聊
        if digest and kws and random.random() < 0.55:
            return {"kind": "join",
                    "digest": digest,
                    "keywords": kws,
                    "hint": "接着刚才的话题说一句"}

        # 上网冲浪（搜索 / 刷 B 站）要同时过两关：本次抽中的概率，和每群最短间隔。
        # 没过就接着群里的话说，没有话题可接就干脆这次不开口。
        if not self._surf_allowed(session_id):
            if digest and kws:
                return {"kind": "join", "digest": digest, "keywords": kws,
                        "hint": "接着刚才的话题说一句"}
            return {"kind": "skip", "hint": "不冲浪也没话题，这次不说"}

        mode = str(self._cfg("idle_activity", "auto"))
        if mode == "bilibili":
            bili = await self._fun_bili(session_id)
            return bili if bili["kind"] == "bili" else self._fun_search(kws)
        if mode == "search":
            return self._fun_search(kws)
        # auto：多数时候只是上网看看，偶尔才刷 B 站。
        # 视频是群里最显眼的东西，发得太勤就成了刷屏的搬运工。
        chance = float(self._cfg("bili_chance", 0.2))
        if random.random() < chance:
            picked = await self._fun_bili(session_id)
            if picked["kind"] == "bili":
                return picked
        return self._fun_search(kws)

    def _surf_allowed(self, session_id: str) -> bool:
        """主动上网冲浪的节流：概率 surf_chance，加上每群 surf_min_gap_min 分钟的最短间隔。"""
        now = time.time()
        last = self._last_surf.get(session_id, 0.0)
        if now - last < float(self._cfg("surf_min_gap_min", 120)) * 60:
            return False
        if random.random() >= float(self._cfg("surf_chance", 0.25)):
            return False
        self._last_surf[session_id] = now
        return True

    def _bili_gap(self) -> float:
        return float(self._cfg("bili_min_gap", 3600))

    def _bili_watch_dir(self) -> str:
        """外接插件共享目录；配置缺失/读坏都用默认目录，绝不抛。"""
        try:
            return str(self.bot.config.get("tools.bili_watch_dir",
                                           bili_watch.DEFAULT_DIR)
                       or bili_watch.DEFAULT_DIR)
        except Exception as exc:
            log.warn(f"读 B 站观看目录配置失败（用默认目录）：{type(exc).__name__} {exc}")
            return bili_watch.DEFAULT_DIR

    def _bili_watch_enabled(self) -> bool:
        try:
            return bool(self.bot.config.get("tools.bili_watch_enabled", True))
        except Exception as exc:
            log.warn(f"读 B 站观看开关失败（按开启处理）：{type(exc).__name__} {exc}")
            return True

    async def _fun_bili(self, session_id: str) -> dict:
        # 刚发过视频就先不刷，别一个群一晚上全在转 B 站
        if not bilibili.share_allowed(session_id, self._bili_gap()):
            log.debug(f"主动刷 B 站太频繁，这次换成上网看：{session_id}")
            return {"kind": "search", "query": "今天网上在聊什么",
                    "hint": "上网看看有什么值得说的，挑一件分享"}
        # 只用「多条消息都提到」的词去搜，碎片词会搜出垃圾
        solid = self.bot.topic.solid_keywords(session_id, 4)
        keyword = random.choice(solid) if (solid and random.random() < 0.4) else None

        # 优先用插件真的看过之后写的报告：有报告 = 真的看到内容，才配说感想。
        wdir = self._bili_watch_dir()
        report = None
        if self._bili_watch_enabled():
            try:
                report = bili_watch.pick_report(
                    session_id, keyword=keyword, max_age_hours=72, dir=wdir)
            except Exception as exc:
                log.warn(f"读 B 站观看报告失败（退回只看标题）："
                         f"{type(exc).__name__} {exc}")
                report = None
            if isinstance(report, dict) and report.get("bvid"):
                title = str(report.get("title") or "").strip()
                level = str(report.get("watched_level") or "title_only")
                # 按真实观看程度说人话，别把 title_only 也说成「看过」。
                level_hint = {
                    "subtitle": "你看过字幕，真的看过内容，可以聊里面的细节",
                    "desc_comments": "你只看了简介和评论，没看到画面，别假装看过画面",
                    "title_only": "你只看到标题，没看过内容，别假装看过",
                }.get(level, "你还不知道看过多少，别假装看过内容")
                return {
                    "kind": "bili",
                    "videos": [],
                    "report": report,
                    "hint": (f"刷到个视频《{title}》（观看程度：{level}）。"
                             f"{level_hint}；分享时别超出这个程度编内容"),
                }
            # 没有报告：让插件去看一眼，同时先退回只看标题（并老实说没看）。
            try:
                bili_watch.request_watch("keyword" if keyword else "auto",
                                         keyword, session_id, dir=wdir)
            except Exception as exc:
                log.warn(f"投递 B 站观看请求失败：{type(exc).__name__} {exc}")

        loop = asyncio.get_running_loop()
        try:
            picks = await loop.run_in_executor(
                None, lambda: bilibili.pick_lively(3, keyword))
        except Exception:
            picks = []
        if not picks:
            return self._fun_search([])
        return {"kind": "bili", "videos": picks,
                "hint": ("刷到个视频，但你还没看过这条视频的内容，只知道标题和播放数据。"
                         "分享时说清楚你还没看，可以说「还没看，标题看着挺有意思」，"
                         "别假装看过，别说里面的具体情节")}

    def _fun_search(self, kws: list[str] | None = None) -> dict:
        topics = ["今天有什么新鲜事", "最近科技圈什么热闹", "今天网上在聊什么"]
        return {"kind": "search",
                "query": random.choice(topics),
                "hint": "上网看看有什么值得说的，挑一件分享"}

    # ------------------------------------------------------------ 生成
    async def _compose(self, group_id: int, session_id: str, action: dict):
        """走 Agent（带工具），让它自己决定要不要顺手用工具。"""
        from . import perceive  # noqa: F401  保持 import 一致性

        cfg = self.bot.config
        rounds = int(cfg.get("tools.max_rounds", 2))
        tools_on = bool(cfg.get("tools.enabled", True))

        extra = self._prompt(action, session_id)
        if cfg.get("life.enabled", True):
            from . import groupinfo
            extra += "\n\n" + life.life_hint(
                group_id=group_id,
                group_info=groupinfo.for_bot(self.bot).cached(group_id))
        system = self.bot.persona.build(group_id=group_id, sender="", extra=extra)
        messages: list[dict] = [{"role": "system", "content": system}]

        summary = self.bot.store.summary(session_id)
        turn_summary = self.bot.store.turn_summary(session_id)
        if summary and turn_summary:
            from difflib import SequenceMatcher
            if SequenceMatcher(None, summary, turn_summary).ratio() > 0.6:
                summary = ""
        if summary:
            messages.append({"role": "system",
                             "content": "## 这个群之前聊过的（摘要）\n" + summary})

        # 最近话题作为检索词；主动发言的事件没有明确说话人。
        query = self.bot.topic.digest(session_id, 6) or str(action.get("hint") or action.get("query") or "")
        longmem = getattr(self.bot, "longmem", None)
        if longmem is not None:
            event = _FakeEvent(self.bot, group_id)
            from types import SimpleNamespace
            ctx = SimpleNamespace(user_id=0, session_id=session_id, is_group=True,
                                  group_id=group_id, event=event)
            recalled = longmem.format_recall(ctx, query, int(cfg.get("longmem.top_k", 6)))
            if recalled:
                messages.append({"role": "system", "content": recalled})

        # 同一套 300 条加权窗口，和它回话时看到的是一样的语境
        from . import ctxwin
        rows = self.bot.store.window(session_id,
                                     int(cfg.get("chat.context_messages", 300)))
        window_msgs, _stats = ctxwin.build_messages(rows, cfg)
        messages += window_msgs

        turn_summary = self.bot.store.turn_summary(session_id)
        if turn_summary:
            messages.append({"role": "system", "content":
                             "## 你记得的刚才这几轮（内部记忆，不要复述、不要提「记录」二字）\n"
                             + turn_summary})

        # 视频已经在 _choose 里挑好了，直接当素材给模型，不用再调工具
        report = action.get("report") if action.get("kind") == "bili" else None
        if not isinstance(report, dict) or not report.get("bvid"):
            report = None
        if action["kind"] == "bili":
            if report is not None:
                # 这里**不能** mark_shared：生成阶段还没发出去，模型可能回「-」
                # 或者发送失败，记了就等于把这条片永久消耗掉（P2-6）。
                # 真正记账在 _speak_once 发送成功之后。
                messages.append({
                    "role": "user",
                    "content": ("（你刚刷到这个视频，下面是你真实的观看记录）\n"
                                + bili_watch.format_for_model(report)),
                })
            elif action.get("videos"):
                # 只给一条：给了多条它可能说 A 挂 B，卡片和文案就对不上了
                v = action["videos"][0]
                messages.append({
                    "role": "user",
                    "content": ("（你刚刷到这个视频，但你还没看过内容，"
                                "只看到标题和数据，别假装看过）\n"
                                + bilibili.brief(v)),
                })
        elif action["kind"] == "search":
            messages.append({"role": "user",
                             "content": f"（你想搜：{action['query']}）"})

        schemas = self.bot.toolbox.schemas() if tools_on else []
        # 空间正文/留言没有可靠的逐条公开性证明，不能成为群聊生成素材。
        # 不只隐藏 schema；下面执行时也拦住模型自行编出的调用。
        private_space_reads = {"qzone_list", "qzone_comments"}
        schemas = [s for s in schemas
                   if s["function"]["name"] not in private_space_reads
                   and not self._qzone_blocked(s["function"]["name"])]
        # 视频已选好，别再让它去刷 B 站（会重复）
        if action["kind"] == "bili":
            schemas = [s for s in schemas
                       if s["function"]["name"] != "bili_video"]
        messages.append({"role": "user",
                         "content": "（现在按上面的要求说点什么。直接给你要说的话，"
                                    "不要解释你在干什么，不要加旁白。）"})

        media: list[dict] = []
        # 视频是先挑好的，卡片直接挂上去——不靠模型再调一次工具
        if action["kind"] == "bili":
            if report is not None:
                title = str(report.get("title") or "").strip()
                url = str(report.get("url") or "").strip()
                # 默认不发 JSON 卡片（手机 QQ 会提示「发送者版本过低」）。
                # 报告字段和 bilibili.card 要求的略有不同，只在前置字段齐了才组。
                if bilibili.card_enabled(self.bot.config) and title and url:
                    try:
                        media.append(bilibili.card(report))
                    except Exception as exc:
                        log.warn(f"B 站视频卡片组装失败（跳过）："
                                 f"{type(exc).__name__} {exc}")
                if title and url:
                    from . import message as _msg
                    media.append(_msg.Text(f"{title}\n{url}"))
            elif action.get("videos"):
                v = action["videos"][0]
                if bilibili.card_enabled(self.bot.config):
                    try:
                        media.append(bilibili.card(v))
                    except Exception:
                        pass
                from . import message as _msg
                media.append(_msg.Text(f"{v['title']}\n{v['url']}"))

        for turn in range(rounds + 1):
            allow = schemas if (tools_on and turn < rounds and schemas) else None
            msg = await self.bot.llm.chat_message(messages, allow)
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip(), media
            assistant = {"role": "assistant",
                         "content": msg.get("content") or "",
                         "tool_calls": calls}
            if msg.get("reasoning_content"):
                assistant["reasoning_content"] = msg["reasoning_content"]
            messages.append(assistant)
            import json as _json
            for call in calls:
                fn = call.get("function") or {}
                try:
                    args = _json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if fn.get("name", "") in private_space_reads:
                    res, extra = "不能把私人空间动态或留言读取到群聊中。", []
                elif self._qzone_blocked(fn.get("name", "")):
                    res, extra = "主动空间操作未开启（qzone.proactive_enabled=false）；不能执行。", []
                else:
                    # A proactive run has no verified human actor. Even with
                    # opt-in, Qzone mutations must reject this absent context.
                    res, extra = await self.bot.toolbox.execute(None, fn.get("name", ""), args)
                media.extend(extra)
                messages.append({"role": "tool",
                                 "tool_call_id": call.get("id", ""),
                                 "content": str(res)[:1500]})
        return "", media

    def _prompt(self, action: dict, session_id: str) -> str:
        digest = self.bot.topic.digest(session_id, 6)
        mood = self.bot.mood.hint()

        head = ("## 现在的情况\n群里安静下来了，没人叫你，是你自己想说话。\n"
                "这不算回答问题，就是你刷到点什么，随手丢到群里。\n")
        if digest:
            head += f"\n群里刚才在聊：\n{digest}\n"

        if action["kind"] == "bili":
            body = ("\n你刚才在 B 站刷到一个视频。**分享它，然后说你自己的想法**——"
                    "一句就够，别写影评。可以吐槽，可以觉得有意思，可以说跟群里刚才的话题有关。")
        elif action["kind"] == "search":
            body = (f"\n你打算上网看看「{action['query']}」。"
                    "先调 web_search 搜一下，挑一件真的值得说的，"
                    "用一两句话丢到群里，别写成新闻稿。")
        else:
            body = ("\n接着群里刚才的话题，说一句你自己的看法。"
                    "不要总结别人说过的话，不要问「你们怎么看」，就是随口一句。")

        n = random.choices((1, 2, 3, 4, 5), weights=(3, 3, 2, 1, 1))[0]
        tail = ("\n\n硬性要求：\n"
                f"· 这次说 {n} 句话（内容多就分成几句写，别硬塞进一句），最多 {25 + 15 * n} 字\n"
                "· 不要开场白、不要「话说」「说起来」这种播音腔\n"
                "· 不要说自己在干什么，不要加括号旁白\n"
                "· 不要 @ 任何人\n"
                "· 觉得没什么好说的就回一个「-」，我当你这次不想说")
        if mood:
            tail += "\n· " + mood
        return head + body + tail