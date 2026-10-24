/**
 * @local/pawbot —— client.js（瀏覽器半端：DSH「設定」頁裡的 PawBot 控制面板）
 * ============================================================================
 * 設計目標（這一版整個重寫）：
 *   1. 所有看得到的欄位都有繁體中文標籤與「改了會怎樣」的說明。
 *   2. 設定頁不再把 200+ 個鍵平鋪：預設只顯示 73 個常用鍵，其餘 213 個
 *      收進「進階（原始 JSON）」；有中文標籤 / 原始 key / 說明全文搜尋。
 *   3. 字級、行高、間距、對比都照驗收規格（14px / 1.65、說明 12.5px、
 *      卡片 padding 14px 16px、卡片間 12px、每列上下 10px + 1px 分隔線）。
 *   4. 分頁順序：启动 / 状态 / 设定 / 人设，预设打开「启动」——新用户第一眼
 *      就能看到「一键安装 → 连 NapCat → 启动 PawBot」的可视化引导。每个动作
 *      按钮旁都写清楚后果，重启、发送测试消息、覆写人设、保存设定都必须二次确认。
 *   5. 每一列都有影响徽章：立即生效 / 需重启 PawBot / 需连上 NapCat /
 *      会影响她说话 / 会影响发图语音 / 会影响记忆 / 旧版兼容。
 *
 * 与 Host 的路由契约（全部同源、回 JSON）：
 *   GET  /pawbot-ui/status              GET  /pawbot-ui/config
 *   POST /pawbot-ui/config {patch}       GET  /pawbot-ui/persona
 *   POST /pawbot-ui/persona {content}    POST /pawbot-ui/restart
 *   POST /pawbot-ui/plugins/reload       POST /pawbot-ui/send {target,message,is_group}
 *
 * 启动页专用的路由契约（Host 侧由另一个代理实作，这里只照着写、不另造）：
 *   GET  /pawbot-ui/setup/status         POST /pawbot-ui/setup/run   {force?}
 *   GET  /pawbot-ui/napcat/status        POST /pawbot-ui/napcat/setup
 *   POST /pawbot-ui/napcat/qr            POST /pawbot-ui/boot
 *   GET  /pawbot-ui/boot/status
 *
 * 安全鐵律：遮罩值 "***已設定***" 只顯示「已設定（不會顯示）」，空的 secret 顯示「未設定」；
 * 絕不當真值寫回；每次存檔只送改動過的鍵（diff）；清空 secret 必須二次確認。
 * ============================================================================
 */

/* ═══════════════════════════════════════════════════════════════════════════
 * METADATA：所有可見欄位的中文標籤 / 說明 / 影響 / 控件型別
 * 每一列：[key, 中文標籤, 說明, 影響徽章[], 控件型別?, 額外設定?]
 * ═══════════════════════════════════════════════════════════════════════════ */

const IMPACTS = {
  instant: { label: '立即生效', tone: 'good' },
  restart: { label: '需重启 PawBot', tone: 'warn' },
  napcat: { label: '需连上 NapCat', tone: 'warn' },
  speech: { label: '会影响她说话', tone: 'info' },
  media: { label: '会影响发图／语音', tone: 'info' },
  memory: { label: '会影响记忆', tone: 'info' },
  compat: { label: '旧版兼容（现在无作用）', tone: 'muted' },
}

const COMMON = [
  // ── 模型與金鑰 ────────────────────────────────────────────────────────────
  ['llm.model', '聊天主模型', '她回话时主要调用的模型；换模型会直接改变说话风格、速度与额度。', ['speech', 'instant'], 'text'],
  ['llm.api_key', '主模型 API 密钥', '调用主模型的凭证；填错她会完全回不了话。留空或遮罩值都不会覆盖。', ['speech', 'instant'], 'secret'],
  ['llm.reasoning_effort', '主模型思考强度', '回话前想得多深：low 快、high 慢但更会推理；留空用服务端默认。', ['speech', 'instant'], 'select', { options: [['', '服务端默认'], ['low', '低（快）'], ['medium', '中'], ['high', '高（慢）']] }],
  ['llm.max_tokens', '主模型回复上限', '一次最多生成多少 token；太小会话说一半被切断。', ['speech', 'instant'], 'number', { min: 64, max: 32000, step: 64 }],
  ['llm.timeout', '主模型等待上限', '等主模型回话最多几秒；太短容易把慢请求误判成超时。', ['instant'], 'number', { min: 5, max: 600, step: 1 }],
  ['llm.vision_model', '看图用的视觉模型', '她看图时用的模型；留空就跟主模型一致。', ['media', 'instant'], 'text'],
  ['judge.model', '评审模型', '情绪判读与图片画质评分用的模型；换了会影响她读空气的判断。', ['speech', 'media', 'instant'], 'text'],
  ['emotion.enabled', '情绪系统总开关', '开启后她会依聊天内容产生情绪，回话语气也会跟着变。', ['speech', 'instant'], 'switch'],
  ['emotion.judge_model', '情绪判官模型', '专门判读情绪变化的模型；留空就沿用 judge 通道。', ['speech', 'instant'], 'text'],

  // ── 聊天與回覆 ────────────────────────────────────────────────────────────
  ['chat.reply_mode', '回话时机', '什么情况她才回：被 @ 或被叫才回、只认 @、每句都回，或完全不主动回。', ['speech', 'instant', 'napcat'], 'select', { options: [['at_or_wake', '被 @ 或被叫才回'], ['at_only', '只认 @'], ['always', '每句都回'], ['never', '不主动回']] }],
  ['chat.reply_in_private', '私聊也回', '开启后私聊消息也会走同一套回复流程。', ['speech', 'instant'], 'switch'],
  ['chat.context_messages', '上下文消息条数', '每次塞给模型的最近消息条数上限；越大越记得前面，但更花 token。', ['speech', 'instant', 'memory'], 'number', { min: 10, max: 2000, step: 10 }],
  ['chat.context_chars', '上下文总字数上限', '上下文加起来最多多少字；超过会从最旧的开始丢。', ['speech', 'instant'], 'number', { min: 1000, max: 200000, step: 500 }],
  ['chat.time_decay_hours', '旧消息遗忘半衰期', '几小时前的消息权重掉到约 0.37；越小越只看眼前。', ['speech', 'instant'], 'number', { min: 0.5, max: 168, step: 0.5 }],
  ['chat.batch_enabled', '攒话再一次回', '普通群消息先攒几句、等群里安静再一起回，避免一句一句刷屏。', ['speech', 'instant'], 'switch'],
  ['chat.rate_limit_per_min', '每分钟最多回几次', '同一个会话每分钟的回复上限，防止她刷屏。', ['speech', 'instant'], 'number', { min: 1, max: 60, step: 1 }],
  ['reply.max_parts', '一则回复最多分几条', '她用「‖」分条时最多拆成几条 QQ 消息；1 代表不分条。', ['speech', 'instant'], 'number', { min: 1, max: 10, step: 1 }],
  ['reply.quote', '引用原消息', '回话时要不要引用对方那句：auto 按概率、always 总是、off 不引用。', ['speech', 'instant'], 'select', { options: [['auto', '自动（按概率）'], ['always', '总是引用'], ['off', '不引用']] }],
  ['reply.at_sender', '回话时 @ 对方', '要不要 @ 发话的人：auto 按概率、always 总是、off 不 @。', ['speech', 'instant'], 'select', { options: [['auto', '自动（按概率）'], ['always', '总是 @'], ['off', '不 @']] }],
  ['reply.question_rate', '用问句收尾的概率', '每条回复允许用问句或追问收尾的概率，其余会被去掉。', ['speech', 'instant'], 'ratio'],
  ['reply.strip_ai_phrases', '去掉 AI 味固定句', '自动清掉「我认为／不是…而是…」这类很像 AI 的固定句式。', ['speech', 'instant'], 'switch'],
  ['reply.body_reaction_max', '自身反应句上限', '一整条回复最多留几句「脸红／尾巴」这类自身反应；0 代表全不留。', ['speech', 'instant'], 'number', { min: 0, max: 9, step: 1 }],
  ['reply.strip_filler_opener', '去掉「唔……」开场', '把「唔……」这种模板开场按内容抽掉一半，保留自然感。', ['speech', 'instant'], 'switch'],
  ['safety.enabled', '拟人延迟总开关', '关掉后她会立刻回话；开启才会有打字、反应与分条延迟。', ['speech', 'instant'], 'switch'],
  ['safety.typing_cps', '模拟打字速度', '每秒打几个字；数字越大，模拟打字时间越短。', ['speech', 'instant'], 'number', { min: 1, max: 30, step: 0.5 }],
  ['safety.min_delay', '最短回复延迟', '就算字很少，也至少等几秒才送出第一段。', ['speech', 'instant'], 'number', { min: 0, max: 60, step: 0.1 }],
  ['safety.max_delay', '最长回复延迟', '模拟等待的上限秒数，避免长句等到天荒地老。', ['speech', 'instant'], 'number', { min: 0, max: 300, step: 0.5 }],
  ['safety.stop_when_replied', '对方已回就停止（兼容字段）', '旧版兼容字段；目前不会截断已生成的分段，改它基本上没有作用。', ['compat'], 'switch'],

  // ── 主動與情緒 ────────────────────────────────────────────────────────────
  ['proactive.enabled', '冷场主动开口', '群里没人说话时，她会自己找话题或分享东西，不用等被叫。', ['speech', 'instant', 'napcat'], 'switch'],
  ['proactive.daily_max', '每群每日主动上限', '同一个群一天最多主动发言几次，按本地日期重置。', ['speech', 'instant', 'napcat'], 'number', { min: 0, max: 200, step: 1 }],
  ['proactive.idle_min', '冷场最短等待', '群里安静至少几秒才可能主动开口；实际会在上下限间随机。', ['speech', 'instant'], 'number', { min: 60, max: 86400, step: 30 }],
  ['proactive.idle_max', '冷场最长等待', '最多等到几秒就一定找事做；必须大于最短等待。', ['speech', 'instant'], 'number', { min: 60, max: 86400, step: 30 }],
  ['proactive.bili_chance', '主动刷 B 站的概率', 'auto 模式下，每次主动发言抽到分享 B 站视频的概率。', ['speech', 'instant'], 'ratio'],
  ['proactive.idle_activity', '冷场时做什么', '主动发言的内容来源：自动混合、刷 B 站，或上网搜索。', ['speech', 'instant'], 'select', { options: [['auto', '自动混合'], ['bilibili', '刷 B 站'], ['search', '上网搜索']] }],
  ['proactive.surf_chance', '主动上网冲浪概率', '主动发言时抽到上网搜索或刷 B 站的概率，其余是接话。', ['speech', 'instant'], 'ratio'],
  ['random_interject.enabled', '随机插话', '允许她在没被叫的群消息里偶尔插一句；概率与冷却在高级设置。', ['speech', 'instant'], 'switch'],
  ['life.enabled', '生活作息', '依时间套用她的作息与精神状态；睡觉时段会完全安静。', ['speech', 'instant'], 'switch'],

  // ── 記憶與人物 ────────────────────────────────────────────────────────────
  ['memory.engine', '记忆引擎版本', 'legacy 走传统行为；v2 是四层记忆＋总预算，切换会改变她记得什么。', ['memory', 'speech', 'instant'], 'select', { options: [['legacy', '旧版 legacy'], ['v2', '四层记忆 v2']] }],
  ['memory.total_chars', '记忆总字数预算', '四层记忆加起来的总字数上限；超了会从最远的层开始砍。', ['memory', 'speech', 'instant'], 'number', { min: 500, max: 100000, step: 100 }],
  ['memory.l0_hours', 'L0 原话回看小时数', 'L0 最多往回看几小时的最近原话，和条数上限取先到的。', ['memory', 'instant'], 'number', { min: 1, max: 168, step: 1 }],
  ['memory.l1_chars', 'L1 本轮小结字数', '这一轮对话的小结最多多少字。', ['memory', 'speech', 'instant'], 'number', { min: 100, max: 5000, step: 50 }],
  ['memory.l2_chars', 'L2 近日摘要字数', '这几天的日摘要加日记最多多少字。', ['memory', 'speech', 'instant'], 'number', { min: 100, max: 10000, step: 50 }],
  ['memory.l3_chars', 'L3 长期记忆字数', '实体长期记忆的召回预算。', ['memory', 'speech', 'instant'], 'number', { min: 100, max: 10000, step: 50 }],
  ['memes.enabled', '梗图鉴注入', '消息命中梗的触发词时，把那几条梗递给模型。', ['speech', 'instant'], 'switch'],
  ['diary.enabled', '每日日记', '每天把前一天的群聊写成一篇第一人称日记，隔天会影响她。', ['memory', 'speech', 'instant'], 'switch'],
  ['longmem.enabled', '跨会话长期记忆', '允许她检索以前别的时间/会话的经历。', ['memory', 'speech', 'instant'], 'switch'],
  ['people.auto_profile', '自动人物侧写', '从聊天里自动提炼每个人的性格与好感。', ['memory', 'instant'], 'switch'],
  ['relations.enabled', '关系网背景', '把谁和谁互动多、群主/管理这些背景写进提示词。', ['speech', 'instant'], 'switch'],

  // ── 圖片與媒體 ────────────────────────────────────────────────────────────
  ['media.sticker_chance', '表情包发送概率', '表情包间隔到了之后，实际决定要发的概率。', ['media', 'instant'], 'ratio'],
  ['persona.sticker_fetch', '缺图时自动找表情包', '表情包库没有贴合的图时，去公开图片搜索找一张存进库。', ['media', 'instant'], 'switch'],
  ['persona.sticker_anime_only', '只收二次元图', '自动抓图先过画风门槛，真人照片与照片合成不入库。', ['media', 'instant'], 'switch'],
  ['video.enabled', '看视频抽帧', 'QQ 视频段均匀抽几帧转述，让她真的能「看」视频。', ['media', 'instant'], 'switch'],
  ['speech.enabled', '听语音转文字', '把 QQ 语音先转 mp3 再交给多模态模型听。', ['media', 'instant'], 'switch'],
  ['tts.enabled', '语音回复', '把文字回复交给语音合成服务，用 QQ 原生语音送出。', ['media', 'instant'], 'switch'],
  ['tts.auto_chance', '自动转语音概率', '不用工具，自动把这次文字回复换成语音的概率。', ['media', 'speech', 'instant'], 'ratio'],
  ['tools.image_judge', '图片画质评审', '用评审模型看缩略图打分，淘汰明显不好看的图。', ['media', 'instant'], 'switch'],
  ['tools.nsfw_censor', '成人图打码方式', '侦测到成人图时的处理：盖黑条、裁切、马赛克或关闭。', ['media', 'instant'], 'select', { options: [['bar', '盖黑条'], ['crop', '裁掉露点以下'], ['blur', '马赛克'], ['off', '不打码（不建议）']] }],
  ['tools.block_minor', '未成年相关硬挡', '查询词或结果沾到未成年相关说法一律挡掉；不建议关闭。', ['media', 'instant'], 'switch'],

  // ── B 站與搜尋 ────────────────────────────────────────────────────────────
  ['tools.enabled', '工具调用总开关', '关掉后她不能用搜索、B 站、图片等任何工具，只能纯聊天。', ['speech', 'instant'], 'switch'],
  ['tools.max_rounds', '工具最多几轮', '一次问答最多连续调用几轮工具；太大会让回复变慢。', ['speech', 'instant'], 'number', { min: 0, max: 10, step: 1 }],
  ['tools.search_provider', '联网搜索来源', '要用哪种搜索后端：官方 DeepSeek、本地抓取、Bing、DuckDuckGo 或关闭。', ['instant'], 'select', { options: [['deepseek', 'DeepSeek 官方搜索'], ['local', '本地抓取'], ['bing', 'Bing'], ['duckduckgo', 'DuckDuckGo'], ['none', '关闭搜索']] }],
  ['tools.search_model', '搜索用的模型', '搜索那次调用使用的模型；和聊天主模型可以不同。', ['instant'], 'text'],
  ['tools.bili_min_gap', '同群 B 站分享间隔', '同一个群两次分享 B 站视频至少隔几秒，避免一直贴。', ['instant'], 'number', { min: 0, max: 86400, step: 60 }],
  ['tools.bili_json_card', 'B 站用 JSON 卡片', '开启会送结构化卡片；新版手机 QQ 可能显示不出来，纯文字链接反而能展开。', ['instant'], 'switch'],
  ['tools.x_enabled', 'X（推特）工具', '允许她搜索与读取 X 上的帖子。', ['instant'], 'switch'],

  // ── QQ 與服務 ────────────────────────────────────────────────────────────
  ['account.owner_ids', '主人 QQ 名单', '只有这些 QQ 号能命令她改资料或做敏感操作；留空等于没有主人。', ['napcat', 'instant'], 'json'],
  ['qzone.enabled', 'QQ 空间功能', '允许她读取、发布与回应 QQ 空间说说。', ['napcat', 'instant'], 'switch'],
  ['qzone.daily_limit', '空间每日发文上限', '每天最多发几条说说；要开启强制限额才会真的检查。', ['napcat', 'instant'], 'number', { min: 0, max: 100, step: 1 }],
  ['selfedit.enabled', '允许她修改自己人设', '开启后她能在群里自行调整自己的说话方式；默认关闭。', ['speech', 'instant'], 'switch'],
  ['server.port', 'NapCat 反向 WS 端口', 'NapCat 要连进来的 WebSocket 端口；改完必须重启，且两边要一致。', ['restart', 'napcat'], 'number', { min: 1, max: 65535, step: 1 }],
  ['webui.port', 'PawBot WebUI 端口', 'PawBot 自己的网页界面端口；改完必须重启 PawBot。', ['restart'], 'number', { min: 1, max: 65535, step: 1 }],
  ['whitelist.group_ids', '只服务这些群', '白名单群号；留空等于不限制。填入后其他群她不会回。', ['napcat', 'instant'], 'json'],
  ['whitelist.blacklist_group_ids', '封锁这些群', '黑名单群号；这些群她完全不回。', ['napcat', 'instant'], 'json'],
]

