#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "pytest",
# ]
# ///
"""
test_android_pages_to_templates.py

Covers android_pages_to_templates.py: the Pebble text rules (newline trimming,
verbatim, a final "{"), every record shape read out of a page and written back,
the inherited-member includes (one per header, rows a page leaves out, headers
whose pages disagree kept in the page), and whole runs on small synthetic
databases of Doclava- and Dackka-style pages: every converted page read back
and rendered through the stored templates equals the original; a page that
does not render back, or is over the render limit, stays HTML and is named;
a page whose render fails stays HTML and says why; rows the step must leave
alone are left alone; a dry run, a second run, an unreadable row and a
missing renderer write nothing; a chunked page is read
whole and its JSON written in chunks; a failure while writing rolls back.

Run it either way - as a plain script:

    uv run scripts/test_android_pages_to_templates.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_android_pages_to_templates.py

Tests that render need `java` (17 or later) and the Pebble 4.1.1, unbescape,
slf4j-api and Gson jars in the Gradle or Maven cache (or $HM_RENDER_CLASSPATH);
without them they are skipped. Every test that builds a database needs the
`brotli` CLI on PATH.
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import android_pages_to_templates as apt  # noqa: E402
import hm_pages_to_templates as hm  # noqa: E402
import optimize_db_media  # noqa: E402
import update_media_references  # noqa: E402


class Skip(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest
    except ImportError:
        raise Skip(reason)
    pytest.skip(reason)


_HARNESS = []


def _harness() -> hm.PebbleHarness:
    """One renderer for the whole run: templates are redefined by name, so tests do not see each other's."""
    if not _HARNESS:
        try:
            if shutil.which("java") is None:
                raise RuntimeError("no java on PATH")
            harness = hm.PebbleHarness(os.environ.get("HM_RENDER_CLASSPATH") or hm.find_classpath())
            atexit.register(harness.close)
            _HARNESS.append(harness)
        except RuntimeError as exc:
            _HARNESS.append(exc)
    if isinstance(_HARNESS[0], Exception):
        _skip(f"needs the Pebble renderer: {_HARNESS[0]}")
    return _HARNESS[0]


def _render(template: str, context: dict) -> bytes:
    h = _harness()
    h.define("t", template)
    ok, body, _ = h.render("t", json.dumps(context).encode())
    assert ok, body
    return body


# --- synthetic pages -------------------------------------------------------------

MARK = '<div itemscope="" itemtype="http://developers.google.com/ReferenceObject">\n'
# The style holds Pebble syntax, so the head shape needs a verbatim block.
HEAD = ('<html><head><title>{title}</title><style>.broken-link {{ color: red !important; }}\n'
        '/* {{{{ not a tag }}}} {{% nor this %}} */\n</style></head><body><article class="devsite-article">\n'
        '<div class="devsite-article-body clearfix devsite-no-page-title">\n' + MARK)
TAIL = ('<devsite-hats-survey class="nocontent" hats-id="H1"></devsite-hats-survey>\n</div>\n'
        '<devsite-thumb-rating position="footer">\n</devsite-thumb-rating>\n</article></body></html>\n')
API_LINK = '<a class="broken-link" href="/guide/topics/manifest/uses-sdk-element#ApiLevels">'
PRE = '<div></div><devsite-code><pre class="api-signature no-pretty-print" dir="ltr" is-upgraded="" translate="no">'


def doclava_row(owner: str, name: str, params: str = "()", added: str = "1") -> str:
    return (f'<tr data-version-added="{added}">\n<td><code dir="ltr" translate="no">\nboolean</code>\n</td>\n'
            f'<td width="100%">\n<code dir="ltr" translate="no">\n<a href="/a/{owner}.html#{name}{params}">{name}</a>'
            f'{params}\n</code>\n<p>About {name}, café.\n</p>\n</td>\n</tr>\n')


def doclava_block(owner: str, rows: list, kind: str = "class") -> str:
    dotted = owner.replace("/", ".")
    return (f'<tr>\n<td colspan="2">\n<div class="expandable jd-inherited-apis">\n<span class="expand-control">From '
            f'{kind}\n<code dir="ltr" translate="no">\n<a href="/a/{owner}.html">{dotted}</a>\n</code>\n</span>\n'
            '<table class="responsive">\n' + "".join(doclava_row(owner, n) for n in rows) +
            '</table>\n</div>\n</td></tr>\n')


OBJECT_ROWS = ["clone", "equals", "hashCode", "toString"]


def doclava_page(cls: str, blocks: str = None, extra: str = "") -> str:
    """A Doclava-style class page under a/pkg/ using every Doclava shape."""
    url = f"/a/pkg/{cls}.html"
    blocks = doclava_block("java/lang/Object", OBJECT_ROWS) if blocks is None else blocks
    return (HEAD.format(title=cls) + f'<meta content="pkg.{cls}" itemprop="name"/>\n</div>\n'
            f'<h1 class="api-title">{cls}</h1>\n<p>About {cls} {{{{ braces }}}} and {{% more %}}.</p>\n' + extra +
            '<h2 class="api-section" data-text="Summary" id="summary" tabindex="-1">Summary</h2>\n'
            '<table class="responsive constants" id="constants">\n<tr><th colspan="2"><h3 data-text="Constants" '
            'id="constants" tabindex="-1">Constants</h3></th></tr>\n'
            f'<tr data-version-added="1">\n<td><code dir="ltr" translate="no">int</code></td>\n<td width="100%">\n'
            f'<code dir="ltr" translate="no"><a href="{url}#MAX">MAX</a></code>\n<p>The maximum.\n</p>\n</td>\n</tr>\n'
            '</table>\n<table class="responsive constructors" id="pubctors">\n<tr><th colspan="2"><h3 data-text='
            '"Public constructors" id="public-constructors" tabindex="-1">Public constructors</h3></th></tr>\n'
            f'<tr data-version-added="1">\n<td width="100%">\n<code dir="ltr" translate="no">\n<a href="{url}#{cls}()">'
            f'{cls}</a>()\n</code>\n<p>Makes one.\n</p>\n</td>\n</tr>\n</table>\n'
            '<table class="responsive methods inhtable" id="inhmethods">\n<tr><th><h3 data-text="Inherited methods" '
            'id="inherited-methods" tabindex="-1">Inherited methods</h3></th></tr>\n' + blocks + '</table>\n'
            '<h2 class="api-section" data-text="Constants" id="constants_1" tabindex="-1">Constants</h2>\n'
            '<div data-version-added="1">\n<h3 class="api-name" data-text="MAX" id="MAX" tabindex="-1">MAX</h3>\n'
            f'<div class="api-level">\nAdded in {API_LINK}API level 1</a>\n</div>\n{PRE}\n'
            'public static final int MAX</pre></devsite-code>\n<p>The maximum value.</p>\n'
            '<p>Constant Value:\n7\n(0x00000007)\n</p></div>\n'
            '<h2 class="api-section" data-text="Public methods" id="public-methods_1" tabindex="-1">Public methods</h2>\n'
            '<div data-version-added="21">\n<h3 class="api-name" data-text="run" id="run(int)" tabindex="-1">run</h3>\n'
            f'<div class="api-level">\n<div>\nAdded in {API_LINK}API level 21</a>\n</div>\n</div>\n{PRE}\n'
            'public void run (int times)</pre></devsite-code>\n<p>Runs it.</p>\n'
            '<table class="responsive">\n<tr><th colspan="2">Parameters</th></tr>\n<tr>\n'
            '<td><code dir="ltr" translate="no">times</code></td>\n<td width="100%">\n'
            '<code dir="ltr" translate="no">int</code>: how often.</td>\n</tr>\n</table>\n'
            '<table class="responsive">\n<tr><th colspan="2">Returns</th></tr>\n<tr>\n'
            '<td><code dir="ltr" translate="no">void</code></td>\n<td width="100%">nothing.</td>\n</tr>\n</table>\n'
            '</div>\n' + TAIL)


