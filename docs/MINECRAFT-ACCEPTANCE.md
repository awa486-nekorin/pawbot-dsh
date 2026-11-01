# Minecraft 居民：需求覆盖矩阵与验收证据

> 状态：**验收证据定稿，2026-11-02 00:16–00:30 最终独立复核快照**（HEAD `3dff9ef`，未提交）。
> 最终独立复核报告：[final-verification-2026-11-02.md](../reports/minecraft-review/final-verification-2026-11-02.md)
> （2026-11-02，verdict **READY OFFLINE**）。
> 「READY OFFLINE」只覆盖当前树的**离线工程与证据**；第 4 节的 L1–L8 实机项**一项都没有做，一项都不算已验证**。
> 需求来源：桌面 `不可用提示詞-修改稿.txt`（v4）与 [MINECRAFT-PROTOCOL.md](MINECRAFT-PROTOCOL.md)；
> 操作步骤见 [MINECRAFT-RESIDENT.md](MINECRAFT-RESIDENT.md)。
> 证据文件都在 `reports/minecraft-review/` 下。除特别标注外，本文只引用**已完成的日志**，不把仍在写入的文件当结论。

## 1. 状态词（全文统一）

| 状态 | 含义 | 允许措辞 |
| --- | --- | --- |
| `源码` | 代码存在且能编译/导入；没有行为证据 | 「已实现，未验证」 |
| `离线测试` | 临时目录/合成适配器下通过，附命令与日期、数量 | 「离线测试通过（N 项）」 |
| `静态核对` | 只读源码逐项比对（配置键、默认值、粘贴链） | 「与源码一致（静态核对）」 |
| `实机未验证` | 需要真实游戏/微软账号/真实 LAN | 「实机未验证」 |
| `缺失` | 源码确实没有 | 「缺失」 |

「源码缺口」与「实机限制」必须分开看：前者是编码方还能补的，后者只能由主人在真实环境完成。

## 2. 测试与证据表（2026-11-02 最终独立复核快照）

> 下表是**同一次冻结快照**的唯一证据表：2026-11-02 00:16:40 – 00:30:20 +08:00，HEAD `3dff9ef`，
> 快照期间关键文件 mtime/SHA-256 未变（最后一次 Minecraft 源码写入是 00:06:39 / 00:07:54）。
> 每一行都是独立复核者在这份树上**复跑**的结果，不采信作者报告的数字；全部 exit 0。
> 报告：[final-verification-2026-11-02.md](../reports/minecraft-review/final-verification-2026-11-02.md)（verdict **READY OFFLINE**）。
> 旧日志 `reports/minecraft-review/verified-*.log`（23:45–23:53）与 `final-verify/04-focused-minecraft-python.log`
> （00:02 中断留下的仅表头文件）属于更早/不完整的快照，**不与本表混用**。

