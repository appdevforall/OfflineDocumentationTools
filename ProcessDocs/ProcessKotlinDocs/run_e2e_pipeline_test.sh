#!/usr/bin/env bash
# End-to-end test of the Kotlin website JSON/DB pipeline (ADFA-4737): convert
# docs + prune blacklist into the database, re-optimize/reinsert media, then
# run the kotlin-stdlib doc sync. Operates on a scratch copy of
# documentation.db so the real database is never touched. Re-run freely; each
# run recopies the source db from scratch.
#
# Before running: fill in every <PLACEHOLDER> value below for your machine.
# The script refuses to start if any are left unfilled or don't exist on disk.
set -euo pipefail

# --- Repo-relative paths - auto-detected, no edits needed ---------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROCESS_DIR="$REPO_ROOT/ProcessDocs/ProcessKotlinDocs/ProcessKotlinWebsiteJSON"
SYNC_SCRIPT="$REPO_ROOT/scripts/sync_kotlin_stdlib_docs/sync_kdoc_json_to_db.py"

# config.json, templates/, and assets/ are staged directly in $PROCESS_DIR
# (this repo) rather than pulled from anyone's local machine - populate_db.py
# looks these up next to its own script location:
#   config.json      - theming config (broken-ext-link-color, menu-no-link-color)
#   templates/*.peb  - page.peb / nav.peb, upserted into the Templates table
#   assets/*         - docs.css / tabs.js / sidebar.js, inserted at assets/<name>
CONFIG_JSON="$PROCESS_DIR/config.json"

# --- Machine-specific paths - fill these in --------------------------------

# DOCS_ROOT: the "docs" subdirectory of a Writerside checkout of the official
# Kotlin website. Get it from https://github.com/JetBrains/kotlin-web-site -
# clone that repo and point this at "<checkout>/kotlin-web-site/docs" (the
# directory directly containing kr.tree, topics/, images/, v.list).
DOCS_ROOT="<PATH_TO_KOTLIN_WEB_SITE_DOCS_CHECKOUT>"

# IMAGES_ZIP: Writerside's own image export for that same docs project (e.g.
# "webHelpImages.zip"). Produced by running IntelliJ IDEA's Writerside plugin
# build/export action against DOCS_ROOT's parent Writerside project; the zip
# is written next to kr.tree once that build finishes.
IMAGES_ZIP="<PATH_TO_WEBHELPIMAGES_ZIP>"

# STDLIB_ALL_LIBS: the "all-libs" output directory of a KDoc-to-JSON Dokka
# plugin run (directly containing kotlin-stdlib/, kotlin-reflect/,
# kotlin-test/ subdirectories of .json files). Generate it by running the
# Dokka plugin in this repo's Dokka-plugin-kdoc2json/ against the Kotlin
# stdlib sources - see that directory's own docs for the exact Gradle task.
# The path typically ends in ".../json/latest/all-libs".
# Check out the most recent changes on
# https://github.com/appdevforall/OfflineDocumentationTools/tree/fix/ADFA-4514
# to produce Kotlin stdlib docs that have source sets whitelisted.
STDLIB_ALL_LIBS="<PATH_TO_KDOC2JSON_ALL_LIBS_OUTPUT_DIR>"

# SOURCE_DB: the runtime "documentation.db" SQLite database this project's
# offline documentation app/server reads from (see docdb-studio/ and
# check-tools/ in this repo for tooling that operates on the same file). Must
# already have its schema populated (Languages, ContentTypes, Templates
# tables) - point this at your own working copy.
SOURCE_DB="<PATH_TO_DOCUMENTATION_DB>"

TEST_DB="$(dirname "$SOURCE_DB")/documentation.test.db"

JPEG_QUALITY=85
WEBP_QUALITY=90

# Full toc-title path (top-level -> ... -> target), joined with "\/" per
# populate_db.py's --blacklisted-element-titles convention. These are the
# concrete cases named in ADFA-4737; add more "path" entries here to prune
# additional sections. Re-derive these from your own DOCS_ROOT/kr.tree if the
# site's navigation structure has changed since this was written.
BLACKLIST=(
  'Development\/Web development'
  'Interoperability\/Swift/Objective-C and C interop'
  'Interoperability\/JavaScript interop'
)

