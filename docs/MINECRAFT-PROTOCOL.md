# Minecraft resident transport contract v1

Status: **frozen contract (v1), implementation snapshot 2026-11-01**. Scope of this file is the
interface between the three components, not a claim that any live Minecraft run happened. Evidence
for what has actually been executed lives in [MINECRAFT-ACCEPTANCE.md](MINECRAFT-ACCEPTANCE.md);
owner-facing setup lives in [MINECRAFT-RESIDENT.md](MINECRAFT-RESIDENT.md).

Source-only working rule: no developer/test may connect to a real game, read existing world saves,
runtime personas, private memories, or credentials. Tests use temporary synthetic data only.

Who is authoritative for what:

| Fact | Authority |
| --- | --- |
| which integrated-server save is open, its canonical path, its LAN port | the Fabric guard, over loopback (never a game packet) |
| whether a write/PvP action is allowed | the guard permit, re-requested before every side effect |
| what skills exist and their argument schemas | the Node sidecar (`app/minecraft-sidecar/src/skills.js`) |
| whether the resident may run at all | Python host config + this contract |
| snapshot freshness window | the guard (`backup_interval_sec`, 60–1800 s) |

Implementation contract for the sidecar is [app/minecraft-sidecar/README.md](../app/minecraft-sidecar/README.md)
§Trusted environment, §JSON-RPC and §Skill arguments. Where that README and this file disagree, the
README describes what the code actually does and this file must be corrected in the same change
(see §5).

## 1. Local guard

Fabric 1.20.1 client mod (`environment: client`), Java 17, bound to the **integrated-server**
lifecycle. It starts on `SERVER_STARTED`, stops on `SERVER_STOPPING` / `CLIENT_STOPPING`, and refuses
a dedicated server outright (`refused: not an integrated server`). Loopback HTTP only, one route,
default `127.0.0.1:6198`. Config is `<game dir>/config/pawbot-guard.json` (never committed); the
revocation marker is `<game dir>/config/pawbot-guard.revoked`.

Config constraints (full key list and examples: `minecraft-guard/README.md` §3 and
`minecraft-guard/config.example.json`): secret 32–256 chars; `world_id` `[A-Za-z0-9._-]{1,64}`;
`save_path`/`backup_root` absolute, link-free, disjoint; `actor_uuid` exact lowercase UUID;
`pvp_uuids` exact UUIDs, **at most 16**, no wildcard, must not contain the actor; `enabled` /
`write_enabled` / `pvp_enabled` only on the literal JSON `true`; `retention` 1–30 default 7;
`backup_interval_sec` 60–1800 default 1800; `port` 1024–65535 default 6198; unknown keys rejected.

### 1.1 World attestation

`MinecraftServer.getSavePath(WorldSavePath.ROOT)` is authoritative; the configured `save_path` is only
compared against it. All checks and every snapshot run on the integrated-server thread. Refused when:
not integrated, save root ≠ configured real path, any link/junction/reparse point in the chain,
missing/non-regular/oversized `.pawbot-test-world`, or marker content (UTF-8, `trim()`) ≠ `world_id`.
A marker written with a UTF-8 BOM therefore does not match. The marker is required both at startup and
again for every non-revoke permit, but the guard itself only starts when the world starts: adding the
marker to an already-open world achieves nothing until the world is re-entered. **Every** action except
`revoke` (including `observe`) additionally requires the port reported by `getServerPort()` to be
1024–65535 while `isRemote()` is true; a world that was never "Open to LAN" is `lan closed`, and
closing LAN later makes every subsequent non-revoke permit fail. `revoke` is deliberately outside this
LAN/world attestation path and still works while LAN is closed.

The world must be a **new disposable world registered by hand**. Saves are never adopted, copied or
renamed by the guard.

### 1.2 Request

