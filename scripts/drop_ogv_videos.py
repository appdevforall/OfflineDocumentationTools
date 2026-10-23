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
drop_ogv_videos.py

Removes the stored Ogg Theora (.ogv) videos from a documentation database and
takes the pages that link to them off those files, leaving every <video>
playing its WEBM instead.

Ogg Theora is dead weight here. Every .ogv this schema stores also exists as a
.webm beside it, and every browser the offline docs run in plays WEBM, so the
Ogg copy is a second encoding of the same clip that nothing ever fetches - in
the database measured, 1.8 MB of stored bytes across 7 files, each between 1.4x
and 5.6x the size of the WEBM it duplicates.

What it does to the markup, and why that is a deletion rather than a rename:
these references are not bare URLs but <source> elements inside a <video>, and
the fallback list is already written mp4, webm, ogv -

    <video controls="">
    <source src="media/..._anim_zoom.mp4" type="video/mp4"/>
    <source src="media/..._anim_zoom.webm" type="video/webm"/>
    <source src="media/..._anim_zoom.ogv" type="video/ogg"/>
    </video>

Rewriting that last src to the WEBM would leave the same file listed twice,
the second time under type="video/ogg" - a declared type contradicting its
file, which is precisely the attribute a browser uses to skip a source without
fetching it. Dropping the element says the same thing correctly: the WEBM
source directly above it is the reference that survives.

So a site is only dropped when the WEBM is demonstrably already there - a
<source> for the counterpart file inside the same <video>. A lone .ogv source,
with no WEBM sibling to fall back to, is rewritten in place instead (src and
type both), because deleting it would leave that video with nothing to play.
Neither path can strand a page: whichever applies, the enclosing <video> ends
the run pointing at the WEBM.

Anything else that mentions an .ogv filename - an <a href>, a <video src>
attribute, a string in a script - is a reference this tool does not know how to
re-point, and stops the run before it deletes the file out from under it. So is
an .ogv with no .webm counterpart. Both are refusals, not warnings: the failure
they prevent is a page that has lost its video, which nothing downstream would
report.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards. --dry-run does all the work and reports what
would change without writing.

    uv run scripts/drop_ogv_videos.py documentation.db --dry-run
    uv run scripts/drop_ogv_videos.py documentation.db

Recovering: the timestamped backup taken before the first write is the whole
undo. This tool deliberately writes no rollback bundle of its own, unlike
optimize_db_media.py --save-originals, because it is not a lossy conversion
whose inputs need archiving - it deletes a redundant copy of a video that stays
in the database as WEBM, and restoring the backup puts the Ogg copies and the
markup back together.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import (
    BrotliCodec, backup_database, delete_media_row, human, is_continuation_path, load_dictionary,
    owned_fragment_paths, reassemble,
)
from update_media_references import TEXT_TYPES, replace_row

# The extension being retired and the one that replaces it. Kept as names
# rather than spelled inline so the mapping is stated once.
OLD_EXT = ".ogv"
NEW_EXT = ".webm"
NEW_MIME = "video/webm"

# One <source> element, captured with the whitespace that precedes it so
# removing it does not leave a blank line where the element used to be. The
# body is "anything but >", which is enough for HTML this pipeline has already
# normalised (attribute values here are quoted and contain no ">"), and the tag
# is matched non-greedily so two adjacent <source> elements never collapse into
# one match.
SOURCE_RE = re.compile(r"[ \t]*\n?[ \t]*<source\b[^>]*>(?:\s*</source\s*>)?", re.IGNORECASE)
# The src attribute inside one such element, quoted either way.
SRC_RE = re.compile(r"""\bsrc\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)
# Its type attribute, replaced alongside src when a lone .ogv source is
# rewritten rather than dropped.
TYPE_RE = re.compile(r"""(\btype\s*=\s*)("[^"]*"|'[^']*')""", re.IGNORECASE)
# A <video> element, used to scope "is the WEBM already listed here?" to the
# fallback list the .ogv actually belongs to. Non-greedy, so nested-looking
# markup takes the nearest close rather than swallowing the next video whole.
VIDEO_RE = re.compile(r"<video\b[^>]*>[\s\S]*?</video\s*>", re.IGNORECASE)


