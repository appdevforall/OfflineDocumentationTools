package org.appdevforall.docs.android;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Renders real page documents through the real templates.
 *
 * These exist because the interesting failures here are silent ones. Pebble is built with
 * {@code strictVariables(false)}, so a field the extractor stops writing renders as nothing rather
 * than as an error, and -- as macros.peb's own header warns -- a macro called with a {@code macros.}
 * prefix resolves to nothing and renders empty. Either mistake would blank out part of every one
 * of 12,000 pages while the build still reported success. So the assertions are deliberately about
 * content appearing, not about markup matching byte for byte.
 */
class AndroidDocRendererTest {

    /** A class page carrying one of everything a template branches on. */
    private static final String CLASS_PAGE = """
            {
              "page": "android-class",
              "library": "android",
              "packageName": "android.demo",
              "name": "Widget",
              "qualifiedName": "android.demo.Widget",
              "flavor": "doclava",
              "versionScheme": "api-level",
              "kind": "class",
              "title": "Widget",
              "addedIn": "4",
              "sourceUrl": "https://cs.android.com/widget",
              "signature": "public class Widget extends Base",
              "signatureHtml": "public class Widget extends <a href=\\"Base.json\\">Base</a>",
              "implements": [
                {"label": "Widget.Listener", "url": "Widget.Listener.json"},
                {"label": "Cloneable", "url": "https://developer.android.com/x",
                 "linkClass": "external-link"},
                {"label": "Mystery", "url": "URL", "linkClass": "broken-link"}
              ],
              "inheritance": [
                {"label": "java.lang.Object", "url": "https://developer.android.com/x"},
                {"label": "android.demo.Widget"}
              ],
              "knownDirectSubclasses": [{"label": "FancyWidget", "url": "FancyWidget.json"}],
              "description": "<p>A widget that does something.</p>",
              "brief": "A widget that does something.",
              "summary": [
                {"id": "public-methods", "title": "Public methods", "rows": [
                  {"type": "void", "name": "setMode",
                   "member": "<a href=\\"Widget.json#setMode(int)\\">setMode</a>(int mode)",
                   "anchor": "setMode(int)", "description": "<p>Sets the mode.</p>",
                   "deprecatedIn": "11"}
                ]}
              ],
              "inherited": [
                {"id": "inherited-methods", "title": "Inherited methods", "groups": [
                  {"from": {"label": "android.demo.Base", "url": "Base.json"},
                   "rows": [{"type": "void", "name": "reset",
                             "member": "<a href=\\"Base.json#reset()\\">reset</a>()"}]}
                ]}
              ],
              "details": [
                {"id": "public-methods", "title": "Public methods", "members": [
                  {"name": "setMode", "anchor": "setMode(int)", "addedIn": "4",
                   "deprecatedIn": "11", "deprecated": true,
                   "deprecationLabel": "This method was deprecated in API level 11.",
                   "deprecationNote": "Use <a href=\\"Widget.json#setSpeed(int)\\">setSpeed</a>.",
                   "signature": "public Base setMode (int mode)",
                   "signatureHtml": "public <a href=\\"Base.json\\">Base</a> setMode (int mode)",
                   "description": "<p>Sets the mode.</p>",
                   "parameters": [{"name": "mode", "type": "int", "declaration": "int mode",
                                   "description": "One of the mode constants."}],
                   "returns": [{"type": "<a href=\\"Base.json\\">Base</a>",
                                "description": "This widget."}],
                   "throws": [{"type": "IllegalStateException", "description": "if too late."}],
                   "seeAlso": [{"label": "setSpeed(int)", "url": "Widget.json#setSpeed(int)"}]}
                ]},
                {"id": "constants", "title": "Constants", "members": [
                  {"name": "MODE_FAST", "anchor": "MODE_FAST", "addedIn": "4",
                   "signature": "public static final int MODE_FAST",
                   "signatureHtml": "public static final int MODE_FAST",
                   "description": "<p>Go quickly.</p>",
                   "constantValue": "1 (0x00000001)"}
                ]}
              ]
            }
            """;

