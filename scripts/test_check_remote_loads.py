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
test_check_remote_loads.py

Covers check_remote_loads.py: that every kind of off-device load it names is
reported with its row, URL and shape and fails the run - among them the 13 an
independent review found passing the three ADFA-6589 steps - that links,
canonical links, comments and namespace URIs are not, that markup and CSS
html.parser or a regex would misread are read as a browser reads them, that
JSON pages, dictionary databases, chunked pages, templates and tooltips are
read, that a row which cannot be read or parsed fails the run, and that the
database is never written.

Run it either way - as a plain script, needing nothing installed:

    uv run scripts/test_check_remote_loads.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_check_remote_loads.py

The dictionary test needs the `brotli` CLI on PATH, as the tool itself does.
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_remote_loads as tool  # noqa: E402

CHUNK_SIZE = 1024 * 1024
TYPES = {"text/html": (12, "brotli"), "text/css": (1, "brotli"), "image/svg+xml": (2, "brotli"),
         "text/javascript": (6, "brotli"), "application/json": (8, "brotli"), "text/markdown": (4, "brotli"),
         "image/png": (3, "none"), "application/xhtml+xml": (40, "brotli")}

HEAD = b'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8"/>\n</head>\n<body>\n'
TAIL = b"\n</body>\n</html>\n"
YOUTUBE_EMBED = "https://www.youtube.com/embed/6-XSehwRgSY"

# The independent review's fixture: 13 loads that bundle_remote_images.py and
# remove_remote_loads.py both pass with exit 0. Each is (markup, shape, url);
# the last two are JSON page blocks.
REVIEW_SHAPES = [
    ('<img alt="" src="https://example.com/figure.png"/>', "<img src>", "https://example.com/figure.png"),
    ('<img alt="" src="https://developer.android.com/images/unlisted.png"/>', "<img src>",
     "https://developer.android.com/images/unlisted.png"),
    ('<img alt="" src="http://developer.android.com/images/axis_globe.png"/>', "<img src>",
     "http://developer.android.com/images/axis_globe.png"),
    ('<img alt="" src="//developer.android.com/images/axis_globe.png"/>', "<img src>",
     "//developer.android.com/images/axis_globe.png"),
    ('<img alt="" src="media/x.png" srcset="https://example.com/x-2x.png 2x"/>', "<img srcset>",
     "https://example.com/x-2x.png"),
    (f'<iframe src="{YOUTUBE_EMBED}"></iframe>', "<iframe src>", YOUTUBE_EMBED),
    ('<iframe src="https://www.youtube-nocookie.com/embed/6-XSehwRgSY"></iframe>', "<iframe src>",
     "https://www.youtube-nocookie.com/embed/6-XSehwRgSY"),
    ("<div style=\"background-image: url('https://example.com/bg.png')\">x</div>", "<div style> url()",
     "https://example.com/bg.png"),
    ('<video controls=""><source src="https://example.com/clip.mp4" type="video/mp4"/></video>', "<source src>",
     "https://example.com/clip.mp4"),
    ('<img alt="" data-src="https://example.com/lazy.png"/>', "<img data-src>", "https://example.com/lazy.png"),
    ('<link href="https://fonts.googleapis.com/css?family=Roboto" rel="stylesheet"/>',
     '<link rel="stylesheet" href>', "https://fonts.googleapis.com/css?family=Roboto"),
]
REVIEW_JSON_BLOCKS = [
    ({"type": "image", "src": "https://example.com/json-figure.png", "alt": "figure"},
     'JSON "src" of an image block', "https://example.com/json-figure.png"),
    ({"type": "html", "html": f'<iframe src="{YOUTUBE_EMBED}?start=5"></iframe>'},
     "JSON string <iframe src>", f"{YOUTUBE_EMBED}?start=5"),
]