const ADVANCED = [
  // store
  ['store.flush_interval', '保存：后台落盘间隔', '会话脏数据每隔几秒写回磁盘；越小越安全但越常写文件。', ['instant'], 'number', { min: 0.5, max: 60, step: 0.5 }],
  // longmem
  ['longmem.dir', '长期记忆：数据目录', '跨会话经历记忆存放的文件夹，改路径等于搬家。', ['restart', 'memory'], 'text'],
  ['longmem.half_life_days', '长期记忆：时间衰减半衰期', '越久以前的经历相关度掉得越快；单位天。', ['memory', 'instant'], 'number'],
  ['longmem.top_k', '长期记忆：每轮注入条数', '每次回话最多捞几条长期记忆。', ['memory', 'speech', 'instant'], 'number'],
  ['longmem.max_chars', '长期记忆：每轮字数上限', '注入长期记忆的总字数，太大会挤掉眼前对话。', ['memory', 'speech', 'instant'], 'number'],
  ['longmem.daily_merge_threshold', '长期记忆：每日合并门槛', '同一天同一会话超过几条经历就合并成摘要。', ['memory', 'instant'], 'number'],
  ['longmem.keep_days', '长期记忆：逐轮保留天数', '超过天数的逐轮经历会并进日摘要。', ['memory', 'instant'], 'number'],
  ['longmem.max_episodes', '长期记忆：容量上限', '长期经历最多存几条，满了会淘汰旧的。', ['memory', 'instant'], 'number'],
  // memory
  ['memory.l0_limit', '记忆：L0 原话条数', 'L0 最多放几条最近原话。', ['memory', 'speech', 'instant'], 'number'],
  ['memory.l0_chars', '记忆：L0 字数预算', 'L0 原话层最多占多少字。', ['memory', 'speech', 'instant'], 'number'],
  ['memory.l2_days', '记忆：L2 回看天数', 'L2 日摘要往回看几天。', ['memory', 'instant'], 'number'],
  ['memory.event_min_importance', '记忆：事件进流门槛', '重要度 1–5，达到几才单独进事件流。', ['memory', 'instant'], 'number', { min: 1, max: 5, step: 1 }],
  ['memory.auto_day_merge', '记忆：跨天自动合并', '跨天时自动把前一天合并成日摘要。', ['memory', 'instant'], 'switch'],
  ['memory.l2_day_chars', '记忆：日摘要字数上限', '单日摘要最多多少字。', ['memory', 'speech', 'instant'], 'number'],
  // diary
  ['diary.keep_days', '日记：保留天数', '日记文件保留几天，过期会清。', ['memory', 'instant'], 'number'],
  ['diary.max_chars', '日记：单篇字数上限', '一篇日记正文最多多少字。', ['memory', 'speech', 'instant'], 'number'],
  // hobbies
  ['hobbies.enabled', '兴趣：总开关', '让她记得自己在追的番与在玩的游戏。', ['speech', 'instant'], 'switch'],
  ['hobbies.check_every_hours', '兴趣：追进度间隔', '同一个兴趣至少隔几小时才查新进展。', ['instant'], 'number'],
  ['hobbies.max_items', '兴趣：最多记几项', '最多同时追几个兴趣。', ['instant'], 'number'],
  // reminders
  ['reminders.enabled', '提醒：总开关', '到点提醒群友；重启后待办仍然保留。', ['speech', 'instant', 'napcat'], 'switch'],
  ['reminders.poll_sec', '提醒：检查间隔', '每隔几秒检查有没有到点的提醒。', ['instant'], 'number'],
  ['reminders.quiet_hours', '提醒：安静时段', '这段时间不吵人、顺延到结束；格式 [起, 止] 小时。', ['instant'], 'json'],
  // memes
  ['memes.inject_limit', '梗：每轮注入上限', '命中梗时最多注入几条。', ['speech', 'instant'], 'number'],
  // server
  ['server.host', '连接服务器：监听主机', 'NapCat 反向 WS 要连的地址；改错就收不到消息。', ['restart', 'napcat'], 'text'],
  ['server.path', '连接服务器：WS 路径', 'NapCat 反向 WS 的 URL 路径，两边要一致。', ['restart', 'napcat'], 'text'],
  ['server.access_token', '连接服务器：连接令牌', '与 NapCat 的 Token 一致；留空则不验证。', ['restart', 'napcat'], 'secret'],
  // webui
  ['webui.host', '网页界面：监听主机', 'PawBot 自己的 WebUI 绑在哪个地址。', ['restart'], 'text'],
  ['webui.token', '网页界面：登录令牌', 'WebUI 登录用；留空不验证。', ['restart'], 'secret'],
  // bot
  ['bot.nickname', '机器人：昵称', '她自称与唤醒判断用的名字。', ['speech', 'instant'], 'text'],
  ['bot.command_prefixes', '机器人：指令前缀', '哪些符号开头算指令，例如 / 与 !。', ['instant'], 'json'],
  ['bot.allow_no_prefix', '机器人：免前缀指令', '允许直接打 ping 也触发，不必加前缀。', ['speech', 'instant'], 'switch'],
  ['bot.wake_words', '机器人：唤醒词', '消息出现这些词就算在叫她。', ['speech', 'instant'], 'json'],
  // chat
  ['chat.settle_sec', '聊天：先等下一句', '收到消息先等几秒，对方可能还有下一句。', ['speech', 'instant'], 'number', { min: 0, max: 30, step: 0.1 }],
  ['chat.settle_max', '聊天：最多等多久', '就算对方还在打，最多等几秒就开始回。', ['speech', 'instant'], 'number', { min: 0.1, max: 60, step: 0.1 }],
  ['chat.batch_msgs', '聊天：攒几句才回', '普通群消息攒够几句（区间）再一起回。', ['speech', 'instant'], 'json'],
  ['chat.batch_quiet', '聊天：攒完等安静', '攒够后群里安静几秒才回（区间）。', ['speech', 'instant'], 'json'],
  ['chat.batch_max_wait', '聊天：单句最长等待', '就算只有一句，最多等几秒也要回（区间）。', ['speech', 'instant'], 'json'],
  ['chat.batch_tick', '聊天：批次检查间隔', '批次回复引擎每隔几秒检查一次。', ['instant'], 'number', { min: 0.1, max: 10, step: 0.1 }],
  ['chat.context_turns', '聊天：上下文轮数', '提示词里保留几轮对话（旧字段，条数以上下文消息条数为主）。', ['speech', 'compat'], 'number'],
  ['chat.no_silence', '聊天：被点名不许沉默', '被点名时空回或 <pass> 会被保底句顶替。', ['speech', 'instant'], 'switch'],
  ['chat.context_keep', '聊天：会话保留条数', '内存里实际保留几条消息，超出会封存。', ['memory', 'instant'], 'number'],
  ['chat.turn_summary', '聊天：每轮小结', '每轮回完更新内部小结，只进提示词、不发群。', ['speech', 'instant'], 'switch'],
  ['chat.turn_summary_chars', '聊天：小结字数上限', '希望模型输出的小结字数；超过两倍会截断。', ['speech', 'instant'], 'number'],
  ['chat.turn_summary_min_rows', '聊天：小结最少新消息', '这一轮至少几条新消息才值得做小结。', ['speech', 'instant'], 'number'],
  ['chat.turn_summary_timeout', '聊天：小结等待上限', '等小结模型的秒数，超时就跳过。', ['instant'], 'number'],
  ['chat.gap_mark_minutes', '聊天：时间断点标记', '间隔超过几分钟就在提示词标「隔了多久」。', ['speech', 'instant'], 'number'],
  ['chat.quote_reply', '聊天：旧引用开关', '旧版兼容字段；现在引用行为要看「引用原消息」。', ['compat'], 'switch'],
  // spice
  ['spice.default', '尺度：默认等级', '0 素／1 接荤梗／2 开黄腔；个别群可另外覆盖。', ['speech', 'instant'], 'number', { min: 0, max: 2, step: 1 }],
  ['spice.allow_joke', '尺度：允许荤段子', '允许模型讲 dirty_joke。', ['speech', 'instant'], 'switch'],
  ['spice.allow_flirt', '尺度：允许撩骚', '允许暧昧、撩骚式互动。', ['speech', 'instant'], 'switch'],
  ['spice.topics', '尺度：预置主题', '荤段子主题列表；留空则随机。', ['speech', 'instant'], 'json'],
  // people
  ['people.dir', '人物侧写：数据目录', '一人一份侧写文件存放的文件夹。', ['restart', 'memory'], 'text'],
  ['people.update_every', '人物侧写：提炼间隔', '攒够几条消息才提炼一次性格。', ['memory', 'instant'], 'number'],
  ['people.update_cooldown', '人物侧写：提炼冷却', '两次提炼至少隔几秒。', ['memory', 'instant'], 'number'],
  ['people.affinity_half_life_days', '人物侧写：好感半衰期', '多久不互动，好感向 50 回落一半。', ['memory', 'instant'], 'number'],
  ['people.affinity_daily_gain', '人物侧写：每日好感上限', '每人每天最多涨多少好感；扣分不受限。', ['memory', 'instant'], 'number'],
  // relations
  ['relations.min_score', '关系网：写入门槛', '互动分数低于这个值就不写进提示词。', ['speech', 'instant'], 'number'],
  ['relations.flush_interval', '关系网：落盘间隔', '脏数据每隔几秒写回磁盘。', ['instant'], 'number'],
  // reply
  ['reply.at_chance_addressed', '回复：被叫时 @ 的概率', '被 @ 或被叫名字时，回话顺便 @ 对方的概率。', ['speech', 'instant'], 'ratio'],
  ['reply.quote_chance_addressed', '回复：被叫时引用概率', '被叫时引用对方那句的概率。', ['speech', 'instant'], 'ratio'],
  ['reply.at_chance', '回复：自行接话 @ 概率', '没人叫、自己接话时 @ 人的概率。', ['speech', 'instant'], 'ratio'],
  ['reply.quote_chance', '回复：单句引用概率', '没人叫时引用原消息的概率。', ['speech', 'instant'], 'ratio'],
  ['reply.quote_chance_batch', '回复：批次引用概率', '攒好几句一起回时，引用其中一句的概率。', ['speech', 'instant'], 'ratio'],
  ['reply.length_weights', '回复：句数权重', '每条回复写 1–5 句的随机权重，依序对应。', ['speech', 'instant'], 'json'],
  ['reply.length_enforce', '回复：硬截句数', '模型写超了就发送前截到「最多几句」。', ['speech', 'instant'], 'switch'],
  ['reply.length_max', '回复：最多几句', '闲聊回复最多几句；程序、链接与长解释不截。', ['speech', 'instant'], 'number', { min: 1, max: 10, step: 1 }],
  // slang
  ['slang.base_url', '网络用语：API 地址', '研究「现在中文网上怎么说话」的接口。', ['instant'], 'text'],
  ['slang.api_key', '网络用语：API 密钥', '此研究接口的密钥。', ['instant'], 'secret'],
  ['slang.model', '网络用语：模型', '研究网络用语时用的模型。', ['instant'], 'text'],
  ['slang.fallback_model', '网络用语：备用模型', '主模型挂了改用的模型。', ['instant'], 'text'],
  ['slang.timeout', '网络用语：超时', '等这个接口的秒数。', ['instant'], 'number'],
  ['slang.max_tokens', '网络用语：token 上限', '单次调用最多产多少 token。', ['instant'], 'number'],
  // persona
  ['persona.file', '人设：文件路径', '人设文件在项目里的相对路径。', ['restart', 'speech'], 'text'],
  ['persona.memory_dir', '人设：记忆目录', '记忆相关文件存放的文件夹。', ['restart', 'memory'], 'text'],
  ['persona.stickers_dir', '人设：表情包目录', '表情包库存放的文件夹。', ['restart', 'media'], 'text'],
  ['persona.stickers_max', '人设：表情包容量', '库满几张后挤掉最旧的。', ['media', 'instant'], 'number'],
  ['persona.anime_check_model', '人设：画风检查模型', '自动抓图时判断是否二次元的模型。', ['media', 'instant'], 'text'],
  // media
  ['media.image_repeat_turns', '媒体：同图重发间隔', '同一张图隔几轮才能再发；太小会一直重复。', ['media', 'instant'], 'number'],
  ['media.sticker_gap', '媒体：表情包间隔轮数', '发完表情包后至少隔几轮（区间）。', ['media', 'instant'], 'json'],
  // tools
  ['tools.time_budget', '工具：整轮时间预算', '一次问答的工具链最多跑几秒，到点就收口。', ['instant'], 'number'],
  ['tools.search_count', '搜索：返回几条', '官方搜索一次回几条结果。', ['instant'], 'number'],
  ['tools.search_timeout', '搜索：超时', '等搜索结果的秒数。', ['instant'], 'number'],
  ['tools.bili_watch_enabled', 'B 站：外接看片插件', '启用「大肥鱼看 B 站」外接插件；关掉她只能看标题。', ['instant'], 'switch'],
  ['tools.bili_watch_dir', 'B 站：看片数据目录', '外接插件读写报告与凭证的文件夹。', ['instant'], 'text'],
  ['tools.bili_watch_credential_file', 'B 站：凭证文件路径', '主人提供的 B 站登录凭证文件；留空用默认。', ['instant'], 'text'],
  ['tools.search_base_url', '搜索：API 地址', '搜索服务的接口地址。', ['instant'], 'text'],
  ['tools.search_reasoning_effort', '搜索：思考强度', '搜索那次调用的思考强度 low/medium/high。', ['instant'], 'select', { options: [['', '服务端默认'], ['low', '低'], ['medium', '中'], ['high', '高']] }],
  ['tools.search_api_key', '搜索：API 密钥', '留空就借主模型的 key。', ['instant'], 'secret'],
  ['tools.image_api_key', '图片：图库 API 密钥', 'Pexels 等图库的 key；留空只走公共图库。', ['instant'], 'secret'],
  ['tools.image_provider', '图片：图源策略', '要用哪种图源：自动、网页、Pixiv 或 booru。', ['media', 'instant'], 'select', { options: [['auto', '自动'], ['web', '一般网页'], ['pixiv', 'Pixiv'], ['booru', 'Booru 图库']] }],
  ['tools.image_custom_endpoint', '图片：自建图源端点', '自架图源接口，用 {query} 占位。', ['instant'], 'text'],
  ['tools.image_stock_fallback', '图片：公共图库备用', '所有图源都空时，用 Openverse／Wikimedia 兜底。', ['media', 'instant'], 'switch'],
  ['tools.nsfw_vision', '图片：先用视觉定位', '先让视觉模型找露点再打码，较准但每张多一次调用。', ['media', 'instant'], 'switch'],
  ['tools.pixiv_refresh_token', 'Pixiv：Refresh 令牌', '填了才能搜 R-18 与原图。', ['instant'], 'secret'],
  ['tools.pixiv_use_browser', 'Pixiv：用专用浏览器', '改用专用 Chrome 跑 Pixiv。', ['instant'], 'switch'],
  ['tools.pixiv_auto_launch', 'Pixiv：自动拉起浏览器', '专用 Chrome 不在时自动开一次。', ['instant'], 'switch'],
  ['tools.pixiv_sessid', 'Pixiv：SESSID 登录码', '直接填 SESSID，最省事，不用重启浏览器。', ['instant'], 'secret'],
  ['tools.pixiv_popular', 'Pixiv：收藏层级', '例如 10000,5000,1000，从高往低找。', ['instant'], 'text'],
  ['tools.pixiv_min_bookmarks', 'Pixiv：最低收藏数', '作品收藏数低于此不采用。', ['instant'], 'number'],
  ['tools.pixiv_min_side', 'Pixiv：短边最低像素', '图片短边低于此不采用。', ['instant'], 'number'],
  ['tools.pixiv_exclude_ai', 'Pixiv：排除 AI 作品', '排除标记为 AI 生成的图。', ['media', 'instant'], 'switch'],
  ['tools.pixiv_candidate_limit', 'Pixiv：候选上限', '单次最多读几个作品详情，最高 30。', ['instant'], 'number', { min: 1, max: 30, step: 1 }],
  ['tools.pixiv_record_view', 'Pixiv：留下浏览记录', '发出的作品在专用 Chrome 后台开一次。', ['instant'], 'switch'],
  ['tools.pixiv_home_source', 'Pixiv：首页来源', 'auto／daily／weekly／monthly／recommend。', ['instant'], 'select', { options: [['auto', '自动'], ['daily', '每日'], ['weekly', '每周'], ['monthly', '每月'], ['recommend', '推荐']] }],
  ['tools.pixiv_home_top', 'Pixiv：首页取前 N', '排行榜或推荐只取前 N 个再过滤。', ['instant'], 'number'],
  ['tools.pixiv_home_query', 'Pixiv：默认查询词', '不用浏览器、空查询时默认搜这个。', ['instant'], 'text'],
  ['tools.image_judge_min', '画质：最低分数', '1–10 分，低于这个分数的图不发。', ['media', 'instant'], 'number', { min: 1, max: 10, step: 1 }],
  ['tools.image_judge_timeout', '画质：评审超时', '单张画质评审最多等几秒。', ['instant'], 'number'],
  ['tools.image_timeout', '图片：处理超时', '单次图片处理最多等几秒。', ['instant'], 'number'],
  ['tools.timeout', '工具：单次超时', '单一工具调用最多等几秒。', ['instant'], 'number'],
  // speech
  ['speech.timeout', '语音转写：超时', '等转写结果的秒数。', ['instant'], 'number'],
  ['speech.max_chars', '语音转写：字数上限', '转写结果截到多少字。', ['instant'], 'number'],
  ['speech.api_key', '语音转写：API 密钥', '留空就跟看图／主模型用同一组。', ['instant'], 'secret'],
  ['speech.base_url', '语音转写：API 地址', '留空就跟主模型一致。', ['instant'], 'text'],
  ['speech.model', '语音转写：模型', '留空就跟主模型一致。', ['instant'], 'text'],
  ['speech.reasoning_effort', '语音转写：思考强度', '留空就跟主模型的思考强度一致。', ['instant'], 'select', { options: [['', '跟随主模型'], ['low', '低'], ['medium', '中'], ['high', '高']] }],
  // video
  ['video.frames', '视频：抽几帧', '均匀抽 1–6 帧给视觉模型。', ['media', 'instant'], 'number', { min: 1, max: 6, step: 1 }],
  ['video.timeout', '视频：处理超时', '抽帧加转述的整体预算秒数。', ['instant'], 'number'],
  // tts
  ['tts.url', '语音合成：服务地址', '合成语音的 HTTP 端点。', ['media', 'instant'], 'text'],
  ['tts.health_url', '语音合成：健康检查地址', '启动时探测语音服务是否活着。', ['restart'], 'text'],
  ['tts.timeout', '语音合成：超时', '等合成的秒数，长句会自动放宽。', ['instant'], 'number'],
  ['tts.max_chars', '语音合成：字数上限', '0 代表不限；正数才把超长部分留给文字。', ['media', 'instant'], 'number'],
  ['tts.auto_max_chars', '语音合成：自动语音字数', '0 代表不限；保留旧版兼容。', ['media', 'compat'], 'number'],
  ['tts.chunk_chars', '语音合成：单段字数', '一条语音最多念多少字，多出来接着发下一条。', ['media', 'instant'], 'number'],
  ['tts.autostart', '语音合成：自动拉起服务', '启动时健康检查不通就拉起一次语音服务。', ['restart'], 'switch'],
  ['tts.server_cmd', '语音合成：启动指令', '自动拉起服务要跑的命令与参数。', ['restart'], 'json'],
  ['tts.provider', '语音合成：供应商', 'cloud 走云端，local 走本机服务。', ['media', 'instant'], 'select', { options: [['cloud', '云端'], ['local', '本机']] }],
  ['tts.cloud_url', '语音合成：云端地址', '云端 TTS 的 WebSocket 地址。', ['instant'], 'text'],
  ['tts.cloud_key', '语音合成：云端密钥', '阿里云语音合成的 key。', ['instant'], 'secret'],
  ['tts.cloud_voice', '语音合成：云端音色', '云端 TTS 的音色 ID。', ['media', 'instant'], 'text'],
  ['tts.cloud_format', '语音合成：音频格式', 'pcm 等输出格式。', ['instant'], 'text'],
  ['tts.cloud_sample_rate', '语音合成：采样率', '云端输出的采样率 Hz。', ['instant'], 'number'],
  // qzone
  ['qzone.enforce_limits', 'QQ 空间：强制限额', '开启才检查主人名单与每日上限。', ['napcat', 'instant'], 'switch'],
  ['qzone.owner_ids', 'QQ 空间：可发文主人', '只有这些 QQ 号能命令她发说说。', ['napcat', 'instant'], 'json'],
  ['qzone.ugc_right', 'QQ 空间：可见范围', '1 代表公开，4 代表仅自己可见。', ['napcat', 'instant'], 'number', { min: 1, max: 4, step: 1 }],
  ['qzone.actions', 'QQ 空间：NapCat 动作名', '读、改、删、留言用的扩展动作名称。', ['napcat', 'instant'], 'json'],
  // selfedit
  ['selfedit.cooldown_sec', '自我编辑：冷却', '同一节两次改动至少隔几秒。', ['speech', 'instant'], 'number'],
  ['selfedit.daily_limit', '自我编辑：每日次数', '一天最多改几次。', ['speech', 'instant'], 'number'],
  ['selfedit.max_chars', '自我编辑：单节字数', '单节最多改多少字。', ['speech', 'instant'], 'number'],
  // album
  ['album.dir', '图库：数据目录', '带描述的图库存放文件夹。', ['restart'], 'text'],
  // llm
  ['llm.base_url', '主模型：API 地址', '聊天模型的中转或官方端点。', ['instant'], 'text'],
  ['llm.temperature', '主模型：创造力', '越高越发散，0 最保守；太高会开始胡言乱语。', ['speech', 'instant'], 'number', { min: 0, max: 2, step: 0.1 }],
  ['llm.first_timeout', '主模型：首包超时', '主模型单次最多等几秒，超时立刻换备用；0 代表沿用等待上限。', ['instant'], 'number'],
  ['llm.fallback_model', '主模型：备用模型', '主模型超时或 429/5xx 时改用它；留空不备用。', ['speech', 'instant'], 'text'],
  ['llm.breaker_threshold', '主模型：熔断门槛', '连续失败几次就熔断。', ['instant'], 'number'],
  ['llm.breaker_cooldown', '主模型：熔断冷却', '熔断几秒后才放一次试探。', ['instant'], 'number'],
  ['llm.vision_reasoning_effort', '主模型：看图思考强度', '留空就跟主模型的思考强度一致。', ['media', 'instant'], 'select', { options: [['', '跟随主模型'], ['low', '低'], ['medium', '中'], ['high', '高']] }],
  ['llm.vision_base_url', '主模型：看图 API 地址', '留空就跟主模型地址一致。', ['media', 'instant'], 'text'],
  ['llm.vision_api_key', '主模型：看图 API 密钥', '留空就跟主模型密钥一致。', ['media', 'instant'], 'secret'],
  ['llm.vision_timeout', '主模型：看图超时', '主视觉模型最多等几秒，超时退备用。', ['media', 'instant'], 'number'],
  ['llm.vision_fallback_model', '主模型：看图备用模型', '视觉模型超时或报错时退到这。', ['media', 'instant'], 'text'],
  ['llm.vision_fallback_timeout', '主模型：看图备用超时', '看图备用模型等几秒。', ['media', 'instant'], 'number'],
  ['llm.vision_max_images', '主模型：一次看几张图', '一次最多把几张原图给视觉模型。', ['media', 'instant'], 'number', { min: 1, max: 10, step: 1 }],
  ['llm.system_prompt', '主模型：系统提示词', '她回话的底层规则；改这里等于改世界观，请小心。', ['speech', 'instant'], 'multiline'],
  // judge
  ['judge.enabled', '评审模型：启用', '情绪与图片评审的总开关。', ['speech', 'media', 'instant'], 'switch'],
  ['judge.base_url', '评审模型：API 地址', '评审模型的端点。', ['instant'], 'text'],
  ['judge.api_key', '评审模型：API 密钥', '留空就借主模型的 key。', ['instant'], 'secret'],
  ['judge.reasoning_effort', '评审模型：思考强度', 'low／medium／high。', ['instant'], 'select', { options: [['', '服务端默认'], ['low', '低'], ['medium', '中'], ['high', '高']] }],
  ['judge.timeout', '评审模型：超时', '等评审结果的秒数。', ['instant'], 'number'],
  // libido
  ['libido.half_life_min', '欲望：回落半衰期', '不互动时向基准回落一半的分钟数。', ['speech', 'instant'], 'number'],
  ['libido.max_step', '欲望：单条最大变化', '一则消息最多让数值变多少。', ['speech', 'instant'], 'number'],
  // emotion
  ['emotion.half_life_min', '情绪：回落半衰期', '不互动时向基准回归的分钟数。', ['speech', 'instant'], 'number'],
  ['emotion.turn_regress', '情绪：每轮回归点数', '每轮回复后向基准回归多少。', ['speech', 'instant'], 'number'],
  ['emotion.max_step', '情绪：单轮最大变化', '每个维度单轮最多变多少。', ['speech', 'instant'], 'number'],
  ['emotion.judge_base_url', '情绪：判官 API 地址', '留空就用主模型地址。', ['instant'], 'text'],
  ['emotion.judge_api_key', '情绪：判官 API 密钥', '留空就用主模型 key。', ['instant'], 'secret'],
  ['emotion.judge_effort', '情绪：判官思考强度', 'low／medium／high。', ['instant'], 'select', { options: [['', '服务端默认'], ['low', '低'], ['medium', '中'], ['high', '高']] }],
  ['emotion.judge_timeout', '情绪：判官超时', '等情绪增减结果的秒数。', ['instant'], 'number'],
  // whitelist
  ['whitelist.user_ids', '白名单：只理这些 QQ', '留空等于不限制；填了只回这些人的消息。', ['napcat', 'instant'], 'json'],
  // random_interject
  ['random_interject.probability', '随机插话：每条概率', '每条群消息被插话的概率。', ['speech', 'instant'], 'ratio'],
  ['random_interject.cooldown_sec', '随机插话：冷却', '两次插话至少隔几秒。', ['speech', 'instant'], 'number'],
  // topic
  ['topic.digest_lines', '读空气：摘要条数', '塞进上下文的「群里刚才聊的」条数。', ['speech', 'instant'], 'number'],
  ['topic.enabled', '读空气：启用', '追踪话题、判断该不该插话。', ['speech', 'instant'], 'switch'],
  ['topic.history_size', '读空气：每群记录条数', '每个群记多少条最近消息。', ['speech', 'instant'], 'number'],
  ['topic.min_idle', '读空气：最短安静时间', '至少安静几秒才主动开口。', ['speech', 'instant'], 'number'],
  ['topic.busy_threshold', '读空气：忙碌门槛', '一分钟超过几条就不插嘴。', ['speech', 'instant'], 'number'],
  ['topic.chime_cooldown', '读空气：插话冷却', '两次主动插话的最小间隔秒数。', ['speech', 'instant'], 'number'],
  // life
  ['life.schedule', '生活：作息表', '时段、标签、主动倍率与 quiet 的列表。', ['speech', 'instant'], 'json'],
  ['life.periods', '生活：时段名称', '凌晨／早上／上午…的边界设置。', ['speech', 'instant'], 'json'],
  ['life.weekend_shift_hours', '生活：周末延后时数', '周末整张作息表往后推几小时。', ['speech', 'instant'], 'number', { min: 0, max: 12, step: 0.5 }],
  ['life.groupinfo_ttl', '生活：群信息缓存', '群名、群主、管理缓存几秒刷新。', ['instant'], 'number'],
  ['life.birthday_file', '生活：生日表路径', '生日数据文件的位置，程序自己维护。', ['restart'], 'text'],
  ['life.birthday_wish', '生活：生日祝福', '当天要不要发祝福。', ['speech', 'instant'], 'switch'],
  ['life.birthday_wish_hours', '生活：祝福时段', '只在这个时段内祝；格式 [起, 止] 小时。', ['speech', 'instant'], 'json'],
  // proactive
  ['proactive.groups', '主动：指定群', '留空等于自动用它收到过消息的群。', ['napcat', 'instant'], 'json'],
  ['proactive.auto_groups', '主动：自动选群', '自动把收过消息的群纳入主动范围。', ['napcat', 'instant'], 'switch'],
  ['proactive.adaptive', '主动：依活跃度缩放', '死群更积极、热群更保守。', ['speech', 'instant'], 'switch'],
  ['proactive.adaptive_range', '主动：缩放范围', '例如 [0.75, 1.5]。', ['speech', 'instant'], 'json'],
  ['proactive.idle_floor', '主动：最短冷场下限', '再活跃也别低于几秒，否则像刷屏。', ['speech', 'instant'], 'number'],
  ['proactive.bili_min_gap', '主动：B 站分享间隔', '同群两次分享视频至少隔几秒。', ['instant'], 'number'],
  ['proactive.min_gap_min', '主动：两次发言间隔', '同群两次主动发言至少隔几分钟。', ['speech', 'instant'], 'number'],
  ['proactive.surf_min_gap_min', '主动：两次冲浪间隔', '同群两次主动上网至少隔几分钟。', ['speech', 'instant'], 'number'],
  // events
  ['events.recall_reply_chance', '群事件：撤回时调侃概率', '别人撤回消息时开口调侃的概率。', ['speech', 'instant', 'napcat'], 'ratio'],
  ['events.recall_cooldown', '群事件：撤回冷却', '同群两次调侃撤回至少隔几秒。', ['instant'], 'number'],
  ['events.ban_reply_chance', '群事件：禁言时开口概率', '别人被禁言时说一句的概率。', ['speech', 'instant', 'napcat'], 'ratio'],
  ['events.ban_cooldown', '群事件：禁言冷却', '同群两次说禁言至少隔几秒。', ['instant'], 'number'],
  ['events.admin_reply_chance', '群事件：设管理时道贺概率', '别人被设成管理员时道贺的概率。', ['speech', 'instant', 'napcat'], 'ratio'],
  ['events.decrease_reply_chance', '群事件：退群时惋惜概率', '有人退群时说一句的概率。', ['speech', 'instant', 'napcat'], 'ratio'],
  ['events.essence_reply_chance', '群事件：精华时开口概率', '消息被设精华时说一句的概率。', ['speech', 'instant', 'napcat'], 'ratio'],
  ['events.auto_accept_friend', '群事件：自动加好友', '自动同意好友申请；默认只记日志、不同意。', ['napcat', 'instant'], 'switch'],
  // safety
  ['safety.no_delay', '延迟：取消所有等待', '开启就取消打字、反应与分条延迟；限速仍然生效。', ['speech', 'instant'], 'switch'],
  ['safety.react_delay', '延迟：看到消息的反应时间', '第一段先等几秒再算打字时间（区间）。', ['speech', 'instant'], 'json'],
  ['safety.subtract_elapsed', '延迟：扣掉生成时间', '开启后模型生成越慢，回复前的等待越短。', ['speech', 'instant'], 'switch'],
  ['safety.split_threshold', '延迟：旧分条门槛', '旧版兼容字段；目前只按「‖」切分。', ['compat'], 'number'],
  ['safety.split_gap', '延迟：分条间隔', '每多一条消息额外等几秒（区间）。', ['speech', 'instant'], 'json'],
  ['safety.per_minute', '限速：每分钟上限', '每个会话每分钟最多几条回复。', ['speech', 'instant'], 'number'],
  ['safety.per_hour', '限速：每小时上限', '每个会话每小时最多几条回复。', ['speech', 'instant'], 'number'],
  ['safety.burst', '限速：一次最多拆几条', '一次回复最多拆成几条 QQ 消息。', ['speech', 'instant'], 'number'],
  ['safety.quiet_hours', '限速：安静时段', '这段时间不回；格式 [起, 止] 小时，可跨零点。', ['speech', 'instant'], 'json'],
  ['safety.silence_chance', '延迟：装死概率', '小概率已读不回；0 代表关闭。', ['speech', 'instant'], 'ratio'],
  ['safety.typing_indicator', '延迟：正在输入提示', '发送前给协议端「正在输入」；不支持时会自动忽略。', ['speech', 'instant'], 'switch'],
  ['safety.typo_chance', '延迟：手滑概率', '偶尔打错一个字再补一句；0 代表关闭。', ['speech', 'instant'], 'ratio'],
  ['safety.typo_fix_delay', '延迟：更正间隔', '手滑到更正的间隔秒数（区间）。', ['speech', 'instant'], 'json'],
]

