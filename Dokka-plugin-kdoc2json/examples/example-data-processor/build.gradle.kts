import org.jetbrains.dokka.gradle.DokkaTask

plugins {
    kotlin("jvm") version "1.9.23"
    id("org.jetbrains.dokka") version "1.9.20"
}

repositories {
    // CRITICAL: This allows Gradle to find your locally published json-dokka-plugin
    mavenLocal() 
    mavenCentral()
}

dependencies {
    // Inject your custom Dokka plugin into the documentation pipeline
    dokkaPlugin("com.example.dokka:json-dokka-plugin:1.0.0")
}

// Configure the Dokka execution
tasks.withType<DokkaTask>().configureEach {
    // Tell your custom plugin exactly what to name the output file
    pluginsMapConfiguration.set(
        mapOf(
            "com.example.dokka.JsonExportPlugin" to """{"outputFileName": "api-documentation.json"}"""
        )
    )
}
