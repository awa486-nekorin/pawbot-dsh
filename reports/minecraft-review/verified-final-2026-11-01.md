# Minecraft independent verification — final snapshot (2026-11-01)

**Verdict: NOT READY (offline).** The final offline matrix is green, and the original M1–M5 / S1–S6
findings, the guard specialist F1–F6 findings, and the snapshot-busy gate are independently confirmed
fixed or working. Two required-behavior gaps remain reproducible on the final source: **semantic habits
are never formed**, and the farm **“no plantable soil → shelter” fallback is unreachable**. The protocol
§4 text also lags the final core. This is not a hardware blocker; it is a source/acceptance gap.

- Reviewer role: independent verifier. No source, docs, runtime config, persona, memory, credential,
  world save, browser, deployment or service state was modified.
- Written by this review: this report, probes under `reports/minecraft-review/probe/`, and UTF-8 logs
  named `reports/minecraft-review/verified-*.log`. Nothing else in the repository was written.
- No live game, LAN, Microsoft login, real save, real credential, browser start, service restart or
  deployment was performed. Every world, guard endpoint and secret below is synthetic.
- No delegation, teammate wake-up, model-route change or global-setting change was made. The explicit
  route `opencode-go/deepseek-v4.1-flash` was untouched.

## 1. Snapshot and moving-tree note

Final source snapshot: **2026-11-01 23:39:29 local** (last Minecraft source write), verified stable for
180 s before the final runs and unchanged through them. Key files:

| File | Final mtime | Size |
| --- | --- | --- |
| `app/pawbot/core.py` | 23:35:59 | 57,684 B |
| `app/pawbot/minecraft_control.py` | 23:39:29 | 11,104 B |
| `app/pawbot/minecraft_resident.py` | 23:27:21 | 153,241 B |
| `app/pawbot/minecraft_policy.py` | 23:25:27 | 8,265 B |
| `app/tools/test_minecraft_resident.py` | 23:28:53 | 100,146 B |
| `app/tools/test_minecraft_control.py` | 23:36:06 | 29,675 B |
| `docs/MINECRAFT-PROTOCOL.md` | 23:36:40 | 24,116 B |

The writers’ handoff was **not actually frozen** when this review started. During the review the tree
changed at 23:25–23:39 (farm reach filter, canonical owner-human delegation, durable-revoke reporting,
protocol text). An intermediate focused run at 23:35 failed on a test that was being rewritten at the
same moment; that run is **not** used below. All final numbers are from the 23:39:29 snapshot, run
23:43–23:51. Earlier runs at 23:16 and 23:34 were on older snapshots and are discarded.

## 2. Evidence actually run (exact commands, counts, skips)

Python commands were run in `D:\paw-old\pawbot-dsh\app` unless noted.

| Area | Exact command | Final result | Log |
| --- | --- | --- | --- |
| Full Python discovery | `py -3.13 -m unittest discover -s tools -q` | **Ran 2177 tests in 183.549 s — OK (skipped=2)**, exit 0 | [verified-full-unittest.log](verified-full-unittest.log) |
| Focused Minecraft | `py -3.13 -m unittest tools.test_minecraft_guard tools.test_minecraft_policy tools.test_minecraft_sidecar tools.test_minecraft_control tools.test_minecraft_integration tools.test_minecraft_resident -q` | **Ran 199 tests in 43.443 s — OK**, exit 0 | [verified-focused-minecraft.log](verified-focused-minecraft.log) |
| Resident/lifecycle/HA | `py -3.13 -m unittest tools.test_minecraft_resident tools.test_behavior_acceptance tools.test_shutdown_admission tools.test_runtime_lifecycle tools.test_ha_shutdown_order tools.test_ha_core_integration tools.test_data_layout tools.test_longmem -q` | **Ran 190 tests in 46.686 s — OK**, exit 0 | [verified-lifecycle-ha.log](verified-lifecycle-ha.log) |
| Guard clean build + self-test | `gradle clean build --offline --no-daemon --console=plain` (bundled Gradle 8.7, JBR 21, `--release 17`) | **BUILD SUCCESSFUL in 18 s; selfTest `passed=63 failed=0`; `remapJar` executed** | [verified-guard-build.log](verified-guard-build.log) |
| Sidecar tests | `cd app/minecraft-sidecar; npm test` | **115 pass, 0 fail, 0 cancelled, 0 skipped; 4889 ms** | [verified-sidecar-npm-test.log](verified-sidecar-npm-test.log) |
| Sidecar syntax | `node --check` on `index.mjs`, `auth.mjs`, `src/{main,adapter,controller,guard,skills,auth}.js` | **8/8 exit 0** | [verified-sidecar-syntax.log](verified-sidecar-syntax.log) |
| Sidecar audit | `npm audit --offline --audit-level=moderate` | **0 vulnerabilities** | [verified-sidecar-audit.log](verified-sidecar-audit.log) |
| Architecture baseline | `node scripts/architecture-baseline.mjs` | **19 passed, 0 failed, 19 total** | [verified-architecture-baseline.log](verified-architecture-baseline.log) |
| Baseline contract tests | `node test/architecture-baseline.mjs` | **46 passed, 0 failed, 0 skipped** | [verified-architecture-baseline-tests.log](verified-architecture-baseline-tests.log) |
| Guard artifact | `Get-FileHash` + jar entry/bytecode check | **70,856 B; SHA-256 `B373B470F6BDE30A01EEC5C9723340DF6FD81EA43459AA97552FD4AA29D7A82F`; class major 61; remapped intermediary refs; `minecraft: 1.20.1`, `java >=17`** | [verified-guard-artifact.log](verified-guard-artifact.log) |

