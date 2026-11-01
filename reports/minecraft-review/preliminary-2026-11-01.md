# Minecraft preliminary review (agent-1d) - snapshot 2026-11-01 ~19:50

## Evidence actually run (offline, temp only)
- sidecar `npm test`: 93 pass / 0 fail / 0 skip; `npm audit --offline`: 0 vulnerabilities (uuid 11.1.1 override installed, `npm ls` shows single deduped copy).
- `node scripts/architecture-baseline.mjs`: 19/19 PASS. `node test/architecture-baseline.mjs`: 43 passed, 0 failed, 0 skipped.
- Python: `tools.test_minecraft_guard` + `tools.test_minecraft_policy`: 14 OK.
- NOT run: full Python suite, Java build (no java on PATH; 1f owns Fabric), test_minecraft_sidecar/integration/resident (files do not exist yet).

## Snapshot caveat
minecraft_resident.py mtime is 2026-11-01 15:28 (lead skeleton, not yet rewritten by agent-1b). Findings 1-4, 7-9 apply to that file as it exists on disk; re-verify on 1b's rewrite. Finding 10 is not a defect (ignore).

## Findings
1. BLOCKING (1b) `app/pawbot/minecraft_resident.py:264` SyntaxError: one extra `)` after `"decision_timeout_sec", 12))))`. `py -m py_compile` fails; module cannot import. core.py swallows it as "resident remains offline", so enabling minecraft would silently never start. No test_minecraft_resident.py exists to catch it.
2. SHOULD-FIX (1b) resident.py `decide`: `daily_calls` only increments after a successful valid decider answer. Failed/timeout/invalid-choice calls cost money but are not counted. Count the attempt before awaiting decider.
3. SHOULD-FIX (1b) `_due_decision` with decision_enabled False returns True every 60s; fine, but `max_decisions_per_day` does not apply to fallback path (OK) - confirm intent in tests.
4. SHOULD-FIX (1b) `_sidecar_env`: `str(_cfg(...,"write_enabled")).lower()` - a config string "True"/"TRUE" becomes "true" and enables the flag in Node, while Python policy requires `is True`. Use `"true" if value is True else "false"`.
5. SHOULD-FIX (1c) `minecraft_guard.GuardSettings.validate`: `urlsplit(...).port` raises raw ValueError for `:abc` / `:99999` (reproduced: "Port could not be cast..."), not GuardDenied; message text is unsanitized. Wrap in try/except.
6. SHOULD-FIX (1c) `verify_attestation` compares save_path with `os.path.realpath` on both sides: lexical/real-path resolution on the client can mask a guard reporting a different path via a link. Spec says canonical exact; compare normalized strings, keep realpath only for the configured side if wanted.
7. SHOULD-FIX (1b) `_run` calls `guard.request("observe")` then sidecar observe; on any exception it calls `sidecar.stop()` but does not `guard.request("revoke")`; only `stop()` revokes. Loop failure leaves guard permitting.
8. SHOULD-FIX (1b) `_execute`: skill chosen from project data without schema/type check of `args` before send; Node rejects, but errors propagate and kill the whole loop (any skill rejection => resident stops). Spec asks failure loop handling; a rejected skill should be recorded and backed off, not terminate.
9. NIT (1b) resident `start()` computes `permit["port"]` but `connect` timeout_sec passed as float (`float(...)`); Node requires integer, so default 15 -> `15.0` serialises as `15.0` and JSON.parse yields integer 15 in JS, OK; but a configured 12.5 is rejected. Use int.
10. SHOULD-FIX (mc-sidecar) `src/main.js` `write` path: if stdout backlog >256KB it calls finish() but the event that triggered it is dropped silently; acceptable. No finding.
11. SHOULD-FIX (1c) `paths._minecraft_path` checks links lexically before use (check-then-use TOCTOU) and `.strip("/")` normalises; acceptable for config paths but resident writes `resident.json` via tmp+replace without re-checking. Low risk; document.
12. VERIFIED OK: sidecar framing (real child, ready event, newline JSON, cancel round-trip), HMAC verified before parsing, exact nonce/action/actor/target/world/path/host/port match, 5s lifetime, backup required for connect/write/pvp, auth cache confined with lstat/symlink checks, device code only on TTY CLI, no FakeAdapter in production, uuid override compatible.

## Not yet reviewed
Java guard (1f), restore tool, webui control stop/CLI (1c scope), integration tests, resident acceptance, full Python suite, lifecycle stop races in minecraft_sidecar.py (read only; stop() kills on timeout, never replays - looks sound).



## Sidecar final pass (mc-sidecar ready snapshot, 19:52)
npm test: 93/94 pass, 1 fail (see below). npm audit: 0. node --check all 8 source files: syntax OK.

### Sidecar finding S1 — SHOULD-FIX (mc-sidecar) auth.test.js:27
test/auth.test.js line 27: `assert.rejects(flow.msa.authDeviceCode(), AuthRequiredError)` passes an eager promise, not a thunk. On Node 24 this allows the rejection to escape before assert.rejects wraps it, and the real Authflow constructor receiving no Titles triggers an unrelated TypeError. Fix: `assert.rejects(() => flow.msa.authDeviceCode(), AuthRequiredError)`. Production code is correct (confirmed: real createAuthflow with prismarine-auth 3.1.1, `.default.Titles` resolves, device code blocked, no files written).

### Sidecar verified OK (final)
- Socket gate: `beforeSocket` is a required function parameter (throws GameError if not); called after auth token fetch, before `client.setSocket`; freshness checked. socketAuthorized flag verified in controller before declaring connected.
- Player-type/registry check in attack: cross-checks entity.type against `b.registry.entitiesByName[entity.name]?.type` and playerRecord presence; mob-player fallback cannot occur.
- Hostile-mob defense: same registry cross-check, playerUuidSeen guard prevents pvp.
- All skill paths go through `finally { this.stop(); }` in adapter.run.
- Breed: re-fetches entity after equip to confirm still valid animal.
- Attack: re-fetches entity after permit to confirm still same entity/player match.
- No UID/persona/secret in RPC output; stderr discarded; stdout exclusive to protocol.