const SECTION_LABELS = {
  store: '保存', longmem: '长期记忆', memory: '记忆', diary: '日记', hobbies: '兴趣',
  reminders: '提醒', memes: '梗', server: '连接服务器', webui: '网页界面', bot: '机器人身份',
  chat: '聊天', spice: '尺度', people: '人物侧写', relations: '关系网', reply: '回复',
  slang: '网络用语研究', persona: '人设', media: '媒体', account: '账号', tools: '工具',
  speech: '语音转写', video: '视频', tts: '语音合成', qzone: 'QQ 空间', selfedit: '自我编辑',
  album: '图库', llm: '主模型', judge: '评审模型', libido: '欲望', emotion: '情绪',
  whitelist: '白名单', random_interject: '随机插话', topic: '读空气', life: '生活',
  proactive: '主动', events: '群事件', safety: '安全延迟',
}

const GROUPS = [
  { id: 'model', label: '模型与密钥', hint: '主模型、评审模型与密钥' },
  { id: 'chat', label: '聊天与回复', hint: '怎么回、回多长、延迟多久' },
  { id: 'proactive', label: '主动与情绪', hint: '冷场、插话、作息与情绪' },
  { id: 'memory', label: '记忆与人物', hint: '记忆、日记、人物与关系' },
  { id: 'media', label: '图片与媒体', hint: '表情包、图片、视频、语音' },
  { id: 'bili', label: 'B 站与搜索', hint: '工具、搜索、B 站与 X' },
  { id: 'qq', label: 'QQ 与服务', hint: 'QQ 账号、空间、白名单与服务端口' },
  { id: 'advanced', label: '高级（原始 JSON）', hint: '其余原始键与整份 JSON' },
]

function groupForCommonKey(key) {
  if (key.startsWith('llm.') || key.startsWith('judge.') || key.startsWith('emotion.')) return 'model'
  if (key.startsWith('chat.') || key.startsWith('reply.') || key.startsWith('safety.')) return 'chat'
  if (key.startsWith('proactive.') || key.startsWith('random_interject.') || key === 'life.enabled') return 'proactive'
  if (key.startsWith('memory.') || key.startsWith('memes.') || key.startsWith('diary.') ||
      key.startsWith('longmem.') || key.startsWith('people.') || key.startsWith('relations.')) return 'memory'
  if (key.startsWith('media.') || key.startsWith('persona.') || key.startsWith('video.') ||
      key.startsWith('speech.') || key.startsWith('tts.') || key === 'tools.image_judge' ||
      key === 'tools.nsfw_censor' || key === 'tools.block_minor') return 'media'
  if (key.startsWith('tools.')) return 'bili'
  return 'qq'
}

function buildMeta(list, advanced) {
  return list.map((row) => {
    const [key, label, help, impact, control, extra] = row
    const dot = key.indexOf('.')
    return {
      key,
      section: dot < 0 ? key : key.slice(0, dot),
      leaf: dot < 0 ? '' : key.slice(dot + 1),
      label,
      help,
      impact: Array.isArray(impact) && impact.length ? impact : ['instant'],
      control: control || null,
      advanced: Boolean(advanced),
      group: advanced ? 'advanced' : groupForCommonKey(key),
      ...(extra || {}),
    }
  })
}

const COMMON_META = buildMeta(COMMON, false)
const ADVANCED_META = buildMeta(ADVANCED, true)
const ALL_META = COMMON_META.concat(ADVANCED_META)
const META_BY_KEY = Object.create(null)
for (const item of ALL_META) META_BY_KEY[item.key] = item

const PANEL_META = {
  common: COMMON_META,
  advanced: ADVANCED_META,
  metadata: META_BY_KEY,
  groups: GROUPS,
  impacts: IMPACTS,
  sectionLabels: SECTION_LABELS,
}

/* ═══════════════════════════════════════════════════════════════════════════
 * 共用小工具（純函數）
 * ═══════════════════════════════════════════════════════════════════════════ */

const MASK = '***已设定***'
const API = '/pawbot-ui'

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key)

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function getPath(obj, key) {
  if (!obj || typeof key !== 'string') return undefined
  let cur = obj
  for (const part of key.split('.')) {
    if (cur === null || typeof cur !== 'object') return undefined
    cur = cur[part]
  }
  return cur
}

function setPath(obj, key, value) {
  const parts = key.split('.')
  let cur = obj
  for (let i = 0; i < parts.length - 1; i += 1) {
    const part = parts[i]
    if (!isPlainObject(cur[part])) cur[part] = {}
    cur = cur[part]
  }
  cur[parts[parts.length - 1]] = value
}

function deepEqual(a, b) {
  if (Object.is(a, b)) return true
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false
    for (let i = 0; i < a.length; i += 1) if (!deepEqual(a[i], b[i])) return false
    return true
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const ak = Object.keys(a)
    const bk = Object.keys(b)
    if (ak.length !== bk.length) return false
    for (const k of ak) if (!hasOwn(b, k) || !deepEqual(a[k], b[k])) return false
    return true
  }
  return false
}

/** 只回「next 有變動」的嵌套物件；不做刪鍵（Host 是深合併）。 */
function deepDiff(base, next) {
  if (deepEqual(base, next)) return undefined
  if (!isPlainObject(base) || !isPlainObject(next)) return next
  const out = {}
  for (const key of Object.keys(next)) {
    const d = deepDiff(base[key], next[key])
    if (d !== undefined) out[key] = d
  }
  return Object.keys(out).length ? out : undefined
}

function countLeaves(value) {
  if (!isPlainObject(value)) return 1
  let n = 0
  for (const k of Object.keys(value)) n += countLeaves(value[k])
  return n
}

function flattenLeaves(value, prefix, out) {
  const target = out || []
  if (isPlainObject(value)) {
    for (const k of Object.keys(value)) flattenLeaves(value[k], prefix ? prefix + '.' + k : k, target)
  } else {
    target.push(prefix)
  }
  return target
}

/** 把遮罩字串從 patch 裡拿掉（host 也會丟，但這裡先不送）。 */
function stripMask(value) {
  if (value === MASK) return undefined
  if (Array.isArray(value)) {
    const out = []
    for (const item of value) {
      const stripped = stripMask(item)
      if (stripped !== undefined) out.push(stripped)
    }
    return out.length ? out : undefined
  }
  if (!isPlainObject(value)) return value
  const out = {}
  for (const k of Object.keys(value)) {
    const v = stripMask(value[k])
    if (v !== undefined) out[k] = v
  }
  return Object.keys(out).length ? out : undefined
}

function fmtDuration(sec) {
  const n = Math.max(0, Math.floor(Number(sec) || 0))
  const d = Math.floor(n / 86400)
  const h = Math.floor((n % 86400) / 3600)
  const m = Math.floor((n % 3600) / 60)
  const s = n % 60
  if (d) return `${d} 天 ${h} 时`
  if (h) return `${h} 时 ${m} 分`
  if (m) return `${m} 分 ${s} 秒`
  return `${s} 秒`
}

function fmtTime(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return ''
  try {
    return new Date(n).toLocaleString('zh-TW', { hour12: false })
  } catch {
    return String(n)
  }
}

function countOf(value) {
  if (typeof value === 'number') return value
  if (Array.isArray(value)) return value.length
  if (isPlainObject(value)) return Object.keys(value).length
  return null
}

function fmtCount(value) {
  const n = countOf(value)
  return n === null ? '—' : String(n)
}

function charCount(text) {
  return Array.from(String(text || '')).length
}

function isAbortError(err) {
  return Boolean(err && (err.name === 'AbortError' || err.code === 'ABORT_ERR'))
}

function friendlyError(err) {
  const msg = String((err && err.message) || err || '未知错误')
  if (err && err.status === 0) return msg + '（请确认 DSH Host 还活着）'
  return msg
}

