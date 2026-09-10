#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "scour",
#     "cairosvg",
# ]
# ///
"""
update_media_references.py

Fixes up the references left dangling by optimize_db_media.py --webp, which
converts stored media to WEBP under a new path/extension and deletes the old
row without touching the pages that link to it. This script rewrites those
links, across every doc set, so pages stop pointing at filenames that no
longer exist.

How the rename map is derived - by diffing this database against the
pre-conversion copy given as --before (the backup optimize_db_media.py makes
for itself): an image path present there and gone here, whose directory and
stem now hold a file with a different extension, was converted, and only
those names are rewritten.

That diff is deliberate rather than inferring renames from this database
alone. Inference - "for every stored <stem>.webp, treat <stem>.png as a name
that must have converted into it" - looks self-contained but invents renames
for images that were ALWAYS WEBP: measured against a real database it
produced 4530 candidate names where only 745 files had actually been
converted, and each spurious entry would rewrite references to an unrelated
asset this database never stored (an external "/images/foo.png", say) into a
link to a WEBP that has nothing to do with it. The diff cannot make that
mistake, because a name only enters the map when a file really did leave.

Why matching on the filename is safe here, rather than on the full stored
path: the conversion changes only the extension, and references are written
in whatever form each doc set happens to use - root-relative
("/images/foo.png"), relative ("media/static_..._foo.gif",
"./images/foo.gif"), or absolute URL - with no single form that maps onto
Content.path. The filename is the one component every form shares. It's
unambiguous in this database because no two images share a basename within a
doc set, and no converted basename collides with an image that was left
alone (both verified before this was written). Matches are still anchored so
a filename only counts when it appears as a reference - not as a substring
of a longer name ("my_copy.png" when rewriting "copy.png") and not with
trailing junk.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards. --dry-run does all the work and reports what
would change without writing. Decompression/recompression is parallelised
because a dictionary-compressed database shells out to the brotli CLI per
row, which is where the time goes.

    uv run scripts/update_media_references.py documentation.db --dry-run
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import (
    BrotliCodec, CHUNK_SIZE, backup_database, clear_fragment_slots, is_continuation_path,
    load_dictionary, owned_fragment_paths, reassemble,
)

# Content types whose stored text can carry a link to a media file.
TEXT_TYPES = ("text/html", "application/json", "text/css", "text/markdown",
              "text/javascript", "text/plain", "application/xml")
# A "<path>-<N>" chunk continuation row, which vanishes with its base rather
# than being renamed.
_FRAGMENT_RE = re.compile(r"-\d+$")


def image_paths(conn) -> set:
    return {
        row[0] for row in conn.execute(
            "SELECT c.path FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value LIKE 'image/%'"
        )
    }


def check_same_lineage(conn, before_conn) -> str:
    """Returns a complaint if --before doesn't look like a pre-conversion copy
    of this same database, or "" if it does.

    Cheap insurance against a mistyped or tab-completed path: several
    same-named backups sit side by side, and pointing this at the wrong one
    yields a plausible-looking rename map that rewrites thousands of pages to
    filenames that never existed - while reporting success. Two markers that
    the conversion cannot change: the shared Brotli dictionary bytes, and the
    set of non-media paths (converting media never adds or removes a page)."""
    if load_dictionary(conn) != load_dictionary(before_conn):
        return "their CompressionDictionary bytes differ"

    def pages(c):
        return {r[0] for r in c.execute(
            "SELECT c.path FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value NOT LIKE 'image/%'")}

    here, there = pages(conn), pages(before_conn)
    if not there:
        return "it contains no non-image content at all"
    drift = len(here ^ there) / len(there)
    if drift > 0.01:
        return (f"{len(here ^ there)} of {len(there)} non-image paths differ ({drift:.0%}); "
                "converting media does not add or remove pages")
    return ""


def build_rename_map(conn, before_conn, logger=print) -> dict:
    """{old_basename: new_basename} for every image that left `before_conn`
    and came back under a different extension here. See the module docstring
    for why this is a diff rather than an inference.

    Chunk continuation rows ("<path>-2") are skipped: they disappear because
    their base was rewritten, which is a deletion, not a rename. Anything
    whose stem gained no replacement, or gained an ambiguous one, is reported
    and left out rather than guessed at."""
    before, after = image_paths(before_conn), image_paths(conn)
    gone = {p for p in before - after if not _FRAGMENT_RE.search(p)}
    by_stem = {}
    for path in after - before:
        by_stem.setdefault(path.rsplit(".", 1)[0], []).append(path)

    renames, unresolved = {}, []
    for old_path in sorted(gone):
        candidates = by_stem.get(old_path.rsplit(".", 1)[0], [])
        if len(candidates) != 1:
            unresolved.append(old_path)
            continue
        old_name = old_path.rsplit("/", 1)[-1]
        new_name = candidates[0].rsplit("/", 1)[-1]
        # Two doc sets can hold same-named files; both convert to the same new
        # name, so an identical mapping is agreement, not a conflict.
        if renames.get(old_name, new_name) != new_name:
            unresolved.append(old_path)
            continue
        renames[old_name] = new_name
    if unresolved:
        logger(f"note: {len(unresolved)} removed image(s) had no unambiguous replacement and are "
               f"not being rewritten (e.g. {unresolved[0]})")
    return renames


def build_pattern(renames: dict) -> re.Pattern:
    """One alternation over every old filename, anchored so it only matches a
    filename used as a reference: not preceded by a character that would make
    it the tail of a longer name (so "copy.png" never matches inside
    "my_copy.png"), and not followed by one that would make it a prefix of
    something else. Longest names first so an alternation never settles for a
    shorter overlapping match."""
    names = sorted(renames, key=len, reverse=True)
    body = "|".join(re.escape(n) for n in names)
    # The character before must mark the start of a URL or attribute value -
    # a path separator, a quote (JSON-escaped or not), "(" for CSS url()/
    # markdown, or "=" for an unquoted attribute. Requiring one keeps a
    # filename mentioned in prose or a code sample ("save it as copy.png")
    # from being silently rewritten; 39 of the converted names are short and
    # generic enough for that to be a real risk.
    return re.compile(rf"(?<=[/\"'(=])({body})(?![A-Za-z0-9_])")


def rewrite_text(text: str, pattern: re.Pattern, renames: dict) -> tuple:
    """Returns (new_text, number_of_replacements) - a single pass over the
    original text, so a replacement can never be re-matched by another."""
    count = 0

    def repl(match):
        nonlocal count
        count += 1
        return renames[match.group(1)]

    return pattern.sub(repl, text), count


def replace_row(conn, path: str, base_length: int, stored: bytes, language_id: int,
                content_type_id: int, template_id: int, logger=print) -> None:
    """Rewrites one row's stored bytes in place, re-chunking as needed and
    preserving its language, content type and templateId (Kotlin's templated
    pages carry a non-zero templateId; losing it would stop them rendering)."""
    for fragment_path in owned_fragment_paths(conn, path, base_length):
        conn.execute("DELETE FROM Content WHERE path = ?", (fragment_path,))
    chunks = [stored[i:i + CHUNK_SIZE] for i in range(0, len(stored), CHUNK_SIZE)] or [b""]
    # A rewritten page can cross CHUNK_SIZE when it previously did not, and the
    # "<path>-1" slot it then needs may hold an unrelated row that
    # owned_fragment_paths never claimed - inserting over it would raise UNIQUE
    # and roll back the whole run. See clear_fragment_slots.
    clear_fragment_slots(conn, path, len(chunks) - 1, logger)
    conn.execute("UPDATE Content SET content = ? WHERE path = ?", (chunks[0], path))
    for number, chunk in enumerate(chunks[1:], start=1):
        conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            (f"{path}-{number}", language_id, chunk, content_type_id, template_id),
        )


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    before_path = cfg["before"]
    if not before_path.is_file():
        print(f"error: --before database {before_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    before_conn = sqlite3.connect(before_path)
    codec = None
    try:
        complaint = check_same_lineage(conn, before_conn)
        if complaint:
            print(f"error: {before_path} does not look like a pre-conversion copy of {db_path}: "
                  f"{complaint}. Refusing to rewrite references off an unrelated database.",
                  file=sys.stderr)
            return 1
        codec = BrotliCodec(load_dictionary(conn))
        renames = build_rename_map(conn, before_conn)
        if not renames:
            print("No converted media found - nothing to rewrite.")
            return 0
        print(f"Found {len(renames)} converted filename(s) to rewrite references for.")
        if cfg["verbose"]:
            for old in sorted(renames)[:10]:
                print(f"  {old} -> {renames[old]}")
        pattern = build_pattern(renames)

        placeholders = ",".join("?" * len(TEXT_TYPES))
        rows = conn.execute(
            f"SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            f"FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            f"WHERE ct.value IN ({placeholders}) ORDER BY c.path", TEXT_TYPES
        ).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        bases = [r for r in rows if not is_continuation_path(lengths, r[0])]
        print(f"Scanning {len(bases)} text row(s) for references "
              f"({len(rows) - len(bases)} chunk-continuation row(s) folded into their base)...")

        # Reassembly is done here, on the main thread, and the whole stored blob
        # handed to the workers: a sqlite3 connection may only be used from the
        # thread that created it, and reassemble() queries for continuation
        # rows - so calling it inside the pool blew up on precisely the large,
        # chunked pages this most needs to handle (j/html/api/index-all.html).
        # The DB work is cheap anyway; the time goes on brotli.
        work = [(row[0], reassemble(conn, row[0], row[1]), len(row[1]), *row[2:]) for row in bases]

        def scan(item):
            """Decompress one row's bytes and rewrite them if they mention a
            converted file. Pure CPU/subprocess - touches no database."""
            path, full, base_len, language_id, content_type_id, template_id, compression = item
            try:
                raw = codec.decompress(full, compression)
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return ("error", path, str(exc))
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Binary content filed under a text content type - this database
                # stores .ogv/.webm video as text/plain. It cannot contain a
                # textual reference, so skip it rather than failing the run.
                return ("binary", path, None)
            new_text, hits = rewrite_text(text, pattern, renames)
            if not hits:
                return None
            new_stored = codec.compress(new_text.encode("utf-8"), compression)
            return ("ok", path, base_len, new_stored, language_id, content_type_id, template_id, hits)

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = [r for r in pool.map(scan, work) if r is not None]

        errors = [r for r in results if r[0] == "error"]
        binary = [r for r in results if r[0] == "binary"]
        changes = [r for r in results if r[0] == "ok"]
        if binary:
            print(f"{len(binary)} row(s) skipped: binary content stored under a text content type.")
        for _kind, path, message in errors:
            print(f"  error: could not read {path}: {message}", file=sys.stderr)

        total_hits = sum(r[7] for r in changes)
        print(f"{len(changes)} row(s) reference a converted file; {total_hits} reference(s) to rewrite.")

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        else:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                for _kind, path, base_len, new_stored, lang, ctid, tid, hits in changes:
                    replace_row(conn, path, base_len, new_stored, lang, ctid, tid,
                                lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [REF] {path}: {hits} reference(s)")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        if codec is not None:
            codec.close()
        before_conn.close()
        conn.close()

    if not cfg["dry_run"] and changes:
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()

    verb = "would rewrite" if cfg["dry_run"] else "rewrote"
    print(f"\nDone. {verb} {total_hits} reference(s) across {len(changes)} row(s); "
          f"{len(renames)} converted filename(s) known; {len(binary)} binary row(s) skipped; "
          f"{len(errors)} error(s).")
    return 1 if errors else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database whose media references should be updated")
    p.add_argument("--before", type=Path, required=True,
                   help="The pre-conversion copy of this database (the backup optimize_db_media.py wrote), "
                        "diffed against it to learn exactly which files were renamed")
    p.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    p.add_argument("--workers", type=int, default=8,
                   help="Parallel (de)compression workers (default: 8)")
    p.add_argument("--verbose", action="store_true", help="List rewritten rows and sample renames")
    args = p.parse_args()
    sys.exit(run({"db_path": args.db_path, "before": args.before, "dry_run": args.dry_run,
                  "workers": args.workers, "verbose": args.verbose}))


if __name__ == "__main__":
    main()
