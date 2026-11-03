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
from . import identity
from . import message as msg
from . import paths as layout
from . import perceive
from . import shortterm
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
from .onebot import ActionError, Connection, OneBotClient, OneBotServer
from .persona import Persona
from .people import PersonBook
from .spice import Spice
from .plugin import PluginManager
from .proactive import Proactive
from .qzone import Qzone
from .relay_client import RelayClient
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


def _incoming_body(ev) -> str:
    """入站消息写进 Store 的正文：文字优先，纯图片保留 `[图片]` 占位。

    ``event.parse`` 已经把消息段转成 ``plain``（图片就是 ``[图片]``），所以正常
    情况下这里只做去空白；只有上游漏填 ``plain`` 时才按消息段重建，绝不因为
    「没有文字」把一条只有图片消息丢掉。
    """
    body = str(getattr(ev, "plain", "") or "").strip()
    if body:
        return body
    try:
        return msg.to_plain(getattr(ev, "message", None) or [],
                            self_id=getattr(ev, "self_id", 0)).strip()
    except Exception:
        return ""


def _hearing_text(value) -> str:
    """压成一行、去首尾空白；情绪上下文与判官输入共用。"""
    return " ".join(str(value or "").split())


def _has_meaning(text: str) -> bool:
    """有没有可解读的内容（字母/数字/汉字）。

    纯标点（「？？！」）不构成情绪刺激：那只是语气符号，不能让她的八维情绪
    因为一个标点被推着走。图片/语音占位里有汉字，仍然会进判官。
    """
    return any(ch.isalnum() for ch in str(text or ""))


def _feel_line(row: dict) -> str:
    """情绪上下文的一行：完整 uid + 清洗过的昵称 + 去换行正文。"""
    if not isinstance(row, dict):
        return ""
    body = _hearing_text(row.get("content"))
    if not _has_meaning(body):
        return ""
    return f"{identity.label(row)}：{body[:60]}"


def _open_config(base_dir: str, data_paths) -> Config:
    """读配置：正常在 ``state/config.json``，还没迁移的数据根就地读旧位置。

    迁移由 ``run.py`` 在 Bot 之前做完；这里只是兜底——直接跑工具脚本、
    或还没跑过迁移的老数据目录，配置不会「看起来丢了」。
    """
    state_config = data_paths.canonical("state", "config.json")
    if not os.path.isfile(state_config):
        legacy = os.path.join(base_dir, "data", "config.json")
        if os.path.isfile(legacy):
            log.warn("配置仍在旧位置 data/config.json（尚未迁移），本次就地读取；"
                     "用 run.py 启动时会自动搬进 state/")
            return Config(os.path.join(base_dir, "data", "config.json"))
    return Config(state_config)


def build_onebot_transport(config, on_event) -> OneBotServer | OneBotClient | RelayClient:
    """按 ``server.mode`` 选传输方式：reverse（默认，本机监听）/ forward（外拨中继）/
    relay（高可用后端，lease + ready 门禁 + 内容副本）。

    三者对调用方暴露同一套接口（start/stop/get/connections/on_event），
    所以 ``Bot.server`` 的既有用法不需要改。forward/relay 不会绑定本地端口。
    relay 的生命周期回调（on_acquired/on_lost/before_release）由 ``Bot`` 在
    构造时交给 ``ha_runtime.HARuntime`` 接线；这里只负责按配置构造传输。
    """
    mode = str(config.get("server.mode", "reverse") or "reverse").strip().lower()
    if mode == "forward":
        return OneBotClient.from_config(config, on_event=on_event)
    if mode == "relay":
        return RelayClient.from_config(config, on_event=on_event)
    if mode not in ("reverse", ""):
        log.warn(f"未知 server.mode={mode!r}，按 reverse 处理")
    return OneBotServer(
        host=config.get("server.host", "0.0.0.0"),
        port=int(config.get("server.port", 6199)),
        path=config.get("server.path", "/ws"),
        access_token=config.get("server.access_token", ""),
        on_event=on_event,
    )


