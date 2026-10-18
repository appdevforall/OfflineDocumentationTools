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
test_share_skin_sprite.py

Covers share_skin_sprite.py: that it replaces Help+Manual's inline icon sprite
in each i/ page with one <script> tag and changes no other byte, that the
i/js/hm_sprite.js it writes gives back exactly the markup it took out, that
every row it does not change keeps its stored bytes, and that every sprite it
cannot share safely is reported and left alone.

The sprite used here is a small one in the skin's shape - the real opening tag,
CRLF line endings, tabs - with the characters a JavaScript string has to escape
added: double and single quotes, a backslash, "</" and "<!--". The script is
checked two ways: its string literal is decoded by a parser written here, which
shares nothing with the json module the tool escapes with, and, when `node` is
on PATH, the script is run against a stand-in document.currentScript and what
it inserts is compared. Stored rows are compressed at a quality the tool never
uses, so a row it re-compressed cannot pass for one it left alone.

Run it either way - as a plain script, needing nothing installed:

    uv run scripts/test_share_skin_sprite.py

or under pytest, which needs the same packages on the path:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_share_skin_sprite.py

The dictionary tests need the `brotli` CLI on PATH, as the tool itself does.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import share_skin_sprite as tool  # noqa: E402

CHUNK_SIZE = 1024 * 1024

SPRITE = (
    b'<svg version="1.1" xmlns="http://www.w3.org/2000/svg" style="display: none;">\r\n<defs>\r\n'
    b'<g class="svgsprite" id="menu" width="32" height="32" viewBox="0 0 32 32">\r\n'
    b'<path d="M27.424 24v2.272q0 0.48-0.32 0.8t-0.8 0.352h-25.12z"></path>\r\n\t</g>\r\n'
    b"<!-- the skin's own note -->\r\n"
    b'<g class="svgsprite" id="eye-off" viewBox="0 0 32 32"><title>Hide \'expanding\' text \\ </title>'
    b'<path d="M1 1h30z"></path>\r\n\t</g>\r\n</defs>\r\n</svg>'
)
VARIANT = SPRITE.replace(b'id="menu"', b'id="menu2"')

# A Help+Manual page reduced to what matters here: the sprite opens <body>,
# after a comment, and <use> references to it follow.
HEAD = (b'<!DOCTYPE html>\r\n<html lang="en">\r\n<head>\r\n<title>Install add-ons</title>\r\n'
        b'<script type="text/javascript" src="./js/jquery.js"></script>\r\n</head>\r\n'
        b'<body>\r\n<!--ZOOMSTOP-->\r\n')
TAIL = (b'\r\n<div id="fouc"></div>\r\n<a class="topicnavlink"><svg class="navicon" viewBox="0 0 27 32">'
        b'<use xlink:href="#menu"></use></svg></a>\r\n<!--ZOOMRESTART-->\r\n<p>Install an add-on.</p>\r\n'
        b'</body>\r\n</html>\r\n')
PAGE = HEAD + SPRITE + TAIL
EXPECTED = HEAD + tool.SCRIPT_TAG + TAIL

# The i/js/ rows' languageID and templateId differ from the pages' (1, 0), so a
# script row carrying them was given them from i/js/, not from a page.
JS_LANGUAGE, JS_TEMPLATE = 4, 5
OTHER_JS = {"i/js/hm_webhelp.js": ("text/javascript", b"var hmWebHelp = {};", JS_LANGUAGE, JS_TEMPLATE)}

DICTIONARY = (HEAD + TAIL) * 4 + SPRITE


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


