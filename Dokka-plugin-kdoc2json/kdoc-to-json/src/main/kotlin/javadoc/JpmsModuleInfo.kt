package org.appdevforall.dokka.kdoc2json.javadoc

import org.appdevforall.dokka.kdoc2json.PluginLogger
import java.io.File

/** One `requires` directive. */
class JpmsRequires(val module: String, val isTransitive: Boolean, val isStatic: Boolean)

/** One `exports` or `opens` directive; [to] is empty for an unqualified one. */
class JpmsExports(val packageName: String, val to: List<String>)

/** One `provides ... with ...` directive. */
class JpmsProvides(val service: String, val implementations: List<String>)

/**
 * A JPMS module descriptor, read straight from a `module-info.java` in a source root.
 *
 * Dokka's model has no notion of JPMS: a Dokka "module" is a build-level grouping, so the module
 * directories, the requires/exports/uses/provides tables and the module description that javadoc's
 * module-summary page is built from are simply absent from it. They are all right there in
 * `module-info.java` though, so Javadoc mode reads that file directly rather than doing without.
 *
 * @param description the module's doc comment with its block tags removed, still carrying javadoc
 *   inline tags (`{@link ...}`, `{@code ...}`). Resolving those needs the type index, so it is
 *   left to [JavadocMapper] rather than done here.
 */
class JpmsModuleInfo(
    val name: String,
    val sourceRoot: File,
    val description: String?,
    val since: List<String>,
    val requires: List<JpmsRequires>,
    val exports: List<JpmsExports>,
    val opens: List<JpmsExports>,
    val uses: List<String>,
    val provides: List<JpmsProvides>,
    val tags: List<JdTag>
) {
    /** Packages this module exports to everyone -- exactly the set javadoc documents. */
    val exportedPackages: List<String> get() = exports.filter { it.to.isEmpty() }.map { it.packageName }
}

/**
 * Finds and parses the `module-info.java` at the root of each configured source root.
 *
 * A source root holding a `module-info.java` *is* a JPMS module root, which makes this a
 * self-validating signal: an ordinary `src/main/java` root has no such file, so nothing is
 * misidentified as a module and non-modular projects are unaffected.
 */
object JpmsModuleScanner {

    // A directive is terminated by ';', and '[^;]' matches newlines, so multi-line `to` and
    // `with` lists are handled without needing DOT_MATCHES_ALL.
    private val MODULE_DECL = Regex("""\bmodule\s+([\w.]+)\s*\{""")
    private val REQUIRES = Regex("""\brequires\s+((?:transitive\s+|static\s+)*)([\w.]+)\s*;""")
    private val EXPORTS = Regex("""\bexports\s+([\w.]+)\s*(?:to\s+([^;]+?))?\s*;""")
    private val OPENS = Regex("""\bopens\s+([\w.]+)\s*(?:to\s+([^;]+?))?\s*;""")
    private val USES = Regex("""\buses\s+([\w.$]+)\s*;""")
    private val PROVIDES = Regex("""\bprovides\s+([\w.$]+)\s+with\s+([^;]+?)\s*;""")

    private val BLOCK_COMMENT = Regex("""/\*.*?\*/""", RegexOption.DOT_MATCHES_ALL)
    private val LINE_COMMENT = Regex("""//[^\n]*""")
    private val DOC_COMMENT = Regex("""/\*\*(.*?)\*/""", RegexOption.DOT_MATCHES_ALL)
    private val BLOCK_TAG = Regex("""^\s*@(\w+)\s*(.*)$""")

    private const val MODULE_INFO = "module-info.java"

