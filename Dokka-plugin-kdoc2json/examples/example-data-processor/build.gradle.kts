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
    dokkaPlugin("my.dokka.plugin:json-output-plugin:1.0.0-SNAPSHOT")
}