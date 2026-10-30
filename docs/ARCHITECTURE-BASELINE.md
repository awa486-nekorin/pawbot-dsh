# PawBot DSH 架构基线与契约冻结（方案 A / 第 1 批次）

> 第 1 批次交付物
> 适用仓库：`pawbot-dsh`
> 检查器：`scripts/architecture-baseline.mjs`
> 离线测试：`test/architecture-baseline.mjs`
> 最后核对：2026-10-30

## 1. 目标与范围

把当前已跑通的架构事实固化成可重复执行的只读基线。后续改动先跑基线；契约漂移以非零退出码暴露。

冻结范围：

1. 关键文件与入口直接相对依赖；
2. `package.json` 身份、Node/DSH 版本下限、`exports`、DSH 打包入口；
3. Node 入口（`index.js`、`client.js`、`bili-watch/index.js`）与 Python 入口（`app/run.py`）；
4. 旧 `/pawbot-ui/*` 代理：前缀、16 条 exact 子路径、HTTP 方法、未知路径 404；
5. 只读桥 `/pawbot-dsh-bridge/execute`、大脑桥 `/pawbot-dsh-brain/turn|status` 的路径、环境变量、工具白名单、限额与 Host exact 注册；
6. Python `config.py` 的 `DEFAULT` 单一来源、38 个顶层配置段、运行态深合并；
7. WebUI 关键 GET/POST 端点，以及 token/body/大脑工具回调的静态安全观测。

不做：修改业务源码、`app/data`、人设、密钥或 `package.json`；联网；启动 PawBot/DSH；执行 Python；运行时端口探测。

## 2. 交付物

| 文件 | 作用 |
| --- | --- |
| `scripts/architecture-baseline.mjs` | 只读基线检查器；人类输出、`--json`、`--out`、`--verbose` |
| `test/architecture-baseline.mjs` | 离线测试；成功/负例/参数/拒写/数值表达式/DEFAULT/端点/CSRF 行为 |
| `docs/ARCHITECTURE-BASELINE.md` | 本文件 |

三个文件也在检查器的 `REQUIRED_FILES` 内。

## 3. 运行与退出码

```bash
node scripts/architecture-baseline.mjs
node scripts/architecture-baseline.mjs --root D:/paw-old/pawbot-dsh
node scripts/architecture-baseline.mjs --json
node scripts/architecture-baseline.mjs --json --out D:/tmp/pawbot-baseline.json
node scripts/architecture-baseline.mjs --verbose
node scripts/architecture-baseline.mjs --help
```

| 参数 | 说明 |
| --- | --- |
| `--root <dir>` | 仓库根目录；默认 cwd。以 `-` 开头的值用 `--root=<dir>` |
| `--json` | JSON 报告到 stdout；不混人类输出 |
| `--out <new.json>` | 新建 JSON 报告；父目录必须已存在；root 内只允许 `<root>/reports/`；不覆盖任何已有文件 |
| `--verbose` | 人类输出显示 expected/actual |
| `--help`, `-h` | 帮助 |

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部通过 |
| `1` | 契约检查失败 |
| `2` | 参数/根目录/`--out` 安全校验/写入失败 |

仅使用 Node 内置模块，无 npm 依赖。

## 4. JSON 报告

```json
{
  "schema": "pawbot-dsh/architecture-baseline@1",
  "generated_at": "2026-10-30T00:00:00.000Z",
  "root": "D:\\paw-old\\pawbot-dsh",
  "node": "v24.14.0",
  "platform": "win32",
  "duration_ms": 12,
  "ok": true,
  "summary": { "total": 19, "passed": 19, "failed": 0 },
  "checks": [
    {
      "id": "package.contract",
      "category": "package",
      "title": "package 身份 / engines / exports / dsh",
      "ok": true,
      "expected": {},
      "actual": {}
    }
  ]
}
```

失败项带 `expected`/`actual`，异常带 `error`，多标记检查带 `detail`。`python.webui.security-static` 与 `python.config.source` 带 `mode: "static"` 与 `limitation`。

## 5. `--out` 保守策略

