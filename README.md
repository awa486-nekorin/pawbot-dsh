# @local/pawbot —— PawBot 受管服务 + 大肥鱼看 B 站

一个 DSH Host bundle，把 **PawBot（QQ 机器人）** 封装成 DSH 受管服务，并把原本独立的
`@local/pawbot-bili-watch`（大肥鱼看 B 站）合并进同一个 bundle。装这一个 bundle 就有全部功能。

**2026-10-27 新增「大肥鱼的大脑」**：她的一轮对话可以不再直连外部 `/chat/completions`，
而是交给 **DSH 进程里的一个真 Agent** 跑——完整人设、能用 `pwsh / 读文件 / 写文件 / 搜索`
这些电脑工具，还能回调 PawBot 自己的 30 个业务工具（表情包、B 站、记忆、空间……）。
开关是 PawBot 配置 `llm.provider = "dsh"`，随时可回退。细节：[docs/BRAIN.md](docs/BRAIN.md)、
实测记录：[docs/BRAIN-VERIFICATION.md](docs/BRAIN-VERIFICATION.md)。

## 这个仓库包含什么

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| 插件本体 | `index.js` / `client.js` / `lib/` / `bili-watch/` / `locale/` | DSH Host bundle：PawBot 受管服务 + 「大肥鱼看 B 站」 |
| **PawBot 本体** | `app/` | PawBot 的 Python 源码：`run.py`、`pawbot/`（核心包）、`plugins/`（内置插件）、`tools/`（测试与辅助脚本）、`data/`（**模板**：配置、人设、空记忆） |
| 部署说明 | [DEPLOY.md](DEPLOY.md) | 云服务器（Linux）从零部署清单：NapCat、SESSDATA、目录与权限 |

**PawBot 源码已经内附在本仓库的 `app/` 里**——装这个插件就有 PawBot 本体，不需要另外下载/上传。
`app/data/` 里只有模板：`config.json` 的密钥字段全部为空、`memory/*.md` 是空文件、`people/` 与 `diary/`
是空目录（只有 `.gitkeep`）。真实会话、群友档案、图片缓存、表情包、密钥都不进仓库（`.gitignore` 已排除）。

## 结构

```
index.js                      # pawbot 受管服务插件（apply/inject/Config + 11 个工具 + /pawbot-ui 代理路由）
client.js                     # 设置页面板（PawBot 标签页：状态/API/设置/人设）
lib/hostdeps.js               # 从宿主树拿 schemastery / defineTool / 其它 @deepseek-ai/*（拿不到就降级）
lib/config-utils.js           # 纯函数：遮罩 secrets / 去遮罩值 / 深合并 / 区段过滤 / 差异
lib/webui-proxy.js            # /pawbot-ui/* 同源代理路由（status/config/persona/reload-plugins/send/restart）
lib/dsh-brain.js              # ★ 大肥鱼的大脑：进程内 Agent + complete 人设 + 电脑工具 preset + PawBot 工具代理
test/offline.mjs              # 离线单测（node test/offline.mjs）
test/dsh-brain-offline.mjs    # 大脑桥离线单测（协议/鉴权/限额，不启 DSH）
docs/BRAIN.md                 # 大脑设计/协议/配置/验证/回滚
docs/BRAIN-VERIFICATION.md    # 大脑 2026-10-27 本机实测记录（含 QQ 全链路证据）
docs/PROJECT-STRUCTURE-AND-WORKFLOW-2026-10-29.md  # ★ 结构/目录/流程/部署/坑（新窗口先读）
docs/por-20261027/            # 人设交付包归档 + 落库收据（回滚备份也在这里）
bili-watch/                   # 从 @local/pawbot-bili-watch 原样复制过来的插件
  index.js                    #   挂成 @local/pawbot/bili-watch（subpath export）
  lib/…                       #   WBI 签名 / buvid 指纹 / view / player / subtitle / reply 等
  locale/…, icon.svg
app/                          # ★ PawBot 本体的 Python 源码（自包含，见下节）
  run.py                      #   启动器
  requirements.txt            #   pillow / websockets 两个第三方依赖
  pawbot/                     #   核心包（OneBot / LLM / 记忆 / 发送层 / WebUI …）
  plugins/                    #   内置插件（basic / ai_chat / life_events / pixiv …）
  tools/                      #   测试（test_*.py）+ 辅助脚本（mock_qq / selftest …）
  data/                       #   运行时数据（只有模板进库：config.json / persona.md / 空记忆）
  README.upstream.md, 使用指南.md, ARCHITECTURE.md   # 上游文档
locale/en.json, locale/zh.json, icon.svg   # 本 bundle 的显示 metadata / 图标
cordis.patch.yml              # 插入两列：pawbot 与 pawbot-bili-watch
DEPLOY.md                     # Linux 云服务器部署清单
```

