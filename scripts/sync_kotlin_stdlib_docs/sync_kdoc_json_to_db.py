#!/usr/bin/env python3
"""
Overwrites the kotlin-stdlib / kotlin-reflect / kotlin-test Content rows in
documentation.db with fresh output from the KDoc-to-JSON Dokka plugin.

For every existing Content row whose path starts with "k/kotlin-stdlib",
"k/kotlin-reflect", or "k/kotlin-test":
  - Compute the corresponding file in the plugin output tree: strip the "k/"
    prefix, and if the path ends in ".html", swap that for ".json" (paths with
    no extension, e.g. ".../package-list", are looked up unchanged).
  - If that file exists, re-compress it (matching the row's existing
    ContentTypes.compression) and overwrite the row's `content` blob only --
    `path`, `languageID`, `contentTypeID`, and `templateId` are left untouched.
    A result too large for a single row is split into "<path>-1", "<path>-2",
    ... continuation rows instead (see CHUNK_SIZE), the same fragmentation
    populate_db.py writes and WebServer.kt reads back; that case replaces the
    base row rather than updating it in place, so its `id` changes, but the
    four columns above still carry over unchanged.
  - If it doesn't exist, delete the row (and any continuation rows it owns).

Existing "<path>-N" continuation rows are not treated as pages of their own:
they belong to their base row and are rewritten or removed along with it.

Any TooltipButtons row whose `uri` (ignoring a trailing "#fragment") matches one
of the deleted Content paths is now a dead link. Its entire parent Tooltips
record -- along with all of that tooltip's other TooltipButtons rows, dead or
not -- is deleted too, since TooltipButtons has no ON DELETE CASCADE and a
dangling tooltipId would otherwise be left behind.

Compression follows whatever the target database uses. From schema version
2.0.0 (ADFA-5153) every "brotli" Content row is compressed against the shared
raw LZ77 dictionary in the CompressionDictionary table, and a plain-Brotli
blob written into such a database is simply undecodable by the server -- so
this script reads that dictionary and compresses against it whenever the
table is present, falling back to plain Brotli only for older databases that
predate it. It never creates or retrains a dictionary: an existing one is the
only thing the rows already in that database can be decoded with.

A timestamped backup of the database is made before anything is modified.
"""
import argparse
import atexit
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import brotli

PREFIXES = ["k/kotlin-stdlib", "k/kotlin-reflect", "k/kotlin-test"]

# Must match WebServer.kt's "contentChunkSize" (1024 * 1024) and
# populate_db.py's CHUNK_SIZE exactly. The server decides a row is fragmented
# purely by its content being exactly this many bytes, then keeps requesting
# "<path>-1", "<path>-2", ... until it gets a shorter fragment or a missing
# row - so anything written here that exceeds it has to be split the same way
# populate_db.py splits it, or the server will serve a truncated page.
CHUNK_SIZE = 1024 * 1024


