# 批次 2 验收记录（本机验收通过，生产边界见文末）

日期：2026-10-30。主工程：`D:/paw-old/pawbot-dsh`。本文件记录实际命令和结果，不将静态检查等同于真实部署验收。

## 接手基线

- `node --check scripts/architecture-baseline.mjs` → exit 0。
- `node --check test/architecture-baseline.mjs` → exit 0。
- `node scripts/architecture-baseline.mjs` → 19 passed，0 failed。
- `node test/architecture-baseline.mjs` → 43 passed，0 failed，0 skipped。
- 其余 7 组原有 JS 离线套件均 exit 0；最终整体验收时重新汇总每组实际计数。
- 系统 Python：3.13；未安装 pytest，原测试使用 unittest。
- `py -3.13 -m unittest discover -s tools -p 'test_*.py' -q`（cwd=app）→ 1319 项，1317 通过、1 失败、1 跳过。
- `py -3.13 -m unittest tools.test_pixiv_session.PixivSessionTests.test_filters_and_sorting -v` → 同一失败可单独复现，0.019 秒。

## 既有测试问题的修正

### 离线测试启动真实浏览器

现有 Pixiv 主页/路由/标记测试未隔离浏览器自启动及浏览历史任务。本轮意外拉起的专用 Chrome 已按启动时间、退出的测试父进程和 `chrome-pixiv` 专用用户目录核实并关闭；未删除浏览器用户目录。没有将该路径当作截图验收。

加入 Popen 阻断和 cleanup 的 `assert_not_called` 后，原 HomeReportTests 路径为 3 项失败：`Expected 'Popen' to not have been called. Called 1 times.`（仅阻断异常会被业务捕获，原有行为断言仍可能全绿，故必须同时断言未调用）。

补齐就绪和 CDP fixture，并隔离路由与后台 record_views 后：

```text
py -3.13 -m unittest tools.test_pixiv_home -q
Ran 14 tests in 0.273s
OK
```

业务代码未为此修改。

### 排序 fixture 与现有过滤策略冲突

旧排序 fixture 把含 child 标题的候选当成普通 SFW 候选，注释与当前 `_allowed` 的明确契约相反。将普通排序候选改为中性标题，保留原排序断言，并新增完整搜索流程验证该类候选在 SFW/R18 两种模式均排除。

```text
py -3.13 -m unittest tools.test_pixiv_session tools.test_pixiv_home -q
Ran 32 tests in 0.511s
OK
```

进程内 mutation：用 `patch.object(imagesearch, '_allowed', return_value=True)` 临时模拟策略失效，再运行 `test_minor_metadata_is_excluded_even_for_sfw_results`：两个 subTest 均失败，实际返回 `['1','2']` 而预期 `['2']`。mutation harness 以“正好两个失败、无其他 error”为成功判据；没有改业务源码。

## 批次 2 实现与独立检查

