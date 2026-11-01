# Minecraft 文档一致性修复记录（2026-11-02）

> 角色：文档修复者，不是验证者。输入是 `reports/minecraft-review/docs-consistency-audit.md`（审计）；
> 每条修复都先对照**当前源码**或**被引日志**核验，未直接照抄审计结论。
> 写入范围只有本文件与下列 3 个文档；**未改**源码/测试/配置/AGENTS/其它 docs/审计报告，
> 未启动游戏/浏览器/微软登录，未读取真实凭证、人设、记忆或原始存档，未部署、未重启服务。
> 未重跑四个离线探针；只读源码与现有日志（需求即离线文档修复）。

## 0. 变更文件

| 文件 | 改了什么 |
| --- | --- |
| `docs/MINECRAFT-RESIDENT.md` | F1（§11 撤销语义）、F2（§9.4 农田无观测行为）、F4（§4.1 重映射计数）、F7（§12 闩锁顺序）、F8（§5 配置别名）、F9（§4.3 静默关闭） |
| `docs/MINECRAFT-ACCEPTANCE.md` | F2（§5 M4 行）、F4（§2 E10 行）、F5（§2 补充说明的 warning 计数） |
| `reports/minecraft-review/final-verification-2026-11-02.md` | F3（D1 Addendum）、F4（§2 E10 与 §9 remap 行）、F5（E6 warning 计数）、F6（E13 删除无日志分句） |
| `reports/minecraft-review/docs-fixes.md` | 本记录 |

## 1. Blocking

### F1 — 已进入守卫服务器线程的请求：不是「不会被打断」，而是「复制可能跑完、许可绝不签发/发布」

- **旧文（`docs/MINECRAFT-RESIDENT.md:396`）**：
  「已经进入守卫服务器线程的那一个请求不会被撤销打断；撤销保证的是「不再签发新许可 + 断开 actor」……」
  紧接的 `:397` 又写「守卫现在会在签名前再次检查撤销代次，发布也会与撤销串行化」。
- **新文（`docs/MINECRAFT-RESIDENT.md:396-399`）**：
  「已经进入守卫服务器线程的**复制**可能继续跑完，但该请求不会签发或发布许可：撤销代次（`revocationGeneration`）会在构建、签名前和发布前重新检查，
  排队中/构建中/签名前/发布前的请求都会得到 `409 revoked`，`SnapshotGate.commit` 也会拒绝，绝不发布许可。
  撤销保证的是「不再签发新许可 + 尽力断开 actor」（回执不确认 actor 已被踢出），不是「这一 tick 世界绝不会再变」。
  只有撤销前**已经返回**给客户端的 5 秒许可仍是既有语义：不会被追溯作废，也不是租约。」
- **源码证据（逐条核对当前树）**：
  - `minecraft-guard/src/main/java/dev/pawbot/guard/PermitService.java:382-388`：进入许可处理时先在同一临界区检查撤销闩锁并抓取 `revocationGeneration`；撤销后新请求在这里就得到 `409 revoked`。
  - `PermitService.java:518-529`（`build`）：服务器线程入口再次 `refreshRevocation()` 与 `generationCurrent()`，不符即 `409 revoked`——这正是「排队/复制阶段被打断」。
  - `PermitService.java:614-623`（`signIfCurrent`）：签名前最后一次复核代次，不符即 `409 revoked`，签名不会发生。
  - `PermitService.java:462-493`（`finish`）：发布前在 gate 临界区内复核代次；`SnapshotGate.commit` 拒绝已撤销/过期 ticket，`publish` 不会执行。
  - `minecraft-guard/src/main/java/dev/pawbot/guard/SnapshotGate.java:139-154`：`commit` 检查 `revoked`、ticket 代次与 gate 代次，任一不符返回 `false`，快照不发布。
  - `docs/MINECRAFT-PROTOCOL.md:169-176` §1.5：撤销不能追溯作废已返回许可；排队或 save/copy 中的请求得到 `409`，快照永不发布，复制本身可能跑完；保证是「no new permit + best-effort kick」。
  - `docs/MINECRAFT-ACCEPTANCE.md:104`（M1）：排队/构建/签名/发布都校验，E13 `late200=0`。
