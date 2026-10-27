# 云端共享 / 同步（人设、记忆、配置）

PawBot 可以把「人设 + 记忆 + 配置（含 API key）」打成一个包，上传到**用户自己的**云端
服务器，再在另一台机器（或重装后）拉回来。也支持手动 `push` / `pull`。

- 代码：`app/pawbot/cloudsync.py`
- CLI：`app/tools/cloud_sync.py`
- 配置：`app/data/config.json` → `"sync"` 段（默认值在 `app/pawbot/config.py`）
- 测试：`app/tools/test_cloudsync.py`

> 一句话：**这是给自己用的备份/迁移工具，不是多端实时同步。** 没有自动 push，
> 冲突策略是「手动那一下说了算」。

---

## 1. 配置段

```json
"sync": {
  "enabled": false,
  "provider": "local",
  "url": "",
  "username": "", "password": "", "token": "",
  "remote_dir": "pawbot-sync",
  "passphrase": "",
  "include_persona": true,
  "include_memory": true,
  "include_config": true,
  "include_stickers": false,
  "overwrite_config": false,
  "auto_pull_on_start": false,
  "last_push": "", "last_pull": ""
}
```

| 字段 | 说明 |
| --- | --- |
| `enabled` | 只控制**启动时自动拉取**；手动 `push/pull` 不受它限制 |
| `provider` | `local` / `webdav` / `http` |
| `url` | `local`：本机目录；`webdav`：目录 URL；`http`：完整端点 |
| `username` / `password` | WebDAV Basic Auth；`local` / `http` 不用 |
| `token` | `http` 的 `Authorization: Bearer <token>` |
| `remote_dir` | 仅 WebDAV：`url` 下再建的子目录（自动 MKCOL） |
| `passphrase` | 非空即加密；**丢了不能恢复**（见第 4 节） |
| `include_persona` | 打包 `data/persona.md` |
| `include_memory` | 打包 `data/memory/`、`data/people/`、`data/longmem/`、`data/diary/`、`data/sessions.json`、`data/relations.json`、`data/media_ledger.json`、`data/memes.json` |
| `include_config` | 打包 `data/config.json`；**必须同时设置 `passphrase`** |
| `include_stickers` | 打包 `data/stickers/`（默认关：体积大） |
| `overwrite_config` | pull 时是否覆盖 `data/config.json`（默认 false） |
| `auto_pull_on_start` | 启动时后台拉一次（失败不影响启动） |
| `last_push` / `last_pull` | 成功后自动写回的时间戳 |

`password`、`token`、`passphrase` 是 secret：CLI、日志、函数返回值都不会打印它们的值；
`status` 只回 `password_set` / `token_set` / `passphrase_set` 这类布尔量。

---

## 2. 三种 provider

### 2.1 `local`（本机 / 挂载目录；测试、离线、群晖挂载都行）

```json
"sync": {
  "enabled": true,
  "provider": "local",
  "url": "D:/pawbot-sync",
  "include_config": true,
  "passphrase": "换成一个只有你知道的长口令"
}
```

- `url` 可以是绝对路径，也可以相对 PawBot 根目录（如 `"data/sync-remote"`）。
- 上传是原子写：先写 `*.tmp-<pid>-<线程>`，再 `os.replace`，断电不会留下半个包。
- `test` 会真的建目录、写一个探针文件再删掉，确认可读写。

### 2.2 `webdav`（坚果云 / Nextcloud / 群晖）

坚果云：

```json
"sync": {
  "enabled": true,
  "provider": "webdav",
  "url": "https://dav.jianguoyun.com/dav/",
  "username": "you@example.com",
  "password": "坚果云 → 账户信息 → 应用密码（不是登录密码）",
  "remote_dir": "pawbot-sync",
  "include_config": true,
  "passphrase": "长口令"
}
```

Nextcloud（应用密码同理）：

