# Minecraft 文档定稿清单（只读审计，2026-11-01 20:12 快照）

> 交付物：给 Lead / 三位作者在**写完源码与测试之后**定稿文档用的核对清单。
> 本文**没有**修改任何源码、文档或配置；**没有**重跑任何测试套件（不重复作者/复核者的证据）；**没有**连接游戏、没有读取世界存档、运行态人设、私人记忆或真实凭证。
> 快照边界：`minecraft_resident.py` 在本次审计期间被作者从 15,965 B 改到 104,964 B（20:08），并新增 `test_minecraft_resident.py`（20:11）。凡引用行号，都以本快照为准，定稿前必须重新 `git status`/重读。
> 结论先行：**这几份文档不能直接定稿**。其中「缺口」与「实机未验证」被混写，且多条事实已被 19:30–20:10 的提交推翻；手动步骤里有两处按字面执行会失败或等于绕过安全校验。

## 0. 状态词（建议全文统一，防止再次自相矛盾）

| 状态 | 含义 | 允许的措辞 |
|---|---|---|
| `源码` | 文件存在且（本快照）能编译/导入；无行为证据 | 「已实现，未验证」 |
| `离线测试` | 临时目录/合成适配器下通过，须给命令+日期+数量 | 「离线测试通过（N 项，日期）」 |
| `在写` | 作者正在改，快照不可引用 | 「实现中，不计入定稿」 |
| `实机未验证` | 需要真实游戏/微软账号/真实 LAN | 「实机未验证」 |
| `缺失` | 源码确实没有 | 「缺失」 |

禁止在第 1 节表格之外使用「已支持/可运行/已验证」；禁止把 `in-flight` 文件算进交付声明。

## 1. 明确的过期或误导陈述（按文件）

### 1.1 `docs/MINECRAFT-RESIDENT.md`

| # | 位置 | 现文 | 快照事实 | 定稿动作 |
|---|---|---|---|---|
| S1 | :29 | 「未发现 `java`、`gradle`。守卫因此**尚未在本机编译或运行**」 | `java`/`gradle` 不在 PATH，但 JDK 存在：`C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr`（OpenJDK 21.0.8，含 `javac`）；Gradle 8.7 已落在 `minecraft-guard/.tooling/gradle-8.7`（`bin/gradle.bat`） | 改成「PATH 上没有，但工程内可移植工具链 + 本机 JDK 已用于构建」；补精确命令 |
| S2 | :42 | 「细节以 `minecraft-guard/README` 为准」 | 该文件不存在（`glob` 只命中 `.tooling/gradle-8.7/README`） | 删除该指引，或先补 README 再引用 |
| S3 | :54 | `backup_interval_sec`「60–86400，默认 900」 | `GuardConfig.java:104` 校验 60–1800；:`176` 默认 1800；`config.example.json` 也是 1800 | 改为 60–1800 / 默认 1800，并与 §5 一致 |
| S4 | :62 | 生成密钥的 PowerShell 一行命令 | 实测输出 **36** 字符（`Get-Random -Count` 不允许超过数组长度 36，静默截断），不是预期的 48；虽仍 ≥32 可用 | 换 `[System.Security.Cryptography.RandomNumberGenerator]` 写法，或明说输出 36–48 |
| S5 | :111–117 | 「源码尚未全部对齐：守卫默认 900、mod 固定 1800、侧车 300 秒」 | 已对齐：`config.example.json`/`GuardConfig` 默认 1800，守卫上限 1800；`controller.js:51` 为 `1800000` ms；`minecraft_resident.py:1196` 上限 30 min | 整段改为「已验证事实」并引用三处代码；删掉 900/300 |
| S6 | :156–159 | 「恢复**未实现为工具**」+「手工改名留存/复制/补标记」 | `RestoreTool.java` 已存在（离线 CLI、拒绝覆盖、拒绝运行中世界祖先、标记取自备份、源只读），`GuardSelfTest` 有 4 项 restore 用例；但当前 `compileJava` 失败、无 jar，工具尚不可执行 | 改为「工具存在（源码）、当前无 jar 可运行」，删除手写标记的手工流程（见 B5） |
| S7 | :161–170 | 「独立于模型的紧急停止……2026-11-01 这些都还没有交付」 | 已交付源码：`minecraft_control.py`（CLI，control 路由 + 直连 revoke 回退）、`webui.py:412` 的 `/api/control/minecraft-stop`、`core.py:393` 的 `minecraft_emergency_stop` 与 `_minecraft_estop` 闩锁 | 改为「源码已交付、无测试证据」，并给出可直接照抄的命令与环境变量（见 B4） |
| S8 | :182 | 「共用 `bot.persona`…当前这一共用只有部分落地」 | `minecraft_resident.py` 现在读 `bot.persona.core()`、调用 `emotion.apply/snapshot`、`longmem.add_episode`（`_record_episode`） | 改为「源码已接线，待 resident 测试证明」；不要再说「未读 persona/未写 longmem」 |
| S9 | :189 | 中继实例「契约要求；集成测试待 agent-1c 提供」 | `core.py:344–349` relay 模式提前 return，居民源码上确实不会启动；仍无测试 | 保留「无测试」，删掉「待 agent-1c」这类内部编号 |
| S10 | :11 | 表格把守卫写成「客户端 mod」 | `fabric.mod.json` 是 `environment: client`、`client` 入口，但只在集成服务器上生效；`PROTOCOL.md:7` 写「integrated-server mod」 | 统一一句：「客户端安装、仅绑定集成服务器生命周期」 |

