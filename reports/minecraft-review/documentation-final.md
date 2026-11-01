# Minecraft 文档定稿（2026-11-02 最终独立复核后）

> 角色：只做文档定稿的证据更新；不持有源码/测试/配置/AGENTS/sidecar/guard README 的写入意图。
> 写入范围：`docs/MINECRAFT-RESIDENT.md`、`docs/MINECRAFT-ACCEPTANCE.md`、`docs/ARCHITECTURE-BASELINE.md`、
> `docs/decisions-minecraft-resident.md`、`docs/MINECRAFT-PROTOCOL.md` §4、本文件。
> 未改任何源码、测试、配置、AGENTS、sidecar/guard README、其它文档，未启动游戏/浏览器/部署，
> 未读取运行态人设、记忆、真实凭证或原存档。

## 1. 前提

- 最终独立复核报告 `reports/minecraft-review/final-verification-2026-11-02.md` 存在且 verdict 为
  **READY OFFLINE**（不是 NOT READY），因此按定稿清单执行。
- 快照：2026-11-02 00:16:40 – 00:30:20 +08:00，HEAD `3dff9ef`，`git status --porcelain` 206 条；
  最后一次 Minecraft 源码写入是 00:06:39（`minecraft_resident.py`）与 00:07:54（`test_minecraft_resident.py`）。
- 依据：`reports/minecraft-review/documentation-refresh.md` §7（定稿补丁清单）、最终报告 §2/§3/§7/§9、
  修复日志 `fix-farm.log`、`fix-farm-fallback.log`、`fix-revoke.log`、`fix-identity.log`、
  `fix-habit-formation.log`、`fix-protocol.log`，以及 `app/pawbot/core.py`、`minecraft_control.py` 的当前源码。

## 2. 实际修改

### 2.1 `docs/MINECRAFT-ACCEPTANCE.md`

- 头部改为「验收证据定稿，2026-11-02 00:16–00:30 最终独立复核快照」并链接最终报告（READY OFFLINE），
  明确 READY OFFLINE 只覆盖离线工程。
- **§2 整节重写**：删除 `PENDING FINAL INDEPENDENT SNAPSHOT` 表与旧的多快照限制说明，替换为**唯一一张**
  最终快照证据表（E1–E14 + E6b/E7b）。每行给命令、完成时间、退出码、数量/失败/跳过、日志路径；
  E6b 单独给出两个 skip 的完整用例名与原因；补齐 `:test SKIPPED` 不是跳过测试、jar 级恢复 smoke 仍未重跑、
  R1/R2 低风险残余等注记；并把 `verified-*.log`（23:45–23:53）与仅表头的
  `final-verify/04-focused-minecraft-python.log`（00:02）明确标为不采用。
- **§3 重写**：从「源码缺口」改为「源码项：已修复 / 仍开放的限制」，新增「状态」列。G3、G4、G6、G7、G8
  以及新增的 V1（语义习惯）、V2（农田 shelter 回退）标为**已修复/已关闭**并引用修复日志 + 最终复核行号；
  G1、G2、G5 保留为**仍开放（限制）**。
- §4 实机表补 L8（真实 Node 24 + Mineflayer 行为），并加一句「L1–L8 一项都不算已验证」。
- §5 上一轮发现表的「仍缺什么」列改为「最终复核」，M1/M2/M4/F3 的结论按最终复验收口；
  Python 185 → **201**。
- §6 需求矩阵：紧急停止不再写「G4 未闭合」；农田两行改为已收敛（E2/E3）；习惯行补语义键形成；
  owner_human 行去掉「见 G8」的开放措辞；证据编号 E1/E2/E3/E4/E7/E8 全部改到新表对应行。
- §9 结论改为「离线工程 READY（READY OFFLINE）」，列出仍开放限制与 L1–L8 未做。

### 2.2 `docs/MINECRAFT-RESIDENT.md`

- 头部改为最终复核快照 + 报告链接，删除「最终独立复核尚未产出 / PENDING 表」。
- §4.1：构建证据改为 `clean build`（2026-11-02 00:24，`remapJar`、7 tasks）与
  `final-verify/10-guard-clean-build.log` / `11-guard-artifact.log`；自测 63 不变，补 `test { enabled = false }` 事实。
- §5：删除「居民没有复用 policy 冲突检查、G8 修好前别信 owner_mappings」的过度告警，改为两层**同一判定**
  （委托 `owner_human_identity`、冲突失败关闭）。
