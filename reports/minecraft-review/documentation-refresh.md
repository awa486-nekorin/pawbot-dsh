# Minecraft 主人文档刷新（2026-11-01 23:05 快照）

> 角色：只做文档刷新的子代理；不持有源码/协议/测试/AGENTS 的写权限。
> 写入范围**严格**限定为：`docs/MINECRAFT-RESIDENT.md`、`docs/MINECRAFT-ACCEPTANCE.md`、
> `docs/ARCHITECTURE-BASELINE.md`、`docs/decisions-minecraft-resident.md`、本文件。
> 未修改 `docs/MINECRAFT-PROTOCOL.md`、`app/minecraft-sidecar/README.md`、`minecraft-guard/README.md`、
> 任何源码、测试、配置、AGENTS，也未启动游戏/浏览器/部署/服务重启，未读取运行态人设、记忆、凭证或原存档。

## 1. 目标与边界

本次只做一件事：把主人文档对齐到**当前已实现接口**，并明确区分：

- 已完成的离线证据（带时间戳，且不把不同运行混成同一快照）；
- 源码缺口（编码方还能补，且不能降级写成「等主人」）；
- 实机门槛（只能由主人在真实 LAN/微软账号/专用副本里完成）；
- 最终独立复核仍缺失（PENDING FINAL INDEPENDENT SNAPSHOT）。

结论先行：**不是 READY，不是 acceptance complete。** 最终独立复核报告尚未产出；当前源码还有两个必须正视的缺口
（G3 农田可达边界、G4 撤销持久化假成功），另有 owner-human 判定双实现（G8）、协议文档过期（G6）与测试快照口径问题（G7）。

## 2. 读取的输入（只读）

- `AGENTS.md`（本仓库开发约束）。
- 桌面 v4 需求提示词：`不可用提示詞-修改稿.txt`（Minecraft 居民需求；本次通过验收文档既有引用与源码对照使用，
  未复制任何真实身份/凭证）。
- `reports/minecraft-review/docs-finalization-checklist.md`（上一轮文档缺口清单）。
- 当前源码与文档：
  - `app/pawbot/config.py`（`minecraft` 默认段、33 键）
  - `app/pawbot/minecraft_guard.py`、`minecraft_policy.py`、`minecraft_sidecar.py`、
    `minecraft_resident.py`、`minecraft_control.py`
  - `app/pawbot/core.py`（居民启动/停止、`minecraft_emergency_stop`、guard URL 优先级）
  - `app/pawbot/webui.py`（`/api/control/minecraft-stop`）
  - `minecraft-guard/src/main/java/dev/pawbot/guard/*.java`（`GuardConfig`、`PermitService`、`SnapshotGate`、
    `BackupSchedule`、`HttpGuard`、`GuardMod`、`RestoreTool`）
  - `app/minecraft-sidecar/src/{controller,guard,adapter,skills,main}.js`
  - `scripts/architecture-baseline.mjs`、`test/architecture-baseline.mjs`
  - `docs/MINECRAFT-PROTOCOL.md`（仅读取；未改）
- 已完成日志：`reports/minecraft-review/backup-watchdog-*.log`、`reports/minecraft-review/repaired/*-final*.log`。
- 失败/边界探针：`reports/minecraft-review/probe/{revoke_durability_false_success.py,farm_stand_reach_probe.py}`。

## 3. 实际修改内容

### 3.1 `docs/MINECRAFT-RESIDENT.md`

- 头部改为「按 2026-11-01 23:05 工作树刷新」；明确最终独立复核 PENDING，不写成定稿/已验收。
- 工具链与产物：保留本机 JBR 21 + `.tooling` Gradle 8.7 事实；把自测数从旧 37 改为 **63**，产物改为当前
  **70,856 B / SHA-256 B373…**；保留「`gradle test` 故意禁用，不能当覆盖证据」。
- 备份：明确**守卫是唯一调度方**（`backup_interval_sec` 60–1800，默认 1800）；Node/居民不再有周期备份计时器；
  `backup_interval_min` 是旧兼容键、只被未使用的辅助函数读取、不参与调度；连接只做一次快照。
- 主人身份：新增 `owner_human_uuid`（可选、精确 UUID、必须不同于 bot actor）；明确 `owner_player_uuid` 是 bot actor，
  `owner_qq_uid` 只接受完整数字 UID，`owner_mappings` 不会提升 owner 角色；示例全部是占位符。
