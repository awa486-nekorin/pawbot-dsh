# Minecraft 最终离线验证报告（2026-11-02 快照）

**Verdict: READY OFFLINE.**

本报告对 **当前工作树** 独立复跑全部离线证据（9 组必跑命令 + 5 个 Java/Node 探针 + 10 项源码抽查），
不采信作者报告里的数字。所有必跑命令 exit 0；源码抽查与探针均与作者声称一致；**没有复现出源码缺陷**。
唯一需要处理的是**文档漂移**（owner 文档仍写着已修复的 G3/G4/G8 与旧协议语义）。该漂移已在
00:46–00:51 由文档 owner 按本报告 §7 的数字与结论定稿修复（见 §4 D1 Addendum），因此不再阻塞 handoff；
本报告保留 D1 的原始发现作为历史证据，不改变离线工程结论。实机项（Fabric 真实加载、Open-to-LAN、微软/离线认证、真实 Mineflayer 行为、
真实备份/恢复演练、30–60 分钟主人观察）**一项都没有做，也一项都不算已验证**（见 §6）。

- 角色：独立验证者，非作者。**未修改任何源码、测试、配置或文档**；本报告与
  `reports/minecraft-review/final-verify/*.log` 是唯一写入。
  `minecraft-guard/build/**` 被任务要求的 `gradle clean build` 重新生成（构建产物，不是源码）。
- 未做：真实游戏/浏览器/微软登录/真实存档/真实凭证/人设/记忆/部署/服务重启。所有世界、守卫端点、
  secret、UUID 均为合成临时数据。未委派子代理、未唤醒 teammate、未改模型路由或任何全局设置。
- 环境：工作目录 `D:\paw-old\pawbot-dsh`；Python 3.13.5、Node v24.14.0 / npm 11.9.0、
  JBR 21.0.8（`C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr`）、bundled Gradle 8.7
  （`minecraft-guard\.tooling\gradle-8.7`，`GRADLE_USER_HOME=minecraft-guard\.tooling\gradle-home`）。
  系统 PATH 无 JDK，未安装任何东西。
- 所有 Python 命令在 `D:\paw-old\pawbot-dsh\app` 下执行，统一 `PYTHONIOENCODING=utf-8`；
  Node/Java 命令在仓库根执行（另注明者除外）。日志均为 UTF-8。
  PowerShell 会把 unittest 的 stderr 横幅包成 `NativeCommandError`，那是包装层噪声；
  **以 exit code 与 unittest 的 `Ran ... / OK` 摘要为准**。

## 1. 快照与「移动的树」说明

本报告快照：**2026-11-02 00:16:40 +08:00**（首轮哈希），最后一轮复核结束 **00:30:20**，
期间关键文件 mtime/SHA-256 未变（00:16 与 00:30 两次一致）。最后一次 Minecraft 源码写入是
**00:06:39**（`minecraft_resident.py`）与 **00:07:54**（`test_minecraft_resident.py`）；
`reports/minecraft-review/fix-habit-formation.log` 的 00:13:06 是修复者日志，不是源码写入。

| 文件 | 大小 | mtime | SHA-256 |
| --- | --- | --- | --- |
| `app/pawbot/minecraft_resident.py` | 153,652 | 2026-11-02 00:06:39 | `3BB111ECA3CED18DDEF670774C0B0D025F663A73285C93FB8B6CC0ADD0B698D1` |
| `app/tools/test_minecraft_resident.py` | 104,626 | 2026-11-02 00:07:54 | `9911D20FC13A2B4794D018D766015AC6D26AEBE30FCD94D56F2F40E953A94758` |
| `app/pawbot/core.py` | 57,684 | 2026-11-01 23:35:59 | `4F45F12EA2190D3EFE362D4F1B425D7C6A4CF901D9AF8F513CC4B0B94B21A068` |
| `app/pawbot/minecraft_policy.py` | 8,265 | 2026-11-01 23:25:27 | `66700522DEFC2BCAF179BCE7648AE332093E93A17AAA64A6115D7CE9A2E46C8A` |
| `app/pawbot/minecraft_control.py` | 11,104 | 2026-11-01 23:39:29 | `5C346322E8CF684AFACC84F2BBEAE413F506B26C1238B41025B89C65EB2EA479` |
| `app/pawbot/minecraft_guard.py` | 14,274 | 2026-11-01 22:42:59 | `3CAF7AA495831529CD04057B226D64425A0C2B6C3D08BCDFA5B8DF86541C2C32` |
| `app/pawbot/minecraft_sidecar.py` | 21,456 | 2026-11-01 23:02:52 | `37CBF484AB08C3567E0B6D1A870E4844EC99FD1EA2A563567F28873884BAD33D` |
| `app/tools/test_minecraft_guard.py` | 25,193 | 2026-11-01 22:44:50 | `7C6CB33ADEE3BCA73402CC0E171E1CC8B944D0503B72BF0B375D3D8FE7C44D9F` |
| `app/tools/test_minecraft_policy.py` | 14,676 | 2026-11-01 23:28:06 | `7272F57B8C6E82D598BBAA606CCADEC58971F007F66C1C317AE535B37BD55E7C` |
| `app/tools/test_minecraft_control.py` | 29,675 | 2026-11-01 23:36:06 | `17964DB7C30806F6362AB873F4E0BCC2A61CDB41479C7F159183CB0CF52F423B` |
| `app/tools/test_minecraft_integration.py` | 21,835 | 2026-11-01 21:59:46 | `F59C123563112058AB9B47D3EBA83D5A0443374010B2EDA72EA1CDC39E8C9EB2` |
| `app/tools/test_minecraft_sidecar.py` | 19,270 | 2026-11-01 20:34:09 | `CCD927B2541A01F28FC2A777F8579051FE46056665BCDC55BBC6DE40EE80B9BA` |
| `minecraft-guard/src/main/java/dev/pawbot/guard/PermitService.java` | 36,313 | 2026-11-01 22:32:10 | `84442FD7D05F66A25C3B6B182E3AB757C7E4181188B4C42D55312179F5127B9B` |
| `minecraft-guard/src/test/java/dev/pawbot/guard/GuardSelfTest.java` | 100,121 | 2026-11-01 22:37:32 | `A180F262E36C24E734A783665CDCF3C7043502DFBD295743EC6B64E6481CF003` |
| `app/minecraft-sidecar/src/adapter.js` | 22,942 | 2026-11-01 21:26:28 | `16F699DE859FA614AD73D382E08BF9B031B972F06AB28446551487A7CBDF35B5` |
| `app/minecraft-sidecar/src/guard.js` | 8,162 | 2026-11-01 22:39:48 | `8DF942CC6031A26DE9B02DB60D1041A5BAA13460F301F615C11BF4490BD39A11` |
| `app/minecraft-sidecar/src/controller.js` | 10,491 | 2026-11-01 22:53:01 | `CE70835B31B315372E7520CED93D24AB042FEA097B06D3B1E7DD12336FA37D60` |
| `docs/MINECRAFT-PROTOCOL.md` | 24,116 | 2026-11-01 23:36:40 | `A1625BCEFEEEA7A86453157EBDC5183C9FC440AB6B30C37DCBE77FC86AC0A465` |
| `docs/MINECRAFT-RESIDENT.md` | 42,739 | 2026-11-01 23:14:56 | `EBE9BB2DC911967871EDFAEA2FC96186F82CB18E340F58C64AA5553B7BA094C7` |
| `docs/MINECRAFT-ACCEPTANCE.md` | 26,281 | 2026-11-01 23:15:58 | `2D46C6E3D27A6D9579D6BB5CCEB50B2B751D97CB1B7D794D31475C6D62C42CCD` |

