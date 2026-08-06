#!/usr/bin/env bash
# Verifies the `omitFields` config option strips exactly the listed JSON keys
# (recursively) from the output, and leaves other fields untouched.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SAMPLE="$OUTPUT_DIR/com.example.utils/-data-processor/index.json"

publish_plugin

echo "=== omitFields=[] (default): sources and documentation are present ==="
run_dokka '{ "omitFields": [] }'
assert_contains "$SAMPLE" '"sources"' "sources field present by default"
assert_contains "$SAMPLE" '"documentation"' "documentation field present by default"

echo "=== omitFields=[\"sources\", \"documentation\"]: both keys stripped everywhere ==="
run_dokka '{ "omitFields": ["sources", "documentation"] }'
assert_not_contains "$SAMPLE" '"sources"' "sources field stripped"
assert_not_contains "$SAMPLE" '"documentation"' "documentation field stripped"
assert_contains "$SAMPLE" '"name":"DataProcessor"' "unrelated fields (e.g. name) are left untouched"

echo "=== omitFields=[\"sources\"]: only the named field is stripped ==="
run_dokka '{ "omitFields": ["sources"] }'
assert_not_contains "$SAMPLE" '"sources"' "sources field stripped"
assert_contains "$SAMPLE" '"documentation"' "documentation field NOT stripped when not listed"

summarize_and_exit
