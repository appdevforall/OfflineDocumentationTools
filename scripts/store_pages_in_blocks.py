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

Stores the pages of five documentation sets up to 64 to a compressed block,
instead of one compressed Content row each, and declares documentation
version 4.0.0 (ADFA-6683):

    uv run scripts/store_pages_in_blocks.py documentation.db --dry-run
    uv run scripts/store_pages_in_blocks.py documentation.db

Why: each Content row is compressed on its own, so what neighbouring pages
share - the same member tables, inherited-method lists and markup - is paid
for again in every row, and the shared CompressionDictionary only covers what
all pages share. Compressed together, the pages of a block in path order share
the rest. On the 2026-10-20 G02 ship-time build (3.0.0, after its re-mint),
this step stored 22,478 pages in 356 blocks: their rows held 34,834,228 bytes,
the blocks hold 24,608,732 (a/ 5,457,314 less, j/ 3,549,974, k/ 758,039,
a/devsite/ 296,984, i/ 163,185). Packed with rebuild_database.py --pack
Templates,Content,TooltipTexts,ContentBlocks --page-size 4096, the file went
from 84,688,896 bytes (the same build packed without blocks) to 74,616,832,
and its release .br (brotli -q 11 -w 24) from 66,283,003 to 55,914,367:
10,368,636 bytes smaller. The cap kept 18 pages as rows (5 a/, 12 j/, 1 k/),
and the largest block decodes to 4,172,562 bytes. With 16 workers the step
took 53 seconds.

The format, documentation version 4.0.0 (CLAUDE.md, "Schema 4.0.0"):

    ContentBlocks(id, data)
        data: Brotli of the block's items, concatenated - quality 11, window
        24, against CompressionDictionary, as Content rows are.
    ContentBlockItems(contentId, blockId, offset, length) WITHOUT ROWID
        The item is [offset, offset + length) of the decoded block. Its
        foreign key to Content lets rebuild_database.py --pack Content
        renumber contentId with Content.

An item is exactly its row's decoded bytes, before any template is applied. A
blocked row keeps its path, languageID, contentTypeID and templateId, so every
lookup by path and every page's template work as before; only its content
becomes an empty blob.

The version: 4.0.0 is a new major, because an app that does not read blocks
would serve every blocked page empty. The app reads blocks from a database
declaring major 4 or later, and never decides by whether the tables exist.
4.0.0 promises everything 3.0.0 does too: keyed tooltips and compressed
templates. So this step runs only on a database whose newest
DocumentationDatabaseVersion row (ORDER BY changeTime DESC, rowid DESC, as the
app and remint_dictionary.declared_major read it) declares major 3, and in the
same transaction as the blocks it replaces that table's rows with one, 4.0.0,
as migrate_tooltip_schema.py declares 3.0.0. A database declaring no version,
a major below 3, or a major of 4 or more is refused with nothing written.

Which rows: five sets, each in ORDER BY path COLLATE BINARY, in this order; a
block never mixes sets. Blocks are numbered in this order as written;
rebuild_database.py --pack ContentBlocks renumbers them, and nothing reads the
order. Paths are matched with GLOB, which is case-sensitive as paths are:

    1. a/ templated JSON:  path GLOB 'a/*' AND NOT GLOB 'a/devsite/*' AND templateId > 0
    2. j/ HTML:            path GLOB 'j/*' AND content type text/html, templated or not
    3. k/ templated JSON:  path GLOB 'k/*' AND templateId > 0
    4. a/devsite/:         every row under it
    5. i/:                 every row under it

j/ takes every text/html row, including templated JSON rows:
sync_javadoc_json_to_db.py keeps contentTypeID as text/html and sets templateId
to the javadoc.peb id (its docstring, lines 7-8; the UPDATE at line 375 and the
INSERT at line 398), so there is no templateId condition here.

Only rows whose content type is compressed with brotli, and only items held in
one row: a row that owns continuation rows ("path-1", ... after a 1 MiB row),
and each continuation row, stay as they are. So does an empty row, which has
nothing to decode.

