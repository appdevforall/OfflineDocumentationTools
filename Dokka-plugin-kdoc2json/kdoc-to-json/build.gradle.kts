plugins {
    kotlin("jvm") version "1.9.24"
    kotlin("plugin.serialization") version "1.9.24"
    `maven-publish`
}

group = "org.appdevforall.dokka"
version = "1.0.0-SNAPSHOT"

repositories {
    mavenCentral()
}

dependencies {
    compileOnly("org.jetbrains.dokka:dokka-core:2.2.0-Beta")
    compileOnly("org.jetbrains.dokka:dokka-base:2.2.0-Beta") 
    // Dokka deserializes this plugin's config block with Jackson, not kotlinx.serialization
    // (see org.jetbrains.dokka.utilities.parseJson), so a config key whose JSON spelling
    // differs from its Kotlin property name needs @JsonProperty to be seen -- @SerialName
    // alone is ignored on that path. compileOnly: Dokka already brings Jackson at runtime.
    compileOnly("com.fasterxml.jackson.core:jackson-annotations:2.15.3")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.0")
}

publishing {
    publications {
        create<MavenPublication>("maven") {
            from(components["java"])
        }
    }
}