"""Reviewer-only reproduction for the no-resident emergency-stop false success.

Run from the repository root:

    py -3.13 reports\\minecraft-review\\probe\\emergency_stop_false_success.py

The probe uses only a synthetic in-memory config; it never reads runtime
config/persona/memory and never opens a guard socket.  The secret is a
throwaway 40-character test value.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))

from pawbot.core import Bot  # noqa: E402


class Cfg:
    def __init__(self, values):
        self.values = values

    def get(self, dotted, default=None):
        node = self.values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def bind(bot, *names):
    for name in names:
        raw = inspect.getattr_static(Bot, name)
        if isinstance(raw, staticmethod):
            setattr(bot, name, raw.__func__)
        else:
            setattr(bot, name, types.MethodType(getattr(Bot, name), bot))
    return bot


minecraft = {
    "enabled": True,
    # Default/stale LAN port: the operator configured a guard secret, but the
    # local GuardSettings is invalid because port must be 1..65535.
    "port": 0,
    "owner_qq_uid": "123456789",
    "owner_player_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "test_world_id": "test-world",
    "world_path": "C:\\synthetic\\world",
    "guard_required": True,
}
bot = SimpleNamespace(
    config=Cfg({"minecraft": minecraft}),
    minecraft_resident=None,
    _minecraft_estop=False,
    _minecraft_lock=asyncio.Lock(),
)
bind(
    bot,
    "minecraft_emergency_stop",
    "_minecraft_stop_session",
    "_minecraft_guard_revoke",
    "_minecraft_guard_settings",
    "_minecraft_lifecycle_lock",
)

os.environ["PAWBOT_MC_GUARD_SECRET"] = "x" * 40
os.environ["PAWBOT_MC_GUARD_URL"] = "http://127.0.0.1:6198/v1/permit"
print(asyncio.run(bot.minecraft_emergency_stop(source="review-probe")))
