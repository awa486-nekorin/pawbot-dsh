# PawBot × DSH 插件 — 交接文档（新窗口先读这一份）

> **给下一个 Agent**：你没有之前的对话上下文。这份文档是自包含的——读完你应该知道：
> 这个项目是什么、代码在哪、现在做到哪一步、哪些硬规则不能碰、下一步该干什么、哪些坑已经踩过。
>
> 最后更新：2026-10-26 · 对应 commit `ff19c36`

---

## 0. 三十秒上手

```
仓库（同时也是 DSH bundle 源码）  D:\WorkBuddy\pawbot-dsh
GitHub                           https://github.com/awa486-nekorin/pawbot-dsh  (public, MIT, main)
当前 HEAD                         ff19c36  (6 commits)
DSH profile 里的安装形态           link:D:/WorkBuddy/pawbot-dsh → node_modules/@local/pawbot
Python 本体（随仓库自带）           app/   （180 个被追踪文件 / 2.06 MB）
共享数据目录                       D:/WorkBuddy/pawbot-bili-watch-data
另一台机器（云服务器）              Windows，**没有 VPN**，插件目录没有 .git
```

三条命令验证一切还活着：

```powershell
cd D:\WorkBuddy\pawbot-dsh
node test/offline.mjs              # 期望 19 passed
node test/client-meta-check.mjs    # 期望 18 passed
node bili-watch/selfcheck.mjs      # 期望 52 passed
node test/setup-offline.mjs        # 期望 57 passed
```

---

## 1. 这个项目是什么

**一句话**：把已有的一整套 QQ 机器人 **PawBot**（Python，~2.2 万行）包装成一个 **DSH（DeepSeek Harness）插件**，让它在 DSH 里「一键安装 → 扫码接 QQ → 启动」，并把控制面板收进 DSH 设置页（不再需要开 PawBot 自己的网页）。

三块组成：

| 组成 | 位置 | 干什么 |
|---|---|---|
| **Host 插件** | `index.js` + `lib/*.js` | 把 PawBot 当受管子进程（起停/日志/健康检查）；一键安装（建 venv、装依赖）；NapCat 编排；注册 `/pawbot-ui/*` 同源代理路由；暴露 8 个工具 |
| **Client 面板** | `client.js` | DSH 设置页里 4 个分页：**启动 / 状态 / 设定 / 人设**。286 个 config key 的中文表单、人设编辑器、一键启动四步引导 |
| **B 站子插件** | `bili-watch/` | 「真的去看 B 站」：抓字幕+热评 → 一次 LLM → 结构化报告；判重三层、候选池、笔记 md。同时注册 `bili_watch` / `bili_watch_status` |
| **PawBot 本体** | `app/` | Python 项目自带在仓库里（`app/pawbot/` 核心 + `app/plugins/` 12 个内置插件 + `app/tools/` 66 个测试 + `app/web/` 自带 WebUI 页） |

---

## 2. 工程目录（带注释）

