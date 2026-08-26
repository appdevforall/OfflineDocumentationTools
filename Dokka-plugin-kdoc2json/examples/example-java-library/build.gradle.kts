import org.jetbrains.dokka.gradle.engine.parameters.VisibilityModifier
import org.jetbrains.dokka.InternalDokkaApi
import org.jetbrains.dokka.gradle.engine.plugins.DokkaPluginParametersBaseSpec
import javax.inject.Inject

// A Java-only sibling of examples/example-data-processor, used by tests/test_javadoc_mode.sh.
// Javadoc mode mirrors the output of the `javadoc` tool, so it needs Java sources exercising the
// constructs a javadoc page actually has sections for: generic interfaces and their implementors,
// an abstract base class, an enum, an annotation type, a checked exception, nested types,
// compile-time constants, deprecation, and the full set of javadoc block tags.
plugins {
    java
    // Must match the dokka-core/dokka-base version kdoc-to-json was compiled against.
    id("org.jetbrains.dokka") version "2.2.0-Beta"
}

repositories {
    // Lets Gradle find the locally published kdoc-to-json plugin.
    mavenLocal()
    mavenCentral()
}

dependencies {
    dokkaPlugin("org.appdevforall.dokka:kdoc-to-json:1.0.0-SNAPSHOT")
}

@OptIn(InternalDokkaApi::class)
abstract class JsonOutputPluginParameters @Inject constructor(
    name: String
) : DokkaPluginParametersBaseSpec(name, "org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") {
    // As in example-data-processor: point KDOC2JSON_TEST_CONFIG at a JSON file to drive this
    // project through an arbitrary plugin config without editing this build script.
    override fun jsonEncode(): String {
        val overridePath = System.getenv("KDOC2JSON_TEST_CONFIG")
        if (overridePath != null) {
            return File(overridePath).readText()
        }
        return """{
            "logLevel": "debug",
            "javadoc-mode": true,
            "prettyPrint": true
        }"""
    }
}

dokka {
    dokkaSourceSets.configureEach {
        // javadoc documents public *and* protected members by default; Dokka documents only
        // public. Without this, Javadoc mode would silently omit every protected member that a
        // real javadoc build would have shown.
        documentedVisibilities.set(setOf(VisibilityModifier.Public, VisibilityModifier.Protected))
    }

    pluginsConfiguration {
        registerBinding(JsonOutputPluginParameters::class, JsonOutputPluginParameters::class)
        register<JsonOutputPluginParameters>("org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") { }
    }
}
