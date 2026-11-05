# 双端功能矩阵与安全对齐（2026-11-03 15:00 CST）

对象：本地 `D:\WorkBuddy\pawbot-dsh`（HEAD `408e740`，branch `upload-2026-10-27`）与云端运行源
`C:\Users\Administrator\.dsh\profiles\desktop\node_modules\@local\pawbot`（下称 live）。

数据来源：
- live 只读快照：管理员 SSH 逐文件 SHA-256（本机 `tmp/pawbot-align-20261103/manifest-live.tsv`）。
- 云端 staging `C:\pawbot-code-staging\worktree`：`pawbot_cloud_code` 读到 239 个代码文件，provenance 标注
  `source.kind=live`，管理员核对 239/239 同哈希。
- 本地清单：同一排除规则（`app/data`、`.venv`、`__pycache__`、`.git`、`dist`、`napcat`、日志、凭证）。

> 低权限桥的 `live_status` / `live_list` / `live_read` 已可**只读**使用（17:00 复测：`live_status`
> 返回 `readable: true`、242 个代码文件，`live_list` 正常列出 242 项）；桥仍然不能写、不能读
> 运行时 `app/data`/凭证，也没有绕过 ACL。没有写生产、没有重启、没有 Git push、没有读取
> `app/data` 或凭证内容。

> **2026-11-04 当前状态（覆盖下文历史记录）**：仓库当前默认与云端等价——`bili_auto_*`、
> `bili_write_*`、`admin_intent_enabled` 默认**全开**；`bili_write_quota` 默认 `{}`，
> 额度 0 = **不限**；`bili_gate.REJECTED_ACTIONS` **为空**（删除/点赞/收藏/投币/react
> 不再永久拒绝，但仍逐动作授权、审计、去重）。§3.2 / §5.2 / §7.2 / §7.3 / §9.3 里
> 「默认全关 / 0=不发 / 删除与 react 被拒绝」是当时某一轮的记录或 staging 补丁，
> 均已被 §10 的当前实现取代；**以 §10 为准**。

## 1. 总量与分类

| 指标 | 数量 |
|---|---|
| 本地代码文件 | 367 |
| live 代码文件（排除 32 个 `.before-*`） | 259 |
| 两边同路径 | 244 |
| └ 哈希一致（已一致） | 165 |
| └ 哈希不同（双方冲突/代差） | 79 |
| 仅本地有 | 123 |
| 仅 live 有 | 15（13 个 `backup-syncui-20261102-1359/*`、1 个乱码重名 md、1 个 UTF-8 BOM 重名 md） |
| live 内 `.before-*` 编辑备份 | 32 |

按目录（共同 / 同哈希 / 不同 / 仅本地 / 仅 live）：

| 区域 | 共同 | 相同 | 不同 | 仅本地 | 仅 live |
|---|---|---|---|---|---|
| `app/pawbot` | 78 | 31 | 47 | 24 | 0 |
| `app/plugins` | 13 | 10 | 3 | 0 | 0 |
| `app/tools` | 92 | 78 | 14 | 11 | 0 |
| `app/minecraft-sidecar` | 0 | 0 | 0 | 17 | 0 |
| `lib` | 11 | 9 | 2 | 0 | 0 |
| `bili-watch` | 22 | 21 | 1 | 0 | 0 |
| `test` | 5 | 2 | 3 | 11 | 0 |
| `scripts` | 6 | 5 | 1 | 9 | 0 |
| `docs` | 1 | 0 | 1 | 34 | 0 |
| 根目录（壳/metadata） | 8 | 4 | 4 | 15 | 14 |

## 2. 四类功能判定

### A. 已一致（165）
`package.json`、`icon.svg`、`locale/*`、`bili-watch` 21/22 个文件、`lib` 9/11、`app/tools` 78/92、
`app/plugins` 10/13、以及 `app/pawbot` 31 个外围模块（account/album/bus/mem 等）。

### B. 本地独有：安全运行时（123）
- `app/pawbot`：`paths.py`、`runtime_lock.py`、`lifecycle.py`、`cloudsync.py`、`sync_realtime.py`、
  `ha_content.py`、`ha_runtime.py`、`relay_client.py`、`perception.py`、`placement.py`、`policy.py`、
  `untrusted.py`、`bili_gate.py`、`bili_learning.py`、`identity.py`、`affect.py`、`brain.py`、
  `reply_policy.py`、`shortterm.py`、`minecraft_*` 等 24 个模块。
- `app/minecraft-sidecar/*`（17）、`app/tools` 11 个、`test` 11 个、`scripts` 9 个、`docs` 34 个。
- 这些是 DataRoot、单实例锁、优雅停机、外部内容隔离、B 站写闸门、MC 常驻的实现，**不得被旧版覆盖**。

### C. 云端独有/更新：11-03 当天 08:32–14:04 的手工补丁
从 `.before-*` 复原出的增量（相对本地 08:20 合并点）：
1. `config.py` + `core.py` + `ai_chat.py` + `admin_cmds.py`：指令前缀改为 **只有 `//`**，
   `allow_no_prefix=False`；聊天文本改用 `ctx.raw_text` 去掉前缀。
2. `tools.py` + `bili_scroll.py`：B 站动作结果改成「✅ 成功 / ⚠️ 不确定 / ❌ 失败」三类回执，
   并要求模型不得把未确认讲成已完成（14:02–14:04 的 receipt/verify）。
3. `bili_extra.py` + `bili_auto.py`：评论账本、`my_comments`、`delete_comment/delete_recent/
   delete_by_index/delete_by_text`、`verify_comment/verify_like/verify_favorite/verify_coin`、
   `react()`；`bili_auto` 把 `bot/bvid` 传下去做回执。
