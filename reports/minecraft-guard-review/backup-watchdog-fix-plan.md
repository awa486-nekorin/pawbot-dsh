# Fix plan — Node 650 ms watchdog vs. server-thread snapshot (>650 ms)

Date: 2026-11-01 (Asia/Shanghai)
Author: delegated review/planning agent
Source of the finding: `reports/minecraft-guard-review/final-2026-11-01.md` §3 F3 / F3b (lines 95–119), §6 (line 207), §8 item 3.
Status: **plan only.** No source, test, config, or doc file was modified. Only this report was written.
Read-only inputs: `minecraft-guard/src/main/java/dev/pawbot/guard/{PermitService,GuardMod,BackupManager,IntegratedWorldAdapter,WorldView,GuardConfig,HttpGuard}.java`, `minecraft-guard/src/test/java/dev/pawbot/guard/{GuardSelfTest,FakeWorld,Fixtures}.java`, `app/minecraft-sidecar/src/{controller,guard,adapter,main}.js` and its tests, `app/pawbot/{minecraft_guard,minecraft_resident,minecraft_sidecar}.py`, the Python MC tests, and `minecraft-guard/README.md`.
Line numbers are as read on 2026-11-01 and will drift; treat them as anchors, not contracts.
Not done here (and not required for this plan): live game, real world, private data, credentials, builds. No `gradle`/`node`/`python` command was run.

> **Coordination constraint:** guard/Node/host/resident files are being edited by other writers for other review findings (F1/F2/F4/F5/F6). Every item below is scoped to avoid their edits (see §8.2 for the exact overlap points and merge order).

---

## 1. The problem, stated precisely

Three facts, all sourced, produce the defect:

1. The permit decision point does world attestation **on the integrated-server thread** (`PermitService.java:287` `world.onServerThread(...)`; adapter at `IntegratedWorldAdapter.java:77-88`).
2. A snapshot (`BACKUP`/`CONNECT`, and the periodic tick job) runs `saveAll()` + a full synchronous tree copy **inside that same server-thread task** (`PermitService.java:333-343` → `BackupManager.java:70-117`; periodic path `GuardMod.java:132-157` → `PermitService.java:161-192`). The copy is deliberately on the server thread so the snapshot is one stable save (`BackupManager.java:101-107`, `:228-235`).
3. The Node sidecar's watchdog asks the guard for a permit every **250 ms** with a **650 ms** deadline (`controller.js:50`, `:63`) and quarantines on *any* failure (`:114-119`, `:127-134`). The permit is answered only after the server-thread task runs. So when a snapshot is in progress, the watchdog's request sits in the server-thread queue and the 650 ms deadline expires → `GuardError` → `quarantine('guard_unavailable')` → the bot is disconnected and the sidecar stops, on a healthy world.

Consequences that make this recurring rather than rare:

- The Java guard snapshots on its own tick every `backup_interval_sec` (60–1800, default 1800; `GuardConfig.java:104-108`, `GuardMod.java:142-153`). Any interval short enough to be useful (e.g. 60 s, explicitly allowed) means a quarantine trigger every minute on a slow copy.
- The Node sidecar *also* snapshots every 1 800 000 ms (`controller.js:51`) and the Python resident snapshots independently (`minecraft_resident.py:1572-1576`, called from `:1564` and `:2046`), so three schedules overlap; F3b already notes the 60 s vs 1800 s mismatch that turns the write path into `403 backup stale` → also quarantined.
- Therefore "increase the timeout" is not a fix: it only lengthens the window in which a *real* revoke, a wedged server thread, or a closed LAN goes unnoticed, and F3's own report says so.
- "Ignore timeouts / suppress quarantine" is not a fix either: it converts a fail-closed guard into one that keeps a bot connected after the guard is gone.

**What is genuinely wrong is not the copy duration; it is that a non-mutating liveness question is forced through the same single-threaded queue as the copy, and that the client cannot distinguish "the guard is busy doing exactly what it should" from "the guard is gone/revoked".**

---

## 2. Requirements distilled

| id | requirement | where it comes from |
| --- | --- | --- |
| R1 | A real revoke is still observed by the sidecar in **≤ 1 s**, including during a copy. | user instruction; existing test `sidecar.test.js:103-112` |
| R2 | **No valid write/pvp permit is issued while a copy runs** (structural: signed 200 with `can_write`/`can_pvp` true and a non-empty `backup_id`). | user instruction; `PermitService.java:333-361` |
| R3 | The watchdog **never queues on the integrated-server thread** while a snapshot owns it. | user instruction ("no server queue waiting for watchdog") |
| R4 | Do not fix this by raising the 650 ms deadline, swallowing failures, or weakening quarantine. | user instruction; F3 §Fix |
| R5 | Actual world path, LAN port, world_id/marker and session freshness are still attested live for anything that can mutate; a cache may only ever answer read-only questions. | user instruction ("protect actual world path/port freshness … without trust in stale cache") |
| R6 | One authoritative periodic-backup schedule, owned by the guard. | user instruction; F3b |
| R7 | Minimal, implementable by agents who are concurrently editing other parts of these files; backward compatible on the wire. | user instruction; AGENTS.md compat contract |

