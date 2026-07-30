package org.appdevforall.dokka.kdoc2json

import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.*
import org.appdevforall.dokka.kdoc2json.dtos.*
import org.jetbrains.dokka.base.DokkaBase
import org.jetbrains.dokka.model.*
import org.jetbrains.dokka.pages.PageNode
import org.jetbrains.dokka.pages.RootPageNode
import org.jetbrains.dokka.pages.WithDocumentables
import org.jetbrains.dokka.plugability.DokkaContext
import org.jetbrains.dokka.plugability.configuration
import org.jetbrains.dokka.plugability.plugin
import org.jetbrains.dokka.plugability.querySingle
import org.jetbrains.dokka.renderers.Renderer
import java.io.File
import java.util.concurrent.ConcurrentLinkedQueue

class JsonRenderer(private val context: DokkaContext) : Renderer {

    override fun render(root: RootPageNode) {
        val fqcn = "org.appdevforall.dokka.kdoc2json.JsonOutputPlugin"
        var config = configuration<JsonOutputPlugin, JsonPluginConfig>(context)

        if (config == null) {
            val rawConfig = context.configuration.pluginsConfiguration.find { it.fqPluginName == fqcn }?.values
            if (rawConfig != null) {
                context.logger.info("Dokka native config parsing failed. Manually parsing: $rawConfig")
                try {
                    // A dedicated instance because finalConfig (and the "real" json below) isn't known yet;
                    // ignoreUnknownKeys tolerates extra keys in this user-authored config rather than
                    // failing to parse it at all.
                    config = Json { ignoreUnknownKeys = true }.decodeFromString<JsonPluginConfig>(rawConfig)
                } catch (e: Exception) {
                    context.logger.error("Manual config parsing failed: ${e.message}")
                }
            } else {
                context.logger.warn("No JSON config found in pluginsConfiguration for $fqcn! Falling back to defaults.")
            }
        }

        val finalConfig = config ?: JsonPluginConfig()
        val logger = PluginLogger(context.logger, finalConfig.logLevel, finalConfig.logFile)

        val json = Json {
            prettyPrint = finalConfig.prettyPrint
            classDiscriminator = finalConfig.classDiscriminator
        }

        logger.info("Initializing JSON Renderer with config: $finalConfig")

        val locationProvider = context.plugin<DokkaBase>()
            .querySingle { locationProviderFactory }
            .getLocationProvider(root)

        val outputDir = context.configuration.outputDir
        logger.debug("Output directory set to: ${outputDir.absolutePath}")

        // --- Collect and write actual packages to package-list ---
        val packages = mutableSetOf<String>()
        fun collectPackages(node: PageNode) {
            if (node is WithDocumentables) {
                node.documentables.forEach { doc ->
                    if (doc is org.jetbrains.dokka.model.DPackage) {
                        if (!passesSourceSetWhitelist(doc.sourceSets, finalConfig.sourceSetWhitelist)) {
                            val docSourceSets = doc.sourceSets.map { it.sourceSetID.toString().substringAfterLast("/") }
                            logger.info("Omitting '${doc.name}' from package-list: sourceSets=$docSourceSets not in whitelist ${finalConfig.sourceSetWhitelist}")
                            return@forEach
                        }
                        doc.dri.packageName?.takeIf { it.isNotBlank() }?.let { packages.add(it) }
                    }
                }
            }
            node.children.forEach { collectPackages(it) }
        }
        collectPackages(root)

        val packageListFile = File(outputDir, "package-list")
        packageListFile.parentFile.mkdirs()
        
        val packageListContent = buildString {
            appendLine("\$dokka.format:json-v1\$")
            appendLine("\$dokka.linkExtension:json\$")
            packages.sorted().forEach {
                appendLine(it)
            }
        }
        
        packageListFile.writeText(packageListContent)
        logger.debug("Generated package-list with ${packages.size} packages.")

        if (context.configuration.modules.isNotEmpty()) {
            logger.info("Multimodule project detected. Generating root index.json...")
            
            val ext = if (finalConfig.replaceHtmlExtension) "json" else "html"
            val rootDto = MultimoduleRootDto(
                name = root.name,
                modules = context.configuration.modules.map { module ->
                    ModuleReferenceDto(
                        name = module.name,
                        url = "${module.relativePathToOutputDirectory.invariantSeparatorsPath}/index.$ext"
                    )
                }
            )
            
            val outputFile = File(outputDir, "index.json")
            outputFile.parentFile.mkdirs()
            
            val rawJsonElement = json.encodeToJsonElement(DocumentableDto.serializer(), rootDto)
            val filteredJsonElement = filterJson(rawJsonElement, finalConfig.omitFields, finalConfig.omitNulls)
            outputFile.writeText(json.encodeToString(JsonElement.serializer(), filteredJsonElement))
            
            logger.debug("Wrote Multimodule Root JSON to: ${outputFile.name}")
        }

        // Thread-safe list to aggregate all type documentables during traversal
        val allTypesList = ConcurrentLinkedQueue<TypeIndexEntryDto>()

        fun traverse(node: PageNode, ancestors: List<PageNode>) {
            val currentPath = ancestors + node
            
            if (node is WithDocumentables && node.documentables.isNotEmpty()) {
                val documentable = node.documentables.first()
                logger.debug("Processing documentable: ${documentable.name} (${documentable.dri})")

                if (!passesSourceSetWhitelist(documentable.sourceSets, finalConfig.sourceSetWhitelist)) {
                    val docSourceSets = documentable.sourceSets.map { it.sourceSetID.toString().substringAfterLast("/") }
                    logger.info("Omitting '${documentable.name}': sourceSets=$docSourceSets not in whitelist ${finalConfig.sourceSetWhitelist}")
                    node.children.forEach { traverse(it, currentPath) }
                    return
                }

                if (documentable is DClasslike || documentable is DTypeAlias) {
                    var typeUrl = locationProvider.resolve(node, context = null, skipExtension = false)
                    if (typeUrl != null && finalConfig.replaceHtmlExtension && !typeUrl.startsWith("http")) {
                        typeUrl = typeUrl.replace(".html", ".json")
                    }
                    
                    val kindStr = when (documentable) {
                        is DClass -> "class"
                        is DInterface -> "interface"
                        is DEnum -> "enum"
                        is DObject -> "object"
                        is DAnnotation -> "annotation"
                        is DTypeAlias -> "typeAlias"
                        else -> "type"
                    }
                    
                    allTypesList.add(
                        TypeIndexEntryDto(
                            name = documentable.name ?: "Unknown",
                            kind = kindStr,
                            dri = documentable.dri.toString(),
                            url = typeUrl,
                            sourceSets = documentable.sourceSets.map { it.sourceSetID.toString().substringAfterLast("/") }
                        )
                    )
                }

                val breadcrumbs = currentPath.map { ancestor ->
                    var url = locationProvider.resolve(ancestor, context = node, skipExtension = false)
                    if (url != null && finalConfig.replaceHtmlExtension && !url.startsWith("http")) {
                        url = url.replace(".html", ".json")
                    }
                    BreadcrumbNode(name = ancestor.name, url = url)
                }
                
                val mapper = ModelMapper(
                    locationProvider = locationProvider,
                    contextNode = node,
                    logger = logger,
                    replaceHtmlExtension = finalConfig.replaceHtmlExtension,
                    sourceSetWhitelist = finalConfig.sourceSetWhitelist
                )
                
                val dto = mapper.mapToDto(documentable, breadcrumbs)
                
                if (dto != null) {
                    try {
                        val pagePath = locationProvider.resolve(node, context = null, skipExtension = true)
                        val outputFile = File(outputDir, "$pagePath.json")
                        outputFile.parentFile.mkdirs()

                        val rawJsonElement = json.encodeToJsonElement(DocumentableDto.serializer(), dto)
                        // Deeply filters out the nulls and empties before writing
                        val filteredJsonElement = filterJson(rawJsonElement, finalConfig.omitFields, finalConfig.omitNulls)
                        outputFile.writeText(json.encodeToString(JsonElement.serializer(), filteredJsonElement))

                        logger.debug("Wrote JSON to: ${outputFile.name}")
                    } catch (e: Exception) {
                        // One unwritable/unserializable page (permissions, disk full, a
                        // serialization edge case) shouldn't abort every other page's output --
                        // matches the resilience LinkPostProcessor's own per-file passes already have.
                        logger.warn("Failed to write JSON for '${documentable.name}' (${documentable.dri}): ${e.message}")
                    }
                }
            }
            
            node.children.forEach { traverse(it, currentPath) }
        }

        traverse(root, emptyList())

        if (allTypesList.isNotEmpty()) {
            logger.info("Generating all-types.json index...")
            val allTypesDto = AllTypesDto(
                types = allTypesList.sortedBy { it.name }
            )
            val allTypesFile = File(outputDir, "all-types.json")
            allTypesFile.parentFile.mkdirs()
            
            val rawJsonElement = json.encodeToJsonElement(DocumentableDto.serializer(), allTypesDto)
            val filteredJsonElement = filterJson(rawJsonElement, finalConfig.omitFields, finalConfig.omitNulls)
            allTypesFile.writeText(json.encodeToString(JsonElement.serializer(), filteredJsonElement))
            
            logger.debug("Wrote All-Types JSON to: ${allTypesFile.name}")
        }
        
        LinkPostProcessor.postProcess(context)
        logger.info("JSON rendering completed.")
    }

