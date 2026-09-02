#!/usr/bin/env bash
# End-to-end test of the Kotlin website JSON/DB pipeline (ADFA-4737): convert
# docs + prune blacklist into the database, re-optimize/reinsert media,
# generate fresh kotlin-stdlib/-reflect/-test JSON docs via a freshly-built
# kdoc-to-json plugin, then sync them into the database. Operates on a
# scratch copy of documentation.db so the real database is never touched.
# Re-run freely; each run recopies the source db from scratch.
#
# Before running: fill in every <PLACEHOLDER> value below for your machine.
# The script refuses to start if any are left unfilled or don't exist on disk.
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required - see https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# --- Repo-relative paths - auto-detected, no edits needed ---------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROCESS_DIR="$REPO_ROOT/ProcessDocs/ProcessKotlinDocs/ProcessKotlinWebsiteJSON"
SYNC_SCRIPT="$REPO_ROOT/scripts/sync_kotlin_stdlib_docs/sync_kdoc_json_to_db.py"
UV_RUN=(uv run --with-requirements "$REPO_ROOT/requirements.txt")

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

# STDLIB_DOCS_DIR: the "libraries/tools/kotlin-stdlib-docs" directory inside
# a full clone of https://github.com/JetBrains/kotlin (not the kotlin repo
# root itself - this exact subdirectory). Step 4 below builds a fresh copy
# of the kdoc-to-json plugin from this repo's Dokka-plugin-kdoc2json/ and
# runs it against this checkout to produce kotlin-stdlib/-reflect/-test JSON
# docs (common + jvm source sets only) - no separate manual doc-generation
# step needed.
STDLIB_DOCS_DIR="<PATH_TO_KOTLIN_STDLIB_DOCS_DIR>"

# SOURCE_DB: the runtime "documentation.db" SQLite database this project's
# offline documentation app/server reads from (see docdb-studio/ and
# check-tools/ in this repo for tooling that operates on the same file). Must
# already have its schema populated (Languages, ContentTypes, Templates
# tables) - point this at your own working copy.
SOURCE_DB="<PATH_TO_DOCUMENTATION_DB>"

TEST_DB="$(dirname "$SOURCE_DB")/documentation.test.db"

JPEG_QUALITY=85
WEBP_QUALITY=90

# Which published kotlin-stdlib/-reflect/-test artifacts step 4/5 documents.
# kotlin_big (inside kotlin-stdlib-docs) extracts the real binaries from a
# Maven repo; left unset it looks for "<kotlin-root>/build/repo" at the
# checkout's own defaultSnapshotVersion, i.e. artifacts that only exist if you
# have built the whole kotlin repo locally. Naming a released version instead
# resolves them straight from Maven Central. Keep it in step with the ref your
# STDLIB_DOCS_DIR checkout is on; set empty to use the checkout's own default.
KOTLIN_LIBS_VERSION=2.4.10
# Maven repo to resolve them from. Empty is fine for a released
# KOTLIN_LIBS_VERSION (kotlin_big already declares mavenCentral()); set this
# only to point at a private or snapshot repository.
KOTLIN_LIBS_REPO=""

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
require_path STDLIB_DOCS_DIR "$STDLIB_DOCS_DIR"
require_path SOURCE_DB "$SOURCE_DB"

WORKDIR="$(mktemp -d /tmp/adfa4737-e2e.XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

echo "== Copying $SOURCE_DB -> $TEST_DB =="
rm -f "$TEST_DB"
cp "$SOURCE_DB" "$TEST_DB"

echo
echo "== Step 1/5: find_missing_assets.py (source QA report) =="
REPORT_PATH="$WORKDIR/missing-assets-report.md"
"${UV_RUN[@]}" "$PROCESS_DIR/find_missing_assets.py" "$DOCS_ROOT" "$REPORT_PATH"
echo "Report written to $REPORT_PATH"

echo
echo "== Step 2/5: populate_db.py (convert docs, prune blacklist, insert into test db) =="
( cd "$PROCESS_DIR" && "${UV_RUN[@]}" populate_db.py "$DOCS_ROOT" "$CONFIG_JSON" "$IMAGES_ZIP" "$TEST_DB" \
    --blacklisted-element-titles "${BLACKLIST[@]}" )

echo
echo "== Step 3/5: insert_optimized_media.py (re-optimize + reinsert k/html/images/*) =="
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

"${UV_RUN[@]}" "$PROCESS_DIR/insert_optimized_media.py" "$MEDIA_DIR" "$TEST_DB" \
    --jpeg-quality "$JPEG_QUALITY" --webp --webp-quality "$WEBP_QUALITY" --verbose