仓库状态：`HEAD=3dff9ef7689666ca583b6f0d27df76c858a6d2d5`，`git status --porcelain` 共 **206** 条
（本次 Minecraft 交付与其它框架/业务脏改混在同一工作树；本报告只验证 Minecraft 范围）。

**移动树警告（对前序报告的影响）：**

1. `reports/minecraft-review/verified-final-2026-11-01.md`（23:53 快照）**早于** 00:06/00:07 的习惯修复，
   它列出的 **V1（习惯永不形成）已过期**；本报告在 §3/§4 用探针与源码证明其已修复。
2. `reports/minecraft-review/final-verify/` 下原有 5 个日志（00:02）同样早于 00:06/00:07 的源码写入，
   **不作为本报告证据**；本报告已用 00:16 之后的复跑覆盖 `00/01/02/03`，并新增
   `04-habit-formation / 05-focused / 06 / 06b / 07a / 07b / 08 / 09 / 10 / 11 / 12 / 13`。
   注意：`final-verify/04-focused-minecraft-python.log`（00:02:37，266 B）是那次中断尝试留下的**仅表头**
   文件，已由 `05-focused-minecraft-python.log` 取代，**不要把它当证据**（本报告未删除它，以免破坏原目录）。
3. `reports/minecraft-review/repaired-2026-11-01.md` 是 23:03 快照，更早，其中 R1/R2/R3/R4 均已被后续修复。
4. 作者在 `documentation-refresh.md` 里的「G3/G4/G8 未修」是 23:05 口径，**已过期**；见 §4 D1。

## 2. 实际执行的证据（命令 / 结果 / 日志）

