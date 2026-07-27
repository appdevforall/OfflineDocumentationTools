# Test Plan: kdoc-to-json Core Functionality (PR #18)

## Scope

`tests/test_*.sh` currently covers every `JsonPluginConfig` option (`omitFields`, `omitNulls`, `logLevel`, `logFile`, `classDiscriminator`, `prettyPrint`, `replaceHtmlExtension`, `sourceSetWhitelist`) against the existing `examples/example-data-processor` fixture. None of that exercises whether the plugin actually maps Dokka's model correctly — that's everything in `ModelMapper.kt`, the traversal/index-generation logic in `JsonRenderer.kt`, and the cross-module link resolution in `LinkPostProcessor.kt`. This plan covers that remaining surface.

It follows the existing harness pattern (`tests/lib.sh`, one `test_*.sh` per concern, run via `run_all.sh`), reusing `publish_plugin`/`run_dokka`/`assert_*` where possible. New Kotlin fixtures will need to be added to `examples/example-data-processor` since the current one (`DataProcessor`, `ConnectionManager`, `Level1/Level2/Level3`, `Provider<T>`, `Meta`, `DataMap`) doesn't exercise most of the type-system and doc-tag branches below.

## 1. Fixture gaps to fill first

The example library needs new declarations before several tests below are possible. Recommend adding a `com.example.testlib.Advanced` (or similar) file covering:

| Construct | Why it's missing today | Exercises |
| --- | --- | --- |
| Generic class/function with bounded type param (`<T : Comparable<T>>`) | `Provider<T>` has an unbounded param | `TypeParameterDto.bounds`, `mapProjection` variance branches |
| Function with `in`/`out` variance use-site (`Consumer<in T>`) | nothing uses declaration- or use-site variance | `CovarianceDto`/`ContravarianceDto`/`InvarianceDto` |
| Nullable and platform types (`String?`, a Java-interop call like `java.util.Date`) | everything today is non-null Kotlin | `NullableDto`, `PrimitiveJavaTypeDto`/`JavaObjectDto` |
| Extension function/property, suspend function, infix/operator function, function with default parameter value | none exist | `FunctionalTypeConstructorDto.isExtensionFunction`, `additionalModifiers`, `ExtrasDto.defaultValues` |
| Annotation with multiple parameters, applied more than once, and an annotation used on a parameter | `@Meta` has one param, used once, only on classes | `AnnotationWrapperDto.params`, `ExtrasDto.annotations` per-target |
| Data class, sealed class hierarchy, companion object with real members | `companion` is always null today | `companion` field, `supertypes` with a sealed base |
| A class that shadows/overrides `equals`/`toString`, and a class implementing `Throwable` | nothing trips `ObviousMember`/`ExceptionInSupertypes` | `ExtrasDto.isObviousMember`, `isException` |
| KDoc with `@see`, `@throws`, a fenced code block, a `@sample` tag pointing at a runnable function, nested `<b>`/`<i>`/list markup, and a raw `<`/`>`/`&` in prose | current KDoc is plain prose + `@param`/`@return`/`@author` | `extractText` HTML-tag branches and escaping, `Sample` fallback-to-page-content logic in `mapDocNodes` |
| `expect`/`actual` declarations across two source sets (or reuse a second Gradle source set) | example lib is JVM-only, single source set | `expectPresentInSet`, per-source-set maps (`visibility`, `sources`, `modifier`, `underlyingType`) resolving differently per set |

## 2. DTO mapping correctness (`ModelMapper.mapToDto`)

For each Documentable kind, assert the emitted JSON has the right `kind` discriminator and the fields that only apply to that kind are populated correctly (not just non-null, but matching the actual source construct).