`POST /v1/permit`, `Authorization: Bearer <secret>` (constant-time compare),
`Content-Type: application/json` (media type only, parameters allowed), body ≤ 4096 bytes,
exactly `{nonce, actor_uuid, action, target_uuid}`:
remote address must be loopback, nonce 32 lowercase hex chars, actor a lowercase UUID equal to the
configured actor, `action` ∈ connect/observe/write/pvp/backup/revoke, `target_uuid` empty except for
`pvp` (and then it must be in the allowlist). Non-snapshot, non-revoke permits wait ≤ 10 s;
`connect` and `backup` wait ≤ 60 s because both take a server-thread snapshot; `revoke` is answered
without a server-thread wait. The nonce is claimed **before** any decision, for 60 s, in a bounded
cache: a denied nonce stays burned and a replay is refused even if the first attempt was denied.

Loopback transport bounds: the Bearer header is verified before the body is read, and an
authenticated body that stalls past a 2 s read deadline is closed. `revoke` is not gated by a
snapshot and is never queued behind one, so emergency stop stays reachable while a copy or a stalled
body occupies a worker; it still requires authentication, `enabled`, the exact actor and an empty
target.

### 1.3 Response and status codes

Success is `200` with exact UTF-8 JSON bytes ≤ 16384 and `X-PawBot-Signature` =
lowercase hex HMAC-SHA256(secret, raw response bytes). Fields: `nonce` echo, `world_id`,
`save_path` (canonical), `host` = `127.0.0.1`, `port` (JSON number; see the revoke exception below),
`actor_uuid`, `action`, `target_uuid` echo, `issued_at` (Unix seconds, number), `expires_at` =
`issued_at + 5`, `can_write`, `can_pvp`, `backup_id`.

A signed `revoke` receipt is the one deliberate port exception: revocation is independent of LAN
state, so its `port` may be `0` (LAN closed) or a current port that changed after the client
connected. Python accepts `0` or `1024–65535` for `revoke`; Node accepts `0–65535`; neither accepts
the exception for any other action. Every non-revoke permit must still report the exact
configured/attested port. A `revoke` receipt is a stop acknowledgement, not a permit: its
`backup_id` is empty, and `can_write`/`can_pvp` may still echo the configured flags.
Clients must not treat those booleans as authorisation for an action.

A `200` may also be a **busy receipt** while a snapshot owns or is queued for the server thread (the
busy answer itself is produced off that thread). It adds exactly `busy` (true), `retry_after_ms`
(integer 50–5000; the current guard sends 250) and `attested_at` (Unix seconds, the time of the cached
live attestation), and is structurally non-mutating: `can_write`/`can_pvp` are false and `backup_id`
is empty. The cache is filled only by a successful live attestation and is cleared on revoke;
`attested_at` can be older than the 5 s permit window because a busy receipt is not a permit. A busy
`observe` is a valid observation; a busy `connect`/`backup`/`write`/`pvp` is a bounded pause (retry,
never replay). A client that does not understand `busy` fails closed on the empty `backup_id`, so the
field is additive for old clients. The guard emits no busy receipt when it has no live attestation:
it answers `503` instead of guessing a path or port.

Every non-200 carries a static ASCII body and **no** signature, no world path, no secret. Diagnostic
meaning matters, because the Python client deliberately collapses them (see the last column):

| status | body | when | Python client reports |
| --- | --- | --- | --- |
| 200 | signed JSON | action permit or signed revoke receipt | verified fields |
| 200 | signed busy JSON | a snapshot owns the server thread | `observe` row; `GuardBusy` for mutations |
| 400 | `bad request` | bad UTF-8/JSON, wrong/extra keys, bad nonce/UUID/action | `guard_unavailable` |
| 401 | `unauthorized` | missing/wrong Bearer | `guard_unavailable` |
| 403 | `forbidden` | peer is not loopback (transport level) | `guard_denied` |
| 403 | `disabled` / `not integrated` / `world unavailable` / `world rejected` / `marker mismatch` / `lan closed` / `actor rejected` / `target rejected` / `target not allowed` / `write disabled` / `pvp disabled` / `backup required` / `backup stale` | policy denial | `guard_denied` |
| 404 | `not found` | wrong path | `guard_unavailable` |
| 405 | `method not allowed` | not POST (`Allow: POST`) | `guard_unavailable` |
| 409 | `replay` / `revoked` | nonce reuse; sticky revocation | `guard_denied` |
| 413 | `invalid size` | empty body or > 4096 bytes | `guard_unavailable` |
| 415 | `unsupported media type` | not `application/json` | `guard_unavailable` |
| 503 | `unavailable` / `response too large` | server-thread refusal/timeout, no live busy attestation, non-durable revoke marker; signed body would exceed 16384 | `guard_unavailable` |

