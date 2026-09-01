#!/usr/bin/env python3
"""Replaces the Java API documentation in documentation.db with javadoc-mode JSON.

The Java docs currently sit in `Content` as scraped HTML under `j/html/api/`. This swaps each of
those rows' blob for the JSON the kdoc-to-json plugin's Javadoc mode produces, and points the row
at a Pebble template that renders it -- the same arrangement the Kotlin website docs already use
(`contentTypeID` stays text/html because that is what the *served* page is; the blob is JSON and
`templateId` names the template that turns one into the other).

For every existing Content row under `j/html/api/`:
  - Find the matching file in the JSON tree: strip `j/html/api/`, swap `.html` for `.json`.
  - If it exists, rewrite it for the database (below), compress it the way the rest of the
    database is compressed, and update `content`, `contentTypeID` and `templateId`.
  - If it doesn't, leave the row alone. javadoc emits page kinds this pipeline does not
    (class-use/, package-use, the tree pages, serialized-form) -- about half the rows -- and those
    are working documentation whose paths the new pages do not link to anyway. Deleting them would
    remove information from the database rather than add it, so it takes an explicit
    --delete-missing.

Three things are rewritten on the way in, all so the templates can be plain Pebble with no custom
filters (documentation.db's reader has none, and none of the templates already in there use any):
  - `.json` links become `.html`, because the row's *path* -- and so the URL a browser asks for --
    ends in `.html`. The link text inside a doc comment is rewritten too, which is what the
    renderer's `doc` filter did.
  - `pathToRoot` is injected, since the templates need it for the stylesheet and the top nav and
    the reader passes nothing but the JSON itself.
  - `page` is left in the JSON, because the single template branches on it: the reader can only
    load one template per page, so all nine page kinds share one and choose their markup from it.

A timestamped backup is taken before anything is written.
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "j/html/api/"

# Only consulted with --delete-missing. A change in the plugin's output layout would otherwise gut
# the Java docs and report it as a clean run.
MAX_DELETE_FRACTION = 0.35

# The one template every Java page uses; it branches on the JSON's `page` field. Matches
# flatten_templates.OUTPUT_NAME.
TEMPLATE_NAME = "javadoc.peb"

# Page kinds that template knows how to render. A row whose JSON says anything else is left alone
# rather than pointed at a template that would not produce a page.
KNOWN_PAGES = frozenset({
    "class", "package", "module", "overview", "all-classes",
    "all-packages", "deprecated-list", "constant-values", "index",
})

# Templates this script installed before it used a single one. Removed on sight so a stale row
# cannot go on being referenced.
SUPERSEDED_TEMPLATES = tuple(f"javadoc-{kind}.peb" for kind in [
    "class", "package", "module", "overview", "all-classes",
    "all-packages", "deprecated-list", "constant-values", "index",
])

# `.json` at the end of a string, or followed by a quote or a fragment. Applied to *parsed* JSON
# strings, so the quote here is a real one rather than a backslash-escaped one in the raw text.
JSON_EXTENSION = re.compile(r'\.json(?=["#]|$)')


def backup_database(db_path: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{db_path}.bak.{stamp}"
    shutil.copy2(db_path, backup)
    return backup


def load_compression_dictionary(conn):
    """This database's shared Brotli dictionary (ADFA-5153), or None."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='CompressionDictionary'"
    ).fetchone() is None:
        return None
    row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
    return row[0] if row and row[0] else None


