package org.appdevforall.dokka.kdoc2json

import org.appdevforall.dokka.kdoc2json.dtos.*
import org.jetbrains.dokka.base.resolvers.local.LocationProvider
import org.jetbrains.dokka.links.DRI
import org.jetbrains.dokka.links.PointingToDeclaration
import org.jetbrains.dokka.model.*
import org.jetbrains.dokka.model.doc.*
import org.jetbrains.dokka.model.properties.PropertyContainer
import org.jetbrains.dokka.pages.PageNode
import org.jetbrains.dokka.pages.ContentPage
import org.jetbrains.dokka.pages.ContentNode
import org.jetbrains.dokka.pages.ContentCodeBlock
import org.jetbrains.dokka.pages.ContentText
import org.jetbrains.dokka.pages.ContentBreakLine
import org.jetbrains.dokka.pages.ContentKind

class ModelMapper(
    private val locationProvider: LocationProvider,
    private val contextNode: PageNode,
    private val logger: PluginLogger,
    private val replaceHtmlExtension: Boolean,
    private val sourceSetWhitelist: List<String> = emptyList()
) {
    private fun resolveUrl(dri: DRI?, sourceSets: Set<DisplaySourceSet>): String? {
        if (dri == null) return null

        var url = locationProvider.resolve(dri, sourceSets, contextNode)
        if (url == null) {
            url = locationProvider.resolve(dri, emptySet(), contextNode)
        }

        if (url == null && dri.callable != null) {
            // Synthetic accessors (getX/setX), constructors, and callable/type parameters never
            // get their own PageNode -- Dokka renders them inline on their enclosing
            // declaration's page. Falling back to that declaration's own DRI (same
            // package/class, no callable, pointing at the declaration itself) turns what would
            // otherwise be a permanently dead "#" link (see LinkPostProcessor) into a real link
            // to the page that actually documents this member.
            val parentDri = dri.copy(callable = null, target = PointingToDeclaration)
            url = locationProvider.resolve(parentDri, sourceSets, contextNode)
                ?: locationProvider.resolve(parentDri, emptySet(), contextNode)
        }

        if (url == null) {
            url = "unresolved:${dri}"
        }

        if (replaceHtmlExtension && !url.startsWith("http") && !url.startsWith("unresolved:")) {
            url = url.replace(".html", ".json")
        }
        return url
    }

    // --- ADDED shallow PARAMETER HERE ---
    fun mapToDto(doc: Documentable, breadcrumbs: List<BreadcrumbNode> = emptyList(), shallow: Boolean = false): DocumentableDto? {
        logger.debug("Mapping documentable ${doc.name} of type ${doc::class.java.simpleName} (shallow=$shallow)")
        
        val displaySourceSets = doc.sourceSets.map { it.toDisplaySourceSet() }.toSet()
        val url = resolveUrl(doc.dri, displaySourceSets)

        return when (doc) {
            is DModule -> ModuleDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation),
                sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(),
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                // Pass shallow = true to children so the tree stops recursing!
                packages = if (shallow) emptyList() else filterWhitelisted(doc.packages).mapNotNull { mapToDto(it, emptyList(), true) }
            )
            is DPackage -> PackageDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                functions = if (shallow) emptyList() else filterWhitelisted(doc.functions).mapNotNull { mapToDto(it, emptyList(), true) },
                properties = if (shallow) emptyList() else filterWhitelisted(doc.properties).mapNotNull { mapToDto(it, emptyList(), true) },
                classlikes = if (shallow) emptyList() else filterWhitelisted(doc.classlikes).mapNotNull { mapToDto(it, emptyList(), true) },
                typeAliases = if (shallow) emptyList() else filterWhitelisted(doc.typealiases).mapNotNull { mapToDto(it, emptyList(), true) }
            )
            is DClass -> ClassDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                constructors = if (shallow) emptyList() else filterWhitelisted(doc.constructors).mapNotNull { mapToDto(it, emptyList(), true) },
                functions = if (shallow) emptyList() else filterWhitelisted(doc.functions).mapNotNull { mapToDto(it, emptyList(), true) },
                properties = if (shallow) emptyList() else filterWhitelisted(doc.properties).mapNotNull { mapToDto(it, emptyList(), true) },
                classlikes = if (shallow) emptyList() else filterWhitelisted(doc.classlikes).mapNotNull { mapToDto(it, emptyList(), true) },
                sources = mapSourceSetDependent(doc.sources) { _, it -> it.path },
                visibility = mapSourceSetDependent(doc.visibility) { _, it -> it.name },
                companion = if (shallow) null else doc.companion?.takeIfWhitelisted()?.let { mapToDto(it, emptyList(), true) as? ObjectDto },
                generics = doc.generics.mapNotNull { mapToDto(it) as? TypeParameterDto },
                supertypes = mapSourceSetDependent(doc.supertypes) { ss, list -> 
                    list.map { TypeConstructorWithKindDto(mapBound(it.typeConstructor, setOf(ss.toDisplaySourceSet())), it.kind.toString()) } 
                },
                modifier = mapSourceSetDependent(doc.modifier) { _, it -> it.name },
                isExpectActual = doc.isExpectActual,
                typealiases = emptyList() 
            )
            is DEnum -> EnumDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                entries = if (shallow) emptyList() else filterWhitelisted(doc.entries).mapNotNull { mapToDto(it, emptyList(), true) },
                constructors = if (shallow) emptyList() else filterWhitelisted(doc.constructors).mapNotNull { mapToDto(it, emptyList(), true) },
                functions = if (shallow) emptyList() else filterWhitelisted(doc.functions).mapNotNull { mapToDto(it, emptyList(), true) },
                properties = if (shallow) emptyList() else filterWhitelisted(doc.properties).mapNotNull { mapToDto(it, emptyList(), true) },
                classlikes = if (shallow) emptyList() else filterWhitelisted(doc.classlikes).mapNotNull { mapToDto(it, emptyList(), true) },
                sources = mapSourceSetDependent(doc.sources) { _, it -> it.path },
                visibility = mapSourceSetDependent(doc.visibility) { _, it -> it.name },
                companion = if (shallow) null else doc.companion?.takeIfWhitelisted()?.let { mapToDto(it, emptyList(), true) as? ObjectDto },
                supertypes = mapSourceSetDependent(doc.supertypes) { ss, list -> 
                    list.map { TypeConstructorWithKindDto(mapBound(it.typeConstructor, setOf(ss.toDisplaySourceSet())), it.kind.toString()) } 
                },
                isExpectActual = doc.isExpectActual,
                typealiases = emptyList() 
            )
            is DEnumEntry -> EnumEntryDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                functions = if (shallow) emptyList() else filterWhitelisted(doc.functions).mapNotNull { mapToDto(it, emptyList(), true) },
                properties = if (shallow) emptyList() else filterWhitelisted(doc.properties).mapNotNull { mapToDto(it, emptyList(), true) },
                classlikes = if (shallow) emptyList() else filterWhitelisted(doc.classlikes).mapNotNull { mapToDto(it, emptyList(), true) }
            )
            is DFunction -> FunctionDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                isConstructor = doc.isConstructor,
                parameters = doc.parameters.mapNotNull { mapToDto(it) as? ParameterDto },
                sources = mapSourceSetDependent(doc.sources) { _, it -> it.path },
                visibility = mapSourceSetDependent(doc.visibility) { _, it -> it.name },
                type = mapBound(doc.type, displaySourceSets),
                generics = doc.generics.mapNotNull { mapToDto(it) as? TypeParameterDto },
                receiver = doc.receiver?.let { mapToDto(it) as? ParameterDto },
                modifier = mapSourceSetDependent(doc.modifier) { _, it -> it.name },
                isExpectActual = doc.isExpectActual,
                contextParameters = emptyList() 
            )
            is DInterface -> InterfaceDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                functions = if (shallow) emptyList() else filterWhitelisted(doc.functions).mapNotNull { mapToDto(it, emptyList(), true) },
                properties = if (shallow) emptyList() else filterWhitelisted(doc.properties).mapNotNull { mapToDto(it, emptyList(), true) },
                classlikes = if (shallow) emptyList() else filterWhitelisted(doc.classlikes).mapNotNull { mapToDto(it, emptyList(), true) },
                sources = mapSourceSetDependent(doc.sources) { _, it -> it.path },
                visibility = mapSourceSetDependent(doc.visibility) { _, it -> it.name },
                companion = if (shallow) null else doc.companion?.takeIfWhitelisted()?.let { mapToDto(it, emptyList(), true) as? ObjectDto },
                generics = doc.generics.mapNotNull { mapToDto(it) as? TypeParameterDto },
                supertypes = mapSourceSetDependent(doc.supertypes) { ss, list -> 
                    list.map { TypeConstructorWithKindDto(mapBound(it.typeConstructor, setOf(ss.toDisplaySourceSet())), it.kind.toString()) } 
                },
                modifier = mapSourceSetDependent(doc.modifier) { _, it -> it.name },
                isExpectActual = doc.isExpectActual,
                typealiases = emptyList() 
            )
            is DObject -> ObjectDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                functions = if (shallow) emptyList() else filterWhitelisted(doc.functions).mapNotNull { mapToDto(it, emptyList(), true) },
                properties = if (shallow) emptyList() else filterWhitelisted(doc.properties).mapNotNull { mapToDto(it, emptyList(), true) },
                classlikes = if (shallow) emptyList() else filterWhitelisted(doc.classlikes).mapNotNull { mapToDto(it, emptyList(), true) },
                sources = mapSourceSetDependent(doc.sources) { _, it -> it.path },
                visibility = mapSourceSetDependent(doc.visibility) { _, it -> it.name },
                supertypes = mapSourceSetDependent(doc.supertypes) { ss, list -> 
                    list.map { TypeConstructorWithKindDto(mapBound(it.typeConstructor, setOf(ss.toDisplaySourceSet())), it.kind.toString()) } 
                },
                isExpectActual = doc.isExpectActual,
                typealiases = emptyList() 
            )
            is DAnnotation -> AnnotationDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                functions = if (shallow) emptyList() else filterWhitelisted(doc.functions).mapNotNull { mapToDto(it, emptyList(), true) },
                properties = if (shallow) emptyList() else filterWhitelisted(doc.properties).mapNotNull { mapToDto(it, emptyList(), true) },
                classlikes = if (shallow) emptyList() else filterWhitelisted(doc.classlikes).mapNotNull { mapToDto(it, emptyList(), true) },
                sources = mapSourceSetDependent(doc.sources) { _, it -> it.path },
                visibility = mapSourceSetDependent(doc.visibility) { _, it -> it.name },
                companion = if (shallow) null else doc.companion?.takeIfWhitelisted()?.let { mapToDto(it, emptyList(), true) as? ObjectDto },
                constructors = if (shallow) emptyList() else filterWhitelisted(doc.constructors).mapNotNull { mapToDto(it, emptyList(), true) },
                generics = doc.generics.mapNotNull { mapToDto(it) as? TypeParameterDto },
                isExpectActual = doc.isExpectActual
            )
            is DProperty -> PropertyDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                sources = mapSourceSetDependent(doc.sources) { _, it -> it.path },
                visibility = mapSourceSetDependent(doc.visibility) { _, it -> it.name },
                type = mapBound(doc.type, displaySourceSets),
                receiver = doc.receiver?.let { mapToDto(it) as? ParameterDto },
                setter = doc.setter?.let { mapToDto(it) as? FunctionDto },
                getter = doc.getter?.let { mapToDto(it) as? FunctionDto },
                modifier = mapSourceSetDependent(doc.modifier) { _, it -> it.name },
                generics = doc.generics.mapNotNull { mapToDto(it) as? TypeParameterDto },
                isExpectActual = doc.isExpectActual,
                contextParameters = emptyList() 
            )
            is DParameter -> ParameterDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                type = mapBound(doc.type, displaySourceSets)
            )
            is DTypeParameter -> TypeParameterDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                bounds = doc.bounds.map { mapBound(it, displaySourceSets) },
                variantTypeParameter = mapVariance(doc.variantTypeParameter, displaySourceSets)
            )
            is DTypeAlias -> TypeAliasDto(
                dri = doc.dri.toString(), name = doc.name, url = url,
                documentation = mapDocNodes(doc.documentation), sourceSets = mapSourceSets(doc.sourceSets),
                expectPresentInSet = doc.expectPresentInSet?.sourceSetID?.toString(), 
                extras = mapExtras(doc.extra),
                breadcrumbs = breadcrumbs,
                type = mapBound(doc.type, displaySourceSets),
                underlyingType = mapSourceSetDependent(doc.underlyingType) { ss, it -> mapBound(it, setOf(ss.toDisplaySourceSet())) },
                visibility = mapSourceSetDependent(doc.visibility) { _, it -> it.name },
                generics = doc.generics.mapNotNull { mapToDto(it) as? TypeParameterDto },
                sources = mapSourceSetDependent(doc.sources) { _, it -> it.path }
            )
            else -> null
        }
    }

    // VERSION-COUPLED: isObviousMember/isException below detect Dokka-internal extras by
    // comparing ::class.java.simpleName against string literals, because ObviousMember and
    // ExceptionInSupertypes aren't public API to check against directly. Bumping the Dokka
    // version pinned in kdoc-to-json/build.gradle.kts (2.2.0-Beta) could rename, move, or remove
    // either class with no compile error -- the simpleName check would just always return false.
    // DokkaVersionCheck.runOnce below verifies the exact FQCNs this file depends on are still
    // loadable, so that kind of break is a loud warning instead of a silent one.
    private fun mapExtras(extra: PropertyContainer<*>): ExtrasDto {
        DokkaVersionCheck.runOnce(logger)
        val isObviousMember = extra.allOfType<Any>().any { it::class.java.simpleName == "ObviousMember" }
        val isException = extra.allOfType<Any>().any { it::class.java.simpleName == "ExceptionInSupertypes" }

        val annotationsMap = extra.allOfType<org.jetbrains.dokka.model.Annotations>().firstOrNull()?.directAnnotations?.entries?.associate { (ss, list) ->
            ss.sourceSetID.toString() to list.map { anno ->
                AnnotationWrapperDto(
                    dri = anno.dri.toString(),
                    params = anno.params.entries.associate { (k, v) -> k to v.toString() },
                    url = resolveUrl(anno.dri, setOf(ss.toDisplaySourceSet()))
                )
            }
        } ?: emptyMap()
        
        val defaultValuesMap = mutableMapOf<String, String>()
        extra.allOfType<Any>().firstOrNull { it::class.java.simpleName == "DefaultValue" }?.let { defValue ->
            try {
                val valueMethod = defValue::class.java.methods.firstOrNull { it.name == "getValue" || it.name == "getExpression" }
                val valueObj = valueMethod?.invoke(defValue)
                if (valueObj is Map<*, *>) {
                    valueObj.forEach { (ss, expr) ->
                        val ssName = ss?.let { it::class.java.getMethod("getSourceSetID").invoke(it).toString() } ?: "unknown"
                        defaultValuesMap[ssName] = expr.toString()
                    }
                } else if (valueObj != null) {
                    defaultValuesMap["unknown"] = valueObj.toString()
                }
            } catch (e: Exception) {
                logger.debug("Failed to safely extract DefaultValue: ${e.message}")
            }
        }

        val additionalModifiersMap = extra.allOfType<org.jetbrains.dokka.model.AdditionalModifiers>().firstOrNull()?.content?.entries?.associate { (ss, set) ->
            ss.sourceSetID.toString() to set.map { modifier ->
                try {
                    modifier::class.java.getMethod("getName").invoke(modifier).toString().lowercase()
                } catch (e: Exception) {
                    modifier.toString().substringAfterLast("$").substringBefore("@").lowercase()
                }
            }
        } ?: emptyMap()

        return ExtrasDto(
            annotations = annotationsMap,
            defaultValues = defaultValuesMap,
            additionalModifiers = additionalModifiersMap,
            isObviousMember = isObviousMember,
            isException = isException
        )
    }

    private fun mapProjection(proj: Projection, sourceSets: Set<DisplaySourceSet>): ProjectionDto {
        return when (proj) {
            is Star -> StarDto
            is Variance<*> -> when (proj) {
                is Covariance<*> -> CovarianceDto(mapBound(proj.inner, sourceSets))
                is Contravariance<*> -> ContravarianceDto(mapBound(proj.inner, sourceSets))
                is Invariance<*> -> InvarianceDto(mapBound(proj.inner, sourceSets))
            }
            is Bound -> mapBound(proj, sourceSets)
        }
    }

    // A DTypeParameter's own variantTypeParameter is a Projection (Star | Variance | Bound) even
    // though in practice Dokka only ever hands us a Variance here -- Star/bare-Bound would only
    // show up for a use-site projection, not a type parameter's own declared variance. Rather
    // than a hard `as VarianceDto` cast that throws ClassCastException if that assumption is ever
    // wrong, degrade to Invariance around whatever bound (if any) we can salvage.
    private fun mapVariance(proj: Projection, sourceSets: Set<DisplaySourceSet>): VarianceDto {
        val mapped = mapProjection(proj, sourceSets)
        return mapped as? VarianceDto ?: InvarianceDto(mapped as? BoundDto ?: UnresolvedBoundDto("*"))
    }

    private fun mapBound(bound: Bound, sourceSets: Set<DisplaySourceSet>): BoundDto {
        return when (bound) {
            is TypeParameter -> TypeParameterBoundDto(bound.dri.toString(), bound.name, bound.presentableName, resolveUrl(bound.dri, sourceSets))
            is Nullable -> NullableDto(mapBound(bound.inner, sourceSets), resolveUrl(null, sourceSets))
            is DefinitelyNonNullable -> DefinitelyNonNullableDto(mapBound(bound.inner, sourceSets))
            is TypeAliased -> TypeAliasedDto(mapBound(bound.typeAlias, sourceSets), mapBound(bound.inner, sourceSets), resolveUrl(null, sourceSets))
            is PrimitiveJavaType -> PrimitiveJavaTypeDto(bound.name)
            is JavaObject -> JavaObjectDto()
            is Void -> VoidDto()
            is Dynamic -> DynamicDto()
            is UnresolvedBound -> UnresolvedBoundDto(bound.name)
            is GenericTypeConstructor -> GenericTypeConstructorDto(
                dri = bound.dri.toString(), 
                projections = bound.projections.map { mapProjection(it, sourceSets) }, 
                presentableName = bound.presentableName,
                url = resolveUrl(bound.dri, sourceSets)
            )
            is FunctionalTypeConstructor -> FunctionalTypeConstructorDto(
                dri = bound.dri.toString(), 
                projections = bound.projections.map { mapProjection(it, sourceSets) }, 
                isExtensionFunction = bound.isExtensionFunction, 
                isSuspendable = bound.isSuspendable, 
                presentableName = bound.presentableName,
                url = resolveUrl(bound.dri, sourceSets)
            )
        }
    }

    private fun mapDocNodes(docs: SourceSetDependent<DocumentationNode>): Map<String, List<TagWrapperDto>> {
        return docs.entries.associate { (sourceSet, node) ->
            val displaySourceSet = sourceSet.toDisplaySourceSet()
            val displaySourceSets = setOf(displaySourceSet)
            
            val pageSamples = mutableListOf<String>()
            if (contextNode is ContentPage) {
                fun walk(n: ContentNode) {
                    if (n is ContentCodeBlock &&
                        n.dci.kind == ContentKind.Sample &&
                        n.sourceSets.contains(displaySourceSet)
                    ) {
                        fun extractContentText(cn: ContentNode): String {
                            if (cn is ContentText) return cn.text
                            if (cn is ContentBreakLine) return "\n"
                            return cn.children.joinToString("") { extractContentText(it) }
                        }
                        pageSamples.add(extractContentText(n))
                    }
                    n.children.forEach { walk(it) }
                }
                walk(contextNode.content)
            }
            
            var sampleIndex = 0
            val tags = node.children.map { tagWrapper ->
                val type = tagWrapper::class.java.simpleName
                var text = extractText(tagWrapper.root, displaySourceSets).trim()
                
                if (type == "Sample" && text.isEmpty()) {
                    if (sampleIndex < pageSamples.size) {
                        text = pageSamples[sampleIndex]
                        sampleIndex++
                    }
                }
                
                TagWrapperDto(
                    type = type,
                    text = text,
                    name = if (tagWrapper is NamedTagWrapper) tagWrapper.name else null
                )
            }
            sourceSet.sourceSetID.toString() to tags
        }
    }

    // Escapes an HTML attribute value (e.g. the href of an <a> tag built from KDoc content).
    // "&" must be escaped first, or escaping "\"" afterwards would double-escape any "&quot;"
    // that was already literally present in the source text.
    private fun escapeHtmlAttribute(value: String): String {
        return value.replace("&", "&amp;").replace("\"", "&quot;")
    }

    private fun extractText(tag: DocTag, sourceSets: Set<DisplaySourceSet>): String {
        return when (tag) {
            is Text -> tag.body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            
            is P -> "<p>${tag.children.joinToString("") { extractText(it, sourceSets) }}</p>"
            is Br -> "<br/>"
            is BlockQuote -> "<blockquote>${tag.children.joinToString("") { extractText(it, sourceSets) }}</blockquote>"
            
            is B -> "<b>${tag.children.joinToString("") { extractText(it, sourceSets) }}</b>"
            is Strong -> "<strong>${tag.children.joinToString("") { extractText(it, sourceSets) }}</strong>"
            is I -> "<i>${tag.children.joinToString("") { extractText(it, sourceSets) }}</i>"
            is Em -> "<em>${tag.children.joinToString("") { extractText(it, sourceSets) }}</em>"
            
            is CodeInline -> "<code>${tag.children.joinToString("") { extractText(it, sourceSets) }}</code>"
            is CodeBlock -> "<pre><code>${tag.children.joinToString("") { extractText(it, sourceSets) }}</code></pre>"
            
            is Ul -> "<ul>${tag.children.joinToString("") { extractText(it, sourceSets) }}</ul>"
            is Ol -> "<ol>${tag.children.joinToString("") { extractText(it, sourceSets) }}</ol>"
            is Li -> "<li>${tag.children.joinToString("") { extractText(it, sourceSets) }}</li>"
            
            is H1 -> "<h1>${tag.children.joinToString("") { extractText(it, sourceSets) }}</h1>"
            is H2 -> "<h2>${tag.children.joinToString("") { extractText(it, sourceSets) }}</h2>"
            is H3 -> "<h3>${tag.children.joinToString("") { extractText(it, sourceSets) }}</h3>"
            is H4 -> "<h4>${tag.children.joinToString("") { extractText(it, sourceSets) }}</h4>"
            is H5 -> "<h5>${tag.children.joinToString("") { extractText(it, sourceSets) }}</h5>"
            is H6 -> "<h6>${tag.children.joinToString("") { extractText(it, sourceSets) }}</h6>"
            
            is A -> {
                val href = escapeHtmlAttribute(tag.params["href"] ?: "")
                "<a href=\"$href\">${tag.children.joinToString("") { extractText(it, sourceSets) }}</a>"
            }
            is DocumentationLink -> {
                val href = escapeHtmlAttribute(resolveUrl(tag.dri, sourceSets) ?: "")
                "<a href=\"$href\">${tag.children.joinToString("") { extractText(it, sourceSets) }}</a>"
            }
            
            is CustomDocTag -> tag.children.joinToString("") { extractText(it, sourceSets) }
            else -> tag.children.joinToString("") { extractText(it, sourceSets) }
        }
    }

    private fun abbreviateSourceSet(id: String): String {
        return id.substringAfterLast("/")
    }

    private fun <T, R> mapSourceSetDependent(
        dependent: SourceSetDependent<T>, 
        mapper: (org.jetbrains.dokka.DokkaConfiguration.DokkaSourceSet, T) -> R
    ): Map<String, R> {
        return dependent.entries.associate { 
            abbreviateSourceSet(it.key.sourceSetID.toString()) to mapper(it.key, it.value) 
        }
    }

    private fun mapSourceSets(sets: Set<org.jetbrains.dokka.DokkaConfiguration.DokkaSourceSet>): List<String> {
        return sets.map { abbreviateSourceSet(it.sourceSetID.toString()) }
    }

    private fun passesWhitelist(doc: Documentable): Boolean {
        if (sourceSetWhitelist.isEmpty()) return true
        return doc.sourceSets.any { abbreviateSourceSet(it.sourceSetID.toString()) in sourceSetWhitelist }
    }

    // Drops a shallow child reference (e.g. a package/classlike/function listed in a parent's
    // own index) whose source sets don't overlap the whitelist, so index pages don't dangle
    // links to files that JsonRenderer omits entirely.
    private fun <T : Documentable> T.takeIfWhitelisted(): T? {
        if (passesWhitelist(this)) return this
        logger.info("Omitting reference to '${name}' from index: sourceSets=${mapSourceSets(sourceSets)} not in whitelist $sourceSetWhitelist")
        return null
    }

    private fun <T : Documentable> filterWhitelisted(docs: List<T>): List<T> = docs.mapNotNull { it.takeIfWhitelisted() }
}

