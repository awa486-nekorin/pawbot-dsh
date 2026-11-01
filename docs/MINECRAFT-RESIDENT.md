# PawBot Minecraft 居民：本机搭建与运维手册

> 状态：**主人手册，按 2026-11-02 00:16–00:30 最终独立复核快照刷新**（HEAD `3dff9ef`，未提交）。
> 最终独立复核报告：[final-verification-2026-11-02.md](../reports/minecraft-review/final-verification-2026-11-02.md)
> （2026-11-02，verdict **READY OFFLINE**）。离线测试数字一律以 [MINECRAFT-ACCEPTANCE.md](MINECRAFT-ACCEPTANCE.md)
> 第 2 节的唯一证据表为准；本文不把任何离线结果写成实机已验证。
> 凡是**实机未验证**的内容都会明确写出：真实 LAN 连接、微软登录、专用副本内 30–60 分钟观察目前一次都没有做过，
> 必须由主人在本机完成。
> 契约以 [MINECRAFT-PROTOCOL.md](MINECRAFT-PROTOCOL.md) 为准；侧车实现契约以
> [app/minecraft-sidecar/README.md](../app/minecraft-sidecar/README.md) 为准；守卫实现与构建说明以
> [minecraft-guard/README.md](../minecraft-guard/README.md) 为准。本文不复述那两处的数字，只给主人要做的步骤。
> 本文不含任何真实 UID、玩家 UUID、密钥或令牌，示例值全是占位符。

## 1. 组成与信任边界

| 组件 | 位置 | 作用 |
| --- | --- | --- |
| Fabric 世界守卫 | `minecraft-guard/`（Java 17，Fabric 1.20.1，**客户端** mod） | 安装到客户端，但只在**单人世界开放到局域网时的集成服务器**上生效；核验世界路径与标记、签发 5 秒有效期的 HMAC 许可、做备份与撤销 |
| Node 侧车 | `app/minecraft-sidecar/`（Node ≥ 22） | Mineflayer 连接 `127.0.0.1` 上的 LAN 世界，只接受预定义 schema 的 17 个技能，stdio JSON-RPC |
| Python 宿主 | `app/pawbot/minecraft_*.py`、`app/pawbot/paths.py` | 守卫客户端、侧车进程监督、居民循环、离线策略、紧急停止 CLI |
| 控制面 | `app/pawbot/webui.py` 的 `/api/control/minecraft-stop`、`app/pawbot/core.py` 的 `minecraft_emergency_stop` | 不经模型的本机紧急停止；闩锁到 PawBot 重启 |

边界（全部由宿主代码强制，不靠角色「自觉」）：

- 世界写入与 PvP 的最终授权来自守卫，不来自模型、侧车自身判断或游戏数据包。
- 侧车只拿到 `PAWBOT_MC_*` 白名单环境变量与基础系统变量；QQ 令牌、模型密钥、人设、记忆不会传进去。
- Microsoft 缓存只放在认证目录（`<数据根>/state/minecraft/auth`），不进日志、事件、模型上下文。
- 默认全关：`minecraft.enabled=false`、`write_enabled=false`、`pvp_enabled=false`、`port=0`。
- 守卫是周期快照的**唯一调度方**：Java 侧 `backup_interval_sec` 是唯一生效的备份间隔；Python 的
  `backup_interval_min` 只是旧配置兼容键，不参与调度；Node 与居民都**不再**运行自己的周期备份计时器。

## 2. 前置条件

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Minecraft Java | **1.20.1**（exact） | 守卫与侧车都只接受 `1.20.1`，其它版本会被拒绝，不会自动适配 |
| Fabric Loader | ≥ 0.15.0（构建用 0.15.11） | 需要主人用官方 Fabric Installer 给 1.20.1 建一个带 Fabric 的启动配置 |
| Fabric API | 0.92.2+1.20.1 | `fabric.mod.json` 依赖 `fabric-lifecycle-events-v1`，缺它守卫不会加载 |
| 游戏运行 JDK | Java 17+ | 守卫 class 版本是 Java 17 |
| Node.js | ≥ 22（本机是 24.14.0） | 侧车 `package.json` 的 `engines.node` |
| Python | 本机 `py -3.13`（项目既有约定） | 居民与 CLI |
| 网络 | **同一台机器**的 `127.0.0.1` | 只支持同机 Open to LAN；不做 LAN 广播发现，也没有 25565 默认端口 |

构建工具链（**PATH 上没有 `java`/`gradle` 不等于没有**）：

- JDK 就用本机已有的 JetBrains Runtime：`C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr`（含 `javac.exe`，OpenJDK 21，用 `--release 17` 产出 Java 17 字节码）。
- Gradle 8.7 与依赖缓存已在 `minecraft-guard/.tooling/`（`bin/gradle.bat`、`gradle-home/`）。首次在空缓存机器上构建需要一次联网解析，之后可 `--offline`。
- `.tooling/`、`build/`、`config/pawbot-guard.json`、`node_modules/` 都已在各自的 `.gitignore` 里；**不要 `git add -f` 提交这些**（`.tooling` 里含上百 MB 的 zip 与 Gradle home）。

## 3. 创建专用一次性测试世界

只能用**新建**的世界。不得复制、改名或接管主人的原存档；守卫会拒绝路径不符的世界，也从不自动接管任何存档。
守卫只把配置的 `save_path` 与集成服务器上报的真实存档根做比较，**不读取原存档内容**，也不复制/改名原存档。

1. 游戏里新建单人世界（下文用 `PawBotTestWorld`），进入一次确认能正常生成，然后**退出到主菜单**（守卫只在世界启动时加载；给已经打开的世界补标记不会让它开始工作）。
2. 在该世界存档目录根部写标记文件 `.pawbot-test-world`，内容**恰好**是 `world_id`（例：`pawbot-test-1`），UTF-8 **无 BOM**、行尾随意（读取时会 `trim()`，但 BOM 不会被去掉）：

   ```powershell
   $marker = '<存档根>\.pawbot-test-world'
   [IO.File]::WriteAllText($marker, 'pawbot-test-1', (New-Object System.Text.UTF8Encoding($false)))
   ```

