package dev.pawbot.guard;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.integrated.IntegratedServer;
import net.minecraft.util.WorldSavePath;

/**
 * Entry point. The guard is bound to the integrated server lifecycle, never to a dedicated server,
 * and only starts when a valid local config and an already-marked disposable world are both
 * present.
 *
 * <p>Why server events instead of polling the client:
 * <ul>
 *   <li>{@link ServerLifecycleEvents#SERVER_STARTED} runs on the integrated-server thread after all
 *       worlds are live, which is exactly when the authoritative save root and LAN state exist;</li>
 *   <li>{@link ServerLifecycleEvents#SERVER_STOPPING} runs while the world is still open, so the
 *       loopback listener is closed before the save is unloaded;</li>
 *   <li>{@link ServerTickEvents#END_SERVER_TICK} schedules the periodic snapshot from the server
 *       thread, so the snapshot always sees a flushed, stable save.</li>
 * </ul>
 *
 * <p>Config lives at {@code <game dir>/config/pawbot-guard.json} and is never committed. The
 * revocation marker {@code config/pawbot-guard.revoked} survives restarts and must be deleted by
 * hand to re-arm the guard. Logs are static messages only: no secret, request body, world path or
 * token is ever written.
 */
public final class GuardMod implements ClientModInitializer {

    private static final String CONFIG_NAME = "pawbot-guard.json";
    private static final String REVOKE_NAME = "pawbot-guard.revoked";
    private static final String MARKER = ".pawbot-test-world";
    private static final long MAX_MARKER_BYTES = 4096L;
    private static final String LOG = "[pawbot-guard] ";

    private final Object lock = new Object();
    private volatile HttpGuard http;
    private volatile PermitService service;
    private volatile GuardConfig config;
    /** The single periodic-snapshot schedule; clients no longer run their own backup timers. */
    private volatile BackupSchedule schedule;

    @Override
    public void onInitializeClient() {
        ServerLifecycleEvents.SERVER_STARTED.register(this::onServerStarted);
        ServerLifecycleEvents.SERVER_STOPPING.register(this::onServerStopping);
        ServerTickEvents.END_SERVER_TICK.register(this::onServerTick);
        // Belt and braces for quitting the client before the server stopping event is delivered.
        ClientLifecycleEvents.CLIENT_STOPPING.register(client -> shutdown());
    }

    private void onServerStarted(MinecraftServer server) {
        if (!(server instanceof IntegratedServer) || server.isDedicated()) {
            log("refused: not an integrated server");
            return;
        }
        synchronized (lock) {
            shutdownLocked();
            Path configDir = FabricLoader.getInstance().getConfigDir();
            Path configFile = configDir.resolve(CONFIG_NAME);
            if (!Files.isRegularFile(configFile, LinkOption.NOFOLLOW_LINKS)) {
                // Absent config (or a link where a local file is required) is a normal off state.
                return;
            }
            GuardConfig loaded;
            try {
                loaded = GuardConfig.load(configFile);
            } catch (GuardConfig.ConfigException e) {
                log("refused: " + e.getMessage());
                return;
            }
            if (!loaded.enabled) {
                return;
            }
            if (!attestWorld(server, loaded)) {
                return;
            }
            if (Files.exists(configDir.resolve(REVOKE_NAME))) {
                log("refused: revoked (delete config/" + REVOKE_NAME + " to re-arm)");
                return;
            }
            BackupManager backups = new BackupManager(loaded.backupRoot, loaded.retention,
                    loaded.maxBackupFiles, loaded.maxBackupBytes);
            try {
                backups.cleanupTemp();
            } catch (Exception e) {
                log("refused: backup root unavailable");
                return;
            }
            PermitService permitService = new PermitService(loaded, new IntegratedWorldAdapter(server),
                    backups, configDir);
            if (permitService.isRevoked()) {
                log("refused: revoked (delete config/" + REVOKE_NAME + " to re-arm)");
                return;
            }
            // Server thread: prime the non-mutating attestation cache so a busy answer during the
            // first snapshot can still prove liveness/path/port instead of failing closed.
            permitService.primeAttestation(System.currentTimeMillis() / 1000L);
            HttpGuard guard = new HttpGuard(permitService, loaded.port);
            try {
                guard.start();
            } catch (Exception bindFailure) {
                // No fallback port: an unexpected listener would be worse than no guard.
                log("refused: bind failed on port " + loaded.port);
                return;
            }
            config = loaded;
            service = permitService;
            http = guard;
            BackupSchedule started = new BackupSchedule(loaded.backupIntervalSec);
            started.markAttempt(System.nanoTime());
            schedule = started;
            log("listening on 127.0.0.1:" + loaded.port);
            try {
                if (new IntegratedWorldAdapter(server).lanPort() == 0) {
                    log("world is not open to LAN yet; every permit will be denied until it is");
                }
            } catch (Exception ignored) {
                log("LAN state unavailable; permits fail closed until it can be read");
            }
        }
    }