- §9.4：删除 4.583 格 G3 边界段落，改为「站位取最坏距离最小 + 丢弃 >4.5 格的土，实测 4.123/4.123；
  无土回退 shelter（E2/E3）」。
- §11：重写持久化边界。裸的已认证 409 = 内存闩锁 only（`revoked:false`/`revoke_durable:false`/`ok:false`/
  `error:"already_revoked_unverified"`）；durable 只在此前记录过**该守卫身份**签名回执时成立；
  回归探针改为「已修复并通过（`no false success`）」。
- §12 注：把「不要把一次 ok:true 当 durable」改为「出现 `revoke_durable:false` / `already_revoked_unverified`
  时才不要当 durable」；第 3 步补一句：裸 409 的直连回退输出 `revoked:false`/`revoke_durable:false`/
  `already_revoked:true`/`error:"revoke_not_durable"`。
- §15：重写为「实机未验证（L1–L8）」+「仍开放限制 G1/G2/G5 + R1/R2」+「已修复、不再是缺口」三段，
  不再把 G3/G4/G6/G7/G8/V1/V2 写成开放项。

### 2.3 `docs/ARCHITECTURE-BASELINE.md`

- 头部「最后核对」改为 2026-11-02 00:18，并指向最终报告。
- 复核证据段改为最终快照的 `final-verify/08-baseline-checker.log`（19/19）与
  `final-verify/09-baseline-contract-tests.log`（46/0/0），删除「最终独立复核仍需重跑」。
- §9 覆盖说明的日期/日志同步为 00:18:33 的最终快照；46 项与 19 项数量本身未变（复核复现一致）。

### 2.4 `docs/decisions-minecraft-resident.md`

- 状态改为「最终独立复核已于 2026-11-02 完成（READY OFFLINE）；实机未连接」；快照改为最终复核快照并链接报告，
  删除 PENDING 表引用。
- 「当前实现」标题日期改为 2026-11-02；离线证据改为同一快照数字（63/115/201/2179/19/46），
  删除「没有最终独立复核报告」。
- 「什么时候可以认为接入了」：把「最终独立复核未做」改为「离线门槛已关闭，但离线绿灯不等于验收完成」。
- 「已知偏差与待补」：G3/G4/G8/V1/V2/G6/G7 收口为「已修复、不再列为缺口」并给修复日志名；
  只保留 G1/G2/G5 与 R1/R2 作为仍开放限制。

### 2.5 `docs/MINECRAFT-PROTOCOL.md`

- §4「A secret is configured」：写明只有**签名**回执才令 `revoked:true` + `revoke_durable:true` 并可能 `ok:true`；
  删除「verified signed receipt **or an authenticated 409** … can make ok:true」。
- 删除旧残余段落（「409 只证明内存闩锁 … residual in `repaired-2026-11-01.md` §R1 … probe reproduces it」），
  替换为：裸 409 = 内存闩锁 only（`revoked:false`/`revoke_durable:false`/`ok:false`/
  `error:"already_revoked_unverified"`）；durable already-revoked 只在本进程此前记录过**该精确守卫身份**
  （URL + world id + world path + actor UUID）的签名回执时成立；`503 → 409` 永不报成功；
  回归探针已修复并通过（`no false success`，第二次 `already_revoked_unverified` / `ok:false`）；
  签名回执路径与 `port: 0` / 当前 LAN 端口语义**不变**。
- 同步修正两处会与新语义冲突的旧措辞：§1.3「authenticated 409 revoked is the already-latched answer
  (already safe)」补上「in-memory latch only，durability 需更早的签名回执（见 §4）」；
  §4 direct fallback 的 `revoked: true` / `revoke_receipt: "signed" or "already_revoked"` 改为：只有签名路径
  `revoked:true`/`revoke_durable:true`，裸 409 是 `revoked:false`/`already_revoked:true`/`revoke_durable:false`/
  `error:"revoke_not_durable"`（与 `minecraft_control.revoke_direct` 当前源码一致）。
- 未改：§1.1/§1.2/§1.5/§1.6、§2、§3、§5，port 0/当前端口语义、busy 回执、备份调度均保持原文。

## 3. 最终使用的数字（全部来自 2026-11-02 00:16–00:30 同一快照）