Per-module focused counts (loader `countTestCases`): guard 42, policy 21, sidecar 20, control 31,
integration 19, resident 66 = **199**. Lifecycle group: resident 66 + behavior 21 + shutdown 7 +
runtime 56 + HA shutdown 5 + HA core 5 + data layout 19 + longmem 11 = **190**.

The full run’s **2 skips are pre-existing and unrelated to Minecraft**:

1. `tools.test_memes.SeedDataTests.test_seeded_book_is_usable` — `还没跑 tools/seed_memes.py`.
2. `tools.test_ha_content.SnapshotTests.test_build_rejects_names_no_windows_peer_could_apply` — Windows
   strips trailing dots/spaces, so that filename cannot be constructed.

The focused and lifecycle groups reported 0 skips. The full run emitted pre-existing non-fatal warnings
(unawaited coroutines in unrelated proactive/mock tests); there were **0 failures**. No Chrome,
Minecraft client, game process or autostart remained after the runs; the one
`专用 Chrome 没在运行，自动启动` log line is the mocked `launch_chrome → false` path
(`test_pixiv_home.AutoLaunchTests`), and no `chrome.exe` was started.

## 3. Confirmed remaining findings (why the verdict is NOT READY)

### V1 — P2 functional: semantic habits are never formed; the habit branches are dead

**Where:** `app/pawbot/minecraft_resident.py:1198-1211` reads `habits["greet_known"]`,
`habits["tend_farm"]` and `habits["return_home"]`; `:2757` records every success as
`self._bump_habit(skill)`; `:3143-3155` stores only that skill name. Successful `home` / `farm` /
`follow` actions therefore create `home` / `farm` / `follow`, never the three semantic keys.

**Concrete reproduction** (`reports/minecraft-review/probe/habit_formation_probe.py`, exit 1):

```text
habit_keys= ['farm', 'follow', 'home']
return_home count= 0
tend_farm count= 0
greet_known count= 0
candidate_branches_reachable= False
```

Log: [verified-habit-formation.log](verified-habit-formation.log).

**Impact:** requirement v4 §4.7 / acceptance “她能形成习惯和仪式（检查农田、下雨回家、睡前写日记）”
cannot be demonstrated for learned habits. The weather-driven “rain → rest” candidate still works, and
the generic `habits` workspace item still shows any existing habit, but the three habit-specific
candidates never fire from behavior. The docs claim they do
(`docs/MINECRAFT-RESIDENT.md:348`, `docs/MINECRAFT-ACCEPTANCE.md:159`), and the test
`test_return_home_habit_reads_game_time_not_the_wall_clock` **manually seeds**
`habits["return_home"]` instead of forming it, so the green suite does not catch this.

**Fix direction:** map the success path to the semantic keys (`home` → `return_home`, `farm` →
`tend_farm`, `follow`/`chat` → `greet_known`), or make `_bump_habit` accept a semantic name from
`_success`; add a test that repeated real actions (no manual seed) cross the threshold and produce the
habit candidate.

### V2 — P2 functional: the farm “no plantable soil → shelter” fallback is unreachable

**Where:** `app/pawbot/minecraft_resident.py:2986-2992` (`_farm_soil`) returns
`{"x": fx, "y": fy-1, "z": fz, "block": "unknown"}` whenever `fy-1` is inside the world, even when the
observation contains no soil. `:2921-2926` (`_ensure_project`) falls back to `shelter` only when
`_project_steps` returns an empty list, so that branch is never reached for a normal y.

