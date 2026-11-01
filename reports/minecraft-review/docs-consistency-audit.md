# Minecraft 文档一致性审计（只读）

**审计时间**：2026-11-02 01:20 +08:00  
**审计对象**：`docs/MINECRAFT-PROTOCOL.md`、`docs/MINECRAFT-RESIDENT.md`、`docs/MINECRAFT-ACCEPTANCE.md`、
`docs/ARCHITECTURE-BASELINE.md`、`docs/decisions-minecraft-resident.md`、
`reports/minecraft-review/final-verification-2026-11-02.md`，以及它们引用的源码与日志。  
**写入范围**：本文件。未改任何 docs/source/tests/config。  
**总结论**：源码与离线证据仍然成立，**READY OFFLINE 不受影响**；但文档/报告有 **1 项 blocking、3 项
should-fix、5 项 nit** 的一致性缺陷，定稿前应处理。没有发现新的源码缺陷，也没有发现把实机项写成已验证、
或把 `READY OFFLINE` 写成整体 READY 的语句。

## 0. 方法与快照

- 本审计的基准是 `final-verification-2026-11-02.md` 的 00:16:40–00:30:20 快照，报告 mtime `00:35:02`。
- 当前源码 mtime 仍是最后一次写入：`minecraft_resident.py` 00:06:39、`test_minecraft_resident.py` 00:07:54；
  以下 10 个关键文件的 SHA-256 与报告第 1 节逐字节一致（重新用 `Get-FileHash` 核对）：
  `minecraft_resident.py`、`test_minecraft_resident.py`、`core.py`、`minecraft_policy.py`、
  `minecraft_guard.py`、`PermitService.java`、`GuardSelfTest.java`、`adapter.js`、`guard.js`、`controller.js`。
  因此 01–04 探针日志（00:17）晚于最后一次源码写入，仍对应当前树。
- 当前文档是在报告**之后**定稿的：`ARCHITECTURE-BASELINE.md` 00:46:01、`decisions-minecraft-resident.md`
  00:46:59、`MINECRAFT-PROTOCOL.md` 00:47:52、`MINECRAFT-RESIDENT.md` 00:50:00、
  `MINECRAFT-ACCEPTANCE.md` 00:51:00。这一点直接导致报告 D1 过期（见 F3）。
- 按任务要求做只读 grep/字节级核对；**没有重跑四个离线探针**，因为源码哈希与报告快照一致、日志已在最后
  源码写入之后生成。为避免新增写入，未生成新的证据日志。独立用 Python 重新扫描了当前
  `pawbot-guard-1.0.0.jar` 的 class 常量池（只读），用于核对 E10（见 F4）。
- `HEAD=3dff9ef7689666ca583b6f0d27df76c858a6d2d5`，`git status --porcelain` 仍为 206 条。

## 1. Blocking

### F1 — 撤销演练说“已进入守卫服务器线程的请求不会被撤销打断”，与协议和源码相反

- **Problem**：`MINECRAFT-RESIDENT.md` §11 说
  “已经进入守卫服务器线程的那一个请求不会被撤销打断”。源码中撤销会递增
  `revocationGeneration` 并置 `revoked`；请求在排队、`build`、`signIfCurrent`、`finish`/`SnapshotGate.commit`
  任一处都会复核代次，撤销期间进入的请求最终得到 `409 revoked`，**不会签发或发布许可**。只有已经完成的
  世界复制本身可能继续跑完，不能写成“请求不会被打断”。
- **Location**：
  - `docs/MINECRAFT-RESIDENT.md:396`
  - 反证：`docs/MINECRAFT-PROTOCOL.md:171-174`（“already queued or inside the save/copy is answered `409`
    and its snapshot is never published”）
  - 反证：`docs/MINECRAFT-ACCEPTANCE.md:104`（排队/构建/签名/发布都校验，`late200=0`）
  - 源码：`minecraft-guard/src/main/java/dev/pawbot/guard/PermitService.java:382-387, 462-493, 518-528, 614-621`；
    `SnapshotGate.java:139-154`
