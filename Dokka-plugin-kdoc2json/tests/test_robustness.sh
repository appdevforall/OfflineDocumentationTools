#!/usr/bin/env bash
# Verifies TEST_PLAN.md §9: robustness / edge cases.
#
# The "malformed plugin config" row turned out to only be partially true --
# see the two scenarios below marked "expected to fail". Dokka's own Gradle
# plugin deserializes the raw jsonEncode() string directly against
# JsonPluginConfig (via Jackson, client-side) before our worker/renderer code
# ever runs. That means:
#   - valid JSON with only an unknown extra key survives (both Dokka's own
#     decoder and our manual ignoreUnknownKeys fallback tolerate it), but
#   - genuinely invalid JSON syntax, and valid JSON with a wrong-typed known
#     field, both fail the WHOLE GRADLE BUILD at that upfront layer -- never
#     reaching the try/catch in JsonRenderer.render() at all.
# There's nothing in this plugin's own code to fix for those last two: the
# failure happens one layer above it, in Dokka's Gradle plugin itself.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SAMPLE="$OUTPUT_DIR/com.example.testlib/-bounded-container/index.json"

publish_plugin

echo "=== Unknown extra config key: build succeeds, known keys still applied ==="
run_dokka '{ "omitNulls": true, "totallyUnknownKey": "whatever" }'
if grep -qE ':[[:space:]]*(null|""|\[\]|\{\})' "$SAMPLE"; then
    fail "omitNulls=true is still applied when an unrelated unknown key is present"
else
    pass "omitNulls=true is still applied when an unrelated unknown key is present"
fi

echo "=== Genuinely malformed JSON syntax: build fails (expected -- see header comment) ==="
run_dokka_expect_failure '{ this is not valid json at all !!! '
assert_eq "$LAST_EXIT_CODE" "1" "build fails on syntactically invalid config JSON"
assert_contains "$LAST_GRADLE_LOG" "Unexpected character" "failure is a JSON parse error, not an unrelated crash"

echo "=== Wrong-typed known field: build fails (expected -- see header comment) ==="
run_dokka_expect_failure '{ "omitNulls": "not-a-boolean" }'
assert_eq "$LAST_EXIT_CODE" "1" "build fails when a known field has the wrong JSON type"
assert_contains "$LAST_GRADLE_LOG" "JsonPluginConfig[\"omitNulls\"]" "failure clearly names the offending field"

echo "=== No plugin config registered at all: build succeeds using JsonPluginConfig() defaults ==="
run_dokka_no_plugin_config
assert_contains "$LAST_GRADLE_LOG" "No JSON config found in pluginsConfiguration" "the fallback-to-defaults warning is logged"
lines=$(line_count "$SAMPLE")
assert_eq "$lines" "1" "output is compact (prettyPrint default false) when no config is registered"
assert_contains "$SAMPLE" '"url":"index.html"' "urls are left as .html (replaceHtmlExtension default false) when no config is registered"

echo "=== Empty/near-empty module (package with zero public declarations): build succeeds ==="
run_dokka '{ "omitNulls": false, "replaceHtmlExtension": true }'
assert_file_exists "$OUTPUT_DIR/package-list" "package-list is still written"
assert_not_contains "$OUTPUT_DIR/package-list" "com.example.emptypkg" "the internal-only package is cleanly skipped (Dokka's own skipEmptyPackages), not left half-written"
assert_file_exists "$OUTPUT_DIR/index.json" "root module index.json is still written"
if python3 -c "
import json, sys
d = json.load(open('$OUTPUT_DIR/index.json'))
sys.exit(0 if d.get('kind') == 'module' and isinstance(d.get('packages'), list) else 1)
"; then
    pass "root module index.json is still valid JSON with the expected shape"
else
    fail "root module index.json is still valid JSON with the expected shape"
fi

echo "=== classDiscriminator collision (set to an existing field name): build fails with a clear error ==="
run_dokka_expect_failure '{ "classDiscriminator": "name" }'
assert_eq "$LAST_EXIT_CODE" "1" "build fails when classDiscriminator collides with a real field name"
assert_contains "$LAST_GRADLE_LOG" "conflicts with JSON class discriminator" "the error clearly names the discriminator collision, not silent corruption"

summarize_and_exit
