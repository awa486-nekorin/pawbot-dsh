# PawBot × DSH 插件 — 结构、项目与工作流程（2026-10-29 版）

> **给新窗口的第一份阅读材料（替代 10-26 版）。** 读完这份，你应该知道：这个仓库是什么、
> 每个目录干什么、要改一个功能该动哪个文件、怎么在本地验证、怎么上传、云端怎么部署，
> 以及这台机器/这个项目当前处于什么状态、有哪些**不能踩的坑**。
>
> 历史交接与决策背景见 `docs/HANDOFF.md`；界面现状见 `docs/UI-CURRENT.md` / `docs/UI-PLAN-v2.md`；
> 云同步见 `docs/CLOUD-SYNC.md`；数据保护见 `docs/DATA-PERSISTENCE.md`；决策模型见 `docs/DECIDER.md`。
>
> 最后核对：**2026-10-29** · 本机分支 `upload-2026-10-27`（工作区含本文件）· 远端 `main` = `6539deb`

---

## 0. 一分钟速览

| 项 | 值 |
|---|---|
| 仓库 | `D:\WorkBuddy\pawbot-dsh`（**同时就是这个 DSH 插件的源码本体**） |
| 远端 | `https://github.com/awa486-nekorin/pawbot-dsh`（public / MIT） |
| 当前分支 | `upload-2026-10-27`（已推 GitHub，含本文件）；`main` 还停在 `6539deb`，未合并 |
| 是什么 | 把「PawBot（QQ 机器人）」包装成 **DSH 受管服务**：起停、状态、日志、设置面板、一键安装、NapCat 编排；并内嵌「大肥鱼看 B 站」插件 |
| 语言 | Host/Client 半端 = JavaScript（ESM，Node ≥22）；本体 = Python 3.13（`app/`） |
| 入口 | `index.js`（Host）、`client.js`（浏览器面板）、`app/run.py`（Python 本体） |
| 安装形态 | DSH profile 里是**符号链接**：`@local/pawbot → link:D:/WorkBuddy/pawbot-dsh` |
| 数据位置 | `app/data/`（记忆、人设、config、梗库、账本…）；可选 `dataDir` 迁到插件外 |
| 包版本 | `package.json` = `1.0.0`；「2.0.1 / 2026-10-27 批次」是功能批次名，不是包版本 |
| 面板 | 5 分页：启动 / 状态 / API 密钥 / 设置 / 人设；344 个中文配置键（74 常用 + 270 进阶） |
| 测试 | Python **1319**（1 个既有 pixiv 失败 / 1 skip）；JS **207**（8 个套件） |
| 大脑 | 「大肥鱼大脑」= DSH 进程内 Agent（`lib/dsh-brain.js`），默认只给主人用电脑工具；**不能开子代理/工作流** |

**最重要的一条**：profile 是 `link:`，**仓库文件就是线上插件**。改完 → 重启 DSH / 重载插件 → 生效，
不存在「打包再复制」这一步。

---

## 1. 目录结构

