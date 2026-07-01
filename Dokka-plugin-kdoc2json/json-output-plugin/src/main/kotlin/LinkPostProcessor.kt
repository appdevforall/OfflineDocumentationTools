package my.dokka.plugin

import kotlinx.serialization.json.*
import org.jetbrains.dokka.plugability.DokkaContext

object LinkPostProcessor {
    fun postProcess(context: DokkaContext) {
        val dir = context.configuration.outputDir
        context.logger.info("Running Universal Cross-Module Link Resolution in ${dir.absolutePath}...")
        
        val jsonFiles = dir.walkTopDown().filter { it.extension == "json" }.toList()
        val driIndex = mutableMapOf<String, String>()
        
        // --- PASS 1: Build Global Index ---
        for (file in jsonFiles) {
            try {
                val text = file.readText()
                val rootElement = Json.parseToJsonElement(text)
                val relativePath = file.parentFile.toRelativeString(dir).replace("\\", "/")
                extractDris(rootElement, relativePath, driIndex)
            } catch (e: Exception) {
                context.logger.warn("Failed to index ${file.name}: ${e.message}")
            }
        }
        
        context.logger.info("Indexed ${driIndex.size} DRIs across ${jsonFiles.size} JSON files.")
        
        // --- PASS 2: Resolve Links ---
        val unresolvedRegex = """unresolved:([^\s")\\]+)""".toRegex()
        var replacedCount = 0
        
        for (file in jsonFiles) {
            val text = file.readText()
            if (text.contains("unresolved:")) {
                val relativeParent = file.parentFile.toRelativeString(dir).replace("\\", "/")
                val depth = if (relativeParent.isEmpty()) 0 else relativeParent.split("/").filter { it.isNotEmpty() }.size
                val rootPrefix = if (depth == 0) "./" else "../".repeat(depth)
                
                val replaced = unresolvedRegex.replace(text) { matchResult ->
                    val dri = matchResult.groupValues[1]
                    val resolved = driIndex[dri]
                    
                    if (resolved != null) {
                        replacedCount++
                        "$rootPrefix$resolved"
                    } else {
                        "#"
                    }
                }
                file.writeText(replaced)
            }
        }
        context.logger.info("Successfully resolved $replacedCount cross-module links!")
    }

    private fun extractDris(element: JsonElement, relativePath: String, index: MutableMap<String, String>) {
        if (element is JsonObject) {
            val dri = (element["dri"] as? JsonPrimitive)?.contentOrNull
            val url = (element["url"] as? JsonPrimitive)?.contentOrNull
            
            if (dri != null && url != null && !url.startsWith("unresolved:") && !url.startsWith("http") && !url.startsWith("#")) {
                val cleanParent = relativePath.trim('/')
                val fullUrl = if (cleanParent.isEmpty()) url else "$cleanParent/$url"
                index[dri] = fullUrl
            }
            
            element.values.forEach { extractDris(it, relativePath, index) }
        } else if (element is JsonArray) {
            element.forEach { extractDris(it, relativePath, index) }
        }
    }
}