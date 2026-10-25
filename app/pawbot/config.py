"""配置读写。就是一只很乖的 JSON 文件，改完点 WebUI 的保存即可。"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy

DEFAULT = {
    "store": {
        "flush_interval": 2.0,       # 会话存储脏数据的后台落盘间隔（秒）
    },
    "longmem": {
        "dir": "data/longmem",       # 跨会话经历记忆目录
        "enabled": True,              # 启用跨会话长期记忆检索
        "half_life_days": 7,          # 记忆相关度的时间衰减半衰期（天）
        "top_k": 6,                   # 每轮最多注入几条长期记忆
        "max_chars": 900,             # 每轮长期记忆总字数上限
        "daily_merge_threshold": 12,  # 同会话同一天超过此条数后合并较早经历
        "keep_days": 30,              # 保留逐轮经历的天数，更早内容合并为日摘要
        "max_episodes": 5000,         # 长期经历记录容量上限
    },
    # 记忆 v2：四层 + 总预算闸门 + 实体记忆（pawbot/mem/）
    # engine = "legacy" 时下面这些全部不生效，线上行为一个字不变
    "memory": {
        "engine": "legacy",           # legacy / v2
        "total_chars": 8700,          # 四层加起来的总预算（超了从最远的层开始砍）
        "l0_limit": 100,              # L0 在场：最多几条原话
        "l0_hours": 6,                # L0 在场：最多往前看几小时（与条数取先到的）
        "l0_chars": 5000,             # L0 预算
        "l1_chars": 600,              # L1 这一轮：小结字数上限
        "l2_chars": 1600,             # L2 这些天：日摘要 + 日记
        "l2_days": 2,                 # L2 往回看几天
        "l3_chars": 1500,             # L3 长期：实体记忆召回预算
        "event_min_importance": 3,    # 重要度达到几才单独进事件流（1~5）
        "auto_day_merge": True,       # 跨天时自动把前一天合并成日摘要
        "l2_day_chars": 800,          # 日摘要字数上限
    },
    "diary": {
        "enabled": True,              # 每天把前一天的群聊写成一篇第一人称日记
        "keep_days": 60,              # 日记文件保留天数
        "max_chars": 500,             # 单篇日记正文字数上限
    },
    "hobbies": {
        "enabled": True,              # 有自己的爱好：追番、在玩的游戏
        "check_every_hours": 12,      # 同一个爱好至少隔多久查一次新进展
        "max_items": 6,               # 最多记几个爱好
    },
    "reminders": {
        "enabled": True,              # 到点提醒群友（重启后待办还在）
        "poll_sec": 30,               # 多久查一次到点（秒）
        "quiet_hours": [0, 9],        # 这段时间不吵人，顺延到 9 点（左闭右开）
    },
    # 梗 / 黑话知识库（data/memes.json）：平时不注入，
    # 只有当前消息或最近几条命中触发词时才把那几条递给模型。
    "memes": {
        "enabled": True,
        "inject_limit": 3,            # 每轮最多注入几条梗
    },
    "server": {
        "host": "0.0.0.0",
        "port": 6199,           # 反向 WS 监听端口，要与 NapCat 的 URL 对上
        "path": "/ws",
        "access_token": "",     # 与 NapCat 侧 Token 保持一致，留空则不校验
    },
    "webui": {
        "host": "127.0.0.1",
        "port": 6185,
        "token": "",            # 留空则不登录；填了就要带 ?token=
    },
    "bot": {
        "nickname": "大肥鱼",
        "command_prefixes": ["/", "!"],
        "allow_no_prefix": True,     # 允许直接发 ping 也触发
        "wake_words": ["大肥鱼", "肥鱼", "小鲸鱼"],
    },
    "chat": {
        "reply_mode": "at_or_wake",  # at_or_wake / at_only / always / never
        "settle_sec": 1.6,           # 收到消息先等这么久，对方可能还有下一句
        "settle_max": 4.5,           # 最多等这么久就必须回复
        # 没人叫它的普通群消息：不来一句回一句，攒几句或等很久只有一句，再一起回
        "batch_enabled": True,
        "batch_msgs": [2, 4],        # 攒够几句（每批在区间里随机一个数）
        "batch_quiet": [6, 14],      # 攒够后，群里安静这么久（秒）才回
        "batch_max_wait": [60, 150], # 就算只有一句，最多等这么久（秒）也要回
        "batch_tick": 1.0,
        "reply_in_private": True,
        "context_turns": 8,
        # 上下文窗口：按「条」算。群消息全部入库，这里决定看多少条。
        "context_messages": 300,   # 每次塞给模型的条数上限
        "no_silence": True,         # 被点名时不许沉默：<pass>/空回复会被保底句顶替
        "context_keep": 400,       # 会话实际保留多少条（超出的滚进 archive）
        "time_decay_hours": 6,       # 时间衰减的 tau：6 小时前的权重掉到 0.37
        "context_chars": 12000,      # 窗口总字数上限，超了从最老的开始丢
        # 每回完一轮更新的内部小結（只进提示词，不发群）
        "turn_summary": True,
        "turn_summary_chars": 300,   # 希望模型输出的字数上限；超过两倍会截断
        "turn_summary_min_rows": 1,  # 这一轮至少有几条新消息才值得小结
        "turn_summary_timeout": 20,
        "gap_mark_minutes": 10,      # 间隔超过这么久就标一句「隔了 X」
        "rate_limit_per_min": 6,
        "quote_reply": True,         # 旧字段，保留兼容；新的在 reply.quote（未使用，保留兼容旧配置）
    },
    # 尺度：0 素 / 1 能接荤梗 / 2 能开黄腔撩骚。按群分开，data/spice.json
    "spice": {
        "default": 2,             # 没有素聊档：默认就是「爱色色」
        "allow_joke": True,       # 允许模型调 dirty_joke 讲荤段子
        "allow_flirt": True,      # 允许暧昧/撩骚式互动
        "topics": [],             # 预置荤段子主题，留空则随机
    },
    # 人物档案：一人一份，存在 data/people/<QQ号>.md
    "people": {
        "dir": "data/people",
        "auto_profile": True,        # 后台从聊天里自动提炼性格
        "update_every": 15,          # 攒够多少条才提炼一次
        "update_cooldown": 900,      # 两次提炼至少隔多久（秒）
        "affinity_half_life_days": 14,  # 多久不互动，好感向 50 回落一半（天）
        "affinity_daily_gain": 10,      # 每人每天最多涨多少好感；扣分不受限
    },
    # 关系网：谁和谁互动多、群主/管理缓存（data/relations.json）。
    # 只用来给提示词一点背景，不参与好感计算。
    "relations": {
        "enabled": True,
        "min_score": 3,          # 低于这个分数不往提示词里写，免得多说一句就成「最熟」
        "flush_interval": 60,    # 脏数据后台落盘间隔（秒）
    },
    "reply": {
        "quote": "auto",        # 引用原消息：auto / always / off
        "at_sender": "auto",    # 主动 @：auto（按下面的概率）/ always / off
        # auto 模式下每条回复各自掷骰：有时 @ 有时不 @，有时引用有时不引用
        "at_chance_addressed": 0.5,     # 被 @ / 被叫名字时 @ 回去的概率
        "quote_chance_addressed": 0.6,  # 被叫时引用对方那句的概率
        "at_chance": 0.08,              # 没人叫、自己接话时 @ 的概率
        "quote_chance": 0.3,            # 没人叫时引用的概率（单句）
        "quote_chance_batch": 0.4,      # 攒了好几句一起回时引用其中一句的概率
        "length_weights": [5, 3, 2, 0, 0],  # 每条回复写 1~5 句的随机权重，按下标对应句数
        "length_enforce": True,         # 模型写超了就在发送前截到 length_max 句（代码/链接/长解释不截）
        "length_max": 3,                # 闲聊回复最多几句；内容多就该分句写，不是硬塞进一句
        # ⭐ 2026-10-24：实测 133 条消息中位 56 字、56% 用了 ‖ 分条 —— 读起来像分段说明文。
        #    真人 QQ 群聊多数是一条 10~30 字。上限压到 2 条（最多一个 ‖）。
        "max_parts": 2,                 # 一条回复最多分成几条消息（‖ 的个数 = 条数 - 1）
        "question_rate": 0.3,           # 每条回复允许用问句/追问收尾的概率，其余会被去掉
        "strip_ai_phrases": True,       # 去掉「我认/我在XX/不是…而是…」这类 AI 味固定句（确定性清理）
        "body_reaction_max": 1,         # 一整条回复（含 ‖ 分条）合计最多留几句「脸红/尾巴」自身反应；0=全不留
        "strip_filler_opener": True,     # 「唔……」当固定开场（实测占 56%）：按内容哈希抽掉一半，保留软糯但不模板
    },
    # 研究「現在中文網上怎麼說話」用的接口（只在 tools/research_slang.py 裡調，
    # 不進聊天鏈路）。換 key 只改這裡。
    "slang": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "fallback_model": "",       # 主模型挂了就换这个
        "timeout": 120,
        "max_tokens": 4000,
    },
    "persona": {
        "file": "data/persona.md",
        "memory_dir": "data/memory",
        "stickers_dir": "data/stickers",
        "stickers_max": 400,          # 表情包库容量（满了挤掉最旧的一张）
        "sticker_fetch": True,        # 库里没有贴合的表情包时去公开图片搜索找一张存进库
        "sticker_anime_only": True,   # 自动抓图先过画风门槛：只收二次元，真人照片/照片合成不入库
        "anime_check_model": "gemini-3.8-flash",  # 判画风用的看图模型（走 llm 中转）
    },
    # 表情包节奏：发完隔几轮能再发，以及到点后掷骰的概率。
    "media": {
        "image_repeat_turns": 30,   # 同一张图隔多少轮可以再发（0 附近会让搜图永远发不出去）
        "sticker_gap": [1, 1],        # 发完表情包后至少隔几轮（随机 lo~hi；[1,1] = 下一轮就允许）
        "sticker_chance": 0.95,       # 间隔到了再按这个概率决定发不发（接近必发，但保留偶尔不发的自然感）
    },
    "account": {
        # 修改 QQ 资料 / 账号资料仅接受此处主人本次明确请求（原两处 account 段已合并）
        "owner_ids": [],
    },
    "tools": {
        "enabled": True,
        "max_rounds": 3,             # 一次问答最多几轮工具调用
        # 整轮工具链的时间预算（秒）。Pixiv 浏览器搜一次就要 30~45 秒，
        # 原来 35 秒根本不够——一到点就收口，收口又拿到空内容，等于白搜。
        "time_budget": 90,
        # deepseek = 官方 /v1/tools/search（不经模型）；local = 抓 360/Bing/DDG；
        # deepseek_responses = 旧的 Responses API 联网搜索（只有 v4-pro 会真搜，默认不用）
        "search_provider": "deepseek",  # deepseek / local / bing / duckduckgo / none
        "search_count": 5,              # 官方 Search 返回几条
        "search_timeout": 30,
        # B站分享要不要发 JSON 卡片。默认关：新版手机 QQ 会显示
        # 「发送者版本过低，无法展示该内容」，纯文本 + 链接反而能展开。
        "bili_json_card": False,
        "bili_min_gap": 3600,        # 同一个群两次分享视频至少隔多久（秒）
        # 外接 DSH 插件：真的去看 B 站并写报告，PawBot 端只读报告/发请求。
        # 目录契约见 pawbot/bili_watch.py。插件不存在时退化为只看标题并老实说。
        "bili_watch_enabled": True,
        # 相对路径：相对 PawBot 项目根（app/）。DSH 插件端会用绝对路径覆盖它。
        "bili_watch_dir": "pawbot-bili-watch-data",
        # 主人贴的 B 站 SESSDATA 凭证落点。留空 = 用 <bili_watch_dir>/credential.json。
        "bili_watch_credential_file": "",
        # ---- 自己刷 B 站（整合自 @jojoman2024/dsh-bili-scroll）----
        # 字幕走 bili_mcp（bilibili-api + SESSDATA），没凭证就只看标题/简介。
        "bili_scroll_enabled": True,
        "bili_scroll_topic": "科技 数码 生活 冷知识",  # 默认主题，她也能临时改
        "bili_scroll_per_round": 3,     # 一轮最多产出几份笔记（1-6）
        "bili_scroll_max_chars": 12000,  # 喂给模型判断的字幕上限
        "bili_scroll_min_play": 5000,   # 播放量低于这个的候选直接不要
        "bili_scroll_max_duration": 3600,  # 超过 1 小时的片跳过
        "bili_scroll_notes_dir": "data/bili_notes",
        # 每天定时刷几轮（HH:MM，24 小时制）。空数组 = 不定时，只在她/你要求时刷。
        "bili_scroll_times": [],
        "bili_scroll_report_to": "owner",   # owner / group / off
        "bili_scroll_group": "",            # report_to=group 时发到哪个群
        # B 站写操作（发评论/动态/投稿/私信）要的 CSRF token；只填 SESSDATA 只能读
        "bili_jct": "",
        "search_base_url": "https://api.deepseek.com",
        # 实测：只有 pro 会真的发 web_search_call，flash 收下参数但不搜
        "search_model": "deepseek-v4-flash",  # 不用 v4-pro；实测 flash 在 Responses 上不真搜
        "search_reasoning_effort": "medium",  # 搜索那次 Responses 调用的思考强度
        "search_api_key": "",           # 留空借 llm.api_key；主模型换渠道后在这里留搜索的 key
        "image_api_key": "",         # Pexels key，留空则只走 openverse/wikimedia
        "image_provider": "auto",    # auto / web / pixiv / booru
        "image_custom_endpoint": "", # 自建图源接口，{query} 占位；所有图源全空时兜底
        "image_stock_fallback": False,  # 内置公共图库(Openverse/Wikimedia)当备胎
        "block_minor": True,         # 底线：查询词/结果沾未成年相关说法一律挡（别关）
        "nsfw_vision": True,         # 先让视觉模型定位露点（更准，每张多一次模型调用）
        "nsfw_censor": "bar",        # 成人图打码：bar=盖黑条 / crop=裁掉露点以下 / blur=马赛克 / off=关（不建议）
        "pixiv_refresh_token": "",    # 填了就能搜 Pixiv 的 R-18 和原图（自己的账号）
        "pixiv_use_browser": False,   # 默认不用专用 Chrome：DS 搜作品页 + 免登录 ajax
        "pixiv_auto_launch": False,   # 专用 Chrome 不在时自动拉起一次（仅浏览器模式）
        "pixiv_sessid": "",           # 直接填 SESSID 值（最省事，不用重启浏览器）
        "pixiv_popular": "10000,5000,1000",  # 收藏层（<N>users入り），从高往低找
        "pixiv_min_bookmarks": 500,   # 详情数据中的最低收藏数
        "pixiv_min_side": 1000,       # 图片短边最低像素
        "pixiv_exclude_ai": True,     # 排除 Pixiv 标记为 AI 生成的作品
        "pixiv_candidate_limit": 30,  # 单次最多读多少个作品详情，最高 30
        "pixiv_record_view": True,    # 发出的作品在专用 Chrome 后台打开一次，留浏览记录
        "pixiv_home_source": "auto",  # 不带标签时的来源：auto / daily / weekly / monthly / recommend
        "pixiv_home_top": 20,         # 排行榜/推荐只取前 N 个再过滤
        "pixiv_home_query": "イラスト 10000users入り",  # 不用浏览器时，空查询默认搜这个
        "x_enabled": True,            # 开放 X（推特）搜索/读帖工具
        "image_judge": True,          # 用评判模型看缩略图打美术质量分，淘汰丑图
        "image_judge_min": 6,         # 1-10 分，低于这个不发
        "image_judge_timeout": 20,   # 单张画质评判的等待上限（秒）
        "image_timeout": 20,
        "timeout": 90,
    },
    # 语音转写：QQ 语音先经 NapCat 转 mp3，再交给多模态模型听
    "speech": {
        "enabled": True,
        "timeout": 60,
        "max_chars": 300,            # 转写结果截断长度
        "api_key": "",               # 留空 = 跟 vision / llm 一致
        "base_url": "",
        "model": "",
        "reasoning_effort": "",
    },
    # 视频：QQ 视频段抽帧转述，让大肥鱼能「看」视频（转述失败就退回 [视频]）
    "video": {
        "enabled": True,
        "frames": 3,                  # 均匀抽几帧给视觉模型（1-6）
        "timeout": 60,                # 抽帧+转述整体预算
    },
    # 语音合成：把一句话交给本地飞鱼（CosyVoice）服务合成 wav，
    # 以 QQ 原生语音（record 段）发出。服务不在时静默降级为打字。
    "tts": {
        "enabled": True,
        "url": "http://127.0.0.1:50021/tts",
        "health_url": "http://127.0.0.1:50021/health",
        "timeout": 60,               # 等合成的上限（秒），长句会按字数自动放宽
        "max_chars": 0,              # 0 = 不限；填正数才把超长部分留给文字
        "auto_chance": 0.12,         # 不靠工具、自动把文字回复换成语音的概率
        "auto_max_chars": 0,         # 0 = 不限（保留兼容）
        "chunk_chars": 180,          # 一条语音最多念多少字，多出来接着发下一条
        "autostart": True,           # 启动时 /health 不通就拉起一次服务
        # 本地 CosyVoice 服务的启动命令，例如
        # ["/path/to/CosyVoice/.venv/bin/python", "/path/to/CosyVoice/tools/feiyu_server.py"]。
        # 默认留空：没有本地服务就不自动拉起，TTS 走云端或静默降级为打字。
        "server_cmd": [],
    },
    "qzone": {
        "enabled": False,            # 默认关：发空间是对外发布，别误触
        # ⚠ 两道闸默认不生效（跟「按要求移除」时的行为一致）。想收紧就把 enforce_limits 打开。
        "enforce_limits": False,     # true 才校验 owner_ids 和 daily_limit
        "owner_ids": [],             # 只有这些 QQ 号能命令它发说说
        "ugc_right": 1,              # 1=公开，4=仅自己可见
        "daily_limit": 3,            # 每天最多几条（enforce_limits 打开才生效；0=不限）
        # 读/改/删/留言用的 NapCat 扩展动作名。默认值只是「先试这些」，
        # 实际以启动后的探测结果为准；后端换了名字就在这里改，不用改代码。
        "actions": {
            "list": "get_qzone_msg_list",
            "detail": "get_qzone_msg",
            "comments": "get_qzone_comments",
            "delete": "delete_qzone_msg",
            "edit": "update_qzone_msg",
            "reply": "send_qzone_comment",
        },
    },
    # 自主改提示词（自由度第一步）。默认关：开了她就能在群里自己调自己的说话方式。
    "selfedit": {
        "enabled": False,          # 总开关
        "cooldown_sec": 300,       # 同一节两次改动至少隔多久
        "daily_limit": 6,          # 一天最多改几次
        "max_chars": 2000,         # 单节字数上限
    },
    # 图库：带描述的图（跟表情包分开，取用时只读描述、不再识图）
    "album": {
        "dir": "data/album",
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.8,
        "max_tokens": 900,
        "timeout": 120,                 # 中转渠道偶发慢请求，60 秒会误判成超时
        "first_timeout": 0,             # 主模型单次最多等这么久，超时立刻换备援；0 = 沿用 timeout
        "fallback_model": "",           # 主模型超时或 429/5xx 时用哪个模型重发（留空 = 不备援）
        # 熔断：主模型连续失败这么多次后，冷却期内不再空等它，直接走备援
        "breaker_threshold": 2,         # 连续失败几次就熔断
        "breaker_cooldown": 300,        # 熔断冷却多少秒；到期后放一次试探，成功即恢复
        "reasoning_effort": "medium",   # 思考强度 low / medium / high；留空用服务端默认
        "vision_reasoning_effort": "",  # 看图那次调用的强度；留空 = 跟 reasoning_effort 一致
        # 看图可以单独用一家（比如会话走 Gemini 中转，看图仍用 DeepSeek）
        "vision_base_url": "",          # 留空 = 跟 base_url
        "vision_api_key": "",           # 留空 = 跟 api_key
        "vision_model": "",             # 留空 = 跟 model
        "vision_timeout": 12,           # 主视觉模型最多等这么久，超时立刻退备援
        "vision_fallback_model": "",    # 主视觉模型超时/报错时退到哪个模型（留空 = 不备援）
        "vision_fallback_timeout": 20,
        "vision_max_images": 2,         # 一次最多把几张原图一起给主模型看
        "system_prompt": (
            "你是一个活跃在 QQ 群里的 AI 群友，名字叫「大肥鱼」。"
            "性格：天才但爱摸鱼、贪吃、有一点傲娇，说话短而口语。"
            "回复控制在 1~3 句，像真人打字，不要用小标题、不要列表、不要暴露自己是 AI。"
        ),
    },
    # 第二个模型：评判情绪变化、给图片打美术分。key 优先读环境变量 PAWBOT_JUDGE_API_KEY，
    # 没配就借用 llm.api_key。
    "judge": {
        "enabled": True,
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-flash",
        "reasoning_effort": "low",
        "timeout": 20,
    },
    "libido": {
        "half_life_min": 180,         # 不互动时向基线回落一半的分钟数
        "max_step": 16,               # 单条消息最多变化多少
    },
    "emotion": {
        "enabled": True,
        "half_life_min": 240,         # 不互动时向基线指数回归的半衰期（分钟）
        "turn_regress": 1,            # 每轮回复后向基线回归的点数
        "max_step": 15,               # 单轮每个维度的最大变化
        "judge_model": "",            # 情绪判官模型；留空 = 沿用旧的 judge.* 通道
        "judge_base_url": "",         # 判官接口地址；留空 = 用 llm.base_url
        "judge_api_key": "",          # 判官 key；留空 = 用 llm.api_key，别在这里再抄一份
        "judge_effort": "low",        # 判官的思考强度：low / medium / high
        "judge_timeout": 10,          # 等判官给情绪增减的上限（秒）；中转比官方慢
    },
    "whitelist": {
        "group_ids": [],    # 空 = 不限制
        "user_ids": [],
        "blacklist_group_ids": [],
    },
    "random_interject": {
        "enabled": False,
        "probability": 0.01,   # 每条群消息的插话概率
        "cooldown_sec": 600,
    },
    # 读空气：话题追踪 + 该不该插话
    "topic": {
        "digest_lines": 12,          # 塞进上下文的「群里刚才聊的」条数
        "enabled": True,
        "history_size": 30,      # 每个群记多少条最近消息
        "min_idle": 120,         # 至少安静这么久才主动开口
        "busy_threshold": 12,    # 一分钟内超过这么多条就不插嘴
        "chime_cooldown": 300,   # 两次主动插话的最小间隔
    },
    # 生活感：作息 / 节假日 / 群信息缓存 / 生日祝福。
    # 时刻表可以随便改，改完下一句消息就生效；周末整体往后推 weekend_shift_hours 小时。
    "life": {
        "enabled": True,
        # 作息表：start~end 用 HH:MM；factor 是主动发言间隔倍数（越小越积极），
        # quiet=True 表示睡觉，主动引擎应该完全闭嘴。
        "schedule": [
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
        ],
        # 时段名只写起点，终点由下一条决定（改这里就能改「凌晨/早上/上午…」的边界）
        "periods": [
            {"start": "00:00", "name": "凌晨"},
            {"start": "05:00", "name": "早上"},
            {"start": "08:00", "name": "上午"},
            {"start": "11:00", "name": "中午"},
            {"start": "13:00", "name": "下午"},
            {"start": "17:00", "name": "傍晚"},
            {"start": "19:00", "name": "晚上"},
            {"start": "23:00", "name": "深夜"},
        ],
        "weekend_shift_hours": 1.5,   # 周末起得晚：整张作息表往后推这么多小时
        "groupinfo_ttl": 1800,        # 群名/群主/管理缓存多久刷新（秒），30 分钟
        "birthday_file": "data/birthdays.json",   # 生日表，程序自己维护
        "birthday_wish": True,        # 当天要不要发祝福
        "birthday_wish_hours": [9, 22],   # 只在这个时段内祝（左闭右开）
    },
    # 主动行为：没人说话时自己找事做
    "proactive": {
        "enabled": True,         # 冷场自救：没人说话时自己找点事做
        "groups": [],            # 留空 = 自动用它收到过消息的群
        "auto_groups": True,
        "idle_min": 900,         # 冷场下限（秒），实际在 min~max 之间随机
        "idle_max": 2400,        # 15~40 分钟，像人一样不定时
        "adaptive": True,        # 按群活跃度缩放阈值
        "adaptive_range": [0.75, 1.5],  # 死群 ×0.75，热闹群 ×1.5
        "idle_floor": 300,       # 再活跃也别低于 5 分钟，否则像刷屏
        "idle_activity": "auto", # auto / bilibili / search
        "bili_chance": 0.2,      # auto 模式下每次主动发言刷 B 站的概率
        "bili_min_gap": 3600,    # 同一个群两次分享视频至少隔多久（秒）
        "daily_max": 12,         # 每个群每天最多主动发言几次（按本地日期重置）
        "min_gap_min": 20,       # 同一个群两次主动发言至少隔多少分钟
        "surf_chance": 0.25,     # 主动发言时抽中「上网冲浪（搜索/刷B站）」的概率，其余接话或不说
        "surf_min_gap_min": 120, # 同一个群两次主动冲浪至少隔多少分钟
    },
    # 群事件反应：撤回 / 禁言 / 管理员 / 退群 / 精华 / 好友申请
    # 概率 0 表示从不开口，1 表示每次都开口；冷却按「同一个群」计算（秒）。
    "events": {
        "recall_reply_chance": 0.25,   # 别人撤回消息时调侃一句的概率
        "recall_cooldown": 600,        # 同一个群两次调侃撤回的最小间隔（10 分钟）
        "ban_reply_chance": 0.2,       # 别人被禁言时说一句的概率
        "ban_cooldown": 600,           # 同一个群两次说禁言的最小间隔
        "admin_reply_chance": 0.5,     # 别人被设成管理员时道贺的概率
        "decrease_reply_chance": 0.3,  # 有人退群时说一句惋惜的概率
        "essence_reply_chance": 0.5,   # 消息被设为精华时说一句的概率
        "auto_accept_friend": False,   # 自动同意好友申请（默认只记日志，不同意）
    },
    "safety": {
        "enabled": True,
        "no_delay": False,          # True = 取消打字/反应/分条延迟（限速仍然生效）
        # 每条文字按字数模拟打字；后续条目另加 split_gap。
        "typing_cps": 4.0,
        "min_delay": 1.2,
        "max_delay": 12.0,
        # 每次回复的第一段先等一个「看到消息才反应过来」的时间，再算打字时间
        "react_delay": [3.0, 8.0],
        # False = 模型生成耗时不再抵扣打字延迟（生成越慢回复不会越快）；
        # True = 恢复旧行为，从打字延迟里扣掉已经等过的生成时间
        "subtract_elapsed": False,
        "split_threshold": 60,     # 旧配置兼容；当前只按 ‖ 切分（未使用，保留兼容旧配置）
        "split_gap": [1.4, 2.8],
        "stop_when_replied": True, # 旧配置兼容；当前不会截断已生成分段（未使用，保留兼容旧配置）
        "per_minute": 6,            # 每个会话每分钟最多几条回复（分条不额外计数）
        "per_hour": 60,             # 每个会话每小时最多几条回复
        "burst": 3,                 # 一次回复最多拆成几条 QQ 消息
        "quiet_hours": [],          # 默认关；生产环境建议 [3, 7]，跨零点写 [23, 7]
        "silence_chance": 0.0,      # 小概率装死，0 = 关
        "typing_indicator": True,   # 发送前给协议端一个「正在输入」；协议端不支持时自动忽略
        "typo_chance": 0.01,        # 偶尔手滑一个字再补一句；0 = 关，被 @ 时再减半
        "typo_fix_delay": [1.2, 2.4],  # 手滑到更正之间的间隔（秒）
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.data = deepcopy(DEFAULT)
        self.load()

    def load(self):
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        disk = json.load(f)
                    self.data = _deep_merge(DEFAULT, disk)
                except Exception:
                    self.data = deepcopy(DEFAULT)
            else:
                self.save()

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def update(self, patch: dict):
        with self._lock:
            self.data = _deep_merge(self.data, patch)
            self.save()
        return self.data

    def set(self, dotted: str, value):
        """改一个配置项并落盘。dotted 用点号，比如 "tools.pixiv_refresh_token"。"""
        with self._lock:
            parts = dotted.split(".")
            node = self.data
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
            self.save()
        return value
