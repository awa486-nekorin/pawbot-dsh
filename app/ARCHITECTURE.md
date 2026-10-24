# PawBot 架構說明

這份文檔給改代碼的人看：哪個功能在哪個檔案、提示詞在哪裡、改錯了會牽動什麼。不是用戶使用指南（那份是 `使用指南.md`）。

> 寫這份文檔時順手發現幾個現狀問題，已經標成「⚠️」，改代碼前先看完。

---

## 0. 一張圖看懂消息怎麼走

```
QQ 群消息
  │
  ▼
NapCat（第三方，不在這個倉庫裡）
  │ OneBot v11 反向 WebSocket
  ▼
pawbot/wsproto.py      —— 手寫 WebSocket 幀，收發原始 frame
pawbot/onebot.py       —— 連線管理、事件入隊、call() 等 API 回執
  │
  ▼
pawbot/event.py        —— JSON → Event 物件（MessageEvent/NoticeEvent/RequestEvent）
pawbot/message.py      —— 消息段 ↔ CQ 碼 ↔ 純文字
  │
  ▼
pawbot/core.py  Bot._on_event()   —— 總入口：過濾、記錄、分發
  │
  ▼
pawbot/perceive.py     —— 把圖片/語音/視頻/引用/轉發「看懂」，補到 event 上
  │
  ▼
plugins/ai_chat.py     —— 攢話、批量回覆節奏控制
  │
  ▼
pawbot/agent.py  Agent.run()      —— 組 system prompt + 上下文，調模型，跑工具循環
  │          （這裡會用到 pawbot/tools.py 裡所有工具）
  ▼
pawbot/style.py        —— 清洗文字（去廢話、去 emoji、分條標記）
  │
  ▼
pawbot/safety.py       —— 打字延遲、限速、分條發送、媒體去重
  │
  ▼
NapCat → QQ 群
```

---

## 1. 完整目錄結構

```
app/                       ← PawBot 項目根
├── run.py                  啟動入口
├── requirements.txt         第三方依賴（Pillow、websockets，其餘是標準庫）
├── README.upstream.md / 使用指南.md   使用者文檔（不是本檔）
│
├── pawbot/                  核心套件（66 個 .py）
├── plugins/                 功能插件（12 個 .py，啟動時自動掃描加載）
├── web/                     WebUI 前端（單檔 index.html；按 DSH 插件面板使用，本倉庫未附帶）
├── tools/                   測試（66 個 test_*.py）+ 離線輔助腳本（16 個 .py）
├── data/                    運行時資料（配置、人設、記憶模板；真實資料不入庫）
└── （deploy/ 部署腳本隨上游專案分發，本倉庫未附帶）
```

### `pawbot/` 套件內所有檔案