```
D:\WorkBuddy\pawbot-dsh\
├─ index.js                  ★ Host 主插件（~1564 行）：子进程管理、/pawbot-ui 路由（18 条）、
│                             一键安装、NapCat 编排、10 个 DSH 工具、数据守卫、大脑桥、工具桥
├─ client.js                 ★ 浏览器面板（~3716 行）：5 分页 + 344 个 config key 中文表单 +
│                             API 统一页（分组/遮罩/diff/吸附保存条/详细参数折叠）
├─ cordis.patch.yml          ★ bundle patch：注册两条 Host 插件行（pawbot / pawbot-bili-watch）
├─ package.json              bundle 清单：exports / files / engines / dsh.bundle / dsh.client
├─ icon.svg / locale/        bundle 图标与中英文案
├─ lib/                      Host 侧 8 个模块
│   ├─ setup.js              一键安装：venv + pip + config 引导 + NapCat 启动状态机
│   ├─ webui-proxy.js        /pawbot-ui/* 同源代理：CSRF、415/413、secrets 遮罩、502 映射
│   ├─ napcat.js             NapCat 编排（docker / binary / 手动步骤）
│   ├─ dsh-tool-bridge.js    Host→PawBot 只读工具桥（allowlist 只有 bili_watch_status）
│   ├─ dsh-brain.js          ★ 大肥鱼大脑：在 DSH 进程里 create Agent + 挂电脑工具 preset
│   ├─ data-guard.js         ★ 数据守卫：版本快照 + 可选外部 dataDir（junction/symlink）
│   ├─ hostdeps.js           从宿主树解析 DSH 依赖（bundle 里不装第二份）
│   └─ config-utils.js       config 深合并、遮罩、可更新键过滤
├─ bili-watch/               「大肥鱼看 B 站」插件（bundle 的 subpath export）
│   ├─ index.js              工具注册（bili_watch / bili_watch_status）、定时器、队列
│   ├─ lib/                  抓取、判重、候选池、字幕、模型守门、报告、去敏
│   ├─ locale/               中英文案
│   └─ selfcheck.mjs         离线下限自检（52 项）
├─ test/                     Host 侧 JS 测试（离线、全假依赖），共 7 个 .mjs
├─ scripts/                  6 个脚本
│   ├─ local-update.ps1      ★ 本机更新：备份 data + 语法检查 + 快速测试 + 重启提示
│   ├─ pack-server.ps1/.sh   ★ 打「上传/部署包」：默认不含数据；-IncludeSecrets 含 key+人设
│   ├─ apply-update.sh       服务器离线包更新（只换代码，绝不碰 app/data）
│   └─ collect-server-changes.ps1/.sh  把服务器改动收成 zip 带回
├─ docs/                     10 份 md（本文件 + HANDOFF / BRAIN / BRAIN-VERIFICATION / UI-CURRENT /
│                             UI-PLAN-v2 / DECIDER / CLOUD-SYNC / DATA-PERSISTENCE / 交付报告）
├─ app/                      Python 本体（DSH 子进程）
│   ├─ run.py                入口：Bot(BASE_DIR) + 信号处理
│   ├─ requirements.txt      pillow / websockets / cryptography
│   ├─ web/index.html        PawBot 自己的最小 WebUI 页面
│   ├─ pawbot/               ★ 65 个模块（+ mem/、bili_mcp/ 子包），见 §2
│   ├─ plugins/              ★ 12 个消息/事件插件
│   ├─ tools/                20 个运行脚本 + 81 个 Python 测试（test_*.py）
│   └─ data/                 ★ 运行时数据（记忆/人设/config/账本/备份），见 §5
├─ README.md / DEPLOY.md / LICENSE / .gitignore
```

---

## 2. 执行层与模块地图

理解这个项目 = 理解四条边界。

```
┌──────────────────────── DSH Host 进程（Node） ──────────────────────────────┐
│ index.js                                                                    │
│  ├─ 管 PawBot 子进程（spawn / 停止 / 健康 / 日志 / 崩溃重启）                 │
│  ├─ 注册 /pawbot-ui/* 同源代理（16 个 exact + 2 个 prefix = 18 条）           │
│  ├─ 注册 /pawbot-dsh-bridge/execute（只读工具桥）                             │
│  ├─ 注册 /pawbot-dsh-brain/turn（大肥鱼大脑：交给 DSH Agent 跑一轮）           │
│  ├─ data-guard：版本快照 / 外部 dataDir 迁移                                  │
│  └─ 注册 10 个 DSH 工具：pawbot_status/logs/restart/health/setup/napcat/     │
│     config_get/config_set/data_backup/brain                                 │
└───────────────┬───────────────────────────────────────────┬─────────────────┘
                │ spawn(python run.py)                      │ HTTP loopback
                │ + 启用时注入 BRAIN_URL / BRIDGE_URL/TOKEN   │（只读桥 + 大脑桥）
┌───────────────▼──────────────── Python 子进程（PawBot） ───▼─────────────────┐
│ app/run.py → pawbot/core.py（Bot）                                          │
│  ├─ wsproto/onebot.py   ← NapCat 反向 WS（QQ 事件入口 :6199）                │
│  ├─ plugins/*.py        消息与事件钩子（12 个）                               │
│  ├─ pawbot/agent.py     模型对话循环（工具调用 + 收口）                        │
│  ├─ pawbot/tools.py     模型可用工具（search/bili/qzone/album/moegirl…）      │
│  ├─ pawbot/decider.py   ★ 类 JEV 决策模型（OpenRouter decisions）             │
│  ├─ pawbot/cloudsync.py ★ 云同步（local/webdav/http + 加密 + 冲突）           │
│  ├─ pawbot/sync_realtime.py ★ 近实时同步线程                                  │
│  ├─ pawbot/emotion.py   八维情绪矩阵（已改名为 开心/兴奋/惊讶/烦躁/低落/     │
│  │                       感动/得意/害羞；decider 开启时优先走决策模型）        │
│  └─ pawbot/webui.py     自己的 HTTP WebUI（:6185，面板代理的目标）            │
└──────────────────────────────────────────────────────────────────────────────┘

┌────────────────── 浏览器半端（client.js）──────────────────┐
│ DSH 设置页 PawBot：启动 / 状态 / API 密钥 / 设置 / 人设      │
│ 全部请求走同源 /pawbot-ui/*，不直连 Python                    │
└──────────────────────────────────────────────────────────────┘

┌──────────── ★ 大肥鱼大脑（可选）────────────┐
│ PawBot(agent.py) ──HTTP──▶ DSH Host /pawbot-dsh-brain/turn │
│ Host 用 agents.create() 造一个 session（origin=subagent）   │
│ 挂 pawbot-brain preset：只有电脑工具（shell/fs/search/web） │
│ + 每轮注入 PawBot 人设 & PawBot 业务工具代理                 │
│ 身份闸：电脑工具默认只给主人（brainOwnerOnly=true）          │
│ 工具闸：allowlist + guard 执行前拒绝；无 subagent/workflow    │
└──────────────────────────────────────────────────────────────┘
```

