package dev.pawbot.guard;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.List;
import java.util.UUID;

/** Shared fixture helpers. Everything lives under a JUnit-created temp directory. */
final class Fixtures {

    static final String SECRET = "0123456789abcdef0123456789abcdef";
    static final String WORLD_ID = "pawbot-test-1";
    static final String ACTOR = "11111111-1111-1111-1111-111111111111";
    static final String TARGET = "22222222-2222-2222-2222-222222222222";

    private Fixtures() {
    }

    static Path world(Path base) throws IOException {
        Path world = base.resolve("world");
        Files.createDirectories(world.resolve("region"));
        Files.writeString(world.resolve(".pawbot-test-world"), WORLD_ID + "\n", StandardCharsets.UTF_8);
        Files.writeString(world.resolve("level.dat"), "synthetic", StandardCharsets.UTF_8);
        Files.writeString(world.resolve("region").resolve("r.0.0.mca"), "synthetic-region", StandardCharsets.UTF_8);
        Files.writeString(world.resolve("session.lock"), "lock", StandardCharsets.UTF_8);
        return world;
    }

    static GuardConfig config(Path base, Path world, boolean write, boolean pvp) {
        return config(base, world, write, pvp, 1800);
    }

    static GuardConfig config(Path base, Path world, boolean write, boolean pvp, int backupIntervalSec) {
        try {
            return GuardConfig.of(SECRET, WORLD_ID, world, ACTOR,
                    pvp ? List.of(TARGET) : List.of(), true, write, pvp, base.resolve("backups"), 7,
                    backupIntervalSec, 6198, 20000, 1L << 28);
        } catch (GuardConfig.ConfigException e) {
            throw new IllegalStateException(e);
        }
    }

    static String request(String nonce, String action, String target) {
        return "{\"nonce\":\"" + nonce + "\",\"actor_uuid\":\"" + ACTOR + "\",\"action\":\"" + action
                + "\",\"target_uuid\":\"" + target + "\"}";
    }

    static String nonce() {
        return UUID.randomUUID().toString().replace("-", "");
    }

    static String bearer() {
        return "Bearer " + SECRET;
    }

    static PermitService.Response call(PermitService service, String nonce, String action, String target) {
        return callAt(service, nonce, action, target, System.currentTimeMillis() / 1000L);
    }

    static PermitService.Response callAt(PermitService service, String nonce, String action, String target,
            long nowSeconds) {
        return service.handle(bearer(), "application/json",
                request(nonce, action, target).getBytes(StandardCharsets.UTF_8), nowSeconds);
    }

    /** Stable content fingerprint for "the backup was not modified by restore" assertions. */
    static String treeFingerprint(Path root) {
        if (!Files.exists(root)) {
            return "<missing>";
        }
        StringBuilder sb = new StringBuilder();
        try (var walk = Files.walk(root)) {
            walk.sorted().forEach(p -> {
                try {
                    sb.append(root.relativize(p)).append('|');
                    if (Files.isRegularFile(p)) {
                        sb.append(Files.size(p)).append('|');
                        sb.append(Integer.toHexString(java.util.Arrays.hashCode(Files.readAllBytes(p))));
                    } else {
                        sb.append("dir");
                    }
                    sb.append('\n');
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                }
            });
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        return sb.toString();
    }

    static void deleteTree(Path root) {
        if (root == null || !Files.exists(root)) {
            return;
        }
        try (var walk = Files.walk(root)) {
            walk.sorted(Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.deleteIfExists(p);
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                }
            });
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