```
D:\WorkBuddy\pawbot-dsh\
├─ package.json              # name=@local/pawbot；files 含 app/lib/bili-watch/DEPLOY.md；dsh.bundle.patch + dsh.client
├─ cordis.patch.yml          # ⚠ 服务器上被本地改过（见 §9.3）；仓库版本只有两行 insert：pawbot + pawbot-bili-watch
├─ index.js                  # ★ Host 主文件（~1300 行）：子进程管理、一键安装编排、NapCat 编排、8 个工具、路由注册
├─ client.js                 # ★ Client 面板（~3200 行）：4 分页 + 286 key 的中文 METADATA（label/help/impact 徽章）
├─ lib/
│  ├─ hostdeps.js            # 从宿主树解析 schemastery / defineTool（拿不到就降级）
│  ├─ config-utils.js        # maskSecrets / pickUpdatable / deepMerge / pickSection / diffKeys（MASK = '***已设定***'）
│  ├─ webui-proxy.js         # /pawbot-ui/* 全部动作 + CSRF 守卫（Origin / Sec-Fetch-Site / Referer / Content-Type）
│  ├─ setup.js               # detectPython / ensureVenv / ensureDeps / ensureConfig / setupState / boot 状态机（可注入 run()）
│  └─ napcat.js              # detectMode(docker|binary|none) / 生成 OneBot 反向 WS 配置 / 启动 / 读 WebUI+token / 抓二维码 / 判登录
├─ test/
│  ├─ offline.mjs            # 19 条：config-utils 纯函数 + 代理动作 + CSRF/413/415/502
│  ├─ setup-offline.mjs      # 57 条：装环境、NapCat 编排、boot 四步状态机、路由接线（全用假 run()/假 fetch）
│  └─ client-meta-check.mjs  # 18 条：拿真 config（6185 或 app/data/config.json）校验 286 key 的中文字典
├─ bili-watch/               # ★ B 站子插件（22 个文件）
│  ├─ index.js               # 抓取编排、请求队列、候选池、自动备货、2 个工具
│  ├─ lib/{bili,http,cookie,credential,wbi,subtitle,report,notes,seen,pool,queue,inflight,request-lifecycle,model-guard,atomic,hostdeps}.js
│  ├─ selfcheck.mjs          # 52 条离线测试（假 fetch / 假时钟）
│  ├─ locale/{zh,en}.json · icon.svg · README.md
├─ app/                      # ★ PawBot Python 本体（随仓库自带）
│  ├─ run.py                 # 入口（零依赖，除 pillow + websockets）
│  ├─ requirements.txt       # pillow>=10 / websockets>=12
│  ├─ requirements-bili.txt  # 【可选】mcp<2 + bilibili-api-python（只有要读字幕才需要）
│  ├─ pawbot/                # 核心包（85 个模块）
│  │  ├─ agent.py            # 主循环 + 记忆分流的注入点
│  │  ├─ core.py             # OneBot 事件管线、全量入库、bili_scroll 定时器
│  │  ├─ ctxwin.py           # ★ 聊天记录渲染：每行 [HH:MM] 时间戳 + 尾号认人 + L0_HEADER / L0_HEADER_PRIVATE
│  │  ├─ style.py            # ★ 表达层后处理：去 AI 味、身体反应配额、唔开头抽稀、分条 ‖
│  │  ├─ store.py            # 会话窗口 + annotate_last（图片描述回填，按 uid 归属）
│  │  ├─ perceive.py         # 感知层：图/引用/转发/表情包（sticker 模式）/视频
│  │  ├─ vision.py           # 看图（含 STICKER_PROMPT：表情包只判语义）
│  │  ├─ tools.py            # 26+ 个工具定义（含 bili_video / bili_scroll / bili_notes / bili_action）
│  │  ├─ bili_watch.py       # ★ 读 B 站插件写的报告（PawBot 侧契约）
│  │  ├─ bili_scroll.py      # ★ 自己刷 B 站（读字幕→写 md 笔记→定时）
│  │  ├─ bili_mcp/           # ★ 内置 bilibili-mcp（MIT）；bili_credential.json 已被 gitignore
│  │  ├─ mem/                # 记忆 v2：assemble / layers / entities / decay（渐忘）/ write / bridge（跨会话桥）
│  │  ├─ persona.py          # 人设 + owner_line（主人=老爹）
│  │  ├─ emotion.py / proactive.py / life.py / reminders.py / qzone.py / selfedit.py …
│  ├─ plugins/               # 12 个内置插件（ai_chat / group_events / life_events / relations / qzone …）
│  ├─ tools/                 # 66 个 test_*.py + 辅助脚本
│  ├─ web/index.html         # PawBot 自带 WebUI（DSH 面板不依赖它）
│  └─ data/                  # ⚠ 只放模板：config.json（密钥全空）+ persona.md + 空记忆档 + .gitkeep
├─ scripts/
│  ├─ collect-server-changes.sh / .ps1   # 服务器上打包改动（无 VPN 时用）
│  └─ apply-update.sh                    # 服务器上用离线包更新（先备份、不碰 app/data）
├─ README.md                 # 面向使用者：装什么、面板怎么用、B站这半、诚说明
├─ DEPLOY.md                 # 面向部署者：Linux 清单、必改配置、NapCat、SESSDATA、已知降级
└─ docs/HANDOFF.md           # ← 本文
```

