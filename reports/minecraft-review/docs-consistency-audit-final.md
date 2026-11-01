# Minecraft 文档一致性复审计（最终，只读）

**审计时间**：2026-11-02 02:12 +08:00  
**审计对象**：`docs/MINECRAFT-PROTOCOL.md`、`docs/MINECRAFT-RESIDENT.md`、`docs/MINECRAFT-ACCEPTANCE.md`、
`docs/ARCHITECTURE-BASELINE.md`、`docs/decisions-minecraft-resident.md`、
`reports/minecraft-review/final-verification-2026-11-02.md`、`documentation-final.md`、`docs-fixes.md`、
`docs-residual-fixes.md`、`docs-consistency-audit.md`，以及它们引用的源码与日志。  
**写入范围**：仅本文件。未修改 docs/source/tests/config 或其它 reports；未启动游戏、浏览器、微软登录，
未读取运行态人设、记忆、真实凭证或原始存档。  
**结论先行**：**没有 blocking 文档矛盾**；`READY OFFLINE` 对当前树的离线范围仍然成立。复审计发现
**2 项 should-fix、3 项 nit**（见 §2）；它们不否定离线证据，也不把任何 L1–L8 实机项写成已验证。

## 0. 快照与方法

- 只读核对当前树：`app/pawbot/core.py`、`minecraft_resident.py`、`minecraft_guard.py`、
  `minecraft_control.py`、`minecraft_policy.py`，`minecraft-guard/.../PermitService.java`、
  `SnapshotGate.java`、`GuardMod.java`、`GuardConfig.java`，`app/minecraft-sidecar/src/{guard,controller,adapter}.js`。
- 这些关键源码的 SHA-256 与 `final-verification-2026-11-02.md` §1 的快照表逐字节一致（重新
  `Get-FileHash` 核对；源码 mtime 未变，最后一次 Minecraft 源码写入仍是 2026-11-02 00:06:39 /
  00:07:54）。因此 00:16–00:30 的快照日志仍对应当前源码。
- 当前 jar `minecraft-guard/build/libs/pawbot-guard-1.0.0.jar` 实测 **70,856 B**、SHA-256
  **`B373B470F6BDE30A01EEC5C9723340DF6FD81EA43459AA97552FD4AA29D7A82F`**，与
  `final-verify/11-guard-artifact.log` 的 `size_bytes` / `sha256` 一致。
- 当前文档是在复核快照之后定稿/修正的：PROTOCOL 00:47:52、ARCHITECTURE-BASELINE 00:46:01、
  RESIDENT 01:39:20、ACCEPTANCE 01:30:50、decisions 01:44:28；定义记录与修复记录为
  `documentation-final.md` 01:44:38、`docs-fixes.md` 01:40:30、`docs-residual-fixes.md` 01:47:41。
  因此报告 §1 中 23:14–23:36 的文档 mtime/hash 是历史快照值，不是当前文档值（§2 F2 相关状态问题）。
- 复审计按只读 `Select-String` / `Get-Content` / `Get-FileHash` 核对计数与逐行语义；**没有重跑**
  四个 Python 探针、Java 探针或 Node 探针（源码哈希与报告快照一致，日志均晚于最后一次源码写入）。
  没有生成证据日志，也没有写入本文件之外的任何文件。

## 1. PASS（逐条）

### (a) 撤销持久化语义（durable revoke）

**PASS**。

- `core._minecraft_guard_revoke` 对裸的已认证 `409 revoked` 返回
  `(False, "already_revoked_unverified")`；`_record_revoke_outcome` 明确写
  `revoked:false`、`revoke_durable:false`、`already_revoked:true`、
  `revoke_receipt:"already_revoked"`、`revoke_error:"already_revoked_unverified"`（`core.py:181-200,
  497-532`）。无居民分支同样处理（`core.py:634-648`）。
- `ok:true` 的源码条件包含 `revoke_durable is True` 且 `revoked is True`，并同时要求
  `latched`/`resident_stopped`/`sidecar_reaped`/`writes_drained` 且无 `error`
  （`core.py:665-672`）。因此「先 503、后 409」不会变成成功。
- durable 的「已撤销」只在本进程此前记录过**该精确守卫身份**的签名回执时成立；身份为
  `(url, world_id, world_path, actor_uuid)`，receipts 仅为本进程内存集合
  （`core.py:153-178, 519-525`）。
