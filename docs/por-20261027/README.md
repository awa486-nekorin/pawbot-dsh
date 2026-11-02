# 大肥鱼人设交付包（2026-10-27）— 落库说明

这个目录是桌面交付包 `C:\Users\Administrator\Desktop\11\por\` 的**脱敏归档**，
以及「这些文档到底落进了插件哪些文件」的收据。以后要回滚人设，先看本文件。

## 这份交付包是什么

一次针对「AI 端语料没有活人感」的诊断 + 人设重写：

| 文件 | 作用 |
|---|---|
| `00_交付说明.md` | 交付清单与三个关键数字 |
| `04_诊断_AI端语料为什么没有活人感.md` | 量化证据、六组对照、提示词病灶、S 级 81 句重评 |
| `05_persona_v3_优化版.md` | por 交付的 v3 定稿（3236 字符 / 2446 汉字 / 9 节；含应用时第 68 行梗库修正） |
| `05b_persona_v4_自我认知.md` | **2026-10-27 追加的 v4（当前线上版）**：新增「你脑子里怎么想」（第一人称 RP 思考）+「你怎么看自己、你在哪」（跑在主人设备 / DeepSeek 模型 / 属于主人）+ 软糯活人感微调；见 `09_活人感A_B_v3v4.md` |
| `06_改进说明_v0到v4.md` | 改了什么、删了什么、四轮优化过程、代码层配套建议 |
| `07_评测报告_新旧对照.md` | 同上下文 A/B、盲评 31:0、句面指标 |
| `08_应用说明与回滚.md` | 应用/回滚方式（原交付时受沙箱权限所限没能自动写入） |
| `09_活人感A_B_v3v4.md` | v4 的改法、gemini-4-argon-medium 生成+盲评过程、10 组 A/B 结果、真机验收、回滚 |
| `大肥鱼提示词全集.md` | 按调用链路逐块列出的全部「影响说话」的提示词原文与触发条件 |
| `persona_v0_回滚备份_20261027.md` | **改动前的人设逐字备份**（25934 B / 138 行）——回滚只认这一份 |
| `persona_v3_20261027.md` | v4 应用前的 v3 线上备份（9056 B） |

**归档时做过的脱敏**（`git log` 里能看到修改时间）：

- `04` 里一处 `@<真实QQ号>` → `@某群友`，示例行号 `Uxxxx`。
- `大肥鱼提示词全集` 里真实群号 / 主人 QQ 号 → `某群（号已隐去）` / `尾号 xxxx`。
- 以下**没有**归档（原因见桌面交付包的审计报告）：`02_影响说话的配置快照.json`
  （含真实 key 前后各 4 位与明文 QQ 号）、`ab_*.jsonl` / `cases.json` / `samples.md` /
  `human_ref.md` / `evidence.md`（真实群聊上下文）、`03_*` / `judge_*` / `r*.json`
  （体积大且是一次性评测数据）。

## 落在了哪些文件（收据）

| 交付项 | 落点 | 状态 |
|---|---|---|
| 05 persona v3 | `app/data/persona.md`（2026-10-27 15:xx 起被 v4 取代；v3 归档在 `05_persona_v3_优化版.md` / `persona_v3_20261027.md`） | ✅ 已应用过（已被 v4 取代） |
| **05b persona v4（追加需求）** | `app/data/persona.md`（= `05b_persona_v4_自我认知.md`，10598 B / 2906 汉字） | ✅ **当前线上版** |
| 06 §6.1 网络用词口径 | `app/pawbot/persona.py` 的 `Persona.build()`：追加块改成「## 网络用词 / 跟着用，不用刻意学，也别刻意避开」 | ✅ |
| 06 §6.2 成稿约束 | `app/pawbot/agent.py` `_drafting_hint()`：「能接就接，接不上就丢个反应」 | ✅ |
| 06 §6.3 句数诱导 | `app/pawbot/agent.py` `_batch_hint()`：「大概 N 句的量……别为了凑句数把话撑长」 | ✅ |
| 06 §6.4 长度权重 | 不动（`reply.length_weights` 仍 `[5,3,2,0,0]`；v4 人设 + 6.3 已经压长度，cap 由 `reply.length_max=3` 兜底） | ⏸ 观察后再说 |
| 审计补充 P1 梗库口径 | `persona.md`「先查本地梗库（moegirl 只查本地已有资料，不联网）」——与 `agent.py` 的本地优先实现一致 | ✅（v4 保留） |
| 审计补充 P2 措辞 | `agent.py` `_drafting_hint()`「猫娘感」→「软妹感」（角色是鲸鱼娘） | ✅ |
| 审计补充 P3 漂移 | `config.py` `reply.max_parts` 默认 `2` → `3`（与运行态、`style._DEFAULT_MAX_PARTS`、「最多三条」对齐） | ✅ |
| 测试同步 | `app/tools/test_persona_tone.py`（反例小节 / 边界句 / 取消过期的 skip）、`test_batch_reply.py`、`test_moegirl.py`、`test_owner_persona.py`、`selftest.py`（v4 沿用这些硬句子） | ✅ |

## 怎么验证

```powershell
cd D:\WorkBuddy\pawbot-dsh\app
$env:PYTHONPATH = '.'
py -m unittest tools.test_persona_tone tools.test_batch_reply tools.test_owner_persona tools.test_moegirl -q
```

人设注入冒烟（`selftest.py` 在本机会卡在 Pixiv 元数据，不能当门禁）：

```powershell
cd D:\WorkBuddy\pawbot-dsh\app
py -c "import sys; sys.path.insert(0,'.'); from pawbot.persona import Persona; from pawbot.config import Config; t=Persona('.', Config('data/config.json')).build(group_id=1, sender='阿明'); print(len(t), '## 网络用词' in t, '跟着用' in t, '不是你的词库' in t)"
```

## 怎么回滚

只回滚人设（热加载，不用重启 PawBot）：

```powershell
# 回到 v3（v4 应用前的线上版）
Copy-Item "D:\WorkBuddy\pawbot-dsh\docs\por-20261027\persona_v3_20261027.md" `
          "D:\WorkBuddy\pawbot-dsh\app\data\persona.md" -Force

# 或者回到 por 交付的 v3 定稿（含第 68 行梗库修正）
Copy-Item "D:\WorkBuddy\pawbot-dsh\docs\por-20261027\05_persona_v3_优化版.md" `
          "D:\WorkBuddy\pawbot-dsh\app\data\persona.md" -Force

# 或者回到最原版 v0（25934 B，por 改动前）
Copy-Item "D:\WorkBuddy\pawbot-dsh\docs\por-20261027\persona_v0_回滚备份_20261027.md" `
          "D:\WorkBuddy\pawbot-dsh\app\data\persona.md" -Force
```

代码层三处（`persona.py` / `agent.py`）与测试同步如果要一起回滚：**不要** `git checkout` 整文件——
`agent.py` 同文件里还混着 moegirl 本地知识等无关未提交改动，会被一起冲掉；按 `git diff` 反向改。

## 两个容易踩的事实

1. 桌面交付包里的 `01_persona_原文.md` **不是**改动前原件：它与 `backup/persona_v0_20261027.md`
   只差第 31 行「老爹」→「监护人」（+3 字节）。**回滚只认 `persona_v0_回滚备份_20261027.md`。**
2. 人设文档和运行时文件是两个位置：`05_persona_v3_优化版.md`（v3） / `05b_persona_v4_自我认知.md`（v4）
   与 `app/data/persona.md`。**当前线上 = v4**；改人设时只改 `app/data/persona.md`，再同步对应归档，
   否则这份归档会变成"看起来是定稿、其实不是"的陷阱。