| # | 命令（cwd） | 完成时间 | 退出码 | 结果（数量 / 失败 / 跳过） | 日志 |
| --- | --- | --- | --- | --- | --- |
| E1 | `py -3.13 reports\minecraft-review\probe\revoke_durability_false_success.py`（仓库根） | 00:17:01 | 0 | `no false success`；第一次 `ok:false`；第二次 409 → `already_revoked_unverified`、`revoke_durable:false`、`ok:false`；0 失败 | `final-verify/01-revoke-durability.log` |
| E2 | `py -3.13 reports\minecraft-review\probe\farm_fallback_probe.py`（仓库根） | 00:17:01 | 0 | `kind = shelter`；5 个步骤（move→collect dirt×4→place×2→eat）；`fallback behaved as claimed` | `final-verify/02-farm-fallback.log` |
| E3 | `py -3.13 reports\minecraft-review\probe\farm_stand_reach_probe.py`（仓库根） | 00:17:01 | 0 | `stand=-2.0,64.0,2.0`；两块土 `distance=4.123 / 4.123`，`child_limit=4.5 ok=True`；`all generated farm steps are within child reach` | `final-verify/03-farm-stand-reach.log` |
| E4 | `py -3.13 reports\minecraft-review\probe\habit_formation_probe.py`（仓库根） | 00:17:02 | 0 | `habit_keys=['greet_known','return_home','tend_farm']`；三者 count 均为 3；`candidate_branches_reachable=True` | `final-verify/04-habit-formation.log` |
| E5 | `py -3.13 -m unittest tools.test_minecraft_guard tools.test_minecraft_policy tools.test_minecraft_sidecar tools.test_minecraft_control tools.test_minecraft_integration tools.test_minecraft_resident -q`（`app`） | 00:17:55 | 0 | **`Ran 201 tests in 43.540s — OK`**；0 failure / 0 error / 0 skip | `final-verify/05-focused-minecraft-python.log` |
| E6 | `py -3.13 -m unittest discover -s tools -q`（`app`） | 00:22:51 | 0 | **`Ran 2179 tests in 288.260s — OK (skipped=2)`**；0 failure / 0 error；跳过 2 项见 E6b | `final-verify/06-full-python-discovery.log` |
| E6b | 两条 skip 单独 `-v` 复跑（`app`） | 00:30:20 | 0 | 跳过原因确认：`tools.test_memes.SeedDataTests.test_seeded_book_is_usable`（`还没跑 tools/seed_memes.py`）、`tools.test_ha_content.SnapshotTests.test_build_rejects_names_no_windows_peer_could_apply`（Windows 会剥掉结尾点/空格，构造不出该文件名） | `final-verify/06b-skip-reasons.log` |
| E7 | `cd app/minecraft-sidecar; npm test` | 00:18:16 | 0 | `115 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo`；`duration_ms=4798.5569`（wall 5334 ms） | `final-verify/07a-sidecar-npm-test.log` |
| E7b | `node --check index.mjs auth.mjs src/{main,adapter,controller,guard,skills,auth}.js`（`app/minecraft-sidecar`） | 00:18:16 | 0（8/8） | 8 个模块全部通过语法检查 | 同 E7 日志的 `--- syntax ---` 段 |
| E8 | `cd app/minecraft-sidecar; npm audit --offline --audit-level=moderate` | 00:18:25 | 0 | `found 0 vulnerabilities` | `final-verify/07b-sidecar-npm-audit.log` |
| E9 | `gradle.bat -p minecraft-guard clean build --offline --no-daemon --console=plain`（仓库根；`JAVA_HOME`=JBR 21.0.8，`GRADLE_USER_HOME`=`minecraft-guard\.tooling\gradle-home`） | 00:24:05 | 0 | `BUILD SUCCESSFUL in 18s`；**`passed=63 failed=0`**；`> Task :remapJar` executed；`:test SKIPPED`（`build.gradle` 故意禁用，`check`→`selfTest`）；`7 actionable tasks: 7 executed` | `final-verify/10-guard-clean-build.log` |
| E10 | jar 产物检查（`Get-FileHash`；class 常量池解析；官方 merged jar 对照） | 00:27:04 | 0 | **70,856 B**；SHA-256 `B373B470F6BDE30A01EEC5C9723340DF6FD81EA43459AA97552FD4AA29D7A82F`；39 entries；class major 61（Java 17）；**8 个 intermediary Class 引用（含 `class_3324` PlayerManager）/ 0 个 yarn-only 引用**；另有 1 个 yarn 名引用 = `MinecraftServer`（official identity mapping） | `final-verify/11-guard-artifact.log` |
| E11 | `node scripts/architecture-baseline.mjs`（仓库根） | 00:18:33 | 0 | `PASS — 19 passed, 0 failed, 19 total` | `final-verify/08-baseline-checker.log` |
| E12 | `node test/architecture-baseline.mjs`（仓库根） | 00:18:33 | 0 | `46 passed, 0 failed, 0 skipped` | `final-verify/09-baseline-contract-tests.log` |
| E13 | `javac --release 17` 编译 4 个 Java 探针后逐个 `java -cp` 运行（仓库根） | 00:27:34 | 0 | RevokeDurability：`503 unsigned` → `409 revoked`，`restarted_revoked=false`；RevokeRace：`revoke_status=200`、撤销后 connect `409`；Stress 100 轮：`connect200=61 connect409=39 late200=0`；JunctionRestore：`ALL PROBES PASSED` | `final-verify/12-java-probes.log` |
| E14 | `node reports\minecraft-review\probe\node_busy_http_probe.mjs`（仓库根） | 00:29:23 | 0 | busy observe=`17` 且不隔离；busy skill=`-32008`、`stopCount=1`、连接保持；`revoke_detect_ms=2` 且立即隔离；`PROBE PASSED` | `final-verify/13-node-busy-http.log` |

补充说明：

- E6 的 2 个 skip 与本次 Minecraft 交付无关（memes 种子数据未生成；Windows 文件系统构造不出该负例），
  E6b 已单独确认原因；`FAIL:`/`ERROR:` 计数为 0，日志里 16 条未 await 协程 `RuntimeWarning`（32 行含该词，无关模块）是既有噪声。
- E9 的 `:test SKIPPED` **不是跳过测试**：`build.gradle` 故意 `test { enabled = false }`，`check` 依赖无 JUnit 依赖的
  `GuardSelfTest`，其结果就是 `passed=63 failed=0`。
