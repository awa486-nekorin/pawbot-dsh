# 資料分離與安全更新（Key / API / 記憶 / 人設）

> 適用版本：`@local/pawbot` 1.0.0（本機正在運行的部署）
> 產出：`docs/DATA-SEPARATION.md`、`scripts/data_separation.py`、`scripts/update_code_safe.py`
> 原則：**程式可整包替換，資料永不在程式目錄內；密鑰不進程式目錄、不進人設/記憶檔。**

## 1. 結論先講

把資料分成三層，更新只碰第一層：

| 層 | 位置（本機建議） | 內容 | 更新時 |
| --- | --- | --- | --- |
| 程式碼 | `D:\WorkBuddy\pawbot-dsh` | DSH 插件宿主 + `app/`（run.py、pawbot、plugins、web） | **可整包替換** |
| 資料 | `D:\PawBotData` | `content/`（人設、記憶、人物、日記、愛好）、`state/`（設定、會話、情緒、主動發言狀態）、`backups/` | **不動** |
| 密鑰 | 系統/DSH 憑證（+ 可選 `D:\PawBotSecrets`） | 模型 key、B 站 SESSDATA、判官/決策 key | **不動** |

`app/data` 會由資料守衛自動換成指向 `D:\PawBotData` 的目錄聯接（junction），所以 Python 與宿主都只認 `app/data`，但實際檔案在程式目錄外。**升級/重裝插件只覆蓋程式碼，聯接與外部資料不受影響。**

## 2. 現狀審計（2026-11-02）

- 目前資料根：`D:\WorkBuddy\pawbot-dsh\app\data`（來源 `default`，`external=false`）→ **在程式目錄內，升級有被覆蓋的風險**。
- `app/data/state/config.json` 中 **非空密鑰欄位 = 0**；目前模型走 `llm.provider=dsh`，憑證由 DSH 保管在 `~\.dsh\.credentials.yaml`。
- 人設/記憶在 `app/data/content/`，設定在 `app/data/state/`，兩者已分檔；但整個 `app/data` 仍在程式樹內。
- DSH 插件已內建安全遷移：`lib/data-root.js` → `lib/data-guard.js` 的 `resolveDataDir()`，在 `Config.dataDir` 或 `PAWBOT_DATA_DIR` 指向外部時，自動「複製 → 把 `app/data` 改名留檔 → 建立目錄聯接」，失敗則回退 `app/data`，不刪原資料；版本變化時自動快照 config/persona/記憶到 `<dataRoot>/backups/`。

## 3. 目標目錄

```text
D:\
├─ WorkBuddy\pawbot-dsh\             ← 程式（可整包替換、可重裝）
│  ├─ index.js / lib / bili-watch …
│  └─ app\
│     ├─ run.py / pawbot / plugins / web   ← 更新時替換
│     └─ data  →  D:\PawBotData              ← 目錄聯接（自動建立）
│
├─ PawBotData\                       ← 資料（升級絕不覆蓋）
│  ├─ content\
│  │  ├─ persona.md                  ← 人設
│  │  ├─ memory\ (facts/impression/slang.md)
│  │  ├─ people\  diary\  hobbies.json  relations.json
│  │  └─ stickers\  album\
│  ├─ state\
│  │  ├─ config.json                 ← 行為設定（密鑰建議留空）
│  │  ├─ sessions.json / emotion.json / mood.json / topic.json
│  │  └─ proactive_state.json / plugin_state.json …
│  ├─ backups\                        ← 版本/手動/升級前快照
│  └─ secrets\                        ← 可選：獨立密鑰檔（權限 600）
│
└─ PawBotSecrets\（可選，更嚴格）    ← 與資料分開的密鑰目錄
   ├─ bili-credential.json            ← tools.bili_watch_credential_file
   ├─ decider.key                     ← PAWBOT_DECIDER_KEY_FILE
   └─ pawbot.env（僅 systemd/Windows 使用者環境引用，不進 repo）
```