- 协议 §4 的对应段落（`MINECRAFT-PROTOCOL.md:299-329`）与 §1.3 的
  `GuardAlreadyRevoked` 说明（`:121-124`）与源码一致；直连回退的裸 409 输出
  `revoked:false` / `already_revoked:true` / `revoke_durable:false` /
  `error:"revoke_not_durable"`（`minecraft_control.py:151-165`），与协议 §4 `:290-295` 一致。
- 被引日志 `final-verify/01-revoke-durability.log`：`no false success`；第二次 409 为
  `already_revoked_unverified`、`revoke_durable:false`、`ok:false`。**PASS**（字段名问题见 §2 F1）。

### (b) Busy 回执精确性、非变更与单一 Java 调度

**PASS**。

- Java `signedBusy` 正好 16 个字段（`nonce, world_id, save_path, host, port, actor_uuid, action,
  target_uuid, issued_at, expires_at, can_write, can_pvp, backup_id, busy, retry_after_ms,
  attested_at`），`can_write:false`、`can_pvp:false`、`backup_id:""`、`retry_after_ms=250`，
  `attested_at` 来自缓存的成功 live attestation（`PermitService.java:790-816`）；缓存只由成功
  attestation 填充（`:557-558`）；无 live attestation 时直接 503，不猜路径/端口
  （`:497-506`）。
- `SnapshotGate` 的 `Cache` / `remember` / `busyCache` / `commit` 与撤销代次检查
  （`SnapshotGate.java:44-54, 92-103, 131-154`）符合非变更、单 ticket、撤销后不发布。
- Node `guard.js` 的 `BUSY_KEYS` 精确 16 键，拒绝额外字段，并在 `backup_id != ""`、
  `can_write`/`can_pvp` 非 false 时拒绝（`guard.js:8-10, 70-83`）；Python
  `_BUSY_KEYS` 同样精确 16 键（`minecraft_guard.py:30-35, 192-214`）。`observe` 接受 busy，
  其它变更动作转 `GuardBusy` / `-32008` 有界暂停，不重放。
- 单一调度：`GuardConfig` 的 `backup_interval_sec` 为 60–1800，默认 1800（`GuardConfig.java:104-107,
  182`）；`GuardMod.onServerTick` 是唯一 tick 调度点（`GuardMod.java:118, 136-165`）；
  `BackupSchedule` 的 busy skip 不推进 deadline（`BackupSchedule.java:39-50`）；Node
  `controller.js` 只有 250 ms 守卫 watchdog、无 backup timer（`:52-53`）；居民只发
  `observe`/`connect`/`revoke`，没有周期 `backup`，`_backup_interval_seconds()` 只有定义、
  无调用者（`minecraft_resident.py:1541-1549, 1848`）。
- 文档对应：`MINECRAFT-PROTOCOL.md:92-101`（busy）、`:144-149`（单调度）；`MINECRAFT-RESIDENT.md:29-30,
  117, 142, 308-310, 376`；`decisions-minecraft-resident.md:26-29, 46-48`；验收 F3 行
  `MINECRAFT-ACCEPTANCE.md:113`。**PASS**。

### (c) 农田：无 `y-1` 回退、无土返回 `[]` 并回退 `shelter`、4.5 可达

**PASS**（关于 4.5 的措辞精度见 §2 F4）。

- `_farm_soil` 只接受观测块中 `sy == fy - 1`、水平 `max(|dx|,|dz|) <= 2` 的土壤，跳过已有作物列；
  没有可用土壤时 `return []`（`minecraft_resident.py:2940-2997`）。`_project_steps("farm")` 在
  `soils` 为空时 `return []`（`:2999-3008`）；`_ensure_project` 在 farm 步骤为空时把项目
  `kind` 改为 `shelter` 并重新生成步骤（`:2926-2931`）。
- 源码中没有「观测不到时猜脚下 `y-1`」的分支；`FARM_SOIL_REACH = 4.5`（`:98`），站位按
  「最坏距离最小」选择并丢弃超过 4.5 的土（`:3025-3047`）；`adapter.js` 的 `loadedBlock(p, 4.5)`
  与 `farm` 的「土壤 `(x,y,z)`、作物 `y+1`」约定一致（`adapter.js:129, 238-254`）。
