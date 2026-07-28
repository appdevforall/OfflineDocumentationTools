// A stock-Dokka-HTML sibling of examples/example-data-processor, reusing the exact
// same sources but WITHOUT the kdoc-to-json plugin. Exists purely so
// tests/test_renderer.sh can confirm our JSON renderer writes each page at the same
// path (extension aside) that Dokka's own default HTML renderer would -- a silent
// path mismatch here would mean some page went missing or was misplaced.
plugins {
    kotlin("jvm") version "1.9.23"
    id("org.jetbrains.dokka") version "2.2.0-Beta"
}

repositories {
    mavenCentral()
}

kotlin {
    sourceSets.main {
        kotlin.srcDir("../example-data-processor/src/main/kotlin")
    }
}