def stored_lengths(conn) -> dict:
    """{path: stored byte length} for the whole Content table.

    Read once and passed around rather than re-queried, because every caller
    below needs it only to tell a real file from a chunk continuation, and this
    is a full scan of a table with tens of thousands of rows."""
    return dict(conn.execute("SELECT path, LENGTH(content) FROM Content").fetchall())


def video_paths(lengths: dict, extension: str) -> set:
    """Every stored path ending in `extension`, ignoring the "<path>-<N>" chunk
    continuation rows that a large video is split across - those belong to
    their base and are deleted with it, not counted as files of their own."""
    return {path for path in lengths
            if path.lower().endswith(extension) and not is_continuation_path(lengths, path)}


def counterpart(path: str) -> str:
    """The .webm path an .ogv one should be replaced by: same directory, same
    stem, new extension."""
    return path[:-len(OLD_EXT)] + NEW_EXT


def orphaned(lengths: dict) -> list:
    """The .ogv paths with no .webm counterpart stored beside them.

    Checked before anything is deleted because those are the files whose
    removal would leave a video with nothing to play at all - the one case
    where this tool has no correct edit to make and must not guess."""
    webm = {path.lower() for path in video_paths(lengths, NEW_EXT)}
    return sorted(path for path in video_paths(lengths, OLD_EXT)
                  if counterpart(path).lower() not in webm)


def build_pattern(names: set) -> re.Pattern:
    """One alternation over every .ogv filename being removed, anchored so it
    only matches the name used as a reference.

    Same anchoring as update_media_references.build_pattern, and for the same
    reasons: the preceding character must open a URL or attribute value, so a
    filename named in prose is not rewritten, and the following one must not
    extend the name, so "zoom.ogv" never matches inside "zoom.ogv2". Longest
    first so the alternation cannot settle for a shorter overlapping match.

    The anchors earn their keep on this extension in particular. "ogv" occurs
    in this database as a substring of ordinary markup - <a name="ondialogview">
    in the androidx.preference pages - and as a bare word in pdf.worker.mjs's
    extension table (case "ogv":). Matching whole filenames, anchored, misses
    both."""
    body = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<=[/\"'(=])({body})(?![A-Za-z0-9_])")


def source_src(tag: str) -> str:
    """The src attribute of one <source> element, or "" if it has none."""
    match = SRC_RE.search(tag)
    if match is None:
        return ""
    return match.group(2) if match.group(2) is not None else match.group(3)


def basename(src: str) -> str:
    """The filename a reference points at, with any query or fragment trimmed.

    References are written in whatever form their doc set uses -
    "media/foo.ogv", "/videos/foo.ogv", an absolute URL - and the filename is
    the one part every form shares, which is why matching happens on it.
    Unambiguous here for the reason update_media_references documents: no two
    videos in this database share a basename (checked in run() regardless)."""
    return src.rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]


def references(src: str, names: set) -> str:
    """The .ogv filename `src` points at, or "" if it points elsewhere."""
    name = basename(src)
    return name if name in names else ""


def rewrite_video(block: str, names: set) -> tuple:
    """Edits the <source> elements of one <video> block.

    Returns (new_block, dropped, rewritten). A source pointing at an .ogv is
    dropped when this same block already offers its .webm counterpart, and
    rewritten to that counterpart when it does not - so the block always comes
    out referring to the WEBM, never to nothing.

    The decision is made against the sources present BEFORE any edit, so a
    block listing the same .ogv twice - which would otherwise have its second
    copy rewritten to a .webm the first copy had just introduced - drops both."""
    tags = [(match.start(), match.end(), match.group(0)) for match in SOURCE_RE.finditer(block)]
    present = {basename(source_src(tag)).lower() for _s, _e, tag in tags}

    pieces, dropped, rewritten = [], 0, 0
    read = 0
    for start, end, tag in tags:
        old_name = references(source_src(tag), names)
        if not old_name:
            continue
        pieces.append(block[read:start])
        if counterpart(old_name).lower() in present:
            dropped += 1  # the WEBM is already listed; this element is redundant
        else:
            new_tag = SRC_RE.sub(
                lambda m: m.group(0).replace(old_name, counterpart(old_name)), tag, count=1)
            new_tag = TYPE_RE.sub(lambda m: f'{m.group(1)}{m.group(2)[0]}{NEW_MIME}{m.group(2)[0]}',
                                  new_tag, count=1)
            pieces.append(new_tag)
            rewritten += 1
        read = end
    if not dropped and not rewritten:
        return block, 0, 0
    pieces.append(block[read:])
    return "".join(pieces), dropped, rewritten