| 分類 | 檔案 | 作用 |
|---|---|---|
| **協議層** | `onebot.py` | OneBot 反向 WebSocket 伺服器、連線管理、API 調用（`conn.call()`） |
| | `wsproto.py` | 手寫 RFC6455 WebSocket 幀編解碼 |
| | `event.py` | 原始 OneBot JSON → `Event` 物件 |
| | `message.py` | 消息段構造（`Text/At/Image/Record`）、CQ 碼轉換、`to_plain()` |
| **核心調度** | `core.py` | `Bot` 類：初始化所有模組、事件總入口 `_on_event()` |
| | `context.py` | 插件用的 `Context`（`ctx.reply()`/`ctx.send()`） |
| | `bus.py` | 事件處理器註冊與分發 |
| | `plugin.py` | 掃描 `plugins/` 目錄、熱重載、開關插件 |
| | `tasks.py` | 背景 asyncio task 管理、停機時 drain |
| **配置與模型** | `config.py` | `DEFAULT` 配置結構（見第 9 節）、讀寫 `data/config.json` |
| | `llm.py` | OpenAI 相容的模型調用客戶端，含工具調用解析、mock 模式 |
| | `judge.py` | 便宜的「二次判斷」模型通道（給日記分類、畫質評判用） |
| | `breaker.py` | 模型熔斷器：連續失敗後暫停調用一段時間 |
| | `log.py` | 日誌（終端 + 檔案 + 環形緩衝 + SSE 推送給 WebUI） |
| | `webui.py` | WebUI 的 HTTP API（見第 8 節） |
| **人設與對話** | `agent.py` | **核心**：組裝提示詞、跑多輪工具調用（見第 3 節） |
| | `persona.py` | 讀 `data/persona.md` + `data/memory/`，組 system prompt |
| | `memory.py` | Markdown 記憶讀寫（facts/slang/impression） |
| | `store.py` | 按會話存儲的聊天記錄（`data/sessions.json`） |
| | `ctxwin.py` | 把聊天記錄按時間衰減壓縮成給模型看的文字 |
| | `turns.py` | 每輪結束後滾動生成小結，沉澱進長期記憶 |
| | `longmem.py` | 跨會話的「經歷」記憶（`data/longmem/episodes.jsonl`） |
| | `people.py` | 群友個人檔案、好感度（`data/people/<QQ>.md`） |
| | `relations.py` | 群友互動關係、群管理資訊（`data/relations.json`） |
| | `topic.py` | 話題追蹤、群活躍節奏、Mood 視圖 |
| | `diary.py` | 按群聊經歷生成每日日記 |
| | `memes.py` | 梗庫（關鍵詞觸發、注入） |
| **感知與發送** | `perceive.py` | **核心**：引用/圖片/語音/視頻/轉發/卡片的理解邏輯 |
| | `vision.py` | 圖片下載、縮放、拼宮格、調視覺模型描述 |
| | `video.py` | 用 ffmpeg 給視頻抽幀 |
| | `speech.py` | QQ 語音轉寫 |
| | `safety.py` | **核心**：打字延遲、分條發送、限速、媒體去重發送 |
| | `style.py` | **核心**：發送前文字清洗（見第 6 節） |
| | `emotion.py` | 八維情緒矩陣 |
| | `libido.py` | 獨立的性欲值 |
| | `spice.py` | 每群的尺度檔位（1 收斂 / 2 愛色色） |
| | `stickers.py` | 表情包索引、挑選、收藏 |
| | `animecheck.py` | 表情包畫風判定（是否二次元） |
| | `media.py` | `[IMG:]`/`[STICKER:]` 標記解析、媒體去重帳本 |
| **工具與外部能力** | `tools.py` | **核心**：所有 function calling 工具的 schema 和實現（見第 5 節） |
| | `imagesearch.py` | 搜圖整合入口、畫質評判、成人內容打碼 |
| | `pixiv_session.py` | 專用 Chrome 操作 Pixiv 登入態 |
| | `websearch.py` | DeepSeek 搜尋/抓取、X 搜尋 |
| | `moegirl.py` | 萌娘百科查詢 |
| | `bilibili.py` | B 站搜尋、排行榜 |
| | `doutu.py` | 鬥圖網表情包下載 |
| | `tts.py` | 語音合成（雲端/本地雙通道，見第 5 節） |
| **生活與QQ動作** | `life.py` | 日期、時段、作息、節日 |
| | `groupinfo.py` | QQ 群資訊快取（群名、成員數、管理員） |
| | `hobbies.py` | 個人愛好、定期更新 |
| | `proactive.py` | 冷場時主動發話 |
| | `reminders.py` | 定時提醒 |
| | `qzone.py` | 發 QQ 空間 |
| | `account.py` | 主人授權的帳號資料修改 |

### `plugins/` 下所有插件

每個檔案用裝飾器（`@on_message`/`@on_command` 等）把 handler 註冊到全域 `BUS`，`plugin.py` 啟動時自動掃描加載。

| 檔案 | 作用 |
|---|---|
| `ai_chat.py` | **核心**：旁聽群聊、攢話批量回覆、調用 `Agent.run()` |
| `account.py` | 主人用自然語句改帳號資料 |
| `basic.py` | `/ping`、`/help` 等基礎指令 |
| `group_events.py` | 群撤回/禁言/管理變更/退群/精華/加群請求 |
| `group_tools.py` | 新成員歡迎、早安晚安關鍵詞 |
| `hobbies.py` | 排程日記補寫、愛好檢查 |
| `life_events.py` | 刷新群資訊、記生日、送祝福 |
| `pixiv.py` | Pixiv 登入/狀態查詢指令 |
| `qq_actions.py` | 戳一戳、撤回、群互動動作 |
| `random_interject.py` | 按機率隨機插話 |
| `relations.py` | 記錄回覆/@/戳一戳關係 |
| `spice.py` | `/荤` 尺度檔位指令 |

