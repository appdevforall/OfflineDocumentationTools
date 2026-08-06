#!/usr/bin/env bash
# Builds the kotlin-stdlib/kotlin-test/kotlin-reflect API docs twice against a
# kotlin-stdlib-docs checkout: once with Dokka's default HTML renderer, and once with the
# kdoc-to-json plugin's JSON renderer (via the dokkaGenerateModuleJson task). Each build writes
# to its own output directory so the two can be compared directly.
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <path-to-kotlin-stdlib-docs> [output-dir]" >&2
    exit 1
fi

STDLIB_DOCS_DIR="$(cd "$1" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_ROOT="$(mkdir -p "${2:-$SCRIPT_DIR/build-output}" && cd "${2:-$SCRIPT_DIR/build-output}" && pwd)"
HTML_OUTPUT_DIR="$OUTPUT_ROOT/html"
JSON_OUTPUT_DIR="$OUTPUT_ROOT/json"

if [ ! -f "$STDLIB_DOCS_DIR/settings.gradle.kts" ] || [ ! -x "$STDLIB_DOCS_DIR/gradlew" ]; then
    echo "Error: '$STDLIB_DOCS_DIR' doesn't look like a kotlin-stdlib-docs checkout (missing settings.gradle.kts or gradlew)." >&2
    exit 1
fi

echo "==> Installing kdoc-to-json-enabled build.gradle.kts into $STDLIB_DOCS_DIR"
if [ -f "$STDLIB_DOCS_DIR/build.gradle.kts" ] && [ ! -f "$STDLIB_DOCS_DIR/build.gradle.kts.orig" ]; then
    cp "$STDLIB_DOCS_DIR/build.gradle.kts" "$STDLIB_DOCS_DIR/build.gradle.kts.orig"
    echo "    (original build.gradle.kts backed up to build.gradle.kts.orig)"
fi
cp "$SCRIPT_DIR/build.gradle.kts" "$STDLIB_DOCS_DIR/build.gradle.kts"

echo "==> [1/2] Generating default HTML documentation..."
(cd "$STDLIB_DOCS_DIR" && ./gradlew dokkaGenerateHtml "-PdocsBuildDir=$HTML_OUTPUT_DIR")

echo "==> [2/2] Generating JSON documentation via kdoc-to-json..."
(cd "$STDLIB_DOCS_DIR" && ./gradlew dokkaGenerateModuleJson "-PdocsBuildDir=$JSON_OUTPUT_DIR")

echo
echo "Done."
echo "  HTML docs: $HTML_OUTPUT_DIR/latest/all-libs"
echo "  JSON docs: $JSON_OUTPUT_DIR/latest/all-libs"
