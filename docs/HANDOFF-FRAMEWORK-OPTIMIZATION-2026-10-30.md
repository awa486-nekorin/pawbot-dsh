# PawBot × DSH 框架优化 — 总交接文档

> 创建：2026-10-30
> 适用对象：接手本优化任务的下一名 Agent（人类协作者也可读）
> 主工程：`D:\paw-old\pawbot-dsh`（**唯一**修改目标）
> 关联文档：`docs/decisions-framework-optimization.md`（决策记录）、`docs/ARCHITECTURE-BASELINE.md`（批次 1 契约）、`docs/HANDOFF.md`（2026-10-29 历史交接，讲的是项目本身而非本次优化）

---

## 0. 三十秒上手

1. 这是一个 **DSH Host bundle（Node ESM）+ 内附 PawBot（Python 3.13）** 的单仓库项目，部署目标是 **阿里云 Windows Server**。
2. 本次任务不是重写，而是在**保持兼容**的前提下做 7 个批次的渐进式架构优化，已选定**方案 A：渐进式模块化单体 + 授权租约层**。
3. **批次 1 已完成并实测通过**（基线检查 19/19，离线测试 43/43），但**尚未 git commit**。
4. **批次 2 已完成本机验收与独立复核**：JS 277 项、Python 1383 项运行成功（1 skip）、基线 19/19；完整review APPROVE、最终稳定迁移专项 BLOCKING=0。尚未提交或生产部署。最终证据与明确限制见 `docs/FRAMEWORK-BATCH2-VERIFICATION.md`，最新交接见 `docs/HANDOFF-BATCH2-LIVE-2026-10-30.md`。批次 3–7 未开始。
5. 三条硬红线：**不改 `app/pawbot/` 业务语义、不破坏旧路由/配置键/数据格式、安全词绝不落盘**。

---

## 1. 工程结构与运行方式

### 1.1 仓库组成

```text
D:\paw-old\pawbot-dsh\
├── package.json              # @local/pawbot，type=module，engines node>=22，dsh>=0.1.7-rc.1
├── cordis.patch.yml          # 向 DSH 插入两列：pawbot、pawbot-bili-watch
├── index.js                  # ★ DSH Host 入口（~1600 行）：Config、进程管理、11 个工具、UI 路由、启动编排
├── client.js                 # DSH 设置面板（PawBot 标签页：启动/状态/API 密钥/设置/人设）
├── lib/
│   ├── hostdeps.js           # 从宿主树取 schemastery / defineTool，取不到则降级
│   ├── config-utils.js       # 纯函数：secret 遮罩、深合并、区段过滤、diff
│   ├── webui-proxy.js        # /pawbot-ui/* 同源代理（含 CSRF 守卫）
│   ├── dsh-brain.js          # ★ 大肥鱼大脑：进程内 Agent + 人设 + 电脑工具 preset + 回调 PawBot 工具
│   ├── dsh-tool-bridge.js    # ★ 只读工具桥 /pawbot-dsh-bridge/execute（白名单：bili_watch_status）
│   ├── data-guard.js         # 版本快照 + 外部 dataDir 迁移（Windows junction / POSIX symlink）
│   └── setup.js              # 一键安装/启动引导
├── bili-watch/               # 原 @local/pawbot-bili-watch，作为 subpath 并入
├── app/                      # ★ PawBot 本体（Python），随插件分发
│   ├── run.py                # 启动器 / 信号处理 / asyncio 主循环
│   ├── requirements.txt      # pillow、websockets（未锁版本）
│   ├── pawbot/
│   │   ├── core.py           # ★ Bot 上帝对象：装配 Config/Store/LLM/Safety/Persona/Memory/...
│   │   ├── config.py         # ★ DEFAULT 38 个区段（运行态配置唯一来源）+ Config 深合并/原子写
│   │   ├── webui.py          # ★ 手写 HTTP：15 GET + 13 POST + SSE，token 校验
│   │   ├── agent.py          # Agent.run，大脑会话 key = session_id#uuid
│   │   ├── brain.py          # 大脑桥客户端：URL/token 从 env 读取，_ACTIVE/_ALLOWED
│   │   ├── dsh_bridge.py     # 只读工具桥客户端
│   │   ├── cloudsync.py      # tar.gz + AES-256-GCM + PBKDF2，local/webdav/http 适配器
│   │   ├── store.py / memory*.py / sender.py / llm.py / safety.py / ...
│   │   └── bili_scroll.py, bili_mcp/
│   ├── plugins/              # basic / ai_chat / life_events / pixiv ...
│   ├── tools/                # test_*.py + chat_window.py / cloud_sync.py / mock_qq ...
│   └── data/                 # 运行时数据（仓库里只有模板；真实数据不入库）
├── scripts/
│   ├── architecture-baseline.mjs   # ★ 批次 1 新增：只读架构基线检查器
│   └── pack-server.ps1/.sh, apply-update.sh, local-update.ps1, ...
├── test/
│   ├── architecture-baseline.mjs   # ★ 批次 1 新增：基线器 + 代理 CSRF 契约测试
│   ├── offline.mjs / setup-offline.mjs / dsh-brain-offline.mjs
│   ├── dsh-tool-bridge-*.mjs / data-guard-offline.mjs / client-meta-check.mjs
├── docs/                     # BRAIN.md、DATA-PERSISTENCE.md、CLOUD-SYNC.md、DECIDER.md、UI-PLAN-v2.md ...
└── locale/ icon.svg
```

