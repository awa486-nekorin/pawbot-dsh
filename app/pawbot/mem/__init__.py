"""记忆系统 v2：四层 + 总预算闸门 + 实体记忆。

为什么要重做，见 `记忆系统重做-逻辑判定.md`。一句话：
**旧实现把「累积摘要」当成「不可变事件」追加，于是同一件事被反复压缩、反复追加（实测重复率 36%）。**

用法（`agent.py` 里只改一处）：

    from .mem import assemble, write
    if bot.config.get("memory.engine", "legacy") == "v2":
        块, 统计 = assemble.build(bot, ctx, user_text)
        messages += 块

四层：
    L0 在场   最近的原话（条数 + 时间双上限）
    L1 这一轮 本轮**增量**小结（不含历史）
    L2 这些天 日摘要 + 日记
    L3 长期   实体记忆（人 / 群 / 约定 / 事实 / 高重要度事件）

默认不启用（`memory.engine = "legacy"`）。装上新代码不影响线上。
"""

from .assemble import build, 取实体库
from .entities import EntityStore
from .write import merge_day, write_turn

__all__ = ["build", "write_turn", "merge_day", "EntityStore", "取实体库"]