```json
"sync": {
  "provider": "webdav",
  "url": "https://cloud.example.com/remote.php/dav/files/<用户名>/pawbot/",
  "username": "<用户名>",
  "password": "<应用密码>",
  "remote_dir": "backup",
  "passphrase": "长口令"
}
```

实现细节：

- `PUT` 上传、`GET` 下载；`MKCOL` 逐级创建 `remote_dir`（已存在 405/409 视为成功）；
  连接测试用 `PROPFIND Depth: 0`。
- Basic Auth；URL / 路径按段 percent-encode。
- 建议全程 HTTPS；不要用账号主密码，用应用专用密码。

### 2.3 `http`（自建端点：POST 上传、GET 下载 + Bearer）

```json
"sync": {
  "enabled": true,
  "provider": "http",
  "url": "https://sync.example.com/pawbot",
  "token": "和你的服务端约定的 Bearer token",
  "include_config": true,
  "passphrase": "长口令"
}
```

对端契约（两种 URL 写法任选）：

1. 完整端点 + query：`POST https://sync.example.com/pawbot?name=pawbot-sync.tar.gz.enc`
2. 模板：`"url": "https://sync.example.com/pawbot/{name}"`

请求：

- `POST <url>`，`Content-Type: application/octet-stream`，Body 是完整包字节；
  `Authorization: Bearer <token>`（配置了 token 才带）。
- `GET <url>` 下载同一个文件。
- 建议成功返回 `200/201/204`；服务端返回 `4xx/5xx` 会被映射成
  `AUTH_FAILED` / `NOT_FOUND` / `HTTP_4XX` / `HTTP_5XX`，不会打印 token。
- `test` 会用 `GET ?name=__pawbot_selftest__` 探测；`200/201/204/404` 都算端点可达。

最小 Flask 例子：

```python
from flask import Flask, request, send_file, abort
import io, os

app = Flask(__name__)
TOKEN = os.environ["PAWBOT_SYNC_TOKEN"]
BLOB = "pawbot-sync.tar.gz.enc"          # 加密包名见第 4 节

@app.post("/pawbot")
def upload():
    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        abort(403)
    name = request.args.get("name", "")
    if not name.startswith("pawbot-sync.tar.gz"):
        abort(400)
    open(f"/srv/sync/{name}", "wb").write(request.get_data())
    return "", 201

@app.get("/pawbot")
def download():
    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        abort(403)
    name = request.args.get("name", "")
    path = f"/srv/sync/{name}"
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/octet-stream")
```

> 服务端务必再做一次限流/鉴权/磁盘上限；这个例子只是示意。

---

## 3. Bundle 里有什么

格式：`tar.gz`（加密时是自定义信封，见第 4 节），文件名固定：

- 未加密：`pawbot-sync.tar.gz`
- 加密：`pawbot-sync.tar.gz.enc`

包内布局（`manifest.json` 里的 `target` 才是落盘依据）：

```
persona.md                     ← data/persona.md（或 config.persona.file）
memory/facts.md                ← data/memory/facts.md
memory/slang.md                ← data/memory/slang.md
memory/impression.md           ← data/memory/impression.md
memory/people/123.md           ← data/people/123.md
memory/people/.daily_gain.json ← data/people/.daily_gain.json
memory/longmem/episodes.jsonl  ← data/longmem/episodes.jsonl
memory/diary/2026-10-21.md     ← data/diary/2026-10-21.md
memory/sessions.json           ← data/sessions.json
memory/relations.json          ← data/relations.json
memory/media_ledger.json       ← data/media_ledger.json
memory/memes.json              ← data/memes.json
stickers/<文件名>               ← data/stickers/（include_stickers=true 才有）
config.json                    ← data/config.json（必须加密）
manifest.json
```

`manifest.json`：