**Concrete reproduction** (`reports/minecraft-review/probe/farm_fallback_probe.py`, exit 1, only stone
under the player):

```text
kind = farm
steps = [{'skill': 'move', 'args': {'x': 2.0, 'y': 64.0, 'z': 0.0, 'radius': 1.0}},
         {'skill': 'farm', 'args': {'x': 0, 'y': 63, 'z': 0, 'seed': 'wheat_seeds'}},
         {'skill': 'eat', 'args': {}}]
FARM_ON_UNPLANTABLE_GROUND: project kept a farm step instead of falling back to shelter
```

Log: [verified-farm-fallback.log](verified-farm-fallback.log).

**Impact:** on stone/gravel/air ground the project starts as `farm` and emits coordinates the child
rejects with `not_soil`; after repeated failures the project pauses. The documented shelter fallback is
dead code in this case. The reach half of the old M4/farm finding is fixed (see §4); this fallback half
is not.

**Fix direction:** return `[]` when no observed soil matches; only emit the `y-1` fallback when that
exact block is observed as `farmland`/`dirt`/`grass_block`; add an `_ensure_project` test with barren
ground that asserts a shelter project (or no farm project).

### V3 — P3 documentation/contract drift (not a source defect)

`docs/MINECRAFT-PROTOCOL.md:312-319` still says a bare authenticated `409 revoked` “sets
`revoked: true` and can make `ok: true`”, and that a `503` followed by `409` can still report a stop a
restart forgets. The final `core.py:497-525` does the opposite: a bare 409 is
`(False, "already_revoked_unverified")`, `revoke_durable: false`, `error: already_revoked_unverified`,
`ok: false`; only a 409 after a previously recorded signed receipt for the same guard identity is
treated as durable. The offline probe now prints `no false success` and exits 0
([verified-revoke-durability.log](verified-revoke-durability.log)). The same section still references
`reports/minecraft-review/repaired-2026-11-01.md` §R1 as a current residual; that report is a 23:03
snapshot and is now stale for R1/R3/R4. `docs/MINECRAFT-ACCEPTANCE.md` (23:15) likewise still lists G3,
G4 and G8 as open although the final source fixes them. The doc owner should update; this review did not
edit docs.

## 4. Original findings: confirmed fixed / rejected

All items below were re-run on the final snapshot; the old probe failure is stated where a finding no
longer reproduces.

| Finding | Final status | Evidence |
| --- | --- | --- |
| **M1** permit after revoke | **Fixed** | `RevokeRaceStressProbe` 100 rounds: `connect200=66 connect409=34 connectOther=0 late200=0`; [verified-revoke-race-stress.log](verified-revoke-race-stress.log) |
| **M2** no-resident false success | **Fixed** | Original probe: `ok:false`, `revoked:false`, `error:guard_unavailable`, no `revoke_skipped`; [verified-emergency-stop-m2.log](verified-emergency-stop-m2.log) |
| **M3** home never seeded | **Fixed** | Contract-faithful child: `home` refused until `set_home`; `set_home` uses an observe permit; reconnect clears and reseeds; focused resident tests pass |
| **M4** farm uses player feet as soil | **Original defect fixed** | `_farm_soil` emits observed `y-1` soil and Node’s soil-at-`(x,y,z)` contract is tested; the reach half also passes now (`stand=-2.0,64.0,2.0`, distances 4.123 ≤ 4.5, [verified-farm-reach.log](verified-farm-reach.log)). The barren-ground fallback remains V2 |
| **M5** no owner-human identity | **Fixed** | Optional `minecraft.owner_human_uuid`; exact normalized UUID only; equal-to-actor rejected; `owner_mappings` never grants owner; resident now delegates to `minecraft_policy.owner_human_identity` (G8 closed) |
| **S1** revoke port 0 rejected | **Fixed** | Python accepts `0` or current `1024–65535` for revoke only; Node accepts `0–65535` for revoke only; tests pass |
| **S2** no-resident ignores `minecraft.guard_url` | **Fixed** | `core._minecraft_guard_settings`: env → `minecraft.guard_url` → default; test passes |
| **S3** case-only path passes validation | **Fixed** | `GuardSettings.validate` requires `realpath(world_path) == world_path`; my probe: exact accepted, uppercase rejected with `invalid_guard_settings` |
| **S4** restore overwrite race | **Fixed** | `publishNoReplace` creates the target atomically; my probe creates a concurrent target and sentinel, restore refuses and preserves the sentinel; [verified-junction-restore.log](verified-junction-restore.log) |
| **S5** config bound mismatch | **Fixed** | Guard/Python/Node all use world id 1–64, secret 32–256, PvP targets ≤16; guard self-test passes |
| **S6** observation breadth | **Fixed** | Node emits `time_of_day`, `weather`, `light`, `roof`, bounded safety events; resident derives game clock/comfort and uses game time for habits; tests pass. The habit *formation* gap is V1 |
| **F1** junction-following delete | **Fixed** | Real NTFS junction probe: `cleanupTemp` and `prune` delete only the link, victim files kept; restore failure cleanup keeps the junction target; [verified-junction-restore.log](verified-junction-restore.log) |
| **F2** signed revoke on marker failure | **Fixed at guard and host** | Java probe: first revoke `503` unsigned, marker absent, second `409`, restart re-armed; host now reports `revoke_durable:false`, `ok:false` on the 503→409 path; [verified-revoke-durability-java.log](verified-revoke-durability-java.log), [verified-revoke-durability.log](verified-revoke-durability.log) |
| **F3/F3b** snapshot busy kills the bot / duplicate schedules | **Fixed** | `SnapshotGate` + `BackupSchedule`; 63 Java self-tests; 115 Node tests; no Node/Python backup timer; see §5 |
| **F4** 64-ancestor limit | **Fixed** | `rejectRunningWorldAncestor` walks every parent; my 70-deep live-world probe refuses and publishes nothing |
| **F5** PvP without write | **Fixed** | `pvp` requires `writeEnabled`; `can_pvp = pvpEnabled && writeEnabled`; `pvpRequiresWrite` passes |
| **F6** case-only path | **Fixed** | Same as S3 |
| Old sidecar `-32008` mapping finding | **Rejected / fixed** | `sidecar_busy_mapping_probe.py` now returns `reason=guard_busy`, `failures_before=0 failures_after=0`, exit 0; [verified-sidecar-busy-mapping.log](verified-sidecar-busy-mapping.log) |