3. 记录该存档目录的**绝对规范路径**（例如 `C:\Users\<你>\AppData\Roaming\.minecraft\saves\PawBotTestWorld`），供守卫 `save_path` 与 PawBot `world_path` 使用。路径里不能有符号链接、目录联接或重解析点，也不能有结尾的点/空格；**大小写也必须逐字符正确**（见第 5、10 节）。
4. 备份根另选一个**不在存档内、也不包含存档**的绝对目录（例如 `D:\PawBotGuardBackups`），同样不能是链接。

## 4. 守卫：构建、安装、本机配置

### 4.1 构建（本机使用中的精确命令）

```powershell
cd D:\paw-old\pawbot-dsh
$env:JAVA_HOME = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr'
$env:GRADLE_USER_HOME = (Resolve-Path 'minecraft-guard\.tooling\gradle-home').Path
& (Resolve-Path 'minecraft-guard\.tooling\gradle-8.7\bin\gradle.bat').Path `
  -p minecraft-guard build --offline --no-daemon --console=plain
```

产物：`minecraft-guard\build\libs\pawbot-guard-1.0.0.jar`。最终独立复核用上面的命令做了一次 `clean build`
（2026-11-02 00:24）并复现出产物：**70,856 B**，SHA-256
`B373B470F6BDE30A01EEC5C9723340DF6FD81EA43459AA97552FD4AA29D7A82F`；`remapJar` 已执行，
`7 actionable tasks: 7 executed`。你自己重建的 hash 会不同。证据：
`reports/minecraft-review/final-verify/10-guard-clean-build.log` 与 `11-guard-artifact.log`（39 entries、class major 61、
8 个 intermediary / 0 个 yarn-only 引用；另 1 个 yarn 名引用 `MinecraftServer` 属 official identity mapping）。

`build` 会通过 `check` 跑 `selfTest`（JDK-only 回归，无 JUnit），当前用例数为 **63**，最后一行是
`passed=63 failed=0`。`gradle test` 任务是**故意禁用**的（`build.gradle` 里 `test { enabled = false }`，`check` 依赖
`selfTest`）：本工程没有 JUnit 用例，别把「`gradle test` 通过」当成覆盖证据。

> 构建与离线测试由编码方完成；主人不需要为了验收重新构建，除非自己改了源码。改源码后按上面的命令重建，
> 并重新核对产物大小/hash 与 `passed=63`。

### 4.2 安装到客户端

1. 用官方 Fabric Installer 给 1.20.1 装 Fabric Loader（≥ 0.15.0），启动器里会多一个带 Fabric 的配置。
2. 把 **Fabric API**（0.92.2+1.20.1）和 `pawbot-guard-1.0.0.jar` 一起放进该配置对应的 `mods\` 目录。
3. 用该启动配置启动游戏。守卫是**客户端** mod，但只在进入单人世界（集成服务器）时生效；连接别人的服务器时它只会打印
   `[pawbot-guard] refused: not an integrated server` 并退出。

> 这里没有「一键安装」：Fabric 与启动器步骤属于通用操作，本仓库不代做。守卫本身未在真实客户端加载过（见验收文档）。

### 4.3 配置文件与字段

配置位置：`<游戏目录>\config\pawbot-guard.json`（`<游戏目录>` 是启动器里该实例的 game directory，默认
`%APPDATA%\.minecraft`）。字段与约束以 `minecraft-guard/config.example.json` 与
[minecraft-guard/README.md](../minecraft-guard/README.md) §3 为准，这里是操作视角的摘要：

| 字段 | 约束 | 你要填什么 |
| --- | --- | --- |
| `secret` | 32–256 字符，随机 | 与 PawBot 环境变量 `PAWBOT_MC_GUARD_SECRET` **完全相同**；只存本机 |
| `world_id` | `[A-Za-z0-9._-]{1,64}` | 与标记文件内容一致（例 `pawbot-test-1`）；守卫上限是 64，客户端再宽也不会放宽它 |
| `save_path` | 绝对、规范、无链接，**大小写逐字符正确** | 第 3 节记录的存档根 |
| `backup_root` | 绝对、无链接、与 `save_path` 互不嵌套 | 专用备份目录 |
| `actor_uuid` | 精确小写 UUID | **机器人账号**的玩家 UUID（不是主人人类玩家的 UUID） |
| `pvp_uuids` | 精确 UUID 列表，**最多 16**，不含 `actor_uuid`，无通配 | 默认 `[]`；不确定就留空 |
| `enabled` / `write_enabled` / `pvp_enabled` | 只有字面量 `true` 生效 | 初始全 `false` |
| `retention` | 1–30，默认 7 | 保留几份快照 |
| `backup_interval_sec` | 60–1800，默认 1800 | 守卫是新鲜度与周期快照的唯一权威；只能更短，不能超过 1800 |
| `port` | 1024–65535，默认 6198 | 守卫回环监听端口，须与 `PAWBOT_MC_GUARD_URL`/`minecraft.guard_url` 一致 |
| `max_backup_files` / `max_backup_bytes` | 正整数；示例 20000 / 2 GiB | 快照预算 |
| 未知键 | 一律拒绝加载 | 不要塞自定义字段 |

> 客户端（Python/Node）对 `world_id`/`secret`/`pvp_uuids` 的校验比守卫宽；最终以**守卫**为准：
> `world_id` ≤64、`secret` 32–256、PvP 目标 ≤16。超过守卫上限的配置会在运行期被拒，不要拿客户端的宽松值当依据。
> `pvp_uuids` 超过 16 个会直接拒绝启动（`refused: too many pvp targets`）；`target rejected` 表示目标不在守卫名单里。

生成密钥（本机终端跑，输出只写进上面两处，不要贴到聊天、仓库或截图）：

```powershell
$b = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
-join ($b | ForEach-Object { $_.ToString('x2') })     # 64 位小写十六进制，满足 32–256 字符
```

（不要用 `Get-Random -Count 48`：它最多只能从 36 个字符里取，会静默给出 36 长度。）

行为要点（源码核实）：

- 只在集成服务器上启动；配置缺失或 `enabled=false` 时**静默保持关闭**（不打印 `refused:`）；世界未标记/标记不符/路径不符、备份根不可用、端口被占等其余拒绝会打印静态
  `refused: ...` 并保持关闭，不会换端口、不会降级。
- 启动成功打印 `[pawbot-guard] listening on 127.0.0.1:6198`；世界还没开 LAN 时会再打印一行提示，此时所有许可都会被拒绝。
- 定时快照成功打印 `[pawbot-guard] periodic backup ok`。**连接前的备份和连接许可不打日志**，不要拿「没看到备份日志」当失败。
- 每次**成功的** `connect` 只做**一次** `saveAll` + 一次快照（守卫的 `connect` 自己完成）；Node 不再额外请求 `backup`，居民也不再发周期 `backup`。
- 日志只有静态信息：不含密钥、请求体、世界路径、令牌。

## 5. PawBot 侧配置

写在 PawBot 的运行态配置里（`<数据根>/state/config.json`，不入库）。**数据根**由 DSH 宿主解析：
`Config.dataDir` > `PAWBOT_DATA_DIR` > `<appDir>/data`；Python 侧 `run.py` 只读 `PAWBOT_DATA_DIR`
（未设置则用 `app/data`），并在两者不一致时建立/校验目录联接，不一致就 **fail-closed 退出（退出码 2）**，
绝不双端分裂。想知道自己实际用的根：看启动日志第一行 `数据根：...（来源：...）`。

`config.DEFAULT` 里 `minecraft` 段的**全部**键与默认值（下表 33 个键均已在 `DEFAULT` 登记；代码还会读旧别名 `minecraft.decision_cost_usd`，它只作 `decision_cost_usd_per_call` 的回退、不在 `DEFAULT` 里，见本节末——除此之外没有「代码读了但没登记」的键）：

| 键 | 默认 | 必填/作用 |
| --- | --- | --- |
| `enabled` | `false` | 只有字面量 `true` 才拉起居民 |
| `host` | `"127.0.0.1"` | 必须就是 `127.0.0.1`，其它值 → `connect_config_invalid` |
| `port` | `0` | **必须填整数**的当前 LAN 端口；写字符串 `"25565"` 或不填都连不上。紧急停止的 revoke 例外接受 0 |
| `version` | `"1.20.1"` | 只能 `1.20.1` |
| `connect_timeout_sec` | `15` | 整数 1–30 |
| `reconnect_attempts` | `3` | 1–6；断线重连尝试次数 |
| `reconnect_backoff_sec` | `1` | 0.05–30；指数退避基数（每次 ×2，单次封顶 30 秒） |
| `tick_interval_sec` | `3` | 0.5–10；每个「观测→评估→决定→执行」周期 |
| `username` | `""` | 机器人账号别名 `[A-Za-z0-9_]{1,16}`，必须与 `auth.mjs` 用的别名一致 |
| `owner_qq_uid` | `""` | 主人的**完整数字 QQ UID**（`identity.uid_of` 校验；昵称/后缀不算）；只写本机 |
| `owner_player_uuid` | `""` | **机器人账号**的精确 UUID（守卫 `actor_uuid` 同一个） |
| `owner_human_uuid` | `""` | **可选**：主人人类玩家自己的精确 UUID，必须不同于 `owner_player_uuid`；留空表示不区分主人角色 |
| `auth_mode` | `"microsoft"` | `microsoft` / `offline` |
| `test_world_id` | `""` | 必须等于守卫 `world_id`（守卫上限 64） |
| `world_path` | `""` | 必须与守卫上报的 `save_path` **逐字符相同**（含大小写，不做大小写折叠） |
| `guard_required` | `true` | 设成 `false` 会让 `authorize_action` 一律拒绝，等于关掉写入 |
| `guard_url` | `"http://127.0.0.1:6198/v1/permit"` | 只接受回环 origin 或 `/v1/permit`；密钥只从环境来 |
| `permit_max_age_sec` | `30` | 本地只允许更短：实际用 `min(5, 该值)` |
| `write_enabled` | `false` | 设为 `true` 前先完成只读观察 |
| `pvp_enabled` | `false` | 还需 `write_enabled=true` + 精确目标名单 |
| `game_chat_enabled` | `false` | 游戏内 `chat` 技能总开关；不开就永远不会用 `chat` 技能 |
| `pvp_target_uuids` | `[]` | 精确 UUID，无通配；守卫侧上限 16，超过 16 个本地策略会全部拒绝 PvP |
| `backup_interval_min` | `30` | 1–30；**旧配置兼容键，不参与调度**（只被一个未使用的辅助函数读取；守卫 `backup_interval_sec` 才是唯一排程） |
| `backup_retention` | `7` | **当前源码没有读取这个键**；保留份数以守卫 `retention` 为准 |
| `decision_enabled` | `false` | 远程决策总开关（默认关） |
| `decision_interval_sec` | `60` | 30–90；两次远程决策的最小间隔 |
| `decision_remote_enabled` | `false` | 远程决策二级开关；默认关闭，只是「允许」，不是「启用」 |
| `decision_cost_usd_per_call` | `0` | 单次调用成本上限；`0` = 不发起远程决策 |
| `decision_timeout_sec` | `12` | 5–30 |
| `max_decisions_per_day` | `120` | 远程决策次数上限 |
| `decision_budget_usd_per_day` | `0.05` | 金额上限；与单次成本一起决定远程决策是否可能 |
| `owner_mappings` | `{}` | 显式 UUID↔QQ 映射；不按昵称合并，也**不会**据此把玩家提升为主人角色 |
| `interest` | `""` | **当前源码没有读取这个键**；保留只为旧配置兼容 |

`owner_human_uuid` 的准确语义（源码核实）：

- 它和 `owner_player_uuid` 是**两个不同的字段**：后者是机器人 actor，前者是主人人类玩家的头像 UUID。
- 只认精确 UUID；昵称、QQ 号后缀、`owner_mappings`、推断/猜测都不能把玩家变成 owner。
- 值非法、为空、或等于 `owner_player_uuid` 时，一律按「没有配置主人人类身份」处理（不会报错打断启动，也不会让谁变成 owner）。
- 只有该 UUID 的玩家出现在观测里，`owner_present` 才为真；没有配置时永远是 `false`。
- 这仍是**本机配置**，不读真实账号、不读 QQ 资料，也不把 UUID 写进模型上下文之外的日志。
- 居民与政策层现在是**同一套判定**：居民的 `_owner_human_uuid()` 直接委托 `minecraft_policy.owner_human_identity`，
  只接受精确规范化 UUID、必须不同于 bot actor，并对 `owner_human_uuid`/`owner_mappings`/`owner_qq_uid` 的冲突与
  一对多映射一律**失败关闭**（2026-11-02 最终复核源码抽查，G8 已修）。`owner_mappings` 仍然只是跨渠道链接，
  永远不会把谁提升为 owner 角色。

`owner_qq_uid` 仍然只接受**完整数字 QQ UID**；它是「主人身份绑定」的一部分，和 `owner_player_uuid` 一起在启动时校验
（`owner_identity_unbound`）。不要把真实 UID/UUID 写进源码、测试夹具、示例或公开文档。

远程决策的真实启用顺序是：`decision_enabled=true` → `decision_remote_enabled` 不为 `false` →
`decision_cost_usd_per_call`（或旧别名 `decision_cost_usd`）> 0 → `decision_budget_usd_per_day` > 0 →
`max_decisions_per_day` 是 ≥1 的整数。缺任何一条都只用确定性规则回退，**不会**偷偷调模型。
`workspace_capacity` 不是配置键：它从已持久化的居民状态读取。

示例（所有值都是占位符，必须由主人在本机改成真实值；不要照抄成真实账号）：

```json
{
  "minecraft": {
    "enabled": false,
    "host": "127.0.0.1",
    "port": 0,
    "version": "1.20.1",
    "connect_timeout_sec": 15,
    "username": "<账号别名>",
    "owner_qq_uid": "<完整数字 QQ UID>",
    "owner_player_uuid": "<机器人账号的精确 UUID>",
    "owner_human_uuid": "<主人人类玩家的精确 UUID，可留空>",
    "auth_mode": "microsoft",
    "test_world_id": "pawbot-test-1",
    "world_path": "<专用测试世界的绝对路径>",
    "guard_url": "http://127.0.0.1:6198/v1/permit",
    "write_enabled": false,
    "pvp_enabled": false,
    "pvp_target_uuids": [],
    "backup_interval_min": 30,
    "decision_enabled": false
  }
}
```

## 6. 环境变量

PawBot（Python）进程启动前设置；不要写进 `config.json`，也不要放进 argv：

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `PAWBOT_MC_GUARD_SECRET` | 是 | 与守卫 `secret` 相同；侧车启动时缺失 → `sidecar_config_invalid` |
| `PAWBOT_MC_GUARD_URL` | 否 | 环境优先于 `minecraft.guard_url`；origin 或该 endpoint 均可，端口须等于守卫 `port` |
| `PAWBOT_DATA_DIR` | 否 | 数据根（DSH 托管时由宿主注入；独立运行时才需要自己设） |
| `PAWBOT_CONTROL_TOKEN` | 紧急停止要用 | 独立 Bearer 控制令牌；PawBot 启动时会把它从环境里**取走并删除**，所以另开的终端要自己再设一份 |
| `PAWBOT_WEBUI_PORT` | 否 | 控制面端口，默认 6185（仅 CLI/控制路由用） |
| `PAWBOT_MC_LAN_PORT` | CLI 直连回退要用 | 当前 LAN 端口；`revoke` 接受 `0` 或当前 1024–65535，其他动作必须是当前端口 |

其余 `PAWBOT_MC_*`（`WORLD_ID`/`WORLD_PATH`/`ACTOR_UUID`/`PVP_UUIDS`/`WRITE_ENABLED`/`PVP_ENABLED`/`AUTH_DIR`）
由居民代码从配置推导后传给侧车。居民对这 9 个键总是显式赋值，会覆盖父进程里的同名变量，所以**不要手填**、也不要指望手填能生效。

紧急停止的控制面路径（`core._minecraft_guard_settings`）按 **环境变量 > `minecraft.guard_url` > 默认值** 取端点；
而 CLI 的直连回退（`minecraft_control.settings_from_env`）只读环境变量，不读运行态配置。这是两条不同的路径，排障时不要混。

## 7. Microsoft 登录（只能由人在本机终端做）

侧车协议进程**不会**申请设备码；缓存不存在时它只返回静态错误 `Local authentication setup required`，也不会自动退到离线模式。

```powershell
cd D:\paw-old\pawbot-dsh\app\minecraft-sidecar
npm ci --ignore-scripts                                   # 首次安装锁定依赖
$env:PAWBOT_MC_AUTH_DIR = '<数据根>\state\minecraft\auth'  # 绝对路径，不能是 UNC/链接/联接
node auth.mjs <账号别名>                                   # 与 minecraft.username 相同
```

- stdin 与 stdout 都必须接在交互终端上（重定向任意一个就拒绝，退出码 2），设备码只打印在这个终端里，180 秒超时。
- 缓存文件名是 `<sha256(别名) 前 24 位>_<cache>.json`，只写在认证目录；目录用 OS 账户权限保护。
- 脚本不连接游戏；它成功只说明缓存就绪。
- 不要把设备码、缓存内容、登录输出复制到聊天、日志、截图或模型上下文。
- 离线模式（`auth_mode: "offline"`）只在目标 LAN 本来就允许时使用，不绕过任何正版验证；无论哪种模式，登录后的 UUID 都必须等于守卫的 `actor_uuid`。
- 实机未验证：真实微软设备流程、令牌续期、正版账号的 LAN 行为都还没有跑过。

## 8. 启用顺序（每步都能回退）

**关键顺序**：先把世界、标记、守卫配置**登记好** → 进世界 → Open to LAN → 抄端口 → 填 PawBot 配置（含 `port`）→ 重启 PawBot。
反过来做（先记端口、再改守卫并重载世界）一定连错端口，因为**每次关闭/重开 LAN 端口都会变**，而且重进世界会关闭 LAN。
守卫的重启/重载也必须在 Open to LAN **之前**完成（改守卫配置 → 重进世界 → 再 Open to LAN），否则刚记下的端口会失效。

1. 守卫配置先全 `false`，装好 Fabric + Fabric API + 守卫 jar，启动游戏确认能进单人世界（此时守卫静默关闭）。
2. 按第 3 节新建世界、退出到主菜单、写标记文件；确认 `world_path`/`save_path` 是**完全相同的绝对规范路径**（大小写一致）。
3. 填守卫配置（`enabled=true`，`write_enabled=false`，`pvp_enabled=false`）。
4. **进入这个测试世界** → `Esc` → `Open to LAN` → 记下聊天栏里的端口（形如 `Local game hosted on port 54321`）。守卫此时应打印 `listening on 127.0.0.1:6198`。
5. 填 PawBot 配置：`minecraft.port` = 刚才那个端口（整数）、`test_world_id`、`world_path`、`owner_qq_uid`、`owner_player_uuid`、按需 `owner_human_uuid`、`username`、`auth_mode`。
6. 设 `PAWBOT_MC_GUARD_SECRET`（和需要时的控制令牌），把 `minecraft.enabled` 改成 `true`，**重启 PawBot**。
7. 观察日志（判据见第 10 节），先只读观察。确认无异常后，再把**守卫与 PawBot 两侧**的 `write_enabled` 改为 `true`
   （守卫侧改完需要重进世界，因此要再走一次「重进世界 → 重开 LAN → 抄端口 → 改 pawbot.port → 重启 PawBot」）。
8. PvP 保持关闭，除非你已决定目标名单、同意方式与撤销方式（守卫上限 16 个精确 UUID）。

关掉世界再打开时：LAN 端口会变 → 更新 `minecraft.port` → 重启 PawBot。

## 9. 数据位置、备份、恢复与家/习惯

### 9.1 数据位置

| 内容 | 位置 |
| --- | --- |
| 居民状态（项目/承诺/人物/习惯/日记索引/情绪残留） | `<数据根>/content/minecraft/resident.json` |
| 每日日记（Markdown） | `<数据根>/content/minecraft/diary/<YYYY-MM-DD>.md` |
| 远程决策预算（含已花金额与次数） | `<数据根>/state/minecraft/budget.json` |
| actor/world 绑定记录 | `<数据根>/state/minecraft/bindings.json` |
| Microsoft 认证缓存 | `<数据根>/state/minecraft/auth/` |

只新增路径，不迁移、不覆盖旧数据；旧版本不认识这些目录。日志文件仍是 `<数据根>/state/run.log`。

### 9.2 备份（守卫是唯一调度方）

- 每次 `connect` 前只做**一次**快照：守卫的 `connect` 自己 `saveAll` + 复制；Node 不再额外请求 `backup`。
- 之后只由守卫按 `backup_interval_sec`（60–1800 秒，默认 1800）定时快照；成功打印 `periodic backup ok`。
- Python 的 `backup_interval_min` 只是旧配置兼容键，**不参与调度**；居民不再主动请求周期 `backup`，Node 也不再有自己的备份计时器。
- 快照在集成服务器线程内做，先保存再复制；拒绝链接/重解析点与「复制期间世界还在变」；排除根 `session.lock`；有文件数与字节预算（示例 20000 个 / 2 GiB）。
- 保留份数以守卫 `retention` 为准（默认 7），只清理自己生成的 `backup-<13位数字>-<3位数字>` 目录。

演练（请保留输出，作为验收记录）：

1. 连接成功、跑一段时间、看到 `periodic backup ok` 后，列出 `<backup_root>` 下的 `backup-*` 目录。
2. 退出世界（不要在开着世界时恢复）。
3. 按下面命令恢复到**一个不存在的新目录**，再人工登记。

### 9.3 恢复（离线 CLI，必须用新目录）

```powershell
cd D:\paw-old\pawbot-dsh
java -cp minecraft-guard\build\libs\pawbot-guard-1.0.0.jar `
  dev.pawbot.guard.RestoreTool `
  <备份根> <backup_id> <新的目标目录> <world_id> [主人原始世界目录]