- **结论**：旧句与协议 §1.5、验收 M1、`PermitService` 三处相反，属于 blocking 文档错误；源码不需要改。已按上述语义重写，并保留「尽力断开 actor」边界。

## 2. Should-fix

### F2 — 无观测土壤时没有 `y-1` 回退，项目回退 `shelter`

- **旧文**：
  - `docs/MINECRAFT-RESIDENT.md:347`：「……没有可用观测时才回退到脚下 `y-1`。」
  - `docs/MINECRAFT-ACCEPTANCE.md:107`（M4）：「已改为优先观测到的 `y-1` 土壤……无观测时回退 `y-1`」。
- **新文**：
  - `docs/MINECRAFT-RESIDENT.md:347`：「居民只使用观测到的、在脚下 1 格（`y-1`）且水平 2 格内的土壤，跳过已有作物的列；
    没有观测到可种植土壤时不生成 farm 步骤，整条项目回退到 `shelter`（E2/V2），不会去猜脚下的方块。」
  - `docs/MINECRAFT-ACCEPTANCE.md:107`（M4）：「只使用观测到的 `y-1` 土壤……没有观测到土壤就不生成 farm 步骤，项目回退 `shelter`（V2/E2）」。
- **源码证据**：
  - `app/pawbot/minecraft_resident.py:2940-2951`：`_farm_soil` docstring 明确「No observed soil means no farm step, so `_ensure_project`
    can fall back to shelter」；并说明猜 `y-1` 可能落到 stone/gravel/air，被侧车以 `not_soil` 拒绝。
  - `:2984-2990`：只接受观测块中 `sy == fy - 1` 且水平 `max(|dx|,|dz|) <= 2` 的土壤，且作物列已占用则跳过。
  - `:2994-2997`：有可用土壤才返回最多 2 块，否则 `return []`。
  - `:3005-3008`：`_project_steps("farm")` 在 `soils` 为空时直接 `return []`。
  - `:2927-2931`：`_ensure_project` 在 farm 步骤为空时把 `kind` 改为 `shelter` 并重新生成步骤。
- **日志证据**：`reports/minecraft-review/final-verify/02-farm-fallback.log:9-11`：`kind = shelter`，5 步，
  `fallback behaved as claimed`，exit 0——证明的是「回退 shelter」，不是「回退 y-1」。
  `docs/MINECRAFT-ACCEPTANCE.md:71` 的 V2 行、`docs/MINECRAFT-RESIDENT.md:349` 的「无土可种时整条项目回退 `shelter`」本来正确，未改。
- **结论**：旧 M4 行与同文档 V2 行互相矛盾，且无当前日志支持；已删除两处不存在的 `y-1` 回退说法。

### F3 — 报告 D1 已过期：保留原文，加 Addendum 说明已由 00:46–00:51 文档定稿解决

- **旧文**：`reports/minecraft-review/final-verification-2026-11-02.md:119-135` 的 D1 仍把三份 owner 文档描述为
  「G3/G4/G8 未修、协议 §4 仍写 409 可 `ok:true`、验收表仍是 PENDING」，并给出已失效行号；
  报告 mtime 为 **00:35:02**，早于文档定稿。
- **新文**：在 D1 的 Severity 之后新增 `:137-146` 的
  「> **Addendum 2026-11-02 01:00+ — D1 已由文档定稿解决（上面的 Problem/Evidence/Severity 保留为
  00:16–00:30 快照的历史发现，不改写）。**」：列出三份文档的定稿时间（PROTOCOL 00:47:52、RESIDENT 00:50:00、
  ACCEPTANCE 00:51:00），说明 G3/G4/G6/G7/G8/V1/V2 已标为已修复/已关闭、协议 §4 已改为「裸 409 只是内存闩锁」、
  验收表已由 E1–E14 取代 PENDING；明确「D1 不再是 handoff 待办项，旧行号已失效，不要按它们改文档」；
  链接 `documentation-final.md` 与三份当前文档。
