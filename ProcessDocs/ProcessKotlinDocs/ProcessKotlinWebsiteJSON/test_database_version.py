#!/usr/bin/env python3
"""Tests for populate_db's database-version declaration (ADFA-5220).

Run directly: python3 test_database_version.py
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from populate_db import DATABASE_FORMAT_VERSION, declare_database_version


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

    def test_a_second_run_adds_nothing(self):
        """The table logs what the format became, not who ran what -- a row per
        invocation would bury the entries that matter."""
        declare_database_version(self.conn)
        self.assertFalse(declare_database_version(self.conn))
        self.assertEqual(len(self.declared()), 1)

    def test_an_older_declaration_is_superseded(self):
        declare_database_version(self.conn, who="someone", comment="older format")
        self.conn.execute(
            "UPDATE DocumentationDatabaseVersion SET major = 1, minor = 4, patch = 0"
        )
        self.assertTrue(declare_database_version(self.conn))
        self.assertEqual([row[:3] for row in self.declared()], [(1, 4, 0), DATABASE_FORMAT_VERSION])

    def test_a_downgrade_is_recorded_rather_than_hidden(self):
        """Rebuilding from an older pipeline really is a downgrade, and the app
        reads the row inserted last -- so it has to be written."""
        self.conn.execute(
            """
            CREATE TABLE DocumentationDatabaseVersion (
              major INT NOT NULL, minor INT NOT NULL, patch INT NOT NULL,
              who TEXT NOT NULL, comment TEXT NOT NULL,
              changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
            """
        )
        self.conn.execute(
            "INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) "
            "VALUES (9, 0, 0, 'future', 'a format this pipeline does not produce')"
        )
        self.assertTrue(declare_database_version(self.conn))
        self.assertEqual(self.declared()[-1][:3], DATABASE_FORMAT_VERSION)

    def test_the_declared_version_is_what_the_app_gates_on(self):
        """CoGo's DatabaseVersionResolver.MAJOR_VERSION_WITH_COMPRESSION_DICTIONARY
        is 2; a database this script builds carries dictionary-compressed rows, so
        declaring anything lower would make the app decline to use the dictionary
        and fail every brotli row."""
        self.assertGreaterEqual(DATABASE_FORMAT_VERSION[0], 2)


if __name__ == "__main__":
    unittest.main()
