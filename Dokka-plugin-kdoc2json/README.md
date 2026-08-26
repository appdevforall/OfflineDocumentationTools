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
| `javadoc-mode` | Boolean | `false` | If `true`, emit **javadoc-shaped** JSON mirroring the `api/` tree of the `javadoc` tool instead of Dokka-shaped JSON. See [§10](#10-javadoc-mode). Note the kebab-case key -- it is spelled that way in the config, unlike every other option here. |

> **`omitNulls` also strips *empty* values, not just `null`.** Despite the name, `omitNulls: true` removes a key whenever its value is `null`, `""`, `[]`, or `{}` (see the filter in `JsonRenderer.filterJson`) — so with it enabled, `"functions": []` doesn't appear at all rather than appearing as an empty array. Consumers must treat a **missing** key as equivalent to its empty value (e.g. `functions is defined and functions is not empty`, as in the Pebble example in §8), not assume every key is always present.

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

---

## 10. Javadoc Mode

Setting `"javadoc-mode": true` replaces the plugin's whole output with JSON that mirrors what the
`javadoc` tool produces under its `api/` directory -- same file layout, same page sections, same
member anchors. It is intended for documenting **Java** sources (the JDK's own API docs being the
motivating case) where the downstream templates expect javadoc's structure rather than Dokka's.

Only JSON is written. The one non-JSON file is `element-list`, which javadoc itself emits as a
plain-text manifest and external tooling reads to resolve links into the output. No HTML pages are
produced -- rendering stays the job of the downstream template engine.

> The key is spelled `javadoc-mode`, not `javadocMode`. Every other option in this block is
> camelCase; this one is deliberately kebab-case.

### Output layout

```
index.json                                   overview: the run's modules and packages
element-list                                 javadoc's plain-text manifest (not JSON)
allclasses-index.json                        every documented type
allpackages-index.json                       every documented package
deprecated-list.json                         deprecated elements, grouped by kind
constant-values.json                         static final fields, grouped by package then type
index-files/index-N.json                     the A-Z index, one file per letter
<module>/module-summary.json                 module page
<module>/<pkg/as/path>/package-summary.json  package page
<module>/<pkg/as/path>/<Outer.Nested>.json   type page
```

#### How modules are determined

Dokka's own model has no notion of JPMS -- a Dokka "module" is a build-level grouping -- so
Javadoc mode reads `module-info.java` directly instead. Every configured source root is checked
for one; a root that has one *is* a JPMS module root, which makes this self-validating (an
ordinary `src/main/java` has no `module-info.java`, so a non-modular project is unaffected).

From each descriptor the plugin takes the module name, its doc comment, and its `requires`,
`exports`, `opens`, `uses` and `provides` directives -- everything javadoc's module-summary page
is built from. A package is attributed to the module that declares it, falling back to whichever
module's source root the declaration's file sits under.

The leading `<module>/` segment appears when the run contains more than one module -- more than
one JPMS module if the sources are modular, otherwise more than one Dokka module. That mirrors
javadoc's own split between modular and non-modular builds. A single-module run also writes
`module-summary.json` at the root: javadoc omits a module page entirely for a non-modular build,
but the module's documentation would otherwise be dropped.

To document a modular codebase, then, give Dokka **one source root per module directory** and let
the descriptors do the rest -- see `scripts/java/` for a worked example that does this for the
entire JDK.

#### Multi-module Gradle builds

Separately from JPMS, a *Gradle* multi-module build makes Dokka run the renderer once per
subproject into that subproject's own output directory, then make an aggregating pass. Each
per-module run therefore writes global index files scoped to its own module, and the aggregating
pass writes only the overview `index.json` linking to each. Merging those per-module indexes into
run-wide ones is a downstream step this plugin does not perform. Documenting a modular codebase
from a single Dokka run (as `scripts/java/` does) avoids this entirely.

All links between pages are **relative to the page they appear on** (`../lang/Object.json`), as
javadoc's are, so the tree can be served from any prefix. This includes links inside rendered doc
comments. A link to something the run does not document resolves to `null` (or, inside a comment,
degrades to plain text) rather than becoming a dead `href`.

### Page shape

Type pages carry the sections a javadoc class page has: the type signature and its parts
(`modifiers`, `typeParameters`, `superclass`, `superinterfaces`), the hierarchy closures
(`inheritance`, `allImplementedInterfaces`, `allSuperinterfaces`, `directKnownSubclasses`,
`allKnownSubinterfaces`, `allKnownImplementingClasses`), the doc comment and its block tags
(`description`, `since`, `seeAlso`, `authors`, `versions`, `deprecated`, `tags`), the member
tables (`nestedTypes`, `enumConstants`, `fields`, `constructors`, `methods`, `annotationElements`)
and the inherited-member groups (`inheritedFields`, `inheritedMethods`).

Structured data is primary: types, modifiers, parameters, throws clauses and override
relationships are all discrete fields. Each declaration also carries a flat `signature` string
(`public default Shape<U> scaled(double factor) throws IllegalArgumentException`) as a
convenience -- ignore it if you would rather compose signatures in the template.

Member `anchor` values follow javadoc's scheme: a bare name for a field, `name(erasedParamTypes)`
for an executable, and `<init>(...)` for a constructor -- so `toArray(java.lang.Object[])`, not
`toArray(T[])`. `overrides` and `specifiedBy` are derived from those same erased signatures.

Doc-comment text is HTML, because a javadoc comment's body already is (`<p>`, `<code>`, `<table>`,
`<dl>`). That matches the convention the default output mode already uses.