```json
{
  "format": "pawbot-sync",
  "format_version": 1,
  "pawbot_version": "1.0.0",
  "created_at": "2026-10-27T10:00:00Z",
  "encrypted": true,
  "encryption": "AES-256-GCM",
  "includes": {"persona": true, "memory": true, "config": true, "stickers": false},
  "entry_count": 12,
  "total_bytes": 34567,
  "entries": [
    {"path": "persona.md", "target": "persona.md", "size": 123, "sha256": "…"}
  ]
}
```

拉取时会逐条校验 `size` + `sha256`；manifest 没声明的文件、声明了但不在包里的文件、
条目路径不一致都会导致失败（`MANIFEST_INVALID` / `MANIFEST_MISMATCH`）。

**永不进包**：`data/brain/`、`data/browser/`、`data/album/`、`data/image_cache/`、
`data/napcat/`、`*.log`、`*.tmp`、`*.bak` / `*.bak-*`、`__pycache__`、符号链接。

`data/brain/` 是运行时工具产物，`data/stickers/` 默认不同步（要同步就 `include_stickers`）。
记忆里如果有什么不该上云的东西，别放这些目录；这个工具不做内容级过滤。

---

## 4. 加密

### 4.1 规则

- `passphrase` 非空 → 用 **AES-256-GCM**（随机 16 字节 salt + 12 字节 nonce，
  PBKDF2-HMAC-SHA256 200000 轮派生 32 字节密钥）。
- 加密库优先 `cryptography`，没有则用 `pycryptodome`；两个都没有就明确报
  `CRYPTO_UNAVAILABLE`。**不会自己实现 AES。**
- **未加密 + `include_config=true` → 直接拒绝**（`CONFIG_REQUIRES_ENCRYPTION`）。
  这是 fail-closed：配置里有 API key，不允许明文上云。
- 未加密只同步人设/记忆时是允许的；但建议永远设 `passphrase`。

依赖（可选装，不进 `requirements.txt`）：

```powershell
py -m pip install cryptography
# 或者
py -m pip install pycryptodome
```

### 4.2 信封格式

```
"MAGIC(8B) = PWBSYNC\x01" | salt(16B) | nonce(12B) | AES-256-GCM(ciphertext||tag)
PBKDF2-HMAC-SHA256(passphrase, salt, 200000 轮) → 32B key
```

- 拉取加密包时 passphrase 为空 → `PASSPHRASE_REQUIRED`；
- 本机设了 passphrase、远端却只有明文包 → 拒绝拉取（`ENCRYPTION_MISMATCH`，防降级）；
- **passphrase 丢了不能恢复**。没有后门、没有密钥托管；只能重新 push 一份新的。
- 本机 `data/config.json` 里的 `sync.passphrase` 是明文存储的（和里面的 LLM key 一样）。
  要更安全就限制 `data/` 的文件权限，并给云账号开二次验证。

---

## 5. 冲突策略

默认：**手动 push 覆盖远端；pull 以云端为准，但不碰 config.json。**

| 情况 | 行为 |
| --- | --- |
| 远端已有同名包 | push 直接覆盖（远端只保留最新一份；要历史版本就自己备份或让 http 端点做版本化） |
| pull 时本地人设/记忆与云端不同 | 以云端为准覆盖；覆盖前备份到 `data/backup-sync-<时间>/` |
| pull 时内容完全一致 | 不重写、不备份（结果里记在 `unchanged`，避免每拉一次攒一个备份目录） |
| pull 时本地多出云端没有的文件 | **不删除**（只覆盖/新增，避免误删） |
| pull `config.json`（默认） | 不覆盖；写 `data/config.synced.json`，返回/打印 key 级别的 diff（只有 key 名，没有值） |
| pull 且 `overwrite_config=true` / CLI `--overwrite-config` | 先备份，再覆盖 `data/config.json`；**保留本机 `sync` 段**（否则可能把本机的云同步配置冲掉） |
| 启动自动拉取 | 只 pull 一次，从不自动 push；失败只 `warn`，不影响 Bot 启动 |

`data/config.synced.json` 是「云端那份配置的副本」，方便对比后手动合并：