### `app/pawbot/`（65 个模块，按职责）

| 组 | 文件 | 作用 |
|---|---|---|
| 核心 | `core.py` `event.py` `bus.py` `turns.py` `tasks.py` | Bot 装配、事件总线、回复回合 |
| 协议 | `wsproto.py` `onebot.py` `message.py` | NapCat 反向 WS、OneBot 动作、消息段 |
| 对话 | `agent.py` `llm.py` `persona.py` `style.py` `safety.py` `context.py` `ctxwin.py` | 模型循环、提示词、去 AI 味、延迟与限额 |
| 记忆 | `memory.py` `store.py` `longmem.py` `mem/` `people.py` `relations.py` `diary.py` `hobbies.py` | 分层记忆、人物侧写、关系网 |
| 知识 | `memes.py` `moegirl.py` `websearch.py` | 梗库、本地百科、联网搜索 |
| B 站 | `bilibili.py` `bili_watch.py` `bili_scroll.py` `bili_post.py` `bili_mcp/` | 找片/看片/刷片/自动发动态 |
| 外部 | `qzone.py` `account.py` `dsh_bridge.py` | QQ 空间、账号动作、DSH 只读桥 |
| 媒体 | `media.py` `imagesearch.py` `pixiv_session.py` `stickers.py` `doutu.py` `album.py` `vision.py` `video.py` `animecheck.py` | 图/表情/看图/视频抽帧 |
| 情绪 | `emotion.py` `decider.py` `judge.py` `topic.py`（Mood 只读视图） | 八维情绪、决策模型、画质判官、心情值 |
| 同步 | `cloudsync.py` `sync_realtime.py` | 云同步适配器/加密/冲突、近实时线程 |
| 输出 | `tts.py` `speech.py` | 语音合成、语音转写 |
| 运维 | `config.py` `log.py` `plugin.py` `webui.py` `breaker.py` `reminders.py` `proactive.py` `life.py` `spice.py` `libido.py` `selfedit.py` `groupinfo.py` `perceive.py` | 配置、插件加载、主动发言、限额 |

### `app/plugins/`（12 个钩子）

`ai_chat.py`（主对话，含 B 站送达记账）、`basic.py`、`qq_actions.py`、`account.py`、
`group_events.py`、`group_tools.py`、`life_events.py`、`hobbies.py`、`relations.py`、
`random_interject.py`、`spice.py`、`pixiv.py`

### DSH 工具与路由

- **Host 工具（10）**：`pawbot_status` `pawbot_logs` `pawbot_restart` `pawbot_health`
  `pawbot_setup` `pawbot_napcat` `pawbot_config_get` `pawbot_config_set`
  `pawbot_data_backup` `pawbot_brain`
- **bili-watch 工具（2）**：`bili_watch` `bili_watch_status` → 合计 **12**
- **面板路由（18）**：2 个 prefix + 16 个 exact（status/config/persona/reload-plugins/plugins/reload/
  send/restart/context/memory/setup/status/setup/run/napcat/status/napcat/setup/napcat/qr/boot/boot/status）

---

## 3. 「要改 X，该动哪个文件」

