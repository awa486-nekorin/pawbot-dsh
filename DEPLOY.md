# 部署到云服务器（Linux）清单

本 bundle 本身是跨平台的（停止进程有 POSIX 分支，走 `SIGTERM` → 超时再 `SIGKILL`），而且
**PawBot 本体已经内附在仓库的 `app/` 里**——装插件就有 Python 源码，不用另外上传项目。

> 一句话：**插件自带 `app/`，一键安装会建 venv 并启动；两端（插件与 PawBot）要指向同一个数据目录。**

---

## 0. 先确认三件事

| 检查 | 命令 | 期望 |
|---|---|---|
| DSH 版本 | `dsh --version` | ≥ `0.1.7-rc.1`（低了会被内核静默跳过） |
| Python | `python3 --version` | ≥ 3.10 |
| 能否连 GitHub | `git ls-remote https://github.com/awa486-nekorin/pawbot-dsh.git` | 能列出 `refs/heads/main` |

服务器连不上 GitHub 时，用 GitHub 页面「Download ZIP」把包传上去，然后装本地目录（见 §1 的 B 方案）。

---

## 1. 装插件（二选一）

```bash
# A. 从 GitHub
dsh plugin --profile <profile> add github:awa486-nekorin/pawbot-dsh

# B. 从本地目录（已解压的 zip）
dsh plugin --profile <profile> add link:/opt/pawbot-dsh
```

装完**必须重启一次 dsh**（bundle 层要重启才组装）。

---

## 2. PawBot 本体：随插件自带（`app/`）

插件里已经包含 PawBot 的完整 Python 源码，位置在**本 bundle 的 `app/`**：

```
/opt/pawbot-dsh/app/
├── run.py              # 启动器
├── requirements.txt    # pillow / websockets
├── pawbot/             # 核心包
├── plugins/            # 内置插件
├── tools/              # 测试 + 辅助脚本
└── data/               # 配置 / 人设 / 记忆模板（密钥字段全为空）
```

安装时插件会建 venv 并启动它（`python run.py`，cwd = `app/`）。需要手动补一遍或排查时：

```bash
cd /opt/pawbot-dsh/app
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt     # 只有 pillow、websockets 两个第三方依赖
python -m py_compile run.py         # 可选自检（不联网、不连 QQ）
```

> 如果插件没有自动找到 Python，就在设置页把 `projectDir` 指到 `/opt/pawbot-dsh/app`、
> `pythonExe` 指到 `/opt/pawbot-dsh/app/.venv/bin/python`（见 §3）。

---

## 3. 改 4 个配置（**不改就连不上**）

在 DSH 设置页 → 插件 → **`@local/pawbot`**：

| 字段 | Linux 上要填什么 | 说明 |
|---|---|---|
| `projectDir` | `/opt/pawbot-dsh/app` | `run.py` 所在目录，**就是插件自带的 `app/`**（装成 npm 包时是 `node_modules/@local/pawbot/app`） |
| `pythonExe` | `python3`（推荐 `app/.venv/bin/python`） | Linux 没有 `py` 这个 launcher |
| `autostart` | 按需 | 打开＝DSH 启动时自动拉起 PawBot |
| `webuiHost` / `webuiPort` | `127.0.0.1` / `6185` | 必须和 PawBot 的 `app/data/config.json` 的 `webui` 段一致 |


在 **`@local/pawbot/bili-watch`** 的配置里：

| 字段 | 要填什么 |
|---|---|
| `dataRoot` | 共享目录，例如 `/opt/pawbot-bili-watch-data`（Linux 默认是 `$HOME/pawbot-bili-watch-data`） |
| `model` / `allowedModel` | 保持 `deepseek-flash`（v4-pro 会被白名单拦掉） |
| `sessdata` | 想让 `watched_level` 升到 `subtitle` 才需要（见 §5） |

### 3.1 大肥鱼的大脑（可选，2026-10-27）

`llm.provider` 默认空＝直连外部 API，老部署不受影响。要用「DSH 进程内 Agent + 电脑工具」：

1. PawBot 设置页「进阶 → 主模型 → 大脑走哪儿」选 **DSH 大脑**（等价于 `app/data/config.json` 的 `"llm": {"provider": "dsh"}`），热生效；
2. 宿主插件 Config 检查 `brainEnabled=true`（默认）与 `brainSandbox`（默认 `workspace-write`；要全盘读写才改 `danger-full-access`）；
3. 验证：`Invoke-RestMethod -Method Post http://127.0.0.1:6185/api/llm/test` → 回「（来自 DSH 大脑 …）」；
   或 DSH 工具 `pawbot_brain action=status`。
