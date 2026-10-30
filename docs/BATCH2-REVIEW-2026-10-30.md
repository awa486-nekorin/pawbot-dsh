# 批次 2 独立复核与修复清单

日期：2026-10-30。状态：第一版实现已报告测试通过，但本表未关闭前不得认定批次完成。

## 锁协议独立复核

复核代理：`d15b3eb5-ab61-4c50-b095-0a6b322e5d4b`，DeepSeek Flash / max，干净上下文只读，未改源码。范围为第一版 `lib/instance-lock.js` 及对应测试；Python 镜像规则需同步修正。结论 REQUEST_CHANGES。

| 编号 | 等级 | 问题与位置 | 证据/验收 |
| --- | --- | --- | --- |
| L1 | blocking | `acquireMutex` 将 age>120s 的活 PID 或 age>30s 的未知 PID 当 stale。Node 第一版 164–173；Python 镜像 330–332。 | Lead 当前进程及 reviewer 的真实活子进程均被抢占；必须仅对明确死亡 owner 接管，墙钟不能让活/未知 owner 失去互斥。 |
| L2 | blocking | stale mutex 的 read→unlink 按路径删除，没有身份保护，可能删除新持有者。 | reviewer 用 isAlive seam 在读与删间替换为 fresh-live-owner 后，仍 acquire 成功；并发接管必须只有一个 winner。 |
| L3 | blocking | mutex open(wx) 后原地写，崩溃留下 0 字节/截断 JSON 时永不恢复。 | 空文件、截断 JSON、dead pid 缺 started_at 三例均反复 LOCK_RACE，1 小时 mtime 也无效；初始化应原子发布，强杀恢复不应永久卡死。 |
| L4 | should-fix | unlink 失败的 continue 跳过 deadline/backoff，可能无限忙等。 | PowerShell FileShare.Read 打开 stale mutex 时，timeoutMs=50 的调用超过 2.5s 且 CPU 忙转；所有错误路径必须有界。 |
| L5 | should-fix | archiveStaleLock 的 verify/restore 是 best effort，mutex 一旦失效，ABA 可能把 live lock 留到归档并让第三实例持锁。 | reviewer 三进程窗口 2/2 复现：P1 的 live nonce 被归档，P3 也取得运行锁；不应依赖身份核对失败后静默 continue。 |
| L6 | should-fix | 不支持 hardlink 时 fallback 直接写正式 .runtime.lock，崩溃/ENOSPC 留半截文件且永久 LOCK_UNREADABLE。 | Node 第一版 196–233，Python 第一版 374–395；不能静默降级成非原子发布。若不支持必要文件系统能力，可明确拒绝启动。 |

第一版 `node test/instance-lock-offline.mjs` 的 12/12 全绿仍未覆盖上述负例。releaseRuntimeLock 在互斥有效前提下的 nonce/pid 验证未发现独立缺陷；跨语言字段及时间串兼容。修复需加入针对缺陷本身的回归，不重复已有 happy path。

复现脚本由 reviewer 留在系统临时目录 `paw-lock-review`；仅操作临时根，不碰真实数据。

## Lead 独立探针

### R1：最终 flush 失败的退出码（待修）

第一版 `app/run.py main` 在 try 内 return life.exit_code，再于 finally 的 final_flush 失败时修改该字段。Python 已先求值 return，因此最终失败仍可返回 0。

Lead 在临时根调用真实 main(args.check=True)，使用真实 Store，仅替换 Bot/WebUI 等外部服务；Bot.stop 将后续 flush 改为抛 OSError。实际返回 0，探针 exit 1。需在 finally 完成后决定退出码，并覆盖正常/check/控制退出路径；最终失败不应返回 0 或表示成功控制停机的 10。

### C1/C2：控制客户端目标及回包（待修）