class DictionaryBrotli:
    """Compresses against a raw Brotli dictionary via the `brotli` CLI -- the Python package
    exposes no dictionary parameter. Mirrors sync_kdoc_json_to_db.DictionaryBrotli."""

    def __init__(self, dictionary_data: bytes):
        path = shutil.which("brotli")
        if path is None:
            raise RuntimeError(
                "this database uses a shared Brotli dictionary (ADFA-5153), which needs the "
                "`brotli` command-line tool; install it (brew install brotli) and retry"
            )
        self._brotli = path
        self._dir = Path(tempfile.mkdtemp(prefix="sync-javadoc-brotli-"))
        self._dict = self._dir / "dictionary.bin"
        self._dict.write_bytes(dictionary_data)
        atexit.register(lambda: shutil.rmtree(self._dir, ignore_errors=True))

    def compress(self, data: bytes) -> bytes:
        result = subprocess.run(
            [self._brotli, "-D", str(self._dict), "-c"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"brotli failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout


def rewrite_links(value):
    """Recursively swap `.json` for `.html` in every string of a parsed JSON document.

    Rewriting the parsed strings rather than the raw text is what makes this reliable: in the raw
    text a link inside documentation HTML is `href=\\"List.json\\"`, whose quotes are escaped, and
    a regex written against the unparsed form silently misses them.
    """
    if isinstance(value, str):
        return JSON_EXTENSION.sub(".html", value)
    if isinstance(value, list):
        return [rewrite_links(v) for v in value]
    if isinstance(value, dict):
        return {k: rewrite_links(v) for k, v in value.items()}
    return value


def path_to_root(content_path: str) -> str:
    """`j/html/api/java.base/java/util/ArrayList.html` -> `../../../`.

    Relative to the page, exactly as the file renderer computes it, so the templates that read it
    behave identically whether they are serving from disk or from the database.
    """
    depth = content_path[len(PREFIX):].count("/")
    return "../" * depth


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("json_root", help="the javadoc-mode JSON tree (the api/ directory)")
    parser.add_argument("--db", default="documentation.db", help="path to documentation.db")
    parser.add_argument("--templates", type=Path, default=Path(__file__).resolve().parent / "db-templates",
                        help="directory holding the composed javadoc.peb")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--delete-missing", action="store_true",
                        help="also delete rows with no JSON counterpart (class-use/, package-use, "
                             "the tree pages...). Off by default: those are working pages this "
                             "pipeline simply does not regenerate.")
    args = parser.parse_args()

    json_root = Path(args.json_root)
    if not json_root.is_dir():
        print(f"Error: '{json_root}' is not a directory.", file=sys.stderr)
        return 2
    if not Path(args.db).is_file():
        print(f"Error: database '{args.db}' not found.", file=sys.stderr)
        return 2
    if not args.templates.is_dir():
        print(f"Error: template directory '{args.templates}' not found; run flatten_templates.py first.",
              file=sys.stderr)
        return 2

    if args.dry_run:
        print("Dry run: no backup will be made and nothing will be written.")
    else:
        print(f"Backed up database to: {backup_database(args.db)}")

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    dictionary = load_compression_dictionary(conn)
    compressor = DictionaryBrotli(dictionary) if dictionary else None
    print(f"Compression: {'shared-dictionary Brotli' if compressor else 'plain Brotli'}")

    # --- template --------------------------------------------------------
    source = (args.templates / TEMPLATE_NAME).read_text(encoding="utf-8")
    existing = cur.execute("SELECT id FROM Templates WHERE name = ?", (TEMPLATE_NAME,)).fetchone()
    if args.dry_run:
        template_id = existing[0] if existing else -1
        print(f"  [{'UPDATE' if existing else 'INSERT'} TEMPLATE] {TEMPLATE_NAME} ({len(source)} bytes)")
    elif existing:
        cur.execute("UPDATE Templates SET content = ? WHERE id = ?", (source.encode(), existing[0]))
        template_id = existing[0]
    else:
        cur.execute("INSERT INTO Templates (name, content) VALUES (?, ?)", (TEMPLATE_NAME, source.encode()))
        template_id = cur.lastrowid
    print(f"Installed template '{TEMPLATE_NAME}' ({len(source)} bytes) for all page kinds.")

    stale = [row[0] for row in cur.execute(
        f"SELECT name FROM Templates WHERE name IN ({','.join('?' * len(SUPERSEDED_TEMPLATES))})",
        SUPERSEDED_TEMPLATES,
    )]
    if stale:
        if args.dry_run:
            print(f"  [DELETE TEMPLATE] {len(stale)} superseded per-page template(s): {', '.join(sorted(stale))}")
        else:
            cur.executemany("DELETE FROM Templates WHERE name = ?", [(n,) for n in stale])
            print(f"Removed {len(stale)} superseded per-page template(s).")

    html_type = cur.execute("SELECT id FROM ContentTypes WHERE value = 'text/html'").fetchone()[0]

    rows = cur.execute(
        "SELECT id, path, contentTypeID FROM Content WHERE path LIKE ?", (PREFIX + "%",)
    ).fetchall()
    print(f"Found {len(rows)} existing Content row(s) under '{PREFIX}'.")

    updated = deleted = kept = 0
    delete_ids: list[int] = []
    unknown_pages: set[str] = set()

    passed_through = 0
    for row_id, path, content_type in rows:
        source_file = json_root / (path[len(PREFIX):][: -len(".html")] + ".json") \
            if path.endswith(".html") else json_root / path[len(PREFIX):]
        if not source_file.is_file():
            if args.delete_missing:
                delete_ids.append(row_id)
                deleted += 1
            else:
                kept += 1
            continue

        # javadoc's plain-text manifest (element-list) and any other non-JSON file the tree
        # carries is stored as-is: it is not a page, has no template, and needs no rewriting.
        if source_file.suffix != ".json":
            raw = source_file.read_bytes()
            blob = compressor.compress(raw) if compressor else raw
            if not args.dry_run:
                cur.execute("UPDATE Content SET content = ?, templateId = 0 WHERE id = ?", (blob, row_id))
            passed_through += 1
            continue

        document = json.loads(source_file.read_text(encoding="utf-8"))
        page_kind = document.get("page")
        if page_kind not in KNOWN_PAGES:
            unknown_pages.add(str(page_kind))
            kept += 1
            continue

        document = rewrite_links(document)
        document["pathToRoot"] = path_to_root(path)
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        blob = compressor.compress(payload) if compressor else payload

        if not args.dry_run:
            cur.execute(
                "UPDATE Content SET content = ?, contentTypeID = ?, templateId = ? WHERE id = ?",
                (blob, html_type, template_id, row_id),
            )
        updated += 1

    # --- new pages the JSON has and the database doesn't ------------------
    existing_paths = {path for _, path, _ in rows}
    added = 0
    language_id = cur.execute("SELECT id FROM Languages WHERE value = 'en-US'").fetchone()[0]
    for source_file in sorted(json_root.rglob("*.json")):
        content_path = PREFIX + str(source_file.relative_to(json_root))[: -len(".json")] + ".html"
        if content_path in existing_paths:
            continue
        document = json.loads(source_file.read_text(encoding="utf-8"))
        page_kind = document.get("page")
        if page_kind not in KNOWN_PAGES:
            continue
        document = rewrite_links(document)
        document["pathToRoot"] = path_to_root(content_path)
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        blob = compressor.compress(payload) if compressor else payload
        if not args.dry_run:
            cur.execute(
                "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                "VALUES (?, ?, ?, ?, ?)",
                (content_path, language_id, blob, html_type, template_id),
            )
        added += 1

    if args.delete_missing and rows and deleted / len(rows) >= MAX_DELETE_FRACTION:
        conn.rollback()
        print(
            f"\nAborting: {deleted} of {len(rows)} rows ({deleted / len(rows):.0%}) resolved to no "
            f"JSON file, at or above the {MAX_DELETE_FRACTION:.0%} guard. Nothing was written.",
            file=sys.stderr,
        )
        if unknown_pages:
            print(f"Unrecognised page kinds: {sorted(unknown_pages)}", file=sys.stderr)
        return 1

    if delete_ids and not args.dry_run:
        cur.executemany("DELETE FROM Content WHERE id = ?", [(i,) for i in delete_ids])

    if not args.dry_run:
        cur.execute("INSERT INTO LastChange (documentationSet, who) VALUES (?, ?)",
                    ("java", "sync_javadoc_json_to_db.py"))
        conn.commit()
    conn.close()

    print(f"\nDone: updated {updated}, added {added}, deleted {deleted}, "
          f"passed through unchanged {passed_through}.")
    if kept:
        print(f"Left {kept} row(s) as HTML -- no JSON counterpart (class-use/, package-use, the "
              f"tree pages...). Pass --delete-missing to remove them instead.")
    if unknown_pages:
        print(f"Note: {len(unknown_pages)} unrecognised page kind(s) treated as deletions: {sorted(unknown_pages)}")
    if args.dry_run:
        print("(dry run - nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