| 需求 | 主要文件 | 连带要改 |
|---|---|---|
| 新增/修改**模型工具** | `app/pawbot/tools.py`（`SCHEMAS` + `ToolBox.available()` + `_t_xxx`） | 权限穿 `ctx`；补 `app/tools/test_*.py` |
| 改**对话行为/提示词** | `app/pawbot/agent.py`、`app/pawbot/persona.py` | 系统提示词在 `app/data/persona.md`（红线，见 §9） |
| 改**情绪/心情** | `app/pawbot/emotion.py`；决策模型 `app/pawbot/decider.py` | `stickers.EMOTION_TAGS`、`topic._POSITIVE/ NEGATIVE_DIMS`、`config emotion.*`、`tools/test_emotion.py` 的维度契约测试 |
| 改**画质判官** | `app/pawbot/judge.py`；`vision.py`（多模态） | 配置 `judge.*`；若接 decision 模型见 §11 待办 |
| 改**多模态/看图** | `llm.vision_*` + `media.sticker_vision_*` + `app/pawbot/vision.py`、`stickers.py` | DSH 凭证 `TOGEMINI_API_KEY` 或面板 key |
| 改**云同步/共享** | `app/pawbot/cloudsync.py`、`sync_realtime.py` | `app/tools/cloud_sync.py`、`config sync.*`、面板 metadata、`tools/test_cloudsync.py`/`test_realtime_sync.py` |
| 改**数据保护/升级** | `lib/data-guard.js` + `index.js`（加载时 prepareData） | `test/data-guard-offline.mjs`、`docs/DATA-PERSISTENCE.md` |
| 改**DSH 大脑/电脑工具** | `lib/dsh-brain.js`、`index.js`（brain 路由/工具/配置） | preset 行、allowlist、`test/dsh-brain-offline.mjs` |
| 改 **DSH 工具/路由** | `index.js` + `lib/*.js` | `test/dsh-tool-bridge-*.mjs`、`test/offline.mjs` |
| 改 **PawBot↔DSH 桥** | Host：`lib/dsh-tool-bridge.js`；Python：`app/pawbot/dsh_bridge.py` | 两侧协议一起改 + 各自测试 |
| 改**设置面板** | `client.js` | 只做静态/jsdom 检查；真浏览器未验 |
| 新增**配置项** | ① `app/pawbot/config.py` 的 `DEFAULT` ② `client.js` 的 `COMMON`/`ADVANCED` ③ `test/client-meta-check.mjs` 计数 | **三处必须同步**，否则 metadata 测试红 |
| 新增**定时任务** | 新模块 + `core.py` 的 `start()`/`stop()` | 配置默认值 + 面板字段 |
| 改**B 站看片插件** | `bili-watch/index.js`、`bili-watch/lib/*` | `bili-watch/selfcheck.mjs` |
| 改**部署/更新** | `scripts/`、`DEPLOY.md`、`docs/DATA-PERSISTENCE.md` | 打包 + 云端验证 |

---

## 4. 制作新功能的标准流程

1. **先定层**：Python 行为？Host 能力？面板字段？还是 DSH 大脑工具？—— 决定动哪个文件（§3）。
2. **定权限模型**：谁能触发？要不要 `ctx`？要不要持久化账本？对外发布类默认关闭。
3. **写配置**：`config.py DEFAULT`（对外发布类必须 `False`）→ `client.js` 中文 label + help（≥8 字）+ 影响徽章。
4. **实现**：并发用 `asyncio.Lock`；有额度先落盘 pending 再调后端；只调一次；回执验证；失败降级。
5. **写测试**：`app/tools/test_<feature>.py`（假对象/假连接，不联网、不连 QQ）或 `test/*.mjs`（纯离线）。
6. **跑验证**（§7），修到绿。
7. **更新文档**：`docs/HANDOFF.md` 现状表 + 本文件 §11 + 对应专题文档。
8. **提交上传**（§8）：先扫 secrets，再 commit/push；**keys 永不入库**。

### 对外发布类功能的硬规则（照 `qzone.py` / `bili_post.py`）

```
enabled 必须是字面 True（"true" / 1 不算）
↓ 需要凭据（owner_ids / bili_jct）
↓ 每日额度：正整数字面校验，从磁盘账本读，不信任内存计数
↓ 先写 pending 并落盘（写不进去 → 不调后端）
↓ 只调一次后端
↓ 回执验证（success is True）
↓ 成功 confirmed / 超时取消 unconfirmed（额度照扣，不自动重试）
↓ 最终落盘失败 → 不回报成功
```

### AI/外部服务的通用规则

- 决策模型/判官/看图都是**增强**，失败必须静默降级，不能阻塞聊天；
- 单次请求必须有超时（情绪 decider 默认 12s，judge 20s，视觉 30s）；
- key 只从「面板 secret / 环境变量 / DSH 凭证 / 专用 key 文件」读，日志只记错误类型；
- 概率化接口（OpenRouter decisions）要按 response 实际形状解析：`noul` ∈ [0,1]、`choice` 有
  `probabilities/confidence`、`score` ∈ [0,1]（不是 legend 下标）。

---

## 5. `app/data/` — 数据区、升级与备份

**所有运行时数据都在 `app/data/`**（各模块统一 `os.path.join(base_dir, "data", ...)`）。

| 类别 | 例子 | git | 升级/更新时 |
|---|---|---|---|
| **被跟踪** | `config.json`、`persona.md`、`memory/facts.md`、`memory/impression.md`、`memory/slang.md`、`hobbies.json`、`plugin_state.json` | 已提交 | `git reset --hard` / `checkout` **会**；普通拉取有本地修改时会拒绝 |
| **被忽略** | `sessions.json`、`relations.json`、`emotion/mood/libido/spice/topic/media_ledger/proactive_state.json`、`album/`、`people/*.md`、`diary/*.md`、`memory/entities.jsonl`、`stickers/`、`image_cache/`、`browser/`、`bili_notes/`、`napcat/`、`run.log`、`bili_credential.json` | `.gitignore` | **不会** |
| **新增运行产物** | `data/backups/plugin-update-*`（数据守卫快照）、`.data-guard.json`、`.sync-state.json`、`config.synced.json`、`sync-conflict-*/`、`backup-sync-*/`、`data.migrated-*/` | `.gitignore` | **不会**；不要提交 |

