package dev.pawbot.guard;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * The permit decision point. Transport-free so it can be unit tested without Minecraft.
 *
 * <p>Order of checks is deliberate and fails closed at every step:
 * authentication, framing, external revocation marker, nonce replay, configured revocation, actor
 * identity, capability, then the server-thread world attestation. Nothing before authentication
 * ever reveals a path.
 */
public final class PermitService {

    public static final int MAX_REQUEST_BYTES = 4096;
    public static final int MAX_RESPONSE_BYTES = 16384;
    public static final long NORMAL_TIMEOUT_MS = 10_000L;
    public static final long BACKUP_TIMEOUT_MS = 60_000L;
    /** Generous bound for the best-effort disconnect, which never delays the revoke receipt. */
    private static final long KICK_TIMEOUT_MS = 120_000L;

    private static final Pattern NONCE_RE = Pattern.compile("^[0-9a-f]{32}$");
    private static final Pattern UUID_RE =
            Pattern.compile("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");
    private static final String MARKER = ".pawbot-test-world";
    private static final long MAX_MARKER_BYTES = 4096L;

    public enum Action {
        CONNECT, OBSERVE, WRITE, PVP, BACKUP, REVOKE;

        static Action parse(String raw) {
            if (raw == null) {
                return null;
            }
            switch (raw.toLowerCase(Locale.ROOT)) {
                case "connect":
                    return CONNECT;
                case "observe":
                    return OBSERVE;
                case "write":
                    return WRITE;
                case "pvp":
                    return PVP;
                case "backup":
                    return BACKUP;
                case "revoke":
                    return REVOKE;
                default:
                    return null;
            }
        }
    }

    /** Transport-neutral result. A non-200 status never carries a signature. */
    public static final class Response {
        public final int status;
        public final byte[] body;
        public final String signature;

        Response(int status, byte[] body, String signature) {
            this.status = status;
            this.body = body;
            this.signature = signature;
        }

        public String bodyAsString() {
            return new String(body, StandardCharsets.UTF_8);
        }
    }

    /** Immutable snapshot reference; swapped as one value so id and age can never disagree. */
    private static final class BackupRef {
        static final BackupRef NONE = new BackupRef("", 0L);
        final String id;
        final long atSeconds;

        BackupRef(String id, long atSeconds) {
            this.id = id;
            this.atSeconds = atSeconds;
        }
    }

    /**
     * A server-thread build result. It is signed inside the build (after the revocation re-check)
     * but its session is published by the waiting request only after the future completed
     * successfully, so a timed-out snapshot can never refresh freshness.
     */
    private static final class BuildResult {
        final Response response;
        final BackupRef newSession;

        BuildResult(Response response, BackupRef newSession) {
            this.response = response;
            this.newSession = newSession;
        }
    }

    private final GuardConfig config;
    private final WorldView world;
    private final BackupManager backups;
    private final NonceCache nonces = new NonceCache(4096, 60);
    private final Path revokeMarker;
    /**
     * Serializes snapshots and answers non-mutating probes off-thread while a copy owns the
     * integrated-server thread. See {@link SnapshotGate}.
     */
    private final SnapshotGate gate = new SnapshotGate();
    /**
     * Serializes revocation state transitions with permit publication. The lock is only held for
     * a state read/write and one final signature, never across a world save or snapshot, so revoke
     * is not blocked behind a slow server-thread task and the server thread never waits on a
     * world operation while holding it.
     */
    private final Object stateLock = new Object();
    /** Monotonic revocation generation: any permit that saw an older generation is refused. */
    private long revocationGeneration;

    private volatile boolean revoked;
    private volatile BackupRef sessionBackup = BackupRef.NONE;

    public PermitService(GuardConfig config, WorldView world, BackupManager backups, Path configDir) {
        this.config = config;
        this.world = world;
        this.backups = backups;
        this.revokeMarker = configDir.resolve("pawbot-guard.revoked");
        this.revoked = Files.exists(revokeMarker, LinkOption.NOFOLLOW_LINKS);
    }

    public boolean isRevoked() {
        return revoked;
    }

    public boolean isEnabled() {
        return config.enabled;
    }

    /** Exposes the loaded config to the owner of this service so periodic work shares one instance. */
    public GuardConfig config() {
        return config;
    }

    /** Exposes the backup manager for the periodic snapshot job. */
    public BackupManager backups() {
        return backups;
    }