| # | 命令（cwd 见注） | 结果 | 日志 |
| --- | --- | --- | --- |
| E1 | `py -3.13 reports\minecraft-review\probe\revoke_durability_false_success.py` | **exit 0**；`no false success`；`first={ok:false, revoked:false, revoke_durable:false, error:guard_unavailable, latched:true, session:none}`；`second={ok:false, already_revoked_unverified, revoke_receipt:already_revoked, revoke_durable:false}`；`no_resident_guard_requests=2`；`resident_single_call={ok:false, revoked:false, revoke_durable:false, resident_stopped:true}`；`resident_guard_requests=2` | `final-verify/01-revoke-durability.log` |
| E2 | `py -3.13 reports\minecraft-review\probe\farm_fallback_probe.py` | **exit 0**；`kind = shelter`；5 个步骤（move→collect dirt×4→place×2→eat）；`fallback behaved as claimed` | `final-verify/02-farm-fallback.log` |
| E3 | `py -3.13 reports\minecraft-review\probe\farm_stand_reach_probe.py` | **exit 0**；`stand=-2.0,64.0,2.0`，两块土 `distance=4.123 / 4.123`，`child_limit=4.5 ok=True`；`all generated farm steps are within child reach` | `final-verify/03-farm-stand-reach.log` |
| E4 | `py -3.13 reports\minecraft-review\probe\habit_formation_probe.py` | **exit 0**；`habit_keys=['greet_known','return_home','tend_farm']`；`return_home count=3`、`tend_farm count=3`、`greet_known count=3`；`candidate_branches_reachable=True` | `final-verify/04-habit-formation.log` |
| E5 | `cd app; py -3.13 -m unittest tools.test_minecraft_guard tools.test_minecraft_policy tools.test_minecraft_sidecar tools.test_minecraft_control tools.test_minecraft_integration tools.test_minecraft_resident -q` | **Ran 201 tests in 43.540s — OK**，exit 0，0 fail，0 skip | `final-verify/05-focused-minecraft-python.log` |
| E6 | `cd app; py -3.13 -m unittest discover -s tools -q` | **Ran 2179 tests in 288.260s — OK (skipped=2)**，exit 0，**0 failure / 0 error** | `final-verify/06-full-python-discovery.log` |
| E7 | `cd app/minecraft-sidecar; npm test` | **115 pass / 0 fail / 0 cancelled / 0 skipped / 0 todo**，`duration_ms=4798.5569`（wall 5334 ms），exit 0 | `final-verify/07a-sidecar-npm-test.log` |
| E7b | `node --check index.mjs auth.mjs src/{main,adapter,controller,guard,skills,auth}.js` | **8/8 exit 0** | 同上（07a 的 `--- syntax ---` 段） |
| E8 | `cd app/minecraft-sidecar; npm audit --offline --audit-level=moderate` | **found 0 vulnerabilities**，exit 0 | `final-verify/07b-sidecar-npm-audit.log` |
| E9 | `"<gradle-8.7>\bin\gradle.bat" -p minecraft-guard clean build --offline --no-daemon --console=plain`（`JAVA_HOME=JBR 21.0.8`，`GRADLE_USER_HOME=minecraft-guard\.tooling\gradle-home`） | **BUILD SUCCESSFUL in 18s**（wall 19.2 s，exit 0）；`> Task :remapJar` executed；**`passed=63 failed=0`**；`:test SKIPPED`（`build.gradle` 故意 `test { enabled = false }`，`check` 依赖 `selfTest`）；`7 actionable tasks: 7 executed` | `final-verify/10-guard-clean-build.log` |
| E10 | jar 产物检查（`Get-FileHash`；Python 解析 class 常量池；官方 merged jar 对照） | **70,856 B**；SHA-256 **`B373B470F6BDE30A01EEC5C9723340DF6FD81EA43459AA97552FD4AA29D7A82F`**（与 23:47 前序构建一致，clean build 可复现）；39 entries；**class major = 61**（Java 17）于 `GuardMod`/`PermitService`/`BackupManager`/`RestoreTool`/`SnapshotGate`/`IntegratedWorldAdapter`/`BackupSchedule`/`HttpGuard`；`fabric.mod.json`: `minecraft:1.20.1`, `java>=17`, `loader>=0.15.0`；**重映射证据：最终 jar 有 8 个 intermediary Class 引用**（`class_1132`=IntegratedServer、`class_2561`=Text、`class_310`=MinecraftClient、`class_3222`=ServerPlayerEntity、`class_3244`=ServerPlayNetworkHandler、`class_3324`=PlayerManager、`class_5218`=WorldSavePath、`class_5250`=MutableText）；另有 1 个 yarn 名引用 `net/minecraft/server/MinecraftServer`，它在 1.20.1 是 official/intermediary/yarn 三列相同的 official identity mapping；**0 个 yarn-exclusive 引用**；日志 remap proof 段的 dev jar 对照列举了 7 个 yarn Class 引用，但该摘要与 raw scan 不一致（`intermediary(remapped)_refs=8`，摘要写 7 且漏掉 `class_3324`），最终 jar 计数以 raw scan 为准 | `final-verify/11-guard-artifact.log` |
| E11 | `node scripts/architecture-baseline.mjs` | **PASS — 19 passed, 0 failed, 19 total**，exit 0 | `final-verify/08-baseline-checker.log` |
| E12 | `node test/architecture-baseline.mjs` | **46 passed, 0 failed, 0 skipped**，exit 0 | `final-verify/09-baseline-contract-tests.log` |
| E13 | `javac --release 17 ... probe/{RevokeDurabilityProbe,RevokeRaceProbe,RevokeRaceStressProbe,JunctionRestoreSafetyProbe}.java` + 逐个 `java -cp <probeclasses;main;test>` | javac exit 0；**RevokeDurabilityProbe**：`first_status=503 signature=false body=unavailable`、`first_latched=true`、`marker=false`、`second_status=409 signature=false body=revoked`、`restarted_revoked=false`；**RevokeRaceProbe**：`revoke_status=200 isRevoked=true`、`connect_after_revoke_status=409 body=revoked`；**RevokeRaceStressProbe 100**：`rounds=100 connect200=61 connect409=39 connectOther=0 late200=0`；**JunctionRestoreSafetyProbe**：`cleanupTemp victim_kept=true link_removed=true`、`prune removed=1 victim_kept=true old_backup_removed=true`、`restore target race refused=target exists... sentinel_intact=true`、`restore failure junction refused=IOException victim_kept=true staging_removed=true partial_target_removed=true`、`70-deep running ancestor refused=... target_absent=true`、`ALL PROBES PASSED`；全部 exit 0 | `final-verify/12-java-probes.log` |
| E14 | `node reports\minecraft-review\probe\node_busy_http_probe.mjs` | **exit 0**；`busy_observe_health=17 quarantined=false connected=true`；`busy_skill_code=-32008 stopCount=1 quarantined=false connected=true`；`revoke_detect_ms=2 quarantined=true`；`PROBE PASSED` | `final-verify/13-node-busy-http.log` |

**E6 的 2 个 skip（已在 `06b-skip-reasons.log` 复跑确认，非 Minecraft、非本机缺陷）：**

