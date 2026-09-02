#!/usr/bin/env python3
"""
migrate_content_to_dictionary_brotli.py

One-time, resumable, whole-database migration: recompresses every Content row
whose ContentTypes.compression is 'brotli' against this database's shared
CompressionDictionary (see ADFA-5153), replacing plain (no dictionary) Brotli
blobs with dictionary-compressed ones in place.

Why this exists: populate_db.py and insert_optimized_media.py only ever touch
their own subset of Content ("k/html/%", "assets/%"). Every other Content row
in documentation.db - reference docs, tooltip-linked pages, whatever else - was
compressed with plain Brotli by whichever pipeline wrote it. Dictionary
compression pays off best when it covers the whole corpus, so this script is
what converts the rows outside populate_db.py's reach.

Note what it does NOT establish: an invariant that every 'brotli' row in a
shipped database uses the dictionary. That is unachievable by construction - a
plugin installed on-device contributes plain-Brotli rows at any time (see
PluginDocumentationManager/BrotliCompressor in the app). WebServer.kt therefore
tries a dictionary-attached decode and falls back to a plain one, and that
fallback is load-bearing rather than defensive. Any other reader of this
database needs the same fallback.

Classification, per row, is by *decoding* rather than by assumption, because
"plain decode failed" alone means very little:

  * decodes plainly AND with the dictionary, to identical bytes -> the encoder
    never referenced the dictionary (small or already-compressed payloads,
    ~0.5% of the real corpus). Nothing to gain; left untouched, so re-runs do
    not churn it. This is the case that makes a naive "plain decode succeeded,
    so it needs migrating" test re-migrate the same rows on every run.
  * decodes plainly only -> not yet migrated. Recompress.
  * decodes with the dictionary only -> already migrated.
  * decodes neither way -> reported as an ERROR, never counted as success. A
    truncated ADFA-5171 chain lands here, and silently counting it as
    "already migrated" is exactly how such a row stays plain-Brotli while the
    run reports a clean finish.

Chunked rows are reassembled via populate_db.fragment_chain, which finds a
chain by LIKE plus parsed suffix rather than by probing "<path>-1" - an
ADFA-5171 chain numbered from -2 would otherwise reassemble truncated. Run
renumber_misnumbered_fragments.py first if the database still has those; this
script reports them rather than repairing them.

Writes are in place: UPDATE on the base row, then the continuation rows are
reconciled by exact path. The base row is never DELETEd and re-INSERTed,
because Content carries AddBook/DeleteBook triggers on paths matching
'%.pdf' - a delete/insert cycle drops the curated Bookshelf entry (title,
description, bookCategoryID) and replaces it with 'CURRENT_TIMESTAMP || id'
under a fresh Content.id. 15 brotli-typed .pdf rows and all 7 Bookshelf rows
are in scope on the real database.

Dictionary training (only when CompressionDictionary does not exist yet) draws
a sample stratified across doc sets in proportion to their stored bytes, and is
bounded by a plaintext byte budget rather than a row count. Both halves matter,
measured on the real corpus with only the sampling varied:

    first 300 rows by path (all under "a/")        36.2% smaller than plain
    300 rows stratified across doc sets            33.2%  <- worse
    stratified, 32 MiB plaintext budget            48.3%  <- best
    first-by-path, same 32 MiB budget              36.4%  <- volume alone: nil

Stratifying at a fixed row count draws quotas from smaller doc sets, so total
material falls and the trainer cannot even fill a 256 KiB dictionary. The
spread is the win; the byte budget is what makes the spread affordable.

Safety: backs up the database first (VACUUM INTO, same as populate_db.py),
commits in batches (a single transaction spanning the whole run holds a write
lock that the readers below cannot work around under rollback-journal mode),
verifies every recompressed row round-trips before writing it, and VACUUMs
afterward on a separate connection (SQLite refuses VACUUM inside a
transaction). Interrupting it is safe: finished batches stand, and re-running
resumes.

Performance: all database access happens on the calling thread; the worker pool
only ever receives bytes. Each recompress spawns a `brotli` subprocess, so the
work parallelizes well, and keeping SQLite single-threaded avoids the
"database is locked" failure that worker-owned read connections hit under
journal_mode=delete (documentation.db's actual mode) once the write
transaction's page cache spills.

Usage:
    python3 migrate_content_to_dictionary_brotli.py <db_path> [--sample-size N]
        [--dict-size BYTES] [--max-workers N] [--training-bytes BYTES] [--sample-seed N]
"""
import argparse
import random
import sqlite3
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import brotli

from populate_db import (
    CHUNK_SIZE, DEFAULT_DICT_SIZE, DictionaryCompressor, backup_database, fragment_chain,
    load_or_create_dictionary,
)