- E10 的 hash 由 `clean build` 复现（与 23:47 前序构建一致）；宿主自己重建仍可能因环境差异得到不同 hash。
- E1–E14（含 E6b/E7b）都属于同一份 00:16–00:30 快照；`19/19`（检查器）与 `46/0/0`（离线测试）都在这份树上重跑。
- **jar 级恢复 smoke 仍未重跑**：E13 是对 `build/classes` 上同一 `RestoreTool` 的行为探针，不等价于 jar 演练；
  真实恢复演练属 L5，仍未做。
- 最终报告另列两项**低风险残余**（不是本表的失败项，也不是实机项）：R1 没有单一 Java→Node 真实 socket 的
  busy/revoke 端到端用例；R2 busy 回执的 `attested_at` 没有最大年龄。

## 3. 源码项：已修复 / 仍开放的限制（编码方范围，不要写成「等主人」）

| # | 项 | 状态 | 事实与证据 | 对主人的影响 |
| --- | --- | --- | --- | --- |
| G1 | 两个配置键没有读取者 | **仍开放（限制）** | `config.DEFAULT.minecraft.backup_retention` 与 `minecraft.interest` 在 `app/` 下没有调用点；保留份数由守卫 `retention` 决定 | 按文档改这两个键不会有效果；文档已写明「以守卫为准」 |
| G2 | 401 与其他传输错误被合并 | **仍开放（限制）** | `minecraft_guard.GuardClient._request` 只把 HTTP 403/409 归为 `guard_denied`，401/413/405/404/415/503 一律 `guard_unavailable` | 密钥写错时排障会误判成「守卫没起」；手册第 10 节已解释 |
| G3 | 农田回退坐标超出侧车可达范围 | **已修复** | 站位改为「最坏距离最小」再**丢弃超过 4.5 格的土**（`minecraft_resident.py:3025-3047`），`FARM_SOIL_REACH=4.5` 与 `adapter.js loadedBlock(p,4.5)` 对齐。修复证据 `reports/minecraft-review/fix-farm.log`（2026-11-01 23:25，`4.123/4.123 ≤ 4.5`，负例 `test_opposite_corner_soil_stays_within_the_child_reach_from_stand`）；最终复核 E3 | 农田步骤不再因站位被侧车拒绝 |
| V2 | 无土可种时不回退 shelter | **已修复** | 可种植土壤为空时项目按文档回退到 `shelter`。修复证据 `reports/minecraft-review/fix-farm-fallback.log`（2026-11-01 23:57，`kind = shelter`、5 步、exit 0）；最终复核 E2 | 荒地上不会卡在不可能完成的农田项目 |
| G4 | 撤销持久化失败仍可能被重报为成功 | **已修复** | `core._minecraft_guard_revoke` 对**裸的已认证 409** 返回 `(False, "already_revoked_unverified")`；`ok` 显式要求 `revoke_durable is True and revoked is True`（`core.py:497-532, 665-672`）。修复证据 `reports/minecraft-review/fix-revoke.log`（2026-11-01 23:43，`no false success`、`revoked:false`、`durable:false`、`ok:false`）＋ Java 用例 `revoke is not acknowledged when the marker cannot be persisted`；最终复核 E1/E13 | 看到 `ok:false`+`already_revoked_unverified` 时按「本次停止不完整」处理，人工核对标记 |
| G5 | 守卫定时快照仍在服务器线程内联复制 | **仍开放（限制）** | `GuardMod.onServerTick` → `BackupSchedule` → `periodicBackupAttemptOnServerThread` 内联 `saveAll` + 复制；只有文件数/字节预算兜底 | 大世界可能卡 tick；真实耗时未测，属 L4 |
| G6 | 协议文档的紧急停止段落已过期 | **已修复** | `MINECRAFT-PROTOCOL.md` §4 已改为真实语义：裸 409 只证明内存闩锁（`revoke_durable:false`、`ok:false`、`revoke_error already_revoked_unverified`；无居民控制路径同时写 `error`），durable 的「已撤销」只在本进程此前记录过**该守卫身份**的签名回执时成立；旧的 503→409 残余段落已删除；port 0/当前 LAN 端口语义不变。port-0 与诚实 noop 段落自 `fix-protocol.log`（2026-11-01 23:36）起已与源码一致 | 排障者不会再照过期协议得出相反结论 |
| G7 | 最终测试表不是同一快照 | **已关闭** | 最终独立复核在同一棵树上重跑并给出唯一一张表：聚焦 **201 / 43.540 s / OK**、全量 **2179 / 288.260 s / OK (skipped=2)**（第 2 节 E5/E6）。「E4 早于最后 Python 改动」的限制不再适用 | 可以放心引用单一数字表 |
| G8 | owner-human 的两套判定不一致 | **已修复** | 居民不再自实现：`_owner_human_uuid()` 直接 `return owner_human_identity(self.config)`（`minecraft_resident.py:1583-1594`）；policy 层要求精确规范化 UUID、必须 ≠ `owner_player_uuid`、拒绝 `owner_mappings`/`owner_qq_uid` 冲突与一对多；`_role_for` 只按 actor→self、该精确 UUID→owner。修复证据 `reports/minecraft-review/fix-identity.log`（2026-11-01 23:29）；最终报告 §3.6 | 冲突的本机映射下两层判定一致地失败关闭，不会一边认主一边不认 |
| V1 | 语义习惯永不形成 | **已修复** | `_HABIT_KEY_FOR_SKILL` 把 home/farm/follow/chat 映射到 `return_home`/`tend_farm`/`greet_known`，`_success()` 末尾 `_bump_habit(...)`，阈值 count≥2；`_bump_habit` 持久化计数。修复证据 `reports/minecraft-review/fix-habit-formation.log`（2026-11-02 00:13，3/3/3、负例失败符合预期）＋ 用例 `test_successful_actions_form_semantic_habits_across_threshold`；最终复核 E4 | 习惯分支（回家/照看农田/熟人问候）不再是死代码 |

