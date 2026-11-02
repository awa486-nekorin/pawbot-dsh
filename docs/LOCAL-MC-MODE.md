# 本地 MC 模式：一鍵拉雲端 → 玩 MC → 一鍵推回雲端

> **現在最簡單的是 SCP 版**：不需要 WebDAV/同步服務，見 [SCP-SYNC.md](SCP-SYNC.md)。
> 下面這份是「內建雲端同步」流程，仍然可用，但需要先配置 sync provider。

> 適用：雲端常駐 PawBot；想玩 Minecraft 時，改在**本機**跑「PawBot + MC」，玩完再把記憶推回雲端。
> 檔案：`scripts/mc_sync.py`、根目錄三個 `.bat`、同步底座見 [SYNC-SETUP.md](SYNC-SETUP.md)。

## 1. 你要的流程（一頁）

```
平常：雲端 PawBot 在跑（auto_pull_on_start=true）
  │
  ├─ 想玩 MC：
  │   1) 在雲端面板「停止 PawBot」
  │   2) 本機雙擊 1-进入MC模式-拉取云端.bat   ← 從雲端 pull 到本機
  │   3) DSH 啟動本機 PawBot，開 Minecraft
  │
  └─ 玩完：
      4) 退出 MC；在 DSH 停止本機 PawBot
      5) 雙擊 2-结束MC模式-推回云端.bat       ← 本機 push 回雲端
      6) 雲端啟動 PawBot（auto_pull_on_start 自動拉最新）
```

重點：**同一時間只有一邊 PawBot 在跑**（同一 QQ 也不能兩邊同時登入）。腳本會在本地 PawBot 仍運行時拒絕同步，避免半寫資料。

## 2. 一次性設定（兩邊都要）

資料根外置（見 [DATA-SEPARATION.md](DATA-SEPARATION.md)），兩邊 `sync` 指向同一雲端（WebDAV / HTTP / 共享目錄）：

```json
"sync": {
  "enabled": true,
  "provider": "webdav",
  "url": "https://dav.jianguoyun.com/dav/",
  "username": "you@example.com",
  "password": "应用密码",
  "remote_dir": "pawbot-sync",
  "passphrase": "",
  "include_persona": true,
  "include_memory": true,
  "include_config": false,
  "include_stickers": false,
  "auto_pull_on_start": true,
  "mode": "manual"
}
```

- `mode=manual`：不跑實時線程，只有 push/pull 那一下同步（符合你的用法）。
- `include_config=false`：不同步 key；key 留在 DSH 憑證/環境變數。若真要同步 config，必須設 `passphrase`。
- `auto_pull_on_start=true`：雲端重開時自動拉最新；本機也可以用，但建議進 MC 前先手動 pull（更確定）。

## 3. 日常操作

### 進 MC（本機）

1. 雲端面板停止 PawBot。
2. 雙擊 **`1-进入MC模式-拉取云端.bat`**。
   - 自動檢查本機 PawBot 未運行；
   - 先把本機內容備份到 `<dataRoot>/backups/pre-pull-*.tar.gz`；
   - 從雲端 pull（人設/記憶/人物/日記…）；
   - 顯示寫入檔案數、跳過數、加密狀態。
3. 在 DSH 啟動本機 PawBot；開 MC。

### 結束 MC（本機）

1. 退出 MC；在 DSH 停止本機 PawBot（必須先停，否則腳本拒絕）。
2. 雙擊 **`2-结束MC模式-推回云端.bat`**。
   - 檢查本機 PawBot 未運行；
   - push 到雲端，顯示檔案數/大小/sha256 前綴/時間戳。
3. 雲端啟動 PawBot；`auto_pull_on_start` 會拉這份最新資料。

### 查看狀態

雙擊 **`3-查看同步状态.bat`**：provider、目標、加密、包含項、上次 push/pull、本地 PawBot 是否在跑。

## 4. 手動 / Linux 對應

```bash
cd /opt/pawbot-dsh
python3 scripts/mc_sync.py --action status --root /opt/pawbot-dsh
python3 scripts/mc_sync.py --action enter  --root /opt/pawbot-dsh   # 拉
python3 scripts/mc_sync.py --action exit   --root /opt/pawbot-dsh   # 推
```

或直接用內建 CLI（效果相同）：

```bash
cd app
python3 tools/cloud_sync.py pull
python3 tools/cloud_sync.py push
```

Linux 如果想一鍵，可做兩個 alias / systemd oneshot，或直接執行上面的命令。

## 5. 腳本的安全行為

- 本地 `data/.runtime.lock` 的 pid 仍存活 → **拒絕 enter/exit**，提示先停 PawBot。
- pull 前自動備份 `content/`＋關鍵 `state/*.json` 到 `backups/pre-pull-*`。
- 失敗回 `SyncError` 的錯誤碼（已脫敏），不會打印 password/token/passphrase。
- 不自動啟動/停止服務，不碰 MC 存檔；只同步 PawBot 資料根。
- `include_config=true` 但沒有 passphrase → 雲端同步本身會 fail-closed 拒絕。

## 6. 邊界與注意

- **不要兩邊同時跑**：同一 QQ 會互踢；兩邊同時寫，內建同步是整包上傳、後寫贏，不合并。
- 同步是「整包」：本機 `sessions.json` 等高頻檔也會打包上傳；嫌大可把 `include_memory` 設 false（只同步人設）或容忍。
- 雲端只保留最新一份包；pull 前的本機備份、`backup-sync-*`、`sync-conflict-*` 都在資料根下，可人工還原。
- 「即時」不是毫秒級：手動模式只在點擊時同步；你要的「進 MC 前拉、結束推回」完全夠用。
- 實測（合成資料）：雲端 push → 本機 pull 後人設/記憶一致；本機改完 push → 雲端拉回一致；本機 PawBot 運行中會拒絕同步。

## 7. 可選：更自動一點

- 雲端 PawBot 可在啟動時自動 pull（已預設建議 `auto_pull_on_start=true`）。
- 本機也可開 auto_pull，但「進 MC 前手動 pull」更可控，避免啟動順序不确定。
- 想要兩邊同時常駐、自動接管，才需要改用 HA relay（見 SYNC-SETUP.md 方案 B）。