---

## 3. 运行时架构与数据流

```
DSH Web GUI（同源）
   │  浏览器打 /pawbot-ui/*
   ▼
Host 插件 index.js  ──(CSRF 守卫)──►  lib/webui-proxy.js
   │                                      │ 内部 HTTP（127.0.0.1:6185）
   │                                      ▼
   │                              PawBot WebUI（app/pawbot/webui.py，PawBot 自带）
   │                                      │
   ├─ spawn/kill 子进程 ──────────────────► py run.py（cwd=appDir）
   │                                        │
   │                                        ├─ OneBot 反向 WS 监听 0.0.0.0:6199/ws ◄── NapCat（QQ 协议端）
   │                                        └─ 读写 app/data/*（config/persona/记忆/群友档）
   │
   └─ bili-watch 插件 ◄── 共享目录 D:/WorkBuddy/pawbot-bili-watch-data
                                   ▲            ▲
                                   │            │
                          （插件写）reports/  （PawBot 写）requests/ + shared.json
                                   └──► PawBot 的 bili_watch.py 读报告 → 群聊里说出来
```

**两条独立的数据通道，别混**：

1. **配置/控制**：面板 → `/pawbot-ui/*` → PawBot WebUI `/api/*`（改 `app/data/config.json`）
2. **看片结果**：bili-watch 插件 → 共享目录 `reports/*.json` → PawBot `bili_watch.py` 读 → 表达层发群

---

## 4. 「一键启动」到底做了什么

`POST /pawbot-ui/boot` 触发四步（非阻塞，`GET /pawbot-ui/boot/status` 轮询，面板 1.5s 一次）：

| 步 | id | 做什么 | 失败会怎样 |
|---|---|---|---|
| ① 环境 | `env` | 找 Python（Win: `py`/`python`；Linux: `python3`）→ 建 `app/.venv` → pip 装 `app/requirements.txt` → 准备 `app/data/config.json` | 该步 fail，后续 skip；面板显示日志最后 10 行 + 「重试这一步」 |
| ② NapCat | `napcat` | 探测 docker / 本机 NapCat → 生成 OneBot 反向 WS 配置（指向 `ws://127.0.0.1:6199/ws`）→ 启动 → 读 WebUI 地址+token、抓二维码 | 没有 docker/NapCat 时标 `skip` 并列出**手动安装步骤**（不阻塞 ③④） |
| ③ PawBot | `pawbot` | 用 venv 的 python spawn `run.py` | fail 并给 stderr 尾部 |
| ④ 就绪 | `ready` | 轮询 `/api/status` 直到可达 | 超时 fail |

幂等：重复点不会重装 venv、不会起第二个 NapCat 容器、不会 spawn 第二个 PawBot。

---

## 5. 硬规则（违反会出事）

1. ⛔ **模型只准 `deepseek-flash`**。`bili-watch/lib/model-guard.js` 是**白名单**（不是黑名单）：
   任何不等于 `deepseek-flash` 的值（含 `deepseek-v4-pro` / `v4-pro` / `gemini` / `claude`）一律换成 flash 并留日志。
   `allowedModel` 自己也不能填 pro。**派子代理也不要用 v4-pro**（用 flash；pro 又贵又差）。
2. ⛔ **`app/data/` 永不进公开仓库**：里面的 `config.json` 有真实 API key，
   `people/*.md` / `diary/*.md` / `memory/entities.jsonl` 是群友个人数据，`bili_mcp/bili_credential.json` 是 **SESSDATA**。
   `.gitignore` 已覆盖，新增敏感文件时**必须补规则并跑 `git check-ignore` 验证**。
