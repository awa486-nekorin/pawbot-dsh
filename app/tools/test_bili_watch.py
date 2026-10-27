"""bili_watch：讀外接插件報告、挑報告、原子寫 shared/requests、模型上下文。全部離線。"""
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import bilibili
from pawbot import bili_watch
from pawbot.tools import SCHEMAS, ToolBox, _bili_extra


def fresh_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat().replace("+00:00", "Z")


def make_report(bvid, title="测试视频", watched_level="subtitle", share_worthy=True,
                created_at=None, **extra):
    rep = {
        "version": 1,
        "bvid": bvid,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "title": title,
        "author": "某UP主",
        "duration_sec": 120,
        "play": 10000,
        "pubdate": "2026-10-20T10:00:00Z",
        "tname": "科技",
        "watched_level": watched_level,
        "summary": f"{title}的摘要内容",
        "highlights": ["03:12 具体细节一", "05:00 具体细节二"],
        "comment_vibe": "评论区都在说好",
        "top_comments": [{"text": "前排", "like": 5}],
        "take": "这个挺有意思的",
        "share_worthy": share_worthy,
        "share_reason": "值得一看",
        "created_at": created_at or fresh_iso(),
        "source_request": None,
    }
    rep.update(extra)
    return rep


def write_report(root, rep):
    reports = os.path.join(root, "reports")
    os.makedirs(reports, exist_ok=True)
    path = os.path.join(reports, rep["bvid"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False)
    return path


def read_single_request(root):
    """读 requests 目录里唯一一个 *.json 请求档；多个/没有就返回 (None, files)。"""
    reqdir = os.path.join(root, "requests")
    if not os.path.isdir(reqdir):
        return None, []
    files = [f for f in os.listdir(reqdir) if f.endswith(".json")]
    if len(files) != 1:
        return None, files
    with open(os.path.join(reqdir, files[0]), "r", encoding="utf-8") as f:
        return json.load(f), files


def leftover_tmp_files(root):
    """目录树里残留的 *.tmp（原子写的中间态必须自己清掉）。"""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".tmp"):
                out.append(os.path.join(dirpath, name))
    return out


class _Cfg:
    """最小 config：只支援 dotted get，跟 pawbot.config.Config 的用法一致。"""

    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class LoadReportsTests(unittest.TestCase):
    def test_skips_tmp_badjson_wrong_version_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVgood", title="好的视频"))
            # 坏 JSON
            with open(os.path.join(tmp, "reports", "BVbad.json"), "w", encoding="utf-8") as f:
                f.write("{ 这不是 JSON")
            # .tmp 中间态
            with open(os.path.join(tmp, "reports", "BVtmp.json.tmp"), "w", encoding="utf-8") as f:
                f.write("{}")
            # version != 1
            write_report(tmp, make_report("BVold", title="旧版本", version=2))
            # index.json（插件维护的索引，不是报告）
            with open(os.path.join(tmp, "reports", "index.json"), "w", encoding="utf-8") as f:
                f.write('[{"bvid": "BVindex"}]')

            reps = bili_watch.load_reports(dir=tmp)
        self.assertEqual([r["bvid"] for r in reps], ["BVgood"])

    def test_missing_reports_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bili_watch.load_reports(dir=tmp), [])


