# 自主生活、成长与养成实施记录

更新：2026-11-04。用户已授权立即实现已讨论规划并新增养成、修正 token/白饭表达。完整需求见 [规划](<D:/whale/pawbot-自主生活与成长机制规划.md>)。

## 目标和约束

- 隔 2–4 小时获得自主活动机会，能安静做事并在以后相关时提及。群主动新话题节流约 3 小时，空间更稀疏。普通直接对话不机械套用此间隔。
- 初始任性爱闹、傲娇好胜、鬼点子多；实际经历驱动意图和逐条变化。
- 记忆分层、来源归并、候选/冲突/时间语义、预算相关召回；日记按需搜索再读，双上下文路径都不自动注入。
- 愿望/执念/项目少量持久化，能暂停完成放弃，相关时自然提，不设提及配额。
- 养成：领域了解与实践经验分开，成果纪念有经历来源，不凭消息数/emoji/token消费刷点，不设日常喂养/断签惩罚。
- 消耗的是主人 token。删除 token=白饭的常驻引导，不把🍚/图片/文字喂饭当资源充值；不主动讨 token、不编余额。
- 数据写入走 content/state；不读真实人设/记忆/凭证作为开发测试输入。保留既有未提交改动。
- 功能代码由 opencode-go/deepseek-v4.1-flash 完成；Lead 负责接口、集成审查和独立行为验证。

## 当前已验证基线

分支 upload-2026-10-27，HEAD 408e740，有既有未提交代码和数据变化。未创建缺少既有更改的新 worktree。

`py -3.13 -m unittest tools.test_owner_persona tools.test_diary tools.test_mem tools.test_chat_draft -q` → 65 tests，OK（2026-11-04 19:13）。这些是旧行为基线，不证明新增需求已经实现。

## 工作拆分与唯一写入范围

1. 状态基础 worker：新增 `app/pawbot/life_store.py` 与 `app/tools/test_life_store.py`。不编辑其他文件。
2. 日记/记忆 worker：`diary.py`、`memory.py`、`mem/layers.py`、`mem/assemble.py` 及对应测试；等待固定状态合同后接入。
3. 自主与平台 worker：新增 `autonomy.py`，修改 `proactive.py`、`bili_scroll.py`、`qzone.py` 及其新测试；不编辑 core/tools/agent。
4. 集成 worker：core、tools、agent、persona、draft、config、webui、client 和新增测试；以其他模块合同为准，Lead 审核后使用。

按依赖推进，完成前不宣称整个规划完成。不同时给两个编码过程相同文件范围。

## 状态基础合同（v1）

模块 `pawbot.life_store`，`LifeStore(base_dir, config, *, paths=None)`。构造只读，不建默认剧情。持久化到 `paths.content('life/state.json')`，内部 RLock、读取当前版本、唯一临时文件原子替换；所有公开返回深拷贝。`reload()` 可供 HA 刷新。

方法：

- `record_experience(source_id, summary, *, platform='local', topics=None, people=None, reaction='', observed_level='', goal_id='', kind='episode', confidence=0.6, salience=3, now=None) -> dict`。稳定来源 ID 去重，不重复提升证据；字段带 id/root_source_id/ts/summary/reaction/topics/people/platform/goal_id/observed_level。
- `recent_experiences(limit=10, *, start=None, end=None) -> list[dict]`。
- `remember(text, *, kind='semantic', subject='self', source_ids=None, confidence=0.6, salience=3, confirmed=False, now=None) -> dict`。六类逻辑类型，支持事实候选、明确确认、同义去重，冲突不静默覆盖。
- `recall(query, *, kinds=None, limit=6, budget=1400, now=None) -> list[dict]`。相关性必须先过门槛，来源频率饱和，候选带状态、冲突默认跳过；检索不增加置信度；时间有效性与last_mentioned分离。
- `mark_mentioned(ids, *, surface='', now=None)`。
- `goals(*, active_only=True) -> list[dict]`。
- `patch_goal(operation, *, target_id='', expected_version=None, fields=None, reason='', source_ids=None, now=None) -> dict`。op add/revise/complete/pause/abandon/delete；愿望/执念/任务类型；少量active上限，返回ok/error/id/version。
- `self_entries(*, active_only=True) -> list[dict]`。
- `patch_self(operation, *, target_id='', expected_version=None, fields=None, reason='', source_ids=None, now=None) -> dict`。op add/revise/retire/delete/merge；稳定ID+CAS，局部改、保留其他字段；source_ids引用有效经历，不覆盖外部人设正文。
- `practice(topic, *, experience_id, mode='understanding', outcome='observed', artifact='', people=None, now=None) -> dict`。mode understanding/practice；真实存在经历、同一经历同一能力只计一次；低理解级别不计实践。技能阶段有解释，纪念有来源。不奖励token/emoji。
- `growth_snapshot() -> dict`。返回skills/mementos/artifacts/changes/goals的有界可查看状态，资源余额未知。
- `context_for(query, *, budget=1600, now=None) -> str`。只相关记忆、目标、自我条目；不输出全愿望清单、不插默认台词。短身份倾向由persona负责。

错误数据应明确报错或只读降级，不能悄悄重置成长。删除/退役有tombstone，默认检索/投影排除；删除内容在当前文件不保留正文。

