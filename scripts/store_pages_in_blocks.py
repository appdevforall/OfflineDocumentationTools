#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
store_pages_in_blocks.py

Stores the pages of three documentation sets N to a compressed block instead
of one compressed Content row each (ADFA-6683, prototype):

    uv run scripts/store_pages_in_blocks.py documentation.db --pages-per-block 16 --dry-run
    uv run scripts/store_pages_in_blocks.py documentation.db --pages-per-block 16

Why: each Content row is compressed on its own, so what neighbouring pages
share - the same member tables, inherited-method lists and markup - is paid
for again in every row, and the shared CompressionDictionary only covers what
all pages share. Compressed together, N pages in path order share the rest.
The 2026-10-20 size analysis put the release .br saving at 7,865,965 bytes for
N = 16 and 10,062,166 for N = 64. The cost is at read time: serving one page
decodes its whole block, so the app caches decoded blocks.

The format (ADFA-6683-block-format.md, which the app reader follows too):

    ContentBlocks(id, data)
        data: Brotli of the block's plain items, concatenated - quality 11,
        window 24, against CompressionDictionary, as Content rows are.
    ContentBlockItems(contentId, blockId, offset, length) WITHOUT ROWID
        The item is [offset, offset + length) of the decoded block. Its
        foreign key to Content lets rebuild_database.py --pack Content
        renumber contentId with Content.

Which rows: three sets, each cut in ORDER BY path into blocks of N
consecutive items, the last block of a set maybe shorter; a block never mixes
sets. Block ids follow the sets in this order:

    1. a/ templated JSON: path LIKE 'a/%' AND path NOT LIKE 'a/devsite/%' AND templateId > 0
    2. j/ HTML:           path LIKE 'j/%' AND content type text/html
    3. k/ templated JSON: path LIKE 'k/%' AND templateId > 0

Only rows whose content type is compressed with brotli, and only items held in
one row: a row that owns continuation rows ("path-1", ... after a 1 MiB row),
and each continuation row, stay as they are. An item is exactly its row's
decoded bytes, before any template rendering. A blocked row keeps its path,
languageID, contentTypeID and templateId, so every lookup by path and every
page's template work as before; only its content becomes an empty blob.

Every block is decoded again before it is stored and compared with the items
it was built from, and before the commit every item is sliced from its stored
block and compared, by sha256, with its row's decoded bytes.

A database that already has ContentBlockItems is refused: its blocked rows
are empty, so a second run would store empty pages over the blocked ones.

Runs in one transaction and VACUUMs afterwards. Takes no backup: run it on a
copy, then pack that with rebuild_database.py --pack Templates,Content,TooltipTexts.
--dry-run does all the work, writes included, and rolls it back.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import human, is_continuation_path, load_dictionary, owned_fragment_paths

# (set, condition on Content AS c joined to ContentTypes AS t), in block-id order.
SETS = (
    ("a/", "c.path LIKE 'a/%' AND c.path NOT LIKE 'a/devsite/%' AND c.templateId > 0"),
    ("j/", "c.path LIKE 'j/%' AND t.value = 'text/html'"),
    ("k/", "c.path LIKE 'k/%' AND c.templateId > 0"),
)

SCHEMA = (
    """CREATE TABLE ContentBlocks (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    data BLOB NOT NULL      -- Brotli of the block's plain items, concatenated
)""",
    """CREATE TABLE ContentBlockItems (
    contentId INTEGER NOT NULL PRIMARY KEY REFERENCES Content(id),
    blockId   INTEGER NOT NULL REFERENCES ContentBlocks(id),
    offset    INTEGER NOT NULL, -- into the decoded block, in bytes
    length    INTEGER NOT NULL  -- in bytes
) WITHOUT ROWID""",
)