说明：标为「已修复」的项在 2026-11-02 的最终独立复核里都有对应的探针或源码抽查，**不需要主人做任何动作**，也不再是缺口。
仍开放的 G1/G2 是配置与排障层面的已知限制（文档已按现状写明），G5 只在真实大存档时才可能显现（属 L4）。

## 4. 实机限制（只能由主人完成，不能用 mock 替代）

| # | 项 | 现状 |
| --- | --- | --- |
| L1 | 真实客户端加载守卫 jar（Fabric 0.15.x + Fabric API 0.92.2+1.20.1） | 未做 |
| L2 | 真实 Open-to-LAN 端口获取、变化后的重连 | 未做 |
| L3 | 微软设备码登录、令牌续期、正版账号在 LAN 下的行为 | 未做（不读取真实凭证，属主人操作） |
| L4 | 真实 `saveAll` + 大存档快照耗时、Windows 链接/重解析点边界 | 未做 |
| L5 | 真实恢复演练（备份 → 新目录恢复 → 重新登记） | 未做；当前 70,856 B jar 也未重跑离线 smoke |
| L6 | 专用副本内 30–60 分钟自主观察（含自动日志） | 未做 |
| L7 | 真实 JEV 服务接入（地址/认证/超时/预算） | 未提供；默认关闭 + 规则回退已实现，不阻塞默认态 |
| L8 | 真实 Node 24 + Mineflayer 4.39.0 在 1.20.1 的寻路/挖掘/放置/农田/箱子/死亡重生/取消（20 Hz 从未声称） | 未做 |

> L1–L8 **一项都不算已验证**。离线绿不能替代实机，也不批准任何真实世界兼容性。

## 5. 上一轮独立复核（`final-2026-11-01.md`）发现的当前状态

> 这一节来自 2026-11-01 上一轮独立复核（`reports/minecraft-review/final-2026-11-01.md`）的发现。
> 「最终复核」列是 2026-11-02 最终独立复核（`final-verification-2026-11-02.md`）的结论：除明确写成实机项的以外，均已关闭。

