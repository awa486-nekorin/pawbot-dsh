"""时间感：今天几号、星期几、是不是节假日，以及「这个点她大概在干嘛」。

只做三件事，不碰网络、不碰 data/ 里的现有文件：

  1. now_info()      日期 / 星期 / 时段
  2. schedule_state() 作息：现在大概在干嘛、精神怎么样
  3. holiday()       法定节假日、调休上班日、常见节日

时刻表全部来自配置 ``life.schedule`` / ``life.periods``（见 config.py 的 life 段）。
周末整体往后推 ``life.weekend_shift_hours`` 小时（默认 1.5），也就是起得晚一点。

时间一律用参数注入，方便离线测试：

    life.now_info(datetime(2026, 10, 21, 9, 0))
    life.schedule_state(datetime(2026, 10, 24, 8, 0))   # 周六，会按 +1.5h 算

节假日数据来源与不确定处见文件末尾的 ``_HOLIDAY_SOURCES`` 注释。
"""

from __future__ import annotations

import random
from datetime import date, datetime, time as dtime, timedelta
from time import struct_time

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 时段边界：只写起点，终点由下一条的起点决定。改成配置 life.periods 即可覆盖。
DEFAULT_PERIODS = [
    {"start": "00:00", "name": "凌晨"},
    {"start": "05:00", "name": "早上"},
    {"start": "08:00", "name": "上午"},
    {"start": "11:00", "name": "中午"},
    {"start": "13:00", "name": "下午"},
    {"start": "17:00", "name": "傍晚"},
    {"start": "19:00", "name": "晚上"},
    {"start": "23:00", "name": "深夜"},
]

# 默认作息表。config.py 的 life.schedule 是同一份；这里留一份兜底，
# 方便 life.py 单独被测试/引用时也能工作。
DEFAULT_SCHEDULE = [
    {"start": "02:00", "end": "07:00", "key": "sleep", "label": "睡着",
     "factor": 1000.0, "quiet": True},
    {"start": "07:00", "end": "08:30", "key": "wake", "label": "刚醒迷糊",
     "factor": 1.3, "quiet": False},
    {"start": "08:30", "end": "11:30", "key": "morning", "label": "上午精神",
     "factor": 0.7, "quiet": False},
    {"start": "11:30", "end": "13:30", "key": "noon", "label": "午饭摸鱼",
     "factor": 0.9, "quiet": False},
    {"start": "13:30", "end": "14:00", "key": "doze", "label": "饭后发呆",
     "factor": 1.2, "quiet": False},
    {"start": "14:00", "end": "16:00", "key": "sleepy", "label": "犯睏",
     "factor": 1.5, "quiet": False},
    {"start": "16:00", "end": "18:00", "key": "afternoon", "label": "下午缓过来",
     "factor": 0.9, "quiet": False},
    {"start": "18:00", "end": "19:30", "key": "evening", "label": "傍晚",
     "factor": 1.0, "quiet": False},
    {"start": "19:30", "end": "23:30", "key": "night", "label": "晚上最精神",
     "factor": 0.7, "quiet": False},
    {"start": "23:30", "end": "02:00", "key": "latenight", "label": "熬夜",
     "factor": 1.8, "quiet": False},
]

