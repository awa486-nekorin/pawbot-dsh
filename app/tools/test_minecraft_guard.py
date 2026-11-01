"""Guard client: pure attestation verification plus a real loopback HTTP guard.

The fake guard is a real ``http.server`` bound to 127.0.0.1:0 that signs raw
response bytes with HMAC-SHA256 exactly as docs/MINECRAFT-PROTOCOL.md says.
Nothing here touches a real game, world, config or secret.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawbot.minecraft_guard import (  # noqa: E402
    GuardAlreadyRevoked,
    GuardBusy,
    GuardClient,
    GuardDenied,
    GuardSettings,
    sign,
    verify_attestation,
)

UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SECRET = "x" * 40
LAN_PORT = 32123


class FakeGuard:
    """Real loopback HTTP server; ``mode`` selects the misbehaviour under test."""

    def __init__(self, world_path):
        self.world_path = world_path
        self.mode = "ok"
        self.changes = {}
        self.requests = []
        self.replay_cache = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner.requests.append((self.path, dict(self.headers), body))
                owner.handle(self, json.loads(body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1/permit"

    def row(self, req):
        now = time.time()
        row = {"nonce": req["nonce"], "world_id": "test-world",
               "save_path": self.world_path, "host": "127.0.0.1", "port": LAN_PORT,
               "actor_uuid": req["actor_uuid"], "action": req["action"],
               "target_uuid": req["target_uuid"], "issued_at": now,
               "expires_at": now + 5, "can_write": True, "can_pvp": True,
               "backup_id": "backup-1"}
        row.update(self.changes)
        return row

    def handle(self, h, req):
        mode = self.mode
        if mode == "redirect":
            h.send_response(302)
            h.send_header("Location", "http://127.0.0.1:1/elsewhere")
            h.end_headers()
            return
        if mode in ("deny403", "deny409", "error500", "revoked"):
            h.send_response({"deny403": 403, "deny409": 409, "error500": 500,
                             "revoked": 409}[mode])
            h.end_headers()
            h.wfile.write(b"revoked" if mode == "revoked"
                          else b'{"path":"C:/secret/world"}')
            return
        raw = json.dumps(self.row(req), separators=(",", ":")).encode()
        signature = sign(SECRET, raw)
        if mode == "badsig":
            signature = "0" * 64
        elif mode == "tamper":
            raw = raw.replace(b"backup-1", b"backup-2")
        elif mode == "oversize":
            raw = b" " * 16385
            signature = sign(SECRET, raw)
        elif mode == "nosig":
            signature = ""
        elif mode == "replay" and self.replay_cache is not None:
            raw, signature = self.replay_cache
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("X-PawBot-Signature", signature)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)
        if self.replay_cache is None:
            self.replay_cache = (raw, signature)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.world = os.path.realpath(os.path.join(self.temp.name, "isolated-test"))
        os.makedirs(self.world)
        self.guard = FakeGuard(self.world)
        self.addCleanup(self.guard.close)
        self.addCleanup(self.temp.cleanup)
        self.settings = GuardSettings(self.guard.url, SECRET, "test-world",
                                      self.world, UUID, LAN_PORT)
        self.client = GuardClient(self.settings)


class LoopbackGuardTests(GuardBase):
    def test_signed_fresh_response_is_accepted_for_every_action(self):
        for action in ("connect", "observe", "write", "backup", "revoke"):
            with self.subTest(action=action):
                row = self.client.request_blocking(action)
                self.assertEqual(row["action"], action)
                self.assertEqual(row["port"], LAN_PORT)
        self.assertEqual(self.client.request_blocking("pvp", OTHER)["target_uuid"], OTHER)

    def test_request_is_authenticated_bounded_and_bound_to_actor(self):
        self.client.request_blocking("observe")
        path, headers, body = self.guard.requests[0]
        self.assertEqual(path, "/v1/permit")
        self.assertEqual(headers["Authorization"], "Bearer " + SECRET)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertLessEqual(len(body), 4096)
        sent = json.loads(body)
        self.assertEqual(set(sent), {"nonce", "actor_uuid", "action", "target_uuid"})
        self.assertRegex(sent["nonce"], r"^[0-9a-f]{32}$")
        self.assertEqual(sent["actor_uuid"], UUID)

    def test_nonce_is_fresh_for_each_request(self):
        for _ in range(5):
            self.client.request_blocking("observe")
        nonces = {json.loads(body)["nonce"] for _, _, body in self.guard.requests}
        self.assertEqual(len(nonces), 5)

    def test_uppercase_actor_config_still_matches_lowercase_attestation(self):
        client = GuardClient(GuardSettings(self.guard.url, SECRET, "test-world",
                                           self.world, UUID.upper(), LAN_PORT))
        self.assertEqual(client.request_blocking("observe")["actor_uuid"], UUID)

    def test_forged_tampered_missing_or_oversize_signature_denied(self):
        for mode in ("badsig", "tamper", "nosig", "oversize"):
            with self.subTest(mode=mode):
                self.guard.mode = mode
                with self.assertRaises(GuardDenied):
                    self.client.request_blocking("observe")

    def test_replayed_old_response_is_rejected_by_nonce_binding(self):
        self.client.request_blocking("observe")
        self.guard.mode = "replay"
        with self.assertRaises(GuardDenied) as ctx:
            self.client.request_blocking("observe")
        self.assertEqual(str(ctx.exception), "guard_context_mismatch")

    def test_context_mismatch_is_denied_even_with_valid_signature(self):
        cases = {
            "world": {"world_id": "original-world"},
            "path": {"save_path": os.path.dirname(self.world)},
            "port": {"port": 25565},
            "host": {"host": "0.0.0.0"},
            "actor": {"actor_uuid": OTHER},
            "port_type": {"port": str(LAN_PORT)},
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                self.guard.changes = changes
                with self.assertRaises(GuardDenied):
                    self.client.request_blocking("observe")

    def test_expired_future_and_unbounded_lifetimes_are_denied(self):
        now = time.time()
        cases = {
            "expired": {"issued_at": now - 20, "expires_at": now - 15},
            "future": {"issued_at": now + 30, "expires_at": now + 35},
            "too_long": {"issued_at": now, "expires_at": now + 60},
            "inverted": {"issued_at": now, "expires_at": now - 1},
            "nan": {"issued_at": float("nan")},
            "bool": {"issued_at": True},
            "string": {"expires_at": "9999999999"},
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                self.guard.changes = changes
                with self.assertRaises(GuardDenied):
                    self.client.request_blocking("observe")

    def test_mutations_need_backup_and_capability_booleans(self):
        for changes, action in (({"backup_id": ""}, "connect"), ({"backup_id": ""}, "write"),
                                ({"backup_id": ""}, "backup"), ({"can_write": False}, "write"),
                                ({"can_pvp": False}, "pvp"), ({"can_write": "true"}, "observe"),
                                ({"backup_id": "../../etc"}, "connect")):
            with self.subTest(changes=changes, action=action):
                self.guard.changes = changes
                with self.assertRaises(GuardDenied):
                    self.client.request_blocking(action, OTHER if action == "pvp" else "")

    def test_observe_does_not_require_a_backup_identifier(self):
        self.guard.changes = {"backup_id": ""}
        self.assertEqual(self.client.request_blocking("observe")["backup_id"], "")

    def test_revoke_receipt_allows_zero_or_changed_lan_port_only(self):
        # LAN closed: the guard signs port 0. LAN re-opened: it signs a new
        # port. Both are valid proofs for revoke, but never for an action.
        for port in (0, 45555):
            with self.subTest(port=port):
                self.guard.changes = {"port": port}
                self.assertEqual(self.client.request_blocking("revoke")["port"], port)
                with self.assertRaises(GuardDenied):
                    self.client.request_blocking("observe")

    def test_revoke_only_client_accepts_port_zero_and_stays_revoke_only(self):
        client = GuardClient(GuardSettings(
            self.guard.url, SECRET, "test-world", self.world, UUID, 0),
            revoke_only=True)
        self.guard.changes = {"port": 0, "backup_id": ""}
        self.assertEqual(client.request_blocking("revoke")["action"], "revoke")
        before = len(self.guard.requests)
        with self.assertRaises(GuardDenied):
            client.request_blocking("observe")
        self.assertEqual(len(self.guard.requests), before,
                         "a revoke-only client must never send another action")

    def test_authenticated_409_revoked_is_already_safe_for_revoke_only(self):
        self.guard.mode = "revoked"
        with self.assertRaises(GuardAlreadyRevoked):
            self.client.request_blocking("revoke")
        with self.assertRaises(GuardDenied) as ctx:
            self.client.request_blocking("observe")
        self.assertNotIsInstance(ctx.exception, GuardAlreadyRevoked)

    def test_redirect_is_never_followed(self):
        self.guard.mode = "redirect"
        with self.assertRaises(GuardDenied) as ctx:
            self.client.request_blocking("observe")
        self.assertEqual(str(ctx.exception), "guard_redirect_denied")

    def test_http_denials_use_static_errors_without_leaking_body(self):
        expected = {"deny403": "guard_denied", "deny409": "guard_denied",
                    "error500": "guard_unavailable"}
        for mode, message in expected.items():
            with self.subTest(mode=mode):
                self.guard.mode = mode
                with self.assertRaises(GuardDenied) as ctx:
                    self.client.request_blocking("observe")
                self.assertEqual(str(ctx.exception), message)
                self.assertNotIn("secret", str(ctx.exception))

    def test_unreachable_guard_is_a_static_denial(self):
        self.guard.close()
        with self.assertRaises(GuardDenied) as ctx:
            self.client.request_blocking("observe")
        self.assertEqual(str(ctx.exception), "guard_unavailable")

    def test_invalid_actions_and_targets_never_reach_the_network(self):
        before = len(self.guard.requests)
        for action, target in (("shell", ""), ("pvp", ""), ("pvp", "*"), ("observe", OTHER),
                               ("write", OTHER)):
            with self.subTest(action=action, target=target):
                with self.assertRaises(GuardDenied):
                    self.client.request_blocking(action, target)
        self.assertEqual(len(self.guard.requests), before)

    def test_async_request_matches_blocking_behaviour(self):
        row = asyncio.run(self.client.request("observe"))
        self.assertEqual(row["world_id"], "test-world")
        self.guard.mode = "badsig"
        with self.assertRaises(GuardDenied):
            asyncio.run(self.client.request("observe"))

    def test_signed_busy_receipt_over_loopback_pauses_mutations(self):
        self.guard.changes = {"busy": True, "can_write": False, "can_pvp": False,
                              "backup_id": "", "retry_after_ms": 250,
                              "attested_at": int(time.time())}
        for action in ("connect", "write", "backup"):
            with self.subTest(action=action), self.assertRaises(GuardBusy) as ctx:
                self.client.request_blocking(action)
            self.assertEqual(ctx.exception.retry_after_ms, 250)
        with self.assertRaises(GuardBusy):
            self.client.request_blocking("pvp", OTHER)
        self.assertTrue(self.client.request_blocking("observe")["busy"])


class AttestationUnitTests(GuardBase):
    def verify(self, row, *, secret=SECRET, nonce="f" * 32, action="write", target="", now=None):
        raw = json.dumps(row, separators=(",", ":")).encode()
        return verify_attestation(self.settings, raw, sign(secret, raw), nonce, action,
                                  target, now=now)

    def base(self, **changes):
        row = {"nonce": "f" * 32, "world_id": "test-world", "save_path": self.world,
               "host": "127.0.0.1", "port": LAN_PORT, "actor_uuid": UUID,
               "action": "write", "target_uuid": "", "issued_at": 100.0,
               "expires_at": 105.0, "can_write": True, "can_pvp": False,
               "backup_id": "backup-1"}
        row.update(changes)
        return row

    def test_accepts_exact_context(self):
        self.assertEqual(self.verify(self.base(), now=102)["backup_id"], "backup-1")

    def test_signature_is_checked_before_any_field_is_read(self):
        # Garbage bytes with a wrong signature must fail on the signature,
        # not on JSON parsing: no field of an unauthenticated body is trusted.
        with self.assertRaises(GuardDenied) as ctx:
            verify_attestation(self.settings, b"not json", "0" * 64, "f" * 32, "write", now=102)
        self.assertEqual(str(ctx.exception), "invalid_guard_signature")

    def test_signed_non_object_and_empty_bodies_are_denied(self):
        for raw in (b"[]", b"null", b"\xff\xfe", b""):
            with self.subTest(raw=raw), self.assertRaises(GuardDenied):
                verify_attestation(self.settings, raw, sign(SECRET, raw), "f" * 32,
                                   "write", now=102)

    def test_wrong_secret_fails(self):
        with self.assertRaises(GuardDenied):
            self.verify(self.base(), secret="y" * 40, now=102)

    def test_time_window_boundaries(self):
        self.verify(self.base(), now=104.9)
        for now in (105.0, 99.0 - 1, 90.0):
            with self.subTest(now=now), self.assertRaises(GuardDenied):
                self.verify(self.base(), now=now)

    def test_path_comparison_is_exact_not_via_realpath_of_reported_value(self):
        link_like = self.world + os.sep + ".."
        with self.assertRaises(GuardDenied):
            self.verify(self.base(save_path=link_like), now=102)
        with self.assertRaises(GuardDenied):
            self.verify(self.base(save_path="relative/path"), now=102)
        with self.assertRaises(GuardDenied):
            self.verify(self.base(save_path=self.world + "\x00"), now=102)

    def test_target_must_echo_exactly(self):
        with self.assertRaises(GuardDenied):
            self.verify(self.base(action="pvp", target_uuid=OTHER, can_pvp=True),
                        action="pvp", target=UUID, now=102)
        row = self.verify(self.base(action="pvp", target_uuid=OTHER, can_pvp=True),
                          action="pvp", target=OTHER, now=102)
        self.assertEqual(row["target_uuid"], OTHER)

    def test_signature_must_be_lowercase_hex_of_exact_length(self):
        raw = json.dumps(self.base(), separators=(",", ":")).encode()
        valid = sign(SECRET, raw)
        for signature in (valid.upper(), "0" * 63, "0" * 65, "g" * 64,
                          valid + "0", " " + valid):
            with self.subTest(signature=signature[:12]), self.assertRaises(GuardDenied) as ctx:
                verify_attestation(self.settings, raw, signature, "f" * 32,
                                   "write", now=102)
            self.assertEqual(str(ctx.exception), "invalid_guard_signature")

    def test_reported_path_is_exact_canonical_not_case_folded(self):
        # Previously normcase() would accept a differently spelled path; the
        # guard reports one exact canonical spelling, so aliases must fail.
        with self.assertRaises(GuardDenied) as ctx:
            self.verify(self.base(save_path=self.world.upper()), now=102)
        self.assertEqual(str(ctx.exception), "guard_world_mismatch")
        with self.assertRaises(GuardDenied):
            self.verify(self.base(save_path=self.world + os.sep), now=102)

    def test_revoke_receipt_allows_zero_or_current_changed_port_only(self):
        for port in (0, 45555):
            with self.subTest(port=port):
                row = self.verify(self.base(action="revoke", port=port, backup_id=""),
                                  action="revoke", now=102)
                self.assertEqual(row["port"], port)
        # A non-revoke action still requires the exact configured LAN port.
        for port in (0, 45555):
            with self.subTest(port=port), self.assertRaises(GuardDenied):
                self.verify(self.base(port=port), now=102)
        with self.assertRaises(GuardDenied):
            self.verify(self.base(action="revoke", port="0", backup_id=""),
                        action="revoke", now=102)

    def test_backup_identifier_shape_is_bounded(self):
        for backup in ("backup:1", "backup/1", "backup 1", "x" * 257,
                       "backup\n1"):
            with self.subTest(backup=backup[:12]), self.assertRaises(GuardDenied) as ctx:
                self.verify(self.base(backup_id=backup), now=102)
            self.assertEqual(str(ctx.exception), "invalid_guard_backup")

    def test_actor_and_target_must_be_strings(self):
        with self.assertRaises(GuardDenied):
            self.verify(self.base(actor_uuid=123), now=102)
        with self.assertRaises(GuardDenied):
            self.verify(self.base(target_uuid=456), now=102)

    # ---- busy receipt (F3): signed, exact shape, structurally non-mutating ----

    def busy(self, **changes):
        row = self.base(can_write=False, can_pvp=False, backup_id="", busy=True,
                        retry_after_ms=250, attested_at=101)
        row.update(changes)
        return row

    def test_busy_receipt_is_a_nonmutating_observe_only(self):
        for action in ("connect", "write", "backup"):
            with self.subTest(action=action), self.assertRaises(GuardBusy) as ctx:
                self.verify(self.busy(action=action), action=action, now=102)
            self.assertEqual(ctx.exception.retry_after_ms, 250)
        with self.assertRaises(GuardBusy):
            self.verify(self.busy(action="pvp", target_uuid=OTHER),
                        action="pvp", target=OTHER, now=102)
        row = self.verify(self.busy(action="observe"), action="observe", now=102)
        self.assertIs(row["busy"], True)
        self.assertEqual(row["backup_id"], "")

    def test_forged_or_malformed_busy_receipts_are_denied(self):
        for changes in ({"can_write": True}, {"can_pvp": True}, {"backup_id": "backup-1"},
                        {"retry_after_ms": 0}, {"retry_after_ms": 6000},
                        {"retry_after_ms": 250.0}, {"attested_at": 0},
                        {"attested_at": 999}, {"busy": "true"}, {"secret": "leak"}):
            with self.subTest(changes=changes), self.assertRaises(GuardDenied) as ctx:
                self.verify(self.busy(**changes), action="observe", now=102)
            self.assertNotIsInstance(ctx.exception, GuardBusy)
        with self.assertRaises(GuardDenied) as ctx:
            self.verify(self.busy(), secret="y" * 40, action="observe", now=102)
        self.assertEqual(str(ctx.exception), "invalid_guard_signature")

    def test_busy_flag_absent_keeps_every_old_fixture_valid(self):
        row = self.base()
        self.assertNotIn("busy", row)
        self.assertEqual(self.verify(row, now=102)["backup_id"], "backup-1")


class SettingsValidationTests(GuardBase):
    def make(self, **changes):
        values = dict(url=self.guard.url, secret=SECRET, world_id="test-world",
                      world_path=self.world, actor_uuid=UUID, port=LAN_PORT)
        values.update(changes)
        return GuardSettings(**values)

    def test_valid_settings_pass(self):
        self.make().validate()

    def test_bad_urls_and_ports_raise_guard_denied_never_valueerror(self):
        for url in ("http://example.com:6198/v1/permit", "https://127.0.0.1:6198/v1/permit",
                    "http://user:pw@127.0.0.1:6198/v1/permit", "http://127.0.0.1/v1/permit",
                    "http://127.0.0.1:abc/v1/permit", "http://127.0.0.1:99999/v1/permit",
                    "http://127.0.0.1:6198/other", "http://127.0.0.1:6198/v1/permit?x=1",
                    "http://127.0.0.1:6198/v1/permit#f", "http://localhost:6198/v1/permit",
                    "http://127.0.0.1:6198", "", "not a url", "http://[::1]:6198/v1/permit"):
            with self.subTest(url=url), self.assertRaises(GuardDenied):
                self.make(url=url).validate()

    def test_bad_secret_world_actor_and_lan_port_are_denied(self):
        for changes in ({"secret": "short"}, {"secret": "x" * 31}, {"secret": "x" * 39 + "\n"},
                        {"secret": "é" * 40}, {"secret": None}, {"world_id": ""},
                        {"world_id": "a" * 65}, {"world_id": "bad id"},
                        {"world_id": "../x"}, {"world_path": "relative"},
                        {"world_path": ""}, {"actor_uuid": "not-a-uuid"},
                        {"actor_uuid": ""}, {"port": 0}, {"port": 65536},
                        {"port": True}, {"port": "32123"}):
            with self.subTest(changes=changes), self.assertRaises(GuardDenied):
                self.make(**changes).validate()

    def test_validation_bounds_match_the_java_guard(self):
        # Java: world_id 1-64, secret 32-256, PvP list <= 16.
        self.make(secret="x" * 256, world_id="a" * 64).validate()
        for changes in ({"secret": "x" * 257}, {"world_id": "a" * 65}):
            with self.subTest(changes=changes), self.assertRaises(GuardDenied):
                self.make(**changes).validate()

    def test_revoke_relaxes_only_the_lan_port_requirement(self):
        self.make(port=0).validate(action="revoke")
        with self.assertRaises(GuardDenied):
            self.make(port=0).validate()
        with self.assertRaises(GuardDenied):
            self.make(port=0, world_path=self.world.upper()).validate(action="revoke")

    def test_non_canonical_world_path_is_rejected(self):
        with self.assertRaises(GuardDenied):
            self.make(world_path=self.world + os.sep + "..").validate()
        # Exact canonical Windows spelling: normcase() must not accept a
        # differently cased spelling that the guard's exact receipt rejects.
        with self.assertRaises(GuardDenied):
            self.make(world_path=self.world.upper()).validate()
        self.make(world_path=self.world).validate()

    def test_client_construction_validates_immediately(self):
        with self.assertRaises(GuardDenied):
            GuardClient(self.make(port=0))

    def test_client_accepts_origin_or_endpoint_and_always_posts_to_permit(self):
        origin = f"http://127.0.0.1:{self.guard.port}"
        for url in (origin, origin + "/", origin + "/v1/permit"):
            with self.subTest(url=url):
                client = GuardClient(self.make(url=url))
                self.assertEqual(client.settings.url, origin + "/v1/permit")
                self.assertEqual(client.request_blocking("observe")["world_id"], "test-world")
        # Strict Settings validation is unchanged; only GuardClient normalizes.
        with self.assertRaises(GuardDenied):
            self.make(url=origin).validate()
        for bad in ("http://localhost:6198", "https://127.0.0.1:6198",
                    "http://127.0.0.1:6198/other", "http://127.0.0.1:6198/x?y=1"):
            with self.subTest(bad=bad), self.assertRaises(GuardDenied):
                GuardClient(self.make(url=bad))


if __name__ == "__main__":
    unittest.main()
