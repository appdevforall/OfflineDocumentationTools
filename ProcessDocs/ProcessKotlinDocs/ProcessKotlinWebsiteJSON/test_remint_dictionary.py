#!/usr/bin/env python3
"""Tests for remint_dictionary.py / verify_remint_dictionary.py (ADFA-5153; templates: ADFA-3305).

Run directly: python3 test_remint_dictionary.py
"""
import contextlib
import gc
import io
import random
import shutil
import hashlib
import string
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest import mock

import brotli

from migrate_content_to_dictionary_brotli import load_base_rows, migrate
from populate_db import CHUNK_SIZE, DictionaryCompressor, load_dictionary
import remint_dictionary
from remint_dictionary import remint
from verify_remint_dictionary import verify
from test_migrate_content_to_dictionary_brotli import SCHEMA_SQL, WORDS, insert_plain_chunked, make_text

# The real database's version and template tables, which SCHEMA_SQL leaves out.
VERSION_SQL = """
CREATE TABLE DocumentationDatabaseVersion (
  major INT NOT NULL, minor INT NOT NULL, patch INT NOT NULL, who TEXT NOT NULL, comment TEXT NOT NULL,
  changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
TEMPLATES_SQL = """
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL, UNIQUE(name));
"""
TEMPLATE_NAMES = ("layout.pebble", "page.peb", "nav.peb")
# The tables scripts/store_pages_in_blocks.py adds (ADFA-6683), as it creates them.
BLOCK_TABLES = {
    "ContentBlocks": "CREATE TABLE ContentBlocks (id INTEGER PRIMARY KEY AUTOINCREMENT, data BLOB NOT NULL)",
    "ContentBlockItems": ("CREATE TABLE ContentBlockItems (contentId INTEGER NOT NULL PRIMARY KEY REFERENCES "
                          "Content(id), blockId INTEGER NOT NULL REFERENCES ContentBlocks(id), offset INTEGER NOT "
                          "NULL, length INTEGER NOT NULL) WITHOUT ROWID"),
}
HERE = Path(__file__).resolve().parent

# The dictionary the fixture is first minted with. Small on purpose, so a 16 KiB re-mint beats it by hundreds of
# bytes rather than a handful, whatever zstd and brotli version runs the tests.
MIGRATE_DICT_SIZE = 1024


def make_template(seed: int, size: int = 6000) -> bytes:
    """Template text that shares nothing with make_text's words, so only a dictionary that has seen it
    compresses it well."""
    rng = random.Random(seed)
    return ("{% block body %}" + "".join(rng.choices(string.ascii_letters + string.digits + "<>/= ", k=size))
            + "{% endblock %}").encode("utf-8")


class _RemintFixture(unittest.TestCase):
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

    def add_versions_and_templates(self, majors=(3,), compressed=True, version_table=True):
        """Version rows oldest first, each with a later changeTime than the last, then TEMPLATE_NAMES'
        templates, compressed against the current dictionary or stored as plain text."""
        self.conn.executescript((VERSION_SQL if version_table else "") + TEMPLATES_SQL)
        for month, major in enumerate(majors, start=1):
            self.conn.execute("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                              "VALUES (?, 0, 0, 'test', 'test', ?)", (major, f"2026-{month:02}-01 00:00:00"))
        self.templates = {name: make_template(i) for i, name in enumerate(TEMPLATE_NAMES)}
        with DictionaryCompressor(self.first_dictionary) as compressor:
            for name, text in self.templates.items():
                self.conn.execute("INSERT INTO Templates (name, content) VALUES (?, ?)",
                                  (name, compressor.compress(text) if compressed else text))
        self.conn.commit()

    def stored_templates(self) -> dict:
        return {name: bytes(content) for name, content in self.conn.execute("SELECT name, content FROM Templates")}


class RemintDictionaryTest(_RemintFixture):
    def test_replaces_the_dictionary_and_preserves_every_payload(self):
        before = self.snapshot()
        # A different seed is what makes this a real re-mint: on this fixture the
        # same seed draws the same sample and retrains the stored dictionary byte for
        # byte (see the sibling test).
        stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999)
        self.assertEqual(stats["items"], len(self.originals))

        self.assertNotEqual(load_dictionary(self.conn), self.first_dictionary,
                            "the dictionary was not actually re-minted")
        for path, plain in self.originals.items():
            self.assertEqual(self.plaintext(path), plain, f"{path} lost its content")
        self.assertEqual(verify(before, self.db_path), [])

    def test_same_seed_reproduces_the_stored_dictionary(self):
        """The sampler is seeded so a dictionary's training set can be
        reproduced later. On this fixture the same seed draws the same sample, so
        the re-mint keeps the stored dictionary and the verifier finds nothing
        changed. That is not true in general: the sampler weighs each doc set by
        its stored bytes, which a re-mint changes, so a second re-mint of real data
        at the same seed drew a different sample (and stored 0.1% less)."""
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
        self.assertEqual(list(self.db_path.parent.glob(self.db_path.name + ".backup-*")), [],
                         "--no-backup wrote a backup")
        self.assertIn(f"{stored_before:,} -> {stored_after:,} bytes "
                      f"({100 * (stored_before - stored_after) / stored_before:.1f}% smaller)", printed)

    def test_each_pass_removes_its_temporary_dictionaries(self):
        """Every worker's compressors write their dictionary to a temp directory; a pass must remove them when it
        ends, not leave them to the process's exit."""
        private = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, private, True)
        with mock.patch.object(tempfile, "tempdir", str(private)):
            remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=3,
                   max_workers=2)
            left = sorted(p.name for p in private.iterdir())
        self.assertEqual(left, [], "temp dictionaries left behind")

    def test_a_losing_candidates_rows_are_freed_before_the_next_pass(self):
        """A trained dictionary, then noise twice: entering the third pass, only the best so far (the first pass's
        rows) may still be alive, not the second pass's losing rows as well."""
        real_convert, real_train = remint_dictionary.convert_all, remint_dictionary.train_dictionary
        noise = random.Random(13)

        class Rows(list):
            """A list that can be weakly referenced."""

        returned, alive_at_entry, trained = [], [], []

        def convert_all(*args):
            gc.collect()
            alive_at_entry.append(sum(ref() is not None for ref in returned))
            converted, total, problems = real_convert(*args)
            rows = Rows(converted)
            returned.append(weakref.ref(rows))
            return rows, total, problems

        def train(samples, dict_size):
            trained.append(real_train(samples, dict_size) if not trained else noise.randbytes(dict_size))
            return trained[-1]

        with mock.patch.object(remint_dictionary, "convert_all", side_effect=convert_all), \
                mock.patch.object(remint_dictionary, "train_dictionary", side_effect=train):
            stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=3)

        self.assertEqual(stats["seed"], 999)
        self.assertEqual(alive_at_entry, [0, 1, 1])

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

    def test_a_candidate_that_only_ties_writes_nothing(self):
        """Every candidate is the stored dictionary itself, so it stores exactly as much, and a tie is no gain."""
        remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999)
        dictionary, rows, changes = load_dictionary(self.conn), self.contents(), self.conn.total_changes
        with mock.patch.object(remint_dictionary, "train_dictionary", side_effect=lambda samples, size: dictionary):
            stats = remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, seeds=2)

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