def rewrite_text(text: str, names: set, pattern: re.Pattern) -> tuple:
    """Takes every <video> in `text` off the .ogv files named in `names`.

    Returns (new_text, dropped, rewritten, stranded), where stranded lists any
    .ogv reference left over - one that did not sit in a <source> inside a
    <video>, and so was not something this tool knows how to re-point. The
    caller refuses the run on a non-empty stranded list rather than deleting a
    file that something still links to.

    Rewriting only inside <video> blocks, and re-scanning the result for
    leftovers, is what makes that check trustworthy: the leftovers are found by
    the same pattern that found the references in the first place, so a shape
    of reference this function silently failed to edit shows up as stranded
    instead of passing as done."""
    pieces, dropped, rewritten = [], 0, 0
    read = 0
    for match in VIDEO_RE.finditer(text):
        new_block, block_dropped, block_rewritten = rewrite_video(match.group(0), names)
        if not block_dropped and not block_rewritten:
            continue
        pieces.append(text[read:match.start()])
        pieces.append(new_block)
        dropped += block_dropped
        rewritten += block_rewritten
        read = match.end()
    pieces.append(text[read:])
    new_text = "".join(pieces)
    return new_text, dropped, rewritten, sorted({m.group(1) for m in pattern.finditer(new_text)})


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    codec = None
    try:
        lengths = stored_lengths(conn)
        videos = sorted(video_paths(lengths, OLD_EXT))
        if not videos:
            print(f"No {OLD_EXT} videos stored - nothing to do.")
            return 0

        missing = orphaned(lengths)
        if missing:
            print(f"error: {len(missing)} {OLD_EXT} file(s) have no {NEW_EXT} counterpart stored, e.g. "
                  f"{missing[0]!r}. Removing those would leave their pages with no video to play; "
                  f"encode the {NEW_EXT} copies first.", file=sys.stderr)
            return 1

        names = {path.rsplit("/", 1)[-1] for path in videos}
        if len(names) != len(videos):
            # references() matches on the basename, so two videos sharing one
            # would make a reference ambiguous. Not the case in this database;
            # checked because the failure is a page re-pointed at the wrong clip.
            print(f"error: two or more {OLD_EXT} files share a filename; references to them cannot be "
                  "told apart. Refusing to rewrite.", file=sys.stderr)
            return 1
        pattern = build_pattern(names)
        print(f"Found {len(videos)} {OLD_EXT} file(s) to remove, each with a {NEW_EXT} counterpart.")

        codec = BrotliCodec(load_dictionary(conn))
        placeholders = ",".join("?" * len(TEXT_TYPES))
        rows = conn.execute(
            f"SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            f"FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            f"WHERE ct.value IN ({placeholders}) ORDER BY c.path", TEXT_TYPES
        ).fetchall()
        text_lengths = {row[0]: len(row[1]) for row in rows}
        bases = [row for row in rows if not is_continuation_path(text_lengths, row[0])]
        print(f"Scanning {len(bases)} text row(s) for references "
              f"({len(rows) - len(bases)} chunk-continuation row(s) folded into their base)...")

        # Reassembled here on the main thread, not in the pool: a sqlite3
        # connection may only be used from the thread that created it, and
        # reassemble() queries for continuation rows. The time goes on brotli,
        # which is what the pool is for.
        work = [(row[0], reassemble(conn, row[0], row[1]), len(row[1]), *row[2:]) for row in bases]

        def scan(item):
            """Decompress one row and take its videos off the .ogv files.
            Pure CPU/subprocess - touches no database."""
            path, full, base_len, language_id, content_type_id, template_id, compression = item
            try:
                raw = codec.decompress(full, compression)
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return ("error", path, str(exc))
            if OLD_EXT.encode().lower() not in raw.lower():
                return None  # cheap reject before the decode and the regexes
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Insurance, not a known case: TEXT_TYPES excludes text/plain,
                # which is where this schema files the videos themselves, so
                # nothing binary should reach here. A row that is not text is
                # skipped rather than failing the whole run - but it is counted
                # and reported, since a page that could not be read is a page
                # whose references went unchecked.
                return ("binary", path, None)
            new_text, dropped, rewritten, stranded = rewrite_text(text, names, pattern)
            if not dropped and not rewritten and not stranded:
                return None
            if stranded:
                return ("stranded", path, stranded)
            new_stored = codec.compress(new_text.encode("utf-8"), compression)
            return ("ok", path, base_len, new_stored, language_id, content_type_id, template_id,
                    dropped, rewritten)

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = [r for r in pool.map(scan, work) if r is not None]

        errors = [r for r in results if r[0] == "error"]
        binary = [r for r in results if r[0] == "binary"]
        stranded = [r for r in results if r[0] == "stranded"]
        changes = [r for r in results if r[0] == "ok"]

        for _kind, path, message in errors:
            print(f"  error: could not read {path}: {message}", file=sys.stderr)
        if errors:
            print(f"error: {len(errors)} row(s) could not be read, so they could not be checked for "
                  f"references. Refusing to delete videos that might still be linked.", file=sys.stderr)
            return 1
        if stranded:
            for _kind, path, leftovers in stranded[:10]:
                print(f"  error: {path} references {', '.join(leftovers)} outside a <video> <source>",
                      file=sys.stderr)
            print(f"error: {len(stranded)} row(s) reference an {OLD_EXT} file in a form this tool cannot "
                  "re-point. Refusing to delete files that are still linked; fix those references by hand "
                  "and re-run.", file=sys.stderr)
            return 1
        if binary:
            print(f"{len(binary)} row(s) skipped: binary content stored under a text content type.")

        total_dropped = sum(r[7] for r in changes)
        total_rewritten = sum(r[8] for r in changes)
        # Each video's own row plus the chunk continuations it owns, which go
        # with it - counted through owned_fragment_paths rather than by matching
        # "<video>-*" by name, so an unrelated page that happens to sit at such
        # a name is not counted as bytes this run frees.
        freed = sum(lengths[path] for video in videos
                    for path in [video, *owned_fragment_paths(conn, video, lengths[video])])
        print(f"{len(changes)} page(s) reference an {OLD_EXT} file: {total_dropped} redundant "
              f"<source> element(s) to drop, {total_rewritten} to re-point at the {NEW_EXT}.")
        print(f"Removing the {OLD_EXT} rows frees {human(freed)} stored byte(s).")

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        else:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                for _kind, path, base_len, new_stored, lang, ctid, tid, dropped, rewritten in changes:
                    replace_row(conn, path, base_len, new_stored, lang, ctid, tid,
                                lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [REF] {path}: dropped {dropped}, re-pointed {rewritten}")
                for video in videos:
                    row = conn.execute("SELECT LENGTH(content) FROM Content WHERE path = ?",
                                       (video,)).fetchone()
                    delete_media_row(conn, video, row[0])
                    if cfg["verbose"]:
                        print(f"  [DEL] {video}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    if not cfg["dry_run"]:
        print("Vacuuming database...")
        vac = sqlite3.connect(db_path)
        try:
            vac.execute("VACUUM")
        finally:
            vac.close()

    verb = "would remove" if cfg["dry_run"] else "removed"
    print(f"\nDone. {verb} {len(videos)} {OLD_EXT} file(s); {total_dropped} redundant <source> "
          f"element(s) dropped and {total_rewritten} re-pointed across {len(changes)} page(s); "
          f"{len(binary)} binary row(s) skipped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work and report what would change, without writing or backing up")
    p.add_argument("--workers", type=int, default=8,
                   help="Threads used to decompress and recompress rows (default: 8)")
    p.add_argument("--verbose", action="store_true",
                   help="Log every page edited and every video deleted")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "workers": args.workers,
                  "verbose": args.verbose}))


if __name__ == "__main__":
    main()