- **Reason**：这是主人撤销演练中最安全敏感的一段，且与协议 §1.5、验收矩阵 M1、源码三处冲突。它会让
  读者以为撤销后仍可能有一个排队许可被签发，正好与已修复的 M1 语义相反。文档定稿不能保留这一句。
- **Severity**：**blocking（文档定稿）**。不是代码缺陷；源码与协议是正确的，必须改文档。
- **必须的修改**：把该句替换为——
  “已经进入守卫服务器线程的**复制**可能继续跑完，但该请求不会签发或发布许可：撤销代次会让排队中/
  构建中/签名前/发布前的请求得到 `409 revoked`（`SnapshotGate.commit` 也会拒绝），只有撤销前已经返回的
  5 秒许可仍是既有语义。撤销保证的是「不再签发新许可 + 尽力断开 actor」，不是「这一 tick 世界绝不会再变」。”
  并保留下一句的“尽力断开 actor”语义。

## 2. Should-fix

### F2 — “无观测时回退脚下 y-1”与源码、E2、V2 相反

- **Problem**：文档说农田在没有观测到土壤时会回退到脚下 `y-1`。当前源码明确**没有这个回退**：
  `_farm_soil` 只接受观测到的、`sy == fy - 1` 且水平 2 格内的土壤；没有观测就返回 `[]`；
  `_ensure_project` 随后把项目从 `farm` 改为 `shelter`。E2 的日志正是 `kind = shelter`。
- **Location**：
  - `docs/MINECRAFT-RESIDENT.md:347`（“没有可用观测时才回退到脚下 `y-1`”）
  - `docs/MINECRAFT-ACCEPTANCE.md:107`（M4：“无观测时回退 `y-1`”）
  - 反证：同文档 `docs/MINECRAFT-ACCEPTANCE.md:71`（V2：项目回退 `shelter`）、`:187`
  - 源码：`app/pawbot/minecraft_resident.py:2926-2931, 2940-2951, 2994-2997`
  - 日志：`reports/minecraft-review/final-verify/02-farm-fallback.log`（`kind = shelter`）
- **Reason**：同一份验收文档的 M4 行和 V2 行互相矛盾；M4 的“当前源码状态”停留在中间修复版本，
  会让主人误以为机器人会尝试猜一个 `y-1` 方块，而实际会直接放弃农田项目。该说法也没有任何当前
  日志支持（E2 反而证明相反行为）。
- **Severity**：**should-fix**。
- **必须的修改**：删除两处 `y-1` 回退句，改为：
  “没有观测到可种植土壤时不生成 farm 步骤，项目回退到 `shelter`（E2/V2）；农田仍要求土壤方块
  `(x,y,z)`、作物在 `y+1`，并保留 4.5 格站位/丢弃规则。”

### F3 — 最终验证报告的 D1 已过期，行号已不指向它描述的旧文档

- **Problem**：报告 D1 说三份 owner 文档仍把 G3/G4/G8 列为未修、协议 §4 仍写“已认证 409 可令
  `ok:true`”、验收表还是 `PENDING FINAL INDEPENDENT SNAPSHOT`。当前文档（00:46–00:51 定稿）已经
  全部相反：G3/G4/G8/G6/G7 标为已修复，协议 §4 明确裸 409 只是内存闩锁，验收表已替换为 E1–E14。
  D1 给出的 `MINECRAFT-PROTOCOL.md:310-318`、`MINECRAFT-RESIDENT.md:343-344, 400-401, 445, 482-488`、
  `MINECRAFT-ACCEPTANCE.md:23, 40, 42, 60, 61, 65, 146` 现在指向的是新正文/无关段落。
- **Location**：
  - `reports/minecraft-review/final-verification-2026-11-02.md:119-133`（D1）
  - `reports/minecraft-review/final-verification-2026-11-02.md:279-281`（“唯一需要先处理的是文档 D1 … 必须先按本报告改写”）
  - 当前文档：`docs/MINECRAFT-PROTOCOL.md:310-330`、`docs/MINECRAFT-RESIDENT.md:505-507`、
    `docs/MINECRAFT-ACCEPTANCE.md:70-77`
