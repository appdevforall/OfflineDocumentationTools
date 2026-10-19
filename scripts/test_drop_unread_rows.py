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
test_drop_unread_rows.py

Covers drop_unread_rows.py: that each group deletes its rows, their chunk rows
and nothing else; that scripting is turned off in the viewer, even once the
sandbox rows are gone; that tooltips are read in documentation schema 2 and 3,
and templates as plain text below documentation version 3 and decoded from it;
and that every check refuses the run, writing nothing, when a kept row,
template, tooltip or Bookshelf entry refers to a deleted row, when a row to
delete is not what its group expects, when a stored PDF needs a CMap or its
fonts cannot all be read, when the viewer cannot be edited, when the tooltip
tables are in neither shape, or when a kept row, a template, a row to delete or
a PDF cannot be read. Byte-identical media copies (one type and language,
chunks joined; not PDFs or text) are merged into the copy the most rows link
to, then the shortest, then the lowest id, with every link to a dropped copy -
in pages, templated rows' JSON and tooltip button uris - pointed at the kept
one, prefix and percent-encoding kept; a link the rewrite cannot reach, and a
JSON row that would stop parsing, refuse the run. A run is all or nothing, a
dry run writes nothing, and a second run changes nothing.

Run it either way - as a plain script:

    uv run scripts/test_drop_unread_rows.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_drop_unread_rows.py

The dictionary and template-version tests need the `brotli` CLI on PATH, as the
tool itself does.
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
import zlib
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drop_unread_rows as tool  # noqa: E402

CHUNK_SIZE = 1024 * 1024

TYPES = {"text/css": (1, "brotli"), "image/svg+xml": (2, "brotli"), "image/webp": (3, "none"),
         "image/png": (4, "none"), "video/webm": (5, "none"), "text/javascript": (6, "brotli"),
         "application/json": (8, "brotli"), "text/markdown": (9, "brotli"), "text/html": (12, "brotli"),
         "text": (13, "none"),
         "application/pdf": (14, "brotli"), "application/wasm": (19, "brotli"),
         "application/octet-stream": (27, "brotli")}

VIEWER_MJS = (b'const defaultOptions={enableScripting:{value:true,kind:OptionKind.VIEWER+OptionKind.PREFERENCE},'
              b'wasmUrl:{value:"../web/wasm/"}};defaultOptions.sandboxBundleSrc={value:"../build/pdf.sandbox.mjs"};'
              b'scriptingManager:AppOptions.get("enableScripting")&&pdfScriptingManager')
VIEWER_MJS_OFF = VIEWER_MJS.replace(b"enableScripting:{value:true", b"enableScripting:{value:false")
WORKER_MJS = (b'class JpxImage{_filename="openjpeg.wasm";_noWasmFilename="openjpeg_nowasm_fallback.js"}'
              b'class JBig2{_filename="jbig2.wasm";_noWasmFilename="jbig2_nowasm_fallback.js"}')
SANDBOX_MJS = b'import("./quickjs-eval.js");fetch("../web/wasm/quickjs-eval.wasm")'
CLASS_PAGE = b'<html><head><title>B</title></head><body><a href="../../m/a/B.html">B</a></body></html>'
USE_PAGE = (b'<html><head><title>Uses of Class a.B</title></head><body><a href="../B.html">B</a>'
            b'<a href="../package-use.html">package</a></body></html>')
PACKAGE_USE = b'<html><head><title>Uses of Package a</title></head><body><a href="class-use/B.html">B</a></body></html>'
MODULE_GRAPH = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'


def _placeholder(name: str) -> bytes:
    return (b"<html><head><title>Placeholder for %s</title></head>"
            b"<body>Placeholder for XML layout attribute %s</body></html>" % (name.encode(), name.encode()))


def _pdf(*objects: bytes) -> bytes:
    return (b"%PDF-1.7\n" + b"".join(b"%d 0 obj\n%s\nendobj\n" % (i + 1, o) for i, o in enumerate(objects))
            + b"%%EOF\n")


def _flate(body: bytes) -> bytes:
    data = zlib.compress(body)
    return b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(data), data)


def _stream(data: bytes, entries: bytes = b"") -> bytes:
    return b"<< /Length %d %s >>\nstream\n%s\nendstream" % (len(data), entries, data)


def _run_length(data: bytes) -> bytes:
    """RunLengthDecode data in 3-byte literal runs, so no name survives whole in the raw file."""
    return b"".join(bytes([len(data[i:i + 3]) - 1]) + data[i:i + 3] for i in range(0, len(data), 3)) + b"\x80"


JAPANESE_FONT = b"<< /Type /Font /Subtype /Type0 /BaseFont /MS-Gothic /Encoding /90ms-RKSJ-H >>"

PLAIN_PDF = _pdf(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                 b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+Noto /Encoding /Identity-H >>",
                 b"<< /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>",
                 _flate(b"BT /F1 12 Tf (Hello) Tj ET"))

# What run() deletes from _base_rows() with every group on.
DELETED = {
    "j/html/api/m/a/class-use/B.html", "j/html/api/m/a/package-use.html", "j/html/api/m/module-graph.svg",
    "p/web/cmaps/90ms-RKSJ-H.bcmap", "p/web/cmaps/Adobe-Japan1-UCS2.bcmap", "p/web/cmaps/LICENSE",
    "p/web/wasm/openjpeg_nowasm_fallback.js", "p/web/wasm/jbig2_nowasm_fallback.js",
    "p/web/wasm/quickjs-eval.wasm", "p/web/wasm/quickjs-eval.js", "p/build/pdf.sandbox.mjs",
    "x/a.html", "x/b.html",
}


def _base_rows() -> dict:
    """path -> (content type, decoded bytes [, templateId [, stored as they are]]) for a database holding a row
    of every group, the pdf.js files that refer to them, and kept pages."""
    return {
        "i/index.html": ("text/html", b'<a href="../x.html">x</a><img src="images/logo.png"><a href="../p/web/'
                                      b'viewer.html?file=/d/guide.pdf">guide</a>'),
        "j/html/api/m/a/B.html": ("text/html", CLASS_PAGE),
        "j/html/api/m/a/class-use/B.html": ("text/html", USE_PAGE),
        "j/html/api/m/a/package-use.html": ("text/html", PACKAGE_USE),
        "j/html/api/m/module-graph.svg": ("image/svg+xml", MODULE_GRAPH),
        "j/html/api/script.js": ("text/javascript", b'var page = "B.html"; var tree = "package-tree.html";'),
        "j/html/api/m/a/package-tree.html": ("text/html", b"<p>tree</p>"),
        "p/web/cmaps/90ms-RKSJ-H.bcmap": ("application/octet-stream", b"\xe0\x01cmap"),
        "p/web/cmaps/Adobe-Japan1-UCS2.bcmap": ("application/octet-stream", b"\xe0\x02cmap"),
        "p/web/cmaps/LICENSE": ("text", b"license text"),
        "p/web/wasm/openjpeg.wasm": ("application/wasm", b"\x00asm\x01"),
        "p/web/wasm/openjpeg_nowasm_fallback.js": ("text/javascript", b"export default function(){}"),
        "p/web/wasm/jbig2_nowasm_fallback.js": ("text/javascript", b"export default function(){ }"),
        "p/web/wasm/quickjs-eval.wasm": ("application/wasm", b"\x00asm\x02"),
        "p/web/wasm/quickjs-eval.js": ("text/javascript", b'fetch("quickjs-eval.wasm")'),
        "p/build/pdf.sandbox.mjs": ("text/javascript", SANDBOX_MJS),
        "p/build/pdf.worker.mjs": ("text/javascript", WORKER_MJS),
        "p/web/viewer.mjs": ("text/javascript", VIEWER_MJS),
        "p/web/viewer.html": ("text/html", b'<script src="viewer.mjs" type="module"></script>'),
        "x/a.html": ("text/html", _placeholder("a")),
        "x/b.html": ("text/html", _placeholder("b")),
        "x.html": ("text", b"<p>Hello, App Dev for All</p>"),
        "x.pdf": ("application/pdf", PLAIN_PDF),
        "d/guide.pdf": ("application/pdf", PLAIN_PDF + b" "),
    }


def _compress(raw: bytes, dictionary: bytes = None) -> bytes:
    """Stored bytes for a brotli row, at quality 5, so a row the tool re-compressed (at 11) looks different."""
    if dictionary is None:
        return brotli.compress(raw, quality=5)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-q", "5", "-c"], input=raw,
                              stdout=subprocess.PIPE, check=True).stdout


def _decompress(blob: bytes, dictionary: bytes = None) -> bytes:
    if dictionary is None:
        return brotli.decompress(blob)
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(dictionary)
        return subprocess.run(["brotli", "-D", str(dict_path), "-d", "-c"], input=blob,
                              stdout=subprocess.PIPE, check=True).stdout


