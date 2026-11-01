package dev.pawbot.guard;

/**
 * Pure periodic-snapshot policy. The guard is the only scheduler: clients no longer run their own
 * backup timers, so a configured interval and the observed snapshots cannot disagree.
 *
 * <p>Semantics, all fail-safe:
 * <ul>
 *   <li>{@link #due(long)} is true when at least {@code max(interval, minSpacing)} has elapsed since
 *       the last <em>attempt</em>; exactly at the boundary counts as due.</li>
 *   <li>An attempt that ran (success or failure) advances the deadline, so a broken world cannot
 *       produce a retry storm on every tick.</li>
 *   <li>{@link #skip()} advances nothing: when a request-driven snapshot already owns the gate, the
 *       tick simply retries next tick instead of losing a whole interval.</li>
 * </ul>
 */
final class BackupSchedule {

    private final long intervalNanos;
    private final long minSpacingNanos;
    private long lastAttemptNanos = Long.MIN_VALUE;

    BackupSchedule(int intervalSec) {
        this(intervalSec, 1_000_000_000L);
    }

    BackupSchedule(int intervalSec, long minSpacingNanos) {
        if (intervalSec < 60 || intervalSec > 1800) {
            throw new IllegalArgumentException("backup interval out of range");
        }
        this.intervalNanos = intervalSec * 1_000_000_000L;
        this.minSpacingNanos = Math.max(0L, minSpacingNanos);
    }

    long spacingNanos() {
        return Math.max(minSpacingNanos, intervalNanos);
    }

    boolean due(long nowNanos) {
        return lastAttemptNanos == Long.MIN_VALUE || nowNanos - lastAttemptNanos >= spacingNanos();
    }

    void markAttempt(long nowNanos) {
        lastAttemptNanos = nowNanos;
    }

    /** A busy gate is not an attempt: keep the deadline so the next tick retries immediately. */
    void skip() {
        // Intentionally empty; named for the caller's intent and covered by a regression.
    }

    long lastAttemptNanos() {
        return lastAttemptNanos;
    }
}
