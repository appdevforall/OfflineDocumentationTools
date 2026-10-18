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
test_hm_pages_to_templates.py

Covers hm_pages_to_templates.py: deriving the page template from small
synthetic Help+Manual pages, the JSON each page gets, that every page renders
back byte for byte through the real Pebble engine, that a second run changes
nothing, that a page which does not fit - or does not render back - stays HTML
and is named, and that a dry run writes nothing. For phase 2
(--drop-jstopics): that the jstopics template is stored and the rows it
rebuilds are dropped, chunks included, and that a topic it does not rebuild
keeps its row, is named and fails the run.

The synthetic topics copy the real export's habits that matter here: CRLF
pages with a BOM, jstopics bodies whose line breaks are "\\n\\r", a first and a
last topic with disabled links, a top-level topic without breadcrumbs, a meta
description Help+Manual writes differently from its jstopics value, and a
caption that is not the topic's title.

Run it either way - as a plain script:

    uv run scripts/test_hm_pages_to_templates.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_hm_pages_to_templates.py

Tests that render need `java` (17 or later) and the Pebble 4.1.1, unbescape,
slf4j-api and Gson jars in the Gradle or Maven cache (or $HM_RENDER_CLASSPATH);
without them they are skipped. Every test needs the `brotli` CLI on PATH.
"""
from __future__ import annotations

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

import hm_pages_to_templates as hm  # noqa: E402


class Skip(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest
    except ImportError:
        raise Skip(reason)
    pytest.skip(reason)


_CLASSPATH = []


def _classpath() -> str:
    if not _CLASSPATH:
        try:
            if shutil.which("java") is None:
                raise RuntimeError("no java on PATH")
            _CLASSPATH.append(os.environ.get("HM_RENDER_CLASSPATH") or hm.find_classpath())
        except RuntimeError as exc:
            _CLASSPATH.append(exc)
    if isinstance(_CLASSPATH[0], Exception):
        _skip(f"needs the Pebble renderer: {_CLASSPATH[0]}")
    return _CLASSPATH[0]


def _harness() -> hm.PebbleHarness:
    return hm.PebbleHarness(_classpath())


# --- synthetic Help+Manual topics ----------------------------------------------

FILLER = "".join(f"\t.rule{n} {{ margin: {n}px; }}\r\n" for n in range(120))

TOPICS = [
    # name, title, caption, description (jstopics), description (page), keywords, crumbs
    ("t0", "Start here", "Start here", "Welcome.", "Welcome.", "", ""),
    ("t1", "Build", "Build", 'Tap "Run" to build.', "Tap &quot;Run&quot; to build.", "build & run",
     '<a href="t0.html">Start here</a>'),
    ("t2", "Debug your app", "Debug", "Pair the device. ", "Pair the device.&nbsp;", "",
     '<a href="t0.html">Start here</a>'),
    ("t3", "Keys", "Keys", "Press a key.", "Press a key.", "keys",
     '<a href="t0.html">Start here</a> &gt; <a href="t1.html">Build</a>'),
    ("t4", "Projects", "Projects", "Make a project.", "Make a project.", "", ""),
    ("t5", "The end", "The end", "Bye.", "Bye.", "", '<a href="t4.html">Projects</a>'),
]


def _js(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'").replace("/", "\\/")
            .replace("\n", "\\n").replace("\r", "\\r"))


def _body(index: int, breaks: str = "\n\r") -> str:
    """A body with Help+Manual's jstopics line breaks ("\\n\\r"; the page has
    CRLF), or with `breaks` for an export that writes them the same way in both."""
    return (f'<p class="p_Normal">Topic {index} text, caf\u00e9 \U0001F600, and {{{{ not a tag }}}}.</p>{breaks}'
            f'<p class="p_Normal">Second paragraph of topic {index}.</p>')


def make_topic(index: int, names=None, breaks: str = "\n\r") -> tuple:
    """(page bytes, jstopics bytes) for TOPICS[index]."""
    name, title, caption, desc_js, desc_page, keywords, crumbs = TOPICS[index]
    names = names or [t[0] for t in TOPICS]
    prev = f"{names[index - 1]}.html" if index > 0 else ""
    nxt = f"{names[index + 1]}.html" if index + 1 < len(names) else ""
    parent = "t0.html" if index else "index.html"
    keywords_page = keywords.replace("&", "&amp;")
    lines = [
        "\ufeff<!DOCTYPE html>\r\n", '<html lang="en-us">\r\n', "\t<head>\r\n",
        f"\t<title>{title}</title>\r\n",
        '\t<meta name="generator" content="Help+Manual" />\r\n',
        f'\t<meta name="keywords" content="{keywords_page}" />\r\n',
        f'\t<meta name="description" content="{desc_page}" />\r\n',
        "\t<style>\r\n", FILLER, "\t</style>\r\n",
        "\t<script>var o = {{ a: 1 }}; /* {% not %} {# a tag #} */ var p = {\r\n", "\t};</script>\r\n",
        "</head>\r\n", "<body>\r\n", "<!--ZOOMSTOP-->\r\n",
        (f'\t<p id="ptopic_breadcrumbs">Navigation: {crumbs}</p> \r\n' if crumbs else
         '\t<p id="ptopic_breadcrumbs">Navigation: <span>&raquo;&nbsp;No topics above this level&nbsp;&laquo;'
         '</span></p> \r\n'),
        f'\t<p class="topictitle">{caption}</p>\r\n',
        (f'\t<a id="prev" class="nav" href="{prev}" title="Go to previous topic">\r\n' if prev else
         '\t<a id="prev" class="disabled"  title="This is the first topic">\r\n'),
        "\t<span>Prev</span></a>\r\n",
        (f'\t<a id="next" class="nav" href="{nxt}" title="Go to next topic">\r\n' if nxt else
         '\t<a id="next" class="disabled" title="This is the last topic">\r\n'),
        "\t<span>Next</span></a>\r\n",
        '\t<div id="hmpagebody_scroller">\r\n', "<!--ZOOMRESTART-->\r\n",
        "\t" + _body(index, breaks).replace("\n\r", "\r\n") + "\r\n",
        "\t</div>\r\n",
        f'\thmFlags.thisTopic = "{name}.html";\r\n',
        f'\tvar titleBarText = "{title}";\r\n',
        "</body>\r\n", "</html>\r\n",
    ]
    fields = [("hmKeywords", keywords), ("hmTitle", title), ("hmDescription", desc_js), ("hmPrevLink", prev),
              ("hmNextLink", nxt), ("hmParentLink", parent), ("hmBreadCrumbs", crumbs),
              ("hmTitlePath", f"Root > {title}"),
              ("hmHeader", f'<h1 class="p_Heading1"><span class="f_Heading1">{caption}</span></h1>\n\r'),
              ("hmBody", _body(index, breaks))]
    jstopic = "hmLoadTopic({\r\n" + ",\r\n".join(f'{k}:"{_js(v)}"' for k, v in fields) + "\r\n})\r\n"
    return "".join(lines).encode("utf-8"), jstopic.encode("utf-8")


def make_topics() -> list:
    out = []
    for i, t in enumerate(TOPICS):
        page, jstopic = make_topic(i)
        fields, frame = hm.parse_jstopic(jstopic.decode("utf-8"))
        out.append(hm.Topic(t[0], f"i/{t[0]}.html", page.decode("utf-8"), fields, frame))
    return out


# --- a synthetic documentation.db --------------------------------------------

DICTIONARY = (b'<p class="p_Normal">' * 50 + b"hmLoadTopic({" * 20 + FILLER.encode() * 3)
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
    """Dictionary Brotli at quality 5, independent of the tool's codec: the tool
    compresses at 11, so a row it rewrote never matches one it left alone."""
    with tempfile.TemporaryDirectory() as directory:
        dict_path = Path(directory) / "dictionary.bin"
        dict_path.write_bytes(DICTIONARY)
        args = ["brotli", "-D", str(dict_path)] + (["-d"] if decompress else ["-q", "5"]) + ["-c"]
        return subprocess.run(args, input=data, stdout=subprocess.PIPE, check=True).stdout


def make_db(path: Path, pages: dict = None, template_ids: dict = None, breaks: str = "\n\r") -> Path:
    """A database with the synthetic topics, their jstopics files, a page that
    is not a topic, a page outside i/ and an unrelated template. `pages`
    replaces or adds rows (None leaves a row out); `template_ids` sets rows'
    templateId."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO Languages (value) VALUES ('en-US')")
    conn.execute("INSERT INTO ContentTypes (id, value, compression) VALUES (12, 'text/html', 'brotli')")
    conn.execute("INSERT INTO ContentTypes (id, value, compression) VALUES (6, 'text/javascript', 'brotli')")
    conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (DICTIONARY,))
    conn.execute("INSERT INTO Templates (name, content) VALUES ('page.peb', ?)", (b"<p>{{ title }}</p>",))
    rows = dict(pages or {})
    for i, t in enumerate(TOPICS):
        page, jstopic = make_topic(i, breaks=breaks)
        rows.setdefault(f"i/{t[0]}.html", page)
        rows.setdefault(f"i/jstopics/{t[0]}.js", jstopic)
    rows.setdefault("i/hmftsearch.html", b"\xef\xbb\xbf<!DOCTYPE html>\r\n<p>Search</p>\r\n")
    rows.setdefault("a/other.html", b"<p>Not Help+Manual</p>")
    for p, data in sorted(rows.items()):
        if data is None:
            continue
        type_id = 6 if p.endswith(".js") else 12
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, ?)",
                     (p, data if data.startswith(b"RAW:") else _brotli(data), type_id, (template_ids or {}).get(p, 0)))
    conn.commit()
    conn.close()
    return path