- 文档对应：`MINECRAFT-RESIDENT.md:347-349`；`MINECRAFT-ACCEPTANCE.md:70-71, 107`；
  日志 `final-verify/02-farm-fallback.log`（`kind = shelter`、5 步）与
  `03-farm-stand-reach.log`（`4.123 / 4.123 <= 4.5`）。**PASS**。

### (d) 语义习惯键由成功动作形成，无人工 seed

**PASS**。

- `_HABIT_KEY_FOR_SKILL` 把 `home/farm/follow/chat` 映射为
  `return_home/tend_farm/greet_known`（`minecraft_resident.py:596-597`）；`_success()` 末尾调用
  `_bump_habit(...)`（`:2762`），而 `_success` 只从唯一成功路径调用（`:2638`）；习惯候选阈值
  为 `count >= 2`（`:1203-1216`）；`_bump_habit` 持久化计数（`:3148-3159`）。
- `ResidentState._load` 的初始 `habits` 只来自已持久化数据，空状态为 `{}`
  （`:726-770, 800-812`）；没有手工 seed 语义键的代码路径。
- 文档对应：`MINECRAFT-ACCEPTANCE.md:77`（V1）、`:178`；`MINECRAFT-RESIDENT.md:354`；
  日志 `final-verify/04-habit-formation.log`（`3/3/3`、`candidate_branches_reachable=True`）。
  **PASS**（探针文件头问题见 §2 F5）。

### (e) 紧急停止：先闩锁，再撤销/停止/回收；`ok:true` 要求全部确认

**PASS**（字段名问题见 §2 F1）。

- `Bot.minecraft_emergency_stop` 在方法入口先置 `self._minecraft_estop = True`，再获取生命周期锁、
  移除居民引用（`core.py:600-609`）；`_minecraft_stop_session` 的顺序是 revoke → resident
  estop/stop → 兜底侧车关闭 → 回收/写入排空（`core.py:534-579`）。闩锁只在 `__init__` 复位
  （`core.py:265`），运行期没有其它清零点。
- `ok` 精确要求 `latched`、`revoked`、`revoke_durable`、`resident_stopped`、
  `sidecar_reaped`、`writes_drained` 全真且没有 `error`（`core.py:665-672`）；成功/不完整
  分别记录闩锁到重启（`:673-682`）。
- 文档对应：`decisions-minecraft-resident.md:33-36`；`MINECRAFT-RESIDENT.md:432-437`；
  `MINECRAFT-PROTOCOL.md:299-304`。`MINECRAFT-RESIDENT.md` 中旧的「先确认撤销……并闩锁」已不存在
  （全文 `先确认守卫撤销` / `最后闩锁` 命中 0）。**PASS**。

### (f) 报告证据数字

**PASS**（artifact 日志摘要的引用提示见 §2 F3）。

逐项与当前日志/当前 jar 复核：

| 指标 | 文档/报告写法 | 当前日志或实测 | 结论 |
| --- | --- | --- | --- |
| 聚焦 Python | 201 / 43.540 s / OK | `05-focused-minecraft-python.log` 的 `Ran 201 tests in 43.540s` + `OK` | 一致 |
| 全量 Python | 2179 / 288.260 s / OK (skipped=2) | `06-full-python-discovery.log` 的 `Ran 2179 tests in 288.260s` + `OK (skipped=2)`；`FAIL:`/`ERROR:` 均为 0 | 一致 |
| 侧车 | 115 pass / 0 fail | `07a-sidecar-npm-test.log` 的 `pass 115` / `fail 0` / `duration_ms 4798.5569` | 一致 |
| 守卫自测 | 63 / 0 fail | `10-guard-clean-build.log` 的 `passed=63 failed=0` | 一致 |
| jar | 70,856 B / `B373…A82F` / 39 entries / major 61 | `11-guard-artifact.log:2-3,6`；当前 jar 实测同大小同 hash | 一致 |
| 重映射 | 8 个 intermediary（含 `class_3324`）/ 1 个 official identity 名 `MinecraftServer` / 0 个 yarn-exclusive | `11-guard-artifact.log:82-87` raw scan 为 8、样本含 `class_3324`；`:96-97` 证明 `MinecraftServer` 为 official identity mapping；summary 的 7 见 §2 F3 | 一致（以 raw scan 为准） |
| 基线 | 19/19、46/0/0 | `08-baseline-checker.log` 的 `19 passed, 0 failed, 19 total`；`09-baseline-contract-tests.log` 的 `46 passed, 0 failed, 0 skipped` | 一致 |
| E6 warning | 16 条 warning / 32 行含 `RuntimeWarning` / 15 条 `Coroutine created at` | `06` 实测：`16`（`: RuntimeWarning:`）、`32`（含该词）、`15`；`FAIL:`/`ERROR:` 0 | 一致 |
| E13 | 100 轮 `connect200=61 connect409=39 late200=0`，无 55/45 | `12-java-probes.log:21`；全仓目标文档/报告无 55/45（只在历史修复/审计记录引用旧句） | 一致 |
| E13 撤销耐久 | 503 unsigned → 409 revoked、`restarted_revoked=false` | `12-java-probes.log:9-13` | 一致 |
| E14 | busy observe 不隔离；`-32008` stopCount=1；`revoke_detect_ms=2` | `13-node-busy-http.log` | 一致 |

