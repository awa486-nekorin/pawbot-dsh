package dev.pawbot.guard;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.ArrayList;
import java.util.Locale;
import java.util.Map;

/**
 * Minimal strict JSON reader/writer.
 *
 * <p>Deliberately dependency-free: the guard core must stay unit-testable with plain
 * {@code javac} + JUnit, without a Minecraft/Loom classpath. Only the shapes this contract
 * needs are supported, and anything unexpected is rejected rather than coerced.
 */
public final class Json {

    public static final class JsonException extends Exception {
        private static final long serialVersionUID = 1L;

        public JsonException(String message) {
            super(message);
        }
    }

    private static final int MAX_DEPTH = 32;
    private static final int MAX_LENGTH = 65536;

    private final String src;
    private int pos;

    private Json(String src) {
        this.src = src;
    }

    public static Object parse(String text) throws JsonException {
        if (text == null) {
            throw new JsonException("null input");
        }
        if (text.length() > MAX_LENGTH) {
            throw new JsonException("input too long");
        }
        Json p = new Json(text);
        p.skipWs();
        Object value = p.readValue(0);
        p.skipWs();
        if (p.pos != text.length()) {
            throw new JsonException("trailing data");
        }
        return value;
    }

    /** Parses an object whose members are all strings; any other member type is rejected. */
    public static Map<String, String> parseFlatStringObject(String text) throws JsonException {
        Object value = parse(text);
        if (!(value instanceof Map)) {
            throw new JsonException("not an object");
        }
        Map<String, String> out = new LinkedHashMap<>();
        for (Map.Entry<?, ?> entry : ((Map<?, ?>) value).entrySet()) {
            if (!(entry.getKey() instanceof String) || !(entry.getValue() instanceof String)) {
                throw new JsonException("non-string member");
            }
            out.put((String) entry.getKey(), (String) entry.getValue());
        }
        return out;
    }

    public static String quote(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 2);
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                default:
                    if (c < 0x20 || c > 0x7e) {
                        sb.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
        return sb.toString();
    }

    private Object readValue(int depth) throws JsonException {
        if (depth > MAX_DEPTH) {
            throw new JsonException("too deep");
        }
        char c = peek();
        switch (c) {
            case '{':
                return readObject(depth + 1);
            case '[':
                return readArray(depth + 1);
            case '"':
                return readString();
            case 't':
                expect("true");
                return Boolean.TRUE;
            case 'f':
                expect("false");
                return Boolean.FALSE;
            case 'n':
                expect("null");
                return null;
            default:
                return readNumber();
        }
    }

    private Map<String, Object> readObject(int depth) throws JsonException {
        Map<String, Object> map = new LinkedHashMap<>();
        pos++;
        skipWs();
        if (peek() == '}') {
            pos++;
            return map;
        }
        while (true) {
            skipWs();
            if (peek() != '"') {
                throw new JsonException("expected key");
            }
            String key = readString();
            if (map.containsKey(key)) {
                throw new JsonException("duplicate key");
            }
            skipWs();
            if (peek() != ':') {
                throw new JsonException("expected colon");
            }
            pos++;
            skipWs();
            map.put(key, readValue(depth));
            skipWs();
            char c = peek();
            if (c == ',') {
                pos++;
                continue;
            }
            if (c == '}') {
                pos++;
                return map;
            }
            throw new JsonException("expected comma or brace");
        }
    }

    private List<Object> readArray(int depth) throws JsonException {
        List<Object> list = new ArrayList<>();
        pos++;
        skipWs();
        if (peek() == ']') {
            pos++;
            return list;
        }
        while (true) {
            skipWs();
            list.add(readValue(depth));
            skipWs();
            char c = peek();
            if (c == ',') {
                pos++;
                continue;
            }
            if (c == ']') {
                pos++;
                return list;
            }
            throw new JsonException("expected comma or bracket");
        }
    }

    private String readString() throws JsonException {
        if (peek() != '"') {
            throw new JsonException("expected string");
        }
        pos++;
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= src.length()) {
                throw new JsonException("unterminated string");
            }
            char c = src.charAt(pos++);
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                if (pos >= src.length()) {
                    throw new JsonException("unterminated escape");
                }
                char e = src.charAt(pos++);
                switch (e) {
                    case '"':
                        sb.append('"');
                        break;
                    case '\\':
                        sb.append('\\');
                        break;
                    case '/':
                        sb.append('/');
                        break;
                    case 'b':
                        sb.append('\b');
                        break;
                    case 'f':
                        sb.append('\f');
                        break;
                    case 'n':
                        sb.append('\n');
                        break;
                    case 'r':
                        sb.append('\r');
                        break;
                    case 't':
                        sb.append('\t');
                        break;
                    case 'u':
                        if (pos + 4 > src.length()) {
                            throw new JsonException("bad unicode escape");
                        }
                        try {
                            sb.append((char) Integer.parseInt(src.substring(pos, pos + 4), 16));
                        } catch (NumberFormatException ex) {
                            throw new JsonException("bad unicode escape");
                        }
                        pos += 4;
                        break;
                    default:
                        throw new JsonException("bad escape");
                }
                continue;
            }
            if (c < 0x20) {
                throw new JsonException("control char in string");
            }
            sb.append(c);
        }
    }

    private Number readNumber() throws JsonException {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        boolean integral = true;
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (c >= '0' && c <= '9') {
                pos++;
            } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                integral = false;
                pos++;
            } else {
                break;
            }
        }
        if (pos == start) {
            throw new JsonException("expected number");
        }
        String raw = src.substring(start, pos);
        try {
            if (integral) {
                return Long.valueOf(raw);
            }
            return Double.valueOf(raw);
        } catch (NumberFormatException e) {
            throw new JsonException("bad number");
        }
    }

    private void expect(String literal) throws JsonException {
        if (!src.startsWith(literal, pos)) {
            throw new JsonException("expected " + literal);
        }
        pos += literal.length();
    }

    private char peek() throws JsonException {
        if (pos >= src.length()) {
            throw new JsonException("unexpected end");
        }
        return src.charAt(pos);
    }

    private void skipWs() {
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                pos++;
            } else {
                break;
            }
        }
    }
}
