# PawBot

一个 **零依赖**、能完整读懂的 QQ 机器人框架。
照着 AstrBot 的思路做，但只保留骨架——全部代码不到 1500 行，你可以照着改。

```
NapCatQQ（协议端） ──WebSocket──► PawBot（本框架） ──HTTP──► 大模型
        │                              │
        └──► QQ 群 ◄───────────────────┘
```

**为什么不用现成的？** 因为 AstrBot 装不上。它依赖的 `aiocqhttp` 要现场编译 C 扩展，
在沙盒/没编译器/没网的环境下必挂。PawBot 只用 Python 标准库——连 WebSocket 协议都是手写的。

---

## 30 秒跑起来

```bash
cd pawbot
python run.py
```

看到这两行就算成功：

```
[INFO ] OneBot 反向 WebSocket 已监听 ws://0.0.0.0:6199/ws
[INFO ] WebUI 已启动 http://127.0.0.1:6185
```

浏览器打开 <http://127.0.0.1:6185>。（装成 DSH 插件后，它会在 apply 时自动拉起 PawBot；也可以手动 `python run.py`。）

**没有 QQ 也能测试**（模拟一个 NapCat 连上来）：

```bash
python tools/mock_qq.py --text "大肥鱼在干嘛"
```

---

## 接上真的 QQ 群

> 完整的登录、接入、日常使用说明见 **[使用指南.md](使用指南.md)**（三步走：启动 PawBot → NapCat 登录 QQ → 群里 @ 它）。

前提：NapCat 已经在跑，QQ 已登录，WebUI 在 6099。

### 1. 配 NapCat

打开 `http://127.0.0.1:6099/webui` → **网络配置** → 新建 **WebSocket 客户端**：

| 项 | 值 |
|---|---|
| 名称 | PawBot |
| URL | `ws://127.0.0.1:6199/ws` |
| 消息上报格式 | **array** |
| Token | 留空（或与 `config.json` 的 `server.access_token` 一致） |
| 心跳间隔 / 重连间隔 | 3000 / 3000 |

> NapCat 的 `config/onebot11.json` 里那条 WebSocket 客户端配置填 `ws://127.0.0.1:6199/ws`，
> 端口对上就能直接接上 PawBot。

### 2. 填大模型

WebUI → **配置** → `llm` 分组：

```json
"base_url": "https://api.deepseek.com/v1",
"api_key":  "sk-...",
"model":    "deepseek-chat"
```

不想用 DeepSeek 就换成任何 OpenAI 兼容地址（通义、Kimi、智谱、Ollama、LM Studio 都行）。
不填 key 会进入 **mock 模式**，只回固定话术，用来跑通链路。

主模型换到第三方中转（如 `https://9527.codes/v1` + `gemini-3.8-flash`）时可以只让它管会话，
搜索、看图、判官继续留在 DeepSeek：

| 用途 | 配置 | 说明 |
|---|---|---|
| 会话（含工具调用） | `llm.base_url` / `api_key` / `model` | 中转渠道 |
| 联网搜索 | `tools.search_base_url` / `search_api_key` / `search_model` | 仍打 DeepSeek Responses API |
| 看图（多模态） | `llm.vision_base_url` / `vision_api_key` / `vision_model` | 留空则跟随会话那套；当前也走 Gemini |
| 判官 | `judge.base_url` / `api_key` / `model` | 情绪判定、图片打分 |

中转渠道偶发慢请求，`llm.timeout` 建议 120 秒。

思考型模型（deepseek-flash、gemini-3.8-flash 这类）默认就带思考强度：

| 键 | 默认 | 作用 |
|---|---|---|
| `llm.reasoning_effort` | `medium` | 聊天和工具调用那次请求 |
| `llm.vision_reasoning_effort` | 空（跟随上一行） | 看图那次请求 |
| `tools.search_reasoning_effort` | `medium` | 联网搜索（Responses API）那次请求 |
| `judge.reasoning_effort` | `low` | 判官：情绪判定、图片打分，要快 |

取值 `low` / `medium` / `high`，写空或不认识的词就交给服务端默认。思考型模型会把
`reasoning_tokens` 算进 `max_tokens`，调高强度的同时要留意预算是否够用。

### 3. 群里正常聊天

普通群消息都会进入上下文与 AI 参与判断；不需要先 @ 或叫名字。模型想接当前话题时回复，不想参与时返回内部 `<pass>`，不会向 QQ 发送，也不会写入 assistant 历史。指令和机器人的消息除外；`reply_mode=never` 关闭群 AI，其他模式的 @/唤醒词用于标识是否直接找它。每条普通群消息都会调用模型，忙群可能出现排队。

---

## 目录结构

