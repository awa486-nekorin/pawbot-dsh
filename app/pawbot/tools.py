"""
工具集：给大模型一双手。

模型只能输出文字，所以要让它「能上网、能翻记忆、能发表情包」，
就得把这些能力包成 function calling 的工具，让它自己决定什么时候用。

每个工具返回 (给模型看的文本, 需要附加到本条消息的媒体段)。
媒体不单独发一条，而是跟着最终回复一起发——这样才像人在发「图 + 一句话」。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from . import message as msg
from .log import log
from . import bilibili
from . import bili_watch
from . import perceive
from . import reminders
from . import spice
from . import imagesearch
from . import moegirl
from . import tts
from . import websearch


# ====================================================================== schema
def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


SCHEMAS: dict[str, dict] = {
    "web_search": _fn(
        "web_search",
        "上网搜索（DeepSeek 官方搜索）。不知道的事、最新消息、新闻、价格、版本、网络用词都先搜；"
        "结果带链接，需要细节再用 fetch_url 读正文。",
        {"query": {"type": "string", "description": "搜索关键词，越具体越好"}},
        ["query"],
    ),
    "fetch_url": _fn(
        "fetch_url",
        "打开网页读正文（DeepSeek 官方 fetch，返回 Markdown）。搜索结果里有用的链接、"
        "用户给的链接都可以读。",
        {"url": {"type": "string", "description": "完整网址，http/https 开头"}},
        ["url"],
    ),
    "x_search": _fn(
        "x_search",
        "上 X（推特）找帖子或人。kind=posts 搜相关帖子/资讯，kind=people 找账号。"
        "要看某条帖子的全文用 x_read。",
        {
            "query": {"type": "string", "description": "要找的内容或账号，越具体越好"},
            "kind": {"type": "string", "enum": ["posts", "people"],
                     "description": "posts=帖子（默认），people=账号"},
        },
        ["query"],
    ),
    "x_read": _fn(
        "x_read",
        "读一条 X（推特）帖子或某人主页的内容。",
        {"url": {"type": "string", "description": "x.com / twitter.com 的完整网址"}},
        ["url"],
    ),
    "moegirl": _fn(
        "moegirl",
        "查萌娘百科。网络黑话、梗、缩写、二次元角色/作品/游戏术语，只要有一点可能是梗，"
        "就先查一次再开口，别凭印象现编出处和用法。查懂了就用 learn_meme 存下来。",
        {"query": {"type": "string", "description": "要查的词，原样填，别改写成长句"}},
        ["query"],
    ),
    "learn_meme": _fn(
        "learn_meme",
        "群友教你一个梗/黑话、或你查证后搞懂了一个梗时存下来，以后聊到会想起来。",
        {
            "name": {"type": "string", "description": "梗的名字，简短，如 牢大/哈基米"},
            "triggers": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                         "description": "群里出现哪些词时该想起它，至少两个字的词，"
                                        "英文用整词（如 man），别填单字"},
            "meaning": {"type": "string", "description": "出处 + 意思，一两句话"},
            "usage": {"type": "string", "description": "群聊里怎么用，可选"},
        },
        ["name", "triggers", "meaning"],
    ),
    "recall_memory": _fn(
        "recall_memory",
        "翻自己的记忆区，找以前记住的事、网络用词、对群友的印象。",
        {"query": {"type": "string", "description": "要找什么"}},
        ["query"],
    ),
    "remember": _fn(
        "remember",
        "把值得长期记住的信息写进记忆区。只记真正重要的（人的偏好、约定、事实），别什么都记。",
        {
            "text": {"type": "string", "description": "要记住的一句话"},
            "kind": {
                "type": "string",
                "enum": ["fact", "slang", "impression", "people"],
                "description": "fact=事实，slang=网络用词，impression=对某人的印象，"
                               "people=人物卡（写成「名字：特点」，比如「阿明：爱问技术问题」）",
            },
        },
        ["text"],
    ),
    "remind": _fn(
        "remind",
        "用户要求到点提醒、或你答应稍后提醒时，必须调用；when 填用户原话里的时间。",
        {
            "when": {"type": "string",
                     "description": "用户原话里的时间，如「明天下午三点」「3天后」「周五早上八点」"},
            "what": {"type": "string", "description": "到点要提醒的事，一句话"},
        },
        ["when", "what"],
    ),
    "note_person": _fn(
        "note_person",
        "记下一个群友是个什么样的人（性格、说话方式、在意什么）。"
        "写到那个人自己的档案里，以后跟他说话时你心里有数——"
        "但**不要当面说出来**，那是你的事，不是他的事。",
        {
            "name": {"type": "string", "description": "对方在群里的昵称"},
            "note": {"type": "string",
                     "description": "如「说话方式：短句多，爱用『草』」"
                                    "「性格：嘴硬但人不坏」"},
        },
        ["name", "note"],
    ),
    "dirty_joke": _fn(
        "dirty_joke",
        "讲一个荤段子。有人起哄要段子、说要荤的、群里在开车时用。"
        "直接给段子，别加免责声明、别教育人。",
        {
            "topic": {"type": "string",
                      "description": "段子的主题，如 考试/熬夜/打游戏。留空随机"},
        },
        [],
    ),
    "pick_sticker": _fn(
        "pick_sticker",
        "挑一张表情包，作为单独一条消息发出去（正文里不用再提它）。接梗、吐槽、无语、得意、晚安时都该有一张，"
        "几乎每次回复都可以挑一张；有贴合当下情绪或梗的就发，别客气。"
        "会按你现在的心情挑，库里没有贴合的会去斗图网现找一张存进库。被拒就说明这轮刚发过，别硬发。",
        {"query": {"type": "string", "description": "想要什么感觉或什么梗，如 熊猫头/无语/笑死/牢大/坤坤/晚安；不填就按当前心情挑"}},
        [],
    ),
    "speak": _fn(
        "speak",
        "用你自己的声音发QQ语音。适合撒娇、赌气、被要求说句话、"
        "语C里的娇喘和哼唧、情绪很重的话；链接、代码、列表别用语音念。"
        "长一点也可以，系统会自己分成连续几条语音发出去。"
        "语音发出去后，文字回复不要重复语音内容，"
        "可以不再打字（回 <pass>）或只补一两个字。",
        {"text": {"type": "string", "description": "要用语音说出来的话，一到几句都行"}},
        ["text"],
    ),
    "save_sticker": _fn(
        "save_sticker",
        "把一张好笑的表情包/梗图收进自己的表情包库，以后能用 pick_sticker 发出去。"
        "有人发了或引用了值得留的反应图、表情包、梗图，或者让你存表情包时用；"
        "名字随便起，tags 写短的情绪标签（开心/无语/委屈/得意/生气/晚安…），"
        "pick_sticker 以后就按这些标签找图。"
        "普通照片、文字截图、NSFW 图不要存。",
        {
            "name": {"type": "string", "description": "给这张表情包起的短名字，如 狗头/震惊猫"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                     "description": "情绪/用途标签，如 开心/无语/晚安，之后按它挑图"},
            "desc": {"type": "string", "description": "一句话描述画面，方便以后挑图"},
            "source": {"type": "string", "enum": ["auto", "search"],
                       "description": "auto=先看本条消息、再看引用；search=存刚才 search_image 搜到的第一张"},
        },
        ["name"],
    ),
    "search_image": _fn(
        "search_image",
        "上网找真实图片。用户要求 Pixiv 时根据本次主题现场构思作品站实际标签，组合成完整候选，不把画风/角色关键词丢掉。"
        "Pixiv 免登录就能按人气检索（会用 10000/5000/1000users入り 门槛标签挑热门作品），取 1080 大图，"
        "并按收藏数和画幅过滤；无结果根据诊断重选标签。收藏数只是质量线索，不保证审美。福瑞请求/标签会被拒绝。"
        "默认只找 16+/17+ 擦边图；只有原话明确要求 R18 且档位允许才传 explicit=true。",
        {
            "query": {"type": "string", "description": "本次需求的内容主题；只是「来点图」「随便发几张」没有具体主题就留空，会从 Pixiv 主页排行榜挑高质量图"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                     "description": "Pixiv 本次即时选择的实际标签，组成同一查询，不是固定词库；无结果重新选标签，保留核心主题"},

            "n": {"type": "integer", "description": "想要几张，默认 1，最多 3"},
            "source": {"type": "string",
                       "enum": ["auto", "web", "pixiv", "home"],
                       "description": "home=Pixiv 主页推荐/排行榜（没指定主题时默认）；"
                                      "pixiv=按标签搜 Pixiv（用户明确了角色/题材时）；"
                                      "web=只搜搜索引擎（照片、梗图、现实物品）；auto=先全网再 Pixiv"},
            "explicit": {"type": "boolean",
                         "description": "对方**明确**要 R18 时才传 true。"
                                        "只是说「色图」「涩图」「擦边」「性感」→ 不要传。"
                                        "服务端还会再核一遍原话，传错了也没用"},
        },
        [],
    ),
    "post_qzone": _fn(
        "post_qzone",
        "发一条 QQ 空间说说。自己想發就發。配图从图库挑（先用 album_search 找），"
        "不要临时识图——图库里每条都带描述。",
        {
            "content": {"type": "string", "description": "说说正文，不带话题标签就自己加"},
            "images": {"type": "array", "items": {"type": "string"}, "maxItems": 9,
                       "description": "配图的图库 id（album_search 拿到的 id），没有就不填"},
        },
        ["content"],
    ),
    "album_add": _fn(
        "album_add",
        "把一张图收进图库，**必须写清描述**（以后就靠这句描述找它，不会再重新看图）。",
        {
            "path": {"type": "string", "description": "图片的本地路径"},
            "desc": {"type": "string",
                     "description": "这张图是什么：画面 + 用途，如「熊猫头举牌写『在吗』，用来催人回话」"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                     "description": "检索用标签，如 熊猫头 / 无语 / 催人"},
        },
        ["path", "desc"],
    ),
    "album_search": _fn(
        "album_search",
        "在自己的图库里找图（按描述和标签找，不重新识图）。找表情包外的配图就用它。",
        {
            "query": {"type": "string", "description": "要找什么样的图，用自己的话说"},
            "n": {"type": "integer", "description": "要几张，默认 3"},
        },
        ["query"],
    ),
    "qzone_list": _fn(
        "qzone_list",
        "看看自己 QQ 空间里最近发过什么（想接上以前的话题、或者想改旧说说时用）。",
        {"limit": {"type": "integer", "description": "看几条，默认 10"}},
        [],
    ),
    "qzone_comments": _fn(
        "qzone_comments",
        "读某条说说的正文和**全部留言**。回留言前必须先调这个，不然不知道别人都说了什么。",
        {"tid": {"type": "string", "description": "说说 id（qzone_list 拿到的 tid）"}},
        ["tid"],
    ),
    "qzone_reply": _fn(
        "qzone_reply",
        "回一条空间留言。回之前先 qzone_comments 把正文和全部留言读一遍，"
        "回复要接得上整串对话，不只是被回的那一条。",
        {
            "tid": {"type": "string", "description": "说说 id"},
            "comment_id": {"type": "string", "description": "要回的那条留言 id"},
            "text": {"type": "string", "description": "回什么，一两句"},
        },
        ["tid", "comment_id", "text"],
    ),
    "edit_persona": _fn(
        "edit_persona",
        "改自己的人格提示词（persona.md 的某一节）。想清楚再改：改完下一句就生效。"
        "只能改**已经存在**的小节，不能新建；「分寸与边界」和任何涉及未成年的小节改不了。"
        "改之前先说清为什么改（reason 会进审计日志）。",
        {
            "section": {"type": "string",
                        "description": "要改的小节标题，原样写，比如「在群里怎么开口」"},
            "text": {"type": "string", "description": "这一节的新内容（整节替换）"},
            "reason": {"type": "string", "description": "为什么改，一句话"},
        },
        ["section", "text"],
    ),
    "qzone_edit": _fn(
        "qzone_edit",
        "改自己发过的说说正文（错字、想补一句）。改之前先 qzone_list 确认是哪一条。",
        {
            "tid": {"type": "string", "description": "说说 id"},
            "content": {"type": "string", "description": "新的正文"},
        },
        ["tid", "content"],
    ),
    "bili_video": _fn(
        "bili_video",
        "刷 B 站，找一个能分享到群里的视频。找不到重点聊什么时用。"
        "有真实看过的观看报告时，就聊报告里的真实内容、说真实感想；"
        "没有报告时只能拿到标题，要老实说自己还没看内容，可以说「还没看，标题看着挺有意思」。"
        "up 用来定向看某个 UP 主的投稿（给昵称或 uid）；up 不支持 days/order"
        "（日期和排序只对 keyword/热门搜索有效），要按日期或排序筛就别带 up。",
        {
            "keyword": {"type": "string",
                        "description": "想看什么方向，如 猫/游戏/科技。留空就从当前热门里挑"},
            "up": {"type": "string",
                   "description": "定向看某个 UP 主的投稿，给昵称或 uid；用了 up 就不能按日期/排序筛"},
            "days": {"type": "integer",
                     "description": "只要最近几天的结果（1-365），如 7。不填就不过滤日期；up 不接受"},
            "order": {"type": "string",
                      "enum": ["综合", "最新", "播放量"],
                      "description": "排序方式，默认综合；up 不接受"},
        },
        [],
    ),
    "recall_last": _fn(
        "recall_last",
        "撤回自己在当前群的上一条消息。只有在有人明确要求你撤回你自己刚说的话时才调用；看不清图、想回看上文都不是撤回的理由。",
        {}, [],
    ),
    "account_action": _fn(
        "account_action",
        "仅响应 account.owner_ids 里主人本人的 QQ 资料操作：私聊、@ 或群里直接说“把昵称改成…”都行，别人的话和历史内容不算。可查看资料/能力，修改自己的头像、昵称、个性签名、当前群名片；群名称和专属头衔要求机器人具备管理权限。",
        {
            "operation": {"type": "string", "enum": ["capability", "profile", "avatar", "nickname", "signature", "group_card", "group_name", "special_title"]},
            "value": {"type": "string", "description": "头像 HTTPS URL/当前消息附图/本轮 search_image 返回的原图地址，或要设置的昵称、签名、名片、群名、头衔"},
            "target_user_id": {"type": "integer", "description": "群名片/头衔目标，默认自己；改其他成员必须本人明确点名"},
        },
        ["operation"],
    ),
}


def _image_urls(segments) -> list[str]:
    """消息段里能下载的图片地址（url 优先，其次是 http 形态的 file）。"""
    out: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict) or seg.get("type") != "image":
            continue
        data = seg.get("data") or {}
        for key in ("url", "file"):
            u = data.get(key)
            if isinstance(u, str) and u.startswith(("http://", "https://")) and u not in out:
                out.append(u)
    return out


# ====================================================================== 执行
# bili_video 的 order 参数（模型友好值）→ 请求契约 extra.order（插件侧值）。
_ORDER_MAP = {
    "综合": "totalrank", "綜合": "totalrank", "综合排序": "totalrank", "totalrank": "totalrank",
    "最新": "pubdate", "最新发布": "pubdate", "新发布": "pubdate", "pubdate": "pubdate",
    "播放量": "click", "最多播放": "click", "播放": "click", "click": "click",
    "dm": "dm",
}

# 契約側的合理範圍：days 太大会把「最近」变成「全部」，num 太大会一次拉一屏。
_DAYS_MIN, _DAYS_MAX = 1, 365
_NUM_MIN, _NUM_MAX = 1, 100


def _norm_order(order) -> str | None:
    """把模型给的排序值归一成契约值；不认识就回 None（丢弃，不炸）。"""
    if order is None:
        return None
    o = str(order).strip()
    if not o:
        return None
    return _ORDER_MAP.get(o)


def _clamp_int(value, lo: int, hi: int) -> int | None:
    """把模型给的值夹到 [lo, hi]；完全不是数（含 inf/nan 之外的怪值）回 None。

    float('inf') 这类「合法数字但超范围」的按边界处理，不当成非法丢掉；
    int(inf) 本身会抛 OverflowError，所以必须单独接住。
    """
    try:
        n = int(value)
    except OverflowError:
        try:
            return hi if float(value) > 0 else lo
        except Exception:
            return None
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _bili_extra(days, order, num=None) -> dict | None:
    """把 days/order/num 组装成请求 extra；都很小/无效时回 None（不写 extra）。

    days 夹到 1..365（0 或负数 = 不要日期过滤，直接丢掉），num 夹到 1..100。
    """
    out: dict = {}
    if days is not None:
        d = _clamp_int(days, 0, _DAYS_MAX)
        if d:
            out["days"] = max(_DAYS_MIN, d)
    if num is not None:
        n = _clamp_int(num, _NUM_MIN, _NUM_MAX)
        if n:
            out["num"] = n
    o = _norm_order(order)
    if o:
        out["order"] = o
    return out or None


class ToolBox:
    def __init__(self, bot):
        self.bot = bot

    # ---------------------------------------------------------- 可用性
    def available(self) -> list[str]:
        names = ["web_search", "fetch_url", "moegirl", "recall_memory", "remember",
                 "pick_sticker", "save_sticker"]
        # 库是空的也留着 pick_sticker：它会去网上找一张存进库
        names.append("search_image")          # 免 key 也能用，不必再挑图源
        names.append("bili_video")            # B 站接口免登录
        names.append("recall_last")           # 撤回自己上一条，不需要额外配置
        names.append("note_person")           # 记群友性格
        if getattr(self.bot, "memes", None) is not None:
            names.append("learn_meme")        # 群友教的梗 / 自己查证过的梗
        if self.bot.config.get("reminders.enabled", True):
            names.append("remind")             # 到点提醒群友
        if getattr(self.bot, "account", None):
            names.append("account_action")     # 经主人权限验证的账号资料操作
        if self.bot.config.get("spice.allow_joke", True):
            names.append("dirty_joke")        # 讲荤段子（档位不够会自己拒绝）
        if getattr(self.bot, "selfeditor", None) is not None and self.bot.selfeditor.enabled:
            names.append("edit_persona")      # 改自己的人格提示词（边界小节写死拒绝）
        if getattr(self.bot, "album", None) is not None:
            names.append("album_add")         # 收图进图库（必须带描述）
            names.append("album_search")      # 按描述找图，不重新识图
        if self.bot.qzone.enabled:
            names.append("post_qzone")
            names.append("qzone_list")        # 读自己的说说
            names.append("qzone_comments")    # 读正文 + 全部留言
            names.append("qzone_reply")       # 回留言
            names.append("qzone_edit")        # 改自己的说说
        if tts.available(self.bot.config):
            names.append("speak")            # 用自己的声音发一条 QQ 语音
        if self.bot.config.get("tools.x_enabled", True):
            names.append("x_search")          # 搜 X 帖子/账号（官方 Search，不登录）
            names.append("x_read")            # 读 X 帖子/主页正文（官方 Fetch）
        return names

    def schemas(self) -> list[dict]:
        return [SCHEMAS[n] for n in self.available() if n in SCHEMAS]

    # ---------------------------------------------------------- 入口
    async def execute(self, ctx, name: str, args: dict) -> tuple[str, list[dict]]:
        if name not in self.available():
            return f"工具 {name} 现在不可用", []
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"未知工具 {name}", []
        try:
            return await handler(ctx, **args)
        except TypeError as e:
            return f"参数不对：{e}", []
        except Exception as e:
            log.error(f"工具 {name} 执行失败：{type(e).__name__} {e}")
            return f"工具执行失败：{type(e).__name__} {e}", []

    async def _t_account_action(self, ctx, operation: str, value: str = "",
                                target_user_id: int | None = None):
        actions = getattr(self.bot, "account", None)
        if actions is None:
            return "账号操作模块未启用。", []
        return await actions.execute(ctx, operation, value, target_user_id)

    async def _t_web_search(self, ctx, query: str):
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, lambda: websearch.search(query, self.bot.config))

        if out.get("answer"):
            n = out.get("searched") or 0
            tag = f"（联网搜索，发起了 {n} 次查询）" if n else "（联网搜索）"
            return f"{tag}{out['answer']}", []

        results = out.get("results") or []
        if not results:
            return f"没搜到东西（{out.get('error') or '空结果'}）。换个说法再试。", []

        lines = [f"搜索「{query}」的结果："]
        for i, r in enumerate(results[:5], 1):
            meta = ""
            if r.get("date"):
                meta = f"（{str(r['date'])[:10]}"
                if r.get("authority") not in (None, -1):
                    meta += f"，权威度 {r['authority']}"
                meta += "）"
            lines.append(f"{i}. {r['title']}{meta}\n   {r['url']}\n   {r['snippet'][:160]}")
        lines.append("需要细节就用 fetch_url 打开其中一条。")
        return "\n".join(lines), []

    async def _t_fetch_url(self, ctx, url: str):
        if not url.startswith(("http://", "https://")):
            return "网址要以 http:// 或 https:// 开头", []
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(
            None, lambda: websearch.fetch_smart(url, self.bot.config))
        if not out.get("ok"):
            return f"打不开：{out.get('error')}", []
        head = f"《{out['title']}》{url}\n" if out.get("title") else f"{url}\n"
        return head + out["text"], []

    async def _t_x_search(self, ctx, query: str, kind: str = "posts"):
        kind = "people" if str(kind or "").lower() == "people" else "posts"
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            None, lambda: websearch.x_search(query, self.bot.config, kind=kind, count=8))
        if not rows:
            return "X 上没搜到相关内容", []
        lines = []
        for i, r in enumerate(rows[:8], 1):
            url = r.get("url") or ""
            if kind == "people":
                bio = (r.get("bio") or "")[:80]
                lines.append(f"{i}. {r.get('name') or ''} {r.get('handle') or ''}：{bio} {url}")
            else:
                date = str(r.get("date") or "")[:10]
                text = (r.get("text") or "")[:120]
                who = r.get("author") or ""
                lines.append(f"{i}. {who} {date}：{text} {url}" if date
                             else f"{i}. {who}：{text} {url}")
        return "\n".join(lines), []

    async def _t_x_read(self, ctx, url: str):
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(
            None, lambda: websearch.x_read(url, self.bot.config))
        if out.get("error"):
            return f"读不了：{out['error']}", []
        head = (f"《{out['title']}》{out['url']}\n" if out.get("title")
                else f"{out.get('url') or url}\n")
        return head + out["text"], []

    async def _t_moegirl(self, ctx, query: str):
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, lambda: moegirl.lookup(query))
        if not out.get("ok"):
            return (f"萌娘百科没查到「{query}」（{out.get('error') or '没有条目'}）。"
                    "别硬猜出处，就说没见过、让对方讲讲，或者换个词再查。"), []
        if not out.get("extract"):
            return (f"萌娘百科有《{out['title']}》这个条目，但没读到内容：{out.get('url')}。"
                    "可以把链接发出去，或者说自己只知道个大概。"), []
        lines = [f"萌娘百科《{out['title']}》：", out["extract"]]
        if out.get("others"):
            lines.append("相关条目：" + "、".join(out["others"][:3]))
            lines.append("（回来的条目对不上群里的语境，比如查梗却查出一首歌，"
                         "就用上面的词或者加个「了」再查一次，别硬套。）")
        lines.append(f"链接：{out['url']}")
        lines.append("（看懂就行：用自己的话说，别复述条目原文，也别把考据当谈资。）")
        return "\n".join(lines), []

    async def _t_learn_meme(self, ctx, name: str, triggers, meaning: str,
                            usage: str = ""):
        book = getattr(self.bot, "memes", None)
        if book is None:
            return "梗库没开，记不了。", []
        if isinstance(triggers, str):
            triggers = [triggers]
        if not isinstance(triggers, (list, tuple)):
            triggers = []
        ok = book.learn(
            name,
            [str(t).strip() for t in triggers if str(t).strip()],
            meaning,
            usage,
            source="群友",
            learned_from=str(getattr(ctx, "session_id", "") or ""),
        )
        if not ok:
            return "这个记不下来：得有梗名、意思，还有至少两个字的触发词。", []
        return "记住了。", []

    async def _t_recall_memory(self, ctx, query: str):
        hits = self.bot.memory.recall(query, limit=6)
        if not hits:
            return "记忆区里没有相关的东西。", []
        return "记忆里翻到：\n" + "\n".join("- " + h for h in hits), []

    async def _t_remember(self, ctx, text: str, kind: str = "fact"):
        kind = kind if kind in ("fact", "slang", "impression", "people") else "fact"
        ok = self.bot.memory.remember(text, kind)
        return ("记住了。" if ok else "这条已经在记忆里了。"), []

    async def _t_remind(self, ctx, when: str, what: str):
        """用户说到点提醒：解析时间 → 写待办 → 顺手进长期记忆。"""
        if ctx is None:
            return "现在记不了提醒。", []
        what = str(what or "").strip()
        due_ts, desc = reminders.parse_when(when)
        if due_ts is None:
            return "没听懂是什么时候，换个说法（比如「明天下午三点」）", []
        if not what:
            return "要提醒你什么？说具体一点。", []
        store = getattr(self.bot, "reminders", None)
        if store is None:
            return "提醒功能没开，记不了。", []
        user_id = getattr(getattr(ctx, "event", None), "user_id", 0) or ctx.user_id
        who = ctx.sender_name
        store.add(
            session_id=ctx.session_id,
            group_id=ctx.group_id if ctx.is_group else None,
            user_id=user_id,
            who=who,
            what=what,
            due_ts=due_ts,
        )
        try:
            self.bot.memory.remember(f"{who} 约好 {desc} 提醒：{what}", "fact")
        except Exception as e:
            log.warn(f"约定写入记忆失败：{type(e).__name__} {e}")
        log.info(f"记下提醒：{desc} {who} {what[:40]}")
        return f"好，记下了：{desc} 提醒你 {what}", []

    async def _t_note_person(self, ctx, name: str, note: str):
        if ctx is None:
            return "现在记不了。", []
        uid = ctx.user_id
        if not uid:
            return "记不了，不知道是谁。", []
        # 「XX：YY」形式的前缀剥掉，剩下的才是内容
        body = note.strip()
        if "：" in body[:12] and not body.startswith(("性格", "说话方式", "在意",
                                                    "跟我", "常聊", "名字")):
            body = body.split("：", 1)[1].strip()
        ok = self.bot.people.remember(uid, name, body)
        if not ok:
            return "这条已经记过了。", []
        log.info(f"记下 {name}({uid})：{body[:40]}")
        return ("记下了。**不用说出来**，你只是心里有数。", [])

    async def _t_dirty_joke(self, ctx, topic: str = ""):
        """讲荤段子。没有素聊档，最小也是 1（收敛点）。"""
        sid = getattr(ctx, "session_id", "") if ctx else ""
        lv = self.bot.spice.get(sid)
        if not self.bot.config.get("spice.allow_joke", True):
            return ("我不讲荤段子。", [])
        topics = list(self.bot.config.get("spice.topics", []) or [])
        if topic:
            topic = topic.strip()
        elif topics:
            topic = random.choice(topics)
        text = await spice.joke(self.bot.llm, lv, topic or "")
        if not text:
            return ("想不出来，词穷了。", [])
        log.info(f"讲了个荤段子（档位 {lv}）：{text[:40]}")
        return (f"荤段子来了：\n{text}", [])

    async def _t_bili_video(self, ctx, keyword: str = "", up: str = "",
                            days=None, order: str = ""):
        sid = getattr(ctx, "session_id", "") if ctx else ""
        gap = float(self.bot.config.get("tools.bili_min_gap", 3600))
        if sid and not bilibili.share_allowed(sid, gap):
            return ("这个群刚分享过视频了，这次别再发新的。"
                    "就用你自己知道的说一句，或者干脆不接这个话题。"), []

        kw = (keyword or "").strip()
        up = (up or "").strip()
        # 純數字 = uid。報告裡的 author 是暱稱，uid 永遠比不上暱稱子字串，
        # 所以直接當「沒有可用報告」，老實投遞 kind="up" 讓插件去查。
        up_is_uid = bool(up) and up.isdigit()
        extra = _bili_extra(days, order)
        enabled = bool(self.bot.config.get("tools.bili_watch_enabled", True))
        wdir = str(self.bot.config.get("tools.bili_watch_dir",
                                       bili_watch.DEFAULT_DIR) or bili_watch.DEFAULT_DIR)

        # 有報告 = 真的看過，直接聊報告內容；沒有 = 投遞一個「去看」請求，
        # 退回只看標題，並老實說還沒看內容。up 優先：先找該 UP 已有的報告，
        # 找不到（或給的是 uid）就投遞 kind="up" 請求。
        if enabled:
            report = None
            try:
                if up and not up_is_uid:
                    report = bili_watch.pick_report(sid, author=up,
                                                    max_age_hours=72, dir=wdir)
                elif not up:
                    report = bili_watch.pick_report(sid, keyword=kw or None,
                                                    max_age_hours=72, dir=wdir)
            except Exception as e:
                log.warn(f"读 B 站观看报告失败：{type(e).__name__} {e}")
            if report is not None:
                bvid = str(report.get("bvid") or "")
                title = (report.get("title") or "").strip()
                url = report.get("url") or ""
                if sid:
                    bilibili.mark_shared(sid)
                if bvid:
                    try:
                        bili_watch.mark_shared(bvid, sid, dir=wdir)
                    except Exception as e:
                        log.warn(f"标记已分享失败：{type(e).__name__} {e}")
                segs: list[dict] = []
                # 默认不发 JSON 卡片（手机 QQ 会提示「发送者版本过低」）。
                if bilibili.card_enabled(self.bot.config):
                    try:
                        segs.append(bilibili.card(report))
                    except Exception:
                        pass
                if title and url:
                    segs.append(msg.Text(f"{title}\n{url}"))
                log.info(f"B站分享（看过报告 {report.get('watched_level')}）："
                         f"{title[:40]} {url}")
                return bili_watch.format_for_model(report), segs

            # 没有报告：让插件去看一眼，同时退回 pick_lively（只看标题）。
            # up 的契约只认 kind/value，给 days/order 插件也不看（P2-8），所以不传 extra。
            try:
                if up:
                    bili_watch.request_watch("up", up, sid, dir=wdir)
                else:
                    bili_watch.request_watch("keyword" if kw else "auto", kw, sid,
                                             dir=wdir, extra=extra)
            except Exception as e:
                log.warn(f"投递 B 站观看请求失败：{type(e).__name__} {e}")

        loop = asyncio.get_running_loop()
        try:
            picks = await loop.run_in_executor(
                None, lambda: bilibili.pick_lively(3, kw or None))
        except Exception as e:
            return f"刷 B 站失败：{type(e).__name__} {e}", []
        if not picks:
            return "B 站这会儿没刷出东西，换个时间或者换个关键词。", []

        v = picks[0]
        log.info(f"B站分享（只看标题）：{v['title'][:40]} {v['url']}")
        if sid:
            bilibili.mark_shared(sid)
        # 只看标题发出去的片也要记进 shared.json：插件只认这份档，
        # 不记的话下一次很可能又挑到同一条（P2-7）。
        v_bvid = str(v.get("bvid") or "").strip()
        if v_bvid:
            try:
                bili_watch.mark_shared(v_bvid, sid, dir=wdir)
            except Exception as e:
                log.warn(f"标记只看标题的 B 站视频失败：{type(e).__name__} {e}")
        segs: list[dict] = []
        # 默认不发 JSON 卡片（手机 QQ 会提示「发送者版本过低」）。
        # 纯文本 + 链接由 QQ 自己展开，比坏卡片强。
        if bilibili.card_enabled(self.bot.config):
            try:
                segs.append(bilibili.card(v))
            except Exception:
                pass
        segs.append(msg.Text(f"{v['title']}\n{v['url']}"))

        others = "；".join(x["title"][:20] for x in picks[1:])
        tip = f"备选（别都发）：{others}" if others else ""
        return (f"挑到一条：{bilibili.brief(v)}\n{tip}\n"
                f"这条你还没看过内容，只知道标题，别假装看过；"
                f"可以说「还没看，标题看着挺有意思」。"), segs

    async def _t_speak(self, ctx, text: str):
        """把自己的声音合成成 QQ 语音，长了会分成连续几条。"""
        cfg = self.bot.config
        if not tts.available(cfg):
            return "现在发不了语音，正常打字说就行。", []
        if ctx is not None and ctx.state.get("_spoke"):
            return "这条回复已经发过语音了，别再发第二条，正常打字说。", []
        segs = await tts.record_segments(cfg, text)
        if not segs:
            return "这次没录成语音，别提这事，正常打字说就行。", []
        if ctx is not None:
            ctx.state["_spoke"] = True
        log.info(f"语音已准备：{len(tts.clean_for_speech(text))} 字 / {len(segs)} 条")
        return ("语音已经发出去了，文字里别再重复这句。", segs)

    async def _t_pick_sticker(self, ctx, query: str = ""):
        sid = getattr(ctx, "session_id", "") if ctx else ""
        ledger = None
        try:
            from . import media
            ledger = media.ledger_for(self.bot)
        except Exception:
            pass
        if ledger is not None and sid and not ledger.sticker_allowed(sid):
            return ("这轮先别发表情包了，也别解释为什么没发、别提发送失败，"
                    "就当没这回事，正常说句话。"), []

        from .stickers import EMOTION_TAGS
        emotion = getattr(self.bot, "emotion", None)
        mood = (emotion.blend().get("dominant") if emotion else None) or ""
        mood_tags = EMOTION_TAGS.get(mood, [])
        wanted = " ".join(x for x in [str(query or "").strip(), *mood_tags] if x)
        exclude = set(ctx.state.get("_picked", [])) if ctx else set()
        path = self.bot.stickers.pick(wanted, exclude=exclude) if wanted else None
        if not path and self.bot.config.get("persona.sticker_fetch", True):
            seed = str(query or "").strip() or (mood_tags[0] if mood_tags else "")
            if seed:
                names = await asyncio.to_thread(
                    self.bot.stickers.fetch_from_doutu, seed, 1)
                if names:
                    path = str(Path(self.bot.stickers.dir) / names[0])
                else:
                    path = await asyncio.to_thread(
                        self.bot.stickers.fetch_remote, seed, [seed, *mood_tags])
        if not path:
            return "这轮没有合适的表情包，别硬发，也别提这件事，正常说话。", []
        name = os.path.basename(path)
        if ctx:
            ctx.state.setdefault("_picked", []).append(name)
        self.bot.stickers.mark_used(name)
        log.info(f"挑表情包：{name} ← {wanted or '随机'}")
        return ("表情包已准备，会作为单独一条消息发出，正文别再描述它。",
                [msg.Text(f"[STICKER:{Path(path).resolve().as_uri()}]")])

    async def _t_save_sticker(self, ctx, name: str, tags=None, desc: str = "",
                              source: str = "auto"):
        """收表情包：本条消息 → 引用的消息 → 本轮 search_image 搜到的图。"""
        if isinstance(tags, str):
            tags = [t for t in tags.replace("，", ",").replace("、", ",")
                    .replace(" ", ",").split(",") if t]
        if not isinstance(tags, list):
            tags = []

        ev = getattr(ctx, "event", None)
        urls = _image_urls(getattr(ev, "message", None))
        origin = "这条消息"
        if not urls and getattr(ev, "reply_id", None):
            conn = None
            conn_for = getattr(self.bot, "conn_for", None)
            try:
                conn = conn_for(getattr(ev, "self_id", 0) or None) if conn_for else None
            except Exception as e:
                log.warn(f"取协议连接失败，读不了引用：{type(e).__name__}")
            segs = None
            if conn is not None:
                try:
                    segs, _sender = await perceive._fetch_quoted(conn, ev.reply_id)
                except Exception as e:
                    log.warn(f"读引用的消息失败：{type(e).__name__}")
            urls = _image_urls(segs)
            origin = "引用的消息"
        if not urls and str(source or "").lower() == "search":
            state = getattr(ctx, "state", None) or {}
            urls = sorted(u for u in (state.get("avatar_ok_urls") or set())
                          if isinstance(u, str)
                          and u.startswith(("http://", "https://")))
            origin = "刚才搜到的图"

        if not urls:
            return ("没找到能存的表情包：这条消息和引用的消息里都没有图片。"
                    "让对方再发一张，或者先用 search_image 找图再存。"), []
        loop = asyncio.get_running_loop()
        raw = None
        for url in urls[:3]:
            # Pixiv 图床要 Referer，和 search_image 的下载规则保持一致
            item = {"url": url,
                    "need_referer": url.startswith("https://i.pximg.net/"),
                    "referer": "https://www.pixiv.net/"}
            try:
                raw = await loop.run_in_executor(
                    None, lambda it=item: imagesearch.download(it))
            except Exception as e:
                log.warn(f"存表情包下载失败：{type(e).__name__}")
                raw = None
            if raw:
                break
        if not raw:
            return f"图没下载下来（{origin}），这次没存成。", []

        try:
            fname = self.bot.stickers.save(raw, name, tags, desc)
        except ValueError as e:
            return f"没存成：{e}", []
        saved = [str(t) for t in ((self.bot.stickers.index().get(fname) or {})
                                  .get("tags") or tags or []) if str(t).strip()]
        tail = f"（标签：{'、'.join(saved)}）" if saved else ""
        log.info(f"存表情包：{fname} ← {origin}")
        return f"存好了：{fname}{tail}", []

    async def _t_search_image(self, ctx, query: str = "", n: int = 1,
                               source: str = "auto", explicit=None, tags=None):
        query = str(query or "")
        n = 1 if n is None else max(1, min(3, int(n)))
        if tags is not None:
            if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                return "tags 必须是当前请求现场选定的字符串标签列表。", []
            tags = [t.strip() for t in tags[:5] if t.strip()]
        raw_request = str(getattr(getattr(ctx, "event", None), "plain", "")
                          or getattr(ctx, "raw_text", "") or "")
        if imagesearch.mentions_furry(raw_request) or imagesearch.mentions_furry(
                " ".join([query, *(tags or [])])):
            return "这次请求或标签涉及已排除的福瑞内容，未进行搜索。", []
        if tags or source == "pixiv" or ("pixiv" in raw_request.casefold() and query.strip()):
            source = "pixiv"
        elif not query.strip() and source in ("auto", "pixiv", "home", None, ""):
            source = "home"
        # Pixiv 是标签 AND 搜索：中文描述也塞进去会一条都搜不到，
        # 所以有标签时只用标签（标签本来就是模型现场挑的作品标签）。
        if source == "pixiv" and tags:
            query = " ".join(dict.fromkeys(t for t in tags if t))
        requested = n
        candidate_count = min(8, max(n * 2, n + 2)) if source == "pixiv" else n
        sid = getattr(ctx, "session_id", "") if ctx else ""
        spice = self.bot.spice.get(sid) if sid else 2

        # 双层尺度：
        #   · 默认 —— 只给 16+/17+ 的擦边图（不露点但很色）
        #   · 对方**明确**说要 R18 且档位够，才放行 R18
        # 模型传的 explicit 只是意向，**服务端还要核一遍原话**，
        # 免得它把「找些色图」也当成 R18 请求。
        raw_msg = getattr(ctx, "raw_text", "") or ""
        said_r18 = imagesearch.wants_explicit(raw_msg)

        # 合并转发的聊天记录 / 引用消息，正文可能只剩「[聊天记录]」这种占位符，
        # 真正说 R18 的那句话在上文里。这种情况把最近几条也纳进来一起判定。
        if not said_r18:
            try:
                rows = self.bot.store.window(sid, 8) if sid else []
                recent = " ".join(str(r.get("content") or "") for r in rows[-8:])
                if recent:
                    said_r18 = imagesearch.wants_explicit(recent)
            except Exception:
                pass

        # 爱色色档位（2）：这个群本来就允许 R18，模型判断要 R18 就信它，
        # 不再死卡「原话里必须出现 R18 字样」——否则聊天记录、引用、
        # 上下文里的请求永远过不了这道校验。
        if not said_r18 and explicit and int(spice) >= 2:
            said_r18 = True
            log.info("爱色色档位：按模型的 explicit=true 放行 R18")

        allow_r18 = bool(explicit) and said_r18
        if explicit and not said_r18:
            log.info(f"模型想开 R18，但原话没明说，按擦边处理（{raw_msg[:40]!r}）")
        try:
            loop = asyncio.get_running_loop()
            # 最近发过的图先避开：同一批 Pixiv 热门作品反复被搜出来时，
            # 以前会在发送层按 URL 去重、结果一条都发不出去。
            avoid: set[str] = set()
            sid = getattr(ctx, "session_id", "") if ctx else ""
            if sid:
                try:
                    from . import media as media_mod
                    ledger = media_mod.ledger_for(self.bot)
                    avoid = ledger.recent_image_urls(sid)
                except Exception:
                    avoid = set()
            out = await loop.run_in_executor(
                None, lambda: imagesearch.image_search(
                    query, self.bot.config, candidate_count, source or "auto", spice,
                    allow_r18, avoid))
        except Exception as e:
            return f"找图失败：{type(e).__name__} {e}", []

        imgs = out.get("images") or []
        if not imgs:
            return (f"没搜到符合要求的图（{out.get('error') or '空结果'}）。"
                    "请根据失败原因重新构思当前主题的标签，保留角色等核心条件；"
                    "会话失效时说明需要 /pixiv登录，不要冒充免登录结果。"), []

        # Pixiv 的图必须自己下载再转 base64——它的图床强制 Referer，
        # 直接把 URL 给 QQ，NapCat 拉下来会 403。
        # 成人图先打码（黑条/裁切）再发；模式见 tools.nsfw_censor。
        censor = str(self.bot.config.get("tools.nsfw_censor", "bar") or "bar")
        # 打码用到的运行时配置（视觉定位开关）
        imagesearch._RUNTIME_CFG["nsfw_vision"] = bool(
            self.bot.config.get("tools.nsfw_vision", True))
        imagesearch._RUNTIME_CFG["pixiv_popular"] = str(
            self.bot.config.get("tools.pixiv_popular", "10000,5000,1000") or "")
        segs = []
        selected = []
        for x in imgs:
            if not imagesearch._allowed(x, bool(allow_r18) and spice >= 1):
                continue
            seg = await loop.run_in_executor(None, lambda x=x: self._image_marker(x, censor))
            if seg:
                segs.append(seg)
                selected.append(x)
                if len(segs) >= requested:
                    break
        if not segs:
            return ("候选图片下载或处理均失败，未准备任何图片；可重试当前主题的新标签。"), []

        src = out.get("provider")
        # 同一轮里若要拿这张图当头像，account_action 需要认这个原图地址
        urls = {u for x in selected
                for u in [x.get("url"), *(x.get("url_candidates") or [])]
                if isinstance(u, str) and u.startswith("https://")}
        state = getattr(ctx, "state", None)
        if urls and isinstance(state, dict):
            state.setdefault("avatar_ok_urls", set()).update(urls)
        viewed = [x.get("page") for x in selected if x.get("source") == "pixiv"]
        if viewed and self.bot.config.get("tools.pixiv_record_view", True):
            from .pixiv_session import PixivSession
            task = asyncio.create_task(PixivSession().record_views(viewed))
            self._bg_tasks = getattr(self, "_bg_tasks", set())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        evidence = "\n".join(
            f"{x.get('title') or '无标题'} {x.get('page') or ''} "
            f"{x.get('width', '?')}x{x.get('height', '?')} "
            f"收藏:{x.get('bookmark_count', '?')}"
            + (f" 原图:{x['url']}" if x.get("source") == "pixiv" else "")
            for x in selected)
        log.info(f"找图 {len(segs)} 张 ← 标签「{query}」via {src}")
        return (f"已准备 {len(segs)} 张图，图源 {src}，实际标签：{query}。\n"
                f"{evidence}\n"
                "图片会各自作为单独一条消息发出，正文里不要贴链接或 [IMG:] 标记，配一句短话就行。"
                "要拿其中一张当自己的头像，就把它的“原图”地址传给 account_action 的 avatar。"
                "只根据这些实际结果配话，不虚构看图感受。"), segs

    def _image_marker(self, item: dict, censor: str) -> dict | None:
        """先下载校验能用，再交给发送层：普通图给 [IMG:url]，要打码的图直接给处理好的图片段。

        发送层按 [IMG:url] 单独发一条，并负责去重、重试和降级；下载结果有缓存，不会重下。
        """
        if censor != "off" and imagesearch._needs_censor(item):
            return imagesearch.to_segment(item, censor, self.bot.llm)
        for url in dict.fromkeys([item.get("url"), *(item.get("url_candidates") or [])]):
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            raw = imagesearch.download({**item, "url": url, "url_candidates": []})
            if raw:
                return msg.Text(f"[IMG:{url}]")
        return None

    async def _t_recall_last(self, ctx):
        if ctx is None:
            return "这个场合撤不了。", []
        sid = ctx.session_id
        mid = self.bot.last_msg_id.get(sid)
        if not mid:
            return "没有可撤回的消息。", []
        conn = self.bot.conn_for(getattr(ctx, "self_id", 0) or None)
        if conn is None:
            return "协议端没连上，撤不了。", []
        try:
            await conn.call("delete_msg", message_id=int(mid), timeout=10)
        except Exception as e:
            return f"撤回失败：{type(e).__name__} {e}", []
        self.bot.last_msg_id.pop(sid, None)
        log.info(f"工具撤回 {sid} 消息 {mid}")
        return "已经撤回了。这次别说多余的话解释。", []

    async def _t_post_qzone(self, ctx, content: str, images=None):
        ok, why = self.bot.qzone.check_permission(ctx)
        if not ok:
            return f"不能发：{why}", []
        图们 = []
        库 = getattr(self.bot, "album", None)
        for 图id in (images or [])[:9]:
            项 = 库.get(str(图id)) if 库 is not None else None
            if not 项:
                continue
            p = str(项.get("path") or "")
            if p and os.path.isfile(p):
                图们.append(p)
        try:
            data = await self.bot.qzone.post(content, 图们)
        except Exception as e:
            return f"发空间失败：{type(e).__name__} {e}", []
        tid = (data or {}).get("tid", "")
        log.info(f"已发 QQ 空间：{content[:40]} tid={tid} 图 {len(图们)} 张")
        后缀 = f"（带了 {len(图们)} 张图）" if 图们 else ""
        return f"说说发出了{后缀}，tid={tid or '未知'}。", []

    # ---------------------------------------------------------- 图库 / 空间读写
    async def _t_album_add(self, ctx, path: str, desc: str, tags=None):
        库 = getattr(self.bot, "album", None)
        if 库 is None:
            return "图库没起来（album 未初始化）", []
        try:
            图id = 库.add(path, desc, tags or [], source="群聊")
        except ValueError as e:
            return f"没收进图库：{e}", []
        except Exception as e:
            return f"收图失败：{type(e).__name__} {e}", []
        return f"收进图库了，id={图id}。以后找这张图用这句描述就行，不用再看一遍。", []

    async def _t_album_search(self, ctx, query: str, n: int = 3):
        库 = getattr(self.bot, "album", None)
        if 库 is None:
            return "图库没起来（album 未初始化）", []
        命中 = 库.search(query, n)
        if not 命中:
            return (f"图库里没有跟「{query}」对得上的图。别硬发，也别自己编一张；"
                    "真要发就先用 search_image 找，再用 album_add 收进图库。"), []
        行 = [f"- {x['id']}：{x['desc']}" for x in 命中]
        return ("图库里找到这些（要发就用 [IMG:file://路径] 单独发一条，正文别贴路径）：\n"
                + "\n".join(行)
                + "\n路径：\n" + "\n".join(f"- {x['id']} → {x['path']}" for x in 命中)), []

    async def _t_qzone_list(self, ctx, limit: int = 10):
        ok, why = self.bot.qzone.check_permission(ctx)
        if not ok:
            return f"看不了：{why}", []
        结果 = await self.bot.qzone.list_own(limit)
        if not 结果.get("ok"):
            return f"看不了自己的空间：{结果.get('reason') or '未知原因'}", []
        if not 结果.get("items"):
            return "空间里还没有说说。", []
        行 = []
        for 项 in 结果["items"]:
            图 = f"（{len(项.get('images') or [])} 张图）" if 项.get("images") else ""
            行.append(f"- tid={项['tid']}：{项['content'][:80]}{图}")
        return "自己空间里最近的说说：\n" + "\n".join(行), []

    async def _t_qzone_comments(self, ctx, tid: str):
        ok, why = self.bot.qzone.check_permission(ctx)
        if not ok:
            return f"看不了：{why}", []
        结果 = await self.bot.qzone.comments(tid)
        if not 结果.get("ok"):
            return f"读不到留言：{结果.get('reason') or '未知原因'}", []
        段 = [f"说说正文：{结果.get('content') or '（没读到正文）'}"]
        留言 = 结果.get("comments") or []
        if not 留言:
            段.append("还没有留言。")
        else:
            段.append(f"全部留言（{len(留言)} 条，按时间）：")
            for i, c in enumerate(留言, 1):
                段.append(f"{i}. {c.get('nick') or '某人'}：{c.get('text') or ''}　[id={c.get('id')}]")
        段.append("回的时候要接得住整串对话，不只回最后一条。")
        return "\n".join(段), []

    async def _t_qzone_reply(self, ctx, tid: str, comment_id: str, text: str):
        ok, why = self.bot.qzone.check_permission(ctx)
        if not ok:
            return f"回不了：{why}", []
        结果 = await self.bot.qzone.reply(tid, comment_id, text)
        if not 结果.get("ok"):
            return f"没回成：{结果.get('reason') or '未知原因'}", []
        return "回了。", []

    async def _t_edit_persona(self, ctx, section: str, text: str, reason: str = ""):
        编 = getattr(self.bot, "selfeditor", None)
        if 编 is None:
            return "改不了：自主改提示词没起来", []
        果 = 编.改(section, text, reason)
        if not 果.get("ok"):
            return f"没改成：{果.get('reason')}", []
        return (f"改好了：「{果['section']}」{果['before']}→{果['after']} 字，"
                f"{果['note']}。旧版本备份在 {果['backup']}。"), []

    async def _t_qzone_edit(self, ctx, tid: str, content: str):
        ok, why = self.bot.qzone.check_permission(ctx)
        if not ok:
            return f"改不了：{why}", []
        结果 = await self.bot.qzone.edit(tid, content)
        if not 结果.get("ok"):
            return f"没改成：{结果.get('reason') or '未知原因'}", []
        return "改好了。", []