| 发现 | 当前源码状态 | 现有离线证据 | 最终复核 |
| --- | --- | --- | --- |
| M1 撤销完成后仍可能签发排队中的 connect | 已加撤销代次 `revocationGeneration`：排队/构建/签名/发布都校验，发布与撤销串行化；`SnapshotGate.commit` 拒绝已撤销/过期 ticket | `GuardSelfTest` 的 queued connect/backup/write、撤销期间快照用例；E13 100 轮 `late200=0` | 已复验：E13；源码抽查通过 |
| M2 无居民时 `invalid_guard_settings` 被误判成功 | 已改为：只有**完全没有** `PAWBOT_MC_GUARD_SECRET` 才是 `ok:false` 的诚实 noop（`revoke_skipped="no_guard_configured"`）；有 secret 但端口/路径/actor 不合法时返回失败 | `test_minecraft_integration` 的 no-resident/no-secret、bad-settings、stale-endpoint、port-0 用例；`probe-emergency-stop-final.log` 第一次 `ok:false` | 已复验：E1 两次调用都 `ok:false`；G4 边界已修 |
| M3 `home` 从未被 `set_home` seed | 已实现：首次验证观测后自动 `set_home`（observe 许可）；距持久化 home >3 格时先 move；失败 3 次/600 秒冷却；重连重置并重 seed；未 seed 时 `home` 不进入可选技能 | `test_home_rejected_until_set_home_seeds_persisted_home`、`test_home_seed_failure_is_never_reported_as_success`、`test_reconnect_loses_sidecar_home_and_reseeds_before_home`、`test_home_is_not_seeded_while_far_from_the_persisted_home` | 离线已复验；实机 home 路径/寻路未验证（L1/L6） |
| M4 农田用玩家脚下当土壤坐标 | 只使用观测到的 `y-1` 土壤（farmland/dirt/grass_block），跳过已有作物列；没有观测到土壤就不生成 farm 步骤，项目回退 `shelter`（V2/E2） | 农田契约用例 + `probe-farm-reach-final.log` | 已修：E3 `4.123/4.123 ≤ 4.5` |
| M5 没有主人人类身份 | 已新增可选 `minecraft.owner_human_uuid`：精确 UUID、必须不同于 bot actor；owner 角色只由该字段决定；`owner_present` 只在观测到该 UUID 时为真；`owner_mappings` 只记录跨渠道链接，不提升角色 | resident 的 owner 精确 UUID、actor 冲突、mapping 不授权、重启保持用例 | 离线已复验（G8 已统一）；真实游戏内 owner 行为未验证 |
| S1 revoke 回执 `port:0` 被客户端拒绝 | 已修：`revoke` 接受 `0` 或当前 1024–65535；其他动作仍要求精确当前端口 | guard/control 的 port-0/changed-port 用例 | 离线已复验；实机 LAN 关闭场景演练未做 |
| S2 无居民直连 revoke 忽略配置 `guard_url` | 已修：`core._minecraft_guard_settings` 用环境 > `minecraft.guard_url` > 默认；CLI 直连仍只读环境变量（这是两条路径） | `test_no_resident_guard_url_prefers_env_then_config` | 已复验；文档已注明差异 |
| S3 Windows 大小写路径通过校验却在每次许可失败 | 已修：`GuardSettings.validate` 要求 `realpath(world_path) == world_path` 的**精确规范拼写**；大小写/别名在启动时就是 `invalid_guard_settings`，不再拖到每次许可 | guard 的路径用例；文档 §10 | 离线已复验；真实 Windows 存档路径演练未做 |
| S4 RestoreTool 原子移动可覆盖竞态 | 已修：发布改为 `Files.createDirectory(target)`（已存在即失败），再逐项搬入；目标在复制期间出现时拒绝；清理不跟随 junction | `restoreTargetRace`、`restoreDeepRunningWorld`、junction 清理用例 | 已复验：E13 `ALL PROBES PASSED`；jar 级 smoke 与实机演练仍未做（L5） |
| F3/F3b/HttpGuard-S4 快照忙时误杀/多备份/慢 body 占满 worker | 已实现：`SnapshotGate` 单 ticket、签名 busy 回执（16 字段、非变更）、`-32008` 有界暂停、Node/居民去掉重复周期备份、`HttpGuard` 先鉴权后读 body + 2 s 读超时 + 3 worker、revoke 不排在快照后 | Java 63 项（含 busy/HTTP 用例）、Node 115 项、Python 201 项 | 离线已复验（E7/E9/E14）；真实大存档复制耗时属 L1/L4 |

## 6. 需求覆盖矩阵

状态词见第 1 节。测试证据统一指第 2 节；「离线测试」不等于实机。

### A. 授权与边界（v4 第〇、七、八节）

| 需求 | 状态 | 依据 |
| --- | --- | --- |
| 默认关闭、不写世界 | 离线测试 | `config.DEFAULT.minecraft` 4 个开关默认关；policy/resident 默认态用例 |
| 专用副本 + 集成服务器限定 | 离线测试（合成世界） | `PermitService`/`GuardMod`/`BackupManager` + 63 项 Java 自测；真实客户端加载属 L1 |
| 许可签名、重放缓存、5 秒时效、未来漂移 | 离线测试 | Python guard 测试与 Java `hmac`/`replay`/time 用例；E5 |
| 世界写入与 PvP 分离、PvP 精确名单 | 离线测试 | `authorize_action`、`allowed_pvp_targets`；Java `pvp needs exact allowlisted target`、`pvp requires write`；守卫上限 16 |
| 备份：连接前一次、之后守卫定时、保留 7 | 离线测试 | Java `connect saves then backs up and excludes session.lock`、`write/pvp need existing backup`、freshness 1800 边界、retention 用例；E9/E5 |
| 离线恢复 | 源码 + Java 自测；当前 jar 未重跑 smoke，未演练 | `RestoreTool` + 6 条 restore 用例（含 no-replace/deep ancestor/junction）；L5 |
| 紧急停止（宿主执行、不经模型） | 离线测试（诚实失败语义已复验） | control/integration 用例覆盖诚实失败、noop、merge；E1 反例保持 `ok:false`；Java `revoke is not acknowledged when the marker cannot be persisted` |
| 看门狗 ≤1 秒 | 源码 + 侧车测试（作者口径） | 侧车 250 ms 间隔 / 650 ms 超时；busy 时保持连接；真实卡顿属 L1/L2 |
| 快照忙不误判为守卫离线 | 离线测试 | 签名 busy 16 字段、非变更、`-32008` 暂停、无重放；Java/Node/Python 三侧用例 |

