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

Putting the references back (--save-originals / --restore-originals):
  optimize_db_media.py --save-originals keeps every asset's original bytes;
  this is the other half - what it takes to reconstruct the original HTML.
  --save-originals <bundle> writes two small files into that same bundle:

      <bundle>/renames.tsv     old filename -> new filename, one per line
      <bundle>/references.tsv  sha256-before, sha256-after, path, and the
                               offset of every reference this run rewrote

  That is deliberately the minimum rather than a copy of each page. A rewrite
  substitutes filenames and changes nothing else, so recording where each
  substitution landed - its character offset in the REWRITTEN text - and what
  the name used to be describes the edit exactly, in tens of bytes per
  reference instead of a megabyte per page. The two digests bracket it: the
  "after" one identifies the row this run actually produced, and the "before"
  one is what undoing the edits has to reproduce.

  Reversing cannot be inferred from renames.tsv alone, which is why the
  offsets are recorded at all. Rewriting every "foo.webp" back to "foo.png"
  would also hit references that always said foo.webp - the same mistake the
  rename map itself is diffed rather than inferred to avoid.

  Both files accumulate rather than replace, so one bundle can describe several
  passes over a database: a page rewritten twice gets two records, chained by
  their digests - the second pass's "before" is the first pass's "after".
  --restore-originals follows that chain backwards, undoing whichever record
  produced the text in hand and repeating until none matches. What decides
  whether a row came back is where that walk LANDED - on the single digest the
  chain starts from - not how many records it consumed, so a row that merely
  was not at the newest state still rewinds the whole way. A history with two
  such starting points was split by someone editing the page between passes:
  the earlier pass's offsets no longer address anything, so the row is left
  exactly as it is and named. A row whose bytes match no record at all has been
  edited since and is likewise left alone; one already at the start is
  recognised as put back already. Anything left unrestored makes the run exit
  non-zero, because those pages still link to filenames the media restore has
  just deleted.

  Run optimize_db_media.py --restore-originals over the same bundle for the
  media itself; between them the original documentation is reconstructed.

  The logs are written inside the rewriting transaction and renamed into place,
  so a bundle that cannot be written takes the rewrite down with it instead of
  leaving the database changed with no way back. Like optimize_db_media.py's,
  --save-originals is refused with --dry-run: a log of edits that were never
  made is a trap, not a record.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import (
    BrotliCodec, CHUNK_SIZE, backup_database, clear_fragment_slots, digest, is_continuation_path,
    load_dictionary, owned_fragment_paths, reassemble, write_atomically,
)

# The two files this script contributes to optimize_db_media.py's rollback
# bundle (see that module's "original-asset archive" section for the rest of the
# layout). Both are tab-separated text with "#" headers, like every other
# manifest in this pipeline, so a reviewer can read one without a tool.
BUNDLE_RENAMES = "renames.tsv"
BUNDLE_REFERENCES = "references.tsv"
RENAMES_HEADER = "# update_media_references rename map v1"
RENAMES_COLUMNS = "# old-filename\tnew-filename"
REFERENCES_HEADER = "# update_media_references reference-edit log v1"
REFERENCES_COLUMNS = ("# sha256-before\tsha256-after\tpath\tsites (offset:old-filename, comma-separated;"
                      " offsets index the rewritten text)")
# Characters a filename may not contain for these files to stay parseable.
# TSV_RESERVED breaks any column of either file; SITE_RESERVED additionally
# covers the separators inside the sites field, which only old filenames go
# into. Both are checked before anything is rewritten rather than at write
# time, so a bundle can never fail after the work is already done.
TSV_RESERVED = ("\t", "\n", "\r")
SITE_RESERVED = (",", ":") + TSV_RESERVED

