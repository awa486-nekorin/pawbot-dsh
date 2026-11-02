# 兩邊實時同步人設 / 記憶（Windows ↔ Linux）

> 結論：**PawBot 內建的是「近實時、整包同步」，不是多寫入者合併。**
> 兩邊同時接 QQ、同時寫記憶，會變成「後寫贏」，輸的一方改動可能被整包覆蓋（本機已實測，見 §5）。
> 所以要嘛**只有一邊在寫**（另一邊待命），要嘛用專案的 **HA relay**（lease 保證只有一個 active）。
> 本文給兩種做法，先看 §0 選型。

## 0. 選型：你要哪一種？

| 目標 | 做法 | 難度 | 實時程度 |
| --- | --- | --- | --- |
| 本機平時跑，伺服器待命；切換時資料已是最新 | **近實時同步（內建）**：兩邊 `sync.mode=realtime`，但只有一邊接 QQ、另一邊停機或至少不寫入 | ★☆☆ | 本地 2s 偵測 + 3s 防抖；遠端 30s 輪詢（可調） |
| 兩邊都常駐、要自動接管（同一 QQ） | **HA relay**：伺服器中繼 + lease，只有 active 寫 content，standby 只拉 | ★★★ | 內容每 3s 複製；lease 秒級切換 |
| 只是想要備份 / 搬家 | 手動 `push` / `pull` | ★☆☆ | 不是實時 |

**不要**把 Syncthing / rsync 直接架在 `app/data` 上當「兩邊實時」：兩個 PawBot 同時寫同一批檔案，最後只會得到互相覆蓋與半寫檔。若真的要用外部檔案同步，也必須保證**只有一邊在跑**。

## 1. 前置條件（兩邊都要）

1. 資料已外置（見 [DATA-SEPARATION.md](DATA-SEPARATION.md)）：`app/data` 只是聯接，真正資料在 `D:\PawBotData` / `/opt/pawbot-data`。
2. 兩邊的 **PawBot 版本一致**（至少 `sync` 段與 bundle 格式相容；升級時先同步版本）。
3. 選一個共享端點（三選一）：
   - **WebDAV**（最省事）：堅果雲 / Nextcloud / 群暉；用「應用密碼」，不要用主密碼。
   - **HTTP 自建端點**：伺服器上跑一個只有 POST/GET 的端點 + Bearer token（見 CLOUD-SYNC.md §2.3）。
   - **local**：兩邊都掛載同一個網路目錄（例如 Windows 掛載伺服器 SMB，或反過來）。
4. `sync.passphrase`：**只要同步 config（含 key）就必須設**，否則 push 會 fail-closed 拒絕；建議兩邊用同一個長口令。
5. 人設/記憶不要放 key：`content/` 只放 persona/memory/people/diary；key 用 DSH 憑證或環境變數。

## 2. 方案 A：近實時同步（推薦，最簡單）

### 2.1 兩邊都填同一組 `sync`（`app/data/state/config.json`）

```json
"sync": {
  "enabled": true,
  "provider": "webdav",
  "url": "https://dav.jianguoyun.com/dav/",
  "username": "you@example.com",
  "password": "应用密码",
  "remote_dir": "pawbot-sync",
  "passphrase": "同一个长口令（含 config 时必需）",
  "include_persona": true,
  "include_memory": true,
  "include_config": false,
  "include_stickers": false,
  "overwrite_config": false,
  "auto_pull_on_start": true,
  "mode": "realtime",
  "poll_sec": 2,
  "debounce_sec": 3,
  "remote_poll_sec": 30,
  "conflict_policy": "keep_both",
  "first_run": "keep_both"
}
```

- **含 key 的 config**：把 `include_config` 改 `true` 並確保 `passphrase` 已設；否則保持 `false`（key 留在本機/DSH 憑證）。
- 想更快：`remote_poll_sec` 改 5–10；想少上傳：`include_memory` 關掉（只同步人設）。
- 兩個 adapter 的 `url` 寫法不同：WebDAV 用目錄 URL；HTTP 用完整端點（可含 `{name}` 模板）；local 用絕對路徑。

### 2.2 命令列（跨平台）

用包內 `app/tools/cloud_sync.py`（伺服器包已含）：

```bash
cd app
python tools/cloud_sync.py status      # 看是否啟用、provider、上次同步
python tools/cloud_sync.py test        # 真連一次，測認證/寫入
python tools/cloud_sync.py push --dry  # 只打包，不上傳
python tools/cloud_sync.py push        # 第一次：把當前人設/記憶推上去
python tools/cloud_sync.py pull --dry  # 另一邊：看會改什麼
python tools/cloud_sync.py pull        # 另一邊：拉下來
python tools/cloud_sync.py watch --interval 2 --remote-interval 30 --debounce 3
                                       # 前台觀察近實時行為（除錯用）
```

