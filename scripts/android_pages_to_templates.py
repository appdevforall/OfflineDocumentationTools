#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
# ]
# ///
"""
android_pages_to_templates.py

Stores the Android reference pages under a/ as JSON plus shared Pebble
templates, instead of as finished HTML pages (ADFA-3305).

An a/ page repeats the same structures many times: summary rows, detail
headers, parameter rows, and above all the "Inherited methods" blocks, which
copy a superclass's member rows onto every subclass page (ListView, CheckBox,
Switch and the other widgets each carry about 0.5 MB of View's). A shared
256 KiB dictionary cannot hold those, so every page pays for them. This step
moves them into templates:

  - Records. A page is cut into segments. A segment is either raw HTML or a
    record of one "shape": HTML with named holes, written once in SHAPES. The
    same shape text gives the regex that reads a record out of a page and the
    template branch that writes it back, so a record renders to exactly the
    text it was read from. A hole used twice in one shape is stored once, and
    a link into the page itself is stored as its fragment only. The fixed head
    and footer variants are found in the pages themselves (each used by at
    least MIN_SHARED_PAGES pages) and stored with the title as a hole.
  - Shared includes. Each inherited-member block (Doclava's "expandable
    jd-inherited-apis" or Dackka's <devsite-expandable>) is keyed by its exact
    header. The rows every page's block for one key shows are merged into one
    order; the include template android-inherited-<n>.peb holds the header,
    each row behind {% if x contains <i> %}, and the footer. A page stores
    {"k": ..., "i": <n>, "x": [rows it leaves out]} in place of the block: the
    members its class overrides. A key whose pages disagree on the order of
    their rows, or repeat a row, keeps its blocks in the pages.
  - The main template android-reference.peb loops over a page's segments.

Pages: every text/html row under a/ with templateId 0, except a/devsite/
(guide pages of another shape). A row in another template, a page that is not
UTF-8 or holds a NUL is left as it is and named.

Before anything is written, every converted page is rendered with the real
Pebble engine (scripts/hm_render_check/Render.java, built as the app builds
it, with the app's 4 Mi-character render limit) and compared with the stored
page byte for byte. A page that does not render back exactly, or is over the
limit (a/android/R.styleable.html, 4,455,003 characters), stays HTML and is
named. Rendering needs `java` (17 or later) and the Pebble 4.1.1, unbescape,
slf4j-api and Gson jars, from the Gradle or Maven cache or --classpath.

Templates are written as plain UTF-8. On the ship-time copy the 3.0.0
migration then compresses every template against the shared dictionary,
which the app reads in documentation schema 3.0.0 (CodeOnTheGo, stacked on
#2163); today's app reads templates as plain text, so a database from this
step needs that app. For the same reason the step refuses a database that
already declares documentation version 3 or later (its newest
DocumentationDatabaseVersion row, ordered as the app orders them: changeTime,
then rowid): its plain templates would sit among compressed ones, and the app
would fail every converted page.

Runs in one transaction, backs the database up first and VACUUMs afterwards.
--dry-run does everything, the render check included, and writes nothing.
The step does not run twice: on a database that already holds its templates
it refuses. Exit 1 always means nothing was written: a database declaring
version 3 or later, a missing renderer, an a/ row that cannot be read, or
templates already present.

Pipeline position: on the ship-time copy, never on the master database the
other tools edit. After the steps that write a/ pages (minify_text.py,
drop_unread_rows.py) and trim_kotlin_json.py; before the 3.0.0 migration
(migrate_tooltip_schema.py, which compresses the templates) and the final
dictionary re-mint (remint_dictionary.py, which re-mints templates too), then
packing (PR #38's rebuild_database.py).

Measured on the 2026-10-18 schema-3 test database (98,201,600 bytes), against
the same database with only its templates compressed; both re-minted with 4
seeds (templates sampled and recompressed with the rows), packed with
rebuild_database.py --pack Content,TooltipTexts --page-size 4096, .br with
brotli -q 11 -w 24:

    pages converted   12,827 of 12,828 (R.styleable stays HTML)
    templates         1 main + 1,119 includes, 12.1 MB as text
    file              98,271,232 -> 90,857,472  (-7,413,760, -7.5%)
    .br               80,139,063 -> 72,644,272  (-7,494,791, -9.4%)

The seed-to-seed spread within each re-mint was at most 0.4 MB.
Render time on a desktop JVM, JSON parse included: median 0.15 ms per page,
largest page (R.attr, 2.1 M characters) 13 ms.

Usage:
    uv run scripts/android_pages_to_templates.py documentation.db [--dry-run] [--workers N] [--verbose]
"""
from __future__ import annotations

