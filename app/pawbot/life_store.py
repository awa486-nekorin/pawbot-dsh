"""自主生活与成长的基础状态层（LifeStore v1）。

这个模块只负责“记得住、改得准、找得到”的持久状态，不生成剧情、不调用模型、
不读人设正文，也不碰任何运行态文件。调用方给什么经历就记什么；没有经历时
状态就是空的。

设计要点
--------
* 持久化只有一份：``paths.content('life/state.json')``。构造只读，真正写入时才
  建目录；写盘用同目录唯一临时文件 + fsync + ``os.replace``。
* 每个公开方法都返回深拷贝；调用方改返回值不会改到内部状态。
* 进程内用 RLock（同一路径的多个实例共享同一把锁）；每次操作前检查文件戳，
  别的实例/HA 刷新写了盘就先重读，避免用旧内存覆盖新文件。
* 条目有 ``version``；``patch_goal`` / ``patch_self`` 的 ``expected_version``
  是局部 CAS：除 ``add`` 外所有已有条目的变更都必须带版本，版本对不上就不写，
  其他条目原样保留。``patch_self`` 的 ``merge`` 同时改两个条目，还必须用
  ``fields.expected_other_version`` 校验另一条目，任何一边冲突都整体拒绝。
* 删除/退役只做 tombstone。默认投影、召回、上下文都排除；``delete`` 会把正文从
  当前文件里真正抹掉，避免“删了还能从历史里复活”。
* 六类逻辑记忆：working / episodic / semantic / common / procedural / candidate。
  候选带状态；冲突不静默覆盖；检索不增加置信度；提及时间与有效时间分开。
* 养成把“领域了解”和“实践经验”分开记。同一经历同一能力只计一次；只有实际
  层级足够高的经历才能增加实践。token、emoji、米饭图片都不产生任何资源或熟练度。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid

from . import paths as layout

__all__ = ["LifeStore", "LifeStoreError", "LifeDataError"]

STATE_VERSION = 1
STATE_NAME = "life/state.json"

MEMORY_KINDS = (
    "working",
    "episodic",
    "semantic",
    "common",
    "procedural",
    "candidate",
)

_KIND_ALIASES = {
    "working": "working",
    "working_memory": "working",
    "工作": "working",
    "工作记忆": "working",
    "episode": "episodic",
    "episodic": "episodic",
    "情景": "episodic",
    "情景记忆": "episodic",
    "semantic": "semantic",
    "fact": "semantic",
    "语义": "semantic",
    "语义记忆": "semantic",
    "common": "common",
    "common_sense": "common",
    "common_rule": "common",
    "rule": "common",
    "rules": "common",
    "常识": "common",
    "常识规则": "common",
    "procedural": "procedural",
    "procedure": "procedural",
    "skill": "procedural",
    "程序": "procedural",
    "程序记忆": "procedural",
    "candidate": "candidate",
    "candidate_high_value": "candidate",
    "候选": "candidate",
    "候选高价值": "candidate",
}

_GOAL_TYPES = {
    "wish": "wish",
    "愿望": "wish",
    "obsession": "obsession",
    "执念": "obsession",
    "task": "task",
    "todo": "task",
    "project": "task",
    "任务": "task",
    "未完事项": "task",
}

_GOAL_STATUS = {"active", "waiting", "paused", "completed", "abandoned", "deleted"}
_GOAL_TERMINAL = {"completed", "abandoned", "deleted"}

_MEMORY_HIDDEN = {"retired", "deleted", "superseded", "merged"}

# 这些二元组在中文里出现得太频繁，撞上它们不代表相关。
_WEAK_TOKENS = frozenset("""
什么 意思 啥意 是啥 你们 我们 他们 她们 这个 那个 怎么 为什 一下 一个 是不 不是
可以 还是 就是 但是 然后 因为 所以 如果 的话 一点 有点 没有 知道 觉得 现在 今天
说的 说什 帮我 给我 我的 你的 这是 那是 好的 好吧 了吗 呢呢 在吗 在不在 你好
""".split())

# 观察层级：标题/字幕/简介/搜索摘要以及全文、阅读、笔记、转录都属于“了解”，
# 不能当成操作能力。低层级优先，避免“字幕里提到 practiced”被误判为实践。
_LOW_LEVEL_MARKERS = (
    "title", "subtitle", "desc", "description", "summary", "search", "metadata",
    "index", "cover", "preview", "thumbnail", "headline", "only",
    "read", "full", "article", "notes", "note", "transcript",
    "watching", "watched", "viewing", "viewed",
    "标题", "字幕", "简介", "摘要", "搜索", "封面", "预览", "全文", "阅读",
    "读了", "读完", "笔记", "转录", "观看", "看过",
)
# 只有明确真实执行/练习/实验/完成作品的层级才能增加实践经验。
_HIGH_LEVEL_MARKERS = (
    "tried", "try", "practice", "practiced", "hands_on", "handson", "executed",
    "experiment", "project", "built", "build", "tested", "completed", "made",
    "created",
    "试过", "练习", "实践", "动手", "实验", "项目", "完成", "制作", "做过", "执行",
)
_SIGNIFICANT_OUTCOMES = {"success", "completed", "complete", "first", "breakthrough",
                         "成功", "完成", "首次", "突破", "作品"}

DEFAULT_MAX_ACTIVE_GOALS = 8
SNAPSHOT_LIMIT = 12
CHANGE_LIMIT = 20


class LifeStoreError(RuntimeError):
    """持久状态不可读、版本未知或写盘失败。宁可明确报错，不悄悄重置成长。"""


class LifeDataError(ValueError):
    """调用参数不合法（空内容、未知经历、无效类型、版本冲突等）。"""


# --------------------------------------------------------------------------- 工具
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _shared_lock(path: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _now(value=None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        return float(timestamp())
    raise LifeDataError(f"now 必须是时间戳：{value!r}")


def _optional_ts(value) -> float | None:
    if value is None or value == "":
        return None
    return _now(value)


def _cfg(config, key: str, default):
    full = f"life.{key}"
    try:
        value = config.get(full, None)
    except AttributeError:
        value = None
    if value is None:
        try:
            value = config.get("life", {}).get(key, default)
        except (AttributeError, TypeError):
            value = default
    return default if value is None else value


def _cfg_float(config, key: str, default: float) -> float:
    try:
        return float(_cfg(config, key, default))
    except (TypeError, ValueError):
        return float(default)


def _text(value) -> str:
    return str(value if value is not None else "").strip()


def _one_line(value) -> str:
    return " ".join(str(value if value is not None else "").split())


def _norm_text(value) -> str:
    """用于去重/检索的规范形式：全半角统一、大小写、空白和标点都去掉。"""
    raw = unicodedata.normalize("NFKC", str(value or "")).casefold()
    raw = (raw.replace("喜爱", "喜欢").replace("钟意", "喜欢")
              .replace("中意", "喜欢").replace("讨厌", "不喜欢")
              .replace("反感", "不喜欢"))
    return re.sub(r"[\s\W_]+", "", raw, flags=re.UNICODE)


def _tokens(value) -> set[str]:
    raw = unicodedata.normalize("NFKC", str(value or "")).casefold()
    out: set[str] = set(re.findall(r"[a-z0-9_]{2,}", raw))
    for run in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(run) <= 2:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


# 否定判断只认“落在谓词上的否定”，不能看到句子里任意一个“不/没/别”就
# 当成否定：例如“不错/非常/特别/别人”是正向表达。跨分句的多个否定也不做
# 奇偶抵消；只有同一谓词内紧邻的固定双重否定（不可能不/不是不/不得不等）
# 才视为肯定。先把这些固定搭配遮住/抵消，再收集剩余的真正否定标记。
# “是不是不喜欢”是疑问而不是双重否定，所以“不是不”前不能再是“是”。
_NEGATION_CHARS = frozenset("不没未无别若非莫勿甭")
_NEGATION_PLACEHOLDER = "〇"
_DOUBLE_NEGATION = re.compile(
    r"(?<!是)不是不|不可能不|不能不|不会不|未必不|未尝不|并非不|没有不|不得不|不无")
_NEGATION_EXCEPTIONS = tuple(sorted((
    "不由得", "不外乎", "不失为", "差不多", "差一点", "忍不住",
    "对不起", "没关系", "没错", "不但", "不仅", "不光", "不单",
    "不只", "不断", "不停", "不住", "不同", "不少", "不久", "不免",
    "不禁", "不管", "不论", "不错", "无论", "非但", "非常", "无比",
    "无疑", "无数", "无穷", "无限", "无妨", "无常", "未来", "非洲",
    "非议", "是非", "除非", "特别", "别人", "别的", "别处", "告别",
    "区别", "个别", "性别", "类别", "分别", "识别", "级别", "别墅",
    "别扭", "派别", "鉴别", "辨别", "离别", "别致", "别样", "别具",
), key=len, reverse=True))
_NEGATION_ALIASES = (
    ("喜爱", "喜欢"), ("钟意", "喜欢"), ("中意", "喜欢"),
    ("讨厌", "不喜欢"), ("反感", "不喜欢"),
)
_EN_NEGATION = re.compile(
    r"\b(?:not|no|never|none|nobody|nothing|neither|nor|without|hardly|barely)\b"
    r"|n't\b", re.IGNORECASE)


def _negation_view(value) -> tuple[str, bool]:
    """返回（去掉否定词后的核心，是否被否定）。

    否定按分句/谓词各自成立：两个独立分句各有一个“不”，整体仍是肯定句
    被否定，不能因为全句出现偶数个否定字就相互抵消。只有紧邻的固定双重
    否定（“不可能不/不是不/不得不”等）在同一谓词内才抵消回肯定；这样
    “喜欢猫”和“不喜欢猫”核心相同但极性相反，“这个方案不错”也不会被
    当成否定。
    """
    raw = unicodedata.normalize("NFKC", str(value or "")).casefold()
    for old, new in _NEGATION_ALIASES:
        raw = raw.replace(old, new)
    # 先在各自谓词内抵消固定双重否定；跨分句的否定字不会被拿来配对。
    raw = _DOUBLE_NEGATION.sub("", raw)
    for word in _NEGATION_EXCEPTIONS:
        if any(char in _NEGATION_CHARS for char in word):
            masked = "".join(
                _NEGATION_PLACEHOLDER if char in _NEGATION_CHARS else char
                for char in word)
            raw = raw.replace(word, masked)

    negated = _EN_NEGATION.search(raw) is not None
    raw = _EN_NEGATION.sub(" ", raw)
    core_chars: list[str] = []
    for char in raw:
        if char in _NEGATION_CHARS:
            negated = True
            continue
        core_chars.append(char)
    return _norm_text("".join(core_chars)), negated


def _has_negation(value) -> bool:
    return _negation_view(value)[1]


def _negation_conflict(first, second) -> bool:
    """两条文本是否在“同一核心命题”上极性相反。"""
    left, left_negated = _negation_view(first)
    right, right_negated = _negation_view(second)
    if left_negated == right_negated or not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= 4 and shorter in longer:
        return True
    return _jaccard(_tokens(left), _tokens(right)) >= 0.8


# 时间、程度等状语不改变命题真假。判断新说法是否属于已有冲突时先忽略它们，
# 避免“主人喜欢猫”和“主人现在喜欢猫”因为多一个状语就绕开冲突图。
_CONFLICT_NEUTRAL_MARKERS = (
    "现在", "目前", "如今", "最近", "刚才", "刚刚", "已经", "曾经", "一直",
    "仍然", "依然", "还是", "其实", "真的", "确实", "后来", "以前", "之前",
    "本来", "原来",
)


def _proposition_core(value) -> str:
    raw = _norm_text(value)
    for marker in _CONFLICT_NEUTRAL_MARKERS:
        raw = raw.replace(marker, "")
    return raw or _norm_text(value)


def _conflict_related(first, second) -> bool:
    """新说法是否与一条已有冲突落在同一命题附近（含近义改写）。"""
    if _negation_conflict(first, second):
        return True
    left = _negation_view(_proposition_core(first))[0]
    right = _negation_view(_proposition_core(second))[0]
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= 4 and shorter in longer:
        return True
    return _jaccard(_tokens(left), _tokens(right)) >= 0.8


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _similarity(first, second) -> float:
    a, b = _norm_text(first), _norm_text(second)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) < 4 or len(b) < 4:
        return 0.0
    score = _jaccard(_tokens(a), _tokens(b))
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer:
        score = max(score, 0.82 * len(shorter) / len(longer))
    return score


def _string_list(values, *, limit: int = 64) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    elif isinstance(values, dict):
        values = list(values.keys())
    try:
        items = list(values)
    except TypeError:
        items = [values]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = _one_line(item)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _clamp_confidence(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.6
    return round(max(0.0, min(1.0, number)), 4)


def _clamp_salience(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 3
    return max(1, min(5, number))


def _canonical_kind(value) -> str:
    key = _one_line(value).casefold()
    kind = _KIND_ALIASES.get(key)
    if kind is None:
        raise LifeDataError(
            f"未知记忆类型：{value!r}；可用：{'、'.join(MEMORY_KINDS)}")
    return kind


def _canonical_kinds(values) -> set[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    result: set[str] = set()
    for value in values:
        result.add(_canonical_kind(value))
    return result or None


def _canonical_goal_type(value) -> str:
    key = _one_line(value).casefold()
    goal_type = _GOAL_TYPES.get(key)
    if goal_type is None:
        raise LifeDataError(
            f"未知目标类型：{value!r}；可用：wish / obsession / task")
    return goal_type


def _canonical_goal_status(value) -> str:
    key = _one_line(value).casefold()
    if key not in _GOAL_STATUS:
        raise LifeDataError(f"未知目标状态：{value!r}")
    return key


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _experience_id(source_id: str) -> str:
    # 稳定来源对应稳定经历 ID；同一 source_id 重复写入永远落到同一条。
    return "exp_" + uuid.uuid5(uuid.NAMESPACE_URL, f"pawbot-life:{source_id}").hex[:16]


def _field_text(fields: dict) -> str:
    for key in ("text", "content", "title", "summary", "name"):
        value = fields.get(key)
        if value is not None and _text(value):
            return _text(value)
    return ""


def _level(value) -> tuple[int, str]:
    """把 observed_level 归一成 0=未知 / 1=了解 / 3=实践。

    未识别的字符串一律按未知处理，绝不默认成“可以实践”。
    """
    raw = _one_line(value).casefold()
    if not raw:
        return 0, ""
    if any(marker in raw for marker in _LOW_LEVEL_MARKERS):
        return 1, raw
    if any(marker in raw for marker in _HIGH_LEVEL_MARKERS):
        return 3, raw
    return 0, raw


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "revision": 0,
        "updated_at": 0.0,
        "experiences": [],
        "memories": [],
        "goals": [],
        "self": [],
        "skills": [],
        "mementos": [],
        "artifacts": [],
        "changes": [],
    }


def _atomic_write(path: str, payload: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".life-state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- Store
class LifeStore:
    """生活状态仓库。构造只读；没有经历就没有任何默认剧情。"""

    def __init__(self, base_dir: str, config, *, paths=None):
        self.base_dir = os.path.abspath(str(base_dir or "."))
        self.config = config
        self.paths = paths if paths is not None else layout.resolve(base_dir, config)
        self.path = self.paths.content(STATE_NAME)
        self._lock = _shared_lock(self.path)
        self._state: dict = _empty_state()
        self._stamp: tuple[int, int] | None = None
        self._had_file = False
        with self._lock:
            self._load_locked(missing_ok=True)

    # ------------------------------------------------------------ 持久化
    def _stat_locked(self) -> tuple[int, int] | None:
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LifeStoreError(f"读不了生活状态文件：{exc}") from exc
        return int(info.st_mtime_ns), int(info.st_size)

    def _load_locked(self, *, missing_ok: bool) -> None:
        stamp = self._stat_locked()
        if stamp is None:
            if not missing_ok:
                raise LifeStoreError(f"生活状态文件不见了：{self.path}")
            self._state = _empty_state()
            self._stamp = None
            self._had_file = False
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LifeStoreError(f"生活状态文件损坏，拒绝继续：{exc}") from exc

        if not isinstance(raw, dict):
            raise LifeStoreError("生活状态文件不是对象")
        if raw.get("version") != STATE_VERSION:
            raise LifeStoreError(
                f"生活状态版本未知：{raw.get('version')!r}（期望 {STATE_VERSION}）")
        for key in ("experiences", "memories", "goals", "self", "skills",
                    "mementos", "artifacts", "changes"):
            if not isinstance(raw.get(key, []), list):
                raise LifeStoreError(f"生活状态字段 {key} 不是列表")
            if any(not isinstance(item, dict) for item in raw.get(key, [])):
                raise LifeStoreError(f"生活状态字段 {key} 里有非对象条目")
        raw.setdefault("revision", 0)
        try:
            raw["revision"] = int(raw["revision"])
        except (TypeError, ValueError) as exc:
            raise LifeStoreError("生活状态 revision 不合法") from exc
        raw.setdefault("updated_at", 0.0)
        self._state = raw
        self._had_file = True
        self._stamp = self._stat_locked()

    def _refresh_locked(self) -> None:
        stamp = self._stat_locked()
        if stamp == self._stamp:
            return
        if stamp is None:
            if self._had_file:
                raise LifeStoreError(f"生活状态文件在运行中消失：{self.path}")
            self._state = _empty_state()
            self._stamp = None
            return
        self._load_locked(missing_ok=False)

    def reload(self) -> dict:
        """从磁盘重新读取（HA/外部同步后调用）。"""
        with self._lock:
            self._load_locked(missing_ok=not self._had_file)
            return copy.deepcopy({
                "ok": True,
                "version": self._state["version"],
                "revision": int(self._state.get("revision") or 0),
                "path": self.path,
            })

    def _commit_locked(self, state: dict) -> None:
        state["version"] = STATE_VERSION
        state["revision"] = int(self._state.get("revision") or 0) + 1
        state["updated_at"] = time.time()
        payload = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            _atomic_write(self.path, payload)
        except OSError as exc:
            raise LifeStoreError(f"生活状态写盘失败，本次改动未生效：{exc}") from exc
        self._state = state
        self._stamp = self._stat_locked()
        self._had_file = True

    def _write_op(self, fn):
        """复制 → 改副本 → 成功才落盘/替换内存。异常时原状态不变。"""
        with self._lock:
            self._refresh_locked()
            state = copy.deepcopy(self._state)
            result, changed = fn(state)
            if changed:
                self._commit_locked(state)
            return copy.deepcopy(result)

    def _read_op(self, fn):
        with self._lock:
            self._refresh_locked()
            return copy.deepcopy(fn(self._state))

    # ------------------------------------------------------------ 内部查找
    @staticmethod
    def _find_experience(state: dict, reference) -> dict | None:
        ref = _text(reference)
        if not ref:
            return None
        for row in state.get("experiences", []):
            if ref in (row.get("id"), row.get("source_id"), row.get("root_source_id")):
                return row
        return None

    def _validate_source_ids(self, state: dict, source_ids) -> list[str]:
        result: list[str] = []
        for ref in _string_list(source_ids):
            row = self._find_experience(state, ref)
            if row is None:
                raise LifeDataError(f"source_ids 引用了不存在的经历：{ref}")
            if row["id"] not in result:
                result.append(row["id"])
        return result

    @staticmethod
    def _find_goal(state: dict, target_id) -> dict | None:
        target = _text(target_id)
        if not target:
            return None
        return next((row for row in state.get("goals", []) if row.get("id") == target), None)

    @staticmethod
    def _find_self(state: dict, target_id) -> dict | None:
        target = _text(target_id)
        if not target:
            return None
        return next((row for row in state.get("self", []) if row.get("id") == target), None)

    @staticmethod
    def _append_change(state: dict, *, target_kind: str, target_id: str, op: str,
                       before_version: int, after_version: int, reason: str,
                       source_ids: list[str], now: float, fields: list[str]) -> None:
        state.setdefault("changes", []).append({
            "id": _new_id("chg"),
            "target_kind": target_kind,
            "target_id": target_id,
            "op": op,
            "before_version": int(before_version),
            "after_version": int(after_version),
            "reason": _one_line(reason)[:500],
            # 只记字段名，不把正文再抄一份，删除时不会从变更日志泄漏。
            "fields": sorted(set(fields)),
            "source_ids": list(dict.fromkeys(source_ids)),
            "at": float(now),
        })
        if len(state["changes"]) > 500:
            state["changes"] = state["changes"][-500:]

    # ------------------------------------------------------------ 经历
    def record_experience(self, source_id, summary, *, platform="local",
                          topics=None, people=None, reaction="",
                          observed_level="", goal_id="", kind="episode",
                          confidence=0.6, salience=3, now=None) -> dict:
        """记录一条实际经历；同一稳定来源只落一条，重复写入不提升证据。"""
        source = _text(source_id)
        if not source:
            raise LifeDataError("source_id 不能为空")
        body = _text(summary)
        if not body:
            raise LifeDataError("summary 不能为空")
        ts = _now(now)

        def apply(state):
            existing = next(
                (row for row in state.get("experiences", [])
                 if source in (row.get("source_id"), row.get("root_source_id"))),
                None,
            )
            if existing is not None:
                return existing, False
            row = {
                "id": _experience_id(source),
                "root_source_id": source,
                "source_id": source,
                "ts": ts,
                "summary": body,
                "reaction": _text(reaction),
                "topics": _string_list(topics),
                "people": _string_list(people),
                "platform": _text(platform) or "local",
                "goal_id": _text(goal_id),
                "observed_level": _text(observed_level),
                "kind": _text(kind) or "episode",
                "confidence": _clamp_confidence(confidence),
                "salience": _clamp_salience(salience),
                "created_at": ts,
            }
            state["experiences"].append(row)
            return row, True

        return self._write_op(apply)

    def recent_experiences(self, limit=10, *, start=None, end=None) -> list[dict]:
        begin, finish = _optional_ts(start), _optional_ts(end)

        def read(state):
            rows = list(state.get("experiences", []))
            if begin is not None:
                rows = [row for row in rows if float(row.get("ts") or 0) >= begin]
            if finish is not None:
                rows = [row for row in rows if float(row.get("ts") or 0) <= finish]
            rows.sort(key=lambda row: (float(row.get("ts") or 0), str(row.get("id"))),
                      reverse=True)
            try:
                count = max(0, int(limit))
            except (TypeError, ValueError):
                count = 10
            return rows[:count] if count else []

        return self._read_op(read)

    # ------------------------------------------------------------ 记忆
    def _memory_duplicate(self, state: dict, kind: str, subject_key: str,
                          text: str) -> tuple[list[dict], list[dict]]:
        """分出重复条目与冲突条目。

        语义否定优先于相似度：长句只多一个“不”时相似度也会超过 0.92，
        但极性相反的两句必须进冲突，不能当成重复而吞掉否定、提升旧置信度。
        """
        target_norm = _norm_text(text)
        duplicates: list[dict] = []
        conflicts: list[dict] = []
        unresolved: list[dict] = []
        for row in state.get("memories", []):
            if row.get("kind") != kind:
                continue
            if _one_line(row.get("subject")).casefold() != subject_key:
                continue
            status = row.get("status")
            if status == "conflicted":
                unresolved.append(row)
            score = _similarity(row.get("text"), text)
            negated = _negation_conflict(row.get("text"), text)
            same = _norm_text(row.get("text")) == target_norm or score >= 0.92
            if same:
                if negated:
                    # 正负命题不能合并；隐藏条目也不再参与新的有效证据。
                    if status not in _MEMORY_HIDDEN:
                        conflicts.append(row)
                    continue
                duplicates.append(row)
                continue
            if status in _MEMORY_HIDDEN:
                continue
            if status == "conflicted":
                # 未解决冲突不能因为新说法换了措辞就被绕过：只要它仍落在
                # 同一命题附近，就先进入冲突图，等明确确认再解决。
                if _conflict_related(row.get("text"), text):
                    conflicts.append(row)
                continue
            if negated or score >= 0.55:
                conflicts.append(row)

        # 未解决冲突只沿 conflict_with 显式边传播，并限定在同一 subject/kind
        # 内遍历：命中咖啡冲突不能把同 subject/kind 但互不相连的猫冲突也拖进来。
        if unresolved:
            related = {
                row.get("id"): row for row in state.get("memories", [])
                if row.get("kind") == kind
                and _one_line(row.get("subject")).casefold() == subject_key
            }
            reached: set[str] = set()
            stack = [row.get("id") for row in conflicts if row.get("id") in related]
            while stack:
                current = stack.pop()
                if current in reached:
                    continue
                reached.add(current)
                for neighbor in related[current].get("conflict_with") or []:
                    if neighbor in related and neighbor not in reached:
                        stack.append(neighbor)
            if reached:
                duplicate_ids = {row.get("id") for row in duplicates}
                conflict_ids = {row.get("id") for row in conflicts}
                for row in unresolved:
                    row_id = row.get("id")
                    if row_id in reached and row_id not in duplicate_ids \
                            and row_id not in conflict_ids:
                        conflicts.append(row)
        return duplicates, conflicts

    @staticmethod
    def _supersede_memory(row: dict, by_id: str, now: float) -> None:
        row["status"] = "superseded"
        row["valid_until"] = now
        row["superseded_by"] = by_id
        row["conflict_with"] = list(dict.fromkeys(
            list(row.get("conflict_with") or []) + [by_id]))
        row["version"] = int(row.get("version") or 1) + 1
        row["updated_at"] = now

    def _merge_memory_evidence(self, row: dict, source_ids: list[str],
                               state: dict, confidence: float, now: float) -> bool:
        old_ids = list(row.get("source_ids") or [])
        old_roots = set(row.get("evidence_roots") or [])
        new_roots: list[str] = []
        for exp_id in source_ids:
            exp = self._find_experience(state, exp_id)
            root = str((exp or {}).get("root_source_id") or exp_id)
            if root not in old_roots and root not in new_roots:
                new_roots.append(root)
        changed = False
        for exp_id in source_ids:
            if exp_id not in old_ids:
                old_ids.append(exp_id)
                changed = True
        if new_roots:
            row["source_ids"] = old_ids
            row["evidence_roots"] = list(old_roots) + new_roots
            # 独立来源只加很小的一点，重复同一来源完全不提升。
            row["confidence"] = round(min(0.98, max(
                float(row.get("confidence") or 0), confidence
            ) + 0.04 * len(new_roots)), 4)
            row["last_evidence_at"] = now
            changed = True
        return changed

    def remember(self, text, *, kind="semantic", subject="self", source_ids=None,
                 confidence=0.6, salience=3, confirmed=False, now=None) -> dict:
        """写入六类逻辑记忆；同义去重、候选/确认、冲突不静默覆盖。"""
        body = _text(text)
        if not body:
            raise LifeDataError("记忆内容不能为空")
        memory_kind = _canonical_kind(kind)
        subject_text = _text(subject) or "self"
        subject_key = subject_text.casefold()
        ts = _now(now)
        conf = _clamp_confidence(confidence)
        sal = _clamp_salience(salience)

        def apply(state):
            source_ids_ok = self._validate_source_ids(state, source_ids)
            roots: list[str] = []
            for exp_id in source_ids_ok:
                exp = self._find_experience(state, exp_id)
                root = str((exp or {}).get("root_source_id") or exp_id)
                if root not in roots:
                    roots.append(root)

            duplicates, conflicts = self._memory_duplicate(
                state, memory_kind, subject_key, body)
            active = [row for row in duplicates
                      if row.get("status") not in _MEMORY_HIDDEN]
            hidden = [row for row in duplicates
                      if row.get("status") in {"retired", "deleted", "merged"}]
            superseded = [row for row in duplicates
                          if row.get("status") == "superseded"]
            extra_conflict_ids: set[str] = set()

            if active:
                duplicate = max(active, key=lambda row: (
                    row.get("status") == "confirmed",
                    float(row.get("updated_at") or 0)))
                changed = self._merge_memory_evidence(
                    duplicate, source_ids_ok, state, conf, ts)
                if confirmed and duplicate.get("status") != "confirmed":
                    duplicate["status"] = "confirmed"
                    duplicate["confirmed_at"] = ts
                    duplicate["valid_from"] = float(
                        duplicate.get("valid_from")
                        or duplicate.get("created_at") or ts)
                    duplicate["valid_until"] = None
                    other_ids = list(duplicate.get("conflict_with") or [])
                    other_ids += [other["id"] for other in conflicts]
                    for other_id in dict.fromkeys(other_ids):
                        other = next((m for m in state.get("memories", [])
                                      if m.get("id") == other_id), None)
                        if other and other.get("status") not in _MEMORY_HIDDEN:
                            self._supersede_memory(other, duplicate["id"], ts)
                    duplicate["conflict_with"] = []
                    changed = True
                elif duplicate.get("status") == "confirmed" and \
                        duplicate.get("valid_until") is not None:
                    # 有效版本不能带着旧的有效期上限；重复确认也不延长证据。
                    duplicate["valid_until"] = None
                    changed = True
                if duplicate.get("valid_from") is None:
                    duplicate["valid_from"] = float(
                        duplicate.get("created_at") or ts)
                    changed = True
                if changed:
                    duplicate["version"] = int(duplicate.get("version") or 1) + 1
                    duplicate["updated_at"] = ts
                return duplicate, changed

            if hidden:
                # 已删除/退役的条目不因为再次写入而复活。
                return max(hidden, key=lambda row: float(
                    row.get("updated_at") or 0)), False

            if superseded:
                old_roots: set[str] = set()
                for row in superseded:
                    old_roots.update(row.get("evidence_roots") or [])
                new_roots = [root for root in roots if root not in old_roots]
                if not new_roots:
                    # 同一来源重放旧版本不是新证据：不复活旧版本，也不动
                    # 当前有效版本，避免“过期版本把新版本反超”的循环。
                    return max(superseded, key=lambda row: float(
                        row.get("updated_at") or 0)), False
                # 有独立新来源，说明是新一轮真实纠正：建新有效版本，而不是
                # 把旧版本原地复活（旧 valid_until 仍保留在旧条目上）。
                for row in superseded:
                    extra_conflict_ids.update(row.get("conflict_with") or [])

            new_id = _new_id("mem")
            status = "confirmed" if confirmed else "candidate"
            conflict_with: list[str] = []
            if confirmed:
                other_rows = list(conflicts)
                seen_ids = {row["id"] for row in other_rows}
                for other_id in extra_conflict_ids:
                    if other_id in seen_ids:
                        continue
                    other = next((m for m in state.get("memories", [])
                                  if m.get("id") == other_id), None)
                    if other is not None:
                        other_rows.append(other)
                        seen_ids.add(other_id)
                for other in other_rows:
                    if other.get("status") in _MEMORY_HIDDEN:
                        continue
                    conflict_with.append(other["id"])
                    self._supersede_memory(other, new_id, ts)
                conflict_with = list(dict.fromkeys(conflict_with))
            elif conflicts:
                confirmed_exists = any(
                    other.get("status") == "confirmed" for other in conflicts)
                status = "candidate" if confirmed_exists else "conflicted"
                for other in conflicts:
                    conflict_with.append(other["id"])
                    other["conflict_with"] = list(dict.fromkeys(
                        list(other.get("conflict_with") or []) + [new_id]))
                    if other.get("status") != "confirmed":
                        other["status"] = "conflicted"
                    other["version"] = int(other.get("version") or 1) + 1
                    other["updated_at"] = ts

            row = {
                "id": new_id,
                "kind": memory_kind,
                "subject": subject_text,
                "text": body,
                "confidence": conf,
                "salience": sal,
                "source_ids": source_ids_ok,
                "evidence_roots": roots,
                "status": status,
                "conflict_with": conflict_with,
                "valid_from": ts,
                "valid_until": None,
                "created_at": ts,
                "updated_at": ts,
                "last_evidence_at": ts,
                "last_retrieved_at": 0.0,
                "last_used_at": 0.0,
                "last_mentioned_at": 0.0,
                "last_mentioned_surface": "",
                "confirmed_at": ts if confirmed else 0.0,
                "version": 1,
            }
            state["memories"].append(row)
            return row, True

        return self._write_op(apply)

    @staticmethod
    def _query_hits(query, text) -> tuple[int, float]:
        query_norm = _norm_text(query)
        text_norm = _norm_text(text)
        if not query_norm or not text_norm:
            return 0, 0.0
        query_tokens = _tokens(query) - _WEAK_TOKENS
        if not query_tokens:
            return 0, 0.0
        hits = len(query_tokens & (_tokens(text) - _WEAK_TOKENS))
        bonus = 0.0
        if query_norm in text_norm:
            bonus = 1.5 if len(query_norm) <= 4 else 1.0
        return hits, bonus

    def recall(self, query, *, kinds=None, limit=6, budget=1400, now=None) -> list[dict]:
        """按相关度召回；先过门槛，再受预算约束，冲突/删退默认跳过。"""
        query_text = _text(query)
        if not query_text:
            return []
        wanted = _canonical_kinds(kinds)
        now_ts = _now(now)
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 6
        try:
            char_budget = max(0, int(budget))
        except (TypeError, ValueError):
            char_budget = 1400
        if count == 0 or char_budget == 0:
            return []

        with self._lock:
            self._refresh_locked()
            state = self._state
            scored = []
            for row in state.get("memories", []):
                if row.get("status") in _MEMORY_HIDDEN:
                    continue
                if row.get("status") == "conflicted":
                    continue
                if row.get("status") == "candidate" and row.get("conflict_with"):
                    continue
                if wanted is not None and row.get("kind") not in wanted:
                    continue
                valid_from = float(row.get("valid_from") or 0)
                valid_until = row.get("valid_until")
                if valid_from and valid_from > now_ts:
                    continue
                if valid_until not in (None, 0, 0.0) and now_ts >= float(valid_until):
                    continue
                hits, bonus = self._query_hits(query_text, row.get("text"))
                if not hits and not bonus:
                    continue
                if hits < 1 and bonus <= 0:
                    continue
                age_days = max(0.0, now_ts - float(row.get("ts") or row.get("created_at")
                                                   or now_ts)) / 86400.0
                recency = math.exp(-age_days / 45.0)
                prior = {
                    "common": 0.3, "procedural": 0.25, "semantic": 0.2,
                    "episodic": 0.1, "working": 0.0, "candidate": -0.25,
                }.get(row.get("kind"), 0.0)
                evidence = len(set(row.get("evidence_roots") or []))
                frequency = math.log1p(evidence) * 0.12
                score = (
                    hits * 1.0
                    + bonus * 1.2
                    + int(row.get("salience") or 1) * 0.12
                    + float(row.get("confidence") or 0) * 0.35
                    + recency * 0.25
                    + prior
                    + min(0.6, frequency)
                )
                threshold = _cfg_float(self.config, "recall_min_score", 1.0)
                if score < threshold:
                    continue
                scored.append((score, float(row.get("updated_at") or 0),
                               str(row.get("id")), row))
            scored.sort(key=lambda item: (-item[0], -item[1], item[2]))

            selected: list[dict] = []
            remaining = char_budget
            for score, _updated, _id, row in scored:
                if len(selected) >= count:
                    break
                body = str(row.get("text") or "")
                if remaining <= 0:
                    break
                clipped = body
                truncated = False
                if len(clipped) > remaining:
                    clipped = clipped[:max(0, remaining)]
                    truncated = True
                if not clipped:
                    continue
                item = copy.deepcopy(row)
                item["text"] = clipped
                item["recall_score"] = round(score, 4)
                if truncated:
                    item["truncated"] = True
                if item.get("status") == "candidate":
                    item["uncertain"] = True
                selected.append(item)
                remaining -= len(clipped)
                if remaining <= 0:
                    break

            if selected:
                ids = [item["id"] for item in selected]
                new_state = copy.deepcopy(state)
                for row in new_state.get("memories", []):
                    if row.get("id") in ids:
                        row["last_retrieved_at"] = now_ts
                try:
                    self._commit_locked(new_state)
                except (LifeStoreError, OSError):
                    # 检索本身不该因为一次元数据写盘失败而不可用。
                    pass
            return copy.deepcopy(selected)

    def mark_mentioned(self, ids, *, surface="", now=None) -> dict:
        """只记录“对外提过”，不改置信度、不改有效时间。"""
        targets = set(_string_list(ids))
        if not targets:
            return {"ok": True, "count": 0, "ids": []}
        ts = _now(now)
        label = _one_line(surface)[:120]

        def apply(state):
            found: list[str] = []
            for collection in ("memories", "goals", "self"):
                for row in state.get(collection, []):
                    if row.get("id") not in targets:
                        continue
                    if row.get("status") in {"deleted"}:
                        continue
                    row["last_mentioned_at"] = ts
                    row["last_mentioned_surface"] = label
                    if collection == "memories":
                        row["mention_count"] = int(row.get("mention_count") or 0) + 1
                    found.append(row["id"])
            found = list(dict.fromkeys(found))
            if not found:
                return {"ok": True, "count": 0, "ids": []}, False
            return {"ok": True, "count": len(found), "ids": found}, True

        return self._write_op(apply)

    # ------------------------------------------------------------ 目标
    def goals(self, *, active_only=True) -> list[dict]:
        def read(state):
            rows = [row for row in state.get("goals", [])
                    if row.get("status") != "deleted"]
            if active_only:
                rows = [row for row in rows if row.get("status") == "active"]
            else:
                rows = [row for row in rows if row.get("status") in _GOAL_STATUS]
            rows.sort(key=lambda row: (
                0 if row.get("status") == "active" else 1,
                -float(row.get("updated_at") or 0),
                str(row.get("id")),
            ))
            return rows
        return self._read_op(read)

    def _active_goal_limit(self) -> int:
        for key in ("max_active_goals", "active_goal_limit", "goals_active_limit"):
            value = _cfg(self.config, key, None)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return DEFAULT_MAX_ACTIVE_GOALS
        return DEFAULT_MAX_ACTIVE_GOALS

    def _active_goal_count(self, state: dict, *, exclude: str = "") -> int:
        return sum(1 for row in state.get("goals", [])
                   if row.get("id") != exclude and row.get("status") == "active")

    def patch_goal(self, operation, *, target_id="", expected_version=None,
                   fields=None, reason="", source_ids=None, now=None) -> dict:
        """目标 add/revise/complete/pause/abandon/delete；局部改 + CAS。"""
        op = _one_line(operation).casefold()
        if op not in {"add", "revise", "complete", "pause", "abandon", "delete"}:
            return {"ok": False, "error": "invalid-operation", "id": _text(target_id),
                    "version": 0, "message": f"未知目标操作：{operation!r}"}
        if fields is not None and not isinstance(fields, dict):
            return {"ok": False, "error": "invalid-fields", "id": _text(target_id),
                    "version": 0, "message": "fields 必须是对象"}
        patch = dict(fields or {})
        ts = _now(now)
        base_reason = _one_line(reason)
        expected = None
        if expected_version is not None:
            try:
                expected = int(expected_version)
            except (TypeError, ValueError):
                return {"ok": False, "error": "invalid-version",
                        "id": _text(target_id), "version": 0,
                        "message": "expected_version 必须是整数"}

        def apply(state):
            source_ok: list[str] = []
            if source_ids is not None:
                try:
                    source_ok = self._validate_source_ids(state, source_ids)
                except LifeDataError as exc:
                    return {"ok": False, "error": "invalid-source",
                            "id": _text(target_id), "version": 0,
                            "message": str(exc)}, False
            field_sources = patch.get("source_ids")
            if field_sources is not None:
                try:
                    source_ok = list(dict.fromkeys(
                        source_ok + self._validate_source_ids(state, field_sources)))
                except LifeDataError as exc:
                    return {"ok": False, "error": "invalid-source",
                            "id": _text(target_id), "version": 0,
                            "message": str(exc)}, False

            if op == "add":
                text = _field_text(patch)
                if not text:
                    return {"ok": False, "error": "invalid-fields", "id": "",
                            "version": 0, "message": "目标正文不能为空"}, False
                try:
                    goal_type = _canonical_goal_type(
                        patch.get("type") or patch.get("kind") or "task")
                except LifeDataError as exc:
                    return {"ok": False, "error": "invalid-type", "id": "",
                            "version": 0, "message": str(exc)}, False
                limit = self._active_goal_limit()
                if limit and self._active_goal_count(state) >= limit:
                    return {"ok": False, "error": "active-limit", "id": "",
                            "version": 0,
                            "message": f"活跃目标已达上限 {limit}"}, False
                new_id = _text(target_id) or _new_id("goal")
                if self._find_goal(state, new_id) is not None:
                    return {"ok": False, "error": "duplicate-id", "id": new_id,
                            "version": 0, "message": "目标 ID 已存在"}, False
                row = {
                    "id": new_id,
                    "type": goal_type,
                    "text": text,
                    "status": "active",
                    "version": 1,
                    "source_ids": source_ok,
                    "reason": base_reason,
                    "next_step": _text(patch.get("next_step")),
                    "progress": _text(patch.get("progress")),
                    "blockers": _string_list(patch.get("blockers")),
                    "people": _string_list(patch.get("people")),
                    "created_at": ts,
                    "updated_at": ts,
                    "last_progress_at": 0.0,
                    "completed_at": 0.0,
                    "abandoned_at": 0.0,
                    "deleted_at": 0.0,
                    "last_mentioned_at": 0.0,
                    "last_mentioned_surface": "",
                    "history": [{
                        "version": 1, "op": "add", "at": ts,
                        "reason": base_reason, "fields": sorted(patch),
                    }],
                }
                state["goals"].append(row)
                self._append_change(
                    state, target_kind="goal", target_id=new_id, op="add",
                    before_version=0, after_version=1, reason=base_reason,
                    source_ids=source_ok, now=ts, fields=list(patch))
                return {"ok": True, "id": new_id, "version": 1,
                        "goal": row}, True

            target = self._find_goal(state, target_id)
            if target is None:
                return {"ok": False, "error": "not-found", "id": _text(target_id),
                        "version": 0, "message": "没有这个目标"}, False
            current_version = int(target.get("version") or 1)
            if target.get("status") == "deleted":
                return {"ok": False, "error": "deleted", "id": target["id"],
                        "version": current_version,
                        "message": "目标已删除，不能复活"}, False
            if expected is None:
                # 已有对象的所有变更都必须带 CAS 版本；缺少版本时宁可拒绝，
                # 也不能拿旧快照覆盖别人的修改。
                return {"ok": False, "error": "version-required",
                        "id": target["id"], "version": current_version,
                        "message": "修改已有目标必须提供 expected_version"}, False
            if expected != current_version:
                return {"ok": False, "error": "version-conflict", "id": target["id"],
                        "version": current_version,
                        "message": "目标已被其他过程改过，请读新版本再合并"}, False

            before = current_version
            if op == "delete":
                target.update({
                    "status": "deleted",
                    "text": "",
                    "reason": base_reason,
                    "next_step": "",
                    "progress": "",
                    "blockers": [],
                    "people": [],
                    "source_ids": [],
                    "deleted_at": ts,
                    "updated_at": ts,
                    "version": before + 1,
                })
                target["history"] = [{
                    "version": target["version"], "op": "delete", "at": ts,
                    "reason": base_reason, "fields": ["text"],
                }]
                self._append_change(
                    state, target_kind="goal", target_id=target["id"], op="delete",
                    before_version=before, after_version=target["version"],
                    reason=base_reason, source_ids=[], now=ts, fields=["text"])
                return {"ok": True, "id": target["id"],
                        "version": target["version"],
                        "goal": target}, True

            if op in {"complete", "pause", "abandon"}:
                wanted = {"complete": "completed", "pause": "paused",
                          "abandon": "abandoned"}[op]
                if target.get("status") == wanted:
                    return {"ok": True, "id": target["id"],
                            "version": before, "goal": target}, False
                target["status"] = wanted
                if op == "complete":
                    target["completed_at"] = ts
                elif op == "abandon":
                    target["abandoned_at"] = ts
                target["updated_at"] = ts
                target["version"] = before + 1
                target.setdefault("history", []).append({
                    "version": target["version"], "op": op, "at": ts,
                    "reason": base_reason, "fields": ["status"],
                })
                self._append_change(
                    state, target_kind="goal", target_id=target["id"], op=op,
                    before_version=before, after_version=target["version"],
                    reason=base_reason, source_ids=source_ok, now=ts,
                    fields=["status"])
                return {"ok": True, "id": target["id"],
                        "version": target["version"],
                        "goal": target}, True

            # revise：逐个字段局部合并，未涉及字段原样保留。
            protected = {"id", "version", "created_at", "updated_at", "history",
                         "deleted_at", "status", "source_ids", "type", "text",
                         "content", "title", "summary"}
            merged_keys: list[str] = []
            new_text = _field_text(patch)
            if new_text:
                target["text"] = new_text
                merged_keys.append("text")
            if patch.get("type") is not None or patch.get("kind") is not None:
                try:
                    target["type"] = _canonical_goal_type(
                        patch.get("type") or patch.get("kind"))
                except LifeDataError as exc:
                    return {"ok": False, "error": "invalid-type", "id": target["id"],
                            "version": before, "message": str(exc)}, False
                merged_keys.append("type")
            if patch.get("status") is not None:
                try:
                    status = _canonical_goal_status(patch["status"])
                except LifeDataError as exc:
                    return {"ok": False, "error": "invalid-status", "id": target["id"],
                            "version": before, "message": str(exc)}, False
                if status == "deleted":
                    return {"ok": False, "error": "invalid-status", "id": target["id"],
                            "version": before,
                            "message": "删除请使用 delete 操作"}, False
                if status == "active" and target.get("status") != "active":
                    limit = self._active_goal_limit()
                    if limit and self._active_goal_count(
                            state, exclude=target["id"]) >= limit:
                        return {"ok": False, "error": "active-limit",
                                "id": target["id"], "version": before,
                                "message": f"活跃目标已达上限 {limit}"}, False
                target["status"] = status
                merged_keys.append("status")
            for key, value in patch.items():
                if key in protected or key in {"kind", "source_ids"}:
                    continue
                if key in {"next_step", "progress"}:
                    target[key] = _text(value)
                elif key in {"blockers", "people"}:
                    target[key] = _string_list(value)
                elif key in {"last_progress_at", "completed_at", "abandoned_at"}:
                    target[key] = _now(value)
                else:
                    target[key] = copy.deepcopy(value)
                merged_keys.append(key)
            if not merged_keys and not source_ok:
                return {"ok": False, "error": "invalid-fields", "id": target["id"],
                        "version": before, "message": "没有可改的字段"}, False
            if source_ok:
                target["source_ids"] = list(dict.fromkeys(
                    list(target.get("source_ids") or []) + source_ok))
                merged_keys.append("source_ids")
            if "progress" in merged_keys:
                target["last_progress_at"] = ts
            if base_reason:
                target["reason"] = base_reason
            target["updated_at"] = ts
            target["version"] = before + 1
            target.setdefault("history", []).append({
                "version": target["version"], "op": "revise", "at": ts,
                "reason": base_reason, "fields": sorted(set(merged_keys)),
            })
            self._append_change(
                state, target_kind="goal", target_id=target["id"], op="revise",
                before_version=before, after_version=target["version"],
                reason=base_reason, source_ids=source_ok, now=ts,
                fields=merged_keys)
            return {"ok": True, "id": target["id"],
                    "version": target["version"], "goal": target}, True

        return self._write_op(apply)

    # ------------------------------------------------------------ 自我条目
    def self_entries(self, *, active_only=True) -> list[dict]:
        def read(state):
            rows = [row for row in state.get("self", [])
                    if row.get("status") not in {"deleted"}]
            if active_only:
                rows = [row for row in rows if row.get("status") == "active"]
            rows.sort(key=lambda row: (
                0 if row.get("status") == "active" else 1,
                -float(row.get("updated_at") or 0),
                str(row.get("id")),
            ))
            return rows
        return self._read_op(read)

    def patch_self(self, operation, *, target_id="", expected_version=None,
                   expected_other_version=None, fields=None, reason="",
                   source_ids=None, now=None) -> dict:
        """自我条目 add/revise/retire/delete/merge；稳定 ID + 局部 CAS。

        merge 同时改两个条目，除 ``expected_version``（target_id 的版本）外
        还必须给 ``fields.expected_other_version``（或本参数）校验另一条目。
        也接受 ``fields.expected_destination_version`` /
        ``fields.expected_source_version`` 作为该字段的别名。

        只操作本 store 自己的条目，永远不写外部 ``persona.md``。
        """
        op = _one_line(operation).casefold()
        if op not in {"add", "revise", "retire", "delete", "merge"}:
            return {"ok": False, "error": "invalid-operation", "id": _text(target_id),
                    "version": 0, "message": f"未知自我操作：{operation!r}"}
        if fields is not None and not isinstance(fields, dict):
            return {"ok": False, "error": "invalid-fields", "id": _text(target_id),
                    "version": 0, "message": "fields 必须是对象"}
        patch = dict(fields or {})
        ts = _now(now)
        base_reason = _one_line(reason)
        expected = None
        if expected_version is not None:
            try:
                expected = int(expected_version)
            except (TypeError, ValueError):
                return {"ok": False, "error": "invalid-version",
                        "id": _text(target_id), "version": 0,
                        "message": "expected_version 必须是整数"}
        other_expected = expected_other_version
        for key in ("expected_other_version", "expected_destination_version",
                    "expected_source_version"):
            value = patch.pop(key, None)
            if other_expected is None and value is not None:
                other_expected = value
        if other_expected is not None:
            try:
                other_expected = int(other_expected)
            except (TypeError, ValueError):
                return {"ok": False, "error": "invalid-version",
                        "id": _text(target_id), "version": 0,
                        "message": "expected_other_version 必须是整数"}

        def apply(state):
            source_ok: list[str] = []
            if source_ids is not None:
                try:
                    source_ok = self._validate_source_ids(state, source_ids)
                except LifeDataError as exc:
                    return {"ok": False, "error": "invalid-source",
                            "id": _text(target_id), "version": 0,
                            "message": str(exc)}, False
            field_sources = patch.get("source_ids")
            if field_sources is not None:
                try:
                    source_ok = list(dict.fromkeys(
                        source_ok + self._validate_source_ids(state, field_sources)))
                except LifeDataError as exc:
                    return {"ok": False, "error": "invalid-source",
                            "id": _text(target_id), "version": 0,
                            "message": str(exc)}, False

            if op == "add":
                text = _field_text(patch)
                if not text:
                    return {"ok": False, "error": "invalid-fields", "id": "",
                            "version": 0, "message": "自我条目正文不能为空"}, False
                new_id = _text(target_id) or _new_id("self")
                if self._find_self(state, new_id) is not None:
                    return {"ok": False, "error": "duplicate-id", "id": new_id,
                            "version": 0, "message": "自我条目 ID 已存在"}, False
                row = {
                    "id": new_id,
                    "category": _one_line(
                        patch.get("category") or patch.get("kind") or "self") or "self",
                    "text": text,
                    "status": "active",
                    "version": 1,
                    "valid_scope": _text(patch.get("valid_scope")),
                    "source_ids": source_ok,
                    "reason": base_reason,
                    "created_at": ts,
                    "updated_at": ts,
                    "retired_at": 0.0,
                    "deleted_at": 0.0,
                    "merged_into": "",
                    "merged_from": [],
                    "last_mentioned_at": 0.0,
                    "last_mentioned_surface": "",
                    "history": [{
                        "version": 1, "op": "add", "at": ts,
                        "reason": base_reason, "fields": sorted(patch),
                    }],
                }
                state["self"].append(row)
                self._append_change(
                    state, target_kind="self", target_id=new_id, op="add",
                    before_version=0, after_version=1, reason=base_reason,
                    source_ids=source_ok, now=ts, fields=list(patch))
                return {"ok": True, "id": new_id, "version": 1,
                        "entry": row}, True

            target = self._find_self(state, target_id)
            if target is None:
                return {"ok": False, "error": "not-found", "id": _text(target_id),
                        "version": 0, "message": "没有这个自我条目"}, False
            current_version = int(target.get("version") or 1)
            if target.get("status") == "deleted":
                return {"ok": False, "error": "deleted", "id": target["id"],
                        "version": current_version,
                        "message": "自我条目已删除，不能复活"}, False
            if expected is None:
                return {"ok": False, "error": "version-required",
                        "id": target["id"], "version": current_version,
                        "message": "修改已有自我条目必须提供 expected_version"}, False
            if expected != current_version:
                return {"ok": False, "error": "version-conflict", "id": target["id"],
                        "version": current_version,
                        "message": "条目已被其他过程改过，请读新版本再合并"}, False
            if op == "merge" and other_expected is None:
                return {"ok": False, "error": "version-required",
                        "id": target["id"], "version": current_version,
                        "message": "merge 必须提供 "
                                   "fields.expected_other_version 校验另一条目"}, False

            before = current_version
            if op == "delete":
                target.update({
                    "status": "deleted",
                    "text": "",
                    "valid_scope": "",
                    "reason": base_reason,
                    "source_ids": [],
                    "merged_into": "",
                    "merged_from": [],
                    "deleted_at": ts,
                    "updated_at": ts,
                    "version": before + 1,
                })
                # 历史里只留字段名，避免正文从旧版本泄漏。
                target["history"] = [{
                    "version": target["version"], "op": "delete", "at": ts,
                    "reason": base_reason, "fields": ["text"],
                }]
                self._append_change(
                    state, target_kind="self", target_id=target["id"], op="delete",
                    before_version=before, after_version=target["version"],
                    reason=base_reason, source_ids=[], now=ts, fields=["text"])
                return {"ok": True, "id": target["id"],
                        "version": target["version"], "entry": target}, True

            if target.get("status") in {"retired", "merged"}:
                return {"ok": False, "error": "inactive", "id": target["id"],
                        "version": current_version,
                        "message": "已退役/已合并的条目不参与修改，也不能复活"}, False

            if op == "retire":
                target["status"] = "retired"
                target["retired_at"] = ts
                target["updated_at"] = ts
                target["version"] = before + 1
                if base_reason:
                    target["reason"] = base_reason
                target.setdefault("history", []).append({
                    "version": target["version"], "op": "retire", "at": ts,
                    "reason": base_reason, "fields": ["status"],
                })
                self._append_change(
                    state, target_kind="self", target_id=target["id"], op="retire",
                    before_version=before, after_version=target["version"],
                    reason=base_reason, source_ids=source_ok, now=ts,
                    fields=["status"])
                return {"ok": True, "id": target["id"],
                        "version": target["version"], "entry": target}, True

            if op == "merge":
                def first_id(keys):
                    for key in keys:
                        value = _text(patch.get(key))
                        if value:
                            return value
                    return ""

                into_id = first_id(
                    ("into", "into_id", "merge_into", "merge_target", "target",
                     "target_id", "with_id", "merge_with"))
                source = target
                if into_id:
                    destination = self._find_self(state, into_id)
                else:
                    # 也接受 target_id 为目标条目、fields.source_id/from 为被并条目。
                    source_id = first_id(
                        ("source_id", "source", "from", "other_id"))
                    destination = target
                    source = self._find_self(state, source_id) if source_id else None
                    if source is not None:
                        into_id = destination["id"]
                    else:
                        return {"ok": False, "error": "invalid-fields",
                                "id": destination["id"], "version": before,
                                "message": "merge 需要 fields.into 指定合并目标"}, False
                if destination is None:
                    return {"ok": False, "error": "not-found", "id": into_id,
                            "version": 0, "message": "没有要合并到的自我条目"}, False
                if destination["id"] == source["id"]:
                    return {"ok": False, "error": "invalid-fields", "id": source["id"],
                            "version": int(source.get("version") or 1),
                            "message": "不能合并到自己"}, False
                dest_version = int(destination.get("version") or 1)
                source_version = int(source.get("version") or 1)
                if destination.get("status") == "deleted":
                    return {"ok": False, "error": "deleted", "id": destination["id"],
                            "version": dest_version,
                            "message": "不能合并到已删除条目"}, False
                if destination.get("status") in {"retired", "merged"}:
                    return {"ok": False, "error": "inactive", "id": destination["id"],
                            "version": dest_version,
                            "message": "不能合并到已退役/已合并条目"}, False
                if source.get("status") in {"deleted", "retired", "merged"}:
                    return {"ok": False, "error": "inactive", "id": source["id"],
                            "version": source_version,
                            "message": "已删除/退役/合并条目不参与合并"}, False

                other = destination if target is source else source
                other_current = int(other.get("version") or 1)
                if other_expected != other_current:
                    # 双 CAS：另一条目在读取后被改过就整体拒绝，两边都不写。
                    return {"ok": False, "error": "version-conflict",
                            "id": other["id"], "version": other_current,
                            "message": "另一条目已被其他过程改过，"
                                       "请读新版本再合并"}, False

                source_text = _text(source.get("text"))
                dest_text = _text(destination.get("text"))
                if source_text and source_text not in dest_text:
                    destination["text"] = (
                        (dest_text.rstrip("。；; ") + "；" if dest_text else "")
                        + source_text)
                destination["source_ids"] = list(dict.fromkeys(
                    list(destination.get("source_ids") or [])
                    + list(source.get("source_ids") or []) + source_ok))
                destination["merged_from"] = list(dict.fromkeys(
                    list(destination.get("merged_from") or [])
                    + [source["id"]] + list(source.get("merged_from") or [])))
                if base_reason:
                    destination["reason"] = base_reason
                destination["updated_at"] = ts
                destination["version"] = dest_version + 1
                destination.setdefault("history", []).append({
                    "version": destination["version"], "op": "merge-source",
                    "at": ts, "reason": base_reason,
                    "fields": ["text", "source_ids", "merged_from"],
                })
                self._append_change(
                    state, target_kind="self", target_id=destination["id"],
                    op="merge-source", before_version=dest_version,
                    after_version=destination["version"], reason=base_reason,
                    source_ids=destination["source_ids"], now=ts,
                    fields=["text", "source_ids", "merged_from"])

                source["status"] = "merged"
                source["merged_into"] = destination["id"]
                source["updated_at"] = ts
                source["version"] = source_version + 1
                source.setdefault("history", []).append({
                    "version": source["version"], "op": "merge",
                    "at": ts, "reason": base_reason,
                    "fields": ["status", "merged_into"],
                })
                self._append_change(
                    state, target_kind="self", target_id=source["id"], op="merge",
                    before_version=source_version, after_version=source["version"],
                    reason=base_reason, source_ids=source_ok, now=ts,
                    fields=["status", "merged_into"])
                return {"ok": True, "id": destination["id"],
                        "version": destination["version"],
                        "merged_id": source["id"],
                        "entry": destination}, True

            # revise：只合并传入字段，其余字段和条目原样保留。
            protected = {"id", "version", "created_at", "updated_at", "history",
                         "deleted_at", "status", "source_ids", "text", "content",
                         "title", "summary", "merged_into", "merged_from"}
            merged_keys: list[str] = []
            new_text = _field_text(patch)
            if new_text:
                target["text"] = new_text
                merged_keys.append("text")
            if patch.get("category") is not None or patch.get("kind") is not None:
                target["category"] = _one_line(
                    patch.get("category") or patch.get("kind")) or "self"
                merged_keys.append("category")
            if patch.get("valid_scope") is not None:
                target["valid_scope"] = _text(patch.get("valid_scope"))
                merged_keys.append("valid_scope")
            for key, value in patch.items():
                if key in protected or key in {"kind", "category", "valid_scope",
                                               "source_ids"}:
                    continue
                target[key] = copy.deepcopy(value)
                merged_keys.append(key)
            if source_ok:
                target["source_ids"] = list(dict.fromkeys(
                    list(target.get("source_ids") or []) + source_ok))
                merged_keys.append("source_ids")
            if not merged_keys:
                return {"ok": False, "error": "invalid-fields", "id": target["id"],
                        "version": before, "message": "没有可改的字段"}, False
            if base_reason:
                target["reason"] = base_reason
            target["updated_at"] = ts
            target["version"] = before + 1
            target.setdefault("history", []).append({
                "version": target["version"], "op": "revise", "at": ts,
                "reason": base_reason, "fields": sorted(set(merged_keys)),
            })
            self._append_change(
                state, target_kind="self", target_id=target["id"], op="revise",
                before_version=before, after_version=target["version"],
                reason=base_reason, source_ids=source_ok, now=ts,
                fields=merged_keys)
            return {"ok": True, "id": target["id"],
                    "version": target["version"], "entry": target}, True

        return self._write_op(apply)

    # ------------------------------------------------------------ 养成
    def practice(self, topic, *, experience_id, mode="understanding",
                 outcome="observed", artifact="", people=None, now=None) -> dict:
        """记录一次了解/实践。了解与操作经验分开；同一经历同一能力只计一次。"""
        name = _text(topic)
        if not name:
            raise LifeDataError("topic 不能为空")
        if not _norm_text(name):
            raise LifeDataError("topic 需要可识别的文字，不能只有 emoji/符号")
        requested = _one_line(mode).casefold()
        if requested not in {"understanding", "practice"}:
            raise LifeDataError("mode 只能是 understanding 或 practice")
        result = _one_line(outcome) or "observed"
        artifact_text = _text(artifact)
        people_list = _string_list(people)
        ts = _now(now)
        topic_key = _norm_text(name)

        def apply(state):
            exp = self._find_experience(state, experience_id)
            if exp is None:
                raise LifeDataError(f"experience_id 不存在：{experience_id!r}")
            exp_id = exp["id"]
            level, level_raw = _level(exp.get("observed_level"))
            # mode 只是调用方意图，不能升级已保存经历的证据层级；只有明确
            # 的执行/练习/实验/完成作品层级才允许计 practice。
            downgraded = requested == "practice" and level < 3
            effective = "understanding" if downgraded else requested

            skill = next((row for row in state.get("skills", [])
                          if row.get("topic_key") == topic_key), None)
            if skill is None:
                skill = {
                    "id": _new_id("skill"),
                    "topic": name,
                    "topic_key": topic_key,
                    "stage": "understanding",
                    "stage_reason": "还没有计入任何经历",
                    "understanding_count": 0,
                    "practice_count": 0,
                    "understanding_level": 0,
                    "understanding_sources": [],
                    "practice_sources": [],
                    "experience_roots": [],
                    "events": [],
                    "first_at": ts,
                    "last_at": ts,
                    "updated_at": ts,
                    "version": 1,
                }
                state["skills"].append(skill)

            def already_seen(axis: str) -> bool:
                return any(
                    event.get("experience_id") == exp_id
                    and event.get("mode") == axis
                    for event in skill.get("events", []))

            if already_seen(effective):
                return {
                    "ok": True, "counted": False, "duplicate": True,
                    "id": skill["id"], "topic": skill["topic"],
                    "mode": effective, "requested_mode": requested,
                    "downgraded": bool(downgraded),
                    "stage": skill.get("stage"),
                    "stage_reason": skill.get("stage_reason"),
                    "understanding_count": int(
                        skill.get("understanding_count") or 0),
                    "practice_count": int(skill.get("practice_count") or 0),
                    "explanation": "同一经历在同一能力上只计一次，不重复提升",
                    "experience_id": exp_id,
                    "version": skill.get("version"),
                    "skill": skill,
                }, False

            if effective == "understanding":
                skill["understanding_count"] = int(
                    skill.get("understanding_count") or 0) + 1
                if exp_id not in skill.setdefault("understanding_sources", []):
                    skill["understanding_sources"].append(exp_id)
                skill["understanding_level"] = max(
                    int(skill.get("understanding_level") or 0), level)
                change_text = "领域了解 +1"
            else:
                skill["practice_count"] = int(skill.get("practice_count") or 0) + 1
                if exp_id not in skill.setdefault("practice_sources", []):
                    skill["practice_sources"].append(exp_id)
                change_text = "实践经验 +1"

            root = str(exp.get("root_source_id") or exp_id)
            if root not in skill.setdefault("experience_roots", []):
                skill["experience_roots"].append(root)
            skill.setdefault("events", []).append({
                "experience_id": exp_id,
                "root_source_id": root,
                "mode": effective,
                "requested_mode": requested,
                "downgraded": bool(downgraded),
                "outcome": result,
                "artifact": artifact_text,
                "people": people_list,
                "observed_level": level_raw,
                "at": ts,
            })
            practice_count = int(skill.get("practice_count") or 0)
            understanding_count = int(skill.get("understanding_count") or 0)
            if practice_count >= 3:
                skill["stage"] = "experienced"
                skill["stage_reason"] = (
                    f"已有 {practice_count} 次实际练习，超过 3 次，进入有经验阶段")
            elif practice_count >= 1:
                skill["stage"] = "tried"
                skill["stage_reason"] = (
                    f"已有 {practice_count} 次实际练习、"
                    f"{understanding_count} 次了解，处在尝试阶段")
            else:
                skill["stage"] = "understanding"
                skill["stage_reason"] = (
                    f"目前只有 {understanding_count} 次领域了解，还没有实际练习")
            if downgraded:
                skill["stage_reason"] += "；这条经历只到观察层级，不能算操作能力"
            skill["last_at"] = ts
            skill["updated_at"] = ts
            skill["version"] = int(skill.get("version") or 1) + 1

            made_artifact = None
            if effective == "practice" and not downgraded and artifact_text:
                key = (exp_id, topic_key, artifact_text)
                exists = any(
                    (item.get("experience_id"), item.get("topic_key"),
                     item.get("artifact")) == key
                    for item in state.get("artifacts", []))
                if not exists:
                    made_artifact = {
                        "id": _new_id("art"),
                        "topic": name,
                        "topic_key": topic_key,
                        "experience_id": exp_id,
                        "source_experience_id": exp_id,
                        "source_ids": [exp_id],
                        "artifact": artifact_text,
                        "people": people_list,
                        "outcome": result,
                        "kind": "practice",
                        "created_at": ts,
                    }
                    state["artifacts"].append(made_artifact)

            made_memento = None
            if (effective == "practice" and not downgraded
                    and (people_list or artifact_text
                         or result.casefold() in _SIGNIFICANT_OUTCOMES)):
                exists = any(
                    (item.get("experience_id"), item.get("topic_key")) ==
                    (exp_id, topic_key)
                    for item in state.get("mementos", []))
                if not exists:
                    made_memento = {
                        "id": _new_id("memento"),
                        "topic": name,
                        "topic_key": topic_key,
                        "experience_id": exp_id,
                        "source_experience_id": exp_id,
                        "source_ids": [exp_id],
                        "outcome": result,
                        "artifact": artifact_text,
                        "people": people_list,
                        "created_at": ts,
                    }
                    state.setdefault("mementos", []).append(made_memento)

            return {
                "ok": True,
                "counted": True,
                "duplicate": False,
                "id": skill["id"],
                "topic": skill["topic"],
                "mode": effective,
                "requested_mode": requested,
                "downgraded": bool(downgraded),
                "stage": skill["stage"],
                "stage_reason": skill["stage_reason"],
                "understanding_count": int(skill.get("understanding_count") or 0),
                "practice_count": int(skill.get("practice_count") or 0),
                "explanation": skill["stage_reason"],
                "change": change_text,
                "experience_id": exp_id,
                "version": skill["version"],
                "artifact_id": (made_artifact or {}).get("id", ""),
                "memento_id": (made_memento or {}).get("id", ""),
                "skill": skill,
            }, True

        return self._write_op(apply)

    def growth_snapshot(self) -> dict:
        """有界、可查看的成长投影；资源余额始终未知，不编造。"""
        def read(state):
            def recent(rows, key, count=SNAPSHOT_LIMIT):
                return sorted(rows, key=lambda row: -float(row.get(key) or 0))[:count]

            goals = [row for row in state.get("goals", [])
                     if row.get("status") != "deleted"]
            goals.sort(key=lambda row: (
                0 if row.get("status") == "active" else 1,
                -float(row.get("updated_at") or 0)))
            changes = sorted(
                state.get("changes", []),
                key=lambda row: float(row.get("at") or 0), reverse=True,
            )[:CHANGE_LIMIT]
            return {
                "skills": recent(state.get("skills", []), "updated_at"),
                "mementos": recent(state.get("mementos", []), "created_at"),
                "artifacts": recent(state.get("artifacts", []), "created_at"),
                "changes": changes,
                "goals": goals[:SNAPSHOT_LIMIT],
                "resources": {"known": False, "balance": None, "unit": "unknown"},
                "resource_balance": "unknown",
            }

        return self._read_op(read)

    # ------------------------------------------------------------ 上下文
    def context_for(self, query, *, budget=1600, now=None) -> str:
        """只拼相关记忆/目标/自我条目；没有相关项就返回空串。"""
        query_text = _text(query)
        if not query_text:
            return ""
        try:
            char_budget = max(0, int(budget))
        except (TypeError, ValueError):
            char_budget = 1600
        if not char_budget:
            return ""
        now_ts = _now(now)

        memories = self.recall(
            query_text, limit=6, budget=min(char_budget, 1200), now=now_ts)

        def read(state):
            goals = []
            for row in state.get("goals", []):
                if row.get("status") != "active":
                    continue
                hits, bonus = self._query_hits(query_text, row.get("text"))
                if hits or bonus:
                    goals.append((hits + bonus, row))
            goals.sort(key=lambda item: (-item[0],
                                         -float(item[1].get("updated_at") or 0)))
            entries = []
            for row in state.get("self", []):
                if row.get("status") != "active":
                    continue
                hits, bonus = self._query_hits(query_text, row.get("text"))
                if hits or bonus:
                    entries.append((hits + bonus, row))
            entries.sort(key=lambda item: (-item[0],
                                           -float(item[1].get("updated_at") or 0)))
            return [row for _score, row in goals[:3]], [row for _score, row in entries[:3]]

        relevant_goals, relevant_self = self._read_op(read)
        blocks: list[str] = []
        if memories:
            lines = []
            for row in memories:
                suffix = "（候选，尚未确认）" if row.get("status") == "candidate" else ""
                lines.append(f"- {row.get('text')}{suffix}")
            if lines:
                blocks.append("相关记忆：\n" + "\n".join(lines))
        if relevant_goals:
            blocks.append("相关目标：\n" + "\n".join(
                f"- [{row.get('type')}] {row.get('text')}" for row in relevant_goals))
        if relevant_self:
            blocks.append("相关自我：\n" + "\n".join(
                f"- {row.get('text')}" for row in relevant_self))
        if not blocks:
            return ""
        text = "\n".join(blocks)
        return text[:char_budget]
