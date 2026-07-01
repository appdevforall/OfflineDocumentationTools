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
./gradlew publishToMavenLocal
```

### Applying the Plugin

In the target project where you want to generate documentation, add the plugin to your Dokka dependencies block:

```kotlin
dependencies {
    dokkaPlugin("my.dokka.plugin:json-output-plugin:1.0.0-SNAPSHOT")
}
```

## 3. Configuration Options

You can configure the JSON plugin by extending `DokkaPluginParametersBaseSpec` and registering it in your `dokka` configuration block. This utilizes the modern Dokka V2 plugin API.

```kotlin
import org.jetbrains.dokka.gradle.engine.plugins.DokkaPluginParametersBaseSpec
import org.jetbrains.dokka.InternalDokkaApi
import javax.inject.Inject

@OptIn(InternalDokkaApi::class)
abstract class JsonOutputPluginParameters @Inject constructor(
    name: String
) : DokkaPluginParametersBaseSpec(name, "my.dokka.plugin.JsonOutputPlugin") {
    
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
        register<JsonOutputPluginParameters>("my.dokka.plugin.JsonOutputPlugin") { }
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

If you are inspecting the JSON and notice a URL like `unresolved:kotlin.collections/List///PointingToDeclaration/`, this means the `LinkPostProcessor` failed to find that DRI in the current build environment.

This usually happens when:

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