# 每个状态至少 3 种说法；按「日期 + 状态」做确定性随机，
# 所以同一天同一时段的描述固定，不会每条消息都换一句。
_DESCRIPTIONS = {
    "sleep": {
        "activity": ["缩在被窝里睡得很沉", "抱着枕头睡成一团",
                     "睡梦中翻了个身", "整只瘫在床上不动"],
        "spirit": ["困得睁不开眼，被叫醒也迷迷糊糊",
                   "脑子还没开机，只想继续睡",
                   "反应很慢，一句话要缓半天",
                   "又困又懵，语气软绵绵的"],
    },
    "wake": {
        "activity": ["刚醒，赖在床上刷手机", "迷迷糊糊地爬起来找水喝",
                     "眼睛还没完全睁开，正发呆", "裹着被子不想起，先摸手机"],
        "spirit": ["脑子还在缓冲，说话慢半拍", "有点起床气，但还没真的生气",
                   "半醒不醒，语气比平时软", "刚开机，耐心不多"],
    },
    "morning": {
        "activity": ["在桌前一边吃早饭一边看群", "趁着精神好处理手头的事",
                     "上午效率还行，正忙自己的", "端着杯子在电脑前坐下"],
        "spirit": ["挺清醒的，愿意接话", "精神不错，思路清楚",
                   "心情平稳，能聊", "脑子转得动，但不亢奋"],
    },
    "noon": {
        "activity": ["在吃午饭，边吃边刷群", "午休摸鱼中，饭还没吃完",
                     "刚吃完饭，瘫着不想动", "在纠结下午吃什么"],
        "spirit": ["放松，懒懒的", "饭后有点满足",
                   "心情不错，但不想费脑子", "吃饱了，有点想睡"],
    },
    "doze": {
        "activity": ["饭后发呆，什么都不想干", "趴在桌上放空",
                     "刚放下碗，在犯懒", "盯着屏幕出神"],
        "spirit": ["懒得动脑", "有点昏沉", "懒得说话", "脑子转得慢"],
    },
    "sleepy": {
        "activity": ["在桌前犯睏，靠咖啡续命", "下午最困的时候，一直在打哈欠",
                     "对着屏幕走神", "趴着眯了一会儿"],
        "spirit": ["困，反应慢半拍", "精神不济，只想眯一会",
                   "脑子有点转不动", "眼皮打架，耐心也少"],
    },
    "afternoon": {
        "activity": ["下午缓过来了，在收尾手头的事", "在等下班/放学，顺手看看群",
                     "刚喝了点东西，精神回来一些", "把没做完的事慢慢做完"],
        "spirit": ["还算平静", "不急不躁", "正常状态，能好好说话", "有点盼着下班"],
    },
    "evening": {
        "activity": ["在吃晚饭", "吃完饭在外面走走",
                     "傍晚有点放空，随便看看", "刚到家，正在缓一口气"],
        "spirit": ["松弛下来", "一天下来有点累，但还行",
                   "舒服，想随便聊聊", "懒得动，但心情不差"],
    },
    "night": {
        "activity": ["晚上最精神，在电脑前瞎逛", "窝着看视频/打游戏",
                     "一边听歌一边看群", "正折腾自己感兴趣的东西"],
        "spirit": ["精神很好，话也多", "兴致不错",
                   "脑子转得快", "比白天活跃"],
    },
    "latenight": {
        "activity": ["又在熬夜，嘴上说明天一定早睡", "深夜还捧着手机不睡",
                     "半夜在刷东西，越刷越精神", "明明白天很累，这个点反而清醒"],
        "spirit": ["困但舍不得睡", "有点亢奋，也有点迷糊",
                   "已经很累了，还在硬撑", "脑子发飘，说话不太过脑"],
    },
    "awake": {
        "activity": ["醒着，随便看看群", "在做自己的事", "手头闲着"],
        "spirit": ["正常状态", "还行", "不困也不亢奋"],
    },
}


# ------------------------------------------------------------------ 节假日
def _days(year: int, month: int, day: int, count: int) -> list[str]:
    start = date(year, month, day)
    return [(start + timedelta(days=i)).isoformat() for i in range(count)]


