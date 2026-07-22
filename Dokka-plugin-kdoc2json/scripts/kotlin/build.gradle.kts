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

        // ALL REMOTE JETBRAINS SNAPSHOT SERVERS HAVE BEEN REMOVED!
    }

    // --- ADDED THIS EXCLUSION BLOCK ---
    // Globally ignore the remote playground plugin since it is trapped on the dead 503 server.
    configurations.all {
        exclude(group = "org.jetbrains.dokka", module = "kotlin-playground-samples-plugin")
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
val artifactsVersion by extra(if (isTeamcityBuild) project.property("deployVersion") as String else defaultSnapshotVersion)
val artifactsRepo by extra(if (isTeamcityBuild) project.property("kotlinLibsRepo") as String else "$kotlin_root/build/repo")
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
