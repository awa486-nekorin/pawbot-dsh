# Hand-off: PawBot 批次2最终验收

Updated: 2026-10-30 14:12 Asia/Shanghai
Branch: upload-2026-10-27 / HEAD 3dff9ef，全部改动未提交。

## 目标与红线

仅批次2：DataRoot、独立/托管单实例锁、生命周期和Windows优雅停止。批次3–7未做。唯一仓库D:/paw-old/pawbot-dsh；不读写真实app/data/凭证/人设/记忆，不起QQ/模型/浏览器/真实插件，不改DSH profile，不提交。测试仅系统临时根。所有子代理指定deepseek-account/deepseek-flash + max；未公开1017日期版本，勿虚称。

## 源码与已关闭问题

- Python runtime_lock.py为唯一锁算法：固定不删OS guard，原子发布JSON，活/未知不抢、dead归档、pid+nonce释放。Node只调用短命helper、最小env；inspect只读不spawn。
- DataRoot Config > PAWBOT_DATA_DIR > runtimeDir/data；适配旧签名与junction，自定义projectDir接线；实路径重叠拒绝，Windows normcase覆盖祖先/后代。
- rename失败不删原数据。迁移/快照预约运行锁；源父目录稳定 .<dataName>.pawbot-migration 预约覆盖rename→junction间隙。正常acquire在稳定根OS guard中串行化marker判断与源锁发布，不长期持guard跨rename。Node提前mkdir已删。helper失败必须跳过迁移/快照，禁止降级复检。复制排除运行锁与guard元数据，释放按nonce+改名后的路径。
- 控制面仅真实loopback+独立非空Bearer，禁redirect/递归遮罩/鉴权先于body。draining拒绝新变更；取消排空任务和同步线程，真实Store flush成功才ack；非控制finally亦排空，失败不能0/10。
- OneBot worker/login probe登记tasks.spawn，覆盖停机前已断连worker；core保留auto_pull线程引用。Host停止串行化，控制请求+等待→平台兜底；restart成功保留stop证据。
- 测试隔离继承PAWBOT_DATA_DIR/PAWBOT_PYTHON_EXE，离线Pixiv禁止浏览器及后台记录；只修fixture/隔离，不变业务过滤。

## 最新完整证据

- JS14套件277项，0fail；baseline19/19；CRLF感知diff-check通过。最终job pwsh-94f07489edd1438b已收，日志TEMP/pawbot-lead-batch2-gate-serialized-js.log。
- Python1383 tests in68.197s，OK(skip1)，exit0。最终job pwsh-d007f3bc8fb14391已收，日志TEMP/pawbot-lead-batch2-gate-serialized-python.log。
- 稳定预约live/corrupt/dead三项已并入全量；真实rename后竞争者被LOCKED且不重建源，先RED后GREEN。helper错误继承两套22/16全绿（pwsh-0aed9f1e88fe4585已收）。
- 真实run.py控制握手→exit10→Store落盘/锁释放、强杀stale接管、Windows合成launcher/Python/grandchild超时后树清理均已通过。
- 所有Lead后台job均已收，无需重复跑同一任务。

## 代理状态（源码均停止写入）

- dbdd首作者、f2修复作者、4c743b76迁移writer均停止。Lead也已冻结源码。
- dc1a锁专项APPROVE；326 OneBot/restart小补丁无blocking；7f295b0f最新Python线程/非控制收尾无blocking，21项连跑3次OK。
- 6db8d985-9094-402c-b5b9-e7f8d06458ba完整review已结束APPROVE，无blocking。
- **唯一待回报：9c809403-66c7-4b8f-a0f1-168e20153732** 最终稳定迁移协议只读review（含14:07稳定guard序列化delta）。必须收到结论再交付。普通子代理不在Team list_agents；send_message为Team版，不绕过归属。

## 诚实边界/非阻断后续

Windows/NTFS实测，不宣称POSIX/SMB/非NTFS、阿里云服务器、真实DSH reload/dispose、QQ/NapCat真收发已验收；无UI改动截图不适用。release helper失败安全保留活Host预约并警告，需停Host确认后人工恢复，不能在线删guard。固定隐藏guard目录保留是防ABA协议预期。custom projectDir/外部根Host组合、真实SIGINT/SIGTERM/二次中断及真实RealtimeSyncer形态有覆盖缺口。控制端口不一致会强制兜底；custom appDir无可发现helper会跳过迁移/快照。迁移沿用SKIP_DIRS，brain/backups/logs/napcat等留.migrated原目录，新根不自动可见。

## 最终状态（验收完成）

9c809403最终迁移专项已结束，BLOCKING=0；Lead核对五个源码/测试hash与其最终报告一致。所有required reviewer已结束，无阻断。最终JS277项0fail、Python1383项OK(skip1)、基线19/19。全部后台job已收，源码冻结，无提交或真实插件部署。批次2本机验收完成，批次3–7未开展。

稳定guard需要父目录可写；Python外部env alias/Host clean-install并发可让迁移回退但不启动第二实例；rename→link硬崩溃和release失败需要停机核对人工恢复。已在验收记录与DATA-PERSISTENCE明确接受，不把本机验收宣称为生产完备。下次接手先看FRAMEWORK-BATCH2-VERIFICATION最终结论，不重复历史待项。

## 历史收尾步骤（已执行）

1. 收9c最终迁移review；如有可复现blocking修复并按影响复跑，否则不扩展非阻断事项。
2. 更新FRAMEWORK-BATCH2-VERIFICATION、BATCH2-REVIEW、decisions及总交接顶部最终状态；历史RED证据保留并标明已关闭。
3. team task-1 revision2、task-3 revision3目前in_progress；完成时按最新revision关闭，todos全completed，goal goal-0df8f70c-90fa-419d-aa10-b004987ca348 revision1 active/armed最终complete。不能在待review时提前完成。
4. 不提交、不安装/重载真实插件，最终答复主结果+测试+边界+文档链接。