# 2026 年：国务院办公厅《关于 2026 年部分节假日安排的通知》
# （国办发明电〔2025〕7 号，2025-11-04 发布）。
_STATUTORY: dict[str, str] = {}
for _name, _span in (
    ("元旦", _days(2026, 1, 1, 3)),
    ("春节", _days(2026, 2, 15, 9)),      # 2/15（腊月二十八）至 2/23（正月初七）
    ("清明节", _days(2026, 4, 4, 3)),
    ("劳动节", _days(2026, 5, 1, 5)),
    ("端午节", _days(2026, 6, 19, 3)),
    ("中秋节", _days(2026, 9, 25, 3)),
    ("国庆节", _days(2026, 10, 1, 7)),
    # 2027 年：官方通知通常在 2026 年 11 月才发布，下面是按
    # 2024-11-12《关于修改〈全国年节及纪念日放假办法〉的决定》（2025-01-01 施行）
    # 和当时网上最常见的推演排的，**不是官方最终版**，发布后请替换。
    ("元旦", _days(2027, 1, 1, 3)),
    ("春节", _days(2027, 2, 5, 9)),       # 除夕 2/5（周五），连休至 2/13
    ("清明节", _days(2027, 4, 3, 3)),
    ("劳动节", _days(2027, 5, 1, 5)),
    ("端午节", _days(2027, 6, 9, 1)),     # 周三，按规则只放当天
    ("中秋节", _days(2027, 9, 15, 1)),    # 周三，按规则只放当天
    ("国庆节", _days(2027, 10, 1, 7)),
):
    for _d in _span:
        _STATUTORY[_d] = _name

# 调休上班日（周末但要上班）。同样：2026 官方，2027 推演。
_MAKEUP = {
    "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09",
    "2026-09-20", "2026-10-10",
    "2027-01-31", "2027-02-14", "2027-05-08",
    "2027-09-26", "2027-10-09",
}

# 固定公历的常见日子（每年都用同一张表）
_FIXED = {
    "02-14": "情人节",
    "04-01": "愚人节",
    "05-20": "520",
    "09-10": "教师节",
    "10-31": "万圣节",
    "11-11": "双十一",
    "12-24": "平安夜",
    "12-25": "圣诞节",
}

# 农历/节气里没法直接算的日子，2026、2027 两年手工查表写死。
# 来源：公开日历/黄历换算（2026 七夕 8/19、2027 七夕 8/8；2026 中秋 9/25、
# 2027 中秋 9/15；元宵、重阳同源）。这两年的中秋已在法定表里，不会走到这里。
_LUNAR = {
    "2026-03-03": "元宵节",
    "2026-08-19": "七夕",
    "2026-10-18": "重阳节",
    "2027-02-20": "元宵节",
    "2027-08-08": "七夕",
    "2027-10-08": "重阳节",
}

# 数据来源与不确定处：
#   · 2026：国务院办公厅《关于 2026 年部分节假日安排的通知》（国办发明电〔2025〕7号）
#   · 2027：截至 2026-10 官方尚未发布，按 2025 年起施行的放假办法 + 主流推演排：
#       春节 2/5–2/13（9 天，1/31、2/14 补班）、清明 4/3–4/5、
#       劳动节 5/1–5/5（5/8 补班）、端午 6/9、中秋 9/15、国庆 10/1–10/7
#       （9/26、10/9 补班）。官方通知发布后需要回来核对。
#   · 非法定节日只用于「有点气氛」，不影响作息判断。
_HOLIDAY_SOURCES = "2026: 国办发明电〔2025〕7号；2027: 按放假办法推演，非官方"


def _cfg(config, key: str, default):
    """同时支持 Config 对象和普通 dict。"""
    if config is None:
        return default
    try:
        val = config.get(f"life.{key}", default)
    except Exception:
        return default
    return default if val is None else val


def _as_dt(t=None) -> datetime:
    if t is None:
        return datetime.now()
    if isinstance(t, datetime):
        return t
    if isinstance(t, date):
        return datetime.combine(t, dtime(0, 0))
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t)
    if isinstance(t, struct_time):
        return datetime(*t[:6])
    if isinstance(t, (tuple, list)) and len(t) >= 3:
        return datetime(*t[:6])
    raise TypeError(f"不认识的时间类型：{type(t).__name__}")


