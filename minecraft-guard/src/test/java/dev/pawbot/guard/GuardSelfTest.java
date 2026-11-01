package dev.pawbot.guard;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.reflect.Field;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

/**
 * Dependency-free test runner (JDK only) so the guard core can be verified without Gradle,
 * Minecraft, or a JUnit download. Every test uses temporary synthetic directories; no real world,
 * runtime data, or credential is read. Exit code is the number of failures.
 */
public final class GuardSelfTest {

    private interface Body {
        void run(Path base) throws Exception;
    }

    private static int passed;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        test("hmac matches RFC 4231 case 2", GuardSelfTest::hmacKnownVector);
        test("success response is exact bytes signed by HMAC", GuardSelfTest::signedExactBytes);
        test("response carries every contract field with 5s lifetime", GuardSelfTest::responseFields);
        test("missing/wrong bearer is 401 with no body detail", GuardSelfTest::authFailures);
        test("replayed nonce is 409 even after a denial", GuardSelfTest::replay);
        test("wrong actor is denied", GuardSelfTest::wrongActor);
        test("pvp needs exact allowlisted target plus flag", GuardSelfTest::pvpTargets);
        test("write needs its separate capability", GuardSelfTest::writeCapability);
        test("target only allowed for pvp", GuardSelfTest::targetOnlyForPvp);
        test("malformed, oversize and wrong-type requests rejected", GuardSelfTest::badFraming);
        test("unknown/extra keys and non-hex nonce rejected", GuardSelfTest::strictShape);
        test("closed LAN is denied", GuardSelfTest::lanClosed);
        test("port follows current LAN port", GuardSelfTest::currentPort);
        test("dedicated server is denied", GuardSelfTest::dedicated);
        test("world path mismatch is denied without leaking paths", GuardSelfTest::worldMismatch);
        test("missing or wrong marker is denied", GuardSelfTest::markerChecks);
        test("server-thread timeout fails closed", GuardSelfTest::timeoutFailsClosed);
        test("a snapshot that finishes after timeout is not published", GuardSelfTest::timeoutDoesNotPublish);
        test("connect saves then backs up and excludes session.lock", GuardSelfTest::connectBackup);
        test("write/pvp need an existing backup but never copy again", GuardSelfTest::writeNeedsBackup);
        test("write/pvp freshness is exact up to 1800 seconds", GuardSelfTest::backupAgeBoundary);
        test("periodic snapshot refreshes the write/pvp freshness window", GuardSelfTest::periodicBackupRefreshes);
        test("retention keeps newest 7 and only own-shaped dirs", GuardSelfTest::retention);
        test("backup refuses file-count and byte budgets", GuardSelfTest::budgets);
        test("backup refuses symlink inside the world", GuardSelfTest::linkInWorld);
        test("config rejects links in world or backup paths", GuardSelfTest::pathSafety);
        test("revoke is sticky, persists, and kicks the actor", GuardSelfTest::revoke);
        test("revoke marker blocks a restarted service", GuardSelfTest::revokePersists);
        test("external revoke marker latches without restart", GuardSelfTest::externalRevokeLatches);
        test("revoke refuses a connect queued on the server thread", GuardSelfTest::revokeVsQueuedConnect);
        test("revoke refuses queued backup/write and publishes no session", GuardSelfTest::revokeVsQueuedActions);
        test("revoke during the post-save snapshot refuses the permit", GuardSelfTest::revokeDuringSnapshot);
        test("revoke is not blocked by a busy server thread", GuardSelfTest::revokeNotBlockedByBusyServer);
        test("revoke receipt is signed with port 0 while LAN is closed", GuardSelfTest::revokeReceiptPortZero);
        test("revoke is not acknowledged when the marker cannot be persisted",
                GuardSelfTest::revokeMarkerWriteFailure);
        test("pvp requires the write capability", GuardSelfTest::pvpRequiresWrite);
        test("config authority bounds match the contract (64/256/16)", GuardSelfTest::authorityBounds);
        test("config rejects short secret, wildcard, bad uuid, overlap", GuardSelfTest::configRejects);
        test("config only enables on literal true", GuardSelfTest::configFlags);
        test("restore creates a new world and never overwrites", GuardSelfTest::restoreRules);
        test("restore preserves the source backup byte-for-byte", GuardSelfTest::restorePreservesSource);
        test("restore refuses locked, live, and unsafe sources", GuardSelfTest::restoreRejectsLockedOrRunning);
        test("restore rejects wrong marker world_id and bad ids", GuardSelfTest::restoreRejects);
        test("restore never replaces a target created during copying", GuardSelfTest::restoreTargetRace);
        test("restore rejects a missing protected original through an alias ancestor",
                GuardSelfTest::restoreProtectedMissingAncestor);
        test("restore refuses a target deep inside a running world", GuardSelfTest::restoreDeepRunningWorld);
        test("restore refuses a protected original used as its staging path",
                GuardSelfTest::restoreProtectedStaging);
        test("backup cleanup never follows a junction", GuardSelfTest::cleanupTempDoesNotFollowJunction);
        test("backup prune never follows a nested junction", GuardSelfTest::pruneDoesNotFollowJunction);
        test("loopback HTTP: only POST /v1/permit, signature header", GuardSelfTest::httpTransport);
        test("nonce cache is bounded and expires", GuardSelfTest::nonceBounds);
        test("backup schedule is exact, non-overlapping and skip-safe",
                GuardSelfTest::backupSchedulePolicy);
        test("slow snapshot answers signed busy receipts off-thread",
                GuardSelfTest::slowSnapshotServesBusyOffThread);
        test("a request queued before a tick snapshot escapes with a busy receipt",
                GuardSelfTest::escapeReleasesQueuedObserve);
        test("no mutation permit exists while a snapshot holds the server thread",
                GuardSelfTest::noMutationPermitDuringCopy);
        test("revoke answers during a snapshot and the copy never publishes",
                GuardSelfTest::concurrentRevokeDuringCopy);
        test("busy receipt is exact, signed and structurally non-mutating",
                GuardSelfTest::busyReceiptContract);
        test("tick snapshot skips without advancing while the gate is busy",
                GuardSelfTest::periodicBackupSkipsWithoutAdvancing);
        test("one snapshot at a time even under concurrent requests",
                GuardSelfTest::singleSnapshotAtATime);
        test("configured 60s interval is the only freshness schedule",
                GuardSelfTest::backupInterval60);
        test("loopback HTTP answers an unauthenticated slow body before reading it",
                GuardSelfTest::httpRejectsUnauthenticatedSlowBody);
        test("loopback HTTP closes an authenticated stalled body at the read deadline",
                GuardSelfTest::httpReadDeadlineClosesSlowBody);
        test("revoke stays reachable while a slow authenticated body holds a worker",
                GuardSelfTest::revokeReachableUnderSlowBody);

