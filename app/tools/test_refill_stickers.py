"""tools/refill_stickers.py 的离线测试：搜索 / 下载 / 看图分类全部 patch。

只写临时目录，不碰真的 data/stickers，也不读真的 config.json。
"""

import base64
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import refill_stickers as rf  # noqa: E402

# 1x1 PNG，够过 StickerBook 的魔数判断
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg==")

NAME_RE = re.compile(r"^二次元_[0-9a-f]{6}\.(png|jpg|gif|webp)$")
KW = "无语 动漫"


def png(seed: int) -> bytes:
    return PNG + bytes([seed])


def verdict(anime, mood="", kind="Q版"):
    return {"anime": anime, "mood": mood, "kind": kind, "reason": "测试",
            "status": "ok"}


class RefillBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.sdir = os.path.join(self.base, "data", "stickers")
        os.makedirs(self.sdir)
        self.config = os.path.join(self.base, "data", "config.json")
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"llm": {"base_url": "http://fake.local/v1",
                               "api_key": "test-key"}}, f)
        self.cfg = {"base_url": "http://fake.local/v1", "api_key": "test-key"}
        self.searches = []
        self.downloads = []
        self.classify_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------
    def run_refill(self, urls, images, verdicts, keywords=(KW,), *,
                   per_keyword=4, limit=60, dry=False, argv=None):
        """urls 顺序即处理顺序；images: {url: bytes}；verdicts 按调用顺序。"""
        queue = list(verdicts)

        def fake_search(keyword, n=20):
            self.searches.append(keyword)
            return list(urls)

        def fake_download(url, *args, **kwargs):
            self.downloads.append(url)
            return images.get(url)

        def fake_classify(path, cfg, timeout=60):
            self.classify_calls.append(path)
            item = queue.pop(0) if queue else verdict(True, "可爱")
            if isinstance(item, Exception):
                raise item
            return dict(item)

        with patch.object(rf.doutu, "search", side_effect=fake_search), \
                patch.object(rf.doutu, "download", side_effect=fake_download), \
                patch.object(rf, "classify_sticker", side_effect=fake_classify):
            out = io.StringIO()
            with redirect_stdout(out):
                if argv is None:
                    result = rf.run(self.base, self.cfg, list(keywords),
                                    per_keyword=per_keyword, limit=limit, dry=dry)
                else:
                    result = rf.main(["--base", self.base,
                                      "--config", self.config, *argv])
        self.stdout = out.getvalue()
        return result

    def files(self):
        return sorted(f for f in os.listdir(self.sdir)
                      if os.path.splitext(f)[1].lower() in
                      (".png", ".jpg", ".jpeg", ".gif", ".webp"))

    def index(self):
        path = os.path.join(self.sdir, "index.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)


class FilterTests(RefillBase):
    def test_only_anime_true_is_saved(self):
        images = {"u1": png(1), "u2": png(2), "u3": png(3)}
        self.run_refill(["u1", "u2", "u3"], images, [
            verdict(True, "无语"), verdict(False, "嫌弃"), verdict(None, ""),
        ])
        files = self.files()
        self.assertEqual(len(files), 1, files)
        self.assertRegex(files[0], NAME_RE)
        self.assertIs(self.index()[files[0]]["anime"], True)
        # false / unknown 的图不能留在目录里
        self.assertEqual(len(self.classify_calls), 3)
        self.assertIn("非二次元 1", self.stdout)
        self.assertIn("判定失败 1", self.stdout)
        self.assertIn("入库 1", self.stdout)

    def test_tags_desc_and_anime_field(self):
        self.run_refill(["u1"], {"u1": png(4)}, [verdict(True, "嫌弃")])
        name = self.files()[0]
        meta = self.index()[name]
        self.assertEqual(meta["tags"][0], "嫌弃")
        self.assertIn("无语", meta["tags"])          # 近义标签
        self.assertEqual(meta["desc"], f"斗图网：{KW}（二次元）")
        self.assertIs(meta["anime"], True)
        self.assertEqual(len(meta["sha1"]), 40)

    def test_unknown_and_false_never_written(self):
        images = {"u1": png(5), "u2": png(6)}
        self.run_refill(["u1", "u2"], images,
                        [verdict(None, ""), verdict(False, "可爱")])
        self.assertEqual(self.files(), [])
        self.assertFalse(os.path.exists(os.path.join(self.sdir, "index.json")))
        self.assertEqual(sorted(os.listdir(self.sdir)), [])

    def test_classify_exception_is_skipped_not_fatal(self):
        images = {"u1": png(7), "u2": png(8)}
        self.run_refill(["u1", "u2"], images,
                        [OSError("boom"), verdict(True, "无语")])
        self.assertEqual(len(self.files()), 1)
        self.assertIn("判定失败", self.stdout)

    def test_download_failure_is_skipped(self):
        self.run_refill(["u1"], {}, [])
        self.assertEqual(self.classify_calls, [])
        self.assertEqual(self.files(), [])
        self.assertIn("下载失败", self.stdout)


class MoodFallbackTests(RefillBase):
    def test_mood_outside_set_falls_back_to_keyword(self):
        self.run_refill(["u1"], {"u1": png(9)}, [verdict(True, "爆笑")])
        meta = self.index()[self.files()[0]]
        self.assertEqual(meta["tags"][0], "无语")

    def test_empty_mood_falls_back_to_keyword(self):
        self.run_refill(["u1"], {"u1": png(10)}, [verdict(True, "")])
        meta = self.index()[self.files()[0]]
        self.assertEqual(meta["tags"][0], "无语")

    def test_valid_model_mood_wins(self):
        self.run_refill(["u1"], {"u1": png(11)}, [verdict(True, "害羞")])
        meta = self.index()[self.files()[0]]
        self.assertEqual(meta["tags"][0], "害羞")

    def test_unknown_keyword_guess(self):
        self.assertEqual(rf.mood_for_keyword("生气 动漫"), "生气")
        self.assertEqual(rf.mood_for_keyword("问号表情"), "疑惑")
        self.assertEqual(rf.mood_for_keyword("完全没见过的词"), "可爱")


class NameAndDedupTests(RefillBase):
    def test_filename_format_and_sha1_dedupe(self):
        same = png(12)
        self.run_refill(["u1", "u2"], {"u1": same, "u2": same},
                        [verdict(True, "无语"), verdict(True, "无语")])
        files = self.files()
        self.assertEqual(len(files), 1, files)
        self.assertRegex(files[0], NAME_RE)
        self.assertIn("重复", self.stdout)

    def test_filename_conflict_is_skipped(self):
        image = png(13)
        import hashlib
        digest = hashlib.sha1(image).hexdigest()
        other = png(14)
        # 先塞一张同名前缀、不同内容的图
        with open(os.path.join(self.sdir, f"二次元_{digest[:6]}.png"), "wb") as f:
            f.write(other)
        self.run_refill(["u1"], {"u1": image}, [verdict(True, "无语")])
        self.assertFalse(os.path.exists(os.path.join(self.sdir, "index.json")))
        self.assertEqual(len(self.files()), 1)          # 只有原先那张
        self.assertIn("重名", self.stdout)


class DryRunTests(RefillBase):
    def test_dry_writes_nothing(self):
        self.run_refill(["u1", "u2"], {"u1": png(15), "u2": png(16)},
                        [verdict(True, "无语"), verdict(True, "可爱")], dry=True)
        self.assertEqual(sorted(os.listdir(self.sdir)), [])
        self.assertIn("[dry]", self.stdout)
        self.assertIn("未写任何文件", self.stdout)

    def test_main_dry_does_not_write(self):
        with patch.object(rf, "DEFAULT_KEYWORDS", [KW]):
            self.run_refill(["u1"], {"u1": png(17)},
                            [verdict(True, "无语")], argv=["--dry"])
        self.assertEqual(sorted(os.listdir(self.sdir)), [])


class LimitTests(RefillBase):
    def test_limit_caps_total_saved(self):
        images = {f"u{i}": png(20 + i) for i in range(5)}
        urls = list(images)
        self.run_refill(urls, images, [verdict(True, "无语")] * 5, limit=2)
        self.assertEqual(len(self.files()), 2)
        self.assertIn("上限 2 张", self.stdout)

    def test_per_keyword_caps_saved(self):
        images = {f"u{i}": png(30 + i) for i in range(5)}
        urls = list(images)
        self.run_refill(urls, images, [verdict(True, "无语")] * 5,
                        per_keyword=2, limit=60)
        self.assertEqual(len(self.files()), 2)


class RerunTests(RefillBase):
    def test_existing_image_is_skipped_before_classify(self):
        same = png(40)
        self.run_refill(["u1"], {"u1": same}, [verdict(True, "无语")])
        self.assertEqual(len(self.files()), 1)
        self.classify_calls.clear()
        self.run_refill(["u1"], {"u1": same}, [verdict(True, "无语")])
        self.assertEqual(self.classify_calls, [])         # 不重复看图
        self.assertEqual(len(self.files()), 1)
        self.assertIn("重复", self.stdout)

    def test_only_keywords_skips_defaults(self):
        with patch.object(rf, "DEFAULT_KEYWORDS", ["默认词一", "默认词二"]):
            self.run_refill([], {}, [],
                            argv=["--only-keywords", "--keywords", "疑惑 动漫"])
        self.assertEqual(self.searches, ["疑惑 动漫"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
