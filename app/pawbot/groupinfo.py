"""群感知：自己待的这个群叫什么、多少人、群主/管理是谁、自己是什么身分。

只做缓存，不主动轮询。消息进来时由插件在后台调一次 ``get()``，
提示词那边只读 ``cached()``，绝不在拼 prompt 的路径上打网络。

缓存结构（每个群一份）::

    {
      "name": "测试群",
      "member_count": 42,
      "owner_id": 10001,
      "owner_name": "老王",
      "admins": [{"uid": 10002, "name": "阿明"}, ...],
      "self_role": "member",          # owner / admin / member
      "fetched_at": 1760000000.0,
    }

任何协议调用失败都不会抛出去：有旧缓存就返回旧的，没有就返回 ``{}``。
TTL 来自 ``life.groupinfo_ttl``（默认 1800 秒）。
"""

from __future__ import annotations

import asyncio
import time

from .log import log

ROLE_NAMES = {"owner": "群主", "admin": "管理员", "member": "普通成员"}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _payload(resp) -> dict:
    """OneBot 回执有时在顶层、有时在 data 里，两种都认。"""
    if not isinstance(resp, dict):
        return {}
    data = resp.get("data")
    if isinstance(data, dict):
        return data
    return resp


def _payload_list(resp) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return data
    return []


def _name_of(member: dict) -> str:
    return str(member.get("card") or member.get("nickname") or "").strip()


class GroupInfo:
    def __init__(self, ttl: float = 1800.0):
        self.ttl = float(ttl)
        self._cache: dict[int, dict] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # 拉取失败后的冷却：NapCat 报 inner_error 时别每条消息都去撞一次
        self._fail_until: dict[int, float] = {}

    # ---------------------------------------------------------- 纯缓存
    def cached(self, group_id) -> dict | None:
        """只读缓存，不判断过期、不打网络。返回 None 表示还没缓存过。"""
        gid = _int(group_id, -1)
        if gid < 0:
            return None
        entry = self._cache.get(gid)
        return entry.get("info") if entry else None

    def fetched_at(self, group_id) -> float:
        gid = _int(group_id, -1)
        entry = self._cache.get(gid)
        return float(entry.get("fetched_at") or 0.0) if entry else 0.0

    def is_fresh(self, group_id, now=None) -> bool:
        gid = _int(group_id, -1)
        if gid < 0:
            return False
        entry = self._cache.get(gid)
        if not entry:
            return False
        current = time.time() if now is None else float(now)
        return current - float(entry.get("fetched_at") or 0.0) < self.ttl

    def clear(self, group_id=None):
        if group_id is None:
            self._cache.clear()
        else:
            self._cache.pop(_int(group_id, -1), None)

    # ---------------------------------------------------------- 拉取
    async def get(self, bot, group_id, now=None) -> dict:
        """过期才刷新；失败返回旧缓存（可能已过期）或空 dict。永不抛异常。"""
        gid = _int(group_id, -1)
        if gid < 0:
            return {}
        current = time.time() if now is None else float(now)
        old = self.cached(gid)
        if old is not None and self.is_fresh(gid, current):
            return old
        # 刚失败过：冷却期内直接用旧缓存，绝不再打 NapCat
        if self._fail_until.get(gid, 0.0) > current:
            return old or {}

        lock = self._locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[gid] = lock
        async with lock:
            # 排队期间可能已经有别的任务刷过了
            old = self.cached(gid)
            if old is not None and self.is_fresh(gid, current):
                return old
            if self._fail_until.get(gid, 0.0) > current:
                return old or {}

            try:
                conn = bot.conn_for()
            except Exception:
                conn = None
            if conn is None:
                return old or {}

            try:
                info = await self._fetch(conn, gid)
            except Exception as e:
                self._fail_until[gid] = current + 1800
                log.info(f"群信息拉取失败 {gid}：{type(e).__name__} {e}；"
                         f"30 分钟内不再重试")
                return old or {}
            if not info:
                self._fail_until[gid] = current + 1800
                log.info(f"群信息为空 {gid}；30 分钟内不再重试")
                return old or {}

            self._fail_until.pop(gid, None)
            info["fetched_at"] = current
            self._cache[gid] = {"info": info, "fetched_at": current}
            return info

    async def _fetch(self, conn, gid: int) -> dict:
        """get_group_info + 成员列表过滤 + 自己的身分。

        三个调用里任何一个失败都往上抛，由 ``get()`` 统一退回旧缓存/空 dict——
        半截数据（只有群名没有管理）会让提示词里出现自相矛盾的信息。
        """
        raw = await conn.call("get_group_info", group_id=gid)
        data = _payload(raw)
        info = {
            "name": str(data.get("group_name") or data.get("name") or ""),
            "member_count": _int(data.get("member_count") or data.get("member_num")),
            "owner_id": _int(data.get("owner_id")),
            "owner_name": str(data.get("owner_name") or ""),
            "admins": [],
            "self_role": "",
        }

        # 成员列表只留群主/管理，不把几百人的列表塞进 prompt。
        member_raw = await conn.call("get_group_member_list", group_id=gid)
        members = [m for m in _payload_list(member_raw) if isinstance(m, dict)]
        if not info["member_count"]:
            info["member_count"] = len(members)
        for m in members:
            role = str(m.get("role") or "")
            uid = _int(m.get("user_id"))
            if role == "owner":
                info["owner_id"] = info["owner_id"] or uid
                info["owner_name"] = info["owner_name"] or _name_of(m)
            elif role == "admin":
                info["admins"].append({"uid": uid, "name": _name_of(m)})

        # 自己的身分以 get_group_member_info 为准；失败再退回成员列表。
        self_id = _int(getattr(conn, "self_id", 0))
        if self_id:
            me = _payload(await conn.call(
                "get_group_member_info", group_id=gid, user_id=self_id))
            info["self_role"] = str(me.get("role") or "")
            if not info["self_role"]:
                for m in members:
                    if _int(m.get("user_id")) == self_id:
                        info["self_role"] = str(m.get("role") or "")
                        break
        return info


def for_bot(bot) -> GroupInfo:
    """懒建立挂在 Bot 上的实例（不改 core.py）。"""
    gi = getattr(bot, "_groupinfo", None)
    if isinstance(gi, GroupInfo):
        return gi
    ttl = 1800.0
    try:
        cfg = getattr(bot, "config", None)
        if cfg is not None:
            ttl = float(cfg.get("life.groupinfo_ttl", 1800) or 1800)
    except Exception:
        ttl = 1800.0
    gi = GroupInfo(ttl=ttl)
    try:
        setattr(bot, "_groupinfo", gi)
    except Exception:
        pass
    return gi


def describe(info) -> str:
    """「你在「群名」里（N 人），群主是 X，管理有 A、B；你自己是普通成员」"""
    if not isinstance(info, dict) or not info:
        return ""
    name = str(info.get("name") or "").strip() or "这个群"
    count = _int(info.get("member_count"))
    text = f"你在「{name}」里"
    if count > 0:
        text += f"（{count} 人）"

    owner = str(info.get("owner_name") or "").strip()
    if owner:
        text += f"，群主是 {owner}"
    admins = []
    for item in info.get("admins") or []:
        if isinstance(item, dict):
            nm = str(item.get("name") or "").strip()
        else:
            nm = str(item or "").strip()
        if nm:
            admins.append(nm)
    if admins:
        text += "，管理有 " + "、".join(admins[:6])

    role = ROLE_NAMES.get(str(info.get("self_role") or ""), "群成员")
    text += f"；你自己是{role}"
    return text + "。"