3. ⛔ **不在服务器上改插件源码**。服务器装的是 pnpm 拷贝（无 `.git`），改那里等于改依赖缓存，
   重装/更新即丢。代码在本机改 → 推 GitHub → 服务器用离线包更新（`scripts/apply-update.sh`）。
4. ⛔ **服务器的个性化设置不要写进源码**（provider、dataRoot、调参）。写在 bundle 的 `cordis.patch.yml`
   或 `bili-watch/index.js` 默认值里，**每次更新都会被覆盖**。正确位置是 DSH 设置页（存 profile 层）。
5. ⛔ **面板显示文字一律简体中文**；代码注释可以留繁中。
6. ⛔ **`MASK` 常量必须两边一致**：`lib/config-utils.js` 与 `client.js` 都是 `'***已设定***'`，
   改一边不改另一边 → 遮罩失效、可能把 `***已设定***` 写回 config 覆盖真 key。
7. ⛔ **`/pawbot-ui/*` 每条新路由都要过 CSRF 守卫**（跨源 403、非 GET 必须 `application/json`）。
   不要绕过 `webui-proxy.js` 的 `guardRequest`。
8. ⛔ **发给浏览器的 config 必须遮罩 secrets**（`api_key` / `token` / `access_token` / `cloud_key` / `*secret*`），
   且写回时丢掉「值等于遮罩串」的键。已实现并有测试，别退回去。

---

## 6. 关键契约

### 6.1 `/pawbot-ui/*` 路由（同源，共 18 条注册 = 2 prefix + 16 exact）

```
GET  /pawbot-ui/status             进程信息 + PawBot /api/status
GET  /pawbot-ui/config             整份 config（secrets 遮罩）→ {ok, config, masked_keys}
POST /pawbot-ui/config             body {patch:{...}} 或直接 partial → 深合并写回
GET  /pawbot-ui/persona            人设（同时回 text 与 content）
POST /pawbot-ui/persona            {text}/{content}/纯字符串，上限 200KB
POST /pawbot-ui/reload-plugins     热重载（别名 /plugins/reload）
POST /pawbot-ui/send               {target, message, is_group} 真发消息；PawBot 回 ok:false 时这里必须 502
POST /pawbot-ui/restart            用本插件 stopChild+startChild，不绕 PawBot
GET  /pawbot-ui/context | /memory  上下文 / 记忆总览
GET  /pawbot-ui/setup/status       {appDir, python, venv, deps, config, phase, progress, log[60]}
POST /pawbot-ui/setup/run          {force?} 非阻塞，进度轮询 setup/status
GET  /pawbot-ui/napcat/status      {mode, running, webui:{url,token}, qr, loggedIn, onebot, hint, manual}
POST /pawbot-ui/napcat/setup       探测+写配置+启动（mode=none 也回 ok:true + started:false + manual）
POST /pawbot-ui/napcat/qr          重抓二维码
POST /pawbot-ui/boot               一键四步（非阻塞）
GET  /pawbot-ui/boot/status        {phase, steps[4], done}
```

### 6.2 工具（10 个）

`pawbot_status` · `pawbot_logs` · `pawbot_restart` · `pawbot_health` · `pawbot_config_get`（`reveal:true` 才回真值）· `pawbot_config_set` · `pawbot_setup` · `pawbot_napcat` · `bili_watch` · `bili_watch_status`

### 6.3 共享目录契约（`dataRoot`）

```
<ROOT>/requests/<uuid>.json   PawBot 写 → 插件消费（成功移 done/，失败重试 1 次后进 failed/，坏档进 bad/）
<ROOT>/reports/<bvid>.json     插件写（原子 tmp→rename）→ PawBot 读
<ROOT>/reports/index.json     插件维护（最多 200 条）
<ROOT>/notes/<bvid>.md         人读笔记（可选）
<ROOT>/seen.json               判重表（watched / skipped）
<ROOT>/shared.json             PawBot 写，插件只读（别重复推荐）
<ROOT>/state.json              每日计数
<ROOT>/credential.json         可选 SESSDATA（插件只读，不回传值）
```