```
app/                      ← PawBot 项目根（本插件的 app/ 目录）
├── run.py                启动器
├── README.upstream.md    上游项目 README（本文件）
├── data/                   ← 你的地盘，全是纯文本
│   ├── config.json         配置（WebUI 里改，自动写回）
│   ├── persona.md          ★ 人设（直接改，下一句生效）
│   ├── memory/             ★ 记忆区
│   │   ├── facts.md        长期事实
│   │   ├── slang.md        网络用词 / 梗
│   │   └── impression.md   对群友的印象
│   ├── stickers/           ★ 表情包区（图片 + index.json 标签）
│   ├── sessions.json       对话上下文
│   ├── qzone.json          发说说记录
│   └── plugin_state.json   插件启停状态
├── pawbot/
│   ├── wsproto.py          ★ 手写 WebSocket（RFC6455）
│   ├── onebot.py           ★ OneBot v11 协议层（反向 WS + 动作调用）
│   ├── event.py            原始 JSON → 统一事件对象
│   ├── message.py          消息段/CQ 码/纯文本互转
│   ├── bus.py              处理器注册表 + 分发
│   ├── context.py          插件用的 ctx（引用 / @ 都在这里）
│   ├── plugin.py           ★ 装饰器 + 热重载
│   ├── core.py             ★ 主流程（流水线）
│   ├── persona.py          ★ 人设 + 记忆拼装成 system prompt
│   ├── memory.py           ★ 记忆读写与检索
│   ├── stickers.py         ★ 表情包索引与挑选
│   ├── websearch.py        ★ 联网搜索 + 网页抓取
│   ├── tools.py            ★ function calling 工具集
│   ├── agent.py            ★ 多轮工具调用循环
│   ├── qzone.py            ★ QQ 空间发说说
│   ├── llm.py              OpenAI 兼容客户端（支持 tools）
│   ├── safety.py           ★ 拟人化发送（延迟/拆句/节流/静默）
│   ├── store.py            会话存储
│   ├── webui.py            后台 HTTP + SSE
│   └── log.py              日志缓冲
├── plugins/                ← 你只需要动这里
│   ├── basic.py            ping / help / echo / 状态
│   ├── ai_chat.py          ★ 大模型接话（走 Agent）
│   ├── group_tools.py      入群欢迎 / 关键词
│   └── random_interject.py 随机插话
├── web/                    后台页面（单文件 index.html；按 DSH 插件面板使用，本仓库未附带此目录）
└── tools/
    ├── mock_qq.py          假 NapCat，离线调试用
    ├── selftest.py         自检：人设/记忆/表情包/工具循环
    └── make_stickers.py    生成几张占位表情包
```

---

## 写一个插件

放进 `plugins/my_plugin.py`，WebUI 点「热重载全部」，完事。**不用重启进程。**

```python
from pawbot import message as msg
from pawbot.plugin import on_command, on_keyword, on_message, on_notice

__plugin_name__ = "我的插件"
__version__ = "1.0.0"
__description__ = "演示用"


@on_command("签到", aliases=("打卡",), priority=5)
async def sign(ctx):
    await ctx.reply(f"{ctx.sender_name} 签到成功，第 {ctx.user_id % 7 + 1} 名")
    ctx.stop()          # 处理完就别让后面的（LLM）接话了


@on_keyword("摸鱼")
async def touch_fish(ctx):
    await ctx.reply("又在摸鱼。")


@on_notice(notice_types=("group_increase",))
async def hello(ctx):
    await ctx.send([msg.At(ctx.event.user_id), msg.Text(" 欢迎")],
                   group_id=ctx.event.group_id)


@on_message(priority=90)      # 优先级数字越大越靠后
async def fallback(ctx):
    if ctx.state.get("wake"):  # 只有叫了它才跑
        ...
```

### ctx 上有什么

| 属性 / 方法 | 说明 |
|---|---|
| `ctx.plain` | 去掉 @ 和表情后的纯文本 |
| `ctx.raw_text` | 去掉指令名之后的参数文本 |
| `ctx.args` | 按空格切好的参数列表 |
| `ctx.user_id` / `ctx.group_id` / `ctx.self_id` | 各种号 |
| `ctx.sender_name` / `ctx.role` | 昵称、群权限 |
| `ctx.is_group` / `ctx.session_id` | 是否群聊、会话键 |
| `ctx.state["wake"]` | 这条消息算不算叫了机器人 |
| `await ctx.reply(text)` | 回复（默认带引用） |
| `await ctx.send(segments)` | 发送消息段数组 |
| `await ctx.send(text, group_id=123)` | 发到别的群 |
| `ctx.stop()` | 阻断后续处理器 |
| `ctx.bot.store` / `ctx.bot.llm` | 会话存储 / 大模型 |
| `ctx.bot.conn_for(id).call("get_group_list")` | 调任意 OneBot API |

### 装饰器一览

| 装饰器 | 触发条件 |
|---|---|
| `on_command("ping", aliases=("在吗",))` | `/ping` 或直接 `ping` |
| `on_keyword("早安")` | 消息包含关键词 |
| `on_regex(r"^1+$")` | 正则匹配（用 `ctx.matched` 取分组） |
| `on_message()` | 任何消息（兜底，通常给 LLM） |
| `on_notice(notice_types=("group_increase",))` | 入群/退群/撤回/戳一戳 |
| `on_request()` | 加好友、加群请求 |

通用参数：`priority`（越小越先）、`block`（命中后是否阻断）、
`group_only` / `private_only` / `require_at` / `admin_only`。

---

---

## 人设 / 记忆 / 表情包：三个纯本地目录

WebUI 里有一页 **「人设 / 记忆」**，左边列文件、右边直接编辑、下面看表情包。
也可以直接用记事本改——**改完不用重启，下一句话就生效**（按文件 mtime 热加载）。

