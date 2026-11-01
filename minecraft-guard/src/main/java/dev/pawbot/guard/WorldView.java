package dev.pawbot.guard;

import java.nio.file.Path;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.function.BooleanSupplier;

/**
 * Narrow view of the integrated server. The Minecraft adapter implements this; tests use a fake
 * backed by temporary synthetic directories. Nothing here exposes commands, RCON, or arbitrary files.
 */
public interface WorldView {

    /**
     * Raised when a request that is still waiting for the server thread is released because a
     * snapshot gate opened (or a revoke landed) instead of making it wait for the whole copy.
     */
    final class BusyAborted extends Exception {
        private static final long serialVersionUID = 1L;

        public BusyAborted() {
            super("snapshot gate busy", null, false, false);
        }
    }

    /** True only for a real integrated (single-player/Open-to-LAN) server; dedicated servers say false. */
    boolean isIntegrated();

    /** Authoritative save root as reported by the server (WorldSavePath.ROOT). */
    Path savePath();

    /** Current Open-to-LAN port, or a value <= 0 when the world is not open to LAN. */
    int lanPort();

    /** Runs the task on the server thread and waits at most timeoutMs; times out fail-closed. */
    <T> T onServerThread(Callable<T> task, long timeoutMs) throws Exception;

    /**
     * Runs the task on the server thread but gives up as soon as {@code abortIf} becomes true,
     * raising {@link BusyAborted} instead of waiting behind a snapshot that owns the server thread.
     *
     * <p>The default delegates to the plain form so a minimal fake keeps working; the real adapter
     * implements the polling escape.
     */
    default <T> T onServerThread(Callable<T> task, long timeoutMs, BooleanSupplier abortIf)
            throws Exception {
        return onServerThread(task, timeoutMs);
    }

    /** Flushes world data to disk. Must only be called on the server thread. */
    void saveAll() throws Exception;

    /** Disconnects the registered actor if present. Must only be called on the server thread. */
    void disconnectActor(UUID actor);
}