- **Reason**：D1 对 00:16–00:30 快照是真实的，但报告被当作当前 handoff 时会误导：读者会以为文档仍是旧版，
  或按已失效的行号去改。这是报告层面的状态漂移，不是 docs 仍存在旧语义的证据。
- **Severity**：**should-fix**。
- **必须的修改**：在 D1 增加 “Resolved by 00:46–00:51 documentation finalization; see
  `reports/minecraft-review/documentation-final.md`”，或明确把 D1 限定为 00:16–00:30 快照的历史发现；
  同时把 §9 的“唯一需要先处理”改为“已由后续文档定稿解决，不再阻塞 handoff”。不要让读者按旧行号改文档。

### F4 — E10 的“7 个 intermediary 引用”与所引日志、当前 jar 实际扫描均为 8

- **Problem**：报告、验收表、主人手册都写“7 个 intermediary / 0 个 yarn-only”。所引
  `11-guard-artifact.log` 的原始扫描行是 `intermediary(remapped)_refs=8`，样本包含
  `net/minecraft/class_3324`（PlayerManager）；同文件后面的“remap proof”摘要却写 7，并漏掉
  `class_3324`。我独立扫描当前 `pawbot-guard-1.0.0.jar` 的常量池也得到 **8** 个 intermediary 引用。
  “0 yarn-only”只有在把 `net/minecraft/server/MinecraftServer` 视为 official identity mapping 时成立；
  原始扫描行同时给出 `yarn_like_refs=1`（就是 MinecraftServer）。
- **Location**：
  - `reports/minecraft-review/final-verification-2026-11-02.md:84`（E10）、`:160`（dev jar 7 个 yarn 引用）
  - `docs/MINECRAFT-ACCEPTANCE.md:45`（“7 个 intermediary / 0 个 yarn-only”）
  - `docs/MINECRAFT-RESIDENT.md:80-83`（同）
  - 日志：`reports/minecraft-review/final-verify/11-guard-artifact.log:80, 82, 84, 98, 99`
- **Reason**：这是报告/文档引用的核心重映射证据数字，差 1 会让“重映射完整性”的复核数字不可复现；
  同一日志的 raw scan 与 summary 也互相矛盾。实质结论（已重映射、无 yarn-exclusive 引用）仍成立，
  但数字必须按 raw scan 写。
- **Severity**：**should-fix（证据准确性）**。
- **必须的修改**：改为“8 个 intermediary Class 引用（含 `class_3324` PlayerManager）；1 个 yarn 名引用
  = `MinecraftServer`，属 official identity mapping；0 个 yarn-exclusive 引用”。若保留 `7`，必须解释
  被排除的是哪一个 class，并修正 `11-guard-artifact.log` 的摘要/样本不一致。

## 3. Nit

### F5 — “33 条 RuntimeWarning”与日志的实际数量不符

- **Problem**：报告 §2 与验收 §2 注都说 E6 有 33 条 `RuntimeWarning`。实际日志中
  `: RuntimeWarning:` 为 **16** 条（32 行包含 `RuntimeWarning`，因为另有 16 行 “Enable tracemalloc”），
  `Coroutine created at` 为 15 条；来源也不只 proactive/turns，还包含 `app/plugins/ai_chat.py:584`
  和 `unittest.mock` 内部。这个噪声计数不影响 `OK (skipped=2)` 与 0 failure/0 error。
- **Location**：`reports/minecraft-review/final-verification-2026-11-02.md:97-98`；
  `docs/MINECRAFT-ACCEPTANCE.md:54`；`reports/minecraft-review/final-verify/06-full-python-discovery.log`
- **Reason**：数字不可从日志复现；描述也不完整。
- **Severity**：**nit**。
- **必须的修改**：写“16 条 warning（32 行含 RuntimeWarning）”，或删除具体条数，只保留“既有未 await 噪声、
  非 Minecraft、0 failure/0 error”的结论。

### F6 — E13 的“另一次 55/45 复跑”没有日志

