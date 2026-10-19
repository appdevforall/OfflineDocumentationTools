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
and nothing else; that scripting is turned off in the viewer; and that every
check refuses the run, writing nothing, when a kept row, template, tooltip or
Bookshelf entry refers to a deleted row, when a row to delete is not what its
group expects, when a stored PDF needs a CMap, when the viewer cannot be
edited, or when a kept row cannot be read. A run is all or nothing, a dry run
writes nothing, and a second run changes nothing.

Run it either way - as a plain script:

    uv run scripts/test_drop_unread_rows.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_drop_unread_rows.py

The dictionary test needs the `brotli` CLI on PATH, as the tool itself does.
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

TYPES = {"text/css": (1, "brotli"), "image/svg+xml": (2, "brotli"), "text/javascript": (6, "brotli"),
         "application/json": (8, "brotli"), "text/html": (12, "brotli"), "text": (13, "none"),
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
              buttons: list = None, tooltips: list = None, schema: int = 2) -> Path:
    """A miniature documentation database with the production schema's triggers and the tooltip tables of
    documentation schema 2 or 3. `templates` maps id -> (name, body), `buttons` lists (tooltipId, uri) and
    `tooltips` (id, summary, detail)."""
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


def test_a_reference_the_allowlist_names_only_for_another_row_still_refuses(tmp_path: Path):
    rows = _base_rows()
    rows["p/web/other.mjs"] = ("text/javascript", b'import("../build/pdf.sandbox.mjs")')
    _refused(tmp_path, rows, "p/web/other.mjs links to p/build/pdf.sandbox.mjs")


def test_a_template_link_refuses(tmp_path: Path):
    rows = _base_rows()
    rows["k/topic.html"] = ("text/html", b'{"title": "t"}', 1)
    _refused(tmp_path, rows, "template page.peb links to x/a.html",
             templates={1: ("page.peb", b'<a href="../x/a.html">{{ title }}</a>')})


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
    ]
    for i, (pdf, expected) in enumerate(cases):
        rows = _base_rows()
        rows["d/cjk.pdf"] = ("application/pdf", pdf)
        _refused(tmp_path / str(i), rows, f"d/cjk.pdf {expected}")


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
