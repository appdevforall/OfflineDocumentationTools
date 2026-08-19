# JavaTemplated

Generates HTML API documentation for OpenJDK 17's `java.base` module: Dokka (with the
`kdoc-to-json` plugin from [`../Dokka-plugin-kdoc2json`](../Dokka-plugin-kdoc2json))
produces JSON, then a small Pebble-based renderer turns that JSON into HTML.

```
jdk17 source  --[Dokka + kdoc-to-json]-->  JSON  --[Pebble template]-->  HTML
```

## Layout

- **`dokka-java-base-docs/`** - the Dokka project. `build.gradle.kts` points Dokka's Java
  source set at a `jdk17` checkout (see below) and applies `kdoc-to-json` so the
  `dokkaGenerateHtml` task writes JSON instead of HTML. Also bakes in a required
  workaround - see "The Dokka bug" below.
  - `bisect_inheritdoc.py` - the tool that found every file in that workaround. Rerun it
    if a JDK/Dokka/plugin version bump reintroduces the crash on a new file.
- **`pebble-renderer/`** - a small Kotlin CLI (`RenderHtml.kt`) that walks a directory of
  `*.json` files, evaluates each one through a Pebble template, and writes the result to
  the same relative path with a `.html` extension - so the output tree mirrors the JSON
  tree.
- **`peb.peb.txt`** - the actual Pebble template consumed by `pebble-renderer`. Renders
  Dokka's `kind`-discriminated JSON shapes (module / package / class / function /
  property / ...) into styled HTML pages, with internal cross-references rewritten from
  `.json` to `.html` so links between generated pages resolve correctly.

Not checked in - fetched or generated locally (see `.gitignore`):
- `jdk17/` - the OpenJDK source, cloned fresh as a sibling of `dokka-java-base-docs/`.
- `dokka-java-base-docs/build/dokka-json-output/` - Dokka's JSON output.
- `html-output/` (or wherever you point the renderer) - the final HTML.

## Prerequisites

A JDK 17 install, distinct from whatever this machine's default `java` is - the Kotlin
Gradle plugin `kdoc-to-json` depends on doesn't support newer JDK targets, and
`compileJava`/`compileKotlin` need to agree on one. `bisect_inheritdoc.py` finds one
automatically (via macOS's `java_home -v 17`, overridable with `JDK17_HOME`); for the raw
`./gradlew` invocations below, export it yourself, e.g.:

```bash
export JAVA_HOME=$(/usr/libexec/java_home -v 17)   # macOS
# or, if you don't have one: brew install openjdk@17
```

## 1. Fetch the JDK 17 source

```bash
cd JavaTemplated
git clone --no-checkout --depth 1 --filter=blob:none https://github.com/openjdk/jdk17.git jdk17
cd jdk17
git sparse-checkout init --cone
git sparse-checkout set src/java.base
git checkout
```

(A full checkout works too; `dokka-java-base-docs/build.gradle.kts` only reads
`src/java.base/share/classes`. The sparse checkout above just avoids pulling ~30 other
modules you don't need.)

## 2. Build and publish the kdoc-to-json plugin

```bash
cd ../../Dokka-plugin-kdoc2json/kdoc-to-json
./gradlew clean publishToMavenLocal
```

This publishes `org.appdevforall.dokka:kdoc-to-json:1.0.0-SNAPSHOT` to `~/.m2`, which
`dokka-java-base-docs/build.gradle.kts` depends on.

## 3. Generate the JSON

```bash
cd ../../JavaTemplated/dokka-java-base-docs
export JAVA_TOOL_OPTIONS="-Xss256m"   # see "The Dokka bug" below
./gradlew dokkaGenerateHtml
```

(The task is still named `dokkaGenerateHtml` - `kdoc-to-json` overrides Dokka's renderer
to emit JSON instead, it doesn't rename the task.) Output lands in
`build/dokka-json-output/` - 22,209 files, ~215MB for the full module.

## 4. Render the JSON to HTML

```bash
cd ../pebble-renderer
./gradlew run --args="../dokka-java-base-docs/build/dokka-json-output ../html-output"
```

The Pebble template defaults to `../peb.peb.txt` (this project's sibling above); pass a
third arg to `--args` to use a different one. See `pebble-renderer`'s own usage text
(`./gradlew run` with no args) for details.

## The Dokka bug

A full `java.base` run crashes with `java.lang.StackOverflowError` deep in Dokka's own
Java `{@inheritDoc}` resolver
(`org.jetbrains.dokka.analysis.java.parsers.doctag.PsiElementToHtmlConverter`) - a real,
still-open upstream bug ([kotlin/dokka#2171](https://github.com/Kotlin/dokka/issues/2171)),
not something introduced by `kdoc-to-json` or this project's config. Confirmed independent
of stack size (crashes identically at a 256MB thread stack, 512x the JVM default) and
**not** fixed by Dokka 2.2.0 GA (`kdoc-to-json` normally targets 2.2.0-Beta).

`bisect_inheritdoc.py` binary-searches subsets of a package's source files (via a staging
directory of symlinks passed as Dokka's sole `sourceRoots` entry - the only mechanism that
actually excludes a file from analysis; Dokka re-walks each `sourceRoots` directory from
disk rather than respecting Gradle-level `FileTree`/`suppressedFiles` filters) to find the
minimal file or file-pair that reproduces the crash.

`dokka-java-base-docs/build.gradle.kts` hard-excludes the 9 pairs (18 files) found this
way - each a class alongside its immediate super/interface, both carrying heavy
`{@inheritDoc}` javadoc:

| Pair | Package |
| --- | --- |
| `Executable` / `Constructor` | `java.lang.reflect` |
| `AccessibleObject` / `Field` | `java.lang.reflect` |
| `BufferedReader` / `LineNumberReader` | `java.io` |
| `AbstractList` / `AbstractSequentialList` | `java.util` |
| `NavigableMap` / `TreeMap` | `java.util` |
| `NavigableSet` / `TreeSet` | `java.util` |
| `ConcurrentNavigableMap` / `ConcurrentSkipListMap` | `java.util.concurrent` |
| `ScheduledThreadPoolExecutor` / `ThreadPoolExecutor` | `java.util.concurrent` |
| `BlockingDeque` / `LinkedBlockingDeque` | `java.util.concurrent` |

That's 18 of ~2,750 `java.base` source files (0.65%) missing their own page; everything
else - including the *other* half of each pair above - documents normally. If this needs
revisiting, rerun `bisect_inheritdoc.py <package>` for any package that fails and fold the
newly found files into the exclude list in `build.gradle.kts`.

## Not carried over from the original investigation

A one-off fork of `kdoc-to-json` rebuilt against Dokka 2.2.0 GA (to test whether a newer
Dokka fixed the bug above - it didn't) isn't included here; it added nothing to the
working pipeline. Raw bisection/build logs from that investigation also aren't included -
none of it is needed to build the docs, only `bisect_inheritdoc.py` itself is kept, for
future maintenance.