1. `tools.test_memes.SeedDataTests.test_seeded_book_is_usable` —
   `skipped '还没跑 tools/seed_memes.py'`（`PATH = app/data/memes.json` 不存在；全量套件用临时目录，不会生成它）。
2. `tools.test_ha_content.SnapshotTests.test_build_rejects_names_no_windows_peer_could_apply` —
   `skipped 'Windows 会自动剥掉结尾点 / 空格，构造不出这种文件名'`（该用例显式 `if os.name == "nt": self.skipTest(...)`）。

E6 日志里另有 **16 条**未 await 协程 `RuntimeWarning`（日志共 32 行含 `RuntimeWarning`，其中 16 行是
`Enable tracemalloc`；来源为 `proactive.py:423`、`unittest.mock` 内部、`unittest.case` 与 `app/plugins/ai_chat.py:584`，
没有 Minecraft 模块）与对应的 `Coroutine created at ...` 堆栈（15 条）；这些是**既有噪声**，`FAIL:`/`ERROR:`
计数为 **0**，`OK` 摘要成立。

## 3. 独立源码抽查（逐项，不采信作者结论）

| 抽查项 | 结论 | 证据（当前源码/复跑） |
| --- | --- | --- |
| 1. 撤销标记落盘失败 → 无签名 503，且**永远不能**报 `ok:true` | **成立** | Java：`PermitService.revoke()` 先 `revoked=true`/`revocationGeneration++`、`gate.revokeAll()`，再 `persistRevocationMarker()`；非 durable 直接 `error(503,"unavailable")`（无签名），只有 durable 才 `signed(...)`（`PermitService.java:648-671`）。Python：`core._minecraft_guard_revoke` 只有在 `request("revoke")` 正常返回（=签名回执）才 `return True`；`ok` 显式要求 `revoke_durable is True and revoked is True`（`core.py:497-532, 665-672`）。探针 E1/E13 双向确认 |
| 2. 裸的已认证 `409` 只是内存闩锁；durable 的「已撤销」必须有**本进程此前记录的签名回执**，且绑定**同一守卫身份** | **成立** | `GuardAlreadyRevoked` → 若 `identity in receipts` 才 `(True,"already_revoked")`，否则 `(False,"already_revoked_unverified")`（`core.py:519-525`）；`_guard_identity` 用 `(url, world_id, world_path, actor_uuid)` 精确匹配（`core.py:167-178`），receipts 只存本进程 `set`（`core.py:153-164`）；无居民分支同规则（`core.py:634-648`）。探针第二个 409 得到 `already_revoked_unverified` + `ok:false` |
| 3. 签名忙回执：字段精确、非变更、HMAC 认证、不可当许可 | **成立** | Java `signedBusy` 精确 16 字段、`can_write:false`、`can_pvp:false`、`backup_id:""`、`busy:true`、`retry_after_ms`、`attested_at`，并用 `Hmac.hmacSha256(config.secret, body)` 签名（`PermitService.java:790-816`）；busy 缓存只由成功 live attestation 填充（`:557-558`）；Node 先验 HMAC 再解析，`BUSY_KEYS` 精确 16 键、拒绝额外字段、`backup_id!==''`/`can_write`/`can_pvp` 即拒；observe 可返回 busy 观测，其它动作一律 `GuardBusy`（`guard.js:55-83`）；Python 同规则（`minecraft_guard.py:30-34, 192-214`）。E14：`busy_skill_code=-32008`、`stopCount=1`、不隔离、连接保持 |
| 4. `revoke` 不被快照阻塞 | **成立** | `handle()` 在 gate/busy/服务器线程判断**之前**：`if (action == Action.REVOKE) return revoke(nonce);`（`PermitService.java:414-417`）；`revoke()` 不触碰 `SnapshotGate` 的服务器线程，kick 在 daemon 线程（`:706-720`）。Java 自测 `revoke answers during a snapshot and the copy never publishes` 通过；E13 `RevokeRaceProbe` 撤销后 connect 409 |
| 5. 单一 Java 60–1800 s 备份调度；Node/Python 无重复周期快照 | **成立** | `GuardConfig` 默认 `backup_interval_sec=1800`，范围 60–1800（`GuardConfig.java:104-107, 182`）；`BackupSchedule` 同样拒绝 <60/>1800，`GuardMod` 是唯一 tick 调度点（`GuardMod.java:118,139-148`）；Node 只有 250 ms 守卫 watchdog（`controller.js:53`），无 backup 定时器；`backup` 仅由 Java 周期任务或 `connect` 触发；Python `_backup_interval_seconds()` 注释明确「advisory only，Java 是唯一调度」，resident 无任何 `"backup"` 动作发射（`minecraft_resident.py:1541-1549, 1847`） |
| 6. owner-human 身份精确、区别于 bot actor、两层判定一致且无冲突 | **成立** | 居民不再自实现：`_owner_human_uuid()` 直接 `return owner_human_identity(self.config)`（`minecraft_resident.py:1583-1594`）；policy 层要求精确规范化 UUID、必须 ≠ `owner_player_uuid`、拒绝 `owner_mappings` 与 `owner_qq_uid` 冲突/一对多（`minecraft_policy.py:77-120`）；`_role_for` 仅 actor→self、该精确 UUID→owner（`minecraft_resident.py:1626-1635`），`_owner_present` 只认同 UUID（`:1637-1639`）；`owner_mappings` 只产生布尔链接、不授权（`:1596-1620`） |
| 7. 农田 4.5 格在居民生成与侧车适配器之间一致 | **成立** | 居民 `FARM_SOIL_REACH = 4.5`，注释直接引用 `adapter.js loadedBlock(p, 4.5)`（`minecraft_resident.py:96-98`）；站位按「最坏距离最小」挑选后**丢弃超过 4.5 的土**（`:3025-3047`）；适配器 `loadedBlock(p, max=4.5)` 且 `farm` 以土壤方块 `(x,y,z)`、作物 `y+1` 校验（`adapter.js:129, 238-254`）。E3：4.123/4.123 ≤ 4.5 |
| 8. 语义习惯由**真实成功动作**形成，不是手工 seed | **成立** | `_HABIT_KEY_FOR_SKILL = {"home":"return_home","farm":"tend_farm","follow":"greet_known","chat":"greet_known"}`（`minecraft_resident.py:596-597`）；`_success()` 末尾 `_bump_habit(_HABIT_KEY_FOR_SKILL.get(skill, skill))`（`:2762`），`_success` 只从唯一成功路径调用（`:2638`）；习惯分支阈值 count≥2（`:1203-1216`）；`_bump_habit` 持久化计数（`:3148-3159`）。E4 直接驱动 `_success` 三次得到 3/3/3；测试 `test_successful_actions_form_semantic_habits_across_threshold` 明确 `habits.clear()` 后从动作形成（`test_minecraft_resident.py:1953-2007`） |
| 9. RestoreTool 不覆盖 / 不跟随 junction / 拒绝运行中世界的任意深度祖先 | **成立** | 目标已存在即拒绝（`RestoreTool.java:129,191`），发布用 `Files.createDirectory(finalTarget)` 原子「已存在即失败」再搬入（`:310-350`）；`rejectProtectedWorld`+`rejectOverlap` 双向包含都拒（`:244-262`）；`rejectRunningWorldAncestor` 逐级走到文件系统根、任一祖先含 `session.lock` 即拒（`:294-303`）；junction 用 `NOFOLLOW_LINKS` 检查、删除链接不下降（`:355-360`）。E13 全部场景通过（含 70 层祖先、竞态 target、junction 清理） |
| 10. 家 seed 与无居民紧急停止诚实 | **成立** | `_home_seeded` 初始 False、重连复位（`minecraft_resident.py:1502, 1727`）；`_ensure_home_seed` 只在站在持久化 home 3 格内才 `set_home`（`:1907-1936`），失败只累加 `_home_seed_failures` 并 `{"seeded": False}`，成功才置 True（`:1936-1951`）；`home` 只在 seeded 后进入可选技能（`:2130, 2359`），`set_home` 从 affordance 移除（`:2347-2348`）。无居民：只有**完全没有 secret** 才是 `ok:false + noop + revoke_skipped:"no_guard_configured"`，其余一律 `ok:false`（`core.py:611-662`）；E1 复跑 `session:"none"`、两次调用都 `ok:false` |