### `data/` 下的關鍵檔案

| 檔案/目錄 | 作用 |
|---|---|
| `config.json` | **實際生效**的配置（區別於 `config.py` 的預設值） |
| `persona.md` | 人設正文，改了下一句話就生效，不用重啟 |
| `sessions.json` | 所有會話的聊天記錄、摘要 |
| `memory/facts.md` | 長期事實記憶 |
| `memory/impression.md` | 對群友的印象 |
| `memory/slang.md` | 網路用詞理解 |
| `longmem/episodes.jsonl` | 跨會話「經歷」記憶 |
| `people/<QQ号>.md` | 每個群友的個人檔案 |
| `diary/<日期>.md` | 每日日記 |
| `stickers/` | 表情包庫 + `index.json` 索引 |
| `models.json` | WebUI「模型調控」面板的預設清單 |
| `spice.json` | 每群的尺度檔位 |
| `emotion.json` / `libido.json` / `mood.json` | 情緒/性欲/心情狀態 |
| `relations.json` | 群友關係網 |
| `*.before-*` / `*.backup-*` | 各種操作前的自動備份，可以放心刪 |

### `tools/` 下的非測試輔助腳本

| 腳本 | 作用 |
|---|---|
| `classify_stickers.py` | 批量判斷表情包畫風，非二次元可移出 |
| `refill_stickers.py` | 搜圖+判畫風+情緒，自動補充表情包庫 |
| `fetch_stickers.py` | 按標籤批量抓表情包 |
| `strict_anime_pass.py` | 對已入庫的表情包做更嚴格複檢 |
| `make_stickers.py` | 生成占位表情包（已不在啟動流程裡用） |
| `seed_memes.py` | 預置梗庫 |
| `research_slang.py` | 批量研究網路用詞寫入 `slang.md` |
| `selftest.py` | 用假模型跑一遍 persona/memory/sticker/工具循環，不連 QQ |
| `mock_qq.py` | 模擬 NapCat，本地測試用 |
| `mock_send.py` | 用 mock 連線發一條測試消息 |
| `chat_demo.py` | 依序跑多輪示例對話 |
| `bind_pixiv.py` | 啟動專用 Chrome 綁定 Pixiv |

### `deploy/` 部署相關（上游專案附帶，本倉庫未包含）

| 檔案 | 作用 |
|---|---|
| `pack.sh` | 打包成可上傳雲端的 `tar.gz`，排除緩存/備份/日誌 |
| `pawbot.service` | systemd 服務配置（Linux 雲主機開機自啟用） |

---

## 2. 啟動流程

1. `run.py` 把項目根目錄加進 `sys.path`，解析 `--port`/`--webui`/`--check` 參數
2. 建 `Bot(config_path)` 實例 → `Bot.__init__()`（`pawbot/core.py:65`）按順序初始化：
   ```
   Config → Store → LLMClient → SafetyGuard
     → Persona / Memory / MemeBook / LongMemory / Diary / Hobbies
     → ReminderStore+Service / PersonBook / Spice / StickerBook
     → Qzone / AccountActions / ToolBox / Agent
     → TopicTracker / Mood / Emotion / Libido / Proactive
     → PluginManager / OneBotServer
   ```
3. `await bot.start()`：加載插件 → 背景確保 TTS 服務可用 → 啟動 Store 定期落盤 → 啟動 OneBot WebSocket 伺服器監聽 `0.0.0.0:6199` → 啟動主動發話和提醒服務
4. `await webui.start()`：啟動 `127.0.0.1:6185` 的 HTTP 後台
5. 等待 Ctrl+C，收到後依序停止提醒服務、主動服務、drain 背景任務、落盤所有狀態、關閉伺服器

事件總入口是 `Bot._on_event()`（`pawbot/core.py:197`）：解析事件 → 跳過自己發的消息 → 記錄 inbound 計數 → 過白名單/黑名單 → 解析命令和喚醒詞 → 群消息先進 `TopicTracker` → 全量寫入 `Store` → 需要時更新情緒 → 對要回覆/旁聽的消息跑 `perceive.enrich()` 補全感知 → `BUS.dispatch(ctx)` 分發給插件。

