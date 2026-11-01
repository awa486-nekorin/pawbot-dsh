package dev.pawbot.guard;

import java.nio.file.Path;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.function.BooleanSupplier;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.integrated.IntegratedServer;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.Text;
import net.minecraft.util.WorldSavePath;

/**
 * Minecraft adapter for {@link WorldView}.
 *
 * <p>Kept deliberately thin: every Minecraft-version-specific accessor lives here, so a mapping
 * change is a one-file fix. The authoritative save root comes from
 * {@link MinecraftServer#getSavePath(WorldSavePath)} and never from configuration; configuration is
 * only compared against it.
 *
 * <p>Version-specific notes for 1.20.1 / Yarn ({@code 1.20.1+build.10}):
 * <ul>
 *   <li>save root: {@code getSavePath(WorldSavePath.ROOT)};</li>
 *   <li>dedicated flag: {@code isDedicated()} (not {@code isDedicatedServer()});</li>
 *   <li>LAN port: {@code getServerPort()}, combined with {@code isRemote()} so a world that was
 *       never opened to LAN reports 0 rather than the internal local-connection port;</li>
 *   <li>server thread: {@code submit(Supplier)} from {@code ThreadExecutor}; a checked exception
 *       thrown by the task is wrapped and rethrown to the caller.</li>
 * </ul>
 */
public final class IntegratedWorldAdapter implements WorldView {

    private final MinecraftServer server;

    public IntegratedWorldAdapter(MinecraftServer server) {
        this.server = server;
    }

    @Override
    public boolean isIntegrated() {
        return server instanceof IntegratedServer && !server.isDedicated();
    }

    @Override
    public Path savePath() {
        return server.getSavePath(WorldSavePath.ROOT).toAbsolutePath().normalize();
    }

    /**
     * Port a client can connect to, or 0 when the world is not open to LAN.
     *
     * <p>An integrated server always binds an internal port for the host's own client, so the raw
     * port alone does not prove Open-to-LAN. {@code isRemote()} is the vanilla signal that the
     * server is accepting remote players; a world that was never published returns 0 here and every
     * permit is denied until it is.
     */
    @Override
    public int lanPort() {
        if (!server.isRemote()) {
            return 0;
        }
        int port = server.getServerPort();
        return port > 0 ? port : 0;
    }

    @Override
    public <T> T onServerThread(Callable<T> task, long timeoutMs) throws Exception {
        if (task == null) {
            throw new IllegalArgumentException("task");
        }
        CompletableFuture<T> future = submit(task);
        try {
            return future.get(Math.max(1L, timeoutMs), TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            // Fail closed: the task must not run later, after the caller was already refused.
            future.cancel(false);
            throw e;
        } catch (ExecutionException | InterruptedException e) {
            if (e instanceof InterruptedException) {
                future.cancel(false);
                Thread.currentThread().interrupt();
            }
            throw unwrap(e, future);
        }
    }

    /**
     * Submits once, then polls in short slices. If {@code abortIf} turns true (a snapshot gate
     * opened, or a revoke landed) the queued task is cancelled and {@link BusyAborted} is raised, so
     * a client learns "busy" instead of waiting for the whole copy and treating the guard as dead.
     */
    @Override
    public <T> T onServerThread(Callable<T> task, long timeoutMs, BooleanSupplier abortIf)
            throws Exception {
        if (task == null) {
            throw new IllegalArgumentException("task");
        }
        CompletableFuture<T> future = submit(task);
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(Math.max(1L, timeoutMs));
        try {
            while (true) {
                if (abortIf != null && abortIf.getAsBoolean()) {
                    future.cancel(false);
                    throw new BusyAborted();
                }
                long remainingMs = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime());
                if (remainingMs <= 0) {
                    future.cancel(false);
                    throw new TimeoutException("server thread timeout");
                }
                try {
                    return future.get(Math.min(50L, remainingMs), TimeUnit.MILLISECONDS);
                } catch (TimeoutException slice) {
                    // Not the caller's deadline: poll the abort predicate again.
                }
            }
        } catch (ExecutionException | InterruptedException e) {
            if (e instanceof InterruptedException) {
                future.cancel(false);
                Thread.currentThread().interrupt();
            }
            throw unwrap(e, future);
        }
    }

    private <T> CompletableFuture<T> submit(Callable<T> task) {
        // ThreadExecutor.submit takes a Supplier, and a Supplier cannot throw checked exceptions.
        // Wrap the checked failure in a private runtime marker; the caller below unwraps it so the
        // WorldView contract still propagates the original exception.
        return server.submit(() -> {
            try {
                return task.call();
            } catch (Exception e) {
                throw new ServerTaskFailure(e);
            }
        });
    }

    private static Exception unwrap(Exception e, CompletableFuture<?> future) {
        future.cancel(false);
        if (e instanceof InterruptedException) {
            return (InterruptedException) e;
        }
        Throwable cause = e.getCause();
        if (cause instanceof ServerTaskFailure) {
            return ((ServerTaskFailure) cause).failure;
        }
        if (cause instanceof Exception) {
            return (Exception) cause;
        }
        return new IllegalStateException("server task failed");
    }

    @Override
    public void saveAll() {
        server.saveAll(true, true, true);
    }

    @Override
    public void disconnectActor(UUID actor) {
        if (actor == null) {
            return;
        }
        for (ServerPlayerEntity player : server.getPlayerManager().getPlayerList()) {
            if (actor.equals(player.getUuid())) {
                player.networkHandler.disconnect(Text.literal("PawBot guard: permit revoked"));
            }
        }
    }

    /** Carries the original checked failure out of a {@link java.util.function.Supplier}. */
    private static final class ServerTaskFailure extends RuntimeException {
        private static final long serialVersionUID = 1L;
        private final Exception failure;

        ServerTaskFailure(Exception failure) {
            super(null, null, false, false);
            this.failure = failure;
        }
    }
}
