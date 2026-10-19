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

from migrate_content_to_dictionary_brotli import migrate
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
        self.conn.execute("DELETE FROM CompressionDictionary")
        self.conn.commit()
        result = self.run_script("verify_remint_dictionary.py", str(original), str(self.db_path))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.startswith("error: ") and "no CompressionDictionary row" in result.stderr,
                        result.stderr)
        self.assertNotIn("Byte-identical", result.stdout)


if __name__ == "__main__":
    unittest.main()
