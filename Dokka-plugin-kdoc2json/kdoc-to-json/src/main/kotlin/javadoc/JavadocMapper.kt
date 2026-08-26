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
    }

    // Anchors of the members each type declares itself, used to derive Overrides/Specified by.
    // Keyed by type key; built on demand because most runs only touch part of the graph.
    private val declaredMemberAnchors = mutableMapOf<String, Set<String>>()

    /** A single output file, and everything that has to be resolved relative to it. */
    inner class PageScope(private val fromFile: String) {

        val docs = JavadocDocs { dri -> linkFor(dri) }

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
            inheritedNestedTypes = emptyList()
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
            url = pkg.filePath,
            description = bundle.description,
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            since = bundle.since,
            seeAlso = scope.seeRefs(bundle),
            deprecated = pkg.documentables.firstNotNullOfOrNull { deprecationOf(it, bundle) },
            tags = bundle.other,
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

        return JdModulePage(
            name = module.name,
            url = module.filePath,
            description = bundle.description,
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            since = bundle.since,
            seeAlso = scope.seeRefs(bundle),
            deprecated = module.documentables.firstNotNullOfOrNull { deprecationOf(it, bundle) },
            tags = bundle.other,
            packages = packagesInModule.map { packageSummary(it, scope) }
        )
    }

    // ------------------------------------------------------------- summaries

    fun typeSummary(type: JdType, scope: PageScope): JdTypeSummary {
        val bundle = scope.docs.bundleFor(type.documentable)
        return JdTypeSummary(
            name = type.classNames,
            qualifiedName = type.qualifiedName,
            kind = if (index.isException(type.key)) "exception" else type.kind,
            packageName = type.packageName,
            moduleName = type.moduleName,
            url = scope.url(type.filePath),
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            deprecated = deprecationOf(type.documentable, bundle)
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
        return JdModuleSummary(
            name = module.name,
            url = scope.url(module.filePath),
            firstSentence = JavadocDocs.firstSentence(bundle.description)
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
                description = parameter.name?.let { bundle.params[it] }?.ifBlank { null },
                annotations = annotationNamesOf(parameter)
            )
        }

        val declaredThrows = scope.throwsList(bundle)
        val kind = when {
            isConstructor -> "constructor"
            owner.kind == "annotation" -> "annotationElement"
            else -> "method"
        }

        val (overrides, specifiedBy) =
            if (isConstructor) null to emptyList() else overrideInfo(owner, anchor, scope)

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
            description = bundle.description,
            firstSentence = JavadocDocs.firstSentence(bundle.description),
            returns = bundle.returns,
            specifiedBy = specifiedBy,
            overrides = overrides,
            since = bundle.since,
            seeAlso = scope.seeRefs(bundle),
            deprecated = deprecationOf(function, bundle),
            annotations = annotationNamesOf(function),
            defaultValue = defaultValueOf(function),
            tags = bundle.other
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
        return if (fromKey == ownerKey || fromKey.isBlank()) null else fromKey
    }

    /** javadoc marks an interface with exactly one abstract method as a functional interface. */
    private fun isFunctionalInterface(type: JdType, methods: List<JdExecutable>): Boolean {
        if (type.kind != "interface") return false
        return methods.count { "abstract" in it.modifiers || ("default" !in it.modifiers && "static" !in it.modifiers) } == 1
    }

    private fun boundDri(bound: Bound): DRI? = when (bound) {
        is GenericTypeConstructor -> bound.dri
        is FunctionalTypeConstructor -> bound.dri
        is TypeParameter -> null
        is Nullable -> boundDri(bound.inner)
        is DefinitelyNonNullable -> boundDri(bound.inner)
        is TypeAliased -> boundDri(bound.typeAlias)
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
