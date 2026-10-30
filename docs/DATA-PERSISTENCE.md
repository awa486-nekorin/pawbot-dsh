# 数据持久化：DSH 更新 / 插件重装之后，人设、记忆、API key 不被覆盖

> 适用于 2026-10-27 之后的插件版本（`lib/data-guard.js`）。
> 目标：**插件升级只动代码，不动你的数据**；即使升级把 `app/data` 弄丢，也能从快照捞回来。

---

## 1. 为什么会被覆盖

PawBot 的运行时数据默认在插件目录里：

```text
<插件目录>/app/data/
  config.json      ← 人设之外的几乎全部设置，含 llm.api_key、tts/bili/pixiv 等密钥
  persona.md       ← 人设
  memory/ people/ longmem/ diary/ sessions.json relations.json ...  ← 记忆
```

如果插件更新是「整包替换/重装」，`app/` 被新的包盖掉时，`app/data` 就可能随包一起变回模板。
DSH 自己的插件 Config（profile 里那份）通常不会被插件更新覆盖，但数据目录会。

---

## 2. 两层保护（自动，无需你记得备份）

### 2.1 版本快照（默认开启，零配置）

插件每次加载时对比版本号（`package.json` 的 `version`）：**版本变了就快照一次**数据，
写到数据目录的 `backups/`：

```text
data/backups/plugin-update-<版本>-<时间>/
  config.json          ← 含 API key（文件权限随系统，别外传）
  persona.md
  sessions.json / relations.json / media_ledger.json / memes.json / ...
  memory/ people/ longmem/ diary/
  manifest.json
```

- 保留最近 `dataBackupKeep` 份（默认 5），更旧的自动清掉。
- 数据目录里的 `.data-guard.json` 记录「上次快照的版本」，同版本重复加载不会重复快照。
- 关掉：宿主 Config `dataGuardEnabled=false`（不建议）。

### 2.2 外部数据目录（推荐开启，一劳永逸）

把数据放到插件目录**外面**，插件升级/重装永远碰不到它：

```text
宿主 Config：dataDir = D:/PawBotData
```

生效方式（任选其一）：

- DSH「设置 → 插件 → PawBot」的 Config 里填 `dataDir`，然后重载插件；
- 或在 DSH 启动脚本里设环境变量 `PAWBOT_DATA_DIR=D:/PawBotData`（Config 的 `dataDir` 为空时用它），再重启 DSH。
插件加载时会：

1. 把当前 `app/data` **不覆盖地**复制到 `D:/PawBotData`（外部已有同名文件优先，绝不会被模板盖掉）；
2. 把 `app/data` 改名为 `data.migrated-<时间>` 留档；
3. 在 `app/data` 建一个指向 `D:/PawBotData` 的目录联接（Windows junction / Linux symlink）。

之后所有读写都走外部目录；哪天升级把 `app/` 整个换掉，下次启动会自动重建联接，数据还在。
Windows 上目录改名被占用会自动重试；仍失败时保留原目录并回退，外部副本也保留。不会删除原目录来强行建立联接。日志和状态会提示实际采用的根。

> 非 Windows 也可以：`dataDir` 填绝对路径，会建 symlink（需要文件系统支持）。

---

## 3. 手动操作

DSH 工具：

```text
pawbot_data_backup action=status   # 数据目录、是否外部、快照列表、各记忆目录文件数
pawbot_data_backup action=backup   # 立刻快照一份（data/backups/manual-<时间>/）
pawbot_data_backup action=list     # 只列快照
```

`pawbot_status` 的 `data_guard` 字段也会显示当前数据目录、是否外部、最近快照与警告。

---

## 4. 恢复（升级后发现数据没了 / 被模板盖了）

先 **停 PawBot**（面板「状态 → 重启」不行，要停；或直接关插件），然后任选：

**只恢复人设 + key：**

```powershell
$b = "D:\WorkBuddy\pawbot-dsh\app\data\backups\plugin-update-1.0.0-20261027-195445"
Copy-Item "$b\config.json" "D:\PawBotData\config.json" -Force
Copy-Item "$b\persona.md"  "D:\PawBotData\persona.md"  -Force
```

