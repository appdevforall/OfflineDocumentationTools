"""Tests for verify_all_docs_db.py, the final gate of build-all-docs(.yaml / -local.yaml).

Each case is a way the combined rebuild could hand the app a database that looks fine and is
not: a row the app's decoder cannot read, a row pointing at a template that is not there, a
documentation set that silently stayed HTML, a row lost outside the rebuilt paths, or a tooltip
button left pointing at nothing.
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import brotli
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_all_docs_db as verify  # noqa: E402
from populate_db import DictionaryCompressor  # noqa: E402  (on sys.path via the module above)

needs_brotli_cli = pytest.mark.skipif(shutil.which("brotli") is None, reason="brotli CLI not installed")

SCHEMA = """
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE,
                           compression TEXT NOT NULL);
CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
    languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL,
    templateId INTEGER NOT NULL DEFAULT 0, UNIQUE('path'));
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    content BLOB NOT NULL, UNIQUE('name'));
CREATE TABLE Tooltips (id INTEGER PRIMARY KEY AUTOINCREMENT, categoryId INTEGER NOT NULL,
    tag TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE TooltipButtons (tooltipId INTEGER, buttonNumberId INTEGER, description TEXT, uri TEXT);
CREATE TABLE DocumentationDatabaseVersion (major INTEGER, minor INTEGER, patch INTEGER,
    changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP, who TEXT, comment TEXT);
CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY, data BLOB NOT NULL);
INSERT INTO ContentTypes (id, value, compression) VALUES (3, 'image/png', 'none');
INSERT INTO ContentTypes (id, value, compression) VALUES (12, 'text/html', 'brotli');
INSERT INTO Templates (id, name, content) VALUES (1, 'layout.pebble', CAST('{{ name }}' AS BLOB));
INSERT INTO Templates (id, name, content) VALUES (2, 'page.peb', CAST('{{ title }}' AS BLOB));
INSERT INTO Templates (id, name, content) VALUES (5, 'javadoc.peb', CAST('{{ page }}' AS BLOB));
INSERT INTO Templates (id, name, content) VALUES (7, 'android-class.peb', CAST('{{ page }}' AS BLOB));
"""

DICTIONARY = b'{"kind":"class","name":"","page":"class","title":"<p>documentation</p>"}' * 64

# path -> (templateId, document or raw bytes)
PAGES = {
    "k/html/home.html": (2, {"title": "Home"}),
    "k/html/images/logo.png": (0, b"\x89PNG"),
    "k/kotlin-stdlib/index.html": (1, {"kind": "module", "name": "kotlin-stdlib"}),
    "k/kotlin-stdlib/package-list": (1, b"$dokka.format:json-v1$\nkotlin\n"),
    "j/html/api/java.base/java/util/List.html": (5, {"page": "class", "name": "List"}),
    "a/android/app/Activity.html": (7, {"page": "android-class", "name": "Activity"}),
    "assets/docs.css": (0, b"body{}"),
    "bookshelf/b.html": (0, b"<html>untouched</html>"),
}


def make_db(path: Path, version=(2, 1, 0), dictionary=DICTIONARY, pages=PAGES) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    if version:
        conn.execute("INSERT INTO DocumentationDatabaseVersion (major, minor, patch) VALUES (?, ?, ?)",
                     version)
    if dictionary:
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (dictionary,))
    use_dictionary = bool(version and version[0] >= 2)
    codec = DictionaryCompressor(dictionary) if use_dictionary else None
    for p, (template_id, body) in pages.items():
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        type_id = 3 if p.endswith(".png") else 12
        stored = raw if type_id == 3 else (codec.compress(raw) if codec else brotli.compress(raw))
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, ?)", (p, stored, type_id, template_id))
    tid = conn.execute("INSERT INTO Tooltips (categoryId, tag, summary, detail) "
                       "VALUES (1, 'List', 's', 'd')").lastrowid
    conn.execute("INSERT INTO TooltipButtons VALUES (?, 1, 'more', ?)",
                 (tid, "j/html/api/java.base/java/util/List.html#add(E)"))
    conn.commit()
    conn.close()
    return path


def run(db, original=None, sets=("kotlin-website", "kotlin-stdlib", "java", "android")):
    return verify.verify(db, original, list(sets), sample=0, seed=1, workers=2, render=False)


def execute(db, sql, *params):
    conn = sqlite3.connect(db)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


@needs_brotli_cli
class TestDictionaryDatabase:
    def test_a_good_database_passes(self, tmp_path):
        db = make_db(tmp_path / "new.db")
        original = tmp_path / "orig.db"
        shutil.copy(db, original)
        assert run(db, original).failures == []

    def test_a_plain_row_in_a_2x_database_fails_like_it_would_in_the_app(self, tmp_path):
        # Prose, so the plain stream leans on Brotli's built-in word list - which is exactly what
        # an attached dictionary reinterprets. (A tiny payload that never references it decodes
        # identically either way, in the app too, and is rightly not flagged.)
        db = make_db(tmp_path / "new.db")
        prose = json.dumps({"title": "Home", "text": (
            "The quick brown fox jumps over the lazy dog. Returns the number of elements in this "
            "collection, which should be considered when designing interfaces for international "
            "applications and government information.")}).encode()
        execute(db, "UPDATE Content SET content = ? WHERE path = 'k/html/home.html'",
                brotli.compress(prose))
        assert any("decode" in f for f in run(db).failures)

    def test_a_2x_database_without_its_dictionary_is_refused(self, tmp_path):
        db = make_db(tmp_path / "new.db")
        execute(db, "DELETE FROM CompressionDictionary")
        with pytest.raises(SystemExit, match="CompressionDictionary"):
            run(db)

    def test_a_missing_template_fails(self, tmp_path):
        db = make_db(tmp_path / "new.db")
        execute(db, "DELETE FROM Templates WHERE name = 'layout.pebble'")
        assert any("Templates row" in f for f in run(db).failures)

    def test_a_page_that_is_not_json_fails_but_package_list_is_only_noted(self, tmp_path, capsys):
        db = make_db(tmp_path / "new.db")
        assert run(db).failures == []
        assert "k/kotlin-stdlib/package-list" in capsys.readouterr().out
        with DictionaryCompressor(DICTIONARY) as codec:
            execute(db, "UPDATE Content SET content = ? WHERE path = 'a/android/app/Activity.html'",
                    codec.compress(b"<html>still scraped</html>"))
        assert any("parse as JSON" in f for f in run(db).failures)

    def test_a_set_left_untemplated_fails(self, tmp_path):
        db = make_db(tmp_path / "new.db")
        execute(db, "UPDATE Content SET templateId = 0 WHERE path LIKE 'j/html/api/%'")
        failures = run(db).failures
        assert any(f.startswith("java:") for f in failures)
        assert run(db, sets=("android",)).failures == []

    def test_rows_lost_outside_the_rebuilt_paths_fail_but_inside_them_do_not(self, tmp_path):
        db = make_db(tmp_path / "new.db")
        original = tmp_path / "orig.db"
        shutil.copy(db, original)
        execute(db, "DELETE FROM Content WHERE path = 'k/html/images/logo.png'")
        assert run(db, original).failures == []
        execute(db, "DELETE FROM Content WHERE path = 'bookshelf/b.html'")
        assert any("outside" in f for f in run(db, original).failures)

    def test_a_recompressed_item_changing_its_chunk_count_is_not_a_lost_row(self, tmp_path):
        """The re-mint recompresses every row, so an item stored as a base row plus continuation
        rows can come out needing fewer of them. That is not a row disappearing."""
        from content_chunking import CHUNK_SIZE
        db = make_db(tmp_path / "new.db")
        execute(db, "INSERT INTO Content (path, languageID, content, contentTypeID) "
                    "VALUES ('p/book.pdf', 1, ?, 3)", bytes(CHUNK_SIZE))
        execute(db, "INSERT INTO Content (path, languageID, content, contentTypeID) "
                    "VALUES ('p/book.pdf-1', 1, x'00', 3)")
        original = tmp_path / "orig.db"
        shutil.copy(db, original)
        execute(db, "DELETE FROM Content WHERE path = 'p/book.pdf-1'")
        execute(db, "UPDATE Content SET content = x'0000' WHERE path = 'p/book.pdf'")
        assert run(db, original).failures == []

    def test_a_button_newly_pointing_at_nothing_fails(self, tmp_path):
        db = make_db(tmp_path / "new.db")
        original = tmp_path / "orig.db"
        shutil.copy(db, original)
        execute(db, "DELETE FROM Content WHERE path = 'j/html/api/java.base/java/util/List.html'")
        assert any("tooltip button" in f for f in run(db, original).failures)


class TestPre2Database:
    def test_rows_are_read_plainly_below_version_2(self, tmp_path):
        db = make_db(tmp_path / "new.db", version=None, dictionary=None)
        assert run(db).failures == []


def test_doc_set_of():
    assert verify.doc_set_of("a/androidx/core/Foo.html") == "android"
    assert verify.doc_set_of("a/index.html") == "android"
    assert verify.doc_set_of("a/devsite/x.html") is None
    assert verify.doc_set_of("k/kotlin-reflect/index.html") == "kotlin-stdlib"
    assert verify.doc_set_of("k/html/x.html") == "kotlin-website"
    assert verify.doc_set_of("j/html/api/index.html") == "java"
    assert verify.in_rebuildable_namespace("assets/android-reference.css")
    assert not verify.in_rebuildable_namespace("bookshelf/b.html")


def test_unknown_sets_are_rejected(tmp_path):
    db = make_db(tmp_path / "new.db", version=None, dictionary=None)
    with pytest.raises(SystemExit):
        verify.main([str(db), "--sets", "java,cobol"])