def backup_database(db_path):
    """Writes a timestamped backup beside db_path. Uses SQLite's own VACUUM
    INTO rather than a file copy: it takes a read transaction for the
    duration, so the result is always an internally consistent database even
    if something else is mid-write (a plain copy of a live, or WAL-mode,
    database can be torn). Same approach populate_db.py uses."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.bak.{timestamp}"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (backup_path,))
    finally:
        conn.close()
    return backup_path


def relative_target_path(content_path):
    """'k/kotlin-stdlib/kotlin.text/index.html' -> 'kotlin-stdlib/kotlin.text/index.json'
    'k/kotlin-stdlib/package-list' -> 'kotlin-stdlib/package-list' (no extension to swap)"""
    without_prefix = content_path[len("k/"):]
    if without_prefix.endswith(".html"):
        return without_prefix[: -len(".html")] + ".json"
    return without_prefix


class DictionaryCompressor:
    """Compresses bytes against a fixed raw Brotli dictionary by shelling out
    to the `brotli` CLI's -D flag. The installed Python `brotli` package has
    no dictionary parameter at all, and brotlicffi dropped the custom-
    dictionary API in 1.2, so the CLI is the only route available here.

    Deliberately mirrors the DictionaryCompressor in
    ProcessKotlinWebsiteJSON/populate_db.py (ADFA-5153): same -D invocation,
    same one-temp-file-per-instance lifetime, so the two converge trivially
    if this ever moves into a module both can import.

    A dictionary-compressed stream and a plain one are not interchangeable in
    either direction - decoding one as the other fails - which is exactly why
    writing plain Brotli into a dictionary database corrupts it."""

    def __init__(self, dictionary_data):
        self._brotli_path = shutil.which("brotli")
        if self._brotli_path is None:
            raise RuntimeError(
                "this database uses dictionary-compressed content (CompressionDictionary), which needs the "
                "`brotli` CLI to read or write; install it (e.g. `apt install brotli` / `brew install brotli`) "
                "and retry"
            )
        self._work_dir = Path(tempfile.mkdtemp(prefix="brotli-dict-"))
        self._dict_path = self._work_dir / "dictionary.bin"
        self._dict_path.write_bytes(dictionary_data)
        atexit.register(self.close)

    def _run(self, *extra_args, data):
        result = subprocess.run(
            [self._brotli_path, "-D", str(self._dict_path), *extra_args, "-c"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"brotli failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def compress(self, data):
        return self._run(data=data)

    def decompress(self, data):
        return self._run("-d", data=data)

    def close(self):
        shutil.rmtree(self._work_dir, ignore_errors=True)


def load_dictionary(conn):
    """Returns this database's CompressionDictionary bytes, or None for a
    database that predates schema 2.0.0 and so has no dictionary at all.

    Never creates or trains one. Whatever is already stored here is the only
    thing the rows already in this database can be decoded against, so
    inventing a dictionary would orphan every one of them - and this script
    only ever rewrites a subset of an existing corpus, so there is no case
    where it should be the one deciding what the dictionary is."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'CompressionDictionary'"
    ).fetchone()
    if not exists:
        return None
    row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
    return row[0] if row else None


def compress_for(compression, raw_bytes, path, compressor=None):
    """Compresses raw_bytes for storage under the given ContentTypes.
    compression policy. `compressor`, when set, is a DictionaryCompressor for
    databases whose brotli rows use the shared dictionary; without one,
    "brotli" means plain Brotli (pre-2.0.0 databases only)."""
    if compression == "brotli":
        return compressor.compress(raw_bytes) if compressor else brotli.compress(raw_bytes)
    if compression == "none":
        return raw_bytes
    raise ValueError(f"Unknown compression '{compression}' needed for {path}")


