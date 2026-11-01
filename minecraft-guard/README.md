# PawBot Guard (Fabric 1.20.1, Java 17)

Loopback permit and backup guard for the **dedicated disposable integrated-server test world**.
It is a client-side Fabric mod: it starts only while an integrated server (single-player world
opened to LAN) is running, and it refuses dedicated servers. It never exposes commands, RCON,
arbitrary files, shell, JavaScript, or a model-facing tool.

> Status (2026-11-01, task16): source builds with Fabric Loom and `remapJar` succeeds. 63
> dependency-free offline regressions pass. The mod has **not** been loaded into a real
> Minecraft client and no real LAN connection was made; those remain live-unverified.

## 1. What it enforces

- **World identity**: the authoritative save root is
  `MinecraftServer.getSavePath(WorldSavePath.ROOT)`, never the configured path. The real path must
  be exactly the configured canonical `save_path`, contain no symlink/junction/reparse point, and
  hold a regular `.pawbot-test-world` file whose trimmed content equals `world_id`.
- **Integrated server only**: `isIntegrated()` is true only for `IntegratedServer` with
  `isDedicated() == false`.
- **LAN only when actually open**: the reported port comes from `getServerPort()` and is forced to
  0 unless `isRemote()` is true, so a never-published world cannot be attested.
- **Authenticated loopback HTTP**: one route, `POST /v1/permit`, bound to `127.0.0.1`. Requests
  require `Authorization: Bearer <secret>` (constant-time comparison), `Content-Type:
  application/json`, and a <= 4096-byte body with exactly `{nonce, actor_uuid, action,
  target_uuid}`.
- **Exact request shape**: nonce is 32 lowercase hex chars, actor is a lowercase UUID equal to the
  configured actor, target is empty except for `pvp`, and `pvp` targets must be in the exact local
  allowlist. Wildcards are not a UUID and are rejected.
- **Replay and revocation**: each nonce is claimed before any decision (a denied nonce stays
  burned) for 60 s in a bounded cache. `revoke` latches the in-memory generation first, creates and
  fsyncs `pawbot-guard.revoked`, verifies it, drops the session backup, and answers immediately with
  a signed receipt; the actor kick is queued best-effort so a slow save cannot delay the stop.
  Revocation is checked on every authenticated request, inside the queued server-thread build,
  immediately before signing, and once more before publication, so a connect/backup/write that was
  already queued or mid-snapshot is refused (`409 revoked`) and its snapshot is never published
  after the revoke. If the marker cannot be made durable, the in-memory latch still refuses every
  permit but the response is an **unsigned 503**, so the host reports an incomplete stop instead of
  claiming a revocation a restart would forget. The receipt may report `port: 0` when LAN is
  closed; a durable revoke receipt is valid either way. Revocation survives restart; the marker is
  also re-checked on every authenticated request so an operator can stop the guard by creating the
  file. Only a human deleting the marker re-arms it.
- **Exact signed response bytes**: success returns `200` with a UTF-8 JSON body <= 16384 bytes and
  `X-PawBot-Signature = hex HMAC-SHA256(secret, raw response bytes)`. Denials carry no signature and
  no world path. Fields: `nonce`, `world_id`, `save_path` (canonical), `host`, `port` (current LAN
  port; JSON number), `actor_uuid`, `action`, `target_uuid`, `issued_at`, `expires_at` (issued + 5),
  `can_write`, `can_pvp`, `backup_id`. `can_pvp` is true only when both `pvp_enabled` and
  `write_enabled` are on, because PvP mutates the world. Normal permits wait at most 10 s; `backup`
  and `connect` wait at most 60 s because both take a server-thread snapshot.
- **Busy receipts during a snapshot**: a save/snapshot runs inline on the integrated-server thread, so
  it can outlast any client deadline. Instead of making a liveness probe wait behind that copy, the
  guard answers a signed **busy receipt** (`200`, additive fields `busy: true`, `retry_after_ms`,
  `attested_at`). It is structurally non-mutating: `can_write: false`, `can_pvp: false` and an empty
  `backup_id`, so no client version can accept it as a mutation permit. Its `save_path`/`port` come
  only from the most recent successful live server-thread attestation; if none exists the guard
  fails closed with `503` instead of guessing. A busy `observe` is a valid non-mutating observation.
  The guard keeps the client deadline (and revocation latency) short because `revoke` is never gated
  and never queued behind a copy.
- **Snapshot gate invariants**: at most one snapshot at a time; a request that needs a snapshot opens
  a ticket before it touches the server-thread queue; a request already queued when a tick snapshot
  opens the gate is released with a busy receipt within ~50 ms; the new session is published and the
  gate released in one critical section (a write immediately after the copy reads the new session,
  never the pre-copy one); a snapshot the caller did not receive in time, and any snapshot that
  completes after a revoke, is never published.