- **Problem**：报告 E13 说“另一次复跑 55/45，同样 late200=0”，但仓库里唯一 100 轮日志是
  `final-verify/12-java-probes.log` 的 `connect200=61 connect409=39`；其他旧日志是 75/25、70/30、66/34，
  没有 55/45。
- **Location**：`reports/minecraft-review/final-verification-2026-11-02.md:87`；
  `reports/minecraft-review/final-verify/12-java-probes.log`；
  `reports/minecraft-review/repaired/revoke-race-stress*.log`
- **Reason**：额外复跑是支持性证据，但不该以无日志的数字出现；容易被误当成可复现证据。
- **Severity**：**nit**。
- **必须的修改**：删除 55/45 分句，或补一份对应的日志并注明命令/时间。

### F7 — 紧急停止的“最后闩锁”顺序与源码相反（同一决策文档内也互相矛盾）

- **Problem**：`decisions-minecraft-resident.md` 决策 8 说“先撤销守卫授权，再取消并回收居民/侧车，
  最后闩锁到 PawBot 重启”；同一文件后面的实现说明说“`minecraft_emergency_stop` 先关闸、再撤销/停止/回收”，
  源码也是先 `self._minecraft_estop = True`，再进入 `_minecraft_stop_session` 撤销/停止。
  `MINECRAFT-RESIDENT.md:432` 的“先确认撤销…并闩锁”同样把闩锁写在后面。
- **Location**：`docs/decisions-minecraft-resident.md:33` vs `:58`；
  `docs/MINECRAFT-RESIDENT.md:432`；`app/pawbot/core.py:600-609`
- **Reason**：实际顺序更安全（先关闸），文档写成“最后闩锁”会让读者以为存在先撤销后关闸的窗口。
  功能与 `ok:true` 条件不受影响。
- **Severity**：**nit**（可归入 should-fix，但非阻塞）。
- **必须的修改**：统一写成“先置本地闩锁/关闸，再撤销守卫，再取消并回收居民/侧车；成功回执仍要求
  `revoked`/`revoke_durable`/`stopped`/`reaped`/`writes_drained`/`latched` 全为真”。

### F8 — “没有代码读了但没登记的键”与 `decision_cost_usd` 旧别名冲突

- **Problem**：`MINECRAFT-RESIDENT.md` §5 说 `config.DEFAULT.minecraft` 全部键已登记、“没有「代码读了
  但没登记」的键”。但 `JevAdapter._settings` 会读取旧别名 `minecraft.decision_cost_usd`，它不在
  `DEFAULT` 的 33 个 `minecraft` 键里（表里只有 `decision_cost_usd_per_call`）。文档自己在同一节
  第 206 行又提到这个别名，前后不一致。
- **Location**：`docs/MINECRAFT-RESIDENT.md:152` 与 `:206`；
  `app/pawbot/minecraft_resident.py:1338-1339`；`app/pawbot/config.py` 的 `DEFAULT.minecraft`（33 键）
- **Reason**：这是配置登记/基线的事实性细节；把别名当成“已登记键”会误导读者的配置核对。
- **Severity**：**nit**。
- **必须的修改**：改为“除旧别名 `minecraft.decision_cost_usd` 外，其余读取键均已登记；该别名只作
  `decision_cost_usd_per_call` 的回退”。如果选择登记它，会改变 33 键基线，必须同步
  `scripts/architecture-baseline.mjs` 与测试，不能只改文档。

### F9 — “配置缺失 / enabled=false 也会打印 refused”与源码不符

- **Problem**：`MINECRAFT-RESIDENT.md` §4.3 说配置缺失、`enabled=false`、世界/路径/标记问题、备份根
  不可用、端口被占“都只会打印静态 `refused: ...`”。源码中**配置缺失**（不是常规文件）和
  `enabled=false` 是静默返回，不打印任何 `refused:`；只有加载失败、世界/路径/标记、备份根、端口、
  revoke marker 等情况才打印。
- **Location**：`docs/MINECRAFT-RESIDENT.md:138-139`；
  `minecraft-guard/src/main/java/dev/pawbot/guard/GuardMod.java:69-89`