### `data/persona.md` —— 人设

整个 system prompt 的主体。想让它换个性格、换个说话方式，改这里就行。
文件里分了几块：性格 / 说话风格 / 边界 / 工具使用习惯 / 和群的关系。

### `data/memory/` —— 记忆区

| 文件 | 放什么 |
|---|---|
| `facts.md` | 长期事实（谁是谁、约定、偏好） |
| `slang.md` | 网络用词 / 梗，遇到不懂的它会自己搜了记进来 |
| `impression.md` | 对群友的印象 |

格式就是一行一条 `- [日期] 内容`，人能读，机器能写。
检索用的是字词重叠打分，够用——而且你打开文件就知道它到底记住了什么。

模型自己有三个相关工具：`recall_memory`（翻记忆）、`remember`（写进去）。

### `data/stickers/` —— 表情包区

把图片（png/jpg/gif/webp）丢进去就行，**文件名本身就是标签**。
想更准就在 `index.json` 里写：

```json
{
  "开心.png": {"tags": ["开心", "笑", "好耶"], "desc": "眯眼笑的猫脸"}
}
```

模型用 `pick_sticker("无语")` 挑图，挑中的图会**跟文字一起发**，不是单独一条。
没有素材可以先跑 `python tools/make_stickers.py` 生成几张占位的。

### 找图：全网搜 + Pixiv `pawbot/imagesearch.py`

不用公共图库（Openverse / Wikimedia 那种）——那里面根本没有真人想看的东西。
改成真的去搜索引擎和画师站找。实测（2026-10-20）能用的：

| 图源 | 能用 | 说明 |
|---|---|---|
| **搜狗** | ✅ | 中文网页覆盖最广，JSON 里直接有**原图** `oriPicUrl`，还带 `pic_porn_score` / `pic_sexy_score` 成人评分 |
| **360** | ✅ | 国内老牌图库，图多 |
| **Pixiv** | ✅ | 画师站，中文标签搜得准，带 `xRestrict`（R-18）标记，`/ajax/search/top/<词>` 免登录可用 |
| **Wallhaven** | ✅ | 壁纸/艺术，带 `purity` 分级 |
| **safebooru** | ✅ | SFW 动漫图库 |
| Google / Bing | ❌ | 图片页是 JS 渲染的，HTML 里没有直链 |
| Yandex | ❌ | 返回 1778 字节的验证页 |
| DuckDuckGo | ❌ | 要人机挑战 |

`search_image` 工具多了一个 `source` 参数：

```
auto   先搜全网，再搜 Pixiv
web    只搜搜索引擎
pixiv  只搜 Pixiv（画师向、动漫、二次元、原创插画）
```

模型会自己判断——实测「找张猫的图」→ `source=auto, query="cute cat"`（自己翻成英文），
「找张pixiv上的动漫插画」→ `source=pixiv, query="anime illustration girl"`。

#### Pixiv 的防盗链

`i.pximg.net` **强制 Referer**，没 Referer 直接 403。
所以 Pixiv 的图不能把 URL 丢给 QQ（NapCat 拉不到），
必须机器人自己先下载（带 `Referer: https://www.pixiv.net/`），
再转成 `base64://` 发出去。`imagesearch.to_segment()` 负责这件事。

顺带把 250px 缩略图地址改写成 540px 的大图。

#### Pixiv 登录会话

PawBot 把 Pixiv 登录会话通过 CDP 连接到**PawBot 专用 Chrome**，浏览器数据保存在项目内的
`data/browser/chrome-pixiv`（配置目录不加入版本控制），不会复用或关闭日常 Edge / Chrome。首次使用运行
`/pixiv登录` 会拉起独立资料夹的专用 Chrome，在新窗口登录 Google 和 Pixiv；不要把 Cookie 发给机器人或粘贴进聊天。
保持专用 Chrome 运行，`/pixiv登录 状态` 可检查登录状态。Chrome 136+ 不再允许在默认资料夹开启远程调试端口，因此必须使用独立资料夹。

#### 成人内容分级

搜狗给每张图打了 `pic_porn_score`，Pixiv 给 `xRestrict`。
配合 `spice` 档位：0 档只发 SFW，1 档以上才放行 R18。

```json
"tools": {
  "image_provider": "auto",
  "image_custom_endpoint": "",      // 自建图源，{query} 占位，链尾兜底
  "image_stock_fallback": false     // 内置公共图库当备胎（默认关）
}
```

---

## 联网：它真的会自己搜

这事的难点在于——**普通 chat/completions 接口没有联网能力**。
所以分两条路：

### 路线一：DeepSeek 官方 Search / Fetch（当前默认，不经过模型）

官方有独立的工具接口，跟对话模型无关，flash 也能用（其实根本不用模型）：

```json
"tools": {
  "search_provider": "deepseek",
  "search_base_url": "https://api.deepseek.com",
  "search_api_key": "sk-...",     // 留空则借 llm.api_key
  "search_count": 5
}
```

| 接口 | 作用 | 返回 |
|---|---|---|
| `POST /v1/tools/search` | 搜索，支持 site: / after: / lang: 等语法 | `results[]`：title、url、snippet、summary、date、`rerank_score`（0~3，≥1.5 优质）、`authority`（0~4） |
| `POST /v1/tools/fetch` | 把网页正文抓成 Markdown | `title` + `content` |

