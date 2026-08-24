#!/usr/bin/env python3
"""
Populates a documentation.db-schema SQLite database with the same content
templates/page.peb and this project's md_to_json.py conversion pipeline
produce for the static site, replacing what's currently at k/html/*.

Usage:
    python3 populate_db.py <docs-root> <config> <images-zip> [db-path]
        [--tree-file kr.tree] [--topics-subdir topics]
        [--blacklisted-element-titles "Ancestor\\/.../Element Title" ...]

--blacklisted-element-titles names <toc-element toc-title="..."> element(s)
to drop from kr.tree entirely before anything else below reads it: the
element and its whole subtree get no nav entry, none of their .md sub-topics
get converted or inserted, and any *other*, non-blacklisted page's in-content
link to one of those .md files renders broken/styled (same as any other
unresolved link - see broken-ext-link-color) rather than pointing somewhere
that no longer exists.

Each value is the *full* toc-title path from a top-level <toc-element> down
to the one being blacklisted, since toc-title alone is not unique across
kr.tree (e.g. plenty of "Overview"s). Levels are joined by the two-character
sequence "\\/" (backslash then slash) rather than a bare "/", because a bare
"/" routinely appears *within* a single real toc-title (e.g. "Swift/
Objective-C and C interop") and this way that overwhelmingly common case
needs no escaping at all - only the rare level separator does. So to
blacklist the "Swift/Objective-C and C interop" element nested under the
top-level "Interoperability" element, pass
"Interoperability\\/Swift/Objective-C and C interop": split on "\\/" that's
["Interoperability", "Swift/Objective-C and C interop"], matching kr.tree's
actual nesting - the inner "/" is left untouched since it wasn't preceded by
a backslash.

<db-path> defaults to "documentation.db". A safety backup (via SQLite's
"VACUUM INTO", which is safe even against a live/WAL-mode database) is
written next to it before any changes: "<db-path>.backup-<timestamp>".

<images-zip> is Writerside's own official image output for this doc set
(e.g. "webHelpImages.zip", found next to kr.tree) - a flat archive with no
subdirectories, one entry per image, already exactly as Writerside itself
would serve them. Rather than re-deriving image content/sizing ourselves
from the raw source tree (which is a plain, uncompressed truecolor export -
several times larger than what a real Writerside build actually ships,
since it applies its own image optimization we have no easy way to
replicate faithfully), this script just copies that zip's entries in
directly, so k/html/images/<name> ends up byte-for-byte what Writerside
itself produces.

What this does, inside a single transaction (rolled back on any error):
  1. Deletes every Content row with path LIKE 'k/html/%' or 'assets/%' - the
     former includes the existing *.html doc pages AND everything else
     parked there (images, the old Writerside JS bundle under
     k/html/frontend/, none of which this script replaces); the latter is
     wherever a previous run of this script put images/CSS/JS, all of
     which get freshly re-inserted below.
  2. Upserts templates/page.peb and templates/nav.peb into Templates.
     page.peb's <aside id="sidebar"> is adapted for this deployment: rather
     than a build-time {% include "nav.html" %} (this server's Pebble
     templates are fully self-contained - see the existing "layout.pebble"
     row, which has no includes at all), it's rendered empty with a
     data-nav-src attribute, and assets/sidebar.js fetches + injects the
     nav content client-side once the page loads. nav.peb itself needs no
     changes for this - see the next point.
  3. Converts every topic .md file the same way md_to_json.py does (same
     Converter, same config-driven link/image coloring), except every
     internal link/image is resolved against "k/html/" instead of "/" - so
     the exact same page ids from kr.tree/topics/ can be reused unmodified
     as nav.json's node ids, and templates/nav.peb's existing
     href="/{{ node.id }}.html" naturally produces the right k/html/ URLs
     with no template changes needed. Old page paths were flat
     ("k/html/books.html", no subfolders) even for topics nested under a
     subdirectory in the source tree (e.g. "tour/kotlin-tour-hello-world.md"),
     so ids are flattened to their bare filename stem to match that old
     convention (filenames are already unique across the whole topics/ tree).
     Images resolve the same way, flattened to their bare filename to match
     <images-zip>'s own flat layout - the same convention Writerside itself
     uses to resolve a bare "foo.png" reference in the first place.
     Also synthesizes an empty placeholder page (no title, no blocks) at
     k/html/home.html, since kr.tree's start page (home.topic) isn't a .md
     file and so never goes through this conversion - without it, nav's
     "Home" link had nowhere to go. It still renders through page.peb, so
     the sidebar nav shows up on it like any other page.
  4. Inserts one Content row per page at k/html/<flattened-id>.html,
     JSON-encoded and Brotli-compressed against this database's shared
     CompressionDictionary (see ADFA-5153; trained once and reused forever -
     never retrained - since a dictionary-compressed row is only decodable
     against the exact dictionary it was compressed with), with prev/next
     computed from kr.tree's document order, the same way RenderDocs.java
     does it for the static site. contentTypeID is the "text/html" row
     (12|text/html|brotli), not "application/json": the stored bytes are
     JSON, but templateId points the server at page.peb to render that JSON
     into HTML before a browser ever sees it, so the Content-Type the server
     actually sends back should describe that rendered output, not the
     storage format.
  5. Builds the same navigation tree build_nav.py does from kr.tree, and
     inserts it as one more Content row (see NAV_CONTENT_PATH below),
     associated with the nav.peb template and the same "text/html"
     contentTypeID as every page, for the same reason.
  6. Inserts every entry in <images-zip> at k/html/images/<name>, and
     assets/docs.css, assets/tabs.js, assets/sidebar.js at "assets/<name>"
     (matching page.peb's own href/src for them). All as raw, untemplated
     Content rows (templateId 0), content-type/compression looked up from
     ContentTypes by file extension.
  7. Runs VACUUM on the database (outside the transaction above - SQLite
     refuses to VACUUM inside one). Deleting/replacing rows only ever frees
     internal pages for future reuse, it never shrinks the file on disk, so
     without this the file keeps growing over repeated runs even when the
     actual data gets smaller.

Every one of those inserts goes through insert_chunked_content, which splits
anything over 1,048,576 bytes (post-compression) into that many-byte
fragments stored as additional rows at "<path>-1", "<path>-2", ... - the
exact scheme WebServer.kt's request handler expects (see CHUNK_SIZE): it
detects a fragmented row purely by the first row's content being exactly
that many bytes, then keeps requesting "<path>-N" for increasing N until it
gets either a shorter fragment or a missing row. Every file that ended up
chunked is logged by name at the end of the run.
"""
import argparse
import atexit
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