```

规则（违反任意一条即拒绝，工具不会「尽量试试」）：

- 目标目录**必须不存在**——所以不可能覆盖任何现有世界，包括主人的原始世界。
- 发布采用「先创建目标目录、再搬入暂存内容」；目标在复制期间被别的进程创建时拒绝，**绝不替换已有目标**。
- 目标父目录必须存在，整条链不得有链接/重解析点；目标的**任一祖先**里若有 `session.lock`（正在运行/被锁的世界）则拒绝，不设深度上限。
- 备份 id 必须是 `backup-<13位数字>-<3位数字>`；备份内的 `.pawbot-test-world` 必须与 `world_id` 精确一致；备份内出现 `session.lock` 视为活世界，拒绝。
- 目标与备份根/源不得互相嵌套；源备份只读，逐文件复制并校验大小，先写 `<target>.restoring`，完成后用不可覆盖的 `createDirectory` 发布，失败清理暂存；清理不跟随联接/链接。
- 第五个参数可以显式保护主人的原始世界路径：目标等于它或位于其内部就拒绝，**即使该原始路径当前不存在**；只做路径比较，不读取原世界内容。
- 恢复出的目录不会自动生效：它已经带有**来自备份本身**的 `.pawbot-test-world`（不要手写、不要改），核对内容等于 `world_id` 后，退出游戏，再人工把守卫 `save_path` 指向它，并按第 8 节流程重进世界。

> 不要用「手工复制 + 自己补 `.pawbot-test-world`」代替这个工具：手写标记会把「来源不可验证」的目录伪装成已校验副本，而工具要求标记来自备份本身。

### 9.4 家、农田与环境习惯（自动、但只在验证后）

- Node 的 `home` 只存在于侧车进程内存；`home` 在 `set_home` 成功前会失败。`set_home` 是**只读观察**技能（不需要写权限，不改世界）。
- 居民在**首次成功观测**后自动 seed：只有当前位置距持久化 home ≤3 格、且维度一致时才发 `set_home`；更远时且 `write_enabled=true` 会先用 `move` 靠近；
  `write_enabled=false` 或走不过去时保持「未 seed」并记录原因，不会把脚下位置偷偷改写成家。
- seed 失败最多 3 次，之后进入 600 秒冷却；重连会重置并重新 seed（侧车进程/内存里的 home 已丢失）。只有子进程接受 `set_home` 后，`home` 才会进入可选技能。
- 农田坐标是**土壤方块** `(x,y,z)`（`farmland`/`dirt`/`grass_block`），作物在 `y+1`；居民只使用观测到的、在脚下 1 格（`y-1`）且水平 2 格内的土壤，跳过已有作物的列；没有观测到可种植土壤时不生成 farm 步骤，整条项目回退到 `shelter`（E2/V2），不会去猜脚下的方块。
- 站位按「到最远一块土的距离最小」挑选，并把超过侧车 4.5 格上限的土**直接丢弃**，所以生成的农田步骤不会再被子进程拒绝；
  最终复核实测最远 `4.123` 格（两块土相同），无土可种时整条项目回退到 `shelter`（验收文档 E2/E3，G3/V2 已修）。
  坐标/作物契约有离线测试，真实世界仍属未验证（L1/L2）。
- 环境观察（`time_of_day`/`weather`/`light`/`roof`）由 Node 从真实 bot 读取，未知时省略而不猜：
  - 游戏内时间只从 `time_of_day` 推导，**不用宿主机 wall-clock**；没有该字段时不会假装「天晚了」。
  - 下雨/雷暴且没有屋顶会提高 `rest`（进屋）候选；已知有屋顶会抑制这个拉动。
  - `return_home` 习惯达到阈值后，只在游戏内傍晚/夜晚或能量低时触发回家。
- 上述习惯的离线测试覆盖「游戏时间、天气、屋顶、回家习惯」；真实天气/屋顶/寻路表现仍属实机未验证。

## 10. 成功判据与排障

成功时应看到（顺序大致如下）：

1. 守卫（游戏侧）：`[pawbot-guard] listening on 127.0.0.1:6198`，之后周期性 `[pawbot-guard] periodic backup ok`。
2. PawBot（`state/run.log`）：`Minecraft resident connected to guard-verified disposable world`，随后是结构化的
   `Minecraft decision` / `Minecraft action`（含 intent、skill、ok、reason、project/promise id）。
3. 断线重连成功时：`Minecraft sidecar reconnected under a fresh guard permit`。

失败时**先查什么**（这些错误码是静态的；日志只记异常类型，不会告诉你哪一条校验失败）：

| 现象/错误码 | 最可能的原因 | 你该做什么 |
| --- | --- | --- |
| `owner_identity_unbound` | `owner_qq_uid` 不是完整数字 UID，或 `owner_player_uuid` 没填/格式不对 | 填完整数字 UID 与精确 actor UUID；重启 PawBot。`owner_human_uuid` 是可选键，不影响这条 |
| `connect_config_invalid` | `host`/`port`/`version`/`username`/`auth_mode`/`timeout` 类型或取值不合规（尤其 `port` 写成了字符串） | 按第 5 节核对；`port` 必须是整数 |
| `invalid_guard_settings` | 环境里没有 `PAWBOT_MC_GUARD_SECRET`；或 `world_id`/`world_path`/`actor_uuid`/端口不合规；**`world_path` 不是逐字符规范路径（含大小写）也会在这里被拒** | 设密钥；核对路径逐字符一致；`port` 为整数 |
| `guard_context_mismatch` | 守卫上报的端口/世界/actor 与本地配置不一致（最常见：LAN 重开后端口变了） | 重抄端口 → 改 `minecraft.port` → 重启 PawBot |
| `guard_world_mismatch` | 本地路径已通过格式校验，但守卫上报的 `save_path` 与配置不是同一字符串（不同目录/不同实际路径） | 用存档的真实规范路径，逐字符对齐 |
| `guard_denied` | 守卫明确拒绝（`lan closed`、`marker mismatch`、`disabled`、`write disabled`、`backup stale`、`revoked`…） | 看守卫是否已开 LAN、标记是否正确、两侧开关是否都开了、是否被撤销 |
| `guard_busy` | 守卫正在做快照（`busy` 签名回执）；不是拒绝，也不是守卫挂了 | 不需要人工处理；居民会暂停一个 tick，侧车以 `-32008` 做有界暂停，**不会重放**被打断的动作 |
| `guard_unavailable` | 守卫没起/端口错/**密钥错（401）**/body 过大/方法错 | 先确认 `listening` 那行；再核对密钥与端口。**401 也会被归成这一类** |
| `guard_expired` / `guard_backup_required` | 时钟漂移 > 1 秒；或该动作没有 `backup_id` | 校准系统时间；先让它成功做一次备份 |
| `sidecar_not_ready` / `sidecar_unavailable` | 侧车进程起不来、Node 缺失、握手超时（10 秒） | 确认 `node -v` ≥ 22；把完整日志交给编码方/复核方，不要自己改业务代码或测试 |
| `sidecar_config_invalid` | 传给侧车的环境变量缺密钥或 actor，或 guard URL 不是回环 | 检查上面第 6 节 |
| `queue_full` | 同时有 8 个以上技能排队（正常不会发生） | 通常意味着卡住的任务；用紧急停止后重启 |
| `actor_mismatch` | 登录的账号 ≠ 配置的 `actor_uuid`（离线/正版模式选错账号） | 用正确账号登录，或修正 `owner_player_uuid` |
| `world_mismatch` | 侧车/守卫上报的世界 id 与 `test_world_id` 不一致 | 核对标记内容与两侧 `world_id` |
| `Local authentication setup required` | 认证缓存缺失/过期 | 按第 7 节由人在终端重做登录 |
| `connect_rejected` / `skill_rejected` / `call_failed` | 具体技能被拒或调用失败；居民只记失败并继续 | 看 `Minecraft action` 的 `skill`/`reason`；连续失败会自动冷却 |
| `resident_latched` | 之前发生过紧急停止/安全停机 | 重启 PawBot 才会解除闩锁 |
| `control_disabled` | 没配 `PAWBOT_CONTROL_TOKEN`，控制路由被禁用 | 这不是 Minecraft 授权失败；按第 12 节用直连回退或重启 PawBot |

另外：`GuardDenied`（`guard_denied`）**不等于**「守卫挂了」；守卫拒绝是正常的安全行为，先按上表查配置与世界状态。
`guard_busy` 更不能当失败：它只表示「守卫活着，正在快照，请稍后重试」，不要因此重启游戏或删配置。

## 11. 撤销演练

1. 发一次带密钥的 `revoke`（最方便的是第 12 节的 CLI；它会走控制面或守卫直连）。
2. 预期：守卫把 `config/pawbot-guard.revoked` 写出来，已登记的机器人被断开，之后**新的**许可请求都返回 `409 revoked`。
   已经进入守卫服务器线程的**复制**可能继续跑完，但该请求不会签发或发布许可：撤销代次（`revocationGeneration`）会在构建、签名前和发布前重新检查，
   排队中/构建中/签名前/发布前的请求都会得到 `409 revoked`，`SnapshotGate.commit` 也会拒绝，绝不发布许可。
   撤销保证的是「不再签发新许可 + 尽力断开 actor」（回执不确认 actor 已被踢出），不是「这一 tick 世界绝不会再变」。
   只有撤销前**已经返回**给客户端的 5 秒许可仍是既有语义：不会被追溯作废，也不是租约。
3. 验证文件确实存在；重启游戏后守卫仍打印 `refused: revoked (delete config/pawbot-guard.revoked to re-arm)`。
4. **重新启用** = 人工删除该文件 **且** 重新进入世界（守卫在内存里粘住了撤销状态，只删文件不会立即恢复）。
5. 撤销不会自动恢复，也不会因重启 PawBot 而消失。

撤销回执的端口语义：`revoke` 允许 `port: 0`（LAN 已关闭）或当前 LAN 端口；其它动作仍要求精确的当前端口。
所以「LAN 关了但撤销成功」是正常的，不应把它当成伪造回执。

**持久化失败的边界（诚实失败）**：守卫在无法持久化撤销标记时返回**无签名的 503**，不会假装成功。
宿主把**裸的已认证 `409 revoked`** 记为「仅内存闩锁」：`revoked:false`、`revoke_durable:false`、`ok:false`、
`revoke_error:"already_revoked_unverified"`（有居民会话的结果；无居民的控制路径会同时写 `error`）；
只有本进程此前确实拿到过**该守卫身份**（URL + world_id + world_path + actor_uuid）
的签名回执，第二次 409 才算「已持久化撤销」。所以「503 → 409」不会再变成成功。
离线回归探针 `reports/minecraft-review/probe/revoke_durability_false_success.py` 已修复并通过
（2026-11-02 00:17 复跑：`no false success`、第二次 `already_revoked_unverified`、`ok:false`；
`final-verify/01-revoke-durability.log`）。运维结论：看到 `ok:false` 且 `revoke_error` 是
`already_revoked_unverified`（无居民控制路径也可能在 `error` 里给出同一字符串）时，
说明守卫已停止签发许可、但**标记是否落盘未知**，请按第 12 节人工核对 `config/pawbot-guard.revoked`，
并把这当成「不完整停止」。

## 12. 紧急停止（不经模型、不经角色）

从强到弱：

1. **退出游戏 / 关闭世界**：集成服务器停止 → 守卫随之关闭 → 侧车 250 ms 看门狗发现后断开。
2. **本机 CLI（推荐）**：在 PawBot 所在机器上执行

   ```powershell
   cd D:\paw-old\pawbot-dsh\app
   $env:PAWBOT_CONTROL_TOKEN = '<与 PawBot 启动时相同的控制令牌>'
   py -3.13 -m pawbot.minecraft_control stop
   ```

   - 它先 `POST http://127.0.0.1:<PAWBOT_WEBUI_PORT 或 6185>/api/control/minecraft-stop`，请求头是
     `Authorization: Bearer <token>`；令牌只从环境读，**不会**出现在命令行参数里。
   - 成功时输出 `{"ok": true, "path": "control", "revoked": true, "resident_stopped": true,
     "sidecar_reaped": true, "writes_drained": true, "latched": true, ...}`，退出码 0。
   - 成功语义：先置本地闩锁（`_minecraft_estop`，先关闸），再确认守卫撤销，再取消并回收居民/侧车（在途技能被隔离、不重放）；回执要求 `latched`/`revoked`/`revoke_durable`/`resident_stopped`/`sidecar_reaped`/`writes_drained` 全为真且无 `error`，闩锁保持到 PawBot 重启。
   - 没有活动居民时，**只有本机完全没有守卫凭证（`PAWBOT_MC_GUARD_SECRET` 为空）**才会走诚实 noop：
     `ok:false`、`revoke_skipped:"no_guard_configured"`，**不**声称已撤销。只要配置了密钥，就必须确认撤销；
     `port=0` 不再让 revoke 直接失败（revoke 接受 0 或当前端口）。
   - 控制面路径会尊重 `minecraft.guard_url`（环境变量优先，其次配置，最后默认），不再只认环境变量。