    fun scan(sourceRoots: Collection<File>, logger: PluginLogger): List<JpmsModuleInfo> {
        logger.debug("javadoc-mode: scanning ${sourceRoots.size} source root entry/entries for $MODULE_INFO")
        val found = LinkedHashMap<String, JpmsModuleInfo>()
        sourceRoots.forEach { entry ->
            // Dokka hands over source roots either as directories or, when the Gradle plugin has
            // already expanded a source set, as the individual files in them. Both spellings of
            // "here is a module root" are accepted.
            val moduleInfo = when {
                entry.isDirectory -> File(entry, MODULE_INFO)
                entry.name == MODULE_INFO -> entry
                else -> return@forEach
            }
            if (!moduleInfo.isFile) return@forEach
            val root = moduleInfo.parentFile ?: return@forEach
            try {
                val parsed = parse(moduleInfo, root)
                // Two source roots for the same module (e.g. a split main/generated layout) would
                // otherwise fight over the mapping; the first wins, as it does for packages.
                if (found.putIfAbsent(parsed.name, parsed) != null) {
                    logger.warn("javadoc-mode: module '${parsed.name}' declared in more than one source root; using the first.")
                }
            } catch (e: Exception) {
                logger.warn("javadoc-mode: could not parse ${moduleInfo.path}: ${e.message}")
            }
        }
        if (found.isNotEmpty()) {
            logger.info("javadoc-mode: found ${found.size} JPMS module descriptor(s): ${found.keys.sorted().joinToString(", ")}")
        }
        return found.values.toList()
    }

    private fun parse(moduleInfo: File, root: File): JpmsModuleInfo {
        val text = moduleInfo.readText()
        val (description, since, tags) = parseDocComment(text)

        // Comments are stripped before the directives are read, so a commented-out `exports` is
        // never mistaken for a live one.
        val body = LINE_COMMENT.replace(BLOCK_COMMENT.replace(text, " "), " ")
        val name = MODULE_DECL.find(body)?.groupValues?.get(1) ?: root.name

        fun moduleList(raw: String?): List<String> =
            raw?.split(',')?.map { it.trim() }?.filter { it.isNotEmpty() }.orEmpty()

        return JpmsModuleInfo(
            name = name,
            sourceRoot = root,
            description = description,
            since = since,
            requires = REQUIRES.findAll(body).map { match ->
                val modifiers = match.groupValues[1]
                JpmsRequires(
                    module = match.groupValues[2],
                    isTransitive = modifiers.contains("transitive"),
                    isStatic = modifiers.contains("static")
                )
            }.toList(),
            exports = EXPORTS.findAll(body).map {
                JpmsExports(it.groupValues[1], moduleList(it.groupValues[2].ifBlank { null }))
            }.toList(),
            opens = OPENS.findAll(body).map {
                JpmsExports(it.groupValues[1], moduleList(it.groupValues[2].ifBlank { null }))
            }.toList(),
            uses = USES.findAll(body).map { it.groupValues[1] }.toList(),
            provides = PROVIDES.findAll(body).map {
                JpmsProvides(it.groupValues[1], moduleList(it.groupValues[2]))
            }.toList(),
            tags = tags
        )
    }

    /**
     * Pulls the module's doc comment apart into description, `@since`, and every other block tag.
     *
     * The comment taken is the last one before the `module` declaration -- `module-info.java`
     * opens with a license header, which must not be mistaken for the module's documentation.
     */
    private fun parseDocComment(text: String): Triple<String?, List<String>, List<JdTag>> {
        val declarationAt = MODULE_DECL.find(text)?.range?.first ?: text.length
        val comment = DOC_COMMENT.findAll(text)
            .lastOrNull { it.range.last < declarationAt }
            ?.groupValues?.get(1)
            ?: return Triple(null, emptyList(), emptyList())

        val lines = comment.lines().map { it.trim().removePrefix("*").let { l -> if (l.startsWith(" ")) l.substring(1) else l } }

        val descriptionLines = mutableListOf<String>()
        val since = mutableListOf<String>()
        val tags = mutableListOf<JdTag>()
        var currentTag: String? = null
        val currentText = StringBuilder()

        fun flush() {
            val tag = currentTag ?: return
            val value = currentText.toString().trim()
            if (tag == "since") since += value else tags += JdTag(tag, value)
            currentTag = null
            currentText.setLength(0)
        }

        lines.forEach { line ->
            val match = BLOCK_TAG.find(line)
            if (match != null) {
                flush()
                currentTag = match.groupValues[1]
                currentText.append(match.groupValues[2])
            } else if (currentTag != null) {
                currentText.append('\n').append(line)
            } else {
                descriptionLines += line
            }
        }
        flush()

        val description = descriptionLines.joinToString("\n").trim().ifBlank { null }
        return Triple(description, since.filter { it.isNotBlank() }, tags.filter { it.text.isNotBlank() || it.name == "moduleGraph" })
    }
}
