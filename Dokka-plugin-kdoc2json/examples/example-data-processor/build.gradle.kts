import org.jetbrains.dokka.InternalDokkaApi
import org.jetbrains.dokka.gradle.engine.plugins.DokkaPluginParametersBaseSpec
import javax.inject.Inject

plugins {
    kotlin("jvm") version "1.9.23"
    // This must match the dokka-core/dokka-base version that kdoc-to-json/build.gradle.kts
    // was compiled against, or the plugin may fail to load or behave unexpectedly.
    id("org.jetbrains.dokka") version "2.2.0-Beta"
}

repositories {
    // CRITICAL: This allows Gradle to find your locally published kdoc-to-json plugin
    mavenLocal()
    mavenCentral()
}

dependencies {
    // Inject your custom Dokka plugin into the documentation pipeline
    dokkaPlugin("org.appdevforall.dokka:kdoc-to-json:1.0.0-SNAPSHOT")
}

@OptIn(InternalDokkaApi::class)
abstract class JsonOutputPluginParameters @Inject constructor(
    name: String
) : DokkaPluginParametersBaseSpec(name, "org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") {
    override fun jsonEncode(): String = """{
        "logLevel": "debug",
        "omitFields": [],
        "replaceHtmlExtension": true,
        "omitNulls": true,
        "prettyPrint": true
    }"""
}

dokka {
    pluginsConfiguration {
        registerBinding(JsonOutputPluginParameters::class, JsonOutputPluginParameters::class)
        register<JsonOutputPluginParameters>("org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") { }
    }
}