def _database(tmp_path: Path, rows: dict, dictionary: bytes = None, html_compression: str = "brotli",
              with_js_type: bool = True) -> Path:
    """A miniature documentation database: ContentTypes, Content, and - when
    `dictionary` is given - a CompressionDictionary every brotli row is
    compressed against. `rows` maps path -> (content type, decoded bytes
    [, languageID, templateId [, stored as they are]]); languageID and
    templateId default to 1 and 0."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, "
        "compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, "
        "templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
    )
    types = {"text/html": (12, html_compression), "text/plain": (5, "none")}
    if with_js_type:
        types["text/javascript"] = (6, "brotli")
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)",
                     [(type_id, value, compression) for value, (type_id, compression) in types.items()])
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary,))
    for path, (content_type, raw, *extra) in rows.items():
        language_id, template_id, as_is = (list(extra) + [1, 0, False][len(extra):])[:3]
        type_id, compression = types.get(content_type, (99, "brotli"))
        stored = raw if as_is or compression == "none" else _compress(raw, dictionary)
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, ?, ?, ?, ?)", (path, language_id, stored, type_id, template_id))
    conn.commit()
    conn.close()
    return db_path


def _stored(db_path: Path) -> dict:
    """{path: (id, languageID, content, contentTypeID, templateId)} for every row."""
    conn = sqlite3.connect(db_path)
    try:
        return {row[0]: row[1:] for row in conn.execute(
            "SELECT path, id, languageID, content, contentTypeID, templateId FROM Content")}
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


_SIMPLE_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}


def _js_literal_at(source: str, start: int) -> tuple:
    """(decoded value, index just past it) of the JavaScript string literal
    opening at source[start], by the language's rules for the escapes a
    literal may hold - written here rather than borrowed from json, which the
    tool itself uses."""
    quote = source[start]
    assert quote in "'\"", f"no string literal at {start}"
    units, i = [], start + 1
    while True:
        ch = source[i]
        if ch == quote:
            text = "".join(units).encode("utf-16-le", "surrogatepass").decode("utf-16-le")
            return text, i + 1
        assert ch not in "\r\n\u2028\u2029", "a line terminator inside a string literal is a syntax error"
        if ch != "\\":
            units.append(ch)
            i += 1
            continue
        escape = source[i + 1]
        if escape == "u":
            units.append(chr(int(source[i + 2:i + 6], 16)))
            i += 6
        elif escape == "x":
            units.append(chr(int(source[i + 2:i + 4], 16)))
            i += 4
        elif escape in _SIMPLE_ESCAPES and not (escape == "0" and source[i + 2].isdigit()):
            units.append(_SIMPLE_ESCAPES[escape])
            i += 2
        else:
            assert escape not in "0123456789\r\n", f"escape \\{escape} is not allowed here"
            units.append(escape)  # \" \' \\ \/ \! stand for themselves
            i += 2


def _inserted_markup(script: bytes) -> str:
    """The markup the script passes to insertAdjacentHTML, decoded from its
    source text."""
    source = script.decode("ascii")
    call = 'script.insertAdjacentHTML("beforebegin", '
    assert source.count(call) == 1
    value, end = _js_literal_at(source, source.index(call) + len(call))
    assert source[end:end + 2] == ");"
    return value


_NODE_HARNESS = r"""
const calls = [];
const parent = { removeChild(node) { calls.push(["removeChild", node === script]); } };
const script = {
    parentNode: parent,
    insertAdjacentHTML(position, html) { calls.push(["insertAdjacentHTML", position, html]); },
};
globalThis.document = { currentScript: script };
require("vm").runInThisContext(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(calls));
"""


def test_find_sprite_spans_exactly_the_block():
    assert tool.find_sprite(PAGE) == ((len(HEAD), len(HEAD) + len(SPRITE)), None)
    assert tool.replace_block(PAGE, (len(HEAD), len(HEAD) + len(SPRITE))) == EXPECTED


def test_the_block_is_replaced_and_every_other_byte_kept(tmp_path: Path):
    """The page loses the block and nothing else: the CRLFs either side, the
    comments and the <use> references all survive. The row keeps its id,
    language, content type and templateId."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE), **OTHER_JS})
    before = _stored(db_path)
    code, out, err = _run(db_path)
    assert (code, err) == (0, "")
    after = _stored(db_path)
    assert _decompress(after["i/page.html"][2]) == EXPECTED
    row_id, language_id, _content, content_type_id, template_id = after["i/page.html"]
    assert (row_id, language_id, content_type_id, template_id) == (
        before["i/page.html"][:2] + before["i/page.html"][3:])
    assert "1 page(s) carry the sprite block" in out
    assert f"{len(SPRITE) - len(tool.SCRIPT_TAG):,} decoded" in out


