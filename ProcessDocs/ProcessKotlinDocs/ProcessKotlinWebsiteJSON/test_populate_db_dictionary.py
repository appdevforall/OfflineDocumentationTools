#!/usr/bin/env python3
"""Tests for populate_db.py's shared-dictionary Brotli compression (ADFA-5153).

Run directly: python3 test_populate_db_dictionary.py
"""
import random
import sqlite3
import unittest

from populate_db import DictionaryCompressor, load_dictionary, load_or_create_dictionary, train_dictionary

WORDS = [
    "kotlin", "class", "fun", "val", "var", "override", "interface", "object", "companion",
    "sidebar", "nav", "template", "docs-sidebar", "toc-element", "page.peb", "Content-Type",
]


def make_samples(count: int, seed: int = 1) -> list:
    rng = random.Random(seed)
    return [
        (" ".join(rng.choice(WORDS) for _ in range(150))).encode("utf-8")
        for _ in range(count)
    ]


class TrainDictionaryTest(unittest.TestCase):
    def test_produces_nonempty_dictionary(self):
        dictionary_data = train_dictionary(make_samples(120))
        self.assertGreater(len(dictionary_data), 0)


class DictionaryCompressorTest(unittest.TestCase):
    def setUp(self):
        self.dictionary_data = train_dictionary(make_samples(120))

    def test_round_trip(self):
        payload = make_samples(1)[0]
        with DictionaryCompressor(self.dictionary_data) as compressor:
            compressed = compressor.compress(payload)
            self.assertNotEqual(compressed, payload)
            self.assertEqual(compressor.decompress(compressed), payload)

    def test_compresses_smaller_than_plain_brotli_for_repetitive_corpus(self):
        # The whole point of a shared dictionary: content similar to the
        # training samples should compress smaller with the dictionary than
        # without one.
        import brotli
        payload = make_samples(1)[0]
        with DictionaryCompressor(self.dictionary_data) as compressor:
            with_dict = compressor.compress(payload)
        without_dict = brotli.compress(payload)
        self.assertLess(len(with_dict), len(without_dict))

    def test_wrong_dictionary_never_returns_the_original_bytes(self):
        # A mismatched dictionary is not guaranteed to fail loudly. Measured on
        # the real corpus (perturbing one 16 KiB region of the real dictionary,
        # decoding real rows): 50% raised, 38% decoded with no error into
        # *different* bytes, 12% decoded identically because the perturbed
        # region was never referenced.
        #
        # So asserting that the decode does not raise would be asserting a coin
        # flip, brittle across brotli versions and payloads. The invariant that
        # actually holds is the one that matters: a wrong dictionary never
        # yields the original bytes *and* reports success. That is why
        # load_or_create_dictionary must never retrain over a stored dictionary
        # - no runtime check can catch the mismatch afterwards.
        other_dictionary_data = train_dictionary(make_samples(120, seed=99))
        payload = make_samples(1)[0]
        with DictionaryCompressor(self.dictionary_data) as compressor:
            compressed = compressor.compress(payload)
        with DictionaryCompressor(other_dictionary_data) as wrong_compressor:
            try:
                result = wrong_compressor.decompress(compressed)
            except RuntimeError:
                return  # failed loudly, which is the other acceptable outcome
        self.assertNotEqual(result, payload, "a wrong dictionary must not appear to succeed")

    def test_no_dictionary_stream_fails_to_decode_with_dictionary_attached(self):
        import brotli
        payload = make_samples(1)[0]
        plain_compressed = brotli.compress(payload)
        with DictionaryCompressor(self.dictionary_data) as compressor:
            with self.assertRaises(RuntimeError):
                compressor.decompress(plain_compressed)


class LoadOrCreateDictionaryTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_first_call_trains_and_stores(self):
        dictionary_data = load_or_create_dictionary(self.conn, make_samples(120))
        self.assertGreater(len(dictionary_data), 0)
        row = self.conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
        self.assertEqual(row[0], dictionary_data)

    def test_second_call_reuses_stored_dictionary_without_retraining(self):
        first = load_or_create_dictionary(self.conn, make_samples(120, seed=1))
        second = load_or_create_dictionary(self.conn, make_samples(120, seed=2))
        self.assertEqual(first, second)

    def test_content_survives_a_reused_dictionary_across_separate_connections(self):
        # Mirrors the real cross-repo split: populate_db.py trains/stores the
        # dictionary once; a later run (or a different process entirely,
        # like WebServer.kt) must be able to decode against the same bytes
        # loaded back from the database.
        dictionary_data = load_or_create_dictionary(self.conn, make_samples(120))
        payload = make_samples(1)[0]
        with DictionaryCompressor(dictionary_data) as compressor:
            compressed = compressor.compress(payload)

        reloaded = load_dictionary(self.conn)
        self.assertEqual(reloaded, dictionary_data)
        with DictionaryCompressor(reloaded) as compressor:
            self.assertEqual(compressor.decompress(compressed), payload)


class LoadDictionaryTest(unittest.TestCase):
    def test_raises_when_missing(self):
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(RuntimeError):
                load_dictionary(conn)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
