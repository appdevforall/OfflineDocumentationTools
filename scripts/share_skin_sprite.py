#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
share_skin_sprite.py

Moves the SVG icon sprite that Help+Manual's skin copies into its pages out of
them and into one shared script, i/js/hm_sprite.js (ADFA-4195). In the
2026-10-13 database 359 of the 366 text/html rows under i/ carry it: the same
16,817 bytes in each, an

    <svg version="1.1" xmlns="http://www.w3.org/2000/svg" style="display: none;">
    <defs> 27 x <g class="svgsprite" id="..."> ... </defs></svg>

block, 6.0 MB decoded in all. The other seven are the skin's templates
(_hmXpopup.htm, _hmXtopic.htm, _hm_print_window.htm, _hmpermalink.html) and its
contents, search and index pages (hmcontent.html, hmftsearch.html,
hmkwindex.html), none of which uses an icon.

Why the icons cannot simply move to an icons.svg file: the pages hold 7,898
<use xlink:href="#id"> references to 21 of the icons, and hm_webhelp.js,
hmDoToggle.js and hm_autotoc.js create or change such references at run time.
A "#id" reference finds its icon only in the same document, so the sprite has
to stay in each page's DOM.

What it does: each page's block is replaced with

    <script src="./js/hm_sprite.js"></script>