### 1.2 运行时拓扑

```text
QQ ←→ NapCat ←(OneBot v11 反向 WS, 127.0.0.1:6199/ws)→ PawBot(Python, run.py)
                                                        │
                                                        ├─ WebUI 127.0.0.1:6185（手写 HTTP；仅供本机/代理）
                                                        │
DSH Host(Node, index.js) ──spawn(pythonExe, run.py, cwd=app/)──┘
   │  ├─ /pawbot-ui/*          同源代理 → 6185（浏览器面板用）
   │  ├─ /pawbot-dsh-brain/turn   ← Python 调 Host：一轮对话交给进程内 Agent（llm.provider=dsh）
   │  └─ /pawbot-dsh-bridge/execute ← Python 调 Host：只读工具（bili_watch_status）
   └─ DSH Web GUI（端口由 ctx.webServer 决定）
```

关键事实：

- 两个桥都是 **loopback + 一次性 Bearer token + 限额**，token 通过子进程 env 注入（`PAWBOT_DSH_BRAIN_TOKEN` / `PAWBOT_DSH_BRIDGE_TOKEN`）。
- 大脑每轮 **新建一个 Agent**，人设为 PawBot 整段 system，工具 = 电脑工具 preset + PawBot 业务工具代理；当前 `restrict` 掉 subagent/workflow/goal 等全局工具。
- 面板代理有 CSRF 守卫（Origin/Sec-Fetch-Site/Content-Type，拿不到 origin 时 fail-closed）。
- 数据默认在 `app/data`；宿主 Config 的 `dataDir` 或环境变量 `PAWBOT_DATA_DIR` 可把它换成外部目录 + junction。

### 1.3 常用命令

```bash
cd /d/paw-old/pawbot-dsh

# 语法检查
node --check index.js
node --check client.js
node --check lib/dsh-brain.js

# 离线 JS 测试（均不联网、不需要 DSH）
node test/offline.mjs
node test/setup-offline.mjs
node test/dsh-brain-offline.mjs
node test/dsh-tool-bridge-offline.mjs
node test/dsh-tool-bridge-host.mjs
node test/data-guard-offline.mjs
node test/client-meta-check.mjs
node test/architecture-baseline.mjs

# 架构基线
node scripts/architecture-baseline.mjs            # 人类可读
node scripts/architecture-baseline.mjs --json     # 机器可读
node scripts/architecture-baseline.mjs --out reports/baseline.json   # 只允许新建，拒绝 data/

# Python 测试（在 app/ 下；先设 PYTHONPATH=.）
cd app && PYTHONPATH=. python -m unittest discover -s tools -p 'test_*.py' -q
```

> Windows 上把 `PYTHONPATH=.` 换成 `$env:PYTHONPATH="."`；测试数量随版本变化，不要写死数字。

---

## 2. 当前状态（务必先读）

### 2.1 已完成：批次 1 — 基线与契约冻结

新增文件（均已实测，**尚未提交**）：

| 文件 | 作用 |
| --- | --- |
| `scripts/architecture-baseline.mjs` | 只读架构基线检查器，19 项契约 |
| `test/architecture-baseline.mjs` | 43 个离线测试：成功路径、5 类漂移负例、`--out` 路径安全、代理 CSRF 运行时契约 |
| `docs/ARCHITECTURE-BASELINE.md` | 基线契约说明与使用方式 |
| `docs/ARCHITECTURE-BASELINE-VERIFICATION.txt` | 本机实测输出留档 |
| `docs/decisions-framework-optimization.md` | 决策记录（架构选择、批次顺序、批次 1 结论） |

实测结果（2026-10-30，本机 Node v24.14.0）：

```text
node scripts/architecture-baseline.mjs  → PASS — 19 passed, 0 failed
node test/architecture-baseline.mjs     → 43 passed, 0 failed, 0 skipped
node --check scripts/... && node --check test/... → OK
```