| Test | Assertion |
| --- | --- |
| Module → Package → Class → Function nesting | Root `index.json` deserializes to a `ModuleDto` containing the expected package names; a package `index.json` lists its classlikes/functions/properties/typeAliases with correct names and counts |
| Class vs. Interface vs. Enum vs. Object vs. Annotation | Each emits its own `kind` value and only its own DTO's fields (e.g. `EnumDto.entries` is non-empty and `ClassDto` has no `entries` key) |
| `EnumEntryDto` | Each `Level3` entry (`ACTIVE`, `INACTIVE`) appears with its own KDoc text intact |
| Constructor mapping | `DFunction.isConstructor` round-trips as `true` for constructors and `false` for regular functions |
| Property with getter/setter | `PropertyDto.getter`/`setter` populated for a `var`, `setter` null for a `val` |
| `TypeAliasDto` | `DataMap`'s `underlyingType` resolves to a `GenericTypeConstructorDto` for `Map<String, Any>`, not a raw string |
| Nested classlikes at every depth | `Level1 → Level2 → Level3` each produce the correct `breadcrumbs` list (name + url pairs) in root-to-leaf order |
| Shallow vs. deep recursion | A package's `index.json` lists its classes with `shallow=true` (empty `functions`/`properties`/`classlikes` on the nested entries), while that class's own `index.json` has those same members fully populated |

## 3. Type/Bound/Projection mapping

| Test | Assertion |
| --- | --- |
| Nullable wrapping | `String?` maps to `NullableDto(inner=...)`, not a flag on the inner bound |
| Generic type with projections | `List<String>` (or the new bounded-generic fixture) maps to `GenericTypeConstructorDto` with one projection, correct `presentableName` |
| Variance | `in T` / `out T` map to `ContravarianceDto`/`CovarianceDto`; invariant default maps to `InvarianceDto` |
| Functional types | A lambda parameter type maps to `FunctionalTypeConstructorDto` with `isExtensionFunction`/`isSuspendable` set correctly for a plain lambda, an extension lambda, and a suspend lambda |
| Java interop | A call into a Java stdlib type maps to `PrimitiveJavaTypeDto` (for primitives) or `JavaObjectDto`/`GenericTypeConstructorDto` as appropriate, with a resolvable `url` when Dokka has external doc links configured, or `unresolved:` otherwise (see §5) |
| Type parameter bounds | A bounded generic (`<T : Comparable<T>>`) emits `TypeParameterDto.bounds` with the bound's own DRI/url, not just the parameter name |

## 4. Documentation tag & text extraction (`mapDocNodes`, `extractText`)

| Test | Assertion |
| --- | --- |
| HTML escaping | KDoc prose containing literal `<`, `>`, `&` is escaped in the output text, not passed through raw (would otherwise corrupt a downstream HTML renderer) |
| Nested inline markup | `**bold** with *italic* and \`code\`` produces correctly nested `<strong>`/`<em>`/`<code>` tags, not flattened or mis-ordered |
| Block-level tags | A fenced code block, a blockquote, and a bulleted list in KDoc all round-trip to their respective `<pre><code>`/`<blockquote>`/`<ul><li>` wrappers |
| `@see`/`@throws`/custom named tags | Emit `TagWrapperDto.name` correctly and resolve any `[Foo]`-style links inside them to a real `url` via `resolveUrl` |
| `@sample` | When the KDoc `@sample` tag has no inline body, the plugin falls back to pulling the runnable sample's source text from the page's `ContentCodeBlock`; verify multiple `@sample` tags on one doc pull the *correct* sample each (via `sampleIndex`), not just the first one repeated |
| Per-source-set documentation | On a construct present in two source sets with different KDoc (or an expect/actual pair), `documentation` is keyed by source set and each entry only contains that set's content |

## 5. `resolveUrl` / link resolution edge cases

| Test | Assertion |
| --- | --- |
| In-module link | A `[Provider]`-style KDoc link or a supertype reference resolves to a real relative path pointing at the target's own JSON file |
| Genuinely external/unconfigured link | Resolves to `"unresolved:<DRI>"` in the pre-postprocess output, confirming the fallback marker still fires (needed for §7 below to have something to resolve) |
| `replaceHtmlExtension` interaction | Confirmed already covered by `test_replace_html_extension.sh` at the config layer — no new test needed here, just noting the overlap |

## 6. `JsonRenderer` traversal & index generation