4. `bili_scroll.py`：刷完自动 like/fav/coin（模型按口味判断）、`my_comments` 查询；
   `intent.py` + `admin_cmds.py`：`delcomment` 删除评论动作。
5. `config.py` 新增 `bili_scroll_react_*`，默认 **True + 每日上限 0（不限）**；
   `bili_auto_enabled`、`admin_intent_enabled` 默认 **True**，额度 0（不限）。

### D. 双方冲突（79）
- **代差型（约 47 个 `app/pawbot` + 核心壳）**：live 是 10-25 安装基线 + 手工补丁，本地是
  10-30/11-02 的 DataRoot/HA/MC 版本再合并 11-03。本地是功能与安全超集 → **不反向覆盖**。
- **云端当天行为改进**：`//` 前缀、三类回执 → 可安全合并（本轮已合）。
- **云端写/删/自动互动**：当时与本地 `bili_gate`、fail-closed 默认值冲突 → **不合并，列阻断**。
  19:30 后本地已改为云端等价默认（全开、0=不限），但写仍只经 `bili_gate` 授权（见 §9、§10）。
- **插件壳**：本地 `index.js`(77,044B)/`client.js`(238,110B)/`lib/napcat.js`/`webui-proxy.js`
  比 live 新（同步分页版），live 跑的是 11-02 版 → 部署层决定，本轮不动。

## 3. 本轮实际修改

### 3.1 本地（未提交，保留可复核 diff）
| 文件 | 改动 | 目的 |
|---|---|---|
| `app/pawbot/config.py` | 指令前缀 `["/","!"]`→`["//"]`，`allow_no_prefix`→False | 与云端/主人 11-03 要求一致；减少误触发 |
| `app/pawbot/core.py` | 未匹配命令的 `//` 消息剥前缀再交给聊天 | 聊天里不再出现 `//` |
| `app/plugins/ai_chat.py` | 用 `ctx.raw_text`（已剥前缀）而非 `ctx.plain` | 同上 |
| `app/plugins/admin_cmds.py` | 帮助文案改为 `//` 指令 | 用户可见行为一致 |
| `app/pawbot/tools.py` | B 站结果分 ✅/⚠️/❌，附「别编成功」约束；只读结果仍走 `untrusted` | 与云端回执语义一致，且保留本地外部内容隔离 |
| `app/tools/test_command_prefix.py` | 新增 4 个离线测试 | 锁住 `//` 规则 |
| `app/tools/test_bili_action_result.py` | 新增 3 个离线测试 | 锁住只读包装 + 失败/不确定措辞 |

明确未动：`app/data/*`（6 个入口仍为原有改动，未 `git add`）、`bili_gate.py`、`untrusted.py`、
`policy.py`、`paths.py`、`runtime_lock.py`、`lifecycle.py`、`bili_auto.py`、`bili_extra.py`、
`bili_scroll.py`、`intent.py`。

> 上述「未动」只记录 15:00 那一轮的状态；§7、§8 的后续轮次已继续修改
> `bili_gate.py`、`bili_scroll.py`、`tools.py`、`basic.py` 等文件，以 §7/§8 为准。

### 3.2 云端 staging（历史：15:00 的 fail-closed 补丁，未发布）
只改 `app/pawbot/config.py`，把高风险默认值改成 fail-closed。这是当时写入 staging 的
一次性实验，**没有发布，也没有回写本地**；本地代码在 19:30 后已按云端等价改为
**默认全开、0=不限**（见 §9、§10）。当时写入 staging 的值：

| 键 | 改前 | 改后 |
|---|---|---|
| `bili_auto_enabled` | True | **False** |
| `bili_auto_interval_min` | 10 | 30 |
| `bili_auto_daily_limit` | 0（不限） | 20 |
| `bili_auto_reply_limit` | 0 | 5 |
| `bili_auto_comment_limit` | 0 | 2 |
| `bili_auto_danmaku_limit` | 0 | 2 |
| `admin_intent_enabled` | True | **False** |
| `bili_scroll_react_like/fav/coin` | True | **False** |
| `bili_scroll_react_*_daily` | 0（不限） | 5 / 2 / 1 |

- 新 SHA-256：`6901265479bb66960b303565a2f25b9ddb2fedbbdc2576a0e1bf6fcb03c64df0`
- `check` → `python_ast` 通过；`diff` 仅此 1 个文件；`production_modified: false`。
- 未发布、未重启；staging 与 baseline 的差异只有这一个文件。
- **2026-11-04 校正**：上表只是当时 staging 的一次性补丁。本地代码当前是
  默认全开、额度 0=不限、`REJECTED_ACTIONS` 为空（§10），不要拿这张表当现状。

## 4. 验证

语法：`py_compile`（本地 5 个改动文件）通过；staging `config.py` AST 通过。

Python（本地 venv，离线）：
| 测试 | 结果 |
|---|---|
| `test_command_prefix.py`（新） | 4 OK |
| `test_bili_action_result.py`（新） | 3 OK |
| `test_account.py` | 30 OK |
| `test_bili_scroll_report.py` | 10 OK |
| `test_context_hints.py` | 4 OK |
| `test_whitelist.py` | 11 OK |
| `test_llm_options.py` | 11 OK |
| `test_runtime_lifecycle.py`（DataRoot/锁/停机） | 56 OK |
| `test_shutdown_admission.py`（需 `PYTHONPATH=app`） | 7 OK |

Node：
| 测试 | 结果 |
|---|---|
| `test/instance-lock-offline.mjs` | 17 passed |
| `test/data-root-offline.mjs` | 16 passed |
| `test/client-meta-check.mjs` | 31 passed / **1 failed（改动前后相同）** |

原有失败（基线就有，与本轮无关）：
- `client-meta-check`：`config 每个 key 都有 metadata`——缺 `tools.bili_write_*`、
  `tools.bili_learning_*`、`selfedit.*`、`judge.decider_*`、`server.relay_*` 等 30+ 个 key 的客户端元数据。