import argparse
import graphlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from hm_pages_to_templates import PebbleHarness, find_classpath, first_difference
from optimize_db_media import BrotliCodec, backup_database, human, is_continuation_path, load_dictionary, reassemble
from update_media_references import replace_row

PREFIX = "a/"
EXCLUDED = ("a/devsite/",)
CONTENT_TYPE = "text/html"
MAIN_TEMPLATE = "android-reference.peb"
INCLUDE_PREFIX = "android-inherited-"
INCLUDE_SUFFIX = ".peb"

# A head or footer variant becomes a template shape only when this many pages share it exactly.
MIN_SHARED_PAGES = 5

# From this documentation major on, migrate_tooltip_schema.py (ADFA-3305) has stored every template compressed
# against the dictionary, and the app decodes every one: the plain templates this step writes would not render.
COMPRESSED_TEMPLATES_MAJOR = 3

# Hole marker inside SHAPES text: H + name + H. Pages holding a NUL are not converted, so it never collides.
H = "\x00"

# Hole name -> regex. Multi-line holes are bounded so that a match that fails cannot scan a whole page.
# A name with trailing digits (id2) uses its base name's regex.
HOLES = {
    "t": r"[^<]*",
    "v": r'[^"\n]*', "lv": r'[^<\n]*', "id": r'[^"\n]*',
    "n": r'[^<"\n]*',
    "h": r'[^"\n]*',
    "ps": r"[^\n]*?", "c": r"[^\n]*?", "a": r"[^\n]*", "w": r"[^<\n]*", "cl": r'[^"\n]*', "x": r"[^\n]*",
    "ty": r"(?s:.{0,4000}?)", "d": r"(?s:.{0,20000}?)", "sig": r"(?s:.{0,8000}?)",
}
# Holes holding a link: a link into the page itself is stored as its fragment, under "<name>#".
LINK_HOLES = {"h"}