基线覆盖的契约（改动这些会直接失败，属于**故意**的回归护栏）：

- 关键文件齐全；`package.json` 身份/engines/exports/dsh 字段。
- Node 入口 `index.js`、`client.js`、`bili-watch/index.js`、Python `app/run.py` 及入口相对依赖可解析。
- `/pawbot-ui` 注册方式与 exact 子路径、GET/POST 方法集合。
- 只读桥常量、白名单、限额；Python 侧只读桥客户端协议。
- 大脑桥路径/客户端/Host 注册；`BRAIN_TURN_PATH` 等常量。
- `app/pawbot/config.py` 的 DEFAULT 段数、默认值、单一赋值来源、与 `core.py`/`run.py` 的接线。
- WebUI 关键 GET（15）/POST（13）端点与静态安全边界。

**下一 Agent 的第一件事**：先跑上面三条命令确认仍全绿，再开始批次 2。若基线红了，先判断是“预期契约变更”（需同步更新基线并说明理由）还是“误伤”。

### 2.2 未完成

- 批次 2 已完成本机实现、全量回归与独立复核，未提交/未生产部署；批次 3–7 未开始。批次 2 历史失败探针与关闭记录见 `docs/BATCH2-REVIEW-2026-10-30.md`，接受边界见 `docs/FRAMEWORK-BATCH2-VERIFICATION.md`。
- 批次 1 的文件未 commit（工作区有 5 个未跟踪项，含 `docs/por-20261027/` 历史目录）。
- 决策记录里“独立复核”一栏可以按需关闭：基线器与测试已在本机实跑通过，剩余风险是真实 DSH Host 行为未验证（见 §9）。

---

## 3. 现有问题清单（问题 → 证据 → 影响 → 归属批次）

> 证据中的行号基于 2026-10-30 工作区，改动后可能漂移；以符号名/行为为准。

| # | 问题 | 证据 | 影响 | 批次 |
| --- | --- | --- | --- | --- |
| P1 | **Windows 上只会 `taskkill /T /F` 强杀**，PawBot 收不到信号，无法在停机前 flush | `index.js:347-359` 注释、`index.js:397-420 stopChild` | 正在写的 store/记忆可能截断；升级/重启有数据损坏风险 | 2 |
| P2 | **数据路径分散**：`app/data`、`dataDir`、`PAWBOT_DATA_DIR`、`brainCwd`、`bili_watch_dir`、云同步目录各自解析 | `index.js:279,290,342,782,825`；`lib/data-guard.js:137-173` | 双端不一致（插件与 PawBot 各写各的）、升级迁移边界模糊 | 2 |
| P3 | **没有单实例锁**：同一数据目录可被两个 DSH profile / 两个 PawBot 同时打开 | 仓库内无 lock 实现 | 并发写 config/store，互相覆盖 | 2 |
| P4 | **配置三处来源**：Python `DEFAULT`(38 段)、Host Config schema、面板 `COMMON/ADVANCED` 元数据，靠测试人工对齐 | `app/pawbot/config.py`、`index.js:100-170`、`client.js` 顶部、`test/client-meta-check.mjs` | 新增键漏一端 → 面板不显示/写入被丢/默认值漂移 | 3 |
| P5 | **协议无版本层**：两个桥各自实现 body 解析、限额、错误码；无 request id / revision | `lib/dsh-brain.js`、`lib/dsh-tool-bridge.js` 全文 | 加字段只能靠“兼容猜测”；排障无法串联一次调用 | 4 |
| P6 | **授权是静态白名单 + 布尔 ownerOnly**，不是授权模型：无租约、无时长、无撤销、无审计、子代理无法继承 | `lib/dsh-brain.js:14,54,148-150,454`；`app/pawbot/brain.py` `_ALLOWED` | 无法满足“主人在 QQ 临时授权 1 小时、可撤销、子代理继承”的目标 | 5 |
| P7 | **大脑主动封死 subagent/workflow/goal**，与“完整 Agent 能力”目标冲突 | `lib/dsh-brain.js:14` 注释与 restrict 逻辑 | 大肥鱼不能开子代理/工作流，能力天花板被写死 | 6 |
| P8 | **每轮新建 Agent，session 日志只增不删**；延迟 3.7–41s | `README.md`「诚实说明」；`docs/BRAIN.md §7`；`index.js` 无清理逻辑 | 磁盘长期增长；长任务体验差；无法取消整棵 Agent 树 | 6 |
| P9 | **上帝对象/高耦合**：`core.py` 装配一切；`webui.py` 手写 28 个端点；`index.js` ~1600 行 | 三个文件体量 | 任何改动都牵一发动全身，测试只能靠整体 | 2/4（只做接缝，不重写） |
| P10 | **无结构化审计**：特权动作只进环形日志缓冲，重启即丢 | `index.js` ring buffer；`lib/dsh-tool-bridge.js` audit 回调仅内存 | 出事后无法追溯“谁在何时授权了什么” | 5/6 |
| P11 | **依赖未锁版本**：`requirements.txt` 只有包名；peer deps 可选 | `app/requirements.txt`、`package.json` | 服务器重装可能拉到不兼容版本 | 7 |
| P12 | **升级/回滚缺自动验收**：有快照，但没有“升级后数据仍可读”的自动检查 | `lib/data-guard.js`、`DEPLOY.md §8.6` | 回滚靠人肉；云服务器上风险放大 | 7 |