| 项 | 数字 | 日志 |
| --- | --- | --- |
| Python 聚焦（六个 `tools.test_minecraft_*`） | **201 tests / 43.540 s / OK / exit 0 / 0 fail / 0 skip** | `final-verify/05-focused-minecraft-python.log` |
| Python 全量 | **2179 tests / 288.260 s / OK (skipped=2) / exit 0 / 0 fail / 0 error** | `final-verify/06-full-python-discovery.log` |
| 全量 skip（2，非 Minecraft） | `tools.test_memes.SeedDataTests.test_seeded_book_is_usable`；`tools.test_ha_content.SnapshotTests.test_build_rejects_names_no_windows_peer_could_apply` | `final-verify/06b-skip-reasons.log` |
| 侧车 | **115 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo**；`duration_ms=4798.5569` | `final-verify/07a-sidecar-npm-test.log` |
| 侧车语法 | **8/8** `node --check` exit 0 | 07a `--- syntax ---` |
| 侧车审计 | **0 vulnerabilities** | `final-verify/07b-sidecar-npm-audit.log` |
| 守卫构建 + 自测 | **BUILD SUCCESSFUL 18s**；**passed=63 failed=0**；`remapJar` executed；`:test SKIPPED`（设计如此） | `final-verify/10-guard-clean-build.log` |
| 守卫产物 | **70,856 B**；SHA-256 `B373B470F6BDE30A01EEC5C9723340DF6FD81EA43459AA97552FD4AA29D7A82F`；39 entries；class major 61；**8 个 intermediary Class 引用（含 `class_3324` PlayerManager）/ 0 个 yarn-exclusive 引用**；另 1 个 official identity-name 引用 `MinecraftServer`（raw scan `yarn_like_refs=1`） | `final-verify/11-guard-artifact.log:82-87,96-97` |
| 架构基线 | 检查器 **19 passed / 0 failed / 19 total**；离线测试 **46 passed / 0 failed / 0 skipped** | `final-verify/08-…log`、`09-…log` |
| 撤销持久化探针 | exit 0；`no false success`；第二次 409 → `already_revoked_unverified`、`revoke_durable:false`、`ok:false` | `final-verify/01-revoke-durability.log` |
| 农田回退探针 | exit 0；`kind = shelter`，5 步 | `final-verify/02-farm-fallback.log` |
| 农田站位探针 | exit 0；`4.123 / 4.123 ≤ 4.5` | `final-verify/03-farm-stand-reach.log` |
| 习惯形成探针 | exit 0；`greet_known`/`return_home`/`tend_farm` 各 count 3；`candidate_branches_reachable=True` | `final-verify/04-habit-formation.log` |
| Java 探针 | 503 unsigned → 409 revoked、`restarted_revoked=false`；100 轮 `connect200=61 connect409=39 late200=0`；`ALL PROBES PASSED` | `final-verify/12-java-probes.log` |
| Node busy HTTP 探针 | exit 0；busy observe 17 不隔离；busy skill `-32008` stopCount=1；`revoke_detect_ms=2` | `final-verify/13-node-busy-http.log` |

未使用的旧数字：**185**（23:05 聚焦）、**2161**（23:02 全量）——两者都被 201/2179 取代，五份文档内已无残留。

## 4. 关闭的缺口与依据

| 缺口 | 关闭依据（修复日志 + 最终复核） |
| --- | --- |
| G3 农田 4.583 > 4.5 | `fix-farm.log`（2026-11-01 23:25，4.123/4.123、农场契约 4 项 OK）+ 最终 E3 + 源码 `minecraft_resident.py:3025-3047` |
| V2 无土不回退 shelter | `fix-farm-fallback.log`（23:57，`kind=shelter`、exit 0）+ 最终 E2 |
| G4 撤销持久化假成功 | `fix-revoke.log`（23:43，probe `no false success`；`revoked:false`/`durable:false`/`ok:false`）+ 最终 E1/E13 + Java 用例 `revoke is not acknowledged when the marker cannot be persisted` |
| G8 owner-human 双实现 | `fix-identity.log`（23:29）+ 最终报告 §3.6 源码抽查（`minecraft_resident.py:1583-1594` 委托 `owner_human_identity`） |
| V1 语义习惯不形成 | `fix-habit-formation.log`（2026-11-02 00:13，3/3/3、负例按预期失败）+ 最终 E4 + 用例 `test_successful_actions_form_semantic_habits_across_threshold` |
| G6 协议 §4 过期 | 本次定稿直接改 §4（裸 409 只算内存闩锁；旧残余段落删除；port 0 语义不变），与 `core.py`/`minecraft_control.py` 当前源码一致 |
| G7 测试表非同一快照 | 最终报告在同一棵树重跑并给出唯一表（201/2179），旧「E4 早于最后改动」限制不再适用 |

