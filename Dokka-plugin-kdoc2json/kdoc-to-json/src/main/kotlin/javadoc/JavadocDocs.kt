package org.appdevforall.dokka.kdoc2json.javadoc

import org.jetbrains.dokka.links.DRI
import org.jetbrains.dokka.model.Documentable
import org.jetbrains.dokka.model.doc.*
// kotlin.Deprecated is a default import and would otherwise win over the star import here.
import org.jetbrains.dokka.model.doc.Deprecated as DeprecatedTag

/**
 * The javadoc block tags of one declaration, pulled out of Dokka's [DocumentationNode] and sorted
 * into the buckets a javadoc page actually renders.
 *
 * @param params `@param` text keyed by parameter name -- type parameters included, keyed by their
 *   bare name (Dokka strips the angle brackets `@param <T>` is written with).
 * @param other every block tag with no dedicated bucket (`@apiNote`, `@implSpec`, `@serial`, ...),
 *   in source order, so nothing in the source comment is silently dropped.
 */
class JavadocDocBundle(
    val description: String? = null,
    val params: Map<String, String> = emptyMap(),
    val returns: String? = null,
    val throws: List<Triple<String, DRI?, String>> = emptyList(),
    val seeAlso: List<Triple<String, DRI?, String>> = emptyList(),
    val since: List<String> = emptyList(),
    val authors: List<String> = emptyList(),
    val versions: List<String> = emptyList(),
    val deprecated: String? = null,
    val isDeprecatedTagPresent: Boolean = false,
    val other: List<JdTag> = emptyList()
)

/**
 * Turns Dokka doc trees into the HTML strings javadoc pages carry, and sorts block tags into
 * [JavadocDocBundle].
 *
 * Output is HTML rather than Markdown because that is what a javadoc comment already contains and
 * what the existing renderer emits, so a downstream template can drop either into a page unchanged.
 *
 * @param resolveLink maps a link target to a URL relative to the page being written; returns null
 *   for a target this run doesn't document, in which case the link degrades to plain text rather
 *   than becoming a dead `href`.
 */