- 只允许新建 `.json` 报告；父目录必须已存在。
- **root 内只允许写入 `<root>/reports/` 下的新报告**；root 外允许新 `.json`，但仍受数据根与 `package.json` 规则限制。
- **任何位置、任何大小写的 `package.json` 都禁止作为报告目标**，无论文件是否存在；避免缺文件时把报告写进生产路径。
- 拒绝覆盖任何已有文件（包括已有报告）；用户应指定新文件名。
- 拒绝叶子符号链接、目录、特殊文件。
- 同时校验词法路径、父目录 realpath、目标 realpath、`<root>/data` 与 `<root>/app/data` 的 realpath；覆盖根目录本身是链接、外部数据目录 junction 的情况。
- 原子发布：临时文件 `wx` 写入并 `fsync`，用 `linkSync` 发布；目标已存在时 `EEXIST`，绝不覆盖。临时文件随后删除。
- 不带 `--out` 时不创建任何文件；报告内容不读取 `app/data` 或密钥。

被拒绝的路径示例：

```text
<root>/data/report.json
<root>/app/data/report.json
<root>/data/nested/report.json
<root>/data -> junction 的外部目录/report.json
<root>/linked-data/report.json          # 父目录 junction -> data
<root>/leaf-report.json                 # 叶子 symlink
<root>/package.json                     # 已存在
<root>/package.json                     # 即使缺文件也拒绝
<root>/reports/package.json             # reports 内也拒绝
<root>/not-reports.json                 # root 内非 reports
<root>/reports/../not-reports.json      # .. 回溯到 root 非 reports
```

## 6. 冻结契约

### 6.1 关键文件（29 个）

```text
package.json
cordis.patch.yml
index.js
client.js
lib/webui-proxy.js
lib/dsh-tool-bridge.js
lib/dsh-brain.js
lib/hostdeps.js
lib/config-utils.js
lib/setup.js
lib/data-guard.js
app/run.py
app/requirements.txt
app/pawbot/core.py
app/pawbot/config.py
app/pawbot/webui.py
app/pawbot/dsh_bridge.py
app/pawbot/brain.py
bili-watch/index.js
bili-watch/selfcheck.mjs
bili-watch/locale/en.json
bili-watch/locale/zh.json
bili-watch/icon.svg
locale/en.json
locale/zh.json
icon.svg
scripts/architecture-baseline.mjs
test/architecture-baseline.mjs
docs/ARCHITECTURE-BASELINE.md
```

### 6.2 `package.json`

```json
{
  "name": "@local/pawbot",
  "type": "module",
  "engines": { "dsh": ">=0.1.7-rc.1", "node": ">=22" }
}
```

`exports`：

| 键 | 目标 |
| --- | --- |
| `.` | `./index.js` |
| `./client` | `./client.js` |
| `./bili-watch` | `./bili-watch/index.js` |
| `./bili-watch/locale/*.json` | `./bili-watch/locale/*.json` |
| `./bili-watch/icon` | `./bili-watch/icon.svg` |
| `./locale/*.json` | `./locale/*.json` |
| `./icon` | `./icon.svg` |
| `./package.json` | `./package.json` |

`dsh`：

```json
{
  "bundle": { "patch": "./cordis.patch.yml" },
  "client": {
    "platform": "web",
    "inject": [
      "@deepseek-ai/dsh-client-runtime",
      "@deepseek-ai/dsh-client-ui-settings",
      "@deepseek-ai/dsh-client-ui-settings-plugins"
    ]
  }
}
```

导出目标文件必须实际存在；`*.json` 通配目录至少有一个匹配文件。

### 6.3 Node / Python 入口

| 入口 | 冻结事实 |
| --- | --- |
| `index.js` | `name='pawbot'`；`inject=['tools','webServer']`；有 `Config`、`apply(ctx, config)` |
| `client.js` | `window.__ModuleLoader__.load({...})`；plugin id `@local/pawbot`；`const API = '/pawbot-ui'` |
| `bili-watch/index.js` | `name='pawbot-bili-watch'`；`inject=['tools','llm']`；有 `Config`、`apply` |
| `app/run.py` | 导入 `pawbot.core.Bot` 与 `pawbot.webui.WebUI`；`BASE_DIR`；`Bot(BASE_DIR)`；`__main__` → `asyncio.run(main(parse_args()))` |

