#!/usr/bin/env python3
"""
remint_dictionary.py

Trains a NEW shared Brotli dictionary for an already-migrated database and
recompresses every 'brotli' Content row against it, replacing the
CompressionDictionary row.

This deliberately does what load_or_create_dictionary refuses to do. That
refusal is right for the pipeline: a dictionary-compressed row is only decodable
with the exact dictionary it was compressed against, so replacing the stored
dictionary without recompressing the content orphans every row -- the dictionary
decode fails and the plain fallback fails too. The only safe way to change a
dictionary is to change the content with it, in one operation, which is what this
script is for. Everything runs in a single transaction: either every row is
converted and the dictionary replaced, or nothing is written.

Why bother re-minting at all: the dictionary a database is first minted with is
permanent for its content, so a poorly-sampled one is permanently expensive.
Measured on the real corpus with only the training sample varied:

    first 300 rows by path (all under "a/")        36.2% smaller than plain
    300 rows stratified across doc sets            33.2%  <- worse
    stratified, 32 MiB plaintext budget            48.3%  <- best
    first-by-path, same 32 MiB budget              36.4%  <- volume alone: nil

Re-minting the 21-Aug database took its brotli content from 83.4 MiB to 65.6
MiB and the vacuumed file from 268 MB to 249 MB, with all 29,677 items verified
byte-identical afterwards.

Rows are read with the outgoing dictionary, falling back to a plain decode --
which is not optional, because a dictionary database always holds some plain
rows: anything a plugin contributed on-device, anything written by a script
outside populate_db.py's reach, and any row whose payload the encoder never
needed the dictionary for.

Every row is proved to round-trip against the new dictionary before it is
written. Afterwards, run verify_remint_dictionary.py against a copy of the
original database: it re-reads every row through both dictionaries and requires
the plaintexts to match. That check is not paranoia -- a row recompressed
against a mismatched dictionary decodes without error into *different* bytes
(measured: 38% of the time), so nothing at runtime would catch it.

Usage:
    cp documentation.db remint.db
    ./remint_dictionary.py remint.db
    ./verify_remint_dictionary.py documentation.db remint.db && mv remint.db documentation.db
"""
import argparse
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import brotli

from migrate_content_to_dictionary_brotli import (
    BATCH_ROWS, DEFAULT_SAMPLE_SEED, DEFAULT_SAMPLE_SIZE, DEFAULT_TRAINING_BYTES,
    collect_training_samples, load_base_rows, read_item, write_item,
)
from populate_db import DEFAULT_DICT_SIZE, DictionaryCompressor, backup_database, train_dictionary

_thread_local = threading.local()


def _compressors(old_dictionary: bytes, new_dictionary: bytes) -> tuple:
    """One pair of DictionaryCompressors per worker thread; each writes its
    dictionary to a temp file once, so this avoids doing that per row."""
    pair = getattr(_thread_local, "pair", None)
    if pair is None:
        pair = (DictionaryCompressor(old_dictionary), DictionaryCompressor(new_dictionary))
        _thread_local.pair = pair
    return pair


def decode_outgoing(old: DictionaryCompressor, stored: bytes) -> bytes | None:
    """Plaintext of one item as stored today: against the outgoing dictionary,
    or plainly for the rows that never used it."""
    try:
        return old.decompress(stored)
    except RuntimeError:
        pass
    try:
        return brotli.decompress(stored)
    except brotli.error:
        return None


def convert(old_dictionary: bytes, new_dictionary: bytes, path: str, stored: bytes) -> dict:
    """Runs on a worker thread: bytes in, bytes out, no database access."""
    old, new = _compressors(old_dictionary, new_dictionary)
    plain = decode_outgoing(old, stored)
    if plain is None:
        return {"path": path, "error": "decodes with neither the outgoing dictionary nor plainly"}
    recompressed = new.compress(plain)
    try:
        if new.decompress(recompressed) != plain:
            return {"path": path, "error": "recompressed bytes do not decode back to the same plaintext"}
    except RuntimeError as exc:
        return {"path": path, "error": f"recompressed bytes failed to decode: {exc}"}
    return {"path": path, "data": recompressed, "before": len(stored), "after": len(recompressed)}


