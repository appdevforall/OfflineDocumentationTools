#!/usr/bin/env python3
"""Tests for migrate_content_to_dictionary_brotli.py (ADFA-5153).

Run directly: python3 test_migrate_content_to_dictionary_brotli.py
"""
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

import brotli

from migrate_content_to_dictionary_brotli import migrate
from populate_db import CHUNK_SIZE, DictionaryCompressor, load_dictionary

SCHEMA_SQL = """
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);
CREATE TABLE Content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    languageID INTEGER NOT NULL,
    content BLOB NOT NULL,
    contentTypeID INTEGER NOT NULL,
    templateId INTEGER,
    UNIQUE(path)
);
"""

WORDS = [
    "kotlin", "class", "fun", "val", "var", "override", "interface", "object", "companion",
    "sidebar", "nav", "template", "docs-sidebar", "toc-element", "page.peb", "Content-Type",
]


def make_text(word_count: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return (" ".join(rng.choice(WORDS) for _ in range(word_count))).encode("utf-8")


def insert_plain_chunked(conn, path, language_id, content_type_id, template_id, plain_bytes):
    """Mimics populate_db.py's insert_chunked_content, but with plain
    (no-dictionary) Brotli - i.e. exactly what every pre-ADFA-5153 pipeline
    actually wrote."""
    compressed = brotli.compress(plain_bytes)
    conn.execute(
        "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
        (path, language_id, compressed[:CHUNK_SIZE], content_type_id, template_id),
    )
    offset = CHUNK_SIZE
    n = 1
    while offset < len(compressed):
        conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            (f"{path}-{n}", language_id, compressed[offset:offset + CHUNK_SIZE], content_type_id, template_id),
        )
        offset += CHUNK_SIZE
        n += 1
    return compressed


def reassemble(conn, path, first_content):
    if len(first_content) < CHUNK_SIZE:
        return first_content
    parts = [first_content]
    n = 1
    while True:
        row = conn.execute("SELECT content FROM Content WHERE path = ?", (f"{path}-{n}",)).fetchone()
        if row is None:
            break
        parts.append(row[0])
        if len(row[0]) < CHUNK_SIZE:
            break
        n += 1
    return b"".join(parts)


class MigrateContentToDictionaryBrotliTest(unittest.TestCase):
    def setUp(self):
        # A real file, not :memory: - migrate() parallelizes the read+compress
        # phase across worker threads, each opening its own read-only
        # connection to db_path, which an in-memory database has no path for
        # (and can't share across connections at all).
        fd, path = tempfile.mkstemp(suffix=".db")
        Path(path).unlink(missing_ok=True)
        self.db_path = Path(path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute("INSERT INTO Languages (value) VALUES ('en-US')")
        self.conn.execute("INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli')")
        self.conn.execute("INSERT INTO ContentTypes (value, compression) VALUES ('image/png', 'none')")
        self.language_id = 1
        self.html_type_id = 1
        self.png_type_id = 2
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def test_migrates_plain_brotli_rows_preserving_content(self):
        originals = {}
        for i in range(20):
            plain = make_text(200, seed=i)
            insert_plain_chunked(self.conn, f"k/html/page{i}.html", self.language_id, self.html_type_id, 5, plain)
            originals[f"k/html/page{i}.html"] = plain
        self.conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            ("assets/logo.png", self.language_id, b"\x89PNG-not-really-compressed", self.png_type_id, 0),
        )
        self.conn.commit()

        stats = migrate(self.conn, self.db_path, sample_size=20, dict_size=16384)
        self.assertEqual(stats["scanned"], 20)
        self.assertEqual(stats["migrated"], 20)
        self.assertEqual(stats["already_migrated"], 0)

        dictionary_data = load_dictionary(self.conn)
        with DictionaryCompressor(dictionary_data) as compressor:
            for path, plain in originals.items():
                row = self.conn.execute("SELECT content, templateId FROM Content WHERE path = ?", (path,)).fetchone()
                first_content, template_id = row
                full = reassemble(self.conn, path, first_content)
                self.assertEqual(compressor.decompress(full), plain)
                self.assertEqual(template_id, 5)

        # untouched: not a 'brotli' content type
        png_row = self.conn.execute("SELECT content FROM Content WHERE path = 'assets/logo.png'").fetchone()
        self.assertEqual(png_row[0], b"\x89PNG-not-really-compressed")

    def test_preserves_chunked_rows_across_the_1mb_boundary(self):
        # A handful of small filler rows so the dictionary trainer has
        # more than one sample to work with (zstd's trainer refuses "too
        # few samples" on just one row) - a realistic database always has
        # many rows, this test's chunked row just happens to be one of them.
        for i in range(20):
            insert_plain_chunked(self.conn, f"k/html/filler{i}.html", self.language_id, self.html_type_id, 0,
                                  make_text(150, seed=i))

        # 1.2 MB of high-entropy (incompressible) bytes, so its plain-Brotli
        # form still lands comfortably over CHUNK_SIZE - low-entropy text
        # (e.g. make_text's small vocabulary) compresses far too well at any
        # realistic size to reliably cross that boundary. Exercises the
        # multi-row fragment path on both read (reassemble) and write
        # (re-chunk) sides.
        plain = random.Random(777).randbytes(int(CHUNK_SIZE * 1.2))
        insert_plain_chunked(self.conn, "k/html/big.html", self.language_id, self.html_type_id, 5, plain)
        self.conn.commit()

        # Confirm the fixture actually produced a chunked row before relying on it
        fragment_exists = self.conn.execute(
            "SELECT 1 FROM Content WHERE path = 'k/html/big.html-1'"
        ).fetchone()
        self.assertIsNotNone(fragment_exists, "test fixture did not produce a chunked row; adjust its size")

        stats = migrate(self.conn, self.db_path, sample_size=21, dict_size=16384)
        self.assertEqual(stats["migrated"], 21)

        dictionary_data = load_dictionary(self.conn)
        first_content = self.conn.execute(
            "SELECT content FROM Content WHERE path = 'k/html/big.html'"
        ).fetchone()[0]
        with DictionaryCompressor(dictionary_data) as compressor:
            full = reassemble(self.conn, "k/html/big.html", first_content)
            self.assertEqual(compressor.decompress(full), plain)

    def test_idempotent_second_run_is_a_no_op(self):
        originals = {}
        for i in range(15):
            plain = make_text(150, seed=100 + i)
            insert_plain_chunked(self.conn, f"k/html/p{i}.html", self.language_id, self.html_type_id, 0, plain)
            originals[f"k/html/p{i}.html"] = plain
        self.conn.commit()

        first_stats = migrate(self.conn, self.db_path, sample_size=15, dict_size=16384)
        self.assertEqual(first_stats["migrated"], 15)
        dictionary_after_first_run = load_dictionary(self.conn)

        snapshot = {
            path: self.conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0]
            for path in originals
        }

        second_stats = migrate(self.conn, self.db_path, sample_size=15, dict_size=16384)
        self.assertEqual(second_stats["migrated"], 0)
        self.assertEqual(second_stats["already_migrated"], 15)

        # dictionary must not have been retrained
        self.assertEqual(load_dictionary(self.conn), dictionary_after_first_run)
        # and no row's bytes changed on the no-op second pass
        for path, before in snapshot.items():
            after = self.conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0]
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
