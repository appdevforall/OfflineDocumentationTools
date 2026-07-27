#!/usr/bin/env bash
# Verifies the `sourceSetWhitelist` config option: documentables outside the
# whitelist are omitted from the output entirely, and a whitelist that matches
# every source set behaves like no filtering at all. Reuses
# scripts/verify_sourceset_whitelist.py for the "matches" case, since that's
# exactly the tool it exists to exercise.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# example-data-processor is a single-source-set (JVM-only) project; Dokka names
# that source set "main".
ALL_TYPES="$OUTPUT_DIR/all-types.json"
PACKAGE_LIST="$OUTPUT_DIR/package-list"

publish_plugin

echo "=== sourceSetWhitelist=[] (default): no filtering, all-types.json present ==="
run_dokka '{ "sourceSetWhitelist": [] }'
assert_file_exists "$ALL_TYPES" "all-types.json generated with no whitelist"
assert_contains "$ALL_TYPES" '"name":"DataProcessor"' "DataProcessor present with no whitelist"

echo "=== sourceSetWhitelist=[\"main\"] (matches the project's only source set): nothing filtered ==="
run_dokka '{ "sourceSetWhitelist": ["main"] }'
assert_file_exists "$ALL_TYPES" "all-types.json generated when whitelist matches"
assert_contains "$ALL_TYPES" '"name":"DataProcessor"' "DataProcessor present when whitelist matches"
if python3 "$ROOT_DIR/scripts/verify_sourceset_whitelist.py" "$OUTPUT_DIR" main >"$TMP_DIR/verify.log" 2>&1; then
    pass "verify_sourceset_whitelist.py confirms no violations"
else
    fail "verify_sourceset_whitelist.py reported violations: $(cat "$TMP_DIR/verify.log")"
fi

echo "=== sourceSetWhitelist=[\"does-not-exist\"] (matches nothing): everything omitted ==="
run_dokka '{ "sourceSetWhitelist": ["does-not-exist"] }'
assert_file_not_exists "$ALL_TYPES" "all-types.json not generated when nothing passes the whitelist"
assert_file_exists "$PACKAGE_LIST" "package-list header is still written"
assert_not_contains "$PACKAGE_LIST" 'com.example' "package-list has no packages when nothing passes the whitelist"

summarize_and_exit