# What must never be reported: links, canonical and alternate links, comments
# (the Help+Manual Google Fonts note among them), namespaces, a DOCTYPE, local
# addresses in every form, CSS comments, and scripts' strings.
NOT_LOADS = (
    b'<link href="https://www.helpandmanual.com/help/index.html" rel="canonical"/>\n'
    b'<link href="https://example.com/de/" hreflang="de" rel="alternate"/>\n'
    b'<link href="https://example.com/feed.xml" rel="alternate" type="application/rss+xml"/>\n'
    b'<a href="https://developer.android.com/reference">API reference</a>\n'
    b'<map name="m"><area alt="" href="https://example.com/area" shape="rect"/></map>\n'
    b'<!-- Insert Google web fonts references here.\r\n'
    b'<link href="https://fonts.googleapis.com/css?family=Roboto:300" rel="stylesheet">\r\n'
    b'<img src="https://example.com/in-a-comment.png"> -->\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    b'<use xlink:href="#icon"/><rect fill="url(#gradient)"/></svg>\n'
    b'<img alt="" src="media/local.png" srcset="media/local.png 1x, /a/devsite/media/local_2x.png 2x"/>\n'
    b'<img alt="" src="http://localhost:6174/a/x.png"/><img alt="" src="http://127.0.0.1:6174/a/x.png"/>\n'
    b'<img alt="" src="http://[::1]:6174/a/x.png"/><img alt="" src="data:image/png;base64,iVBORw0KGgo="/>\n'
    b'<iframe src="javascript:void(0)"></iframe><img alt="" src=""/><img alt="" src="mailto:a@example.com"/>\n'
    b'<style>/* url(https://example.com/commented-out.png) */ .a { background: url(media/a.png); }</style>\n'
    b'<script>var home = "https://www.helpandmanual.com/"; var ns = "http://www.w3.org/2000/svg";</script>\n'
    b'<p>See https://example.com/plain-text and url(https://example.com/in-text).</p>\n'
)


def _compress(raw: bytes, dictionary: bytes = None) -> bytes:
    if dictionary is None:
        return brotli.compress(raw, quality=5)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-q", "5", "-c"], input=raw,
                              stdout=subprocess.PIPE, check=True).stdout


def _json_page(page_id: str, blocks: list) -> bytes:
    """A page as populate_db.py stores it: no spaces, so markup quotes are \\"."""
    return json.dumps({"id": page_id, "sourceFile": f"topics/{page_id}.md", "title": "T", "blocks": blocks},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _database(tmp_path: Path, rows: dict, dictionary: bytes = None, types: dict = None, templates: dict = None,
              tooltips: list = None) -> Path:
    """A miniature documentation database. `rows` maps path -> (content type,
    decoded bytes), or (content type, bytes, True) to store the bytes as they
    are; `templates` maps name -> text; `tooltips` is [(tag, summary, detail)]."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, "
        "compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, "
        "templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
    )
    types = TYPES if types is None else types
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)",
                     [(type_id, value, compression) for value, (type_id, compression) in types.items()])
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary,))
    for path, (content_type, raw, *as_is) in rows.items():
        type_id, compression = types[content_type]
        stored = raw if as_is or compression == "none" else _compress(raw, dictionary)
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, 0)", (path, stored, type_id))
    if templates is not None:
        conn.execute("CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                     "content BLOB NOT NULL, UNIQUE('name'))")
        conn.executemany("INSERT INTO Templates (name, content) VALUES (?, ?)",
                         [(name, text.encode()) for name, text in templates.items()])
    if tooltips is not None:
        conn.execute("CREATE TABLE Tooltips (id INTEGER PRIMARY KEY AUTOINCREMENT, categoryId INTEGER NOT NULL, "
                     "tag TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL, UNIQUE (categoryId, tag))")
        conn.executemany("INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (1, ?, ?, ?)", tooltips)
    conn.commit()
    conn.close()
    return db_path


def _run(db_path: Path, **overrides) -> tuple:
    """(exit code, stdout, stderr) of one run of the tool."""
    cfg = {"db_path": db_path, "workers": 2, "verbose": False}
    cfg.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.run(cfg)
    return code, out.getvalue(), err.getvalue()


def _reported(err: str) -> list:
    """The lines naming a load, in order."""
    return [line for line in err.splitlines() if " loads " in line]


def _found(markup: str, reader: str = "markup") -> list:
    """[(shape, url)] counted as loads in one decoded row."""
    return [(shape, url) for shape, url, counted in tool.check_text(markup.encode(), reader) if counted]


def test_the_reviews_thirteen_shapes_are_each_reported(tmp_path: Path):
    """Each by row, shape and URL - as plain markup on an HTML page and as JSON
    blocks on a Kotlin page - and the run fails."""
    page = HEAD + b"\n".join(markup.encode() for markup, _shape, _url in REVIEW_SHAPES) + TAIL
    json_page = _json_page("k/html/odd", [block for block, _shape, _url in REVIEW_JSON_BLOCKS])
    db_path = _database(tmp_path, {"a/android/Odd.html": ("text/html", page),
                                   "k/html/odd.html": ("text/html", json_page)})
    code, out, err = _run(db_path)
    assert code == 1
    expected = ([f"  a/android/Odd.html: {shape} loads {url}" for _markup, shape, url in REVIEW_SHAPES]
                + [f"  k/html/odd.html: {shape} loads {url}" for _block, shape, url in REVIEW_JSON_BLOCKS])
    assert _reported(err) == expected
    assert "13 load(s) from off the device on 2 row(s), and 0 row(s) that could not be checked" in err
    assert "13 load(s) from off the device, by shape:" in out


def test_what_is_not_a_load_is_not_reported(tmp_path: Path):
    """Links, canonical and alternate links, everything in a comment, XML
    namespaces, local and non-http addresses, a CSS comment, a script's strings
    and URLs in text: exit 0 and nothing on stderr."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", HEAD + NOT_LOADS + TAIL)})
    code, out, err = _run(db_path)
    assert (code, err) == (0, "")
    assert "Nothing loads from off the device." in out
    assert _found(NOT_LOADS.decode()) == []


