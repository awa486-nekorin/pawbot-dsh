package dev.pawbot.guard;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.regex.Pattern;

/**
 * Offline restore. Standalone CLI, never called by the mod, so it cannot run inside a live game.
 *
 * <pre>
 * java -cp pawbot-guard.jar dev.pawbot.guard.RestoreTool \
 *     &lt;backup_root&gt; &lt;backup_id&gt; &lt;new_target_dir&gt; &lt;world_id&gt; [protected_original_world]
 * </pre>
 *
 * <p>The optional fifth argument is the owner's original world directory (or any other directory
 * that must never be restored over). It is compared lexically and, when present, canonically; the
 * tool refuses the original path and anything inside it. Without that argument the tool still
 * refuses every existing target, so a live original world cannot be overwritten, but a deleted
 * original path is indistinguishable from any other new path.
 *
 * <p>Safety rules, all fail-closed:
 * <ul>
 *   <li>the target must NOT exist (no overwrite, ever, and therefore the original world cannot be
 *       replaced); its parent must exist and must not be a link/reparse point;</li>
 *   <li>no ancestor of the target may be a live Minecraft world (a directory holding a
 *       {@code session.lock}), so a restore cannot write into an open save;</li>
 *   <li>the target may not sit inside the backup root or vice versa, and may not be the source;</li>
 *   <li>the backup id must be one of this guard's own {@code backup-<13 digits>-<3 digits>} names,
 *       the backup must carry the dedicated marker with the exact world_id, and it may not contain a
 *       live {@code session.lock};</li>
 *   <li>the backup itself is only read: source files are never moved, deleted or rewritten.</li>
 * </ul>
 *
 * <p>The result is a new directory the operator then registers manually; nothing is adopted
 * automatically and the original world is never touched.
 */
public final class RestoreTool {

    private static final Pattern BACKUP_NAME = Pattern.compile("^backup-\\d{13}-\\d{3}$");
    private static final Pattern WORLD_ID_RE = Pattern.compile("^[A-Za-z0-9._-]{1,64}$");
    private static final String MARKER = ".pawbot-test-world";
    private static final String SESSION_LOCK = "session.lock";
    private static final long MAX_MARKER_BYTES = 4096L;

    private RestoreTool() {
    }

    public static void main(String[] args) {
        if (args.length != 4 && args.length != 5) {
            System.err.println("usage: RestoreTool <backup_root> <backup_id> <new_target_dir> <world_id> "
                    + "[protected_original_world]");
            System.exit(2);
            return;
        }
        try {
            Path protectedWorld = args.length == 5 ? Path.of(args[4]) : null;
            Path restored = restore(Path.of(args[0]), args[1], Path.of(args[2]), args[3], protectedWorld);
            System.out.println("restored " + restored);
            System.out.println("register the new directory manually before use");
        } catch (IOException e) {
            System.err.println("restore refused: " + e.getMessage());
            System.exit(1);
        } catch (RuntimeException e) {
            // Malformed local path/argument: never print the value, just refuse.
            System.err.println("restore refused: invalid path");
            System.exit(1);
        }
    }

    /** Restores one backup into a brand-new directory and returns that directory. */
    public static Path restore(Path backupRoot, String backupId, Path target, String worldId) throws IOException {
        return restore(backupRoot, backupId, target, worldId, null, null);
    }

    /**
     * Restores one backup into a brand-new directory and returns that directory.
     *
     * @param protectedWorld optional owner original world; the target may not equal or sit inside it
     */
    public static Path restore(Path backupRoot, String backupId, Path target, String worldId, Path protectedWorld)
            throws IOException {
        return restore(backupRoot, backupId, target, worldId, protectedWorld, null);
    }