class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function api(method, path, body, signal) {
  let res
  try {
    res = await fetch(API + path, {
      method,
      credentials: 'same-origin',
      headers: body === undefined ? undefined : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if (isAbortError(err)) throw err
    throw new ApiError('连不上 DSH Host 的 /pawbot-ui 通道：' + String((err && err.message) || err), 0)
  }
  let data = null
  try {
    data = await res.json()
  } catch {
    data = null
  }
  if (!res.ok) {
    throw new ApiError((data && (data.error || data.message)) || 'HTTP ' + res.status, res.status)
  }
  if (data && data.ok === false) {
    throw new ApiError(data.error || '主机回报失败', res.status)
  }
  return data
}

/* ═══════════════════════════════════════════════════════════════════════════
 * 樣式：全部用 theme token（token 名取自 Theme.listTokens / BUILTIN_INSPECT_TOKENS）
 * ═══════════════════════════════════════════════════════════════════════════ */

const CSS = `
[data-pb-root]{
  font-size:14px;line-height:1.65;color:var(--dsw-alias-label-primary);
  display:flex;flex-direction:column;gap:16px;width:100%;max-width:1180px;
  container-type:inline-size;
}
[data-pb-top]{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;}
[data-pb-eyebrow]{font-size:12.5px;line-height:1.6;color:var(--dsw-alias-label-secondary);}
[data-pb-title]{font-size:16px;line-height:1.4;font-weight:600;margin:0;color:var(--dsw-alias-label-primary);}
[data-pb-sub]{margin:2px 0 0;font-size:12.5px;line-height:1.6;color:var(--dsw-alias-label-secondary);}
[data-pb-tabs]{display:flex;gap:4px;flex-wrap:wrap;padding:4px;border-radius:var(--dsw-radius-md);background:var(--dsw-alias-bg-layer-2);}
[data-pb-tab]{font:inherit;font-size:14px;line-height:1.65;cursor:pointer;border:0;background:none;
  color:var(--dsw-alias-label-secondary);padding:7px 14px;border-radius:var(--dsw-radius-sm);}
[data-pb-tab]:hover{color:var(--dsw-alias-label-primary);background:var(--dsw-alias-interactive-bg-hover);}
[data-pb-tab][data-active]{color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-1);
  font-weight:600;box-shadow:var(--dsw-elevation-soft);}
[data-pb-tabpanel][data-pb-hidden]{display:none;}

[data-pb-root] :is(button,input,select,textarea):focus-visible{
  outline:none;box-shadow:0 0 0 var(--dsw-focus-ring-width) var(--dsw-focus-ring-color);
}

[data-pb-stack]{display:flex;flex-direction:column;gap:12px;}
[data-pb-card]{padding:14px 16px;border:1px solid var(--dsw-alias-border-l1);
  border-radius:var(--dsw-radius-lg);background:var(--dsw-alias-settings-card-fill);}
[data-pb-card-head]{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
  flex-wrap:wrap;margin-bottom:6px;}
[data-pb-card-title]{font-size:16px;font-weight:600;line-height:1.4;margin:0;color:var(--dsw-alias-label-primary);}
[data-pb-card-sub]{font-size:12.5px;line-height:1.6;color:var(--dsw-alias-label-secondary);}

[data-pb-kpis]{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;}
[data-pb-kpi]{padding:14px 16px;border:1px solid var(--dsw-alias-border-l1);
  border-radius:var(--dsw-radius-lg);background:var(--dsw-alias-settings-card-fill);min-width:0;}
[data-pb-kpi-label]{font-size:12.5px;line-height:1.6;color:var(--dsw-alias-label-secondary);}
[data-pb-kpi-num]{font-size:28px;font-weight:600;line-height:1.2;margin:2px 0 0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--dsw-alias-label-primary);}
[data-pb-kpi-sub]{font-size:12.5px;line-height:1.5;color:var(--dsw-alias-label-secondary);}
[data-pb-kpi][data-tone="good"] [data-pb-kpi-num]{color:var(--dsw-alias-state-success-primary);}
[data-pb-kpi][data-tone="err"] [data-pb-kpi-num]{color:var(--dsw-alias-state-error-primary);}
[data-pb-kpi][data-tone="warn"] [data-pb-kpi-num]{color:var(--dsw-alias-state-warn-primary);}

[data-pb-info-row]{display:flex;gap:16px;padding:10px 0;border-bottom:1px solid var(--dsw-alias-border-l1);
  align-items:flex-start;}
[data-pb-info-row]:last-child{border-bottom:0;}
[data-pb-info-label]{flex:0 0 150px;color:var(--dsw-alias-label-secondary);font-size:14px;}
[data-pb-info-value]{flex:1;min-width:0;color:var(--dsw-alias-label-primary);word-break:break-word;}
[data-pb-info-value][data-tone="err"]{color:var(--dsw-alias-state-error-primary);}
[data-pb-info-value][data-tone="warn"]{color:var(--dsw-alias-state-warn-primary);}
[data-pb-info-value][data-tone="good"]{color:var(--dsw-alias-state-success-primary);}

[data-pb-config]{display:grid;grid-template-columns:208px minmax(0,1fr);gap:16px;align-items:start;}
[data-pb-config][data-nav-collapsed]{grid-template-columns:minmax(0,1fr);}
[data-pb-nav]{position:sticky;top:0;align-self:start;display:flex;flex-direction:column;gap:4px;
  max-height:calc(100vh - 140px);overflow:auto;padding:10px;border:1px solid var(--dsw-alias-border-l1);
  border-radius:var(--dsw-radius-lg);background:var(--dsw-alias-bg-layer-1);}
[data-pb-nav-head]{display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:2px 4px 8px;border-bottom:1px solid var(--dsw-alias-border-l1);margin-bottom:4px;}
[data-pb-nav-title]{font-size:14px;font-weight:600;color:var(--dsw-alias-label-primary);}
[data-pb-nav-list]{display:flex;flex-direction:column;gap:2px;}
[data-pb-nav-btn]{display:flex;align-items:center;gap:6px;width:100%;box-sizing:border-box;
  text-align:left;padding:8px 10px;border-radius:var(--dsw-radius-sm);border:0;cursor:pointer;
  background:none;font:inherit;font-size:14px;line-height:1.5;color:var(--dsw-alias-label-secondary);}
[data-pb-nav-btn]:hover{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary);}
[data-pb-nav-btn][data-active]{background:var(--dsw-alias-interactive-bg-active);
  color:var(--dsw-alias-label-primary);font-weight:600;}
[data-pb-nav-count]{margin-left:auto;font-size:12.5px;color:var(--dsw-alias-label-secondary);}
[data-pb-cfg-main]{min-width:0;display:flex;flex-direction:column;gap:12px;}
[data-pb-cfg-top]{position:sticky;top:0;z-index:6;display:flex;flex-direction:column;gap:6px;
  padding:10px 0 8px;background:var(--dsw-alias-bg-base);border-bottom:1px solid var(--dsw-alias-border-l1);}
[data-pb-cfg-tools]{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
[data-pb-search]{flex:1 1 260px;min-width:180px;max-width:520px;}
[data-pb-toggle]{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--dsw-alias-label-primary);}
[data-pb-cfg-hint]{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12.5px;
  line-height:1.6;color:var(--dsw-alias-label-secondary);}
[data-pb-grow]{flex:1;min-width:0;}
[data-pb-dirty]{color:var(--dsw-alias-state-warn-primary);}

[data-pb-fields]{display:grid;grid-template-columns:minmax(0,1fr);gap:0;column-gap:28px;}
[data-pb-field]{display:flex;flex-direction:column;gap:6px;padding:10px 0;
  border-bottom:1px solid var(--dsw-alias-border-l1);min-width:0;}
[data-pb-field-head]{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;flex-wrap:wrap;}
[data-pb-field-label]{font-size:14px;font-weight:600;line-height:1.5;color:var(--dsw-alias-label-primary);}
[data-pb-help]{font-size:12.5px;line-height:1.6;color:var(--dsw-alias-label-secondary);}
[data-pb-help] code,[data-pb-keychip]{font-family:ui-monospace,Consolas,monospace;font-size:12px;
  color:var(--dsw-alias-label-secondary);background:var(--dsw-alias-bg-layer-2);
  border-radius:var(--dsw-radius-xs);padding:1px 5px;}
[data-pb-control]{min-width:0;}

[data-pb-input],[data-pb-textarea],[data-pb-select]{
  width:100%;box-sizing:border-box;font:inherit;font-size:14px;line-height:1.5;
  color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-1);
  border:1px solid var(--dsw-alias-border-l2);border-radius:var(--dsw-radius-sm);padding:7px 10px;}
[data-pb-input]::placeholder,[data-pb-textarea]::placeholder{color:var(--dsw-alias-label-tertiary);}
[data-pb-textarea]{resize:vertical;min-height:76px;line-height:1.65;}
[data-pb-number]{max-width:220px;}

[data-pb-ratio]{display:flex;align-items:center;gap:12px;}
[data-pb-ratio] input[type="range"]{flex:1;min-width:0;accent-color:var(--dsw-alias-brand-primary);}
[data-pb-ratio-val]{font-size:14px;min-width:52px;text-align:right;color:var(--dsw-alias-label-primary);}

[data-pb-sw]{flex:none;width:42px;height:24px;border-radius:999px;border:1px solid var(--dsw-alias-border-l2);
  cursor:pointer;position:relative;padding:0;background:var(--dsw-alias-state-idle-primary);}
[data-pb-sw][data-on]{background:var(--dsw-alias-brand-primary);border-color:var(--dsw-alias-brand-primary);}
[data-pb-sw] i{position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;
  background:var(--dsw-alias-switch-thumb);transition:left .15s;display:block;}
[data-pb-sw][data-on] i{left:21px;}
[data-pb-sw][disabled]{opacity:.45;cursor:not-allowed;}

[data-pb-btn]{font:inherit;font-size:14px;line-height:1.5;padding:7px 12px;
  border-radius:var(--dsw-radius-sm);cursor:pointer;border:1px solid var(--dsw-alias-border-l2);
  background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);}
[data-pb-btn]:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover);}
[data-pb-btn][data-primary]{background:var(--dsw-alias-button-primary-fill);
  border-color:var(--dsw-alias-button-primary-fill);color:var(--dsw-alias-label-primary-inverted);}
[data-pb-btn][data-primary]:hover:not(:disabled){background:var(--dsw-alias-button-primary-hover);}
[data-pb-btn][data-danger]{color:var(--dsw-alias-state-error-primary);
  border-color:var(--dsw-alias-state-error-primary);background:none;}
[data-pb-btn][data-danger]:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover-danger);}
[data-pb-btn]:disabled{opacity:.5;cursor:not-allowed;}

[data-pb-badge]{display:inline-flex;align-items:center;gap:4px;font-size:12.5px;line-height:1.5;
  padding:2px 8px;border-radius:999px;white-space:nowrap;background:var(--dsw-alias-bg-layer-2);
  color:var(--dsw-alias-label-secondary);border:1px solid var(--dsw-alias-border-l1);}
[data-pb-badge][data-tone="good"]{color:var(--dsw-alias-state-success-primary);}
[data-pb-badge][data-tone="warn"]{color:var(--dsw-alias-state-warn-primary);}
[data-pb-badge][data-tone="err"]{color:var(--dsw-alias-state-error-primary);}
[data-pb-badge][data-tone="info"]{color:var(--dsw-alias-brand-primary);}
[data-pb-badge][data-tone="muted"]{color:var(--dsw-alias-label-secondary);}
[data-pb-impacts]{display:flex;gap:4px;flex-wrap:wrap;align-items:center;}
[data-pb-secret-row]{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}

[data-pb-notice]{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;padding:10px 12px;
  border-radius:var(--dsw-radius-sm);font-size:14px;line-height:1.6;
  border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-2);
  color:var(--dsw-alias-label-primary);}
[data-pb-notice][data-tone="good"]{border-color:var(--dsw-alias-state-success-primary);}
[data-pb-notice][data-tone="warn"]{border-color:var(--dsw-alias-state-warn-primary);}
[data-pb-notice][data-tone="err"]{border-color:var(--dsw-alias-state-error-primary);}
[data-pb-notice-text]{flex:1;min-width:180px;}

[data-pb-action]{padding:10px 0;border-bottom:1px solid var(--dsw-alias-border-l1);
  display:flex;flex-direction:column;gap:6px;}
[data-pb-action]:last-child{border-bottom:0;}
[data-pb-action-title]{font-size:14px;font-weight:600;color:var(--dsw-alias-label-primary);}
[data-pb-action-row]{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
[data-pb-form]{display:flex;flex-direction:column;gap:10px;padding-top:4px;}
[data-pb-form-row]{display:flex;flex-direction:column;gap:6px;}
[data-pb-form-row] label,[data-pb-form-label]{font-size:14px;font-weight:600;color:var(--dsw-alias-label-primary);}

[data-pb-fold]{display:flex;align-items:center;gap:8px;background:none;border:0;padding:10px 0;
  font:inherit;font-size:14px;line-height:1.5;color:var(--dsw-alias-label-primary);cursor:pointer;
  width:100%;text-align:left;}
[data-pb-fold] .caret{transition:transform .12s;}
[data-pb-fold][data-open] .caret{transform:rotate(90deg);}
[data-pb-pre]{margin:8px 0 0;padding:10px 12px;border-radius:var(--dsw-radius-sm);max-height:360px;
  overflow:auto;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;
  font-family:ui-monospace,Consolas,monospace;background:var(--dsw-alias-bg-layer-1);
  border:1px solid var(--dsw-alias-border-l1);color:var(--dsw-alias-label-secondary);}

[data-pb-code]{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px;line-height:1.6;
  width:100%;box-sizing:border-box;padding:10px 12px;border-radius:var(--dsw-radius-sm);
  border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1);
  color:var(--dsw-alias-label-primary);resize:vertical;}
[data-pb-persona-editor]{min-height:420px;font-size:14px;line-height:1.7;}
[data-pb-persona-tools]{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 0;
  border-bottom:1px solid var(--dsw-alias-border-l1);}
[data-pb-count]{font-size:14px;color:var(--dsw-alias-label-secondary);}

[data-pb-modal]{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;
  padding:24px;background:var(--dsw-alias-bg-mask-1);}
[data-pb-modal-card]{width:min(600px,100%);max-height:85vh;overflow:auto;padding:18px 20px;
  border-radius:var(--dsw-radius-lg);background:var(--dsw-alias-bg-overlay);
  border:1px solid var(--dsw-alias-border-l2);box-shadow:var(--dsw-elevation-prominent);}
[data-pb-modal-title]{font-size:16px;font-weight:600;line-height:1.4;margin:0 0 6px;color:var(--dsw-alias-label-primary);}
[data-pb-modal-body]{font-size:14px;line-height:1.65;color:var(--dsw-alias-label-secondary);}
[data-pb-modal-body] ul{margin:8px 0 0;padding-left:20px;color:var(--dsw-alias-label-primary);}
[data-pb-modal-body] li{margin:2px 0;}
[data-pb-modal-actions]{display:flex;justify-content:flex-end;gap:8px;margin-top:16px;flex-wrap:wrap;}
[data-pb-changelist]{margin:8px 0 0;padding-left:20px;font-size:14px;line-height:1.65;color:var(--dsw-alias-label-primary);}
[data-pb-changelist] code{font-family:ui-monospace,Consolas,monospace;font-size:12px;
  color:var(--dsw-alias-label-secondary);}
[data-pb-empty]{padding:10px 0;}
[data-pb-search-summary]{font-size:14px;color:var(--dsw-alias-label-primary);}

/* ── 启动页：一键引导流程（hero / stepper / 日志窗 / 就绪卡） ─────────── */
[data-pb-hero]{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;
  padding:14px 16px;border:1px solid var(--dsw-alias-border-l1);border-radius:var(--dsw-radius-lg);
  background:var(--dsw-alias-settings-card-fill);}
[data-pb-hero-main]{flex:1 1 340px;min-width:0;}
[data-pb-hero-actions]{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
[data-pb-btn][data-hero]{padding:9px 18px;font-weight:600;}

[data-pb-stepper]{display:flex;flex-direction:column;}
[data-pb-step]{display:flex;gap:10px;align-items:flex-start;padding:10px 0;
  border-bottom:1px solid var(--dsw-alias-border-l1);}
[data-pb-step]:last-child{border-bottom:0;}
[data-pb-step-body]{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px;}
[data-pb-step-name]{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;
  font-size:14px;line-height:1.5;font-weight:600;color:var(--dsw-alias-label-primary);}
[data-pb-step-index]{font-size:14px;line-height:1.5;color:var(--dsw-alias-label-secondary);}
[data-pb-step-detail]{font-size:12.5px;line-height:1.6;color:var(--dsw-alias-label-secondary);
  word-break:break-word;}
[data-pb-step][data-state="fail"] [data-pb-step-detail]{color:var(--dsw-alias-state-error-primary);}
[data-pb-step-error]{display:flex;flex-direction:column;gap:6px;align-items:flex-start;margin-top:4px;}
[data-pb-step-dot]{flex:none;width:22px;height:22px;box-sizing:border-box;border-radius:50%;
  display:inline-flex;align-items:center;justify-content:center;font-size:12px;line-height:1;
  border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);
  color:var(--dsw-alias-label-secondary);}
[data-pb-step-dot][data-state="ok"]{border-color:var(--dsw-alias-state-success-primary);
  color:var(--dsw-alias-state-success-primary);}
[data-pb-step-dot][data-state="fail"]{border-color:var(--dsw-alias-state-error-primary);
  color:var(--dsw-alias-state-error-primary);}
[data-pb-step-dot][data-state="skip"]{border-style:dashed;color:var(--dsw-alias-label-tertiary);}
[data-pb-step-dot][data-state="running"]{background:transparent;}
[data-pb-step-dot][data-state="running"] i{display:block;width:11px;height:11px;border-radius:50%;
  border:2px solid var(--dsw-alias-border-l2);border-top-color:var(--dsw-alias-brand-primary);
  animation:pb-step-spin .9s linear infinite;}
@keyframes pb-step-spin{to{transform:rotate(360deg);}}
@media (prefers-reduced-motion: reduce){
  [data-pb-step-dot][data-state="running"] i{animation:none;}
}

[data-pb-flags]{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:2px 0 0;}
[data-pb-flag]{display:inline-flex;gap:4px;align-items:center;font-size:12.5px;line-height:1.5;
  padding:2px 8px;border-radius:999px;border:1px solid var(--dsw-alias-border-l1);
  background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-secondary);}
[data-pb-flag][data-tone="good"]{color:var(--dsw-alias-state-success-primary);
  border-color:var(--dsw-alias-state-success-primary);}
[data-pb-flag][data-tone="err"]{color:var(--dsw-alias-state-error-primary);
  border-color:var(--dsw-alias-state-error-primary);}

[data-pb-log]{margin-top:8px;max-height:210px;overflow:auto;padding:8px 10px;box-sizing:border-box;
  border-radius:var(--dsw-radius-sm);border:1px solid var(--dsw-alias-border-l1);
  background:var(--dsw-alias-bg-layer-1);font-family:ui-monospace,Consolas,monospace;
  font-size:12px;line-height:1.6;color:var(--dsw-alias-label-secondary);
  white-space:pre-wrap;word-break:break-word;}
[data-pb-log-line]{display:block;}
[data-pb-log-line][data-stream="stderr"]{color:var(--dsw-alias-state-error-primary);}

[data-pb-manual]{margin:8px 0 0;padding-left:22px;font-size:14px;line-height:1.65;
  color:var(--dsw-alias-label-primary);}
[data-pb-manual] li{margin:2px 0;word-break:break-word;}

[data-pb-token]{display:block;margin-top:6px;padding:6px 8px;box-sizing:border-box;
  font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:1.6;word-break:break-all;
  color:var(--dsw-alias-label-secondary);background:var(--dsw-alias-bg-layer-2);
  border:1px solid var(--dsw-alias-border-l1);border-radius:var(--dsw-radius-xs);}

[data-pb-btn][data-link]{display:inline-flex;align-items:center;text-decoration:none;}

[data-pb-ready]{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;
  padding:14px 16px;border-radius:var(--dsw-radius-lg);
  border:1px solid var(--dsw-alias-state-success-primary);
  background:var(--dsw-alias-bg-layer-2);
  background:color-mix(in srgb, var(--dsw-alias-state-success-primary) 14%, var(--dsw-alias-bg-layer-1));}
[data-pb-ready-text]{flex:1 1 260px;min-width:0;}
[data-pb-ready-title]{font-size:14px;line-height:1.5;font-weight:600;color:var(--dsw-alias-label-primary);}
[data-pb-ready-sub]{font-size:12.5px;line-height:1.6;color:var(--dsw-alias-label-secondary);}
[data-pb-ready-actions]{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}

@container (min-width:760px){
  [data-pb-fields]{grid-template-columns:repeat(2,minmax(0,1fr));}
}
@container (max-width:759px){
  [data-pb-config]{grid-template-columns:minmax(0,1fr);}
  [data-pb-nav]{position:static;max-height:none;}
  [data-pb-nav-list]{flex-direction:row;flex-wrap:wrap;}
  [data-pb-nav-btn]{width:auto;}
}
`

/* ═══════════════════════════════════════════════════════════════════════════
 * Client plugin：註冊 settings.section，掛上控制面板
 * ═══════════════════════════════════════════════════════════════════════════ */
if (typeof window === 'undefined') {
  // Node（test/client-meta-check.mjs）用：只暴露 metadata，不碰 DOM / React。
  globalThis.__PAWBOT_PANEL_META__ = PANEL_META
} else {
  window.__ModuleLoader__.load({
    id: '@local/pawbot',
    factory: (require) => {
      const React = require('react')
      const h = React.createElement
      const { useState, useEffect, useRef } = React

      /* ── 小元件 ─────────────────────────────────────────────────────────── */

      function Badge({ tone, children }) {
        return h('span', { 'data-pb-badge': '', 'data-tone': tone || undefined }, children)
      }

      function Impacts({ codes }) {
        const list = Array.isArray(codes) && codes.length ? codes : ['instant']
        return h('div', { 'data-pb-impacts': '' },
          list.map((code) => {
            const meta = IMPACTS[code] || { label: String(code), tone: 'muted' }
            return h('span', { key: code, 'data-pb-badge': '', 'data-tone': meta.tone }, meta.label)
          }))
      }

      function Switch({ on, onChange, label, disabled }) {
        return h('button', {
          type: 'button',
          role: 'switch',
          'aria-checked': Boolean(on),
          'aria-label': label || '开关',
          'data-pb-sw': '',
          'data-on': on ? '' : undefined,
          disabled: Boolean(disabled),
          onClick: () => {
            if (!disabled) onChange(!on)
          },
        }, h('i'))
      }

      function Notice({ tone, children, action }) {
        return h('div', {
          'data-pb-notice': '',
          'data-tone': tone || 'info',
          role: tone === 'err' ? 'alert' : 'status',
        }, h('div', { 'data-pb-notice-text': '' }, children), action || null)
      }

      function InfoRow({ label, value, tone }) {
        const text = value === null || value === undefined || value === '' ? '—' : value
        return h('div', { 'data-pb-info-row': '' },
          h('span', { 'data-pb-info-label': '' }, label),
          h('span', { 'data-pb-info-value': '', 'data-tone': tone || undefined }, text))
      }

      function Kpi({ label, value, sub, tone }) {
        return h('div', { 'data-pb-kpi': '', 'data-tone': tone || undefined },
          h('div', { 'data-pb-kpi-label': '' }, label),
          h('div', { 'data-pb-kpi-num': '' }, value),
          sub ? h('div', { 'data-pb-kpi-sub': '' }, sub) : null)
      }

      function ConfirmDialog({ open, title, children, confirmLabel, cancelLabel, danger, busy, onConfirm, onCancel }) {
        useEffect(() => {
          if (!open) return undefined
          const onKey = (event) => {
            if (event.key === 'Escape' && !busy) onCancel()
          }
          window.addEventListener('keydown', onKey)
          return () => window.removeEventListener('keydown', onKey)
        }, [open, busy, onCancel])
        if (!open) return null
        return h('div', { 'data-pb-modal': '', role: 'presentation' },
          h('div', {
            'data-pb-modal-card': '',
            role: 'dialog',
            'aria-modal': 'true',
            'aria-label': title,
          },
          h('h2', { 'data-pb-modal-title': '' }, title),
          h('div', { 'data-pb-modal-body': '' }, children),
          h('div', { 'data-pb-modal-actions': '' },
            h('button', {
              'data-pb-btn': '',
              type: 'button',
              disabled: Boolean(busy),
              onClick: onCancel,
            }, cancelLabel || '取消'),
            h('button', {
              'data-pb-btn': '',
              'data-primary': danger ? undefined : '',
              'data-danger': danger ? '' : undefined,
              type: 'button',
              disabled: Boolean(busy),
              onClick: onConfirm,
            }, busy ? '处理中…' : (confirmLabel || '确定')))))
      }

      function NoticeBlock({ notice, onGoStatus }) {
        if (!notice) return null
        const action = notice.restart && notice.restart.length && onGoStatus
          ? h('button', {
            'data-pb-btn': '',
            type: 'button',
            onClick: onGoStatus,
          }, '去状态页重启')
          : null
        return h(Notice, { tone: notice.tone, action },
          notice.text,
          notice.restart && notice.restart.length
            ? h('div', { 'data-pb-help': '' },
              '其中需要重启 PawBot 才生效：' +
              notice.restart.map((key) => (META_BY_KEY[key] && META_BY_KEY[key].label) || key).join('、') + '。')
            : null)
      }

      /* ── 启动页：一键引导（装环境 → 连 NapCat → 启动 PawBot → 就绪） ─────── */

      // 四步的固定顺序与简体名称。来源是 /pawbot-ui/boot/status 的 steps，
      // 但名称以面板这一份为准，避免主机文案与面板其它文字不一致。
      const BOOT_STEP_IDS = ['env', 'napcat', 'pawbot', 'ready']
      const BOOT_STEP_META = {
        env: { index: '①', name: '环境', hint: 'Python / venv / 依赖 / 设定' },
        napcat: { index: '②', name: 'NapCat', hint: '侦测与连线' },
        pawbot: { index: '③', name: 'PawBot 进程', hint: '拉起服务并等它回报' },
        ready: { index: '④', name: '就绪', hint: '等 /api/status 可达' },
      }
      const BOOT_STEP_STATE_TEXT = {
        pending: '待办',
        running: '进行中',
        ok: '完成',
        fail: '失败',
        skip: '跳过',
      }
      const BOOT_STEP_TONE = {
        pending: 'muted',
        running: 'info',
        ok: 'good',
        fail: 'err',
        skip: 'muted',
      }
      const NAPCAT_MODE_TEXT = {
        docker: '已用 Docker 启动',
        binary: '使用本机已安装的 NapCat',
        none: '这台机器没有 Docker，也没装 NapCat',
      }

      function bootStepState(value) {
        const key = String(value || '')
        return BOOT_STEP_STATE_TEXT[key] ? key : 'pending'
      }

      /** 把 /boot/status 的 steps 整理成固定的四行（缺的补成「待办」）。 */
      function normalizeBootSteps(bootSt) {
        const raw = bootSt && Array.isArray(bootSt.steps) ? bootSt.steps : []
        const known = Object.create(null)
        const extra = []
        for (const step of raw) {
          if (!step || typeof step !== 'object') continue
          const id = String(step.id || '')
          if (!id) continue
          if (BOOT_STEP_META[id]) known[id] = step
          else extra.push(step)
        }
        const rows = BOOT_STEP_IDS.map((id) => {
          const meta = BOOT_STEP_META[id]
          const step = known[id] || null
          const state = step ? bootStepState(step.state) : 'pending'
          const detail = step && step.detail ? String(step.detail) : ''
          return {
            key: id,
            index: meta.index,
            name: meta.name,
            state,
            detail: detail || (state === 'pending' ? meta.hint : ''),
          }
        })
        for (const step of extra) {
          const state = bootStepState(step.state)
          rows.push({
            key: 'extra-' + String(step.id),
            index: '',
            name: String(step.name || step.id || '其他步骤'),
            state,
            detail: step.detail ? String(step.detail) : (state === 'pending' ? '还没开始' : ''),
          })
        }
        return rows
      }

      /** 状态接口没回 qr 时保留上一次抓到的二维码，但登入后就不再显示旧的。 */
      function mergeNapcat(prev, data) {
        if (!data || typeof data !== 'object') return prev
        const next = { ...data }
        const prevQr = prev && prev.qr ? prev.qr : null
        if (!next.qr && prevQr && !next.loggedIn) next.qr = prevQr
        return next
      }

      function fileBaseName(value) {
        const text = String(value || '').replace(/\\/g, '/')
        const cut = text.lastIndexOf('/')
        return cut >= 0 ? text.slice(cut + 1) : text
      }

      function fileDirName(value) {
        const text = String(value || '').replace(/\\/g, '/')
        const cut = text.lastIndexOf('/')
        return cut > 0 ? text.slice(0, cut) : ''
      }

      /** 路径是否落在 appDir 之下（不分大小写、两种分隔符都接受）。 */
      function pathHasPrefix(file, dir) {
        const norm = (value) => String(value || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
        const f = norm(file)
        const d = norm(dir)
        if (!f || !d) return false
        return f === d || f.indexOf(d + '/') === 0
      }

      function lastLogLines(log, count) {
        if (!Array.isArray(log)) return []
        return log.filter((entry) => entry && typeof entry === 'object').slice(-count)
      }

      function isHttpUrl(value) {
        return /^https?:\/\//i.test(String(value || '').trim())
      }

      function SetupStepDot({ state }) {
        const label = BOOT_STEP_STATE_TEXT[state] || String(state || '')
        if (state === 'running') {
          return h('span', {
            'data-pb-step-dot': '',
            'data-state': 'running',
            role: 'img',
            'aria-label': label,
          }, h('i'))
        }
        const glyph = state === 'ok' ? '✓' : state === 'fail' ? '✕' : state === 'skip' ? '⊘' : '·'
        return h('span', {
          'data-pb-step-dot': '',
          'data-state': state,
          role: 'img',
          'aria-label': label,
        }, glyph)
      }

      function SetupStepRow({ row, onRetry, retrying }) {
        return h('div', { 'data-pb-step': '', 'data-state': row.state },
          h(SetupStepDot, { state: row.state }),
          h('div', { 'data-pb-step-body': '' },
            h('div', { 'data-pb-step-name': '' },
              row.index
                ? h('span', { 'data-pb-step-index': '', 'aria-hidden': 'true' }, row.index)
                : null,
              h('span', null, row.name),
              h('span', {
                'data-pb-badge': '',
                'data-tone': BOOT_STEP_TONE[row.state] || 'muted',
              }, BOOT_STEP_STATE_TEXT[row.state] || row.state)),
            row.detail ? h('div', { 'data-pb-step-detail': '' }, row.detail) : null,
            row.state === 'fail'
              ? h('div', { 'data-pb-step-error': '' },
                h('div', { 'data-pb-help': '' },
                  '这一步没有过。修好上面的原因后按下面的按钮，会把整套流程再跑一次（已经好的步骤会自动跳过）。'),
                h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  disabled: Boolean(retrying),
                  onClick: onRetry,
                }, retrying ? '重试中…' : '重试这一步'))
              : null))
      }

      function SetupEnvCard({ st, err, busy, installing, logRef, onRerun, onReload }) {
        const python = (st && st.python) || {}
        const venv = (st && st.venv) || {}
        const deps = (st && st.deps) || {}
        const conf = (st && st.config) || {}
        const appDir = String((st && st.appDir) || '')
        const pythonOk = Boolean(python.ok)
        const pythonText = pythonOk
          ? 'Python ' + (python.version ? String(python.version) : '（版本未知）')
          : '未找到 Python'
        const venvPath = String(venv.path || '')
        const confText = !conf.exists
          ? '还没有配置文件'
          : conf.valid ? '配置文件已就绪' : '配置文件存在，但内容不合法'
        const rows = lastLogLines(st && st.log, 10)

        return h('section', { 'data-pb-card': '' },
          h('div', { 'data-pb-card-head': '' },
            h('h2', { 'data-pb-card-title': '' }, '环境'),
            h('span', { 'data-pb-card-sub': '' }, 'Python、venv、依赖与设定；每 3 秒自动更新')),
          err
            ? h(Notice, {
              tone: 'err',
              action: h('button', { 'data-pb-btn': '', type: 'button', onClick: onReload }, '再读一次'),
            }, '读不到安装状态：' + err)
            : null,
          !st && !err ? h('div', { 'data-pb-help': '' }, '正在读取安装状态…') : null,
          st
            ? h(React.Fragment, null,
              h(InfoRow, { label: 'Python', value: pythonText, tone: pythonOk ? 'good' : 'err' }),
              h(InfoRow, {
                label: 'venv',
                value: (venvPath || '（主机没有回报路径）') + ' · ' + (venv.exists ? '已建立' : '还没建立'),
                tone: venv.exists ? 'good' : 'warn',
              }),
              h(InfoRow, {
                label: '设定档',
                value: confText,
                tone: conf.exists && conf.valid ? 'good' : 'err',
              }),
              appDir ? h(InfoRow, { label: '应用目录', value: appDir }) : null,
              !pythonOk
                ? h('div', { 'data-pb-help': '' },
                  '这台机器还没有可用的 Python。先装 python3（Windows 装 Python 3.x 并勾选 Add to PATH），装好后再按「只重跑环境安装」。')
                : null,
              st.appPresent === false
                ? h('div', { 'data-pb-help': '' },
                  '应用目录里还找不到 PawBot 的程序；一键启动会先把程序放好再继续。')
                : null,
              h('div', { 'data-pb-flags': '' },
                h('span', { 'data-pb-flag': '', 'data-tone': deps.pillow ? 'good' : 'err' },
                  (deps.pillow ? '✓ ' : '✕ ') + 'pillow'),
                h('span', { 'data-pb-flag': '', 'data-tone': deps.websockets ? 'good' : 'err' },
                  (deps.websockets ? '✓ ' : '✕ ') + 'websockets'),
                h('span', {
                  'data-pb-flag': '',
                  'data-tone': st.appPresent === false ? 'err' : 'good',
                }, (st.appPresent === false ? '✕ ' : '✓ ') + 'PawBot 程序')),
              h('div', { 'data-pb-action-row': '' },
                h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  disabled: Boolean(busy),
                  onClick: onRerun,
                }, busy ? '送出中…' : '只重跑环境安装'),
                h('span', { 'data-pb-help': '' },
                  '只重装 venv 与依赖，不会动设定，也不会重启 PawBot。')),
              installing
                ? h('div', null,
                  h('div', { 'data-pb-card-sub': '' },
                    (st.message ? '安装进度：' + String(st.message) : '正在安装…') +
                    '（下面是日志最后 10 行，会自动滚动）'),
                  h('div', {
                    'data-pb-log': '',
                    ref: logRef,
                    role: 'log',
                    'aria-live': 'polite',
                  },
                  rows.length
                    ? rows.map((entry, index) => h('span', {
                      key: index,
                      'data-pb-log-line': '',
                      'data-stream': String(entry.stream || 'stdout'),
                    }, entry.line === undefined || entry.line === null ? '' : String(entry.line)))
                    : h('span', { 'data-pb-log-line': '' }, '（还没有日志）')))
                : null)
            : null)
      }

      function SetupNapCatCard({
        st, appDir, err, busy, manual, notice, onSetup, onQr, onReload,
      }) {
        const [tokenOpen, setTokenOpen] = useState(false)
        const [copyHint, setCopyHint] = useState('')
        const mode = st && (st.mode === 'docker' || st.mode === 'binary' || st.mode === 'none')
          ? st.mode
          : 'none'
        // 上一次「重新侦测」的提示只在模式没变时显示，避免和最新状态互相矛盾。
        const noticeText = notice && notice.text && (!notice.mode || notice.mode === mode)
          ? String(notice.text)
          : ''
        const webui = (st && st.webui) || null
        const qr = (st && st.qr) || null
        const qrUrl = qr && qr.kind === 'url' ? String(qr.value || '') : ''
        const qrFile = qr && qr.kind === 'file' ? String(qr.value || '') : ''
        const qrInAppDir = qrFile ? pathHasPrefix(qrFile, appDir) : false
        const manualSteps = Array.isArray(manual) ? manual.filter((step) => String(step || '').trim()) : []

        const copySteps = async () => {
          const text = manualSteps.join('\n')
          try {
            if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
              await navigator.clipboard.writeText(text)
              setCopyHint('已复制到剪贴板。')
            } else {
              setCopyHint('这个浏览器不给用剪贴板；请直接选取上面的步骤复制。')
            }
          } catch (error) {
            setCopyHint('复制失败：' + String((error && error.message) || error) +
              '；请直接选取上面的步骤复制。')
          }
        }

        return h('section', { 'data-pb-card': '' },
          h('div', { 'data-pb-card-head': '' },
            h('h2', { 'data-pb-card-title': '' }, 'NapCat'),
            h('span', { 'data-pb-card-sub': '' }, '侦测与连线；NapCat 登入后 PawBot 才收得到 QQ 消息')),
          err
            ? h(Notice, {
              tone: 'err',
              action: h('button', { 'data-pb-btn': '', type: 'button', onClick: onReload }, '再读一次'),
            }, '读不到 NapCat 状态：' + err)
            : null,
          noticeText ? h(Notice, { tone: 'info' }, noticeText) : null,
          !st && !err ? h('div', { 'data-pb-help': '' }, '正在侦测 NapCat…') : null,
          st
            ? h(React.Fragment, null,
              h(InfoRow, {
                label: '运行方式',
                value: NAPCAT_MODE_TEXT[mode] || String(mode),
                tone: mode === 'none' ? 'err' : 'good',
              }),
              mode !== 'none'
                ? h(InfoRow, {
                  label: '进程状态',
                  value: st.running ? '正在运行' : '没有在运行',
                  tone: st.running ? 'good' : 'warn',
                })
                : null,
              h(InfoRow, {
                label: '登入状态',
                value: st.loggedIn
                  ? '已登录，NapCat 已连上 PawBot'
                  : (mode === 'none' ? '还没装 NapCat' : '还没登录（用手机 QQ 扫码）'),
                tone: st.loggedIn ? 'good' : 'warn',
              }),
              h('div', { 'data-pb-flags': '' },
                st.loggedIn
                  ? h('span', { 'data-pb-flag': '', 'data-tone': 'good' },
                    '✓ 已登录，NapCat 已连上 PawBot')
                  : h('span', { 'data-pb-flag': '' }, '· 还没登录'),
                mode === 'docker' ? h('span', { 'data-pb-flag': '', 'data-tone': 'good' }, 'Docker 模式') : null,
                mode === 'binary' ? h('span', { 'data-pb-flag': '', 'data-tone': 'good' }, '本机 NapCat') : null,
                mode === 'none' ? h('span', { 'data-pb-flag': '', 'data-tone': 'err' }, '✕ 没有 NapCat') : null),
              mode === 'none'
                ? h('div', null,
                  h(Notice, { tone: 'err' },
                    '这台机器没有 Docker，也没装 NapCat：一键启动会在第 ② 步停下，' +
                    'PawBot 就算起来了也收不到 QQ 消息。'),
                  manualSteps.length
                    ? h(React.Fragment, null,
                      h('ol', { 'data-pb-manual': '' },
                        manualSteps.map((step, index) => h('li', { key: index }, String(step)))),
                      h('div', { 'data-pb-action-row': '' },
                        h('button', {
                          'data-pb-btn': '',
                          type: 'button',
                          onClick: copySteps,
                        }, '复制这些步骤'),
                        copyHint ? h('span', { 'data-pb-help': '' }, copyHint) : null))
                    : h('div', { 'data-pb-help': '' },
                      '按下面的「重新侦测 / 重连 NapCat」，主机就会把这台机器要手动做的步骤列在这里。'))
                : null,
              webui && webui.url
                ? h('div', { 'data-pb-action': '' },
                  h('div', { 'data-pb-action-title': '' }, 'NapCat 登录页'),
                  h('div', { 'data-pb-help': '' },
                    '用要当机器人的 QQ 手机端扫码；token 已自动带在网址里，所以这个链接本身就是凭证，别转给别人。'),
                  h('div', { 'data-pb-action-row': '' },
                    isHttpUrl(webui.url)
                      ? h('a', {
                        'data-pb-btn': '',
                        'data-primary': '',
                        'data-link': '',
                        href: String(webui.url),
                        target: '_blank',
                        rel: 'noopener noreferrer',
                      }, '打开 NapCat 登录页')
                      : h('code', { 'data-pb-token': '' }, String(webui.url)),
                    h('button', {
                      'data-pb-btn': '',
                      type: 'button',
                      'aria-expanded': Boolean(tokenOpen),
                      onClick: () => setTokenOpen((value) => !value),
                    }, tokenOpen ? '收起 token' : '查看 token')),
                  tokenOpen
                    ? h('code', { 'data-pb-token': '' },
                      String(webui.token || '（主机没有回传 token）'))
                    : null)
                : null,
              qrUrl
                ? h('div', { 'data-pb-action': '' },
                  h('div', { 'data-pb-action-title': '' }, '登录二维码'),
                  h('div', { 'data-pb-help': '' }, '二维码是抓取当次有效的；过期就按「重新抓二维码」。'),
                  h('div', { 'data-pb-action-row': '' },
                    isHttpUrl(qrUrl)
                      ? h('a', {
                        'data-pb-btn': '',
                        'data-link': '',
                        href: qrUrl,
                        target: '_blank',
                        rel: 'noopener noreferrer',
                      }, '打开二维码')
                      : h('code', { 'data-pb-token': '' }, qrUrl)))
                : null,
              qrFile
                ? h('div', { 'data-pb-action': '' },
                  h('div', { 'data-pb-action-title': '' }, '登录二维码'),
                  qrInAppDir
                    ? h(React.Fragment, null,
                      h('div', { 'data-pb-help': '' }, '二维码已生成：' + fileBaseName(qrFile)),
                      h('div', { 'data-pb-help': '' },
                        '文件在 ' + fileDirName(qrFile) + ' 下面；用文件管理器打开就看得到' +
                        '（面板不读取图片文件，所以这里只给路径）。'))
                    : h('div', { 'data-pb-help': '' },
                      '二维码图片在 ' + qrFile + '；用文件管理器打开即可（面板不读取图片文件）。'))
                : null,
              st.hint ? h('div', { 'data-pb-help': '' }, String(st.hint)) : null,
              h('div', { 'data-pb-action-row': '' },
                h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  disabled: busy === 'setup',
                  onClick: onSetup,
                }, busy === 'setup' ? '侦测中…' : '重新侦测 / 重连 NapCat'),
                h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  disabled: busy === 'qr',
                  onClick: onQr,
                }, busy === 'qr' ? '抓取中…' : '重新抓二维码'),
                h('span', { 'data-pb-help': '' },
                  '侦测只读本机状态；重连会请 PawBot 再连一次 NapCat。')))
            : null)
      }

      function SetupTab({ active, onGoStatus, onGoConfig }) {
        const [setupSt, setSetupSt] = useState(null)
        const [setupErr, setSetupErr] = useState('')
        const [setupTick, setSetupTick] = useState(0)
        const [setupBusy, setSetupBusy] = useState(false)
        const [bootSt, setBootSt] = useState(null)
        const [bootErr, setBootErr] = useState('')
        const [bootTick, setBootTick] = useState(0)
        const [bootArmed, setBootArmed] = useState(false)
        const [bootRequesting, setBootRequesting] = useState(false)
        const [napcat, setNapcat] = useState(null)
        const [napcatErr, setNapcatErr] = useState('')
        const [napcatTick, setNapcatTick] = useState(0)
        const [napcatBusy, setNapcatBusy] = useState('')
        const [napcatNotice, setNapcatNotice] = useState(null)
        const [manual, setManual] = useState([])
        const [actErr, setActErr] = useState('')
        const [actInfo, setActInfo] = useState('')
        const logRef = useRef(null)
        const actCtrl = useRef(null)

        // 所有 POST 共用同一个 controller：离开这个分页就整批取消。
        useEffect(() => {
          const ctrl = new AbortController()
          actCtrl.current = ctrl
          return () => ctrl.abort()
        }, [])

        // 环境状态：只有「启动」页可见时才轮询（3 秒）。
        useEffect(() => {
          if (!active) return undefined
          let alive = true
          const ctrl = new AbortController()
          const tick = async () => {
            try {
              const data = await api('GET', '/setup/status', undefined, ctrl.signal)
              if (!alive) return
              setSetupSt(data)
              setSetupErr('')
            } catch (error) {
              if (alive && !isAbortError(error)) setSetupErr(friendlyError(error))
            }
          }
          tick()
          const timer = setInterval(tick, 3000)
          return () => {
            alive = false
            clearInterval(timer)
            ctrl.abort()
          }
        }, [active, setupTick])

        // NapCat 状态：同一个分页可见时每 3 秒更新，登入状态才会自己变绿。
        useEffect(() => {
          if (!active) return undefined
          let alive = true
          const ctrl = new AbortController()
          const tick = async () => {
            try {
              const data = await api('GET', '/napcat/status', undefined, ctrl.signal)
              if (!alive) return
              setNapcat((prev) => mergeNapcat(prev, data))
              setNapcatErr('')
            } catch (error) {
              if (alive && !isAbortError(error)) setNapcatErr(friendlyError(error))
            }
          }
          tick()
          const timer = setInterval(tick, 3000)
          return () => {
            alive = false
            clearInterval(timer)
            ctrl.abort()
          }
        }, [active, napcatTick])

        // 四步进度：每 1.5 秒轮询，done:true 就停。
        useEffect(() => {
          if (!active) return undefined
          let alive = true
          let timer = null
          const ctrl = new AbortController()
          const stop = () => {
            if (timer !== null) {
              clearInterval(timer)
              timer = null
            }
          }
          const tick = async () => {
            try {
              const data = await api('GET', '/boot/status', undefined, ctrl.signal)
              if (!alive) return
              setBootSt(data)
              setBootErr('')
              const steps = data && Array.isArray(data.steps) ? data.steps : []
              // 只要没有任何一步在跑，就认为这次「刚送出」的状态已结算：
              // 避免请求被中断时按钮一直卡在 disabled。
              const running = steps.some((step) => step && step.state === 'running')
              if (!running) setBootArmed(false)
              if (data && data.done) stop()
            } catch (error) {
              if (alive && !isAbortError(error)) {
                setBootErr(friendlyError(error))
                setBootArmed(false)
              }
            }
          }
          tick()
          timer = setInterval(tick, 1500)
          return () => {
            alive = false
            stop()
            ctrl.abort()
          }
        }, [active, bootTick])

        const setupPhase = setupSt && setupSt.phase ? String(setupSt.phase) : ''
        const installing = setupPhase === 'installing'
        const logLength = setupSt && Array.isArray(setupSt.log) ? setupSt.log.length : 0

        // 日志窗自动滚到底：像终端一样，永远看得到最新一行。
        useEffect(() => {
          const el = logRef.current
          if (el) el.scrollTop = el.scrollHeight
        }, [logLength, setupPhase])

        const refreshSetup = () => setSetupTick((value) => value + 1)
        const refreshNapcat = () => setNapcatTick((value) => value + 1)
        const refreshBoot = () => setBootTick((value) => value + 1)

        const runBoot = async () => {
          setBootRequesting(true)
          setActErr('')
          setActInfo('')
          try {
            await api('POST', '/boot', {}, actCtrl.current && actCtrl.current.signal)
            setBootArmed(true)
            setBootSt((prev) => (prev ? { ...prev, done: false } : prev))
            refreshBoot()
            refreshSetup()
            refreshNapcat()
          } catch (error) {
            if (!isAbortError(error)) {
              setActErr('一键启动没跑起来：' + friendlyError(error) + '（可以再按一次）')
            }
          } finally {
            setBootRequesting(false)
          }
        }

        const runSetupInstall = async () => {
          setSetupBusy(true)
          setActErr('')
          setActInfo('')
          try {
            const result = await api('POST', '/setup/run', { force: false }, actCtrl.current && actCtrl.current.signal)
            setActInfo(result && result.started
              ? '已开始重跑环境安装；进度会自动更新，日志会出现在下面的日志窗。'
              : '已送出环境安装请求；进度会自动更新。')
            refreshSetup()
          } catch (error) {
            if (!isAbortError(error)) setActErr('环境安装没跑起来：' + friendlyError(error))
          } finally {
            setSetupBusy(false)
          }
        }

        const setupNapcat = async () => {
          setNapcatBusy('setup')
          setActErr('')
          setActInfo('')
          setNapcatNotice(null)
          try {
            const result = await api('POST', '/napcat/setup', {}, actCtrl.current && actCtrl.current.signal)
            const steps = result && result.manual && Array.isArray(result.manual.steps)
              ? result.manual.steps.map((step) => String(step))
              : []
            if (steps.length) setManual(steps)
            const bits = []
            if (result && result.mode) {
              bits.push('侦测结果：' + (NAPCAT_MODE_TEXT[result.mode] || String(result.mode)))
            }
            if (result && result.hint) bits.push(String(result.hint))
            setNapcatNotice({
              mode: result && result.mode ? String(result.mode) : '',
              text: bits.join('；') || '已重新侦测一次。',
            })
            refreshNapcat()
            refreshBoot()
          } catch (error) {
            if (!isAbortError(error)) setActErr('NapCat 重连失败：' + friendlyError(error))
          } finally {
            setNapcatBusy('')
          }
        }

        const fetchQr = async () => {
          setNapcatBusy('qr')
          setActErr('')
          setActInfo('')
          setNapcatNotice(null)
          try {
            const result = await api('POST', '/napcat/qr', {}, actCtrl.current && actCtrl.current.signal)
            const qr = result && result.qr ? result.qr : null
            const webui = result && result.webui ? result.webui : undefined
            setNapcat((prev) => {
              const base = prev && typeof prev === 'object' ? prev : {}
              return {
                ...base,
                qr: qr || base.qr || null,
                webui: webui === undefined ? base.webui : webui,
              }
            })
            setNapcatNotice({
              text: qr
                ? '已重新抓一次二维码。'
                : '主机没有回传二维码；可能是 NapCat 没在跑，或已经登录了。',
            })
            refreshNapcat()
          } catch (error) {
            if (!isAbortError(error)) setActErr('抓二维码失败：' + friendlyError(error))
          } finally {
            setNapcatBusy('')
          }
        }

        const bootRows = normalizeBootSteps(bootSt)
        const bootRunning = bootRows.some((row) => row.state === 'running')
        const bootFailed = bootRows.some((row) => row.state === 'fail')
        const bootDone = Boolean(bootSt && bootSt.done)
        const bootBusy = bootRequesting || bootRunning || bootArmed
        const readyRow = bootRows.find((row) => row.key === 'ready') || null
        const showReady = Boolean(readyRow && readyRow.state === 'ok' && napcat && napcat.loggedIn === true)

        return h(React.Fragment, null,
          h('div', { 'data-pb-hero': '' },
            h('div', { 'data-pb-hero-main': '' },
              h('h1', { 'data-pb-title': '' }, '一键把 PawBot 跑起来'),
              h('p', { 'data-pb-sub': '' }, '装环境 → 连 NapCat → 启动 PawBot，三步自动完成'),
              h('div', { 'data-pb-help': '' }, bootBusy
                ? '正在跑，进度会自动更新；这一步不用一直盯着。'
                : bootFailed
                  ? '有步骤失败了；修好原因后按右边的「一键启动」，或该步骤的「重试这一步」再跑一次。'
                  : '按右边的主按钮会从第 ① 步一路跑到第 ④ 步；也可以用下面各张卡的按钮单独重跑某一步。')),
            h('div', { 'data-pb-hero-actions': '' },
              h('button', {
                'data-pb-btn': '',
                'data-primary': '',
                'data-hero': '',
                type: 'button',
                disabled: bootBusy,
                'aria-busy': bootBusy,
                onClick: runBoot,
              }, bootBusy ? '执行中…' : '一键启动'))),

          actErr ? h(Notice, { tone: 'err' }, actErr) : null,
          actInfo ? h(Notice, { tone: 'good' }, actInfo) : null,

          h('section', { 'data-pb-card': '' },
            h('div', { 'data-pb-card-head': '' },
              h('h2', { 'data-pb-card-title': '' }, '四步进度'),
              h('span', { 'data-pb-card-sub': '' }, bootDone
                ? '四步都跑完了，已经停止轮询'
                : '来自 /pawbot-ui/boot/status，每 1.5 秒自动更新；done 为 true 就停')),
            bootErr
              ? h(Notice, {
                tone: 'err',
                action: h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  onClick: refreshBoot,
                }, '再读一次'),
              }, '读不到启动进度：' + bootErr)
              : null,
            !bootSt && !bootErr ? h('div', { 'data-pb-help': '' }, '正在读取启动进度…') : null,
            bootSt
              ? h('div', { 'data-pb-stepper': '' },
                bootRows.map((row) => h(SetupStepRow, {
                  key: row.key,
                  row,
                  retrying: bootBusy,
                  onRetry: runBoot,
                })))
              : null,
            bootSt && !bootDone && !bootBusy
              ? h('div', { 'data-pb-help': '' },
                '目前没有在跑。按上面的「一键启动」会整套重跑；也可以用下面卡片上的按钮单独重跑一步。')
              : null),

          h(SetupEnvCard, {
            st: setupSt,
            err: setupErr,
            busy: setupBusy,
            installing,
            logRef,
            onRerun: runSetupInstall,
            onReload: refreshSetup,
          }),

          h(SetupNapCatCard, {
            st: napcat,
            appDir: setupSt && setupSt.appDir ? String(setupSt.appDir) : '',
            err: napcatErr,
            busy: napcatBusy,
            manual,
            notice: napcatNotice,
            onSetup: setupNapcat,
            onQr: fetchQr,
            onReload: refreshNapcat,
          }),

          showReady
            ? h('div', { 'data-pb-ready': '', role: 'status' },
              h('div', { 'data-pb-ready-text': '' },
                h('div', { 'data-pb-ready-title': '' }, '✓ 都好了 — 去群里说句话试试'),
                h('div', { 'data-pb-ready-sub': '' },
                  '环境、NapCat 与 PawBot 进程都好了；去群里 @ 她一下，或到状态页确认连接数。')),
              h('div', { 'data-pb-ready-actions': '' },
                h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  onClick: onGoStatus,
                }, '去状态页'),
                h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  onClick: onGoConfig,
                }, '去设定页')))
            : null)
      }

      /* ── 狀態頁 ─────────────────────────────────────────────────────────── */

      function describeConnection(value) {
        if (value === null || value === undefined) return '一条连接'
        if (typeof value === 'string' || typeof value === 'number') return String(value)
        if (typeof value !== 'object') return String(value)
        const bits = []
        if (value.self_id !== undefined) bits.push('QQ ' + value.self_id)
        if (value.nickname) bits.push(String(value.nickname))
        if (value.online === true) bits.push('在线')
        if (value.online === false) bits.push('离线')
        if (value.connected_at) bits.push('连上于 ' + (fmtTime(value.connected_at) || value.connected_at))
        return bits.length ? bits.join('・') : '一条连接'
      }

      function humanMood(mood) {
        if (mood === null || mood === undefined) return ''
        if (typeof mood === 'string' || typeof mood === 'number') return String(mood)
        if (typeof mood !== 'object') return String(mood)
        const label = mood.label || mood.mood || mood.name || ''
        const score = Number(mood.score)
        const bits = []
        if (label) bits.push(String(label))
        if (Number.isFinite(score)) bits.push(score + ' 分')
        return bits.join('・')
      }

      function humanEmotion(emotion) {
        if (emotion === null || emotion === undefined) return ''
        if (typeof emotion === 'string') return emotion
        if (typeof emotion !== 'object') return String(emotion)
        if (emotion.dominant) return String(emotion.dominant)
        if (isPlainObject(emotion.values)) {
          const top = Object.keys(emotion.values)
            .map((key) => [key, Number(emotion.values[key])])
            .filter((pair) => Number.isFinite(pair[1]))
            .sort((a, b) => b[1] - a[1])[0]
          if (top) return top[0] + '（' + top[1] + '）'
        }
        return ''
      }

      function humanLibido(libido) {
        if (libido === null || libido === undefined) return ''
        if (typeof libido === 'string' || typeof libido === 'number') return String(libido)
        if (typeof libido !== 'object') return String(libido)
        const label = libido.label || libido.name || ''
        const value = Number(libido.value)
        const bits = []
        if (label) bits.push(String(label))
        if (Number.isFinite(value)) bits.push('数值 ' + value)
        return bits.join('・')
      }

      function lastExitText(lastExit) {
        if (!lastExit || typeof lastExit !== 'object') return '—'
        const bits = []
        if (lastExit.code !== null && lastExit.code !== undefined) bits.push('代码 ' + lastExit.code)
        if (lastExit.signal) bits.push('信号 ' + lastExit.signal)
        if (lastExit.at) bits.push(fmtTime(lastExit.at) || String(lastExit.at))
        return bits.length ? bits.join('・') : '—'
      }

      function todayWatchCount(pawbot) {
        if (!pawbot || typeof pawbot !== 'object') return null
        const candidates = [
          pawbot.bili_today,
          pawbot.today_watch,
          pawbot.watch_today,
          pawbot.stats && pawbot.stats.bili_today,
          pawbot.stats && pawbot.stats.today_watch,
        ]
        for (const value of candidates) {
          if (typeof value === 'number' && Number.isFinite(value)) return value
        }
        return null
      }

      function StatusTab({ onGoConfig, active }) {
        const [st, setSt] = useState(null)
        const [err, setErr] = useState('')
        const [notice, setNotice] = useState(null)
        const [showRaw, setShowRaw] = useState(false)
        const [restartOpen, setRestartOpen] = useState(false)
        const [reloadOpen, setReloadOpen] = useState(false)
        const [sendOpen, setSendOpen] = useState(false)
        const [sendConfirm, setSendConfirm] = useState(false)
        const [send, setSend] = useState({ target: '', message: '', isGroup: true })
        const [busy, setBusy] = useState('')
        const [refreshing, setRefreshing] = useState(false)
        const ctrlRef = useRef(null)

        useEffect(() => {
          if (!active) return undefined
          let alive = true
          const ctrl = new AbortController()
          ctrlRef.current = ctrl
          const tick = async () => {
            try {
              const data = await api('GET', '/status', undefined, ctrl.signal)
              if (alive) {
                setSt(data)
                setErr('')
              }
            } catch (error) {
              if (alive && !isAbortError(error)) setErr(friendlyError(error))
            }
          }
          tick()
          // 每 5 秒自動刷新；離開分頁（active=false 或卸載）就清掉 interval 並取消請求。
          const timer = setInterval(tick, 5000)
          return () => {
            alive = false
            clearInterval(timer)
            ctrl.abort()
          }
        }, [active])

        const refresh = async () => {
          setRefreshing(true)
          try {
            const data = await api('GET', '/status', undefined, ctrlRef.current && ctrlRef.current.signal)
            setSt(data)
            setErr('')
          } catch (error) {
            if (!isAbortError(error)) setErr(friendlyError(error))
          } finally {
            setRefreshing(false)
          }
        }

        const runAction = async (kind, path, body, successText) => {
          setBusy(kind)
          setNotice(null)
          try {
            const result = await api('POST', path, body || {}, ctrlRef.current && ctrlRef.current.signal)
            setNotice({ tone: 'good', text: successText(result) })
            return result
          } catch (error) {
            if (!isAbortError(error)) setNotice({ tone: 'err', text: '操作失败：' + friendlyError(error) })
            return null
          } finally {
            setBusy('')
          }
        }

        const doRestart = async () => {
          const result = await runAction('restart', '/restart', {}, (res) => (
            '重启指令已送出' + (res && res.pid ? '，新 PID ' + res.pid : '') +
            '；请给它几秒重新连上 NapCat。'
          ))
          setRestartOpen(false)
          if (result) await refresh()
        }

        const doReload = async () => {
          setReloadOpen(false)
          await runAction('reload', '/plugins/reload', {}, () => (
            '已请 PawBot 热重载插件；进程没有重启，但外挂状态会重置，正在生成的回复可能受影响。'
          ))
        }

        const requestSend = () => {
          if (!send.target.trim()) {
            setNotice({ tone: 'err', text: '请先填目标群号或 QQ 号，才知道要发到哪里。' })
            return
          }
          if (!send.message.trim()) {
            setNotice({ tone: 'err', text: '请先输入要发送的内容。' })
            return
          }
          setSendConfirm(true)
        }

        const doSend = async () => {
          const target = send.target.trim()
          const isGroup = send.isGroup
          const result = await runAction('send', '/send', {
            target,
            message: send.message,
            is_group: isGroup,
          }, (res) => (
            '已真的用机器人账号发出（' + (isGroup ? '群' : '私聊') + ' ' + target + '）' +
            (res && res.note ? '；' + res.note : '。')
          ))
          setSendConfirm(false)
          if (result) setSend((prev) => ({ ...prev, message: '' }))
        }

        const proc = (st && st.process) || {}
        const running = Boolean(st && (st.running !== undefined ? st.running : proc.running))
        const uptimeSource = st && st.uptime_sec !== undefined ? st.uptime_sec : (proc && proc.uptime_sec)
        const uptime = Number(uptimeSource) || 0
        const pawbot = (st && st.pawbot) || null
        const httpOk = Boolean(st && (st.http_ok !== undefined ? st.http_ok : st.webui_reachable))
        const connections = pawbot && Array.isArray(pawbot.connections) ? pawbot.connections : []
        const watch = todayWatchCount(pawbot)

        const kpis = [
          {
            label: '运行状态',
            value: running ? '运行中' : '已停止',
            sub: running ? 'PID ' + (proc.pid || st && st.pid || '—') : ((st && st.reason) || 'PawBot 进程没在跑'),
            tone: running ? 'good' : 'err',
          },
          {
            label: '已运行',
            value: running ? fmtDuration(uptime) : '—',
            sub: running ? '从这次启动算起' : '启动后才有数字',
          },
          {
            label: '连接数',
            value: pawbot ? fmtCount(pawbot.connections) : '—',
            sub: 'NapCat / WebSocket 连接',
            tone: pawbot && connections.length === 0 ? 'warn' : undefined,
          },
        ]
        if (watch !== null) {
          kpis.push({
            label: '今日看片',
            value: String(watch),
            sub: 'B 站外接插件回报',
          })
        }

        const rows = []
        rows.push(h(InfoRow, {
          key: 'proc',
          label: 'PawBot 进程',
          value: running ? '正在运行' : '没有在跑',
          tone: running ? 'good' : 'err',
        }))
        rows.push(h(InfoRow, {
          key: 'webui',
          label: 'WebUI 通道',
          value: httpOk ? '可连接' : '连不上',
          tone: httpOk ? 'good' : 'warn',
        }))
        if (pawbot) {
          if (connections.length) {
            connections.slice(0, 6).forEach((connection, index) => {
              rows.push(h(InfoRow, {
                key: 'conn-' + index,
                label: '连接 ' + (index + 1),
                value: describeConnection(connection),
              }))
            })
            if (connections.length > 6) {
              rows.push(h(InfoRow, {
                key: 'conn-more',
                label: '其他连接',
                value: '还有 ' + (connections.length - 6) + ' 条',
              }))
            }
          } else {
            rows.push(h(InfoRow, {
              key: 'conn-none',
              label: 'NapCat 连接',
              value: '目前没有连接（NapCat 可能没开或正在重连）',
              tone: 'warn',
            }))
          }
          rows.push(h(InfoRow, { key: 'sessions', label: '会话数', value: fmtCount(pawbot.sessions) }))
          rows.push(h(InfoRow, { key: 'plugins', label: '已加载插件', value: fmtCount(pawbot.plugins) }))
          rows.push(h(InfoRow, { key: 'handlers', label: '指令处理器', value: fmtCount(pawbot.handlers) }))
          if (pawbot.mood) {
            rows.push(h(InfoRow, { key: 'mood', label: '目前心情', value: humanMood(pawbot.mood) }))
          }
          const emotion = humanEmotion(pawbot.emotion)
          if (emotion) rows.push(h(InfoRow, { key: 'emotion', label: '情绪主轴', value: emotion }))
          const libido = humanLibido(pawbot.libido)
          if (libido) rows.push(h(InfoRow, { key: 'libido', label: '亲密度', value: libido }))
          if (pawbot.llm_model) {
            rows.push(h(InfoRow, { key: 'llm', label: '回复模型', value: String(pawbot.llm_model) }))
          }
          if (pawbot.stats && typeof pawbot.stats === 'object') {
            rows.push(h(InfoRow, {
              key: 'stats',
              label: '消息统计',
              value: '收 ' + (pawbot.stats.recv !== undefined ? pawbot.stats.recv : 0) +
                '／送 ' + (pawbot.stats.sent !== undefined ? pawbot.stats.sent : 0) +
                '／错误 ' + (pawbot.stats.errors !== undefined ? pawbot.stats.errors : 0),
            }))
          }
          if (Array.isArray(pawbot.proactive)) {
            rows.push(h(InfoRow, {
              key: 'proactive',
              label: '主动观察中的群',
              value: pawbot.proactive.length + ' 群',
            }))
          }
        } else {
          rows.push(h(InfoRow, {
            key: 'no-pawbot',
            label: '连接与会话',
            value: 'PawBot WebUI 没有响应，所以拿不到连接、会话与心情数据。',
            tone: 'warn',
          }))
        }
        if (st && st.last_error) {
          rows.push(h(InfoRow, { key: 'last-error', label: '最后错误', value: String(st.last_error), tone: 'err' }))
        }
        if (st && st.last_exit) {
          rows.push(h(InfoRow, { key: 'last-exit', label: '上次退出', value: lastExitText(st.last_exit) }))
        }
        if (proc.project_dir) {
          rows.push(h(InfoRow, { key: 'dir', label: '项目目录', value: String(proc.project_dir) }))
        }

        const loading = !st && !err

        return h(React.Fragment, null,
          h('div', { 'data-pb-top': '' },
            h('div', null,
              h('h1', { 'data-pb-title': '' }, 'PawBot 状态'),
              h('p', { 'data-pb-sub': '' }, '每 5 秒自动更新；离开这个标签页就停止，不会在后台一直请求主机。')),
            h('div', { 'data-pb-action-row': '' },
              h('button', {
                'data-pb-btn': '',
                type: 'button',
                disabled: refreshing,
                onClick: refresh,
              }, refreshing ? '更新中…' : '立即刷新'),
              h('span', { 'data-pb-help': '' }, '只重新抓一次状态，不会改任何设置'))),

          err ? h(Notice, { tone: 'err', action: h('button', {
            'data-pb-btn': '',
            type: 'button',
            onClick: refresh,
          }, '再试一次') }, '读不到状态：' + err) : null,

          loading
            ? h('div', { 'data-pb-card': '' }, h('div', { 'data-pb-help': '' }, '正在读取 PawBot 状态…'))
            : h(React.Fragment, null,
              h('div', { 'data-pb-kpis': '' }, kpis.map((kpi) => h(Kpi, { key: kpi.label, ...kpi }))),

              !running
                ? h(Notice, { tone: 'warn' }, 'PawBot 目前没有在跑。可以到下面的「快捷操作」按重启；它会重新拉起进程，但 NapCat 连接要等它重连。')
                : null,

              h('div', { 'data-pb-stack': '' },
                h('section', { 'data-pb-card': '' },
                  h('div', { 'data-pb-card-head': '' },
                    h('h2', { 'data-pb-card-title': '' }, '连接与会话'),
                    h('span', { 'data-pb-card-sub': '' }, '把那包 JSON 翻成人话')),
                  rows),

                h('section', { 'data-pb-card': '' },
                  h('div', { 'data-pb-card-head': '' },
                    h('h2', { 'data-pb-card-title': '' }, '快捷操作'),
                    h('span', { 'data-pb-card-sub': '' }, '每颗按钮都写了后果；破坏性操作会再问一次')),
                  h(NoticeBlock, { notice, onGoStatus: null }),

                  h('div', { 'data-pb-action': '' },
                    h('div', { 'data-pb-action-title': '' }, '重启 PawBot'),
                    h('div', { 'data-pb-help': '' },
                      '会先收掉整个 PawBot 进程树再重新启动：与 NapCat 的连接会断开，正在生成或正在回复的消息会丢失，服务会短暂离线。'),
                    h('div', { 'data-pb-action-row': '' },
                      h('button', {
                        'data-pb-btn': '',
                        'data-danger': '',
                        type: 'button',
                        disabled: busy === 'restart',
                        onClick: () => setRestartOpen(true),
                      }, busy === 'restart' ? '重启中…' : '重启 PawBot'))),

                  h('div', { 'data-pb-action': '' },
                    h('div', { 'data-pb-action-title': '' }, '重载 PawBot 插件'),
                    h('div', { 'data-pb-help': '' },
                      '只请 PawBot 重新加载插件，不重启进程；外挂状态会重置，正在生成的回复可能被打断。'),
                    h('div', { 'data-pb-action-row': '' },
                      h('button', {
                        'data-pb-btn': '',
                        type: 'button',
                        disabled: busy === 'reload',
                        onClick: () => setReloadOpen(true),
                      }, busy === 'reload' ? '重载中…' : '重载 PawBot 插件'))),

                  h('div', { 'data-pb-action': '' },
                    h('div', { 'data-pb-action-title': '' }, '发测试消息'),
                    h('div', { 'data-pb-help': '' },
                      '会用机器人账号真的发出去，不是模拟；发到群需要群号，私聊需要 QQ 号。'),
                    h('div', { 'data-pb-action-row': '' },
                      h('button', {
                        'data-pb-btn': '',
                        type: 'button',
                        onClick: () => setSendOpen((value) => !value),
                      }, sendOpen ? '收起测试消息表单' : '发测试消息')),
                    sendOpen ? h('div', { 'data-pb-form': '' },
                      h('div', { 'data-pb-form-row': '' },
                        h('label', null, '目标（群号或 QQ 号）'),
                        h('input', {
                          'data-pb-input': '',
                          type: 'text',
                          value: send.target,
                          placeholder: '例如：123456789',
                          autoComplete: 'off',
                          spellCheck: false,
                          onChange: (event) => setSend((prev) => ({ ...prev, target: event.target.value })),
                        }),
                        h('div', { 'data-pb-help': '' }, '群聊填群号，私聊填对方 QQ 号；填错会送到别的地方。')),
                      h('div', { 'data-pb-form-row': '' },
                        h('label', null, '消息内容'),
                        h('textarea', {
                          'data-pb-textarea': '',
                          rows: 4,
                          value: send.message,
                          onChange: (event) => setSend((prev) => ({ ...prev, message: event.target.value })),
                        })),
                      h('div', { 'data-pb-action-row': '' },
                        h(Switch, {
                          on: send.isGroup,
                          label: '发到群',
                          onChange: (value) => setSend((prev) => ({ ...prev, isGroup: value })),
                        }),
                        h('span', { 'data-pb-help': '' }, send.isGroup ? '目前：发到群' : '目前：发私聊'),
                        h('div', { 'data-pb-grow': '' }),
                        h('button', {
                          'data-pb-btn': '',
                          'data-primary': '',
                          type: 'button',
                          disabled: busy === 'send',
                          onClick: requestSend,
                        }, busy === 'send' ? '发送中…' : '发送测试消息')),
                      h('div', { 'data-pb-help': '' }, '按下发送后还会再确认一次，确认后就真的送出去。')) : null),
                )),

              h('section', { 'data-pb-card': '' },
                h('button', {
                  'data-pb-fold': '',
                  'data-open': showRaw ? '' : undefined,
                  type: 'button',
                  'aria-expanded': Boolean(showRaw),
                  onClick: () => setShowRaw((value) => !value),
                }, h('span', { className: 'caret' }, '▸'), '原始 JSON（/pawbot-ui/status）'),
                h('div', { 'data-pb-help': '' }, '展开只会显示内容，不会改任何东西；一般排查才需要看。'),
                showRaw
                  ? h('pre', { 'data-pb-pre': '' }, JSON.stringify(st, null, 2))
                  : null)),

          h(ConfirmDialog, {
            open: restartOpen,
            title: '确定要重启 PawBot？',
            danger: true,
            busy: busy === 'restart',
            confirmLabel: '确定重启',
            onCancel: () => setRestartOpen(false),
            onConfirm: doRestart,
          },
          h('p', null, '重启会先杀掉整个 PawBot 进程树，再重新启动。'),
          h('ul', null,
            h('li', null, '与 NapCat 的连接会断开；NapCat 通常会自动重连，但需要几秒。'),
            h('li', null, '正在生成或正在回复的消息会丢失，无法补救。'),
            h('li', null, '重启期间 PawBot 会短暂离线，群里可能看到它没反应。'))),

          h(ConfirmDialog, {
            open: reloadOpen,
            title: '确定要重载 PawBot 插件？',
            danger: true,
            busy: busy === 'reload',
            confirmLabel: '确定重载',
            onCancel: () => setReloadOpen(false),
            onConfirm: doReload,
          },
          h('p', null, '这不会重启 PawBot 进程，但会请它重新加载所有插件。'),
          h('ul', null,
            h('li', null, '外挂自己的内存状态会重置；正在生成的回复可能被打断。'),
            h('li', null, '与 NapCat 的连接不受影响；配置文件也不会被改动。'))),

          h(ConfirmDialog, {
            open: sendConfirm,
            title: '确定要真的发送这条消息？',
            danger: true,
            busy: busy === 'send',
            confirmLabel: '确定送出',
            onCancel: () => setSendConfirm(false),
            onConfirm: doSend,
          },
          h('p', null, '这会用机器人账号真的发出去，不是测试模式，也无法收回。'),
          h('ul', null,
            h('li', null, '目标：' + (send.target.trim() || '（未填）') + '（' + (send.isGroup ? '群聊' : '私聊') + '）'),
            h('li', null, '内容：' + (send.message.length > 80 ? send.message.slice(0, 80) + '…' : send.message)))))
      }

      /* ── 控件 ───────────────────────────────────────────────────────────── */

      function boundsFor(item, current) {
        const n = Number(current)
        let min = item.min
        let max = item.max
        if (min === undefined) min = 0
        if (max === undefined) {
          if (/port$/.test(item.key)) max = 65535
          else if (/(chars?|_chars|tokens?)$/.test(item.key)) max = 1000000
          else if (/(timeout|_sec|interval|cooldown|_gap|delay)$/.test(item.key)) max = 86400
          else if (/_hours$/.test(item.key)) max = 8760
          else if (/_days$/.test(item.key)) max = 3650
          else max = 100000
        }
        const step = item.step !== undefined ? item.step : (Number.isInteger(n) ? 1 : 0.01)
        return { min, max, step }
      }

      function NumberControl({ item, current, onChange, onClear }) {
        const [text, setText] = useState(current === null || current === undefined ? '' : String(current))
        const focused = useRef(false)
        const bounds = boundsFor(item, current)
        useEffect(() => {
          if (!focused.current) setText(current === null || current === undefined ? '' : String(current))
        }, [current])
        const commit = (raw) => {
          const trimmed = String(raw).trim()
          if (trimmed === '') {
            onClear()
            return false
          }
          const parsed = Number(trimmed)
          if (!Number.isFinite(parsed)) {
            onClear()
            return false
          }
          if (parsed !== Number(current)) onChange(parsed)
          return true
        }
        return h('input', {
          'data-pb-input': '',
          'data-pb-number': '',
          type: 'number',
          value: text,
          min: bounds.min,
          max: bounds.max,
          step: bounds.step,
          onChange: (event) => {
            const raw = event.target.value
            setText(raw)
            if (raw.trim() === '') {
              onClear()
              return
            }
            const parsed = Number(raw)
            if (!Number.isFinite(parsed)) return
            if (parsed === Number(current)) onClear()
            else onChange(parsed)
          },
          onFocus: () => { focused.current = true },
          onBlur: () => {
            focused.current = false
            if (!commit(text)) {
              setText(current === null || current === undefined ? '' : String(current))
            }
          },
        })
      }

      function RatioControl({ item, current, onChange }) {
        const raw = Number(current)
        const value = Number.isFinite(raw) ? Math.min(1, Math.max(0, raw)) : 0
        return h('div', { 'data-pb-ratio': '' },
          h('input', {
            type: 'range',
            min: 0,
            max: 1,
            step: 0.01,
            value,
            'aria-label': item.label,
            onChange: (event) => onChange(Number(event.target.value)),
          }),
          h('span', { 'data-pb-ratio-val': '' }, (Math.round(value * 1000) / 10) + '%'))
      }

      function selectOptions(options) {
        return (Array.isArray(options) ? options : []).map((option) => {
          if (Array.isArray(option)) return { value: String(option[0]), label: String(option[1]) }
          return { value: String(option.value), label: String(option.label === undefined ? option.value : option.label) }
        })
      }

      function SelectControl({ item, current, onChange }) {
        const options = selectOptions(item.options)
        const value = current === null || current === undefined ? '' : String(current)
        const hasValue = options.some((option) => option.value === value)
        const list = hasValue ? options : [{ value, label: value === '' ? '（空值）' : '目前：' + value }].concat(options)
        return h('select', {
          'data-pb-select': '',
          value,
          onChange: (event) => onChange(event.target.value),
        }, list.map((option) => h('option', { key: option.value, value: option.value }, option.label)))
      }

      function JsonControl({ item, current, isDirty, onChange, onClear }) {
        const [open, setOpen] = useState(false)
        const [text, setText] = useState(() => JSON.stringify(current === undefined ? null : current, null, 2))
        const [err, setErr] = useState('')
        useEffect(() => {
          if (!open) {
            setText(JSON.stringify(current === undefined ? null : current, null, 2))
            setErr('')
          }
        }, [current, open])
        const summary = Array.isArray(current)
          ? '数组 · ' + current.length + ' 项'
          : isPlainObject(current)
            ? '对象 · ' + Object.keys(current).length + ' 个键'
            : '目前值：' + String(current)
        const edit = (value) => {
          setText(value)
          if (value.trim() === '') {
            setErr('')
            onClear()
            return
          }
          try {
            const parsed = JSON.parse(value)
            setErr('')
            onChange(parsed)
          } catch (error) {
            setErr('JSON 格式错误：' + String((error && error.message) || error) + '（还没应用）')
            onClear()
          }
        }
        return h('div', { 'data-pb-control': '' },
          h('div', { 'data-pb-action-row': '' },
            h('span', { 'data-pb-help': '' }, summary),
            h('button', {
              'data-pb-btn': '',
              type: 'button',
              onClick: () => setOpen((value) => !value),
            }, open ? '收起 JSON' : '展开编辑 JSON')),
          open ? h('div', null,
            h('textarea', {
              'data-pb-code': '',
              rows: 6,
              value: text,
              spellCheck: false,
              onChange: (event) => edit(event.target.value),
            }),
            err ? h(Notice, { tone: 'err' }, err) : h('div', { 'data-pb-help': '' },
              isDirty ? '已应用，尚未保存；保存只会送这个键。' : '目前与服务器上的值相同。'),
            h('div', { 'data-pb-help': '' },
              'PawBot 对对象只做深合并、对数组是整包替换；删掉对象里的键不会被移除。')) : null)
      }

      function SecretControl({ item, masked, isDirty, draft, onChange, onClearSecret }) {
        const [text, setText] = useState('')
        const [clearConfirm, setClearConfirm] = useState(false)
        // 明確的「清除」會把 dirty 設成空字串；空白輸入框本身仍然只是「不改」。
        const pendingClear = Boolean(isDirty && draft === '')
        const badge = pendingClear
          ? h(Badge, { tone: 'err' }, '将清除（送空字符串）')
          : masked
            ? h(Badge, { tone: 'warn' }, '已设置（不会显示）')
            : h(Badge, { tone: 'muted' }, '未设置')
        return h('div', { 'data-pb-control': '' },
          h('div', { 'data-pb-secret-row': '' },
            badge,
            h('span', { 'data-pb-help': '' },
              pendingClear ? '保存后会把这个值清空。' : '留空＝不改；输入新值才会覆盖'),
            h('button', {
              'data-pb-btn': '',
              type: 'button',
              disabled: !masked && !isDirty,
              onClick: () => setClearConfirm(true),
            }, '清除（设为空）')),
          h('input', {
            'data-pb-input': '',
            type: 'password',
            value: text,
            placeholder: '输入新值才会覆盖…',
            autoComplete: 'new-password',
            spellCheck: false,
            'aria-label': item.label + ' 新值',
            onChange: (event) => {
              setText(event.target.value)
              onChange(event.target.value)
            },
          }),
          h(ConfirmDialog, {
            open: clearConfirm,
            title: '确定清除「' + item.label + '」？',
            danger: true,
            confirmLabel: '标记为清除',
            onCancel: () => setClearConfirm(false),
            onConfirm: () => {
              setClearConfirm(false)
              setText('')
              onClearSecret()
            },
          },
          h('p', null, '按下「保存改动」后会送空字符串给 PawBot，把这个值清空；原本的值无法恢复。'),
          h('p', null, '如果只是不想改，按「取消」即可。')))
      }

      function Field({ item, value, masked, draft, isDirty, onChange, onClear, onClearSecret }) {
        const current = isDirty ? draft : value
        let control = null

        if (masked || item.control === 'secret') {
          control = h(SecretControl, {
            item,
            masked: Boolean(masked),
            isDirty: Boolean(isDirty),
            draft,
            onChange,
            onClearSecret,
          })
        } else if (item.control === 'switch' || typeof current === 'boolean') {
          control = h(Switch, {
            on: Boolean(current),
            label: item.label,
            onChange: (next) => onChange(next),
          })
        } else if (item.control === 'ratio' || (item.control !== 'number' && typeof current === 'number' && current >= 0 && current <= 1 && !Number.isInteger(current))) {
          control = h(RatioControl, { item, current, onChange })
        } else if (item.control === 'number' || typeof current === 'number') {
          control = h(NumberControl, { item, current, onChange, onClear })
        } else if (item.control === 'select') {
          control = h(SelectControl, { item, current, onChange })
        } else if (item.control === 'json' || Array.isArray(current) || isPlainObject(current)) {
          control = h(JsonControl, { item, current, isDirty, onChange, onClear })
        } else if (item.control === 'multiline' || (typeof current === 'string' && (current.length > 80 || current.indexOf('\n') >= 0))) {
          control = h('textarea', {
            'data-pb-textarea': '',
            rows: 4,
            value: current === null || current === undefined ? '' : String(current),
            onChange: (event) => onChange(event.target.value),
          })
        } else {
          control = h('input', {
            'data-pb-input': '',
            type: 'text',
            value: current === null || current === undefined ? '' : String(current),
            autoComplete: 'off',
            spellCheck: false,
            onChange: (event) => onChange(event.target.value),
          })
        }

        return h('div', { 'data-pb-field': '', 'data-pb-field-key': item.key },
          h('div', { 'data-pb-field-head': '' },
            h('label', { 'data-pb-field-label': '', title: item.key }, item.label),
            h(Impacts, { codes: item.impact })),
          h('div', { 'data-pb-control': '' }, control),
          h('div', { 'data-pb-help': '' },
            item.help,
            item.advanced ? h('span', null, ' ', h('code', { 'data-pb-keychip': '' }, item.key)) : null))
      }

      /* ── 設定頁 ─────────────────────────────────────────────────────────── */

      function ConfigTab({ onGoStatus, onDirtyChange }) {
        const [cfg, setCfg] = useState(null)
        const [maskedKeys, setMaskedKeys] = useState([])
        const [err, setErr] = useState('')
        const [loading, setLoading] = useState(true)
        const [dirty, setDirty] = useState({})
        const [search, setSearch] = useState('')
        const [showAdvanced, setShowAdvanced] = useState(false)
        const [activeGroup, setActiveGroup] = useState('model')
        const [navCollapsed, setNavCollapsed] = useState(false)
        const [rev, setRev] = useState(0)
        const [rawOpen, setRawOpen] = useState(false)
        const [rawText, setRawText] = useState('')
        const [rawErr, setRawErr] = useState('')
        const [confirm, setConfirm] = useState(null)
        const [discardConfirm, setDiscardConfirm] = useState(false)
        const [saving, setSaving] = useState(false)
        const [notice, setNotice] = useState(null)
        const ctrlRef = useRef(null)

        const applyServerConfig = (config, masked) => {
          const safe = config && typeof config === 'object' && !Array.isArray(config) ? config : {}
          setCfg(safe)
          setMaskedKeys(Array.isArray(masked) ? masked : [])
          setRawText(JSON.stringify(safe, null, 2))
          setDirty({})
          setRev((value) => value + 1)
          setRawErr('')
        }

        const load = async (signal) => {
          setLoading(true)
          setErr('')
          try {
            const result = await api('GET', '/config', undefined, signal)
            applyServerConfig(result && result.config, result && result.masked_keys)
          } catch (error) {
            if (!isAbortError(error)) setErr(friendlyError(error))
          } finally {
            if (!(signal && signal.aborted)) setLoading(false)
          }
        }

        useEffect(() => {
          const ctrl = new AbortController()
          ctrlRef.current = ctrl
          load(ctrl.signal)
          return () => ctrl.abort()
        }, [])

        useEffect(() => {
          if (!showAdvanced && activeGroup === 'advanced') setActiveGroup('model')
        }, [showAdvanced, activeGroup])

        const clearField = (key) => {
          setDirty((prev) => {
            if (!hasOwn(prev, key)) return prev
            const next = { ...prev }
            delete next[key]
            return next
          })
        }

        const maskedSet = new Set(maskedKeys)
        // 渲染與寫入守衛共用同一個判斷：真的被遮罩的鍵，或 metadata 標成 secret 的鍵。
        const isSecretField = (key) => {
          const item = META_BY_KEY[key]
          return maskedSet.has(key) || Boolean(item && item.control === 'secret')
        }

        const setField = (key, value) => {
          // 遮罩值 / 空白輸入＝不改；要清空必須按明確的「清除（設為空）」。
          if (isSecretField(key) && (value === '' || value === MASK)) {
            clearField(key)
            return
          }
          const original = getPath(cfg, key)
          if (deepEqual(value, original)) {
            clearField(key)
            return
          }
          setDirty((prev) => ({ ...prev, [key]: value }))
        }

        // 明確清除 secret：dirty 存空字串，存檔時真的把值清空（不是「不改」）。
        const clearSecretToEmpty = (key) => {
          const original = getPath(cfg, key)
          if (deepEqual(original, '')) {
            clearField(key)
            return
          }
          setDirty((prev) => ({ ...prev, [key]: '' }))
        }

        const dirtyKeys = Object.keys(dirty)
        const dirtyCount = dirtyKeys.length

        // 讓外層 Panel 知道目前有多少未儲存改動（切分頁時要提示）。
        useEffect(() => {
          if (typeof onDirtyChange === 'function') onDirtyChange(dirtyCount)
        }, [dirtyCount, onDirtyChange])

        const patchFromDirty = () => {
          const patch = {}
          for (const key of dirtyKeys) setPath(patch, key, dirty[key])
          // 表單路徑也要過 stripMask（例如某個值是遮罩字串），避免把遮罩寫回。
          return stripMask(patch)
        }

        const restartKeysOf = (keys) => keys.filter((key) => {
          const item = META_BY_KEY[key]
          return Boolean(item && item.impact.indexOf('restart') >= 0)
        })

        const requestSave = () => {
          if (!dirtyCount) {
            setNotice({ tone: 'info', text: '没有改动，不需要保存。' })
            return
          }
          const patch = patchFromDirty()
          if (!patch) {
            setNotice({ tone: 'warn', text: '这次的改动只有遮罩值，没有可送出的内容；请按「放弃全部改动」重来。' })
            return
          }
          setNotice(null)
          setConfirm({
            kind: 'form',
            patch,
            keys: dirtyKeys.slice(),
            count: dirtyCount,
            restart: restartKeysOf(dirtyKeys),
          })
        }

        const requestRawSave = () => {
          setRawErr('')
          let parsed
          try {
            parsed = JSON.parse(rawText)
          } catch (error) {
            setRawErr('原始 JSON 格式错误：' + String((error && error.message) || error))
            return
          }
          if (!isPlainObject(parsed)) {
            setRawErr('最外层必须是对象（不能是数组或单一值）。')
            return
          }
          const diff = deepDiff(cfg, parsed)
          const stripped = diff === undefined ? undefined : stripMask(diff)
          if (!stripped) {
            setNotice({ tone: 'info', text: '原始 JSON 没有可送出的改动（遮罩值不会被写回）。' })
            return
          }
          const keys = flattenLeaves(stripped, '')
          setNotice(null)
          setConfirm({
            kind: 'raw',
            patch: stripped,
            keys,
            count: countLeaves(stripped),
            restart: restartKeysOf(keys),
          })
        }

        const performSave = async () => {
          if (!confirm) return
          const plan = confirm
          setSaving(true)
          setNotice(null)
          try {
            const result = await api('POST', '/config', { patch: plan.patch }, ctrlRef.current && ctrlRef.current.signal)
            const next = result && result.config ? result.config : cfg
            const nextMasked = result && Array.isArray(result.masked_keys) ? result.masked_keys : maskedKeys
            setCfg(next)
            setMaskedKeys(nextMasked)
            setRawText(JSON.stringify(next, null, 2))
            setDirty({})
            setRev((value) => value + 1)
            setRawErr('')
            setConfirm(null)
            setNotice({
              tone: 'good',
              text: '已保存 ' + plan.count + ' 项改动。',
              restart: plan.restart,
            })
          } catch (error) {
            if (!isAbortError(error)) {
              setNotice({ tone: 'err', text: '保存失败：' + friendlyError(error) + '；改动还保留着，可以再试一次。' })
              setConfirm(null)
            }
          } finally {
            setSaving(false)
          }
        }

        const discardAll = () => {
          const count = dirtyCount
          setDirty({})
          setRawText(JSON.stringify(cfg, null, 2))
          setRawErr('')
          setDiscardConfirm(false)
          // 重掛欄位元件，順便清掉 SecretControl 等元件的本地輸入狀態。
          setRev((value) => value + 1)
          setNotice({
            tone: 'info',
            text: count ? '已放弃 ' + count + ' 项未保存的表单改动。' : '已还原原始 JSON 编辑框。',
          })
        }

        const q = search.trim().toLowerCase()
        const matches = (item) => {
          if (!q) return true
          let extra = ''
          if (Array.isArray(item.options)) {
            extra = item.options.map((option) => (
              Array.isArray(option) ? String(option[1]) : String(option.label === undefined ? option.value : option.label)
            )).join(' ')
          }
          return (item.label + ' ' + item.key + ' ' + item.help + ' ' + extra).toLowerCase().indexOf(q) >= 0
        }
        const commonMatches = COMMON_META.filter(matches)
        const advancedMatches = ADVANCED_META.filter(matches)

        const renderFields = (items) => h('div', { 'data-pb-fields-wrap': '' },
          h('div', { 'data-pb-fields': '' },
            items.map((item) => h(Field, {
              key: rev + ':' + item.key,
              item,
              value: getPath(cfg, item.key),
              masked: maskedSet.has(item.key),
              draft: dirty[item.key],
              isDirty: hasOwn(dirty, item.key),
              onChange: (value) => setField(item.key, value),
              onClear: () => clearField(item.key),
              onClearSecret: () => clearSecretToEmpty(item.key),
            }))))

        const renderGroupCard = (group, items) => h('section', { key: group.id, 'data-pb-card': '' },
          h('div', { 'data-pb-card-head': '' },
            h('h2', { 'data-pb-card-title': '' }, group.label),
            h('span', { 'data-pb-card-sub': '' },
              items.length + ' 项' + (q ? '符合' : '') + ' · ' + group.hint)),
          items.length
            ? renderFields(items)
            : h('div', { 'data-pb-empty': '' }, h('div', { 'data-pb-help': '' }, '这个分组没有符合条件的设置。')))

        const renderSectionCard = (section, items) => h('section', { key: 'section-' + section, 'data-pb-card': '' },
          h('div', { 'data-pb-card-head': '' },
            h('h2', { 'data-pb-card-title': '' }, SECTION_LABELS[section] || section),
            h('span', { 'data-pb-card-sub': '' }, items.length + ' 项 · 原始区段 ' + section)),
          renderFields(items))

        const rawCard = () => h('section', { 'data-pb-card': '' },
          h('div', { 'data-pb-card-head': '' },
            h('h2', { 'data-pb-card-title': '' }, '原始 JSON（整份设置）'),
            h('span', { 'data-pb-card-sub': '' }, (cfg ? Object.keys(cfg).length : 0) + ' 个区段')),
          h('p', { 'data-pb-help': '' },
            '想一次看全部设置才用这个。保存只送有改动的键；遮罩值「已设置」不会被写回。' +
            'PawBot 只做深合并，删掉对象里的键不会被移除。'),
          h('div', { 'data-pb-action-row': '' },
            h('button', {
              'data-pb-btn': '',
              type: 'button',
              onClick: () => setRawOpen((value) => !value),
            }, rawOpen ? '收起原始 JSON' : '展开原始 JSON'),
            h('span', { 'data-pb-help': '' }, rawOpen ? '编辑后按下方按钮，会先验证 JSON 再让你确认。' : '展开后可直接编辑整份 JSON。')),
          rawOpen ? h('div', null,
            h('textarea', {
              'data-pb-code': '',
              rows: 18,
              value: rawText,
              spellCheck: false,
              onChange: (event) => {
                setRawText(event.target.value)
                setRawErr('')
              },
            }),
            rawErr ? h(Notice, { tone: 'err' }, rawErr) : null,
            h('div', { 'data-pb-action-row': '' },
              h('button', {
                'data-pb-btn': '',
                'data-primary': '',
                type: 'button',
                disabled: saving,
                onClick: requestRawSave,
              }, saving ? '处理中…' : '保存原始 JSON'),
              h('span', { 'data-pb-help': '' }, '会先算差异，再列出这次要改的中文标签。'))) : null)

        const advancedSections = Object.keys(SECTION_LABELS).map((section) => ({
          section,
          items: (q ? advancedMatches : ADVANCED_META).filter((item) => item.section === section),
        })).filter((entry) => entry.items.length)

        const navItems = GROUPS.filter((group) => group.id !== 'advanced').map((group) => ({
          ...group,
          count: (q ? commonMatches : COMMON_META).filter((item) => item.group === group.id).length,
        }))
        if (showAdvanced) {
          navItems.push({
            ...GROUPS.find((group) => group.id === 'advanced'),
            count: q ? advancedMatches.length : ADVANCED_META.length,
          })
        }

        const content = []
        if (q) {
          const commonGroups = GROUPS.filter((group) => group.id !== 'advanced')
            .map((group) => ({ group, items: commonMatches.filter((item) => item.group === group.id) }))
            .filter((entry) => entry.items.length)
          const hiddenAdvancedNotice = !showAdvanced && advancedMatches.length
            ? h(Notice, {
              key: 'hidden-advanced',
              tone: 'info',
              action: h('button', {
                'data-pb-btn': '',
                type: 'button',
                onClick: () => setShowAdvanced(true),
              }, '显示 ' + advancedMatches.length + ' 个高级结果'),
            }, '还有 ' + advancedMatches.length + ' 个符合的高级设置；它们收在「高级（原始 JSON）」里。')
            : null
          if (!commonMatches.length && !(showAdvanced && advancedMatches.length)) {
            content.push(h('section', { key: 'no-result', 'data-pb-card': '' },
              h('div', { 'data-pb-card-head': '' }, h('h2', { 'data-pb-card-title': '' },
                advancedMatches.length ? '常用设置里没有符合的项目' : '找不到符合的设置')),
              h('div', { 'data-pb-help': '' }, advancedMatches.length
                ? '但高级设置里有 ' + advancedMatches.length + ' 项符合；按下面的按钮把它们显示出来。'
                : '试试中文关键词（例如「记忆」「延迟」）或原始 key（例如 llm.model）。')))
            if (hiddenAdvancedNotice) content.push(hiddenAdvancedNotice)
          } else {
            content.push(h('div', { key: 'summary', 'data-pb-search-summary': '' },
              '搜索「' + search.trim() + '」：' + commonMatches.length + ' 个常用' +
              (showAdvanced ? '、' + advancedMatches.length + ' 个高级' : '') + '结果'))
            for (const entry of commonGroups) content.push(renderGroupCard(entry.group, entry.items))
            if (showAdvanced) {
              for (const entry of advancedSections) content.push(renderSectionCard(entry.section, entry.items))
            } else if (hiddenAdvancedNotice) {
              content.push(hiddenAdvancedNotice)
            }
          }
        } else if (activeGroup === 'advanced') {
          content.push(h(React.Fragment, { key: 'raw' }, rawCard()))
          for (const entry of advancedSections) content.push(renderSectionCard(entry.section, entry.items))
        } else {
          const group = GROUPS.find((entry) => entry.id === activeGroup) || GROUPS[0]
          content.push(renderGroupCard(group, COMMON_META.filter((item) => item.group === group.id)))
        }

        const head = h('div', { 'data-pb-top': '' },
          h('div', null,
            h('h1', { 'data-pb-title': '' }, 'PawBot 设置'),
            h('p', { 'data-pb-sub': '' },
              '默认只显示常用设置；其余 ' + ADVANCED_META.length + ' 个原始键收在「高级」。改完按保存，会先列出中文标签让你确认。')))

        if (!cfg) {
          return h(React.Fragment, null, head,
            loading
              ? h('div', { 'data-pb-card': '' }, h('div', { 'data-pb-help': '' }, '正在读取 PawBot 设置…'))
              : h(React.Fragment, null,
                h(Notice, {
                  tone: 'err',
                  action: h('button', {
                    'data-pb-btn': '',
                    type: 'button',
                    onClick: () => load(ctrlRef.current && ctrlRef.current.signal),
                  }, '再试一次'),
                }, '读不到设置：' + (err || '未知错误') + '。可以到状态页确认 PawBot 有没有在跑。'),
                h('div', { 'data-pb-card': '' },
                  h('div', { 'data-pb-help': '' }, '读取成功后才会出现设置表单；现在按保存不会送出任何东西。'))))
        }

        return h(React.Fragment, null,
          head,
          err ? h(Notice, { tone: 'warn' }, '上次读取失败：' + err) : null,
          h('div', {
            'data-pb-config': '',
            'data-nav-collapsed': navCollapsed ? '' : undefined,
          },
          navCollapsed ? null : h('nav', { 'data-pb-nav': '' },
            h('div', { 'data-pb-nav-head': '' },
              h('span', { 'data-pb-nav-title': '' }, '分组导航'),
              h('button', {
                'data-pb-btn': '',
                type: 'button',
                onClick: () => setNavCollapsed(true),
              }, '收起')),
            h('div', { 'data-pb-help': '' }, '点一组只看那一组；不会改任何设置。'),
            h('div', { 'data-pb-nav-list': '' },
              navItems.map((item) => h('button', {
                key: item.id,
                'data-pb-nav-btn': '',
                'data-active': !q && activeGroup === item.id ? '' : undefined,
                type: 'button',
                onClick: () => {
                  setActiveGroup(item.id)
                  setSearch('')
                },
              }, h('span', null, item.label), h('span', { 'data-pb-nav-count': '' }, String(item.count)))))),
          h('div', { 'data-pb-cfg-main': '' },
            h('div', { 'data-pb-cfg-top': '' },
              h('div', { 'data-pb-cfg-tools': '' },
                h('input', {
                  'data-pb-input': '',
                  'data-pb-search': '',
                  type: 'search',
                  value: search,
                  placeholder: '搜索中文标签、原始 key 或说明…',
                  'aria-label': '搜索设置',
                  onChange: (event) => setSearch(event.target.value),
                }),
                search ? h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  onClick: () => setSearch(''),
                }, '清除') : null,
                h('div', { 'data-pb-grow': '' }),
                navCollapsed ? h('button', {
                  'data-pb-btn': '',
                  type: 'button',
                  onClick: () => setNavCollapsed(false),
                }, '显示分组导航') : null,
                h('div', { 'data-pb-toggle': '' },
                  h(Switch, {
                    on: showAdvanced,
                    label: '显示高级项目',
                    onChange: (value) => {
                      setShowAdvanced(value)
                      if (value) setActiveGroup('advanced')
                    },
                  }),
                  h('span', null, '显示高级项目')),
                h('button', {
                  'data-pb-btn': '',
                  'data-primary': '',
                  type: 'button',
                  disabled: !dirtyCount || saving,
                  onClick: requestSave,
                }, saving ? '保存中…' : '保存改动' + (dirtyCount ? '（' + dirtyCount + '）' : '')),
                dirtyCount ? h('button', {
                  'data-pb-btn': '',
                  'data-danger': '',
                  type: 'button',
                  disabled: saving,
                  onClick: () => setDiscardConfirm(true),
                }, '放弃全部改动') : null),
              h('div', { 'data-pb-cfg-hint': '' },
                h('span', null, '常用 ' + COMMON_META.length + ' 项；高级 ' + ADVANCED_META.length + ' 项只在开启后出现。'),
                dirtyCount
                  ? h('span', { 'data-pb-dirty': '' }, dirtyCount + ' 项未保存')
                  : h('span', null, '只会送出改动过的键。'),
                h('span', null, '保存前会列出中文标签让你确认。'))),
            h(NoticeBlock, { notice, onGoStatus }),
            h('div', { 'data-pb-stack': '' }, content))),

          h(ConfirmDialog, {
            open: Boolean(confirm),
            title: '这次会改动 ' + (confirm ? confirm.count : 0) + ' 项设置',
            busy: saving,
            confirmLabel: '确定保存 ' + (confirm ? confirm.count : 0) + ' 项',
            onCancel: () => { if (!saving) setConfirm(null) },
            onConfirm: performSave,
          },
          h('p', null, '只会把下面这些键送回 PawBot；没动到的设置不会被覆盖。'),
          h('ul', { 'data-pb-changelist': '' },
            (confirm ? confirm.keys : []).map((key) => h('li', { key },
              h('b', null, (META_BY_KEY[key] && META_BY_KEY[key].label) || key),
              ' ',
              h('code', null, key)))),
          confirm && confirm.restart && confirm.restart.length
            ? h('p', null, '其中 ' + confirm.restart.length + ' 项需要重启 PawBot 才生效：' +
              confirm.restart.map((key) => (META_BY_KEY[key] && META_BY_KEY[key].label) || key).join('、') + '。')
            : h('p', null, '这些项目都会立即生效。')),

          h(ConfirmDialog, {
            open: discardConfirm,
            title: '确定放弃全部改动？',
            danger: true,
            busy: saving,
            confirmLabel: '确定放弃',
            onCancel: () => setDiscardConfirm(false),
            onConfirm: discardAll,
          },
          h('p', null, '表单里 ' + dirtyCount + ' 项未保存的改动会消失，无法恢复。'),
          h('p', null, '原始 JSON 编辑框也会回到上次读到的设置；服务器上的 config 不会被改动。')))
      }

      /* ── 人設頁 ─────────────────────────────────────────────────────────── */

      function PersonaTab() {
        const [draft, setDraft] = useState('')
        const [original, setOriginal] = useState('')
        const [path, setPath] = useState('')
        const [loaded, setLoaded] = useState(false)
        const [err, setErr] = useState('')
        const [busy, setBusy] = useState(false)
        const [notice, setNotice] = useState(null)
        const [confirm, setConfirm] = useState(false)
        const [resetConfirm, setResetConfirm] = useState(false)
        const ctrlRef = useRef(null)

        const load = async (signal) => {
          setErr('')
          try {
            const result = await api('GET', '/persona', undefined, signal)
            const text = String((result && (result.content !== undefined ? result.content : result.text)) || '')
            setDraft(text)
            setOriginal(text)
            setPath(String((result && result.path) || ''))
            setLoaded(true)
          } catch (error) {
            if (!isAbortError(error)) setErr(friendlyError(error))
          }
        }

        useEffect(() => {
          const ctrl = new AbortController()
          ctrlRef.current = ctrl
          load(ctrl.signal)
          return () => ctrl.abort()
        }, [])

        const count = charCount(draft)
        const countNoSpace = charCount(String(draft).replace(/\s/g, ''))
        const originalCount = charCount(original)
        const changed = draft !== original

        const doSave = async () => {
          setBusy(true)
          setNotice(null)
          try {
            await api('POST', '/persona', { content: draft }, ctrlRef.current && ctrlRef.current.signal)
            setOriginal(draft)
            setConfirm(false)
            setNotice({ tone: 'good', text: '已保存 ' + count + ' 字；她下一轮回话就会用新的说法。' })
          } catch (error) {
            if (!isAbortError(error)) {
              setNotice({ tone: 'err', text: '保存失败：' + friendlyError(error) + '；编辑框内容还在，可以再试一次。' })
              setConfirm(false)
            }
          } finally {
            setBusy(false)
          }
        }

        return h(React.Fragment, null,
          h('div', { 'data-pb-top': '' },
            h('div', null,
              h('h1', { 'data-pb-title': '' }, 'PawBot 人设'),
              h('p', { 'data-pb-sub': '' }, '人设会直接影响她说话的方式，保存后下一轮就生效。'))),

          err ? h(Notice, {
            tone: 'err',
            action: h('button', {
              'data-pb-btn': '',
              type: 'button',
              onClick: () => load(ctrlRef.current && ctrlRef.current.signal),
            }, '再试一次'),
          }, '读不到人设：' + err) : null,

          h('section', { 'data-pb-card': '' },
            h('div', { 'data-pb-card-head': '' },
              h('h2', { 'data-pb-card-title': '' }, '人设内容'),
              h('span', { 'data-pb-card-sub': '' }, path ? '文件：' + path : '全宽编辑器')),
            h('p', { 'data-pb-help': '' },
              '这是她回话时的世界观与说话规则，会直接影响她怎么讲话。保存是整份覆盖，不是合并。'),
            h('div', { 'data-pb-persona-tools': '' },
              h('button', {
                'data-pb-btn': '',
                'data-primary': '',
                type: 'button',
                disabled: !loaded || busy || !changed,
                onClick: () => setConfirm(true),
              }, busy ? '保存中…' : '保存人设'),
              h('button', {
                'data-pb-btn': '',
                type: 'button',
                disabled: !loaded || busy || !changed,
                onClick: () => setResetConfirm(true),
              }, '还原成上次读到的版本'),
              h('span', { 'data-pb-count': '' }, '共 ' + count + ' 字（不含空白 ' + countNoSpace + ' 字；Unicode 字符，不是字节）'),
              changed
                ? h(Badge, { tone: 'warn' }, '尚未保存')
                : h(Badge, { tone: 'good' }, '与读到的版本相同')),
            h('div', { 'data-pb-help': '' },
              '保存：覆盖 PawBot 的人设文件，下一轮回话立即生效。'),
            h('div', { 'data-pb-help': '' },
              '还原：只把编辑框回到这次打开面板时读到的版本（' + originalCount + ' 字），不会动服务器上的文件，也不是还原到出厂值。'),
            h(NoticeBlock, { notice }),
            h('textarea', {
              'data-pb-code': '',
              'data-pb-persona-editor': '',
              rows: 22,
              value: draft,
              disabled: !loaded || busy,
              spellCheck: false,
              placeholder: loaded ? '' : '读取人设中…',
              onChange: (event) => {
                setDraft(event.target.value)
                setConfirm(false)
              },
            })),

          h(ConfirmDialog, {
            open: confirm,
            title: '确定要保存人设？',
            danger: true,
            busy,
            confirmLabel: '确定保存',
            onCancel: () => setConfirm(false),
            onConfirm: doSave,
          },
          h('p', null, '这会直接覆盖 PawBot 的人设文件，不是合并，也不会自动备份旧版本。'),
          h('ul', null,
            h('li', null, '这次会写入 ' + count + ' 字。'),
            h('li', null, '保存后，她下一轮回话就会用新的说法。'),
            h('li', null, '如果后悔，只能用你手上记得的内容再改回来。'))),

          h(ConfirmDialog, {
            open: resetConfirm,
            title: '确定要丢弃这次修改？',
            danger: true,
            busy,
            confirmLabel: '确定还原',
            onCancel: () => setResetConfirm(false),
            onConfirm: () => {
              setDraft(original)
              setResetConfirm(false)
              setNotice({ tone: 'info', text: '已还原成上次读到的版本；服务器上的人设文件没有被改动。' })
            },
          },
          h('p', null, '这只会把编辑框回到这次打开面板时读到的版本（' + originalCount + ' 字）。'),
          h('ul', null,
            h('li', null, '你这次还没保存的修改会消失，无法恢复。'),
            h('li', null, '服务器上的人设文件不会被改动，也不会变成出厂值。'))))
      }

      /* ── 頂層面板：4 個分頁（启动 / 状态 / 设定 / 人设） ─────────────────── */

      const TABS = [
        ['setup', '启动'],
        ['status', '状态'],
        ['config', '设置'],
        ['persona', '人设'],
      ]

      function Panel() {
        // 「启动」排第一且预设打开：新用户第一眼就看到一键引导。
        const [tab, setTab] = useState('setup')
        // 只掛載使用者拜訪過的分頁；之後用 CSS 隱藏而不是卸載，避免切走再回來掉草稿。
        const [mounted, setMounted] = useState({ setup: true, status: false, config: false, persona: false })
        const [configDirty, setConfigDirty] = useState(0)
        const [tabNotice, setTabNotice] = useState('')
        const selectTab = (id) => {
          if (id !== 'config' && tab === 'config' && configDirty > 0) {
            setTabNotice('设置页还有 ' + configDirty + ' 项未保存的改动；已切换标签页，改动还保留着，回到「设置」就能继续。')
          } else if (id === 'config') {
            setTabNotice('')
          }
          setTab(id)
          setMounted((prev) => (prev[id] ? prev : { ...prev, [id]: true }))
        }
        return h('div', { 'data-pb-root': '' },
          h('div', { 'data-pb-top': '' },
            h('div', null,
              h('div', { 'data-pb-eyebrow': '' }, 'DSH 受管服务'),
              h('h1', { 'data-pb-title': '' }, 'PawBot 控制面板'),
              h('p', { 'data-pb-sub': '' },
                '一键启动、看状态、改设置、写人设，都在这里完成；不用再开 PawBot 自己的 6185 网页。')),
            h('div', { 'data-pb-tabs': '', role: 'tablist', 'aria-label': 'PawBot 控制面板标签页' },
              TABS.map(([id, label]) => h('button', {
                key: id,
                type: 'button',
                role: 'tab',
                'aria-selected': tab === id,
                'data-pb-tab': '',
                'data-active': tab === id ? '' : undefined,
                onClick: () => selectTab(id),
              }, label + (id === 'config' && configDirty > 0 ? '（' + configDirty + '）' : ''))))),
          tabNotice
            ? h(Notice, {
              tone: 'warn',
              action: h('button', {
                'data-pb-btn': '',
                type: 'button',
                onClick: () => selectTab('config'),
              }, '回到设置'),
            }, tabNotice)
            : null,
          h('div', { 'data-pb-tabpanel': '', 'data-pb-hidden': tab !== 'setup' ? '' : undefined },
            mounted.setup
              ? h(SetupTab, {
                active: tab === 'setup',
                onGoStatus: () => selectTab('status'),
                onGoConfig: () => selectTab('config'),
              })
              : null),
          h('div', { 'data-pb-tabpanel': '', 'data-pb-hidden': tab !== 'status' ? '' : undefined },
            mounted.status ? h(StatusTab, { active: tab === 'status', onGoConfig: () => selectTab('config') }) : null),
          h('div', { 'data-pb-tabpanel': '', 'data-pb-hidden': tab !== 'config' ? '' : undefined },
            mounted.config ? h(ConfigTab, { onGoStatus: () => selectTab('status'), onDirtyChange: setConfigDirty }) : null),
          h('div', { 'data-pb-tabpanel': '', 'data-pb-hidden': tab !== 'persona' ? '' : undefined },
            mounted.persona ? h(PersonaTab) : null))
      }

      return {
        inject: ['slots'],
        apply(ctx) {
          let style = document.querySelector('style[data-plugin-css="@local/pawbot"]')
          if (!style) {
            style = document.createElement('style')
            style.setAttribute('data-plugin-css', '@local/pawbot')
            document.head.appendChild(style)
          }
          style.textContent = CSS
          ctx.effect(() => () => style.remove(), '@local/pawbot: 控制面板样式')

          const slots = (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots
          if (!slots) return
          slots.inject('settings.section', () => slots.register(
            { name: 'settings.section', id: 'pawbot', order: 40, label: 'PawBot' },
            () => h(Panel),
          ))
        },
      }

    },
  })
}

