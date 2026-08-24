"""Regression tests for sync_kdoc_json_to_db.py.

Covers the three ways this script could write content the server cannot read
back: ignoring the CHUNK_SIZE fragmentation contract, guessing at a
compression policy it can't determine, and writing plain Brotli into a
database whose rows are dictionary-compressed (schema >= 2.0.0, ADFA-5153).
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_kdoc_json_to_db import (  # noqa: E402
    CHUNK_SIZE,
    DictionaryCompressor,
    backup_database,
    compress_for,
    delete_content_with_fragments,
    is_fragment_path,
    load_dictionary,
    write_content,
)

SCHEMA = """
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE,
                           compression TEXT NOT NULL);
CREATE TABLE Content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    languageID INTEGER NOT NULL,
    content BLOB NOT NULL,
    contentTypeID INTEGER NOT NULL,
    templateId INTEGER NOT NULL DEFAULT 0,
    UNIQUE(path)
);
"""

HTML_TYPE_ID = 12
needs_brotli_cli = pytest.mark.skipif(shutil.which("brotli") is None, reason="brotli CLI not installed")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO ContentTypes (id, value, compression) VALUES (?, 'text/html', 'brotli')",
                       (HTML_TYPE_ID,))
    yield connection
    connection.close()


def add_row(conn, path, blob=b"old", template_id=7, language_id=1):
    cur = conn.execute(
        "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
        (path, language_id, blob, HTML_TYPE_ID, template_id),
    )
    return cur.lastrowid


def rows(conn):
    return dict(conn.execute("SELECT path, content FROM Content"))


class TestIsFragmentPath:
    def test_recognises_a_continuation_row(self):
        assert is_fragment_path("k/kotlin-stdlib/x.html-1", {"k/kotlin-stdlib/x.html", "k/kotlin-stdlib/x.html-1"})

    def test_a_base_row_is_not_a_fragment(self):
        assert not is_fragment_path("k/kotlin-stdlib/x.html", {"k/kotlin-stdlib/x.html"})

    def test_trailing_digits_without_a_base_are_not_a_fragment(self):
        # A real page that just happens to end in "-2" keeps its identity.
        assert not is_fragment_path("k/kotlin-stdlib/part-2", {"k/kotlin-stdlib/part-2"})

    def test_non_numeric_suffix_is_not_a_fragment(self):
        assert not is_fragment_path("k/kotlin-stdlib/all-types", {"k/kotlin-stdlib/all", "k/kotlin-stdlib/all-types"})


class TestWriteContent:
    def test_small_blob_updates_in_place_and_keeps_the_row_id(self, conn):
        row_id = add_row(conn, "k/kotlin-stdlib/x.html")
        write_content(conn, row_id, "k/kotlin-stdlib/x.html", b"new", 1, HTML_TYPE_ID, 7, [])

        assert rows(conn) == {"k/kotlin-stdlib/x.html": b"new"}
        assert conn.execute("SELECT id FROM Content").fetchone()[0] == row_id

    def test_small_blob_clears_stale_fragments_from_a_previous_larger_version(self, conn):
        row_id = add_row(conn, "k/kotlin-stdlib/x.html")
        add_row(conn, "k/kotlin-stdlib/x.html-1")
        add_row(conn, "k/kotlin-stdlib/x.html-2")

        write_content(conn, row_id, "k/kotlin-stdlib/x.html", b"new", 1, HTML_TYPE_ID, 7, [])

        assert set(rows(conn)) == {"k/kotlin-stdlib/x.html"}

    def test_oversized_blob_is_split_across_continuation_rows(self, conn):
        row_id = add_row(conn, "k/kotlin-stdlib/big.html")
        blob = b"z" * (CHUNK_SIZE * 2 + 17)
        chunked_log = []

        write_content(conn, row_id, "k/kotlin-stdlib/big.html", blob, 1, HTML_TYPE_ID, 7, chunked_log)

        stored = rows(conn)
        assert set(stored) == {"k/kotlin-stdlib/big.html", "k/kotlin-stdlib/big.html-1", "k/kotlin-stdlib/big.html-2"}
        # The server detects fragmentation by the base row being exactly
        # CHUNK_SIZE, then reads on until a short row.
        assert len(stored["k/kotlin-stdlib/big.html"]) == CHUNK_SIZE
        assert len(stored["k/kotlin-stdlib/big.html-1"]) == CHUNK_SIZE
        assert len(stored["k/kotlin-stdlib/big.html-2"]) == 17
        reassembled = (stored["k/kotlin-stdlib/big.html"] + stored["k/kotlin-stdlib/big.html-1"]
                       + stored["k/kotlin-stdlib/big.html-2"])
        assert reassembled == blob
        assert chunked_log == [("k/kotlin-stdlib/big.html", len(blob), 3)]

    def test_fragments_inherit_the_base_row_columns(self, conn):
        row_id = add_row(conn, "k/kotlin-stdlib/big.html", template_id=9, language_id=1)
        write_content(conn, row_id, "k/kotlin-stdlib/big.html", b"z" * (CHUNK_SIZE + 1), 1, HTML_TYPE_ID, 9, [])

        for language_id, content_type_id, template_id in conn.execute(
            "SELECT languageID, contentTypeID, templateId FROM Content"
        ):
            assert (language_id, content_type_id, template_id) == (1, HTML_TYPE_ID, 9)

    def test_exactly_chunk_size_stays_a_single_row(self, conn):
        row_id = add_row(conn, "k/kotlin-stdlib/x.html")
        write_content(conn, row_id, "k/kotlin-stdlib/x.html", b"z" * CHUNK_SIZE, 1, HTML_TYPE_ID, 7, [])
        assert set(rows(conn)) == {"k/kotlin-stdlib/x.html"}


class TestDeleteContentWithFragments:
    def test_removes_the_base_row_and_its_fragments_only(self, conn):
        row_id = add_row(conn, "k/kotlin-stdlib/x.html")
        add_row(conn, "k/kotlin-stdlib/x.html-1")
        add_row(conn, "k/kotlin-stdlib/y.html")

        delete_content_with_fragments(conn, row_id, "k/kotlin-stdlib/x.html")

        assert set(rows(conn)) == {"k/kotlin-stdlib/y.html"}

    def test_underscore_in_a_path_is_not_a_like_wildcard(self, conn):
        row_id = add_row(conn, "k/kotlin-stdlib/a_b.html")
        add_row(conn, "k/kotlin-stdlib/a_b.html-1")
        add_row(conn, "k/kotlin-stdlib/aXb.html-1")

        delete_content_with_fragments(conn, row_id, "k/kotlin-stdlib/a_b.html")

        assert set(rows(conn)) == {"k/kotlin-stdlib/aXb.html-1"}


class TestCompressFor:
    def test_unknown_compression_is_an_error(self):
        with pytest.raises(ValueError, match="Unknown compression"):
            compress_for("lzma", b"data", "k/kotlin-stdlib/x.html")

    def test_none_passes_bytes_through(self):
        assert compress_for("none", b"data", "p") == b"data"

    def test_brotli_without_a_dictionary_is_plain_brotli(self):
        import brotli
        assert brotli.decompress(compress_for("brotli", b"data" * 50, "p")) == b"data" * 50


class TestLoadDictionary:
    def test_returns_none_when_the_table_predates_schema_2(self, conn):
        assert load_dictionary(conn) is None

    def test_returns_none_for_an_empty_dictionary_table(self, conn):
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL)")
        assert load_dictionary(conn) is None

    def test_returns_the_stored_bytes(self, conn):
        conn.execute("CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL)")
        conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (b"dictionary-bytes",))
        assert load_dictionary(conn) == b"dictionary-bytes"


@needs_brotli_cli
class TestDictionaryCompressor:
    def test_round_trips_through_the_dictionary(self):
        dictionary = bytes(range(256)) * 64
        payload = b'{"id":"k/kotlin-stdlib/x","blocks":[]}' * 20
        compressor = DictionaryCompressor(dictionary)
        try:
            assert compressor.decompress(compressor.compress(payload)) == payload
        finally:
            compressor.close()

    def test_dictionary_output_is_not_readable_as_plain_brotli(self):
        # The whole reason this matters: the two encodings are not
        # interchangeable, so writing plain Brotli into a dictionary database
        # produces rows nothing can decode.
        import brotli
        dictionary = bytes(range(256)) * 64
        compressor = DictionaryCompressor(dictionary)
        try:
            blob = compressor.compress(b"kotlin stdlib documentation payload" * 40)
        finally:
            compressor.close()
        with pytest.raises(Exception):
            brotli.decompress(blob)

    def test_compress_for_uses_the_dictionary_when_given_one(self):
        dictionary = bytes(range(256)) * 64
        compressor = DictionaryCompressor(dictionary)
        try:
            payload = b"payload" * 100
            blob = compress_for("brotli", payload, "p", compressor)
            assert compressor.decompress(blob) == payload
        finally:
            compressor.close()


class TestBackupDatabase:
    def test_backup_is_a_readable_database_not_a_file_copy(self, tmp_path):
        db_path = tmp_path / "documentation.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli')")
        conn.commit()
        conn.close()

        backup_path = backup_database(str(db_path))

        assert Path(backup_path).is_file()
        restored = sqlite3.connect(backup_path)
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute("SELECT value FROM ContentTypes").fetchone()[0] == "text/html"
        finally:
            restored.close()