DEFAULT_SAMPLE_SIZE = 300
# ~128x the 256 KiB dictionary. zstd's cover trainers want roughly two orders of
# magnitude more material than the dictionary they produce; below that they
# return a dictionary smaller than the cap, which measurably compresses worse.
DEFAULT_TRAINING_BYTES = 32 * 1024 * 1024
DEFAULT_SAMPLE_SEED = 0x5153
# Rows per write transaction. Small enough that the write lock is never held
# across a long stretch of compression work, large enough that commit overhead
# stays negligible against a q11 recompress.
BATCH_ROWS = 200

_thread_local = threading.local()


def read_item(conn, path: str) -> bytes:
    """The full stored bytes of one logical item: its base row plus every
    continuation row in its chain, in suffix order. Suffix-agnostic (see
    populate_db.fragment_chain), so an ADFA-5171 chain numbered from -2
    reassembles correctly rather than truncating."""
    row = conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()
    if row is None:
        return b""
    parts = [row[0]]
    if len(row[0]) < CHUNK_SIZE:
        return parts[0]
    for _n, fragment_path in fragment_chain(conn, path):
        fragment = conn.execute("SELECT content FROM Content WHERE path = ?", (fragment_path,)).fetchone()
        if fragment is not None:
            parts.append(fragment[0])
    return b"".join(parts)


def is_chunked_base(lengths: dict, base_path: str) -> bool:
    """Whether `base_path` is the head of a chunked item, given a
    {path: content length} map.

    A path merely *looking* like "<base>-<N>" does not make it a fragment.
    Chunking only ever splits a blob that exceeded CHUNK_SIZE, so the base row
    of a chunked item is always exactly CHUNK_SIZE bytes - the same test
    read_item, WebServer.kt and renumber_misnumbered_fragments.py all use.
    Without it, two independent pages named "X" and "X-1" read as one chunked
    item: "X-1" is classified as a fragment, so it is never scanned, never
    counted and never reported, and write_item then deletes it as surplus.

    Deliberately only the base row's length, not a contiguous walk from "-1":
    an ADFA-5171 chain numbered from "-2" is still a real chain, and requiring
    "-1" to exist would misread it as an independent page and migrate it twice.
    """
    return lengths.get(base_path) == CHUNK_SIZE


def _db_continuation_paths(conn, base_path: str) -> set:
    """The continuation rows belonging to `base_path`, empty unless it is
    actually a chunked base (see is_chunked_base). Chain membership itself
    comes from fragment_chain, which is suffix-agnostic and so handles an
    ADFA-5171 chain numbered from "-2"."""
    row = conn.execute("SELECT LENGTH(content) FROM Content WHERE path = ?", (base_path,)).fetchone()
    if row is None or not is_chunked_base({base_path: row[0]}, base_path):
        return set()
    return {fragment_path for _n, fragment_path in fragment_chain(conn, base_path)}


def write_item(conn, path: str, language_id: int, content_type_id: int, template_id: int, data: bytes) -> None:
    """Replaces one item's stored bytes in place: UPDATE on the base row, then
    the continuation rows reconciled by exact path (updated, inserted, or
    deleted as the new chunk count requires).

    Deliberately never DELETEs the base row: Content's AddBook/DeleteBook
    triggers fire on '%.pdf' paths and a delete/insert cycle silently replaces
    the curated Bookshelf entry with a timestamp title under a new
    Content.id. Continuation paths end in "-<N>", so they never match those
    triggers and are safe to delete. Nothing here goes through LIKE, so no
    unrelated row can be caught by a `_` wildcard in a path."""
    # Resolved before the UPDATE below: it is the base row's *current* length
    # that marks it as chunked, and overwriting it first would lose that.
    existing = _db_continuation_paths(conn, path)

    conn.execute("UPDATE Content SET content = ? WHERE path = ?", (data[:CHUNK_SIZE], path))

    wanted = {}
    for number, offset in enumerate(range(CHUNK_SIZE, len(data), CHUNK_SIZE), start=1):
        wanted[f"{path}-{number}"] = data[offset:offset + CHUNK_SIZE]

    for fragment_path, blob in wanted.items():
        if fragment_path in existing:
            conn.execute("UPDATE Content SET content = ? WHERE path = ?", (blob, fragment_path))
        else:
            conn.execute(
                "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                "VALUES (?, ?, ?, ?, ?)",
                (fragment_path, language_id, blob, content_type_id, template_id),
            )
    for surplus in sorted(existing - set(wanted)):
        conn.execute("DELETE FROM Content WHERE path = ?", (surplus,))


def _thread_compressor(dictionary_data: bytes) -> DictionaryCompressor:
    """One DictionaryCompressor per worker thread, reused across every row that
    thread processes - creating one per row would re-write the same dictionary
    bytes to a fresh temp file on every call for no benefit."""
    compressor = getattr(_thread_local, "compressor", None)
    if compressor is None:
        compressor = DictionaryCompressor(dictionary_data)
        _thread_local.compressor = compressor
    return compressor