## 与旧 bundle 的关系（重要）

`@local/pawbot-bili-watch` 原本是独立 bundle，会插入 `pawbot-bili-watch` 这一列并注册
`bili_watch` / `bili_watch_status`。若在本 bundle 里再插一列同 id / 同名模块，就会
**注册第二个 `bili_watch`**（tools 注册表对同层重名会失败），造成冲突。

因此本 bundle 采用「**复制进 bundle 当第二列**」的做法：

- `bili-watch/index.js` + `lib/` 原样复制进来，挂成 **subpath `@local/pawbot/bili-watch`**（id `pawbot-bili-watch`）。
- 安装本 bundle 后，要**移除旧的 `@local/pawbot-bili-watch` bundle**，让 `bili_watch` / `bili_watch_status`
  只有一份（来自本 bundle 的 subpath）。
- 数据目录：插件把 `dataRoot` 的**绝对路径**传给 PawBot；PawBot 端 `tools.bili_watch_dir` 的默认值
  已改成相对值 `pawbot-bili-watch-data`（避免写死任何 Windows 路径），实际以插件传入的绝对路径为准。

> 选「复制」而不是「引用已安装包」的原因：引用已安装包会在同一个 profile 里出现两个
> 解析到同一模块的列 → `bili_watch` 注册两次 → 冲突；复制进 bundle 才能做到「一个 bundle 全功能」，
> 且只保留一份 bili_watch 工具。

## Config（均可在 DSH 插件设置页改）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `projectDir` | `D:/WorkBuddy/2026-10-20-00-14-25/pawbot`（旧默认） | PawBot 项目根目录（`run.py` 所在）。**PawBot 已内附于本仓库的 `app/`，把这里指到插件安装目录下的 `app/`**（Linux 例如 `/opt/pawbot-dsh/app`） |
| `pythonExe` | `py` | 用哪个 python 启动（`py` = Windows launcher，等同 start.bat 的 Python313） |
| `autostart` | `true` | apply 时是否自动启动 |
| `restartOnCrash` | `false` | 进程意外退出后是否自动重启（主动 stop 不触发） |
| `logLines` | `200` | `pawbot_status` 默认返回的日志行数 |
| `webuiHost` | `127.0.0.1` | 健康检查请求的 WebUI host（对应 PawBot `webui.host`） |
| `webuiPort` | `6185` | 健康检查请求的 WebUI port（对应 PawBot `webui.port`） |
| `webuiToken` | `""` | WebUI token（PawBot 默认空；设了走 `Authorization: Bearer`） |
| `healthTimeoutMs` | `3000` | 健康检查 HTTP 超时（毫秒） |

### 模型规则（硬性）

- ⛔ **不允许任何 V4 Pro 的调用，只能用 V4 Flash**（用户 2026-10-25 定：V4.1 Flash 比 V4 Pro 强，而且 pro 烧 token）。
- 设置位置：**DSH 设置 → 插件 → `@local/pawbot/bili-watch`** 里的 `model` 与 `allowedModel`，两个默认都是 `deepseek-flash`。
- 不是只改默认值：[lib/model-guard.js](bili-watch/lib/model-guard.js) 在**每次发请求前**过一遍，
  命中 `deepseek-v4-pro` / `v4_pro` / `V4-Pro` / `v4 pro` / `deepseek-v4pro` 任何写法就换成 `allowedModel` 并写日志。
  **`allowedModel` 本身也不能填 v4-pro**——填了会回退到 `deepseek-flash`，避免把它当后门绕过规则。