- `test_shutdown_admission.py` 直接 `python tools/...` 会 `ModuleNotFoundError: pawbot`，需
  `PYTHONPATH=app`（脚本自身没插 sys.path）。

## 5. 阻断项（需用户决定，未假装一致）

1. **完整安全运行时对齐（最高优先级）**：staging/live 没有 `paths/runtime_lock/lifecycle/policy/
   untrusted/bili_gate/bili_learning` 等 24 个模块，`run.py` 仍是 3,192B 旧启动器。补齐是
   ~500KB 的整包部署级变更（还会替换 core/config/agent），必须走「停写 → 冷备 `app\data` →
   整包同步 → 重启 → 校验 run.log 落点」，超出本会话「只写 staging、禁止重启」的边界。
   在完成前，**云端与本地不是功能等价**。
2. **生产配置可能覆盖默认值**：§3.2 的 staging 补丁只是当时的一次性 fail-closed 实验，
   本地代码当前默认全开、0=不限（见 §10）。live 的 `app/data/config.json`
   （14,509B，13:54 修改）可能显式写着 `bili_auto_enabled=true` / `admin_intent_enabled=true` /
   `react=true`。本轮未读、未改 app/data；要真正生效必须单独审计并改配置（不是代码）。
3. **删除评论 / 自动互动 / QQ 派活在云端代码里没有运行时闸门**：只有默认开关和额度。
   `delete_*`、`react()` 一旦被指令触发仍会执行；本地有 `bili_gate` 的进程内凭证与预占/结算，
   云端没有。要合并这些功能，必须先设计同等 gate，不能只改默认值。
4. **凭证与 `.before-*` 位置**：`app/pawbot/bili_mcp/bili_credential.json` 和 32 个 `.before-*`
   仍在代码树内，未清理（避免误删）。
5. **插件壳未对齐**：本地同步分页版 `index.js/client.js` 没有部署到云端任何一处；云端 HMR/配置
   漂移（`cordis.patch.yml` 指向 `C:\pawbot\pawbot-1.0.0` + 停更的 `pawbot-data`）仍会在下次重启时生效。
6. ~~低权限 live 读仍被 ACL 阻断~~（**已过时，见 §8**）：`live_read/live_list/live_status`
   现在低权只读可用；但仍是只读，不能写生产、不能读运行时数据。

## 6. 复核命令

```powershell
cd D:\WorkBuddy\pawbot-dsh
git status --porcelain=v1            # 只应看到 app/data 旧改动 + 本轮代码/测试/文档改动（见 §7.2、§8）
git diff -- app/pawbot app/plugins   # 可复核 diff
python app\tools\test_command_prefix.py
python app\tools\test_bili_action_result.py
```

云端 staging：

```text
pawbot_cloud_code diff  app/pawbot/config.py
pawbot_cloud_code check app/pawbot/config.py
```

矩阵/清单原始数据：`D:\whale\tmp\pawbot-align-20261103\{manifest-local.tsv,manifest-live.tsv,matrix.txt,diffs\}`。

## 7. 本轮（16:30–17:00）逐项对齐：只移植可证明安全的部分

本轮只改本地代码/测试/文档。live 仅用 `pawbot_cloud_code live_read` 只读核对；
没有写云端、没有重启、没有 push、没有碰 `app/data`，也没有发任何真实 B 站/QQ 请求。

### 7.1 核对到的 live 源（只读，SHA-256 来自桥）

| 文件 | live SHA-256 | 本轮结论 |
|---|---|---|
| `app/pawbot/bili_extra.py` | `0edd6f50…4d07ae` | 评论账本 / `my_comments` / `delete_*` / `like/coin/favorite` / `react()` / `verify_*` |
| `app/pawbot/bili_auto.py` | `c7f143c8…a9ae97` | 仍**直连** `bili_extra` 写；默认 True、额度 0=不限 |
| `app/pawbot/bili_scroll.py` | `817b1cb9…ca97f50` | `action()` 里 `comment` 后调 `verify_comment`；`my_comments`；刷完 `react`；无 `bili_gate` |
| `app/pawbot/config.py` | `2f86d253…d7456b2` | `bili_auto_enabled=True`、`admin_intent_enabled=True`、`react_*` 全 True 且 `*_daily=0` |
| `app/pawbot/intent.py` | `7705fb32…011dc3` | 新增 `delcomment`；数量上限被去掉（`max(1, n)`） |
| `app/plugins/admin_cmds.py` | `7eb53865…c75e8de` | `delcomment` 指令；`_fmt_react` / `_fmt_delete`；写动作仍裸调 |
| `app/pawbot/tools.py` | `2264b557…1a0503` | 本轮只读抽查；B 站回执/措辞已在上一轮合入本地，本轮没有新增可复用写入口 |

> live 的 `app/pawbot` 只有 78 个文件，没有 `paths/runtime_lock/lifecycle/policy/
> untrusted/bili_gate/bili_learning/identity/HA` 等模块；本地仍是安全运行时超集。
> 因此**本地与云端不是功能等价**，只是把云端 11-03 的纯交互/回执语义按本地闸门重写。

### 7.2 已安全移植 / 重写