### Recommended Dokka settings

javadoc documents public **and protected** members by default; Dokka documents only public. For
parity, set this on the consuming project:

```kotlin
dokka {
    dokkaSourceSets.configureEach {
        documentedVisibilities.set(setOf(VisibilityModifier.Public, VisibilityModifier.Protected))
    }
}
```

`examples/example-java-library` is a working Java example wired up this way; `tests/test_javadoc_mode.sh`
drives it.

### Known limitations

These are places where Dokka's model does not carry something a real javadoc page shows. Each is a
missing *input*, not a gap in the mapping:

| Limitation | Effect |
| --- | --- |
| Dokka has no JPMS model | Worked around by parsing `module-info.java` directly (see above), so `requires`/`exports`/`opens`/`uses`/`provides` and the module description *are* populated for modular sources. Without `module-info.java` in a source root those sections are empty. |
| Dokka does not record annotation-element defaults | Annotation elements are reported as one `annotationElements` list rather than being split into javadoc's Required/Optional tables. `defaultValue` is populated only when Dokka does supply it. |
| Dokka has no `record` class kind | Java records are documented as classes; `recordComponents` stays empty. |
| Dokka merges a private field and its accessors into one property | Unfolded back into methods so `getWidth()` is a method and the private field is not documented, as javadoc has it. Note this means a *public* field that happens to have a same-named accessor pair is reported through its accessors. |
| Inherited members depend on Dokka's inheritance propagation | If Dokka does not attach `InheritedMember`, those members appear as declared rather than in an inherited group. |

### What Javadoc mode does not change

`omitFields`, `omitNulls`, `prettyPrint`, `logLevel`, `logFile` and `sourceSetWhitelist` all behave
as documented in [§3](#3-configuration-options). `replaceHtmlExtension` and `classDiscriminator`
have no effect: javadoc-mode pages are written with `.json` links throughout and none of its DTOs
are polymorphic. Javadoc mode also skips the `LinkPostProcessor` pass, since it resolves every link
itself rather than rewriting Dokka's.

Pages are serialized with `encodeDefaults = true`, so every documented key is present on every page
even when empty -- a template can test a field without also testing whether it exists. Enabling
`omitNulls` strips the empty ones back out if you prefer that.

---

## 11. Reproducing the JDK API Docs (`scripts/java`)

`scripts/java/` builds the JDK's own API documentation as javadoc-shaped JSON -- the JSON
counterpart of the `api/` tree in `SourceDocs/JavaDocs/html/api`.

```bash
# Document the JDK that JAVA_HOME points at (use a JDK 17 to match SourceDocs/JavaDocs)
scripts/java/build-jdk-json-docs.sh -j /path/to/jdk-17 -o /path/to/output/api

# Quick check on two small modules instead of all 60
scripts/java/build-jdk-json-docs.sh -j /path/to/jdk-17 -m java.sql,java.transaction.xa
```

### How it works

1. **`stage_jdk_sources.py`** unpacks the JDK's `lib/src.zip` and keeps only what javadoc
   documents. javadoc's rule turns out to be exact: a package appears in `api/` if and only if its
   module `exports` it **unqualified**. For JDK 17's `java.base`, the 53 unqualified exports are
   precisely the 53 documented packages, with nothing left over on either side. The script also
   drops the modules the JDK's own docs build filters out (`jdk.internal.*`, `jdk.unsupported*`,
   `jdk.random`), leaving 60 modules and 224 packages -- exactly what the official docs contain.

   Each module becomes its own directory in the staging tree, with its `module-info.java` copied
   alongside. Everything left behind still resolves from the JDK on the analysis classpath.