**report schema v1**：`{version:1, bvid, url, title, author, duration_sec, play, pubdate, tname,
watched_level∈{subtitle,desc_comments,title_only}, summary, highlights[], comment_vibe,
top_comments[{text,like}], take, share_worthy, share_reason, created_at, source_request}`

**request**：`{id, kind∈{bvid,keyword,auto,up}, value, requested_at, session, extra?{order,days,num}}`
（`extra` 只在非空时写；`kind=up` 不接受 extra）

### 6.4 两条「路径必须一致」的坑

- 插件 `dataRoot` ↔ PawBot `data/config.json` 的 `tools.bili_watch_dir` **必须指向同一个目录**，
  否则：插件一直兜底「只看标题」，PawBot 说「没看过任何视频」。
- 插件 `webuiPort` ↔ PawBot `webui.port` 必须一致（默认 6185）；`onebotToken` 若填了，
  要同时写进 PawBot `server.access_token` 和 NapCat 的 `token`。

---

## 7. 现状进度

| 块 | 状态 | 备注 |
|---|---|---|
| PawBot 包成 DSH Host 插件（起停/日志/健康） | ✅ 可用 | Windows 用 `taskkill /T /F`（优雅停止在 Windows 不可得）；Linux 走 SIGTERM |
| `/pawbot-ui` 同源代理 + CSRF + secrets 遮罩 | ✅ 可用并实测 | 跨源 403 / 非 JSON 415 / send ok:false→502 |
| DSH 设置页面板（4 分页、286 key 中文表单） | ✅ 代码完成 | **真浏览器渲染未验证过**；面板改动需刷新页面或重载 bundle |
| 一键安装（venv + 依赖 + 配置） | ✅ 逻辑完成 + 离线单测 | **真跑一次 venv+pip 未验证** |
| NapCat 编排（docker / binary / 手动步骤） | ⚠ 代码完成，**完全未实机验证** | 本机无 docker、无 NapCat |
| B 站看片（抓字幕+热评→报告） | ✅ 可用 | 无 SESSDATA 时 `watched_level` 停在 `desc_comments`（如实降级） |
| 判重三层 + 候选池 + 失败重试/隔离 | ✅ 可用 | 8 线程并发写 shared.json 已修（0 异常、240 笔不丢） |
| 模型白名单（只准 flash） | ✅ 可用 | 实测 8 种 pro 写法全部被收敛 |
| 认人/时间戳/图片归属/去 AI 味/记忆来源标注 | ✅ 可用 | 66 个 Python 测试，65 通过 |
| 服务器侧「自己刷 B 站」(`bili_scroll` + `bili_mcp`) | ✅ 已并入仓库 | 可选依赖 `app/requirements-bili.txt` **未纳入一键安装** |
| 跨会话记忆桥 (`mem/bridge.py`) | ✅ 已并入 | 默认单向 群→私聊 |

**已知失败（不是这次引入的）**：`app/tools/test_pixiv_session.py::test_filters_and_sorting`
（图片排序断言 `['9','8','7','10'] != ['9','8','2','7']`）。`pixiv_session.py` 没人改过，与主线无关。

---

## 8. 测试与验证

```powershell
# JS（在 D:\WorkBuddy\pawbot-dsh）
node test/offline.mjs             # 19 passed — 代理动作 + CSRF/413/415/502
node test/setup-offline.mjs       # 57 passed — 装环境/NapCat/boot 状态机/路由接线（全假依赖）
node test/client-meta-check.mjs   # 18 passed — 286 key 中文字典 vs 真 config（6185 或 app/data/config.json）
node bili-watch/selfcheck.mjs     # 52 passed — 抓取/判重/守门/超时/去敏

# Python（在 D:\WorkBuddy\pawbot-dsh\app，先 $env:PYTHONPATH="."）
py -m compileall -q pawbot plugins run.py
py tools/test_mem.py …            # 66 个测试文件；一次全跑约 3~5 分钟（基线 65 过 / 1 个既有失败）
Get-ChildItem tools/test_*.py | ForEach-Object { cmd /c "py $($_.FullName) 2>&1" }
```