### 1.2 `docs/MINECRAFT-ACCEPTANCE.md`

| # | 位置 | 现文 | 快照事实 | 定稿动作 |
|---|---|---|---|---|
| S11 | :22–28 | `test_minecraft_guard` 5 项、`test_minecraft_policy` 9 项、sidecar「95 项，94 通过，1 失败」、`test_minecraft_{sidecar,integration,resident}.py` 不存在、基线「19 项 + 43 项」待跑 | guard 测试文件现有 **27** 个 `def test_`（静态计数），policy **11** 个；sidecar README 与作者口径均为 **94 通过 / 0 失败 / 0 跳过**；`test_minecraft_resident.py` 20:11 已出现；sidecar/integration 仍无 | 整表用定稿时一次运行的输出重写；区分「文件已存在」与「已跑过」 |
| S12 | :41 | 「守卫从未编译，无 Java 测试证据」 | `build/pure-classes`（19:19）与 `build/test-classes`（19:22）说明纯 Java 核心**曾**用 JDK 直接编译通过（不证明当前源码仍通过）；`GuardSelfTest.java` 有 **36** 个用例（含 restore/revoke/backup）；无运行记录；Fabric 适配层 20:02 `compileJava` **失败** | 拆成三条：核心曾编译（未跑）/自测用例已写（未跑）/Fabric 适配未编译通过 |
| S13 | :44 | 「源码默认值（900/1800/300 秒）待对齐」 | 已对齐（见 S5） | 删除该缺口 |
| S14 | :45 | 「离线恢复 缺失」 | `RestoreTool.java` 已存在且有 4 项自测用例 | 改为「源码已有；jar 未产出，未演练」 |
| S15 | :46 | 「回环停止路由、命令行与 `minecraft_control.py` 已指派 agent-1c，未交付、无测试」 | 三者均已有源码；**`app/tools` 下未找到任何引用 `minecraft_control`/`minecraft-stop` 的测试** | 改为「已交付源码、无测试、无演练」 |
| S16 | :66–69 | 重连退避「缺失」、HA「缺失」、`core.py` 仅警告 | `minecraft_sidecar.py:237` 有 `connect_with_retry`（指数退避 + 只重放 connect）；`minecraft_resident.py:1389` 有 `_reconnect`；`core.py` relay 提前 return | 逐行改状态；仍标注「无测试」 |
| S17 | :74–98 | 心智模型大面积「缺失/部分」（工作空间、appraisal、习惯、ImpulseEngine、Arbiter、预算、防循环、结构化日志） | resident 源码已含 `GlobalWorkspace`、`Appraiser`、`Drives`、`projects/promises/people/habits/journal`、`ImpulseEngine`、`DeterministicPlanner`、`Arbiter`、`JevAdapter`、`DecisionBudget`、`_failure_streak`、`Decision.safe_log` | 不要整段保留；按「源码已实现（在写）/离线测试（待）/实机未验证」重列，并等 resident 测试结果 |
| S18 | :108 | 「JEV 真实接口接入｜阻塞于主人答复」 | 默认关闭；`decision_enabled=false` 时规则回退可独立工作；`JevAdapter` 只有在成本上限/每日次数都配置时才启用 | 改为「真实 JEV 接入是**启用远程决策**的前置，不是默认态阻塞」；保留「接口/预算需主人答复」 |
| S19 | :3–5、:24、:116 | 大量 `agent-1e`、`task17/18/19` 内部编号 | 面向主人的文档不应要求读者知道内部 agent/task | 正文删编号；需要追溯时放附录「证据出处」 |