### 数据守卫（`lib/data-guard.js`）

- 插件加载时比较 `package.json` 版本与 `data/.data-guard.json`：**版本变化 → 快照**
  `config.json + persona.md + 记忆文件` 到 `data/backups/plugin-update-<版本>-<时间>/`，保留 5 份。
- 宿主 Config `dataDir` 非空时，会把 `app/data` **不覆盖地**迁到外部目录（如 `D:/PawBotData`），
  再把 `app/data` 换成 junction/symlink；升级整包替换 `app/` 后下次启动自动重建。**本机目前未开**
  `dataDir`，只开快照（本机已有 `plugin-update-1.0.0-20261027-195445` 快照）。
- 工具：`pawbot_data_backup action=status|backup|list`。

### 加密与凭证

- 含 API key 的 `config.json` 同步必须设 `sync.passphrase`（AES-256-GCM；未设时 fail-closed）。
- 实际使用的 key 不写进仓库：DSH 凭证 `~/.dsh/.credentials.yaml`、决策 key `~/.pawbot/decider.key`、
  面板 secret 字段。提交前必查 `git diff --cached` 里没有 `sk-` / `credentials` / `app/data` 变更。

---

## 6. 配置体系（三处同步 + 运行时深合并）

```
app/pawbot/config.py  DEFAULT（38 个区段 / 344 个面板键）  ←── 唯一真实默认值
        ↓ Config.load 深合并
app/data/config.json                     ←── 运行时覆盖（可能只是部分键，不是完整拷贝）
        ↓ bot.config.update(payload)
pawbot webui /api/config（改完即生效）    ←── 定时器类配置要重启
        ↓
client.js  COMMON(74) / ADVANCED(270)     ←── 中文 label + help ≥8 字 + 影响徽章 + 19 个 secret 控件
        ↓
test/client-meta-check.mjs                ←── 32 项检查（344 键全有 metadata、secret 被 API 分组覆盖）
```

- **常用页固定 74 个 key**；其余全进「进阶」；目前共 **344 key = 74 + 270**，38 个区段。
- 面板 5 分页：启动 / 状态 / API 密钥 / 设置 / 人设。API 页按服务分组（llm/judge/search/credentials/
  speech/sticker/conn/sync），只显示常用项，详细参数折叠；secret 遮罩、diff 保存、保存前确认。
- 运行时 `config.json` 可能只有面板改过的键 —— **不要把它当完整默认值文件**；缺的键由 `DEFAULT` 补。
- 关键配置默认：`llm.provider=dsh`、`emotion.decider_enabled` 默认 False（本机运行态已开）、
  `sync.enabled=false/mode=manual`、`judge.decider_enabled` 仅方案（见 §11）。

---

## 7. 验证工作流（全部离线）

```powershell
# ── JS（仓库根，共 207）────────────────────────────────────
node test/offline.mjs                          # 19 passed  代理动作 + CSRF/413/415/502
node test/client-meta-check.mjs                # 32 passed  344 key metadata ↔ config.py 一致
node test/dsh-brain-offline.mjs                # 20 passed  大脑桥：身份闸/超时/中断/降级
node --test test/dsh-tool-bridge-offline.mjs   # 15 passed  工具桥协议/限流/投影
node test/dsh-tool-bridge-host.mjs             # 3 passed   与 Host 路由接线
node test/setup-offline.mjs                    # 57 passed  一键安装状态机
node test/data-guard-offline.mjs               # 9 passed   数据守卫：快照/迁移/重装不丢 key
node bili-watch/selfcheck.mjs                  # 52 passed  B 站插件下限自检
node --check index.js; node --check client.js  # 语法

# ── Python（在 app\，先 $env:PYTHONPATH="."）───────────────
py -m unittest discover -s tools -p "test_*.py" -q
#   预期：1319 ran / 1 failure（既有 pixiv 排序）/ 1 skipped
```

### 预期内的「失败」——不要当成你改坏了

| 现象 | 原因 |
|---|---|
| `test_pixiv_session.py::test_filters_and_sorting` 1 个失败 | 既有问题（排序断言），与主线无关 |
| `setup-offline.mjs` / `selfcheck.mjs` 在部分机器上 EPERM | 测试硬编码临时路径，环境限制 |
| PowerShell 里 Python 测试「失败」但其实 OK | stderr 噪音；看 `Ran N tests` / `OK` |
| `ConvertFrom-Json` 解析中文 JSON 报错 | PS 5.1 按 GBK 读 UTF-8；用 Python 或 `-Encoding utf8`，文件本身没问题 |

