package org.appdevforall.dokka.kdoc2json

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * The `omitFields` / `omitNulls` post-processing both renderers apply to a page before writing it.
 *
 * Lives outside [JsonRenderer] so Javadoc mode honours exactly the same two config options with
 * exactly the same semantics, rather than reimplementing them and drifting.
 */
internal object JsonFilters {

    /** Strips [omitFields] keys everywhere, and (when [omitNulls]) null/empty values with them. */
    fun filterJson(element: JsonElement, omitFields: List<String>, omitNulls: Boolean): JsonElement {
        if (omitFields.isEmpty() && !omitNulls) return element

        return when (element) {
            is JsonObject -> {
                val filteredMap = element.entries
                    .filterNot { omitFields.contains(it.key) }
                    .mapNotNull { (key, value) ->
                        val filteredValue = filterJson(value, omitFields, omitNulls)
                        if (omitNulls && isNullOrEmpty(filteredValue)) null else key to filteredValue
                    }
                    .toMap()
                JsonObject(filteredMap)
            }
            is JsonArray -> {
                val mapped = element.map { filterJson(it, omitFields, omitNulls) }
                if (omitNulls) JsonArray(mapped.filterNot { isNullOrEmpty(it) }) else JsonArray(mapped)
            }
            else -> element
        }
    }

    /** `omitNulls` drops empty values too, not just nulls -- see the note in the plugin README. */
    fun isNullOrEmpty(element: JsonElement): Boolean =
        element is JsonNull ||
            (element is JsonPrimitive && element.isString && element.content.isEmpty()) ||
            (element is JsonArray && element.isEmpty()) ||
            (element is JsonObject && element.isEmpty())
}