| 本地文件 | 改动 | 安全边界 |
|---|---|---|
| `app/pawbot/bili_extra.py` | 新增 `comment_receipt` / `danmaku_receipt` / `verify_comment` / `verify_danmaku` / `_first_id` | 只读核验；任何异常/没凭证都回 False，绝不据此说成功 |
| `app/pawbot/bili_gate.py` | 写动作扩到 `reply/dynamic/comment/send_danmaku`；新增 `downgrade()`；新增 `REJECTED_ACTIONS` + `refusal()` | 评论/弹幕仍要字面 True + 进程内凭证 + 每日额度；`danmaku` 读动作与 `send_danmaku` 写动作分离，避免读写撞名 |
| `app/pawbot/bili_scroll.py` | `action()` 支持 `send_danmaku`/`comment`；发布后 `verify_*`，核验不到就 `downgrade` 为 unconfirmed；拒绝动作在碰后端前返回 | 唯一写入口仍是 `action()`；`bili_extra` 只做后端调用，不绕过闸门 |
| `app/pawbot/bili_auto.py` | 删除所有 `bili_extra.reply_comment/comment_video/send_danmaku` 直连；改为 `bili_scroll.action(..., grant/grant_factory)`（当时曾改默认全关、额度 0=不发；2026-11-04 已改回默认全开、0=不限，见 §10） | 定时任务先领 `SchedulerCapability`，每次写前换一次性 grant；没有 grant 连读都不做 |
| `app/pawbot/intent.py` | `run(..., ctx=)` 用主人真实消息换 grant；`auto` 传 grant 工厂；`danmaku`/`comment` 走 `bili_scroll.action`；`delcomment` 走同一套删除闸门 | 数量 `max(1, n)` 不设上限（云端等价）；删除只删自己的评论，与其它写一样要凭证/额度/审计 |
| `app/plugins/admin_cmds.py` | `//评论` / `//发弹幕` / `//互动` 先 `owner_turn_grant`；白名单只能指挥只读/刷片/学评论；`//评论` 的写回执按 ✅/⚠️/❌ 汇报 | 非主人（含白名单）**不能代发**；没有凭证时不进入写路径 |
| `app/pawbot/config.py` | `bili_auto_*` / `bili_write_comment/danmaku` 当时曾改成默认 False；2026-11-04 已改回默认全 True、`bili_write_quota` 默认 `{}`（0=不限）；注释保留「不再直连 bili_extra」 | 默认与云端等价；额度 0 = 不限，靠逐动作开关 + 凭证 + 审计兜底 |
| `client.js` | 补齐 38 个缺失 metadata（写入闸门/学习/自愈/中继/评审 + 新增两键），并让 API 分组覆盖 `judge.decider_*` secret | `test/client-meta-check.mjs` 从 1 failed 变 0 failed（361→399 项） |
| `test/client-meta-check.mjs` | 计数断言 361→399 | 元数据确实新增 38 项，不是放宽检查 |

> 上表是 17:00 那轮的记录；其中「默认值 / 额度 / 拒绝动作」已被后续轮次改写。
> 当前真实状态（默认全开、0=不限、`REJECTED_ACTIONS` 为空）见 §10。

三态回执在本轮的落点：
- ✅ `ok: True` 只有在 **gate confirmed + 发布后核验通过** 时才返回；
- ⚠️ `unconfirmed`：回执没有 rpid/dmid，或发布后评论区/弹幕列表查不到；
- ❌ `ok: False` 且无 `unconfirmed`：闸门拒绝、没有凭证、后端明确失败。
- `tools._t_bili_action` 与 `admin_cmds._report/_report_tasks` 都只按这三个状态措辞，
  并在 ⚠️/❌ 时明确写「别当成已经发出去了」。

### 7.3 明确拒绝、不假装等价的功能

> **2026-11-03 19:30 与 2026-11-04 收尾已覆盖本节**：19:30 决定保留云端原有
> 删除评论 / 点赞 / 收藏 / 投币写功能，改成「逐动作授权」；2026-11-04 收尾又把
> 默认值校正为与云端等价（全开、0=不限），`bili_gate.REJECTED_ACTIONS` 为空。
> 下表是 17:00 那轮「全部拒绝」的历史记录，**不代表现状**；当前状态见 §10。

| 云端功能 | 17:00 当时 | 现在（2026-11-04，见 §10） |
|---|---|---|
| `delete_comment` / `delete_recent` / `delete_by_index` / `delete_by_text` / `delete_on_video` | `REJECTED_ACTIONS` 明确拒绝；`bili_extra` 没有这些函数 | 全部开放；单条删除先核实评论作者是凭证账户，批量入口只删账本/公开评论区里自己的评论；仍逐动作开关 + 凭证 + 审计/额度 |
| `like_video` / `coin_video` / `favorite_video` / `react()` | 明确拒绝；未移植 write 版 | 全部开放；逐动作开关 + 主人当轮绑定动作/目标的凭证 + 审计/额度；`react` 另有 `bili_scroll_react_*` 与账本去重 |
| QQ 自然语言「删评论」 | 当场回绝，从任务里移除 | `intent.delcomment` 走同一套删除闸门；数量 `max(1, n)` 不设上限，删除只动自己的评论 |
| 云端 `bili_auto_enabled/admin_intent_enabled=True`、额度 0=不限 | 本地保持 False / 正整数额度 | 本地也已改为默认 True、额度 0=不限；仍靠 `bili_gate` 的逐动作凭证约束 |
| 云端 `intent` 去掉数量上限 | 本地保留 1–20 夹取 | 本地也不设上限（`max(1, n)`，云端等价） |

**结论（2026-11-04 校正）：** 「看评论 / 发评论 / 发弹幕 / 回执核验」与删除、点赞、
收藏、投币、`react` 都已接入本地 `bili_gate` 单一写入口；默认值不再 fail-closed，
安全性由「逐动作开关 + 主人当轮绑定动作/目标的进程内凭证 + 审计 + 去重 + 额度」
承担，而不是靠默认关闭。云端 live 仍没有 `bili_gate` 等安全模块，所以**两边仍不是
功能等价**（本地是安全运行时超集），也不应宣称等价。

### 7.4 本轮验证（全部离线）

Python（`app/.venv`，无网络、无真实凭证、`app/data` 未参与）：

