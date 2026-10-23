#!/usr/bin/env python3
"""Tests that populate_db.py writes no plain template into a database whose templates are compressed (ADFA-3305).

From documentation 3.0.0 migrate_tooltip_schema.py stores every template compressed, and the app decodes every one,
so a plain page.peb or nav.peb written afterwards would not render while the file still declared 3.x.

Run directly: python3 test_populate_db_templates.py
"""
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from populate_db import compressed_templates_refusal, declared_major, upsert_template

HERE = Path(__file__).resolve().parent

SCHEMA = """
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE, compression TEXT NOT NULL);
CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL,
    content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL, UNIQUE(name));
CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL);
CREATE TABLE DocumentationDatabaseVersion (major INT NOT NULL, minor INT NOT NULL, patch INT NOT NULL,
    who TEXT NOT NULL, comment TEXT NOT NULL, changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
INSERT INTO Languages (value) VALUES ('en-US');
INSERT INTO ContentTypes (value, compression) VALUES ('text/html', 'brotli'), ('text/css', 'brotli'),
    ('text/javascript', 'brotli');
INSERT INTO CompressionDictionary (id, data) VALUES (1, CAST('<html><head><title></title></head><body></body>' AS BLOB));
INSERT INTO Templates (name, content) VALUES ('page.peb', CAST('stored before' AS BLOB));
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

    def declare(self, *rows):
        """Appends (major, changeTime) rows to DocumentationDatabaseVersion, in order."""
        conn = sqlite3.connect(self.db_path)
        conn.executemany("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                         "VALUES (?, 0, 0, 'test', 'test', ?)", rows)
        conn.commit()
        conn.close()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def templates(self) -> list:
        return self.connect().execute("SELECT name, content FROM Templates ORDER BY id").fetchall()

    def test_upsert_template_refuses_major_3(self):
        self.declare((2, "2026-01-01 00:00:00"), (3, "2026-10-19 00:00:00"))
        before = self.templates()
        conn = self.connect()
        with self.assertRaises(RuntimeError) as raised:
            upsert_template(conn, "page.peb", "<html>new</html>")
        self.assertIn("declares documentation version 3", str(raised.exception))
        self.assertIn("Run it before migrate_tooltip_schema.py", str(raised.exception))
        conn.commit()
        self.assertEqual(self.templates(), before)

    def test_upsert_template_refuses_a_later_major(self):
        self.declare((4, "2026-10-19 00:00:00"))
        with self.assertRaises(RuntimeError):
            upsert_template(self.connect(), "nav.peb", "<nav/>")

    def test_upsert_template_writes_at_major_2_and_without_a_version(self):
        conn = self.connect()
        upsert_template(conn, "nav.peb", "<nav/>")
        conn.commit()
        self.declare((2, "2026-10-19 00:00:00"))
        upsert_template(conn, "page.peb", "<html>new</html>")
        conn.commit()
        self.assertEqual(self.templates(), [("page.peb", b"<html>new</html>"), ("nav.peb", b"<nav/>")])

    def test_reads_the_newest_version_row_as_the_app_does(self):
        # Newest changeTime first, whatever the insert order; then the newest rowid.
        self.declare((3, "2026-10-19 00:00:00"), (2, "2026-01-01 00:00:00"))
        self.assertEqual(declared_major(self.connect()), 3)
        self.declare((2, "2026-10-19 00:00:00"))
        self.assertEqual(declared_major(self.connect()), 2)
        self.assertIsNone(compressed_templates_refusal(self.connect()))

    def test_command_line_refuses_before_converting_or_backing_up(self):
        self.declare((3, "2026-10-19 00:00:00"))
        docs = self.dir / "docs"
        (docs / "topics").mkdir(parents=True)
        (docs / "topics" / "home.md").write_text("# Home\n\nText.\n", encoding="utf-8")
        (docs / "kr.tree").write_text('<instance-profile id="kr" start-page="home.topic">'
                                      '<toc-element topic="home.md"/></instance-profile>', encoding="utf-8")
        config = self.dir / "config.json"
        config.write_text('{"broken-ext-link-color": "#c00", "menu-no-link-color": "#888"}', encoding="utf-8")
        images = self.dir / "images.zip"
        zipfile.ZipFile(images, "w").close()
        sha = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        result = subprocess.run([sys.executable, str(HERE / "populate_db.py"), str(docs), str(config), str(images),
                                 str(self.db_path)], capture_output=True, text=True, cwd=self.dir, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Run it before migrate_tooltip_schema.py; nothing was changed", result.stderr)
        self.assertNotIn("Backing up", result.stderr)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), sha)
        self.assertEqual(sorted(p.name for p in self.dir.glob("documentation.db*")), ["documentation.db"])


if __name__ == "__main__":
    unittest.main()
