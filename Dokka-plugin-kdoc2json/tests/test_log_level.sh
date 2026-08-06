#!/usr/bin/env bash
# Verifies the `logLevel` config option filters which severities get written to
# the plugin's log file: "debug" writes everything, "info" suppresses debug
# lines, and "error" (with no actual errors in a clean build) writes nothing at
# all -- exercising every rung of PluginLogger's level filter.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

publish_plugin

echo "=== logLevel=debug: DEBUG lines are written ==="
LOG_DEBUG="$(unique_tmp_path log_debug)"
run_dokka "{ \"logLevel\": \"debug\", \"logFile\": \"$LOG_DEBUG\" }"
assert_contains "$LOG_DEBUG" 'JSON Plugin DEBUG' "DEBUG lines present at logLevel=debug"

echo "=== logLevel=info: DEBUG lines suppressed, INFO lines still present ==="
LOG_INFO="$(unique_tmp_path log_info)"
run_dokka "{ \"logLevel\": \"info\", \"logFile\": \"$LOG_INFO\" }"
assert_not_contains "$LOG_INFO" 'JSON Plugin DEBUG' "DEBUG lines suppressed at logLevel=info"
assert_contains "$LOG_INFO" 'JSON Plugin INFO' "INFO lines present at logLevel=info"

echo "=== logLevel=error: no PluginLogger output at all on a clean, error-free build ==="
LOG_ERROR="$(unique_tmp_path log_error)"
run_dokka "{ \"logLevel\": \"error\", \"logFile\": \"$LOG_ERROR\" }"
assert_file_not_exists "$LOG_ERROR" "no log file written at logLevel=error when nothing errors"

summarize_and_exit