    // --- RECURSIVE JSON AST FILTER ---
    private fun filterJson(element: JsonElement, omitFields: List<String>, omitNulls: Boolean): JsonElement {
        if (omitFields.isEmpty() && !omitNulls) return element
        
        return when (element) {
            is JsonObject -> {
                val filteredMap = element.entries
                    .filterNot { omitFields.contains(it.key) }
                    .mapNotNull { (key, value) ->
                        val filteredValue = filterJson(value, omitFields, omitNulls)
                        if (omitNulls && isNullOrEmpty(filteredValue)) {
                            null
                        } else {
                            key to filteredValue
                        }
                    }
                    .toMap()
                JsonObject(filteredMap)
            }
            is JsonArray -> {
                val mapped = element.map { filterJson(it, omitFields, omitNulls) }
                if (omitNulls) {
                    JsonArray(mapped.filterNot { isNullOrEmpty(it) })
                } else {
                    JsonArray(mapped)
                }
            }
            else -> element
        }
    }

    private fun isNullOrEmpty(element: JsonElement): Boolean {
        return element is JsonNull ||
               (element is JsonPrimitive && element.isString && element.content.isEmpty()) ||
               (element is JsonArray && element.isEmpty()) ||
               (element is JsonObject && element.isEmpty())
    }

    private fun passesSourceSetWhitelist(
        sourceSets: Set<org.jetbrains.dokka.DokkaConfiguration.DokkaSourceSet>,
        whitelist: List<String>
    ): Boolean {
        if (whitelist.isEmpty()) return true
        return sourceSets.any { it.sourceSetID.toString().substringAfterLast("/") in whitelist }
    }
}