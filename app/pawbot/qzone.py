"""QQ 空间：发 / 读 / 改 / 删 / 看留言 / 回留言。

走 NapCat 的**扩展接口**（不属于 OneBot 标准），所以有一个前提必须说清楚：

    ⚠ 空间写接口没有可靠的通用探测动作。写操作只能使用显式配置的后端动作，
      通过授权、额度预留和回执验证后最多调用一次；不猜测 like/repost 能力。
    读接口可配置并探测，失败时返回明确错误，不伪造数据。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import time

from .log import log

# 发说说：这是原有代码一直在用的，确认可用
动作_发 = "send_qzone_msg"

# 其余动作：默认值只是「先试试这些名字」，实际以探测为准，可在 config 里覆盖
默认动作 = {
    "list": "get_qzone_msg_list",       # 读自己的说说列表
    "detail": "get_qzone_msg",          # 读单条说说
    "comments": "get_qzone_comments",   # 读留言
    # Mutations have no guessed fallback; use only configured backend actions.
}


def _取(d, *keys, default=None):
    """从 NapCat 的嵌套回包里挖字段（不同版本层级不一样，能挖到就算）。"""
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        for v in d.values():
            got = _取(v, *keys, default=None)
            if got is not None:
                return got
    elif isinstance(d, list):
        for v in d:
            got = _取(v, *keys, default=None)
            if got is not None:
                return got
    return default


def _response_ok(data) -> bool:
    """Accept decoded OneBot data, but never turn explicit failures into success.

    Connection.call already checks the wire status/retcode and returns data (an
    empty dict is a valid acknowledged success). Test/custom adapters may return
    an envelope, so validate those flags too. None/booleans are not receipts.
    """
    if not isinstance(data, dict):
        return False
    if data.get("ok") is False or data.get("success") is False:
        return False
    if "status" in data and data["status"] not in ("ok", "success"):
        return False
    if "retcode" in data and (isinstance(data["retcode"], bool) or
                              str(data["retcode"]) != "0"):
        return False
    nested = data.get("data")
    return not isinstance(nested, dict) or _response_ok(nested)


class AutonomyCapability:
    """由 Qzone 运行时签发、绑定到具体实例的自主操作能力。

    它不是主人身份，也不能通过普通消息权限检查：只证明「这个运行时的
    life.autonomy 被允许代表自己安静地读写空间」。Qzone 每次使用都会重新
    检查 qzone.enabled / qzone.proactive_enabled，配置关掉立即失效。
    """

    __slots__ = ("_qzone", "owner")

    def __init__(self, qzone: "Qzone", owner: str):
        self._qzone = qzone
        self.owner = str(owner or "autonomy")[:80]

    def __repr__(self) -> str:  # 不泄漏内部对象和任何凭证
        return f"<AutonomyCapability owner={self.owner!r}>"


class Qzone:
    def __init__(self, bot):
        self.bot = bot
        self.path = os.path.join(bot.base_dir, "data", "qzone.json")
        self.records: list[dict] = []
        self._支持: dict[str, bool] = {}      # 动作 → 后端支不支持
        self._mutation_lock = asyncio.Lock()
        self._state_ok = True
        self._autonomy_caps: set[AutonomyCapability] = set()
        self._load()

    # ---------------------------------------------------------- 配置
    @property
    def enabled(self) -> bool:
        return self.bot.config.get("qzone.enabled", False) is True

    def _cfg(self, key, default=None):
        return self.bot.config.get(f"qzone.{key}", default)

    def _动作(self, 名: str) -> str:
        return str(self._cfg(f"actions.{名}", "") or 默认动作.get(名, ""))

    # ---------------------------------------------------------- 记录
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list) or any(
                    not isinstance(r, dict) or not isinstance(r.get("day"), str)
                    or isinstance(r.get("ts"), bool)
                    or not isinstance(r.get("ts"), (int, float))
                    or not math.isfinite(r["ts"]) or r["ts"] < 0
                    for r in records):
                raise ValueError("invalid mutation ledger")
            self.records = records
        except FileNotFoundError:
            pass
        except Exception as exc:
            self._state_ok = False
            log.warn(f"QQ 空间记录读取失败，禁止写操作：{type(exc).__name__}")

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        # Never truncate today's quota; retain recent history across midnight.
        records = [r for r in self.records[:-200] if r.get("day") == day] + self.records[-200:]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=os.path.dirname(self.path), delete=False) as f:
            tmp = f.name
            try:
                json.dump(records, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            except BaseException:
                f.close()
                os.unlink(tmp)
                raise
        try:
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def today_count(self) -> int:
        day = time.strftime("%Y-%m-%d")
        return sum(1 for r in self.records if r.get("day") == day)

    # ---------------------------------------------------------- 权限
    def check_permission(self, ctx, *, mutation=False) -> tuple[bool, str]:
        """Space permissions never bypass message admission or a configured owner list.

        Passive reads do not spend the mutation quota. Legacy enforce_limits is
        retained in configuration for migration, not used as an authorization bypass.
        """
        if not self.enabled:
            return False, "QQ 空间功能没开（config 里 qzone.enabled=false）"
        ev = getattr(ctx, "event", None)
        uid = str(getattr(ev, "user_id", "") or "")
        gid = str(getattr(ev, "group_id", "") or "")
        if (getattr(ev, "type", None) != "message"
                or not uid.isascii() or not uid.isdecimal() or int(uid) <= 0):
            return False, "空间操作需要可验证的当前消息用户"
        # Keep the exact reject-priority/AND semantics of core._pass_filter,
        # including for callers outside the regular message pipeline.
        cfg = self.bot.config
        def ids(raw):
            if not isinstance(raw, (list, tuple)):
                raise ValueError("ID lists must be arrays")
            return {str(x).strip() for x in raw if str(x).strip()}

        try:
            blocked = ids(cfg.get("whitelist.blacklist_group_ids", []))
            users = ids(cfg.get("whitelist.user_ids", []))
            groups = ids(cfg.get("whitelist.group_ids", []))
            owners = ids(self._cfg("owner_ids", []))
        except (TypeError, ValueError):
            return False, "白名单或 qzone.owner_ids 配置无效"
        if (gid and gid in blocked) or (users and uid not in users) or (groups and gid and gid not in groups):
            return False, "当前用户或群未通过白名单准入"
        admission = getattr(self.bot, "_pass_filter", None)
        if callable(admission) and not admission(ev):
            return False, "当前消息未通过白名单准入"
        if not owners or uid not in owners:
            action = "写入" if mutation else "读取"
            return False, f"只有 qzone.owner_ids 中明确列出的主人可以{action}空间"
        if mutation:
            if not self._state_ok:
                return False, "空间操作记录不可用，禁止写操作"
            try:
                limit = self._cfg("daily_limit", 3)
                if isinstance(limit, bool) or int(limit) != float(limit) or int(limit) <= 0:
                    raise ValueError("invalid daily limit")
            except (TypeError, ValueError, OverflowError):
                return False, "qzone.daily_limit 必须是正整数"
            if self.today_count() >= int(limit):
                return False, f"今天的空间操作已到上限（qzone.daily_limit={int(limit)}）"
        return True, ""

    def _read_permission(self, ctx) -> tuple[bool, str]:
        ok, why = self.check_permission(ctx)
        if not ok:
            return ok, why
        ev = ctx.event
        if (getattr(ev, "message_type", None) != "private"
                or getattr(ctx, "is_group", False) or getattr(ev, "is_group", False)
                or getattr(ev, "group_id", None)):
            return False, "私人空间动态和留言只能在授权私聊中读取，不能传到群聊"
        return True, ""

    # ---------------------------------------------------------- 探测
    async def _探测(self, 名: str) -> bool:
        """第一次调用前试一下这个动作在不在。不通就记下来，之后直接降级。"""
        # 写操作不能拿真实接口做无参数探测：一次请求可能就产生副作用。
        # 支持情况只由经过授权的真实调用及其回执更新。
        if 名 in {"post", "edit", "delete", "reply", "like", "repost"}:
            return self._支持.get(名, False)
        if 名 in self._支持:
            return self._支持[名]
        动作 = self._动作(名)
        if not 动作:
            self._支持[名] = False
            return False
        conn = self.bot.conn_for()
        if conn is None:
            return False      # 没连上不算「不支持」，下次再试
        try:
            data = await conn.call(动作, timeout=8)
            self._支持[名] = _response_ok(data)
        except Exception as e:
            self._支持[名] = False
            log.info(f"QQ 空间接口 {动作}（{名}）不可用，功能降级：{type(e).__name__} {e}")
        return self._支持[名]

    def 支持情况(self) -> dict:
        return dict(self._支持)

    # ---------------------------------------------------------- 自主 capability
    def _autonomy_gates(self) -> bool:
        """自主读写只沿显式开启的空间功能，且必须允许主动行为。"""
        return (self.bot.config.get("qzone.enabled", False) is True
                and self.bot.config.get("qzone.proactive_enabled", False) is True)

    def issue_autonomy_capability(self, owner: str = "autonomy") -> AutonomyCapability | None:
        """签发给自主运行时的专用能力；未显式开启时返回 None。"""
        if not self._autonomy_gates():
            return None
        capability = AutonomyCapability(self, owner)
        self._autonomy_caps.add(capability)
        return capability

    def _check_autonomy(self, capability) -> tuple[bool, str]:
        if not isinstance(capability, AutonomyCapability):
            return False, "缺少空间自主操作能力"
        if capability._qzone is not self or capability not in self._autonomy_caps:
            return False, "空间自主操作能力无效"
        if not self._autonomy_gates():
            return False, "空间自主操作未开启（qzone.enabled / proactive_enabled）"
        return True, ""

    async def autonomy_read(self, capability, limit: int = 5, *,
                            source: str = "own",
                            include_comments: bool = False) -> dict:
        """自主读取自己的说说及其留言；只有显式 feed 配置时才读好友流。

        不经过主人消息上下文，也不把私人内容伪装成 owner 视角。
        """
        ok, why = self._check_autonomy(capability)
        if not ok:
            return {"ok": False, "reason": why, "items": []}
        source = str(source or "own").strip().lower()
        if source == "feed":
            data = await self.autonomy_feed(capability, limit=limit)
            if data.get("ok"):
                data["source"] = "feed"
            return data
        if source != "own":
            return {"ok": False, "items": [],
                    "reason": f"未知的空间来源：{source}"}
        data = await self._read_own(limit)
        if not data.get("ok"):
            return data
        data["source"] = "own"
        if include_comments:
            for row in data.get("items") or []:
                if not isinstance(row, dict) or not row.get("tid"):
                    continue
                detail = await self._read_comments_raw(str(row["tid"]), limit=10)
                if detail.get("ok"):
                    row["comments"] = detail.get("comments") or []
                    if detail.get("content"):
                        row["comment_context"] = detail["content"]
        return data

    async def autonomy_post(self, capability, content: str,
                            images: list[str] | None = None) -> dict:
        """自主发布自己的说说；账本记 autonomy 而不是伪造主人 ID。"""
        ok, why = self._check_autonomy(capability)
        if not ok:
            return {"ok": False, "reason": why}
        text = str(content or "").strip()
        if not text:
            return {"ok": False, "reason": "空间正文不能为空"}
        result = await self._mutate(
            "post", {"content": text, "images": images or [],
                     "ugc_right": int(self._cfg("ugc_right", 1))},
            autonomy=capability)
        if not result.get("ok"):
            return result
        data = result.get("data") or {}
        return {"ok": True, "tid": str(_取(data, "tid", "id", default="") or ""),
                "data": data}

    async def autonomy_feed(self, capability, limit: int = 10) -> dict:
        """好友信息流只在配置了明确后端动作时可用；不猜测默认动作名。"""
        ok, why = self._check_autonomy(capability)
        if not ok:
            return {"ok": False, "reason": why, "items": []}
        action = str(self._cfg("actions.feed", "") or "").strip()
        if not action:
            return {"ok": False, "items": [],
                    "reason": "未配置已确认的 qzone.actions.feed 好友动态接口"}
        conn = self.bot.conn_for()
        if conn is None:
            return {"ok": False, "items": [], "reason": "协议端没连上"}
        try:
            data = await conn.call(action, timeout=15,
                                   **({"count": max(1, int(limit or 10))}
                                      if limit else {}))
        except Exception as exc:
            return {"ok": False, "items": [],
                    "reason": f"读好友动态失败：{type(exc).__name__} {exc}"}
        if not _response_ok(data):
            return {"ok": False, "items": [], "reason": "后端没有确认读取成功"}
        raw = _取(data, "feeds", "list", "msgs", default=[]) or []
        if isinstance(raw, dict):
            raw = _取(raw, "feeds", "list", default=[]) or []
        items = []
        for row in (raw if isinstance(raw, list) else [])[:max(1, int(limit or 10))]:
            if not isinstance(row, dict):
                continue
            items.append({
                "tid": str(_取(row, "tid", "id", "unikey", default="") or ""),
                "content": str(_取(row, "content", "text", "summary", default="") or "")[:500],
                "nick": str(_取(row, "nickname", "name", "uin", default="") or ""),
                "ts": _取(row, "created_time", "time", "ts", default=0) or 0,
            })
        return {"ok": True, "items": items, "count": len(items)}

    async def autonomy_interact(self, capability, name: str, *,
                                tid: str = "", comment_id: str = "",
                                content: str = "") -> dict:
        """reply/like/repost 逐项走显式配置的真实后端，并复用写账本。

        没有真实 tid（reply 还要 comment_id）就不调用后端；配置缺失明确跳过。
        """
        ok, why = self._check_autonomy(capability)
        if not ok:
            return {"ok": False, "reason": why}
        action_name = str(name or "").strip().lower()
        if action_name not in {"reply", "like", "repost"}:
            return {"ok": False,
                    "reason": "只支持 reply / like / repost 三种空间互动"}
        tid = str(tid or "").strip()
        if not tid:
            return {"ok": False, "reason": "空间互动缺少真实 tid"}
        comment_id = str(comment_id or "").strip()
        if action_name == "reply" and not comment_id:
            return {"ok": False, "reason": "回复留言缺少真实 comment_id"}
        if action_name == "reply" and not str(content or "").strip():
            return {"ok": False, "reason": "回复留言缺少正文"}
        action = str(self._cfg(f"actions.{action_name}", "") or "").strip()
        if not action:
            return {"ok": False,
                    "reason": f"未配置已确认的 qzone.actions.{action_name} 接口"}
        params: dict = {"tid": tid}
        if comment_id:
            params["comment_id"] = comment_id
        if content:
            params["content"] = str(content)
        result = await self._mutate(action_name, params, autonomy=capability)
        if not result.get("ok"):
            return result
        return {"ok": True, "data": result.get("data") or {}}

    # ---------------------------------------------------------- 发
    async def _mutate(self, name, params, *, ctx=None, autonomy=None) -> dict:
        async with self._mutation_lock:
            if autonomy is not None:
                ok, reason = self._check_autonomy(autonomy)
                actor_id = f"autonomy:{autonomy.owner}"
            else:
                ok, reason = self.check_permission(ctx, mutation=True)
                event = getattr(ctx, "event", None) if ctx is not None else None
                actor_id = str(getattr(event, "user_id", "") or "")
            if not ok:
                return {"ok": False, "reason": reason}
            if name not in {"post", "edit", "delete", "reply", "like", "repost"}:
                return {"ok": False, "reason": "不支持这个空间操作"}
            if name in {"like", "repost"} and autonomy is None:
                # 普通消息路径不能猜 like/repost 能力；只有自主 capability 才可
                # 走显式配置的互动动作。
                return {"ok": False, "reason": "普通消息路径不支持这个空间互动"}
            # Non-post mutations need an explicitly configured backend adapter.
            action = 动作_发 if name == "post" else str(self._cfg(f"actions.{name}", "") or "").strip()
            if not action:
                return {"ok": False, "reason": f"未配置已确认的 qzone.actions.{name} 接口"}
            conn = self.bot.conn_for()
            if conn is None:
                return {"ok": False, "reason": "协议端没连上"}
            # Reserve before dispatch: a timeout may hide a completed write, so every attempt spends quota.
            record = {"day": time.strftime("%Y-%m-%d"), "ts": time.time(),
                      "action": name, "actor_id": actor_id[:120],
                      "status": "pending", "tid": str(params.get("tid", "")),
                      "content": str(params.get("content", ""))[:500],
                      "images": len(params.get("images", []))}
            self.records.append(record)
            try:
                self._save()
            except Exception as exc:
                self._state_ok = False
                return {"ok": False, "reason": f"操作额度无法保存，未调用后端：{type(exc).__name__}"}
            cancelled = None
            try:
                data = await conn.call(action, timeout=15, **params)
                success = _response_ok(data)
                self._支持[name] = success
                record["status"] = "confirmed" if success else "unconfirmed"
                if success:
                    record["tid"] = str(_取(data, "tid", default=record["tid"]))
                    result = {"ok": True, "data": data}
                else:
                    result = {"ok": False, "reason": "后端没有确认写操作成功；本次尝试已计入限额"}
            except asyncio.CancelledError as exc:
                cancelled = exc
                self._支持[name] = False
                record["status"] = "unconfirmed"
                result = {"ok": False, "reason": "空间操作被取消，结果未确认；本次尝试已计入限额"}
            except Exception as exc:
                self._支持[name] = False
                record["status"] = "unconfirmed"
                result = {"ok": False, "reason": f"空间操作未确认：{type(exc).__name__} {exc}；本次尝试已计入限额"}
            try:
                self._save()
            except Exception as exc:
                self._state_ok = False
                result = {"ok": False, "reason": "空间回执已返回，但审计账本保存失败，结果不再视为已确认"}
                log.warn(f"QQ 空间回执保存失败，禁止后续写操作：{type(exc).__name__}")
            if cancelled is not None:
                raise cancelled
            return result

    async def post(self, content: str, images: list[str] | None = None, *, ctx=None) -> dict:
        result = await self._mutate("post", {"content": content, "images": images or [],
                                   "ugc_right": int(self._cfg("ugc_right", 1))}, ctx=ctx)
        if not result["ok"]:
            # Keep existing callers' exception-based failure contract.
            raise RuntimeError(result["reason"])
        return result["data"]

    # ---------------------------------------------------------- 读
    async def list_own(self, limit: int = 10, *, ctx=None) -> dict:
        """Read only into an authorized private conversation, never group context."""
        ok, why = self._read_permission(ctx)
        if not ok:
            return {"ok": False, "reason": why, "items": []}
        return await self._read_own(limit)

    async def _read_own(self, limit: int = 10) -> dict:
        """共享读取实现；调用方必须先过手动私聊权限或自主 capability。"""
        if not await self._探测("list"):
            return {"ok": False, "reason": "这个后端不支持读说说（接口探测失败）", "items": []}
        conn = self.bot.conn_for()
        if conn is None:
            return {"ok": False, "reason": "协议端没连上", "items": []}
        try:
            data = await conn.call(self._动作("list"), timeout=15)
            if not _response_ok(data):
                return {"ok": False, "reason": "后端没有确认读取成功", "items": []}
        except Exception as e:
            return {"ok": False, "reason": f"读说说失败：{type(e).__name__} {e}", "items": []}

        原 = _取(data, "list", "feeds", "msgs", default=[]) or []
        if isinstance(原, dict):
            原 = _取(原, "list", "feeds", default=[]) or []
        items = []
        for x in (原 if isinstance(原, list) else [])[:max(1, int(limit or 10))]:
            if not isinstance(x, dict):
                continue
            items.append({
                "tid": str(_取(x, "tid", "id", "unikey", default="") or ""),
                "content": str(_取(x, "content", "text", "summary", default="") or "")[:500],
                "ts": _取(x, "created_time", "time", "ts", default=0) or 0,
                "images": _取(x, "images", "pics", default=[]) or [],
            })
        return {"ok": True, "items": items, "count": len(items)}

    async def comments(self, tid: str, *, ctx=None) -> dict:
        """Read post/comments only into an authorized private conversation."""
        ok, why = self._read_permission(ctx)
        if not ok:
            return {"ok": False, "reason": why, "content": "", "comments": []}
        tid = str(tid or "").strip()
        if not tid:
            return {"ok": False, "reason": "没给 tid", "content": "", "comments": []}
        return await self._read_comments_raw(tid)

    async def _read_comments_raw(self, tid: str, limit: int = 30) -> dict:
        """共享读取实现；调用方必须先过私聊权限或自主 capability。"""
        conn = self.bot.conn_for()
        action = self._动作("comments")
        if conn is None or not action:
            return {"ok": False, "reason": "留言接口未配置或协议端没连上",
                    "content": "", "comments": []}
        try:
            data = await conn.call(action, tid=str(tid), timeout=15)
            self._支持["comments"] = _response_ok(data)
            if not self._支持["comments"]:
                return {"ok": False, "reason": "后端没有确认读取成功",
                        "content": "", "comments": []}
        except Exception as e:
            return {"ok": False, "reason": f"读留言失败：{type(e).__name__} {e}",
                    "content": "", "comments": []}

        原 = _取(data, "comments", "list", default=[]) or []
        if isinstance(原, dict):
            原 = _取(原, "comments", "list", default=[]) or []
        rows = []
        for x in (原 if isinstance(原, list) else [])[:max(1, int(limit or 30))]:
            if not isinstance(x, dict):
                continue
            rows.append({
                "id": str(_取(x, "commentid", "id", "cid", default="") or ""),
                "nick": str(_取(x, "nickname", "name", "uin", default="") or ""),
                "text": str(_取(x, "content", "text", default="") or "")[:300],
                "ts": _取(x, "create_time", "time", "ts", default=0) or 0,
            })
        return {"ok": True,
                "content": str(_取(data, "content", "summary", default="") or "")[:800],
                "comments": rows, "count": len(rows)}

    # ---------------------------------------------------------- 改 / 删
    async def edit(self, tid: str, content: str, *, ctx=None) -> dict:
        return await self._mutate("edit", {"tid": str(tid), "content": str(content)}, ctx=ctx)

    async def delete(self, tid: str, *, ctx=None) -> dict:
        return await self._mutate("delete", {"tid": str(tid)}, ctx=ctx)

    # ---------------------------------------------------------- 回留言
    async def reply(self, tid: str, comment_id: str, text: str, *, ctx=None) -> dict:
        return await self._mutate("reply", {"tid": str(tid), "comment_id": str(comment_id or ""),
                                           "content": str(text)}, ctx=ctx)