API = 'Added in <a class="broken-link" href="/guide/topics/manifest/uses-sdk-element#ApiLevels">API level '
PRE = '<div></div><devsite-code><pre class="api-signature no-pretty-print" dir="ltr" is-upgraded="" translate="no">'
SHAPES = [
    # Doclava summary rows: constant or field, method, constructor.
    ('<tr data-version-added="\0v\0">\n<td><code dir="ltr" translate="no">\0ty\0</code></td>\n<td width="100%">\n'
     '<code dir="ltr" translate="no"><a href="\0h\0">\0n\0</a></code>\n\0d\0</td>\n</tr>\n'),
    ('<tr data-version-added="\0v\0">\n<td><code dir="ltr" translate="no">\n\0ty\0</code>\n</td>\n<td width="100%">\n'
     '<code dir="ltr" translate="no">\n<a href="\0h\0">\0n\0</a>\0ps\0\n</code>\n\0d\0</td>\n</tr>\n'),
    ('<tr data-version-added="\0v\0">\n<td width="100%">\n'
     '<code dir="ltr" translate="no">\n<a href="\0h\0">\0n\0</a>\0ps\0\n</code>\n\0d\0</td>\n</tr>\n'),
    # Doclava detail headers: method or constructor (api-level in an inner div), field or constant.
    ('<div data-version-added="\0v\0">\n<h3 class="api-name" data-text="\0n\0" id="\0id\0" tabindex="-1">\0n\0</h3>\n'
     '<div class="api-level">\n<div>\n' + API + '\0v\0</a>\n</div>\n</div>\n' + PRE + '\n\0sig\0</pre></devsite-code>\n'),
    ('<div data-version-added="\0v\0">\n<h3 class="api-name" data-text="\0n\0" id="\0n\0" tabindex="-1">\0n\0</h3>\n'
     '<div class="api-level">\n' + API + '\0v\0</a>\n</div>\n' + PRE + '\n\0sig\0</pre></devsite-code>\n'),
    # Parameter, return and exception rows, and their table heads.
    ('<tr>\n<td><code dir="ltr" translate="no">\0n\0</code></td>\n<td width="100%">\n'
     '<code dir="ltr" translate="no">\0ty\0</code>\0d\0</td>\n</tr>\n'),
    '<tr>\n<td><code dir="ltr" translate="no">\0ty\0</code></td>\n<td width="100%">\0d\0</td>\n</tr>\n',
    '<table class="responsive">\n<tr><th colspan="2">\0n\0</th></tr>\n',
    '<p>Constant Value:\n\0c\0\n(\0x\0)\n</p>',
    # Doclava section and table headings, and an inherited block kept in the page.
    '<h2 class="api-section" data-text="\0n\0" id="\0id\0" tabindex="-1">\0n\0</h2>\n',
    ('<table class="\0cl\0" id="\0id2\0">\n<tr><th colspan="2">'
     '<h3 data-text="\0n\0" id="\0id\0" tabindex="-1">\0n\0</h3></th></tr>\n'),
    '<table class="\0cl\0" id="\0id2\0">\n<tr><th><h3 data-text="\0n\0" id="\0id\0" tabindex="-1">\0n\0</h3></th></tr>\n',
    ('<tr>\n<td colspan="2">\n<div class="expandable jd-inherited-apis">\n<span class="expand-control">From \0w\0\n'
     '<code dir="ltr" translate="no">\n<a href="\0h\0">\0n\0</a>\n</code>\n</span>\n<table class="responsive">\n'),
    '</table>\n</div>\n</td></tr>\n',
    # Dackka summary rows, table heads and ends, detail headers with and without version metadata, sections.
    ('<tr>\n<td><code dir="ltr" translate="no">\0ty\0</code></td>\n<td>\n'
     '<div><code dir="ltr" translate="no"><a href="\0h\0">\0n\0</a>\0ps\0</code></div>\n\0d\0</td>\n</tr>\n'),
    '<tr>\n<td>\n<div><code dir="ltr" translate="no">\0c\0</code></div>\n\0d\0</td>\n</tr>\n',
    ('<div class="devsite-table-wrapper">\n<table class="responsive">\n<colgroup>\n<col width="40%"/>\n<col/>\n'
     '</colgroup>\n<thead>\n<tr>\n<th colspan="100%"><h3 data-text="\0n\0" id="\0id\0" tabindex="-1">\0n\0</h3></th>\n'
     '</tr>\n</thead>\n<tbody class="list">\n'),
    '</tbody>\n</table>\n</div>\n',
    ('<div class="api-item">\0a\0\n<div class="api-name-block">\n<div>\n'
     '<h3 data-text="\0n\0" id="\0id\0" tabindex="-1">\0n\0</h3>\n</div>\n<div id="metadata-info-block">\n'
     '<div id="version-metadata">\n<div id="added-in">Added in <a class="broken-link" href="\0h\0">\0lv\0</a></div>\n'
     '</div>\n</div>\n</div>\n' + PRE + '\0sig\0</pre></devsite-code>\n'),
    ('<div class="api-item">\0a\0\n<div class="api-name-block">\n<div>\n'
     '<h3 data-text="\0n\0" id="\0id\0" tabindex="-1">\0n\0</h3>\n</div>\n</div>\n' + PRE + '\0sig\0</pre></devsite-code>\n'),
    '<div class="list">\n<h2 data-text="\0n\0" id="\0id\0" tabindex="-1">\0n\0</h2>\n',
]