3. **CLI 回退（没有控制令牌、控制面不可达或已被拒绝时自动走这条路）**：直接用 `PAWBOT_MC_*` 做一次签名
   `revoke`。使用前该终端里必须有：`PAWBOT_MC_GUARD_SECRET`、`PAWBOT_MC_GUARD_URL`（可选）、`PAWBOT_MC_WORLD_ID`、
   `PAWBOT_MC_WORLD_PATH`、`PAWBOT_MC_ACTOR_UUID`、`PAWBOT_MC_LAN_PORT`（`revoke` 可接受 0；其它动作必须是当前端口）。
   输出形如 `{"ok": false, "path": "guard_direct", "revoked": true, "resident_stopped": false,
   "sidecar_reaped": false, ..., "error": "resident_not_reaped"}`，退出码 1。
   **这是部分成功**：守卫已撤销、actor 已被守卫断开，但本进程够不到侧车；接着你要自己重启 PawBot，
   让本地侧车进程被回收。注意直连回退只读环境变量，不读 `minecraft.guard_url`。
   如果守卫回的是**裸的 `409`**（标记是否落盘未知），直连回退输出 `revoked:false`、`revoke_durable:false`、
   `already_revoked:true`、`error:"revoke_not_durable"`：这表示「不再签发新许可，但持久化未确认」，
   请按第 11 节人工核对 `config/pawbot-guard.revoked`。
