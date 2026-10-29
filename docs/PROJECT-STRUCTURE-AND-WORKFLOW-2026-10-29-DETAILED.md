# PawBot × DSH 插件 — 项目结构、实现与开发手册（2026-10-29 深度版）

> **给接手开发的新 Agent（也是给人看的第一份完整材料）。**
> 读完这份，你应该能回答：这个项目由哪些进程/文件组成、每个文件到底干什么、
> 关键功能是怎么实现的、改一个需求要动哪些文件、怎么验证、怎么部署、哪些地方不能碰。
>
> 姊妹文档：
> - `docs/HANDOFF.md` —— 历史决策与踩坑（较早，数字以本文为准）
> - `docs/UI-CURRENT.md` / `docs/UI-PLAN-v2.md` —— 面板现状与规划
> - `docs/DECIDER.md` —— 心情矩阵的决策模型
> - `docs/CLOUD-SYNC.md` —— 云同步协议/加密/冲突
> - `docs/DATA-PERSISTENCE.md` —— 升级不丢数据
> - `docs/BRAIN.md` / `docs/BRAIN-VERIFICATION.md` —— 大肥鱼大脑
>
> 最后核对：2026-10-29 · 本机分支 `upload-2026-10-27`（含本文提交）· 远端 `main = 6539deb`（未合并）

---

## 0. 30 秒认识这个项目

| 项 | 值 |
|---|---|
| 仓库 | `D:\WorkBuddy\pawbot-dsh`，**本身就是 DSH 插件的源码**（profile 里是 `link:`，改完重启即生效） |
| 它是什么 | 把「PawBot」QQ 机器人包装成 **DSH 受管服务**：起停/状态/日志/设置面板/一键安装/NapCat 编排；另带「大肥鱼看 B 站」子插件 |
| 技术栈 | Host/Client：JavaScript ESM（Node ≥22，无构建、无 JSX）；PawBot：Python 3.13（零框架，标准库 + pillow/websockets/cryptography） |
| 三个入口 | `index.js`（DSH Host）、`client.js`（浏览器面板）、`app/run.py`（PawBot 进程） |
| 进程模型 | DSH Host ⟷ 子进程 `python run.py` ⟷ NapCat（QQ 协议端）；可选「大脑」把单轮对话回调给 DSH Agent |
| 数据 | `app/data/`（人设/记忆/config/表情包/账本/备份）；可外置 `dataDir` |
| 面板 | 5 分页、344 个配置键（74 常用 + 270 进阶）、19 个 secret 控件 |
| 测试 | Python **1319 ran**（1 既有失败 / 1 skip）；JS **207**（8 套件） |
| 当前风险 | `main` 与工作分支分叉未合并；判官接 decision 未实现；真实 WebDAV/阿里云最终运行未验证 |

### 新 Agent 的最短上手路线

1. 读本文 §1（拓扑图）→ §2（根文件）→ §4（Host）→ §5（面板）→ §6（Python 运行时）。
2. 需要改功能时查 §14「开发任务索引」，它告诉你读哪节、动哪些文件、加哪些测试。
3. 改前跑 §13 的验证命令，改后必须再跑。
4. 任何涉及 key/记忆/对外发布/升级的操作，先看 §15 红线与 §16 坑。

---

## 1. 系统总览

### 1.1 组件拓扑（端口 / 路径 / 依赖方向）

```
┌──────────────────────────── DSH 桌面（Electron / Node 进程）────────────────────────────┐
│                                                                                         │
│  Host 插件 @local/pawbot = D:\WorkBuddy\pawbot-dsh（profile 用 link: 指过来）             │
│  ├─ index.js  ────────────────────────────────────────────────────────────────┐         │
│  │  ● Config(schemastery)  ● 子进程生命周期  ● 环形日志  ● 一键安装状态机     │         │
│  │  ● NapCat 编排            ● 10 个 DSH 工具  ● /pawbot-ui/* 代理（18 条）   │         │
│  │  ● /pawbot-dsh-bridge/execute（只读工具桥）                                 │         │
│  │  ● /pawbot-dsh-brain/turn + /status（大肥鱼大脑）                            │         │
│  │  ● data-guard：版本快照 / 外部 dataDir                                      │         │
│  └──────────────────────────────────────────────────────────────────────────────┘         │
│         │ spawn(子进程)                          ▲ HTTP loopback                         │
│         │ env: PAWBOT_BRAIN_URL/TOKEN(可选)       │ 只读桥 + 大脑回调 + 面板代理          │
│         ▼                                         │                                       │
│  ┌──────────────────────────────┐        ┌────────┴──────────────────────────────┐       │
│  │ PawBot（Python 3.13）          │        │ DSH Agent（大脑，可选）                │       │
│  │ app/run.py → core.Bot         │        │ agents.create() → preset pawbot-brain  │       │
│  │  :6199 OneBot 反向 WS  ◀──────┼────────│ 只有电脑工具 shell/fs/search/web        │       │
│  │  :6185 WebUI（仅本机）         │        │ + PawBot 业务工具代理（每轮注入）        │       │
│  └──────────┬───────────────────┘        └────────────────────────────────────────┘       │
│             │                                                                             │
│  ┌──────────▼───────────────────┐        ┌────────────────────────────────────────┐       │
│  │ NapCat（QQ 协议端）            │        │ 浏览器：DSH 设置页 → PawBot 面板        │       │
│  │ 反向 WS ws://…:6199/ws        │        │ 所有请求走同源 /pawbot-ui/*（不直连     │       │
│  └──────────────────────────────┘        │ Python），client.js 渲染                │       │
│                                           └────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

**依赖方向只有三条**：
1. DSH Host 管理 Python 子进程（启动/停止/健康/日志），不直接读写 `app/data`；
2. Python 通过 HTTP 回调 Host（面板代理、只读桥、大脑桥），Host 不回写 Python 数据；
3. 浏览器只与 DSH WebServer 同源通信，由 Host 代理到 `127.0.0.1:6185`。

### 1.2 一条 QQ 消息 → 回复 的完整链路

```
NapCat  ──WS──▶ app/pawbot/onebot.py（OneBotServer 收 JSON）
                     │  event.parse()  event.py
                     ▼
        core.Bot._on_event(conn, raw)
          ├─ _pass_filter(ev)                     # 白名单/黑名单/self/群过滤
          ├─ bus.dispatch(ev)                     # 事件总线（plugins 注册的 handlers）
          │    ├─ account / group_events / ...    # 非聊天钩子
          │    └─ ai_chat.on_message(priority=90) # ★ 主对话（见下）
          └─ 其他插件（pixiv/relactions/hobbies/...）
                     │
ai_chat.py：
  perceive.enrich()  补全：引用/图片/视频/语音/拍一拍/群名片/在叫谁
  ctxwin.build()     最近 300 条群聊 → 时间加权上下文
  agent.run(ctx, text, history)
      ├─ persona.build() → system prompt（人设 + 记忆 + 情绪提示 + 工具口径）
      ├─ mem.assemble.build()（memory.engine=v2）或 legacy context
      ├─ 循环 ≤ tools.max_rounds：
      │    ├─ 【可选】brain：把本轮交给 DSH Agent（llm.chat_message(brain_conv=…)）
      │    └─ 普通：llm.chat_message(messages, tools) → tool_calls → toolbox.execute()
      └─ 返回 (文本, 媒体段)
  style.polish() / safety / split / tts / stickers → 发送
                     │
                 safety.Send / onebot action ──▶ NapCat ──▶ QQ 群