# Inherited-member blocks: (header regex, footer). Between them, only table rows.
BLOCK_STYLES = (
    (re.compile(r'<tr>\n<td colspan="2">\n<div class="expandable jd-inherited-apis">\n<span class="expand-control">'
                r'From [^\n]*\n<code dir="ltr" translate="no">\n<a href="[^"\n]*">[^<\n]*</a>\n</code>\n</span>\n'
                r'<table class="responsive">\n'),
     '</table>\n</div>\n</td></tr>\n'),
    (re.compile(r'<tr>\n<td><devsite-expandable><span class="expand-control">From [^\n]*</span>\n'
                r'<div class="devsite-table-wrapper">\n<table class="responsive">\n<colgroup>\n<col width="40%"/>\n'
                r'<col/>\n</colgroup>\n<tbody class="list">\n'),
     '</tbody>\n</table>\n</div>\n</devsite-expandable> </td>\n</tr>\n'),
)
KEY_JOIN = "\0"  # between a block's header and footer in its key
_TR_OPEN = re.compile(r"<tr[\s>]")

# Pebble drops the line break right after any tag: one match of these, in this order
# (io.pebbletemplates.pebble.lexer.Syntax), at the tag's end.
_PEBBLE_NEWLINE = re.compile("\r\n|\n\r|\r|\n|\u0085| | ")
_PEBBLE_SYNTAX = ("{{", "{%", "{#")
_END_VERBATIM = re.compile(r"\{%-?\s*endverbatim")
# What Pebble 4.1.1 strips from the start of a verbatim block, all of it: Java's Character.isWhitespace plus
# U+0085 (measured, one character at a time).
_VERBATIM_STRIPS = frozenset("\t\n\u000b\f\r\u001c\u001d\u001e\u001f \u0085      "
                             "        　")


class Unrepresentable(ValueError):
    """Literal text a template cannot hold as written (it would end a verbatim block)."""


def after_tag(text: str) -> str:
    """Text that follows a tag, written so Pebble's newline trimming leaves it whole: when it starts with a
    line break, that character is doubled, and the match Pebble drops is then exactly the extra character
    (a doubled character never forms one of the two-character breaks)."""
    if text and _PEBBLE_NEWLINE.match(text):
        return text[0] + text
    return text


def static_text(text: str, after_a_tag: bool = True) -> str:
    """Pebble source for literal text, by default text that follows a tag. Text holding Pebble syntax goes in
    a verbatim block, its leading whitespace before the block (Pebble strips all of it inside); a final "{"
    is printed from a string literal, since a tag right after it would read as "{{"."""
    if not text:
        return ""
    if text.endswith("{"):
        return static_text(text[:-1], after_a_tag) + '{{ "{" }}'
    if any(s in text for s in _PEBBLE_SYNTAX):
        if _END_VERBATIM.search(text):
            raise Unrepresentable(text[:80])
        i = 0
        while text[i] in _VERBATIM_STRIPS:
            i += 1
        lead = text[:i]
        return (after_tag(lead) if after_a_tag else lead) + "{% verbatim %}" + text[i:] + "{% endverbatim %}"
    return after_tag(text) if after_a_tag else text


def hole_regex(name: str) -> str:
    return HOLES[name] if name in HOLES else HOLES[name.rstrip("0123456789")]


def compile_shape(text: str):
    parts = text.split(H)
    pattern, seen = [], set()
    for i, part in enumerate(parts):
        if i % 2 == 0:
            pattern.append(re.escape(part))
        elif part in seen:
            pattern.append(f"(?P={part})")
        else:
            seen.add(part)
            pattern.append(f"(?P<{part}>{hole_regex(part)})")
    return re.compile("".join(pattern))


def hole_source(name: str) -> str:
    if name in LINK_HOLES:
        return (f'{{% if s["{name}#"] is not null %}}{{{{ u | raw }}}}#{{{{ s["{name}#"] | raw }}}}'
                f'{{% else %}}{{{{ s.{name} | raw }}}}{{% endif %}}')
    return f"{{{{ s.{name} | raw }}}}"


def branch_source(shape: str) -> str:
    """The template source writing one record of `shape`. Every literal part follows a tag: the elseif, or
    a hole's print or endif."""
    out, parts = [], shape.split(H)
    for i, part in enumerate(parts):
        out.append(hole_source(part) if i % 2 else static_text(part))
    return "".join(out)