class DictionaryBrotli:
    """The `brotli` CLI against one dictionary. No Python binding takes a
    custom dictionary, so every tool here shells out the same way."""

    def __init__(self, dictionary: bytes):
        self._brotli = shutil.which("brotli")
        if self._brotli is None:
            raise RuntimeError("the `brotli` CLI is not on PATH; install it and retry")
        self._work_dir = Path(tempfile.mkdtemp(prefix="store_pages_in_blocks_"))
        self._dictionary = self._work_dir / "dict.bin"
        self._dictionary.write_bytes(dictionary)

    def _run(self, args: list, data: bytes) -> bytes:
        result = subprocess.run([self._brotli, *args, "-D", str(self._dictionary)], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"brotli failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def encode(self, data: bytes) -> bytes:
        return self._run(["-c", "-q", "11", "-w", "24"], data)

    def decode(self, data: bytes) -> bytes:
        return self._run(["-d", "-c"], data)

    def close(self) -> None:
        shutil.rmtree(self._work_dir, ignore_errors=True)


def table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def select_sets(conn) -> list:
    """[(set, [(content id, path, stored bytes)] in path order, items held in
    more than one row, their continuation rows)] for each of SETS."""
    lengths = dict(conn.execute("SELECT path, LENGTH(content) FROM Content"))
    selected = []
    for name, condition in SETS:
        rows, chunked, continuations = [], 0, 0
        for content_id, path, stored in conn.execute(
                "SELECT c.id, c.path, c.content FROM Content AS c JOIN ContentTypes AS t ON t.id = c.contentTypeID "
                f"WHERE t.compression = 'brotli' AND {condition} ORDER BY c.path COLLATE BINARY"):
            if is_continuation_path(lengths, path):
                continuations += 1
            elif owned_fragment_paths(conn, path, len(stored)):
                chunked += 1
            else:
                rows.append((content_id, path, stored))
        selected.append((name, rows, chunked, continuations))
    return selected


def build_block(codec: DictionaryBrotli, rows: list) -> tuple:
    """(compressed block, plain length, [(content id, offset, length, sha256)])
    for one block's rows, the block decoded again and checked."""
    plain, items, offset = [], [], 0
    for content_id, path, stored in rows:
        try:
            raw = codec.decode(stored)
        except RuntimeError as exc:
            raise RuntimeError(f"{path}: {exc}") from exc
        items.append((content_id, offset, len(raw), hashlib.sha256(raw).digest()))
        plain.append(raw)
        offset += len(raw)
    plain = b"".join(plain)
    data = codec.encode(plain)
    if codec.decode(data) != plain:
        raise RuntimeError(f"the block starting at {rows[0][1]} did not decode back to its items")
    return data, len(plain), items


def check_stored(conn, codec: DictionaryBrotli, expected: dict, workers: int) -> None:
    """Slices every item from its block as stored and compares it with its
    row's decoded bytes (`expected`: {content id: sha256}); every blocked row
    must now be empty."""
    items = {}
    for content_id, block_id, offset, length in conn.execute(
            "SELECT contentId, blockId, offset, length FROM ContentBlockItems"):
        items.setdefault(block_id, []).append((content_id, offset, length))
    if sum(len(group) for group in items.values()) != len(expected):
        raise RuntimeError("ContentBlockItems does not hold one row per blocked item")
    blocks = conn.execute("SELECT id, data FROM ContentBlocks ORDER BY id").fetchall()

    def check(block):
        block_id, data = block
        plain = codec.decode(data)
        for content_id, offset, length in items.get(block_id, ()):
            if hashlib.sha256(plain[offset:offset + length]).digest() != expected.get(content_id):
                raise RuntimeError(f"Content id {content_id} does not read back from block {block_id}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(check, blocks))
    left = conn.execute("SELECT COUNT(*) FROM Content WHERE LENGTH(content) > 0 AND id IN "
                        "(SELECT contentId FROM ContentBlockItems)").fetchone()[0]
    if left:
        raise RuntimeError(f"{left} blocked row(s) still hold their content")


def run(cfg: dict) -> int:
    db_path, per_block, workers = cfg["db_path"], cfg["pages_per_block"], cfg.get("workers") or 1
    if per_block < 1:
        print(f"error: --pages-per-block must be at least 1, not {per_block}", file=sys.stderr)
        return 1
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    size_before = db_path.stat().st_size

    conn = sqlite3.connect(db_path, isolation_level=None)
    codec = None
    summary = []
    try:
        for table in ("ContentBlockItems", "ContentBlocks"):
            if table_exists(conn, table):
                print(f"error: {db_path} already has {table}: it has been through this script, and its blocked "
                      "rows are empty, so a second run would store empty pages over them. Start again from a "
                      "database that has not.", file=sys.stderr)
                return 1
        dictionary = load_dictionary(conn)
        if not dictionary:
            print(f"error: {db_path} has no CompressionDictionary, which blocks are compressed against",
                  file=sys.stderr)
            return 1
        codec = DictionaryBrotli(dictionary)
        selected = select_sets(conn)

        if cfg["dry_run"]:
            print(f"Dry run: every write is rolled back. Storing {db_path}'s pages {per_block} to a block ...")
        else:
            print(f"Storing {db_path}'s pages {per_block} to a block ...")
        conn.execute("BEGIN")
        try:
            for sql in SCHEMA:
                conn.execute(sql)
            expected = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for name, rows, chunked, continuations in selected:
                    groups = [rows[start:start + per_block] for start in range(0, len(rows), per_block)]
                    stored = compressed = plain_total = 0
                    for group, (data, plain_length, items) in zip(
                            groups, pool.map(lambda group: build_block(codec, group), groups)):
                        block_id = conn.execute("INSERT INTO ContentBlocks (data) VALUES (?)", (data,)).lastrowid
                        for content_id, offset, length, digest in items:
                            conn.execute("INSERT INTO ContentBlockItems (contentId, blockId, offset, length) "
                                         "VALUES (?, ?, ?, ?)", (content_id, block_id, offset, length))
                            conn.execute("UPDATE Content SET content = x'' WHERE id = ?", (content_id,))
                            expected[content_id] = digest
                        stored += sum(len(row[2]) for row in group)
                        compressed += len(data)
                        plain_total += plain_length
                    summary.append((name, len(rows), len(groups), stored, compressed, plain_total, chunked,
                                    continuations))
            check_stored(conn, codec, expected, workers)
            if cfg["dry_run"]:
                conn.execute("ROLLBACK")
            else:
                conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"error: {exc}; nothing was written", file=sys.stderr)
        return 1
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    for name, items, blocks, stored, compressed, plain, chunked, continuations in summary:
        print(f"  {name}: {human(items)} page(s) in {human(blocks)} block(s); their rows held {human(stored)} "
              f"bytes, the blocks hold {human(compressed)} ({human(plain)} decoded).")
        if chunked or continuations:
            print(f"  {name}: left as they are: {human(chunked)} page(s) held in more than one row, and "
                  f"{human(continuations)} continuation row(s).")
    saved = sum(row[3] - row[4] for row in summary)
    verb = "Would store" if cfg["dry_run"] else "Stored"
    print(f"{verb} {human(sum(row[1] for row in summary))} page(s) in {human(sum(row[2] for row in summary))} "
          f"block(s) of up to {per_block}, {human(saved)} bytes smaller than their rows.")

    if not cfg["dry_run"]:
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()
        print(f"{db_path}: {human(size_before)} -> {human(db_path.stat().st_size)} bytes.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to change in place; run it on a copy")
    p.add_argument("--pages-per-block", type=int, required=True, metavar="N",
                   help="Pages per block; the last block of each set may hold fewer")
    p.add_argument("--dry-run", action="store_true", help="Do all the work, then roll back without writing")
    p.add_argument("--workers", type=int, default=8,
                   help="brotli processes run at once to decode rows and compress blocks (default: 8)")
    args = p.parse_args()
    sys.exit(run({"db_path": args.db_path, "pages_per_block": args.pages_per_block, "dry_run": args.dry_run,
                  "workers": args.workers}))


if __name__ == "__main__":
    main()