- **Reason**：排障者看不到 `refused:` 时会误以为日志丢了或配置已加载；这是可观察行为差异。
- **Severity**：**nit**。
- **必须的修改**：改为“配置缺失或 `enabled=false` 时静默保持关闭；其余拒绝会打印静态 `refused: ...`”。

## 4. PASS（本次审计未发现问题的部分）

- **快照完整性**：上述 10 个关键源码文件 SHA-256 与报告第 1 节一致；源码 mtime 未变；`HEAD` 与
  dirty 数量一致。01–04 日志均晚于最后一次源码写入，因此不需要重跑探针也能支撑本轮文档审计。
- **撤销持久化（源码/探针）**：`PermitService.revoke()` 先置内存闩锁/代次，`persistRevocationMarker()`
  成功才签名；失败为无签名 503；`core._minecraft_guard_revoke` 只有签名回执或“本进程此前记录过该守卫
  身份签名回执后的 409”才算 durable；裸 409 为 `already_revoked_unverified`。E1（`no false success`）
  与 E13（503→409、`restarted_revoked=false`）与源码一致。**F1 是文档句子错误，不是这里的行为错误。**
- **Busy 回执**：Java `signedBusy` 正好 16 字段、`can_write/can_pvp=false`、`backup_id=""`、
  `retry_after_ms=250`、`attested_at` 来自成功 live attestation；Python/Node 都校验精确 16 键并拒绝
  额外字段；`observe` 接受 busy，变更动作 `GuardBusy`/`-32008` 有界暂停；E14（`-32008`、`stopCount=1`、
  `revoke_detect_ms=2`）与协议 §1.3 一致。
- **单一备份调度**：`GuardConfig` 60–1800（默认 1800）、`BackupSchedule`、`GuardMod` 是唯一 tick 调度点；
  Node `controller.js` 只有 250 ms watchdog、没有 backup timer；居民只做 `observe`，没有周期 `backup`；
  `_backup_interval_seconds()` 无调用者。与协议 §1.4/§2、主人手册 §9.2、决策 6 一致。
- **农田 4.5**：`FARM_SOIL_REACH = 4.5`，站位按“最坏距离最小”选择，超过 4.5 的土被丢弃；
  `adapter.js loadedBlock(p, 4.5)` 与 `farm` 的土壤 `(x,y,z)`/作物 `y+1` 约定一致；E3 为 4.123/4.123。
  无土时项目回退 `shelter`（E2）。**F2 只涉及“y-1”这句错误描述。**
- **owner-human 身份**：居民 `_owner_human_uuid()` 委托 `minecraft_policy.owner_human_identity`，只接受
  精确规范化 UUID、必须不同于 bot actor，冲突/一对多 `owner_mappings` 失败关闭；`_role_for` 只把 actor
  映射为 self、该精确 UUID 映射为 owner；`_owner_present` 只认该 UUID。没有第二套判定（G8 已修）。
- **家 seed**：`_home_seeded` 初始 False、重连复位；`HOME_SEED_TOLERANCE=3.0`；`MAX_HOME_SEED_ATTEMPTS=3`、
  `HOME_SEED_RETRY_COOLDOWN=600`；`set_home` 走 `observe`，`home` 只在 seeded 后进入可选技能，
  `set_home` 从 affordance 移除；`HOME_RETURN_RADIUS=12` 只在 `_home_step` 使用。与手册 §9.4、验收 M3、
  决策 11 一致。
- **重复实现/重复调度**：未发现第二套 owner-human 判定、第二套周期快照计时器或居民主动 `backup`
  请求；与 G8、单调度、决策 6/9 的结论一致。
- **协议 vs 两个客户端**：revoke 端口例外（Python receipt 接受 `0` 或 `1024–65535`，Node 接受
  `0–65535`，其它动作要求精确端口）；busy 字段集、`-32008`、connect 前恰好一个 `connect` 许可
  （Node 60 s、居民只 `observe`、没有额外 `backup`）、409 映射、17 个技能/队列 8/在途 16+2/超时 30/
  响应 32768/事件 4096 均与源码一致。没有发现协议与两个客户端的字段或语义冲突。
