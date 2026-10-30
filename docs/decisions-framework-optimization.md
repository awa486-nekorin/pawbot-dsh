# PawBot × DSH 框架优化决策记录

> 创建日期：2026-10-29；最后更新：2026-10-30
> 状态：架构已选择；批次 1 基线复跑通过；批次 2 本机验收与独立复核通过（生产限制见验收记录）；批次 3–7 未开展

## 已确定

### 1. 项目与范围

- 唯一主工程：`D:\paw-old\pawbot-dsh`。
- `D:\paw-old\dsh-pawbot`、`D:\paw-old\pawbot-bili-watch`、`D:\paw-old\pawbot-dsh-integration` 只作历史参考，不作为本次修改目标。
- 本次优化目标覆盖稳定性、可维护性和性能，优先级为：稳定性 > 可维护性 > 性能。
- 密钥保密、权限隔离和数据完整性是硬门槛；备份频率、同步便利性等可在性能和运维成本之间权衡。
- 基础框架与业务模块都纳入整体路线，但按小批次实现，避免一次性修改全部业务链路。

### 2. 兼容与部署

- 采用渐进兼容，不进行一次性推倒重写。
- 目标是保留现有 DSH 插件导出、`/pawbot-ui/*` 与 `/api/*` 兼容入口、OneBot/NapCat 行为、配置键、运行时数据格式、B 站文件协议和启动命令。
- 主要生产环境为中国大陆阿里云 Windows Server。
- DSH 管理模式与独立服务模式并存；DSH 不可用时普通机器人可降级运行，高风险设备能力必须明确拒绝，不能伪装成功。
- 允许有限引入成熟依赖，不引入不必要的复杂构建链或托管基础设施。

### 3. Agent 能力与授权

- DSH Agent 目标能力包括完整 DSH 工具、computer-use、设备读写/命令/联网、多轮对话、子代理和工作流；实际系统权限受运行它的 Windows 账户限制。
- 低风险操作直接执行；高风险操作在主人有效授权期间内全部允许。
- 授权可在群聊或私聊发起，但只有 `owner_ids` 中的主人能改变授权状态。
- 默认授权有效期为 1 小时，主人可以在授权时指定其他时长。
- 主人可通过 QQ 撤销授权；撤销使用安全凭据。安全凭据不得写入代码、文档、日志、测试输出或模型上下文，只保存安全校验材料。
- 子代理继承父 Agent 的授权范围和剩余有效期，不得扩大 scope 或延长授权；撤销需同步到 Host、Python、当前 Agent 和所有子代理。
- 授权状态使用带版本号的 capability lease；授权失效或版本不一致时，高风险操作 fail-closed。

### 4. 选定架构

**结论：选择方案 A——渐进式模块化单体 + 授权租约层。**

**理由**：该方案不要求立即迁移现有 Python 模块，也不破坏 OneBot/NapCat、面板和数据格式；可以优先解决 Windows 生命周期、数据目录、跨进程协议和权限控制，并保留未来演进到独立 Agent Supervisor 的路径。这最符合稳定性优先、渐进兼容和单机 Windows 部署约束。

目标骨架：

```text
现有 DSH Host + Python Runtime
  + Protocol v2（兼容旧路由）
  + Authorization Lease / Capability Guard
  + Agent Supervisor
  + DataRoot
  + Windows graceful shutdown
```

## 方案 A 实施顺序

1. 基线与契约冻结：记录入口、版本、宿主能力、测试和数据/路由/配置契约。
2. 统一运行时与数据目录：DataRoot、单实例锁、生命周期编排、Windows 优雅停止与强杀兜底。
3. 配置 schema 单一来源：生成 Python/Node/面板契约，保留旧键迁移。
4. Protocol v2：统一 envelope、request ID、capability lease、revision，旧接口通过 adapter 兼容。
5. QQ 授权租约：主人识别、授权时长解析、撤销、持久化、审计和状态广播。
6. 完整 Capability Guard：按风险、scope、lease、revision 在工具执行前统一检查。
7. Agent Supervisor：父子 Agent 生命周期、权限继承、取消树、日志清理和重启恢复规则。
8. Windows 阿里云运行化：DSH/standalone 双模式、健康检查、升级回滚、部署文档和实机验收。