补充（不单列批次，随批次 5/6 处理）：

- WebUI 默认 token 为空，安全边界依赖“只绑 127.0.0.1”；一旦反代配置错误就裸奔。
- 云同步 passphrase 丢失不可恢复，当前没有密钥轮换流程。
- 大脑图片入参未打通（降级为纯文字）。

---

## 4. 硬约束与安全红线

### 4.1 不可违反

1. **安全词绝不落盘**。主人在聊天中给出的“授权安全词”和“撤销安全词”是机密，只能以**加盐哈希/校验材料**形式保存（建议 scrypt 或 PBKDF2-HMAC-SHA256，每份独立 salt）。禁止出现在：源码、注释、文档、测试、日志、错误信息、审计记录、模型上下文、提交信息。需要引用时只写“授权安全词”“撤销安全词”。
2. **高风险操作 fail-closed**。租约缺失、过期、revision 不一致、进程重启后状态未知 → 一律拒绝，不得“默认放行”或“伪装成功”。
3. **只有 `account.owner_ids` 中的人能改变授权状态**；非主人发出的授权/撤销指令必须被忽略并审计（审计里也不记录安全词原文）。
4. **兼容优先**：旧 `/pawbot-ui/*`、`/api/*`、OneBot 行为、配置键、`app/data` 格式、bili-watch 文件协议、启动命令都要继续可用。新协议只能**并行增加**，不能删除旧路径。
5. **不修改 `app/pawbot/` 的业务语义**来做“顺手重构”；架构接缝通过新增模块 + 适配器完成。
6. 不引入需要常驻外部服务的依赖（Redis/DB/消息队列）；单机 Windows 可运行是底线。

### 4.2 安全词的正确用法（给实现者）

- 首次配置：主人在私聊中设置安全词 → 立即生成 `{algo, salt, iterations, hash}` 写入 `data/authz/secret.json`（权限收紧），**不回显**。
- 校验：常量时间比较；失败计数 + 退避；失败原因对外统一为“校验失败”。
- 轮换：需旧安全词 + 新安全词；轮换后 revision+1 并广播。
- 测试：只用固定假值（如 `test-secret-1`）在临时目录跑，测试产物不得进仓库。

---

## 5. 已确定的决策（来自用户，勿再询问）

| 主题 | 结论 |
| --- | --- |
| 架构 | **方案 A**：渐进式模块化单体 + 授权租约层 |
| 兼容策略 | 渐进兼容，不推倒重写 |
| 部署环境 | 阿里云 **Windows Server**（中国大陆） |
| 优先级 | 稳定性 > 可维护性 > 性能 > 数据安全（但安全是硬门槛） |
| Agent 能力 | 要完整 DSH 权限：工具、computer-use、设备读写/命令/联网、多轮、**子代理、工作流** |
| 低风险操作 | 直接执行 |
| 高风险操作 | 主人有效授权期间内全部允许 |
| 授权发起 | 群聊、私聊都可以；只有主人能改变授权状态 |
| 授权时长 | 默认 1 小时；主人可在授权时指定 |
| 撤销 | 主人通过 QQ 撤销，使用撤销安全词 |
| 子代理 | 继承父 Agent 的 scope 与**剩余**有效期；不得扩大/延长；撤销同步传播 |
| 失效语义 | 带版本号的 capability lease；失效/版本不一致 → 高风险 fail-closed |
| 模式 | DSH 托管与独立服务并存；DSH 不可用时普通机器人降级运行，高风险能力明确拒绝 |
| 依赖 | 允许有限引入成熟依赖，不要复杂构建链 |

**不要再次向用户确认这些**；只有出现新的、会改变实现方向的岔路时才问。

---

## 6. 目标架构 A

