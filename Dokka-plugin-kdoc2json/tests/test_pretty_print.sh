#!/usr/bin/env bash
# Verifies the `prettyPrint` config option switches the written JSON between
# compact single-line output (default) and indented multi-line output.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SAMPLE="$OUTPUT_DIR/com.example.utils/-data-processor/index.json"

publish_plugin

echo "=== prettyPrint=false (default): compact single-line JSON ==="
run_dokka '{ "prettyPrint": false }'
lines=$(line_count "$SAMPLE")
assert_eq "$lines" "1" "output file is a single line when compact"

echo "=== prettyPrint=true: indented multi-line JSON ==="
run_dokka '{ "prettyPrint": true }'
lines=$(line_count "$SAMPLE")
assert_gt "$lines" 1 "output file spans multiple lines when pretty-printed ($lines lines)"
assert_contains "$SAMPLE" '    "kind"' "output uses indentation, not just newlines"

summarize_and_exit