def test_links_are_tallied_but_not_counted_with_verbose(tmp_path: Path):
    db_path = _database(tmp_path, {"i/page.html": ("text/html", HEAD + NOT_LOADS + TAIL)})
    code, out, _err = _run(db_path, verbose=True)
    assert code == 0
    assert "  <a href>: 1" in out and "  <area href>: 1" in out
    assert '  <link rel="canonical" href>: 1' in out and '  <link rel="alternate" href>: 2' in out


def test_every_element_and_attribute_that_loads_is_reported():
    """Beyond the review's shapes: each attribute that makes a browser fetch,
    on the elements that use it."""
    cases = [
        ('<video poster="https://example.com/poster.jpg"></video>', "<video poster>"),
        ('<table background="https://example.com/bg.gif"></table>', "<table background>"),
        ('<object data="https://example.com/movie.swf"></object>', "<object data>"),
        ('<embed src="https://example.com/movie.swf"/>', "<embed src>"),
        ('<frameset><frame src="https://example.com/frame.html"/></frameset>', "<frame src>"),
        ('<script src="https://example.com/app.js"></script>', "<script src>"),
        ('<audio src="https://example.com/sound.ogg"></audio>', "<audio src>"),
        ('<video><track src="https://example.com/captions.vtt"/></video>', "<track src>"),
        ('<input src="https://example.com/button.png" type="image"/>', "<input src>"),
        ('<picture><source srcset="https://example.com/dark.png" media="(prefers-color-scheme: dark)"/></picture>',
         "<source srcset>"),
        ('<img alt="" data-srcset="https://example.com/lazy-2x.png 2x"/>', "<img data-srcset>"),
        ('<base href="https://example.com/docs/"/>', "<base href>"),
        ('<meta content="0; url=https://example.com/moved" http-equiv="refresh"/>',
         '<meta http-equiv="refresh">'),
        ('<a href="local.html" ping="https://example.com/track">x</a>', "<a ping>"),
        ('<form action="https://example.com/search"><input name="q"/></form>', "<form action>"),
        ('<button formaction="https://example.com/post">Go</button>', "<button formaction>"),
        ('<svg><image href="https://example.com/i.png"/></svg>', "<image href>"),
        ('<svg><use xlink:href="https://example.com/sprite.svg#icon"/></svg>', "<use xlink:href>"),
        ('<svg><filter><feImage href="https://example.com/f.png"/></filter></svg>', "<feimage href>"),
        ('<svg><script href="https://example.com/s.js"></script></svg>', "<script href>"),
        ('<svg><rect fill="url(https://example.com/p.svg#pattern)"/></svg>', "<rect fill> url()"),
        ('<svg><g clip-path="url(https://example.com/c.svg#clip)"/></svg>', "<g clip-path> url()"),
    ]
    for markup, shape in cases:
        found = _found(markup)
        assert len(found) == 1 and found[0][0] == shape and found[0][1].startswith("https://example.com/"), \
            (markup, found)