- **Backups**: every `connect` runs `saveAll` and a fresh snapshot before responding. **The guard is
  the only periodic-snapshot scheduler** (`BackupSchedule`, `backup_interval_sec` 60–1800, default
  1800); the Node sidecar and Python resident run no backup timer of their own. write/pvp accept a
  snapshot whose age is `-1..backup_interval_sec` seconds; exactly 1800 s old is accepted, 1801 s is
  denied. A tick that finds the gate busy skips without advancing its deadline; a failed attempt
  advances it so a broken world cannot retry every tick. Snapshots exclude the root `session.lock`,
  refuse links/reparse points, enforce file-count/byte/disk budgets, and refuse a world that changed
  during the copy. Retention keeps the newest 7 snapshots by default and only prunes names this
  guard created (`backup-<13 digits>-<3 digits>`). All deletion (startup `tmp-*` cleanup, retention,
  restore staging cleanup) inspects every directory entry with `NOFOLLOW_LINKS` and deletes a
  link/junction/reparse point itself instead of descending through it.
- **Restore is offline**: `RestoreTool` is a standalone main class, not called by the mod. See §5.

Defaults are disabled: `enabled=false`, `write_enabled=false`, `pvp_enabled=false`, and empty
`pvp_uuids`. The guard HTTP port defaults to `6198`.

## 2. Layout

```
src/main/java/dev/pawbot/guard/
  GuardMod.java              Fabric client entry point; server lifecycle/tick hooks
  IntegratedWorldAdapter.java Minecraft 1.20.1 Yarn adapter
  WorldView.java             narrow, mockable server view
  PermitService.java         transport-free permit/attestation/revocation decision point
  SnapshotGate.java          one-snapshot-at-a-time gate + non-mutating busy cache
  BackupSchedule.java        pure periodic-snapshot policy (the single schedule)
  GuardConfig.java           strict local config loader
  BackupManager.java         bounded, link-safe snapshots
  PathSafety.java            link/reparse-point and disjoint-root checks
  RestoreTool.java           offline restore CLI
  HttpGuard.java             loopback-only HTTP transport
  Hmac.java, Json.java, NonceCache.java
src/test/java/dev/pawbot/guard/
  GuardSelfTest.java         JDK-only regression runner
  FakeWorld.java, ThreadedFakeWorld.java, Fixtures.java
```

Config file: `<game dir>/config/pawbot-guard.json` (git-ignored). The revocation marker is
`<game dir>/config/pawbot-guard.revoked`.

## 3. Configuration

Start from `config.example.json`; all values are owner-local. `enabled` only turns on for the
literal JSON `true` (same for `write_enabled` / `pvp_enabled`). Unknown keys are rejected.

| key | rule |
| --- | --- |
| `secret` | 32–256 characters, random, local only |
| `world_id` | `[A-Za-z0-9._-]{1,64}`, equals the marker file content |
| `save_path` | absolute canonical path of the disposable world, no links |
| `backup_root` | absolute, no links, disjoint from `save_path` |
| `actor_uuid` | exact lowercase UUID of the bot player |
| `pvp_uuids` | exact UUID list, <= 16, must not contain `actor_uuid`; empty by default |
| `enabled` / `write_enabled` / `pvp_enabled` | literal `true` only; examples are `false` |
| `retention` | 1–30, default 7 |
| `backup_interval_sec` | 60–1800, default 1800; the guard is the freshness authority |
| `port` | 1024–65535, default 6198, loopback only |
| `max_backup_files` / `max_backup_bytes` | positive bounds; examples 20000 / 2 GiB |

The world must already exist, be manually marked, and never be an original save:

```powershell
# content must be exactly world_id (one line is fine)
Set-Content -LiteralPath '<new-world>\.pawbot-test-world' -Value 'pawbot-test-1' -NoNewline
```

## 4. Build and test

This repository carries Gradle 8.7 and its dependency cache under `minecraft-guard/.tooling`.
No JDK was installed for this task; the existing JetBrains Runtime 21 was used with
`--release 17`, so the produced class files are Java 17 (major version 61). Do not add a system
JDK. The `.tooling` cache is git-ignored and is not committed; a machine with an empty cache needs
one online dependency resolution (Minecraft/Loom/LWJGL) before `--offline` works.

```powershell
$env:JAVA_HOME = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr'   # existing runtime
$env:GRADLE_USER_HOME = (Resolve-Path 'minecraft-guard\.tooling\gradle-home').Path

# compile + remap the real Fabric mod
& (Resolve-Path 'minecraft-guard\.tooling\gradle-8.7\bin\gradle.bat').Path `
  -p minecraft-guard build --offline --no-daemon --console=plain