# Content types whose stored text can carry a link to a media file.
#
# Three are measured carriers: over a full production database every rewritten
# row was text/html (541, page markup), text/javascript (29, javadoc's search
# UI referencing glass.png/x.png) or text/css (8, url(...) backgrounds).
#
# application/json, text/markdown and application/xml rewrote nothing in that
# particular database but are kept, because each genuinely embeds references
# elsewhere in this schema: Kotlin's nav row is application/json carrying
# literal "/k/html/images/<name>" links (insert_optimized_media.py rewrites
# exactly that row), markdown embeds images as ![](foo.png), and XML does so
# in an attribute. Scanning them costs one decode of a handful of rows.
#
# text/plain is deliberately NOT here, having been measured and found to hold
# no references at all: this schema files ~61 "j/html/api/*/module-graph.svg"
# under it - and SVG can reference a raster via <image href="..."> - but none
# of those module graphs does, while it also files 16 binary .ogv/.webm videos
# as text, the only rows that ever hit the decode guard below. The one
# text/plain row that even resembles a reference is a false positive:
# "javax.imageio.plugins.jpeg", a Java package name in j/html/api/element-list.
TEXT_TYPES = ("text/html", "application/json", "text/css", "text/markdown",
              "text/javascript", "application/xml")
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
    """Returns (new_text, sites), where sites is [(offset, old_filename)] for
    every substitution made - the offset being where the NEW name starts in
    new_text, which is what undo_rewrite needs to walk it back.

    Still a single pass over the original text, so a replacement can never be
    re-matched by another; the output is assembled piece by piece instead of
    through pattern.sub purely so those offsets can be counted as it grows."""
    pieces, sites = [], []
    read = written = 0
    for match in pattern.finditer(text):
        old_name = match.group(1)
        new_name = renames[old_name]
        pieces.append(text[read:match.start()])
        written += match.start() - read
        sites.append((written, old_name))
        pieces.append(new_name)
        written += len(new_name)
        read = match.end()
    if not sites:
        return text, []
    pieces.append(text[read:])
    return "".join(pieces), sites


def undo_rewrite(text: str, sites: list, renames: dict) -> str:
    """rewrite_text's inverse: puts each recorded site's old filename back.

    Walks the sites in offset order building a new string, rather than splicing
    repeatedly, so a page carrying hundreds of references (j/html/api/
    index-all.html) stays linear instead of quadratic. Every site is checked
    against the text actually there - a mismatch means this is not the row the
    log describes, and raises rather than writing a corrupted page."""
    pieces = []
    read = 0
    for offset, old_name in sorted(sites):
        new_name = renames.get(old_name)
        if new_name is None:
            raise RuntimeError(f"no rename recorded for {old_name!r}")
        if offset < read:
            raise RuntimeError(f"overlapping edit sites at offset {offset}")
        if text[offset:offset + len(new_name)] != new_name:
            raise RuntimeError(f"expected {new_name!r} at offset {offset}, found "
                               f"{text[offset:offset + len(new_name)]!r}")
        pieces.append(text[read:offset])
        pieces.append(old_name)
        read = offset + len(new_name)
    pieces.append(text[read:])
    return "".join(pieces)


def unloggable_names(renames: dict) -> list:
    """Filenames that could not be written into the bundle unambiguously.

    Both halves of the map are checked, not just the keys: old names go into
    the sites field and so must avoid its separators too, while new names still
    occupy a column of renames.tsv and would break it with a tab or a newline.
    Empty for every real filename in this database; the check exists so a name
    that did contain one would stop the run up front with a clear complaint
    instead of producing a log that silently cannot be parsed back."""
    bad = {old for old in renames if any(char in old for char in SITE_RESERVED)}
    bad.update(new for new in renames.values() if any(char in new for char in TSV_RESERVED))
    return sorted(bad)


def conflicting_renames(path: Path, renames: dict) -> list:
    """Old filenames this run maps somewhere other than the bundle's existing
    rename map already does.

    A bundle accumulates passes (see write_reference_log), and undoing a pass
    resolves each recorded old name through one shared map - so the same old
    name cannot mean two different new names in one bundle. It takes an unusual
    sequence to produce that (a source image re-inserted and converted again
    without --manifest-in, so it de-conflicts to a different name), but the
    result would be a bundle that silently restores the wrong filename, which
    is worth one comparison up front."""
    if not path.is_file():
        return []
    existing = read_rename_log(path)
    return sorted(old for old, new in renames.items() if existing.get(old, new) != new)


def write_rename_log(root: Path, renames: dict) -> Path:
    """Merges `renames` into whatever rename map the bundle already holds.

    Additive, like optimize_db_media.py's originals.tsv and for the same
    reason: one bundle can describe several passes over a database, and a
    rename map that replaced the previous pass's would leave those earlier
    edits recorded in references.tsv with no way to resolve their names."""
    path = root / BUNDLE_RENAMES
    merged = read_rename_log(path) if path.is_file() else {}
    merged.update(renames)
    lines = [RENAMES_HEADER, RENAMES_COLUMNS,
             f"# written {time.strftime('%Y-%m-%dT%H:%M:%S')} - {len(merged)} rename(s)"]
    lines.extend(f"{old}\t{merged[old]}" for old in sorted(merged))
    return write_atomically(path, "\n".join(lines) + "\n")