- 实际用了哪个模型：`bili_watch_status` 会返回 `model` / `model_requested` / `model_allowed` / `model_blocked` / `model_block_reason`。

## 进程管理

- **启动**：`spawn(pythonExe, ['run.py'], { cwd: projectDir })`，stdout/stderr 进环形缓冲区
  （上限 5000 行、按行存、带时间戳），并设 `PYTHONUNBUFFERED=1`、`PYTHONIOENCODING=utf-8`
  （与 start.bat 一致，保证中文日志不乱码、即时输出）。
- **停止 / dispose / restart**：收掉「整棵子进程树」。
  - **Windows**：`taskkill /PID <pid> /T /F`。原因见 `index.js` 注释——`py run.py` 是
    `py.exe`(launcher)→`python.exe` 两层；且 Windows 上 Node 的 `kill('SIGTERM')` 只是
    `TerminateProcess`（无信号语义），`run.py` 的 signal handler 在 Windows 收不到。
    「优雅停止」在 Windows 上不可得，只能用 taskkill 原子收树，保证不留孤儿 python。
  - **POSIX**：先 `SIGTERM`（`run.py` 会优雅关闭并 flush 存储），超时再 `SIGKILL`。
- **回收**：所有子进程、重启退避 timer 都经 `ctx.effect` 回收，dispose 时同步杀树。

## 工具

| 工具 | 说明 |
| --- | --- |
| `pawbot_status` | running / pid / uptime / spawn 次数 / 最近退出 / 最后错误 / 日志尾部（行数 = `logLines`） |
| `pawbot_logs` | 参数 `lines`（默认 80，最多 500）；返回日志**尾部**（不是全文），每行带 stream |
| `pawbot_restart` | 重启；**副作用**：杀整棵树→重新 spawn，正在回的话会丢、NapCat 断线（通常自动重连）、服务短暂中断 |
| `pawbot_health` | 真的请求 PawBot WebUI 的 `GET /api/status`；请求不到就只回进程层状态，不自己造 API |
| `pawbot_config_get` | 读 config（可 `section` 只回某区段）；secrets 一律遮罩成 `***已设定***`，`reveal:true` 才回真值（有泄漏风险，除非明确要看别用） |
| `pawbot_config_set` | 改 config（`patch` 深合并、直接写 `data/config.json`）；返回改动前后「键名」差异，不列值；遮罩值会被丢掉 |
| `pawbot_brain` | 大肥鱼大脑自检：`action=status` 看通不通（模型路由/沙箱/工具白名单）、`selftest` 真跑一轮、`turn` 用自定义 system/prompt 跑一轮（不发 QQ） |

## 她的大脑可以长在 DSH 里（2026-10-27）

默认 `llm.provider` 是空的（= 直连 `llm.base_url` 的 `/chat/completions`，老行为）。
把它改成 `dsh` 之后，一次 QQ 对话变成：

```
QQ → PawBot(Python) → POST /pawbot-dsh-brain/turn（loopback + 一次性 token）
   → DSH 进程内新 Agent（人设 = PawBot 整段 system；工具 = 电脑工具 preset + PawBot 30 个工具代理）
   → 最终文本 + 媒体段 → PawBot 表达层（style / ‖ 分条 / 表情包 / @ 引用 / TTS）→ QQ
```

能做什么（本机实测）：

| 场景 | 旧链路 | DSH 大脑 |
| --- | --- | --- |
| 普通聊天 | 外部 API，1~3s | 进程内 Agent，3.7~5.5s |
| 「看看这台电脑几个核、内存多少」（主人） | ❌ 只能编 | ✅ 真跑 `pwsh` 读真实数据（17~41s，因为她跑了好几轮） |
| 同一句由群友说 | — | 不给电脑工具，她会如实说「真跑不了」（不会编） |
| 表情包 / B 站 / 记忆 / 空间 | ✅ | ✅ 通过 PawBot 工具代理照旧（写类工具只认主人） |
| 需要 API key | 要 | **不要**（用 DSH 的模型凭据） |

安全默认：

- `brainOwnerOnly = true`：**电脑工具只给主人**（`account.owner_ids` 里的人；设置页「账号 → 主人 QQ 名单」）。
  没配置主人＝谁都没有电脑工具；群友轮次只给 PawBot 业务工具。
