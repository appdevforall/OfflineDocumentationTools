#!/usr/bin/env bash
# Throwaway helper for reviewers of ADFA-5039 - clones kotlin-web-site and
# runs md_to_json.py against it via `uv run` so you can look at real JSON
# output without any other setup. Not part of the actual pipeline (that's
# ADFA-4739, a separate ticket/PR).
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required - see https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WORKDIR="$(mktemp -d)"

echo "== Cloning kotlin-web-site into $WORKDIR/kotlin-web-site =="
git clone --depth 1 https://github.com/JetBrains/kotlin-web-site.git "$WORKDIR/kotlin-web-site"

echo "== Running md_to_json.py =="
uv run --with-requirements "$REPO_ROOT/requirements.txt" \
  "$SCRIPT_DIR/md_to_json.py" \
  "$WORKDIR/kotlin-web-site/docs" \
  "$WORKDIR/json-output" \
  "$SCRIPT_DIR/config.json"

echo
echo "Done. JSON output at $WORKDIR/json-output"