# --- Fail fast on unfilled placeholders or missing paths -------------------
require_path() {
  local name="$1" value="$2"
  if [[ "$value" == "<"*">" ]]; then
    echo "error: $name is still a placeholder ('$value') - edit this script and fill in your local path." >&2
    exit 1
  fi
  if [[ ! -e "$value" ]]; then
    echo "error: $name points to '$value', which does not exist." >&2
    exit 1
  fi
}
require_path DOCS_ROOT "$DOCS_ROOT"
require_path IMAGES_ZIP "$IMAGES_ZIP"
require_path STDLIB_ALL_LIBS "$STDLIB_ALL_LIBS"
require_path SOURCE_DB "$SOURCE_DB"

WORKDIR="$(mktemp -d /tmp/adfa4737-e2e.XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

echo "== Copying $SOURCE_DB -> $TEST_DB =="
rm -f "$TEST_DB"
cp "$SOURCE_DB" "$TEST_DB"

echo
echo "== Step 1/4: find_missing_assets.py (source QA report) =="
REPORT_PATH="$WORKDIR/missing-assets-report.md"
python3 "$PROCESS_DIR/find_missing_assets.py" "$DOCS_ROOT" "$REPORT_PATH"
echo "Report written to $REPORT_PATH"

echo
echo "== Step 2/4: populate_db.py (convert docs, prune blacklist, insert into test db) =="
( cd "$PROCESS_DIR" && python3 populate_db.py "$DOCS_ROOT" "$CONFIG_JSON" "$IMAGES_ZIP" "$TEST_DB" \
    --blacklisted-element-titles "${BLACKLIST[@]}" )

echo
echo "== Step 3/4: insert_optimized_media.py (re-optimize + reinsert k/html/images/*) =="
# --webp requires an "image/webp" ContentTypes row, which this database
# doesn't ship with (see insert_optimized_media.py's own module docstring) -
# add it (idempotent) before running.
sqlite3 "$TEST_DB" "INSERT OR IGNORE INTO ContentTypes (value, compression) VALUES ('image/webp', 'brotli');"

# insert_optimized_media.py addresses images by bare filename, matching
# populate_db.py's own flat k/html/images/<name> convention - so its input
# media_dir needs to be a directory of files with those same basenames.
# The images actually inserted above came from IMAGES_ZIP, so extract that
# same zip here rather than pointing at DOCS_ROOT/images (the raw, unoptimized
# Writerside source tree - a different, much larger set of files).
MEDIA_DIR="$WORKDIR/media"
mkdir -p "$MEDIA_DIR"
unzip -q "$IMAGES_ZIP" -d "$MEDIA_DIR"

python3 "$PROCESS_DIR/insert_optimized_media.py" "$MEDIA_DIR" "$TEST_DB" \
    --jpeg-quality "$JPEG_QUALITY" --webp --webp-quality "$WEBP_QUALITY" --verbose

echo
echo "== Step 4/4: sync_kdoc_json_to_db.py (overwrite kotlin-stdlib/-reflect/-test content) =="
python3 "$SYNC_SCRIPT" "$STDLIB_ALL_LIBS" --db "$TEST_DB"

echo
echo "== Summary =="
python3 - "$TEST_DB" <<'PYEOF'
import sqlite3
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(db_path)


def count(where, params=()):
    return conn.execute(f"SELECT count(*) FROM Content WHERE {where}", params).fetchone()[0]


print(f"Database: {db_path}")
print(f"  k/html/*        rows: {count('path LIKE ?', ('k/html/%',))}")
print(f"  k/html/images/* rows: {count('path LIKE ?', ('k/html/images/%',))}")
print(f"  k/html/images/*.webp rows: {count('path LIKE ?', ('k/html/images/%.webp%',))}")
print(f"  k/kotlin-stdlib/*  rows: {count('path LIKE ? OR path = ?', ('k/kotlin-stdlib/%', 'k/kotlin-stdlib'))}")
print(f"  k/kotlin-reflect/* rows: {count('path LIKE ? OR path = ?', ('k/kotlin-reflect/%', 'k/kotlin-reflect'))}")
print(f"  k/kotlin-test/*    rows: {count('path LIKE ? OR path = ?', ('k/kotlin-test/%', 'k/kotlin-test'))}")

wasm_rows = conn.execute(
    "SELECT path FROM Content WHERE path LIKE 'k/html/wasm%'"
).fetchall()
print(f"  k/html/wasm* rows remaining (should be 0 after pruning): {len(wasm_rows)}")
for (path,) in wasm_rows[:10]:
    print(f"    {path}")

conn.close()
PYEOF

echo
echo "Done. Backups (populate_db.py, insert_optimized_media.py, and sync_kdoc_json_to_db.py"
echo "each make their own) live alongside $TEST_DB as documentation.test.db.backup-* and"
echo "documentation.test.db.bak.*"
echo "The real database at $SOURCE_DB was never opened for writing."