class _StopTraining(Exception):
    pass


class RemintDefaultsTest(_RemintFixture):
    """The re-mint's defaults, measured on the ADFA-3305 ship-time database: a 768 KiB dictionary trained on up to
    3 x 1,200 rows within 128 MiB. With the first mint's 300-row sample, two of four 768 KiB seeds stored 4-5 MB more
    than a 256 KiB dictionary."""

    def test_a_default_remint_trains_768_kib_on_the_larger_sample(self):
        seen = {}
        real_collect = remint_dictionary.collect_training_samples

        def collect(conn, rows, sample_size, byte_budget, seed, **kwargs):
            seen["sample"] = (sample_size, byte_budget)
            return real_collect(conn, rows, sample_size, byte_budget, seed, **kwargs)

        def train(samples, dict_size):
            seen["dict_size"] = dict_size
            raise _StopTraining()

        with mock.patch.object(remint_dictionary, "collect_training_samples", side_effect=collect), \
                mock.patch.object(remint_dictionary, "train_dictionary", side_effect=train):
            with self.assertRaises(_StopTraining):
                remint(self.conn)
        self.assertEqual(seen, {"sample": (1200, 128 * 1024 * 1024), "dict_size": 768 * 1024})
        self.assertEqual(load_dictionary(self.conn), self.first_dictionary, "a stopped re-mint wrote a dictionary")

    def test_the_command_line_uses_the_same_defaults(self):
        calls = []

        def fake_remint(conn, *args, **kwargs):
            calls.append(args)
            return {"items": 0, "templates": 0, "before": 1, "after": 1, "kept": "current", "best_candidate": (0, 1),
                    "dictionary_bytes": 0, "seed": None}

        with mock.patch.object(remint_dictionary, "remint", side_effect=fake_remint), \
                mock.patch.object(sys, "argv", ["remint_dictionary.py", str(self.db_path), "--no-backup"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            remint_dictionary.main()
        sample_size, dict_size, training_bytes = calls[0][:3]
        self.assertEqual((sample_size, dict_size, training_bytes), (1200, 768 * 1024, 128 * 1024 * 1024))

    def test_the_first_mint_keeps_its_smaller_defaults(self):
        # The larger defaults are the re-mint's own: the docstring's "Dictionary size" and CLAUDE.md compare them
        # with the first mint's 256 KiB on up to 900 rows within 32 MiB.
        import migrate_content_to_dictionary_brotli
        import populate_db
        self.assertEqual((populate_db.DEFAULT_DICT_SIZE, migrate_content_to_dictionary_brotli.DEFAULT_SAMPLE_SIZE,
                          migrate_content_to_dictionary_brotli.DEFAULT_TRAINING_BYTES),
                         (256 * 1024, 300, 32 * 1024 * 1024))


class RemintTemplatesTest(_RemintFixture):
    """From documentation version 3.0.0 on every Templates row is compressed against the shared dictionary,
    so a re-mint has to carry the templates across with the Content rows."""

    def remint(self, **kwargs):
        return remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, **kwargs)

    def test_schema_3_templates_are_recompressed_against_the_new_dictionary(self):
        self.add_versions_and_templates(majors=(2, 3))  # the newest row decides, as in the app
        before = self.snapshot()
        stats = self.remint(seed=999)

        new_dictionary = load_dictionary(self.conn)
        self.assertNotEqual(new_dictionary, self.first_dictionary)
        with DictionaryCompressor(new_dictionary) as compressor:
            for name, stored in self.stored_templates().items():
                try:
                    plain = compressor.decompress(stored)
                except RuntimeError:
                    self.fail(f"{name} does not decode with the new dictionary")
                self.assertEqual(plain, self.templates[name], f"{name} does not decode with the new dictionary")
        self.assertEqual(stats["templates"], len(TEMPLATE_NAMES))
        self.assertEqual(verify(before, self.db_path), [])

    def test_a_template_that_does_not_decode_refuses_before_training(self):
        self.add_versions_and_templates()
        self.conn.execute("UPDATE Templates SET content = ? WHERE name = 'page.peb'", (b"plain text, not brotli",))
        self.conn.commit()
        content = dict(self.conn.execute("SELECT path, content FROM Content"))
        templates = self.stored_templates()

        with mock.patch.object(remint_dictionary, "train_dictionary",
                               side_effect=AssertionError("trained despite a template the app cannot read")):
            with self.assertRaises(RuntimeError) as raised:
                self.remint(seed=999)

        self.assertIn("page.peb", str(raised.exception))
        self.assertEqual(load_dictionary(self.conn), self.first_dictionary, "dictionary was replaced anyway")
        self.assertEqual(dict(self.conn.execute("SELECT path, content FROM Content")), content)
        self.assertEqual(self.stored_templates(), templates)

    def test_templates_are_left_untouched_below_schema_3(self):
        """Plain templates in a database declaring less than 3, declaring nothing, or with no version table:
        the newest row decides, so 3 then 2 is a 2."""
        cases = {"declares 2": dict(majors=(2,)), "newest row declares 2": dict(majors=(3, 2)),
                 "no version row": dict(majors=()), "no version table": dict(majors=(), version_table=False)}
        for label, case in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.add_versions_and_templates(compressed=False, **case)
                before = self.snapshot()
                stats = self.remint(seed=999)
                self.assertEqual(self.stored_templates(), self.templates, "plain templates were rewritten")
                self.assertEqual(stats.get("templates", 0), 0)
                self.assertEqual(verify(before, self.db_path), [])

    def test_the_winner_is_chosen_on_content_and_templates_together(self):
        """The first candidate stores the Content rows smaller, the second stores everything smaller once
        the templates count: the second must be kept."""
        self.add_versions_and_templates()
        before = self.snapshot()
        words = " ".join(WORDS).encode("utf-8") * 40
        candidates = [words, words + b"".join(self.templates.values())]

        def stored(dictionary, texts):
            with DictionaryCompressor(dictionary) as compressor:
                return sum(len(compressor.compress(t)) for t in texts)
        content = [stored(d, self.originals.values()) for d in candidates]
        templates = [stored(d, self.templates.values()) for d in candidates]
        self.assertLess(content[0], content[1], "fixture premise: the first candidate stores Content smaller")
        self.assertLess(content[1] + templates[1], content[0] + templates[0],
                        "fixture premise: the second candidate stores everything smaller")

        with mock.patch.object(remint_dictionary, "train_dictionary", side_effect=list(candidates)):
            stats = self.remint(seed=999, seeds=2)

        self.assertEqual(load_dictionary(self.conn), candidates[1],
                         "kept the candidate that stores less Content but more in all")
        self.assertEqual(stats["candidate_parts"], [(999, content[0], templates[0]), (1000, content[1], templates[1])])
        self.assertEqual(verify(before, self.db_path), [])

    def remint_with_shifted_totals(self, content_shift: int, template_shift: int):
        """One real candidate, with its reported totals moved by the given bytes against what the dictionary in
        place stores (the rows and templates themselves stay real, so whatever is written round-trips)."""
        real_rows, real_templates = remint_dictionary.convert_all, remint_dictionary.convert_templates

        def rows(conn, base_rows, old, new, workers):
            converted, _total, problems = real_rows(conn, base_rows, old, new, workers)
            return converted, sum(c[2] for c in converted) + content_shift, problems

        def templates(templates, old, new, workers):
            converted, _total, problems = real_templates(templates, old, new, workers)
            return converted, sum(len(t[2]) for t in templates) + template_shift, problems

        with mock.patch.object(remint_dictionary, "convert_all", side_effect=rows), \
                mock.patch.object(remint_dictionary, "convert_templates", side_effect=templates):
            return self.remint(seed=999)

    def test_the_dictionary_in_place_is_weighed_with_its_templates(self):
        """A candidate that stores 10 bytes less Content and 5 more template bytes stores less in all, so it
        replaces the dictionary in place - which only holds if the in-place total counts the templates too, as
        each candidate's does."""
        self.add_versions_and_templates()
        before = self.snapshot()
        stats = self.remint_with_shifted_totals(content_shift=-10, template_shift=5)
        self.assertEqual(stats["kept"], "candidate", "kept the dictionary in place though a candidate stores less in all")
        self.assertEqual(stats["after"], stats["before"] - 5)
        self.assertNotEqual(load_dictionary(self.conn), self.first_dictionary)
        self.assertEqual(verify(before, self.db_path), [])

    def test_a_candidate_better_on_content_alone_is_not_kept(self):
        """10 bytes less Content but 15 more template bytes is 5 more in all: the dictionary in place stays."""
        self.add_versions_and_templates()
        templates = self.stored_templates()
        stats = self.remint_with_shifted_totals(content_shift=-10, template_shift=15)
        self.assertEqual(stats["kept"], "current")
        self.assertEqual(stats["best_candidate"][1], stats["before"] + 5)
        self.assertEqual(load_dictionary(self.conn), self.first_dictionary)
        self.assertEqual(self.stored_templates(), templates)

    def test_a_template_that_does_not_round_trip_writes_nothing(self):
        """The new dictionary's decoder is made to garble the templates (and only them): the run must stop
        with nothing written, as it does for a Content row that does not round-trip."""
        self.add_versions_and_templates()
        content = dict(self.conn.execute("SELECT path, content FROM Content"))
        templates = self.stored_templates()
        real = remint_dictionary._compressors

        class Garbling:
            def __init__(self, inner):
                self.inner = inner

            def compress(self, data):
                return self.inner.compress(data)

            def decompress(self, data):
                plain = self.inner.decompress(data)
                return b"garbled" if plain.startswith(b"{% block body %}") else plain

        def compressors(old_dictionary, new_dictionary):
            old, new = real(old_dictionary, new_dictionary)
            return old, Garbling(new)

        with mock.patch.object(remint_dictionary, "_compressors", side_effect=compressors):
            with self.assertRaises(RuntimeError):
                self.remint(seed=999)
        self.assertEqual(load_dictionary(self.conn), self.first_dictionary, "dictionary was replaced anyway")
        self.assertEqual(dict(self.conn.execute("SELECT path, content FROM Content")), content)
        self.assertEqual(self.stored_templates(), templates)

    def test_each_pass_removes_its_temporary_dictionaries_templates_included(self):
        """#44's check, on a database whose templates are compressed: the template pass's workers make compressors
        of their own, and they must be gone when the pass ends too."""
        self.add_versions_and_templates()
        private = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, private, True)
        with mock.patch.object(tempfile, "tempdir", str(private)):
            stats = self.remint(seed=999, seeds=3, max_workers=2)
            left = sorted(p.name for p in private.iterdir())
        self.assertEqual(stats["templates"], len(TEMPLATE_NAMES))
        self.assertEqual(left, [], "temp dictionaries left behind")

    def test_the_newest_version_row_decides_by_changetime_then_rowid(self):
        """As the app's DatabaseVersionResolver reads it: the greatest changeTime wins, and rowid breaks a tie. Each
        case is inserted so that the other orders (rowid alone, changeTime alone, the tie broken the other way)
        pick the other row."""
        jan, feb = "2026-01-01 00:00:00", "2026-02-01 00:00:00"
        cases = {
            "older changeTime, newer rowid: 3 then 2": ([(3, feb), (2, jan)], True),
            "older changeTime, newer rowid: 2 then 3": ([(2, feb), (3, jan)], False),
            "tie, the newer rowid declares 3": ([(2, feb), (3, feb)], True),
            "tie, the newer rowid declares 2": ([(3, feb), (2, feb)], False),
        }
        for label, (rows, compressed) in cases.items():
            with self.subTest(label):
                conn = sqlite3.connect(":memory:")
                conn.executescript(VERSION_SQL)
                conn.executemany("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, "
                                 "changeTime) VALUES (?, 0, 0, 'test', 'test', ?)", rows)
                self.assertEqual(remint_dictionary.templates_compressed(conn), compressed)
                conn.close()

    def test_a_newer_changetime_in_an_older_row_still_gets_its_templates_reminted(self):
        """The whole run, for the first case above: version 3 was written first but with the later changeTime.
        Read by rowid it would look like 2, and the compressed templates would be left behind, undecodable."""
        self.add_versions_and_templates(majors=())
        self.conn.executemany("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, "
                              "changeTime) VALUES (?, 0, 0, 'test', 'test', ?)",
                              [(3, "2026-02-01 00:00:00"), (2, "2026-01-01 00:00:00")])
        self.conn.commit()
        before = self.snapshot()
        stats = self.remint(seed=999)
        self.assertEqual(stats["templates"], len(TEMPLATE_NAMES))
        with DictionaryCompressor(load_dictionary(self.conn)) as compressor:
            for name, stored in self.stored_templates().items():
                self.assertEqual(compressor.decompress(stored), self.templates[name], name)
        self.assertEqual(verify(before, self.db_path), [])

    def test_the_templates_share_of_the_sample_follows_their_stored_bytes(self):
        """Templates are one doc set, weighted by their stored bytes like the others: heavy templates are all drawn,
        light ones only in proportion to their few bytes. Weighting them by anything else - nothing, or one per
        template - draws a different number."""
        for label, size in {"heavy": 6000, "light": 8}.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.add_versions_and_templates()
                with DictionaryCompressor(self.first_dictionary) as compressor:
                    for i in range(27):
                        text = make_template(100 + i, size)
                        self.templates[f"extra{i}.peb"] = text
                        self.conn.execute("INSERT INTO Templates (name, content) VALUES (?, ?)",
                                          (f"extra{i}.peb", compressor.compress(text)))
                    for name in TEMPLATE_NAMES:  # the three of add_versions_and_templates, at this size too
                        text = make_template(TEMPLATE_NAMES.index(name), size)
                        self.templates[name] = text
                        self.conn.execute("UPDATE Templates SET content = ? WHERE name = ?",
                                          (compressor.compress(text), name))
                self.conn.commit()

                # collect_training_samples' rule, at this fixture's sizes, where neither the 3x sample cap nor the
                # byte budget binds: a set draws up to 3 x max(1, round(sample_size x its share of stored bytes)).
                content_bytes = sum(row[4] for row in load_base_rows(self.conn))
                template_bytes = self.conn.execute("SELECT SUM(LENGTH(content)) FROM Templates").fetchone()[0]
                quota = max(1, round(30 * template_bytes / (content_bytes + template_bytes)))
                expected = min(len(self.templates), 3 * quota)
                if label == "heavy":
                    self.assertGreater(expected, 3, "fixture premise: heavy templates draw more than the floor")
                else:
                    self.assertLess(expected, len(self.templates), "fixture premise: light templates draw only some")

                real, seen = remint_dictionary.train_dictionary, []

                def train(samples, dict_size):
                    seen.extend(samples)
                    return real(samples, dict_size)

                with mock.patch.object(remint_dictionary, "train_dictionary", side_effect=train):
                    self.remint(seed=999)
                drawn = sum(1 for sample in seen if sample in set(self.templates.values()))
                self.assertEqual(drawn, expected)

    def test_a_candidate_whose_content_alone_beats_the_best_total_is_not_kept(self):
        """The first candidate stores Content and templates smallest in all; the second stores more in all, though
        its Content alone is less than the first's total. Weighing a candidate's Content against the best's total
        would keep the second."""
        self.add_versions_and_templates()
        before = self.snapshot()
        words = " ".join(WORDS).encode("utf-8") * 40
        candidates = [words + b"".join(self.templates.values()), words]

        def stored(dictionary, texts):
            with DictionaryCompressor(dictionary) as compressor:
                return sum(len(compressor.compress(t)) for t in texts)
        content = [stored(d, self.originals.values()) for d in candidates]
        templates = [stored(d, self.templates.values()) for d in candidates]
        self.assertLess(content[1], content[0] + templates[0],
                        "fixture premise: the second candidate's Content is less than the first's total")
        self.assertLess(content[0] + templates[0], content[1] + templates[1],
                        "fixture premise: the first candidate stores less in all")

        with mock.patch.object(remint_dictionary, "train_dictionary", side_effect=list(candidates)):
            stats = self.remint(seed=999, seeds=2)

        self.assertEqual(stats["kept"], "candidate")
        self.assertEqual(load_dictionary(self.conn), candidates[0], "kept the candidate that stores more in all")
        self.assertEqual(stats["after"], content[0] + templates[0])
        self.assertEqual(verify(before, self.db_path), [])

    def test_templates_are_in_the_training_sample(self):
        self.add_versions_and_templates()
        real = remint_dictionary.train_dictionary
        seen = []

        def train(samples, dict_size):
            seen.extend(samples)
            return real(samples, dict_size)

        with mock.patch.object(remint_dictionary, "train_dictionary", side_effect=train):
            self.remint(seed=999)
        for name, text in self.templates.items():
            self.assertIn(text, seen, f"{name} was not sampled")

    def test_a_content_path_in_the_templates_doc_set_refuses(self):
        self.add_versions_and_templates()
        with DictionaryCompressor(self.first_dictionary) as compressor:
            self.conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                              "VALUES (?, 1, ?, ?, 0)",
                              ("(templates)/page.peb", compressor.compress(b"a page"), self.html_type_id))
        self.conn.commit()
        with self.assertRaises(RuntimeError) as raised:
            self.remint(seed=999)
        self.assertIn("(templates)/page.peb", str(raised.exception))
        self.assertEqual(load_dictionary(self.conn), self.first_dictionary)

    def test_verifier_catches_a_changed_missing_stale_or_extra_template(self):
        """A template rewritten, dropped, added, or left compressed against the old dictionary -- the silent
        case, since a wrong dictionary can decode into different bytes without an error."""
        self.add_versions_and_templates()
        before = self.snapshot()
        self.remint(seed=4242)
        old_page = dict(sqlite3.connect(before).execute("SELECT name, content FROM Templates"))["page.peb"]
        with DictionaryCompressor(load_dictionary(self.conn)) as compressor:
            changed = compressor.compress(b"{% block body %}something else{% endblock %}")
        self.conn.execute("UPDATE Templates SET content = ? WHERE name = 'layout.pebble'", (changed,))
        self.conn.execute("UPDATE Templates SET content = ? WHERE name = 'page.peb'", (old_page,))
        self.conn.execute("DELETE FROM Templates WHERE name = 'nav.peb'")
        self.conn.execute("INSERT INTO Templates (name, content) VALUES ('extra.peb', ?)", (changed,))
        self.conn.commit()

        problems = dict(verify(before, self.db_path))
        self.assertEqual(sorted(problems), ["template extra.peb", "template layout.pebble", "template nav.peb",
                                            "template page.peb"])


