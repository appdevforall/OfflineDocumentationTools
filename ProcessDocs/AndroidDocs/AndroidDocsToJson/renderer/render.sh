#!/usr/bin/env bash
# Renders a JSON documentation tree written by android_docs_to_json.py to browsable HTML.
#
#   ./render.sh <json-dir> <html-dir>
#
# Builds the renderer if needed, then walks the JSON tree writing one .html per .json at the same
# relative path. Because the trees mirror each other file-for-file, the relative links already in
# the JSON resolve as soon as their .json extension is swapped for .html, which is what the
# templates' `href` and `doc` filters do.
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <json-dir> <html-dir>" >&2
    echo >&2
    echo "Example, after a full extraction:" >&2
    echo "  ../android_docs_to_json.py ../../android ../../androidx --out /tmp/android-json" >&2
    echo "  $0 /tmp/android-json /tmp/android-html" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JSON_DIR="$(cd "$1" && pwd)"
mkdir -p "$2"
HTML_DIR="$(cd "$2" && pwd)"

echo "==> Building the renderer"
(cd "$SCRIPT_DIR" && ./gradlew --console=plain -q installDist)

echo "==> Rendering $JSON_DIR -> $HTML_DIR"
"$SCRIPT_DIR/build/install/android-doc-renderer/bin/android-doc-renderer" "$JSON_DIR" "$HTML_DIR"

echo
echo "Open it with:"
echo "  (cd \"$HTML_DIR\" && python3 -m http.server 8000)"
echo "  then browse http://localhost:8000/index.html"