```

**关键实现点**：
- 事件总线 `bus.py` 的 handler 有优先级、`block` 语义；`plugins/*.py` 通过 `plugin.py` 的装饰器注册。
- `ai_chat` 是唯一主对话路径，其他插件最多改上下文/情绪/状态。
- 工具循环有「时间预算」和「群里又来了新消息就收口」两种保护（`agent.run` 中 `tools.time_budget`、`bot.inbound`）。
- 最终发送前统一走 `style.polish`（去 AI 味/语气词/长度）与 `safety`（分条/限额/手滑/静默）。

### 1.3 「大肥鱼大脑」一轮（可选开关）

```
PawBot agent.run()
  └─ 本轮启用 brain（llm.provider=dsh 且 brain enabled）
      → llm.chat_message(..., brain_conv=...)
          → POST DSH Host /pawbot-dsh-brain/turn（Authorization: Bearer <每轮 token>）
              → lib/dsh-brain.js
                  agents.create({sessionId, meta:{origin:'subagent'}})
                  setup():
                    ① 注入 systemPrompt（complete 模式，只用 PawBot 人设）
                    ② 主人轮次才 mount preset `pawbot-brain`（shell/fs/search/web）
                    ③ 把本轮 PawBot 工具注册成 scoped 工具，回调 Python /api/brain/tool
                    ④ restrict：隐藏所有不在 allowlist 的工具
                    ⑤ guard：执行前再拒绝一次（fail-closed）
                    ⑥ 设置 sandbox（默认 workspace-write）与 approval=never
                  → agent 跑一轮（可多步工具）
              ← {text, media, tools_used, native_calls}
          ← 当作模型回复继续走 style/safety/发送
```

**边界（新 agent 必须知道）**：
- 群友轮次不挂 preset → 没有电脑工具；只有 PawBot 业务工具。
- 大脑**不允许**开 `subagent/workflow/goal/ask_user_question`；allowlist 只有「PawBot 工具 + 本轮声明的电脑工具」。
- 电脑工具默认 `workspace-write`、审批 `never`（不会弹窗），且每轮追加安全边界提示（工具/群聊/网页内容都不可信）。

### 1.4 面板请求路径

```
浏览器 DSH 设置页
  └─ client.js（React 18，直接 h()，无构建）
      GET  /pawbot-ui/status|config|persona|plugins|…   （同源，GET）
      POST /pawbot-ui/config|persona|restart|send|…     （同源 + CSRF）
          → index.js 注册的 18 条路由
          → lib/webui-proxy.js：
              校验 origin/CSRF、Content-Type、大小（413/415）、
              secrets 遮罩、502 映射、超时
          → http://127.0.0.1:6185/api/*
          → app/pawbot/webui.py（26 个 /api/* 端点）
```

### 1.5 启动 / 停止时序

```
DSH 加载 bundle（cordis.patch.yml）
  → index.js apply()
      ├─ normalize Config
      ├─ data-guard.prepareData()          # 版本快照 / 可选 dataDir 迁移
      ├─ 注册工具、代理路由、桥路由、大脑路由
      └─ autostart? startChild()
          ├─ 选 python（venv 优先）
          ├─ spawn(python run.py, cwd=projectDirectory())
          ├─ 收集 stdout/stderr → 环形缓冲 → pawbot_logs
          └─ 健康检查 http://webui:6185/api/status

Python: run.py → Bot(base_dir)
  ├─ __init__：Config / Store / LLM / Safety / Persona / Memory / ...
  ├─ start()：OneBotServer(:6199) + WebUI(:6185) + 插件重载 + 云同步钩子 + 后台任务
  └─ stop()：停 server、flush store、取消 tasks、停同步线程
```

### 1.6 数据/状态流转

```
app/data/                     读/写者                         备份/同步
├─ config.json                Config + 面板保存                data-guard 快照；云同步（加密）
├─ persona.md                 Persona（每轮读，mtime 缓存）    data-guard 快照；云同步
├─ sessions.json              Store（滚动上下文+小结）          云同步（可选 memory）
├─ memory/ people/ longmem/   记忆各层                         云同步（memory）
├─ emotion.json               Emotion（情绪值）                云同步
├─ stickers/index.json        StickerBook（元数据+权重）        云同步（stickers 可选）
├─ backups/plugin-update-*    data-guard 自动产生              不参与同步
├─ .sync-state.json           RealtimeSyncer                   不参与同步
└─ sync-conflict-* / backup-sync-*  同步冲突/备份              不参与同步
```

---

## 2. 仓库根目录逐个文件

| 文件 | 行数/大小 | 作用 | 实现要点 |
|---|---|---|---|
| `index.js` | ~1564 行 | DSH Host 主插件 | `apply(ctx, config)` 里完成 Config 归一化、数据守卫、子进程管理、工具注册、路由注册、桥/大脑接入；详见 §4 |
| `client.js` | ~3716 行 | 浏览器面板 | 单文件 React（`h()`，无 JSX/构建）；5 个 Tab、344 键元数据、API 统一页、保存 diff 管线；详见 §5 |
| `cordis.patch.yml` | 526 B | bundle patch | `insert` 两条插件行：`pawbot` 与 `pawbot-bili-watch`（后者以 subpath export 挂载，避免同名工具冲突） |
| `package.json` | 1398 B | bundle 清单 | `exports`（. / client / bili-watch / locale / icon）、`files`、`engines.dsh>=0.1.7-rc.1`、`dsh.bundle.patch`、`dsh.client.inject`；版本 `1.0.0` |
| `icon.svg` | 878 B | bundle 图标 | 纯静态 |
| `locale/en.json` `locale/zh.json` | 6 行 | bundle 级 i18n | 插件名/描述 |
| `README.md` | ~390 行 | 用户向说明 | 安装/面板/功能/文档索引 |
| `DEPLOY.md` | ~10.4 KB | 部署清单 | Linux 安装、4 个必改配置、云服务器差异、**§8 阿里云 Windows 部署** |
| `LICENSE` | MIT | 许可 | — |
| `.gitignore` | ~80 行 | 关键安全文件 | 排除 `app/data` 运行时产物、`dist/`、备份、同步状态；见 §11 |

---

## 3. `lib/`（Host 侧 8 个模块）

| 文件 | 行 | 导出（要点） | 职责 / 实现 |
|---|---|---|---|
| `hostdeps.js` | 134 | `loadHostDeps`, `loadHostModule` | 从 DSH 宿主树动态解析 `@deepseek-ai/*`（schemastery、tools、sandbox、agent 等）；拿不到就降级，避免 bundle 里装第二份依赖 |
| `config-utils.js` | 122 | `MASK`, `isSecretKey`, `maskSecrets`, `pickUpdatable`, `deepMerge`, `pickSection`, `diffKeys` | 纯函数：secrets 遮罩、只允许更新可写键、深合并、按区段取、diff；被 index.js 的 config 工具使用 |
| `setup.js` | 953 | `TIMEOUT`, `defaultRun`, `safeRun`, `detectPython`, `venvPaths`, `venvExists`, `ensureVenv`, `ensureDeps`, `ensureConfig`, `createSetupJob`, `createBootOrchestrator`, `runEnvSetup`, `MINIMAL_CONFIG`, `validateConfigObject` | 一键安装状态机：检测 python → 建 venv → pip 装依赖 → 写最小配置 → 启动 NapCat → 起 PawBot；命令执行可注入（离线测试） |
| `napcat.js` | 488 | `detectNapcat`, `detectMode`, `napcatConfigDir`, `writeOnebotConfig`, `startNapcat`, `napcatRunning`, `isLoggedIn`, `qrFromLog`, `readWebui`, `manualGuide` | NapCat 编排：docker / binary / 手动三种模式；写 OneBot 反向 WS 配置；从日志/WebUI 取二维码；检测登录 |
| `webui-proxy.js` | 513 | `createPawbotProxy`, `PERSONA_API_PATH` | `/pawbot-ui/*` 同源代理核心：路由表、CSRF/origin、Content-Type/大小限制、secrets 遮罩、错误映射、persona 文本通道 |
| `dsh-tool-bridge.js` | 206 | `BRIDGE_PATH`, `BRIDGE_LIMITS`, `BRIDGE_TOOLS`, `createBridgeBudget`, `projectStatus`, `createToolBridge`, `stripBridgeEnv`, `redactBridgeText` | 只读工具桥：allowlist 只有 `bili_watch_status`；限流/预算、loopback 校验、去敏、请求投影 |
| `dsh-brain.js` | 983 | `BRAIN_TURN_PATH`, `BRAIN_STATUS_PATH`, `BRAIN_LIMITS`, `DEFAULT_NATIVE_TOOLS`, `BRAIN_PRESET_ID`, `nativePresetRows`, `nativeToolNames`, `computerToolGrant` 等 | 大肥鱼大脑：注册 preset、`agents.create()`、人设注入、工具代理、restrict+guard、sandbox/approval、超时/取消；详见 §12.13 |
| `data-guard.js` | 360 | `readPluginVersion`, `isLink`, `linkTarget`, `listBackups`, `createBackup`, `prepareData`, `describe` | 数据守卫：版本快照、外部 dataDir 迁移（Windows junction / POSIX symlink）、不覆盖复制、留档、重试；详见 §12.16 |

---

## 4. `index.js`（Host 主插件）详解

### 4.1 Config 与归一化

- `Config` 是 schemastery schema（拿不到就 `undefined`，面板降级但本体照跑）。
- `normalize(config, DEFAULTS)`：逐键类型收敛（string 允许数组→逗号串、number/boolean 强校验），`refreshCfg()` 在每次工具/请求前重新合并。
- 新增 Host 配置必须同时改：`Config` schema 注释、`DEFAULTS`、`normalize` 行为（一般自动）、面板或文档。
- 当前 Host 配置分组：项目路径/python/自动启动/大脑（provider/model/effort/sandbox/cwd/tools/complete/ownerOnly/timeout）/日志/健康检查/WebUI/appDir/**dataDir/dataGuard/dataBackupKeep**/NapCat/OneBot token。

### 4.2 子进程生命周期

| 函数 | 行为 |
|---|---|
| `pythonForRun()` | 优先 `app/.venv/Scripts/python.exe`（或 bin/python），否则 `cfg.pythonExe` |
| `projectDirectory()` | 用户显式 projectDir 有 run.py → 用它；否则 `app/run.py` 存在 → 用 app；否则回退配置 |
| `startChild()` | 已在跑则跳过；spawn（cwd=projectDir，合并 env），接 stdout/stderr 到环缓冲；记录 pid/startedAt |
| `stopChild()` / `taskkillTree()` | 先 SIGTERM/温和退出，超时再整棵进程树杀掉（Windows 用 taskkill /T /F） |
| `restartChild()` | 停 → 等退出 → 起；重启失败记 last_error |
| `onExit` | 记录 last_exit；`restartOnCrash` 且非主动停止 → 退避重启（上限 maxCrashRestarts） |
| `health` | `pawbot_health` 打 WebUI `/api/status`，失败时返回进程层信息 |

### 4.3 日志与环缓冲

- 两套缓冲：主进程日志（≤5000 行）与「一键安装日志」（只保留最后 60 行给 setup/status）。
- `pushLine` 同时写 `ctx.logger`（Host 日志）与 ring；`redactBridgeText` 对 token/URL 去敏。
- `pawbot_logs` 返回按时间排序的 `{stream,line}`；面板状态页轮询同一数据。

### 4.4 一键安装 + NapCat 编排

`lib/setup.js` 状态机步骤（`bootStepState` / `setupState`）：
1. 环境检测（python/venv 是否存在）
2. 建 venv
3. `pip install -r app/requirements.txt`
4. 确保 `app/data/config.json` 存在（`MINIMAL_CONFIG`）
5. 检测/启动 NapCat（docker 或本地二进制）
6. 写 OneBot 反向 WS 配置（WS 地址 + token）
7. 启动 PawBot 子进程
8. 健康检查

- 每个步骤都有状态/日志/错误；`pawbot_setup` 非阻塞，重复调用幂等。
- NapCat 的 token 与 PawBot `server.access_token` 用同一个 `effectiveOnebotToken()`。
- 二维码：从 NapCat 日志 / WebUI 抓，缓存给面板 `napcat/qr`。

### 4.5 面板代理路由（18 条）

- 2 条 prefix：`/pawbot-ui`、`/pawbot-ui/`。
- 16 条 exact：`status`、`config`、`persona`、`reload-plugins`、`plugins/reload`、`send`、`restart`、`context`、`memory`、`setup/status`、`setup/run`、`napcat/status`、`napcat/setup`、`napcat/qr`、`boot`、`boot/status`。
- `uiRouteState` 记录 `registered (18 routes)` 或 `partial`，`pawbot_status` 会回传，用于排查「面板打不开」。

### 4.6 DSH 工具（10 个）

| 工具 | 输入 | 行为 |
|---|---|---|
| `pawbot_status` | — | 运行状态/pid/uptime/spawn/last_exit/日志尾 + ui_route + brain 摘要 + data_guard |
| `pawbot_logs` | `lines?` | 最近 N 行 stdout/stderr |
| `pawbot_restart` | — | 重启子进程（会短暂断线） |
| `pawbot_health` | — | HTTP 打 WebUI `/api/status` |
| `pawbot_setup` | `force?` | 一键安装/继续安装 |
| `pawbot_napcat` | `action=status/start/qr` | NapCat 编排 |
| `pawbot_config_get` | `section?`, `reveal?` | 读 PawBot config（默认遮罩 secrets） |
| `pawbot_config_set` | `patch`, `reason?` | 深合并写 config（遮罩值会被丢弃） |
| `pawbot_data_backup` | `action=status/backup/list` | 数据守卫状态/手动快照/列表 |
| `pawbot_brain` | `action=status/selftest/turn` | 大脑桥诊断/自检/自定义一轮 |