## 4. 資料根優先級與自動遷移

優先級（兩端一致）：

1. DSH 插件 `@local/pawbot` 的 `dataDir`（最高）
2. 環境變數 `PAWBOT_DATA_DIR`
3. `<app>/data`（預設，不建議長期使用）

遷移由 DSH 插件在載入時自動執行（`lib/data-guard.js:migrateToExternal`）：

1. 比較 `app/data` 與 `dataDir` 的實際路徑（含 junction/大小寫）→ 防循環/自我複製；
2. 預約 `<app>/data/.runtime.lock`，避免與正在運行的 Python 互相踩；
3. 複製 `app/data` → `dataDir`（不覆蓋已有檔案）；
4. 把 `app/data` 改名為 `app/data.migrated-<時間>` 留檔；
5. 建立 `app/data → dataDir` 目錄聯接；失敗則整體回退，**不刪原資料**；
6. 之後 Python `resolve_data_root` 跟隨聯接，兩端看到同一個根。

> 因此 **不要在運行中手動搬 `app/data`**；設定好 `dataDir` 後讓插件在下次啟動時自己遷移。

## 5. 密鑰邊界（Key / API）

| 密鑰 | 建議位置 | 目前狀態 |
| --- | --- | --- |
| 主模型（`llm.provider=dsh`） | DSH 憑證 `~/.dsh/.credentials.yaml` | 已在用，**不在 PawBot 資料內** |
| 主模型（直連 API） | 環境變數或 `state/config.json`（external）；不要放程式目錄 | 目前未用 |
| 判官 `PAWBOT_JUDGE_API_KEY` | 環境變數（優先於 config） | 可選 |
| 決策模型 `PAWBOT_DECIDER_KEY` / `_FILE` | 環境變數或密鑰檔 | 可選 |
| B 站 SESSDATA | `BILI_SESSDATA` 或 `tools.bili_watch_credential_file` 指到 `D:\PawBotSecrets\bili-credential.json` | 可選 |
| 其它直連 key（搜尋/識圖/TTS/STT） | `state/config.json`（external）或環境變數；**絕不寫入人設/記憶檔** | 目前為空 |

規則：

1. `content/`（人設/記憶）**永不放密鑰**；`state/config.json` 可放非敏感設定，敏感欄位建議留空並用環境變數/憑證檔。
2. 密鑰檔權限：Windows 只用目前使用者可讀（`icacls` 移除繼承）；Linux `chmod 600`。
3. 不把 `D:\PawBotData`、`D:\PawBotSecrets` 放進任何 git repo；`.gitignore` 已排除 `app/data`，外部目錄天然不在 repo 內。

## 6. 一次性啟用（維護窗口）

### 6.1 準備

```powershell
# 1) 看現狀與計畫（唯讀，不改任何東西）
py -3.13 scripts\data_separation.py --mode plan

# 2) 可選：先做資料守衛快照（不改資料）
#    DSH 工具：pawbot_data_backup action=backup
```

### 6.2 設定外部資料根（寫入插件設定）

```powershell
# 寫入 DSH profile 的 @local/pawbot.dataDir，並備份原設定
py -3.13 scripts\data_separation.py --mode apply --data-dir D:\PawBotData
```

- 會修改 `C:\Users\<你>\.dsh\profiles\<profile>\cordis.patch.yml`，先備份為 `.bak-datasep-<時間>`。
- **此時服務仍在跑舊資料；不要手動搬 `app/data`。**
- 關閉 DSH（或重啟 DSH / 重啟電腦），插件載入時自動遷移並建立聯接。

### 6.3 遷移後驗證

```powershell
py -3.13 scripts\data_separation.py --mode verify
```

