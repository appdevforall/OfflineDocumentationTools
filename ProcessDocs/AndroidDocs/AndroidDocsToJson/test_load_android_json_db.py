"""Tests for the documentation-database loader and the row-writing helpers.

The loader's output is a 250 MB artifact that is only exercised for real by an Android app, so
what is worth testing here is everything that decides *where* a byte goes and *what shape* it is
in: the path a page lands at, the links it carries, and the row layout the server reassembles.
"""

import json
import sqlite3
from pathlib import Path

import pytest

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


class TestStorePage:
    def test_a_page_that_is_not_there_is_inserted(self, db):
        assert loader.store_page(db, "a/x.html", 1, 1, 5, b"hello", []) is False
        assert db.execute("SELECT content, templateId FROM Content "
                          "WHERE path='a/x.html'").fetchone() == (b"hello", 5)

    def test_a_page_that_is_there_is_replaced_in_place(self, db):
        db.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                   "VALUES ('a/x.html', 1, X'00', 1, 0)")
        original = db.execute("SELECT id FROM Content WHERE path='a/x.html'").fetchone()[0]
        assert loader.store_page(db, "a/x.html", 1, 1, 5, b"replaced", []) is True
        # The same row, not a delete and a re-insert: Content's AddBook trigger fires on insert,
        # and a new id would break anything holding the old one.
        assert db.execute("SELECT id, content, templateId FROM Content "
                          "WHERE path='a/x.html'").fetchone() == (original, b"replaced", 5)

    def test_an_absent_page_is_never_left_to_the_updater(self, db):
        # write_item's UPDATE on a path that is not there succeeds while writing nothing, so
        # picking the wrong half of the pair loses the page silently rather than loudly.
        loader.store_page(db, "a/new.html", 1, 1, 5, b"content", [])
        assert db.execute("SELECT COUNT(*) FROM Content WHERE path='a/new.html'").fetchone()[0] == 1

    def test_a_page_over_the_chunk_size_is_split_and_comes_back_whole(self, db):
        from migrate_content_to_dictionary_brotli import read_item
        from populate_db import CHUNK_SIZE
        data = bytes(CHUNK_SIZE + 100)
        chunked: list = []
        loader.store_page(db, "a/big.html", 1, 1, 5, data, chunked)
        # The server decides a row is fragmented by its first chunk being exactly CHUNK_SIZE.
        assert len(db.execute("SELECT content FROM Content "
                              "WHERE path='a/big.html'").fetchone()[0]) == CHUNK_SIZE
        assert read_item(db, "a/big.html") == data
        assert chunked and chunked[0][0] == "a/big.html"

    def test_a_page_survives_the_round_trip_through_the_real_compressor(self, db):
        # Wrong-dictionary decoding is the silent failure mode, so the bytes that go in have to
        # come back out through a compressor built from the same dictionary.
        from migrate_content_to_dictionary_brotli import read_item
        from populate_db import DictionaryCompressor, load_dictionary
        compressor = DictionaryCompressor(load_dictionary(db))
        payload = json.dumps({"page": "android-class", "name": "Widget" * 500}).encode()
        loader.store_page(db, "a/x.html", 1, 1, 5, compressor.compress(payload), [])
        assert compressor.decompress(read_item(db, "a/x.html")) == payload

    def test_replacing_also_sets_the_columns_that_say_how_to_serve_it(self, db):
        # The whole point of the load: a scraped page is templateId 0 and has to come out
        # templated. write_item rewrites only the bytes, so a row replaced without this is served
        # as raw JSON instead of being rendered -- and nothing downstream would say so.
        db.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                   "VALUES ('a/old.html', 1, X'00', 2, 0)")
        loader.store_page(db, "a/old.html", 1, 1, 7, b"json", [])
        assert db.execute("SELECT contentTypeID, templateId FROM Content "
                          "WHERE path='a/old.html'").fetchone() == (1, 7)

    def test_a_continuation_row_is_served_the_same_way_as_its_base(self, db):
        from populate_db import CHUNK_SIZE
        db.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                   "VALUES ('a/big.html', 1, X'00', 2, 0)")
        loader.store_page(db, "a/big.html", 1, 1, 7, bytes(CHUNK_SIZE + 10), [])
        assert db.execute("SELECT contentTypeID, templateId FROM Content "
                          "WHERE path='a/big.html-1'").fetchone() == (1, 7)


# --------------------------------------------------------------------------------------------
# Schema expectations
# --------------------------------------------------------------------------------------------

VERSION_TABLES = """
    CREATE TABLE DocumentationDatabaseVersion (major INTEGER, minor INTEGER, patch INTEGER,
        changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP, who TEXT, comment TEXT);
    CREATE TABLE LastChange (documentationSet TEXT,
        changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP, who TEXT);
"""


class TestSchemaPreflight:
    """record_version writes two tables nothing else in this repository creates, at the very end
    of a run that takes 90 seconds locally and a quarter of an hour in CI. Checking up front is
    the difference between a one-line argument error and losing all of that."""

    def test_a_database_with_every_table_passes(self, db):
        db.executescript(VERSION_TABLES)
        assert loader.check_schema(db) == []

    def test_the_version_tables_are_required(self, db):
        # The fixture is the rest of the schema, so these two are exactly what is missing.
        assert loader.check_schema(db) == ["DocumentationDatabaseVersion", "LastChange"]

    def test_a_missing_content_table_is_reported_too(self, tmp_path):
        empty = sqlite3.connect(tmp_path / "empty.db")
        assert set(loader.check_schema(empty)) == set(loader.REQUIRED_TABLES)

    def test_every_required_table_is_one_the_loader_touches(self):
        source = Path(loader.__file__).read_text(encoding="utf-8")
        for table in loader.REQUIRED_TABLES:
            assert table in source, f"{table} is required but never referenced"


class TestRecordVersion:
    """Untested until now, and the only writer of DocumentationDatabaseVersion in the repo."""

    def test_the_minor_version_is_bumped_and_the_patch_reset(self, db):
        db.executescript(VERSION_TABLES)
        db.execute("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) "
                   "VALUES (2, 1, 3, 'someone', 'before')")
        loader.record_version(db, {"updated": 12106, "inserted": 800})
        assert db.execute("SELECT major, minor, patch FROM DocumentationDatabaseVersion "
                          "ORDER BY rowid DESC LIMIT 1").fetchone() == (2, 2, 0)

    def test_an_empty_version_table_starts_at_2_1_0(self, db):
        db.executescript(VERSION_TABLES)
        loader.record_version(db, {"updated": 1, "inserted": 0})
        assert db.execute("SELECT major, minor, patch FROM DocumentationDatabaseVersion "
                          "ORDER BY rowid DESC LIMIT 1").fetchone() == (2, 1, 0)

    def test_the_documentation_set_is_named_in_lastchange(self, db):
        db.executescript(VERSION_TABLES)
        loader.record_version(db, {"updated": 1, "inserted": 0})
        assert db.execute("SELECT documentationSet FROM LastChange").fetchone() == ("android",)

    def test_the_comment_carries_the_counts(self, db):
        db.executescript(VERSION_TABLES)
        loader.record_version(db, {"updated": 12106, "inserted": 800})
        comment = db.execute("SELECT comment FROM DocumentationDatabaseVersion").fetchone()[0]
        assert "12,106 rows replaced" in comment and "800 added" in comment

