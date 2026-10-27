#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""大肥鱼 · 对话测试窗口（Tkinter，零依赖）

用途：不装 NapCat、不开 QQ，也能像在群里一样跟她聊天，用来看人设 / 大脑 / 表情包 / 分条效果。

原理：这个窗口假装成 NapCat，用 OneBot v11 **反向 WebSocket** 连到 PawBot 的
`ws://127.0.0.1:6199/ws`（和真协议端同一条路），所以走的是**完整真链路**：

    Tk 窗口 → 反向 WS → PawBot core → ai_chat/persona/memory → llm.provider=dsh 的大脑
             → 表达层（style.polish / ‖ 分条 / 表情包）→ send_group_msg → 窗口显示

用法：
    py tools/chat_window.py                  # 打开窗口
    py tools/chat_window.py --check "在吗"   # 无窗口自检：连上、发一句、打印回复后退出
    py tools/chat_window.py --group 123 --qq 456 --nick 阿明

窗口里的按钮：
    清空          清空聊天记录（不影响 PawBot 记忆）
    设为主人/取消  把「我的 QQ」写进/移出 PawBot 的 account.owner_ids
                  （只有主人能用电脑工具；默认名单为空，所以需要点一下才测得到 pwsh）
    私聊测试      勾上后按私聊发送（不再需要 @），适合测主人专属能力

设置存在 app/data/chat_window.json（不进仓库）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import tkinter as tk

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from pawbot import wsproto  # noqa: E402

DEFAULT_SELF_ID = 10001  # 假装成 NapCat 登录的机器人 QQ（--check 默认 10002，避免和窗口互相顶掉）
WEBUI = "http://127.0.0.1:6185"
SETTINGS_FILE = APP_DIR / "data" / "chat_window.json"
DEFAULTS = {
    "host": "127.0.0.1",
    "port": 6199,
    "path": "/ws",
    "group_id": 987654321,
    "user_id": 123456,
    "nickname": "阿明",
}


# ============================================================ WS 客户端（线程）

class WsClient:
    """在后台线程里跑 asyncio：连反向 WS、发消息、收动作、回执。"""

    def __init__(self, emit, settings, self_id=DEFAULT_SELF_ID):
        self.emit = emit  # emit(kind, payload)
        self.self_id = int(self_id)
        self.settings = settings
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="pawbot-ws")
        self.outbox: queue.Queue = queue.Queue()
        self.running = False
        self.connected = False
        self.stopping = False
        self.ws = None

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping = True
        self.outbox.put(None)
        if self.ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
            except Exception:
                pass

    def send(self, text: str):
        self.outbox.put(str(text))

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as exc:  # noqa: BLE001
            self.emit("sys", f"后台线程异常：{type(exc).__name__}: {exc}")
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    async def _main(self):
        while not self.stopping:
            try:
                ws = await wsproto.connect(
                    self.settings["host"], int(self.settings["port"]),
                    self.settings["path"],
                    {"X-Self-ID": str(self.self_id), "X-Client-Role": "Universal"},
                )
                self.ws = ws
                self.connected = True
                self.emit("conn", True)
                # 真 NapCat 连上后会发 lifecycle/connect；照发一遍，让 PawBot 走同一套初始化。
                await ws.send_text(json.dumps({
                    "post_type": "meta_event", "meta_event_type": "lifecycle",
                    "sub_type": "connect", "time": int(time.time()), "self_id": self.self_id,
                }))
                heartbeat = asyncio.create_task(self._heartbeat(ws))
                reader = asyncio.create_task(self._reader(ws))
                sender = asyncio.create_task(self._sender(ws))
                done, pending = await asyncio.wait(
                    {reader, sender}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                heartbeat.cancel()
                for task in (heartbeat, *pending):
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                try:
                    await ws.close()
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                if not self.stopping:
                    self.emit("sys", f"连不上 {self.settings['host']}:{self.settings['port']}：{exc}")
            finally:
                self.connected = False
                self.ws = None
                self.emit("conn", False)
            if self.stopping:
                break
            await asyncio.sleep(2.0)

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(4)
            try:
                await ws.send_text(json.dumps({
                    "post_type": "meta_event", "meta_event_type": "heartbeat",
                    "time": int(time.time()), "self_id": self.self_id,
                    "status": {"online": True, "good": True}, "interval": 4000,
                }))
            except Exception:
                return

    async def _sender(self, ws):
        while True:
            text = await self.loop.run_in_executor(None, self.outbox.get)
            if text is None:
                return
            event = build_event(text, self.settings, self.self_id)
            await ws.send_text(json.dumps(event, ensure_ascii=False))
            self.emit("sent", text)

    async def _reader(self, ws):
        while True:
            raw = await ws.recv()
            if raw is None:
                self.emit("sys", "协议端断开（PawBot 重启了？）")
                return
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            action = data.get("action")
            if not action:
                continue
            if action == "set_input_status":
                self.emit("typing", True)
            elif action in ("send_group_msg", "send_private_msg"):
                self.emit("reply", data.get("params") or {})
            await ws.send_text(json.dumps({
                "status": "ok", "retcode": 0,
                "data": {"message_id": int(time.time() * 1000) % 10 ** 8, "online": True},
                "echo": data.get("echo", ""),
            }, ensure_ascii=False))


def build_event(text: str, settings: dict, self_id: int = DEFAULT_SELF_ID) -> dict:
    """把一句话包成 OneBot 群/私聊消息事件（和真 NapCat 的形状一致）。"""
    now = int(time.time())
    sender = {"user_id": int(settings["user_id"]), "nickname": settings["nickname"],
              "card": settings["nickname"]}
    if settings.get("private"):
        return {
            "post_type": "message", "message_type": "private", "sub_type": "friend",
            "time": now, "self_id": self_id,
            "message_id": int(time.time() * 1000) % 10 ** 8,
            "user_id": int(settings["user_id"]),
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text, "font": 0, "sender": sender,
        }
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "time": now, "self_id": self_id,
        "message_id": int(time.time() * 1000) % 10 ** 8,
        "group_id": int(settings["group_id"]),
        "user_id": int(settings["user_id"]),
        "message": [{"type": "at", "data": {"qq": str(self_id)}},
                    {"type": "text", "data": {"text": " " + text}}],
        "raw_message": f"[CQ:at,qq={self_id}] {text}", "font": 0, "sender": sender,
    }


