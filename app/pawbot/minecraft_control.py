"""Host-side Minecraft emergency stop, independent of the resident and any model.

    python -m pawbot.minecraft_control stop [--webui-port N]

Primary path: POST the local control endpoint ``/api/control/minecraft-stop``
with ``PAWBOT_CONTROL_TOKEN`` (environment only; never argv, never logged).
That runs ``Bot.minecraft_emergency_stop()``: guard revoke, resident/sidecar
cancel + reap, and a latch that refuses any restart until PawBot restarts.

Fallback (control server unreachable or rejected): a direct, signed guard
``revoke`` using the ``PAWBOT_MC_*`` environment. That revokes the actor's
permits and disconnects it on the guard side, but this process cannot reach the
sidecar, so the report states ``sidecar_reaped: false`` and never claims
otherwise. When the control path stopped the resident but could not confirm the
guard revoke, a successful direct revoke is merged and the report says
``control+guard_direct``.

``ok`` is only true when every step is confirmed **and** the guard revoke is
durably proven. The guard signs a ``revoke`` receipt only after it persisted and
verified its revocation marker; a bare authenticated ``409 revoked`` proves only
the in-memory latch (the guard answers 409 before it writes the marker), so it
is reported as ``already_revoked`` with ``revoke_durable: false`` and never as a
successful stop. A partial stop prints the true per-step booleans, exits
non-zero, and tells the operator to verify manually. No config file is read;
nothing here widens any permission.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request

from .minecraft_guard import (
    GuardAlreadyRevoked,
    GuardClient,
    GuardDenied,
    GuardSettings,
)

CONTROL_TOKEN_ENV = "PAWBOT_CONTROL_TOKEN"
DEFAULT_WEBUI_PORT = 6185
STOP_PATH = "/api/control/minecraft-stop"
_REPORT_FIELDS = ("revoked", "revoke_durable", "resident_stopped",
                  "sidecar_reaped", "writes_drained", "latched")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _as_bool(value) -> bool:
    return value is True


def stop_via_control(port: int, token: str, *, timeout: float = 70.0) -> dict:
    """Authenticated call to the running PawBot. Raises OSError if unreachable."""
    if not token or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid control settings")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{STOP_PATH}", data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with _opener().open(request, timeout=timeout) as response:
            status, raw = response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(65536)
        except Exception:  # noqa: BLE001 - body is untrusted and optional
            raw = b""
        finally:
            exc.close()
    payload = {}
    try:
        parsed = json.loads(raw) if raw else {}
        if isinstance(parsed, dict):
            payload = parsed
    except (ValueError, UnicodeError):
        payload = {}
    result = {"path": "control", "http_status": status,
              "session": str(payload.get("session") or "")[:16]}
    for field in _REPORT_FIELDS:
        result[field] = _as_bool(payload.get(field))
    result["revoke_receipt"] = str(payload.get("revoke_receipt") or "")[:32]
    # A signed revoke receipt is the only durable proof; a bare
    # ``revoke_receipt="already_revoked"`` (HTTP 409) proves the in-memory
    # latch only. An explicit ``revoke_durable`` flag always wins; without it,
    # only a 409 receipt is refused (legacy payload compatibility).
    if "revoke_durable" in payload:
        durable = result["revoke_durable"]
    else:
        durable = (result["revoked"]
                   and result["revoke_receipt"] != "already_revoked")
    stopped = (status == 200 and _as_bool(payload.get("ok"))
               and result["latched"] and result["resident_stopped"]
               and result["sidecar_reaped"] and result["writes_drained"]
               and result["revoked"] and durable)
    result["ok"] = bool(stopped)
    if not result["ok"]:
        result["error"] = ("control_invalid_response" if not payload
                           else "control_incomplete")
    return result


def settings_from_env(env=None) -> GuardSettings:
    env = os.environ if env is None else env
    try:
        port = int(env.get("PAWBOT_MC_LAN_PORT", "0") or 0)
    except (TypeError, ValueError):
        port = 0
    return GuardSettings(
        env.get("PAWBOT_MC_GUARD_URL", "http://127.0.0.1:6198/v1/permit"),
        env.get("PAWBOT_MC_GUARD_SECRET", ""), env.get("PAWBOT_MC_WORLD_ID", ""),
        env.get("PAWBOT_MC_WORLD_PATH", ""), env.get("PAWBOT_MC_ACTOR_UUID", ""),
        port)


def revoke_direct(env=None) -> dict:
    """Signed guard revoke only. Evidence = a verified *durable* guard receipt.

    The guard signs a ``revoke`` receipt only after it persisted and verified its
    revocation marker, so a signed receipt is durable proof. An authenticated
    ``409 revoked`` proves only the in-memory latch (the guard answers 409 before
    writing the marker); it is reported as ``already_revoked`` and never as a
    confirmed or durable revoke. An empty ``PAWBOT_MC_GUARD_SECRET`` means this
    process holds no guard credential at all; that is reported as a distinct
    honest noop, never as a successful revoke. Every other invalid setting is a
    hard failure.
    """
    settings = settings_from_env(env)
    if not settings.secret:
        return {"path": "guard_direct", "ok": False, "revoked": False,
                "revoke_durable": False,
                "resident_stopped": False, "sidecar_reaped": False,
                "writes_drained": False, "latched": False,
                "guard_configured": False, "noop": True,
                "revoke_skipped": "no_guard_configured",
                "error": "guard_not_configured"}
    try:
        GuardClient(settings, revoke_only=True).request_blocking("revoke")
    except GuardAlreadyRevoked:
        # Authenticated 409: the guard is already latched in memory, but it
        # answers 409 *before* it persists the marker. That is "no new permits"
        # (already safe) without proof of durability, so it is never reported as
        # a confirmed revoke or a durable success.
        return {"path": "guard_direct", "ok": False, "revoked": False,
                "already_revoked": True, "revoke_receipt": "already_revoked",
                "revoke_durable": False,
                "actor_disconnected_by_guard": False,
                "resident_stopped": False, "sidecar_reaped": False,
                "writes_drained": False, "latched": False,
                "error": "revoke_not_durable",
                "note": "guard answered 409 revoked (in-memory latch only, no "
                        "durable receipt) - verify config/pawbot-guard.revoked "
                        "and restart PawBot"}
    except GuardDenied as exc:
        return {"path": "guard_direct", "ok": False, "revoked": False,
                "revoke_durable": False,
                "resident_stopped": False, "sidecar_reaped": False,
                "writes_drained": False, "latched": False,
                "guard_configured": True,
                "error": str(exc)}
    return {"path": "guard_direct", "ok": False, "revoked": True,
            "revoke_durable": True, "revoke_receipt": "signed",
            "actor_disconnected_by_guard": True, "resident_stopped": False,
            "sidecar_reaped": False, "writes_drained": False, "latched": False,
            "error": "resident_not_reaped",
            "note": "guard revoked; sidecar process not reached - restart PawBot"}


def run_stop(webui_port: int, env=None) -> dict:
    env = os.environ if env is None else env
    token = str(env.get(CONTROL_TOKEN_ENV, "") or "").strip()
    control_error = "no_control_token"
    control = None
    if token:
        try:
            control = stop_via_control(webui_port, token)
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
            control_error = "control_unreachable"
        else:
            if control["ok"]:
                return control
            control_error = ("control_rejected"
                             if control["http_status"] in (401, 403)
                             else str(control.get("error") or "control_incomplete"))
    fallback = revoke_direct(env)
    fallback["control_error"] = control_error
    if control is not None:
        fallback["control"] = {key: control.get(key) for key in
                               ("http_status", "session", *_REPORT_FIELDS)}
        stop_confirmed = bool(control["resident_stopped"]
                              and control["sidecar_reaped"]
                              and control["writes_drained"]
                              and control["latched"])
        if stop_confirmed and fallback["revoked"] and fallback.get("revoke_durable"):
            merged = {"path": "control+guard_direct", "ok": True,
                      "revoked": True, "revoke_durable": True,
                      "revoke_receipt": "signed",
                      "resident_stopped": True,
                      "sidecar_reaped": True, "writes_drained": True,
                      "latched": True, "session": control["session"],
                      "control_error": control_error}
            return merged
    fallback["ok"] = False
    return fallback


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pawbot.minecraft_control")
    sub = parser.add_subparsers(dest="command", required=True)
    stop = sub.add_parser("stop", help="revoke guard permits and stop the resident")
    stop.add_argument("--webui-port", type=int, default=None)
    args = parser.parse_args(argv)
    port = args.webui_port
    if port is None:
        try:
            port = int(os.environ.get("PAWBOT_WEBUI_PORT", DEFAULT_WEBUI_PORT))
        except (TypeError, ValueError):
            port = DEFAULT_WEBUI_PORT
    if type(port) is not int or not 1 <= port <= 65535:
        print(json.dumps({"ok": False, "error": "invalid_webui_port",
                          "latched": False}, ensure_ascii=False, sort_keys=True))
        return 1
    try:
        result = run_stop(port)
    except Exception as exc:  # noqa: BLE001 - 静态错误，不打印堆栈/环境
        print(json.dumps({"ok": False, "error": type(exc).__name__,
                          "latched": False}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
