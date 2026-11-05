"""LifeStore v1 的真实持久化测试（临时目录，不 mock 被测 Store）。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.life_store import (
    LifeDataError, LifeStore, LifeStoreError, _negation_view, _similarity)
from pawbot.paths import DataPaths


class LifeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.paths = DataPaths(root=self.root, base_dir=self.root)
        self.paths.ensure_layout()
        self.config = {"life.max_active_goals": 8}
        self.store = LifeStore(self.root, self.config, paths=self.paths)

    def tearDown(self):
        self.tmp.cleanup()

    def raw(self):
        with open(self.store.path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def add_exp(self, source, summary, **kwargs):
        return self.store.record_experience(source, summary, **kwargs)

    # ---------------------------------------------------------------- 构造/重载
    def test_construct_is_empty_and_does_not_create_story(self):
        fresh = tempfile.TemporaryDirectory()
        try:
            paths = DataPaths(root=fresh.name, base_dir=fresh.name)
            paths.ensure_layout()
            store = LifeStore(fresh.name, {}, paths=paths)
            self.assertFalse(os.path.exists(store.path))
            self.assertEqual(store.goals(), [])
            self.assertEqual(store.goals(active_only=False), [])
            self.assertEqual(store.self_entries(), [])
            self.assertEqual(store.recent_experiences(), [])
            self.assertEqual(store.context_for("随便聊聊"), "")
        finally:
            fresh.cleanup()

    def test_experience_source_dedupe_reload_and_deepcopy(self):
        first = self.add_exp("bili:1", "看了一个机械臂视频", platform="bilibili",
                             topics=["机械臂"], reaction="想试试")
        again = self.add_exp("bili:1", "重复回执不应该算第二条")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.store.recent_experiences()), 1)
        self.assertEqual(again["summary"], "看了一个机械臂视频")
        self.assertEqual(again["root_source_id"], "bili:1")

        returned = self.store.recent_experiences()
        returned[0]["summary"] = "被调用方改掉"
        self.assertEqual(self.store.recent_experiences()[0]["summary"],
                         "看了一个机械臂视频")

        second = LifeStore(self.root, self.config, paths=self.paths)
        second.reload()
        self.assertEqual(len(second.recent_experiences()), 1)
        self.add_exp("search:2", "查了扫地机器人")
        self.assertEqual(len(second.recent_experiences()), 2)
        self.store.reload()
        self.assertEqual(len(self.store.recent_experiences()), 2)

    def test_corrupt_or_unknown_state_never_silently_resets(self):
        bad = tempfile.TemporaryDirectory()
        try:
            paths = DataPaths(root=bad.name, base_dir=bad.name)
            paths.ensure_layout()
            target = paths.content("life/state.json")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(LifeStoreError):
                LifeStore(bad.name, {}, paths=paths)

            with open(target, "w", encoding="utf-8") as handle:
                json.dump({"version": 99, "experiences": []}, handle)
            with self.assertRaises(LifeStoreError):
                LifeStore(bad.name, {}, paths=paths)

            with open(target, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "experiences": [1]}, handle)
            with self.assertRaises(LifeStoreError):
                LifeStore(bad.name, {}, paths=paths)
        finally:
            bad.cleanup()

    # ---------------------------------------------------------------- 记忆
    def test_memory_source_validation_synonym_dedupe_and_confirmation(self):
        exp = self.add_exp("chat:1", "主人说自己喜欢猫", platform="qq")

        with self.assertRaises(LifeDataError):
            self.store.remember("无来源事实", source_ids=["missing-exp"])

        first = self.store.remember(
            "主人喜欢猫", kind="semantic", subject="主人",
            source_ids=[exp["id"]], confidence=0.6)
        same = self.store.remember(
            "主人 喜欢猫。", kind="semantic", subject="主人",
            source_ids=[exp["id"]], confidence=0.9)
        self.assertEqual(first["id"], same["id"])
        self.assertEqual(same["confidence"], 0.6, "同一来源重复写入不能提升证据")
        self.assertEqual(len(self.raw()["memories"]), 1)

        confirmed = self.store.remember(
            "主人喜欢猫", kind="semantic", subject="主人",
            source_ids=[exp["id"]], confirmed=True)
        self.assertEqual(confirmed["status"], "confirmed")
        recalled = self.store.recall("猫")
        self.assertEqual([row["id"] for row in recalled], [first["id"]])

    def test_conflict_not_silently_overwritten_then_confirmation_wins(self):
        left = self.add_exp("chat:10", "主人提到喜欢猫")
        right = self.add_exp("chat:11", "主人后来改口")
        old = self.store.remember("主人喜欢猫", subject="主人",
                                  source_ids=[left["id"]])
        candidate = self.store.recall("猫")
        self.assertEqual(candidate[0]["status"], "candidate")

        conflict = self.store.remember("主人不喜欢猫", subject="主人",
                                       source_ids=[right["id"]])
        self.assertEqual(conflict["status"], "conflicted")
        self.assertEqual(self.store.recall("猫"), [],
                         "未确认的冲突默认不应该当确定事实注入")

        fixed = self.store.remember("主人不喜欢猫", subject="主人",
                                    source_ids=[right["id"]], confirmed=True)
        self.assertEqual(fixed["status"], "confirmed")
        rows = {row["id"]: row for row in self.raw()["memories"]}
        self.assertEqual(rows[old["id"]]["status"], "superseded")
        self.assertEqual(rows[fixed["id"]]["status"], "confirmed")
        recalled = self.store.recall("猫")
        self.assertEqual([row["id"] for row in recalled], [fixed["id"]])

    def test_unresolved_conflict_propagates_to_related_paraphrase(self):
        first_exp = self.add_exp("chat:12", "主人最早说喜欢猫")
        second_exp = self.add_exp("chat:13", "主人后来改口说不喜欢猫")
        third_exp = self.add_exp("chat:14", "主人现在又说喜欢猫")
        first = self.store.remember(
            "主人喜欢猫", subject="主人", source_ids=[first_exp["id"]])
        second = self.store.remember(
            "主人不喜欢猫", subject="主人", source_ids=[second_exp["id"]])
        self.assertEqual(second["status"], "conflicted")

        # 第三条用不同来源、换了时间状语，仍是同一命题的近义表达；未解决
        # 冲突必须传播到它，不能让它以“干净 candidate”绕开冲突图被召回。
        third = self.store.remember(
            "主人现在喜欢猫", subject="主人", source_ids=[third_exp["id"]])
        self.assertNotEqual(third["id"], first["id"])
        self.assertNotEqual(third["id"], second["id"])
        self.assertEqual(third["status"], "conflicted")
        self.assertIn(first["id"], third["conflict_with"])
        self.assertIn(second["id"], third["conflict_with"])
        self.assertEqual(self.store.recall("猫"), [])

        # 明确 confirmed 仍然是解决冲突的唯一出口，并结束整组旧冲突。
        fixed = self.store.remember(
            "主人现在喜欢猫", subject="主人", source_ids=[third_exp["id"]],
            confirmed=True)
        self.assertEqual(fixed["status"], "confirmed")
        self.assertEqual(fixed["id"], third["id"])
        rows = {row["id"]: row for row in self.raw()["memories"]}
        self.assertEqual(rows[first["id"]]["status"], "superseded")
        self.assertEqual(rows[second["id"]]["status"], "superseded")
        self.assertEqual(rows[third["id"]]["status"], "confirmed")
        self.assertEqual([row["id"] for row in self.store.recall("猫")],
                         [third["id"]])

    def test_recall_threshold_budget_and_mention_is_separate(self):
        exp = self.add_exp("chat:20", "听到海边的风声")
        row = self.store.remember("海边的风很舒服", source_ids=[exp["id"]])
        self.add_exp("chat:21", "研究机械臂结构")
        self.store.remember("机械臂项目还没做完", source_ids=[
            self.store.recent_experiences()[0]["id"]])

        self.assertEqual(self.store.recall("量子力学"), [])
        hit = self.store.recall("海边", budget=4)
        self.assertEqual(len(hit), 1)
        self.assertLessEqual(len(hit[0]["text"]), 4)
        self.assertTrue(hit[0].get("truncated"))

        before = {m["id"]: m for m in self.raw()["memories"]}[row["id"]]
        self.store.mark_mentioned([row["id"]], surface="群聊")
        after = {m["id"]: m for m in self.raw()["memories"]}[row["id"]]
        self.assertGreater(after["last_mentioned_at"], 0)
        self.assertEqual(after["confidence"], before["confidence"])
        self.assertEqual(after["version"], before["version"])

        mentioned_at = after["last_mentioned_at"]
        self.store.recall("海边")
        later = {m["id"]: m for m in self.raw()["memories"]}[row["id"]]
        self.assertGreater(later["last_retrieved_at"], 0)
        self.assertEqual(later["last_mentioned_at"], mentioned_at)
        self.assertEqual(later["confidence"], before["confidence"])

    def test_memory_superseded_same_source_replay_never_revives_expired_version(self):
        left = self.add_exp("chat:70", "主人最早说喜欢猫")
        right = self.add_exp("chat:71", "主人后来确认不喜欢猫")
        older = self.store.remember(
            "主人喜欢猫", subject="主人", source_ids=[left["id"]],
            confirmed=True, confidence=0.6)
        newer = self.store.remember(
            "主人不喜欢猫", subject="主人", source_ids=[right["id"]],
            confirmed=True, confidence=0.7)
        rows = {row["id"]: row for row in self.raw()["memories"]}
        self.assertEqual(rows[older["id"]]["status"], "superseded")
        self.assertIsNotNone(rows[older["id"]]["valid_until"])
        self.assertEqual(rows[newer["id"]]["status"], "confirmed")
        self.assertIsNone(rows[newer["id"]]["valid_until"])
        expired_at = rows[older["id"]]["valid_until"]
        snapshot = dict(rows[older["id"]])

        # A（confirmed）→ B（confirmed，A 被结束）→ 同一来源重放 A：不能复活
        # 过期版本，也不能把 B 再压掉。
        replay = self.store.remember(
            "主人喜欢猫", subject="主人", source_ids=[left["id"]],
            confirmed=True, confidence=0.95)
        self.assertEqual(replay["id"], older["id"])
        self.assertEqual(replay["status"], "superseded")
        rows = {row["id"]: row for row in self.raw()["memories"]}
        self.assertEqual(rows[older["id"]]["valid_until"], expired_at)
        self.assertEqual(rows[older["id"]]["version"], snapshot["version"])
        self.assertEqual(rows[older["id"]]["updated_at"], snapshot["updated_at"])
        self.assertEqual(rows[older["id"]]["source_ids"], snapshot["source_ids"])
        self.assertEqual(rows[older["id"]]["confidence"], snapshot["confidence"])
        self.assertEqual(rows[newer["id"]]["status"], "confirmed")
        self.assertEqual([row["id"] for row in self.store.recall("猫")],
                         [newer["id"]])

        # 真正的新来源纠正 A：创建新的有效版本并结束 B，而不是原地复活旧 A。
        third = self.add_exp("chat:72", "主人再次确认喜欢猫")
        corrected = self.store.remember(
            "主人喜欢猫", subject="主人", source_ids=[third["id"]],
            confirmed=True, confidence=0.8)
        self.assertNotEqual(corrected["id"], older["id"])
        self.assertEqual(corrected["status"], "confirmed")
        self.assertGreater(corrected["valid_from"], 0)
        self.assertIsNone(corrected["valid_until"])
        rows = {row["id"]: row for row in self.raw()["memories"]}
        self.assertEqual(rows[older["id"]]["status"], "superseded")
        self.assertEqual(rows[newer["id"]]["status"], "superseded")
        self.assertIsNotNone(rows[newer["id"]]["valid_until"])
        self.assertEqual([row["id"] for row in self.store.recall("猫")],
                         [corrected["id"]])

    def test_long_sentence_semantic_negation_is_not_a_duplicate(self):
        positive_text = (
            "主人今天在群里说自己很喜欢吃海边的烤鱼，也喜欢傍晚一个人去码头看日落")
        negative_text = (
            "主人今天在群里说自己不喜欢吃海边的烤鱼，也不喜欢傍晚一个人去码头看日落")
        positive_exp = self.add_exp("chat:80", "长句正向事实")
        negative_exp = self.add_exp("chat:81", "长句否定纠正")
        older = self.store.remember(
            positive_text, subject="主人", source_ids=[positive_exp["id"]],
            confirmed=True, confidence=0.6)
        older_snapshot = dict(older)

        # 未确认的否定只登记冲突，不能因为相似度高被当作重复而吞掉。
        negative = self.store.remember(
            negative_text, subject="主人", source_ids=[negative_exp["id"]],
            confidence=0.95)
        self.assertNotEqual(negative["id"], older["id"])
        self.assertIn(negative["status"], {"candidate", "conflicted"})
        rows = {row["id"]: row for row in self.raw()["memories"]}
        self.assertEqual(rows[older["id"]]["status"], "confirmed")
        self.assertEqual(rows[older["id"]]["confidence"],
                         older_snapshot["confidence"])
        self.assertEqual(rows[older["id"]]["text"], positive_text)
        self.assertEqual([row["id"] for row in self.store.recall("烤鱼")],
                         [older["id"]])

        # 明确确认后，否定事实作为新有效版本结束旧正向版本。
        fixed = self.store.remember(
            negative_text, subject="主人", source_ids=[negative_exp["id"]],
            confirmed=True, confidence=0.9)
        self.assertEqual(fixed["status"], "confirmed")
        rows = {row["id"]: row for row in self.raw()["memories"]}
        self.assertEqual(rows[older["id"]]["status"], "superseded")
        self.assertTrue(rows[older["id"]]["valid_until"])
        self.assertEqual(rows[fixed["id"]]["status"], "confirmed")
        self.assertIsNone(rows[fixed["id"]]["valid_until"])
        self.assertEqual([row["id"] for row in self.store.recall("烤鱼")],
                         [fixed["id"]])

    def test_clause_scoped_negation_does_not_merge_high_similarity_text(self):
        old_text = (
            "主人喜欢在天气晴朗的周末早上和朋友一起去附近海边散步拍照观察潮汐"
            "记录海鸟并且在回家的路上买咖啡和早餐然后整理照片分享给家人；"
            "主人习惯晚上读书整理当天拍摄的照片并认真记录当天的行程和心得"
            "然后把这些内容分享给一起旅行的朋友")
        new_text = old_text.replace("主人喜欢", "主人不喜欢", 1).replace(
            "主人习惯", "主人不习惯", 1)
        self.assertGreaterEqual(_similarity(old_text, new_text), 0.95)

        # 两个独立分句各有一个否定，不能按全句偶数抵消成肯定。
        self.assertTrue(_negation_view(new_text)[1])
        self.assertTrue(_negation_view("主人不喜欢猫，也不喜欢狗")[1])
        # 只有同一谓词内紧邻的双重否定才抵消。
        self.assertFalse(_negation_view("主人不可能不喜欢猫")[1])
        self.assertFalse(_negation_view("主人不是不喜欢猫")[1])

        old_exp = self.add_exp("chat:82", "长句正向偏好")
        new_exp = self.add_exp("chat:83", "两处分句否定纠正")
        older = self.store.remember(
            old_text, subject="主人", source_ids=[old_exp["id"]],
            confirmed=True, confidence=0.6)
        snapshot = dict(older)
        changed = self.store.remember(
            new_text, subject="主人", source_ids=[new_exp["id"]],
            confidence=0.95)
        self.assertNotEqual(changed["id"], older["id"])
        self.assertIn(changed["status"], {"candidate", "conflicted"})
        self.assertIn(older["id"], changed["conflict_with"])
        rows = {row["id"]: row for row in self.raw()["memories"]}
        self.assertEqual(rows[older["id"]]["status"], "confirmed")
        self.assertEqual(rows[older["id"]]["confidence"],
                         snapshot["confidence"])
        self.assertEqual(rows[older["id"]]["text"], old_text)

    def test_unresolved_conflict_propagation_follows_explicit_edges(self):
        def exp(source, summary):
            return self.add_exp(source, summary)["id"]

        cat_positive = self.store.remember(
            "主人喜欢猫", subject="主人", source_ids=[exp("chat:90", "猫正向")])
        cat_negative = self.store.remember(
            "主人不喜欢猫", subject="主人", source_ids=[exp("chat:91", "猫否定")])
        coffee_positive = self.store.remember(
            "主人喜欢咖啡", subject="主人",
            source_ids=[exp("chat:92", "咖啡正向")])
        coffee_negative = self.store.remember(
            "主人不喜欢咖啡", subject="主人",
            source_ids=[exp("chat:93", "咖啡否定")])
        cat_ids = {cat_positive["id"], cat_negative["id"]}
        coffee_ids = {coffee_positive["id"], coffee_negative["id"]}
        self.assertEqual(len(cat_ids | coffee_ids), 4)

        # 咖啡冲突和猫冲突同 subject/kind 但互不相连，先记下猫簇快照。
        cat_before = {row["id"]: row for row in self.raw()["memories"]
                      if row["id"] in cat_ids}

        third = self.store.remember(
            "主人现在喜欢咖啡", subject="主人",
            source_ids=[exp("chat:94", "咖啡近义改写")])
        self.assertEqual(third["status"], "conflicted")
        self.assertEqual(set(third["conflict_with"]), coffee_ids)
        self.assertTrue(cat_ids.isdisjoint(third["conflict_with"]))

        rows = {row["id"]: row for row in self.raw()["memories"]}
        for cat_id in cat_ids:
            for key in ("status", "version", "updated_at", "conflict_with",
                        "confidence"):
                self.assertEqual(rows[cat_id][key], cat_before[cat_id][key],
                                 (cat_id, key))

    # ---------------------------------------------------------------- 目标
    def test_goal_cas_preserves_other_entries_and_delete_is_tombstone(self):
        exp = self.add_exp("chat:30", "主人委托整理照片")
        one = self.store.patch_goal(
            "add", fields={"type": "task", "text": "整理海边照片"},
            source_ids=[exp["id"]])
        two = self.store.patch_goal(
            "add", fields={"type": "wish", "text": "修好机械臂"})
        self.assertTrue(one["ok"] and two["ok"])
        goal_id = one["id"]

        revised = self.store.patch_goal(
            "revise", target_id=goal_id, expected_version=1,
            fields={"next_step": "先挑十张"}, reason="开始整理")
        self.assertTrue(revised["ok"])
        self.assertEqual(revised["version"], 2)

        stale = self.store.patch_goal(
            "revise", target_id=goal_id, expected_version=1,
            fields={"text": "旧版本覆盖"})
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"], "version-conflict")
        self.assertEqual(stale["version"], 2)
        goals = {g["id"]: g for g in self.store.goals()}
        self.assertEqual(goals[goal_id]["next_step"], "先挑十张")
        self.assertEqual(goals[two["id"]]["text"], "修好机械臂")

        deleted = self.store.patch_goal(
            "delete", target_id=goal_id, expected_version=2, reason="不做了")
        self.assertTrue(deleted["ok"])
        self.assertNotIn(goal_id, [g["id"] for g in self.store.goals()])
        self.assertNotIn(goal_id, [g["id"] for g in self.store.goals(active_only=False)])
        self.assertNotIn("整理海边照片", json.dumps(self.raw(), ensure_ascii=False))
        self.assertFalse(self.store.patch_goal(
            "revise", target_id=goal_id, fields={"text": "复活"}).get("ok"))

        reloaded = LifeStore(self.root, self.config, paths=self.paths)
        self.assertFalse(reloaded.patch_goal(
            "revise", target_id=goal_id, fields={"text": "复活"}).get("ok"))

    def test_goal_existing_ops_require_version_and_leave_other_entries(self):
        one = self.store.patch_goal(
            "add", fields={"type": "task", "text": "整理海边照片"})
        two = self.store.patch_goal(
            "add", fields={"type": "wish", "text": "修好机械臂"})
        before = self.raw()
        for op in ("revise", "complete", "pause", "abandon", "delete"):
            fields = {"next_step": "旧快照"} if op == "revise" else None
            result = self.store.patch_goal(
                op, target_id=one["id"], fields=fields, reason="旧快照覆盖")
            self.assertFalse(result["ok"], op)
            self.assertEqual(result["error"], "version-required", op)
            self.assertEqual(result["version"], 1, op)
        self.assertEqual(self.raw(), before,
                         "没有版本号的修改不能写盘，其他条目也要原样保留")
        goals = {row["id"]: row for row in self.store.goals(active_only=False)}
        self.assertEqual(goals[one["id"]]["text"], "整理海边照片")
        self.assertEqual(goals[two["id"]]["text"], "修好机械臂")

    def test_self_existing_ops_require_version_and_merge_has_dual_cas(self):
        first = self.store.patch_self(
            "add", fields={"category": "habit", "text": "早起先看消息"})
        second = self.store.patch_self(
            "add", fields={"category": "interest", "text": "喜欢收集贴纸"})
        before = self.raw()
        for op in ("revise", "retire", "delete"):
            fields = {"text": "旧快照覆盖"} if op == "revise" else None
            result = self.store.patch_self(
                op, target_id=first["id"], fields=fields, reason="旧快照覆盖")
            self.assertFalse(result["ok"], op)
            self.assertEqual(result["error"], "version-required", op)
            self.assertEqual(result["version"], 1, op)
        # merge 必须同时给出另一条目的版本，不能省略。
        merged = self.store.patch_self(
            "merge", target_id=first["id"], expected_version=1,
            fields={"into": second["id"]})
        self.assertFalse(merged["ok"])
        self.assertEqual(merged["error"], "version-required")
        self.assertEqual(self.raw(), before,
                         "失败的双对象 CAS 不能改任何一边")

        # 另一条目版本对不上：整体拒绝，两边版本和正文都不变。
        destination_stale = self.store.patch_self(
            "merge", target_id=first["id"], expected_version=1,
            fields={"into": second["id"], "expected_other_version": 9})
        self.assertFalse(destination_stale["ok"])
        self.assertEqual(destination_stale["error"], "version-conflict")
        self.assertEqual(destination_stale["id"], second["id"])
        self.assertEqual(self.raw(), before)

        # target 条目版本对不上：同样整体拒绝。
        source_stale = self.store.patch_self(
            "merge", target_id=first["id"], expected_version=9,
            fields={"into": second["id"], "expected_other_version": 1})
        self.assertFalse(source_stale["ok"])
        self.assertEqual(source_stale["error"], "version-conflict")
        self.assertEqual(source_stale["id"], first["id"])
        self.assertEqual(self.raw(), before)

        # 双 CAS 都正确才同时推进两个条目。
        merged = self.store.patch_self(
            "merge", target_id=first["id"], expected_version=1,
            fields={"into": second["id"], "expected_other_version": 1},
            reason="重复条目")
        self.assertTrue(merged["ok"], merged)
        self.assertEqual(merged["id"], second["id"])
        self.assertEqual(merged["version"], 2)
        entries = {row["id"]: row for row in self.store.self_entries(
            active_only=False)}
        self.assertEqual(entries[first["id"]]["status"], "merged")
        self.assertEqual(entries[first["id"]]["version"], 2)
        self.assertEqual(entries[first["id"]]["merged_into"], second["id"])
        self.assertEqual(entries[second["id"]]["status"], "active")
        self.assertEqual(entries[second["id"]]["version"], 2)
        self.assertIn("喜欢收集贴纸", entries[second["id"]]["text"])
        self.assertIn("早起先看消息", entries[second["id"]]["text"])

    def test_goal_active_limit(self):
        limited = tempfile.TemporaryDirectory()
        try:
            paths = DataPaths(root=limited.name, base_dir=limited.name)
            paths.ensure_layout()
            store = LifeStore(limited.name, {"life.max_active_goals": 2}, paths=paths)
            self.assertTrue(store.patch_goal(
                "add", fields={"type": "task", "text": "一"})["ok"])
            self.assertTrue(store.patch_goal(
                "add", fields={"type": "task", "text": "二"})["ok"])
            blocked = store.patch_goal("add", fields={"type": "task", "text": "三"})
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"], "active-limit")
        finally:
            limited.cleanup()

    # ---------------------------------------------------------------- 自我条目
    def test_self_local_edit_retire_merge_delete(self):
        exp = self.add_exp("chat:40", "一起做了贴纸")
        persona = self.paths.content("persona.md")
        with open(persona, "w", encoding="utf-8") as handle:
            handle.write("外部人设正文，不允许被生活状态覆盖\n")

        one = self.store.patch_self(
            "add", fields={"category": "habit", "text": "早起先看消息"},
            source_ids=[exp["id"]])
        two = self.store.patch_self(
            "add", fields={"category": "interest", "text": "喜欢收集贴纸"})
        three = self.store.patch_self(
            "add", fields={"category": "self", "text": "临时自我条目要删掉"})
        four = self.store.patch_self(
            "add", fields={"category": "self", "text": "其实很怕冷"},
            source_ids=[exp["id"]])
        five = self.store.patch_self(
            "add", fields={"category": "self", "text": "喜欢热闹"})

        revised = self.store.patch_self(
            "revise", target_id=one["id"], expected_version=1,
            fields={"text": "早起先看群消息", "valid_scope": "工作日"},
            reason="相处后调整")
        self.assertTrue(revised["ok"])
        entries = {row["id"]: row for row in self.store.self_entries()}
        self.assertEqual(entries[one["id"]]["text"], "早起先看群消息")
        self.assertEqual(entries[one["id"]]["category"], "habit")
        self.assertEqual(entries[one["id"]]["source_ids"], [exp["id"]])
        self.assertEqual(entries[two["id"]]["text"], "喜欢收集贴纸")

        stale = self.store.patch_self(
            "revise", target_id=one["id"], expected_version=1,
            fields={"text": "旧版本"})
        self.assertEqual(stale["error"], "version-conflict")

        retired = self.store.patch_self(
            "retire", target_id=two["id"], expected_version=1, reason="不迷了")
        self.assertTrue(retired["ok"])
        self.assertNotIn(two["id"], [r["id"] for r in self.store.self_entries()])
        self.assertIn(two["id"],
                      [r["id"] for r in self.store.self_entries(active_only=False)])
        self.assertFalse(self.store.patch_self(
            "revise", target_id=two["id"], fields={"text": "复活"}).get("ok"))

        merged = self.store.patch_self(
            "merge", target_id=four["id"], expected_version=1,
            fields={"into": five["id"], "expected_other_version": 1},
            reason="重复条目")
        self.assertTrue(merged["ok"])
        self.assertEqual(merged["id"], five["id"])
        entries = {row["id"]: row for row in self.store.self_entries()}
        self.assertNotIn(four["id"], entries)
        self.assertIn("喜欢热闹", entries[five["id"]]["text"])
        self.assertIn("其实很怕冷", entries[five["id"]]["text"])
        self.assertIn(four["id"], entries[five["id"]]["merged_from"])
        self.assertIn(exp["id"], entries[five["id"]]["source_ids"])

        deleted = self.store.patch_self(
            "delete", target_id=three["id"], expected_version=1, reason="清理")
        self.assertTrue(deleted["ok"])
        raw_text = json.dumps(self.raw(), ensure_ascii=False)
        self.assertNotIn("临时自我条目要删掉", raw_text)
        self.assertFalse(self.store.patch_self(
            "revise", target_id=three["id"], fields={"text": "复活"}).get("ok"))

        with open(persona, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "外部人设正文，不允许被生活状态覆盖\n")

        reloaded = LifeStore(self.root, self.config, paths=self.paths)
        self.assertNotIn(two["id"], [r["id"] for r in reloaded.self_entries()])
        self.assertFalse(reloaded.patch_self(
            "revise", target_id=three["id"], fields={"text": "复活"}).get("ok"))

    # ---------------------------------------------------------------- 养成
    def test_practice_separates_understanding_from_operation_and_has_sources(self):
        watched = self.add_exp(
            "bili:50", "看了烹饪视频的字幕", platform="bilibili",
            observed_level="subtitle", topics=["烹饪"])
        downgraded = self.store.practice(
            "烹饪", experience_id=watched["id"], mode="practice",
            artifact="", people=[])
        self.assertTrue(downgraded["counted"])
        self.assertTrue(downgraded["downgraded"])
        self.assertEqual(downgraded["mode"], "understanding")
        self.assertEqual(downgraded["skill"]["practice_count"], 0)
        self.assertEqual(downgraded["skill"]["understanding_count"], 1)
        self.assertEqual(downgraded["skill"]["stage"], "understanding")
        self.assertIn("不能算操作能力", downgraded["explanation"])

        duplicate = self.store.practice(
            "烹饪", experience_id=watched["id"], mode="practice")
        self.assertFalse(duplicate["counted"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["skill"]["practice_count"], 0)

        cooked = self.add_exp(
            "local:51", "真的下厨做了一次", observed_level="practiced",
            topics=["烹饪"], people=["阿明"])
        actual = self.store.practice(
            "烹饪", experience_id=cooked["id"], mode="practice",
            outcome="success", artifact="content/photo.jpg", people=["阿明"])
        self.assertEqual(actual["mode"], "practice")
        self.assertEqual(actual["skill"]["practice_count"], 1)
        self.assertEqual(actual["skill"]["stage"], "tried")
        self.assertTrue(actual["memento_id"])
        self.assertTrue(actual["artifact_id"])
        self.assertIn("实际练习", actual["explanation"])

        repeated = self.store.practice(
            "烹饪", experience_id=cooked["id"], mode="practice",
            outcome="success", artifact="content/photo.jpg", people=["阿明"])
        self.assertFalse(repeated["counted"])

        snapshot = self.store.growth_snapshot()
        self.assertEqual(len(snapshot["skills"]), 1)
        self.assertEqual(len(snapshot["mementos"]), 1)
        self.assertEqual(len(snapshot["artifacts"]), 1)
        self.assertEqual(snapshot["mementos"][0]["source_ids"], [cooked["id"]])
        self.assertEqual(snapshot["artifacts"][0]["experience_id"], cooked["id"])
        self.assertIsNone(snapshot["resources"]["balance"])
        self.assertFalse(snapshot["resources"]["known"])
        self.assertEqual(snapshot["resource_balance"], "unknown")
        self.assertEqual(snapshot["skills"][0]["practice_count"], 1)
        self.assertIn("goals", snapshot)
        self.assertIn("changes", snapshot)

    def test_practice_needs_explicit_execution_evidence_not_mode(self):
        # 阅读全文、普通阅读、笔记、空级别和未知级别都只是了解；调用方把
        # mode 传成 practice 也不能把已保存的经历证据升级成实践经验。
        low_levels = ("full_article", "read", "notes", "", "some_unknown_level",
                      "subtitle 里提到 practiced")
        for index, level in enumerate(low_levels):
            topic = f"阅读烹饪{index}"
            exp = self.add_exp(
                f"web:{100 + index}", f"阅读/观看烹饪内容（{level or '无级别'}）",
                platform="web", observed_level=level, topics=[topic])
            result = self.store.practice(
                topic, experience_id=exp["id"], mode="practice",
                artifact="伪造作品.jpg", people=["阿明"])
            self.assertEqual(result["mode"], "understanding", level)
            self.assertTrue(result["downgraded"], level)
            self.assertEqual(result["skill"]["practice_count"], 0, level)
            self.assertEqual(result["skill"]["understanding_count"], 1, level)
            self.assertEqual(result["artifact_id"], "", level)
            self.assertEqual(result["memento_id"], "", level)

        # 明确执行/练习/动手/制作/测试过的经历仍然可以计实践。
        high_levels = ("executed", "practiced", "hands_on", "built", "tested")
        for index, level in enumerate(high_levels):
            topic = f"实践烹饪{index}"
            exp = self.add_exp(
                f"local:{200 + index}", f"实际完成烹饪（{level}）",
                platform="local", observed_level=level, topics=[topic])
            result = self.store.practice(
                topic, experience_id=exp["id"], mode="practice",
                outcome="success", artifact=f"content/{index}.jpg")
            self.assertEqual(result["mode"], "practice", level)
            self.assertFalse(result["downgraded"], level)
            self.assertEqual(result["skill"]["practice_count"], 1, level)

        # 同一根来源重复回执/重复 practice 不能刷实践次数。
        rooted = self.add_exp(
            "local:300", "同一来源的真实练习", observed_level="executed",
            topics=["烘焙"])
        first = self.store.practice(
            "烘焙", experience_id=rooted["id"], mode="practice")
        self.assertEqual(first["skill"]["practice_count"], 1)
        receipt = self.add_exp(
            "local:300", "同一来源的重复回执", observed_level="executed")
        self.assertEqual(receipt["id"], rooted["id"])
        repeated = self.store.practice(
            "烘焙", experience_id=receipt["id"], mode="practice")
        self.assertFalse(repeated["counted"])
        self.assertEqual(repeated["skill"]["practice_count"], 1)
        self.assertEqual(len(repeated["skill"]["practice_sources"]), 1)
        self.assertEqual(len(repeated["skill"]["experience_roots"]), 1)

    # ---------------------------------------------------------------- 上下文
    def test_context_only_relevant_and_no_default_lines(self):
        exp = self.add_exp("chat:60", "和主人聊到海边")
        memory = self.store.remember("主人喜欢海边", source_ids=[exp["id"]])
        goal = self.store.patch_goal(
            "add", fields={"type": "wish", "text": "去海边看一次日出"})
        other = self.store.patch_goal(
            "add", fields={"type": "task", "text": "修好机械臂的第三关节"})
        self_ = self.store.patch_self(
            "add", fields={"category": "preference", "text": "喜欢海边的风"})

        text = self.store.context_for("海边", budget=1000)
        self.assertIn("主人喜欢海边", text)
        self.assertIn("去海边看一次日出", text)
        self.assertIn("喜欢海边的风", text)
        self.assertNotIn("修好机械臂的第三关节", text)
        self.assertNotIn("大肥鱼", text)
        self.assertNotIn("你是一个", text)
        self.assertLessEqual(len(text), 1000)

        self.assertEqual(self.store.context_for("量子力学"), "")
        self.assertEqual(self.store.context_for(""), "")

        # 只是确认这些 ID 都被真实写入，不是默认剧情。
        self.assertTrue(memory["id"] and goal["id"] and other["id"] and self_["id"])


if __name__ == "__main__":
    unittest.main()