4. **改配置**：`minecraft.enabled=false` 后重启 PawBot（会走正常停机流程）。
5. **改守卫配置** `enabled=false` 后重进世界（不是即时生效，需要重新进世界）。

注意：

- 控制面要求「真实 loopback 对端 + 独立 Bearer 令牌」；面板 token、查询参数、浏览器代理都不算数。没配
  `PAWBOT_CONTROL_TOKEN` 时该路由返回 `403 control_disabled`。
- 如果 PawBot 是 DSH 宿主拉起的，控制令牌由宿主随机生成并注入，人通常拿不到；此时可用的手段是第 1、3、4 种。
- 闩锁（`_minecraft_estop`）与守卫的撤销标记都**不会**因为重启 PawBot 之外的任何操作自动清除；
  重启 PawBot 会清本地闩锁，但**不会**清守卫的 `pawbot-guard.revoked` 标记，也不会清守卫内存里的撤销状态。
- 由于撤销的持久化证据只来自签名回执（第 11 节），若结果里出现 `revoke_durable:false` 或
  `revoke_error:"already_revoked_unverified"`（无居民控制路径同时有 `error`），**不要把它当成
  「重启后也一定仍撤销」**；请按第 11 节人工核对标记。

## 13. 回滚与升级兼容

- 回滚 = 把 `minecraft.enabled` 置 `false`（保留环境变量也行，它们不会自行启用任何东西）并重启 PawBot。
  守卫侧再按需要置 `enabled=false` 或退出世界。
