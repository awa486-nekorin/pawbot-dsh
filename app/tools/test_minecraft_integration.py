"""Integration contracts between core, guard, policy and the sidecar supervisor.

Uses shell Bot objects (bound real methods) plus synthetic fakes: no resident
loop, game, world, runtime config, persona, memory, browser or network guard is
started. The goal is to prove the host-level invariants that the individual
unit suites cannot: HA relay never starts a resident, a sidecar failure never
escapes into the QQ main flow, emergency stop orders revoke → cancel/reap and
fails honestly when partial, and the guard permit type feeds the policy.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from pawbot.core import Bot
from pawbot.minecraft_guard import GuardClient, GuardDenied, GuardSettings
from pawbot.minecraft_policy import authorize_action
from pawbot.minecraft_sidecar import SidecarError

ACTOR = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TARGET = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WORLD = "test-world"


class Cfg:
    """Minimal dotted ``config.get`` like ``pawbot.config.Config``."""

    def __init__(self, values=None):
        self.values = dict(values or {})

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


def minecraft_config(**overrides):
    values = {
        "minecraft": {
            "enabled": True, "host": "127.0.0.1", "port": 32123,
            "version": "1.20.1", "connect_timeout_sec": 15,
            "username": "SyntheticBot", "auth_mode": "offline",
            "owner_qq_uid": "123456789", "owner_player_uuid": ACTOR,
            "test_world_id": WORLD, "world_path": "C:\\synthetic\\world",
            "guard_required": True, "permit_max_age_sec": 30,
            "write_enabled": True, "pvp_enabled": True,
            "pvp_target_uuids": [TARGET], "backup_interval_min": 30,
            "backup_retention": 7,
        }
    }
    values["minecraft"].update(overrides)
    return Cfg(values)


class FakeGuard:
    def __init__(self, order, failures=0):
        self.order = order
        self.failures = failures
        self.calls = []

    async def request(self, action, target=""):
        self.order.append("guard:" + action)
        self.calls.append(action)
        if self.failures:
            self.failures -= 1
            raise GuardDenied("guard_denied")
        return {"action": action}


class FakeSidecar:
    def __init__(self, order, reaped=True):
        self.order = order
        self.process = None if reaped else object()
        self.alive = not reaped
        self.stop_calls = []

    async def stop(self, *, graceful=True):
        self.order.append("sidecar:stop")
        self.stop_calls.append(graceful)
        self.process = None
        self.alive = False


class FakeResident:
    def __init__(self, order, *, guard=None, sidecar=None, stop_error=None):
        self.order = order
        self.guard = guard if guard is not None else FakeGuard(order)
        self.sidecar = sidecar if sidecar is not None else FakeSidecar(order)
        self.stop_error = stop_error

    async def estop(self):
        self.order.append("resident:estop")
        if self.stop_error is not None:
            raise self.stop_error


def emergency_bot(resident):
    bot = SimpleNamespace(config=minecraft_config(), minecraft_resident=resident,
                          _minecraft_estop=False,
                          _minecraft_lock=asyncio.Lock())
    return bind(bot, "minecraft_emergency_stop", "_minecraft_stop_session",
                "_minecraft_guard_revoke", "_minecraft_guard_settings",
                "_minecraft_lifecycle_lock", "_start_minecraft_resident")


def shell_bot(**attrs):
    values = {
        "config": minecraft_config(),
        "_minecraft_estop": False,
        "_minecraft_lock": asyncio.Lock(),
        "minecraft_resident": None,
        "_store_flush_task": None,
    }
    values.update(attrs)
    return SimpleNamespace(**values)


class RelayResidentTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_mode_never_starts_a_resident(self):
        bot = shell_bot(
            plugins=SimpleNamespace(reload_all=mock.Mock()),
            _ha_mode=True,
            _ha_stopping=False,
            store=SimpleNamespace(flush=mock.Mock()),
            ha_runtime=SimpleNamespace(start=mock.AsyncMock(),
                                       stop=mock.AsyncMock()),
            server=SimpleNamespace(start=mock.AsyncMock(),
                                   stop=mock.AsyncMock()),
            _store_flush_task=None,
            mood=SimpleNamespace(save=mock.Mock()),
            topic=SimpleNamespace(save=mock.Mock()),
        )
        bind(bot, "start", "_stop_relay", "_flush_store_loop",
             "_minecraft_lifecycle_lock", "_minecraft_stop_session",
             "_minecraft_guard_revoke", "_start_minecraft_resident")
        resident = mock.Mock(side_effect=AssertionError(
            "HA relay must never construct a Minecraft resident"))
        with mock.patch("pawbot.tts.ensure_server", new=mock.AsyncMock()), \
                mock.patch("pawbot.minecraft_resident.MinecraftResident", new=resident):
            await Bot.start(bot)
            self.assertIsNone(bot.minecraft_resident)
            resident.assert_not_called()
            self.assertTrue(bot.config.get("minecraft.enabled") is True,
                            "the config being enabled must not matter in relay mode")
            task = bot._store_flush_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                bot._store_flush_task = None
            await Bot._stop_relay(bot)
        bot.server.stop.assert_awaited()
        bot.ha_runtime.stop.assert_awaited()

    async def test_stop_relay_reaps_a_stray_resident_and_continues(self):
        order: list[str] = []
        resident = FakeResident(order, stop_error=RuntimeError("boom"))
        bot = shell_bot(
            _ha_mode=True, _ha_stopping=False, minecraft_resident=resident,
            store=SimpleNamespace(flush=mock.Mock()),
            ha_runtime=SimpleNamespace(stop=mock.AsyncMock()),
            server=SimpleNamespace(stop=mock.AsyncMock()),
            _store_flush_task=None,
            mood=SimpleNamespace(save=mock.Mock()),
            topic=SimpleNamespace(save=mock.Mock()),
        )
        bind(bot, "_stop_relay", "_minecraft_stop_session",
             "_minecraft_guard_revoke", "_minecraft_lifecycle_lock")
        await Bot._stop_relay(bot)   # must not raise despite the stop error
        self.assertIsNone(bot.minecraft_resident)
        self.assertTrue(bot._minecraft_estop)
        bot.server.stop.assert_awaited()
        bot.ha_runtime.stop.assert_awaited()
        bot.store.flush.assert_called_once_with(force=True)


class ResidentStartContainmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_sidecar_start_failure_is_contained(self):
        class BrokenResident:
            def __init__(self, bot):
                self.bot = bot

            async def start(self):
                raise SidecarError("sidecar_unavailable")

        bot = shell_bot()
        bind(bot, "_start_minecraft_resident", "_minecraft_lifecycle_lock")
        with mock.patch("pawbot.minecraft_resident.MinecraftResident", new=BrokenResident):
            started = await Bot._start_minecraft_resident(bot)
        self.assertFalse(started)
        self.assertIsNone(bot.minecraft_resident)

    async def test_latch_and_disabled_config_never_construct_a_resident(self):
        resident = mock.Mock()
        bot = shell_bot(_minecraft_estop=True)
        bind(bot, "_start_minecraft_resident", "_minecraft_lifecycle_lock")
        with mock.patch("pawbot.minecraft_resident.MinecraftResident", new=resident):
            self.assertFalse(await Bot._start_minecraft_resident(bot))
            resident.assert_not_called()

        disabled = shell_bot(config=minecraft_config(enabled=False))
        bind(disabled, "_start_minecraft_resident", "_minecraft_lifecycle_lock")
        with mock.patch("pawbot.minecraft_resident.MinecraftResident", new=resident):
            self.assertFalse(await Bot._start_minecraft_resident(disabled))
            resident.assert_not_called()


class EmergencyStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_revokes_before_cancel_and_reap(self):
        order: list[str] = []
        resident = FakeResident(order)
        bot = emergency_bot(resident)
        result = await bot.minecraft_emergency_stop(source="test")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["latched"] and result["revoked"])
        self.assertTrue(result["resident_stopped"] and result["sidecar_reaped"])
        self.assertTrue(result["writes_drained"])
        self.assertEqual(result["session"], "resident")
        self.assertLess(order.index("guard:revoke"), order.index("resident:estop"))
        self.assertIsNone(bot.minecraft_resident)
        self.assertTrue(bot._minecraft_estop)

    async def test_unconfirmed_revoke_fails_honestly_after_retry(self):
        order: list[str] = []
        guard = FakeGuard(order, failures=99)
        resident = FakeResident(order, guard=guard)
        bot = emergency_bot(resident)
        result = await bot.minecraft_emergency_stop(source="test")
        self.assertFalse(result["ok"])
        self.assertFalse(result["revoked"])
        self.assertTrue(result["resident_stopped"])
        self.assertTrue(result["sidecar_reaped"])
        self.assertEqual(result["revoke_error"], "GuardDenied")
        self.assertEqual(guard.calls, ["revoke", "revoke"],
                         "a failed revoke is retried once after the stop")

    async def test_partial_stop_is_reported_and_sidecar_is_reaped_defensively(self):
        order: list[str] = []
        resident = FakeResident(order, stop_error=RuntimeError("boom"))
        bot = emergency_bot(resident)
        result = await bot.minecraft_emergency_stop(source="test")
        self.assertFalse(result["ok"])
        self.assertFalse(result["resident_stopped"])
        self.assertEqual(result["stop_error"], "RuntimeError")
        self.assertIn("sidecar:stop", order, "host must still close the sidecar")
        self.assertTrue(result["sidecar_reaped"])

    async def test_unreaped_sidecar_cannot_be_success(self):
        order: list[str] = []
        guard = FakeGuard(order)
        sidecar = FakeSidecar(order, reaped=False)

        class StubbornResident:
            def __init__(self):
                self.guard = guard
                self.sidecar = sidecar

            async def estop(self):
                order.append("resident:estop")

        bot = emergency_bot(StubbornResident())
        result = await bot.minecraft_emergency_stop(source="test")
        self.assertTrue(result["revoked"])
        self.assertFalse(result["sidecar_reaped"])
        self.assertFalse(result["writes_drained"])
        self.assertFalse(result["ok"])

    async def test_no_resident_is_latched_and_revokes_best_effort(self):
        class FakeClient:
            instances = []

            def __init__(self, settings, *, revoke_only=False):
                self.settings = settings
                self.revoke_only = revoke_only
                self.calls = []
                FakeClient.instances.append(self)

            async def request(self, action, target=""):
                self.calls.append(action)
                return {}

        bot = emergency_bot(None)
        with mock.patch.dict(os.environ, {
                "PAWBOT_MC_GUARD_SECRET": "g" * 40,
                "PAWBOT_MC_GUARD_URL": "http://127.0.0.1:6198/v1/permit"}), \
                mock.patch("pawbot.minecraft_guard.GuardClient", new=FakeClient):
            result = await bot.minecraft_emergency_stop(source="test")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["session"], "none")
            self.assertTrue(result["revoked"])
            self.assertTrue(result["resident_stopped"])
            self.assertTrue(result["sidecar_reaped"])
            self.assertTrue(FakeClient.instances[0].revoke_only)
            self.assertEqual(FakeClient.instances[0].calls, ["revoke"])
            again = await bot.minecraft_emergency_stop(source="test")
            self.assertTrue(again["ok"])
            self.assertTrue(bot._minecraft_estop)

    async def test_no_resident_reports_revoke_failure_without_false_claim(self):
        class FailingClient:
            def __init__(self, settings, *, revoke_only=False):
                pass

            async def request(self, action, target=""):
                raise GuardDenied("guard_unavailable")

        bot = emergency_bot(None)
        with mock.patch.dict(os.environ, {
                "PAWBOT_MC_GUARD_SECRET": "g" * 40}), \
                mock.patch("pawbot.minecraft_guard.GuardClient", new=FailingClient):
            result = await bot.minecraft_emergency_stop(source="test")
        self.assertFalse(
            result["ok"],
            "an unreachable guard must not be reported as a confirmed stop")
        self.assertFalse(result["revoked"])
        self.assertEqual(result["revoke_error"], "GuardDenied")
        self.assertEqual(result["session"], "none")

    async def test_no_resident_and_no_guard_credential_is_an_honest_noop(self):
        bot = emergency_bot(None)
        with mock.patch.dict(os.environ, {"PAWBOT_MC_GUARD_SECRET": "",
                                          "PAWBOT_MC_GUARD_URL":
                                              "http://127.0.0.1:6198/v1/permit"}):
            result = await bot.minecraft_emergency_stop(source="test")
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["revoked"])
        self.assertTrue(result["noop"])
        self.assertFalse(result["guard_configured"])
        self.assertEqual(result["revoke_skipped"], "no_guard_configured")
        self.assertEqual(result["error"], "guard_not_configured")
        self.assertEqual(result["session"], "none")

    async def test_no_resident_configured_secret_with_bad_settings_is_not_skipped(self):
        cases = (
            {"port": 0, "owner_player_uuid": "not-a-uuid"},
            {"port": 32123, "world_path": "relative-world"},
            {"port": 32123, "test_world_id": "bad world id"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                bot = emergency_bot(None)
                bot.config = minecraft_config(**overrides)
                with mock.patch.dict(os.environ, {
                        "PAWBOT_MC_GUARD_SECRET": "g" * 40}):
                    result = await bot.minecraft_emergency_stop(source="test")
                self.assertFalse(result["ok"], result)
                self.assertFalse(result["revoked"], result)
                self.assertTrue(result["guard_configured"], result)
                self.assertEqual(result["error"], "invalid_guard_settings")
                self.assertNotIn("revoke_skipped", result)
                self.assertNotIn("noop", result)

    async def test_no_resident_stale_endpoint_is_a_confirmed_failure(self):
        bot = emergency_bot(None)
        with mock.patch.dict(os.environ, {
                "PAWBOT_MC_GUARD_SECRET": "g" * 40,
                "PAWBOT_MC_GUARD_URL": "http://127.0.0.1:1/v1/permit"}):
            result = await bot.minecraft_emergency_stop(source="test")
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["revoked"], result)
        self.assertTrue(result["guard_configured"], result)
        self.assertEqual(result["error"], "guard_unavailable")
        self.assertNotIn("revoke_skipped", result)

    async def test_no_resident_port_zero_without_a_reachable_guard_is_not_success(self):
        # The revoke path tolerates a closed/zero LAN port, but that must not
        # turn an unreachable guard into a false success (M2).
        bot = emergency_bot(None)
        bot.config = minecraft_config(port=0)
        with mock.patch.dict(os.environ, {
                "PAWBOT_MC_GUARD_SECRET": "g" * 40,
                "PAWBOT_MC_GUARD_URL": "http://127.0.0.1:1/v1/permit"}):
            result = await bot.minecraft_emergency_stop(source="test")
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["revoked"], result)
        self.assertEqual(result["error"], "guard_unavailable")
        self.assertNotIn("revoke_skipped", result)

    async def test_no_resident_guard_url_prefers_env_then_config(self):
        class CapturingClient:
            seen = []

            def __init__(self, settings, *, revoke_only=False):
                self.settings = settings
                self.revoke_only = revoke_only
                CapturingClient.seen.append(self)

            async def request(self, action, target=""):
                return {}

        bot = emergency_bot(None)
        bot.config = minecraft_config(
            guard_url="http://127.0.0.1:65500/v1/permit")
        with mock.patch.dict(os.environ, {
                "PAWBOT_MC_GUARD_SECRET": "g" * 40,
                "PAWBOT_MC_GUARD_URL": ""}), \
                mock.patch("pawbot.minecraft_guard.GuardClient", new=CapturingClient):
            result = await bot.minecraft_emergency_stop(source="test")
        self.assertTrue(result["ok"], result)
        self.assertEqual(CapturingClient.seen[0].settings.url,
                         "http://127.0.0.1:65500/v1/permit")

        CapturingClient.seen.clear()
        with mock.patch.dict(os.environ, {
                "PAWBOT_MC_GUARD_SECRET": "g" * 40,
                "PAWBOT_MC_GUARD_URL": "http://127.0.0.1:65501/v1/permit"}), \
                mock.patch("pawbot.minecraft_guard.GuardClient", new=CapturingClient):
            result = await bot.minecraft_emergency_stop(source="test")
        self.assertTrue(result["ok"], result)
        self.assertEqual(CapturingClient.seen[0].settings.url,
                         "http://127.0.0.1:65501/v1/permit")

    async def test_latch_blocks_restart_after_emergency_stop(self):
        resident = mock.Mock()
        bot = emergency_bot(FakeResident([]))
        await bot.minecraft_emergency_stop(source="test")
        with mock.patch("pawbot.minecraft_resident.MinecraftResident", new=resident):
            self.assertFalse(await bot._start_minecraft_resident())
            resident.assert_not_called()


class ShutdownContainmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_stop_is_exception_safe_and_latches(self):
        order: list[str] = []
        resident = FakeResident(order, stop_error=RuntimeError("boom"))
        bot = shell_bot(
            minecraft_resident=resident,
            _bili_scroll_task=None,
            _bili_post_task=None,
            reminder_service=SimpleNamespace(stop=mock.AsyncMock()),
            proactive=SimpleNamespace(stop=mock.AsyncMock()),
            _store_flush_task=None,
            mood=SimpleNamespace(save=mock.Mock()),
            topic=SimpleNamespace(save=mock.Mock()),
            server=SimpleNamespace(stop=mock.AsyncMock()),
            store=SimpleNamespace(flush=mock.Mock()),
            memes=SimpleNamespace(flush=mock.Mock()),
        )
        bind(bot, "_stop_legacy", "_minecraft_stop_session",
             "_minecraft_guard_revoke", "_minecraft_lifecycle_lock")
        await Bot._stop_legacy(bot)   # must not raise
        self.assertTrue(bot._minecraft_estop)
        self.assertIsNone(bot.minecraft_resident)
        self.assertIn("sidecar:stop", order)
        bot.reminder_service.stop.assert_awaited()
        bot.proactive.stop.assert_awaited()
        bot.server.stop.assert_awaited()
        bot.store.flush.assert_called_once_with(force=True)
        bot.memes.flush.assert_called_once_with()


class GuardPolicyInterfaceTests(unittest.TestCase):
    def test_guard_permit_feeds_policy_with_exact_capabilities(self):
        settings = GuardSettings(
            "http://127.0.0.1:6198/v1/permit", "s" * 40, WORLD,
            "C:\\synthetic\\world", ACTOR, 32123)
        row = {"world_id": WORLD, "issued_at": 100.0,
               "can_write": True, "can_pvp": True}
        permit = GuardClient.policy_permit(row)
        config = minecraft_config()
        self.assertTrue(authorize_action(
            config, permit, action="write", player_uuid=ACTOR, now=101.0))
        self.assertTrue(authorize_action(
            config, permit, action="pvp", target_uuid=TARGET,
            player_uuid=ACTOR, now=101.0))
        self.assertFalse(authorize_action(
            config, permit, action="pvp", target_uuid=TARGET,
            player_uuid=ACTOR, now=110.0), "attestation freshness is capped at 5s")
        self.assertTrue(settings.validate() is None)

    def test_string_capabilities_never_authorize(self):
        row = {"world_id": WORLD, "issued_at": 100.0,
               "can_write": "true", "can_pvp": "true"}
        permit = GuardClient.policy_permit(row)
        self.assertFalse(authorize_action(
            minecraft_config(), permit, action="write",
            player_uuid=ACTOR, now=101.0))


if __name__ == "__main__":
    unittest.main()