---

## 3. Two feasible designs

### 3.1 Design A — non-blocking busy gate around an unchanged server-thread snapshot

Keep `saveAll` + copy on the integrated-server thread (no new Minecraft API, no change to what a snapshot *is*). Change only the request-arbitration around it:

- A **snapshot gate** with tickets. A request that needs a snapshot (`BACKUP`, `CONNECT`) atomically opens the gate *before* submitting to the server thread; the tick-driven snapshot opens it inline on the server thread. While the gate is open:
  - `observe` → answered **off-thread** as a signed *busy observe*: a genuinely non-mutating permit that still proves guard liveness + non-revocation.
  - `write`, `pvp`, `connect`, `backup` → answered **off-thread** as a signed *busy receipt* that is structurally incapable of acting as a mutation permit (`can_write:false`, `can_pvp:false`, `backup_id:""`).
  - `revoke` → unchanged fast path (latch before any server-thread work), no longer blocks for 10 s behind the copy.
- A **wait-escape**: a request that passed the gate check *before* the gate opened and is now queued on the server thread is released within ~50 ms with a busy receipt instead of waiting for the copy.
- **Generation-latched publication**: the new session is published only if (a) the caller received the build result in time, (b) no newer snapshot superseded it, (c) the guard is not revoked. Publication happens before the gate closes, so a write never reads the pre-copy session after the copy.
- Clients **classify** the busy receipt as pause/cancel-and-retry (never quarantine) and keep the 250 ms / 650 ms watchdog unchanged; `409` stays terminal; transport failures get bounded retries and then still quarantine.

Source anchors: `PermitService.java:199-298` (handle), `:330-361` (build), `:386-404` (revoke), `:406-439` (signing); `WorldView.java:22-23`; `IntegratedWorldAdapter.java:69-104`; `GuardMod.java:132-157`.

