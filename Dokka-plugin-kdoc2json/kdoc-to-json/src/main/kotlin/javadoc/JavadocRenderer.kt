package org.appdevforall.dokka.kdoc2json.javadoc

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.serializer
import org.appdevforall.dokka.kdoc2json.JsonFilters
import org.appdevforall.dokka.kdoc2json.JsonPluginConfig
import org.appdevforall.dokka.kdoc2json.PluginLogger
import org.jetbrains.dokka.model.Documentable
import org.jetbrains.dokka.pages.RootPageNode
import java.io.File

/**
 * Writes the Javadoc-mode output tree.
 *
 * The layout mirrors what the `javadoc` tool produces under its `api/` directory, with `.json`
 * in place of `.html`:
 *
 * ```
 * index.json                                  overview: every module (or package) in the run
 * element-list                                javadoc's plain-text manifest of modules/packages
 * allclasses-index.json                       every documented type
 * allpackages-index.json                      every documented package
 * deprecated-list.json                        deprecated elements, grouped by kind
 * constant-values.json                        static final fields with constant values
 * index-files/index-N.json                    the A-Z index, one file per letter
 * <module>/module-summary.json                (module directories only for a multi-module run)
 * <module>/<pkg/as/path>/package-summary.json
 * <module>/<pkg/as/path>/<Outer.Nested>.json
 * ```
 *
 * Only JSON (plus javadoc's own plain-text `element-list`) is written -- no HTML. Rendering the
 * pages is the downstream template engine's job.
 */