```powershell
# 看差异（只列 key）
py tools/cloud_sync.py pull --dry
# 真的覆盖（先备份）
py tools/cloud_sync.py pull --overwrite-config
```

---

## 6. 命令

先 `cd app`，并设置 `PYTHONPATH=.`（或直接用 `py -m` 的方式从 `app/` 里跑）。

```powershell
cd app
$env:PYTHONPATH="."

py tools/cloud_sync.py status
py tools/cloud_sync.py test
py tools/cloud_sync.py push
py tools/cloud_sync.py push --dry          # 只打包看摘要，不写远端
py tools/cloud_sync.py pull
py tools/cloud_sync.py pull --dry          # 只下载 + 校验 + 看 diff，不写盘
py tools/cloud_sync.py pull --overwrite-config
```

输出示例（不含 secret）：

```
[OK] 上传完成
  目标      webdav（WebDAV） · pawbot-sync.tar.gz.enc
  大小      48.2 KB · 文件 12 个 · 加密 是（AES-256-GCM） · 耗时 312 ms
```

- 退出码：`0` 成功，`1` 失败（打印 `[错误码] 中文说明`）。
- CLI 每次都会重新读 `data/config.json`，改完配置不用重启。
- 要指定别的项目根目录：`py tools/cloud_sync.py --base D:\pawbot pull`（`--base` 写在子命令**前面**）。

---

## 7. 启动时自动拉取

`Bot.start()` 里有一行钩子：

```python
cloudsync.start_auto_pull(self.config, self.base_dir)
```

满足 `enabled && auto_pull_on_start` 时，起一个后台 daemon 线程执行一次 `pull`：

- 不 `await`、不阻塞事件循环；
- 成功：`log.info("云同步启动拉取完成：文件=… 写入=… 加密=…")`；
- 失败：`log.warn("云同步启动拉取失败（不影响启动）：<错误码> …")`，然后照常启动；
- **不会自动 push**；也不会因为拉取失败而退出。

---

## 8. 备份 / 回滚

- 每次 pull 覆盖已有文件前，备份到 `data/backup-sync-YYYYmmdd-HHMMSS/`（冲突时自动加 `-2`），
  目录结构与 `data/` 相同（如 `data/backup-sync-…/config.json`、
  `data/backup-sync-…/persona.md`、`data/backup-sync-…/people/123.md`）。没有文件被覆盖就不建目录。
- 回滚：

```powershell
# 停掉 PawBot，然后把备份里的文件按相对路径拷回去
Copy-Item app\data\backup-sync-20261027-183000\config.json app\data\config.json -Force
Copy-Item app\data\backup-sync-20261027-183000\persona.md app\data\persona.md -Force
```

- 备份目录不会被同步（`backup-sync-*` 在排除名单里），也不受 `pull` 删除。
- `data/config.synced.json` 可以一直留着做「最近一次云端的配置」参照，不会被覆盖逻辑删掉。

---

## 9. 安全边界

- **`include_config=true` 时，API key / 各类 token 会被上传。** 包必须加密；服务器必须是
  你自己信得过的机器。用别人的 WebDAV/对象存储就要接受「对方管理员理论上能拿到密文」这件事。
- 加密只保护「包在静止/传输时」；本机 `data/config.json`、`data/config.synced.json` 仍是明文。
- bundle 里的 `config.json` 含 `sync` 段本身（包括 WebDAV 密码 / Bearer token / passphrase）。
  所以：**用应用专用密码，不要用主密码**；`http`/`webdav` 一律走 HTTPS。
- `passphrase` 丢失 = 数据不可恢复；不存在绕过路径。
- 日志、CLI 输出、`status()` / `push()` / `pull()` 返回值都不含 password / token / passphrase
  的值；异常消息也会做一次 `scrub`（把配置里的 secret 替换成 `***`）。
  `status().url` 会去掉 URL 里的 `user:pass@` 和 query（query 常带 token）。
