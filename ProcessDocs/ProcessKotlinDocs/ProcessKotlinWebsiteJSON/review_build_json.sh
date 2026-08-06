#!/usr/bin/env bash
# Throwaway helper for reviewers of ADFA-5039 - installs requirements, clones
# kotlin-web-site, and runs md_to_json.py against it so you can look at real
# JSON output without any other setup. Not part of the actual pipeline
# (that's ADFA-4739, a separate ticket/PR).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d)"

echo "== Installing requirements =="
python3 -m pip install markdown-it-py

echo "== Cloning kotlin-web-site into $WORKDIR/kotlin-web-site =="
git clone --depth 1 https://github.com/JetBrains/kotlin-web-site.git "$WORKDIR/kotlin-web-site"

echo "== Running md_to_json.py =="
python3 "$SCRIPT_DIR/md_to_json.py" \
  "$WORKDIR/kotlin-web-site/docs" \
  "$WORKDIR/json-output" \
  "$SCRIPT_DIR/config.json"

echo
echo "Done. JSON output at $WORKDIR/json-output"