def classify(compressor: DictionaryCompressor, stored: bytes) -> tuple:
    """Returns (state, plaintext) for one item's stored bytes, by decoding it
    both ways. See the module docstring for why each state means what it does.
    state is one of "both", "plain", "dictionary", "undecodable"."""
    try:
        plain = brotli.decompress(stored)
    except brotli.error:
        plain = None
    try:
        via_dictionary = compressor.decompress(stored)
    except RuntimeError:
        via_dictionary = None

    if plain is not None and via_dictionary is not None and plain == via_dictionary:
        return "both", plain
    if plain is not None:
        return "plain", plain
    if via_dictionary is not None:
        return "dictionary", via_dictionary
    return "undecodable", None


def recompress_item(dictionary_data: bytes, path: str, stored: bytes) -> dict:
    """Runs on a worker thread: pure bytes in, pure bytes out, no database
    access. Returns a result dict the caller writes back (or reports)."""
    compressor = _thread_compressor(dictionary_data)
    state, plain = classify(compressor, stored)
    if state == "undecodable":
        return {"path": path, "state": "error",
                "detail": "decodes neither plainly nor with the dictionary; "
                          "run renumber_misnumbered_fragments.py if its chain is numbered from -2"}
    if state in ("dictionary", "both"):
        return {"path": path, "state": "already"}

    recompressed = compressor.compress(plain)
    # A migration that does not round-trip is worse than no migration: the row
    # would only fail later, on-device, at read time.
    try:
        if compressor.decompress(recompressed) != plain:
            return {"path": path, "state": "error", "detail": "recompressed bytes do not decode back to the original"}
    except RuntimeError as exc:
        return {"path": path, "state": "error", "detail": f"recompressed bytes failed to decode: {exc}"}

    return {"path": path, "state": "migrated", "data": recompressed,
            "before": len(stored), "after": len(recompressed)}