def test_every_data_src_and_data_srcset_attribute_is_reported():
    """The database's own sidebar.js fetches data-nav-src, and only data-src
    was counted. Any data-*src or data-*srcset counts now, failing safe."""
    assert _found('<aside id="sidebar" data-nav-src="https://example.com/nav.html"></aside>') == [
        ("<aside data-nav-src>", "https://example.com/nav.html")]
    assert _found('<img alt="" data-lazy-src="//example.com/a.png" data-lazy-srcset="https://example.com/b.png 2x"/>') \
        == [("<img data-lazy-src>", "//example.com/a.png"), ("<img data-lazy-srcset>", "https://example.com/b.png")]
    assert _found('<aside data-nav-src="/k/html/_nav.html" data-source="https://example.com/x"></aside>') == []


def test_markup_and_css_a_parser_misreads_are_read_as_a_browser_reads_them():
    """"<!-->" and "<!--->" are whole empty comments to a browser, but
    html.parser read them as opening one that ran to the next "-->". CSS
    decodes escapes in a URL, and a "/*" inside a CSS string opens no comment,
    while one in a comment does not start a string. Each hid a load."""
    for comment in ("<!-->", "<!--->"):
        assert _found(f'{comment}<img src="https://example.com/a.png">-->') == [
            ("<img src>", "https://example.com/a.png")], comment
    assert _found('<div style="background:url(htt\\70://example.com/b.png)">x</div>') == [
        ("<div style> url()", "http://example.com/b.png")]
    assert _found('<div style="background:url(http\\3a //example.com/c.png)">x</div>') == [
        ("<div style> url()", "http://example.com/c.png")]
    assert _found("@import '\\68ttps://example.com/d.css';", "css") == [("CSS @import", "https://example.com/d.css")]
    assert _found('<style>a{content:"/*"} b{background:url(http://example.com/e.png)} /* */</style>') == [
        ("<style> url()", "http://example.com/e.png")]
    assert _found('<style>/* "*/ b{background:url(https://example.com/f.png)} /* " */</style>') == [
        ("<style> url()", "https://example.com/f.png")]
    assert _found("<style>/\\2a b{background:url(https://example.com/g.png)} \\2a/</style>") == [
        ("<style> url()", "https://example.com/g.png")]


def test_a_repeated_attribute_is_read_as_a_browser_reads_it():
    """The first of two src attributes is the one a browser loads."""
    assert _found('<img src="https://example.com/first.png" src="media/second.png"/>') == [
        ("<img src>", "https://example.com/first.png")]
    assert _found('<img src="media/first.png" src="https://example.com/second.png"/>') == []


def test_a_style_block_never_closed_is_read_to_the_end():
    assert _found('<p>x</p><style>@import "https://example.com/unclosed.css";') == [
        ("<style> @import", "https://example.com/unclosed.css")]


def test_every_link_that_loads_is_reported_and_no_other():
    """A <link> counts unless every word of its rel loads nothing; a rel this
    tool does not know counts, failing safe."""
    loading = ["stylesheet", "icon", "shortcut icon", "preload", "prefetch", "preconnect", "dns-prefetch",
               "manifest", "modulepreload", "alternate stylesheet", "search", "something-new"]
    for rel in loading:
        assert _found(f'<link href="https://example.com/x" rel="{rel}"/>') == [
            (f'<link rel="{rel}" href>', "https://example.com/x")], rel
    for rel in ("canonical", "alternate", "author license", "next", "prev", "help", ""):
        assert _found(f'<link href="https://example.com/x" rel="{rel}"/>') == [], rel
    assert _found('<link imagesrcset="https://example.com/a.png 1x" href="local.png" rel="preload" as="image"/>') \
        == [("<link imagesrcset>", "https://example.com/a.png")]


