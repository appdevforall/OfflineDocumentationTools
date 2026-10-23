// Renders the JSON that android_docs_to_json.py writes into browsable HTML, using Pebble
// templates that follow the layout of the reference pages the JSON was read from.
//
// Deliberately plain Java, and compiled for 17 rather than through a toolchain: this has to build
// with whatever JDK happens to be on the machine, and nothing here needs anything newer.
plugins {
    application
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("io.pebbletemplates:pebble:3.2.2")
    implementation("com.fasterxml.jackson.core:jackson-databind:2.17.2")

    testImplementation("org.junit.jupiter:junit-jupiter:5.10.2")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

// The templates are the part of this module most likely to break silently: Pebble is configured
// with strictVariables(false), so a field that stops existing renders as nothing rather than as
// an error, and a macro called with a `macros.` prefix renders empty too. The tests render a real
// page and assert the pieces are there.
tasks.test {
    useJUnitPlatform()
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

application {
    mainClass.set("org.appdevforall.docs.android.AndroidDocRenderer")
}

tasks.named<JavaExec>("run") {
    // Lets the driver script pass "<json-dir> <html-dir>" through as -Pargs="..."
    (findProperty("args") as String?)?.let { args = it.split(" ").filter { a -> a.isNotEmpty() } }
}
