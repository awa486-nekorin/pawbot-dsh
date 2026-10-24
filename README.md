# @local/pawbot —— PawBot 受管服务 + 大肥鱼看 B 站

一个 DSH Host bundle，把 **PawBot（QQ 机器人）** 封装成 DSH 受管服务，并把原本独立的
`@local/pawbot-bili-watch`（大肥鱼看 B 站）合并进同一个 bundle。装这一个 bundle 就有全部功能。

## 结构

```
index.js                      # pawbot 受管服务插件（apply/inject/Config + 6 个工具 + /pawbot-ui 代理路由）
lib/hostdeps.js               # 从宿主树拿 schemastery 与 defineTool（拿不到就降级）
lib/config-utils.js           # 纯函数：遮罩 secrets / 去遮罩值 / 深合并 / 区段过滤 / 差异
lib/webui-proxy.js            # /pawbot-ui/* 同源代理路由（status/config/persona/reload-plugins/send/restart）
test/offline.mjs              # 离线单测（node test/offline.mjs）
bili-watch/                   # 从 @local/pawbot-bili-watch 原样复制过来的插件
  index.js                    #   挂成 @local/pawbot/bili-watch（subpath export）
  lib/…                       #   WBI 签名 / buvid 指纹 / view / player / subtitle / reply 等
  locale/…, icon.svg
locale/en.json, locale/zh.json, icon.svg   # 本 bundle 的显示 metadata / 图标
cordis.patch.yml              # 插入两列：pawbot 与 pawbot-bili-watch
```

## 与旧 bundle 的关系（重要）

`@local/pawbot-bili-watch` 原本是独立 bundle，会插入 `pawbot-bili-watch` 这一列并注册
`bili_watch` / `bili_watch_status`。若在本 bundle 里再插一列同 id / 同名模块，就会
**注册第二个 `bili_watch`**（tools 注册表对同层重名会失败），造成冲突。

因此本 bundle 采用「**复制进 bundle 当第二列**」的做法：

- `bili-watch/index.js` + `lib/` 原样复制进来，挂成 **subpath `@local/pawbot/bili-watch`**（id `pawbot-bili-watch`）。
- 安装本 bundle 后，要**移除旧的 `@local/pawbot-bili-watch` bundle**，让 `bili_watch` / `bili_watch_status`
  只有一份（来自本 bundle 的 subpath）。
- 数据目录沿用原默认 `D:/WorkBuddy/pawbot-bili-watch-data`，报告/请求/已分享状态无缝延续。

> 选「复制」而不是「引用已安装包」的原因：引用已安装包会在同一个 profile 里出现两个
> 解析到同一模块的列 → `bili_watch` 注册两次 → 冲突；复制进 bundle 才能做到「一个 bundle 全功能」，
> 且只保留一份 bili_watch 工具。

## Config（均可在 DSH 插件设置页改）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `projectDir` | `D:/WorkBuddy/2026-10-20-00-14-25/pawbot` | PawBot 项目根目录（run.py 所在） |
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

## 控制面板（在 DSH 内）

不用再开 PawBot 的 `http://127.0.0.1:6185` 网页——所有控制都收进 DSH 的 Web GUI（同源，无 CORS）。
本插件在 `inject: ['tools', 'webServer']` 下注册一条 prefix 路由 `/pawbot-ui/`，浏览器请求它，
它内部再去请求 PawBot 的 `http://<webuiHost>:<webuiPort>/api/...`（PawBot 网页只当内部通道保留）。

### 面板在哪、能做什么

重启 DSH 后：**左侧「设置」→ 导航栏多一项「PawBot」**（`settings.section` slot，`id: pawbot`），四个标签页：

| 标签页 | 内容 |
| --- | --- |
| 状态 | running / PID / uptime / spawn 次数 / PawBot `/api/status`（连接、会话、插件数、心情）；按钮：重启 PawBot（两段式确认）、重载插件；每 5 秒刷新，离开面板即停 |
| 设置 | **全部 37 个区段、200+ 个键**分组表单（bool→开关、number→数字框、string→输入框/textarea、数组与对象→JSON），有搜索框、折叠、原始 JSON 模式；**只送出改动过的键**；机密栏显示「已设置（不改）」 |
| 人设 | 直接编辑 `data/persona.md`（保存前两段式确认，显示字数） |
| 工具 | 发测试消息（会真的用机器人账号发）、看上下文 `/api/context` 与记忆 `/api/memory` |

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
- 本 bundle **不读写 PawBot 的 `data/`**、**不改任何 py**、**不改 profile 的 package.json / cordis.patch.yml**、
  **不装其他 npm 包**。

## 安装 / 验证

1. `plugin_manager` `action: install_bundle`，`target` 为本目录绝对路径。
2. 读回 `application` 与 `warnings` 决定是否生效。
3. `cordis_inspect_query`（Config.listConfigs）确认 `@local/pawbot` 列存在。
4. `Tool.listTools` 确认 `pawbot_status` / `pawbot_logs` / `pawbot_restart` / `pawbot_health` 已注册，
   且 `bili_watch` / `bili_watch_status` 只有一份。
5. 移除旧的 `@local/pawbot-bili-watch` bundle（避免 bili_watch 注册两次）。