    /**
     * Package-private seam for the regression suite: {@code beforePublish} runs after the staging
     * tree is complete and immediately before publication, letting a test simulate a concurrent
     * process creating the target at the worst possible moment.
     */
    static Path restore(Path backupRoot, String backupId, Path target, String worldId, Path protectedWorld,
            Runnable beforePublish) throws IOException {
        if (backupId == null || !BACKUP_NAME.matcher(backupId).matches()) {
            throw new IOException("invalid backup id");
        }
        if (worldId == null || !WORLD_ID_RE.matcher(worldId).matches()) {
            throw new IOException("invalid world id");
        }
        if (target == null) {
            throw new IOException("invalid target");
        }
        Path root = PathSafety.realPathChecked(backupRoot);
        if (!Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("backup root missing");
        }
        Path source = root.resolve(backupId);
        if (!Files.isDirectory(source, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("backup not found");
        }
        PathSafety.requireNoLinks(source);
        Path realSource = source.toRealPath();
        if (!realSource.getParent().equals(root)) {
            throw new IOException("backup outside root");
        }

        Path abs = PathSafety.normalizedAbsolute(target);
        String name = abs.getFileName() == null ? "" : abs.getFileName().toString();
        if (name.isEmpty() || name.equals(".") || name.equals("..")) {
            throw new IOException("invalid target name");
        }
        if (Files.exists(abs, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("target exists; overwrite is never allowed");
        }
        Path parent = abs.getParent();
        if (parent == null || !Files.isDirectory(parent, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("target parent missing");
        }
        Path realParent = PathSafety.realPathChecked(parent);
        Path finalTarget = realParent.resolve(name).normalize();
        if (!finalTarget.getParent().equals(realParent)) {
            throw new IOException("invalid target");
        }
        if (finalTarget.startsWith(root) || root.startsWith(finalTarget)
                || finalTarget.startsWith(realSource)) {
            throw new IOException("target overlaps backup root");
        }
        rejectProtectedWorld(finalTarget, protectedWorld);
        rejectRunningWorldAncestor(realParent);

        Path marker = source.resolve(MARKER);
        if (!Files.isRegularFile(marker, LinkOption.NOFOLLOW_LINKS)
                || Files.size(marker) > MAX_MARKER_BYTES
                || !Files.readString(marker, StandardCharsets.UTF_8).trim().equals(worldId)) {
            throw new IOException("backup marker mismatch");
        }
        if (Files.exists(source.resolve(SESSION_LOCK), LinkOption.NOFOLLOW_LINKS)
                || containsSessionLock(realSource)) {
            throw new IOException("backup contains session.lock (live world)");
        }

        Path staging = realParent.resolve(name + ".restoring");
        rejectProtectedWorld(staging, protectedWorld);
        if (Files.exists(staging, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("staging exists");
        }
        Files.createDirectory(staging);
        boolean ok = false;
        try {
            Files.walkFileTree(realSource, new SimpleFileVisitor<Path>() {
                @Override
                public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes a) throws IOException {
                    if (a.isSymbolicLink() || a.isOther()) {
                        throw new IOException("link in backup");
                    }
                    Files.createDirectories(staging.resolve(realSource.relativize(dir).toString()));
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes a) throws IOException {
                    if (a.isSymbolicLink() || a.isOther() || !a.isRegularFile()) {
                        throw new IOException("link or special file in backup");
                    }
                    Path destination = staging.resolve(realSource.relativize(file).toString());
                    Files.copy(file, destination, StandardCopyOption.COPY_ATTRIBUTES);
                    if (Files.size(destination) != a.size()) {
                        throw new IOException("short copy");
                    }
                    return FileVisitResult.CONTINUE;
                }
            });
            // Re-check, then publish without ever replacing an existing target.
            if (Files.exists(finalTarget, LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("target exists; overwrite is never allowed");
            }
            // A world can be opened at an ancestor while the tree is being copied; never publish
            // into a live save that appeared after the initial check.
            rejectRunningWorldAncestor(realParent);
            if (beforePublish != null) {
                // The test seam sits after the re-check and before publication, i.e. exactly in
                // the window where another process can still create the target.
                beforePublish.run();
            }
            publishNoReplace(staging, finalTarget);
            ok = true;
            deleteTreeQuietly(staging);
            return finalTarget;
        } finally {
            if (!ok) {
                deleteTreeQuietly(staging);
            }
        }
    }

    /** True when any entry in the backup is named session.lock, at any depth. */
    private static boolean containsSessionLock(Path root) throws IOException {
        final boolean[] found = { false };
        Files.walkFileTree(root, new SimpleFileVisitor<Path>() {
            @Override
            public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes a) {
                if (dir.getFileName() != null && dir.getFileName().toString().equals(SESSION_LOCK)) {
                    found[0] = true;
                    return FileVisitResult.TERMINATE;
                }
                return FileVisitResult.CONTINUE;
            }

            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes a) {
                if (file.getFileName().toString().equals(SESSION_LOCK)) {
                    found[0] = true;
                    return FileVisitResult.TERMINATE;
                }
                return FileVisitResult.CONTINUE;
            }
        });
        return found[0];
    }

    /**
     * Refuses the configured original world even when it no longer exists on disk. Both the
     * normalized path and a canonical form are compared; the canonical form resolves the deepest
     * existing ancestor (following links/junctions) and appends the missing tail, so a deleted
     * original can no longer be reached through an alias ancestor that also contains the target.
     */
    private static void rejectProtectedWorld(Path finalTarget, Path protectedWorld) throws IOException {
        if (protectedWorld == null) {
            return;
        }
        Path absProtected = PathSafety.normalizedAbsolute(protectedWorld);
        Path canonicalTarget = canonicalAllowMissing(finalTarget);
        Path canonicalProtected = canonicalAllowMissing(absProtected);
        rejectOverlap(finalTarget, absProtected);
        rejectOverlap(finalTarget, canonicalProtected);
        rejectOverlap(canonicalTarget, absProtected);
        rejectOverlap(canonicalTarget, canonicalProtected);
    }

    /** Rejects equality or containment in either direction (target inside protected or vice versa). */
    private static void rejectOverlap(Path target, Path protectedPath) throws IOException {
        if (target.equals(protectedPath) || target.startsWith(protectedPath)
                || protectedPath.startsWith(target)) {
            throw new IOException("target is the protected original world");
        }
    }

    /**
     * Canonicalizes a path that may not exist yet by resolving its deepest existing ancestor and
     * appending the missing components. Uses {@code toRealPath()} on the existing prefix (which
     * follows links) so comparison is about the real location, not its spelling.
     */
    private static Path canonicalAllowMissing(Path path) throws IOException {
        Path abs = PathSafety.normalizedAbsolute(path);
        Path existing = abs;
        ArrayList<Path> missingTail = new ArrayList<>();
        while (existing != null && !Files.exists(existing, LinkOption.NOFOLLOW_LINKS)) {
            Path name = existing.getFileName();
            if (name == null) {
                break;
            }
            missingTail.add(0, name);
            existing = existing.getParent();
        }
        if (existing == null) {
            return abs;
        }
        Path rebuilt = existing.toRealPath();
        for (Path name : missingTail) {
            rebuilt = rebuilt.resolve(name);
        }
        return rebuilt.normalize();
    }

    /**
     * A target below an open world is a live-save write even when the leaf name is new. Walk every
     * ancestor to the filesystem root and refuse if any of them is a Minecraft world root; a fixed
     * depth would accept a sufficiently nested path.
     */
    private static void rejectRunningWorldAncestor(Path start) throws IOException {
        for (Path current = start; current != null; current = current.getParent()) {
            if (Files.exists(current.resolve(SESSION_LOCK), LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("target is inside a running world");
            }
        }
    }

    /**
     * Publishes the completed staging tree without ever replacing an existing target.
     *
     * <p>{@code Files.move(..., ATOMIC_MOVE)} can replace an existing target on some providers and
     * a plain move is check-then-rename on POSIX, so neither is a portable no-overwrite primitive.
     * {@code Files.createDirectory} is: it fails atomically if anything already exists at the
     * target. Once the directory is ours, the staged entries are moved into it; on any failure the
     * directory we created is removed again, and the caller's cleanup removes the rest of staging.
     */
    private static void publishNoReplace(Path staging, Path finalTarget) throws IOException {
        try {
            Files.createDirectory(finalTarget);
        } catch (FileAlreadyExistsException e) {
            throw new IOException("target exists; overwrite is never allowed", e);
        }
        boolean published = false;
        try {
            Files.walkFileTree(staging, new SimpleFileVisitor<Path>() {
                @Override
                public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) throws IOException {
                    if (attrs.isSymbolicLink() || attrs.isOther()) {
                        throw new IOException("link in staging");
                    }
                    Path relative = staging.relativize(dir);
                    if (!relative.toString().isEmpty()) {
                        Files.createDirectory(finalTarget.resolve(relative.toString()));
                    }
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                    if (attrs.isSymbolicLink() || attrs.isOther() || !attrs.isRegularFile()) {
                        throw new IOException("link or special file in staging");
                    }
                    Files.move(file, finalTarget.resolve(staging.relativize(file).toString()));
                    return FileVisitResult.CONTINUE;
                }
            });
            published = true;
        } finally {
            if (!published) {
                deleteTreeQuietly(finalTarget);
            }
        }
    }

    /**
     * Best-effort cleanup that never crosses a link, junction or other reparse point. Restore only
     * creates normal staging trees, but an attacker or crash can replace a staging entry, so each
     * directory is inspected with {@code NOFOLLOW_LINKS} and a reparse point is deleted as a link.
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
            // best effort
        }
    }
}
