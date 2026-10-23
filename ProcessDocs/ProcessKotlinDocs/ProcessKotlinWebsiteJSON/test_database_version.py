#!/usr/bin/env python3
"""Tests for populate_db's database-version declaration (ADFA-5220).

Run directly: python3 test_database_version.py
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from populate_db import (
    DATABASE_FORMAT_VERSION,
    VERSION_TABLE_SQL,
    declare_database_version,
)


class DeclareDatabaseVersionTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        Path(path).unlink(missing_ok=True)
        self.db_path = Path(path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.db_path.unlink(missing_ok=True)

    def declared(self) -> list:
        return self.conn.execute(
            "SELECT major, minor, patch, who FROM DocumentationDatabaseVersion ORDER BY rowid"
        ).fetchall()

    def test_creates_the_table_and_declares_the_version(self):
        """A database built before the table exists gets one, populated."""
        self.assertTrue(declare_database_version(self.conn))
        major, minor, patch = DATABASE_FORMAT_VERSION
        self.assertEqual(self.declared(), [(major, minor, patch, "populate_db.py")])

    def test_a_second_run_changes_nothing(self):
        """Re-declaring the same version is a no-op, so a rebuild does not
        rewrite a row that already says the right thing."""
        declare_database_version(self.conn)
        self.assertFalse(declare_database_version(self.conn))
        self.assertEqual(len(self.declared()), 1)

    def test_an_older_declaration_is_replaced_not_appended(self):
        """The table holds the version the file *is*, so the old row goes."""
        declare_database_version(self.conn, who="someone", comment="older format")
        self.conn.execute(
            "UPDATE DocumentationDatabaseVersion SET major = 1, minor = 4, patch = 0"
        )
        self.assertTrue(declare_database_version(self.conn))
        self.assertEqual([row[:3] for row in self.declared()], [DATABASE_FORMAT_VERSION])

    def test_a_3x_declaration_is_kept(self):
        """This step writes schema 3 too (compressed templates, no tooltips), so a
        3.0.0 file stays 3.0.0 rather than being declared 2.0.0 (ADFA-6740)."""
        self.conn.execute(VERSION_TABLE_SQL)
        self.conn.execute(
            "INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) "
            "VALUES (3, 0, 0, 'ADFA-3305', 'keyed tooltips')"
        )
        self.assertFalse(declare_database_version(self.conn))
        self.assertEqual(self.declared(), [(3, 0, 0, "ADFA-3305")])

    def test_several_rows_are_collapsed_to_one(self):
        """A database that picked up extra rows -- an older tool that appended, a
        hand-edit -- is repaired rather than read around, so "the version" can
        never be ambiguous."""
        self.conn.execute(VERSION_TABLE_SQL)
        for major in (1, 2, 1):
            self.conn.execute(
                "INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) "
                "VALUES (?, 0, 0, 'older tool', 'appended')",
                (major,),
            )
        self.assertTrue(declare_database_version(self.conn))
        self.assertEqual([row[:3] for row in self.declared()], [DATABASE_FORMAT_VERSION])

    def test_the_declared_version_is_what_the_app_gates_on(self):
        """CoGo's DatabaseVersionResolver.MAJOR_VERSION_WITH_COMPRESSION_DICTIONARY
        is 2; a database this script builds carries dictionary-compressed rows, so
        declaring anything lower would make the app decline to use the dictionary
        and fail every brotli row."""
        self.assertGreaterEqual(DATABASE_FORMAT_VERSION[0], 2)


if __name__ == "__main__":
    unittest.main()