## 4. 发现（Problem / Location / Reason / Severity）

### D1 — 文档仍描述已修复的行为（文档漂移）

- **Problem：** 三份 owner 文档仍把 **G3 农田可达**、**G4 撤销持久化假成功**、**G8 owner-human 双实现**
  列为未修，并且 `MINECRAFT-PROTOCOL.md` §4 仍写「已认证 409 可令 `ok:true`」；验收文档的证据表还是
  `PENDING FINAL INDEPENDENT SNAPSHOT`，E8 仍写「探针 exit 1」。当前源码与探针全部相反。
- **Location：**
  - `docs/MINECRAFT-PROTOCOL.md:310-318` — 「A verified signed receipt **or an authenticated `409 revoked`**
    sets `revoked: true` and can make `ok: true`」+「`503`→`409` can still report a stop that a restart
    forgets … residual in `repaired-2026-11-01.md` §R1」。
  - `docs/MINECRAFT-RESIDENT.md:343-344, 400-401, 445, 482-488` — 4.583 > 4.5 的 G3；
    409 当已确认的 G4；「不要把 ok:true 当重启后仍撤销」的旧语义。
  - `docs/MINECRAFT-ACCEPTANCE.md:23`（PENDING 表头）、`:42`（E8 `OUT_OF_REACH` 失败证据）、
    `:60`（G3）、`:61`（G4）、`:65` 与 `:146`（G8）、`:40`（E6 旧 75/25 数字）。
- **Reason：** 文档最后写入是 23:14–23:36，而源码修复持续到 00:07；文档 owner 没有拿到修复后的复跑数字。
  **不是代码缺陷**：E1–E14 全部通过，§3 源码抽查逐条支持修复后的语义。
- **Severity：** **Medium（文档/handoff，非代码阻塞）**。在定稿前，文档不得被引用为「G3/G4/G8 仍开放」或
  「409 可成功」的证据；owner 文档会因此低估紧急停止与农田路径的现状。

> **Addendum 2026-11-02 01:00+ — D1 已由文档定稿解决（上面的 Problem/Evidence/Severity 保留为
> 00:16–00:30 快照的历史发现，不改写）。** 三份 owner 文档在报告写就**之后**的 00:46–00:51 完成定稿
> （`MINECRAFT-PROTOCOL.md` 00:47:52、`MINECRAFT-RESIDENT.md` 00:50:00、`MINECRAFT-ACCEPTANCE.md` 00:51:00）：
> G3/G4/G6/G7/G8/V1/V2 已标为已修复/已关闭，协议 §4 已改为「裸 409 只是内存闩锁」，
> 验收表已由 E1–E14 取代旧 PENDING 表。因此 **D1 不再是 handoff 待办项，上面列出的旧行号也已失效，
> 不要按它们改文档**。定稿记录见
> [`documentation-final.md`](documentation-final.md)；当前文档见
> [`docs/MINECRAFT-ACCEPTANCE.md`](../../docs/MINECRAFT-ACCEPTANCE.md)、
> [`docs/MINECRAFT-RESIDENT.md`](../../docs/MINECRAFT-RESIDENT.md)、
> [`docs/MINECRAFT-PROTOCOL.md`](../../docs/MINECRAFT-PROTOCOL.md)。

