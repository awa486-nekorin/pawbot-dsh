"""数据布局契约：人设/记忆/笔记/日记在 content，配置/会话在 state。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.paths import DataPaths, classify, migrate_legacy


class DataLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.paths = DataPaths(root=self.root, base_dir=self.root)
        self.paths.ensure_layout()

    def test_content_and_state_are_separate_roots(self):
        content, state = self.paths.ensure_layout()
        self.assertEqual(os.path.basename(content), "content")
        self.assertEqual(os.path.basename(state), "state")
        self.assertNotEqual(os.path.normcase(content), os.path.normcase(state))
        self.assertFalse(content.startswith(state + os.sep))
        self.assertFalse(state.startswith(content + os.sep))

    def test_user_content_lives_in_content(self):
        for name in ("persona.md", "memory", "longmem", "diary", "bili_notes", "people"):
            self.assertEqual(classify(name), "content", name)
            path = self.paths.content(name)
            self.assertTrue(path.startswith(self.paths.content_root + os.sep), path)

    def test_machine_state_lives_in_state(self):
        for name in ("config.json", "sessions.json", "autonomy.json", "run.log"):
            self.assertEqual(classify(name), "state", name)
            path = self.paths.state(name)
            self.assertTrue(path.startswith(self.paths.state_root + os.sep), path)

    def test_legacy_settings_resolve_to_new_layers(self):
        self.assertEqual(self.paths.setting("data/persona.md"),
                         self.paths.content("persona.md"))
        self.assertEqual(self.paths.setting("data/bili_notes"),
                         self.paths.content("bili_notes"))
        self.assertEqual(self.paths.setting("data/config.json"),
                         self.paths.state("config.json"))
        self.assertEqual(self.paths.setting("content/diary"),
                         self.paths.content("diary"))
        self.assertEqual(self.paths.setting("state/sessions.json"),
                         self.paths.state("sessions.json"))

    def test_legacy_layout_migrates_without_mixing(self):
        (Path(self.root) / "persona.md").write_text("人设", encoding="utf-8")
        (Path(self.root) / "config.json").write_text("{}", encoding="utf-8")
        diary = Path(self.root) / "diary"
        diary.mkdir()
        (diary / "2026-11-05.md").write_text("日记", encoding="utf-8")

        result = migrate_legacy(self.root, link_compat=False)
        self.assertIn("persona.md", [row["name"] for row in result["moved"]])
        self.assertIn("config.json", [row["name"] for row in result["moved"]])
        self.assertIn("diary", [row["name"] for row in result["moved"]])

        self.assertTrue((Path(self.paths.content("persona.md"))).is_file())
        self.assertTrue((Path(self.paths.content("diary")) / "2026-11-05.md").is_file())
        self.assertTrue(Path(self.paths.state("config.json")).is_file())

        # 再跑一次不能重复搬，也不能把 content 塞进 state
        again = migrate_legacy(self.root, link_compat=False)
        self.assertEqual(again["moved"], [])


if __name__ == "__main__":
    unittest.main()