- 实现代理已保存 RED 日志（新增模块缺失时测试 exit 非 0），路径与最终测试结果待实现完成后统一收集。
- Lead 的临时目录锁探针已复现活互斥持有者被时间阈值抢占：`.locks/takeover.lock` 的 PID 为当前进程，时间为 121 秒前，调用 `acquireRuntimeLock({dataRoot, timeoutMs:50})`，预期拒绝，实际 acquired，exit 1。
- 已派干净上下文只读局部复核检查该锁及并发/崩溃路径；未关闭问题前不得宣称本批次验收完成。
- Lead 在临时根调用真实 `run.py main(args.check=True)`、使用真实 Store，仅替换 Bot/WebUI 外部服务；令 Bot.stop 后的 Store.flush 抛 OSError，main 实际仍返回 0。原因是 try 中的 return 先求值，finally 中再修改 life.exit_code 无法改变返回值。该探针 exit 1，待修并补永久回归。
- 控制客户端待修已用纯假 fetch 复现：`host=203.0.113.9` 仍调用 fetch，错误 ack 回显 token 后结果仍含 token，探针输出 `fetchAttempted:true, tokenReflected:true`，exit 1，无真实网络请求。
- 普通子代理消息桥已尝试一次，但运行时以“belongs to another parent session”拒绝向同级子代理投递。根会话当前仅暴露 Team 版 send_message，不能直接投递到普通子代理。以上问题尚未送达原实现者；第二轮修复代理已接手完整落盘清单，未重复尝试绕过归属限制。
- Lead 新增独立永久回归 `app/tools/test_shutdown_admission.py`：`py -3.13 -m unittest tools.test_shutdown_admission -q` → Ran 3 tests in 2.122s，FAILED(failures=3)。覆盖停机后新改 Store、取消未完成却成功 ack、在途面板发送尚未结束却成功 ack。仅临时 Store 和 loopback HTTP。
- Lead 新增 `test/windows-process-tree-integration.mjs`：`node --check` exit 0；实跑 1 passed, 0 failed。真实 loopback HTTP 控制请求不返回，客户端实际超时后调用 taskkill /T /F；直接启动的 py.exe launcher、实际 Python、Python 孙进程均确认不存在。全部为新建测试进程，未触及 PawBot/QQ 用户进程。

## 第二轮修复快照的 Lead 复验

2026-10-30 13:21，修复代理仍在收尾，以下为实际独立探针结果而非最终全量结论：

- 原 D1 改名 EBUSY 探针：survived:true、migrated:false、fellBack:true、warnings:1，exit 0。
- 原 C1/C2 纯假 fetch：非loopback无fetch调用，嵌套token/key反射不回传，redirect=error，2 passed。
- 活PID古老时间戳运行锁：ok:false、code:LOCKED、nonce保留，exit 0。
- `py -3.13 -m unittest tools.test_shutdown_admission -q` → Ran 4 tests in 2.141s，OK，exit 0。四个Lead独立负例（包括Windows只读Store目的文件ready）均转绿，断言未削弱。
- `git -c core.whitespace=cr-at-eol diff --check` → exit 0。既有CRLF的默认diff检查误报无需全仓改行尾。
- Lead 全部 `test/*.mjs`（14 个文件）逐套件执行并聚合退出码：14 suites、267 passed、0 failed；architecture43、client-meta32、data-guard14、data-root14、brain20、bridge-host3、bridge-offline15、host-runpy4、host-runtime6、instance-lock17、lifecycle19、offline19、setup60、Windows-process-tree1。完整日志：系统临时目录 `pawbot-lead-batch2-js-verification.log`。这是13:25前后修复快照，后续源码改动须按受影响范围重验。

- Lead Python 全量：`py -3.13 -m unittest discover -s tools -p 'test_*.py' -q` → Ran 1376 tests in 160.321s，OK(skipped=1)，exit 0。日志在系统临时目录 `pawbot-lead-batch2-python-verification.log`。该进程导入测试模块早于Lead新增S2，**不包含新第五项worker断连负例**；后者单跑失败，不能据此关闭最终验收。输出另有测试日志文件/子进程pipe未关闭的ResourceWarning，尚未消除。

- 13:32 Lead 最新独立 `tools.test_shutdown_admission` → Ran 5 tests in 2.159s，OK，exit 0；新增 S2 关闭入口前保存 worker 快照的负例已由红转绿，断言未修改。
- 第二轮锁专项独立复核（dc1a，576行runtime_lock.py版本）结论 APPROVE，无blocking。独立实跑Node锁17/17、Python锁15项OK；额外3轮五竞争者跨语言stale接管每轮唯一winner、guard崩溃自动释放、inode稳定、inspect不写盘和失败failclosed均通过。非NTFS实际卷与POSIX系统未验收，解释器发现不包含在短timeoutMs内仍可能耗时数十秒（有界），作为边界记录，不掩饰为已验证。

