#!/usr/bin/env bash
# Shared harness for the kdoc-to-json config-option tests. Each test_*.sh script
# sources this file, calls publish_plugin once, then calls run_dokka one or more
# times with a JSON config string to regenerate examples/example-data-processor's
# docs under that config and assert on the resulting output.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$TESTS_DIR/.." && pwd)"
PLUGIN_DIR="$ROOT_DIR/kdoc-to-json"
EXAMPLE_DIR="$ROOT_DIR/examples/example-data-processor"
OUTPUT_DIR="$EXAMPLE_DIR/build/dokka/html"
# Javadoc mode mirrors the output of the `javadoc` tool, so it is exercised against a
# Java-only example rather than the Kotlin one every other test uses.
JAVA_EXAMPLE_DIR="$ROOT_DIR/examples/example-java-library"
JAVA_OUTPUT_DIR="$JAVA_EXAMPLE_DIR/build/dokka/html"

TMP_DIR="$(mktemp -d /tmp/kdoc2json_test.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0

# Publishes the plugin under test to mavenLocal so the example project can pick it
# up. Skipped when run_all.sh has already done this for the whole suite.
publish_plugin() {
    if [[ "${KDOC2JSON_SKIP_PUBLISH:-}" == "1" ]]; then
        return 0
    fi
    local log="$TMP_DIR/publish.log"
    echo "==> Publishing kdoc-to-json to mavenLocal..." >&2
    if ! (cd "$PLUGIN_DIR" && ./gradlew --console=plain publishToMavenLocal) >"$log" 2>&1; then
        echo "FATAL: failed to publish kdoc-to-json to mavenLocal" >&2
        cat "$log" >&2
        exit 1
    fi
}

# run_dokka '<json config>' regenerates examples/example-data-processor's docs with
# the given plugin config. Wipes any previous output first, so afterwards
# $OUTPUT_DIR reflects only this run. Aborts the whole test script (not just the
# current assertion) if the Dokka build itself fails, since that means the harness
# is broken rather than the config option under test.
# After a successful call, LAST_GRADLE_LOG holds the path to that run's captured
# Gradle output -- useful for tests asserting on build-log content (e.g. the
# "Failed to resolve N DRIs" warning), not just the written JSON files.
LAST_GRADLE_LOG=""

run_dokka() {
    local config_json="$1"
    local config_file="$TMP_DIR/config-$RANDOM.json"
    local gradle_log="$TMP_DIR/gradle-$RANDOM.log"
    printf '%s' "$config_json" >"$config_file"

    rm -rf "$EXAMPLE_DIR/build/dokka"

    if ! (cd "$EXAMPLE_DIR" && KDOC2JSON_TEST_CONFIG="$config_file" ./gradlew --console=plain dokkaGenerate) >"$gradle_log" 2>&1; then
        echo "FATAL: dokkaGenerate failed for config: $config_json" >&2
        cat "$gradle_log" >&2
        exit 1
    fi
    LAST_GRADLE_LOG="$gradle_log"
}

# run_dokka_java '<json config>' is run_dokka against examples/example-java-library, the
# Java-source example Javadoc mode is tested with. Afterwards $JAVA_OUTPUT_DIR reflects only
# this run. Aborts the whole script if the Dokka build itself fails, like run_dokka.
run_dokka_java() {
    local config_json="$1"
    local config_file="$TMP_DIR/config-java-$RANDOM.json"
    local gradle_log="$TMP_DIR/gradle-java-$RANDOM.log"
    printf '%s' "$config_json" >"$config_file"

    rm -rf "$JAVA_EXAMPLE_DIR/build/dokka"

    if ! (cd "$JAVA_EXAMPLE_DIR" && KDOC2JSON_TEST_CONFIG="$config_file" ./gradlew --console=plain dokkaGenerate) >"$gradle_log" 2>&1; then
        echo "FATAL: dokkaGenerate failed for config: $config_json" >&2
        cat "$gradle_log" >&2
        exit 1
    fi
    LAST_GRADLE_LOG="$gradle_log"
}

# run_dokka_expect_failure '<json config>' is run_dokka's counterpart for tests
# that assert the build SHOULD fail (e.g. a genuinely malformed config, or a
# classDiscriminator collision). Never aborts the script on a Dokka failure --
# instead records the outcome in LAST_EXIT_CODE (0 or 1) and the log path in
# LAST_GRADLE_LOG, leaving the assertion itself to the caller.
LAST_EXIT_CODE=""

