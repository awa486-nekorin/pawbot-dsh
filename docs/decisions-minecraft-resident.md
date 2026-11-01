# 决策记录：PawBot 作为 Minecraft Java 世界中的自主居民

- 状态：**源码与离线测试已推进；最终独立复核已于 2026-11-02 完成（READY OFFLINE）；实机未连接**。默认关闭，未连接任何真实游戏或真实 LAN。
- 快照：2026-11-02 00:16–00:30 最终独立复核快照（HEAD `3dff9ef`，未提交）。测试数字都来自这一份快照，唯一证据表见
  [MINECRAFT-ACCEPTANCE.md](MINECRAFT-ACCEPTANCE.md) 第 2 节；报告
  [final-verification-2026-11-02.md](../reports/minecraft-review/final-verification-2026-11-02.md)（verdict READY OFFLINE）。
- 范围：Minecraft Java 1.20.1、同机 Open-to-LAN、原版玩法、Microsoft 与离线两种认证方式、**专用一次性测试世界**。
- 运行态与源码目标分离：本次工作只改 `D:\paw-old\pawbot-dsh`；不部署到另行托管的 PawBot 进程，不读取运行态数据、
  真实凭证或原存档。
- 相关文档：[MINECRAFT-PROTOCOL.md](MINECRAFT-PROTOCOL.md)（契约）、[MINECRAFT-RESIDENT.md](MINECRAFT-RESIDENT.md)
  （主人手册）、[MINECRAFT-ACCEPTANCE.md](MINECRAFT-ACCEPTANCE.md)（覆盖与证据）。

## 决策（仍然有效）

1. **默认关闭。** `minecraft.enabled=false`、`write_enabled=false`、`pvp_enabled=false`、`port=0`。
   配置必须指明专用测试世界的身份；仅凭协议连接状态无法证明当前是哪个集成服务器存档，因此「连上了」不构成授权。
2. **写入与 PvP 必须先由本机 Fabric 守卫证明。** 缺失、过期、畸形、世界/端口/actor 不符或无法验证的证明一律拒绝；
   守卫以 `MinecraftServer.getSavePath(WorldSavePath.ROOT)` 为准，从不相信调用方给的路径或布尔值。路径按精确规范
   拼写比较，大小写/别名写法在本地设置校验阶段就被拒绝，不会拖到每次许可才失败。
3. **写入与 PvP 是两种能力。** PvP 额外要求精确 UUID 白名单（守卫上限 16，无通配，不含 actor），且同时要求
   `write_enabled`。模型不能点名技能，也不能提供代码。守卫是最终上限：`world_id` ≤64、`secret` 32–256、
   PvP 目标 ≤16；客户端更宽的值不能反过来放宽守卫。
4. **模型输出不含可执行内容。** Mineflayer 侧只暴露有界、参数 schema 固定的 17 个命名技能；模型只能选高层意图。
5. **Minecraft 的记忆落 `content/minecraft/`**，与 QQ 对话分开，episode 带 `source=minecraft` 后才进入共享回忆；
   认证与守卫状态在 `state/`，不进内容同步。只复用 `bot.persona`、`bot.emotion`、`bot.longmem`，不新建并行人格。
6. **备份只有一条调度。** 守卫的 `backup_interval_sec`（60–1800 s，默认 1800）是唯一周期排程；连接时只由守卫的
   `connect` 做一次 `saveAll` + 快照，Node 不再额外请求 `backup`，居民也不再主动请求周期备份。Python 的
   `backup_interval_min` 只保留为旧配置兼容键，**不参与调度**；`backup_retention` 无读取者，保留份数以守卫
   `retention` 为准。
7. **撤销是粘性的。** 撤销后不再签发许可、断开 actor、写 `config/pawbot-guard.revoked`，重启游戏仍有效；重新启用必须
   由人删标记**并且**重建守卫（重进世界）。只删文件不会立即恢复。撤销回执允许 `port:0`（LAN 已关闭）或当前端口；
   其他动作仍要求精确当前端口。守卫在签名前再次校验撤销代次，发布与撤销串行化，排队中的许可不能在撤销后签发。
