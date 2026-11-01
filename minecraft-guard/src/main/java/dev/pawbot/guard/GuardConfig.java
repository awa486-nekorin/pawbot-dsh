package dev.pawbot.guard;

import java.io.IOException;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.InvalidPathException;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * Local guard configuration (never checked in). Validation is strict and fail-closed: any problem
 * throws {@link ConfigException} whose message is static and never contains a value.
 */
public final class GuardConfig {

    public static final class ConfigException extends Exception {
        private static final long serialVersionUID = 1L;

        public ConfigException(String message) {
            super(message);
        }
    }

    private static final Pattern UUID_RE =
            Pattern.compile("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");
    private static final Pattern WORLD_ID_RE = Pattern.compile("^[A-Za-z0-9._-]{1,64}$");
    private static final long MAX_CONFIG_BYTES = 65536;

    public final byte[] secret;
    public final String worldId;
    public final Path savePath;
    public final UUID actorUuid;
    public final Set<UUID> pvpTargets;
    public final boolean enabled;
    public final boolean writeEnabled;
    public final boolean pvpEnabled;
    public final Path backupRoot;
    public final int retention;
    public final int backupIntervalSec;
    public final int port;
    public final int maxBackupFiles;
    public final long maxBackupBytes;

    private GuardConfig(byte[] secret, String worldId, Path savePath, UUID actorUuid, Set<UUID> pvpTargets,
            boolean enabled, boolean writeEnabled, boolean pvpEnabled, Path backupRoot, int retention,
            int backupIntervalSec, int port, int maxBackupFiles, long maxBackupBytes) {
        this.secret = secret;
        this.worldId = worldId;
        this.savePath = savePath;
        this.actorUuid = actorUuid;
        this.pvpTargets = pvpTargets;
        this.enabled = enabled;
        this.writeEnabled = writeEnabled;
        this.pvpEnabled = pvpEnabled;
        this.backupRoot = backupRoot;
        this.retention = retention;
        this.backupIntervalSec = backupIntervalSec;
        this.port = port;
        this.maxBackupFiles = maxBackupFiles;
        this.maxBackupBytes = maxBackupBytes;
    }

    /** Test/programmatic constructor with the same validation as file loading. */
    public static GuardConfig of(String secret, String worldId, Path savePath, String actorUuid,
            List<String> pvpUuids, boolean enabled, boolean writeEnabled, boolean pvpEnabled, Path backupRoot,
            int retention, int backupIntervalSec, int port, int maxBackupFiles, long maxBackupBytes)
            throws ConfigException {
        if (secret == null || secret.length() < 32 || secret.length() > 256) {
            throw new ConfigException("secret must be 32-256 characters");
        }
        if (worldId == null || !WORLD_ID_RE.matcher(worldId).matches()) {
            throw new ConfigException("invalid world_id");
        }
        UUID actor = strictUuid(actorUuid, "actor_uuid");
        Set<UUID> targets = new LinkedHashSet<>();
        for (String raw : pvpUuids) {
            UUID u = strictUuid(raw, "pvp_uuids");
            if (u.equals(actor)) {
                throw new ConfigException("pvp target equals actor");
            }
            targets.add(u);
        }
        if (targets.size() > 16) {
            throw new ConfigException("too many pvp targets");
        }
        if (savePath == null || backupRoot == null || !savePath.isAbsolute() || !backupRoot.isAbsolute()) {
            throw new ConfigException("paths must be absolute");
        }
        if (retention < 1 || retention > 30) {
            throw new ConfigException("retention out of range");
        }
        // 1800 s (30 min) is the shared ceiling: the guard is the freshness authority and refuses
        // any interval longer than that. A shorter explicit local value is allowed.
        if (backupIntervalSec < 60 || backupIntervalSec > 1800) {
            throw new ConfigException("backup interval out of range");
        }
        if (port < 1024 || port > 65535) {
            throw new ConfigException("port out of range");
        }
        if (maxBackupFiles < 1 || maxBackupFiles > 200000) {
            throw new ConfigException("max_backup_files out of range");
        }
        if (maxBackupBytes < 1024 || maxBackupBytes > (16L << 30)) {
            throw new ConfigException("max_backup_bytes out of range");
        }
        try {
            PathSafety.requireNoLinks(savePath);
            PathSafety.requireNoLinks(backupRoot);
            PathSafety.requireDisjointRoots(savePath, backupRoot);
        } catch (IOException e) {
            throw new ConfigException("unsafe path");
        }
        return new GuardConfig(secret.getBytes(StandardCharsets.UTF_8), worldId,
                PathSafety.normalizedAbsolute(savePath), actor, Collections.unmodifiableSet(targets), enabled,
                writeEnabled, pvpEnabled, PathSafety.normalizedAbsolute(backupRoot), retention,
                backupIntervalSec, port, maxBackupFiles, maxBackupBytes);
    }