入口的直接相对导入必须全部可解析，包括：

- `index.js -> ./lib/hostdeps.js`；
- `bili-watch/index.js -> ./lib/hostdeps.js`（解析到 `bili-watch/lib/hostdeps.js`）；
- 其余 `./lib/*.js`。

### 6.4 旧 `/pawbot-ui/*`

Host 注册：

```text
prefix /pawbot-ui
prefix /pawbot-ui/
exact  /pawbot-ui/${p}
```

16 条 exact 子路径：

```text
status, config, persona, reload-plugins, plugins/reload,
send, restart, context, memory,
setup/status, setup/run,
napcat/status, napcat/setup, napcat/qr,
boot, boot/status
```

方法契约：

| 方法 | 子路径 |
| --- | --- |
| GET | `status` |
| GET / POST | `config` |
| GET / POST | `persona` |
| POST | `reload-plugins` |
| POST | `plugins/reload` |
| POST | `send` |
| POST | `restart` |
| GET | `context` |
| GET | `memory` |
| GET | `setup/status` |
| POST | `setup/run` |
| GET | `napcat/status` |
| POST | `napcat/setup` |
| POST | `napcat/qr` |
| POST | `boot` |
| GET | `boot/status` |

未知子路径返回 404。代理 import 的引号类型与花括号内空白不敏感（`import {createPawbotProxy} from "./lib/webui-proxy.js"` 等价）。CSRF/同源守卫不在静态基线里冒充证明；它由离线测试直接调用 `createPawbotProxy().dispatch` 验证：跨源 POST 403、非 JSON POST 415、同源 JSON POST 放行、跨源 GET Referer 403、拿不到 Host origin 时非 GET fail-closed。

### 6.5 桥接端点

只读桥：

| 契约 | 值 |
| --- | --- |
| Host exact 路由 | `/pawbot-dsh-bridge/execute` |
| Python 路径 | `/pawbot-dsh-bridge/execute` |
| URL / Token 环境变量 | `PAWBOT_DSH_BRIDGE_URL` / `PAWBOT_DSH_BRIDGE_TOKEN` |
| Token 形态 | `pbdsh_` + 64 位小写十六进制 |
| 工具白名单 | 仅 `bili_watch_status` |
| 请求 / 响应体 | 1024 B / 1024 B |
| 每分钟 / 并发 / 超时 | 30 / 2 / 2000 ms |
| Python 上限 | `MAX_RESPONSE_BYTES=1024`、`TIMEOUT_SECONDS=2` |

Host 必须 `import { BRIDGE_PATH, ... }` 并注册：

```js
ctx.webServer.register({ kind: 'exact', path: BRIDGE_PATH, handler: bridge.dispatch })
```

Python 客户端只接受 loopback、精确路径、POST、Bearer、JSON；Host 注入前剥离同名环境变量。

大脑桥：

| 契约 | 值 |
| --- | --- |
| 轮次路由 | `/pawbot-dsh-brain/turn` |
| 状态路由 | `/pawbot-dsh-brain/status` |
| URL / Token 环境变量 | `PAWBOT_DSH_BRAIN_URL` / `PAWBOT_DSH_BRAIN_TOKEN` |
| Python 请求 | POST + Bearer + JSON |
| Host 注册 | 两个独立 `register` 对象：`kind: 'exact'` + `path: BRAIN_TURN_PATH` + `handler: brain.dispatchTurn`；`kind: 'exact'` + `path: BRAIN_STATUS_PATH` + `handler: brain.dispatchStatus`；单双引号/空白无关 |

### 6.6 Python `config.py` 的 DEFAULT

`DEFAULT` 是运行态配置的唯一默认来源：