8. **紧急停止不经模型，且必须诚实报告。** 宿主先置本地闩锁（`_minecraft_estop`，先关闸）并保持到 PawBot 重启；再尝试撤销守卫授权（持久化按回执诚实报告：只有签名回执、或本进程此前记录过该守卫身份的签名回执后再收到已认证 `409`，才给出 `revoked`+`revoke_durable`；裸 `409` 只算内存闩锁，报 `already_revoked_unverified`）；随后取消并回收居民/侧车，等侧车进程确认回收、写入排空。
   `ok:true` 只应在每一步都确认时出现；没有活动居民时，只有**完全没有** `PAWBOT_MC_GUARD_SECRET` 才是
   `ok:false` 的诚实 noop（`revoke_skipped="no_guard_configured"`）。控制面路径尊重 `minecraft.guard_url`
   （环境变量优先），CLI 直连只读环境变量。
9. **主人人类身份是显式可选项，不是推断。** `owner_player_uuid` 是机器人 actor；可选的
   `owner_human_uuid` 是主人人类玩家自己的精确 UUID，必须不同于 actor。只有该精确 UUID 能获得 owner 角色，
   `owner_present` 只在观测到它时为真；昵称、QQ 后缀、`owner_mappings` 都不能提升角色。`owner_qq_uid` 仍只接受
   完整数字 UID，且只写本机配置。
10. **远程决策默认不可用。** 需要同时显式打开 `decision_enabled`、不关闭 `decision_remote_enabled`、配置单次成本上限
    与每日金额/次数上限；缺任一项都只用确定性规则回退。预算在调用**之前**扣，失败/超时/非法输出不退款。
11. **家由显式 `set_home` 建立，失败不伪装成功。** Node 的 `home` 只存在进程内存；居民在首次验证观测后自动 seed
    （observe 许可），只有距持久化 home ≤3 格才 seed，更远时先 move；失败 3 次/600 秒冷却，重连重置并重 seed；
    未 seed 时 `home` 不进入可选技能，也不会把脚下位置偷偷改写成家。
12. **快照忙是暂停，不是拒绝。** 守卫在服务器线程快照期间返回签名的 busy 回执（精确 16 字段、`can_write=false`、
    `can_pvp=false`、`backup_id=""`、bounded `retry_after_ms`）。`observe` 可接受；变更动作暂停并重试，**不重放**，
    不隔离连接。侧车以 JSON-RPC `-32008` 表达同一语义；进程看门狗仍是 250 ms/650 ms，不因快照延长。
13. **恢复只写新目录。** `RestoreTool` 离线执行；目标必须不存在，发布用「创建目录再搬入」而不是可覆盖的原子移动；
    目标父链不得有链接/重解析点，任一祖先含 `session.lock` 即拒绝；备份 id/marker 必须精确，源备份只读；恢复出的目录
    必须人工重新登记 `save_path`，绝不自动采用。

## 当前实现（源码事实，2026-11-02 最终独立复核快照）

- `config.DEFAULT` 的 `minecraft` 段默认关闭，全部 33 个键已登记（含 `owner_human_uuid`、`game_chat_enabled`、
  旧兼容键 `backup_interval_min`/`backup_retention`/`interest`）；`architecture-baseline` 整段冻结这 33 个键。
- `app/pawbot/core.py` 只在未闩锁且 `enabled is True` 时构造居民，失败只记警告；HA relay 在更早分支返回，永不启动居民。
  `minecraft_emergency_stop` 先关闸、再撤销/停止/回收，返回结构化结果；控制面端点经 loopback + 独立 Bearer。
- `minecraft_guard.py`：回环许可客户端。先验签再看字段；校验 nonce/action/actor/target/world_id/精确规范路径/host/端口、
  `expires_at≤issued+5`、未来漂移 ≤1 s、`backup_id` 形状；`connect`/`write`/`pvp`/`backup` 必须有 `backup_id`。
  `revoke` 只放宽 LAN 端口（0 或当前 1024–65535），并单独识别已撤销的 `409`。
- `minecraft_policy.py`：离线授权（`enabled`、`guard_required`、owner 绑定、世界 id、能力位、精确 PvP 目标）；
  `owner_human_identity` 提供可选的精确主人人类 UUID；`WorldPermit` 本身不是密码学证据，必须由守卫许可构造。
- `minecraft_sidecar.py`：子进程监督（ready 握手、有界请求、事件、超时隔离、只重放 connect、指数退避、回收）；
  子进程环境是白名单；`SidecarError.code` 只传整数 JSON-RPC 错误码，用来把 `-32008` 识别为有界暂停。
