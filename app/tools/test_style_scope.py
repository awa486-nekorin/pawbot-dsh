"""B 站学来的说话方式必须进入自主活动/空间发布等截断路径。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.autonomy import Autonomy
from pawbot.paths import DataPaths
from pawbot.persona import Persona

STYLE = "## 学来的说话方式（从 B 站评论里观察来的，照这个语气说，别照抄内容）\n- 短句接梗，别客套"


class PersonaStyleBlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = DataPaths(root=self.tmp.name, base_dir=self.tmp.name)
        self.paths.ensure_layout()
        self.persona = Persona(self.tmp.name, {}, paths=self.paths)
        Path(self.persona.persona_path).write_text(
            "# 测试人设\n你说话短，像群友随手回消息。\n", encoding="utf-8")
        style_path = Path(self.persona.memory_file("style_learned.md"))
        style_path.parent.mkdir(parents=True, exist_ok=True)
        style_path.write_text(
            "# 学来的说话方式\n- 短句接梗，别客套\n", encoding="utf-8")

    def test_style_block_is_in_full_persona(self):
        text = self.persona.build()
        self.assertIn("学来的说话方式", text)
        self.assertIn("短句接梗，别客套", text)

    def test_style_block_has_header(self):
        block = self.persona.style_block()
        self.assertTrue(block.startswith("## 学来的说话方式"))
        self.assertIn("短句接梗", block)


class AutonomyStyleContextTests(unittest.TestCase):
    def _auto(self, build_text, style=STYLE):
        persona = SimpleNamespace(
            build=lambda **kwargs: build_text,
            style_block=lambda limit=1200: style,
        )
        bot = SimpleNamespace(persona=persona, config={})
        return Autonomy(bot)

    def test_truncated_context_still_gets_style(self):
        auto = self._auto("长" * 9000)
        text = auto._persona_context("状态", limit=3600)
        self.assertIn("学来的说话方式", text)
        self.assertIn("短句接梗", text)
        self.assertLessEqual(len(text), 3700)

    def test_existing_style_is_not_duplicated(self):
        auto = self._auto("前文\n\n" + STYLE)
        text = auto._persona_context(limit=3600)
        self.assertEqual(text.count("学来的说话方式"), 1)


if __name__ == "__main__":
    unittest.main()