E1/E2/E3/E4 的日志行也与验收/报告一致（`01` 的 `no false success`、`02` 的 `kind = shelter`、
`03` 的 `4.123/4.123`、`04` 的 `3/3/3`）。**PASS**。

### (g) 实机项范围与总 verdict

**PASS**。

- 五份文档与最终报告只写 `READY OFFLINE`；L1–L8（真实 Fabric 加载、Open-to-LAN、微软/离线认证、
  真实备份/恢复、30–60 分钟观察、真实 JEV、真实 Mineflayer）均明确写「未做/未验证」：
  `MINECRAFT-RESIDENT.md:6, 486-497`、`MINECRAFT-ACCEPTANCE.md:6, 82-95, 252-259`、
  `MINECRAFT-PROTOCOL.md`（契约文档，不声称实机验证）、`decisions-minecraft-resident.md:3, 89`、
  `ARCHITECTURE-BASELINE.md:8`。
- 对这些文档搜索 `实机验证|实测通过|已验证|验收完成|实机已`：命中只有「一项都不算已验证」和
  「不把离线结果写成实机已验证」等否定句；没有把 L1–L8 写成已验证，也没有把 `READY OFFLINE`
  写成整体 `READY`。
- `ARCHITECTURE-BASELINE.md:346-348` 仍明确把 `backup_retention`/`interest` 的「登记但无读取者」
  指向验收 §3，不把基线绿当成实机生效。**PASS**。

### 前一轮审计项（F1–F9）的复查

- **F1**：`MINECRAFT-RESIDENT.md:396-399` 已按源码改写；`不会被撤销打断` 在五份文档中命中 0。
- **F2**：`MINECRAFT-RESIDENT.md:347` 与 `MINECRAFT-ACCEPTANCE.md:107` 已删除 `y-1` 回退说法；
  `回退到脚下` / `无观测时回退` 命中 0。
- **F3**：报告 D1 已有 Addendum（`final-verification-2026-11-02.md:137-146`），说明 D1 已由
  00:46–00:51 定稿解决；残留的报告 §9 状态见 §2 F2。
- **F4**：文档/报告已按 raw scan 写 8；`7 个 intermediary` / `7 intermediary` 命中 0；
  artifact 日志自身摘要仍是 7（§2 F3）。
- **F5**：`33 条` 命中 0；验收/报告已写 16 条（32 行含该词）与 15 条堆栈。
- **F6**：最终报告 E13 已无 55/45；只在 `docs-fixes.md` / `docs-consistency-audit.md` 的
  旧文引用中出现，属于历史记录，不是当前主张。
- **F7**：`decisions-minecraft-resident.md:33` 与 `MINECRAFT-RESIDENT.md:434` 已写成
  「先闩锁 → 再撤销 → 停/回收」；`先撤销守卫授权` / `最后闩锁` 命中 0。源码
  `core.py:582` 的旧 docstring 仍是历史残留，已被 `docs-fixes.md` / `docs-residual-fixes.md`
  明确记录，不在五份文档中作为依据。
- **F8**：`MINECRAFT-RESIDENT.md:152` 已写 33 个登记键 + 旧别名 `decision_cost_usd` 的回退；
  `config.DEFAULT.minecraft` 实测 33 键，`minecraft_resident.py:1338-1339` 确实读取旧别名。
