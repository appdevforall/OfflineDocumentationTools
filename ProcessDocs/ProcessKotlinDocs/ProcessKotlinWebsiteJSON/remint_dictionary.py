#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "brotli",
#     "markdown-it-py>=2.0",
# ]
# ///
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

A dictionary trained on one sample is not the best one the corpus allows, and
which sample does best is not predictable. On the minified 2026-10-18 database
(ADFA-6627), four consecutive seeds stored 62,045,716, 61,689,943, 62,286,820
and 62,404,993 bytes: 715 KB from best to worst. So --seeds trains a candidate
for each of several consecutive sample seeds, recompresses every row with each,
and keeps the one that stores smallest. Each candidate costs a full
recompression pass (about 3.4 minutes each with 16 workers there); the winner's
rows are kept from its pass, so writing costs none.

The winner is written only if it stores less than the dictionary already in
place, measured in the same pass (every row's size as read). Otherwise nothing
is written and the current dictionary stays: a re-mint is not always smaller -
one after a step that changed little stored 110,803 bytes more than the
dictionary it replaced - and a tie is no gain either. The same seeds do not
redraw the same samples once a re-mint has changed the stored sizes, because
the sampler weights each doc set by its stored bytes: re-minting a re-minted
database at the same 2 seeds drew different samples and stored 0.1% less.

Dictionary size (ADFA-3305). A re-mint defaults to a 768 KiB dictionary trained
on up to 3,600 rows within 128 MiB of plaintext (--sample-size 1200,
--training-bytes), where the first mint (populate_db.py,
migrate_content_to_dictionary_brotli.py) uses 256 KiB and up to 900 rows within
32 MiB. Measured on the 2026-10-19 ship-time database (22,745 items, 1,126
templates), 4 seeds each, packed with rebuild_database.py --pack
Content,TooltipTexts --page-size 4096, .br with brotli -q 11 -w 24:

    dictionary, sample      stored by seed (MB)           file          .br
    256 KiB, 900 rows       47.92 47.99 47.82 47.89   90,726,400   72,436,600
    768 KiB, 900 rows       53.04 51.75 46.19 46.30   89,604,096   70,924,003
    256 KiB, 3,600 rows     47.54 47.50 47.53 47.53   90,419,200   72,191,833
    512 KiB, 3,600 rows     46.47 46.63 46.46 46.37   89,526,272   71,148,049
    768 KiB, 3,600 rows     45.65 45.98 45.69 45.71   89,063,424   70,517,587
    1 MiB, 3,600 rows       63.20 45.53 45.24 45.22   88,875,008   70,131,363

The small sample is what limits a large dictionary: with it, two of four 768
KiB seeds stored more than 256 KiB did, and zstd could not fill a 1 MiB
dictionary (it returned 965,307 bytes, which stored 7.7% more than 256 KiB on a
probe of 1,500 rows). The brotli CLI's window is not the cause: an explicit -w 24
compressed every probe row to the same bytes as the default. With the larger
sample, 768 KiB saves 1,662,976 file and 1,919,013 .br bytes against the first
mint's settings. Its seeds spread 0.33 MB (256 KiB's: 0.16 MB on the small
sample, 0.03 MB on the large), but its worst seed still stores less than 512
KiB's best. 1 MiB stored 0.43 MB less at its best, but one seed in four stored
17.55 MB more than 768 KiB's best, so it is not the default. On a Pixel 6 Pro
the app showed no measurable difference in page load time or memory between the
256 KiB database and a 768 KiB one. That one was trained on the small sample
(the second row), so its dictionary is this default's size but not its bytes.