- 路径/上限：强调守卫最终上限（`world_id` 64、`secret` 32–256、PvP 16）与**精确规范路径/大小写**；
  大小写/别名错误在启动时落 `invalid_guard_settings`，不是每次许可才失败。
- 家/农田/环境：新增 §9.4，写明自动 `set_home` seed/reseed（observe 许可、≤3 格、失败 3 次/600 秒、重连重置）、
  农田土壤方块坐标、天气/游戏内时间/屋顶习惯；明确「未 seed 时 `home` 不进入可选技能」。
- 排障：新增 `guard_busy`（不是失败、不重放）、`control_disabled`；更新 `invalid_guard_settings`、
  `guard_world_mismatch`、`guard_unavailable`（401 仍被合并）。
- 紧急停止：更新「无居民时只有完全没有 secret 才是诚实 noop」「revoke 接受 port 0/当前 LAN 端口」
  「控制面尊重 `minecraft.guard_url`，CLI 直连只读环境变量」；同时明确记录 G4 的 `409 already revoked`
  持久化假成功风险，不把 `ok:true` 写成重启后仍撤销的证明。
- 回滚/兼容：基线现在整段冻结 `minecraft` 33 键；`backup_retention`/`interest` 无读取者。

### 3.2 `docs/MINECRAFT-ACCEPTANCE.md`

- 重写第 2 节为 **PENDING FINAL INDEPENDENT SNAPSHOT** 表：逐条给已完成命令/日志、最后已知结果、时间、快照限制；
  明确 E3（185，23:05）与 E4（2161，23:02）**不是同一快照**，E4 早于最后 Python 改动。
- 证据表加入：守卫 63/63、jar 70,856 B/hash；Node 115/0；基线 19/19 + 46/0/0；撤销压力 100 轮；
  撤销持久化探针 `FALSE_SUCCESS`；农田可达探针 `OUT_OF_REACH`；最终独立复核缺失。
- 重写源码缺口：G1 无读取者键、G2 401 合并、G3 农田 4.583>4.5、G4 撤销持久化假成功、G5 快照卡 tick、
  G6 协议 §4 过期、G7 测试快照不一致、G8 owner-human 判定双实现。
- 更新上一轮 M1–M5/S1–S4 的当前状态表；只写「源码/现有离线证据」，不写「独立复核已通过」。
- 更新需求覆盖矩阵：家/农田/天气时间屋顶/owner_human_uuid/忙回执/单备份调度；保留 L1–L7 实机未验证。
- 文件清单只列 Minecraft 范围；明确工作树里其它框架/业务脏改不属于本集成。
- 结论：不是 READY；G3/G4 是必须先修的源码缺口，G8 是 owner 判定一致性缺口，G6 交协议作者。

### 3.3 `docs/ARCHITECTURE-BASELINE.md`

- 复核证据改为 2026-11-01 23:02:24 的 19/19 与 23:02:33 的 46/0/0，并注明最终独立复核仍未签字。
- `minecraft` 段从「只冻结一组关键默认值」改为**整段 33 键冻结**；列出键集合并说明
  `backup_retention`/`interest` 虽冻结但无读取者、`backup_interval_min` 只作兼容提示。
- 检查项 14、测试覆盖（43→46）与新增负例（新增/删除键、默认值漂移、集合非空）同步更新。
- 已知边界补一句：整段冻结只证明键集合/默认值不漂移，不证明每个键都有行为读取者。

### 3.4 `docs/decisions-minecraft-resident.md`

- 状态改为「源码与离线测试已推进；最终独立复核未产出，实机未连接」。
- 决策新增/更新：单一备份调度、撤销代次与 sticky、诚实紧急停止、`owner_human_uuid` 可选且不推断、
  `set_home` 自动 seed、busy 是暂停不是拒绝、RestoreTool 不覆盖。
- 「当前实现」改为按当前源码事实描述；证据数注明年份/时间与「不是同一冻结窗口」。
- 「已知偏差」保留 G1/G2/G3/G4/G5/G6/G7，明确 G3/G4 是源码缺口，不能降级成实机门槛。

## 4. 本次使用的已完成证据（只列完成日志/探针）

