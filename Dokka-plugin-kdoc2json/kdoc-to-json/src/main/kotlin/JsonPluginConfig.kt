package org.appdevforall.dokka.kdoc2json

import com.fasterxml.jackson.annotation.JsonProperty
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import org.jetbrains.dokka.plugability.ConfigurableBlock

@Serializable
data class JsonPluginConfig(
    val logLevel: String = "debug",
    val omitFields: List<String> = emptyList(),
    val logFile: String? = null,
    val replaceHtmlExtension: Boolean = false,
    val omitNulls: Boolean = false,
    val classDiscriminator: String = "kind",
    val prettyPrint: Boolean = false,
    val sourceSetWhitelist: List<String> = emptyList(),
    // Opt-in "Javadoc mode": instead of Dokka-shaped JSON at Dokka's own page paths, emit
    // javadoc-shaped JSON laid out like the `api/` tree that the `javadoc` tool produces
    // (module-summary / package-summary / <ClassName> pages plus the global index files).
    //
    // Spelled kebab-case in the config on purpose -- that is the documented spelling of the
    // switch -- even though every other option here is camelCase. That costs two annotations
    // rather than one, because this config block is read by two different deserializers:
    // Dokka's own pluginsConfiguration parsing uses Jackson (@JsonProperty), while
    // JsonRenderer's manual fallback uses kotlinx.serialization (@SerialName). Dropping either
    // would leave "javadoc-mode" silently ignored on one of the two paths.
    @JsonProperty("javadoc-mode")
    @SerialName("javadoc-mode")
    val javadocMode: Boolean = false
) : ConfigurableBlock