- `brainSandbox = workspace-write`（能读、能跑命令、只能写工作目录/临时目录）、
  `approval = never`（不弹窗，越界直接拒）；白名单 `pwsh,read,write,edit,glob,grep,web_search,web_fetch`。
- 执行闸 fail-closed（guard 装不上就本轮失败）、写类 PawBot 工具按发起人再验一次、
  中断/超时不算成功（残句不会发进群）。
- 要全盘读写就把 `brainSandbox` 改成 `danger-full-access`——等于把整台电脑交给一个会读群聊的大脑，请想清楚；
  更保守就 `brainSandbox=read-only` 并把 `pwsh`/`web_*` 从白名单拿掉。

回退：设置页「主模型：大脑走哪儿」改回「直连 API」即可（热生效）；彻底关：宿主 Config `brainEnabled=false`。
设计/协议/配置/验证/回滚：[docs/BRAIN.md](docs/BRAIN.md)；实测证据：[docs/BRAIN-VERIFICATION.md](docs/BRAIN-VERIFICATION.md)。

## 对话测试窗口（不装 QQ 也能跟她聊）

`app/tools/chat_window.py`：一个零依赖的 Tkinter 小窗口，**假装自己是 NapCat**，通过 OneBot 反向 WebSocket
连到 PawBot 的 `ws://127.0.0.1:6199/ws`——所以走的是和真 QQ 完全一样的链路
（人设 / 记忆 / 大脑 / 工具 / 表情包 / ‖ 分条都会真实发生）。

打开方式（任选）：

```powershell
# A. 双击
D:\WorkBuddy\pawbot-dsh\app\对话测试窗口.bat

# B. 命令行
cd D:\WorkBuddy\pawbot-dsh\app
py tools\chat_window.py

# C. 无窗口自检（发一句、打印回复后退出；适合脚本/排查）
py tools\chat_window.py --check "今天好累啊"
```

窗口里：

| 控件 | 作用 |
|---|---|
| 输入框 + 发送 | Enter 发送、Shift+Enter 换行；群聊自动 @ 她 |
| 群号 / 我的QQ / 昵称 | 模拟哪个群、谁在说话；保存在 `app/data/chat_window.json`（不进仓库） |
| 私聊测试 | 勾上后按私聊发（不需要 @），适合测主人专属能力 |
| 设为主人 / 取消主人 | 把「我的QQ」写进/移出 PawBot 的 `account.owner_ids`；**只有主人能用电脑工具** |
| 清空 | 只清窗口，不动 PawBot 的记忆 |

已知注意：

- 「在吗 / ping / 延迟」会命中基本插件的 `pong` 指令，不走 AI——想测人设直接说别的话。
- 表情包/图片在窗口里显示成 `[图片/表情包]`（Tk 默认只认 PNG/GIF，不想为了测试引入图片库）。
- 她记得之前的聊天（PawBot 记忆），所以第一次回复可能是接着旧上下文。
- 窗口只是测试客户端：它不会改 PawBot 的记忆/人设/配置（除了你点「设为主人」）。

## API 与密钥统一页（2026-10-27）

以前 15 个密钥散在「高级 → 9 个原始区段」里。现在设置面板新增 **「API 密钥」** 标签页，按服务聚成卡片：

| 分组 | 内容 |
| --- | --- |
| 主模型（聊天 + 看图） | `llm.model / api_key / base_url / reasoning_effort / timeout`，视觉模型单独一组 |
| 评审 / 情绪判官 | `judge.*` + `emotion.judge_*`（留空＝沿用主模型 key） |
| 联网搜索 / 图库 | `tools.search_*`、`tools.image_*` |
| Pixiv / B 站凭证 | `pixiv_sessid`、`pixiv_refresh_token`、`bili_jct`、凭证文件 |
| 语音（听 + 说） | `speech.*` 与 `tts.cloud_*` / `tts.url` |
| 表情包描述（看图） | `media.sticker_vision*`：收新图时自动写画面描述的接口 |
| 连接与网页令牌 | `server.access_token`、`webui.token`（改完要重启 PawBot） |