机器人的 `fetch_url` 会优先走官方 Fetch，失败再退回本地解析 HTML。
官方接口失败或无结果时，搜索自动降级到本地抓取，不会哑掉。
计费：每千次约 5 元，2026-11-01 起开始收费（今天还免费）。

### 路线二：本地抓搜索结果（兜底）

`search_provider: "local"` 依次尝试 360 → Bing → DuckDuckGo 并解析。
2026-10-21 实测：Bing 的 HTML 已经没有 `b_algo` 结构、DDG 的 html 端点给空壳，
国内能直连又还能解析的是 360，所以 360 排第一。

### 路线三：Responses API 的 `web_search`（默认不用）

这条只有 `deepseek-v4-pro` 会真的发起搜索；`deepseek-v4-flash` / `deepseek-flash`
参数照收但返回空（实测）。本项目的约定是不调用 `v4-pro`，所以 `deepseek_responses`
只保留代码能力，默认不启用。

### 路线二：本地抓搜索结果（默认，不需要 key）

`tools.search_provider = "bing"`（国内可直连）或 `"duckduckgo"`。
本地抓 HTML、解析标题/链接/摘要，然后把结果喂给模型，模型再决定要不要
`fetch_url` 打开某一条读正文。

> Bing 的结果链接是跳转壳，真地址藏在 `u=a1<base64url>` 里，代码里已经解开了。

两条路的差别：路线一是「模型搜完直接给你答案」，路线二是「模型看到原始结果自己判断」。
后者更可控，前者更省事。

### 工具一览

| 工具 | 作用 | 何时触发 |
|---|---|---|
| `web_search` | 上网搜 | 遇到不懂的、需要最新信息 |
| `fetch_url` | 打开网页读正文 | 搜索结果里觉得某条有用 |
| `recall_memory` | 翻记忆区 | 不确定自己记没记过 |
| `remember` | 写记忆 | 值得长期记住的事 |
| `note_person` | 记某人的性格/说话方式 | 觉得这人有点特点 |
| `dirty_joke` | 讲一个荤段子 | 群里起哄要段子 |
| `pick_sticker` | 挑表情包 | 想表达情绪 |
| `save_sticker` | 收藏表情包/梗图 | 群友发了好笑的反應图，或主人让它多存 |
| `[IMG:url]` / `[STICKER:url]` | 媒体标记（工具内部用） | 发送层解析成独立的一条图片/表情包消息 |
| `search_image` | 联网找图（免 key） | 有人说「找图 / 找张XX图」 |
| `bili_video` | 刷 B 站挑视频 | 没话题时自己找乐子 |
| `recall_last` | 撤回自己上一条 | 发现自己说错了 |
| `post_qzone` | 发 QQ 空间 | 你明确要求时 |

一次问答最多 `tools.max_rounds` 轮（默认 3），跑完强制收口，不会死循环。

---

## 它现在真的「看得见、记得住、会插话」

### 感知层 `pawbot/perceive.py`

原来图片全被压成 `[图片]`、引用只有一个 id，等于瞎着聊天。现在：

- 被引用的那条，用 `get_msg` 拉回来，变成「（他引用了这条消息：他昨天说要去上海）」
- 引用的是它自己说的话 → 判定成「在跟它说话」，不再需要 @
- 图片交给 Gemini 看图，变成「（对方发的图长这样：一只绿眼睛的虎斑猫蹲在石台上）」
- 合并转发用 `get_forward_msg` 展开成「暱称：正文」列表（最多 30 条）
- 小程序 / B 站卡片解析出标题、描述、链接
- 话题里提到它（没 @、没叫名字）会标一句「他们好像在说你」，但不会强行接话

> 代价：看图要走一次多模态，只在**真的要回话**时才做，普通群聊不烧这个钱。

### 群事件与生活节奏

- `plugins/group_events.py`：撤回、禁言、管理员变动、退群、加好友、精华、群文件都记进上下文，
  其中一部分按概率（带冷却）接一句话；自己被禁言/解禁会记得并抱怨；好友申请默认不自动同意
- `pawbot/life.py`：星期、时段、2026/2027 法定节假日与调休、作息表（睡得晚、午后犯困、晚上最精神，
  周末整体延后）；主动发言的频率也会随作息浮动，睡觉时段不冒泡
- `pawbot/groupinfo.py`：群名、人数、群主、管理、自己在群里的身分，缓存 30 分钟
- 生日：群里有人说「我生日是10月21号」会记下来，当天在 9:00–22:00 间祝福一次

### 记忆 `pawbot/store.py` + `topic.py` + `people.py`

**上下文按条算，不按轮算。** 群里每一条消息都入库（不只是回过的那几条），
回复时取最近 **300 条** 当语境。

旧做法是超出 8 轮直接丢弃。现在是三段式：

```
summary   旧对话压成一段摘要（越聊越长，有上限）
archive   刚溢出、还没压缩的原文
messages  当前活跃窗口（300 条）
```

攒够 40 条自动触发一次压缩，压失败会还原，不会丢数据。

#### 每轮滚动小结 `pawbot/turns.py`