第一版 `lib/lifecycle.js sendControlShutdown` 注释说仅 loopback，但 host 无校验。纯假 fetch 探针传 host=203.0.113.9，仍调用 fetch；假 ack 的 error/echo 反射合成 token 后，返回对象仍含 token。探针输出 fetchAttempted:true、tokenReflected:true，exit 1，无真实网络请求。

需显式拒绝非 loopback 目标且不调用 fetch，IPv6 URL 正确构造，禁止重定向泄漏 Authorization；对 ack/error 中的 token 反射做完整遮罩。Host 实际目标也需独立核查（已读注释称固定 127.0.0.1，可能降低 C1 当前生产影响，但不消除公开 helper 的契约错误）。

## D1：迁移改名失败后的删除兜底丢数据（blocking，已复现）

`lib/data-guard.js migrateToExternal` 先用带 SKIP_DIRS 的 copyTree 复制（排除 brain/backups/logs 等），rename 失败后却 rmSync 删除原目录再建联接。临时根放 config.json 与 brain/kept.txt，仅将对 source 的 renameSync 注入 EBUSY（其它文件操作真实），调用 resolveDataDir：结果 migrated:true、warnings:[]，但原目录与外部均找不到 brain/kept.txt；探针输出 survived:false，exit 1。旧fallback即存在此风险，本批明确承诺失败不删除原数据，必须修复，最稳妥为 rename 失败则保留原目录并回退，不做 destructive rm fallback。

另需校验 dataDir 为源目录后代/祖先、junction 词法不同但同实际目录时的重叠，避免复制自递归或自迁移。后者尚未执行破坏探针，仅源码审查疑点。

## S1：取消未完成仍返回成功 ack（blocking，Lead 已复现）

`RuntimeLifecycle._cancel_inflight` 对 asyncio.wait(timeout=2) 的 pending 结果没有检查，`request_shutdown` 无条件随后 flush 并回 ok/flushed:true。Lead 临时真实 Store + tasks.spawn 的任务在取消时等待 release Event（模拟工具/网络库的取消收尾），调用 request_shutdown 后得到 ack_ok:true、flushed:true，任务仍 pending；release 后该任务继续 Store.append，store_dirty_after_ack:true。探针 exit 1，任务已正常结束，无残留。

必须在 ack 成功前确保所有可写/外发任务终止/排空；超时有 pending 就应失败回执并退出异常，不能把成功 ack 当“已请求cancel”。还需停止 proactive/reminder/OneBot 接单/连接worker/brain callbacks 等周期生产者，不能仅tasks registry；不要为满足测试禁止业务在正常运行时生成任务。

### S2：先关连接再抓任务快照可能漏 worker（第二版快照待修）

Lead 于13:27新增并实跑 `test_successful_ack_does_not_lose_disconnected_onebot_worker`：OneBot server.stop 中cancel worker并清 connections（真实handle finally也会移除），worker捕获取消后仍在收尾；_cancel_inflight随后才读connections，已找不到worker。结果ack成功但task仍pending，Ran1 FAILED，0.029s。必须在关闭入口前抓取worker/周期任务/WebUI handler快照保留强引用，直到排空确认；可对保留victims统一cancel/wait，不能只依赖随后动态映射。新增测试仍为独立契约，不应削弱断言。

## Q1：ready 的 db_writable 未反映真实 Store 替换权限（Lead 已复现，待修）

`RuntimeLifecycle.probe_db_writable` 仅在 dataRoot/tmp 中写fsync探针；Store.flush实际在root写临时文件并replace sessions.json。Windows临时真实Store先flush，然后chmod sessions.json为S_IREAD：ready_state仍ready:true/db_writable:true，Store.append后flush_store返回ok:false/PermissionError。退出前恢复权限，probe exit1，无真实数据。

独立 `test_shutdown_admission.py` 已加 `test_ready_rejects_read_only_store_destination`（Windows）；db_writable须同时核对真实Store所在目录/目标文件替换权限，不能仅保证tmp/可写。无需为ready探测写入业务内容，但应至少识别Windows只读属性和root不可写，保留动态权限变化的事实边界。

