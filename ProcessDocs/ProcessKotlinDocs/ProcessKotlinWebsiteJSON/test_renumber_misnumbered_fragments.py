#!/usr/bin/env python3
"""Tests for renumber_misnumbered_fragments.py (ADFA-5171).

Run directly: python3 test_renumber_misnumbered_fragments.py
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from populate_db import CHUNK_SIZE
from renumber_misnumbered_fragments import repair

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


def chunk_bytes(n: int, fill: bytes) -> bytes:
    return (fill * (n // len(fill) + 1))[:n]


class RenumberMisnumberedFragmentsTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        Path(path).unlink(missing_ok=True)
        self.db_path = Path(path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute("INSERT INTO Languages (value) VALUES ('en-US')")
        self.conn.execute("INSERT INTO ContentTypes (value, compression) VALUES ('image/gif', 'none')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def insert(self, path: str, content: bytes):
        self.conn.execute(
            "INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)",
            (path, content),
        )

    def all_paths(self) -> set:
        return {row[0] for row in self.conn.execute("SELECT path FROM Content")}

    def content_at(self, path: str) -> bytes:
        return self.conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()[0]

    def test_renumbers_chain_starting_at_minus_2(self):
        base = "a/devsite/media/size-range.gif"
        self.insert(base, chunk_bytes(CHUNK_SIZE, b"A"))
        self.insert(f"{base}-2", chunk_bytes(CHUNK_SIZE, b"B"))
        self.insert(f"{base}-3", chunk_bytes(CHUNK_SIZE, b"C"))
        self.insert(f"{base}-4", chunk_bytes(CHUNK_SIZE, b"D"))
        self.insert(f"{base}-5", b"E" * 100)
        self.conn.commit()

        stats = repair(self.conn)
        self.conn.commit()

        self.assertEqual(stats["chains_renumbered"], 1)
        self.assertEqual(stats["fragments_moved"], 4)
        self.assertEqual(stats["chains_gapped"], 0)
        self.assertEqual(
            self.all_paths(),
            {base, f"{base}-1", f"{base}-2", f"{base}-3", f"{base}-4"},
        )
        self.assertEqual(self.content_at(f"{base}-1"), chunk_bytes(CHUNK_SIZE, b"B"))
        self.assertEqual(self.content_at(f"{base}-2"), chunk_bytes(CHUNK_SIZE, b"C"))
        self.assertEqual(self.content_at(f"{base}-3"), chunk_bytes(CHUNK_SIZE, b"D"))
        self.assertEqual(self.content_at(f"{base}-4"), b"E" * 100)

    def test_single_orphaned_continuation(self):
        base = "j/html/api/index-all.html"
        self.insert(base, chunk_bytes(CHUNK_SIZE, b"A"))
        self.insert(f"{base}-2", b"tail" * 10)
        self.conn.commit()

        stats = repair(self.conn)
        self.conn.commit()

        self.assertEqual(stats["chains_renumbered"], 1)
        self.assertEqual(stats["fragments_moved"], 1)
        self.assertEqual(self.all_paths(), {base, f"{base}-1"})
        self.assertEqual(self.content_at(f"{base}-1"), b"tail" * 10)

    def test_correctly_numbered_chain_untouched(self):
        base = "k/html/already-fine.html"
        self.insert(base, chunk_bytes(CHUNK_SIZE, b"A"))
        self.insert(f"{base}-1", chunk_bytes(CHUNK_SIZE, b"B"))
        self.insert(f"{base}-2", b"tail")
        self.conn.commit()

        stats = repair(self.conn)
        self.conn.commit()

        self.assertEqual(stats["chains_renumbered"], 0)
        self.assertEqual(stats["fragments_moved"], 0)
        self.assertEqual(self.all_paths(), {base, f"{base}-1", f"{base}-2"})

    def test_idempotent_second_run(self):
        base = "a/devsite/media/size-range.gif"
        self.insert(base, chunk_bytes(CHUNK_SIZE, b"A"))
        self.insert(f"{base}-2", b"tail")
        self.conn.commit()

        repair(self.conn)
        self.conn.commit()
        stats = repair(self.conn)
        self.conn.commit()

        self.assertEqual(stats["chains_renumbered"], 0)
        self.assertEqual(stats["fragments_moved"], 0)

    def test_exact_size_file_with_no_continuation_left_alone(self):
        path = "k/html/exactly-one-mb.bin"
        self.insert(path, chunk_bytes(CHUNK_SIZE, b"A"))
        self.conn.commit()

        stats = repair(self.conn)
        self.conn.commit()

        self.assertEqual(stats["chains_renumbered"], 0)
        self.assertEqual(stats["chains_gapped"], 0)
        self.assertEqual(self.all_paths(), {path})

    def test_chain_with_real_gap_reported_and_left_untouched(self):
        base = "k/html/actually-missing-a-chunk.html"
        self.insert(base, chunk_bytes(CHUNK_SIZE, b"A"))
        self.insert(f"{base}-2", chunk_bytes(CHUNK_SIZE, b"B"))
        self.insert(f"{base}-4", b"tail")  # -3 is genuinely missing
        self.conn.commit()

        stats = repair(self.conn)
        self.conn.commit()

        self.assertEqual(stats["chains_renumbered"], 0)
        self.assertEqual(stats["chains_gapped"], 1)
        self.assertEqual(self.all_paths(), {base, f"{base}-2", f"{base}-4"})


if __name__ == "__main__":
    unittest.main()
