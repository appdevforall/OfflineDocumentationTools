#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
bundle_remote_images.py

Stores in the database the diagrams that API reference pages load from
developer.android.com, and points the pages at the stored copies (ADFA-6589).
In the 2026-10-13 database that is 21 <img> elements on 11 pages under
a/android/ and a/androidx/ - MediaPlayer's state diagram, Activity's
lifecycle, Media3's Timeline figures and others - showing 20 files:

    <img alt="MediaPlayer State diagram" border="0"
         src="https://developer.android.com/images/mediaplayer_state_diagram.gif"/>

Offline, each shows as a broken image. 19 of the files are not in the
database. The 20th, Activity's lifecycle diagram, already is, as the WebP the
lifecycle guide page shows (ADFA-6614).

The files come from this repository, not the internet: each was downloaded
once, on 2026-10-17, into remote_media/adfa-6589/ beside this script, where
files.tsv records its source URL, the Content path it is stored under, its
content type, size, sha256 and license. Every file is checked against that
record before the database is opened; if any is missing or differs, the run
writes nothing and exits non-zero, so what goes into the database is exactly
what was reviewed. A URL whose image the database already holds under
another path is listed instead in existing.tsv beside it, in the same columns
(the last is a note rather than a license), and has no file here: its pages
are pointed at that row, and nothing is stored. The row must hold exactly the
bytes existing.tsv records, under its content type; if it doesn't, it is
reported like a taken path (below).

Each file is stored the way the database already stores developer.android.com
media (682 rows under a/devsite/media/): path "a/devsite/media/" plus the URL
path with "/" turned into "_" (.../images/axis_globe.png ->
a/devsite/media/images_axis_globe.png), languageID 1, templateId 0,
and the ContentTypes row named in files.tsv, compressed as that row says
(against the shared CompressionDictionary for a brotli type in a database that
has one). Each <img src> is rewritten to the stored file as a relative path,
the form every existing reference to a/devsite/media/ uses - from
a/android/media/MediaPlayer.html that is
../../devsite/media/images_mediaplayer_state_diagram.gif.

Only an exact src="URL" on an <img> is rewritten. Any other appearance of one
of these URLs - another attribute, a srcset, a link, JSON-escaped markup - is
left as it is, named, and the run exits non-zero at the end. A file is stored
only when a page still loads it from the internet, so a second run (or a run
after optimize_db_media.py has converted a stored file) adds nothing. A row
already at a file's path is used only if it holds the same bytes; one holding
anything else is reported, and the pages that load that file are left alone.

Where it goes in the pipeline: right after remove_skin_font_comment.py, before
the `cp DB migrated.db` snapshot. The new rows are then media like any other:
update_media_references.py builds its rename map from files present in that
snapshot and gone afterwards, so a file added after it would never have its
references rewritten when optimize_db_media.py --webp converts it; and, as
remove_skin_font_comment.py explains, a page edited after the snapshot could
not be put back by --restore-originals. remove_remote_loads.py goes in the same
place; the two touch different elements, so their order does not matter.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards - all three only when there is something to
change, so a second run writes nothing at all. --dry-run does all the work and
reports what would change without writing.

    uv run scripts/bundle_remote_images.py documentation.db --dry-run
    uv run scripts/bundle_remote_images.py documentation.db

