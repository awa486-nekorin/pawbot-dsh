"""会话上下文存储：JSON 落盘，按 session_id 存最近 N 轮 + 滚动摘要。

原来的做法是「超出窗口直接丢弃」，聊到第 9 句就把开头忘了。
现在改成三段式：

    summary   旧对话压缩成的一段话（越聊越长，但有上限）
    archive   刚溢出、还没被压缩的原始消息（等攒够了再交给模型压）
    messages  当前活跃窗口

这样它「记不清细节，但记得聊过什么」，跟人一样。
"""

from __future__ import annotations

import json
import os
import threading
import time

from .log import log

ARCHIVE_MAX = 40           # 攒到这么多条就触发一次压缩
ARCHIVE_LIMIT = ARCHIVE_MAX * 2


class Store:
    def __init__(self, path: str, max_turns: int = 8, max_messages: int = 300):
        self.path = path
        self.max_turns = max_turns
        # 上下文窗口按「条」算，不再按「轮」——群里 300 条才是一段真实的聊天
        self.max_messages = int(max_messages)
        self.lock = threading.RLock()
        self.data: dict[str, dict] = {}
        self.dirty = False
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        self.dirty = False

    def save(self):
        """立即原子落盘；作为 flush 的兼容别名。"""
        self.flush(force=True)

    def flush(self, force: bool = False) -> bool:
        """把有变更的数据原子写盘。"""
        with self.lock:
            if not force and not self.dirty:
                return False
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self.dirty = False
            return True

    def _mark_dirty(self):
        self.dirty = True

    @staticmethod
    def _mechanical_summary(rows: list[dict]) -> str:
        parts = []
        for row in rows:
            who = "我" if row.get("role") == "assistant" else (row.get("who") or "群友")
            text = " ".join(str(row.get("content") or "").split())[:40]
            if text:
                parts.append(f"{who}：{text}")
        if not parts:
            return ""
        return "（未壓縮的較早內容，已粗略合併）" + "；".join(parts)

    def _node(self, session_id: str) -> dict:
        node = self.data.setdefault(session_id, {})
        node.setdefault("messages", [])
        node.setdefault("archive", [])
        node.setdefault("summary", "")
        node.setdefault("updated", 0)
        # 每轮更新的滚动小結：只进提示词，永不发到群里
        node.setdefault("turn_summary", "")
        node.setdefault("turn_summary_ts", 0.0)
        return node

    def history(self, session_id: str) -> list[dict]:
        with self.lock:
            return list(self._node(session_id)["messages"])

    def summary(self, session_id: str) -> str:
        with self.lock:
            return str(self.data.get(session_id, {}).get("summary") or "")

    def set_summary(self, session_id: str, text: str):
        with self.lock:
            self._node(session_id)["summary"] = text.strip()[:2000]
            self._mark_dirty()

    def append(self, session_id: str, role: str, content: str,
               who: str = "", ts: float | None = None, uid: str | int | None = None):
        """记一条。who 是说话人，ts 是时间；uid 可选保存 OneBot 说话人 QQ 号。"""
        with self.lock:
            node = self._node(session_id)
            row = {
                "role": role,
                "content": content,
                "who": who or ("我" if role == "assistant" else ""),
                "ts": float(ts if ts is not None else time.time()),
            }
            if uid is not None and str(uid).strip():
                row["uid"] = str(uid)
            node["messages"].append(row)
            keep = self.max_messages
            if len(node["messages"]) > keep:
                # 溢出的不是扔掉，而是移进 archive 等压缩
                overflow = node["messages"][:-keep]
                node["messages"] = node["messages"][-keep:]
                node["archive"].extend(overflow)
                if len(node["archive"]) > ARCHIVE_LIMIT:
                    count = len(node["archive"]) - ARCHIVE_LIMIT
                    earlier = node["archive"][:count]
                    node["archive"] = node["archive"][count:]
                    rough = self._mechanical_summary(earlier)
                    if rough:
                        previous = str(node.get("summary") or "").strip()
                        node["summary"] = (previous + "\n" + rough).strip()
                    log.warn(f"会话 archive 超出上限，已将 {len(earlier)} 条较早消息机械合并：{session_id}")
            node["updated"] = int(time.time())
            self._mark_dirty()

    def annotate_last(self, session_id: str, uid, desc) -> bool:
        """把这个人最近一条「还有没被描述过的 `[图片]`」的图片行补上视觉转述。

        归属优先于覆盖：`uid` 为空时不贴（宁可没有描述，也不能把别人的图
        说成是他发的）；只找**该 uid 自己**的 user 行，且行内还留着裸的
        `[图片]` 才动它。

        一张行里有多张图时（`[图片][图片][图片]`）：
          · `desc` 是 list → 逐格填，第 N 格用第 N 条描述；
          · `desc` 是 str  → 只填第一格，多出来的格子不动；
          · 描述条数比格子多时，多出来的描述用「；」接在行尾，不丢。
        全部格子都填过的行走不到（没有裸 `[图片]`），所以重复调用会返回 False。
        """
        if isinstance(desc, (list, tuple)):
            descs = [" ".join(str(item or "").split())[:200] for item in desc]
        else:
            descs = [" ".join(str(desc or "").split())[:200]]
        descs = [snippet for snippet in descs if snippet]
        if not descs:
            return False
        uid_s = str(uid if uid is not None else "").strip()
        if not uid_s:
            # 没带 uid 就无从归属：不贴，绝不要退化回「找最近一条图行」。
            return False
        with self.lock:
            node = self.data.get(session_id)
            if not isinstance(node, dict):
                return False
            target = None
            for row in reversed(node.get("messages") or []):
                if str(row.get("role") or "") != "user":
                    continue
                if str(row.get("uid") or "") != uid_s:
                    continue
                content = str(row.get("content") or "")
                # 已经描述过的格子是 `[图片：…]`，不含裸的 `[图片]`；这里只认还没填的。
                if "[图片]" in content:
                    target = row
                    break
            if target is None:
                return False
            content = str(target.get("content") or "")
            filled = 0
            for snippet in descs:
                if "[图片]" not in content:
                    break
                content = content.replace("[图片]", f"[图片：{snippet}]", 1)
                filled += 1
            if not filled:
                return False
            extras = descs[filled:]
            if extras:
                # 格子不够：多出来的描述并到行尾，用「；」分隔，别默默丢掉。
                content = content.rstrip() + "；" + "；".join(extras)
            target["content"] = content
            self._mark_dirty()
            self.flush()
            return True

    def window(self, session_id: str, limit: int | None = None) -> list[dict]:
        """取最近的若干条（默认全窗口），带上 who / ts。"""
        with self.lock:
            rows = list(self._node(session_id)["messages"])
        if limit:
            rows = rows[-int(limit):]
        return rows

    def window_archive(self, session_id: str) -> list[dict]:
        """已溢出窗口、还没被压缩的原始消息（只读，给 WebUI 看）。"""
        with self.lock:
            return list(self._node(session_id)["archive"])

    # ------------------------------------------------------------ 滚动小結
    def turn_summary(self, session_id: str) -> str:
        with self.lock:
            return str(self.data.get(session_id, {}).get("turn_summary") or "")

    def turn_summary_ts(self, session_id: str) -> float:
        with self.lock:
            return float(self.data.get(session_id, {}).get("turn_summary_ts") or 0.0)

    def set_turn_summary(self, session_id: str, text: str, upto: float):
        """写入新的小結和它覆盖到的时间点（upto 之后的消息算下一轮）。"""
        with self.lock:
            node = self._node(session_id)
            node["turn_summary"] = (text or "").strip()[:1200]
            node["turn_summary_ts"] = float(upto or time.time())
            self._mark_dirty()

    def archive_size(self, session_id: str) -> int:
        with self.lock:
            return len(self.data.get(session_id, {}).get("archive") or [])

    def take_archive(self, session_id: str) -> list[dict]:
        """取走待压缩的消息（压缩失败时可还原）。"""
        with self.lock:
            node = self._node(session_id)
            rows = list(node.get("archive") or [])
            node["archive"] = []
            self._mark_dirty()
            return rows

    def restore_archive(self, session_id: str, rows: list[dict]):
        with self.lock:
            if not rows:
                return
            node = self._node(session_id)
            node["archive"] = rows + (node.get("archive") or [])
            if len(node["archive"]) > ARCHIVE_LIMIT:
                count = len(node["archive"]) - ARCHIVE_LIMIT
                earlier = node["archive"][:count]
                node["archive"] = node["archive"][count:]
                rough = self._mechanical_summary(earlier)
                if rough:
                    previous = str(node.get("summary") or "").strip()
                    node["summary"] = (previous + "\n" + rough).strip()
                log.warn(f"会话 archive 超出上限，已将 {len(earlier)} 条较早消息机械合并：{session_id}")
            self._mark_dirty()

    def needs_compress(self, session_id: str) -> bool:
        return self.archive_size(session_id) >= ARCHIVE_MAX

    def clear(self, session_id: str):
        with self.lock:
            self.data.pop(session_id, None)
            self._mark_dirty()

    def sessions(self) -> list[dict]:
        with self.lock:
            return [{"session_id": k,
                     "messages": len(v.get("messages", [])),
                     "archive": len(v.get("archive") or []),
                     "has_summary": bool(v.get("summary")),
                     "updated": v.get("updated", 0)}
                    for k, v in self.data.items()]
