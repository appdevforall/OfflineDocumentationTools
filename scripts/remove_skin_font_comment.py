#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
remove_skin_font_comment.py

Removes the commented-out Google Fonts note that Help+Manual's skin writes into
its pages. In the 2026-10-13 database 362 of the 366 text/html rows under i/
carry it, each the same 1,005-byte comment; the other four are Help+Manual
template files without it (_hmXpopup.htm, _hmXtopic.htm, _hm_print_window.htm,
_hmpermalink.html):

    <!-- Insert Google web fonts references here.
    ...
    Example of content of this file for dynamic Google Fonts:

    <link href="https://fonts.googleapis.com/css?family=Roboto:..." rel="stylesheet">
    ...
    -->

It does nothing on screen. But every tool that searches pages for internet
addresses counts it as a page loading Google Fonts: ADFA-6589 first reported 362
such loads, none of which exist. The skin itself is being fixed upstream
(ADFA-6609); until that ships, this step takes the note out of the pages.

Only this one comment goes. The same pages carry comments that must stay -
<!--ZOOMSTOP--> / <!--ZOOMRESTART-->, which i/js/highlight.js uses to limit
search highlighting, and skin markers such as <!--HMSKIN BODY START--> - so
comments in general are never touched. A comment is removed only when it begins,
after leading whitespace, with the note's opening sentence AND its content hashes
to the digest of the known note (KNOWN_NOTE_SHA256 below). Anything else that
carries the opening sentence - a comment that has been edited, one that is never
closed, the sentence anywhere other than at the start of a comment - is left
alone, named, and makes the run exit non-zero at the end. A changed skin note is
therefore never removed on a guess, and never kept without anyone being told.

The edit is made on the decoded bytes, not on re-encoded text, and removes just
the "<!--" ... "-->" bytes: the whitespace around the comment stays, as does
every other byte of the page. Each changed row is re-compressed the way its
ContentTypes row says (against the shared CompressionDictionary when the
database has one, which is what BrotliCodec handles); rows without the note keep
their stored bytes exactly, never re-compressed. A page over the 1 MiB chunk size
is read whole across its "<path>-1", "-2", ... rows and written back re-chunked.

Where it goes in the pipeline: FIRST, before set_content_type_compression.py and
before the `cp DB migrated.db` snapshot that update_media_references.py --before
diffs against. update_media_references.py --save-originals records each page it
rewrites by the sha256 of its decoded text, and --restore-originals leaves alone
any page whose text no longer matches that record, so removing the note after it
would leave every page that both steps edit unrestorable (i/hmftsearch.html is
one: a GIF conversion rewrites a link in it). Run first, the note is already
gone from both the snapshot and the database, so neither --before's lineage
check (dictionary bytes and non-media paths, which this step does not change)
nor the bundle's digests see a difference.

Runs in one transaction (rolled back on any error), backs the database up first,
and VACUUMs afterwards - all three only when there is something to change, so a
second run writes nothing at all. --dry-run does all the work and reports what
would change without writing.

    uv run scripts/remove_skin_font_comment.py documentation.db --dry-run
    uv run scripts/remove_skin_font_comment.py documentation.db

Recovering: the timestamped backup taken before the first write is the whole
undo.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import (
    BrotliCodec, backup_database, digest, human, is_continuation_path, load_dictionary, reassemble,
)
from update_media_references import replace_row

# How the note is found: its first sentence, which is distinctive enough that
# any other appearance of it in a page is worth reporting.
OPENING = b"Insert Google web fonts references here."

# sha256 of the note's content - every byte between "<!--" and "-->", including
# the leading space and the CRLF line endings - as Help+Manual's skin wrote it
# into 362 of the 2026-10-13 database's i/ pages (1,005 bytes, one digest).
# A comment opening with OPENING but hashing to anything else is a different
# note and is reported, not removed.
KNOWN_NOTE_SHA256 = "63838dc41d48cb0c3d0dc09d94b08e2c20e9f95cb9c033d2d875b1ef4b60ecea"

COMMENT_OPEN = b"<!--"
COMMENT_CLOSE = b"-->"

CONTENT_TYPE = "text/html"


def find_notes(page: bytes) -> tuple:
    """([(start, end)] byte span of every copy of the known note in `page`,
    [(offset, reason)] for every other appearance of OPENING).

    Found from the sentence outwards rather than by matching every comment in
    the page: a stray "<!--" earlier on (inside a script string, say, or an
    empty "<!-->") would make a comment-by-comment scan pair the wrong opener
    with the wrong closer and miss the note without a word. Here every
    appearance of the sentence is accounted for, either as a span to remove or
    as a reason it was not."""
    spans, odd = [], []
    search_from = 0
    while True:
        at = page.find(OPENING, search_from)
        if at < 0:
            return spans, odd
        opener = page.rfind(COMMENT_OPEN, 0, at)
        if opener < 0 or page[opener + len(COMMENT_OPEN):at].strip():
            odd.append((at, "the opening sentence appears, but not at the start of a comment"))
            search_from = at + len(OPENING)
            continue
        closer = page.find(COMMENT_CLOSE, at)
        if closer < 0:
            odd.append((opener, "a comment starts with the opening sentence but is never closed"))
            return spans, odd
        content = page[opener + len(COMMENT_OPEN):closer]
        if digest(content) == KNOWN_NOTE_SHA256:
            spans.append((opener, closer + len(COMMENT_CLOSE)))
        else:
            odd.append((opener, f"a comment starts with the opening sentence but differs from the known "
                                f"note ({len(content):,} bytes, sha256 {digest(content)[:12]}...)"))
        search_from = closer + len(COMMENT_CLOSE)