（如果没设 `dataDir`，目标就是 `<插件目录>\app\data\`。）

**整份恢复：**

```powershell
$b = "...\backups\plugin-update-...\"
Copy-Item "$b\*" "D:\PawBotData\" -Recurse -Force
```

改完重载插件/重启 PawBot。恢复前建议把当前目录也改名留档（`data.before-restore-<时间>`），别二次覆盖。

---

## 5. 和云同步的关系

- 数据守卫解决「本机升级/重装」；
- `docs/CLOUD-SYNC.md` 的云同步解决「换机器 / 多端共享 / 离机备份」；
- 两者可以同时开：守卫管本地，云同步管远端；都开启时同一次改动会各存一份。

---

## 6. 已知边界

- 快照/迁移**不删除**你的文件，也不做内容合并；外部目录与旧目录都可能各留一份（`.migrated-*`）。
- 快照包含 `config.json` 里的明文密钥（否则恢复后就丢 key）——**备份目录别提交 git、别丢到公共网盘**。
  仓库 `.gitignore` 已排除 `app/data/` 下的运行产物。
- 首次启用 `dataDir` 时应先停 PawBot，再重载插件；检测到活实例或无法确认的运行锁时，迁移和版本快照会跳过并返回警告。
- DSH 自己的插件 Config（profile 里那份，含 `dataDir` 等）由 DSH 管理；插件更新通常保留，但建议你在改完后导出/记录一次。
- 版本号没变但包内容变了（同版本重发）不会触发快照；这种情况靠 `dataDir` 或手动 `action=backup`。

## 7. 批次 2：统一运行根与停机保护

2026-10-30 本机 Windows/NTFS 临时根验收通过，最终证据与限制见 [验收记录](FRAMEWORK-BATCH2-VERIFICATION.md)。不等于已在生产 DSH 或云服务器部署。

稳定协调guard需要数据根父目录可写，当前不保证只读app或只读外部父目录部署。迁移时应先停服务，不并发改变dataDir或启动：独立env别名/另一Host的clean-install路径可能让迁移回退并跟随实际联接，虽然运行锁仍拒绝第二实例。若rename之后、建立联接之前硬崩溃，应停机核对外部根与.migrated留档后恢复联接，避免把缺失默认路径当作空数据新安装。预约释放失败会返回警告并安全拒绝后续启动；确认Host停止后再人工恢复，不在线删除固定guard。

数据守卫开启时，Host 按以下顺序选择数据根：显式 Config `dataDir` → `PAWBOT_DATA_DIR` → 运行目录下的 `data`。运行目录默认是插件 `app`；兼容安装若显式选择了含 `run.py` 的 `projectDir`，则以它为运行目录。Host 的 setup/config/brain/napcat/备份和 Python 通过同一根读写。数据守卫关闭时保持旧行为：使用运行目录下的 `data`，不执行外部迁移/快照。

迁移后的目录联接使仍按 `BASE_DIR/data` 寻址的 Python 业务模块继续兼容。已有联接的实际目标优先，若请求不同根则保留并提示；禁止源/目标祖先后代重叠。独立启动时，环境根若与现有真实 `data` 不一致，会拒绝启动并说明修复方式，避免悄悄使用两份配置。修改宿主 `dataDir` 后需停服务并重载插件，单独重启 Python 不会重新解析宿主根。

同一根中的 `.runtime.lock` 记录真实运行进程的 PID、启动时间、Host PID、版本和 nonce。Node/Python 共同使用 `.locks/guard.lock` 上的操作系统锁来串行处理元数据；guard 文件长期存在是正常现象，不要把它当成需要手动删除的残留。进程退出或强杀后内核释放 guard。只有明确死亡的运行 PID 可被归档接管，活 PID 或未知状态均拒绝第二实例；释放时校验 PID 与 nonce。PID 被其他活进程复用时仍拒绝，需人工核实后处理，不按文件年龄抢占。

迁移和版本快照会先预约同一套运行锁，helper 不可用或失败则跳过迁移/快照并返回警告，不降级为只读复检。源目录改名到建立联接之间，还由源父目录中的 `.<数据目录名>.pawbot-migration` 稳定预约保护；Python 在创建数据根前及持 guard 后均检查它，活或未知的预约会拒绝启动，明确死亡的预约不会永久阻塞。该预约目录的 guard 长期存在是正常情况，不应在线删除。

DSH 托管停机先使用独立、短期、仅环境注入的控制 token 请求 loopback `/api/control/shutdown`。成功回执表示入口停止接收变更、在途写入/外发任务结束、Store 真正落盘；任务或落盘失败会返回失败，退出码不能伪报正常。正常独立退出码为 `0`，成功控制停机为 `10`，错误使用其他非零值。控制不可用或超过宿主 `gracefulStopMs`（默认 10000 毫秒）时，Host 才进入平台兜底；Windows 使用 `taskkill /T /F` 清理进程树。强杀不保证尚在内存中的数据落盘，重启结果应保留停止阶段的证据。

成功回执也覆盖实时同步线程和启动后台拉取：先停止或等待线程，超时仍存活则拒绝成功回执；独立信号退出和 `--check` 收尾采用同一排空路径，成功仍保留原退出码。Python 独立环境根同样拒绝与 `app/data` 的祖先/后代重叠，避免循环联接。

成功迁移仍沿用原来的目录筛选：`brain`、`backups`、`logs`、`napcat` 等被排除目录留在 `.migrated-*` 原目录归档，不会自动复制到新根；它们并未删除，但新根运行时不可见。需要这些历史内容时，应先停机并核对归档/目标后人工迁移，禁止直接删除归档。此次没有改变这项既有迁移策略。

锁协议实机验收范围为本机 Windows/NTFS。其他卷或远程文件系统若不支持所需 OS 锁/原子发布，会拒绝启动而不是降低互斥要求；POSIX `flock` 仅代码复核，未做实机验收。Node 锁 helper 依赖可用的系统 Python，解释器发现时间不计入短 `timeoutMs`，最坏有数十秒的有界等待；正常托管路径由 Python 自身持锁，Host inspect 不启动 helper。

控制 token 与面板 token 独立；为空时控制面关闭，只接受真实 loopback peer 的 Bearer 鉴权，不从 URL 查询参数取 token，也不通过面板代理公开。`/api/control/ready` 显示 ready、db_writable、store_dirty 和 shutting_down；可写探针仅能表示检查时刻的状态，后续权限或磁盘状况变化仍可能使 flush 失败。
