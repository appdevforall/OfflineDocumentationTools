package org.appdevforall.dokka.kdoc2json.javadoc

import org.appdevforall.dokka.kdoc2json.PluginLogger
import org.jetbrains.dokka.links.DRI
import org.jetbrains.dokka.model.DAnnotation
import org.jetbrains.dokka.model.DClass
import org.jetbrains.dokka.model.DClasslike
import org.jetbrains.dokka.model.DEnum
import org.jetbrains.dokka.model.DInterface
import org.jetbrains.dokka.model.DModule
import org.jetbrains.dokka.model.DObject
import org.jetbrains.dokka.model.DPackage
import org.jetbrains.dokka.model.Documentable
import org.jetbrains.dokka.model.WithSupertypes
import org.jetbrains.dokka.pages.PageNode
import org.jetbrains.dokka.model.WithSources
import org.jetbrains.dokka.pages.WithDocumentables
import java.io.File

/** A type this documentation run covers, plus everything the renderer needs to place it. */
class JdType(
    val documentable: DClasslike,
    val key: String,
    val qualifiedName: String,
    /** Dotted name relative to the package, e.g. `Map.Entry`. */
    val classNames: String,
    val simpleName: String,
    val packageName: String,
    val moduleName: String?,
    val kind: String,
    val filePath: String
)

class JdPackage(
    val name: String,
    val moduleName: String?,
    val documentables: List<DPackage>,
    val filePath: String
)

class JdModule(
    val name: String,
    val documentables: List<DModule>,
    val filePath: String,
    /** The JPMS descriptor this module was built from, when the sources are modular. */
    val jpms: JpmsModuleInfo? = null
)

/**
 * A whole-run view of the documented model.
 *
 * A javadoc page is not derivable from its own declaration alone: "All Implemented Interfaces",
 * "Direct Known Subclasses", "All Known Implementing Classes" and the inherited-member groups are
 * all *global* facts about the type graph. So Javadoc mode does one collection pass up front,
 * builds the hierarchy in both directions, and then renders every page against this index.
 *
 * All closures are computed once in [build] rather than on demand: a per-page closure walk would
 * be quadratic in the number of types, which is the difference between usable and unusable on a
 * JDK-sized run.
 *
 * Types outside the run (e.g. `java.lang.Object` when only one package is documented) are simply
 * absent, which is also what javadoc does -- it links only what it documents.
 */