驗證項：`dataDir` 已生效、`app/data` 是聯接且指向外部、`content/persona.md` 與 `state/config.json` 存在、密鑰欄位為 0、備份目錄存在；輸出 config/persona 的 SHA-256 供前後比對。

### 6.4 回滾

1. 停 DSH/PawBot；
2. 刪除 `app\data` 聯接（只刪聯接，不刪目標）；
3. 把 `app\data.migrated-<時間>` 改回 `app\data`；
4. 用 `.bak-datasep-<時間>` 還原 `cordis.patch.yml`；
5. 重啟 DSH。

## 7. 日常升級流程（不覆蓋資料）

```powershell
# 1) 確認資料已外置（必須通過才升級）
py -3.13 scripts\data_separation.py --mode verify

# 2) 升級程式（預設 dry-run，看清楚再做）
py -3.13 scripts\update_code_safe.py --package dist\pawbot-server-no-mc-*.tar.gz
py -3.13 scripts\update_code_safe.py --package dist\pawbot-server-no-mc-*.tar.gz --apply

# 3) 重啟 DSH（或讓插件重載），驗證
py -3.13 scripts\data_separation.py --mode verify
```

升級腳本保證：

- 只覆蓋程式檔案與 `docs/scripts`；**永不刪除/覆蓋 `app/data`、`.venv`、`node_modules`、`.git`**；
- 升級前把 `content/` + `state/config.json` + 關鍵 state 檔快照到 `<dataRoot>/backups/pre-update-<時間>.tar.gz`；
- 升級前後比對 `persona.md`、`config.json` 的 SHA-256，必須一致；
- 升級後再次確認 `app/data` 聯接目標未變。

## 8. Linux 雲伺服器對應

```text
/opt/pawbot-dsh/          程式（可替換）
/opt/pawbot-data/         資料（dataDir 指向這裡）
/opt/pawbot-secrets/      可選密鑰
```

- DSH 插件設定：`dataDir=/opt/pawbot-data`（或 systemd `PAWBOT_DATA_DIR`）。
- 首次啟動時同樣自動遷移：`app/data → /opt/pawbot-data`，並建立符號聯接。
- 權限：`chmod 700 /opt/pawbot-data /opt/pawbot-secrets`；`chmod 600` config/persona/密鑰。
- 更新：解壓新包到暫存 → 同 §7 的更新腳本（Python 跨平台）→ 重啟 DSH。
- 注意：`/opt/pawbot-data` 不要放在 `/opt/pawbot-dsh` 裡面（會被判定祖先/後代而拒絕遷移）。

## 9. 驗證與限制

已在本機驗證（唯讀 + 沙盒）：

- `plan`/`verify` 對目前部署輸出正確（external=false、密鑰 0）；
- 沙盒模擬 `dataDir` 外部目錄：`resolveDataDir()` 自動複製 → 改名 → 建立聯接，資料與原檔一致（見命令輸出）；
- 對插件 profile 的 `dataDir` 寫入在**暫存副本**上演練，未改正在運行的 DSH 設定。

未做（需維護窗口）：

- 真正重啟 DSH 讓本機完成遷移（會短暫斷線）；
- 在 Linux 上實跑首次遷移與升級腳本；
- 升級腳本對「跨版本插件 + 正在運行的 bot」的實機演練。

## 10. 一頁檢查表

- [ ] `data_separation.py --mode verify` 顯示 `update_safe=true`
- [ ] `D:\PawBotData\content\persona.md`、`memory\`、`state\config.json` 存在
- [ ] `app\data` 是聯接，指向 `D:\PawBotData`
- [ ] `state\config.json` 非空密鑰欄位 = 0（或密鑰在環境/DSH 憑證）
- [ ] 人設/記憶目錄不包含任何 key/API 字樣
- [ ] 升級前後 persona/config SHA-256 一致
- [ ] 備份目錄有 `plugin-update-*` 或 `pre-update-*`
- [ ] 程式目錄內沒有真實資料（僅聯接）