    private static final String PACKAGE_PAGE = """
            {
              "page": "android-package",
              "generated": true,
              "library": "androidx",
              "versionScheme": "library-version",
              "packageName": "androidx.demo",
              "name": "androidx.demo",
              "title": "androidx.demo",
              "groups": [
                {"id": "classes", "title": "Classes", "rows": [
                  {"member": "<a href=\\"Gadget.json\\">Gadget</a>", "name": "Gadget",
                   "description": "A gadget.", "deprecatedIn": "1.3.0"}
                ]}
              ]
            }
            """;

    private String render(Path dir, String relative, String document) throws IOException {
        Path source = dir.resolve("json").resolve(relative);
        Files.createDirectories(source.getParent());
        Files.writeString(source, document, StandardCharsets.UTF_8);
        Path target = dir.resolve("html");
        assertEquals(0, AndroidDocRenderer.render(dir.resolve("json"), target),
                "no page should be skipped for want of a template");
        // Only the extension: replacing every ".json" in the path is the mistake the extractor
        // was corrected for, and repeating it here invites it back.
        Path out = target.resolve(
                relative.substring(0, relative.length() - ".json".length()) + ".html");
        assertTrue(Files.exists(out), "expected " + out);
        return Files.readString(out);
    }

    @Test
    void rendersEveryPartOfAClassPage(@TempDir Path dir) throws Exception {
        String html = render(dir, "android/demo/Widget.json", CLASS_PAGE);

        assertTrue(html.contains("<title>Widget (android.demo)</title>"), html);
        assertTrue(html.contains("public class Widget extends"), "the signature");
        assertTrue(html.contains("Added in API level 4"), "the version, worded for the scheme");
        // The interface list is a field the extractor once failed to emit at all, leaving this
        // branch dead; asserting it keeps that from happening again unnoticed.
        assertTrue(html.contains("Implements:"), "the implements block");
        assertTrue(html.contains("Widget.Listener.html"), "the interface link");
        assertTrue(html.contains("Known direct subclasses:"), "the subclass list");
        assertTrue(html.contains("A widget that does something."), "the class prose");
        assertTrue(html.contains("Public methods"), "the summary section");
        assertTrue(html.contains("Sets the mode."), "the summary description");
        assertTrue(html.contains("Deprecated in API level 11"), "the deprecated row marker");
        assertTrue(html.contains("From <a href=\"Base.html\">android.demo.Base</a>"),
                "the inherited group's heading, with its link rewritten");
        assertTrue(html.contains("Base.html#reset()"), "the inherited member's link");
        assertTrue(html.contains("<details"), "inherited members collapse without JavaScript");
        assertTrue(html.contains("id=\"setMode(int)\""), "the member anchor");
        assertTrue(html.contains("This method was deprecated in API level 11."), "the notice");
        assertTrue(html.contains("Parameters"), "the parameter table");
        assertTrue(html.contains("One of the mode constants."), "the parameter prose");
        assertTrue(html.contains("Returns"), "the returns table");
        assertTrue(html.contains("Throws"), "the throws table");
        assertTrue(html.contains("See also"), "the see-also list");
        assertTrue(html.contains("Constant value:"), "the constant value");
    }

    @Test
    void rewritesEveryLinkFromJsonToHtml(@TempDir Path dir) throws Exception {
        String html = render(dir, "android/demo/Widget.json", CLASS_PAGE);
        // The extractor writes paths into its own tree; the renderer's whole link contract is
        // swapping the extension as it mirrors that tree.
        assertFalse(html.contains(".json"), "no .json link may survive into the HTML");
        assertTrue(html.contains("href=\"Base.html\""), "a relative link");
        assertTrue(html.contains("href=\"Widget.html#setMode(int)\""), "a link with a fragment");
        assertTrue(html.contains("https://developer.android.com/x"), "an external link, untouched");
    }

    @Test
    void wordsVersionsForTheLibraryOnAPackagePage(@TempDir Path dir) throws Exception {
        String html = render(dir, "androidx/demo/package-summary.json", PACKAGE_PAGE);
        assertTrue(html.contains("androidx.demo"), "the package name");
        assertTrue(html.contains("Gadget"), "the row");
        // Jetpack is versioned by artifact release, so "API level" would name something that
        // does not exist.
        assertTrue(html.contains("Deprecated in 1.3.0"), html);
        assertFalse(html.contains("API level 1.3.0"), "no API level wording for a Jetpack page");
    }

