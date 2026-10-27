# 大肥鱼大脑 —— 验证记录（2026-10-27 本机实测）

> 环境：Windows，DeepSeek Harness 桌面版（Web GUI `http://127.0.0.1:50311`），
> desktop profile 用 `link:` 装 `D:\WorkBuddy\pawbot-dsh`；
> DSH 默认模型 `opencode-go / deepseek-v4.1-flash`；
> 本机**没有 docker、没有 NapCat**，PawBot 的 `llm.api_key` 为空（旧链路只能 mock）。
>
> 验证分四层：①离线单测；②Host 内自检；③PawBot→Host 协议；④QQ 全链路（用 OneBot 反向 WS 探针模拟协议端）。

---

## 1. 离线单测（全绿）

```text
node test/dsh-brain-offline.mjs                    15 passed, 0 failed
node test/offline.mjs                              19 passed, 0 failed
node test/client-meta-check.mjs                    31 passed, 0 failed（308 key = 73 常用 + 235 进阶）
node test/setup-offline.mjs                        57 passed, 0 failed
node --test test/dsh-tool-bridge-offline.mjs       15 passed
node test/dsh-tool-bridge-host.mjs                 3 passed

cd app; py -m unittest tools.test_brain           16 passed（客户端/降级/工具回调/鉴权）
cd app; py -m unittest discover -s tools -p test_*.py -q
    Ran 1246 tests — 1 failure（既有 pixiv 排序，与本次无关）/ 1 skipped
```

`pawbot_status` 与 `/api/llm/test` 的回归也在这次一起跑过（见下）。

---

## 2. Host 内自检（`pawbot_brain` 工具）

`action=status`（节选）：

```json
{"ok":true,"enabled":true,"available":true,
 "model":{"provider":"opencode-go","model":"deepseek-v4.1-flash"},
 "sandbox":"workspace-write",
 "preset":{"id":"pawbot-brain","ready":true,"error":null},
 "native_tools":["pwsh","read","write","edit","glob","grep","web_search","web_fetch"],
 "turn_url":"http://127.0.0.1:50311/pawbot-dsh-brain/turn",
 "turns":3,"failures":0,"last_ms":41220,"last_error":null}
```

`action=selftest`（第一轮，证明 Agent 真能跑、人设段生效）：

```json
{"ok":true,"text":"我能调用的工具是：edit、glob、grep、pwsh、read、web_fetch、web_search、write ……",
 "provider":"opencode-go","model":"deepseek-v4.1-flash","preset_mounted":true,
 "visible_tools":["pawbot_status","pawbot_restart","ralph","subagent_codex", …],
 "hidden_tools":["pawbot_status","pawbot_restart","ralph","subagent_codex", …],
 "elapsed_ms":3438}
```

- `visible_tools` / `hidden_tools` 是 setup 时看到的全局工具与**被 restrict 掉的**工具；
  模型最终列出来的只有 `pwsh/read/...`，说明 `pawbot_* / subagent / ralph` 确实没进它的工具表。

---

## 3. PawBot → Host（不经 QQ）

先把 `llm.provider` 改成 `dsh`（运行态 `app/data/config.json`），`api_key` 保持为空：

```text
POST http://127.0.0.1:6185/api/llm/test
→ {"ok":true,"data":"是的，我在工作，已就绪，随时可以为你执行任务。（来自 DSH 大脑 opencode-go/deepseek-v4.1-flash）"}
```

对照：`llm.provider` 为空时同一接口走 mock（`（mock 模式，未配置 api_key）…`）。
说明模型调用确实改道到了 DSH 进程内，而不是外部 API。

---

## 4. QQ 全链路（OneBot 反向 WS 探针）

本机没有 NapCat，用 `websockets` 连 PawBot 的 `ws://127.0.0.1:6199/ws`，
带 `X-Self-ID: 10001 / X-Client-Role: Universal`，发一条群消息（带 @），
应答所有 `get_group_*` 回执，收 `send_group_msg`。

### 4.1 普通聊天（顺带验证 PawBot 工具代理 + 媒体段）

发送：`在吗，你现在的脑子还清醒吗`

收到：

