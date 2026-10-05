// Standalone Dokka project used to generate JSON API docs for the OpenJDK 17 `java.base`
// module (https://github.com/openjdk/jdk17, src/java.base) via the kdoc-to-json plugin
// (../../ADFA/DocsPipeline/OfflineDocumentationTools/Dokka-plugin-kdoc2json). No Kotlin/Java
// compilation happens here - Dokka only reads the source tree - so no `java`/`kotlin` plugin
// is applied, just `org.jetbrains.dokka` with a manually-declared source set.
import org.jetbrains.dokka.InternalDokkaApi
import org.jetbrains.dokka.gradle.engine.plugins.DokkaPluginParametersBaseSpec
import javax.inject.Inject

plugins {
    // Must match the dokka-core/dokka-base version kdoc-to-json/build.gradle.kts was
    // compiled against, or the plugin may fail to load or behave unexpectedly.
    id("org.jetbrains.dokka") version "2.2.0"
}

repositories {
    mavenLocal()
    mavenCentral()
}

dependencies {
    dokkaPlugin("org.appdevforall.dokka:kdoc-to-json:1.0.0-dokka2.2-SNAPSHOT")
}

@OptIn(InternalDokkaApi::class)
abstract class JsonOutputPluginParameters @Inject constructor(
    name: String
) : DokkaPluginParametersBaseSpec(name, "org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") {
    override fun jsonEncode(): String = """{
        "logLevel": "debug",
        "logFile": "build/dokka_json.log",
        "omitFields": ["sources"],
        "replaceHtmlExtension": true,
        "omitNulls": true,
        "prettyPrint": true
    }"""
}

// jdk17 checkout lives one directory up, sparse-checked-out to just src/java.base.
val jdkBaseClasses = file("../jdk17/src/java.base/share/classes")

// Bisection knob: -PsrcRoots=java/util,java/lang restricts the source set to just those
// dirs (relative to jdkBaseClasses) instead of the full java/javax/jdk/sun/com set, so a
// StackOverflowError can be narrowed down to a specific package without editing this file.
val srcRootsProp = (findProperty("srcRoots") as String?)
val defaultRoots = listOf("java", "javax", "jdk", "sun", "com")
val selectedRoots = srcRootsProp?.split(",")?.map { it.trim() } ?: defaultRoots

// Every pair below triggers https://github.com/Kotlin/dokka/issues/2171 (a genuine
// StackOverflowError deep in Dokka's own Java {@inheritDoc} resolver, confirmed via
// bisect_inheritdoc.py - reproduces even at 256MB thread stack, so it's a true infinite
// loop, not just deep recursion). Each pair is a class alongside its immediate
// super/interface, both carrying heavy {@inheritDoc} javadoc - excluding either file in a
// pair is enough to avoid the crash, at the cost of that one class's page. Confirmed newer
// Dokka (2.2.0 GA vs the 2.2.0-Beta kdoc-to-json normally targets) does NOT fix this.
val dokka2171ExcludedFiles = listOf(
    "java/lang/reflect/Constructor.java",
    "java/lang/reflect/Executable.java",
    "java/lang/reflect/AccessibleObject.java",
    "java/lang/reflect/Field.java",
    "java/io/BufferedReader.java",
    "java/io/LineNumberReader.java",
    "java/util/AbstractList.java",
    "java/util/AbstractSequentialList.java",
    "java/util/NavigableMap.java",
    "java/util/TreeMap.java",
    "java/util/concurrent/ConcurrentNavigableMap.java",
    "java/util/concurrent/ConcurrentSkipListMap.java",
    "java/util/concurrent/ScheduledThreadPoolExecutor.java",
    "java/util/concurrent/ThreadPoolExecutor.java",
    "java/util/NavigableSet.java",
    "java/util/TreeSet.java",
    "java/util/concurrent/BlockingDeque.java",
    "java/util/concurrent/LinkedBlockingDeque.java",
)

dokka {
    moduleName.set("java.base")

    dokkaSourceSets.create("java_base") {
        displayName.set("java.base")
        jdkVersion.set(17)

        // Bisection knob: -PabsSourceRoot=/tmp/staging-dir overrides everything below with a
        // single arbitrary directory (e.g. a staging tree of symlinks mirroring the real
        // java/lang/... package layout for just a candidate subset of files) - used to
        // binary-search for the specific file(s) that trigger the crash above. NOTE:
        // suppressedFiles is NOT used for this, because it only filters final output pages -
        // the analysis phase where the crash happens still processes every file under
        // sourceRoots regardless of suppressedFiles, so excluding candidates that way doesn't
        // actually change what gets analyzed.
        val absSourceRoot = (findProperty("absSourceRoot") as String?)
        if (absSourceRoot != null) {
            sourceRoots.from(file(absSourceRoot))
        } else {
            // A fileTree(dir) { exclude(...) } does NOT work here - confirmed by direct
            // experiment. Dokka's Java analysis re-walks each sourceRoots entry as a real
            // directory on disk rather than iterating Gradle's already-filtered FileTree, so
            // Gradle-level excludes are silently ignored and the excluded files get analyzed
            // anyway. The only mechanism that reliably keeps a file out of analysis is for it
            // to not physically exist under the sourceRoot on disk - so build a staging
            // directory of symlinks (mirroring the real java/javax/jdk/sun/com package
            // layout) that just omits the known-bad files, and hand Dokka that single
            // directory. Rebuilt fresh on every configure - cheap (symlinks only, no copying).
            val stagingDir = layout.buildDirectory.dir("dokka-source-staging").get().asFile
            stagingDir.deleteRecursively()
            selectedRoots.forEach { root ->
                jdkBaseClasses.resolve(root).walkTopDown().filter { it.isFile }.forEach { src ->
                    val rel = src.relativeTo(jdkBaseClasses).invariantSeparatorsPath
                    if (rel !in dokka2171ExcludedFiles) {
                        val dest = stagingDir.resolve(rel)
                        dest.parentFile.mkdirs()
                        java.nio.file.Files.createSymbolicLink(dest.toPath(), src.toPath())
                    }
                }
            }
            sourceRoots.from(stagingDir)
        }
    }

    pluginsConfiguration {
        registerBinding(JsonOutputPluginParameters::class, JsonOutputPluginParameters::class)
        register<JsonOutputPluginParameters>("org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") { }
    }

    dokkaPublications.html {
        outputDirectory.set(layout.buildDirectory.dir("dokka-json-output"))
    }
}
