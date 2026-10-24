"""
插件系统：装饰器注册 + 目录扫描 + 热重载。

写一个插件只要三行：

    from pawbot.plugin import on_command
    @on_command("ping")
    async def ping(ctx):
        await ctx.reply("pong")

放进 plugins/ 目录，WebUI 点一下「重载」就生效，不用重启进程。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import sys
import traceback
from typing import Callable

from .bus import BUS, Handler
from .log import log


def _caller_module() -> str:
    frame = inspect.currentframe()
    try:
        while frame:
            name = frame.f_globals.get("__name__", "")
            if name != __name__:
                return name
            frame = frame.f_back
        return ""
    finally:
        del frame


def _wrap(fn, kind: str, **kw) -> Handler:
    module = _caller_module()
    h = Handler(fn=fn, kind=kind, module=module, **kw)
    BUS.register(h)
    return h


# ---------------------------------------------------------------- 装饰器
def on_command(*names: str, aliases: tuple[str, ...] = (), priority: int = 10, **kw):
    """指令：/ping 或（allow_no_prefix 时）直接 ping"""
    def deco(fn):
        _wrap(fn, "command", commands=tuple(names), aliases=aliases,
              priority=priority, **kw)
        return fn
    return deco


def on_keyword(keyword: str, priority: int = 40, **kw):
    """消息里包含关键词就触发"""
    def deco(fn):
        _wrap(fn, "keyword", keyword=keyword, priority=priority, **kw)
        return fn
    return deco


def on_regex(pattern: str, flags: int = 0, priority: int = 30, **kw):
    def deco(fn):
        _wrap(fn, "regex", pattern=re.compile(pattern, flags), priority=priority, **kw)
        return fn
    return deco


def on_message(priority: int = 90, **kw):
    """所有消息（兜底，通常给 LLM 用）"""
    def deco(fn):
        _wrap(fn, "message", priority=priority, **kw)
        return fn
    return deco


def on_notice(notice_types: tuple[str, ...] = (), priority: int = 20, **kw):
    def deco(fn):
        _wrap(fn, "notice", notice_types=notice_types, priority=priority, **kw)
        return fn
    return deco


def on_request(priority: int = 20, **kw):
    def deco(fn):
        _wrap(fn, "request", priority=priority, **kw)
        return fn
    return deco


# ---------------------------------------------------------------- 加载器
class PluginManager:
    def __init__(self, plugin_dir: str, state_path: str):
        self.plugin_dir = plugin_dir
        self.state_path = state_path
        self.state: dict[str, bool] = {}
        self.loaded: dict[str, dict] = {}     # module_name -> meta
        self._load_state()

    # ---- 状态持久化
    def _load_state(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
            except Exception:
                self.state = {}
        else:
            self._save_state()      # 首次运行落一份，方便你直接编辑

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ---- 扫描
    def scan(self) -> list[str]:
        if not os.path.isdir(self.plugin_dir):
            return []
        return sorted(
            f[:-3] for f in os.listdir(self.plugin_dir)
            if f.endswith(".py") and not f.startswith("_")
        )

    # ---- 载入单个
    def load(self, name: str) -> bool:
        path = os.path.join(self.plugin_dir, f"{name}.py")
        if not os.path.exists(path):
            log.warn(f"插件不存在：{name}")
            return False
        if not self.state.get(name, True):
            log.debug(f"插件已禁用，跳过：{name}")
            return False

        self.unload(name)

        mod_name = f"pawbot_plugin_{name}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)          # 装饰器在这里执行
        except Exception as e:
            log.error(f"插件加载失败 {name}: {type(e).__name__} {e}")
            traceback.print_exc()
            sys.modules.pop(mod_name, None)
            return False

        desc = getattr(module, "__description__", "") or ""
        if not desc and module.__doc__:
            desc = module.__doc__.strip().splitlines()[0]
        meta = {
            "name": name,
            "title": getattr(module, "__plugin_name__", name),
            "version": getattr(module, "__version__", "0.0.0"),
            "description": desc,
            "handlers": len(BUS.by_module(mod_name)),
            "enabled": True,
            "error": "",
        }
        self.loaded[mod_name] = meta
        log.info(f"插件已加载：{meta['title']} v{meta['version']} "
                 f"（{meta['handlers']} 个处理器）")
        return True

    # ---- 卸载
    def unload(self, name: str):
        mod_name = f"pawbot_plugin_{name}"
        removed = BUS.remove_module(mod_name)
        self.loaded.pop(mod_name, None)
        sys.modules.pop(mod_name, None)
        if removed:
            log.debug(f"卸载插件 {name}（移除 {removed} 个处理器）")

    # ---- 全部重载
    def reload_all(self) -> dict:
        for name in self.scan():
            self.unload(name)
        ok, fail = [], []
        for name in self.scan():
            (ok if self.load(name) else fail).append(name)
        return {"loaded": ok, "failed": fail}

    # ---- 启用 / 禁用
    def set_enabled(self, name: str, enabled: bool):
        self.state[name] = enabled
        self._save_state()
        if enabled:
            self.load(name)
        else:
            self.unload(name)

    # ---- 列表
    def list(self) -> list[dict]:
        rows = []
        for name in self.scan():
            mod_name = f"pawbot_plugin_{name}"
            meta = self.loaded.get(mod_name)
            if meta:
                rows.append({**meta, "enabled": self.state.get(name, True)})
            else:
                rows.append({
                    "name": name, "title": name, "version": "-",
                    "description": "（未加载）", "handlers": len(BUS.by_module(mod_name)),
                    "enabled": self.state.get(name, True), "error": "",
                })
        return rows
