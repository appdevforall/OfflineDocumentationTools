package org.appdevforall.docs.render;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * Draws the `module-graph.svg` that each javadoc module page embeds.
 *
 * The official ones are produced by graphviz, which isn't a dependency here, so these are laid out
 * directly: a module's transitive `requires` closure, arranged in rows by depth. They carry the
 * same information and the same visual language as the originals -- text-only nodes, grey arrows,
 * and javadoc's two module colours -- without being byte-identical to a graphviz rendering.
 *
 * The colour rule is javadoc's: modules that make up the Java SE platform (`java.se` and
 * everything it requires) are orange, JDK-specific modules blue.
 */
final class ModuleGraphWriter {

    private static final String SE_COLOR = "#e76f00";
    private static final String JDK_COLOR = "#437291";
    private static final String EDGE_COLOR = "#999999";
    private static final String FONT = "DejaVuSans";
    private static final int FONT_SIZE = 12;
    private static final int ROW_HEIGHT = 42;
    private static final int CHAR_WIDTH = 7;   // approximate advance for the 12pt font
    private static final int COLUMN_GAP = 24;
    private static final int MARGIN = 8;

    /**
     * module name -> the modules it `requires transitive`.
     *
     * Plain `requires` is deliberately absent: javadoc's module graph draws the *readability*
     * graph, which only propagates through `requires transitive`. `java.naming` plainly requires
     * `java.security.sasl` and its graph shows only itself and `java.base` -- verified against all
     * 60 JDK module graphs, which this rule reproduces exactly.
     */
    private final Map<String, Set<String>> requires;
    private final Set<String> platformModules;

    private static final String BASE_MODULE = "java.base";

    ModuleGraphWriter(Map<String, Set<String>> transitiveRequires) {
        this.requires = transitiveRequires;
        this.platformModules = platformModules(transitiveRequires);
    }



    /**
     * The Java SE platform: `java.se`, everything it requires, and `java.base`, which every module
     * requires implicitly. Empty when the run has no `java.se`, in which case every node is drawn
     * in the JDK colour.
     */
    private static Set<String> platformModules(Map<String, Set<String>> requires) {
        Set<String> platform = new HashSet<>();
        Set<String> se = requires.get("java.se");
        if (se == null) return platform;
        platform.add("java.se");
        platform.add(BASE_MODULE);
        platform.addAll(se);
        return platform;
    }

    /** Writes `<moduleDir>/module-graph.svg` for [moduleName]. */
    void write(Path moduleDir, String moduleName) throws IOException {
        List<List<String>> rows = layout(moduleName);
        if (rows.isEmpty()) return;
        Files.createDirectories(moduleDir);
        Files.writeString(moduleDir.resolve("module-graph.svg"), render(moduleName, rows),
                StandardCharsets.UTF_8);
    }

    /**
     * Groups the transitive closure into rows by longest distance from the root, so a module is
     * always drawn below everything that requires it.
     */
    private List<List<String>> layout(String root) {
        Map<String, Integer> depth = new HashMap<>();
        depth.put(root, 0);
        Deque<String> queue = new ArrayDeque<>();
        queue.add(root);
        // Longest-path depth needs re-visiting when a longer route to a node turns up, which is
        // why this is a worklist rather than a plain BFS.
        while (!queue.isEmpty()) {
            String current = queue.poll();
            int next = depth.get(current) + 1;
            for (String required : requires.getOrDefault(current, Set.of())) {
                if (!requires.containsKey(required)) continue;   // undocumented module
                if (next > depth.getOrDefault(required, -1)) {
                    depth.put(required, next);
                    queue.add(required);
                }
            }
        }
        // Every module reads java.base implicitly. JPMS grants that without it being written, so
        // it is absent from module-info.java and from the JSON, but javadoc still draws it (while
        // leaving it out of the Requires *table*).
        if (!root.equals(BASE_MODULE) && requires.containsKey(BASE_MODULE)) {
            int deepest = depth.values().stream().mapToInt(Integer::intValue).max().orElse(0);
            depth.put(BASE_MODULE, deepest + 1);
        }

        Map<Integer, List<String>> byDepth = new TreeMap<>();
        depth.forEach((name, level) -> byDepth.computeIfAbsent(level, k -> new ArrayList<>()).add(name));
        List<List<String>> rows = new ArrayList<>();
        byDepth.values().forEach(row -> {
            row.sort(Comparator.naturalOrder());
            rows.add(row);
        });
        return rows;
    }

