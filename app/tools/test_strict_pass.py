"""tools/strict_anime_pass.py 的离线测试：网络 patch 掉，只写临时目录。

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
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import strict_anime_pass as sp  # noqa: E402

# 1x1 PNG，够 Pillow 开图、缩放
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg==")

TRUE_REPLY = '{"anime": true, "kind": "日系插畫", "reason": "日系少女插畫"}'
FALSE_REPLY = ('```json\n{"anime": false, "kind": "真人Q版化", '
               '"reason": "明星Q版畫像"}\n```')


class FakeResp:
    def __init__(self, text: str):
        self._data = json.dumps(
            {"choices": [{"message": {"content": text}}]}).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StrictBase(unittest.TestCase):
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

    def entry(self, tags=("开心",), desc="测试图", sha1="a" * 40,
              uses=7, anime=True) -> dict:
        out = {"tags": list(tags), "desc": desc, "sha1": sha1, "uses": uses}
        if anime is not None:
            out["anime"] = anime
        return out

    def run_main(self, replies, argv=()):
        """replies: 依调用顺序给回复文本或异常；patched 掉 urlopen 和 sleep。"""
        queue = list(replies)

        def fake_urlopen(req, timeout=None):
            self.calls.append(req)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return FakeResp(item)

        argv = ["--base", self.base, "--config", self.config,
                "--workers", "1", *argv]
        with patch.object(sp.urllib.request, "urlopen",
                          side_effect=fake_urlopen), \
                patch.object(sp.time, "sleep"):
            out = io.StringIO()
            with redirect_stdout(out):
                code = sp.main(argv)
        self.stdout = out.getvalue()
        return code

    def removed_dirs(self):
        return sorted(glob.glob(os.path.join(self.sdir, sp.REMOVED_PREFIX + "*")))

    def report_files(self):
        return sorted(glob.glob(os.path.join(self.sdir, sp.REPORT_PREFIX + "*.json")))


class ApplyTests(StrictBase):
    def setUp(self):
        super().setUp()
        self.put_image("动漫.jpg")
        self.put_image("偶像.jpg")
        self.put_image("坏图.gif")
        self.put_index({
            "动漫.jpg": self.entry(tags=["可爱", "开心"], desc="Q版女仆",
                                  sha1="1" * 40, uses=9),
            "偶像.jpg": self.entry(tags=["牢大", "搞笑"], desc="Q版头像",
                                  sha1="2" * 40),
            "坏图.gif": self.entry(tags=["疑惑"], desc="未知图", sha1="3" * 40),
        })

    def test_false_moved_not_deleted(self):
        code = self.run_main([TRUE_REPLY, FALSE_REPLY, OSError("boom")], ["--apply"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.sdir, "偶像.jpg")))

        removed = self.removed_dirs()
        self.assertEqual(len(removed), 1)
        self.assertIn(os.path.basename(removed[0]), self.stdout)
        moved = os.path.join(removed[0], "偶像.jpg")
        self.assertTrue(os.path.isfile(moved))
        with open(moved, "rb") as f:
            self.assertEqual(f.read(), PNG)

    def test_unknown_kept(self):
        self.run_main([TRUE_REPLY, FALSE_REPLY, OSError("boom")], ["--apply"])
        self.assertTrue(os.path.isfile(os.path.join(self.sdir, "坏图.gif")))
        self.assertIs(self.read_index()["坏图.gif"]["anime"], True)
        self.assertIn("unknown", self.stdout)

    def test_index_drops_false_and_marks_strict_desc(self):
        self.run_main([TRUE_REPLY, FALSE_REPLY, OSError("boom")], ["--apply"])
        idx = self.read_index()
        self.assertEqual(set(idx), {"动漫.jpg", "坏图.gif"})

        kept = idx["动漫.jpg"]
        self.assertEqual(kept["tags"], ["可爱", "开心"])
        self.assertEqual(kept["sha1"], "1" * 40)
        self.assertEqual(kept["uses"], 9)
        self.assertIs(kept["anime"], True)
        self.assertIn("｜strict: 日系插畫", kept["desc"])

        # unknown 不追加 strict，也不改其他字段
        self.assertEqual(idx["坏图.gif"]["desc"], "未知图")

    def test_strict_mark_not_duplicated(self):
        self.run_main([TRUE_REPLY, OSError("boom")], ["--apply"])
        first = self.read_index()["动漫.jpg"]["desc"]
        self.run_main([TRUE_REPLY, OSError("boom")], ["--apply"])
        self.assertEqual(self.read_index()["动漫.jpg"]["desc"], first)

    def test_report_written(self):
        self.run_main([TRUE_REPLY, FALSE_REPLY, OSError("boom")], ["--apply"])
        reports = self.report_files()
        self.assertEqual(len(reports), 1)
        with open(reports[0], encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["counts"],
                         {"total": 3, "anime": 1, "nonanime": 1, "unknown": 1})
        self.assertEqual(report["moved"], ["偶像.jpg"])
        self.assertEqual(report["results"]["偶像.jpg"]["kind"], "真人Q版化")


class DryRunTests(StrictBase):
    def test_dry_writes_nothing(self):
        self.put_image("a.jpg")
        self.put_image("b.jpg")
        self.put_index({"a.jpg": self.entry(), "b.jpg": self.entry(desc="B")})
        before = sorted(os.listdir(self.sdir))
        code = self.run_main([FALSE_REPLY, FALSE_REPLY])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(self.sdir)), before)
        self.assertEqual(self.removed_dirs(), [])
        self.assertEqual(self.report_files(), [])
        self.assertNotIn("strict:", self.read_index()["a.jpg"]["desc"])

    def test_only_anime_true_entries_judged(self):
        self.put_image("真.jpg")
        self.put_image("假.jpg")
        self.put_index({"真.jpg": self.entry(),
                        "假.jpg": self.entry(anime=False)})
        self.run_main([TRUE_REPLY])
        self.assertEqual(len(self.calls), 1)

    def test_only_unchecked_skips_already_strict_entries(self):
        self.put_image("旧的.jpg")
        self.put_image("新的.jpg")
        self.put_index({
            "旧的.jpg": self.entry(desc="Q版女仆｜strict: Q版"),
            "新的.jpg": self.entry(desc="斗图网：摸鱼（二次元）"),
        })
        self.run_main([FALSE_REPLY], ["--only-unchecked", "--apply"])
        # 只打了一次模型，且只打新的
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.sdir, "旧的.jpg")))
        self.assertFalse(os.path.exists(os.path.join(self.sdir, "新的.jpg")))
        idx = self.read_index()
        self.assertEqual(set(idx), {"旧的.jpg"})
        self.assertEqual(idx["旧的.jpg"]["desc"], "Q版女仆｜strict: Q版")

    def test_only_unchecked_marks_new_kept_entry(self):
        self.put_image("旧的.jpg")
        self.put_image("新的.jpg")
        self.put_index({
            "旧的.jpg": self.entry(desc="老图｜strict: 動漫角色"),
            "新的.jpg": self.entry(desc="斗图网：愉快（二次元）"),
        })
        self.run_main([TRUE_REPLY], ["--only-unchecked", "--apply"])
        idx = self.read_index()
        self.assertEqual(idx["新的.jpg"]["desc"], "斗图网：愉快（二次元）｜strict: 日系插畫")
        self.assertIs(idx["新的.jpg"]["anime"], True)
        self.assertEqual(idx["旧的.jpg"]["desc"], "老图｜strict: 動漫角色")


class RetryTests(StrictBase):
    def test_429_retried_once_then_ok(self):
        self.put_image("a.jpg")
        self.put_index({"a.jpg": self.entry()})
        err = urllib.error.HTTPError("http://fake.local", 429,
                                     "Too Many", {}, None)
        self.run_main([err, TRUE_REPLY], ["--apply"])
        self.assertEqual(len(self.calls), 2)
        self.assertIs(self.read_index()["a.jpg"]["anime"], True)

    def test_timeout_marks_unknown_without_retry(self):
        self.put_image("a.jpg")
        self.put_index({"a.jpg": self.entry()})
        self.run_main([OSError("timeout")], ["--apply"])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.sdir, "a.jpg")))


class ParseTests(unittest.TestCase):
    def test_plain_json(self):
        got = sp.parse_strict(TRUE_REPLY)
        self.assertEqual(got["anime"], True)
        self.assertEqual(got["kind"], "日系插畫")

    def test_fenced_json(self):
        got = sp.parse_strict(FALSE_REPLY)
        self.assertIs(got["anime"], False)
        self.assertEqual(got["kind"], "真人Q版化")

    def test_garbage_around_json(self):
        got = sp.parse_strict(
            '好的：\n{"anime": true, "kind": "動漫角色", "reason": "动漫截图"}\n以上。')
        self.assertIs(got["anime"], True)

    def test_string_bool(self):
        self.assertIs(sp.parse_strict('{"anime": "false", "kind": "照片"}')["anime"],
                      False)

    def test_unparsable(self):
        self.assertIsNone(sp.parse_strict("不知道"))
        self.assertIsNone(sp.parse_strict('{"kind": "Q版"}'))
        self.assertIsNone(sp.parse_strict(""))


if __name__ == "__main__":
    unittest.main()
