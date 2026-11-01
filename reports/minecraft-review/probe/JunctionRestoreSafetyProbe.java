package dev.pawbot.guard;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.List;

/**
 * Reviewer-only, independent reproduction for F1/F4/S4 using real NTFS junctions.
 *
 * <p>Creates junctions with {@code cmd /c mklink /J} under a synthetic temp root and
 * exercises the *built* guard classes (no source edits). It does not touch a game,
 * world save, credential or the repository working tree.
 *
 * <p>Run from the repository root after {@code gradle build}:
 *
 * <pre>
 * $javac = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\javac.exe'
 * $java  = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\java.exe'
 * &amp; $javac --release 17 -encoding UTF-8 `
 *   -cp 'minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test' `
 *   -d $env:TEMP\pawbot-junction-verify reports\minecraft-review\probe\JunctionRestoreSafetyProbe.java
 * &amp; $java -cp "$env:TEMP\pawbot-junction-verify;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" `
 *   dev.pawbot.guard.JunctionRestoreSafetyProbe
 * </pre>
 */
public final class JunctionRestoreSafetyProbe {

    private static int failures;

    public static void main(String[] args) throws Exception {
        Path base = Files.createTempDirectory("pawbot-junction-probe-");
        try {
            cleanupTempJunction(base.resolve("cleanup"), base.resolve("victim-cleanup"));
            pruneJunction(base.resolve("prune"), base.resolve("victim-prune"));
            restoreTargetRace(base.resolve("race"));
            restoreFailureJunction(base.resolve("restore-fail"), base.resolve("victim-restore"));
            deepRunningWorld(base.resolve("deep"));
        } finally {
            Fixtures.deleteTree(base);
        }
        System.out.println(failures == 0 ? "ALL PROBES PASSED" : "FAILURES=" + failures);
        if (failures != 0) {
            System.exit(1);
        }
    }

    private static void cleanupTempJunction(Path root, Path victim) throws Exception {
        try {
            Files.createDirectories(root);
            Files.createDirectories(victim.resolve("sub"));
            Files.writeString(victim.resolve("important.txt"), "keep", StandardCharsets.UTF_8);
            Files.writeString(victim.resolve("sub/nested.txt"), "keep", StandardCharsets.UTF_8);
            Path link = root.resolve("tmp-0123456789abcdef0123456789abcdef");
            if (!junction(link, victim)) {
                System.out.println("cleanupTemp junction: SKIPPED (mklink unavailable)");
                return;
            }
            new BackupManager(root, 7, 1000, 1L << 24).cleanupTemp();
            boolean victimKept = Files.isRegularFile(victim.resolve("important.txt"))
                    && Files.isRegularFile(victim.resolve("sub/nested.txt"));
            boolean linkGone = !Files.exists(link, LinkOption.NOFOLLOW_LINKS);
            System.out.println("cleanupTemp junction: victim_kept=" + victimKept
                    + " link_removed=" + linkGone);
            if (!victimKept || !linkGone) {
                failures++;
            }
        } catch (Exception e) {
            failures++;
            System.out.println("cleanupTemp junction: FAILED " + e.getClass().getSimpleName());
        }
    }

    private static void pruneJunction(Path root, Path victim) throws Exception {
        try {
            Files.createDirectories(root);
            Files.createDirectories(victim);
            Files.writeString(victim.resolve("important.txt"), "keep", StandardCharsets.UTF_8);
            Path old = Files.createDirectories(root.resolve("backup-1700000000000-000"));
            Files.createDirectories(root.resolve("backup-1700000000001-000"));
            Path nested = old.resolve("nested");
            if (!junction(nested, victim)) {
                System.out.println("prune junction: SKIPPED (mklink unavailable)");
                return;
            }
            int removed = new BackupManager(root, 1, 1000, 1L << 24).prune(root);
            boolean victimKept = Files.isRegularFile(victim.resolve("important.txt"));
            boolean oldGone = !Files.exists(old, LinkOption.NOFOLLOW_LINKS);
            System.out.println("prune junction: removed=" + removed + " victim_kept=" + victimKept
                    + " old_backup_removed=" + oldGone);
            if (!victimKept || !oldGone || removed != 1) {
                failures++;
            }
        } catch (Exception e) {
            failures++;
            System.out.println("prune junction: FAILED " + e.getClass().getSimpleName());
        }
    }

