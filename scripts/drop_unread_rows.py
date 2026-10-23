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
drop_unread_rows.py

Deletes Content rows that nothing in the app or the documentation reads, after
checking that no row it keeps refers to one of them.

The rows, in seven groups (pick some with --only; the default is all seven):

- javadoc-use: the javadoc Use pages (j/html/api/**/class-use/*.html and
  *package-use.html) and the module graphs (j/html/api/**/*module-graph.svg).
  Only other Use pages link to a Use page, and no page names a module graph.
  4,896 + 60 rows.
- pdfjs-cmaps: pdf.js's predefined CMaps (p/web/cmaps/, 168 .bcmap files and
  their LICENSE). pdf.js fetches one by name only for a font that needs it; see
  "PDF check" below.
- pdfjs-extras: pdf.js's no-WebAssembly fallbacks (openjpeg_nowasm_fallback.js,
  jbig2_nowasm_fallback.js), which its worker imports only when WebAssembly
  cannot start, and its scripting sandbox (pdf.sandbox.mjs, quickjs-eval.js,
  quickjs-eval.wasm), which the viewer loads only with enableScripting on. The
  step also sets enableScripting's default to false in p/web/viewer.mjs, so the
  viewer never asks for the sandbox: with it off, the viewer passes no
  scripting manager to its PDFViewer, which is the only caller of the one that
  imports pdf.sandbox.mjs. The generic viewer never writes the pdfjs.preferences
  entry that could turn it back on.
- x-placeholders: the x/ pages, each "Placeholder for XML layout attribute
  ...". XML tooltips link to a/ pages instead. 1,238 rows. x.html and x.pdf
  are not in this group.
- javadoc-index-all: javadoc's one-page index (j/**/index-all.html), which
  repeats its split index (index-files/index-1.html to index-27.html) in one
  page of 23 MB. It is the only group that changes kept rows: every quoted
  href or src that resolves to it, its file name matched in any letter case,
  is pointed at index-files/index-1.html instead, keeping the link's own
  relative or rooted prefix (a fragment goes to the split page that holds its
  anchor, index-1.html first when several do). On a Pixel 6 Pro, in one
  timepage.mjs run per page with the cache disabled, the one-page index took
  5,033 ms to its load event, and the split pages index-1.html and
  index-19.html 537 ms and 832 ms. A page named
  index-all.html must be a javadoc index page (body class "index-page"), and
  the split index must exist, or the run refuses.
- duplicate-media: byte-identical copies of an image, video or audio row
  (same stored bytes, chunks joined, same type and language). One copy of each
  set is kept: the one the most rows link to (pages, templates, tooltip
  buttons and Bookshelf entries counted), then the shortest path, then the
  lowest id. Every link to a dropped copy is pointed at the kept one, keeping
  the link's own directory prefix: in kept text rows, templated rows' JSON
  included, and in tooltip button URIs (at schema 3, a button whose new URI
  TooltipButtonUris already holds is pointed at that row, so each URI stays
  stored once). A link in a template is not rewritten, so it refuses the run,
  as does any link the rewrite does not reach. Most of
  these are the Help+Manual image toggles whose small and zoomed images became
  identical when the images were resized (ADFA-6645, item 2), so a toggle now
  swaps one file for itself. PDFs are left alone: the Bookshelf lists each
  placeholder book on its own even when their bytes match, and deleting one
  fires the DeleteBook trigger. So are text rows: the only identical ones are
  licence notices, kept beside what they cover. So are SVG images: an SVG is a
  document whose relative references resolve against its own URL, so a copy
  kept in another directory could load other files in its place.
- unlinked: 31 rows no page, template, script, stylesheet, tooltip, Bookshelf
  entry or app URL reaches, each named in UNLINKED with why it is unread. The
  list is closed, so a row is never deleted because a scan missed its link,
  and the reference check still refuses the run if anything links to one.
  Rows that a stricter check could not prove unread are left out on purpose:
  the Kotlin topic pages in the sidebar tree (nav.peb and page.peb build their
  links from page ids, with no ".html" in the data), the Help+Manual scripts
  and images that hm_webhelp.js and its kin load by name, k/navigation.html,
  the playground pages of WebServer's disabled playground endpoint, the PUCC
  example data, x.html, x.pdf, the t/ placeholder book, the licence notices
  and the index pages.

A row's chunk continuations ("<path>-1", "-2", ...) go with it.

Measured on the 2026-10-18 database after ADFA-6627, each group alone, packed
with rebuild_database.py --pack Content,Tooltips --page-size 4096, as bytes
saved in the file and in its brotli -q 11 -w 24: javadoc-use 5,042,176 and
4,330,908; pdfjs-cmaps 860,160 and 851,736; pdfjs-extras 339,968 and 346,809;
x-placeholders 122,880 and 103,920. Repacking alone moves the .br by up to
about 0.2 MB, so the last figure is within that. javadoc-index-all, measured
on the 2026-10-19 ship-time build (every ADFA-3305 step, 90,783,744 bytes),
against the same build repacked, both with --pack Content,TooltipTexts:
1,241,088 and 1,250,482, with 5,224 links in 5,222 javadoc pages pointed at
the split index. duplicate-media and unlinked, measured the same way on the
2026-10-19 item-B build (85,708,800 bytes): 892,928 and 165,045 (53 copies,
98 links in 13 rows pointed at the kept ones), and 94,208 and 86,465 (31
rows); both together 983,040 and 259,386. The download compressor already
finds most of a copy's bytes, which is why its file saving is the larger.

Before deleting anything the step checks, and refuses with the list and exit 1
when any check fails, writing nothing:

- References. Every kept text row (HTML, CSS, JS, SVG, JSON, XML, Markdown and
  plain text, decoded, chunks joined), every Templates row, every tooltip
  button's uri, every tooltip's text and the Bookshelf entries are read for
  links. The tooltip tables are read in either documentation schema: 2
  (TooltipButtons.uri, Tooltips.summary and detail) or 3, after
  migrate_tooltip_schema.py (TooltipButtonUris, TooltipTexts); tables in
  neither shape refuse the run before anything is read. From documentation
  major version 3 (the newest DocumentationDatabaseVersion row, ordered as the
  app orders them) every template is stored Brotli-compressed against the
  dictionary, so templates are decoded first there and read as plain text
  below it. Links are: HTML and
  SVG attributes, srcset, CSS url() and @import, quoted file names in scripts,
  the string values of a templated row's JSON, viewer.html?file= parameters and
  http://localhost:6174/ URLs. A link that resolves (relative to its row, or
  from the root) to a deleted row is refused. So is a quoted file name that
  resolves to no row at all but is the name of a deleted row and of no kept
  one, which is how a script that builds its URLs names a file. Three such
  references are expected and allowed; ALLOWED below says why each is dead.
  Rows are read after javadoc-index-all's rewrite, so a link to a one-page
  index that the rewrite does not reach (unquoted, built by a script, or in a
  templated row's JSON, which is not rewritten) refuses the run too. They are
  also read after duplicate-media's rewrite, and tooltip button uris as they
  will be written, so a link to a dropped copy that it does not reach refuses
  the run the same way; so does a templated row whose JSON no longer parses
  after it.
- Content. Each x/ row must be a placeholder page, each javadoc-use HTML row a
  "Uses of" page and each module graph an SVG, and p/web/cmaps/ may hold only
  .bcmap files and LICENSE, so a real page that lands on one of these paths is
  never deleted.
- PDF check (pdfjs-cmaps). pdf.js 6.1.200 fetches a CMap file for: a font whose
  /Encoding names a predefined CMap other than Identity-H or Identity-V; an
  embedded CMap that uses one (usecmap); a CID font of the Adobe GB1, CNS1,
  Japan1 or Korea1 collection (Adobe-<ordering>-UCS2, unless the font has its
  own ToUnicode map); and a font with one of ten Chinese names (GBK-EUC-H),
  read from both /BaseFont and the FontDescriptor's /FontName, which pdf.js
  prefers when the two differ, as a name or a string.
  Every stored PDF is read and each stream decoded through its filters
  (FlateDecode, ASCII85Decode, ASCIIHexDecode and RunLengthDecode, in any
  chain), skipping image and cross-reference streams, which cannot hold a font;
  any sign of these refuses the run. So does an encrypted PDF, or a stream it
  cannot decode (another filter, a predictor, corrupt data), since its fonts
  cannot be read. The collection check ignores ToUnicode, so it refuses more
  than pdf.js needs, never less. It cannot see PDFs that a plugin installs on
  the phone.
- The viewer edit (pdfjs-extras). p/web/viewer.mjs must hold exactly one
  enableScripting default; if it holds none or more, nothing is written.
- The index links (javadoc-index-all). A link whose fragment names an anchor
  no split page holds refuses, as does a split page that cannot be read.
- A kept text row or template that cannot be decoded cannot be checked, so it
  refuses too.

Runs in one transaction (rolled back on any error), backs the database up
first, and VACUUMs afterwards. --dry-run does all the checks and reports what
would change without writing. A second run finds nothing to do and exits 0.

Pipeline position: after every step that writes page content, links or
tooltips, so the reference check sees the final pages, and before
rebuild_database.py. It may run before or after migrate_tooltip_schema.py,
since it reads both tooltip shapes and both template encodings. Deleting
rows does not call for a dictionary re-mint. rebuild_database.py --pack has to
follow it: rows whose links it rewrites (javadoc-index-all, duplicate-media)
change size, and javadoc-index-all's store a few bytes more each, and
VACUUM keeps row order, so in a file that was already packed they move to new
pages and leave their old ones mostly empty. On the 2026-10-19 ship-time build
(packed, 90,783,744 bytes) the file grew to 99,475,456 bytes; packed again it
is 89,542,656.

    uv run scripts/drop_unread_rows.py documentation.db --dry-run
    uv run scripts/drop_unread_rows.py documentation.db [--only GROUP ...] [--workers N] [--verbose]

Recovering: the timestamped backup taken before the first write is the whole
undo.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import hashlib
import html
import json
import posixpath
import re
import sqlite3
import sys
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, unquote

from optimize_db_media import (
    BrotliCodec, backup_database, delete_media_row, human, is_continuation_path, load_dictionary,
    owned_fragment_paths, reassemble,
)
from update_media_references import replace_row

# Content types read for references. Everything else is media.
SCANNED_TYPES = ("text/html", "text/css", "text/javascript", "image/svg+xml", "application/json",
                 "application/xml", "text/markdown", "text/plain", "text", "text/text",
                 "application/x-typescript")

VIEWER = "p/web/viewer.mjs"
# From this documentation major version on, migrate_tooltip_schema.py has stored every template compressed.
COMPRESSED_TEMPLATES_MAJOR = 3
PDFJS_EXTRAS = ("p/web/wasm/openjpeg_nowasm_fallback.js", "p/web/wasm/jbig2_nowasm_fallback.js",
                "p/web/wasm/quickjs-eval.wasm", "p/web/wasm/quickjs-eval.js", "p/build/pdf.sandbox.mjs")
PLACEHOLDER = b"Placeholder for XML layout attribute"
# javadoc's one-page index, and the split index (index-files/index-N.html) its links are pointed at instead.
INDEX_ALL = "index-all.html"
SPLIT_INDEX_DIR = "index-files"
_SPLIT_ANCHOR = re.compile(rb'\bid="([^"]+)"')
_INDEX_LINK = re.compile(rb"""((?<![\w:-])(?:href|src)\s*=\s*)(["'])([^"'<>]*?)index-all\.html(#[^"'<>]*)?\2""",
                         re.I)
# The cheap test for a row _INDEX_LINK might change, in the same letter case rule, so a row's links are rewritten
# whether or not it also names the index in lower case.
_INDEX_ALL_NAME = re.compile(re.escape(INDEX_ALL.encode()), re.I)
# Rows nothing reads, each with why, for the unlinked group. Checked on the 2026-10-19 item-B ship-time build: no
# page, template, script, stylesheet, tooltip or Bookshelf entry names one - by file name, by path without the
# extension (nav.peb and page.peb build links from those), or by file name without the extension (Help+Manual's
# scripts load files by name) - and none is among the app's own documentation URLs. The reference check still
# refuses the run if anything links to one.
_UNLINKED = {
    "a Help+Manual embedded-popup script no page, template or script loads, or a file only those scripts name": (
        "i/js/hmEmbeddedPopups.js", "i/js/hmEmbedHelp.js", "i/_hmXpopup.htm", "i/_hmXtopic.htm",
        "i/css/hm_popup.css"),
    "a Help+Manual skin file nothing names (GoogleFonts.css is the 7-byte stub left by ADFA-4480)": (
        "i/css/GoogleFonts.css", "i/images/company-logo.webp", "i/images/page-bg.webp", "i/images/social_mail.webp"),
    "a javadoc doc-files image no page shows (GroupLayout.html shows groupLayout.1-3 and .example)": (
        "j/html/api/java.desktop/javax/swing/doc-files/groupLayout.sequential.webp",
        "j/html/api/java.desktop/javax/swing/doc-files/groupLayout.sequential.horizontal.webp",
        "j/html/api/java.desktop/javax/swing/doc-files/groupLayout.sequential.vertical.webp",
        "j/html/api/java.desktop/javax/swing/text/doc-files/OpenBookIcon.webp"),
    "a javadoc file no page links or loads (the pages load jQuery 3.7.1, not 3.6.1)": (
        "j/html/api/overview-summary.html", "j/html/api/script-dir/jquery-3.6.1.min.js",
        "j/html/resources/jdk-default.css", "j/html/specs/jdwp/jdwp-protocol.html", "j/java_keywords.html"),
    "a Dokka disambiguation page neither the navigation tree nor any page links": (
        "k/disambiguation/kotlin.concurrent.AtomicArray.html", "k/disambiguation/kotlin.concurrent.AtomicIntArray.html",
        "k/disambiguation/kotlin.concurrent.AtomicLongArray.html",
        "k/disambiguation/kotlin.concurrent.atomics.AtomicArray.html",
        "k/disambiguation/kotlin.concurrent.atomics.AtomicLongArray.html",
        "k/disambiguation/kotlin.coroutines.cancellation.CancellationException.html"),
    "a Kotlin topic page outside the sidebar tree (k/html/_nav.html) that no page links": (
        "k/html/jvm-spring-boot-collections.html", "k/html/ksp-command-line.html"),
    "a Dokka package list, which Dokka reads when another build links to these docs and no browser reads": (
        "k/package-list", "k/kotlin-reflect/package-list", "k/kotlin-stdlib/package-list", "k/kotlin-test/package-list"),
    "Dokka's version-switcher fallback page, which nothing links or loads": ("k/not-found-version.html",),
}
UNLINKED = {path: why for why, paths in _UNLINKED.items() for path in paths}
# The types whose byte-identical copies duplicate-media merges; the docstring says why PDFs, text and SVG are not.
DUPLICATE_MEDIA_PREFIXES = ("image/", "video/", "audio/")
DUPLICATE_MEDIA_EXCLUDED = ("image/svg+xml",)
# A character a link's path can hold as written in a page; anything else (a quote, "(", "=", a space, a JSON
# escape's backslash) ends the path.
_PATH_CHARS = rb"A-Za-z0-9._~%:@+\-"

# References to rows this step deletes that nothing follows, as (referring row,
# deleted row) -> why. Each is printed when found, so it stays visible.
ALLOWED = {
    ("p/build/pdf.worker.mjs", "p/web/wasm/openjpeg_nowasm_fallback.js"):
        "the worker imports it only when WebAssembly cannot start",
    ("p/build/pdf.worker.mjs", "p/web/wasm/jbig2_nowasm_fallback.js"):
        "the worker imports it only when WebAssembly cannot start",
    ("p/web/viewer.mjs", "p/build/pdf.sandbox.mjs"):
        "the viewer imports it only with enableScripting on, and this step turns that off",
}

# The enableScripting entry of the viewer's default options, minified or not.
_SCRIPTING_DEFAULT = re.compile(rb"(\benableScripting\s*:\s*\{\s*value\s*:\s*)(true|false|!0|!1)\b")

_ATTR = re.compile(
    r"""(?<![\w:-])(?:href|src|poster|data-src|data-href|action|formaction|background|data|content|cite|"""
    r"""xlink:href)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""", re.I)
_SRCSET = re.compile(r"""(?<![\w-])srcset\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_CSS_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)"'\s]*))\s*\)""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s+(?:"([^"]*)"|'([^']*)')""", re.I)
_QUOTED = re.compile(
    r"""["'`]([^"'`\s<>]{1,400}?\.(?:html?|m?js|css|png|gif|webp|svg|webm|mp4|json|pdf|wasm|bcmap|pfb|ttf|"""
    r"""otf|woff2?|icc|ico|md|xml|txt|jpe?g|ogv|mov|peb))(?:[?#][^"'`\s]*)?["'`]""", re.I)
_META_REFRESH = re.compile(r"\s*\d+\s*;\s*url\s*=\s*", re.I)
_FILE_PARAM = re.compile(r"[?&]file=([^&#]+)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
LOCAL_ORIGINS = ("http://localhost:6174/", "http://127.0.0.1:6174/")

# What pdf.js 6.1.200 reads to decide it needs a CMap file; see the docstring.
_PDF_NAME = rb"/((?:[^\s/<>\[\]()%{}#]|#[0-9A-Fa-f]{2})+)"
_PDF_ENCODING = re.compile(rb"/(?:Encoding|UseCMap)\s*" + _PDF_NAME)
_PDF_USECMAP = re.compile(_PDF_NAME + rb"\s+usecmap\b")
_PDF_ORDERING = re.compile(rb"/Ordering\s*(?:\(([^)]*)\)|<([0-9A-Fa-f\s]*)>)")
_PDF_FONT_NAME_KEY = re.compile(rb"/(?:BaseFont|FontName)\s*")
_PDF_NAME_AT = re.compile(_PDF_NAME)
_PDF_HEX_AT = re.compile(rb"<([0-9A-Fa-f\s]*)>")
_PDF_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}
# A stream starts after its dictionary's ">>"; matching that keeps "endstream" from reading as a start.
_PDF_STREAM = re.compile(rb">>\s*stream(?:\r\n|\n|\r)")
_PDF_OBJ = re.compile(rb"\d+\s+\d+\s+obj\b")
_PDF_LENGTH = re.compile(rb"/Length\s+(\d+)\b(?!\s+\d+\s+R)")
_PDF_FILTER = re.compile(rb"/Filter\s*(\[[^\]]*\]|" + _PDF_NAME + rb")")
_PDF_PREDICTOR = re.compile(rb"/Predictor\s+(\d+)")
_PDF_IMAGE = re.compile(rb"/Subtype\s*/Image\b")
_PDF_XREF = re.compile(rb"/Type\s*/XRef\b")
_PDF_ENCRYPT = re.compile(rb"/Encrypt\s*(?:\d+\s+\d+\s+R|<<)")
# Filters that only ever hold image data, so their streams cannot hold a font.
IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"}
CJK_ORDERINGS = (b"GB1", b"CNS1", b"Japan1", b"Korea1")
# pdf.js's chineseFontNames, as the bytes it compares a font name with.
CHINESE_FONT_NAMES = {name.encode("latin-1") for name in (
    "\u00cb\u00ce\u00cc\u00e5", "\u00ba\u00da\u00cc\u00e5", "\u00bf\u00ac\u00cc\u00e5", "\u00b7\u00c2\u00cb\u00ce",
    "\u00bf\u00ac\u00cc\u00e5_GB2312", "\u00b7\u00c2\u00cb\u00ce_GB2312", "\u00c1\u00a5\u00ca\u00e9",
    "\u00d0\u00c2\u00cb\u00ce", "\u00b7\u00c2\u00cb\u00ce\u00cc\u00e5", "\u00d0\u00a1\u00b1\u00ea\u00cb\u00ce")}
IDENTITY_CMAPS = {"Identity-H", "Identity-V"}


def _javadoc_use(path: str) -> bool:
    if not path.startswith("j/html/api/"):
        return False
    directory, name = posixpath.split(path)
    return ((directory.endswith("/class-use") and name.endswith(".html")) or name.endswith("package-use.html")
            or name.endswith("module-graph.svg"))


def _check_javadoc_use(path: str, raw: bytes):
    if path.endswith(".svg"):
        return None if b"<svg" in raw else "is not an SVG"
    return None if b"<title>Uses of " in raw else "is not a javadoc Use page"


def _check_cmap(path: str, raw: bytes):
    name = posixpath.basename(path)
    return None if name.endswith(".bcmap") or name == "LICENSE" else "is not a CMap or its LICENSE"


def _check_placeholder(path: str, raw: bytes):
    return None if PLACEHOLDER in raw else "is not a placeholder page"


def _javadoc_index_all(path: str) -> bool:
    return path.startswith("j/") and posixpath.basename(path) == INDEX_ALL


def _check_index_page(path: str, raw: bytes):
    return None if b'class="index-page"' in raw else "is not a javadoc index page"


# name -> (what it holds, which rows it selects, a check on each row's decoded bytes)
GROUPS = {
    "javadoc-use": ("javadoc Use pages and module graphs", _javadoc_use, _check_javadoc_use),
    "pdfjs-cmaps": ("pdf.js CMaps", lambda path: path.startswith("p/web/cmaps/"), _check_cmap),
    "pdfjs-extras": ("pdf.js no-WebAssembly fallbacks and scripting sandbox", lambda path: path in PDFJS_EXTRAS,
                     None),
    "x-placeholders": ("x/ placeholder pages", lambda path: path.startswith("x/"), _check_placeholder),
    "javadoc-index-all": ("javadoc one-page indexes", _javadoc_index_all, _check_index_page),
    # Chosen from the data, not by path: see run().
    "duplicate-media": ("byte-identical media copies", None, None),
    "unlinked": ("rows nothing links to", lambda path: path in UNLINKED, None),
}


def point_at_split_index(raw: bytes, row_dir: str, split_index: dict) -> tuple:
    """(raw with every quoted href or src that resolves to a one-page index in split_index pointed at that index's
    split pages, the number of links changed, problems). split_index maps each index-all.html path to
    {anchor id: split page path}, its first page under the key None. The link keeps its own prefix, so it stays
    relative or rooted as it was; a fragment goes to the split page that holds its anchor."""
    changed, problems = 0, []

    def repl(m):
        nonlocal changed
        prefix, fragment = m.group(3).decode("utf-8", errors="replace"), m.group(4) or b""
        target = next((p for p in link_paths(prefix + INDEX_ALL, (row_dir,)) if p in split_index), None)
        if target is None:
            return m.group(0)
        anchor = unquote(fragment[1:].decode("utf-8", errors="replace")) if fragment else None
        page = split_index[target].get(anchor)
        if page is None:
            problems.append(f"links to {target}{fragment.decode('utf-8', errors='replace')}, an anchor no split "
                            "index page holds")
            return m.group(0)
        changed += 1
        new = prefix + posixpath.relpath(page, posixpath.dirname(target))
        return m.group(1) + m.group(2) + new.encode("utf-8") + fragment + m.group(2)

    return _INDEX_LINK.sub(repl, raw), changed, problems


def dropped_copy_links(remap: dict):
    """A pattern for the links point_at_kept_copies may change: a path whose last segment is a dropped copy's file
    name, as stored or percent-encoded, with its directory prefix in group 1 and the name in group 2."""
    names = set()
    for dropped in remap:
        name = posixpath.basename(dropped)
        names.update({name, quote(name)})
    alternatives = b"|".join(re.escape(name.encode("utf-8")) for name in sorted(names, key=len, reverse=True))
    return re.compile(rb"(?<![" + _PATH_CHARS + rb"/])((?:[" + _PATH_CHARS + rb"]*/)*)(" + alternatives
                      + rb")(?![" + _PATH_CHARS + rb"/])")


def point_at_kept_copies(raw: bytes, row_dir: str, remap: dict, pattern) -> tuple:
    """(raw with every link that resolves to a dropped copy pointed at the copy kept in its place, the number of links
    changed). remap maps each dropped path to the path kept; pattern is dropped_copy_links(remap). A link keeps its
    own directory prefix, and stays percent-encoded if it was, so a relative, rooted or http://localhost:6174/ link
    stays one. JSON text is rewritten in place, inside its strings."""
    changed = 0

    def repl(m):
        nonlocal changed
        prefix, name = m.group(1).decode("utf-8", errors="replace"), m.group(2).decode("utf-8", errors="replace")
        target = next((p for p in link_paths(prefix + name, (row_dir,)) if p in remap), None)
        if target is None:
            return m.group(0)
        new = posixpath.relpath(remap[target], posixpath.dirname(target))
        if name != posixpath.basename(target):  # the link spelled the name percent-encoded
            new = quote(new)
        changed += 1
        return m.group(1) + new.encode("utf-8")

    return pattern.sub(repl, raw), changed


def turn_scripting_off(viewer: bytes) -> tuple:
    """(viewer with enableScripting's default false, whether that changed it), or (None, why not)."""
    found = list(_SCRIPTING_DEFAULT.finditer(viewer))
    if len(found) != 1:
        return None, f"holds {len(found)} enableScripting defaults, not 1"
    m = found[0]
    if m.group(2) in (b"false", b"!1"):
        return viewer, False
    return viewer[:m.start(2)] + b"false" + viewer[m.end(2):], True


def link_candidates(text: str, is_json: bool) -> set:
    """Every string in text that could be a link to a stored file."""
    found = set()
    blobs = [text]
    if is_json:
        try:
            strings = []
            _json_strings(json.loads(text), strings)
        except ValueError:
            pass  # not JSON after all; read it as text
        else:
            blobs = strings
            found.update(s for s in strings if len(s) < 400 and "<" not in s)
    for blob in blobs:
        for m in _ATTR.finditer(blob):
            value = next(g for g in m.groups() if g is not None)
            if "&" in value:
                value = html.unescape(value)
            refresh = _META_REFRESH.match(value)
            found.add(value[refresh.end():] if refresh else value)
        for m in _SRCSET.finditer(blob):
            for part in (m.group(1) or m.group(2) or "").split(","):
                if part.strip():
                    found.add(html.unescape(part.split()[0]))
        for rx in (_CSS_URL, _CSS_IMPORT):
            for m in rx.finditer(blob):
                found.add(next((g for g in m.groups() if g is not None), ""))
        for m in _QUOTED.finditer(blob):
            found.add(m.group(1))
    found.discard("")
    return found


def _json_strings(value, out: list) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _json_strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _json_strings(item, out)


def is_external(ref: str) -> bool:
    """Whether ref points off this server: another scheme, or a host of its own."""
    ref = ref.strip()
    if ref.lower().startswith(LOCAL_ORIGINS):
        return False
    return ref.startswith("//") or bool(_SCHEME.match(ref))


def link_paths(ref: str, base_dirs, nested: bool = False) -> list:
    """The stored paths ref could name, read from each of base_dirs (from the root if it starts with "/")."""
    ref = ref.strip()
    out = []
    param = _FILE_PARAM.search(ref)
    if param and not nested:
        out.extend(link_paths(unquote(param.group(1)), base_dirs, nested=True))
    lowered = ref.lower()
    for origin in LOCAL_ORIGINS:
        if lowered.startswith(origin):
            ref = "/" + ref[len(origin):]
            break
    if not ref or ref.startswith(("#", "//")) or _SCHEME.match(ref):
        return out
    ref = ref.split("#", 1)[0].split("?", 1)[0]
    for candidate in {ref, unquote(ref)}:
        if not candidate:
            continue
        rooted = candidate.startswith("/")
        for base in ("",) if rooted else base_dirs:
            path = posixpath.normpath(candidate.lstrip("/") if rooted else posixpath.join(base, candidate))
            while path == ".." or path.startswith("../"):  # ".." stops at the root, as in a browser
                path = path[3:]
            if path not in ("", "."):
                out.append(path)
    return out


class References:
    """Finds the references a text makes to rows being deleted."""

    def __init__(self, stored: set, deleted: set):
        self.stored = stored
        self.deleted = deleted
        kept_names = {posixpath.basename(p) for p in stored - deleted}
        self.only_deleted = defaultdict(list)
        for path in deleted:
            name = posixpath.basename(path)
            if name not in kept_names:
                self.only_deleted[name].append(path)

    def targets(self, ref: str, base_dirs) -> list:
        """[(deleted path, how)] for one reference."""
        out, resolved = [], False
        for path in link_paths(ref, base_dirs):
            for variant in (path, path + ".html", path + "/index.html"):
                if variant in self.stored:
                    resolved = True
                    if variant in self.deleted:
                        out.append((variant, "links to"))
                    break
        if not resolved and not is_external(ref):
            name = unquote(posixpath.basename(ref.strip().split("#", 1)[0].split("?", 1)[0]))
            out.extend((target, "names") for target in self.only_deleted.get(name, ()))
        return out

    def in_text(self, text: str, base_dirs, is_json: bool = False) -> set:
        found = set()
        for ref in link_candidates(text, is_json):
            found.update(self.targets(ref, base_dirs))
        return found


def _unescape_name(name: bytes) -> bytes:
    return re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), name)