---

## 3. Agent 提示詞在哪裡（最常改的地方）

`pawbot/agent.py` 的 `Agent.run()` 組裝 system prompt 時，會依序拼接這些 `_xxx_hint()` 方法的輸出。**要改「她為什麼這麼說話/這麼決策」，先看這張表找對方法：**

| 方法 | 提示詞內容 | 大概行號 |
|---|---|---|
| `_participation_hint()` | 要不要插話、`<pass>` 沉默規則、被點名必須回應 | `agent.py:248` |
| `_batch_hint()` | 攢話數、回覆對象、句數目標、是否帶問句 | `agent.py:270` |
| `_work_feedback_hint()` | 主人提工作意見時要認真回應，不嬉皮笑臉 | `agent.py:339` |
| `_sense_hint()` | 強制語音、引用內容、圖片/視頻/轉發描述、人物關係 | `agent.py:352` |
| `_life_hint()` | 日期、時間、作息、群資訊 | `agent.py:443` |
| `_mood_hint()` | 情緒/心情提示（不直接說情緒名字） | `agent.py:459` |
| `_spice_hint()` | 尺度檔位、性欲值語氣提示 | `agent.py:467` |
| `_tool_hint()` | 工具使用原則、搜尋/查梗/表情包/找圖規則 | `agent.py:490` |

這些只是**補充提示**，大的性格設定在 `data/persona.md`（見第 4 節）。

工具循環邏輯在 `Agent.run()` 本體（`agent.py:38`）：按 `tools.max_rounds` 和時間預算跑模型調用，防止重複相同工具參數、搜索次數過多、對話被新消息打斷——這幾條邏輯都在這個方法裡。

---

## 4. 人設文件 `data/persona.md` 結構

這是**改性格最直接的地方**，改完下一句話就生效，不用重啟。目前分這幾段（用 `##` 標題分隔）：

1. 開頭段：角色設定（成年、DeepSeek 鯨魚娘、外貌）
2. 「你的脾氣」：性格基調（溫順、心軟、好哄、帶點傲嬌）
3. 「在群裡怎麼開口」：說話方式規則（句數、語氣詞、不用 emoji、不寫動作旁白等一大堆結構性規則）
4. 「語C」：角色扮演寫法和尺度
5. 「不要這樣說 → 可以這樣說」：口氣對照表
6. 「做事與記憶」：工具使用態度
7. 「群裡的尺度與邊界」：色色內容的分寸、找圖分級、未成年守護句

**⚠️ 第 19 行左右有一條守護句**（「萌系那一頭只停在可愛畫風…色色、擦邊、R18 的話題和圖只往成年角色那邊走」），改人設時千萬不要動這一句，也別讓新加的內容和它衝突。

---

## 5. 工具（Function Calling）清單

`pawbot/tools.py` 的 `ToolBox.execute()` 把工具名映射到 `_t_<工具名>` 方法。`available()` 方法決定哪些工具當前可用（受配置開關控制）。

| 工具名 | handler | 作用 |
|---|---|---|
| `web_search` | `_t_web_search` | 搜網頁 |
| `fetch_url` | `_t_fetch_url` | 讀網頁正文 |
| `x_search` / `x_read` | `_t_x_search` / `_t_x_read` | 搜/讀 X（推特） |
| `moegirl` | `_t_moegirl` | 查萌娘百科 |
| `learn_meme` | `_t_learn_meme` | 記錄新梗 |
| `recall_memory` | `_t_recall_memory` | 檢索長期記憶 |
| `remember` | `_t_remember` | 寫入長期記憶 |
| `remind` | `_t_remind` | 設定時提醒 |
| `note_person` | `_t_note_person` | 記群友性格 |
| `dirty_joke` | `_t_dirty_joke` | 講葷段子 |
| `pick_sticker` | `_t_pick_sticker` | 挑/找表情包 |
| `speak` | `_t_speak` | 轉 QQ 語音 |
| `save_sticker` | `_t_save_sticker` | 收藏表情包 |
| `search_image` | `_t_search_image` | 搜圖（含畫質評判、打碼） |
| `post_qzone` | `_t_post_qzone` | 發 QQ 空間 |
| `bili_video` | `_t_bili_video` | 找 B 站視頻分享 |
| `recall_last` | `_t_recall_last` | 撤回自己上一條（僅限被要求時） |
| `account_action` | `_t_account_action` | 主人授權的帳號操作 |

