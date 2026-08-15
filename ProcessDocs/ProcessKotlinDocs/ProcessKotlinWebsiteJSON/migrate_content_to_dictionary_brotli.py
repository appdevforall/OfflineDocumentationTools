#!/usr/bin/env python3
"""
migrate_content_to_dictionary_brotli.py

One-time, idempotent, whole-database migration: recompresses every Content
row whose ContentTypes.compression is 'brotli' against this database's
shared CompressionDictionary (see ADFA-5153), replacing plain (no
dictionary) Brotli blobs with dictionary-compressed ones in place.

Why this exists: populate_db.py and insert_optimized_media.py only ever
touch their own subset of Content ("k/html/%", "assets/%"). Every other
Content row in documentation.db - reference docs, tooltip-linked pages,
whatever else - was compressed with plain Brotli by whichever pipeline
wrote it, no dictionary involved. Once anything in this database is
dictionary-compressed, WebServer.kt's reader has to be able to assume EVERY
'brotli' row uses the same dictionary (a per-row dictionary/no-dictionary
flag was explicitly rejected in ADFA-5153 in favor of "convert everything,
once"). This script is what makes that assumption actually true for rows
outside populate_db.py's own reach.

Trains the shared dictionary from a random sample drawn across the WHOLE
Content table (not just one doc set) if CompressionDictionary doesn't
already exist - broader and more representative than populate_db.py's own
Kotlin-website-only bootstrap sample. Run this BEFORE ever running
populate_db.py against a fresh database, so the one dictionary that ends up
stored is trained on real cross-corpus data.

Idempotency: for each candidate row, a plain (no-dictionary) decompress is
attempted first. That reliably fails when the row is already
dictionary-compressed (verified empirically over 200 trials - a genuinely
missing dictionary, unlike a *wrong* one, can't coincidentally produce a
parseable stream), so a row that already migrated is left untouched and
counted as "already migrated" rather than reprocessed. Re-running this
script is therefore always safe.

Chunked rows (see CHUNK_SIZE in populate_db.py) are reassembled before
decompression and re-chunked identically after recompression, the same
fragmentation scheme WebServer.kt expects on read.

Safety: backs up the database first (VACUUM INTO, same as populate_db.py),
runs entirely inside one transaction (rolled back on any error), and VACUUMs
afterward on a separate connection (SQLite refuses VACUUM inside a
transaction).

Usage:
    python3 migrate_content_to_dictionary_brotli.py <db_path> [--sample-size N] [--dict-size BYTES]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import brotli

from populate_db import (
    CHUNK_SIZE, DEFAULT_DICT_SIZE, DictionaryCompressor, backup_database, insert_chunked_content,
    load_or_create_dictionary,
)

DEFAULT_SAMPLE_SIZE = 300


def reassemble_content(conn, path: str, first_content: bytes) -> bytes:
    """Reassembles a possibly-chunked row's full bytes - a row is fragmented
    purely when its content is exactly CHUNK_SIZE bytes, in which case
    "<path>-1", "<path>-2", ... are concatenated until a missing or
    shorter-than-CHUNK_SIZE row is hit. Mirrors WebServer.kt's own
    reassembly and insert_optimized_media.py's copy of the same logic."""
    if len(first_content) < CHUNK_SIZE:
        return first_content
    parts = [first_content]
    n = 1
    while True:
        row = conn.execute("SELECT content FROM Content WHERE path = ?", (f"{path}-{n}",)).fetchone()
        if row is None:
            break
        parts.append(row[0])
        if len(row[0]) < CHUNK_SIZE:
            break
        n += 1
    return b"".join(parts)


def delete_content(conn, path: str) -> None:
    """Deletes a Content row and any chunked continuation fragments for it.
    Content.path is UNIQUE, so this has to run before any re-insert at the
    same path."""
    conn.execute("DELETE FROM Content WHERE path = ? OR path LIKE ?", (path, f"{path}-%"))


def list_fragment_paths(conn) -> set:
    """Every Content.path that's a chunked continuation fragment of another
    row in this table (a path whose trailing "-<digits>" strip yields
    another path that's also present) - same convention as
    insert_optimized_media.py's is_fragment, generalized to the whole table
    instead of just one path prefix. Base (non-fragment) rows are the ones
    this migration processes; fragments are only ever touched indirectly,
    via reassemble_content/delete_content on their base row's path."""
    all_paths = {row[0] for row in conn.execute("SELECT path FROM Content")}
    fragments = set()
    for path in all_paths:
        prefix, sep, suffix = path.rpartition("-")
        if sep == "-" and suffix.isdigit() and prefix in all_paths:
            fragments.add(path)
    return fragments


