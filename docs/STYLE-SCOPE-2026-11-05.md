# B 站学来的说话方式接入其它生成路径

日期：2026-11-05。

## 问题

`bili_learn` 把 B 站评论里学到的语气写进 `content/memory/style_learned.md`，`persona.build()` 会把它作为「学来的说话方式」注入。QQ 群聊、私聊、主动插话、B 站评论/弹幕/动态用的是完整 persona，所以本来就能吃到。

但自主活动、QQ 空间发布/反思走的是 `Autonomy._persona_context()`，它把 `persona.build()` **按开头截断到 3600 字**。云端实测：

- `persona.build()` 长度 11072 字
- 「学来的说话方式」在 **8696 字**处
- 截断到 3600 字后，风格段整段丢失

所以这些“别的地方”说的还是没有人设语气兜底的内容。

## 修改

- `Persona.style_block(limit=1200)`：统一的风格段（自带标题），`build()` 也复用它。
- `Autonomy._persona_context()`：截断后检查是否含「学来的说话方式」；没有就单独补回（默认最多 1200 字），并把正文压缩到剩余预算，总长仍约 3600。已有风格段时不重复追加。
- 覆盖：自主活动的计划/反应、QQ 空间发布、空间评论/反思等所有走 `_persona_context()` 的路径。
- QQ 完整 persona、B 站评论/弹幕/动态原本已有，不做重复改动。

## 验证

- 新增 `app/tools/test_style_scope.py`：
  - 完整 `build()` 含风格段；
  - `_persona_context()` 截断 9000 字后仍含风格段；
  - 已含风格段时不重复。
- 全量回归：**572 tests，OK，0 外部连接尝试**（`D:\whale\tmp\style-scope-regression.log`）。
- 云端热补后进程内探针：`contains_style=true`、`contains_sample=true`、`len=3602`、`style_at=3557`（`deploy_hotfix6.json`）。
- 云端插件重载：14 loaded / 0 failed；本地 PawBot 已重启加载新代码。

## 说明

这里接的是现有 `style_learned.md`（从 B 站**评论**学来的语气），不是视频正文/摘要。若要让“视频笔记里的表达方式”也参与，需要另做一条风格提炼链路，不能直接复用这份文件。
