/*
 * Render.java - renders documentation.db templates with the Pebble engine the app uses.
 *
 * Built the way CodeOnTheGo's DocumentationContentSource builds its engine (Pebble 4.1.1,
 * default settings - HTML autoescaping on, newline trimming on, strict variables off - and
 * maxRenderedSize of 4 Mi chars), and given its context the same way: Gson with
 * ToNumberPolicy.LONG_OR_DOUBLE parses the row's JSON into a Map<String, Object>, and the
 * rendered String is returned as UTF-8 bytes. Gson 2.12.1 is the version CodeOnTheGo's :common
 * module renders with.
 *
 * Templates are defined over stdin and resolve by exact name, as the app's
 * DatabaseTemplateLoader resolves Templates rows, so a caller can check a template before it is
 * written anywhere.
 *
 * Run from source, no build step:
 *
 *     java -cp pebble-4.1.1.jar:unbescape-1.1.6.RELEASE.jar:slf4j-api.jar:gson-2.12.1.jar Render.java
 *
 * Protocol, on stdin and stdout: each request is one ASCII header line, "<cmd> <name> <length>",
 * followed by <length> bytes.
 *
 *     T <name> <length>   define (or replace) template <name>; the bytes are its UTF-8 text
 *     R <name> <length>   render template <name>; the bytes are the JSON context
 *     Q - 0               quit
 *
 * Each request gets one header line, "OK <length> <nanos>" or "ERR <length> 0", followed by
 * <length> bytes: the rendered page, or the error message. <nanos> is the time from parsing the
 * JSON to holding the rendered bytes.
 */

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.ToNumberPolicy;
import com.google.gson.reflect.TypeToken;
import io.pebbletemplates.pebble.PebbleEngine;
import io.pebbletemplates.pebble.error.LoaderException;
import io.pebbletemplates.pebble.error.PebbleException;
import io.pebbletemplates.pebble.loader.Loader;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.Reader;
import java.io.StringReader;
import java.io.StringWriter;
import java.lang.reflect.Type;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class Render {
    // DocumentationContentSource.MAX_RENDERED_CHARS
    static final int MAX_RENDERED_CHARS = 4 * 1024 * 1024;

    static final Map<String, String> defined = new ConcurrentHashMap<>();

    /** Resolves a name to a template defined over stdin, exactly, like DatabaseTemplateLoader. */
    static final class Templates implements Loader<String> {
        @Override
        public Reader getReader(String name) {
            String text = defined.get(name);
            if (text == null) throw new LoaderException(null, "Template '" + name + "' not found");
            return new StringReader(text);
        }

        @Override
        public boolean resourceExists(String name) {
            return defined.containsKey(name);
        }

        @Override
        public String createCacheKey(String name) {
            return name;
        }

        @Override
        public String resolveRelativePath(String relativePath, String anchorPath) {
            return relativePath;
        }

        @Override
        public void setCharset(String charset) {
        }

        @Override
        public void setPrefix(String prefix) {
        }

        @Override
        public void setSuffix(String suffix) {
        }
    }

    static PebbleEngine newEngine() {
        return new PebbleEngine.Builder().loader(new Templates()).maxRenderedSize(MAX_RENDERED_CHARS).build();
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 0) {
            System.err.println("usage: Render.java (templates and contexts come over stdin)");
            System.exit(2);
        }
        Gson gson = new GsonBuilder().setObjectToNumberStrategy(ToNumberPolicy.LONG_OR_DOUBLE).create();
        Type contextType = new TypeToken<Map<String, Object>>() {}.getType();
        PebbleEngine engine = newEngine();

        InputStream in = new BufferedInputStream(System.in, 1 << 16);
        OutputStream out = new BufferedOutputStream(System.out, 1 << 16);
        while (true) {
            String header = readLine(in);
            if (header == null) break;
            String[] parts = header.split(" ");
            if (parts.length != 3) {
                reply(out, false, ("bad request header: " + header).getBytes(StandardCharsets.UTF_8), 0);
                break;
            }
            byte[] payload = in.readNBytes(Integer.parseInt(parts[2]));
            switch (parts[0]) {
                case "Q":
                    out.flush();
                    return;
                case "T":
                    defined.put(parts[1], new String(payload, StandardCharsets.UTF_8));
                    // A replaced template must not be served from the engine's cache.
                    engine = newEngine();
                    reply(out, true, new byte[0], 0);
                    break;
                case "R":
                    long start = System.nanoTime();
                    try {
                        String json = new String(payload, StandardCharsets.UTF_8);
                        if (json.isBlank() || json.trim().equals("null")) {
                            throw new IllegalArgumentException("Template '" + parts[1] + "' has empty or null JSON context");
                        }
                        Map<String, Object> context = gson.fromJson(json, contextType);
                        StringWriter writer = new StringWriter();
                        engine.getTemplate(parts[1]).evaluate(writer, context);
                        byte[] page = writer.toString().getBytes(StandardCharsets.UTF_8);
                        reply(out, true, page, System.nanoTime() - start);
                    } catch (PebbleException e) {
                        String message = e.getFileName() == null && e.getLineNumber() == null ? e.getPebbleMessage() : e.getMessage();
                        reply(out, false, String.valueOf(message).getBytes(StandardCharsets.UTF_8), 0);
                    } catch (Exception | StackOverflowError e) {
                        reply(out, false, (e.getClass().getSimpleName() + ": " + e.getMessage()).getBytes(StandardCharsets.UTF_8), 0);
                    }
                    break;
                default:
                    reply(out, false, ("unknown command: " + parts[0]).getBytes(StandardCharsets.UTF_8), 0);
            }
        }
        out.flush();
    }

    static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        int c;
        while ((c = in.read()) != -1 && c != '\n') line.write(c);
        if (c == -1 && line.size() == 0) return null;
        return line.toString(StandardCharsets.US_ASCII);
    }

    static void reply(OutputStream out, boolean ok, byte[] body, long nanos) throws IOException {
        out.write(((ok ? "OK " : "ERR ") + body.length + " " + nanos + "\n").getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }
}