def dackka_block(owner: str, rows: list) -> str:
    dotted = owner.replace("/", ".")
    body = "".join(f'<tr>\n<td><code dir="ltr" translate="no">int</code></td>\n<td>\n'
                   f'<div><code dir="ltr" translate="no"><a href="/a/{owner}.html#{n}()">{n}</a>()</code></div>\n'
                   f'</td>\n</tr>\n' for n in rows)
    return (f'<tr>\n<td><devsite-expandable><span class="expand-control">From <a href="/a/{owner}.html">{dotted}</a>'
            '</span>\n<div class="devsite-table-wrapper">\n<table class="responsive">\n<colgroup>\n<col width="40%"/>\n'
            '<col/>\n</colgroup>\n<tbody class="list">\n' + body +
            '</tbody>\n</table>\n</div>\n</devsite-expandable> </td>\n</tr>\n')


def dackka_table(title: str, ident: str, rows: str) -> str:
    return ('<div class="devsite-table-wrapper">\n<table class="responsive">\n<colgroup>\n<col width="40%"/>\n<col/>\n'
            f'</colgroup>\n<thead>\n<tr>\n<th colspan="100%"><h3 data-text="{title}" id="{ident}" tabindex="-1">{title}'
            '</h3></th>\n</tr>\n</thead>\n<tbody class="list">\n' + rows + '</tbody>\n</table>\n</div>\n')


def dackka_page(cls: str, inherited: list = None) -> str:
    """A Dackka-style class page under a/x/ using every Dackka shape."""
    url = f"/a/x/{cls}.html"
    inherited = ["getA", "getB", "getC"] if inherited is None else inherited
    methods = (f'<tr>\n<td><code dir="ltr" translate="no">boolean</code></td>\n<td>\n'
               f'<div><code dir="ltr" translate="no"><a href="{url}#equals(kotlin.Any)">equals</a>(Object other)</code>'
               f'</div>\n</td>\n</tr>\n<tr>\n<td>\n<div><code dir="ltr" translate="no">public final class '
               f'<a href="/a/x/{cls}.Inner.html">{cls}.Inner</a></code></div>\n<p>An inner class.</p>\n</td>\n</tr>\n')
    return (HEAD.format(title=cls) + f'<meta content="{cls}" itemprop="name"/>\n</div>\n'
            f'<h1 data-text="{cls}" id="{cls.lower()}" tabindex="-1">{cls}</h1>\n<p>About {cls}.</p>\n' +
            dackka_table("Public methods", "public-methods", methods) +
            dackka_table("Inherited methods", "inherited-methods", dackka_block("x/Base", inherited)) +
            '<div class="list">\n<h2 data-text="Public methods" id="public-methods_1" tabindex="-1">Public methods</h2>\n'
            '<div class="api-item"><a name="equals-kotlin.Any-"></a><a name="equals"></a>\n<div class="api-name-block">\n'
            '<div>\n<h3 data-text="equals" id="equals(kotlin.Any)" tabindex="-1">equals</h3>\n</div>\n</div>\n'
            f'{PRE}public boolean <a href="{url}#equals(kotlin.Any)">equals</a>(Object other)</pre></devsite-code>\n'
            '</div>\n<div class="api-item"><a name="size--"></a>\n<div class="api-name-block">\n<div>\n'
            '<h3 data-text="size" id="size()" tabindex="-1">size</h3>\n</div>\n<div id="metadata-info-block">\n'
            '<div id="version-metadata">\n<div id="added-in">Added in <a class="broken-link" '
            'href="/jetpack/androidx/releases/x#1.0.0">1.0.0</a></div>\n</div>\n</div>\n</div>\n'
            f'{PRE}public int size()</pre></devsite-code>\n</div>\n</div>\n' + TAIL)


def standard_pages() -> dict:
    """Six Doclava and six Dackka pages (enough for their head and footer to become shapes). C3 overrides
    hashCode, so it leaves that inherited row out; D2 overrides getB."""
    pages = {}
    for i in range(6):
        rows = [r for r in OBJECT_ROWS if not (i == 3 and r == "hashCode")]
        pages[f"a/pkg/C{i}.html"] = doclava_page(f"C{i}", doclava_block("java/lang/Object", rows))
        pages[f"a/x/D{i}.html"] = dackka_page(f"D{i}", ["getA", "getC"] if i == 2 else None)
    return pages


# --- a synthetic documentation.db --------------------------------------------------

DICTIONARY = (b'<tr data-version-added="1">\n<td><code dir="ltr" translate="no">' * 40 + HEAD.encode() * 4)
SCHEMA = """
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);
CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL,
    content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0, UNIQUE('path'));
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL,
    UNIQUE('name'));
CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL);
"""


def _brotli(data: bytes, decompress: bool = False) -> bytes:
    """Dictionary Brotli at quality 5, independent of the tool's codec."""
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(DICTIONARY)
        args = ["brotli", "-D", str(dict_path)] + (["-d"] if decompress else ["-q", "5"]) + ["-c"]
        return subprocess.run(args, input=data, stdout=subprocess.PIPE, check=True).stdout