语义和「设置」页完全一致：留空＝不改、遮罩值不回显、只送 diff、清空需二次确认；顶部实时显示「密钥已配置 x / 15」。

## 表情包 2.0 与语气词收敛（2026-10-27）

- **每张表情包都有真实描述**：`data/stickers/index.json` 的条目从 `{tags}` 升级为 `{tags, desc, mood, desc_source}`；入库时用看图模型（默认 togemini `gemini-4-argon-medium`）写「画面/动作/配字」，失败用标签拼人话兜底。工具 `app/tools/describe_stickers.py` 可给旧库补描述（`--dry` / `--apply`，写前备份）。
- **按描述挑图**：`pick_sticker(query)` 的 query 现在是「想发的画面/情绪描述」，匹配时 tags +2、desc/mood/文件名 +1；系统提示里会列当前情绪下最贴的 8 条 `描述（tags）` 供她挑。
- **不再几乎每条都发**：`media.sticker_chance` 默认 0.95 → **0.12**，`persona.sticker_gap` 默认 [1,1] → **[3,6]**；只有**情绪强烈**（默认「强烈」）或主导情绪是 **兴奋/烦躁/感动** 时才绕过随机闸主动发（`media.sticker_force_*` 可调）；对方明确说「发个表情包」也会放行。
- **语气词收敛**：`reply.tone_trim`（默认 0.7）按确定性哈希削句末「啊/嘛/啦/呀/哦/哟」；「呢/吧/吗」作为疑问/商量语气保留（削了会改意思）；「干嘛/哎呀」这类词不误伤。人设里也把「语气词十句最多一两个」写进规则。

## 心情矩阵：概率化决策模型（2026-10-27）

心情从「关键词/聊天模型判官」升级为可选的**类 JEV 决策模型**：
POST https://openrouter.ai/api/alpha/decisions，模型 perplexity/pplx-decider-v1.2。
一次请求问三个问题：shift（会不会明显变化，noul 概率）、emotion（八种情绪 + none，choice 概率分布）、
intensity（强度，score 0~1），拿到概率后映射成情绪增减；失败自动退回关键词/判官，聊天不中断。

`ini
emotion.decider_enabled = true
emotion.decider_model = perplexity/pplx-decider-v1.2
emotion.decider_base_url = https://openrouter.ai/api/alpha/decisions
emotion.decider_timeout = 12
emotion.decider_min_shift = 0.5
emotion.decider_weight = 1.0
`

- Key 不写进仓库：优先面板 emotion.decider_api_key，否则环境变量 PAWBOT_DECIDER_KEY /
  OPENROUTER_API_KEY，或专用文件 ~/.pawbot/decider.key（本机已放好；升级不碰）。
- 单独试一句：py tools\decider_once.py --text "群友：你今天怎么这么可爱" --addressed。
- 完整请求/响应、映射规则、费用与边界见 [docs/DECIDER.md](docs/DECIDER.md)。

## 升级不丢数据（2026-10-27）

插件更新/重装最怕把 `app/data`（人设、记忆、`config.json` 里的 API key）盖掉。现在有两层保护：

1. **版本快照（默认开）**：插件版本一变，自动把 config/persona/记忆快照到
   `data/backups/plugin-update-<版本>-<时间>/`，保留最近 5 份。
   手动：`pawbot_data_backup action=backup`；查看：`action=status`。
2. **外部数据目录（强烈推荐）**：DSH 设置 → 插件 → PawBot 的 Config 里把 `dataDir` 填成
   插件目录外的路径（如 `D:/PawBotData`），重载插件。插件会把现有数据**不覆盖地**迁过去，
   再把 `app/data` 换成指向它的联接；以后升级只换代码，碰不到数据。

恢复步骤、留档目录（`data.migrated-*`）与边界见 [docs/DATA-PERSISTENCE.md](docs/DATA-PERSISTENCE.md)。

## 云端共享（人设 / 记忆 / 配置，2026-10-27）

把「人设 + 记忆 + 配置（含 API key）」打包同步到你自己的云服务器，或在另一台机器上拉回来。
适配器三种：`local`（本机/挂载盘）、`webdav`（坚果云 / Nextcloud）、`http`（自建端点）。
默认关闭（`sync.enabled=false`），不会自动联网；不自动 push，只有 CLI/你点操作才上传。

