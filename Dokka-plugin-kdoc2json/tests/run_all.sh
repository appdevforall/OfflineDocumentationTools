#!/usr/bin/env bash
# Runs every config-option test in this directory against
# examples/example-data-processor and prints an overall summary.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$TESTS_DIR/lib.sh"

publish_plugin
export KDOC2JSON_SKIP_PUBLISH=1

overall_pass=0
overall_fail=0
failed_scripts=()

for test_script in "$TESTS_DIR"/test_*.sh; do
    name="$(basename "$test_script")"
    echo
    echo "########## $name ##########"
    if "$test_script"; then
        overall_pass=$((overall_pass + 1))
    else
        overall_fail=$((overall_fail + 1))
        failed_scripts+=("$name")
    fi
done

echo
echo "================================================"
echo "Test scripts: $overall_pass passed, $overall_fail failed"
if [[ "$overall_fail" -gt 0 ]]; then
    echo "Failed: ${failed_scripts[*]}"
    exit 1
fi
exit 0