def test_the_script_row(tmp_path: Path):
    """Written as text/javascript, brotli as that ContentTypes row says, with the
    languageID and templateId of the other i/js/ rows - not the page's."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE), **OTHER_JS})
    assert _run(db_path)[0] == 0
    _row_id, language_id, content, content_type_id, template_id = _stored(db_path)[tool.SCRIPT_PATH]
    assert (language_id, content_type_id, template_id) == (JS_LANGUAGE, 6, JS_TEMPLATE)
    script = _decompress(content)
    assert script == tool.script_for(SPRITE)
    assert script.isascii()
    assert b"</" not in script
    assert b"<!--" not in script


def test_the_script_string_decodes_to_the_block():
    """Parsed as JavaScript source, the string handed to insertAdjacentHTML is
    the block, every byte - CRs, tabs, quotes and the backslash included."""
    assert _inserted_markup(tool.script_for(SPRITE)).encode("ascii") == SPRITE
    assert _inserted_markup(tool.script_for(VARIANT)).encode("ascii") == VARIANT


def test_running_the_script_inserts_the_block_before_itself():
    """Run by node against a stand-in document.currentScript: it inserts
    exactly the block "beforebegin" - where its own element is - and then
    removes that element, in that order."""
    node = shutil.which("node")
    if node is None:
        _skip("node is not on PATH")
    with tempfile.TemporaryDirectory() as directory:
        harness = Path(directory) / "harness.js"
        harness.write_text(_NODE_HARNESS)
        result = subprocess.run([node, str(harness)], input=tool.script_for(SPRITE), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=True)
    assert json.loads(result.stdout) == [["insertAdjacentHTML", "beforebegin", SPRITE.decode("ascii")],
                                         ["removeChild", True]]


def test_js_string_escapes_what_could_end_a_script():
    text = "a</script><!-- b \\ \" ' \r\n\t\u2028"
    literal = tool.js_string(text)
    assert "</" not in literal
    assert "<!--" not in literal
    assert literal.isascii()
    assert _js_literal_at(literal, 0) == (text, len(literal))


def test_a_differing_sprite_is_reported_and_left_alone(tmp_path: Path):
    """The block most pages carry is shared; a page carrying another is named,
    keeps its stored bytes, and fails the run."""
    db_path = _database(tmp_path, {"i/a.html": ("text/html", PAGE), "i/b.html": ("text/html", PAGE),
                                   "i/odd.html": ("text/html", HEAD + VARIANT + TAIL), **OTHER_JS})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    after = _stored(db_path)
    assert after["i/odd.html"] == before["i/odd.html"], "i/odd.html changed"
    assert code == 1
    assert _decompress(after["i/a.html"][2]) == EXPECTED and _decompress(after["i/b.html"][2]) == EXPECTED
    assert _decompress(after[tool.SCRIPT_PATH][2]) == tool.script_for(SPRITE)
    assert "skipped i/odd.html" in err and "differs from the one 2 page(s) carry" in err
    assert "i/a.html" not in err and "i/b.html" not in err


def test_no_most_common_sprite_changes_nothing(tmp_path: Path):
    """One page each of two blocks: neither is picked, both are named."""
    db_path = _database(tmp_path, {"i/a.html": ("text/html", PAGE),
                                   "i/b.html": ("text/html", HEAD + VARIANT + TAIL), **OTHER_JS})
    before = db_path.read_bytes()
    code, _out, err = _run(db_path)
    assert code == 1
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))
    assert err.count("none carried by more pages than every other") == 2


def test_a_page_without_the_sprite_keeps_its_stored_bytes(tmp_path: Path):
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE),
                                   "i/hmcontent.html": ("text/html", HEAD + TAIL), **OTHER_JS})
    before = _stored(db_path)
    assert _run(db_path)[0] == 0
    after = _stored(db_path)
    assert after["i/hmcontent.html"] == before["i/hmcontent.html"]
    assert after["i/js/hm_webhelp.js"] == before["i/js/hm_webhelp.js"]


def test_rows_outside_i_and_jstopics_are_untouched(tmp_path: Path):
    """Only text/html rows under i/ are edited: pages elsewhere, a jstopics
    script, other scripts and a text row keep their stored bytes, whatever
    they hold. (k/page.html is one folder deep, like an i/ page, so only the
    i/ filter keeps it out; a deeper path would also meet the subfolder
    refusal.)"""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE),
                                   "k/page.html": ("text/html", PAGE),
                                   "k/html/page.html": ("text/html", PAGE),
                                   "i/jstopics/page.js": ("text/javascript", PAGE, JS_LANGUAGE, JS_TEMPLATE),
                                   "i/notes.txt": ("text/plain", PAGE), **OTHER_JS})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    after = _stored(db_path)
    for path in ("k/page.html", "k/html/page.html", "i/jstopics/page.js", "i/notes.txt", "i/js/hm_webhelp.js"):
        assert after[path] == before[path], f"{path} changed"
    assert (code, err) == (0, "")
    assert _decompress(after["i/page.html"][2]) == EXPECTED
    assert set(after) == set(before) | {tool.SCRIPT_PATH}


def test_an_identical_script_row_is_reused(tmp_path: Path):
    """Its stored bytes - compressed at quality 5, which the tool never uses -
    are kept, while the pages still change."""
    db_path = _database(tmp_path, {
        "i/page.html": ("text/html", PAGE),
        tool.SCRIPT_PATH: ("text/javascript", tool.script_for(SPRITE), JS_LANGUAGE, JS_TEMPLATE), **OTHER_JS})
    before = _stored(db_path)
    code, out, err = _run(db_path)
    assert (code, err) == (0, "")
    after = _stored(db_path)
    assert after[tool.SCRIPT_PATH] == before[tool.SCRIPT_PATH]
    assert _decompress(after["i/page.html"][2]) == EXPECTED
    assert "reuses the existing i/js/hm_sprite.js" in out


def test_a_conflicting_script_row_is_refused(tmp_path: Path):
    """Other content, or the right content with another templateId (which
    would run it through a Pebble template): nothing at all is written."""
    cases = {
        "content": ("text/javascript", tool.script_for(VARIANT), JS_LANGUAGE, JS_TEMPLATE),
        "templateId": ("text/javascript", tool.script_for(SPRITE), JS_LANGUAGE, 9),
        "type": ("text/plain", tool.script_for(SPRITE), JS_LANGUAGE, JS_TEMPLATE),
    }
    for name, row in cases.items():
        directory = tmp_path / name
        directory.mkdir()
        db_path = _database(directory, {"i/page.html": ("text/html", PAGE), tool.SCRIPT_PATH: row, **OTHER_JS})
        before = db_path.read_bytes()
        code, _out, err = _run(db_path)
        assert code == 1, name
        assert db_path.read_bytes() == before, name
        assert not list(directory.glob("*.backup-*")), name
        assert "i/js/hm_sprite.js already exists" in err and "Nothing was changed" in err, name


def test_script_row_settings_that_cannot_be_copied_refuse(tmp_path: Path):
    """i/js/ rows that disagree on languageID or templateId, none at all, or no
    text/javascript type: nothing is written."""
    cases = {
        "disagree": ({**OTHER_JS, "i/js/other.js": ("text/javascript", b"1;", 1, 0)}, True, "do not share one"),
        "none": ({}, True, "found none"),
        "no type": (OTHER_JS, False, "ContentTypes has no text/javascript row"),
    }
    for name, (js_rows, with_js_type, message) in cases.items():
        directory = tmp_path / name.replace(" ", "_")
        directory.mkdir()
        db_path = _database(directory, {"i/page.html": ("text/html", PAGE), **js_rows}, with_js_type=with_js_type)
        before = db_path.read_bytes()
        code, _out, err = _run(db_path)
        assert code == 1, name
        assert db_path.read_bytes() == before, name
        assert message in err, name


def test_a_second_run_changes_nothing(tmp_path: Path):
    """Exit 0 and not a byte written - no backup, no VACUUM - the second time."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE), **OTHER_JS})
    assert _run(db_path)[0] == 0
    first = db_path.read_bytes()
    backups = sorted(tmp_path.glob("*.backup-*"))
    assert len(backups) == 1
    code, out, err = _run(db_path)
    assert (code, err) == (0, "")
    assert "No page carries a sprite block" in out
    assert db_path.read_bytes() == first
    assert sorted(tmp_path.glob("*.backup-*")) == backups