```powershell
cd D:\WorkBuddy\pawbot-dsh\app
py tools\cloud_sync.py status
py tools\cloud_sync.py test                  # 只测连通性
py tools\cloud_sync.py push --dry            # 只打包，不上传
py tools\cloud_sync.py push
py tools\cloud_sync.py pull --dry            # 只看远端有什么/会改什么
py tools\cloud_sync.py pull                  # 默认只写 data/config.synced.json
py tools\cloud_sync.py pull --overwrite-config
```

- **含 API key 的 config 必须加密**：设 `sync.passphrase`（AES-256-GCM + PBKDF2，需 `cryptography` 或 `pycryptodome`；都没有会明确报错）。未加密时只允许同步人设/记忆，`include_config=true` 会直接拒绝（fail-closed）。
- **拉取不粗暴覆盖**：默认只落地 `data/config.synced.json` 并给出 key 级 diff；加 `--overwrite-config` 才覆盖，覆盖前自动备份到 `data/backup-sync-<时间>/`，并保留本机 `sync` 段（不会把本机同步凭据冲掉）。
- 启动自动拉取：`sync.auto_pull_on_start=true` 时后台拉一次，失败只记日志、不影响启动。
- 安全：日志/返回值/status 都不含 password/token/passphrase；拒绝路径穿越/符号链接；密钥会上传到你的云，**必须信得过那台服务器**；passphrase 丢了无法恢复。
- 配置字段在面板「设置 → 高级 → 云端同步」或「API 密钥 → 云端同步」组里填；完整说明见 [docs/CLOUD-SYNC.md](docs/CLOUD-SYNC.md)。

## 控制面板（在 DSH 内）

不用再开 PawBot 的 `http://127.0.0.1:6185` 网页——所有控制都收进 DSH 的 Web GUI（同源，无 CORS）。
本插件在 `inject: ['tools', 'webServer']` 下注册一条 prefix 路由 `/pawbot-ui/`，浏览器请求它，
它内部再去请求 PawBot 的 `http://<webuiHost>:<webuiPort>/api/...`（PawBot 网页只当内部通道保留）。

### 面板在哪、能做什么

重启 DSH 后：**左侧「设置」→ 导航栏多一项「PawBot」**（`settings.section` slot，`id: pawbot`），五个标签页：

| 标签页 | 内容 |
| --- | --- |
| 启动 | 一键安装 / 连 NapCat / 启动 PawBot 的四步引导、日志、重载 |
| 状态 | running / PID / uptime / spawn 次数 / PawBot `/api/status`（连接、会话、插件数、心情）；按钮：重启 PawBot（两段式确认）、重载插件；每 5 秒刷新，离开面板即停 |
| API 密钥 | 15 个 key / endpoint / 模型按服务分组统一填写；顶部显示密钥配置统计 |
| 设置 | **全部 37 个区段、331 个键**分组表单（bool→开关、number→数字框、string→输入框/textarea、数组与对象→JSON），有搜索框、折叠、原始 JSON 模式；**只送出改动过的键**；机密栏显示「已设置（不改）」 |
| 人设 | 直接编辑 `data/persona.md`（保存前两段式确认，显示字数）；发测试消息、看上下文与记忆 |

| 路由（同源，相对 DSH GUI 根） | 方法 | 作用 |
| --- | --- | --- |
| `/pawbot-ui/status` | GET | PawBot `/api/status` + 本插件进程信息（追加 `process` / `http_ok` 两种命名，给面板用） |
| `/pawbot-ui/config` | GET | 读整份 config，secrets 遮罩；回 `{ config, masked_keys }` |
| `/pawbot-ui/config` | POST | 写 partial config（PawBot 深合并）；**收 `{patch:{…}}` 或直接 partial 两种 body**；遮罩值会被丢掉 |
| `/pawbot-ui/persona` | GET | 读人设文字（同时回 `text` 与 `content`） |
| `/pawbot-ui/persona` | POST | 写人设；收 `{text}` / `{content}` / 纯字符串；上限 200KB |
| `/pawbot-ui/reload-plugins`（别名 `/pawbot-ui/plugins/reload`） | POST | 热重载全部插件 |
| `/pawbot-ui/send` | POST | `{ target, message, is_group }` 以机器人账号**真的**发消息（响应标 `sent:true`） |
| `/pawbot-ui/restart` | POST | 用本插件 `stopChild + startChild` 重启（不绕 PawBot webui） |
| `/pawbot-ui/context`、`/pawbot-ui/memory` | GET | 上下文可视化 / 记忆总览 |