class JavadocModelIndex private constructor(
    val paths: JavadocPaths,
    val modules: List<JdModule>,
    val packages: List<JdPackage>,
    val types: List<JdType>,
    /** True when page paths carry a leading `<module>/` segment. */
    val useModuleDirs: Boolean,
    private val byKey: Map<String, JdType>,
    private val superclassByKey: Map<String, String>,
    private val interfacesByKey: Map<String, List<String>>,
    private val allSuperinterfacesByKey: Map<String, List<String>>,
    private val directSubclassesByKey: Map<String, List<String>>,
    private val subinterfacesByKey: Map<String, List<String>>,
    private val implementorsByKey: Map<String, List<String>>,
    private val exceptionKeys: Set<String>
) {

    companion object {
        const val OBJECT_FQN = "java.lang.Object"
        private const val THROWABLE_FQN = "java.lang.Throwable"

        /** Normalized cross-reference key: a type's fully qualified dotted name. */
        fun keyOf(dri: DRI): String {
            val pkg = dri.packageName.orEmpty()
            val cls = dri.classNames.orEmpty()
            return if (pkg.isBlank()) cls else "$pkg.$cls"
        }

        fun build(
            root: PageNode,
            logger: PluginLogger,
            sourceSetWhitelist: List<String>,
            sourceRoots: Collection<File> = emptyList()
        ): JavadocModelIndex {
            val collected = collectDocumentables(root)

            val moduleDocs = collected.filterIsInstance<DModule>()
            val jpmsModules = JpmsModuleScanner.scan(sourceRoots, logger)

            // Module directories when the run genuinely spans several modules -- JPMS modules read
            // off module-info.java if the sources are modular, Dokka modules otherwise. javadoc
            // likewise flattens packages to the output root for a non-modular build.
            val useModuleDirs =
                if (jpmsModules.isNotEmpty()) jpmsModules.size > 1
                else moduleDocs.distinctBy { it.name }.size > 1
            val paths = JavadocPaths(useModuleDirs)

            // A package belongs to whichever module declares it, which module-info.java states
            // outright. Qualified exports count too: the package is still *in* that module even
            // though javadoc won't document it.
            val moduleOfPackage = mutableMapOf<String, String>()
            jpmsModules.forEach { module ->
                (module.exports + module.opens).forEach { export ->
                    moduleOfPackage.putIfAbsent(export.packageName, module.name)
                }
            }
            if (jpmsModules.isEmpty()) {
                moduleDocs.forEach { module ->
                    module.packages.forEach { pkg ->
                        moduleOfPackage.putIfAbsent(pkg.dri.packageName.orEmpty(), module.name)
                    }
                }
            }

            // Fallback for a modular project that documents a package its module never exports:
            // the source file still sits under exactly one module's source root.
            val moduleRoots = jpmsModules.map { it.sourceRoot.absolutePath.trimEnd(File.separatorChar) to it.name }
            fun moduleForSourcePath(path: String?): String? {
                if (path.isNullOrBlank() || moduleRoots.isEmpty()) return null
                val normalized = File(path).absolutePath
                return moduleRoots.firstOrNull { (rootPath, _) ->
                    normalized.startsWith(rootPath + File.separatorChar)
                }?.second
            }

            fun moduleFor(packageName: String, doc: Documentable): String? =
                moduleOfPackage[packageName]
                    ?: moduleForSourcePath((doc as? WithSources)?.sources?.values?.firstOrNull()?.path)

            fun passesWhitelist(doc: Documentable): Boolean {
                if (sourceSetWhitelist.isEmpty()) return true
                return doc.sourceSets.any { it.sourceSetID.toString().substringAfterLast("/") in sourceSetWhitelist }
            }

            // --- Types ---
            val types = mutableListOf<JdType>()
            val byKey = mutableMapOf<String, JdType>()
            collected.filterIsInstance<DClasslike>().forEach { doc ->
                if (!passesWhitelist(doc)) {
                    logger.info("javadoc-mode: omitting '${doc.name}' (source sets not in whitelist $sourceSetWhitelist)")
                    return@forEach
                }
                val key = keyOf(doc.dri)
                if (byKey.containsKey(key)) return@forEach
                val packageName = doc.dri.packageName.orEmpty()
                val classNames = doc.dri.classNames ?: doc.name ?: return@forEach
                val moduleName = moduleFor(packageName, doc)
                val type = JdType(
                    documentable = doc,
                    key = key,
                    qualifiedName = key,
                    classNames = classNames,
                    simpleName = classNames.substringAfterLast('.'),
                    packageName = packageName,
                    moduleName = moduleName,
                    kind = kindOf(doc),
                    filePath = paths.classFile(packageName, classNames, moduleName)
                )
                types += type
                byKey[key] = type
            }

            // --- Direct hierarchy ---
            val superclassByKey = mutableMapOf<String, String>()
            val interfacesByKey = mutableMapOf<String, List<String>>()

            types.forEach { type ->
                val doc = type.documentable
                if (doc !is WithSupertypes) {
                    interfacesByKey[type.key] = emptyList()
                    return@forEach
                }
                val supers = doc.supertypes.values.flatten().distinctBy { keyOf(it.typeConstructor.dri) }
                val ifaces = mutableListOf<String>()
                supers.forEach { supertype ->
                    val superKey = keyOf(supertype.typeConstructor.dri)
                    if (superKey == type.key) return@forEach
                    // Prefer what the supertype actually *is* over the kind recorded at the use
                    // site; the recorded kind only has to be trusted for types we don't document.
                    val known = byKey[superKey]
                    val isInterface = when {
                        known != null -> known.kind == "interface" || known.kind == "annotation"
                        else -> supertype.kind.toString().uppercase().contains("INTERFACE")
                    }
                    if (isInterface) {
                        ifaces += superKey
                    } else {
                        // A class has at most one superclass; keep the first and ignore any
                        // duplicate a cross-source-set merge might have produced.
                        superclassByKey.putIfAbsent(type.key, superKey)
                    }
                }
                interfacesByKey[type.key] = ifaces.distinct()
            }

            val directSubclasses = mutableMapOf<String, MutableList<String>>()
            types.forEach { type ->
                superclassByKey[type.key]?.let { superKey ->
                    directSubclasses.getOrPut(superKey) { mutableListOf() } += type.key
                }
            }

            // --- Transitive interface closure, memoized across the whole graph ---
            val closureMemo = mutableMapOf<String, List<String>>()
            val inProgress = mutableSetOf<String>()

            fun closureOf(key: String): List<String> {
                closureMemo[key]?.let { return it }
                // Guards against a cycle in a malformed/merged hierarchy; without it a cyclic
                // `extends` chain would recurse until the stack blew.
                if (!inProgress.add(key)) return emptyList()
                val result = LinkedHashSet<String>()
                interfacesByKey[key].orEmpty().forEach { iface ->
                    result += iface
                    result += closureOf(iface)
                }
                superclassByKey[key]?.let { result += closureOf(it) }
                inProgress.remove(key)
                val list = result.toList()
                closureMemo[key] = list
                return list
            }

            val allSuperinterfacesByKey = types.associate { it.key to closureOf(it.key) }

            val subinterfaces = mutableMapOf<String, MutableList<String>>()
            val implementors = mutableMapOf<String, MutableList<String>>()
            types.forEach { type ->
                val bucket = if (type.kind == "interface") subinterfaces else implementors
                allSuperinterfacesByKey[type.key].orEmpty().forEach { iface ->
                    bucket.getOrPut(iface) { mutableListOf() } += type.key
                }
            }

            // --- Exception classification (javadoc tables exceptions separately) ---
            val exceptionKeys = mutableSetOf<String>()
            types.forEach { type ->
                if (isExceptionType(type, superclassByKey)) exceptionKeys += type.key
            }

            // --- Packages and modules ---
            // A package with no exports entry falls back to whichever module its own types
            // resolved to, so the two never disagree about where the package page belongs.
            val moduleOfTypePackage = types.groupBy { it.packageName }
                .mapValues { (_, inPackage) -> inPackage.firstNotNullOfOrNull { it.moduleName } }

            val packages = collected.filterIsInstance<DPackage>()
                .groupBy { it.dri.packageName.orEmpty() }
                .map { (name, docs) ->
                    val moduleName = moduleOfPackage[name] ?: moduleOfTypePackage[name]
                    JdPackage(name, moduleName, docs, paths.packageFile(name, moduleName))
                }
                .sortedBy { it.name }

            val modules = if (jpmsModules.isNotEmpty()) {
                val dokkaModuleByName = moduleDocs.groupBy { it.name }
                jpmsModules
                    .map { jpms ->
                        JdModule(
                            name = jpms.name,
                            documentables = dokkaModuleByName[jpms.name].orEmpty(),
                            filePath = paths.moduleFile(jpms.name),
                            jpms = jpms
                        )
                    }
                    .sortedBy { it.name }
            } else {
                moduleDocs.groupBy { it.name }
                    .map { (name, docs) -> JdModule(name, docs, paths.moduleFile(name)) }
                    .sortedBy { it.name }
            }

            logger.info(
                "javadoc-mode: indexed ${types.size} types, ${packages.size} packages, " +
                    "${modules.size} module(s); module directories=$useModuleDirs"
            )

            return JavadocModelIndex(
                paths = paths,
                modules = modules,
                packages = packages,
                types = types.sortedBy { it.qualifiedName },
                useModuleDirs = useModuleDirs,
                byKey = byKey,
                superclassByKey = superclassByKey,
                interfacesByKey = interfacesByKey,
                allSuperinterfacesByKey = allSuperinterfacesByKey,
                directSubclassesByKey = directSubclasses.mapValues { it.value.distinct().sorted() },
                subinterfacesByKey = subinterfaces.mapValues { it.value.distinct().sorted() },
                implementorsByKey = implementors.mapValues { it.value.distinct().sorted() },
                exceptionKeys = exceptionKeys
            )
        }

        /**
         * Whether a type belongs in javadoc's "Exception Classes" table.
         *
         * The reliable signal is a `java.lang.Throwable` ancestor, but a run that documents only
         * part of a codebase often stops short of it -- the chain ends at, say, an undocumented
         * `java.lang.RuntimeException`. Dokka's own `ExceptionInSupertypes` extra covers most of
         * that gap; the trailing name check is the last resort for the remainder, and only ever
         * looks at *ancestors*, never at the type's own name, so a class merely called
         * `ExceptionHandler` isn't miscategorised.
         */
        private fun isExceptionType(type: JdType, superclassByKey: Map<String, String>): Boolean {
            if (type.documentable.extrasOrEmpty().allOfType<Any>()
                    .any { it::class.java.simpleName == "ExceptionInSupertypes" }
            ) {
                return true
            }
            val ancestors = mutableListOf<String>()
            val seen = mutableSetOf(type.key)
            var current = superclassByKey[type.key]
            while (current != null && seen.add(current)) {
                ancestors += current
                current = superclassByKey[current]
            }
            return ancestors.any {
                it == THROWABLE_FQN || it.endsWith("Exception") || it.endsWith("Error")
            }
        }

        private fun kindOf(doc: DClasslike): String = when (doc) {
            is DInterface -> "interface"
            is DEnum -> "enum"
            is DAnnotation -> "annotation"
            is DObject -> "object"
            is DClass -> "class"
            else -> "class"
        }

        /**
         * Every documentable reachable from the page tree, deduplicated.
         *
         * Walking pages alone is not enough: a package's classlikes and a class's nested types
         * hang off the *documentable* tree, and Dokka does not always give every one of them its
         * own page, so both trees are traversed.
         */
        private fun collectDocumentables(root: PageNode): List<Documentable> {
            val seen = LinkedHashMap<String, Documentable>()

            fun visitDocumentable(doc: Documentable) {
                val id = "${doc::class.java.simpleName}|${doc.dri}"
                if (seen.putIfAbsent(id, doc) != null) return
                when (doc) {
                    is DModule -> doc.packages.forEach { visitDocumentable(it) }
                    is DPackage -> {
                        doc.classlikes.forEach { visitDocumentable(it) }
                        doc.typealiases.forEach { visitDocumentable(it) }
                    }
                    is DClasslike -> doc.classlikes.forEach { visitDocumentable(it) }
                    else -> Unit
                }
            }

            fun visitPage(node: PageNode) {
                if (node is WithDocumentables) node.documentables.forEach { visitDocumentable(it) }
                node.children.forEach { visitPage(it) }
            }

            visitPage(root)
            return seen.values.toList()
        }
    }

    fun typeFor(dri: DRI): JdType? = byKey[keyOf(dri)]

    fun typeForKey(key: String): JdType? = byKey[key]

    fun superclassOf(key: String): String? = superclassByKey[key]

    fun directInterfacesOf(key: String): List<String> = interfacesByKey[key].orEmpty()

    /**
     * The superclass chain, outermost ancestor first and [key] itself last -- the order javadoc
     * prints its inheritance tree in. Cyclic input terminates rather than looping.
     */
    fun inheritanceChain(key: String): List<String> {
        val chain = mutableListOf<String>()
        val seen = mutableSetOf<String>()
        var current: String? = key
        while (current != null && seen.add(current)) {
            chain += current
            current = superclassByKey[current]
        }
        return chain.reversed()
    }

    /** Superclass chain excluding [key] itself, nearest ancestor first. */
    fun superclassChain(key: String): List<String> = inheritanceChain(key).dropLast(1).reversed()

    /**
     * Every interface reachable from [key] through superclasses and interface extension. Backs
     * both "All Implemented Interfaces" (for a class) and "All Superinterfaces" (for an interface).
     */
    fun allSuperinterfaces(key: String): List<String> = allSuperinterfacesByKey[key].orEmpty()

    fun directKnownSubclasses(key: String): List<String> = directSubclassesByKey[key].orEmpty()

    /** Interfaces that extend [key], directly or transitively. */
    fun allKnownSubinterfaces(key: String): List<String> = subinterfacesByKey[key].orEmpty()

    /** Classes, enums and objects that implement [key], directly or transitively. */
    fun allKnownImplementingClasses(key: String): List<String> = implementorsByKey[key].orEmpty()

    /** True when [key] is a `Throwable` subtype, which javadoc tables separately. */
    fun isException(key: String): Boolean = key in exceptionKeys

    /** The enclosing type of a nested type, or null for a top-level one. */
    fun enclosingTypeOf(type: JdType): JdType? {
        if (!type.classNames.contains('.')) return null
        val outer = type.classNames.substringBeforeLast('.')
        val outerKey = if (type.packageName.isBlank()) outer else "${type.packageName}.$outer"
        return byKey[outerKey]
    }
}