### 1.3 `docs/decisions-minecraft-resident.md`

| # | 位置 | 现文 | 快照事实 | 定稿动作 |
|---|---|---|---|---|
| S20 | :16–22 | 「本切片**没有** Fabric mod、Node 侧车、命名技能、备份调度、实机连接」 | mod、侧车、技能、备份、恢复、控制面均已有源码 | 把「Current implementation」整节重写为「已实现（源码）/未验证（实机）」；保留「no live connection」这一条 |
| S21 | :14 | 备份策略「documented but not yet implemented」 | 已实现于 `BackupManager`/`GuardMod`，并有自测用例 | 删除「not yet implemented」 |
| S22 | :26 | 「首次切片时没有 Java/Gradle 工具链」 | 作为历史事实可保留，但必须加现状：JBR JDK + `.tooling` Gradle 8.7 已存在，编译尝试有日志 | 补一句现状，避免读者误以为仍无工具链 |

### 1.4 `docs/MINECRAFT-PROTOCOL.md`

| # | 位置 | 现文 | 快照事实 | 定稿动作 |
|---|---|---|---|---|
| S23 | :9 | 「Return 403/409 on denial」 | 实际还有 401（Bearer 错）、413（体过大）、405/404/503；`minecraft_guard.py:161` 只把 403/409 当 `guard_denied`，401 会落到 `guard_unavailable` | 协议要么补全状态码语义，要么明确「401 = 密钥不匹配」的诊断含义；否则排障会误判 |
| S24 | RESIDENT:52 | 守卫 `pvp_uuids` 上限 16 | 守卫确实 16（`GuardConfig.java:93`）；侧车环境接受到 64（`guard.js:13`）；resident 直接透传配置列表 | 文档明确「以守卫 16 为上限」，否则主人填 20 会在运行期被拒 |

### 1.5 计数口径互相矛盾

- RESIDENT:187 说基线「39 个顶层段」，ACCEPTANCE:27 说「架构基线 19 项 + 43 项」——两者其实是不同套件（配置段数 vs 脚本/测试计数），但文档都没写清；定稿时写成「配置段 39（`config.DEFAULT`）/基线脚本 19 项/基线测试 43 项」并给同一次运行的时间戳。
- 守卫测试：`build.gradle` 配了 JUnit Jupiter，但测试目录**没有任何 `@Test`**（`GuardSelfTest` 是 `main` 运行器）。若定稿写「`gradle test` 通过」等于零覆盖——必须写实际运行方式（见 E1）。

## 2. 按字面执行会失败或会绕过校验的指令

