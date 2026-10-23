package org.appdevforall.docs.android;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.pebbletemplates.pebble.PebbleEngine;
import io.pebbletemplates.pebble.template.PebbleTemplate;

import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

/**
 * Renders one stored template against one stored page, and prints the result.
 *
 * This exists to check the documentation database rather than the file tree: given a Templates row
 * and a Content row pulled straight out of it, does that template render that JSON? Nothing else
 * answers that question, because the templates and the pages are only brought together by the
 * server, in an engine this project does not run.
 *
 * The engine is configured the way the database's own pages need it -- autoescaping on, lenient
 * about absent variables, no filter this project defines -- so a template that renders here is one
 * that asks nothing of the server that its existing Kotlin pages do not already ask.
 *
 *   java -cp ... TemplateCheck &lt;template-file&gt; &lt;page-json-file&gt;
 */
public final class TemplateCheck {

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: TemplateCheck <template-file> <page-json-file>");
            System.exit(2);
        }
        String source = Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
        @SuppressWarnings("unchecked")
        Map<String, Object> page = new ObjectMapper().readValue(Path.of(args[1]).toFile(), Map.class);

        PebbleEngine engine = new PebbleEngine.Builder()
                .autoEscaping(true)
                .strictVariables(false)
                .build();
        PebbleTemplate template = engine.getLiteralTemplate(source);
        try (PrintWriter out = new PrintWriter(System.out, true, StandardCharsets.UTF_8)) {
            template.evaluate(out, page);
        }
    }
}