- **证据（文件时间 + 定稿记录，未改原文任何一句）**：
  - `Get-ChildItem` 实测：`docs/MINECRAFT-PROTOCOL.md` 00:47:52、`docs/MINECRAFT-RESIDENT.md` 00:50:00、
    `docs/MINECRAFT-ACCEPTANCE.md` 00:51:00、`reports/minecraft-review/documentation-final.md` 00:51:38；
    报告原 mtime 00:35:02。
  - `reports/minecraft-review/documentation-final.md:21-56,75-90` 记录了三份文档的定稿内容（PENDING 表删除、
    G3/G4/G6/G7/G8/V1/V2 收口、协议 §4 重写）。
  - 定稿后的验收 `§3`（`:70-77`）与居民 `§15`（`:507-509`）确实已把上述项标为已修复/已关闭。
- **结论**：D1 作为 00:16–00:30 快照的历史发现保留原文；Addendum 只做状态标注，未改写原 verdict/证据。
- **刻意未改**：报告 `§9:291`「唯一需要先处理的是文档 D1」与 `:310`「findings=1 documentation drift (D1, Medium)」
  属于原 verdict/结论块，按 F3 的「不要重写原 verdict 或证据」保留；以 D1 Addendum 为准阅读。见 §5 残余 2。

### F4 — 重映射计数 7 → 8：含 `class_3324` PlayerManager，`MinecraftServer` 是 official identity mapping

- **旧文**：
  - `reports/minecraft-review/final-verification-2026-11-02.md:84`（E10）：「最终 jar 有 **7 个 intermediary Class 引用**
    （……）**0 个仅 yarn 的引用**；dev jar 对照有 7 个 yarn 引用」。
  - `reports/minecraft-review/final-verification-2026-11-02.md:304`（§9 remap 行）：`remap=7 intermediary Class refs`。
  - `docs/MINECRAFT-ACCEPTANCE.md:45`（E10 行）：「**7 个 intermediary Class 引用 / 0 个 yarn-only 引用**」。
  - `docs/MINECRAFT-RESIDENT.md:83`（§4.1）：「7 个 intermediary / 0 个 yarn-only 引用」。
- **新文**：
  - 报告 E10：「**8 个 intermediary Class 引用**（`class_1132`=IntegratedServer、`class_2561`=Text、
    `class_310`=MinecraftClient、`class_3222`=ServerPlayerEntity、`class_3244`=ServerPlayNetworkHandler、
    `class_3324`=PlayerManager、`class_5218`=WorldSavePath、`class_5250`=MutableText）；另有 1 个 yarn 名引用
    `net/minecraft/server/MinecraftServer`，它在 1.20.1 是 official/intermediary/yarn 三列相同的 official identity mapping；
    **0 个 yarn-exclusive 引用**；……最终 jar 计数以 raw scan 为准」。
  - 报告 §9：`remap=8 intermediary Class refs / 0 yarn-only refs ...`。
  - 验收 E10 行：「**8 个 intermediary Class 引用（含 `class_3324` PlayerManager）/ 0 个 yarn-only 引用**；
    另有 1 个 yarn 名引用 = `MinecraftServer`（official identity mapping）」。
  - 居民 §4.1：「8 个 intermediary / 0 个 yarn-only 引用；另 1 个 yarn 名引用 `MinecraftServer` 属 official identity mapping」。
- **日志证据（`reports/minecraft-review/final-verify/11-guard-artifact.log`）**：
  - `:80`：`dev_jar_intermediary_refs=0 dev_jar_yarn_like_refs=0 sample=`（dev jar 的 raw 计数本身是 0/0）。
  - `:82`：`intermediary(remapped)_refs=8`；`:83` 的样本包含
    `class_1132,class_2561,class_310,class_3222,class_3244,class_3324,class_5218,class_5250`（含 `class_3324`）。
  - `:84-87`：`yarn_like_refs=1`、`yarn_like_sample=net/minecraft/server/MinecraftServer`；
    `mojmap_like_refs=1`、同一样本。
  - `:96`：`mappings.tiny` 的 class 行 official == intermediary == yarn，证明 `MinecraftServer` 是 identity mapping。
  - `:98-99`：同文件 summary 段写「7 intermediary Class refs」且样本漏掉 `class_3324`，与 `:82` 的 raw scan 矛盾；
    日志里 dev jar 的「7 个 yarn 引用」只出现在这段 summary 列举中。报告 E10 现已注明「以 raw scan 为准」，保留日志引用。
  - 审计另用 Python 独立扫描当前 jar 常量池同样得到 8（`docs-consistency-audit.md:97-98`）。