from build_nav import build_node
from md_to_json import (
    Converter,
    build_topic_index,
    load_config,
    load_variables,
    make_markdown_it,
)

NAV_CONTENT_PATH = "k/html/_nav.html"
# Content.path prefix images are stored under, and the URL prefix baked into
# every "src=" reference to one of them at conversion time (see Converter's
# image_url_prefix) - the same string except for the leading "/", since
# Content.path values never have one. Shared with insert_optimized_media.py,
# which reinserts these same rows after an out-of-band optimization pass.
IMAGES_DB_PATH_PREFIX = "k/html/images/"
IMAGES_URL_PREFIX = f"/{IMAGES_DB_PATH_PREFIX}"
# Page/nav rows store JSON, but templateId points the server at page.peb/
# nav.peb to render that JSON into HTML before it ever reaches a browser -
# so the Content-Type the server actually sends back should describe that
# rendered output (text/html), not the JSON it's stored as internally.
PAGE_CONTENT_TYPE = "text/html"

# Must match WebServer.kt's "contentChunkSize" exactly (1024 * 1024): its
# request handler decides a row is fragmented purely by the first row's
# content being exactly this many bytes, so this can't just be "close to
# 1MB" - it has to be the identical constant on both sides.
CHUNK_SIZE = 1024 * 1024
LANGUAGE = "en-US"

PAGE_PEB_STATIC_ASIDE = '  <aside class="docs-sidebar" id="sidebar">\n    {% include "nav.html" %}\n  </aside>'
HOME_PAGE_ID = "k/html/home"

# File extension -> ContentTypes.value, for the raw (untemplated) files
# inserted alongside the JSON pages: images from <images-zip> and page.peb's
# own CSS/JS. Compression is looked up from ContentTypes itself
# (get_content_type below) rather than assumed here, so it always matches
# whatever this database actually declares.
EXTENSION_TO_CONTENT_TYPE = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".css": "text/css",
    ".js": "text/javascript",
}

# pngquant is a lossy PNG-only compressor; it can't touch svg/gif/jpeg, so
# this is the only content type run through it before insertion.
PNGQUANT_CONTENT_TYPE = "image/png"
PNGQUANT_QUALITY = "65-80"

# Single-row table: the whole documentation.db has exactly one shared Brotli
# dictionary, embedded here so it always ships in sync with the content
# compressed against it (see ADFA-5153). The id/CHECK pair enforces "exactly
# one row" at the schema level - a second INSERT fails outright instead of
# silently leaving two rows for a reader to pick between arbitrarily.
DICTIONARY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS CompressionDictionary (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    data BLOB NOT NULL
);
"""
# 256 KiB fast-cover dictionary was the measured sweet spot in ADFA-5153
# (16.26x held-out ratio vs. 8.20x undictionaried; a larger, exhaustively-
# trained dictionary bought another 0.35x for 144x the training time - not
# worth it).
DEFAULT_DICT_SIZE = 256 * 1024


def find_pngquant() -> str:
    """Locates the pngquant executable on PATH. Raises if it's missing,
    rather than silently inserting uncompressed PNGs - that'd be a silent
    regression in output size that's easy to miss."""
    path = shutil.which("pngquant")
    if path is None:
        raise RuntimeError("pngquant not found on PATH; install it (e.g. `apt install pngquant`) and retry")
    return path