def test_css_url_import_and_image_set_are_reported_wherever_css_is():
    """In a <style> block, a style attribute and a text/css row; with CSS
    comments skipped; and each address counted once, a quoted url() inside
    image-set() too."""
    css = ("/* url(https://example.com/no.png) */\n"
           '@import "https://example.com/a.css";\n'
           "@import url(https://example.com/b.css);\n"
           ".x { background: url( 'https://example.com/c.png' ); }\n"
           '.y { background-image: image-set("https://example.com/d.png" 1x, url("https://example.com/e.png") 2x); }\n'
           ".z { background: url(media/local.png); }\n")
    assert _found(css, "css") == [("CSS url()", "https://example.com/b.css"),
                                  ("CSS url()", "https://example.com/c.png"),
                                  ("CSS url()", "https://example.com/e.png"),
                                  ("CSS @import", "https://example.com/a.css"),
                                  ("CSS image-set()", "https://example.com/d.png")]
    assert [shape for shape, _url in _found(f"<style>{css}</style>")] == [
        "<style> url()", "<style> url()", "<style> url()", "<style> @import", "<style> image-set()"]
    assert _found('<p style="background:url(https://example.com/1.png), url(//example.com/2.png)">x</p>') == [
        ("<p style> url()", "https://example.com/1.png"), ("<p style> url()", "//example.com/2.png")]


def test_markup_in_iframe_srcdoc_and_xml_stylesheets_are_read():
    srcdoc = '<iframe srcdoc="&lt;img src=&quot;https://example.com/in-srcdoc.png&quot;&gt;"></iframe>'
    assert _found(srcdoc) == [("<iframe srcdoc>: <img src>", "https://example.com/in-srcdoc.png")]
    svg = ('<?xml version="1.0"?>\n<?xml-stylesheet type="text/css" href="https://example.com/s.css"?>\n'
           '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
           '<svg xmlns="http://www.w3.org/2000/svg"><style>rect { fill: url(https://example.com/f.svg#g); }</style>'
           '</svg>')
    assert _found(svg, "xml") == [("<?xml-stylesheet href?>", "https://example.com/s.css"),
                                  ("<style> url()", "https://example.com/f.svg#g")]
    # In an HTML page a processing instruction is a bogus comment and loads nothing.
    assert _found('<?xml-stylesheet href="https://example.com/s.css"?>') == []


def test_every_spelling_of_an_off_device_address_is_recognised():
    """As a browser reads an address: entities decoded, tabs and line breaks
    dropped, backslashes read as slashes, any number of slashes after
    http(s):, the host after the last "@", any case."""
    remote = ["HTTPS://EXAMPLE.COM/a.png", "https:example.com/a.png", "https:/example.com/a.png",
              "https:\\\\example.com\\a.png", "\\\\example.com/a.png", "  https://example.com/a.png  ",
              "ht\ttps://example.com/a.png", "https://localhost@example.com/a.png", "https://127.0.0.1.example.com/",
              "https://[2001:db8::1]/a.png", "https://", "//"]
    for url in remote:
        assert tool.remote_url(url) == url.strip(), url
    local = ["media/a.png", "/a/devsite/media/a.png", "../a.png", "#top", "?q=1", "http://localhost/a.png",
             "https://LOCALHOST:6174/a.png", "http://127.1.2.3/a.png", "http://[::1]/a.png",
             "http://example.com@localhost/a.png", "data:image/png;base64,AAAA", "javascript:alert(1)",
             "about:blank", "file:///sdcard/a.png", "", None]
    for url in local:
        assert tool.remote_url(url) is None, url
    assert _found('<img src="&#104;ttps://example.com/entity.png">') == [
        ("<img src>", "https://example.com/entity.png")]


def test_a_srcset_is_one_load_however_many_remote_candidates():
    """And the first remote candidate is the one named, after any local one."""
    markup = ('<img src="media/a.png" srcset="media/a.png 1x, https://example.com/a-2x.png 2x,'
              'https://example.com/a-3x.png 3x"/>')
    assert _found(markup) == [("<img srcset>", "https://example.com/a-2x.png")]
    assert tool.srcset_urls("a.png 1x,b.png 2x, c.png, d.png 100w") == ["a.png", "b.png", "c.png", "d.png"]
    assert tool.srcset_urls("a.png,b.png") == ["a.png,b.png"]
    assert tool.srcset_urls("a.png 1x (x, y), b.png") == ["a.png", "b.png"]