def test_dry_run_writes_nothing(tmp_path: Path):
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE), **OTHER_JS})
    before = db_path.read_bytes()
    code, out, _err = _run(db_path, dry_run=True)
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))
    assert code == 0
    assert "1 page(s) carry the sprite block" in out and "Dry run: nothing written." in out


def test_an_error_while_writing_rolls_everything_back(tmp_path: Path):
    """One transaction: a write that fails on the second page leaves the first
    page and the script row unwritten too."""
    db_path = _database(tmp_path, {"i/a.html": ("text/html", PAGE), "i/b.html": ("text/html", PAGE), **OTHER_JS})
    before = _stored(db_path)
    real_replace_row, calls = tool.replace_row, []

    def failing_replace_row(conn, path, *args):
        calls.append(path)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        real_replace_row(conn, path, *args)

    tool.replace_row = failing_replace_row
    try:
        _run(db_path)
    except RuntimeError as exc:
        assert str(exc) == "disk full"
    else:
        raise AssertionError("the failed write did not stop the run")
    finally:
        tool.replace_row = real_replace_row
    assert calls == ["i/a.html", "i/b.html"]
    assert _stored(db_path) == before


def test_a_dictionary_database(tmp_path: Path):
    """Pages are decoded and re-encoded against the shared dictionary, the
    script row is encoded against it too, and a page without the sprite keeps
    its stored bytes."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE),
                                   "i/other.html": ("text/html", HEAD + TAIL), **OTHER_JS}, dictionary=DICTIONARY)
    before = _stored(db_path)
    # The fixture means something only if its rows really depend on the dictionary.
    try:
        plain = brotli.decompress(before["i/page.html"][2])
    except brotli.error:
        plain = None
    assert plain != PAGE
    assert _run(db_path)[0] == 0
    after = _stored(db_path)
    assert _decompress(after["i/page.html"][2], DICTIONARY) == EXPECTED
    assert _decompress(after[tool.SCRIPT_PATH][2], DICTIONARY) == tool.script_for(SPRITE)
    assert after["i/other.html"] == before["i/other.html"]


def test_a_chunked_page_is_read_and_written_whole(tmp_path: Path):
    """A page over CHUNK_SIZE is read across its "-1" row. One that fits in a
    row once the sprite is out loses the continuation it no longer needs; one
    still over CHUNK_SIZE is written back in two. Stored uncompressed
    (html_compression="none") so sizes are exact."""
    shrinks = HEAD + SPRITE + b"x" * (CHUNK_SIZE + 100 - len(PAGE)) + TAIL
    stays = HEAD + SPRITE + b"y" * (CHUNK_SIZE + 5000 - len(PAGE)) + TAIL
    assert len(SPRITE) - len(tool.SCRIPT_TAG) > 100
    db_path = _database(tmp_path, {"i/shrinks.html": ("text/html", shrinks[:CHUNK_SIZE], 1, 0, True),
                                   "i/shrinks.html-1": ("text/html", shrinks[CHUNK_SIZE:], 1, 0, True),
                                   "i/stays.html": ("text/html", stays[:CHUNK_SIZE], 1, 0, True),
                                   "i/stays.html-1": ("text/html", stays[CHUNK_SIZE:], 1, 0, True), **OTHER_JS},
                        html_compression="none")
    code, out, _err = _run(db_path)
    assert code == 0
    assert "1 chunk-continuation row(s)" not in out and "2 chunk-continuation row(s)" in out
    after = _stored(db_path)
    assert "i/shrinks.html-1" not in after
    assert after["i/shrinks.html"][2] == shrinks.replace(SPRITE, tool.SCRIPT_TAG)
    new_stays = stays.replace(SPRITE, tool.SCRIPT_TAG)
    assert len(new_stays) > CHUNK_SIZE
    assert after["i/stays.html"][2] + after["i/stays.html-1"][2] == new_stays
    assert len(after["i/stays.html"][2]) == CHUNK_SIZE


def test_an_unreadable_row_is_reported_and_the_rest_still_change(tmp_path: Path):
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE),
                                   "i/broken.html": ("text/html", b"not brotli at all", 1, 0, True), **OTHER_JS})
    code, _out, err = _run(db_path)
    assert code == 1
    assert "i/broken.html" in err and "could not be read" in err
    assert _decompress(_stored(db_path)["i/page.html"][2]) == EXPECTED


def test_a_page_in_a_subfolder_is_refused(tmp_path: Path):
    """./js/hm_sprite.js from i/sub/ would mean i/sub/js/: the page is named and
    keeps its block; a subfolder page without the sprite is no problem."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE),
                                   "i/sub/page.html": ("text/html", PAGE),
                                   "i/sub/plain.html": ("text/html", HEAD + TAIL), **OTHER_JS})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    after = _stored(db_path)
    assert after["i/sub/page.html"] == before["i/sub/page.html"], "i/sub/page.html changed"
    assert code == 1
    assert after["i/sub/plain.html"] == before["i/sub/plain.html"]
    assert "skipped i/sub/page.html" in err and "subfolder" in err
    assert "i/sub/plain.html" not in err
    assert _decompress(after["i/page.html"][2]) == EXPECTED


