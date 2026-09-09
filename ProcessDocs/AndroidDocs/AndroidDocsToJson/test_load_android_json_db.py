"""Tests for the documentation-database loader and the row-writing helpers.

The loader's output is a 250 MB artifact that is only exercised for real by an Android app, so
what is worth testing here is everything that decides *where* a byte goes and *what shape* it is
in: the path a page lands at, the links it carries, and the row layout the server reassembles.
"""

import json
import sqlite3

import pytest

import dbwrite
import load_android_json_db as loader


# --------------------------------------------------------------------------------------------
# Paths and links
# --------------------------------------------------------------------------------------------

# What the database calls the pages of `android.os.strictmode`. The scrape has them under
# `StrictMode/`, because the filesystem it is read from cannot give a package and a class whose
# names differ only in case one directory each.
KNOWN = {
    "a/android/os/strictmode/violation.html".casefold(): "a/android/os/strictmode/Violation.html",
    "a/android/os/strictmode/networkviolation.html".casefold():
        "a/android/os/strictmode/NetworkViolation.html",
    "a/android/app/activity.html".casefold(): "a/android/app/Activity.html",
}


class TestPaths:
    def test_a_json_path_becomes_a_served_path(self):
        assert loader.db_path_for("android/app/Activity.json") == "a/android/app/Activity.html"
        assert loader.db_path_for("index.json") == "a/index.html"

    def test_a_page_takes_the_case_the_database_already_uses(self):
        # Writing it under the scrape's spelling would leave the row it is meant to replace in
        # place and put the replacement beside it.
        assert loader.canonical("a/android/os/StrictMode/Violation.html", KNOWN) == \
            "a/android/os/strictmode/Violation.html"

    def test_a_page_the_database_does_not_have_keeps_its_own_path(self):
        assert loader.canonical("a/android/demo/Widget.html", KNOWN) == \
            "a/android/demo/Widget.html"


class TestLinks:
    @pytest.mark.parametrize("url,expected", [
        ("Base.json", "Base.html"),
        ("../view/View.json#foo(int)", "../view/View.html#foo(int)"),
        # Left alone: it does not point into this database.
        ("https://developer.android.com/reference/java/lang/Object",
         "https://developer.android.com/reference/java/lang/Object"),
        ("#summary", "#summary"),
        ("ftp:/dkuug.dk/x.txt", "ftp:/dkuug.dk/x.txt"),
    ])
    def test_extension_and_nothing_else(self, url, expected):
        assert loader.rewrite_link(url, "a/android/app", KNOWN) == expected

    def test_a_link_is_recased_to_match_the_row_it_names(self):
        # From android/os, the scrape spells the package directory `StrictMode`; the row is
        # `strictmode`. A link that keeps the scrape's spelling names no row at all.
        assert loader.rewrite_link("StrictMode/Violation.json", "a/android/os", KNOWN) == \
            "strictmode/Violation.html"

    def test_links_are_rewritten_wherever_they_appear(self):
        document = {
            "page": "android-class",
            "inheritance": [{"label": "Activity", "url": "../app/Activity.json"}],
            "description": '<p>See <a href="StrictMode/Violation.json#x">Violation</a>.</p>',
            "name": "Example",
        }
        out = loader.rewrite_document(document, "a/android/os", KNOWN)
        assert out["inheritance"][0]["url"] == "../app/Activity.html"
        assert out["description"] == \
            '<p>See <a href="strictmode/Violation.html#x">Violation</a>.</p>'
        assert out["name"] == "Example"

    def test_the_three_served_links_are_absolute(self):
        # The server resolves a leading slash against Content.path, and these three depend on
        # where pages are served from rather than on the documentation.
        links = loader.served_links({"page": "android-class", "packageName": "android.app"},
                                    "a/android/app/Activity.html")
        assert links["stylesheetUrl"] == "/assets/android-reference.css"
        assert links["indexUrl"] == "/a/index.html"
        assert links["packageUrl"] == "/a/android/app/package-summary.html"

    def test_a_package_page_gets_no_link_to_itself(self):
        links = loader.served_links({"page": "android-package", "packageName": "android.app"},
                                    "a/android/app/package-summary.html")
        assert "packageUrl" not in links


class TestTemplates:
    @pytest.mark.parametrize("name", ["class", "package", "index"])
    def test_each_template_is_assembled_self_contained(self, name):
        source = loader.assemble_template(name)
        # A Templates row is one template: the server has nothing to resolve an extends or an
        # import against, and macros are only visible inside their own file.
        assert "{% extends" not in source
        assert "{% import" not in source
        assert "{% macro summaryRows" in source
        # `raw` is the only filter the database's own templates are known to use.
        for absent in ("| doc", "| href", "| anchor"):
            assert absent not in source