| 命令 | 结果 |
|---|---|
| `python -m unittest tools.test_bili_gate_alignment` | 18 OK |
| `python -m unittest tools.test_bili_auto_gate` | 13 OK |
| `python -m unittest tools.test_command_prefix tools.test_bili_action_result tools.test_bili_scroll_report tools.test_account tools.test_whitelist` | 58 OK |
| `python -m unittest tools.test_runtime_lifecycle tools.test_bili_post` | 73 OK |
| `PYTHONPATH=app python -m unittest tools.test_shutdown_admission` | 7 OK |

Node：

| 命令 | 结果 |
|---|---|
| `node test/client-meta-check.mjs` | 32 passed / **0 failed**（本轮前 31/1，缺 38 个 metadata） |
| `node test/instance-lock-offline.mjs` | 17 passed |
| `node test/data-root-offline.mjs` | 16 passed |
| `node test/architecture-baseline.mjs` | 34 passed / **9 failed（已用干净 HEAD 导出复核：同样 34/9，基线既有）**。9 个失败**全部是“成功路径”断言**（它们要求 `scripts/architecture-baseline.mjs` 退出 0）：`--json` 19/19、默认 cwd、人类输出、`--json --out`、`--root=/--out=`、同路径二次运行、代理 import 写法等价、数值表达式等价、config 改写等价。脚本本身在当前 HEAD 返回 **17/19**，真正失败的契约检查只有两项：`routes.pawbot-ui.subpaths` 与 `routes.pawbot-ui.methods`——`index.js` 比冻结契约多注册了 `stop` 和 `sync/scp/status|pull|push`。不是“9 项各自路由失败”，根因是这 2 项契约检查；`index.js` 本轮未改 |

新测试覆盖的关键断言：
- 没有 `bili_jct` / 没有 gate 开关 / 没有进程内 grant / 非主人（含白名单）→ 后端调用次数为 0；
- 回执没有 rpid/dmid → `unconfirmed`，不返回 ✅；
- 发布后复核失败 → 从 confirmed **降级**为 unconfirmed，且当天同内容不再重发；
- `bili_auto` 所有写只经 `bili_scroll.action`，`bili_extra` 写函数零调用；
- `delete_*` / `like` / `coin` / `favorite` / `react` 是显式拒绝，不是「未知动作」；
- `intent` 的 `delcomment` 回绝且不调用任何删除函数。

### 7.5 仍未对齐 / 需要用户决定

1. 云端 live 仍没有 `paths/runtime_lock/lifecycle/policy/untrusted/bili_gate/bili_learning/identity/HA`
   等安全模块；本轮没有写生产、没有部署，云端仍不是本地安全超集。
2. 云端 `app/data/config.json` 可能显式写着 `bili_auto_enabled=true` /
   `admin_intent_enabled=true` / `bili_scroll_react_*=true`；本轮按约束**没有读/改** `app/data`。
   要真正生效必须单独审计并改配置，而且改完仍会被本地 gate 拦住写动作。
3. 插件壳（`index.js/client.js`）仍只在本地更新；没有部署到云端，也没有重启。
4. `test/architecture-baseline.mjs` 的 9 个失败是当前 HEAD 的既有基线；这 9 个都是
   「成功路径」断言（要求基线脚本 exit 0），真正失败的契约检查只有 2 项：
   `routes.pawbot-ui.subpaths` / `routes.pawbot-ui.methods`（`index.js` 多注册了 `stop`、
   `sync/scp/*`）。本轮没有改 `/pawbot-ui` 路由，未修这些失败，也不宣称已修。

## 8. 本轮（17:00–17:30）离线修正：四个实际缺陷 + 文档纠偏

本轮仍**只改本地代码/测试/文档**：没有写 `app/data`、没有写云端 staging/live、没有
restart/publish/push，也没有发任何真实 B 站/QQ 请求。live 只用低权
`live_status` / `live_list` / `live_read` 只读核对。

### 8.1 修掉的问题（每项都有离线测试）

| 文件 | 问题（可复现） | 修法 | 测试 |
|---|---|---|---|
| `app/plugins/basic.py` | `//help` 仍打印 `/ping`；用户照帮助发 `/ping` 现在只是普通聊天 | 帮助前缀改读 `bot.command_prefixes`（缺省 `//`、配置异常也回退 `//`），不再写死 `/` | `test_command_prefix.HelpPrefixTest`（3 个） |
| `app/pawbot/bili_gate.py` | 发弹幕被拒时提示 `tools.bili_write_send_danmaku`（不存在的键），照提示改永远改不对 | 拒绝文案经 `_CONFIG_KEY` 映射到真实键 `tools.bili_write_danmaku` | `test_bili_gate_alignment.test_danmaku_denial_names_the_real_config_key` |
| `app/pawbot/bili_scroll.py` | `float(参.get("progress") or 5.0)` 把合法的 `progress=0` 吞成第 5 秒 | 只有 `progress is None` 才用 5.0；`0` 原样传给 `bili_extra.send_danmaku` | `test_danmaku_progress_zero_is_kept`、`test_danmaku_default_progress_when_absent` |
| `app/pawbot/tools.py` | 写动作把后端 `data`/错误正文裸着交给模型，与只读出口的 untrusted 围栏不一致 | 新增 `_bili_external_receipt()`：回执正文走同一套 `untrusted.wrap_external`（伪围栏标记会被消毒）；`✅ 成功` / `⚠️ 不确定` / `❌ 没成` 与「别编」提示留在围栏外 | `test_bili_action_result.BiliWriteReceiptIsolationTest`（3 个） |

三态语义没有被围栏吃掉：状态标记、以及我们自己写的 `hint`（不是外部数据）仍在围栏外；
回执里的外部文本（含错误页/服务端消息）在围栏内，模型只能当数据看。

