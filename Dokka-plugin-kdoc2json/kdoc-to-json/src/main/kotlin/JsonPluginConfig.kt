package org.appdevforall.dokka.kdoc2json

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import org.jetbrains.dokka.plugability.ConfigurableBlock

@Serializable
data class JsonPluginConfig(
    val logLevel: String = "debug",
    val omitFields: List<String> = emptyList(),
    val logFile: String? = null,
    val replaceHtmlExtension: Boolean = false,
    @SerialName("omit-nulls")
    val omitNulls: Boolean = false,
    val classDiscriminator: String = "kind",
    val prettyPrint: Boolean = false
) : ConfigurableBlock