For `revoke`, an authenticated `409 revoked` is the already-latched answer: the Python client raises
`GuardAlreadyRevoked`. It proves the in-memory latch only (durability needs an earlier signed receipt
for the same guard identity — see §4), and no signed receipt is issued, so a second revoke never
returns a fresh signature.

Consequence for operators: **`guard_unavailable` also covers a wrong secret** (401). See
[MINECRAFT-RESIDENT.md](MINECRAFT-RESIDENT.md) §10 before assuming the guard is down. Splitting 401
out of `guard_unavailable` would be a client-code change, not a documentation fix.

### 1.4 Capabilities, freshness, backups

World mutation and PvP are separate capabilities: `write` needs `can_write`, `pvp` needs
`can_pvp` **and** a target-specific allowlist entry. `observe` needs no backup id; `connect`,
`write`, `pvp` and `backup` responses must carry a nonempty `backup_id`.

`connect` first runs `saveAll` and a full snapshot, so every connection starts from a fresh backup.
`backup` does the same on demand. The sidecar asks for exactly this one `connect` permit immediately
before the socket opens and does **not** send a separate `backup` first; the resident makes no
snapshot request of its own, so a connect is exactly one guard snapshot. After that, `write`/`pvp`
accept a snapshot whose age is `-1 … backup_interval_sec` seconds: exactly 1800 s is accepted, 1801 s
is denied, and a snapshot timestamped more than 1 s in the future is denied. The guard is the
unconditional upper bound; a caller (sidecar, resident) cannot widen the window by asking more often.

**One schedule.** The guard's `backup_interval_sec` is the only periodic-snapshot schedule. The Node
sidecar starts no backup timer and the Python resident makes no periodic `backup` request; the
resident's old `backup_interval_min` key is read only by an advisory helper and never schedules a
backup, so it is compatibility-only. A configured 60 s interval therefore produces one snapshot per
60 s of server ticks, and a failed attempt does not retry every tick. A tick that finds another
snapshot already running skips without losing the interval.

Snapshot rules: flush/save first, copy on the server thread, refuse links/reparse points and a world
that changed during the copy, exclude the root `session.lock`, enforce file-count/byte budgets
(examples 20000 files / 2 GiB), keep the newest `retention` snapshots and only prune names this guard
produced (`backup-<13 digits>-<3 digits>`).

### 1.5 Revocation

`revoke` latches revocation in memory, drops the session backup, tries to make
`config/pawbot-guard.revoked` durable, and starts a best-effort disconnect of the registered actor on
the server thread. It returns a signed receipt only when the marker is durable (present, regular and
containing `revoked`); if persistence fails, the in-memory latch still refuses every permit but the
answer is an unsigned `503 unavailable`, so a host must report an incomplete stop rather than a
durable one. The receipt
does not confirm that the actor was kicked. After revocation, **new** requests are refused with
`409 revoked`; a second `revoke` gets that authenticated `409` and no signed receipt. The marker is
re-checked on every authenticated request and at startup, so a human can latch the stop by creating
the file while the guard is already listening.

Limits of that guarantee (source behaviour, not a doc omission): revocation cannot retroactively
invalidate a permit that was already returned, and a side effect whose permit was obtained just
before the revoke can still be executing. It does refuse the snapshot: a request that is already
queued or inside the save/copy is answered `409` and its snapshot is never published, while the copy
itself may still finish on the server thread. Revocation guarantees "no new permit" plus a best-effort
actor kick, not "the actor was disconnected" and not "the world never changes again in this tick". The
offline review at `reports/minecraft-review/repaired-2026-11-01.md` records the queued-permit race as
fixed (100 synthetic rounds, `late200=0`); that is an offline result, not a live-game claim.

