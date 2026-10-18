#!/usr/bin/env python3
"""Tests for remint_dictionary.py / verify_remint_dictionary.py (ADFA-5153).

Run directly: python3 test_remint_dictionary.py
"""
import random
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import brotli

from migrate_content_to_dictionary_brotli import migrate
from populate_db import CHUNK_SIZE, DictionaryCompressor, load_dictionary
import remint_dictionary
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

    def stored_bytes(self) -> int:
        return self.conn.execute("SELECT SUM(LENGTH(content)) FROM Content").fetchone()[0]

    def test_more_seeds_keep_the_candidate_that_stores_smallest(self):
        before = self.snapshot()
        stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=3)

        self.assertEqual([seed for seed, _total in stats["candidates"]], [999, 1000, 1001])
        smallest = min(stats["candidates"], key=lambda c: c[1])
        self.assertEqual((stats["seed"], stats["after"]), smallest)
        self.assertEqual(self.stored_bytes(), stats["after"], "the rows written are not the chosen candidate's")
        self.assertEqual(verify(before, self.db_path), [])

    def test_a_worse_first_candidate_is_not_kept(self):
        """A dictionary of noise first, a trained one second: the trained one
        must win, and every row must be written with it. Each candidate also
        needs its own compressors; reusing the first one's would tie the two
        and keep the noise."""
        before = self.snapshot()
        noise = random.Random(7).randbytes(16384)
        real = remint_dictionary.train_dictionary
        trained = []

        def candidates(samples, dict_size):
            if not trained:
                trained.append(noise)
                return noise
            trained.append(real(samples, dict_size))
            return trained[-1]

        with mock.patch.object(remint_dictionary, "train_dictionary", side_effect=candidates):
            stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=2)

        self.assertEqual(stats["seed"], 1000)
        self.assertEqual(load_dictionary(self.conn), trained[1])
        self.assertLess(stats["candidates"][1][1], stats["candidates"][0][1])
        for path, plain in self.originals.items():
            self.assertEqual(self.plaintext(path), plain, f"{path} lost its content")
        self.assertEqual(verify(before, self.db_path), [])

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


if __name__ == "__main__":
    unittest.main()