### R1 — 没有单一 Java→Node 真实 socket 的 busy/revoke 集成测试（残余覆盖缺口）

- **Problem：** Java busy/revoke 由 Java 自测覆盖，Node 侧由真实 loopback HTTP 探针覆盖，但两者跑在
  同一个 socket 上的端到端用例不存在。
- **Location：** `minecraft-guard/src/test/java/dev/pawbot/guard/GuardSelfTest.java`；
  `reports/minecraft-review/probe/node_busy_http_probe.mjs`。
- **Reason：** 两侧的签名 shape/字段集各自被断言（E10/E13/E14），跨语言差异只能靠人工比对；这是覆盖缺口，
  不是已复现缺陷。
- **Severity：** **Low**。

### R2 — busy 缓存 `attested_at` 没有最大年龄（残余风险）

- **Problem：** `GuardBusy` 回执的 `attested_at` 只要求 `<= issued_at+1`，没有「距 now 不得超过 N 秒」。
  缓存只由 live attestation 填充，且回执结构上不可变更（`backup_id=""`、`can_write/can_pvp=false`），
  但仍可能返回较旧的观测坐标/路径。
- **Location：** `app/minecraft-sidecar/src/guard.js:75`；`minecraft_guard.py:196-214`；
  `PermitService.java:786-816`。
- **Reason：** 「非变更」把影响限制在观测新鲜度；真实写/连接仍需新的 live attestation。建议文档给出上限或
  在客户端加最大年龄。
- **Severity：** **Low**。

## 5. 被驳回的发现（含理由）

| 候选发现 | 驳回理由 |
| --- | --- |
| 最终 jar 里仍有 `net/minecraft/server/MinecraftServer`（yarn 名）引用，疑似未重映射、实机会 `NoClassDefFoundError` | **驳回。** 该类的官方名（obf）在 1.20.1 就是 `net/minecraft/server/MinecraftServer`：`mappings.tiny` 的 class 行三列相同（`c net/minecraft/server/MinecraftServer net/minecraft/server/MinecraftServer net/minecraft/server/MinecraftServer`），且官方 merged jar 内确实存在 `net/minecraft/server/MinecraftServer.class`。即 identity 映射，运行时能解析。dev jar 的 7 个 yarn Class 引用里，除该 identity 类外的 6 个（PlayerManager/IntegratedServer/ServerPlayNetworkHandler/ServerPlayerEntity/Text/WorldSavePath）在最终 jar 中都已变成 intermediary（`class_3244`/`class_1132`/`class_3222`/`class_2561`/`class_5218` 等）；最终 jar 的 **yarn-only Class 引用数为 0** |
| `:test SKIPPED`，说明没跑测试 | **驳回。** `build.gradle:47-49` 故意 `test { enabled = false }`，改用 `check → selfTest`（`GuardSelfTest`，无 JUnit 依赖、可离线）；E9 输出 `passed=63 failed=0`，任务列表含 `:remapJar` |
| `habit_formation_probe.py` 的 docstring 还写着「the three semantic keys stay at count 0 / branches are dead」，说明 V1 未修 | **驳回。** 探针的**断言**已更新为 `reachable=...; return 0 if reachable else 1`，E4 输出 `count=3/3/3`、`True`、exit 0；只有文件头注释过期（复核工件，不是仓库缺陷） |
| V1「语义习惯永不形成」（23:53 报告） | **驳回/已修复。** §3 第 8 条 + E4 |
| V2「无土可种时不回退 shelter」（23:53 报告） | **驳回/已修复。** E2 输出 `kind = shelter`，5 步 |
| G3「农田 4.583 > 4.5」 | **驳回/已修复。** E3：站位重选 + 超 4.5 土被丢弃，输出 4.123/4.123 |
| G4「503→409 可假成功」 | **驳回/已修复。** `core.py` 把裸 409 记为 `already_revoked_unverified`（`revoke_durable:false`，`ok:false`）；E1 两次调用都 `ok:false` |
| G8「owner-human 双实现」 | **驳回/已修复。** 居民委托 `owner_human_identity`（`minecraft_resident.py:1594`），已无第二套规则 |
| M1「撤销后仍可签许可」（首轮） | **驳回/已修复。** E13 压力 100 轮 `late200=0`；`revoke` 在 gate 之前且代次在签名/发布处复核 |
| 作者报告数字「focused 199 / full 2177」 | **过期。** 00:06/00:07 新增习惯形成用例后，实际为 **201 / 2179**（同一快照，E5/E6），差值与新增测试一致 |
| `docs` 说「当前 70,856 B jar 未重跑 restore smoke」 | **保留为事实。** 本报告未跑 jar CLI restore smoke；E13 是对 `build/classes` 的同一 `RestoreTool` 行为探针，不等于 jar 级演练（属 L5） |

## 6. 需求覆盖：已映射能力 vs 实机未验证（后者绝不称“已验证”）

**离线已映射（有本轮或套件证据，属“离线已验证”，不是实机）：**