```text
现有 DSH Host + Python Runtime
  + Protocol v2（新路由，旧路由 adapter 兼容）
  + Authorization Lease / Capability Guard（租约 + 风险分级 + 执行前统一闸门）
  + Agent Supervisor（父子生命周期、取消树、日志清理）
  + DataRoot（单一数据根 + 单实例锁）
  + Windows graceful shutdown（HTTP 控制面 + 强杀兜底）
```

分层原则：

- **控制面**（谁能做什么）与**数据面**（具体业务）分离；所有特权动作先过 Capability Guard。
- **单一事实来源**：数据根、配置 schema、协议常量各只有一处定义，其余生成或引用。
- **可降级**：DSH 不在时，Python 侧仍能跑基础机器人；需要 Host 的能力返回明确错误码。

---

## 7. 批次 2–7 实施计划

> 每个批次都要求：先写测试/验收脚本 → 小步改动 → 全量离线测试 + 基线全绿 → 更新文档与决策记录 → 再进下一批。

### 批次 2：统一 DataRoot、单实例锁、生命周期与 Windows 优雅停止

**目标**：数据只有一个根；同一时刻只有一个实例；停机前一定 flush。

**改动**：

- 新增 `lib/data-root.js`：
  - `resolveDataRoot({ appDir, configDataDir, env })`，优先级：显式 Config > `PAWBOT_DATA_DIR` > `app/data`。
  - 统一导出子路径：`config.json`、`persona.md`、`memory/`、`backups/`、`brain/`、`napcat/`、`logs/`、`authz/`、`sessions/`、`tmp/`。
  - 迁移与 junction 逻辑从 `data-guard.js` 抽出复用，保留旧函数签名做 adapter。
- 新增 `lib/instance-lock.js`：`<dataRoot>/.runtime.lock`，内容 `{pid, started_at, host_pid, version, nonce}`；启动时若存在且 pid 存活 → 拒绝启动并给出明确提示；pid 不存在 → 视为 stale，归档后接管。
- Python 侧新增控制端点（`webui.py`，仅 loopback + token）：
  - `POST /api/control/shutdown` → 停止接收新事件 → 取消进行中的 Agent/工具 → `store.flush()` → 返回 `{ok:true, flushed:true}` → 延迟退出。
  - `GET /api/control/ready` → 返回 `{ready, db_writable, store_dirty}`。
- `index.js stopChild` 顺序改为：控制面 shutdown → 等 ack（默认 10s，可配）→ POSIX `SIGTERM` → 超时 `taskkill /T /F`（Windows）或 `SIGKILL`。
- `run.py`：注册 atexit/信号双通道；Windows 上用控制端点作为唯一优雅路径；退出码语义固定（0 正常 / 10 收到 shutdown / 其它异常）。
- `index.js` 的 `dataDirectory()`、`brainCwd`、`webui-proxy`、`data-guard` 全部改为引用 `data-root`。

**验收**：

1. 启动 → 写一条记忆 → 调 shutdown → 进程退出后重启，记忆完整（自动化脚本用 `chat_window.py --check` 或直接调 store）。
2. 强杀（模拟 taskkill）后 lock 残留 → 下次启动能识别 stale 并接管。
3. 双实例启动 → 第二个明确失败，不写任何数据。
4. 旧 `dataDir` 迁移路径仍可用；junction 场景测试通过。
5. 全部离线 JS 测试 + 基线 19/19 通过。

**风险**：junction 在非 NTFS/无权限环境失败 → 保持“失败只告警、不迁移”的旧行为。

---

### 批次 3：配置 schema 单一来源

**目标**：一份 schema 生成三端产物，杜绝漂移。

**改动**：

- 新增 `config/schema.mjs`（或 `config/schema.json`）：每个键包含 `path`、`type`、`default`、`secret`、`section`、`label`、`advanced`、`impact`、`migrate`。
- 新增 `scripts/gen-config.mjs`：
  - 生成 `app/pawbot/config_generated.py`（`DEFAULT` 常量；`config.py` 改为 `from .config_generated import DEFAULT` 并保留同名导出）。
  - 生成 `client.js` 的 `COMMON`/`ADVANCED` 元数据块（用显式标记区间，脚本只替换区间内容）。
  - 生成 `docs/CONFIG.md`。
  - `--check` 模式：只比对不写入，供 CI/测试使用。
- 迁移：首次运行把旧 `config.json` 里已废弃键按 `migrate` 映射到新键，未知键保留原样（不丢用户数据）。
- 扩展 `test/client-meta-check.mjs`：断言生成物与 schema 一致、secret 集合一致、默认值一致。

**验收**：

