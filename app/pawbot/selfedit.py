"""让她自己改提示词 —— 只改「风格 / 性格」小节，安全规则不在这里。

为什么先做这个而不是「让她改代码」：
    改代码要跑、要回滚、会把自己搞挂；改提示词是**热加载**的，下一句就生效，
    改坏了人还能救回来。但「可编辑的人设」和「不可变的代码策略」必须分开：
    身份、主人、安全、工具权限写在 ``policy.py``，任何改人设的路径都碰不到。

硬约束（写死，不靠提示词）：
    1. 只能改**已经存在**的 `##` 小节，不能新建；候选正文里出现 Markdown
       标题、控制字符、CQ 命令或明显的工具/权限/身份/主人/安全指令一律拒绝。
    2. 「分寸 / 边界 / 未成年 / 底线」以及任何身份、主人、安全、工具、权限
       相关小节永远不可写。允许改哪些风格/性格小节由
       ``selfedit.allowed_sections`` 收紧，没配时用 ``policy`` 的模块常量兜底。
    3. 每次改动：备份 → 审计 intent（fsync）→ CAS 校验 → 原子替换 → 审计
       result（fsync）。审计写不进去就不改；审计日志损坏/未知/有未完成 intent
       时 fail-closed。
    4. 冷却和每日上限**持久化**，重启后仍然生效；同一进程/多进程用锁串行化。
    5. 回滚：备份当前版本 → 校验备份 basename / 非符号链接 / 受保护小节与当前
       一致 → 审计 → 原子替换，绝不把旧备份里的边界改回去。

关于关键词过滤：
    下面的 ``policy.scan_edit_body`` 只是输入卫生的一层，**不是注入问题的完整
    答案**。真正的保证来自代码策略、允许小节白名单、审计、CAS、回滚和权限来源
    校验；不要因为「关键词没命中」就认为外部内容安全。

默认关闭（`selfedit.enabled = false`）。开了以后她在群里说话时才能调
``edit_persona``。自主模式（未来由调度器触发）还必须带
``selfedit.autonomous_grant`` 配置和可信调度器的 provenance，``ctx=None``
不会退化成默认授权。
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

from . import paths as layout
from . import policy
from .log import log

标题行 = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)

# 兼容旧名字：新代码从 policy 读；这里只是避免外部脚本 import 失败。
禁改词 = policy.PROTECTED_TITLE_WORDS
允许小节兜底 = policy.DEFAULT_ALLOWED_SECTIONS

AUDIT_VERSION = 1
DEFAULT_COOLDOWN_SEC = 300.0
DEFAULT_DAILY_LIMIT = 6
DEFAULT_MAX_CHARS = 2000
LOCK_TIMEOUT_SEC = 10.0
LOCK_STALE_SEC = 120.0
MAX_DIFF_CHARS = 8000
MAX_STATE_BYTES = 1024 * 1024

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class AuditError(RuntimeError):
    """审计日志写不进去 / 不可用。按 fail-closed 处理。"""


class JournalError(RuntimeError):
    """审计日志内容未知或损坏。按 fail-closed 处理。"""


class QuotaError(RuntimeError):
    """额度状态损坏。按 fail-closed 处理。"""


def 分节(text: str) -> list[dict]:
    """把 markdown 按 `##` 切开。返回 [{title, start, end, body}]，行号从 0 起。"""
    return policy.split_sections(text)


def 小节标题们(text: str) -> list[str]:
    return [x["title"] for x in 分节(text)]


def _可改吗(标题: str) -> tuple[bool, str]:
    return policy.protected_title(标题)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_dir(path: str) -> None:
    """把目录项也刷盘（Windows 上目录不能 open，忽略即可）。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _atomic_write(path: str, text: str) -> None:
    """唯一临时名 + fsync + 原子替换；失败时原文件不变。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".selfedit-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _read_file(path: str) -> tuple[str, str]:
    with open(path, "rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8")
    # 统一换行符再比较/拼正文：与旧版文本模式读法保持一致，避免 Windows 上
    # CRLF 文件被误判成「内容变了」。哈希仍用原始字节，CAS 才能发现真实外部写入。
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, _sha256_bytes(raw)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class _FileLock:
    """线程 RLock + 跨进程锁目录；拿不到就 fail-closed，不无限等。"""

    def __init__(self, path: str, *, timeout: float = LOCK_TIMEOUT_SEC,
                 stale: float = LOCK_STALE_SEC):
        self.path = str(path)
        self.directory = self.path + ".d"
        self.timeout = max(0.0, float(timeout))
        self.stale = max(1.0, float(stale))
        self._thread_lock: threading.RLock | None = None
        self._held = False

    def _stale(self) -> bool:
        try:
            return time.time() - os.path.getmtime(self.directory) > self.stale
        except OSError:
            return False

    def _clear_stale(self) -> None:
        try:
            shutil.rmtree(self.directory, ignore_errors=True)
        except OSError:
            pass

    def __enter__(self) -> bool:
        key = os.path.normcase(os.path.abspath(self.path))
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        if not lock.acquire(timeout=self.timeout):
            return False
        self._thread_lock = lock
        try:
            os.makedirs(os.path.dirname(self.directory) or ".", exist_ok=True)
        except OSError:
            lock.release()
            self._thread_lock = None
            return False
        deadline = time.time() + self.timeout
        while True:
            try:
                os.mkdir(self.directory)
                self._held = True
                try:
                    with open(os.path.join(self.directory, "owner"), "w",
                              encoding="utf-8") as handle:
                        handle.write(f"pid={os.getpid()} ts={time.time():.3f}\n")
                except OSError:
                    pass
                return True
            except FileExistsError:
                if self._stale():
                    self._clear_stale()
                    continue
                if time.time() >= deadline:
                    lock.release()
                    self._thread_lock = None
                    return False
                time.sleep(0.02)
            except OSError:
                lock.release()
                self._thread_lock = None
                return False

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._held:
            self._held = False
            try:
                shutil.rmtree(self.directory, ignore_errors=True)
            except OSError:
                pass
        if self._thread_lock is not None:
            try:
                self._thread_lock.release()
            except RuntimeError:
                pass
            self._thread_lock = None
        return False


class SelfEditor:
    def __init__(self, bot):
        self.bot = bot
        self.paths = layout.for_bot(bot)
        # 改的就是 Persona 读的那份人设（content/persona.md），备份/审计也放 content/
        self.path = self.paths.setting(bot.config.get("persona.file", ""),
                                       kind="content", default="persona.md")
        self.bak_dir = self.paths.content("persona.bak")
        self.log_path = self.paths.content("self_edit.log")
        # 机器审计 / 额度是「只属于这台机器」的状态，放 state/；不和人设一起同步。
        self.audit_path = self.paths.state("selfedit/audit.jsonl")
        self.state_path = self.paths.state("selfedit/state.json")
        self.lock_path = self.paths.state("selfedit/edit.lock")

    # ---------------------------------------------------------- 配置
    def _cfg(self, key, default=None):
        return self.bot.config.get(f"selfedit.{key}", default)

    def _cfg_float(self, key, default: float) -> float:
        value = self._cfg(key, None)
        if value is None:
            return float(default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _cfg_int(self, key, default: int) -> int:
        value = self._cfg(key, None)
        if value is None:
            return int(default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enabled", False))

    # ---------------------------------------------------------- 读
    def 读(self) -> str:
        try:
            return _read_file(self.path)[0]
        except (OSError, UnicodeError):
            return ""

    def 概览(self) -> list[dict]:
        text = self.读()
        titles = 小节标题们(text)
        allowed, _why = policy.allowed_sections(self.bot.config, titles)
        out: list[dict] = []
        for section in 分节(text):
            protected_ok, _ = _可改吗(section["title"])
            editable = protected_ok and section["title"] in allowed
            out.append({
                "title": section["title"],
                "chars": len(section["body"]),
                "可改": editable,
                "允许编辑": editable,
                "受保护": not protected_ok,
            })
        return out

    # ---------------------------------------------------------- 额度（持久化）
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _empty_quota(self) -> dict:
        return {
            "v": 1,
            "date": self._today(),
            "count": 0,
            "sections": {},
            "updated_at": time.time(),
        }

    def _load_quota(self) -> dict:
        if not os.path.exists(self.state_path):
            return self._empty_quota()
        try:
            if os.path.getsize(self.state_path) > MAX_STATE_BYTES:
                raise QuotaError("额度状态文件过大")
            with open(self.state_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except QuotaError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise QuotaError(f"额度状态损坏：{exc}") from exc
        if not isinstance(raw, dict) or raw.get("v") != 1:
            raise QuotaError("额度状态版本未知")
        date = raw.get("date")
        count = raw.get("count")
        sections = raw.get("sections")
        if not isinstance(date, str) or not isinstance(count, int) or count < 0:
            raise QuotaError("额度状态字段不合法")
        if not isinstance(sections, dict):
            raise QuotaError("额度状态 sections 不合法")
        clean: dict[str, float] = {}
        for key, value in sections.items():
            if not isinstance(key, str) or not isinstance(value, (int, float)):
                raise QuotaError("额度状态 sections 里有非法值")
            clean[key] = float(value)
        state = {
            "v": 1,
            "date": date,
            "count": count,
            "sections": clean,
            "updated_at": float(raw.get("updated_at") or 0),
        }
        if date != self._today():
            return self._empty_quota()
        return state

    def _save_quota(self, state: dict) -> None:
        _atomic_write(self.state_path,
                      json.dumps(state, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")) + "\n")

    def _预约配额(self, state: dict, section: str, now: float) -> dict:
        reserved = copy.deepcopy(state)
        if reserved.get("date") != self._today():
            reserved = self._empty_quota()
        reserved["date"] = self._today()
        reserved["count"] = int(reserved.get("count") or 0) + 1
        reserved.setdefault("sections", {})[section] = float(now)
        reserved["updated_at"] = float(now)
        return reserved

    def _恢复配额(self, previous: dict | None) -> None:
        if previous is None:
            return
        try:
            self._save_quota(previous)
        except OSError:
            # 恢复失败只会保守多记一次额度，不会放宽限制。
            pass

    def _检查额度(self, section: str, now: float, state: dict) -> tuple[bool, str]:
        cooldown = max(0.0, self._cfg_float("cooldown_sec", DEFAULT_COOLDOWN_SEC))
        if cooldown > 0:
            last = float(state.get("sections", {}).get(section, 0) or 0)
            if last and now - last < cooldown:
                return False, f"「{section}」刚改过，再等 {int(cooldown - (now - last))} 秒"
        limit = self._cfg_int("daily_limit", DEFAULT_DAILY_LIMIT)
        if limit < 0:
            limit = 0
        count = int(state.get("count") or 0)
        if limit > 0 and count >= limit:
            return False, (
                f"今天已经改过 {count} 次，到上限了（selfedit.daily_limit={limit}）"
            )
        return True, ""

    # ---------------------------------------------------------- 审计日志
    def _journal_records(self) -> list[dict]:
        if not os.path.exists(self.audit_path):
            return []
        try:
            with open(self.audit_path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise JournalError(f"审计日志读不了：{exc}") from exc
        if not raw.strip():
            return []
        if not raw.endswith(b"\n"):
            raise JournalError("审计日志最后一行不完整（可能写到一半）")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise JournalError(f"审计日志不是 UTF-8：{exc}") from exc
        records: list[dict] = []
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                raise JournalError(f"审计日志第 {line_no} 行是空行，按损坏处理")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalError(f"审计日志第 {line_no} 行不是合法 JSON") from exc
            self._validate_record(record, line_no)
            records.append(record)
        pending: dict[str, dict] = {}
        for record in records:
            txn = record["txn"]
            if record["event"] == "intent":
                if txn in pending:
                    raise JournalError(f"审计日志有重复的 intent：{txn}")
                pending[txn] = record
            else:
                intent = pending.pop(txn, None)
                if intent is None:
                    raise JournalError(f"审计日志有找不到 intent 的 result：{txn}")
                if intent["op"] != record["op"]:
                    raise JournalError(f"审计日志 intent/result 操作不一致：{txn}")
        if pending:
            raise JournalError(
                f"审计日志有 {len(pending)} 个未完成的操作；结果未确认，拒绝改动"
            )
        return records

    @staticmethod
    def _validate_record(record, line_no: int) -> None:
        if not isinstance(record, dict):
            raise JournalError(f"审计日志第 {line_no} 行不是对象")
        if record.get("v") != AUDIT_VERSION:
            raise JournalError(f"审计日志第 {line_no} 行版本未知")
        event = record.get("event")
        if event not in ("intent", "result"):
            raise JournalError(f"审计日志第 {line_no} 行事件未知：{event!r}")
        txn = record.get("txn")
        if not isinstance(txn, str) or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", txn):
            raise JournalError(f"审计日志第 {line_no} 行 txn 不合法")
        if record.get("op") not in ("edit", "rollback"):
            raise JournalError(f"审计日志第 {line_no} 行 op 未知")
        for key in ("before_sha256", "after_sha256"):
            value = record.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise JournalError(f"审计日志第 {line_no} 行 {key} 不合法")
        for key in ("source", "reason", "diff"):
            if not isinstance(record.get(key), str):
                raise JournalError(f"审计日志第 {line_no} 行缺少字符串字段 {key}")
        if event == "result" and not isinstance(record.get("ok"), bool):
            raise JournalError(f"审计日志第 {line_no} 行 result 缺少 ok")

    def _append_journal(self, record: dict) -> None:
        payload = dict(record)
        payload.setdefault("v", AUDIT_VERSION)
        payload.setdefault("ts", time.time())
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n"
        directory = os.path.dirname(self.audit_path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd = os.open(self.audit_path,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            raise AuditError(f"审计日志打不开：{exc}") from exc
        try:
            with os.fdopen(fd, "a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise AuditError(f"审计日志写入失败：{exc}") from exc
        _fsync_dir(directory)

    def _ensure_journal_ok(self) -> None:
        self._journal_records()

    # ---------------------------------------------------------- 写入辅助
    def _diff(self, old: str, new: str, section: str) -> str:
        diff = "".join(difflib.unified_diff(
            str(old).splitlines(keepends=True),
            str(new).splitlines(keepends=True),
            fromfile=f"persona/{section}（改前）",
            tofile=f"persona/{section}（改后）",
        ))
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n…（diff 已截断）"
        return diff

    def _备份(self, 正文: str) -> str:
        os.makedirs(self.bak_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        fingerprint = _sha256_text(正文)[:8]
        base = f"{stamp}-{fingerprint}"
        path = os.path.join(self.bak_dir, base + ".md")
        if os.path.exists(path):
            path = os.path.join(self.bak_dir, f"{base}-{uuid.uuid4().hex[:8]}.md")
        _atomic_write(path, 正文)
        return path

    def _记账(self, 小节: str, 原因: str, 旧: str, 新: str, 备份: str,
              来源: str = "") -> bool:
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{小节}\t"
                f"{原因 or '（没写原因）'}\t{len(旧)}→{len(新)} 字\t"
                f"备份 {os.path.basename(备份) or '（无）'}")
        if 来源:
            line += f"\t来源 {来源}"
        line += "\n"
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except OSError:
            return False

    def _允许改(self, 标题: str, 标题们: list[str]) -> tuple[bool, str]:
        allowed, why = policy.allowed_sections(self.bot.config, 标题们)
        if why:
            return False, why
        if 标题 not in allowed:
            return False, (
                f"「{标题}」不在允许编辑的风格/性格小节里"
                f"（selfedit.allowed_sections：{'、'.join(allowed) or '（空）'}）"
            )
        return True, ""

    def _存储安全(self) -> tuple[bool, str]:
        """目标文件/备份目录不能是符号链接，避免写到人设之外。"""
        try:
            if os.path.islink(self.path):
                return False, "persona.md 是符号链接，拒绝写入"
            if os.path.isdir(self.path):
                return False, "persona.md 是目录，拒绝写入"
            if os.path.islink(self.bak_dir):
                return False, "备份目录是符号链接，拒绝写入"
        except OSError:
            return False, "检查 persona.md / 备份目录失败"
        return True, ""

    def _自主模式要求显式来源(self) -> bool:
        # 未来调度器打开 selfedit.autonomous 后，漏传 provenance 不再退回默认。
        return bool(self._cfg("autonomous", False))

    def _记账可写(self) -> tuple[bool, str]:
        """落盘前先确认人类可读审计文件能打开；打不开就不改。"""
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            if os.path.isdir(self.log_path):
                return False, "审计日志路径是目录，拒绝改动"
            with open(self.log_path, "a", encoding="utf-8", newline=""):
                pass
        except OSError as exc:
            return False, f"审计日志不可写，拒绝改动：{exc}"
        return True, ""

    # ---------------------------------------------------------- 改
    def 改(self, 小节: str, 新正文: str, 原因: str = "",
           provenance=None) -> dict:
        """改一节。返回 {ok, reason, chars}。任何一步不满足都不写盘。"""
        if not self.enabled:
            return {"ok": False, "reason": "自主改提示词没开（selfedit.enabled=false）"}

        标题 = str(小节 or "").strip().lstrip("#").strip()
        正文 = str(新正文 or "").strip()
        if not 标题:
            return {"ok": False, "reason": "没说改哪一节"}
        if not 正文:
            return {"ok": False, "reason": "新内容不能是空的（想删就说清楚，别清空）"}
        max_chars = int(self._cfg("max_chars", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS)
        if len(正文) > max_chars:
            return {"ok": False, "reason": "这一节太长了（超过 selfedit.max_chars）"}

        可, why = _可改吗(标题)
        if not 可:
            return {"ok": False, "reason": why}
        可, why = self._存储安全()
        if not 可:
            return {"ok": False, "reason": why}
        可, why = self._记账可写()
        if not 可:
            return {"ok": False, "reason": why}

        try:
            prov = policy.authorize_provenance(
                provenance, self.bot.config,
                require_explicit=self._自主模式要求显式来源(),
            )
        except policy.PolicyError as exc:
            return {"ok": False, "reason": f"改不了：{exc}"}

        txn = uuid.uuid4().hex
        with _FileLock(self.lock_path,
                       timeout=self._cfg_float("lock_timeout_sec", LOCK_TIMEOUT_SEC)) as locked:
            if not locked:
                return {"ok": False, "reason": "另一处正在改人设，等一下再试"}

            try:
                self._ensure_journal_ok()
            except JournalError as exc:
                return {"ok": False, "reason": f"审计日志不可用，拒绝改动：{exc}"}

            try:
                现在文本, 旧哈希 = _read_file(self.path)
            except FileNotFoundError:
                现在文本, 旧哈希 = "", _sha256_bytes(b"")
            except (OSError, UnicodeError) as exc:
                return {"ok": False, "reason": f"读不了 persona.md：{exc}"}

            节们 = 分节(现在文本)
            标题们 = [x["title"] for x in 节们]
            目标 = next((x for x in 节们 if x["title"] == 标题), None)
            if 目标 is None:
                return {"ok": False,
                        "reason": f"没有「{标题}」这一节。只能改已有的："
                                  f"{'、'.join(标题们)}"}

            可, why = self._允许改(标题, 标题们)
            if not 可:
                return {"ok": False, "reason": why}
            可, why = policy.scan_edit_body(正文)
            if not 可:
                return {"ok": False, "reason": why}

            # 拼新全文：只替换这一节的正文，其余逐字节保留
            行们 = 现在文本.splitlines()
            新行们 = (行们[:目标["start"] + 1] + [""] + 正文.splitlines()
                      + [""] + 行们[目标["end"]:])
            新文本 = "\n".join(新行们).rstrip() + "\n"
            if 新文本 == 现在文本:
                return {"ok": False, "reason": "内容没变，不用改"}

            现在 = time.time()
            try:
                配额 = self._load_quota()
            except QuotaError as exc:
                return {"ok": False, "reason": f"额度状态不可用，拒绝改动：{exc}"}
            可, why = self._检查额度(标题, 现在, 配额)
            if not 可:
                return {"ok": False, "reason": why}
            前配额 = copy.deepcopy(配额)
            try:
                self._save_quota(self._预约配额(配额, 标题, 现在))
            except OSError as exc:
                return {"ok": False, "reason": f"额度状态写不进，改动已放弃：{exc}"}

            备份 = None
            已写 = False
            after_hash = _sha256_text(新文本)
            diff = self._diff(目标["body"], 正文, 标题)
            try:
                备份 = self._备份(现在文本)
                self._append_journal({
                    "v": AUDIT_VERSION,
                    "event": "intent",
                    "txn": txn,
                    "op": "edit",
                    "section": 标题,
                    "source": prov["source"],
                    "actor": prov.get("actor", ""),
                    "mode": prov.get("mode", ""),
                    "grant_id": prov.get("grant_id", ""),
                    "reason": str(原因 or ""),
                    "before_sha256": 旧哈希,
                    "after_sha256": after_hash,
                    "backup": os.path.basename(备份),
                    "diff": diff,
                })

                # CAS：审计 intent 之后、真正替换之前再确认一次当前文件没被
                # 别的进程/同步器改过。外部写入优先，放弃我们的修改。
                try:
                    最新文本, 最新哈希 = _read_file(self.path)
                except FileNotFoundError:
                    最新文本, 最新哈希 = "", _sha256_bytes(b"")
                if 最新哈希 != 旧哈希:
                    self._append_journal({
                        "v": AUDIT_VERSION,
                        "event": "result",
                        "txn": txn,
                        "op": "edit",
                        "ok": False,
                        "section": 标题,
                        "source": prov["source"],
                        "reason": "CAS 冲突：persona.md 在改动前被其他程序修改",
                        "before_sha256": 旧哈希,
                        "after_sha256": after_hash,
                        "observed_sha256": 最新哈希,
                        "diff": diff,
                    })
                    self._恢复配额(前配额)
                    return {"ok": False,
                            "reason": "persona.md 刚被其他程序改过（CAS 冲突），这次不写"}

                _atomic_write(self.path, 新文本)
                已写 = True
                self._append_journal({
                    "v": AUDIT_VERSION,
                    "event": "result",
                    "txn": txn,
                    "op": "edit",
                    "ok": True,
                    "section": 标题,
                    "source": prov["source"],
                    "reason": str(原因 or ""),
                    "before_sha256": 旧哈希,
                    "after_sha256": after_hash,
                    "diff": diff,
                })
            except AuditError as exc:
                if 已写:
                    try:
                        _atomic_write(self.path, 现在文本)
                    except OSError:
                        pass
                self._恢复配额(前配额)
                return {"ok": False, "reason": f"审计写入失败，改动已放弃：{exc}"}
            except OSError as exc:
                if 已写:
                    try:
                        _atomic_write(self.path, 现在文本)
                    except OSError:
                        pass
                self._恢复配额(前配额)
                return {"ok": False, "reason": f"写不进 persona.md：{exc}"}

        self._记账(标题, 原因, 目标["body"], 正文, 备份 or "", prov["source"])
        log.info(f"她自己改了人格「{标题}」：{len(目标['body'])}→{len(正文)} 字｜{原因[:40]}")
        return {"ok": True, "section": 标题, "before": len(目标["body"]),
                "after": len(正文), "backup": os.path.basename(备份 or ""),
                "note": "下一句就生效，不用重启"}

    # ---------------------------------------------------------- 回滚
    def _合法备份名(self, 名: str) -> tuple[bool, str]:
        if not 名 or 名 != os.path.basename(名) or 名 in (".", ".."):
            return False, "备份名不合法"
        if re.search(r'[\\/:*?"<>|\x00-\x1f]', 名):
            return False, "备份名不合法"
        return True, ""

    def _安全备份文件(self, 源: str) -> tuple[bool, str]:
        try:
            if os.path.islink(源):
                return False, "备份是符号链接，拒绝回滚"
            real_dir = os.path.realpath(self.bak_dir)
            real_src = os.path.realpath(源)
            common = os.path.commonpath([real_dir, real_src])
            if os.path.normcase(common) != os.path.normcase(real_dir):
                return False, "备份路径越界，拒绝回滚"
        except (OSError, ValueError):
            return False, "备份路径不合法"
        if not os.path.isfile(源):
            return False, "找不到这个备份"
        return True, ""

    def 回滚(self, 备份名: str, provenance=None) -> dict:
        """把某一版备份盖回来（人用，不是她用的）。

        校验 basename / 非符号链接 / 受保护小节与当前一致；备份当前版本后再
        原子替换，并把 intent/result 写进审计日志。
        """
        名 = str(备份名 or "")
        可, why = self._合法备份名(名)
        if not 可:
            return {"ok": False, "reason": why}
        可, why = self._存储安全()
        if not 可:
            return {"ok": False, "reason": why}
        可, why = self._记账可写()
        if not 可:
            return {"ok": False, "reason": why}
        try:
            prov = policy.authorize_provenance(
                provenance, self.bot.config,
                require_explicit=self._自主模式要求显式来源(),
            )
        except policy.PolicyError as exc:
            return {"ok": False, "reason": f"回滚不了：{exc}"}

        源 = os.path.join(self.bak_dir, 名)
        txn = uuid.uuid4().hex
        with _FileLock(self.lock_path,
                       timeout=self._cfg_float("lock_timeout_sec", LOCK_TIMEOUT_SEC)) as locked:
            if not locked:
                return {"ok": False, "reason": "另一处正在改人设，等一下再试"}

            try:
                self._ensure_journal_ok()
            except JournalError as exc:
                return {"ok": False, "reason": f"审计日志不可用，拒绝回滚：{exc}"}

            可, why = self._安全备份文件(源)
            if not 可:
                return {"ok": False, "reason": why}
            try:
                现在文本, 旧哈希 = _read_file(self.path)
            except FileNotFoundError:
                现在文本, 旧哈希 = "", _sha256_bytes(b"")
            except (OSError, UnicodeError) as exc:
                return {"ok": False, "reason": f"读不了 persona.md：{exc}"}
            try:
                文本 = _read_text(源)
            except (OSError, UnicodeError) as exc:
                return {"ok": False, "reason": f"读不了备份：{exc}"}

            可, why = policy.verify_rollback_preserves_policy(现在文本, 文本)
            if not 可:
                return {"ok": False, "reason": why}
            if 文本 == 现在文本:
                return {"ok": False, "reason": "内容没变，不用回滚"}

            after_hash = _sha256_text(文本)
            diff = self._diff(现在文本, 文本, "（回滚）")
            备份 = None
            已写 = False
            try:
                备份 = self._备份(现在文本)
                self._append_journal({
                    "v": AUDIT_VERSION,
                    "event": "intent",
                    "txn": txn,
                    "op": "rollback",
                    "section": "（回滚）",
                    "source": prov["source"],
                    "actor": prov.get("actor", ""),
                    "mode": prov.get("mode", ""),
                    "grant_id": prov.get("grant_id", ""),
                    "reason": f"回到 {名}",
                    "before_sha256": 旧哈希,
                    "after_sha256": after_hash,
                    "backup": os.path.basename(备份),
                    "diff": diff,
                })
                try:
                    最新文本, 最新哈希 = _read_file(self.path)
                except FileNotFoundError:
                    最新文本, 最新哈希 = "", _sha256_bytes(b"")
                if 最新哈希 != 旧哈希:
                    self._append_journal({
                        "v": AUDIT_VERSION,
                        "event": "result",
                        "txn": txn,
                        "op": "rollback",
                        "ok": False,
                        "section": "（回滚）",
                        "source": prov["source"],
                        "reason": "CAS 冲突：persona.md 在回滚前被其他程序修改",
                        "before_sha256": 旧哈希,
                        "after_sha256": after_hash,
                        "observed_sha256": 最新哈希,
                        "diff": diff,
                    })
                    return {"ok": False,
                            "reason": "persona.md 刚被其他程序改过（CAS 冲突），这次不回滚"}

                _atomic_write(self.path, 文本)
                已写 = True
                self._append_journal({
                    "v": AUDIT_VERSION,
                    "event": "result",
                    "txn": txn,
                    "op": "rollback",
                    "ok": True,
                    "section": "（回滚）",
                    "source": prov["source"],
                    "reason": f"回到 {名}",
                    "before_sha256": 旧哈希,
                    "after_sha256": after_hash,
                    "diff": diff,
                })
            except AuditError as exc:
                if 已写:
                    try:
                        _atomic_write(self.path, 现在文本)
                    except OSError:
                        pass
                return {"ok": False, "reason": f"审计写入失败，回滚已放弃：{exc}"}
            except OSError as exc:
                if 已写:
                    try:
                        _atomic_write(self.path, 现在文本)
                    except OSError:
                        pass
                return {"ok": False, "reason": f"回滚失败：{exc}"}

        self._记账("（回滚）", f"回到 {名}", 现在文本, 文本, 备份 or "", prov["source"])
        return {"ok": True,
                "note": f"已回滚到 {名}；回滚前的版本存在 {os.path.basename(备份 or '')}"}

    def 历史(self, limit: int = 20, provenance=None) -> list[str]:
        try:
            with open(self.log_path, "r", encoding="utf-8") as handle:
                return [line.rstrip("\n") for line in handle if line.strip()][-limit:]
        except OSError:
            return []


def _指纹(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:8]