- 解包只认 `manifest.json` 声明的相对路径：拒绝 `../`、绝对路径、Windows 盘符、符号链接/硬链接、
  非普通文件、manifest 未声明的文件；写入目标必须落在 `data/` 内。所有文件先写临时文件再
  `os.replace`。
- 远端包被替换/损坏：sha256 校验会发现（`MANIFEST_MISMATCH`）。但**没有签名**——能改你云盘的人
  可以换一份完整的、manifest 自洽的包。要让这个也防住，请给服务器加写权限控制。

---

## 10. 错误码

| 错误码 | 含义 / 处理 |
| --- | --- |
| `CONFIG_REQUIRES_ENCRYPTION` | `include_config=true` 但没设 `passphrase`；补 passphrase 或关掉 include_config |
| `CRYPTO_UNAVAILABLE` | 没有 `cryptography` / `pycryptodome`；装一个 |
| `BAD_PASSPHRASE` | 解密失败：口令错或包损坏 |
| `PASSPHRASE_REQUIRED` | 远端是加密包，本机没配 passphrase |
| `ENCRYPTION_MISMATCH` | 本机配了 passphrase，远端却是明文包（防降级），拒绝拉取 |
| `BAD_BUNDLE` | 不是合法的 gzip/同步包、被截断、config.json 不是 JSON |
| `MANIFEST_INVALID` | manifest 缺字段/版本不支持/声明与包内文件不一致 |
| `MANIFEST_MISMATCH` | 文件大小或 sha256 对不上（包被改过/损坏） |
| `PATH_TRAVERSAL` | 包内出现 `../`、绝对路径、符号链接等，整包拒绝 |
| `NOT_FOUND` | 远端没有包，先 push；或服务端 404 |
| `AUTH_FAILED` | 401/403：用户名/密码或 Bearer token 不对 |
| `HTTP_4XX` / `HTTP_5XX` / `HTTP_ERROR` | 其他 HTTP 状态；看服务端日志 |
| `TIMEOUT` | 30 秒超时，网络慢或服务端没响应 |
| `NETWORK` | 连不上：URL、DNS、代理、防火墙 |
| `IO_ERROR` | `local` 目录不可读写 |
| `CONFIG_INVALID` | `url` 没填 / 协议不对（http、webdav 必须 http(s)://） |
| `UNKNOWN_PROVIDER` | `provider` 不是 `local` / `webdav` / `http` |
| `BUNDLE_CONFLICT` | 包内路径重名（极少见，通常是自定义目录撞名） |

---

## 11. 面板接入提示（给 WebUI）

`from pawbot import cloudsync`：

| 调用 | 用途 |
| --- | --- |
| `cloudsync.status(config, base_dir)` | 只读状态；`url` 已脱敏，secret 只有 `*_set` 布尔 |
| `cloudsync.test_connection(config, base_dir)` | 探测远端；失败抛 `SyncError` |
| `cloudsync.push(config, base_dir, dry=False)` | 上传；返回 `remote_name/bytes/files/encrypted/encryption/duration_ms/last_push` |
| `cloudsync.pull(config, base_dir, dry=False, overwrite_config=None)` | 下载应用；返回 `applied/skipped/config.diff/backup_dir` |
| `cloudsync.make_adapter(sync_section, base_dir)` | 底层适配器（`upload/download/test`） |
| `cloudsync.sync_section(config)` | 合并默认值后的 sync 段，**含 secret 原值**，只给后端用 |

- 面板保存配置时：secret 输入框留空 = 保持原值（不要把 `***` 写回去）。
- `status().warning == "CONFIG_REQUIRES_ENCRYPTION"` 时应在 UI 提示。
- push/pull 建议放到后台线程，耗时取决于包大小和网络；不要在 WebUI 请求线程里同步跑大包。
- `pull()` 的 `config.diff` 只有 key 名（`changed` / `added` / `removed`），可以安全展示。