def remint(conn, sample_size: int = DEFAULT_SAMPLE_SIZE, dict_size: int = DEFAULT_DICT_SIZE,
           training_bytes: int = DEFAULT_TRAINING_BYTES, seed: int = DEFAULT_SAMPLE_SEED,
           max_workers: int | None = None) -> dict:
    """Re-mints in one transaction. Raises RuntimeError, having written nothing,
    if any row fails to convert."""
    row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
    if row is None or not row[0]:
        raise RuntimeError("this database has no CompressionDictionary to re-mint; "
                           "run migrate_content_to_dictionary_brotli.py instead")
    old_dictionary = row[0]
    base_rows = load_base_rows(conn)

    with DictionaryCompressor(old_dictionary) as old:
        samples = collect_training_samples(
            conn, base_rows, sample_size, training_bytes, seed,
            decode=lambda stored: decode_outgoing(old, stored) or b"",
        )
    if not samples:
        raise RuntimeError("no training samples could be decoded; refusing to train on nothing")
    new_dictionary = train_dictionary(samples, dict_size)

    stats = {"items": len(base_rows), "before": 0, "after": 0}
    problems = []
    conn.execute("BEGIN")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for start in range(0, len(base_rows), BATCH_ROWS):
            batch = base_rows[start:start + BATCH_ROWS]
            payloads = [(r[0], read_item(conn, r[0])) for r in batch]
            results = executor.map(
                lambda item: convert(old_dictionary, new_dictionary, item[0], item[1]), payloads
            )
            for r, result in zip(batch, results):
                if "error" in result:
                    problems.append((result["path"], result["error"]))
                    continue
                stats["before"] += result["before"]
                stats["after"] += result["after"]
                write_item(conn, r[0], r[1], r[2], r[3], result["data"])

    if problems:
        conn.rollback()
        for path, why in problems[:10]:
            print(f"error: {path}: {why}", file=sys.stderr)
        raise RuntimeError(f"{len(problems)} row(s) failed to convert; nothing written")

    conn.execute("UPDATE CompressionDictionary SET data = ? WHERE id = 1", (new_dictionary,))
    conn.commit()
    stats["dictionary_bytes"] = len(new_dictionary)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db_path", type=Path, help="database to re-mint; work on a copy")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help=f"rows to aim for when training (default: {DEFAULT_SAMPLE_SIZE}); "
                             f"--training-bytes is the real limit")
    parser.add_argument("--training-bytes", type=int, default=DEFAULT_TRAINING_BYTES,
                        help=f"plaintext byte budget for training (default: {DEFAULT_TRAINING_BYTES:,})")
    parser.add_argument("--sample-seed", type=int, default=DEFAULT_SAMPLE_SEED,
                        help="seed for the stratified sample, so a dictionary's training set stays "
                             "reproducible (default: %(default)s)")
    parser.add_argument("--dict-size", type=int, default=DEFAULT_DICT_SIZE,
                        help=f"new dictionary size in bytes (default: {DEFAULT_DICT_SIZE})")
    parser.add_argument("--max-workers", type=int, default=None, help="worker threads for recompression")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the VACUUM INTO backup (for a copy you already treat as disposable)")
    args = parser.parse_args()

    if not args.db_path.is_file():
        sys.exit(f"error: {args.db_path} does not exist")

    if not args.no_backup:
        print(f"Backing up {args.db_path}...", file=sys.stderr)
        print(f"Backup written to {backup_database(args.db_path)}", file=sys.stderr)

    conn = sqlite3.connect(args.db_path)
    try:
        stats = remint(conn, args.sample_size, args.dict_size, args.training_bytes,
                       args.sample_seed, args.max_workers)
    finally:
        conn.close()

    print("Vacuuming database to reclaim freed space...", file=sys.stderr)
    vacuum_conn = sqlite3.connect(args.db_path)
    try:
        vacuum_conn.execute("VACUUM")
    finally:
        vacuum_conn.close()

    before, after = stats["before"], stats["after"]
    print(f"Re-minted {stats['items']:,} item(s) against a {stats['dictionary_bytes']:,}-byte dictionary: "
          f"{before:,} -> {after:,} bytes ({100 * (before - after) / before:.1f}% smaller)")
    print("Now run verify_remint_dictionary.py <original> <this file> before putting it in place.")


if __name__ == "__main__":
    main()