## T1：真实进程验收需显式隔离继承环境（待修）

`test/host-runpy-integration.mjs` 第一版 spawn 的 env 展开 process.env，却未覆盖/删除 PAWBOT_DATA_DIR。用户若在 DSH 启动环境设置外部根，临时 app 的测试进程会尝试使用用户根（当前不一致时多半 failclosed，但若以后适配逻辑变化可能写入）；必须显式设为该测试的临时 dataDir，并清理所有 PAWBOT_DSH_* bridge token/URL，不应依赖运行环境恰好干净。Python RunPyTests 已显式删除 PAWBOT_DATA_DIR，可统一检查所有fixture与spawn。

## 已完成的测试基线修正

Lead 只修改了以下测试，未改业务代码：

- `app/tools/test_pixiv_home.py`：补齐就绪/CDP/Popen 隔离及 record_views fixture；guard 在旧路径证明会失败，修复后 14/14，0.273s。
- `app/tools/test_pixiv_session.py`：中性排序 fixture 保留原断言；新增 SFW/R18 均排除未成年相关元数据的完整搜索用例；两组共32/32。进程内禁用 `_allowed` 后，新增两个 subTest 均失败。

新增 `AGENTS.md` 和 docs 由 Lead 维护；修复代理不要覆盖这些修改。

## 完整独立复核补充（18cc9f46，第一版快照）

- F1 面板/brain handler未取消、周期生产者ack后才停：接受，与S1相同，属于必须修的停机契约，不缩小定义。
- F2 setup/boot硬编码appDir/data：接受，修复代理已获扩写setup.js授权；需自定义projectDir≠appDir与外部dataDir测试，不能留作遗留说明。
- F3 控制停机10会掩盖final flush/stop失败：接受，与R1同属退出码正确性，必须修；首作者已修check返回0分支不代表此项关闭。
- F4 restart丢弃stop.ack/graceful：应在兼容旧字段的同时增加stop证据，使强杀和flushed:false可观察；不要把forced restart描述为成功优雅落盘。
- F5 控制token被业务孙进程继承：接受收窄env建议，修复代理已获授权。
- F6 ensureDataLayout只有测试调用、runtimeLock/runtime_lock键命名不同：不作为阻断；两端语言风格不同且底层路径一致，不做无收益重构。
- F7 body无超时且鉴权前读：接受，修复代理已获授权，补有限超时及未鉴权请求负例。

完整复核未再次挖锁，未跑Python全量，其“无数据丢失级blocking”结论不覆盖Lead已证实的D1/L1–6；最终评估须合并这些证据，不能采用单一绿灯结论。

## 最终交付前硬检查（2026-10-30 13:33）

修复者在结束写入前必须重新读取本清单，并执行最新 `py -3.13 -m unittest tools.test_shutdown_admission -q`：当前有 **5 项**，其中S2为Lead13:27新增，原先4项通过不覆盖它；此前全量1376项在测试模块载入时尚未包含S2。完整review代理6db8也独立复现S2失败，不能以旧全量绿灯关闭。第二轮锁review代理dc1a仍在运行。退出后给Lead明确停止写入通知再交主代理修剩余问题。

另有全量ResourceWarning来自新生命周期测试的main日志文件和Popen pipes未关闭；应修测试清理，不为此改业务日志模块或全仓重构。

## 第二轮已关闭证据（截至13:34）