        System.out.println("passed=" + passed + " failed=" + failures.size());
        for (String f : failures) {
            System.out.println("FAIL " + f);
        }
        System.exit(failures.size());
    }

    private static void test(String name, Body body) {
        Path base = null;
        try {
            base = Files.createTempDirectory("pawbot-guard-test-");
            body.run(base);
            passed++;
            System.out.println("ok   " + name);
        } catch (Throwable t) {
            failures.add(name + " :: " + t);
        } finally {
            Fixtures.deleteTree(base);
        }
    }

    // ---------- helpers ----------

    private static void check(boolean cond, String msg) {
        if (!cond) {
            throw new AssertionError(msg);
        }
    }

    private static PermitService service(Path base, boolean write, boolean pvp, FakeWorld[] outWorld) throws IOException {
        Path world = Fixtures.world(base);
        FakeWorld fw = new FakeWorld(world);
        outWorld[0] = fw;
        GuardConfig cfg = Fixtures.config(base, world, write, pvp);
        BackupManager bm = new BackupManager(cfg.backupRoot, cfg.retention, cfg.maxBackupFiles, cfg.maxBackupBytes);
        Files.createDirectories(base.resolve("cfg"));
        return new PermitService(cfg, fw, bm, base.resolve("cfg"));
    }

    private static PermitService service(Path base) throws IOException {
        return service(base, true, true, new FakeWorld[1]);
    }

    private static Map<String, String> bodyFields(PermitService.Response r) throws Exception {
        Object parsed = Json.parse(r.bodyAsString());
        Map<String, String> out = new java.util.LinkedHashMap<>();
        for (Map.Entry<?, ?> e : ((Map<?, ?>) parsed).entrySet()) {
            out.put((String) e.getKey(), String.valueOf(e.getValue()));
        }
        return out;
    }

    /** Polls a condition for a bounded time so asynchronous best-effort work is observable. */
    private static void awaitTrue(BooleanSupplier condition, String message) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (System.nanoTime() < deadline) {
            if (condition.getAsBoolean()) {
                return;
            }
            Thread.sleep(10);
        }
        check(condition.getAsBoolean(), message);
    }

    /**
     * Reads the private session reference so a regression can prove that a snapshot completing
     * after revoke was never published. Reflection is test-only; production has no accessor.
     */
    private static String sessionBackupId(PermitService service) throws Exception {
        Field session = PermitService.class.getDeclaredField("sessionBackup");
        session.setAccessible(true);
        Object ref = session.get(service);
        Field id = ref.getClass().getDeclaredField("id");
        id.setAccessible(true);
        return (String) id.get(ref);
    }

    // ---------- tests ----------

    /**
     * Creates an NTFS junction (not a symlink) for deletion-path regressions. Junctions are the
     * shape that {@code walkFileTree} descends as a directory while {@code isSymbolicLink()} stays
     * false. Returns false and skips on non-Windows or when the host forbids it.
     */
    private static boolean createJunction(Path link, Path target) {
        String os = System.getProperty("os.name", "").toLowerCase(java.util.Locale.ROOT);
        if (!os.contains("win")) {
            return false;
        }
        try {
            Process process = new ProcessBuilder("cmd.exe", "/c", "mklink", "/J", link.toString(),
                    target.toString()).redirectErrorStream(true).start();
            process.getInputStream().readAllBytes();
            return process.waitFor() == 0 && Files.isDirectory(link, LinkOption.NOFOLLOW_LINKS);
        } catch (Exception e) {
            return false;
        }
    }

    private static void hmacKnownVector(Path base) {
        byte[] key = "Jefe".getBytes(StandardCharsets.UTF_8);
        byte[] data = "what do ya want for nothing?".getBytes(StandardCharsets.UTF_8);
        check(Hmac.hmacSha256(key, data).equals("5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"),
                "HMAC-SHA256 vector mismatch");
    }

    private static void signedExactBytes(Path base) throws Exception {
        PermitService s = service(base);
        PermitService.Response r = Fixtures.call(s, Fixtures.nonce(), "connect", "");
        check(r.status == 200, "status " + r.status + " " + r.bodyAsString());
        check(r.signature != null && r.signature.matches("^[0-9a-f]{64}$"), "signature format");
        check(r.signature.equals(Hmac.hmacSha256(Fixtures.SECRET.getBytes(StandardCharsets.UTF_8), r.body)),
                "signature must cover the exact raw bytes");
        byte[] tampered = r.body.clone();
        tampered[tampered.length - 2] ^= 1;
        check(!r.signature.equals(Hmac.hmacSha256(Fixtures.SECRET.getBytes(StandardCharsets.UTF_8), tampered)),
                "tampering must invalidate signature");
        check(r.body.length <= PermitService.MAX_RESPONSE_BYTES, "response cap");
    }

    private static void responseFields(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        String nonce = Fixtures.nonce();
        PermitService.Response r = Fixtures.call(s, nonce, "connect", "");
        Map<String, String> f = bodyFields(r);
        check(f.keySet().equals(new java.util.LinkedHashSet<>(List.of("nonce", "world_id", "save_path", "host",
                "port", "actor_uuid", "action", "target_uuid", "issued_at", "expires_at", "can_write", "can_pvp",
                "backup_id"))), "field set " + f.keySet());
        check(f.get("nonce").equals(nonce), "nonce echo");
        check(f.get("host").equals("127.0.0.1"), "host");
        check(f.get("port").equals("25565"), "port");
        check(f.get("action").equals("connect"), "action echo");
        check(f.get("save_path").equals(w[0].savePath.toRealPath().toString()), "canonical save path");
        long issued = Long.parseLong(f.get("issued_at"));
        long expires = Long.parseLong(f.get("expires_at"));
        check(expires - issued <= 5 && expires > issued, "lifetime");
        check(f.get("can_write").equals("true") && f.get("can_pvp").equals("true"), "capabilities");
        check(f.get("backup_id").startsWith("backup-"), "backup id");
        check(r.bodyAsString().contains("\"port\":25565"), "port must be a JSON number");
        check(r.bodyAsString().contains("\"can_write\":true"), "booleans must be JSON booleans");
    }

    private static void authFailures(Path base) throws Exception {
        PermitService s = service(base);
        byte[] body = Fixtures.request(Fixtures.nonce(), "observe", "").getBytes(StandardCharsets.UTF_8);
        long now = System.currentTimeMillis() / 1000;
        for (String auth : new String[] { null, "", "Bearer ", "Bearer wrong", "bearer " + Fixtures.SECRET,
                Fixtures.SECRET, "Bearer " + Fixtures.SECRET + "x", "Basic " + Fixtures.SECRET }) {
            PermitService.Response r = s.handle(auth, "application/json", body, now);
            check(r.status == 401, "auth " + auth + " gave " + r.status);
            check(r.signature == null, "no signature on denial");
            check(!r.bodyAsString().contains(base.toString()), "no path in unauthenticated error");
        }
    }

    private static void replay(Path base) throws Exception {
        PermitService s = service(base);
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 200, "observe needs no backup");
        PermitService s2 = service(base.resolve("two"));
        String n = Fixtures.nonce();
        check(Fixtures.call(s2, n, "backup", "").status == 200, "first");
        check(Fixtures.call(s2, n, "backup", "").status == 409, "replay");
        String d = Fixtures.nonce();
        check(Fixtures.call(s2, d, "pvp", "").status == 403, "denied pvp without target");
        check(Fixtures.call(s2, d, "pvp", "").status == 409, "denied nonce stays burned");
    }

    private static void wrongActor(Path base) throws Exception {
        PermitService s = service(base);
        String req = "{\"nonce\":\"" + Fixtures.nonce() + "\",\"actor_uuid\":\"" + UUID.randomUUID()
                + "\",\"action\":\"observe\",\"target_uuid\":\"\"}";
        PermitService.Response r = s.handle(Fixtures.bearer(), "application/json",
                req.getBytes(StandardCharsets.UTF_8), System.currentTimeMillis() / 1000);
        check(r.status == 403, "wrong actor " + r.status);
    }

    private static void pvpTargets(Path base) throws Exception {
        PermitService s = service(base, true, true, new FakeWorld[1]);
        check(Fixtures.call(s, Fixtures.nonce(), "connect", "").status == 200, "connect first");
        check(Fixtures.call(s, Fixtures.nonce(), "pvp", Fixtures.TARGET).status == 200, "allowlisted target");
        check(Fixtures.call(s, Fixtures.nonce(), "pvp", UUID.randomUUID().toString()).status == 403, "unknown target");
        check(Fixtures.call(s, Fixtures.nonce(), "pvp", "").status == 403, "empty target");
        check(Fixtures.call(s, Fixtures.nonce(), "pvp", "*").status == 400, "wildcard is not a uuid");
        PermitService off = service(base.resolve("off"), true, false, new FakeWorld[1]);
        check(Fixtures.call(off, Fixtures.nonce(), "pvp", Fixtures.TARGET).status == 403, "pvp disabled");
        PermitService.Response r = Fixtures.call(s, Fixtures.nonce(), "pvp", Fixtures.TARGET);
        check(bodyFields(r).get("target_uuid").equals(Fixtures.TARGET), "target echo");
    }

    private static void pvpRequiresWrite(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService service = service(base, false, true, w);
        check(Fixtures.call(service, Fixtures.nonce(), "connect", "").status == 200,
                "connect with writes disabled but pvp enabled");
        PermitService.Response pvp = Fixtures.call(service, Fixtures.nonce(), "pvp", Fixtures.TARGET);
        check(pvp.status == 403 && pvp.signature == null,
                "pvp must not be permitted while write is disabled: " + pvp.status);
        Map<String, String> fields = bodyFields(Fixtures.call(service, Fixtures.nonce(), "observe", ""));
        check(fields.get("can_write").equals("false"), "can_write false");
        check(fields.get("can_pvp").equals("false"),
                "can_pvp must not advertise a capability that write-disabled config cannot use");
    }

    private static void writeCapability(Path base) throws Exception {
        PermitService off = service(base, false, false, new FakeWorld[1]);
        check(Fixtures.call(off, Fixtures.nonce(), "write", "").status == 403, "write disabled");
        check(Fixtures.call(off, Fixtures.nonce(), "observe", "").status == 200, "observe unaffected");
        PermitService.Response r = Fixtures.call(off, Fixtures.nonce(), "observe", "");
        check(bodyFields(r).get("can_write").equals("false"), "can_write false");
    }

    private static void targetOnlyForPvp(Path base) throws Exception {
        PermitService s = service(base);
        for (String a : new String[] { "connect", "observe", "write", "backup", "revoke" }) {
            check(Fixtures.call(s, Fixtures.nonce(), a, Fixtures.TARGET).status == 403, a + " with target");
        }
    }

    private static void badFraming(Path base) throws Exception {
        PermitService s = service(base);
        long now = System.currentTimeMillis() / 1000;
        check(s.handle(Fixtures.bearer(), "text/plain", "{}".getBytes(), now).status == 415, "content type");
        check(s.handle(Fixtures.bearer(), null, "{}".getBytes(), now).status == 415, "no content type");
        check(s.handle(Fixtures.bearer(), "application/json", new byte[0], now).status == 413, "empty");
        check(s.handle(Fixtures.bearer(), "application/json", new byte[PermitService.MAX_REQUEST_BYTES + 1], now)
                .status == 413, "oversize");
        check(s.handle(Fixtures.bearer(), "application/json", "not json".getBytes(), now).status == 400, "not json");
        check(s.handle(Fixtures.bearer(), "application/json", new byte[] { (byte) 0xff, (byte) 0xfe }, now)
                .status == 400, "invalid utf-8");
        check(s.handle(Fixtures.bearer(), "application/json", "[]".getBytes(), now).status == 400, "array");
        String numeric = "{\"nonce\":1,\"actor_uuid\":\"" + Fixtures.ACTOR + "\",\"action\":\"observe\",\"target_uuid\":\"\"}";
        check(s.handle(Fixtures.bearer(), "application/json", numeric.getBytes(), now).status == 400, "numeric nonce");
    }

    private static void strictShape(Path base) throws Exception {
        PermitService s = service(base);
        long now = System.currentTimeMillis() / 1000;
        String extra = "{\"nonce\":\"" + Fixtures.nonce() + "\",\"actor_uuid\":\"" + Fixtures.ACTOR
                + "\",\"action\":\"observe\",\"target_uuid\":\"\",\"path\":\"x\"}";
        check(s.handle(Fixtures.bearer(), "application/json", extra.getBytes(), now).status == 400, "extra key");
        String missing = "{\"nonce\":\"" + Fixtures.nonce() + "\",\"actor_uuid\":\"" + Fixtures.ACTOR + "\",\"action\":\"observe\"}";
        check(s.handle(Fixtures.bearer(), "application/json", missing.getBytes(), now).status == 400, "missing key");
        check(Fixtures.call(s, "short", "observe", "").status == 400, "short nonce");
        check(Fixtures.call(s, "ZZ" + Fixtures.nonce().substring(2), "observe", "").status == 400, "non-hex nonce");
        check(Fixtures.call(s, Fixtures.nonce(), "shell", "").status == 400, "unknown action");
        check(Fixtures.call(s, Fixtures.nonce(), "rcon", "").status == 400, "rcon is not an action");
        String dup = "{\"nonce\":\"" + Fixtures.nonce() + "\",\"nonce\":\"" + Fixtures.nonce() + "\",\"actor_uuid\":\""
                + Fixtures.ACTOR + "\",\"action\":\"observe\",\"target_uuid\":\"\"}";
        check(s.handle(Fixtures.bearer(), "application/json", dup.getBytes(), now).status == 400, "duplicate key");
    }

    private static void lanClosed(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        w[0].port = 0;
        for (String a : new String[] { "connect", "observe", "backup", "write" }) {
            PermitService.Response r = Fixtures.call(s, Fixtures.nonce(), a, "");
            check(r.status == 403, a + " with closed LAN gave " + r.status);
            check(!r.bodyAsString().contains(base.toString()), "no path leak");
        }
    }

    private static void currentPort(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        check(bodyFields(Fixtures.call(s, Fixtures.nonce(), "observe", "")).get("port").equals("25565"), "first");
        w[0].port = 41234;
        check(bodyFields(Fixtures.call(s, Fixtures.nonce(), "observe", "")).get("port").equals("41234"), "re-opened LAN");
    }

    private static void dedicated(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        w[0].integrated = false;
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 403, "dedicated");
    }

    private static void worldMismatch(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        Path other = base.resolve("other-world");
        Files.createDirectories(other);
        Files.writeString(other.resolve(".pawbot-test-world"), Fixtures.WORLD_ID);
        w[0].savePath = other;
        PermitService.Response r = Fixtures.call(s, Fixtures.nonce(), "observe", "");
        check(r.status == 403, "other world accepted: " + r.status);
        check(!r.bodyAsString().contains("other-world") && !r.bodyAsString().contains(base.toString()), "no path leak");
        w[0].savePath = base.resolve("does-not-exist");
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 403, "missing world");
        w[0].savePath = null;
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 403, "null world");
    }

    private static void markerChecks(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        Path marker = w[0].savePath.resolve(".pawbot-test-world");
        Files.writeString(marker, "some-other-world");
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 403, "wrong marker content");
        Files.writeString(marker, "x".repeat(5000));
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 403, "oversize marker");
        Files.delete(marker);
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 403, "missing marker");
        Files.writeString(marker, Fixtures.WORLD_ID + "\n");
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 200, "restored marker");
    }

    private static void timeoutFailsClosed(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        w[0].failServerThread = true;
        PermitService.Response r = Fixtures.call(s, Fixtures.nonce(), "connect", "");
        check(r.status == 503 && r.signature == null, "timeout must refuse unsigned: " + r.status);
    }

    private static void timeoutDoesNotPublish(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        long t = 1_700_000_000L;
        check(Fixtures.callAt(s, Fixtures.nonce(), "connect", "", t).status == 200, "connect");
        w[0].timeOutAfterTask = true;
        PermitService.Response timedOut = Fixtures.callAt(s, Fixtures.nonce(), "backup", "", t + 1801);
        check(timedOut.status == 503 && timedOut.signature == null, "timed-out backup is refused");
        w[0].timeOutAfterTask = false;
        check(Fixtures.callAt(s, Fixtures.nonce(), "write", "", t + 1801).status == 403,
                "a backup that completed after timeout must not refresh the session");
    }

    private static void connectBackup(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        PermitService.Response r = Fixtures.call(s, Fixtures.nonce(), "connect", "");
        check(r.status == 200, "connect " + r.status);
        check(w[0].saveCount == 1, "world must be saved before the snapshot");
        String id = bodyFields(r).get("backup_id");
        Path backup = base.resolve("backups").resolve(id);
        check(Files.isRegularFile(backup.resolve("level.dat")), "level.dat copied");
        check(Files.isRegularFile(backup.resolve("region").resolve("r.0.0.mca")), "region copied");
        check(Files.isRegularFile(backup.resolve(".pawbot-test-world")), "marker copied");
        check(!Files.exists(backup.resolve("session.lock")), "session.lock must be excluded");
        PermitService.Response again = Fixtures.call(s, Fixtures.nonce(), "connect", "");
        check(!bodyFields(again).get("backup_id").equals(id), "every connect gets a fresh snapshot");
    }

    private static void writeNeedsBackup(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        check(Fixtures.call(s, Fixtures.nonce(), "write", "").status == 403, "write before any backup");
        PermitService.Response c = Fixtures.call(s, Fixtures.nonce(), "connect", "");
        String id = bodyFields(c).get("backup_id");
        PermitService.Response wr = Fixtures.call(s, Fixtures.nonce(), "write", "");
        check(wr.status == 200 && bodyFields(wr).get("backup_id").equals(id), "write reuses the session snapshot");
        check(w[0].saveCount == 1, "no extra save for write");
        try (var list = Files.list(base.resolve("backups"))) {
            check(list.count() == 1, "write must not copy the world again");
        }
    }

    private static void backupAgeBoundary(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        long t = 1_700_000_000L;
        PermitService.Response connect = Fixtures.callAt(s, Fixtures.nonce(), "connect", "", t);
        check(connect.status == 200, "connect " + connect.status);
        String id = bodyFields(connect).get("backup_id");
        check(id.startsWith("backup-"), "backup id");

        check(Fixtures.callAt(s, Fixtures.nonce(), "write", "", t + 1800).status == 200,
                "exactly 1800 s old snapshot must still be accepted");
        PermitService.Response pvp = Fixtures.callAt(s, Fixtures.nonce(), "pvp", Fixtures.TARGET, t + 1800);
        check(pvp.status == 200 && bodyFields(pvp).get("backup_id").equals(id),
                "pvp uses the same exact-boundary snapshot");
        check(Fixtures.callAt(s, Fixtures.nonce(), "write", "", t + 1801).status == 403,
                "1801 s old snapshot must be refused");
        check(Fixtures.callAt(s, Fixtures.nonce(), "write", "", t - 2).status == 403,
                "a snapshot more than one second in the future is refused");

        PermitService.Response fresh = Fixtures.callAt(s, Fixtures.nonce(), "backup", "", t + 1801);
        check(fresh.status == 200, "explicit backup " + fresh.status);
        String freshId = bodyFields(fresh).get("backup_id");
        check(!freshId.equals(id), "backup must create a new snapshot");
        check(Fixtures.callAt(s, Fixtures.nonce(), "write", "", t + 1801 + 1800).status == 200,
                "explicit backup resets the freshness window");
    }

    private static void periodicBackupRefreshes(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        long t = 1_700_000_000L;
        check(Fixtures.callAt(s, Fixtures.nonce(), "connect", "", t).status == 200, "connect");
        check(Fixtures.callAt(s, Fixtures.nonce(), "write", "", t + 1801).status == 403, "stale first");

        String id = s.periodicBackupOnServerThread(t + 1801);
        check(id.startsWith("backup-"), "periodic backup id " + id);
        check(Fixtures.callAt(s, Fixtures.nonce(), "write", "", t + 1801).status == 200,
                "periodic snapshot refreshes the session");
        check(Fixtures.callAt(s, Fixtures.nonce(), "write", "", t + 1801 + 1801).status == 403,
                "periodic snapshot is still bounded");

        Path marker = w[0].savePath.resolve(".pawbot-test-world");
        Files.writeString(marker, "wrong-world");
        check(s.periodicBackupOnServerThread(t + 4000).isEmpty(), "marker mismatch blocks periodic backup");
        Files.writeString(marker, Fixtures.WORLD_ID + "\n");
        check(s.periodicBackupOnServerThread(t + 4000).startsWith("backup-"), "restored marker permits again");
    }

    private static void retention(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path root = base.resolve("backups");
        BackupManager bm = new BackupManager(root, 7, 1000, 1L << 24);
        Files.createDirectories(root);
        Files.createDirectories(root.resolve("keep-me-unrelated"));
        Files.createDirectories(root.resolve("backup-notmine"));
        List<String> ids = new ArrayList<>();
        for (int i = 0; i < 10; i++) {
            ids.add(bm.backup(world));
        }
        try (var list = Files.list(root)) {
            List<String> names = list.map(p -> p.getFileName().toString()).sorted().toList();
            long mine = names.stream().filter(n -> n.matches("^backup-\\d{13}-\\d{3}$")).count();
            check(mine == 7, "retention 7 expected, got " + mine);
            check(names.contains("keep-me-unrelated") && names.contains("backup-notmine"), "foreign dirs untouched");
            check(!names.contains(ids.get(0)) && names.contains(ids.get(9)), "oldest pruned, newest kept");
        }
    }

    private static void budgets(Path base) throws Exception {
        Path world = Fixtures.world(base);
        for (int i = 0; i < 5; i++) {
            Files.writeString(world.resolve("f" + i + ".dat"), "x");
        }
        try {
            new BackupManager(base.resolve("b1"), 7, 3, 1L << 24).backup(world);
            check(false, "file budget must refuse");
        } catch (IOException expected) {
            check(!Files.exists(base.resolve("b1")) || Files.list(base.resolve("b1")).count() == 0, "nothing copied");
        }
        try {
            new BackupManager(base.resolve("b2"), 7, 1000, 8).backup(world);
            check(false, "byte budget must refuse");
        } catch (IOException expected) {
            check(Files.list(base.resolve("b2")).count() == 0, "nothing copied");
        }
    }

    private static void linkInWorld(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path outside = base.resolve("outside");
        Files.createDirectories(outside);
        Files.writeString(outside.resolve("secret.txt"), "do not copy");
        try {
            Files.createSymbolicLink(world.resolve("linked"), outside);
        } catch (IOException | UnsupportedOperationException e) {
            System.out.println("     (symlink creation unavailable on this host; skipped)");
            return;
        }
        try {
            new BackupManager(base.resolve("b"), 7, 1000, 1L << 24).backup(world);
            check(false, "link in world must be refused");
        } catch (IOException expected) {
            try (var list = Files.list(base.resolve("b"))) {
                check(list.count() == 0, "no partial backup left");
            }
        }
    }

    private static void pathSafety(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path link = base.resolve("world-link");
        try {
            Files.createSymbolicLink(link, world);
        } catch (IOException | UnsupportedOperationException e) {
            System.out.println("     (symlink creation unavailable on this host; skipped)");
            return;
        }
        try {
            GuardConfig.of(Fixtures.SECRET, Fixtures.WORLD_ID, link, Fixtures.ACTOR, List.of(), true, false, false,
                    base.resolve("backups"), 7, 1800, 6198, 1000, 1L << 24);
            check(false, "symlinked save path accepted");
        } catch (GuardConfig.ConfigException expected) {
            // fail-closed as required
        }
        Path realBackups = base.resolve("real-backups");
        Files.createDirectories(realBackups);
        Path backupLink = base.resolve("backup-link");
        Files.createSymbolicLink(backupLink, realBackups);
        try {
            GuardConfig.of(Fixtures.SECRET, Fixtures.WORLD_ID, world, Fixtures.ACTOR, List.of(), true, false, false,
                    backupLink, 7, 1800, 6198, 1000, 1L << 24);
            check(false, "symlinked backup root accepted");
        } catch (GuardConfig.ConfigException expected) {
            // fail-closed as required
        }
        try {
            PathSafety.requireNoLinks(link);
            check(false, "link component accepted");
        } catch (IOException expected) {
            // fail-closed as required
        }
    }

    private static void revoke(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        check(Fixtures.call(s, Fixtures.nonce(), "connect", "").status == 200, "pre-revoke connect");
        w[0].port = 41234;
        PermitService.Response r = Fixtures.call(s, Fixtures.nonce(), "revoke", "");
        check(r.status == 200 && r.signature != null, "revoke returns signed receipt");
        check(bodyFields(r).get("port").equals("41234"), "revoke receipt carries the current LAN port");
        awaitTrue(() -> w[0].disconnected.contains(UUID.fromString(Fixtures.ACTOR)), "registered actor kicked");
        check(Files.exists(base.resolve("cfg").resolve("pawbot-guard.revoked")), "marker persisted");
        for (String a : new String[] { "connect", "observe", "write", "backup" }) {
            check(Fixtures.call(s, Fixtures.nonce(), a, "").status == 409, a + " after revoke");
        }
        check(Fixtures.call(s, Fixtures.nonce(), "revoke", "").status == 409, "no automatic re-enable");
        check(s.periodicBackupOnServerThread(1_700_000_000L).isEmpty(), "revoked guard never snapshots");
    }

    private static void externalRevokeLatches(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService s = service(base, true, true, w);
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 200, "pre-marker observe");
        Path marker = base.resolve("cfg").resolve("pawbot-guard.revoked");
        Files.writeString(marker, "revoked\n");
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 409,
                "marker created while running must latch immediately");
        Files.delete(marker);
        check(Fixtures.call(s, Fixtures.nonce(), "observe", "").status == 409,
                "in-memory revocation cannot be undone by deleting the marker");
    }

    private static void revokePersists(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService first = service(base, true, true, w);
        Fixtures.call(first, Fixtures.nonce(), "revoke", "");
        GuardConfig cfg = Fixtures.config(base, w[0].savePath, true, true);
        PermitService restarted = new PermitService(cfg, w[0],
                new BackupManager(cfg.backupRoot, 7, 1000, 1L << 24), base.resolve("cfg"));
        check(restarted.isRevoked(), "restart must stay revoked");
        check(Fixtures.call(restarted, Fixtures.nonce(), "connect", "").status == 409, "denied after restart");
        Files.delete(base.resolve("cfg").resolve("pawbot-guard.revoked"));
        PermitService manual = new PermitService(cfg, w[0],
                new BackupManager(cfg.backupRoot, 7, 1000, 1L << 24), base.resolve("cfg"));
        check(Fixtures.call(manual, Fixtures.nonce(), "observe", "").status == 200, "manual marker removal re-arms");
    }

    private static void revokeVsQueuedConnect(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService service = service(base, true, true, w);
        w[0].serverGateEntered = new CountDownLatch(1);
        w[0].serverGateRelease = new CountDownLatch(1);
        w[0].serverGateCalls = 1;
        ExecutorService executor = Executors.newSingleThreadExecutor();
        Future<PermitService.Response> queued = executor.submit(
                () -> Fixtures.call(service, Fixtures.nonce(), "connect", ""));
        try {
            check(w[0].serverGateEntered.await(5, TimeUnit.SECONDS), "connect never reached the server thread");
            PermitService.Response revoke = Fixtures.call(service, Fixtures.nonce(), "revoke", "");
            check(revoke.status == 200 && revoke.signature != null, "revoke receipt " + revoke.status);
            check(service.isRevoked(), "revocation must be latched before the receipt");
            // The queued snapshot task is released only after the revoke has been acknowledged.
            w[0].serverGateRelease.countDown();
            PermitService.Response after = queued.get(5, TimeUnit.SECONDS);
            check(after.status == 409 && after.signature == null,
                    "connect that was already queued must be refused after revoke: " + after.status);
            check(sessionBackupId(service).isEmpty(), "revoked service must not publish a session backup");
        } finally {
            w[0].serverGateRelease.countDown();
            executor.shutdownNow();
        }
    }

    private static void revokeVsQueuedActions(Path base) throws Exception {
        for (String action : new String[] { "backup", "write" }) {
            Path sub = base.resolve(action);
            FakeWorld[] w = new FakeWorld[1];
            PermitService service = service(sub, true, true, w);
            check(Fixtures.call(service, Fixtures.nonce(), "connect", "").status == 200,
                    action + " seed connect");
            check(!sessionBackupId(service).isEmpty(), action + " seed session must exist");
            w[0].serverGateEntered = new CountDownLatch(1);
            w[0].serverGateRelease = new CountDownLatch(1);
            w[0].serverGateCalls = 1;
            ExecutorService executor = Executors.newSingleThreadExecutor();
            Future<PermitService.Response> queued = executor.submit(
                    () -> Fixtures.call(service, Fixtures.nonce(), action, ""));
            try {
                check(w[0].serverGateEntered.await(5, TimeUnit.SECONDS), action + " never queued");
                PermitService.Response revoke = Fixtures.call(service, Fixtures.nonce(), "revoke", "");
                check(revoke.status == 200, action + " revoke receipt " + revoke.status);
                w[0].serverGateRelease.countDown();
                PermitService.Response after = queued.get(5, TimeUnit.SECONDS);
                check(after.status == 409 && after.signature == null,
                        "queued " + action + " must be refused after revoke: " + after.status);
                check(sessionBackupId(service).isEmpty(),
                        action + " must not republish the session after revoke");
            } finally {
                w[0].serverGateRelease.countDown();
                executor.shutdownNow();
            }
        }
    }

    private static void revokeDuringSnapshot(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService service = service(base, true, true, w);
        w[0].saveGateEntered = new CountDownLatch(1);
        w[0].saveGateRelease = new CountDownLatch(1);
        ExecutorService executor = Executors.newSingleThreadExecutor();
        Future<PermitService.Response> connect = executor.submit(
                () -> Fixtures.call(service, Fixtures.nonce(), "connect", ""));
        try {
            check(w[0].saveGateEntered.await(5, TimeUnit.SECONDS), "connect never reached saveAll");
            PermitService.Response revoke = Fixtures.call(service, Fixtures.nonce(), "revoke", "");
            check(revoke.status == 200, "revoke receipt " + revoke.status);
            // The snapshot (save + copy) finishes only after the revoke receipt exists.
            w[0].saveGateRelease.countDown();
            PermitService.Response after = connect.get(10, TimeUnit.SECONDS);
            check(after.status == 409 && after.signature == null,
                    "a snapshot that completed after revoke must not be signed: " + after.status);
            check(sessionBackupId(service).isEmpty(),
                    "a snapshot that completed after revoke must not publish the session");
            check(Fixtures.call(service, Fixtures.nonce(), "write", "").status == 409,
                    "no write permit after revoke");
        } finally {
            w[0].saveGateRelease.countDown();
            executor.shutdownNow();
        }
    }

    private static void revokeNotBlockedByBusyServer(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService service = service(base, true, true, w);
        w[0].serverGateEntered = new CountDownLatch(1);
        w[0].serverGateRelease = new CountDownLatch(1);
        ExecutorService executor = Executors.newFixedThreadPool(2);
        Future<PermitService.Response> blockedPermit = executor.submit(
                () -> Fixtures.call(service, Fixtures.nonce(), "connect", ""));
        Future<PermitService.Response> revoke = null;
        try {
            check(w[0].serverGateEntered.await(5, TimeUnit.SECONDS), "busy server task never started");
            revoke = executor.submit(() -> Fixtures.call(service, Fixtures.nonce(), "revoke", ""));
            PermitService.Response receipt = revoke.get(3, TimeUnit.SECONDS);
            check(receipt.status == 200 && receipt.signature != null,
                    "revoke must answer while the server thread is busy: " + receipt.status);
            check(service.isRevoked(), "revocation must be latched even if the kick is still queued");
        } catch (java.util.concurrent.TimeoutException e) {
            check(false, "revoke was blocked behind the busy server thread");
        } finally {
            if (revoke != null) {
                revoke.cancel(true);
            }
            w[0].serverGateRelease.countDown();
            blockedPermit.cancel(true);
            executor.shutdownNow();
        }
    }

    private static void revokeReceiptPortZero(Path base) throws Exception {
        FakeWorld[] w = new FakeWorld[1];
        PermitService service = service(base, true, true, w);
        w[0].port = 0;
        PermitService.Response receipt = Fixtures.call(service, Fixtures.nonce(), "revoke", "");
        check(receipt.status == 200 && receipt.signature != null,
                "revoke must still be a signed receipt while LAN is closed: " + receipt.status);
        check(receipt.signature.equals(Hmac.hmacSha256(
                Fixtures.SECRET.getBytes(StandardCharsets.UTF_8), receipt.body)), "receipt signature");
        Map<String, String> fields = bodyFields(receipt);
        check(fields.get("action").equals("revoke"), "revoke action echo");
        check(fields.get("port").equals("0"), "closed LAN must be reported as port 0, not rejected");
        check(fields.get("backup_id").isEmpty(), "revoke carries no session backup");
        check(Fixtures.call(service, Fixtures.nonce(), "connect", "").status == 409,
                "connect after a closed-LAN revoke receipt");
    }

    private static void revokeMarkerWriteFailure(Path base) throws Exception {
        Path world = Fixtures.world(base);
        // The config directory is deliberately absent, so no durable marker can be written.
        Path missingConfig = base.resolve("missing-config");
        GuardConfig cfg = Fixtures.config(base, world, true, true);
        FakeWorld fw = new FakeWorld(world);
        PermitService service = new PermitService(cfg, fw,
                new BackupManager(cfg.backupRoot, cfg.retention, cfg.maxBackupFiles, cfg.maxBackupBytes),
                missingConfig);
        PermitService.Response receipt = Fixtures.call(service, Fixtures.nonce(), "revoke", "");
        check(receipt.status == 503 && receipt.signature == null,
                "revoke without a durable marker must be an unsigned failure: " + receipt.status);
        check(service.isRevoked(), "the in-memory latch must still stop this process");
        check(Fixtures.call(service, Fixtures.nonce(), "connect", "").status == 409,
                "no permit after a non-durable revoke");
        check(!Files.exists(missingConfig.resolve("pawbot-guard.revoked"), LinkOption.NOFOLLOW_LINKS),
                "no marker was persisted");
        // Documented consequence: without the marker a restart is re-armed. The host must report
        // the incomplete stop rather than treat this as a successful emergency stop.
        PermitService restarted = new PermitService(cfg, new FakeWorld(world),
                new BackupManager(cfg.backupRoot, cfg.retention, cfg.maxBackupFiles, cfg.maxBackupBytes),
                missingConfig);
        check(!restarted.isRevoked(), "a restart is re-armed when the marker was never durable");
    }

    private static void authorityBounds(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path backups = base.resolve("bounds-backups");
        List<String> pvp16 = new ArrayList<>();
        for (int i = 0; i < 16; i++) {
            pvp16.add(String.format("00000000-0000-0000-0000-%012d", i));
        }
        // Exactly at the authority ceiling: a 256-char secret, a 64-char world id, 16 PvP targets.
        GuardConfig atCeiling = GuardConfig.of("s".repeat(256), "w".repeat(64), world, Fixtures.ACTOR, pvp16,
                true, false, true, backups, 7, 1800, 6198, 1000, 1L << 24);
        check(atCeiling.secret.length == 256 && atCeiling.worldId.length() == 64
                && atCeiling.pvpTargets.size() == 16, "ceiling values must be accepted");
        String[][] bad = {
                { "s".repeat(257), Fixtures.WORLD_ID, "secret above 256" },
                { "s".repeat(31), Fixtures.WORLD_ID, "secret below 32" },
                { Fixtures.SECRET, "w".repeat(65), "world_id above 64" },
        };
        for (String[] row : bad) {
            try {
                GuardConfig.of(row[0], row[1], world, Fixtures.ACTOR, List.of(), true, false, false, backups,
                        7, 1800, 6198, 1000, 1L << 24);
                check(false, "accepted " + row[2]);
            } catch (GuardConfig.ConfigException expected) {
                // rejected as required
            }
        }
        List<String> pvp17 = new ArrayList<>(pvp16);
        pvp17.add("00000000-0000-0000-0000-000000000016");
        try {
            GuardConfig.of(Fixtures.SECRET, Fixtures.WORLD_ID, world, Fixtures.ACTOR, pvp17, true, false, true,
                    backups, 7, 1800, 6198, 1000, 1L << 24);
            check(false, "accepted 17 pvp targets");
        } catch (GuardConfig.ConfigException expected) {
            // rejected as required
        }
    }

    private static void configRejects(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path backups = base.resolve("backups");
        String ok = Fixtures.SECRET;
        String[][] bad = {
                { "short", Fixtures.ACTOR },
                { ok, "not-a-uuid" },
                { ok, "11111111-1111-1111-1111-11111111111G" },
                { ok, "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA" },
        };
        for (String[] b : bad) {
            try {
                GuardConfig.of(b[0], Fixtures.WORLD_ID, world, b[1], List.of(), true, false, false, backups, 7, 1800,
                        6198, 1000, 1L << 24);
                check(false, "accepted " + b[0] + " / " + b[1]);
            } catch (GuardConfig.ConfigException expected) {
                check(!expected.getMessage().contains(ok), "message must not echo the secret");
            }
        }
        for (String wildcard : new String[] { "*", "all", "" }) {
            try {
                GuardConfig.of(ok, Fixtures.WORLD_ID, world, Fixtures.ACTOR, List.of(wildcard), true, false, true,
                        backups, 7, 1800, 6198, 1000, 1L << 24);
                check(false, "wildcard accepted: " + wildcard);
            } catch (GuardConfig.ConfigException expected) {
                // rejected as required
            }
        }
        try {
            GuardConfig.of(ok, Fixtures.WORLD_ID, world, Fixtures.ACTOR, List.of(Fixtures.ACTOR), true, false, true,
                    backups, 7, 1800, 6198, 1000, 1L << 24);
            check(false, "actor as pvp target accepted");
        } catch (GuardConfig.ConfigException expected) {
            // rejected
        }
        try {
            GuardConfig.of(ok, Fixtures.WORLD_ID, world, Fixtures.ACTOR, List.of(), true, false, false,
                    world.resolve("backups"), 7, 1800, 6198, 1000, 1L << 24);
            check(false, "backup root inside world accepted");
        } catch (GuardConfig.ConfigException expected) {
            // rejected
        }
        try {
            GuardConfig.of(ok, "../escape", world, Fixtures.ACTOR, List.of(), true, false, false, backups, 7, 1800,
                    6198, 1000, 1L << 24);
            check(false, "bad world id accepted");
        } catch (GuardConfig.ConfigException expected) {
            // rejected
        }
        for (int bad2 : new int[] { 0, 80, 70000 }) {
            try {
                GuardConfig.of(ok, Fixtures.WORLD_ID, world, Fixtures.ACTOR, List.of(), true, false, false, backups, 7,
                        1800, bad2, 1000, 1L << 24);
                check(false, "port " + bad2);
            } catch (GuardConfig.ConfigException expected) {
                // rejected
            }
        }
        for (int badInterval : new int[] { 0, 59, 1801, 86400 }) {
            try {
                GuardConfig.of(ok, Fixtures.WORLD_ID, world, Fixtures.ACTOR, List.of(), true, false, false, backups, 7,
                        badInterval, 6198, 1000, 1L << 24);
                check(false, "backup interval " + badInterval);
            } catch (GuardConfig.ConfigException expected) {
                // rejected
            }
        }
        try {
            GuardConfig exact = GuardConfig.of(ok, Fixtures.WORLD_ID, world, Fixtures.ACTOR, List.of(), true, false,
                    false, backups, 7, 1800, 6198, 1000, 1L << 24);
            check(exact.backupIntervalSec == 1800, "1800 s ceiling must be accepted");
        } catch (GuardConfig.ConfigException e) {
            check(false, "1800 s ceiling rejected: " + e.getMessage());
        }
    }

    private static void configFlags(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path file = base.resolve("pawbot-guard.json");
        String json = "{\"secret\":\"" + Fixtures.SECRET + "\",\"world_id\":\"" + Fixtures.WORLD_ID
                + "\",\"save_path\":" + Json.quote(world.toString()) + ",\"actor_uuid\":\"" + Fixtures.ACTOR
                + "\",\"pvp_uuids\":[],\"enabled\":\"true\",\"write_enabled\":1,\"pvp_enabled\":true,"
                + "\"backup_root\":" + Json.quote(base.resolve("bk").toString()) + "}";
        Files.writeString(file, json);
        GuardConfig c = GuardConfig.load(file);
        check(!c.enabled && !c.writeEnabled && c.pvpEnabled, "only literal true enables a flag");
        check(c.retention == 7 && c.backupIntervalSec == 1800 && c.port == 6198, "defaults");
        Files.writeString(file, json.replace("\"pvp_uuids\":[]", "\"pvp_uuids\":[],\"rcon\":\"x\""));
        try {
            GuardConfig.load(file);
            check(false, "unknown key accepted");
        } catch (GuardConfig.ConfigException expected) {
            // rejected
        }
        for (String huge : new String[] {
                json.replace("\"pvp_uuids\":[]", "\"pvp_uuids\":[],\"backup_interval_sec\":4294969096"),
                json.replace("\"pvp_uuids\":[]", "\"pvp_uuids\":[],\"port\":4294973494"),
                json.replace("\"pvp_uuids\":[]", "\"pvp_uuids\":[],\"retention\":4294967303"),
                json.replace("\"pvp_uuids\":[]", "\"pvp_uuids\":[],\"max_backup_files\":4294987296") }) {
            Files.writeString(file, huge);
            try {
                GuardConfig.load(file);
                check(false, "wrapped integer accepted");
            } catch (GuardConfig.ConfigException expected) {
                // rejected before narrowing
            }
        }
        Files.writeString(file, json.replace(Json.quote(world.toString()), Json.quote("bad\u0000path")));
        try {
            GuardConfig.load(file);
            check(false, "invalid path accepted");
        } catch (GuardConfig.ConfigException expected) {
            check(!expected.getMessage().contains("bad"), "message must not echo the path");
        }
    }

    private static void restoreRules(Path base) throws Exception {
        Path world = Fixtures.world(base);
        BackupManager bm = new BackupManager(base.resolve("bk"), 7, 1000, 1L << 24);
        String id = bm.backup(world);
        Path target = base.resolve("restored-world");
        Path restored = RestoreTool.restore(base.resolve("bk"), id, target, Fixtures.WORLD_ID);
        check(Files.isSameFile(restored, target), "restore returns the created directory");
        check(Files.readString(target.resolve("level.dat")).equals("synthetic"), "content restored");
        check(Files.isRegularFile(target.resolve(".pawbot-test-world")), "marker restored");
        check(!Files.exists(target.resolve("session.lock")), "restored world is closed (no session.lock)");
        try {
            RestoreTool.restore(base.resolve("bk"), id, target, Fixtures.WORLD_ID);
            check(false, "overwrite must be refused");
        } catch (IOException expected) {
            check(Files.readString(target.resolve("level.dat")).equals("synthetic"), "existing untouched");
        }
        try {
            RestoreTool.restore(base.resolve("bk"), id, world, Fixtures.WORLD_ID);
            check(false, "restore onto existing (original) world must be refused");
        } catch (IOException expected) {
            check(Files.readString(world.resolve("level.dat")).equals("synthetic"), "original untouched");
        }
        try {
            RestoreTool.restore(base.resolve("bk"), id, base.resolve("bk").resolve(id + "-copy"), Fixtures.WORLD_ID);
            check(false, "target inside backup root must be refused");
        } catch (IOException expected) {
            // rejected
        }
    }

    private static void restorePreservesSource(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path backupRoot = base.resolve("bk");
        BackupManager bm = new BackupManager(backupRoot, 7, 1000, 1L << 24);
        String id = bm.backup(world);
        Path source = backupRoot.resolve(id);
        String before = Fixtures.treeFingerprint(source);
        Path target = base.resolve("restored-preserved");
        RestoreTool.restore(backupRoot, id, target, Fixtures.WORLD_ID);
        check(Fixtures.treeFingerprint(source).equals(before), "restore must not modify the source backup");
        check(Files.readString(target.resolve("level.dat")).equals("synthetic"), "restored content");
        check(Files.isRegularFile(target.resolve(".pawbot-test-world")), "restored marker");
    }

    private static void restoreRejectsLockedOrRunning(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path backupRoot = base.resolve("bk");
        BackupManager bm = new BackupManager(backupRoot, 7, 1000, 1L << 24);
        String id = bm.backup(world);

        // A backup that captured a live session.lock is refused: it is not a closed snapshot.
        Path lockedId = backupRoot.resolve("backup-1700000000000-000");
        Files.createDirectories(lockedId);
        Files.writeString(lockedId.resolve(".pawbot-test-world"), Fixtures.WORLD_ID + "\n");
        Files.writeString(lockedId.resolve("level.dat"), "live");
        Files.writeString(lockedId.resolve("session.lock"), "lock");
        try {
            RestoreTool.restore(backupRoot, lockedId.getFileName().toString(), base.resolve("from-locked"),
                    Fixtures.WORLD_ID);
            check(false, "backup containing session.lock accepted");
        } catch (IOException expected) {
            check(!Files.exists(base.resolve("from-locked")), "nothing created from locked backup");
        }

        // A target below an open world would write into a live save.
        Path runningWorld = base.resolve("running-world");
        Files.createDirectories(runningWorld);
        Files.writeString(runningWorld.resolve(".pawbot-test-world"), Fixtures.WORLD_ID + "\n");
        Files.writeString(runningWorld.resolve("level.dat"), "running");
        Files.writeString(runningWorld.resolve("session.lock"), "lock");
        try {
            RestoreTool.restore(backupRoot, id, runningWorld.resolve("restored-inside"), Fixtures.WORLD_ID);
            check(false, "target inside a running world accepted");
        } catch (IOException expected) {
            check(!Files.exists(runningWorld.resolve("restored-inside")), "nothing created inside a live world");
        }

        // Missing marker is not a disposable-world snapshot.
        Path noMarkerId = backupRoot.resolve("backup-1700000000001-000");
        Files.createDirectories(noMarkerId);
        Files.writeString(noMarkerId.resolve("level.dat"), "no marker");
        try {
            RestoreTool.restore(backupRoot, noMarkerId.getFileName().toString(), base.resolve("from-nomarker"),
                    Fixtures.WORLD_ID);
            check(false, "backup without marker accepted");
        } catch (IOException expected) {
            check(!Files.exists(base.resolve("from-nomarker")), "nothing created without marker");
        }

        for (String badWorld : new String[] { "", "../escape", "bad world" }) {
            try {
                RestoreTool.restore(backupRoot, id, base.resolve("bad-" + Math.abs(badWorld.hashCode())), badWorld);
                check(false, "bad world_id accepted: " + badWorld);
            } catch (IOException expected) {
                // rejected before creating anything
            }
        }

        // The optional protected original path is refused even when it no longer exists.
        Path deletedOriginal = base.resolve("deleted-original");
        try {
            RestoreTool.restore(backupRoot, id, deletedOriginal, Fixtures.WORLD_ID, deletedOriginal);
            check(false, "protected original world accepted");
        } catch (IOException expected) {
            check(!Files.exists(deletedOriginal), "nothing created at original path");
        }
        Path existingOriginal = base.resolve("protected-world");
        Files.createDirectories(existingOriginal);
        try {
            RestoreTool.restore(backupRoot, id, existingOriginal.resolve("child"), Fixtures.WORLD_ID,
                    existingOriginal);
            check(false, "target inside protected original accepted");
        } catch (IOException expected) {
            check(!Files.exists(existingOriginal.resolve("child")), "nothing created inside original path");
        }
    }

    private static void restoreRejects(Path base) throws Exception {
        Path world = Fixtures.world(base);
        BackupManager bm = new BackupManager(base.resolve("bk"), 7, 1000, 1L << 24);
        String id = bm.backup(world);
        String[] badIds = { "../" + id, id + "/..", "backup-1", "..", "" };
        for (String b : badIds) {
            try {
                RestoreTool.restore(base.resolve("bk"), b, base.resolve("t-" + Math.abs(b.hashCode())),
                        Fixtures.WORLD_ID);
                check(false, "bad id accepted: " + b);
            } catch (IOException expected) {
                // rejected
            }
        }
        try {
            RestoreTool.restore(base.resolve("bk"), id, base.resolve("wrong-world"), "different-world-id");
            check(false, "world_id mismatch accepted");
        } catch (IOException expected) {
            check(!Files.exists(base.resolve("wrong-world")), "nothing created");
        }
        try {
            RestoreTool.restore(base.resolve("bk"), id, base.resolve("no-parent").resolve("child"), Fixtures.WORLD_ID);
            check(false, "missing parent accepted");
        } catch (IOException expected) {
            // rejected
        }
    }

    private static void restoreTargetRace(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path backupRoot = base.resolve("race-bk");
        BackupManager bm = new BackupManager(backupRoot, 7, 1000, 1L << 24);
        String id = bm.backup(world);
        Path source = backupRoot.resolve(id);
        String before = Fixtures.treeFingerprint(source);
        Path target = base.resolve("race-target");
        Runnable createTarget = () -> {
            try {
                // A different process creates the target while the tree is still being copied.
                Files.writeString(target, "owner-file-appeared-during-copy");
            } catch (IOException e) {
                throw new java.io.UncheckedIOException(e);
            }
        };
        try {
            RestoreTool.restore(backupRoot, id, target, Fixtures.WORLD_ID, null, createTarget);
            check(false, "a target created during copying must never be replaced");
        } catch (IOException expected) {
            check(Files.isRegularFile(target, LinkOption.NOFOLLOW_LINKS),
                    "the concurrently created target must survive");
            check(Files.readString(target).equals("owner-file-appeared-during-copy"),
                    "the concurrently created target must keep its bytes");
        }
        check(Fixtures.treeFingerprint(source).equals(before), "source backup must remain unchanged");
        Path staging = target.resolveSibling(target.getFileName().toString() + ".restoring");
        check(!Files.exists(staging, LinkOption.NOFOLLOW_LINKS), "failed publication must clean its staging tree");
    }

    private static void restoreProtectedMissingAncestor(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path backupRoot = base.resolve("protected-bk");
        BackupManager bm = new BackupManager(backupRoot, 7, 1000, 1L << 24);
        String id = bm.backup(world);

        // The protected original no longer exists, but its existing ancestor is an alias for the
        // target's real parent. Lexical comparison alone would miss this overlap.
        Path real = Files.createDirectories(base.resolve("canonical-root"));
        Path alias = base.resolve("alias-root");
        try {
            Files.createSymbolicLink(alias, real);
        } catch (IOException | UnsupportedOperationException e) {
            System.out.println("     (symlink creation unavailable on this host; alias case skipped)");
        }
        if (Files.isSymbolicLink(alias)) {
            Path protectedThroughAlias = alias.resolve("original-world");
            Path targetAtCanonicalPath = real.resolve("original-world");
            try {
                RestoreTool.restore(backupRoot, id, targetAtCanonicalPath, Fixtures.WORLD_ID, protectedThroughAlias);
                check(false, "missing protected original reached through an alias ancestor was accepted");
            } catch (IOException expected) {
                check(!Files.exists(targetAtCanonicalPath, LinkOption.NOFOLLOW_LINKS),
                        "nothing may be created at the protected canonical path");
            }
        }

        // Plain missing original: creating its parent would also create an ancestor of the
        // protected path, so it must be refused even though both paths are absent.
        Path missingParent = base.resolve("missing-parent");
        Path protectedPlain = missingParent.resolve("original-world");
        try {
            RestoreTool.restore(backupRoot, id, missingParent, Fixtures.WORLD_ID, protectedPlain);
            check(false, "target that is an ancestor of the missing protected world was accepted");
        } catch (IOException expected) {
            check(!Files.exists(missingParent, LinkOption.NOFOLLOW_LINKS), "nothing may be created at the ancestor");
        }
    }

    private static void restoreDeepRunningWorld(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path backupRoot = base.resolve("deep-bk");
        BackupManager bm = new BackupManager(backupRoot, 7, 1000, 1L << 24);
        String id = bm.backup(world);

        Path running = Files.createDirectories(base.resolve("live"));
        Files.writeString(running.resolve(".pawbot-test-world"), Fixtures.WORLD_ID + "\n");
        Files.writeString(running.resolve("level.dat"), "running");
        Files.writeString(running.resolve("session.lock"), "lock");
        Path deep = running;
        for (int i = 0; i < 70; i++) {
            deep = Files.createDirectories(deep.resolve("d"));
        }
        Path target = deep.resolve("restored");
        try {
            RestoreTool.restore(backupRoot, id, target, Fixtures.WORLD_ID);
            check(false, "a target more than 64 ancestors below a running world was accepted");
        } catch (IOException expected) {
            check(!Files.exists(target, LinkOption.NOFOLLOW_LINKS), "nothing may be written into a live save");
        }
    }

    private static void restoreProtectedStaging(Path base) throws Exception {
        Path world = Fixtures.world(base);
        Path backupRoot = base.resolve("staging-bk");
        BackupManager bm = new BackupManager(backupRoot, 7, 1000, 1L << 24);
        String id = bm.backup(world);
        Path target = base.resolve("new-world");
        Path protectedStaging = target.resolveSibling(target.getFileName().toString() + ".restoring");
        try {
            RestoreTool.restore(backupRoot, id, target, Fixtures.WORLD_ID, protectedStaging);
            check(false, "a missing protected original used as the staging directory was accepted");
        } catch (IOException expected) {
            check(!Files.exists(target, LinkOption.NOFOLLOW_LINKS), "nothing may be published");
            check(!Files.exists(protectedStaging, LinkOption.NOFOLLOW_LINKS),
                    "the protected original path must never be used as scratch");
        }
    }

    private static void cleanupTempDoesNotFollowJunction(Path base) throws Exception {
        Path backups = Files.createDirectories(base.resolve("junction-cleanup-bk"));
        Path victim = Files.createDirectories(base.resolve("junction-victim"));
        Files.writeString(victim.resolve("important.txt"), "owner-data");
        Path link = backups.resolve("tmp-0123456789abcdef0123456789abcdef");
        if (!createJunction(link, victim)) {
            System.out.println("     (junction creation unavailable on this host; skipped)");
            return;
        }
        new BackupManager(backups, 7, 1000, 1L << 24).cleanupTemp();
        check(Files.isRegularFile(victim.resolve("important.txt"), LinkOption.NOFOLLOW_LINKS),
                "cleanupTemp must never delete through a junction");
        check(!Files.exists(link, LinkOption.NOFOLLOW_LINKS), "the junction itself may be removed");
    }

    private static void pruneDoesNotFollowJunction(Path base) throws Exception {
        Path backups = Files.createDirectories(base.resolve("junction-prune-bk"));
        Path victim = Files.createDirectories(base.resolve("junction-prune-victim"));
        Files.writeString(victim.resolve("important.txt"), "owner-data");
        Path oldBackup = Files.createDirectories(backups.resolve("backup-1700000000000-000"));
        Files.createDirectories(backups.resolve("backup-1700000000001-000"));
        if (!createJunction(oldBackup.resolve("nested"), victim)) {
            System.out.println("     (junction creation unavailable on this host; skipped)");
            return;
        }
        new BackupManager(backups, 1, 1000, 1L << 24).prune(backups);
        check(Files.isRegularFile(victim.resolve("important.txt"), LinkOption.NOFOLLOW_LINKS),
                "prune must never delete through a nested junction");
        check(!Files.exists(oldBackup, LinkOption.NOFOLLOW_LINKS), "the old backup directory itself is pruned");
    }

    private static void httpTransport(Path base) throws Exception {
        PermitService s = service(base);
        int port;
        try (var probe = new java.net.ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress())) {
            port = probe.getLocalPort();
        }
        HttpGuard guard = new HttpGuard(s, port);
        guard.start();
        try {
            HttpClient client = HttpClient.newHttpClient();
            URI uri = URI.create("http://127.0.0.1:" + port + "/v1/permit");
            HttpRequest ok = HttpRequest.newBuilder(uri).header("Authorization", Fixtures.bearer())
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(Fixtures.request(Fixtures.nonce(), "connect", "")))
                    .build();
            HttpResponse<byte[]> r = client.send(ok, HttpResponse.BodyHandlers.ofByteArray());
            check(r.statusCode() == 200, "http status " + r.statusCode());
            String sig = r.headers().firstValue("X-PawBot-Signature").orElse("");
            check(sig.equals(Hmac.hmacSha256(Fixtures.SECRET.getBytes(StandardCharsets.UTF_8), r.body())),
                    "header signature covers the exact wire bytes");
            HttpResponse<byte[]> get = client.send(HttpRequest.newBuilder(uri).GET().build(),
                    HttpResponse.BodyHandlers.ofByteArray());
            check(get.statusCode() == 405, "GET " + get.statusCode());
            HttpResponse<byte[]> lookalike = client.send(HttpRequest.newBuilder(uri)
                    .header("Authorization", Fixtures.bearer())
                    .header("Content-Type", "application/jsonp")
                    .POST(HttpRequest.BodyPublishers.ofString(Fixtures.request(Fixtures.nonce(), "observe", "")))
                    .build(), HttpResponse.BodyHandlers.ofByteArray());
            check(lookalike.statusCode() == 415, "lookalike content-type " + lookalike.statusCode());
            HttpResponse<byte[]> charset = client.send(HttpRequest.newBuilder(uri)
                    .header("Authorization", Fixtures.bearer())
                    .header("Content-Type", "application/json; charset=utf-8")
                    .POST(HttpRequest.BodyPublishers.ofString(Fixtures.request(Fixtures.nonce(), "observe", "")))
                    .build(), HttpResponse.BodyHandlers.ofByteArray());
            check(charset.statusCode() == 200, "json charset parameter " + charset.statusCode());
            HttpResponse<byte[]> other = client.send(HttpRequest.newBuilder(
                    URI.create("http://127.0.0.1:" + port + "/v1/exec")).POST(HttpRequest.BodyPublishers.ofString("{}"))
                    .build(), HttpResponse.BodyHandlers.ofByteArray());
            check(other.statusCode() == 404, "other route " + other.statusCode());
            HttpResponse<byte[]> noAuth = client.send(HttpRequest.newBuilder(uri)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(Fixtures.request(Fixtures.nonce(), "connect", "")))
                    .build(), HttpResponse.BodyHandlers.ofByteArray());
            check(noAuth.statusCode() == 401 && noAuth.headers().firstValue("X-PawBot-Signature").isEmpty(),
                    "unauthenticated is bare 401");
            check(!new String(noAuth.body(), StandardCharsets.UTF_8).contains(base.toString()), "no path");
            HttpResponse<byte[]> big = client.send(HttpRequest.newBuilder(uri).header("Authorization", Fixtures.bearer())
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("x".repeat(PermitService.MAX_REQUEST_BYTES + 100)))
                    .build(), HttpResponse.BodyHandlers.ofByteArray());
            check(big.statusCode() == 413, "oversize http " + big.statusCode());
        } finally {
            guard.stop();
        }
    }

    private static void nonceBounds(Path base) {
        NonceCache cache = new NonceCache(3, 60);
        check(cache.claim("a", 100) && cache.claim("b", 100) && cache.claim("c", 100), "fill");
        check(!cache.claim("c", 101), "replay inside ttl");
        check(cache.claim("d", 101), "overflow admits");
        check(cache.size() <= 3, "bounded size " + cache.size());
        check(cache.claim("c", 161), "expired entry may be reused after 60s");
    }

    // ---------- snapshot gate / busy receipts / single schedule ----------

    private static PermitService threadedService(Path base, ThreadedFakeWorld[] out, int intervalSec)
            throws IOException {
        Path world = Fixtures.world(base);
        ThreadedFakeWorld worldView = new ThreadedFakeWorld(world);
        out[0] = worldView;
        GuardConfig cfg = Fixtures.config(base, world, true, true, intervalSec);
        BackupManager bm = new BackupManager(cfg.backupRoot, cfg.retention, cfg.maxBackupFiles,
                cfg.maxBackupBytes);
        Files.createDirectories(base.resolve("cfg"));
        return new PermitService(cfg, worldView, bm, base.resolve("cfg"));
    }

    /** Asserts a signed, structurally non-mutating busy receipt for {@code action}. */
    private static Map<String, String> checkBusy(PermitService.Response r, String action) throws Exception {
        check(r.status == 200, action + " busy status " + r.status + " " + r.bodyAsString());
        check(r.signature != null && r.signature.equals(
                Hmac.hmacSha256(Fixtures.SECRET.getBytes(StandardCharsets.UTF_8), r.body)),
                action + " busy signature must cover the exact bytes");
        Map<String, String> fields = bodyFields(r);
        check(fields.get("busy").equals("true"), action + " busy flag");
        check(fields.get("action").equals(action), action + " busy action echo");
        check(fields.get("can_write").equals("false") && fields.get("can_pvp").equals("false"),
                action + " busy receipt must advertise no capability");
        check(fields.get("backup_id") == null || fields.get("backup_id").isEmpty(),
                action + " busy receipt must carry no backup id");
        return fields;
    }

    private static void backupSchedulePolicy(Path base) {
        long s = 1_000_000_000L;
        BackupSchedule schedule = new BackupSchedule(60);
        schedule.markAttempt(0L);
        check(!schedule.due(59L * s), "not due before the interval");
        check(schedule.due(60L * s), "due exactly at the interval");
        schedule.skip();
        check(schedule.due(60L * s), "skip must not advance the deadline");
        schedule.markAttempt(60L * s);
        check(!schedule.due(119L * s), "a failed attempt still advances the deadline");
        check(schedule.due(120L * s), "next interval is due");

        BackupSchedule longInterval = new BackupSchedule(1800);
        longInterval.markAttempt(0L);
        check(!longInterval.due(1799L * s), "1800 s interval not due early");
        check(longInterval.due(1800L * s), "1800 s interval due exactly");
        check(schedule.spacingNanos() != longInterval.spacingNanos(), "intervals stay distinct");

        for (int bad : new int[] { 0, 59, 1801 }) {
            try {
                new BackupSchedule(bad);
                check(false, "accepted illegal interval " + bad);
            } catch (IllegalArgumentException expected) {
                // fail closed
            }
        }
    }

    private static void slowSnapshotServesBusyOffThread(Path base) throws Exception {
        ThreadedFakeWorld[] w = new ThreadedFakeWorld[1];
        PermitService service = threadedService(base, w, 1800);
        long t = 1_700_000_000L;
        check(Fixtures.callAt(service, Fixtures.nonce(), "observe", "", t).status == 200,
                "prime a live attestation first");
        w[0].blockSave();
        ExecutorService client = Executors.newSingleThreadExecutor();
        try {
            Future<PermitService.Response> copy = client.submit(
                    () -> Fixtures.callAt(service, Fixtures.nonce(), "connect", "", t));
            check(w[0].saveGateEntered.await(5, TimeUnit.SECONDS), "snapshot never reached saveAll");
            int submissions = w[0].submissions.get();
            for (String action : new String[] { "observe", "write", "pvp", "connect", "backup" }) {
                String target = action.equals("pvp") ? Fixtures.TARGET : "";
                long start = System.nanoTime();
                PermitService.Response busy = Fixtures.callAt(service, Fixtures.nonce(), action, target, t);
                long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
                check(elapsedMs < 250, action + " busy answer took " + elapsedMs + " ms");
                Map<String, String> fields = checkBusy(busy, action);
                check(fields.get("port").equals("25565"), action + " cached live port");
                check(fields.get("save_path").equals(w[0].savePath.toRealPath().toString()),
                        action + " cached canonical path");
                check(Long.parseLong(fields.get("attested_at")) > 0, "attested_at present");
            }
            check(w[0].submissions.get() == submissions,
                    "busy answers must never queue on the server thread");
            w[0].releaseSave();
            PermitService.Response connected = copy.get(15, TimeUnit.SECONDS);
            check(connected.status == 200, "connect after release " + connected.status);
            String id = bodyFields(connected).get("backup_id");
            check(id.startsWith("backup-"), "connect backup id " + id);
            PermitService.Response write = Fixtures.callAt(service, Fixtures.nonce(), "write", "", t);
            check(write.status == 200 && bodyFields(write).get("backup_id").equals(id),
                    "publish-before-unbusy: a write must read the new session, not the pre-copy one");
        } finally {
            w[0].releaseSave();
            client.shutdownNow();
            w[0].shutdown();
        }
    }

    private static void escapeReleasesQueuedObserve(Path base) throws Exception {
        ThreadedFakeWorld[] w = new ThreadedFakeWorld[1];
        PermitService service = threadedService(base, w, 1800);
        long t = 1_700_000_000L;
        check(Fixtures.callAt(service, Fixtures.nonce(), "observe", "", t).status == 200,
                "prime a live attestation first");
        w[0].blockSave();
        ExecutorService holder = Executors.newSingleThreadExecutor();
        ExecutorService client = Executors.newSingleThreadExecutor();
        ExecutorService tick = Executors.newSingleThreadExecutor();
        CountDownLatch holdEntered = new CountDownLatch(1);
        CountDownLatch holdRelease = new CountDownLatch(1);
        try {
            // Occupy the integrated-server thread so a client request is accepted and queued.
            Future<?> held = holder.submit(() -> {
                try {
                    return w[0].onServerThread(() -> {
                        holdEntered.countDown();
                        holdRelease.await(15, TimeUnit.SECONDS);
                        return null;
                    }, 20_000L);
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            });
            check(holdEntered.await(5, TimeUnit.SECONDS), "server thread was not held");
            Future<PermitService.Response> queued = client.submit(
                    () -> Fixtures.callAt(service, Fixtures.nonce(), "observe", "", t));
            awaitTrue(() -> w[0].submissions.get() >= 2, "observe was never queued on the server thread");
            // The tick snapshot opens the gate after the request was already queued.
            Future<String> snapshot = tick.submit(() -> service.periodicBackupAttemptOnServerThread(t));
            awaitTrue(service::isSnapshotBusy, "tick snapshot never opened the gate");
            long start = System.nanoTime();
            PermitService.Response escaped = queued.get(3, TimeUnit.SECONDS);
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
            check(elapsedMs < 1_000, "queued observe was not released in time: " + elapsedMs + " ms");
            checkBusy(escaped, "observe");
            w[0].releaseSave();
            String id = snapshot.get(15, TimeUnit.SECONDS);
            check(id != null && id.startsWith("backup-"), "tick snapshot completes: " + id);
            holdRelease.countDown();
            held.get(5, TimeUnit.SECONDS);
        } finally {
            holdRelease.countDown();
            w[0].releaseSave();
            holder.shutdownNow();
            client.shutdownNow();
            tick.shutdownNow();
            w[0].shutdown();
        }
    }

    private static void noMutationPermitDuringCopy(Path base) throws Exception {
        ThreadedFakeWorld[] w = new ThreadedFakeWorld[1];
        PermitService service = threadedService(base, w, 1800);
        long t = 1_700_000_000L;
        check(Fixtures.callAt(service, Fixtures.nonce(), "observe", "", t).status == 200, "prime");
        w[0].blockSave();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        ExecutorService copyThread = Executors.newSingleThreadExecutor();
        try {
            Future<PermitService.Response> copy = copyThread.submit(
                    () -> Fixtures.callAt(service, Fixtures.nonce(), "connect", "", t));
            check(w[0].saveGateEntered.await(5, TimeUnit.SECONDS), "copy never started");
            List<Future<PermitService.Response>> calls = new ArrayList<>();
            for (int i = 0; i < 60; i++) {
                final boolean pvp = i % 2 == 1;
                calls.add(pool.submit(() -> Fixtures.callAt(service, Fixtures.nonce(),
                        pvp ? "pvp" : "write", pvp ? Fixtures.TARGET : "", t)));
            }
            boolean sawBusy = false;
            for (Future<PermitService.Response> call : calls) {
                PermitService.Response r = call.get(10, TimeUnit.SECONDS);
                if (r.status == 503) {
                    continue;
                }
                check(r.status == 200, "unexpected status during copy: " + r.status);
                Map<String, String> fields = bodyFields(r);
                String backup = fields.get("backup_id");
                boolean canWrite = "true".equals(fields.get("can_write"));
                check(!(canWrite && backup != null && !backup.isEmpty()),
                        "a mutation permit was issued while a snapshot held the server thread");
                if ("true".equals(fields.get("busy"))) {
                    sawBusy = true;
                    check(fields.get("can_pvp").equals("false"), "busy pvp capability");
                }
            }
            check(sawBusy, "the snapshot gate never produced a busy answer");
            w[0].releaseSave();
            check(copy.get(15, TimeUnit.SECONDS).status == 200, "copy completes after release");
        } finally {
            w[0].releaseSave();
            pool.shutdownNow();
            copyThread.shutdownNow();
            w[0].shutdown();
        }
    }

    private static void concurrentRevokeDuringCopy(Path base) throws Exception {
        ThreadedFakeWorld[] w = new ThreadedFakeWorld[1];
        PermitService service = threadedService(base, w, 1800);
        long t = 1_700_000_000L;
        check(Fixtures.callAt(service, Fixtures.nonce(), "observe", "", t).status == 200, "prime");
        w[0].blockSave();
        ExecutorService client = Executors.newSingleThreadExecutor();
        try {
            Future<PermitService.Response> copy = client.submit(
                    () -> Fixtures.callAt(service, Fixtures.nonce(), "connect", "", t));
            check(w[0].saveGateEntered.await(5, TimeUnit.SECONDS), "copy never started");
            long start = System.nanoTime();
            PermitService.Response revoke = Fixtures.callAt(service, Fixtures.nonce(), "revoke", "", t);
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
            check(revoke.status == 200 && revoke.signature != null,
                    "revoke must answer during a copy: " + revoke.status);
            check(elapsedMs < 500, "revoke took " + elapsedMs + " ms during a copy");
            check(service.isRevoked(), "revocation latched before the receipt");
            check(Files.exists(base.resolve("cfg").resolve("pawbot-guard.revoked")),
                    "durable marker exists");
            check(Fixtures.callAt(service, Fixtures.nonce(), "observe", "", t).status == 409,
                    "no permit after revoke");
            w[0].releaseSave();
            PermitService.Response after = copy.get(15, TimeUnit.SECONDS);
            check(after.status == 409 && after.signature == null,
                    "the snapshot that completed after revoke must be refused: " + after.status);
            check(sessionBackupId(service).isEmpty(),
                    "a snapshot that finished after revoke must never publish");
        } finally {
            w[0].releaseSave();
            client.shutdownNow();
            w[0].shutdown();
        }
    }

    private static void busyReceiptContract(Path base) throws Exception {
        ThreadedFakeWorld[] w = new ThreadedFakeWorld[1];
        PermitService service = threadedService(base, w, 1800);
        long t = 1_700_000_000L;
        check(Fixtures.callAt(service, Fixtures.nonce(), "observe", "", t).status == 200, "prime");
        w[0].blockSave();
        ExecutorService client = Executors.newSingleThreadExecutor();
        try {
            Future<PermitService.Response> copy = client.submit(
                    () -> Fixtures.callAt(service, Fixtures.nonce(), "connect", "", t));
            check(w[0].saveGateEntered.await(5, TimeUnit.SECONDS), "copy never started");
            PermitService.Response busy = Fixtures.callAt(service, Fixtures.nonce(), "write", "", t);
            Map<String, String> fields = checkBusy(busy, "write");
            check(fields.keySet().equals(new java.util.LinkedHashSet<>(List.of("nonce", "world_id",
                    "save_path", "host", "port", "actor_uuid", "action", "target_uuid", "issued_at",
                    "expires_at", "can_write", "can_pvp", "backup_id", "busy", "retry_after_ms",
                    "attested_at"))), "busy field set " + fields.keySet());
            check(fields.get("retry_after_ms").equals("250"), "retry_after_ms");
            long issued = Long.parseLong(fields.get("issued_at"));
            long expires = Long.parseLong(fields.get("expires_at"));
            long attested = Long.parseLong(fields.get("attested_at"));
            check(expires == issued + 5, "busy lifetime");
            check(attested > 0 && attested <= issued + 1, "attested_at is a past attestation");
            check(busy.bodyAsString().contains("\"busy\":true"), "JSON boolean busy");
            check(busy.bodyAsString().contains("\"backup_id\":\"\""), "empty backup id JSON");
            w[0].releaseSave();
            check(copy.get(15, TimeUnit.SECONDS).status == 200, "copy completes");
            PermitService.Response normal = Fixtures.callAt(service, Fixtures.nonce(), "observe", "", t);
            check(normal.status == 200, "normal observe after release");
            Map<String, String> normalFields = bodyFields(normal);
            check(!normalFields.containsKey("busy") && !normalFields.containsKey("retry_after_ms")
                    && !normalFields.containsKey("attested_at"),
                    "normal permits keep exactly the 13 contract fields");
            check(normalFields.size() == 13, "normal permit field count " + normalFields.size());
        } finally {
            w[0].releaseSave();
            client.shutdownNow();
            w[0].shutdown();
        }
    }

    private static void periodicBackupSkipsWithoutAdvancing(Path base) throws Exception {
        ThreadedFakeWorld[] w = new ThreadedFakeWorld[1];
        PermitService service = threadedService(base, w, 1800);
        long t = 1_700_000_000L;
        w[0].blockSave();
        ExecutorService client = Executors.newSingleThreadExecutor();
        try {
            Future<PermitService.Response> copy = client.submit(
                    () -> Fixtures.callAt(service, Fixtures.nonce(), "connect", "", t));
            check(w[0].saveGateEntered.await(5, TimeUnit.SECONDS), "request snapshot never started");
            check(service.periodicBackupAttemptOnServerThread(t) == null,
                    "tick snapshot must skip while a request snapshot owns the gate");
            check(!service.periodicBackupOnServerThread(t).startsWith("backup-"),
                    "the legacy entry point also reports no snapshot, not a fake success");
            w[0].releaseSave();
            check(copy.get(15, TimeUnit.SECONDS).status == 200, "copy completes");
            String id = service.periodicBackupAttemptOnServerThread(t);
            check(id != null && id.startsWith("backup-"), "tick snapshot resumes once idle: " + id);
        } finally {
            w[0].releaseSave();
            client.shutdownNow();
            w[0].shutdown();
        }
    }

    private static void singleSnapshotAtATime(Path base) throws Exception {
        ThreadedFakeWorld[] w = new ThreadedFakeWorld[1];
        PermitService service = threadedService(base, w, 1800);
        long t = 1_700_000_000L;
        w[0].blockSave();
        ExecutorService client = Executors.newSingleThreadExecutor();
        try {
            Future<PermitService.Response> first = client.submit(
                    () -> Fixtures.callAt(service, Fixtures.nonce(), "connect", "", t));
            check(w[0].saveGateEntered.await(5, TimeUnit.SECONDS), "first snapshot never started");
            PermitService.Response second = Fixtures.callAt(service, Fixtures.nonce(), "backup", "", t);
            checkBusy(second, "backup");
            w[0].releaseSave();
            check(first.get(15, TimeUnit.SECONDS).status == 200, "first snapshot completes");
            long backups;
            try (var list = Files.list(base.resolve("backups"))) {
                backups = list.filter(p -> p.getFileName().toString().startsWith("backup-")).count();
            }
            check(backups == 1, "exactly one snapshot directory expected, got " + backups);
        } finally {
            w[0].releaseSave();
            client.shutdownNow();
            w[0].shutdown();
        }
    }

    private static void backupInterval60(Path base) throws Exception {
        ThreadedFakeWorld[] w = new ThreadedFakeWorld[1];
        PermitService service = threadedService(base, w, 60);
        long t = 1_700_000_000L;
        PermitService.Response connect = Fixtures.callAt(service, Fixtures.nonce(), "connect", "", t);
        check(connect.status == 200, "connect at 60 s interval: " + connect.status);
        check(Fixtures.callAt(service, Fixtures.nonce(), "write", "", t + 60).status == 200,
                "exactly 60 s old snapshot must be accepted");
        PermitService.Response stale = Fixtures.callAt(service, Fixtures.nonce(), "write", "", t + 61);
        check(stale.status == 403 && stale.bodyAsString().equals("backup stale"),
                "61 s must be refused at a 60 s interval: " + stale.status);
        w[0].shutdown();
    }

    /** Reads one HTTP response head (through CRLFCRLF) from a raw socket. */
    private static String readHttpHead(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        int b;
        while ((b = in.read()) >= 0) {
            out.write(b);
            byte[] bytes = out.toByteArray();
            int n = bytes.length;
            if (n >= 4 && bytes[n - 4] == '\r' && bytes[n - 3] == '\n'
                    && bytes[n - 2] == '\r' && bytes[n - 1] == '\n') {
                break;
            }
            if (n > 8192) {
                break;
            }
        }
        return out.toString(StandardCharsets.US_ASCII);
    }

    private static void httpRejectsUnauthenticatedSlowBody(Path base) throws Exception {
        PermitService service = service(base);
        int port = freeLoopbackPort();
        HttpGuard guard = new HttpGuard(service, port);
        guard.start();
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress("127.0.0.1", port), 2_000);
            socket.setSoTimeout(3_000);
            OutputStream out = socket.getOutputStream();
            out.write(("POST /v1/permit HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    + "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n")
                    .getBytes(StandardCharsets.US_ASCII));
            out.flush();
            long start = System.nanoTime();
            String head = readHttpHead(socket.getInputStream());
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
            check(head.contains("401"), "unauthenticated slow body must be answered 401: " + head);
            check(elapsedMs < 1_500, "401 must not wait for the body: " + elapsedMs + " ms");
        } finally {
            guard.stop();
        }
    }

    private static void httpReadDeadlineClosesSlowBody(Path base) throws Exception {
        PermitService service = service(base);
        int port = freeLoopbackPort();
        HttpGuard guard = new HttpGuard(service, port);
        guard.start();
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress("127.0.0.1", port), 2_000);
            socket.setSoTimeout(6_000);
            OutputStream out = socket.getOutputStream();
            out.write(("POST /v1/permit HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: "
                    + Fixtures.bearer() + "\r\nContent-Type: application/json\r\n"
                    + "Content-Length: 100\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
            out.flush();
            long start = System.nanoTime();
            boolean closed;
            try {
                closed = socket.getInputStream().read() < 0;
            } catch (IOException expectedReset) {
                closed = true;
            }
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
            check(closed, "an authenticated stalled body must be closed, not held forever");
            check(elapsedMs < 5_500, "read deadline did not fire: " + elapsedMs + " ms");
        } finally {
            guard.stop();
        }
    }

    private static void revokeReachableUnderSlowBody(Path base) throws Exception {
        PermitService service = service(base);
        int port = freeLoopbackPort();
        HttpGuard guard = new HttpGuard(service, port);
        guard.start();
        try (Socket stalled = new Socket()) {
            stalled.connect(new InetSocketAddress("127.0.0.1", port), 2_000);
            OutputStream out = stalled.getOutputStream();
            out.write(("POST /v1/permit HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: "
                    + Fixtures.bearer() + "\r\nContent-Type: application/json\r\n"
                    + "Content-Length: 100\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
            out.flush();
            // One worker is now stuck on the stalled body; revoke must still be served.
            long start = System.nanoTime();
            HttpClient client = HttpClient.newHttpClient();
            HttpResponse<byte[]> revoke = client.send(HttpRequest.newBuilder(
                    URI.create("http://127.0.0.1:" + port + "/v1/permit"))
                    .header("Authorization", Fixtures.bearer())
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(
                            Fixtures.request(Fixtures.nonce(), "revoke", "")))
                    .build(), HttpResponse.BodyHandlers.ofByteArray());
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
            check(revoke.statusCode() == 200, "revoke under a slow body: " + revoke.statusCode());
            check(elapsedMs < 1_500, "revoke was starved by a slow body: " + elapsedMs + " ms");
            check(revoke.headers().firstValue("X-PawBot-Signature").isPresent(),
                    "revoke receipt signature");
        } finally {
            guard.stop();
        }
    }

    private static int freeLoopbackPort() throws IOException {
        try (var probe = new java.net.ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress())) {
            return probe.getLocalPort();
        }
    }
}