| # | 位置 | 问题 |
|---|---|---|
| B1 | RESIDENT:40–42 | §4 标题承诺「构建、安装」，正文只指向不存在的 README；**全文没有任何** Fabric Loader/API 安装、`mods/` 放置、启动器配置、jar 产物路径的步骤 → 读者无法进行到下一步 |
| B2 | RESIDENT:62 | 密钥命令输出 36 字符（见 S4）：不报错、但和「48」的隐含意图不符；若有人按「Count 48」理解会以为失败 |
| B3 | RESIDENT:139–147 | 启用顺序有硬伤：步骤 4「守卫 `enabled=true`，重载世界」会退出并重新进入世界 → **LAN 会关闭、端口变化**；步骤 2–3 记下的端口随即失效，步骤 5 的连接必然连错端口。正确顺序必须是：改配置 → 重进世界 → 重新 Open to LAN → 再填 PawBot `port` → 重启 PawBot |
| B4 | RESIDENT:161–170 | 紧急停止写「只有退出游戏/撤销」：新 CLI 无法按文档使用——`minecraft_control.py:74–80` 的直连回退需要调用者 shell 里同时有 `PAWBOT_MC_GUARD_SECRET`、`PAWBOT_MC_WORLD_ID`、`PAWBOT_MC_WORLD_PATH`、`PAWBOT_MC_ACTOR_UUID`、**`PAWBOT_MC_LAN_PORT`**（默认 0 → `GuardSettings.validate` 直接 `invalid_guard_settings`）；control 路由还需要 `PAWBOT_CONTROL_TOKEN`，而该变量在 PawBot 启动时已被 `lifecycle.take_control_token` **从环境里 pop 掉**，CLI 必须自己另带一份。这五个变量文档一个都没写 |
| B5 | RESIDENT:159 | 手工恢复流程要求「复制备份内容到新空目录并**补上** `.pawbot-test-world` 标记」，与 `RestoreTool` 的安全规则冲突（标记必须来自备份且 `world_id` 精确匹配；目标不得已存在；祖先含 `session.lock` 即拒绝）。手写标记会把「来源不可验证」的目录伪装成已校验副本 |
| B6 | RESIDENT:126 | 认证目录 `"<数据根>\state\minecraft\auth"` 依赖「数据根」，但全文没定义数据根优先级：`Config.dataDir` > `PAWBOT_DATA_DIR` > `<appDir>/data`（`paths.py`/`lifecycle.resolve_data_root`）。填错目录时 `auth.mjs` 会把缓存写去别处，协议进程随后只报静态错误 `Local authentication setup required`，无法看出是路径问题 |
| B7 | RESIDENT:143 | 「观察日志：应出现备份成功、连接成功」：连接前备份**不打日志**（只有周期备份打 `periodic backup ok`，`GuardMod.java:149`）；失败时 `core.py:362` 只记异常类型（如 `GuardDenied`），不告诉人是哪个校验失败 |
| B8 | RESIDENT:166 | 「重新启用必须由人手工删除该文件」：`PermitService` 的 `revoked` 是内存闩锁，删文件后**必须重进世界/重启**才会重新构造服务；只删文件不会恢复。守卫日志自己写的是 `delete ... to re-arm`，容易让人以为立即生效 |

## 3. 遗漏的必需准备/恢复/面向主人的步骤

