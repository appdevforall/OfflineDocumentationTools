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

from migrate_content_to_dictionary_brotli import collect_training_samples, migrate, read_item
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
        # A real file, not :memory: - the tests below reopen the database to
        # check what was actually committed, and the trigger fixture needs a
        # schema an in-memory connection would not outlive.
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

        stats = migrate(self.conn, sample_size=20, dict_size=16384)
        self.assertEqual(stats["scanned"], 20)
        self.assertEqual(stats["migrated"], 20)
        self.assertEqual(stats["already"], 0)

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

        # High-entropy bytes to push the compressed form over CHUNK_SIZE (text
        # from make_text's small vocabulary compresses far too well to cross
        # that boundary at any realistic size), plus a text tail so the encoder
        # actually references the shared dictionary. Noise alone references
        # nothing, and migrate() then correctly reports the row as having
        # nothing to gain rather than migrating it.
        plain = random.Random(777).randbytes(int(CHUNK_SIZE * 1.2)) + make_text(4000, seed=777)
        insert_plain_chunked(self.conn, "k/html/big.html", self.language_id, self.html_type_id, 5, plain)
        self.conn.commit()

        # Confirm the fixture actually produced a chunked row before relying on it
        fragment_exists = self.conn.execute(
            "SELECT 1 FROM Content WHERE path = 'k/html/big.html-1'"
        ).fetchone()
        self.assertIsNotNone(fragment_exists, "test fixture did not produce a chunked row; adjust its size")

        stats = migrate(self.conn, sample_size=21, dict_size=16384)
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

        first_stats = migrate(self.conn, sample_size=15, dict_size=16384)
        self.assertEqual(first_stats["migrated"], 15)
        dictionary_after_first_run = load_dictionary(self.conn)

        snapshot = {
            path: self.conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0]
            for path in originals
        }

        second_stats = migrate(self.conn, sample_size=15, dict_size=16384)
        self.assertEqual(second_stats["migrated"], 0)
        self.assertEqual(second_stats["already"], 15)

        # dictionary must not have been retrained
        self.assertEqual(load_dictionary(self.conn), dictionary_after_first_run)
        # and no row's bytes changed on the no-op second pass
        for path, before in snapshot.items():
            after = self.conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0]
            self.assertEqual(before, after)

    def test_chain_numbered_from_minus_two_is_migrated_not_miscounted(self):
        """ADFA-5171 chains must not read as 'already dictionary-compressed'.

        Probing "<path>-1" returns a truncated stream for a chain numbered from
        -2; the decode then fails, and counting that as already-migrated leaves
        the row plain-Brotli while the run reports a clean finish."""
        for i in range(20):
            insert_plain_chunked(self.conn, f"k/html/filler{i}.html", self.language_id, self.html_type_id, 0,
                                 make_text(150, seed=i))
        plain = random.Random(4242).randbytes(int(CHUNK_SIZE * 1.4)) + make_text(4000, seed=4242)
        compressed = brotli.compress(plain)
        self.assertGreater(len(compressed), CHUNK_SIZE, "fixture must be chunked; adjust its size")
        self.conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            ("k/html/misnumbered.html", self.language_id, compressed[:CHUNK_SIZE], self.html_type_id, 0),
        )
        # the continuation numbered from -2, with no -1 at all
        offset, number = CHUNK_SIZE, 2
        while offset < len(compressed):
            self.conn.execute(
                "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
                (f"k/html/misnumbered.html-{number}", self.language_id,
                 compressed[offset:offset + CHUNK_SIZE], self.html_type_id, 0),
            )
            offset += CHUNK_SIZE
            number += 1
        self.conn.commit()

        stats = migrate(self.conn, sample_size=21, dict_size=16384)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["already"], 0, "a -2 chain was miscounted as already migrated")
        self.assertEqual(stats["migrated"], 21)

        # Content survives, and the rewritten chain is -1-based, which is what
        # WebServer.kt's reassembly loop actually probes.
        with DictionaryCompressor(load_dictionary(self.conn)) as compressor:
            self.assertEqual(compressor.decompress(read_item(self.conn, "k/html/misnumbered.html")), plain)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM Content WHERE path = 'k/html/misnumbered.html-1'").fetchone())

    def test_undecodable_row_is_an_error_not_a_success(self):
        for i in range(20):
            insert_plain_chunked(self.conn, f"k/html/ok{i}.html", self.language_id, self.html_type_id, 0,
                                 make_text(150, seed=i))
        self.conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            ("k/html/corrupt.html", self.language_id, b"not brotli at all", self.html_type_id, 0),
        )
        self.conn.commit()

        stats = migrate(self.conn, sample_size=20, dict_size=16384)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["already"], 0)
        self.assertEqual([path for path, _detail in stats["problems"]], ["k/html/corrupt.html"])
        # left exactly as it was, not half-written
        self.assertEqual(
            self.conn.execute("SELECT content FROM Content WHERE path = 'k/html/corrupt.html'").fetchone()[0],
            b"not brotli at all")

    def test_bookshelf_entry_survives_migration(self):
        """Content's AddBook/DeleteBook triggers make a delete+insert cycle on a
        '%.pdf' path silently replace the curated Bookshelf row."""
        self.conn.executescript("""
            CREATE TABLE BookCategories (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT);
            CREATE TABLE Bookshelf (
                contentID INTEGER NOT NULL, title STRING DEFAULT '', description STRING DEFAULT '',
                bookCategoryID INTEGER,
                FOREIGN KEY (bookCategoryID) REFERENCES BookCategories(id), UNIQUE(title, bookCategoryId));
            CREATE TRIGGER DeleteBook AFTER DELETE ON Content WHEN OLD.path LIKE '%.pdf'
            BEGIN DELETE FROM Bookshelf WHERE contentID = OLD.id; END;
            CREATE TRIGGER AddBook AFTER INSERT ON Content WHEN NEW.path LIKE '%.pdf'
            BEGIN INSERT INTO Bookshelf (contentID, title) VALUES (NEW.id, CURRENT_TIMESTAMP || NEW.id); END;
        """)
        self.conn.execute("INSERT INTO ContentTypes (value, compression) VALUES ('application/pdf', 'brotli')")
        pdf_type_id = self.conn.execute(
            "SELECT id FROM ContentTypes WHERE value = 'application/pdf'").fetchone()[0]
        self.conn.execute("INSERT INTO BookCategories (category) VALUES ('Programming')")

        for i in range(20):
            insert_plain_chunked(self.conn, f"k/html/f{i}.html", self.language_id, self.html_type_id, 0,
                                 make_text(150, seed=i))
        insert_plain_chunked(self.conn, "bookshelfplugin/Notes.pdf", self.language_id, pdf_type_id, 0,
                             make_text(400, seed=9))
        pdf_id = self.conn.execute(
            "SELECT id FROM Content WHERE path = 'bookshelfplugin/Notes.pdf'").fetchone()[0]
        self.conn.execute("DELETE FROM Bookshelf")   # drop what AddBook just generated
        self.conn.execute(
            "INSERT INTO Bookshelf (contentID, bookCategoryID, title, description) VALUES (?, 1, ?, ?)",
            (pdf_id, "Notes for Professionals", "A curated description"))
        self.conn.commit()

        stats = migrate(self.conn, sample_size=21, dict_size=16384)
        self.assertEqual(stats["migrated"], 21)

        shelf = self.conn.execute(
            "SELECT contentID, bookCategoryID, title, description FROM Bookshelf").fetchall()
        self.assertEqual(shelf, [(pdf_id, 1, "Notes for Professionals", "A curated description")],
                         "the curated Bookshelf entry was replaced")

    def test_second_run_leaves_rows_whose_dictionary_was_never_referenced(self):
        """A payload the encoder never needed the dictionary for decodes both
        ways, so 'a plain decode succeeded' does not mean 'not yet migrated'."""
        for i in range(20):
            insert_plain_chunked(self.conn, f"k/html/t{i}.html", self.language_id, self.html_type_id, 0,
                                 make_text(150, seed=i))
        # 24 bytes of high-entropy noise: nothing in any dictionary can help it.
        self.conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, ?, ?, ?, ?)",
            ("k/html/tiny.bin", self.language_id, brotli.compress(random.Random(5).randbytes(24)),
             self.html_type_id, 0),
        )
        self.conn.commit()

        migrate(self.conn, sample_size=20, dict_size=16384)
        snapshot = self.conn.execute("SELECT path, content FROM Content").fetchall()
        second = migrate(self.conn, sample_size=20, dict_size=16384)
        self.assertEqual(second["migrated"], 0, "a second run re-migrated rows it should have left alone")
        self.assertEqual(second["errors"], 0)
        self.assertEqual(self.conn.execute("SELECT path, content FROM Content").fetchall(), snapshot)

    def test_training_sample_is_stratified_and_reproducible(self):
        for doc_set, count in (("a", 40), ("j", 30), ("k", 10)):
            for i in range(count):
                insert_plain_chunked(self.conn, f"{doc_set}/page{i}.html", self.language_id,
                                     self.html_type_id, 0, make_text(200, seed=hash((doc_set, i)) % 10_000))
        self.conn.commit()
        base_rows = self.conn.execute(
            "SELECT C.path, C.languageID, C.contentTypeID, C.templateId, LENGTH(C.content) "
            "FROM Content C, ContentTypes CT WHERE C.contentTypeID = CT.id AND CT.compression = 'brotli' "
            "ORDER BY C.path").fetchall()

        first = collect_training_samples(self.conn, base_rows, sample_size=30, byte_budget=1 << 20, seed=7)
        again = collect_training_samples(self.conn, base_rows, sample_size=30, byte_budget=1 << 20, seed=7)
        self.assertEqual(first, again, "same seed must produce the same training set")

        # Every doc set should be represented -- the whole point of stratifying.
        # Sample paths back out by matching the plaintext we know we inserted.
        sampled_sets = set()
        for sample in first:
            row = self.conn.execute(
                "SELECT path FROM Content WHERE content = ?", (brotli.compress(sample),)).fetchone()
            if row:
                sampled_sets.add(row[0].split("/", 1)[0])
        self.assertGreaterEqual(len(sampled_sets), 2, f"sample covered only {sampled_sets}")


if __name__ == "__main__":
    unittest.main()