> 坑：PowerShell 里 `py test.py 2>&1 | Select-String "OK"` 会因 stderr 噪音误判。
> 用 `cmd /c "py x.py 2>&1"` 再把输出当字符串搜 `FAILED (` / `^OK` 才准。

---

## 9. 部署与更新

### 9.1 新机器（Linux 云服务器）

```bash
dsh plugin --profile <profile> add github:awa486-nekorin/pawbot-dsh
# 重启 dsh → 设置 → PawBot →「启动」页 → 点「一键启动」
```
必改：`projectDir` 指到 bundle 里的 `app/`（如 `/opt/pawbot-dsh/app`）、`pythonExe` 用 venv 的或 `python3`。
NapCat：有 docker 自动起；没有就按面板列的手动步骤装。
详细清单见 `DEPLOY.md`。

### 9.2 服务器（Windows，**没有 VPN**）— 双向离线通道

```powershell
# 服务器上的改动 → 带回本机（本机有网/直连 GitHub）
#   在服务器跑 scripts/collect-server-changes.ps1（Windows）
#   或用 bash 版 scripts/collect-server-changes.sh（Linux）
#   → 产出 %TEMP%\pawbot-changes-<时间>.zip → SFTP/桌面拷回本机 → 交给 Lead 比对提交

# 本机 → 服务器（更新）
git archive --format=tar.gz -o pawbot-dsh-update.tgz --prefix=pawbot-dsh/ HEAD -- . ':(exclude)app/data'
#   传到服务器后：bash scripts/apply-update.sh pawbot-dsh-update.tgz
#   脚本会：整份备份 → 只覆盖代码 → 绝不碰 app/data → 打印回滚命令
```

### 9.3 服务器现状（截至 2026-10-26）

- Windows（`C:\Users\Administrator`，RDP 第二个会话 → TEMP 在 `Temp\2`）
- **没有 VPN**，连不上 GitHub
- 插件目录是 pnpm 拷贝，**没有 `.git`**
- 服务器上曾有两处**写进 bundle 源码**的机器专属覆盖（已确认会在更新时被覆盖，应改到设置页）：
  - `bili-watch/index.js`：`provider: deepseek-account`、`dataRoot: C:/Users/Administrator/.dsh/pawbot-bili-watch-data`
  - bundle 的 `cordis.patch.yml`：同上的显式 config
- 服务器还有自己的运行时数据与 SESSDATA，**从没进过 repo**

### 9.4 SESSDATA（字幕唯一开关）

三个来源，优先级 `env > credential.json > 设置页`：

1. 环境变量 `BILI_SESSDATA`
2. `<dataRoot>/credential.json` = `{"sessdata":"…","updated_at":"…"}`
3. 插件设置页 `sessdata`（secret 字段）

没配 → 报告 `watched_level=desc_comments`（只看简介+评论），**这是如实降级，不是 bug**。

---

## 10. 待办（按优先级，含验收标准）

### P1

1. **在服务器上真跑一次「一键启动」**（①③④；② 无 docker 会 skip）
   - 验收：`pawbot_status.ui_route === 'registered (18 routes)'`、面板四步全绿、群里能对话
   - 当前最大未知：venv/pip 真安装路径从未跑过
2. **NapCat 全链路实机验证**（docker run/pull、`host.docker.internal` 反向连、WebUI token 正则、扫码登录判定）
   - 验收：面板出现「打开 NapCat 登录页」，扫码后 `loggedIn:true`