（bili-watch 子插件另注册 2 个工具：`bili_watch`、`bili_watch_status` → 全局 12 个。）

### 4.7 桥路由

- `/pawbot-dsh-bridge/execute`：Host→PawBot 只读工具桥，仅当 `dshBridgeEnabled=true` 时注册；每轮注入 URL+token；allowlist 单工具；限流/预算；loopback 校验；响应去敏。
- `/pawbot-dsh-brain/turn`、`/pawbot-dsh-brain/status`：大脑桥；Bearer token 每轮生成；见 §12.13。

### 4.8 数据守卫接入

- `apply()` 早期调用 `dataGuard.prepareData({appDir, dataDir, version, keep, enabled, log})`。
- 依赖 `package.json.version` 与 `data/.data-guard.json` 比对；失败只告警，不影响启动。
- 结果缓存在 `dataGuardInfo`，`pawbot_status.data_guard` 与 `pawbot_data_backup` 读取。

### 4.9 错误与降级语义

- Host 侧所有外部能力（webServer/agentPresets/sandbox/approval）都按「拿不到就降级/明确报错」处理；
- 面板路由注册失败 → `uiRouteState=unavailable/partial`，但不阻止 PawBot 运行；
- 大脑 preset 挂不上 → 本轮只有 PawBot 工具（degraded 文案）；
- 工具闸门装不上 → 本轮失败（fail-closed），不冒险执行。

---

## 5. `client.js`（浏览器面板）详解

### 5.1 渲染架构

- React 18 + `h()`（无 JSX、无打包、无第三方 UI 库）；一个文件导出 `Panel` 与设置页注入。
- CSS 是模板字符串，作用域用 `[data-pb-*]` 属性选择器 + `--dsw-*` 主题 token；支持窄屏容器查询与 `focus-visible`。
- 组件树：`Panel` → 5 个 Tab（`SetupTab`/`StatusTab`/`ApiTab`/`ConfigTab`/`PersonaTab`）+ 共用 `Field`/`Notice`/`ConfirmDialog`/`Badge` 等。
- `Panel({initialTab})` 支持测试注入；`__components` 暴露给 jsdom 冒烟。

### 5.2 五个 Tab

| Tab | 内容 | 数据来源 |
|---|---|---|
| 启动 | 一键安装步骤、venv/python 状态、NapCat 状态/二维码、启动/重启 | `/pawbot-ui/setup/*`、`napcat/*`、`boot*` |
| 状态 | 运行/连接/消息统计 KPI、最近日志、连接说明、崩溃信息 | `/pawbot-ui/status` |
| API 密钥 | 按服务分组（llm/judge/search/credentials/speech/sticker/conn/sync）；只显示常用项，详细参数折叠；secret 遮罩/已配置徽章；吸附保存条；保存前 diff 确认 | `GET/POST /pawbot-ui/config` |
| 设置 | 服务视图（常用 74 键按服务分卡 + 高级参数折叠） + 原始区段模式 + 全局搜索 + 原始 JSON | 同上 |
| 人设 | persona 文本编辑、字数/保存/提示 | `/pawbot-ui/persona`（GET/POST） |

### 5.3 元数据系统（三处契约之一）

- `COMMON`（74 行）与 `ADVANCED`（270 行）：每行 `[key, label, help, impacts[], control, extra]`。
- `IMPACTS` 徽章（如 speech/media/instant/restart/napcat…），`GROUPS`/`SECTION_LABELS`/`advancedGroupOf` 决定「服务视图」分组。
- `buildMeta` 生成查找表；`Form` 按 control 渲染 switch/number/text/textarea/json/select/ratio/secret。
- **新增配置必须三处同步**：`config.py DEFAULT`、`client.js` 行、`test/client-meta-check.mjs` 计数（当前 344 = 74+270）。

### 5.4 配置保存管线（重要）

```
GET /pawbot-ui/config
  → 遮罩 secrets（"***已设定***"）
用户编辑 → deepDiff(original, edited) 只取改动键
  → 去除遮罩值（stripMask；留空=不改）
  → 二次确认（secret 清空要再次确认）
  → POST /pawbot-ui/config {patch}
  → Python Config.update 深合并并落盘
  → 返回新配置（重新遮罩）→ 更新基线
```
- `useUnsavedGuard` 阻止未保存离开；保存失败自动 focus 错误提示（`NoticeRegion`）。
- 逐字段「撤销」按钮从 original 取回；全局搜索同时搜常用+高级。

### 5.5 状态/安装的「人话」映射

`describeConnection`、`humanMood/humanEmotion/humanLibido`、`lastExitText`、`bootStepState` 等把后端枚举翻译成中文；改协议时要同步这些函数。

---

## 6. Python 运行时总览

### 6.1 `app/run.py` 与 `core.Bot`

`run.py`（~87 行）：解析 base_dir、`Bot(base_dir)`、`asyncio.run(bot.start())`、SIGINT/SIGTERM 优雅停止。

`core.Bot.__init__` 装配顺序（决定依赖关系）：

1. `Config` → `Store`（sessions.json）→ `LLMClient` → `SafetyGuard`
2. `Persona` / `Memory` / `MemeBook` / `LongMemory` / `Diary` / `Hobbies`
3. `ReminderStore` / `ReminderService`
4. `PersonBook` / `Spice` / `StickerBook` / `Album` / `SelfEditor`
5. `Qzone` / `AccountActions` / `ToolBox` / `Agent`
6. `TopicTracker` / `Mood` / `Emotion`（`mood.bind_emotion`）/ `Libido` / `Proactive`
7. `PluginManager`（扫描 `plugins/`）
8. `OneBotServer`（:6199，`on_event=_on_event`）
9. 其余运行时（`inbound` 计数、压缩锁等）

`Bot.start()`：起 OneBotServer + WebUI + 插件 reload + 云同步启动钩子（`cloudsync.start_auto_pull`、`sync_realtime.start_realtime`）+ 后台任务；`stop()` 反向清理。

### 6.1.1 Python 模块依赖图（谁调谁）

```
run.py
└─ core.Bot
   ├─ config.Config ────────────── 所有模块读配置
   ├─ onebot.OneBotServer(:6199) ── wsproto.WSConnection
   │    └─ core._on_event ── event.parse ── bus.Bus ── plugins/*
   ├─ plugins/PluginManager ── 扫描 app/plugins，注册装饰器 handler
   ├─ Chat 链：ai_chat ─▶ perceive.enrich ─▶ agent.Agent
   │        ├─ persona.Persona（persona.md + owner + hints）
   │        ├─ mem.assemble（memory v2；失败退回 memory/longmem/ctxwin）
   │        ├─ llm.LLMClient ──（可选）brain.Brain ─▶ DSH Host
   │        └─ tools.ToolBox ── 30 个工具 ── websearch/moegirl/memes/
   │             bilibili/bili_watch/qzone/album/stickers/reminders/tts/...
   ├─ 状态：emotion/decider/judge/libido/mood/topic/spice/life/proactive
   ├─ 发送：style.polish ─▶ safety.SafetyGuard ─▶ onebot action
   ├─ 后台：tasks.TaskRegistry / reminders / bili_scroll / bili_post /
   │        cloudsync / sync_realtime / tts server
   └─ webui.WebUI(:6185) ── 26 个 /api/*（面板代理目标）
```

**依赖原则**：底层模块（config/log/message/wsproto/llm）不反向依赖 `core`；需要 bot 的模块通过 `bot` 引用或 `ctx`；
新增模块尽量只依赖底层，避免循环 import（`core` 在末尾装配）。


### 6.2 事件总线与插件

- `bus.py`：`Handler`（priority/block/once）、`Bus.register/dispatch`；异步 handler 串行 await，按优先级。
- `plugin.py`：装饰器 `on_message/on_command/on_keyword/on_regex/on_notice/on_request`；`PluginManager` 扫描目录、import、注册、热重载（`/api/plugins/reload`）、状态存 `data/plugin_state.json`。
- `context.py`：`Context` 是插件拿到的唯一对象（conn/bot/event/群/发送/回复/配置/记忆接口）。

### 6.3 `_on_event` 过滤顺序（不要乱改）

```
ignore self / non-message?
  → whitelist: 黑名单群优先拒绝
  → group_ids 非空 ⇒ 必须在群名单
  → user_ids 非空 ⇒ 必须用户名单（群聊+私聊都限）
  → owner 不是白名单通行证
  → dispatch（插件）
```

### 6.4 回复回合

- `turns.py`：每轮把「上一份小结 + 本轮」合成滚动小结（300 字），注入上下文。
- `ai_chat.py` 负责：攒话/合批（`chat.batch_*`）、触发条件、调用 agent、后处理、发送、记账（B 站送达）。
- `safety.py`：分条阈值、每会话限额、静默时段、手滑+更正、打字延迟/正在输入。
- `style.py`：去 AI 味、去表情/动作、限句、去公式化反问、语气词削减（`reply.tone_trim`）。

---

## 7. `app/pawbot/` 逐文件说明（65 个顶层模块 + 子包）

> 「关键接口/实现」列给出主要公开 API 或实现要点；测试文件见 §9。

### 7.1 核心 / 协议 / 插件