| 证据 | 结果 | 时间 | 备注 |
| --- | --- | --- | --- |
| `backup-watchdog-guard-build.log` | `selfTest passed=63 failed=0`；`BUILD SUCCESSFUL`；`remapJar` | 2026-11-01 22:50 | 产物 70,856 B，SHA-256 `B373B470…A82F`（22:53） |
| `backup-watchdog-sidecar-npm-test.log` | 115 pass / 0 fail / 0 skip | 22:57 | 最新 Node 源码 |
| `backup-watchdog-sidecar-npm-audit.log` | 0 vulnerabilities | 22:57 | 依赖面 |
| `backup-watchdog-python-minecraft.log` + `repaired/focused-minecraft-final3.log` | 185 tests, OK | 23:04 / 23:05:45 | 最新 Python 源码 |
| `repaired/full-unittest-final.log` | 2161 tests, OK (skipped=2) | 23:02:11 | **早于 23:03 最后 Python 改动**，不作为最新全量证据 |
| `repaired/architecture-baseline-final.log` | 19/19 PASS | 23:02:24 | 基线检查器 |
| `repaired/architecture-baseline-tests-final.log` | 46 passed, 0 failed, 0 skipped | 23:02:33 | 基线离线测试 |
| `repaired/revoke-race-stress-final.log` | 100 轮，`late200=0` | 22:53 | 合成世界 |
| `probe/revoke_durability_false_success.py` | `FALSE_SUCCESS`，exit 1 | 本次 23:07 复跑 | 失败证据，见 G4 |
| `probe/farm_stand_reach_probe.py` | `OUT_OF_REACH 4.583 > 4.5`，exit 1 | 本次 23:07 复跑 | 失败证据，见 G3 |

未使用：仍在写入的日志（如 0 字节的 `backup-watchdog-sidecar-syntax.log`、`lifecycle-focused-final2.log`）；
未运行中的复核报告；未重跑当前 jar 的恢复 smoke。
并发工作流状态：第一轮 repair/review 的最新重跑在本次写入时仍在进行（`lifecycle-focused-final2.log` 为空；
`focused-minecraft-final3.log` 已是完成的 185 OK）；第二轮 backup-watchdog 的实现说明在 23:04 更新、Python 聚焦
日志在 23:04 完成。本文只取**已写出最终 `Ran/OK` 或明确结果**的日志，不等待、不轮询其它工作流。

## 5. 当前必须保留的精确未解决项

1. **G3 农田可达边界（源码缺口）**
   - 位置：`app/pawbot/minecraft_resident.py:3027-3041`（`_farm_soil`/`_project_steps`）。
   - 行为：回退 `y-1` 时站位取 (2,2) 偏移，最远土壤 4.583 格 > 侧车 `farm` 4.5 格；探针 exit 1。
   - 归属：居民作者；不能写成「已修复」或「等主人」。

2. **G4 撤销持久化假成功（源码缺口，安全相关）**
   - 位置：`app/pawbot/core.py:453-460`（把 `409 revoked` 当已确认）、`:504-511`（停机后二次确认）。
   - 行为：守卫无法落盘时第一次 revoke 返回无签名 503；第二次 409 仅证明内存闩锁，宿主仍可能 `ok:true`。
   - 探针：`reports/minecraft-review/probe/revoke_durability_false_success.py`，2026-11-01 23:07 复跑
     `FALSE_SUCCESS`，exit 1。
   - 归属：Core/集成作者 + 守卫作者（需要可验证的持久化语义）；不能降级成实机门槛。

3. **G6 协议文档过期（文档问题，协议作者维护）**
   - 位置：`docs/MINECRAFT-PROTOCOL.md` §4，约 `:259-274`。
   - 过期内容：`PAWBOT_MC_LAN_PORT` 的 0 被写成 `invalid_guard_settings`；无居民 + 设置不完整时返回
     `ok:true` + `revoke_skipped:"not_configured"`。
   - 当前源码：revoke 接受 0/当前端口；只有完全没有 secret 才是 `ok:false` 的诚实 noop。
   - 本次未改协议文件；主人手册以当前源码为准。

4. **G7 测试快照口径（证据问题）**
   - 最新全量 2161（23:02:11）早于 23:03 的 `minecraft_resident.py`/`minecraft_sidecar.py` 最后改动；
     最新聚焦 185（23:05:45）覆盖最新源码，但不是全量。
   - 最终独立复核必须重跑并给唯一一份数字表，不能把 2161 与 185 混成「同一快照全绿」。

5. **G8 owner-human 判定双实现（源码不一致）**
   - 位置：`app/pawbot/minecraft_policy.py:53-90` 的 `owner_human_identity` 与
     `app/pawbot/minecraft_resident.py:1574-1589` 的 `_owner_human_uuid`。
   - 行为：policy 会拒绝 `owner_human_uuid` 与 `owner_mappings` 指向不同 QQ UID 的冲突；居民只检查精确 UUID
     且不等于 actor，没有复用该冲突检查。需要统一判定，或明确 `owner_human_uuid` 是唯一权威并删掉 policy 的冲突分支。
   - 归属：居民/集成作者；不能把 `owner_mappings` 写成 owner 角色的第二重校验。

