"""
Core：把协议层、总线、插件、WebUI 串起来的主程序。

消息的完整生命周期：

  NapCat ──WS──► OneBotServer._dispatch
                      │
                      ├─ meta_event → 心跳/生命周期（不进流水线）
                      └─ 业务事件  → adapter.parse → Event
                                        │
                                        ▼
                                    Pipeline
                      ┌─────────┬───────┼────────┐
                   限流      白名单   唤醒判定   指令解析
                                        │
                                        ▼
                                    Bus.dispatch → 插件
                                        │
                                        ▼
                              ctx.reply → send_group_msg → NapCat → QQ 群
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any

from . import event as ev_mod
from . import message as msg
from . import perceive
from . import tasks
from .agent import Agent
from .bus import BUS
from .config import Config
from .context import Context
from .diary import Diary
from .hobbies import Hobbies
from .llm import LLMClient
from .log import log
from .memory import Memory
from .memes import MemeBook
from .longmem import LongMemory
from .onebot import ActionError, Connection, OneBotServer
from .persona import Persona
from .people import PersonBook
from .spice import Spice
from .plugin import PluginManager
from .proactive import Proactive
from .qzone import Qzone
from .reminders import ReminderService, ReminderStore
from .safety import SafetyGuard
from .stickers import StickerBook
from .album import Album
from .selfedit import SelfEditor
from .store import Store
from .emotion import Emotion
from .libido import Libido
from .topic import Mood, TopicTracker
from .tools import ToolBox

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Bot:
    def __init__(self, base_dir: str = BASE_DIR):
        self.base_dir = base_dir
        self.config = Config(os.path.join(base_dir, "data", "config.json"))
        self.store = Store(os.path.join(base_dir, "data", "sessions.json"),
                           max_turns=int(self.config.get("chat.context_turns", 8)),
                           max_messages=int(self.config.get("chat.context_keep",
                                            self.config.get("chat.context_messages", 300))))
        self.llm = LLMClient(self.config)
        self.safety = SafetyGuard(self)

        # 人设 / 记忆 / 表情包 / 空间 / 工具 —— 都是纯本地文件，随时可改
        self.persona = Persona(base_dir, self.config)
        self.memory = Memory(base_dir, self.config)
        self.memes = MemeBook(os.path.join(base_dir, "data", "memes.json"), self.config)
        self.longmem = LongMemory(base_dir, self.config, self.memory)
        self.diary = Diary(base_dir, self.config)
        self.hobbies = Hobbies(base_dir, self.config)
        self.reminders = ReminderStore(os.path.join(base_dir, "data", "reminders.json"))
        self.reminder_service = ReminderService(self)
        self._store_flush_task: asyncio.Task | None = None
        self._compress_locks: dict[str, asyncio.Lock] = {}
        self.people = PersonBook(base_dir, self.config)
        self.spice = Spice(os.path.join(base_dir, "data", "spice.json"),
                           default=int(self.config.get("spice.default", 0)))
        self.stickers = StickerBook(base_dir, self.config)
        # 图库：带描述的图，专门给「不用再看一遍图」用（跟表情包分开）
        self.album = Album(base_dir, self.config)
        # 自主改提示词（默认关，见 selfedit.py 的三条硬约束）
        self.selfeditor = SelfEditor(self)
        self.qzone = Qzone(self)
        from .account import AccountActions
        self.account = AccountActions(self)
        # Initialize the dedicated account-action owner list from the existing Qzone owner list once.
        if not self.config.get("account.owner_ids", []):
            existing_owners = list(self.config.get("qzone.owner_ids", []) or [])
            if existing_owners:
                self.config.set("account.owner_ids", existing_owners)
        self.toolbox = ToolBox(self)
        self.agent = Agent(self)

        # 读空气 / 心情 / 主动说话
        self.topic = TopicTracker(self.config.get("topic.history_size", 30),
                                  os.path.join(base_dir, "data", "topic.json"))
        self.mood = Mood(os.path.join(base_dir, "data", "mood.json"))
        self.emotion = Emotion(os.path.join(base_dir, "data", "emotion.json"), self.config)
        # 心情不再单独漂移，改成从八维情绪推导，免得两套状态各说各话
        if self.config.get("emotion.enabled", True):
            self.mood.bind_emotion(self.emotion)
        self.libido = Libido(os.path.join(base_dir, "data", "libido.json"), self.config)
        self.proactive = Proactive(self)

        self.plugins = PluginManager(
            os.path.join(base_dir, "plugins"),
            os.path.join(base_dir, "data", "plugin_state.json"),
        )
        self.server = OneBotServer(
            host=self.config.get("server.host", "0.0.0.0"),
            port=int(self.config.get("server.port", 6199)),
            path=self.config.get("server.path", "/ws"),
            access_token=self.config.get("server.access_token", ""),
            on_event=self._on_event,
        )
        self.started_at = time.time()
        self.stats = {"recv": 0, "sent": 0, "errors": 0}
        self.recent_events: deque[dict] = deque(maxlen=100)
        self._rate: dict[str, deque] = {}
        # 自己最后说了什么（撤回 / 自检重复用）
        self.last_msg_id: dict[str, int] = {}
        self.last_msg_text: dict[str, str] = {}
        # 每个会话收到过多少条**别人**的消息。
        # 用来判断「我这句还没说完，群里是不是已经有人接话了」。
        self.inbound: dict[str, int] = {}

    # ------------------------------------------------------------ 生命周期
    async def start(self):
        self.plugins.reload_all()
        # 云同步：开启 sync.auto_pull_on_start 才后台拉一次；失败只告警，不影响启动
        try:
            from . import cloudsync
            cloudsync.start_auto_pull(self.config, self.base_dir)
        except Exception as e:
            log.warn(f"云同步启动拉取调度失败（不影响启动）：{type(e).__name__} {e}")
        # 实时同步：sync.mode=realtime 时起后台线程（本地变化防抖上传 + 远端轮询拉取）
        try:
            from . import sync_realtime
            self.realtime_sync = sync_realtime.start_realtime(self.config, self.base_dir)
        except Exception as e:
            self.realtime_sync = None
            log.warn(f"实时同步启动调度失败（不影响启动）：{type(e).__name__} {e}")
        # 本地 TTS 服务不在就后台拉起一次；不 await，别挡住启动
        try:
            from . import tts
            asyncio.create_task(tts.ensure_server(self.config), name="tts_ensure")
        except Exception as e:
            log.warn(f"TTS 自启动调度失败：{type(e).__name__} {e}")
        if not self._store_flush_task or self._store_flush_task.done():
            self._store_flush_task = asyncio.create_task(self._flush_store_loop(), name="store-flush")
        try:
            await self.longmem.maintain(self)
        except Exception as e:
            log.warn(f"長期記憶維護失敗：{type(e).__name__} {e}")
        await self.server.start()
        await self.proactive.start()
        await self.reminder_service.start()
        # 定时刷 B 站（tools.bili_scroll_times，如 12:40 / 20:00）；没配时间就自己退掉
        try:
            from . import bili_scroll as _bili_scroll
            self._bili_scroll_task = asyncio.create_task(
                _bili_scroll.scheduler(self), name="bili-scroll")
        except Exception as e:
            log.warn(f"B站定时刷启动失败：{type(e).__name__} {e}")
        # 自动发 B 站动态（tools.bili_post_times）；默认关，没配时间就自己退掉
        try:
            from . import bili_post as _bili_post
            self._bili_post_task = asyncio.create_task(
                _bili_post.scheduler(self), name="bili-post")
        except Exception as e:
            log.warn(f"B站自动发动态启动失败：{type(e).__name__} {e}")

    async def _flush_store_loop(self):
        interval = max(0.1, float(self.config.get("store.flush_interval", 2.0) or 2.0))
        while True:
            await asyncio.sleep(interval)
            try:
                self.store.flush()
            except Exception as e:
                log.warn(f"会话存储落盘失败：{type(e).__name__} {e}")

    async def stop(self):
        # 部分测试用 Bot.__new__ 造壳，没走 __init__，这里容错一下
        刷站 = getattr(self, "_bili_scroll_task", None)
        if 刷站 is not None:
            刷站.cancel()
            try:
                await 刷站
            except (asyncio.CancelledError, Exception):
                pass
            self._bili_scroll_task = None
        发动态 = getattr(self, "_bili_post_task", None)
        if 发动态 is not None:
            发动态.cancel()
            try:
                await 发动态
            except (asyncio.CancelledError, Exception):
                pass
            self._bili_post_task = None
        reminder_service = getattr(self, "reminder_service", None)
        if reminder_service is not None:
            await reminder_service.stop()
        await self.proactive.stop()
        if self._store_flush_task:
            self._store_flush_task.cancel()
            try:
                await self._store_flush_task
            except asyncio.CancelledError:
                pass
            self._store_flush_task = None
        await tasks.drain(5)
        self.mood.save()
        self.topic.save(force=True)
        await self.server.stop()
        self.store.flush(force=True)
        memes = getattr(self, "memes", None)
        if memes is not None:
            memes.flush()

    def conn_for(self, self_id: int | None = None) -> Connection | None:
        return self.server.get(self_id)

    def clear_session(self, session_id: str):
        """清除会话聊天与话题历史，以及依会话维护的短期状态。"""
        self.store.clear(session_id)
        self.topic.clear(session_id)
        self.inbound.pop(session_id, None)
        self.last_msg_id.pop(session_id, None)
        self.last_msg_text.pop(session_id, None)

    # ------------------------------------------------------------ 事件入口
    async def _on_event(self, conn: Connection, raw: dict):
        ev = ev_mod.parse(raw)
        if ev is None:
            return
        self.stats["recv"] += 1
        if ev.type == "message" and ev.user_id == ev.self_id:
            return
        # 记一下「别人又说话了」——分段回复时会用它判断要不要说完
        if (ev.type == "message" and ev.user_id != ev.self_id
                and str(ev.plain or "").strip()):
            self.inbound[ev.session_id] = self.inbound.get(ev.session_id, 0) + 1

        self.recent_events.append({
            "ts": time.strftime("%H:%M:%S"),
            "post_type": ev.post_type,
            "summary": self._summarize(raw),
        })

        ctx = Context(event=ev, bot=self, config=self.config)

        # --- 流水线 1：白名单 / 黑名单
        if not self._pass_filter(ev):
            return

        # --- 流水线 2：唤醒判定 & 指令解析
        if ev.type == "message":
            self._prepare_text(ctx, ev)

        # --- 读空气：不管说不说话，先记下群里发生了什么
        if ev.type == "message" and getattr(ev, "is_group", False):
            self.topic.seen(ev.session_id, ev.sender_name, ev.plain,
                            is_self=(ev.user_id == ev.self_id))

        # --- 全量入库：300 条上下文靠的是「每条都记」，不是「回过的才记」
        if ev.type == "message" and ev.user_id != ev.self_id:
            body = ev.plain.strip()
            if body:
                self.store.append(ev.session_id, "user", body,
                                  who=ev.sender_name,
                                  ts=float(getattr(ev, "time", time.time())),
                                  uid=ev.user_id)

        # --- 流水线 3：群聊旁听不依赖 @ 或唤醒词
        ctx.state["group_observe"] = bool(
            ev.type == "message" and ev.is_group and not ctx.matched_command
            and self.config.get("chat.reply_mode", "at_or_wake") != "never")
        will_speak = bool(ctx.matched_command or ctx.state.get("wake")
                          or getattr(ev, "to_me", False))
        # 指令保留原有限流；普通旁听在模型决定开口后再计发送额度。
        if ev.type == "message" and ev.is_group and ctx.matched_command:
            if not self._pass_rate(ev):
                log.debug(f"限流丢弃：{ev.session_id}")
                return
        # 情绪：旁聽的群消息用關鍵詞即時更新（不花模型）；被叫到才讓判官仔細判斷。
        feel = None
        if (ev.type == "message" and not ctx.matched_command
                and getattr(self, "emotion", None) is not None
                and self.config.get("emotion.enabled", True)
                and (will_speak or ctx.state["group_observe"])):
            feel = asyncio.create_task(self._feel(ev, use_judge=will_speak))
        if ev.type == "message" and (will_speak or ctx.state["group_observe"]):
            try:
                await perceive.enrich(self, conn, ev)
                # 引用机器人自己说过的话 = 在跟它说话（enrich 之后才知道引用的是谁）
                if getattr(ev, "quoted_is_self", False) and not ctx.state.get("wake"):
                    ctx.state["wake"] = True
                    ev.to_me = True
                await self._recall_person(ev)
            except Exception as e:
                log.warn(f"感知层失败：{type(e).__name__} {e}")
        if feel is not None:
            await feel

        # --- 流水线 5：分发
        await BUS.dispatch(ctx)

        # --- 收尾：心情随对话漂移，攒够了就压缩旧对话
        if ev.type == "message" and (ctx.state.get("ai_spoke")
                                      or (ctx.matched_command and will_speak)):
            if getattr(ev, "to_me", False):
                self.mood.on_mentioned()
            else:
                self.mood.on_chatted()
            self.mood.save()
        if ev.type == "message":
            tasks.spawn(self._maybe_compress(ev.session_id), name="compress")

    async def _feel(self, ev, *, use_judge: bool):
        """过一遍情绪，回复时就用更新后的情绪。永不抛异常。"""
        window = getattr(self.store, "window", None)
        rows = window(ev.session_id, 6) if window else []
        context = "\n".join(f"{r.get('who') or ''}：{str(r.get('content') or '')[:60]}"
                            for r in rows[:-1][-4:])
        applied = await self.emotion.update(ev.plain or "", config=self.config,
                                            context=context, addressed=use_judge,
                                            use_judge=use_judge)
        if applied:
            log.debug(f"情绪变化 {ev.session_id}：{applied}")

    async def _recall_person(self, ev):
        """把说话人的档案挂上去——只用来说明他是个什么样的人，不还原具体经历。

        注意用的是 card_for_prompt() 而不是 card()：带具体字面（口头禅）
        的行不注入，否则模型会照着复读。
        """
        # 好感：说一句话算一次互动；骂人掉得快，夸人涨得慢，久了会回落
        try:
            spice = self.spice.get(ev.session_id)
            self.libido.note(ev.plain or "", spice=spice,
                             addressed=bool(getattr(ev, "to_me", False)))
            if spice > 0:
                self.libido.save()
        except Exception as e:
            log.debug(f"性欲值更新失败：{type(e).__name__}")
        try:
            self.people.note_interaction(ev.user_id, ev.sender_name, ev.plain or "",
                                         addressed=bool(getattr(ev, "to_me", False)))
        except Exception as e:
            log.debug(f"好感更新失败：{type(e).__name__}")
        card = self.people.card_for_prompt(ev.user_id)
        tone = self.people.affinity_tone(ev.user_id)
        if card or tone:
            ev.person_card = "\n".join(x for x in (card, tone) if x)
        # 顺便攒样本，够数了后台提炼一次性格
        if self.people.observe(ev.user_id):
            tasks.spawn(self._profile_person(ev), name="profile")

    async def _profile_person(self, ev):
        """从这个人最近说过的话里提炼性格，覆盖式写入档案。"""
        try:
            # ⭐ 2026-10-24：样本按 uid 挑（原来按昵称，同名会串味、改名就失忆）
            samples = self.people.pick_samples(
                self.store.window(ev.session_id, 300), ev.user_id, ev.sender_name, 40)
            await self.people.update_profile(
                self.llm, ev.user_id, ev.sender_name, samples)
        except Exception as e:
            log.warn(f"人物档案后台任务失败：{type(e).__name__} {e}")

    async def _maybe_compress(self, session_id: str):
        """把溢出的旧对话压成摘要，让它记得「聊过什么」。"""
        lock = self._compress_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._compress_session(session_id)

    async def _compress_session(self, session_id: str):
        if session_id not in self.store.data:
            return
        if not self.store.needs_compress(session_id):
            return
        rows = self.store.take_archive(session_id)
        if not rows:
            return
        old = self.store.summary(session_id)
        recent = self.store.turn_summary(session_id)
        lines = []
        for r in rows:
            who = "我" if r.get("role") == "assistant" else "对方"
            lines.append(f"{who}：{str(r.get('content'))[:200]}")
        body = "\n".join(lines)[-4000:]

        prompt = (
            "把下面这段 QQ 群聊天压缩成一段摘要，给后面接话用。\n"
            "保留：在聊什么、结论是什么、谁说过什么关键的话、有什么约定。\n"
            "丢掉：寒暄、重复、表情。\n"
            "输出不超过 150 字，直接给摘要正文，不要前言；避免重复最近小结中的内容。\n\n"
            + (f"【已有摘要】\n{old}\n\n" if old else "")
            + (f"【最近几轮小结（避免重复）】\n{recent}\n\n" if recent else "")
            + f"【新对话】\n{body}"
        )
        try:
            text = await self.llm.chat([{"role": "user", "content": prompt}])
            text = (text or "").strip()
            if not text:
                self.store.restore_archive(session_id, rows)
                return
            self.store.set_summary(session_id, text)
            log.info(f"对话已压缩 {session_id}：{text[:60]}")
        except Exception as e:
            self.store.restore_archive(session_id, rows)
            log.warn(f"压缩失败（已还原）：{type(e).__name__} {e}")

    # ------------------------------------------------------------ 流水线
    def _pass_filter(self, ev) -> bool:
        """消息准入：群黑名单优先，用户与群白名单按 AND 组合。

        每个空白名单只放开自己的维度；私聊不检查群白名单。
        非 message 事件沿用原有放行规则。account.owner_ids 仅供主人专属
        操作授权，不是这里的通行证，也不能越过群黑名单。
        """
        if ev.type != "message":
            return True
        uid = str(getattr(ev, "user_id", 0))
        gid = str(getattr(ev, "group_id", "") or "")
        users = [str(x) for x in self.config.get("whitelist.user_ids", [])]
        groups = [str(x) for x in self.config.get("whitelist.group_ids", [])]
        blocked = [str(x) for x in self.config.get("whitelist.blacklist_group_ids", [])]
        if gid and gid in blocked:
            return False
        if users and uid not in users:
            return False
        if groups and gid and gid not in groups:
            return False
        return True

    def _pass_rate(self, ev) -> bool:
        limit = int(self.config.get("chat.rate_limit_per_min", 15))
        if limit <= 0:
            return True
        slot = f"{ev.group_id}"
        now = time.time()
        dq = self._rate.setdefault(slot, deque())
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    def _prepare_text(self, ctx: Context, ev):
        """算出去掉唤醒词/指令前缀后的文本，并标记这条消息算不算「叫了机器人」。

        注意：这里不决定要不要吞掉事件——指令和关键词插件在没被 @ 时也该能工作，
        真正决定「平时聊不聊天」的是 ai_chat 插件里的 wake 判断。
        """
        text = ev.plain.strip()
        prefixes = list(self.config.get("bot.command_prefixes", ["/"]))
        commands = {c for h in BUS.handlers if h.kind == "command" and h.enabled
                    for c in (h.commands + h.aliases)}
        allow_no_prefix = bool(self.config.get("bot.allow_no_prefix", True))

        matched_cmd = ""
        rest = text
        for p in prefixes:
            if p and text.startswith(p):
                tail = text[len(p):]
                candidate = tail.split()[0] if tail.strip() else ""
                if candidate in commands:
                    matched_cmd = candidate
                    rest = tail[len(candidate):]
                break
        if not matched_cmd and allow_no_prefix:
            first = text.split()[0] if text.strip() else ""
            if first in commands:
                matched_cmd = first
                rest = text[len(first):]

        ctx.matched_command = matched_cmd
        ctx.raw_text = rest.strip()
        ctx.state["wake"] = self._is_wake(ev)

    def _is_wake(self, ev) -> bool:
        mode = self.config.get("chat.reply_mode", "at_or_wake")
        if mode == "always":
            return True
        if mode == "never":
            return False
        if ev.to_me:
            return True
        if mode == "at_only":
            return False
        words = self.config.get("bot.wake_words", [])
        plain = getattr(ev, "plain", "")
        return any(w and w in plain for w in words)

    # ------------------------------------------------------------ 工具
    def _summarize(self, raw: dict) -> str:
        pt = raw.get("post_type")
        if pt in ("message", "message_sent"):
            sender = raw.get("sender") or {}
            name = sender.get("card") or sender.get("nickname") or raw.get("user_id")
            body = msg.to_plain(msg.normalize(raw.get("message")),
                                self_id=int(raw.get("self_id") or 0))[:60]
            where = f"群{raw.get('group_id')}" if raw.get("group_id") else "私聊"
            return f"{where} {name}: {body}"
        if pt == "notice":
            return f"{raw.get('notice_type')} 群{raw.get('group_id', '')} 用户{raw.get('user_id', '')}"
        if pt == "meta_event":
            return f"meta:{raw.get('meta_event_type')}:{raw.get('sub_type', '')}"
        return str(raw)[:80]

    def status(self) -> dict:
        conns = []
        for c in self.server.connections.values():
            conns.append({
                "self_id": c.self_id,
                "nickname": c.nickname,
                "role": c.role,
                "ready": c.ready,
                "online_sec": int(time.time() - c.connected_at),
                "last_alive": int(time.time() - c.alive_at),
            })
        return {
            "uptime": int(time.time() - self.started_at),
            "connections": conns,
            "handlers": len(BUS.handlers),
            "plugins": len(self.plugins.loaded),
            "stats": dict(self.stats),
            # provider=dsh 时不用 api_key（大脑用 DSH 的模型凭据），不能显示成 mock。
            "llm_provider": str(self.config.get("llm.provider", "") or ""),
            "llm_mock": (str(self.config.get("llm.provider", "") or "").strip().lower() != "dsh"
                         and not bool(self.config.get("llm.api_key", ""))),
            "llm_model": self.config.get("llm.model", ""),
            "sessions": len(self.store.sessions()),
            "mood": {"score": round(self.mood.score, 1), "label": self.mood.label},
            "emotion": self.emotion.blend(with_values=True),
            "libido": {"value": round(self.libido.value, 1), "label": self.libido.label()},
            "proactive": self.proactive.snapshot(),
        }

    async def manual_send(self, target: str, message: str, is_group: bool = True) -> dict:
        conn = self.conn_for()
        if conn is None:
            raise ActionError("没有在线的协议端，请先启动 NapCat 并连上来")
        self.stats["sent"] += 1
        if is_group:
            return await conn.send_message(msg.normalize(message), group_id=int(target))
        return await conn.send_message(msg.normalize(message), user_id=int(target))