echo
echo "== Step 4/5: build-stdlib-json-docs.sh (fresh plugin build -> kotlin-stdlib/-reflect/-test JSON) =="
# STDLIB_DOCS_DIR is .../kotlin/libraries/tools/kotlin-stdlib-docs; the
# kotlin repo root (needed to locate gradle/libs.versions.toml and to
# resolve kotlin_root inside the injected build.gradle.kts) is exactly three
# levels up, matching that build.gradle.kts's own "../../../" convention.
KOTLIN_ROOT="$(cd "$STDLIB_DOCS_DIR/../../.." && pwd)"
STDLIB_ARGS=()
[ -n "$KOTLIN_LIBS_VERSION" ] && STDLIB_ARGS+=(--kotlin-libs-version "$KOTLIN_LIBS_VERSION")
[ -n "$KOTLIN_LIBS_REPO" ] && STDLIB_ARGS+=(--kotlin-libs-repo "$KOTLIN_LIBS_REPO")
STDLIB_ALL_LIBS="$("$REPO_ROOT/Dokka-plugin-kdoc2json/scripts/kotlin/build-stdlib-json-docs.sh" \
    ${STDLIB_ARGS[@]+"${STDLIB_ARGS[@]}"} "$KOTLIN_ROOT" "$WORKDIR/stdlib-json")"
echo "Generated JSON docs at $STDLIB_ALL_LIBS"

echo
echo "== Step 5/5: sync_kdoc_json_to_db.py (overwrite kotlin-stdlib/-reflect/-test content) =="
"${UV_RUN[@]}" "$SYNC_SCRIPT" "$STDLIB_ALL_LIBS" --db "$TEST_DB"

echo
echo "== Summary =="
"${UV_RUN[@]}" python3 - "$TEST_DB" <<'PYEOF'
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

conn.close()
PYEOF

echo
echo "== Blacklist pruning verification =="
# Recomputes, from the same kr.tree and BLACKLIST used above, exactly which
# topic stems populate_db.py's own prune_blacklisted_elements() decided to
# exclude - then confirms none of those pages made it into the database.
# Reusing that real pruning logic (rather than guessing at path patterns)
# means this check stays correct if BLACKLIST or kr.tree's structure change.
"${UV_RUN[@]}" python3 - "$PROCESS_DIR" "$DOCS_ROOT" "$TEST_DB" "${BLACKLIST[@]}" <<'PYEOF'
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

process_dir, docs_root, db_path, *blacklist_raw = sys.argv[1:]
sys.path.insert(0, process_dir)
import populate_db  # noqa: E402

root = ET.parse(Path(docs_root) / "kr.tree").getroot()
blacklisted_paths = {populate_db.parse_blacklist_path(raw) for raw in blacklist_raw}
blacklisted_stems, unmatched_paths = populate_db.prune_blacklisted_elements(root, blacklisted_paths)

conn = sqlite3.connect(db_path)
leftover = []
for stem in sorted(blacklisted_stems):
    path = f"k/html/{stem}.html"
    if conn.execute("SELECT 1 FROM Content WHERE path = ?", (path,)).fetchone():
        leftover.append(path)
conn.close()

print(f"Blacklisted toc-element path(s) checked: {len(blacklisted_paths)}")
for path in sorted(blacklisted_paths):
    status = "unmatched (no such element in kr.tree)" if path in unmatched_paths else "matched"
    print(f"  {' > '.join(path)}: {status}")
print(f"Topic page(s) expected removed: {len(blacklisted_stems)}")

if unmatched_paths:
    print(f"FAIL: {len(unmatched_paths)} blacklist path(s) never matched a <toc-element> - "
          "check BLACKLIST against this DOCS_ROOT's kr.tree.")
    sys.exit(1)
if leftover:
    print(f"FAIL: {len(leftover)} blacklisted page(s) still present in the database:")
    for path in leftover:
        print(f"  {path}")
    sys.exit(1)

print(f"PASS: all {len(blacklisted_stems)} blacklisted topic page(s) confirmed absent from {db_path}.")
PYEOF

echo
echo "Done. Backups (populate_db.py, insert_optimized_media.py, and sync_kdoc_json_to_db.py"
echo "each make their own) live alongside $TEST_DB as documentation.test.db.backup-* and"
echo "documentation.test.db.bak.*"
echo "The real database at $SOURCE_DB was never opened for writing."
