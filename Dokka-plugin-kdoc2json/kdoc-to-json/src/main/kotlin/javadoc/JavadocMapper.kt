package org.appdevforall.dokka.kdoc2json.javadoc

import org.appdevforall.dokka.kdoc2json.PluginLogger
import org.jetbrains.dokka.links.DRI
import org.jetbrains.dokka.model.*

/**
 * Builds javadoc-shaped page DTOs from Dokka's model.
 *
 * Every method that produces a page takes the output-relative path of the file being written,
 * because javadoc links relatively (`../lang/Object.json`) and a link is therefore only meaningful
 * with respect to the page it appears on. [PageScope] binds that path once and carries it through
 * type references, member links and rendered doc comments.
 */
class JavadocMapper(
    private val index: JavadocModelIndex,
    private val logger: PluginLogger
) {

    companion object {
        /** Java modifier order, as javadoc prints it; anything unrecognized is appended after. */
        private val MODIFIER_ORDER = listOf(
            "public", "protected", "private", "abstract", "default", "static", "final",
            "sealed", "non-sealed", "transient", "volatile", "synchronized", "native", "strictfp"
        )

        /** Dokka spells a "no modifier" visibility/modifier as an empty or Kotlin-only name. */
        private val NON_JAVA_MODIFIERS = setOf("", "open", "empty", "final_kotlin")

        private const val OBJECT_SIMPLE_NAME = "Object"

        /** A javadoc inline tag: `{@code x}`, `{@link a.B#c label}`, `{@docRoot}`. */
        private val INLINE_TAG = Regex("""\{@(\w+)\s*([^}]*)\}""")

        /**
         * Stand-in for `{@inheritDoc}`, substituted into the sources before analysis.
         *
         * Dokka's own `{@inheritDoc}` resolver recurses without bound on parts of the JDK
         * (`InheritDocTagResolver.resolveThrowsTag` -> `toInheritDocHtml`) and takes the whole run
         * down with a StackOverflowError. Rewriting the tag to an inert text marker before Dokka
         * sees it sidesteps that, and this mapper resolves the marker itself -- walking the same
         * supertype chain "Overrides:" and "Specified by:" are derived from, which is what javadoc
         * does. See scripts/java/stage_jdk_sources.py.
         */
        const val INHERIT_DOC_MARKER = "ADFAINHERITDOC"

        /** Depth cap for chained `{@inheritDoc}`, in case a hierarchy is cyclic after merging. */
        private const val MAX_INHERIT_DEPTH = 16

        /** Above this many related packages javadoc drops the siblings -- see [relatedPackages]. */
        private const val MAX_RELATED_PACKAGES = 5

        /** The stand-in occupying a paragraph of its own, the usual way `{@inheritDoc}` is written. */
        private val MARKER_PARAGRAPH = Regex("""<p>\s*$INHERIT_DOC_MARKER\s*</p>""")
    }

    // Anchors of the members each type declares itself, used to derive Overrides/Specified by.
    // Keyed by type key; built on demand because most runs only touch part of the graph.
    private val declaredMemberAnchors = mutableMapOf<String, Set<String>>()

    /** A single output file, and everything that has to be resolved relative to it. */
    inner class PageScope(private val fromFile: String) {

        val docs = JavadocDocs(resolveLink = { dri -> linkFor(dri) }, docRoot = pathToRoot())

        /**
         * A renderer for a comment that *belongs* to the page at [declaringFile] but is being
         * shown on this one -- a summary sentence on an index page. Hand-written relative links
         * inside it are rebased from that page's directory to this one's.
         */
        fun docsFrom(declaringFile: String): JavadocDocs {
            if (declaringFile == fromFile) return docs
            return JavadocDocs(
                resolveLink = { dri -> linkFor(dri) },
                docRoot = pathToRoot(),
                rebaseRelativeHref = { href -> rebase(declaringFile, href) }
            )
        }

        /** Re-expresses [href], written relative to [declaringFile], relative to this page. */
        private fun rebase(declaringFile: String, href: String): String {
            val anchorAt = href.indexOf('#')
            val path = if (anchorAt < 0) href else href.substring(0, anchorAt)
            val anchor = if (anchorAt < 0) "" else href.substring(anchorAt)
            if (path.isEmpty()) return href
            val declaringDir = declaringFile.substringBeforeLast('/', "")
            val absolute = normalize(if (declaringDir.isEmpty()) path else "$declaringDir/$path")
            return index.paths.relativeUrl(fromFile, absolute) + anchor
        }

        /** Collapses `.` and `..` segments so the result can be compared against page paths. */
        private fun normalize(path: String): String {
            val parts = mutableListOf<String>()
            path.split('/').forEach { segment ->
                when (segment) {
                    "", "." -> Unit
                    ".." -> if (parts.isNotEmpty()) parts.removeAt(parts.size - 1)
                    else -> parts += segment
                }
            }
            return parts.joinToString("/")
        }

        /** Output-relative path [targetFile], expressed relative to this page. */
        fun url(targetFile: String, anchor: String? = null): String {
            val relative = index.paths.relativeUrl(fromFile, targetFile)
            return if (anchor.isNullOrBlank()) relative else "$relative#$anchor"
        }

        /** A link to whatever [dri] points at, or null when this run doesn't document it. */
        fun linkFor(dri: DRI): String? {
            val ownerKey = JavadocModelIndex.keyOf(dri)
            val type = index.typeForKey(ownerKey) ?: return null
            val callable = dri.callable ?: return url(type.filePath)
            val isConstructor = isConstructorCallableName(callable.name, type.simpleName)
            return url(type.filePath, index.paths.memberAnchor(dri, isConstructor))
        }

        /** A reference to a documented type by key, or a name-only reference when undocumented. */
        fun typeRefForKey(key: String, display: String = key.substringAfterLast('.')): JdTypeRef {
            val type = index.typeForKey(key)
            return JdTypeRef(
                display = if (type != null) type.classNames else display,
                qualifiedName = key,
                url = type?.let { url(it.filePath) },
                kind = type?.kind
            )
        }

        /** A reference to a type *use*, keeping its type arguments and array dimensions. */
        fun typeRef(bound: Bound): JdTypeRef {
            val dri = boundDri(bound)
            val type = dri?.let { index.typeForKey(JavadocModelIndex.keyOf(it)) }
            return JdTypeRef(
                display = renderBound(bound),
                qualifiedName = dri?.let { JavadocModelIndex.keyOf(it) },
                url = type?.let { url(it.filePath) },
                kind = type?.kind
            )
        }

        /**
         * Resolves a javadoc reference written as text -- `java.sql.Driver`, `Connection#close()`
         * -- to a URL, or null when this run doesn't document it. Used for the inline tags in
         * comment text the plugin parsed itself.
         */
        fun linkForReference(reference: String): String? {
            val typePart = reference.substringBefore('#').trim().trimEnd('.')
            val memberPart = reference.substringAfter('#', "").trim()
            val type = index.typeForKey(typePart)
                // A bare `#member` reference, or a simple name, can't be resolved without a
                // context type; only fully qualified references are linked.
                ?: return null
            if (memberPart.isEmpty()) return url(type.filePath)
            // The text form carries the *declared* parameter types, which are not necessarily the
            // erased ones the anchor uses, so only the no-arg form is linked precisely.
            return url(type.filePath, memberPart)
        }

        /** A relative path from this page back to the output root, for `{@docRoot}`. */
        fun pathToRoot(): String {
            val depth = fromFile.count { it == '/' }
            return if (depth == 0) "." else List(depth) { ".." }.joinToString("/")
        }

        fun seeRefs(bundle: JavadocDocBundle): List<JdSeeRef> = bundle.seeAlso.map { (name, address, text) ->
            JdSeeRef(
                // Dokka puts the referenced symbol in the tag's name and any trailing label in
                // its body; javadoc shows the label when there is one.
                label = text.ifBlank { name },
                url = address?.let { linkFor(it) },
                qualifiedName = address?.let { JavadocModelIndex.keyOf(it) }
            )
        }

        fun throwsList(bundle: JavadocDocBundle): List<JdThrows> = bundle.throws.map { (name, address, text) ->
            JdThrows(
                type = JdTypeRef(
                    display = name.substringAfterLast('.'),
                    qualifiedName = address?.let { JavadocModelIndex.keyOf(it) } ?: name,
                    url = address?.let { linkFor(it) },
                    kind = address?.let { index.typeForKey(JavadocModelIndex.keyOf(it))?.kind }
                ),
                description = text.ifBlank { null }
            )
        }
    }

    fun scope(fromFile: String) = PageScope(fromFile)

    // ------------------------------------------------------------------ pages

    fun classPage(type: JdType): JdClassPage {
        val scope = PageScope(type.filePath)
        val doc = type.documentable
        val bundle = scope.docs.bundleFor(doc)

        val generics = (doc as? WithGenerics)?.generics.orEmpty()
        val superclassKey = index.superclassOf(type.key)
        val declaredInterfaceKeys = index.directInterfacesOf(type.key)

        // Supertype *uses* keep their type arguments (`AbstractList<E>`), which the key-only
        // hierarchy maps can't carry, so they are read back off the documentable here.
        val supertypeUses = (doc as? WithSupertypes)?.supertypes?.values?.flatten()
            ?.distinctBy { JavadocModelIndex.keyOf(it.typeConstructor.dri) }
            .orEmpty()
            .associate { JavadocModelIndex.keyOf(it.typeConstructor.dri) to it.typeConstructor }

        val superclassRef = superclassKey?.let { key ->
            supertypeUses[key]?.let { scope.typeRef(it) } ?: scope.typeRefForKey(key)
        }
        val superinterfaceRefs = declaredInterfaceKeys.map { key ->
            supertypeUses[key]?.let { scope.typeRef(it) } ?: scope.typeRefForKey(key)
        }

        val members = membersOf(type, scope)
        val modifiers = modifiersOf(doc)

        val nestedTypes = doc.classlikes
            .mapNotNull { index.typeFor(it.dri) }
            .sortedBy { it.simpleName }
            .map { nested ->
                // Rendered in *this* page's scope, not the nested type's own, so the links inside
                // the summary resolve relative to the page the summary appears on.
                val nestedBundle = scope.docs.bundleFor(nested.documentable)
                JdNestedTypeRef(
                    name = nested.classNames,
                    qualifiedName = nested.qualifiedName,
                    kind = nested.kind,
                    modifiers = modifiersOf(nested.documentable),
                    url = scope.url(nested.filePath),
                    firstSentence = JavadocDocs.firstSentence(nestedBundle.description),
                    deprecated = deprecationOf(nested.documentable, nestedBundle)
                )
            }

        val isInterfaceLike = type.kind == "interface" || type.kind == "annotation"
        val inheritanceRefs =
            if (isInterfaceLike) emptyList()
            else index.inheritanceChain(type.key).map { scope.typeRefForKey(it) }

        return JdClassPage(
            kind = if (index.isException(type.key)) "exception" else type.kind,
            name = type.classNames,
            simpleName = type.simpleName,
            qualifiedName = type.qualifiedName,
            packageName = type.packageName,
            moduleName = type.moduleName,
            moduleUrl = moduleUrlFor(type.moduleName, scope),
            packageUrl = index.packages.firstOrNull { it.name == type.packageName }
                ?.let { scope.url(it.filePath) },
            url = type.filePath,
            modifiers = modifiers,
            signature = classSignature(type, modifiers, generics, superclassRef, superinterfaceRefs, scope),
            typeParameters = typeParameters(generics, bundle, scope),
            superclass = superclassRef,
            superinterfaces = superinterfaceRefs,
            inheritance = inheritanceRefs,
            allImplementedInterfaces =
                if (isInterfaceLike) emptyList()
                else index.allSuperinterfaces(type.key).map { scope.typeRefForKey(it) },
            allSuperinterfaces =
                if (isInterfaceLike) index.allSuperinterfaces(type.key).map { scope.typeRefForKey(it) }
                else emptyList(),
            directKnownSubclasses = index.directKnownSubclasses(type.key).map { scope.typeRefForKey(it) },
            allKnownSubinterfaces = index.allKnownSubinterfaces(type.key).map { scope.typeRefForKey(it) },
            allKnownImplementingClasses = index.allKnownImplementingClasses(type.key).map { scope.typeRefForKey(it) },
            enclosingType = index.enclosingTypeOf(type)?.let { scope.typeRefForKey(it.key) },
            isFunctionalInterface = isFunctionalInterface(type, members.methods),
            description = bundle.description,
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            since = bundle.since,
            seeAlso = scope.seeRefs(bundle),
            authors = bundle.authors,
            versions = bundle.versions,
            deprecated = deprecationOf(doc, bundle),
            annotations = annotationNamesOf(doc),
            tags = bundle.other,
            nestedTypes = nestedTypes,
            enumConstants = members.enumConstants,
            fields = members.fields,
            constructors = members.constructors,
            methods = members.methods,
            annotationElements = members.annotationElements,
            inheritedFields = members.inheritedFields,
            inheritedMethods = members.inheritedMethods,
            inheritedNestedTypes = inheritedNestedTypes(type, scope)
        )
    }

    fun packagePage(pkg: JdPackage, typesInPackage: List<JdType>): JdPackagePage {
        val scope = PageScope(pkg.filePath)
        val bundle = pkg.documentables
            .map { scope.docs.bundleFor(it) }
            .firstOrNull { it.description != null || it.other.isNotEmpty() }
            ?: JavadocDocBundle()

        val summaries = typesInPackage.sortedBy { it.classNames }.map { typeSummary(it, scope) }
        fun of(vararg kinds: String) = summaries.filter { it.kind in kinds }

        return JdPackagePage(
            name = pkg.name,
            moduleName = pkg.moduleName,
            moduleUrl = moduleUrlFor(pkg.moduleName, scope),
            url = pkg.filePath,
            description = bundle.description,
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            since = bundle.since,
            seeAlso = scope.seeRefs(bundle),
            deprecated = pkg.documentables.firstNotNullOfOrNull { deprecationOf(it, bundle) },
            tags = bundle.other,
            relatedPackages = relatedPackages(pkg).map { packageSummary(it, scope) },
            interfaces = of("interface"),
            classes = of("class", "object"),
            enums = of("enum"),
            records = of("record"),
            exceptions = of("exception"),
            annotationTypes = of("annotation"),
            allTypes = summaries
        )
    }

    fun modulePage(module: JdModule, packagesInModule: List<JdPackage>): JdModulePage {
        val scope = PageScope(module.filePath)
        val bundle = module.documentables
            .map { scope.docs.bundleFor(it) }
            .firstOrNull { it.description != null || it.other.isNotEmpty() }
            ?: JavadocDocBundle()

        val jpms = module.jpms
        // A JPMS module's documentation lives in module-info.java, which Dokka does not read, so
        // it is preferred over whatever the Dokka module happens to carry (usually nothing).
        val description = jpms?.description?.let { renderJavadocText(it, scope) } ?: bundle.description
        val documentedPackages = packagesInModule.map { packageSummary(it, scope) }
        val documentedByName = documentedPackages.associateBy { it.name }

        return JdModulePage(
            name = module.name,
            url = module.filePath,
            description = description,
            firstSentence = JavadocDocs.firstSentence(description),
            since = jpms?.since?.takeIf { it.isNotEmpty() } ?: bundle.since,
            seeAlso = scope.seeRefs(bundle),
            deprecated = module.documentables.firstNotNullOfOrNull { deprecationOf(it, bundle) },
            tags = jpms?.tags?.takeIf { it.isNotEmpty() } ?: bundle.other,
            packages = documentedPackages,
            requires = jpms?.requires.orEmpty().map { requires ->
                JdModuleRequires(
                    module = requires.module,
                    isTransitive = requires.isTransitive,
                    isStatic = requires.isStatic,
                    url = index.modules.firstOrNull { it.name == requires.module }
                        ?.let { scope.url(it.filePath) }
                )
            },
            exports = jpms?.exports.orEmpty().map { export ->
                val documented = documentedByName[export.packageName]
                JdModuleExport(
                    packageName = export.packageName,
                    to = export.to,
                    // A qualified export is not documented, so it has no page to link to.
                    url = documented?.url,
                    firstSentence = documented?.firstSentence
                )
            },
            opens = jpms?.opens.orEmpty().map { opens ->
                val documented = documentedByName[opens.packageName]
                JdModuleExport(
                    packageName = opens.packageName,
                    to = opens.to,
                    url = documented?.url,
                    firstSentence = documented?.firstSentence
                )
            },
            indirectRequires = indirectlyReadable(module).map { name ->
                JdModuleRequires(module = name, isTransitive = true, url = moduleUrlFor(name, scope))
            },
            indirectExports = readableThrough(module)
                .mapNotNull { name -> index.modules.firstOrNull { it.name == name } }
                .filter { it.jpms?.exportedPackages?.isNotEmpty() == true }
                .sortedBy { it.name }
                .map { readable ->
                    JdIndirectExport(
                        module = readable.name,
                        moduleUrl = scope.url(readable.filePath),
                        packages = readable.jpms?.exportedPackages.orEmpty().sorted().map { packageName ->
                            JdPackageSummary(
                                name = packageName,
                                moduleName = readable.name,
                                url = index.packages.firstOrNull { it.name == packageName }
                                    ?.let { scope.url(it.filePath) }
                            )
                        }
                    )
                },
            uses = jpms?.uses.orEmpty().map { scope.typeRefForKey(it) },
            provides = jpms?.provides.orEmpty().map { provides ->
                JdModuleProvides(
                    service = scope.typeRefForKey(provides.service),
                    implementations = provides.implementations.map { scope.typeRefForKey(it) }
                )
            }
        )
    }

    /**
     * Renders raw javadoc comment text -- text this plugin read itself rather than getting from
     * Dokka, i.e. `module-info.java`'s doc comment.
     *
     * Only the inline tags are handled: block-level HTML in a javadoc comment is already HTML and
     * passes through untouched. An `{@link}` whose target this run does not document degrades to
     * `<code>` rather than becoming a dead link, matching what [JavadocDocs] does for the doc
     * trees Dokka hands over.
     */
    private fun renderJavadocText(raw: String, scope: PageScope): String {
        var result = INLINE_TAG.replace(raw) { match ->
            val tag = match.groupValues[1]
            val body = match.groupValues[2].trim()
            when (tag) {
                "code", "literal" -> {
                    val escaped = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    if (tag == "code") "<code>$escaped</code>" else escaped
                }
                "link", "linkplain" -> {
                    val target = body.substringBefore(' ').trim()
                    val label = body.substringAfter(' ', "").trim().ifBlank { target.substringAfterLast('.') }
                    val href = scope.linkForReference(target)
                    val text = if (tag == "link") "<code>$label</code>" else label
                    if (href == null) text else "<a href=\"$href\">$text</a>"
                }
                // {@docRoot} is a path back to the documentation root, which is exactly what a
                // relative link from this page to the root looks like.
                "docRoot" -> scope.pathToRoot()
                else -> body
            }
        }
        // Collapse the blank lines a stripped block-tag section can leave behind.
        result = result.trim()
        return result
    }

    // ------------------------------------------------------------- summaries

    fun typeSummary(type: JdType, scope: PageScope): JdTypeSummary {
        val bundle = scope.docsFrom(type.filePath).bundleFor(type.documentable)
        return JdTypeSummary(
            name = type.classNames,
            qualifiedName = type.qualifiedName,
            kind = if (index.isException(type.key)) "exception" else type.kind,
            packageName = type.packageName,
            moduleName = type.moduleName,
            url = scope.url(type.filePath),
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            deprecated = deprecationOf(type.documentable, bundle),
            modifiers = modifiersOf(type.documentable)
        )
    }

    fun packageSummary(pkg: JdPackage, scope: PageScope): JdPackageSummary {
        val bundle = pkg.documentables
            .map { scope.docs.bundleFor(it) }
            .firstOrNull { it.description != null }
            ?: JavadocDocBundle()
        return JdPackageSummary(
            name = pkg.name,
            moduleName = pkg.moduleName,
            url = scope.url(pkg.filePath),
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            deprecated = pkg.documentables.firstNotNullOfOrNull { deprecationOf(it, bundle) }
        )
    }

    fun moduleSummary(module: JdModule, scope: PageScope): JdModuleSummary {
        val bundle = module.documentables
            .map { scope.docs.bundleFor(it) }
            .firstOrNull { it.description != null }
            ?: JavadocDocBundle()
        // As on the module page itself, module-info.java's own comment is the real source of a
        // module's description -- Dokka's module carries none -- so the overview's Description
        // column is empty without this.
        val description = module.jpms?.description?.let { renderJavadocText(it, scope) }
            ?: bundle.description
        return JdModuleSummary(
            name = module.name,
            url = scope.url(module.filePath),
            firstSentence = JavadocDocs.firstSentence(description)
        )
    }

    // --------------------------------------------------------------- members

    /** Everything a class page lists, split the way javadoc splits it into tables. */
    class ClassMembers(
        val enumConstants: List<JdField> = emptyList(),
        val fields: List<JdField> = emptyList(),
        val constructors: List<JdExecutable> = emptyList(),
        val methods: List<JdExecutable> = emptyList(),
        val annotationElements: List<JdExecutable> = emptyList(),
        val inheritedFields: List<JdInheritedMembers> = emptyList(),
        val inheritedMethods: List<JdInheritedMembers> = emptyList()
    )

    /**
     * The members of a type, sorted into javadoc's four buckets (declared/inherited x field/method).
     *
     * Dokka merges a private Java field and its accessor pair into a single `DProperty` with a
     * non-null `getter`, the way a Kotlin property looks. javadoc shows the opposite: the private
     * field is not documented at all and `getWidth()` is a *method*. So a property that carries
     * accessors is unfolded back into its accessor methods, and only an accessor-less property --
     * a genuine Java field -- is reported as a field.
     */
    private class SplitMembers(
        val declaredFields: List<DProperty> = emptyList(),
        val inheritedFields: List<Pair<String, DProperty>> = emptyList(),
        val declaredMethods: List<DFunction> = emptyList(),
        val inheritedMethods: List<Pair<String, DFunction>> = emptyList()
    )

    private val splitMembersCache = mutableMapOf<String, SplitMembers>()

    private fun splitMembers(type: JdType): SplitMembers = splitMembersCache.getOrPut(type.key) {
        val doc = type.documentable

        val declaredFields = mutableListOf<DProperty>()
        val inheritedFields = mutableListOf<Pair<String, DProperty>>()
        val declaredMethods = mutableListOf<DFunction>()
        val inheritedMethods = mutableListOf<Pair<String, DFunction>>()

        doc.properties.forEach { property ->
            val from = inheritedFromKey(property, type.key)
            val accessors = listOfNotNull(property.getter, property.setter)
            if (accessors.isNotEmpty()) {
                accessors.forEach { accessor ->
                    if (from == null) declaredMethods += accessor else inheritedMethods += from to accessor
                }
            } else {
                if (from == null) declaredFields += property else inheritedFields += from to property
            }
        }

        doc.functions.forEach { function ->
            val from = inheritedFromKey(function, type.key)
            if (from == null) declaredMethods += function else inheritedMethods += from to function
        }

        SplitMembers(declaredFields, inheritedFields, declaredMethods, inheritedMethods)
    }

    fun membersOf(type: JdType, scope: PageScope): ClassMembers {
        val doc = type.documentable
        val split = splitMembers(type)

        val constructors = (doc as? WithConstructors)?.constructors.orEmpty()
        val enumEntries = (doc as? DEnum)?.entries.orEmpty()

        val isAnnotation = doc is DAnnotation
        val executables = split.declaredMethods.map { executable(it, type, scope, isConstructor = false) }

        return ClassMembers(
            enumConstants = enumEntries.map { enumConstant(it, type, scope) },
            fields = split.declaredFields.map { field(it, type, scope) }.sortedBy { it.name },
            constructors = constructors.map { executable(it, type, scope, isConstructor = true) },
            methods = if (isAnnotation) emptyList() else executables.sortedBy { it.anchor },
            annotationElements = if (!isAnnotation) emptyList() else executables.sortedBy { it.name },
            inheritedFields = groupInherited(split.inheritedFields, scope) { property, owner ->
                memberRef(property.name, property.dri, owner, scope, isConstructor = false)
            },
            inheritedMethods = groupInherited(split.inheritedMethods, scope) { function, owner ->
                memberRef(function.name, function.dri, owner, scope, isConstructor = false)
            }
        )
    }

    private fun <T : Documentable> groupInherited(
        members: List<Pair<String, T>>,
        scope: PageScope,
        toRef: (T, String) -> JdMemberRef
    ): List<JdInheritedMembers> =
        members.groupBy({ it.first }, { it.second })
            .toSortedMap()
            .map { (ownerKey, owned) ->
                JdInheritedMembers(
                    declaringType = scope.typeRefForKey(ownerKey),
                    members = owned.map { toRef(it, ownerKey) }.sortedBy { it.signature }
                )
            }

    private fun memberRef(
        name: String?,
        dri: DRI,
        ownerKey: String,
        scope: PageScope,
        isConstructor: Boolean
    ): JdMemberRef {
        val anchor = index.paths.memberAnchor(dri, isConstructor)
        val owner = index.typeForKey(ownerKey)
        return JdMemberRef(
            name = name.orEmpty(),
            signature = anchor,
            url = owner?.let { scope.url(it.filePath, anchor) },
            declaringType = scope.typeRefForKey(ownerKey)
        )
    }

    private fun field(property: DProperty, owner: JdType, scope: PageScope): JdField {
        val bundle = scope.docs.bundleFor(property)
        val modifiers = modifiersOf(property)
        val typeRef = scope.typeRef(property.type)
        val anchor = index.paths.memberAnchor(property.dri, isConstructor = false)
        return JdField(
            name = property.name,
            anchor = anchor,
            modifiers = modifiers,
            type = typeRef,
            signature = (modifiers + typeRef.display + property.name).joinToString(" "),
            url = scope.url(owner.filePath, anchor),
            description = bundle.description,
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            constantValue = defaultValueOf(property),
            since = bundle.since,
            seeAlso = scope.seeRefs(bundle),
            deprecated = deprecationOf(property, bundle),
            annotations = annotationNamesOf(property),
            tags = bundle.other
        )
    }

    private fun enumConstant(entry: DEnumEntry, owner: JdType, scope: PageScope): JdField {
        val bundle = scope.docs.bundleFor(entry)
        val anchor = entry.name
        return JdField(
            name = entry.name,
            anchor = anchor,
            modifiers = listOf("public", "static", "final"),
            type = scope.typeRefForKey(owner.key, owner.classNames),
            signature = "public static final ${owner.simpleName} ${entry.name}",
            url = scope.url(owner.filePath, anchor),
            description = bundle.description,
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            since = bundle.since,
            seeAlso = scope.seeRefs(bundle),
            deprecated = deprecationOf(entry, bundle),
            annotations = annotationNamesOf(entry),
            tags = bundle.other
        )
    }

    /**
     * The nearest ancestor declaring the same erased signature, and its declaration -- the method
     * `{@inheritDoc}` inherits from. Superclasses are searched before interfaces, as javadoc does.
     */
    private fun inheritedFrom(owner: JdType, anchor: String): Pair<JdType, DFunction>? {
        val ancestors = index.superclassChain(owner.key) + index.allSuperinterfaces(owner.key)
        ancestors.forEach { key ->
            val type = index.typeForKey(key) ?: return@forEach
            val declaration = splitMembers(type).declaredMethods.firstOrNull {
                index.paths.memberAnchor(it.dri, isConstructor = false) == anchor
            }
            if (declaration != null) return type to declaration
        }
        return null
    }

    /**
     * Replaces [INHERIT_DOC_MARKER] in [text] with the corresponding text from the method this one
     * overrides, recursing when the ancestor's own comment inherits in turn. [select] picks which
     * part of the ancestor's comment to pull in, so one walk serves the description, `@return`,
     * `@param` and `@throws`.
     */
    private fun resolveInheritDoc(
        text: String?,
        owner: JdType,
        anchor: String,
        scope: PageScope,
        depth: Int = 0,
        select: (JavadocDocBundle) -> String?
    ): String? {
        if (text == null || !text.contains(INHERIT_DOC_MARKER)) return text
        if (depth >= MAX_INHERIT_DEPTH) return clean(text.replace(INHERIT_DOC_MARKER, ""))

        val parent = inheritedFrom(owner, anchor)
        val inherited = parent?.let { (parentType, declaration) ->
            // Rendered in the *current* page's scope, so links in the inherited prose resolve
            // relative to the page it is being shown on.
            resolveInheritDoc(
                select(scope.docs.bundleFor(declaration)), parentType, anchor, scope, depth + 1, select
            )
        }
        val block = inherited.orEmpty()
        // Where the marker occupies a paragraph of its own -- `{@inheritDoc}` on its own line,
        // which is how javadoc comments almost always write it -- that whole paragraph is
        // replaced by the inherited block, wrapper included. Splicing inside the existing <p>
        // would nest paragraphs whenever the inherited prose runs to more than one.
        var result = MARKER_PARAGRAPH.replace(text) { block }
        // Any marker left is inline within a sentence, so the inherited fragment's own enclosing
        // <p> comes off before it is spliced in.
        if (result.contains(INHERIT_DOC_MARKER)) {
            result = result.replace(INHERIT_DOC_MARKER, JavadocDocs.unwrapParagraph(block))
        }
        return clean(result)
    }

    /**
     * As [resolveInheritDoc], but an *absent* value is treated as an implicit `{@inheritDoc}`.
     *
     * javadoc inherits a missing `@param`/`@return`/`@throws` from the overridden method even
     * without the tag being written out, so a method that documents only some of its parameters
     * still shows text for the rest.
     */
    private fun inheritIfAbsent(
        text: String?,
        owner: JdType,
        anchor: String,
        scope: PageScope,
        select: (JavadocDocBundle) -> String?
    ): String? = resolveInheritDoc(text ?: INHERIT_DOC_MARKER, owner, anchor, scope, select = select)

    /**
     * The summary sentence for a declaration as it should read on [scope]'s page.
     *
     * Global index pages re-render summaries against their own location; passing [owner] and
     * [anchor] for a member runs the same `{@inheritDoc}` resolution there as on the member's own
     * page, instead of leaking an unresolved marker into the index.
     */
    fun summaryFor(
        doc: Documentable,
        scope: PageScope,
        owner: JdType? = null,
        anchor: String? = null
    ): String? {
        val raw = scope.docsFrom(owner?.filePath ?: "").bundleFor(doc).description
        val resolved =
            if (owner != null && anchor != null) {
                resolveInheritDoc(raw ?: INHERIT_DOC_MARKER, owner, anchor, scope) { it.description }
            } else {
                raw
            }
        return JavadocDocs.firstSentence(resolved)
    }

    /**
     * Link to a module's page, relative to [scope]'s page.
     *
     * Where the module page sits depends on whether the run uses module directories, so the link
     * is resolved from the index rather than assembled from the module name in a template.
     */
    private fun moduleUrlFor(moduleName: String?, scope: PageScope): String? {
        if (moduleName == null) return null
        return index.modules.firstOrNull { it.name == moduleName }?.let { scope.url(it.filePath) }
    }

    /**
     * javadoc's "Nested classes/interfaces declared in class X" groups.
     *
     * Unlike fields and methods, Dokka does not copy a supertype's nested types down onto the
     * subtype, so there is no `InheritedMember` to read: the groups are walked out of the
     * hierarchy directly. A nested type the subtype redeclares under the same simple name shadows
     * the inherited one and is left out, as it is in javadoc.
     */
    private fun inheritedNestedTypes(type: JdType, scope: PageScope): List<JdInheritedMembers> {
        val shadowed = type.documentable.classlikes.mapNotNull { it.name }.toMutableSet()
        val alreadyListed = mutableSetOf<String>()

        return (index.superclassChain(type.key) + index.allSuperinterfaces(type.key))
            .mapNotNull { index.typeForKey(it) }
            .mapNotNull { ancestor ->
                val nested = ancestor.documentable.classlikes
                    .mapNotNull { index.typeFor(it.dri) }
                    .filter { it.simpleName !in shadowed && alreadyListed.add(it.qualifiedName) }
                    .sortedBy { it.simpleName }
                if (nested.isEmpty()) {
                    null
                } else {
                    JdInheritedMembers(
                        declaringType = scope.typeRefForKey(ancestor.key),
                        members = nested.map { inner ->
                            JdMemberRef(
                                name = inner.classNames,
                                signature = inner.classNames,
                                url = scope.url(inner.filePath),
                                declaringType = scope.typeRefForKey(ancestor.key)
                            )
                        }
                    )
                }
            }
    }

    /**
     * Every module a consumer of [module] also gets to read: the `requires transitive` closure.
     *
     * Only `transitive` edges carry readability onward, so a plain `requires` is not followed and
     * the walk is seeded with the transitive requires alone -- seeding it with *all* direct
     * requires is what makes the result disagree with javadoc. Checked against every JDK module
     * page that has one of these tables.
     */
    private fun readableThrough(module: JdModule): Set<String> {
        val result = LinkedHashSet<String>()
        val seed = module.jpms?.requires.orEmpty().filter { it.isTransitive }.map { it.module }
        val seen = seed.toMutableSet()
        val work = ArrayDeque(seed)
        while (work.isNotEmpty()) {
            val current = work.removeFirst()
            if (current == module.name) continue
            result += current
            index.modules.firstOrNull { it.name == current }?.jpms?.requires.orEmpty()
                .filter { it.isTransitive }
                .forEach { if (seen.add(it.module)) work += it.module }
        }
        return result
    }

    /** [readableThrough] minus what this module already requires directly. */
    private fun indirectlyReadable(module: JdModule): List<String> {
        val direct = module.jpms?.requires.orEmpty().map { it.module }.toSet()
        return readableThrough(module).filterNot { it in direct || it == module.name }.sorted()
    }

    /**
     * The packages javadoc lists under "Related Packages": the parent, the direct children, and --
     * only when the result stays small -- the siblings.
     *
     * The size condition is javadoc's, not an invention: `java.nio.channels` lists its siblings
     * `java.nio.charset` and `java.nio.file`, while `java.util.concurrent` and
     * `java.lang.annotation` list none, because `java.util` and `java.lang` have too many
     * children for the table to stay useful. A cut-off of five reproduces 181 of the 190 JDK
     * package pages that have this table.
     */
    private fun relatedPackages(pkg: JdPackage): List<JdPackage> {
        val name = pkg.name
        val parentName = name.substringBeforeLast('.', "")

        fun childrenOf(prefix: String) = index.packages.filter {
            it.name != prefix &&
                it.name.startsWith("$prefix.") &&
                !it.name.removePrefix("$prefix.").contains('.')
        }

        val parent = index.packages.filter { it.name == parentName }
        val children = childrenOf(name)
        val siblings = if (parentName.isEmpty()) emptyList() else childrenOf(parentName).filter { it.name != name }

        val core = parent + children
        val related = if (core.size + siblings.size <= MAX_RELATED_PACKAGES) core + siblings else core
        return related.distinctBy { it.name }.sortedBy { it.name }
    }

    private fun clean(text: String): String? = text.trim().ifBlank { null }

    private fun executable(
        function: DFunction,
        owner: JdType,
        scope: PageScope,
        isConstructor: Boolean
    ): JdExecutable {
        val bundle = scope.docs.bundleFor(function)
        val modifiers = run {
            val found = modifierSetOf(function)
            // Dokka reports an interface's default methods simply as "not abstract" and never
            // emits the `default` keyword. In Java an interface method that is neither abstract
            // nor static is exactly a default method, so the keyword is recovered here rather
            // than being lost from the signature.
            if (owner.kind == "interface" && "abstract" !in found && "static" !in found) {
                found += "default"
            }
            orderModifiers(found)
        }
        val anchor = index.paths.memberAnchor(function.dri, isConstructor)
        val returnType = if (isConstructor) null else scope.typeRef(function.type)

        val parameters = function.parameters.map { parameter ->
            JdParameter(
                name = parameter.name.orEmpty(),
                type = scope.typeRef(parameter.type),
                description = inheritIfAbsent(
                    parameter.name?.let { bundle.params[it] }?.ifBlank { null }, owner, anchor, scope
                ) { parent -> parent.params[parameter.name.orEmpty()] },
                annotations = annotationNamesOf(parameter)
            )
        }

        val declaredThrows = scope.throwsList(bundle).map { thrown ->
            thrown.copy(
                description = inheritIfAbsent(thrown.description, owner, anchor, scope) { parent ->
                    parent.throws.firstOrNull { it.first.substringAfterLast('.') == thrown.type.display }?.third
                }
            )
        }
        val kind = when {
            isConstructor -> "constructor"
            owner.kind == "annotation" -> "annotationElement"
            else -> "method"
        }

        val (overrides, specifiedBy) =
            if (isConstructor) null to emptyList() else overrideInfo(owner, anchor, scope)

        // A method that documents only its tags -- or has no comment at all -- still shows the
        // overridden method's description in javadoc, so an absent description inherits too.
        val description = inheritIfAbsent(bundle.description, owner, anchor, scope) { it.description }

        return JdExecutable(
            name = if (isConstructor) owner.simpleName else function.name,
            anchor = anchor,
            kind = kind,
            modifiers = modifiers,
            typeParameters = typeParameters(function.generics, bundle, scope),
            returnType = returnType,
            parameters = parameters,
            exceptions = declaredThrows,
            signature = executableSignature(
                if (isConstructor) owner.simpleName else function.name,
                modifiers, function.generics, returnType, parameters, declaredThrows, scope
            ),
            url = scope.url(owner.filePath, anchor),
            description = description,
            firstSentence = JavadocDocs.firstSentence(description),
            returns = inheritIfAbsent(bundle.returns, owner, anchor, scope) { it.returns },
            specifiedBy = specifiedBy,
            overrides = overrides,
            since = bundle.since,
            seeAlso = scope.seeRefs(bundle),
            deprecated = deprecationOf(function, bundle),
            annotations = annotationNamesOf(function),
            defaultValue = defaultValueOf(function),
            tags = bundle.other.map { tag ->
                tag.copy(
                    text = resolveInheritDoc(tag.text, owner, anchor, scope) { parent ->
                        parent.other.firstOrNull { it.name == tag.name }?.text
                    }.orEmpty()
                )
            }
        )
    }

    /**
     * javadoc's "Overrides:" (nearest superclass declaring the same erased signature) and
     * "Specified by:" (every superinterface declaring it). Both are derived from the anchor,
     * which already encodes name plus erased parameter types -- exactly the identity Java uses
     * to decide whether one method overrides another.
     */
    private fun overrideInfo(
        owner: JdType,
        anchor: String,
        scope: PageScope
    ): Pair<JdMemberRef?, List<JdMemberRef>> {
        val overriddenIn = index.superclassChain(owner.key).firstOrNull { anchor in anchorsDeclaredIn(it) }
        val specifiedIn = index.allSuperinterfaces(owner.key).filter { anchor in anchorsDeclaredIn(it) }

        fun refTo(ownerKey: String): JdMemberRef? {
            val target = index.typeForKey(ownerKey) ?: return null
            return JdMemberRef(
                name = anchor.substringBefore('('),
                signature = anchor,
                url = scope.url(target.filePath, anchor),
                declaringType = scope.typeRefForKey(ownerKey)
            )
        }

        return (overriddenIn?.let { refTo(it) }) to specifiedIn.mapNotNull { refTo(it) }
    }

    private fun anchorsDeclaredIn(key: String): Set<String> = declaredMemberAnchors.getOrPut(key) {
        val type = index.typeForKey(key) ?: return@getOrPut emptySet()
        val split = splitMembers(type)
        val anchors = mutableSetOf<String>()
        split.declaredMethods.forEach { anchors += index.paths.memberAnchor(it.dri, isConstructor = false) }
        split.declaredFields.forEach { anchors += index.paths.memberAnchor(it.dri, isConstructor = false) }
        anchors
    }

    // ------------------------------------------------------------ signatures

    private fun classSignature(
        type: JdType,
        modifiers: List<String>,
        generics: List<DTypeParameter>,
        superclass: JdTypeRef?,
        superinterfaces: List<JdTypeRef>,
        scope: PageScope
    ): String {
        val keyword = when (type.kind) {
            "interface" -> "interface"
            "enum" -> "enum"
            "annotation" -> "@interface"
            else -> "class"
        }
        val typeParams = if (generics.isEmpty()) "" else
            generics.joinToString(",", "<", ">") { renderTypeParameterDeclaration(it, scope) }

        return buildString {
            append((modifiers + keyword).joinToString(" "))
            append(' ')
            append(type.classNames)
            append(typeParams)
            // An interface's parents are all spelled `extends`; a class extends one and
            // implements the rest.
            if (type.kind == "interface" || type.kind == "annotation") {
                val parents = listOfNotNull(superclass) + superinterfaces
                if (parents.isNotEmpty()) append(parents.joinToString(", ", " extends ") { it.display })
            } else {
                superclass?.let { append(" extends ${it.display}") }
                if (superinterfaces.isNotEmpty()) {
                    append(superinterfaces.joinToString(", ", " implements ") { it.display })
                }
            }
        }
    }

    private fun executableSignature(
        name: String,
        modifiers: List<String>,
        generics: List<DTypeParameter>,
        returnType: JdTypeRef?,
        parameters: List<JdParameter>,
        exceptions: List<JdThrows>,
        scope: PageScope
    ): String = buildString {
        if (modifiers.isNotEmpty()) append(modifiers.joinToString(" ")).append(' ')
        if (generics.isNotEmpty()) {
            append(generics.joinToString(",", "<", "> ") { renderTypeParameterDeclaration(it, scope) })
        }
        returnType?.let { append(it.display).append(' ') }
        append(name)
        append(parameters.joinToString(", ", "(", ")") { "${it.type.display} ${it.name}" })
        if (exceptions.isNotEmpty()) {
            append(exceptions.joinToString(", ", " throws ") { it.type.display })
        }
    }

    private fun renderTypeParameterDeclaration(generic: DTypeParameter, scope: PageScope): String {
        // `extends Object` is implicit in Java and javadoc omits it, so an Object-only bound is
        // dropped rather than printed.
        val bounds = generic.bounds.map { renderBound(it) }.filter { it != OBJECT_SIMPLE_NAME }
        val name = generic.variantTypeParameter.let { generic.name }
        return if (bounds.isEmpty()) name else "$name extends ${bounds.joinToString(" & ")}"
    }

    private fun typeParameters(
        generics: List<DTypeParameter>,
        bundle: JavadocDocBundle,
        scope: PageScope
    ): List<JdTypeParameter> = generics.map { generic ->
        JdTypeParameter(
            name = generic.name,
            bounds = generic.bounds.map { scope.typeRef(it) },
            // Dokka keeps the angle brackets a type parameter's @param was written with, so
            // `@param <U> ...` is filed under "<U>" rather than "U".
            description = (bundle.params["<${generic.name}>"] ?: bundle.params[generic.name])
                ?.ifBlank { null }
        )
    }

    // ------------------------------------------------------------- type text

    /** Renders a [Bound] the way javadoc spells a type in a signature. */
    fun renderBound(bound: Bound): String = when (bound) {
        is TypeParameter -> bound.presentableName ?: bound.name
        is Nullable -> renderBound(bound.inner)
        is DefinitelyNonNullable -> renderBound(bound.inner)
        is TypeAliased -> renderBound(bound.typeAlias)
        is PrimitiveJavaType -> bound.name
        is JavaObject -> OBJECT_SIMPLE_NAME
        is Void -> "void"
        is Dynamic -> "dynamic"
        is UnresolvedBound -> bound.name
        is GenericTypeConstructor -> renderConstructor(bound.dri, bound.projections, bound.presentableName)
        is FunctionalTypeConstructor -> renderConstructor(bound.dri, bound.projections, bound.presentableName)
    }

    private fun renderConstructor(dri: DRI, projections: List<Projection>, presentableName: String?): String {
        val key = JavadocModelIndex.keyOf(dri)
        // Dokka models a Java array as a single-argument `kotlin.Array`.
        if (key == "kotlin.Array") {
            val element = projections.firstOrNull()?.let { renderProjection(it) } ?: OBJECT_SIMPLE_NAME
            return "$element[]"
        }
        PRIMITIVE_ARRAYS[key]?.let { return it }
        val name = presentableName ?: dri.classNames ?: key.substringAfterLast('.')
        if (projections.isEmpty()) return name
        return name + projections.joinToString(",", "<", ">") { renderProjection(it) }
    }

    private fun renderProjection(projection: Projection): String = when (projection) {
        is Star -> "?"
        is Covariance<*> -> "? extends ${renderBound(projection.inner)}"
        is Contravariance<*> -> "? super ${renderBound(projection.inner)}"
        is Invariance<*> -> renderBound(projection.inner)
        is Bound -> renderBound(projection)
    }

    // ------------------------------------------------------------- modifiers

    /**
     * The Java modifier list for a declaration, in javadoc's order.
     *
     * Three Dokka sources are merged: `visibility`, `modifier` (final/abstract) and the
     * `AdditionalModifiers` extra (static, synchronized, transient, volatile, native, default...).
     * Modifiers Dokka reports that aren't Java keywords are kept at the end rather than dropped,
     * so nothing from the model is lost when the source is Kotlin.
     */
    fun modifiersOf(doc: Documentable): List<String> = orderModifiers(modifierSetOf(doc))

    private fun modifierSetOf(doc: Documentable): MutableSet<String> {
        val found = LinkedHashSet<String>()

        (doc as? WithVisibility)?.visibility?.values?.forEach { visibility ->
            visibility.name.lowercase().takeIf { it !in NON_JAVA_MODIFIERS }?.let { found += it }
        }
        (doc as? WithAbstraction)?.modifier?.values?.forEach { modifier ->
            modifier.name.lowercase().takeIf { it !in NON_JAVA_MODIFIERS }?.let { found += it }
        }
        doc.extrasOrEmpty().allOfType<AdditionalModifiers>().forEach { additional ->
            additional.content.values.flatten().forEach { found += it.name.lowercase() }
        }
        return found
    }

    /** Puts a modifier set into javadoc's print order, keeping anything unrecognized at the end. */
    private fun orderModifiers(found: Set<String>): List<String> =
        MODIFIER_ORDER.filter { it in found } + found.filterNot { it in MODIFIER_ORDER }.sorted()

    // ------------------------------------------------------------------ misc

    /**
     * A declaration's deprecation as it should read *on [scope]'s page*.
     *
     * The comment is a rendered doc fragment and can contain links, so it cannot be lifted from
     * one page onto another -- a global index page has to re-render it against its own location
     * or the links inside it point at the wrong place.
     */
    fun deprecationFor(doc: Documentable, scope: PageScope): JdDeprecation? =
        deprecationOf(doc, scope.docs.bundleFor(doc))

    private fun deprecationOf(doc: Documentable, bundle: JavadocDocBundle): JdDeprecation? {
        val annotation = deprecatedAnnotation(doc)
        if (!bundle.isDeprecatedTagPresent && annotation == null) return null
        return JdDeprecation(
            comment = bundle.deprecated,
            forRemoval = annotation?.get("forRemoval")?.contains("true") == true,
            since = annotation?.get("since")?.trim('"')?.ifBlank { null }
        )
    }

    /** The parameters of a `@Deprecated`/`@kotlin.Deprecated` annotation, if one is present. */
    private fun deprecatedAnnotation(doc: Documentable): Map<String, String>? =
        doc.extrasOrEmpty().allOfType<Annotations>()
            .flatMap { it.directAnnotations.values.flatten() }
            .firstOrNull { it.dri.classNames == "Deprecated" }
            ?.params
            ?.mapValues { it.value.toString() }

    private fun annotationNamesOf(doc: Documentable): List<String> =
        doc.extrasOrEmpty().allOfType<Annotations>()
            .flatMap { it.directAnnotations.values.flatten() }
            .mapNotNull { it.dri.classNames }
            .distinct()
            .map { "@$it" }

    /**
     * A constant field's value, or an annotation element's `default`. Dokka records both in the
     * same `DefaultValue` extra, which is read reflectively because its accessor name has moved
     * between Dokka versions (see `ModelMapper.mapExtras` for the same treatment).
     */
    private fun defaultValueOf(doc: Documentable): String? {
        val extra = doc.extrasOrEmpty().allOfType<Any>()
            .firstOrNull { it::class.java.simpleName == "DefaultValue" } ?: return null
        return try {
            val accessor = extra::class.java.methods
                .firstOrNull { it.name == "getValue" || it.name == "getExpression" } ?: return null
            when (val value = accessor.invoke(extra)) {
                null -> null
                is Map<*, *> -> value.values.firstOrNull()?.let { renderExpression(it) }
                else -> renderExpression(value)
            }
        } catch (e: Exception) {
            logger.debug("javadoc-mode: could not read DefaultValue for ${doc.dri}: ${e.message}")
            null
        }
    }

    /**
     * Renders one of Dokka's `Expression` values (`IntegerConstant`, `StringConstant`, ...) as the
     * literal javadoc would print. Their `toString` is the data-class form -- `IntegerConstant(
     * value=4)` -- so the wrapped value is unwrapped reflectively, string constants being quoted
     * the way javadoc's constant-values page quotes them.
     */
    private fun renderExpression(expression: Any): String {
        val unwrapped = try {
            expression::class.java.methods
                .firstOrNull { it.name == "getValue" && it.parameterCount == 0 }
                ?.invoke(expression)
        } catch (e: Exception) {
            logger.debug("javadoc-mode: could not unwrap expression ${expression::class.java.simpleName}: ${e.message}")
            null
        } ?: return expression.toString()

        return if (expression::class.java.simpleName == "StringConstant") "\"$unwrapped\"" else unwrapped.toString()
    }

    /**
     * The key of the type a member was inherited from, or null when [ownerKey] declares it itself.
     * Dokka attaches this as the `InheritedMember` extra when it copies members down a hierarchy.
     */
    private fun inheritedFromKey(doc: Documentable, ownerKey: String): String? {
        val inherited = doc.extrasOrEmpty().allOfType<InheritedMember>().firstOrNull() ?: return null
        val from = inherited.inheritedFrom.values.firstOrNull { it != null } ?: return null
        val fromKey = JavadocModelIndex.keyOf(from)
        if (fromKey == ownerKey || fromKey.isBlank()) return null
        // A member inherited from a type this run does not document is shown by javadoc as if the
        // subtype declared it -- there is no page to send the reader to, so an "inherited from"
        // group would be a dead end. java.util.jar.JarEntry gets its 40 CEN*/END*/LOC* constants
        // this way, from the package-private java.util.zip.ZipConstants.
        if (index.typeForKey(fromKey) == null) return null
        return fromKey
    }

    /** javadoc marks an interface with exactly one abstract method as a functional interface. */
    private fun isFunctionalInterface(type: JdType, methods: List<JdExecutable>): Boolean {
        if (type.kind != "interface") return false
        return methods.count { "abstract" in it.modifiers || ("default" !in it.modifiers && "static" !in it.modifiers) } == 1
    }

    /**
     * The DRI a type *use* should link to.
     *
     * An array links to its element type, which is what javadoc does -- `BodyPublisher[]` links to
     * `BodyPublisher`. Dokka models an array as `kotlin.Array`, a type nothing documents, so
     * without this unwrapping every array-typed parameter and return renders as dead text.
     */
    private fun boundDri(bound: Bound): DRI? = when (bound) {
        is GenericTypeConstructor ->
            if (JavadocModelIndex.keyOf(bound.dri) == "kotlin.Array") {
                bound.projections.firstOrNull()?.let { projectionBound(it) }?.let { boundDri(it) }
            } else {
                bound.dri
            }
        is FunctionalTypeConstructor -> bound.dri
        is TypeParameter -> null
        is Nullable -> boundDri(bound.inner)
        is DefinitelyNonNullable -> boundDri(bound.inner)
        is TypeAliased -> boundDri(bound.typeAlias)
        else -> null
    }

    /** The bound inside a projection, or null for a star projection. */
    private fun projectionBound(projection: Projection): Bound? = when (projection) {
        is Bound -> projection
        is Variance<*> -> projection.inner
        else -> null
    }

    private fun isConstructorCallableName(callableName: String, simpleName: String): Boolean =
        callableName == "<init>" || callableName == simpleName

    private val PRIMITIVE_ARRAYS = mapOf(
        "kotlin.IntArray" to "int[]", "kotlin.LongArray" to "long[]",
        "kotlin.ShortArray" to "short[]", "kotlin.ByteArray" to "byte[]",
        "kotlin.CharArray" to "char[]", "kotlin.BooleanArray" to "boolean[]",
        "kotlin.FloatArray" to "float[]", "kotlin.DoubleArray" to "double[]"
    )
}
