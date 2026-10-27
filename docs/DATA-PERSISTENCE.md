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
Windows 上偶尔 `rename`/`rm` 被占用会自动重试；仍失败时保持原目录并保留外部副本（日志里有警告）。

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
- 首次启用 `dataDir` 时如果 PawBot 正在运行，迁移会在插件重载时进行；建议先停服务再改。
- DSH 自己的插件 Config（profile 里那份，含 `dataDir` 等）由 DSH 管理；插件更新通常保留，但建议你在改完后导出/记录一次。
- 版本号没变但包内容变了（同版本重发）不会触发快照；这种情况靠 `dataDir` 或手动 `action=backup`。
