# 2026-11-04 集成复核与补齐清单

这是实施中发现的具体缺口，不替代总规划。用户已授权本轮直接实现。

## 已确认修复中

- 记忆旧同源confirmed重放不能复活superseded；长句否定优先于相似度去重；conflicted记录不能被近义新候选绕过。
- 既存goal/self所有修改要expected_version；merge两侧CAS，采用fields.expected_other_version表示非target一侧版本。
- 日记同summary独立ID保留全部出处和人物主题；同日补注/归档不能覆盖旧正文/索引。
- 面板/pawbot-ui/life代理和life内容同步。

## 自主能力仍需补齐

1. Autonomy._plan/_react须实际带完整角色的简短规划上下文（调用persona.build，不把裸通用system当大肥鱼）；plan带目标ID和当前版本，支持goal进展/完成/暂停，而非只新增。兴趣来自hobbies与近期真实学习，养成快照实际影响下一步。上下文有界。
2. self_patch反思必须拿到self_entries的ID/version；op扩到add/revise/retire/delete/merge，已有条目要版本，merge另一侧版本。life.evolution_enabled/growth_enabled要真实控制相应行为。
3. _apply_goal可按op更新已存在目标，用新经历作为依据、补progress/next_step；新记录带goal_id。看到相关目标不等于完成，只有实际结果才能推进。
4. 平台动作结果的facts与个人reaction分离；记录真实所见summary保底，不允许二次模型把材料改成没发生的事实。模型反应可存reaction，生成自述摘要要保留来源和理解程度。
5. Qzone自主读支持已有自己动态及其留言；显式配置好友feed时能选择浏览，留言回复/like/repost逐项接真实适配，缺能力明确跳过。不能只有不可被规划选择的壳方法。
6. B站信息源必须超出关键词。新增bili_discovery.discover(bot,source,*,query='',uid=0,media_id=0,limit=12)->dict{ok,source,items,reason}，采用本机已安装bilibili_api接口，来源recommend/following/toview/favorites/user_videos/related/search。bili_scroll.run_round扩可选candidates/source参数，既有调用兼容；Autonomy按plan.bili_source去取实际候选后复用字幕/笔记管线。API实际签名先读本机库，不猜。无结果可以选择search fallback，明确来源。
7. 冷却用结束时间，正常对话recent_conversation信号应影响主动开新话题（例如最近15分钟不另起话题），直接回复照常。强制三小时gap还需压住旧bili_report/爱好/主动入口。
8. 静默活动不得堵Proactive._tick几十秒；有限task在现有producer下受取消/stop/HA排空管理，无法绕过停机准入。

## 建议额外边界

主界面缺desktop bridge控制，不能为截图启动替代GUI；安装/浏览器核验如无法实操，实话记限制。继续进行语法、真实loopback与Client slot验证。