### 8.2 产品边界：视频评论 / 发弹幕不直接给模型调用（未改 allowlist）

`tools.BILI_TEXT_WRITE_ACTIONS` 仍是 `{"reply", "dynamic"}`，`tools._bili_allowed_actions()`
= B 站只读动作 ∪ 这两个。因此 `comment`（视频评论）和 `send_danmaku`（发弹幕）
**不在模型可直接调用的 `bili_action` 动作集合里**：只能由主人 `//评论` / `//发弹幕`
指令、或受控 `bili_auto` 经 `bili_scroll.action()` 发起，而且都要过 `bili_gate`
（字面 True + 进程内凭证 + 每日额度 + 发布后核验）。

这是本轮确认的**产品边界，不是遗漏**：本轮没有把这两个动作加进 allowlist；后续若要开放，
必须单独评审写闸门与授权路径，不能顺手打开。

### 8.3 本轮验证（全部离线）

Python（`app/.venv`，无网络、无真实凭证、`app/data` 未参与）：

| 命令 | 结果 |
|---|---|
| `python -m unittest tools.test_command_prefix tools.test_bili_gate_alignment tools.test_bili_action_result` | **34 OK** |
| `python -m unittest tools.test_bili_scroll_report tools.test_bili_auto_gate` | **23 OK** |
| `python -m py_compile`（`bili_gate.py` / `bili_scroll.py` / `tools.py` / `basic.py`） | exit 0 |

Node：

| 命令 | 结果 |
|---|---|
| `node test/client-meta-check.mjs` | **32 passed / 0 failed** |
| `node test/architecture-baseline.mjs` | 34 passed / 9 failed（与 §7.4 一致；干净 HEAD 导出同样 34/9，根因仍是 2 项 `/pawbot-ui` 契约检查，本轮未修） |

### 8.4 仍然不能宣称对齐（保留警告）

- 云端 live 的 242 个代码文件里仍没有 `bili_gate` / `untrusted` / `policy` / `paths` /
  `runtime_lock` / `lifecycle` / `identity` / HA 等安全模块；staging 是从 live 复制的基线 +
  单文件 patch，同样没有。本地是安全运行时超集。
- **staging 与 live 仍不是同一份**，也都没有本轮（§8.1）的四个修正；本轮没有部署、
  没有发布，所以**不能宣称云端与本地功能/安全对齐**。
- 云端生产配置可能仍显式开着 `bili_auto_*` / `admin_intent_*` / `bili_scroll_react_*`
  （见 §5.2）；本轮按约束未读未改 `app/data`。
- 未做：publish、restart、git push、`git add -A`、真实 B 站写操作、真实 QQ 消息。

## 9. 本轮（19:30–20:00）按用户最新决定：逐动作授权，保留云端写功能

用户最新明确选择：**只统一核心运行代码**，保留云端原有的评论删除 / 点赞 / 收藏 /
投币写功能，但每一类都必须单独开关、单独额度、单独授权。本节覆盖 §7.3 的「全拒绝」。

### 9.1 单一写入口与新动作（`bili_gate.py` / `bili_scroll.py` / `bili_extra.py`）

- 写动作集合：`reply` / `dynamic` / `comment` / `send_danmaku`（原有）+
  **`like` / `favorite` / `coin` / `delete_comment`**（新增）。全部只经
  `bili_scroll.action()` → `bili_gate.guard()`；`bili_extra` 仍只做后端调用，
  任何插件/工具/定时任务都不直连它的写函数（已用 `Select-String` 复核）。
- `bili_extra.py` 新增：`like_video` / `coin_video`（`like=False`，**投币不顺手点赞**）/
  `favorite_video`（收藏夹不存在才新建，且只在该动作过闸后）/ `delete_comment(oid, rpid)`
  单条删除；`comment_state`（评论作者/存在性三态）/ `my_mid` / `scan_my_comments` /
  `verify_like` / `verify_coin` / `verify_favorite`。**没有** `delete_recent` /
  `delete_by_index` / `delete_by_text` / `delete_on_video` / `react`。
- `delete_comment` 流程：`gate.authorize()` 无副作用预检 → `comment_state()` 核实
  **凭证账户 mid == 评论实际作者** → `guard()` 预留 → 删除 → 再查一次确认「真的没了」；
  作者不是自己 / 查不到作者 / 身份为空 → **拒绝且不碰删除后端、不扣额度**。
- 点赞/收藏/投币/删除属于 `VERIFY_ONLY_ACTIONS`：`settle(verified=...)` 只认读后核验，
  后端回执再「成功」也不算；投币特别要求**这次多出来的币**（先读一次币数，读不到就不投）。

### 9.2 授权模型（本次最重要的收紧）

- `WriteGrant` 现在**绑定一个动作 + 一个目标**：`owner_turn_grant(bot, ctx, action=,
  target=)` / `scheduler_grant(cap, action=, target=)`；`guard()` 逐项核对，换动作或
  换 BV 号/rpid 都返回 `not_authorized`，不能跨动作复用。
- 凭证来源仍只认「account.owner_ids 的真实 message 事件」或受控定时任务能力；
  dict / 字符串 / 通知事件 / 非主人（含白名单）都换不到。
- **投币和删除永不由 scheduler 授权**：`SCHEDULER_ACTIONS` 只有 reply/dynamic/
  comment/send_danmaku；`scheduler_grant()` 直接返回 None，`guard()` 也会再拦一次。
- 自然语言派活（`intent.TASK_ORDER`）含 `delcomment` / `danmaku` / `comment`，
  但没有 like / coin / favorite；删除只动自己的评论，且要过与显式指令同一套
  `bili_gate`（动作+目标绑定）。`bili_auto` 仍只回评论/发评论/发弹幕。