# ============================================================ HTTP 小工具

def http_json(path: str, payload: dict | None = None, timeout: float = 4.0):
    url = WEBUI + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if payload is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def service_line() -> str:
    """给状态栏用：PawBot 在线？大脑走哪？mock 吗？"""
    try:
        status = (http_json("/api/status") or {}).get("data") or {}
        provider = status.get("llm_provider") or ("dsh" if not status.get("llm_mock") else "api")
        model = status.get("llm_model") or "?"
        mock = "mock" if status.get("llm_mock") else "非mock"
        conns = len(status.get("connections") or [])
        return f"PawBot 在线 · 连接 {conns} · {provider}/{model} · {mock}"
    except Exception as exc:  # noqa: BLE001
        return f"PawBot 连不上：{exc}"


# ============================================================ 窗口

class ChatWindow:
    PLACEHOLDER = "在这里输入要说的话（Enter 发送，Shift+Enter 换行）"

    def __init__(self, root, settings, self_id=DEFAULT_SELF_ID):
        self.root = root
        self.settings = dict(settings)
        self.self_id = int(self_id)
        self.client = WsClient(self._emit, self.settings, self.self_id)
        self.events: queue.Queue = queue.Queue()
        self.pending_since = None
        self.last_reply_at = None
        self.typing_until = 0.0
        self._connected = False
        self._build()
        self.client.start()
        self.root.after(80, self._poll)
        self.root.after(500, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    # ---------- 布局 ----------
    def _build(self):
        self.root.title("大肥鱼 · 对话测试")
        # 固定一个「矮一点」的尺寸：高分屏缩放时 640 高容易把输入框挤出屏幕。
        # 输入区在 pack 顺序上先占位（见下方），就算窗口再小也不会看不到输入框。
        self.root.geometry("800x560+40+40")
        self.root.minsize(520, 380)
        bg = "#f7f3f6"
        self.root.configure(bg=bg)
        base = ("Microsoft YaHei UI", 10)
        bold = ("Microsoft YaHei UI", 10, "bold")
        small = ("Microsoft YaHei UI", 9)

        top = tk.Frame(self.root, bg=bg)
        top.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(top, text="大肥鱼 · 对话测试", bg=bg, font=("Microsoft YaHei UI", 13, "bold"),
                 fg="#b03a6a").pack(side="left")
        self.conn_label = tk.Label(top, text="● 连接中…", bg=bg, font=small, fg="#b07a00")
        self.conn_label.pack(side="left", padx=10)
        tk.Button(top, text="清空", command=self._clear, font=small).pack(side="right")
        tk.Button(top, text="取消主人", command=lambda: self._set_owner(False),
                  font=small).pack(side="right", padx=4)
        tk.Button(top, text="设为主人", command=lambda: self._set_owner(True),
                  font=small).pack(side="right")

        form = tk.Frame(self.root, bg=bg)
        form.pack(fill="x", padx=10, pady=(2, 4))
        tk.Label(form, text="群号", bg=bg, font=small).pack(side="left")
        self.group_var = tk.StringVar(value=str(self.settings["group_id"]))
        tk.Entry(form, textvariable=self.group_var, width=12, font=small).pack(side="left", padx=(2, 8))
        tk.Label(form, text="我的QQ", bg=bg, font=small).pack(side="left")
        self.qq_var = tk.StringVar(value=str(self.settings["user_id"]))
        tk.Entry(form, textvariable=self.qq_var, width=12, font=small).pack(side="left", padx=(2, 8))
        tk.Label(form, text="昵称", bg=bg, font=small).pack(side="left")
        self.nick_var = tk.StringVar(value=str(self.settings["nickname"]))
        tk.Entry(form, textvariable=self.nick_var, width=10, font=small).pack(side="left", padx=(2, 8))
        self.private_var = tk.BooleanVar(value=bool(self.settings.get("private")))
        tk.Checkbutton(form, text="私聊测试", variable=self.private_var, bg=bg,
                       font=small, activebackground=bg,
                       command=self._apply_settings).pack(side="left", padx=(4, 8))
        tk.Button(form, text="保存", command=self._save, font=small).pack(side="left")

        box = tk.Frame(self.root, bg=bg)
        scroll = tk.Scrollbar(box)
        scroll.pack(side="right", fill="y")
        self.chat = tk.Text(box, wrap="word", state="disabled", font=base, bg="#fffdfd",
                            relief="flat", padx=10, pady=8, spacing1=2, spacing3=4,
                            yscrollcommand=scroll.set)
        self.chat.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.chat.yview)
        self.chat.tag_configure("user", foreground="#245a8d", font=bold, spacing1=8)
        self.chat.tag_configure("fish", foreground="#b03a6a", font=bold, spacing1=8)
        self.chat.tag_configure("body", foreground="#222222")
        self.chat.tag_configure("meta", foreground="#999999", font=small)
        self.chat.tag_configure("sys", foreground="#888888", font=small, justify="center",
                                spacing1=4, spacing3=4)

        bottom = tk.Frame(self.root, bg=bg)
        tk.Label(bottom, text="在这里输入 →", bg=bg, fg="#b03a6a",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(0, 6))
        self.entry = tk.Text(bottom, height=3, font=base, wrap="word", relief="solid",
                             borderwidth=1, padx=8, pady=6)
        self.entry.pack(side="left", fill="both", expand=True)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Shift-Return>", lambda e: None)
        self.entry.insert("1.0", self.PLACEHOLDER)
        self.entry.config(fg="#999999")
        self.entry.bind("<FocusIn>", self._clear_placeholder, add="+")
        tk.Button(bottom, text="发送", command=self._send, font=bold, width=7,
                  bg="#d75a88", fg="white", activebackground="#b03a6a").pack(side="right",
                                                                            padx=(8, 0), ipady=12)

        self.status = tk.Label(self.root, text=service_line(), bg="#efe8ee", fg="#555555",
                               font=small, anchor="w", padx=10)
        self.status.pack(side="bottom", fill="x")
        bottom.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        box.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        self._append_sys("窗口已启动。群聊直接输入即可（自动 @ 她）；勾「私聊测试」可测私聊。")
        self._append_sys("提示：她记得之前的聊天（PawBot 记忆），所以第一次回你可能是接着之前的上下文。")
        self._append_sys("注意：「在吗 / ping / 延迟」会触发基本插件的 pong 指令（不走 AI）；想测人设直接聊别的。")
        self._append_sys("想测电脑工具（pwsh/读写文件）：先把上面「我的QQ」改成你自己的号，再点右上角「设为主人」。")

    # ---------- 事件循环 ----------
    def _emit(self, kind, payload):
        self.events.put((kind, payload))

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "conn":
                    self._connected = bool(payload)
                    if self._connected:
                        self.conn_label.config(text="● 已连接", fg="#1f8a4c")
                        self.status.config(text=service_line())
                    else:
                        self.conn_label.config(text="● 未连接（2 秒后重连）", fg="#b03a2e")
                        self.pending_since = None
                elif kind == "sys":
                    self._append_sys(str(payload))
                elif kind == "typing":
                    self.typing_until = time.time() + 3
                elif kind == "sent":
                    self.pending_since = time.time()
                elif kind == "reply":
                    self._render_reply(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _tick(self):
        self.root.after(500, self._tick)
        if int(time.time()) % 8 == 0:
            self.status.config(text=service_line())
        if self.pending_since is not None:
            waited = time.time() - self.pending_since
            if waited > 150:
                self.pending_since = None
                self._append_sys("150 秒没有回复：可能被限流、她选择沉默，或大脑/模型那边卡住了。")
            else:
                hint = " · 正在输入…" if time.time() < self.typing_until else ""
                self.status.config(text=f"等待回复 {waited:0.1f}s{hint}")

    # ---------- 渲染 ----------
    def _append(self, tag, text, newline=True):
        self.chat.configure(state="normal")
        self.chat.insert("end", text + ("\n" if newline else ""), tag)
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _append_sys(self, text):
        self._append("sys", f"— {text} —")

    def _render_reply(self, params):
        segments = params.get("message") or []
        texts, images = [], 0
        for seg in segments:
            kind = seg.get("type")
            data = seg.get("data") or {}
            if kind == "text":
                value = str(data.get("text") or "")
                if value.strip():
                    texts.append(value)
            elif kind in ("image", "face", "mface"):
                images += 1
            elif kind == "record":
                texts.append("[语音]")
            elif kind == "at":
                pass
            elif kind == "reply":
                pass
        latency = ""
        if self.pending_since is not None:
            latency = f"（{time.time() - self.pending_since:0.1f}s）"
            self.pending_since = None
            self.last_reply_at = time.time()
        self.typing_until = 0
        for text in texts:
            self._append("fish", "大肥鱼  ▸ ", newline=False)
            self._append("body", text, newline=False)
            self._append("meta", " " + latency)
        if images and not texts:
            self._append("fish", "大肥鱼  ▸ ", newline=False)
            self._append("body", f"[图片/表情包 ×{images}]", newline=False)
            self._append("meta", " " + latency)

    # ---------- 操作 ----------
    def _clear_placeholder(self, event=None):
        if self.entry.get("1.0", "end").strip() == self.PLACEHOLDER:
            self.entry.delete("1.0", "end")
            self.entry.config(fg="#222222")

    def _restore_placeholder(self):
        if not self.entry.get("1.0", "end").strip():
            self.entry.insert("1.0", self.PLACEHOLDER)
            self.entry.config(fg="#999999")

    def _on_return(self, event):
        if event.state & 0x0001:  # Shift
            return None
        self._send()
        return "break"

    def _send(self):
        text = self.entry.get("1.0", "end").strip()
        if not text or text == self.PLACEHOLDER:
            return
        self.entry.delete("1.0", "end")
        self._restore_placeholder()
        if not self.client.connected:
            self._append_sys("还没连上 PawBot：确认 DSH 里 PawBot 在跑（设置 → PawBot → 启动）。")
            return
        self._apply_settings()
        self._append("user", "我  ▸ ", newline=False)
        self._append("body", text)
        self.client.send(text)

    def _apply_settings(self):
        try:
            self.settings["group_id"] = int(self.group_var.get().strip() or 0)
            self.settings["user_id"] = int(self.qq_var.get().strip() or 0)
        except ValueError:
            self._append_sys("群号 / QQ 必须是数字")
            return
        self.settings["nickname"] = self.nick_var.get().strip() or "群友"
        self.settings["private"] = bool(self.private_var.get())

    def _save(self):
        self._apply_settings()
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
            self._append_sys(f"设置已保存到 {SETTINGS_FILE}")
        except OSError as exc:
            self._append_sys(f"保存失败：{exc}")

    def _set_owner(self, enable: bool):
        self._apply_settings()
        uid = int(self.settings["user_id"])
        try:
            data = http_json("/api/config", {"account": {"owner_ids": [str(uid)] if enable else []}})
            if not data.get("ok"):
                raise RuntimeError(data.get("error") or "接口返回失败")
            current = (data.get("data") or {}).get("account", {}).get("owner_ids")
            self._append_sys(f"已{'设为主人' if enable else '清除主人名单'}：account.owner_ids = {current}")
            self.status.config(text=service_line())
        except Exception as exc:  # noqa: BLE001
            self._append_sys(f"改主人名单失败：{exc}")

    def _clear(self):
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self._append_sys("已清空窗口（PawBot 的记忆没有动）")

    def _close(self):
        try:
            self.client.stop()
        finally:
            self.root.destroy()


# ============================================================ 无窗口自检

def run_check(settings: dict, text: str, self_id: int = 10002, wait: float = 120.0) -> int:
    async def main():
        print(f"连接 ws://{settings['host']}:{settings['port']}{settings['path']} …", flush=True)
        ws = await wsproto.connect(settings["host"], int(settings["port"]), settings["path"],
                                   {"X-Self-ID": str(self_id), "X-Client-Role": "Universal"})
        await ws.send_text(json.dumps({
            "post_type": "meta_event", "meta_event_type": "lifecycle",
            "sub_type": "connect", "time": int(time.time()), "self_id": self_id}))
        await asyncio.sleep(0.4)
        event = build_event(text, settings, self_id)
        await ws.send_text(json.dumps(event, ensure_ascii=False))
        print(f"发送：{text}", flush=True)
        deadline = time.time() + wait
        original_deadline = deadline
        replies = []
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            if raw is None:
                break
            data = json.loads(raw)
            action = data.get("action")
            if not action:
                continue
            if action in ("send_group_msg", "send_private_msg"):
                parts = []
                for seg in (data.get("params") or {}).get("message") or []:
                    if seg.get("type") == "text" and str(seg["data"].get("text") or "").strip():
                        parts.append(str(seg["data"]["text"]))
                    elif seg.get("type") in ("image", "face", "mface"):
                        parts.append("[图]")
                replies.append("".join(parts))
                print("大肥鱼：" + "".join(parts), flush=True)
                # 分条回复（‖）会连发好几条；AI 回复也可能隔几秒才来。
                # 策略：每收到一条就把截止时间往后推 12 秒，静默 12 秒才退出（总上限 wait）。
                deadline = min(original_deadline, time.time() + 12.0)
            await ws.send_text(json.dumps({
                "status": "ok", "retcode": 0,
                "data": {"message_id": 1, "online": True}, "echo": data.get("echo", ""),
            }, ensure_ascii=False))
        await ws.close()
        return 0 if replies else 1

    return asyncio.run(main())


# ============================================================ main

def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        if SETTINGS_FILE.exists():
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    return settings


def main():
    parser = argparse.ArgumentParser(description="大肥鱼对话测试窗口（假装 NapCat）")
    parser.add_argument("--check", metavar="TEXT", help="无窗口自检：发一句话、打印回复后退出")
    parser.add_argument("--group", type=int, help="群号")
    parser.add_argument("--qq", type=int, help="我的 QQ")
    parser.add_argument("--nick", help="我的昵称")
    parser.add_argument("--private", action="store_true", help="按私聊发送")
    parser.add_argument("--self-id", type=int, dest="self_id",
                        help="伪装成哪个机器人 QQ（默认：窗口 10001、--check 10002）")
    args = parser.parse_args()

    settings = load_settings()
    if args.group:
        settings["group_id"] = args.group
    if args.qq:
        settings["user_id"] = args.qq
    if args.nick:
        settings["nickname"] = args.nick
    if args.private:
        settings["private"] = True

    if args.check:
        return run_check(settings, args.check, args.self_id or 10002)

    try:
        import tkinter as tk
    except Exception as exc:  # noqa: BLE001
        print(f"没有 tkinter，无法开窗口：{exc}\n可以先用 --check 自检。")
        return 1
    root = tk.Tk()
    ChatWindow(root, settings, args.self_id or DEFAULT_SELF_ID)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
