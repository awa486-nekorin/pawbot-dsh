package dev.pawbot.guard;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Filesystem guardrails.
 *
 * <p>Rules enforced here, all of them fail-closed:
 * <ul>
 *   <li>no component of a trusted path may be a symlink, junction or other reparse point;</li>
 *   <li>path comparisons use real (canonical) paths, never string prefixes;</li>
 *   <li>a world root and a backup root must not contain one another.</li>
 * </ul>
 * Junctions surface as symbolic links through {@code NOFOLLOW_LINKS} attributes on Windows; the
 * {@code isOther()} check catches remaining reparse-point shapes.
 */
public final class PathSafety {

    private PathSafety() {
    }

    public static final class UnsafePathException extends IOException {
        private static final long serialVersionUID = 1L;

        public UnsafePathException(String message) {
            super(message);
        }
    }

    public static Path normalizedAbsolute(Path path) {
        return path.toAbsolutePath().normalize();
    }

    /** Rejects any existing component along the path that is a link or reparse point. */
    public static void requireNoLinks(Path path) throws IOException {
        Path abs = normalizedAbsolute(path);
        List<Path> parts = new ArrayList<>();
        for (Path p = abs; p != null; p = p.getParent()) {
            parts.add(p);
        }
        Collections.reverse(parts);
        for (Path p : parts) {
            if (!Files.exists(p, LinkOption.NOFOLLOW_LINKS)) {
                return;
            }
            BasicFileAttributes attrs;
            try {
                attrs = Files.readAttributes(p, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
            } catch (IOException e) {
                throw new UnsafePathException("unreadable path component");
            }
            if (attrs.isSymbolicLink()) {
                throw new UnsafePathException("link in path");
            }
            if (attrs.isOther()) {
                throw new UnsafePathException("reparse point in path");
            }
        }
    }

    /** Real path of an existing location, after rejecting links along its whole chain. */
    public static Path realPathChecked(Path path) throws IOException {
        requireNoLinks(path);
        Path real = path.toRealPath();
        requireNoLinks(real);
        return real;
    }

    /** True when the two real paths denote exactly the same location. */
    public static boolean sameLocation(Path a, Path b) {
        try {
            return a.toRealPath().equals(b.toRealPath());
        } catch (IOException e) {
            return false;
        }
    }

    /** Rejects two roots where either contains the other (including equality). */
    public static void requireDisjointRoots(Path worldRoot, Path backupRoot) throws IOException {
        Path w = normalizedAbsolute(worldRoot);
        Path b = normalizedAbsolute(backupRoot);
        if (w.startsWith(b) || b.startsWith(w)) {
            throw new UnsafePathException("overlapping roots");
        }
    }
}