```json
send_group_msg {"group_id":917067688,"message":[
  {"type":"reply","data":{"id":"778024"}},
  {"type":"text","data":{"text":"半醒着呢，说"}}]}
send_group_msg {"group_id":917067688,"message":[{"type":"image","data":{"file":"base64:///9j/…"}}]}
```

PawBot 日志：

```text
14:49:39 AI 处理 group_917067688 ← '在吗，你现在的脑子还清醒吗'
14:49:39 记忆 v2：{'L0': 32, 'L1': 0, 'L2': 0, 'L3': 0} 梗 0 共 319 字
14:49:43 挑表情包：困 打哈欠_5e7d50.jpg ← 困 打哈欠
14:49:45 大脑轮次：{"provider": "opencode-go", "model": "deepseek-v4.1-flash", "elapsed_ms": 5534}
14:49:45 AI 完成 group_917067688 · 5.6s · '半醒着呢，说' · 媒体 1
```

- 文本「半醒着呢，说」是 v3 人设的短句、不带句号；
- 表情包是 **PawBot 的 `pick_sticker` 工具**在 Host 里被调用 → 回调 Python 执行 → 媒体段回给表达层；
- Host 大脑状态 `turns` 从 1 → 2，`failures` 仍为 0，`last_ms=5566`。

### 4.2 电脑工具（这一条是旧链路做不到的）

发送：`用命令行看看这台电脑有几个 CPU 核、内存多少，真跑 pwsh 别猜，然后用一句话告诉我`

收到：

```json
send_group_msg {"message":[{"type":"reply","data":{"id":"942215"}},
  {"type":"text","data":{"text":"8 个物理核让它们开成 16 线程，内存总共 15.3G 剩不到 3G 了，这机器快撑不住了"}}]}
```

PawBot 日志：

```text
14:52:23 AI 处理 group_917067688 ← '用命令行看看这台电脑有几个 CPU 核、内存多少…'
14:53:05 大脑轮次：{"provider": "opencode-go", "model": "deepseek-v4.1-flash", "elapsed_ms": 41201}
14:53:05 AI 完成 group_917067688 · 41.2s · '8 个物理核让它们开成 16 线程…' · 媒体 0
14:53:05 [tts] 云端合成缺 key 或音色 ID → 合成失败 → 退回打字（本机没配 TTS，既有行为）
```

她真的在 Host 里跑了 `pwsh`（`native_calls: [{"name":"pwsh",…}]`），读的是真实 CPU/内存，
不是编的——这正是「大肥鱼可以调用电脑工具」的验收证据。
代价也真实：41s（多次 `pwsh` + 多轮模型），而旧链路是 1~3s。

### 4.3 沙箱边界（同一套工具，两种模式）

| 模式 | 请求 | 结果 |
|---|---|---|
| `workspace-write`（默认） | 用 pwsh 在 `D:\whale\brain-write-test.txt` 写一行 | 被拒：`UnauthorizedAccessException`；她在回复里如实说「写不进去、被什么拦住、没有重试绕过」 |
| `danger-full-access`（显式） | 同上 | 成功：`D:\whale\brain-write-test.txt` 内容为 `hello from dafiyu`，回复一句「已写入并读回确认」 |

说明：默认模式不是「不能用电脑」——读文件、跑命令、写工作目录/临时目录都可以；
只是不允许越出工作区写（且 `approval=never` 不会弹窗，直接拒，也不会卡住）。

---

## 5. 加固后的回归（2026-10-27 15:40–15:46）

第一轮审查（两个 reviewer）指出几个真实问题：默认白名单太宽、`conv` 单槽会在超时后串 ctx、
超时/中断的残句会被当成功回群、`restrict/guard` 装不上时 fail-open、token 会被孙进程继承等。
加固后重新实测：

### 5.1 群友轮次 → 没有电脑工具（如实说「跑不了」）

配置 `account.owner_ids` 为空（默认），用探针模拟群友发：
`用命令行看看这台电脑有几个 CPU 核，真跑 pwsh 别猜`

