#!/usr/bin/env python3
"""Tests for populate_db.py's vacuum_and_pin_page_size (ADFA-5141).

Run directly: python3 test_vacuum_and_pin_page_size.py
"""
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_preserves_file_permissions(self):
        # VACUUM INTO rewrites through a tempfile.mkstemp() temp file, which
        # is always created mode 0600 regardless of the original's mode or
        # the process umask - confirmed via real-world QA (on the mirrored
        # docdb-studio.py fix) to silently drop a 644 documentation.db to 600
        # on every vacuum if not restored after the os.replace swap.
        db = _make_db(starting_page_size=1024)
        try:
            os.chmod(db, 0o644)
            vacuum_and_pin_page_size(db)
            mode = stat.S_IMODE(db.stat().st_mode)
            self.assertEqual(mode, 0o644)
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

    def test_succeeds_with_other_connections_still_open(self):
        # An earlier version of this function (and docdb-studio.py's
        # vacuum_database(), which it mirrored) did an in-place VACUUM +
        # journal_mode round-trip, which requires exclusive access: SQLite
        # refuses to switch a WAL-mode db away from WAL while ANY other
        # connection has it open -- even one from a function that has
        # already returned, since `with sqlite3.connect(...) as conn:` does
        # not close conn on exit. VACUUM INTO only needs a read snapshot of
        # the source, so this must succeed even with an unrelated open
        # connection (e.g. something else in the pipeline reading the db)
        # and an unclosed caller-style connection both still around.
        db = _make_db(starting_page_size=1024)
        try:
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA journal_mode=WAL")

            reader_conn = sqlite3.connect(db)
            reader_conn.execute("SELECT * FROM t")

            writer_conn = sqlite3.connect(db)
            writer_conn.execute("INSERT INTO t (data) VALUES (?)", (b"y" * 100,))
            writer_conn.commit()

            try:
                vacuum_and_pin_page_size(db)  # must not raise "database is locked"
            finally:
                reader_conn.close()
                writer_conn.close()

            with sqlite3.connect(db) as conn:
                (page_size,) = conn.execute("PRAGMA page_size").fetchone()
                (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
                (count,) = conn.execute("SELECT count(*) FROM t").fetchone()
            self.assertEqual(page_size, SQLITE_PAGE_SIZE_BYTES)
            self.assertEqual(journal_mode.lower(), "wal")
            self.assertEqual(count, 2)
        finally:
            db.unlink(missing_ok=True)

    def test_handles_quote_in_parent_dir_name(self):
        # An earlier version built "VACUUM INTO '{tmp_path}'" via an
        # f-string; a single quote anywhere in db_path's parent directory
        # (e.g. a real user directory like "David's Docs") broke that
        # statement outright. Now a bound parameter, which needs no escaping.
        tmp_dir = tempfile.mkdtemp()
        quote_dir = Path(tmp_dir) / "David's Docs"
        quote_dir.mkdir()
        db = quote_dir / "test.db"
        try:
            with sqlite3.connect(db) as conn:
                conn.executescript(
                    "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t (v) VALUES ('x');"
                )
                conn.commit()
            vacuum_and_pin_page_size(db)  # must not raise
            with sqlite3.connect(db) as conn:
                (count,) = conn.execute("SELECT count(*) FROM t").fetchone()
            self.assertEqual(count, 1)
        finally:
            db.unlink(missing_ok=True)
            quote_dir.rmdir()
            os.rmdir(tmp_dir)

    def test_leaves_original_untouched_if_vacuum_into_fails(self):
        db = _make_db(starting_page_size=1024)
        try:
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
            size_before = db.stat().st_size

            real_connect = sqlite3.connect

            class _FailOnVacuumIntoConn:
                def __init__(self, real):
                    self._real = real

                def execute(self, sql, *args, **kwargs):
                    if sql.strip().upper().startswith("VACUUM INTO"):
                        raise sqlite3.OperationalError("simulated vacuum-into failure")
                    return self._real.execute(sql, *args, **kwargs)

                def __enter__(self):
                    self._real.__enter__()
                    return self

                def __exit__(self, *exc_info):
                    return self._real.__exit__(*exc_info)

                def __getattr__(self, name):
                    return getattr(self._real, name)

            with mock.patch("populate_db.sqlite3.connect") as mock_connect:
                mock_connect.side_effect = lambda *a, **k: _FailOnVacuumIntoConn(
                    real_connect(*a, **k)
                )
                with self.assertRaisesRegex(
                    sqlite3.OperationalError, "simulated vacuum-into failure"
                ):
                    vacuum_and_pin_page_size(db)

            self.assertEqual(db.stat().st_size, size_before)
            with sqlite3.connect(db) as conn:
                (page_size,) = conn.execute("PRAGMA page_size").fetchone()
                (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual(page_size, 1024)
            self.assertEqual(journal_mode.lower(), "wal")
            leftover_tmp = list(db.parent.glob(f"{db.name}*.vacuum.tmp"))
            self.assertEqual(leftover_tmp, [])
        finally:
            db.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