- `scheduler_grant` 预算从「一轮一次」改为「每条写单独签」：`bili_scroll` 自动回一条时
  按 `(reply, BV|rpid)` 逐条现签；`bili_post` 固定 `(dynamic, "dynamic")`。

### 9.3 配置 / QQ / 工具 / 面板

- `config.py` 新增 `tools.bili_write_like/favorite/coin/delete_comment`；**2026-11-04
  校正为与云端等价：所有 `bili_write_*` 默认 True，`bili_write_quota` 默认 `{}`
  （0 / 不填 = 不限）**。`_quota()` 仍对负数 / 布尔 / 字符串 / 非对象报配置错
  （`GateError`），0 表示不限。
- QQ 显式指令新增 `//点赞` / `//收藏` / `//投币 [1|2]` / `//删评论 [BV号|序号|关键词|全部]` /
  `//我的评论 [BV号]`；`//删评论` 不带参数先列出自己最近发过的评论，只删自己的。
  `//权限` 帮助同步（2026-11-04 已恢复批量删除入口）。
- 工具展示：`bili_action` 的 enum 增加 `like` / `favorite` / `my_comments`；
  **`coin` 与 `delete_comment` 不在 enum 里**（模型不能自己决定花硬币或删东西），
  描述里写明投币/删除只能由主人显式指令。点赞/收藏各自要求「本动作」的明确措辞
  （`_BILI_LIKE_CUES` / `_BILI_FAVORITE_CUES`），不能借「评论一下」顺带授权。
- `client.js` 补齐 4 个新开关的 metadata（高级 325→329，总 399→403）；
  `test/client-meta-check.mjs` 计数同步，**32 passed / 0 failed**。

### 9.4 本轮验证（全部离线，无真实 B 站/QQ 请求）

| 命令 | 结果 |
|---|---|
| `python -m unittest tools.test_bili_write_actions`（新，34 个） | OK |
| `python -m unittest tools.test_bili_gate_alignment tools.test_bili_auto_gate tools.test_bili_action_result tools.test_bili_write_actions` | **80 OK** |
| `… + tools.test_bili_post tools.test_bili_scroll_report` | **101 OK** |
| `python -m unittest tools.test_command_prefix tools.test_bili_scroll_report tools.test_account tools.test_whitelist tools.test_llm_options tools.test_bili_post` | 86 OK |
| `python -m unittest tools.test_runtime_lifecycle` | 56 OK |
| `PYTHONPATH=app python -m unittest tools.test_shutdown_admission` | 7 OK |
| `node test/client-meta-check.mjs` | 32 passed / 0 failed |
| `node test/instance-lock-offline.mjs` / `test/data-root-offline.mjs` | 17 / 16 passed |
| `node test/architecture-baseline.mjs` | 34 passed / 9 failed（与 §8.4 相同；既有基线） |

新测试用临时目录 + AsyncMock 假传输，覆盖：默认关闭、非主人/白名单不可写、
授权动作或目标不符、过期/重复授权、0/负数/布尔/字符串额度拒绝、删除他人评论拒绝、
作者/身份查不清拒绝、未核验（含超时、无 delta、删除后查不到）一律 unconfirmed 且
登记额度不自动重试、正常授权单次成功、scheduler 不能授权投币/删除、
自然语言「互动」不隐式授权投币/删除、模型工具看不到 coin/delete。

### 9.5 仍未完成 / 明确边界

- 云端 live / staging **完全没有改动**，也没有读 `app/data`、没有重启/publish/push/
  `git add`；云端仍没有 `bili_gate` 等安全模块，因此两端仍不是功能等价。
- 投币的「这次多出来的币」依赖 `archive/coins` 的 `multiply` 计数：如果账号在这条视频
  上已经投满 2 个，B 站不会再让投，此时会落成 unconfirmed（不会谎报成功）。
- 删除只支持**顶层视频评论**（按 rpid 在前 3 页里核实）；楼中楼回复/超出前 3 页的评论
  会在身份核实阶段被拒绝（安全侧偏保守，未做更深的翻页扫描）。
- `favorite` 在目标收藏夹不存在时**会新建**「大肥鱼觉得好的」——这是云端原有行为，
  发生在此动作已过闸之后；读后核验只用 `create=False`，不会借核验再写。
- 云端生产 `app/data/config.json` 可能仍写着旧默认值；本轮按约束未读未改。
  即使打开开关，没有主人当轮绑定动作+目标的凭证仍然写不出去。
- `//我的评论` 只读、依赖凭证里的 `DedeUserID`；没有该字段就返回错误，不猜身份。


## 10. 2026-11-04 收尾：当前真实状态 + 人味守门

本轮只改本地代码/测试/文档：没有写 `app/data`、没有 commit/push/部署/重启，
没有联网、没有发真实 B 站/QQ；测试全部离线（`app/.venv`）。

### 10.1 当前默认值（以此为准）

| 配置 | 当前值 | 说明 |
|---|---|---|
| `tools.bili_auto_enabled` / `_reply` / `_comment` / `_danmaku` | `True` | 自动互动默认开；每次写仍过 `bili_gate` |
| `tools.bili_auto_daily_limit` / `*_limit` | `0` | 0 = **不限** |
| `tools.admin_intent_enabled` | `True` | QQ 自然语言派活默认开 |
| `tools.bili_write_enabled` 与各 `tools.bili_write_<动作>` | `True` | 逐动作开关，字面 `True` 才放行 |
| `tools.bili_write_quota` | `{}` | 0 / 不填 = 不限；负数/布尔/字符串仍报配置错 |
| `bili_gate.REJECTED_ACTIONS` | `{}`（空） | 删除/点赞/收藏/投币/react 不再永久拒绝 |
| `bili_gate.ALWAYS_DENIED` | 5 项 | 投稿上传、私信、专栏/图文、登录仍永久拒绝 |
| `tools.bili_post_enabled` | `False` | 自动发动态是独立开关；不配时间也不跑 |

