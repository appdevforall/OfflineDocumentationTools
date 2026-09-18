import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

import org.jetbrains.dokka.gradle.engine.plugins.DokkaPluginParametersBaseSpec
import org.jetbrains.dokka.InternalDokkaApi
import javax.inject.Inject

plugins {
    base
    `dokka-convention`
}

@OptIn(InternalDokkaApi::class)
abstract class JsonOutputPluginParameters @Inject constructor(
    name: String
) : DokkaPluginParametersBaseSpec(name, "org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") {

    // Define the plugin's behavior via a JSON string
    override fun jsonEncode(): String = """{
        "logLevel": "debug",
        "logFile": "build/dokka_json.log",
        "omitFields": ["sources"],
        "replaceHtmlExtension": false,
        "omitNulls": true,
	"sourceSetWhitelist": ["common", "jvm"]
    }"""
}

// kdoc-to-json overrides Dokka's HTML renderer wholesale, so it must only be added to the
// dokkaPlugin classpath (and registered in the dokka{} config) when dokkaGenerateModuleJson
// is actually among the requested tasks -- otherwise every other Dokka task
// (dokkaGenerateHtml, dokkaGeneratePublicationHtml, etc.) would silently emit JSON instead
// of HTML. Note this matches against exactly-typed task names; abbreviated/camelCase task
// matching on the CLI won't be detected here.
val useJsonPlugin = gradle.startParameter.taskNames.any { it.substringAfterLast(":") == "dokkaGenerateModuleJson" }

allprojects {
    repositories {
        // 1. Your Custom Local Repo (Where the bash script just downloaded the plugins)
        maven(url = rootProject.projectDir.parentFile.parentFile.parentFile.resolve("build/repo"))

        // 2. Standard Local Maven Cache (~/.m2/repository)
        mavenLocal()

        // 3. Maven Central (Keep this for standard standard stable libraries like Gson/Coroutines)
        mavenCentral()

        // 4. Dokka's own dev-snapshot server - required by plugins:dokka-samples-transformer-plugin
        // and plugins:dokka-version-filter-plugin (both included by kotlin-stdlib-docs'
        // settings.gradle.kts and pulled onto the build graph by its dokka-convention plugin),
        // which pin to a Dokka dev build rather than a Maven Central release. Same property +
        // default kotlin-stdlib-docs' own settings.gradle.kts uses, so this only ever points
        // wherever that project already expects it to.
        maven(url = providers.gradleProperty("dokka_repository")
            .getOrElse("https://redirector.kotlinlang.org/maven/dokka-dev"))
    }

    // --- ADDED THIS EXCLUSION BLOCK ---
    // Globally ignore the remote playground plugin since it is trapped on the dead 503 server.
    configurations.all {
        exclude(group = "org.jetbrains.dokka", module = "kotlin-playground-samples-plugin")
    }

    // kotlin-stdlib-docs' own two Dokka plugin subprojects
    // (plugins:dokka-samples-transformer-plugin, plugins:dokka-version-filter-plugin)
    // each hardcode `kotlin { jvmToolchain(8) }`. Both are pulled onto the build
    // graph by the dokka-convention plugin, and dokkaGenerateModuleJson below
    // depends on dokkaGeneratePublicationHtml, so their dependencies have to
    // resolve even though this build only ever wants JSON out. Gradle then needs a
    // JDK 8 toolchain, and with no toolchain download repository configured it
    // fails during task-graph resolution before a single doc is generated:
    //   > Failed to calculate the value of task
    //     ':plugins:dokka-samples-transformer-plugin:compileJava' property 'javaCompiler'.
    //      > Cannot find a Java installation on your machine ... matching:
    //        {languageVersion=8, ...}. Toolchain download repositories have not been configured.
    // That only survives on a runner that happens to have an EOL JDK 8 lying
    // around for auto-detection to find; it fails on any that doesn't, which
    // includes the act container build-kotlin-docs-local.yaml is written for.
    // These two plugins are only ever loaded in-process by Dokka, under the same
    // JVM this build is already running on, so compiling them for that JVM instead
    // is sufficient - and it means the pipeline needs exactly one JDK (the 17 that
    // .github/workflows/build-kotlin-docs*.yaml installs), not two.
    //
    // Deliberately keyed off the running JVM rather than a hardcoded 17: whatever
    // JDK Gradle is on is guaranteed to be present, so this can never ask for a
    // toolchain that isn't installed, even if the workflow's java-version moves.
    // Registered via plugins.withId + afterEvaluate so it runs after each
    // subproject's own jvmToolchain(8) call rather than being overwritten by it.
    plugins.withId("org.jetbrains.kotlin.jvm") {
        afterEvaluate {
            extensions.findByType(JavaPluginExtension::class.java)?.toolchain {
                languageVersion.set(JavaLanguageVersion.of(JavaVersion.current().majorVersion))
            }
        }
    }
}