- 只允许一次 `DEFAULT = { ... }` 字典赋值；
- 不允许后续 `DEFAULT[...] = ...` 或 `DEFAULT.update(...)`；
- 38 个顶层段必须全部是 dict，且无重复键；
- `Config` 使用 `self.data = deepcopy(DEFAULT)`；
- 磁盘读取变量（如 `disk` / `payload`）必须来自 `json.load(...)`，并作为 `self.data = _deep_merge(DEFAULT, 同变量)` 的第二个参数；只改 `json.load` 变量名而不同步 merge 使用点视为破坏链路；
- patch 使用 `self.data = _deep_merge(self.data, patch)`；
- 检查前剥离 Python 行注释，注释里的 `self.data = _deep_merge(DEFAULT, disk)` 不能冒充有效合并；
- 检查不依赖具体局部变量名，也不限制 `deepcopy(DEFAULT)` 出现次数；
- 该项是静态源码观测（`mode: "static"`），不执行 `Config`，只验证文本赋值链路。

38 个顶层段：

```text
store, longmem, memory, diary, hobbies, reminders, memes,
server, webui, bot, chat, spice, people, relations, reply,
slang, persona, media, account, tools, speech, video, tts,
qzone, selfedit, album, llm, judge, libido, emotion,
whitelist, random_interject, topic, life, proactive, events,
safety, sync
```

关键默认值：

| 段 | 键 | 值 |
| --- | --- | --- |
| `server` | `host` / `port` / `path` / `access_token` | `0.0.0.0` / `6199` / `/ws` / `""` |
| `webui` | `host` / `port` / `token` | `127.0.0.1` / `6185` / `""` |

`Bot` 固定读 `<base_dir>/data/config.json`；`run.py` 用 `bot.config.get("webui.host|port|token", ...)` 构造 WebUI。

### 6.7 WebUI 端点

GET：

```text
/, /index.html, /api/status, /api/brain/status, /api/config,
/api/plugins, /api/events, /api/logs, /api/sessions, /api/people,
/api/memory, /api/context, /api/models, /api/files, /api/file,
/api/stickers, /stickers/<name>, /api/stream (SSE)
```

POST：

```text
/api/brain/tool, /api/config, /api/plugins/reload, /api/plugins/toggle,
/api/models/save, /api/models/delete, /api/models/apply,
/api/context/window, /api/send, /api/llm/test, /api/session/clear,
/api/memory/forget, /api/file
```

端点解析只认可执行的 `if`/`elif` 行；Python 注释中的旧路径和 `if False and path == ...` 死分支不会算端点。

安全观测（`mode: static`）：`MAX_HTTP_BODY=4MB`、面板 token 校验（`/api/brain/tool` 例外）、`hmac.compare_digest`、大脑工具回调的 loopback/brain token/active ctx/工具白名单/主人写权限，以及 `python.config.source` 的静态赋值链路。基线只声明这些代码事实存在，不声称已验证运行时安全效果；CSRF 行为由测试套件验证。

## 7. 检查项（19 项）

| # | id | 类别 | 内容 |
| --- | --- | --- | --- |
| 1 | `files.required` | files | 29 个关键文件 |
| 2 | `package.contract` | package | 身份/engines/exports/目标文件/dsh |
| 3 | `entry.node.index` | entry | name/inject/Config/apply |
| 4 | `entry.node.client` | entry | ModuleLoader/plugin id/API base |
| 5 | `entry.node.bili-watch` | entry | name/inject/Config/apply |
| 6 | `entry.python.run` | entry | 导入/BASE_DIR/main/`__main__` |
| 7 | `entry.relative-imports` | entry | 两个入口的直接相对依赖可解析 |
| 8 | `routes.pawbot-ui.subpaths` | routes | 两个 prefix + exact 模板 + 16 条子路径 |
| 9 | `routes.pawbot-ui.methods` | routes | 18 个 method+sub + 未知 404 |
| 10 | `bridge.tool.constants` | bridge | 路径/环境变量/token/loopback/Host exact 注册 |
| 11 | `bridge.tool.allowlist-limits` | bridge | 白名单 + JS/Python 限额（按值） |
| 12 | `bridge.tool.python-client` | bridge | Python POST/Bearer/JSON/响应上限 + Host 注入 |
| 13 | `bridge.brain.protocol` | bridge | 路径/环境变量/客户端/两条 exact register |
| 14 | `python.config.default` | python | 38 段/无重复/全 dict/单一赋值/默认值 |
| 15 | `python.config.source` | python | 注释剥离 + json.load 变量与 `_merge(DEFAULT, 同变量)` 链路（静态） |
| 16 | `python.config.wiring` | python | core.py 与 run.py 接线 |
| 17 | `python.webui.endpoints.get` | webui | 关键 GET + index/stickers/SSE |
| 18 | `python.webui.endpoints.post` | webui | 关键 POST + brain/tool/未知 404 |
| 19 | `python.webui.security-static` | webui | token/body/brain tool 静态观测 |