| 文件 | 行 | 职责 | 关键接口 / 实现要点 |
|---|---|---|---|
| `__init__.py` | 4 | 包标记 | — |
| `core.py` | 562 | 主装配 | `Bot.__init__/start/stop/_on_event/_pass_filter/status/manual_send`；见 §6.1/§6.3 |
| `event.py` | 161 | OneBot→统一事件 | `Event/MessageEvent/NoticeEvent/RequestEvent/parse()`；段解析、@/回复识别 |
| `bus.py` | 129 | 事件总线 | `Handler/Bus`；priority/block、异步 dispatch |
| `turns.py` | 214 | 滚动小结 | `update()`；合并旧小结+本轮，超长截断后注入 |
| `tasks.py` | 55 | 后台任务登记 | `spawn/pending/drain`；不丢引用、异常日志、停机等待 |
| `context.py` | 152 | 插件上下文 | `Context`；conn/bot/ev/群/发送/回复/记忆接口 |
| `plugin.py` | 212 | 插件系统 | 装饰器 + `PluginManager`（扫描/热重载/开关） |
| `config.py` | 613 | 配置 | `Config`（深合并 DEFAULT、`get/set/update/save`）；见 §12.17 |
| `log.py` | 98 | 日志 | 控制台 + 环形缓冲 + SSE 推送 WebUI |
| `store.py` | 268 | 会话存储 | `Store`；按 session_id 存最近 N 条 + 小结，JSON 原子写 |
| `message.py` | 161 | 消息段 | `Text/At/Image/Record/Reply/Face`、CQ↔array、`to_plain` |
| `wsproto.py` | 223 | 手写 WebSocket | RFC6455 `accept_key/WSConnection/accept/connect`，零依赖 |
| `onebot.py` | 301 | OneBot v11 服务端 | `OneBotServer`；反向 WS、动作调用、token、连接管理 |
| `ctxwin.py` | 230 | 上下文窗口 | `speaker/present/weight/build/build_messages`；时间加权压成文本 |

### 7.2 对话 / 提示词 / 风格 / 安全

| 文件 | 行 | 职责 | 关键接口 / 实现要点 |
|---|---|---|---|
| `agent.py` | 837 | 工具循环 | `Agent.run(ctx,text,history)`；拼 system（人设+记忆+提示）、≤max_rounds、预算/新消息收口、brain 分流、媒体收集 |
| `llm.py` | 433 | 模型客户端 | `LLMClient.chat/chat_message`；OpenAI 兼容 urllib；超时/备援/熔断/推理强度/视觉/DSML 解析；brain 模式转发 |
| `persona.py` | 150 | system 拼装 | `Persona.build(group, sender, extra)`；persona.md + owner line + 群/时间补充 |
| `style.py` | 844 | 回复清洗 | `strip_emoji/strip_actions/cap_sentences/drop_trailing_question/polish`；`_trim_tone_particles`（tone_trim） |
| `safety.py` | 510 | 发送层 | `SafetyGuard`；`typo_plan`；分条/限额/静默/延迟/正在输入 |
| `perceive.py` | 868 | 感知补全 | `enrich/render/card_text/talks_about_bot`；引用链、图/视频/语音、被 talking、拍一拍 |
| `breaker.py` | 132 | 熔断 | `Breaker`；连续失败跳过、冷却后半开 |

### 7.3 记忆

| 文件 | 行 | 职责 | 关键接口 / 实现要点 |
|---|---|---|---|
| `memory.py` | 131 | 可读写文本记忆 | `Memory`；`data/memory/*.md` 增删改查、检索 |
| `longmem.py` | 363 | 跨会话经历 | `LongMemory`；逐轮追加、按日合并、相关度检索 |
| `mem/`（8 个） | 见下 | 记忆 v2 | 四层 + 预算 + 实体 + 衰减 + 迁移 |
| `mem/assemble.py` | 195 | 拼装门面 | `build(bot,ctx,text)` → messages + 统计；失败由 agent 退回 legacy |
| `mem/layers.py` | 137 | 四层取数 | `l0_window/l1_turn/l2_days/l3_entities` |
| `mem/budget.py` | 95 | 预算闸门 | 单层截断、总预算分配、摘要行 |
| `mem/entities.py` | 472 | L3 实体库 | `EntityStore`；JSONL、去重、gist/points、按相关度召回 |
| `mem/decay.py` | 234 | 渐忘 | 按龄降分辨率（抹时间/细节/截首句），骨架保留 |
| `mem/write.py` | 273 | L1/L2 写入 | `write_turn/merge_day`；先落盘、预算内、去重 |
| `mem/bridge.py` | 168 | 跨会话桥 | 私聊记忆带进群聊，保留来源标注 |
| `mem/migrate.py` | 210 | 迁移 | 旧数据→实体库，幂等 |
| `people.py` | 656 | 群友档案 | `PersonBook`；`people/{uid}.md`、好感增减/衰减/标签/每日收益 |
| `relations.py` | 408 | 关系网 | `Relations`；互动计数、群主/管理、关系摘要 |
| `diary.py` | 269 | 日记 | `Diary`；每日合成第一人称日记、隔天回想 |
| `hobbies.py` | 214 | 爱好看板 | `Hobbies`；追番/游戏，定期检索新进展（可离线降级） |

### 7.4 知识与工具

| 文件 | 行 | 职责 | 关键接口 / 实现要点 |
|---|---|---|---|
| `tools.py` | 1408 | 模型工具集 | `SCHEMAS`（30 个工具）、`ToolBox.available/schemas/execute`；每个 `_t_xxx` 返回 (文本, 媒体段) |
| `websearch.py` | 533 | 联网搜索/正文 | `deepseek_search/fetch_smart/search_bing/...`；多源降级、超时、正文提取 |
| `moegirl.py` | 169 | 本地 ACG 资料 | `search_titles/intro/lookup`；聊天路径 `allow_network=False` |
| `memes.py` | 431 | 梗/黑话库 | `MemeBook`；触发词命中才注入、学习入库、落盘 |
| `reminders.py` | 724 | 约定提醒 | `parse_when/ReminderStore/ReminderService`；待办存储、到点 @、工具文案 |
| `selfedit.py` | 200 | 自主改提示词 | `SelfEditor`；只改允许分节、拒绝越界、冷却/额度 |

### 7.5 B 站

| 文件 | 行 | 职责 | 关键接口 / 实现要点 |
|---|---|---|---|
| `bilibili.py` | 223 | 找片/分享 | `ranking/popular/search/pick_lively/share_allowed/mark_shared/card` |
| `bili_watch.py` | 439 | 读外部报告 | `load_reports/pick_report/mark_shared/request_watch/format_for_model`；与 bili-watch 插件文件协议 |
| `bili_scroll.py` | 612 | 刷站/笔记 | `run_round/notes/actions/scheduler`；每日一轮、视频笔记 |
| `bili_post.py` | 267 | 自动发动态 | 默认关；`enabled/today_count/compose/post_once/scheduler`；额度/回执/pending |
| `bili_mcp/mcp_server.py` | 1477 | 独立 MCP 服务 | B 站数据采集（登录/搜索/评论/字幕/弹幕/视频信息），默认不参与主流程 |

### 7.6 外部 / 媒体 / 视觉

| 文件 | 行 | 职责 | 关键接口 / 实现要点 |
|---|---|---|---|
| `qzone.py` | 372 | QQ 空间 | `Qzone`；发/读/改/删/留言；授权、额度、回执、隐私闸 |
| `account.py` | 362 | 账号动作 | `AccountActions`；主人专属，改资料/头像等；与 qzone owner 迁移 |
| `dsh_bridge.py` | 115 | 只读桥客户端 | `is_configured/bili_watch_status`；只读、失败静默 |
| `media.py` | 370 | 媒体协议 | `MediaRef/extract/MediaLedger/sticker_allowed/resolve`；去重账本、安全解析 |
| `imagesearch.py` | 1678 | 找图 | 搜狗/360/wallhaven/Pixiv 多种来源、筛选、画质、降级 |
| `pixiv_session.py` | 836 | Pixiv 浏览器会话 | `PixivSession`；专用 Chrome、CDP、登录/抓图（仅桌面） |
| `stickers.py` | 812 | 表情包 | `StickerBook`；索引 schema、`pick_with_meta`、视觉描述、收藏/使用计数 |
| `doutu.py` | 134 | 斗图网 | `search/download`；抓取+入库 |
| `album.py` | 153 | 图库 | `Album`；带描述图片，避免重复看图 |
| `vision.py` | 433 | 看图 | `describe/see/collect_images/grid_data_url`；双通道（详细转述+原图） |
| `video.py` | 113 | 视频感知 | `extract_frames/local_path`；抽帧→视觉转述 |
| `animecheck.py` | 355 | 二次元门槛 | `configured/parse_anime/is_anime(_cached)`；抓图前过滤真人照片 |

### 7.7 情绪 / 状态 / 主动

| 文件 | 行 | 职责 | 关键接口 / 实现要点 |
|---|---|---|---|
| `emotion.py` | 436 | 八维情绪 | `DIMS`(开心/兴奋/惊讶/烦躁/低落/感动/得意/害羞)、`Emotion.apply/decay/blend/hint/update`；规则+判官/decider |
| `decider.py` | 332 | 决策模型 | `decide/decide_mood/deltas_from_answers/build_state/resolve_key`；OpenRouter decisions，失败降级 |
| `judge.py` | 155 | 判官 | `JudgeLLM/judge_enabled/judge_json`；便宜多模态二次判断（画质/看图）；decision 改造待做 |
| `topic.py` | 457 | 读空气 + Mood | `TopicTracker`、`Mood`（绑定 emotion 后的只读视图）、`mood_label` |
| `libido.py` | 133 | 性欲值 | `Libido`；独立 0~100，加减/回落/标签 |
| `spice.py` | 130 | 尺度 | `Spice`；群当前荤度、命令与提示 |
| `proactive.py` | 735 | 主动行为 | `Proactive`；空闲触发、话题/视频/冲浪、频率控制 |
| `life.py` | 454 | 时间感 | `now_info/holiday/schedule_state/quiet_now/proactive_factor/life_hint` |
| `groupinfo.py` | 242 | 群感知 | `GroupInfo.for_bot/describe`；群名/人数/身份缓存 |
| `random_interject`（插件） | — | 随机插话 | 见 §8 |

### 7.8 输出 / 运维 / 同步 / 大脑

| 文件 | 行 | 职责 | 关键接口 / 实现要点 |
|---|---|---|---|
| `tts.py` | 553 | 语音合成 | `synth/to_record/record_segments`；本地飞鱼/云端双 provider、清洗、分条、失败降级 |
| `speech.py` | 84 | 语音转写 | `transcribe()`；QQ 语音→文字，失败降级 |
| `webui.py` | 906 | 内置 WebUI | `WebUI`；26 个 `/api/*`（config/persona/logs/status/send/memory/people/plugins/...）；面板代理目标 |
| `cloudsync.py` | 1315 | 云同步 | `SyncError/Adapter/LocalAdapter/HttpAdapter/WebdavAdapter/make_adapter/push/pull/status/start_auto_pull`；bundle/加密/校验/冲突 |
| `sync_realtime.py` | 331 | 近实时 | `local_fingerprint/RealtimeSyncer/start_realtime`；防抖 push + manifest 轮询 + 冲突 |
| `brain.py` | 202 | 大脑客户端 | `env_url/env_token/enabled/register/unregister/active_ctx/tool_allowed/turn`；HTTP 回调 Host |
| `dsh_bridge.py` | 115 | 只读桥 | 见 §7.6 |

