"""Offline autonomy-clock/activity regressions.

真实 LifeStore + 临时数据目录 + 虚拟时钟；平台输入（搜索、B 站、空间、模型）
都只提供模拟回包，不 mock 被测的状态/时钟/分享逻辑。
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import autonomy as autonomy_mod
from pawbot import bili_scroll
from pawbot import brain
from pawbot import paths as layout
from pawbot import proactive as proactive_mod
from pawbot import tasks as tasks_mod
import pawbot
from pawbot.autonomy import Autonomy
from pawbot.life_store import LifeStore
from pawbot.llm import LLMClient
from pawbot.proactive import Proactive


class Clock:
    def __init__(self, start=1_760_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class FixedRng:
    """uniform 返回下界，让 2–4 小时间隔可复现成 2 小时。"""

    def __init__(self):
        self.value = 0.25

    def uniform(self, lo, hi):
        return float(lo)

    def random(self):
        return self.value


class FakeLLM:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.calls = 0

    async def chat_message(self, messages, tools=None, **kwargs):
        self.calls += 1
        return {"content": json.dumps(self.payload, ensure_ascii=False)}


class AutonomyCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.paths = layout.from_data_root(self.root, base_dir=self.root)
        self.clock = Clock()
        self.config = {
            "life.autonomy_enabled": True,
            "life.activity_min_hours": 2,
            "life.activity_max_hours": 4,
            "life.share_gap_hours": 3,
            "life.global_share_gap_hours": 1,
            "life.qzone_gap_hours": 8,
            "tools.search_provider": "bing",
            "tools.bili_scroll_enabled": True,
            "qzone.enabled": False,
        }
        self.store = LifeStore(self.root, self.config, paths=self.paths)
        self.sent = []

        async def manual_send(target, message, is_group=True):
            self.sent.append((target, message, is_group))

        self.bot = SimpleNamespace(
            base_dir=self.root, paths=self.paths, config=self.config,
            life_store=self.store, topic=SimpleNamespace(digest=lambda *a: ""),
            manual_send=manual_send,
        )

    def auto(self, planner=None, llm=None):
        if llm is not None:
            self.bot.llm = llm
        return Autonomy(self.bot, now=self.clock, planner=planner, rng=FixedRng())

    async def schedule(self, auto):
        result = await auto.tick()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not-due")
        return auto.snapshot()

    def due(self):
        self.clock.advance(2 * 3600 + 1)

    # ---------------------------------------------------------- 首轮/重启
    async def test_first_wake_is_random_wait_and_persisted(self):
        auto = self.auto()
        snap = await self.schedule(auto)
        self.assertAlmostEqual(snap["next_at"], self.clock.now + 2 * 3600, delta=2)
        self.assertTrue(os.path.isfile(auto.state_path))
        on_disk = json.loads(Path(auto.state_path).read_text(encoding="utf-8"))
        self.assertFalse(on_disk["running"])
        self.assertEqual(on_disk["next_at"], snap["next_at"])

    async def test_restart_does_not_catch_up_missed_activity(self):
        first = self.auto()
        await self.schedule(first)
        self.clock.advance(3 * 3600)       # 离线期间错过了这次机会

        calls = []

        async def planner(context):
            calls.append(context)
            return {"action": "skip", "reason": "不应执行"}

        restarted = self.auto(planner=planner)
        snap = restarted.snapshot()
        self.assertEqual(calls, [])
        self.assertGreater(snap["next_at"], self.clock.now + 3600)
        result = await restarted.tick()
        self.assertEqual(result["reason"], "not-due")
        self.assertEqual(calls, [])

    async def test_interrupted_running_is_abandoned_without_replay(self):
        first = self.auto()
        await self.schedule(first)
        raw = json.loads(Path(first.state_path).read_text(encoding="utf-8"))
        raw["running"] = True
        raw["activity"] = {"id": "act_x", "status": "running", "started_at": self.clock.now}
        Path(first.state_path).write_text(json.dumps(raw), encoding="utf-8")

        calls = []

        async def planner(context):
            calls.append(context)
            return {"action": "skip", "reason": "不应执行"}

        restarted = self.auto(planner=planner)
        snap = restarted.snapshot()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["activity"]["status"], "abandoned")
        self.clock.advance(10)             # 恢复时已重排，不会立刻补发
        self.assertEqual((await restarted.tick())["reason"], "not-due")
        self.assertEqual(calls, [])

    # ---------------------------------------------------------- 真实动作
    async def test_search_activity_records_real_experience_and_practice(self):
        reaction = {
            "summary": "搜了机械臂坐骑，真有人拿扫地机器人改装，还写了承重问题。",
            "reaction": "比想象中离谱，但承重这条是真麻烦。",
            "topics": ["机械臂", "扫地机器人"],
            "confidence": 0.72,
            "share": True,
            "share_point": "扫地机器人当坐骑的承重问题",
            "share_hours": 24,
            "goal": {"type": "task", "text": "再看看承重方案",
                     "next_step": "找一篇实际改装记录"},
        }
        llm = FakeLLM(reaction)

        async def planner(context):
            self.assertIn("search", context["available_actions"])
            return {"action": "search", "query": "机械臂 扫地机器人",
                    "topic": "机械臂", "reason": "突然好奇"}

        def fake_search(query, config):
            return {"provider": "fake", "results": [{
                "title": "机械臂能当坐骑吗 有人试过", "url": "https://example.test/a",
                "snippet": "有人用扫地机器人底盘改了承重结构",
            }]}

        def fake_fetch(url, max_chars=3000):
            return {"ok": True, "title": "机械臂坐骑", "url": url,
                    "text": "实测承重不足，转弯会打滑。"}

        auto = self.auto(planner=planner, llm=llm)
        await self.schedule(auto)
        self.due()
        with patch.object(autonomy_mod.websearch, "search", fake_search), \
                patch.object(autonomy_mod.websearch, "fetch", fake_fetch):
            result = await auto.tick()

        self.assertTrue(result["ok"], result)
        rows = self.store.recent_experiences(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform"], "web")
        self.assertEqual(rows[0]["observed_level"], "page_text")
        self.assertIn("承重", rows[0]["summary"])
        skills = self.store.growth_snapshot()["skills"]
        self.assertTrue(any(skill.get("understanding_count", 0) >= 1 for skill in skills))
        self.assertTrue(any(goal.get("text") == "再看看承重方案" for goal in self.store.goals()))
        self.assertEqual(self.sent, [])
        self.assertTrue(rows[0]["source_id"].startswith("web:"))
        candidate = auto.share_candidate("group_1", "机械臂 视频")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["experience_id"], rows[0]["id"])
        self.assertIsNone(auto.share_candidate("group_1", "今晚吃什么"))

    async def test_bili_round_writes_notes_record_with_real_source_id(self):
        async def planner(context):
            return {"action": "bili", "topic": "机械臂"}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "summary": "刷到机械臂视频，字幕里说承重会打滑。",
            "reaction": "又是承重。", "topics": ["机械臂"],
            "share": False}))
        await self.schedule(auto)
        self.due()
        round_result = {
            "ok": True, "credential": True,
            "notes": [{"bvid": "BV1xx", "title": "机械臂实测", "author": "up",
                       "url": "https://b23.tv/BV1xx", "gist": "承重会打滑",
                       "note": "a.md", "watched_level": "subtitle"}],
            "subtitle": {"ok": 1}, "reacted": {}, "failed": 0,
        }
        discovery = SimpleNamespace(discover=AsyncMock(return_value={
            "ok": True, "source": "recommend",
            "items": [{"bvid": "BV1xx", "title": "机械臂实测"}]}))
        run = AsyncMock(return_value=round_result)
        with patch.dict(sys.modules, {"pawbot.bili_discovery": discovery}), \
                patch.object(pawbot, "bili_discovery", discovery, create=True), \
                patch.object(autonomy_mod.bili_scroll, "run_round", run):
            result = await auto.tick()
        self.assertTrue(result["ok"], result)
        discovery.discover.assert_awaited_once()
        self.assertEqual(discovery.discover.await_args.args[1], "search")
        self.assertEqual(run.await_args.kwargs["candidates"],
                         [{"bvid": "BV1xx", "title": "机械臂实测"}])
        self.assertEqual(run.await_args.kwargs["source"], "recommend")
        rows = self.store.recent_experiences(5)
        self.assertEqual(rows[0]["source_id"], "bilibili:BV1xx")
        self.assertEqual(rows[0]["observed_level"], "subtitle")
        self.assertEqual(self.sent, [])

    async def test_share_cooldowns_are_persisted_and_success_only(self):
        auto = self.auto()
        self.assertTrue(auto.can_share("group_1"))
        auto.note_shared("group_1")
        self.assertFalse(auto.can_share("group_1"))
        self.assertFalse(auto.can_share("group_2"))       # 全局 1 小时
        self.clock.advance(3601)
        self.assertFalse(auto.can_share("group_1"))       # 本群 3 小时
        self.assertTrue(auto.can_share("group_2"))

        restarted = self.auto()
        self.assertFalse(restarted.can_share("group_1"))
        self.clock.advance(3 * 3600)
        self.assertTrue(restarted.can_share("group_1"))

    async def test_model_failure_backs_off_without_fake_experience(self):
        calls = []

        async def planner(context):
            calls.append(1)
            raise RuntimeError("model down")

        auto = self.auto(planner=planner, llm=FakeLLM())
        await self.schedule(auto)
        self.due()
        result = await auto.tick()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.recent_experiences(5), [])
        snap = auto.snapshot()
        self.assertEqual(snap["failure_count"], 1)
        self.assertGreater(snap["next_at"], self.clock.now)
        self.clock.advance(60)
        self.assertEqual((await auto.tick())["reason"], "not-due")
        self.assertEqual(len(calls), 1)                  # 不无意义重试

    async def test_empty_search_is_failure_not_a_success_story(self):
        async def planner(context):
            return {"action": "search", "query": "完全不存在的问题"}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "summary": "我搜到了很多", "share": True}))

        def empty_search(query, config):
            return {"provider": "fake", "results": [], "error": "没有结果"}

        await self.schedule(auto)
        self.due()
        with patch.object(autonomy_mod.websearch, "search", empty_search):
            result = await auto.tick()
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.recent_experiences(5), [])
        self.assertIsNone(auto.share_candidate("group_1", "完全不存在的问题"))

    async def test_skip_keeps_quiet_and_schedules_normally(self):
        async def planner(context):
            return {"action": "skip", "reason": "今天不想动"}

        auto = self.auto(planner=planner)
        await self.schedule(auto)
        self.due()
        result = await auto.tick()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.store.recent_experiences(5), [])
        snap = auto.snapshot()
        self.assertEqual(snap["last_reason"], "今天不想动")
        self.assertGreaterEqual(snap["next_at"] - self.clock.now, 2 * 3600 - 1)

    async def test_invalid_plan_action_is_not_executed(self):
        async def planner(context):
            return {"action": "post_qzone", "content": "乱发"}

        auto = self.auto(planner=planner)
        await self.schedule(auto)
        self.due()
        result = await auto.tick()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.store.recent_experiences(5), [])

    async def test_reflect_requires_real_evidence_for_self_patch(self):
        async def reflect(context):
            return {"action": "reflect"}

        no_evidence = self.auto(planner=reflect, llm=FakeLLM())
        await self.schedule(no_evidence)
        self.due()
        result = await no_evidence.tick()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.store.self_entries(), [])

        # 有真实经历后，引用不存在来源的补丁仍必须被拒绝。
        row = self.store.record_experience(
            "bilibili:BV123", "看了一个机械臂承重的视频，实测会打滑。",
            platform="bilibili", topics=["机械臂"], observed_level="subtitle",
            now=self.clock.now)
        self.bot.llm = FakeLLM({
            "summary": "", "self_patch": {
                "op": "add", "fields": {"text": "我擅长机械设计"},
                "source_ids": ["exp_does_not_exist"], "reason": "一次视频"}})
        self.clock.advance(2 * 3600 + 1)
        bad_result = await no_evidence.tick()
        self.assertTrue(bad_result["ok"], bad_result)
        self.assertEqual(self.store.self_entries(), [])

        self.bot.llm = FakeLLM({
            "summary": "", "self_patch": {
                "op": "add", "fields": {"text": "我知道承重不能只看参数"},
                "source_ids": [row["id"]], "reason": "实际视频里看到打滑"}})
        self.clock.advance(2 * 3600 + 1)
        good_result = await no_evidence.tick()
        self.assertTrue(good_result["ok"])
        entries = self.store.self_entries()
        self.assertEqual(entries[0]["text"], "我知道承重不能只看参数")
        self.assertEqual(entries[0]["source_ids"], [row["id"]])

    async def test_qzone_post_uses_capability_and_cools_down(self):
        from pawbot.qzone import Qzone

        self.config.update({"qzone.enabled": True, "qzone.proactive_enabled": True,
                            "qzone.actions.list": "get_qzone_msg_list"})
        conn = SimpleNamespace(call=AsyncMock(return_value={"tid": "tid-9"}))
        self.bot.conn_for = lambda: conn
        self.bot.qzone = Qzone(self.bot)
        calls = []

        async def planner(context):
            calls.append(context["available_actions"])
            return {"action": "qzone_post", "content": "今天把机械臂方案画歪了"}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "summary": "在空间发了机械臂方案", "reaction": "画歪了也想留着",
            "topics": ["机械臂"], "share": False}))
        await self.schedule(auto)
        self.due()
        result = await auto.tick()
        self.assertTrue(result["ok"], result)
        conn.call.assert_awaited()
        self.assertEqual(conn.call.await_args.args[0], "send_qzone_msg")
        rows = self.store.recent_experiences(5)
        self.assertEqual(rows[0]["platform"], "qzone")
        self.assertEqual(self.bot.qzone.records[-1]["actor_id"],
                         "autonomy:life.autonomy")
        self.assertIn("qzone_post", calls[0])

        # 8 小时空间冷却：下一次计划即使还提 qzone_post 也不会执行。
        self.due()
        second = await auto.tick()
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(len([r for r in self.store.recent_experiences(5)
                              if r["platform"] == "qzone"]), 1)

    async def test_planning_uses_persona_hobbies_skills_and_goal_versions(self):
        builds = []

        def build(**kwargs):
            builds.append(kwargs)
            return "角色上下文：" + str(kwargs.get("extra") or "")

        self.bot.persona = SimpleNamespace(build=build)
        self.bot.hobbies = SimpleNamespace(items=[
            {"name": "鸣潮", "kind": "game", "last_news": "新版本"}])
        goal = self.store.patch_goal(
            "add", fields={"type": "task", "text": "把机械臂方案画完",
                           "next_step": "先画结构图"}, now=self.clock.now)
        exp = self.store.record_experience(
            "bilibili:BVskill", "看了机械臂承重的实测。", platform="bilibili",
            observed_level="subtitle", now=self.clock.now)
        self.store.practice("机械臂", experience_id=exp["id"],
                            mode="understanding", now=self.clock.now)
        seen = {}

        async def planner(context):
            seen.update(context)
            return {"action": "skip", "reason": "先看看"}

        auto = self.auto(planner=planner)
        await self.schedule(auto)
        self.due()
        await auto.tick()

        self.assertTrue(builds, "规划必须调用 persona.build")
        self.assertIn("角色上下文", seen["role"])
        self.assertEqual(seen["hobbies"][0]["name"], "鸣潮")
        self.assertTrue(any(skill["topic"] == "机械臂" and
                            skill["understanding_count"] >= 1
                            for skill in seen["skills"]))
        self.assertEqual(seen["goals"][0]["id"], goal["id"])
        self.assertEqual(seen["goals"][0]["version"], 1)
        self.assertIn("把机械臂方案画完", seen["role"])

    async def test_goal_revise_uses_current_version_and_real_experience(self):
        goal = self.store.patch_goal(
            "add", fields={"type": "task", "text": "把机械臂方案画完",
                           "next_step": "先画结构图"}, now=self.clock.now)
        exp_rows = []

        async def planner(context):
            return {"action": "search", "query": "机械臂 承重",
                    "topic": "机械臂", "goal_id": goal["id"]}

        def fake_search(query, config):
            return {"provider": "fake", "results": [{
                "title": "机械臂承重实测", "url": "https://example.test/g",
                "snippet": "结构图要先算受力"}]}

        def fake_fetch(url, max_chars=3000):
            return {"ok": True, "title": "机械臂", "url": url,
                    "text": "受力点在关节处。"}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "reaction": "这个受力点确实得先算。",
            "goal": {"op": "revise", "progress": "已经找到承重计算资料",
                     "next_step": "按受力点重画结构图"},
            "share": False}))
        await self.schedule(auto)
        self.due()
        with patch.object(autonomy_mod.websearch, "search", fake_search), \
                patch.object(autonomy_mod.websearch, "fetch", fake_fetch):
            result = await auto.tick()
        self.assertTrue(result["ok"], result)
        rows = self.store.recent_experiences(5)
        exp_rows.extend(rows)
        self.assertEqual(rows[0]["goal_id"], goal["id"])
        updated = next(row for row in self.store.goals()
                       if row["id"] == goal["id"])
        self.assertEqual(updated["version"], 2)
        self.assertIn("承重计算资料", updated["progress"])
        self.assertEqual(updated["next_step"], "按受力点重画结构图")
        self.assertIn(rows[0]["id"], updated["source_ids"])

    async def test_reading_cannot_complete_a_goal(self):
        goal = self.store.patch_goal(
            "add", fields={"type": "task", "text": "做完机械臂项目"},
            now=self.clock.now)

        async def planner(context):
            return {"action": "search", "query": "机械臂 资料",
                    "goal_id": goal["id"]}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "reaction": "看完了资料。",
            "goal": {"op": "complete", "target_id": goal["id"],
                     "reason": "读到资料了"}}))
        await self.schedule(auto)
        self.due()
        with patch.object(autonomy_mod.websearch, "search",
                          lambda q, c: {"results": [{"title": "资料",
                                                     "url": "https://e.test/x",
                                                     "snippet": "只读资料"}]}), \
                patch.object(autonomy_mod.websearch, "fetch",
                             lambda u, max_chars=3000: {"ok": False}):
            result = await auto.tick()
        self.assertTrue(result["ok"], result)
        updated = next(row for row in self.store.goals()
                       if row["id"] == goal["id"])
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["version"], 2)
        self.assertIn("资料", updated["progress"])

    async def test_facts_summary_comes_from_execution_not_model(self):
        async def planner(context):
            return {"action": "search", "query": "真实资料"}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "summary": "我亲眼看见外星人降临了", "reaction": "标题有点离谱。",
            "share": False}))
        await self.schedule(auto)
        self.due()
        with patch.object(autonomy_mod.websearch, "search",
                          lambda q, c: {"results": [{
                              "title": "普通新闻", "url": "https://e.test/n",
                              "snippet": "一次普通的测试"}]}), \
                patch.object(autonomy_mod.websearch, "fetch",
                             lambda u, max_chars=3000: {
                                 "ok": True, "text": "正文只讲普通测试。"}):
            result = await auto.tick()
        self.assertTrue(result["ok"], result)
        row = self.store.recent_experiences(5)[0]
        self.assertIn("普通测试", row["summary"])
        self.assertNotIn("外星人", row["summary"])
        self.assertEqual(row["reaction"], "标题有点离谱。")

    async def test_bili_source_parameters_reach_discovery_and_round(self):
        async def planner(context):
            return {"action": "bili", "bili_source": "favorites",
                    "query": "机械臂", "uid": 42, "media_id": 7,
                    "bvid": "BVseed"}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "reaction": "收藏里这个看着还行。", "share": False}))
        await self.schedule(auto)
        self.due()
        discovery = SimpleNamespace(discover=AsyncMock(return_value={
            "ok": True, "source": "favorites",
            "items": [{"bvid": "BVfav", "title": "收藏的机械臂"}]}))
        run = AsyncMock(return_value={
            "ok": True, "notes": [{"bvid": "BVfav", "title": "收藏的机械臂",
                                   "url": "https://b23.tv/BVfav",
                                   "gist": "承重", "watched_level": "subtitle"}],
            "subtitle": {}, "reacted": {}})
        with patch.dict(sys.modules, {"pawbot.bili_discovery": discovery}), \
                patch.object(pawbot, "bili_discovery", discovery, create=True), \
                patch.object(autonomy_mod.bili_scroll, "run_round", run):
            result = await auto.tick()
        self.assertTrue(result["ok"], result)
        call = discovery.discover.await_args
        self.assertEqual(call.args[1], "favorites")
        self.assertEqual(call.kwargs["uid"], 42)
        self.assertEqual(call.kwargs["media_id"], 7)
        self.assertEqual(call.kwargs["bvid"], "BVseed")
        self.assertEqual(run.await_args.kwargs["source"], "favorites")
        self.assertEqual(run.await_args.kwargs["candidates"][0]["bvid"], "BVfav")
        row = self.store.recent_experiences(5)[0]
        self.assertIn("来源 favorites", row["summary"])

    async def test_missing_bili_source_falls_back_to_explicit_search(self):
        async def planner(context):
            return {"action": "bili", "bili_source": "following",
                    "query": "机械臂", "uid": 42}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "reaction": "没刷到视频，搜到的这个还行。", "share": False}))
        await self.schedule(auto)
        self.due()
        discovery = SimpleNamespace(discover=AsyncMock(return_value={
            "ok": False, "source": "following", "items": [],
            "reason": "关注列表为空"}))
        run = AsyncMock()
        with patch.dict(sys.modules, {"pawbot.bili_discovery": discovery}), \
                patch.object(pawbot, "bili_discovery", discovery, create=True), \
                patch.object(autonomy_mod.bili_scroll, "run_round", run), \
                patch.object(autonomy_mod.websearch, "search",
                             lambda q, c: {"results": [{
                                 "title": "机械臂新闻", "url": "https://e.test/b",
                                 "snippet": "一条普通新闻"}]}), \
                patch.object(autonomy_mod.websearch, "fetch",
                             lambda u, max_chars=3000: {"ok": False}):
            result = await auto.tick()
        self.assertTrue(result["ok"], result)
        run.assert_not_awaited()
        row = self.store.recent_experiences(5)[0]
        self.assertEqual(row["platform"], "web")
        self.assertTrue(row["source_id"].startswith("web:"))
        self.assertIn("搜了", row["summary"])
        self.assertNotIn("推荐", row["summary"])

    async def test_self_patch_delete_and_merge_need_versions(self):
        first = self.store.patch_self(
            "add", fields={"text": "我做事很急"}, source_ids=[], now=self.clock.now)
        second = self.store.patch_self(
            "add", fields={"text": "我学会先看承重"}, source_ids=[],
            now=self.clock.now + 1)
        third = self.store.patch_self(
            "add", fields={"text": "我不爱解释"}, source_ids=[],
            now=self.clock.now + 2)
        exp = self.store.record_experience(
            "web:merge", "实际做了一次承重测试。", platform="web",
            now=self.clock.now)

        async def reflect(context):
            return {"action": "reflect"}

        auto = self.auto(planner=reflect)
        await self.schedule(auto)
        self.due()
        self.bot.llm = FakeLLM({
            "self_patch": {"op": "merge", "target_id": first["id"],
                           "expected_version": 1,
                           "fields": {"into": second["id"],
                                      "expected_other_version": 1},
                           "source_ids": [exp["id"]], "reason": "经历合并"}})
        result = await auto.tick()
        self.assertTrue(result["ok"], result)
        active = self.store.self_entries()
        texts = [row["text"] for row in active]
        self.assertTrue(any("承重" in text for text in texts))
        merged = next(row for row in self.store.self_entries(active_only=False)
                      if row["id"] == first["id"])
        self.assertEqual(merged["status"], "merged")

        self.clock.advance(2 * 3600 + 1)
        deleted = self.store.self_entries()[0]
        self.bot.llm = FakeLLM({
            "self_patch": {"op": "delete", "target_id": deleted["id"],
                           "expected_version": deleted["version"],
                           "source_ids": [exp["id"]], "reason": "不再成立"}})
        result = await auto.tick()
        self.assertTrue(result["ok"], result)
        self.assertNotIn(deleted["id"],
                         [row["id"] for row in self.store.self_entries()])

        # 缺版本的已有条目修改必须拒绝。
        remaining = self.store.self_entries()[0]
        self.clock.advance(2 * 3600 + 1)
        self.bot.llm = FakeLLM({
            "self_patch": {"op": "revise", "target_id": remaining["id"],
                           "fields": {"text": "没有版本也要改"},
                           "source_ids": [exp["id"]], "reason": "无版本"}})
        result = await auto.tick()
        self.assertTrue(result["ok"], result)
        kept = next(row for row in self.store.self_entries()
                    if row["id"] == remaining["id"])
        self.assertNotEqual(kept["text"], "没有版本也要改")

    async def test_finish_schedules_from_end_time_not_start(self):
        async def planner(context):
            self.clock.advance(600)      # 慢动作：开始后过了十分钟
            return {"action": "skip", "reason": "慢吞吞"}

        auto = self.auto(planner=planner)
        await self.schedule(auto)
        self.due()
        result = await auto.tick()
        self.assertEqual(result["status"], "skipped")
        snap = auto.snapshot()
        self.assertAlmostEqual(snap["next_at"], self.clock.now + 2 * 3600, delta=2)
        self.assertGreaterEqual(snap["last_activity_at"], self.clock.now - 1)

    async def test_recent_conversation_blocks_new_topic_without_blocking_replies(self):
        auto = self.auto()
        self.assertTrue(auto.can_share("group_1"))
        auto.note_conversation("group_1")
        self.assertFalse(auto.can_share("group_1"))
        self.clock.advance(15 * 60 - 1)
        self.assertFalse(auto.can_share("group_1"))
        self.clock.advance(2)
        self.assertTrue(auto.can_share("group_1"))

    async def test_qzone_read_then_reply_uses_real_tid_and_comment_id(self):
        from pawbot.qzone import Qzone

        self.config.update({
            "qzone.enabled": True,
            "qzone.proactive_enabled": True,
            "qzone.actions.feed": "configured_feed",
            "qzone.actions.reply": "configured_reply",
        })

        async def call(action, **kwargs):
            if action == "get_qzone_msg_list":
                return {"list": [{"tid": "t1", "content": "我自己发的说说"}]}
            if action == "get_qzone_comments":
                return {"comments": [{"commentid": "c1", "content": "一条留言"}]}
            if action == "configured_feed":
                return {"feeds": [{"tid": "f1", "content": "好友动态"}]}
            if action == "configured_reply":
                return {"ok": True, "data": {"done": True}}
            return {}

        conn = SimpleNamespace(call=AsyncMock(side_effect=call))
        self.bot.conn_for = lambda: conn
        self.bot.qzone = Qzone(self.bot)
        rounds = []

        async def planner(context):
            rounds.append(context)
            items = context["qzone_items"]
            if not items:
                return {"action": "qzone_read", "qzone_source": "own"}
            item = items[-1]
            return {"action": "qzone_interact", "qzone_interact": "reply",
                    "tid": item["tid"],
                    "comment_id": item["comments"][0]["id"],
                    "content": "回你一句"}

        auto = self.auto(planner=planner, llm=FakeLLM({
            "reaction": "这些都是真实看到的。", "share": False}))
        await self.schedule(auto)
        self.due()
        first = await auto.tick()
        self.assertTrue(first["ok"], first)
        self.assertEqual(rounds[0]["qzone_sources"], ["own", "feed"])
        self.assertIn("qzone_interact", rounds[0]["available_actions"])
        self.clock.advance(2 * 3600 + 1)
        second = await auto.tick()
        self.assertTrue(second["ok"], second)
        self.assertEqual(conn.call.await_args.args[0], "configured_reply")
        self.assertEqual(conn.call.await_args.kwargs["tid"], "t1")
        self.assertEqual(conn.call.await_args.kwargs["comment_id"], "c1")
        self.assertEqual(self.bot.qzone.records[-1]["action"], "reply")
        rows = self.store.recent_experiences(5)
        self.assertTrue(any("回复" in row["summary"] for row in rows))

    async def test_disabled_autonomy_never_runs(self):
        self.config["life.autonomy_enabled"] = False

        async def planner(context):
            raise AssertionError("disabled autonomy must not plan")

        auto = self.auto(planner=planner)
        result = await auto.tick()
        self.assertEqual(result["reason"], "autonomy-disabled")
        self.assertFalse(os.path.exists(auto.state_path))

    async def test_ha_denial_keeps_clock_without_execution(self):
        self.bot.ha_runtime = SimpleNamespace(can_mutate=lambda: False)
        calls = []

        async def planner(context):
            calls.append(1)
            return {"action": "skip"}

        auto = self.auto(planner=planner)
        result = await auto.tick()
        self.assertEqual(result["reason"], "ha-not-admitted")
        self.assertEqual(calls, [])


    async def test_dsh_plan_and_react_use_life_model_helper(self):
        """provider=dsh 时必须走 life_model.chat 的 brain 轮次，而不是 mock 直连。"""
        self.config.update({
            "llm.provider": "dsh",
            "llm.brain_timeout": 5,
            "llm.base_url": "https://api.example.invalid/v1",
            "llm.api_key": "",
            "llm.max_tokens": 900,
            "llm.temperature": 0.8,
            "llm.timeout": 30,
        })
        self.bot.llm = LLMClient(self.config)
        payloads = []

        def fake_turn(payload, timeout=None):
            payloads.append(payload)
            if "自主时间" in str(payload.get("system") or ""):
                text = {"action": "search", "query": "机械臂 承重",
                        "topic": "机械臂", "reason": "想看看真实案例"}
            else:
                text = {"reaction": "看完只觉得承重真是麻烦事。",
                        "topics": ["机械臂"], "share": False}
            return {"text": json.dumps(text, ensure_ascii=False)}

        def fake_search(query, config):
            return {"provider": "fake", "results": [{
                "title": "机械臂承重实测", "url": "https://example.test/a",
                "snippet": "关节处受力会打滑"}]}

        auto = self.auto(llm=self.bot.llm)
        await self.schedule(auto)
        self.due()
        with patch.object(brain, "_URL", "http://127.0.0.1:50311/turn"), \
                patch.object(brain, "_TOKEN", "pdbrain_" + "d" * 64), \
                patch.object(brain, "turn", side_effect=fake_turn), \
                patch.object(autonomy_mod.websearch, "search", fake_search), \
                patch.object(autonomy_mod.websearch, "fetch",
                             lambda url, max_chars=3000: {"ok": False}):
            result = await auto.tick()

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(payloads), 2)
        self.assertTrue(payloads[0]["conv"].startswith("life:autonomy-plan:"))
        self.assertTrue(payloads[1]["conv"].startswith("life:autonomy-react:"))
        for payload in payloads:
            self.assertEqual(payload["tools"], [])
            self.assertEqual(payload["context"], {
                "autonomous": True, "native_tools": False,
                "is_owner": False, "computer_access": False})
            self.assertEqual(payload["options"]["reasoningEffort"], "low")
        self.assertIn("机械臂承重实测", payloads[1]["user"])
        rows = self.store.recent_experiences(5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reaction"], "看完只觉得承重真是麻烦事。")

    async def test_dsh_helper_rejection_is_not_written_as_experience(self):
        """helper 拒绝 mock/空输出时只能失败退避，不能伪造经历。"""
        self.config.update({"llm.provider": "dsh", "llm.brain_timeout": 5,
                            "llm.base_url": "https://api.example.invalid/v1",
                            "llm.api_key": ""})
        self.bot.llm = LLMClient(self.config)
        auto = self.auto(llm=self.bot.llm)
        await self.schedule(auto)
        self.due()
        with patch.object(brain, "_URL", "http://127.0.0.1:50311/turn"), \
                patch.object(brain, "_TOKEN", "pdbrain_" + "d" * 64), \
                patch.object(brain, "turn",
                             return_value={"text": "（mock 模式，未配置 api_key）"}):
            result = await auto.tick()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.recent_experiences(5), [])
        self.assertEqual(auto.snapshot()["failure_count"], 1)


class ProactiveAutonomyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = os.path.join(self.tmp.name, "proactive.json")

    def bot(self, **overrides):
        cfg = {"proactive.enabled": False, "proactive.groups": [],
               "proactive.state_file": self.state_file}
        cfg.update(overrides.pop("config", {}))
        return SimpleNamespace(config=cfg, **overrides)

    async def test_tick_runs_autonomy_before_disabled_group_gate(self):
        tick = AsyncMock()
        bot = self.bot(autonomy=SimpleNamespace(tick=tick))
        pro = Proactive(bot)
        await pro._tick()
        self.assertIsNotNone(pro._autonomy_task)
        await pro._autonomy_task
        tick.assert_awaited_once()

    async def _slow_autonomy(self, planner):
        paths = layout.from_data_root(self.tmp.name, base_dir=self.tmp.name)
        clock = Clock()
        config = {"life.autonomy_enabled": True,
                  "life.activity_min_hours": 2,
                  "life.activity_max_hours": 4}
        auto_bot = SimpleNamespace(config=config, paths=paths,
                                   base_dir=self.tmp.name)
        auto = Autonomy(auto_bot, now=clock, planner=planner, rng=FixedRng())
        await auto.tick()
        clock.advance(2 * 3600 + 1)
        return auto, clock, self.bot(autonomy=auto)

    async def test_slow_activity_does_not_block_tick_or_spawn_twice(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def planner(context):
            calls.append(1)
            started.set()
            await release.wait()
            return {"action": "skip", "reason": "慢动作"}

        auto, _clock, bot = await self._slow_autonomy(planner)
        pro = Proactive(bot)
        await asyncio.wait_for(pro._tick(), 1)     # 不等待几十秒的活动
        await asyncio.wait_for(started.wait(), 1)
        self.assertIsNotNone(pro._autonomy_task)
        self.assertFalse(pro._autonomy_task.done())

        await asyncio.wait_for(pro._tick(), 1)     # 并发 tick 不能再起一个
        self.assertEqual(len(calls), 1)

        release.set()
        await asyncio.wait_for(pro._autonomy_task, 1)
        self.assertEqual(len(calls), 1)
        snap = auto.snapshot()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["last_status"], "skipped")

    async def test_stop_cancels_activity_abandons_it_and_leaves_no_late_write(self):
        started = asyncio.Event()
        never = asyncio.Event()
        calls = []

        async def planner(context):
            calls.append(1)
            started.set()
            await never.wait()

        auto, _clock, bot = await self._slow_autonomy(planner)
        pro = Proactive(bot)
        await asyncio.wait_for(pro._tick(), 1)
        await asyncio.wait_for(started.wait(), 1)
        self.assertTrue(auto.snapshot()["running"])

        await asyncio.wait_for(pro.stop(), 2)
        snap = auto.snapshot()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["activity"]["status"], "abandoned")
        self.assertIsNone(pro._autonomy_task)

        before = Path(auto.state_path).read_bytes()
        await asyncio.sleep(0.05)
        self.assertEqual(Path(auto.state_path).read_bytes(), before)
        # 停机后即使有人再调 tick，也不允许偷偷派发新活动。
        await pro._tick()
        await asyncio.sleep(0)
        self.assertIsNone(pro._autonomy_task)
        self.assertEqual(len(calls), 1)

    async def test_autonomy_subtask_keeps_legacy_scope_owner_and_cleans_up(self):
        hold = asyncio.Event()

        async def tick():
            await hold.wait()

        bot = self.bot(autonomy=SimpleNamespace(tick=tick))
        pro = Proactive(bot)
        with tasks_mod.scope("proactive-scope"):
            await pro._tick()
            task = pro._autonomy_task
            self.assertEqual(tasks_mod.owner_of(task), "proactive-scope")
        hold.set()
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, tasks_mod.pending())

    async def test_dsh_compose_prepares_readonly_search_then_uses_helper(self):
        cfg = {
            "llm.provider": "dsh",
            "llm.brain_timeout": 5,
            "llm.base_url": "https://api.example.invalid/v1",
            "llm.api_key": "",
            "tools.enabled": True,
            "tools.max_rounds": 2,
            "life.enabled": False,
        }
        bot = SimpleNamespace(
            config=cfg,
            topic=SimpleNamespace(digest=lambda *a: "机械臂"),
            mood=SimpleNamespace(hint=lambda: ""),
            persona=SimpleNamespace(build=lambda **kw: "系统"),
            store=SimpleNamespace(summary=lambda *a: "",
                                  turn_summary=lambda *a: "",
                                  window=lambda *a: []),
            toolbox=SimpleNamespace(read_only_schemas=lambda: []),
        )
        bot.llm = LLMClient(cfg)
        payloads = []

        def fake_turn(payload, timeout=None):
            payloads.append(payload)
            return {"text": "刚看到有人拿机械臂改扫地机，承重还是老问题。"}

        def fake_search(query, config):
            return {"provider": "fake", "results": [{
                "title": "机械臂改装实录", "url": "https://example.test/s",
                "snippet": "关节承重会打滑"}]}

        with patch.object(brain, "_URL", "http://127.0.0.1:50311/turn"), \
                patch.object(brain, "_TOKEN", "pdbrain_" + "d" * 64), \
                patch.object(brain, "turn", side_effect=fake_turn), \
                patch.object(proactive_mod.websearch, "search", fake_search):
            text, media = await Proactive(bot)._compose(
                1, "group_1", {"kind": "search", "query": "机械臂 改装"})

        self.assertEqual(text, "刚看到有人拿机械臂改扫地机，承重还是老问题。")
        self.assertEqual(media, [])
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["context"], {
            "autonomous": True, "native_tools": False,
            "is_owner": False, "computer_access": False})
        combined = str(payload.get("system") or "") + str(payload.get("user") or "")
        self.assertIn("机械臂改装实录", combined)
        self.assertNotIn("先调 web_search", payload["system"])

    async def test_quota_uses_autonomy_cooldown(self):
        bot = self.bot(autonomy=SimpleNamespace(can_share=lambda sid: False))
        ok, why = Proactive(bot)._quota_ok("group_1")
        self.assertFalse(ok)
        self.assertIn("分享冷却", why)

    async def test_note_spoken_counts_then_notes_shared(self):
        note = unittest.mock.Mock()
        bot = self.bot(autonomy=SimpleNamespace(note_shared=note))
        pro = Proactive(bot)
        pro._note_spoken("group_1", "exp_1")
        self.assertEqual(pro._load_state()["counts"]["group_1"], 1)
        note.assert_called_once_with("group_1", "exp_1")

    async def test_quota_blocks_new_topic_after_recent_conversation(self):
        clock = Clock()
        paths = layout.from_data_root(self.tmp.name, base_dir=self.tmp.name)
        auto = Autonomy(SimpleNamespace(config={}, paths=paths), now=clock)
        auto.note_conversation("group_1")
        bot = self.bot(
            config={"life.share_gap_hours": 0, "life.global_share_gap_hours": 0,
                    "life.recent_conversation_gap_min": 15,
                    "proactive.state_file": os.path.join(self.tmp.name,
                                                          "proactive.json")},
            autonomy=auto)
        ok, why = Proactive(bot)._quota_ok("group_1")
        self.assertFalse(ok)
        self.assertIn("分享冷却", why)
        clock.advance(16 * 60)
        self.assertTrue(Proactive(bot)._quota_ok("group_1")[0])

    async def test_choose_prefers_relevant_experience_candidate(self):
        candidate = {"experience_id": "exp_1", "summary": "机械臂承重实测"}
        bot = self.bot(
            topic=SimpleNamespace(digest=lambda *a: "机械臂", keywords=lambda *a: []),
            autonomy=SimpleNamespace(share_candidate=lambda *a: candidate))
        action = await Proactive(bot)._choose("group_1")
        self.assertEqual(action["kind"], "experience")
        self.assertIs(action["experience"], candidate)

    async def test_compose_includes_experience_material(self):
        seen = []

        async def chat(messages, tools=None, **kwargs):
            seen.append(messages)
            return {"content": "说起来那个承重确实不行"}

        bot = self.bot(
            config={"tools.enabled": False, "tools.max_rounds": 0,
                    "life.enabled": False},
            topic=SimpleNamespace(digest=lambda *a: "机械臂"),
            mood=SimpleNamespace(hint=lambda: ""),
            persona=SimpleNamespace(build=lambda **kw: "系统"),
            store=SimpleNamespace(summary=lambda *a: "", turn_summary=lambda *a: "",
                                  window=lambda *a: []),
            llm=SimpleNamespace(chat_message=chat))
        text, media = await Proactive(bot)._compose(1, "group_1", {
            "kind": "experience",
            "experience": {"experience_id": "exp_1", "summary": "机械臂承重实测",
                           "share_point": "会打滑", "topics": ["机械臂"]},
        })
        self.assertEqual(text, "说起来那个承重确实不行")
        self.assertEqual(media, [])
        self.assertIn("机械臂承重实测", json.dumps(seen, ensure_ascii=False))

    async def test_old_bili_report_obeys_and_updates_shared_cooldown(self):
        class FakeBot:
            def __init__(self, allowed):
                self.config = {"tools.bili_scroll_report_to": "group",
                               "tools.bili_scroll_group": "123",
                               "account.owner_ids": []}
                self.sent = []
                self.autonomy = SimpleNamespace(
                    can_share=lambda sid: allowed,
                    note_shared=unittest.mock.Mock())
                self.persona = SimpleNamespace(build=lambda **kw: "system")
                self.llm = SimpleNamespace()

            async def manual_send(self, target, message, is_group=True):
                self.sent.append((target, message, is_group))

        notes = {"checked": 3, "notes": [{"title": "片子", "gist": "讲的",
                                          "url": "https://b23.tv/a"}]}
        blocked = FakeBot(False)
        await bili_scroll._report(blocked, notes, "12:00")
        self.assertEqual(blocked.sent, [])
        blocked.autonomy.note_shared.assert_not_called()

        allowed = FakeBot(True)
        await bili_scroll._report(allowed, notes, "12:00")
        self.assertEqual(len(allowed.sent), 1)
        allowed.autonomy.note_shared.assert_called_once_with("group_123")


if __name__ == "__main__":
    unittest.main()
