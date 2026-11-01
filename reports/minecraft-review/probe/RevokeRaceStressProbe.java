package dev.pawbot.guard;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Reviewer-only concurrent stress for the revoke/permit race (M1).
 *
 * <p>Runs many rounds in which a {@code connect} permit and a {@code revoke} are released
 * simultaneously. Invariants checked for every round:
 *
 * <ul>
 *   <li>the revoke receipt is a signed 200 and the service reports revoked afterwards;</li>
 *   <li>if the connect returned 200, its server-thread build completed strictly before the
 *       revoke returned. A build that finishes after the revoke may never be signed/published,
 *       so a 200 there is the original defect;</li>
 *   <li>no session backup is left published, and every request issued after the revoke is 409
 *       (connect, observe, write, backup).</li>
 * </ul>
 *
 * <p>Run from the repository root after {@code gradle build}:
 *
 * <pre>
 * $javac = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\javac.exe'
 * $java  = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\java.exe'
 * &amp; $javac --release 17 -encoding UTF-8 `
 *   -cp 'minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test' `
 *   -d $env:TEMP\pawbot-race-stress reports\minecraft-review\probe\RevokeRaceStressProbe.java
 * &amp; $java -cp "$env:TEMP\pawbot-race-stress;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" `
 *   dev.pawbot.guard.RevokeRaceStressProbe 100
 * </pre>
 */
public final class RevokeRaceStressProbe {

    public static void main(String[] args) throws Exception {
        int rounds = args.length > 0 ? Integer.parseInt(args[0]) : 100;
        int connect200 = 0;
        int connect409 = 0;
        int connectOther = 0;
        int late200 = 0;
        for (int round = 0; round < rounds; round++) {
            Path base = Files.createTempDirectory("pawbot-race-stress-");
            try {
                Path world = Fixtures.world(base);
                GuardConfig config = Fixtures.config(base, world, true, true);
                BackupManager backups = new BackupManager(config.backupRoot, config.retention,
                        config.maxBackupFiles, config.maxBackupBytes);
                Path cfgDir = base.resolve("cfg");
                Files.createDirectories(cfgDir);
                AtomicLong buildDone = new AtomicLong();
                WorldView view = new WorldView() {
                    @Override
                    public boolean isIntegrated() {
                        return true;
                    }

                    @Override
                    public Path savePath() {
                        return world;
                    }

                    @Override
                    public int lanPort() {
                        return 25565;
                    }

                    @Override
                    public <T> T onServerThread(Callable<T> task, long timeoutMs) throws Exception {
                        T value = task.call();
                        if (!Thread.currentThread().getName().contains("revoke-kick")) {
                            buildDone.compareAndSet(0L, System.nanoTime());
                        }
                        return value;
                    }

                    @Override
                    public void saveAll() {
                    }

                    @Override
                    public void disconnectActor(UUID actor) {
                    }
                };
                PermitService service = new PermitService(config, view, backups, cfgDir);
                ExecutorService pool = Executors.newFixedThreadPool(2);
                CountDownLatch start = new CountDownLatch(1);
                try {
                    Future<PermitService.Response> connect = pool.submit(() -> {
                        start.await();
                        return Fixtures.call(service, Fixtures.nonce(), "connect", "");
                    });
                    Future<PermitService.Response> revoke = pool.submit(() -> {
                        start.await();
                        // Vary the interleaving so both "connect won the lock" and
                        // "revoke won the lock" outcomes are exercised for real.
                        Thread.sleep(java.util.concurrent.ThreadLocalRandom.current().nextInt(0, 20));
                        return Fixtures.call(service, Fixtures.nonce(), "revoke", "");
                    });
                    start.countDown();
                    PermitService.Response r = revoke.get(15, TimeUnit.SECONDS);
                    long revokeDone = System.nanoTime();
                    PermitService.Response c = connect.get(15, TimeUnit.SECONDS);

                    if (r.status != 200 || r.signature == null) {
                        throw new AssertionError("round " + round + ": revoke status " + r.status);
                    }
                    if (!service.isRevoked()) {
                        throw new AssertionError("round " + round + ": latched revoke not visible");
                    }
                    if (c.status == 200) {
                        connect200++;
                        long done = buildDone.get();
                        if (done == 0L) {
                            throw new AssertionError("round " + round + ": 200 without a build");
                        }
                        if (done > revokeDone) {
                            late200++;
                            System.out.println("LATE200 round=" + round
                                    + " buildAfterRevokeNanos=" + (done - revokeDone));
                        }
                    } else if (c.status == 409) {
                        connect409++;
                    } else {
                        connectOther++;
                    }
                    if (!sessionId(service).isEmpty()) {
                        throw new AssertionError("round " + round + ": session published after revoke");
                    }
                    for (String action : new String[] { "connect", "observe", "write", "backup" }) {
                        PermitService.Response after = Fixtures.call(service, Fixtures.nonce(), action, "");
                        if (after.status != 409 || after.signature != null) {
                            throw new AssertionError("round " + round + ": " + action
                                    + " after revoke gave " + after.status);
                        }
                    }
                } finally {
                    pool.shutdownNow();
                }
            } finally {
                Fixtures.deleteTree(base);
            }
        }
        System.out.println("rounds=" + rounds + " connect200=" + connect200
                + " connect409=" + connect409 + " connectOther=" + connectOther
                + " late200=" + late200);
        if (late200 > 0 || connectOther > 0) {
            System.exit(1);
        }
    }

    private static String sessionId(PermitService service) throws Exception {
        java.lang.reflect.Field field = PermitService.class.getDeclaredField("sessionBackup");
        field.setAccessible(true);
        Object ref = field.get(service);
        java.lang.reflect.Field id = ref.getClass().getDeclaredField("id");
        id.setAccessible(true);
        return (String) id.get(ref);
    }
}
