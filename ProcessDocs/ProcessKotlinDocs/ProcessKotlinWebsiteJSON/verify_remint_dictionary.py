#!/usr/bin/env python3
"""
verify_remint_dictionary.py

Proves a re-minted database still holds exactly the content the original did.

For every 'brotli' item: decode it out of the new database with the new
dictionary, decode the same path out of the original with the old dictionary
(falling back to plain), and require the two plaintexts to be byte-identical.
Exits non-zero on any mismatch, so it can gate the swap:

    ./verify_remint_dictionary.py documentation.db remint.db && mv remint.db documentation.db

This is the check that makes re-minting safe to do at all. A row recompressed
against a mismatched dictionary does not fail loudly -- measured on the real
corpus, a wrong dictionary decodes without error into *different* bytes 38% of
the time (50% raises, 12% is identical because the perturbed region was never
referenced). Nothing at runtime detects that, so it has to be caught here,
against the original, before the file is put in place.

Reads only: neither database is modified.
"""
import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import brotli

from populate_db import DictionaryCompressor
from migrate_content_to_dictionary_brotli import load_base_rows, read_item

_thread_local = threading.local()


def _state(old_dictionary: bytes, new_dictionary: bytes, old_db: Path, new_db: Path):
    """Per-thread connections and compressors: a sqlite3.Connection cannot be
    shared across threads, and a DictionaryCompressor holds a temp file."""
    import sqlite3
    state = getattr(_thread_local, "state", None)
    if state is None:
        state = {
            "old_conn": sqlite3.connect(f"file:{old_db}?mode=ro", uri=True),
            "new_conn": sqlite3.connect(f"file:{new_db}?mode=ro", uri=True),
            "old": DictionaryCompressor(old_dictionary),
            "new": DictionaryCompressor(new_dictionary),
        }
        _thread_local.state = state
    return state


def check(old_dictionary: bytes, new_dictionary: bytes, old_db: Path, new_db: Path, path: str):
    """Returns None when the item matches, else a description of how it differs."""
    state = _state(old_dictionary, new_dictionary, old_db, new_db)
    was = read_item(state["old_conn"], path)
    now = read_item(state["new_conn"], path)
    if not now:
        return "row missing from the re-minted database"

    try:
        old_plain = state["old"].decompress(was)
    except RuntimeError:
        try:
            old_plain = brotli.decompress(was)
        except brotli.error:
            return "original row could not be decoded at all"
    try:
        new_plain = state["new"].decompress(now)
    except RuntimeError:
        return "re-minted row does not decode with the new dictionary"

    if old_plain != new_plain:
        return f"plaintext differs ({len(old_plain):,} vs {len(new_plain):,} bytes)"
    return None


def verify(old_db: Path, new_db: Path, max_workers: int | None = None) -> list:
    import sqlite3
    old_conn = sqlite3.connect(f"file:{old_db}?mode=ro", uri=True)
    new_conn = sqlite3.connect(f"file:{new_db}?mode=ro", uri=True)
    old_dictionary = old_conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()[0]
    new_dictionary = new_conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()[0]
    if old_dictionary == new_dictionary:
        print("warning: both databases carry the same dictionary; nothing was re-minted", file=sys.stderr)

    items = [row[0] for row in load_base_rows(new_conn)]
    print(f"Verifying {len(items):,} item(s)...", file=sys.stderr)

    bad = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, (path, problem) in enumerate(
            zip(items, executor.map(
                lambda p: check(old_dictionary, new_dictionary, old_db, new_db, p), items)), start=1
        ):
            if problem:
                bad.append((path, problem))
            if i % 6000 == 0:
                print(f"  {i:,}/{len(items):,} checked, {len(bad)} mismatched", file=sys.stderr)
    print(f"Byte-identical: {len(items) - len(bad):,}/{len(items):,}")
    return bad


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("original_db", type=Path, help="the database as it was before re-minting")
    parser.add_argument("reminted_db", type=Path, help="the re-minted database to check")
    parser.add_argument("--max-workers", type=int, default=None, help="worker threads")
    args = parser.parse_args()

    for path in (args.original_db, args.reminted_db):
        if not path.is_file():
            sys.exit(f"error: {path} does not exist")

    bad = verify(args.original_db, args.reminted_db, args.max_workers)
    for path, problem in bad[:20]:
        print(f"MISMATCH {path}: {problem}", file=sys.stderr)
    if len(bad) > 20:
        print(f"... and {len(bad) - 20} more", file=sys.stderr)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