## 5. Snapshot-busy gate verification

- **Active backup >650 ms / authenticated busy keeps the connection and stops the active action:**
  Java self-tests use `ThreadedFakeWorld.blockSave` to hold the server thread and assert signed busy
  answers are off-thread, non-mutating, and never queue (`slowSnapshotServesBusyOffThread`,
  `escapeReleasesQueuedObserve`, `noMutationPermitDuringSnapshot`, `busyReceiptContract`). Node’s
  controller on `GuardBusy` calls `adapter.stop()`, pauses admissions and rejects `-32008` without
  quarantining. My real-loopback HTTP probe confirms: `busy_skill_code=-32008 stopCount=1
  quarantined=false connected=true` ([verified-node-busy-http.log](verified-node-busy-http.log)).
- **True revoke latched/detected ≤1 s:** the same probe changes the real HTTP server to `409 revoked`
  and the watchdog quarantines in **2 ms** (`revoke_detect_ms=2 quarantined=true`). The Java `revoke`
  latches in memory and answers immediately; the kick is asynchronous.
- **Unauthenticated busy cannot suppress stop:** `HttpGuard` verifies the Bearer header before reading
  the body; `revoke` is never gated. Java self-tests `httpRejectsUnauthenticatedSlowBody`,
  `httpReadDeadlineClosesSlowBody`, `revokeReachableUnderSlowBody` pass.
- **Exact world/path/port/HMAC caps:** signed response ≤16384 B, request ≤4096 B, exact raw-byte HMAC,
  exact world id/path/port checks; Java/Python/Node tests pass.
- **Busy cache has no stale gameplay authority:** the busy receipt is exactly the 16 fields, with
  `can_write=false`, `can_pvp=false`, empty `backup_id`; clients reject any other shape and any
  mutation action (`-32008`/`GuardBusy`). The cache is filled only by a live attestation and cleared on
  revoke.
- **No timed-out task publish:** Java `timeoutDoesNotPublish` runs the task, abandons the future, and
  proves the session is not refreshed; request-driven builds publish only in `finish()` after the future
  completed.
- **Configured 60 s is the sole Java authority:** `BackupSchedule` is the only periodic scheduler; Node
  has no backup timer/`periodicBackup`; the resident’s `backup_interval_min` is advisory only. Java
  `configured 60s interval is the only freshness schedule` and Node `the guard is the only periodic
  snapshot scheduler` pass.