### B. 侧车与技能（v4 第二节）

| 需求 | 状态 | 依据 |
| --- | --- | --- |
| stdio JSON-RPC、行上限、静态错误 | 离线测试 | E7 的 115 项；`stdio.test.js` |
| 17 个技能与 schema | 离线测试（假 bot、合成世界状态） | `skills.js`/`adapter.js`；真实 Mineflayer 行为属 L1/L2 |
| 取消、超时、隔离、不重放 | 离线测试 | `sidecar.test.js`/`adapter.test.js`；Python `test_minecraft_sidecar` |
| 有界观测、天气/时间/光/屋顶 | 离线测试 | `adapter.test.js` 的 derived facts；未知值省略；真实游戏值属 L1/L2 |
| `home` 依赖 `set_home`、`set_home` 只读 | 离线测试 | adapter/controller 的 observe 许可；resident seed 用例；真实 home 路径未验证 |
| 农田坐标是土壤方块 | 离线测试 | 真实 adapter 用例 + Python `_farm_soil`；回退站位已收敛（E3：`4.123/4.123 ≤ 4.5`） |
| Microsoft 登录仅人工 | 离线测试（拒绝非交互、缓存边界） | `auth.mjs` 要求 TTY + 1 个参数 + 180 s 超时；真实登录属 L3 |
| 控制频率（不宣称 20 Hz） | 源码 + 测试测量间隔 | 变更类动作准入 ≥125 ms（≤8 次/秒）；物理由 Mineflayer 控制 |
| 版本/插件兼容性 | 实机未验证 | 直接依赖精确锁定（mineflayer 4.39.0 / pathfinder 2.4.5 / pvp 1.3.2 / prismarine-auth 3.1.1 / vec3 0.1.10） |

### C. Python 宿主（v4 第二、九节）

| 需求 | 状态 | 依据 |
| --- | --- | --- |
| 侧车监督、有界请求、超时后不重放 | 离线测试 | `test_minecraft_sidecar`；busy 以 `SidecarError.code == -32008` 传回 |
| 重连退避、只重放 connect | 离线测试 | `connect_with_retry` + `test_minecraft_sidecar`；居民指数退避 |
| 主进程不因侧车异常退出 | 离线测试 | `test_minecraft_integration` |
| HA 中继不启动居民 | 离线测试 | `test_relay_mode_never_starts_a_resident` |
| 数据路径只写 `content/minecraft`、`state/minecraft` | 离线测试 | `paths.py` + `test_data_layout`；避免旧路径迁移 |
| 错误消息不含密钥/路径/私有文本 | 源码 + 测试（静态面） | `Decision.safe_log`、静态异常类型；不等于完整渗透测试 |

### D. 同一人格与记忆（v4 第三节）

| 需求 | 状态 | 依据 |
| --- | --- | --- |
| 共用 `bot.persona` / `bot.emotion` / `bot.longmem` | 离线测试 | 居民 `_context`/`_appraise`/`_record_episode`；resident 用例 |
| 记忆带 `source`、QQ 与 Minecraft 分组 | 源码 + 全量套件中的 longmem | `longmem.py`；E6 |
| 主人数值 UID 与玩家 UUID 显式绑定、不猜 | 离线测试 | `owner_identity_matches`/`uid_of`；`owner_qq_uid` 只认完整数字 |
| 主人人类 UUID 可选、与 bot actor 分离 | 离线测试 | 可选 `owner_human_uuid`；精确 UUID、不同于 actor；owner 角色/owner_present 用例；`owner_mappings` 冲突由同一 `owner_human_identity` 失败关闭（G8 已修）；真实身份未配置（L3/L6） |
| 其他玩家仅显式映射 | 离线测试 | `owner_mappings` 只做跨渠道链接，不提升 owner 角色 |

