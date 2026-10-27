# 心情矩阵：类 JEV 的概率化决策模型（2026-10-27）

把「收到一条消息 → 心情怎么变」从关键词/普通聊天模型判官，改成调用概率化决策模型：

```text
POST https://openrouter.ai/api/alpha/decisions
model: perplexity/pplx-decider-v1.2
```

请求体就是 JEV 的形态：

```json
{
  "model": "perplexity/pplx-decider-v1.2",
  "state": "角色设定 + 当前情绪 + 最近上下文 + 新消息",
  "questions": {
    "shift":     { "type": "noul",   "instructions": "这条消息会不会让她的情绪产生明显变化？" },
    "emotion":   { "type": "choice", "instructions": "她最可能变成哪种主导情绪？",
                   "criteria": { "开心": "...", "兴奋": "...", "好奇": "...", "烦躁": "...",
                                 "低落": "...", "亲昵": "...", "得意": "...", "疲惫": "...",
                                 "none": "没有明显情绪变化" } },
    "intensity": { "type": "score",  "instructions": "如果情绪有变化，强度大概多强？",
                   "criteria": ["轻微", "明显", "强烈"] }
  }
}
```

响应（真实样例，2026-10-27）：

```json
{"answers": {
  "shift":   {"type":"noul","noul":0.88},
  "emotion": {"type":"choice","choice":"低落","confidence":0.92,
              "probabilities":{"低落":0.927,"开心":0.011,"none":0.010,"...":0}},
  "intensity": {"type":"score","score":0.66,"legend":{"0":"轻微","1":"明显","2":"强烈"}}
}, "usage": {"input_tokens":252,"output_tokens":3,"cost":0.00000504}}
```

## 怎么映射回心情矩阵

`app/pawbot/decider.py`：

1. `shift < emotion.decider_min_shift`（默认 0.5）或 `choice=none` → 返回 `{}`，交给关键词兜底；
2. 否则按 `intensity`（0~1 连续分）给主导情绪加 **4~15** 点，再乘 `decider_weight` 与置信度；
3. 主导是负面的（烦躁/低落）→ 开心、得意各降一半；主导正面的（开心/兴奋/感动/得意/害羞）→ 烦躁、低落降 40%；
4. 其余维度轻微 -1，避免长期全高；
5. `emotion.max_step`（默认 15）仍会兜底裁剪。

真实跑通（本机 2026-10-27）：

```text
群友：今天加班到十点，人都要散架了……（她软糯心软）
决策模型：低落 概率 0.93 / 强度 0.66 / shift 0.88
applied = {低落 +6, 开心 -3, 得意 -3, 兴奋 -1, 惊讶 -1, 烦躁 -1, 感动 -1, 害羞 -1}
```

## 配置（面板「设置 → 高级 → 情绪」，或 `app/data/config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `emotion.decider_enabled` | `false` | 开关；开了才走决策模型 |
| `emotion.decider_model` | `perplexity/pplx-decider-v1.2` | 决策模型 |
| `emotion.decider_base_url` | `https://openrouter.ai/api/alpha/decisions` | 接口 |
| `emotion.decider_api_key` | 空 | 留空＝用环境变量/key 文件（不把 key 写进仓库） |
| `emotion.decider_timeout` | `12` | 单次超时（秒） |
| `emotion.decider_min_shift` | `0.5` | 低于这个「明显变化」概率就不更新 |
| `emotion.decider_weight` | `1.0` | 情绪幅度倍率（0.5 更含蓄 / 1.5 更大起大落） |

失败（没 key / 超时 / HTTP 错误 / 解析失败）不会抛异常：记一条 debug 日志，退回原来的
`emotion.judge_model` → `judge.*` → 关键词，聊天不中断。所以**没网也能正常回话**，只是情绪变回关键词驱动。

## Key 放哪（三选一，按优先级）

1. 面板填 `emotion.decider_api_key`（遮罩保存，写进 `app/data/config.json`——注意这是明文，config 还有别的 key）；
2. 环境变量 `PAWBOT_DECIDER_KEY` / `OPENROUTER_API_KEY`；
3. 专用 key 文件（推荐，升级/重装都不碰）：

```powershell
New-Item -ItemType Directory -Force "$HOME\.pawbot" | Out-Null
Set-Content "$HOME\.pawbot\decider.key" "sk-or-v1-..." -NoNewline
```

也支持 `PAWBOT_DECIDER_KEY_FILE` 指定别的路径。

> 本机已经写到 `C:\Users\Administrator\.pawbot\decider.key`，并在运行态把
> `emotion.decider_enabled` 打开了。

## 调试 / 单独试一句

```powershell
cd D:\WorkBuddy\pawbot-dsh\app
py tools\decider_once.py --text "群友：你今天怎么这么可爱" --context "群里在闲聊" --addressed
```

输出：answers（概率/置信度）、映射后的情绪增减、耗时与花费（usage.cost）。

## 测试

```powershell
cd app
py -m unittest tools.test_decider -q     # 16 条，离线假 HTTP 端点
```

覆盖：请求形状/鉴权、HTTP 500 → `HTTP_500`、缺 key → `NO_KEY`、noul/choice/score 解析、
情绪映射（正/负/阈值/none/权重）、key 解析优先级、失败降级。

## 已知边界

- 每次更新多一次网络往返（实测 1.4~2.5s）；`emotion.update` 在回话路径上，网络差时会把
  `decider_timeout` 等满——嫌慢就把超时降到 5~6，或关掉开关退回判官。
- 决策模型只负责「情绪怎么变」，不管说什么、怎么回；人设和语气仍由同一套 prompt 决定。
- `choice` 的概率分布里 `开心` 和 `感动` 常同时偏高，模型给的是最高概率那个；这是模型特性，
  不是 bug。想更细腻可以给每个维度单独加 `noul` 问题（会变贵/变慢）。
- 费用极低（本机样例一次 `cost=0.00000504` 美元），但按量计费，长期开着请自己看 OpenRouter 账單。

## 情绪维度调整（2026-10-27，argon4 评审）

原来 8 维：开心、兴奋、好奇、烦躁、低落、亲昵、得意、疲惫。
让人「gemini-4-argon-medium」按「严格情绪定义」评了一次，结论：

- `好奇` 是认知状态/探索动机，不是情绪 → 换成 **惊讶**（基础情绪，口语/表情包都匹配）；
- `亲昵` 是关系态度/互动行为，不是情绪 → 换成 **感动**（保留高亲密度的正向内部体验）；
- `疲惫` 是生理/精力状态，不是情绪 → 换成 **害羞**（社交退缩类情绪，扭捏语气+脸红表情包）。

最终 8 维：`开心、兴奋、惊讶、烦躁、低落、感动、得意、害羞`。
同步改了：`emotion.DIMS`、决策模型 `EMOTIONS`、关键词兜底、表情包 `EMOTION_TAGS`
（惊讶→震惊/吃瓜/问号，感动→哭哭/贴贴/抱抱，害羞→害羞/脸红/捂脸）、心情视图正/负维度
（正：开心/兴奋/感动/得意；负：烦躁/低落），以及 `sticker_force_dominant` 默认值（兴奋/烦躁/感动）。

新增契约测试 `tools.test_emotion.DimensionDriftTests`：以后改维度必须同步 decider / 表情包 / 心情视图，否则测试红。