1. `node scripts/gen-config.mjs --check` 在干净工作区返回 0；手改任一默认值后返回非 0。
2. Python 测试全通过；`/pawbot-ui/config` 读写行为不变。
3. 旧配置样本（含未知键）载入后键值不丢。

**风险**：schema 与 Python 动态默认值（如从 env 推导）冲突 → 这类键标记 `computed: true`，生成器只写占位并在 `config.py` 覆盖。

---

### 批次 4：Protocol v2

**目标**：所有跨进程调用有统一信封、版本、请求 ID、错误码；旧路由零破坏。

**改动**：

- 新增 `lib/protocol.js`（Node）与 `app/pawbot/protocol.py`（Python），共享同一份常量定义（由 `config/protocol.json` 生成或直接双端各写一份并由测试比对）。
- 信封：

```json
{
  "v": 2,
  "id": "uuid",
  "ts": 1730000000,
  "kind": "brain.turn | bridge.execute | control.shutdown | authz.state",
  "grant": { "grant_id": "...", "revision": 3 },
  "payload": {},
  "error": { "code": "LEASE_EXPIRED", "message": "..." }
}
```

- 新路由（并行存在）：
  - `POST /pawbot-dsh-brain/v2/turn`
  - `POST /pawbot-dsh-bridge/v2/execute`
  - `POST /api/control/shutdown`（批次 2 已有，纳入 v2 envelope 可选）
- 旧路由改为调用同一 `dispatch` 内核的 v1 adapter（字段映射 + 默认值），不再各写一份限额/解析。
- 错误码表集中定义：`BAD_ENVELOPE`、`UNSUPPORTED_VERSION`、`LEASE_MISSING|EXPIRED|REVOKED|REVISION_MISMATCH`、`TOOL_NOT_ALLOWED`、`RISK_DENIED`、`CANCELLED`、`TIMEOUT`、`INTERNAL_ERROR`。
- 限额保持现状（body/response/RPM/并发/超时），v2 不允许放宽。

**验收**：

1. 旧测试（`dsh-brain-offline.mjs`、`dsh-tool-bridge-*.mjs`）不改一行仍通过。
2. 新测试：信封缺失/版本不支持/超大 body/重复 id/非法 kind → 对应错误码。
3. 一次 turn 的 Host 日志能用同一 `id` 串起“Python 发起 → Agent → 工具 → 返回”。

---

### 批次 5：QQ 授权租约（Authorization Lease）

**目标**：主人可在 QQ 群/私聊临时授权，限时、可撤销、可审计、可广播。

**数据模型**（`data/authz/leases.json`，原子写 + 版本号）：

```json
{
  "revision": 7,
  "updated_at": 1730000000,
  "grants": [{
    "grant_id": "g_...",
    "owner_id": "12345",
    "source": { "kind": "group|private", "id": "..." },
    "scopes": ["device.exec", "fs.write", "net.outbound", "agent.spawn"],
    "issued_at": 1730000000,
    "expires_at": 1730003600,
    "parent_agent_id": null,
    "revoked": false
  }]
}
```

**改动**：

- 新增 `app/pawbot/authz/`：
  - `model.py`：Lease/Grant 数据类、scope 枚举、风险分级表。
  - `store.py`：加载/原子保存/迁移/容量上限；损坏时 fail-closed 并备份坏文件。
  - `secret.py`：安全词哈希（scrypt/PBKDF2，独立 salt，常量时间比较，失败退避）。
  - `commands.py`：解析主人指令（授权 / 撤销 / 状态 / 时长），时长支持 `30m`、`1h`、`2h30m`、中文“半小时/两小时”；未写时长 = 默认 1h；上限可配（建议 24h）。
  - `service.py`：授权/撤销/查询/过期清理/事件广播（`BUS` 或直接消息）。
- 主人判定只认 `account.owner_ids`；非主人指令 → 静默忽略 + 审计（`owner_denied`）。
- 撤销：校验撤销安全词 → 对应 grant `revoked=true` → revision+1 → 广播到 Host（`/pawbot-dsh-brain/revoke` 或 v2 `authz.state`）→ Host 取消该 grant 下所有 Agent。
- 审计：`data/authz/audit.log`（JSONL），只记录 `grant_id/owner_id/action/scope/时间/结果`，**绝不记录安全词**；滚动上限。

**验收**：

1. 非主人发授权 → 无效果 + 审计。
2. 主人授权 1h → 有效期内 scope 生效；到期后高风险操作被拒。
3. 撤销 → 立即失效；Host 侧正在跑的 Agent 收到取消。
4. 错误安全词 → 拒绝 + 退避；日志/响应不含原文。
5. 进程重启后租约仍有效（持久化），revision 连续。