The cap: an item over MAX_ITEM_BYTES (1 MiB) decoded stays a row. A block
closes at --pages-per-block items (default 64), or before the next item would
take it past MAX_BLOCK_BYTES (4 MiB) decoded. Serving a page decodes its whole
block, and the app caches decoded blocks up to 8 MiB, so two blocks always fit.
Without the cap, j/'s Javadoc index pages (up to 5,065,005 bytes each) made a
64-page block decode to 28,055,018 bytes. The summary counts, for each set,
the pages in blocks and their content types, the pages the cap kept as rows,
the rows left for their continuation rows, and the empty rows.

Checks: every block is decoded again before its rows are emptied; it must
decode to its items' total length, and each item's slice of it is compared byte
for byte with its row's decoded bytes. Before the commit ContentBlocks must
hold exactly the blocks written, each the same bytes (by sha256) as the block
that was decoded back, ContentBlockItems exactly the items written, each with
its block, offset and length, and every blocked row must be empty. A block
stored as it was built decodes as it did, so it is not decoded twice. Any
failure rolls everything back.

A database that already has ContentBlocks or ContentBlockItems is refused
too: its blocked rows are empty, so a second run would store empty pages over
the blocked ones.

Where it goes: on the copy that ships, after the last dictionary re-mint and
its verify (remint_dictionary.py and verify_remint_dictionary.py refuse a 4.x
database: they do not recompress blocks) and before the pack. On that copy the
order is: minify, row drops, Kotlin trim, a/ pages-to-templates,
migrate_tooltip_schema.py, remint_dictionary.py --seeds 4 and
verify_remint_dictionary.py, this step, then rebuild_database.py --pack
Templates,Content,TooltipTexts,ContentBlocks --page-size 4096. That order needs
a 3.x copy, so the Drive master it is made from stays 3.x until ADFA-6871. The
master is to become 4.0.0 too (CLAUDE.md, "Schema 4.0.0"); from then on the
build first turns the copy's blocks back into rows (ADFA-6871), so the same
order runs and this step blocks every page again, those added or edited on the
master since included. A 4.0.0 database goes to dev-assets or Drive only once
the app's block reader is on CodeOnTheGo's stage: builds take whatever
dev-assets holds, and an app without the reader would show every blocked page
empty.

Runs in one transaction and VACUUMs afterwards. Takes no backup: run it on a
copy. --dry-run does all the work, writes and version included, and rolls it
back.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from migrate_tooltip_schema import declare_version
from optimize_db_media import (
    BrotliCodec,
    declared_major,
    human,
    is_continuation_path,
    load_dictionary,
    owned_fragment_paths,
    table_exists,
)

# (set, condition on Content AS c joined to ContentTypes AS t), in block-id order. GLOB, not LIKE: LIKE ignores case.
SETS = (
    ("a/", "c.path GLOB 'a/*' AND c.path NOT GLOB 'a/devsite/*' AND c.templateId > 0"),
    ("j/", "c.path GLOB 'j/*' AND t.value = 'text/html'"),
    ("k/", "c.path GLOB 'k/*' AND c.templateId > 0"),
    ("a/devsite/", "c.path GLOB 'a/devsite/*'"),
    ("i/", "c.path GLOB 'i/*'"),
)

# Decoded sizes. An item over MAX_ITEM_BYTES stays a row; a block closes before an item would take it past
# MAX_BLOCK_BYTES, so two blocks fit the app's 8 MiB cache of decoded blocks.
MAX_ITEM_BYTES = 1024 * 1024
MAX_BLOCK_BYTES = 4 * 1024 * 1024
PAGES_PER_BLOCK = 64
# Rows decoded at a time ahead of the grouping walk, so a set is never held decoded all at once. Read when a set is
# decoded, not bound as a default argument, so a test can shrink it.
DECODE_WINDOW = 256

# This step turns a 3.x database into 4.0.0, as one DocumentationDatabaseVersion row.
FROM_MAJOR, TO_MAJOR = 3, 4
# migrate_tooltip_schema.py turns this major, and no other, into 3.0.0.
TOOLTIP_MIGRATION_FROM_MAJOR = 2
WHO = "ADFA-6683"
COMMENT = ("ADFA-6683: a/, j/, k/, a/devsite/ and i/ pages stored up to 64 to a Brotli block in ContentBlocks, each "
           "located by ContentBlockItems; a blocked Content row's content is empty. Keyed tooltips and compressed "
           "templates as in 3.0.0")

