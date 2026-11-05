"""Life 内容/状态分类与云同步往返（临时目录，不读运行 data）。

覆盖：
* ``paths`` 把 ``life/`` 归为 content、``autonomy.json`` 归为 state；旧位置迁移兼容。
* ``cloudsync`` 在 include_memory 下打包并应用 ``life/state.json``；
  include_memory=false 时既不打包也不落盘。
* 真实 tar.gz pack/apply roundtrip 保留 LifeStore 写下的目标、自改记录与删除 tombstone；
  自主活动运行态 ``state/autonomy.json`` 不会被当成内容打包。

    py -3.13 -m unittest tools.test_life_sync -q
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import cloudsync  # noqa: E402
from pawbot.config import DEFAULT  # noqa: E402
from pawbot.life_store import LifeStore  # noqa: E402
from pawbot.paths import DataPaths, classify, migrate_legacy  # noqa: E402


class Cfg(dict):
    """Config 替身：支持 'a.b' 点号读写，不碰真实 config.json。"""

    def __init__(self, sync=None):
        super().__init__(copy.deepcopy(DEFAULT))
        self["sync"].update(sync or {})

    def get(self, key, default=None):
        node = self
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key, value):
        parts = str(key).split(".")
        node = self
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        return value


class LifePathsTests(unittest.TestCase):
    def test_classification_and_legacy_migration(self):
        self.assertEqual(classify("life"), "content")
        self.assertEqual(classify("life/state.json"), "content")
        self.assertEqual(classify("autonomy.json"), "state")
        # 旧条目语义不变
        self.assertEqual(classify("memory/facts.md"), "content")
        self.assertEqual(classify("config.json"), "state")

        with tempfile.TemporaryDirectory(prefix="pawbot-life-paths-") as root:
            os.makedirs(os.path.join(root, "life"))
            with open(os.path.join(root, "life", "state.json"), "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "goals": []}, handle)
            with open(os.path.join(root, "autonomy.json"), "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "running": False}, handle)

            paths = DataPaths(root=root, base_dir=root)
            paths.ensure_layout()
            # 还没迁移时先读旧位置，不能假装数据丢了
            self.assertEqual(paths.content("life/state.json"),
                             os.path.join(root, "life", "state.json"))
            self.assertEqual(paths.state("autonomy.json"),
                             os.path.join(root, "autonomy.json"))

            report = migrate_legacy(root, link_compat=False)
            moved = {(item["name"], item["kind"]) for item in report["moved"]}
            self.assertIn(("life", "content"), moved)
            self.assertIn(("autonomy.json", "state"), moved)
            self.assertTrue(os.path.isfile(
                os.path.join(root, "content", "life", "state.json")))
            self.assertTrue(os.path.isfile(
                os.path.join(root, "state", "autonomy.json")))

            # 迁移后 canonical 优先，content/state 路径语义不变
            self.assertEqual(paths.content("life/state.json"),
                             os.path.join(root, "content", "life", "state.json"))
            self.assertEqual(paths.state("autonomy.json"),
                             os.path.join(root, "state", "autonomy.json"))


class LifeCloudSyncRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pawbot-life-sync-")
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.remote = os.path.join(self.base, "remote")
        os.makedirs(os.path.join(self.base, "data"), exist_ok=True)
        self.paths = DataPaths(root=os.path.join(self.base, "data"), base_dir=self.base)
        self.paths.ensure_layout()
        self.life_path = self.paths.content("life/state.json")
        # 运行态：绝不能随内容包同步
        self.autonomy_path = self.paths.state("autonomy.json")
        with open(self.autonomy_path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "running": True, "next_at": 1700000000}, handle)

    # ------------------------------------------------------------ 工具

    def cfg(self, **sync):
        values = {
            "enabled": True,
            "provider": "local",
            "url": self.remote,
            "include_config": False,
            "include_persona": False,
            "include_memory": True,
            "include_stickers": False,
        }
        values.update(sync)
        return Cfg(values)

    def seed_life(self):
        """用真实 LifeStore 造目标、自改记录与删除 tombstone。"""
        store = LifeStore(self.base, self.cfg(), paths=self.paths)
        self.assertTrue(store.record_experience(
            "chat:1", "主人委托整理海边照片", platform="qq")["id"])
        active = store.patch_goal(
            "add", fields={"type": "task", "text": "整理海边照片"},
            reason="主人托付")
        self.assertTrue(active["ok"], active)
        self.assertTrue(store.patch_goal(
            "add", fields={"type": "wish", "text": "修好机械臂"})["ok"])
        doomed = store.patch_goal(
            "add", fields={"type": "task", "text": "临时任务"})
        self.assertTrue(doomed["ok"], doomed)
        current = next(row for row in store.goals(active_only=False)
                       if row["id"] == doomed["id"])
        deleted = store.patch_goal(
            "delete", target_id=doomed["id"],
            expected_version=current["version"], reason="不做了")
        self.assertTrue(deleted["ok"], deleted)
        self.assertTrue(store.patch_self(
            "add", fields={"category": "trait", "text": "更喜欢安静地做事"},
            reason="自我修订")["ok"])

        with open(self.life_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def read_bundle(self, name=cloudsync.PLAIN_NAME):
        path = os.path.join(self.remote, name)
        with tarfile.open(path, "r:gz") as tar:
            manifest = json.load(tar.extractfile(cloudsync.MANIFEST_NAME))
            names = tar.getnames()
        return manifest, names

    def new_base(self):
        directory = tempfile.TemporaryDirectory(prefix="pawbot-life-apply-")
        self.addCleanup(directory.cleanup)
        data_root = os.path.join(directory.name, "data")
        os.makedirs(data_root, exist_ok=True)
        # 真实启动会先建好/迁移 content 与 state；这样 life 才落到 canonical content/life。
        DataPaths(root=data_root, base_dir=directory.name).ensure_layout()
        return directory.name

    def target(self, base, rel):
        return os.path.join(base, "data", *rel.split("/"))

    # ------------------------------------------------------------ 往返

    def test_roundtrip_preserves_life_goals_self_edits_and_tombstones(self):
        state = self.seed_life()
        with open(self.life_path, "rb") as handle:
            original_bytes = handle.read()

        pushed = cloudsync.push(self.cfg(), self.base)
        self.assertTrue(pushed["ok"])
        manifest, names = self.read_bundle()
        self.assertIn("memory/life/state.json", names)
        targets = {entry["target"] for entry in manifest["entries"]}
        self.assertIn("life/state.json", targets)
        self.assertFalse(any(name.endswith("autonomy.json") for name in names),
                         "自主活动运行态不能进内容包")
        self.assertNotIn("autonomy.json", targets)

        other = self.new_base()
        pulled = cloudsync.pull(self.cfg(), other)
        self.assertTrue(pulled["ok"])
        self.assertIn("life/state.json", pulled["applied"])

        applied_path = self.target(other, "content/life/state.json")
        self.assertTrue(os.path.isfile(applied_path), "life/state.json 应落到 content/life")
        with open(applied_path, "rb") as handle:
            self.assertEqual(handle.read(), original_bytes, "life 状态应逐字节一致")

        reloaded = json.loads(original_bytes.decode("utf-8"))
        goals = {row["id"]: row for row in reloaded.get("goals", [])}
        texts = {row.get("text") for row in goals.values()}
        self.assertIn("整理海边照片", texts, "目标应保留")
        tombstones = [row for row in goals.values() if row.get("status") == "deleted"]
        self.assertTrue(tombstones, "删除应留 tombstone")
        self.assertGreater(tombstones[0].get("deleted_at") or 0, 0)
        self.assertNotIn("临时任务", original_bytes.decode("utf-8"),
                         "删除内容不应留在正文")
        changes = reloaded.get("changes", [])
        self.assertTrue(any(row.get("op") == "add" for row in changes), "自改记录应保留")
        self.assertTrue(any(row.get("op") == "delete" for row in changes), "删除变更应保留")

        # 运行态既没有被打包，也不应在目标机出现
        self.assertFalse(os.path.exists(self.target(other, "state/autonomy.json")))
        self.assertFalse(os.path.exists(self.target(other, "content/autonomy.json")))
        self.assertFalse(os.path.exists(self.target(other, "autonomy.json")))

    def test_include_memory_controls_life_entries(self):
        self.seed_life()
        cloudsync.push(self.cfg(include_memory=False), self.base)
        manifest, names = self.read_bundle()
        self.assertFalse(any(name.startswith("memory/life/") for name in names),
                         "include_memory=false 不应打包 life/")
        self.assertNotIn("life/state.json",
                         {entry["target"] for entry in manifest["entries"]})

        # 同一远端包：拉取时 include_memory=false 必须跳过 life 文件
        cloudsync.push(self.cfg(), self.base)
        other = self.new_base()
        pulled = cloudsync.pull(self.cfg(include_memory=False), other)
        self.assertIn("life/state.json", pulled["skipped"])
        self.assertFalse(os.path.exists(self.target(other, "content/life/state.json")))


if __name__ == "__main__":
    unittest.main()