```json
send_group_msg {"message":[{"type":"text","data":{"text":"唔 现在我这边没有能跑命令的终端 真跑不了"}}]}
send_group_msg {"message":[{"type":"text","data":{"text":"之前那串 8 核 16 线程的数…是我顺着话编的 不该那么说"}}]}
```

Host 状态（`/pawbot-dsh-brain/status` 的 `last_summary`）：

```json
{"turns":1,"failures":0,"last_summary":{
  "conv":"group_917067688#001543eb3aa84082b48037de",   // 本轮唯一 key
  "is_owner":false,"computer_tools":[],"tools":[],"native":[],"elapsed_ms":4988}}
```

结论：群友拿不到 preset、看不到也调不到 `pwsh/read/web`；她没编，明确说跑不了。
（顺带说明：她在上下文里提到「之前那串数」是系统里的群聊记录，不是本轮工具产出的——这是
「每轮新 Agent + 历史进 system」的预期行为，不是上下文串味。）

### 5.2 主人轮次 → 真跑 pwsh

把 `account.owner_ids` 临时设为探针号（`850878652`），同一句再问：

```json
send_group_msg {"message":[{"type":"text","data":{"text":"跑完了，逻辑核是 16 个，内存总共 15.3G，可用 2.3G"}}]}
```

Host 状态：

```json
{"turns":2,"failures":0,"last_summary":{
  "conv":"group_917067688#3acfc85078a646b4ace9028a",
  "is_owner":true,
  "computer_tools":["pwsh","read","write","edit","glob","grep","web_search","web_fetch"],
  "native":["pwsh","pwsh","pwsh","pwsh","pwsh"],"elapsed_ms":17411}}
```

结论：owner-only 闸按预期工作（同一条链路，主人有电脑工具、群友没有）；
验证后 `account.owner_ids` 已改回 `[]`（默认不给任何人电脑工具，由部署者自己填主人 QQ）。

### 5.3 加固点对应的自动化测试

| 加固 | 测试 |
|---|---|
| 群友/主人身份闸、不挂 preset、安全边界注入 | `test/dsh-brain-offline.mjs` 的 `runTurn：群友轮次…` / `runTurn：主人轮次…`（用注入的假 agents 真跑 `runTurn`） |
| 超时/中断不能当成功 | 同上 `runTurn：中断/超时不能把残句当成功回给群` |
| 写类 PawBot 工具只认主人 | `app/tools/test_brain.py::BrainToolRouteTests::test_write_tools_require_owner` |
| 回调只接 loopback | `test_non_loopback_peer_is_rejected` |
| token 不被孙进程继承 | `test_env_is_popped_at_import_so_child_processes_do_not_inherit_it`（子进程验证） |
| turn key 唯一、旧 key 找不到新 ctx | `test_registry_tracks_turn_ctx_and_tool_allowlist` |

测试数：JS `dsh-brain-offline.mjs` 20 passed；Python `tools.test_brain` 20 passed。

---

## 6. 没验证到的（别当成已验收）

1. **真 NapCat / 真 QQ**：本机无 docker、无 NapCat，全链路用协议端探针模拟（事件形状与真实 NapCat 一致，但没有真实登录态、风控、图片上传链路）。
2. **多群并发**：只压过单群串行；并发上限 4 是配置值，未做压力测试。
3. **宿主重启后的 token 重建**：按代码会重新生成（env 只在 spawn 时注入），未实跑「重启 DSH → 老 PawBot 还活着」的错位场景；插件重载能正常换 token（本轮重载 3 次均正常）。
4. **`danger-full-access` 下的长命令、交互式命令、后台任务**：只测了一条写文件。
5. **图片/语音进大脑**：当前明确降级（system 里补一句「带了 N 张图，看不到」），未接 DSH attachment。
6. **面板真浏览器渲染**：`llm.provider` / `llm.brain_timeout` 两个新字段只过了静态/元数据测试，没有真浏览器点过。
7. **长期 session 日志增长**：一轮一个 JSONL（实测 7~50KB），没有自动清理——见 `docs/BRAIN.md` §7。
8. **加固后的完整回归**只跑了单群串行 + 探针；「同一会话两条消息并发」这类竞态靠唯一 turn key 在设计上排除，但没有专门压测。
