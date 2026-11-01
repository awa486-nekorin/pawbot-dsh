package dev.pawbot.guard;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;

/** Fake integrated server over a temporary synthetic world directory. */
final class FakeWorld implements WorldView {

    boolean integrated = true;
    Path savePath;
    int port = 25565;
    boolean failServerThread;
    boolean timeOutAfterTask;
    long saveCount;
    final List<UUID> disconnected = Collections.synchronizedList(new ArrayList<>());

    /** When set, server-thread calls signal entry and wait for release before running. */
    CountDownLatch serverGateEntered;
    CountDownLatch serverGateRelease;
    /** How many calls enter the gate (default all); lets a revoke's kick pass a blocked permit. */
    int serverGateCalls = Integer.MAX_VALUE;
    private final java.util.concurrent.atomic.AtomicInteger serverGateHits =
            new java.util.concurrent.atomic.AtomicInteger();
    /** When set, saveAll signals entry and waits for release (simulates a slow post-save snapshot). */
    CountDownLatch saveGateEntered;
    CountDownLatch saveGateRelease;

    FakeWorld(Path savePath) {
        this.savePath = savePath;
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
        if (serverGateEntered != null && serverGateHits.getAndIncrement() < serverGateCalls) {
            serverGateEntered.countDown();
            serverGateRelease.await();
        }
        if (failServerThread) {
            throw new java.util.concurrent.TimeoutException("simulated");
        }
        if (timeOutAfterTask) {
            // Simulates a task that finished on the server thread but whose future was already
            // abandoned by the caller. Its side effects must not become visible.
            task.call();
            throw new java.util.concurrent.TimeoutException("simulated after task");
        }
        return task.call();
    }

    @Override
    public void saveAll() throws Exception {
        saveCount++;
        if (saveGateEntered != null) {
            saveGateEntered.countDown();
            saveGateRelease.await();
        }
    }

    @Override
    public void disconnectActor(UUID actor) {
        disconnected.add(actor);
    }
}