# --------------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """A database with just enough of the real schema to write content into."""
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.executescript("""
        CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
        CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL
            UNIQUE, compression TEXT NOT NULL);
        CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            content BLOB NOT NULL, UNIQUE(name));
        CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1),
            data BLOB NOT NULL);
        CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
            languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL,
            templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));
        INSERT INTO Languages (value) VALUES ('en-US');
        INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli'),
            ('image/png', 'none');
        INSERT INTO CompressionDictionary (id, data) VALUES (1, X'0102030405');
    """)
    conn.commit()
    return conn


class TestLookups:
    def test_ids_come_from_the_database(self, db):
        assert dbwrite.get_id(db, "Languages", "en-US") == 1
        assert dbwrite.get_content_type(db, "text/html") == (1, True)
        assert dbwrite.get_content_type(db, "image/png") == (2, False)

    def test_a_missing_row_is_an_error_not_a_guess(self, db):
        # Inventing a Languages or ContentTypes row would put content in the database that the
        # server has no way to interpret.
        with pytest.raises(RuntimeError, match="no row for"):
            dbwrite.get_content_type(db, "text/nonsense")

    def test_the_dictionary_is_read_never_made(self, db):
        assert dbwrite.load_dictionary(db) == b"\x01\x02\x03\x04\x05"
        db.execute("DELETE FROM CompressionDictionary")
        with pytest.raises(RuntimeError, match="missing or empty"):
            dbwrite.load_dictionary(db)

    def test_a_template_is_upserted_by_name(self, db):
        first = dbwrite.upsert_template(db, "android-class.peb", "one")
        again = dbwrite.upsert_template(db, "android-class.peb", "two")
        assert first == again, "the same name keeps the same id, so Content.templateId stays valid"
        assert db.execute("SELECT content FROM Templates WHERE id = ?",
                          (first,)).fetchone()[0] == b"two"


class TestWriteContent:
    def test_a_new_path_is_inserted(self, db):
        assert dbwrite.write_content(db, "a/x.html", 1, 1, 5, b"hello") == 1
        row = db.execute("SELECT content, templateId FROM Content WHERE path = 'a/x.html'").fetchone()
        assert row == (b"hello", 5)

    def test_an_existing_path_is_updated_in_place(self, db):
        db.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                   "VALUES ('a/x.html', 1, X'00', 1, 0)")
        original = db.execute("SELECT id FROM Content WHERE path='a/x.html'").fetchone()[0]
        dbwrite.write_content(db, "a/x.html", 1, 1, 5, b"replaced")
        row = db.execute("SELECT id, content, templateId FROM Content "
                         "WHERE path='a/x.html'").fetchone()
        # The same row, not a delete and a re-insert: Content's AddBook trigger fires on an
        # insert, and a new id would break anything referencing the old one.
        assert row == (original, b"replaced", 5)

    def test_content_over_the_chunk_size_is_split_the_way_the_server_reassembles_it(self, db):
        data = bytes(dbwrite.CHUNK_SIZE + 100)
        assert dbwrite.write_content(db, "a/big.html", 1, 1, 5, data) == 2
        base = db.execute("SELECT content FROM Content WHERE path='a/big.html'").fetchone()[0]
        tail = db.execute("SELECT content FROM Content WHERE path='a/big.html-1'").fetchone()[0]
        # The server decides a row is fragmented by its first chunk being exactly CHUNK_SIZE.
        assert len(base) == dbwrite.CHUNK_SIZE
        assert len(tail) == 100
        assert dbwrite.read_content(db, "a/big.html") == data

    def test_a_shorter_payload_leaves_no_stale_tail(self, db):
        dbwrite.write_content(db, "a/x.html", 1, 1, 5, bytes(dbwrite.CHUNK_SIZE * 2 + 5))
        assert len(dbwrite.fragment_chain(db, "a/x.html")) == 2
        dbwrite.write_content(db, "a/x.html", 1, 1, 5, b"small")
        assert dbwrite.fragment_chain(db, "a/x.html") == [], "surplus fragments must be deleted"
        assert dbwrite.read_content(db, "a/x.html") == b"small"

    def test_a_fragment_chain_is_matched_exactly_not_by_pattern(self, db):
        # The LIKE that finds a chain over-matches: `_` and `%` are wildcards and the suffix is
        # not constrained to digits. Only the regex re-check makes the answer exact.
        for path in ("a/x.html", "a/x.html-1", "a/xyhtml-1", "a/x.html-notanumber"):
            db.execute("INSERT INTO Content (path, languageID, content, contentTypeID) "
                       "VALUES (?, 1, X'00', 1)", (path,))
        assert dbwrite.fragment_chain(db, "a/x.html") == [(1, "a/x.html-1")]

    def test_a_round_trip_through_the_real_compressor(self, db):
        # Wrong-dictionary decoding is the silent failure mode, so the bytes that go in have to
        # come back out through a compressor built from the same dictionary.
        compressor = dbwrite.DictionaryCompressor(dbwrite.load_dictionary(db))
        payload = json.dumps({"page": "android-class", "name": "Widget" * 500}).encode()
        dbwrite.write_content(db, "a/x.html", 1, 1, 5, compressor.compress(payload))
        assert compressor.decompress(dbwrite.read_content(db, "a/x.html")) == payload