    /**
     * Runs one snapshot on the server thread. Returns the backup id, or an empty string when the
     * world is not currently attestable or another snapshot owns the gate. Never revokes on
     * failure. Call from a non-server thread; a server-thread hook must use
     * {@link #periodicBackupOnServerThread(long)} instead.
     */
    public String periodicBackup() {
        if (revoked || !config.enabled) {
            return "";
        }
        refreshRevocation();
        long generation = currentGeneration();
        if (generation < 0) {
            return "";
        }
        SnapshotGate.Ticket ticket = gate.tryOpen();
        if (ticket == null) {
            return "";
        }
        try {
            return world.onServerThread(() -> periodicBackupOnServerThread(
                    System.currentTimeMillis() / 1000L, generation, ticket), BACKUP_TIMEOUT_MS,
                    () -> gate.shouldAbort(ticket));
        } catch (Exception e) {
            return "";
        } finally {
            gate.abortIfOpen(ticket);
        }
    }

    /**
     * Runs one snapshot inline. The caller must already be on the integrated-server thread; calling
     * this from a server-thread hook avoids re-submitting to the same thread and deadlocking.
     */
    public String periodicBackupOnServerThread(long nowSeconds) {
        String attempt = periodicBackupAttemptOnServerThread(nowSeconds);
        return attempt == null ? "" : attempt;
    }

    /**
     * One tick-driven snapshot attempt.
     *
     * @return {@code null} when another snapshot already owns the gate (the caller must not advance
     *     its schedule: retry next tick), otherwise the backup id or {@code ""} on failure
     */
    public String periodicBackupAttemptOnServerThread(long nowSeconds) {
        if (!config.enabled) {
            return "";
        }
        refreshRevocation();
        long generation = currentGeneration();
        if (generation < 0) {
            return "";
        }
        SnapshotGate.Ticket ticket = gate.tryOpen();
        if (ticket == null) {
            return null;
        }
        try {
            return periodicBackupOnServerThread(nowSeconds, generation, ticket);
        } finally {
            gate.abortIfOpen(ticket);
        }
    }

    /** True while a snapshot (request-driven or tick-driven) owns the server thread. */
    public boolean isSnapshotBusy() {
        return gate.isActive();
    }

