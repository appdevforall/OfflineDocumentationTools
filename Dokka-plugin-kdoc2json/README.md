# JSON Dokka Plugin

A custom Dokka plugin that replaces Dokka's default HTML renderer to output a raw, structured JSON representation of your Kotlin documentation.

This plugin is designed for "headless" documentation pipelines where you want Dokka to handle the complex parsing, AST resolution, and multi-platform expect/actual merging, but you want to render the final visual output using a custom Static Site Generator (SSG) or templating engine (such as Pebble, Jinja, or React).

---

## 1. Introduction

This plugin intercepts base Dokka's pipeline just before rendering to output JSON data in place of HTML files. It maps Dokka's internal Documentable Abstract Syntax Tree (AST) into clean, serializable Data Transfer Objects (DTOs) and writes them to disk as `.json` files. It preserves package hierarchies, generic bounds, platform source sets, and documentation tags while allowing frontend developers total freedom over the final HTML/CSS.

## 2. Getting Started

### Building the Plugin

Clone this repository and publish the plugin to your local Maven repository:

```bash
cd kdoc-to-json
./gradlew publishToMavenLocal
```

### Applying the Plugin

In the target project where you want to generate documentation, add the plugin to your Dokka dependencies block:

```kotlin
dependencies {
    dokkaPlugin("org.appdevforall.dokka:kdoc-to-json:1.0.0-SNAPSHOT")
}
```