// One-time-per-process sanity check for mapExtras' reflective, simpleName-based detection of
// Dokka-internal extras (see the VERSION-COUPLED comment on mapExtras). Verifies the exact FQCNs
// that detection depends on are still loadable against whatever Dokka version is actually on the
// classpath, so a future Dokka bump that renames/moves/removes one of these fails loudly instead
// of silently degrading to "never detected."
private object DokkaVersionCheck {
    @Volatile
    private var checked = false

    private val REFLECTED_DOKKA_CLASSES = listOf(
        "org.jetbrains.dokka.model.ObviousMember",
        "org.jetbrains.dokka.model.ExceptionInSupertypes",
        "org.jetbrains.dokka.model.DefaultValue",
        "org.jetbrains.dokka.model.AdditionalModifiers"
    )

    @Synchronized
    fun runOnce(logger: PluginLogger) {
        if (checked) return
        checked = true
        REFLECTED_DOKKA_CLASSES.forEach { fqcn ->
            try {
                Class.forName(fqcn)
            } catch (e: ClassNotFoundException) {
                logger.warn(
                    "Dokka-internal class '$fqcn' not found on the classpath. " +
                        "mapExtras' reflective detection of ObviousMember/ExceptionInSupertypes/" +
                        "DefaultValue/AdditionalModifiers may be silently broken for this Dokka version."
                )
            }
        }
    }
}