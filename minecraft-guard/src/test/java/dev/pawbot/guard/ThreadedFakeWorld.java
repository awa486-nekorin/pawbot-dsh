package dev.pawbot.guard;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;

/**
 * Fake integrated server backed by one real single-thread executor. Unlike {@link FakeWorld}, which
 * runs tasks on the calling thread, this models "the server thread is blocked inside a snapshot while
 * another thread asks for a permit" — the exact shape of the F3 defect.
 */
final class ThreadedFakeWorld implements WorldView {

    boolean integrated = true;
    Path savePath;
    int port = 25565;
    /** Every server-thread submission, including escaped ones, so a test can prove none happened. */
    final AtomicInteger submissions = new AtomicInteger();
    long saveCount;
    final List<UUID> disconnected = Collections.synchronizedList(new ArrayList<>());

    private final ExecutorService serverThread =
            Executors.newSingleThreadExecutor(r -> {
                Thread t = new Thread(r, "fake-integrated-server");
                t.setDaemon(true);
                return t;
            });

    /** When set, saveAll signals entry and waits for release (a snapshot that outlives any client). */
    CountDownLatch saveGateEntered;
    CountDownLatch saveGateRelease;

    ThreadedFakeWorld(Path savePath) {
        this.savePath = savePath;
    }

    void blockSave() {
        saveGateEntered = new CountDownLatch(1);
        saveGateRelease = new CountDownLatch(1);
    }

    void releaseSave() {
        if (saveGateRelease != null) {
            saveGateRelease.countDown();
        }
    }

    void shutdown() {
        serverThread.shutdownNow();
    }

    @Override
    public boolean isIntegrated() {
        return integrated;
    }

    @Override
    public Path savePath() {
        return savePath;
    }

    @Override
    public int lanPort() {
        return port;
    }

    @Override
    public <T> T onServerThread(Callable<T> task, long timeoutMs) throws Exception {
        Future<T> future = serverThread.submit(task);
        submissions.incrementAndGet();
        try {
            return future.get(Math.max(1L, timeoutMs), TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            future.cancel(false);
            throw e;
        } catch (ExecutionException e) {
            throw unwrap(e);
        } catch (InterruptedException e) {
            future.cancel(false);
            Thread.currentThread().interrupt();
            throw e;
        }
    }

    @Override
    public <T> T onServerThread(Callable<T> task, long timeoutMs, BooleanSupplier abortIf)
            throws Exception {
        Future<T> future = serverThread.submit(task);
        submissions.incrementAndGet();
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(Math.max(1L, timeoutMs));
        while (true) {
            if (abortIf != null && abortIf.getAsBoolean()) {
                future.cancel(false);
                throw new BusyAborted();
            }
            long remainingMs = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime());
            if (remainingMs <= 0) {
                future.cancel(false);
                throw new TimeoutException("fake server thread timeout");
            }
            try {
                return future.get(Math.min(20L, remainingMs), TimeUnit.MILLISECONDS);
            } catch (TimeoutException slice) {
                // poll the abort predicate again
            } catch (ExecutionException e) {
                throw unwrap(e);
            } catch (InterruptedException e) {
                future.cancel(false);
                Thread.currentThread().interrupt();
                throw e;
            }
        }
    }

    private static Exception unwrap(ExecutionException e) {
        Throwable cause = e.getCause();
        if (cause instanceof Exception) {
            return (Exception) cause;
        }
        return new IllegalStateException("fake server task failed");
    }

    @Override
    public void saveAll() throws Exception {
        saveCount++;
        CountDownLatch entered = saveGateEntered;
        CountDownLatch release = saveGateRelease;
        if (entered != null && release != null) {
            entered.countDown();
            release.await(10, TimeUnit.SECONDS);
        }
    }

    @Override
    public void disconnectActor(UUID actor) {
        disconnected.add(actor);
    }
}