def rows(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT path, content, languageID, contentTypeID, templateId FROM Content ORDER BY path")}
    finally:
        conn.close()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tool(db: Path, **extra) -> tuple:
    cfg = {"db_path": db, "dry_run": False, "workers": 4, "verbose": True, "prefix": "i/",
           "classpath": _classpath(), "java": "java", "jstopic_template_out": None}
    cfg.update(extra)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hm.run(cfg)
    return code, out.getvalue(), err.getvalue()


def backups(db: Path) -> list:
    return sorted(db.parent.glob(db.name + ".backup-*"))


# --- derivation ----------------------------------------------------------------

def test_split_lines_keeps_every_byte():
    for text in ["", "a", "a\r\nb", "a\n\rb\r", "\r\n\r\n", "x\ry\nz\r\n"]:
        assert "".join(hm.split_lines(text)) == text
    assert hm.split_lines("a\r\nb\rc\nd") == ["a\r\n", "b\r", "c\n", "d"]


def test_parse_jstopic_unescapes_fields():
    _page, jstopic = make_topic(1)
    fields, frame = hm.parse_jstopic(jstopic.decode("utf-8"))
    assert fields["hmDescription"] == 'Tap "Run" to build.'
    assert fields["hmBreadCrumbs"] == '<a href="t0.html">Start here</a>'
    assert fields["hmBody"].count("\n\r") == 1
    assert '"' not in frame.replace('""', "")
    assert hm.parse_jstopic('hmLoadTopic({hmTitle:"x"})') is None


def test_template_has_a_hole_for_each_per_topic_part():
    d = hm.derive(make_topics())
    t = d.template
    assert not d.unfit, d.unfit
    assert "\t<title>{{ hmTitle | raw }}</title>" in t
    assert '<meta name="keywords" content="{{ hmKeywords | raw }}" />' in t
    assert '<meta name="description" content="{{ hmDescription | raw }}" />' in t
    assert "{% if hmBreadCrumbs is empty %}" in t
    assert "Navigation: {{ hmBreadCrumbs | raw }}</p>" in t
    assert "{% if hmPrevLink is empty %}" in t and 'href="{{ hmPrevLink | raw }}"' in t
    assert "{% if hmNextLink is empty %}" in t and 'href="{{ hmNextLink | raw }}"' in t
    assert "\t{{ hmBody | raw }}\r\n" in t
    assert 'hmFlags.thisTopic = "{{ file | raw }}";' in t
    assert "{{ (part_topictitle is null ? hmTitle : part_topictitle) | raw }}" in t
    # The skin is in the template once, and nothing that belongs to one topic is.
    assert t.count(".rule119 { margin: 119px; }") == 1
    for _name, title, *_rest in TOPICS:
        assert f">{title}<" not in t
    assert "Second paragraph" not in t
    # Delimiters in the skin's own text are escaped, not left to Pebble.
    assert 'var o = {{ "{" }}{ a: 1 }}; /* {{ "{" }}% not %} {{ "{" }}# a tag #} */' in t


def test_json_holds_each_part_as_its_page_shows_it():
    d = hm.derive(make_topics())
    c1, c2 = d.contexts["t1"], d.contexts["t2"]
    assert c1["hmDescription"] == "Tap &quot;Run&quot; to build."       # escaped, as in the page
    assert c1["js_hmDescription"] == 'Tap "Run" to build.'              # the jstopics value
    assert c1["hmKeywords"] == "build &amp; run" and c1["js_hmKeywords"] == "build & run"
    assert c2["hmDescription"] == "Pair the device.&nbsp;"              # read out of the page itself
    assert c2["js_hmDescription"] == "Pair the device. "
    assert c2["part_topictitle"] == "Debug" and c2["hmTitle"] == "Debug your app"
    assert "part_topictitle" not in c1 and "js_hmDescription" not in d.contexts["t3"]
    assert c1["hmBody"] == _body(1).replace("\n\r", "\r\n")
    assert c1["file"] == "t1.html" and c1["hmTitlePath"] == "Root > Build"
    assert c1["hmHeader"].endswith("</h1>\n\r")
    assert list(c1)[:2] == ["file", "hmKeywords"]


def test_emit_survives_pebble_lexing():
    texts = ["a{{b", "{%", "{#c#}", "x{", "\r\nafter", "\n\rafter", "\rafter", "\nafter", "\u2028after",
             "\r\r\nafter", "}} %} #}", "{{{"]
    harness = _harness()
    try:
        for text in texts:
            segments = [hm.tag_seg("{{ v | raw }}"), hm.text_seg(text), hm.tag_seg("{{ v | raw }}"),
                        hm.text_seg(text)]
            harness.define("t", hm.emit(segments))
            ok, out, _ = harness.render("t", json.dumps({"v": "V"}).encode())
            assert ok and out.decode("utf-8") == "V" + text + "V" + text, (text, out)
    finally:
        harness.close()


def test_every_page_renders_back_byte_for_byte():
    topics = make_topics()
    d = hm.derive(topics)
    harness = _harness()
    try:
        harness.define(hm.PAGE_TEMPLATE, d.template)
        for t in topics:
            ok, out, _ = harness.render(hm.PAGE_TEMPLATE, hm.context_json(d.contexts[t.name]))
            assert ok, out
            assert out == t.page.encode("utf-8"), t.name
    finally:
        harness.close()


def test_jstopic_template_rebuilds_each_topics_fields():
    topics = make_topics()
    d = hm.derive(topics)
    harness = _harness()
    try:
        harness.define(hm.JSTOPIC_TEMPLATE, d.jstopic_template)
        for t in topics:
            ok, out, _ = harness.render(hm.JSTOPIC_TEMPLATE, hm.context_json(d.contexts[t.name]))
            assert ok, out
            fields, frame = hm.parse_jstopic(out.decode("utf-8"))
            assert fields == t.fields, t.name
            assert frame == t.frame
    finally:
        harness.close()


# --- runs against a database -----------------------------------------------------

def test_run_stores_json_and_points_rows_at_the_template(tmp_path):
    db = make_db(tmp_path / "doc.db")
    before = rows(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    after = rows(db)
    conn = sqlite3.connect(db)
    template_id, template = conn.execute("SELECT id, content FROM Templates WHERE name = ?",
                                         (hm.PAGE_TEMPLATE,)).fetchone()
    assert conn.execute("SELECT content FROM Templates WHERE name = 'page.peb'").fetchone()[0] == b"<p>{{ title }}</p>"
    assert conn.execute("SELECT 1 FROM Templates WHERE name = ?", (hm.JSTOPIC_TEMPLATE,)).fetchone() is None
    conn.close()
    assert template.decode("utf-8").startswith("{#")
    for t in TOPICS:
        path = f"i/{t[0]}.html"
        content, language, type_id, tid = after[path]
        assert (language, type_id, tid) == (before[path][1], before[path][2], template_id)
        context = json.loads(_brotli(content, decompress=True))
        assert context["file"] == f"{t[0]}.html"
        assert after[f"i/jstopics/{t[0]}.js"] == before[f"i/jstopics/{t[0]}.js"]
    for path in ("i/hmftsearch.html", "a/other.html"):
        assert after[path] == before[path]
    assert len(backups(db)) == 1
    assert "6 of 6 topic page(s) render back byte for byte" in out


def test_second_run_changes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    code, _out, err = run_tool(db)
    assert code == 0, err
    digest = sha(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    assert "Nothing to change." in out
    assert sha(db) == digest
    assert len(backups(db)) == 1


def test_dry_run_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    digest = sha(db)
    code, out, err = run_tool(db, dry_run=True)
    assert code == 0, err
    assert "Would write the template (changed), 6 page(s) as JSON" in out
    assert "Dry run: nothing written." in out
    assert sha(db) == digest
    assert backups(db) == []


def test_page_that_does_not_fit_stays_html(tmp_path):
    odd = ("\ufeff<html><body>\r\n" + "".join(f"<p>other skin {n}</p>\r\n" for n in range(150))
           + "\t" + _body(3).replace("\n\r", "\r\n") + "\r\n</body></html>\r\n").encode("utf-8")
    db = make_db(tmp_path / "doc.db", {"i/t3.html": odd})
    before = rows(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    after = rows(db)
    assert after["i/t3.html"] == before["i/t3.html"]
    assert "stays HTML: i/t3.html - shares only" in err
    assert "5 of 6 topic page(s) render back byte for byte" in out and "1 stay HTML" in out
    assert after["i/t4.html"][3] != 0


def test_page_whose_render_differs_stays_html(tmp_path):
    db = make_db(tmp_path / "doc.db")
    before = rows(db)
    real = _harness()

    class Corrupting:
        """The real renderer, except one page comes back one byte short."""

        def define(self, name, text):
            real.define(name, text)

        def render(self, name, context):
            ok, out, nanos = real.render(name, context)
            if ok and name == hm.PAGE_TEMPLATE and b'"file":"t2.html"' in context:
                out = out[:-1]
            return ok, out, nanos

    try:
        code, out, err = run_tool(db, renderer=Corrupting())
    finally:
        real.close()
    assert code == 0, err
    after = rows(db)
    assert after["i/t2.html"] == before["i/t2.html"]
    assert "stays HTML: i/t2.html - its rendered page differs from the stored one" in err
    assert after["i/t1.html"][3] != 0


def test_unreadable_row_is_named_and_left_alone(tmp_path):
    db = make_db(tmp_path / "doc.db", {"i/t4.html": b"RAW:not brotli at all"})
    code, out, err = run_tool(db)
    assert code == 1
    assert "skipped i/t4.html: could not be read" in err
    assert rows(db)["i/t4.html"][0] == b"RAW:not brotli at all"


# --- phase 2: --drop-jstopics ---------------------------------------------------

def jstopic_rows(db: Path) -> list:
    return sorted(p for p in rows(db) if p.startswith("i/jstopics/"))


def test_drop_jstopics_stores_the_template_and_drops_every_row(tmp_path):
    db = make_db(tmp_path / "doc.db")
    before = rows(db)
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    after = rows(db)
    assert jstopic_rows(db) == []
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT content FROM Templates WHERE name = ?", (hm.JSTOPIC_TEMPLATE,)).fetchone()[0]
    conn.close()
    assert stored.decode("utf-8") == hm.derive(make_topics()).jstopic_template
    assert stored.decode("utf-8").startswith("hmLoadTopic({\r\nhmKeywords:\"{{ ")
    for path in ("i/hmftsearch.html", "a/other.html"):
        assert after[path] == before[path]
    assert all(after[f"i/{t[0]}.html"][3] != 0 for t in TOPICS)
    assert "Dropping 6 jstopics row(s)" in out and len(backups(db)) == 1


def test_drop_jstopics_after_a_phase_1_run_leaves_the_pages_alone(tmp_path):
    db = make_db(tmp_path / "doc.db")
    code, _out, err = run_tool(db)
    assert code == 0, err
    pages = {p: v for p, v in rows(db).items() if not p.startswith("i/jstopics/")}
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    assert jstopic_rows(db) == []
    assert {p: v for p, v in rows(db).items()} == pages
    assert "Writing the template" not in out


def test_drop_jstopics_second_run_changes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    code, _out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    digest = sha(db)
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    assert "No jstopics rows left to drop." in out and "Nothing to change." in out
    assert sha(db) == digest and len(backups(db)) == 1


def test_drop_jstopics_dry_run_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    digest = sha(db)
    code, out, err = run_tool(db, drop_jstopics=True, dry_run=True)
    assert code == 0, err
    assert "Would drop 6 jstopics row(s)" in out
    assert sha(db) == digest and backups(db) == []


def test_later_run_checks_what_the_stored_jstopic_template_builds(tmp_path):
    db = make_db(tmp_path / "doc.db")
    code, _out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    assert "6 of 6 topic(s) without a jstopics row get one built from their JSON" in out
    conn = sqlite3.connect(db)
    text = conn.execute("SELECT content FROM Templates WHERE name = ?", (hm.JSTOPIC_TEMPLATE,)).fetchone()[0]
    conn.execute("UPDATE Templates SET content = ? WHERE name = ?",
                 (text.replace(b'hmTitle:"{{ hmTitle', b'hmTitle:"x{{ hmTitle'), hm.JSTOPIC_TEMPLATE))
    conn.commit()
    conn.close()
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 1
    assert "broken: i/jstopics/t0.js - the jstopics file built from its JSON" in err


def test_jstopic_the_template_does_not_rebuild_keeps_its_row(tmp_path):
    db = make_db(tmp_path / "doc.db")
    before = rows(db)
    real = _harness()

    class Corrupting:
        """The real renderer, except t2's jstopics file comes back with another title."""

        def define(self, name, text):
            real.define(name, text)

        def render(self, name, context):
            ok, out, nanos = real.render(name, context)
            if ok and name == hm.JSTOPIC_TEMPLATE and b'"file":"t2.html"' in context:
                out = out.replace(b'hmTitle:"Debug your app"', b'hmTitle:"Debug your ap"')
            return ok, out, nanos

    try:
        code, out, err = run_tool(db, drop_jstopics=True, renderer=Corrupting())
    finally:
        real.close()
    assert code == 1
    assert jstopic_rows(db) == ["i/jstopics/t2.js"]
    assert rows(db)["i/jstopics/t2.js"] == before["i/jstopics/t2.js"]
    assert "keeps i/jstopics/t2.js - the built file differs in hmTitle" in err
    assert "1 jstopics row(s) left in place" in err


def test_jstopic_with_other_text_around_its_fields_keeps_its_row(tmp_path):
    page, jstopic = make_topic(3)
    odd = jstopic.replace(b"hmLoadTopic({", b"hmLoadTopic( {")
    db = make_db(tmp_path / "doc.db", {"i/jstopics/t3.js": odd})
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 1
    assert jstopic_rows(db) == ["i/jstopics/t3.js"]
    assert "keeps i/jstopics/t3.js - the built file differs outside its field values" in err


def test_topic_whose_page_stays_html_keeps_its_jstopics_row(tmp_path):
    odd = ("\ufeff<html><body>\r\n" + "".join(f"<p>other skin {n}</p>\r\n" for n in range(150))
           + "\t" + _body(3).replace("\n\r", "\r\n") + "\r\n</body></html>\r\n").encode("utf-8")
    db = make_db(tmp_path / "doc.db", {"i/t3.html": odd})
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 1
    assert jstopic_rows(db) == ["i/jstopics/t3.js"]
    assert "keeps i/jstopics/t3.js - its page stays HTML" in err


def test_drop_jstopics_deletes_a_chunked_row_whole(tmp_path):
    """A jstopics row stored in chunks goes with all of them. CHUNK_SIZE is
    made tiny for the test so a small row can be stored the chunked way."""
    import optimize_db_media
    saved = optimize_db_media.CHUNK_SIZE
    optimize_db_media.CHUNK_SIZE = 40
    try:
        db = make_db(tmp_path / "doc.db")
        conn = sqlite3.connect(db)
        blob = conn.execute("SELECT content FROM Content WHERE path = 'i/jstopics/t5.js'").fetchone()[0]
        assert len(blob) > 80 and len(blob) % 40
        conn.execute("UPDATE Content SET content = ? WHERE path = 'i/jstopics/t5.js'", (blob[:40],))
        for n, start in enumerate(range(40, len(blob), 40), start=1):
            conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 6)",
                         (f"i/jstopics/t5.js-{n}", blob[start:start + 40]))
        conn.commit()
        conn.close()
        assert len(jstopic_rows(db)) > 7
        code, out, err = run_tool(db, drop_jstopics=True)
        assert code == 0, err
        assert jstopic_rows(db) == []
        assert "left alone" not in err
    finally:
        optimize_db_media.CHUNK_SIZE = saved


# --- review of PR #43 -------------------------------------------------------------

def test_topic_page_not_named_html_keeps_its_jstopics_row(tmp_path):
    """R43-01: the app builds jstopics/X.js only from X.html, so X.htm is not a topic page."""
    page, _jstopic = make_topic(5)
    db = make_db(tmp_path / "doc.db", {"i/t5.html": None, "i/t5.htm": page})
    before = rows(db)
    code, out, err = run_tool(db, drop_jstopics=True)
    after = rows(db)
    assert code == 1
    assert after["i/t5.htm"] == before["i/t5.htm"]
    assert jstopic_rows(db) == ["i/jstopics/t5.js"] and after["i/jstopics/t5.js"] == before["i/jstopics/t5.js"]
    assert "left alone: i/jstopics/t5.js" in err
    assert "Found 5 Help+Manual topic page(s)" in out


def _json_row(db: Path, path: str) -> dict:
    conn = sqlite3.connect(db)
    try:
        return json.loads(_brotli(conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0],
                                  decompress=True))
    finally:
        conn.close()


def _set_json_row(db: Path, path: str, context: dict) -> None:
    conn = sqlite3.connect(db)
    conn.execute("UPDATE Content SET content = ? WHERE path = ?", (_brotli(hm.context_json(context)), path))
    conn.commit()
    conn.close()


def test_page_back_to_html_gets_its_dropped_jstopics_row_back(tmp_path):
    """R43-02: a page that no longer fits the template goes back to HTML; its
    jstopics row, dropped earlier, is built again from the stored template."""
    db = make_db(tmp_path / "doc.db")
    code, _out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    context = _json_row(db, "i/t2.html")
    context["hmTitle"] = "Debug\r\n" * 200 + "your app"      # pushes its page far off the others' shell
    _set_json_row(db, "i/t2.html", context)
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 1
    assert "writes back i/jstopics/t2.js" in err
    after = rows(db)
    assert after["i/t2.html"][3] == 0
    assert _brotli(after["i/t2.html"][0], decompress=True).count(b"Debug\r\n") > 200
    content, _lang, type_id, tid = after["i/jstopics/t2.js"]
    assert (type_id, tid) == (6, 0)
    fields, _frame = hm.parse_jstopic(_brotli(content, decompress=True).decode("utf-8"))
    assert fields == hm.jstopic_fields_from_context(context, hm.context_fields(context), hm.BODY_FORMS[0])
    assert jstopic_rows(db) == ["i/jstopics/t2.js"]


def test_page_back_to_html_with_no_way_to_rebuild_its_row_writes_nothing(tmp_path):
    db = make_db(tmp_path / "doc.db")
    code, _out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    context = _json_row(db, "i/t2.html")
    context["hmTitle"] = "Debug\r\n" * 200 + "your app"
    _set_json_row(db, "i/t2.html", context)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM Templates WHERE name = ?", (hm.JSTOPIC_TEMPLATE,))
    conn.commit()
    conn.close()
    digest = sha(db)
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 1
    assert "refused: i/t2.html - its page goes back to HTML, its jstopics row is gone" in err
    assert sha(db) == digest


def test_export_whose_bodies_keep_their_crlf_runs_twice_cleanly(tmp_path):
    """R43-05: bodies written with the same breaks in page and jstopics take the
    form with no filter, and a later run reads that form from the stored template."""
    db = make_db(tmp_path / "doc.db", breaks="\r\n")
    code, _out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT content FROM Templates WHERE name = ?", (hm.JSTOPIC_TEMPLATE,)).fetchone()[0]
    conn.close()
    assert b"replace(" not in stored
    assert "js_hmBody" not in _json_row(db, "i/t1.html")
    digest = sha(db)
    code, out, err = run_tool(db, drop_jstopics=True)
    assert code == 0, err
    assert "6 of 6 topic(s) without a jstopics row get one built" in out and "Nothing to change." in out
    assert sha(db) == digest


def test_pebble_empty_agrees_with_pebbles_is_empty():
    values = ["", " ", "\t\r\n", "\u00a0", "a", " a ", "\x0b"]
    harness = _harness()
    try:
        harness.define("t", "{% if x is empty %}E{% else %}N{% endif %}")
        for value in values:
            ok, out, _ = harness.render("t", json.dumps({"x": value}).encode())
            assert ok and (out == b"E") == hm.pebble_empty(value), repr(value)
    finally:
        harness.close()


def test_rows_the_step_does_not_own_are_left_alone(tmp_path):
    """A page under another template, a page in a subfolder, and pages whose
    prefix matches only without case: none is a topic page."""
    page1, jstopic1 = make_topic(1)
    extra = {"i/t9.html": page1, "i/jstopics/t9.js": jstopic1,
             "i/sub/t1.html": page1, "i/jstopics/sub/t1.js": jstopic1,
             "I/t1.html": page1, "I/jstopics/t1.js": jstopic1}
    db = make_db(tmp_path / "doc.db", extra, template_ids={"i/t9.html": 1})
    before = rows(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    after = rows(db)
    for path in extra:
        assert after[path] == before[path], path
    assert "skipped i/t9.html: rendered through another template (templateId 1)" in err
    assert "Found 6 Help+Manual topic page(s)" in out


def test_chunked_topic_page_is_read_whole_and_rewritten(tmp_path):
    import optimize_db_media
    saved = optimize_db_media.CHUNK_SIZE
    optimize_db_media.CHUNK_SIZE = 64
    try:
        db = make_db(tmp_path / "doc.db")
        conn = sqlite3.connect(db)
        blob = conn.execute("SELECT content FROM Content WHERE path = 'i/t4.html'").fetchone()[0]
        assert len(blob) % 64
        conn.execute("UPDATE Content SET content = ? WHERE path = 'i/t4.html'", (blob[:64],))
        for n, start in enumerate(range(64, len(blob), 64), start=1):
            conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 12)",
                         (f"i/t4.html-{n}", blob[start:start + 64]))
        conn.commit()
        conn.close()
        code, out, err = run_tool(db)
        assert code == 0, err
        after = rows(db)
        assert not [p for p in after if p.startswith("i/t4.html-")]
        assert after["i/t4.html"][3] != 0 and "6 of 6 topic page(s) render back" in out
    finally:
        optimize_db_media.CHUNK_SIZE = saved