SCHEMA = (
    """CREATE TABLE ContentBlocks (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    data BLOB NOT NULL      -- Brotli (q11, w24) of the block's items, concatenated, against CompressionDictionary
)""",
    """CREATE TABLE ContentBlockItems (
    contentId INTEGER NOT NULL PRIMARY KEY REFERENCES Content(id),
    blockId   INTEGER NOT NULL REFERENCES ContentBlocks(id),
    offset    INTEGER NOT NULL, -- into the decoded block, in bytes
    length    INTEGER NOT NULL  -- in bytes
) WITHOUT ROWID""",
)


def version_refusal(conn) -> str | None:
    """Why this database's declared version keeps it from getting blocks, or None if it declares FROM_MAJOR."""
    major = declared_major(conn)
    if major is None:
        return (f"it declares no documentation version (no DocumentationDatabaseVersion row), and this step turns "
                f"only a database declaring {FROM_MAJOR}.x into {TO_MAJOR}.0.0. Run it on the copy that ships, once "
                f"that declares {FROM_MAJOR}.0.0")
    if major < FROM_MAJOR:
        refusal = (f"it declares documentation major {major}, and this step turns {FROM_MAJOR}.x into "
                   f"{TO_MAJOR}.0.0, which promises everything {FROM_MAJOR}.0.0 does (keyed tooltips, compressed "
                   "templates)")
        if major == TOOLTIP_MIGRATION_FROM_MAJOR:
            refusal += f"; run migrate_tooltip_schema.py first, which turns {major}.x into {FROM_MAJOR}.0.0"
        return refusal
    if major > FROM_MAJOR:
        return (f"it declares documentation major {major}, and this step turns {FROM_MAJOR}.x into {TO_MAJOR}.0.0; "
                f"from {TO_MAJOR} on, pages are already in blocks or in a format this step does not know")
    return None


def select_sets(conn) -> list:
    """[(set, [(content id, path, content type, stored bytes)] in path order, items held in more than one row,
    their continuation rows, empty rows)] for each of SETS."""
    lengths = dict(conn.execute("SELECT path, LENGTH(content) FROM Content"))
    selected = []
    for name, condition in SETS:
        rows, chunked, continuations, empty = [], 0, 0, 0
        for content_id, path, content_type, stored in conn.execute(
                "SELECT c.id, c.path, t.value, c.content FROM Content AS c JOIN ContentTypes AS t "
                f"ON t.id = c.contentTypeID WHERE t.compression = 'brotli' AND {condition} "
                "ORDER BY c.path COLLATE BINARY"):
            if not stored:
                # Not a Brotli stream: `brotli -d` calls it corrupt input, and there is no page to block.
                empty += 1
            elif is_continuation_path(lengths, path):
                continuations += 1
            elif owned_fragment_paths(conn, path, len(stored)):
                chunked += 1
            else:
                rows.append((content_id, path, content_type, stored))
        selected.append((name, rows, chunked, continuations, empty))
    return selected


def decode_row(codec: BrotliCodec, row: tuple) -> bytes:
    try:
        return codec.decompress(row[3], "brotli")
    except RuntimeError as exc:
        raise RuntimeError(f"{row[1]}: {exc}") from exc


def decoded_in_order(pool, codec: BrotliCodec, rows: list):
    """(row, its decoded bytes) for every row, in order, decoded in the pool DECODE_WINDOW rows at a time: the next
    window only once the walk has taken the last row of this one."""
    window = DECODE_WINDOW
    for start in range(0, len(rows), window):
        batch = rows[start:start + window]
        yield from zip(batch, pool.map(lambda row: decode_row(codec, row), batch))


def group_items(decoded, per_block: int, kept: list):
    """Walks (row, decoded bytes) in path order and yields the blocks, each a list of them. A block closes at
    `per_block` items, or before the next item would take it past MAX_BLOCK_BYTES decoded. An item over
    MAX_ITEM_BYTES decoded goes to `kept` instead, and stays a row."""
    group, size = [], 0
    for row, plain in decoded:
        if len(plain) > MAX_ITEM_BYTES:
            kept.append(row)
            continue
        if group and (len(group) == per_block or size + len(plain) > MAX_BLOCK_BYTES):
            yield group
            group, size = [], 0
        group.append((row, plain))
        size += len(plain)
    if group:
        yield group


