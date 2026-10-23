package org.appdevforall.docs.render;

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
 * The two filters every template needs.
 *
 * The JSON links to `.json` files, because that is what the plugin writes. The HTML output mirrors
 * that tree file-for-file with `.html` names instead, so a link is correct in HTML as soon as its
 * extension is swapped -- the relative path itself already points at the right place. Both filters
 * here do that swap; they differ only in whether they are handed a bare URL or a block of
 * documentation HTML with URLs inside it.
 */
public final class JavadocExtension extends AbstractExtension {

    @Override
    public Map<String, Filter> getFilters() {
        return Map.of(
                "href", new HrefFilter(),
                "doc", new DocFilter()
        );
    }

    /** `.json` -> `.html`, leaving any `#anchor` and any absolute URL alone. */
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
     * Doc text arrives as HTML already -- a javadoc comment's body is HTML -- so it must not be
     * escaped, but the links it contains still point at `.json`. Marking it safe here rather than
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
}