    public static GuardConfig load(Path file) throws ConfigException {
        String text;
        try {
            if (!Files.isRegularFile(file, LinkOption.NOFOLLOW_LINKS)) {
                throw new ConfigException("config must be a regular file");
            }
            if (Files.size(file) > MAX_CONFIG_BYTES) {
                throw new ConfigException("config too large");
            }
            byte[] raw = Files.readAllBytes(file);
            text = StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(raw)).toString();
        } catch (CharacterCodingException e) {
            throw new ConfigException("config is not utf-8");
        } catch (IOException e) {
            throw new ConfigException("config unreadable");
        }
        Object parsed;
        try {
            parsed = Json.parse(text);
        } catch (Json.JsonException e) {
            throw new ConfigException("config is not valid json");
        }
        if (!(parsed instanceof Map)) {
            throw new ConfigException("config must be an object");
        }
        Map<?, ?> m = (Map<?, ?>) parsed;
        Set<String> allowed = new HashSet<>(List.of("secret", "world_id", "save_path", "actor_uuid", "pvp_uuids",
                "enabled", "write_enabled", "pvp_enabled", "backup_root", "retention", "backup_interval_sec",
                "port", "max_backup_files", "max_backup_bytes"));
        for (Object k : m.keySet()) {
            if (!allowed.contains(k)) {
                throw new ConfigException("unknown config key");
            }
        }
        List<String> pvp = new ArrayList<>();
        Object pv = m.get("pvp_uuids");
        if (pv != null) {
            if (!(pv instanceof List)) {
                throw new ConfigException("pvp_uuids must be a list");
            }
            for (Object o : (List<?>) pv) {
                if (!(o instanceof String)) {
                    throw new ConfigException("pvp_uuids must hold strings");
                }
                pvp.add((String) o);
            }
        }
        return of(str(m, "secret"), str(m, "world_id"), path(m, "save_path"), str(m, "actor_uuid"), pvp,
                bool(m, "enabled"), bool(m, "write_enabled"), bool(m, "pvp_enabled"),
                path(m, "backup_root"), intNum(m, "retention", 7, 1, 30),
                intNum(m, "backup_interval_sec", 1800, 60, 1800), intNum(m, "port", 6198, 1024, 65535),
                intNum(m, "max_backup_files", 20000, 1, 200000),
                num(m, "max_backup_bytes", 2147483648L));
    }

    private static UUID strictUuid(String raw, String field) throws ConfigException {
        if (raw == null || !UUID_RE.matcher(raw).matches()) {
            throw new ConfigException("invalid " + field);
        }
        return UUID.fromString(raw);
    }

    private static String str(Map<?, ?> m, String key) throws ConfigException {
        Object v = m.get(key);
        if (!(v instanceof String)) {
            throw new ConfigException("missing string " + key);
        }
        return (String) v;
    }

    private static Path path(Map<?, ?> m, String key) throws ConfigException {
        String raw = str(m, key);
        try {
            return Path.of(raw);
        } catch (InvalidPathException e) {
            // Never echo the value: it may contain a machine-local path.
            throw new ConfigException("invalid path " + key);
        }
    }

    /** Only the literal JSON true enables a flag; absent or anything else means false. */
    private static boolean bool(Map<?, ?> m, String key) {
        return Boolean.TRUE.equals(m.get(key));
    }

    private static long num(Map<?, ?> m, String key, long dflt) throws ConfigException {
        Object v = m.get(key);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof Long)) {
            throw new ConfigException("number must be an integer " + key);
        }
        return (Long) v;
    }

    /** Parses an integer with an explicit range before any narrowing cast. */
    private static int intNum(Map<?, ?> m, String key, int dflt, int min, int max) throws ConfigException {
        long v = num(m, key, dflt);
        if (v < min || v > max) {
            throw new ConfigException("number out of range " + key);
        }
        return (int) v;
    }
}