---

## 12. 已验证 / 未验证

已验证（`py -m unittest tools.test_cloudsync -q`，14 个用例）：

- local 往返、manifest 逐条 sha256、路径穿越/绝对路径/盘符/符号链接拒绝；
- 未加密 + include_config 被拒；
- 有 `cryptography` 时加密往返、错 passphrase 失败、密文不含明文 key；
- `include_memory=false` / `include_stickers` 开关；
- 默认不覆盖 config、写 `config.synced.json`、diff、覆盖前备份、保留本机 sync 段；
- 本地假 HTTP 服务上的 push/pull、403 → `AUTH_FAILED`、500 → `HTTP_5XX`；
- `status` 不含 secret、URL 脱敏；启动钩子开关与后台拉取。

未验证 / 已知限制：

- **没有真实 WebDAV 账号**：坚果云 / Nextcloud / 群晖未实机跑过，`PROPFIND` 和
  `MKCOL` 的兼容性按标准实现，但可能有服务端差异；建议先 `py tools/cloud_sync.py test`。
- 没有对超大 bundle（比如几万条 sessions）做性能压测；目前是整包读进内存。
- 单次 push/pull 不并发合并：同一时间两台机器各 push 一次，后写的赢；实时模式（见下）用 manifest + 冲突策略缓解。
- 不删除本地多余文件、不做内容级过滤；敏感内容请勿放进这些目录。
- WebUI 面板（`client.js`）由 Lead 接，本模块只提供后端函数与 CLI。


## 13. 近实时同步（可选，2026-10-27 追加）

不想手动 push/pull 时，可以让 PawBot 自己同步。注意：这不是 Syncthing 那种秒级双向文件系统，
而是「够实时 + 不把写记忆变成上传风暴」的折中：

`ini
sync.enabled = true
sync.mode = realtime          # manual（默认）/ realtime
sync.poll_sec = 2             # 每 2 秒看一次本地文件
sync.debounce_sec = 3         # 发现变化后再等 3 秒（避免写一半）
sync.remote_poll_sec = 30     # 每 30 秒看一次远端 manifest
sync.conflict_policy = keep_both   # keep_both / local / remote
sync.first_run = keep_both    # 首次发现远端数据：keep_both / pull
`

行为：

1. 本地文件（人设/记忆/可选 config）内容指纹变了 → 防抖后整包 push；再上传一个小
   pawbot-sync.manifest.json（sha256/文件数/大小），远端判断变化只读这个。
2. 远端 manifest 的 sha256 变了 → 若本机没改过就 pull；若两边都改过：
   - keep_both（默认）：远端包存到 data/sync-conflict-<时间>/（含 README），**本机不动**；
   - local：忽略这次远端变化，只记日志；
   - emote：远端优先，直接 pull（persona/记忆覆盖；config 仍按 overwrite_config）。
3. 首次运行（本机还没有 data/.sync-state.json）默认 keep_both：宁可让你确认，也不悄悄
   用远端覆盖本机人设。
4. 状态文件：data/.sync-state.json（bundle_sha / local_fp / last_push / last_pull），可删，
   删掉相当于「首次运行」。

前台观察 / 调试：

`powershell
py tools/cloud_sync.py watch --interval 2 --remote-interval 30 --debounce 3
`

限制：

- 轮询是秒级到几十秒级，不是毫秒级；两边同时 push 仍是「后写赢」，冲突要靠上面的策略兜底。
- sessions.json 每条聊天都会变，实时模式下会每 debounce_sec 左右上传一次（包小但频繁）；
  嫌吵就把 sync.include_memory 关掉，只同步人设/配置。
- 多台机器同时开 realtime 指向同一个远端时，务必用同一个 passphrase，并接受「冲突目录人工确认」。
- 运行态 .sync-state.json 和 sync-conflict-*、ackup-sync-* 都应在 .gitignore 里（仓库已排除 data/ 下的运行产物）。