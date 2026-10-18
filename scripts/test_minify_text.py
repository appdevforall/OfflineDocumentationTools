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
test_minify_text.py

Covers minify_text.py: that each kind of text row loses only what a browser
would not show (comments, and whitespace it collapses anyway), that nothing
changes where whitespace is preserved or where a value could hold it, that the
HTML templates keep what Pebble renders, that rows it does not change keep
their stored bytes, and that a run is all or nothing.

The template tests check rendering, not just bytes: _render() does what Pebble
does with the whitespace after a tag (drops one newline) and what the browser
does with the rest (collapses each run), so a minified template must render to
the same text as the original. Stored rows are compressed at a quality the tool
never uses, so a row it re-compressed cannot pass for one it left alone.

Run it either way - as a plain script:

    uv run scripts/test_minify_text.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_minify_text.py

The database tests need the `brotli` CLI on PATH, and the JS tests `terser`, as
the tool itself does.
"""
from __future__ import annotations

import contextlib
import io
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import minify_text as tool  # noqa: E402

CHUNK_SIZE = 1024 * 1024
NONE = tool.Preserving()

HTML_PAGE = (b'<!DOCTYPE html>\n<html>\n  <head>\n    <title>  A   page </title>\n'
             b'    <style>\n      p   { color: red }\n    </style>\n  </head>\n  <body>\n'
             b'    <!-- navigation -->\n    <p class="lead"   id="top" >  Hello,\n      world  </p>\n'
             b'    <pre>  code\n    indented </pre>\n  </body>\n</html>\n')
HTML_MINIFIED = (b'<!DOCTYPE html>\n<html>\n<head>\n<title>  A   page </title>\n'
                 b'<style>\n      p   { color: red }\n    </style>\n</head>\n<body>\n'
                 b'<p class="lead" id="top"> Hello,\nworld </p>\n'
                 b'<pre>  code\n    indented </pre>\n</body>\n</html>\n')
SVG_IMAGE = (b'<?xml version="1.0"?>\n<!-- drawn by hand -->\n<svg xmlns="http://www.w3.org/2000/svg"  '
             b'viewBox="0 0 10 10" >\n  <g>\n    <path d="M 1  1 L 9 9" />\n  </g>\n'
             b'  <text x="1">  two  spaces </text>\n</svg>\n')
SVG_MINIFIED = (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                b'<g><path d="M 1  1 L 9 9"/></g><text x="1">  two  spaces </text></svg>')
CSS_SHEET = b"/* layout */\n.sig ,\n.note {\n  white-space : pre ;\n  margin: 0/**/auto;\n}\n/*! license */\n"
CSS_MINIFIED = b".sig,.note{white-space : pre;margin: 0 auto}/*! license */"
JS_SCRIPT = b"/* helper */\nfunction  add ( a , b ) {\n  return a  +  b ; // sum\n}\n/*! keep me */\n"


def _compress(raw: bytes, dictionary: bytes = None) -> bytes:
    """Stored bytes for a brotli row, at quality 5: the tool re-compresses at the
    default (11), so a row it touched comes out different from one it did not."""
    if dictionary is None:
        return brotli.compress(raw, quality=5)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-q", "5", "-c"], input=raw,
                              stdout=subprocess.PIPE, check=True).stdout


def _decompress(blob: bytes, dictionary: bytes = None) -> bytes:
    """Decoded independently of the tool's own codec."""
    if dictionary is None:
        return brotli.decompress(blob)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-d", "-c"], input=blob,
                              stdout=subprocess.PIPE, check=True).stdout


TYPES = {"text/html": (12, "brotli"), "image/svg+xml": (13, "brotli"), "text/css": (14, "brotli"),
         "text/javascript": (6, "brotli"), "text/plain": (5, "none")}