**风险**：QQ 消息被伪造/重放 → 只认 owner_id + 安全词；必要时加“授权需私聊确认”可配开关。

---

### 批次 6：Capability Guard + Agent Supervisor

**目标**：所有工具调用统一过闸；Agent 树可管、可取消、可清理；放开子代理/工作流。

**改动**：

- 新增 `lib/capability-guard.js`：
  - 输入 `{tool, args, caller, grant, revision}`，输出 `allow | deny(code)`。
  - 风险分级：`low`（读状态、查询、业务只读）直接放行；`high`（命令执行、写文件、网络外发、子代理/工作流、computer-use）要求有效 lease 且 scope 覆盖。
  - fail-closed：无 lease / 过期 / revoked / revision 不匹配 / 未知工具 → deny。
  - 每次判定写结构化审计（内存环形 + 可选 JSONL）。
- 接入点：
  - `lib/dsh-brain.js` 的 `agentCtx.tools.guard(...)`（已有，替换内部策略）。
  - 只读桥 `lib/dsh-tool-bridge.js` 的 `execute` 前。
  - Host 工具入口（`pawbot_*` 中带副作用的：restart/config_set/data_backup/send 等）。
- 新增 `lib/agent-supervisor.js`：
  - 注册表：`agent_id → {parent_id, grant_id, scopes, started_at, status}`。
  - 取消树：撤销/超时 → 递归取消子 Agent；`AbortController` 贯穿。
  - 会话日志：按 `dataRoot/sessions/` 归档；保留策略（默认 7 天/500MB，可配），启动时清理。
  - 重启恢复：未完成 Agent 标记 `orphaned`，不自动续跑（避免重复副作用）。
- `lib/dsh-brain.js`：
  - 移除“永久 restrict subagent/workflow/goal”的硬编码；改为“有对应 scope 才挂载”。
  - 子 Agent 创建时继承 `grant_id` + `scopes ∩ parent` + `expires_at = min(parent, now+child_ttl)`；**禁止扩大或延长**（Host 侧二次校验）。
  - 保留现有 `restrict` 作为“隐藏”优化，但安全由 guard 保证。

**验收矩阵**（自动化测试）：

| 场景 | 期望 |
| --- | --- |
| 无租约 + 低风险 | allow |
| 无租约 + 高风险 | deny `LEASE_MISSING` |
| 租约过期 + 高风险 | deny `LEASE_EXPIRED` |
| 已撤销 + 高风险 | deny `LEASE_REVOKED` |
| revision 落后 | deny `REVISION_MISMATCH` |
| 子 Agent 申请父 scope 之外 | deny `SCOPE_ESCALATION` |
| 子 Agent 申请超过父到期时间 | 截断到父到期 |
| 撤销父租约 | 父 + 所有子收到取消，状态 `cancelled` |
| 工具抛异常 | 审计记录 + 对外统一错误码 |

- 会话日志清理：跑 100 轮后目录大小受控；启动清理不删非本系统文件。

---

### 批次 7：Windows / 阿里云运行化

**目标**：一台干净的 Windows Server 能按文档一次装好、可观测、可升级、可回滚。

**改动**：

- 双模式：DSH 托管（现状）+ 独立服务（`app/run.py` 直接跑，Host 不可用时基础功能可用）。
- 启动方式评估：计划任务 / NSSM / WinSW 三选一，**在目标服务器实测后**写入文档；提供安装/卸载脚本。
- 健康检查：`GET /api/control/ready` + `pawbot_health` 组合；失败分级（进程在 / WebUI 通 / NapCat 连 / 模型通）。
- 升级：`scripts/apply-update.ps1` 增加“升级前快照 → 升级 → 数据可读性自检 → 失败自动回滚”。
- 依赖锁定：`requirements.txt` 固定版本（或 `requirements.lock`），`package.json` engines 保持。
- 文档：`docs/DEPLOY-WINDOWS.md`（端口、防火墙、账户权限、日志位置、常见故障）。
- 安全基线：6185/6199 只绑 127.0.0.1；DSH Web 端口对外；WebUI token 建议非空。

**验收**（目标服务器实机）：

1. 全新机器按文档装完 → QQ 收发一条消息。
2. 执行 shutdown → 进程退出、锁释放、无孤儿 python。
3. 升级一次 → 人设/记忆/config 完整；回滚一次 → 恢复。
4. 断网/DSH 未启动 → 基础机器人可用，高风险能力明确拒绝。

---

## 8. 批次之间的协作规则