class Bot:
    def __init__(self, base_dir: str = BASE_DIR):
        self.base_dir = base_dir
        # 数据布局：content/（可搬走）+ state/（机器状态）。数据根仍由 lifecycle 决定。
        self.paths = layout.resolve(base_dir)
        self.config = _open_config(base_dir, self.paths)
        self.store = Store(self.paths.state("sessions.json"),
                           max_turns=int(self.config.get("chat.context_turns", 8)),
                           max_messages=int(self.config.get("chat.context_keep",
                                            self.config.get("chat.context_messages", 300))))
        self.llm = LLMClient(self.config)
        self.safety = SafetyGuard(self)

        # 人设 / 记忆 / 表情包 / 空间 / 工具 —— 都是纯本地文件，随时可改
        self.persona = Persona(base_dir, self.config, paths=self.paths)
        self.memory = Memory(base_dir, self.config, paths=self.paths)
        self.memes = MemeBook(self.paths.content("memes.json"), self.config)
        self.longmem = LongMemory(base_dir, self.config, self.memory, paths=self.paths)
        self.diary = Diary(base_dir, self.config, paths=self.paths)
        self.hobbies = Hobbies(base_dir, self.config, paths=self.paths)
        self.reminders = ReminderStore(self.paths.state("reminders.json"))
        self.reminder_service = ReminderService(self)
        self._store_flush_task: asyncio.Task | None = None
        self.minecraft_resident = None
        self._compress_locks: dict[str, asyncio.Lock] = {}
        self.people = PersonBook(base_dir, self.config, paths=self.paths)
        self.spice = Spice(self.paths.state("spice.json"),
                           default=int(self.config.get("spice.default", 0)))
        self.stickers = StickerBook(base_dir, self.config, paths=self.paths)
        # 图库：带描述的图，专门给「不用再看一遍图」用（跟表情包分开）
        self.album = Album(base_dir, self.config, paths=self.paths)
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
                                  self.paths.state("topic.json"))
        self.mood = Mood(self.paths.state("mood.json"))
        self.emotion = Emotion(self.paths.state("emotion.json"), self.config)
        # 心情不再单独漂移，改成从八维情绪推导，免得两套状态各说各话
        if self.config.get("emotion.enabled", True):
            self.mood.bind_emotion(self.emotion)
        self.libido = Libido(self.paths.state("libido.json"), self.config)
        self.proactive = Proactive(self)

        self.plugins = PluginManager(
            os.path.join(base_dir, "plugins"),
            self.paths.state("plugin_state.json"),
        )
        self.server = build_onebot_transport(self.config, self._on_event)
        # 高可用（server.mode=relay）只在构造时接线一次：reverse / forward 的
        # 行为完全不变（没有 ha_runtime，也不启动任何额外循环）。
        self._ha_mode = str(getattr(self.server, "mode", "") or "").lower() == "relay"
        self._ha_stopping = False
        self.ha_runtime = self._build_ha_runtime() if self._ha_mode else None
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

    # ------------------------------------------------------------ 高可用接线
    def _build_ha_runtime(self):
        """构造 relay 模式的 HA 运行时，并把三个生命周期回调挂到 ``self.server``。

        这里只依赖 ``RelayClient`` 现有的回调契约（on_acquired/on_lost/
        before_release）；没有回调或回调失败时返回 None，relay 业务保持关闭
        （fail closed），但传输层仍可启动。reverse / forward 不会走到这里。
        """
        try:
            from .ha_runtime import HARuntime  # noqa: PLC0415 - 仅 relay 模式需要
        except Exception as e:  # noqa: BLE001 - 模块缺失时明确降级
            log.warn(f"HA 运行时不可用，relay 业务保持关闭：{type(e).__name__} {e}")
            return None
        try:
            # 调用方显式注入的副本优先；没有时 HARuntime 会按 relay 的
            # content_root/content_url 自己构造真实 ContentReplica。
            replica = getattr(self.server, "replica", None)
            runtime = HARuntime(self, self.server, replica=replica,
                                auto_attach=False)
            base_acquired = runtime.on_acquired

            async def on_acquired(client=None, lease=None):
                # 先经过 HARuntime 的真实 pull/apply/缓存刷新与 revision 门禁，
                # 再补刷 core 独有的缓存；两步都完成后才允许返回 revision，
                # RelayClient 之后才会发 ready。
                revision = await base_acquired(client, lease)
                self._refresh_ha_caches()
                return revision

            # 用包装后的回调接管；attach() 会把它写到 relay 上，并保留旧值。
            runtime.on_acquired = on_acquired
            runtime.attach()
            return runtime
        except Exception as e:  # noqa: BLE001 - 初始化失败时 relay 仍可启动但无业务
            log.warn(f"HA 运行时初始化失败，relay 业务保持关闭："
                     f"{type(e).__name__} {e}")
            return None

    def _refresh_ha_caches(self) -> dict:
        """刷新 HARuntime 未覆盖的 core 内存缓存（只读盘，绝不 flush）。

        persona / longmem / memes / hobbies / album / relations 由 HARuntime 的
        ``refresh_content_caches`` 处理；这里补上 core 自己的 people 与 stickers。
        失败只记录，不把旧缓存写回磁盘，也不静默假装刷新成功。
        """
        refreshed: list[str] = []
        errors: list[str] = []
        people = getattr(self, "people", None)
        cache = getattr(people, "_cache", None)
        if isinstance(cache, dict):
            try:
                cache.clear()
                refreshed.append("people")
            except Exception as e:  # noqa: BLE001 - 单个缓存失败不影响其它
                errors.append(f"people:{type(e).__name__}")
        stickers = getattr(self, "stickers", None)
        index = getattr(stickers, "index", None)
        if callable(index):
            try:
                # StickerBook 没有常驻索引缓存，这里做一次显式磁盘读取，
                # 保证接管后立即看到 canonical 的 stickers/index.json。
                index()
                refreshed.append("stickers")
            except Exception as e:  # noqa: BLE001
                errors.append(f"stickers:{type(e).__name__}")
        for label in errors:
            log.warn(f"HA 内容缓存刷新失败：{label}")
        return {"ok": not errors, "refreshed": refreshed, "errors": errors}

    # ------------------------------------------------------------ 生命周期
    async def start(self):
        self.plugins.reload_all()
        self._ha_stopping = False
        relay_mode = bool(getattr(self, "_ha_mode", False))
        if relay_mode:
            # relay 模式的内容只允许 HARuntime 一条通道：legacy 云同步的
            # auto_pull 与实时同步都是双写来源，必须明确不启动。
            self._auto_pull_thread = None
            self.realtime_sync = None
        else:
            # 云同步：开启 sync.auto_pull_on_start 才后台拉一次；失败只告警，不影响启动
            try:
                from . import cloudsync
                self._auto_pull_thread = cloudsync.start_auto_pull(self.config, self.base_dir)
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
        if not relay_mode:
            # longmem.maintain 会写 content；standby 阶段不能抢先改便携内容。
            try:
                await self.longmem.maintain(self)
            except Exception as e:
                log.warn(f"長期記憶維護失敗：{type(e).__name__} {e}")
        if relay_mode and self.ha_runtime is not None:
            # 先挂好内容同步循环；业务生产者仍要等 relay ready/can_emit。
            await self.ha_runtime.start()
        await self.server.start()
        if relay_mode:
            # relay 模式的生产者由 HARuntime 在 ready/can_emit 后启动；
            # 这里绝不提前启动主动发言 / 提醒 / B 站定时任务。
            if self.ha_runtime is None:
                log.warn("relay 模式下 HA 运行时不可用：业务生产者保持关闭（fail closed）")
            return
        await self.proactive.start()
        await self.reminder_service.start()
        # Minecraft remains optional; any startup failure is logged by the resident and does not affect QQ.
        await self._start_minecraft_resident()
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
        # 自动互动：回评论 / 发评论 / 发弹幕（tools.bili_auto_*）
        # 默认关（见 config.py 里 bili_auto_enabled 的说明）；关着就不起任务。
        if self.config.get("tools.bili_auto_enabled", False) is True:
            try:
                from . import bili_auto as _bili_auto
                self._bili_auto_task = asyncio.create_task(
                    _bili_auto.scheduler(self), name="bili-auto")
            except Exception as e:
                log.warn(f"自动互动启动失败：{type(e).__name__} {e}")

    async def _flush_store_loop(self):
        interval = max(0.1, float(self.config.get("store.flush_interval", 2.0) or 2.0))
        while True:
            await asyncio.sleep(interval)
            try:
                self.store.flush()
            except Exception as e:
                log.warn(f"会话存储落盘失败：{type(e).__name__} {e}")

    async def _start_minecraft_resident(self):
        if self.config.get("minecraft.enabled", False) is not True:
            return False
        try:
            from .minecraft_resident import MinecraftResident
            resident = MinecraftResident(self)
            self.minecraft_resident = resident
            started = await resident.start()
            if started is False:
                self.minecraft_resident = None
                return False
            return True
        except Exception as e:
            self.minecraft_resident = None
            log.warn(f"Minecraft resident remains offline: {type(e).__name__}")
            return False

    async def _stop_minecraft_resident(self):
        resident = getattr(self, "minecraft_resident", None)
        self.minecraft_resident = None
        if resident is not None:
            try:
                result = await resident.stop()
                if result.get("revoked") is not True:
                    log.warn("Minecraft stop did not confirm guard revocation")
            except Exception as e:
                log.warn(f"Minecraft resident stop failed: {type(e).__name__}")

    async def stop(self):
        if getattr(self, "_ha_mode", False):
            await self._stop_relay()
            return
        await self._stop_legacy()

    async def _stop_legacy(self):
        # 部分测试用 Bot.__new__ 造壳，没走 __init__，这里容错一下
        await self._stop_minecraft_resident()
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
        自动互动 = getattr(self, "_bili_auto_task", None)
        if 自动互动 is not None:
            自动互动.cancel()
            try:
                await 自动互动
            except (asyncio.CancelledError, Exception):
                pass
            self._bili_auto_task = None
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

    async def _stop_relay(self):
        """relay 模式停机：先让 HARuntime 在仍持 lease 时排空并 flush，再 bye。

        顺序由 ``RelayClient.stop`` 的回调契约保证：它先调 ``before_release``
        （停生产者 / 排空本 owner 业务 / flush 内容 / push），再发 bye。
        这里只负责先停本机 state 落盘循环，且**不**使用全局 ``tasks.drain``，
        更不在非 owner 状态下 flush 便携内容（避免旧缓存覆盖 canonical）。
        """
        # 先关 core 自己的业务入口：即使 relay 还在处理停止前的最后一帧，
        # 也不能再进入业务流水线。
        self._ha_stopping = True
        flush_task, self._store_flush_task = self._store_flush_task, None
        if flush_task is not None:
            flush_task.cancel()
            try:
                await flush_task
            except (asyncio.CancelledError, Exception):
                pass
        server = getattr(self, "server", None)
        runtime = getattr(self, "ha_runtime", None)
        try:
            if server is not None:
                await server.stop()
        finally:
            if runtime is not None:
                try:
                    await runtime.stop()
                except Exception as e:  # noqa: BLE001 - 停机清理不应阻断最后 flush
                    log.warn(f"停止 HA 运行时失败：{type(e).__name__} {e}")
            # 只保存本机状态（state）；便携内容已由 before_release 在持 lease 时处理。
            self.mood.save()
            self.topic.save(force=True)
            self.store.flush(force=True)

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
        """传输层事件入口：relay 模式先过 HA 准入。

        standby / 接管中 / 丢租约后 / fail-closed 时直接丢弃，不解析、不写
        Store、不产生业务副作用；只有在 active 且 relay 可外发时才进入原流水线。
        reverse / forward 没有 ``ha_runtime``，行为与旧版完全一致。
        """
        runtime = getattr(self, "ha_runtime", None)
        if getattr(self, "_ha_stopping", False):
            return None
        if runtime is None or not callable(getattr(runtime, "handle_event", None)):
            return await self._on_event_business(conn, raw)
        return await runtime.handle_event(conn, raw, self._on_event_business)

    async def _on_event_business(self, conn: Connection, raw: dict):
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
            body = _incoming_body(ev)
            if body:
                # uid / 真实事件时间 / 稳定 message_id 一起进 Store：
                # 活跃期要靠 message_id 精确认出「当前这条」，只靠昵称或秒级
                # 时间都可能把同一个人同一秒的两条消息混成一条。
                extra = {}
                message_id = getattr(ev, "message_id", 0)
                if message_id:
                    extra["message_id"] = message_id
                self.store.append(
                    ev.session_id, "user", body,
                    who=ev.sender_name,
                    ts=float(getattr(ev, "time", 0) or time.time()),
                    uid=ev.user_id,
                    **extra)

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
            # 登记到 tasks registry：停机时 lifecycle 才能真正取消/等待它，
            # 而不是只取消事件 worker 后留下一个还在写 Store 的孤儿任务。
            feel = tasks.spawn(self._feel(ev, use_judge=will_speak), name="feel")
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
        """过一遍情绪，回复时就用更新后的情绪。永不抛异常。

        喂给情绪层的上下文带**完整 uid + 清洗后的昵称**（`identity.label`），
        不再只给昵称/尾号；当前这条如果已经入库，用 message_id 精确剔掉，
        不让它同时出现在「上下文」和「新消息」里。纯标点消息不构成刺激，
        直接跳过，避免一个「？」就把她的心情推走。

        ``addressed`` 沿用调用方已经算好的 ``use_judge``（是否在叫她），
        这里不重新猜，也不改 emotion.update 的既有契约。
        """
        text = _hearing_text(getattr(ev, "plain", "") or "")
        if not _has_meaning(text):
            return {}
        window = getattr(self.store, "window", None)
        rows = window(ev.session_id, 6) if callable(window) else []
        try:
            history = shortterm.exclude_current_event(rows, ev)
            if (len(history) == len(rows) and rows
                    and not getattr(ev, "message_id", 0)):
                # 事件没有 message_id 可定位时，沿用旧行为：
                # 当前这条刚写进 Store，上下文里去掉最后一条。
                history = rows[:-1]
        except Exception:
            history = rows[:-1] if rows else []
        context = "\n".join(
            line for line in (_feel_line(row) for row in history[-4:]) if line)
        applied = await self.emotion.update(text, config=self.config,
                                            context=context,
                                            addressed=use_judge,
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
        result = {
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
        runtime = getattr(self, "ha_runtime", None)
        if runtime is not None and callable(getattr(runtime, "status", None)):
            try:
                result["ha"] = runtime.status()
            except Exception as e:  # noqa: BLE001 - 状态展示失败不影响主状态
                result["ha"] = {"error": type(e).__name__}
        return result

    async def manual_send(self, target: str, message: str, is_group: bool = True) -> dict:
        conn = self.conn_for()
        if conn is None:
            raise ActionError("没有在线的协议端，请先启动 NapCat 并连上来")
        self.stats["sent"] += 1
        if is_group:
            return await conn.send_message(msg.normalize(message), group_id=int(target))
        return await conn.send_message(msg.normalize(message), user_id=int(target))