class JavadocDocs(
    private val resolveLink: (DRI) -> String?,
    /**
     * What `{@docRoot}` expands to on the page being rendered -- the relative path back to the
     * documentation root. JDK comments use it inside raw `<a href="{@docRoot}/...">` markup, which
     * Dokka hands over as a literal attribute value, so it has to be substituted here or the link
     * ships with the tag still in it.
     */
    private val docRoot: String = ".",
    /**
     * Rebases a *relative* href written by hand in a doc comment (`<a href="Vector.html">`).
     *
     * Such an href is relative to the page that *declares* the comment. When the same comment is
     * shown somewhere else -- a summary on an index page -- it has to be re-expressed relative to
     * the page it now appears on, or it points at nothing. The identity default is correct while
     * rendering a declaration on its own page.
     */
    private val rebaseRelativeHref: (String) -> String = { it }
) {

    /** Applies [rebaseRelativeHref] to an `href` attribute, leaving every other attribute alone. */
    private fun rebased(params: Map<String, String>): Map<String, String> {
        val href = params["href"] ?: return params
        if (!isRelative(href)) return params
        return params.toMutableMap().apply { put("href", rebaseRelativeHref(href)) }
    }

    /** True for an href that resolves against the current page rather than a root or a host. */
    private fun isRelative(href: String): Boolean =
        href.isNotBlank() &&
            !href.startsWith("#") &&
            !href.startsWith("/") &&
            !href.contains("://") &&
            !href.startsWith("mailto:") &&
            !href.startsWith(DOC_ROOT_TAG)

    /**
     * Picks the doc comment to render. Javadoc has no notion of source sets, so where Dokka has
     * several this takes the first that actually carries tags, which for a Java run is the only one.
     */
    fun bundleFor(doc: Documentable): JavadocDocBundle {
        val node: DocumentationNode = doc.documentation.entries
            .firstOrNull { it.value.children.isNotEmpty() }
            ?.value
            ?: return JavadocDocBundle()

        var description: String? = null
        val params = LinkedHashMap<String, String>()
        var returns: String? = null
        val throws = mutableListOf<Triple<String, DRI?, String>>()
        val seeAlso = mutableListOf<Triple<String, DRI?, String>>()
        val since = mutableListOf<String>()
        val authors = mutableListOf<String>()
        val versions = mutableListOf<String>()
        var deprecated: String? = null
        var deprecatedPresent = false
        val other = mutableListOf<JdTag>()

        node.children.forEach { tag ->
            val text = render(tag.root).trim()
            when (tag) {
                is Description -> description = listOfNotNull(description?.takeIf { it.isNotBlank() }, text)
                    .filter { it.isNotBlank() }
                    .joinToString("\n")
                    .ifBlank { null }
                is Param -> params[tag.name] = text
                is Return -> returns = text
                is Throws -> throws += Triple(tag.name, tag.exceptionAddress, text)
                is See -> seeAlso += Triple(tag.name, tag.address, text)
                is Since -> since += unwrapParagraph(text)
                is Author -> authors += unwrapParagraph(text)
                is Version -> versions += unwrapParagraph(text)
                is DeprecatedTag -> {
                    deprecatedPresent = true
                    deprecated = text.ifBlank { null }
                }
                is CustomTagWrapper -> other += JdTag(tag.name, text)
                else -> other += JdTag(tag::class.java.simpleName, text)
            }
        }

        return JavadocDocBundle(
            description = description?.ifBlank { null },
            params = params,
            returns = returns?.ifBlank { null },
            throws = throws,
            seeAlso = seeAlso,
            since = since.filter { it.isNotBlank() },
            authors = authors.filter { it.isNotBlank() },
            versions = versions.filter { it.isNotBlank() },
            deprecated = deprecated,
            isDeprecatedTagPresent = deprecatedPresent,
            other = other.filter { it.text.isNotBlank() }
        )
    }

    /**
     * Renders one doc tree back to HTML.
     *
     * JDK javadoc comments are full HTML -- tables, definition lists, `<div class="block">` -- so
     * structural tags and their attributes are preserved rather than flattened to their text.
     * Anything Dokka parsed into a tag this doesn't know is emitted as its children, which loses
     * the wrapper but never the content.
     */
    fun render(tag: DocTag): String {
        val children = tag.children.joinToString("") { render(it) }
        return when (tag) {
            is Text -> escapeHtmlText(tag.body)
            is Br -> "<br/>"
            is HorizontalRule -> "<hr/>"
            is Img -> "<img${attributes(tag.params, docRoot)}/>"
            is CodeBlock -> "<pre><code${attributes(tag.params, docRoot)}>$children</code></pre>"
            is CodeInline -> "<code${attributes(tag.params, docRoot)}>$children</code>"
            is A -> "<a${attributes(rebased(tag.params), docRoot)}>$children</a>"
            is DocumentationLink -> {
                val href = resolveLink(tag.dri)
                // An unresolvable {@link} degrades to its own text rather than an href to nowhere:
                // javadoc-mode output is meant to be servable as-is, and a dead link is worse than
                // a plain-text mention of the symbol.
                if (href == null) children else "<a href=\"${escapeHtmlAttribute(href)}\">$children</a>"
            }
            is CustomDocTag -> children
            else -> {
                val htmlName = HTML_TAG_NAMES[tag::class.java.simpleName]
                if (htmlName == null) children else "<$htmlName${attributes(tag.params, docRoot)}>$children</$htmlName>"
            }
        }
    }

    companion object {
        /**
         * Dokka doc-tag class name -> HTML element name, for every tag that is just a wrapper
         * around its children. Tags needing special handling (links, images, code, line breaks)
         * are matched by type in [render] instead and are deliberately absent here.
         */
        private val HTML_TAG_NAMES: Map<String, String> = mapOf(
            "P" to "p", "B" to "b", "I" to "i", "Em" to "em", "Strong" to "strong",
            "BlockQuote" to "blockquote", "Pre" to "pre", "Ul" to "ul", "Ol" to "ol", "Li" to "li",
            "H1" to "h1", "H2" to "h2", "H3" to "h3", "H4" to "h4", "H5" to "h5", "H6" to "h6",
            "Dl" to "dl", "Dt" to "dt", "Dd" to "dd", "Div" to "div", "Span" to "span",
            "Table" to "table", "THead" to "thead", "TBody" to "tbody", "TFoot" to "tfoot",
            "Tr" to "tr", "Td" to "td", "Th" to "th", "Caption" to "caption",
            "Sub" to "sub", "Sup" to "sup", "Small" to "small", "Big" to "big", "Var" to "var",
            "Tt" to "tt", "U" to "u", "Strikethrough" to "del", "Cite" to "cite", "Code" to "code",
            "Dfn" to "dfn", "Mark" to "mark", "Font" to "font", "Menu" to "menu", "Dir" to "dir",
            "Section" to "section", "Main" to "main", "Nav" to "nav", "Header" to "header",
            "Footer" to "footer", "Listing" to "listing"
        )

        /**
         * Strips a single enclosing `<p>` from a one-paragraph fragment.
         *
         * Dokka wraps every tag body in a paragraph, but `@since`, `@author` and `@version` are
         * plain text in javadoc ("1.0", not "<p>1.0</p>"). Anything with internal structure is
         * left exactly as it is.
         */
        fun unwrapParagraph(html: String): String {
            val trimmed = html.trim()
            if (!trimmed.startsWith("<p>") || !trimmed.endsWith("</p>")) return trimmed
            val inner = trimmed.removePrefix("<p>").removeSuffix("</p>")
            return if (inner.contains("<p>", ignoreCase = true)) trimmed else inner.trim()
        }

        private const val DOC_ROOT_TAG = "{@docRoot}"

        /** Renders a tag's attributes back into HTML, in the order Dokka recorded them. */
        private fun attributes(params: Map<String, String>, docRoot: String): String =
            params.entries.joinToString("") { (key, value) ->
                " $key=\"${escapeHtmlAttribute(expandDocRoot(value, docRoot))}\""
            }

        /** Substitutes javadoc's `{@docRoot}` in a raw attribute value. */
        fun expandDocRoot(value: String, docRoot: String): String =
            if (DOC_ROOT_TAG in value) value.replace(DOC_ROOT_TAG, docRoot) else value

        private fun escapeHtmlText(value: String): String =
            value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        // "&" first, or escaping the rest afterwards would double-escape a "&quot;" that was
        // already literally present in the source text. "<" and ">" are escaped as well as the
        // quotes: an unescaped ">" inside an attribute value would otherwise look like the end of
        // the tag to anything scanning the markup, firstSentence's depth tracking included.
        private fun escapeHtmlAttribute(value: String): String =
            value.replace("&", "&amp;")
                .replace("\"", "&quot;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")

        /**
         * The leading sentence of an HTML description, as javadoc shows it in a summary table.
         *
         * Cuts at the first `.` that sits outside a tag and is followed by whitespace (or ends the
         * text), then closes any element the cut left open so the fragment is still well-formed
         * HTML. Returns null when there is no description to summarise.
         */
        fun firstSentence(html: String?): String? {
            if (html.isNullOrBlank()) return null
            var depth = 0
            var cut = -1
            for (i in html.indices) {
                when (html[i]) {
                    '<' -> depth++
                    '>' -> if (depth > 0) depth--
                    '.' -> if (depth == 0) {
                        val next = html.getOrNull(i + 1)
                        if (next == null || next.isWhitespace()) {
                            cut = i + 1
                        }
                    }
                }
                if (cut >= 0) break
            }
            val fragment = if (cut >= 0) html.substring(0, cut) else html
            return closeOpenTags(fragment.trim()).ifBlank { null }
        }

        private val TAG_REGEX = Regex("<\\s*(/?)\\s*([a-zA-Z][a-zA-Z0-9]*)[^>]*?(/?)\\s*>")
        private val VOID_ELEMENTS = setOf("br", "hr", "img", "input", "meta", "link", "wbr")

        /** Appends closing tags for any element left open by a truncated HTML fragment. */
        private fun closeOpenTags(fragment: String): String {
            val open = ArrayDeque<String>()
            TAG_REGEX.findAll(fragment).forEach { match ->
                val closing = match.groupValues[1] == "/"
                val name = match.groupValues[2].lowercase()
                val selfClosing = match.groupValues[3] == "/"
                if (name in VOID_ELEMENTS || selfClosing) return@forEach
                if (closing) {
                    // Tolerate stray/mismatched closers instead of corrupting the stack.
                    if (open.isNotEmpty() && open.last() == name) open.removeLast()
                    else open.remove(name)
                } else {
                    open.addLast(name)
                }
            }
            return fragment + open.reversed().joinToString("") { "</$it>" }
        }
    }
}