def doc_set(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(root)"


def collect_training_samples(conn, base_rows: list, sample_size: int,
                             byte_budget: int = DEFAULT_TRAINING_BYTES,
                             seed: int = DEFAULT_SAMPLE_SEED, decode=None) -> list:
    """Plaintext samples for training a new dictionary, stratified across doc
    sets in proportion to their stored bytes and bounded by `byte_budget` of
    plaintext. See the module docstring for the measurements behind both
    choices. Deterministic for a given seed, because a dictionary is never
    retrained once stored - being able to reproduce the training set later is
    the only way to explain the bytes you are then stuck with.

    `decode` reads one item's stored bytes back to plaintext, defaulting to plain
    Brotli because this only ever runs before CompressionDictionary exists. A
    re-mint (see remint_dictionary.py) passes one that decodes against the
    outgoing dictionary instead."""
    decode = decode or brotli.decompress
    by_set = defaultdict(list)
    for row in base_rows:
        by_set[doc_set(row[0])].append(row)

    weight = {name: sum(row[4] for row in rows) for name, rows in by_set.items()}
    total_weight = sum(weight.values()) or 1
    rng = random.Random(seed)

    drawn = []
    for name, rows in by_set.items():
        shuffled = list(rows)
        rng.shuffle(shuffled)
        # Oversample per set: the byte budget below is the real limit, and a
        # short set should not strand budget that another set could use.
        quota = max(1, round(sample_size * weight[name] / total_weight))
        drawn.append((name, shuffled, quota))

    ordered = []
    for name, shuffled, quota in drawn:
        ordered.extend(shuffled[:quota * 3])
    rng.shuffle(ordered)

    samples = []
    used = 0
    for row in ordered:
        if used >= byte_budget or len(samples) >= sample_size * 3:
            break
        try:
            plain = decode(read_item(conn, row[0]))
        except brotli.error as exc:
            print(f"warning: could not decompress {row[0]!r} for training sample: {exc}", file=sys.stderr)
            continue
        samples.append(plain)
        used += len(plain)

    histogram = defaultdict(int)
    for row in ordered[:len(samples)]:
        histogram[doc_set(row[0])] += 1
    print(f"Training dictionary on {len(samples)} rows, {used / 1048576:.1f} MiB of plaintext "
          f"across {len(histogram)} doc set(s): {dict(sorted(histogram.items(), key=lambda kv: -kv[1]))}",
          file=sys.stderr)
    return samples


def load_base_rows(conn) -> list:
    """Every brotli-typed base row as (path, language_id, content_type_id,
    template_id, stored_len). Blobs are deliberately not selected here - the
    real table is ~130 MB of compressed content, and each row's bytes are read
    only when its turn comes."""
    rows = conn.execute(
        "SELECT C.path, C.languageID, C.contentTypeID, C.templateId, LENGTH(C.content) "
        "FROM Content C, ContentTypes CT "
        "WHERE C.contentTypeID = CT.id AND CT.compression = 'brotli' "
        "ORDER BY C.path"
    ).fetchall()
    # Classifying a row as a continuation purely on its name matching
    # "<existing path>-<digits>" is not safe: two independent pages named "X"
    # and "X-1" would make "X-1" invisible here - never scanned, never counted,
    # never reported - and write_item would then delete it as surplus. Gate on
    # the actual chunking rule instead (see continuation_paths).
    lengths = {path: length for path, length in conn.execute("SELECT path, LENGTH(content) FROM Content")}
    base_rows = []
    for row in rows:
        prefix, sep, suffix = row[0].rpartition("-")
        if sep == "-" and suffix.isdigit() and is_chunked_base(lengths, prefix):
            continue  # a continuation row; handled with its base
        base_rows.append(row)
    return base_rows


def migrate(conn, sample_size: int, dict_size: int, max_workers: int | None = None,
            training_bytes: int = DEFAULT_TRAINING_BYTES, sample_seed: int = DEFAULT_SAMPLE_SEED) -> dict:
    base_rows = load_base_rows(conn)

    has_dictionary = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'CompressionDictionary'"
    ).fetchone() is not None
    training_samples = [] if has_dictionary else collect_training_samples(
        conn, base_rows, sample_size, training_bytes, sample_seed
    )
    dictionary_data = load_or_create_dictionary(conn, training_samples, dict_size)
    conn.commit()

    stats = {"scanned": len(base_rows), "migrated": 0, "already": 0, "errors": 0,
             "bytes_before": 0, "bytes_after": 0}
    problems = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for start in range(0, len(base_rows), BATCH_ROWS):
            batch = base_rows[start:start + BATCH_ROWS]
            payloads = [(row, read_item(conn, row[0])) for row in batch]
            results = executor.map(
                lambda item: recompress_item(dictionary_data, item[0][0], item[1]), payloads
            )
            for row, result in zip(batch, results):
                if result["state"] == "already":
                    stats["already"] += 1
                    continue
                if result["state"] == "error":
                    stats["errors"] += 1
                    problems.append((result["path"], result["detail"]))
                    continue
                stats["migrated"] += 1
                stats["bytes_before"] += result["before"]
                stats["bytes_after"] += result["after"]
                write_item(conn, row[0], row[1], row[2], row[3], result["data"])
            conn.commit()

    stats["problems"] = problems
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db_path", type=Path, help="SQLite database to migrate, e.g. documentation.db")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help=f"Rows to aim for when training a dictionary, if none exists yet "
                             f"(default: {DEFAULT_SAMPLE_SIZE}); --training-bytes is the real limit")
    parser.add_argument("--training-bytes", type=int, default=DEFAULT_TRAINING_BYTES,
                        help=f"Plaintext byte budget for dictionary training "
                             f"(default: {DEFAULT_TRAINING_BYTES:,})")
    parser.add_argument("--sample-seed", type=int, default=DEFAULT_SAMPLE_SEED,
                        help="Seed for the stratified training sample, so a dictionary's training set "
                             "stays reproducible (default: %(default)s)")
    parser.add_argument("--dict-size", type=int, default=DEFAULT_DICT_SIZE,
                        help=f"Dictionary size in bytes if training a new one (default: {DEFAULT_DICT_SIZE})")
    parser.add_argument("--max-workers", type=int, default=None,
                        help="Worker threads for the recompress phase (default: ThreadPoolExecutor's own "
                             "min(32, cpu_count+4)); database access stays on the calling thread")
    args = parser.parse_args()

    if not args.db_path.is_file():
        print(f"error: {args.db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"Backing up {args.db_path}...", file=sys.stderr)
    backup_path = backup_database(args.db_path)
    print(f"Backup written to {backup_path}", file=sys.stderr)

    conn = sqlite3.connect(args.db_path)
    try:
        stats = migrate(conn, args.sample_size, args.dict_size, args.max_workers,
                        args.training_bytes, args.sample_seed)
        conn.commit()
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
        f"already dictionary-compressed {stats['already']}, errors {stats['errors']}."
    )
    if stats["migrated"]:
        before, after = stats["bytes_before"], stats["bytes_after"]
        pct = (1 - after / before) * 100 if before else 0.0
        print(f"Migrated bytes: {before:,} -> {after:,} ({pct:.1f}% smaller)")
    for path, detail in stats["problems"][:20]:
        print(f"error: {path}: {detail}", file=sys.stderr)
    if len(stats["problems"]) > 20:
        print(f"error: ... and {len(stats['problems']) - 20} more", file=sys.stderr)
    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