- S2：保留关闭入口前的 `_collect_inflight()` 强引用，并将 early 快照合入最终cancel/wait；Lead最新5项独立停机测试全部OK（2.159s）。
- L1–L6：第二轮锁专项review dc1a给APPROVE，无阻断，Node17/17+Python锁15项OK，额外三轮跨语言五竞争者仅一个winner。固定guard不删除、强杀自动释放和原子JSON发布已证实。
- D1/C1/C2/Q1/S1/R1：Lead原探针及独立回归已转绿，详细见验收记录。
- 锁review的非阻断项：非NTFS不支持硬链接若返回EINVAL会failclosed拒绝启动，不会非原子落盘；当前未承诺支持此文件系统，记录限制，不扩展fallback错误掩盖真实IO错误。解释器发现时间未受短timeoutMs覆盖但总有界，Host生产inspect不触发helper，暂不扩大重构。PID复用仍failclosed需人工确认。

## 最后补修（Lead，修复代理已明确停止写入）

- S3：第二轮review发现停机开始前已断连的worker不在connections中。Lead新增真实OneBot `_handle` + Store回归，13:38单跑确实失败（ack成功但worker仍pending）；改OneBot worker/login probe为tasks.spawn留强引用，6项独立停机测试4.191s全部OK，不修改业务发送/过滤语义。
- F4：为重启结果新增stop字段保留method/graceful/控制回包。Host回归新增断言先报2失败；修补后待最终全量确认。控制客户端回包有两层ack，断言按已有结构读取，不修改原结构。
- 测试清理：main测试模拟to_file避免反复打开临时日志；RealRunPy和guard holder关闭stdout/stderr并等待结束。只改测试，不重构业务日志。

## RACE-1：迁移与实例启动之间缺少互斥（待修）

完整review6db8在copy前检查之后、rename之前注入活运行锁，其余真实临时文件系统：迁移仍成功，活锁随原目录归档，外部根无锁。一次inspect不足以覆盖copy期间新实例启动。已委派4c743b76-4282-45fb-9c0a-bf0f7227958f（Flash/max）独占data-guard及相关测试修补，优先迁移期间预约源/目标运行锁，不能仅缩小竞态窗口便声称完全互斥。此前全量JS269/Python1378全绿不覆盖此项，不得认定最终完成。

## T2 / P2 补充修正

- T2：Host runtime测试也显式delete/restore继承PAWBOT_DATA_DIR，避免临时apply写入真实外部根。reviewer用临时继承根已复现旧版向该根写marker；Lead已修并用临时继承根复验。
- P2：Python独立数据根拒绝app_data祖先/后代，防止data→base循环联接；保留等于已有真实目标的兼容行为。新增三个subTest祖先/父/后代，DataRoot8项OK。
- 最后两补丁复核326已完成：OneBot登记及restart停止证据无blocking，6Python/6Host全绿；各自独立进程注回旧缺陷精确命中RED。
- reviewer观察的realtime/启动auto_pull线程与非控制路径排空需要Lead确认；不能把后台线程写根遗漏伪称完整停机。

## 同步线程与非控制退出收尾

Lead确认启动auto_pull返回Thread原先丢引用，realtime_sync也未停止。core保留_auto_pull_thread，生命周期先to_thread停止realtime并检查is_alive，再等待auto_pull并检查is_alive；超时拒绝成功ack。新增真实thread+Store回归守护，不修改云同步业务。run.py finally中对非控制退出也走相同排空，成功保留原退出码，失败不得0/10。此补修需要最终全量和独立复核确认。

## RACE-1修补审查注意（13:51）

Lead正在观察4c writer的813行草稿，暂不并写。预约方向合理，但 `LOCK_HELPER_SOFT_CODES` 不能视为安全迁移授权：Host找不到解释器不代表不存在其它已运行Python/独立venv可在copy期间启动。helper缺失/超时应failclosed跳过迁移/快照并给warning，而不是held:false继续有写入的inspect兜底。既有“helper失败failclosed”不应在data-guard被弱化。切换间隙目录重现只保留/报错，不能删除对方目录；复制必须排除.runtime.lock/.locks，释放按归属与新路径处理。

## RACE-1草稿导入回归