def _database(tmp_path: Path, rows: dict, dictionary: bytes = None, templates: dict = None) -> Path:
    """A miniature documentation database. `rows` maps path -> (content type,
    decoded bytes [, templateId [, stored as they are]]); `templates` maps
    id -> (name, body). Brotli rows are compressed against `dictionary` when one
    is given."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, "
        "compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, "
        "templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
        "CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "content BLOB NOT NULL, UNIQUE(name));"
    )
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)",
                     [(type_id, value, compression) for value, (type_id, compression) in TYPES.items()])
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary,))
    for path, (content_type, raw, *extra) in rows.items():
        template_id, as_is = (list(extra) + [0, False][len(extra):])[:2]
        type_id, compression = TYPES[content_type]
        stored = raw if as_is or compression == "none" else _compress(raw, dictionary)
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, ?)", (path, stored, type_id, template_id))
    for template_id, (name, body) in (templates or {}).items():
        conn.execute("INSERT INTO Templates (id, name, content) VALUES (?, ?, ?)", (template_id, name, body))
    conn.commit()
    conn.close()
    return db_path


def _stored(db_path: Path) -> dict:
    """{path: (languageID, content, contentTypeID, templateId)} for every row."""
    conn = sqlite3.connect(db_path)
    try:
        return {row[0]: row[1:] for row in conn.execute(
            "SELECT path, languageID, content, contentTypeID, templateId FROM Content")}
    finally:
        conn.close()


def _templates(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return {name: bytes(body) for name, body in conn.execute("SELECT name, content FROM Templates")}
    finally:
        conn.close()


def _run(db_path: Path, **overrides) -> tuple:
    """(exit code, stdout, stderr) of one run of the tool."""
    cfg = {"db_path": db_path, "dry_run": False, "workers": 2, "verbose": False}
    cfg.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.run(cfg)
    return code, out.getvalue(), err.getvalue()


def _skip(reason: str) -> None:
    try:
        import pytest
    except ImportError:
        raise _Skipped(reason) from None
    pytest.skip(reason)


class _Skipped(Exception):
    pass


def _needs(tool_name: str) -> None:
    if shutil.which(tool_name) is None:
        _skip(f"{tool_name} is not on PATH")


def _render(template: bytes, values: dict) -> bytes:
    """What a browser shows of a template rendered by Pebble, as far as whitespace
    goes: each tag replaced by its value, the one newline after it dropped, and
    every remaining run of whitespace collapsed to one space."""
    def tag(m):
        body = m.group(1).strip()
        return values.get(body, b"") if m.group(0).startswith(b"{{") else b""
    rendered = re.sub(rb"(?:\{\{(.*?)\}\}|\{%(.*?)%\}|\{#(.*?)#\})(?:\r\n|\n)?",
                      lambda m: tag(m) if m.group(1) is not None else b"", template, flags=re.S)
    return re.sub(rb"[ \t\n\r\f]+", b" ", rendered)


def test_text_whitespace_becomes_one_space_or_one_newline():
    assert tool.minify_html(b"<p>  a \t b\n\n   c </p>", NONE) == b"<p> a b\nc </p>"


def test_comments_go_but_ie_conditional_comments_stay():
    page = b"a<!-- one -->b <!--> c <!---> d<!--[if IE]><b>old</b><![endif]--> e <!-- unclosed"
    assert tool.minify_html(page, NONE) == b"ab c d<!--[if IE]><b>old</b><![endif]--> e "


def test_a_comment_dropped_between_two_runs_leaves_one():
    assert tool.minify_html(b"a <!-- x -->\n b", NONE) == b"a b"


def test_comments_dropped_one_after_another_leave_one_run():
    assert tool.minify_html(b"x <!-- c -->\n<!-- d -->\ny", NONE) == b"x y"
    assert tool.minify_html(b"a <!--1--> <!--2--> <!--3--> b", NONE) == b"a b"


def test_tags_are_read_as_the_html_tokenizer_reads_them():
    # A quote opens a value only after "="; inside an unquoted value it is a character like any other.
    assert tool.minify_html(b'<a href=x"y   title=q>t</a>', NONE) == b'<a href=x"y title=q>t</a>'
    page = b"<img alt=Bob's>  x  <pre> a  b </pre>  <i title='it'>y</i>"
    assert tool.minify_html(page, NONE) == b"<img alt=Bob's> x <pre> a  b </pre> <i title='it'>y</i>"
    # A quoted value runs to its closing quote, past any ">" in it.
    assert tool.minify_html(b'<a title="1 > 0   ok">  t  </a>', NONE) == b'<a title="1 > 0   ok"> t </a>'
    # Values and names that run into each other stay as they are.
    assert tool.minify_html(b'<a b="1"c=\'2\'   d>t</a>', NONE) == b'<a b="1"c=\'2\' d>t</a>'


def test_a_page_that_ends_inside_a_tag_keeps_the_rest_as_it_is():
    assert tool.minify_html(b'<p>  a  </p><a title="never   closed', NONE) == b'<p> a </p><a title="never   closed'
    assert tool.minify_html(b"<p>  a  </p><div   class=x", NONE) == b"<p> a </p><div   class=x"


def test_odd_markup_stays_as_it_is():
    page = b"a < b  and  </ c > <!DOCTYPE  x> <?pi  x?> </>"
    assert tool.minify_html(page, NONE) == b"a < b and </ c > <!DOCTYPE  x> <?pi  x?> </>"


def test_hard_pages_minify_to_a_fixed_point():
    for page in (b"x <!-- c -->\n<!-- d -->\ny", b"<img alt=Bob's>  x  <pre> a </pre>", b'<a href=x"y  >  t',
                 b"<p>  <!--a-->  <!--b-->  </p>", HTML_PAGE):
        once = tool.minify_html(page, NONE)
        assert tool.minify_html(once, NONE) == once, page


def test_preserving_and_raw_text_elements_keep_their_whitespace():
    for tag in (b"pre", b"listing", b"textarea", b"script", b"style", b"title", b"xmp", b"noscript"):
        page = b"<" + tag + b">  a\n   b  </" + tag + b">  x"
        assert tool.minify_html(page, NONE) == b"<" + tag + b">  a\n   b  </" + tag + b"> x", tag


def test_a_pre_inside_a_pre_ends_at_its_own_end_tag():
    page = b"<pre> a <pre>  b </pre>  c </pre>  d"
    assert tool.minify_html(page, NONE) == b"<pre> a <pre>  b </pre>  c </pre> d"


def test_attribute_values_are_never_touched():
    page = b'<input value="  two  spaces " title=\'a\n  b\'>'
    assert tool.minify_html(page, NONE) == page


def test_runs_between_attributes_become_one_space():
    assert tool.minify_html(b'<a\n   href="x"   class=y >t</a  >', NONE) == b'<a href="x" class=y>t</a>'
    # An unquoted value would take a slash that touched it.
    assert tool.minify_html(b"<a href=x  />", NONE) == b"<a href=x />"


def test_a_stylesheet_that_preserves_whitespace_keeps_what_it_names():
    p = tool.Preserving()
    p.add_css(b".sig { white-space: pre-wrap } #out{white-space:pre} code.block{ white-space : pre-line }"
              b" .box > * { white-space: break-spaces } kbd { white-space-collapse: preserve }")
    for page in (b'<span class="a sig">  x  </span>', b'<div id="out">  x  </div>',
                 b'<div class="block">  x  </div>', b'<div class="box">  x  </div>', b"<kbd>  x  </kbd>"):
        assert tool.minify_html(page, p) == page, page
    assert tool.minify_html(b'<div class="other">  x  </div>', p) == b'<div class="other"> x </div>'


def test_a_rule_keeps_the_outermost_element_it_names():
    p = tool.Preserving()
    p.add_css(b".outer .inner > em { white-space: pre } body .code { white-space: pre }")
    assert p.classes == {b"outer", b"code"} and not p.whole
    page = b'<div class="outer"> a  <span class="inner">  b  </span> </div>  <p> c  d </p>'
    assert tool.minify_html(page, p) == b'<div class="outer"> a  <span class="inner">  b  </span> </div> <p> c d </p>'


def test_a_sibling_rule_keeps_the_sibling_not_the_element_before_it():
    p = tool.Preserving()
    p.add_css(b".a + .b { white-space: pre } .c ~ p span { white-space: pre-wrap }")
    assert p.classes == {b"b"} and p.tags == {b"p"}


def test_a_nested_rule_is_read_with_its_parent():
    p = tool.Preserving()
    p.add_css(b".layer { color: red; &.on { x: y } :is(span, br) { white-space: pre } }"
              b" .pop, .side .rich { p { m: 0 } > * { white-space: pre-wrap } }"
              b" @media screen { .m { white-space: pre-line } }")
    assert p.classes == {b"layer", b"pop", b"side", b"m"} and not p.whole


def test_names_inside_is_count_when_nothing_else_is_named():
    p = tool.Preserving()
    p.add_css(b":is(span, .q) { white-space: pre } :where(#r) > em { white-space: pre }")
    assert p.tags == {b"span"} and p.classes == {b"q"} and p.ids == {b"r"} and not p.whole


def test_rules_inside_keyframes_and_font_face_preserve_nothing():
    p = tool.Preserving()
    p.add_css(b"@keyframes k { 50% { white-space: pre } } @font-face { white-space: pre }")
    assert not (p.tags or p.classes or p.ids or p.whole)


def test_a_rule_for_a_pseudo_element_preserves_nothing():
    p = tool.Preserving()
    p.add_css(b"ul.see li:not(:last-child)::after, li:before { white-space: pre }")
    assert tool.minify_html(b"<li>  x  </li>", p) == b"<li> x </li>"


def test_a_rule_for_the_whole_document_leaves_the_page_alone():
    for css in (b"body { white-space: pre }", b"* { white-space: pre }", b":root{white-space:pre-wrap}"):
        p = tool.Preserving()
        p.add_css(css)
        assert tool.minify_html(b"<p>  x  </p>", p) == b"<p>  x  </p>", css


def test_a_page_s_own_style_block_and_style_attribute_preserve_whitespace():
    page = b"<style>.k{white-space:pre}</style><p class=k>  x  </p><p style='white-space: pre'>  y  </p>"
    assert tool.minify_html(page, NONE) == page


def test_a_nested_element_of_the_same_name_does_not_end_a_preserved_one():
    p = tool.Preserving()
    p.add_css(b".k { white-space: pre }")
    page = b'<div class="k"> a <div> b </div>  c </div>  d'
    assert tool.minify_html(page, p) == b'<div class="k"> a <div> b </div>  c </div> d'


def test_an_end_tag_inside_a_script_does_not_end_a_preserved_element():
    p = tool.Preserving()
    p.add_css(b".k { white-space: pre }")
    page = b'<div class="k"> a <script>s = "</div>";</script>  b  </div>  c'
    assert tool.minify_html(page, p) == b'<div class="k"> a <script>s = "</div>";</script>  b  </div> c'


def test_an_unclosed_preserved_element_keeps_the_rest_of_the_page():
    p = tool.Preserving()
    p.add_css(b".k { white-space: pre }")
    page = b'<div class="k"> a  b'
    assert tool.minify_html(page, p) == page


def test_minifying_twice_changes_nothing_more():
    assert tool.minify_html(HTML_MINIFIED, NONE) == HTML_MINIFIED
    assert tool.minify_svg(SVG_MINIFIED) == SVG_MINIFIED
    assert tool.minify_css(CSS_MINIFIED) == CSS_MINIFIED


def test_a_page_is_minified_as_a_whole():
    assert tool.minify_html(HTML_PAGE, NONE) == HTML_MINIFIED


def test_template_tags_stay_whole():
    template = b'<p>{{ "}}  " }}  {% if a  >  b %}  x  {% endif %}{# a  b #}</p>'
    out = tool.minify_html(template, NONE, template=True)
    assert b'{{ "}}  " }}' in out and b"{% if a  >  b %}" in out and b"{# a  b #}" in out
    assert out == b'<p>{{ "}}  " }} {% if a  >  b %} x {% endif %}{# a  b #}</p>'


def test_a_template_renders_the_same_after_it_is_minified():
    values = {b"x": b"X", b"y": b"Y"}
    templates = [
        b"<p>{{ x }}\n    <b>{{ y }}</b>{% if a %}  \n  <i>z</i>{% endif %}\n\n<u>w</u></p>",
        b"<p>a{{ x }}\nb {{ y }}\r\n  c{% if a %}\n{% endif %}\nd</p>",
        b"<p>{{ x }} \n {{ y }}\n\n\n e</p>",
    ]
    for template in templates:
        out = tool.minify_html(template, NONE, template=True)
        assert _render(out, values) == _render(template, values), (template, out)
        assert len(out) < len(template), template


def test_a_template_comment_holding_a_tag_stays():
    template = b"<p><!-- {% if a %} --> x <!-- plain --> y <!-- {% endif %} --></p>"
    assert tool.minify_html(template, NONE, template=True) == \
        b"<p><!-- {% if a %} --> x y <!-- {% endif %} --></p>"


def test_an_element_whose_attributes_a_template_fills_in_keeps_its_whitespace():
    for template in (b'<div class="{{ c }}">  x  </div>', b"<div {{ attrs }}>  x  </div>"):
        assert tool.minify_html(template, NONE, template=True) == template, template


def test_svg_loses_comments_and_whitespace_only_text():
    assert tool.minify_svg(SVG_IMAGE) == SVG_MINIFIED


def test_svg_text_elements_and_preserved_space_keep_their_whitespace():
    # Inside these, whitespace between two child elements is text that shows.
    for inner in (b"<text>  <a>x</a> <a>y</a> </text>", b"<tspan> <a>x</a> <a>y</a> </tspan>",
                  b"<textPath> <a>x</a> <a>y</a> </textPath>", b"<title> <a/> b </title>", b"<desc> <a/> </desc>",
                  b"<style> .a { } </style>", b"<script> var a; </script>",
                  b"<foreignObject> <p> a  b </p> </foreignObject>", b'<g xml:space="preserve"> <t/> </g>'):
        svg = b"<svg>" + inner + b"</svg>"
        assert tool.minify_svg(svg) == svg, inner


def test_an_svg_doctype_with_an_internal_subset_stays_whole():
    svg = b'<!DOCTYPE svg [ <!ENTITY a "b > c"> ]>\n<svg>\n</svg>'
    assert tool.minify_svg(svg) == b'<!DOCTYPE svg [ <!ENTITY a "b > c"> ]><svg></svg>'


def test_css_loses_comments_and_whitespace():
    assert tool.minify_css(CSS_SHEET) == CSS_MINIFIED


def test_css_keeps_strings_urls_escapes_and_spaces_that_matter():
    css = (b"@media screen and ( max-width: 9px ) { .a\\ b { content: '  x  ' ; "
           b'background: url( a.png ) ; font: 1px "A  B" } }\n.c > .d + .e { x: calc(1px + 2px) !important }')
    assert tool.minify_css(css) == (
        b"@media screen and (max-width: 9px){.a\\ b{content: '  x  ';background: url( a.png );"
        b'font: 1px "A  B"}}.c > .d + .e{x: calc(1px + 2px) !important}')


def test_js_keeps_license_and_pipeline_comments():
    _needs("terser")
    out = tool.minify_js(JS_SCRIPT + b"/* Written by a step (ADFA-1234). */\n", tool.find_terser())
    assert b"helper" not in out and b"// sum" not in out
    assert b"/*! keep me */" in out and b"ADFA-1234" in out
    assert b"function add(a,b){return a+b}" in out


def test_js_reads_a_module_and_refuses_what_it_cannot_parse():
    _needs("terser")
    terser = tool.find_terser()
    assert tool.minify_js(b"export const  a = 1;\n", terser) == b"export const a=1;\n"
    # Top-level await parses only as a module.
    assert tool.minify_js(b"await  fetch( 'x' );\n", terser) == b'await fetch("x");\n'
    assert tool.minify_js(b"function (( {", terser) is None


def test_rows_are_minified_and_the_rest_keep_their_stored_bytes(tmp_path: Path):
    _needs("brotli")
    _needs("terser")
    dictionary = HTML_PAGE * 3 + SVG_IMAGE + CSS_SHEET
    db = _database(tmp_path, {
        "a/page.html": ("text/html", HTML_PAGE),
        "a/image.svg": ("image/svg+xml", SVG_IMAGE),
        "a/style.css": ("text/css", CSS_SHEET),
        "a/script.js": ("text/javascript", JS_SCRIPT),
        "k/topic.html": ("text/html", b'{"title":  "kept  as  it  is"}', 3),
        "a/notes.txt": ("text/plain", b"  plain   text  "),
        "a/small.html": ("text/html", b"<p>x</p>"),
    }, dictionary)
    before = _stored(db)
    code, out, err = _run(db)
    after = _stored(db)

    assert code == 0, err
    assert _decompress(after["a/page.html"][1], dictionary) == HTML_MINIFIED
    assert _decompress(after["a/image.svg"][1], dictionary) == SVG_MINIFIED
    assert _decompress(after["a/style.css"][1], dictionary) == CSS_MINIFIED
    assert _decompress(after["a/script.js"][1], dictionary).startswith(b"function add(a,b){return a+b}")
    for path in ("k/topic.html", "a/notes.txt", "a/small.html"):
        assert after[path] == before[path], path
    for path in after:
        assert after[path][0] == before[path][0] and after[path][2:] == before[path][2:], path
    assert "changed 4 row(s)" in out


def test_a_stylesheet_in_the_database_preserves_whitespace_in_every_page(tmp_path: Path):
    _needs("brotli")
    page = b'<div class="member"> a  <span class="sig">  keep   this </span> </div>  <p> b  c </p>'
    db = _database(tmp_path, {"j/page.html": ("text/html", page),
                              "j/style.css": ("text/css", b".member .sig { white-space: pre }")})
    code, _out, err = _run(db)
    assert code == 0, err
    assert _decompress(_stored(db)["j/page.html"][1]) == \
        b'<div class="member"> a  <span class="sig">  keep   this </span> </div> <p> b c </p>'


def test_only_templates_that_html_rows_use_are_minified(tmp_path: Path):
    _needs("brotli")
    body = b"<p>\n    {{ x }}\n    <b> y </b>\n</p>"
    db = _database(tmp_path, {
        "k/a.html": ("text/html", b'{"x": 1}', 1),
        "k/b.html": ("text/html", b'{"x": 2}', 1),
        "i/a.js": ("text/javascript", b'{"x": 3}', 2),
    }, templates={1: ("page.peb", body), 2: ("script.peb", body), 3: ("unused.peb", body)})
    code, out, err = _run(db)
    templates = _templates(db)
    assert code == 0, err
    assert templates["page.peb"] == b"<p>\n{{ x }}\n <b> y </b>\n</p>"
    assert templates["script.peb"] == body and templates["unused.peb"] == body
    assert "script.peb left as it is: used by text/javascript rows" in out
    assert "unused.peb left as it is: no row names it" in out


def test_a_second_run_changes_nothing(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"a/page.html": ("text/html", HTML_PAGE), "a/style.css": ("text/css", CSS_SHEET)},
                   templates={})
    assert _run(db)[0] == 0
    once = _stored(db)
    code, out, _err = _run(db)
    assert code == 0 and "changed 0 row(s) and 0 template(s)" in out
    assert _stored(db) == once


def test_dry_run_writes_nothing(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"a/page.html": ("text/html", HTML_PAGE)})
    before = _stored(db)
    code, out, _err = _run(db, dry_run=True)
    assert code == 0 and "would change 1 row(s)" in out
    assert _stored(db) == before
    assert not list(tmp_path.glob("*.bak*")) and len(list(tmp_path.iterdir())) == 1


def test_the_backup_holds_the_database_as_it_was(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"a/page.html": ("text/html", HTML_PAGE)})
    before = _stored(db)
    code, out, _err = _run(db)
    backup = Path(re.search(r"Backup written to (.+)", out).group(1).strip())
    assert code == 0 and backup.is_file()
    assert _stored(backup) == before


def test_an_error_while_writing_rolls_everything_back(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"a/one.html": ("text/html", HTML_PAGE), "a/two.html": ("text/html", HTML_PAGE + b" ")},
                   templates={1: ("page.peb", b"<p>  {{ x }}  </p>")})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE Content SET templateId = 0")
    conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES "
                 "('k/t.html', 1, ?, 12, 1)", (_compress(b"{}"),))
    conn.commit()
    conn.close()
    before, templates_before = _stored(db), _templates(db)
    real = tool.replace_row
    calls = []

    def failing(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    tool.replace_row = failing
    try:
        try:
            _run(db)
        except RuntimeError:
            pass
        else:
            raise AssertionError("the failure did not propagate")
    finally:
        tool.replace_row = real
    assert _stored(db) == before and _templates(db) == templates_before


def test_a_chunked_row_is_read_and_written_whole(tmp_path: Path):
    _needs("brotli")
    # Random-looking text compresses poorly, so the stored row spans chunks.
    import random
    rng = random.Random(7)
    words = b" ".join(bytes(rng.choice(b"abcdefghijklmnopqrstuvwxyz") for _ in range(8)) for _ in range(260_000))
    page = b"<p>" + words.replace(b" ", b"   ") + b"</p>"
    stored = brotli.compress(page, quality=5)
    assert len(stored) > CHUNK_SIZE
    db = _database(tmp_path, {"a/big.html": ("text/html", stored[:CHUNK_SIZE], 0, True),
                              "a/big.html-1": ("text/html", stored[CHUNK_SIZE:], 0, True)})
    code, _out, err = _run(db)
    rows = _stored(db)
    assert code == 0, err
    joined = b"".join(rows[p][1] for p in sorted(rows, key=lambda p: (len(p), p)))
    assert _decompress(joined) == b"<p>" + words + b"</p>"


def test_an_unreadable_row_is_reported_and_the_rest_still_change(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"a/page.html": ("text/html", HTML_PAGE),
                              "a/broken.html": ("text/html", b"not brotli at all", 0, True)})
    code, _out, err = _run(db)
    assert code == 1 and "skipped a/broken.html: could not be read" in err
    assert _decompress(_stored(db)["a/page.html"][1]) == HTML_MINIFIED


def test_without_terser_nothing_changes(tmp_path: Path):
    _needs("brotli")
    db = _database(tmp_path, {"a/page.html": ("text/html", HTML_PAGE), "a/script.js": ("text/javascript", JS_SCRIPT)})
    before = _stored(db)
    real = tool.find_terser
    tool.find_terser = lambda: None
    try:
        code, _out, err = _run(db)
    finally:
        tool.find_terser = real
    assert code == 1 and "terser is not installed" in err
    assert _stored(db) == before


def test_a_script_terser_cannot_parse_is_left_as_it_is(tmp_path: Path):
    _needs("brotli")
    _needs("terser")
    db = _database(tmp_path, {"a/bad.js": ("text/javascript", b"function (( {  ")})
    before = _stored(db)
    code, out, _err = _run(db)
    assert code == 0 and "left a/bad.js as it is: terser could not parse it" in out
    assert _stored(db) == before


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_minify_text.py`)."""
    failures = []
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        with tempfile.TemporaryDirectory() as directory:
            needs_tmp = "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]
            try:
                function(Path(directory)) if needs_tmp else function()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - a failure is the result here
                if type(exc).__name__ in ("Skipped", "_Skipped"):
                    print(f"skip {name}: {exc}")
                    continue
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): "
                                f"{type(exc).__name__}: {exc}")
                print(f"FAIL {name}", file=sys.stderr)
            else:
                print(f"ok   {name}")
    print()
    print(f"{len(failures)} failure(s)" if failures else "all tests passed")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