---

## 8. `app/plugins/`（12 个钩子）

| 文件 | 注册的钩子 | 作用 |
|---|---|---|
| `ai_chat.py` | `on_message(priority=90)` + 命令「清空/记住」 | 主对话：攒话、触发、agent、后处理、发送、B 站送达记账 |
| `basic.py` | ping/help/状态/echo 命令 | 基础指令 |
| `account.py` | `on_message(priority=85)` | 主人账号动作（改资料等） |
| `group_events.py` | recall/ban/admin/decrease/friend_add/essence/upload + request | 群事件反应 |
| `group_tools.py` | group_increase + 早安/晚安关键词 | 入群欢迎、作息问候 |
| `qq_actions.py` | notify/撤回/拍一拍 | 撤回、拍一拍→被点名 |
| `hobbies.py` | on_message(70) | 爱好看板钩子 |
| `life_events.py` | on_message(80) | 生活事件（作息/节日） |
| `pixiv.py` | pixiv登录 命令 | Pixiv 会话绑定 |
| `random_interject.py` | on_message(95) | 随机插话 |
| `relations.py` | on_message(70) + notify | 关系网更新 |
| `spice.py` | 荤/素/尺度命令 | 群尺度控制 |

---

## 9. `app/tools/` 脚本与测试地图

### 9.1 运行脚本（20 个，非 test_）

| 脚本 | 用途 | 联网/风险 |
|---|---|---|
| `chat_window.py` | Tkinter 假 NapCat 对话测试窗口（`--check`/`--self-id`） | 本机 WS，无外网 |
| `cloud_sync.py` | 云同步 CLI：status/test/push/pull/watch | 按配置联网 |
| `decider_once.py` | 单独试一次 decision 模型 | 联网（OpenRouter） |
| `describe_stickers.py` | 表情包补视觉描述（--dry/--apply，备份索引） | 联网（totokens） |
| `selftest.py` | 离线自检：人设/记忆/表情/工具循环 | 离线 |
| `chat_demo.py` / `mock_qq.py` / `mock_send.py` | 无 QQ 下的链路演示/假协议端 | 本机 |
| `classify_stickers.py` / `strict_anime_pass.py` / `refill_stickers.py` / `fetch_stickers.py` / `make_stickers.py` | 表情包清洗/抓取/生成 | 联网（工具专用） |
| `research_slang.py` / `seed_memes.py` / `backfill_gist.py` / `migrate_memory.py` | 语料/梗库/记忆迁移 | 部分联网 |
| `bind_pixiv.py` | 绑定 Pixiv 浏览器会话 | 桌面+联网 |
| `_proto_decay.py` / `_sticker_probe.py` | 原型/探针（不接主流程） | 本地 |

### 9.2 测试地图（81 个 `test_*.py`，共 1319 用例；全部离线、假对象/临时目录）

| 领域 | 文件（示例） | 主要覆盖 |
|---|---|---|
| 对话/上下文 | `test_turns` `test_ctxwin` `test_context_hints` `test_message_split` `test_split_and_quote` `test_batch_reply` `test_pacing` | 小结、上下文窗口、分条、合批、节奏 |
| 人设/风格 | `test_persona_tone` `test_style` `test_identity` `test_roleplay_style` `test_poke_and_tone` `test_owner_persona` | 去助手化、语气词、认人、拍一拍 |
| 记忆 | `test_mem` `test_mem_source` `test_longmem` `test_decay` `test_people` `test_people_memory` `test_relations` `test_diary` `test_hobbies` `test_reminders` | 四层/预算/实体/衰减/档案/关系/日记/提醒 |
| 情绪/决策 | `test_emotion` `test_emotion_model` `test_decider` `test_libido` `test_robustness` | 八维、判官、决策映射、心情视图 |
| 模型/可靠性 | `test_llm_dsml` `test_llm_fallback` `test_llm_options` `test_breaker` | 工具调用解析、备援、推理强度、熔断 |
| 媒体/视觉 | `test_media` `test_stickers` `test_sticker_desc` `test_sticker_classify` `test_doutu` `test_vision_dual` `test_see_chain` `test_perceive_chain` `test_image_owner` `test_image_quality` `test_anime_gate` | 媒体协议、表情 2.0、看图链路、图片归属、二次元门槛 |
| B 站 | `test_bili_watch` `test_bili_delivery` `test_bili_scroll_report` `test_bili_post` `test_pixiv_home` `test_pixiv_public` `test_pixiv_session` `test_search_tool` `test_xsearch` | 报告/送达/刷片/动态/Pixiv |
| Qzone/账号 | `test_qzone` `test_qzone_security` `test_qzone_tools` `test_qzone_proactive` `test_account` | 授权/额度/回执/隐私 |
| 事件/群聊 | `test_events` `test_group_observe` `test_whitelist` `test_surf` `test_proactive` `test_proactive_bili` `test_life` `test_reminders` | 群事件、旁听、白名单、主动 |
| 语音/多媒体 | `test_tts` `test_speech` | TTS 降级/record、语音转写 |
| 同步/守卫/桥 | `test_cloudsync` `test_realtime_sync` `test_dsh_bridge` `test_brain` `test_memes_local` | 云同步往返/加密/冲突/实时；只读桥；大脑客户端 |
| Host（mjs） | `test/offline` `client-meta-check` `dsh-brain-offline` `dsh-tool-bridge-offline` `dsh-tool-bridge-host` `setup-offline` `data-guard-offline`；`bili-watch/selfcheck` | 代理/CSRF/413/415/502、面板契约、大脑桥、工具桥、安装状态机、数据守卫、B 站自检 |

**写测试的约定**：
- 只用假对象/临时目录；不读真实 `data/config.json`；不联网、不连 QQ。
- 断言输出/返回结构，不断言日志文案以外的实现细节；对时间/随机/模型调用一律打桩。
- 新增模块放在 `app/tools/test_<module>.py`；Host 逻辑放 `test/`；必须能离线跑。

---

## 10. `bili-watch/`（「大肥鱼看 B 站」子插件）

| 文件 | 行 | 作用 / 实现 |
|---|---|---|
| `index.js` | 925 | 插件本体：注册 `bili_watch`、`bili_watch_status` 工具；定时器、候选池、请求队列、报告生成、与 PawBot 的文件协议（shared/requests/seen）、模型守门 |
| `lib/bili.js` | 319 | B 站抓取（Node fetch）：视频信息/字幕/热门评论/榜单/搜索/用户/UP 投稿 |
| `lib/http.js` | 257 | 请求纯逻辑：标头、风控判定、重试决策、可中断 sleep（fetch/sleep/random 可注入） |
| `lib/wbi.js` | 40 | WBI 签名 |
| `lib/cookie.js` | 97 | buvid3/buvid4 指纹拼接与缓存 |
| `lib/credential.js` | 56 | SESSDATA 多来源解析（env > credential.json > 面板） |
| `lib/subtitle.js` | 30 | 字幕 JSON→带时间戳文本 |
| `lib/report.js` | 80 | 报告 schema 校验、watched_level 修正、去时间戳 |
| `lib/queue.js` | 82 | 请求队列扫描（文件协议） |
| `lib/request-lifecycle.js` | 82 | 请求结案规则（pending→done/expired） |
| `lib/seen.js` | 57 | 判重表（插件写 watched/skipped，PawBot 写 shared） |
| `lib/pool.js` | 40 | 候选池 + 判重前移 |
| `lib/notes.js` | 58 | 人读笔记 md |
| `lib/model-guard.js` | 44 | 模型白名单（纯函数） |
| `lib/hostdeps.js` | 122 | 宿主依赖解析（同 Host） |
| `lib/atomic.js` | 22 | 原子写文件 |
| `lib/inflight.js` | 30 | 同 key 请求去重 |
| `selfcheck.mjs` | 970 | 离线自检（52 项） |
| `README.md` / `locale/*` | — | 说明与文案 |

---

## 11. `app/data/` 数据模型（哪些文件、什么格式、谁读写）

### 11.1 git 视图

| 类别 | 文件/目录 | git |
|---|---|---|
| 被跟踪（模板/人设） | `config.json`、`persona.md`、`memory/facts.md`、`memory/impression.md`、`memory/slang.md`、`hobbies.json`、`plugin_state.json` | 已提交；`git reset/checkout` 会覆盖 |
| 运行时（忽略） | `sessions.json`、`relations.json`、`emotion.json`、`mood.json`、`libido.json`、`spice.json`、`topic.json`、`media_ledger.json`、`proactive_state.json`、`people/*.md`、`diary/*.md`、`memory/entities.jsonl`、`album/`、`stickers/`、`image_cache/`、`browser/`、`bili_notes/`、`napcat/`、`run.log`、`brain/` | 不会 |
| 备份/同步产物 | `backups/plugin-update-*`、`.data-guard.json`、`.sync-state.json`、`config.synced.json`、`sync-conflict-*/`、`backup-sync-*/`、`data.migrated-*/` | 不会 |

### 11.2 关键文件格式

- `config.json`：**运行时覆盖层**（不是完整默认值）；`Config.load` 与 `config.py DEFAULT` 深合并。含 secrets 时面板遮罩，同步需加密。
- `persona.md`：整段 system 人设（当前 86 行），按 mtime 热加载。
- `sessions.json`：`{session_key: {messages:[{role,content,who,ts,uid}], summary, ...}}`，Store 原子写。
- `memory/*.md`：`facts/impression/slang` 纯文本，`Memory` 读写/检索。
- `mem/entities.jsonl`：每行一个实体 `{id,kind,gist,points,ts,...}`（`EntityStore`）。
- `people/{uid}.md`：群友档案（称呼/印象/好感/标签）；`.daily_gain.json` 记每日加成。
- `relations.json`：互动计数/关系摘要；`emotion.json`：`{values:{8维},updated}`；`libido/mood/topic/spice`: 对应模块状态。
- `stickers/index.json`：`{"文件名": {tags:[], desc, sha1, uses, mood, desc_source}}`；挑图时 tags+2 / desc+mood+name+1，按 uses 降权。
- `media_ledger.json`：已发媒体去重账本；`album/`：带描述的图库。
- `memes.json`：梗库；`reminders.json`：待办（含 when/重复/已提醒）。
- `backups/plugin-update-*`：数据守卫快照（config/persona/sessions/relations/memory/people/diary 等）。

