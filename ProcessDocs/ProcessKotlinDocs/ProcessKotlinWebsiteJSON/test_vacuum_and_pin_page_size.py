#!/usr/bin/env python3
"""Tests for populate_db.py's vacuum_and_pin_page_size (ADFA-5141).

Run directly: python3 test_vacuum_and_pin_page_size.py
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from populate_db import SQLITE_PAGE_SIZE_BYTES, vacuum_and_pin_page_size


def _make_db(starting_page_size: int) -> Path:
    """Minimal temp DB pinned to starting_page_size before any table is
    created - page_size only takes effect on an empty database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.execute(f"PRAGMA page_size={starting_page_size}")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data BLOB)")
        conn.execute("INSERT INTO t (data) VALUES (?)", (b"x" * 4096,))
        conn.commit()
    return p


class VacuumAndPinPageSizeTest(unittest.TestCase):
    def test_migrates_real_starting_page_size(self):
        # Real production DBs start at page_size=1024 (ADFA-5141); exercise
        # that actual 1024 -> 2048 growth, not just an already-larger default.
        db = _make_db(starting_page_size=1024)
        try:
            with sqlite3.connect(db) as conn:
                (before,) = conn.execute("PRAGMA page_size").fetchone()
            self.assertEqual(before, 1024)
            vacuum_and_pin_page_size(db)
            with sqlite3.connect(db) as conn:
                (after,) = conn.execute("PRAGMA page_size").fetchone()
            self.assertEqual(after, SQLITE_PAGE_SIZE_BYTES)
        finally:
            db.unlink(missing_ok=True)

    def test_migrates_under_wal_journal_mode(self):
        # PRAGMA page_size silently fails to take effect on VACUUM under WAL
        # journal mode; vacuum_and_pin_page_size must work around it.
        db = _make_db(starting_page_size=1024)
        try:
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
            vacuum_and_pin_page_size(db)
            with sqlite3.connect(db) as conn:
                (page_size,) = conn.execute("PRAGMA page_size").fetchone()
                (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual(page_size, SQLITE_PAGE_SIZE_BYTES)
            self.assertEqual(journal_mode.lower(), "wal")
        finally:
            db.unlink(missing_ok=True)

    def test_preserves_schema_and_data(self):
        db = _make_db(starting_page_size=1024)
        try:
            vacuum_and_pin_page_size(db)
            with sqlite3.connect(db) as conn:
                rows = conn.execute("SELECT id, data FROM t").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], b"x" * 4096)
        finally:
            db.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