## 当前批次记录

### 批次 1：基线与契约冻结

**结论**：新增离线架构基线检查器、离线测试和基线文档；测试及检查器只允许在仓库 `reports/` 下创建新 JSON 报告，拒绝覆盖已有文件、`package.json`、`data/` 或 `app/data/`，并检查 junction/符号链接路径。基线报告明确区分静态源码观测与需在真实 DSH Host 验证的行为。

**理由**：后续架构拆分需要共同的回归尺子；将关键入口、兼容路由、桥协议、配置来源和 WebUI 端点写成可执行契约，并先覆盖路径保护和漂移负例，避免“基线工具”本身破坏生产数据或掩盖真实变化。

**交付状态**：候选检查器、测试和文档已安装到主工程；主工程实测基线为 19/19，离线测试为 43/43，Node 语法检查通过。独立复核正在确认最终修订未遗留阻断问题，复核完成后再正式关闭批次。

### 批次 2：统一数据根与生命周期（实施中）

**计划**：统一 DataRoot 与迁移 adapter；DSH 托管与独立启动共用单实例锁；增加独立 token + loopback 的 ready/shutdown 控制面；Host 优先优雅停机，再按平台兜底强杀。详细范围与验收见 `docs/PLAN-BATCH2-2026-10-30.md`。

**执行**：用户要求 DeepSeek Flash、Max 思考强度。实际工具确认 `deepseek-account/deepseek-flash` 显示为 DeepSeek-V4.1-Flash，支持 `max`；目录未公开“1017”的日期版本，不额外断言。一个子代理负责完整实现，Lead 独立验收和文档，完成后再做干净上下文复核。

**开工回归**（尚无批次 2 源码改动时）：架构基线 19/19；8 组现有 JS 离线测试共 198 通过、0 失败。系统 Python 3.13 未装 pytest，项目现有测试采用 unittest，执行 `py -3.13 -m unittest discover -s tools -p 'test_*.py' -q`，1319 项中 1317 通过、1 失败、1 跳过。唯一失败 `tools.test_pixiv_session.PixivSessionTests.test_filters_and_sorting` 已单独复现，期望 ID `9,8,2,7`，实际 `9,8,7,10`；这是接手基线问题，禁止为本批次绿灯改变 Pixiv 业务语义。

**测试环境修正**：现有 Pixiv 主页、搜索路由和图片标记测试未完整隔离浏览器自启动/后台浏览记录，初次回归意外拉起专用 Chrome。Lead 已按父进程、启动时间与专用目录确认并关闭本轮测试进程，补齐就绪/浏览记录 fixture、Popen 阻断与 `assert_not_called`。原缺陷路径已实跑为 3 个失败，修复后主页整组 14/14 通过、0.273 秒；不修改业务代码、不删除已有浏览器用户目录。

**既有失败修正**：旧排序测试的注释/fixture 假设含 `child` 标题的 SFW 结果可放行，与当前 `_allowed` 明确规定“未成年相关结果始终排除”矛盾。只将排序 fixture 的该标题改为中性场景，保留原排序断言；新增通过搜索完整流程验证 SFW/R18 两种模式均排除相关结果。两组 Pixiv 测试 32/32 通过。临时在测试进程内令 `_allowed` 总是放行后，新测试的两种模式均失败；恢复后通过，业务源码无改动。

## 批次 2 独立复核后的决定（修复中）

第一版实现已报告 JS/真实 run.py 验收通过，Python 全量 1354 项 OK（跳过 1）；独立复核及 Lead 负例仍发现互斥、数据保留和停机回执问题，故保持批次进行中。确认清单与复现见 `docs/BATCH2-REVIEW-2026-10-30.md`，最新验证记录见 `docs/FRAMEWORK-BATCH2-VERIFICATION.md`。

