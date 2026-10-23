package org.appdevforall.docs.android;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.pebbletemplates.pebble.PebbleEngine;
import io.pebbletemplates.pebble.template.PebbleTemplate;

import java.io.IOException;
import java.io.InputStream;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Turns the JSON tree written by {@code android_docs_to_json.py} into browsable HTML.
 *
 * Every page carries a {@code page} field naming its kind, which selects the Pebble template; the
 * parsed JSON becomes the template context directly, so a template reads the same field names that
 * appear in the JSON. Output mirrors the input tree exactly with {@code .json} swapped for
 * {@code .html}, which is what makes the relative links already in the JSON resolve -- the
 * extractor wrote them as paths into this very tree.
 *
 * <pre>
 *   android-doc-renderer &lt;json-dir&gt; &lt;html-dir&gt;
 * </pre>
 */
public final class AndroidDocRenderer {

    /** {@code page} field value -> template resource name. A kind with no entry here is skipped. */
    private static final Map<String, String> TEMPLATES = Map.of(
            "android-class", "class",
            "android-package", "package",
            "android-index", "index"
    );

    /** Where the shared stylesheet is written, relative to the output root. */
    static final String STYLESHEET = "assets/android-reference.css";

    private final ObjectMapper json = new ObjectMapper();
    private final PebbleEngine engine;
    private final Map<String, PebbleTemplate> compiled = new LinkedHashMap<>();

    private AndroidDocRenderer() throws IOException {
        this.engine = new PebbleEngine.Builder()
                // The documentation fields in the JSON are HTML already -- they were read out of
                // HTML -- and the templates put those through `raw`. Autoescaping stays on so that
                // everything else (names, signatures, titles) is escaped by default rather than by
                // memory. Both settings match what the templates rely on in the documentation
                // database, which is the other place they run.
                .autoEscaping(true)
                .strictVariables(false)
                .build();
        // Compiled from source rather than loaded by name: a page template and the shared macros
        // are concatenated into one self-contained source, because that is the only shape the
        // database can store and macros are visible only inside their own file. Compiled once,
        // not once per page.
        for (Map.Entry<String, String> entry : TEMPLATES.entrySet()) {
            compiled.put(entry.getKey(),
                    engine.getLiteralTemplate(assembleTemplate(entry.getValue())));
        }
    }

    /** One page template with the shared macros appended, as the database stores them. */
    static String assembleTemplate(String name) throws IOException {
        return readResource("/templates/" + name + ".peb") + "\n"
                + readResource("/templates/_macros.peb");
    }

    private static String readResource(String path) throws IOException {
        try (InputStream in = AndroidDocRenderer.class.getResourceAsStream(path)) {
            if (in == null) throw new IOException("missing template resource: " + path);
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: AndroidDocRenderer <json-dir> <html-dir>");
            System.exit(2);
        }
        Path source = Path.of(args[0]).toAbsolutePath().normalize();
        Path target = Path.of(args[1]).toAbsolutePath().normalize();
        if (!Files.isDirectory(source)) {
            System.err.println("Not a directory: " + source);
            System.exit(2);
        }
        System.exit(render(source, target));
    }

    /**
     * Renders {@code source} into {@code target}, returning the number of JSON files skipped for
     * want of a template. Separate from {@code main} so that a test can render a tree without the
     * {@code System.exit} that a command-line tool owes its caller.
     */
    static int render(Path source, Path target) throws IOException {
        return new AndroidDocRenderer().renderTree(source, target);
    }

    private int renderTree(Path source, Path target) throws IOException {
        Files.createDirectories(target);
        copyStaticAssets(target);

        int[] counters = {0, 0};
        Files.walkFileTree(source, new SimpleFileVisitor<>() {
            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs)
                    throws IOException {
                Path relative = source.relativize(file);
                if (!file.getFileName().toString().endsWith(".json")) {
                    Path copy = target.resolve(relative);
                    Files.createDirectories(copy.getParent());
                    Files.copy(file, copy, StandardCopyOption.REPLACE_EXISTING);
                    return FileVisitResult.CONTINUE;
                }
                if (renderPage(file, relative, target)) counters[0]++; else counters[1]++;
                if (counters[0] % 1000 == 0 && counters[0] > 0) {
                    System.out.println("    " + counters[0] + " pages");
                }
                return FileVisitResult.CONTINUE;
            }
        });
        System.out.println("Wrote " + counters[0] + " HTML pages to " + target);
        if (counters[1] > 0) {
            System.out.println("Skipped " + counters[1] + " JSON file(s) with no matching template.");
        }
        return counters[1] == 0 ? 0 : 1;
    }

    private boolean renderPage(Path file, Path relative, Path target) throws IOException {
        Map<String, Object> data = readPage(file);
        if (data == null) return false;

        Object kind = data.get("page");
        if (kind == null || !compiled.containsKey(kind.toString())) {
            System.err.println("No template for page kind '" + kind + "' (" + relative + ")");
            return false;
        }

        @SuppressWarnings("unchecked")
        Map<String, Object> context =
                new LinkedHashMap<>((Map<String, Object>) HtmlLinks.rewrite(data));
        // The three links that depend on where the pages are served from rather than on the
        // documentation, which is why they are not in the JSON. Relative to this page, so the
        // output works over file:// as well as from a server root.
        String root = pathToRoot(relative);
        context.put("stylesheetUrl", root + STYLESHEET);
        context.put("indexUrl", root + "index.html");
        context.put("packageUrl", packageUrl(relative));

        Path out = target.resolve(withHtmlExtension(relative));
        Files.createDirectories(out.getParent());
        PebbleTemplate template = compiled.get(kind.toString());
        try (Writer writer = Files.newBufferedWriter(out, StandardCharsets.UTF_8)) {
            template.evaluate(writer, context);
        } catch (IOException e) {
            throw e;
        } catch (RuntimeException e) {
            throw new IOException("Failed rendering " + relative + ": " + e.getMessage(), e);
        }
        return true;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> readPage(Path file) {
        try {
            return json.readValue(file.toFile(), Map.class);
        } catch (IOException e) {
            System.err.println("Could not read " + file + ": " + e.getMessage());
            return null;
        }
    }

    private static Path withHtmlExtension(Path relative) {
        String name = relative.getFileName().toString();
        String renamed = name.substring(0, name.length() - ".json".length()) + ".html";
        Path parent = relative.getParent();
        return parent == null ? Path.of(renamed) : parent.resolve(renamed);
    }

    /** {@code ""} at the root, {@code "../"} one level down, and so on. */
    private static String pathToRoot(Path relative) {
        return "../".repeat(Math.max(0, relative.getNameCount() - 1));
    }

    /** The package summary beside this page, or null for a page that sits at the root. */
    private static String packageUrl(Path relative) {
        if (relative.getNameCount() < 2) return null;
        if (relative.getFileName().toString().equals("package-summary.json")) return null;
        return "package-summary.html";
    }

    /**
     * Writes the one stylesheet every page links to.
     *
     * Under `assets/`, and shared, which is the point: the scraped pages each inline the site's
     * whole stylesheet, tens of kilobytes repeated 12,000 times.
     */
    private void copyStaticAssets(Path target) throws IOException {
        Path out = target.resolve(STYLESHEET);
        Files.createDirectories(out.getParent());
        try (InputStream in = getClass().getResourceAsStream("/static/stylesheet.css")) {
            if (in == null) throw new IOException("missing /static/stylesheet.css");
            Files.copy(in, out, StandardCopyOption.REPLACE_EXISTING);
        }
    }
}