def build_block(codec: BrotliCodec, group: list) -> tuple:
    """(compressed block, decoded length, [(content id, offset, length)]) for one block's (row, decoded bytes),
    once the block has been decoded back and each item's slice of it matched its row's decoded bytes byte for
    byte."""
    plain = b"".join(raw for _row, raw in group)
    data = codec.compress(plain, "brotli")
    back = codec.decompress(data, "brotli")
    first = group[0][0][1]
    if len(back) != len(plain):
        raise RuntimeError(f"the block starting at {first} decoded back to {human(len(back))} bytes, not "
                           f"{human(len(plain))}")
    view, items, offset = memoryview(back), [], 0
    for (content_id, path, _type, _stored), raw in group:
        if view[offset:offset + len(raw)] != raw:
            raise RuntimeError(f"{path} does not read back byte for byte from the block starting at {first}")
        items.append((content_id, offset, len(raw)))
        offset += len(raw)
    return data, len(plain), items


def store_set(conn, pool, codec: BrotliCodec, rows: list, per_block: int, workers: int, written: tuple) -> dict:
    """Stores one set's rows in blocks, written in order as they are built, a few blocks ahead at most. Adds what
    it writes to `written`, ({block id: sha256 of its data}, {content id: (block id, offset, length)}); returns
    the set's figures."""
    stored_length = {row[0]: len(row[3]) for row in rows}
    content_type = {row[0]: row[2] for row in rows}
    figures = {"blocks": 0, "items": 0, "stored": 0, "compressed": 0, "decoded": 0, "types": Counter(), "kept": []}
    written_blocks, written_items = written
    pending = deque()

    def write(future):
        data, plain_length, items = future.result()
        block_id = conn.execute("INSERT INTO ContentBlocks (data) VALUES (?)", (data,)).lastrowid
        written_blocks[block_id] = hashlib.sha256(data).digest()
        for content_id, offset, length in items:
            conn.execute("INSERT INTO ContentBlockItems (contentId, blockId, offset, length) VALUES (?, ?, ?, ?)",
                         (content_id, block_id, offset, length))
            conn.execute("UPDATE Content SET content = x'' WHERE id = ?", (content_id,))
            written_items[content_id] = (block_id, offset, length)
            figures["stored"] += stored_length[content_id]
            figures["types"][content_type[content_id]] += 1
        figures["blocks"] += 1
        figures["items"] += len(items)
        figures["compressed"] += len(data)
        figures["decoded"] += plain_length

    for group in group_items(decoded_in_order(pool, codec, rows), per_block, figures["kept"]):
        pending.append(pool.submit(build_block, codec, group))
        while len(pending) > 2 * workers:
            write(pending.popleft())
    while pending:
        write(pending.popleft())
    return figures


def check_stored(conn, written: tuple) -> None:
    """Compares the tables with what was written (`written`, as store_set fills it): ContentBlocks must hold
    exactly the blocks written, each the same bytes by sha256, ContentBlockItems exactly the items written, each
    with its block, offset and length, and every blocked row must now be empty. build_block decoded each block
    back and matched every item's slice of it, so a block stored as built needs no second decode."""
    written_blocks, written_items = written
    items = {content_id: (block_id, offset, length) for content_id, block_id, offset, length in conn.execute(
        "SELECT contentId, blockId, offset, length FROM ContentBlockItems")}
    wrong = sorted(content_id for content_id in items.keys() | written_items.keys()
                   if items.get(content_id) != written_items.get(content_id))
    if wrong:
        raise RuntimeError(f"ContentBlockItems does not hold {human(len(wrong))} item(s) as written, Content id "
                           f"{wrong[0]} first")
    stored = set()
    # One block at a time from the cursor, so the blocks are never all in memory at once.
    for block_id, data in conn.execute("SELECT id, data FROM ContentBlocks"):
        if hashlib.sha256(data).digest() != written_blocks.get(block_id):
            raise RuntimeError(f"block {block_id} as stored is not the block written")
        stored.add(block_id)
    lost = sorted(written_blocks.keys() - stored)
    if lost:
        raise RuntimeError(f"{human(len(lost))} block(s) written are not in ContentBlocks, block {lost[0]} first")
    left = conn.execute("SELECT COUNT(*) FROM Content WHERE LENGTH(content) > 0 AND id IN "
                        "(SELECT contentId FROM ContentBlockItems)").fetchone()[0]
    if left:
        raise RuntimeError(f"{left} blocked row(s) still hold their content")


