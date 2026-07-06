#!/usr/bin/env bash
# Verifies that the .html pages from a default Dokka run and the .json pages from the
# kdoc-to-json plugin are a one-to-one match: every .html file has a same-named .json file
# at the same place in the hierarchy, and every .json file has a same-named .html file.
# Non-page assets in the HTML tree (css/js/images/etc.) are ignored -- only .html and .json
# files are compared, with extensions swapped accordingly.
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <html-output-dir> <json-output-dir>" >&2
    exit 1
fi

HTML_DIR="$(cd "$1" && pwd)"
JSON_DIR="$(cd "$2" && pwd)"

missing_json=0
missing_html=0

echo "==> Checking every .html file in $HTML_DIR has a matching .json file..."
while IFS= read -r -d '' f; do
    rel="${f#"$HTML_DIR"/}"
    if [ ! -f "$JSON_DIR/${rel%.html}.json" ]; then
        echo "  MISSING .json for: $rel"
        missing_json=$((missing_json + 1))
    fi
done < <(find "$HTML_DIR" -type f -name '*.html' -print0)

echo "==> Checking every .json file in $JSON_DIR has a matching .html file..."
while IFS= read -r -d '' f; do
    rel="${f#"$JSON_DIR"/}"
    if [ ! -f "$HTML_DIR/${rel%.json}.html" ]; then
        echo "  MISSING .html for: $rel"
        missing_html=$((missing_html + 1))
    fi
done < <(find "$JSON_DIR" -type f -name '*.json' -print0)

echo
echo "Summary: $missing_json .html file(s) with no matching .json, $missing_html .json file(s) with no matching .html."

if [ "$missing_json" -eq 0 ] && [ "$missing_html" -eq 0 ]; then
    echo "SUCCESS: one-to-one match between HTML and JSON output."
    exit 0
else
    echo "FAILED: structure mismatch between HTML and JSON output."
    exit 1
fi
