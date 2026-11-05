"""B 站写操作的优先级闸（priority capability gate）。

放在 `bili_scroll.action()` 的**最前面**，所以不管入口是模型的 `bili_action`、
定时发动态，还是以后新加的工具，只要走 `action()` 就绕不过这一层。

2026-11-03 策略：主人明确选择「完整云端策略（含批量删评论）」。闸门仍然是
**可关的总开关**，但默认与云端等价地全开；额度 0 = 不限。

硬规则（写死，不靠提示词）：
  1. **读动作永远可用**；未知动作、投稿/上传、私信/专栏一律拒绝。
     批量删评论（按最近/序号/文本/整条视频）与「点赞+收藏+投币」聚合入口
     **不再被永久拒绝**：它们和单条写一样要过下面 2~8 条（主人身份、审计、
     参数形状、额度），只是额度 0 时不再卡次数。
  2. **评论/回复、视频评论、弹幕、纯文字动态、点赞、收藏、投币、删除评论
     （单条/最近/序号/文本/整条视频）、按判断互动 react**：必须同时满足
       a. 配置里是**字面 True**（`"true"` / `1` / None 都不算），且**按动作分开**；
          总开关 ``tools.bili_write_enabled`` 仍是可关的总闸；
       b. 调用方拿得到一枚**进程内授权凭证**（owner 的一轮真实消息，或受控定时任务），
          而且这枚凭证**绑定了一个动作和一个目标**（BV 号 / BV+rpid / 序号 / 文本）。
       dict / 用户文本 / ctx=None / 伪造对象都换不到凭证；凭证也不能跨动作复用。
       **投币、删除、点赞、收藏、react 永远不能由定时任务授权**（见 ``SCHEDULER_ACTIONS``）。
  3. 公开内容路径：**按天配额 → 先预留（pending 落盘并 fsync）→ 只调用一次后端**；
     回执是 confirmed / unconfirmed 才落定；同一天同内容不重复；坏账本拒写。
     fsync 失败 = 这次不发（掉电丢 pending 会让人以为没发过而重发）。
     **额度 0 = 不限**（云端等价）；负数 / 布尔 / 字符串一律报配置错。
  4. 回执语义与云端一致：**后端返回成功即成功**；读后核验只是附加信息
     （``verified`` 字段），不再把已经成功的操作降级成 unconfirmed。
     只有后端明确报错、或回执里没有这个动作自己的可验证证据（reply/comment 的
     rpid、danmaku 的 dmid、dynamic 的 dynamic_id）时才算 unconfirmed。
     未确认 = 额度照扣、**不自动重试**（同内容再来会被去重拦下，跨天也一样）。
     **失败不谎报**：后端失败 / 抛异常一律不报成功。
  5. 审计只记动作/状态/哈希/字数/来源，不记正文、不记凭证；审计必须 fsync，
     写不进去就不发 / 不报 confirmed（fail-closed）。
  6. 熔断状态**按 bot 隔离**：一个 bot 的账本坏了不能冻结别的 bot。
  7. 写动作只接受已知参数形状（reply: bvid/text/rpid/root；dynamic: text；
      comment/send_danmaku: bvid/text；like/favorite: bvid；coin: bvid/num；
      delete_comment: bvid/rpid；delete_recent: n；delete_by_index: index；
      delete_by_text: text；delete_on_video: bvid；react: bvid/judge），
     多传的参数（images / video_path / schedule_time / 任意 URL）一律拒绝。
  8. **删除目标必须真是凭证账户自己发的**：``delete_comment`` 先核实作者；
     批量入口只删账本里记着的、或评论区里 ``member.mid`` 等于凭证账户的评论。
     调用方必须先核实身份，再用 ``authorize()`` 无副作用预检，最后才 ``guard()`` 预留。
     删除不参与「内容去重」（删两次不会产生重复公开内容）；发布类动作照旧去重。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid

from . import paths as layout
from .log import log

# ---------------------------------------------------------------- 动作分类
READ_ACTIONS = frozenset({
    "search", "video_info", "subtitle", "comments", "danmaku", "crawl",
    "hot", "buzzwords", "weekly", "rank", "zones",
    "user_info", "search_user", "user_videos",
    "favorites", "favorite_content",
    "unread", "received_replies", "received_at",
    # 只读查询：凭证账户自己在某个视频下发过的评论 / 她自己还有多少硬币。
    "my_comments", "coin_balance",
})

# 只允许「纯文字」的公开写动作
PUBLIC_TEXT_ACTIONS = frozenset({"reply", "dynamic", "comment", "send_danmaku"})

# 点赞 / 收藏 / 投币：后端回执不可信，读后核验只作附加信息。
REACTION_ACTIONS = frozenset({"like", "favorite", "coin"})

# 按她自己的判断一次做点赞/收藏/投币（云端原版的「互动」聚合入口）。
REACT_ACTIONS = frozenset({"react"})

# 删除：单条 + 批量（最近 / 序号 / 文本 / 整条视频）。批量入口只删自己的评论。
DELETE_ACTIONS = frozenset({"delete_comment", "delete_recent", "delete_by_index",
                            "delete_by_text", "delete_on_video"})

WRITE_ACTIONS = (PUBLIC_TEXT_ACTIONS | REACTION_ACTIONS | REACT_ACTIONS
                 | DELETE_ACTIONS)

# 这些动作不能只看后端回执：读后核验是**附加信息**（不再降级成功）。
VERIFY_ONLY_ACTIONS = REACTION_ACTIONS | REACT_ACTIONS | DELETE_ACTIONS

# 受控定时任务最多只能授权这几类文本写。投币/删除/点赞/收藏/react 永远不由
# scheduler 授权；刷片后的自动互动按云端原版由 ``bili_extra.react`` 自己判断
# （开关是 tools.bili_scroll_react_*，另有账本去重）。
SCHEDULER_ACTIONS = frozenset({"reply", "dynamic", "comment", "send_danmaku"})

# 读动作里的 "danmaku" 是「看弹幕」；发弹幕单独叫 send_danmaku，避免同一个
# 名字既当读又当写（否则读弹幕会被当成写、发弹幕会被当成读，两边都错）。
_CONFIG_KEY = {"send_danmaku": "danmaku"}

ALWAYS_DENIED = {
    "upload_video": "投稿上传不在授权范围（要本地文件）",
    "upload_video_multi": "投稿上传不在授权范围（要本地文件）",
    "send_message": "私信不在授权范围",
    "opus": "专栏/图文发布不在授权范围",
    "login": "登录不在授权范围",
}

# 批量删除、「一键点赞+收藏+投币」按主人 2026-11-03 的选择开放：
# 它们走和单条写一样的授权 / 审计 / 参数校验，不再被永久拒绝。
REJECTED_ACTIONS: dict[str, str] = {}


def refusal(name: str) -> str:
    """这个动作是否被明确拒绝；返回中文理由，未知动作为空串。"""
    return str(REJECTED_ACTIONS.get(str(name or "").strip().lower(), ""))


# 每类动作的缺省配额（每天）；0 = 不限（云端等价）。动态会尽量跟随
# tools.bili_post_daily_limit。
_DEFAULT_QUOTA = {kind: 0 for kind in WRITE_ACTIONS}

# 配置键（都在 tools. 下）：bili_write_<key>，**每个动作单独一个开关**。
# 2026-11-03：默认与云端等价地全开；闸门仍是可关的总开关。
_默认 = {
    "enabled": True,           # 总开关（字面 True）
    "reply": True,             # 评论/回复（字面 True）
    "dynamic": True,           # 纯文字动态（字面 True）
    "comment": True,           # 视频顶层评论（字面 True）
    "send_danmaku": True,      # 发弹幕（配置键 tools.bili_write_danmaku；字面 True）
    "like": True,              # 点赞（字面 True）
    "favorite": True,          # 收藏（字面 True）
    "coin": True,              # 投币（字面 True）
    "delete_comment": True,    # 单条删除自己的评论（字面 True）
    "delete_recent": True,     # 删最近几条自己的评论（字面 True）
    "delete_by_index": True,   # 按序号删自己的评论（字面 True）
    "delete_by_text": True,    # 按文本删自己的评论（字面 True）
    "delete_on_video": True,   # 删整条视频下自己发过的评论（字面 True）
    "react": True,             # 按判断自动点赞/收藏/投币（字面 True）
    "scheduler": True,         # 允许受控定时任务发起写操作（字面 True）
    "quota": {},               # 每个动作一个每日上限；0 / 不填 = 不限
    "max_chars": 300,
    "ledger": "bili_write_ledger.json",
    "audit": "bili_write_audit.jsonl",
}

_LEDGER_VERSION = 1
_KEEP_DAYS = 7
_MAX_ROWS_PER_DAY = 200
_AUDIT_MAX_BYTES = 512 * 1024
_GRANT_TTL = 180.0
_GRANT_MAX_BUDGET = 3

_LOCK = threading.RLock()

# 进程内「熔断」：账本写不进去 / 回执无法落定之后，拒绝**这个 bot** 的后续写，
# 直到重启。键是 bot 自己的数据根，所以一个 bot 坏了不会连坐别的 bot。
_FROZEN: dict[str, str] = {}

_BV = re.compile(r"^BV[0-9A-Za-z]{8,12}$")

# 写动作只允许这些参数：接口/模型再怎么塞，也塞不进图片/文件/URL/定时。
_ALLOWED_ARGS = {
    "reply": frozenset({"bvid", "text", "rpid", "root"}),
    "dynamic": frozenset({"text"}),
    "comment": frozenset({"bvid", "text"}),
    "send_danmaku": frozenset({"bvid", "text", "progress"}),
    "like": frozenset({"bvid"}),
    "favorite": frozenset({"bvid"}),
    "coin": frozenset({"bvid", "num"}),
    "delete_comment": frozenset({"bvid", "rpid"}),
    # 批量删除（云端等价）：只接受「最近几条 / 序号 / 文本 / 整条视频」，
    # 不接受 URL、文件、任意 rpid 列表。
    "delete_recent": frozenset({"n"}),
    "delete_by_index": frozenset({"index"}),
    "delete_by_text": frozenset({"text"}),
    "delete_on_video": frozenset({"bvid"}),
    # 按判断互动：判断只收窄成 like/fav/coin/react 四个键。
    "react": frozenset({"bvid", "judge"}),
}

# 回执里必须找到的「这个动作自己的」证据键。
_EVIDENCE_KEYS = {
    "reply": ("rpid", "rpid_str"),
    "comment": ("rpid", "rpid_str"),
    "dynamic": ("dynamic_id", "dynamic_id_str", "dyn_id", "dyn_id_str"),
    "send_danmaku": ("dmid", "dmid_str", "dm_id", "dm_id_str"),
}


class GateError(RuntimeError):
    """闸门内部状态坏了（账本/配置）——一律 fail closed。"""


# ================================================================ 落盘 / 位置
def _bot_key(bot) -> str:
    """每个 bot 自己的数据根（熔断/路径都按它区分）。"""
    try:
        root = str(getattr(layout.for_bot(bot), "root", "") or "")
    except Exception:
        root = ""
    if not root:
        root = str(getattr(bot, "base_dir", "") or "")
    return os.path.abspath(root) if root else "default"


def _fsync_fd(fd: int) -> None:
    """把已写入的文件描述符刷到盘上（掉电测试会替换它）。"""
    os.fsync(fd)


def _fsync_dir(dirpath: str) -> None:
    """目录项也落盘（POSIX 需要）；Windows 上打不开目录就跳过。"""
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


# ================================================================ 配置
def _cfg(bot, key, default=None):
    actual = _CONFIG_KEY.get(str(key), str(key))
    try:
        return bot.config.get(f"tools.bili_write_{actual}", _默认.get(key, default))
    except Exception:
        return _默认.get(key, default)


def _literal_true(value) -> bool:
    return value is True


def _kind_enabled(bot, kind: str) -> bool:
    return _literal_true(_cfg(bot, "enabled", False)) and _literal_true(_cfg(bot, kind, False))


def _max_chars(bot) -> int:
    raw = _cfg(bot, "max_chars", 300)
    if type(raw) is not int or raw <= 0 or raw > 2000:
        raise GateError("tools.bili_write_max_chars 必须是 1~2000 的整数")
    return raw


def _quota(bot, kind: str) -> int:
    """这个动作今天的上限；**0 = 不限**（云端等价）。"""
    raw = _cfg(bot, "quota", {})
    if raw not in (None, {}):
        if not isinstance(raw, dict):
            raise GateError("tools.bili_write_quota 必须是对象")
        config_key = _CONFIG_KEY.get(kind, kind)
        value = raw.get(config_key)
        if value is None and config_key != kind:
            value = raw.get(kind)
        if value is not None:
            if type(value) is not int or value < 0:
                raise GateError(
                    f"tools.bili_write_quota.{config_key} 必须是 0（不限）或正整数")
            return value
    if kind == "dynamic":
        # 跟自动发动态的既有上限对齐，避免两套额度互相打架
        try:
            post_limit = bot.config.get("tools.bili_post_daily_limit", None)
        except Exception:
            post_limit = None
        if type(post_limit) is int and post_limit > 0:
            return post_limit
    return _DEFAULT_QUOTA.get(kind, 0)


# ================================================================ 位置
def _state_path(bot, name: str) -> str:
    value = str(name or "").strip() or "bili_write_ledger.json"
    try:
        return layout.for_bot(bot).state(value)
    except Exception as e:
        log.warn(f"B站写账本路径解析失败（退回数据根）：{type(e).__name__} {e}")
        root = str(getattr(bot, "base_dir", "") or ".")
        return os.path.join(root, "data", value)


def ledger_path(bot) -> str:
    return _state_path(bot, _cfg(bot, "ledger", _默认["ledger"]))


def audit_path(bot) -> str:
    return _state_path(bot, _cfg(bot, "audit", _默认["audit"]))


# ================================================================ 授权凭证
_TOKEN = object()


class WriteGrant:
    """进程内授权凭证。只能由本模块签发；参数里塞 dict/字符串换不到它。

    凭证**绑定一个动作和一个目标**：给「评论 BV1xx」的授权不能拿去投币，
    也不能换个 BV 号再用一次。绑定在签发时写死，``__slots__`` 之外加不上去。
    """

    __slots__ = ("_nonce", "_token", "_source", "_action", "_target")

    def __init__(self, token, nonce: str, source: str,
                 action: str = "", target: str = ""):
        if token is not _TOKEN:
            raise TypeError("WriteGrant 只能由 bili_gate 内部签发")
        self._nonce = nonce
        self._token = token
        self._source = source
        self._action = str(action or "")
        self._target = str(target or "")

    @property
    def source(self) -> str:
        return str(self._source)

    @property
    def action(self) -> str:
        return str(self._action)

    @property
    def target(self) -> str:
        return str(self._target)

    def __repr__(self) -> str:  # 不暴露 nonce
        return f"<WriteGrant {self._source} {self._action}>"


class SchedulerCapability:
    """受控定时任务持有的能力。启动时领一次，进程内复用。"""

    __slots__ = ("_name",)

    def __init__(self, name: str):
        self._name = str(name or "default")

    def __repr__(self) -> str:
        return f"<SchedulerCapability {self._name}>"


_GRANTS: dict[str, dict] = {}
_SCHEDULERS: dict[str, SchedulerCapability] = {}


def _purge_grants(now: float) -> None:
    for nonce, rec in list(_GRANTS.items()):
        if rec["expires"] < now:
            _GRANTS.pop(nonce, None)


def _mint(source: str, action: str, target: str, budget: int = 1) -> WriteGrant:
    budget = max(1, min(_GRANT_MAX_BUDGET, int(budget)))
    nonce = secrets.token_hex(16)
    grant = WriteGrant(_TOKEN, nonce, source, action, target)
    _GRANTS[nonce] = {"grant": grant, "left": budget, "expires": time.time() + _GRANT_TTL}
    return grant


def _lookup(grant) -> dict | None:
    if type(grant) is not WriteGrant:
        return None
    rec = _GRANTS.get(getattr(grant, "_nonce", ""))
    if rec is None or rec["grant"] is not grant:
        return None
    if rec["expires"] < time.time():
        _GRANTS.pop(grant._nonce, None)
        return None
    if rec["left"] <= 0:
        return None
    return rec


def _consume(rec: dict) -> None:
    rec["left"] = max(0, int(rec["left"]) - 1)


def _owner_ids(bot) -> set[str]:
    try:
        raw = bot.config.get("account.owner_ids", []) or []
    except Exception:
        return set()
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def owner_turn_grant(bot, ctx, *, action: str, target: str,
                     budget: int = 1) -> WriteGrant | None:
    """从「主人的一条真实消息」换一枚写凭证；换不到就回 None。

    明确不接受：None / dict / 字符串 / 数字 / 没有 event 的对象 /
    发送者不在 account.owner_ids 的事件。
    ``action`` / ``target`` 必须由调用方按**这条消息明确要求的那一件事**填：
    凭证只对这一个动作 + 这一个目标有效，不能拿来干别的。
    """
    if ctx is None or isinstance(ctx, (dict, list, tuple, str, bytes, int, float, bool)):
        return None
    event = getattr(ctx, "event", None)
    if event is None or isinstance(event, (dict, list, str)):
        return None
    if getattr(event, "post_type", "") != "message":
        return None
    owner = getattr(ctx, "bot", None)
    if owner is not None and owner is not bot:
        return None
    uid = getattr(event, "user_id", None)
    if uid is None:
        return None
    if str(uid) not in _owner_ids(bot):
        return None
    name = str(action or "").strip().lower()
    where = str(target or "").strip()
    if name not in WRITE_ACTIONS or not where:
        return None
    return _mint("owner_turn", name, where, budget)


def register_scheduler(name: str = "default") -> SchedulerCapability:
    """受控定时任务启动时领一枚能力（同名复用，不重复发）。"""
    key = str(name or "default")
    with _LOCK:
        cap = _SCHEDULERS.get(key)
        if cap is None:
            cap = SchedulerCapability(key)
            _SCHEDULERS[key] = cap
        return cap


def scheduler_grant(capability, *, action: str, target: str,
                    budget: int = 1) -> WriteGrant | None:
    """受控定时任务把能力换成一次性写凭证；不是登记的同一枚就回 None。

    投币 / 删除 / 点赞 / 收藏**不在** ``SCHEDULER_ACTIONS`` 里：定时任务
    换不到它们的凭证，必须由主人当轮明确指令单独授权。
    """
    if type(capability) is not SchedulerCapability:
        return None
    name = str(action or "").strip().lower()
    where = str(target or "").strip()
    if name not in SCHEDULER_ACTIONS or not where:
        return None
    with _LOCK:
        registered = _SCHEDULERS.get(capability._name)
        if registered is not capability:
            return None
    return _mint(f"scheduler:{capability._name}", name, where, budget)


def reset_for_tests() -> None:
    """只给测试用：清掉进程内凭证/熔断状态。"""
    with _LOCK:
        _GRANTS.clear()
        _SCHEDULERS.clear()
        _FROZEN.clear()


# ================================================================ 账本
def _fresh_ledger() -> dict:
    return {"version": _LEDGER_VERSION, "days": {}}


def _load_ledger(bot) -> dict:
    path = ledger_path(bot)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _fresh_ledger()
    except Exception as e:
        raise GateError(f"写账本读不出来（{type(e).__name__}）：{path}") from e
    if not isinstance(data, dict):
        raise GateError("写账本不是对象")
    if data.get("version") != _LEDGER_VERSION:
        raise GateError("写账本版本不认识")
    days = data.get("days")
    if not isinstance(days, dict):
        raise GateError("写账本缺 days")
    for day, bucket in days.items():
        if not isinstance(bucket, dict):
            raise GateError(f"写账本 [{day}] 不是对象")
        rows = bucket.get("rows")
        if rows is None:
            bucket["rows"] = []
            continue
        if not isinstance(rows, list):
            raise GateError(f"写账本 [{day}].rows 不是数组")
        for row in rows:
            if not isinstance(row, dict) or not row.get("kind") or not row.get("id"):
                raise GateError(f"写账本 [{day}] 有坏行")
    return data


def _save_ledger(bot, ledger: dict) -> None:
    path = ledger_path(bot)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False)
            # 预留行必须先真正落盘，再 os.replace：否则掉电会丢 pending 记录，
            # 下一次启动就会把「可能已经发过」的内容当成没发过。
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


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _bucket(ledger: dict, day: str) -> dict:
    bucket = ledger["days"].get(day)
    if not isinstance(bucket, dict):
        bucket = {"rows": []}
        ledger["days"][day] = bucket
    return bucket


def _prune(ledger: dict, today: str) -> None:
    for day in list(ledger["days"].keys()):
        if day >= today:
            continue
        try:
            old = (time.mktime(time.strptime(today, "%Y-%m-%d"))
                   - time.mktime(time.strptime(day, "%Y-%m-%d"))) / 86400.0
        except Exception:
            old = 0
        if old >= _KEEP_DAYS:
            ledger["days"].pop(day, None)


def _target_key(kind: str, args: dict) -> str:
    if kind in ("reply", "delete_comment"):
        bvid = str(args.get("bvid") or "")
        rpid = args.get("rpid") or 0
        return f"{bvid}|{rpid}"
    if kind == "delete_recent":
        return f"recent|{int(args.get('n') or 1)}"
    if kind == "delete_by_index":
        return f"index|{int(args.get('index') or 0)}"
    if kind == "delete_by_text":
        text = re.sub(r"\s+", " ", str(args.get("text") or "")).strip()
        # 审计/账本只存哈希，不把用户输入的文本原样落盘。
        return "text|" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if kind in ("comment", "send_danmaku", "like", "favorite", "coin",
                "delete_on_video", "react"):
        return str(args.get("bvid") or "")
    return "dynamic"


def target_key(kind: str, args: dict | None = None) -> str:
    """给调用方算「这次授权绑定哪个目标」用（和 guard 内的算法同一份）。"""
    return _target_key(str(kind or "").strip().lower(),
                       args if isinstance(args, dict) else {})


def _content_key(kind: str, args: dict) -> str:
    text = re.sub(r"\s+", " ", str(args.get("text") or "")).strip()
    raw = f"{kind}\x00{_target_key(kind, args)}\x00{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_ok(row: dict) -> bool:
    return row.get("status") in ("pending", "confirmed", "unconfirmed")


# ================================================================ 审计
def _audit(bot, record: dict) -> bool:
    """追加一条审计并 fsync；写不进去回 False（调用方必须 fail-closed）。"""
    path = audit_path(bot)
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            if os.path.getsize(path) > _AUDIT_MAX_BYTES:
                os.replace(path, path + ".1")
                _fsync_dir(d)
        except OSError:
            pass
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            # 审计是「这次公开写到底发生了什么」的唯一痕迹，必须落盘。
            f.flush()
            _fsync_fd(f.fileno())
        return True
    except OSError as e:
        log.warn(f"B站写审计写不进去：{e}")
        return False


def _audit_base(bot, kind: str, args: dict, source: str) -> dict:
    text = str(args.get("text") or "")
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": kind,
        "source": source,
        "target_hash": hashlib.sha256(_target_key(kind, args).encode("utf-8")).hexdigest()[:16],
        "content_hash": _content_key(kind, args)[:16],
        "chars": len(text),
    }


# ================================================================ 拒绝
def _deny(bot, kind: str, args: dict, source: str, reason: str, code: str) -> dict:
    record = _audit_base(bot, kind, args if isinstance(args, dict) else {}, source)
    record.update({"status": "denied", "code": code, "reason": reason[:120]})
    _audit(bot, record)
    log.info(f"B站写操作被拒（{kind}/{code}）：{reason[:80]}")
    return {"ok": False, "denied": True, "code": code, "error": reason,
            "hint": "这是权限/额度限制，不是临时故障；没有主人授权或额度用完就不要重试。"}


# ================================================================ 许可 + 预留
def _authorize_checks(bot, name: str, args: dict, grant) -> tuple[dict, dict, int]:
    """开关 / 凭证绑定 / 参数形状的共用检查。通过返回 (cleaned, rec, quota)。"""
    frozen = _FROZEN.get(_bot_key(bot), "")
    if frozen:
        raise GateError(frozen)
    if not _literal_true(_cfg(bot, "enabled", False)):
        raise GateError("自主写 B 站没开（tools.bili_write_enabled 要是字面 true）")
    if not _literal_true(_cfg(bot, name, False)):
        # 拒绝文案必须写**真实存在的配置键**（send_danmaku 的键是
        # tools.bili_write_danmaku），否则用户照着去改永远改不对。
        config_key = _CONFIG_KEY.get(name, name)
        raise GateError(
            f"这一类写操作没授权（tools.bili_write_{config_key} 要是字面 true）")
    rec = _lookup(grant)
    if rec is None:
        raise GateError("缺少进程内授权凭证（owner 的一轮真实消息或受控定时任务才有）")
    g = rec["grant"]
    if not g.action or g.action != name:
        raise GateError(
            f"凭证绑定的动作是「{g.action or '未绑定'}」，不能用来做 {name}；"
            "一次授权只够一个动作")
    cleaned = _validate_args(name, args, _max_chars(bot))
    target = _target_key(name, cleaned)
    if not g.target or g.target != target:
        raise GateError(
            f"凭证绑定的目标不是这次要动的（凭证「{g.target or '未绑定'}」/ 本次「{target}」）；"
            "一次授权只够一个目标")
    if g.source.startswith("scheduler:"):
        if not _literal_true(_cfg(bot, "scheduler", False)):
            raise GateError("受控定时任务写操作没授权（tools.bili_write_scheduler 要是字面 true）")
        if name not in SCHEDULER_ACTIONS:
            raise GateError(
                f"定时任务不能授权 {name}：投币/删除/点赞/收藏/react 只能由主人当轮明确指令授权")
    return cleaned, rec, _quota(bot, name)


def authorize(bot, action: str, args: dict | None = None, *, grant=None) -> dict:
    """**无副作用**预检：只查开关/凭证绑定/参数形状，不预留、不扣额度。

    给「先读后写」的动作用（删除评论要先核实评论作者）：预检不过就根本不发读请求；
    过完预检还要走 ``guard()`` 才真正预留——那里会把所有检查重做一遍，这里
    不是为了绕过闸门，而是为了在**不消耗额度**的前提下拒绝明显越权的调用。
    """
    name = str(action or "").strip().lower()
    args = args if isinstance(args, dict) else {}
    source = str(getattr(grant, "source", "") or "none")
    if name in READ_ACTIONS:
        return {"ok": True, "read": True}
    if name in ALWAYS_DENIED:
        return _deny(bot, name, args, source, ALWAYS_DENIED[name], "action_not_allowed")
    拒绝 = refusal(name)
    if 拒绝:
        return _deny(bot, name, args, source, 拒绝, "action_not_allowed")
    if name not in WRITE_ACTIONS:
        return _deny(bot, name, args, source, f"没有这个动作：{action}", "unknown_action")
    try:
        cleaned, rec, quota = _authorize_checks(bot, name, args, grant)
    except GateError as e:
        return _deny(bot, name, args, source, str(e), "not_authorized")
    return {"ok": True, "action": name, "args": cleaned,
            "target": _target_key(name, cleaned),
            "source": rec["grant"].source, "quota": quota}


def guard(bot, action: str, args: dict | None = None, *, grant=None) -> dict:
    """写动作的许可与预留。返回 {ok: True, reservation} 或拒绝 dict。**永不抛。**"""
    name = str(action or "").strip().lower()
    args = args if isinstance(args, dict) else {}
    source = str(getattr(grant, "source", "") or "none")

    if name in READ_ACTIONS:
        return {"ok": True, "read": True}
    if name in ALWAYS_DENIED:
        return _deny(bot, name, args, source, ALWAYS_DENIED[name], "action_not_allowed")
    拒绝 = refusal(name)
    if 拒绝:
        return _deny(bot, name, args, source, 拒绝, "action_not_allowed")
    if name not in WRITE_ACTIONS:
        return _deny(bot, name, args, source, f"没有这个动作：{action}",
                     "unknown_action")

    try:
        cleaned, rec, quota = _authorize_checks(bot, name, args, grant)
        # 调用方（bili_scroll.action）随后把**同一个 args dict** 转给后端；
        # 这里就地换成校验后的形状，后端拿到的就是通过校验的那份值
        # （不会出现"校验的是副本、传的是原文"）。
        if isinstance(args, dict) and args:
            args.clear()
            args.update(cleaned)
    except GateError as e:
        return _deny(bot, name, args, source, str(e), "not_authorized")

    key = _content_key(name, cleaned)
    with _LOCK:
        try:
            ledger = _load_ledger(bot)
        except GateError as e:
            return _deny(bot, name, cleaned, source, str(e), "ledger_unusable")
        day = _today()
        _prune(ledger, day)
        bucket = _bucket(ledger, day)
        rows = bucket["rows"]
        used = sum(1 for r in rows if r.get("kind") == name and _row_ok(r))
        # 额度 0 = 不限（云端等价）；负数 / 布尔 / 字符串在 _quota() 里已报配置错。
        if quota > 0 and used >= quota:
            return _deny(bot, name, cleaned, source,
                         f"今天的额度用完了（{name} 上限 {quota}/天）", "quota_exhausted")
        if name not in DELETE_ACTIONS:
            # 发布类动作：跨天/当天同内容去重（避免重复公开内容）。删除是幂等的
            # （删两次不会产生重复内容），所以只做授权、审计与额度，不去重。
            for other_day, other_bucket in ledger["days"].items():
                if other_day == day or not isinstance(other_bucket, dict):
                    continue
                for row in other_bucket.get("rows") or []:
                    if row.get("key") == key and row.get("status") == "unconfirmed":
                        return _deny(bot, name, cleaned, source,
                                     f"同内容在 {other_day} 没确认，不自动重试（避免重复发）",
                                     "dedupe_unconfirmed")
            for row in rows:
                if row.get("key") != key or not _row_ok(row):
                    continue
                if row.get("status") == "unconfirmed":
                    return _deny(bot, name, cleaned, source,
                                 "同内容上次没确认，今天不重试（避免重复发）", "dedupe_unconfirmed")
                return _deny(bot, name, cleaned, source,
                             "同内容今天已经发过", "dedupe")

        row = {
            "id": uuid.uuid4().hex,
            "kind": name,
            "key": key,
            "status": "pending",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": rec["grant"].source,
            "target": _target_key(name, cleaned),
            "chars": len(str(cleaned.get("text") or "")),
        }
        rows.append(row)
        if len(rows) > _MAX_ROWS_PER_DAY:
            del rows[:_MAX_ROWS_PER_DAY // 2]
        try:
            _save_ledger(bot, ledger)
        except OSError as e:
            log.warn(f"B站写预留落盘失败：{e}")
            return _deny(bot, name, cleaned, source,
                         "写预留落盘失败，这次不发（避免重复）", "ledger_write_failed")
        _consume(rec)

    record = _audit_base(bot, name, cleaned, rec["grant"].source)
    record.update({"status": "reserved", "reservation": row["id"]})
    if not _audit(bot, record):
        # 审计写不进去也不发：宁可少发一条，也不做没有痕迹的公开写入
        with _LOCK:
            row["status"] = "denied"
            try:
                _save_ledger(bot, ledger)
            except OSError:
                pass
        return _deny(bot, name, cleaned, source,
                     "审计写不进去，这次不发", "audit_failed")
    return {"ok": True, "reservation": row["id"], "kind": name, "source": rec["grant"].source}


def _validate_args(kind: str, args: dict, max_chars: int) -> dict:
    """只接受已知参数形状；多出来的键一律拒绝（防止借参数传图片/文件/URL/定时）。"""
    allowed = _ALLOWED_ARGS.get(kind, frozenset())
    extra = sorted(str(k) for k in args if str(k) not in allowed)
    if extra:
        raise GateError(
            f"{kind} 只接受参数 {sorted(allowed)}；"
            f"{'、'.join(extra)} 一律拒绝（不能借参数传图片/文件/URL/定时）")

    def _bvid_of() -> str:
        bvid = str(args.get("bvid") or "").strip()
        if not _BV.match(bvid):
            raise GateError("bvid 不合法")
        return bvid

    # ---- 点赞 / 收藏 / 整条视频删除：只有一个明确视频目标 ----
    if kind == "like":
        return {"bvid": _bvid_of()}
    if kind == "favorite":
        return {"bvid": _bvid_of()}
    if kind == "delete_on_video":
        return {"bvid": _bvid_of()}
    # ---- 投币：目标 + 1/2 个硬币；不接受 bool / 字符串 / 任意数字 ----
    if kind == "coin":
        bvid = _bvid_of()
        num = args.get("num", 1)
        if isinstance(num, bool) or not isinstance(num, int) or num not in (1, 2):
            raise GateError("投币数量只能是整数 1 或 2")
        return {"bvid": bvid, "num": num}
    # ---- react：目标 + 她自己的判断（只收窄成 like/fav/coin/react 四个键） ----
    if kind == "react":
        bvid = _bvid_of()
        judge = args.get("judge")
        if judge is None:
            judge = {}
        if not isinstance(judge, dict):
            raise GateError("judge 必须是对象")
        extra = sorted(str(k) for k in judge
                       if str(k) not in ("like", "fav", "coin", "react"))
        if extra:
            raise GateError(
                f"react.judge 只接受 like/fav/coin/react；{'、'.join(extra)} 一律拒绝")
        coin = judge.get("coin") or 0
        if isinstance(coin, bool) or not isinstance(coin, int) or coin not in (0, 1, 2):
            raise GateError("react.judge.coin 只能是整数 0 / 1 / 2")
        return {"bvid": bvid,
                "judge": {"like": judge.get("like") is True,
                          "fav": judge.get("fav") is True,
                          "coin": coin,
                          "react": str(judge.get("react") or "")[:200]}}
    # ---- 删除：单条认「视频 BV 号 + 明确 rpid」；批量入口只认自己的账本/评论区 ----
    if kind == "delete_comment":
        bvid = _bvid_of()
        rpid = args.get("rpid")
        if isinstance(rpid, bool):
            raise GateError("rpid 必须是明确的正整数")
        if isinstance(rpid, str) and rpid.strip().isdigit():
            rpid = int(rpid.strip())
        if not isinstance(rpid, int) or rpid <= 0:
            raise GateError("rpid 必须是明确的正整数（不接受序号/文本/最近）")
        return {"bvid": bvid, "rpid": rpid}
    if kind == "delete_recent":
        n = args.get("n", 1)
        if isinstance(n, bool) or not isinstance(n, int) or not (1 <= n <= 999):
            raise GateError("n 必须是 1~999 的整数（999 = 账本里最近的全部）")
        return {"n": n}
    if kind == "delete_by_index":
        index = args.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not (1 <= index <= 999):
            raise GateError("index 必须是 1~999 的整数")
        return {"index": index}
    if kind == "delete_by_text":
        raw = args.get("text")
        if not isinstance(raw, str):
            raise GateError("text 必须是字符串")
        text = raw.strip()
        if not text:
            raise GateError("text 不能是空的")
        if len(text) > 100:
            raise GateError("text 太长（超过 100 字）")
        return {"text": text}

    text = str(args.get("text") or "")
    if not isinstance(args.get("text"), str):
        raise GateError("text 必须是字符串")
    text = text.strip()
    if not text:
        raise GateError("text 不能是空的")
    if len(text) > max_chars:
        raise GateError(f"text 太长（超过 {max_chars} 字）")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise GateError("text 里有控制字符")
    if kind in ("reply", "comment", "send_danmaku"):
        bvid = _bvid_of()
    if kind == "reply":
        rpid = args.get("rpid") or 0
        if isinstance(rpid, bool):
            raise GateError("rpid 不合法")
        if isinstance(rpid, str) and rpid.isdigit():
            rpid = int(rpid)
        if not isinstance(rpid, int) or rpid < 0:
            raise GateError("rpid 不合法")
        out = {"bvid": bvid, "text": text, "rpid": rpid}
        root = args.get("root") or 0
        if isinstance(root, str) and root.isdigit():
            root = int(root)
        if isinstance(root, int) and not isinstance(root, bool) and root > 0:
            out["root"] = root
        return out
    if kind == "comment":
        return {"bvid": bvid, "text": text}
    if kind == "send_danmaku":
        if len(text) > 60:
            raise GateError("弹幕最多 60 字")
        raw = args.get("progress", 5.0)
        try:
            progress = float(raw)
        except (TypeError, ValueError):
            raise GateError("progress 必须是数字") from None
        if progress != progress or progress < 0 or progress > 3600:
            raise GateError("progress 必须在 0~3600 秒之间")
        return {"bvid": bvid, "text": text, "progress": round(progress, 3)}
    # dynamic：只有纯文字，参数白名单里已经没有 images 了
    if args.get("images"):
        raise GateError("只允许纯文字动态（images 一律拒绝）")
    return {"text": text}


# ================================================================ 回执落定
def _find_evidence(kind: str, payload) -> dict:
    """在回执里找**这个动作自己的** id（rpid / dynamic_id），最多下探 3 层。

    只认非零数字（int 或纯数字串）；bool、空串、别的动作的 id 都不算。
    返回 {"rpid": "321"} / {"dynamic_id": "777"} 这类，找不到就是 {}。
    """
    wanted = _EVIDENCE_KEYS.get(kind, ())
    if not wanted:
        return {}
    found: dict = {}

    def walk(node, depth: int) -> None:
        if depth > 3:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key)
                if name in wanted and name not in found:
                    text = str(value).strip()
                    if text.isdigit() and int(text) > 0:
                        found[name] = text
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:8]:
                walk(item, depth + 1)

    walk(payload, 0)
    return found


def _classify_response(kind: str, response) -> tuple[str, str, dict]:
    """后端回执 → (status, reason, extra)。**后端返回成功即成功**（云端等价）。

    仍然 fail-closed：回执不是对象 / ``ok`` 不是 True / 后端报错误码 / 显式
    ``error`` 字段 → unconfirmed。文本动作还要求回执里有**这个动作自己的**
    可验证 id（云端 ``_判定`` 也把「没拿到 rpid」当作没确认）。
    读后核验（``settle(verified=...)``）只是附加信息，不参与这里的判定。
    """
    if not isinstance(response, dict):
        return "unconfirmed", "回执不是对象", {}
    if response.get("ok") is not True:
        return "unconfirmed", str(response.get("error") or "后端返回失败")[:120], {}
    data = response.get("data")
    if not isinstance(data, dict):
        return "unconfirmed", "回执不是 JSON 对象", {}

    if kind in DELETE_ACTIONS:
        if kind == "delete_comment":
            # 单条删除的后端回执就是 B 站 API 的 {code, message}：code 0 即成功。
            # 「删完真的没了」由调用方的读后核验作为附加信息给出，不参与判定。
            return "confirmed", "", {}
        # 批量删除：bili_extra 返回 {ok, deleted, failed}；一条都没删掉不算成功。
        if data.get("ok") is not True:
            return "unconfirmed", str(data.get("reason") or "一条都没删掉")[:120], {}
        return "confirmed", "", {"deleted": len(data.get("deleted") or []),
                                 "failed": len(data.get("failed") or [])}

    if kind == "react":
        errors = [str(x) for x in (data.get("errors") or [])]
        did = (bool(data.get("like")) or bool(data.get("fav"))
               or int(data.get("coin") or 0) > 0)
        if errors and not did:
            return "unconfirmed", "；".join(errors)[:120], {}
        extra: dict = {"like": bool(data.get("like")),
                       "fav": bool(data.get("fav")),
                       "coin": int(data.get("coin") or 0)}
        if errors:
            extra["errors"] = errors[:3]
        return "confirmed", "", extra

    code = data.get("code")
    if type(code) is int and code != 0:
        return "unconfirmed", f"后端返回错误码 {code}", {}
    err = data.get("error") or data.get("error_msg")
    if err:
        return "unconfirmed", str(err)[:120], {}
    if data.get("success") is False:
        return "unconfirmed", str(
            data.get("message") or data.get("msg") or "后端返回失败")[:120], {}

    if kind in REACTION_ACTIONS:
        return "confirmed", "", {}

    if data.get("success") is not True:
        if data.get("success") is None:
            return "unconfirmed", "回执里没有 success", {}
        return "unconfirmed", "success 不是字面 true", {}
    evidence = _find_evidence(kind, data)
    if not evidence:
        what = {"reply": "rpid（评论/回复）", "comment": "rpid（视频评论）",
                "send_danmaku": "dmid（弹幕）"}.get(kind, "dynamic_id（动态）")
        return "unconfirmed", f"回执没有可验证的 {what}，不能算发布成功", {}
    extra = {}
    if "rpid" in evidence:
        extra["rpid"] = evidence["rpid"]
    elif "rpid_str" in evidence:
        extra["rpid"] = evidence["rpid_str"]
    for key in ("dmid", "dmid_str", "dm_id", "dm_id_str"):
        if key in evidence:
            extra["dmid"] = evidence[key]
            break
    for key in ("dynamic_id", "dynamic_id_str", "dyn_id", "dyn_id_str"):
        if key in evidence:
            extra["dynamic_id"] = evidence[key]
            break
    return "confirmed", "", extra


def _find_row(ledger: dict, rid: str) -> dict | None:
    for bucket in ledger["days"].values():
        if not isinstance(bucket, dict):
            continue
        for item in bucket.get("rows") or []:
            if item.get("id") == rid:
                return item
    return None


def settle(bot, reservation: str, response=None, *,
           error: BaseException | None = None, verified=None) -> dict:
    """把一次后端调用的结果落定。**永不抛。**

    云端等价语义：**后端返回成功即成功**；文本动作还要求回执里有这个动作自己的
    可验证 id。``verified`` 是调用方读后核验的结论，只作为附加信息写进账本/
    返回值，不把 confirmed 降级成 unconfirmed（失败仍然不会谎报成功）。
    """
    rid = str(reservation or "")
    if not rid:
        return {"status": "unconfirmed", "reason": "没有预留记录"}
    status: str | None = None
    reason = ""
    extra: dict = {}
    if error is not None:
        status, reason, extra = "unconfirmed", f"调用出错：{type(error).__name__}", {}

    with _LOCK:
        try:
            ledger = _load_ledger(bot)
        except GateError as e:
            _freeze(bot, str(e), "ledger_unusable")
            log.warn(f"B站写回执落不了账（{status or 'unknown'}）：{e}")
            _audit(bot, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "action": "?", "status": "unconfirmed",
                         "reservation": rid, "reason": str(e)[:120]})
            return {"status": "unconfirmed", "reason": str(e)}
        row = _find_row(ledger, rid)
        if row is None:
            _freeze(bot, "回执找不到预留记录", "reservation_missing")
            _audit(bot, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "action": "?", "status": "unconfirmed",
                         "reservation": rid, "reason": "reservation_missing"})
            return {"status": "unconfirmed", "reason": "回执找不到预留记录"}
        kind = str(row.get("kind") or "")
        if status is None:
            status, reason, extra = _classify_response(kind, response)
            if verified is not None:
                # 读后核验只是附加信息：不参与 confirmed / unconfirmed 判定。
                extra["verified"] = bool(verified)
        row["status"] = status
        row["settled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if reason:
            row["reason"] = reason[:120]
        row.update(extra)
        try:
            _save_ledger(bot, ledger)
        except OSError as e:
            # 回执落不了账 ⇒ 结果不能再算 confirmed（否则以后可能重复发）
            status = "unconfirmed"
            reason = f"回执落盘失败：{e}"
            row["status"] = status
            row["reason"] = reason[:120]
            _freeze(bot, reason, "ledger_write_failed")
            log.warn(f"B站写回执落盘失败，后续写操作熔断：{e}")

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": row.get("kind", ""),
        "source": row.get("source", ""),
        "status": status,
        "reservation": rid,
        "content_hash": str(row.get("key") or "")[:16],
        "target_hash": hashlib.sha256(
            str(row.get("target") or "").encode("utf-8")).hexdigest()[:16],
        "chars": int(row.get("chars") or 0),
    }
    if reason:
        record["reason"] = reason[:120]
    if error is not None:
        record["error"] = type(error).__name__
    audited = _audit(bot, record)
    if not audited:
        # 审计是「这次公开写到底发生了什么」的唯一痕迹：写不进去就不再写，
        # 已经落定的 confirmed 也要降级（不能对调用方说"确认成功"却没有痕迹）。
        reason = "审计写不进去，回执不再算确认"
        if status == "confirmed":
            status = "unconfirmed"
            with _LOCK:
                try:
                    ledger = _load_ledger(bot)
                    locked = _find_row(ledger, rid)
                    if locked is not None:
                        locked["status"] = status
                        locked["reason"] = reason[:120]
                        _save_ledger(bot, ledger)
                except (GateError, OSError):
                    pass
            record.update({"status": status, "reason": reason})
            _audit(bot, record)
        _freeze(bot, reason, "audit_failed")
        log.warn("B站写回执审计失败，按未确认处理并熔断")
    if status != "confirmed":
        log.info(f"B站写回执未确认（{row.get('kind')}）：{reason or '未知'}")
    return {"status": status, "reason": reason, **extra}


def downgrade(bot, reservation: str, reason: str = "", *,
              code: str = "verify_failed") -> dict:
    """把已经 confirmed 的预留**降级**为 unconfirmed（人工纠正账本用）。

    只降不升：已经 unconfirmed 的不会被改回 confirmed；找不到预留也按未确认处理。
    注意：``bili_scroll.action()`` 在云端等价语义下**不再自动调用它**——
    读后核验只是附加信息，不会因为「评论区暂时找不到」就把成功降级。
    """
    rid = str(reservation or "")
    text = str(reason or "发布后复核没找到这条")[:120]
    if not rid:
        return {"status": "unconfirmed", "reason": "没有预留记录"}
    with _LOCK:
        try:
            ledger = _load_ledger(bot)
        except GateError as e:
            _freeze(bot, str(e), "ledger_unusable")
            return {"status": "unconfirmed", "reason": str(e)}
        row = _find_row(ledger, rid)
        if row is None:
            _freeze(bot, "复核找不到预留记录", "reservation_missing")
            return {"status": "unconfirmed", "reason": "复核找不到预留记录"}
        if row.get("status") == "confirmed":
            row["status"] = "unconfirmed"
            row["reason"] = text
            row["verified"] = False
            row["settled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            try:
                _save_ledger(bot, ledger)
            except OSError as e:
                _freeze(bot, f"复核降级落盘失败：{e}", "ledger_write_failed")
                log.warn(f"B站写操作复核降级落盘失败：{e}")
                return {"status": "unconfirmed", "reason": text}
        else:
            return {"status": "unconfirmed",
                    "reason": str(row.get("reason") or text)}

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": row.get("kind", ""),
        "source": row.get("source", ""),
        "status": "unconfirmed",
        "reservation": rid,
        "reason": text,
        "code": str(code or "verify_failed"),
    }
    if not _audit(bot, record):
        _freeze(bot, "复核降级审计写不进去", "audit_failed")
    log.info(f"B站写操作复核未通过，按未确认处理：{text}")
    return {"status": "unconfirmed", "reason": text}


def _freeze(bot, reason: str, code: str) -> None:
    key = _bot_key(bot)
    with _LOCK:
        if key not in _FROZEN:
            _FROZEN[key] = f"写操作已熔断：{reason}"
            log.warn(f"B站写操作熔断（{code}）：{reason}（impact bot={key}）")


# ================================================================ 只读状态
def status(bot) -> dict:
    """给面板/诊断：今天的用量、配额、**这个 bot 自己**的熔断状态。"""
    frozen = _FROZEN.get(_bot_key(bot), "")
    try:
        ledger = _load_ledger(bot)
        bucket = _bucket(ledger, _today())
        rows = bucket["rows"]
        used = {kind: sum(1 for r in rows if r.get("kind") == kind and _row_ok(r))
                for kind in WRITE_ACTIONS}
        return {"ok": True, "day": _today(), "used": used,
                "quota": {kind: _quota(bot, kind) for kind in WRITE_ACTIONS},
                "frozen": frozen, "ledger": ledger_path(bot)}
    except GateError as e:
        return {"ok": False, "error": str(e), "frozen": frozen,
                "ledger": ledger_path(bot)}