- **READY 范围与实机项**：五份文档和报告都只写 `READY OFFLINE`；L1–L8（真实 Fabric 加载、Open-to-LAN、
  微软/离线认证、真实备份/恢复、30–60 分钟观察、真实 JEV、真实 Mineflayer 行为）均标“未做/未验证”；
  没有把实机项写成已验证，也没有把离线绿写成 acceptance complete。报告 §6 的“L1–L8 一项都不算已验证”
  与验收 §4 一致。
- **Stale PENDING/旧数字**：当前 Minecraft 文档中没有 `PENDING FINAL INDEPENDENT SNAPSHOT`、`4.583`、
  `OUT_OF_REACH`、`2177`/`199`、`75/25`、`FALSE_SUCCESS` 等旧口径；`verified-*.log` 与 00:02 的
  仅表头 `final-verify/04-focused-minecraft-python.log` 都被明确排除。F3 是报告 D1 的过期状态，不是
  当前 docs 仍有 PENDING。
- **主证据数字**（除 F4/F5/F6）：聚焦 201/43.540 s、全量 2179/288.260 s（skipped=2）、侧车 115/
  4798.5569 ms、selfTest 63、jar 70,856 B/`B373…A82F`/39 entries/class major 61、基线 19/19 与 46/0/0、
  E3 4.123/4.123、E4 3/3/3、E13 61/39 `late200=0`、E14 `revoke_detect_ms=2`，均可在对应日志中逐项复现。

## 5. 数字对照（供定稿直接使用）

| 指标 | 文档/报告写法 | 日志/独立核对 | 判定 |
| --- | --- | --- | --- |
| 聚焦 Python | 201 / 43.540 s / OK | `05-focused-minecraft-python.log` | PASS |
| 全量 Python | 2179 / 288.260 s / OK (skipped=2) | `06-full-python-discovery.log` | PASS |
| 侧车 | 115 / 4798.5569 ms | `07a-sidecar-npm-test.log` | PASS |
| 守卫自测 | 63 / 0 fail | `10-guard-clean-build.log` | PASS |
| jar | 70,856 B / `B373…A82F` / 39 entries / major 61 | `11-guard-artifact.log` + 当前 jar | PASS |
| 基线 | 19/19 + 46/0/0 | `08`/`09` | PASS |
| 农田站位 | 4.123/4.123 ≤ 4.5 | `03-farm-stand-reach.log` | PASS |
| 习惯 | 3/3/3 | `04-habit-formation.log` | PASS |
| 撤销竞态 | 100 轮 61/39 / late200=0 | `12-java-probes.log` | PASS |
| 重映射 | 7 intermediary | raw scan 8；独立扫描 8 | **F4** |
| E6 警告 | 33 RuntimeWarning | 16 条 warning（32 行） | **F5** |
| E13 复跑 | 另一次 55/45 | 无日志 | **F6** |

## 6. 结论与定稿前动作

1. **F1 必须改**（blocking）：`MINECRAFT-RESIDENT.md:396` 的撤销语义与协议/源码相反；按上文替换句修改后，
   撤销演练才与 `PROTOCOL §1.5`、`ACCEPTANCE M1`、`PermitService` 一致。源码不需要改。
2. **F2/F3/F4 应改**（should-fix）：F2 去掉不存在的 `y-1` 回退；F3 把 D1 标为已由 00:46–00:51 文档定稿
   解决/限定为历史快照；F4 把 7 改为 8（或解释被排除的 class_3324），并修正 artifact 日志摘要与 raw scan
   的不一致。
3. **F5–F9 可选但建议**：均为计数/顺序/措辞 nit，不影响 READY OFFLINE 结论，但会影响 handoff 的可复核性。
4. **结论不变**：没有复现新的源码缺陷；`READY OFFLINE` 仍是对当前树的正确离线结论；实机 L1–L8 一项都
   不算已验证。文档侧最需要先处理的是 F1，其次是 F2/F3/F4。

> 本审计只写本文件；未修改 docs/source/tests/config，也未重跑会新增证据的探针。