def remove_spans(page: bytes, spans: list) -> bytes:
    """`page` with each (start, end) span cut out and nothing else changed."""
    pieces, read = [], 0
    for start, end in spans:
        pieces.append(page[read:start])
        read = end
    pieces.append(page[read:])
    return b"".join(pieces)


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    codec = None
    try:
        codec = BrotliCodec(load_dictionary(conn))
        rows = conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value = ? ORDER BY c.path", (CONTENT_TYPE,)
        ).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        bases = [row for row in rows if not is_continuation_path(lengths, row[0])]
        print(f"Scanning {len(bases)} {CONTENT_TYPE} row(s) for Help+Manual's Google Fonts note "
              f"({len(rows) - len(bases)} chunk-continuation row(s) folded into their base)...")

        # Reassembled here on the main thread, not in the pool: a sqlite3
        # connection may only be used from the thread that created it, and
        # reassemble() queries for continuation rows. The time goes on brotli,
        # which is what the pool is for.
        work = [(row[0], reassemble(conn, row[0], row[1]), len(row[1]), *row[2:]) for row in bases]

        def scan(item):
            """Decompress one row and cut the known note out of it. Pure
            CPU/subprocess - touches no database."""
            path, full, base_len, language_id, content_type_id, template_id, compression = item
            try:
                raw = codec.decompress(full, compression)
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return ("skip", path, [f"could not be read: {exc}"])
            if OPENING not in raw:
                return None
            spans, odd = find_notes(raw)
            # Anything other than the known note is left in place and reported;
            # a known copy on the same page still goes, since it is identified
            # by its digest and nothing about its neighbour changes that.
            left = [f"at byte {offset:,}, {reason}; left in place" for offset, reason in odd]
            if not spans:
                return ("skip", path, left)
            new_raw = remove_spans(raw, spans)
            new_stored = codec.compress(new_raw, compression)
            return ("ok", path, left, base_len, new_stored, language_id, content_type_id, template_id,
                    len(raw) - len(new_raw), len(full) - len(new_stored), len(spans))

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = [r for r in pool.map(scan, work) if r is not None]

        changes = [r for r in results if r[0] == "ok"]
        skipped = [r for r in results if r[0] == "skip"]
        flagged = [r for r in results if r[2]]
        decoded_saved = sum(r[8] for r in changes)
        stored_saved = sum(r[9] for r in changes)

        if changes:
            print(f"{len(changes)} row(s) carry the note; removing it saves {human(decoded_saved)} decoded "
                  f"byte(s) and {human(stored_saved)} stored byte(s).")
        else:
            print("No row carries the note - nothing to remove.")
        for kind, path, reasons, *_rest in flagged:
            for reason in reasons:
                print(f"  {'skipped' if kind == 'skip' else 'changed'} {path}: {reason}", file=sys.stderr)
        if skipped:
            print(f"{len(skipped)} row(s) skipped and left exactly as they were (listed above).",
                  file=sys.stderr)

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        elif changes:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                for _kind, path, _left, base_len, new_stored, lang, ctid, tid, decoded, stored, notes in changes:
                    replace_row(conn, path, base_len, new_stored, lang, ctid, tid,
                                lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [NOTE] {path}: removed {notes} note(s), {human(decoded)} decoded / "
                              f"{human(stored)} stored byte(s) saved")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    if not cfg["dry_run"] and changes:
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()

    verb = "would change" if cfg["dry_run"] else "changed"
    print(f"\nDone. {verb} {len(changes)} row(s), saving {human(decoded_saved)} decoded and "
          f"{human(stored_saved)} stored byte(s); {len(skipped)} row(s) skipped.")
    if flagged:
        print(f"error: {len(flagged)} row(s) carry the note's opening sentence in a form this tool does not "
              "recognise, or could not be read. That text was left in place; look at those rows before "
              "relying on this step.", file=sys.stderr)
    return 1 if flagged else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work and report what would change, without writing or backing up")
    p.add_argument("--workers", type=int, default=8,
                   help="Threads used to decompress and recompress rows (default: 8)")
    p.add_argument("--verbose", action="store_true", help="Log every page edited")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "workers": args.workers,
                  "verbose": args.verbose}))


if __name__ == "__main__":
    main()
