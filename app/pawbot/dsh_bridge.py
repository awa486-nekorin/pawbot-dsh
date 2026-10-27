"""Narrow client for the optional Host-to-PawBot read-only tool bridge."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

URL_ENV = "PAWBOT_DSH_BRIDGE_URL"
TOKEN_ENV = "PAWBOT_DSH_BRIDGE_TOKEN"
PATH = "/pawbot-dsh-bridge/execute"
MAX_RESPONSE_BYTES = 1024
TIMEOUT_SECONDS = 2
_TOKEN_RE = re.compile(r"pbdsh_[a-f0-9]{64}\Z")
_NUMERIC_FIELDS = ("queue_length", "inflight_watches", "watched_today", "daily_limit")
_BOOLEAN_FIELDS = ("auto_stock", "has_credential")


def _endpoint(raw: str) -> tuple[str, int, bool] | None:
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password
                or parsed.path != PATH or parsed.query or parsed.fragment):
            return None
        host = parsed.hostname
        address = ipaddress.ip_address(host) if host else None
        if address is None:
            return None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_loopback:
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        return host, port, parsed.scheme == "https"
    except (ValueError, TypeError):
        return None


def is_configured(environ=None) -> bool:
    env = os.environ if environ is None else environ
    token = env.get(TOKEN_ENV, "")
    return bool(_TOKEN_RE.fullmatch(token) and _endpoint(env.get(URL_ENV, "")))


def _project_status(value) -> dict | None:
    if not isinstance(value, dict) or value.get("ok") is not True:
        return None
    out = {"ok": True}
    for key in _NUMERIC_FIELDS:
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if item < 0 or item != item or item in (float("inf"), float("-inf")):
                return None
            out[key] = min(1_000_000, int(item))
    for key in _BOOLEAN_FIELDS:
        item = value.get(key)
        if isinstance(item, bool):
            out[key] = item
    if "credential_source" in value:
        source = value["credential_source"]
        if source is None or source in ("env", "file", "config"):
            out["credential_source"] = source
    return out


def bili_watch_status(environ=None) -> dict:
    """Fetch and project the one allowed Host status result; never follows redirects."""
    env = os.environ if environ is None else environ
    endpoint = _endpoint(env.get(URL_ENV, ""))
    token = env.get(TOKEN_ENV, "")
    if not endpoint or not _TOKEN_RE.fullmatch(token):
        return {"ok": False, "error": "DSH bridge is not configured"}

    host, port, tls = endpoint
    connection = None
    try:
        cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
        connection = cls(host, port, timeout=TIMEOUT_SECONDS)
        body = json.dumps({"name": "bili_watch_status", "arguments": {}},
                          separators=(",", ":")).encode("utf-8")
        connection.request("POST", PATH, body=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Connection": "close",
        })
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "")
        if response.status != 200:
            return {"ok": False, "error": f"DSH bridge returned HTTP {response.status}"}
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return {"ok": False, "error": "DSH bridge returned an invalid content type"}
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return {"ok": False, "error": "DSH bridge response exceeded its limit"}
        payload = json.loads(raw.decode("utf-8"))
        if (not isinstance(payload, dict) or payload.get("ok") is not True
                or payload.get("name") != "bili_watch_status"):
            return {"ok": False, "error": "DSH bridge returned an invalid response"}
        status = _project_status(payload.get("value"))
        if status is None:
            return {"ok": False, "error": "DSH bridge returned an invalid status"}
        return status
    except Exception:
        return {"ok": False, "error": "DSH bridge request failed"}
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