```powershell
# 无 __pycache__ 的语法检查
py -c "import ast,pathlib;[ast.parse(p.read_text(encoding='utf-8'),str(p)) for p in pathlib.Path('.').rglob('*.py')];print('AST OK')"
```

---

## 8. 上传 / 更新 / 云端部署工作流

### 关键环境事实

| 能力 | 本会话实测 |
|---|---|
| 写 `D:\WorkBuddy\pawbot-dsh` | ✅ 可以（write/edit、git 都能用） |
| git add/commit/push | ✅ 可以（凭据在 Windows Credential Manager，GCM） |
| 写 `C:\Users\Administrator\...` | ✅ 可以 |
| 写 `C:\` 根 / 系统目录 | ❌ 拒绝 |
| DSH GUI 地址 | 每次启动可能不同（本会话 `http://127.0.0.1:50455`），**别写死** |

### A. 只在本机生效

```powershell
cd D:\WorkBuddy\pawbot-dsh
powershell -ExecutionPolicy Bypass -File scripts\local-update.ps1
# 备份 app/data → 语法检查 → 快速测试 → 打印重启步骤
```
重启 **DSH**（Host 层）+ 面板里重启 **PawBot**（Python 层）。

### B. 上传 GitHub（当前状态）

- 公开仓库 **不能放 key**；只提交代码/文档/人设，`secrets/`、`dist/`、`app/data/backups` 都不进库。
- 本机分支 `upload-2026-10-27` 已推（含本文件）；`main` 仍是 `6539deb`（未合并，因为两边都有提交）。
- 下载分支 zip：`https://github.com/awa486-nekorin/pawbot-dsh/archive/refs/heads/upload-2026-10-27.zip`
- 提交前检查：

```powershell
git diff --cached --name-only | Select-String "credentials|app/data/"
git diff --cached | Select-String "sk-[A-Za-z0-9]|BEGIN .*PRIVATE KEY"
```
（测试里的 `sk-unit-test-placeholder` 之类假值除外。）

### C. 阿里云 Windows 服务器部署（当前目标，进行中）

服务器 `47.111.130.215` 是 **Windows OpenSSH**（不是 Linux，不能用 systemd/bash 那套）。

```powershell
# 本地：打「含 key + 人设」包（不含记忆）
cd D:\WorkBuddy\pawbot-dsh
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pack-server.ps1 -IncludeSecrets
# 产物：dist\pawbot-dsh-<版本>-<时间>-full.tar.gz + .sha256

# 上传（用户名/密钥按实际）
scp dist\pawbot-dsh-*-full.tar.gz Administrator@47.111.130.215:C:/pawbot/
scp dist\pawbot-dsh-*-full.tar.gz.sha256 Administrator@47.111.130.215:C:/pawbot/
```

```powershell
# 服务器（Windows PowerShell）：先校验、再解压到 C:\pawbot
cd C:\pawbot
$tgz = (Get-ChildItem -Filter *-full.tar.gz | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
tar -xzf $tgz
cd .\pawbot-dsh
# 放 key
New-Item -ItemType Directory -Force "$env:USERPROFILE\.dsh","$env:USERPROFILE\.pawbot" | Out-Null
Copy-Item .\secrets\credentials.full.yaml "$env:USERPROFILE\.dsh\.credentials.yaml" -Force
Copy-Item .\secrets\decider.key "$env:USERPROFILE\.pawbot\decider.key" -Force
# 装依赖 + 启动
py -3 -m venv app\.venv
.\app\.venv\Scripts\python -m pip install -r .\app\requirements.txt
cd app; ..\app\.venv\Scripts\python run.py
```

- 若服务器装 DSH：不要 `run.py`，改成 `dsh plugin add link:C:\pawbot\pawbot-dsh`。
- 传完删掉含 key 的 tar.gz；服务器上 `chmod`/ACL 收紧凭证文件。
- 记忆迁移：`py tools\cloud_sync.py push/pull`（加密）或手工 scp `app/data`；**不要**指望安装包带记忆。

### D. 离线更新包（服务器无 GitHub 时）

```powershell
git archive --format=tar.gz -o pawbot-dsh-update.tgz --prefix=pawbot-dsh/ HEAD -- . ":(exclude)app/data"
```
```bash
# Linux 服务器
bash scripts/apply-update.sh pawbot-dsh-update.tgz
```

### E. 在 DSH 里「添加插件」

接受 GitHub 地址或本地目录；当前 profile 已通过 `link:` 安装同一插件，**先卸载旧的再装**，否则两份。

---

## 9. 红线（不要碰）

