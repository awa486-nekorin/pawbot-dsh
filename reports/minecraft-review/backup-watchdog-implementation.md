# F3 implementation note — snapshot-busy receipts, single backup schedule, S4 bounds

Date: 2026-11-01 (Asia/Shanghai)
Scope: `minecraft-guard/**`, `app/minecraft-sidecar/**`,
`app/pawbot/minecraft_guard.py`, `app/pawbot/minecraft_resident.py`, the corresponding Python
tests, `docs/MINECRAFT-PROTOCOL.md`, and the two package READMEs. No other file, config, world,
credential, persona, memory, browser, deployment, or live game was touched. All work is offline
and synthetic; all evidence is under `reports/minecraft-review/`.

This note implements `reports/minecraft-guard-review/backup-watchdog-fix-plan.md` **Design A**
(fix F3/F3b) and the S4 item, and confirms the F1/F2/F4/F5/F6 repairs that were already present in
the latest source. It is a code/test change, not a plan.

## 1. What was wrong (final-2026-11-01 §3 F3/F3b, S4)

- A snapshot (`saveAll` + synchronous tree copy) runs on the integrated-server thread. The Node
  watchdog asked for a permit every 250 ms with a 650 ms deadline and quarantined on any failure, so
  any copy longer than 650 ms disconnected a healthy bot; a short `backup_interval_sec` made it
  recur every interval.
- Node and Python each ran their own periodic backup timer in addition to the guard's, and a
  connect could copy the world two or three times.
- `HttpGuard` read the request body before authentication and had a fixed two-thread pool, so a
  local slow-body peer could hold both workers.

## 2. Guard (Java) — the core design

New `SnapshotGate` (package-private, dependency-free) with one monitor:

- `tryOpen()` is called on the **requesting** thread *before* anything is submitted to the server
  thread, so a request cannot pass the busy check and then queue behind a copy; at most one ticket
  exists.
- `remember(path, port, now)` is the **only** writer of the non-mutating cache, and it is called only
  after the same live server-thread attestation a normal permit performs (integrated only, canonical
  save root equal to config, marker content, LAN port 1024–65535). No cache → `503`, never a guessed
  path/port.
- `commit(ticket, publish)` publishes the produced session and releases the gate in one critical
  section (publish-before-unbusy), rejects a superseded/aborted/revoked ticket, and refuses to
  publish a snapshot the caller did not receive in time.
- `revokeAll()` invalidates in-flight tickets and the cache; `shouldAbort()` is the escape predicate:
  a revoke always aborts, and a request without a ticket aborts as soon as a snapshot owns the gate.

`PermitService`:

- `handle()` checks the gate after capability checks and before any server-thread handoff. While a
  snapshot is active it answers **off-thread** with a signed busy receipt (or `503` without a live
  cache); `BACKUP`/`CONNECT` open a ticket first; `REVOKE` is never gated.
- Tick/request snapshots publish through `gate.commit`; a tick that finds the gate busy returns
  `null` and `GuardMod` skips **without advancing** its schedule.
- Revocation and marker refresh latch under `stateLock`, release it, then call `gate.revokeAll()`
  (lock order gate → stateLock in commit; never stateLock → gate), so no deadlock and no publish
  after a revoke.
- `signedBusy()` emits exactly the 13 normal fields plus `busy`, `retry_after_ms` (250) and
  `attested_at`, signed with the same `Hmac` over the exact bytes. `can_write`/`can_pvp` are false
  and `backup_id` is empty, so no client can accept it as a mutation permit.

`WorldView` gains a default `onServerThread(task, timeout, abortIf)` and a `BusyAborted` exception;
`IntegratedWorldAdapter` submits once and polls in ≤50 ms slices, cancelling the queued task when the
gate opens. `BackupSchedule` is a pure policy (60–1800 s bound, exact boundary, skip no-advance,
failure advances). `GuardMod` uses it as the **single** scheduler and primes the attestation cache
on the server thread at startup.

`HttpGuard` (S4): verifies the Bearer header **before** reading the body, closes an authenticated
body that stalls past a 2 s read deadline, and uses three workers so a snapshot wait cannot starve
revoke. The emergency file marker and the `revoke` action remain independent of the pool.

## 3. Node sidecar

- `GuardBusy`/`GuardUnavailable` extend `GuardError`. `permit()` verifies signature and
  context/time first; a busy body must have the exact 16-field shape, empty `backup_id`, both
  capabilities false, `retry_after_ms` 50–5000 and a plausible `attested_at`; observe returns the
  row, mutations throw `GuardBusy` before the backup-id/capability checks. Extra/private fields and
  forged values fail closed.
- `controller.js`: the 1800 s backup timer and `periodicBackup()` are gone. A busy checkpoint stops
  the active action, rejects the job with `-32008`, pauses admissions for the bounded
  `retry_after_ms` and schedules a resume; it never quarantines or replays. A busy watchdog does the
  same and keeps the connection. Connect retries busy within its own `timeout_sec` budget and now
  obtains **exactly one** `connect` permit immediately before the socket opens — the guard's
  `connect` action already performs the mandatory fresh `saveAll`+snapshot, so there is no extra
  `backup` request. A real transport failure/timeout/context change still quarantines; the 650 ms
  deadine and 250 ms cadence are unchanged.

## 4. Python host / resident

- `GuardBusy(GuardDenied)` carries `retry_after_ms`. `verify_attestation()` treats `busy` as
  optional (absent = false), enforces the exact 16-field shape and the non-mutating invariants,
  returns a busy observe and raises `GuardBusy` for connect/backup/write/pvp.