    private String render(String root, List<List<String>> rows) {
        Map<String, int[]> centres = new HashMap<>();   // name -> {cx, cy}
        int width = 0;
        for (List<String> row : rows) {
            int rowWidth = row.stream().mapToInt(n -> n.length() * CHAR_WIDTH).sum()
                    + COLUMN_GAP * Math.max(0, row.size() - 1);
            width = Math.max(width, rowWidth);
        }
        width += MARGIN * 2;
        int height = rows.size() * ROW_HEIGHT + MARGIN * 2;

        for (int level = 0; level < rows.size(); level++) {
            List<String> row = rows.get(level);
            int rowWidth = row.stream().mapToInt(n -> n.length() * CHAR_WIDTH).sum()
                    + COLUMN_GAP * Math.max(0, row.size() - 1);
            int x = (width - rowWidth) / 2;
            int y = MARGIN + level * ROW_HEIGHT + FONT_SIZE;
            for (String name : row) {
                int nodeWidth = name.length() * CHAR_WIDTH;
                centres.put(name, new int[]{x + nodeWidth / 2, y});
                x += nodeWidth + COLUMN_GAP;
            }
        }

        StringBuilder svg = new StringBuilder();
        svg.append("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"no\"?>\n");
        svg.append("<svg xmlns=\"http://www.w3.org/2000/svg\" xmlns:xlink=\"http://www.w3.org/1999/xlink\" ")
           .append("width=\"").append(width).append("\" height=\"").append(height)
           .append("\" viewBox=\"0 0 ").append(width).append(' ').append(height).append("\">\n");
        svg.append("<title>").append(escape(root)).append("</title>\n");
        svg.append("<rect width=\"100%\" height=\"100%\" fill=\"white\"/>\n");
        svg.append("<defs><marker id=\"arrow\" markerWidth=\"8\" markerHeight=\"8\" refX=\"7\" refY=\"3\" ")
           .append("orient=\"auto\"><path d=\"M0,0 L8,3 L0,6 z\" fill=\"").append(EDGE_COLOR)
           .append("\"/></marker></defs>\n");

        // Edges first so the labels sit on top of them.
        Set<String> drawn = new LinkedHashSet<>();
        centres.keySet().stream().sorted().forEach(from -> {
            Set<String> targets = new LinkedHashSet<>();
            requires.getOrDefault(from, Set.of()).stream()
                    .filter(centres::containsKey)
                    .forEach(targets::add);
            // A module with no transitive requires of its own reads java.base directly, and that
            // is the edge javadoc draws for it.
            if (targets.isEmpty() && !from.equals(BASE_MODULE) && centres.containsKey(BASE_MODULE)) {
                targets.add(BASE_MODULE);
            }
            for (String to : targets) {
                if (!drawn.add(from + "->" + to)) continue;
                int[] a = centres.get(from);
                int[] b = centres.get(to);
                if (b[1] <= a[1]) continue;   // only draw downwards, never back up a cycle
                svg.append("<path fill=\"none\" stroke=\"").append(EDGE_COLOR)
                   .append("\" stroke-width=\"2\" marker-end=\"url(#arrow)\" d=\"M")
                   .append(a[0]).append(',').append(a[1] + 5).append(" L")
                   .append(b[0]).append(',').append(b[1] - FONT_SIZE - 2).append("\"/>\n");
            }
        });

        centres.keySet().stream().sorted().forEach(name -> {
            int[] c = centres.get(name);
            String colour = platformModules.contains(name) ? SE_COLOR : JDK_COLOR;
            svg.append("<text text-anchor=\"middle\" x=\"").append(c[0]).append("\" y=\"").append(c[1])
               .append("\" font-family=\"").append(FONT).append("\" font-size=\"").append(FONT_SIZE)
               .append(".00\" fill=\"").append(colour).append("\">").append(escape(name))
               .append("</text>\n");
        });

        svg.append("</svg>\n");
        return svg.toString();
    }

    private static String escape(String value) {
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }
}