| # | 类别 | 必须补进最终文档的内容 |
|---|---|---|
| O1 | 工具链 | 明确「不在 PATH ≠ 不存在」：JDK 路径（含 `javac`）、`minecraft-guard/.tooling/gradle-8.7/bin/gradle.bat`、需设的 `JAVA_HOME`、以及 `.tooling/`（含 134 MB zip 与 Gradle home）**未被 `.gitignore` 覆盖**——定稿前要么忽略、要么写清「不得提交」 |
| O2 | 安装 | Fabric Loader ≥0.15.0 + Fabric API 0.92.2+1.20.1（`gradle.properties`/`fabric.mod.json` 依赖）安装到客户端、guard jar 放进 `mods/`、启动器选择带 Fabric 的实例；守卫只对**单人集成服务器**生效 |
| O3 | 世界与标记 | 先建世界并**退出到主菜单**再放 `.pawbot-test-world`；内容必须与 `world_id` 完全一致（读取处会 `trim()`，但 BOM 会造成 mismatch）；路径不得含链接/联接；标记只在**进入世界时**校验，给已打开的世界补标记无效 |
| O4 | 配置完整性 | RESIDENT:76–97 的样例缺 `connect_timeout_sec`、`permit_max_age_sec`、`max_decisions_per_day`、`decision_budget_usd_per_day`、`owner_mappings`、`interest`（均为 `config.DEFAULT` 已有键）；守卫样例缺 `max_backup_files`/`max_backup_bytes`（`config.example.json` 有）。应给「全部键 + 默认值 + 是否必填」 |
| O4b | 未登记的可选键 | resident 还会读 `decision_remote_enabled`、`decision_cost_usd_per_call`、`decision_interval_sec`、`reconnect_attempts`、`reconnect_backoff_sec`，但这些键**不在 `config.DEFAULT`**，也没进样例；`workspace_capacity` 则是从持久化居民状态读取、**不是配置键**。文档不要把 `workspace_capacity` 写成可配；其余要么登记为可选键，要么删掉读取 |
| O5 | 控制面 | `PAWBOT_CONTROL_TOKEN`（生成方式、只走环境变量、PawBot 启动即消费）、`PAWBOT_WEBUI_PORT`（默认 6185）、`PAWBOT_MC_LAN_PORT`（仅 CLI 直连回退用）；以及 `python -m pawbot.minecraft_control stop` 的完整可复制命令 |
| O6 | 排障 | 静态错误码 → 主人动作的对照表：`owner_identity_unbound`（`owner_qq_uid`/`owner_player_uuid` 未配或不是完整数字 UID）、`connect_config_invalid`（host/port/version/username/auth_mode/timeout）、`guard_context_mismatch`（端口/世界/actor 不符）、`actor_mismatch`（登录的账号 ≠ 配置 UUID）、`world_mismatch`、`sidecar_not_ready`、`queue_full`、`Local authentication setup required`、`invalid_guard_settings`（含“密钥/端口没进环境”）。当前日志只给异常类型，文档必须承担这部分 |
| O7 | 成功/失败判据 | 给「成功时应看到的两行日志」（守卫 `listening on 127.0.0.1:6198`、居民 `Minecraft resident connected ...`）与「失败时应先查什么」；说明 `GuardDenied` 不等于“守卫挂了”，也可能是 401 密钥错 |
| O8 | 备份演练 | 明确备份根、`retention`、20000 文件 / 2 GiB 上限、排除 `session.lock`；要求主人做一次「备份→列出 backup-<13位>-<3位>→离线恢复→注册新目录」演练并保留输出 |
| O9 | 撤销演练 | 撤销后验证：许可 409、actor 被断开、`config/pawbot-guard.revoked` 存在；重新启用 = 删文件 **且** 重进世界；不要写成“删了就好” |
| O10 | 回滚 | `minecraft.enabled=false` 重启即可回滚，但要提醒：`PAWBOT_MC_*` 环境变量可留着（不会自行启用），而 `_minecraft_estop` 闩锁与 revoke 标记**不会**因重启 PawBot 自动清除 |
| O11 | 隐私边界 | 设备码/缓存/密钥/世界路径不进日志与聊天；认证目录只在本机；控制 token 不进 argv/history（`minecraft_control.py` 已按此实现，文档要写成主人义务） |

## 4. 必须保留的「原始设计意图」与恢复安全（定稿时不要被改写掉）

**隔离与授权**
- 只用**新建的一次性世界**；不复制、不改名、不接管主人原存档；无法确认副本就拒绝写入。
- 守卫是唯一授权方：写入与 PvP 分离，PvP 另需精确 UUID 名单（守卫上限 16，无通配，不含 actor）。
- 每次连接前必须有一次**新鲜备份**；守卫是“备份是否有效”的权威，最长 1800 秒；默认保留 7 份。
- 撤销是粘性的：撤销后不再签发许可、断开 actor、**不会自动恢复**。
- 宿主紧急停止不经模型：先撤销 → 再停居民/侧车 → 闩锁到 PawBot 重启。

**恢复安全（来自 `RestoreTool`，定稿必须照此描述，不要回退到手写标记）**
- 只离线执行；目标目录**必须不存在**（绝不覆盖，因此不可能覆盖原世界）。
- 目标父目录必须存在且不是链接/重解析点；目标的任一祖先若含 `session.lock`（正在运行的世界）即拒绝。
- 目标不得与备份根互相嵌套，也不得是备份源。
- 备份 id 必须是 `backup-<13位数字>-<3位数字>`；备份内的 `.pawbot-test-world` 必须与 `world_id` 精确一致；备份内出现 `session.lock` 视为活世界，拒绝。
- 源备份只读，逐文件复制并校验大小，先写 `<target>.restoring` 再原子改名；失败清理暂存。
- 恢复出的目录**需要人工重新登记**（更新守卫 `save_path`），绝不自动采用。

**认证**
- 设备码只在本机交互终端出现一次；协议进程永不申请新设备码；缓存只在 `PAWBOT_MC_AUTH_DIR`；无自动回退到离线。

## 5. 定稿前必须补齐的最终证据（按作者/复核者）

> 每条都要**命令 + 日期 + 完整输出/退出码 + 环境版本**；「作者口头说通过」不算。

