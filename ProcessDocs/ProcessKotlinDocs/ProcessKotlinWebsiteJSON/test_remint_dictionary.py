#!/usr/bin/env python3
"""Tests for remint_dictionary.py / verify_remint_dictionary.py (ADFA-5153).

Run directly: python3 test_remint_dictionary.py
"""
import contextlib
import io
import random
import shutil
import sqlite3
import sys
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

# The dictionary the fixture is first minted with. Small on purpose, so a 16 KiB re-mint beats it by hundreds of
# bytes rather than a handful, whatever zstd and brotli version runs the tests.
MIGRATE_DICT_SIZE = 1024


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
        # Spread across doc sets, so the stratified sampler has something to stratify. The text seeds are fixed:
        # hash() of a tuple changes with PYTHONHASHSEED, which made which candidate won vary from run to run.
        for k, doc_set in enumerate(("a", "j", "k")):
            for i in range(12):
                path_ = f"{doc_set}/page{i}.html"
                plain = make_text(220, seed=100 * k + i)
                insert_plain_chunked(self.conn, path_, 1, self.html_type_id, 0, plain)
                self.originals[path_] = plain
        self.conn.commit()
        migrate(self.conn, sample_size=30, dict_size=MIGRATE_DICT_SIZE)
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
        remint(self.conn, sample_size=30, dict_size=MIGRATE_DICT_SIZE, training_bytes=1 << 20)
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

    def test_a_winner_in_the_middle_is_kept(self):
        """Noise, a trained dictionary, then noise: the second candidate must be kept, its dictionary stored and its
        rows written - neither the first nor the last, so keeping or writing either of those fails."""
        before = self.snapshot()
        noise = random.Random(9)
        real = remint_dictionary.train_dictionary
        trained = []

        def candidates(samples, dict_size):
            trained.append(real(samples, dict_size) if len(trained) == 1 else noise.randbytes(dict_size))
            return trained[-1]

        with mock.patch.object(remint_dictionary, "train_dictionary", side_effect=candidates):
            stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=3)

        self.assertEqual(stats["seed"], 1000)
        self.assertEqual(load_dictionary(self.conn), trained[1])
        self.assertEqual(stats["after"], stats["candidates"][1][1])
        self.assertEqual(self.stored_bytes(), stats["after"], "the rows written are not the chosen candidate's")
        self.assertLess(stats["candidates"][1][1], min(stats["candidates"][0][1], stats["candidates"][2][1]))
        self.assertEqual(verify(before, self.db_path), [])

    def test_each_candidate_trains_on_its_own_sample(self):
        """Candidates differ only by their sample seed; if every pass drew the same sample, --seeds would retrain
        one dictionary several times."""
        real = remint_dictionary.train_dictionary
        samples_seen = []

        def record(samples, dict_size):
            samples_seen.append(list(samples))
            return real(samples, dict_size)

        with mock.patch.object(remint_dictionary, "train_dictionary", side_effect=record):
            remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=3)

        self.assertEqual(len(samples_seen), 3)
        for i in range(3):
            for j in range(i + 1, 3):
                self.assertNotEqual(samples_seen[i], samples_seen[j], f"candidates {i} and {j} trained on one sample")

    def test_main_reports_how_much_smaller(self):
        stored_before = self.stored_bytes()
        printed = self.run_main("--sample-seed", "999", "--seeds", "1")
        stored_after = self.stored_bytes()
        self.assertLess(stored_after, stored_before)
        self.assertIn(f"{stored_before:,} -> {stored_after:,} bytes "
                      f"({100 * (stored_before - stored_after) / stored_before:.1f}% smaller)", printed)

    def contents(self) -> dict:
        return {path: content for path, content in self.conn.execute("SELECT path, content FROM Content")}

    def test_keeps_the_current_dictionary_when_no_candidate_stores_less(self):
        """Every candidate is noise, so each stores more than the dictionary in place: nothing may be written."""
        before, rows = self.snapshot(), self.contents()
        noise = random.Random(11)
        with mock.patch.object(remint_dictionary, "train_dictionary",
                               side_effect=lambda samples, size: noise.randbytes(size)):
            stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=2)

        self.assertEqual(load_dictionary(self.conn), self.first_dictionary,
                         "replaced by a candidate that stores more")
        self.assertEqual(self.contents(), rows, "rows were rewritten")
        self.assertEqual(verify(before, self.db_path), [])
        self.assertEqual(stats["kept"], "current")
        self.assertGreater(stats["best_candidate"][1], stats["before"])
        self.assertEqual(stats["after"], stats["before"])

    def test_a_second_remint_with_the_same_seeds_writes_nothing(self):
        """The winning seed retrains the stored dictionary exactly, so it ties, and a tie is no gain."""
        remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=3)
        dictionary, rows, changes = load_dictionary(self.conn), self.contents(), self.conn.total_changes
        stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=3)

        # Rewriting identical bytes leaves the contents equal, so count the writes.
        self.assertEqual(self.conn.total_changes, changes, "rows were rewritten for no gain")
        self.assertEqual(load_dictionary(self.conn), dictionary)
        self.assertEqual(self.contents(), rows)
        self.assertEqual(stats["kept"], "current")
        self.assertEqual(stats["best_candidate"][1], stats["before"])

    def run_main(self, *args: str) -> str:
        """main() on the fixture's file, with --no-backup; returns what it printed."""
        self.conn.close()
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["remint_dictionary.py", str(self.db_path), "--no-backup",
                                             "--sample-size", "30", "--dict-size", "16384",
                                             "--training-bytes", str(1 << 20), *args]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            remint_dictionary.main()
        self.conn = sqlite3.connect(self.db_path)
        return out.getvalue()

    def test_main_says_it_kept_the_dictionary_and_leaves_the_file_alone(self):
        data = self.db_path.read_bytes()
        noise = random.Random(12)
        with mock.patch.object(remint_dictionary, "train_dictionary",
                               side_effect=lambda samples, size: noise.randbytes(size)):
            printed = self.run_main("--sample-seed", "999", "--seeds", "2")
        self.assertIn("Kept the current dictionary", printed)
        self.assertNotIn("Re-minted", printed)
        self.assertEqual(self.db_path.read_bytes(), data, "the file changed although nothing was to be written")

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