Recovering: the timestamped backup taken before the first write is the whole
undo.
"""
from __future__ import annotations

import argparse
import posixpath
import re
import sqlite3
import sys
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from optimize_db_media import (
    BrotliCodec, backup_database, digest, human, is_continuation_path, load_dictionary, reassemble, write_chunks,
)
from update_media_references import replace_row

MEDIA_DIR = Path(__file__).resolve().parent / "remote_media" / "adfa-6589"
INDEX_NAME = "files.tsv"
INDEX_HEADER = "# bundle_remote_images file list v1"
INDEX_COLUMNS = ("source-url", "database-path", "content-type", "bytes", "sha256", "license")
EXISTING_NAME = "existing.tsv"
EXISTING_HEADER = "# bundle_remote_images existing-row list v1"

SOURCE_PREFIX = "https://developer.android.com/"
MEDIA_PREFIX = "a/devsite/media/"
# Characters a stored path may hold for its relative reference to need no
# percent-encoding.
_PLAIN_PATH = re.compile(r"[A-Za-z0-9._/-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
LANGUAGE_ID = 1  # en-US, as on every a/devsite/media/ row
TEMPLATE_ID = 0  # served as stored, not through a Pebble template
CONTENT_TYPE = "text/html"
# Only used to check that a rewritten src resolves to the stored file.
SERVER = "http://localhost:6174/"

# existing: the image is already in the database at database_path, and only
# pages are changed; `license` then holds existing.tsv's note.
Entry = namedtuple("Entry", "source_url database_path content_type size sha256 license existing", defaults=(False,))


def database_path_for(url: str) -> str:
    """The a/devsite/media/ path the database stores a developer.android.com file under."""
    return MEDIA_PREFIX + urlsplit(url).path.lstrip("/").replace("/", "_")


def read_index(media_dir: Path) -> tuple:
    """([Entry], [problem]) from media_dir/files.tsv and, if there is one,
    media_dir/existing.tsv. A problem is anything that keeps the record from
    describing the files exactly: a missing or foreign header, a line with the
    wrong number of fields, a malformed size or digest, a files.tsv path that
    does not follow the a/devsite/media/ naming, a URL or path listed twice in
    either list or across them."""
    entries, problems, paths, urls = [], [], set(), set()
    _read_list(media_dir / INDEX_NAME, INDEX_HEADER, False, entries, problems, paths, urls)
    if (media_dir / EXISTING_NAME).exists():
        _read_list(media_dir / EXISTING_NAME, EXISTING_HEADER, True, entries, problems, paths, urls)
    if not any(not entry.existing for entry in entries) and not problems:
        problems.append(f"{media_dir / INDEX_NAME} lists no files")
    return entries, problems


def _read_list(path: Path, header: str, existing: bool, entries: list, problems: list, paths: set,
               urls: set) -> None:
    """Appends one list's entries and problems; `paths` and `urls` carry what
    the lists read before it hold, so a duplicate across lists is caught."""
    if not path.is_file():
        problems.append(f"{path} does not exist")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != header:
        problems.append(f"{path}: the first line is not {header!r}")
        return
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != len(INDEX_COLUMNS):
            problems.append(f"{path}:{number}: expected {len(INDEX_COLUMNS)} tab-separated fields, got {len(fields)}")
            continue
        url, database_path, content_type, size, sha, license_ = fields
        parts = urlsplit(url)
        if not url.startswith(SOURCE_PREFIX) or parts.query or parts.fragment:
            problems.append(f"{path}:{number}: {url!r} is not a {SOURCE_PREFIX} URL without a query or fragment")
            continue
        if not existing and database_path != database_path_for(url):
            problems.append(f"{path}:{number}: {database_path!r} is not the a/devsite/media/ path for {url!r}")
            continue
        if not _PLAIN_PATH.fullmatch(database_path):
            problems.append(f"{path}:{number}: {database_path!r} would need percent-encoding in a relative src")
            continue
        if not size.isdigit() or not _SHA256.fullmatch(sha):
            problems.append(f"{path}:{number}: expected a byte count and a sha256, got {size!r} and {sha!r}")
            continue
        if not content_type.startswith("image/"):
            problems.append(f"{path}:{number}: {content_type!r} is not an image/ content type")
            continue
        if database_path in paths or url in urls:
            problems.append(f"{path}:{number}: {url} is listed twice")
            continue
        paths.add(database_path)
        urls.add(url)
        entries.append(Entry(url, database_path, content_type, int(size), sha, license_, existing))


def check_files(media_dir: Path, entries: list) -> tuple:
    """({database path: file bytes}, [problem]): each listed file, named after
    its database path's last component, read and checked against its recorded
    size and sha256. An existing.tsv entry has no file."""
    data, problems = {}, []
    for entry in entries:
        if entry.existing:
            continue
        file_path = media_dir / posixpath.basename(entry.database_path)
        if not file_path.is_file():
            problems.append(f"{file_path} is missing")
            continue
        blob = file_path.read_bytes()
        if len(blob) != entry.size or digest(blob) != entry.sha256:
            problems.append(f"{file_path}: {len(blob):,} bytes with sha256 {digest(blob)}, but {INDEX_NAME} "
                            f"records {entry.size:,} bytes with sha256 {entry.sha256}")
            continue
        data[entry.database_path] = blob
    return data, problems


def relative_reference(page_path: str, database_path: str) -> str:
    """`database_path` as a src relative to `page_path`, checked to resolve to it."""
    reference = posixpath.relpath(database_path, posixpath.dirname(page_path) or ".")
    if urljoin(SERVER + page_path, reference) != SERVER + database_path:
        raise RuntimeError(f"{reference} on {page_path} does not resolve to {database_path}")
    return reference


def find_sites(page: bytes, pattern: re.Pattern) -> tuple:
    """([(start, end, url)] for each src="URL" on an <img>, [(offset, reason)] for
    every other appearance of a listed URL)."""
    sites, odd = [], []
    for match in pattern.finditer(page):
        start, end = match.span()
        url = match.group(0).decode("ascii")
        tag = page.rfind(b"<", 0, start)
        if (page[start - 5:start] == b'src="' and page[start - 6:start - 5].isspace() and page[end:end + 1] == b'"'
                and tag >= 0 and b">" not in page[tag:start] and re.match(rb"<img\s", page[tag:tag + 5], re.I)):
            sites.append((start, end, url))
        else:
            odd.append((start, f"{url} appears other than as the whole src of an <img>; left in place"))
    return sites, odd


def rewrite(page_path: str, page: bytes, sites: list, by_url: dict) -> bytes:
    """`page` with each site's URL replaced by its stored file's relative path."""
    pieces, read = [], 0
    for start, end, url in sorted(sites):
        pieces.append(page[read:start])
        pieces.append(relative_reference(page_path, by_url[url].database_path).encode("ascii"))
        read = end
    pieces.append(page[read:])
    return b"".join(pieces)


def run(cfg: dict) -> int:
    db_path, media_dir = cfg["db_path"], cfg["media_dir"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    entries, problems = read_index(media_dir)
    data, file_problems = check_files(media_dir, entries) if not problems else ({}, [])
    problems += file_problems
    if problems:
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(f"error: the files in {media_dir} do not match {INDEX_NAME}; nothing was written.", file=sys.stderr)
        return 1
    print(f"{len(data)} file(s) in {media_dir} match {INDEX_NAME} (size and sha256); "
          f"{len(entries) - len(data)} URL(s) in {EXISTING_NAME} name images already in the database.")
    by_url = {entry.source_url: entry for entry in entries}

    conn = sqlite3.connect(db_path)
    codec = None
    try:
        types = {value: (type_id, compression) for type_id, value, compression in
                 conn.execute("SELECT id, value, compression FROM ContentTypes")}
        compression_of = {type_id: compression for type_id, compression in types.values()}
        missing = sorted({entry.content_type for entry in entries} - set(types))
        if missing:
            print(f"error: ContentTypes has no row for {', '.join(missing)}; nothing was written.", file=sys.stderr)
            return 1
        codec = BrotliCodec(load_dictionary(conn))
        rows = conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value = ? ORDER BY c.path", (CONTENT_TYPE,)
        ).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        bases = [row for row in rows if not is_continuation_path(lengths, row[0])]
        print(f"Scanning {len(bases)} {CONTENT_TYPE} row(s) for <img> elements loading these files from the "
              f"internet ({len(rows) - len(bases)} chunk-continuation row(s) folded into their base)...")
        pattern = re.compile(b"|".join(re.escape(url.encode("ascii")) for url in sorted(by_url, key=len, reverse=True)))

        # Reassembled on the main thread, not in the pool: a sqlite3 connection
        # may only be used from the thread that created it.
        work = [(row[0], reassemble(conn, row[0], row[1]), len(row[1]), *row[2:]) for row in bases]

        def scan(item):
            """Decompress one row and find the listed URLs in it. Touches no database."""
            path, full, base_len, language_id, content_type_id, template_id, compression = item
            try:
                raw = codec.decompress(full, compression)
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return ("skip", path, [f"could not be read: {exc}"])
            if SOURCE_PREFIX.encode("ascii") not in raw:
                return None
            sites, odd = find_sites(raw, pattern)
            if not sites and not odd:
                return None
            left = [f"at byte {offset:,}, {reason}" for offset, reason in odd]
            return ("page", path, left, raw, sites, base_len, language_id, content_type_id, template_id, compression)

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = [r for r in pool.map(scan, work) if r is not None]
        pages = [r for r in results if r[0] == "page"]
        skipped = [r for r in results if r[0] == "skip"]

        # A file is needed while some page still loads it from the internet; it is
        # stored unless an identical row is already there, and a row holding
        # anything else keeps its pages pointing where they were. An existing.tsv
        # entry is never stored: its row must already hold the recorded bytes.
        needed = sorted({url for page in pages for _start, _end, url in page[4]})
        inserts, conflicts = [], {}
        for url in needed:
            entry = by_url[url]
            row = conn.execute("SELECT content, contentTypeID FROM Content WHERE path = ?",
                               (entry.database_path,)).fetchone()
            if row is None and entry.existing:
                conflicts[url] = (f"{entry.database_path}, which {EXISTING_NAME} names for {url}, is not in the "
                                  "database; pages loading it left in place")
                continue
            if row is None:
                inserts.append(entry)
                continue
            try:
                held = codec.decompress(reassemble(conn, entry.database_path, row[0]),
                                        compression_of.get(row[1], "none"))
            except Exception as exc:  # noqa: BLE001 - reported like any other mismatch
                held = f"unreadable ({exc})".encode()
            same = (len(held) == entry.size and digest(held) == entry.sha256 if entry.existing
                    else held == data[entry.database_path])
            if not same or row[1] != types[entry.content_type][0]:
                listed_in = EXISTING_NAME if entry.existing else INDEX_NAME
                conflicts[url] = (f"{entry.database_path} already holds something other than {url} "
                                  f"(as {listed_in} records it); pages loading it left in place")
        for page in pages:
            for _start, _end, url in page[4]:
                if url in conflicts and conflicts[url] not in page[2]:
                    page[2].append(conflicts[url])
        stored = {entry.database_path: codec.compress(data[entry.database_path], types[entry.content_type][1])
                  for entry in inserts}

        def edit(page):
            """The page's sites for storable files rewritten and re-compressed."""
            _kind, path, left, raw, sites, base_len, language_id, content_type_id, template_id, compression = page
            sites = [site for site in sites if site[2] not in conflicts]
            if not sites:
                return ("skip", path, left)
            new_stored = codec.compress(rewrite(path, raw, sites, by_url), compression)
            return ("ok", path, left, base_len, new_stored, language_id, content_type_id, template_id, len(sites))

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            edited = list(pool.map(edit, pages))
        changes = [r for r in edited if r[0] == "ok"]
        skipped += [r for r in edited if r[0] == "skip"]
        flagged = [r for r in skipped + changes if r[2]]
        references = sum(r[8] for r in changes)
        reused = len(needed) - len(inserts) - len(conflicts)
        elsewhere = sum(1 for url in needed if by_url[url].existing and url not in conflicts)

        if changes:
            print(f"{len(changes)} page(s) load {len(needed) - len(conflicts)} of these file(s) from the internet, "
                  f"by {references} <img src>.")
            print(f"Storing {len(inserts)} file(s) ({human(sum(len(data[e.database_path]) for e in inserts))} "
                  f"bytes) under {MEDIA_PREFIX}; {reused} already stored, {elsewhere} of them under another "
                  f"path ({EXISTING_NAME}).")
        else:
            print("No page loads these files from the internet in a shape this tool rewrites - nothing to change.")
        for kind, path, reasons, *_rest in flagged:
            for reason in reasons:
                print(f"  {'skipped' if kind == 'skip' else 'changed'} {path}: {reason}", file=sys.stderr)
        if skipped:
            print(f"{len(skipped)} row(s) skipped and left exactly as they were (listed above).", file=sys.stderr)

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        elif changes:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            logger = lambda m: print(m, file=sys.stderr)  # noqa: E731
            conn.execute("BEGIN")
            try:
                for entry in inserts:
                    write_chunks(conn, entry.database_path, stored[entry.database_path], LANGUAGE_ID,
                                 types[entry.content_type][0], TEMPLATE_ID, logger, update_base=False)
                    if cfg["verbose"]:
                        print(f"  [STORE] {entry.database_path} <- {entry.source_url}")
                for _kind, path, _left, base_len, new_stored, lang, ctid, tid, count in changes:
                    replace_row(conn, path, base_len, new_stored, lang, ctid, tid, logger)
                    if cfg["verbose"]:
                        print(f"  [EDIT] {path}: {count} <img src> rewritten")
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

    verb = "would store" if cfg["dry_run"] else "stored"
    print(f"\nDone. {verb} {len(inserts)} file(s) and "
          f"{'would rewrite' if cfg['dry_run'] else 'rewrote'} {references} reference(s) on {len(changes)} "
          f"page(s); {len(skipped)} row(s) skipped.")
    if flagged:
        print(f"error: {len(flagged)} row(s) name these files in a shape this tool does not rewrite, could not be "
              "read, or load a file whose path already holds something else. That markup was left in place; look "
              "at those rows before relying on this step.", file=sys.stderr)
    return 1 if flagged else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--media-dir", type=Path, default=MEDIA_DIR,
                   help=f"Folder holding the files, their {INDEX_NAME} and any {EXISTING_NAME} "
                        f"(default: {MEDIA_DIR})")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work and report what would change, without writing or backing up")
    p.add_argument("--workers", type=int, default=8,
                   help="Threads used to decompress and recompress rows (default: 8)")
    p.add_argument("--verbose", action="store_true", help="Log every file stored and page edited")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "media_dir": args.media_dir, "dry_run": args.dry_run,
                  "workers": args.workers, "verbose": args.verbose}))


if __name__ == "__main__":
    main()