### E. 心智模型与决策（v4 第四至六节）

| 需求 | 状态 | 依据（均在 `minecraft_resident.py`） |
| --- | --- | --- |
| 自我模型（叙事、关注、身体、社会、元认知） | 源码 + 离线测试 | `self_narrative`、`projects`、`players`、`affect_residue`、`confidence` |
| 全局工作空间 3–5 项 | 离线测试 | `GlobalWorkspace`（默认 4，读取持久状态容量） |
| 情绪 appraisal / 残留 / 衰减 / 再激活 | 离线测试 | `Appraiser`；`residue` 半衰期 600 s；天气/屋顶会影响 appraisal |
| 稳定人格 + 动态驱动 | 离线测试 | 10 个人格维度、7 个驱动；缓慢漂移 |
| 自传体记忆、日记 | 源码 + 离线测试 | `_record_episode`（≥60 s 节流）、`journal`、按天 markdown（256 KiB 上限） |
| 社会认知（熟悉/信任/亲近/边界） | 离线测试 | `_update_social`；owner/self/player 角色基线；名字不参与身份 |
| 习惯与仪式 | 离线测试 | `habits`；语义键 `return_home`/`tend_farm`/`greet_known` 由成功动作形成（阈值 ≥2，E4 为 3/3/3）；`return_home` 读游戏时间；下雨/无屋顶拉 `rest` |
| ImpulseEngine / Arbiter（犹豫、延迟、改变主意） | 离线测试 | `ImpulseEngine`、`Arbiter.resolve` |
| 决策接口 + JEV 适配 + 规则回退 | 离线测试 | `JevAdapter`（intent-only、预算先扣、失败不退款）、`DeterministicPlanner` |
| 频率与成本限制 | 离线测试 | `DecisionBudget`、`decision_interval_sec`、`max_decisions_per_day` |
| 防循环与失败检测 | 离线测试 | `_repeat_blocked`、`_failure_streak`、`_record_failure`；`guard_busy` 不记失败 |
| 死亡/断线/寻路失败恢复 | 源码 + 离线测试 | `_reconnect`、`recover` 意图；真实死亡/断线属 L1/L2 |
| 结构化决策/执行日志 | 离线测试 | `Decision.safe_log()` 与 `Minecraft action`；不含私密独白/路径 |
| 天气/时间/屋顶影响习惯 | 离线测试 | `game_clock` 只用 `time_of_day`；rain/roof 用例；真实值属 L1/L2 |
| 家：自动 seed/reseed | 离线测试 | `_ensure_home_seed`；observe 许可；3 格容差/3 次/600 s；重连重置 |
| 农田：土壤坐标 | 离线测试 | `_farm_soil`；E3（`4.123/4.123 ≤ 4.5`，超限土被丢弃）；无土回退 `shelter` 由 E2 覆盖 |

### F. 验收（v4 第九、十一节）

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| mock/加速 60 分钟行为验收 | 离线测试 | resident 的合成时钟用例；不是实机 |
| 真实 Open-to-LAN 连接并记录版本/端口/认证 | 实机未验证 | L1/L2 |
| 微软与离线模式分别实测 | 实机未验证 | L3 |
| 专用副本内 30–60 分钟观察 + 自动日志 | 实机未验证 | L6 |
| 重启后仍记得地点/项目/承诺/关系 | 离线测试（重载断言） | resident 用例；真实重启属 L6 |
| JEV mock↔真实切换 | 离线测试（mock/回退）+ 实机未验证 | 真实接口属 L7 |
| 改人格权重后行为分布变化 | 源码（权重进入打分） | 没有统计显著性证据 |
| 死亡/断线/端口变化不崩主进程、不重复破坏 | 离线测试（隔离与不重放） | 真实故障注入属 L1/L2 |
| 非许可世界/未知身份无法触发写入 | 离线测试 | policy + guard 双重校验；实机未验证 |
| 无模型生成代码执行/凭证泄漏/越权 | 源码 + 测试（静态面） | 技能白名单、环境白名单、无 eval/exec；不等于完整渗透测试 |

## 7. 开工前仍需主人确认（v4 第十二节）

文档不替主人作答。以下各项当前一律按最保守默认处理，主人答复前不会自行放宽：

1. 精确 Minecraft 版本（当前唯一支持 1.20.1）、启动器/模组情况、由哪台主机开 LAN。
2. 微软正版账号状态；若考虑离线模式，目标 LAN 的实际认证设置。**不要在对话里贴密码/token。**
3. 账号别名、完整数字 QQ UID、机器人 actor UUID；以及**可选**的主人人类 UUID（`owner_human_uuid`）。
   只写本机配置；不得进源码、测试夹具或公开文档。
