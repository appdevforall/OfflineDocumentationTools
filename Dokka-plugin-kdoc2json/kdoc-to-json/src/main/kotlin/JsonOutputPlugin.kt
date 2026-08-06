package org.appdevforall.dokka.kdoc2json

import org.jetbrains.dokka.CoreExtensions
import org.jetbrains.dokka.base.DokkaBase
import org.jetbrains.dokka.plugability.DokkaPlugin
import org.jetbrains.dokka.plugability.DokkaPluginApiPreview
import org.jetbrains.dokka.plugability.PluginApiPreviewAcknowledgement

class JsonOutputPlugin : DokkaPlugin() {

    // FIX: plugin<T>() is a standard function in Dokka 1.9+, not a property delegate.
    // We use a getter so it is evaluated lazily when the context is available.
    private val dokkaBase get() = plugin<DokkaBase>()

    val jsonRenderer by extending {
        CoreExtensions.renderer providing ::JsonRenderer override dokkaBase.htmlRenderer
    }

    @OptIn(DokkaPluginApiPreview::class)
    override fun pluginApiPreviewAcknowledgement() = PluginApiPreviewAcknowledgement
}