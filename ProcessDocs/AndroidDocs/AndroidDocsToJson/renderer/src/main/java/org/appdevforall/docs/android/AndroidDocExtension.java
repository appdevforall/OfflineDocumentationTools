package org.appdevforall.docs.android;

import io.pebbletemplates.pebble.extension.AbstractExtension;
import io.pebbletemplates.pebble.extension.Filter;
import io.pebbletemplates.pebble.extension.escaper.SafeString;
import io.pebbletemplates.pebble.template.EvaluationContext;
import io.pebbletemplates.pebble.template.PebbleTemplate;

import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The filters the templates need: two that fix up links, and one that turns a member anchor into
 * something usable as an HTML id.
 *
 * The JSON links to `.json` files, because the extractor writes paths into its own tree. The HTML
 * output mirrors that tree file-for-file with `.html` names, so a link is correct as soon as its
 * extension is swapped -- the relative path itself already points at the right page. `href` does
 * that for a bare URL and `doc` for a block of documentation HTML with URLs inside it.
 */
public final class AndroidDocExtension extends AbstractExtension {

    @Override
    public Map<String, Filter> getFilters() {
        return Map.of(
                "href", new HrefFilter(),
                "doc", new DocFilter(),
                "anchor", new AnchorFilter()
        );
    }

    /** `.json` -> `.html`, leaving any `#fragment` and any absolute URL alone. */
    static String toHtmlLink(String url) {
        if (url == null || url.isEmpty()) return url;
        if (url.startsWith("http://") || url.startsWith("https://")) return url;
        int hash = url.indexOf('#');
        String path = hash < 0 ? url : url.substring(0, hash);
        String fragment = hash < 0 ? "" : url.substring(hash);
        if (path.endsWith(".json")) {
            path = path.substring(0, path.length() - ".json".length()) + ".html";
        }
        return path + fragment;
    }

    /** Rewrites a single URL, e.g. `{{ type.url | href }}`. */
    static final class HrefFilter implements Filter {
        @Override
        public List<String> getArgumentNames() {
            return null;
        }

        @Override
        public Object apply(Object input, Map<String, Object> args, PebbleTemplate self,
                            EvaluationContext context, int lineNumber) {
            return input == null ? null : toHtmlLink(input.toString());
        }
    }

    /**
     * Rewrites every `href` inside a block of documentation HTML and marks the result safe.
     *
     * These fields arrive as HTML because they were read out of HTML, so they must not be escaped,
     * but the links inside them still name `.json` files. Marking the result safe here rather than
     * writing `| raw` at each use keeps the "this is trusted HTML" decision in one place.
     */
    static final class DocFilter implements Filter {
        private static final Pattern HREF = Pattern.compile("href=\"([^\"]*)\"");

        @Override
        public List<String> getArgumentNames() {
            return null;
        }

        @Override
        public Object apply(Object input, Map<String, Object> args, PebbleTemplate self,
                            EvaluationContext context, int lineNumber) {
            if (input == null) return null;
            Matcher matcher = HREF.matcher(input.toString());
            StringBuilder result = new StringBuilder();
            while (matcher.find()) {
                String replacement = "href=\"" + toHtmlLink(matcher.group(1)) + "\"";
                matcher.appendReplacement(result, Matcher.quoteReplacement(replacement));
            }
            matcher.appendTail(result);
            return new SafeString(result.toString());
        }
    }

    /**
     * A member anchor, as an attribute value.
     *
     * The reference site's own anchors are the member's erased signature, parentheses, commas and
     * all -- `setDefaultKeyMode(int)`, or `addContentView(android.view.View,%20android.view....)`.
     * They are already percent-encoded where doclava wrote them that way, and the links in the
     * scrape point at exactly these strings, so the only thing to do is quote the double quote
     * that would otherwise end the attribute. Escaping them further would break every inbound
     * link in the corpus.
     */
    static final class AnchorFilter implements Filter {
        @Override
        public List<String> getArgumentNames() {
            return null;
        }

        @Override
        public Object apply(Object input, Map<String, Object> args, PebbleTemplate self,
                            EvaluationContext context, int lineNumber) {
            if (input == null) return null;
            return new SafeString(input.toString().replace("&", "&amp;").replace("\"", "&quot;"));
        }
    }
}