### 2.3 首次同步順序（避免 first-run 衝突）

1. **當前在跑的一邊（Windows）**：`test` → `push`。
2. **另一邊（伺服器）**：先確認 `app/data` 已有正確資料根（external）；`pull --dry` 看 diff → `pull`。
3. 兩邊都改成 `mode=realtime`、`enabled=true`，重啟 PawBot。
4. 在 Windows 改一句人設 → 30 秒內伺服器 `content/persona.md` 應更新（看日誌「已拉取」）。

### 2.4 切換主機（單寫入者）

1. 停掉目前 active 的那一邊 PawBot；
2. 等最後一次 push（或在該邊手動 `push` 一次）；
3. 啟動另一邊；`auto_pull_on_start=true` 會先拉一次最新；
4. 確認 `status` 的 `last_pull` 是剛剛、`persona.md` 內容正確，再讓它接 QQ。

> 兩邊都接同一個 QQ 號會互踢／重複回覆；同一時間只能有一個 active。

### 2.5 衝突與限制（務必看）

- 同步單位是**整個 bundle**（persona/memory/選配 config），不是逐檔合併。
- 兩邊都改 → 預設 `keep_both` 會把遠端包放進 `<dataRoot>/state/sync-conflict-<時間>/`，本機不動；但有時（兩邊都先 push 再拉）會表現為**後寫贏**——本機實測到了，所以**不要兩邊同時寫**。
- `sessions.json`、`relations.json` 等高頻檔會讓整包頻繁上傳（每 3 秒左右）；在意流量就 `include_memory=false` 或關 realtime。
- 遠端只保留**最新一份**包；要歷史版本請用支援版本化的 http 端點或自己備份。
- 還原：`<dataRoot>/backup-sync-*` 是 pull 前的備份；`sync-conflict-*` 是衝突包。

## 3. 方案 B：HA relay（兩邊都常駐、自動接管）

專案另有 `server.mode=relay` 的高可用模式（Python：`ha_runtime.py` / `ha_content.py` / `relay_client.py`；中繼：Node `onebot-relay.mjs`）：

- NapCat 只連中繼；兩台 PawBot 都以 backend 連中繼；中繼用 **lease** 保證同一時間只有一個 active。
- active 把 `content/`（人設/記憶/人物/日記）以快照 push 到中繼；standby 只 pull，不寫。
- 切換：lease 過期/釋放後，另一台接管並先拉 canonical 內容；`state/`（會話、提醒、額度）**不隨內容同步**。
- 需要：伺服器跑中繼（`scripts/onebot-relay.mjs`，目前**不在**這個遷移包內）、兩邊 `server.mode=relay`、`relay_content_url`/token、`relay_priority` 等；協議見專案 `docs/HA-CONTENT-PROTOCOL.md`。
- 這是「真正的兩邊常駐」方案，但部署與驗收都比方案 A 重；**目前未在你們環境實測**。

## 4. 安全邊界

- `passphrase` 丟了無法解包；WebDAV 用應用密碼；HTTP 端點走 HTTPS。
- 日誌/CLI 不打印 password/token/passphrase；`status` 只回 `*_set` 布林。
- 不要把 `D:\PawBotData` 放進公開 repo；不要用公共網盤分享連結。
- 同步端點若被入侵 = 人設/記憶/（若含 config）key 洩漏；只用自己的伺服器/可信 WebDAV。

## 5. 本機實測（2026-11-02）

用兩個臨時資料根 + `provider=local` 跑 `sync.mode=realtime`：

1. A 首次 push → 共享目錄出現 `pawbot-sync.tar.gz` + manifest；
2. B 首次 `pull` → B 的 persona/memory 變成 A 的內容；
3. A 改 persona → 約 1–3 秒後 B 自動拉成 A 的新內容；
4. **A、B 同時改**：兩邊都 push，最後 B 被拉成 A 的版本，**沒有**產生 `sync-conflict-*`（後寫贏）——所以「同時寫」不安全。

> 實測用合成資料，未連真實 QQ/雲端；WebDAV/HTTP 認證與公網延遲未實測。

## 6. 一頁檢查表

- [ ] 兩邊資料根都已外置，`app/data` 是聯接
- [ ] 兩邊 `sync` 指向同一個 `remote_dir` / 同一個 passphrase
- [ ] `cloud_sync.py test` 兩邊都成功
- [ ] 第一次：A `push`，B `pull`；兩邊 persona 內容一致
- [ ] `mode=realtime`、`auto_pull_on_start=true`
- [ ] 同一時間只有一邊接 QQ / 只有一邊在寫
- [ ] 切換主機有照 §2.4 做，`last_pull` 是剛剛
- [ ] 知道衝突包/備份目錄在哪，必要時人工確認
