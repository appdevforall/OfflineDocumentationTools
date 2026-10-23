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
test_remove_skin_font_comment.py

Covers remove_skin_font_comment.py: that it cuts Help+Manual's Google Fonts note
out of a page and changes no other byte, that it leaves every other row's stored
bytes exactly as they were, and that anything resembling the note without being
it is reported rather than removed or ignored.

The note used here is the real one, byte for byte (checked against the digest
the tool carries), so a fixture can never drift into testing a note that does
not exist. Stored rows are compressed at a quality the tool never uses, so a row
it re-compressed cannot pass for one it left alone.

Run it either way - as a plain script, needing nothing installed:

    uv run scripts/test_remove_skin_font_comment.py

or under pytest, which needs the same packages on the path:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_remove_skin_font_comment.py

The dictionary tests need the `brotli` CLI on PATH, as the tool itself does.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import remove_skin_font_comment as skin  # noqa: E402

CHUNK_SIZE = 1024 * 1024

# The note's content as the skin writes it: CRLF line endings, a leading space,
# and trailing spaces on four lines.
NOTE = b"\r\n".join([
    b" Insert Google web fonts references here. ",
    b"",
    b"You can use either dynamic Google Fonts hosted by Google, or download",
    b"and host the fonts yourself by adding them to the skin. ",
    b"",
    b"DYNAMIC GOOGLE FONTS ARE NOT GDPR COMPLIANT",
    b"===========================================",
    b"Note that dynamic Google Fonts hosted by Google are not compliant",
    b"with the EU's GDPR legislation because the system sends the user's",
    b"IP address to Google. If your site is in the EU or targets users",
    b"in the EU you should use locally hosted fonts. ",
    b"",
    b"Content of this file for local fonts (you must add the CSS code to",
    b"the file yourself, see the help):",
    b"",
    b'<link type="text/css" href="./css/GoogleFonts.css" rel="stylesheet" />',
    b"",
    b"Example of content of this file for dynamic Google Fonts:",
    b"",
    b'<link href="https://fonts.googleapis.com/css?family=Roboto:300,300i,400,400i,500,500i" rel="stylesheet">',
    b"",
    b"See the section on Google Web Fonts in the PP help for more details.",
    b"Delete this comment to save space after adding your references. ",
    b"",
    b"",
])
NOTE_COMMENT = b"<!--" + NOTE + b"-->"

# A Help+Manual page reduced to what matters here: the note sits between two
# CRLFs in the head, and the comments that must survive are spread around it.
HEAD = (b'<!DOCTYPE html>\r\n<html lang="en">\r\n<head>\r\n<!--ZOOMSTOP-->\r\n'
        b'<link rel="shortcut icon" href="favicon.ico" /> \r\n')
TAIL = (b'\r\n\t<link type="text/css" href="./css/hmprojectstyles.css" rel="stylesheet" />\r\n</head>\r\n'
        b'<body>\r\n<!-- TOPIC CONTENT -->\r\n<!-- HMSKIN BODY START -->\r\n<!--ZOOMRESTART-->\r\n'
        b'<p>Install an add-on.</p>\r\n<!-- topicbody-->\r\n</body>\r\n</html>\r\n')
PAGE = HEAD + NOTE_COMMENT + TAIL
EXPECTED = HEAD + TAIL

# A shared dictionary small enough to read, but holding enough of the page that
# a dictionary-compressed row really refers into it.
DICTIONARY = (HEAD + TAIL) * 4 + NOTE


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