- **结论**：实质结论（已重映射、无 yarn-exclusive 引用）不变，但计数必须按 raw scan 写 8；`MinecraftServer`
  不再被含混地算作「纯 yarn-only」，而是单列为 official identity mapping。四处旧数字已全部修正。

## 3. Nit（逐条核验后只改真错的地方）

### F5 — E6 warning 计数 33 → 16 条（32 行含 `RuntimeWarning`）

- **旧文**：报告 `:97-98`「另有 33 条 `RuntimeWarning`（无关模块 `proactive`/`turns` 的未 await 协程）」；
  验收 `:54`「日志里 33 条 `RuntimeWarning`（无关模块）」。
- **新文**：报告 `:97-100`「**16 条**未 await 协程 `RuntimeWarning`（日志共 32 行含 `RuntimeWarning`，其中 16 行是
  `Enable tracemalloc`；来源为 `proactive.py:423`、`unittest.mock` 内部、`unittest.case` 与
  `app/plugins/ai_chat.py:584`，没有 Minecraft 模块）与对应的 `Coroutine created at ...` 堆栈（15 条）」；
  验收 `:54` 同步改为「16 条未 await 协程 `RuntimeWarning`（32 行含该词，无关模块）」。
- **日志证据（`reports/minecraft-review/final-verify/06-full-python-discovery.log`）**：
  - 含 `: RuntimeWarning:` 的行 = **16**；含 `RuntimeWarning` 的行 = **32**（16 条 warning + 16 行
    `Enable tracemalloc`）；`Coroutine created at` = **15**。
  - 抽样来源：`proactive.py:423`、`unittest/mock.py`（494/799/2247）、`unittest/case.py:606`、
    `app/plugins/ai_chat.py:584`；没有 Minecraft 模块。
  - `^FAIL:`/`^ERROR:` = **0**，摘要仍是 `Ran 2179 tests ... OK (skipped=2)`、exit 0。
- **结论**：旧数字无法从日志复现且来源描述不完整；已按实测改写，结论「既有噪声、不影响 OK」不变。

### F6 — 删除 E13「另一次复跑 55/45」

- **旧文**：报告 `:87`（E13）：「……`connect200=61 connect409=39 connectOther=0 late200=0`
  （另一次复跑 55/45，同样 `late200=0`）」。
- **新文**：删除括号分句，只保留 `rounds=100 connect200=61 connect409=39 connectOther=0 late200=0`。
- **日志证据**：`reports/minecraft-review/final-verify/12-java-probes.log:21` 是仓库里唯一的 100 轮压力日志，
  内容为 `rounds=100 connect200=61 connect409=39 connectOther=0 late200=0`；全仓 `*.log`/`*.md` 扫描中
  除本报告与审计外没有 `55/45`，其它旧日志是 75/25、70/30、66/34。无日志的「另一次复跑」不能作为可复现证据。

### F7 — 紧急停止顺序：先本地闩锁/关闸，再撤销，再停/回收

- **旧文**：`docs/MINECRAFT-RESIDENT.md:434`「成功语义：先确认守卫撤销，再取消并回收居民/侧车（在途技能被隔离、不重放），
  并闩锁到 PawBot 重启。」
- **新文**：`docs/MINECRAFT-RESIDENT.md:434`「成功语义：先置本地闩锁（`_minecraft_estop`，先关闸），再确认守卫撤销，
  再取消并回收居民/侧车（在途技能被隔离、不重放）；回执要求 `latched`/`revoked`/`revoke_durable`/
  `resident_stopped`/`sidecar_reaped`/`writes_drained` 全为真且无 `error`，闩锁保持到 PawBot 重启。」
- **源码证据**：`app/pawbot/core.py:600`：`self._minecraft_estop = True` 在任何撤销/停止之前；
  `:665-672`：`ok` 精确要求 `latched`、`revoked`、`revoke_durable`、`resident_stopped`、`sidecar_reaped`、
  `writes_drained` 全真且没有 `error`。`_minecraft_stop_session` 内部顺序仍是「revoke → stop → reap」（`:534-579`），
  但它发生在宿主闩锁之后，所以整体顺序是「闩锁 → 撤销 → 停止/回收」。
- **结论**：按源码改写。`docs/decisions-minecraft-resident.md:33` 的「先撤销……最后闩锁」是同一矛盾的另一个位置，
  但不在本次写入范围（见 §5 残余 1）。

