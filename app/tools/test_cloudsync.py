"""云同步測試：本地往返、加密、路徑穿越、HTTP 假服務。

    py -m unittest tools.test_cloudsync -q

全程離線；HTTP 用本機 http.server 起一個假端點，不連外網。
"""

from __future__ import annotations

import copy
import gc
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot import cloudsync                                    # noqa: E402
from pawbot.config import DEFAULT                               # noqa: E402


class Cfg(dict):
    """Config 的替身：支持 'a.b' 點號讀寫，不改真的 config.json。"""

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


class FakeHttpHandler(BaseHTTPRequestHandler):
    blobs = {}
    token = "good-token"

    def log_message(self, *args):  # 別把測試日誌刷滿屏
        pass

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {self.token}"

    def _name(self):
        return parse_qs(urlparse(self.path).query).get("name", [""])[0]

    def do_POST(self):
        if not self._authed():
            return self._reply(403)
        name = self._name()
        if not name:
            return self._reply(400)
        length = int(self.headers.get("Content-Length") or 0)
        FakeHttpHandler.blobs[name] = self.rfile.read(length)
        self._reply(201)

    def do_GET(self):
        if not self._authed():
            return self._reply(403)
        if self.path.startswith("/boom"):
            return self._reply(500)
        name = self._name()
        blob = FakeHttpHandler.blobs.get(name)
        if blob is None:
            return self._reply(404)
        self._reply(200, blob)


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.data = os.path.join(self.base, "data")
        self.remote = os.path.join(self.base, "remote")
        os.makedirs(os.path.join(self.data, "memory"))
        os.makedirs(os.path.join(self.data, "people"))
        os.makedirs(os.path.join(self.data, "brain"))
        os.makedirs(os.path.join(self.data, "stickers"))

        self.write("data/persona.md", "我是大肥魚，住在海裡。\n")
        self.write("data/memory/facts.md", "- 主人喜歡數學\n")
        self.write("data/memory/slang.md", "- 絕絕子 = 太棒了\n")
        self.write("data/memory/notes.tmp", "臨時文件，不該進包")
        self.write("data/people/123.md", "阿明：愛問技術問題\n")
        self.write("data/relations.json", json.dumps({"123": 60}, ensure_ascii=False))
        self.write("data/sessions.json", json.dumps({"group_1": []}))
        self.write("data/memes.json", json.dumps({"梗": 1}, ensure_ascii=False))
        self.write("data/brain/secret.txt", "不該同步的大腦")
        self.write("data/stickers/开心.png", b"\x89PNG\r\n")
        self.write("data/stickers/index.json.bak-20260101", "{}")
        self.write("data/run.log", "日志不該進包")
        self.write("data/config.json", json.dumps({
            "bot": {"nickname": "雲上魚"},
            "llm": {"api_key": "sk-remote-secret"},
            "memory": {"engine": "legacy"},
        }, ensure_ascii=False, indent=2))

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------ 工具

    def write(self, rel: str, content) -> str:
        path = os.path.join(self.base, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": ""}
        with open(path, mode, **kwargs) as handle:
            handle.write(content)
        return path

    def read(self, rel: str):
        path = os.path.join(self.base, *rel.split("/"))
        with open(path, "rb") as handle:
            return handle.read()

    def cfg(self, **sync):
        base = {"enabled": True, "provider": "local", "url": self.remote,
                "include_config": False}
        base.update(sync)
        return Cfg(base)

    def remote_tar(self, name=cloudsync.PLAIN_NAME) -> tarfile.TarFile:
        return tarfile.open(os.path.join(self.remote, name), "r:gz")

    def make_bundle(self, files: dict[str, bytes], entries=None,
                    extra_members=None) -> bytes:
        entries = entries or []
        manifest = {
            "format": cloudsync.BUNDLE_FORMAT,
            "format_version": 1,
            "pawbot_version": cloudsync.PAWBOT_VERSION,
            "created_at": "2026-10-27T00:00:00Z",
            "encrypted": False,
            "entry_count": len(entries),
            "entries": entries,
        }
        payload = dict(files)
        payload[cloudsync.MANIFEST_NAME] = json.dumps(manifest, ensure_ascii=False).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, blob in payload.items():
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
            for info in extra_members or []:
                tar.addfile(info)
        return buffer.getvalue()

    def entry(self, path: str, target: str, blob: bytes) -> dict:
        return {"path": path, "target": target, "size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest()}

    # ------------------------------------------------------------ 本地往返

    def test_local_roundtrip_persona_and_manifest(self):
        cfg = self.cfg()
        result = cloudsync.push(cfg, self.base)

        self.assertTrue(result["ok"])
        self.assertFalse(result["encrypted"])
        self.assertGreaterEqual(result["files"], 7)
        self.assertTrue(os.path.isfile(os.path.join(self.remote, cloudsync.PLAIN_NAME)))
        self.assertEqual([n for n in os.listdir(self.remote) if ".tmp" in n], [])
        self.assertTrue(cfg.get("sync.last_push"))

        with self.remote_tar() as tar:
            names = set(tar.getnames())
            self.assertIn("persona.md", names)
            self.assertIn("manifest.json", names)
            self.assertIn("memory/facts.md", names)
            self.assertIn("memory/people/123.md", names)
            self.assertIn("memory/sessions.json", names)
            self.assertNotIn("config.json", names)
            self.assertNotIn("memory/notes.tmp", names)
            self.assertFalse(any("brain" in n for n in names))
            self.assertFalse(any(n.startswith("stickers/") for n in names))

            manifest = json.loads(tar.extractfile("manifest.json").read().decode())
            self.assertEqual(manifest["format"], cloudsync.BUNDLE_FORMAT)
            self.assertEqual(manifest["format_version"], 1)
            self.assertTrue(manifest["created_at"])
            self.assertTrue(manifest["pawbot_version"])
            self.assertFalse(manifest["encrypted"])
            self.assertEqual(manifest["entry_count"], len(manifest["entries"]))
            for item in manifest["entries"]:
                blob = tar.extractfile(item["path"]).read()
                self.assertEqual(item["size"], len(blob))
                self.assertEqual(item["sha256"], hashlib.sha256(blob).hexdigest())

        # 本地改壞人設，pull 應該拉回雲端版本，並先備份本地那份
        self.write("data/persona.md", "本地亂改的人設")
        pulled = cloudsync.pull(cfg, self.base)
        self.assertIn("persona.md", pulled["applied"])
        self.assertEqual(self.read("data/persona.md").decode(), "我是大肥魚，住在海裡。\n")
        self.assertTrue(pulled["backup_dir"])
        backup_persona = os.path.join(self.base, *pulled["backup_dir"].split("/"),
                                      "persona.md")
        with open(backup_persona, "rb") as handle:
            self.assertEqual(handle.read().decode(), "本地亂改的人設")
        self.assertTrue(cfg.get("sync.last_pull"))

        # 再拉一次：內容一致，不重寫也不再攢備份
        again = cloudsync.pull(cfg, self.base)
        self.assertNotIn("persona.md", again["applied"])
        self.assertIn("persona.md", again["unchanged"])

    def test_include_memory_false_skips_memory(self):
        cfg = self.cfg(include_memory=False)
        cloudsync.push(cfg, self.base)

        with self.remote_tar() as tar:
            names = set(tar.getnames())
        self.assertIn("persona.md", names)
        self.assertIn("manifest.json", names)
        self.assertFalse(any(n.startswith("memory/") for n in names))
        self.assertFalse(any(n.startswith("people/") for n in names))

    def test_include_stickers_opt_in(self):
        cfg = self.cfg(include_stickers=True)
        result = cloudsync.push(cfg, self.base)
        self.assertTrue(result["includes"]["stickers"])
        with self.remote_tar() as tar:
            names = set(tar.getnames())
        self.assertIn("stickers/开心.png", names)
        self.assertNotIn("stickers/index.json.bak-20260101", names)

    def test_config_requires_encryption_when_plain(self):
        cfg = self.cfg(include_config=True)
        with self.assertRaises(cloudsync.SyncError) as ctx:
            cloudsync.push(cfg, self.base)
        self.assertEqual(ctx.exception.code, "CONFIG_REQUIRES_ENCRYPTION")
        self.assertFalse(os.path.exists(os.path.join(self.remote, cloudsync.PLAIN_NAME)))

    # ------------------------------------------------------------ 加密

    @unittest.skipUnless(cloudsync.crypto_available(), "沒有 cryptography / pycryptodome")
    def test_encrypted_roundtrip_and_wrong_passphrase(self):
        passphrase = "correct horse battery staple"
        cfg = self.cfg(include_config=True, passphrase=passphrase)
        result = cloudsync.push(cfg, self.base)

        self.assertTrue(result["encrypted"])
        self.assertEqual(result["remote_name"], cloudsync.ENCRYPTED_NAME)
        self.assertEqual(result["encryption"], cloudsync.ENCRYPTION_ALGO)
        blob = self.read(f"remote/{cloudsync.ENCRYPTED_NAME}")
        self.assertTrue(cloudsync.is_encrypted(blob))
        self.assertNotEqual(blob[:2], b"\x1f\x8b")
        self.assertNotIn(b"sk-remote-secret", blob)
        self.assertNotIn(passphrase.encode(), blob)

        # 本地改成和雲端不同，pull 後默認只寫 config.synced.json
        self.write("data/config.json", json.dumps({
            "bot": {"nickname": "本地魚"},
            "llm": {"api_key": "sk-local-secret"},
        }, ensure_ascii=False, indent=2))
        local_config_before = self.read("data/config.json")
        pulled = cloudsync.pull(cfg, self.base)

        self.assertTrue(pulled["encrypted"])
        self.assertTrue(os.path.isfile(os.path.join(self.data, cloudsync.CONFIG_SYNCED)))
        synced = json.loads(self.read(f"data/{cloudsync.CONFIG_SYNCED}"))
        self.assertEqual(synced["bot"]["nickname"], "雲上魚")
        self.assertEqual(synced["llm"]["api_key"], "sk-remote-secret")
        self.assertEqual(self.read("data/config.json"), local_config_before)
        diff_keys = set(pulled["config"]["diff"]["changed"])
        self.assertIn("llm.api_key", diff_keys)
        self.assertIn("bot.nickname", diff_keys)
        # 返回值和日誌都不該帶出配置裡的 key
        self.assertNotIn("sk-remote-secret", json.dumps(pulled, ensure_ascii=False))

        wrong = self.cfg(include_config=True, passphrase="wrong-passphrase")
        with self.assertRaises(cloudsync.SyncError) as ctx:
            cloudsync.pull(wrong, self.base)
        self.assertEqual(ctx.exception.code, "BAD_PASSPHRASE")

    # ------------------------------------------------------------ 配置衝突

    @unittest.skipUnless(cloudsync.crypto_available(), "沒有 cryptography / pycryptodome")
    def test_pull_overwrite_config_backs_up_and_keeps_sync_section(self):
        cfg = self.cfg(include_config=True, passphrase="pw-for-overwrite")
        cloudsync.push(cfg, self.base)

        self.write("data/config.json", json.dumps({
            "bot": {"nickname": "本地魚"},
            "llm": {"api_key": "sk-local-secret"},
            "sync": {"provider": "local", "url": self.remote, "passphrase": "keep-me"},
        }, ensure_ascii=False, indent=2))

        pulled = cloudsync.pull(cfg, self.base, overwrite_config=True)
        self.assertTrue(pulled["config"]["overwritten"])
        merged = json.loads(self.read("data/config.json"))
        self.assertEqual(merged["bot"]["nickname"], "雲上魚")
        self.assertEqual(merged["llm"]["api_key"], "sk-remote-secret")
        # 本機的 sync 段不被雲端配置頂掉，否則會把自己的同步配置弄壞
        self.assertEqual(merged["sync"]["passphrase"], "keep-me")

        backup = pulled["config"]["backup_dir"]
        self.assertTrue(backup)
        self.assertIn("config.json", pulled["config"]["backed_up"])
        backup_config = json.loads(self.read(f"{backup}/config.json"))
        self.assertEqual(backup_config["bot"]["nickname"], "本地魚")
        self.assertEqual(backup_config["sync"]["passphrase"], "keep-me")

    @unittest.skipUnless(cloudsync.crypto_available(), "沒有 cryptography / pycryptodome")
    def test_default_pull_does_not_backup_config(self):
        cfg = self.cfg(include_config=True, passphrase="pw-default")
        cloudsync.push(cfg, self.base)
        self.write("data/config.json", json.dumps({"bot": {"nickname": "本地魚"}},
                                                  ensure_ascii=False))
        pulled = cloudsync.pull(cfg, self.base, overwrite_config=False)
        self.assertFalse(pulled["config"]["overwritten"])
        if pulled["config"].get("backed_up"):
            self.assertNotIn("config.json", pulled["config"]["backed_up"])

    # ------------------------------------------------------------ 安全：路徑穿越

    def test_path_traversal_and_symlink_rejected(self):
        os.makedirs(self.remote, exist_ok=True)
        evil = b"owned"

        cases = {
            "dotdot": ("../evil.txt", "../evil.txt", None),
            "absolute": ("/tmp/evil.txt", "persona.md", None),
            "drive": ("C:/evil.txt", "persona.md", None),
        }
        for label, (member, target, _unused) in cases.items():
            with self.subTest(label=label):
                payload = self.make_bundle(
                    {member: evil},
                    [self.entry(member, target, evil)],
                )
                with open(os.path.join(self.remote, cloudsync.PLAIN_NAME), "wb") as handle:
                    handle.write(payload)
                with self.assertRaises(cloudsync.SyncError) as ctx:
                    cloudsync.pull(self.cfg(), self.base)
                self.assertEqual(ctx.exception.code, "PATH_TRAVERSAL")
                self.assertFalse(os.path.exists(os.path.join(self.base, "evil.txt")))

        # 符號連結：tar 裡不允許
        symlink = tarfile.TarInfo("memory/link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/etc/passwd"
        payload = self.make_bundle({}, [], extra_members=[symlink])
        with open(os.path.join(self.remote, cloudsync.PLAIN_NAME), "wb") as handle:
            handle.write(payload)
        with self.assertRaises(cloudsync.SyncError) as ctx:
            cloudsync.pull(self.cfg(), self.base)
        self.assertEqual(ctx.exception.code, "PATH_TRAVERSAL")

    def test_manifest_sha256_mismatch_rejected(self):
        os.makedirs(self.remote, exist_ok=True)
        blob = b"evil"
        entry = {"path": "persona.md", "target": "persona.md", "size": len(blob),
                 "sha256": hashlib.sha256(b"good").hexdigest()}
        payload = self.make_bundle({"persona.md": blob}, [entry])
        with open(os.path.join(self.remote, cloudsync.PLAIN_NAME), "wb") as handle:
            handle.write(payload)
        with self.assertRaises(cloudsync.SyncError) as ctx:
            cloudsync.pull(self.cfg(), self.base)
        self.assertEqual(ctx.exception.code, "MANIFEST_MISMATCH")

    # ------------------------------------------------------------ HTTP 假端點

    def test_http_push_pull_and_http_errors(self):
        FakeHttpHandler.blobs = {}
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHttpHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        endpoint = f"http://127.0.0.1:{port}/sync"
        try:
            cfg = self.cfg(provider="http", url=endpoint, token="good-token")
            result = cloudsync.push(cfg, self.base)
            self.assertEqual(result["provider"], "http")
            self.assertIn(cloudsync.PLAIN_NAME, FakeHttpHandler.blobs)

            # 換一個目錄 pull，驗證真的從 HTTP 下載回來了
            other = tempfile.TemporaryDirectory()
            self.addCleanup(other.cleanup)
            os.makedirs(os.path.join(other.name, "data"))
            remote_cfg = self.cfg(provider="http", url=endpoint, token="good-token")
            pulled = cloudsync.pull(remote_cfg, other.name)
            self.assertIn("persona.md", pulled["applied"])
            with open(os.path.join(other.name, "data", "persona.md"), "rb") as handle:
                self.assertEqual(handle.read().decode(), "我是大肥魚，住在海裡。\n")

            bad = self.cfg(provider="http", url=endpoint, token="bad-token")
            with self.assertRaises(cloudsync.SyncError) as ctx:
                cloudsync.push(bad, self.base)
            self.assertEqual(ctx.exception.code, "AUTH_FAILED")
            self.assertEqual(ctx.exception.http_status, 403)

            boom = self.cfg(provider="http", url=f"http://127.0.0.1:{port}/boom",
                            token="good-token")
            with self.assertRaises(cloudsync.SyncError) as ctx:
                cloudsync.pull(boom, self.base)
            self.assertEqual(ctx.exception.code, "HTTP_5XX")
            self.assertEqual(ctx.exception.http_status, 500)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            # http.server 的工作線程退出後才 GC，這裡收一下免得刷 ResourceWarning
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                gc.collect()

    # ------------------------------------------------------------ 狀態 / 啟動鉤子

    def test_status_contains_no_secrets(self):
        cfg = self.cfg(provider="webdav",
                       url="https://davuser:davpass@dav.example.com/remote/",
                       username="davuser", password="p@ss-word-123",
                       token="tok-secret-xyz", passphrase="passphrase-secret")
        info = cloudsync.status(cfg, self.base)
        dumped = json.dumps(info, ensure_ascii=False)

        for secret in ("p@ss-word-123", "tok-secret-xyz", "passphrase-secret", "davpass"):
            self.assertNotIn(secret, dumped)
        self.assertNotIn("davuser@", info["url"])
        self.assertIn("dav.example.com", info["url"])
        self.assertTrue(info["encrypted"])
        self.assertTrue(info["password_set"])
        self.assertTrue(info["token_set"])
        self.assertTrue(info["passphrase_set"])
        self.assertEqual(info["provider"], "webdav")
        self.assertEqual(info["supported_providers"],
                         list(cloudsync.SUPPORTED_PROVIDERS))

    def test_status_flags_config_without_passphrase(self):
        info = cloudsync.status(self.cfg(include_config=True), self.base)
        self.assertEqual(info["warning"], "CONFIG_REQUIRES_ENCRYPTION")

    def test_start_auto_pull_respects_flags(self):
        cfg = self.cfg(auto_pull_on_start=False)
        self.assertIsNone(cloudsync.start_auto_pull(cfg, self.base))

        cloudsync.push(self.cfg(), self.base)
        self.write("data/persona.md", "本機已經沒有云端的版本")
        auto = self.cfg(auto_pull_on_start=True)
        thread = cloudsync.start_auto_pull(auto, self.base)
        self.assertIsNotNone(thread)
        thread.join(15)
        self.assertEqual(self.read("data/persona.md").decode(), "我是大肥魚，住在海裡。\n")

    def test_test_connection_local(self):
        info = cloudsync.test_connection(self.cfg(), self.base)
        self.assertTrue(info["ok"])
        self.assertEqual(info["provider"], "local")
        self.assertIn("目录可读写", info["detail"])


if __name__ == "__main__":
    unittest.main()