def test_page_with_a_nul_stays_html(tmp_path):
    page, _jstopic = make_topic(3)
    db = make_db(tmp_path / "doc.db", {"i/t3.html": page.replace(b".rule7 {", b".rule7\x00 {")})
    before = rows(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    assert rows(db)["i/t3.html"] == before["i/t3.html"]
    assert "stays HTML: i/t3.html - the page contains a NUL character" in err


def test_page_sharing_three_quarters_of_its_lines_stays_html(tmp_path):
    page, _jstopic = make_topic(3)
    text = page.decode("utf-8")
    for n in range(25):
        text = text.replace(f".rule{n} {{ margin: {n}px; }}", f".other{n} {{ padding: {n}px; }}")
    db = make_db(tmp_path / "doc.db", {"i/t3.html": text.encode("utf-8")})
    before = rows(db)
    code, out, err = run_tool(db)
    assert code == 0, err
    assert rows(db)["i/t3.html"] == before["i/t3.html"]
    shared = re.search(r"stays HTML: i/t3.html - shares only (\d+)% of its lines", err)
    assert shared and 60 < int(shared.group(1)) < 90, err


def test_classpath_uses_the_gson_the_app_renders_with():
    classpath = _classpath()
    if os.environ.get("HM_RENDER_CLASSPATH"):
        _skip("the classpath was given, not found")
    assert "gson-2.12.1.jar" in classpath and "pebble-4.1.1.jar" in classpath


def _main() -> int:
    """Runs the tests without pytest, so this file works the way the tools
    beside it do (`uv run scripts/test_hm_pages_to_templates.py`)."""
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