and hm_sprite.js holds the block, byte for byte, in a string. As the parser
runs it, the script inserts that markup just before itself with
document.currentScript.insertAdjacentHTML("beforebegin", ...) and then removes
itself, so the DOM ends up as it was: the same nodes in the same place, ahead of
every <use> that refers to them. insertAdjacentHTML rather than
document.write: run by the parser either would do, but the HTML standard
strongly discourages document.write, and run any other way it is ignored (an
async or injected script) or replaces the whole page (code evaluated after the
page has loaded, which is how jQuery's $.getScript runs a script). The script
here only adds the sprite and removes its own element.

In-page navigation does not reload the page: hm_webhelp.js fetches
jstopics/<topic>.js with $.getScript, and hmLoadTopic() rewrites elements of the
topic area (title, breadcrumbs, header, div#hmpagebody_scroller), so the sprite
inserted when the page loaded stays in the DOM. No jstopics row carries the
sprite; those rows are left alone, as is every row outside i/.

Measured on the 2026-10-13 database (brotli at quality 11 against its
dictionary, which reproduces the pages' total stored size exactly): the pages go
from 971,640 to 804,743 stored bytes and the script takes 937, a net saving of
165,960 stored bytes; decoded, the pages lose 6,022,584 bytes and the script
adds 17,630 once.

It is an interim measure until the skin stops inlining the sprite; then this
step finds no block and does nothing. It also shrinks what ADFA-6617, which
would optimise the sprite and retrain the dictionary, can gain from the sprite:
stored once, in a 937-byte script, optimising it can save only part of that.

What it refuses, naming the row and exiting 1 at the end - a page it refuses
keeps its inline block and renders as before:
  - a block that differs from the one most pages carry. All copies should be
    byte-identical; the most common is shared, since each page it replaces
    carried exactly it. If no version is more common than every other, no page
    changes;
  - a block that is not a direct child of <body> (only whitespace and complete
    comments between the <body> tag and it), whose replacement script would not
    put it back where it was; one with a nested <svg>, never closed, appearing
    twice or holding non-ASCII bytes; icons (class="svgsprite") outside a block,
    which means the skin writes the sprite some way this step does not know;
  - a page in a subfolder of i/, where ./js/hm_sprite.js does not resolve to
    i/js/; a page that already loads hm_sprite.js;
  - a row that cannot be read.
The script row is written as text/javascript, compressed the way that
ContentTypes row says (against the shared CompressionDictionary when the
database has one), with the languageID and templateId every other i/js/ row
has; if those rows disagree, nothing is written. An existing hm_sprite.js with
exactly the content, type, languageID and templateId this run would write is
reused; one with anything else is refused, and then nothing is written at all.

Each changed page is edited on its decoded bytes - the block's bytes replaced,
every other byte kept - and re-compressed; pages without the block keep their
stored bytes, never re-compressed. A page over the 1 MiB chunk size is read
whole across its "<path>-1", "-2", ... rows and written back re-chunked.

Where it goes in the pipeline: before the `cp DB migrated.db` snapshot that
update_media_references.py --before diffs against, like the other steps that
edit pages. update_media_references.py --save-originals records each page it
rewrites by the sha256 of its text, and --restore-originals leaves alone a page
whose text no longer matches, so editing pages after it would make them
unrestorable. With the ADFA-6589 steps (PR #41) the order is
remove_skin_font_comment.py, bundle_remote_images.py, remove_remote_loads.py,
then this step, then check_remote_loads.py. After the three that edit pages,
because their order among themselves has reasons of its own and this step does
not depend on it: their edits leave every sprite block intact, and on the
2026-10-13 database this step changes the same 359 pages and writes the same
script after them as on the database alone. Before check_remote_loads.py,
which writes nothing and so has to run last to check the pages as they ship.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards - all three only when there is something to
change, so a second run writes nothing at all. --dry-run does all the work and
reports what would change without writing.

    uv run scripts/share_skin_sprite.py documentation.db --dry-run
    uv run scripts/share_skin_sprite.py documentation.db

Recovering: the timestamped backup taken before the first write is the whole
undo.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from optimize_db_media import (
    BrotliCodec, backup_database, digest, human, is_continuation_path, load_dictionary, reassemble, write_chunks,
)
from update_media_references import replace_row

# The block's opening tag as the skin writes it; the block ends at the first
# </svg> after it.
SPRITE_OPEN = b'<svg version="1.1" xmlns="http://www.w3.org/2000/svg" style="display: none;">'
SPRITE_CLOSE = b"</svg>"
# Every icon in the sprite is a <g class="svgsprite">. Seen outside a block, it
# means the skin has changed how it writes the sprite.
ICON_MARKER = b'class="svgsprite"'

PAGE_DIR = "i/"
SCRIPT_PATH = "i/js/hm_sprite.js"
SCRIPT_TAG = b'<script src="./js/hm_sprite.js"></script>'
PAGE_TYPE = "text/html"
SCRIPT_TYPE = "text/javascript"

_BODY_TAG = re.compile(rb"<body(?:\s[^>]*)?>", re.I)
_BLANKS_AND_COMMENTS = re.compile(rb"(?:\s|<!--.*?-->)*", re.S)


def find_sprite(page: bytes) -> tuple:
    """((start, end) byte span of the page's sprite block, or None;
    the reason the page cannot be changed, or None)."""
    count = page.count(SPRITE_OPEN)
    if count == 0:
        if ICON_MARKER in page:
            return None, 'carries Help+Manual icons (class="svgsprite") but no sprite block this step knows'
        return None, None
    if count > 1:
        return None, f"carries the sprite block {count} times"
    start = page.find(SPRITE_OPEN)
    close = page.find(SPRITE_CLOSE, start)
    if close < 0:
        return None, "has a sprite block that is never closed"
    end = close + len(SPRITE_CLOSE)
    block = page[start:end]
    if b"<svg" in block[len(SPRITE_OPEN):].lower():
        return None, "has a nested <svg> in its sprite block, so where the block ends is not known"
    if ICON_MARKER in page[:start] or ICON_MARKER in page[end:]:
        return None, 'carries Help+Manual icons (class="svgsprite") outside its sprite block'
    # ASCII reads the same whatever charset the page and the script are
    # decoded with, so the script's string is the block's characters exactly.
    if not block.isascii():
        return None, "has non-ASCII bytes in its sprite block"
    body_tags = list(_BODY_TAG.finditer(page, 0, start))
    if len(body_tags) != 1 or not _BLANKS_AND_COMMENTS.fullmatch(page, body_tags[0].end(), start):
        # The script has to sit where the block did, as a child of <body>, for
        # insertAdjacentHTML to parse the markup in the context the page did.
        return None, ("has its sprite block somewhere other than directly inside <body>, so the script would "
                      "not put it back where it was")
    if SCRIPT_TAG in page:
        return None, "already loads hm_sprite.js and also carries the sprite block"
    return (start, end), None


def replace_block(page: bytes, span: tuple) -> bytes:
    """`page` with the block at `span` replaced by the script tag and every
    other byte kept."""
    start, end = span
    return page[:start] + SCRIPT_TAG + page[end:]


def describe(block: bytes) -> str:
    return f"{len(block):,} bytes, sha256 {digest(block)[:12]}..."


def pick_shared(blocks: dict) -> tuple:
    """(the block to share or None, {path: reason} for every page left with
    its own), given {path: block}. The most common block is shared: each page
    it replaces carried exactly that block, so which version wins changes no
    page's DOM, only how many pages shrink. With no single most common block,
    none is."""
    ranked = Counter(blocks.values()).most_common()
    if not ranked:
        return None, {}
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, {path: (f"carries one of {len(ranked)} different sprite blocks ({describe(block)}), none "
                             "carried by more pages than every other; left in place")
                      for path, block in blocks.items()}
    shared, count = ranked[0]
    return shared, {path: (f"carries a sprite block ({describe(block)}) that differs from the one {count} "
                           f"page(s) carry ({describe(shared)}); left in place")
                    for path, block in blocks.items() if block != shared}


def js_string(text: str) -> str:
    """`text` as a JavaScript string literal. A JSON string is one; "</" and
    "<!--" are escaped as well, so the script stays correct even pasted
    inline, where either would end or confuse the <script> element."""
    return json.dumps(text).replace("</", "<\\/").replace("<!--", "<\\!--")


def script_for(block: bytes) -> bytes:
    """The hm_sprite.js that puts `block` into the document just before its
    own <script> element, then removes that element. `block` is ASCII
    (find_sprite refuses any other)."""
    return (
        "/* Help+Manual's icon sprite for the i/ pages (ADFA-4195), written by share_skin_sprite.py. */\n"
        "(function (script) {\n"
        f"\tscript.insertAdjacentHTML(\"beforebegin\", {js_string(block.decode('ascii'))});\n"
        "\tscript.parentNode.removeChild(script);\n"
        "})(document.currentScript);\n"
    ).encode("ascii")


def script_row_settings(conn) -> tuple:
    """((contentTypeID, compression, languageID, templateId) for the script
    row, or None; the reason it cannot be written, or None)."""
    js_type = conn.execute("SELECT id, compression FROM ContentTypes WHERE value = ?", (SCRIPT_TYPE,)).fetchone()
    if js_type is None:
        return None, f"ContentTypes has no {SCRIPT_TYPE} row"
    pairs = conn.execute(
        "SELECT DISTINCT languageID, templateId FROM Content WHERE path GLOB 'i/js/*' AND path != ?", (SCRIPT_PATH,)
    ).fetchall()
    if len(pairs) != 1:
        found = ", ".join(f"({lang}, {tid})" for lang, tid in sorted(pairs)) or "none"
        return None, f"the other i/js/ rows do not share one (languageID, templateId) to copy; found {found}"
    return (js_type[0], js_type[1], *pairs[0]), None


def check_existing_script(conn, codec, expected: bytes, wanted: tuple) -> tuple:
    """("absent" | "same" | "conflict", reason or None) for the row at
    SCRIPT_PATH, against the content and (contentTypeID, languageID,
    templateId) this run would write."""
    row = conn.execute(
        "SELECT c.content, c.contentTypeID, c.languageID, c.templateId, ct.value, ct.compression "
        "FROM Content c LEFT JOIN ContentTypes ct ON c.contentTypeID = ct.id WHERE c.path = ?", (SCRIPT_PATH,)
    ).fetchone()
    if row is None:
        return "absent", None
    content, content_type_id, language_id, template_id, type_value, compression = row
    if type_value is None:
        return "conflict", f"its contentTypeID {content_type_id} is not in ContentTypes"
    if (content_type_id, language_id, template_id) != wanted:
        return "conflict", (f"has contentTypeID {content_type_id} ({type_value}), languageID {language_id} and "
                            f"templateId {template_id}, not {wanted[0]}, {wanted[1]} and {wanted[2]}")
    try:
        decoded = codec.decompress(reassemble(conn, SCRIPT_PATH, content), compression)
    except Exception as exc:  # noqa: BLE001 - reported as a conflict, not raised
        return "conflict", f"could not be read: {exc}"
    if decoded != expected:
        return "conflict", f"holds {describe(decoded)}, not the script this run would write"
    return "same", None


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    codec = None
    changes, skipped, refusal = [], {}, None
    script = script_stored = settings = None
    try:
        codec = BrotliCodec(load_dictionary(conn))
        rows = conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value = ? AND c.path GLOB 'i/*' ORDER BY c.path", (PAGE_TYPE,)
        ).fetchall()
        lengths = {row[0]: len(row[1]) for row in rows}
        bases = [row for row in rows if not is_continuation_path(lengths, row[0])]
        print(f"Scanning {len(bases)} {PAGE_TYPE} row(s) under {PAGE_DIR} for Help+Manual's icon sprite "
              f"({len(rows) - len(bases)} chunk-continuation row(s) folded into their base)...")

        # Reassembled here on the main thread, not in the pool: a sqlite3
        # connection may only be used from the thread that created it, and
        # reassemble() queries for continuation rows.
        work = [(row[0], reassemble(conn, row[0], row[1]), len(row[1]), *row[2:]) for row in bases]

        def scan(item):
            """Decompress one page and find its sprite block. Touches no database."""
            path, full, base_len, language_id, content_type_id, template_id, compression = item
            try:
                raw = codec.decompress(full, compression)
            except Exception as exc:  # noqa: BLE001 - one unreadable row is not the run
                return path, None, f"could not be read: {exc}"
            span, problem = find_sprite(raw)
            if span is not None and "/" in path[len(PAGE_DIR):]:
                problem = f"is in a subfolder of {PAGE_DIR}, where ./js/hm_sprite.js does not resolve to i/js/"
            if problem is not None or span is None:
                return path, None, problem
            return path, {"raw": raw, "span": span, "block": raw[span[0]:span[1]], "stored": len(full),
                          "base_len": base_len, "language_id": language_id, "content_type_id": content_type_id,
                          "template_id": template_id, "compression": compression}, None

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            scanned = list(pool.map(scan, work))
        skipped = {path: problem for path, _page, problem in scanned if problem is not None}
        found = {path: page for path, page, _problem in scanned if page is not None}

        shared, differing = pick_shared({path: page["block"] for path, page in found.items()})
        skipped.update(differing)
        found = {path: page for path, page in found.items() if path not in differing}

        if found:
            script = script_for(shared)
            settings, refusal = script_row_settings(conn)
            if settings is not None:
                state, reason = check_existing_script(conn, codec, script, (settings[0], settings[2], settings[3]))
                if state == "conflict":
                    refusal = f"{SCRIPT_PATH} already exists and {reason}"
                elif state == "absent":
                    script_stored = codec.compress(script, settings[1])

        if found and refusal is None:
            def rebuild(item):
                """One page with its block replaced, re-compressed. Touches no database."""
                path, page = item
                new_raw = replace_block(page["raw"], page["span"])
                new_stored = codec.compress(new_raw, page["compression"])
                return (path, page["base_len"], new_stored, page["language_id"], page["content_type_id"],
                        page["template_id"], len(page["raw"]) - len(new_raw), page["stored"] - len(new_stored))

            with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
                changes = list(pool.map(rebuild, sorted(found.items())))
        else:
            script_stored = None

        decoded_saved = sum(c[6] for c in changes)
        stored_saved = sum(c[7] for c in changes)
        script_decoded = len(script) if script_stored is not None else 0
        script_stored_len = len(script_stored) if script_stored is not None else 0

        if refusal is not None:
            print(f"error: {refusal}. Nothing was changed: no page is edited unless {SCRIPT_PATH} holds exactly "
                  "the script that puts back the block it takes out.", file=sys.stderr)
        elif changes:
            if script_stored is not None:
                script_note = (f"adds {SCRIPT_PATH} ({human(script_decoded)} decoded / {human(script_stored_len)} "
                               "stored byte(s))")
            else:
                script_note = f"reuses the existing {SCRIPT_PATH}, which already holds this script"
            print(f"{len(changes)} page(s) carry the sprite block ({describe(shared)}); replacing it saves "
                  f"{human(decoded_saved)} decoded and {human(stored_saved)} stored byte(s) on the pages, and "
                  f"{script_note}.")
        else:
            print("No page carries a sprite block this step can share - nothing to change.")
        for path, reason in sorted(skipped.items()):
            print(f"  skipped {path}: {reason}", file=sys.stderr)
        if skipped:
            print(f"{len(skipped)} row(s) skipped and left exactly as they were (listed above).", file=sys.stderr)

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        elif changes:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                if script_stored is not None:
                    content_type_id, _compression, language_id, template_id = settings
                    write_chunks(conn, SCRIPT_PATH, script_stored, language_id, content_type_id, template_id,
                                 lambda m: print(m, file=sys.stderr), update_base=False)
                for path, base_len, new_stored, lang, ctid, tid, decoded, stored in changes:
                    replace_row(conn, path, base_len, new_stored, lang, ctid, tid,
                                lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [SPRITE] {path}: {human(decoded)} decoded / {human(stored)} stored byte(s) saved")
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
    print(f"\nDone. {verb} {len(changes)} page(s) and {'1 script row' if script_stored_len else 'no script row'}, "
          f"saving {human(decoded_saved - script_decoded)} decoded and {human(stored_saved - script_stored_len)} "
          f"stored byte(s) net; {len(skipped)} row(s) skipped.")
    if skipped:
        print(f"error: {len(skipped)} row(s) carry the sprite in a form this step does not share, or could not be "
              "read. They were left as they were; look at them before relying on this step.", file=sys.stderr)
    return 1 if skipped or refusal is not None else 0


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