> **Dokka version compatibility:** `kdoc-to-json/build.gradle.kts` compiles against `dokka-core`/`dokka-base` version `2.2.0-Beta` by default (chosen for compatibility with the [kotlin-stdlib-docs build tool](https://github.com/JetBrains/kotlin/tree/master/libraries/tools/kotlin-stdlib-docs)). Your consuming project's `org.jetbrains.dokka` Gradle plugin version should match (or be binary-compatible with) that version — see `examples/example-data-processor/build.gradle.kts` for a working setup. If you need a different Dokka version, update the `compileOnly` versions in `kdoc-to-json/build.gradle.kts` and republish before applying the plugin to a project on that version.

### Trying It Out with the Example Library

`examples/example-data-processor` is a small sample Kotlin library already wired up to use this plugin (see its `build.gradle.kts`). To build the plugin and generate its JSON documentation in one step, run:

```bash
./scripts/build-example.sh
```

This publishes `kdoc-to-json` to your local Maven repository and then runs Dokka against the example library. Output is written to `examples/example-data-processor/build/dokka/html/`.

### Sanity-Checking Rendered Output

`scripts/sanity_check.py` helps validate documentation rendered downstream from this plugin's JSON (e.g. by a templating engine like Pebble or Jinja that turns the JSON into HTML pages):

```bash
# Check that every file in a list (e.g. Dokka's package-list) exists in the rendered output
python3 scripts/sanity_check.py check-list files.txt path/to/rendered-html

# Compare a standard Dokka HTML build against a JSON-derived HTML build, page for page
python3 scripts/sanity_check.py compare-base path/to/dokka-html path/to/rendered-html

# Scan a directory of rendered HTML files for broken internal links
python3 scripts/sanity_check.py check-links path/to/rendered-html
```

Each subcommand accepts `--output-file/-o` to write a full results log to disk.

`scripts/verify_sourceset_whitelist.py` recursively scans a rendered JSON output directory and confirms every file's top-level `sourceSets` field intersects a given whitelist — useful for confirming the plugin's `sourceSetWhitelist` config option (or a downstream filter) actually excluded everything it should have:

```bash
python3 scripts/verify_sourceset_whitelist.py path/to/rendered-json jvm js
```

Pass one or more source set names (space- or comma-separated) as the whitelist. Files without a top-level `sourceSets` field (e.g. `all-types.json`, the multimodule root `index.json`) are synthetic/aggregate outputs and are skipped rather than flagged. Accepts `--output-file/-o` to log violations and `--verbose/-v` to print every file checked.

## 3. Configuration Options

You can configure the JSON plugin by extending `DokkaPluginParametersBaseSpec` and registering it in your `dokka` configuration block. This utilizes the modern Dokka V2 plugin API.

```kotlin
import org.jetbrains.dokka.gradle.engine.plugins.DokkaPluginParametersBaseSpec
import org.jetbrains.dokka.InternalDokkaApi
import javax.inject.Inject

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
        "omitNulls": true
    }"""
}

dokka {
    pluginsConfiguration {
        registerBinding(JsonOutputPluginParameters::class, JsonOutputPluginParameters::class)
        register<JsonOutputPluginParameters>("org.appdevforall.dokka.kdoc2json.JsonOutputPlugin") { }
    }
}
```

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `logLevel` | String | `"debug"` | Controls the verbosity of the plugin's internal logger (`"info"`, `"debug"`, `"warn"`, `"error"`). |
| `logFile` | String | *(Optional)* | Absolute or relative path to output the plugin's debug logs. Highly recommended as Dokka often swallows standard output. |
| `replaceHtmlExtension` | Boolean | `false` | If `true`, the plugin will rewrite all internal relative URLs to end in `.json` instead of `.html`. |
| `omitFields` | List | `[]` | A list of JSON keys to completely strip from the final output (e.g., `["breadcrumbs", "sources"]`). Useful for reducing disk footprint. |
| `omitNulls` | Boolean | `false` | If `true`, deeply filters the AST payload to remove any keys where the value is null, an empty string, an empty array, or an empty object. |
| `classDiscriminator` | String | `"kind"` | The JSON key used to discriminate between polymorphic `Documentable` types (e.g., `"kind": "class"`). Must not collide with an existing DTO field name (e.g. `"type"` or `"name"`), or serialization will fail. |
| `prettyPrint` | Boolean | `false` | If `true`, formats the written JSON files with indentation for human readability instead of compact single-line output. |
| `sourceSetWhitelist` | List | `[]` | A list of source set names (matching the values that appear in the output `sourceSets` field, e.g. `["jvm"]`). If non-empty, any Documentable that isn't present in at least one whitelisted source set has its output file omitted, and a message is logged with the symbol's name and its `sourceSets`. Leave empty to disable filtering (default: all source sets included). |

## 4. Understanding Dokka Terminology

To successfully consume the JSON output, it helps to understand a few core Dokka concepts that dictate the structure of the data:

* **Documentable**: A node in Dokka's AST. Classes, functions, properties, packages, and modules are all `Documentable` objects.
* **DRI (Dokka Resource Identifier)**: A unique string identifier for every symbol in your codebase. (e.g., `kotlin.collections/List/size/#/PointingToDeclaration/`). DRIs are what Dokka uses to link disparate parts of the codebase together.
* **SourceSet**: Represents a target platform or compilation unit (e.g., `jvm`, `js`, `common`, `native`). Dokka merges declarations across SourceSets, which is why properties like `visibility` or `type` are mapped by SourceSet in the JSON.
* **PageNode**: Dokka's representation of a literal page that will be written to disk. The JSON plugin maps a `PageNode` back to its underlying `Documentable` to generate the JSON payload.

## 5. How It Works: Architecture & Lifecycle

The plugin operates in two distinct phases:

### Phase 1: The `JsonRenderer` (Synchronous AST Traversal)

The plugin implements the Dokka `Renderer` interface, completely overriding the default HTML generation. It walks the `RootPageNode` tree synchronously. For every page, it extracts the underlying `Documentable`, passes it to the `ModelMapper`, and translates the complex Dokka AST into clean Kotlin DTOs. These DTOs are serialized using `kotlinx.serialization` and written to disk.

### Phase 2: The `LinkPostProcessor` (Cross-Module Resolution)

Because Dokka resolves links across different modules *during* the HTML rendering phase, our JSON plugin must do the same. When the `JsonRenderer` encounters a DRI that belongs to an external module, it writes `"url": "unresolved:<DRI>"`.
Once all JSON files are written, the `LinkPostProcessor` spins up. It reads all JSON files on disk, builds a master index of every DRI, and performs a regex replacement to patch all `unresolved:` links into valid relative file paths.

## 6. The JSON Output Format

### Directory Structure

The plugin mirrors Dokka's standard hierarchical folder structure. However, instead of `index.html` files, you will find `.json` files.

Special aggregated files include:

* `index.json`: The index.json files serve as the primary entry points for modules, packages, and classes, containing the structural metadata and immediate member declarations specific to each hierarchical level.
* `all-types.json`: Created at the root of a module. Contains a flat, searchable array of every class, interface, object, and type alias in that module.
* `package-list`: A standard Dokka package list.

### The Semantic Model (Polymorphism)

The JSON payloads are strictly typed. Every top-level object and nested member contains a `"kind"` discriminator (e.g., `"kind": "class"`, `"kind": "function"`, `"kind": "TypeAliased"`). This makes it incredibly easy to parse the JSON back into typed objects in your frontend layer.

*(To minimize disk footprint, you can leverage the `omitFields` and `omitNulls` configuration options. The plugin uses a custom recursive JSON filter to strip out empty lists, objects, and null values before writing to disk).*

## 7. Resolving Cross-Module Links

`LinkPostProcessor` rewrites every `unresolved:<DRI>` marker in a single pass, so the final on-disk JSON never contains an `unresolved:` string to grep for — resolved DRIs become a relative path, and DRIs it couldn't find become a bare `"#"`. To find which links failed to resolve, check the Dokka build log rather than the output JSON: the post-processor logs a `Failed to resolve N DRIs (patched to "#")` warning listing every DRI it couldn't place.

A link typically fails to resolve when:

1. The target module was not included in the Dokka multi-module task.
2. The target dependency is an external library, and external documentation links were not properly configured in the `build.gradle.kts` file.

If the DRI *is* present in the current build, the `LinkPostProcessor` will automatically patch it to a relative path like `../../kotlin-stdlib/kotlin.collections/-list/index.json`.

## 8. Consuming the JSON (Example: Pebble)

Because the JSON maintains Dokka's strict hierarchy, templating engines like Pebble or Jinja can iterate over it natively.

For example, to render a table of functions for a class:

```pebble
{% if functions is defined and functions is not empty %}
    <h2>Functions</h2>
    <table>
        {% for member in functions %}
            <tr>
                <td><a href="{{ member.url }}">{{ member.name }}</a></td>
                <td>
                    {# Render the parameters #}
                    fun {{ member.name }}(
                        {% for param in member.parameters %}
                            {{ param.name }}: {{ param.type.name }}
                        {% endfor %}
                    )
                </td>
            </tr>
        {% endfor %}
    </table>
{% endif %}
```

## 9. Building the Kotlin Standard Library Docs (`scripts/kotlin`)

`scripts/kotlin/` contains everything needed to generate both the default HTML docs and the kdoc-to-json JSON docs for the Kotlin standard library (`kotlin-stdlib`, `kotlin-test`, `kotlin-reflect`), side by side, for comparison purposes.

* **`scripts/kotlin/build.gradle.kts`** — a drop-in replacement for the `build.gradle.kts` in a `kotlin-stdlib-docs` checkout (the Dokka doc-build module inside JetBrains' [kotlin](https://github.com/JetBrains/kotlin) repo, at `libraries/tools/kotlin-stdlib-docs`). It adds a `dokkaGenerateModuleJson` task and gates the `kdoc-to-json` plugin (classpath + `pluginsConfiguration`) behind `gradle.startParameter.taskNames` so the plugin is only active when `dokkaGenerateModuleJson` is explicitly requested — every other Dokka task (`dokkaGenerateHtml`, `dokkaGeneratePublicationHtml`, etc.) is unaffected and still produces normal HTML.
* **`scripts/kotlin/build-kotlin-stdlib.sh`** — installs that `build.gradle.kts` into a `kotlin-stdlib-docs` checkout (backing up the original as `build.gradle.kts.orig` on first run) and then runs both `dokkaGenerateHtml` and `dokkaGenerateModuleJson`, each with its own `-PdocsBuildDir`, so the two outputs land in separate directories instead of overwriting each other.

**Usage:**

```bash
./scripts/kotlin/build-kotlin-stdlib.sh /path/to/kotlin/libraries/tools/kotlin-stdlib-docs [output-dir]
```

This writes HTML output to `<output-dir>/html/latest/all-libs` and JSON output to `<output-dir>/json/latest/all-libs` (`output-dir` defaults to `scripts/kotlin/build-output`).

> **Provenance / staleness warning:** `scripts/kotlin/build.gradle.kts` was derived from the `kotlin-stdlib-docs/build.gradle.kts` in JetBrains' `kotlin` repo as of commit [`cfcb49fd0113`](https://github.com/JetBrains/kotlin/commit/cfcb49fd0113d2300a2b677c4fc2e16dddff7df5) ("[stdlib] Update Dokka to 2.2.0-Beta and migrate to DGPv2"). That upstream file is not under our control and can change — new source sets, Dokka API changes, or a different doc-build structure could all require re-diffing our modifications against a newer upstream version. If `build-kotlin-stdlib.sh` starts failing against a newer `kotlin` checkout, compare `scripts/kotlin/build.gradle.kts` against the current upstream `kotlin-stdlib-docs/build.gradle.kts` and re-apply the `useJsonPlugin`/`dokkaGenerateModuleJson` additions by hand.
