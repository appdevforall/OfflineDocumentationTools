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
    // The tests/ directory drives this example project through many different plugin configs
    // without hand-editing this build script for each run: point KDOC2JSON_TEST_CONFIG at a
    // JSON file and its contents are used verbatim as the plugin config for this build.
    override fun jsonEncode(): String {
        val overridePath = System.getenv("KDOC2JSON_TEST_CONFIG")
        if (overridePath != null) {
            return File(overridePath).readText()
        }
        return """{
            "logLevel": "debug",
            "omitFields": [],
            "replaceHtmlExtension": true,
            "omitNulls": true,
            "prettyPrint": true
        }"""
    }
}

dokka {
    pluginsConfiguration {
        registerBinding(JsonOutputPluginParameters::class, JsonOutputPluginParameters::class)
        register<JsonOutputPluginParameters>("org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") { }
    }
}