class PickReportTests(unittest.TestCase):
    def test_skips_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVexpired", created_at=iso_hours_ago(100)))
            self.assertIsNone(bili_watch.pick_report("s1", max_age_hours=72, dir=tmp))

    def test_skips_already_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVshared"))
            bili_watch.mark_shared("BVshared", "s1", dir=tmp)
            self.assertIsNone(bili_watch.pick_report("s1", dir=tmp))

    def test_skips_not_share_worthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVmeh", share_worthy=False))
            self.assertIsNone(bili_watch.pick_report("s1", dir=tmp))

    def test_keyword_match_beats_level_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            # keyword 命中的是低等级的 title_only，但 keyword 優先於等級
            write_report(tmp, make_report("BVcat", title="猫咪吃播", watched_level="title_only"))
            write_report(tmp, make_report("BVtech", title="量子计算机", watched_level="subtitle"))
            r = bili_watch.pick_report("s1", keyword="猫咪", dir=tmp)
        self.assertEqual(r["bvid"], "BVcat")

    def test_subtitle_beats_desc_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVtitle", title="只有标题", watched_level="title_only"))
            write_report(tmp, make_report("BVdesc", title="只看简介", watched_level="desc_comments"))
            write_report(tmp, make_report("BVsub", title="看完字幕", watched_level="subtitle"))
            r = bili_watch.pick_report("s1", dir=tmp)
        self.assertEqual(r["bvid"], "BVsub")

    def test_desc_beats_title_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVt", title="t", watched_level="title_only"))
            write_report(tmp, make_report("BVd", title="d", watched_level="desc_comments"))
            r = bili_watch.pick_report("s1", dir=tmp)
        self.assertEqual(r["bvid"], "BVd")

    def test_keyword_with_zero_hits_returns_none_not_random_video(self):
        # 庫存只有不相關的片：keyword 0 命中就必須回 None，讓呼叫端去投遞「去看」
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVother", title="量子计算机"))
            self.assertIsNone(bili_watch.pick_report("s1", keyword="猫咪", dir=tmp))

    def test_author_with_zero_hits_returns_none_not_random_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVother", title="别人的片", author="别人"))
            self.assertIsNone(bili_watch.pick_report("s1", author="目标UP", dir=tmp))

    def test_keyword_hit_still_wins_over_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVcat", title="猫咪吃播", watched_level="title_only"))
            write_report(tmp, make_report("BVother", title="量子计算机", watched_level="subtitle"))
            r = bili_watch.pick_report("s1", keyword="猫咪", dir=tmp)
        self.assertEqual(r["bvid"], "BVcat")