def make_db(path: Path, pages: dict = None, extra: dict = None, template_ids: dict = None) -> Path:
    """A database holding `pages` (default: standard_pages()) as text/html rows, plus a guide page under
    a/devsite/, a page outside a/, an image under a/ and an unrelated template. `extra` adds raw rows:
    path -> (bytes, compress?)."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO Languages (value) VALUES ('en-US')")
    conn.execute("INSERT INTO ContentTypes (id, value, compression) VALUES (12, 'text/html', 'brotli')")
    conn.execute("INSERT INTO ContentTypes (id, value, compression) VALUES (3, 'image/png', 'none')")
    conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (DICTIONARY,))
    conn.execute("INSERT INTO Templates (name, content) VALUES ('page.peb', ?)", (b"<p>{{ title }}</p>",))
    rows = {p: (t.encode("utf-8"), True) for p, t in (standard_pages() if pages is None else pages).items()}
    rows.setdefault("a/devsite/guide.html", (doclava_page("Guide").encode("utf-8"), True))
    rows.setdefault("k/other.html", (doclava_page("Other").encode("utf-8"), True))
    rows.update(extra or {})
    for p, (data, compress) in sorted(rows.items()):
        type_id = 3 if p.endswith(".png") else 12
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, ?)",
                     (p, _brotli(data) if compress else data, type_id, (template_ids or {}).get(p, 0)))
    conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES ('a/img.png', 1, ?, 3)",
                 (b"\x89PNG not really",))
    conn.commit()
    conn.close()
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tool(db: Path, **extra) -> tuple:
    cfg = {"db_path": db, "dry_run": False, "workers": 1, "verbose": True, "classpath": None, "java": "java"}
    if "renderer" not in extra:
        cfg["renderer"] = _harness()
    cfg.update(extra)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = apt.run(cfg)
    return code, out.getvalue(), err.getvalue()


def served(db: Path) -> dict:
    """What the app would serve for every Content item: rows with a templateId rendered through the stored
    templates, chunks joined."""
    conn = sqlite3.connect(db)
    try:
        lengths = dict(conn.execute("SELECT path, length(content) FROM Content"))
        names = dict(conn.execute("SELECT id, name FROM Templates"))
        h = _harness()
        for name, content in conn.execute("SELECT name, content FROM Templates"):
            h.define(name, bytes(content).decode("utf-8"))
        out = {}
        for path, blob, tid, compression in conn.execute(
                "SELECT c.path, c.content, c.templateId, ct.compression FROM Content c "
                "JOIN ContentTypes ct ON ct.id = c.contentTypeID"):
            if optimize_db_media.is_continuation_path(lengths, path):
                continue
            data = optimize_db_media.reassemble(conn, path, blob)
            data = _brotli(data, decompress=True) if compression == "brotli" else data
            if tid and names[tid] == apt.MAIN_TEMPLATE:
                ok, body, _ = h.render(names[tid], data)
                assert ok, body
                data = body
            out[path] = (data, tid)
        return out
    finally:
        conn.close()


def templates(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        return {n: bytes(c).decode("utf-8") for n, c in conn.execute("SELECT name, content FROM Templates")}
    finally:
        conn.close()


def stored_context(db: Path, path: str) -> dict:
    conn = sqlite3.connect(db)
    try:
        blob = optimize_db_media.reassemble(conn, path, conn.execute(
            "SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0])
    finally:
        conn.close()
    return json.loads(_brotli(blob, decompress=True))


# --- Pebble text rules ---------------------------------------------------------------

BREAKS = ["\n", "\r", "\r\n", "\n\r", "\u0085", "\u2028", "\u2029", "\n\n", "x\n"]


def test_after_tag_doubles_only_a_leading_line_break():
    for text in BREAKS:
        expected = text[0] + text if text[0] in "\r\n\u0085\u2028\u2029" else text
        assert apt.after_tag(text) == expected, repr(text)
    assert apt.after_tag("") == ""


def test_text_after_a_tag_renders_whole():
    for text in BREAKS + ["\r\nplain", "\n{{ x }}", "\r{% y %}", "a {# z #} b"]:
        source = "{% if true %}{% endif %}" + apt.static_text(text)
        assert _render(source, {}) == text.encode("utf-8"), repr(text)


# Pebble strips all leading whitespace inside a verbatim block (measured), so it is written before the block.
SYNTAX_AFTER_SPACE = ["\n{{ x }}", "\r{% y %}", "  {{ x }}", "\t\n {# c #}", "\u3000{{ z }}", "\u2028\u2029{%",
                      "\u00a0{{ kept inside }}", "\u0085{{ x }}", " \r\n\t{{ x }} \n"]


def test_whitespace_before_pebble_syntax_is_kept():
    for text in SYNTAX_AFTER_SPACE:
        assert _render("{% if true %}{% endif %}" + apt.static_text(text), {}) == text.encode("utf-8"), repr(text)


def test_text_at_the_start_of_a_template_is_not_doubled():
    for text in BREAKS + SYNTAX_AFTER_SPACE + ["\nplain", "<tr>\n"]:
        assert _render(apt.static_text(text, after_a_tag=False) + "{{ '' }}", {}) == text.encode("utf-8"), repr(text)


def test_pebble_syntax_and_a_final_brace_are_written_literally():
    for text in ["a {{ b }} c", "{% if x %}", "{# note #}", "ends {", "{", "{\n{", "x{{"]:
        source = "{{ '' }}" + apt.static_text(text) + "{% if true %}{% endif %}"
        assert _render(source, {}) == text.encode("utf-8"), repr(text)


def test_text_that_would_end_a_verbatim_block_is_refused():
    for text in ["{{ a }}{% endverbatim %}", "{% x %}{%endverbatim%}", "{{ y }}{%- endverbatim -%}"]:
        try:
            apt.static_text(text)
        except apt.Unrepresentable:
            continue
        raise AssertionError(f"accepted {text!r}")


# --- record shapes ---------------------------------------------------------------------

URL = "/a/pkg/Sample.html"
FILL = {"t": "Title", "v": "21", "lv": "1.0.0", "id": "run(int)", "id2": "pubmethods", "n": "run",
        "h": URL + "#run(int)", "ps": "(int times)", "c": "public final class X", "a": '<a name="run-int-"></a>',
        "w": "class", "cl": "responsive methods", "x": "0x00000007", "ty": "int", "d": "<p>Runs.\n</p>\n",
        "sig": "public void run (int times)"}


def sample(shape: str) -> str:
    parts = shape.split(apt.H)
    return "".join(FILL[p] if i % 2 else p for i, p in enumerate(parts))


def test_every_shape_reads_one_record_and_writes_it_back():
    converter = apt.Converter(apt.SHAPES)
    template = converter.template()
    for k, shape in enumerate(apt.SHAPES):
        text = sample(shape)
        segs = converter.segments(text, URL)
        assert [s.get("k") for s in segs] == [k], (k, segs)
        if "\0h\0" in shape:
            assert segs[0]["h#"] == "run(int)" and "h" not in segs[0], segs[0]
        assert _render(template, {"u": URL, "b": segs}) == text.encode("utf-8"), k


def test_a_link_elsewhere_is_stored_whole():
    converter = apt.Converter(apt.SHAPES)
    text = sample(apt.SHAPES[0]).replace(URL + "#run(int)", "/a/other/Page.html#run(int)")
    segs = converter.segments(text, URL)
    assert segs[0]["h"] == "/a/other/Page.html#run(int)" and "h#" not in segs[0]
    assert _render(converter.template(), {"u": URL, "b": segs}) == text.encode("utf-8")


def test_text_no_shape_matches_stays_raw_line_by_line():
    segs = apt.Converter(apt.SHAPES).segments("<p>one</p>\n<p>two</p>", URL)
    assert segs == [{"r": "<p>one</p>\n<p>two</p>"}]


def test_head_and_footer_become_shapes_only_when_enough_pages_share_them():
    pages = [doclava_page(f"C{i}") for i in range(apt.MIN_SHARED_PAGES)]
    shapes = apt.head_and_tail_shapes(pages)
    assert len(shapes) == 2 and "\0t\0" in shapes[0] and shapes[1] == TAIL
    assert apt.head_and_tail_shapes(pages[:-1]) == []


# --- includes ---------------------------------------------------------------------------

def test_rows_of_one_header_merge_into_one_order():
    found = [apt.find_blocks(doclava_page("A", doclava_block("java/lang/Object", ["clone", "equals", "toString"]))),
             apt.find_blocks(doclava_page("B", doclava_block("java/lang/Object", ["clone", "hashCode", "toString"])))]
    (key, order), = apt.plan_includes(found).items()
    names = [row.split("#", 1)[1].split("(", 1)[0] for row, _ in sorted(order.items(), key=lambda t: t[1])]
    assert names[0] == "clone" and names[-1] == "toString" and set(names) == {"clone", "equals", "hashCode",
                                                                               "toString"}


def test_a_header_whose_pages_disagree_on_order_gets_no_include():
    found = [apt.find_blocks(doclava_page("A", doclava_block("java/lang/Object", ["clone", "equals"]))),
             apt.find_blocks(doclava_page("B", doclava_block("java/lang/Object", ["equals", "clone"])))]
    assert apt.plan_includes(found) == {}


def test_a_header_whose_block_repeats_a_row_gets_no_include():
    found = [apt.find_blocks(doclava_page("A", doclava_block("java/lang/Object", ["clone", "clone"])))]
    assert apt.plan_includes(found) == {}


def test_a_block_without_its_footer_is_not_a_block():
    """A header and rows followed by anything but the style's own footer stay in the page, even when a whole
    block of the same style follows."""
    doclava, dackka = doclava_block("java/lang/Object", ["clone"]), dackka_block("pkg/Base", ["getA"])
    for block, foot in ((doclava, "</table>\n</div>\n</td></tr>\n"),
                        (dackka, "</tbody>\n</table>\n</div>\n</devsite-expandable> </td>\n</tr>\n")):
        assert len(apt.find_blocks(block)) == 1
        broken = block.replace(foot, "</table>\n</div>\n</td>\n</tr>\n")
        assert apt.find_blocks(broken) == []
        (start, end, _key, _rows), = apt.find_blocks(broken + block)
        assert (start, end) == (len(broken), len(broken) + len(block))


def test_an_include_starting_with_a_line_break_renders_it_once():
    """An include's first text follows no tag, so a leading line break must not be doubled."""
    source = apt.include_source("\n\n<head>" + apt.KEY_JOIN + "</foot>\n", {"<tr>row</tr>\n": 0})
    assert _render(source, {"x": []}) == b"\n\n<head><tr>row</tr>\n</foot>\n"
    assert _render(source, {"x": [0]}) == b"\n\n<head></foot>\n"


