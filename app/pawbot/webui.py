"""
WebUI：一个跑在 6185 端口的小后台。

不用 FastAPI/Vue，就是手写的 HTTP + 一个 HTML 文件——
好处是你想改哪一行就改哪一行，不用先学前端框架。

    GET  /                 后台页面（web/index.html）
    GET  /api/status       运行状态
    GET  /api/config       读配置
    POST /api/config       改配置（写回 data/config.json）
    GET  /api/plugins      插件列表
    POST /api/plugins/reload   热重载全部插件
    POST /api/plugins/toggle   启用/禁用单个插件
    POST /api/send         手动发一条消息（测试用）
    POST /api/llm/test     测试大模型连通性
    GET  /api/events       最近 100 条事件
    GET  /api/memory       记忆总览（小结 / 长期经历 / 约定 / 日记 / 人物）
    POST /api/memory/forget  清空某一类记忆
    GET  /api/context      上下文可视化：她这一眼能看到的原文 + 各项上限
    POST /api/context/window  调上下文窗口（保留条数 / 给模型看的条数 / 字数预算）
    GET  /api/stream       SSE：日志 + 状态实时推送
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import os
import re
import time
from urllib.parse import parse_qs, quote, urlparse

from .log import log


class WebUI:
    # 模型预设存在 data/models.json；键值不回显，只给首尾各 4 位
    MODELS_FILE = "models.json"

    def __init__(self, bot, host: str = "127.0.0.1", port: int = 6185,
                 token: str = "", root: str = "web"):
        self.bot = bot
        self.host = host
        self.port = port
        self.token = token
        self.root = root
        self._server: asyncio.AbstractServer | None = None
        self._started = time.time()

    async def start(self):
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info(f"WebUI 已启动 http://{self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ---------------------------------------------------------- HTTP 基础
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=20)
        except Exception:
            writer.close()
            return

        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ")
        except ValueError:
            writer.close()
            return

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        body = b""
        length = int(headers.get("content-length") or 0)
        if length:
            body = await reader.readexactly(length)

        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)

        # webui.token 留空则不校验；配置后可用页面现有的 ?token=<token>，
        # 或 Authorization: Bearer <token>。所有页面、API 与 SSE 都走同一道检查。
        if self.token:
            auth = headers.get("authorization", "")
            bearer = auth[7:] if auth[:7].lower() == "bearer " else ""
            query_token = (query.get("token") or [""])[0]
            expected = str(self.token).encode("utf-8")
            valid = any(hmac.compare_digest(candidate.encode("utf-8"), expected)
                        for candidate in (bearer, query_token) if candidate)
            if not valid:
                await self._json(writer, 401, {"ok": False, "error": "unauthorized"})
                writer.close()
                await writer.wait_closed()
                return

        try:
            if path == "/api/stream" and method == "GET":
                await self._sse(reader, writer)
                return
            if method == "GET":
                await self._route_get(writer, path, query)
            elif method == "POST":
                await self._route_post(writer, path, body)
            else:
                await self._json(writer, 405, {"ok": False, "error": "method not allowed"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                await self._json(writer, 500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _json(self, writer, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        await self._bytes(writer, status, data, "application/json; charset=utf-8")

    async def _bytes(self, writer, status: int, data: bytes, ctype: str):
        head = (
            f"HTTP/1.1 {status} {_reason(status)}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + data)
        await writer.drain()

    # ---------------------------------------------------------- 文件白名单
    def _safe_path(self, rel: str) -> str | None:
        """只返回 data/ 内的真实路径；拒绝绝对路径、../ 与符号链接逃逸。"""
        if not rel or os.path.isabs(rel):
            return None
        base = os.path.realpath(os.path.join(self.bot.base_dir, "data"))
        full = os.path.realpath(os.path.join(base, rel))
        try:
            inside = os.path.commonpath((base, full)) == base
        except ValueError:
            return None
        return full if inside and full != base else None

    async def _html(self, writer, path: str):
        rel = path.lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(self.root, rel))
        root = os.path.abspath(self.root)
        if not os.path.exists(full):
            await self._json(writer, 404, {"ok": False, "error": "not found"})
            return
        with open(full, "rb") as f:
            data = f.read()
        ctype = "text/html; charset=utf-8" if full.endswith(".html") else "text/plain; charset=utf-8"
        head = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + data)
        await writer.drain()

    # ---------------------------------------------------------- 路由
    async def _route_get(self, writer, path: str, query: dict | None = None):
        bot = self.bot
        query = query or {}
        if path in ("/", "/index.html"):
            await self._html(writer, "index.html")
            return
        if path == "/api/status":
            await self._json(writer, 200, {"ok": True, "data": bot.status()})
            return
        if path == "/api/config":
            await self._json(writer, 200, {"ok": True, "data": bot.config.data})
            return
        if path == "/api/plugins":
            await self._json(writer, 200, {"ok": True, "data": bot.plugins.list()})
            return
        if path == "/api/events":
            await self._json(writer, 200, {"ok": True, "data": list(bot.recent_events)})
            return
        if path == "/api/logs":
            await self._json(writer, 200, {"ok": True, "data": list(log.buffer)})
            return
        if path == "/api/sessions":
            await self._json(writer, 200, {"ok": True, "data": bot.store.sessions()})
            return
        if path == "/api/people":
            await self._json(writer, 200, {"ok": True,
                                           "data": bot.people.all_people()})
            return
        if path == "/api/memory":
            sid = (query.get("session_id") or [""])[0]
            await self._json(writer, 200, {"ok": True, "data": self._memory_data(sid)})
            return
        if path == "/api/context":
            sid = (query.get("session_id") or [""])[0]
            await self._json(writer, 200, {"ok": True, "data": self._context_data(sid)})
            return
        if path == "/api/models":
            await self._json(writer, 200, {"ok": True, "data": self._models_data()})
            return
        if path == "/api/files":
            await self._json(writer, 200, {"ok": True, "data": self._list_files()})
            return
        if path == "/api/file":
            rel = (query.get("path") or [""])[0]
            full = self._safe_path(rel)
            if not full:
                await self._json(writer, 403, {"ok": False, "error": "路径不允许"})
                return
            if not os.path.isfile(full):
                await self._json(writer, 404, {"ok": False, "error": "文件不存在"})
                return
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            await self._json(writer, 200, {"ok": True,
                                           "data": {"path": rel, "content": content}})
            return
        if path == "/api/stickers":
            files = bot.stickers.files()
            suffix = "?token=" + quote(self.token) if self.token else ""
            await self._json(writer, 200, {"ok": True, "data": [
                {"name": f, "url": "/stickers/" + quote(f) + suffix,
                 "meta": bot.stickers.index().get(f, {})} for f in files]})
            return
        if path.startswith("/stickers/"):
            await self._serve_sticker(writer, path)
            return
        await self._json(writer, 404, {"ok": False, "error": "not found"})

    def _list_files(self) -> list[dict]:
        """列出 data/ 下所有可编辑的文本文件（人设、记忆）。"""
        base = os.path.join(self.bot.base_dir, "data")
        items = []
        for root, _dirs, files in os.walk(base):
            for name in files:
                if os.path.splitext(name)[1].lower() not in (".md", ".txt"):
                    continue
                full = os.path.join(root, name)
                items.append({
                    "path": os.path.relpath(full, base).replace("\\", "/"),
                    "name": name,
                    "size": os.path.getsize(full),
                    "mtime": int(os.path.getmtime(full)),
                })
        return sorted(items, key=lambda x: x["path"])

    # ---------------------------------------------------------- 记忆可检视
    @staticmethod
    def _call(obj, name: str, *args, default=None):
        """调一个可能不存在 / 可能抛异常的方法，失败就吃默认值。"""
        fn = getattr(obj, name, None) if obj is not None else None
        if not callable(fn):
            return default
        try:
            value = fn(*args)
        except Exception as e:
            log.debug(f"记忆读取失败 {name}：{type(e).__name__} {e}")
            return default
        return default if value is None else value

    def _diary_dir(self) -> str:
        diary = getattr(self.bot, "diary", None)
        configured = str(getattr(diary, "dir", "") or "") if diary is not None else ""
        if configured:
            return os.path.realpath(configured)
        base = str(getattr(self.bot, "base_dir", "") or "")
        if not base:
            return ""
        return os.path.realpath(os.path.join(base, "data", "diary"))

    def _diary_rows(self) -> list[dict]:
        """最近 3 天的日记；今天的以文件为准（最近生成的可能还没进索引）。"""
        rows = self._call(getattr(self.bot, "diary", None), "recent", 3, default=[])
        if isinstance(rows, dict):
            rows = [{"day": day, "text": text} for day, text in rows.items()]
        out: dict[str, str] = {}
        if isinstance(rows, (list, tuple)):
            for row in rows:
                day, text = "", ""
                if isinstance(row, dict):
                    day = str(row.get("day") or row.get("date") or "").strip()
                    text = str(row.get("text") or row.get("summary")
                               or row.get("content") or "").strip()
                    if not day and row.get("ts"):
                        try:
                            day = time.strftime("%Y-%m-%d",
                                                time.localtime(float(row["ts"])))
                        except (TypeError, ValueError, OSError):
                            day = ""
                elif isinstance(row, (list, tuple)) and len(row) >= 2:
                    # diary.recent() 现在返回 [(day, 正文)]
                    day = str(row[0] or "").strip()
                    text = str(row[1] or "").strip()
                else:
                    text = str(row or "").strip()
                    m = re.match(r"^(\d{4}-\d{2}-\d{2})[：:\s]+(.*)$", text, re.S)
                    if m:
                        day, text = m.group(1), m.group(2).strip()
                if day and text:
                    out.setdefault(day, text)

        today = time.strftime("%Y-%m-%d")
        folder = self._diary_dir()
        if folder:
            path = os.path.join(folder, today + ".md")
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read().strip()
                except OSError:
                    text = ""
                if text:
                    out[today] = text
        days = sorted(out, reverse=True)[:3]
        return [{"day": day, "text": out[day][:4000]} for day in days]

    def _context_data(self, session_id: str) -> dict:
        """她这一眼能看到的原文 + 三层上限，给 WebUI 的「上下文」页用。"""
        bot = self.bot
        store = getattr(bot, "store", None)
        if store is None:
            return {"sessions": [], "session_id": "", "messages": [], "limits": {}}
        sessions = self._call(store, "sessions", default=[]) or []
        ids = [str(s.get("session_id") or "") for s in sessions if s.get("session_id")]
        sid = str(session_id or "").strip() or (ids[0] if ids else "")
        rows = (self._call(store, "window", sid, default=[]) or []) if sid else []
        try:
            from . import ctxwin
            rendered, stats = ctxwin.build(rows, bot.config) if rows else ("", {})
        except Exception as e:
            rendered, stats = "", {"error": f"{type(e).__name__}: {e}"}
        keep = int(getattr(store, "max_messages", 300) or 300)
        limits = {
            "keep": keep,
            "context_messages": int(bot.config.get("chat.context_messages", 300) or 300),
            "context_chars": int(bot.config.get("chat.context_chars", 12000) or 12000),
            "time_decay_hours": float(bot.config.get("chat.time_decay_hours", 6) or 6),
            "archive": len(self._call(store, "window_archive", sid, default=[]) or []),
        }
        messages = [{
            "ts": float(r.get("ts") or 0),
            "who": str(r.get("who") or ""),
            "role": str(r.get("role") or ""),
            "content": str(r.get("content") or "")[:400],
        } for r in rows]
        return {
            "sessions": [{"session_id": i, "messages": next(
                (int(s.get("messages") or 0) for s in sessions
                 if str(s.get("session_id") or "") == i), 0)} for i in ids],
            "session_id": sid,
            "messages": messages,
            "limits": limits,
            "stats": stats,
            "rendered": rendered,
        }

    # ------------------------------------------------------------ 模型预设
    def _models_path(self) -> str:
        base = getattr(self.bot, "base_dir", None) or os.getcwd()
        return os.path.join(str(base), "data", self.MODELS_FILE)

    @staticmethod
    def _mask_key(key) -> str:
        k = str(key or "")
        if not k:
            return ""
        return (k[:4] + "…" + k[-4:]) if len(k) > 12 else "…"

    def _load_models(self) -> list[dict]:
        try:
            with open(self._models_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            presets = data.get("presets") if isinstance(data, dict) else data
            return [p for p in (presets or []) if isinstance(p, dict) and p.get("id")]
        except Exception:
            return []

    def _save_models(self, presets: list[dict]) -> None:
        path = self._models_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"presets": presets}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _models_data(self) -> dict:
        bot = self.bot
        presets = [{
            "id": p.get("id"),
            "name": p.get("name") or p.get("id"),
            "base_url": p.get("base_url") or "",
            "model": p.get("model") or "",
            "key_masked": self._mask_key(p.get("api_key")),
            "has_key": bool(p.get("api_key")),
        } for p in self._load_models()]
        current = {
            "chat": {"base_url": bot.config.get("llm.base_url", ""),
                     "model": bot.config.get("llm.model", "")},
            "judge": {"base_url": bot.config.get("judge.base_url", ""),
                      "model": bot.config.get("judge.model", "")},
            "emotion": {"base_url": bot.config.get("emotion.judge_base_url", ""),
                        "model": bot.config.get("emotion.judge_model", "")},
            "vision": {"base_url": bot.config.get("llm.vision_base_url", ""),
                       "model": bot.config.get("llm.vision_model", "")},
        }
        return {"presets": presets, "current": current}

    def _save_model(self, payload: dict) -> tuple[bool, dict]:
        presets = self._load_models()
        pid = str(payload.get("id") or "").strip()
        name = str(payload.get("name") or "").strip()
        base = str(payload.get("base_url") or "").strip().rstrip("/")
        model = str(payload.get("model") or "").strip()
        key = str(payload.get("api_key") or "").strip()
        if not name or not base or not model:
            return False, {"ok": False, "error": "名称、API 地址、模型名都要填"}
        row = next((p for p in presets if str(p.get("id")) == pid), None)
        if row is None:
            pid = pid or f"m{int(time.time() * 1000)}"
            row = {"id": pid, "created": int(time.time())}
            presets.append(row)
        row.update({"name": name, "base_url": base, "model": model})
        if key:
            row["api_key"] = key
        self._save_models(presets)
        log.info(f"模型预设已保存：{name}（{model}）")
        return True, {"ok": True, "data": self._models_data()}

    def _delete_model(self, payload: dict) -> tuple[bool, dict]:
        pid = str(payload.get("id") or "")
        presets = self._load_models()
        left = [p for p in presets if str(p.get("id")) != pid]
        if len(left) == len(presets):
            return False, {"ok": False, "error": "没有这个预设"}
        self._save_models(left)
        log.info("模型预设已删除")
        return True, {"ok": True, "data": self._models_data()}

    def _apply_model(self, payload: dict) -> tuple[bool, dict]:
        pid = str(payload.get("id") or "")
        target = str(payload.get("target") or "chat")
        preset = next((p for p in self._load_models() if str(p.get("id")) == pid), None)
        if preset is None:
            return False, {"ok": False, "error": "没有这个预设"}
        base = str(preset.get("base_url") or "").strip().rstrip("/")
        key = str(preset.get("api_key") or "").strip()
        model = str(payload.get("model") or preset.get("model") or "").strip()
        if not base or not key or not model:
            return False, {"ok": False, "error": "这个预设缺 API 地址 / key / 模型名"}
        if target == "chat":
            patch = {"llm": {"base_url": base, "api_key": key, "model": model}}
        elif target == "judge":
            patch = {"judge": {"base_url": base, "api_key": key, "model": model}}
        elif target == "emotion":
            patch = {"emotion": {"judge_base_url": base, "judge_api_key": key,
                                 "judge_model": model}}
        elif target == "vision":
            patch = {"llm": {"vision_base_url": base, "vision_api_key": key,
                             "vision_model": model}}
        else:
            return False, {"ok": False,
                           "error": "target 只能是 chat / judge / emotion / vision"}
        self.bot.config.update(patch)
        log.info(f"模型已切换：{target} → {preset.get('name')} / {model}")
        return True, {"ok": True, "data": self._models_data()}

    def _set_context_window(self, payload: dict) -> tuple[bool, dict]:
        """调三层上限：store 保留条数 / 给模型看的条数 / 字数预算。

        已经丢掉的消息找不回来，这里只影响从现在开始记住多少。
        """
        bot = self.bot
        store = getattr(bot, "store", None)
        if store is None:
            return False, {"ok": False, "error": "会话存储不可用"}

        def num(key, lo, hi, default):
            try:
                v = int(payload.get(key))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        keep = num("keep", 50, 20000, int(getattr(store, "max_messages", 300) or 300))
        msgs = num("context_messages", 20, 5000,
                   int(bot.config.get("chat.context_messages", 300) or 300))
        chars = num("context_chars", 2000, 200000,
                    int(bot.config.get("chat.context_chars", 12000) or 12000))
        if keep < msgs:
            # 保留得比给模型看的还少，等于配置无效，抬到同一档
            keep = msgs
        store.max_messages = keep
        bot.config.update({"chat": {"context_keep": keep,
                                    "context_messages": msgs,
                                    "context_chars": chars}})
        log.info(f"上下文窗口已调：保留 {keep} 条 / 给模型 {msgs} 条 / 预算 {chars} 字")
        return True, {"ok": True, "data": self._context_data(
            str(payload.get("session_id") or ""))}

    def _memory_data(self, session_id: str) -> dict:
        """一次给全：本会话小结 + 长期经历 + 约定 + 日记 + 人物。

        longmem / reminders / diary / people 都可能是别的模块还没加上的，
        全部 getattr + 容错，缺谁就回空列表，绝不让整个接口 500。
        """
        bot = self.bot
        sid = str(session_id or "")
        store = getattr(bot, "store", None)
        summary = str(self._call(store, "summary", sid, default="") or "")
        turn_summary = str(self._call(store, "turn_summary", sid, default="") or "")

        episodes = []
        longmem = getattr(bot, "longmem", None)
        rows = getattr(longmem, "episodes", None) if longmem is not None else None
        if isinstance(rows, list):
            for row in reversed(rows[-20:]):
                if not isinstance(row, dict):
                    continue
                try:
                    ts = int(float(row.get("ts") or 0))
                except (TypeError, ValueError):
                    ts = 0
                try:
                    importance = int(row.get("importance") or 1)
                except (TypeError, ValueError):
                    importance = 1
                episodes.append({
                    "ts": ts,
                    "session_id": str(row.get("session_id") or ""),
                    "summary": str(row.get("summary") or ""),
                    "importance": importance,
                })

        reminders = self._call(getattr(bot, "reminders", None),
                               "list_pending", default=[])
        if not isinstance(reminders, list):
            reminders = []

        people = self._call(getattr(bot, "people", None),
                            "all_people", default=[])
        if not isinstance(people, list):
            people = []

        return {
            "summary": summary,
            "turn_summary": turn_summary,
            "episodes": episodes,
            "reminders": reminders,
            "diary": self._diary_rows(),
            "people": people,
        }

    async def _forget_memory(self, payload: dict) -> tuple[int, dict]:
        """清空某一类记忆。返回 (HTTP 状态码, JSON)。"""
        bot = self.bot
        sid = str(payload.get("session_id") or "")
        what = str(payload.get("what") or "")
        if what == "turn_summary":
            store = getattr(bot, "store", None)
            setter = getattr(store, "set_turn_summary", None)
            if not callable(setter):
                return 501, {"ok": False, "error": "会话存储不支持清空本轮小结"}
            setter(sid, "", time.time())
            log.info(f"记忆已清空：{sid} 的本轮小结")
            return 200, {"ok": True, "data": {"forgot": what, "session_id": sid}}

        if what == "episodes":
            longmem = getattr(bot, "longmem", None)
            drop = getattr(longmem, "drop_session", None)
            if not callable(drop):
                return 501, {"ok": False,
                             "error": "长期记忆暂不支持按会话清空（缺 drop_session）"}
            removed = drop(sid)
            if inspect.isawaitable(removed):
                # drop_session 可能是 async（longmem 里用 asyncio.Lock），两种都接
                removed = await removed
            log.info(f"记忆已清空：{sid} 的长期经历（{removed}）")
            return 200, {"ok": True,
                         "data": {"forgot": what, "session_id": sid, "removed": removed}}

        if what == "diary":
            day = str(payload.get("day") or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return 400, {"ok": False, "error": "day 必须是 YYYY-MM-DD"}
            folder = self._diary_dir()
            if not folder:
                return 400, {"ok": False, "error": "日记目录不可用"}
            # 只允许 data/diary/<day>.md，防目录穿越
            full = os.path.realpath(os.path.join(folder, day + ".md"))
            try:
                inside = os.path.commonpath([full, folder]) == folder
            except ValueError:
                inside = False
            if not inside or os.path.basename(full) != day + ".md":
                return 403, {"ok": False, "error": "路径不允许"}
            removed = False
            if os.path.isfile(full):
                try:
                    os.unlink(full)
                    removed = True
                except OSError as e:
                    return 500, {"ok": False,
                                 "error": f"删除失败：{type(e).__name__}: {e}"}
            log.info(f"记忆已清空：日记 {day}（{'已删' if removed else '本来就没有'}）")
            return 200, {"ok": True,
                         "data": {"forgot": what, "day": day, "removed": removed}}

        return 400, {"ok": False,
                     "error": "what 只能是 turn_summary / episodes / diary"}

    async def _serve_sticker(self, writer, path: str):
        from urllib.parse import unquote
        name = unquote(path[len("/stickers/"):])
        full = os.path.join(self.bot.stickers.dir, os.path.basename(name))
        if not os.path.exists(full):
            await self._json(writer, 404, {"ok": False, "error": "not found"})
            return
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp"}.get(
            os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            await self._bytes(writer, 200, f.read(), ctype)

    async def _route_post(self, writer, path: str, body: bytes):
        bot = self.bot
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            await self._json(writer, 400, {"ok": False, "error": "JSON 解析失败"})
            return

        if path == "/api/config":
            bot.config.update(payload)
            log.info("配置已更新")
            await self._json(writer, 200, {"ok": True, "data": bot.config.data})
            return

        if path == "/api/plugins/reload":
            result = bot.plugins.reload_all()
            log.info(f"插件重载完成：成功 {len(result['loaded'])}，失败 {len(result['failed'])}")
            await self._json(writer, 200, {"ok": True, "data": result})
            return

        if path == "/api/plugins/toggle":
            name = str(payload.get("name", ""))
            enabled = bool(payload.get("enabled", True))
            bot.plugins.set_enabled(name, enabled)
            log.info(f"插件 {name} 已{'启用' if enabled else '禁用'}")
            await self._json(writer, 200, {"ok": True, "data": bot.plugins.list()})
            return

        if path == "/api/models/save":
            ok, result = self._save_model(payload)
            await self._json(writer, 200 if ok else 400, result)
            return

        if path == "/api/models/delete":
            ok, result = self._delete_model(payload)
            await self._json(writer, 200 if ok else 400, result)
            return

        if path == "/api/models/apply":
            ok, result = self._apply_model(payload)
            await self._json(writer, 200 if ok else 400, result)
            return

        if path == "/api/context/window":
            ok, result = self._set_context_window(payload)
            await self._json(writer, 200 if ok else 400, result)
            return

        if path == "/api/send":
            target = str(payload.get("target", "")).strip()
            text = str(payload.get("message", ""))
            is_group = bool(payload.get("is_group", True))
            if not target:
                await self._json(writer, 400, {"ok": False, "error": "目标不能为空"})
                return
            try:
                data = await bot.manual_send(target, text, is_group)
                await self._json(writer, 200, {"ok": True, "data": data})
            except Exception as e:
                await self._json(writer, 200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/llm/test":
            try:
                reply = await bot.llm.test()
                await self._json(writer, 200, {"ok": True, "data": reply})
            except Exception as e:
                await self._json(writer, 200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return

        if path == "/api/session/clear":
            sid = str(payload.get("session_id", ""))
            bot.clear_session(sid)
            await self._json(writer, 200, {"ok": True, "data": bot.store.sessions()})
            return

        if path == "/api/memory/forget":
            status, result = await self._forget_memory(payload)
            await self._json(writer, status, result)
            return

        if path == "/api/file":
            rel = str(payload.get("path", ""))
            content = str(payload.get("content", ""))
            full = self._safe_path(rel)
            if not full:
                await self._json(writer, 403, {"ok": False, "error": "路径不允许"})
                return
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            log.info(f"文件已保存：{rel}")
            await self._json(writer, 200, {"ok": True, "data": {"path": rel}})
            return

        await self._json(writer, 404, {"ok": False, "error": "not found"})

    # ---------------------------------------------------------- SSE
    async def _sse(self, reader, writer):
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream; charset=utf-8\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: close\r\n"
            b"X-Accel-Buffering: no\r\n\r\n"
        )
        await writer.drain()

        q = log.subscribe()
        try:
            for rec in list(log.buffer)[-40:]:
                await self._sse_send(writer, "log", rec)
            await self._sse_send(writer, "status", self.bot.status())

            last_status = time.time()
            while True:
                try:
                    rec = await asyncio.wait_for(q.get(), timeout=1.0)
                    await self._sse_send(writer, "log", rec)
                except asyncio.TimeoutError:
                    pass
                if time.time() - last_status > 3:
                    await self._sse_send(writer, "status", self.bot.status())
                    last_status = time.time()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            log.unsubscribe(q)

    async def _sse_send(self, writer, event: str, data: dict):
        payload = json.dumps(data, ensure_ascii=False, default=str)
        writer.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        await writer.drain()


def _reason(code: int) -> str:
    return {200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            500: "Internal Server Error", 501: "Not Implemented"}.get(code, "OK")
