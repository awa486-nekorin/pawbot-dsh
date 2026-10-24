# @local/pawbot-bili-watch

让 QQ 机器人「大肥鱼」(PawBot) 真的去看 B 站视频：读字幕、翻热门评论，产出结构化内部报告。

插件只做两件事：**看**（抓取 B 站视频信息 / 字幕 / 评论，再用一次 LLM 单次调用消化成 JSON）与**写报告**（原子写入共享目录）。它**不发任何 QQ / B 站消息、不发评论**——怎么在群里说话是 PawBot 表达层的事。

## 安装（本地目录）

在 DSH 里以 `install_bundle` 安装本目录（`plugin_manager` 的 `action: install_bundle`，target 为本目录绝对路径），或让 PawBot 侧在 profile 的 bundle 清单列出本包名。本包零 `dependencies`；`@deepseek-ai/dsh-tools`、`@deepseek-ai/schemastery` 是 optional peer，拿不到时插件会降级（没有设置页 / 工具注册用本地等价实现），本体照常跑。

## Config 字段（均可在 DSH 插件设置页改）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `provider` | `deepseek-official` | LLM provider（单次调用） |
| `model` | `deepseek-flash` | LLM model（⛔ 只允许 v4-flash；填 v4-pro 会被 lib/model-guard.js 挡掉改回 flash） |
| `dataRoot` | `D:/WorkBuddy/pawbot-bili-watch-data` | 共享目录 ROOT（与 PawBot 的契约路径） |
| `cookieEnv` | `BILI_SESSDATA` | 从哪个环境变量读 B 站 SESSDATA |
| `maxSubtitleChars` | `12000` | 字幕文字最多喂给模型几个字 |
| `maxDurationSec` | `3600` | 超过此时长（秒）的长视频直接跳过 |
| `pollSec` | `30` | 请求队列轮询间隔（秒） |
| `reportTtlHours` | `72` | 已有报告几小时内不重看 |
| `autoStock` | `true` | 自动备货总开关 |
| `stockTarget` | `3` | 未分享且值得分享的报告少于此数就自动补 |
| `autoIntervalMin` | `90` | 自动备货间隔（分钟） |
| `dailyLimit` | `12` | 每日自动备货上限（落盘在 `state.json`，重启不重置） |
| `autoWindows` | `["12:00-14:00","00:30-08:30"]` | 允许时段 `HH:MM-HH:MM`，可跨午夜；空数组 = 全天 |
| `maxTokens` | `8192` | 单次 LLM 调用最大输出 tokens |

## 共享目录契约（与 PawBot 一字不差）

ROOT = `dataRoot`（默认 `D:/WorkBuddy/pawbot-bili-watch-data`）

- `ROOT/requests/<id>.json`：PawBot 写入，插件消费。格式 `{"id":string,"kind":"bvid"|"keyword"|"auto","value":string,"requested_at":ISO8601,"session":string}`。插件处理完移到 `ROOT/requests/done/`。
- `ROOT/reports/<bvid>.json`：插件写入。原子写：先写 `<bvid>.json.tmp` 再 rename。
- `ROOT/reports/index.json`：插件维护 `[{bvid, title, created_at, watched_level}]` 最新在前，最多 200 条。
- `ROOT/shared.json`：PawBot 写入 `{"<bvid>": {"session": string, "shared_at": ISO8601}}`，插件只读（用来避免重复推荐）。

Report schema（version 1）见 `reports/<bvid>.json`；`watched_level` 只可能是 `subtitle` / `desc_comments` / `title_only`，由程序决定、不信模型。低于 `subtitle` 时，`summary` / `take` 不会假装看过画面或听过内容，`highlights` 不带时间点。

## SESSDATA 环境变量

B 站字幕需要登录凭证：把 SESSDATA 放进环境变量（默认名 `BILI_SESSDATA`，可用 `cookieEnv` 改）。**凭证只从环境变量读，绝不写进日志或报告。** 所有 `api.bilibili.com` 请求的 Cookie 头由插件自动补上 `buvid3` / `buvid4` 指纹，有 SESSDATA 时再接在后面（同样绝不输出）。

没设 cookie 时，字幕多半拿不到 → 该视频 `watched_level` 自动降为 `desc_comments`，不会报错中断。

## 限制

- **B 站风控**：连续请求同一个接口（尤其搜索 `/x/web-interface/search/type`）会回 HTTP 412。插件首次请求前会调用 `x/frontend/finger/spi` 取得 `buvid3` / `buvid4` 指纹并在进程内缓存（6 小时），遇 412（或 403 且提示风控）会等 0.8~1.5 秒、刷新指纹后重试最多 2 次；仍失败会抛出明确的「B站风控 412」错误，不会静默回空数组。设 `BILI_SESSDATA` 环境变量可大幅降低 412 概率、提升字幕成功率。
- 字幕只取优先 `zh-CN` / `ai-zh` 的一份；AI 字幕（ai-zh）通常需要 cookie。
- 多 P 视频只取 view 接口返回的 `cid`（第一 P）的字幕。
- 搜索接口需要 WBI 签名。搜索被风控时会先降级为「热门榜 + 标题关键词筛选」；若热门榜也筛不到（或筛到的都看过/分享过），会回报「B站搜索失败：<原因>」，与「都看过了」明确区分。
- 插件只写 `ROOT` 下的文件；不装任何依赖、不改 DSH profile。

## 自测

```bash
node --check index.js
node --check lib/wbi.js   # 对每个 .js 逐一 --check
node selfcheck.mjs        # 离线自测（不碰网络、不依赖 DSH）
```