**安全**：返回给浏览器的 config 一律把 `api_key` / `token` / `access_token` / `cloud_key` /
`*secret*` 等遮成 `"***已设定***"`，且写回时把「值等于遮罩字符串」的键丢掉——**API key / token 不会进浏览器**。

**人设文件路径注意**：PawBot 的 `/api/file` 只允许读写 `data/` 底下的相对路径，所以人设文件
（项目根的 `data/persona.md`）在 API 里的路径是 `persona.md`，**不是** `data/persona.md`。

### 如何验证（重启 DSH 后）

1. 状态：`curl http://127.0.0.1:57967/pawbot-ui/status`
   （PowerShell：`Invoke-RestMethod http://127.0.0.1:57967/pawbot-ui/status`）
2. 读设置（secrets 应被遮成 `***已设定***`）：`curl http://127.0.0.1:57967/pawbot-ui/config`
3. 读人设：`curl http://127.0.0.1:57967/pawbot-ui/persona`
   （改设置：`curl -X POST -H "Content-Type: application/json" -d '{"llm":{"temperature":0.7}}' http://127.0.0.1:57967/pawbot-ui/config`）

> 注：`57967` 是当前 DSH Web GUI 的 port，请以实际 `ctx.webServer.getOrigin()` / 浏览器地址栏为准。

## B 站这半（`bili-watch/` 子插件）

从旧的 `@local/pawbot-bili-watch` 原样搬进本 bundle（旧 bundle 已移除），能力边界：

- 抓取：`view` / `player`(字幕) / `reply`(热评) / `popular` / `search`(支持 `order` / `days` / `num`)，
  自带 WBI 签名、buvid 指纹引导、412 风控重试。
- 判重三层落地：`reports/*.json`（看过）+ `shared.json`（PawBot 写，已分享）+ `seen.json`（插件写）。
- 每个视频看完写 `reports/<bvid>.json`（给模型看的契约）＋可选 `notes/<bvid>.md`（给人看的笔记）。
- 队列 `requests/*.json` 支持 `kind = bvid / keyword / auto / up`，`extra` 可带 `order / days / num`。

### 凭证（SESSDATA）：这是「能不能读字幕」的唯一开关

三个来源，插件按 `env > credential.json > 设置页 sessdata` 取用，**任何日志 / 报告 / 工具返回都不含它的值**：

1. 环境变量 `BILI_SESSDATA`（`cookieEnv` 可改）；
2. `<dataRoot>/credential.json` = `{"sessdata": "...", "updated_at": "..."}`
   （PawBot 侧有一个不暴露给模型的入口：`pawbot.bili_watch.save_sessdata(value)`）；
3. 本插件设置页的 `sessdata`（secret 字段）。

没有凭证时：字幕拿不到，报告的 `watched_level` 会停在 `desc_comments`（只看简介＋评论），
这是**如实降级**，不是坏了。`bili_watch_status` 会回 `has_credential` 与 `credential_source`（不给值）。

### 已知限制：UP 主定向（`kind = "up"`）

- 投稿列表接口 `x/space/wbi/arc/search` 在未登录时被风控挡：旧接口回 `-412`，wbi 接口回 `-352`。
- 因此 `up` 目前先试投稿接口，失败再降级「用 UP 名字搜索 → 只留 `author` 对得上的视频」。
  降级路径**常常捞不到**（B 站搜索对 UP 名回的是「别人聊到他」的视频，`author` 不是他本人）。
- 结论：**要真正定向某个 UP，需要先配 SESSDATA**；在那之前 `up` 会如实记「没捞到投稿」，不会假装成功。

## 诚实说明

