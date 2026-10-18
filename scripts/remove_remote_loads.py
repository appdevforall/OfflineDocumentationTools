#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
remove_remote_loads.py

Takes out the markup that makes documentation pages fetch files from the
internet where the page needs no download to work (ADFA-6589). In the
2026-10-13 database that is four kinds of element:

  A. An <img> with a stored src and a srcset on developer.android.com - 14, on
     three a/devsite/ pages:

         <img alt="" sizes="64px" src="media/static_images_picto-icons_reduce.svg"
              srcset="https://developer.android.com/static/images/picto-icons/reduce.svg"/>

     A browser loads from srcset in preference to src, so offline each shows
     broken although its file is in the database. The srcset goes, and so does
     sizes, which only tells the browser how to choose from a srcset; src then
     loads the stored copy.

  B. A <source media="(prefers-color-scheme: dark)"> inside a <picture>, whose
     srcset is on developer.android.com - 7, the stylus page's dark-theme
     icons, which the database does not store. The element goes; in dark mode
     the <picture> then shows its <img>, the light icon.

  D. The four kotl.in stability badges on k/html/components-stability.html,
     <img src="https://kotl.in/badges/stable.svg" alt="Stable stability level" />.
     Each becomes its level in code style, <code>Stable</code>.

  YouTube. 44 players (43 videos) on k/html/ pages,
     <video src="https://www.youtube.com/v/ID" title="TITLE"/>. Each becomes a
     link to the video's page,
     <a href="https://www.youtube.com/watch?v=ID">TITLE (video on YouTube)</a>;
     the app shows its consent dialog before opening an off-device link.

