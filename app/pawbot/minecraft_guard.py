"""Authenticated loopback guard client. No trust is derived from game packets."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .minecraft_policy import WorldPermit, normalize_player_uuid

ACTIONS = frozenset({"connect", "observe", "write", "pvp", "backup", "revoke"})
# The Java guard is the strictest layer: world id 1-64, secret 32-256 chars.
_WORLD_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Same canonical identifier shape the Node sidecar accepts (guard emits
# ``backup-<13 digits>-<3 digits>``); cols/separators are never file paths.
_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
# A busy receipt is exactly the normal 13 fields plus busy/retry_after_ms/attested_at.
_BUSY_KEYS = frozenset({
    "nonce", "world_id", "save_path", "host", "port", "actor_uuid", "action",
    "target_uuid", "issued_at", "expires_at", "can_write", "can_pvp", "backup_id",
    "busy", "retry_after_ms", "attested_at",
})
MAX_RESPONSE = 16384


class GuardDenied(RuntimeError):
    """Static error only; never retain raw HTTP errors, paths or credentials."""


class GuardBusy(GuardDenied):
    """Signed busy receipt: the guard is alive but a snapshot owns its server thread.

    This is a *pause*, not a denial: mutating actions must stop and retry later, never
    replay the interrupted action and never treat the guard as gone.
    """

    def __init__(self, retry_after_ms: int = 250):
        super().__init__("guard_busy")
        self.retry_after_ms = int(retry_after_ms)


class GuardAlreadyRevoked(GuardDenied):
    """The authenticated guard answered ``409 revoked``: already latched, safe."""


@dataclass(frozen=True)
class GuardSettings:
    url: str
    secret: str
    world_id: str
    world_path: str
    actor_uuid: str
    port: int

    def validate(self, *, action: str | None = None):
        """Reject anything but the exact loopback endpoint and canonical identity.

        ``revoke`` is the one action that must still work when LAN is closed or
        the operator's configured port went stale, so only it relaxes the LAN
        port requirement (0 or any current 1024-65535 port). Every other field,
        including the exact canonical world path and actor, stays mandatory.
        """
        try:
            parsed = urllib.parse.urlsplit(self.url)
            url_port = parsed.port
        except (ValueError, TypeError, AttributeError):
            raise GuardDenied("invalid_guard_settings") from None
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.path != "/v1/permit" or not url_port
                or parsed.netloc != f"127.0.0.1:{url_port}"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise GuardDenied("invalid_guard_settings")
        secret, world_id, world_path = self.secret, self.world_id, self.world_path
        if not (isinstance(secret, str) and isinstance(world_id, str)
                and isinstance(world_path, str)):
            raise GuardDenied("invalid_guard_settings")
        if action == "revoke":
            port_ok = type(self.port) is int and 0 <= self.port <= 65535
        else:
            port_ok = type(self.port) is int and 1 <= self.port <= 65535
        if (not 32 <= len(secret) <= 256 or not secret.isascii() or not secret.isprintable()
                or not _WORLD_ID_RE.fullmatch(world_id)
                or "\x00" in world_path or not os.path.isabs(world_path)
                or ".." in world_path.replace("\\", "/").split("/")
                or world_path != os.path.normpath(world_path)
                # Exact canonical spelling only. The guard signs toRealPath()
                # and the receipt is compared as an exact string, so accepting
                # a case/alias spelling here would only move the failure to
                # every later permit as guard_world_mismatch.
                or os.path.realpath(world_path) != world_path
                or not normalize_player_uuid(self.actor_uuid)
                or not port_ok):
            raise GuardDenied("invalid_guard_settings")


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise GuardDenied("invalid_guard_time")
    return float(value)


def sign(secret: str, raw: bytes) -> str:
    """Hex HMAC-SHA256 used for guard responses (also by test fixtures)."""
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def normalize_guard_endpoint(value: object) -> str:
    """Map the documented loopback origin/endpoint to the exact POST endpoint.

    Only ``http://127.0.0.1:<port>`` (optionally with ``/`` or ``/v1/permit``)
    is accepted; nothing here relaxes host, scheme, port or path checks.
    """
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        port = parsed.port
    except (ValueError, TypeError):
        raise GuardDenied("invalid_guard_settings") from None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not port or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or parsed.path not in ("", "/", "/v1/permit")
            or parsed.netloc != f"127.0.0.1:{port}"):
        raise GuardDenied("invalid_guard_settings")
    return f"http://127.0.0.1:{port}/v1/permit"


def verify_attestation(settings: GuardSettings, raw: bytes, signature: str,
                       nonce: str, action: str, target_uuid: str = "", *, now=None) -> dict:
    """Verify the signature over the raw bytes FIRST, then every context field."""
    settings.validate(action=action)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RESPONSE:
        raise GuardDenied("invalid_guard_response")
    if (not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature)
            or not hmac.compare_digest(sign(settings.secret, raw), signature)):
        raise GuardDenied("invalid_guard_signature")
    if action not in ACTIONS or not _NONCE_RE.fullmatch(str(nonce)):
        raise GuardDenied("invalid_guard_action")
    try:
        row = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise GuardDenied("invalid_guard_response") from None
    if not isinstance(row, dict):
        raise GuardDenied("invalid_guard_response")
    actor = normalize_player_uuid(settings.actor_uuid)
    pairs = {"nonce": nonce, "action": action, "world_id": settings.world_id,
             "host": "127.0.0.1"}
    if any(type(row.get(key)) is not type(value) or row.get(key) != value
           for key, value in pairs.items()):
        raise GuardDenied("guard_context_mismatch")
    # Action-specific port receipt rule. The guard signs the *current* LAN
    # port: 0 when LAN is closed, and a changed port after the operator
    # re-opens the world. A revoke is independent of LAN state and must remain
    # provable in both cases; all action permits stay byte-exact.
    reported_port = row.get("port")
    if type(reported_port) is not int:
        raise GuardDenied("guard_context_mismatch")
    if action == "revoke":
        if not (reported_port == 0 or 1024 <= reported_port <= 65535):
            raise GuardDenied("guard_context_mismatch")
    elif reported_port != settings.port:
        raise GuardDenied("guard_context_mismatch")
    for key, expected in (("actor_uuid", actor), ("target_uuid", target_uuid)):
        value = row.get(key)
        if not isinstance(value, str) or value.lower() != expected:
            raise GuardDenied("guard_context_mismatch")
    actual_path = row.get("save_path")
    # Exact canonical string equality: never realpath() the reported value, so
    # an alias/junction spelling of the same folder cannot pass as the
    # configured registered world. Both sides are already canonical.
    if (not isinstance(actual_path, str) or "\x00" in actual_path
            or not os.path.isabs(actual_path) or actual_path != settings.world_path):
        raise GuardDenied("guard_world_mismatch")
    current = _number(time.time() if now is None else now)
    issued, expires = _number(row.get("issued_at")), _number(row.get("expires_at"))
    if issued > current + 1 or issued < current - 5 or not issued < expires <= issued + 5 or current >= expires:
        raise GuardDenied("guard_expired")
    if type(row.get("can_write")) is not bool or type(row.get("can_pvp")) is not bool:
        raise GuardDenied("invalid_guard_capabilities")
    # Optional additive busy flag: absent means false, so every pre-busy fixture stays valid.
    busy = row.get("busy", False)
    if type(busy) is not bool:
        raise GuardDenied("invalid_guard_response")
    if busy:
        # Exact shape: an extra field would be an unauthenticated channel for private data, and a
        # busy receipt must never be capability-bearing.
        if set(row) != _BUSY_KEYS:
            raise GuardDenied("invalid_guard_response")
        if row["backup_id"] != "" or row["can_write"] is not False or row["can_pvp"] is not False:
            raise GuardDenied("invalid_guard_capabilities")
        retry_after = row.get("retry_after_ms")
        if type(retry_after) is not int or not 50 <= retry_after <= 5000:
            raise GuardDenied("invalid_guard_response")
        attested_at = row.get("attested_at")
        if type(attested_at) is not int or attested_at <= 0 or attested_at > issued + 1:
            raise GuardDenied("invalid_guard_response")
        # A busy observe is a real non-mutating observation; every mutation action is paused.
        if action == "observe":
            return row
        if action == "revoke":
            raise GuardDenied("invalid_guard_response")
        raise GuardBusy(retry_after)
    backup = row.get("backup_id")
    if not isinstance(backup, str) or (backup and not _BACKUP_ID_RE.fullmatch(backup)):
        raise GuardDenied("invalid_guard_backup")
    if action in {"connect", "write", "pvp", "backup"} and not backup:
        raise GuardDenied("guard_backup_required")
    if action == "write" and row["can_write"] is not True:
        raise GuardDenied("guard_write_denied")
    if action == "pvp" and row["can_pvp"] is not True:
        raise GuardDenied("guard_pvp_denied")
    return row


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise GuardDenied("guard_redirect_denied")


class GuardClient:
    def __init__(self, settings: GuardSettings, *, revoke_only: bool = False):
        endpoint = normalize_guard_endpoint(settings.url)
        if endpoint != settings.url:
            settings = dataclasses.replace(settings, url=endpoint)
        settings.validate(action="revoke" if revoke_only else None)
        self.settings = settings
        self.revoke_only = revoke_only

    def _request(self, action, target):
        if self.revoke_only and action != "revoke":
            raise GuardDenied("invalid_guard_action")
        nonce = secrets.token_hex(16)
        body = json.dumps({"nonce": nonce,
                           "actor_uuid": normalize_player_uuid(self.settings.actor_uuid),
                           "action": action, "target_uuid": target},
                          separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.settings.url, data=body, method="POST",
            headers={"Authorization": "Bearer " + self.settings.secret,
                     "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=60 if action == "backup" else 10) as response:
                if response.status != 200:
                    raise GuardDenied("guard_unavailable")
                raw = response.read(MAX_RESPONSE + 1)
                signature = response.headers.get("X-PawBot-Signature", "")
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                error_body = exc.read(64)
            except Exception:  # noqa: BLE001 - optional static error body
                error_body = b""
            finally:
                exc.close()
            # The guard authenticates before it checks the sticky revoke
            # marker, so a revoke against an already-latched guard is
            # ``409 revoked``. That is an authenticated "already safe" answer,
            # not a failure, and no signed receipt can exist afterwards.
            if action == "revoke" and code == 409 and error_body == b"revoked":
                raise GuardAlreadyRevoked("revoked") from None
            raise GuardDenied("guard_denied" if code in (403, 409) else "guard_unavailable") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException,
                TimeoutError, ValueError):
            raise GuardDenied("guard_unavailable") from None
        return verify_attestation(self.settings, raw, signature, nonce, action, target)

    @staticmethod
    def _target(action, target_uuid):
        if action not in ACTIONS:
            raise GuardDenied("invalid_guard_action")
        target = normalize_player_uuid(target_uuid) if target_uuid else ""
        if (action == "pvp" and not target) or (action != "pvp" and target_uuid):
            raise GuardDenied("invalid_guard_target")
        return target

    async def request(self, action, target_uuid=""):
        target = self._target(action, target_uuid)
        return await asyncio.to_thread(self._request, action, target)

    def request_blocking(self, action, target_uuid=""):
        """Synchronous variant for the stand-alone emergency-stop CLI."""
        return self._request(action, self._target(action, target_uuid))

    @staticmethod
    def policy_permit(row):
        return WorldPermit(row["world_id"], True, row["issued_at"],
                           row["can_write"], row["can_pvp"])