def report(summary: list, per_block: int, dry_run: bool) -> None:
    for name, figures, chunked, continuations, empty in summary:
        types = ", ".join(f"{value} {human(count)}" for value, count in figures["types"].most_common())
        print(f"  {name}: {human(figures['items'])} page(s) in {human(figures['blocks'])} block(s); their rows held "
              f"{human(figures['stored'])} bytes, the blocks hold {human(figures['compressed'])} "
              f"({human(figures['decoded'])} decoded). Content types: {types or 'none'}.")
        kept = figures["kept"]
        print(f"  {name}: left as rows: {human(len(kept))} page(s) over {human(MAX_ITEM_BYTES)} bytes decoded, "
              f"{human(chunked)} page(s) held in more than one row, {human(continuations)} continuation row(s), "
              f"and {human(empty)} empty row(s).")
        for _id, path, _type, _stored in kept[:10]:
            print(f"    kept by the cap: {path}")
        if len(kept) > 10:
            print(f"    ... and {human(len(kept) - 10)} more")
    every_set = [figures for _name, figures, *_left in summary]
    saved = sum(each["stored"] - each["compressed"] for each in every_set)
    verb, declare = ("Would store", "would declare") if dry_run else ("Stored", "declared")
    print(f"{verb} {human(sum(each['items'] for each in every_set))} page(s) in "
          f"{human(sum(each['blocks'] for each in every_set))} block(s) of up to {per_block} pages and "
          f"{human(MAX_BLOCK_BYTES)} bytes decoded, {human(saved)} bytes smaller than their rows; {declare} "
          f"{TO_MAJOR}.0.0.")


def run(cfg: dict) -> int:
    db_path, per_block, workers = cfg["db_path"], cfg["pages_per_block"], cfg.get("workers", 1)
    if per_block < 1:
        print(f"error: --pages-per-block must be at least 1, not {per_block}", file=sys.stderr)
        return 1
    if workers < 1:
        print(f"error: --workers must be at least 1, not {workers}", file=sys.stderr)
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
        refusal = version_refusal(conn)
        if refusal:
            print(f"error: {db_path}: {refusal}; nothing was written", file=sys.stderr)
            return 1
        dictionary = load_dictionary(conn)
        if not dictionary:
            print(f"error: {db_path} has no CompressionDictionary, which blocks are compressed against",
                  file=sys.stderr)
            return 1
        codec = BrotliCodec(dictionary)
        selected = select_sets(conn)

        if cfg["dry_run"]:
            print(f"Dry run: every write is rolled back. Storing {db_path}'s pages up to {per_block} to a block ...")
        else:
            print(f"Storing {db_path}'s pages up to {per_block} to a block ...")
        conn.execute("BEGIN")
        try:
            for sql in SCHEMA:
                conn.execute(sql)
            written = ({}, {})
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for name, rows, chunked, continuations, empty in selected:
                    figures = store_set(conn, pool, codec, rows, per_block, workers, written)
                    summary.append((name, figures, chunked, continuations, empty))
            check_stored(conn, written)
            declare_version(conn, TO_MAJOR, WHO, COMMENT)
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

    report(summary, per_block, cfg["dry_run"])
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
    p.add_argument("--pages-per-block", type=int, default=PAGES_PER_BLOCK, metavar="N",
                   help=f"Pages per block at most (default: {PAGES_PER_BLOCK}); a block also closes before it would "
                        f"decode to more than {human(MAX_BLOCK_BYTES)} bytes")
    p.add_argument("--dry-run", action="store_true", help="Do all the work, then roll back without writing")
    p.add_argument("--workers", type=int, default=8,
                   help="brotli processes run at once to decode rows and compress blocks (default: 8)")
    args = p.parse_args()
    sys.exit(run({"db_path": args.db_path, "pages_per_block": args.pages_per_block, "dry_run": args.dry_run,
                  "workers": args.workers}))


if __name__ == "__main__":
    main()