上面那条压缩只在窗口溢出时才跑，活跃群一晚上未必触发。所以另有一份**每轮更新**的小结：

```
这一轮（用户消息 + 自己的回复）
   ↓ 和「上一份小结」合并
新的小结（≤300 字：在聊什么、谁的观点、约定、未解决的问题、情绪走向）
   ↓ 存进 sessions.json 的 turn_summary
下一轮作为 system 提示放在对话末尾，继续合并
```

- **只进提示词，永远不发群**；提示词里明确写了「不要复述、不要提记录」
- 用判官模型（deepseek-flash）生成，便宜且快；判官不可用时退回主模型
- 失败不写、不移动游标，下一轮会重试同一段
- 开关：`chat.turn_summary`，字数上限 `chat.turn_summary_chars`

#### 分层长期记忆 `pawbot/longmem.py`

滚动小结只覆盖「刚才几轮」，再往前就靠这一层：

```
工作记忆   最近 300 条原始聊天（ctxwin 加权后注入）
经历记忆   每轮小结自动沉淀成一条 episode：时间 / 群 / 参与者 QQ / 关键词 / 重要度
日汇总     同群同一天超过 12 条，较早的合并成一条 daily digest
事实记忆   data/memory/*.md（facts / slang / impression / people）
```

每轮按「关键词重叠 × 时间衰减（半衰期 7 天）× 重要度」检索最多 6 条，注入在对话末尾、当前
消息之前；同一个人说过的经历加权 1.5，同群 +0.5。跨群、跨私聊都能捞到，但注入时会标注来源，
而**私聊内容不会被带进群里**。另外有个保底：话题完全对不上时，至少捞一条跟当前说话人相关的
经历——真人也不是被问了才想起朋友的事。

配置在 `longmem.*`（`half_life_days`、`top_k`、`max_chars`、`max_episodes` 等）。

#### 时间衰减加权 `pawbot/ctxwin.py`

300 条不能原样塞进去——太贵，也没重点。所以按时间给权重：

```
w = exp(-(距今多久) / tau)      tau 默认 6 小时
```

- **越近权重越大**：1 小时前 0.85，6 小时前 0.37，20 小时前 0.04
- **按权重分档截断**：很新的原样留（220 字），一般的留 110 字，很老的只留 40 字
- **超预算从最老的开始丢**，并在开头标一句「更早的 N 条已经省略」
- 间隔超过 10 分钟才标一句「——（隔了 2 小时）——」

时间参照点用**最后一条消息的时间**而不是「现在」——群里可能一晚上没说话，
用 now 会让所有消息权重都趋零，窗口就白留了。

#### 人物档案 `pawbot/people.py`

一个人一份文件，QQ 号当文件名：

```
data/people/123456.md
# 阿明
- 名字：阿明
- 说话方式：短句多，爱用「草」
- 性格：嘴硬但人不坏
```

两种来源：模型自己调 `note_person` 记一条；或者后台观察——某个人说够 30 条，
自动从这些消息里提炼一次性格（覆盖式写入，不堆叠）。

**铁律：档案只影响语气，不会被说出来。**
prompt 里明确禁止「我记得你说过」「上次你提到」「根据我的记录」这类话。
真人记得朋友的事，但不会拿记忆当简历念。

#### 字面形式不进 prompt

这是个容易踩的坑。档案里写着「爱用『草』开头」，如果整行塞进 prompt，
模型会把它当成**指令**去执行，于是每句话都以「草」开头——那不是像真人，是复读机。

所以档案分两份：

```
data/people/123456.md 里的：   说话方式：短句多，爱用「草」开头
注入 prompt 的：                说话方式：短句多
```

`PersonBook.card_for_prompt()` 按**分句**过滤：带引号字面的那个分句丢掉，
同一行的其他分句保留。档案文件本身一个字都不改，你打开看到的就是完整记录。

同时人设里也写了「别养成固定开场白」——但**不点名禁哪个词**，
因为在禁止句里写出那个词，反而会提高它的出现率（否定式启动）。

实测：档案里留着「爱用『草』开头」，连续 4 句回复开场各不相同，一个「草」都没有。

### 读空气 + 心情

- `TopicTracker` 记每个群最近 30 条，抽话题词、算冷场时长、判断该不该插嘴
- `Mood` 是个 0~100 的漂移值，心情低的时候它会回得更短、不主动扯话题

### 主动行为 `pawbot/proactive.py` ← 重点

没人说话的时候它自己找事做：

```
冷场 15~40 分钟（随机，不是固定值）
   ├─ 有话题残留 → 接着聊一句
   ├─ 没话题     → 刷 B 站挑个视频，配一句感想（带视频卡片）
   └─ 心情太差   → 这次跳过
```

守住的边界：只在收到过消息的群活跃、静默时段不说话、每次说完进 300s 冷却、
连续失败会指数退避、群太热闹（一分钟 12 条以上）不插嘴。

```json
"proactive": {
  "enabled": true,
  "groups": [],           // 留空 = 自动用收到过消息的群
  "idle_min": 900,        // 15 分钟
  "idle_max": 2400,       // 40 分钟
  "adaptive": true,       // 按群活跃度缩放
  "adaptive_range": [0.75, 1.5],
  "idle_floor": 300,      // 再怎么样也不低于 5 分钟
  "idle_activity": "auto", // auto / bilibili / search
  "bili_chance": 0.2,     // auto 模式下每次主动发言刷 B 站的概率
  "bili_min_gap": 3600    // 同一个群两次分享视频至少隔 1 小时
}
```