---

## 12. 关键子系统实现（深挖）

### 12.1 对话生成 `agent.Agent.run`

1. 读配置：`tools.max_rounds`（默认 3）、`tools.time_budget`（90s）、`memory.engine`、`llm.provider`。
2. 组装 system：`persona.build()` + `_participation_hint/_sense_hint/_life_hint/_mood_hint/_sticker_hint/_spice_hint/_drafting_hint/_tool_hint`。
3. 记忆：`mem.assemble.build()`（v2）失败 → `_legacy_context`。
4. 循环（≤rounds+1）：
   - 预算耗尽/群里新消息≥2 条 → 去掉工具、要求收口；
   - 有 brain → `llm.chat_message(..., brain_conv=...)`；否则普通 LLM；
   - 无 `tool_calls` → 返回文本 + 媒体；
   - 有 `tool_calls` → `toolbox.execute()` 逐个执行，结果回填 messages；工具返回的 `media` 汇总随最终消息发送。
5. 返回 `(content, media)` 给 `ai_chat`，再经 `style.polish`/`safety`/发送。

### 12.2 `llm.LLMClient`

- OpenAI 兼容 `POST {base_url}/chat/completions`，urllib 实现，不装 openai。
- 配置：`llm.base_url/api_key/model/timeout/first_timeout/temperature/max_tokens/reasoning_effort/fallback_model`。
- 备援：超时/5xx 换 `fallback_model`；400 不重试；熔断器 `breaker` 跳过坏模型。
- 工具调用：解析 `tool_calls`；兼容 DSML 文本（`test_llm_dsml`）。
- 视觉：`vision_*` 独立通道；`llm.provider=dsh` 时把请求转给 Host 大脑（`brain_conv`）。
- 日志不打印 key；错误分类（NO_MODEL/TIMEOUT/HTTP_4XX/5XX）。

### 12.3 记忆 v2（`app/pawbot/mem/`）

- 目标：四层记忆 + 总预算，解决「记忆越长越乱」。
- 层：
  - L0 最近窗口（`l0_window`）
  - L1 本回合（`l1_turn`）
  - L2 近几天（`l2_days`，按日合并 `merge_day`）
  - L3 长期实体（`l3_entities`，`EntityStore` JSONL）
- 预算：`budget.分配()` 按总字数（`memory.total_chars`）分配各层额度，超了截断；输出统计。
- 写入：`write.write_turn` 先落 L1/L2；`entities` 做实体提取/去重；`decay` 按龄降分辨率（骨架保留）；`bridge` 把私聊记忆带进群聊并标注来源；`migrate` 幂等迁移旧数据。
- 入口：`assemble.build()` 返回 messages + 统计；失败由 agent 退回 legacy，绝不影响回话。

### 12.4 情绪矩阵 + 决策模型 + 判官

**八维**：`开心 兴奋 惊讶 烦躁 低落 感动 得意 害羞`（0~100，基准 50；`BASE_BY_DIM`）。
- `decay()`：半衰期向 50 回归；`turn()`：每轮再回一点；`apply(deltas)`：裁剪 `emotion.max_step`。
- `heuristic_deltas()`：关键词兜底（夸→开心/得意，骂→烦躁/低落，抱抱→感动，问号→惊讶，深夜→低落，被撩→害羞/兴奋）。
- `hint()`：按 dominant/secondary 生成「语气提示」注入 system；`blend()` 给贴纸/状态用（dominant/intensity/secondary）。
- `update()`：decay → **decider（若开）** → judge（`emotion.judge_model` 或 `judge.*`）→ heuristic → apply → save。
- **决策模型**（`decider.py`）：POST `https://openrouter.ai/api/alpha/decisions`，body `{model, state, questions:{shift(noul), emotion(choice 8+none), intensity(score)}}`；响应 `answers`：`noul` 0~1、`choice` 概率分布、`score` 0~1；映射成情绪增减（负面压开心/得意，正面压烦躁/低落，其余 -1），乘 weight/confidence；失败静默退回判官/关键词。key 解析：config → 环境变量 → `~/.pawbot/decider.key` → DSH 凭证。
- **判官**（`judge.py`）：`JudgeLLM` 继承 LLMClient；`judge_json(prompt, image=...)` 让多模态模型返回 JSON；用于画质/看图二次判断。当前**未**接 decision；`docs/DECIDER.md` 有改造方案。

### 12.5 表情包 / 媒体协议

- `StickerBook`：`index()` 读 `stickers/index.json`；`save()` 下载/去重/写 sha1/描述；`pick_with_meta(query, exclude)` 按 tags(+2)/desc/mood/name(+1) 打分，`uses` 降权，排除最近用过；`pick_or_fetch` 不够就抓斗图网；`mark_used` 计数；`to_segment` 转 QQ 图片段。
- 发送节奏：`media.sticker_chance`（0.12）+ `persona.sticker_gap`（[3,6]）+ 情绪绕闸（`sticker_force_intensity`/`sticker_force_dominant`）+ 用户明确要表情包（`_sticker_requested`）。
- 视觉描述：`describe_stickers.py` 用 totokens/OpenRouter 视觉模型回填 `desc/mood/desc_source`（本机 6/6 已有 desc）。
- `media.py`：解析模型输出里的 `[IMG:...]`/图片段、去重账本、尺寸/NSFW 检查、随回复发送。

### 12.6 视觉 / 图片

- `vision.py`：`describe()` 把图片给 `llm.vision_*`（默认 9527.codes 的 gemini-4-argon，可切 totokens gemini-3.8-flash）生成转述；`see()` 组合「详细转述 + 原图」双通道；宫格（多图）与缓存。
- `imagesearch.py`：多源找图（搜狗/360/wallhaven/Pixiv 公开/DeepSeek/Pixiv 登录），带过滤（画幅/收藏/AI/人气）、画质判官、降级。
- `animecheck.py`：抓图前判二次元（真人照片拒绝）；`album.py` 存已看图的描述。
- `perceive.py`：把消息里的图/视频/语音/引用补全成模型能读的内容（最近图回看、引用链、被 talking）。

### 12.7 联网 / 知识

- `websearch.py`：`deepseek_search`（官方搜索 API）、`fetch_smart`（正文抓取）、Bing/360/DDG 降级、X 搜索；超时与截断。
- `moegirl.py`：本地 ACG 资料检索，聊天路径不联网。
- `memes.py`：梗库按触发词注入，学习写回。
- `hobbies.py`：爱好看板定期查新（可离线）。

### 12.8 B 站三条线

1. **看片**：`bili-watch` 插件抓候选/报告，写 `pawbot-bili-watch-data`；`bili_watch.py` 读报告/挑一个/标记 shared/请求看片；PawBot 侧记账最终是否真发出。
2. **刷站**：`bili_scroll.py` 每日一轮（配置时间），生成笔记/汇报（@主人/补一句）。
3. **自动发动态**：`bili_post.py` 默认关；enabled 字面 True + 凭据 + 每日额度 + pending 落盘 + 回执确认。

### 12.9 Qzone / 账号

- `qzone.py`：所有写操作走「授权（owner_ids）→ 额度（磁盘账本）→ pending 落盘 → 调用一次 → 回执校验」；失败不重试、不虚报成功。
- `account.py`：主人专属账号动作（改资料/头像等），权限闸与 qzone 一致。

### 12.10 语音 / 视频

- `tts.py`：`synth()` 本地飞鱼（CosyVoice）或云端 TTS；`to_record` 转 QQ `record` 段；失败退回文字；`should_force_voice`/`split_for_voice`/`clean_for_speech`。
- `speech.py`：QQ 语音→文字（`transcribe`），失败降级。
- `video.py`：QQ 视频抽帧→`vision` 转述。

### 12.11 拟人化 / 安全

- `safety.py`：分条（`‖`/长度）、每会话/全局限额、静默时段、`silence_chance`、手滑+更正（`typo_plan`）、延迟（`typing_cps/min/max`）、正在输入。
- `style.py`：去 AI 味/表情/动作/列表，限句，去公式化反问；`tone_trim` 按确定性哈希削句尾「啊/嘛/啦/呀/哦/哟」，保留「呢/吧/吗」。
- `breaker.py`：模型连续失败熔断。
- `topic.py`：读空气（话题/该不该插话）；`Mood` 只读视图。
- `proactive/life/random_interject/relations`：主动行为与频率控制。

### 12.12 配置系统

- `config.py DEFAULT` 是唯一真相（38 区段 / 344 面板键），`Config.load()` 与 `data/config.json` 深合并；`Config.update(patch)` 只写改动、原子保存。
- 面板元数据契约：`client.js COMMON/ADVANCED` 一一对应；`test/client-meta-check.mjs` 校验「每个 key 都有中文 label/help≥8 字/影响徽章合法/secret 被 API 分组覆盖/无重复」。目前 **32 项检查、344 键 = 74 + 270**。
- secrets：面板遮罩「***已设定***」，保存时剥掉遮罩；留空=不改；清空要二次确认。真实 key 应放 DSH 凭证/环境变量/专用 key 文件。

### 12.13 大肥鱼大脑（Host + Python）

- Python `brain.py`：读 Host 注入的 `PAWBOT_BRAIN_URL/TOKEN`；`turn` 把 system/user/context 发给 Host；`tool_allowed` 控制回调工具白名单；失败降级到普通 LLM。
- Host `lib/dsh-brain.js`：每轮 `agents.create()`（origin=subagent）→ `setup()` 注入 complete 人设 → 主人轮次挂 `pawbot-brain` preset（shell/fs/search/web）→ 注册 PawBot 工具代理（执行时回调 Python `/api/brain/tool`）→ `restrict+guard` allowlist → 设 sandbox/approval → 等 Agent 结束 → 回 `{text, media, tools_used}`。
- 限制：无 subagent/workflow/goal；群友无电脑工具；工具输出视为不可信；超时 `brainTimeoutMs` / `brainToolTimeoutMs`。

### 12.14 只读工具桥

- `lib/dsh-tool-bridge.js` + Python `dsh_bridge.py`：默认关（`dshBridgeEnabled=false`）；每轮注入 URL/token；allowlist 只有 `bili_watch_status`；限流/预算/loopback/去敏；无写操作。