| Test | Assertion |
| --- | --- |
| `package-list` | Contains the `$dokka.format:json-v1$` / `$dokka.linkExtension:json$` header lines and every real package name, sorted, with whitelisted-out packages excluded |
| `all-types.json` | Contains one entry per class/interface/enum/object/annotation/typeAlias across the whole module, each with the correct `kind` string, sorted by name |
| Multimodule `index.json` | When `context.configuration.modules` is non-empty, a root `index.json` is written with a `ModuleReferenceDto` per module and correct relative URLs (with `.json`/`.html` extension per `replaceHtmlExtension`) |
| File path parity with Dokka's own layout | For a representative set of pages, the `.json` file's path (via `locationProvider.resolve(..., skipExtension = true)`) matches the `.html` path Dokka's default renderer would produce, extension aside — this is what `scripts/kotlin/test_kotlin_stdlib.sh` already checks at scale; worth a smaller, fast version of that check in the local test suite against `example-data-processor` so it doesn't require a full stdlib build to catch a regression |
| Breadcrumbs at the root and at max depth | Root module page has an empty (or single-entry) breadcrumb list; `Level1.Level2.Level3` has a 4-or-5-entry list in the right order |

## 7. `LinkPostProcessor` cross-module resolution

| Test | Assertion |
| --- | --- |
| Two-pass index + replace | After running the full plugin, no `.json` file on disk contains the literal string `unresolved:` (matches the guarantee in the README) |
| Relative path depth | A DRI referenced from a deeply nested page (e.g. `Level1/Level2/Level3`) gets the correct number of `../` segments back to the file that DRI resolves to, and a root-level file gets `./` |
| Genuinely unresolvable DRI | Patched to the literal string `"#"`, and the build log contains a `Failed to resolve N DRIs` warning naming it (this is currently only documented, not tested) |
| "Last-writer-wins" for expect/actual | When a DRI legitimately resolves in two files (expect + actual declarations), the postprocessor doesn't crash or produce inconsistent output — pick one deterministically and assert it's a valid link, not asserting a specific winner |

## 8. Structural parity vs. baseline HTML build

The repo already has the tooling for this — it just needs to run as part of the test suite rather than manually:

- `scripts/verify_package_index.py`: run against `example-data-processor`'s JSON output and assert exit code 0 (every object in a package's `index.json` has a page at the URL it points to).
- `scripts/kotlin/test_kotlin_stdlib.sh`: the real stress test — confirms the JSON build and the default HTML build produce a 1:1 set of pages for a large, real-world, multiplatform codebase (kotlin-stdlib). This should be run at least once before merging, even if it's too slow for the fast local suite, since it's the only test that exercises multiplatform expect/actual and large-scale multimodule behavior end to end.
- `scripts/sanity_check.py compare-base`: page-for-page diff between a stock Dokka HTML build and one derived from this plugin's JSON, to catch any page silently dropped or renamed.

## 9. Robustness / edge cases

| Test | Assertion |
| --- | --- |
| Malformed plugin config | An unparseable `pluginsConfiguration` value doesn't crash the build — falls through to the lenient manual `Json { ignoreUnknownKeys = true }` parse, and to `JsonPluginConfig()` defaults if even that fails (per the `try`/`catch` in `JsonRenderer.render`) |
| No config at all | Build succeeds using all-default `JsonPluginConfig` |
| Empty/near-empty module | A module with a package but no public declarations doesn't throw; produces a valid (if mostly empty) `package-list` and module `index.json` |
| `classDiscriminator` collision | Setting it to an existing field name (e.g. `"name"`) — README calls this out as a footgun; confirm it fails with a clear serialization error rather than silently corrupting output |

## Suggested execution order

1. Add the fixture gaps in §1 to `example-data-processor` (this unblocks nearly everything else and is the highest-leverage single change).
2. §2–§4 (DTO/type/doc-tag mapping) as new `tests/test_*.sh` scripts against the expanded fixture — fast, deterministic, run on every commit.
3. §6–§7 (renderer/link-postprocessor behavior) — same harness, still fast.
4. §9 (robustness) — cheap to add alongside the above.
5. §8 (kotlin-stdlib parity) — run manually or in a separate slower CI job before merging; not suitable for the fast local suite given the build time.
