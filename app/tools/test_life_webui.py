"""GET /api/life 的真实 loopback 回归：临时目录、真 LifeStore、真 Autonomy。

面板 handler 本身不 mock；HTTP 走 127.0.0.1 的真实 asyncio 服务。缺组件或单块
读取失败时只返回 null + 中文 errors，不补默认剧情、不伪造资源余额。
测试不启动 Bot、QQ、浏览器或模型请求，也不读写真实 data。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.autonomy import Autonomy
from pawbot.config import Config
from pawbot.life_store import LifeStore
from pawbot.paths import DataPaths
from pawbot.webui import WebUI


class LifeWebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pawbot-life-webui-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.paths = DataPaths(root=self.root, base_dir=self.root)
        self.paths.ensure_layout()
        self.config = Config(os.path.join(self.root, "config.json"))
        self.web = None

    async def asyncTearDown(self):
        if self.web is not None:
            await self.web.stop()

    def make_bot(self, *, life_store=True, autonomy=True):
        values = {
            "base_dir": self.root,
            "paths": self.paths,
            "config": self.config,
        }
        if life_store is True:
            values["life_store"] = LifeStore(self.root, self.config, paths=self.paths)
        elif life_store is not None and life_store is not False:
            values["life_store"] = life_store
        if autonomy is True:
            bot = SimpleNamespace(**values)
            values["autonomy"] = Autonomy(bot, now=time.time)
        elif autonomy is not None and autonomy is not False:
            values["autonomy"] = autonomy
        return SimpleNamespace(**values)

    async def serve(self, bot):
        self.web = WebUI(bot, host="127.0.0.1", port=0)
        await self.web.start()
        self.port = self.web._server.sockets[0].getsockname()[1]

    async def get_json(self, path):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write((
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Connection: close\r\n\r\n"
        ).encode())
        await writer.drain()
        try:
            raw = await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        return status, json.loads(body.decode("utf-8"))

    async def test_life_route_returns_real_growth_and_autonomy(self):
        bot = self.make_bot()
        store = bot.life_store
        # 真实实践 fixture：observed_level 必须是实际执行，摘要也写清完成了什么。
        # 只“看过教程”(read) 能计为领域了解，不能伪装成 practice。
        experience = store.record_experience(
            "test:coffee:1", "按教程实际手冲了一杯咖啡，记录水温和注水节奏",
            platform="local", topics=["手冲咖啡"], reaction="想再试一次",
            observed_level="executed", salience=4)
        practiced = store.practice(
            "手冲咖啡", experience_id=experience["id"], mode="practice",
            outcome="success", artifact="第一次手冲记录", people=["小满"])
        self.assertTrue(practiced["ok"], practiced)
        self.assertFalse(practiced.get("downgraded"), practiced)
        self.assertEqual(practiced.get("mode"), "practice", practiced)
        await self.serve(bot)

        status, payload = await self.get_json("/api/life")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(set(data), {"growth", "autonomy"})
        growth = data["growth"]
        autonomy = data["autonomy"]
        self.assertIsInstance(growth, dict)
        self.assertIsInstance(autonomy, dict)
        self.assertEqual(growth["skills"][0]["topic"], "手冲咖啡")
        self.assertEqual(growth["skills"][0]["practice_count"], 1)
        self.assertTrue(growth["mementos"])
        self.assertTrue(growth["artifacts"])
        self.assertFalse(growth["resources"]["known"])
        self.assertIsNone(growth["resources"]["balance"])
        self.assertTrue(autonomy["enabled"])
        self.assertGreater(float(autonomy["next_at"]), 0)
        self.assertIn("next_in_sec", autonomy)

    async def test_read_experience_only_counts_as_understanding(self):
        store = LifeStore(self.root, self.config, paths=self.paths)
        experience = store.record_experience(
            "test:coffee:read", "读完了手冲咖啡教程，记下了水温",
            platform="local", topics=["手冲咖啡"], observed_level="read")
        result = store.practice(
            "手冲咖啡", experience_id=experience["id"], mode="practice",
            outcome="success", artifact="不应该出现的作品")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mode"], "understanding", result)
        self.assertTrue(result.get("downgraded"), result)
        skill = store.growth_snapshot()["skills"][0]
        self.assertEqual(skill["practice_count"], 0)
        self.assertEqual(skill["understanding_count"], 1)
        self.assertFalse(store.growth_snapshot()["artifacts"])
        self.assertFalse(store.growth_snapshot()["mementos"])

    async def test_missing_components_degrade_without_fabricated_data(self):
        await self.serve(self.make_bot(life_store=False, autonomy=False))

        status, payload = await self.get_json("/api/life")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertIsNone(data["growth"])
        self.assertIsNone(data["autonomy"])
        self.assertEqual(set(data["errors"]), {"growth", "autonomy"})
        self.assertNotIn("skills", data)

    async def test_one_component_failure_does_not_hide_the_other(self):
        class BrokenStore:
            def growth_snapshot(self):
                raise RuntimeError("synthetic corrupt state")

        bot = self.make_bot(life_store=BrokenStore(), autonomy=True)
        await self.serve(bot)

        status, payload = await self.get_json("/api/life")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertIsNone(data["growth"])
        self.assertIsInstance(data["autonomy"], dict)
        self.assertIn("growth", data["errors"])
        self.assertNotIn("autonomy", data["errors"])


if __name__ == "__main__":
    unittest.main()