- 不再采用按 mutex 文件年龄回收的算法：活 PID/未知状态不能被墙钟阈值抢占，文件 read→unlink 也不能保证身份。修复收敛到固定 guard 文件上的 OS advisory lock，进程退出由内核释放；跨语言元数据操作共享 Python 实现，Node inspect 仍只读。具体实现和文件系统支持边界待最终复核确认。
- 取消迁移中的删除原目录兜底：rename 失败就保留原目录回退，防止 copyTree 跳过的 brain/backups/logs 等目录被一并删除。外部同名文件仍保持优先；还须防止数据根祖先/后代重叠与运行中迁移。
- 停机成功回执要求所有写入/外发工作结束；包括面板、brain handler、OneBot worker 和周期生产者。仅调用 cancel 不等于任务已结束；超时有 pending 必须失败回执，不能缩小原停机契约。
- 退出码在 finally 清理后决定：0 为正常、10 为成功控制停机；stop/final flush/cleanup 失败均不能返回这两个成功状态。
- setup/boot 写配置也须使用统一 DataRoot，包含自定义 projectDir≠appDir；将其保留为旧布局缺陷不符合本批目标，允许局部扩展 setup.js 并保持旧签名默认行为。
- 控制面出站仅字面 loopback、不得跟随重定向、反射 token 须遮罩；控制 token 消费后不留给业务孙进程继承。

Lead 已独立实跑 Windows 真实合成进程树：控制 HTTP 超时 → taskkill /T /F，launcher/Python/孙进程全退出，1/1。未在真实 DSH 加载器、QQ/NapCat、云服务器或 POSIX 系统验收，不扩大结论。

## 批次 2 最终收尾决定

- OneBot worker 和登录探针也登记到共享 tasks registry，断连映射删除不代表任务清理已结束。仅关闭前快照不足以涵盖停机前已断连的任务；独立真实OneBot回归先失败再通过，禁止退回裸create_task。
- 重启结果向后兼容增加stop字段，原样保留停止方式、graceful和遮罩后的控制回执。重启成功不等于旧实例优雅落盘。
- 成功迁移保留既有SKIP_DIRS筛选，不在本批扩展历史目录自动copy-forward；原归档必须保留。新根不可见的历史brain/backups/logs/napcat内容需停机核对后人工迁移，运维文档明确说明。
- 实机支持证据限于Windows/NTFS，非NTFS/SMB及POSIX无实机结论；不支持的OS锁/发布能力保持failclosed。短timeoutMs不覆盖解释器发现时间的限制已记录。

- 所有退出路径共用排空；控制停机外的信号/自检finally也必须排空后再flush与释放锁，成功保持原退出码。后台启动拉取Thread须留引用，实时同步stop后须检查is_alive；只等待协程不涵盖线程，超时线程仍活则失败。
- 独立env数据根也拒绝祖先/后代重叠；测试进程内apply必须隔离继承PAWBOT_DATA_DIR，不能向用户外部根写marker/快照。

## 批次 2 稳定迁移预约

迁移复制和版本快照期间实际预约源/目标运行锁，而非仅做inspect。源rename到建立junction之间还要有父目录稳定预约：`.<数据目录名>.pawbot-migration`，Python acquire在创建目录之前和取guard之后复检；同一个Node owner继续自身预约，其他活/未知owner拒绝，dead不永久阻塞。Node不再提前mkdir，以免被拒者把切换间隙重建成空根。复制不携带运行锁、guard或归档，释放按原nonce和改名后的实际路径。

helper缺失/异常/超时必须停止迁移/快照，不允许退回“没有Python对手”的假设。稳定门新增live/corrupt/dead回归，真实rename后竞争者回归先RED再GREEN；最终全量JS277项与Python1383项OK(skip1)，尚待最终迁移专项和完整review报告才关闭批次。

## 待定但不阻塞当前批次

- 授权消息的最终命令语法和时长格式。
- 高风险能力的最终分类策略表。
- DSH 宿主实际可用的 headless Agent、subagent、workflow、computer-use API，需要在真实 Host 做能力探测与验证。
- Windows 服务的具体安装方式（计划任务、NSSM 或原生服务包装器），在目标服务器验证后决定。