- 授权边界：loopback HTTP + Bearer + 精确字段 + 32-hex nonce + actor/world/path/port 精确校验 + 5 s 生命周期 +
  raw-byte HMAC；write/pvp 能力位；撤销持久化与 busy 门（E9/E10/E13/E14，§3.1–3.5）。
- 世界副本隔离：`save_path` 规范路径精确匹配 + `session.lock` marker + `rejectRunningWorldAncestor` +
  受保护原世界重叠拒绝（E9/E13，§3.9）。
- 备份/恢复：Java 单一 60–1800 s 调度；`backup`/`connect` 服务器线程 `saveAll`+快照；预算/保留；
  `RestoreTool` 不覆盖/不跟随 junction/深层祖先拒绝（E9/E13，§3.5/3.9）。
- Sidecar：17 个 schema 技能（`move/follow/chat/dig/collect/craft/inventory/chest/place/farm/breed/attack/eat/explore/home/set_home/respawn`），
  无 eval/exec/RCON/任意按键；stdio JSON-RPC；超时/重连/隔离/有界队列；auth CLI 仅人工 TTY（E7/E7b）。
- Python 宿主/居民：同一 `bot.persona`/`bot.emotion`/`bot.longmem`（`minecraft_resident.py:2165,2192,2303,3278-3289`，
  情节以 `source="minecraft"` 写入既有 longmem）；工作空间容量 4（3–5，`:71`）；drives/情绪残留/衰减；
  项目/承诺/地点/人物/日记；意图级模型输出 + 预算 + 规则回退；习惯形成；owner-human 精确身份；
  家 seed；断线/失败/循环处理（E4/E5/E6 套件 + §3.6–3.10）。
- 紧急停止：宿主级闩锁、撤销→停常驻/侧车→回收、写排空、诚实 `ok:false`（E1，§3.1/3.2/3.10）。
- 兼容契约：`node scripts/architecture-baseline.mjs` 19/19、`node test/architecture-baseline.mjs` 46/0/0（E11/E12）。

**实机未验证（本报告未执行，不能称已验证）：**

| # | 项目 | 状态 |
| --- | --- | --- |
| L1 | 真实 1.20.1 客户端 + Fabric Loader/Fabric API 加载 `pawbot-guard-1.0.0.jar`，integrated-server 生命周期、`isRemote()`/LAN 端口真值、真实 `saveAll`+快照耗时/tick 卡顿、真实 kick | **未做** |
| L2 | 真实 Open-to-LAN：临时端口获取、端口变化后的重连、同机/局域网地址与防火墙边界 | **未做** |
| L3 | 微软设备码登录/令牌续期、离线 LAN 兼容性与主人账号实际策略 | **未做**（不读取真实凭证） |
| L4 | 真实存档上的快照/备份耗时、Windows 重解析点/链接在真实存档树的边界 | **未做** |
| L5 | 真实恢复演练（备份→新目录→重新登记）与当前 70,856 B jar 的 CLI restore smoke | **未做** |
| L6 | 专用副本内 30–60 分钟主人观察运行（自主项目、情绪残留、记忆连续、中断/恢复、自动日志） | **未做**（现有 60 分钟证据是合成/加速的契约测试） |
| L7 | 真实 JEV 服务接入（endpoint/认证/超时/预算）；默认关闭 + 规则回退已实现 | **未做** |
| L8 | 真实 Node 24 + Mineflayer 4.39.0 在 1.20.1 的寻路/挖掘/放置/农田/箱子/死亡重生/取消；20 Hz 从未声称 | **未做** |

> 以上 L1–L8 **一项都不算已验证**。离线绿不能替代实机；本报告不批准任何真实世界兼容性。

## 7. 与作者/前序报告的数字差异（供 documentation finalizer 直接采用）

| 指标 | 前序报告口径 | 本轮实测（同一快照） |
| --- | --- | --- |
| 全量 Python | 2177（`verified-final`，23:53） | **2179 / 288.260 s / OK (skipped=2)** |
| 聚焦 Minecraft | 199 | **201 / 43.540 s / OK** |
| 侧车测试 | 115 | **115 / 0 fail / 0 skip / 4798.5569 ms** |
| Java selfTest | 63 | **63 / 0 fail** |
| jar | 70,856 B / `B373B470…A82F` | **70,856 B / `B373B470…A82F`（clean build 复现）** |
| 基线 | 19/19 + 46/0/0 | **19/19 + 46/0/0** |
| 撤销持久化 | 探针 `FALSE_SUCCESS`（G4） | **probe exit 0，`no false success`** |
| 农田回退 | `OUT_OF_REACH 4.583`（G3） | **exit 0，4.123/4.123 ≤ 4.5；无土→shelter** |
| 习惯形成 | `count=0`（V1） | **exit 0，3/3/3，`True`** |

快照注意：`reports/minecraft-review/final-verify/` 下 00:02 的旧日志与本轮文件名相同者已被**覆盖**
（例外：`04-focused-minecraft-python.log` 是 00:02 的仅表头残留，见 §1 第 2 条）；
00:16 之后的所有日志都属于本报告快照。`reports/minecraft-review/verified-*.log`（23:45–23:53）
属于更早的源码快照，不得与本轮数字混用。

## 8. 复现命令（完整）

