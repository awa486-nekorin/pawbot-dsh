package dev.pawbot.guard;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** HMAC-SHA256 helpers. The secret never leaves this class as a String copy beyond construction. */
public final class Hmac {

    private static final char[] HEX = "0123456789abcdef".toCharArray();

    private Hmac() {
    }

    /** Lowercase hex HMAC-SHA256(secret, payload). */
    public static String hmacSha256(byte[] secret, byte[] payload) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            return toHex(mac.doFinal(payload));
        } catch (Exception e) {
            // No fallback: an unsigned response must never be emitted.
            throw new IllegalStateException("hmac unavailable", e);
        }
    }

    /**
     * Constant-time equality over UTF-8 bytes. Length is compared first (unavoidable), then
     * every byte accumulates differences so timing does not leak the matching prefix.
     */
    public static boolean constantTimeEquals(byte[] a, byte[] b) {
        if (a == null || b == null) {
            return false;
        }
        byte[] x = sha256(a);
        byte[] y = sha256(b);
        int diff = x.length ^ y.length;
        int n = Math.min(x.length, y.length);
        for (int i = 0; i < n; i++) {
            diff |= x[i] ^ y[i];
        }
        return diff == 0;
    }

    public static byte[] digest(byte[] payload) {
        return sha256(payload);
    }

    public static byte[] utf8(String s) {
        return s.getBytes(StandardCharsets.UTF_8);
    }

    private static byte[] sha256(byte[] payload) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(payload);
        } catch (Exception e) {
            throw new IllegalStateException("sha256 unavailable", e);
        }
    }

    private static String toHex(byte[] bytes) {
        char[] out = new char[bytes.length * 2];
        for (int i = 0; i < bytes.length; i++) {
            int v = bytes[i] & 0xff;
            out[i * 2] = HEX[v >>> 4];
            out[i * 2 + 1] = HEX[v & 0x0f];
        }
        return new String(out);
    }
}