def head_and_tail_shapes(pages: list) -> list:
    """The fixed head (with the title as a hole) and footer variants that at least MIN_SHARED_PAGES pages
    share exactly, most shared first."""
    mark = '<div itemscope="" itemtype="http://developers.google.com/ReferenceObject">\n'
    heads, tails = Counter(), Counter()
    for page in pages:
        i = page.find(mark)
        if i > 0:
            heads[re.sub(r"<title>[^<]*</title>", f"<title>{H}t{H}</title>", page[:i + len(mark)], count=1)] += 1
        j = page.rfind("<devsite-hats-survey")
        if j > 0:
            tails[page[j:]] += 1
    out = []
    for shape, count in heads.most_common() + tails.most_common():
        if count < MIN_SHARED_PAGES:
            continue
        try:
            branch_source(shape)
        except Unrepresentable:
            continue
        out.append(shape)
    return out


class Converter:
    def __init__(self, shapes: list, include_kind: int = None):
        self.shapes = shapes
        self.regexes = [compile_shape(s) for s in shapes]
        self.firsts = [s.split(H, 1)[0][:12] for s in shapes]
        self.include_kind = len(shapes) if include_kind is None else include_kind

    def template(self) -> str:
        out = ["{% for s in b %}{% if s.k is null %}{{ s.r | raw }}"]
        for k, shape in enumerate(self.shapes):
            out.append(f"{{% elseif s.k == {k} %}}" + branch_source(shape))
        out.append(f'{{% elseif s.k == {self.include_kind} %}}'
                   f'{{% include ("{INCLUDE_PREFIX}" ~ s.i ~ "{INCLUDE_SUFFIX}") with {{"x": s.x}} %}}')
        out.append("{% endif %}{% endfor %}")
        return "".join(out)

    def segments(self, page: str, url: str) -> list:
        segs, raw, pos, n = [], [], 0, len(page)
        while pos < n:
            for k, rx in enumerate(self.regexes):
                if not page.startswith(self.firsts[k], pos):
                    continue
                m = rx.match(page, pos)
                if m and m.end() > pos:
                    if raw:
                        segs.append({"r": "".join(raw)})
                        raw = []
                    rec = {"k": k}
                    for name, value in m.groupdict().items():
                        if name in LINK_HOLES and value.startswith(url + "#"):
                            rec[name + "#"] = value[len(url) + 1:]
                        else:
                            rec[name] = value
                    segs.append(rec)
                    pos = m.end()
                    break
            else:
                nl = page.find("\n", pos)
                end = n if nl < 0 else nl + 1
                raw.append(page[pos:end])
                pos = end
        if raw:
            segs.append({"r": "".join(raw)})
        return segs


def split_rows(page: str, pos: int):
    """The table rows starting at pos, each a whole <tr>...</tr> with any nested rows, and where they end."""
    rows = []
    while page.startswith("<tr", pos):
        depth, p = 0, pos
        while True:
            nl = page.find("\n", p)
            if nl < 0:
                return None, pos
            line = page[p:nl + 1]
            depth += len(_TR_OPEN.findall(line)) - line.count("</tr>")
            p = nl + 1
            if depth <= 0:
                break
        rows.append(page[pos:p])
        pos = p
    return rows, pos


def find_blocks(page: str) -> list:
    """(start, end, key, rows) for each inherited-member block, in page order."""
    blocks = []
    for rx, foot in BLOCK_STYLES:
        for m in rx.finditer(page):
            rows, pos = split_rows(page, m.end())
            if rows and page.startswith(foot, pos):
                blocks.append((m.start(), pos + len(foot), m.group(0) + KEY_JOIN + foot, rows))
    blocks.sort()
    return blocks


def plan_includes(found: list) -> dict:
    """key -> {row: its index in the merged order}, for each key whose pages agree on their rows' order."""
    seqs = defaultdict(list)
    for blocks in found:
        for _start, _end, key, rows in blocks:
            seqs[key].append(rows)
    keys = {}
    for key, sequences in seqs.items():
        # A row repeated within one block is a cycle too (x -> ... -> x), so the sort refuses it.
        sorter = graphlib.TopologicalSorter()
        for s in sequences:
            for i, row in enumerate(s):
                sorter.add(row, *([s[i - 1]] if i else []))
        try:
            order = list(sorter.static_order())
        except graphlib.CycleError:
            continue
        try:
            include_source(key, {row: i for i, row in enumerate(order)})
        except Unrepresentable:
            continue
        keys[key] = {row: i for i, row in enumerate(order)}
    return keys