3. **`bili_scroll` 的可选依赖纳入一键安装**（`app/requirements-bili.txt`）
   - 建议加 Config 开关 `installBiliDeps`（默认 false），勾了才装
4. **把服务器的机器专属配置从源码搬到设置页**（provider / dataRoot），否则每次更新都要重配

### P2

5. `proactive.py` 的「只看标题」分支仍不写 `shared.json`（工具路径已写）→ 可能重复推荐同一条
6. SESSDATA 没有 UI 入口：`bili_watch.save_sessdata()` 只能从脚本调；面板缺「贴凭据」按钮
7. 面板 a11y（label `htmlFor`、对话框 focus trap）、286 栏每次按键全量重绘、TTL 三处写死 72h、locale 文件未接上（文字硬编码）
8. `app/data/persona.md` 含成人向内容且在**公开仓库**里 → 是否换成中性示例，需用户拍板
9. `setup.js` 的 timer 只在 apply 时算一次：改 `pollSec`/`autoIntervalMin` 要重启 PawBot 才生效，面板没有任何提示

---

## 11. 踩过的坑（症状 → 原因 → 对策）

1. **`&&` 在 PowerShell 5.1 报「不是有效的语句分隔符」** → 我给的是 bash 命令；PS 5.1 不支持 `&&`。→ 分平台给命令。
2. **`.ps1` 脚本一跑就 ParserError、中文乱码** → `write` 写出的是无 BOM UTF-8，PS 5.1 按 GBK 读。→ 脚本里**只用英文**，或存成带 BOM。
3. **`.gitignore` 追加成功但规则没生效** → 那条 `Add-Content` 没 `cd` 到仓库，写进了 `C:\Users\Administrator\.gitignore`。→ 永远用绝对路径，改完 `git check-ignore -v` 验证。
4. **凭据差点被提交** → `bili_credential.json` 含 SESSDATA，`.gitignore` 规则其实没生效。→ 提交前必跑：`git add -A` 后 `git diff --cached --name-only | Select-String "credential|app/data/"`。
5. **整个插件启动失败：`JsonSchemaError: parameters.patch.additionalProperties must be explicitly true or false`** → object 型工具参数必须显式写 `additionalProperties`。→ 动 schema 后先 toggle 重载一次。
6. **`prefix: '/pawbot-ui/'` 路由注册成功但请求 404（空 body）** → 本版 webServer 的 prefix 匹配与预期不同。→ 同时注册 prefix + 逐条 exact，并把子路径的前导斜杠收敛掉。
7. **面板转简后 `compat` 规则变「0 命中却依然绿」** → 测试正则还是繁体，转简后匹配不到。→ 测试里加「命中 <1 就 fail」。
8. **改完源码推送失败 `unable to access github.com ... via 127.0.0.1`** → 本机代理关了、GitHub 反而能直连。→ `git config --unset http.proxy` 后重推。链路会变，别默认代理常开。
9. **`MASK` 简繁不一致** → 会让遮罩失效/把 `***已设定***` 写真进 config。→ 两边一起改 + 跑 `offline.mjs`。
10. **Python 测试结果被 PowerShell 噪声误判** → stderr 的 `NativeCommandError` 混进输出。→ 用 `cmd /c` 包一层再解析。
11. **服务器改动看起来很小，其实 8 个 py + 6 个新文件** → 只比了「差异文件数」。→ 用 `git diff --no-index --numstat` 看行数，并逐文件读 diff。
12. **仓库里 `app/` 本地膨胀到 69MB** → 跑 Python 测试会在 `app/data/` 生成运行时数据（已被 gitignore，但别 `git add -f`）。→ 提交前看 `git count`/`git diff --cached --stat`。
13. **v4-pro 烧 token** → 早期用 v4-pro 跑子代理，又贵又差（用户原话：「V4.1 Flash 比 V4 Pro 强很多」）。→ 只准 flash，且已做成白名单硬拦。

---

## 12. 常用命令速查