```

Artifact: `minecraft-guard/build/libs/pawbot-guard-1.0.0.jar`.

The regression suite is a JDK-only `main`, so it also runs without Gradle or Minecraft:

```powershell
$javac = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\javac.exe'
$java  = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\java.exe'
$src = Resolve-Path 'minecraft-guard'
$out = Join-Path $src 'build\pure-classes'
$core = Get-ChildItem (Join-Path $src 'src\main\java\dev\pawbot\guard\*.java') |
  Where-Object { $_.Name -notin @('GuardMod.java','IntegratedWorldAdapter.java') }
$test = Get-ChildItem (Join-Path $src 'src\test\java\dev\pawbot\guard\*.java')

& $javac --release 17 -encoding UTF-8 -d $out $core $test
& $java -cp $out dev.pawbot.guard.GuardSelfTest
# expected: passed=63 failed=0
```

`gradle selfTest` runs the same suite. `gradle build` runs `selfTest` through `check`; the stock
`test` task is intentionally disabled because the suite has no JUnit dependency. Tests use only
`java.io.tmpdir` synthetic worlds, fake server views, and loopback ports; they never read a real
save, credential, persona, memory, or launch a browser/game.

## 5. Offline restore

Run the standalone class from the built jar; the mod never calls it:

```powershell
java -cp minecraft-guard\build\libs\pawbot-guard-1.0.0.jar `
  dev.pawbot.guard.RestoreTool `
  <backup_root> <backup_id> <new_target_dir> <world_id> [protected_original_world]
```

Rules:

- target must **not exist** (no overwrite; an existing original world cannot be replaced); the
  completed staging tree is published by atomically creating the target directory
  (`Files.createDirectory`) and moving entries into it, so a target created by another process in
  the race is never replaced even on providers where `ATOMIC_MOVE` overwrites; parent must exist
  and the whole chain must be link-free;
- no ancestor of the target, at **any depth**, may contain `session.lock` (running/locked world);
- the backup may not contain `session.lock` (not a closed snapshot), must carry the marker with the
  exact `world_id`, and its id must match `backup-<13 digits>-<3 digits>`;
- target may not overlap the backup root or the source;
- optional fifth argument protects the owner's original world even if that path is currently
  missing; a deleted original is also compared through its deepest existing ancestor (links
  included), so an alias ancestor cannot hide an overlap, and a target or staging path that is an
  ancestor or descendant of it is refused;
- the source backup is read-only: it is never moved, deleted, or rewritten;
- the restored directory is new and must be registered manually in `save_path` before use.

## 6. Integration notes and limits

- Entry point is `ClientModInitializer`; it registers `ServerLifecycleEvents.SERVER_STARTED`,
  `SERVER_STOPPING`, `ServerTickEvents.END_SERVER_TICK`, and `ClientLifecycleEvents.CLIENT_STOPPING`.
  Polling was removed.
- Periodic snapshots run inline on the server thread (the snapshot must see a flushed, stable
  save). This still stalls the tick for the copy's duration and the byte/file budgets bound the
  cost; what changed is the client-visible behaviour: probes and mutation attempts during that
  window get a signed busy receipt instead of a timeout, and a revoke is answered immediately. The
  copy duration on a real save remains live-unverified, and a request-driven `connect`/`backup`
  still holds its own HTTP worker for the copy (a separate worker and the un-gated revoke path stay
  available).
- Loopback HTTP additionally verifies the Bearer header before reading a request body and closes an
  authenticated body that stalls past a 2 s read deadline, so a local slow-body peer cannot park the
  worker pool; the emergency marker and revoke path do not depend on that pool.
- The guard's authority bounds are the strict ones: `world_id` <= 64, `secret` <= 256,
  `pvp_uuids` <= 16. Clients must align to these; the guard fails closed rather than accepting a
  wider value.
- Revocation durability is proven by the marker (`revoke` is a signed 200 only then; a marker
  failure is an unsigned 503 and the host must report an incomplete stop). The actor kick is
  best-effort and is not proven by the receipt; the host also stops the sidecar. A receipt may
  carry `port: 0` when LAN is closed, and clients should accept a revoke receipt independent of
  the current port.
- `MinecraftServer.isDedicated()` and `submit(Supplier)` are the actual Yarn 1.20.1+build.10 names;
  the earlier `isDedicatedServer()` / `submit(Callable)` assumptions did not compile.
- The guard binds only the configured `actor_uuid`. The full numeric QQ UID → player UUID owner
  binding stays in PawBot's host-side config/identity code; the Java guard does not read QQ data.
- Live-unverified: loading the jar in a real client, Open-to-LAN port discovery, a real
  `saveAll`-then-copy on a large save, Microsoft/offline auth, and a real restore drill. No real
  game, original world, credential, or runtime config was touched by these tests.