Deleting the marker **does not** clear the in-memory latch: the guard must be reconstructed
(re-enter the world / restart the game) before it can issue permits again. The log line is
`refused: revoked (delete config/pawbot-guard.revoked to re-arm)`.

### 1.6 Restore

Restore is offline only; `RestoreTool` is a standalone main class that the mod never calls. Rules
(normative): target directory must not exist (so an original world can never be overwritten); parent
must exist and the whole chain must be link-free; no ancestor of the target may contain
`session.lock`; the backup may not contain `session.lock`; the backup id must match
`backup-<13 digits>-<3 digits>`; the backup's `.pawbot-test-world` must equal `world_id` exactly;
target must not overlap the backup root or source; the source backup is read-only; the optional fifth
argument protects the owner's original world path even while that path is absent; the restored
directory must be registered by hand in `save_path` before use.

## 2. Node sidecar

Directory `app/minecraft-sidecar`, independent package and lockfile, Node ≥ 22. stdin/stdout
UTF-8 newline-delimited JSON-RPC 2.0, one request per line, ≤ 65536 bytes/line, no batch/no
notifications, stdout exclusively protocol (library stdout/stderr/console are suppressed and stderr
is discarded by the host). Methods: `connect`, `observe`, `skill`, `cancel`, `shutdown`; request ids
are positive safe integers; params reject unknown keys.

Startup event `{"jsonrpc":"2.0","method":"event","params":{"type":"ready","protocol_version":1}}`;
the host waits for it, and a wrong version is refused.

Trusted environment (never model parameters): `PAWBOT_MC_GUARD_URL`, `PAWBOT_MC_GUARD_SECRET`,
`PAWBOT_MC_WORLD_ID`, `PAWBOT_MC_WORLD_PATH`, `PAWBOT_MC_ACTOR_UUID`, `PAWBOT_MC_PVP_UUIDS` (JSON
list; the sidecar and the guard both accept at most 16 and reject more, so configure at most 16),
`PAWBOT_MC_WRITE_ENABLED`, `PAWBOT_MC_PVP_ENABLED`, `PAWBOT_MC_AUTH_DIR`. Only the literal `"true"`
enables a flag. The child receives only OS basics plus these keys: QQ/LLM keys, `NODE_OPTIONS`,
`DEBUG`/`NODE_DEBUG` and everything else in the parent environment are dropped. Node receives no QQ
UID, persona, memory or model keys. Microsoft cache lives only inside `PAWBOT_MC_AUTH_DIR`; device
codes appear only in the human-run `auth.mjs` terminal; there is no automatic auth fallback and the
protocol process never requests a device code (missing cache → static `Local authentication setup
required`).

`connect` params `{host,port,version,username,auth_mode,timeout_sec}`: literal `127.0.0.1`, explicit
integer port 1–65535 (no 25565 default, no LAN broadcast discovery), version `1.20.1`, username/cache
alias `[A-Za-z0-9_]{1,16}`, explicit `microsoft` or `offline`, timeout 1–30 s. The guard-attested port
must equal the connection port, and the Mineflayer UUID on spawn must equal the configured actor UUID
or the sidecar disconnects immediately. A fresh connect permit (after any cached-token refresh) is
obtained immediately before the game socket opens; because the guard's `connect` action performs the
mandatory fresh snapshot, that one permit is the per-connect backup and no extra `backup` request is
sent.

`skill` request `{name,args,timeout_sec}` with the exact predefined schemas — the 17 skills and their
bounds are specified in [app/minecraft-sidecar/README.md](../app/minecraft-sidecar/README.md) §Skill
arguments; this contract repeats only the invariants: every target player in `attack` needs the exact
local allowlist **and** a target-specific PvP permit; non-player hostile defense uses a write permit
and never falls back to a player; `chat` rejects slash commands with leading whitespace/control
characters; lookups are limited to loaded local state; no eval/exec/dynamic imports, filesystem,
shell, RCON, raw commands, arbitrary keys or generated code.