val isTeamcityBuild = project.hasProperty("teamcity.version") ||
System.getenv("TEAMCITY_VERSION") != null

// kotlin/libraries/tools/kotlin-stdlib-docs  ->  kotlin
val kotlin_root = rootProject.file("../../../").absoluteFile.invariantSeparatorsPath
val kotlin_libs by extra(layout.buildDirectory.dir("libs").get().asFile.path)

val rootProperties = java.util.Properties().apply {
    file(kotlin_root).resolve("gradle.properties").inputStream().use { stream -> load(stream) }
}
val defaultSnapshotVersion: String by rootProperties
val kotlinLanguageVersion: String by rootProperties

val githubRevision = if (isTeamcityBuild) project.property("githubRevision") else "master"

// Where kotlin_big pulls the kotlin-stdlib/-reflect/-test binaries it extracts
// and documents from, and at what version.
//
// Upstream only honours -PkotlinLibsRepo/-PdeployVersion under TeamCity, and
// otherwise hardcodes "$kotlin_root/build/repo" at defaultSnapshotVersion
// (2.5.255-SNAPSHOT here) - i.e. the artifacts a *local build of the kotlin
// repo itself* would have published. A plain `git clone --depth 1` has no such
// build output, and that snapshot version is published nowhere public, so
// :kotlin_big:extractStdlibCommonMain fails to resolve and the whole docs build
// dies before generating anything:
//   > Could not find org.jetbrains.kotlin:kotlin-stdlib:2.5.255-SNAPSHOT
// Building the kotlin repo just to get them is hours of CI for artifacts that
// already exist on Maven Central, so accept both as ordinary Gradle properties
// regardless of TeamCity. kotlin_big already declares mavenCentral() alongside
// artifactsRepo, so a released version resolves with no repo override at all -
// -PkotlinLibsRepo is there for a private/snapshot repo (and to keep the pair
// symmetric with upstream's own TeamCity path).
//
// Blank is treated as unset so a workflow input that defaults to '' falls
// through to the upstream behaviour rather than resolving against an empty URL.
fun overrideOrNull(name: String): String? =
    (findProperty(name) as String?)?.takeIf { it.isNotBlank() }

val artifactsVersion by extra(
    overrideOrNull("deployVersion")
        ?: if (isTeamcityBuild) project.property("deployVersion") as String else defaultSnapshotVersion
)
val artifactsRepo by extra(
    overrideOrNull("kotlinLibsRepo")
        ?: if (isTeamcityBuild) project.property("kotlinLibsRepo") as String else "$kotlin_root/build/repo"
)
val dokka_version: String by project

println("# Parameters summary:")
println("    isTeamcityBuild: $isTeamcityBuild")
println("    dokka version: $dokka_version")
println("    githubRevision: $githubRevision")
println("    language version: $kotlinLanguageVersion")
println("    artifacts version: $artifactsVersion")
println("    artifacts repo: $artifactsRepo")


val outputDir = (findProperty("docsBuildDir") as String?)?.let{ file(it) } ?: layout.buildDirectory.dir("doc").get().asFile
val inputDirPrevious = file(findProperty("docsPreviousVersionsDir") as String? ?: "$outputDir/previous")
val outputDirPartial = outputDir.resolve("partial")
val kotlin_native_root = file("$kotlin_root/kotlin-native").absolutePath
val templatesDir = file(findProperty("templatesDir") as String? ?: "$projectDir/templates").invariantSeparatorsPath

val cleanDocs by tasks.registering(Delete::class) {
    delete(outputDir)
}

tasks.clean {
    dependsOn(cleanDocs)
}