`tools.bili_min_gap` 是同一个上限的工具端版本：它管「聊天时被叫去发个视频」，
两边共用一份计时，主动发过的视频也算数。

#### 活跃度自适应

同样是「冷场 15 分钟」，在死群和热闹群里意思完全不同。所以阈值会按群的**呼吸节奏**缩放：

| 群类型 | 通常间隔 | 算出来 | 实际阈值 |
|---|---|---|---|
| 死群 | 30 分钟一句 | level 0.03 | **12~31 分**（更主动） |
| 普通 | 3 分钟一句 | level 0.33 | 15~40 分 |
| 热闹 | 20 秒一句 | level 1.00 | **22~60 分**（别急着填坑） |

判据是**相邻消息间隔的中位数**，不是累计消息数——累计数只会随时间单调变大，
中位数才代表这个群自己的节奏。取中位数而不是平均，是为了不被「隔夜的 6 小时」带偏。
样本少于 3 条就不乱猜，用中间值。

节奏会写进 `data/topic.json`，重启后接着算，不会每次归零。

WebUI 概览页有一块「冷场自救」，直接显示每个群算出来的阈值和下次最早什么时候开口。

### 荤素档位 `pawbot/spice.py`

尺度是**按群**调的，不是一把全局开关：

| 档位 | 能干嘛 |
|---|---|
| 1 收敛点 | 接荤梗、起哄、讲荤段子、开轻微黄腔 |
| 2 爱色色 | 开黄腔、撩骚、暧昧互动、找擦边的图，明说 R18 也给 |

**没有素聊档了**——原来的 0 档已废除，任何 ≤0 的值一律按 2 爱色色处理。
群里用 `/荤`、`/荤 1`、`/当前尺度` 调，存在 `data/spice.json`。
A 群荤不代表 B 群荤，私聊单独一份。

```json
"spice": {
  "default": 2,
  "allow_joke": true,      // 允许调 dirty_joke 讲荤段子
  "allow_flirt": true,     // 允许暧昧/撩骚式互动
  "topics": [],            // 预置荤段子主题，留空随机
  "owner_only": false      // true = 只有管理员能调档
}
```

`dirty_joke` 工具是**单独调一次模型**、用「讲荤段子」的人设来写——
比指望聊天模型临场发挥稳定得多：要段子的时候就是要段子，不该等它自己决定给不给。
返回的段子会剥掉「段子：」这类前缀，不带免责声明。

**底线只有一条**（写在人设里）：涉及未成年人、偷拍私拍流出的不碰。
其他的不拦。另外人设里也写了「别每句都往荤了带」——荤是调味不是主菜，
整段都在开车跟说教一样让人想退群。

> ⚠️ **图的现实问题**：内置图源（Openverse / Wikimedia）都是公共图库，
> 里面**没有真正的 18+ 内容**——模型翻成 `lingerie model aesthetic` 去搜，
> 搜回来的是风景和艺术照。要真能搜到擦边的图，得自己接一个源：
> `tools.image_custom_endpoint` 填上接口地址（里面用 `{query}` 占位），
> 自定义图源会在内置图源都没结果时兜底。

### QQ 特有动作
- `/撤回` —— 撤回自己在这个群说的最后一条
- 有人说「拍一拍」→ 它拍回去（带 @ 就拍被 @ 的人）
- 模型也能自己调 `recall_last`，说错了会收回去

---

## 引用、主动 @、QQ 空间

### 引用与 @

| 配置 | 说明 |
|---|---|
| `reply.quote` | `auto`（群聊非指令才引用）/ `always` / `off` |
| `reply.at_sender` | `auto`（被叫才 @）/ `always` / `off` |

组装顺序固定：**引用段 → @段 + 空格 → 正文**。
`auto` 的取舍是：**指令回复不引用、不 @**（回个 `/ping` 还引用一下很像机器人），
**闲聊才引用 + @**。

### QQ 空间

走 NapCat 的扩展接口（不属于 OneBot 标准）：

```
action: send_qzone_msg
params: {content, images, ugc_right}
→ {status:"ok", retcode:0, data:{tid:"..."}}
```

**默认整个关掉**，因为这是对外公开发布。要开必须同时满足：

1. `qzone.enabled = true`
2. `qzone.owner_ids` 里有你的 QQ 号（**这个列表空着谁都不行**，防止误开）
3. 没超过 `qzone.daily_limit`（默认每天 3 条）

`qzone.ugc_right`：`1` 公开，`4` 仅自己可见。第一次建议先用 4 试。

---

