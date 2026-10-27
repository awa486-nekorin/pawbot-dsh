"""實時（近實時）雲同步測試：本地變化 push、遠端變化 pull、衝突策略、啟動條件。

    py -m unittest tools.test_realtime_sync -q

全程離線：provider=local，用臨時目錄當「雲端」。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import cloudsync, sync_realtime                 # noqa: E402
from pawbot.config import DEFAULT                            # noqa: E402


class Cfg(dict):
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


class RealtimeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.base = root / "baseA"
        (self.base / "data").mkdir(parents=True)
        self.remote = root / "cloud"
        self.remote.mkdir()
        self._write_persona(self.base, "我是大肥鱼。")
        self.cfg = Cfg({
            "enabled": True,
            "provider": "local",
            "url": str(self.remote),
            "include_persona": True,
            "include_memory": False,
            "include_config": False,
            "include_stickers": False,
            "passphrase": "",
            "mode": "realtime",
        })
        self.syncers = []

    def tearDown(self):
        for syncer in self.syncers:
            syncer.stop()
        self.tmp.cleanup()

    def _write_persona(self, base, text):
        (base / "data" / "persona.md").write_text(text, encoding="utf-8")

    def _persona(self, base):
        return (base / "data" / "persona.md").read_text(encoding="utf-8")

    def _syncer(self, base=None, **kwargs):
        kwargs.setdefault("debounce_sec", 0.0)
        kwargs.setdefault("remote_poll_sec", 600.0)
        syncer = sync_realtime.RealtimeSyncer(self.cfg, str(base or self.base), **kwargs)
        self.syncers.append(syncer)
        return syncer

    def _push_remote(self, persona="远端的人设。"):
        """用另一个 base 造一个远端包（模拟另一台机器）。"""
        other = Path(self.tmp.name) / "baseB"
        (other / "data").mkdir(parents=True)
        self._write_persona(other, persona)
        cloudsync.push(self.cfg, str(other))
        # push 不会写 manifest；实时同步靠 manifest 判断远端变化，这里补一份
        manifest = {
            "format": "pawbot-sync-manifest", "format_version": 1,
            "sha256": cloudsync._sha256((self.remote / cloudsync.PLAIN_NAME).read_bytes()),
            "bundle_name": cloudsync.PLAIN_NAME,
        }
        (self.remote / sync_realtime.MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


class LocalPushTests(RealtimeBase):
    def test_change_is_pushed_after_debounce(self):
        syncer = self._syncer(debounce_sec=0.5)
        # 第一次只记 pending，不传
        self.assertFalse(syncer.tick_local(now=0.0))
        self.assertFalse((self.remote / cloudsync.PLAIN_NAME).exists())
        # 第二次还在防抖里也不传
        self.assertFalse(syncer.tick_local(now=0.1))
        # 超过防抖 → 真的 push + 写 manifest
        self.assertTrue(syncer.tick_local(now=1.0))
        self.assertTrue((self.remote / cloudsync.PLAIN_NAME).exists())
        self.assertTrue((self.remote / sync_realtime.MANIFEST_NAME).exists())
        self.assertEqual(syncer.state["bundle_sha"],
                         json.loads((self.remote / sync_realtime.MANIFEST_NAME).read_text("utf-8"))["sha256"])

    def test_no_change_does_not_push(self):
        syncer = self._syncer()
        syncer.state["local_fp"] = sync_realtime.local_fingerprint(self.cfg, str(self.base))
        self.assertFalse(syncer.tick_local(now=0.0))
        self.assertFalse(syncer.tick_local(now=99.0))
        self.assertFalse((self.remote / cloudsync.PLAIN_NAME).exists())


class RemotePullTests(RealtimeBase):
    def _seed_state(self, syncer):
        syncer.state["local_fp"] = sync_realtime.local_fingerprint(self.cfg, str(self.base))
        syncer.state["bundle_sha"] = "old-sha"
        syncer.state["last_pull"] = ""

    def test_remote_change_is_pulled(self):
        self._push_remote("远端的人设 v2。")
        syncer = self._syncer()
        self._seed_state(syncer)
        result = syncer.tick_remote(now=1.0)
        self.assertTrue(result.get("pulled"), result)
        self.assertEqual(self._persona(self.base), "远端的人设 v2。")
        self.assertNotEqual(syncer.state["bundle_sha"], "old-sha")

    def test_first_run_keeps_both_by_default(self):
        self._push_remote("远端的人设 v2。")
        syncer = self._syncer()
        result = syncer.tick_remote(now=1.0)
        self.assertTrue(result.get("conflict"), result)
        self.assertEqual(self._persona(self.base), "我是大肥鱼。")   # 本机没被覆盖
        out_dir = Path(result["dir"])
        self.assertTrue((out_dir / "remote.bundle.bin").exists())
        self.assertTrue((out_dir / "README.txt").exists())

    def test_first_run_pull_policy(self):
        self.cfg["sync"]["first_run"] = "pull"
        self._push_remote("远端的人设 v2。")
        syncer = self._syncer()
        result = syncer.tick_remote(now=1.0)
        self.assertTrue(result.get("pulled"), result)
        self.assertEqual(self._persona(self.base), "远端的人设 v2。")


class ConflictTests(RealtimeBase):
    def _both_changed(self, policy):
        self.cfg["sync"]["conflict_policy"] = policy
        self._push_remote("远端的人设 v2。")
        syncer = self._syncer()
        syncer.state["local_fp"] = sync_realtime.local_fingerprint(self.cfg, str(self.base))
        syncer.state["bundle_sha"] = "old-sha"
        self._write_persona(self.base, "本机也改了。")
        return syncer, syncer.tick_remote(now=1.0)

    def test_keep_both_saves_remote_and_keeps_local(self):
        syncer, result = self._both_changed("keep_both")
        self.assertTrue(result.get("conflict"), result)
        self.assertEqual(self._persona(self.base), "本机也改了。")
        self.assertTrue(Path(result["dir"]).is_dir())

    def test_local_policy_ignores_remote(self):
        syncer, result = self._both_changed("local")
        self.assertTrue(result.get("ignored"), result)
        self.assertEqual(self._persona(self.base), "本机也改了。")

    def test_remote_policy_overwrites(self):
        syncer, result = self._both_changed("remote")
        self.assertTrue(result.get("pulled"), result)
        self.assertEqual(self._persona(self.base), "远端的人设 v2。")


class StartTests(RealtimeBase):
    def test_start_realtime_requires_enabled_and_mode(self):
        cfg = Cfg({"enabled": False, "provider": "local", "url": str(self.remote), "mode": "realtime"})
        self.assertIsNone(sync_realtime.start_realtime(cfg, str(self.base)))
        cfg = Cfg({"enabled": True, "provider": "local", "url": str(self.remote), "mode": "manual"})
        self.assertIsNone(sync_realtime.start_realtime(cfg, str(self.base)))

    def test_thread_starts_and_stops(self):
        syncer = sync_realtime.start_realtime(self.cfg, str(self.base),
                                              poll_sec=0.2, debounce_sec=0.1, remote_poll_sec=600)
        self.assertIsNotNone(syncer)
        self.assertTrue(syncer.is_alive())
        syncer.stop()
        self.assertFalse(syncer.is_alive())

    def test_state_file_written(self):
        syncer = self._syncer()
        syncer.tick_local(now=0.0)
        syncer.tick_local(now=1.0)
        self.assertTrue((self.base / "data" / sync_realtime.STATE_NAME).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