    @Test
    void marksLinksThatLeaveTheAppAndLinksThatNameNothing(@TempDir Path dir) throws Exception {
        String html = render(dir, "android/demo/Widget.json", CLASS_PAGE);
        // The scraped pages carried these two classes and the site coloured them red; a reader
        // can see which links need the network and which lead nowhere before tapping one.
        assertTrue(html.contains(
                "<a href=\"https://developer.android.com/x\" class=\"external-link\">"
                        + "Cloneable</a>"), html);
        assertTrue(html.contains("<a href=\"URL\" class=\"broken-link\">Mystery</a>"), html);
        // A plain link into the documentation gets no class at all.
        assertTrue(html.contains("<a href=\"Widget.Listener.html\">Widget.Listener</a>"), html);
        // And the source link always leaves the app.
        assertTrue(html.contains("class=\"external-link\">View source</a>"), html);
    }

    @Test
    void stylesBothKindsOfRedLink() throws Exception {
        String css = new String(AndroidDocRenderer.class
                .getResourceAsStream("/static/stylesheet.css").readAllBytes(),
                StandardCharsets.UTF_8);
        assertTrue(css.contains("a.external-link"), "off-site links need a rule");
        assertTrue(css.contains("a.broken-link"), "links that name nothing need a rule");
        // Both colours are defined for either theme, so neither disappears against a dark ground.
        assertEquals(2, css.split("--link-external:", -1).length - 1, "light and dark");
        assertEquals(2, css.split("--link-broken:", -1).length - 1, "light and dark");
    }

    @Test
    void swapsOnlyTheExtensionOfLinksIntoTheTree() {
        assertEquals("Base.html", HtmlLinks.swap("Base.json"));
        assertEquals("../view/View.html#foo(int)", HtmlLinks.swap("../view/View.json#foo(int)"));
        // An absolute URL leaves the tree, and a bare fragment never left this page.
        assertEquals("https://developer.android.com/reference/java/lang/Object",
                HtmlLinks.swap("https://developer.android.com/reference/java/lang/Object"));
        assertEquals("#summary", HtmlLinks.swap("#summary"));
        // A page whose own name contains .json must keep it: only the extension moves.
        assertEquals("a.json.b.html", HtmlLinks.swap("a.json.b.json"));
    }

    @Test
    @SuppressWarnings("unchecked")
    void rewritesLinksAnywhereInTheDocument() {
        Map<String, Object> page = Map.of(
                "inheritance", List.of(Map.of("label", "Base", "url", "Base.json")),
                "description", "<p>See <a href=\"../view/View.json#x\">View</a>.</p>",
                "name", "Widget");
        Map<String, Object> out = (Map<String, Object>) HtmlLinks.rewrite(page);
        assertEquals("Base.html",
                ((List<Map<String, Object>>) out.get("inheritance")).get(0).get("url"));
        assertEquals("<p>See <a href=\"../view/View.html#x\">View</a>.</p>", out.get("description"));
        assertEquals("Widget", out.get("name"), "a string with no link is untouched");
    }

    @Test
    void assemblesEachTemplateWithTheSharedMacros() throws Exception {
        for (String name : new String[]{"class", "package", "index"}) {
            String source = AndroidDocRenderer.assembleTemplate(name);
            // Self-contained is the contract the database imposes: one row, one template.
            assertFalse(source.contains("{% extends"), name + " must not extend anything");
            assertFalse(source.contains("{% import"), name + " must not import anything");
            assertTrue(source.contains("{% macro summaryRows"), name + " needs the macros appended");
            // The only filter the database's engine is known to offer.
            assertFalse(source.contains("| doc") || source.contains("| anchor")
                            || source.contains("| href"),
                    name + " must use no filter this project defines");
        }
    }

    @Test
    void reportsAPageKindItHasNoTemplateFor(@TempDir Path dir) throws Exception {
        Path source = dir.resolve("json");
        Files.createDirectories(source);
        Files.writeString(source.resolve("odd.json"), "{\"page\": \"something-else\"}");
        assertEquals(1, AndroidDocRenderer.render(source, dir.resolve("html")),
                "an unknown page kind is counted as skipped, not rendered as an empty page");
    }
}