- 配置段 `minecraft` 是新增段，缺键由深度合并补默认值；`config.DEFAULT` 现有 39 个顶层段。
- `architecture-baseline` 现在**整段冻结** `minecraft`（33 个键：31 个标量/列表默认 + `pvp_target_uuids` + `owner_mappings`），
  新增/删除键或默认值漂移都会让它变红；它不再只是「冻结一部分关键键」。
- 既有路由、配置键与数据格式不变；`core.py` 只在 `minecraft.enabled is True` 且未闩锁时创建居民，
  失败只记一行警告，不影响 QQ 主流程。
- HA 中继实例在启动更早的分支返回，永远不会启动居民；它只在收尾时做防御性清理。
- 居民与 QQ 共用 `bot.persona`、`bot.emotion`、`bot.longmem`，不新建并行人格；记忆新增可选字段 `source`，
  没有该字段的旧记录仍按 QQ/旧格式处理。
- `backup_interval_min`、`backup_retention`、`interest` 保留为旧配置兼容键；其中只有 `backup_interval_min` 被读取但
  **不参与调度**，后两个当前没有读取者。不要把它们当成实际生效的备份/兴趣配置。

## 14. 隐私与主人义务

- 设备码、认证缓存、控制令牌、守卫密钥、世界路径都不进日志、不贴聊天、不进模型上下文。
- 控制令牌与守卫密钥只走环境变量，不进 argv、不进 `config.json`、不进 shell 历史（打命令时用变量赋值，不要直接写在命令行里）。
- 认证目录只在本机；不要放在网络共享或链接目录。
- 测试与文档一律使用临时数据目录与合成数据，不读取运行态人设、私人记忆、真实凭证或主人原始世界。

