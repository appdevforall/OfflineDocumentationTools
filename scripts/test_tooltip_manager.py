#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pytest",
# ]
# ///
"""
test_tooltip_manager.py

Covers the TooltipButtons index TooltipManager.py keeps (ADFA-6597): a build
from CSV recreates it, the index operation adds it to a copy of an older
database and leaves the input alone, and Code On The Go's buttons query uses
it instead of reading the whole table.

    uv run scripts/test_tooltip_manager.py
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import TooltipManager as tm  # noqa: E402

# The query ToolTipManager.kt runs for a tooltip's buttons.
BUTTONS_QUERY = "SELECT description, uri FROM TooltipButtons WHERE tooltipId = ? ORDER BY buttonNumberId"


def _old_database(path: Path) -> Path:
    """A documentation database as it was before the index: the tooltip tables, filled."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE TooltipCategories (id INTEGER PRIMARY KEY, category TEXT NOT NULL)")
    conn.execute("CREATE TABLE TooltipButtonNumbers (id INTEGER PRIMARY KEY)")
    conn.execute(tm.SCHEMA_TOOLTIPS)
    conn.execute(tm.SCHEMA_TOOLTIP_BUTTONS)
    conn.execute("INSERT INTO TooltipCategories VALUES (1, 'kotlin')")
    conn.executemany("INSERT INTO TooltipButtonNumbers VALUES (?)", [(1,), (2,), (3,)])
    for n in range(1, 201):
        conn.execute("INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (1, ?, 's', 'd')", (f"tag{n}",))
        # Buttons inserted out of order, so ORDER BY has work to do.
        for number in (3, 1, 2):
            conn.execute("INSERT INTO TooltipButtons VALUES (?, ?, ?, ?)", (n, number, f"b{number}", f"u{n}-{number}.html"))
    conn.commit()
    conn.close()
    return path


def _plan(path: Path) -> list:
    with sqlite3.connect(path) as conn:
        return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + BUTTONS_QUERY, (7,))]


def _has_index(path: Path) -> bool:
    with sqlite3.connect(path) as conn:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' "
                                 "AND name = 'TooltipButtonsByTooltip'").fetchone())


def test_an_older_database_reads_the_whole_table_for_a_tooltips_buttons(tmp_path):
    plan = _plan(_old_database(tmp_path / "old.db"))
    assert any(line.startswith("SCAN TooltipButtons") for line in plan), plan


def test_the_index_operation_adds_the_index_to_a_copy_and_leaves_the_input_alone(tmp_path):
    source, output = _old_database(tmp_path / "old.db"), tmp_path / "indexed.db"
    before = source.read_bytes()
    result = subprocess.run([sys.executable, str(Path(tm.__file__)), "--operation", "index", "--name", "test",
                             "--input-db", str(source), "--output-db", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_bytes() == before and not _has_index(source)
    assert _has_index(output)
    plan = _plan(output)
    assert any("USING INDEX TooltipButtonsByTooltip" in line for line in plan), plan
    assert not any("TEMP B-TREE" in line for line in plan), plan
    with sqlite3.connect(output) as conn:
        assert conn.execute(BUTTONS_QUERY, (7,)).fetchall() == [("b1", "u7-1.html"), ("b2", "u7-2.html"),
                                                                ("b3", "u7-3.html")]


def test_indexing_twice_changes_nothing(tmp_path):
    path = _old_database(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    assert tm.index_tooltip_buttons(conn) is True
    assert tm.index_tooltip_buttons(conn) is False
    conn.close()


def test_a_build_from_csv_recreates_the_index(tmp_path):
    # DROP TABLE drops the table's indexes, so a build must put this one back.
    path = _old_database(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    tm.index_tooltip_buttons(conn)
    conn.close()
    csv_file = tmp_path / "tooltips.csv"
    csv_file.write_text("categoryId,tag,summary,detail,description1,uri1,description2,uri2,description3,uri3\n"
                        "1,a,s,d,first,a1.html,second,a2.html,,\n"
                        "1,b,s,d,only,b1.html,,,,\n", encoding="utf-8")
    tm.csv_to_tooltips(sqlite3.connect(path), str(csv_file), "test")
    assert _has_index(path)
    assert any("USING INDEX TooltipButtonsByTooltip" in line for line in _plan(path))
    with sqlite3.connect(path) as conn:
        assert conn.execute(BUTTONS_QUERY, (1,)).fetchall() == [("first", "a1.html"), ("second", "a2.html")]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