1. `app/data/persona.md`、`app/pawbot/style.py` —— 人设与文风，**未经明确要求不要改**。
2. **凭据永不入库、永不打印**：`SESSDATA`、`bili_jct`、`api_key`、`pbdsh_*`、decider key。测试只能假值。
3. **对外发布默认关闭**：`qzone.*`、`bili_post_enabled` 等写操作类默认 `False`。
4. **白名单语义不许改**：黑名单群优先；用户/群白名单 AND；空列表=该维度不限；`account.owner_ids` 不是白名单通行证。
5. **选择 ≠ 送达**：B 站视频只有真正发出去了才写 `shared`。
6. **正常聊天不联网、不自动改梗库**：`moegirl` 聊天路径固定 `allow_network=False`。
7. **没有实机验证就不要说验证过**：报告分开「代码完成 / 离线测试通过 / 环境受阻 / 未验证」。
8. **升级不能动记忆**：只能用数据守卫快照、云同步 pull、或手工 scp；禁止 `git reset --hard` 覆盖 `app/data`。
9. **公开 GitHub 不放任何 key**（含 `secrets/` 目录、含 key 的 tar）。要整包下载只能走私有仓库/云盘。

---

## 10. 踩过的坑（2026-10 版浓缩）

| 症状 | 原因 / 对策 |
|---|---|
| `.ps1` 一跑就 ParserError / 中文乱码 | PS 5.1 把无 BOM UTF-8 当 GBK → 脚本存成 **UTF-8 with BOM** 或只用英文 |
| `tar -xzf *.tar.gz` 没解压/报错 | PS 5.1 不展开通配符；用 `(Get-ChildItem ...).FullName` 再传给 tar |
| `ConvertFrom-Json` 报「无效对象」 | PS 读 UTF-8 无 BOM 中文文件会乱码；用 Python 或 `Get-Content -Encoding utf8`，文件本身没坏 |
| 在错误目录 `Remove-Item .\app -Recurse` | 先 `pwd` + `Get-ChildItem` 确认；本次差点把 `C:\pawbot\app` 当数据删（当时只是空 venv） |
| 数据守卫迁移 `rename EPERM` | Windows 占用/杀软；已加重试 + 清空后重建联接的兜底 |
| `cloud_sync.py push` 报 `CONFIG_INVALID local 需要 sync.url` | 预期：`sync.enabled/provider/url` 没配；服务器已有记忆时不必跑 |
| OpenRouter decisions 解析错 | `score` 是 0~1 连续值，不是 `legend` 下标；`noul` 也是 0~1 |
| 情绪维度改名后测试红/行为怪 | 必须同步：`emotion.DIMS`、`decider.EMOTIONS`、`stickers.EMOTION_TAGS`、`topic` 正负维度、`media.sticker_force_dominant`；有维度契约测试 |
| 表情包太多/太少 | `media.sticker_chance=0.12`、`persona.sticker_gap=[3,6]`；但运行态 config 显式旧值会覆盖默认 |
| 改了 config 行为没变 | 面板改立即生效；**定时器/线程**只在启动时建立（`*_times`、`sync.mode=realtime` 要重启） |
| DSH GUI 连不上 | 端口每次可能不同，别写死；以运行时给的 URL 为准 |
| GitHub push 出警告但成功 | git 把进度写 stderr，PowerShell 当错误；看 `-> branch` 和 `ls-remote` |
| 远端 main 与本机不一致 | 两边各有提交；本机在 `upload-2026-10-27` 分支，不要 `reset --hard main` 覆盖 |

---

## 11. 当前状态快照（2026-10-29）

| 项 | 状态 |
|---|---|
| 本机分支 | `upload-2026-10-27` = 最新提交（含本文件，已推） |
| 远端 | `main` = `6539deb`；分支已推；未合并、未开 PR |
| 工作区 | 干净（本文件与 HANDOFF 已提交）；未跟踪：`docs/por-20261027/`（人设草稿，未提交） |
| Python 测试 | **1319 ran** / 1 既有失败 / 1 skipped |
| JS 测试 | **207**（19+32+20+15+3+57+9+52） |
| 面板 | 5 分页 / 344 key（74 常用 + 270 进阶）/ 32 项检查全过 / 19 个 secret 控件 |
| 情绪 | 8 维已改：开心 兴奋 惊讶 烦躁 低落 感动 得意 害羞；`emotion.decider_enabled=true`（运行态），key 在 `~/.pawbot/decider.key` |
| 大脑 | DSH Agent 桥已实现并自检通过；默认只给主人电脑工具；无子代理/工作流能力 |
| 数据守卫 | 版本快照已生效（`plugin-update-1.0.0-20261027-195445`）；`dataDir` 未启用 |
| 云同步 | local/webdav/http + 加密 + 冲突策略 + 近实时（local provider 实测推送成功）；真实 WebDAV 未验 |
| 部署 | 已产出含 key+人设的 `-full` 包与 GitHub 分支 zip；阿里云 Windows 服务器安装进行到「解压/装依赖」 |
| 未验证 | 面板真浏览器/读屏、QQ 空间真机、B 站真凭据发布、真实 WebDAV、阿里云最终运行、判官接 decision |