run_dokka_expect_failure() {
    local config_json="$1"
    local config_file="$TMP_DIR/config-$RANDOM.json"
    local gradle_log="$TMP_DIR/gradle-$RANDOM.log"
    printf '%s' "$config_json" >"$config_file"

    rm -rf "$EXAMPLE_DIR/build/dokka"

    if (cd "$EXAMPLE_DIR" && KDOC2JSON_TEST_CONFIG="$config_file" ./gradlew --console=plain dokkaGenerate) >"$gradle_log" 2>&1; then
        LAST_EXIT_CODE=0
    else
        LAST_EXIT_CODE=1
    fi
    LAST_GRADLE_LOG="$gradle_log"
}

# run_dokka_no_plugin_config runs dokkaGenerate with KDOC2JSON_NO_PLUGIN_CONFIG=1,
# which build.gradle.kts uses to skip registering any pluginsConfiguration entry
# at all -- distinct from an empty/malformed one, which still registers
# *something*. Aborts the script on failure, like run_dokka.
run_dokka_no_plugin_config() {
    local gradle_log="$TMP_DIR/gradle-$RANDOM.log"

    rm -rf "$EXAMPLE_DIR/build/dokka"

    if ! (cd "$EXAMPLE_DIR" && KDOC2JSON_NO_PLUGIN_CONFIG=1 ./gradlew --console=plain dokkaGenerate) >"$gradle_log" 2>&1; then
        echo "FATAL: dokkaGenerate failed with no plugin config registered" >&2
        cat "$gradle_log" >&2
        exit 1
    fi
    LAST_GRADLE_LOG="$gradle_log"
}

# Returns a path inside the test's tmp dir that does not yet exist, suitable for
# passing as a `logFile` config value when the assertion cares whether the plugin
# itself creates the file.
unique_tmp_path() {
    mktemp -u "$TMP_DIR/$1.XXXXXX"
}

pass() {
    echo "  PASS: $1"
    PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
    echo "  FAIL: $1"
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

assert_file_exists() {
    local path="$1" desc="$2"
    if [[ -f "$path" ]]; then
        pass "$desc"
    else
        fail "$desc (file not found: $path)"
    fi
}

assert_file_not_exists() {
    local path="$1" desc="$2"
    if [[ ! -f "$path" ]]; then
        pass "$desc"
    else
        fail "$desc (file unexpectedly exists: $path)"
    fi
}

assert_contains() {
    local path="$1" pattern="$2" desc="$3"
    if grep -qF -- "$pattern" "$path" 2>/dev/null; then
        pass "$desc"
    else
        fail "$desc (pattern not found: '$pattern' in $path)"
    fi
}

assert_not_contains() {
    local path="$1" pattern="$2" desc="$3"
    if ! grep -qF -- "$pattern" "$path" 2>/dev/null; then
        pass "$desc"
    else
        fail "$desc (pattern unexpectedly found: '$pattern' in $path)"
    fi
}

assert_eq() {
    local actual="$1" expected="$2" desc="$3"
    if [[ "$actual" == "$expected" ]]; then
        pass "$desc"
    else
        fail "$desc (expected '$expected', got '$actual')"
    fi
}

assert_no_local_html_urls() {
    local path="$1" desc="$2"
    local hits
    hits=$(grep -oE '"url":"[^"]*\.html"' "$path" 2>/dev/null | grep -v '"url":"http' || true)
    if [[ -z "$hits" ]]; then
        pass "$desc"
    else
        fail "$desc (found: $(echo "$hits" | head -3 | tr '\n' ' '))"
    fi
}

# assert_json <file> <python-expression> <expected> <desc> evaluates a Python expression
# against the parsed JSON document (bound to `d`) and compares its str() to <expected>. Lets a
# test assert on structure -- a field's value, a list's contents -- instead of grepping for a
# substring that might match somewhere unrelated in the file.
assert_json() {
    local path="$1" expr="$2" expected="$3" desc="$4"
    local actual
    actual=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print('<unreadable: %s>' % e)
    sys.exit(0)
try:
    print($expr)
except Exception as e:
    print('<error: %s>' % e)
" "$path" 2>/dev/null)
    if [[ "$actual" == "$expected" ]]; then
        pass "$desc"
    else
        fail "$desc (expected '$expected', got '$actual')"
    fi
}

assert_gt() {
    local actual="$1" threshold="$2" desc="$3"
    if [[ "$actual" -gt "$threshold" ]]; then
        pass "$desc"
    else
        fail "$desc (expected > $threshold, got $actual)"
    fi
}

# Counts lines in a file, correctly handling a final line with no trailing newline
# (which `wc -l` would otherwise undercount) -- needed to distinguish compact
# single-line JSON from prettyPrint's indented multi-line output.
line_count() {
    awk 'END { print NR }' "$1" 2>/dev/null || echo 0
}

summarize_and_exit() {
    echo
    echo "$(basename "$0"): $PASS_COUNT passed, $FAIL_COUNT failed"
    if [[ "$FAIL_COUNT" -gt 0 ]]; then
        exit 1
    fi
    exit 0
}
