#!/usr/bin/env bash
# Verifies the `logFile` config option: when set, the plugin's log messages are
# written to that path; when unset (the default), no such file is created.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

publish_plugin

echo "=== logFile set to a custom path: file is created and contains plugin log lines ==="
LOG_PATH="$(unique_tmp_path log_file_set)"
run_dokka "{ \"logFile\": \"$LOG_PATH\" }"
assert_file_exists "$LOG_PATH" "custom log file was created"
assert_contains "$LOG_PATH" 'JSON Plugin' "log file contains plugin log lines"

echo "=== logFile unset (default): no log file appears at that same path ==="
rm -f "$LOG_PATH"
run_dokka '{}'
assert_file_not_exists "$LOG_PATH" "no log file written when logFile is not configured"

summarize_and_exit