class WriteTests(unittest.TestCase):
    def test_mark_shared_is_atomic_and_matches_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.mark_shared("BV1", "session-1", dir=tmp)
            path = os.path.join(tmp, "shared.json")
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("BV1", data)
            self.assertEqual(data["BV1"]["session"], "session-1")
            self.assertIn("shared_at", data["BV1"])
            # 固定名的 path+".tmp" 會被併發寫踩壞，這裡連殘留的 *.tmp 都不許有
            self.assertEqual(leftover_tmp_files(tmp), [])

    def test_mark_shared_concurrent_writes_lose_nothing(self):
        # 8 執行緒 × 30 次：以前固定 tmp 名 → WinError 32、筆數掉、JSON 寫壞
        with tempfile.TemporaryDirectory() as tmp:
            bvids = [f"BV{i:03d}" for i in range(240)]
            errors = []

            def worker(chunk):
                for bvid in chunk:
                    try:
                        bili_watch.mark_shared(bvid, "s-concurrent", dir=tmp)
                    except Exception as e:      # noqa: BLE001 - 就是要抓全部
                        errors.append(f"{bvid}: {type(e).__name__} {e}")

            chunks = [bvids[i::8] for i in range(8)]
            threads = [threading.Thread(target=worker, args=(c,)) for c in chunks]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            with open(os.path.join(tmp, "shared.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(sorted(data), sorted(bvids))
            self.assertEqual(leftover_tmp_files(tmp), [])

    def test_mark_shared_with_thread_pool_matches_count(self):
        # 另一種併發形態：run_in_executor 用的就是 thread pool
        with tempfile.TemporaryDirectory() as tmp:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda i: bili_watch.mark_shared(
                    f"BV{i}", "s-pool", dir=tmp), range(240)))
            with open(os.path.join(tmp, "shared.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data), 240)

    def test_bad_shared_json_warns_once_and_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "shared.json"), "w", encoding="utf-8") as f:
                f.write('{"BV1": {"sess')
            old = bili_watch._warned_bad_shared
            bili_watch._warned_bad_shared = False
            try:
                with patch.object(bili_watch.log, "warn") as warn:
                    self.assertEqual(bili_watch._load_shared(dir=tmp), {})
                    self.assertEqual(bili_watch._load_shared(dir=tmp), {})
                self.assertEqual(warn.call_count, 1)      # 只囉嗦一次
            finally:
                bili_watch._warned_bad_shared = old

    def test_request_watch_is_atomic_and_matches_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = bili_watch.request_watch("keyword", "猫", "session-2", dir=tmp)
            reqdir = os.path.join(tmp, "requests")
            files = [f for f in os.listdir(reqdir) if f.endswith(".json")]
            self.assertEqual(len(files), 1)
            path = os.path.join(reqdir, files[0])
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["id"], req["id"])
            self.assertEqual(len(data["id"]), 32)  # uuid hex
            self.assertEqual(data["kind"], "keyword")
            self.assertEqual(data["value"], "猫")
            self.assertEqual(data["session"], "session-2")
            self.assertIn("requested_at", data)
            self.assertEqual(leftover_tmp_files(tmp), [])

    def test_request_watch_old_contract_unchanged_no_extra_key(self):
        # 旧调用（4 参数）行为完全不变：五个键，没有 extra
        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.request_watch("keyword", "猫", "s-old", dir=tmp)
            data, _files = read_single_request(tmp)
            self.assertEqual(set(data.keys()),
                             {"id", "kind", "value", "requested_at", "session"})
            self.assertEqual(data["kind"], "keyword")
            self.assertEqual(data["value"], "猫")
            self.assertEqual(data["session"], "s-old")
            self.assertNotIn("extra", data)

    def test_request_watch_empty_or_none_extra_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.request_watch("keyword", "猫", "s", dir=tmp, extra={})
            data, files = read_single_request(tmp)
            self.assertEqual(len(files), 1)
            self.assertNotIn("extra", data)

        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.request_watch("keyword", "猫", "s", dir=tmp, extra=None)
            data, _files = read_single_request(tmp)
            self.assertNotIn("extra", data)

    def test_request_watch_extra_written_when_non_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.request_watch("keyword", "猫", "s", dir=tmp,
                                     extra={"days": 3, "order": "pubdate"})
            data, _files = read_single_request(tmp)
            self.assertEqual(data["extra"], {"days": 3, "order": "pubdate"})

    def test_request_watch_drops_unknown_extra_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.request_watch("keyword", "猫", "s", dir=tmp,
                                     extra={"days": 3, "hack": "x", "num": 5})
            data, _files = read_single_request(tmp)
            self.assertEqual(data["extra"], {"days": 3, "num": 5})

    def test_request_watch_kind_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.request_watch("up", "某UP主", "s", dir=tmp)
            data, _files = read_single_request(tmp)
            self.assertEqual(data["kind"], "up")
            self.assertEqual(data["value"], "某UP主")

    def test_request_watch_bvid_with_extra(self):
        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.request_watch("bvid", "BV123", "s", dir=tmp,
                                     extra={"num": 5, "order": "click"})
            data, _files = read_single_request(tmp)
            self.assertEqual(data["kind"], "bvid")
            self.assertEqual(data["value"], "BV123")
            self.assertEqual(data["extra"], {"num": 5, "order": "click"})


class FormatTests(unittest.TestCase):
    def test_title_only_warns_not_to_pretend(self):
        text = bili_watch.format_for_model(make_report("BVt", watched_level="title_only"))
        self.assertIn("别假装看过", text)
        self.assertIn("没看过任何内容", text)

    def test_desc_comments_warns_not_to_pretend(self):
        text = bili_watch.format_for_model(make_report("BVd", watched_level="desc_comments"))
        self.assertIn("别假装看过", text)
        self.assertIn("没看到影片画面", text)

    def test_subtitle_does_not_warn_and_includes_report_body(self):
        rep = make_report("BVs", watched_level="subtitle")
        text = bili_watch.format_for_model(rep)
        self.assertNotIn("别假装看过", text)
        self.assertIn("真的看过", text)
        self.assertIn(rep["summary"], text)
        self.assertIn("具体细节一", text)
        self.assertIn("评论区都在说好", text)
        self.assertIn(rep["take"], text)
        self.assertIn("唔", text)          # 提醒别用「唔…」开头
        self.assertIn("用自己的话", text)

    def test_format_for_model_adds_no_fabrication_hint(self):
        # 防脑补：报告没写清的地方就说「这个没细说」，别拿背景知识补
        text = bili_watch.format_for_model(make_report("BVx", watched_level="subtitle"))
        self.assertIn("这个没细说", text)
        self.assertIn("不要拿你已有的背景知识去补", text)

    def test_bad_field_types_do_not_raise(self):
        # 插件写的报告栏位型别不可信：highlights={} 曾 KeyError、highlights=5 曾 TypeError
        rep = {"version": 1, "bvid": "BVx", "title": "t",
               "highlights": {}, "top_comments": 5}
        text = bili_watch.format_for_model(rep)
        self.assertIn("B站视频《t》", text)
        self.assertNotIn("具体细节：", text)      # 非 list 就当空，不要编

    def test_bad_field_types_all_fields(self):
        rep = {"version": 1, "bvid": "BVx", "title": "t", "url": "u",
               "highlights": 5, "top_comments": "x",
               "summary": 123, "take": [], "comment_vibe": {"a": 1},
               "watched_level": ["subtitle"]}
        text = bili_watch.format_for_model(rep)    # 不许抛
        self.assertIn("t", text)
        self.assertIn("讲的是什么：123", text)      # 非 str 就 str()

    def test_non_dict_report_falls_back_to_minimal_text(self):
        for bad in ("garbage", None, 5, ["x"]):
            text = bili_watch.format_for_model(bad)
            self.assertIn("B站视频《", text)

    def test_partial_report_keeps_title_and_link(self):
        text = bili_watch.format_for_model(
            {"version": 1, "bvid": "BVx", "title": "t", "url": "https://x/BVx",
             "highlights": {"broken": True}})
        self.assertIn("B站视频《t》", text)
        self.assertIn("链接：https://x/BVx", text)