- `minecraft_resident.py`: `_backup_if_due()` and both call sites are removed; `backup_interval_min`
  stays as an advisory compatibility key. `_invoke_skill` catches `GuardBusy` first and returns
  `reason="guard_busy"` with a one-tick idle cooldown and **no** failure streak, project pause or
  sidecar call. The child's own snapshot-busy checkpoint arrives over JSON-RPC as `-32008`;
  `SidecarError` now preserves just that integer code, and the resident maps it to the same bounded
  pause instead of `call_failed`. `_run` treats a leaked busy as a bounded pause, never loop-fatal.
  `_connect`'s preflight is a cheap `observe`; the child's single `connect` permit is the mandatory
  snapshot, and the resident now also requires `backup_completed: true` in the child result. A real
  unavailable guard still latches (fail closed).

## 5. Single schedule / snapshot count per connect

- Periodic: only the guard ticks (`BackupSchedule`, `backup_interval_sec`).
- Connect: Node's one `connect` permit → the guard `CONNECT` build does one `saveAll` + one copy.
  Python asks no snapshot. Net: **one** snapshot per connect (previously up to 3–4).

## 6. Existing specialist repairs confirmed in the latest source (not re-implemented)

F1 junction-safe deletion (`BackupManager.deleteTreeQuietly`, `cleanupTemp`, `prune`), F2 durable
revoke marker + unsigned 503 on failure, F4 full-ancestor running-world scan, F5 `pvp` requires
write and `can_pvp = pvp_enabled && write_enabled`, F6 exact canonical path policy — all present,
with the existing junction/revoke/70-deep/pvp-without-write/case regressions still passing. Home,
farm and owner-human suggestion fixes were not touched.

## 7. Evidence (all UTF-8, synthetic temp data only)

| check | command | result | log |
| --- | --- | --- | --- |
| Guard clean build + remap + self-test | `gradle clean build --offline --no-daemon` (bundled Gradle 8.7 / JBR 21, `--release 17`) | `passed=63 failed=0`, `BUILD SUCCESSFUL`, `remapJar` executed | `backup-watchdog-guard-build.log` |
| Guard artifact | `get-FileHash` | `pawbot-guard-1.0.0.jar`, 70 856 bytes, SHA-256 `B373B470F6BDE30A01EEC5C9723340DF6FD81EA43459AA97552FD4AA29D7A82F` | same log / note |
| Node tests | `npm test` | 115 passed, 0 failed/cancelled/skipped (~4.8 s) | `backup-watchdog-sidecar-npm-test.log` |
| Node syntax | `node --check` on 2 entry points + 6 source modules | exit 0 | `backup-watchdog-sidecar-syntax.log` |
| Node audit | `npm audit --audit-level=moderate` | `found 0 vulnerabilities` | `backup-watchdog-sidecar-npm-audit.log` |
| Python focused (no browser/game) | six `tools.test_minecraft_*` modules | 185 tests, `OK` (~43 s): guard 42, resident 63, sidecar 20, policy 17, control 24, integration 19 | `backup-watchdog-python-minecraft.log` |
| Architecture baseline | `node scripts/architecture-baseline.mjs` | 19/19 PASS | (not changed by these edits) |

New Java regressions (12, 51→63): backup-schedule policy; slow snapshot answers busy off-thread;
queued request escapes a tick snapshot; no mutation permit during a copy; revoke during a copy
answers and never publishes; busy receipt exact/signed/non-mutating; tick skips without advancing;
one snapshot at a time; 60 s interval boundary; unauthenticated slow body answered before reading;
authenticated stalled body closed by the deadline; revoke reachable while a slow body holds a worker.

New Node regressions (9, 106→115): busy observe accepted, busy mutations paused; forged/extra-field
busy refused; busy checkpoint cancels without quarantine; watchdog busy keeps connection and stops
the action; real revoke after a busy window closes <1 s; connect retries busy; busy observe valid;
no backup timer/request; transport failure ≠ busy → quarantine.

New Python regressions: busy loopback receipt pauses mutations; busy unit contract and forged shapes;
busy observe valid; resident `guard_busy` pause with no failure streak; busy observe preflight; no
periodic `backup` request; connect requires `backup_completed`; unavailable guard still latches.

## 8. Residual gaps / honest limits

- The copy still stalls the integrated-server tick for its duration (Design B, an off-thread
  two-phase copy, remains deferred). Budgets bound it; real-save duration is **live-unverified**.
- Busy is not a lease: a permit already returned before the copy keeps its 5 s semantics; the next
  checkpoint is refused. This matches the plan's stated residual and is not a regression.
- The 2 s read deadline is implemented with `HttpExchange.close()` from a scheduler thread and is
  regression-tested on this host; behaviour on other JDK providers is not exhaustively verified.
- No live client load, Open-to-LAN port, real `saveAll`/copy timing, kick, auth, restore drill, or
  30–60 min observation was run. Do not describe any of this as live-verified.
- `reports/minecraft-review/repaired-2026-11-01.md` had **not** appeared while this work was being
  written (only the review's own logs under `reports/minecraft-review/repaired/`). This note owns
  the F3/F3b/S4 contract; any later confirmed same-contract finding must be applied on top.
- `app/pawbot/minecraft_sidecar.py` received the minimal IPC change the task requires: `SidecarError`
  carries the child's integer JSON-RPC error code (never its text), so `-32008` reaches the resident
  as a bounded pause. Its `connect_with_retry` docstring was corrected to match the one-connect-per
  snapshot contract; no other behaviour changed.