def test_a_json_page_is_read_from_its_strings(tmp_path: Path):
    """Every "src" string and the markup in every string with a "<", unescaped
    from the stored JSON; a code block showing a remote <img> counts too, as
    the docstring says, failing safe. Local addresses and links do not."""
    blocks = [
        {"type": "paragraph", "html": 'See <a href="https://kotlinlang.org/">the site</a>.'},
        {"type": "image", "src": "images/local.png", "alt": "local"},
        {"type": "image", "src": "https://example.com/remote.png", "alt": "remote"},
        {"type": "table", "headers": ["A"], "rows": [['<img src="https://example.com/cell.png">']]},
        {"type": "code", "lang": "html", "code": '<img src="https://example.com/sample.png">'},
        {"type": "video", "src": "https://example.com/v.mp4"},
    ]
    page = _json_page("k/html/page", blocks)
    assert b'<img src=\\"https://example.com/cell.png\\">' in page
    db_path = _database(tmp_path, {"k/html/page.html": ("text/html", page)})
    code, _out, err = _run(db_path)
    assert code == 1
    assert _reported(err) == [
        '  k/html/page.html: JSON "src" of an image block loads https://example.com/remote.png',
        "  k/html/page.html: JSON string <img src> loads https://example.com/cell.png",
        "  k/html/page.html: JSON string <img src> loads https://example.com/sample.png",
        '  k/html/page.html: JSON "src" loads https://example.com/v.mp4',
    ]


def test_an_application_json_row_is_read_like_a_json_page():
    assert _found('{"name": "x", "icon": {"src": "https://example.com/i.png"}}', "json") == [
        ('JSON "src"', "https://example.com/i.png")]
    assert _found('{"version":"2.2"}', "json") == []


def test_svg_css_json_and_xhtml_rows_are_read_and_scripts_and_markdown_are_not(tmp_path: Path):
    """Scripts are decoded but not searched, and Markdown is not read, as the
    docstring explains."""
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
           b'<image xlink:href="https://example.com/embedded.png"/></svg>')
    xhtml = (b'<?xml version="1.0"?>\n<?xml-stylesheet href="https://example.com/x.css"?>\n'
             b'<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>')
    db_path = _database(tmp_path, {
        "a/devsite/media/figure.svg": ("image/svg+xml", svg),
        "i/css/skin.css": ("text/css", b"@import url('https://fonts.googleapis.com/css?family=Roboto');"),
        "i/js/app.js": ("text/javascript", b'document.write(\'<img src="https://example.com/js.png">\');'),
        "j/html/README.md": ("text/markdown", b"![logo](https://example.com/logo.png)"),
        "k/manifest.json": ("application/json", b'{"icons": [{"src": "https://example.com/icon.png"}]}'),
        "p/page.xhtml": ("application/xhtml+xml", xhtml),
    })
    code, _out, err = _run(db_path)
    assert code == 1
    assert _reported(err) == [
        "  a/devsite/media/figure.svg: <image xlink:href> loads https://example.com/embedded.png",
        "  i/css/skin.css: CSS url() loads https://fonts.googleapis.com/css?family=Roboto",
        '  k/manifest.json: JSON "src" loads https://example.com/icon.png',
        "  p/page.xhtml: <?xml-stylesheet href?> loads https://example.com/x.css",
    ]


def test_templates_and_tooltips_are_read(tmp_path: Path):
    """A template opening with a Pebble comment is markup, not a JSON page."""
    templates = {"page.peb": '{# a comment #}\n<link rel="stylesheet" href="https://example.com/t.css">\n{{ x }}',
                 "nav.peb": '{# nav #}\n<a href="https://example.com/">home</a>'}
    tooltips = [("android.widget.ImageView", "An image", '<img src="https://example.com/tooltip.png">'),
                ("kotlin.Int", "<b>Int</b>", ""),
                ("kotlin.Any", '<img src="https://example.com/summary.png"> Any', "The root.")]
    db_path = _database(tmp_path, {"i/page.html": ("text/html", HEAD + TAIL)}, templates=templates,
                        tooltips=tooltips)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _reported(err) == [
        '  Templates: page.peb: <link rel="stylesheet" href> loads https://example.com/t.css',
        "  Tooltips 1 (android.widget.ImageView) detail: <img src> loads https://example.com/tooltip.png",
        "  Tooltips 3 (kotlin.Any) summary: <img src> loads https://example.com/summary.png",
    ]
    assert "0 row(s) that could not be checked" in err


def test_a_tooltip_opening_with_a_brace_is_markup_not_a_json_page(tmp_path: Path):
    """A Kotlin lambda opens with "{", as a JSON page does; read as one, its
    tooltip failed the run as "does not parse as JSON"."""
    tooltips = [("kotlin.Function", "{ x -> x * 2 } is a lambda", "{ it }"),
                ("kotlin.run", '{ block } <img src="https://example.com/run.png">', "")]
    db_path = _database(tmp_path, {"i/page.html": ("text/html", HEAD + TAIL)}, tooltips=tooltips)
    code, _out, err = _run(db_path)
    assert code == 1, err
    assert _reported(err) == ["  Tooltips 2 (kotlin.run) summary: <img src> loads https://example.com/run.png"]
    assert "0 row(s) that could not be checked" in err, err