def _database(tmp_path: Path, rows: dict, dictionary: bytes = None, html_compression: str = "brotli") -> Path:
    """A miniature documentation database: ContentTypes, Content, and - when
    `dictionary` is given - a CompressionDictionary every brotli row is
    compressed against. `rows` maps path -> (content type, decoded bytes);
    a value of (content type, bytes, True) stores the bytes as they are."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, "
        "compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, "
        "templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
    )
    types = {"text/html": (12, html_compression), "text/javascript": (13, "brotli"), "text/plain": (5, "none")}
    conn.executemany("INSERT INTO ContentTypes (id, value, compression) VALUES (?, ?, ?)",
                     [(type_id, value, compression) for value, (type_id, compression) in types.items()])
    if dictionary is not None:
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary,))
    for path, (content_type, raw, *as_is) in rows.items():
        type_id, compression = types[content_type]
        stored = raw if as_is or compression == "none" else _compress(raw, dictionary)
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, 3)", (path, stored, type_id))
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
        code = skin.run(cfg)
    return code, out.getvalue(), err.getvalue()


def test_the_fixture_is_the_real_note():
    """Everything below depends on NOTE being the note the tool recognises."""
    assert len(NOTE) == 1005
    assert hashlib.sha256(NOTE).hexdigest() == skin.KNOWN_NOTE_SHA256


def test_find_notes_spans_exactly_the_comment():
    spans, odd = skin.find_notes(PAGE)
    assert odd == []
    assert spans == [(len(HEAD), len(HEAD) + len(NOTE_COMMENT))]
    assert skin.remove_spans(PAGE, spans) == EXPECTED


def test_the_note_is_removed_and_every_other_byte_kept(tmp_path: Path):
    """The page loses the comment and nothing else: the CRLFs either side of it,
    ZOOMSTOP/ZOOMRESTART and the HMSKIN markers all survive. The row keeps its
    id, language, content type and templateId."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE)})
    before = _stored(db_path)
    code, out, _err = _run(db_path)
    assert code == 0
    after = _stored(db_path)
    assert _decompress(after["i/page.html"][2]) == EXPECTED
    for comment in (b"<!--ZOOMSTOP-->", b"<!--ZOOMRESTART-->", b"<!-- HMSKIN BODY START -->",
                    b"<!-- TOPIC CONTENT -->", b"<!-- topicbody-->"):
        assert comment in _decompress(after["i/page.html"][2])
    row_id, language_id, _content, content_type_id, template_id = after["i/page.html"]
    assert (row_id, language_id, content_type_id, template_id) == (
        before["i/page.html"][:2] + before["i/page.html"][3:])
    assert "1 row(s) carry the note" in out
    assert f"{len(NOTE_COMMENT):,} decoded byte(s)" in out


def test_a_page_without_the_note_keeps_its_stored_bytes(tmp_path: Path):
    """Not re-compressed: its stored blob is the one it had, byte for byte."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE),
                                   "i/other.html": ("text/html", EXPECTED),
                                   "k/html/a.html": ("text/html", b"<p>kotlin</p>")})
    before = _stored(db_path)
    assert _run(db_path)[0] == 0
    after = _stored(db_path)
    assert after["i/other.html"] == before["i/other.html"]
    assert after["k/html/a.html"] == before["k/html/a.html"]
    assert after["i/page.html"] != before["i/page.html"]


def test_a_changed_note_is_left_in_place_and_reported(tmp_path: Path):
    """A comment opening with the same sentence but saying something else is not
    removed, is named, and fails the run - while the known note elsewhere, even
    on the same page, is still removed."""
    variant = b"<!--" + NOTE.replace(b"Roboto", b"Lato") + b"-->"
    db_path = _database(tmp_path, {"i/known.html": ("text/html", PAGE),
                                   "i/variant.html": ("text/html", HEAD + variant + TAIL),
                                   "i/both.html": ("text/html", HEAD + NOTE_COMMENT + variant + TAIL)})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    after = _stored(db_path)
    assert after["i/variant.html"] == before["i/variant.html"]
    assert _decompress(after["i/known.html"][2]) == EXPECTED
    assert _decompress(after["i/both.html"][2]) == HEAD + variant + TAIL
    assert "i/variant.html" in err and "differs from the known note" in err
    assert "i/both.html" in err
    assert "i/known.html" not in err


def test_other_sightings_of_the_sentence_are_reported(tmp_path: Path):
    """The sentence not at the start of a comment, or a note that is never
    closed: neither is the known note, so each is left alone and fails the run."""
    cases = {
        "i/prose.html": HEAD + b"<p>Insert Google web fonts references here.</p>" + TAIL,
        "i/inside.html": HEAD + b"<!-- See: Insert Google web fonts references here. -->" + TAIL,
        "i/unclosed.html": HEAD + b"<!--" + NOTE,
    }
    db_path = _database(tmp_path, {path: ("text/html", page) for path, page in cases.items()})
    before = _stored(db_path)
    code, _out, err = _run(db_path)
    assert code == 1
    assert _stored(db_path) == before
    assert err.count("not at the start of a comment") == 2
    assert "never closed" in err
    for path in cases:
        assert path in err


def test_a_second_run_changes_nothing(tmp_path: Path):
    """Exit 0 and not a byte written - no backup, no VACUUM - the second time."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE)})
    assert _run(db_path)[0] == 0
    first = db_path.read_bytes()
    backups = sorted(tmp_path.glob("*.backup-*"))
    assert len(backups) == 1
    code, out, err = _run(db_path)
    assert (code, err) == (0, "")
    assert "No row carries the note" in out
    assert db_path.read_bytes() == first
    assert sorted(tmp_path.glob("*.backup-*")) == backups