def _literal_string(data: bytes, start: int):
    """The bytes of the PDF literal string whose "(" is at start (escapes decoded, nested parentheses kept),
    or None if it does not end."""
    out, depth, i = bytearray(), 0, start + 1
    while i < len(data):
        c = data[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= len(data):
                return None
            e = data[i]
            if e in _PDF_ESCAPES:
                out += _PDF_ESCAPES[e]
            elif 0x30 <= e <= 0x37:
                digits = re.match(rb"[0-7]{1,3}", data[i:i + 3]).group(0)
                out.append(int(digits, 8) & 0xFF)
                i += len(digits) - 1
            elif e == 0x0D:  # a line continuation, \r or \r\n
                if data[i + 1:i + 2] == b"\n":
                    i += 1
            elif e != 0x0A:
                out.append(e)
        elif c == 0x28:
            depth += 1
            out.append(c)
        elif c == 0x29:
            if depth == 0:
                return bytes(out)
            depth -= 1
            out.append(c)
        else:
            out.append(c)
        i += 1
    return None


def font_names(part: bytes):
    """Each /BaseFont and /FontName value in part, as the bytes pdf.js compares: a name (#xx decoded), a
    literal string or a hex string. pdf.js takes the FontDescriptor's FontName when it differs from the
    BaseFont, so both are read."""
    for m in _PDF_FONT_NAME_KEY.finditer(part):
        i = m.end()
        lead = part[i:i + 1]
        if lead == b"/":
            name = _PDF_NAME_AT.match(part, i)
            if name:
                yield _unescape_name(name.group(1))
        elif lead == b"(":
            text = _literal_string(part, i)
            if text is not None:
                yield text
        elif lead == b"<" and part[i + 1:i + 2] != b"<":
            hexed = _PDF_HEX_AT.match(part, i)
            if hexed:
                digits = re.sub(rb"\s", b"", hexed.group(1))
                yield bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())