- **F9**：`MINECRAFT-RESIDENT.md:138-139` 已区分「配置缺失 / `enabled=false` 静默关闭」与
  其余打印 `refused:` 的拒绝；`GuardMod.java:69-89` 与其余日志点一致。

## 2. Findings

### F1 — 裸 409 的 `error` 字段名在「有居民会话」结果中不成立（should-fix）

- **Problem**：`MINECRAFT-RESIDENT.md` §11/§12 和 `MINECRAFT-ACCEPTANCE.md` G6 把裸的已认证
  `409 revoked` 结果写成带 `error:"already_revoked_unverified"`。源码中**有居民会话**的
  `minecraft_emergency_stop` 结果只在 `_record_revoke_outcome` 里写
  `revoke_error:"already_revoked_unverified"`，**不写** `error`；只有**无居民**的控制路径在
  `core.py:644-648` 同时写 `error` 与 `revoke_error`。被引的 E1 日志 `resident_single_call`
  正好显示 `session:"resident"`、`revoke_error:"already_revoked_unverified"`，没有 `error`。
  此外 CLI 的 `stop_via_control` 对非 ok 的控制响应把 `error` 归为 `control_incomplete`
  （`minecraft_control.py:109-111`），不会把这个字符串原样放进 `error`。
- **Location**：
  - `docs/MINECRAFT-RESIDENT.md:408-409`（§11 持久化失败边界）
  - `docs/MINECRAFT-RESIDENT.md:459-460`（§12 注意）
  - `docs/MINECRAFT-ACCEPTANCE.md:74`（G6 行）
  - 源码：`app/pawbot/core.py:181-200`（只写 `revoke_error`）、`:534-579`（居民路径返回
    `revoke_error`）、`:644-648`（无居民路径另写 `error`）
  - 证据：`reports/minecraft-review/final-verify/01-revoke-durability.log:15`（`resident_single_call`
    无 `error`，有 `revoke_error`）
  - **反证/限定**：`docs/MINECRAFT-PROTOCOL.md:320-325` 的裸 409 段落位于「No active resident」
    小节，作为无居民路径的契约是正确的；问题在 RESIDENT/ACCEPTANCE 把该字段名泛化到所有路径。
- **Reason**：这是撤销演练/排障的精确 JSON 字段说明；语义结论（`ok:false`、`revoke_durable:false`、
  `already_revoked_unverified`）正确，但操作者按 `error` 查有居民结果会找不到该键。应改为
  「`revoke_error`（无居民控制路径同时有 `error`）」或明确区分两条路径。不是安全语义反转，
  因此不是 blocking。
- **Severity**：**should-fix**（文档准确性；不阻塞 `READY OFFLINE`）。

### F2 — 最终复核报告顶部/§9 仍把 D1 写成 handoff 待办，与 D1 Addendum 和已定稿文档相反（should-fix）

- **Problem**：`final-verification-2026-11-02.md` 顶部仍写「唯一需要处理的是文档漂移（owner 文档仍写着
  已修复的 G3/G4/G8 …）」，§9 仍写「唯一需要先处理的是文档 D1 … 必须先按本报告 §7 的数字与结论改写，
  才能交给主人」，summary 仍写 `findings=1 documentation drift (D1, Medium)`。同一报告的 D1
  Addendum（`:137-146`）已经明确：D1 不再是 handoff 待办项，三份 owner 文档在 00:46–00:51 已定稿，
  旧行号失效；当前文档也确实已改成 G3/G4/G6/G7/G8/V1/V2 收口、协议 §4 裸 409 语义、E1–E14 证据表。
  `docs-fixes.md:216-218` 自认这是「刻意未改的 §9 残余」，以 Addendum 为准；`docs-residual-fixes.md`
  也没有收回它。
- **Location**：
  - `reports/minecraft-review/final-verification-2026-11-02.md:7`（顶部「唯一需要处理的是文档漂移」）
  - `reports/minecraft-review/final-verification-2026-11-02.md:291-293`（§9 D1 待办）
  - `reports/minecraft-review/final-verification-2026-11-02.md:310`（`findings=1 … D1, Medium`）
  - 反证：同文件 `:137-146`（D1 Addendum）；`docs/MINECRAFT-PROTOCOL.md:320-329`、
    `docs/MINECRAFT-RESIDENT.md:505-509`、`docs/MINECRAFT-ACCEPTANCE.md:70-77`（当前已收口）；
    `docs-fixes.md:216-218`（自认保留 §9）