def read_rename_log(path: Path) -> dict:
    renames = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise RuntimeError(f"{path}:{number}: expected 2 tab-separated fields, got {len(fields)}")
        renames[fields[0]] = fields[1]
    return renames


def write_reference_log(root: Path, records: list) -> Path:
    """Adds [(sha_before, sha_after, path, sites)] to the bundle's edit log.

    Appends rather than replaces, so a bundle can describe more than one pass
    over the same database. A page rewritten twice gets two records, chained by
    their digests - the second pass's "before" is the first pass's "after" -
    and restore_references walks that chain backwards. Replacing the file
    instead, as this first did, silently discarded every earlier pass's edits
    while both runs still reported success.

    A record identical to one already present (same row, same before and after)
    is the same pass written twice and is not duplicated."""
    path = root / BUNDLE_REFERENCES
    existing = read_reference_log(path) if path.is_file() else []
    seen = {(record[2], record[0], record[1]) for record in existing}
    merged = existing + [r for r in records if (r[2], r[0], r[1]) not in seen]
    total = sum(len(sites) for _b, _a, _p, sites in merged)
    lines = [REFERENCES_HEADER, REFERENCES_COLUMNS,
             f"# written {time.strftime('%Y-%m-%dT%H:%M:%S')} - {len(merged)} record(s), "
             f"{total} reference(s)"]
    # Sorted by path, then by the digest the record starts from, so a page's
    # records sit together; the chain is followed by digest, not by file order.
    for sha_before, sha_after, stored_path, sites in sorted(merged, key=lambda r: (r[2], r[0])):
        packed = ",".join(f"{offset}:{old_name}" for offset, old_name in sorted(sites))
        lines.append(f"{sha_before}\t{sha_after}\t{stored_path}\t{packed}")
    return write_atomically(path, "\n".join(lines) + "\n")


