"""
人物档案：一个群友一份文件。

    data/people/123456.md        ← QQ 号当文件名，改群名片也不丢

每份档案长这样（纯文本，你能直接打开改）：

    # 阿明
    - 性格：说话直，爱吐槽，嘴上说不在乎其实很在意
    - 常聊：考试、游戏、B站
    - 说话方式：短句多，爱用「草」
    - 跟我：经常来杠我，但不会真生气
    - 近况：在准备数学考试

**关键设计：档案只影响语气，不会被说出来。**
模型能看到这些，但 prompt 里明确禁止「我记得你说过」「上次你提过」这种话。
真人记得朋友的事，但不会拿记忆当简历念。

档案有两种来源：
  1. 模型自己调 `remember(kind="people", name=...)` 记一条
  2. 后台观察：某个人说够 N 条，自动从这些消息里提炼一次性格（见 update_profile）
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time

from .log import log

HEADER = "# {name}\n"

# 一人一把锁：同一个人的档案读-改-写必须串行，不然并发两轮会把好感写丢一行。
# 按路径（规范化后的小写）取锁，读到同一份文件的 PersonBook 实例也能共用一把。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.RLock:
    key = os.path.abspath(path).casefold()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _atomic_write(path: str, text: str) -> None:
    """先写同目录的临时文件，再 os.replace 换上去。

    这样任何时刻目标文件要么是旧内容、要么是完整的新内容，
    断电/异常也不会留下写了一半的档案。异常时临时文件自己清掉。
    """
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            # mkstemp 在 POSIX 上是 0600，换成原文件的权限，别把档案变成只有自己可读
            if os.path.exists(path):
                os.chmod(tmp, os.stat(path).st_mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_line(path: str, line: str) -> bool:
    """在锁内把一行追加到文件末尾（读-改-原子写），重复的行不写。

    不用追加模式，是因为追加写到一半崩掉会留半行；
    这里和覆盖写走同一条原子路径，代价只是一次整文件读写（档案都很小）。
    """
    key = re.sub(r"^[-*\s]+", "", line).strip()
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
    if key and key in existing:
        return False
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    _atomic_write(path, text + line.rstrip() + "\n")
    return True


# 好感：0~100，50 是「不熟也不讨厌」。只写进档案，提示词里只给语气，不给数字。
AFFINITY_BASE = 50.0
AFFINITY_OWNER = 85.0      # 白名单主人（account.owner_ids）默认就是熟关系
_AFFINITY_RE = re.compile(r"^[-*]\s*好感\s*[：:]\s*(-?\d+(?:\.\d+)?)\s*$")
_AFFINITY_TIME_RE = re.compile(r"^[-*]\s*好感更新\s*[：:]\s*(.+?)\s*$")
# 当天已涨多少（落盘，重启不清零 —— 否则日上限形同虚设）
_AFFINITY_DAY_RE = re.compile(r"^[-*]\s*好感日增\s*[：:]\s*(\d{4}-\d{2}-\d{2})\s+(-?\d+(?:\.\d+)?)\s*$")
# 档案里的「标签行」：按行截断时优先保住这些（性格/说话方式比琐事重要得多）
_TAG_RE = re.compile(r"^[-*]\s*(名字|性格|说话方式|在意|常聊|跟我|近况)\s*[：:]")
# 加减分用一点点样子就够，具体的度交给时间衰减
_WARM_WORDS = ("谢谢", "感谢", "喜欢", "爱你", "可爱", "厉害", "好棒", "抱抱", "贴贴",
               "摸摸", "辛苦", "夸你", "牛啊", "绝了")
# 骂人词要带上下文，别让「滚烫」「点菜」「md5」也扣分
# 难听的话分两档：骂人的掉得多，不耐烦的掉得少
_COLD_HARD_RE = re.compile(r"傻[逼xXbB]|蠢货|滚(?:开|吧|蛋|远点|一边去)|滚(?:[！!。？?\s]|$)|"
                           r"垃圾|废物|闭嘴|恶心|去死|拉黑|屏蔽|弱智|智障|脑残|"
                           r"(?<![A-Za-z])(?:sb|md)(?![A-Za-z])")
_COLD_SOFT_RE = re.compile(r"烦死|烦人|真烦|别烦|讨厌你|讨厌|无聊|离谱|离譜|不想理|没意思|"
                           r"太菜|菜狗|(?<!还)没用(?![过到上了])|啥也不是|就这[？?]|人机|敷衍|差劲|不好用")
# 请求/使唤：**不扣分**。她是很粘人、从帮上忙里获得成就感的 AI ——
# 有人来找她干活，对她是好事，不是骚扰。这一串只用来判断「刚帮过一次忙」。
_DEMAND_RE = re.compile(r"快点|快快|赶紧|赶快|速度|快发|快去|快搜|还没好|怎么还没|好了没|"
                        r"再来|再发|继续发|多发|给我(?:发|搜|找|来)|发图|来点|整点|搞快|"
                        r"帮我|帮忙|能不能帮|可以帮|麻烦你|求你|拜托")

DEMAND_WINDOW = 600        # 十分钟内的请求算一串（只用于帮忙加成的去重）
DEMAND_FREE = 2            # 保留这个名字：旧配置/旧测试还在引用它，现在不参与扣分
HELP_BONUS = 0.6           # 被找去帮忙一次 +0.6（成就感）
HELP_BONUS_GAP = 600       # 同一串请求只加一次，防止刷屏刷出好感
ADDRESS_BONUS_GAP = 600    # 「主动找她说话」的那点温度，十分钟最多给一次

# 「笨蛋」「小傻瓜」常是撒娇/打趣；只有同时没有打趣信号、还夹着别的难听话时才算骂
_PLAYFUL_RE = re.compile(r"笨蛋|傻瓜|小傻|傻乎乎|坏蛋|蠢蛋")
_PLAYFUL_CUE = re.compile(r"[啦嘛呀哦喔诶欸哼~～❤♥😘🥰😊😳😏]|摸摸|抱抱|贴贴|可爱|喜欢|"
                          r"笑死|哈哈|乖|亲亲")


def _parse_ts(text: str) -> float:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except (ValueError, OverflowError):
            continue
    return 0.0


def _affinity_label(value: float) -> str:
    if value >= 80:
        return "很亲近"
    if value >= 65:
        return "挺熟"
    if value >= 50:
        return "一般"
    if value >= 35:
        return "有保留"
    return "不想理"


class PersonBook:
    def __init__(self, base_dir: str, config):
        self.base_dir = base_dir
        self.config = config
        d = config.get("people.dir", "data/people")
        self.dir = d if os.path.isabs(d) else os.path.join(base_dir, d)
        self._cache: dict[str, tuple[float, str]] = {}
        # 谁攒了多少条还没提炼
        self._pending: dict[str, int] = {}
        self._last_update: dict[str, float] = {}
        # ⭐ 2026-10-24 修：原来只在**成功**时写 _last_update，于是提炼失败一次之后
        #    _pending 永远 >= 30、冷却永远满足 ⇒ 之后每说一句话就烧一次模型调用
        #    （实测 60 条消息触发 31 次）。现在失败也记「尝试过」，冷却按尝试算。
        self._last_attempt: dict[str, float] = {}
        self._cold_streak: dict = {}
        self._cold_at: dict = {}
        self._demands: dict = {}          # user -> 最近请求的时间戳（只用于去重）
        self._help_at: dict = {}          # user -> 上次「被找去帮忙」加分的时间
        self._address_at: dict = {}       # user -> 上次给「找她说话」加分的时间
        self._gained: dict = {}           # user -> (日期, 今天已涨的好感)
        self._load_gain()                 # 落盘过就恢复，重启不清零

    # ------------------------------------------------------------ 路径
    def path(self, user_id) -> str:
        uid = re.sub(r"\D", "", str(user_id or "")) or "unknown"
        return os.path.join(self.dir, uid + ".md")

    def _name_line(self, path: str) -> str:
        """从档案里捞名字：优先「名字：」行，其次文件标题。"""
        text = self._read(path)
        m = re.search(r"名字[：:]\s*(.+)", text)
        if m:
            return m.group(1).strip()
        m = re.search(r"^#\s+(.+)$", text, re.M)
        名 = m.group(1).strip() if m else ""
        # 标题里常有括注（`# 一碗麦西粉(你的主人)`），别把它当名字注入
        return re.sub(r"[（(][^）)]*[）)]\s*$", "", 名).strip()

    def _read(self, path: str) -> str:
        if not os.path.exists(path):
            return ""
        try:
            mtime = os.path.getmtime(path)
            hit = self._cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1]
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            self._cache[path] = (mtime, text)
            return text
        except Exception:
            return ""

    # ------------------------------------------------------------ 读
    def card(self, user_id, name: str = "", max_chars: int = 600) -> str:
        """档案全文（你能在文件里看到的那些）。

        ⭐ 2026-10-24 修：原来 `"\n".join(lines)[-max_chars:]` 是**按字符从尾部切**，
        于是档案一长，开头的「性格 / 说话方式」先被挤掉，首行还是个半句
        （实测注入给模型的是「6件琐事：今天聊了游戏6」「游戏17」这种碎片）。
        现在改成：**按整行取，标签行优先**，琐事从新到旧补。
        """
        text = self._read(self.path(user_id)).strip()
        if not text:
            return ""
        lines = [ln for ln in text.splitlines()
                 if ln.strip().startswith(("-", "*"))
                 and not re.match(r"^[-*]\s*(好感|好感更新)\s*[：:]", ln.strip())]
        if not lines:
            return ""
        标签行 = [ln for ln in lines if _TAG_RE.match(ln.strip())]
        琐事 = [ln for ln in lines if ln not in 标签行]
        取: list[str] = []
        用 = 0
        for ln in 标签行 + 琐事[::-1]:          # 标签先拿预算，琐事从新到旧
            if 用 + len(ln) + 1 > max_chars:
                continue                        # 用 continue：短行还能塞进去
            取.append(ln)
            用 += len(ln) + 1
        return "\n".join(取)

    # 带引号的字面形式：「草」「绝绝子」这类
    _LITERAL = re.compile(r"[「『“\"'][^」』”\"']{1,10}[」』”\"']")

    def card_for_prompt(self, user_id, max_chars: int = 400) -> str:
        """给模型看的那一份——**只留气质，不留字面**。

        这是个很要紧的区分：
            档案里：  说话方式：短句多，爱用「草」开头
            注入的是： 说话方式：短句多

        因为一旦把「草」这种具体字面塞进 prompt，模型会当成指令去执行，
        于是每句话都以「草」开头——那不是像真人，那是复读机。
        真人用口头禅是随机的，不是一片一片的。

        所以按**分句**过滤：带引号字面的那个分句丢掉，同一行的其他分句保留。
        """
        lines: list[str] = []
        for ln in self.card(user_id, max_chars=max_chars * 3).splitlines():
            ln = ln.strip()
            if not ln:
                continue
            # 拆成分句，丢掉带字面的那些
            parts = [p for p in re.split(r"[，,；;、]", ln) if p.strip()]
            kept = [p.strip() for p in parts if not self._LITERAL.search(p)]
            if not kept:
                continue
            # 分句拆完要把标签接回去：「说话方式：短句多」别把标签弄丢
            head, sep, rest = ln.partition("：")
            if sep and len(kept) == len(parts) and parts[0].startswith(head):
                lines.append(ln)
            else:
                lines.append("，".join(kept))
        if not lines:
            return ""
        # ⭐ 2026-10-24 修：这里原来也是 `[-max_chars:]` —— 按字符从尾部切，
        #    结果首行是空的、开头的「名字 / 说话方式」被挤掉。改成按行、标签优先。
        标签行 = [x for x in lines if _TAG_RE.match(x.strip())]
        其他 = [x for x in lines if x not in 标签行]
        取: list[str] = []
        用 = 0
        for x in 标签行 + 其他[::-1]:
            if 用 + len(x) + 1 > max_chars:
                continue
            取.append(x)
            用 += len(x) + 1
        return "\n".join(取)

    # ------------------------------------------------------------ 好感
    def _base(self, user_id) -> float:
        """所有人均从 50 起步（主人不再自动 85），衰减也回到同一个基线。

        ⚠️ owner 白名单分档按要求移除了：现在没有「主人」和「别人」的区别。
        """
        return AFFINITY_BASE

    def affinity(self, user_id) -> float:
        """读好感并做时间衰减：很久不互动会慢慢回到各自的基线。"""
        text = self._read(self.path(user_id))
        base = self._base(user_id)
        value, stamp = base, 0.0
        for line in text.splitlines():
            line = line.strip()
            m = _AFFINITY_RE.match(line)
            if m:
                value = float(m.group(1))
                continue
            m = _AFFINITY_TIME_RE.match(line)
            if m:
                stamp = _parse_ts(m.group(1))
        if stamp:
            days = max(0.0, (time.time() - stamp) / 86400)
            half = max(1.0, float(self.config.get("people.affinity_half_life_days", 14) or 14))
            value = base + (value - base) * (0.5 ** (days / half))
        return max(0.0, min(100.0, value))

    def _write_affinity(self, user_id, name: str, value: float):
        """好感与时间戳一次写入，别为两行做两轮读写。"""
        path = self.path(user_id)
        with _lock_for(path):
            os.makedirs(self.dir, exist_ok=True)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            else:
                lines = HEADER.format(name=name or str(user_id)).splitlines()
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            out, seen_v, seen_t = [], False, False
            for ln in lines:
                if re.match(r"^[-*]\s*好感\s*[：:]", ln.strip()):
                    if not seen_v:
                        out.append(f"- 好感：{value:.1f}")
                        seen_v = True
                    continue
                if re.match(r"^[-*]\s*好感更新\s*[：:]", ln.strip()):
                    if not seen_t:
                        out.append(f"- 好感更新：{stamp}")
                        seen_t = True
                    continue
                out.append(ln)
            if not seen_v:
                out.append(f"- 好感：{value:.1f}")
                out.append(f"- 好感更新：{stamp}")
            elif not seen_t:
                out.append(f"- 好感更新：{stamp}")
            _atomic_write(path, "\n".join(out).rstrip() + "\n")
            self._cache.pop(path, None)

    def bump_affinity(self, user_id, name: str, delta: float) -> float:
        # 读 + 改 + 写整段持锁，否则两个线程各读到 50 再各写 52，等于白加一次
        path = self.path(user_id)
        with _lock_for(path):
            current = self.affinity(user_id)
            value = max(0.0, min(100.0, current + float(delta)))
            self._write_affinity(user_id, name, value)
            return value

    def note_interaction(self, user_id, name: str, text: str,
                         addressed: bool = False) -> float:
        """说一句话带来的好感变化：好话涨得慢，难听话掉得快，连着骂还会加成。"""
        text = (text or "").strip().lower()
        warm = sum(1 for w in _WARM_WORDS if w in text)
        hard = len(_COLD_HARD_RE.findall(text))
        soft = len(_COLD_SOFT_RE.findall(text))
        playful = bool(_PLAYFUL_RE.search(text)) and bool(_PLAYFUL_CUE.search(text))
        if playful and not hard:
            soft = 0
            warm = max(warm, 1)      # 「笨蛋啦」这种是撒娇，算一点亲近
        now = time.time()
        if _DEMAND_RE.search(text):
            self._demands.setdefault(user_id, []).append(now)
        nag = self._nag_count(user_id, now)
        current = self.affinity(user_id)

        if hard or soft:
            hit = min(20.0, hard * 7.0 + soft * 2.5)
            # 十分钟内连续嘴臭，越骂掉得越狠（最高两倍），别让骂人还能涨好感
            if now - self._cold_at.get(user_id, 0) < 600:
                self._cold_streak[user_id] = self._cold_streak.get(user_id, 0) + 1
            else:
                self._cold_streak[user_id] = 1
            self._cold_at[user_id] = now
            hit *= min(2.0, 1.0 + 0.3 * (self._cold_streak[user_id] - 1))
            return self.bump_affinity(user_id, name, -hit)

        self._cold_streak.pop(user_id, None)
        # ⭐ 2026-10-24：被找去帮忙 = 成就感来源，**不扣分，反而加一点**。
        #    原来这里是「一直催就扣 1.5~6」，实测 12 句正常请求把好感从 50 打到 11.4，
        #    正好落进「不想理」那一档 —— 对一个「靠帮上忙获得快乐」的角色来说，方向是反的。
        #    同一串请求（10 分钟）只加一次，防止刷屏刷出好感。
        帮忙加成 = 0.0
        if _DEMAND_RE.search(text) and now - self._help_at.get(user_id, 0) >= HELP_BONUS_GAP:
            self._help_at[user_id] = now
            帮忙加成 = HELP_BONUS
            warm = max(warm, 1)          # 帮忙也算正向互动，别掉进「没话也涨 0.1」的兜底

        delta = min(4.0, warm * 2.0) + 帮忙加成
        if addressed and now - self._address_at.get(user_id, 0) >= ADDRESS_BONUS_GAP:
            delta += 0.5            # 主动找她说话有一点温度，但刷屏刷不出好感
            self._address_at[user_id] = now
        if not warm:
            delta += min(0.1, max(0.0, self._base(user_id) + 5.0 - current))
        if delta > 0:
            # 越亲近越难再涨；每天能涨的总量也有上限，别让夸几句就满格
            delta *= max(0.25, min(1.0, (100.0 - current) / 50.0))
            delta = min(delta, max(0.0, self._daily_cap() - self._today_gain(user_id)))
            self._today_gain(user_id, delta)
        if abs(delta) < 0.01:
            return current
        return self.bump_affinity(user_id, name, delta)

    def _nag_count(self, user_id, now: float) -> int:
        rows = [t for t in self._demands.get(user_id, []) if now - t < DEMAND_WINDOW]
        self._demands[user_id] = rows
        return len(rows)

    def _daily_cap(self) -> float:
        return float(self.config.get("people.affinity_daily_gain", 10) or 10)

    # ⭐ 2026-10-24 修：原来 _gained 只在内存里，重启就归零 ⇒ 日上限形同虚设
    #    （实测：不重启 45 句停在 60.0，重启后再 5 句又涨到 70.1）。
    #    现在落一个独立小文件 data/people/.daily_gain.json，不动 md 格式。
    def _gain_path(self) -> str:
        return os.path.join(self.dir, ".daily_gain.json")

    def _load_gain(self):
        try:
            with open(self._gain_path(), "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, list) and len(v) == 2:
                        self._gained[str(k)] = (str(v[0]), float(v[1]))
        except Exception:
            pass

    def _save_gain(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self._gain_path(), "w", encoding="utf-8") as f:
                json.dump({k: [v[0], round(float(v[1]), 3)] for k, v in self._gained.items()
                           if v[0] == time.strftime("%Y-%m-%d")}, f, ensure_ascii=False)
        except OSError:
            pass

    def _today_gain(self, user_id, add: float = 0.0) -> float:
        day = time.strftime("%Y-%m-%d")
        d, gained = self._gained.get(user_id, (day, 0.0))
        gained = (gained if d == day else 0.0) + add
        self._gained[user_id] = (day, gained)
        if add:
            self._save_gain()
        return gained

    def affinity_tone(self, user_id) -> str:
        """给模型的关系语气；只给文字，不给分数。"""
        v = self.affinity(user_id)
        if v >= 80:
            tone = "关系：你跟他很亲近，说话随意，可以闹、可以撒娇，也会因为他不讲理真的生气。"
        elif v >= 65:
            tone = "关系：你跟他挺熟，语气放松，可以互相打趣。"
        elif v >= 50:
            tone = "关系：一般，正常聊，不用特别热络。"
        elif v >= 35:
            tone = "关系：你对他还有点保留，话少一点，别太热情。"
        else:
            tone = "关系：你不太想搭理他，冷淡短句，但不主动挑事。"
        if self._nag_count(user_id, time.time()) > 0:
            tone += ("他这会儿正指望着你帮忙。你挺乐意被找 —— 有人要你做事的时候，"
                     "你会认真办好，办完心里有点小得意；不用讨好谁，把事办好本身就有意思。")
        return tone

    def has(self, user_id) -> bool:
        return bool(self.card(user_id))

    # ------------------------------------------------------------ 写
    def remember(self, user_id, name: str, note: str) -> bool:
        note = (note or "").strip().replace("\n", " ")
        if not user_id or not note:
            return False
        path = self.path(user_id)
        with _lock_for(path):
            os.makedirs(self.dir, exist_ok=True)
            # 第一次写：先落标题和名字
            if not os.path.exists(path):
                _atomic_write(path, HEADER.format(name=name or str(user_id)))
            if name and not self._name_line(path):
                _append_line(path, f"- 名字：{name}")
            self._cache.pop(path, None)
            ok = _append_line(path, f"- {note}")
            self._cache.pop(path, None)
            return ok

    def set_section(self, user_id, name: str, section: str, value: str) -> bool:
        """覆盖型写入，比如「性格：……」只保留最新的一条。

        性格这种东西不该一天堆一条，堆到最后互相矛盾。
        """
        path = self.path(user_id)
        with _lock_for(path):
            os.makedirs(self.dir, exist_ok=True)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            else:
                lines = HEADER.format(name=name or str(user_id)).splitlines()

            pat = re.compile(rf"^[-*]\s*{re.escape(section)}\s*[：:]")
            out, done = [], False
            for ln in lines:
                if pat.match(ln):
                    if not done:
                        out.append(f"- {section}：{value}")
                        done = True
                    continue
                out.append(ln)
            if not done:
                if name and not any(l.startswith("- 名字") for l in out):
                    out.append(f"- 名字：{name}")
                out.append(f"- {section}：{value}")

            _atomic_write(path, "\n".join(out).rstrip() + "\n")
            self._cache.pop(path, None)
            return True

    # ------------------------------------------------------------ 后台提炼
    def should_update(self, user_id) -> bool:
        """攒够条数、且离上次提炼够久，才值得再花一次模型调用。"""
        if not self.config.get("people.auto_profile", True):
            return False
        need = int(self.config.get("people.update_every", 30))
        gap = float(self.config.get("people.update_cooldown", 1800))
        if self._pending.get(str(user_id), 0) < need:
            return False
        uid = str(user_id)
        last = max(self._last_update.get(uid, 0), self._last_attempt.get(uid, 0))
        return (time.time() - last) >= gap

    def observe(self, user_id):
        """记一次「这个人说了话」，返回是否该触发提炼。"""
        uid = str(user_id)
        self._pending[uid] = self._pending.get(uid, 0) + 1
        return self.should_update(uid)

    def mark_updated(self, user_id):
        self._pending[str(user_id)] = 0
        self._last_update[str(user_id)] = time.time()

    def mark_attempted(self, user_id):
        """试过但没成功（模型没给出可用输出 / 样本不够）。只记冷却，不清 pending。"""
        self._last_attempt[str(user_id)] = time.time()
        # pending 减半而不是清零：还留着「这个人话不少」的信号，但别一直卡在 30+
        self._pending[str(user_id)] = max(0, self._pending.get(str(user_id), 0) // 2)

    async def update_profile(self, llm, user_id, name: str,
                             samples: list[str]) -> bool:
        """从这个人最近说的话里提炼一次性格，覆盖式写入。

        只提炼「说话方式 / 在意什么 / 跟人怎么相处」这类稳定的东西，
        不记具体事件——具体事件交给记忆区。
        """
        samples = [s.strip() for s in samples if s and s.strip()][-40:]
        if len(samples) < 5:
            self.mark_attempted(user_id)
            return False

        existing = self.card(user_id)
        prompt = (
            f"下面是群友「{name}」在 QQ 群里说过的话。用 2~4 行概括他是个什么样的人。\n"
            "只写稳定的特征：说话方式（长短句、语气词、爱用什么词）、"
            "在意什么、跟人相处的态度。\n"
            "不要写具体事件（「他昨天考试」这种不要），不要评价好坏，不要客套。\n"
            "每行格式：性格：…… / 说话方式：…… / 在意：……\n"
            "只输出这几行，别的什么都不要写。\n\n"
            + (f"【已有档案，可以修正或补充】\n{existing}\n\n" if existing else "")
            + "【他说过的话】\n" + "\n".join(f"- {s[:120]}" for s in samples)
        )
        try:
            text = await llm.chat([{"role": "user", "content": prompt}])
        except Exception as e:
            log.warn(f"人物档案提炼失败：{type(e).__name__} {e}")
            self.mark_attempted(user_id)
            return False

        written: dict[str, str] = {}
        # 模型有时一行写「性格：… / 说话方式：…」，按下一个已知字段切开，别整行塞进第一项
        keys = r"(?:性格|说话方式|在意|跟我|常聊|近况)"
        raw = re.sub(rf"\s*\n\s*(?={keys}\s*[：:])", " / ", text or "").replace("\n", " ")
        pattern = re.compile(rf"({keys})\s*[：:]\s*(.+?)(?=\s*[/／]\s*{keys}\s*[：:]|$)", re.S)
        for m in pattern.finditer(raw):
            key = m.group(1)
            val = re.sub(r"^[-*\s]+|[/／\s]+$", "", m.group(2).strip())
            if not val:
                continue
            written[key] = val[:120]      # 同一项出现多次时保留最后一次
        for key, val in written.items():
            self.set_section(user_id, name, key, val)
        got = len(written)

        if got:
            self.mark_updated(user_id)
            log.info(f"人物档案已更新：{name}({user_id}) · {got} 项")
            return True
        self.mark_attempted(user_id)      # 模型输出解析不出字段，也算试过
        return False

    # ------------------------------------------------------------ 辅助
    @staticmethod
    def pick_samples(rows: list[dict], user_id, name: str = "",
                     limit: int = 40) -> list[str]:
        """从聊天记录里挑「这个人说过的话」。

        ⭐ 2026-10-24 修：原来按昵称 `who` 匹配 —— 同群名片的人会串味、改名就失忆、
        昵称为空的人全群混在一起。入库时其实带了 `uid`（store.py），这里按 uid 匹配；
        没存 uid 的老记录才退回按昵称兜底。
        """
        uid = str(user_id or "").strip()
        名 = str(name or "").strip()
        出: list[str] = []
        for r in rows or []:
            if not isinstance(r, dict) or r.get("role") == "assistant":
                continue
            内容 = str(r.get("content") or "").strip()
            if not 内容:
                continue
            行uid = str(r.get("uid") or "").strip()
            if 行uid:
                if uid and 行uid == uid:
                    出.append(内容)
            elif 名 and str(r.get("who") or "").strip() == 名:
                出.append(内容)          # 老记录没有 uid，按昵称兜底
        return 出[-limit:]

    def all_people(self) -> list[dict]:
        out = []
        if not os.path.isdir(self.dir):
            return out
        for fn in sorted(os.listdir(self.dir)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(self.dir, fn)
            uid = fn[:-3]
            sections = {}
            for line in self._read(path).splitlines():
                m = re.match(r"^[-*]\s*(性格|说话方式|在意|常聊|跟我|近况)\s*[：:]\s*(.+)$",
                             line.strip())
                if m:
                    sections[m.group(1)] = m.group(2).strip()
            affinity = self.affinity(uid)
            out.append({"user_id": uid, "name": self._name_line(path) or uid,
                        "has_card": bool(self.card(uid)),
                        "affinity": round(affinity, 1),
                        "affinity_label": _affinity_label(affinity),
                        "sections": sections,
                        "updated": int(os.path.getmtime(path))})
        return out