def include_source(key: str, order: dict) -> str:
    head, foot = key.split(KEY_JOIN)
    out = [static_text(head, after_a_tag=False)]  # the template's first text follows no tag
    for row, i in sorted(order.items(), key=lambda t: t[1]):
        out.append(f"{{% if x contains {i} %}}{{% else %}}{static_text(row)}{{% endif %}}")
    out.append(static_text(foot))
    return "".join(out)


# Worker state, set by _init in each process.
_CONVERTER = None
_INCLUDES = None


def _init(shapes: list, includes: dict) -> None:
    global _CONVERTER, _INCLUDES
    _CONVERTER = Converter(shapes)
    _INCLUDES = includes


def _find(page: str) -> list:
    return find_blocks(page)


def _convert(item: tuple) -> dict:
    """A page's JSON context: its own URL and its segments, with each inherited block whose key has an
    include replaced by an include record."""
    path, page, blocks = item
    url = "/" + path
    segs, pos = [], 0
    for start, end, key, rows in blocks:
        if key not in _INCLUDES:
            continue
        order, number = _INCLUDES[key]
        segs += _CONVERTER.segments(page[pos:start], url)
        rec = {"k": _CONVERTER.include_kind, "i": number}
        present = {order[row] for row in rows}
        left_out = [i for i in range(len(order)) if i not in present]
        if left_out:
            rec["x"] = left_out
        segs.append(rec)
        pos = end
    segs += _CONVERTER.segments(page[pos:], url)
    return {"u": url, "b": segs}


def context_json(context: dict) -> bytes:
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def include_name(number: int) -> str:
    return f"{INCLUDE_PREFIX}{number}{INCLUDE_SUFFIX}"