class ContentBlocksRefusalTest(_RemintFixture):
    """From documentation version 4.0.0 on (ADFA-6683) most pages are in ContentBlocks, compressed against the
    dictionary, which neither the re-mint nor its verifier handles: both refuse a database declaring major 4 or later,
    as the app reads its version, or holding the blocks step's tables whatever it declares, write nothing, and say the
    re-mint goes before the blocks step."""

    def remint(self, **kwargs):
        return remint(self.conn, sample_size=30, dict_size=16384, training_bytes=1 << 20, seed=999, **kwargs)

    def assertNamesTheBlocksStepAndTheOrder(self, message: str):
        self.assertIn("Re-mint before scripts/store_pages_in_blocks.py", message)
        self.assertIn("verify_remint_dictionary.py, scripts/store_pages_in_blocks.py, then "
                      "scripts/rebuild_database.py --pack Templates,Content,TooltipTexts,ContentBlocks --page-size 4096",
                      message)

    def test_the_remint_refuses_major_4_or_later_before_training(self):
        for majors in ((3, 4), (5,)):
            with self.subTest(majors=majors):
                self.tearDown()
                self.setUp()
                self.add_versions_and_templates(majors=majors)
                content, templates = dict(self.conn.execute("SELECT path, content FROM Content")), \
                    self.stored_templates()
                with mock.patch.object(remint_dictionary, "train_dictionary",
                                       side_effect=AssertionError("trained on a database with blocks")):
                    with self.assertRaises(RuntimeError) as raised:
                        self.remint()
                message = str(raised.exception)
                self.assertIn(f"declares documentation major {majors[-1]}", message)
                self.assertIn("nothing written", message)
                self.assertNamesTheBlocksStepAndTheOrder(message)
                self.assertEqual(load_dictionary(self.conn), self.first_dictionary, "dictionary was replaced anyway")
                self.assertEqual(dict(self.conn.execute("SELECT path, content FROM Content")), content)
                self.assertEqual(self.stored_templates(), templates)

    def test_the_newest_version_row_decides(self):
        """An older row declaring 4 under a newer one declaring 3 is a 3.x database, as the app reads it: re-minted,
        templates and all, and verified."""
        self.add_versions_and_templates(majors=(4, 3))
        before = self.snapshot()
        stats = self.remint()
        self.assertEqual(stats["kept"], "candidate")
        self.assertEqual(stats["templates"], len(TEMPLATE_NAMES))
        self.assertEqual(verify(before, self.db_path), [])

    def test_the_verifier_refuses_either_database_declaring_major_4(self):
        self.add_versions_and_templates(majors=(3,))
        original = self.snapshot()
        self.conn.execute("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                          "VALUES (4, 0, 0, 'ADFA-6683', 'blocks', '2026-10-26 00:00:00')")
        self.conn.commit()
        for old, new in ((original, self.db_path), (self.db_path, original)):
            with self.subTest(old=old.name, new=new.name):
                with self.assertRaises(RuntimeError) as raised:
                    verify(old, new)
                message = str(raised.exception)
                self.assertTrue(message.startswith(f"{self.db_path}: the database declares documentation major 4"),
                                message)
                self.assertIn("This check compares Content rows and templates, not blocks", message)
                self.assertNamesTheBlocksStepAndTheOrder(message)

    def add_block_tables(self, tables=BLOCK_TABLES):
        self.conn.executescript("".join(BLOCK_TABLES[name] + ";" for name in tables))
        self.conn.commit()

    def test_the_remint_refuses_block_tables_whatever_the_database_declares(self):
        """A version row rewritten to 3 after the blocks step, or dropped, leaves the pages in blocks all the same."""
        for majors, tables, declares in (((3,), tuple(BLOCK_TABLES), "documentation major 3"),
                                         ((3,), ("ContentBlockItems",), "documentation major 3"),
                                         (None, ("ContentBlocks",), "no documentation version")):
            with self.subTest(majors=majors, tables=tables):
                self.tearDown()
                self.setUp()
                if majors is not None:
                    self.add_versions_and_templates(majors=majors)
                self.add_block_tables(tables)
                content = dict(self.conn.execute("SELECT path, content FROM Content"))
                templates = self.stored_templates() if majors is not None else None
                with mock.patch.object(remint_dictionary, "train_dictionary",
                                       side_effect=AssertionError("trained on a database with blocks")):
                    with self.assertRaises(RuntimeError) as raised:
                        self.remint()
                message = str(raised.exception)
                self.assertTrue(message.startswith(f"the database has {' and '.join(tables)}, the tables "
                                                   "scripts/store_pages_in_blocks.py (ADFA-6683) adds, though it "
                                                   f"declares {declares}"), message)
                self.assertIn("nothing written", message)
                self.assertNamesTheBlocksStepAndTheOrder(message)
                self.assertEqual(load_dictionary(self.conn), self.first_dictionary, "dictionary was replaced anyway")
                self.assertEqual(dict(self.conn.execute("SELECT path, content FROM Content")), content)
                if templates is not None:
                    self.assertEqual(self.stored_templates(), templates)

    def test_the_verifier_refuses_either_database_with_block_tables(self):
        self.add_versions_and_templates(majors=(3,))
        original = self.snapshot()
        self.add_block_tables(("ContentBlocks",))
        for old, new in ((original, self.db_path), (self.db_path, original)):
            with self.subTest(old=old.name, new=new.name):
                with self.assertRaises(RuntimeError) as raised:
                    verify(old, new)
                message = str(raised.exception)
                self.assertTrue(message.startswith(f"{self.db_path}: the database has ContentBlocks"), message)
                self.assertIn("This check compares Content rows and templates, not blocks", message)
                self.assertNamesTheBlocksStepAndTheOrder(message)

    def test_the_verifier_closes_both_databases_when_it_refuses(self):
        """Otherwise each refused pair leaves two read-only handles open until they are collected, and on Windows
        neither file can be deleted meanwhile."""
        self.add_versions_and_templates(majors=(3,))
        original = self.snapshot()
        self.add_block_tables(("ContentBlocks",))
        opened = []
        connect = sqlite3.connect

        def recording(*args, **kwargs):
            opened.append(connect(*args, **kwargs))
            return opened[-1]

        for old, new in ((original, self.db_path), (self.db_path, original)):
            with self.subTest(old=old.name, new=new.name):
                opened.clear()
                with mock.patch.object(sqlite3, "connect", recording), self.assertRaises(RuntimeError):
                    verify(old, new)
                self.assertEqual(len(opened), 2)
                for conn in opened:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        conn.execute("SELECT 1")


class CommandLineRefusalTest(_RemintFixture):
    """A run that refuses writes nothing at all, the backup included, and says why on one "error:" line rather
    than in a traceback."""

    def setUp(self):
        super().setUp()
        # Its own directory, so a backup written beside the database would show.
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conn.close()
        moved = self.dir / "documentation.db"
        shutil.move(self.db_path, moved)
        self.db_path = moved
        self.conn = sqlite3.connect(self.db_path)

    def sha(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess:
        self.conn.commit()
        return subprocess.run([sys.executable, str(HERE / script), *args], capture_output=True, text=True,
                              cwd=HERE, check=False)

    def run_remint(self) -> subprocess.CompletedProcess:
        """The re-mint as a user runs it, backup on, at the fixture's sizes."""
        return self.run_script("remint_dictionary.py", str(self.db_path), "--sample-size", "30", "--dict-size",
                               "16384", "--training-bytes", str(1 << 20), "--seeds", "1")

    def assertRefusedWithNothingWritten(self, result, sha: str, says: str):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        last = result.stderr.rstrip("\n").splitlines()[-1]
        self.assertTrue(last.startswith("error: ") and says in last, result.stderr)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["documentation.db"], "a backup was written")
        self.assertEqual(self.sha(), sha)

    def test_a_template_that_does_not_decode_refuses_with_no_backup(self):
        self.add_versions_and_templates()
        self.conn.execute("UPDATE Templates SET content = ? WHERE name = 'page.peb'", (b"plain text, not brotli",))
        self.conn.commit()
        sha = self.sha()
        self.assertRefusedWithNothingWritten(self.run_remint(), sha, "page.peb")

    def test_a_content_path_in_the_templates_doc_set_refuses_with_no_backup(self):
        self.add_versions_and_templates()
        with DictionaryCompressor(self.first_dictionary) as compressor:
            self.conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                              "VALUES (?, 1, ?, ?, 0)",
                              ("(templates)/page.peb", compressor.compress(b"a page"), self.html_type_id))
        self.conn.commit()
        sha = self.sha()
        self.assertRefusedWithNothingWritten(self.run_remint(), sha, "(templates)/page.peb")

    def test_a_database_with_blocks_refuses_with_no_backup(self):
        self.add_versions_and_templates(majors=(3, 4))
        sha = self.sha()
        self.assertRefusedWithNothingWritten(self.run_remint(), sha, "Re-mint before scripts/store_pages_in_blocks.py")

    def test_a_3_x_database_with_block_tables_refuses_with_no_backup(self):
        self.add_versions_and_templates(majors=(3,))
        self.conn.executescript("".join(sql + ";" for sql in BLOCK_TABLES.values()))
        sha = self.sha()
        self.assertRefusedWithNothingWritten(self.run_remint(), sha, "the database has ContentBlocks and "
                                            "ContentBlockItems")

    def test_the_verifier_reports_a_database_with_blocks_on_an_error_line(self):
        self.add_versions_and_templates(majors=(3, 4))
        original = self.dir / "original.db"
        shutil.copy2(self.db_path, original)
        result = self.run_script("verify_remint_dictionary.py", str(original), str(self.db_path))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.startswith(f"error: {original}: the database declares documentation major 4"),
                        result.stderr)
        self.assertIn("store_pages_in_blocks.py", result.stderr)
        self.assertNotIn("Byte-identical", result.stdout)

    def test_no_dictionary_refuses_with_no_backup(self):
        self.conn.execute("DELETE FROM CompressionDictionary")
        self.conn.commit()
        sha = self.sha()
        self.assertRefusedWithNothingWritten(self.run_remint(), sha, "no CompressionDictionary")

    def test_a_row_that_fails_to_convert_refuses_with_no_backup(self):
        """Found only by a full recompression pass, after training: the backup still waits for the write."""
        self.conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, 1, ?, ?, 0)",
            ("a/broken.html", b"not brotli at all", self.html_type_id))
        self.conn.commit()
        sha = self.sha()
        result = self.run_remint()
        self.assertRefusedWithNothingWritten(result, sha, "failed to convert")
        self.assertIn("error: a/broken.html:", result.stderr)

    def test_keeping_the_dictionary_in_place_writes_no_backup(self):
        """The migration's own sample and size retrain the stored dictionary, a tie: nothing is written."""
        sha = self.sha()
        result = self.run_script("remint_dictionary.py", str(self.db_path), "--sample-size", "30", "--dict-size",
                                 str(MIGRATE_DICT_SIZE), "--training-bytes", str(1 << 20), "--seeds", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kept the current dictionary", result.stdout)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["documentation.db"], "a backup was written")
        self.assertEqual(self.sha(), sha)

    def test_a_remint_that_writes_backs_up_first(self):
        """The other side of the rule: a run that writes leaves a backup holding the database as it was."""
        sha = self.sha()
        result = self.run_script("remint_dictionary.py", str(self.db_path), "--sample-size", "30", "--dict-size",
                                 "16384", "--training-bytes", str(1 << 20), "--seeds", "1", "--sample-seed", "999")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = [p for p in self.dir.iterdir() if p.name.startswith("documentation.db.backup-")]
        self.assertEqual(len(backups), 1, result.stderr)
        self.assertNotEqual(self.sha(), sha)
        self.assertEqual(verify(backups[0], self.db_path), [])

    def test_the_verifier_reports_a_database_with_no_dictionary_on_an_error_line(self):
        original = self.dir / "original.db"
        shutil.copy2(self.db_path, original)
        cases = {"no row": ("DELETE FROM CompressionDictionary", "no CompressionDictionary row"),
                 "no table": ("DROP TABLE CompressionDictionary", "no CompressionDictionary table")}
        for label, (sql, says) in cases.items():
            with self.subTest(label):
                self.conn.execute(sql)
                self.conn.commit()
                result = self.run_script("verify_remint_dictionary.py", str(original), str(self.db_path))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertTrue(result.stderr.startswith("error: ") and says in result.stderr, result.stderr)
                self.assertNotIn("Byte-identical", result.stdout)


if __name__ == "__main__":
    unittest.main()
