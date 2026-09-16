package org.appdevforall.docs.android;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Rewrites a page's links from the JSON tree to the HTML tree that mirrors it.
 *
 * The extractor writes relative paths into its own tree, because that tree is what it can see: a
 * class page links to `../view/View.json`. Both things that render those pages -- this renderer,
 * and the documentation database's server -- serve HTML at the mirrored path, so every such link
 * needs its extension swapped exactly once, before the templates ever see it.
 *
 * It happens here rather than in a Pebble filter so that the templates need no filter of their
 * own: the database stores one self-contained template per row and evaluates it against the
 * page's JSON, with no opportunity to register anything.
 *
 * `load_android_json_db.py`'s `rewrite_link` does the corresponding job for the database, and is
 * deliberately NOT identical: it also folds each path to the case the database spells it with,
 * because the database holds pages the scrape spelled two ways (`StrictMode/` and `strictmode/`)
 * and a link has to name the row that exists. There is no such second spelling here - this
 * renderer writes the HTML tree itself, mirroring the JSON tree's own paths - so the swap below
 * is the whole job. Change one and the other does not automatically need the same change.
 */
final class HtmlLinks {

    private static final Pattern HREF = Pattern.compile("href=\"([^\"]*)\"");

    private HtmlLinks() {
    }

    /** `.json` -> `.html`, leaving any `#fragment` and any absolute URL alone. */
    static String swap(String url) {
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

    /**
     * Every link in a page document, rewritten.
     *
     * Walks the whole model rather than naming the fields that hold links: a `url` can appear in
     * any of a dozen places (`inheritance`, `implements`, `seeAlso`, a group's `from`, ...) and an
     * `href` can appear inside any documentation fragment, which is most string values on the
     * page. Anything not a map, list or string is returned as it stands.
     */
    @SuppressWarnings("unchecked")
    static Object rewrite(Object node) {
        if (node instanceof Map<?, ?> map) {
            Map<String, Object> result = new LinkedHashMap<>();
            for (Map.Entry<String, Object> entry : ((Map<String, Object>) map).entrySet()) {
                Object value = entry.getValue();
                // A `url` is a bare link; every other string may be a fragment with links inside.
                if ("url".equals(entry.getKey()) && value instanceof String url) {
                    result.put(entry.getKey(), swap(url));
                } else {
                    result.put(entry.getKey(), rewrite(value));
                }
            }
            return result;
        }
        if (node instanceof List<?> list) {
            List<Object> result = new ArrayList<>(list.size());
            for (Object item : list) {
                result.add(rewrite(item));
            }
            return result;
        }
        if (node instanceof String text) {
            return rewriteHrefs(text);
        }
        return node;
    }

    private static String rewriteHrefs(String html) {
        if (!html.contains("href=\"")) return html;
        Matcher matcher = HREF.matcher(html);
        StringBuilder result = new StringBuilder();
        while (matcher.find()) {
            matcher.appendReplacement(result,
                    Matcher.quoteReplacement("href=\"" + swap(matcher.group(1)) + "\""));
        }
        matcher.appendTail(result);
        return result.toString();
    }
}
