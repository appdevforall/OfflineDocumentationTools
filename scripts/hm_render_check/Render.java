/*
 * Render.java - renders documentation.db templates with the Pebble engine the app uses.
 *
 * Built the way CodeOnTheGo's DocumentationContentSource builds its engine (Pebble 4.1.1,
 * default settings - HTML autoescaping on, newline trimming on, strict variables off - and
 * maxRenderedSize of 4 Mi chars), and given its context the same way: Gson with
 * ToNumberPolicy.LONG_OR_DOUBLE parses the row's JSON into a Map<String, Object>, and the
 * rendered String is returned as UTF-8 bytes.
 *
 * Templates resolve by exact name, first from templates defined over stdin (so a caller can
 * check a template before it is written anywhere), then, with --db, from the Templates table,
 * which is how the app's DatabaseTemplateLoader resolves them.
 *
 * Run from source, no build step:
 *
 *     java -cp pebble-4.1.1.jar:unbescape-1.1.6.RELEASE.jar:slf4j-api.jar:gson-2.10.1.jar[:sqlite-jdbc.jar] \
 *         Render.java [--db documentation.db]
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
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class Render {
    // DocumentationContentSource.MAX_RENDERED_CHARS
    static final int MAX_RENDERED_CHARS = 4 * 1024 * 1024;

    static final Map<String, String> defined = new ConcurrentHashMap<>();
    static Connection db;

    /** DatabaseTemplateLoader's lookup, with templates defined over stdin consulted first. */
    static final class Templates implements Loader<String> {
        @Override
        public Reader getReader(String name) {
            String text = defined.get(name);
            if (text != null) return new StringReader(text);
            if (db == null) throw new LoaderException(null, "Template '" + name + "' not found");
            try (PreparedStatement statement = db.prepareStatement("SELECT content FROM Templates WHERE name = ?")) {
                statement.setString(1, name);
                try (ResultSet rows = statement.executeQuery()) {
                    if (!rows.next()) throw new LoaderException(null, "Template '" + name + "' not found in the database");
                    byte[] body = rows.getBytes(1);
                    if (rows.next()) throw new LoaderException(null, "Template '" + name + "' is shared by more than one database row");
                    if (body == null) throw new LoaderException(null, "Template '" + name + "' has no body");
                    return new StringReader(new String(body, StandardCharsets.UTF_8));
                }
            } catch (LoaderException e) {
                throw e;
            } catch (Exception e) {
                throw new LoaderException(e, "Cannot read template '" + name + "' from the database");
            }
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
        for (int i = 0; i < args.length; i++) {
            if (args[i].equals("--db") && i + 1 < args.length) {
                db = DriverManager.getConnection("jdbc:sqlite:file:" + args[++i] + "?mode=ro");
            } else {
                System.err.println("usage: Render.java [--db documentation.db]");
                System.exit(2);
            }
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