From documentation version 3.0.0 on (ADFA-3305), every Templates row is stored
Brotli-compressed against the same dictionary, so a re-mint carries the
templates across too. When the newest DocumentationDatabaseVersion row declares
major 3 or later (read as the app reads it), every template must decode against
the outgoing dictionary -- one that does not stops the run before any training,
since the app could not read it either. The templates join the training sample
as one more doc set, "(templates)", weighted by their stored bytes like the
others; each candidate recompresses them along with the Content rows, and the
winner is the candidate that stores the two together smallest; the dictionary in
place is measured the same way, Content and templates as stored, so the
keep-current comparison weighs like with like. They are written in the same
transaction. Below major 3, templates are plain text and are left
exactly as they are. On the 2026-10-19 ship-time build (1,126 templates, the
a/ pages' among them), the four default seeds stored the templates in 860,808
to 908,471 bytes, beside 46.96 to 47.10 MB of Content, and the templates
decided the outcome: seed 20821, the only one to beat the dictionary in place,
stored 73 bytes more Content than it and 51,657 bytes less in templates.

Every row is proved to round-trip against the new dictionary before it is
written. The backup (VACUUM INTO, unless --no-backup) is taken only then, just
before the write: a run that refuses, or keeps the dictionary in place, writes
nothing at all, backup included, and says why on an "error:" line. Afterwards,
run verify_remint_dictionary.py against a copy of the
original database: it re-reads every row through both dictionaries and requires
the plaintexts to match. That check is not paranoia -- a row recompressed
against a mismatched dictionary decodes without error into *different* bytes
(measured: 38% of the time), so nothing at runtime would catch it.

Usage:
    cp documentation.db remint.db
    uv run remint_dictionary.py remint.db --seeds 4
    uv run verify_remint_dictionary.py documentation.db remint.db && mv remint.db documentation.db
"""
import argparse
import atexit
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import brotli

from migrate_content_to_dictionary_brotli import (
    BATCH_ROWS, DEFAULT_SAMPLE_SEED, collect_training_samples, load_base_rows, read_item, write_item,
)
from populate_db import DictionaryCompressor, backup_database, train_dictionary

_thread_local = threading.local()
# Every DictionaryCompressor the current pass's workers made. Each holds a temp directory, and atexit keeps it alive
# until the process ends, so the pass closes them itself: at 24 workers and 4 seeds they were about 192 directories,
# 48 MiB, in /tmp, which can be RAM.
_pass_compressors = []
_pass_lock = threading.Lock()

# A re-mint's defaults (ADFA-3305), larger than the first mint's (populate_db.DEFAULT_DICT_SIZE and the sample in
# migrate_content_to_dictionary_brotli): see "Dictionary size" in the docstring.
REMINT_DICT_SIZE = 768 * 1024
REMINT_SAMPLE_SIZE = 1200
REMINT_TRAINING_BYTES = 128 * 1024 * 1024

# From this documentation major on, every Templates row is Brotli-compressed against the shared dictionary.
TEMPLATES_COMPRESSED_FROM_MAJOR = 3
# Templates are sampled under this pseudo path, so they form a doc set of their own.
TEMPLATE_SAMPLE_PREFIX = "(templates)/"


def declared_major(conn) -> int | None:
    """The major the newest DocumentationDatabaseVersion row declares, read the way the app's
    DatabaseVersionResolver reads it (newest changeTime, then newest rowid); None without one."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'DocumentationDatabaseVersion'"
                    ).fetchone() is None:
        return None
    row = conn.execute("SELECT major FROM DocumentationDatabaseVersion ORDER BY changeTime DESC, rowid DESC LIMIT 1"
                       ).fetchone()
    return row[0] if row else None


def templates_compressed(conn) -> bool:
    major = declared_major(conn)
    return major is not None and major >= TEMPLATES_COMPRESSED_FROM_MAJOR