Limits: one active skill, waiting queue ≤ 8 (`Skill queue full`), JSON-RPC in flight 16 normal plus 2
reserved cancel/shutdown slots, skill timeout ≤ 30 s measured from queue admission, responses
≤ 32768 bytes, events ≤ 4096 bytes. `cancel` aborts queued work, stops pathfinding/dig/controls/PvP,
closes the connection and quarantines in-flight work. A timeout or disconnect quarantines the process;
the host must never replay a mutation. Node never reconnects itself. A guard watchdog starts a request
every 250 ms (no overlap, 650 ms timeout) and stops/closes on failure or context change; a signed busy
receipt instead stops the active action and pauses admissions for its bounded `retry_after_ms` without
quarantining, and the guard answers busy off-thread so the 650 ms deadline still detects a real
revoke/absence in under one second. The event-loop and server responsiveness behind that watchdog are
still live-unverified.

Static error codes: `-32700` parse, `-32600` invalid request, `-32601` method not found,
`-32602` invalid params, `-32603` operation failed, `-32001` connection unavailable, `-32002` guard
denied, `-32003` queue full, `-32004` cancelled, `-32005` timeout, `-32006` local authentication setup
required, `-32007` request capacity exceeded, `-32008` guard busy (a signed snapshot-busy pause: the
job is cancelled and not replayed, the connection stays up, and the next choice may run after the
bounded pause). Messages are static; adapter exception text is never echoed.

## 3. Python host

`MinecraftSidecar` owns the child process: spawn, `ready` handshake, bounded correlated requests and
events, startup/shutdown/cancel, quarantine-on-timeout, capped exponential reconnect of the process
(restart → fresh guard attestation → `connect`, and only `connect` is ever replayed), and reaping.
The resident consumes perception; stimuli never call skills directly.

Identity: the guard-attested player UUID is the bot's actor identity, bound in local config
(`minecraft.owner_player_uuid`); the owner is the configured **complete numeric QQ UID**
(`minecraft.owner_qq_uid`, validated by the existing `identity.uid_of`), never a nickname or a digit
suffix. Other players are linked to QQ only through explicit local `owner_mappings`; unknown,
conflicting or unverifiable identities stay separate. Local policy (`minecraft_policy.py`) requires
`enabled`, `guard_required` true, an exact actor match, a permit whose `world_id` equals
`test_world_id`, an age ≤ `min(5 s, permit_max_age_sec)`, then `can_write`/`can_pvp` plus the local
flags and target list.

Persistence: resident state in `<DataRoot>/content/minecraft/`, machine state (budget, bindings, auth
cache) in `<DataRoot>/state/minecraft/`; legacy paths are never used and never migrated over. Shared
`bot.persona`, `bot.emotion` and `bot.longmem` are the only personality/affect/long-memory sources;
Minecraft episodes are written with `source="minecraft"` and private thought is never persisted,
logged or sent to a model.

Default disabled: `minecraft.enabled=false`, `write_enabled=false`, `pvp_enabled=false`, `port=0`.
An HA relay instance returns before the resident start path, so a relay never starts a resident
outside the active local owner instance.

## 4. Host emergency stop (outside the game protocol)

`python -m pawbot.minecraft_control stop [--webui-port N]` is a separate, model-independent path:

1. `POST http://127.0.0.1:<webui port>/api/control/minecraft-stop` with
   `Authorization: Bearer $env:PAWBOT_CONTROL_TOKEN`. The route is loopback-peer + independent Bearer
   token only (never the panel token, never a query parameter), and it is disabled (`403
   control_disabled`) when no control token was configured.