def test_a_dictionary_database(tmp_path: Path):
    """Rows are decoded against the shared dictionary, which plain Brotli
    cannot do."""
    page = HEAD + REVIEW_SHAPES[0][0].encode() + TAIL
    clean = HEAD + NOT_LOADS + TAIL
    dictionary = (page + clean) * 3
    db_path = _database(tmp_path, {"a/android/Odd.html": ("text/html", page), "i/page.html": ("text/html", clean)},
                        dictionary=dictionary)
    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT content FROM Content WHERE path = 'a/android/Odd.html'").fetchone()[0]
    conn.close()
    try:
        plain = brotli.decompress(stored)
    except brotli.error:
        plain = None
    assert plain != page
    code, _out, err = _run(db_path)
    assert code == 1
    assert _reported(err) == ["  a/android/Odd.html: <img src> loads https://example.com/figure.png"]


def test_a_chunked_page_is_read_whole(tmp_path: Path):
    """The load sits in the "-1" row; stored uncompressed so the sizes are
    exact."""
    filler = b"x" * (CHUNK_SIZE + 500 - len(HEAD + REVIEW_SHAPES[0][0].encode() + TAIL))
    page = HEAD + filler + REVIEW_SHAPES[0][0].encode() + TAIL
    types = dict(TYPES, **{"text/html": (12, "none")})
    db_path = _database(tmp_path, {"a/android/Big.html": ("text/html", page[:CHUNK_SIZE]),
                                   "a/android/Big.html-1": ("text/html", page[CHUNK_SIZE:])}, types=types)
    code, out, err = _run(db_path)
    assert code == 1
    assert _reported(err) == ["  a/android/Big.html: <img src> loads https://example.com/figure.png"]
    assert "1 chunk-continuation row(s) folded into their base" in out


def test_an_unreadable_row_fails_the_run(tmp_path: Path):
    """On its own, with nothing else to report - and beside a load, which is
    still reported."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", HEAD + TAIL),
                                   "i/broken.html": ("text/html", b"not brotli at all", True)})
    code, _out, err = _run(db_path)
    assert code == 1
    assert "  i/broken.html: could not be read:" in err
    assert "0 load(s) from off the device on 0 row(s), and 1 row(s) that could not be checked" in err
    other = tmp_path / "other"
    other.mkdir()
    db_path = _database(other, {"a/android/Odd.html": ("text/html", HEAD + REVIEW_SHAPES[0][0].encode() + TAIL),
                                "i/broken.js": ("text/javascript", b"not brotli either", True)})
    code, _out, err = _run(db_path)
    assert code == 1
    assert "  i/broken.js: could not be read:" in err
    assert _reported(err) == ["  a/android/Odd.html: <img src> loads https://example.com/figure.png"]


def test_a_json_page_that_does_not_parse_fails_the_run(tmp_path: Path):
    page = _json_page("k/html/broken", [{"type": "paragraph", "html": "x"}])[:-2]
    db_path = _database(tmp_path, {"k/html/broken.html": ("text/html", page)})
    code, _out, err = _run(db_path)
    assert code == 1
    assert "  k/html/broken.html: does not parse as JSON" in err


def test_a_clean_database_passes_and_is_not_written(tmp_path: Path):
    """Exit 0, nothing on stderr, and not a byte of the database changed: no
    backup, no journal."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", HEAD + NOT_LOADS + TAIL),
                                   "k/html/page.html": ("text/html", _json_page("k/html/page", []))})
    before = db_path.read_bytes()
    code, out, err = _run(db_path)
    assert (code, err) == (0, "")
    assert "Nothing loads from off the device." in out
    assert db_path.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["documentation.db"]


def test_a_database_that_does_not_exist_is_refused(tmp_path: Path):
    code, _out, err = _run(tmp_path / "missing.db")
    assert code == 1 and "does not exist" in err
    assert not (tmp_path / "missing.db").exists()


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_check_remote_loads.py`)."""
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
