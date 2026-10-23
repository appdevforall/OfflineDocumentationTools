import org.jetbrains.dokka.InternalDokkaApi
import org.jetbrains.dokka.gradle.engine.parameters.VisibilityModifier
import org.jetbrains.dokka.gradle.engine.plugins.DokkaPluginParametersBaseSpec
import javax.inject.Inject

// Generates the JDK's API documentation as javadoc-shaped JSON.
//
// Sources come from a staging tree produced by ../stage_jdk_sources.py: one directory per JPMS
// module, containing only the packages that module exports unqualified -- which is exactly the
// set the `javadoc` tool documents. Each module directory is registered as its own source root,
// so it is a valid package root and so the plugin can attribute every declaration back to its
// module from `module-info.java`.
//
// Nothing is compiled here. Dokka only needs to *analyse* the sources, and the JDK is not
// buildable as an ordinary Gradle project; the java plugin is applied solely because Dokka's
// Gradle plugin hangs its source sets off one.
plugins {
    id("org.jetbrains.dokka") version "2.2.0-Beta"
}

repositories {
    mavenLocal()
    mavenCentral()
}

dependencies {
    dokkaPlugin("org.appdevforall.dokka:kdoc-to-json:1.0.0-SNAPSHOT")
}

val stagedSources: String = (findProperty("jdkSources") as String?)
    ?: error("Set -PjdkSources=<staging dir> (see scripts/java/stage_jdk_sources.py)")

val stagedModules: List<File> = file(stagedSources)
    .listFiles { f: File -> f.isDirectory && File(f, "module-info.java").isFile }
    ?.sortedBy { it.name }
    ?: error("No JPMS module directories found under $stagedSources")

@OptIn(InternalDokkaApi::class)
abstract class JsonOutputPluginParameters @Inject constructor(
    name: String
) : DokkaPluginParametersBaseSpec(name, "org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") {
    override fun jsonEncode(): String {
        System.getenv("KDOC2JSON_TEST_CONFIG")?.let { return File(it).readText() }
        return """{
            "logLevel": "info",
            "logFile": "build/dokka_json.log",
            "javadoc-mode": true,
            "omitNulls": true
        }"""
    }
}

dokka {
    moduleName.set("jdk")

    // Dokka generates in a *worker*, not in the Gradle daemon, so `org.gradle.jvmargs` in
    // gradle.properties does not size it -- the worker inherits a default heap and, analysing the
    // whole JDK in one pass, dies with an OutOfMemoryError at around 2.5 GB. Give it a process of
    // its own with room to work.
    dokkaGeneratorIsolation.set(
        ProcessIsolation {
            maxHeapSize.set(providers.gradleProperty("dokkaWorkerHeap").orElse("24g"))
            // The JDK's deeply generic types drive Dokka's analysis into recursion far past what
            // the default ~1 MB thread stack survives -- without this it dies with a
            // StackOverflowError about a minute in.
            jvmArgs.add(providers.gradleProperty("dokkaWorkerStack").orElse("-Xss64m"))
        }
    )

    dokkaSourceSets.register("jdk") {
        sourceRoots.from(stagedModules)
        // javadoc documents public and protected members; Dokka defaults to public only.
        documentedVisibilities.set(setOf(VisibilityModifier.Public, VisibilityModifier.Protected))
        // The JDK's own sources are the whole API surface -- there is nothing to link out to.
        enableJdkDocumentationLink.set(false)
        enableKotlinStdLibDocumentationLink.set(false)
        jdkVersion.set(17)
    }

    pluginsConfiguration {
        registerBinding(JsonOutputPluginParameters::class, JsonOutputPluginParameters::class)
        register<JsonOutputPluginParameters>("org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") { }
    }
}

logger.lifecycle("jdk-api-docs: ${stagedModules.size} module source roots from $stagedSources")