### 待办（按优先级）

- **P0**：阿里云 Windows 上跑通 PawBot（venv/pip/keys），确认 `/api/status` 与群聊；把记忆安全迁过去（cloud sync 加密或 scp）。
- **P1**：把 `judge`（画质判官）接 decision 模型（方案已写在 `docs/DECIDER.md` 的「另一台 DSH 提示词」里；本机未实现，要补 `judge.py` 与测试）。
- **P1**：两条线合并：`upload-2026-10-27` 与远端 `main`（`6539deb`）的差异要有一次正式 merge/PR，别长期分叉。
- **P1**：数据守卫启用 `dataDir`（如 `D:/PawBotData`），把升级风险从「能恢复」降到「碰不到」。
- **P2**：真实 WebDAV（坚果云/Nextcloud）联调；云同步 UI（面板目前只暴露字段，没有 push/pull 按钮）。
- **P2**：面板真浏览器 / 读屏 / 深色模式实测；目前只有 jsdom 与离线检查。
- **P2**：补 `docs/HANDOFF.md` 的 10-27→10-29 段（本文件可作索引）。

---

## 12. 常用命令速查

```powershell
# 状态
git -C D:\WorkBuddy\pawbot-dsh status --short
git -C D:\WorkBuddy\pawbot-dsh log --oneline -3
git -C D:\WorkBuddy\pawbot-dsh ls-remote origin refs/heads/main refs/heads/upload-2026-10-27

# 本机更新（备份+检查+提示）
powershell -ExecutionPolicy Bypass -File D:\WorkBuddy\pawbot-dsh\scripts\local-update.ps1

# 只跑面板一致性
cd D:\WorkBuddy\pawbot-dsh; node test/client-meta-check.mjs

# 只跑 Python 聚焦
cd D:\WorkBuddy\pawbot-dsh\app; py -m unittest tools.test_emotion tools.test_decider tools.test_cloudsync tools.test_realtime_sync -q

# 决策模型试一句
cd D:\WorkBuddy\pawbot-dsh\app; py tools\decider_once.py --text "群友：你今天怎么这么可爱" --addressed

# 云同步
cd D:\WorkBuddy\pawbot-dsh\app; py tools\cloud_sync.py status
py tools\cloud_sync.py push          # 需先配 sync.provider/url/passphrase
py tools\cloud_sync.py watch         # 前台观察实时同步

# 打部署包（含 key+人设，不含记忆）
cd D:\WorkBuddy\pawbot-dsh; powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pack-server.ps1 -IncludeSecrets

# 看 DSH profile 的安装形态
Select-String -Path "$env:USERPROFILE\.dsh\profiles\desktop\package.json" -Pattern 'pawbot'
```

---

## 13. 与 10-26 版的主要差异

| 领域 | 10-26 | 10-29 |
|---|---|---|
| 面板 | 4 分页 / 306 key | **5 分页 / 344 key**；API 统一页、折叠详细参数、搜索、逐项撤销、保存确认 |
| 大肥鱼运行方式 | 直连 API / 外部模型 | **DSH 进程内 Agent**（大脑桥，电脑工具仅主人）；人设明确「跑在服务器 DSH」 |
| 情绪 | 8 维（含好奇/亲昵/疲惫） | **8 维改名**（惊讶/感动/害羞）；新增 **decision 模型**（OpenRouter pplx-decider） |
| 表情包 | 高频 | **2.0 低频 + 情绪/描述匹配 + 视觉描述回填** |
| 语气 | 原样 | `reply.tone_trim` 削句尾语气词（确定性哈希） |
| 数据安全 | 靠 `.gitignore`/手工备份 | **数据守卫**（版本快照 + 可选外部 dataDir） |
| 同步 | 无 | **云同步**（local/webdav/http、加密、冲突、近实时）+ CLI |
| 部署 | 只有 Linux 假设 | **Windows 阿里云**打包/部署流程 + `-IncludeSecrets` 全量包 |
| 测试 | Python 1230 / JS 约 128 | **Python 1319 / JS 207** |
| Git | 本机 37 个未提交 | 已提交并推 `upload-2026-10-27`；`main` 仍待合并 |

---

**维护约定**：以后每次大改动，更新本文件 §0、§11、§13，以及 `docs/HANDOFF.md` 的坑列表；
本文件是「结构与流程」的唯一入口，专题细节放各自 `docs/*.md`。