def _pool_map(fn, items: list, workers: int, initargs=None) -> list:
    if workers <= 1:
        if initargs is not None:
            _init(*initargs)
        return [fn(item) for item in items]
    kwargs = {"initializer": _init, "initargs": initargs} if initargs is not None else {}
    with ProcessPoolExecutor(max_workers=workers, **kwargs) as pool:
        return list(pool.map(fn, items, chunksize=max(1, len(items) // (workers * 8) or 1)))


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    log = (lambda m: print(m)) if cfg["verbose"] else (lambda m: None)
    workers = max(1, cfg["workers"])

    conn = sqlite3.connect(db_path)
    codec = None
    harness = cfg.get("renderer")
    own_harness = False
    try:
        # First, so a refusal needs no renderer and writes nothing, in a dry run too.
        refusal = compressed_templates_refusal(conn)
        if refusal:
            print(f"error: {refusal}. Nothing written.", file=sys.stderr)
            return 1
        taken = [name for (name,) in conn.execute("SELECT name FROM Templates")
                 if name == MAIN_TEMPLATE or name.startswith(INCLUDE_PREFIX)]
        if taken:
            print(f"error: this database already holds this step's templates ({', '.join(sorted(taken)[:3])}"
                  f"{', ...' if len(taken) > 3 else ''}); run it on a database it has not converted. "
                  "Nothing written.", file=sys.stderr)
            return 1
        if harness is None:
            try:
                classpath = cfg["classpath"] or find_classpath()
            except RuntimeError as exc:
                print(f"error: {exc}; nothing written.", file=sys.stderr)
                return 1
            harness = PebbleHarness(classpath, cfg["java"])
            own_harness = True
        codec = BrotliCodec(load_dictionary(conn))

        # The prefix is matched exactly: LIKE ignores case and reads "_" as a wildcard.
        rows = conn.execute(
            "SELECT c.path, c.content, c.languageID, c.contentTypeID, c.templateId, ct.compression "
            "FROM Content c JOIN ContentTypes ct ON c.contentTypeID = ct.id "
            "WHERE ct.value = ? AND substr(c.path, 1, ?) = ? ORDER BY c.path",
            (CONTENT_TYPE, len(PREFIX), PREFIX)).fetchall()
        lengths = {r[0]: len(r[1]) for r in rows}
        left, candidates = [], []
        for path, blob, language_id, type_id, template_id, compression in rows:
            if not path.startswith(PREFIX) or is_continuation_path(lengths, path) or path.startswith(EXCLUDED):
                continue
            if template_id:
                left.append((path, f"rendered through another template (templateId {template_id})"))
                continue
            candidates.append((path, reassemble(conn, path, blob), len(blob), language_id, type_id, compression))

        def decode(c):
            try:
                return codec.decompress(c[1], c[5]), None
            except Exception as exc:  # noqa: BLE001 - reported below, and nothing is written
                return None, f"could not be read: {exc}"

        with ThreadPoolExecutor(max_workers=workers) as pool:
            decoded = list(pool.map(decode, candidates))
        unreadable = [(c[0], why) for c, (_d, why) in zip(candidates, decoded) if why]
        if unreadable:
            for path, why in unreadable:
                print(f"  {path}: {why}", file=sys.stderr)
            print(f"error: {len(unreadable)} a/ row(s) could not be read, so nothing was written.", file=sys.stderr)
            return 1

        pages, meta = [], {}
        for c, (data, _why) in zip(candidates, decoded):
            path = c[0]
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                left.append((path, "is not UTF-8"))
                continue
            if H in text:
                left.append((path, "holds a NUL character"))
                continue
            pages.append((path, text))
            meta[path] = c
        print(f"Found {human(len(pages))} a/ page(s) to convert"
              f"{f'; {len(left)} left as they are' if left else ''}.")
        if not pages:
            for path, why in left:
                print(f"  left as it is: {path} - {why}", file=sys.stderr)
            print("Nothing to convert.")
            return 0

        found = _pool_map(_find, [p for _, p in pages], workers)
        keys = plan_includes(found)
        includes = {key: (order, n) for n, (key, order) in enumerate(keys.items())}
        include_sources = {include_name(n): include_source(key, order) for key, (order, n) in includes.items()}
        shapes = head_and_tail_shapes([p for _, p in pages]) + SHAPES
        template = Converter(shapes).template()
        contexts = _pool_map(_convert, [(path, page, blocks) for (path, page), blocks in zip(pages, found)],
                             workers, (shapes, includes))
        blocks_total = sum(len(b) for b in found)
        print(f"{human(blocks_total)} inherited-member block(s) under {human(len({b[2] for bl in found for b in bl}))} "
              f"header(s); {human(len(includes))} include(s) cover "
              f"{human(sum(1 for bl in found for b in bl if b[2] in includes))} of them.")

        harness.define(MAIN_TEMPLATE, template)
        for name, source in include_sources.items():
            harness.define(name, source)
        converted, stay = [], []
        for (path, page), context in zip(pages, contexts):
            data = context_json(context)
            ok, body, _nanos = harness.render(MAIN_TEMPLATE, data)
            expected = page.encode("utf-8")
            if ok and body == expected:
                converted.append((path, data, context))
            elif not ok and "Tried to write more than" in body:
                stay.append((path, "is over the app's render limit"))
            elif not ok:
                stay.append((path, f"does not render: {body.strip().splitlines()[0][:200] if body.strip() else body}"))
            else:
                stay.append((path, f"renders differently from byte {first_difference(body, expected)}"))
        used = {include_name(seg["i"]) for _p, _d, context in converted for seg in context["b"]
                if seg.get("k") == len(shapes)}
        include_sources = {name: source for name, source in include_sources.items() if name in used}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            stored = list(pool.map(lambda t: codec.compress(t[1], meta[t[0]][5]), converted))
        page_of = dict(pages)
        before = sum(len(meta[p][1]) for p, _d, _c in converted)  # chunks joined
        after = sum(map(len, stored))
        decoded_before = sum(len(page_of[p].encode("utf-8")) for p, _d, _c in converted)
        decoded_after = sum(len(d) for _p, d, _c in converted)
        template_bytes = len(template.encode("utf-8")) + sum(len(s.encode("utf-8")) for s in include_sources.values())

        for path, why in left:
            print(f"  left as it is: {path} - {why}", file=sys.stderr)
        for path, why in stay:
            print(f"  stays HTML: {path} - {why}", file=sys.stderr)
        print(f"{'Would convert' if cfg['dry_run'] else 'Converting'} {human(len(converted))} of {human(len(pages))} "
              f"page(s) to JSON through {MAIN_TEMPLATE} and {human(len(include_sources))} include(s): "
              f"{human(before)} -> {human(after)} stored byte(s) against the current dictionary, "
              f"{human(decoded_before)} -> {human(decoded_after)} decoded, plus {human(template_bytes)} bytes of "
              f"templates as text.")
        if not converted:
            print("Nothing to convert.")
            return 0
        if cfg["dry_run"]:
            print("Dry run: nothing written.")
            return 0

        print(f"Backing up {db_path} ...")
        print(f"Backup written to {backup_database(db_path)}")
        conn.execute("BEGIN")
        try:
            conn.execute("INSERT INTO Templates (name, content) VALUES (?, ?)",
                         (MAIN_TEMPLATE, template.encode("utf-8")))
            main_id = conn.execute("SELECT id FROM Templates WHERE name = ?", (MAIN_TEMPLATE,)).fetchone()[0]
            for name, source in sorted(include_sources.items()):
                conn.execute("INSERT INTO Templates (name, content) VALUES (?, ?)", (name, source.encode("utf-8")))
            for (path, _data, _context), blob in zip(converted, stored):
                _p, _full, base_len, language_id, type_id, _compression = meta[path]
                replace_row(conn, path, base_len, blob, language_id, type_id, main_id,
                            lambda m: print(m, file=sys.stderr))
                conn.execute("UPDATE Content SET templateId = ? WHERE path = ?", (main_id, path))
                log(f"  [JSON] {path}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if codec is not None:
            codec.close()
        conn.close()
        if own_harness:
            harness.close()

    print("Vacuuming database...")
    vac = sqlite3.connect(db_path)
    try:
        vac.execute("VACUUM")
    finally:
        vac.close()
    print("Next: the 3.0.0 migration (compresses the templates), the dictionary re-mint, then packing.")
    return 0


def declared_major(conn) -> int | None:
    """The documentation major the database declares - its newest DocumentationDatabaseVersion row, ordered as
    Code On The Go orders them (changeTime, then rowid) - or None if it declares none."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'DocumentationDatabaseVersion'").fetchone():
        return None
    row = conn.execute("SELECT major FROM DocumentationDatabaseVersion "
                       "ORDER BY changeTime DESC, rowid DESC LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def compressed_templates_refusal(conn) -> str | None:
    """Why this step may not write its plain templates into the database, or None."""
    major = declared_major(conn)
    if major is None or major < COMPRESSED_TEMPLATES_MAJOR:
        return None
    return (f"the database declares documentation version {major}; from {COMPRESSED_TEMPLATES_MAJOR}.0.0 every "
            "template is stored compressed, and this step writes plain ones. Run it before migrate_tooltip_schema.py")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to edit in place: a ship-time copy")
    p.add_argument("--dry-run", action="store_true",
                   help="Do all the work, the render check included, and report what would change without writing")
    p.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                   help="Processes used to convert pages and threads used to (de)compress rows (default: CPUs)")
    p.add_argument("--verbose", action="store_true", help="Log every page written")
    p.add_argument("--classpath", default=os.environ.get("HM_RENDER_CLASSPATH"),
                   help="Classpath for Render.java: Pebble 4.1.1, unbescape, slf4j-api and Gson jars "
                        "(default: $HM_RENDER_CLASSPATH, else found in the Gradle or Maven cache)")
    p.add_argument("--java", default="java", help="The java launcher, 17 or later (default: java)")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run, "workers": args.workers,
                  "verbose": args.verbose, "classpath": args.classpath, "java": args.java}))


if __name__ == "__main__":
    main()