## 最新完整回归（13:41，RACE-1修补前）

- Lead全部14个JS套件：269 checks通过，0失败；baseline脚本19/19，CRLF感知diff-check通过。包含最新stop证据断言及真实run.py握手、Windows合成进程树清理。
- Lead Python全量：`py -3.13 -m unittest discover -s tools -p 'test_*.py' -q` → Ran1378 tests in151.719s，OK(skipped=1)，exit0。日志：系统临时目录 `pawbot-lead-batch2-python-closure.log`；该次输出未再出现main日志/pipe ResourceWarning。
- 最新停机独立6项全过，含真实OneBot_handle在shutdown开始前已断连、worker取消清理仍在运行的负例；修复前单跑确定性失败，修后4.191s全过。
- 此次绿灯尚不能关闭最终验收：完整review新增RACE-1（copy前无锁、copy期间新实例发布活锁后仍rename迁移），已委派Flash/max补修与永久回归。迁移路径必须再次验证，不能以旧快照绿灯替代。

## 同步与独立根补修后回归（13:49）

Lead Python全量：1380 tests in72.615s，OK(skipped=1)，exit0，日志为系统临时目录 `pawbot-lead-batch2-sync-final.log`。覆盖祖先/后代数据根拒绝、同步线程仍存活时拒绝成功ack、非控制退出同一排空。MainExit+admission13项OK（16.986s）。同步线程负例经进程内暂时隐藏thread引用恢复旧遗漏后，精确在成功ack且thread活着的断言变红；文件未改动。Host测试带临时继承PAWBOT_DATA_DIR运行6/6OK，外部根保持空目录。RACE-1仍待writer完成后另验。

## 迁移预约草稿独立复跑（13:55）

Lead实际执行 `node test/data-guard-offline.mjs` →20 passed/0 failed；`node test/data-root-offline.mjs` →16 passed/0 failed（job pwsh-f2dc6453e60340dd已收）。新增复制期间双方运行锁预约、第二实例拒绝、rename注入活锁回滚、快照预约及不复制锁元数据均通过。该快照node:url导入完整，先前缺导入报告已不复现。作者尚未停止写入，helper故障不得降级的审查意见仍需关闭，故仍不是最终验收。

## 稳定迁移预约最终复验（14:02）

- Lead完整JS14套件共277项（data-guard22、data-root16，其余同前），0失败，baseline19/19，diff-check通过；日志TEMP/pawbot-lead-batch2-final-js.log，job pwsh-487e63f437564424已收。
- Lead稳定门协议改后Python全量1380 tests in70.431s OK(skip1)，job pwsh-81daaede15bf4403已收；随后新增stable live/corrupt/dead三项边界，RuntimeLockRegressionTests10项OK（2.486s），新完整回归pwsh-07f87a6b162d440d运行中，最终计数以该次为准。
- 7f295b0f最新Python根/线程/非控制退出小补丁review无blocking，21项连续三次OK，并以临时探针验证realtime alive→失败、Ctrl+C正常→0/线程未停→1。
- 迁移作者停止后Lead移除helper失败的降级，补稳定父目录预约与Python guard前后两次检查，并删除Node提前mkdir。真实rename后竞争者测试曾因提前mkdir精确RED，修后data-guard22/22、锁17/17均通过。新review9c809403核最终迁移协议；完整review6db8仍待最终回报，未提前交付。

## 最新结果（14:04，等待最终迁移复核）

- Lead完整Python：1383 tests in71.328s，OK(skipped=1)，exit0。包含稳定预约live/corrupt/dead三个新增回归；日志TEMP/pawbot-lead-batch2-accepted-python.log，job pwsh-07f87a6b162d440d已收。
- Lead完整JS14套件277项，0失败；基线19/19、diff-check通过。data-guard22、data-root16、锁17；真实进程握手与Windows树清理同时通过。
- 最新迁移协议review9c809403及完整review6db8尚待最终结论；当前不据绿灯提前关闭批次。