- `minecraft_resident.py`：有限工作空间、appraisal/情绪残留、人格与驱动、项目/承诺/地点/人物/习惯/日记/长期记忆、
  ImpulseEngine/Arbiter/确定性规划器/JEV 适配器、预算、防循环、失败记录、断线重连。家由 `_ensure_home_seed`
  自动 seed/reseed；农田从观测到的土壤方块生成；天气/时间/屋顶从观察字段进入 appraisal 与习惯，不用 wall-clock。
- `minecraft_control.py` + `webui.py` 控制路由 + `core.minecraft_emergency_stop`：本机紧急停止，令牌只走环境变量、
  只接受 loopback + 独立 Bearer；没有控制令牌时 `403 control_disabled`。
- 守卫（`minecraft-guard/`，**客户端 Fabric mod，仅绑定集成服务器生命周期**）：配置严格加载、世界/标记/链接校验、
  回环 HTTP（单路由，先鉴权后读 body，2 s 读超时，3 worker）、HMAC 签名响应、nonce 重放缓存、撤销代次与持久化、
  单 ticket 快照门、有界且链接安全的快照与保留策略、离线恢复 CLI（目标不存在、标记取自备份、可显式保护原存档路径、
  发布不可覆盖）。
- 离线证据（同一份 2026-11-02 快照）：守卫 `clean build` 成功、`selfTest` **63/63**、产物 **70,856 B**
  （SHA-256 `B373B470…A82F`，`remapJar` 已执行）；侧车 **115/115**（4798.5569 ms）、审计 0、语法 8/8；
  Python 聚焦 **201 OK**（43.540 s）、全量 **2179 OK（skipped=2，288.260 s）**；架构基线 **19/19**、
  离线测试 **46/0/0**。命令、日志与跳过项见验收文档第 2 节。
- **没有**实机 LAN/微软登录/恢复演练/30–60 分钟观察；**没有**任何一项实机行为被验证。

## 什么时候可以认为「接入了」（仍待完成的门槛）

- 在真实客户端加载守卫 jar，并在真实单人世界上验证 LAN 端口、`saveAll`+快照、标记与路径校验。
- 主人完成一次真实恢复演练，并把恢复出的目录**人工**登记为新 `save_path`。
- 两种认证模式各做一次真实连接；确认 `actor_uuid` 与登录账号一致；如要区分主人，填写 `owner_human_uuid` 并确认
  owner 角色只在那个精确 UUID 上出现。
- 在专用副本里完成 30–60 分钟自主观察并保留自动日志；证明项目延续、中断、恢复、家/农田/天气习惯与情绪残留。
- 主人对 PvP 政策、备份频率/保留、JEV 接口与预算作出书面答复；在此之前 PvP 与远程决策保持关闭。
- 离线门槛已由 2026-11-02 最终独立复核关闭（READY OFFLINE）；但离线绿灯不等于验收完成，本节其余实机项仍未做。
- 全程不得连接或写入主人原存档、公共服务器或第三方服务器；不能确认目标是获准副本时，写入技能必须拒绝。

## 已知偏差与待补（不要在文档里绕过去）

**已修复、不再列为缺口（2026-11-02 最终独立复核确认）**：G3 农田可达边界（站位重选 + 丢弃超过 4.5 格的土，
`fix-farm.log`）、V2 无土回退 `shelter`（`fix-farm-fallback.log`）、G4 撤销持久化假成功（裸 409 只算内存闩锁，
`ok` 要求 `revoke_durable`，`fix-revoke.log`）、G8 owner-human 双实现（居民委托 `owner_human_identity`，
`fix-identity.log`）、V1 语义习惯形成（原先三个语义分支是死代码，`fix-habit-formation.log`）、G6 协议 §4 过期语义
（定稿时已改协议）、G7 测试快照口径（最终复核给出唯一一张表）。逐项证据见验收文档第 3 节。

**仍开放的偏差（限制，不是待修缺陷）**：

- `backup_retention` 与 `interest` 仍没有读取者；`backup_interval_min` 只作兼容提示。详见验收文档 G1。
- `guard_unavailable` 仍合并 401（密钥错）与守卫不可达；排障只能靠文档解释。详见 G2。
- 守卫的定时快照仍在服务器线程内联执行，大世界可能卡 tick；真实耗时未测。详见 G5。
- 低风险残余 R1/R2：没有单一 Java→Node socket 的 busy/revoke 端到端用例；busy 回执 `attested_at` 无最大年龄。
  详见验收文档第 2 节。