class JavadocRenderer(
    private val config: JsonPluginConfig,
    private val logger: PluginLogger,
    private val outputDir: File,
    /**
     * The modules of a multi-module Dokka run, as `name to relative output path`. Non-empty only
     * in the aggregating run that Dokka performs after the per-module ones; empty otherwise.
     */
    private val moduleReferences: List<Pair<String, String>> = emptyList(),
    /**
     * The run's configured source roots. Scanned for `module-info.java`, which is where the JPMS
     * module structure javadoc lays its `api/` tree out by actually lives -- Dokka's model has none.
     */
    private val sourceRoots: Collection<File> = emptyList()
) {

    /**
     * Javadoc-mode pages are written with `encodeDefaults = true` so every documented key is
     * present on every page, even when empty -- a template can then test a field without also
     * testing whether it exists. Callers who do want the empty keys gone still get that from
     * `omitNulls`, which is applied afterwards, so the choice stays theirs rather than being
     * baked into the serializer. No class discriminator is configured because none of the
     * javadoc DTOs are polymorphic; their `kind` fields are ordinary data.
     */
    private val json = Json {
        prettyPrint = config.prettyPrint
        encodeDefaults = true
    }

    /** What a global index page needs about one member, without holding the whole class page. */
    private class MemberRecord(
        val documentable: Documentable,
        val label: String,
        val kind: String,
        val anchor: String,
        val owner: JdType,
        val deprecated: JdDeprecation?
    )

    /** A `static final` field carrying a compile-time constant, for `constant-values.json`. */
    private class ConstantRecord(
        val owner: JdType,
        val name: String,
        val anchor: String,
        val modifiers: List<String>,
        val typeDisplay: String,
        val typeQualifiedName: String?,
        val value: String
    )

    fun render(root: RootPageNode) {
        val index = JavadocModelIndex.build(root, logger, config.sourceSetWhitelist, sourceRoots)
        if (index.types.isEmpty() && index.packages.isEmpty()) {
            // Dokka's aggregating pass over a multi-module build sees only module references, no
            // documentables -- the real pages were written by the per-module runs. Emit just the
            // overview, which is the one page that pass is actually responsible for.
            if (moduleReferences.isNotEmpty()) {
                writeAggregateOverview()
                return
            }
            logger.warn("javadoc-mode: no documented types or packages were found; nothing to write.")
            return
        }
        val mapper = JavadocMapper(index, logger)

        val members = mutableListOf<MemberRecord>()
        val constants = mutableListOf<ConstantRecord>()

        writeClassPages(index, mapper, members, constants)
        writePackagePages(index, mapper)
        writeModulePages(index, mapper)
        writeOverview(index, mapper)
        writeAllClassesIndex(index, mapper)
        writeAllPackagesIndex(index, mapper)
        writeDeprecatedList(index, mapper, members)
        writeConstantValues(index, constants)
        writeAlphabeticalIndex(index, mapper, members)
        writeElementList(index)

        logger.info(
            "javadoc-mode: wrote ${index.types.size} type page(s), ${index.packages.size} package " +
                "page(s), ${index.modules.size} module page(s) and the global index files."
        )
    }

    /**
     * The overview page for a multi-module run, listing each module's own output.
     *
     * Only the module list is available here; each module's descriptions and index files live in
     * its own output directory, written by that module's run.
     */
    private fun writeAggregateOverview() {
        logger.info("javadoc-mode: multi-module aggregation pass; writing the overview only.")
        write(
            "index.${JavadocPaths.EXTENSION}",
            JdOverviewPage(
                modules = moduleReferences
                    .sortedBy { it.first }
                    .map { (name, path) ->
                        JdModuleSummary(
                            name = name,
                            url = "$path/${JavadocPaths.MODULE_SUMMARY}.${JavadocPaths.EXTENSION}"
                        )
                    }
            )
        )
    }

    // ------------------------------------------------------------ page passes

    private fun writeClassPages(
        index: JavadocModelIndex,
        mapper: JavadocMapper,
        members: MutableList<MemberRecord>,
        constants: MutableList<ConstantRecord>
    ) {
        index.types.forEach { type ->
            val page = runCatching { mapper.classPage(type) }.getOrElse { error ->
                // One bad page must not cost every other page, matching JsonRenderer's own
                // per-page resilience.
                logger.warn("javadoc-mode: failed to build page for '${type.qualifiedName}': ${error.message}")
                return@forEach
            }
            write(type.filePath, page)
            harvest(index, type, page, members, constants)
        }
    }

    /** Collects the per-member facts the global index pages are built from. */
    private fun harvest(
        index: JavadocModelIndex,
        type: JdType,
        page: JdClassPage,
        members: MutableList<MemberRecord>,
        constants: MutableList<ConstantRecord>
    ) {
        val doc = type.documentable

        // Anchor -> declaration, first occurrence winning. `doc.functions` can also carry members
        // Dokka copied down from a supertype; the page only lists the declared ones, so matching
        // by anchor and keeping the first keeps the declaration rather than the inherited copy.
        fun <T : Documentable> byAnchor(items: List<T>, isConstructor: Boolean): Map<String, T> {
            val result = LinkedHashMap<String, T>()
            items.forEach { result.putIfAbsent(index.paths.memberAnchor(it.dri, isConstructor), it) }
            return result
        }

        val propertyByName = doc.properties.associateBy { it.name }
        val functionByAnchor = byAnchor(doc.functions, isConstructor = false)
        val constructorByAnchor = byAnchor(
            (doc as? org.jetbrains.dokka.model.WithConstructors)?.constructors.orEmpty(),
            isConstructor = true
        )
        val enumEntryByName =
            (doc as? org.jetbrains.dokka.model.DEnum)?.entries?.associateBy { it.name }.orEmpty()

        fun recordField(field: JdField, kind: String, source: Documentable?) {
            if (source != null) {
                members += MemberRecord(source, field.name, kind, field.anchor, type, field.deprecated)
            }
            val value = field.constantValue
            if (value != null && "static" in field.modifiers && "final" in field.modifiers) {
                constants += ConstantRecord(
                    owner = type,
                    name = field.name,
                    anchor = field.anchor,
                    modifiers = field.modifiers,
                    typeDisplay = field.type.display,
                    typeQualifiedName = field.type.qualifiedName,
                    value = value
                )
            }
        }

        page.fields.forEach { recordField(it, "field", propertyByName[it.name]) }
        page.enumConstants.forEach { recordField(it, "enumConstant", enumEntryByName[it.name]) }
        (page.methods + page.annotationElements).forEach { executable ->
            functionByAnchor[executable.anchor]?.let {
                members += MemberRecord(
                    it, executable.name, executable.kind, executable.anchor, type, executable.deprecated
                )
            }
        }
        page.constructors.forEach { executable ->
            constructorByAnchor[executable.anchor]?.let {
                members += MemberRecord(
                    it, executable.name, "constructor", executable.anchor, type, executable.deprecated
                )
            }
        }
    }

    private fun writePackagePages(index: JavadocModelIndex, mapper: JavadocMapper) {
        val typesByPackage = index.types.groupBy { it.packageName }
        index.packages.forEach { pkg ->
            write(pkg.filePath, mapper.packagePage(pkg, typesByPackage[pkg.name].orEmpty()))
        }
    }

    private fun writeModulePages(index: JavadocModelIndex, mapper: JavadocMapper) {
        index.modules.forEach { module ->
            val packages = index.packages.filter { it.moduleName == module.name }
            write(module.filePath, mapper.modulePage(module, packages))
        }
    }

    private fun writeOverview(index: JavadocModelIndex, mapper: JavadocMapper) {
        val path = "index.${JavadocPaths.EXTENSION}"
        val scope = mapper.scope(path)
        write(
            path,
            JdOverviewPage(
                title = index.modules.singleOrNull()?.name,
                modules = index.modules.map { mapper.moduleSummary(it, scope) },
                packages = index.packages.map { mapper.packageSummary(it, scope) }
            )
        )
    }

    private fun writeAllClassesIndex(index: JavadocModelIndex, mapper: JavadocMapper) {
        val path = "allclasses-index.${JavadocPaths.EXTENSION}"
        val scope = mapper.scope(path)
        write(
            path,
            JdAllClassesIndex(
                types = index.types
                    .map { mapper.typeSummary(it, scope) }
                    .sortedWith(compareBy({ it.name.lowercase() }, { it.qualifiedName }))
            )
        )
    }

    private fun writeAllPackagesIndex(index: JavadocModelIndex, mapper: JavadocMapper) {
        val path = "allpackages-index.${JavadocPaths.EXTENSION}"
        val scope = mapper.scope(path)
        write(path, JdAllPackagesIndex(packages = index.packages.map { mapper.packageSummary(it, scope) }))
    }

    private fun writeDeprecatedList(
        index: JavadocModelIndex,
        mapper: JavadocMapper,
        members: List<MemberRecord>
    ) {
        val path = "deprecated-list.${JavadocPaths.EXTENSION}"
        val scope = mapper.scope(path)
        val sections = linkedMapOf<String, MutableList<JdDeprecatedEntry>>()

        fun add(section: String, entry: JdDeprecatedEntry) {
            sections.getOrPut(section) { mutableListOf() } += entry
        }

        index.types.forEach { type ->
            val summary = mapper.typeSummary(type, scope)
            val deprecation = summary.deprecated ?: return@forEach
            // javadoc gives exceptions, interfaces, enums and annotations their own sections.
            add(
                when (summary.kind) {
                    "interface" -> "interfaces"
                    "enum" -> "enums"
                    "annotation" -> "annotationTypes"
                    "exception" -> "exceptions"
                    else -> "classes"
                },
                JdDeprecatedEntry(
                    element = type.qualifiedName,
                    kind = summary.kind,
                    url = scope.url(type.filePath),
                    comment = deprecation.comment,
                    forRemoval = deprecation.forRemoval,
                    since = deprecation.since
                )
            )
        }

        members.forEach { member ->
            if (member.deprecated == null) return@forEach
            // Re-rendered against this page rather than reusing the class page's copy, whose
            // links are relative to the class page.
            val deprecation = mapper.deprecationFor(member.documentable, scope) ?: return@forEach
            add(
                when (member.kind) {
                    "constructor" -> "constructors"
                    "field" -> "fields"
                    "enumConstant" -> "enumConstants"
                    "annotationElement" -> "annotationElements"
                    else -> "methods"
                },
                JdDeprecatedEntry(
                    element = "${member.owner.qualifiedName}.${member.anchor}",
                    kind = member.kind,
                    url = scope.url(member.owner.filePath, member.anchor),
                    comment = deprecation.comment,
                    forRemoval = deprecation.forRemoval,
                    since = deprecation.since
                )
            )
        }

        write(
            path,
            JdDeprecatedList(sections = sections.mapValues { (_, entries) -> entries.sortedBy { it.element } })
        )
    }

    private fun writeConstantValues(index: JavadocModelIndex, constants: List<ConstantRecord>) {
        val path = "constant-values.${JavadocPaths.EXTENSION}"
        val paths = index.paths
        val byPackage = constants
            .groupBy { it.owner.packageName }
            .toSortedMap()
            .mapValues { (_, records) ->
                records.groupBy { it.owner }
                    .toList()
                    .sortedBy { it.first.qualifiedName }
                    .map { (owner, fields) ->
                        JdConstantsForType(
                            qualifiedName = owner.qualifiedName,
                            url = paths.relativeUrl(path, owner.filePath),
                            fields = fields.sortedBy { it.name }.map { record ->
                                JdConstantField(
                                    name = record.name,
                                    modifiers = record.modifiers,
                                    type = JdTypeRef(
                                        display = record.typeDisplay,
                                        qualifiedName = record.typeQualifiedName,
                                        url = record.typeQualifiedName
                                            ?.let { index.typeForKey(it) }
                                            ?.let { paths.relativeUrl(path, it.filePath) }
                                    ),
                                    value = record.value,
                                    url = "${paths.relativeUrl(path, owner.filePath)}#${record.anchor}"
                                )
                            }
                        )
                    }
            }
        write(path, JdConstantValues(packages = byPackage))
    }

    /**
     * javadoc's A-Z index, split one file per letter under `index-files/`.
     *
     * Every documented element -- module, package, type, field, constructor, method -- gets an
     * entry, which is what makes the index usable as a search backing store downstream.
     */
    private fun writeAlphabeticalIndex(
        index: JavadocModelIndex,
        mapper: JavadocMapper,
        members: List<MemberRecord>
    ) {
        class PendingEntry(
            val label: String,
            val kind: String,
            val filePath: String,
            val anchor: String?,
            val containingElement: String?,
            val documentable: Documentable?,
            val deprecated: Boolean,
            /** Set for a member, so its summary resolves `{@inheritDoc}` as its own page does. */
            val owner: JdType? = null
        )

        val pending = mutableListOf<PendingEntry>()

        index.modules.forEach {
            pending += PendingEntry(it.name, "module", it.filePath, null, null, it.documentables.firstOrNull(), false)
        }
        index.packages.forEach {
            pending += PendingEntry(it.name, "package", it.filePath, null, it.moduleName, it.documentables.firstOrNull(), false)
        }
        index.types.forEach { type ->
            pending += PendingEntry(
                label = type.classNames,
                kind = if (index.isException(type.key)) "exception" else type.kind,
                filePath = type.filePath,
                anchor = null,
                containingElement = type.packageName,
                documentable = type.documentable,
                deprecated = false
            )
        }
        members.forEach { member ->
            pending += PendingEntry(
                label = if (member.kind == "constructor") member.owner.simpleName else member.label,
                kind = member.kind,
                filePath = member.owner.filePath,
                anchor = member.anchor,
                containingElement = member.owner.qualifiedName,
                documentable = member.documentable,
                deprecated = member.deprecated != null,
                owner = member.owner
            )
        }

        val grouped = pending
            .sortedWith(compareBy({ it.label.lowercase() }, { it.containingElement.orEmpty() }, { it.kind }))
            .groupBy { groupLabelFor(it.label) }

        // Symbols sort ahead of letters, which is also where javadoc puts them.
        val letters = grouped.keys.sortedWith(compareBy({ it != SYMBOL_GROUP }, { it }))

        letters.forEachIndexed { position, letter ->
            val number = position + 1
            val path = "index-files/index-$number.${JavadocPaths.EXTENSION}"
            val scope = mapper.scope(path)
            val entries = grouped.getValue(letter).map { entry ->
                JdIndexEntry(
                    label = entry.label,
                    kind = entry.kind,
                    url = scope.url(entry.filePath, entry.anchor),
                    containingElement = entry.containingElement,
                    firstSentence = entry.documentable?.let {
                        mapper.summaryFor(it, scope, entry.owner, entry.anchor)
                    },
                    deprecated = entry.deprecated
                )
            }
            write(path, JdIndexPage(letter = letter, index = number, letters = letters, entries = entries))
        }
    }

    /**
     * javadoc's plain-text manifest of what this documentation covers -- the file downstream
     * tooling reads to resolve external links into this output. Modular runs list each module
     * with `module:` followed by its packages; non-modular runs list packages alone.
     */
    private fun writeElementList(index: JavadocModelIndex) {
        val content = buildString {
            if (index.modules.size > 1) {
                index.modules.forEach { module ->
                    appendLine("module:${module.name}")
                    index.packages.filter { it.moduleName == module.name }
                        .map { it.name }
                        .sorted()
                        .forEach { appendLine(it) }
                }
                // A package Dokka surfaced without attaching it to a module would otherwise be
                // absent from the manifest entirely; list it unqualified rather than lose it.
                index.packages.filter { it.moduleName == null }
                    .map { it.name }
                    .sorted()
                    .forEach { appendLine(it) }
            } else {
                index.packages.map { it.name }.sorted().forEach { appendLine(it) }
            }
        }
        val file = File(outputDir, "element-list")
        file.parentFile?.mkdirs()
        file.writeText(content)
    }

    // ------------------------------------------------------------------ i/o

    private inline fun <reified T> write(relativePath: String, value: T) {
        try {
            val element: JsonElement = json.encodeToJsonElement(serializer<T>(), value)
            val filtered = JsonFilters.filterJson(element, config.omitFields, config.omitNulls)
            val file = File(outputDir, relativePath)
            file.parentFile?.mkdirs()
            // Belt and braces: the {@inheritDoc} stand-in is resolved wherever it is meant to be,
            // but one leaking into published output would be visible corruption, so any straggler
            // is dropped here rather than shipped.
            file.writeText(
                json.encodeToString(JsonElement.serializer(), filtered)
                    .replace(JavadocMapper.INHERIT_DOC_MARKER, "")
            )
            logger.debug("javadoc-mode: wrote $relativePath")
        } catch (e: Exception) {
            logger.warn("javadoc-mode: failed to write $relativePath: ${e.message}")
        }
    }

    private companion object {
        const val SYMBOL_GROUP = "SYMBOLS"

        /** The A-Z bucket a label belongs to; anything not starting with a letter is a symbol. */
        fun groupLabelFor(label: String): String {
            val first = label.firstOrNull() ?: return SYMBOL_GROUP
            return if (first.isLetter()) first.uppercaseChar().toString() else SYMBOL_GROUP
        }
    }
}