### 12.15 云同步

- 包：`tar.gz`（persona、memory、可选 config、manifest 逐条 sha256）；排除 brain/browser/album/日志/临时/符号链接。
- 加密：passphrase 非空 → AES-256-GCM + PBKDF2(200k)（cryptography/pycryptodome）；未加密 + include_config → fail-closed。
- 适配器：`LocalAdapter`（目录）、`HttpAdapter`（PUT/GET+Bearer）、`WebdavAdapter`（MKCOL/PUT/GET）。
- `push`：打包→上传→写 `last_push`；`pull`：下载→校验→写文件（config 默认只写 `config.synced.json`，`--overwrite-config` 才覆盖并先备份，保留本机 sync 段）。
- 近实时 `sync_realtime.py`：本地指纹 2s 轮询 + 3s 防抖 push；远端 30s 读 manifest；冲突 keep_both/local/remote；首次 keep_both。
- 启动钩子：`auto_pull_on_start` / `mode=realtime` 在 `Bot.start()` 里起线程。

### 12.16 数据守卫（升级不丢数据）

- `prepareData()`：读 `package.json` 版本 vs `data/.data-guard.json`；版本变 → 快照 config/persona/sessions/relations/memory/people/diary 到 `backups/plugin-update-<版本>-<时间>`，保留 keep 份。
- `dataDir` 非空：把 `app/data` 不覆盖复制到外部 → 旧目录改名 `data.migrated-*` → 建 Windows junction / POSIX symlink；失败重试/回滚；升级后 `app/data` 丢失会自动重建联接。
- 工具 `pawbot_data_backup`；本机目前只开快照、未启用 dataDir。

### 12.17 一键安装 / NapCat / 部署

- 状态机见 §4.4；`pawbot_setup` 幂等、非阻塞。
- NapCat：docker/binary/手动；写 OneBot 配置；二维码；登录检测。
- 打包：`scripts/pack-server.ps1/sh` 默认不含数据，`-IncludeSecrets` 加 config+persona+secrets；产物含 sha256。
- 阿里云 Windows：scp → 校验 → tar 解压 → 放 secrets → venv/pip → `run.py`（详见 DEPLOY.md §8）。

---

## 13. 测试与验证

```powershell
# JS（仓库根，共 207）
node test/offline.mjs                          # 19
node test/client-meta-check.mjs                # 32
node test/dsh-brain-offline.mjs                # 20
node --test test/dsh-tool-bridge-offline.mjs   # 15
node test/dsh-tool-bridge-host.mjs             # 3
node test/setup-offline.mjs                    # 57
node test/data-guard-offline.mjs               # 9
node bili-watch/selfcheck.mjs                  # 52
node --check index.js; node --check client.js

# Python（app\，PYTHONPATH="."）
py -m unittest discover -s tools -p "test_*.py" -q
# 期望：1319 ran / 1 failure（既有 pixiv 排序）/ 1 skipped
```

**预期失败**：`test_pixiv_session::test_filters_and_sorting`（既有）；部分环境 `setup/selfcheck EPERM`。
**验证原则**：报告分开「代码完成 / 离线测试通过 / 环境受阻 / 未实机验证」；不跑真实 QQ/外网。

---

## 14. 开发任务索引（recipe book）

| 我要做什么 | 先读 | 要改的文件 | 配置/契约 | 测试 | 风险/注意 |
|---|---|---|---|---|---|
| 新增一个模型工具 | §7.4、§12.1 | `tools.py`（SCHEMAS + `_t_xxx` + available） | 无（或加权限键） | `tools/test_*.py` | 工具返回 `(text, media)`；失败不要抛 |
| 新增配置项 | §12.12 | `config.py DEFAULT` → `client.js` 行 → `client-meta-check` 计数 | label/help/impact/secret | meta 32 项 | 三处不同步必红 |
| 改人设/语气 | §12.11 | `persona.md`（明确要求才改）、`style.py`、`agent.py` hints | `reply.tone_trim` 等 | `test_persona_tone/style/identity` | 人设是红线 |
| 加一个插件 | §6.2、§8 | `app/plugins/*.py` + `data/plugin_state.json` 自动 | 配置键 | 新 `tools/test_*.py` | 优先级/block 语义 |
| 改情绪维度 | §12.4 | `emotion.py DIMS` + `decider.EMOTIONS` + `stickers.EMOTION_TAGS` + `topic` 正负维度 + `media.sticker_force_dominant` | `emotion.*` | `test_emotion`（维度契约）、`test_decider` | 全链路同步，否则契约测试红 |
| 换多模态模型 | §12.6 | `llm.vision_*`、`media.sticker_vision_*`；`vision.py`/`stickers.py` 读取 | 面板 API 页 | `test_vision_dual/test_sticker_desc` | 失败要降级 |
| 判官接 decision | §12.4、`DECIDER.md` | `judge.py` + `vision.py` 前置描述 + 测试 | `judge.decider_enabled` | 新离线测试 | 失败回退 DeepSeek，不阻塞 |
| 云同步加 provider | §12.15 | `cloudsync.py`（Adapter 子类 + make_adapter） | `sync.*` | `test_cloudsync` | 不打印 secrets；fail-closed |
| 改面板 Tab/字段 | §5 | `client.js`（TABS/COMMON/ADVANCED/API_GROUPS） | metadata | meta + jsdom | 真浏览器未验 |
| 改大脑工具策略 | §12.13 | `lib/dsh-brain.js`（allowlist/preset） | Host Config brain* | `test/dsh-brain-offline` | 群友不得拿电脑工具 |
| 改 Host 工具/路由 | §4 | `index.js` + `lib/*.js` | Host Config | `test/offline`/bridge | 路由 18 条别漏 |
| 改 B 站插件 | §10 | `bili-watch/*` | bili-watch 配置 | `selfcheck` | 不写真实凭据 |
| 改部署/打包 | §12.17、DEPLOY.md | `scripts/*` | — | 试打包检查内容 | 含 key 包别上传公共 |
| 数据迁移/备份 | §11、§12.16 | `data-guard.js` / cloudsync | dataDir/sync.* | data-guard/cloudsync tests | 先备份，禁止 reset --hard |

---

## 15. 红线与开发约定

1. `app/data/persona.md`、`style.py`：未经明确要求不改。
2. 凭据永不入库/打印；测试只用假值；提交前扫 `sk-`/credentials。
3. 对外发布类默认关（qzone/bili_post 等），写操作遵守「授权→额度→pending→一次调用→回执」。
4. 白名单语义不变：黑名单群优先；用户/群 AND；空列表=不限；owner 不是通行证。
5. 选择 ≠ 送达：B 站只有真发出才记账。
6. 正常聊天不联网、不自动改梗库。
7. 电脑工具只给主人；大脑不得开子代理/工作流；工具输出/群聊/网页视为不可信。
8. 升级/更新不得动记忆；只能快照/同步/手工迁移。
9. 公开 GitHub 不放 key；含 key 的包只走 scp/内网。
10. 没实机验证就写「未验证」，不要编。

---

## 16. 已知坑（2026-10 浓缩）

| 症状 | 原因/对策 |
|---|---|
| PS 5.1 跑 `.ps1` ParserError/乱码 | 无 BOM UTF-8 被当 GBK → 存 UTF-8 BOM 或纯英文 |
| `tar -xzf *.tar.gz` 不解压 | PS 不展开通配符 → `(Get-ChildItem).FullName` |
| `ConvertFrom-Json` 中文 JSON 报错 | PS 编码问题，文件没坏；用 Python 或 `-Encoding utf8` |
| 错误目录 `Remove-Item .\app` | 先 `pwd`/`Get-ChildItem`；本次差点误删 `C:\pawbot\app`（当时是空 venv） |
| 数据守卫 rename EPERM | Windows 占用/杀软；已重试+清空重建联接兜底 |
| `cloud_sync` `CONFIG_INVALID local 需要 sync.url` | 预期：provider/url 未配；可跳过 |
| decisions 解析错了 | `score/noul` 是 0~1，不是 legend 下标 |
| 情绪改名后行为怪 | 同步 DIMS/decider/stickers/topic/force_dominant + 契约测试 |
| 表情包频率不对 | 运行态 config 旧值覆盖默认（chance/gap） |
| 改配置不生效 | 面板即时；定时器/线程要重启（`*_times`、`sync.mode`） |
| 大脑工具隔离失败 | preset/guard 装不上 → 本轮 fail-closed；查 `pawbot_status.brain`/日志 |
| DSH GUI 地址变化 | 每次启动端口可能不同，别写死 |
| GitHub push 显示错误但成功 | git 进度在 stderr；看 `-> branch`/`ls-remote` |
| main 与本地不一致 | 两条线都有提交；在 `upload-2026-10-27` 分支，别 reset main |

---

## 17. 当前状态与路线图（2026-10-29）

| 项 | 状态 |
|---|---|
| 分支 | `upload-2026-10-27`（含本文，已推）；`main=6539deb` 未合并 |
| 工作区 | 干净；仅 `docs/por-20261027/` 未跟踪 |
| 测试 | Python 1319（1 既有失败/1 skip）；JS 207 |
| 面板 | 5 Tab / 344 键 / 32 检查 / 19 secret |
| 情绪 | 8 维已改名；decider 运行态开启；key 在 `~/.pawbot/decider.key` |
| 大脑 | 已实现/自检；仅主人电脑工具；无子代理 |
| 数据守卫 | 快照已生效；dataDir 未启用 |
| 云同步 | local/webdav/http + 加密 + 冲突 + 近实时；真实 WebDAV 未验 |
| 部署 | 已出 full 包/GitHub 分支 zip；阿里云 Windows 安装进行到解压/依赖 |

**P0**：阿里云 Windows 跑通 + 记忆安全迁移。
**P1**：judge 接 decision；合并 main；启用 dataDir。
**P2**：真实 WebDAV；云同步面板按钮；真浏览器/读屏；补 HANDOFF 10-29 段。

---

## 18. 附录

### 18.1 术语

- **Host / Client**：DSH 的 Node 侧插件与浏览器侧面板。
- **brain / 大脑**：把一轮对话交给 DSH 进程内 Agent 的模式（可用电脑工具）。
- **preset**：DSH 的 agent 工具组合；`pawbot-brain` 只含电脑工具。
- **guard/restrict**：工具执行前拒绝 / 隐藏工具。
- **decider**：OpenRouter decisions 概率化模型，判「变不变/变什么/多强」。
- **数据守卫**：版本快照 + 可选外部 dataDir。
- **bundle**：DSH 插件包（本仓库）。

