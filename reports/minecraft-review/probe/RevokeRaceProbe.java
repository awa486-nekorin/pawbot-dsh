package dev.pawbot.guard;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Reviewer-only reproduction for the PermitService revoke/permit race.
 *
 * <p>Run from the repository root after `gradle build` (the build also produces
 * the classes this probe uses):
 *
 * <pre>
 * $javac = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\javac.exe'
 * $java  = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\java.exe'
 * New-Item -ItemType Directory -Force -Path $env:TEMP\pawbot-race | Out-Null
 * &amp; $javac --release 17 -encoding UTF-8 `
 *   -cp 'minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test' `
 *   -d $env:TEMP\pawbot-race reports\minecraft-review\probe\RevokeRaceProbe.java
 * &amp; $java -cp "$env:TEMP\pawbot-race;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" `
 *   dev.pawbot.guard.RevokeRaceProbe
 * </pre>
 *
 * <p>Expected current output: revoke returns 200, isRevoked is true, and the
 * blocked connect request still returns 200 with a new signed attestation and
 * a backup identifier.
 */
public final class RevokeRaceProbe {

    public static void main(String[] args) throws Exception {
        Path base = Files.createTempDirectory("pawbot-revoke-race-");
        Path world = base.resolve("world");
        Files.createDirectories(world);
        Files.writeString(world.resolve(".pawbot-test-world"), "pawbot-test-1\n");
        Path configDir = base.resolve("cfg");
        Files.createDirectories(configDir);

        GuardConfig config = GuardConfig.of(
                Fixtures.SECRET, "pawbot-test-1", world, Fixtures.ACTOR,
                List.of(), true, true, true, base.resolve("backups"),
                7, 1800, 6198, 1000, 1L << 24);
        BackupManager backups = new BackupManager(
                config.backupRoot, config.retention, config.maxBackupFiles,
                config.maxBackupBytes);

        AtomicInteger serverThreadCalls = new AtomicInteger();
        CountDownLatch firstTaskEntered = new CountDownLatch(1);
        CountDownLatch releaseFirstTask = new CountDownLatch(1);
        WorldView worldView = new WorldView() {
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
                if (serverThreadCalls.incrementAndGet() == 1) {
                    firstTaskEntered.countDown();
                    releaseFirstTask.await();
                }
                return task.call();
            }

            @Override
            public void saveAll() {
            }

            @Override
            public void disconnectActor(UUID actor) {
            }
        };

        PermitService service = new PermitService(config, worldView, backups, configDir);
        ExecutorService executor = Executors.newSingleThreadExecutor();
        try {
            Future<PermitService.Response> connect = executor.submit(() -> service.handle(
                    "Bearer " + Fixtures.SECRET, "application/json",
                    Fixtures.request(Fixtures.nonce(), "connect", "")
                            .getBytes(StandardCharsets.UTF_8),
                    1_700_000_000L));
            firstTaskEntered.await();

            PermitService.Response revoke = service.handle(
                    "Bearer " + Fixtures.SECRET, "application/json",
                    Fixtures.request(Fixtures.nonce(), "revoke", "")
                            .getBytes(StandardCharsets.UTF_8),
                    1_700_000_001L);
            System.out.println("revoke_status=" + revoke.status
                    + " isRevoked=" + service.isRevoked());

            releaseFirstTask.countDown();
            PermitService.Response after = connect.get();
            System.out.println("connect_after_revoke_status=" + after.status);
            System.out.println("connect_after_revoke_body=" + after.bodyAsString());
        } finally {
            executor.shutdownNow();
            Fixtures.deleteTree(base);
        }
    }
}
