"""数据布局：DataRoot 下的 ``content/``（用户内容）与 ``state/``（机器状态）。

批次 3 只做「解析 + 迁移」，不改任何业务语义。目录约定：

    <DataRoot>/
      content/   人设、记忆、人物档案、日记、长期经历、表情包、图库、B 站笔记、
                 爱好 / 梗 / 关系网 —— 可以整包拷到另一台机器
      state/     配置、会话、心情 / 情绪 / 话题、被动引擎状态、额度表、提醒、日志、
                 同步状态与冲突目录、browser / brain / napcat / backups / logs ——
                 只属于这台机器，跨机器复用没有意义
      .runtime.lock / .locks/ / .data-guard.json   批次 2 基础设施，留在根目录不动

为什么不把这一层的判断散在各模块里：

* **数据根只有一处优先级**：``lifecycle.resolve_data_root``
  （Config.dataDir > PAWBOT_DATA_DIR > <appDir>/data）。这里直接复用它，
  绝不另造一套；``<appDir>/data`` 在批次 2 里已经是指向数据根的目录联接，
  所以 Python 里 ``<base_dir>/data/<x>`` 与 ``<DataRoot>/<x>`` 是同一个位置。
* **旧位置优先读出，绝不假装数据丢了**：每个条目先看 canonical
  （``content/<name>`` 或 ``state/<name>``）；canonical 还不存在、但迁移前的
  旧位置 ``<DataRoot>/<name>`` 还在时，先用旧位置。迁移（``migrate_legacy``）
  跑过一次之后 ``content/`` 与 ``state/`` 就存在了，之后一律走 canonical。
  这条规则同时保住了批次 2 之前写好的配置、临时数据目录和没走 ``run.py``
  的工具脚本。
* **配置键向后兼容**：绝对值原样（显式值优先）；``"data/people"`` 这种旧写法
  按布局表重新落到 ``content/people``；``"content/x"`` / ``"state/x"`` 直接落到
  对应层；其它相对路径仍相对 app 根（和批次 2 一样，例如
  ``tools.bili_watch_dir``）。

关于「content / state 两个目录名能不能配置」：**本批没有加配置键**。
``scripts/architecture-baseline.mjs`` 的 ``python.config.default`` 契约把
``config.DEFAULT`` 顶层段固定成既有集合，新增 ``paths`` 段会直接变红，
而基线脚本不许改。所以两个目录名就是 ``content`` / ``state``。

本模块只在被调用时读文件系统，构造 ``DataPaths`` 不会创建目录；
要建目录请显式用 ``content_dir`` / ``state_dir`` / ``ensure_layout``。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from . import lifecycle

__all__ = [
    "CONTENT_NAMES",
    "STATE_NAMES",
    "SYNC_CONFLICT_PREFIX",
    "DataPaths",
    "PathError",
    "classify",
    "for_bot",
    "from_data_root",
    "migrate_legacy",
    "resolve",
]

CONTENT_DIR = "content"
STATE_DIR = "state"

#: content/ 下的已知条目（可以整包搬走的那一类）
CONTENT_NAMES = (
    "persona.md",
    "persona.bak",
    "self_edit.log",
    "memory",
    "people",
    "diary",
    "longmem",
    "stickers",
    "album",
    "bili_notes",
    "hobbies.json",
    "memes.json",
    "relations.json",
)

#: state/ 下的已知条目（只属于这台机器的那一类）
STATE_NAMES = (
    "config.json",
    "sessions.json",
    "mood.json",
    "emotion.json",
    "topic.json",
    "plugin_state.json",
    "proactive_state.json",
    "media_ledger.json",
    "libido.json",
    "spice.json",
    "chat_window.json",
    "reminders.json",
    "run.log",
    ".sync-state.json",
    "browser",
    "brain",
    "napcat",
    "backups",
    "logs",
)

#: 实时同步的冲突目录是 sync-conflict-<时间戳>，按前缀认
SYNC_CONFLICT_PREFIX = "sync-conflict-"

_CONTENT_SET = frozenset(CONTENT_NAMES)
_STATE_SET = frozenset(STATE_NAMES)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PathError(ValueError):
    """路径名不合法（绝对路径、越界、空段、NUL）。"""


def classify(name: str) -> str | None:
    """一个相对名字属于哪一层：``"content"`` / ``"state"`` / ``None``（不认识）。"""
    head = str(name or "").replace("\\", "/").strip().split("/")[0]
    if not head:
        return None
    if head in _CONTENT_SET:
        return "content"
    if head in _STATE_SET or head.startswith(SYNC_CONFLICT_PREFIX):
        return "state"
    return None


def _parts(name) -> tuple[str, ...]:
    """把用户给的名字切成安全的相对段；不合法就抛 PathError。"""
    raw = str(name or "").replace("\\", "/").strip()
    if not raw:
        return ()
    if "\x00" in raw:
        raise PathError(f"路径里有非法字符：{name!r}")
    if raw.startswith(("/", "~")) or _DRIVE_RE.match(raw) or os.path.isabs(raw):
        raise PathError(f"必须是相对路径：{name!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PathError(f"路径不能越界：{name!r}")
    return tuple(parts)


def _join_inside(base: str, parts: tuple[str, ...]) -> str:
    """拼路径并做越界校验：拼完必须仍在 base 下。"""
    root = os.path.abspath(str(base))
    path = os.path.abspath(os.path.join(root, *parts))
    norm_root = os.path.normcase(root)
    norm = os.path.normcase(path)
    if norm != norm_root and not norm.startswith(norm_root + os.sep):
        raise PathError(f"路径越界：{path} 不在 {root} 下")
    return path


def _expand(name) -> str:
    """配置值规整：去空白、展开 ~（不做绝对路径判断）。"""
    return os.path.expanduser(str(name or "").strip())


@dataclass(frozen=True)
class DataPaths:
    """DataRoot 下的两层布局。``root`` 是真实数据根，``base_dir`` 是 app 目录。"""

    root: str
    base_dir: str = ""
    source: str = "default"
    external: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------ 目录
    @property
    def content_root(self) -> str:
        return os.path.join(self.root, CONTENT_DIR)

    @property
    def state_root(self) -> str:
        return os.path.join(self.root, STATE_DIR)

    def container(self, kind: str = "content") -> str:
        if kind == "content":
            return self.content_root
        if kind == "state":
            return self.state_root
        raise PathError(f"未知的数据层：{kind!r}")

    def adopted(self, kind: str = "content") -> bool:
        """这一层是否已经启用（迁移建过目录就算启用）。"""
        return os.path.isdir(self.container(kind))

    # ------------------------------------------------------------ 单条目解析
    def _entry(self, kind: str, name) -> str:
        parts = _parts(name)
        container = self.container(kind)
        if not parts:
            return container
        canonical = _join_inside(container, parts)
        legacy = _join_inside(self.root, parts)
        if os.path.exists(canonical):
            return canonical
        if os.path.exists(legacy):
            return legacy
        return canonical if self.adopted(kind) else legacy

    def content(self, name: str = "") -> str:
        """content/ 下的路径（不建目录）。"""
        return self._entry("content", name)

    def state(self, name: str = "") -> str:
        """state/ 下的路径（不建目录）。"""
        return self._entry("state", name)

    def minecraft_content(self, name: str = "") -> str:
        """Minecraft 居民的 content/ 路径（不建目录）。"""
        return self.content(os.path.join("minecraft", name))

    def minecraft_state(self, name: str = "") -> str:
        """Minecraft 居民的 state/ 路径（不建目录）。"""
        return self.state(os.path.join("minecraft", name))

    def content_dir(self, name: str = "") -> str:
        """content/ 下的目录，确保存在。"""
        path = self.content(name)
        os.makedirs(path, exist_ok=True)
        return path

    def state_dir(self, name: str = "") -> str:
        """state/ 下的目录，确保存在。"""
        path = self.state(name)
        os.makedirs(path, exist_ok=True)
        return path

    def canonical(self, kind: str, name: str = "") -> str:
        """只算 canonical，不看旧位置（迁移 / 测试用）。"""
        return _join_inside(self.container(kind), _parts(name))

    def legacy(self, name: str = "") -> str:
        """迁移前的旧位置（就在数据根下）。"""
        return _join_inside(self.root, _parts(name))

    def ensure_layout(self) -> tuple[str, str]:
        """建好 content/ 与 state/（已在就什么都不做）。"""
        for path in (self.content_root, self.state_root):
            os.makedirs(path, exist_ok=True)
        return self.content_root, self.state_root

    # ------------------------------------------------------------ 配置值
    def setting(self, raw, *, kind: str = "content", default: str = "") -> str:
        """把配置里的路径值解析成绝对路径。

        - 空值 → 用 ``default``（相对布局层的名字，比如 ``"people"``）；
        - 绝对路径 → 原样（显式值优先）；只有它已经不存在、而迁移后的
          canonical 存在时才改指 canonical（避免旧配置在迁移后指向空位置）；
        - ``"data/x"`` → 按布局表落到 content/state，认不出的 x 仍留在数据根；
        - ``"content/x"`` / ``"state/x"`` → 对应层；
        - 其它相对路径 → 相对 ``base_dir``（与批次 2 一致）。
        """
        value = _expand(raw)
        if not value:
            if not default:
                raise PathError("配置值为空且没有默认目录")
            return self.content(default) if kind == "content" else self.state(default)
        if os.path.isabs(value):
            return self._remap_absolute(os.path.abspath(value))
        norm = value.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        head, _, rest = norm.partition("/")
        if head == "data":
            layer = classify(rest)
            if layer == "content":
                return self.content(rest)
            if layer == "state":
                return self.state(rest)
            # 认不出的旧名字：批次 2 里 <base>/data/<x> 就是 <DataRoot>/<x>，保持原样
            return _join_inside(self.root, _parts(rest)) if rest else self.root
        if head == CONTENT_DIR:
            return self.content(rest) if rest else self.content_root
        if head == STATE_DIR:
            return self.state(rest) if rest else self.state_root
        if not self.base_dir:
            return os.path.abspath(value)
        return os.path.abspath(os.path.join(self.base_dir, value))

    def _remap_absolute(self, path: str) -> str:
        """绝对路径若指向数据根里的已知条目、且迁移后只剩 canonical，就改指 canonical。"""
        try:
            rel = os.path.relpath(path, self.root)
        except ValueError:  # 别的盘
            return path
        if rel == "." or rel.startswith(".."):
            return path
        layer = classify(rel)
        if layer is None or os.path.exists(path):
            return path
        mapped = self.content(rel) if layer == "content" else self.state(rel)
        return mapped

    # ------------------------------------------------------------ 便捷
    def config_file(self) -> str:
        """配置文件位置：``state/config.json``（未迁移时回落到数据根那份）。"""
        return self.state("config.json")

    def info(self) -> dict:
        return {
            "root": self.root,
            "content": self.content_root,
            "state": self.state_root,
            "source": self.source,
            "external": bool(self.external),
        }


def from_data_root(data_root, base_dir: str = "") -> DataPaths:
    """从已经解析好的 ``lifecycle.DataRoot`` 造 DataPaths（不再读 env）。"""
    root = os.path.abspath(str(getattr(data_root, "root", data_root) or "."))
    return DataPaths(
        root=root,
        base_dir=os.path.abspath(str(base_dir or getattr(data_root, "app_data", "") or "")),
        source=str(getattr(data_root, "source", "default") or "default"),
        external=bool(getattr(data_root, "external", False)),
        warnings=tuple(getattr(data_root, "warnings", ()) or ()),
    )


def resolve(base_dir: str, config=None) -> DataPaths:
    """解析数据布局。数据根仍以 ``lifecycle.resolve_data_root`` 为准。

    ``config`` 只用来兜底（目前不参与数据根判断，保留参数是为了调用点统一，
    以后真需要按配置改层名时也只改这一处）。
    """
    base = os.path.abspath(str(base_dir or "."))
    data_root = lifecycle.resolve_data_root(base)
    return from_data_root(data_root, base)


def for_bot(bot) -> DataPaths:
    """拿一个 bot 的布局对象；没有 ``paths`` 属性时按 base_dir 现场解析。"""
    existing = getattr(bot, "paths", None)
    if isinstance(existing, DataPaths):
        return existing
    base = str(getattr(bot, "base_dir", "") or "").strip() or "."
    return resolve(base, getattr(bot, "config", None))


# ---------------------------------------------------------------- 迁移
def _is_link(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        return bool(getattr(os.path, "isjunction", lambda _p: False)(path))
    except OSError:
        return False


def _same_entry(left: str, right: str) -> bool:
    """left 与 right 是不是同一个东西（含我们留下的兼容链接）。"""
    try:
        if os.path.exists(left) and os.path.exists(right) and os.path.samefile(left, right):
            return True
    except OSError:
        pass
    try:
        if _is_link(left):
            return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
                os.path.realpath(right))
    except OSError:
        return False
    return False


def _make_compat_link(source: str, target: str) -> bool:
    """在旧位置留一个指向新位置的链接（尽力而为，失败只当没留）。

    目的：迁移之后，仍按旧路径读写数据的外部脚本 / 老面板 / 上一版 Host
    还能照常打开同一份文件。Windows 上目录用 junction（不需要管理员），
    文件用符号链接（没有权限就算了，迁移本身已经成功）。
    """
    if os.path.lexists(source):
        return False
    is_dir = os.path.isdir(target)
    try:
        if os.name == "nt" and is_dir:
            result = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", source, target],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode == 0 and os.path.exists(source):
                return True
            return False
        os.symlink(target, source, target_is_directory=is_dir)
        return True
    except (OSError, subprocess.SubprocessError, NotImplementedError):
        return False


def _copy_entry(source: str, target: str) -> None:
    """rename 失败时的退化路径：复制过去、**保留来源**（绝不删源）。"""
    if os.path.isdir(source) and not _is_link(source):
        shutil.copytree(source, target, dirs_exist_ok=True)
        return
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)


def known_entries(data_root: str) -> list[tuple[str, str]]:
    """数据根下所有「已知条目」→ [(名字, 层)]；顺带认 sync-conflict-* 目录。"""
    entries = [(name, "content") for name in CONTENT_NAMES]
    entries += [(name, "state") for name in STATE_NAMES]
    try:
        extra = sorted(
            name for name in os.listdir(data_root)
            if name.startswith(SYNC_CONFLICT_PREFIX)
        )
    except OSError:
        extra = []
    entries += [(name, "state") for name in extra]
    return entries


def migrate_legacy(data_root: str, *, link_compat: bool = True) -> dict:
    """把数据根下「旧位置」的已知条目搬进 content/ 或 state/。**幂等**。

    规矩：

    * 只搬白名单（布局表）里的名字；不认识的文件一律不动；
    * 目标已存在时**绝不覆盖**：保留目标、来源留在原处，记 warning 说明没搬；
    * 先在同一文件系统内 ``os.rename``；跨盘/重命名失败退化为 copy + 保留来源；
    * 全程不抛异常：失败只进 ``failed`` 与 ``warnings``，绝不阻断启动；
    * 搬完尽力在旧位置留兼容链接（``link_compat``，失败就算了）。

    返回 ``{"moved": [...], "skipped": [...], "failed": [...], "linked": [...],
    "created": [...], "warnings": [...], "root": ...}``；
    每个条目是 ``{"name", "kind", "source", "target", "mode"/"reason"/"error"}``。
    """
    root = os.path.abspath(str(data_root or "."))
    report: dict = {
        "root": root,
        "content": os.path.join(root, CONTENT_DIR),
        "state": os.path.join(root, STATE_DIR),
        "created": [],
        "moved": [],
        "skipped": [],
        "failed": [],
        "linked": [],
        "warnings": [],
    }
    if not os.path.isdir(root):
        report["warnings"].append(f"数据根不存在，跳过迁移：{root}")
        return report

    # 先把两层目录建好：后面所有解析（含没搬动的条目）都靠它判断「已启用新布局」
    for name in (CONTENT_DIR, STATE_DIR):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            continue
        try:
            os.makedirs(path, exist_ok=True)
            report["created"].append(name)
        except OSError as exc:
            report["failed"].append({"name": name, "kind": "-", "error": str(exc)})
            report["warnings"].append(
                f"{name}/ 建不出来（{type(exc).__name__}: {exc}），本次不迁移任何条目")
            return report

    for name, kind in known_entries(root):
        source = os.path.join(root, name)
        if not os.path.lexists(source):
            continue
        target = os.path.join(os.path.join(root, CONTENT_DIR if kind == "content" else STATE_DIR),
                              name)
        if os.path.lexists(target):
            if _same_entry(source, target):
                # 已经搬过（旧位置是我们留的兼容链接）——重复调用不再有任何动作
                continue
            report["skipped"].append({
                "name": name, "kind": kind, "source": source, "target": target,
                "reason": "target-exists",
            })
            report["warnings"].append(
                f"{name}：{kind}/ 里已经有同名条目，未覆盖；来源仍在 {source}")
            continue

        parent = os.path.dirname(target)
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            report["failed"].append({"name": name, "kind": kind, "error": str(exc)})
            report["warnings"].append(f"{name}：目标目录建不出来（{type(exc).__name__}: {exc}）")
            continue

        mode = "rename"
        try:
            os.rename(source, target)
        except OSError as exc:
            # 跨盘 / 文件被占用：退化成复制，来源留着（宁可多一份，不许丢数据）
            try:
                _copy_entry(source, target)
                mode = "copy"
            except OSError as copy_exc:
                report["failed"].append({
                    "name": name, "kind": kind, "source": source, "target": target,
                    "error": f"rename 失败（{exc}）；copy 也失败（{copy_exc}）",
                })
                report["warnings"].append(
                    f"{name}：搬家失败（{type(exc).__name__}: {exc}），仍留在原处")
                continue

        report["moved"].append({
            "name": name, "kind": kind, "source": source, "target": target, "mode": mode,
        })
        if link_compat and mode == "rename" and _make_compat_link(source, target):
            report["linked"].append(name)

    return report


def summarize(report: dict) -> list[str]:
    """把迁移报告压成几行给人看 / 进日志。"""
    if not isinstance(report, dict):
        return []
    lines: list[str] = []
    moved = report.get("moved") or []
    if moved:
        content = [item["name"] for item in moved if item.get("kind") == "content"]
        state = [item["name"] for item in moved if item.get("kind") == "state"]
        parts = []
        if content:
            parts.append("content/ ← " + "、".join(content))
        if state:
            parts.append("state/ ← " + "、".join(state))
        lines.append(f"数据迁移：搬了 {len(moved)} 项；" + "；".join(parts))
    linked = report.get("linked") or []
    if linked:
        lines.append(f"数据迁移：旧位置留了 {len(linked)} 个兼容链接（老脚本仍可打开）")
    skipped = report.get("skipped") or []
    if skipped:
        lines.append("数据迁移：跳过 " + "、".join(f"{item['name']}（{item.get('reason')}）"
                                                  for item in skipped))
    failed = report.get("failed") or []
    if failed:
        lines.append("数据迁移：失败 " + "、".join(str(item.get("name")) for item in failed))
    if not lines:
        lines.append("数据迁移：没有需要搬的旧条目")
    return lines