def test_a_block_not_directly_inside_body_is_refused(tmp_path: Path):
    """In <head>, inside another element, or inside a comment, the script would
    not put the block back where it was."""
    in_head = HEAD.replace(b"</head>", SPRITE + b"</head>") + TAIL
    cases = {
        "i/head.html": in_head,
        "i/div.html": HEAD + b"<div>" + SPRITE + b"</div>" + TAIL,
        "i/comment.html": HEAD + b"<!-- " + SPRITE + b" -->" + TAIL,
        "i/nobody.html": SPRITE + TAIL,
    }
    db_path = _database(tmp_path, {**{path: ("text/html", page) for path, page in cases.items()},
                                   "i/page.html": ("text/html", PAGE), **OTHER_JS})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    after = _stored(db_path)
    for path in cases:
        assert after[path] == before[path], f"{path} changed"
        assert f"skipped {path}: has its sprite block somewhere other than directly inside <body>" in err, (
            f"{path} not reported as outside <body>")
    assert _decompress(after["i/page.html"][2]) == EXPECTED
    assert code == 1


def test_odd_sprites_are_refused(tmp_path: Path):
    """Every shape this step does not know is named and left alone, while a
    normal page beside it still changes. Each shape gets a database of its
    own, on two pages to the normal page's one, so that only its own guard
    can refuse it: without that guard a differing block would be the most
    common one, not refused for differing."""
    cases = {
        "twice": (HEAD + SPRITE + SPRITE + TAIL, "carries the sprite block 2 times"),
        "unclosed": (HEAD + SPRITE[:-len(b"</svg>")], "never closed"),
        "nested": (HEAD + SPRITE.replace(b"<defs>", b"<defs><svg>", 1) + TAIL, "nested <svg>"),
        "outside": (HEAD + SPRITE + b'<g class="svgsprite" id="x"></g>' + TAIL, "outside its sprite block"),
        "unknown": (HEAD + SPRITE.replace(b'version="1.1" ', b"") + TAIL, "no sprite block this step knows"),
        "both": (HEAD + SPRITE + tool.SCRIPT_TAG + TAIL, "already loads hm_sprite.js"),
        "utf8": (HEAD + SPRITE.replace(b"Hide", "Hide \u2014".encode()) + TAIL, "non-ASCII"),
    }
    for name, (page, why) in cases.items():
        directory = tmp_path / name
        directory.mkdir()
        odd = [f"i/{name}-1.html", f"i/{name}-2.html"]
        db_path = _database(directory, {**{path: ("text/html", page) for path in odd},
                                        "i/page.html": ("text/html", PAGE), **OTHER_JS})
        before = _stored(db_path)
        code, _out, err = _run(db_path)
        after = _stored(db_path)
        for path in odd:
            assert after[path] == before[path], f"{path} changed"
            assert f"skipped {path}: " in err and why in err, f"{path} not reported as: {why}"
        assert _decompress(after["i/page.html"][2]) == EXPECTED, name
        assert code == 1, name


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_share_skin_sprite.py`)."""
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
