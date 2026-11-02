"""B 站凭证的唯一读写入口（SESSDATA / bili_jct / buvid3 / dedeuserid）。

为什么要单独一层：
  * `bili_scroll`（读字幕、发评论）和 vendored `bili_mcp` 都要凭证，但落点必须是
    **每个 bot 自己的数据根 state/**，不能写进 vendor 包里那份 —— 升级/重装插件会
    把它冲掉，测试也可能不小心改到真凭证。
  * 部分字段更新（比如只补 bili_jct）必须**保留**其它字段。以前整份重写，
    只贴了 SESSDATA 就把 bili_jct 抹了，写操作全部失效。
  * 凭证值只在内存里拼请求，回给调用方的永远只有 bool / 字段名，不回传值。

对外只有几个小函数，全部不抛异常：
    path_for(bot) / load(bot) / merge(fields, bot=..., path=...)
    sessdata_of / jct_of / has_sessdata / has_jct
    sync_from_config(bot) / prepare_vendor(bot) / vendor_path()
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import uuid

from . import paths as layout
from .log import log

FILENAME = "bili_credential.json"

# 同一进程里两个协程/线程同时「读→改→写」会把对方的字段吞掉，合并整段加锁。
_WRITE_LOCK = threading.RLock()

# vendored `bili_mcp` 该读哪份凭证：按**当前任务**记，不改进程级 env。
# 以前 `prepare_vendor` 直接写 `os.environ["PAWBOT_BILI_CREDENTIAL_FILE"]`，
# 一个进程里两个 bot 会互相覆盖 → 用 A 的 cookie 发 B 的评论。
_ACTIVE_VENDOR = contextvars.ContextVar("pawbot_bili_vendor_credential", default="")

# 只认这几个字段；其它键原样保留（向前兼容旧文件里多出来的东西）。
FIELDS = ("sessdata", "bili_jct", "buvid3", "dedeuserid")

_MAX_VALUE = 4096
_MASK = "***已设定***"          # 面板/工具回显用的遮罩，绝不能被当成真值写回去


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _valid(value: str) -> bool:
    """一个 cookie 值该长什么样：非空、无空白、无分号、长度有界、不是遮罩。"""
    if not value or value == _MASK:
        return False
    if len(value) > _MAX_VALUE:
        return False
    if any(ch.isspace() for ch in value) or ";" in value:
        return False
    return True


# ---------------------------------------------------------------- 位置
def path_for(bot=None, path: str | None = None) -> str:
    """凭证文件位置。给了 path 就用 path（测试用），否则按 bot 的数据根算。"""
    if path:
        return os.path.abspath(str(path))
    if bot is not None:
        try:
            return layout.for_bot(bot).state(FILENAME)
        except Exception as e:  # 布局解析失败不该让整轮读片挂掉
            log.warn(f"B站凭证路径解析失败（退回数据根）：{type(e).__name__} {e}")
            root = str(getattr(bot, "base_dir", "") or ".")
            return os.path.join(root, "data", FILENAME)
    return vendor_path()


def vendor_path() -> str:
    """vendored `bili_mcp` 读凭证的位置。

    优先**当前任务**绑定的那份（`prepare_vendor` 设置，contextvar 隔离，多 bot
    同进程不会串号），其次 `PAWBOT_BILI_CREDENTIAL_FILE`（独立脚本/子进程用），
    再其次 `PAWBOT_DATA_DIR` 的 `state/bili_credential.json`，最后才退回 vendor
    包目录里的老位置（只读兼容，新代码不再往那写）。
    """
    active = _text(_ACTIVE_VENDOR.get())
    if active:
        return os.path.abspath(active)
    env = _text(os.environ.get("PAWBOT_BILI_CREDENTIAL_FILE"))
    if env:
        return os.path.abspath(env)
    root = _text(os.environ.get("PAWBOT_DATA_DIR"))
    if root:
        return os.path.join(os.path.abspath(root), "state", FILENAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_mcp", FILENAME)


# ---------------------------------------------------------------- 读
def load(bot=None, path: str | None = None) -> dict:
    """读凭证，只回合法字段组成的 dict。缺文件 / 坏 JSON / 类型不对都回 {}。"""
    target = path_for(bot, path)
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warn(f"B站凭证读不出来，按没凭证处理：{type(e).__name__} {target}")
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for key in FIELDS:
        value = _text(data.get(key))
        if _valid(value):
            out[key] = value
    return out


def _load_raw(bot=None, path: str | None = None) -> dict:
    """合并时用：原样读出所有键（未知键也保留），坏文件按空处理。"""
    target = path_for(bot, path)
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def sessdata_of(bot=None, path: str | None = None) -> str:
    return "".join(load(bot, path).get("sessdata", ""))


def jct_of(bot=None, path: str | None = None) -> str:
    return "".join(load(bot, path).get("bili_jct", ""))


def has_sessdata(bot=None, path: str | None = None) -> bool:
    return bool(sessdata_of(bot, path))


def has_jct(bot=None, path: str | None = None) -> bool:
    return bool(jct_of(bot, path))


# ---------------------------------------------------------------- 写
def merge(fields: dict, bot=None, path: str | None = None) -> dict:
    """把给定的字段合并进凭证文件，**保留**没提到的字段（尤其 bili_jct）。

    返回 {ok, path, changed, kept, reason}。`changed` 只列字段名，不含值。
    非法值会被丢掉：宁可保留旧值，也不把遮罩/带空白的串写进去。
    """
    target = path_for(bot, path)
    changed: list[str] = []
    rejected: list[str] = []
    if isinstance(fields, dict):
        for key, raw in fields.items():
            if key not in FIELDS:
                continue
            value = _text(raw)
            if not value:
                continue
            if not _valid(value):
                rejected.append(key)
                continue
            changed.append(key)
    if not changed:
        return {"ok": False, "path": target, "changed": [],
                "reason": "没有可写入的合法字段",
                "rejected": rejected}

    with _WRITE_LOCK:
        current = _load_raw(bot, path)
        actual: list[str] = []
        for key in changed:
            value = _text(fields.get(key))
            if str(current.get(key) or "").strip() != value:
                current[key] = value
                actual.append(key)
        if not actual:
            kept = [k for k in FIELDS if k in current]
            return {"ok": True, "path": target, "changed": [], "kept": kept,
                    "rejected": rejected, "note": "字段没变，没有写盘"}
        current["updated_at"] = _now_iso()
        try:
            _write_atomic(target, current)
        except OSError as e:
            log.warn(f"B站凭证写入失败：{e}")
            return {"ok": False, "path": target, "changed": [],
                    "reason": f"写入失败：{e}"}
    kept = [k for k in FIELDS if k in current and k not in actual]
    log.info(f"B站凭证已更新字段：{'、'.join(actual)}（保留 {len(kept)} 个）")
    return {"ok": True, "path": target, "changed": actual, "kept": kept,
            "rejected": rejected}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _write_atomic(path: str, data: dict) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            # 凭证被掉电截断 = 主人要重新扫码；先落盘再 replace。
            f.flush()
            _fsync_fd(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(d)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


def _fsync_dir(dirpath: str) -> None:
    """目录项落盘（POSIX 需要）；Windows 打不开目录就跳过。"""
    if not dirpath:
        return
    try:
        fd = os.open(dirpath, os.O_RDONLY)
    except OSError:
        return
    try:
        _fsync_fd(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------- 同步
def _shared_watch_file(bot) -> str:
    """`bili_watch` 共享目录里那份 credential.json（老主人可能只在那贴过）。"""
    try:
        explicit = _text(bot.config.get("tools.bili_watch_credential_file", ""))
    except Exception:
        explicit = ""
    if explicit:
        return explicit
    try:
        d = _text(bot.config.get("tools.bili_watch_dir", ""))
    except Exception:
        d = ""
    if not d:
        d = os.path.join(str(getattr(bot, "base_dir", "") or "."),
                         "pawbot-bili-watch-data")
    return os.path.join(d, "credential.json")


def sync_from_config(bot, path: str | None = None) -> dict:
    """把配置里的 SESSDATA / bili_jct 合并进 bot 自己的凭证文件。

    配置顺序：`tools.bili_sessdata` / `bili.sessdata`，`tools.bili_jct` / `bili.jct`，
    再退到 bili_watch 共享目录那份。返回 merge() 的结果（不含值）；没有配置就
    回 {ok: False, reason: "没有配置凭证"}。
    """
    got: dict = {}
    try:
        for key in ("tools.bili_sessdata", "bili.sessdata"):
            v = _text(bot.config.get(key, ""))
            if v:
                got["sessdata"] = v
                break
        for key in ("tools.bili_jct", "bili.jct"):
            v = _text(bot.config.get(key, ""))
            if v:
                got["bili_jct"] = v
                break
    except Exception as e:
        log.warn(f"读配置里的 B 站凭证失败：{type(e).__name__} {e}")
    if "sessdata" not in got or "bili_jct" not in got:
        try:
            with open(_shared_watch_file(bot), "r", encoding="utf-8") as f:
                shared = json.load(f)
        except Exception:
            shared = {}
        if isinstance(shared, dict):
            if "sessdata" not in got:
                v = _text(shared.get("sessdata"))
                if _valid(v):
                    got["sessdata"] = v
            if "bili_jct" not in got:
                v = _text(shared.get("bili_jct"))
                if _valid(v):
                    got["bili_jct"] = v
    if not got:
        return {"ok": False, "reason": "没有配置凭证", "changed": []}
    return merge(got, bot=bot, path=path)


def prepare_vendor(bot, path: str | None = None) -> str:
    """让 vendored `bili_mcp` 读到**这个 bot** 的凭证：同步一次并返回文件路径。

    绑定的是当前任务（contextvar），不改进程级 env —— 同进程两个 bot 并发时
    各读各的，不会用 A 的 cookie 发 B 的写操作。**绝不写 vendor 包里的老文件**。
    没凭证就回空串。
    """
    target = path_for(bot, path)
    try:
        sync_from_config(bot, path=target)
    except Exception as e:
        log.warn(f"B站凭证同步失败（继续用现有文件）：{type(e).__name__} {e}")
    if not os.path.isfile(target):
        return ""
    try:
        _ACTIVE_VENDOR.set(target)
    except Exception:
        pass
    return target


def status(bot, path: str | None = None) -> dict:
    """只读状态：有哪些字段、文件在哪。不含值，给面板/诊断用。"""
    try:
        data = load(bot, path)
    except Exception:
        data = {}
    return {
        "path": path_for(bot, path),
        "fields": sorted(data.keys()),
        "has_sessdata": bool(data.get("sessdata")),
        "has_bili_jct": bool(data.get("bili_jct")),
    }