def read_reference_log(path: Path) -> list:
    """The inverse of write_reference_log. As with every other manifest here, a
    line that will not parse is fatal: a partly-read log would put some
    references back and leave others pointing at converted names, which is a
    worse state than either end of the conversion."""
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise RuntimeError(f"{path}:{number}: expected 4 tab-separated fields, got {len(fields)}")
        sha_before, sha_after, stored_path, packed = fields
        sites = []
        for site in packed.split(","):
            offset, sep, old_name = site.partition(":")
            if not sep or not offset.isdigit() or not old_name:
                raise RuntimeError(f"{path}:{number}: {site!r} is not an offset:filename pair")
            sites.append((int(offset), old_name))
        records.append((sha_before, sha_after, stored_path, sites))
    return records


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

    # Checked here and not only in main(), for the same reason optimize_db_media
    # does: a log describing edits that were rolled back is a trap for whatever
    # tries to undo them later.
    if cfg["save_originals"] is not None and cfg["dry_run"]:
        print("error: --save-originals cannot be combined with a dry run: a dry run rewrites nothing "
              "to undo", file=sys.stderr)
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
        if cfg["save_originals"] is not None:
            offenders = unloggable_names(renames)
            if offenders:
                print(f"error: {len(offenders)} converted filename(s) contain a character the reference log "
                      f"cannot encode ({', '.join(repr(c) for c in SITE_RESERVED)}), e.g. {offenders[0]!r}. "
                      "Re-run without --save-originals, or rename those files first.", file=sys.stderr)
                return 1
            clashes = conflicting_renames(cfg["save_originals"] / BUNDLE_RENAMES, renames)
            if clashes:
                print(f"error: {len(clashes)} filename(s) are already recorded in "
                      f"{cfg['save_originals'] / BUNDLE_RENAMES} as converting to something else, e.g. "
                      f"{clashes[0]!r}. One bundle cannot hold two meanings for one name; point "
                      "--save-originals at a fresh directory for this run.", file=sys.stderr)
                return 1
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
                # Insurance, not a known case: with TEXT_TYPES narrowed to
                # markup/JS/CSS nothing here should be binary, but this schema
                # has been seen filing binary video under a text content type,
                # so a row that isn't text is skipped rather than failing the
                # whole run.
                return ("binary", path, None)
            new_text, sites = rewrite_text(text, pattern, renames)
            if not sites:
                return None
            new_bytes = new_text.encode("utf-8")
            new_stored = codec.compress(new_bytes, compression)
            # Both digests are over the DECODED text, not the stored blob:
            # Brotli need not re-emit byte-identical output, so a digest over
            # the blob would report a row as "edited since" purely because it
            # was recompressed. What has to be reproduced is the content.
            return ("ok", path, base_len, new_stored, language_id, content_type_id, template_id, sites,
                    digest(raw), digest(new_bytes))

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = [r for r in pool.map(scan, work) if r is not None]

        errors = [r for r in results if r[0] == "error"]
        binary = [r for r in results if r[0] == "binary"]
        changes = [r for r in results if r[0] == "ok"]
        if binary:
            print(f"{len(binary)} row(s) skipped: binary content stored under a text content type.")
        for _kind, path, message in errors:
            print(f"  error: could not read {path}: {message}", file=sys.stderr)

        total_hits = sum(len(r[7]) for r in changes)
        print(f"{len(changes)} row(s) reference a converted file; {total_hits} reference(s) to rewrite.")

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        else:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                for _kind, path, base_len, new_stored, lang, ctid, tid, sites, _before, _after in changes:
                    replace_row(conn, path, base_len, new_stored, lang, ctid, tid,
                                lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [REF] {path}: {len(sites)} reference(s)")

                # Written before the commit, not after it. These rewrites are
                # only reversible while the log describing them exists, so a
                # log that cannot be written (a full or read-only bundle
                # directory) has to take the rewrite down with it rather than
                # leave the database changed with no way back. Both files are
                # renamed into place, so a failure here leaves the bundle as it
                # was and the rollback below undoes the rest.
                if cfg["save_originals"] is not None:
                    root = cfg["save_originals"]
                    try:
                        root.mkdir(parents=True, exist_ok=True)
                        rename_path = write_rename_log(root, renames)
                        log_path = write_reference_log(
                            root, [(before, after, path, sites)
                                   for _kind, path, _bl, _ns, _lang, _ct, _tid, sites, before, after
                                   in changes])
                    except OSError as exc:
                        # Reported the way optimize_db_media reports the same
                        # failure, rather than as a traceback: an unwritable
                        # bundle directory is an operator mistake.
                        print(f"error: could not write the reference log: {exc}. Rolling back - these "
                              "rewrites must not be committed without the log that undoes them.",
                              file=sys.stderr)
                        conn.rollback()
                        return 1
                    print(f"Reference log written to {log_path} and {rename_path}; undo this run with "
                          f"--restore-originals {root}.")
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


def chain_roots(chain: list) -> set:
    """The digests a row's recorded history can legitimately end at: every
    "before" that is not also some other record's "after".

    An unbroken history has exactly one - the text before the first pass ran.
    Two mean the chain is in pieces, because someone edited the page between
    passes and the earlier pass's "after" describes text that no longer
    existed by the time the later one ran. That count, not how many records
    happened to be undone, is what says whether a row can be put back: a row
    that simply was not at the newest state still rewinds all the way to the
    single root, and counting records would wrongly refuse it."""
    return {record[0] for record in chain} - {record[1] for record in chain}


def rewind_row(text: str, digest_now: str, chain: list, renames: dict) -> tuple:
    """Undoes the recorded passes over one row, newest first, and returns
    (text, digest, records_undone).

    A bundle can describe several conversions of the same page (see
    write_reference_log), so this follows the chain by digest rather than by
    file order: the record to undo is whichever one produced the text in hand,
    and undoing it yields the text the pass before that produced. It stops when
    no record matches.

    Stopping early is not always success. If someone edited the page between
    two passes, the earlier pass's "after" digest describes text that no longer
    existed by the time the later pass ran, so the chain is broken in the
    middle and the earlier edits cannot be replayed backwards at all - their
    offsets have moved. That is why the undone records are returned rather than
    a count: the caller compares them against the whole chain and refuses a row
    it can only partly put back."""
    undone = []
    remaining = list(chain)
    while True:
        matches = [r for r in remaining if r[1] == digest_now]
        if not matches:
            return text, digest_now, undone
        if len(matches) > 1:
            # Two passes recorded producing byte-identical text for this row
            # (reachable if it was reverted between them and converted again).
            # Picking one arbitrarily would undo the wrong edits; say so instead.
            raise RuntimeError(f"{len(matches)} recorded passes produced the same text, so which one to "
                               "undo is ambiguous")
        record = matches[0]
        text = undo_rewrite(text, record[3], renames)
        digest_now = digest(text.encode("utf-8"))
        if digest_now != record[0]:
            # Belt and braces: the digests bracket each pass, so undoing one
            # has to land on the "before" it recorded. Anything else means the
            # log and the row disagree in a way the per-site checks missed.
            raise RuntimeError(f"undoing {len(record[3])} edit(s) did not reproduce the recorded original")
        remaining.remove(record)
        undone.append(record)


def restore_references(cfg: dict) -> int:
    """Replays a --save-originals reference log backwards, putting every
    rewritten link back to the filename it named before the conversion.

    Each row is identified by the digest of the text this run produced, so the
    only rows touched are the ones still holding exactly what was written. A
    row already matching the "before" digest has been put back already (a
    re-run, or a restore that was interrupted) and is skipped; a row matching
    neither has been edited since and is reported and left alone, because
    replaying offsets into text that has moved would corrupt it."""
    db_path = cfg["db_path"]
    root = cfg["restore_originals"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    renames_path, log_path = root / BUNDLE_RENAMES, root / BUNDLE_REFERENCES
    for required in (renames_path, log_path):
        if not required.is_file():
            print(f"error: {root} holds no {required.name}, so it is not a bundle "
                  "update_media_references.py --save-originals wrote", file=sys.stderr)
            return 1
    try:
        renames = read_rename_log(renames_path)
        records = read_reference_log(log_path)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    by_path = {}
    for record in records:
        by_path.setdefault(record[2], []).append(record)
    total_sites = sum(len(sites) for _b, _a, _p, sites in records)
    passes = f" over {len(records)} pass(es)" if len(records) != len(by_path) else ""
    print(f"Log {log_path} describes {total_sites} rewritten reference(s) across {len(by_path)} row(s)"
          f"{passes}, over {len(renames)} converted filename(s).")

    warn = lambda message: print(message, file=sys.stderr)  # noqa: E731
    stats = {"restored": 0, "sites": 0, "already": 0, "changed_since": 0, "partial": 0,
             "missing": 0, "errors": 0}
    conn = sqlite3.connect(db_path)
    codec = None
    try:
        # Digest matching already makes a wrong database harmless - nothing
        # would be written - but it would report itself as hundreds of rows
        # "edited since", which reads like the operator's own edits rather than
        # a mistyped path. Say which it is, the way restore_originals does.
        stored = {row[0] for row in conn.execute("SELECT path FROM Content")}
        present = sum(1 for path in by_path if path in stored)
        if by_path and present * 2 < len(by_path) and not cfg.get("force_restore"):
            print(f"error: only {present} of the {len(by_path)} logged row(s) exist in {db_path}; this "
                  "does not look like the database those references were rewritten in. Refusing to "
                  "restore. Pass --force-restore if it really is.", file=sys.stderr)
            return 1
        try:
            codec = BrotliCodec(load_dictionary(conn))
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not cfg["dry_run"]:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
        conn.execute("BEGIN")
        try:
            for path in sorted(by_path):
                chain = by_path[path]
                row = conn.execute(
                    "SELECT c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
                    "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id WHERE c.path = ?",
                    (path,)).fetchone()
                if row is None:
                    stats["missing"] += 1
                    warn(f"  error: {path} is no longer in the database; its references cannot be put back")
                    continue
                blob, language_id, content_type_id, template_id, compression = row
                try:
                    raw = codec.decompress(reassemble(conn, path, blob), compression)
                    text = raw.decode("utf-8")
                except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                    stats["errors"] += 1
                    warn(f"  error: could not read {path}: {exc}")
                    continue
                current = digest(raw)
                try:
                    original, final, undone = rewind_row(text, current, chain, renames)
                except RuntimeError as exc:
                    stats["errors"] += 1
                    warn(f"  error: could not undo the edits in {path}: {exc}")
                    continue

                # Whether the row can be put back is decided by where the
                # rewind LANDED, not by how many records it consumed: a row
                # that was simply not at the newest state still rewinds all the
                # way to the single root, and counting records refused it.
                roots = chain_roots(chain)
                complete = len(roots) == 1 and final in roots
                if complete and not undone:
                    stats["already"] += 1
                    continue
                if not complete:
                    if undone or len(roots) != 1:
                        # Someone edited this page between two passes, so the
                        # earlier pass's offsets no longer address anything.
                        # Writing back what did come apart would leave the row
                        # matching no record at all, so it is left exactly as
                        # it is and named - the only state a person can act on.
                        stats["partial"] += 1
                        warn(f"  warning: {path} was rewritten by {len(chain)} pass(es) and only "
                             f"{len(undone)} can be undone (it was edited in between); leaving it alone")
                    else:
                        stats["changed_since"] += 1
                        warn(f"  warning: {path} has changed since its references were rewritten; leaving "
                             "it alone rather than replaying edits into text that has moved")
                    continue
                sites = sum(len(record[3]) for record in undone)
                replace_row(conn, path, len(blob), codec.compress(original.encode("utf-8"), compression),
                            language_id, content_type_id, template_id, warn)
                stats["restored"] += 1
                stats["sites"] += sites
                if cfg["verbose"]:
                    passes = f" over {len(undone)} pass(es)" if len(undone) > 1 else ""
                    print(f"  [UNDO] {path}: {sites} reference(s){passes}")
            if cfg["dry_run"]:
                conn.rollback()
            else:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    if not cfg["dry_run"] and stats["restored"]:
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()

    verb = "would put back" if cfg["dry_run"] else "put back"
    print()
    print(f"{'Dry run complete. ' if cfg['dry_run'] else 'Done. '}{verb} {stats['sites']} reference(s) "
          f"across {stats['restored']} row(s); {stats['already']} row(s) already held the original names, "
          f"{stats['changed_since']} edited since and left alone, {stats['partial']} only partly "
          f"reversible, {stats['missing']} missing, {stats['errors']} error(s).")
    if stats["changed_since"] or stats["missing"] or stats["partial"]:
        print("Some references were NOT put back, so those pages still link to converted filenames that "
              "optimize_db_media.py --restore-originals has removed.", file=sys.stderr)
    # A partial restore leaves pages pointing at media that no longer exists, so
    # it must not look like a clean run to whatever chained this command.
    return 1 if (stats["missing"] or stats["errors"] or stats["changed_since"]
                 or stats["partial"]) else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database whose media references should be updated")
    p.add_argument("--before", type=Path, default=None,
                   help="The pre-conversion copy of this database (the backup optimize_db_media.py wrote), "
                        "diffed against it to learn exactly which files were renamed. Required unless "
                        "--restore-originals is given")
    p.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    p.add_argument("--workers", type=int, default=8,
                   help="Parallel (de)compression workers (default: 8)")
    originals = p.add_mutually_exclusive_group()
    originals.add_argument("--save-originals", type=Path, default=None, metavar="BUNDLE",
                           help="Record where every reference this run rewrites used to point, into the "
                                "same bundle optimize_db_media.py --save-originals fills - the minimum "
                                "needed to reconstruct the original HTML. Cannot be combined with --dry-run")
    originals.add_argument("--restore-originals", type=Path, default=None, metavar="BUNDLE",
                           help="Undo a --save-originals run instead of rewriting: put every recorded "
                                "reference back to the filename it named before. Needs no --before")
    p.add_argument("--force-restore", action="store_true",
                   help="With --restore-originals, proceed even though the logged rows are largely absent "
                        "from this database - which normally means the bundle belongs to a different one")
    p.add_argument("--verbose", action="store_true", help="List rewritten rows and sample renames")
    args = p.parse_args()
    if args.save_originals is not None and args.dry_run:
        p.error("--save-originals cannot be combined with --dry-run: a dry run rewrites nothing to undo")
    if args.restore_originals is None and args.before is None:
        p.error("--before is required (it is what the rename map is diffed from); "
                "only --restore-originals can go without it")
    cfg = {"db_path": args.db_path, "before": args.before, "dry_run": args.dry_run,
           "workers": args.workers, "verbose": args.verbose, "force_restore": args.force_restore,
           "save_originals": args.save_originals, "restore_originals": args.restore_originals}
    sys.exit(restore_references(cfg) if args.restore_originals is not None else run(cfg))


if __name__ == "__main__":
    main()
