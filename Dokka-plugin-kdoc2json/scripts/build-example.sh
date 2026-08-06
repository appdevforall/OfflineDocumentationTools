#!/usr/bin/env bash
# Builds the kdoc-to-json plugin, publishes it to the local Maven repository,
# and runs it against the example library to produce sample JSON output.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> [1/2] Publishing kdoc-to-json to the local Maven repository..."
(cd "$ROOT_DIR/kdoc-to-json" && ./gradlew publishToMavenLocal)

echo "==> [2/2] Generating JSON documentation for examples/example-data-processor..."
(cd "$ROOT_DIR/examples/example-data-processor" && ./gradlew dokkaGenerate)

OUTPUT_DIR="$ROOT_DIR/examples/example-data-processor/build/dokka/html"
echo
echo "Done. JSON output written to: $OUTPUT_DIR"