删除/互动仍不是「打开就随便用」：每次写都要主人当轮真实消息换来的、**绑定动作 + 目标**
的进程内凭证，另加审计、去重与额度。`intent.TASK_ORDER` 现在含 `delcomment`；
`//删评论` 支持「BV号 / 序号 / 关键词 / 全部」，只删自己的评论。

### 10.2 人设 / 反公式化 / 实时上下文（`app/pawbot/bili_voice.py`）

- **生成前**：人设正文 + 视频摘要 + **实时**评论树（含楼中楼，只在内存用、不落盘）+
  她最近说过的话（作为「别说这些」）。
- **生成后硬检查**：近期用过的开头 2~4 字、口头禅/表情、`草` 每日上限、
  与最近自发言/同视频历史的相似度、长度越界、AI 腔；不过就带更强制约重写
  （`tools.bili_voice_max_retry`，默认 1 次），仍不过就放弃这条，不硬发。
- 同一套检查也用于模型自己写的 `reply` / `dynamic`（`tools.bili_action`）与自动动态
  （`bili_post.compose`）；确认发出后 `note_sent` 同时写进程内缓存 + 动作账本，
  同一个进程里紧接着的下一条马上能去重。
- 视频摘要按 bvid 幂等写 `content/bili_summaries/<bvid>.md`（已存在不覆盖）；
  评论原文不缓存、不写磁盘。
- 新增 6 个配置键：`bili_voice_enabled`、`bili_voice_recent_n`、`bili_voice_max_retry`、
  `bili_voice_similarity`、`bili_voice_ban_openers_days`、`bili_voice_max_cao_per_day`。

### 10.3 2026-11-02 截止口径

- 只统计 `ctime`/`ts >= 2026-11-02 00:00`（本地时区）**她自己的**发言；更早的是主人
  自己发的，既不算风格基线，也不算「最近说过的话」。没有时间字段的记录跳过。
- 「草」的每日上限按**当天全部发言**算，不受 `recent_n` 截断影响；开头/口头禅/相似度
  仍只看最近 `recent_n` 条。
- 这条口径同时适用于本地评论账本、自动互动动作账本与本进程内刚发出的缓存。

### 10.4 开关语义与记账（2026-11-04 统一）

- `tools.bili_voice_enabled`（默认开）是**全部检查路径**的总开关：关掉后
  ① 自动互动生成、② 模型写正文、③ 自动动态三条路径都不查，退回旧写法。
- **记账与闸门去重不受影响**：关掉开关后成功发出仍 `note_sent` 记账，`bili_gate`
  的同内容去重/额度/审计照旧。
- 主人手打原文（`//发弹幕` / `//评论`、自然语言派活的原文）不改写、不过人味检查；
  弹幕实际发送截断到 60 字，**记账和返回都用实际发出的那段**（与 `//发弹幕` 一致）。

### 10.5 本轮验证（全部离线，无真实 B 站/QQ 请求）

| 命令 | 结果 |
|---|---|
| `python -m unittest tools.test_bili_voice tools.test_bili_post` | **52 OK** |
| 全部 bili 相关模块（voice / post / gate_alignment / auto_gate / action_result / write_actions / scroll_report / delivery / watch / reply_quota / proactive_bili） | **260 OK** |
| 上述两个测试文件在**屏蔽 `bilibili_api` 导入**后重跑 | **52 OK** |
| `python -m py_compile`（`tools.py` / `bili_post.py` / `intent.py` / `core.py`） | exit 0 |
| `node test/client-meta-check.mjs` | **32 passed / 0 failed**（430 键：常用 74 + 进阶 356） |

`test_bili_voice.py` / `test_bili_post.py` 里 5 个 `skipUnless(_HAS_BILI_API)` 已移除：
这些用例都 mock 了 B 站后端，不需要可选包。`test_bili_voice` 的实时评论树用例改为在
`sys.modules` 里注入假的 `bilibili_api.comment`，所以在没装该包的环境也能跑。
`test_bili_reply_quota` 仍真实依赖 `bilibili_api`，不在本轮两个文件范围内，未动。

### 10.6 猫娘草稿法（`app/pawbot/draft.py`）

三段式：内容草稿 → 中性草稿 → 完整人设成稿。新增 5 个配置键：

| 配置 | 默认值 | 说明 |
|---|---|---|
| `chat.chat_draft_enabled` | `True` | 总开关；关掉只调用一次模型（原单段路径） |
| `chat.chat_draft_persona` | `你是一只猫娘。` | 第 1 段的最小设定，不是完整人设 |
| `chat.chat_draft_cleanup` | `True` | 第 2 段清理猫娘用词 / 口癖 / 动作描写 |
| `chat.chat_draft_timeout` | `8` | 单段超时秒数；**0 = 单段不限**（两段合计仍受总预算限制） |
| `chat.chat_draft_max_chars` | `300` | 内容草稿与中性草稿各自的字数上限；0 = 默认 300 |

- 两段合计总预算默认最多 12s，且不超过 `chat.reply_timeout` 的 40%；预算用尽或任一段
  失败 / 超时 / 空回复都整条降级，按原单段路径回复。
- 两段调用显式传 `reasoning_effort="low"`（payload 级覆盖），运行态配置写 medium 也按 low。
- 主模型没有 api_key（如 `provider=dsh` 且这两段只走直连）时先判 `chat_available()`，
  不可用就跳过；即使拿到带 `mock` 字样的文本也不会注入最终 system。
- `Agent.run` 返回前调用 `draft.scrub(text, draft_block=...)`，剥掉模型回显的
  「## 本轮内容草稿」块与完整复述的中性草稿原文。