def like_escape(value):
    r"""Escapes LIKE's own wildcards ("%" and "_") plus the escape character
    itself, for use with a `LIKE ? ESCAPE '\'` clause - a path containing
    either would otherwise match more rows than intended ("_" matches any
    single character)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def is_fragment_path(path, all_paths):
    """True when path is a "<base>-<N>" chunk continuation row whose base is
    also present - the fragmentation convention populate_db.py writes and
    WebServer.kt reads. Ambiguous only for a real page literally named
    "<some-other-page>-<digits>", which the plugin output never produces."""
    base, sep, suffix = path.rpartition("-")
    return sep == "-" and suffix.isdigit() and base in all_paths


def delete_content_with_fragments(cur, content_id, path):
    """Deletes a Content row along with any chunk continuation rows it owns."""
    cur.execute("DELETE FROM Content WHERE id = ?", (content_id,))
    cur.execute(
        "DELETE FROM Content WHERE path LIKE ? ESCAPE '\\'", (f"{like_escape(path)}-%",)
    )


def write_content(cur, content_id, path, blob, language_id, content_type_id, template_id, chunked_log):
    """Replaces the content stored at `path` with `blob`, honouring the
    CHUNK_SIZE fragmentation contract.

    The common case (blob fits in one row) is an in-place UPDATE, which keeps
    the row's id stable; any stale fragments left over from a previous,
    larger version of the page are removed. An oversized blob can't be stored
    that way at all - the server would only ever serve the first row - so the
    row is replaced by a fresh base row plus "<path>-1", "<path>-2", ...
    continuations, each carrying the original row's languageID/contentTypeID/
    templateId. Appends (path, total size, chunk count) to chunked_log for
    anything that needed more than one row."""
    stale_fragments = "DELETE FROM Content WHERE path LIKE ? ESCAPE '\\'"

    if len(blob) <= CHUNK_SIZE:
        cur.execute("UPDATE Content SET content = ? WHERE id = ?", (blob, content_id))
        cur.execute(stale_fragments, (f"{like_escape(path)}-%",))
        return

    cur.execute("DELETE FROM Content WHERE id = ?", (content_id,))
    cur.execute(stale_fragments, (f"{like_escape(path)}-%",))

    insert = ("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
              "VALUES (?, ?, ?, ?, ?)")
    cur.execute(insert, (path, language_id, blob[:CHUNK_SIZE], content_type_id, template_id))
    fragment_number = 1
    offset = CHUNK_SIZE
    while offset < len(blob):
        cur.execute(insert, (f"{path}-{fragment_number}", language_id, blob[offset:offset + CHUNK_SIZE],
                             content_type_id, template_id))
        offset += CHUNK_SIZE
        fragment_number += 1
    chunked_log.append((path, len(blob), fragment_number))  # fragment_number == total chunk count here


def cleanup_orphaned_tooltips(cur, deleted_paths, dry_run):
    """Delete any Tooltips (and all their TooltipButtons) that reference a
    now-deleted Content path via a TooltipButtons.uri. Returns (tooltips_removed,
    buttons_removed)."""
    if not deleted_paths:
        return 0, 0

    deleted_path_set = set(deleted_paths)

    where_clause = " OR ".join(["uri = ? OR uri LIKE ?"] * len(PREFIXES))
    params = []
    for prefix in PREFIXES:
        params.extend([prefix, prefix + "/%"])

    candidate_buttons = cur.execute(
        f"SELECT tooltipId, uri FROM TooltipButtons WHERE {where_clause}", params
    ).fetchall()

    orphaned_tooltip_ids = sorted(
        {tooltip_id for tooltip_id, uri in candidate_buttons if uri.split("#", 1)[0] in deleted_path_set}
    )
    if not orphaned_tooltip_ids:
        return 0, 0

    placeholders = ",".join("?" * len(orphaned_tooltip_ids))
    buttons_count = cur.execute(
        f"SELECT count(*) FROM TooltipButtons WHERE tooltipId IN ({placeholders})", orphaned_tooltip_ids
    ).fetchone()[0]

    if dry_run:
        for tooltip_id in orphaned_tooltip_ids:
            print(f"  [DELETE TOOLTIP] id={tooltip_id}")
    else:
        cur.execute(f"DELETE FROM TooltipButtons WHERE tooltipId IN ({placeholders})", orphaned_tooltip_ids)
        cur.execute(f"DELETE FROM Tooltips WHERE id IN ({placeholders})", orphaned_tooltip_ids)

    return len(orphaned_tooltip_ids), buttons_count


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "plugin_output_root",
        help="Root dir directly containing kotlin-stdlib/, kotlin-reflect/, kotlin-test/ "
             "(e.g. .../all-libs from a KDoc-to-JSON run)",
    )
    parser.add_argument("--db", default="documentation.db", help="Path to documentation.db (default: documentation.db in the current directory)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen without modifying anything")
    args = parser.parse_args()

    if not os.path.isdir(args.plugin_output_root):
        print(f"Error: '{args.plugin_output_root}' is not a directory.", file=sys.stderr)
        sys.exit(2)
    if not os.path.isfile(args.db):
        print(f"Error: database '{args.db}' not found.", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        print("Dry run: no backup will be made and no changes will be written.")
    else:
        backup_path = backup_database(args.db)
        print(f"Backed up database to: {backup_path}")

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    compression_by_type = dict(cur.execute("SELECT id, compression FROM ContentTypes"))

    # Schema >= 2.0.0 stores every brotli row against a shared dictionary;
    # older databases have no CompressionDictionary table and use plain
    # Brotli. Either way this follows what the database already does rather
    # than imposing a choice - writing the wrong one produces rows the server
    # cannot decode at all.
    dictionary_data = load_dictionary(conn)
    compressor = DictionaryCompressor(dictionary_data) if dictionary_data else None
    print(
        f"Compression: dictionary-based ({len(dictionary_data):,}-byte CompressionDictionary)."
        if compressor else
        "Compression: plain Brotli (no CompressionDictionary table; database predates schema 2.0.0)."
    )

    where_clause = " OR ".join(["path = ? OR path LIKE ?"] * len(PREFIXES))
    params = []
    for prefix in PREFIXES:
        params.extend([prefix, prefix + "/%"])

    all_rows = cur.execute(
        f"SELECT id, path, contentTypeID, languageID, templateId FROM Content WHERE {where_clause}", params
    ).fetchall()

    # A chunked page is stored as a base row plus "<path>-1", "<path>-2", ...
    # continuation rows (see CHUNK_SIZE). Those fragments are part of their
    # base row's content, not pages in their own right - handled wholesale by
    # write_content below - so drop them from the work list. Left in, each
    # would be looked up as its own source file, never found (there's no
    # "index.html-1" in the plugin output), and deleted individually.
    all_paths = {row[1] for row in all_rows}
    rows = [row for row in all_rows if not is_fragment_path(row[1], all_paths)]
    fragments = len(all_rows) - len(rows)

    print(f"Found {len(rows)} existing Content record(s) under {PREFIXES}"
          f"{f' (plus {fragments} chunk continuation row(s))' if fragments else ''}.")

    updated = 0
    deleted = 0
    deleted_paths = []
    chunked_log = []

    try:
        conn.execute("BEGIN")
        for content_id, path, content_type_id, language_id, template_id in rows:
            rel_target = relative_target_path(path)
            source_file = os.path.join(args.plugin_output_root, rel_target)

            if os.path.isfile(source_file):
                with open(source_file, "rb") as f:
                    raw_bytes = f.read()

                # An unresolvable contentTypeID means this row's declared
                # type isn't in ContentTypes at all, so there's no way to
                # know whether the server will try to brotli-decompress what
                # gets written here. Guessing "uncompressed" and committing
                # anyway is how a row ends up serving bytes that don't match
                # its own declared type - fail instead.
                compression = compression_by_type.get(content_type_id)
                if compression is None:
                    raise RuntimeError(
                        f"{path} has contentTypeID {content_type_id}, which has no row in ContentTypes; "
                        "cannot tell how its content should be compressed. Fix the database's ContentTypes "
                        "table (or this row's contentTypeID) and re-run."
                    )

                new_blob = compress_for(compression, raw_bytes, path, compressor)

                if args.dry_run:
                    chunks = -(-len(new_blob) // CHUNK_SIZE) or 1
                    print(f"  [UPDATE] {path}  <-  {rel_target}"
                          f"{f' ({len(new_blob):,} bytes -> {chunks} chunks)' if chunks > 1 else ''}")
                else:
                    write_content(cur, content_id, path, new_blob, language_id, content_type_id, template_id,
                                  chunked_log)
                updated += 1
            else:
                if args.dry_run:
                    print(f"  [DELETE] {path}  (no matching {rel_target})")
                else:
                    delete_content_with_fragments(cur, content_id, path)
                deleted += 1
                deleted_paths.append(path)

        tooltips_removed, buttons_removed = cleanup_orphaned_tooltips(cur, deleted_paths, args.dry_run)

        if chunked_log:
            print(f"Chunked {len(chunked_log)} file(s) over {CHUNK_SIZE:,} bytes:")
            for path, total_size, chunk_count in chunked_log:
                print(f"  {path}: {total_size:,} bytes -> {chunk_count} chunks")

        if args.dry_run:
            conn.rollback()
            print(
                f"\nDry run complete: would update {updated}, delete {deleted} Content record(s); "
                f"would delete {tooltips_removed} Tooltips record(s) ({buttons_removed} TooltipButtons "
                "row(s)). No changes made."
            )
        else:
            conn.commit()
            print(
                f"\nDone: updated {updated}, deleted {deleted} Content record(s); "
                f"deleted {tooltips_removed} Tooltips record(s) ({buttons_removed} TooltipButtons row(s))."
            )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