Residual test-coverage limitation: the Java busy shape is tested in Java, and the Node real-HTTP probe
uses the same signed shape, but there is no single test that runs the real Java `HttpGuard` into the
real Node `GuardClient`/controller over one socket. That is a cross-language integration gap, not a
demonstrated defect; the live Java→Node path remains live-unverified.

## 6. Meaningful uncovered vs live-unverified

**Offline-uncovered (actionable):**

- Habit formation is only tested by manually seeding `habits["return_home"]`; no test forms a habit from
  repeated behavior (V1).
- The farm barren-ground shelter fallback is not asserted; the existing test calls `_project_steps`
  directly and only checks that the child rejects the fallback, not that `_ensure_project` switches to
  shelter (V2).
- No single Java→Node real-HTTP busy/revoke integration test (see §5).
- Protocol/acceptance docs are stale relative to the final core (V3); `repaired-2026-11-01.md` is a
  23:03 snapshot and is not the final word.

**Live-unverified (cannot be claimed from this review):**

- Loading the Fabric jar in a real 1.20.1 client; integrated-server lifecycle events; real
  `isRemote()`/LAN port truth; real `saveAll` + snapshot duration/tick stall; real kick-on-revoke.
- Real Open-to-LAN connection and reconnect on a changed temporary port; real Microsoft login/refresh
  and offline-LAN compatibility.
- Real Node 24 + Mineflayer 4.39.0 behavior on 1.20.1: pathfinding, dig/place/farm/chest,
  death/respawn, cancellation, 20 Hz claim (only ≥125 ms mutation spacing is claimed).
- Real offline restore drill and real emergency-stop drill; a real guard + sidecar + resident
  end-to-end.
- A 30–60 minute owner-observed run in the approved disposable world; all current 60-minute evidence is
  synthetic/accelerated.

Residual low-risk items not re-proved here: the signed revoke receipt does not prove the actor was
kicked (the kick is best-effort); Node still accepts a portless `http://127.0.0.1` guard URL and would
call port 80 if manually misconfigured (Python always passes an explicit port); the busy cache’s
`attested_at` has no maximum age (the receipt is non-mutating and the live path re-attests);
`minecraft.backup_retention` / `minecraft.interest` are compatibility keys with no readers.

## 7. Reproduce (exact probe commands)

From `D:\paw-old\pawbot-dsh`:

```powershell
py -3.13 reports\minecraft-review\probe\habit_formation_probe.py          # exit 1 (V1)
py -3.13 reports\minecraft-review\probe\farm_fallback_probe.py            # exit 1 (V2)
py -3.13 reports\minecraft-review\probe\farm_stand_reach_probe.py         # exit 0 (reach fixed)
py -3.13 reports\minecraft-review\probe\revoke_durability_false_success.py # exit 0 (no false success)
py -3.13 reports\minecraft-review\probe\emergency_stop_false_success.py    # exit 0 (M2 fixed)
py -3.13 reports\minecraft-review\probe\sidecar_busy_mapping_probe.py      # exit 0 (old finding fixed)
node reports\minecraft-review\probe\node_busy_http_probe.mjs               # exit 0 (real HTTP busy/revoke)
```

Java probes (after `gradle build`):

```powershell
$javac='C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\javac.exe'
$java ='C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\java.exe'
& $javac --release 17 -encoding UTF-8 `
  -cp 'minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test' `
  -d $env:TEMP\pawbot-verify reports\minecraft-review\probe\RevokeRaceStressProbe.java `
  reports\minecraft-review\probe\RevokeDurabilityProbe.java `
  reports\minecraft-review\probe\JunctionRestoreSafetyProbe.java
& $java -cp "$env:TEMP\pawbot-verify;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" dev.pawbot.guard.RevokeRaceStressProbe 100
& $java -cp "$env:TEMP\pawbot-verify;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" dev.pawbot.guard.RevokeDurabilityProbe
& $java -cp "$env:TEMP\pawbot-verify;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" dev.pawbot.guard.JunctionRestoreSafetyProbe
```

## 8. Bottom line

The offline engineering is substantially stronger than the previous snapshot: M1–M5, S1–S6, F1–F6 and
the busy gate are confirmed fixed/working, the full/focused/lifecycle suites are green, the Node suite
and audit are clean, and the Gradle clean build/self-test/remap artifact is reproducible. But **green
tests are not sufficient**: two required behaviors remain unimplemented in practice (learned habits and
the farm barren-ground fallback), and the protocol still describes the pre-fix revoke behavior. Until
V1/V2 are fixed (and V3 is updated by the doc owner), this integration is **NOT READY** for the v4
acceptance, independent of the remaining live-only work.