def test_dry_run_writes_nothing(tmp_path: Path):
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE)})
    before = db_path.read_bytes()
    code, out, _err = _run(db_path, dry_run=True)
    assert code == 0
    assert "1 row(s) carry the note" in out and "Dry run: nothing written." in out
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))


def test_a_non_html_row_is_untouched(tmp_path: Path):
    """Only text/html pages are edited, whatever another row happens to hold."""
    db_path = _database(tmp_path, {"i/js/notes.js": ("text/javascript", PAGE),
                                   "i/notes.txt": ("text/plain", PAGE),
                                   "i/page.html": ("text/html", PAGE)})
    before = _stored(db_path)
    assert _run(db_path)[0] == 0
    after = _stored(db_path)
    assert after["i/js/notes.js"] == before["i/js/notes.js"]
    assert after["i/notes.txt"] == before["i/notes.txt"]
    assert _decompress(after["i/page.html"][2]) == EXPECTED


def test_a_dictionary_database(tmp_path: Path):
    """Rows are decoded and re-encoded against the shared dictionary, and a
    page without the note keeps its stored bytes."""
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE),
                                   "i/other.html": ("text/html", EXPECTED)}, dictionary=DICTIONARY)
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
    assert after["i/other.html"] == before["i/other.html"]


def test_a_chunked_page_is_read_and_written_whole(tmp_path: Path):
    """A page over CHUNK_SIZE is read across its "-1" row; once the note is out
    it fits in one row, and the continuation it no longer needs is gone.
    Stored uncompressed (html_compression="none") so its size is exact."""
    filler = b"x" * (CHUNK_SIZE + 500 - len(PAGE))
    page = HEAD + NOTE_COMMENT + filler + TAIL
    db_path = _database(tmp_path, {"i/big.html": ("text/html", page[:CHUNK_SIZE], True),
                                   "i/big.html-1": ("text/html", page[CHUNK_SIZE:], True)},
                        html_compression="none")
    assert _run(db_path)[0] == 0
    after = _stored(db_path)
    assert "i/big.html-1" not in after
    assert after["i/big.html"][2] == HEAD + filler + TAIL


def test_an_unreadable_row_is_reported_and_the_rest_still_change(tmp_path: Path):
    db_path = _database(tmp_path, {"i/page.html": ("text/html", PAGE),
                                   "i/broken.html": ("text/html", b"not brotli at all", True)})
    code, _out, err = _run(db_path)
    assert code == 1
    assert "i/broken.html" in err and "could not be read" in err
    assert _decompress(_stored(db_path)["i/page.html"][2]) == EXPECTED


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_remove_skin_font_comment.py`)."""
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
