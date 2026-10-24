"""QQ 空间：发 / 读 / 改 / 删 / 看留言 / 回留言。

走 NapCat 的**扩展接口**（不属于 OneBot 标准），所以有一个前提必须说清楚：

    ⚠ 除了 `send_qzone_msg`（发说说，原来就在用），其余接口名**没有官方保证**。
      这里不猜、也不硬编码一个名字就当它能用 —— 而是：
        1) 动作名可以在 config 里改（qzone.actions.*）
        2) 第一次调用前**探测一次**，不通就标记「这个后端不支持」并降级
        3) 降级只返回一句人话，不抛异常、不返回假数据

探测结果缓存在内存里（`self._支持`），一次运行只探一次。
"""

from __future__ import annotations

import json
import os
import time

from .log import log

# 发说说：这是原有代码一直在用的，确认可用
动作_发 = "send_qzone_msg"

# 其余动作：默认值只是「先试试这些名字」，实际以探测为准，可在 config 里覆盖
默认动作 = {
    "list": "get_qzone_msg_list",       # 读自己的说说列表
    "detail": "get_qzone_msg",          # 读单条说说
    "comments": "get_qzone_comments",   # 读留言
    "delete": "delete_qzone_msg",       # 删说说
    "edit": "update_qzone_msg",         # 改说说
    "reply": "send_qzone_comment",      # 回留言
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


class Qzone:
    def __init__(self, bot):
        self.bot = bot
        self.path = os.path.join(bot.base_dir, "data", "qzone.json")
        self.records: list[dict] = []
        self._支持: dict[str, bool] = {}      # 动作 → 后端支不支持
        self._load()

    # ---------------------------------------------------------- 配置
    @property
    def enabled(self) -> bool:
        return bool(self.bot.config.get("qzone.enabled", False))

    def _cfg(self, key, default=None):
        return self.bot.config.get(f"qzone.{key}", default)

    def _动作(self, 名: str) -> str:
        return str(self._cfg(f"actions.{名}", "") or 默认动作.get(名, ""))

    # ---------------------------------------------------------- 记录
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.records = json.load(f)
            except Exception:
                self.records = []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.records[-200:], f, ensure_ascii=False, indent=2)

    def today_count(self) -> int:
        day = time.strftime("%Y-%m-%d")
        return sum(1 for r in self.records if r.get("day") == day)

    # ---------------------------------------------------------- 权限
    def check_permission(self, ctx) -> tuple[bool, str]:
        """总开关 + （可选）主人白名单 + （可选）每日上限。

        ⚠ 两道闸默认**不生效**（`enforce_limits=false`），跟之前「按要求移除」的行为一致，
          不擅自收紧。想收紧就把它打开，然后填好 `owner_ids` / `daily_limit`：
              "qzone": {"enforce_limits": true, "owner_ids": ["你的QQ号"], "daily_limit": 3}
        """
        if not self.enabled:
            return False, "QQ 空间功能没开（config 里 qzone.enabled=false）"

        if not self._cfg("enforce_limits", False):
            return True, ""

        白名单 = [str(x).strip() for x in (self._cfg("owner_ids", []) or []) if str(x).strip()]
        if 白名单:
            uid = str(getattr(getattr(ctx, "event", None), "user_id", "") or "")
            if uid not in 白名单:
                return False, "这个号不在 qzone.owner_ids 里，不能动空间"

        上限 = int(self._cfg("daily_limit", 0) or 0)
        if 上限 > 0 and self.today_count() >= 上限:
            return False, f"今天已经发了 {self.today_count()} 条，到上限了（qzone.daily_limit={上限}）"

        return True, ""

    # ---------------------------------------------------------- 探测
    async def _探测(self, 名: str) -> bool:
        """第一次调用前试一下这个动作在不在。不通就记下来，之后直接降级。"""
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
            await conn.call(动作, timeout=8)
            self._支持[名] = True
        except Exception as e:
            self._支持[名] = False
            log.info(f"QQ 空间接口 {动作}（{名}）不可用，功能降级：{type(e).__name__} {e}")
        return self._支持[名]

    def 支持情况(self) -> dict:
        return dict(self._支持)

    # ---------------------------------------------------------- 发
    async def post(self, content: str, images: list[str] | None = None) -> dict:
        conn = self.bot.conn_for()
        if conn is None:
            raise RuntimeError("协议端没连上")

        data = await conn.call(
            动作_发,
            content=content,
            images=images or [],
            ugc_right=int(self._cfg("ugc_right", 1)),   # 1=公开 4=仅自己可见
        )

        self.records.append({
            "day": time.strftime("%Y-%m-%d"),
            "ts": int(time.time()),
            "content": content[:500],
            "tid": (data or {}).get("tid", ""),
            "images": len(images or []),
        })
        self._save()
        return data

    # ---------------------------------------------------------- 读
    async def list_own(self, limit: int = 10) -> dict:
        """读自己发过的说说。不支持就降级（返回 ok=False，不抛异常）。"""
        if not await self._探测("list"):
            return {"ok": False, "reason": "这个后端不支持读说说（接口探测失败）", "items": []}
        conn = self.bot.conn_for()
        try:
            data = await conn.call(self._动作("list"), timeout=15)
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

    async def comments(self, tid: str) -> dict:
        """读某条说说下的全部留言 + 说说正文（回留言时要把这两样都给模型）。"""
        tid = str(tid or "").strip()
        if not tid:
            return {"ok": False, "reason": "没给 tid", "content": "", "comments": []}
        if not await self._探测("comments"):
            return {"ok": False, "reason": "这个后端不支持读留言（接口探测失败）",
                    "content": "", "comments": []}
        conn = self.bot.conn_for()
        try:
            data = await conn.call(self._动作("comments"), tid=tid, timeout=15)
        except Exception as e:
            return {"ok": False, "reason": f"读留言失败：{type(e).__name__} {e}",
                    "content": "", "comments": []}

        原 = _取(data, "comments", "list", default=[]) or []
        if isinstance(原, dict):
            原 = _取(原, "comments", "list", default=[]) or []
        rows = []
        for x in (原 if isinstance(原, list) else []):
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
    async def edit(self, tid: str, content: str) -> dict:
        if not await self._探测("edit"):
            return {"ok": False, "reason": "这个后端不支持改说说（接口探测失败）"}
        conn = self.bot.conn_for()
        try:
            await conn.call(self._动作("edit"), tid=str(tid), content=str(content), timeout=15)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "reason": f"改说说失败：{type(e).__name__} {e}"}

    async def delete(self, tid: str) -> dict:
        if not await self._探测("delete"):
            return {"ok": False, "reason": "这个后端不支持删说说（接口探测失败）"}
        conn = self.bot.conn_for()
        try:
            await conn.call(self._动作("delete"), tid=str(tid), timeout=15)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "reason": f"删说说失败：{type(e).__name__} {e}"}

    # ---------------------------------------------------------- 回留言
    async def reply(self, tid: str, comment_id: str, text: str) -> dict:
        if not await self._探测("reply"):
            return {"ok": False, "reason": "这个后端不支持回留言（接口探测失败）"}
        conn = self.bot.conn_for()
        try:
            await conn.call(self._动作("reply"), tid=str(tid),
                            comment_id=str(comment_id or ""), content=str(text), timeout=15)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "reason": f"回留言失败：{type(e).__name__} {e}"}
