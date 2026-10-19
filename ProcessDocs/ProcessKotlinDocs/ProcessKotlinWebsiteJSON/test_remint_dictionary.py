#!/usr/bin/env python3
"""Tests for remint_dictionary.py / verify_remint_dictionary.py (ADFA-5153).

Run directly: python3 test_remint_dictionary.py
"""
import hashlib
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import brotli

from migrate_content_to_dictionary_brotli import migrate
from populate_db import CHUNK_SIZE, DictionaryCompressor, load_dictionary
from remint_dictionary import remint
from verify_remint_dictionary import verify
from test_migrate_content_to_dictionary_brotli import SCHEMA_SQL, insert_plain_chunked, make_text


class RemintDictionaryTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        Path(path).unlink(missing_ok=True)
        self.db_path = Path(path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute("INSERT INTO Languages (value) VALUES ('en-US')")
        self.conn.execute("INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli')")
        self.html_type_id = 1
        self.originals = {}
        # Spread across doc sets, so the stratified sampler has something to stratify.
        for doc_set in ("a", "j", "k"):
            for i in range(12):
                path_ = f"{doc_set}/page{i}.html"
                plain = make_text(220, seed=hash((doc_set, i)) % 9973)
                insert_plain_chunked(self.conn, path_, 1, self.html_type_id, 0, plain)
                self.originals[path_] = plain
        self.conn.commit()
        migrate(self.conn, sample_size=30, dict_size=16384)
        self.conn.commit()
        self.first_dictionary = load_dictionary(self.conn)

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def snapshot(self) -> Path:
        """A copy of the database as it stands, to verify a re-mint against."""
        fd, path = tempfile.mkstemp(suffix=".db")
        Path(path).unlink(missing_ok=True)
        copy = Path(path)
        shutil.copy2(self.db_path, copy)
        self.addCleanup(copy.unlink, True)
        return copy

    def plaintext(self, path: str) -> bytes:
        with DictionaryCompressor(load_dictionary(self.conn)) as compressor:
            row = self.conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()
            return compressor.decompress(row[0])

    def test_replaces_the_dictionary_and_preserves_every_payload(self):
        before = self.snapshot()
        # A different seed is what makes this a real re-mint: the sampler is
        # deterministic, so re-training on the same corpus with the same seed
        # reproduces the stored dictionary byte for byte (see the sibling test).
        stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999)
        self.assertEqual(stats["items"], len(self.originals))

        self.assertNotEqual(load_dictionary(self.conn), self.first_dictionary,
                            "the dictionary was not actually re-minted")
        for path, plain in self.originals.items():
            self.assertEqual(self.plaintext(path), plain, f"{path} lost its content")
        self.assertEqual(verify(before, self.db_path), [])

    def test_same_seed_reproduces_the_stored_dictionary(self):
        """The sampler is seeded so a dictionary's training set can be
        reproduced later -- which also means re-minting with the same seed and
        corpus is a no-op, and the verifier says so rather than pretending
        something changed."""
        before = self.snapshot()
        remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20)
        self.assertEqual(load_dictionary(self.conn), self.first_dictionary)
        self.assertEqual(verify(before, self.db_path), [])

    def test_undecodable_row_aborts_with_nothing_written(self):
        self.conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, 1, ?, ?, 0)",
            ("a/broken.html", b"not brotli at all", self.html_type_id))
        self.conn.commit()
        snapshot = {path: content for path, content in
                    self.conn.execute("SELECT path, content FROM Content")}

        with self.assertRaises(RuntimeError):
            remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20)

        self.assertEqual(load_dictionary(self.conn), self.first_dictionary, "dictionary was replaced anyway")
        self.assertEqual({path: content for path, content in
                          self.conn.execute("SELECT path, content FROM Content")}, snapshot,
                         "rows were rewritten despite the abort")

    def test_verifier_catches_a_corrupted_remint(self):
        """Guards against a vacuous verifier: hand it a database whose content
        does not match and it must object."""
        before = self.snapshot()
        remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=4242)
        with DictionaryCompressor(load_dictionary(self.conn)) as compressor:
            wrong = compressor.compress(b"replaced with something else entirely")
        self.conn.execute("UPDATE Content SET content = ? WHERE path = 'j/page3.html'", (wrong,))
        self.conn.commit()

        problems = verify(before, self.db_path)
        self.assertEqual([path for path, _why in problems], ["j/page3.html"])


