package dev.pawbot.guard;

/**
 * Serializes snapshot-producing work and answers everything else without the integrated-server
 * queue.
 *
 * <p>A snapshot ({@code saveAll} plus a synchronous tree copy) runs on the integrated-server thread
 * and can take far longer than a client's permit deadline. Without this gate, a client liveness
 * probe that is queued behind that copy looks exactly like a dead guard. The gate fixes that by
 * separating the two questions:
 *
 * <ul>
 *   <li><b>Who may produce a snapshot?</b> At most one ticket at a time. A ticket is opened on the
 *       requesting thread <em>before</em> anything is submitted to the server thread, so a request
 *       can never pass the check and then queue behind a copy.</li>
 *   <li><b>Is the guard alive and not revoked?</b> Answered off-thread from a cache that is only
 *       ever filled by a live server-thread attestation. Busy answers are structurally
 *       non-mutating ({@code can_write=false}, {@code can_pvp=false}, empty {@code backup_id}) and
 *       are signed with the same HMAC as normal permits.</li>
 * </ul>
 *
 * <p>Every transition happens under one monitor so "is it busy", "who owns it" and "was it
 * revoked" can never disagree. The monitor is never held across world/disk work; the only callback
 * run under it is publication, which is a short in-memory swap.
 *
 * <p>Deliberately dependency-free so the JDK-only regression runner can exercise it without
 * Minecraft.
 */
final class SnapshotGate {

    /** How long a paused client should wait before asking again (bounded, client-validated). */
    static final int RETRY_AFTER_MS = 250;

    /** One snapshot attempt. Closed when it is aborted, superseded, or successfully committed. */
    static final class Ticket {
        long generation;
        boolean closed;
    }

    /**
     * Non-mutating attestation cache. Only a successful server-thread attestation fills it, and it
     * can never satisfy a write/PvP decision: those always re-attest on the server thread.
     */
    static final class Cache {
        final String canonicalSavePath;
        final int port;
        final long attestedAtSeconds;

        Cache(String canonicalSavePath, int port, long attestedAtSeconds) {
            this.canonicalSavePath = canonicalSavePath;
            this.port = port;
            this.attestedAtSeconds = attestedAtSeconds;
        }
    }

    private boolean active;
    private long generation;
    private Ticket owner;
    private Cache cache;
    private boolean revoked;

    /** Opens the snapshot gate, or returns {@code null} when another snapshot already owns it. */
    synchronized Ticket tryOpen() {
        if (active || revoked) {
            return null;
        }
        active = true;
        generation++;
        Ticket ticket = new Ticket();
        ticket.generation = generation;
        owner = ticket;
        return ticket;
    }

    synchronized boolean isActive() {
        return active;
    }

    synchronized boolean isRevoked() {
        return revoked;
    }

    /** A closed ticket must skip the copy; {@code null} means "no ticket" (never closed). */
    synchronized boolean isClosed(Ticket ticket) {
        return ticket != null && ticket.closed;
    }

    /**
     * Records a live attestation. A value that cannot be a real Open-to-LAN endpoint is never
     * cached, so a busy answer can never invent a path/port; the guard then fails closed with 503.
     */
    synchronized void remember(String canonicalSavePath, int port, long nowSeconds) {
        if (revoked || canonicalSavePath == null || canonicalSavePath.isEmpty()
                || port < 1024 || port > 65535) {
            return;
        }
        cache = new Cache(canonicalSavePath, port, nowSeconds);
    }

    /** The cache to use for a busy answer, or {@code null} when no live attestation exists. */
    synchronized Cache busyCache() {
        return active ? cache : null;
    }

    /**
     * Abort predicate polled while a request waits for the server thread. A revoke always aborts;
     * a request without a ticket aborts as soon as some snapshot owns the gate (the escape path);
     * a ticket owner aborts when its own ticket was closed underneath it.
     */
    synchronized boolean shouldAbort(Ticket ticket) {
        if (revoked) {
            return true;
        }
        if (ticket == null) {
            return active;
        }
        return ticket.closed;
    }

    synchronized void abortIfOpen(Ticket ticket) {
        if (ticket == null || ticket.closed) {
            return;
        }
        ticket.closed = true;
        if (owner == ticket) {
            active = false;
            owner = null;
        }
    }

    /**
     * Publishes the produced snapshot and releases the gate in one critical section: a write that
     * runs immediately after the copy must see the new session, never the pre-copy one.
     *
     * @param publish short in-memory publication; returning {@code false} vetoes the commit (used
     *     when a revoke landed after the build signed) and nothing is published
     * @return true when this ticket still owned an open, non-revoked, current generation
     */
    synchronized boolean commit(Ticket ticket, java.util.function.BooleanSupplier publish) {
        if (ticket == null || ticket.closed || !active || owner != ticket
                || revoked || ticket.generation != generation) {
            return false;
        }
        boolean accepted;
        try {
            accepted = publish.getAsBoolean();
        } catch (RuntimeException e) {
            accepted = false;
        }
        ticket.closed = true;
        active = false;
        owner = null;
        return accepted;
    }

    /** Invalidates every in-flight snapshot and forbids new ones; the latch is never cleared. */
    synchronized void revokeAll() {
        revoked = true;
        generation++;
        active = false;
        owner = null;
        cache = null;
    }
}
