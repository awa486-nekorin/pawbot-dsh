package dev.pawbot.guard;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.DirectoryStream;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * Bounded, link-safe world backups.
 *
 * <p>Every call is expected to run on the integrated-server thread after a save flush. The copy
 * never follows links, never writes outside the configured backup root, refuses oversized worlds
 * instead of truncating them, and prunes by retention count only among directories this class
 * itself created ({@code backup-<timestamp>-<seq>}).
 */
public final class BackupManager {

    private static final Pattern BACKUP_NAME = Pattern.compile("^backup-\\d{13}-\\d{3}$");
    private static final Pattern TEMP_NAME = Pattern.compile("^tmp-[0-9a-f]{32}$");
    private static final String SESSION_LOCK = "session.lock";
    private static final String MARKER = ".pawbot-test-world";
    private static final long DISK_HEADROOM = 1L << 20;
    private static final int SEQ_PER_MS = 1000;

    private final Path backupRoot;
    private final int retention;
    private final int maxFiles;
    private final long maxBytes;
    private int lastSeq = -1;
    private long lastSeqMs = -1L;

    public BackupManager(Path backupRoot, int retention, int maxFiles, long maxBytes) {
        if (backupRoot == null) {
            throw new IllegalArgumentException("backup root");
        }
        if (retention < 1 || maxFiles < 1 || maxBytes < 1) {
            throw new IllegalArgumentException("backup bounds");
        }
        this.backupRoot = PathSafety.normalizedAbsolute(backupRoot);
        this.retention = retention;
        this.maxFiles = maxFiles;
        this.maxBytes = maxBytes;
    }

    public static final class BackupException extends IOException {
        private static final long serialVersionUID = 1L;

        public BackupException(String message) {
            super(message);
        }
    }

    /**
     * Creates one backup of {@code worldRoot} and prunes older ones. Returns the backup id
     * (the directory name inside the backup root).
     */
    public String backup(Path worldRoot) throws IOException {
        Path world = PathSafety.realPathChecked(worldRoot);
        PathSafety.requireNoLinks(backupRoot);
        PathSafety.requireDisjointRoots(world, backupRoot);
        Files.createDirectories(backupRoot);
        Path realBackupRoot = PathSafety.realPathChecked(backupRoot);
        if (realBackupRoot.startsWith(world)) {
            throw new BackupException("backup root inside world");
        }

        // Enforce the budget on a scan before copying anything, so a failure costs no disk.
        long[] budget = scan(world);
        long files = budget[0];
        long bytes = budget[1];
        if (files > maxFiles) {
            throw new BackupException("too many files");
        }
        if (bytes > maxBytes) {
            throw new BackupException("backup too large");
        }
        long usable = realBackupRoot.toFile().getUsableSpace();
        if (usable > 0 && usable < bytes + DISK_HEADROOM) {
            throw new BackupException("insufficient disk space");
        }

        String id = nextId();
        Path temp = realBackupRoot.resolve("tmp-" + UUID.randomUUID().toString().replace("-", ""));
        Path dest = realBackupRoot.resolve(id);
        Files.createDirectory(temp);
        boolean ok = false;
        try {
            long[] copied = copyTree(world, temp);
            // A snapshot must describe one stable save. The server thread is blocked here, but a
            // second process or an out-of-band editor can still touch the directory; refuse rather
            // than store a mixed-generation tree.
            if (copied[0] != files || copied[1] != bytes) {
                throw new BackupException("world changed during backup");
            }
            Files.move(temp, dest, StandardCopyOption.ATOMIC_MOVE);
            ok = true;
        } finally {
            if (!ok) {
                deleteTreeQuietly(temp);
            }
        }
        prune(realBackupRoot);
        return id;
    }