## 配置项速查（`data/config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `server.port` | 6199 | 反向 WS 端口，要和 NapCat 的 URL 一致 |
| `server.path` | `/ws` | 路径，NapCat 那边填 `ws://ip:6199/ws` |
| `server.access_token` | 空 | 与 NapCat 的 Token 对应，生产环境建议填 |
| `chat.reply_mode` | `at_or_wake` | `never` 关闭群 AI；其他模式都旁听，由模型决定参与，@/唤醒用于标识直接对话 |
| `chat.context_turns` | 8 | 带几轮上下文 |
| `chat.rate_limit_per_min` | 15 | 每群每分钟最多回几条 |
| `chat.quote_reply` | true | 回复时是否引用原消息 |
| `bot.wake_words` | ["大肥鱼","肥鱼","小鲸鱼"] | 唤醒词 |
| `random_interject.enabled` | false | 随机插话开关 |
| `random_interject.probability` | 0.02 | 每条消息的插话概率 |
| `whitelist.group_ids` | [] | 空 = 不限制 |
| `safety.enabled` | true | 拟人化总开关 |
| `safety.typing_cps` | 5.5 | 每条按自己的文字字数计算发送前打字时间（字/秒） |
| `safety.min_delay` / `max_delay` | 0.9 / 6.0 | 每条打字延迟上下限，带随机抖动 |
| `safety.split_gap` | [1.4, 2.8] | 后续每条打字前额外停顿区间（秒） |
| `safety.split_threshold` | 60 | 旧兼容字段，当前只按 `‖` 分条，不按字数硬拆 |
| `safety.per_minute` / `burst` | 12 / 3 | 每分钟上限 / 10 秒突刺上限 |
| `safety.quiet_hours` | [] | 静默时段，如 `[3,7]`；默认关 |
| `safety.silence_chance` | 0.0 | 小概率装死（真人也会没看见） |
| `reply.quote` / `reply.at_sender` | auto / auto | 引用与 @ 策略 |
| `persona.file` / `memory_dir` / `stickers_dir` | data/… | 三个本地目录的位置 |
| `tools.enabled` / `max_rounds` | true / 3 | 工具开关与最多轮数 |
| `tools.time_budget` | 35 | 一轮工具调用的总时间预算（秒），到点就收口回答 |
| `chat.reply_timeout` | 120 | 单条消息从进模型到收尾的上限，超时解锁不会卡死会话 |
| `persona.stickers_max` | 200 | 自存表情包的上限 |
| `people.affinity_half_life_days` / 主人默认 | 14 / 85 | 白名单主人默认好感 85，其他人 50 |
| `chat.settle_sec` / `settle_max` | 1.6 / 4.5 | 收到消息先等这么久（对方可能还有下半句），最多等这么久 |
| `topic.digest_lines` | 12 | 塞进上下文的「群里刚才聊的」条数 |
| `people.update_every` / `update_cooldown` | 15 / 900 | 每人攒多少条提炼一次性格、两次之间至少隔多久 |
| `people.affinity_half_life_days` | 14 | 多久不互动，好感向 50 回落一半 |
| `persona.sticker_fetch` | true | 库里没有贴合表情包时去公开图片搜索找一张存进库 |
| `judge.enabled` / `model` / `base_url` | true / deepseek-flash / api.deepseek.com | 第二个模型：判断情绪增减、给图片打美术分；key 读 `PAWBOT_JUDGE_API_KEY` 或 `judge.api_key`，没配借用 `llm.api_key` |
| `emotion.enabled` / `half_life_min` / `turn_regress` / `max_step` | true / 90 / 2 / 15 | 8 维情绪的回归半衰期（分钟）、每轮回归点数、单轮最大变化 |
| `emotion.judge_timeout` | 4.0 | 等情绪判定的上限，超时用关键词兜底 |
| `tools.pixiv_home_source` / `pixiv_home_top` | auto / 20 | 没有标签时的图源（auto/daily/weekly/monthly/recommend）与前 N 个候选 |
| `tools.image_judge` / `image_judge_min` / `image_judge_timeout` | true / 6 / 10 | 用判官给缩略图打 1-10 分，低于门槛不发 |
| `tools.search_provider` | deepseek | `deepseek`（官方 Search/Fetch）/ `local`（抓 360）/ `bing` / `duckduckgo` / `none` |
| `store.flush_interval` | 2.0 | 会话存储延遲落盤的間隔（秒），不再每條消息重寫整檔 |
| `longmem.*` | half_life 7 天 / top_k 6 / 900 字 | 長期記憶的檢索半衰期、注入條數與字數上限 |
| `life.enabled` / `schedule` / `weekend_shift_hours` | true / 10 段作息 / 1.5 | 作息、週末延後；主動發言頻率跟著浮動 |
| `life.birthday_wish` / `birthday_wish_hours` | true / [9,22] | 群友生日祝福開關與時段 |
| `events.recall_reply_chance` 等 | 0.25 / 0.2 / 0.5 / 0.3 / 0.5 | 撤回、禁言、管理、退群、精華的回話機率（各帶冷卻） |
| `events.auto_accept_friend` | false | 好友申請是否自動同意（預設只記 log） |
| `tools.image_api_key` | 空 | 填了才能 `search_image`（Pexels） |
| `qzone.enabled` / `owner_ids` / `daily_limit` | false / [] / 3 | 空间发帖三道闸 |
| `account.owner_ids` | 从现有 Qzone owner 清单初始化 | 唯一允许修改 QQ 资料的主人清单；填了 QQ 号就能私聊或在群里用普通话说「把昵称改成…」，不需要指令前缀，留空则全部拒绝 |