## 15. 实机未验证与仍开放的限制（详细证据见验收文档）

**实机未验证（只能由主人完成，不能用 mock/离线结果替代）**：

- 真实客户端加载守卫 jar（Fabric Loader/Fabric API、integrated-server 生命周期、真实 kick）。
- 真实 Open-to-LAN 端口获取、端口变化后的重连、同机/局域网地址与防火墙边界。
- 真实微软设备码登录与令牌续期、离线 LAN 兼容性。
- 真实 `saveAll` + 大存档复制耗时、Windows 重解析点在真实存档树的边界。
- 真实恢复演练（备份 → 新目录 → 人工重新登记），以及当前 jar 的 CLI restore smoke。
- 专用副本内 30–60 分钟自主观察与自动日志。
- 真实 JEV 服务接入（地址/认证/超时/预算）；默认关闭 + 规则回退，不阻塞默认态。
- 真实 Node 24 + Mineflayer 4.39.0 在 1.20.1 的寻路/挖掘/放置/农田/箱子/死亡重生/取消。

以上对应验收文档 L1–L8，**一项都不算已验证**。

**仍开放的限制（不是待修缺陷）**：

- **G1**：`backup_retention` 与 `interest` 两个配置键当前没有读取者；保留份数以守卫 `retention` 为准。
- **G2**：`guard_unavailable` 把 401（密钥错）与守卫没起/端口错合并，排障只能靠第 10 节解释。
- **G5**：守卫的周期快照仍在集成服务器线程内联复制；大世界可能卡 tick，真实耗时属实机项。
- 低风险残余 **R1/R2**（没有单一 Java→Node socket 的 busy/revoke 端到端用例；busy 回执的 `attested_at` 无最大年龄），
  详见验收文档第 2 节。

**已修复、不再是缺口（2026-11-02 最终独立复核确认）**：农田可达边界（G3）、无土回退 `shelter`（V2）、
撤销持久化假成功（G4）、协议 §4 过期语义（G6）、测试表快照不一致（G7）、owner-human 双实现（G8）、
语义习惯不形成（V1）。证据见验收文档第 3 节；这些项不需要主人做任何动作。