def compress_png_with_pngquant(data: bytes, pngquant_path: str, name: str) -> bytes:
    """Runs pngquant on a single PNG's raw bytes (stdin -> stdout, no temp
    files), returning the compressed bytes. Falls back to the original bytes
    unchanged if pngquant declines to compress this particular image (e.g.
    its exit code 99 means the result would fall below --quality's floor) or
    otherwise fails, since a slightly larger PNG beats a missing/corrupt one."""
    result = subprocess.run(
        [pngquant_path, "--quality", PNGQUANT_QUALITY, "--strip", "--force", "--output", "-", "-"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0 or not result.stdout:
        print(
            f"warning: pngquant declined to compress {name!r} "
            f"(exit {result.returncode}: {result.stderr.decode(errors='replace').strip()}); keeping original",
            file=sys.stderr,
        )
        return data
    return result.stdout


def find_tool(name: str) -> str:
    """Locates an executable on PATH. Raises if it's missing, rather than
    silently falling back to some other behavior - see find_pngquant."""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} not found on PATH; install it and retry")
    return path


def train_dictionary(samples: list, dict_size: int = DEFAULT_DICT_SIZE) -> bytes:
    """Trains a zstd fast-cover dictionary from `samples` (a list of byte
    strings) and returns its raw bytes. That output is usable directly as
    Brotli's raw `-D` dictionary (validated in ADFA-5153) - zstd's fast-cover
    trainer is dramatically cheaper than Brotli's own dictionary tooling for
    equivalent quality. Needs a few dozen samples at minimum; zstd's trainer
    refuses ("nb of samples too low") on too few/too-small inputs, since a
    dictionary trained on a handful of samples won't generalize.
    """
    zstd_path = find_tool("zstd")
    work_dir = Path(tempfile.mkdtemp(prefix="brotli-dict-train-"))
    try:
        sample_paths = []
        for i, sample in enumerate(samples):
            sample_path = work_dir / f"sample_{i:06}.bin"
            sample_path.write_bytes(sample)
            sample_paths.append(str(sample_path))
        dict_path = work_dir / "dictionary.bin"
        result = subprocess.run(
            [zstd_path, "--train-fastcover", f"--maxdict={dict_size}", "-o", str(dict_path), *sample_paths],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"zstd --train-fastcover failed: {result.stderr.decode(errors='replace').strip()}")
        return dict_path.read_bytes()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


class DictionaryCompressor:
    """Compresses/decompresses bytes against a fixed raw Brotli dictionary,
    shelling out to the `brotli` CLI (the installed Python `brotli` package
    has no dictionary parameter at all). A dictionary-compressed stream and a
    plain one are NOT interchangeable at decode time, but the two directions
    of that mismatch behave differently, and the difference matters - measured
    against the real documentation.db, not assumed:

      * Decoding a dictionary-compressed row with NO dictionary is loud: 398
        of 400 sampled rows raised outright, and the other 2 returned byte-
        identical output because the encoder never referenced the dictionary
        for them. Zero returned wrong bytes. `migrate_content_to_dictionary_
        brotli.py` relies on this direction, and WebServer.kt's plain-decode
        fallback is safe for the same reason.

      * Decoding with the WRONG dictionary is the silent case. Perturbing one
        16 KiB region of the real dictionary and decoding real rows gave 50%
        outright failures, 38% that decoded with no error into *different
        bytes*, and 12% byte-identical (the perturbed region was never
        referenced). Nothing at runtime catches that 38%.

    So every row compressed via this class must
    be decompressed via a `DictionaryCompressor` built from the exact same
    dictionary bytes, and that dictionary must never change once anything
    has been compressed against it - see CompressionDictionary (the single
    source of truth for those bytes) and load_or_create_dictionary's
    never-retrain guarantee.

    The dictionary is written once to a private temp file for this instance's
    lifetime (each compress/decompress call reuses it) rather than per call.
    """

    def __init__(self, dictionary_data: bytes):
        self._brotli_path = find_tool("brotli")
        self._work_dir = Path(tempfile.mkdtemp(prefix="brotli-dict-"))
        self._dict_path = self._work_dir / "dictionary.bin"
        self._dict_path.write_bytes(dictionary_data)
        # Safety net for callers that can't cleanly scope a `with` block around
        # every instance -- e.g. one created per worker thread in a thread pool,
        # where no single point of control can call close() on each. Safe to
        # also call close() explicitly afterward: shutil.rmtree(ignore_errors=True)
        # tolerates a directory that's already gone.
        atexit.register(self.close)

    def _run(self, *extra_args: str, data: bytes) -> bytes:
        result = subprocess.run(
            [self._brotli_path, "-D", str(self._dict_path), *extra_args, "-c"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"brotli failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def compress(self, data: bytes) -> bytes:
        return self._run(data=data)

    def decompress(self, data: bytes) -> bytes:
        return self._run("-d", data=data)

    def close(self) -> None:
        shutil.rmtree(self._work_dir, ignore_errors=True)

    def __enter__(self) -> "DictionaryCompressor":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def load_dictionary(conn) -> bytes:
    """Returns the CompressionDictionary bytes already stored in this
    database. Raises if the table doesn't exist or is empty - callers that
    only ever run against a database populate_db.py already touched (e.g.
    insert_optimized_media.py) should never need to train a new one."""
    row = conn.execute(
        "SELECT data FROM CompressionDictionary WHERE id = 1"
    ).fetchone() if _table_exists(conn, "CompressionDictionary") else None
    if row is None:
        raise RuntimeError(
            "CompressionDictionary is missing or empty; run populate_db.py against this database first"
        )
    return row[0]


def load_or_create_dictionary(conn, samples_for_training: list, dict_size: int = DEFAULT_DICT_SIZE) -> bytes:
    """Returns the dictionary bytes stored in CompressionDictionary, training
    a new one from `samples_for_training` and storing it if the table
    doesn't exist yet or is empty. Never retrains an existing dictionary:
    since dictionary-compressed content elsewhere in this same database can
    only ever be decoded with the exact dictionary it was compressed against
    (see DictionaryCompressor), silently replacing an already-populated
    dictionary would orphan every row compressed against the old one."""
    conn.execute(DICTIONARY_TABLE_SQL)
    row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
    if row is not None:
        return row[0]
    dictionary_data = train_dictionary(samples_for_training, dict_size)
    conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary_data,))
    return dictionary_data


def _table_exists(conn, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone() is not None


def backup_database(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{timestamp}")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(backup_path),))
    finally:
        conn.close()
    return backup_path


def flatten_to_db_ids(stem_to_id: dict) -> dict:
    """Bare filename stem -> "k/html/<stem>" (no ".html"), matching the old
    flat k/html/*.html convention - and, since it's exactly what
    templates/nav.peb's href="/{{ node.id }}.html" expects, that template
    needs no changes to produce correct URLs against this database."""
    return {stem: f"k/html/{stem}" for stem in stem_to_id}


def load_zip_image_names(images_zip: Path) -> list:
    with zipfile.ZipFile(images_zip) as zf:
        return sorted(name for name in zf.namelist() if not name.endswith("/"))


def get_id(conn, table: str, value: str) -> int:
    row = conn.execute(f"SELECT id FROM {table} WHERE value = ?", (value,)).fetchone()
    if row is None:
        raise RuntimeError(f"{table} has no row for {value!r}; expected it to already exist in this database")
    return row[0]


def get_content_type(conn, value: str) -> tuple:
    """Returns (id, compress) for a ContentTypes.value, compress being
    whether its declared compression column says "brotli" (anything else,
    e.g. "none", means store the bytes as-is)."""
    row = conn.execute("SELECT id, compression FROM ContentTypes WHERE value = ?", (value,)).fetchone()
    if row is None:
        raise RuntimeError(f"ContentTypes has no row for {value!r}; expected it to already exist in this database")
    return row[0], row[1] == "brotli"


FRAGMENT_SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")


def fragment_chain(conn, base_path: str) -> list:
    """Every "<base_path>-<N>" continuation row present, as (n, path) sorted by
    n - found by LIKE query and parsed suffix rather than by probing
    constructed paths, so it does not matter what N the chain starts at.

    Probing "<base_path>-1" first (what reassembly used to do) silently returns
    a truncated stream for an ADFA-5171 chain numbered from -2, which then
    fails to decompress and looks indistinguishable from an already-migrated
    row. The LIKE pattern deliberately over-matches - `_` and `%` in a path are
    wildcards, and the suffix is not constrained to digits - so the regex
    re-check below is what makes the result exact. Never build a DELETE or
    UPDATE straight off that pattern.
    """
    rows = conn.execute("SELECT path FROM Content WHERE path LIKE ?", (f"{base_path}-%",)).fetchall()
    chain = []
    for (path,) in rows:
        match = FRAGMENT_SUFFIX_RE.match(path)
        if match and match.group(1) == base_path:
            chain.append((int(match.group(2)), path))
    chain.sort(key=lambda item: item[0])
    return chain


def insert_chunked_content(conn, path: str, language_id: int, content_type_id: int, template_id: int,
                            data: bytes, chunked_log: list) -> None:
    """Inserts `data` (already fully compressed, if applicable - chunking
    happens on the final bytes, matching WebServer.kt reassembling fragments
    before ever decompressing) at `path`, splitting anything over
    CHUNK_SIZE into that many-byte fragments stored as additional rows at
    "<path>-1", "<path>-2", ... - see CHUNK_SIZE's own comment for why the
    boundary has to be exact. Every fragment reuses the first row's
    languageID/contentTypeID/templateId for consistency, though the server
    only ever reads those columns off the first row - fragment rows are
    fetched by path alone. Appends (path, total size, chunk count) to
    chunked_log for anything that needed more than one row."""
    conn.execute(
        "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
        (path, language_id, data[:CHUNK_SIZE], content_type_id, template_id),
    )
    if len(data) <= CHUNK_SIZE:
        return

    fragment_number = 1
    offset = CHUNK_SIZE
    while offset < len(data):
        conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            (f"{path}-{fragment_number}", language_id, data[offset:offset + CHUNK_SIZE], content_type_id, template_id),
        )
        offset += CHUNK_SIZE
        fragment_number += 1
    chunked_log.append((path, len(data), fragment_number))  # fragment_number == total chunk count here


def insert_file(conn, data: bytes, name: str, db_path: str, language_id: int, content_type_cache: dict,
                 chunked_log: list, pngquant_path: str, compressor: "DictionaryCompressor") -> bool:
    """Inserts one raw (templateId 0) file's bytes as a Content row (chunked
    via insert_chunked_content if needed). name is only used to look up its
    content type by extension. Returns False (and skips it, with a warning)
    for an extension not in EXTENSION_TO_CONTENT_TYPE instead of guessing at
    a content type. PNGs are run through pngquant first - the only content
    type it's compatible with - before the usual dictionary-Brotli compression."""
    content_type_value = EXTENSION_TO_CONTENT_TYPE.get(Path(name).suffix.lower())
    if content_type_value is None:
        print(f"warning: no known content type for {name!r}; skipping", file=sys.stderr)
        return False
    if content_type_value not in content_type_cache:
        content_type_cache[content_type_value] = get_content_type(conn, content_type_value)
    content_type_id, compress = content_type_cache[content_type_value]

    if content_type_value == PNGQUANT_CONTENT_TYPE:
        data = compress_png_with_pngquant(data, pngquant_path, name)
    if compress:
        data = compressor.compress(data)
    insert_chunked_content(conn, db_path, language_id, content_type_id, 0, data, chunked_log)
    return True


def upsert_template(conn, name: str, content: str) -> int:
    conn.execute(
        "INSERT INTO Templates (name, content) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET content = excluded.content",
        (name, content.encode("utf-8")),
    )
    return conn.execute("SELECT id FROM Templates WHERE name = ?", (name,)).fetchone()[0]


def build_db_page_template(page_peb_path: Path) -> str:
    text = page_peb_path.read_text(encoding="utf-8")
    if PAGE_PEB_STATIC_ASIDE not in text:
        raise RuntimeError(
            f"{page_peb_path} no longer contains the expected sidebar <aside> block; "
            "update build_db_page_template()/PAGE_PEB_STATIC_ASIDE to match its new shape"
        )
    db_snippet = f'  <aside class="docs-sidebar" id="sidebar" data-nav-src="/{NAV_CONTENT_PATH}"></aside>'
    return text.replace(PAGE_PEB_STATIC_ASIDE, db_snippet)


def flatten_nav_ids(nodes: list) -> list:
    """Depth-first walk of the nav tree, in document order, collecting only linkable (id-bearing) nodes."""
    flat = []
    for node in nodes:
        if node["id"] is not None:
            flat.append(node)
        flat.extend(flatten_nav_ids(node["children"]))
    return flat


def collect_md_stems(el: ET.Element) -> set:
    """Returns the bare .md stem (e.g. "native-objc-interop") of el's own
    topic="...md", if any, plus every descendant toc-element's, recursively -
    used by prune_blacklisted_elements to know every page a removed subtree
    was making available."""
    stems = set()
    topic = el.get("topic")
    if topic and topic.endswith(".md"):
        stems.add(Path(topic).stem)
    for child in el.findall("toc-element"):
        stems |= collect_md_stems(child)
    return stems


def parse_blacklist_path(raw: str) -> tuple:
    """Splits one --blacklisted-element-titles value into its ordered
    toc-title path components, e.g. "Interoperability\\/Swift/Objective-C
    and C interop" -> ("Interoperability", "Swift/Objective-C and C
    interop"). Levels are joined by the two-character sequence "\\/"
    (backslash then slash) rather than a bare "/", since a bare "/"
    routinely appears *within* a single real toc-title (as in that example
    itself) - splitting on the rarer, explicitly-marked separator means the
    overwhelmingly common case needs no escaping at all."""
    return tuple(raw.split("\\/"))


def prune_blacklisted_elements(root: ET.Element, blacklisted_paths: set) -> tuple:
    """Removes every <toc-element> whose *full* toc-title path from a
    top-level element down to itself (see parse_blacklist_path) is in
    blacklisted_paths - along with all of its descendants - wherever it
    appears in the tree, mutating root in place. A full path is required
    (rather than matching toc-title alone) because toc-title is not unique
    across kr.tree (e.g. plenty of "Overview"s share the string). A match is
    not searched for *within* an already-removed subtree, since it's gone
    either way.

    Returns (removed_stems, unmatched_paths): removed_stems is the set of
    bare .md stems (see collect_md_stems) that no longer have a home in the
    tree, across every matched subtree combined - callers use this to keep
    conversion and topic_index consistent with the now-pruned tree (nav
    itself just naturally no longer mentions them, since they're gone from
    the tree build_node walks). unmatched_paths is whatever blacklisted path
    never matched any toc-element, so callers can warn about likely typos
    (or a wrong ancestor chain) instead of silently doing nothing.
    """
    removed_stems = set()
    matched_paths = set()

    def walk(el: ET.Element, prefix: tuple) -> None:
        for child in list(el):
            if child.tag != "toc-element":
                continue
            path = prefix + (child.get("toc-title"),)
            if path in blacklisted_paths:
                matched_paths.add(path)
                removed_stems.update(collect_md_stems(child))
                el.remove(child)
            else:
                walk(child, path)

    walk(root, ())
    return removed_stems, blacklisted_paths - matched_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("docs_root", type=Path, help="Path to kotlin-web-site/docs")
    parser.add_argument("config", type=Path,
                         help='Path to a JSON config with "broken-ext-link-color" and "menu-no-link-color"')
    parser.add_argument("images_zip", type=Path,
                         help='Writerside\'s own image output zip (e.g. "webHelpImages.zip")')
    parser.add_argument("db_path", type=Path, nargs="?", default=Path("documentation.db"),
                         help="SQLite database to populate (default: documentation.db)")
    parser.add_argument("--tree-file", default="kr.tree", help="Filename of the Writerside tree file under docs_root")
    parser.add_argument("--topics-subdir", default="topics", help="Subdirectory of docs_root holding .md files")
    parser.add_argument(
        "--blacklisted-element-titles", dest="blacklisted_element_titles", nargs="*", default=[], metavar="TOC_PATH",
        help='full toc-title path(s) of <toc-element>s to omit entirely, from a top-level element down to the '
             'one being blacklisted, levels joined by "\\/" (e.g. "Interoperability\\/Swift/Objective-C and C '
             'interop" - note the plain "/" left as-is within that last title itself): the element and its whole '
             "subtree get no nav entry, none of their .md sub-topics get converted/inserted, and any other page's "
             "in-content link to one of those .md files renders as broken",
    )
    parser.add_argument(
        "--allow-conversion-failures", action="store_true",
        help="Insert whatever converted successfully even if some .md files failed, instead of the default of "
             "refusing to modify the database at all. Failed pages are dropped from nav and every link to them "
             "renders as broken either way",
    )
    args = parser.parse_args()

    docs_root: Path = args.docs_root
    topics_dir = docs_root / args.topics_subdir
    tree_path = docs_root / args.tree_file
    page_peb_path = Path(__file__).parent / "templates" / "page.peb"
    nav_peb_path = Path(__file__).parent / "templates" / "nav.peb"
    assets_dir = Path(__file__).parent / "assets"

    for required in (topics_dir, tree_path, page_peb_path, nav_peb_path, assets_dir, args.config, args.images_zip):
        if not required.exists():
            print(f"error: {required} does not exist", file=sys.stderr)
            sys.exit(1)
    if not args.db_path.is_file():
        print(f"error: {args.db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    pngquant_path = find_pngquant()

    print(f"Backing up {args.db_path}...", file=sys.stderr)
    backup_path = backup_database(args.db_path)
    print(f"Backup written to {backup_path}", file=sys.stderr)

    config = load_config(args.config)
    variables = load_variables(docs_root)
    topic_index = build_topic_index(topics_dir)  # stem -> nested id, e.g. "tour/kotlin-tour-hello-world"
    topic_index_db = flatten_to_db_ids(topic_index)  # stem -> "k/html/<stem>"

    # Parsed (and pruned) up front, before topic_index_db/Converter are set
    # up, so every later stage - conversion, link resolution, nav - agrees on
    # what no longer exists. Pruning mutates root in place; build_node below
    # reuses this same, already-pruned root rather than re-parsing tree_path.
    root = ET.parse(tree_path).getroot()
    blacklisted_paths = {parse_blacklist_path(raw) for raw in args.blacklisted_element_titles}
    blacklisted_stems, unmatched_paths = prune_blacklisted_elements(root, blacklisted_paths)
    for path in sorted(unmatched_paths):
        print(f"warning: --blacklisted-element-titles {' > '.join(path)!r} matched no <toc-element> in {tree_path}",
              file=sys.stderr)
    for stem in blacklisted_stems:
        topic_index_db.pop(stem, None)  # makes resolve_href treat any link to it as unresolved -> styled broken
    if blacklisted_stems:
        print(f"Blacklisted {len(blacklisted_paths) - len(unmatched_paths)} element(s), "
              f"excluding {len(blacklisted_stems)} topic(s) from nav/conversion: {sorted(blacklisted_stems)}",
              file=sys.stderr)

    image_names = load_zip_image_names(args.images_zip)
    # Bare filename -> bare filename: every image is stored at
    # "k/html/images/<basename>" regardless of where it sat inside the zip,
    # and Converter.resolve_image_src looks its references up by bare
    # filename too (src.rsplit("/")[-1]), so both sides agree even if a
    # future export grows subdirectories. Keeping the full entry name as the
    # index value instead would silently break every reference in a nested
    # zip - the lookup key would never match - and would disagree with
    # insert_optimized_media.py, which flattens to the basename as well.
    image_index_db = {}
    image_entry_by_name = {}
    for name in image_names:
        base = Path(name).name
        if base in image_entry_by_name:
            print(f"warning: {name!r} has the same filename as {image_entry_by_name[base]!r}; "
                  "keeping the first, skipping this one", file=sys.stderr)
            continue
        image_entry_by_name[base] = name
        image_index_db[base] = base
    md = make_markdown_it()
    converter = Converter(
        md, variables, topic_index_db, image_index_db,
        broken_ext_link_color=config.get("broken-ext-link-color"),
        image_url_prefix=IMAGES_URL_PREFIX,
    )

    # Every page is stored at "k/html/<stem>", so two same-stem .md files in
    # different topics/ subdirectories would produce two inserts at one
    # Content.path - a UNIQUE violation that aborts the whole transaction
    # partway through. build_topic_index already resolves that ambiguity
    # (keep the first sorted page id, warn about the rest), so defer to the
    # choice it already made and drop the losers here instead of crashing on
    # them: a file is kept only if topic_index maps its stem back to this
    # exact file. No second warning - build_topic_index printed one already.
    md_files = [
        p for p in sorted(topics_dir.rglob("*.md"))
        if p.stem not in blacklisted_stems
        and topic_index.get(p.stem) == p.relative_to(topics_dir).with_suffix("").as_posix()
    ]
    pages = []
    failed_stems = []
    for md_path in md_files:
        rel = md_path.relative_to(topics_dir)
        db_id = f"k/html/{md_path.stem}"
        source_rel = str(Path(args.topics_subdir) / rel)
        try:
            page = converter.convert_file(md_path, db_id, source_rel)
        except Exception as exc:  # noqa: BLE001 - surface which file broke, keep converting the rest
            print(f"error converting {md_path}: {exc}", file=sys.stderr)
            # No Content row will exist at this page's path, so stop
            # advertising it as a real page: without this, topic_index_db
            # still resolves the stem and nav renders an ordinary,
            # normally-styled link straight to a 404 (and any other page's
            # in-content link to it does the same). Dropping it here is what
            # the blacklist path already does above, and makes every
            # reference render as a styled broken link instead.
            topic_index_db.pop(md_path.stem, None)
            failed_stems.append(md_path.stem)
            continue
        pages.append(page)
    print(f"Converted {len(pages)}/{len(md_files)} pages", file=sys.stderr)
    # A failed conversion means a page that currently exists in the database
    # would be deleted (see the DELETE below) and not replaced. That's a
    # silent regression in a database this script's callers upload straight
    # to production, so refuse to write anything rather than shipping a
    # smaller corpus than the source tree describes. --allow-conversion-
    # failures opts back into best-effort behaviour, matching the
    # --allow-failures escape hatch find_missing_assets.py already has.
    if failed_stems and not args.allow_conversion_failures:
        print(
            f"error: {len(failed_stems)} page(s) failed to convert "
            f"({', '.join(sorted(failed_stems))}); refusing to modify {args.db_path}. "
            "Fix the source, or pass --allow-conversion-failures to insert the rest anyway.",
            file=sys.stderr,
        )
        sys.exit(1)

    # kr.tree's start-page is home.topic, not a .md file, so it never goes
    # through the conversion loop above - nav ends up linking to
    # k/html/home.html with no page actually there. Insert an empty
    # placeholder for it (no title, no blocks) so that link resolves to a
    # real page instead of 404ing; it's still rendered through page.peb, so
    # the sidebar nav appears on it exactly like any other page.
    pages.append({"id": HOME_PAGE_ID, "sourceFile": None, "title": None, "blocks": []})
    topic_index_db["home"] = HOME_PAGE_ID  # lets build_node resolve home.topic as a real link below

    id_to_title = {p["id"]: p["title"] for p in pages}
    nav_warnings = []
    nav_tree = [
        build_node(el, topic_index_db, id_to_title, nav_warnings, config.get("menu-no-link-color"),
                   id_prefix="k/html/")
        for el in root.findall("toc-element")
    ]
    nav_tree = [node for node in nav_tree if node is not None]
    for w in nav_warnings:
        print(f"warning: {w}", file=sys.stderr)

    flat_nav = flatten_nav_ids(nav_tree)
    id_to_index = {}
    for i, node in enumerate(flat_nav):
        id_to_index.setdefault(node["id"], i)
    for page in pages:
        idx = id_to_index.get(page["id"])
        if idx is None:
            continue
        if idx > 0:
            prev_node = flat_nav[idx - 1]
            page["prev"] = {"id": prev_node["id"], "title": prev_node["title"]}
        if idx < len(flat_nav) - 1:
            next_node = flat_nav[idx + 1]
            page["next"] = {"id": next_node["id"], "title": next_node["title"]}

    page_template_content = build_db_page_template(page_peb_path)
    nav_template_content = nav_peb_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(args.db_path)
    try:
        conn.execute("BEGIN")
        deleted = conn.execute("DELETE FROM Content WHERE path LIKE 'k/html/%' OR path LIKE 'assets/%'").rowcount
        print(f"Deleted {deleted} existing k/html/ and assets/ row(s)", file=sys.stderr)

        page_template_id = upsert_template(conn, "page.peb", page_template_content)
        nav_template_id = upsert_template(conn, "nav.peb", nav_template_content)

        page_content_type_id = get_id(conn, "ContentTypes", PAGE_CONTENT_TYPE)
        # WebServer.kt's fragment-continuation query hardcodes "AND
        # languageId = 1" regardless of the first row's own language, so
        # every fragment - and thus, to keep this simple, every row this
        # script writes at all - has to be languageID 1. Only fine because
        # this database has exactly one language ("en-US") today; would need
        # a real fix on the server side before a second language could work.
        language_id = get_id(conn, "Languages", LANGUAGE)

        chunked_log = []

        # Serialized once and reused both as this run's dictionary-training
        # samples (only spent if CompressionDictionary doesn't exist yet, see
        # load_or_create_dictionary) and as the actual bytes to compress
        # below, rather than re-running json.dumps for the same page twice.
        page_json_bytes = [
            json.dumps(page, separators=(",", ":"), ensure_ascii=False).encode("utf-8") for page in pages
        ]
        # The server (see layout.pebble) parses each Content row's JSON as an
        # object and hands its top-level fields to Pebble directly as the
        # model - a bare JSON array wouldn't parse that way at all, so the
        # tree goes under a "tree" key here, matching nav.peb's top-level
        # "{% for node in tree %}".
        nav_json_bytes = json.dumps({"tree": nav_tree}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        dictionary_data = load_or_create_dictionary(conn, page_json_bytes + [nav_json_bytes])
        with DictionaryCompressor(dictionary_data) as compressor:
            for page, json_bytes in zip(pages, page_json_bytes):
                path = f"{page['id']}.html"
                blob = compressor.compress(json_bytes)
                insert_chunked_content(conn, path, language_id, page_content_type_id, page_template_id, blob,
                                        chunked_log)

            nav_blob = compressor.compress(nav_json_bytes)
            insert_chunked_content(conn, NAV_CONTENT_PATH, language_id, page_content_type_id, nav_template_id,
                                    nav_blob, chunked_log)

            content_type_cache = {}
            images_inserted = 0
            with zipfile.ZipFile(args.images_zip) as zf:
                # Keyed by bare filename, matching image_index_db above (and
                # so the "/k/html/images/<basename>" references baked into
                # every page at conversion time) rather than the zip's own
                # entry name, which is the same string for a flat zip and the
                # right one for a nested one.
                for base, entry in sorted(image_entry_by_name.items()):
                    db_path = f"{IMAGES_DB_PATH_PREFIX}{base}"
                    if insert_file(conn, zf.read(entry), base, db_path, language_id, content_type_cache, chunked_log,
                                    pngquant_path, compressor):
                        images_inserted += 1

            assets_inserted = 0
            for asset_path in sorted(assets_dir.iterdir()):
                if not asset_path.is_file():
                    continue
                db_path = f"assets/{asset_path.name}"
                if insert_file(conn, asset_path.read_bytes(), asset_path.name, db_path, language_id,
                                content_type_cache, chunked_log, pngquant_path, compressor):
                    assets_inserted += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # SQLite never shrinks a database file just because rows were deleted or
    # replaced with something smaller (auto_vacuum is off here, and even
    # with it on, freed pages are only reused for future writes, not
    # returned to the OS) - the freed space just becomes an internal
    # freelist. VACUUM is the only thing that actually rebuilds the file at
    # its true minimal size, and it can't run inside the transaction above
    # (SQLite refuses VACUUM while one is active), so it's a separate step
    # on its own connection afterwards.
    print("Vacuuming database to reclaim freed space...", file=sys.stderr)
    vacuum_conn = sqlite3.connect(args.db_path)
    try:
        vacuum_conn.execute("VACUUM")
    finally:
        vacuum_conn.close()

    print(
        f"Inserted {len(pages)} page(s) + 1 navigation row + {images_inserted} image(s) + "
        f"{assets_inserted} asset file(s) into {args.db_path} "
        f"(page.peb template id={page_template_id}, nav.peb template id={nav_template_id})"
    )
    if chunked_log:
        print(f"Chunked {len(chunked_log)} file(s) over {CHUNK_SIZE:,} bytes:")
        for path, total_size, chunk_count in chunked_log:
            print(f"  {path}: {total_size:,} bytes -> {chunk_count} chunks")
    else:
        print(f"No files exceeded {CHUNK_SIZE:,} bytes; nothing needed chunking.")


if __name__ == "__main__":
    main()