def _run_length(data: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(data):
        n = data[i]
        if n == 128:
            break
        if n < 128:
            out += data[i + 1:i + 2 + n]
            i += 2 + n
        else:
            out += data[i + 1:i + 2] * (257 - n)
            i += 2
    return bytes(out)


def _ascii_hex(data: bytes) -> bytes:
    digits = re.sub(rb"\s", b"", data.split(b">", 1)[0])
    return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii"))


def _ascii85(data: bytes) -> bytes:
    text = re.sub(rb"\s", b"", data)
    text = text[2:] if text.startswith(b"<~") else text
    return base64.a85decode(text.split(b"~>", 1)[0])


DECODERS = {"FlateDecode": lambda data: zlib.decompressobj().decompress(data), "ASCIIHexDecode": _ascii_hex,
            "ASCII85Decode": _ascii85, "RunLengthDecode": _run_length}
DECODERS.update({"Fl": DECODERS["FlateDecode"], "AHx": _ascii_hex, "A85": _ascii85, "RL": _run_length})


def pdf_parts(data: bytes) -> tuple:
    """([the PDF's bytes, then each stream decoded], [why each stream that could not be decoded could not]).
    Image streams and cross-reference streams are skipped: neither can hold a font."""
    parts, undecodable = [data], []
    headers = [m.end() for m in _PDF_OBJ.finditer(data)]
    pos = 0
    while True:
        m = _PDF_STREAM.search(data, pos)
        if m is None:
            break
        start = m.end()
        header = bisect.bisect_right(headers, m.start()) - 1
        dictionary = data[headers[header] if header >= 0 else max(0, m.start() - 4096):m.start() + 2]
        length = _PDF_LENGTH.search(dictionary)
        end = start + int(length.group(1)) if length else -1
        if not (length and data[end:end + 20].lstrip().startswith(b"endstream")):
            end = data.find(b"endstream", start)
            if end < 0:
                break
        body, pos = data[start:end], end
        filter_value = _PDF_FILTER.search(dictionary)
        filters = ([_unescape_name(n).decode("latin-1") for n in re.findall(_PDF_NAME, filter_value.group(1))]
                   if filter_value else [])
        if _PDF_IMAGE.search(dictionary) or _PDF_XREF.search(dictionary) or IMAGE_FILTERS & set(filters):
            continue
        predictor = _PDF_PREDICTOR.search(dictionary)
        if predictor and int(predictor.group(1)) > 1:
            undecodable.append(f"predictor {int(predictor.group(1))}")
            continue
        try:
            for name in filters:
                if name not in DECODERS:
                    raise ValueError(name)
                body = DECODERS[name](body)
        except (ValueError, zlib.error) as exc:
            undecodable.append(f"{name}" + ("" if str(exc) == name else f": {exc}"))
            continue
        parts.append(body)
    return parts, undecodable


def cmap_needs(data: bytes, cmap_names: set) -> list:
    """Why pdf.js would fetch a CMap file for this PDF, if it would; see the docstring."""
    if _PDF_ENCRYPT.search(data):
        return ["is encrypted, so its fonts cannot be read"]
    parts, undecodable = pdf_parts(data)
    needs = {f"has a stream this step cannot decode ({why}), so its fonts cannot all be checked"
             for why in undecodable}
    for part in parts:
        for m in list(_PDF_ENCODING.finditer(part)) + list(_PDF_USECMAP.finditer(part)):
            name = _unescape_name(m.group(1)).decode("latin-1")
            if name in cmap_names:
                needs.add(f"uses the predefined CMap {name}")
        for m in _PDF_ORDERING.finditer(part):
            ordering = m.group(1) if m.group(1) is not None else bytes.fromhex(m.group(2).decode().replace(" ", ""))
            if ordering.strip() in CJK_ORDERINGS:
                needs.add(f"has a CID font of the Adobe {ordering.strip().decode()} collection")
        for name in font_names(part):
            if name in CHINESE_FONT_NAMES or name.split(b"+", 1)[-1] in CHINESE_FONT_NAMES:
                needs.add("has a font pdf.js reads as Chinese (GBK-EUC-H)")
    return sorted(needs)


def declared_major(conn) -> int | None:
    """The documentation major version the database declares - its newest DocumentationDatabaseVersion row, ordered
    as Code On The Go orders them - or None if it declares none."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'DocumentationDatabaseVersion'").fetchone():
        return None
    row = conn.execute("SELECT major FROM DocumentationDatabaseVersion "
                       "ORDER BY changeTime DESC, rowid DESC LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def tooltip_queries(conn):
    """(buttons query, texts query) for the database's tooltip tables, schema 2 or 3, or None for any other
    shape. The buttons query yields (tooltipId, uri), the texts query (id, summary, detail)."""
    def columns(table):
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    tooltips, buttons, texts, uris = (columns(t) for t in ("Tooltips", "TooltipButtons", "TooltipTexts",
                                                           "TooltipButtonUris"))
    if {"id", "summary", "detail"} <= tooltips and {"tooltipId", "uri"} <= buttons and not texts and not uris:
        return ("SELECT tooltipId, uri FROM TooltipButtons WHERE uri IS NOT NULL",
                "SELECT id, summary, detail FROM Tooltips")
    if ({"id", "categoryId", "tag"} <= tooltips and not {"summary", "detail"} & tooltips
            and {"tooltipId", "uriId"} <= buttons and {"id", "summary", "detail"} <= texts and {"id", "uri"} <= uris):
        return ("SELECT B.tooltipId, U.uri FROM TooltipButtons B JOIN TooltipButtonUris U ON U.id = B.uriId "
                "WHERE U.uri IS NOT NULL",
                "SELECT id, summary, detail FROM TooltipTexts")
    return None


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    groups = cfg["groups"] or list(GROUPS)

    conn = sqlite3.connect(db_path)
    codec = None
    try:
        queries = tooltip_queries(conn)
        if queries is None:
            print("error: the tooltip tables are in neither the schema-2 shape (Tooltips.summary and detail, "
                  "TooltipButtons.uri) nor the schema-3 shape (TooltipTexts, TooltipButtonUris), so their links "
                  "cannot be read; nothing was changed.", file=sys.stderr)
            return 1
        buttons_query, texts_query = queries
        codec = BrotliCodec(load_dictionary(conn))
        rows = {path: (row_id, length, mime, compression, template_id, language_id)
                for row_id, path, length, mime, compression, template_id, language_id in conn.execute(
                    "SELECT c.id, c.path, LENGTH(c.content), ct.value, ct.compression, c.templateId, c.languageID "
                    "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id ORDER BY c.path")}
        lengths = {path: row[1] for path, row in rows.items()}
        bases = [path for path in rows if not is_continuation_path(lengths, path)]

        def stored_bytes(path: str) -> bytes:
            """A row's stored bytes, chunks joined. Main thread only: it queries."""
            first = conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0]
            return reassemble(conn, path, first)

        doomed = {}  # base row -> its group
        for group in groups:
            if GROUPS[group][1] is not None:
                doomed.update((path, group) for path in bases if GROUPS[group][1](path))

        # A template renders at the paths of the rows that use it.
        dirs_by_template = defaultdict(set)
        for path, row in rows.items():
            if row[4]:
                dirs_by_template[row[4]].add(posixpath.dirname(path))
        major = declared_major(conn)
        compressed = major is not None and major >= COMPRESSED_TEMPLATES_MAJOR
        templates = []  # (name, render dirs, decoded text, or None and why it could not be decoded)
        for template_id, name, content in conn.execute("SELECT id, name, content FROM Templates"):
            dirs = ("",) + tuple(sorted(dirs_by_template[template_id]))
            try:
                raw = codec.decompress(bytes(content), "brotli") if compressed else bytes(content)
            except Exception as exc:  # noqa: BLE001 - reported, and the run refused
                templates.append((name, dirs, None, str(exc)))
                continue
            templates.append((name, dirs, raw.decode("utf-8", errors="replace"), None))

        def kept_copies() -> dict:
            """duplicate-media: {each dropped copy: the copy kept in its place}. Copies are rows of a media type other
            than SVG with the same type, language and stored bytes (chunks joined) that no other group deletes. The
            copy kept is the one the most rows link to (kept text rows, templates, tooltip buttons, Bookshelf
            entries), then the one with the shortest path, then the one with the lowest id."""
            by_shape = defaultdict(list)
            for path in bases:
                content_type = rows[path][2]
                if (path not in doomed and content_type.startswith(DUPLICATE_MEDIA_PREFIXES)
                        and content_type not in DUPLICATE_MEDIA_EXCLUDED):
                    by_shape[(rows[path][2], rows[path][5], lengths[path])].append(path)
            sets = defaultdict(list)
            for shape, paths in by_shape.items():
                if len(paths) > 1:
                    for path in paths:
                        sets[shape + (hashlib.sha256(stored_bytes(path)).digest(),)].append(path)
            copies = [paths for paths in sets.values() if len(paths) > 1]
            if not copies:
                return {}
            members = {path for paths in copies for path in paths}
            counter = References(set(rows), members)
            # A row can link to a copy only by naming it, so a row that does not is not read for links.
            named = dropped_copy_links({path: path for path in members})
            linked_from = defaultdict(set)

            def count(item):
                path, full = item
                try:
                    raw = codec.decompress(full, rows[path][3])
                except Exception:  # noqa: BLE001 - the reference pass reports it and refuses the run
                    return ()
                if not named.search(raw):
                    return ()
                text = raw.decode("utf-8", errors="replace")
                return [(target, path) for target, _how in
                        counter.in_text(text, (posixpath.dirname(path),), rows[path][4] != 0)]

            texts = [(path, stored_bytes(path)) for path in bases
                     if path not in doomed and path not in members and rows[path][2] in SCANNED_TYPES]
            with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
                for links in pool.map(count, texts):
                    for target, source in links:
                        linked_from[target].add(source)
            for name, dirs, text, _error in templates:
                if text is not None:
                    for target, _how in counter.in_text(text, dirs):
                        linked_from[target].add(f"template {name}")
            for tooltip_id, uri in conn.execute(buttons_query):
                for target, _how in counter.targets(uri, ("",)):
                    linked_from[target].add(f"tooltip {tooltip_id} button")
            by_id = {rows[path][0]: path for path in members}
            for (content_id,) in conn.execute("SELECT contentID FROM Bookshelf"):
                if content_id in by_id:
                    linked_from[by_id[content_id]].add(f"Bookshelf {content_id}")
            chosen = {}
            for paths in copies:
                keep = min(paths, key=lambda p: (-len(linked_from[p]), len(p), rows[p][0]))
                chosen.update((path, keep) for path in paths if path != keep)
            return chosen

        remap = kept_copies() if "duplicate-media" in groups else {}
        doomed.update((path, "duplicate-media") for path in remap)
        copy_links = dropped_copy_links(remap) if remap else None
        deleted = {}  # every row deleted, chunk rows included -> its group
        for path, group in doomed.items():
            deleted[path] = group
            deleted.update((fragment, group) for fragment in owned_fragment_paths(conn, path, lengths[path]))

        problems = []
        # Each one-page index being deleted -> {anchor id: split page}, the first split page under None.
        split_index = {}
        for path in sorted(p for p, group in doomed.items() if group == "javadoc-index-all"):
            directory = posixpath.join(posixpath.dirname(path), SPLIT_INDEX_DIR)
            first = posixpath.join(directory, "index-1.html")
            pages = [p for p in bases if posixpath.dirname(p) == directory and rows[p][2] == "text/html"]
            if first not in pages:
                problems.append(f"{path} has no split index ({first}) to point its links at")
                continue
            anchors = {None: first}
            for page in sorted(pages, key=lambda p: (p != first, p)):
                try:
                    raw = codec.decompress(stored_bytes(page), rows[page][3])
                except Exception as exc:  # noqa: BLE001 - reported, and the run refused
                    problems.append(f"{page} could not be read, so its anchors are unknown: {exc}")
                    continue
                for m in _SPLIT_ANCHOR.finditer(raw):
                    anchors.setdefault(m.group(1).decode("utf-8", errors="replace"), page)
            split_index[path] = anchors
        viewer_new, viewer_changed = None, False
        if "pdfjs-extras" in groups:
            if VIEWER in rows:
                viewer_new, viewer_changed = turn_scripting_off(codec.decompress(stored_bytes(VIEWER),
                                                                                 rows[VIEWER][3]))
                if viewer_new is None:
                    problems.append(f"{VIEWER} {viewer_changed}, so scripting cannot be turned off")
                    viewer_changed = False
            elif any(group == "pdfjs-extras" for group in doomed.values()):
                problems.append(f"{VIEWER} is not stored, so this step cannot tell that scripting is off")

        if not doomed and not viewer_changed and not problems:
            print("Nothing to do: no row in " + ", ".join(groups)
                  + (", and scripting is already off." if "pdfjs-extras" in groups else "."))
            return 0

        def decode(item):
            path, full = item
            try:
                if path == VIEWER and viewer_new is not None:
                    return path, viewer_new, None
                return path, codec.decompress(full, rows[path][3]), None
            except Exception as exc:  # noqa: BLE001 - reported, and the run refused
                return path, None, str(exc)

        print(f"Checking the {len(doomed)} row(s) to delete...")
        checked = [(path, stored_bytes(path)) for path in sorted(doomed) if GROUPS[doomed[path]][2] is not None]
        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            for path, raw, error in pool.map(decode, checked):
                problem = f"could not be read: {error}" if raw is None else GROUPS[doomed[path]][2](path, raw)
                if problem:
                    problems.append(f"{path} {problem}")
        checked = None

        refs = References(set(rows), set(deleted))
        found = set()  # (referring row, deleted row, how)
        kept = [path for path in bases if path not in deleted and rows[path][2] in SCANNED_TYPES]
        print(f"Reading {len(kept)} kept text row(s) for references...")
        texts = [(path, stored_bytes(path)) for path in kept]

        def scan(item):
            """References a kept row makes, read after its links to a one-page index being deleted are pointed at
            the split index and its links to dropped copies at the kept ones, so a link either rewrite misses is
            still caught. A templated row's JSON gets only the second: a link there to a deleted index refuses the
            run."""
            path, raw, error = decode(item)
            if raw is None:
                return path, None, error, None, 0, 0, []
            new, count, copies, link_problems = raw, 0, 0, []
            if split_index and rows[path][4] == 0 and _INDEX_ALL_NAME.search(raw):
                new, count, link_problems = point_at_split_index(raw, posixpath.dirname(path), split_index)
            if copy_links is not None and copy_links.search(new):
                new, copies = point_at_kept_copies(new, posixpath.dirname(path), remap, copy_links)
                if copies and rows[path][4] != 0:
                    try:
                        json.loads(new)
                    except ValueError as exc:
                        link_problems.append(f"is no longer JSON once its links point at the kept copies: {exc}")
            text = new.decode("utf-8", errors="replace")
            return (path, refs.in_text(text, (posixpath.dirname(path),), rows[path][4] != 0), None,
                    new if count or copies else None, count, copies, link_problems)

        # kept row -> its decoded bytes with its links pointed at the split index or at the kept copies
        rewrites, links_pointed, index_rows, copies_pointed, copy_rows = {}, 0, 0, 0, 0
        with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            for path, targets, error, new, count, copies, link_problems in pool.map(scan, texts):
                if targets is None:
                    problems.append(f"{path} could not be read, so its references were not checked: {error}")
                    continue
                found.update((path, target, how) for target, how in targets)
                problems.extend(f"{path} {problem}" for problem in link_problems)
                if new is not None:
                    rewrites[path] = new
                    links_pointed += count
                    index_rows += 1 if count else 0
                    copies_pointed += copies
                    copy_rows += 1 if copies else 0
        texts = None

        for name, dirs, text, error in templates:
            if text is None:
                problems.append(f"template {name} could not be decoded, so its links were not checked: {error}")
                continue
            found.update((f"template {name}", target, how) for target, how in refs.in_text(text, dirs))
        # Tooltip button uris pointed at the kept copies, checked as they will be written.
        uri_rewrites = {}
        if copy_links is not None:
            for _tooltip_id, uri in conn.execute(buttons_query):
                new, changed = point_at_kept_copies(uri.encode("utf-8"), "", remap, copy_links)
                if changed:
                    uri_rewrites[uri] = new.decode("utf-8")
        for tooltip_id, uri in conn.execute(buttons_query):
            uri = uri_rewrites.get(uri, uri)
            found.update((f"tooltip {tooltip_id} button", target, how) for target, how in refs.targets(uri, ("",)))
        for tooltip_id, summary, detail in conn.execute(texts_query):
            found.update((f"tooltip {tooltip_id} text", target, how)
                         for target, how in refs.in_text(f"{summary}\n{detail}", ("",)))
        by_id = {rows[path][0]: path for path in deleted}
        for (content_id,) in conn.execute("SELECT contentID FROM Bookshelf"):
            if content_id in by_id:
                found.add(("Bookshelf", by_id[content_id], "lists"))

        allowed = sorted({(src, dst) for src, dst, _how in found if (src, dst) in ALLOWED})
        problems.extend(f"{src} {how} {dst}" for src, dst, how in sorted(found) if (src, dst) not in ALLOWED)

        cmap_names = {posixpath.basename(path)[:-len(".bcmap")] for path, group in doomed.items()
                      if group == "pdfjs-cmaps" and path.endswith(".bcmap")} - IDENTITY_CMAPS
        if cmap_names:
            pdfs = [path for path in bases if path not in deleted and rows[path][2] == "application/pdf"]
            for path, raw, error in map(decode, [(path, stored_bytes(path)) for path in pdfs]):
                needs = [f"could not be read: {error}"] if raw is None else cmap_needs(raw, cmap_names)
                problems.extend(f"{path} {need}" for need in needs)
            print(f"Read {len(pdfs)} PDF(s) for fonts that need a CMap.")

        for src, dst in allowed:
            print(f"  allowed: {src} refers to {dst}; {ALLOWED[(src, dst)]}")
        if problems:
            for problem in problems:
                print(f"  error: {problem}", file=sys.stderr)
            print(f"error: {len(problems)} problem(s) found; nothing was changed.", file=sys.stderr)
            return 1

        freed_total = sum(lengths[path] for path in deleted)
        for group in groups:
            count = sum(1 for g in doomed.values() if g == group)
            chunks = sum(1 for path, g in deleted.items() if g == group and path not in doomed)
            freed = sum(lengths[path] for path, g in deleted.items() if g == group)
            print(f"  {group}: {count} row(s) of {GROUPS[group][0]}"
                  + (f" and {chunks} chunk row(s)" if chunks else "") + f", {human(freed)} stored byte(s)")
        if viewer_changed:
            print(f"  {VIEWER}: enableScripting default set to false")
        if links_pointed:
            print(f"  javadoc-index-all: {links_pointed} link(s) in {index_rows} row(s) pointed at the split index")
        if remap:
            print(f"  duplicate-media: {copies_pointed} link(s) in {copy_rows} row(s) and {len(uri_rewrites)} tooltip "
                  f"uri(s) pointed at the {len(set(remap.values()))} kept copies")
            if cfg["verbose"]:
                for path in sorted(remap):
                    print(f"  [COPY] {path} -> {remap[path]}")

        if cfg["dry_run"]:
            print("Dry run: nothing written.")
        else:
            with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
                recompressed = dict(pool.map(lambda p: (p, codec.compress(rewrites[p], rows[p][3])), sorted(rewrites)))
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
            conn.execute("BEGIN")
            try:
                for path in sorted(doomed):
                    delete_media_row(conn, path, lengths[path])
                    if cfg["verbose"]:
                        print(f"  [DEL] {path}")
                for path in sorted(recompressed):
                    language_id, content_type_id, template_id = conn.execute(
                        "SELECT languageID, contentTypeID, templateId FROM Content WHERE path = ?", (path,)).fetchone()
                    replace_row(conn, path, lengths[path], recompressed[path], language_id, content_type_id,
                                template_id, lambda m: print(m, file=sys.stderr))
                    if cfg["verbose"]:
                        print(f"  [LINK] {path}")
                # The uri table of the database's tooltip schema: TooltipButtonUris in 3, TooltipButtons in 2.
                uri_table = "TooltipButtonUris" if "TooltipButtonUris" in buttons_query else "TooltipButtons"
                for old, new in sorted(uri_rewrites.items()):
                    # TooltipButtonUris stores each distinct uri once (writers look one up before inserting it), so
                    # buttons whose new uri already has a row are pointed at that row and the old row goes.
                    stored = None
                    if uri_table == "TooltipButtonUris":
                        stored = conn.execute("SELECT MIN(id) FROM TooltipButtonUris WHERE uri = ?",
                                              (new,)).fetchone()[0]
                    if stored is None:
                        conn.execute(f"UPDATE {uri_table} SET uri = ? WHERE uri = ?", (new, old))
                    else:
                        conn.execute("UPDATE TooltipButtons SET uriId = ? WHERE uriId IN "
                                     "(SELECT id FROM TooltipButtonUris WHERE uri = ?)", (stored, old))
                        conn.execute("DELETE FROM TooltipButtonUris WHERE uri = ?", (old,))
                if viewer_changed:
                    language_id, content_type_id, template_id = conn.execute(
                        "SELECT languageID, contentTypeID, templateId FROM Content WHERE path = ?",
                        (VIEWER,)).fetchone()
                    replace_row(conn, VIEWER, lengths[VIEWER], codec.compress(viewer_new, rows[VIEWER][3]),
                                language_id, content_type_id, template_id, lambda m: print(m, file=sys.stderr))
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

    verb = "would delete" if cfg["dry_run"] else "deleted"
    print(f"\nDone. {verb} {len(deleted)} row(s), {human(freed_total)} stored byte(s)"
          + ("; scripting turned off in the viewer" if viewer_changed else "")
          + (f"; {'would point' if cfg['dry_run'] else 'pointed'} {links_pointed} index link(s) in {index_rows} "
             "row(s) at the split index" if links_pointed else "")
          + (f"; {'would point' if cfg['dry_run'] else 'pointed'} {copies_pointed} link(s) in {copy_rows} row(s) and "
             f"{len(uri_rewrites)} tooltip uri(s) at the kept copies" if remap else "") + ".")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the checks and report what would change, without writing or backing up")
    p.add_argument("--only", action="append", choices=list(GROUPS), dest="groups",
                   help="Delete only this group; repeat for more (default: all of them)")
    p.add_argument("--workers", type=int, default=8,
                   help="Threads used to decompress and read rows (default: 8)")
    p.add_argument("--verbose", action="store_true", help="Log every row deleted")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "groups": args.groups,
                  "workers": args.workers, "verbose": args.verbose}))


if __name__ == "__main__":
    main()