6. **最终独立复核缺失**
   - `reports/minecraft-review/repaired-2026-11-01.md` 在本次写入时仍不存在（只有 `repaired/` 下的日志）。
   - 在它出现且通过前，任何文档都不得写 READY / acceptance complete。

## 6. 文件清单（只限 Minecraft 范围，按当前 `git status`）

新增/未跟踪（Minecraft 集成）：

- `app/minecraft-sidecar/**`（README、auth、index、package*、`src/*.js`、`test/*.test.js`）
- `minecraft-guard/**`（构建脚本、README、config.example、`src/main/java/dev/pawbot/guard/*.java`、
  `src/main/resources/fabric.mod.json`、`src/test/java/dev/pawbot/guard/*.java`）
- `app/pawbot/minecraft_{guard,policy,sidecar,resident,control}.py`、`app/pawbot/paths.py`
- `app/tools/test_minecraft_{guard,policy,sidecar,integration,resident,control}.py`
- `scripts/architecture-baseline.mjs`、`test/architecture-baseline.mjs`
- `docs/MINECRAFT-{PROTOCOL,RESIDENT,ACCEPTANCE}.md`、`docs/decisions-minecraft-resident.md`、
  `docs/ARCHITECTURE-BASELINE.md`
- `reports/minecraft-review/**`（本报告与证据）

修改（Minecraft 相关）：

- `app/pawbot/config.py`、`app/pawbot/core.py`、`app/pawbot/webui.py`、`app/pawbot/longmem.py`、`app/run.py`、
  `.gitignore`。

明确排除：工作树中其它已修改的框架/业务文件（`app/pawbot/agent.py`、`album.py`、`bili_*`、`mem/*` 等）
不属于本集成清单；不要把 `git status` 的全部脏改算成本次交付。

## 7. 最终定稿补丁清单（等最终独立复核后由 Lead 派一个很小的证据更新）

前提：`reports/minecraft-review/` 下出现独立复核的最终报告，且明确给出同一次源码快照的 verdict；否则不得执行。

1. **只改证据表**：把 `docs/MINECRAFT-ACCEPTANCE.md` §2 的 PENDING 表替换为最终快照的唯一一张表；每行给
   命令、日期/时间、退出码、数量、日志路径、跳过项名称；保留「PENDING」直到报告签字。
2. **同步头部**：四份文档头部从「最终独立复核未产出」改为最终报告路径 + 日期；不要改需求/缺口结论。
3. **核对数字**：用最终报告的数字替换 E1/E2/E3/E5/E5b；若自测数不再是 63、Node 不再是 115、基线不再是 46/19，
   同步 `MINECRAFT-RESIDENT.md` §4.1 与 `ARCHITECTURE-BASELINE.md` 的验证段。
4. **关闭/保留缺口**：若 G3/G4/G6/G8 已被修复，按修复提交更新 G 表与 RESIDENT §5/§9.4/§11/§15；
   若仍未修，保持缺口并保留探针路径/退出码，不降级成实机门槛。G8 若选择「`owner_human_uuid` 唯一权威」，
   要同步删掉 `minecraft_policy.owner_human_identity` 的冲突分支或明确它只用于别的调用点。
5. **更新 G7**：若最终报告重跑了最新全量并给出数字，替换 E4 并删除「E4 早于最后 Python 改动」的限制。
6. **恢复 smoke**：若最终报告对当前 70,856 B jar 做了恢复 smoke，把 E9 从「未重跑」改为实际结果；否则保留。
7. **协议 G6**：等协议作者更新 `docs/MINECRAFT-PROTOCOL.md` §4 后，删除 RESIDENT §11/§15 与 DECISIONS 的 G6 注记，
   并同步行号；**不要**由文档代理直接改协议。
8. **不要**在这次小补丁里改源码、测试、AGENTS、sidecar/guard README；源码缺口归对应作者。

## 8. 未执行/不能执行

- 没有运行中的游戏、LAN、微软登录、真实存档、浏览器、部署或服务重启。
- 没有重跑全量/聚焦套件（只复跑了两个离线探针）；没有改任何源码或测试。
- 没有读取或写入运行态 `app/data`、人设、记忆、真实凭证。
- 没有把仍在写入的日志（0 字节/未完成）当通过证据。