## 5. 一致性检查（五份文档）

对 `docs/MINECRAFT-{RESIDENT,ACCEPTANCE,PROTOCOL}.md`、`docs/ARCHITECTURE-BASELINE.md`、
`docs/decisions-minecraft-resident.md` 搜索并处理：

- `PENDING` / `尚未` / `未产出` / `未签字` / `等最终`：**0 命中**（原 PENDING 表头、E10 缺失行、头部
  「最终独立复核未产出」全部移除）。
- `未修復` / `未修复`：**0 命中**。
- 旧计数 `185` / `2161`：**0 命中**；`4.583`、`FALSE_SUCCESS`、`OUT_OF_REACH`：**0 命中**。
- 保留但确认合法的命中：`63`（自测）、`115`（侧车）、`46`（基线测试）、`19/19`（检查器）——
  这些数字在最终快照里被复现，不是旧口径。
- E 编号交叉引用逐条核对：RESIDENT 的 E2/E3；ACCEPTANCE 内部 E1–E14、E5（许可）、E9/E5（备份）、
  E1（紧急停止）、E7（侧车）、E6（longmem）、E4（习惯）、E3（农田）、E2（回退）；协议 §4 引用 E1——全部指向新表正确行。
- 未改其它文档（包括 `docs/BEHAVIOR-*.md`、`docs/HANDOFF*.md` 等与本次无关的内容）。

## 6. 仍然实机未验证（一项都不算已验证）

L1 真实 1.20.1 客户端 + Fabric Loader/Fabric API 加载守卫 jar、integrated-server 生命周期、真实 kick；
L2 真实 Open-to-LAN 端口获取/端口变化重连/防火墙边界；
L3 微软设备码登录、令牌续期、离线 LAN 兼容性；
L4 真实存档 `saveAll` + 大存档快照耗时、Windows 重解析点边界；
L5 真实恢复演练与当前 70,856 B jar 的 CLI restore smoke；
L6 专用副本内 30–60 分钟主人观察运行；
L7 真实 JEV 服务接入；
L8 真实 Node 24 + Mineflayer 4.39.0 在 1.20.1 的寻路/挖掘/放置/农田/箱子/死亡重生/取消。

## 7. 仍开放的限制 / 残余（不是已修复项）

- G1：`backup_retention`、`interest` 无读取者；保留份数以守卫 `retention` 为准。
- G2：`guard_unavailable` 合并 401（密钥错）与守卫不可达。
- G5：守卫周期快照仍在服务器线程内联复制，大世界耗时未测。
- R1（最终报告 Low）：没有单一 Java→Node 真实 socket 的 busy/revoke 端到端用例。
- R2（最终报告 Low）：busy 回执 `attested_at` 无最大年龄。

## 8. 残留矛盾 / 注意

- 未发现五份文档内部的新矛盾。两处需要读者注意的边界：
  1. 协议 §1.5 仍写「After revocation, new requests are refused with 409 revoked; a second revoke gets
     that authenticated 409 and no signed receipt」，与 §4 一致；它描述守卫行为，不是宿主 `ok` 语义。
  2. ACCEPTANCE §2 明确保留「jar 级恢复 smoke 未重跑」——这是最终报告 §5 明确保留的事实，不是遗漏。
- 文档里的行号引用（如 `minecraft_resident.py:1583-1594`、`core.py:497-532, 665-672`）来自最终报告与
  当前源码抽查；本次只改文档，未改源码，所以这些行号仍指向同一份快照。
- 未做：真实游戏/浏览器/微软登录/真实存档/凭证/部署/服务重启；未改源码、测试、配置、AGENTS、
  sidecar/guard README；未动其它框架/业务脏改。工作树里既有的无关脏改（如 `docs/BRAIN.md`、
  `docs/DATA-PERSISTENCE.md`、`docs/HANDOFF.md`，mtime 分别为 2026-10-31 / 10-30 / 10-30）早于本次定稿，
  本次未触碰；本次实际写入只有上列 5 份文档与本报告。
