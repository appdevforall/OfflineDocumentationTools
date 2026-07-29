#!/usr/bin/env bash
# Builds the kotlin-stdlib/kotlin-test/kotlin-reflect API docs as JSON via the
# kdoc-to-json Dokka plugin, against a full kotlin/ (https://github.com/JetBrains/kotlin)
# repo checkout - freshly compiling and publishing the plugin from source
# first, so every run picks up whatever's currently in
# Dokka-plugin-kdoc2json/kdoc-to-json/src, not a jar left over from an
# earlier run.
#
# Only generates the JSON output (dokkaGenerateModuleJson), not the default
# HTML - JSON/latest/all-libs is the only thing this project's pipeline
# (sync_kdoc_json_to_db.py) consumes. Use build-kotlin-stdlib.sh directly,
# against libraries/tools/kotlin-stdlib-docs, if you also want the HTML
# comparison output that test_kotlin_stdlib.sh checks against.
#
# The target kotlin-stdlib-docs project's build.gradle.kts is swapped out
# for this directory's own (JSON-plugin-enabled) copy for the duration of
# the build, then restored automatically on exit - the kotlin checkout is
# left exactly as it was found, whether the build succeeds or fails.
#
# Only the final output path is written to stdout; every other message goes
# to stderr, so this composes as:
#   STDLIB_ALL_LIBS="$(build-stdlib-json-docs.sh <kotlin-repo-root>)"
set -euo pipefail

log() { echo "$@" >&2; }

if [ $# -lt 1 ]; then
    log "Usage: $0 <path-to-kotlin-repo-root> [output-dir]"
    exit 1
fi

KOTLIN_ROOT="$(cd "$1" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/../../kdoc-to-json" && pwd)"
STDLIB_DOCS_DIR="$KOTLIN_ROOT/libraries/tools/kotlin-stdlib-docs"
OUTPUT_ROOT="$(mkdir -p "${2:-$SCRIPT_DIR/build-output}" && cd "${2:-$SCRIPT_DIR/build-output}" && pwd)"
JSON_OUTPUT_DIR="$OUTPUT_ROOT/json"

if [ ! -f "$KOTLIN_ROOT/gradle.properties" ]; then
    log "error: '$KOTLIN_ROOT' doesn't look like a kotlin repo checkout (missing gradle.properties)."
    exit 1
fi
if [ ! -f "$STDLIB_DOCS_DIR/settings.gradle.kts" ] || [ ! -x "$STDLIB_DOCS_DIR/gradlew" ]; then
    log "error: '$STDLIB_DOCS_DIR' doesn't look like a kotlin-stdlib-docs project (missing settings.gradle.kts or gradlew)."
    exit 1
fi
if [ ! -x "$PLUGIN_DIR/gradlew" ]; then
    log "error: kdoc-to-json plugin project not found at '$PLUGIN_DIR' (missing gradlew)."
    exit 1
fi

# The JSON-plugin-enabled build.gradle.kts we're about to install reads
# dokka_version as a plain Gradle project property (-Pdokka_version=...)
# rather than through this repo's own version catalog, so it has to be
# supplied explicitly - pulled from the same catalog entry the rest of the
# kotlin repo's Dokka usage is pinned to, so it never drifts out of sync.
DOKKA_VERSION="$(grep -m1 '^dokka[[:space:]]*=' "$KOTLIN_ROOT/gradle/libs.versions.toml" | sed -E 's/^dokka[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/')"
if [ -z "$DOKKA_VERSION" ]; then
    log "error: couldn't find a 'dokka = \"...\"' entry in $KOTLIN_ROOT/gradle/libs.versions.toml"
    exit 1
fi

log "==> [1/2] Building and publishing a fresh copy of the kdoc-to-json plugin..."
# Sent to stderr (fd 2), not left on stdout - a caller doing
# STDLIB_ALL_LIBS="$(build-stdlib-json-docs.sh ...)" must only capture the
# final path this script echoes, not gradlew's own build console output.
( cd "$PLUGIN_DIR" && ./gradlew clean publishToMavenLocal ) >&2

log "==> Installing kdoc-to-json-enabled build.gradle.kts into $STDLIB_DOCS_DIR"
ORIGINAL_BUILD_GRADLE="$(mktemp)"
cp "$STDLIB_DOCS_DIR/build.gradle.kts" "$ORIGINAL_BUILD_GRADLE"
restore_build_gradle() {
    cp "$ORIGINAL_BUILD_GRADLE" "$STDLIB_DOCS_DIR/build.gradle.kts"
    rm -f "$ORIGINAL_BUILD_GRADLE"
}
trap restore_build_gradle EXIT
cp "$SCRIPT_DIR/build.gradle.kts" "$STDLIB_DOCS_DIR/build.gradle.kts"

log "==> [2/2] Generating JSON documentation via kdoc-to-json (dokka $DOKKA_VERSION)..."
# --refresh-dependencies forces Gradle to re-resolve the just-published
# SNAPSHOT jar from mavenLocal() rather than serving a same-GAV copy it
# cached from an earlier run of this same script.
( cd "$STDLIB_DOCS_DIR" && ./gradlew dokkaGenerateModuleJson \
    "-PdocsBuildDir=$JSON_OUTPUT_DIR" \
    "-Pdokka_version=$DOKKA_VERSION" \
    --refresh-dependencies ) >&2

ALL_LIBS_DIR="$JSON_OUTPUT_DIR/latest/all-libs"
if [ ! -d "$ALL_LIBS_DIR" ]; then
    log "error: expected output at '$ALL_LIBS_DIR' but it wasn't created."
    exit 1
fi

log "==> Done."
echo "$ALL_LIBS_DIR"