    private static void restoreTargetRace(Path base) throws Exception {
        try {
            Files.createDirectories(base);
            Path world = Fixtures.world(base);
            BackupManager backups = new BackupManager(base.resolve("backups"), 7, 20000, 1L << 28);
            String id = backups.backup(world);
            Path target = base.resolve("race-target");
            Path sentinel = target.resolve("sentinel.txt");
            try {
                RestoreTool.restore(base.resolve("backups"), id, target, Fixtures.WORLD_ID, null,
                        () -> {
                            try {
                                Files.createDirectory(target);
                                Files.writeString(sentinel, "concurrent");
                            } catch (IOException e) {
                                throw new RuntimeException(e);
                            }
                        });
                System.out.println("restore target race: FAILED (accepted a concurrent target)");
                failures++;
            } catch (IOException expected) {
                boolean sentinelIntact = Files.isRegularFile(sentinel)
                        && "concurrent".equals(Files.readString(sentinel));
                System.out.println("restore target race: refused=" + expected.getMessage()
                        + " sentinel_intact=" + sentinelIntact);
                if (!sentinelIntact) {
                    failures++;
                }
            }
        } catch (Exception e) {
            failures++;
            System.out.println("restore target race: FAILED " + e.getClass().getSimpleName());
        }
    }

    private static void restoreFailureJunction(Path base, Path victim) throws Exception {
        try {
            Files.createDirectories(base);
            Files.createDirectories(victim);
            Files.writeString(victim.resolve("important.txt"), "keep", StandardCharsets.UTF_8);
            Path world = Fixtures.world(base);
            BackupManager backups = new BackupManager(base.resolve("backups"), 7, 20000, 1L << 28);
            String id = backups.backup(world);
            Path target = base.resolve("restore-target");
            Path staging = base.resolve("restore-target.restoring");
            try {
                RestoreTool.restore(base.resolve("backups"), id, target, Fixtures.WORLD_ID, null,
                        () -> {
                            try {
                                // Replace the copied empty region dir with a junction to the victim:
                                // restore must fail closed and its cleanup must delete only the link.
                                Path region = staging.resolve("region");
                                Fixtures.deleteTree(region);
                                if (!junction(region, victim)) {
                                    throw new RuntimeException("mklink unavailable");
                                }
                            } catch (IOException | InterruptedException e) {
                                throw new RuntimeException(e);
                            }
                        });
                System.out.println("restore failure junction: FAILED (accepted a linked staging entry)");
                failures++;
            } catch (IOException | RuntimeException expected) {
                boolean victimKept = Files.isRegularFile(victim.resolve("important.txt"));
                boolean stagingGone = !Files.exists(staging, LinkOption.NOFOLLOW_LINKS);
                boolean targetGone = !Files.exists(target, LinkOption.NOFOLLOW_LINKS);
                System.out.println("restore failure junction: refused="
                        + expected.getClass().getSimpleName() + " victim_kept=" + victimKept
                        + " staging_removed=" + stagingGone + " partial_target_removed=" + targetGone);
                if (!victimKept || !stagingGone || !targetGone) {
                    failures++;
                }
            }
        } catch (Exception e) {
            failures++;
            System.out.println("restore failure junction: FAILED " + e.getClass().getSimpleName());
        }
    }

    private static void deepRunningWorld(Path base) throws Exception {
        try {
            Files.createDirectories(base);
            Path world = Fixtures.world(base);
            Path live = base.resolve("live-world");
            Files.createDirectories(live);
            Files.writeString(live.resolve("session.lock"), "live", StandardCharsets.UTF_8);
            BackupManager backups = new BackupManager(base.resolve("backups"), 7, 20000, 1L << 28);
            String id = backups.backup(world);
            Path nested = live;
            for (int i = 0; i < 70; i++) {
                nested = Files.createDirectory(nested.resolve("d"));
            }
            Path target = nested.resolve("restored");
            try {
                RestoreTool.restore(base.resolve("backups"), id, target, Fixtures.WORLD_ID, null);
                System.out.println("70-deep running ancestor: FAILED (accepted)");
                failures++;
            } catch (IOException expected) {
                boolean absent = !Files.exists(target, LinkOption.NOFOLLOW_LINKS);
                System.out.println("70-deep running ancestor: refused=" + expected.getMessage()
                        + " target_absent=" + absent);
                if (!absent) {
                    failures++;
                }
            }
        } catch (Exception e) {
            failures++;
            System.out.println("70-deep running ancestor: FAILED " + e.getClass().getSimpleName());
        }
    }

    private static boolean junction(Path link, Path target) throws IOException, InterruptedException {
        Process p = new ProcessBuilder("cmd.exe", "/c", "mklink", "/J", link.toString(), target.toString())
                .redirectErrorStream(true).start();
        p.getInputStream().readAllBytes();
        return p.waitFor() == 0 && Files.isDirectory(link, LinkOption.NOFOLLOW_LINKS);
    }
}