## 自主活动对接合同（v1）

`Autonomy(bot)` 放在新 `pawbot/autonomy.py`，core 在构造 life_store 后挂 `bot.autonomy`，不另起永久循环；由现有 Proactive._tick 在群/QQ连接/安静时段判断之前调用 `await bot.autonomy.tick()`，因此静默活动不依赖群。autonomy 自己检查启用与HA准入并持久化下一次时间。允许测试注入 now/计划回调。

提供 `snapshot() -> dict`、`share_candidate(session_id, query='') -> dict|None`、`can_share(session_id, *, now=None) -> bool`、`note_shared(session_id, experience_id='', *, now=None)`、`note_conversation(session_id, *, now=None)`。state在paths.state('autonomy.json')，next_at/running/分享/冷却落盘，首次唤醒2–4小时，无补发。分享候选过期不必清空，默认群间隔3小时、全局1小时、空间6–12小时。普通被叫回复不受can_share硬限制，只记近期交流。

tick只执行一次少量动作：search/bili/qzone_read/qzone_post/reflect/skip；模型结构化选择，使用实际内容形成summary/reaction，写life_store，实践调用understanding不能冒充操作。经验和目标状态回流；反思可用life_store.patch_self逐条改有依据条目，不重写persona正文。未有实际资料/失败，不写成成功经历。

Qzone可新增专门的运行时自主capability注册与方法，不能伪造人类owner；保留正常手动API与检查。自主读写沿qzone.enabled/proactive_enabled，回执和发送冷却；好友动态和like/repost只用明确配置的真实后端，不猜动作。B站使用现有run_round并把notes写life_store，旧定时汇报接入can_share；来源扩展必须用现有vendored/API真实方法，明确不支持项。

## 聊天/工具/生命周期集成合同（v1）

- core构造 `bot.life_store=LifeStore(...paths=self.paths)`；挂 `persona.life_store`、`memory.life_store`；构造topic后挂 `bot.autonomy=Autonomy(self)`。不新增独立scheduler（Proactive已执行tick）。HA内容刷新调用life_store.reload，只读。
- `Persona.build`取消最近事实保底注入；常驻初始气质简短，不强制每句傲娇；当前有效SelfEntry形成低预算自我摘要，按category/target处理修订与退役而不复读旧描述。`resource_hint(query='')->str`只在相关输入给token/emoji澄清，不把白饭常驻强化；owner_line/_owner_hint删除token=白饭与讨生活费引导。
- Agent legacy取消diary.recent；统一 life_store.context_for(user_text, budget=...)，v2也接入同一份（不要两边重复）。intent由当前自我/关系/当前目标给简短意图，再交draft，最终可修订；不能每条多加复杂调用。相关检索工具对自然回忆信号明确可发现。所有记忆分支总量按预算记录，不拿无关最新事实填空。
- memory.remember/recall优先life_store并兼容旧条目检索。旧facts只按相关召回，不再每次全灌；旧源以稳定legacy ID导入且标低置信、去重，不读运行数据做测试。新写带来源，subject不是一律self；具体行为有情节记录时作为可靠引用。
- Tools增加 `diary_search`/`diary_read`（按日记worker合同），`life_manage`单一内部成长入口：operation=status/experience/remember/goal/self/practice，query/op/target_id/expected_version/fields/reason/source_ids/experience_id/topic/mode/outcome等必要参数。返回少量ID+状态，可继续读，不暴露整库。goal/self/practice不依赖伪造人类owner；toolbox将它归为内部持久化类，与对外写/账号分开。self修订必须有life_store经历依据，热生效，不重写整节。保留现有edit_persona兼容路径，不擅自破坏旧策略。
- 对话里实际交互可由turns.write_turn（若编辑它需要明确纳入scope）接life_store.record_experience，用session+upto稳定source_id，不把她自述当事实学习。Goal/自我工具若要当轮经历，可从ctx的真实当前输入创建事件，带会话/人物定位；不是每条emoji涨养成。
- `WebUI GET /api/life` 返回 `{ok:true,data:{growth:life_store.growth_snapshot(),autonomy:autonomy.snapshot()}}`；没有组件时空结构降级。

## 配置/界面对接合同（v1）

沿用现有life段，新增 `autonomy_enabled=True, activity_min_hours=2.0, activity_max_hours=4.0, share_gap_hours=3.0, global_share_gap_hours=1.0, qzone_gap_hours=8.0, max_active_goals=8, memory_chars=1600, evolution_enabled=True, growth_enabled=True`。日记 `keep_days`保留兼容但改说明为归档天数。client所有新键metadata中文完整。先提供只读生活成长视图（GET /api/life）集成现有面板，状态/技能/纪念/目标/自改差异显示有界列表，无喂饭按钮/假余额/源码术语堆叠。界面不触发活动和发消息。

## 待续

- 模型通路已验证 MODEL_READY：独立headless overlay使用opencode-go/deepseek-v4.1-flash及已保存凭据引用，未打印key。状态基础worker与日记worker已启动，各自独立文件范围。
- 记录每个worker的修改清单和真实测试结果。
- 后续总验收：虚拟时钟静默活动→持久经历→按需召回→成长/自改→跨日恢复，token资源认知，旧B站/停机/HA契约回归。