TOOLTIP_TABLES = {
    2: "CREATE TABLE Tooltips (id INTEGER PRIMARY KEY AUTOINCREMENT, categoryId INTEGER NOT NULL, tag TEXT NOT NULL,"
       " summary TEXT NOT NULL, detail TEXT NOT NULL, UNIQUE (categoryId, tag));"
       "CREATE TABLE TooltipButtons (tooltipId INTEGER, buttonNumberId INTEGER, description TEXT, uri TEXT);",
    # What migrate_tooltip_schema.py leaves (documentation schema 3).
    3: "CREATE TABLE TooltipTexts (id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT NOT NULL, detail TEXT NOT NULL);"
       "CREATE TABLE Tooltips (id INTEGER NOT NULL, categoryId INTEGER NOT NULL, tag TEXT NOT NULL,"
       " PRIMARY KEY (categoryId, tag)) WITHOUT ROWID;"
       "CREATE TABLE TooltipButtonDescriptions (id INTEGER PRIMARY KEY, description TEXT NOT NULL);"
       "CREATE TABLE TooltipButtonUris (id INTEGER PRIMARY KEY, uri TEXT NOT NULL);"
       "CREATE TABLE TooltipButtons (tooltipId INTEGER NOT NULL, buttonNumberId INTEGER NOT NULL,"
       " descriptionId INTEGER NOT NULL, uriId INTEGER NOT NULL, PRIMARY KEY (tooltipId, buttonNumberId)) WITHOUT ROWID;"
       "INSERT INTO TooltipButtonDescriptions VALUES (1, 'View full documentation');",
}


