package org.appdevforall.docs.render;

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
import java.nio.file.attribute.BasicFileAttributes;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.Map;

/**
 * Turns a javadoc-mode JSON tree into browsable HTML.
 *
 * Every JSON page carries a {@code page} field naming its kind, which selects the Pebble template;
 * the parsed JSON becomes the template context directly, so a template reads the same field names
 * that appear in the JSON. Output mirrors the input tree exactly, with {@code .json} swapped for
 * {@code .html}, which is what makes the relative links in the JSON resolve once rewritten.
 *
 * <pre>
 *   java -jar pebble-renderer.jar &lt;json-dir&gt; &lt;html-dir&gt;
 * </pre>
 */
public final class JavadocHtmlRenderer {

    /** {@code page} field value -> template. A page kind with no entry here is skipped. */
    private static final Map<String, String> TEMPLATES = Map.of(
            "class", "class",
            "package", "package-summary",
            "module", "module-summary",
            "overview", "overview",
            "all-classes", "all-classes",
            "all-packages", "all-packages",
            "deprecated-list", "deprecated-list",
            "constant-values", "constant-values",
            "index", "index-page"
    );

    private final ObjectMapper json = new ObjectMapper();
    private final PebbleEngine engine;

    private JavadocHtmlRenderer() {
        this.engine = new PebbleEngine.Builder()
                .loader(new ClasspathLoader() {{
                    setPrefix("templates");
                    setSuffix(".peb");
                }})
                // The doc text in the JSON is already HTML (a javadoc comment's body is), and the
                // templates mark those values with |raw. Autoescaping stays on so everything else
                // -- names, signatures, modifiers -- is escaped by default rather than by memory.
                .autoEscaping(true)
                .strictVariables(false)
                .extension(new JavadocExtension())
                .build();
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: JavadocHtmlRenderer <json-dir> <html-dir>");
            System.exit(2);
        }
        Path source = Path.of(args[0]).toAbsolutePath().normalize();
        Path target = Path.of(args[1]).toAbsolutePath().normalize();
        if (!Files.isDirectory(source)) {
            System.err.println("Not a directory: " + source);
            System.exit(2);
        }
        int written = new JavadocHtmlRenderer().renderTree(source, target);
        System.out.println("Wrote " + written + " HTML pages to " + target);
    }

    private int renderTree(Path source, Path target) throws IOException {
        Files.createDirectories(target);
        copyStaticAssets(target);
        // Built before the pages are written: a module page embeds its graph, so the graph has to
        // exist, and drawing one needs every module's requires, not just its own.
        ModuleGraphWriter graphs = new ModuleGraphWriter(readModuleRequires(source));

        int[] counters = {0, 0};
        Files.walkFileTree(source, new SimpleFileVisitor<>() {
            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                Path relative = source.relativize(file);
                if (file.getFileName().toString().endsWith(".json")) {
                    if (renderPage(file, relative, target, graphs)) counters[0]++; else counters[1]++;
                } else {
                    // element-list and anything else non-JSON is carried across untouched.
                    Path copy = target.resolve(relative);
                    Files.createDirectories(copy.getParent());
                    Files.copy(file, copy, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                }
                return FileVisitResult.CONTINUE;
            }
        });
        if (counters[1] > 0) {
            System.out.println("Skipped " + counters[1] + " JSON file(s) with no matching template.");
        }
        return counters[0];
    }

    /** Every module's direct `requires`, read from the module pages before rendering starts. */
    @SuppressWarnings("unchecked")
    private Map<String, Set<String>> readModuleRequires(Path source) throws IOException {
        Map<String, Set<String>> result = new LinkedHashMap<>();
        try (var paths = Files.walk(source)) {
            for (Path file : (Iterable<Path>) paths.filter(p -> p.getFileName().toString()
                    .equals("module-summary.json"))::iterator) {
                Map<String, Object> data = readPage(file);
                if (data == null) continue;
                Object name = data.get("name");
                if (name == null) continue;
                // Only `requires transitive` -- see ModuleGraphWriter.requires for why.
                Set<String> required = new LinkedHashSet<>();
                Object requires = data.get("requires");
                if (requires instanceof List<?> list) {
                    for (Object entry : list) {
                        if (entry instanceof Map<?, ?> map && map.get("module") != null
                                && Boolean.TRUE.equals(map.get("isTransitive"))) {
                            required.add(map.get("module").toString());
                        }
                    }
                }
                result.put(name.toString(), required);
            }
        }
        return result;
    }

    private boolean renderPage(Path file, Path relative, Path target, ModuleGraphWriter graphs)
            throws IOException {
        Map<String, Object> data = readPage(file);
        if (data == null) return false;

        Object kind = data.get("page");
        String templateName = kind == null ? null : TEMPLATES.get(kind.toString());
        if (templateName == null) {
            System.err.println("No template for page kind '" + kind + "' (" + relative + ")");
            return false;
        }

        Map<String, Object> context = new LinkedHashMap<>(data);
        // Depth of this page below the output root, so templates can reach shared assets and the
        // top-level index pages regardless of how deep they sit.
        context.put("pathToRoot", pathToRoot(relative));
        context.put("pageKind", kind.toString());

        Path out = target.resolve(withHtmlExtension(relative));
        Files.createDirectories(out.getParent());

        if ("module".equals(kind.toString()) && data.get("name") != null) {
            graphs.write(out.getParent(), data.get("name").toString());
            context.put("hasModuleGraph", true);
        }
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
        int depth = relative.getNameCount() - 1;
        return "../".repeat(Math.max(0, depth));
    }

    private void copyStaticAssets(Path target) throws IOException {
        for (String asset : new String[]{"stylesheet.css"}) {
            try (InputStream in = getClass().getResourceAsStream("/static/" + asset)) {
                if (in == null) continue;
                Files.copy(in, target.resolve(asset), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            }
        }
    }
}
