#!/usr/bin/env bash
# Verifies the `omitNulls` config option deeply strips null values, empty
# strings, empty arrays, and empty objects from the output.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# Matches a JSON value of null, "", [], or {} -- what omitNulls should remove.
# Compact output has no space after the colon while prettyPrint output does, so
# the whitespace after ":" is optional here to match either style.
NULL_OR_EMPTY_PATTERN=':[[:space:]]*(null|""|\[\]|\{\})'

publish_plugin

echo "=== omitNulls=false (default): at least one null/empty value exists somewhere ==="
run_dokka '{ "omitNulls": false }'
hits=$(grep -rlE "$NULL_OR_EMPTY_PATTERN" "$OUTPUT_DIR" --include='*.json' 2>/dev/null | wc -l | tr -d ' ')
assert_gt "$hits" 0 "found null/empty values with omitNulls=false ($hits file(s))"

echo "=== omitNulls=true: no null/empty values remain anywhere in the output ==="
run_dokka '{ "omitNulls": true }'
hits=$(grep -rlE "$NULL_OR_EMPTY_PATTERN" "$OUTPUT_DIR" --include='*.json' 2>/dev/null | wc -l | tr -d ' ')
assert_eq "$hits" "0" "no null/empty values found with omitNulls=true"

summarize_and_exit
