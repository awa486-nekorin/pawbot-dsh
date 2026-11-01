package dev.pawbot.guard;

import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;

/**
 * Reviewer-only probe: what the real Java guard returns when the persistent
 * revocation marker cannot be written.
 *
 * <pre>
 * $javac = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\javac.exe'
 * $java  = 'C:\Program Files\JetBrains\PyCharm 2025.2.4\jbr\bin\java.exe'
 * &amp; $javac --release 17 -encoding UTF-8 `
 *   -cp 'minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test' `
 *   -d $env:TEMP\pawbot-durability reports\minecraft-review\probe\RevokeDurabilityProbe.java
 * &amp; $java -cp "$env:TEMP\pawbot-durability;minecraft-guard\build\classes\java\main;minecraft-guard\build\classes\java\test" `
 *   dev.pawbot.guard.RevokeDurabilityProbe
 * </pre>
 *
 * <p>Expected: first revoke 503 unsigned, in-memory latch true, marker absent;
 * a second revoke is the sticky 409 {@code revoked} (not a signed receipt); a
 * reconstructed service over the same directory is not revoked.
 */
public final class RevokeDurabilityProbe {

    public static void main(String[] args) throws Exception {
        Path base = Files.createTempDirectory("pawbot-revoke-durability-");
        try {
            Path world = Fixtures.world(base);
            GuardConfig config = Fixtures.config(base, world, true, true);
            Path missingConfig = base.resolve("missing-config");
            PermitService service = new PermitService(config, new FakeWorld(world),
                    new BackupManager(config.backupRoot, config.retention,
                            config.maxBackupFiles, config.maxBackupBytes),
                    missingConfig);
            PermitService.Response first = Fixtures.call(service, Fixtures.nonce(), "revoke", "");
            System.out.println("first_status=" + first.status
                    + " signature=" + (first.signature != null)
                    + " body=" + first.bodyAsString());
            System.out.println("first_latched=" + service.isRevoked());
            System.out.println("marker=" + Files.exists(
                    missingConfig.resolve("pawbot-guard.revoked"), LinkOption.NOFOLLOW_LINKS));
            PermitService.Response second = Fixtures.call(service, Fixtures.nonce(), "revoke", "");
            System.out.println("second_status=" + second.status
                    + " signature=" + (second.signature != null)
                    + " body=" + second.bodyAsString());
            PermitService restarted = new PermitService(config, new FakeWorld(world),
                    new BackupManager(config.backupRoot, config.retention,
                            config.maxBackupFiles, config.maxBackupBytes),
                    missingConfig);
            System.out.println("restarted_revoked=" + restarted.isRevoked());
        } finally {
            Fixtures.deleteTree(base);
        }
    }
}
