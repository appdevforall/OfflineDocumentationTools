package org.appdevforall.docs.android;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.pebbletemplates.pebble.PebbleEngine;
import io.pebbletemplates.pebble.loader.ClasspathLoader;
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

    /** {@code page} field value -> template name. A page kind with no entry here is skipped. */
    private static final Map<String, String> TEMPLATES = Map.of(
            "android-class", "class",
            "android-package", "package",
            "android-index", "index"
    );

    private final ObjectMapper json = new ObjectMapper();
    private final PebbleEngine engine;

    private AndroidDocRenderer() {
        this.engine = new PebbleEngine.Builder()
                .loader(new ClasspathLoader() {{
                    setPrefix("templates");
                    setSuffix(".peb");
                }})
                // The documentation fields in the JSON are HTML already -- they were read out of
                // HTML -- and the templates put those through the `doc` filter, which marks them
                // safe. Autoescaping stays on so that everything else (names, signatures, titles)
                // is escaped by default rather than by memory.
                .autoEscaping(true)
                .strictVariables(false)
                .extension(new AndroidDocExtension())
                .build();
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
        String templateName = kind == null ? null : TEMPLATES.get(kind.toString());
        if (templateName == null) {
            System.err.println("No template for page kind '" + kind + "' (" + relative + ")");
            return false;
        }

        Map<String, Object> context = new LinkedHashMap<>(data);
        // How far this page sits below the output root, so a template can reach the shared
        // stylesheet and the root index from any depth.
        context.put("pathToRoot", pathToRoot(relative));
        // Where this page's own package summary sits, which is the one link every page wants and
        // the only one that cannot be written into the JSON: it is derived from the path.
        context.put("packageUrl", packageUrl(relative));

        Path out = target.resolve(withHtmlExtension(relative));
        Files.createDirectories(out.getParent());
        PebbleTemplate template = engine.getTemplate(templateName);
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

    private void copyStaticAssets(Path target) throws IOException {
        for (String asset : new String[]{"stylesheet.css"}) {
            try (InputStream in = getClass().getResourceAsStream("/static/" + asset)) {
                if (in == null) continue;
                Files.copy(in, target.resolve(asset), StandardCopyOption.REPLACE_EXISTING);
            }
        }
    }
}