- **Reason**：报告是 handoff 入口；顶部/§9 的旧状态会让读者以为文档仍需改写，或按已失效行号改文档。
  Addendum 已解决 D1，但没同步报告结论块；这是状态漂移，不是离线证据失败。按前一轮审计 F3 的定性
  仍属 should-fix，不构成 blocking。
- **Severity**：**should-fix**（handoff 状态一致性；不阻塞 `READY OFFLINE`）。

### F3 — `11-guard-artifact.log` 的 summary 仍写 7，文档写 8，引用时没有在每个落点注明 raw scan 优先（nit）

- **Problem**：`11-guard-artifact.log:98-99` 的 remap proof summary 写「7 intermediary Class refs」
  且样本漏掉 `class_3324`；同一文件 `:82-83` 的 raw scan 是 `intermediary(remapped)_refs=8`，样本含
  `class_3324`。当前文档/报告按 raw scan 写 8（正确），最终报告 E10 `:84` 也明确说摘要与 raw scan
  不一致、以 raw scan 为准；但 `MINECRAFT-ACCEPTANCE.md:45` 与 `MINECRAFT-RESIDENT.md:82-83` 只引用
  日志路径，没有重复这句限定，读者打开被引日志会看到 7/8 两个数字。
- **Location**：
  - `reports/minecraft-review/final-verify/11-guard-artifact.log:98-99` vs `:82-83`
  - `docs/MINECRAFT-ACCEPTANCE.md:45`
  - `docs/MINECRAFT-RESIDENT.md:82-83`
  - 反证/限定：`reports/minecraft-review/final-verification-2026-11-02.md:84`（raw scan 优先的说明）；
    `docs-residual-fixes.md:140-141`（该日志摘要未改的残余说明）
- **Reason**：实质结论（8 个 intermediary、`MinecraftServer` 为 official identity mapping、
  0 yarn-exclusive）成立，且原始样本与独立扫描都支持 8；问题只是被引日志自身摘要未同步、文档落点
  没有重复限定。属证据可复核性，不是事实反转。
- **Severity**：**nit**。

### F4 — 农田 4.5 的度量基准是 `move` 目标格，不是 `GoalNear` 后的实际站位（nit）

- **Problem**：`_project_steps` 在选定的 stand 格上用 `distance <= 4.5` 过滤土壤
  （`minecraft_resident.py:3025-3047`），生成的 `move` 带 `radius:1.0`（`:3048-3050`）；而
  `adapter.js` 的 `farm` 用 `loadedBlock(a)`，其中 `nearby` 比较的是**实际 bot 位置**到土壤坐标的
  距离（`adapter.js:128-129, 238-254`），`move` 使用 `GoalNear(..., radius)`（`adapter.js:147-150`）。
  因此 bot 停在 stand 半径 1 格内的另一侧时，stand 距土壤 4.123 的土仍可能距实际站位 > 4.5。
  `farm_stand_reach_probe.py:45-55` 与 `test_opposite_corner_soil_stays_within_the_child_reach_from_stand`
  （`test_minecraft_resident.py:1597-1601`）都从 `move` 目标格测量，所以文档
  「生成的农田步骤不会再被子进程拒绝」（`MINECRAFT-RESIDENT.md:348`）比已证明的保证更强。
- **Location**：
  - `docs/MINECRAFT-RESIDENT.md:348`
  - `docs/MINECRAFT-ACCEPTANCE.md:70`（G3 行「与 adapter.js loadedBlock(p,4.5) 对齐」）
  - `app/pawbot/minecraft_resident.py:3025-3047`、`:3048-3050`
  - `app/minecraft-sidecar/src/adapter.js:128-129, 147-150, 238-254`
  - 证据测量基准：`reports/minecraft-review/probe/farm_stand_reach_probe.py:45-55`；
    `app/tools/test_minecraft_resident.py:1597-1601`