Cost: the copy still stalls the integrated server for its duration (game stutter; a very large world could approach Minecraft's own client timeouts). That is a **performance** gap, not an authorization gap, and it already exists today.

### 3.2 Design B — quiesced two-phase off-thread copy

- Server thread: attest world/port/marker, `saveAll()`, then freeze world persistence (autosave off / save-off equivalent), release the thread.
- Background executor: copy the tree without holding the server thread; re-enable autosave afterwards.
- All the client protocol from A is still needed for the save phase and for classification: B does not remove one line of the Node/Python work.
- New risk: `public` world writes that do not go through autosave (chunk unloads, `playerdata` on join/leave, out-of-band editors) still touch the directory during the off-thread copy. The current safety check would then fail — `"world changed during backup"` (`BackupManager.java:105-107`, `:228-235`) — so either that invariant is weakened (a deliberate downgrade of a documented property, README §1) or the copy fails on active worlds and the freshness window expires, producing **permanent `403 backup stale` write denial**. Either outcome needs its own threat review.
- Also required: a new version-specific Minecraft accessor (autosave freeze) on `WorldView`/`IntegratedWorldAdapter` (`WorldView.java:11-29`, `IntegratedWorldAdapter.java:34-133`), an off-thread mode in `BackupManager` (thread ownership, cancellation, disk-budget races), and a new lifecycle question (what happens if the world stops mid-copy).

### 3.3 Comparison and decision

| criterion | A | B |
| --- | --- | --- |
| preserves single-stable-snapshot invariant | yes, unchanged | no (weakened or fails) |
| new Minecraft-version-specific API | none | autosave/save-off accessor |
| watchdog never queues on server thread | yes (gate + escape) | yes (but only if A's protocol is built too) |
| revoke ≤ 1 s during copy | yes (latch is off-thread, observe answered off-thread) | yes |
| no mutation permit during copy | yes (structural) | yes (longer deny window) |
| diff size / review surface | small, one service + adapter + 3 clients | large, snapshot semantics + new thread ownership |
| fixes tick stall | no (documented residual) | yes |
| test surface | gate state machine + clients | above + filesystem/threading/lifecycle |

Decision (advisory model `perplexity/pplx-decider-v1.2`, confidence 0.752): **"A now, B deferred to a later perf change"** (A 15.6 %, B 1.0 %, A-now/B-later 83.5 %). Same run: P(watchdog detects a real revoke ≤ 1 s under A) = **99.5 %**; P(no valid mutation permit during the copy under A) = **99.6 %**; residual safety risk of A once the §7 tests pass = **low** (0.19/3).

Adopted: **Design A**, with Design B recorded as the optional later performance change (§9, item 1).

---

## 4. Chosen design — mechanics

### 4.1 Gate, tickets, and the one state machine

New type in `PermitService` (nested `static final class SnapshotGate`; a separate file is also fine — keep it package-private and dependency-free so the JDK-only suite can exercise it):

```
Ticket  { long generation; boolean closed; }
Cache   { String canonicalSavePath; int port; long attestedAtSeconds; String publishedSessionId; }
state   { boolean active; long generation; Ticket owner; Cache cache; BackupRef session; boolean revoked; }
```

Transitions (all under one monitor, so "is it busy" and "who owns it" cannot disagree):

| transition | caller thread | effect |
| --- | --- | --- |
| `tryOpen()` | HTTP thread (`BACKUP`/`CONNECT` before submit) or server thread (tick job) | if `active` → `null`; else `active=true`, `generation++`, new `Ticket` |
| `refine(ticket, realRoot, port, now)` | server thread, first action of `build()` | fills `cache.canonicalSavePath/port/attestedAt` from the live attestation |
| `commit(ticket, backupId, atSeconds)` | whoever received the build result in time | if `ticket.generation == generation && !revoked` → publish `session = BackupRef(backupId, atSeconds, generation)`; then `active=false`, `ticket.closed=true`. **Publish-then-unbusy in the same critical section.** |
| `abort(ticket)` / `abortIfOpen(ticket)` | HTTP thread on timeout/exception; server thread on failure | `active=false`, `closed=true`, **no publish** |
| `revokeAll()` | HTTP thread (`revoke`) | `revoked=true`, `generation++`, `session=NONE`; an in-flight copy's later `commit` fails the generation test |
| `activeOtherThan(ticket)` | HTTP thread (escape predicate) | `active && owner != ticket` |
| `busyCache()` | HTTP thread | `active ? cache : null` |

Why the gate is opened **before** submitting to the server thread: it removes the "request passed the check, then the tick started the copy, then the request waited for the copy" race at its source. The escape (§4.2) covers the remaining microsecond window for requests that do not open a ticket.

### 4.2 Request paths

Insert immediately after the capability/target checks (`PermitService.java:263-278`) and before the server-thread handoff (`:283-297`):

```
if (action != REVOKE) {
    Cache busy = gate.busyCache();
    if (busy != null) return busyReceipt(nonce, action, target, busy);      // off-thread, no queue
}
Ticket ticket = (action == BACKUP || action == CONNECT) ? gate.tryOpen() : null;
if ((action == BACKUP || action == CONNECT) && ticket == null) return busyReceipt(...);
try {
    result = world.onServerThread(() -> build(nonce, action, target, now, ticket),
                                  timeout,
                                  () -> gate.activeOtherThan(ticket));      // escape predicate
    if (revoked) return error(409, "revoked");                              // late revoke re-check
    if (ticket != null) {
        if (!gate.commit(ticket, result.backupId(), now)) return busyReceipt(...);
        ticket = null;
    }
    return result.response;
} catch (BusyAborted e) {
    return busyReceipt(nonce, action, target, gate.busyCache());
} catch (TimeoutException | ExecutionException e) {
    return error(503, "unavailable");
} finally {
    if (ticket != null) gate.abortIfOpen(ticket);
}
```

| action | gate closed | gate open |
| --- | --- | --- |
| `observe` | server-thread attestation as today, signed 200 | **signed 200 busy observe** (off-thread) |
| `write` / `pvp` | server-thread attestation + freshness check, signed 200 | **signed 200 busy receipt**, no server-thread work |
| `connect` / `backup` | opens ticket → save+copy on server thread → publish → signed 200 | **signed 200 busy receipt**, no server-thread work |
| `revoke` | latch + marker + `revokeAll()`, then non-blocking kick | same — never gated |
| any, waiting on a server thread that just became busy (tick snapshot) | escape → **signed 200 busy receipt** within ~50 ms | — |

`build()` gains the ticket and the refinement:

```
entry: if (ticket != null && gate.isClosed(ticket)) return reject(503, "abandoned");   // skip a copy nobody waits for
       ... existing isIntegrated/savePath/marker/lanPort checks (unchanged) ...
       after realRoot and port are known:  gate.refine(ticket, realRoot, port, now);
BACKUP/CONNECT: world.saveAll(); id = backups.backup(realRoot); return new BuildResult(signed(...id...), id);
WRITE/PVP:     unchanged freshness check (session.id / age ≤ backupIntervalSec)
```

Publication moves out of `handle()` (`PermitService.java:290-292`) into `gate.commit(...)`, which is the only writer of `sessionBackup`. `periodicBackupOnServerThread` becomes `tryOpen → refine → saveAll → backup → commit → (finally) abortIfOpen`; on `commit` failure it returns `""` and publishes nothing.

### 4.3 Publication ordering and the generation latch

Three invariants, all tested:

1. **Publish before unbusy** (same critical section in `commit`) → a write that runs immediately after the copy reads the *new* session, never the pre-copy one. This closes F3's "queued write reads the old session after the copy".
2. **A snapshot that the caller did not receive in time never publishes.** The build result carries the id; `commit` is called only on the in-time path; `abortIfOpen` clears the gate otherwise. This preserves the existing regression `GuardSelfTest.java:367-378` (`timeoutDoesNotPublish`) unchanged. A late-completing task cannot leave the gate stuck open either: `abortIfOpen` in the `finally` and `isClosed(ticket)` at `build()` entry both cover it.
3. **Revocation invalidates the latch**: `revokeAll()` bumps the generation, so an in-flight copy's `commit` is refused. Test: start a copy, revoke during it, then delete the marker (simulating a human re-arm) and assert a `write` is still `403 backup required` — i.e. the copy did not publish.

### 4.4 Revoke immediacy (and its F2 boundary)

`revoke()` (`PermitService.java:386-404`) currently waits `NORMAL_TIMEOUT_MS = 10 s` on `world.onServerThread(...)` for the kick — during a copy, that is exactly 10 s of delay on the *only* path that must be instant, and with `HttpGuard`'s 2-thread pool (`HttpGuard.java:45-50`) it can also be starved by a concurrent `BACKUP`.

Change (small, does not touch F2's durability bug):

1. Marker write + in-memory latch + `gate.revokeAll()` — all before any server-thread involvement (as today), so every subsequent request is `409` within one round trip.
2. Try the kick with a **short** budget: `world.onServerThread(kick + lanPort, 300 ms)`.
3. On timeout, do not wait: submit the kick best-effort (`WorldView.executeOnServerThread`) and sign the receipt with `gate.cachedPort()` (or `0` if never attested, which is today's F2b behaviour and stays owned by the F2 fix).
4. Sign immediately after step 1–3. The existing test `GuardSelfTest.java:553-568` (live port in the receipt, actor eventually kicked, `awaitTrue`) still passes because the server thread is free in that test.

Ordering note for the F2 owner: the durable-marker check must stay *before* the success signature; our change only moves the kick off the critical path. If F2 later returns `503` on marker failure, the latch still happens (safe) — the two changes compose.

### 4.5 What the cache may and may not do

The cached fields are **only** `canonicalSavePath`, `port`, `attestedAt`, last published session id — captured on the server thread at `refine(...)`, i.e. after the same checks the live path does (`isIntegrated`, `realPathChecked(root).equals(config.savePath.toRealPath())`, marker content, `lanPort()` in 1024..65535; `PermitService.java:301-328`).

- It is used **only** for a busy `observe` and for the receipt's `save_path`/`port` fields. It can never satisfy `build()`'s WRITE/PVP path, which always re-attests on the server thread — that is the structural answer to "no trust in stale cache".
- Live-ness argument: while the gate is open because of a copy, the integrated-server thread is inside the copy, so the world cannot be stopped/unloaded/switched (server-thread lifecycle events) until it finishes; the save root therefore cannot change under a busy observe.
- Residual, stated honestly: the LAN port can in principle be toggled by an Open-to-LAN UI action that is not a server-thread task. A stale `port` in a busy observe is still non-mutating; the sidecar compares it with its pinned connect port and, if it differs, fails closed (`controller.js:66`), and if the world was re-published the bot's socket is dead and `adapter.onDisconnect → quarantine` (`controller.js:25`) already closes it.
- If the cache is empty (guard just started, nothing attested yet) the guard must **not** invent one: return `503 unavailable` (fail-closed) rather than a busy receipt with a guessed path/port. `GuardMod.onServerStarted` primes the cache on the server thread right after the service is constructed (`GuardMod.java:99-105`), so this is a non-event in practice.

### 4.6 One authoritative periodic schedule

- **Guard**: `onServerTick` stays the only periodic snapshot driver (`GuardMod.java:132-157`). Extract the policy into a small pure class `BackupSchedule` (interval, 1 s floor, last-attempt nanos, `due(now)`, `markAttempt(now)`), so the pure suite can test 60 s vs 1800 s and the no-overlap rule without Fabric.
  - If `gate` is already open (a request-driven snapshot owns it), **skip without advancing** the deadline — retry next tick. Today the deadline is advanced before the attempt (`:149-150`), which would cost a whole interval.
  - A failed attempt still advances the deadline (keep today's "no retry storm" rule).
  - `MIN_BACKUP_SPACING_NANOS` stays as a re-entry backstop.
- **Node**: delete `controller.js:51` and `periodicBackup()` (`:120-126`) entirely. `connect` keeps its mandatory backup+connect pair (`:36`, `:41`) — those are not periodic, they are part of establishing a session, and the protocol already requires them.
- **Python**: delete the `_backup_if_due()` calls at `minecraft_resident.py:1564` and `:2046` and the method at `:1572-1576`. Keep `_backup_interval_seconds()` (`:1316-1319`) and the `backup_interval_min` config key for compatibility, documented as advisory/no-op; `MAX_BACKUP_INTERVAL_MIN = 30` (`:75`) stays as a config bound. The pre-connect backup in `_connect` (`:1421-1422`) is **kept** in this change (it is part of connect, not the periodic schedule; removing it is a separate, larger test change — see §9, item 2).
- Result: exactly one scheduler; a configured 60 s interval now genuinely produces a snapshot per 60 s of server ticks, and no client timer can disagree with it.

---

## 5. Wire contract

### 5.1 Additive busy receipt (status 200, signed)

Request bytes and the action set are **unchanged** (`{nonce, actor_uuid, action, target_uuid}` exact keys, `PermitService.java:228-243`). No new action, no new route, no new env var, no new deadline. A `busy` receipt is a normal signed 200 whose body adds three fields:

```json
{"nonce":"<32hex>","world_id":"<id>","save_path":"<canonical real path>","host":"127.0.0.1",
 "port":<cached live LAN port>,"actor_uuid":"<uuid>","action":"write","target_uuid":"",
 "issued_at":<now>,"expires_at":<now+5>,"can_write":false,"can_pvp":false,"backup_id":"",
 "busy":true,"retry_after_ms":250,"attested_at":<unix seconds of the cached attestation>}
```

Normative invariants of a busy receipt (auditable in one place, `signedBusy`):

- `busy == true`, and the signature covers the exact bytes (same `Hmac`/`signed()` path, `PermitService.java:406-439`).
- `backup_id == ""`; `can_write == false`; `can_pvp == false`. **Structural non-mutability**: `guard.js:52` rejects an empty `backup_id` for `connect/backup/write/pvp`, and `minecraft_guard.py:150-151` rejects it for the same set. A busy receipt can therefore never be accepted as a mutation permit by any client version.
- `port`/`save_path` are the cached server-thread values (never a guess). If no cache exists → no busy receipt; return `503`.
- `retry_after_ms` ∈ [50, 5000]; `attested_at ≤ issued_at + 1`, `attested_at > 0`; clients accept `attested_at ≤ issued_at` without a lower bound (a long copy legitimately separates them) and **never** use it for authorization.
- `busy` is not a denial: `409`/`403` semantics, the nonce/replay rule (`PermitService.java:253-259`) and the "denials carry no signature" rule (`:65-80`) are unchanged.

Client rules:

- **Node** `guard.js`: `GuardBusy extends GuardError` (carries `retryAfterMs`), `GuardUnavailable extends GuardError` (transport/timeout/503). `permit()` fully verifies signature + the existing context/time fields first, then: if `busy === true` → validate the busy invariants → **return the row for `observe`** (a busy observe *is* a valid non-mutating observe) and **throw `GuardBusy` for `connect/backup/write/pvp`**, *before* the `backup_id` requirement (`:52`) and capability checks (`:53`). Transport failures/503 → `GuardUnavailable`.
- **Python** `minecraft_guard.py`: `GuardBusy(GuardDenied)`; `verify_attestation` treats `busy` as optional (absent = false) and validates it only when present → returns the row for `observe`, raises `GuardBusy` for `connect/backup/write/pvp`. This keeps every existing fixture valid (`test_minecraft_guard.py:210-222` uses bodies without `busy`).
- Both clients keep the **650 ms deadline unchanged**. The deadline is not the fix; the gate is.

### 5.2 Backwards compatibility matrix

| guard | sidecar / resident | behaviour |
| --- | --- | --- |
| old | old | current bug: quarantine during any copy >650 ms |
| old | new | no busy receipts ever emitted → new code paths are inert; 409/timeouts classified as today (bounded retry then quarantine). Safe. |
| new | old Node | busy observe passes old validation (observe needs no `backup_id`) → **the old sidecar stops quarantining during backups**; a busy *write* fails old `:52` → `GuardError` → quarantine, i.e. exactly today's outcome, no regression. |
| new | old Python | `verify_attestation` raises `GuardDenied` for busy mutations (`guard_backup_required`) → the resident records `call_failed` and retries on its next tick. Busy observe is accepted. `_backup_if_due` (old resident) can raise out of `tick()` → upgrade the host with the guard (documented). |
| new | new | intended behaviour |

Deployment order: **guard + sidecar + resident together**; the guard change alone is not a regression, but it does not fix the bug for old clients either.

---

## 6. Implementation scope, by file

### 6.1 Guard (Java) — the core

| file | change | anchor |
| --- | --- | --- |
| `PermitService.java` | add `SnapshotGate`/`Ticket`/`Cache`; route `handle()` through gate + escape + busy receipt; add late-revoke re-check; move publication into `commit`; `build(nonce, action, target, now, ticket)` with `abandoned` skip + `refine`; `periodicBackupOnServerThread` via gate; `revoke()` non-blocking kick; `signedBusy(...)`; keep `signed()` body layout unchanged for normal permits | `:199-298`, `:301-361`, `:161-192`, `:386-404`, `:406-439` |
| `WorldView.java` | add `default <T> T onServerThread(Callable<T>, long, BooleanSupplier abortIf)` (default delegates to the 2-arg form) and `default void executeOnServerThread(Runnable)`; add `final class BusyAborted extends Exception` | `:22-23` |
| `IntegratedWorldAdapter.java` | implement the escape overload: submit once, then poll `future.get(50 ms)` slices, `if (abortIf.getAsBoolean()) { future.cancel(false); throw new BusyAborted(); }`; implement `executeOnServerThread` as `server.submit(...)` fire-and-forget (no wait) | `:69-104` |
| `GuardMod.java` | `onServerTick` uses new `BackupSchedule`; skip (without advancing) when the gate is busy; prime the attestation cache on the server thread at start; keep the inline snapshot and the static logging | `:132-157`, `:99-121`, `:115-117` |
| `BackupSchedule.java` (new) | pure policy: interval, 1 s floor, `due(now)`, `markAttempt(now)`, `skip()` (no advance) | new |
| `HttpGuard.java` | no functional change. Optionally 2→3 workers as headroom; **not required** now that at most one thread can hold a snapshot ticket | `:45-50` |
| `README.md` | §1: add the busy receipt + busy-observe semantics, the three publication/ordering invariants, single-schedule statement; §6: replace "periodic snapshots run inline … can stall a tick" with the accurate statement (unchanged stall; client-visible behaviour now defined) | `:34-46`, `:163-176` |

Hard constraints for the guard edit: keep `isRevoked()`/`periodicBackupOnServerThread` signatures (used by tests at `GuardSelfTest.java:411-424`, `:567`); keep the normal-permit field set exactly as asserted at `:185-203`.

### 6.2 Node sidecar

| file | change |
| --- | --- |
| `guard.js` | `GuardBusy`/`GuardUnavailable`; busy parsing after signature+context verification; `observe` returns busy rows; mutations throw; transport/timeout/503 → `GuardUnavailable`. Keep the action whitelist (`:36`) and every existing field check. |
| `controller.js` | delete `:51` + `periodicBackup()`; add `pausedUntil` and `pause(retryAfterMs)`; `checkPermit` propagates the two new errors; `pump` on `GuardBusy` → `adapter.stop()`, abort the job, reject `-32008`, no quarantine; on `GuardError` → quarantine as today; `skill()` refuses while paused; `watchGuard` on busy → pause + stop the active action, no quarantine; `connect` retries `GuardBusy` within its `timeout_sec` budget; keep the 650 ms deadline and the 250 ms cadence. |
| `main.js` | add `'-32008': 'Guard busy'` to the static error map (`:9`); `instanceof GuardError` still maps subclasses to `-32002` (`:38`). |
| `README.md` | document the pause/retry contract and the single-schedule statement. |
| `test/helpers.js` | `syntheticGuard(patch)` already supports injecting a busy body; add a `busyBody()` helper producing `{busy:true, backup_id:'', can_write:false, can_pvp:false, retry_after_ms:250, attested_at:...}`. |

### 6.3 Python host / resident

| file | change |
| --- | --- |
| `app/pawbot/minecraft_guard.py` | `class GuardBusy(GuardDenied)`; `verify_attestation` optional `busy` handling (return for observe, raise for mutations); `_request` maps `503` to a retryable class (optional, `GuardDenied("guard_unavailable")` is already retried by the resident). |
| `app/pawbot/minecraft_resident.py` | remove `_backup_if_due()` calls (`:1564`, `:2046`) and the method (`:1572-1576`); catch `GuardBusy` in `_execute` (`:2046-2074`) **before** `GuardDenied` → `{"executed": False, "ok": False, "reason": "guard_busy"}`, set `self._idle_cooldown = 1`, do **not** call `_record_failure`; catch `GuardBusy` in `_connect`'s `prepare()` (`:1416-1429`) and sleep `min(retry_after_ms, 1000) ms` within the existing attempt budget; keep `_backup_interval_seconds`/config key, documented as advisory. |
| `app/tools/test_minecraft_resident.py` | update `test_periodic_backup_runs_before_observe_when_due` (`:445-451`) → assert the resident issues **no** `backup` request on a due tick; add the GuardBusy cases (§7). `test_start_backup_precedes_connect` (`:393-397`) and `test_reconnect…` (`:867-870`) stay valid because the pre-connect backup is kept. |

### 6.4 Protocol / docs

- No new env var: the sidecar allowlist (`app/pawbot/minecraft_sidecar.py:40-42`) is unchanged.
- No new config key; `backup_interval_sec` (Java, 60–1800) is the only schedule authority; `backup_interval_min` (Python) becomes advisory.
- Guard README §1 is the normative protocol text; add the busy receipt field table and the "structural non-mutability" sentence.

---

## 7. Test plan (mapped to the requested scenarios)

### 7.1 Java self-test (`minecraft-guard/src/test/java/dev/pawbot/guard/`)

New fixture `ThreadedFakeWorld` (real single-thread executor implementing both `onServerThread` overloads + the keep-alive gates already present in `FakeWorld.java:22-27`) — needed because `FakeWorld` runs the task on the calling thread and cannot model "the server thread is blocked while another thread calls handle".

| test | scenario | assertions |
| --- | --- | --- |
| `slowSnapshotServesBusyOffThread` | copy > 650 ms (gate on `saveAll`) | `observe` → 200 in <100 ms, `busy:true`, `can_write:false`, `backup_id:""`, valid signature; `write`/`pvp`/`connect`/`backup` → busy receipts in <100 ms; **no** `onServerThread` call for any of them (counted by the fake); after release, `write` → 200 with the **new** `backup_id` (publish-before-unbusy) |
| `escapeReleasesQueuedObserve` | request accepted before the gate opened, server thread busy | response is a busy receipt within ~300 ms, not a 10 s wait; the queued task's result is discarded |
| `noMutationPermitDuringCopy` | 2 threads × 100 `write`/`pvp` during a blocked copy | every response is either busy (200, empty `backup_id`, both capabilities false) or 503; zero responses that are a 200 with a non-empty `backup_id` **and** `can_write` true |
| `concurrentRevokeDuringCopy` | revoke while the copy holds the server thread | revoke returns a signed 200 in <300 ms with the live/cached port; next `observe` → 409; the gate's `commit` is refused (assert via `sessionBackupId(...)` reflection helper at `:149-160`); after deleting the marker (re-arm) a `write` is still `403 backup required` |
| `busyReceiptContract` | field set + bounds | busy field set is exactly the normal 13 + `busy`/`retry_after_ms`/`attested_at`; `retry_after_ms` in range; signature over exact bytes; normal permits still have exactly 13 fields (`:185-203` unchanged) |
| `gateDoesNotPublishOnTimeout` | existing `timeoutDoesNotPublish` (`:367-378`) | must pass unchanged |
| `periodicBackupSkipsWithoutAdvancing` | gate busy at tick | `periodicBackupOnServerThread` returns `""`, publishes nothing, and `BackupSchedule` still reports `due` after `skip()` |
| `backupInterval60` | `backup_interval_sec=60` | write at `t+60` accepted, `t+61` → `403 backup stale`; default 1800 boundary test unchanged (`:410-430`) |
| `singleSnapshotAtATime` | two concurrent `BACKUP` calls | exactly one copy (backup-root directory count), the other gets a busy receipt, never two overlapping copies |

### 7.2 Node (`app/minecraft-sidecar/test/`)

| test | assertions |
| --- | --- |
| busy snapshot pauses instead of quarantining | skill checkpoint gets a busy write receipt → job rejects `-32008`, `adapter.stopCount` incremented, `controller.quarantined === false`, `adapter.connected === true`; a later non-busy skill succeeds |
| watchdog busy keeps the connection | busy observe for >1 s → no quarantine, watchdog keeps polling |
| revoke during busy still closes <1 s | busy responses, then a `world_id` mismatch / 409 → quarantine within 1 s (extends `:103-112`) |
| busy observe is a valid observe | `controller.observe()` returns adapter data during busy |
| connect retries through a busy window | first 2 attempts busy, third succeeds within `timeout_sec` |
| no periodic backup timer | `controller.backupTimer === undefined` after connect; guard calls after connect contain only the connect pair + watchdogs |
| transport failure ≠ busy | a rejected transport → `GuardUnavailable` → bounded retry → quarantine (fail-closed kept) |

### 7.3 Python (`app/tools/`)

- `test_minecraft_guard.py`: busy body with absent `busy` (all existing fixtures) still accepted; `write`/`connect`/`backup`/`pvp` busy → `GuardBusy`; `observe` busy → returned row; forged busy body (bad signature / `can_write:true` / non-empty `backup_id`) → denied.
- `test_minecraft_resident.py`: replaced `:445` test (no resident backup request); `_execute` on `GuardBusy` → `reason == "guard_busy"`, failure counter unchanged, no sidecar skill queued; `_connect` retries a busy `connect` permit and succeeds; no `backup` action emitted by a long simulated tick loop (3600 s).
- Cross-language contract fixture: the same literal busy JSON (with a fixed secret) checked in all three suites, so a field rename fails a test in every language at once.

### 7.4 Interval / overlap (the "60 s vs 1800 timer" ask)

- Java: `backupInterval60` above + `BackupSchedule` unit cases (due at exactly the interval, not before; skip does not advance; failure advances).
- Node: no timer exists (assertion above).
- Python: no periodic backup request (assertion above).
- Composition: one test that simulates 600 s of ticks at a 60 s interval with a 3 s copy and asserts **one snapshot per 60 s (≈10, first at t=60), max concurrency 1, no skipped interval** — i.e. one authoritative schedule with no overlap and no double copy.

---

## 8. Rollout, coordination, and what this does not fix

### 8.1 Suggested commit sequence (each independently reviewable, each green)

1. Guard: `SnapshotGate` + busy receipts + escape overload + `WorldView`/adapter additions (+ self-tests). No behaviour change yet for old clients except no-quarantine-during-backup.
2. Guard: publication/staging rework in `build()`/`handle()`/`revoke()`, generation latch, non-blocking revoke (+ tests).
3. Guard: `BackupSchedule` + tick consolidation (+ tests).
4. Node: busy/unavailable classes, pause semantics, timer removal, `-32008` (+ tests).
5. Python: `GuardBusy`, resident changes, test updates.
6. Docs: guard README protocol section, sidecar README.

### 8.2 Working with the concurrent writers

- `PermitService.java` is touched by the F2 owner (revoke durability, `:386-404`) and likely by F5 (`can_pvp`/write coupling, `:266-278`, `:430-431`). Land our gate/commit code **around** their hunks: our changes are `handle()` insertion before `:283`, a new `build()` parameter, a new `revoke()` tail, and new private types. Do not renumber or reformat their regions.
- `BackupManager.java` is the F1 owner's file (junction-safe deletion, `:120-154`, `:266-284`). This plan requires **no** change there; do not touch it.
- `IntegratedWorldAdapter.java` / `WorldView.java` / `GuardMod.java` are not implicated by F1/F4/F5/F6 except F1's `GuardMod.java:94` recount of `cleanupTemp`; our `GuardMod` edit is limited to `:132-157` and `:99-121`.
- `minecraft_guard.py` is the F6 owner's file (path case policy, `:70`, `:138-140`); our edit is confined to `verify_attestation`'s capability section (`:145-156`) and a new exception class. Coordinate the field-order of the busy checks with F6's canonical-path decision.
- `controller.js` / `guard.js`: no other review finding targets them, but confirm with the other writers before touching the shared error mapping in `main.js:9` and the `GuardError` catch in `main.js:38`.

### 8.3 Explicitly not fixed here

- **F2/F2b** — revoke receipt durability and port-0 receipts stay with their owner. Our revoke change removes the *copy-induced 10 s delay* only; if F2 changes the failure status to `503`, our callers already treat `503` as retryable.
- **F1** (junction deletion), **F4** (64-ancestor restore), **F5** (pvp-without-write), **F6** (case path) — untouched.
- The copy still stalls the integrated server (see §9, item 1).

---

## 9. Residual gaps

1. **Tick stall remains by design.** A multi-GB copy blocks the integrated server for its duration; the game stutters and, on a sufficiently slow disk, Minecraft's own client keep-alives could time out. Design A makes *authorization* correct during that window but does not make the world responsive. This is the main reason to keep Design B as a follow-up.
2. **Pre-connect snapshot duplication** (`minecraft_resident.py:1421-1423` asks `backup` then `connect`, and the sidecar's `connect` asks `backup` then `connect` again at `controller.js:36,41`) causes up to 3 full copies per connect. Not part of this fix; a candidate for a later "coalesce a snapshot younger than N seconds" change, which needs its own freshness review.
3. **A non-copy slow server thread** (vanilla autosave, chunk I/O) can still exceed 650 ms for a mutation checkpoint; the bounded-retry classification turns it into a ~1 s pause and then a fail-closed quarantine. Acceptable, and strictly better than today's instant quarantine.
4. **Cached LAN port** in a busy observe can be a few seconds stale if Open-to-LAN is toggled by a non-server-thread UI path. Non-mutating only; the failing closed paths are `controller.js:66` and the disconnect handler.
5. **Busy is not a lease**: a client that has a valid write permit issued *before* the copy started may still hold it for its 5 s lifetime. The 250 ms watchdog + the gate make the next checkpoint refuse, but the permit itself is not retroactively revoked — same property as today (permits are point-in-time attestations, not locks). If the reviewer wants strict "zero mutation during copy", the host must also stop the bot's action at the first busy (which §6.2 does) — a packet already in flight is not preventable without a server-side deny, which this architecture does not have.
6. `HttpGuard` still reads bodies before authentication (S4) and has a small fixed pool; the gate means only one thread can be stuck in a snapshot, so revoke has a worker, but the S4 DoS remains its own item.

---

## 10. Bounded task list (done criteria)

1. `SnapshotGate` + `Ticket` + busy receipt + tests `slowSnapshotServesBusyOffThread`, `busyReceiptContract` — **done when** a blocked copy yields busy answers in <100 ms and the normal-permit field set is unchanged.
2. `WorldView`/`IntegratedWorldAdapter` escape overload + `escapeReleasesQueuedObserve` — **done when** a request queued behind a just-started copy returns busy in <300 ms.
3. `build()`/`handle()` ticket routing, publication move, late-revoke re-check + `noMutationPermitDuringCopy`, `gateDoesNotPublishOnTimeout` — **done when** no 200 mutation permit exists during a copy and the old `timeoutDoesNotPublish` passes untouched.
4. Non-blocking `revoke()` + `concurrentRevokeDuringCopy` — **done when** revoke answers <300 ms during a copy, the copy never publishes, and `GuardSelfTest:553-568` passes.
5. `BackupSchedule` + `GuardMod` tick consolidation + `periodicBackupSkipsWithoutAdvancing`, `backupInterval60`, `singleSnapshotAtATime` — **done when** 60 s produces 60 s snapshots with max concurrency 1.
6. Node busy/unavailable + pause semantics + timer removal + tests — **done when** a busy window never quarantines and a revoke still closes <1 s.
7. Python `GuardBusy` + resident changes + test updates — **done when** the resident emits no periodic `backup` request and a busy write yields `guard_busy` without a failure streak.
8. Docs (guard README protocol section, sidecar README) + the cross-language busy fixture — **done when** all three suites share the same busy fixture and the README states the publication/ordering invariants.