def collect_training_samples(conn, brotli_base_rows: list, sample_size: int) -> list:
    """Decompresses up to `sample_size` rows' full (reassembled) content as
    plain Brotli - safe to assume plain here, since this only ever runs
    before CompressionDictionary exists, i.e. before anything in this
    database could possibly be dictionary-compressed yet."""
    sample_rows = brotli_base_rows[:sample_size]
    samples = []
    for path, first_content, _language_id, _content_type_id, _template_id in sample_rows:
        full = reassemble_content(conn, path, first_content)
        try:
            samples.append(brotli.decompress(full))
        except brotli.error as exc:
            print(f"warning: could not decompress {path!r} for training sample: {exc}", file=sys.stderr)
    return samples


def dictionary_already_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'CompressionDictionary'"
    ).fetchone() is not None


def migrate(conn, sample_size: int, dict_size: int) -> dict:
    fragment_paths = list_fragment_paths(conn)
    all_brotli_rows = conn.execute(
        "SELECT C.path, C.content, C.languageID, C.contentTypeID, C.templateId "
        "FROM Content C, ContentTypes CT "
        "WHERE C.contentTypeID = CT.id AND CT.compression = 'brotli' "
        "ORDER BY C.path"
    ).fetchall()
    base_rows = [row for row in all_brotli_rows if row[0] not in fragment_paths]

    # Only worth decompressing sample rows for training when there's actually
    # no dictionary yet - on every later run, load_or_create_dictionary would
    # just discard them anyway, and by then every already-migrated row can no
    # longer be plain-decompressed at all (see module docstring), so
    # attempting it would just spend time producing warnings for no benefit.
    training_samples = [] if dictionary_already_exists(conn) else collect_training_samples(conn, base_rows,
                                                                                             sample_size)
    dictionary_data = load_or_create_dictionary(conn, training_samples, dict_size)

    stats = {"scanned": len(base_rows), "migrated": 0, "already_migrated": 0, "bytes_before": 0, "bytes_after": 0}
    with DictionaryCompressor(dictionary_data) as compressor:
        for path, first_content, language_id, content_type_id, template_id in base_rows:
            full = reassemble_content(conn, path, first_content)
            try:
                plain = brotli.decompress(full)
            except brotli.error:
                # Already dictionary-compressed (a plain decode of dictionary-
                # compressed content reliably fails - see module docstring) -
                # nothing to do.
                stats["already_migrated"] += 1
                continue

            recompressed = compressor.compress(plain)
            stats["migrated"] += 1
            stats["bytes_before"] += len(full)
            stats["bytes_after"] += len(recompressed)

            delete_content(conn, path)
            insert_chunked_content(conn, path, language_id, content_type_id, template_id, recompressed, [])

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db_path", type=Path, help="SQLite database to migrate, e.g. documentation.db")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                         help=f"Rows to sample for dictionary training if none exists yet (default: {DEFAULT_SAMPLE_SIZE})")
    parser.add_argument("--dict-size", type=int, default=DEFAULT_DICT_SIZE,
                         help=f"Dictionary size in bytes if training a new one (default: {DEFAULT_DICT_SIZE})")
    args = parser.parse_args()

    if not args.db_path.is_file():
        print(f"error: {args.db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"Backing up {args.db_path}...", file=sys.stderr)
    backup_path = backup_database(args.db_path)
    print(f"Backup written to {backup_path}", file=sys.stderr)

    conn = sqlite3.connect(args.db_path)
    try:
        conn.execute("BEGIN")
        stats = migrate(conn, args.sample_size, args.dict_size)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("Vacuuming database to reclaim freed space...", file=sys.stderr)
    vacuum_conn = sqlite3.connect(args.db_path)
    try:
        vacuum_conn.execute("VACUUM")
    finally:
        vacuum_conn.close()

    print(
        f"Scanned {stats['scanned']} brotli row(s): migrated {stats['migrated']}, "
        f"already dictionary-compressed {stats['already_migrated']}."
    )
    if stats["migrated"]:
        before, after = stats["bytes_before"], stats["bytes_after"]
        pct = (1 - after / before) * 100 if before else 0.0
        print(f"Migrated bytes: {before:,} -> {after:,} ({pct:.1f}% smaller)")


if __name__ == "__main__":
    main()