(C, the API reference diagrams that do need a download, is
bundle_remote_images.py's.)

The k/html/ pages are JSON documents that page.peb renders, their markup
inside JSON strings with quotes written \\". They are edited as bytes inside
those strings, never parsed and re-serialised, so every byte but the element's
own stays as it was. Each is parsed before and after the edit only to check it
is still valid JSON; one that would not be is left alone and reported.

Only those shapes are rewritten, and every candidate is accounted for: each
<img> or <source> whose srcset names developer.android.com, each mention of
kotl.in/badges/, and each <video> naming youtube.com is either rewritten or
left exactly as it is, named, and the run exits non-zero at the end. For
example, an <img> whose src is not a stored file is left (dropping its srcset
would leave it showing nothing), as is a dark <source> outside a <picture>, or
a YouTube <video> with an attribute this tool has not seen. An <img src> on
developer.android.com without a srcset is bundle_remote_images.py's, and is
neither touched nor reported here.

The edit is made on the decoded bytes. Each changed row is re-compressed the
way its ContentTypes row says (against the shared CompressionDictionary when
the database has one, which is what BrotliCodec handles); rows without these
elements keep their stored bytes exactly, never re-compressed. A page over the
1 MiB chunk size is read whole across its "<path>-1", "-2", ... rows and
written back re-chunked.

Where it goes in the pipeline: after remove_skin_font_comment.py and before
the `cp DB migrated.db` snapshot, for the reason that script's docstring
gives - update_media_references.py --restore-originals leaves alone any page
whose text no longer matches what it recorded, so a page edited after the
snapshot could not be restored. bundle_remote_images.py goes in the same
place; the two touch different elements, so their order does not matter.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards - all three only when there is something to
change, so a second run writes nothing at all. --dry-run does all the work and
reports what would change without writing.

    uv run scripts/remove_remote_loads.py documentation.db --dry-run
    uv run scripts/remove_remote_loads.py documentation.db

Recovering: the timestamped backup taken before the first write is the whole
undo.
"""
from __future__ import annotations

import argparse
import html
import json
import posixpath
import re
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlsplit

from optimize_db_media import BrotliCodec, backup_database, human, is_continuation_path, load_dictionary, reassemble
from update_media_references import replace_row

CONTENT_TYPE = "text/html"

DEVSITE = b"https://developer.android.com/"
DARK_SCHEME = b"(prefers-color-scheme: dark)"

# A page holding none of these has nothing for this tool and is not looked at further.
MARKERS = (b"developer.android.com", b"kotl.in/badges/", b"youtube.com")

SRCSET, DARK_SOURCE, BADGE, YOUTUBE = "srcset", "dark-source", "badge", "youtube"
LABELS = {
    SRCSET: "A: remote srcset (and its sizes) dropped from an <img> with a stored src",
    DARK_SOURCE: "B: dark-theme <source> with a remote srcset removed",
    BADGE: "D: kotl.in badge <img> replaced by <code>",
    YOUTUBE: "YouTube <video> replaced by a link",
}

# A start tag as the devsite pages write it (BeautifulSoup output): name="value"
# pairs, double-quoted, separated by whitespace. Anything else - a single-quoted
# or unquoted value, JSON-escaped quotes - does not parse, and such a tag is
# reported when it matters rather than guessed at.
_TAG_NAME = re.compile(rb"<([A-Za-z][A-Za-z0-9]*)")
_ATTRIBUTE = re.compile(rb'\s+([A-Za-z_:][-A-Za-z0-9_:.]*)(?:="([^"]*)")?')
_TAG_CLOSE = re.compile(rb"\s*/?>")
_IMG_OR_SOURCE = re.compile(rb"<(?:img|source)\b", re.I)
_IMG = re.compile(rb"<img\b", re.I)
_PICTURE_OPEN = re.compile(rb"<picture[\s>]", re.I)
_PICTURE_CLOSE = re.compile(rb"</picture\s*>", re.I)

# Shapes inside JSON strings, so every quote is \". The alt text must name the
# badge's own level, and a title may hold no backslash (a JSON escape), quote
# or angle bracket, so it can move from an attribute into link text unchanged.
BADGE_LEVELS = (b"experimental", b"alpha", b"beta", b"stable")
_BADGE = re.compile(rb'<img src=\\"https://kotl\.in/badges/([a-z]+)\.svg\\" alt=\\"([A-Za-z]+) stability level\\" />')
_BADGE_SIGHTING = re.compile(rb"kotl\.in/badges/")
_VIDEO = re.compile(rb"<video\b", re.I)
_YOUTUBE_VIDEO = re.compile(
    rb'<video(?: width=\\"[0-9]+\\" height=\\"[0-9]+\\")?'
    rb' src=\\"https://www\.youtube\.com/(?:v/|watch\?v=)([A-Za-z0-9_-]{11})\\"'
    rb' title=\\"([^"\\<>]+)\\"/>')


def parse_tag(page: bytes, start: int):
    """(end, name, {attribute: (value, span)}) for the start tag at `start`,
    where span is the (start, end) of ' name="value"' including the whitespace
    before it, or None when the tag is not written as plain name="value" pairs
    or names an attribute twice."""
    name = _TAG_NAME.match(page, start)
    if name is None:
        return None
    attributes, at = {}, name.end()
    while True:
        attribute = _ATTRIBUTE.match(page, at)
        if attribute is None:
            break
        key = attribute.group(1).lower().decode("ascii")
        if key in attributes:
            return None
        attributes[key] = (attribute.group(2), attribute.span())
        at = attribute.end()
    close = _TAG_CLOSE.match(page, at)
    if close is None:
        return None
    return close.end(), name.group(1).lower().decode("ascii"), attributes


def srcset_urls(value: bytes) -> list:
    """The URL of each candidate in a srcset value ("a.png 1x,b.png 2x")."""
    return [candidate.split()[0] for candidate in value.split(b",") if candidate.strip()]


def stored_target(page_path: str, url: bytes):
    """The Content path an attribute value on `page_path` loads, when it is a
    relative or root-relative address on the device; None for anything else."""
    parts = urlsplit(html.unescape(url.decode("utf-8", "replace")).strip())
    if parts.scheme or parts.netloc or not parts.path:
        return None
    path = unquote(parts.path)
    if path.startswith("/"):
        return posixpath.normpath(path.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(page_path), path))


def _last(pattern: re.Pattern, page: bytes, before: int) -> int:
    """Offset of the last match of `pattern` that starts before `before`, or -1."""
    last = -1
    for match in pattern.finditer(page, 0, before):
        last = match.start()
    return last


def srcset_edits(page_path: str, page: bytes, stored_paths) -> tuple:
    """([(start, end, replacement)], Counter, [(offset, reason)]) for A and B on one page."""
    edits, counts, odd = [], Counter(), []
    for match in _IMG_OR_SOURCE.finditer(page):
        start = match.start()
        parsed = parse_tag(page, start)
        if parsed is None:
            tag_end = page.find(b">", start)
            rough = page[start:tag_end + 1 if tag_end >= 0 else len(page)]
            if b"srcset" in rough and b"developer.android.com" in rough:
                odd.append((start, "an <img> or <source> with developer.android.com in a srcset, written in a form "
                                   "this tool does not parse; left in place"))
            continue
        end, name, attributes = parsed
        srcset = attributes.get("srcset")
        if srcset is None or srcset[0] is None or b"developer.android.com" not in srcset[0]:
            continue
        urls = srcset_urls(srcset[0])
        if not urls or not all(url.startswith(DEVSITE) for url in urls):
            odd.append((start, f"a <{name}> srcset mixes developer.android.com with other addresses; left in place"))
            continue
        if name == "img":
            src = attributes.get("src")
            target = stored_target(page_path, src[0]) if src is not None and src[0] else None
            if target is None or target not in stored_paths:
                shown = src[0].decode("utf-8", "replace") if src is not None and src[0] is not None else None
                odd.append((start, f"an <img> with a developer.android.com srcset has src {shown!r}, which is not "
                                   "a stored file, so dropping the srcset would leave it showing nothing; "
                                   "left in place"))
                continue
            for key in ("srcset", "sizes"):
                if key in attributes:
                    span_start, span_end = attributes[key][1]
                    edits.append((span_start, span_end, b""))
            counts[SRCSET] += 1
            continue
        media = attributes.get("media")
        if media is None or media[0] != DARK_SCHEME:
            odd.append((start, 'a <source> with a developer.android.com srcset is not the dark-theme variant '
                               '(media="(prefers-color-scheme: dark)"); left in place'))
            continue
        if _last(_PICTURE_OPEN, page, start) <= _last(_PICTURE_CLOSE, page, start):
            odd.append((start, "a dark-theme <source> is not inside a <picture>; left in place"))
            continue
        closer = _PICTURE_CLOSE.search(page, end)
        fallback = _IMG.search(page, end)
        if closer is None or fallback is None or fallback.start() > closer.start():
            odd.append((start, "a dark-theme <source> has no <img> after it in its <picture> to fall back to; "
                               "left in place"))
            continue
        edits.append((start, end, b""))
        counts[DARK_SOURCE] += 1
    return edits, counts, odd


def json_edits(page: bytes) -> tuple:
    """([(start, end, replacement)], Counter, [(offset, reason)]) for D and YouTube."""
    edits, counts, odd, badges = [], Counter(), [], []
    for match in _BADGE.finditer(page):
        level, word = match.group(1), match.group(2)
        if level in BADGE_LEVELS and word == level.capitalize():
            edits.append((match.start(), match.end(), b"<code>" + word + b"</code>"))
            counts[BADGE] += 1
            badges.append(match.span())
    for sighting in _BADGE_SIGHTING.finditer(page):
        if not any(start <= sighting.start() < end for start, end in badges):
            odd.append((sighting.start(), 'a kotl.in badge not in the shape <img src=\\"https://kotl.in/badges/'
                                          'LEVEL.svg\\" alt=\\"Level stability level\\" />; left in place'))
    for match in _VIDEO.finditer(page):
        tag_end = page.find(b">", match.start())
        if b"youtube.com" not in page[match.start():tag_end + 1 if tag_end >= 0 else len(page)].lower():
            continue
        video = _YOUTUBE_VIDEO.match(page, match.start())
        if video is None:
            odd.append((match.start(), "a YouTube <video> not in the shape <video [width height] src title/> with "
                                       "JSON-escaped quotes; left in place"))
            continue
        video_id, title = video.group(1), video.group(2)
        edits.append((video.start(), video.end(), b'<a href=\\"https://www.youtube.com/watch?v=' + video_id
                      + b'\\">' + title + b" (video on YouTube)</a>"))
        counts[YOUTUBE] += 1
    return edits, counts, odd


def apply_edits(page: bytes, edits: list) -> bytes:
    """`page` with each (start, end) span replaced and nothing else changed."""
    pieces, read = [], 0
    for start, end, replacement in sorted(edits):
        if start < read:
            raise RuntimeError(f"overlapping edits at byte {start:,}")
        pieces.append(page[read:start])
        pieces.append(replacement)
        read = end
    pieces.append(page[read:])
    return b"".join(pieces)


def edit_page(page_path: str, page: bytes, stored_paths) -> tuple:
    """(new page or None, Counter, [(offset, reason)]) for one decoded page."""
    if not any(marker in page for marker in MARKERS):
        return None, Counter(), []
    edits, counts, odd = srcset_edits(page_path, page, stored_paths)
    escaped, escaped_counts, escaped_odd = json_edits(page)
    odd += escaped_odd
    looks_json = page.lstrip()[:1] == b"{"
    if escaped and not looks_json:
        odd += [(start, "a kotl.in badge or YouTube <video> written JSON-escaped in a page that is not JSON; "
                        "left in place") for start, _end, _new in escaped]
    else:
        edits += escaped
        counts += escaped_counts
    if not edits:
        return None, Counter(), odd
    new_page = apply_edits(page, edits)
    if looks_json:
        for label, text in (("is not valid JSON", page), ("would not be valid JSON after the edit", new_page)):
            try:
                json.loads(text)
            except ValueError as exc:
                return None, Counter(), odd + [(0, f"the page {label} ({exc}); left exactly as it was")]
    return new_page, counts, odd


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    codec = None
    try:
        codec = BrotliCodec(load_dictionary(conn))
        stored_paths = {row[0] for row in conn.execute("SELECT path FROM Content")}
        rows = conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value = ? ORDER BY c.path", (CONTENT_TYPE,)
        ).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        bases = [row for row in rows if not is_continuation_path(lengths, row[0])]
        print(f"Scanning {len(bases)} {CONTENT_TYPE} row(s) for markup that loads from the internet "
              f"({len(rows) - len(bases)} chunk-continuation row(s) folded into their base)...")

        # Reassembled on the main thread, not in the pool: a sqlite3 connection
        # may only be used from the thread that created it.
        work = [(row[0], reassemble(conn, row[0], row[1]), len(row[1]), *row[2:]) for row in bases]

        def scan(item):
            """Decompress one row and edit it. Pure CPU/subprocess - touches no database."""
            path, full, base_len, language_id, content_type_id, template_id, compression = item
            try:
                raw = codec.decompress(full, compression)
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return ("skip", path, [f"could not be read: {exc}"])
            new_raw, counts, odd = edit_page(path, raw, stored_paths)
            left = [f"at byte {offset:,}, {reason}" for offset, reason in odd]
            if new_raw is None:
                return ("skip", path, left) if left else None
            new_stored = codec.compress(new_raw, compression)
            return ("ok", path, left, base_len, new_stored, language_id, content_type_id, template_id, counts)

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            results = [r for r in pool.map(scan, work) if r is not None]

        changes = [r for r in results if r[0] == "ok"]
        skipped = [r for r in results if r[0] == "skip"]
        flagged = [r for r in results if r[2]]
        totals, pages = Counter(), Counter()
        for change in changes:
            totals += change[8]
            pages.update(change[8].keys())

        if changes:
            print(f"{len(changes)} row(s) load from the internet in a shape this tool rewrites:")
            for group, label in LABELS.items():
                print(f"  {label}: {totals[group]} on {pages[group]} page(s)")
        else:
            print("No row loads from the internet in a shape this tool rewrites - nothing to change.")
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
            conn.execute("BEGIN")
            try:
                for _kind, path, _left, base_len, new_stored, lang, ctid, tid, counts in changes:
                    replace_row(conn, path, base_len, new_stored, lang, ctid, tid,
                                lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [EDIT] {path}: " + ", ".join(f"{group} {n}" for group, n in sorted(counts.items())))
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
    print(f"\nDone. {verb} {len(changes)} row(s), {human(sum(totals.values()))} element(s); "
          f"{len(skipped)} row(s) skipped.")
    if flagged:
        print(f"error: {len(flagged)} row(s) load from the internet in a shape this tool does not recognise, or "
              "could not be read. That markup was left in place; look at those rows before relying on this "
              "step.", file=sys.stderr)
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