val prepare by tasks.registering {
    dependsOn(":kotlin_big:extractLibs")
}

version = (findProperty("version") as String?).takeIf { it != "unspecified"}  ?: kotlinLanguageVersion
val isLatest = (findProperty("isLatest") as String?)?.toBoolean() ?: true


(getTasksByName("dokkaGenerateHtml", true) + getTasksByName("dokkaGenerate", true) + getTasksByName(
    "dokkaGenerateModuleHtml", true
) + getTasksByName("dokkaGeneratePublicationHtml", true)).forEach {
    it.dependsOn(prepare)
}

dependencies {
    dokka(project(":kotlin-stdlib"))
    dokka(project(":kotlin-test"))
    dokka(project(":kotlin-reflect"))
    if (useJsonPlugin) {
        dokkaPlugin("org.appdevforall.dokka:kdoc-to-json:1.0.0-SNAPSHOT")
    }
}

subprojects {
    pluginManager.withPlugin("org.jetbrains.dokka") {
        if (useJsonPlugin) {
            dependencies {
                "dokkaPlugin"("org.appdevforall.dokka:kdoc-to-json:1.0.0-SNAPSHOT")
            }

            // Apply the same configuration so the subprojects don't fall back to defaults
            dokka {
                pluginsConfiguration {
                    registerBinding(JsonOutputPluginParameters::class, JsonOutputPluginParameters::class)
                    register<JsonOutputPluginParameters>("org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") { }
                }
            }
        }
    }
}

buildscript {
    dependencies {
        classpath("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.0")
    }
}

dokka {
     val moduleDirName = "all-libs"

     pluginsConfiguration {
         versioning {
             version.set(kotlinLanguageVersion)
             if (isLatest) {
                 olderVersionsDir.set(inputDirPrevious.resolve(moduleDirName))
             }
         }

         if (isLatest) {
             register<VersionFilterPluginParameters>("VersionFilterPlugin") {
                 targetVersion = kotlinLanguageVersion
             }
         }

         if (useJsonPlugin) {
             // Custom plugin registration
             registerBinding(JsonOutputPluginParameters::class, JsonOutputPluginParameters::class)
             register<JsonOutputPluginParameters>("org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") { }
         }
     }

     moduleName.set("Kotlin libraries")

     dokkaPublications.html {
         if (isLatest) {
             outputDirectory.set(outputDir.resolve("latest").resolve(moduleDirName))
         } else {
             outputDirectory.set(
                 outputDir.resolve("previous").resolve(moduleDirName).resolve(kotlinLanguageVersion)
             )
         }
     }
}

/// Capture all project state at configuration time to stay configuration-cache compatible
val dokkaOutputDirectory = dokka.dokkaPublications.html.get().outputDirectory.get().asFile
val moduleArtifacts = configurations["dokka"].allDependencies.withType(ProjectDependency::class.java)
    .map { dependency ->
        val dependencyProject = project(dependency.path)
        dependencyProject.layout.buildDirectory.file("dokka-module/html/module-descriptor.json").get().asFile to
                dependencyProject.layout.buildDirectory.file("dokka-module/html/module/package-list").get().asFile
    }

getTasksByName("dokkaGeneratePublicationHtml", false).forEach { task ->
    // Copy into locals so the doLast closure captures these values, not the enclosing
    // build-script object (which the configuration cache cannot serialize).
    val outputDirectory = dokkaOutputDirectory
    val artifacts = moduleArtifacts
    task.doLast {
        artifacts.forEach { (jsonFile, packageList) ->
            val fileAsJsonObject = Json.decodeFromString<JsonObject>(jsonFile.readText())
            val modulePath = (fileAsJsonObject.get("modulePath") as JsonPrimitive).content
            val targetDir = outputDirectory.resolve(modulePath)
            targetDir.mkdirs()
            packageList.copyTo(targetDir.resolve(packageList.name), overwrite = true)
        }
    }
}

val dokkaGenerateModuleJson by tasks.registering {
    group = "documentation"
    description = "Builds the kotlin-stdlib/kotlin-test/kotlin-reflect API docs as JSON via the kdoc-to-json Dokka plugin."
    dependsOn(prepare)
    dependsOn(tasks.named("dokkaGeneratePublicationHtml"))
}