def _database(tmp_path: Path, rows: dict, dictionary: bytes = None, templates: dict = None,
              buttons: list = None, tooltips: list = None, schema: int = 2, versions: list = None) -> Path:
    """A miniature documentation database with the production schema's triggers and the tooltip tables of
    documentation schema 2 or 3. `templates` maps id -> (name, stored body), `buttons` lists (tooltipId, uri),
    `tooltips` (id, summary, detail) and `versions` the DocumentationDatabaseVersion rows as (major, minor, patch,
    changeTime); without it there is no such table."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL,"
        " content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
        "CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL,"
        " UNIQUE(name));"
        "CREATE TABLE TooltipCategories (id INTEGER PRIMARY KEY, category TEXT NOT NULL);"
        + TOOLTIP_TABLES[schema] +
        "CREATE TABLE Bookshelf (contentID INTEGER NOT NULL, title STRING DEFAULT '', description STRING DEFAULT '',"
        " bookCategoryID INTEGER, UNIQUE(title, bookCategoryId));"
        "CREATE TRIGGER AddBook AFTER INSERT ON Content WHEN NEW.path LIKE '%.pdf' BEGIN"
        " INSERT INTO Bookshelf (contentID, title) VALUES (NEW.id, CURRENT_TIMESTAMP || NEW.id); END;"
        "CREATE TRIGGER DeleteBook AFTER DELETE ON Content WHEN OLD.path LIKE '%.pdf' BEGIN"
        " DELETE FROM Bookshelf WHERE contentID = OLD.id; END;"
        "INSERT INTO TooltipCategories VALUES (1, 'ide');"
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
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, 1, ?, ?, ?)",
                     (path, stored, type_id, template_id))
    for template_id, (name, body) in (templates or {}).items():
        conn.execute("INSERT INTO Templates (id, name, content) VALUES (?, ?, ?)", (template_id, name, body))
    if versions is not None:
        conn.execute("CREATE TABLE DocumentationDatabaseVersion (major INT NOT NULL, minor INT NOT NULL,"
                     " patch INT NOT NULL, who TEXT NOT NULL, comment TEXT NOT NULL,"
                     " changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.executemany("INSERT INTO DocumentationDatabaseVersion VALUES (?, ?, ?, 'test', '', ?)", versions)
    for tooltip_id, summary, detail in tooltips or []:
        if schema == 2:
            conn.execute("INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES (?, 1, ?, ?, ?)",
                         (tooltip_id, f"tag{tooltip_id}", summary, detail))
        else:
            conn.execute("INSERT INTO TooltipTexts (id, summary, detail) VALUES (?, ?, ?)", (tooltip_id, summary, detail))
            conn.execute("INSERT INTO Tooltips (id, categoryId, tag) VALUES (?, 1, ?)", (tooltip_id, f"tag{tooltip_id}"))
    for number, (tooltip_id, uri) in enumerate(buttons or [], start=1):
        if schema == 2:
            conn.execute("INSERT INTO TooltipButtons VALUES (?, ?, 'View full documentation', ?)",
                         (tooltip_id, number, uri))
        else:
            conn.execute("INSERT INTO TooltipButtonUris (id, uri) VALUES (?, ?)", (number, uri))
            conn.execute("INSERT INTO TooltipButtons VALUES (?, ?, 1, ?)", (tooltip_id, number, number))
    conn.commit()
    conn.close()
    return db_path


def _stored(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return {row[0]: row[1:] for row in conn.execute(
            "SELECT path, languageID, content, contentTypeID, templateId FROM Content")}
    finally:
        conn.close()


def _table(db_path: Path, sql: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return sorted(conn.execute(sql).fetchall())
    finally:
        conn.close()


def _run(db_path: Path, **overrides) -> tuple:
    """(exit code, stdout, stderr) of one run of the tool."""
    cfg = {"db_path": db_path, "dry_run": False, "groups": None, "workers": 2, "verbose": False}
    cfg.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.run(cfg)
    return code, out.getvalue(), err.getvalue()


def _refused(tmp_path: Path, rows: dict, expected: str, **database) -> None:
    """Asserts that a run over rows exits 1 naming `expected` and leaves the database and directory as they were."""
    db = _database(tmp_path, rows, **database)
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 1, f"exit {code}, expected a refusal naming {expected!r}"
    assert expected in err, err
    assert _stored(db) == before
    assert len(list(tmp_path.iterdir())) == 1, "a refused run wrote a backup"


def _skip(reason: str) -> None:
    try:
        import pytest
    except ImportError:
        raise _Skipped(reason) from None
    pytest.skip(reason)


class _Skipped(Exception):
    pass


def test_every_group_deletes_its_rows_and_nothing_else(tmp_path: Path):
    rows = _base_rows()
    db = _database(tmp_path, rows)
    before = _stored(db)
    code, out, err = _run(db)
    after = _stored(db)
    assert code == 0, err
    assert set(before) - set(after) == DELETED
    for path in after:
        if path != tool.VIEWER:
            assert after[path] == before[path], path
    assert _decompress(after[tool.VIEWER][1]) == VIEWER_MJS_OFF
    assert after[tool.VIEWER][0] == before[tool.VIEWER][0] and after[tool.VIEWER][2:] == before[tool.VIEWER][2:]
    assert "javadoc-use: 3 row(s)" in out and "pdfjs-cmaps: 3 row(s)" in out
    assert "pdfjs-extras: 5 row(s)" in out and "x-placeholders: 2 row(s)" in out
    for src, dst in tool.ALLOWED:
        assert f"allowed: {src} refers to {dst}" in out


def test_only_the_named_groups_run(tmp_path: Path):
    db = _database(tmp_path, _base_rows())
    before = _stored(db)
    code, out, err = _run(db, groups=["x-placeholders", "javadoc-use"])
    after = _stored(db)
    assert code == 0, err
    assert set(before) - set(after) == {p for p in DELETED if p.startswith(("x/", "j/"))}
    assert after[tool.VIEWER] == before[tool.VIEWER]
    assert "pdfjs" not in out


def test_the_viewer_is_edited_in_either_form():
    unminified = b"const defaultOptions = {\n  enableScripting: {\n    value: true,\n    kind: OptionKind.VIEWER\n  }};"
    new, changed = tool.turn_scripting_off(unminified)
    assert changed and new == unminified.replace(b"value: true", b"value: false")
    new, changed = tool.turn_scripting_off(b"enableScripting:{value:!0,kind:1}")
    assert changed and new == b"enableScripting:{value:false,kind:1}"
    assert tool.turn_scripting_off(VIEWER_MJS_OFF) == (VIEWER_MJS_OFF, False)


def test_a_viewer_without_exactly_one_scripting_default_refuses(tmp_path: Path):
    for i, viewer in enumerate((b"const defaultOptions={};", VIEWER_MJS + b";" + VIEWER_MJS)):
        rows = _base_rows()
        rows[tool.VIEWER] = ("text/javascript", viewer)
        _refused(tmp_path / str(i), rows, "so scripting cannot be turned off")


def test_a_missing_viewer_refuses_while_the_sandbox_is_stored(tmp_path: Path):
    rows = _base_rows()
    del rows[tool.VIEWER]
    del rows["p/web/viewer.html"]
    _refused(tmp_path, rows, "cannot tell that scripting is off")


def test_a_database_without_pdfjs_needs_no_viewer(tmp_path: Path):
    rows = {path: row for path, row in _base_rows().items() if not path.startswith("p/")}
    db = _database(tmp_path, rows)
    code, _out, err = _run(db)
    assert code == 0, err


def test_each_kind_of_link_to_a_deleted_row_refuses(tmp_path: Path):
    cases = [
        ("i/a.html", "text/html", b'<a href="../j/html/api/m/a/class-use/B.html">use</a>',
         "links to j/html/api/m/a/class-use/B.html"),
        ("i/b.html", "text/html", b"<img src=../x/a.html>", "links to x/a.html"),
        ("i/c.html", "text/html", b'<img srcset="small.png 1x, ../x/b.html 2x">', "links to x/b.html"),
        ("i/d.html", "text/html", b'<meta http-equiv="refresh" content="0; url=../x/a.html">', "links to x/a.html"),
        ("i/e.css", "text/css", b".a { background: url( ../p/web/cmaps/LICENSE ) }", "links to p/web/cmaps/LICENSE"),
        ("i/f.css", "text/css", b"@import '../p/web/cmaps/LICENSE';", "links to p/web/cmaps/LICENSE"),
        ("i/g.svg", "image/svg+xml", b'<svg><use xlink:href="../j/html/api/m/module-graph.svg"/></svg>',
         "links to j/html/api/m/module-graph.svg"),
        ("i/h.js", "text/javascript", b'load("../p/web/wasm/quickjs-eval.js")', "links to p/web/wasm/quickjs-eval.js"),
        ("k/i.js", "text/javascript", b'const name = `${base}/package-use.html`;',
         "names j/html/api/m/a/package-use.html"),
        ("i/j.html", "text/html", b'<a href="../x/%61.html">a</a>', "links to x/a.html"),
        ("i/k.html", "text/html", b'<a href="http://localhost:6174/x/b.html">b</a>', "links to x/b.html"),
        ("i/l.html", "text/html", b'<a href="../p/web/viewer.html?file=/x/a.html">pdf</a>', "links to x/a.html"),
        ("i/l2.html", "text/html", b'<a href="../p/web/viewer.html?zoom=1&amp;file=/x/b.html">pdf</a>',
         "links to x/b.html"),
        ("i/m.html", "text/html", b'<a href="/x/b.html">b</a>', "links to x/b.html"),
        ("i/n.html", "text/html", b'<a href="../x/a">a</a>', "links to x/a.html"),
        ("k/o.html", "text/html", b'{"link": "../x/b.html", "body": "<a href=\\"../x/a.html\\">a</a>"}',
         "k/o.html links to x/a.html"),
        ("k/p.html", "text/html", b'{"next": "../x/b.html"}', "k/p.html links to x/b.html"),
    ]
    for i, (path, content_type, body, expected) in enumerate(cases):
        rows = _base_rows()
        rows[path] = (content_type, body, 7) if path.startswith("k/") and path.endswith(".html") else \
            (content_type, body)
        _refused(tmp_path / str(i), rows, f"{path} {expected}" if not expected.startswith(path) else expected)


def test_a_link_climbing_past_the_root_resolves_as_a_browser_resolves_it(tmp_path: Path):
    # A browser stops ".." at the root, so both links reach x/a.html. A kept i/sub/a.html shares the file name,
    # so only resolving the path, not the file-name rule, can catch them.
    for i, link in enumerate((b"../../x/a.html", b"/../x/a.html")):
        rows = _base_rows()
        rows["i/sub/a.html"] = ("text/html", b"<p>kept</p>")
        rows["i/clamp.html"] = ("text/html", b'<a href="%s">a</a>' % link)
        _refused(tmp_path / str(i), rows, "i/clamp.html links to x/a.html")
    assert tool.link_paths("../../../a/b.html", ("i",)) == ["a/b.html"]
    assert tool.link_paths("..", ("i",)) == []


def test_a_reference_the_allowlist_names_only_for_another_row_still_refuses(tmp_path: Path):
    rows = _base_rows()
    rows["p/web/other.mjs"] = ("text/javascript", b'import("../build/pdf.sandbox.mjs")')
    _refused(tmp_path, rows, "p/web/other.mjs links to p/build/pdf.sandbox.mjs")


def test_a_template_link_refuses(tmp_path: Path):
    rows = _base_rows()
    rows["k/topic.html"] = ("text/html", b'{"title": "t"}', 1)
    _refused(tmp_path / "up", rows, "template page.peb links to x/a.html",
             templates={1: ("page.peb", b'<a href="../x/a.html">{{ title }}</a>')})
    # A link that resolves only from the directory of the row using the template: from the root it names no
    # row, and its file name, B.html, is also a kept row's.
    rows = _base_rows()
    rows["j/html/api/m/a/T.html"] = ("text/html", b'{"title": "t"}', 1)
    _refused(tmp_path / "here", rows, "template page.peb links to j/html/api/m/a/class-use/B.html",
             templates={1: ("page.peb", b'<a href="class-use/B.html">{{ title }}</a>')})


# A template that links to a deleted row, and a dictionary that holds it, as the real ones hold theirs.
LINKING_TEMPLATE = b'<nav>{% for item in items %}<a href="{{ item }}">{{ item }}</a>{% endfor %}</nav>' * 3 + \
    b'<a href="../x/a.html">{{ title }}</a>'
TEMPLATE_DICTIONARY = VIEWER_MJS * 4 + LINKING_TEMPLATE


def test_a_compressed_template_link_refuses_from_version_3(tmp_path: Path):
    if shutil.which("brotli") is None:
        _skip("brotli is not on PATH")
    stored = _compress(LINKING_TEMPLATE, TEMPLATE_DICTIONARY)
    assert b"x/a.html" not in stored, "the link survives compression, so this tests nothing"
    rows = _base_rows()
    rows["k/topic.html"] = ("text/html", b'{"title": "t"}', 1)
    # The newest row decides, by changeTime and then rowid, whatever order the rows were written in.
    for i, versions in enumerate([[(3, 0, 0, "2026-10-19 07:09:52")],
                                  [(3, 0, 0, "2026-10-19 07:09:52"), (2, 1, 0, "2026-10-18 10:00:00")],
                                  [(2, 1, 0, "2026-10-19 07:09:52"), (3, 0, 0, "2026-10-19 07:09:52")]]):
        _refused(tmp_path / str(i), rows, "template page.peb links to x/a.html", dictionary=TEMPLATE_DICTIONARY,
                 schema=3, versions=versions, templates={1: ("page.peb", stored)})


def test_a_version_3_database_with_compressed_templates_deletes_the_same_rows(tmp_path: Path):
    if shutil.which("brotli") is None:
        _skip("brotli is not on PATH")
    rows = _base_rows()
    rows["k/topic.html"] = ("text/html", b'{"title": "t"}', 1)
    kept_link = LINKING_TEMPLATE.replace(b"../x/a.html", b"../x.html")
    db = _database(tmp_path, rows, dictionary=TEMPLATE_DICTIONARY, schema=3,
                   versions=[(3, 0, 0, "2026-10-19 07:09:52")],
                   templates={1: ("page.peb", _compress(kept_link, TEMPLATE_DICTIONARY))})
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 0, err
    assert set(before) - set(_stored(db)) == DELETED


def test_a_plain_template_link_refuses_below_version_3(tmp_path: Path):
    if shutil.which("brotli") is None:
        _skip("brotli is not on PATH")
    rows = _base_rows()
    rows["k/topic.html"] = ("text/html", b'{"title": "t"}', 1)
    # An empty version table declares no version, as a missing one does.
    for i, versions in enumerate([[(2, 1, 0, "2026-10-18 10:00:00")], [],
                                  [(3, 0, 0, "2026-10-17 10:00:00"), (2, 1, 0, "2026-10-18 10:00:00")],
                                  [(3, 0, 0, "2026-10-18 10:00:00"), (2, 1, 0, "2026-10-18 10:00:00")]]):
        _refused(tmp_path / str(i), rows, "template page.peb links to x/a.html", dictionary=TEMPLATE_DICTIONARY,
                 versions=versions, templates={1: ("page.peb", LINKING_TEMPLATE)})


def test_a_template_that_does_not_decode_from_version_3_refuses(tmp_path: Path):
    if shutil.which("brotli") is None:
        _skip("brotli is not on PATH")
    rows = _base_rows()
    rows["k/topic.html"] = ("text/html", b'{"title": "t"}', 1)
    # A step run after the migration left this one plain; its link to a kept row would not refuse.
    _refused(tmp_path, rows, "template page.peb could not be decoded, so its links were not checked",
             dictionary=TEMPLATE_DICTIONARY, schema=3, versions=[(3, 0, 0, "2026-10-19 07:09:52")],
             templates={1: ("page.peb", b'<a href="../x.html">{{ title }}</a>')})


def test_a_tooltip_button_refuses_in_either_schema(tmp_path: Path):
    for schema in (2, 3):
        _refused(tmp_path / str(schema), _base_rows(), "tooltip 3 button links to j/html/api/m/a/package-use.html",
                 tooltips=[(3, "s", "")], buttons=[(3, "i/index.html"), (3, "j/html/api/m/a/package-use.html#x")],
                 schema=schema)


def test_a_link_in_tooltip_text_refuses_in_either_schema(tmp_path: Path):
    for schema in (2, 3):
        _refused(tmp_path / f"s{schema}", _base_rows(), "tooltip 4 text links to x/b.html",
                 tooltips=[(4, 'See <a href="http://localhost:6174/x/b.html">b</a>.', "")], schema=schema)
        _refused(tmp_path / f"d{schema}", _base_rows(), "tooltip 5 text links to x/a.html",
                 tooltips=[(5, "s", '<a href="http://localhost:6174/x/a.html">a</a>')], schema=schema)


def test_a_schema_3_database_deletes_the_same_rows(tmp_path: Path):
    db = _database(tmp_path, _base_rows(), tooltips=[(1, "s", "d")], buttons=[(1, "j/html/api/m/a/B.html")], schema=3)
    before = _stored(db)
    code, _out, err = _run(db)
    assert code == 0, err
    assert set(before) - set(_stored(db)) == DELETED


def test_tooltip_tables_in_neither_shape_refuse_before_the_scan(tmp_path: Path):
    cases = [
        # Half migrated: the schema-2 tables beside a TooltipTexts.
        (2, "CREATE TABLE TooltipTexts (id INTEGER PRIMARY KEY, summary TEXT, detail TEXT);"),
        # Schema 3 without its uri table.
        (3, "DROP TABLE TooltipButtonUris;"),
        # Tooltips without their text anywhere.
        (2, "CREATE TABLE T2 (id INTEGER, categoryId INTEGER, tag TEXT); DROP TABLE Tooltips;"
            " ALTER TABLE T2 RENAME TO Tooltips;"),
        # The schema-3 tables, but Tooltips still holding text.
        (3, "ALTER TABLE Tooltips ADD COLUMN summary TEXT;"),
    ]
    for i, (schema, change) in enumerate(cases):
        db = _database(tmp_path / str(i), _base_rows(), schema=schema)
        conn = sqlite3.connect(db)
        conn.executescript(change)
        conn.close()
        before = _stored(db)
        code, out, err = _run(db)
        assert code == 1 and "in neither the schema-2 shape" in err, (i, err)
        assert "Reading" not in out and "Checking" not in out, (i, out)
        assert _stored(db) == before and len(list((tmp_path / str(i)).iterdir())) == 1


def test_a_bookshelf_entry_refuses(tmp_path: Path):
    db = _database(tmp_path, _base_rows())
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO Bookshelf (contentID, title) SELECT id, 'placeholder book' FROM Content "
                 "WHERE path = 'x/a.html'")
    conn.commit()
    conn.close()
    before, shelf = _stored(db), _table(db, "SELECT * FROM Bookshelf")
    code, _out, err = _run(db)
    assert code == 1 and "Bookshelf lists x/a.html" in err, err
    assert _stored(db) == before and _table(db, "SELECT * FROM Bookshelf") == shelf


def test_links_to_kept_rows_are_not_refused(tmp_path: Path):
    # B.html is kept and class-use/B.html shares only its file name; package-use.html on another host is not
    # this database's.
    rows = _base_rows()
    rows["i/q.html"] = ("text/html", b'<a href="../j/html/api/m/a/B.html">B</a><script>go("B.html")</script>'
                                     b'<a href="https://docs.example/api/package-use.html">elsewhere</a>')
    db = _database(tmp_path, rows, buttons=[(1, "j/html/api/m/a/B.html")])
    code, _out, err = _run(db)
    assert code == 0, err


def test_a_row_to_delete_that_is_not_what_its_group_holds_refuses(tmp_path: Path):
    cases = [
        ("x/c.html", ("text/html", b"<p>A real page</p>"), "x/c.html is not a placeholder page"),
        ("j/html/api/m/b/class-use/C.html", ("text/html", b"<title>C</title>"),
         "j/html/api/m/b/class-use/C.html is not a javadoc Use page"),
        ("j/html/api/n/module-graph.svg", ("image/svg+xml", b"<p>not svg</p>"),
         "j/html/api/n/module-graph.svg is not an SVG"),
        ("p/web/cmaps/notes.html", ("text/html", b"<p>notes</p>"), "p/web/cmaps/notes.html is not a CMap"),
    ]
    for i, (path, row, expected) in enumerate(cases):
        rows = _base_rows()
        rows[path] = row
        _refused(tmp_path / str(i), rows, expected)


def test_a_pdf_that_needs_a_cmap_refuses(tmp_path: Path):
    cases = [
        (_pdf(b"<< /Type /Font /Subtype /Type0 /BaseFont /MS-Gothic /Encoding /90ms-RKSJ-H >>"),
         "uses the predefined CMap 90ms-RKSJ-H"),
        (_pdf(_flate(b"/CIDInit /ProcSet findresource begin /90ms-RKSJ-H usecmap begincmap endcmap")),
         "uses the predefined CMap 90ms-RKSJ-H"),
        (_pdf(_flate(b"<< /Type /Font /Subtype /Type0 /Encoding /Adobe-Japan1-UCS2 >>")),
         "uses the predefined CMap Adobe-Japan1-UCS2"),
        (_pdf(b"<< /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >>"),
         "has a CID font of the Adobe Japan1 collection"),
        (_pdf(b"<< /Registry (Adobe) /Ordering <4B 6F72656131> /Supplement 2 >>"),
         "has a CID font of the Adobe Korea1 collection"),
        (_pdf(b"<< /Type /Font /Subtype /TrueType /BaseFont /#CB#CE#CC#E5 /Encoding /WinAnsiEncoding >>"),
         "has a font pdf.js reads as Chinese"),
        # pdf.js takes the FontDescriptor's FontName when it differs from the BaseFont.
        (_pdf(b"<< /Type /Font /Subtype /TrueType /BaseFont /SimSunX /FontDescriptor 2 0 R"
              b" /Encoding /WinAnsiEncoding >>", b"<< /Type /FontDescriptor /FontName /#CB#CE#CC#E5 >>"),
         "has a font pdf.js reads as Chinese"),
        (_pdf(b"<< /Type /FontDescriptor /FontName (\\313\\316\\314\\345) >>"), "has a font pdf.js reads as Chinese"),
        (_pdf(b"<< /Type /FontDescriptor /FontName <CBCECCE5> >>"), "has a font pdf.js reads as Chinese"),
        (_pdf(_flate(b"<< /Type /FontDescriptor /FontName (ABCDEF+\xcb\xce\xcc\xe5) >>")),
         "has a font pdf.js reads as Chinese"),
    ]
    for i, (pdf, expected) in enumerate(cases):
        rows = _base_rows()
        rows["d/cjk.pdf"] = ("application/pdf", pdf)
        _refused(tmp_path / str(i), rows, f"d/cjk.pdf {expected}")


def test_a_pdf_whose_fonts_cannot_all_be_read_refuses(tmp_path: Path):
    import base64
    cmap = "uses the predefined CMap 90ms-RKSJ-H"
    cases = [
        (_pdf(_stream(base64.a85encode(zlib.compress(JAPANESE_FONT), adobe=True),
                      b"/Type /ObjStm /N 1 /First 0 /Filter [/ASCII85Decode /FlateDecode]")), cmap),
        (_pdf(_stream(JAPANESE_FONT.hex().encode() + b">", b"/Filter /ASCIIHexDecode")), cmap),
        (_pdf(_stream(_run_length(JAPANESE_FONT), b"/Filter /RunLengthDecode")), cmap),
        (_pdf(_stream(base64.a85encode(JAPANESE_FONT), b"/Filter /A85")), cmap),
        (PLAIN_PDF + b"trailer\n<< /Root 1 0 R /Encrypt 5 0 R >>\n", "is encrypted, so its fonts cannot be read"),
        (_pdf(_stream(b"\x80\x0b\x60\x50", b"/Filter /LZWDecode")), "cannot decode (LZWDecode)"),
        (_pdf(_stream(b"not zlib data", b"/Filter /FlateDecode")), "cannot decode (FlateDecode: "),
        (_pdf(_stream(zlib.compress(b"\x02\x00\x00\x00\x01"),
                      b"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 4 >>")),
         "cannot decode (predictor 12)"),
    ]
    for i, (pdf, expected) in enumerate(cases):
        rows = _base_rows()
        rows["d/odd.pdf"] = ("application/pdf", pdf)
        _refused(tmp_path / str(i), rows, f"d/odd.pdf {expected}" if expected == cmap else expected)


def test_images_and_cross_reference_streams_do_not_refuse(tmp_path: Path):
    content = zlib.compress(b"BT /F1 12 Tf (Hello) Tj ET")
    pdf = _pdf(_stream(b"\xff\xd8\xff\xe0 not decodable", b"/Type /XObject /Subtype /Image /Filter /DCTDecode"),
               _stream(b"\x00\x00\x00\x0cjP  ", b"/Filter /JPXDecode"),
               _stream(b"\x80\x0b\x60\x50", b"/Subtype /Image /Filter /LZWDecode"),
               _stream(b"\x02\x01\x00\x10\x00", b"/Type /XRef /Filter /FlateDecode /DecodeParms << /Predictor 12 >>"),
               b"<< /Length 7 0 R /Filter /FlateDecode >>\nstream\n" + content + b"\nendstream",
               b"%d" % len(content),
               _stream(content, b"/Filter /FlateDecode"))
    rows = _base_rows()
    rows["d/images.pdf"] = ("application/pdf", pdf)
    code, _out, err = _run(_database(tmp_path, rows))
    assert code == 0, err


def test_font_names_are_read_as_pdf_js_reads_them():
    names = list(tool.font_names(b"/BaseFont /A#42C /FontName (x\\(y\\)z(n)\\101\\\nq\\\\) /FontName<4142 4>"
                                 b" /FontName <</Nope 1>>"))
    assert names == [b"ABC", b"x(y)z(n)Aq\\", b"AB@"]


def test_the_pdf_check_runs_only_with_the_cmaps(tmp_path: Path):
    rows = _base_rows()
    rows["d/cjk.pdf"] = ("application/pdf", _pdf(b"<< /Registry (Adobe) /Ordering (Japan1) >>"))
    db = _database(tmp_path, rows)
    code, _out, err = _run(db, groups=["x-placeholders"])
    assert code == 0, err


def test_an_unreadable_kept_row_refuses(tmp_path: Path):
    rows = _base_rows()
    rows["i/broken.html"] = ("text/html", b"not brotli at all", 0, True)
    _refused(tmp_path, rows, "i/broken.html could not be read, so its references were not checked")


def test_an_unreadable_row_to_delete_refuses(tmp_path: Path):
    # Its group's check cannot see what it holds, so it is not deleted on trust.
    rows = _base_rows()
    rows["x/c.html"] = ("text/html", b"not brotli at all", 0, True)
    _refused(tmp_path, rows, "x/c.html could not be read")


def test_an_unreadable_pdf_refuses(tmp_path: Path):
    rows = _base_rows()
    rows["d/broken.pdf"] = ("application/pdf", b"not brotli at all", 0, True)
    _refused(tmp_path, rows, "d/broken.pdf could not be read")


def test_scripting_is_turned_off_when_the_sandbox_rows_are_already_gone(tmp_path: Path):
    rows = {path: row for path, row in _base_rows().items() if path not in tool.PDFJS_EXTRAS}
    db = _database(tmp_path, rows)
    before = _stored(db)
    code, out, err = _run(db, groups=["pdfjs-extras"])
    after = _stored(db)
    assert code == 0, err
    assert "enableScripting default set to false" in out and not out.startswith("Nothing to do")
    assert set(after) == set(before) and _decompress(after[tool.VIEWER][1]) == VIEWER_MJS_OFF
    code, out, _err = _run(db, groups=["pdfjs-extras"])
    assert code == 0 and out.startswith("Nothing to do") and "scripting is already off" in out


def test_a_chunked_row_goes_with_its_base_and_others_stay(tmp_path: Path):
    import random
    rng = random.Random(7)
    noise = bytes(rng.getrandbits(8) for _ in range(CHUNK_SIZE + 5000))
    rows = _base_rows()
    # Stored as they are: a deleted placeholder spanning two rows, a kept page spanning two, and a page
    # that only looks like a chunk row because its base is short.
    page = brotli.compress(_placeholder("big") + b"<!--" + noise.hex().encode() + b"-->", quality=0)
    assert len(page) > CHUNK_SIZE
    rows["x/big.html"] = ("text/html", page[:CHUNK_SIZE], 0, True)
    rows["x/big.html-1"] = ("text/html", page[CHUNK_SIZE:], 0, True)
    kept = brotli.compress(b"<p>" + noise.hex().encode() + b"</p>", quality=0)
    rows["i/big.html"] = ("text/html", kept[:CHUNK_SIZE], 0, True)
    rows["i/big.html-1"] = ("text/html", kept[CHUNK_SIZE:], 0, True)
    rows["x/a.html-1"] = ("text/html", _placeholder("a-1"))
    db = _database(tmp_path, rows)
    code, out, err = _run(db)
    after = _stored(db)
    assert code == 0, err
    assert not {"x/big.html", "x/big.html-1", "x/a.html-1"} & set(after)
    assert {"i/big.html", "i/big.html-1"} <= set(after)
    assert "x-placeholders: 4 row(s) of x/ placeholder pages and 1 chunk row(s)" in out


def test_dry_run_writes_nothing(tmp_path: Path):
    db = _database(tmp_path, _base_rows())
    before = _stored(db)
    code, out, _err = _run(db, dry_run=True)
    assert code == 0 and "would delete 13 row(s)" in out
    assert _stored(db) == before
    assert len(list(tmp_path.iterdir())) == 1


def test_a_second_run_changes_nothing(tmp_path: Path):
    db = _database(tmp_path, _base_rows())
    assert _run(db)[0] == 0
    once = _stored(db)
    files = sorted(tmp_path.iterdir())
    code, out, _err = _run(db)
    assert code == 0 and out.startswith("Nothing to do")
    assert _stored(db) == once and sorted(tmp_path.iterdir()) == files


def test_the_backup_holds_the_database_as_it_was(tmp_path: Path):
    db = _database(tmp_path, _base_rows())
    before = _stored(db)
    code, out, _err = _run(db)
    backup = Path(re.search(r"Backup written to (.+)", out).group(1).strip())
    assert code == 0 and backup.is_file()
    assert _stored(backup) == before


def test_an_error_while_writing_rolls_everything_back(tmp_path: Path):
    db = _database(tmp_path, _base_rows())
    before = _stored(db)
    real = tool.delete_media_row
    calls = []

    def failing(conn, path, length):
        calls.append(path)
        if len(calls) == 3:
            raise RuntimeError("disk full")
        return real(conn, path, length)

    tool.delete_media_row = failing
    try:
        try:
            _run(db)
        except RuntimeError:
            pass
        else:
            raise AssertionError("the failure did not propagate")
    finally:
        tool.delete_media_row = real
    assert len(calls) == 3 and _stored(db) == before


def test_a_dictionary_database_is_read_and_written_through_the_dictionary(tmp_path: Path):
    if shutil.which("brotli") is None:
        _skip("brotli is not on PATH")
    dictionary = VIEWER_MJS * 4 + USE_PAGE * 4
    db = _database(tmp_path, _base_rows(), dictionary=dictionary)
    code, _out, err = _run(db)
    assert code == 0, err
    assert _decompress(_stored(db)[tool.VIEWER][1], dictionary) == VIEWER_MJS_OFF


INDEX_ALL_PAGE = (b'<html><head><title>Index</title></head><body class="index-page"><a href="m/a/B.html">B</a>'
                  b'<h2 id="I:A">A</h2><h2 id="I:B">B</h2></body></html>')
SPLIT_PAGE_A = (b'<html><head><title>A-Index</title></head><body class="index-page"><a href="index-2.html">B</a>'
                b'<h2 id="I:A">A</h2></body></html>')
SPLIT_PAGE_B = (b'<html><head><title>B-Index</title></head><body class="index-page"><a href="index-1.html">A</a>'
                b'<h2 id="I:B">B</h2></body></html>')


def _index_rows() -> dict:
    """A javadoc tree with a one-page index, its split index, and links to the one-page index at several depths
    and in several forms, with what each linking row must read once the links are pointed at the split index."""
    rows = {
        "j/html/api/index-all.html": ("text/html", INDEX_ALL_PAGE),
        "j/html/api/index-files/index-1.html": ("text/html", SPLIT_PAGE_A),
        "j/html/api/index-files/index-2.html": ("text/html", SPLIT_PAGE_B),
        "j/html/api/help-doc.html": ("text/html", b'<p>The <a href="index-all.html">Index</a> lists every name.</p>'),
        "j/html/api/m/a/B.html": ("text/html", b'<li><a href="../../index-all.html">Index</a></li>'
                                               b'<a href="C.html">C</a><a href="https://example.com/api/index-all.html">'
                                               b'theirs</a>'),
        "j/html/api/m/a/b/C.html": ("text/html", b"<li><a href='../../../index-all.html'>Index</a></li>"
                                                 b'<a href="../../../index-all.html#I:B">B names</a>'),
        "j/html/api/m/a/doc-files/guide.html": ("text/html", b'<a href="../../../index-files/index-1.html">Index</a>'),
        "i/apis.html": ("text/html", b'<a href="/j/html/api/index-all.html">Java names</a>'),
        # Not javadoc: a page that happens to share the name is neither deleted nor checked as an index.
        "k/notes/index-all.html": ("text/html", b"<p>All the notes</p>"),
    }
    expected = {
        "j/html/api/help-doc.html": b'<p>The <a href="index-files/index-1.html">Index</a> lists every name.</p>',
        "j/html/api/m/a/B.html": b'<li><a href="../../index-files/index-1.html">Index</a></li>'
                                 b'<a href="C.html">C</a><a href="https://example.com/api/index-all.html">theirs</a>',
        "j/html/api/m/a/b/C.html": b"<li><a href='../../../index-files/index-1.html'>Index</a></li>"
                                   b'<a href="../../../index-files/index-2.html#I:B">B names</a>',
        "i/apis.html": b'<a href="/j/html/api/index-files/index-1.html">Java names</a>',
    }
    return rows, expected


def test_index_links_are_pointed_at_the_split_index_and_the_one_page_index_deleted(tmp_path: Path):
    rows, expected = _index_rows()
    db = _database(tmp_path, rows)
    before = _stored(db)
    code, out, err = _run(db, groups=["javadoc-index-all"])
    after = _stored(db)
    assert code == 0, err
    assert set(before) - set(after) == {"j/html/api/index-all.html"}
    for path in after:
        if path in expected:
            assert _decompress(after[path][1]) == expected[path], path
            assert after[path][0] == before[path][0] and after[path][2:] == before[path][2:], path
        else:
            assert after[path] == before[path], path
    assert "javadoc-index-all: 5 link(s) in 4 row(s) pointed at the split index" in out
    assert "javadoc-index-all: 1 row(s) of javadoc one-page indexes" in out


def test_a_fragment_goes_to_the_split_page_that_holds_its_anchor():
    split = {"j/api/index-all.html": {None: "j/api/index-files/index-1.html", "I:A": "j/api/index-files/index-1.html",
                                      "I:B": "j/api/index-files/index-2.html"}}
    new, count, problems = tool.point_at_split_index(b'<a href="../index-all.html#I:B">', "j/api/m", split)
    assert (new, count, problems) == (b'<a href="../index-files/index-2.html#I:B">', 1, [])
    new, count, problems = tool.point_at_split_index(b'<a href="../index-all.html#I:Z">', "j/api/m", split)
    assert new == b'<a href="../index-all.html#I:Z">' and count == 0 and "an anchor no split index page holds" in problems[0]


def test_upper_case_attributes_and_file_names_are_pointed_at_the_split_index():
    split = {"j/api/index-all.html": {None: "j/api/index-files/index-1.html"}}
    for link in (b'<a HREF="../index-all.html">', b'<img SRC="../index-all.html">', b'<a href="../INDEX-ALL.HTML">'):
        new, count, problems = tool.point_at_split_index(link, "j/api/m", split)
        expected = link.replace(b"index-all.html", b"index-files/index-1.html").replace(b"INDEX-ALL.HTML",
                                                                                      b"index-files/index-1.html")
        assert (new, count, problems) == (expected, 1, []), link


def test_a_row_whose_only_link_is_upper_case_is_rewritten_too(tmp_path: Path):
    # The rewrite matches the name in any case, so whether a row is rewritten must not hang on it also holding a
    # lower-case link.
    rows, _expected = _index_rows()
    rows["j/html/api/m/a/U.html"] = ("text/html", b'<a href="../../INDEX-ALL.HTML">Index</a>')
    db = _database(tmp_path, rows)
    code, out, err = _run(db, groups=["javadoc-index-all"])
    assert code == 0, err
    assert _decompress(_stored(db)["j/html/api/m/a/U.html"][1]) == b'<a href="../../index-files/index-1.html">Index</a>'
    assert "javadoc-index-all: 6 link(s) in 5 row(s) pointed at the split index" in out


def test_xlink_and_data_href_links_are_left_to_the_reference_check(tmp_path: Path):
    # Only a bare href or src attribute is rewritten; a longer name ending in one is not, so the reference check
    # sees the link to the one-page index and the run refuses.
    split = {"j/api/index-all.html": {None: "j/api/index-files/index-1.html"}}
    for link in (b'<use xlink:href="../index-all.html"/>', b'<div data-href="../index-all.html">',
                 b'<img data-src="../index-all.html">'):
        assert tool.point_at_split_index(link, "j/api/m", split) == (link, 0, []), link
    cases = [("j/html/api/m/a/G.svg", ("image/svg+xml", b'<svg xmlns:xlink="http://www.w3.org/1999/xlink">'
                                                        b'<a xlink:href="../../index-all.html">Index</a></svg>')),
             ("j/html/api/m/a/H.html", ("text/html", b'<div data-href="../../index-all.html">Index</div>'))]
    for i, (path, row) in enumerate(cases):
        rows, _expected = _index_rows()
        rows[path] = row
        _refused(tmp_path / str(i), rows, f"{path} links to j/html/api/index-all.html")


def test_a_percent_encoded_fragment_goes_to_the_page_holding_the_decoded_id():
    split = {"j/api/index-all.html": {None: "j/api/index-files/index-1.html", "I:A": "j/api/index-files/index-1.html",
                                      "I:B": "j/api/index-files/index-2.html"}}
    new, count, problems = tool.point_at_split_index(b'<a href="../index-all.html#I%3AB">', "j/api/m", split)
    assert (new, count, problems) == (b'<a href="../index-files/index-2.html#I%3AB">', 1, [])


def test_an_id_on_several_split_pages_goes_to_the_first(tmp_path: Path):
    # javadoc repeats its navbar ids on every split page; a link to one goes to index-1.html, not the last page read.
    rows, _expected = _index_rows()
    for page in ("j/html/api/index-files/index-1.html", "j/html/api/index-files/index-2.html"):
        rows[page] = (rows[page][0], rows[page][1].replace(b'<body class="index-page">',
                                                             b'<body class="index-page"><nav id="navbar-top"></nav>'))
    rows["j/html/api/m/a/D.html"] = ("text/html", b'<a href="../../index-all.html#navbar-top">Top</a>')
    db = _database(tmp_path, rows)
    code, _out, err = _run(db, groups=["javadoc-index-all"])
    assert code == 0, err
    assert _decompress(_stored(db)["j/html/api/m/a/D.html"][1]) == \
        b'<a href="../../index-files/index-1.html#navbar-top">Top</a>'


def test_a_link_to_an_anchor_no_split_page_holds_refuses(tmp_path: Path):
    rows, _expected = _index_rows()
    rows["j/html/api/m/a/D.html"] = ("text/html", b'<a href="../../index-all.html#I:Z">Z names</a>')
    _refused(tmp_path, rows, "j/html/api/m/a/D.html links to j/html/api/index-all.html#I:Z, an anchor no split index")


def test_a_link_the_rewrite_does_not_reach_refuses(tmp_path: Path):
    cases = [
        ("j/html/api/m/a/E.html", ("text/html", b"<a href=../../index-all.html>Index</a>"), "links to"),
        ("j/html/api/script.js", ("text/javascript", b'var index = "index-all.html";'), "links to"),
        # A templated row's JSON is not rewritten, whatever its quotes.
        ("j/html/api/m/a/F.html", ("text/html", b"{\"body\": \"<a href='../../index-all.html'>Index</a>\"}", 3),
         "links to"),
    ]
    for i, (path, row, how) in enumerate(cases):
        rows, _expected = _index_rows()
        rows[path] = row
        _refused(tmp_path / str(i), rows, f"{path} {how} j/html/api/index-all.html")


def test_a_template_link_to_the_one_page_index_refuses_in_either_encoding(tmp_path: Path):
    if shutil.which("brotli") is None:
        _skip("brotli is not on PATH")
    # The rewrite reaches Content rows only, so a template's link must refuse, compressed (version 3) or plain.
    template = LINKING_TEMPLATE.replace(b"../x/a.html", b"../../index-all.html")
    stored = _compress(template, TEMPLATE_DICTIONARY)
    assert b"index-all.html" not in stored, "the link survives compression, so this tests nothing"
    for i, (body, versions) in enumerate([(stored, [(3, 0, 0, "2026-10-19 07:09:52")]),
                                          (template, [(2, 1, 0, "2026-10-18 10:00:00")])]):
        rows, _expected = _index_rows()
        rows["j/html/api/m/a/T.html"] = ("text/html", b'{"title": "t"}', 1)
        _refused(tmp_path / str(i), rows, "template page.peb links to j/html/api/index-all.html",
                 dictionary=TEMPLATE_DICTIONARY, schema=3, versions=versions, templates={1: ("page.peb", body)})


def test_a_one_page_index_without_its_split_index_refuses(tmp_path: Path):
    rows, _expected = _index_rows()
    del rows["j/html/api/index-files/index-1.html"]
    _refused(tmp_path, rows, "j/html/api/index-all.html has no split index (j/html/api/index-files/index-1.html)")


def test_an_unreadable_split_index_page_refuses(tmp_path: Path):
    rows, _expected = _index_rows()
    rows["j/html/api/index-files/index-2.html"] = ("text/html", b"not brotli", 0, True)
    _refused(tmp_path, rows, "j/html/api/index-files/index-2.html could not be read, so its anchors are unknown")


def test_a_page_named_index_all_that_is_not_an_index_refuses(tmp_path: Path):
    rows, _expected = _index_rows()
    rows["j/html/api/index-all.html"] = ("text/html", b"<html><body>Not an index</body></html>")
    _refused(tmp_path, rows, "j/html/api/index-all.html is not a javadoc index page")


def test_index_links_dry_run_writes_nothing_and_a_second_run_changes_nothing(tmp_path: Path):
    rows, _expected = _index_rows()
    db = _database(tmp_path, rows)
    before = _stored(db)
    code, out, err = _run(db, dry_run=True, groups=["javadoc-index-all"])
    assert code == 0, err
    assert "would point 5 index link(s) in 4 row(s)" in out and _stored(db) == before
    assert len(list(tmp_path.iterdir())) == 1
    assert _run(db, groups=["javadoc-index-all"])[0] == 0
    once = _stored(db)
    code, out, err = _run(db, groups=["javadoc-index-all"])
    assert code == 0 and "Nothing to do" in out, out + err
    assert _stored(db) == once


def test_a_chunked_page_is_rewritten_and_rechunked(tmp_path: Path):
    import random
    rng = random.Random(11)
    noise = bytes(rng.getrandbits(8) for _ in range(CHUNK_SIZE + 5000)).hex().encode()
    page = b'<li><a href="../../index-all.html">Index</a></li><!--' + noise + b"-->"
    stored = brotli.compress(page, quality=0)
    assert len(stored) > CHUNK_SIZE
    rows, _expected = _index_rows()
    rows["j/html/api/m/a/Big.html"] = ("text/html", stored[:CHUNK_SIZE], 0, True)
    rows["j/html/api/m/a/Big.html-1"] = ("text/html", stored[CHUNK_SIZE:], 0, True)
    db = _database(tmp_path, rows)
    code, _out, err = _run(db, groups=["javadoc-index-all"])
    after = _stored(db)
    assert code == 0, err
    chunks = [after[p][1] for p in sorted(after) if p.startswith("j/html/api/m/a/Big.html")]
    assert len(chunks) >= 2 and all(len(c) == CHUNK_SIZE for c in chunks[:-1])
    assert _decompress(b"".join(chunks)) == page.replace(b"../../index-all.html", b"../../index-files/index-1.html")


PICTURE = b"RIFF\x10\x00\x00\x00WEBPVP8 picture"
DIAGRAM = b"RIFF\x10\x00\x00\x00WEBPVP8 diagram"


def _copy_rows() -> dict:
    """Two sets of byte-identical images, linked in every form the rewrite reaches, with what each linking row must
    read once its links point at the kept copy."""
    rows = {
        # A Help+Manual toggle: the small and zoomed images became identical. Both linked once, so the shorter is kept.
        "i/images/shot.webp": ("image/webp", PICTURE),
        "i/images/shot_zoom25.webp": ("image/webp", PICTURE),
        "i/topic.html": ("text/html", b'{"body": "<a class=\\"imagetogglelink\\" href=\\"./images/shot.webp\\">'
                                      b'<img src=\\"./images/shot_zoom25.webp\\" data-src0=\\"./images/shot_zoom25.webp'
                                      b'\\" data-src1=\\"./images/shot.webp\\"></a>"}', 1),
        # The longer name is linked from four rows, the shorter from two (three with the tooltip button the first
        # test adds), so the longer is kept.
        "a/devsite/media/arch-overview.webp": ("image/webp", DIAGRAM),
        "a/devsite/media/arch.webp": ("image/webp", DIAGRAM),
        "a/devsite/one.html": ("text/html", b'<img src="media/arch-overview.webp">'),
        "a/devsite/three.html": ("text/html", b'<img src="media/arch-overview.webp">'),
        "a/devsite/four.html": ("text/html", b'<img src="./media/arch-overview.webp">'),
        "a/devsite/two.html": ("text/html", b'<img srcset="media/arch.webp 1x, /a/devsite/media/arch-overview.webp '
                                            b'2x"><div style="background:url(\'media/arch.webp\')"></div>'
                                            b'<a href="http://localhost:6174/a/devsite/media/arch.webp">big</a>'),
        "a/devsite/style.css": ("text/css", b".hero{background:url(media/arch.webp)}"),
        # Same name, other bytes, other directory: not a copy, and its links stay.
        "k/images/shot_zoom25.webp": ("image/webp", b"RIFF\x10\x00\x00\x00WEBPVP8 kotlin"),
        "k/page.html": ("text/html", b'<img src="images/shot_zoom25.webp">'),
    }
    expected = {
        "i/topic.html": b'{"body": "<a class=\\"imagetogglelink\\" href=\\"./images/shot.webp\\">'
                        b'<img src=\\"./images/shot.webp\\" data-src0=\\"./images/shot.webp'
                        b'\\" data-src1=\\"./images/shot.webp\\"></a>"}',
        "a/devsite/two.html": b'<img srcset="media/arch-overview.webp 1x, /a/devsite/media/arch-overview.webp '
                              b'2x"><div style="background:url(\'media/arch-overview.webp\')"></div>'
                              b'<a href="http://localhost:6174/a/devsite/media/arch-overview.webp">big</a>',
        "a/devsite/style.css": b".hero{background:url(media/arch-overview.webp)}",
    }
    return rows, expected


def test_copies_are_deleted_and_their_links_pointed_at_the_kept_copy(tmp_path: Path):
    rows, expected = _copy_rows()
    db = _database(tmp_path, rows, buttons=[(1, "a/devsite/media/arch.webp#top"), (2, "k/page.html")])
    before = _stored(db)
    code, out, err = _run(db, groups=["duplicate-media"])
    after = _stored(db)
    assert code == 0, err
    assert set(before) - set(after) == {"i/images/shot_zoom25.webp", "a/devsite/media/arch.webp"}
    for path in after:
        if path in expected:
            assert _decompress(after[path][1]) == expected[path], path
            assert after[path][0] == before[path][0] and after[path][2:] == before[path][2:], path
        else:
            assert after[path] == before[path], path
    assert _table(db, "SELECT uri FROM TooltipButtons") == [("a/devsite/media/arch-overview.webp#top",),
                                                           ("k/page.html",)]
    assert "duplicate-media: 2 row(s) of byte-identical media copies" in out
    assert "duplicate-media: 6 link(s) in 3 row(s) and 1 tooltip uri(s) pointed at the 2 kept copies" in out


def test_tooltip_button_uris_are_pointed_at_the_kept_copy_in_schema_3(tmp_path: Path):
    rows, _expected = _copy_rows()
    db = _database(tmp_path, rows, schema=3, buttons=[(1, "a/devsite/media/arch.webp"), (2, "k/page.html")])
    code, out, err = _run(db, groups=["duplicate-media"])
    assert code == 0, err
    assert _table(db, "SELECT uri FROM TooltipButtonUris") == [("a/devsite/media/arch-overview.webp",),
                                                              ("k/page.html",)]


def test_the_kept_copy_is_the_most_linked_then_the_shortest_then_the_lowest_id(tmp_path: Path):
    rows = {
        # A link from a template, a tooltip button or the Bookshelf outweighs the shorter, unlinked name.
        "m/template-linked.webp": ("image/webp", b"RIFF a"), "m/a.webp": ("image/webp", b"RIFF a"),
        "m/tooltip-linked.webp": ("image/webp", b"RIFF b"), "m/b.webp": ("image/webp", b"RIFF b"),
        "m/bookshelf-listed.webp": ("image/webp", b"RIFF c"), "m/c.webp": ("image/webp", b"RIFF c"),
        # Two linking rows outweigh one.
        "m/two-pages.webp": ("image/webp", b"RIFF f"), "m/f.webp": ("image/webp", b"RIFF f"),
        "m/p1.html": ("text/html", b'<img src="two-pages.webp">'), "m/p2.html": ("text/html", b'<img src="two-pages.webp">'),
        "m/p3.html": ("text/html", b'<img src="f.webp">'),
        # Neither copy linked: the shorter is kept, and of two the same length, the lower id (inserted first).
        "m/d-longer.webp": ("image/webp", b"RIFF d"), "m/d.webp": ("image/webp", b"RIFF d"),
        "m/e2.webp": ("image/webp", b"RIFF e"), "m/e1.webp": ("image/webp", b"RIFF e"),
    }
    template = b'<img src="m/template-linked.webp"><img src="m/template-linked.webp">'
    db = _database(tmp_path, rows, buttons=[(1, "m/tooltip-linked.webp")], templates={1: ("page.peb", template)})
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO Bookshelf (contentID, title) SELECT id, 'b' FROM Content WHERE path = "
                 "'m/bookshelf-listed.webp'")
    conn.commit()
    conn.close()
    code, out, err = _run(db, groups=["duplicate-media"], verbose=True)
    assert code == 0, err
    assert set(_stored(db)) == set(rows) - {"m/a.webp", "m/b.webp", "m/c.webp", "m/f.webp", "m/d-longer.webp", "m/e1.webp"}
    assert "[COPY] m/a.webp -> m/template-linked.webp" in out and "[COPY] m/e1.webp -> m/e2.webp" in out


def test_only_identical_media_of_one_type_and_language_are_copies(tmp_path: Path):
    big = b"\x1aE\xdf\xa3" + bytes(range(256)) * (CHUNK_SIZE // 256)
    rows = {
        "m/same.webp": ("image/webp", PICTURE), "m/same.png": ("image/png", PICTURE),
        "m/other-language.webp": ("image/webp", PICTURE),
        "m/one-byte.webp": ("image/webp", PICTURE[:-1] + b"X"),
        # Placeholder books: identical PDFs, each its own Bookshelf entry. And identical licence notices.
        "b/one.pdf": ("application/pdf", PLAIN_PDF), "b/two.pdf": ("application/pdf", PLAIN_PDF),
        "legal/a.md": ("text/markdown", b"MIT"), "legal/b.md": ("text/markdown", b"MIT"),
        # Chunked videos: the same first chunk is not enough; the same chunks, joined, are.
        "v/first.webm": ("video/webm", big[:CHUNK_SIZE]), "v/first.webm-1": ("video/webm", b"tail one"),
        "v/second.webm": ("video/webm", big[:CHUNK_SIZE]), "v/second.webm-1": ("video/webm", b"tail two"),
        "v/third.webm": ("video/webm", big[:CHUNK_SIZE]), "v/third.webm-1": ("video/webm", b"tail one"),
    }
    db = _database(tmp_path, rows)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE Content SET languageID = 2 WHERE path = 'm/other-language.webp'")
    conn.commit()
    conn.close()
    code, out, err = _run(db, groups=["duplicate-media"])
    assert code == 0, err
    assert set(rows) - set(_stored(db)) == {"v/third.webm", "v/third.webm-1"}


def test_byte_identical_svgs_are_not_merged(tmp_path: Path):
    # An SVG is a document: its relative references resolve against its own URL. Merged, the Android page's
    # logo would load the Kotlin picture beside the kept copy.
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
           b'<image xlink:href="pic.webp" width="10" height="10"/></svg>')
    rows = {
        "a/media/logo.svg": ("image/svg+xml", svg), "k/art/logo.svg": ("image/svg+xml", svg),
        "a/media/pic.webp": ("image/webp", b"RIFF\x10\x00\x00\x00WEBPVP8 android-picture"),
        "k/art/pic.webp": ("image/webp", b"RIFF\x10\x00\x00\x00WEBPVP8 kotlin-picture!!"),
        "a/page.html": ("text/html", b'<img src="media/logo.svg">'),
        "k/page.html": ("text/html", b'<img src="art/logo.svg">'),
    }
    db = _database(tmp_path, rows)
    before = _stored(db)
    code, out, err = _run(db, groups=["duplicate-media"])
    assert code == 0, err
    assert _stored(db) == before, "an SVG copy was merged"


def test_a_link_to_a_dropped_copy_the_rewrite_does_not_reach_refuses(tmp_path: Path):
    cases = [
        # A template is not rewritten; it links to the copy fewer rows link to.
        ("template page.peb links to m/b.webp", {}, {1: ("page.peb", b'<img src="m/b.webp">')}, []),
        # An entity-encoded slash the rewrite cannot read as one path, but the reference check can.
        ("n/p3.html links to m/b.webp", {"n/p3.html": ("text/html", b'<img src="../m&#47;b.webp">')}, {}, []),
        # Tooltip text is not rewritten either.
        ("tooltip 5 text links to m/b.webp", {}, {}, [(5, "See", '<img src="m/b.webp">')]),
    ]
    for i, (expected, extra, templates, tooltips) in enumerate(cases):
        rows = {"m/a.webp": ("image/webp", PICTURE), "m/b.webp": ("image/webp", PICTURE),
                "m/p1.html": ("text/html", b'<img src="a.webp">'), "m/p2.html": ("text/html", b'<img src="a.webp">')}
        rows.update(extra)
        _refused(tmp_path / str(i), rows, expected, templates=templates, tooltips=tooltips)


def test_a_percent_encoded_link_stays_percent_encoded():
    remap = {"m/shot copy.webp": "m/shot.webp"}
    new, changed = tool.point_at_kept_copies(b'<img src="shot%20copy.webp"><img src="shot copy.webp">', "m", remap,
                                             tool.dropped_copy_links(remap))
    assert (new, changed) == (b'<img src="shot.webp"><img src="shot.webp">', 2)
    remap = {"m/a b copy.webp": "m/a b.webp"}
    new, changed = tool.point_at_kept_copies(b'<img src="a%20b%20copy.webp">', "m", remap,
                                             tool.dropped_copy_links(remap))
    assert (new, changed) == (b'<img src="a%20b.webp">', 1)


def test_a_kept_copy_in_another_directory_keeps_the_link_prefix():
    remap = {"i/images/x.webp": "a/media/y.webp"}
    pattern = tool.dropped_copy_links(remap)
    new, changed = tool.point_at_kept_copies(b'src="./images/x.webp" src="/i/images/x.webp" src="images/xx.webp"',
                                             "i", remap, pattern)
    assert (new, changed) == (b'src="./images/../../a/media/y.webp" src="/i/images/../../a/media/y.webp" '
                              b'src="images/xx.webp"', 2)


def test_a_longer_name_holding_a_dropped_copys_name_is_left_alone():
    remap = {"i/images/x.webp": "i/images/y.webp"}
    pattern = tool.dropped_copy_links(remap)
    links = b'src="xx.webp" src="x.webpx" src="x.webp.png" src="x.webp"'
    new, changed = tool.point_at_kept_copies(links, "i/images", remap, pattern)
    assert (new, changed) == (b'src="xx.webp" src="x.webpx" src="x.webp.png" src="y.webp"', 1)


def test_copies_ignore_rows_other_groups_delete(tmp_path: Path):
    rows = _base_rows()
    rows.update({
        # The module graph goes with javadoc-use; its identical doc-files copy is not a copy of it, and stays.
        "j/html/api/m/doc-files/graph.svg": ("image/svg+xml", MODULE_GRAPH),
        "j/html/api/m/doc-files/guide.html": ("text/html", b'<img src="graph.svg">'),
        # A link from a placeholder page, which x-placeholders deletes, does not count: the shorter name is kept.
        "m/a.webp": ("image/webp", PICTURE), "m/bb.webp": ("image/webp", PICTURE),
        "x/c.html": ("text/html", _placeholder("c") + b'<img src="../m/bb.webp">'),
    })
    db = _database(tmp_path, rows)
    code, out, err = _run(db)
    assert code == 0, err
    assert set(rows) - set(_stored(db)) == DELETED | {"x/c.html", "m/bb.webp"}
    assert "javadoc-use: 3 row(s)" in out and "duplicate-media: 1 row(s)" in out


def test_a_json_row_that_would_stop_parsing_refuses(tmp_path: Path):
    # The kept copy's name holds a quote, which would end the JSON string it is written into. Each copy has one
    # link, so the shorter name is kept.
    rows = {'m/".webp': ("image/webp", PICTURE), "m/longer.webp": ("image/webp", PICTURE),
            "m/topic.html": ("text/html", b'{"body": "<img src=\\"longer.webp\\">"}', 1)}
    _refused(tmp_path, rows, "m/topic.html is no longer JSON once its links point at the kept copies",
             buttons=[(1, 'm/".webp')])


def test_copies_dry_run_writes_nothing_and_a_second_run_changes_nothing(tmp_path: Path):
    rows, _expected = _copy_rows()
    db = _database(tmp_path, rows)
    before = _stored(db)
    code, out, err = _run(db, dry_run=True, groups=["duplicate-media"])
    assert code == 0, err
    assert "would point 6 link(s) in 3 row(s)" in out and _stored(db) == before
    assert len(list(tmp_path.iterdir())) == 1
    assert _run(db, groups=["duplicate-media"])[0] == 0
    once = _stored(db)
    code, out, err = _run(db, groups=["duplicate-media"])
    assert code == 0 and "Nothing to do" in out, out + err
    assert _stored(db) == once


def test_copies_are_found_and_rewritten_in_a_dictionary_database_at_version_3(tmp_path: Path):
    if shutil.which("brotli") is None:
        _skip("brotli is not on PATH")
    rows, expected = _copy_rows()
    db = _database(tmp_path, rows, dictionary=TEMPLATE_DICTIONARY, schema=3,
                   versions=[(3, 0, 0, "2026-10-19 07:09:52")],
                   templates={1: ("page.peb", _compress(b"<main>{{ body | raw }}</main>", TEMPLATE_DICTIONARY))})
    code, _out, err = _run(db, groups=["duplicate-media"])
    assert code == 0, err
    after = _stored(db)
    for path, text in expected.items():
        assert _decompress(after[path][1], TEMPLATE_DICTIONARY) == text, path


def _unlinked_rows() -> dict:
    """Three rows UNLINKED names, and look-alikes it does not: a longer name, and the same name elsewhere."""
    return {
        "k/package-list": ("text", b"$dokka.format:html-v1\nkotlin"),
        "i/js/hmEmbedHelp.js": ("text/javascript", b"function hmEmbedHelp(){}"),
        "j/html/specs/jdwp/jdwp-protocol.html": ("text/html", b"<h1>JDWP</h1>"),
        "k/package-list-old": ("text", b"$dokka.format:html-v1\nold"),
        "k/kotlin-stdlib/other/package-list": ("text", b"$dokka.format:html-v1\nother"),
        "i/index.html": ("text/html", b'<a href="topic.html">topic</a>'),
    }


def test_unlinked_rows_are_deleted_and_nothing_else(tmp_path: Path):
    db = _database(tmp_path, _unlinked_rows())
    before = _stored(db)
    code, out, err = _run(db, groups=["unlinked"])
    after = _stored(db)
    assert code == 0, err
    assert set(before) - set(after) == {"k/package-list", "i/js/hmEmbedHelp.js", "j/html/specs/jdwp/jdwp-protocol.html"}
    assert all(after[path] == before[path] for path in after)
    assert "unlinked: 3 row(s) of rows nothing links to" in out


def test_a_link_to_an_unlinked_row_refuses(tmp_path: Path):
    rows = _unlinked_rows()
    rows["k/html/a.html"] = ("text/html", b'<a href="../package-list">packages</a>')
    _refused(tmp_path, rows, "k/html/a.html links to k/package-list")


def test_every_unlinked_row_is_listed_once_with_its_reason():
    assert sum(len(paths) for paths in tool._UNLINKED.values()) == len(tool.UNLINKED)
    assert all(why.strip() and paths for why, paths in tool._UNLINKED.items())
    assert not [path for path in tool.UNLINKED if path != path.strip() or any(c in path for c in "*?[")]


def test_a_missing_database_is_an_error(tmp_path: Path):
    code, _out, err = _run(tmp_path / "absent.db")
    assert code == 1 and "does not exist" in err


def _main() -> int:
    """Runs the tests without pytest installed as a runner (`uv run scripts/test_drop_unread_rows.py`)."""
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
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): {type(exc).__name__}: {exc}")
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