```powershell
# 仓库 & 远端
cd D:\WorkBuddy\pawbot-dsh
git log --oneline -5
git remote -v                       # origin = github.com/awa486-nekorin/pawbot-dsh
git push origin main

# 本机 DSH 的插件安装形态（desktop profile）
Get-ChildItem "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\@local"
Select-String -Path "$env:USERPROFILE\.dsh\profiles\desktop\package.json" -Pattern "pawbot"

# 重载插件（改了 Host 代码后用；会重启 PawBot）
plugin_manager set_plugin target=include:pawbot enabled=false
plugin_manager set_plugin target=include:pawbot enabled=true

# 只改 bili-watch 时
plugin_manager set_plugin target=include:pawbot-bili-watch enabled=false / true

# 运行时探针
Invoke-RestMethod http://127.0.0.1:6185/api/status      # PawBot 直连
Invoke-RestMethod "http://127.0.0.1:<DSH端口>/pawbot-ui/status"   # 面板走的路

# 探 DSH 端口
Get-NetTCPConnection -State Listen | Where-Object OwningProcess -eq (Get-Process 'DeepSeek Harness').Id
```

---

## 13. 外部参考

| 东西 | 位置 / 链接 | 用途 |
|---|---|---|
| 参考插件 `@jojoman2024/dsh-bili-scroll` | npm / `github.com/jojoman2024/dsh-bili-scroll` | 我们 B 站功能的思路来源（判重三层、笔记模板、防脑补约束）；**发评论/回楼中楼/私信一律不接** |
| fork 版 `dsh-bili-subagent` | `D:\WorkBuddy\_refs2\dsh-bili-subagent` | 早先读过的 DSH 插件写法参考（子代理 + MCP） |
| 真 PawBot 原项目 | `D:\WorkBuddy\2026-10-20-00-14-25\pawbot` | `app/` 的来源；里面有更老的数据与备份，**只读** |
| bilibili-mcp | `app/pawbot/bili_mcp/`（MIT, adoresever/bilibili-mcp） | 字幕/评论能力来源，随仓库自带 |
| DSH 插件规范 | `…\dsh\node_modules\@deepseek-ai\dsh-agent-preset\skills\cordis-plugin-development\references\host-plugin.md` | Host/Client 插件写法、locale/icon 要求 |
| 共享数据目录 | `D:\WorkBuddy\pawbot-bili-watch-data` | B站报告/请求/判重表 |

---

## 14. 术语

| 词 | 意思 |
|---|---|
| **大肥鱼** | 这个 QQ 机器人的名字（鲸鱼娘人设） |
| **主人 / 老爹** | 用户本人。对她像老爹：管她、养她、给生活费；**生活费 = token = 大白饭**（同一件事） |
| **watched_level** | 她「真的看到了多少」：`subtitle`（读了字幕）> `desc_comments`（只看简介+评论）> `title_only`（只有标题）。**后两档不能假装看过** |
| **渐忘** | 记忆按龄降分辨率（清晰→略糊→骨架→影影绰绰），但**任何年龄都不为空** |
| **判重三层** | 看过（reports/seen）/ 挑中 / 已分享（shared）——不同闸语义不同，别混 |
| **一枚 ‖** | 表达层的分条记号：一条回复拆成多条 QQ 消息，不会发出去 |
| **一键四步** | env → napcat → pawbot → ready（见 §4） |

---

## 15. 新窗口第一步该做什么（checklist）

1. 读完本文；再读 `README.md`（使用者视角）与 `DEPLOY.md`（部署视角）。
2. 跑 §8 的四条 JS 测试，确认基线是 19/57/18/52 全绿。
3. `pawbot_status` 看当前进程状态与 `ui_route`（应为 `registered (18 routes)`）。
4. 问用户这次要做什么。若没有明确目标，按 §10 的 P1 顺序推进，**先做「在服务器上真跑一次一键启动」**——那是当前最大的未验证面。
5. 任何要改 `app/data/`、要改源码默认值、要新增 secrets 的念头，先回看 §5。