| 流 | 文档将要声称的内容 | 必须提供的证据 | 本快照状态 |
|---|---|---|---|
| E1 守卫（Fabric） | 「守卫可构建、自测通过、含恢复工具」 | ① `compileJava` 变绿的完整输出（当前 **失败**：`IntegratedWorldAdapter.java:68`，`submit(Callable)` 不适用，3 errors，20:02 daemon 日志）② 产出 jar 的路径/大小/SHA-256 ③ 用 JDK 运行 `GuardSelfTest` 的**实际输出与退出码**（36 个用例；注意 `gradle test` 会是 0 用例）④ 明确「Fabric 适配层未在真实集成服务器上验证」 | ①②缺；③只有源码，无运行记录 |
| E2 侧车 | 「94 通过 / 0 失败 / 0 跳过；audit 0；语法 8 文件」 | 定稿冻结后的一次 `npm ci --ignore-scripts`、`npm test`、`npm audit --audit-level=moderate`、`node --check`（8 个源文件）输出；Node 版本；README 与文档数字一致 | 作者口径 94/0，README 已写 94；验收文档仍写 95/94/1，必须二选一改齐 |
| E3 Python 集成 | 「侧车监督/重连/停止/HA 隔离有测试」 | `test_minecraft_sidecar.py`、`test_minecraft_integration.py` 的结果；`test_minecraft_guard`（27）/`test_minecraft_policy`（11）一次运行输出；`scripts/architecture-baseline.mjs`（19）+ `test/architecture-baseline.mjs`（43）；全量 unittest；**控制面**：`/api/control/minecraft-stop` 与 `minecraft_control stop` 的测试（当前不存在） | 文件缺失/无测试 |
| E4 居民 | 「心智模型、预算、回退、恢复、日志已实现并测试」 | `test_minecraft_resident.py` 运行结果（20:11 刚出现）；默认 `decision_enabled=false` 下规则回退可用；预算先扣后调、失败不退款；技能失败不终止循环；断线/死亡/寻路失败恢复；结构化日志字段；persona/emotion/longmem 共用的断言 | 源码在写，测试未跑 |
| E5 复核 | 「计数与结论已复核」 | 复核者给出唯一一份数字表（与 E1–E4 同一次快照），并注明跳过项；`.tooling`/`node_modules` 未入库 | 未提供 |
| E6 实机（主人） | 「真实 LAN 连接/认证/观察」 | 只能是主人执行：记录 MC 版本、LAN 端口获取方式、认证模式、连接成功截图/日志；两种认证模式各一次；备份+恢复演练；撤销/紧急停止演练；专用副本 30–60 分钟观察记录与自动日志 | **仍为零**；任何 mock/离线结果都不得替代，也不得写成“已验证” |
| E7 JEV（主人） | 「真实 JEV 接入」 | 接口地址/认证/超时/预算的书面答复；接入后的 mock↔真实切换证据 | 未提供；默认关闭 + 回退已实现，**不阻塞默认态交付** |

**「源码缺口」与「实机限制」必须分开写**：E1 的适配层编译失败、E3 的缺失测试属于**源码缺口**；E6/E7 属于**无法由编码方自行完成的外部验证**。两类都不能用“已经有 mock”糊过去，也不能把前者写成“等主人”。

## 6. 定稿写作规则

1. 只引用**一个冻结快照**（写明 commit/时间/`git status` 摘要），所有计数来自同一次运行。
2. 三份文档对同一事实只允许一种说法：`backup_interval_sec`（60–1800/1800）、sidecar 计数、Python 计数、pvp 上限 16、工具链状态。
3. 契约以 `MINECRAFT-PROTOCOL.md` 为准；侧车的实现契约以 `app/minecraft-sidecar/README.md` 为准，手册不要用自己的措辞复述这两处（否则又会产生第三套数字/限制）；实现偏差要么改协议（版本化、经主人确认），要么在手册写「实现与本条不同」。
4. 手册面向主人：不要出现 `agent-1x`/`taskNN`；内部证据放附录。
5. 每个「未实现/未验证」后面必须跟：谁能补、补什么证据、补之前用户该怎么做。
6. 定稿前重新核对 `minecraft_resident.py`、`test_minecraft_resident.py` 与守卫 `compileJava`——本快照之后它们仍在变（20:08–20:11 已变两次）。