def _parse_hm(text: str, default: int = 0) -> int:
    try:
        hh, mm = str(text).split(":", 1)
        return max(0, min(24 * 60, int(hh) * 60 + int(mm)))
    except Exception:
        return default


def _fmt_hm(minutes: int) -> str:
    minutes = max(0, min(24 * 60, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _period_list(config) -> list[tuple[int, str]]:
    raw = _cfg(config, "periods", DEFAULT_PERIODS)
    out: list[tuple[int, str]] = []
    try:
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                out.append((_parse_hm(item.get("start"), 0), name))
    except Exception:
        out = []
    if not out:
        out = [(_parse_hm(x["start"]), x["name"]) for x in DEFAULT_PERIODS]
    out.sort()
    return out


def _period(dt: datetime, config) -> tuple[str, str, str]:
    """返回 (时段名, 起点, 终点)。"""
    minutes = dt.hour * 60 + dt.minute
    slots = _period_list(config)
    for i, (start, name) in enumerate(slots):
        end = slots[i + 1][0] if i + 1 < len(slots) else 24 * 60
        if end <= start:            # 最后一段跨零点（自定义配置时才会出现）
            end = 24 * 60
        if start <= minutes < end:
            return name, _fmt_hm(start), _fmt_hm(end)
    # 自定义时段没覆盖到的空档：算当天第一段
    start, name = slots[0]
    end = slots[1][0] if len(slots) > 1 else 24 * 60
    return name, _fmt_hm(start), _fmt_hm(end)


def now_info(t=None, config=None) -> dict:
    """日期 / 星期 / 周末 / 时段。"""
    dt = _as_dt(t)
    weekday_index = dt.weekday()
    period, p_start, p_end = _period(dt, config)
    return {
        "dt": dt,
        "date": dt.strftime("%Y-%m-%d"),
        "year": dt.year,
        "month": dt.month,
        "day": dt.day,
        "hour": dt.hour,
        "minute": dt.minute,
        "weekday": WEEKDAYS[weekday_index],
        "weekday_index": weekday_index,
        "is_weekend": weekday_index >= 5,
        "period": period,
        "period_start": p_start,
        "period_end": p_end,
        "holiday": holiday(dt, config),
    }


def holiday(t=None, config=None) -> str | None:
    """返回节日名 / 「调休上班」/ None。

    优先级：法定假期 > 调休上班 > 固定公历小节日 > 农历节日。
    （比如 2026-02-14 既是情人节又是春节调休上班，返回“调休上班”。）
    """
    d = _as_dt(t).date().isoformat()
    if d in _STATUTORY:
        return _STATUTORY[d]
    if d in _MAKEUP:
        return "调休上班"
    md = d[5:]
    if md in _FIXED:
        return _FIXED[md]
    return _LUNAR.get(d)


# ------------------------------------------------------------------ 作息
def _schedule_blocks(config) -> list[dict]:
    raw = _cfg(config, "schedule", DEFAULT_SCHEDULE)
    blocks: list[dict] = []
    try:
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "awake")
            blocks.append({
                "start": _parse_hm(item.get("start"), 0),
                "end": _parse_hm(item.get("end"), 0),
                "key": key,
                "label": str(item.get("label") or key),
                "factor": float(item.get("factor", 1.0)),
                "quiet": bool(item.get("quiet", False)),
            })
    except Exception:
        blocks = []
    if not blocks:
        blocks = [{
            "start": _parse_hm(x["start"]), "end": _parse_hm(x["end"]),
            "key": x["key"], "label": x["label"],
            "factor": float(x["factor"]), "quiet": bool(x["quiet"]),
        } for x in DEFAULT_SCHEDULE]
    return blocks


def _locate(blocks: list[dict], minutes: int) -> dict:
    for b in blocks:
        start, end = b["start"], b["end"]
        if start == end:
            return b
        if start < end and start <= minutes < end:
            return b
        if start > end and (minutes >= start or minutes < end):
            return b
    return {"start": 0, "end": 24 * 60, "key": "awake",
            "label": "醒着", "factor": 1.0, "quiet": False}


def _pick(key: str, day: str, field: str) -> str:
    """同一天同一状态固定用同一句，避免每条消息都换描述。"""
    pool = (_DESCRIPTIONS.get(key) or _DESCRIPTIONS["awake"]).get(field) or ["…"]
    return pool[random.Random(f"{day}|{key}|{field}").randrange(len(pool))]


def schedule_state(t=None, config=None) -> dict:
    """她现在大概在干嘛。

    周末把作息整体往后推 ``life.weekend_shift_hours`` 小时（默认 1.5），
    也就是周六日更晚起床、更晚睡。
    """
    dt = _as_dt(t)
    info = now_info(dt, config)
    shift = 0.0
    # 调休上班的周六不算真周末：那天还是得按工作日的作息起。
    if info["is_weekend"] and info["holiday"] != "调休上班":
        try:
            shift = max(0.0, float(_cfg(config, "weekend_shift_hours", 1.5)))
        except Exception:
            shift = 1.5
    effective = dt - timedelta(hours=shift)
    blocks = _schedule_blocks(config)
    block = _locate(blocks, effective.hour * 60 + effective.minute)
    day = dt.strftime("%Y-%m-%d")
    return {
        "key": block["key"],
        "label": block["label"],
        "activity": _pick(block["key"], day, "activity"),
        "spirit": _pick(block["key"], day, "spirit"),
        "factor": float(block["factor"]),
        "quiet": bool(block["quiet"]),
        "weekend": info["is_weekend"],
        "shift_hours": shift,
        "period": info["period"],
        "weekday": info["weekday"],
        "date": info["date"],
        "start": _fmt_hm(block["start"]),
        "end": _fmt_hm(block["end"]),
    }


def quiet_now(t=None, config=None) -> bool:
    """睡觉时段返回 True。proactive 接这个当「别吵醒她」的闸门。"""
    return bool(schedule_state(t, config)["quiet"])


def proactive_factor(t=None, config=None) -> float:
    """主动发言的间隔倍数：越小越积极（早上 0.7），越大越懒得开口。

    睡觉时段返回一个很大的数（1000），调用方直接当成「现在别主动说」。
    """
    return float(schedule_state(t, config)["factor"])


# ------------------------------------------------------------------ 提示词
def life_hint(group_id=None, group_info=None, t=None, config=None) -> str:
    """给模型看的一段生活提示。不报精确时刻，只给日期 / 星期 / 时段 / 状态。"""
    if not bool(_cfg(config, "enabled", True)):
        return ""
    info = now_info(t, config)
    state = schedule_state(t, config)
    hol = info["holiday"]

    day = f"今天是 {info['date']}（{info['weekday']}）"
    if hol == "调休上班":
        day += "，是调休上班日"
    elif hol:
        day += f"，{hol}"
    elif info["is_weekend"]:
        day += "，周末"
    bits = [
        day + "。",
        f"现在是{info['period']}。",
        f"你现在大概在：{state['activity']}，精神：{state['spirit']}。",
    ]
    if state["quiet"]:
        bits.append("这个点你已经睡着了：只有被直接叫到（@/点名）才会被吵醒，"
                    "回一句就接着睡，不要主动接群里的闲话。")
    if hol and hol != "调休上班":
        bits.append(f"今天是{hol}，可以自然提一句，但别像客服群发祝福。")
    bits.append("这些是你自己的生活节奏，自然带出来就好，不要每句都提，不要报时。")

    if group_info:
        try:
            from . import groupinfo
            desc = groupinfo.describe(group_info)
            if desc:
                bits.append(desc)
        except Exception:
            pass
    return "\n".join(bits)