- **Windows 上没有真正的「优雅停止」**：`run.py` 的 graceful shutdown 只在 POSIX 信号下生效；
  本插件在 Windows 上用 taskkill 收树，PawBot 的周期性 flush（store 2s）仍会尽量保全数据，
  但「停机前强制 flush」这一步在 Windows 上无法触发。
- **`pawbot_health` 依赖 PawBot 已启动且 WebUI port/host 正确**；PawBot 没连上 NapCat 时它仍会
  正常起 WS/WebUI，所以 `process_running=true` 不代表 QQ 已上线（要看 `/api/status` 的 `connections`）。
- **大肥鱼的大脑（`llm.provider=dsh`）是「换脑」不是「加速」**：每轮在 DSH 里新建一个 Agent，
  延迟 3.7~5.5s（纯聊天）到 40s+（多次电脑工具），比直连 API 慢；每个 Agent 会在
  `~/.dsh/sessions/` 落一份 7~50KB 的日志（DSH 的持久化语义，当前没有 delete API）。
  图片暂时进不了大脑（降级为「只按文字回」）。没有真 NapCat 的机器上，QQ 链路只能用协议端探针验证。
  完整清单见 `docs/BRAIN.md` §7 与 `docs/BRAIN-VERIFICATION.md` §5。
- 本 bundle **自带 `app/`（PawBot 源码 + `data/` 模板）**；运行时只写 `app/data/` 下的数据文件，
  **不改 `app/pawbot/` 等源码**、**不改 profile 的 package.json / cordis.patch.yml**、**不装其他 npm 包**。

## 安装 / 验证

PawBot 本体**已内附在 `app/`**，安装时不需要另外准备 Python 项目。装完要做的是让插件指向它：

1. `plugin_manager` `action: install_bundle`，`target` 为本目录绝对路径（GitHub 上直接装也可以：
   `dsh plugin add github:awa486-nekorin/pawbot-dsh`）。
2. 把插件设置里的 `projectDir` 指到**本 bundle 内的 `app/`**（例如 `/opt/pawbot-dsh/app`；
   装成 npm 包时就是 `node_modules/@local/pawbot/app`），`pythonExe` 用 venv 里的解释器或 `python3`。
   依赖只有 `pillow` / `websockets`：`python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt`。
3. 读回 `application` 与 `warnings` 决定是否生效。
4. `cordis_inspect_query`（Config.listConfigs）确认 `@local/pawbot` 列存在。
5. `Tool.listTools` 确认 `pawbot_status` / `pawbot_logs` / `pawbot_restart` / `pawbot_health` 已注册，
   且 `bili_watch` / `bili_watch_status` 只有一份。
6. 移除旧的 `@local/pawbot-bili-watch` bundle（避免 bili_watch 注册两次）。
7. 云服务器部署（NapCat、SESSDATA、端口）见 [DEPLOY.md](DEPLOY.md)。

## 11. 服务器上的「自己刷 B 站」（bili_scroll）

`app/pawbot/bili_scroll.py` 让 PawBot 自己刷站：搜/热门 → 去重 → **读字幕原文**（走内置的
`app/pawbot/bili_mcp/`，即 [bilibili-mcp](https://github.com/adoresever/bilibili-mcp)，MIT）→ 判断值不值得看
→ 每条写一份笔记到 `app/data/bili_notes/<标题>-<YYMMDD>.md`。

- **可选依赖**：字幕要 `app/requirements-bili.txt`（`mcp<2` + `bilibili-api-python`）。不装也能刷，
  只是没有字幕、笔记降级成标题+简介。
- **凭据**：SESSDATA 从 `tools.bili_sessdata` / `bili.sessdata` 配置或 bili-watch 共享目录的
  `credential.json` 读取，同步到 `app/pawbot/bili_mcp/bili_credential.json`。
  ⚠️ 那个文件已在 `.gitignore` 里，**永远不要提交**。
- **配置**：`app/data/config.json` 的 `tools.bili_scroll_*`（主题、每轮条数、最低播放量、时长上限、
  笔记目录、定时时段 `bili_scroll_times`、报告发给谁）。
- **工具**：`bili_scroll`（现在刷一轮）、`bili_notes`（看笔记）、`bili_action`（B 站写操作，需 `bili_jct`）。