4. Linux 上注意：`brainCwd` 默认落在 `app/data/brain`，要保证运行用户可写；电脑工具需要宿主装有
   `@deepseek-ai/dsh-tool-pwsh|bash`、`dsh-tool-fs`、`dsh-tool-fs-search`、`dsh-tool-web`（DSH 桌面/标准发行版自带）。

设计/回滚见 [docs/BRAIN.md](docs/BRAIN.md)。

**PawBot 那一头也要指到同一个目录**（`app/data/config.json`；默认值是相对路径 `pawbot-bili-watch-data`，
插件会用绝对路径覆盖它，手改时填绝对路径最稳）：

```json
{
  "tools": {
    "bili_watch_dir": "/opt/pawbot-bili-watch-data"
  }
}
```

两头不一致的典型症状：插件一直「兜底看片」，PawBot 却说「没看过任何视频」——因为它们各写各的目录。

---

## 4. QQ 那一头

```bash
# NapCat 要在服务器上跑，并让它的「WebSocket 客户端」连到：
ws://127.0.0.1:6199/ws
```

对外的只需要 DSH 自己的 Web 端口；**6185（PawBot WebUI）和 6199（OneBot 反向 WS）都只绑本地**，不要暴露到公网。

---

## 5. 想让 B 站字幕可用（可选）

B 站 AI 字幕要登录才给。三种塞 SESSDATA 的方式，插件按 `env > credential.json > 设置页` 取：

1. 环境变量 `BILI_SESSDATA=<值>`（最省事，重启 DSH 生效）
2. `<dataRoot>/credential.json` = `{"sessdata": "...", "updated_at": "..."}`
3. 插件设置页的 `sessdata` 字段（secret，不落日志）

没配也能跑，只是报告的 `watched_level` 会停在 `desc_comments`（只看简介＋评论），这是如实降级，不是坏掉。

---

## 6. 云服务器上与桌面机不同的地方（**已知降级**）

| 功能 | 状态 | 原因 |
|---|---|---|
| Pixiv 走本地浏览器抓图 | ❌ 不可用 | 需要本机装 Google Chrome，且 `/pixiv登录` 只在桌面机上有意义。建议把 `tools.pixiv_use_browser` 关掉，改用 API 方式 |
| 本地 TTS（CosyVoice） | ❌ 默认不可用 | `tts.server_cmd` 默认为空（没有写死任何本机路径），要用就自己填本機服务的启动命令；默认 provider 是**云端 TTS**，不受影响 |
| 桌面通知 / 打开文件夹 | ❌ 无 | 桌面机专属，不影响核心功能 |
| 优雅停止 | ✅ 反而更好 | Linux 走 `SIGTERM`，PawBot 能正常 flush 存储；Windows 只能 `taskkill /T /F` |
| 控制面板 | ✅ 一样 | 面板是纯 Web，与桌面机无差别 |

⚠️ **QQ 账号风控**：云服务器 IP 登录 QQ 比家用宽带容易触发验证，建议先只跑一个小号试。

---

## 7. 装完怎么确认正常

1. `pawbot_status` → `running: true`、`ui_route: registered (11 routes)`
2. `pawbot_health` → `http_status: 200`
3. DSH 设置 → PawBot → **状态**页应显示「运行中」、连上 NapCat 后「连接数」≥ 1
4. `bili_watch_status` → `model: deepseek-flash`、`has_credential` 与你是否配了 SESSDATA 一致
5. 在群里发一句话，机器人应能正常回复

---

## 8. 上传到阿里云 ECS + 两端同步（2026-10-27）

### 8.1 打包（一条命令）

本地仓库里已有打包脚本，产物**不含** `app/data`（人设/记忆/config/key）、`.venv`、`node_modules`：

```powershell
# Windows PowerShell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pack-server.ps1
# 或 Git Bash / WSL / Linux
bash scripts/pack-server.sh
```

产物在 `dist/pawbot-dsh-<版本>-<时间>.tar.gz`（约 2.5 MB）+ 同名 `.sha256`。
本次已生成两个包（二选一上传）：

