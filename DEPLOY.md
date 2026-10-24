# 部署到云服务器（Linux）清单

本 bundle 本身是跨平台的（停止进程有 POSIX 分支，走 `SIGTERM` → 超时再 `SIGKILL`），
但**它管的是一个外部 Python 项目 PawBot**，那个项目不在本仓库里。所以部署分两部分。

> 一句话：**插件能直接装，PawBot 要另外上传，并且两头必须指向同一个共享目录。**

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

## 2. 上传 PawBot（本仓库不含它）

把整个 `pawbot/` 项目放到服务器，例如 `/opt/pawbot`，并装依赖：

```bash
cd /opt/pawbot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt     # 只有 pillow、websockets 两个第三方依赖
```

> `requirements.txt` 里只有 `pillow>=10.0` 和 `websockets>=12.0`，其它全是标准库。

可选自检（不联网、不连 QQ）：

```bash
python3 -m py_compile run.py
```

---

## 3. 改 4 个配置（**不改就连不上**）

在 DSH 设置页 → 插件 → **`@local/pawbot`**：

| 字段 | Linux 上要填什么 | 说明 |
|---|---|---|
| `projectDir` | `/opt/pawbot` | `run.py` 所在目录。默认值在 Linux 是 `/opt/pawbot`，路径不同就改这里 |
| `pythonExe` | `python3`（或用 venv 的绝对路径 `/opt/pawbot/.venv/bin/python`） | Linux 没有 `py` 这个 launcher |
| `autostart` | 按需 | 打开＝DSH 启动时自动拉起 PawBot |
| `webuiHost` / `webuiPort` | `127.0.0.1` / `6185` | 必须和 PawBot 的 `data/config.json` 的 `webui` 段一致 |

在 **`@local/pawbot/bili-watch`** 的配置里：

| 字段 | 要填什么 |
|---|---|
| `dataRoot` | 共享目录，例如 `/opt/pawbot-bili-watch-data`（Linux 默认是 `$HOME/pawbot-bili-watch-data`） |
| `model` / `allowedModel` | 保持 `deepseek-flash`（v4-pro 会被白名单拦掉） |
| `sessdata` | 想让 `watched_level` 升到 `subtitle` 才需要（见 §5） |

**PawBot 那一头也要指到同一个目录**（`data/config.json`）：

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
| Pixiv 走本地浏览器抓图 | ❌ 不可用 | `pawbot/pixiv_session.py` 硬编码校验 Chrome 配置目录以 `D:\` 开头。建议把 `tools.pixiv_use_browser` 关掉，改用 API 方式 |
| 本地 TTS（CosyVoice） | ❌ 默认不可用 | 默认 `server_cmd` 指向 `D:/CosyVoice/...`；但默认 provider 是**云端 TTS**，不受影响 |
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
