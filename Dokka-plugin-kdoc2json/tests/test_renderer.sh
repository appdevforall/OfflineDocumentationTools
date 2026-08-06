#!/usr/bin/env bash
# Verifies TEST_PLAN.md §6: JsonRenderer traversal & index generation.
#
# Not covered: "Multimodule index.json" -- needs a real multi-module Dokka
# build, deferred to TEST_PLAN.md §8's kotlin-stdlib stress test rather than
# building a second fixture just for this row (per the user's decision when
# step 3 started).
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

HTML_BASELINE_DIR="$ROOT_DIR/examples/html-baseline"

publish_plugin
run_dokka '{ "omitNulls": false, "replaceHtmlExtension": true }'

echo "==> Building stock-Dokka HTML baseline for file-path-parity comparison..." >&2
rm -rf "$HTML_BASELINE_DIR/build/dokka"
if ! (cd "$HTML_BASELINE_DIR" && ./gradlew --console=plain dokkaGenerate) >"$TMP_DIR/html_baseline.log" 2>&1; then
    echo "FATAL: html-baseline dokkaGenerate failed" >&2
    cat "$TMP_DIR/html_baseline.log" >&2
    exit 1
fi

python3 "$TESTS_DIR/helpers/check_renderer.py" "$OUTPUT_DIR" "$HTML_BASELINE_DIR/build/dokka/html"