1. **一次只做一个批次**，每批独立可回滚；不要跨批混改。
2. 每个改动先补测试/验收脚本；没有自动化验证的改动不算完成。
3. 每批结束更新：
   - `docs/decisions-framework-optimization.md`（新增批次结论：做了什么/为什么/实测结果/遗留）。
   - 相关专题文档（如 `docs/DATA-PERSISTENCE.md`、`docs/BRAIN.md`）。
   - 基线契约（若是**有意**变更，同步更新 `scripts/architecture-baseline.mjs` 并在决策记录说明）。
4. 提交粒度：一个逻辑变更一个 commit；信息写清“批次 + 行为变化 + 验证命令”。
5. 不顺手格式化全仓库；不重命名公开导出；不动 `app/data` 的真实数据。
6. 遇到需要产品决策的岔路（例如授权指令语法），先给默认方案并记录，不要停下来等。

---

## 9. 已知环境坑

| 现象 | 说明 / 对策 |
| --- | --- |
| Bash 每次执行后 cwd 被重置到 `D:\whale` | 每条命令都显式 `cd /d/paw-old/pawbot-dsh && ...` |
| `D:\whale` 不是 git 仓库 | git 操作必须在 `/d/paw-old/pawbot-dsh` 下执行 |
| 曾出现 `EPERM ... Temp\claude\...` | 工具沙箱临时目录权限问题；重试或换 `node`/`git` 直接执行。若持续失败，先报告，不要声称完成 |
| DSH 真实 Host 行为未验证 | 桥/Agent 的运行时契约目前是**静态 + 模拟**验证；批次 4/6 需要在真实 Host 做能力探测（headless agent、subagent、workflow、computer-use API） |
| 模型可用性 | 用户期望用 DeepSeek V4.1 Flash + 最高思考强度执行实现；若当前环境没有该模型，用可用模型继续，不要因此停摆 |
| 端口 | 6185（WebUI）、6199（OneBot 反向 WS）只绑本机；DSH Web 端口以 `ctx.webServer.getOrigin()` 为准 |
| 测试数量 | 文档里出现过的 1319/207 等数字是历史快照，以实际运行输出为准，不要写死 |

---

## 10. 下一 Agent 的开工清单

```bash
# 1) 进入工程
cd /d/paw-old/pawbot-dsh

# 2) 确认基线仍绿（必须）
node --check scripts/architecture-baseline.mjs
node --check test/architecture-baseline.mjs
node scripts/architecture-baseline.mjs
node test/architecture-baseline.mjs

# 3) 确认工作区状态（批次 1 未提交）
git status --short

# 4) 建议先提交批次 1（用户未明确要求提交，提交前先确认）
#    git add scripts/architecture-baseline.mjs test/architecture-baseline.mjs \
#            docs/ARCHITECTURE-BASELINE.md docs/ARCHITECTURE-BASELINE-VERIFICATION.txt \
#            docs/decisions-framework-optimization.md docs/HANDOFF-FRAMEWORK-OPTIMIZATION-2026-10-30.md
#    git commit -m "chore(baseline): 批次 1 架构基线检查器与契约测试"

# 5) 读这三份再动手
#    docs/decisions-framework-optimization.md
#    docs/ARCHITECTURE-BASELINE.md
#    docs/HANDOFF-FRAMEWORK-OPTIMIZATION-2026-10-30.md（本文）

# 6) 开始批次 2
```

批次 2 的第一个具体动作建议：

1. 写 `test/data-root-offline.mjs`（先失败）：优先级解析、子路径、stale lock、双实例拒绝。
2. 实现 `lib/data-root.js` + `lib/instance-lock.js`，让测试转绿。
3. 再接 `index.js`，最后做 Python 控制端点与停机顺序。
4. 每步跑全量离线测试 + 基线。

---

## 11. 待定事项（不阻塞当前批次）

- 授权指令的最终自然语言语法与时长格式（批次 5 先实现一套默认，再按使用反馈调整）。
- 高风险能力的最终分类表（批次 6 先按“命令/写入/外发/子代理/computer-use”五类）。
- DSH 宿主实际可用的 headless Agent / subagent / workflow / computer-use API（批次 4/6 在真实 Host 探测后定稿）。
- Windows 服务安装方式：计划任务 / NSSM / WinSW（批次 7 实机对比后定）。
- 图片进入大脑、Cloud Sync 密钥轮换、DSH 不可用时的降级边界细化。

---

## 12. 一句话交接

> 先跑绿批次 1 的基线与测试；然后按“数据根与停机 → 配置单一来源 → 协议 v2 → QQ 授权租约 → Capability Guard + Supervisor → Windows 运行化”的顺序，一批一测一文档地推进；任何高风险能力都必须有有效租约，任何安全词都只以哈希存在。
