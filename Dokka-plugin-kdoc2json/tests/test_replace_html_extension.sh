#!/usr/bin/env bash
# Verifies the `replaceHtmlExtension` config option: when true, every internal
# relative "url" field in the JSON output should end in .json instead of .html.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SAMPLE="$OUTPUT_DIR/com.example.utils/-data-processor/index.json"

publish_plugin

echo "=== replaceHtmlExtension=true: internal urls should end in .json ==="
run_dokka '{ "replaceHtmlExtension": true }'
assert_contains "$SAMPLE" '"url":"index.json"' "self url rewritten to .json"
assert_no_local_html_urls "$SAMPLE" "no internal .html urls remain (external stdlib links may still end in .html)"

echo "=== replaceHtmlExtension=false (default): internal urls should end in .html ==="
run_dokka '{ "replaceHtmlExtension": false }'
assert_contains "$SAMPLE" '"url":"index.html"' "self url left as .html"
assert_not_contains "$SAMPLE" '.json"' "no .json urls appear when the option is off"

summarize_and_exit