class CredentialTests(unittest.TestCase):
    def test_save_sessdata_writes_and_never_returns_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = "A" * 30
            res = bili_watch.save_sessdata(value, dir=tmp)
            self.assertEqual(set(res.keys()), {"ok", "path", "value_len"})
            self.assertTrue(res["ok"])
            self.assertEqual(res["value_len"], 30)
            # 绝不含值本身
            self.assertNotIn(value, json.dumps(res))
            self.assertNotIn("sessdata", res)
            # 文件内容符合契约
            with open(os.path.join(tmp, "credential.json"), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["sessdata"], value)
            self.assertIn("updated_at", data)

    def test_save_sessdata_rejects_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = bili_watch.save_sessdata("", dir=tmp)
            self.assertFalse(res["ok"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "credential.json")))

    def test_save_sessdata_rejects_too_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = bili_watch.save_sessdata("short", dir=tmp)
            self.assertFalse(res["ok"])

    def test_save_sessdata_rejects_semicolon(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = bili_watch.save_sessdata("A" * 15 + ";" + "B" * 15, dir=tmp)
            self.assertFalse(res["ok"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "credential.json")))

    def test_save_sessdata_rejects_whitespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = bili_watch.save_sessdata("A" * 15 + " " + "B" * 15, dir=tmp)
            self.assertFalse(res["ok"])

    def test_has_credential_env_var_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BILI_SESSDATA": "S" * 20}):
                self.assertTrue(bili_watch.has_credential(dir=tmp))

    def test_has_credential_file_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            bili_watch.save_sessdata("S" * 20, dir=tmp)
            with patch.dict(os.environ, {"BILI_SESSDATA": ""}):
                self.assertTrue(bili_watch.has_credential(dir=tmp))

    def test_has_credential_neither_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"BILI_SESSDATA": ""}):
                self.assertFalse(bili_watch.has_credential(dir=tmp))

    def test_save_sessdata_uses_config_credential_file(self):
        # tools.bili_watch_credential_file 的語意是「檔案」，不是目錄
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nested", "cred.json")
            cfg = _Cfg({
                "tools.bili_watch_credential_file": target,
                "tools.bili_watch_dir": os.path.join(tmp, "watch"),
            })
            value = "C" * 30
            res = bili_watch.save_sessdata(value, config=cfg)
            self.assertTrue(res["ok"])
            self.assertEqual(res["path"], target)
            with open(target, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["sessdata"], value)
            with patch.dict(os.environ, {"BILI_SESSDATA": ""}):
                self.assertTrue(bili_watch.has_credential(config=cfg))

    def test_save_sessdata_config_default_path_is_watch_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = os.path.join(tmp, "watch")
            cfg = _Cfg({"tools.bili_watch_credential_file": "",
                        "tools.bili_watch_dir": watch})
            res = bili_watch.save_sessdata("D" * 25, config=cfg)
            self.assertEqual(res["path"], os.path.join(watch, "credential.json"))
            with patch.dict(os.environ, {"BILI_SESSDATA": ""}):
                self.assertTrue(bili_watch.has_credential(config=cfg))

    def test_explicit_dir_beats_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Cfg({"tools.bili_watch_credential_file":
                        os.path.join(tmp, "elsewhere.json")})
            res = bili_watch.save_sessdata("E" * 25, dir=os.path.join(tmp, "d"),
                                           config=cfg)
            self.assertEqual(res["path"],
                             os.path.join(tmp, "d", "credential.json"))


class ToolWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bilibili._shared_at.clear()
        self.addCleanup(bilibili._shared_at.clear)

    def make_bot(self, tmp, enabled=True):
        return SimpleNamespace(config={
            "tools.bili_watch_dir": tmp,
            "tools.bili_watch_enabled": enabled,
            "tools.bili_min_gap": 0,
            "tools.bili_json_card": False,
        })

    def test_description_is_honest(self):
        desc = SCHEMAS["bili_video"]["function"]["description"]
        self.assertIn("还没看", desc)
        self.assertIn("看过", desc)

    async def test_uses_report_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVwatch", title="看过的好视频", watched_level="subtitle"))
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            text, media = await box._t_bili_video(ctx, "")
            self.assertIn("真的看过", text)
            self.assertIn("看过的好视频", text)
            joined = " ".join(str(s.get("data", {}).get("text", "")) for s in media)
            self.assertIn("看过的好视频", joined)
            self.assertIn("BVwatch", joined)
            # 挑到报告还不算分享；真正送达才会写 shared.json / 更新节流。
            self.assertTrue(bilibili.share_allowed("group_1", 3600))
            self.assertFalse(os.path.exists(os.path.join(tmp, "shared.json")))
            self.assertEqual(media[0]["_bili_share"]["bvid"], "BVwatch")

    async def test_requests_watch_and_stays_honest_without_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "猫")
            self.assertIn("还没看过", text)
            self.assertIn("别假装看过", text)
            reqdir = os.path.join(tmp, "requests")
            reqs = [f for f in os.listdir(reqdir) if f.endswith(".json")]
            self.assertEqual(len(reqs), 1)
            with open(os.path.join(reqdir, reqs[0]), "r", encoding="utf-8") as f:
                req = json.load(f)
            self.assertEqual(req["kind"], "keyword")
            self.assertEqual(req["value"], "猫")

    async def test_disabled_skips_report_and_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(self.make_bot(tmp, enabled=False))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "")
        self.assertFalse(os.path.exists(os.path.join(tmp, "requests")))
        self.assertIn("还没看过", text)
        self.assertIn("别假装看过", text)

    async def test_up_prefers_report_by_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVup", title="该UP的片", author="目标UP主"))
            write_report(tmp, make_report("BVother", title="别人的片", author="别人"))
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            text, media = await box._t_bili_video(ctx, "", up="目标UP主")
            self.assertIn("真的看过", text)
            self.assertIn("该UP的片", text)
            # 有报告就不投递新请求
            self.assertFalse(os.path.exists(os.path.join(tmp, "requests")))

    async def test_up_without_report_requests_up_watch(self):
        # P2-8：up 不帶 days/order（插件根本不看）
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "", up="某UP主",
                                                      days=7, order="最新")
            data, files = read_single_request(tmp)
            self.assertEqual(len(files), 1)
            self.assertEqual(data["kind"], "up")
            self.assertEqual(data["value"], "某UP主")
            self.assertNotIn("extra", data)

    async def test_up_with_only_other_authors_report_requests_up_watch(self):
        # P1-1：庫存只有別人的片，up 定向需求也不能拿別的片充數
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVother", title="别人的片", author="别人"))
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "", up="目标UP")
            self.assertNotIn("别人的片", text)
            data, files = read_single_request(tmp)
            self.assertEqual(len(files), 1)
            self.assertEqual(data["kind"], "up")
            self.assertEqual(data["value"], "目标UP")

    async def test_up_uid_skips_report_and_requests_up_watch(self):
        # 純數字 uid 永遠比不上 author 暱稱：直接當沒命中並投遞 kind=up
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVup", title="该UP的片", author="目标UP主"))
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "", up="12345678")
            self.assertNotIn("该UP的片", text)
            data, files = read_single_request(tmp)
            self.assertEqual(len(files), 1)
            self.assertEqual(data["kind"], "up")
            self.assertEqual(data["value"], "12345678")

    async def test_keyword_zero_hit_requests_keyword_watch(self):
        # P1-1：keyword 0 命中 → 不拿別的片充數，照常投遞 keyword 請求
        with tempfile.TemporaryDirectory() as tmp:
            write_report(tmp, make_report("BVother", title="量子计算机"))
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "猫咪")
            self.assertNotIn("量子计算机", text)
            data, files = read_single_request(tmp)
            self.assertEqual(len(files), 1)
            self.assertEqual(data["kind"], "keyword")
            self.assertEqual(data["value"], "猫咪")

    async def test_title_only_path_defers_shared_until_delivery(self):
        # 只看标题也要等实际送达，挑到候选时不能写 shared.json。
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"bvid": "BVhot", "title": "热门标题",
                     "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "")
            self.assertFalse(os.path.exists(os.path.join(tmp, "shared.json")))
            self.assertTrue(bilibili.share_allowed("group_1", 3600))
            self.assertEqual(media[0]["_bili_share"]["bvid"], "BVhot")

    async def test_days_and_order_go_into_extra(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "猫", days=7, order="最新")
            data, files = read_single_request(tmp)
            self.assertEqual(len(files), 1)
            self.assertEqual(data["kind"], "keyword")
            self.assertEqual(data["value"], "猫")
            self.assertEqual(data["extra"], {"days": 7, "order": "pubdate"})

    async def test_weird_params_do_not_crash(self):
        # 模型给奇怪的值也不许 KeyError / 崩溃：非法 days/order 被丢弃
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "", up="",
                                                      days="不是数字", order="乱写的")
            data, files = read_single_request(tmp)
            self.assertEqual(len(files), 1)
            self.assertEqual(data["kind"], "auto")
            self.assertNotIn("extra", data)

    async def test_inf_days_clamped_not_overflow(self):
        # P2-4：days=inf 以前 int() 直接 OverflowError；现在夹到 365
        with tempfile.TemporaryDirectory() as tmp:
            box = ToolBox(self.make_bot(tmp))
            ctx = SimpleNamespace(session_id="group_1")
            fake = [{"title": "热门标题", "url": "https://www.bilibili.com/video/BVhot",
                     "author": "UP", "play": 100, "tname": "科技", "pic": ""}]
            with patch.object(bilibili, "pick_lively", return_value=fake):
                text, media = await box._t_bili_video(ctx, "猫", days=float("inf"))
            data, files = read_single_request(tmp)
            self.assertEqual(data["extra"], {"days": 365})
            self.assertIn("还没看过", text)