- **Reason**：这是「绝对保证」的措辞/证据基准问题，不否定已修正的「无 `y-1` 回退、无土返回 []
  并回退 `shelter`」；也不影响 E2/E3 的实测数字。若要保留「不会拒绝」的强断言，应把 4.5 过滤改成
  以最优站位加 `GoalNear` 半径（或更保守的 margin）为基准；属于离线边缘，实机寻路本属 L1/L2。
- **Severity**：**nit**。

### F5 — 被引撤销探针的文件头仍描述旧的 false-success 行为（nit）

- **Problem**：`revoke_durability_false_success.py:14-16` 仍写「The second emergency stop then reports
  `ok: true` …」，但当前 E1 输出是 `no false success`，探针自己的断言（`:159-168`）也要求
  两次 `ok` 都不为 true。最终报告 §5 只对 `habit_formation_probe.py` 的过期文件头作了说明，没有
  提这个探针；它被 `MINECRAFT-RESIDENT.md:411-413` 和报告 E1 作为证据引用。
- **Location**：
  - `reports/minecraft-review/probe/revoke_durability_false_success.py:14-16`
  - 对照：`reports/minecraft-review/final-verify/01-revoke-durability.log:17`
    （`no false success`）；探针断言 `:159-168`
- **Reason**：探针输出与断言正确，只有文件头注释过期；属于复核工件，不是仓库 docs 主张。
  与报告 §5 对另一探针文件头的处理标准一致，记为 nit。
- **Severity**：**nit**。

## 3. 是否有 blocking 文档矛盾，以及 READY OFFLINE 是否成立

- **没有 blocking 文档矛盾。** 五份文档对 (a)–(g) 的核心语义与当前源码、被引日志一致；没有发现
  把 L1–L8 实机项写成已验证、或把 `READY OFFLINE` 写成整体 `READY` 的语句。
- **F1/F2 是 should-fix**：前者是居民会话结果的字段名（`revoke_error` vs `error`），后者是最终报告
  顶部/§9 的 D1 状态漂移。两者都不反转撤销/紧急停止的安全语义，也不否定离线证据；应在 handoff
  前修正措辞或补限定。
- **F3/F4/F5 是 nit**：artifact 日志摘要与文档落点的引用提示、农田 4.5 的度量基准、被引探针的
  过期文件头；都不改变 `READY OFFLINE` 的离线结论。
- **`READY OFFLINE` 对当前树的离线范围仍然准确**：聚焦 201、全量 2179（2 个非 Minecraft skip）、
  侧车 115、守卫 63、jar 70,856 B/`B373…A82F`、基线 19/19 与 46/0/0、E6 16/32/15、E13 61/39/
  `late200=0` 均可在当前日志或当前 jar 上复现；源码哈希与最终复核快照一致。文档修正没有把实机
  项升级，也没有新增源码缺陷。

## 4. 只读核对命令（摘要）

```powershell
# 源码/文档 mtime、快照
Get-ChildItem app\pawbot\core.py, app\pawbot\minecraft_resident.py, ...
Get-FileHash -Algorithm SHA256 -LiteralPath <10 个关键源码文件>   # 与 final-verification §1 一致
Get-FileHash -Algorithm SHA256 -LiteralPath minecraft-guard\build\libs\pawbot-guard-1.0.0.jar

# 日志计数（只读）
Select-String -LiteralPath reports\minecraft-review\final-verify\05-focused-minecraft-python.log -Pattern '^Ran \d+ tests|^OK'
Select-String -LiteralPath reports\minecraft-review\final-verify\06-full-python-discovery.log -Pattern '^Ran \d+ tests|^OK \('
(Get-Content reports\minecraft-review\final-verify\06-full-python-discovery.log | Where-Object {$_ -match 'RuntimeWarning'}).Count
(Get-Content ... | Where-Object {$_ -match ': RuntimeWarning:'}).Count
(Get-Content ... | Where-Object {$_ -match 'Coroutine created at'}).Count

# 当前 jar / 关键源码 / 文档措辞
Get-FileHash minecraft-guard\build\libs\pawbot-guard-1.0.0.jar
Select-String docs\MINECRAFT-*.md -Pattern 'y-1|不会签发或发布许可|error:"already_revoked_unverified"|55/45|READY'
```

未重跑四个 Python 探针、Java 探针或 Node 探针；本文件是唯一写入。

> 本复审计只读；除本文件外未修改 docs/source/tests/config 或其它 reports。