> 关于封号：`safety` 抹掉的是「秒回、固定长度、固定间隔、有问必答」这些机器特征，
> 但它**不是防封插件**。完整分析见 `../封号风险与应对.md`——
> 真要零风险只有官方 Bot API 或只读方案，而那两条都不给你「群友」形态。

---

## 和 AstrBot 的对应关系

| AstrBot | PawBot | 差别 |
|---|---|---|
| `aiocqhttp` 适配器 | `onebot.py` | 手写，不编译 C 扩展 |
| `EventBus` + 九级流水线 | `core.py` 四级流水线 | 只留限流/白名单/唤醒/分发 |
| `Star` 插件系统 | `plugin.py` 装饰器 | 语法更简单，没有市场 |
| FastAPI + Vue3 WebUI | 手写 HTTP + 单页 HTML | 前端不用构建，直接改 |
| 18 个平台适配器 | 只有 OneBot v11 | 想加平台就照着 `onebot.py` 写一个 |

---

## 踩过的坑（值得记）

**1. 事件处理不能跑在收包循环里。**
最开始我在 `while True: text = await ws.recv()` 里直接 `await` 插件。
结果插件一调 `send_group_msg`，回执要由收包循环读取，而收包循环正卡在插件上——
自己等自己，20 秒后超时。修法是给每条连接配一个 `asyncio.Queue` + worker，
收包只管往队列里丢。这也是 AstrBot 为什么要单独搞一条 EventBus。

**2. 服务端发帧不能加掩码，客户端必须加。**
RFC 6455 规定：客户端 → 服务端必须掩码，反过来必须不掩码。
方向搞反了，对端会直接断开。

**3. NapCat 的 `messagePostFormat` 要选 array。**
选 string 的话 `message` 字段是 CQ 码字符串，`message.py` 里其实也做了兼容，
但 array 更好处理图文混排。

**4. `X-Self-ID` 是认人的唯一凭据。**
反向 WS 握手头里带 `X-Self-ID`（机器人 QQ 号）和 `X-Client-Role`（Universal）。
不读这两个头，多开账号时分不清谁是谁。

**5. Windows 批处理（`.bat`）编码规范。**
- `.bat` 要么**纯 ASCII**，要么存成 **GBK**。UTF-8 中文 + `chcp 65001` 混用时，
  cmd 读文件的字节偏移会错位，`chcp` 之后每行首字符被吃掉，整个脚本从头崩。
- 本项目统一：bat 纯 ASCII（提示用英文）+ `chcp 65001` + `set PYTHONIOENCODING=utf-8`，
  中文交给 Python 输出。
- `echo` 里的 `> < | & ^` 必须用 `^` 转义。`echo A -> B` 会把后半截重定向成一个叫 `B` 的文件，
  屏幕上什么都不显示——之前桌面多出来的「Continue to pixiv」文件就是这么来的。写成 `echo A -^> B`。

**6. Edge CDP 端口不稳定时使用隔离的 Chrome 配置。**
Edge 启动加速可能让后台实例接管带调试端口的启动；PawBot 现在默认使用独立 Chrome 配置
`data/browser/chrome-pixiv`，通过 `--remote-debugging-address=127.0.0.1` 只开放本机 CDP，
不会关闭日常浏览器。Chrome 136+ 要求使用非默认的 user-data-dir 才能开启远程调试端口；
因此首次必须在这个独立窗口重新登录 Google 和 Pixiv。

---

## 许可与风险

- 本项目 MIT，随便改。
- **NapCat 是混合许可**，README 明确写了「未经作者授权不得基于 NapCat 代码二次开发」。
  这里是「调用它」，不是「改它」，通常没问题，但你要分发自己的协议端就得注意。
- 用非官方协议登录 QQ **存在被风控/封号的风险，无法彻底消除**。
  三条能显著降低概率的做法：
  1. **用老号**（注册久、有正常聊天记录、绑手机）——比任何配置优化都管用
  2. **登录环境固定**（别频繁重建容器、别多号同机、别让 IP 每天准点变）
  3. **行为拟人**（本项目的 `safety.py` 已经把「秒回/定长/定间隔」抹掉了）

  完整分析（四条路线 + 官方 API 限额表 + 封了怎么办）：见 `../封号风险与应对.md`。

---

## 相关项目

完整调研见 `../原理剖析.md`。核心几个：

| 项目 | Star | 语言 | 定位 |
|---|---|---|---|
| [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) | 42.2k | Python | 一站式 IM Agent 平台 |
| [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ) | 11.1k | TypeScript | 基于 NTQQ 的协议端 |
| [nonebot/nonebot2](https://github.com/nonebot/nonebot2) | 7.8k | Python | 插件生态最强的框架 |
| [langbot-app/LangBot](https://github.com/langbot-app/LangBot) | 18.3k | Python | 生产级 LLM 机器人 |
| [LLOneBot/LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot) | 3.7k | TypeScript | LiteLoaderQQNT 插件式协议端 |
| [LagrangeDev/Lagrange.Core](https://github.com/LagrangeDev/Lagrange.Core) | 3.0k | C# | 纯协议实现，不依赖 QQ 客户端 |
| [botuniverse/onebot-11](https://github.com/botuniverse/onebot-11) | 711 | HTML | OneBot 11 标准文档 |