VERSION_TABLE = """
CREATE TABLE DocumentationDatabaseVersion (
  major      INT NOT NULL,
  minor      INT NOT NULL,
  patch      INT NOT NULL,
  who        TEXT NOT NULL,
  comment    TEXT NOT NULL,
  changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL, UNIQUE(name));
"""
HERE = Path(__file__).resolve().parent


class CompressedTemplatesRefusalTest(unittest.TestCase):
    """From documentation 3.0.0 every template is compressed against the dictionary, and this re-mint and its
    verifier handle Content only: re-minting would leave every template undecodable, and the verifier would pass it.
    Both refuse such a database, as the app reads its version, and write nothing."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.db_path = self.dir / "documentation.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA_SQL + VERSION_TABLE)
        conn.execute("INSERT INTO Languages (value) VALUES ('en-US')")
        conn.execute("INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli')")
        for doc_set in ("a", "j", "k"):
            for i in range(12):
                insert_plain_chunked(conn, f"{doc_set}/page{i}.html", 1, 1, 0,
                                     make_text(220, seed=hash((doc_set, i)) % 9973))
        conn.commit()
        migrate(conn, sample_size=30, dict_size=16384)
        conn.commit()
        with DictionaryCompressor(load_dictionary(conn)) as compressor:
            conn.execute("INSERT INTO Templates (name, content) VALUES ('page.peb', ?)",
                         (compressor.compress(b"<html>{{ title }}</html>"),))
        conn.commit()
        conn.close()

    def declare(self, *rows):
        """Appends (major, changeTime) rows to DocumentationDatabaseVersion, in order."""
        conn = sqlite3.connect(self.db_path)
        conn.executemany("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                         "VALUES (?, 0, 0, 'test', 'test', ?)", rows)
        conn.commit()
        conn.close()

    def sha(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def remint(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return remint(conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999)
        finally:
            conn.close()

    def assert_remint_refused(self, major: int):
        sha = self.sha()
        with self.assertRaises(RuntimeError) as raised:
            self.remint()
        self.assertIn(f"declares documentation major {major}", str(raised.exception))
        self.assertIn("PR #48", str(raised.exception))
        self.assertEqual(self.sha(), sha)

    def test_remint_refuses_a_database_declaring_major_3(self):
        self.declare((2, "2026-01-01 00:00:00"), (3, "2026-10-19 00:00:00"))
        self.assert_remint_refused(3)

    def test_remint_refuses_a_later_major_too(self):
        self.declare((4, "2026-10-19 00:00:00"))
        self.assert_remint_refused(4)

    def test_remint_still_re_mints_major_2(self):
        self.declare((2, "2026-10-19 00:00:00"))
        self.assertEqual(self.remint()["items"], 36)

    def test_reads_the_newest_changetime_first(self):
        # An older row inserted later is not the newest: the app orders by changeTime before rowid.
        self.declare((3, "2026-10-19 00:00:00"), (2, "2026-01-01 00:00:00"))
        self.assert_remint_refused(3)

    def test_breaks_a_changetime_tie_by_the_newest_rowid(self):
        self.declare((3, "2026-10-19 00:00:00"), (2, "2026-10-19 00:00:00"))
        self.assertEqual(self.remint()["items"], 36)

    def test_command_line_refuses_before_the_backup(self):
        self.declare((3, "2026-10-19 00:00:00"))
        sha = self.sha()
        result = subprocess.run([sys.executable, str(HERE / "remint_dictionary.py"), str(self.db_path)],
                                capture_output=True, text=True, cwd=self.dir, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("PR #48", result.stderr)
        self.assertIn("nothing was changed", result.stderr)
        self.assertEqual(self.sha(), sha)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["documentation.db"])

    def test_verifier_refuses_either_database_declaring_major_3(self):
        original = self.dir / "original.db"
        shutil.copy2(self.db_path, original)
        self.declare((3, "2026-10-19 00:00:00"))
        for old, new in ((original, self.db_path), (self.db_path, original)):
            with self.assertRaises(RuntimeError) as raised:
                verify(old, new)
            self.assertIn(f"{self.db_path} declares documentation major 3", str(raised.exception))
            self.assertIn("PR #48", str(raised.exception))

    def test_verifier_command_line_exits_1(self):
        original = self.dir / "original.db"
        shutil.copy2(self.db_path, original)
        self.declare((3, "2026-10-19 00:00:00"))
        result = subprocess.run([sys.executable, str(HERE / "verify_remint_dictionary.py"), str(original),
                                 str(self.db_path)], capture_output=True, text=True, cwd=self.dir, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("declares documentation major 3", result.stderr)
        self.assertNotIn("Byte-identical", result.stdout)


if __name__ == "__main__":
    unittest.main()
