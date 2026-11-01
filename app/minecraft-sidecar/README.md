# Minecraft sidecar

Independent Node.js 22+ package implementing `docs/MINECRAFT-PROTOCOL.md` v1. Source-only/offline development: no game/world save, Microsoft login, runtime credentials, persona, memory, or QQ data is used by installation or tests. Installation has no lifecycle scripts. The entry point is `node app/minecraft-sidecar/index.mjs`; imports of source modules do not connect or authenticate.

## Install and verify

From this directory:

```powershell
npm ci --ignore-scripts
npm test
npm audit --audit-level=moderate
```

Only this package/lockfile is involved. Direct pins: Mineflayer 4.39.0 (Node >=22), pathfinder 2.4.5, PvP 1.3.2, prismarine-auth 3.1.1, vec3 0.1.10. Lockfile pins transitive packages. `uuid` is overridden to CommonJS-compatible 11.1.1 to resolve GHSA-w5hq-g745-h8pq; both audited upstream consumers use only `v4()`. `mineflayer-utils` uses the same Mineflayer version instead of introducing an older nested bot runtime. Offline import tests verify exported plugin functions, registry support for 1.20.1, auth flow API and UUID generation. These checks do not prove server compatibility.

## Trusted environment

Python must build a minimal environment; never inherit QQ/OpenRouter keys, memory/persona values, `NODE_OPTIONS`, `DEBUG` or `NODE_DEBUG`. Required:

- `PAWBOT_MC_GUARD_SECRET`: local-only 32..256 characters (the Java guard's authoritative bound); never a model argument.
- `PAWBOT_MC_WORLD_ID`: exact local guard identifier, 1..64 characters matching `[A-Za-z0-9_.-]` (the Java guard's bound).
- `PAWBOT_MC_WORLD_PATH`: exact canonical path spelling emitted by the guard; the Node process does not open world content.
- `PAWBOT_MC_ACTOR_UUID`: exact canonical bot actor UUID, not the owner human avatar.
- `PAWBOT_MC_GUARD_URL`: defaults to `http://127.0.0.1:6198`; either the origin or exact `/v1/permit` endpoint is accepted. Alternate numeric IP spellings, hostnames, IPv6, credentials, other paths, query strings, fragments and HTTPS are rejected.
- `PAWBOT_MC_PVP_UUIDS`: JSON array of canonical exact UUIDs, default `[]`, maximum 16 (the guard refuses more; this client rejects more rather than accepting a config the guard would reject), no wildcards.
- `PAWBOT_MC_WRITE_ENABLED`, `PAWBOT_MC_PVP_ENABLED`: only literal `true` enables each; both required for player attacks.
- `PAWBOT_MC_AUTH_DIR`: canonical absolute machine-local cache directory for Microsoft mode. No network share/link/junction or automatic fallback directory.

The guard is authoritative for dedicated disposable-world membership, world path/link safety, actor, target, write/PvP capability and backup freshness. HMAC is checked over bounded original UTF-8 bytes before decoding JSON. Every successful action permit is checked for exact nonce/action/target/actor/world/path/host/connection-port and a positive <=5-second lifetime with <=1-second future drift. A signed `revoke` receipt is verified with the same signature/identity fields but is allowed to report port `0` (LAN closed) or a port that changed after this process connected, so a real revocation is never falsely rejected; every other action permit still requires an exact integer 1..65535 and the controller pins it to the connect port. A connect obtains **exactly one** guard `connect` permit immediately before the game socket opens (after any cached Microsoft token refresh); the guard's `connect` action itself performs the mandatory fresh `saveAll`+snapshot and returns its `backup_id`, so a separate pre-connect `backup` request would only duplicate the copy. Guard permits for connect/write/PvP must contain a nonempty bounded backup identifier; identifiers and attestations stay out of RPC output. The Java guard is the single periodic-snapshot scheduler: this process starts no backup timer and never asks for a periodic `backup`, so a configured guard interval cannot disagree with a client timer. A signed **busy receipt** (`busy:true`, `can_write:false`, `can_pvp:false`, `backup_id:""`, bounded `retry_after_ms`, `attested_at`) means a snapshot owns the guard's server thread: it is verified like any other receipt, a busy `observe` is accepted, and every mutation action is paused and retried—never replayed and never quarantined. A denied/unavailable guard still closes and quarantines the connection. Observe permits never substitute for a mutation/PvP permit.

## Explicit Microsoft setup

A human must run this separately in an **interactive local terminal**. This step is deliberately not run by installation or tests:

```powershell
# Pick a NEW machine-local cache directory; use the same value in Python's trusted env.
$env:PAWBOT_MC_AUTH_DIR = 'D:\PawBotLocal\minecraft-auth'
node auth.mjs SyntheticBot
```

`SyntheticBot` is a nonsecret cache alias; it must match `connect.username`. The CLI displays a Microsoft device code only in that interactive terminal, authenticates Java ownership/profile through prismarine-auth, and writes cache files only inside the configured directory. It does not connect to Minecraft. It refuses redirected stdin/stdout. Do not capture/relay CLI output into model state, logs, chat or JSON-RPC. Keep the cache directory machine-local and protect it with OS account permissions; POSIX file modes are also restricted by the writer.

The protocol process does not request a new device code. A missing/expired unusable cache fails with the static `Local authentication setup required` error; it never falls back to offline mode. Cached-token refresh and Microsoft service behavior are not tested against real credentials. Offline mode is explicit and still must match the guard's exact bot actor UUID after spawn.

## JSON-RPC and Python integration

UTF-8 newline-delimited JSON-RPC 2.0. Each request has exactly `jsonrpc`, positive safe-integer `id`, `method`, `params`. Maximum line 65536 bytes; fragmented oversized lines are discarded through the next newline. One request per line, no batch RPC, notifications or arbitrary methods. Parameters reject unknown keys. Stdout belongs exclusively to protocol; library stdout/stderr/console output is suppressed, and errors are static.

Startup event:

```json
{"jsonrpc":"2.0","method":"event","params":{"type":"ready","protocol_version":1}}
```

Commands:

- `connect`: `{host,port,version,username,auth_mode,timeout_sec}`; literal host `127.0.0.1`, explicit integer port 1..65535, version `1.20.1`, username/cache alias 1..16 alphanumeric/underscore, explicit `microsoft` or `offline`, timeout 1..30 seconds.
- `observe`: `{}`; returns health, food, position, dimension, at most 64 inventory `{name,count}` records, 32 nearby entities and 32 nearby blocks. It also reports real game facts derived from the live bot, each omitted when the value is unknown rather than guessed: `time_of_day` (integer 0..24000 from `bot.time.timeOfDay`), `weather` (`clear`/`rain`/`thunder` from `bot.isRaining`/`bot.thunderState`), `light` (0..15, higher of block/sky light at the player's feet from loaded blocks) and `roof` (boolean: a loaded full block exists 2..6 blocks directly above; omitted if any scanned chunk block is unloaded). `events` is a bounded list (at most 16 per observation, oldest dropped first) of safe events: `{"type":"health_loss","from":N,"to":N}` with clamped 0..2048 numbers, `{"type":"death"}` and `{"type":"respawn"}`. Events carry fixed names and bounded numbers only — never chat text, display names/nicknames, player/QQ identifiers, UUIDs or tokens. No raw NBT, packets, chat, display names or arbitrary strings.
- `skill`: `{name,args,timeout_sec}`; timeout 1..30 seconds measured from queue admission, including waiting and guard requests.
- `cancel`: `{}`; aborts queued requests, stops active controls/dig/path/PvP, closes active connection and quarantines the sidecar if work is in flight.
- `shutdown`: `{}`; aborts/drains admission, stops controls and socket, emits shutdown and exits after its response. It does not issue persistent guard revocation.

`inventory` and `set_home` are read-only local skills and are admitted with an `observe` permit (no write capability needed); `set_home` only records coordinates in process memory and never mutates the world. The Python caller must send `set_home` after its first verified observation and after every reconnect if it wants `home` to work; the sidecar does not infer or persist a home across restarts.

Connect returns `{connected:true,uuid,world_id,backup_completed:true}`. Events are bounded to 4096 bytes and include `ready`, `connected`, `queued`, `skill_complete`, `disconnected`, `shutdown`; no game chat or raw errors are events. The `connected` event contains actor UUID, world identifier, loopback host and port. Responses/results stay under 32768 bytes. Request IDs are bounded to 16 in flight plus two reserved cancellation/shutdown slots; duplicate active IDs are rejected.

Static error codes: `-32700` parse error; `-32600` invalid request; `-32601` method not found; `-32602` invalid parameters; `-32603` operation failed; `-32001` connection unavailable; `-32002` guard denied; `-32003` queue full; `-32004` cancelled; `-32005` timeout; `-32006` local authentication setup required; `-32007` request capacity exceeded; `-32008` guard busy (a signed snapshot-busy pause, not a denial).

Python owns spawning/restart/backoff and the local owner enable/revoke latch. Wait for `ready` before connect. Never replay a timed-out/disconnected mutation. Node never reconnects itself; cancellation/timeout, identity mismatch, connection end or guard loss quarantines the process. Restart under a fresh guard+backup only when Python's local policy permits it. A watchdog starts a request every 250ms (no overlap), with 650ms timeout, rechecking the active action/target. A signed busy receipt during that watchdog stops the active action and pauses admissions for the receipt's bounded `retry_after_ms` (<=5s) instead of quarantining; any other failure still stops/closes within one watchdog period. The 650ms deadline is unchanged, because a busy answer is produced off the snapshot's server thread. Event-loop scheduling and actual server responsiveness still require runtime verification.

## Exact skill arguments and limits

Every listed argument is required; no extras. Coordinates are finite; y -64..319, x/z within +/-29999984. Block coordinates are integers. Movement may use finite fractional coordinates. All movement targets must be loaded and within 16 blocks. Direct block/entity interactions require 4.5 blocks, attacks 3 blocks. Entity iteration inspects at most 1024 loaded entries, player identity lookup at most 128.

| Skill | Args | Behavior |
|---|---|---|
| move | x,y,z,radius | radius0.5..4; bounded pathfinder goal |
| follow | target_uuid,duration_sec | local player; duration1..30; repeated local goals |
| chat | text | 1..200 chars; rejects slash command after leading whitespace/control/newline text |
| dig | x,y,z | loaded reachable diggable block |
| collect | block,count | registry-style name, count1..16; bounded local scan and individually permitted digs; approaches up to16 matching loaded item drops; reports mined blocks and measured inventory increase |
| craft | item,count | count1..16 recipe operations, one guarded craft at a time; reports actual recipe result count |
| inventory | none | bounded item names/counts |
| chest | x,y,z,operation,item,count | deposit/withdraw, count1..64; loaded chest/trapped_chest/barrel; guard before open and transfer |
| place | x,y,z,face_x,face_y,face_z,item | target cell plus one cardinal face; loaded full support; inert registry block items only |
| farm | x,y,z,seed | wheat_seeds/beetroot_seeds/carrot/potato; **x,y,z is the soil block** (loaded `farmland`/`dirt`/`grass_block`), not the player's feet; the crop lives at `y+1`; only a mature matching crop at `y+1` is harvested, the soil is hoed if needed, then planted |
| breed | target_uuid,item | known local cow/sheep/pig/chicken/rabbit and matching food; never player |
| attack | target_uuid | one strike; known exact player requires local PvP list and target-specific permit; known hostile mob uses write permit; unknown entities deny |
| eat | none | selects nonhazardous inventory food, equips, consumes |
| explore | x,y,z | loaded local path goal, radius2 |
| home | none | bounded same-dimension path to the home recorded by `set_home` in this process; fails until `set_home` ran on this connection |
| set_home | none | stores the current position and dimension in process memory only (no world mutation, keyless arguments); lost on sidecar restart, never sent to the game; the caller must reseed it after the first observation and each reconnect |
| respawn | none | guarded explicit respawn; automatic respawn disabled |

Pathfinder dig/build/doors/towers/parkour/sprinting are disabled; scaffold list empty. Pathfinder computation is limited to 20ms slices, 1000ms thinking and search radius20. PvP plugin is loaded and force-stopped; the bounded attack skill uses Mineflayer's single `attack()` with immediate permit checks, not the plugin's autonomous repeated combat loop. Every mutation admission is spaced at least125ms (<=8/sec); tests measure spacing. This is not a claim of a 20Hz agent control loop. Continuous motion physics is Mineflayer-controlled and subject to the active-action watchdog. No raw commands, eval, generated code, filesystem/RCON/shell/control-state RPC or dynamic path imports are exposed.

## Evidence and remaining runtime verification

Offline verification on November 1, 2026 (Node 24.14.0 on Windows): clean `npm ci --ignore-scripts` added 100 packages and audited 101; `npm test` passed 115 tests with 0 failures/cancellations/skips (about 4.8 seconds); `npm audit --audit-level=moderate` reported 0 vulnerabilities; `node --check` passed the two entry points and six source modules. `npm ls --depth=0` matched all five exact direct pins. The helper module is deliberately excluded from the test count.

Tests exercise real GuardClient verification, real controller admission/cancel/watchdog and real adapter skill functions with synthetic loaded game state; a synthetic loopback HTTP server tests the actual signed transport. Child tests execute the actual entry point and auth CLI refusal path. Auth cache tests use temporary synthetic data. No test logs in, starts a browser, connects to Minecraft, reads a world, loads runtime data or obtains real credentials. Additional offline regressions cover: Java-aligned config bounds and rejected over-bound configs; a signed revoke receipt with port `0`/a changed port accepted while action permits with port `0` stay denied; `set_home` admitted with an observe permit when write is disabled (with real adapter plus real controller); real-adapter farm coordinates (soil block accepted, player-feet coordinate rejected); real-adapter derived `time_of_day`/`weather`/`light`/`roof` with unknown values omitted; bounded health-loss/death/respawn events that carry no chat text, display names, UUIDs or private fields; and the busy contract (signed busy observe accepted, busy mutations paused without quarantine, forged/extra-field busy bodies refused, connect retrying a busy window, a real revoke still closing under 1 s after a busy window, and no client-side backup timer).

Still unverified: real integrated Fabric LAN connection/UUID matching, Microsoft device flow/token renewal, plugin compatibility against an actual server, world-specific crafting/container/farm/breed/server rules, physical path/cancel behavior and wall-clock watchdog under server/event-loop stalls. Dedicated guard backup/restore integration is tested by the guard package's separate synthetic suite, not here. Do not enable the resident until a human performs an isolated disposable-world acceptance run.

Primary API sources checked November1,2026:

- Mineflayer official API and source: https://github.com/PrismarineJS/mineflayer/blob/master/docs/api.md ; https://github.com/PrismarineJS/mineflayer/blob/master/lib/loader.js
- Pathfinder documented promises, movement restrictions and immediate stop: https://github.com/PrismarineJS/mineflayer-pathfinder
- PvP forceStop/automatic loop source: https://github.com/PrismarineJS/mineflayer-pvp (published package 1.3.2 source inspected offline)
- Prismarine auth cache factory/device callback: https://github.com/PrismarineJS/prismarine-auth ; https://github.com/PrismarineJS/node-minecraft-protocol/blob/master/src/client/microsoftAuth.js
- Exact published package metadata: https://registry.npmjs.org/mineflayer/4.39.0 ; https://registry.npmjs.org/mineflayer-pathfinder/2.4.5 ; https://registry.npmjs.org/mineflayer-pvp/1.3.2 ; https://registry.npmjs.org/prismarine-auth/3.1.1 ; https://registry.npmjs.org/uuid/11.1.1