## 稳定门序列化后完整复跑（14:09）

最终协议把稳定marker判断和源运行锁发布放在同一个稳定根OS guard内序列化；无长期guard跨rename。Lead完整JS14套件277项0fail、baseline19/19、diff-check通过（TEMP/pawbot-lead-batch2-gate-serialized-js.log）；Python1383 tests in68.197s，OK(skip1)，exit0（TEMP/pawbot-lead-batch2-gate-serialized-python.log）。两job已收，无待收Lead后台命令。完整review6db8已APPROVE，无blocking；最终迁移专项9c809403仍待回报。原始草稿失败记录仅为历史证据。

## 验收边界与非阻断后续

完整review6db8于最终定向快照给APPROVE，无blocking；锁专项dc1a、小补丁326和Python收尾7f同样无blocking。稳定门最后序列化delta由9c809403另审，结论待回报。不得把以下未覆盖情形写成已实测：

- release helper故障可能保留活Host预约并阻止启动：当前安全拒绝、返回warning，需停Host后确认owner再人工恢复；不能在线删除固定guard。
- 自定义projectDir≠appDir/外部根的完整Host组合，以及真实SIGINT/SIGTERM、二次中断、真实RealtimeSyncer线程形态仍缺永久端到端回归。现有临时探针与代码审查不替代这些证据。
- 控制端口与PawBot实际WebUI端口不同会超时后走进程树强制停止；custom appDir无可发现helper会failclosed跳过迁移/快照。
- 迁移仍沿用既有SKIP_DIRS：brain/napcat/backups/logs等保存在.migrated原目录留档，不自动出现在新根。
- Windows大小写相等的独立数据根、短命alias子进程继承控制环境、restart失败分支停止证据为已记录边界，不扩大本批次改动。

## 最终结论

批次2按本机Windows/NTFS、临时数据根验收口径通过。完整review6db8 APPROVE；最终迁移专项9c809403 BLOCKING=0，最终实跑data-guard22、data-root16、Node锁17、Python锁18项均通过。Lead核对五个源码/测试hash均与最终review一致：data-guard BE2BAC3B73F5、instance-lock DAAF3FC753A5、data-guard-test 381D9CDB67A5、runtime_lock 103AC332D520、runtime-test 8CFB7A8BA0F5。最新完整JS277项、Python1383项OK(skip1)、基线19/19，后台任务已收齐。批次3–7未开展，未提交、未安装或重载真实插件。

### 最终迁移专项的接受边界

- 稳定guard当前要求数据根父目录可写，并会留下固定隐藏guard目录；只读app/外部父目录不属于本次已验收部署范围，可能failclosed拒绝启动，不能据此宣称支持只读部署。
- 独立Python带外部环境根在切换间隙可能先建alias再被目标锁拒绝；clean-install Host路径也可能先建junction。没有第二运行实例或原数据删除，但迁移方可能回退、跟随实际联接；不同dataDir并发配置未经保证。迁移应先停服务，避免同时更改配置/启动。
- rename后link前硬崩溃时，dead预约允许恢复启动，但默认路径可能被当作新安装；原数据仍在外部根和.migrated留档。应停机核对并恢复联接，不能直接创建空根继续使用。
- release helper失败保留预约会阻止后续启动；非合作手工删除稳定marker不属于互斥保证。正常释放扫描已验证，故障释放恢复未自动化。
- 审核中提到的旧degraded迁移/快照分支已删除；verifyReservation仍有不可达held:false兼容分支，reserve本身失败严格ok:false，不构成可达降级授权。

这些是明确接受的生产/覆盖限制，不应被后续代理误读为全场景生产验收。
本轮无 UI 改动。未验收阿里云 Windows Server、QQ/NapCat 真收发、真实 DSH profile 的重载/运行；未执行真实插件安装或重启。