### F8 — 配置登记：除旧别名 `decision_cost_usd` 外无未登记读取键

- **旧文**：`docs/MINECRAFT-RESIDENT.md:152`「`config.DEFAULT` 里 `minecraft` 段的**全部**键与默认值
  （当前源码已全部登记；没有「代码读了但没登记」的键）」。
- **新文**：`docs/MINECRAFT-RESIDENT.md:152`「……（下表 33 个键均已在 `DEFAULT` 登记；代码还会读旧别名
  `minecraft.decision_cost_usd`，它只作 `decision_cost_usd_per_call` 的回退、不在 `DEFAULT` 里，见本节末——
  除此之外没有「代码读了但没登记」的键）」。
- **源码证据**：`app/pawbot/config.py:600-634` 的 `DEFAULT.minecraft` 正好 33 个键，**没有** `decision_cost_usd`；
  `app/pawbot/minecraft_resident.py:1338-1339` 用 `_cfg(config, "decision_cost_usd_per_call",
  _cfg(config, "decision_cost_usd", None))` 读取旧别名。同文档 `:206` 本来就写了该别名，前后现已一致。
  §13 `:467` 的「33 个键」基线说明未变（登记别名会改变 33 键基线与测试，不能只改文档，故不登记）。

### F9 — 配置缺失 / `enabled=false` 时静默关闭，不打印 `refused:`

- **旧文**：`docs/MINECRAFT-RESIDENT.md:138-139`「配置缺失、`enabled=false`、世界未标记/标记不符/路径不符、
  备份根不可用、端口被占，都只会打印静态 `refused: ...` 并保持关闭」。
- **新文**：`docs/MINECRAFT-RESIDENT.md:138-139`「配置缺失或 `enabled=false` 时**静默保持关闭**（不打印 `refused:`）；
  世界未标记/标记不符/路径不符、备份根不可用、端口被占等其余拒绝会打印静态 `refused: ...` 并保持关闭」。
- **源码证据**：`minecraft-guard/src/main/java/dev/pawbot/guard/GuardMod.java:69-72`：配置文件不是普通文件时
  注释「normal off state」并直接 `return`，无日志；`:80-82`：`if (!loaded.enabled) return;`，无日志。
  其余拒绝确实打印：`:77`（加载失败）、`:87`/`:101`（revoked 标记）、`:95`（backup root unavailable）、
  `:112`（bind failed on port）、`:176-202`（save path/marker/path）。
- **结论**：旧文会让排障者以为「没看到 `refused:` 就是日志丢了」；按源码区分配置缺失/关闭与真正的拒绝。

## 4. 修复后审计检索复跑

对五份文档（`MINECRAFT-PROTOCOL/RESIDENT/ACCEPTANCE`、`ARCHITECTURE-BASELINE`、`decisions-minecraft-resident`）
与 `final-verification-2026-11-02.md` 重新执行审计用词的检索（PowerShell `Select-String`，区分大小写、按字面量）：

| 检索词 | 命中 | 结论 |
| --- | --- | --- |
| `不会被撤销打断` | **0** | F1 旧句已消失 |
| `不会签发或发布许可` | 1（RESIDENT:396） | F1 新句在位 |
| `回退到脚下` / `无观测时回退` | **0 / 0** | F2 两处旧说法已消失 |
| `项目回退到` | 2（RESIDENT:347、349） | F2 新说法与 V2 行一致 |
| `项目回退 `（后接空格/占位符） | 1（ACCEPTANCE:107） | M4 新说法 |
| `7 个 intermediary` / `7 intermediary` | **0 / 0** | F4 旧计数已消失 |
| `8 个 intermediary` | 3（RESIDENT:83、ACCEPTANCE:45、报告:84） | F4 三处证据行已改 |
| `8 intermediary` | 1（报告:304） | F4 §9 remap 行已改 |
| `33 条` | **0** | F5 旧计数已消失 |
| `16 条` | 5（ACCEPTANCE:54、报告:97、ARCHITECTURE-BASELINE:31/231/385） | 前两者是 warning；后三者是无关的「16 条 exact 子路径」 |
| `55/45` | **0** | F6 无日志分句已删除 |
| `先确认守卫撤销` | **0** | F7 旧顺序已消失 |
| `最后闩锁` | 1（decisions-minecraft-resident.md:33） | 不在写入范围；见 §5 残余 1 |
| `先置本地闩锁` | 1（RESIDENT:434） | F7 新顺序在位 |
| `都只会打印静态` | **0** | F9 旧说法已消失 |
| `静默保持关闭` | 1（RESIDENT:138） | F9 新说法在位 |
| `没有「代码读了但没登记」` | 1（RESIDENT:152） | 命中在修正句的否定语境（「除此之外没有……」），不是旧主张 |
| `只作 `decision_cost_usd_per_call` 的回退` | 1（RESIDENT:152） | F8 新说法在位 |
| `Addendum 2026-11-02` | 1（报告:137） | F3 Addendum 在位 |