**語音合成雙通道**（`pawbot/tts.py`）：
- `tts.provider = "cloud"`：走阿里雲 Voice Studio WebSocket（`cloud_voice` 填音色 ID）
- `tts.provider = "local"`：走本機 CosyVoice HTTP 服務（需要自行部署常駐服務，例如 `CosyVoice/tools/feiyu_server.py`）
- 雲端失敗會自動退回本地，本地也失敗就退回打字，絕不卡死整條回覆

---

## 6. 發送前文字清洗 `pawbot/style.py`

**這是「為什麼她說話變得奇怪/正常」要查的地方**。`polish()` 函數依序做這些事：

| 規則 | 變數名 | 作用 |
|---|---|---|
| 保護格式 | `_PROTECTED_RE` | 保護 CQ 碼、代碼塊、網址、Markdown 連結不被誤清洗 |
| 去機械開頭 | `_OPENER_RE` | 刪「行行行」「好好好」「哦，懂了」這類重複開頭 |
| 去地域腔 | `_DIALECT` / `_YO_RE` / `_ERHUA` | 中性化方言詞、去句首語氣詞、去過度兒化音 |
| 去 emoji | `_EMOJI_RE` / `_EMOJI_KEEP` | 刪彩色 emoji，保留 ♡ 等語氣符號 |
| 去動作旁白 | `_ACTION_RE` | 刪全形括號裡的神態動作描寫（2-40 字） |
| 語C 判定 | `_RP_CORE` / `_RP_EXTRA` | 判斷是不是角色扮演語氣，決定要不要走語C 專屬規則（不截句數） |
| 去自曝句 | `_META_SENTENCE_RE` | 刪「橫線當場退役」這類自我通報式句子 |
| 去追問收尾 | `_TAG_QUESTION_RE` / `_FOLLOWUP_RE` | 刪「你呢」「對吧」這類公式化追問 |
| 去粗體 | `_BOLD_RE` | 去掉 `**粗體**` 標記但保留文字 |
| 分條 | `_NEWLINE_RE` / `_LIST_ITEM_RE` | 判斷換行要不要當成分條，真正的列表不拆 |

---

## 7. 人格與尺度控制

| 模組 | 作用 | 存檔位置 |
|---|---|---|
| `spice.py` | 每群尺度檔位：1 收斂點 / 2 愛色色（**沒有素聊檔，≤0 會被強制抬到 2**） | `data/spice.json` |
| `emotion.py` | 八維情緒（開心/興奮/好奇/煩躁/低落/親昵/得意/疲憊），基線 50 | `data/emotion.json` |
| `libido.py` | 獨立性欲值，基線 20，0-100 | `data/libido.json` |
| `stickers.py` + `animecheck.py` | 表情包挑選 + 畫風門檻（只收二次元） | `data/stickers/` |

WebUI 首頁有「情緒矩陣」開關按鈕，關掉後 `emotion.enabled=false`，`Mood` 不再綁定 `Emotion`。

---

## 8. 安全與未成年防護現況 ⚠️

這部分**現狀有缺口**，改動前務必確認：

- `pawbot/imagesearch.py:1374` 的 `_MINOR_TERMS` 詞表目前**只剩「福瑞」一個詞**，不是完整的未成年詞表。這不是本次對話造成的，是外部改動遺留的。
- `_allowed()`（`imagesearch.py:1391`）只在 R18 候選上檢查 `mentions_minor()`，SFW 結果完全不檢查。
- `image_search()`（`imagesearch.py:1536`）裡「未成年+性化詞」的查詢層檢查目前是空的，註釋裡寫明原來那行代碼因為邏輯短路從未真正執行過。
- `tools.block_minor` 配置項（`config.py:178`）**沒有任何代碼讀取它**，填 true/false 都不起作用。
- `data/persona.md:19` 有一條守護句，把色色內容限制在成年角色，**這條是目前唯一還在生效的文字層防護**，改人設時不要碰它。

