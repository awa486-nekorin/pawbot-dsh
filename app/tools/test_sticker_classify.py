"""tools/classify_stickers.py 的离线测试：网络全部 patch 掉，只写临时目录。

不碰真的 data/stickers，也不读真的 config.json。
"""

import base64
import glob
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import classify_stickers as ck  # noqa: E402

# 1x1 PNG，够 Pillow 开图、缩放
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg==")

TRUE_REPLY = '{"anime": true, "kind": "Q版", "reason": "蓝发Q版女仆"}'
FALSE_REPLY = '```json\n{"anime": false, "kind": "真人照片", "reason": "真人合影加字"}\n```'


def ok(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


class ClassifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.sdir = os.path.join(self.base, "data", "stickers")
        os.makedirs(self.sdir)
        self.config = os.path.join(self.base, "data", "config.json")
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"llm": {"base_url": "http://fake.local/v1",
                               "api_key": "test-key"}}, f)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------
    def put_image(self, name: str) -> None:
        with open(os.path.join(self.sdir, name), "wb") as f:
            f.write(PNG)

    def put_index(self, entries: dict) -> None:
        with open(os.path.join(self.sdir, "index.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)

    def read_index(self) -> dict:
        with open(os.path.join(self.sdir, "index.json"), encoding="utf-8") as f:
            return json.load(f)

    def entry(self, tags=("开心",), desc="测试图", sha1="a" * 40, uses=7) -> dict:
        return {"tags": list(tags), "desc": desc, "sha1": sha1, "uses": uses}

    def run_main(self, replies, argv=()):
        """replies: 依调用顺序给回复文本或异常；patched 掉网络和 sleep。"""
        queue = list(replies)

        def fake_post(cfg, payload, timeout=60):
            self.calls.append(payload)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return ok(item)

        argv = ["--base", self.base, "--config", self.config, "--workers", "1", *argv]
        with patch.object(ck, "_post_json", side_effect=fake_post), \
                patch.object(ck.time, "sleep"):
            out = io.StringIO()
            with redirect_stdout(out):
                code = ck.main(argv)
        self.stdout = out.getvalue()
        return code

    def report_files(self):
        return sorted(glob.glob(os.path.join(self.sdir, "classify-*.json")))

    def removed_dirs(self):
        return sorted(glob.glob(os.path.join(self.sdir, ck.REMOVED_PREFIX + "*")))


class ApplyTests(ClassifyBase):
    def setUp(self):
        super().setUp()
        self.put_image("动漫.jpg")
        self.put_image("真人.jpg")
        self.put_image("坏图.gif")
        self.put_index({
            "动漫.jpg": self.entry(tags=["可爱", "开心"], desc="Q版女仆",
                                  sha1="1" * 40, uses=9),
            "真人.jpg": self.entry(tags=["牢大", "搞笑"]),
            "坏图.gif": self.entry(tags=["疑惑"]),
        })

    def test_true_kept_false_moved_unknown_kept(self):
        code = self.run_main([TRUE_REPLY, FALSE_REPLY, OSError("boom")], ["--apply"])
        self.assertEqual(code, 0)

        # 保留 / 待定还在原地
        self.assertTrue(os.path.isfile(os.path.join(self.sdir, "动漫.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(self.sdir, "坏图.gif")))
        # false 不在原目录
        self.assertFalse(os.path.exists(os.path.join(self.sdir, "真人.jpg")))

        # 是搬走而不是删掉，内容还能救
        removed = self.removed_dirs()
        self.assertEqual(len(removed), 1)
        self.assertTrue(self.stdout.count(ck.REMOVED_PREFIX) >= 1)
        moved = os.path.join(removed[0], "真人.jpg")
        self.assertTrue(os.path.isfile(moved))
        with open(moved, "rb") as f:
            self.assertEqual(f.read(), PNG)

        idx = self.read_index()
        self.assertEqual(set(idx), {"动漫.jpg", "坏图.gif"})
        self.assertIs(idx["动漫.jpg"]["anime"], True)

    def test_index_fields_preserved(self):
        self.run_main([TRUE_REPLY, FALSE_REPLY, OSError("boom")], ["--apply"])
        idx = self.read_index()
        kept = idx["动漫.jpg"]
        self.assertEqual(kept["tags"], ["可爱", "开心"])
        self.assertEqual(kept["desc"], "Q版女仆")
        self.assertEqual(kept["sha1"], "1" * 40)
        self.assertEqual(kept["uses"], 9)
        self.assertIs(kept["anime"], True)
        # unknown 保留但不冒充 anime
        self.assertNotIn("anime", idx["坏图.gif"])
        self.assertEqual(idx["坏图.gif"]["sha1"], "a" * 40)

    def test_summary_counts_and_report(self):
        self.run_main([TRUE_REPLY, FALSE_REPLY, OSError("boom")], ["--apply"])
        self.assertIn("判定 3 张：保留 1 张，删除 1 张，unknown 1 张", self.stdout)
        self.assertIn("坏图.gif", self.stdout)
        reports = self.report_files()
        self.assertEqual(len(reports), 1)
        with open(reports[0], encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["counts"],
                         {"total": 3, "anime": 1, "nonanime": 1, "unknown": 1})
        self.assertEqual(report["results"]["真人.jpg"]["kind"], "真人照片")

    def test_dry_run_touches_nothing(self):
        code = self.run_main([TRUE_REPLY, FALSE_REPLY, OSError("boom")])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(self.sdir)),
                         sorted(["index.json", "动漫.jpg", "真人.jpg", "坏图.gif",
                                 os.path.basename(self.report_files()[0])]))
        self.assertEqual(self.removed_dirs(), [])
        # index 原样
        self.assertNotIn("anime", self.read_index()["动漫.jpg"])


class ParseTests(unittest.TestCase):
    def test_plain_json(self):
        got = ck.parse_reply(TRUE_REPLY)
        self.assertEqual(got["anime"], True)
        self.assertEqual(got["kind"], "Q版")

    def test_fenced_json(self):
        got = ck.parse_reply(FALSE_REPLY)
        self.assertEqual(got["anime"], False)
        self.assertEqual(got["kind"], "真人照片")

    def test_garbage_around_json(self):
        got = ck.parse_reply(
            '好的，结果如下：\n{"anime": true, "kind": "漫画", "reason": "黑白漫画"}\n以上。')
        self.assertEqual(got["anime"], True)
        self.assertEqual(got["kind"], "漫画")

    def test_string_bool(self):
        self.assertIs(ck.parse_reply('{"anime": "false", "kind": "其它"}')["anime"], False)

    def test_unparsable(self):
        self.assertIsNone(ck.parse_reply("不知道"))
        self.assertIsNone(ck.parse_reply('{"kind": "Q版"}'))
        self.assertIsNone(ck.parse_reply(""))


class EdgeCaseTests(ClassifyBase):
    def test_missing_and_untracked_reported(self):
        self.put_image("在的.jpg")
        self.put_image("多出来的.jpg")
        self.put_index({"在的.jpg": self.entry(), "没了的.jpg": self.entry()})
        code = self.run_main([TRUE_REPLY])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), 1)          # 只判定磁盘上存在的

        with open(self.report_files()[0], encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["missing_files"], ["没了的.jpg"])
        self.assertEqual(report["untracked_files"], ["多出来的.jpg"])
        self.assertNotIn("没了的.jpg", report["results"])
        self.assertIn("跳过", self.stdout)
        self.assertIn("index 里没有", self.stdout)

    def test_limit_only_judges_first_n(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.put_image(name)
        self.put_index({name: self.entry() for name in ("a.jpg", "b.jpg", "c.jpg")})
        self.run_main([TRUE_REPLY], ["--limit", "1"])
        self.assertEqual(len(self.calls), 1)

    def test_broken_image_marks_unknown_without_network(self):
        with open(os.path.join(self.sdir, "坏.jpg"), "wb") as f:
            f.write(b"not an image")
        self.put_index({"坏.jpg": self.entry()})
        self.run_main([])
        self.assertEqual(self.calls, [])
        with open(self.report_files()[0], encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["counts"]["unknown"], 1)

    def test_retries_then_unknown(self):
        self.put_image("a.jpg")
        self.put_index({"a.jpg": self.entry()})
        self.run_main([OSError("boom"), OSError("boom"), OSError("boom")])
        self.assertEqual(len(self.calls), 3)           # 1 次 + 2 次重试
        with open(self.report_files()[0], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["counts"]["unknown"], 1)


if __name__ == "__main__":
    unittest.main()