## 8. 变更维护规则

有意变更时：改生产代码 → 同步检查器常量/正则 → 同步本文件 → 同步测试 → 重跑 `node scripts/architecture-baseline.mjs` 与 `node test/architecture-baseline.mjs`。

不要为迁就断言修改生产源码；基线实现与真实源码不一致时优先修基线。不要放宽检查来掩盖漂移。

## 9. 测试覆盖

运行：

```bash
node test/architecture-baseline.mjs
```

覆盖 43 个用例：

| 场景 | 断言 |
| --- | --- |
| 成功路径 | 19/19 通过、退出 0、报告 schema/root/summary、数据哨兵不变 |
| 默认 cwd / 人类输出 / `--verbose` | 均通过 |
| `--json --out` / `--root=` / `--out=` | 在 `<root>/reports/` 新建报告，文件与 stdout 一致 |
| `--out` 已有文件 / `package.json`（存在/缺失/reports 内） | 退出 2，内容不变或目标不创建 |
| root 内非 `reports/` / `..` 回溯 | 退出 2，不创建生产路径 |
| 非 `.json` / 父目录不存在 / `--out --json` | 退出 2，不创建文件/目录 |
| 同一路径第二次 `--out` | 退出 2，报告不被覆盖 |
| 缺文件 / 缺 hostdeps | `files.required` / `entry.relative-imports` 失败 |
| package engines / 路由 / 桥路径 / Host exact 漂移 | 对应检查失败 |
| 大脑桥 turn / status 各自改成 prefix | `bridge.brain.protocol` 失败 |
| 代理 import 引号/空白变化 | `routes.pawbot-ui.subpaths` 仍通过 |
| JS/Python `1024 * 1024` | 按值失败（1048576） |
| `2 * 512` 等价值 | 通过 |
| `DEFAULT` 后续赋值 / 重复键 / 二次赋值 | `python.config.default` 失败 |
| 只改 `json.load` 变量 / 注释伪合并 | `python.config.source` 失败 |
| 正确的完整变量改名 + 额外 deepcopy | `python.config.source` 仍通过 |
| 端点注释 / `False and` | `/api/send` 缺失，旧路径不算端点 |
| 单双引号 / 额外 deepcopy | 仍通过 |
| data / app/data / 嵌套 / `..` / 父目录 junction / 外部 junction / root 链接 / 叶子 symlink | 退出 2，数据不变；symlink 不支持时 skip |
| CSRF 行为 | 跨源 POST 403、非 JSON 415、同源 JSON 200、跨源 GET 403、无 origin fail-closed |

所有夹具在 `os.tmpdir()` 下 `mkdtempSync` 创建，用例结束和进程退出时清理。

## 10. 已知边界

- 静态契约检查：不 import `index.js`、不执行 Python、不启动 WebServer；不能证明运行时路由一定注册成功。
- `client.js` 是 `window.__ModuleLoader__` bundle，不是 ESM；只校验加载器/id/API 基址。
- 带 `mode: "static"` 的项是源码观测，不是安全/运行时证明：`python.webui.security-static` 未启动 WebUI 验证 token/active_ctx 拒绝行为；`python.config.source` 不执行 `Config`，只验证注释剥离后的赋值链路文本。CSRF 行为由测试覆盖。
- Windows 文件 symlink/junction 创建在权限不足时测试会明确 skip。
- 退出码 `1` 是契约漂移，`2` 是用法/IO/安全校验失败。