    private void onServerStopping(MinecraftServer server) {
        shutdown();
    }

    private void onServerTick(MinecraftServer server) {
        PermitService current = service;
        GuardConfig loaded = config;
        BackupSchedule currentSchedule = schedule;
        if (current == null || loaded == null || currentSchedule == null) {
            return;
        }
        if (!(server instanceof IntegratedServer) || server.isDedicated()) {
            shutdown();
            return;
        }
        long nowNanos = System.nanoTime();
        if (!currentSchedule.due(nowNanos)) {
            return;
        }
        // Already on the integrated-server thread. The snapshot runs inline so it cannot be
        // interleaved with a save; it returns "" on any attestation or budget failure, and null when
        // another snapshot already owns the gate.
        String id = current.periodicBackupAttemptOnServerThread(System.currentTimeMillis() / 1000L);
        if (id == null) {
            // A request-driven snapshot owns the gate: retry next tick without losing the interval.
            currentSchedule.skip();
            return;
        }
        // A failed attempt still advances the deadline: no retry storm on a broken world.
        currentSchedule.markAttempt(nowNanos);
        if (!id.isEmpty()) {
            log("periodic backup ok");
        }
    }

    /**
     * Startup attestation. The authoritative path is the server's save root; the configured path is
     * only compared against it. Links, a missing marker and a wrong world_id all refuse startup.
     */
    private boolean attestWorld(MinecraftServer server, GuardConfig loaded) {
        Path reported;
        try {
            reported = server.getSavePath(WorldSavePath.ROOT).toAbsolutePath().normalize();
        } catch (Exception e) {
            log("refused: save path unavailable");
            return false;
        }
        try {
            Path realReported = PathSafety.realPathChecked(reported);
            Path realConfigured = loaded.savePath.toRealPath();
            if (!realReported.equals(realConfigured)) {
                log("refused: save path mismatch");
                return false;
            }
            Path marker = realReported.resolve(MARKER);
            if (!Files.isRegularFile(marker, LinkOption.NOFOLLOW_LINKS)) {
                log("refused: dedicated-world marker missing");
                return false;
            }
            if (Files.size(marker) > MAX_MARKER_BYTES) {
                log("refused: dedicated-world marker too large");
                return false;
            }
            String markerWorld = Files.readString(marker, StandardCharsets.UTF_8).trim();
            if (!markerWorld.equals(loaded.worldId)) {
                log("refused: dedicated-world marker mismatch");
                return false;
            }
            return true;
        } catch (Exception e) {
            log("refused: unsafe world path");
            return false;
        }
    }

    private void shutdown() {
        synchronized (lock) {
            shutdownLocked();
        }
    }

    private void shutdownLocked() {
        HttpGuard guard = http;
        http = null;
        service = null;
        config = null;
        schedule = null;
        if (guard != null) {
            guard.stop();
        }
    }

    private static void log(String message) {
        System.out.println(LOG + message);
    }
}