### 18.2 常用命令

```powershell
git -C D:\WorkBuddy\pawbot-dsh status --short
git -C D:\WorkBuddy\pawbot-dsh log --oneline -3
cd D:\WorkBuddy\pawbot-dsh; node test/client-meta-check.mjs
cd D:\WorkBuddy\pawbot-dsh\app; py -m unittest discover -s tools -p "test_*.py" -q
cd D:\WorkBuddy\pawbot-dsh\app; py tools\decider_once.py --text "群友：你今天怎么这么可爱" --addressed
cd D:\WorkBuddy\pawbot-dsh\app; py tools\cloud_sync.py status
powershell -ExecutionPolicy Bypass -File D:\WorkBuddy\pawbot-dsh\scripts\local-update.ps1
```

### 18.3 文件规模快照

| 部分 | 数量 |
|---|---|
| `index.js` / `client.js` | ~1564 / ~3716 行 |
| `lib/*.js` | 8 个（134~983 行） |
| `app/pawbot/*.py` | 65 个顶层 + `mem/` 8 + `bili_mcp/` 2 |
| `app/plugins/*.py` | 12 |
| `app/tools/*.py` | 20 脚本 + 81 测试 |
| `bili-watch/` | index + 16 lib + selfcheck |
| `test/*.mjs` | 7 |
| `docs/*.md` | 10 |
| `scripts/` | 6 |

### 18.4 文档索引

| 文档 | 内容 |
|---|---|
| 本文 | 结构/文件职责/实现/任务索引（新窗口先读） |
| `HANDOFF.md` | 历史决策与坑（数字以本文为准） |
| `BRAIN.md` / `BRAIN-VERIFICATION.md` | 大脑设计/协议/实测 |
| `DECIDER.md` | 决策模型请求/映射/key/费用 |
| `CLOUD-SYNC.md` | 云同步协议/加密/冲突 |
| `DATA-PERSISTENCE.md` | 升级不丢数据 |
| `UI-CURRENT.md` / `UI-PLAN-v2.md` | 面板现状/规划 |
| `2026-10-27-交付报告.md` | 10-27 批次报告 |
| `DEPLOY.md` | 安装/部署（含阿里云） |

---

### 18.5 全模块公开 API 速查（自动从源码 AST 提取）

> 只列公开函数/类（不含 `_` 开头）；`() ` 表示函数，裸名字表示类。看实现直接打开对应文件。

| 文件 | 公开 API |
|---|---|
| `account.py` | AccountActions |
| `agent.py` | Agent |
| `album.py` | Album |
| `animecheck.py` | configured(), cache_path(), parse_anime(), is_anime(), is_anime_cached() |
| `bili_post.py` | enabled(), today_count(), has_credential(), compose(), post_once(), scheduler() |
| `bili_scroll.py` | recent_notes(), read_note(), run_round(), actions(), action(), scheduler() |
| `bili_watch.py` | load_reports(), pick_report(), mark_shared(), request_watch(), save_sessdata(), has_credential(), format_for_model() |
| `bilibili.py` | share_allowed(), mark_shared(), ranking(), popular(), search(), pick_lively(), card_enabled(), card(), brief() |
| `brain.py` | BrainError, env_url(), env_token(), enabled(), status(), register(), unregister(), active_ctx(), tool_allowed(), turn() |
| `breaker.py` | Breaker |
| `bus.py` | Handler, Bus |
| `cloudsync.py` | SyncError, scrub(), sync_section(), crypto_available(), is_encrypted(), Adapter, LocalAdapter, HttpAdapter, WebdavAdapter, make_adapter() |
| `config.py` | Config |
| `context.py` | Context |
| `core.py` | Bot |
| `ctxwin.py` | speaker(), present(), weight(), build(), build_messages() |
| `decider.py` | DeciderError, resolve_key(), base_url(), model(), enabled(), decide(), mood_questions(), deltas_from_answers(), build_state(), decide_mood() |
| `diary.py` | Diary |
| `doutu.py` | search(), download() |
| `dsh_bridge.py` | is_configured(), bili_watch_status() |
| `emotion.py` | Emotion |
| `event.py` | Event, MessageEvent, NoticeEvent, RequestEvent, parse() |
| `groupinfo.py` | GroupInfo, for_bot(), describe() |
| `hobbies.py` | Hobbies |
| `imagesearch.py` | search_sogou(), search_360(), search_wallhaven(), search_pixiv(), pixiv_detail(), search_pixiv_public(), search_pixiv_ds(), pixiv_access_token(), search_pixiv_authed(), search_safebooru() |
| `judge.py` | JudgeLLM, judge_enabled(), judge_json() |
| `libido.py` | Libido |
| `life.py` | now_info(), holiday(), schedule_state(), quiet_now(), proactive_factor(), life_hint() |
| `llm.py` | LLMError, LLMClient |
| `log.py` | Logger |
| `longmem.py` | LongMemory |
| `media.py` | MediaRef, extract(), MediaLedger, ledger_for(), sticker_allowed(), resolve() |
| `memes.py` | MemeBook, main() |
| `memory.py` | Memory |
| `message.py` | Text(), At(), Image(), Record(), Reply(), Face(), normalize(), array_to_cq(), cq_to_array(), to_plain() |
| `moegirl.py` | search_titles(), intro(), lookup() |
| `onebot.py` | ActionError, Connection, OneBotServer |
| `people.py` | PersonBook |
| `perceive.py` | card_text(), talks_about_bot(), enrich(), render() |
| `persona.py` | Persona, append_line() |
| `pixiv_session.py` | PixivSession |
| `plugin.py` | on_command(), on_keyword(), on_regex(), on_message(), on_notice(), on_request(), PluginManager |
| `proactive.py` | Proactive |
| `qzone.py` | Qzone |
| `relations.py` | Relations, for_bot() |
| `reminders.py` | parse_when(), ReminderStore, ReminderService |
| `safety.py` | typo_plan(), SafetyGuard |
| `selfedit.py` | 分节(), 小节标题们(), SelfEditor |
| `speech.py` | transcribe() |
| `spice.py` | Spice, joke() |
| `stickers.py` | read_credential(), image_data_url(), parse_desc_reply(), vision_describe(), make_describer(), mood_from_tags(), desc_from_tags(), StickerError, StickerBook |
| `store.py` | Store |
| `style.py` | strip_emoji(), strip_actions(), cap_sentences(), drop_trailing_question(), polish() |
| `sync_realtime.py` | local_fingerprint(), RealtimeSyncer, start_realtime() |
| `tasks.py` | spawn(), pending(), drain() |
| `tools.py` | prepare_bili_delivery(), commit_bili_delivery(), ToolBox |
| `topic.py` | TopicTracker, mood_label(), Mood |
| `tts.py` | voice_request(), explicit_speech_text(), should_force_voice(), split_for_voice(), chunk_for_speech(), clean_for_speech(), synth(), to_record(), record_segments(), record_segment() |
| `turns.py` | update() |
| `video.py` | extract_frames(), local_path() |
| `vision.py` | data_url(), grid_data_url(), preview_urls(), collect_images(), describe(), see(), clear_cache() |
| `websearch.py` | deepseek_search(), deepseek_fetch(), fetch_smart(), x_search(), x_read(), search_bing(), search_360(), search_local(), search_duckduckgo(), search_deepseek() |
| `webui.py` | WebUI |
| `wsproto.py` | accept_key(), WSConnection, accept(), connect() |
| `mem/assemble.py` | 取配置(), 取实体库(), build() |
| `mem/bridge.py` | build() |
| `mem/budget.py` | 单层截断(), 分配(), 摘要行() |
| `mem/decay.py` | 取档(), 档位说明(), 抹时间(), 抹细节(), 截首句(), 渲染(), 需要补骨架(), 时间措辞(), 骨架提示() |
| `mem/entities.py` | EntityStore |
| `mem/layers.py` | l0_window(), l1_turn(), l2_days(), l3_entities() |
| `mem/migrate.py` | run() |
| `mem/write.py` | write_turn(), merge_day() |
| `bili_mcp/mcp_server.py` | load_credential(), get_cred(), bili_login(), bili_login_check(), bili_check_credential(), bili_search(), bili_comments(), bili_subtitle(), bili_danmaku(), bili_video_info() |

### 18.6 常用入口函数/类的一句话说明

| 入口 | 说明 |
|---|---|
| `core.Bot` | 装配所有子系统，`start/stop` 生命周期，`_on_event` 唯一事件入口 |
| `agent.Agent.run(ctx,text,history)` | 一轮对话：拼 prompt → 工具循环 → 返回 (文本,媒体) |
| `agent.Agent._brain_conv(ctx)` | 判断本轮是否交给 DSH 大脑 |
| `llm.LLMClient.chat_message(messages,tools)` | 模型调用（含 brain 分流/备援/熔断） |
| `tools.ToolBox.execute(ctx,name,args)` | 执行模型工具，返回 (text, media) |
| `mem.assemble.build(bot,ctx,text)` | 记忆 v2 拼装入口（失败退回 legacy） |
| `emotion.Emotion.update(text,config=…)` | 情绪更新：decider → judge → 关键词 |
| `decider.decide_mood(config,text,ctx,addressed,values)` | 决策模型情绪判断（失败 None） |
| `stickers.StickerBook.pick_with_meta(query,exclude)` | 按标签/描述挑表情包 |
| `perceive.enrich(bot,ev)` | 把 QQ 消息补全成模型可读上下文 |
| `cloudsync.push/pull/status` | 云同步三件套 |
| `sync_realtime.start_realtime(config,base)` | 启动近实时同步线程 |
| `brain.turn(conv,system,user,…)` | 调 Host 大脑桥 |
| `lib/dsh-brain.js` 的 `createBrainBridge()` | Host 侧大脑桥（注册 preset、建 Agent、工具闸） |
| `lib/data-guard.js` 的 `prepareData()` | 启动时版本快照/迁移 |
| `lib/webui-proxy.js` 的 `createPawbotProxy()` | 面板同源代理 |

**维护约定**：结构/文件职责变化改 §1/§2/§7/§12；状态变化改 §17；新增坑改 §16；
新增任务类型改 §14。专题细节放对应 `docs/*.md`，本文只做总览与索引。
