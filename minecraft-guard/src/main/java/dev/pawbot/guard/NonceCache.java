package dev.pawbot.guard;

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Bounded replay cache for request nonces.
 *
 * <p>Entries expire after a fixed TTL (60 s per the contract) and the map is capped so a flood
 * of distinct nonces cannot grow memory without bound. Both bounds are enforced on every claim.
 */
public final class NonceCache {

    private final int capacity;
    private final long ttlSeconds;
    private final LinkedHashMap<String, Long> seen = new LinkedHashMap<>();

    public NonceCache(int capacity, long ttlSeconds) {
        if (capacity < 1 || ttlSeconds < 1) {
            throw new IllegalArgumentException("bad bounds");
        }
        this.capacity = capacity;
        this.ttlSeconds = ttlSeconds;
    }

    /**
     * Claims a nonce. Returns false when it was already seen (replay) — including a claim that
     * was denied for another reason, so retrying a rejected request does not get a fresh attempt.
     */
    public synchronized boolean claim(String nonce, long nowSeconds) {
        purge(nowSeconds);
        if (seen.containsKey(nonce)) {
            return false;
        }
        seen.put(nonce, nowSeconds + ttlSeconds);
        while (seen.size() > capacity) {
            Iterator<Map.Entry<String, Long>> it = seen.entrySet().iterator();
            if (!it.hasNext()) {
                break;
            }
            it.next();
            it.remove();
        }
        return true;
    }

    public synchronized int size() {
        return seen.size();
    }

    private void purge(long nowSeconds) {
        Iterator<Map.Entry<String, Long>> it = seen.entrySet().iterator();
        while (it.hasNext()) {
            if (it.next().getValue() <= nowSeconds) {
                it.remove();
            }
        }
    }
}
