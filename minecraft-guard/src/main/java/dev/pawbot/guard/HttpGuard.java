package dev.pawbot.guard;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Loopback-only HTTP transport for {@link PermitService}.
 *
 * <p>Deliberately tiny: one route, one method, fixed request/response caps, and no request or
 * response body ever written to a log. Non-loopback peers are refused even though the socket is
 * already bound to 127.0.0.1.
 *
 * <p>Two local-denial-of-service bounds (S4):
 * <ul>
 *   <li>the Bearer header is verified <em>before</em> the body is read, so an unauthenticated peer
 *       cannot hold a worker with a slow body; an unauthenticated request is answered and closed
 *       immediately;</li>
 *   <li>a read deadline closes an authenticated exchange whose body does not arrive in time. The
 *       emergency file marker and a revoke are independent of the worker pool, so the stop path
 *       stays reachable.</li>
 * </ul>
 */
public final class HttpGuard {

    private static final String PATH = "/v1/permit";
    private static final int MAX_HEADER_VALUE = 512;
    /** Generous for a <=4096-byte body; only a stalled peer ever hits it. */
    private static final int REQUEST_READ_DEADLINE_MS = 2_000;
    /**
     * One worker can legitimately sit in a snapshot wait; a second answers revoke/observe, a third
     * is headroom for a slow authenticated body. Unauthenticated bodies never reach a worker.
     */
    private static final int WORKERS = 3;

    private final PermitService service;
    private final int port;
    private HttpServer server;
    private ExecutorService executor;
    private ScheduledExecutorService readDeadline;

    public HttpGuard(PermitService service, int port) {
        this.service = service;
        this.port = port;
    }

    public synchronized void start() throws IOException {
        if (server != null) {
            return;
        }
        // Explicit IPv4 loopback: the signed `host` field is always 127.0.0.1, so binding to an
        // IPv6-only loopback would make the attested address unreachable.
        server = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 8);
        executor = Executors.newFixedThreadPool(WORKERS, r -> {
            Thread t = new Thread(r, "pawbot-guard-http");
            t.setDaemon(true);
            return t;
        });
        readDeadline = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "pawbot-guard-http-deadline");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(executor);
        server.createContext(PATH, this::handleExchange);
        server.start();
    }

    public synchronized void stop() {
        if (server != null) {
            server.stop(0);
            server = null;
        }
        if (executor != null) {
            executor.shutdownNow();
            try {
                executor.awaitTermination(2, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            executor = null;
        }
        if (readDeadline != null) {
            readDeadline.shutdownNow();
            readDeadline = null;
        }
    }

    private void handleExchange(HttpExchange exchange) {
        try {
            InetAddress remote = exchange.getRemoteAddress().getAddress();
            if (remote == null || !remote.isLoopbackAddress()) {
                respond(exchange, new PermitService.Response(403, "forbidden".getBytes(StandardCharsets.UTF_8), null));
                return;
            }
            if (!PATH.equals(exchange.getRequestURI().getPath())) {
                respond(exchange, new PermitService.Response(404, "not found".getBytes(StandardCharsets.UTF_8), null));
                return;
            }
            if (!"POST".equals(exchange.getRequestMethod())) {
                exchange.getResponseHeaders().set("Allow", "POST");
                respond(exchange, new PermitService.Response(405, "method not allowed".getBytes(StandardCharsets.UTF_8), null));
                return;
            }
            String authorization = header(exchange, "Authorization");
            // Authenticate the header before reading the body: a peer without the secret gets a
            // bare 401 and can never park a worker on a slow body.
            if (!service.hasValidBearer(authorization)) {
                respond(exchange, new PermitService.Response(401, "unauthorized".getBytes(StandardCharsets.UTF_8), null));
                return;
            }
            String contentType = header(exchange, "Content-Type");
            byte[] body;
            ScheduledFuture<?> deadline = readDeadline == null ? null
                    : readDeadline.schedule(() -> closeQuietly(exchange), REQUEST_READ_DEADLINE_MS,
                            TimeUnit.MILLISECONDS);
            try {
                body = readBounded(exchange.getRequestBody(), PermitService.MAX_REQUEST_BYTES);
            } catch (IOException tooLarge) {
                respond(exchange, new PermitService.Response(413, "invalid size".getBytes(StandardCharsets.UTF_8), null));
                return;
            } finally {
                if (deadline != null) {
                    deadline.cancel(false);
                }
            }
            PermitService.Response response = service.handle(authorization, contentType, body,
                    System.currentTimeMillis() / 1000L);
            respond(exchange, response);
        } catch (Throwable t) {
            try {
                respond(exchange, new PermitService.Response(503, "unavailable".getBytes(StandardCharsets.UTF_8), null));
            } catch (IOException ignored) {
                // Connection already gone; nothing to do.
            }
        } finally {
            exchange.close();
        }
    }

    private static void closeQuietly(HttpExchange exchange) {
        try {
            exchange.close();
        } catch (RuntimeException ignored) {
            // The handler may already have finished; closing twice is harmless.
        }
    }

    private static String header(HttpExchange exchange, String name) {
        Headers headers = exchange.getRequestHeaders();
        String value = headers.getFirst(name);
        if (value == null || value.length() > MAX_HEADER_VALUE) {
            return null;
        }
        return value;
    }

    /** Reads at most {@code cap} bytes; a longer body is an error rather than truncation. */
    private static byte[] readBounded(InputStream in, int cap) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream(Math.min(cap, 1024));
        byte[] buf = new byte[1024];
        int total = 0;
        int n;
        while ((n = in.read(buf)) > 0) {
            total += n;
            if (total > cap) {
                throw new IOException("too large");
            }
            out.write(buf, 0, n);
        }
        return out.toByteArray();
    }

    private static void respond(HttpExchange exchange, PermitService.Response response) throws IOException {
        Headers headers = exchange.getResponseHeaders();
        headers.set("Content-Type", "application/json; charset=utf-8");
        headers.set("Cache-Control", "no-store");
        headers.set("X-Content-Type-Options", "nosniff");
        if (response.signature != null) {
            headers.set("X-PawBot-Signature", response.signature);
        }
        exchange.sendResponseHeaders(response.status, response.body.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(response.body);
            out.flush();
        }
    }
}