def test_an_include_holds_every_row_behind_its_guard():
    found = [apt.find_blocks(doclava_page("A"))]
    (key, order), = apt.plan_includes(found).items()
    source = apt.include_source(key, order)
    for i in range(len(OBJECT_ROWS)):
        assert f"{{% if x contains {i} %}}{{% else %}}" in source
    assert source.startswith(key.split(apt.KEY_JOIN)[0]) and source.endswith(key.split(apt.KEY_JOIN)[1])


# --- whole runs ---------------------------------------------------------------------------

def test_every_page_converts_and_serves_the_same_bytes(tmp_path):
    db = make_db(tmp_path / "doc.db")
    before = served(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    after = served(db)
    assert after.keys() == before.keys()
    for path, (data, _tid) in before.items():
        assert after[path][0] == data, path
    converted = {p for p, (_d, tid) in after.items() if tid and p.startswith("a/")}
    assert converted == set(standard_pages()), converted
    assert after["a/devsite/guide.html"][1] == 0 and after["k/other.html"][1] == 0
    t = templates(db)
    assert apt.MAIN_TEMPLATE in t and "page.peb" in t
    assert sorted(n for n in t if n.startswith(apt.INCLUDE_PREFIX)) == [apt.include_name(0), apt.include_name(1)]
    assert f'"{apt.INCLUDE_PREFIX}" ~ s.i ~ "{apt.INCLUDE_SUFFIX}"' in t[apt.MAIN_TEMPLATE]
    assert any(p.name.startswith("doc.db.backup-") for p in tmp_path.iterdir())
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    conn.close()


def guarded_rows(source: str) -> list:
    """The member name of each row of an include, in guard order."""
    out = []
    for part in source.split("{% if x contains ")[1:]:
        number, rest = part.split(" %}{% else %}", 1)
        assert int(number) == len(out)
        out.append(rest.split("{% endif %}", 1)[0].split("#", 1)[1].split("(", 1)[0])
    return out


def test_the_space_the_pages_gave_up_is_vacuumed(tmp_path):
    """Pages that lose a large inherited block leave free pages behind until the VACUUM."""
    import random
    rng = random.Random(3305)
    names = [f"m{i}x{rng.getrandbits(64):016x}" for i in range(400)]
    pages = {f"a/pkg/C{i}.html": doclava_page(f"C{i}", doclava_block("java/lang/Object", names)) for i in range(6)}
    db = make_db(tmp_path / "doc.db", pages)
    assert run_tool(db)[0] == 0
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    conn.close()


def test_a_page_stores_the_rows_its_class_overrides(tmp_path):
    db = make_db(tmp_path / "doc.db")
    assert run_tool(db)[0] == 0
    includes = lambda path: [s for s in stored_context(db, path)["b"] if "i" in s]  # noqa: E731
    (c0,), (c3,) = includes("a/pkg/C0.html"), includes("a/pkg/C3.html")
    assert c0["i"] == c3["i"] and "x" not in c0
    assert c3["x"] == [guarded_rows(templates(db)[apt.include_name(c3["i"])]).index("hashCode")]
    (d2,) = includes("a/x/D2.html")
    assert d2["x"] == [1]


def test_blocks_whose_pages_disagree_stay_in_the_pages(tmp_path):
    pages = standard_pages()
    pages["a/pkg/C1.html"] = doclava_page("C1", doclava_block("java/lang/Object", OBJECT_ROWS) +
                                          doclava_block("pkg/I", ["m1", "m2"], "interface"))
    pages["a/pkg/C2.html"] = doclava_page("C2", doclava_block("java/lang/Object", OBJECT_ROWS) +
                                          doclava_block("pkg/I", ["m2", "m1"], "interface"))
    db = make_db(tmp_path / "doc.db", pages)
    before = served(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    after = served(db)
    assert after["a/pkg/C1.html"][0] == before["a/pkg/C1.html"][0] and after["a/pkg/C1.html"][1]
    assert after["a/pkg/C2.html"][0] == before["a/pkg/C2.html"][0] and after["a/pkg/C2.html"][1]
    assert not any("pkg.I" in s for n, s in templates(db).items() if n.startswith(apt.INCLUDE_PREFIX))
    kept = [s for s in stored_context(db, "a/pkg/C1.html")["b"] if s.get("w") == "interface"]
    assert len(kept) == 1 and kept[0]["n"] == "pkg.I"  # the kept block's header, a record of its own


def test_text_that_would_end_a_verbatim_block_stays_in_the_pages(tmp_path):
    """A head shared by enough pages, and an inherited row, each holding text that would end a verbatim
    block: neither becomes template text, and the pages still convert and serve the same bytes."""
    odd_head = "<!-- {{ x }} {% endverbatim %} -->\n"
    pages = standard_pages()
    for i in range(6):
        rows = OBJECT_ROWS + (["odd"] if i == 0 else [])
        page = doclava_page(f"C{i}", doclava_block("java/lang/Object", rows))
        pages[f"a/pkg/C{i}.html"] = odd_head + page
    pages["a/pkg/C0.html"] = pages["a/pkg/C0.html"].replace("About odd, caf", "About odd {% endverbatim %}, caf")
    db = make_db(tmp_path / "doc.db", pages)
    before = served(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    after = served(db)
    for path in pages:
        assert after[path][0] == before[path][0] and after[path][1], path
    sources = templates(db)
    assert not any("endverbatim %} -->" in v or "About odd {%" in v for v in sources.values())
    assert not any("java.lang.Object" in v for n, v in sources.items() if n.startswith(apt.INCLUDE_PREFIX))


def test_a_database_without_a_pages_is_left_alone(tmp_path):
    db = make_db(tmp_path / "doc.db", pages={})
    digest = sha(db)
    code, out, err = run_tool(db)
    assert code == 0 and "Nothing to convert." in out
    assert sha(db) == digest


class Altering:
    """The real renderer, except that pages whose JSON holds `marker` come back with one byte more."""

    def __init__(self, real, marker: bytes):
        self.real, self.marker = real, marker

    def define(self, name, text):
        self.real.define(name, text)

    def render(self, name, context):
        ok, body, nanos = self.real.render(name, context)
        return (ok, body + b"!", nanos) if ok and self.marker in context else (ok, body, nanos)

    def close(self):
        pass


def test_a_page_that_does_not_render_back_stays_html_and_its_include_is_not_written(tmp_path):
    pages = standard_pages()
    pages["a/pkg/C5.html"] = doclava_page("C5", doclava_block("java/lang/Object", OBJECT_ROWS) +
                                          doclava_block("pkg/Only", ["solo"]))
    db = make_db(tmp_path / "doc.db", pages)
    conn = sqlite3.connect(db)
    c5 = conn.execute("SELECT content FROM Content WHERE path = 'a/pkg/C5.html'").fetchone()[0]
    conn.close()
    code, out, err = run_tool(db, renderer=Altering(_harness(), b'"/a/pkg/C5.html"'))
    assert code == 0, err
    assert "stays HTML: a/pkg/C5.html - renders differently from byte" in err
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT content, templateId FROM Content WHERE path = 'a/pkg/C5.html'").fetchone()
    assert row == (c5, 0)
    assert conn.execute("SELECT templateId FROM Content WHERE path = 'a/pkg/C4.html'").fetchone()[0] > 0
    conn.close()
    assert not any("pkg.Only" in s for s in templates(db).values())


class Failing:
    """The real renderer, except that pages whose JSON holds `marker` fail with an error other than the limit."""

    def __init__(self, real, marker: bytes):
        self.real, self.marker = real, marker

    def define(self, name, text):
        self.real.define(name, text)

    def render(self, name, context):
        if self.marker in context:
            return False, "PebbleException: boom (?:?)\n\tat Render.main", 0
        return self.real.render(name, context)

    def close(self):
        pass


def test_a_page_that_fails_to_render_stays_html_and_says_why(tmp_path):
    db = make_db(tmp_path / "doc.db")
    conn = sqlite3.connect(db)
    c5 = conn.execute("SELECT content FROM Content WHERE path = 'a/pkg/C5.html'").fetchone()[0]
    conn.close()
    code, out, err = run_tool(db, renderer=Failing(_harness(), b'"/a/pkg/C5.html"'))
    assert code == 0, err
    assert "stays HTML: a/pkg/C5.html - does not render: PebbleException: boom (?:?)\n" in err
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT content, templateId FROM Content WHERE path = 'a/pkg/C5.html'").fetchone() == (c5, 0)
    assert conn.execute("SELECT templateId FROM Content WHERE path = 'a/pkg/C4.html'").fetchone()[0] > 0
    conn.close()


def test_a_page_over_the_render_limit_stays_html(tmp_path):
    pages = standard_pages()
    pages["a/pkg/Huge.html"] = doclava_page("Huge", extra="<p>" + "x" * (4 * 1024 * 1024) + "</p>\n")
    db = make_db(tmp_path / "doc.db", pages)
    code, out, err = run_tool(db)
    assert code == 0, err
    assert "stays HTML: a/pkg/Huge.html - is over the app's render limit" in err
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT templateId FROM Content WHERE path = 'a/pkg/Huge.html'").fetchone()[0] == 0
    conn.close()


def test_rows_it_must_not_convert_are_left_and_named(tmp_path):
    db = make_db(tmp_path / "doc.db", extra={
        "a/pkg/Templated.html": (doclava_page("Templated").encode(), True),
        "a/pkg/Latin1.html": ("<p>café</p>".encode("latin-1"), True),
        "a/pkg/Nul.html": (b"<p>\x00</p>\n", True)}, template_ids={"a/pkg/Templated.html": 1})
    before = served(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    assert "left as it is: a/pkg/Templated.html - rendered through another template (templateId 1)" in err
    assert "left as it is: a/pkg/Latin1.html - is not UTF-8" in err
    assert "left as it is: a/pkg/Nul.html - holds a NUL character" in err
    conn = sqlite3.connect(db)
    left = dict(conn.execute("SELECT path, templateId FROM Content WHERE path IN "
                             "('a/pkg/Latin1.html', 'a/pkg/Nul.html', 'a/pkg/Templated.html', 'a/img.png')"))
    conn.close()
    assert left == {"a/pkg/Latin1.html": 0, "a/pkg/Nul.html": 0, "a/pkg/Templated.html": 1, "a/img.png": 0}
    after = served(db)
    assert after["a/img.png"] == before["a/img.png"]


def test_a_dry_run_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    digest = sha(db)
    code, out, err = run_tool(db, dry_run=True)
    assert code == 0, err
    assert "Would convert 12 of 12" in out and "Dry run: nothing written." in out
    assert sha(db) == digest and [p.name for p in tmp_path.iterdir()] == ["doc.db"]


def test_a_second_run_refuses_and_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    assert run_tool(db)[0] == 0
    digest = sha(db)
    backups = sorted(p.name for p in tmp_path.iterdir())
    code, out, err = run_tool(db)
    assert code == 1
    assert "already holds this step's templates" in err and "Nothing written." in err
    assert sha(db) == digest and sorted(p.name for p in tmp_path.iterdir()) == backups


def test_an_unreadable_row_refuses_and_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db", extra={"a/pkg/Broken.html": (b"not brotli at all", False)})
    digest = sha(db)
    code, out, err = run_tool(db)
    assert code == 1
    assert "a/pkg/Broken.html: could not be read" in err and "nothing was written" in err
    assert sha(db) == digest and [p.name for p in tmp_path.iterdir()] == ["doc.db"]


def test_a_missing_renderer_refuses_and_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    digest = sha(db)
    saved = apt.find_classpath

    def missing():
        raise RuntimeError("cannot find org.pebbletemplates:pebble:4.1.1")
    apt.find_classpath = missing
    try:
        code, out, err = run_tool(db, renderer=None)
    finally:
        apt.find_classpath = saved
    assert code == 1 and "cannot find org.pebbletemplates" in err and "nothing written" in err
    assert sha(db) == digest


def test_a_missing_database_refuses(tmp_path):
    code, out, err = run_tool(tmp_path / "absent.db")
    assert code == 1 and "does not exist" in err
    assert not (tmp_path / "absent.db").exists()


def add_versions(db: Path, versions: list) -> None:
    """DocumentationDatabaseVersion rows, inserted in the order given: (major, changeTime) each."""
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS DocumentationDatabaseVersion (major INT NOT NULL, minor INT NOT NULL, "
                 "patch INT NOT NULL, who TEXT NOT NULL, comment TEXT NOT NULL, "
                 "changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.executemany("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                     "VALUES (?, 0, 0, 'test', 'test', ?)", versions)
    conn.commit()
    conn.close()


def test_a_database_declaring_version_3_refuses_and_writes_nothing(tmp_path):
    """Its templates are compressed, and this step writes plain ones the app could not decode."""
    db = make_db(tmp_path / "doc.db")
    add_versions(db, [(2, "2026-08-20 22:52:54"), (3, "2026-10-19 07:09:52")])
    digest = sha(db)
    for dry_run in (False, True):
        code, out, err = run_tool(db, dry_run=dry_run, renderer=None)
        assert code == 1, (dry_run, out, err)
        assert "error: the database declares documentation version 3" in err and "Nothing written." in err
        assert "Found" not in out
        assert sha(db) == digest and [p.name for p in tmp_path.iterdir()] == ["doc.db"]


def test_the_newest_version_row_is_read_by_change_time_then_rowid(tmp_path):
    """Ordered as the app orders them: a later changeTime wins over a later rowid, and a tie goes to the
    later rowid. Rowid alone, changeTime alone, or a tie broken by the earlier rowid each reads another row."""
    cases = [
        ([(3, "2026-10-19 07:00:00"), (2, "2026-08-20 00:00:00")], 1),  # the older row is the newer change
        ([(2, "2026-10-19 07:00:00"), (3, "2026-08-20 00:00:00")], 0),
        ([(2, "2026-10-19 07:00:00"), (3, "2026-10-19 07:00:00")], 1),  # a tie: the later rowid
        ([(3, "2026-10-19 07:00:00"), (2, "2026-10-19 07:00:00")], 0),
    ]
    for i, (versions, expected) in enumerate(cases):
        db = make_db(tmp_path / f"doc{i}.db")
        add_versions(db, versions)
        code, out, err = run_tool(db, dry_run=True)
        assert code == expected, (versions, out, err)
        assert ("Would convert 12 of 12" in out) == (expected == 0), (versions, out, err)


def test_a_database_declaring_version_2_or_none_still_converts(tmp_path):
    """No version table, an empty one, and version 2 all hold plain templates."""
    for name, versions in (("none", None), ("empty", []), ("two", [(2, "2026-08-20 22:52:54")])):
        db = make_db(tmp_path / f"{name}.db")
        if versions is not None:
            add_versions(db, versions)
        code, out, err = run_tool(db)
        assert code == 0, (name, err)
        assert "Converting 12 of 12" in out, (name, out)
        assert apt.MAIN_TEMPLATE in templates(db), name


def test_a_chunked_page_is_read_whole_and_its_json_written_in_chunks(tmp_path):
    saved = optimize_db_media.CHUNK_SIZE, update_media_references.CHUNK_SIZE
    optimize_db_media.CHUNK_SIZE = update_media_references.CHUNK_SIZE = 64
    try:
        db = make_db(tmp_path / "doc.db")
        conn = sqlite3.connect(db)
        blob = conn.execute("SELECT content FROM Content WHERE path = 'a/pkg/C0.html'").fetchone()[0]
        assert len(blob) > 128 and len(blob) % 64
        conn.execute("UPDATE Content SET content = ? WHERE path = 'a/pkg/C0.html'", (blob[:64],))
        for n, start in enumerate(range(64, len(blob), 64), start=1):
            conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 12)",
                         (f"a/pkg/C0.html-{n}", blob[start:start + 64]))
        conn.commit()
        conn.close()
        before = served(db)
        code, out, err = run_tool(db)
        assert code == 0, err
        assert "stays HTML" not in err
        after = served(db)
        assert after["a/pkg/C0.html"][0] == before["a/pkg/C0.html"][0] and after["a/pkg/C0.html"][1]
        conn = sqlite3.connect(db)
        chunks = conn.execute("SELECT count(*), min(templateId) FROM Content WHERE path LIKE 'a/pkg/C0.html-%'"
                              ).fetchone()
        conn.close()
        assert chunks[0] >= 1 and chunks[1] > 0
    finally:
        optimize_db_media.CHUNK_SIZE, update_media_references.CHUNK_SIZE = saved


def test_a_failure_while_writing_rolls_everything_back(tmp_path):
    db = make_db(tmp_path / "doc.db")
    conn = sqlite3.connect(db)
    content = sorted(conn.execute("SELECT * FROM Content"))
    names = sorted(conn.execute("SELECT name FROM Templates"))
    conn.close()
    saved, calls = apt.replace_row, []

    def failing(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return saved(*args, **kwargs)
    apt.replace_row = failing
    try:
        try:
            run_tool(db)
        except RuntimeError as exc:
            assert "disk full" in str(exc)
        else:
            raise AssertionError("the failure was swallowed")
    finally:
        apt.replace_row = saved
    conn = sqlite3.connect(db)
    assert sorted(conn.execute("SELECT * FROM Content")) == content
    assert sorted(conn.execute("SELECT name FROM Templates")) == names
    conn.close()


def test_worker_processes_give_the_same_result(tmp_path):
    one, two = make_db(tmp_path / "one.db"), make_db(tmp_path / "two.db")
    assert run_tool(one)[0] == 0
    assert run_tool(two, workers=2)[0] == 0
    contexts = lambda db: {p: stored_context(db, p) for p in standard_pages()}  # noqa: E731
    assert contexts(one) == contexts(two)
    assert templates(one) == templates(two)


# --- shared texts --------------------------------------------------------------------

SHARED = "<p>Every one of these pages repeats this paragraph, word for word.</p>\n"
DESCRIPTION = "Returns the same long description on every one of these pages."


def notes(title: str = "Notes") -> str:
    """A section heading, which is a record: the text around it becomes raw segments of their own."""
    return f'<h2 class="api-section" data-text="{title}" id="{title.lower()}" tabindex="-1">{title}</h2>\n'


def shared_extra() -> str:
    """SHARED as a raw segment of its own, and DESCRIPTION in a returns row's description hole."""
    return (notes() + SHARED + notes() + '<table class="responsive">\n<tr><th colspan="2">Returns</th></tr>\n<tr>\n'
            f'<td><code dir="ltr" translate="no">int</code></td>\n<td width="100%">{DESCRIPTION}</td>\n</tr>\n'
            '</table>\n')


def shared_pages(holders: int, others: bool = True) -> dict:
    """The first `holders` Doclava pages carry shared_extra(); `others` adds the rest of standard_pages()."""
    pages = standard_pages() if others else {}
    for i in range(holders):
        pages[f"a/pkg/C{i}.html"] = doclava_page(f"C{i}", extra=shared_extra())
    return pages


def bundle_texts(db: Path) -> dict:
    """(bundle, index) -> text, read back out of the stored bundles (their texts hold no Pebble syntax)."""
    out = {}
    for name, source in templates(db).items():
        if not name.startswith(apt.TEXT_PREFIX):
            continue
        number = int(name[len(apt.TEXT_PREFIX):-len(apt.INCLUDE_SUFFIX)])
        assert source.endswith("{% endif %}")
        for part in source[:-len("{% endif %}")].split("{% ")[1:]:
            head, text = part.split(" %}", 1)
            out[(number, int(head.rsplit("==", 1)[1]))] = text
    return out


def refs(db: Path, path: str) -> dict:
    """The shared-text references a stored page holds: (bundle, index) -> field."""
    return {tuple(v): f[:-1] for seg in stored_context(db, path)["b"] for f, v in seg.items() if f.endswith("@")}


def test_texts_on_five_pages_are_interned_and_serve_the_same_bytes(tmp_path):
    db = make_db(tmp_path / "doc.db", shared_pages(5))
    before = served(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    after = served(db)
    for path, (data, _tid) in before.items():
        assert after[path][0] == data, path
    assert all(after[path][1] for path in standard_pages()), "a page stayed HTML"
    texts = bundle_texts(db)
    (shared,) = [k for k, v in texts.items() if v == SHARED]
    (description,) = [k for k, v in texts.items() if v == DESCRIPTION]
    for i in range(5):
        held = refs(db, f"a/pkg/C{i}.html")
        assert held[shared] == "r" and held[description] == "d", held
    assert shared not in refs(db, "a/pkg/C5.html") and description not in refs(db, "a/pkg/C5.html")
    # Only the holes that hold a shared text somewhere read one.
    fields = {f for path in standard_pages() for f in refs(db, path).values()}
    assert set(re.findall(r's\["(\w+)@"\]', templates(db)[apt.MAIN_TEMPLATE])) == fields
    assert {"r", "d"} <= fields and "ty" not in fields


def test_a_text_on_four_pages_stays_in_the_pages(tmp_path):
    db = make_db(tmp_path / "doc.db", shared_pages(4))
    assert run_tool(db)[0] == 0
    assert SHARED not in bundle_texts(db).values() and DESCRIPTION not in bundle_texts(db).values()
    segs = stored_context(db, "a/pkg/C0.html")["b"]
    assert {"r": SHARED} in segs and any(seg.get("d") == DESCRIPTION for seg in segs)


def contexts_sharing(texts: list, pages: int = 5) -> list:
    """`pages` contexts, each holding every one of `texts` as a raw segment."""
    return [{"u": f"/a/p{i}.html", "b": [{"r": text} for text in texts]} for i in range(pages)]


def with_bundle_size(size: int, function):
    saved = apt.INTERN_MAX_TEMPLATE
    apt.INTERN_MAX_TEMPLATE = size
    try:
        return function()
    finally:
        apt.INTERN_MAX_TEMPLATE = saved


def test_bundles_fill_up_to_the_bundle_size_and_no_further():
    # Lengths vary, so some text lands right at a bundle's limit.
    texts = [f"<p>Shared text number {i:03d}{'x' * (i % 13)}, long enough to intern.</p>\n" for i in range(40)]
    contexts = contexts_sharing(texts)
    sources, interned = with_bundle_size(400, lambda: apt.intern_texts(contexts))
    assert interned == {"r"}
    names = [apt.text_name(b) for b in range(len(sources))]
    assert sorted(sources) == sorted(names) and len(names) > 3
    for b, name in enumerate(names):
        size = len(sources[name].encode())
        assert size <= 400, (name, size)
        if b + 1 < len(names):  # the next bundle's first text would not have fitted in this one
            first = sources[names[b + 1]].removeprefix("{% if j == 0 %}").split("{% ", 1)[0]
            grown = size + len(f"{{% elseif j == {sources[name].count('{% elseif') + 1} %}}{first}".encode())
            assert grown > 400, (name, grown)
    # Each text has its own place, numbered in order of first use.
    places = [tuple(seg["r@"]) for seg in contexts[0]["b"]]
    assert places == sorted(places) and len(set(places)) == len(texts)
    assert all(c["b"] == contexts[0]["b"] for c in contexts)


def test_a_bundle_may_reach_the_bundle_size_exactly():
    # 15 bytes of "{% if j == 0 %}", 19 of "{% elseif j == 1 %}" and 11 of "{% endif %}": 355 bytes of text make 400.
    texts = ["<p>" + "a" * 193 + "</p>", "<p>" + "b" * 148 + "</p>"]
    sources, _interned = with_bundle_size(400, lambda: apt.intern_texts(contexts_sharing(texts)))
    assert list(sources) == [apt.text_name(0)] and len(sources[apt.text_name(0)].encode()) == 400


def test_only_the_holes_that_hold_a_shared_text_read_one():
    converter = apt.Converter(apt.SHAPES)
    assert "@" not in converter.template()
    some = converter.template(frozenset({"d"}))
    assert 's["d@"]' in some and 's["r@"]' not in some and 's["ty@"]' not in some
    assert 's["r@"]' in converter.template(frozenset({"r"}))


def contexts_using(texts_by_pages: list, pages: int) -> list:
    """`pages` contexts; each (texts, page numbers) pair puts those texts, as raw segments, on those pages."""
    out = [{"u": f"/a/p{i}.html", "b": []} for i in range(pages)]
    for texts, numbers in texts_by_pages:
        for i in numbers:
            out[i]["b"] += [{"r": t} for t in texts]
    return out


def bundle_of(contexts: list, page: int) -> set:
    return {seg["r@"][0] for seg in contexts[page]["b"] if "r@" in seg}


def test_texts_the_same_pages_use_share_a_bundle_and_others_get_their_own():
    a = [f"<p>Text {i} that the first five pages share.</p>\n" for i in range(3)]
    b = [f"<p>Text {i} that the next five pages share.</p>\n" for i in range(3)]
    contexts = contexts_using([(a, range(5)), (b, range(5, 10))], 10)
    sources, _ = apt.intern_texts(contexts)
    assert len(sources) == 2
    assert all(bundle_of(contexts, i) == {0} for i in range(5)) and all(bundle_of(contexts, i) == {1} for i in range(5, 10))
    assert [seg["r@"] for seg in contexts[0]["b"]] == [[0, 0], [0, 1], [0, 2]]  # in order of first use


def big(name: str, size: int) -> str:
    return f"<p>{name} " + "x" * (size - len(name) - 9) + "</p>\n"


def test_groups_merge_when_the_pages_both_serve_outweigh_the_bytes_wasted():
    """A (5,000 bytes, pages 0-4) and B (pages 0-5): merged, five pages save a bundle (5 x 8,000) and page 5 compiles
    A for nothing (5,000). With a bundle worth a tenth as much, they would stay apart."""
    a, b = big("A", 5000), "<p>B, on one page more than A.</p>\n"
    contexts = contexts_using([([a], range(5)), ([b], range(6))], 6)
    sources, _ = apt.intern_texts(contexts)
    assert len(sources) == 1 and bundle_of(contexts, 0) == {0}


def test_groups_stay_apart_when_the_bytes_wasted_outweigh_the_pages_both_serve():
    """A (5,000 bytes, pages 0-4) and B (pages 0-14): merged, five pages would save a bundle (40,000) and ten would
    compile A for nothing (50,000)."""
    a, b = big("A", 5000), "<p>B, on ten pages more than A.</p>\n"
    contexts = contexts_using([([a], range(5)), ([b], range(15))], 15)
    sources, _ = apt.intern_texts(contexts)
    assert len(sources) == 2 and bundle_of(contexts, 0) == {0, 1} and bundle_of(contexts, 10) == {1}


def tiny(name: str) -> str:
    return f"<p>{name}, a short text long enough to share.</p>\n"


def test_a_merge_that_would_overflow_a_bundle_is_refused():
    """Merged, B's ten texts move to indexes 10-19, one digit longer each: the bundle outgrows the two parts it was
    made of, so a cap of exactly their sum must refuse it."""
    a = [tiny(f"A{i}") for i in range(10)]
    b = [tiny(f"B{i}") for i in range(10)]
    size = lambda texts: len(apt.bundle_source(texts).encode())  # noqa: E731
    cap = size(a) + size(b)
    assert size(a + b) > cap
    contexts = contexts_using([(a, range(5)), (b, range(6))], 6)
    sources, _ = with_bundle_size(cap, lambda: apt.intern_texts(contexts))
    assert len(sources) == 2 and all(len(s.encode()) <= cap for s in sources.values())


def test_a_merged_bundle_keeps_its_texts_in_order_of_first_use():
    a1, b1, a2 = tiny("A1"), tiny("B1"), tiny("A2")
    contexts = [{"u": f"/a/p{i}.html", "b": [{"r": a1}, {"r": b1}, {"r": a2}] if i < 5 else [{"r": b1}]}
                for i in range(6)]
    sources, _ = apt.intern_texts(contexts)
    assert len(sources) == 1
    assert [seg["r@"] for seg in contexts[0]["b"]] == [[0, 0], [0, 1], [0, 2]]


def test_bundles_are_numbered_by_their_first_texts_use():
    """P's texts are first used at 0, 1, 3 and 4, Q's at 2; with room for two texts a bundle, P splits in two and Q's
    bundle comes between them."""
    p = [tiny(f"P{i}") for i in range(4)]
    q = [tiny("Q0")]
    contexts = [{"u": f"/a/p{i}.html", "b": [{"r": t} for t in (p[:2] + q + p[2:] if i < 5 else q)]} for i in range(6)]
    cap = len(apt.bundle_source(p[:2]).encode())
    sources, _ = with_bundle_size(cap, lambda: apt.intern_texts(contexts))
    assert [seg["r@"][0] for seg in contexts[0]["b"]] == [0, 0, 1, 2, 2]


def test_a_merge_offered_before_its_group_grew_is_not_taken():
    """A (pages 0-9) merges with B (pages 0-17) first. A and C (6,000 bytes, pages 0-4 and 18) were worth merging
    while A was alone; merged with A and B, C would cost thirteen pages 6,000 bytes each, so it stays apart."""
    a, b, c = tiny("A"), tiny("B"), big("C", 6000)
    contexts = contexts_using([([a], range(10)), ([b], range(18)), ([c], list(range(5)) + [18])], 19)
    sources, _ = apt.intern_texts(contexts)
    holding = lambda text: {name for name, source in sources.items() if text in source}  # noqa: E731
    assert len(sources) == 2 and holding(a) == holding(b) != holding(c)


def test_several_bundles_serve_the_same_bytes(tmp_path):
    extra = "".join(notes() + f"<p>Shared paragraph number {i:03d}, repeated on five pages.</p>\n" for i in range(30))
    pages = standard_pages()
    for i in range(5):
        pages[f"a/pkg/C{i}.html"] = doclava_page(f"C{i}", extra=extra + notes())
    db = make_db(tmp_path / "doc.db", pages)
    before = served(db)
    code, out, err = with_bundle_size(400, lambda: run_tool(db))
    assert code == 0, err
    assert sum(n.startswith(apt.TEXT_PREFIX) for n in templates(db)) > 3
    after = served(db)
    for path, (data, _tid) in before.items():
        assert after[path][0] == data, path
    assert all(after[path][1] for path in pages), "a page stayed HTML"
    assert all(refs(db, f"a/pkg/C{i}.html") for i in range(5))


def test_a_text_longer_than_a_bundle_stays_in_its_pages():
    long_text = "<p>" + "a long shared paragraph " * (apt.INTERN_MAX_TEMPLATE // 20) + "</p>\n"
    short_text = "<p>A short shared paragraph, long enough to intern.</p>\n"
    contexts = contexts_sharing([long_text, short_text])
    sources, _interned = apt.intern_texts(contexts)
    assert contexts[0]["b"] == [{"r": long_text}, {"r@": [0, 0]}]
    assert list(sources) == [apt.text_name(0)] and long_text not in sources[apt.text_name(0)]


def test_links_are_never_interned():
    """A same-page link is stored as its fragment, which often equals a member's id: the id may be shared, the
    link may not, or the page would lose its own URL."""
    fragment = "aVeryLongMemberName(android.content.Context,%20int)"
    link = "/a/android/view/SomeVeryLongClassName.html#member(int)"
    contexts = [{"u": f"/a/p{i}.html", "b": [{"k": 3, "id": fragment, "h#": fragment, "h": link}]} for i in range(5)]
    sources, interned = apt.intern_texts(contexts)
    assert interned == {"id"}
    assert contexts[0]["b"] == [{"k": 3, "h#": fragment, "h": link, "id@": [0, 0]}]


def test_a_database_holding_shared_text_bundles_refuses_and_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO Templates (name, content) VALUES (?, ?)", (apt.text_name(0), b"{% if j == 0 %}x{% endif %}"))
    conn.commit()
    conn.close()
    digest = sha(db)
    code, out, err = run_tool(db)
    assert code == 1 and "already holds this step's templates" in err and apt.text_name(0) in err
    assert sha(db) == digest and [p.name for p in tmp_path.iterdir()] == ["doc.db"]


def test_a_dry_run_with_shared_texts_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db", shared_pages(5))
    digest = sha(db)
    code, out, err = run_tool(db, dry_run=True)
    assert code == 0, err
    assert re.search(r"and [1-9][0-9]* shared-text bundle\(s\)", out) and "Dry run: nothing written." in out
    assert sha(db) == digest and [p.name for p in tmp_path.iterdir()] == ["doc.db"]


class AlteringAll(Altering):
    """Every page whose JSON holds one of `markers` comes back with one byte more."""

    def __init__(self, real, markers: list):
        super().__init__(real, b"")
        self.markers = markers

    def render(self, name, context):
        ok, body, nanos = self.real.render(name, context)
        return (ok, body + b"!", nanos) if ok and any(m in context for m in self.markers) else (ok, body, nanos)


def test_a_bundle_only_pages_that_stay_html_use_is_not_written(tmp_path):
    """Only the five holders share texts, and one more page, which shares none, converts. When every holder stays
    HTML, no bundle has a reader."""
    pages = {**shared_pages(5, others=False), "a/x/D0.html": dackka_page("D0")}
    db = make_db(tmp_path / "doc.db", pages)
    dry = run_tool(db, dry_run=True)
    assert "and 0 shared-text bundle(s)" not in dry[1], dry[1]
    markers = [f'"/a/pkg/C{i}.html"'.encode() for i in range(5)]
    code, out, err = run_tool(db, renderer=AlteringAll(_harness(), markers))
    assert code == 0, err
    assert err.count("stays HTML: a/pkg/C") == 5
    assert served(db)["a/x/D0.html"][1] and refs(db, "a/x/D0.html") == {}
    assert not any(n.startswith(apt.TEXT_PREFIX) for n in templates(db))


def _main() -> int:
    """Runs the tests without pytest, so this file works the way the tools
    beside it do (`uv run scripts/test_android_pages_to_templates.py`)."""
    failures, skipped = [], []
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        with tempfile.TemporaryDirectory() as directory:
            needs_tmp = "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]
            try:
                function(Path(directory)) if needs_tmp else function()
            except Skip as exc:
                skipped.append(name)
                print(f"skip {name}: {exc}")
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - a failure is the result here
                if type(exc).__name__ == "Skipped":  # pytest.skip, when pytest is installed
                    skipped.append(name)
                    print(f"skip {name}: {exc}")
                    continue
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): {type(exc).__name__}: {exc}")
                print(f"FAIL {name}", file=sys.stderr)
            else:
                print(f"ok   {name}")
    print()
    print(f"{len(failures)} failure(s), {len(skipped)} skipped" if failures or skipped else "all tests passed")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
