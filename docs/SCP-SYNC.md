# 一鍵同步：Windows 本機 ↔ Windows 雲伺服器（只傳人設/記憶）

> 你的雲伺服器是 Windows，所以路徑用 `D:\PawBotSync`，按鈕用 `.bat`（不是 `.sh`）。
> 只同步 `app/data/content/`（人設、記憶、人物、日記、貼紙）；**不傳** `state/config.json`、`sessions`、`browser` 快取、密鑰。

## 1. 一次性設定

### 1.1 雲伺服器：建同步目錄

在雲伺服器（RDP 或 PowerShell）執行：

```powershell
New-Item -ItemType Directory -Force D:\PawBotSync
```

### 1.2 雲伺服器：開 OpenSSH Server（本機 scp 要用）

以**系統管理員** PowerShell：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server" -Enabled True `
  -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

> 阿里雲安全組也要放行 22（只放你的家用 IP 更安全）。

### 1.3 設定免密 SSH（本機 Windows）

```powershell
# 沒有 key 就生成
ssh-keygen -t ed25519 -C "pawbot-sync"

# 印出公鑰，整行複製
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

到**雲伺服器**（系統管理員 PowerShell），把剛剛那行公鑰貼進 `$key`：

```powershell
$key = "ssh-ed25519 AAAA...你的公鑰"
Add-Content -Path C:\ProgramData\ssh\administrators_authorized_keys -Value $key
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r `
  /grant "Administrators:F" /grant "SYSTEM:F"
Restart-Service sshd
```

本機測試：

```powershell
ssh Administrator@雲伺服器IP "echo OK"
```

不應再問密碼。

### 1.4 填 `scripts/scp-sync.json`

```json
{
  "bundle": "pawbot-content.tgz",
  "remote": {
    "os": "windows",
    "host": "雲伺服器公網IP",
    "port": 22,
    "user": "Administrator",
    "key": "C:/Users/Administrator/.ssh/id_ed25519",
    "dir": "D:/PawBotSync"
  },
  "local": {
    "data_dir": "",
    "bundle_dir": "D:/PawBotSync"
  },
  "keep_backups": 5
}
```

- `key` 可留空（用 ssh-agent / `~/.ssh/config`）。
- `local.data_dir` 留空 = 自動找 `<部署根>/app/data`（外置資料根會自動解析）。
- 雲伺服器上的同一份 JSON 不用改；它的按鈕走 local 模式，不看 `remote.host`。

## 2. 按鈕

### 本機 Windows（雙擊）

| 按鈕 | 作用 |
| --- | --- |
| `1-进入MC模式-拉取云端.bat` | scp 從雲端拉包 → 備份本機 → 導入 content |
| `2-结束MC模式-推回云端.bat` | 打包本機 content → scp 到雲端 |
| `3-查看同步状态.bat` | 查看本機/雲端同步包狀態 |

### 雲伺服器 Windows（雙擊）

| 按鈕 | 作用 |
| --- | --- |
| `2-push-cloud.bat` | 把雲端 PawBot 最新 content **導出**到 `D:\PawBotSync\pawbot-content.tgz` |
| `1-pull-cloud.bat` | 把同步包 **導入**雲端 PawBot 的 content |
| `3-sync-status.bat` | 查看狀態 |

## 3. 日常流程

### 進 MC 前

1. 雲端停止 PawBot
2. 雲端雙擊 `2-push-cloud.bat`（導出最新資料）
3. 本機雙擊 `1-进入MC模式-拉取云端.bat`（拉取並導入）
4. 啟動本機 PawBot + MC

### 結束 MC 後

1. 退出 MC、停止本機 PawBot
2. 本機雙擊 `2-结束MC模式-推回云端.bat`（推包到雲端）
3. 雲端雙擊 `1-pull-cloud.bat`（導入）
4. 啟動雲端 PawBot

> 記住：本機「推回」只是把包放到 `D:\PawBotSync`；**雲端 PawBot 不會自動導入**，所以第 3 步不能省。

## 4. 如果不想開 OpenSSH（改用 SMB 共享）

兩台都是 Windows，也可以完全不裝 SSH：

1. 雲伺服器把 `D:\PawBotSync` 設為共享（右鍵 → 內容 → 共用），共享名 `PawBotSync`。
2. 安全組只放行你的家用 IP 的 445；或走 VPN/Tailscale（**不建議把 445 對全網開放**）。
3. 本機 `scripts/scp-sync.json` 改成：

```json
"local": { "data_dir": "", "bundle_dir": "\\\\雲伺服器IP\\PawBotSync" }
```

4. 本機三個 `.bat` 改成 `--mode local`（或我幫你另出一組 SMB 按鈕）。
5. 雲端按鈕不變（local 模式）。

## 5. 安全與行為

- 同步前檢查 `data/.runtime.lock` 的 pid；PawBot 還在跑就**拒絕**。
- 導入前自動備份到 `data/backups/pre-pull-*.tgz`，保留 5 份。
- 包裡只有 `content/`；config、sessions、browser 快取都不會傳。
- 兩邊的 key、端口、資料路徑各自保留。
- 同一時間只讓一邊 PawBot 跑。

## 6. 疑難排解

| 現象 | 處理 |
| --- | --- |
| `remote.host 为空` | 填 `scripts/scp-sync.json` 的 host |
| `Permission denied (publickey)` | 公鑰沒進 `administrators_authorized_keys`，或 ACL 不對 |
| `Connection refused` | 雲伺服器 sshd 沒開、22 沒放行 |
| `拉取失败 ... No such file` | 雲端還沒有包；先跑 `2-push-cloud.bat` |
| `PawBot 还在运行` | 先停 PawBot |
| 想改同步範圍 | 目前固定 `content/`；不建議加 config（會覆蓋兩邊端口/密鑰） |