| 用途 | 文件 | 说明 |
|---|---|---|
| 安全部署（不含数据） | `dist/pawbot-dsh-1.0.0-20261027-222423.tar.gz` | 2.5 MB；人设/记忆/key 之后用云同步或手工 scp |
| **含完整 key + 人设（不含记忆）** | `dist/pawbot-dsh-1.0.0-*-full.tar.gz` | 约 1.1 MB；含 `app/data/config.json`、`persona.md`、`secrets/`（DSH 全量凭证 + PawBot 子集 + decider.key） |

生成含 key 的人设包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pack-server.ps1 -IncludeSecrets
```

上传后先看 `secrets/README.txt`：把 `credentials.full.yaml`/`credentials.pawbot.yaml` 放到
`~/.dsh/.credentials.yaml`、`decider.key` 放到 `~/.pawbot/decider.key`，并 `chmod 600`。
这个包是**明文 key**：只走 scp/内网，别放公共 OSS 桶，传完删本地和服务器上的 tar.gz。
（sha256 `036bc6513ee51ae52047402c9f5d1fe3622c714d3f31de83c6e4c2bbad23d4a1`）。

上传（任选其一）：

```bash
# A. scp（最直接）
scp dist/pawbot-dsh-*.tar.gz root@<ECS_IP>:/opt/

# B. 阿里云 OSS + ossutil（scp 被墙/走内网时）
ossutil cp dist/pawbot-dsh-*.tar.gz oss://<bucket>/pawbot/
# 服务器上
ossutil cp oss://<bucket>/pawbot/pawbot-dsh-*.tar.gz /opt/

# C. ECS 控制台 → 远程连接 → 文件上传（小文件，浏览器里拖上去）
```

### 8.2 建议：数据放插件目录外（升级不丢）

DSH 插件 Config 里设 `dataDir=/opt/pawbot-data`（或环境变量 `PAWBOT_DATA_DIR=/opt/pawbot-data`），
首次加载会把 `app/data` 迁过去并建联接；之后升级/重装只换代码。

### 8.3 用「云同步」把本机数据搬过去（加密）

1. 本机先 `push`（`sync.passphrase` 必须设，否则含 key 的 config 会被拒绝）：

```powershell
cd D:\WorkBuddy\pawbot-dsh\app
py tools\cloud_sync.py test
py tools\cloud_sync.py push
```

2. 服务器拉取（同一个 provider/url/passphrase）：

```bash
cd /opt/pawbot-dsh/app
python3 tools/cloud_sync.py test
python3 tools/cloud_sync.py pull --overwrite-config
```

3. 想「实时」就在两端都开 `sync.mode=realtime`（冲突默认 `keep_both`，远端包落在
   `data/sync-conflict-<时间>/`，不会互相覆盖）。

> 阿里云没有内置 WebDAV 的话，三条路：① 在 ECS 上跑 Nextcloud/WebDAV 只给同步用；
> ② 用插件自带的 `http` 端点；③ 直接用 `scp` 传 `data/` 打包（不走云同步）。
> 插件目前**不支持阿里云 OSS/S3 协议**，需要的话可以再加一个 adapter。

### 8.4 常驻（systemd）

```ini
# /etc/systemd/system/pawbot.service
[Unit]
Description=PawBot QQ bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/pawbot-dsh/app
Environment=PYTHONUNBUFFERED=1
Environment=PAWBOT_DATA_DIR=/opt/pawbot-data
ExecStart=/opt/pawbot-dsh/app/.venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now pawbot
journalctl -u pawbot -f
```

### 8.5 安全组 / 端口

- 只开 DSH Web 端口（或你自己的反代 443）给公网；
- `6185`（WebUI）、`6199`（OneBot 反向 WS）**只绑 127.0.0.1**，不要进安全组白名单；
- QQ 登录的 NapCat 可以放本机、通过公网 WS 连服务器，也可以服务器上跑（小号先试，云 IP 风控更严）。

### 8.6 上完检查 + 回滚

```bash
curl -s http://127.0.0.1:6185/api/status | head -c 300     # 有 llm_provider/handlers
python3 tools/cloud_sync.py status
python3 tools/decider_once.py --text "测试一句" --addressed  # 决策模型通不通
```

回滚：云同步覆盖前自动备份在 `data/backup-sync-<时间>/`；数据迁移留档在
`data.migrated-<时间>/`；版本快照在 `data/backups/plugin-update-*`（见 `docs/DATA-PERSISTENCE.md`）。
