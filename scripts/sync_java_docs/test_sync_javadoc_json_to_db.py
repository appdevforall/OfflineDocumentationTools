#!/usr/bin/env python3
"""Tests that sync_javadoc_json_to_db.py writes javadoc.peb the way the database stores templates (ADFA-6740).

From documentation 3.0.0 every template is stored compressed against CompressionDictionary, with no per-row marker,
and the app decodes every one; below it they are plain. A later major, a format this step does not know, is refused
before the backup.

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
DICTIONARY = b"<html><head><title>{{ page }}</title></head><body>{% if page %}{% endif %}</body></html>" * 20
TEMPLATE = b"<html>{{ page }}</html>"

SCHEMA = """
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);
CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL,
    content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL, UNIQUE(name));
CREATE TABLE LastChange (documentationSet TEXT, changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP, who TEXT);
CREATE TABLE DocumentationDatabaseVersion (major INT NOT NULL, minor INT NOT NULL, patch INT NOT NULL,
    who TEXT NOT NULL, comment TEXT NOT NULL, changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL);
INSERT INTO Languages (value) VALUES ('en-US');
INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli');
"""


class TemplateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.db_path = self.dir / "documentation.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO CompressionDictionary VALUES (1, ?)", (DICTIONARY,))
        conn.commit()
        conn.close()
        (self.dir / "api").mkdir()
        (self.dir / "templates").mkdir()
        (self.dir / "templates" / "javadoc.peb").write_bytes(TEMPLATE)
        (self.dir / "dictionary.bin").write_bytes(DICTIONARY)

    def declare(self, *rows):
        """Appends (major, changeTime) rows to DocumentationDatabaseVersion, in order."""
        conn = sqlite3.connect(self.db_path)
        conn.executemany("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                         "VALUES (?, 0, 0, 'test', 'test', ?)", rows)
        conn.commit()
        conn.close()

    def sync(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), str(self.dir / "api"), "--db", str(self.db_path),
                               "--templates", str(self.dir / "templates"), *args],
                              capture_output=True, text=True, cwd=self.dir, check=False)

    def sha(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def stored(self) -> bytes:
        conn = sqlite3.connect(self.db_path)
        try:
            (name, content), = conn.execute("SELECT name, content FROM Templates").fetchall()
        finally:
            conn.close()
        self.assertEqual(name, "javadoc.peb")
        return content

    def decoded(self, stream: bytes) -> bytes:
        self.assertNotEqual(stream[0] & 0x7F, 0x11)  # never large-window
        return subprocess.run(["brotli", "-D", str(self.dir / "dictionary.bin"), "-d", "-c"], input=stream,
                              capture_output=True, check=True).stdout

    def test_refuses_a_later_major_before_the_backup(self):
        self.declare((4, "2026-10-19 00:00:00"))
        sha = self.sha()
        result = self.sync()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("declares documentation version 4, and this step writes versions up to 3.x", result.stderr)
        self.assertEqual(self.sha(), sha)
        self.assertEqual(sorted(p.name for p in self.dir.glob("documentation.db*")), ["documentation.db"])

    def test_compresses_the_template_at_major_3(self):
        self.declare((2, "2026-01-01 00:00:00"), (3, "2026-10-19 00:00:00"))
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.decoded(self.stored()), TEMPLATE)

    def test_compresses_the_template_at_major_3_even_with_plain_brotli(self):
        self.declare((3, "2026-10-19 00:00:00"))
        self.assertEqual(self.sync("--plain-brotli").returncode, 0)
        self.assertEqual(self.decoded(self.stored()), TEMPLATE)

    def test_major_3_without_a_dictionary_is_refused(self):
        self.declare((3, "2026-10-19 00:00:00"))
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM CompressionDictionary")
        conn.commit()
        conn.close()
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("but it has none; nothing was changed", result.stderr)

    def test_writes_the_template_plain_at_major_2(self):
        # The newest rowid breaks the changeTime tie.
        self.declare((3, "2026-10-19 00:00:00"), (2, "2026-10-19 00:00:00"))
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stored(), TEMPLATE)


if __name__ == "__main__":
    unittest.main()
