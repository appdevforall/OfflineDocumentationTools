#!/usr/bin/env python3
"""Tests that sync_javadoc_json_to_db.py writes no plain template into a database whose templates are compressed.

From documentation 3.0.0 migrate_tooltip_schema.py (ADFA-3305) stores every template compressed, and the app decodes
every one, so a plain javadoc.peb written afterwards would not render while the file still declared 3.x.

Run directly (python3 scripts/sync_java_docs/test_sync_javadoc_json_to_db.py) or under pytest. Needs the brotli CLI.
"""
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "sync_javadoc_json_to_db.py"

SCHEMA = """
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);
CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL,
    content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL, UNIQUE(name));
CREATE TABLE LastChange (documentationSet TEXT, changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP, who TEXT);
CREATE TABLE DocumentationDatabaseVersion (major INT NOT NULL, minor INT NOT NULL, patch INT NOT NULL,
    who TEXT NOT NULL, comment TEXT NOT NULL, changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
INSERT INTO Languages (value) VALUES ('en-US');
INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli');
"""


class CompressedTemplatesRefusalTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.db_path = self.dir / "documentation.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.close()
        (self.dir / "api").mkdir()
        (self.dir / "templates").mkdir()
        (self.dir / "templates" / "javadoc.peb").write_text("<html>{{ page }}</html>", encoding="utf-8")

    def declare(self, *rows):
        """Appends (major, changeTime) rows to DocumentationDatabaseVersion, in order."""
        conn = sqlite3.connect(self.db_path)
        conn.executemany("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                         "VALUES (?, 0, 0, 'test', 'test', ?)", rows)
        conn.commit()
        conn.close()

    def sync(self) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), str(self.dir / "api"), "--db", str(self.db_path),
                               "--templates", str(self.dir / "templates")],
                              capture_output=True, text=True, cwd=self.dir, check=False)

    def sha(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def assert_refused(self, major: int):
        sha = self.sha()
        result = self.sync()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"declares documentation version {major}", result.stderr)
        self.assertIn("Run this before migrate_tooltip_schema.py; nothing was changed.", result.stderr)
        self.assertEqual(self.sha(), sha)
        self.assertEqual(sorted(p.name for p in self.dir.glob("documentation.db*")), ["documentation.db"])

    def test_refuses_major_3_before_the_backup(self):
        self.declare((2, "2026-01-01 00:00:00"), (3, "2026-10-19 00:00:00"))
        self.assert_refused(3)

    def test_refuses_a_later_major(self):
        self.declare((4, "2026-10-19 00:00:00"))
        self.assert_refused(4)

    def test_reads_the_newest_changetime_first(self):
        self.declare((3, "2026-10-19 00:00:00"), (2, "2026-01-01 00:00:00"))
        self.assert_refused(3)

    def test_writes_the_template_at_major_2(self):
        # The newest rowid breaks the changeTime tie.
        self.declare((3, "2026-10-19 00:00:00"), (2, "2026-10-19 00:00:00"))
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT name, content FROM Templates").fetchall(),
                             [("javadoc.peb", b"<html>{{ page }}</html>")])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