class BiliExtraTests(unittest.TestCase):
    """_bili_extra：模型给的值再怎么离谱也不许抛，范围要夹住。"""

    def test_inf_and_nan_and_huge(self):
        self.assertEqual(_bili_extra(float("inf"), None), {"days": 365})
        self.assertEqual(_bili_extra(float("-inf"), None), None)
        self.assertEqual(_bili_extra(10 ** 9, None), {"days": 365})
        self.assertEqual(_bili_extra(7, None), {"days": 7})
        self.assertEqual(_bili_extra(0, None), None)          # 0 = 不要日期过滤
        self.assertEqual(_bili_extra(-3, None), None)
        self.assertEqual(_bili_extra(float("nan"), None), None)
        self.assertEqual(_bili_extra("不是数字", None), None)

    def test_num_clamped(self):
        self.assertEqual(_bili_extra(None, None, 500), {"num": 100})
        self.assertEqual(_bili_extra(None, None, 0), {"num": 1})
        self.assertEqual(_bili_extra(None, None, 5), {"num": 5})
        self.assertEqual(_bili_extra(None, None, "x"), None)

    def test_order_normalized(self):
        self.assertEqual(_bili_extra(None, "最新"), {"order": "pubdate"})
        self.assertEqual(_bili_extra(None, "乱写的"), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
