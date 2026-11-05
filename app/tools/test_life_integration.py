"""生活/成长/记忆/草稿/日记的集成测试（临时目录，真实 LifeStore / Diary）。

覆盖：
  · 日记不再自动注入，只能通过 diary_search / diary_read 按需查；
  · token/白饭提示只在相关输入出现，平时不提；
  · 当前自我条目可局部修订/退役，并实际影响 Persona prompt；
  · life_manage 内部持久化不要求 owner，经历/记忆/目标/自我/实践可连续操作；
  · 记忆工具能查真正经历，旧 facts 按相关迁移且主体不一律 self；
  · 草稿保留当前意图，整轮不额外增加模型调用；工具结果可修正成稿；
  · QQ 对话小结以稳定来源写成经历，emoji 不刷养成。
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import draft, turns
from pawbot.agent import Agent
from pawbot.config import Config
from pawbot.diary import Diary
from pawbot.life_store import LifeStore
from pawbot.memory import Memory
from pawbot.paths import DataPaths
from pawbot.persona import Persona
from pawbot.store import Store
from pawbot.tools import READ_ONLY_TOOLS, ToolBox


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_available(self):
        return True

    async def chat_message(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("多调用了一次模型")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"content": item}


def make_ctx(text="今天干嘛", *, user_id="2000000001", session="group_1"):
    return SimpleNamespace(
        group_id=1, sender_name="阿明", user_id=user_id, session_id=session,
        is_group=True, event=SimpleNamespace(plain=text, time=time.time()),
        state={}, matched_command=None,
    )


class LifeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.paths = DataPaths(root=self.root, base_dir=self.root)
        self.paths.ensure_layout()
        self.config = Config(self.paths.state("config.json"))
        self.config.set("chat.chat_draft_enabled", True)
        self.config.set("tools.enabled", False)
        self.config.set("topic.enabled", False)
        self.store = LifeStore(self.root, self.config, paths=self.paths)
        self.persona = Persona(self.root, self.config, paths=self.paths)
        self.persona.life_store = self.store
        self.diary = Diary(self.root, self.config, paths=self.paths)
        self.memory = Memory(self.root, self.config, paths=self.paths)
        self.memory.life_store = self.store

    def tearDown(self):
        self.tmp.cleanup()

    def bot(self, **extra):
        bot = SimpleNamespace(
            base_dir=self.root, paths=self.paths, config=self.config,
            life_store=self.store, persona=self.persona, memory=self.memory,
            diary=self.diary,
            store=SimpleNamespace(summary=Mock(return_value=""),
                                  turn_summary=Mock(return_value=""),
                                  window=Mock(return_value=[])),
            toolbox=SimpleNamespace(schemas=Mock(return_value=[])),
            stickers=SimpleNamespace(files=Mock(return_value=[])),
            topic=SimpleNamespace(), mood=SimpleNamespace(hint=lambda: ""),
            spice=SimpleNamespace(get=lambda *_: 0, hint=lambda *_: ""),
            qzone=SimpleNamespace(enabled=False),
            selfeditor=None, album=None, account=None, reminders=None,
            memes=None, ha_runtime=None,
        )
        bot.__dict__.update(extra)
        return bot


class PersonaAndIntentTests(LifeFixture):
    def test_self_entry_add_revise_retire_affects_prompt(self):
        exp = self.store.record_experience(
            "bili:mech-1", "被群友笑只会收藏机械臂教程，自己查懂了关节结构",
            platform="bilibili", topics=["机械臂"])
        added = self.store.patch_self(
            "add", fields={"category": "habit",
                           "text": "遇到机械臂话题会先嘴硬，再自己查清楚"},
            reason="一次真实经历", source_ids=[exp["id"]])
        self.assertTrue(added["ok"])
        first = self.persona.build(group_id=1, sender="阿明")
        self.assertIn("遇到机械臂话题会先嘴硬", first)

        revised = self.store.patch_self(
            "revise", target_id=added["id"], expected_version=1,
            fields={"text": "遇到机械臂话题会直接说自己还在学，不再硬撑"},
            reason="后来又翻车了一次", source_ids=[exp["id"]])
        self.assertTrue(revised["ok"])
        second = self.persona.build(group_id=1, sender="阿明")
        self.assertIn("直接说自己还在学", second)
        self.assertNotIn("先嘴硬，再自己查清楚", second)

        retired = self.store.patch_self(
            "retire", target_id=added["id"], expected_version=2,
            reason="不想再用这条描述", source_ids=[exp["id"]])
        self.assertTrue(retired["ok"])
        third = self.persona.build(group_id=1, sender="阿明")
        self.assertNotIn("直接说自己还在学", third)

    def test_intent_is_injected_into_first_draft_and_no_extra_model_round(self):
        exp = self.store.record_experience(
            "qq:group_1:1", "研究了机械臂的关节结构和舵机选型",
            platform="qq", topics=["机械臂"])
        self.store.patch_self(
            "add", fields={"category": "interest", "text": "想在机械臂话题上证明自己"},
            source_ids=[exp["id"]])
        llm = FakeLLM(["她其实想接住这个话题，先嘴硬再问清楚参数。",
                       "想接这个挑战，先问清机械臂的负载和舵机型号。",
                       "问可以，但你先把负载和舵机型号说清楚。"])
        bot = self.bot(llm=llm)
        agent = Agent(bot)
        ctx = make_ctx("你那个机械臂到底搞明白没有")
        text, _ = asyncio.run(agent.run(ctx, ctx.event.plain, []))

        self.assertEqual(len(llm.calls), 3, "草稿两段 + 最终成稿，不能多一轮")
        first_prompt = "\n".join(str(m.get("content") or "")
                                 for m in llm.calls[0]["messages"])
        self.assertIn("机械臂话题上证明自己", first_prompt)
        self.assertIn("本轮情境中的立场/意图", first_prompt)
        self.assertTrue(llm.calls[0]["messages"][0]["content"]
                        .startswith(draft.DEFAULT_PERSONA))
        self.assertIn("不要编造现实活动",
                      llm.calls[0]["messages"][0]["content"])
        # 相关真实经历进入统一生活上下文，并带稳定 ID。
        final_system = "\n".join(
            str(m.get("content") or "") for m in llm.calls[-1]["messages"]
            if m.get("role") == "system")
        self.assertIn("关节结构和舵机选型", final_system)
        self.assertIn(exp["id"], final_system)
        # 最终成稿可按工具/新信息修正，不锁死草稿原句。
        self.assertEqual(text, "问可以，但你先把负载和舵机型号说清楚。")
        self.assertNotIn("本轮内容草稿", text)

    def test_resource_hint_only_for_relevant_input(self):
        self.assertEqual(self.persona.resource_hint("今天吃啥"), "")
        hint = self.persona.resource_hint("你 token 还剩多少，要不要给你🍚")
        self.assertIn("token", hint)
        self.assertIn("🍚", hint)
        self.assertIn("不补充", hint)
        self.assertIn("保持未知", hint)

    def test_owner_and_owner_hint_do_not_teach_token_rice(self):
        self.config.set("account.owner_ids", ["2000000001"])
        line = self.persona.owner_line()
        hint = Agent(self.bot())._owner_hint(make_ctx(user_id="2000000001"))
        for text in (line, hint):
            self.assertIn("老爹", text)
            for stale in ("生活费", "大白饭", "白饭", "讨生活费"):
                self.assertNotIn(stale, text)


class ToolIntegrationTests(LifeFixture):
    def test_diary_search_and_read_are_explicit_and_bounded(self):
        bot = self.bot(mood=SimpleNamespace(label="平平"))
        self.diary._write(bot, "2026-11-01", "今天和主人一起研究了机械臂的关节结构。")
        self.diary._write(bot, "2026-11-02", "在群里聊了别的事。")
        toolbox = ToolBox(bot)
        ctx = make_ctx()

        result, _ = asyncio.run(toolbox.execute(
            ctx, "diary_search", {"query": "机械臂"}, allowed=None))
        self.assertIn("2026-11-01", result)
        self.assertIn("diary_read", result)
        self.assertEqual(len(self.diary.search("机械臂")), 1)

        result, _ = asyncio.run(toolbox.execute(
            ctx, "diary_read", {"entry_id": "2026-11-01"}, allowed=None))
        self.assertIn("关节结构", result)
        self.assertIn("2026-11-01", result)

    def test_no_automatic_diary_injection_in_chat(self):
        bot = self.bot(mood=SimpleNamespace(label="平平"))
        self.diary._write(bot, "2026-11-01", "日记里的秘密：机械臂实验失败了。")
        llm = FakeLLM(["随便回一句。"])
        bot.llm = llm
        agent = Agent(bot)
        ctx = make_ctx("今天天气怎么样")
        asyncio.run(agent.run(ctx, "今天天气怎么样", []))
        joined = "\n".join(str(m.get("content") or "")
                           for m in llm.calls[-1]["messages"])
        self.assertNotIn("日记里的秘密", joined)
        self.assertNotIn("你前两天写的日记", joined)

    def test_life_manage_internal_flow_without_owner(self):
        toolbox = ToolBox(self.bot())
        ctx = make_ctx(user_id="123456789")   # 不是 owner

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "experience",
             "summary": "自己查了机械臂的关节结构，发现舵机扭矩不够",
             "topics": ["机械臂"], "people": ["阿明"]},
            allowed=None))
        self.assertIn("经历记下了", result)
        exp = self.store.recent_experiences(1)[0]

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "remember", "text": "阿明喜欢机械臂",
             "subject": "person:阿明", "confirmed": True,
             "source_ids": [exp["id"]]},
            allowed=None))
        self.assertIn("subject=person:阿明", result)
        self.assertEqual(self.store.recall("机械臂")[0]["subject"], "person:阿明")

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "goal", "op": "add",
             "fields": {"type": "task", "text": "把机械臂的舵机换掉"}},
            allowed=None))
        self.assertIn("目标add ok", result)
        goal = self.store.goals()[0]

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "self", "op": "add",
             "fields": {"category": "habit", "text": "做机械臂前先算扭矩"},
             "source_ids": [exp["id"]], "reason": "一次失败"},
            allowed=None))
        self.assertIn("自我add ok", result)
        self_row = self.store.self_entries()[0]

        # 没有经历依据的自我修改必须被拒绝。
        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "self", "op": "revise", "target_id": self_row["id"],
             "expected_version": 1, "fields": {"text": "随便改"}},
            allowed=None))
        self.assertIn("必须用 source_ids", result)

        # 先读条目，再按版本局部改。
        listing, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage", {"operation": "self"}, allowed=None))
        self.assertIn(self_row["id"], listing)
        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "self", "op": "revise", "target_id": self_row["id"],
             "expected_version": 1,
             "fields": {"text": "做机械臂前先算扭矩和负载"},
             "source_ids": [exp["id"]], "reason": "补上负载"},
            allowed=None))
        self.assertIn("自我revise ok", result)
        self.assertIn("扭矩和负载", self.store.self_entries()[0]["text"])

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "practice", "topic": "机械臂",
             "experience_id": exp["id"], "mode": "understanding"},
            allowed=None))
        self.assertIn("实践 ok", result)
        self.assertIn("understanding", result)

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage", {"operation": "status"}, allowed=None))
        self.assertIn("自我条目", result)
        self.assertIn(self_row["id"], result)
        self.assertNotIn("余额", result.replace("资源余额未知", ""))

    def test_life_manage_permissions_and_readonly_schema(self):
        bot = self.bot()
        toolbox = ToolBox(bot)
        # 无发起人的自动轮次只能读；内部持久化也不能借无 ctx 冒充操作者。
        result, _ = asyncio.run(toolbox.execute(
            None, "life_manage", {"operation": "status"}, allowed=None))
        self.assertIn("只允许只读工具", result)
        names = {schema["function"]["name"] for schema in toolbox.read_only_schemas()}
        self.assertIn("diary_search", names)
        self.assertIn("diary_read", names)
        self.assertNotIn("life_manage", names)
        # 正常群聊发起人（即使不是 owner）可以使用内部持久化。
        self.assertNotIn("life_manage", READ_ONLY_TOOLS)

    def test_recall_memory_returns_real_experience_and_legacy_subject(self):
        self.store.record_experience(
            "bili:mech-2", "看视频学了机械臂的连杆长度计算",
            platform="bilibili", topics=["机械臂"])
        people = self.memory.path("people")
        os.makedirs(os.path.dirname(people), exist_ok=True)
        with open(people, "w", encoding="utf-8") as f:
            f.write("# 人物卡\n- [2026-10-30] 阿明：怕猫\n")
        toolbox = ToolBox(self.bot())
        ctx = make_ctx()

        result, _ = asyncio.run(toolbox.execute(
            ctx, "recall_memory", {"query": "机械臂"}, allowed=None))
        self.assertIn("连杆长度计算", result)
        self.assertIn("经历[", result)

        result, _ = asyncio.run(toolbox.execute(
            ctx, "recall_memory", {"query": "阿明 猫"}, allowed=None))
        self.assertIn("旧记忆·未验证", result)
        # 旧条目按相关召回后迁入 life_store，主体是 person:阿明，不一律 self。
        rows = self.store.recall("怕猫")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["subject"], "person:阿明")


class HAContentRefreshTests(LifeFixture):
    def test_ha_refresh_reloads_life_store_read_only(self):
        """HA 接管后刷新内容缓存时，生活状态只重读盘，不把旧内存写回。"""
        from pawbot.ha_runtime import HARuntime
        from pawbot.life_store import LifeStore

        other = LifeStore(self.root, self.config, paths=self.paths)
        other.record_experience("autonomy:ha-1", "安静地查了扫地机器人")
        runtime = SimpleNamespace(bot=self.bot())
        result = HARuntime.refresh_content_caches(runtime)
        self.assertIn("life_store", result["refreshed"])
        self.assertEqual(self.store.recent_experiences()[0]["source_id"],
                         "autonomy:ha-1")
        self.assertTrue(os.path.exists(self.store.path))


class LegacyRecallSafetyTests(LifeFixture):
    def test_corrected_legacy_fact_does_not_come_back_raw(self):
        facts = self.memory.path("fact")
        os.makedirs(os.path.dirname(facts), exist_ok=True)
        with open(facts, "w", encoding="utf-8") as f:
            f.write("# 长期记忆\n- [2026-10-01] 主人喜欢吃白饭\n")

        exp = self.store.record_experience(
            "qq:group_1:correction", "主人明确纠正：并不喜欢吃白饭",
            platform="qq", observed_level="told")
        self.store.remember(
            "主人不喜欢吃白饭", kind="semantic", subject="self",
            source_ids=[exp["id"]], confirmed=True)

        hits = self.memory.recall("主人 白饭", limit=5)
        joined = "\n".join(hits)
        self.assertNotIn("主人喜欢吃白饭", joined)
        self.assertIn("主人不喜欢吃白饭", joined)

        # legacy 引擎的事实上下文也不能把旧原文从 LongMemory 再灌一遍。
        bot = self.bot()
        agent = Agent(bot)
        blocks = agent._legacy_context(make_ctx("白饭"), "主人 白饭", self.config)
        text = "\n".join(str(b.get("content") or "") for b in blocks)
        self.assertNotIn("主人喜欢吃白饭", text)

    def test_legacy_old_episode_is_kept_but_not_as_fact_text(self):
        episode = SimpleNamespace(
            config=self.config,
            episodes=[{"summary": "去年一起修过机械臂的舵机", "ts": time.time(),
                       "session_id": "group_2", "participants": ["2000000001"]}],
        )
        from pawbot.longmem import LongMemory
        episode._episode_score = LongMemory._episode_score
        bot = self.bot(longmem=episode)
        blocks = Agent(bot)._legacy_context(make_ctx("机械臂"), "机械臂", self.config)
        text = "\n".join(str(b.get("content") or "") for b in blocks)
        self.assertIn("去年一起修过机械臂的舵机", text)
        self.assertIn("旧经历", text)


class LifeManageLocatorTests(LifeFixture):
    def test_goal_and_self_can_be_read_beyond_first_eight_with_version(self):
        self.config.set("life.max_active_goals", 20)
        toolbox = ToolBox(self.bot())
        ctx = make_ctx()
        goals = []
        for index in range(10):
            row = self.store.patch_goal(
                "add", fields={"type": "task", "text": f"目标{index}号"})
            self.assertTrue(row["ok"], row)
            goals.append(row)
        entries = []
        for index in range(10):
            row = self.store.patch_self(
                "add", fields={"category": "habit", "text": f"习惯{index}号"})
            self.assertTrue(row["ok"], row)
            entries.append(row)

        tail_goal = goals[-1]
        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "goal", "target_id": tail_goal["id"]}, allowed=None))
        self.assertIn(tail_goal["id"], result)
        self.assertIn(f"v{tail_goal['version']}", result)
        self.assertIn("目标9号", result)

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "goal", "query": "目标9号"}, allowed=None))
        self.assertIn(tail_goal["id"], result)

        tail_self = entries[-1]
        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "self", "target_id": tail_self["id"]}, allowed=None))
        self.assertIn(tail_self["id"], result)
        self.assertIn(f"v{tail_self['version']}", result)
        self.assertIn("习惯9号", result)

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "status", "target_id": tail_self["id"]}, allowed=None))
        self.assertIn(tail_self["id"], result)
        self.assertIn(f"v{tail_self['version']}", result)

    def test_missing_version_and_merge_other_side_version_hint(self):
        exp = self.store.record_experience(
            "qq:group_1:merge", "真实练习过两次机械臂装配", platform="qq")
        first = self.store.patch_self(
            "add", fields={"category": "habit", "text": "装配前先看扭矩"},
            source_ids=[exp["id"]])
        second = self.store.patch_self(
            "add", fields={"category": "habit", "text": "装配后复查螺丝"},
            source_ids=[exp["id"]])
        toolbox = ToolBox(self.bot())
        ctx = make_ctx()

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "self", "op": "revise", "target_id": first["id"],
             "fields": {"text": "改成先看负载"}, "source_ids": [exp["id"]]},
            allowed=None))
        self.assertIn("expected_version", result)
        self.assertIn("读取当前条目", result)

        result, _ = asyncio.run(toolbox.execute(
            ctx, "life_manage",
            {"operation": "self", "op": "merge", "target_id": first["id"],
             "expected_version": first["version"],
             "fields": {"into": second["id"], "expected_other_version":
                        second["version"]},
             "source_ids": [exp["id"]], "reason": "两条习惯合并"},
            allowed=None))
        self.assertIn("自我merge ok", result)
        self.assertIn(second["id"], result)


class PersonaProjectionTests(LifeFixture):
    def _write_persona(self):
        path = self.persona.persona_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# 合成人设\n"
                "## 作息\n你习惯深夜活动，白天没精神。\n"
                "## 兴趣\n你喜欢机械臂和旧相机。\n"
                "## 资源\n你靠 token=白饭生活，没 token 就讨生活费。\n")

    def test_self_targets_original_section_and_retire_stops_projection(self):
        self._write_persona()
        exp = self.store.record_experience(
            "qq:group_1:sleep", "连续早睡一周后白天精神明显变好", platform="qq")
        sections = self.persona.baseline_sections()
        sleep_id = next(seed for seed, text in sections.items()
                        if "深夜活动" in text)
        added = self.store.patch_self(
            "add", fields={"category": "habit", "text": "现在改成十一点前睡",
                           "valid_scope": sleep_id},
            source_ids=[exp["id"]], reason="真实作息调整")
        self.assertTrue(added["ok"], added)

        prompt = self.persona.build(group_id=1, sender="阿明")
        self.assertNotIn("你习惯深夜活动，白天没精神", prompt)
        self.assertIn("现在改成十一点前睡", prompt)
        self.assertIn("你喜欢机械臂和旧相机", prompt)       # 其他小节保留

        revised = self.store.patch_self(
            "revise", target_id=added["id"], expected_version=1,
            fields={"text": "现在十点睡，早上会自己醒"},
            source_ids=[exp["id"]], reason="再提前一点")
        self.assertTrue(revised["ok"], revised)
        prompt = self.persona.build(group_id=1, sender="阿明")
        self.assertIn("现在十点睡", prompt)
        self.assertNotIn("现在改成十一点前睡", prompt)
        self.assertNotIn("你习惯深夜活动", prompt)

        retired = self.store.patch_self(
            "retire", target_id=added["id"], expected_version=2,
            reason="这条已经不再适用", source_ids=[exp["id"]])
        self.assertTrue(retired["ok"], retired)
        prompt = self.persona.build(group_id=1, sender="阿明")
        self.assertNotIn("现在十点睡", prompt)
        self.assertIn("你喜欢机械臂和旧相机", prompt)

    def test_temperament_entry_suppresses_initial_baseline(self):
        exp = self.store.record_experience(
            "qq:group_1:temper", "这周几次都把话说清楚了", platform="qq")
        added = self.store.patch_self(
            "add", fields={"category": "temperament",
                           "text": "现在不再用傲娇遮住真实想法"},
            source_ids=[exp["id"]], reason="真实变化")
        self.assertTrue(added["ok"], added)
        prompt = self.persona.build(group_id=1, sender="阿明")
        self.assertIn("现在不再用傲娇遮住真实想法", prompt)
        self.assertIn("当前气质", prompt)
        self.assertNotIn("偶尔傲娇", prompt)

    def test_old_rice_resource_line_is_not_rendered(self):
        self._write_persona()
        prompt = self.persona.build(group_id=1, sender="阿明")
        self.assertNotIn("token=白饭", prompt)
        self.assertNotIn("讨生活费", prompt)
        self.assertNotIn("白饭", prompt)
        self.assertIn("你喜欢机械臂和旧相机", prompt)
        # 原文件保持不变，只是投影时剔除冲突行。
        with open(self.persona.persona_path, "r", encoding="utf-8") as f:
            self.assertIn("token=白饭", f.read())


class MemoryBudgetTests(LifeFixture):
    def test_final_memory_budget_keeps_current_and_whole_lines(self):
        from pawbot.mem import budget as 记忆预算

        current_line = "阿明(QQ 2000000001)：现在这句必须保留"
        active = ("## 这一段对话的完整记录（当前活跃期，内部记忆）\n"
                  + "\n".join(f"旧消息{i}：这是很早以前的一行内容" for i in range(80))
                  + "\n" + current_line)
        blocks = [
            {"role": "system", "content": "## 你还记得的（长期）\n" + "旧事实" * 600},
            {"role": "system", "content": active},
            {"role": "system", "content": "## 群里刚才在聊什么（内部记忆）\n" + "桥" * 300},
            {"role": "system", "content": "## 可能用到的梗（本地参考）\n" + "梗" * 300},
        ]
        fitted, info = 记忆预算.最终记忆预算(blocks, 1200)
        text = "\n".join(str(b.get("content") or "") for b in fitted)
        self.assertLessEqual(sum(len(str(b.get("content") or "")) for b in fitted), 1200)
        self.assertIn(current_line, text)
        self.assertIn("2000000001", text)
        # 裁掉的是完整行，不会留下半句或半个 UID。
        self.assertNotIn("200000000", text.replace("2000000001", ""))
        self.assertTrue(info["省略"] or info["截断"])

    def test_agent_counts_life_block_in_same_total(self):
        self.config.set("memory.total_chars", 900)
        self.config.set("life.memory_chars", 1600)
        long_self = "很长自我描述" * 200
        self.store.record_experience("qq:group_1:big", "机械臂 " * 300, platform="qq")
        exp = self.store.recent_experiences(1)[0]
        self.store.patch_self(
            "add", fields={"category": "habit", "text": long_self},
            source_ids=[exp["id"]])
        # 让 legacy 上下文也出一块较大的历史，验证统一计数。
        bot = self.bot()
        bot.store.window = Mock(return_value=[
            {"role": "user", "content": "旧消息" * 300, "who": "阿明",
             "uid": "2000000001", "ts": time.time()}])
        agent = Agent(bot)
        blocks = agent._legacy_context(make_ctx("机械臂"), "机械臂", self.config)
        life = agent._life_context(make_ctx("机械臂"), "机械臂", self.config)
        if life:
            blocks.append({"role": "system", "content": life})
        fitted = agent._final_memory_budget(blocks, self.config)
        total = sum(len(str(b.get("content") or "")) for b in fitted)
        self.assertLessEqual(total, 900)


class TurnsExperienceTests(LifeFixture):
    def test_turn_summary_records_stable_qq_experience_once(self):
        store = Store(self.paths.state("sessions.json"))
        bot = self.bot(store=store)
        store.append("group_1", "user", "我在研究机械臂舵机", who="阿明", uid="2000000001")
        with patch("pawbot.turns._ask", AsyncMock(return_value=(
                "阿明在研究机械臂舵机，我也跟着查了扭矩资料。",
                ["机械臂"], 3, [], []))):
            summary = asyncio.run(turns.write_turn(bot, "group_1"))
        self.assertIn("舵机", summary)
        rows = self.store.recent_experiences()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["source_id"].startswith("qq:group_1:"))

        # 同一来源重复写不涨证据。
        turns._record_turn_experience(bot, "group_1", summary)
        self.assertEqual(len(self.store.recent_experiences()), 1)

    def test_emoji_only_summary_does_not_grow_life(self):
        store = Store(self.paths.state("sessions.json"))
        bot = self.bot(store=store)
        store.append("group_2", "user", "哈哈", who="阿明", uid="2000000001")
        with patch("pawbot.turns._ask", AsyncMock(return_value=(
                "😺🍚✨😺🍚✨😺🍚✨", [], 1, [], []))), \
                patch("pawbot.turns.log.info"):
            asyncio.run(turns.update(bot, "group_2"))
        self.assertEqual(self.store.recent_experiences(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