**如果要修復這個缺口**：需要重建 `_MINOR_TERMS` 完整詞表、把 `_allowed()` 的未成年檢查擴大到所有 rating、接上 `image_search()` 裡那段空檢查、讓 `tools.block_minor` 真正生效。這些改動建議單獨一輪做，不要和其他功能改動混在一起。

---

## 9. 配置系統 `pawbot/config.py`

`DEFAULT` 字典定義所有配置的預設值，`Config` 類用深度合併讀取 `data/config.json`（所以新增預設鍵會自動補進舊配置，不用手動遷移）。

頂層區塊分四大類：

**存儲/記憶**：`store`、`longmem`、`diary`、`memes`、`people`、`relations`、`hobbies`、`reminders`

**連線/聊天**：`server`（OneBot）、`webui`、`bot`、`chat`、`reply`、`whitelist`

**人格/輸出**：`persona`、`spice`、`emotion`、`libido`、`media`、`safety`

**模型/工具**：`llm`、`judge`、`tools`、`speech`、`video`、`tts`、`account`、`qzone`

**行為/環境**：`topic`、`life`、`proactive`、`events`、`random_interject`

區分清楚：`pawbot/config.py` 的 `DEFAULT` 是**代碼裡的預設值**，`data/config.json` 是**實際生效的配置**（會覆蓋預設值）。改配置應該優先用 WebUI 的「配置」頁或「模型調控」面板操作，它會同時更新記憶體和磁碟；手改 `data/config.json` 要先停機器人，否則運行中的記憶體值會在下次保存時把你的手改覆蓋掉。

---

## 10. WebUI `pawbot/webui.py` + `web/index.html`

9 個分頁：**概覽**、**配置**、**人設/記憶**、**群友**、**記憶**、**上下文**、**插件**、**日誌**、**收發測試**。

主要 API：

| 方法 | 路徑 | 作用 |
|---|---|---|
| GET | `/api/status` | 運行狀態 |
| GET/POST | `/api/config` | 讀/改配置 |
| GET | `/api/context?session_id=` | 她這一眼能看到的上下文原文 |
| POST | `/api/context/window` | 調上下文窗口三層上限 |
| GET/POST | `/api/models`、`/api/models/save`、`/api/models/apply` | 模型預設管理與套用 |
| GET/POST | `/api/memory`、`/api/memory/forget` | 記憶總覽與清空 |
| POST | `/api/plugins/reload`、`/api/plugins/toggle` | 插件熱重載/開關 |
| POST | `/api/session/clear` | 清空某會話 |

**⚠️ `webui.token` 目前沒有實際校驗邏輯**，`/api/file` 的路徑檢查也被移除了，能讀寫 `data/` 以外的路徑。WebUI 設計上假定只在 `127.0.0.1` 本機訪問；搬到雲端後**不要把 6185 直接暴露到公網**，要用 SSH 隧道或者先把校驗邏輯補回去。

---

## 11. 已知的技術債（看到不用驚訝）

這些是這次探索順手發現的，不是新問題，記下來是為了以後改代碼時別被嚇到：

1. `safety.py` 的 `quiet_hours`、`silence_chance`、`split_threshold`、`stop_when_replied` 配置項存在但沒有接入實際發送邏輯。
2. `onebot.py:173` 附近的 Token 校驗邏輯被整段注釋掉了，`server.access_token` 填了也不會校驗——公網部署必須先把這段放開。
3. `pawbot/memory.py` 支援讀取的 `people.md` 集中式人物卡現在已經沒有這個檔案，人物檔案改成了 `data/people/<QQ>.md` 一人一份。
4. `data/pixiv_token.json`（上游專案根目錄）目前沒有代碼引用，可能是殘留檔；本倉庫未附帶。

---

## 12. 測試

跑全量測試：
```bash
cd app
PYTHONIOENCODING=utf-8 py -m unittest discover -s tools -p "test_*.py"
```

基線是 840 個測試，3 個已知失敗（`test_image_quality.test_furry_is_excluded_for_all_ratings`、`test_media.test_local_file_containment_and_validation`、`test_stickers.test_cap_refuses_instead_of_deleting`），這三個失敗和第 11 節的技術債有關，不是新壞的。

測試檔案和模組的對應關係太多，不在這裡重複列（`tools/` 目錄下檔名基本就是「test_+模組名」，一看就懂），有疑問直接用 grep 搜模組名找對應的 test 檔。