def load_templates(conn, old_dictionary: bytes) -> list:
    """Every Templates row of a database whose templates are compressed, as (id, name, stored, plaintext).
    Raises RuntimeError naming each one that does not decode against the outgoing dictionary."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'Templates'").fetchone() is None:
        return []
    templates, unreadable = [], []
    with DictionaryCompressor(old_dictionary) as old:
        for template_id, name, content in conn.execute("SELECT id, name, content FROM Templates ORDER BY id"):
            stored = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            try:
                templates.append((template_id, name, stored, old.decompress(stored)))
            except RuntimeError:
                unreadable.append(name)
    if unreadable:
        raise RuntimeError(f"{len(unreadable)} template(s) do not decode against the outgoing dictionary, so "
                           f"the app could not read them either; nothing written: {', '.join(unreadable[:10])}")
    return templates


def convert_templates(templates: list, old_dictionary: bytes, new_dictionary: bytes,
                      max_workers: int | None) -> tuple:
    """Every template recompressed against new_dictionary and proved to decode back, written nowhere:
    a list of (id, recompressed bytes), the stored total, and the templates that failed."""
    def one(template):
        template_id, name, _stored, plain = template
        _old, new = _compressors(old_dictionary, new_dictionary)
        data = new.compress(plain)
        try:
            if new.decompress(data) != plain:
                return template_id, name, None, "recompressed bytes do not decode back to the same plaintext"
        except RuntimeError as exc:
            return template_id, name, None, f"recompressed bytes failed to decode: {exc}"
        return template_id, name, data, None

    # A pool per pass, as in convert_all, so each thread's cached compressors hold this candidate; closed when the
    # pass ends, as convert_all closes its own, or their temp dictionaries would outlive it.
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(one, templates))
    finally:
        _close_pass_compressors()
    converted = [(template_id, data) for template_id, _name, data, problem in results if problem is None]
    problems = [(f"template {name}", problem) for _id, name, _data, problem in results if problem]
    return converted, sum(len(data) for _id, data in converted), problems


def _compressors(old_dictionary: bytes, new_dictionary: bytes) -> tuple:
    """One pair of DictionaryCompressors per worker thread; each writes its
    dictionary to a temp file once, so this avoids doing that per row."""
    pair = getattr(_thread_local, "pair", None)
    if pair is None:
        pair = (DictionaryCompressor(old_dictionary), DictionaryCompressor(new_dictionary))
        _thread_local.pair = pair
        with _pass_lock:
            _pass_compressors.extend(pair)
    return pair


def _close_pass_compressors() -> None:
    with _pass_lock:
        opened = list(_pass_compressors)
        _pass_compressors.clear()
    for compressor in opened:
        compressor.close()
        atexit.unregister(compressor.close)


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


def convert_all(conn, base_rows: list, old_dictionary: bytes, new_dictionary: bytes,
                max_workers: int | None) -> tuple:
    """Every row recompressed against new_dictionary, written nowhere: a list of
    (row, recompressed bytes), the stored total, and the rows that failed."""
    converted, problems, total = [], [], 0
    # A pool per pass: its threads are new, so the compressors each one caches
    # (_compressors) hold this pass's candidate dictionary, not an earlier one's.
    try:
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
                    total += result["after"]
                    converted.append((r, result["data"], result["before"]))
    finally:
        _close_pass_compressors()
    return converted, total, problems


def remint(conn, sample_size: int = REMINT_SAMPLE_SIZE, dict_size: int = REMINT_DICT_SIZE,
           training_bytes: int = REMINT_TRAINING_BYTES, seed: int = DEFAULT_SAMPLE_SEED,
           max_workers: int | None = None, seeds: int = 1, before_write=None) -> dict:
    """Re-mints in one transaction, with the best of `seeds` candidate
    dictionaries (sample seeds seed, seed + 1, ...), unless none of them stores
    less than the current dictionary: then nothing is written, and the stats say
    kept = "current". Raises RuntimeError, having written nothing, if any row or
    template fails to convert. In a database whose templates are compressed (see
    TEMPLATES_COMPRESSED_FROM_MAJOR) they are sampled, recompressed and counted
    with the Content rows, in each candidate's total and the current dictionary's
    alike. `before_write` is called once every check has passed, just before the
    write, and not at all when nothing is to be written: main() takes its backup
    there."""
    row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
    if row is None or not row[0]:
        raise RuntimeError("this database has no CompressionDictionary to re-mint; "
                           "run migrate_content_to_dictionary_brotli.py instead")
    old_dictionary = row[0]
    base_rows = load_base_rows(conn)

    templates = []
    if templates_compressed(conn):
        clashing = [r[0] for r in base_rows if r[0].startswith(TEMPLATE_SAMPLE_PREFIX)]
        if clashing:
            raise RuntimeError(f"Content paths start with {TEMPLATE_SAMPLE_PREFIX!r}, the doc set templates are "
                               f"sampled as; nothing written: {', '.join(clashing[:10])}")
        templates = load_templates(conn, old_dictionary)
    sampled_templates = {TEMPLATE_SAMPLE_PREFIX + name: stored for _id, name, stored, _plain in templates}
    sample_rows = base_rows + [(path, None, None, None, len(stored)) for path, stored in sampled_templates.items()]

    def read(c, path):
        return sampled_templates[path] if path in sampled_templates else read_item(c, path)

    best, candidates, parts = None, [], []
    for candidate_seed in range(seed, seed + max(1, seeds)):
        with DictionaryCompressor(old_dictionary) as old:
            samples = collect_training_samples(
                conn, sample_rows, sample_size, training_bytes, candidate_seed,
                decode=lambda stored: decode_outgoing(old, stored) or b"", read=read,
            )
        if not samples:
            raise RuntimeError("no training samples could be decoded; refusing to train on nothing")
        new_dictionary = train_dictionary(samples, dict_size)
        converted, content_total, problems = convert_all(conn, base_rows, old_dictionary, new_dictionary,
                                                         max_workers)
        converted_templates, template_total, template_problems = convert_templates(
            templates, old_dictionary, new_dictionary, max_workers)
        problems += template_problems
        if problems:
            for path, why in problems[:10]:
                print(f"error: {path}: {why}", file=sys.stderr)
            raise RuntimeError(f"{len(problems)} row(s) failed to convert; nothing written")
        total = content_total + template_total
        candidates.append((candidate_seed, total))
        parts.append((candidate_seed, content_total, template_total))
        if seeds > 1:
            print(f"Sample seed {candidate_seed}: {total:,} stored bytes"
                  + (f" ({content_total:,} Content, {template_total:,} Templates)" if templates else ""),
                  file=sys.stderr)
        if best is None or total < best[2]:
            best = (candidate_seed, new_dictionary, total, converted, converted_templates)
        # Only the best candidate's rows are kept: holding this pass's too would keep three passes' rows in memory
        # while the next pass builds its own.
        del converted, converted_templates

    chosen_seed, new_dictionary, total, converted, converted_templates = best
    # What the outgoing dictionary stores, from the same pass: every row's size as read, and every template's, so
    # it is weighed like the candidates' totals, which count both.
    current = sum(c[2] for c in converted) + sum(len(t[2]) for t in templates)
    stats = {"items": len(base_rows), "templates": len(templates), "before": current, "candidates": candidates,
             "candidate_parts": parts, "best_candidate": (chosen_seed, total)}
    if total >= current:
        # No candidate beats the dictionary in place, so keep it and write nothing: a re-mint after a small
        # content change once stored 110,803 bytes more than the dictionary it replaced.
        return {**stats, "kept": "current", "after": current, "dictionary_bytes": len(old_dictionary),
                "seed": None}
    if before_write is not None:
        before_write()
    conn.execute("BEGIN")
    for r, data, _before in converted:
        write_item(conn, r[0], r[1], r[2], r[3], data)
    for template_id, data in converted_templates:
        conn.execute("UPDATE Templates SET content = ? WHERE id = ?", (data, template_id))
    conn.execute("UPDATE CompressionDictionary SET data = ? WHERE id = 1", (new_dictionary,))
    conn.commit()
    return {**stats, "kept": "candidate", "after": total, "dictionary_bytes": len(new_dictionary),
            "seed": chosen_seed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db_path", type=Path, help="database to re-mint; work on a copy")
    parser.add_argument("--sample-size", type=int, default=REMINT_SAMPLE_SIZE,
                        help=f"rows to aim for when training (default: {REMINT_SAMPLE_SIZE}); at most three "
                             f"times this many rows are drawn, within --training-bytes")
    parser.add_argument("--training-bytes", type=int, default=REMINT_TRAINING_BYTES,
                        help=f"plaintext byte budget for training (default: {REMINT_TRAINING_BYTES:,})")
    parser.add_argument("--sample-seed", type=int, default=DEFAULT_SAMPLE_SEED,
                        help="seed for the stratified sample, so a dictionary's training set stays "
                             "reproducible (default: %(default)s)")
    parser.add_argument("--seeds", type=int, default=4,
                        help="candidate dictionaries to train, one per consecutive sample seed from "
                             "--sample-seed; the one that stores smallest is kept, at one full "
                             "recompression pass each (default: %(default)s)")
    parser.add_argument("--dict-size", type=int, default=REMINT_DICT_SIZE,
                        help=f"new dictionary size in bytes (default: {REMINT_DICT_SIZE})")
    parser.add_argument("--max-workers", type=int, default=None, help="worker threads for recompression")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the VACUUM INTO backup, otherwise taken just before the write (for a copy you "
                             "already treat as disposable)")
    args = parser.parse_args()

    if not args.db_path.is_file():
        sys.exit(f"error: {args.db_path} does not exist")

    def backup():
        print(f"Backing up {args.db_path}...", file=sys.stderr)
        print(f"Backup written to {backup_database(args.db_path)}", file=sys.stderr)

    conn = sqlite3.connect(args.db_path)
    try:
        stats = remint(conn, args.sample_size, args.dict_size, args.training_bytes,
                       args.sample_seed, args.max_workers, args.seeds,
                       before_write=None if args.no_backup else backup)
    except RuntimeError as exc:
        # Every refusal is raised before the write, so before the backup too: one line, not a traceback.
        sys.exit(f"error: {exc}")
    finally:
        conn.close()

    before, after = stats["before"], stats["after"]
    if stats["kept"] == "current":
        best_seed, best_total = stats["best_candidate"]
        print(f"Kept the current dictionary: no candidate stores less than its {before:,} bytes (the best, "
              f"sample seed {best_seed}, stores {best_total:,}). Nothing was written.")
        return

    print("Vacuuming database to reclaim freed space...", file=sys.stderr)
    vacuum_conn = sqlite3.connect(args.db_path)
    try:
        vacuum_conn.execute("VACUUM")
    finally:
        vacuum_conn.close()

    templates = f" and {stats['templates']:,} template(s)" if stats["templates"] else ""
    print(f"Re-minted {stats['items']:,} item(s){templates} against a {stats['dictionary_bytes']:,}-byte dictionary "
          f"from sample seed {stats['seed']}: {before:,} -> {after:,} bytes "
          f"({100 * (before - after) / before:.1f}% smaller)")
    print("Now run verify_remint_dictionary.py <original> <this file> before putting it in place.")


if __name__ == "__main__":
    main()
