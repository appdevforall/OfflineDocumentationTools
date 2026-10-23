// Renders the plugin's javadoc-mode JSON into browsable HTML using Pebble templates that follow
// the official javadoc page structure.
//
// Deliberately plain Java: it has to build on whatever JDK is around (including ones too new for
// the Kotlin version the plugin itself is pinned to), and there is nothing here that needs Kotlin.
plugins {
    application
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("io.pebbletemplates:pebble:3.2.2")
    implementation("com.fasterxml.jackson.core:jackson-databind:2.17.2")
}

java {
    // Compatibility rather than a toolchain: this has to build with whatever JDK is on the
    // machine (a toolchain would demand a specific one be installed and registered), and the
    // code targets nothing newer than 17.
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

application {
    mainClass.set("org.appdevforall.docs.render.JavadocHtmlRenderer")
}

tasks.named<JavaExec>("run") {
    // Lets the driver script pass "<json-dir> <out-dir>" through as -Pargs="..."
    (findProperty("args") as String?)?.let { args = it.split(" ").filter { a -> a.isNotEmpty() } }
}