4. 专用副本的目录隔离、备份频率/保留、恢复演练与紧急停止方式；确认原存档绝不作写入目标。
5. PvP 政策：是否启用、允许对象、是否逐次同意、触发条件、默认状态、随时撤销方式（答复前保持关闭）。
6. 恶作剧/破坏的额外限制：珍贵物品/地形保护清单、是否要求可逆。
7. QQ 自主发言与跨渠道记忆边界：消息类型、静默时段、私聊/群聊范围、删除与保留策略。
8. 真实 JEV 接口、认证、可用性与成本/频率预算（未答复时只用 mock/规则回退）。

## 8. 文件清单（只列 Minecraft 范围；以 `git status` 为准）

> 工作树里还有大量框架批次/其它业务的既有改动（例如 `app/pawbot/agent.py`、`album.py`、`webui.py` 之外的
> 多个文件）。它们**不属于**本集成的变更清单，下面不逐条列出，也不应被误算成本次交付。

新增（未跟踪）：

- `app/minecraft-sidecar/**`：`.gitignore`、`README.md`、`auth.mjs`、`index.mjs`、`package.json`、`package-lock.json`、
  `src/{adapter,auth,controller,guard,main,skills}.js`、`test/{adapter,auth,helpers,sidecar,stdio}.test.js`。
- `minecraft-guard/**`：`.gitignore`、`README.md`、`build.gradle`、`gradle.properties`、`settings.gradle`、
  `config.example.json`、`src/main/java/dev/pawbot/guard/*.java`（14 个源文件）、
  `src/main/resources/fabric.mod.json`、`src/test/java/dev/pawbot/guard/*.java`（4 个测试文件）。
- `app/pawbot/minecraft_{guard,policy,sidecar,resident,control}.py`、`app/pawbot/paths.py`。
- `app/tools/test_minecraft_{guard,policy,sidecar,integration,resident,control}.py`。
- `scripts/architecture-baseline.mjs`、`test/architecture-baseline.mjs`。
- `docs/MINECRAFT-{PROTOCOL,RESIDENT,ACCEPTANCE}.md`、`docs/decisions-minecraft-resident.md`、
  `docs/ARCHITECTURE-BASELINE.md`（本文件与基线文档是本次文档刷新的一部分）。
- `reports/minecraft-review/**`：复核/实现/探针/日志证据（不进入产品运行路径）。

修改（Minecraft 相关）：

- `app/pawbot/config.py`：新增 `minecraft` 段与整段默认值（含 `owner_human_uuid`、`game_chat_enabled`、
  `backup_interval_min` 兼容键等）。
- `app/pawbot/core.py`：居民启动/停止挂钩、`minecraft_emergency_stop`、`_minecraft_guard_settings`。
- `app/pawbot/webui.py`：`/api/control/minecraft-stop` 控制路由。
- `app/pawbot/longmem.py`：可选 `source` 字段（Minecraft episode）。
- `app/run.py`：数据根/生命周期接线（Minecraft 相关部分）。
- `.gitignore`：忽略 Minecraft 构建产物、运行态配置与依赖目录。

不入库：`minecraft-guard/.tooling/`、`minecraft-guard/build/`、`config/pawbot-guard.json`、
`app/minecraft-sidecar/node_modules/`。

## 9. 结论

- **离线工程 READY（READY OFFLINE，2026-11-02 最终独立复核）。** 第 2 节的 E1–E14（另含 E6b/E7b 两个补充行）全部 exit 0，且都来自同一份快照：
  聚焦 201 / 43.540 s、全量 2179 / 288.260 s（2 个与本集成无关的 skip）、侧车 115、守卫自测 63、基线 19/19 + 46/0/0。
  报告：[final-verification-2026-11-02.md](../reports/minecraft-review/final-verification-2026-11-02.md)。
- 上一轮的 must-fix（M1–M5、S1–S4、F1–F6、V1、V2）与 G3/G4/G6/G7/G8 在当前源码上**都不再复现**；
  已修复项见第 3 节，不需要主人做任何动作。
- 仍开放的是**限制**而不是待修缺陷：G1（`backup_retention`/`interest` 无读取者）、G2（401 合并进
  `guard_unavailable`）、G5（周期快照仍在服务器线程内联复制，大存档耗时未测）；另有两项低风险残余 R1/R2（第 2 节）。
- 实机侧 **L1–L8 全部未做**；任何 mock/离线结果都不能替代，「READY OFFLINE」不等于 acceptance complete，也不批准任何真实世界兼容性。
