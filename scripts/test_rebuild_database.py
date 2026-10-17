#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pytest",
# ]
# ///
"""
test_rebuild_database.py

Covers rebuild_database.py: the copy drops the reserved bytes, keeps the page
size, and keeps everything a reader could see - schema, rows with their
rowids, sqlite_sequence, user_version, application_id - and a copy that
differs is deleted rather than kept.

The source database is made with the sqlite3 shell, the one way to give a
file reserved bytes, so it must be on PATH.

    uv run scripts/test_rebuild_database.py
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rebuild_database as rebuild  # noqa: E402
import test_media_bundle as base  # noqa: E402

BIG = bytes(range(256)) * 20  # spills onto overflow pages at 1 KB


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "documentation.db"
    subprocess.run(["sqlite3", str(path), "PRAGMA page_size=1024;", ".filectrl reserve_bytes 12", "VACUUM;"],
                   check=True, capture_output=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, content BLOB NOT NULL,
                              UNIQUE(path));
        CREATE TABLE TooltipButtons (tooltipId INTEGER, description TEXT,
                                     FOREIGN KEY(tooltipId) REFERENCES Content(id));
        CREATE INDEX buttons_by_tooltip ON TooltipButtons (tooltipId);
        CREATE VIEW paths AS SELECT path FROM Content;
        CREATE TRIGGER no_empty BEFORE INSERT ON Content WHEN length(NEW.content) = 0
            BEGIN SELECT RAISE(ABORT, 'empty'); END;
    """)
    conn.executemany("INSERT INTO Content (id, path, content) VALUES (?, ?, ?)",
                     [(1, "a.html", b"<p>a</p>"), (5, "big.bin", BIG), (9, "c.html", b"c")])
    conn.executemany("INSERT INTO TooltipButtons (rowid, tooltipId, description) VALUES (?, ?, ?)",
                     [(3, 1, "one"), (40, 5, None), (41, 77, "points nowhere")])
    conn.execute("UPDATE sqlite_sequence SET seq = 120 WHERE name = 'Content'")
    conn.execute("PRAGMA user_version = 7")
    conn.execute("PRAGMA application_id = 1234")
    conn.commit()
    conn.close()
    assert rebuild.reserved_bytes(path) == 12
    return path


def _run(source: Path, output: Path) -> int:
    return rebuild.run({"source": source, "output": output})


def test_the_copy_drops_the_reserved_bytes_and_nothing_else(tmp_path):
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    before = rebuild.fingerprint(source)

    assert _run(source, output) == 0

    assert rebuild.reserved_bytes(output) == 0
    with sqlite3.connect(output) as conn:
        assert conn.execute("PRAGMA page_size").fetchone()[0] == 1024
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    assert rebuild.fingerprint(output) == before
    assert rebuild.fingerprint(source) == before, "the source is never changed"


def test_rowids_gaps_sequence_and_pragmas_survive(tmp_path):
    """The parts a plain INSERT ... SELECT would quietly renumber or drop."""
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    assert _run(source, output) == 0
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT rowid FROM TooltipButtons ORDER BY rowid").fetchall() == [(3,), (40,), (41,)]
        assert conn.execute("SELECT id FROM Content ORDER BY id").fetchall() == [(1,), (5,), (9,)]
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'Content'").fetchone() == (120,)
        assert conn.execute("PRAGMA user_version").fetchone() == (7,)
        assert conn.execute("PRAGMA application_id").fetchone() == (1234,)
        assert conn.execute("SELECT content FROM Content WHERE id = 5").fetchone() == (BIG,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == [("TooltipButtons", 41, "Content", 0)]


def test_an_existing_output_is_refused(tmp_path):
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    output.write_bytes(b"keep me")
    assert _run(source, output) == 1
    assert output.read_bytes() == b"keep me"


def test_a_copy_that_differs_is_deleted(tmp_path):
    """The comparison is what makes the copy safe to swap in, so a copy that
    lost anything must not survive the run."""
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    unpatched = rebuild.rebuild

    def lossy(src, out):
        unpatched(src, out)
        with sqlite3.connect(out) as conn:
            conn.execute("DELETE FROM TooltipButtons WHERE rowid = 40")

    rebuild.rebuild = lossy
    try:
        assert _run(source, output) == 1
    finally:
        rebuild.rebuild = unpatched
    assert not output.exists()


if __name__ == "__main__":
    sys.exit(base._main(globals()))