2. **`jdk-docs/`** is a Dokka project that registers each staged module directory as a source root
   and runs the plugin in javadoc mode. Nothing is compiled -- Dokka only analyses.

3. **`compare_with_javadoc.py`** checks the result against the official HTML, level by level:

   ```bash
   python3 scripts/java/compare_with_javadoc.py <json-dir> <path-to>/SourceDocs/JavaDocs/html/api
   python3 scripts/java/compare_with_javadoc.py <json-dir> <api-dir> --members
   ```

   `--members` compares the member anchors of every type. That is the sharpest of the checks:
   javadoc's anchor encodes a member's name and its erased parameter types, so a matching anchor
   set means the two sides agree on the members, their signatures and their overloads -- not
   merely on the page count.

### Measured result (JDK 17)

A full run takes **about 90 seconds** and writes **4,988 JSON files**. Against the official docs in
`SourceDocs/JavaDocs/html/api`:

| Level | Result |
| --- | --- |
| modules | **60 / 60** — no missing, no extra |
| packages | **224 / 224** — no missing, no extra |
| types | **4,672 / 4,672** — no missing, no extra |
| member anchors | 4,305 / 4,672 types match *exactly* (92%) |

The 367 types whose member sets differ do so for two understood reasons, neither of which is a
missing page:

- **893 anchors we have that javadoc doesn't** (330 types). javadoc folds an override whose entire
  doc comment is `{@inheritDoc}` — adding nothing of its own — into the superclass's "Methods
  declared in…" list instead of giving it a detail section. `java.awt.Frame.setBackground` is a
  typical case. We document them as the declared members they are, so this is extra data, not lost
  data.
- **481 anchors javadoc has that we don't** (37 types). Where a class extends an *undocumented*
  supertype (a package-private base like `java.awt.AttributeValue`), javadoc pulls that supertype's
  members up and shows them as if declared. We only document what the source declares.

### Two Dokka problems this works around

Both were found running the JDK through it, and both are in Dokka rather than in this plugin:

1. **Unbounded recursion in `{@inheritDoc}`.** Dokka's `InheritDocTagResolver.resolveThrowsTag` →
   `PsiElementToHtmlConverter.toInheritDocHtml` recurses until the stack dies, reproducibly, on
   much of the JDK (`java.io` and `java.util` among others). A bigger stack only buys time:
   `-Xss64m` fails after 42 s, `-Xss512m` after 3m28s. `stage_jdk_sources.py` therefore rewrites
   `{@inheritDoc}` to an inert marker before Dokka parses it, and the plugin resolves the marker
   itself, walking the same supertype chain javadoc walks — so all 3,214 occurrences across the JDK
   still resolve, and the plugin additionally inherits a *missing* `@param`/`@return`/`@throws` the
   way javadoc does. Pass `--keep-inherit-doc` to re-check whether a newer Dokka has fixed this.
2. **Type arguments in DRI parameter types.** Dokka builds a Java DRI from the PSI type's canonical
   text, which carries type arguments, so a naive anchor comes out as
   `addAll(java.util.Collection<? extends E>)` where javadoc uses the erasure,
   `addAll(java.util.Collection)`. `JavadocPaths.eraseGenerics` strips them while keeping array
   brackets. This alone moved member-anchor parity from 80% to 92%.

### Notes

- Dokka generates in a *worker process*, not the Gradle daemon, so `org.gradle.jvmargs` does not
  size it — `dokkaGeneratorIsolation` in `jdk-docs/build.gradle.kts` does. Override with
  `-PdokkaWorkerHeap` / `-PdokkaWorkerStack` if needed.
- Use a JDK whose version matches the docs you are reproducing. Source and docs from different
  update releases differ in small ways that are real, not bugs.