```powershell
# 仓库根，Python 探针
py -3.13 reports\minecraft-review\probe\revoke_durability_false_success.py   # exit 0
py -3.13 reports\minecraft-review\probe\farm_fallback_probe.py               # exit 0
py -3.13 reports\minecraft-review\probe\farm_stand_reach_probe.py            # exit 0
py -3.13 reports\minecraft-review\probe\habit_formation_probe.py             # exit 0

# app 下
py -3.13 -m unittest tools.test_minecraft_guard tools.test_minecraft_policy tools.test_minecraft_sidecar `
  tools.test_minecraft_control tools.test_minecraft_integration tools.test_minecraft_resident -q   # 201 OK
py -3.13 -m unittest discover -s tools -q                                                          # 2179 OK (skipped=2)

# app/minecraft-sidecar
npm test                                        # 115 pass
npm audit --offline --audit-level=moderate      # 0 vulnerabilities
node --check index.mjs; node --check auth.mjs
node --check src/main.js; node --check src/adapter.js; node --check src/controller.js
node --check src/guard.js; node --check src/skills.js; node --check src/auth.js

# 仓库根，Node 探针与基线
node reports\minecraft-review\probe\node_busy_http_probe.mjs   # exit 0
node scripts/architecture-baseline.mjs                         # 19 passed
node test/architecture-baseline.mjs                            # 46 passed

# 守卫（仓库根）
$env:JAVA_HOME='C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr'
$env:GRADLE_USER_HOME=(Resolve-Path 'minecraft-guard\.tooling\gradle-home').Path
& (Resolve-Path 'minecraft-guard\.tooling\gradle-8.7\bin\gradle.bat').Path `
  -p minecraft-guard clean build --offline --no-daemon --console=plain   # BUILD SUCCESSFUL; 63/63
$javac='C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\javac.exe'
$java ='C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\java.exe'
& $javac --release 17 -encoding UTF-8 `
  -cp 'minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test' -d $env:TEMP\pawbot-v `
  reports\minecraft-review\probe\RevokeRaceProbe.java reports\minecraft-review\probe\RevokeRaceStressProbe.java `
  reports\minecraft-review\probe\RevokeDurabilityProbe.java reports\minecraft-review\probe\JunctionRestoreSafetyProbe.java
& $java -cp "$env:TEMP\pawbot-v;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" dev.pawbot.guard.RevokeDurabilityProbe
& $java -cp "$env:TEMP\pawbot-v;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" dev.pawbot.guard.RevokeRaceProbe
& $java -cp "$env:TEMP\pawbot-v;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" dev.pawbot.guard.RevokeRaceStressProbe 100
& $java -cp "$env:TEMP\pawbot-v;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" dev.pawbot.guard.JunctionRestoreSafetyProbe
```

## 9. 结论

对 **2026-11-02 00:06/00:07 的最终树**，**离线工程就绪（READY OFFLINE）**：

- 9 组必跑命令全部 exit 0，全量/聚焦/侧车/守卫/基线数字与作者声称一致（差异仅为新增测试导致的
  199→201、2177→2179）；
- 上一轮所有 must-fix（M1–M5、S1–S6、F1–F6、V1、V2）在**当前源码上都不再复现**；
- 撤销持久化语义、busy 回执、单一备份调度、owner 身份、农田 4.5、习惯形成、RestoreTool 与
  紧急停止诚实性均已逐条源码复核。

**文档 D1 已在 00:46–00:51 定稿修复**（不是离线 verdict 的否定项；见 §4 D1 Addendum 与
`documentation-final.md`）：三份 owner 文档已按本报告 §7 的数字与结论改写，协议 §4 已改真实语义，
证据表已换成同一快照；本报告保留 D1 的原始发现作为历史证据，当前交付文档**不需要**再按旧行号改写。
**READY OFFLINE 指的是当前树的离线工程与证据，不能把它读成实机通过。** 定稿依据：

```
VERDICT=READY OFFLINE
full=2179 tests / 288.260s / OK (skipped=2) / exit 0
focused=201 tests / 43.540s / OK / exit 0
sidecar=115 pass / 0 fail / 0 cancelled / 0 skipped / 4798.5569ms / exit 0
sidecar_syntax=8/8 exit 0; audit=0 vulnerabilities / exit 0
guard=BUILD SUCCESSFUL 18s / selfTest 63 passed 0 failed / remapJar executed
jar=70856 bytes / sha256 B373B470F6BDE30A01EEC5C9723340DF6FD81EA43459AA97552FD4AA29D7A82F / class major 61
remap=8 intermediary Class refs / 0 yarn-only refs (dev jar: 7 yarn refs) / MinecraftServer = official identity name
baseline=19 passed 0 failed; baseline_tests=46 passed 0 failed 0 skipped
probes=revoke_durability 0 / farm_fallback 0 / farm_reach 0 (4.123<=4.5) / habit 0 (3/3/3) /
       java revoke 503+409+restart-false / race 100 rounds late200=0 / junction+restore ALL PASSED /
       node busy -32008 stopCount=1 revoke_detect_ms=2
skips=2 (test_memes seed book absent; test_ha_content Windows trailing-dot filename) — pre-existing, non-Minecraft
findings=1 documentation drift (D1, Medium; resolved 00:46-00:51 per Addendum) + 2 low residual risks (R1,R2); 0 source defects reproduced
live_only=NOT VERIFIED (L1-L8): real Fabric load, Open-to-LAN, MS/offline auth, Mineflayer behavior,
          real restore drill, 30-60 min owner observation, real JEV, real backup/load under lag
```

本报告不修改源码/测试/配置/文档，也不宣称任何实机能力。除 `reports/minecraft-review/**` 外未写入其他路径；
无后台任务、无终端会话残留。