6db8报告13:51:55草稿lockPython引用fileURLToPath但缺node:url导入，data-guard 7pass/13fail。此前Lead读过另一瞬时草稿有该导入，说明作者正在编辑，必须以停止写入后的最终快照实跑为准。最终data-guard/data-root/baseline必须重跑，不能只node --check（未定义全局不属语法错误）。

## RACE-1 最终稳定路径补修（Lead）

4c已停止写入；Lead移除helper soft降级，失败一律跳过写入操作。另增加源父目录稳定预约 `.<dataName>.pawbot-migration/.runtime.lock`，Python acquire在guard前后检查它，活/未知预约拒绝启动，同一Node owner可继续自身源/目标预约。源rename后锁不随目录移动，覆盖rename→junction间隙；删除Node acquire提前mkdir，避免被拒者仍重建空源。新增真实rename后竞争者回归最初RED（竞争者虽LOCKED但提前mkdir导致迁移回退），删除提前mkdir后正在复验。新只读review9c809403核该协议delta，必须等报告与测试。

## 最终源码冻结与复核状态（14:05）

所有writer已停止；Lead源码亦冻结，仅文档收尾。源/目标预约、稳定父目录门、helper故障failclosed及Windows大小写保护均已落盘；旧held:false降级分支已删除。Lead最新JS14套件277项0fail+baseline19，Python1383项OK(skip1)，stable live/corrupt/dead三项永久回归已包含。新迁移review9c809403与完整review6db8尚未给最终结论；若无新可复现blocking请明确报告结束，不因旧草稿错误继续阻断。现有历史章节保留RED证据，不代表问题仍开放。

## 稳定门决策与发布序列化（14:07）

Lead自查补强：仅guard前后读稳定marker仍可能让暂停在首检之后的启动者在rename后先mkdir再拒绝，造成空源目录。正常acquire现在在稳定预约根的OS guard内完成第二次marker判断及源运行锁发布；迁移发布稳定marker使用同一个guard，因此二者顺序确定。若启动先赢，迁移源预约失败；迁移先赢，启动在创建根之前被拒绝。没有长期持guard跨rename。最终迁移review需以此最新delta为准。helper override继承隔离复跑job pwsh-0aed9f1e88fe4585待收。

## 最终关闭结论

完整review6db8 APPROVE；最终稳定迁移专项9c809403 BLOCKING=0，已结束。Lead核对最终五个源码/测试hash与review一致；专项Node22/16/17及Python锁18项全绿，完整JS277项、Python1383项OK(skip1)、基线19/19。所有blocking已关闭；历史“待修/仍运行”章节仅保留过程证据，以本节为准。批次2本机验收完成，未提交、未生产部署，不进入批次3–7。

接受的部署限制：稳定guard要求数据根父目录可写；只读部署未支持。Python外部env alias和Host clean-install并发路径可能使迁移回退/跟随实际联接，但没有第二实例或原数据删除。rename→link硬崩溃应停机核对外部根和留档后恢复；release失败保留预约需人工恢复。详细覆盖缺口与生产边界见FRAMEWORK-BATCH2-VERIFICATION。

## 协调状态

所有源码writer已停止。完整review6db8已结束APPROVE、无blocking；最新稳定门序列化delta仍等迁移专项9c809403最终报告。锁reviewdc1a及小补丁review326、7f均完成无blocking。Lead最终序列化门版JS277项0fail、baseline19/19，Python1383 tests68.197s OK(skip1)，相关后台job已收齐。

完整review的非阻断项保留为后续工作：release helper失败会保留活Host预约并拒绝启动（警告已返回，需停止Host后人工恢复，不可在线删guard）；custom projectDir/外部根Host端到端、真实信号与realtime同步形态回归仍有覆盖缺口；控制端口与实际WebUI端口不一致会走强制停止；独立大小写相等路径、短命alias子进程环境及restart失败证据为边界。稳定guard隐藏目录保留是协议预期，不清理以免ABA。不以这些未验收路径声称生产完备。