    private String periodicBackupOnServerThread(long nowSeconds, long generation,
            SnapshotGate.Ticket ticket) {
        if (!config.enabled || !generationCurrent(generation) || gate.isClosed(ticket)) {
            return "";
        }
        try {
            if (!world.isIntegrated()) {
                return "";
            }
            Path root = world.savePath();
            if (root == null) {
                return "";
            }
            Path realRoot;
            try {
                realRoot = PathSafety.realPathChecked(root);
                if (!realRoot.equals(config.savePath.toRealPath())) {
                    return "";
                }
            } catch (IOException e) {
                return "";
            }
            if (!markerMatches(realRoot)) {
                return "";
            }
            rememberAttestation(realRoot, world.lanPort(), nowSeconds);
            world.saveAll();
            String id = backups.backup(realRoot);
            if (id == null || id.isEmpty()) {
                return "";
            }
            final BackupRef produced = new BackupRef(id, nowSeconds);
            // Publish-then-unbusy in one critical section, with the same revocation re-check a
            // request-driven snapshot uses. World/disk work above is deliberately outside every lock.
            boolean committed = gate.commit(ticket, () -> {
                synchronized (stateLock) {
                    if (!generationCurrentLocked(generation)) {
                        return false;
                    }
                    sessionBackup = produced;
                    return true;
                }
            });
            return committed ? id : "";
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * Server-thread only: refresh the non-mutating attestation cache used by busy answers. Fills
     * nothing unless the exact same checks a live permit performs succeed, so a busy observation can
     * never report a world path or port that was not attested.
     */
    public void primeAttestation(long nowSeconds) {
        if (gate.isRevoked() || !config.enabled) {
            return;
        }
        try {
            if (!world.isIntegrated()) {
                return;
            }
            Path root = world.savePath();
            if (root == null) {
                return;
            }
            Path realRoot = PathSafety.realPathChecked(root);
            if (!realRoot.equals(config.savePath.toRealPath()) || !markerMatches(realRoot)) {
                return;
            }
            rememberAttestation(realRoot, world.lanPort(), nowSeconds);
        } catch (Exception ignored) {
            // No cache: busy answers fail closed with 503 instead of guessing a path/port.
        }
    }

    private void rememberAttestation(Path realRoot, int port, long nowSeconds) {
        if (port < 1024 || port > 65535) {
            return;
        }
        try {
            gate.remember(realRoot.toRealPath().toString(), port, nowSeconds);
        } catch (IOException e) {
            gate.remember(PathSafety.normalizedAbsolute(realRoot).toString(), port, nowSeconds);
        }
    }

    /**
     * Handles one request. {@code authorization} is the raw header value, {@code contentType} the
     * raw header, {@code body} at most {@link #MAX_REQUEST_BYTES} bytes. {@code nowSeconds} is
     * server time supplied by the transport; tests use it to exercise freshness boundaries.
     */
    public Response handle(String authorization, String contentType, byte[] body, long nowSeconds) {
        if (!authenticate(authorization)) {
            return error(401, "unauthorized");
        }
        if (!isJsonContentType(contentType)) {
            return error(415, "unsupported media type");
        }
        if (body == null || body.length == 0 || body.length > MAX_REQUEST_BYTES) {
            return error(413, "invalid size");
        }
        // An operator can latch the emergency stop by creating the marker while the guard is
        // already listening; check it on every authenticated request, not only at startup.
        refreshRevocation();
        if (revoked) {
            return error(409, "revoked");
        }
        String text;
        try {
            text = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(body)).toString();
        } catch (CharacterCodingException e) {
            return error(400, "bad request");
        }
        Map<String, String> req;
        try {
            req = Json.parseFlatStringObject(text);
        } catch (Json.JsonException e) {
            return error(400, "bad request");
        }
        if (!req.keySet().equals(Set.of("nonce", "actor_uuid", "action", "target_uuid"))) {
            return error(400, "bad request");
        }
        String nonce = req.get("nonce");
        String actorRaw = req.get("actor_uuid");
        String actionRaw = req.get("action");
        String targetRaw = req.get("target_uuid");
        if (nonce == null || !NONCE_RE.matcher(nonce).matches()
                || actorRaw == null || !UUID_RE.matcher(actorRaw).matches()
                || actionRaw == null || targetRaw == null) {
            return error(400, "bad request");
        }
        Action action = Action.parse(actionRaw);
        if (action == null) {
            return error(400, "bad request");
        }
        UUID actor = UUID.fromString(actorRaw);
        UUID target = null;
        if (!targetRaw.isEmpty()) {
            if (!UUID_RE.matcher(targetRaw).matches()) {
                return error(400, "bad request");
            }
            target = UUID.fromString(targetRaw);
        }

        // Claim before any decision so a denied nonce cannot be retried for a different answer.
        if (!nonces.claim(nonce, nowSeconds)) {
            return error(409, "replay");
        }
        // Capture the revocation generation in the same critical section that checks the latch.
        // The server-thread build re-checks it before doing any work and again at publication, so a
        // revoke that lands while this request is queued or mid-snapshot always wins.
        long generation = currentGeneration();
        if (generation < 0) {
            return error(409, "revoked");
        }
        if (!config.enabled) {
            return error(403, "disabled");
        }
        if (!actor.equals(config.actorUuid)) {
            return error(403, "actor rejected");
        }
        if (action == Action.PVP) {
            if (target == null || !config.pvpTargets.contains(target)) {
                return error(403, "target rejected");
            }
        } else if (target != null) {
            return error(403, "target not allowed");
        }
        if (action == Action.WRITE && !config.writeEnabled) {
            return error(403, "write disabled");
        }
        // PvP mutates the world too, and a consumer that trusts only can_pvp must not receive a
        // permit while writes are disabled. The signed capability is coupled below as well.
        if (action == Action.PVP && !config.writeEnabled) {
            return error(403, "write disabled");
        }
        if (action == Action.PVP && !config.pvpEnabled) {
            return error(403, "pvp disabled");
        }

        if (action == Action.REVOKE) {
            // Revocation is never gated and never queued behind a snapshot.
            return revoke(nonce);
        }
        // A snapshot may own the server thread for far longer than a client deadline. Answer a
        // non-mutating busy receipt off-thread first; never queue behind the copy. If no live
        // attestation was ever cached, fail closed with 503 instead of inventing a path/port.
        if (gate.isActive()) {
            return busyOrUnavailable(nonce, action, target);
        }
        boolean snapshotAction = action == Action.BACKUP || action == Action.CONNECT;
        // Open the gate BEFORE submitting to the server thread, so a request cannot pass the check
        // and then wait behind a copy the tick job started.
        SnapshotGate.Ticket ticket = snapshotAction ? gate.tryOpen() : null;
        if (snapshotAction && ticket == null) {
            return busyOrUnavailable(nonce, action, target);
        }
        long timeout = snapshotAction ? BACKUP_TIMEOUT_MS : NORMAL_TIMEOUT_MS;
        final Action decided = action;
        final UUID decidedTarget = target;
        final SnapshotGate.Ticket decidedTicket = ticket;
        try {
            // The build signs only after its own revocation re-check; publication happens in
            // finish() after the future completed in time. The escape predicate releases a request
            // that queued behind a snapshot that started in the meantime (or a revoke) instead of
            // making it wait for the whole copy.
            BuildResult result = world.onServerThread(
                    () -> build(nonce, decided, decidedTarget, nowSeconds, generation, decidedTicket),
                    timeout, () -> gate.shouldAbort(decidedTicket));
            return finish(nonce, decided, decidedTarget, result, generation, decidedTicket);
        } catch (WorldView.BusyAborted busy) {
            gate.abortIfOpen(decidedTicket);
            return busyOrUnavailable(nonce, decided, decidedTarget);
        } catch (Exception e) {
            // Includes timeouts: a task that could not run on the server thread is a refusal.
            return error(503, "unavailable");
        } finally {
            gate.abortIfOpen(decidedTicket);
        }
    }

    /**
     * Applies a completed build. Publication is ordered against {@link #revoke(String)}; a revoke
     * that landed after the build signed still wins and the already-signed bytes are discarded. A
     * timed-out build never reaches this method, so it cannot publish a session. Publish-then-unbusy
     * happens in one gate critical section so a write immediately after the copy reads the new
     * session, never the pre-copy one.
     */
    private Response finish(String nonce, Action action, UUID target, BuildResult result,
            long generation, SnapshotGate.Ticket ticket) {
        // Also re-read the external marker here: an operator can create it while this request was
        // already past its server-thread build.
        refreshRevocation();
        if (ticket == null) {
            synchronized (stateLock) {
                if (!generationCurrentLocked(generation)) {
                    return error(409, "revoked");
                }
                return result.response;
            }
        }
        boolean committed = gate.commit(ticket, () -> {
            synchronized (stateLock) {
                if (!generationCurrentLocked(generation)) {
                    return false;
                }
                if (result.newSession != null) {
                    sessionBackup = result.newSession;
                }
                return true;
            }
        });
        if (!committed) {
            refreshRevocation();
            if (revoked) {
                return error(409, "revoked");
            }
            return busyOrUnavailable(nonce, action, target);
        }
        return result.response;
    }

    /** Off-thread signed busy answer, or a fail-closed 503 when no live attestation is cached. */
    private Response busyOrUnavailable(String nonce, Action action, UUID target) {
        refreshRevocation();
        if (revoked) {
            return error(409, "revoked");
        }
        SnapshotGate.Cache cache = gate.busyCache();
        if (cache == null) {
            return error(503, "unavailable");
        }
        return signedBusy(nonce, action, target, cache);
    }

    /**
     * Runs on the integrated-server thread: attestation plus (when needed) a fresh snapshot.
     *
     * <p>Revocation is checked at entry (a request may have waited behind another server task) and
     * again under {@link #stateLock} immediately before signing. The session swap happens in
     * {@link #finish} after the future completed in time, under the same lock and re-check, so a
     * permit cannot be signed or published after a revoke has been acknowledged. No world save or
     * snapshot ever runs while the lock is held.
     */
    private BuildResult build(String nonce, Action action, UUID target, long nowSeconds, long generation,
            SnapshotGate.Ticket ticket) throws Exception {
        // A snapshot ticket that was aborted while this task was queued must not copy the world.
        if (gate.isClosed(ticket)) {
            return reject(503, "abandoned");
        }
        // A request can wait behind another server task, and an operator can create the marker in
        // the meantime; re-read it inside the build as well as at signing/publication.
        refreshRevocation();
        if (!generationCurrent(generation)) {
            return reject(409, "revoked");
        }
        if (!world.isIntegrated()) {
            return reject(403, "not integrated");
        }
        Path root = world.savePath();
        if (root == null) {
            return reject(403, "world unavailable");
        }
        Path realRoot;
        try {
            realRoot = PathSafety.realPathChecked(root);
        } catch (IOException e) {
            return reject(403, "world rejected");
        }
        try {
            if (!realRoot.equals(config.savePath.toRealPath())) {
                return reject(403, "world rejected");
            }
        } catch (IOException e) {
            return reject(403, "world rejected");
        }
        if (!markerMatches(realRoot)) {
            return reject(403, "marker mismatch");
        }
        int port = world.lanPort();
        if (port < 1024 || port > 65535) {
            return reject(403, "lan closed");
        }
        // Live attestation succeeded: this is the only place the non-mutating busy cache is filled.
        rememberAttestation(realRoot, port, nowSeconds);

        BackupRef reference;
        BackupRef publish = null;
        synchronized (stateLock) {
            if (!generationCurrentLocked(generation)) {
                return reject(409, "revoked");
            }
            reference = sessionBackup;
        }
        switch (action) {
            case BACKUP: {
                world.saveAll();
                String id = createBackup(realRoot);
                if (id == null || id.isEmpty()) {
                    return reject(503, "unavailable");
                }
                reference = new BackupRef(id, nowSeconds);
                publish = reference;
                break;
            }
            case CONNECT: {
                // Every connection starts from a freshly flushed snapshot.
                world.saveAll();
                String id = createBackup(realRoot);
                if (id == null || id.isEmpty()) {
                    return reject(503, "unavailable");
                }
                reference = new BackupRef(id, nowSeconds);
                publish = reference;
                break;
            }
            case WRITE:
            case PVP:
                // Bounded: no per-request world copy, but the snapshot must still be fresh.
                // The guard is the freshness authority (never longer than backupIntervalSec, which
                // config caps at 1800 s), so a downstream caller cannot widen this window.
                if (reference.id.isEmpty() || reference.atSeconds <= 0L) {
                    return reject(403, "backup required");
                }
                long age = nowSeconds - reference.atSeconds;
                if (age < -1L || age > config.backupIntervalSec) {
                    return reject(403, "backup stale");
                }
                break;
            default:
                break;
        }
        return signIfCurrent(nonce, action, target, realRoot, port, reference.id, generation, publish);
    }

    /**
     * Final check immediately before signing. Runs on the server thread but holds
     * {@code stateLock} for a hash only; the session swap happens later in {@link #finish} after the
     * caller proved the future completed in time, in the gate's publish-then-unbusy critical section.
     */
    private BuildResult signIfCurrent(String nonce, Action action, UUID target, Path realRoot, int port,
            String backupId, long generation, BackupRef publish) {
        refreshRevocation();
        synchronized (stateLock) {
            if (!generationCurrentLocked(generation)) {
                return reject(409, "revoked");
            }
            return new BuildResult(signed(nonce, action, target, realRoot, port, backupId), publish);
        }
    }

    private static BuildResult reject(int status, String code) {
        return new BuildResult(error(status, code), null);
    }

    private boolean markerMatches(Path realRoot) {
        Path marker = realRoot.resolve(MARKER);
        if (!Files.isRegularFile(marker, LinkOption.NOFOLLOW_LINKS)) {
            return false;
        }
        try {
            if (Files.size(marker) > MAX_MARKER_BYTES) {
                return false;
            }
            return Files.readString(marker, StandardCharsets.UTF_8).trim().equals(config.worldId);
        } catch (IOException e) {
            return false;
        }
    }

    private String createBackup(Path realRoot) throws IOException {
        return backups.backup(realRoot);
    }

    /**
     * Latches revocation, persists and verifies the marker, then answers immediately. The receipt
     * is signed only when the marker is durable; otherwise the in-memory latch still refuses every
     * permit, but the response is an unsigned 503 so the host reports an incomplete stop instead of
     * claiming a revocation that a restart would forget. The disconnect kick is queued on its own
     * daemon thread so a slow save/snapshot cannot delay the response.
     */
    private Response revoke(String nonce) {
        synchronized (stateLock) {
            revoked = true;
            revocationGeneration++;
            sessionBackup = BackupRef.NONE;
        }
        // Invalidate every in-flight snapshot ticket and forbid new ones. Never called while
        // stateLock is held: gate.commit takes gate then stateLock, and this takes stateLock then
        // (after releasing it) the gate, so the two orders cannot deadlock.
        gate.revokeAll();
        boolean durable = persistRevocationMarker();
        disconnectActorBestEffort();
        if (!durable) {
            return error(503, "unavailable");
        }
        return signed(nonce, Action.REVOKE, null, config.savePath.toAbsolutePath(), currentLanPort(), "");
    }

    /**
     * Creates the marker with CREATE_NEW + NOFOLLOW_LINKS, flushes it, then verifies it. A
     * concurrent revoke can win the create race; the durability check then accepts the existing
     * regular marker. A symlink, missing directory, read-only disk or wrong content is not durable.
     */
    private boolean persistRevocationMarker() {
        if (markerDurable()) {
            return true;
        }
        try (FileChannel channel = FileChannel.open(revokeMarker,
                Set.of(StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS))) {
            ByteBuffer payload = ByteBuffer.wrap("revoked\n".getBytes(StandardCharsets.UTF_8));
            while (payload.hasRemaining()) {
                channel.write(payload);
            }
            channel.force(true);
        } catch (IOException | RuntimeException e) {
            // Fall through: a concurrent revoke may have created the marker already.
        }
        return markerDurable();
    }

    private boolean markerDurable() {
        if (!Files.isRegularFile(revokeMarker, LinkOption.NOFOLLOW_LINKS)) {
            return false;
        }
        try {
            return Files.readString(revokeMarker, StandardCharsets.UTF_8).trim().equals("revoked");
        } catch (IOException e) {
            return false;
        }
    }

    /** Best-effort kick; never blocks the revoke response and never runs on the server thread. */
    private void disconnectActorBestEffort() {
        Thread kick = new Thread(() -> {
            try {
                world.onServerThread(() -> {
                    world.disconnectActor(config.actorUuid);
                    return null;
                }, KICK_TIMEOUT_MS);
            } catch (Exception ignored) {
                // The revocation still holds; the actor simply was not connected or not kicked.
            }
        }, "pawbot-guard-revoke-kick");
        kick.setDaemon(true);
        kick.start();
    }

    /** Current LAN port for an informational receipt; 0 is valid for revoke when LAN is closed. */
    private int currentLanPort() {
        try {
            int port = world.lanPort();
            return port >= 0 && port <= 65535 ? port : 0;
        } catch (Exception e) {
            return 0;
        }
    }

    private long currentGeneration() {
        synchronized (stateLock) {
            return revoked ? -1L : revocationGeneration;
        }
    }

    private boolean generationCurrent(long generation) {
        synchronized (stateLock) {
            return generationCurrentLocked(generation);
        }
    }

    /** Caller must hold {@link #stateLock}. */
    private boolean generationCurrentLocked(long generation) {
        return !revoked && generation == revocationGeneration;
    }

    private Response signed(String nonce, Action action, UUID target, Path realRoot, int port, String backupId) {
        // Signing time is captured after the (possibly slow) server-thread backup, so a snapshot
        // that takes seconds cannot produce a receipt that is already expired when it is written.
        long issued = System.currentTimeMillis() / 1000L;
        String canonical;
        try {
            canonical = realRoot.toRealPath().toString();
        } catch (IOException e) {
            // Revocation must still return a signed receipt even if the world briefly vanished;
            // the normalized path is what was configured, and it is only sent over an authenticated channel.
            canonical = PathSafety.normalizedAbsolute(realRoot).toString();
        }
        StringBuilder sb = new StringBuilder(512);
        sb.append('{');
        field(sb, "nonce", nonce).append(',');
        field(sb, "world_id", config.worldId).append(',');
        field(sb, "save_path", canonical).append(',');
        field(sb, "host", "127.0.0.1").append(',');
        sb.append("\"port\":").append(port).append(',');
        field(sb, "actor_uuid", config.actorUuid.toString()).append(',');
        field(sb, "action", action.name().toLowerCase(Locale.ROOT)).append(',');
        field(sb, "target_uuid", target == null ? "" : target.toString()).append(',');
        sb.append("\"issued_at\":").append(issued).append(',');
        sb.append("\"expires_at\":").append(issued + 5).append(',');
        sb.append("\"can_write\":").append(config.writeEnabled).append(',');
        sb.append("\"can_pvp\":").append(config.pvpEnabled && config.writeEnabled).append(',');
        field(sb, "backup_id", backupId);
        sb.append('}');
        byte[] body = sb.toString().getBytes(StandardCharsets.UTF_8);
        if (body.length > MAX_RESPONSE_BYTES) {
            return error(503, "response too large");
        }
        return new Response(200, body, Hmac.hmacSha256(config.secret, body));
    }

    /**
     * Signed, structurally non-mutating answer for a request that arrived while a snapshot owns the
     * server thread. The path and port come from the last successful live attestation only; the
     * signature covers the exact bytes like every other 200. Because {@code backup_id} is empty and
     * both capabilities are false, no client can accept this as a mutation permit.
     */
    private Response signedBusy(String nonce, Action action, UUID target, SnapshotGate.Cache cache) {
        long issued = System.currentTimeMillis() / 1000L;
        StringBuilder sb = new StringBuilder(512);
        sb.append('{');
        field(sb, "nonce", nonce).append(',');
        field(sb, "world_id", config.worldId).append(',');
        field(sb, "save_path", cache.canonicalSavePath).append(',');
        field(sb, "host", "127.0.0.1").append(',');
        sb.append("\"port\":").append(cache.port).append(',');
        field(sb, "actor_uuid", config.actorUuid.toString()).append(',');
        field(sb, "action", action.name().toLowerCase(Locale.ROOT)).append(',');
        field(sb, "target_uuid", target == null ? "" : target.toString()).append(',');
        sb.append("\"issued_at\":").append(issued).append(',');
        sb.append("\"expires_at\":").append(issued + 5).append(',');
        sb.append("\"can_write\":false,");
        sb.append("\"can_pvp\":false,");
        field(sb, "backup_id", "").append(',');
        sb.append("\"busy\":true,");
        sb.append("\"retry_after_ms\":").append(SnapshotGate.RETRY_AFTER_MS).append(',');
        sb.append("\"attested_at\":").append(cache.attestedAtSeconds);
        sb.append('}');
        byte[] body = sb.toString().getBytes(StandardCharsets.UTF_8);
        if (body.length > MAX_RESPONSE_BYTES) {
            return error(503, "response too large");
        }
        return new Response(200, body, Hmac.hmacSha256(config.secret, body));
    }

    private void refreshRevocation() {
        if (revoked || !Files.exists(revokeMarker, LinkOption.NOFOLLOW_LINKS)) {
            return;
        }
        boolean latched = false;
        synchronized (stateLock) {
            if (!revoked) {
                revoked = true;
                revocationGeneration++;
                sessionBackup = BackupRef.NONE;
                latched = true;
            }
        }
        if (latched) {
            // Outside stateLock: see the lock-order note in revoke().
            gate.revokeAll();
        }
    }

    /**
     * Transport pre-check used by {@link HttpGuard} to reject an unauthenticated peer before the
     * request body is read, so an unauthenticated slow body cannot hold a worker.
     */
    public boolean hasValidBearer(String authorization) {
        return authenticate(authorization);
    }

    private static boolean isJsonContentType(String contentType) {
        if (contentType == null) {
            return false;
        }
        int semicolon = contentType.indexOf(';');
        String mediaType = (semicolon < 0 ? contentType : contentType.substring(0, semicolon)).trim();
        return mediaType.equalsIgnoreCase("application/json");
    }

    private static StringBuilder field(StringBuilder sb, String key, String value) {
        sb.append(Json.quote(key)).append(':').append(Json.quote(value));
        return sb;
    }

    private static Response error(int status, String code) {
        return new Response(status, code.getBytes(StandardCharsets.UTF_8), null);
    }

    private boolean authenticate(String authorization) {
        if (authorization == null) {
            return false;
        }
        String prefix = "Bearer ";
        if (authorization.length() <= prefix.length() || !authorization.startsWith(prefix)) {
            return false;
        }
        String presented = authorization.substring(prefix.length());
        if (presented.isEmpty() || presented.length() > 256) {
            return false;
        }
        return Hmac.constantTimeEquals(Hmac.utf8(presented), config.secret);
    }

    /** Exposed for diagnostics: never includes nonce contents or secrets. */
    public List<String> describe() {
        return List.of("enabled=" + config.enabled, "revoked=" + revoked, "port=" + config.port,
                "targets=" + config.pvpTargets.size());
    }
}