2. Fallback: a direct signed guard `revoke` built from `PAWBOT_MC_GUARD_URL`,
   `PAWBOT_MC_GUARD_SECRET`, `PAWBOT_MC_WORLD_ID`, `PAWBOT_MC_WORLD_PATH`,
   `PAWBOT_MC_ACTOR_UUID` and `PAWBOT_MC_LAN_PORT`. The port is parsed as an integer (unparseable →
   `0`), and `0` is valid for `revoke`: revocation does not depend on LAN, and the signed receipt may
   echo `port: 0` or a changed current port. An empty secret is the separate honest noop below, not a
   direct revoke. The direct fallback reads only the environment; it does not read
   `minecraft.guard_url`. This path cannot reach the sidecar, so on its own it reports
   `resident_stopped: false`, `sidecar_reaped: false`, `writes_drained: false` and `ok: false` even
   when the revoke succeeded (`revoked: true`, `revoke_durable: true`,
   `revoke_receipt: "signed"`). A bare `409` on this path is reported as `revoked: false`,
   `already_revoked: true`, `revoke_durable: false` and `error: "revoke_not_durable"` — "no new
   permits, durability unverified", never a durable stop.
   Its `actor_disconnected_by_guard: true` on the signed path reflects the accepted revoke, not a
   confirmed kick: the guard's kick is asynchronous and best-effort.

`ok: true` requires every step confirmed: guard revoke, resident stopped, sidecar reaped, writes
drained, latch set (or `session:"none"` with a confirmed revoke). The control path enforces this; the
direct fallback cannot confirm resident/sidecar and never returns `ok: true` by itself (it is merged
only with a partial control result). A partial stop prints the real per-step booleans, exits non-zero
and tells the operator to verify by hand. Nothing here widens a permission and the token is never
placed in argv.

No active resident is not a success exemption. The control path reports `session: "none"` and sets
`resident_stopped`, `sidecar_reaped` and `writes_drained` true, but it still requires a confirmed
revoke:

- No `PAWBOT_MC_GUARD_SECRET`: no guard credential exists in this process, so no revocation is
  attempted. The result is `ok: false`, `revoked: false`, `guard_configured: false`, `noop: true`,
  `revoke_skipped: "no_guard_configured"` and `error: "guard_not_configured"`. It says nothing about
  the guard side. If a guard is actually configured, supply the missing environment and retry (or use
  the direct fallback).
- A secret is configured: the revoke client validates with the revoke action, so `port: 0` is valid
  and the receipt may carry `port: 0` or a changed current port. A verified **signed** receipt sets
  `revoked: true` and `revoke_durable: true` and can make `ok: true`; invalid settings, a denial or an
  unreachable guard leave `ok: false` with the static error.

A bare authenticated `409 revoked` is **not** durable evidence. The guard answers `409` as soon as
its in-memory latch is set, before it persists the marker, so the host records it as `revoked: false`,
`revoke_durable: false`, `ok: false` and `error: "already_revoked_unverified"`. The durable
already-revoked reading exists only when **this process previously recorded a signed revoke receipt
for that exact guard identity** (URL + world id + world path + actor UUID); only then does a later
`409` count as an already-persisted revoke. A `503` followed by `409` therefore never reports a
success. The regression probe
`reports/minecraft-review/probe/revoke_durability_false_success.py` covers this behaviour and now
passes (`no false success`; second call `already_revoked_unverified`, `ok: false`) — see
`reports/minecraft-review/final-verification-2026-11-02.md` E1. The signed-receipt path and the
`port: 0` / current-LAN-port semantics are unchanged.

## 5. Versioning

This contract is versioned by the quoted string `v1` in the sidecar's `ready` event and by the
`protocol_version` field (currently `1`). Behaviour changes need: (a) this file, (b) the sidecar
README, (c) the sidecar `protocol_version` or an explicit "implementation differs" note here, and
(d) owner confirmation for any widening of authority. Adding a state to §1.3, changing a bound, or
relaxing a capability rule is a contract change even if no code is touched.

The snapshot-busy receipt (2026-11-01) is intentionally **additive on status 200** and does not bump
`protocol_version`: the request bytes and the action set are unchanged, `busy` is absent/false for
every pre-existing permit, and an old client that does not know the field rejects a busy mutation on
the empty `backup_id` — exactly today's fail-closed outcome, no regression. Clients and the guard
should still be deployed together to get the no-quarantine benefit.