补充：`y-1` 现在只剩两处正确用法（RESIDENT:347 与 ACCEPTANCE:107，均紧跟「无观测 → 不回退」）；
`intermediary` 的旧计数（7）已消失；报告 §5 的 identity-mapping 论证未改，且与 8 计数不冲突。

## 5. 刻意留下的残余（未改，附理由）

1. **`docs/decisions-minecraft-resident.md:33` 仍写「先撤销守卫授权，再取消并回收居民/侧车，最后闩锁」**：
   审计 F7 的主要位置之一，但本任务禁止改该文档。同文件 `:58` 已写「`minecraft_emergency_stop` 先关闸、再撤销/停止/回收」，
   与 `core.py:600` 一致；`MINECRAFT-RESIDENT.md:434` 已按源码改正。需要该文档 owner 后续统一。
2. **报告 §9 的 verdict 文本未改**：`:291`「唯一需要先处理的是文档 D1」与 `:310`
   「findings=1 documentation drift (D1, Medium)」保留。F3 要求「不要把原 verdict/证据改写」，因此只在 D1 加
   Addendum（`:137-146`）；读者应以 Addendum 为准，D1 已由 00:46–00:51 定稿解决。
3. **`reports/minecraft-review/documentation-final.md:103` 仍写「7 intermediary / 0 yarn-only」**：
   该报告不在写入范围；它是 00:51 定稿记录，现已与修正后的 E10/验收/居民文档不一致。属于后续 owner 的小修，
   不影响 `READY OFFLINE`（实质结论仍是「已重映射、0 yarn-exclusive」）。
4. **`11-guard-artifact.log` 自身摘要不一致未改**：`:82` raw scan 是 8，`:98-99` summary 写 7 且漏 `class_3324`；
   `:80` dev jar raw 计数是 0/0，而 `:99` 又列举 7 个 dev jar yarn 引用。日志是历史证据、不在写入范围；
   报告 E10 已明确「以 raw scan 为准」，并保留日志引用，供复核者看到矛盾来源。
5. **`app/pawbot/core.py:582` docstring 仍写「先撤销守卫授权，再停常驻/侧车，最后闩死不再重启」**：
   与 `:600` 的实际闩锁顺序矛盾；源码不在写入范围，也不是文档主张。居民手册已按代码写，未引用该 docstring。
6. **F2 的 `shelter` 语义在 `MINECRAFT-RESIDENT.md:347` 与 `:349` 各出现一次**：一处是农田行，一处是站位行，
   两处结论一致（无观测 → 不生成 farm 步骤 → 项目回退 `shelter`），保留重复以避免拆散上下文。
7. **未重跑探针**：本任务只做文档修复，且审计已以源码哈希与日志时序证明 00:17 的 E1–E4 日志晚于最后一次源码写入；
   没有新增证据日志，也没有把离线结论升级为实机验证。

## 6. 结论

- 审计的 1 项 blocking（F1）、3 项 should-fix（F2/F3/F4）、5 项 nit（F5–F9）中，
  **F1/F2/F4/F5/F6/F8/F9 已按源码与日志修正；F3 以 Addendum 方式解决**；F7 在可写文档内修正，
  其主位置的 decisions 文档按写入范围留给 owner（§5 残余 1）。
- 所有修改只涉及 3 份文档 + 本记录；没有改源码/测试/配置/AGENTS/其它 docs/审计报告，
  没有新增任何实机声明，`READY OFFLINE` 的范围（L1–L8 未验证）未被改写。