    /** Deletes backups beyond the retention count, newest kept. Only own-shaped names are touched. */
    public int prune(Path root) throws IOException {
        PathSafety.requireNoLinks(root);
        List<Path> backups = new ArrayList<>();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(root)) {
            for (Path p : stream) {
                String name = p.getFileName().toString();
                if (BACKUP_NAME.matcher(name).matches() && Files.isDirectory(p, LinkOption.NOFOLLOW_LINKS)) {
                    backups.add(p);
                }
            }
        }
        backups.sort(Comparator.comparing((Path p) -> p.getFileName().toString()).reversed());
        int removed = 0;
        for (int i = retention; i < backups.size(); i++) {
            Path p = backups.get(i);
            try {
                PathSafety.requireNoLinks(p);
            } catch (IOException unsafe) {
                // A candidate that is itself a reparse point is never descended; leave it alone
                // rather than fail the whole snapshot cycle.
                continue;
            }
            deleteTreeQuietly(p);
            removed++;
        }
        return removed;
    }

    /** Removes stale staging directories left by a crash. Never follows links. */
    public void cleanupTemp() throws IOException {
        // The backup root itself must not be a reparse point: every deletion below trusts it.
        PathSafety.requireNoLinks(backupRoot);
        if (!Files.isDirectory(backupRoot, LinkOption.NOFOLLOW_LINKS)) {
            return;
        }
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(backupRoot)) {
            for (Path p : stream) {
                if (TEMP_NAME.matcher(p.getFileName().toString()).matches()
                        && Files.isDirectory(p, LinkOption.NOFOLLOW_LINKS)) {
                    deleteTreeQuietly(p);
                }
            }
        }
    }

    private long[] scan(Path world) throws IOException {
        long[] acc = new long[2];
        Files.walkFileTree(world, new SimpleFileVisitor<Path>() {
            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                if (attrs.isSymbolicLink() || attrs.isOther()) {
                    throw new BackupException("link in world");
                }
                if (isExcluded(world, file)) {
                    return FileVisitResult.CONTINUE;
                }
                if (!attrs.isRegularFile()) {
                    throw new BackupException("special file in world");
                }
                acc[0]++;
                acc[1] += attrs.size();
                if (acc[0] > maxFiles || acc[1] > maxBytes) {
                    throw new BackupException("backup budget exceeded");
                }
                return FileVisitResult.CONTINUE;
            }

            @Override
            public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) throws IOException {
                if (attrs.isSymbolicLink() || attrs.isOther()) {
                    throw new BackupException("link in world");
                }
                if (isExcluded(world, dir)) {
                    return FileVisitResult.SKIP_SUBTREE;
                }
                return FileVisitResult.CONTINUE;
            }
        });
        return acc;
    }

    private long[] copyTree(Path world, Path temp) throws IOException {
        long[] copied = new long[2];
        Files.walkFileTree(world, new SimpleFileVisitor<Path>() {
            @Override
            public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) throws IOException {
                if (attrs.isSymbolicLink() || attrs.isOther()) {
                    throw new BackupException("link in world");
                }
                if (isExcluded(world, dir)) {
                    return FileVisitResult.SKIP_SUBTREE;
                }
                Path target = temp.resolve(world.relativize(dir).toString());
                Files.createDirectories(target);
                return FileVisitResult.CONTINUE;
            }

            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                if (attrs.isSymbolicLink() || attrs.isOther()) {
                    throw new BackupException("link in world");
                }
                if (isExcluded(world, file)) {
                    return FileVisitResult.CONTINUE;
                }
                if (!attrs.isRegularFile()) {
                    throw new BackupException("special file in world");
                }
                Path target = temp.resolve(world.relativize(file).toString());
                try (InputStream in = Files.newInputStream(file);
                        OutputStream out = Files.newOutputStream(target)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                    }
                }
                BasicFileAttributes after = Files.readAttributes(file, BasicFileAttributes.class,
                        LinkOption.NOFOLLOW_LINKS);
                if (after.isSymbolicLink() || after.isOther() || !after.isRegularFile()
                        || after.size() != attrs.size()
                        || !after.lastModifiedTime().equals(attrs.lastModifiedTime())
                        || Files.size(target) != attrs.size()) {
                    throw new BackupException("world changed during backup");
                }
                copied[0]++;
                copied[1] += attrs.size();
                return FileVisitResult.CONTINUE;
            }
        });
        if (!Files.exists(temp.resolve(MARKER))) {
            throw new BackupException("marker missing in copy");
        }
        return copied;
    }

    /** session.lock is live-process state and is never valid inside a snapshot. */
    private static boolean isExcluded(Path world, Path file) {
        return file.getFileName().toString().equals(SESSION_LOCK) && world.equals(file.getParent());
    }

    private String nextId() {
        long now;
        synchronized (this) {
            now = System.currentTimeMillis();
            if (now == lastSeqMs) {
                lastSeq = (lastSeq + 1) % SEQ_PER_MS;
            } else {
                lastSeqMs = now;
                lastSeq = 0;
            }
        }
        return String.format(Locale.ROOT, "backup-%013d-%03d", now, lastSeq);
    }

    /**
     * Best-effort recursive delete that never crosses a link, junction or other reparse point.
     * A junction is reported by {@code walkFileTree} as a directory, so checking only the root is
     * not enough: every directory entry must be tested with {@code NOFOLLOW_LINKS} first, and a
     * reparse point is deleted as a link only (never descended).
     */
    private static void deleteTreeQuietly(Path root) {
        try {
            BasicFileAttributes rootAttrs = Files.readAttributes(root, BasicFileAttributes.class,
                    LinkOption.NOFOLLOW_LINKS);
            if (rootAttrs.isSymbolicLink() || rootAttrs.isOther()) {
                Files.deleteIfExists(root);
                return;
            }
            Files.walkFileTree(root, new SimpleFileVisitor<Path>() {
                @Override
                public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) throws IOException {
                    if (!dir.equals(root) && (attrs.isSymbolicLink() || attrs.isOther())) {
                        Files.deleteIfExists(dir);
                        return FileVisitResult.SKIP_SUBTREE;
                    }
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                    Files.deleteIfExists(file);
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult postVisitDirectory(Path dir, IOException exc) throws IOException {
                    Files.deleteIfExists(dir);
                    return FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException ignored) {
            // Best effort: the caller already fails closed on the primary error.
        }
    }
}
