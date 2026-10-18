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
it instead of reading the whole table. An index of that name with any other
definition is refused by both operations, not reported as done.

    uv run scripts/test_tooltip_manager.py
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

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


def _searches_by_tooltip(plan: list) -> bool:
    """A SEARCH on tooltipId through the index. "SEARCH", not just "USING INDEX": an index with the
    columns the other way round still appears, in a full SCAN of it. Either INDEX or COVERING INDEX."""
    return any(line.startswith("SEARCH") and "TooltipButtonsByTooltip (tooltipId=?)" in line for line in plan)


def _scans_table(plan: list) -> bool:
    """"SCAN TooltipButtons" from SQLite 3.36 on, "SCAN TABLE TooltipButtons" before."""
    return any(line.startswith("SCAN") and "TooltipButtons" in line for line in plan)


def _run(operation: str, args: list) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(Path(tm.__file__)), "--operation", operation, *args], capture_output=True, text=True)


def _index(path: Path, args: list) -> subprocess.CompletedProcess:
    return _run("index", args)


def _csv(tmp_path: Path) -> Path:
    csv_file = tmp_path / "tooltips.csv"
    csv_file.write_text("categoryId,tag,summary,detail,description1,uri1,description2,uri2,description3,uri3\n"
                        "1,a,s,d,first,a1.html,second,a2.html,,\n"
                        "1,b,s,d,only,b1.html,,,,\n", encoding="utf-8")
    return csv_file


def _has_index(path: Path) -> bool:
    with sqlite3.connect(path) as conn:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' "
                                 "AND name = 'TooltipButtonsByTooltip'").fetchone())


def test_an_older_database_reads_the_whole_table_for_a_tooltips_buttons(tmp_path):
    plan = _plan(_old_database(tmp_path / "old.db"))
    assert _scans_table(plan) and not _searches_by_tooltip(plan), plan


def test_the_index_operation_adds_the_index_to_a_copy_and_leaves_the_input_alone(tmp_path):
    source, output = _old_database(tmp_path / "old.db"), tmp_path / "indexed.db"
    before = source.read_bytes()
    result = _index(source, ["--input-db", str(source), "--output-db", str(output)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_bytes() == before and not _has_index(source)
    assert _has_index(output)
    plan = _plan(output)
    assert _searches_by_tooltip(plan), plan
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


# Each leaves the buttons query scanning the table or sorting, so none may pass as "already exists".
OTHER_DEFINITIONS = {
    "other columns": ("ON TooltipButtons (tooltipId)", "on TooltipButtons (tooltipId),"),
    "columns reversed": ("ON TooltipButtons (buttonNumberId, tooltipId)",
                         "on TooltipButtons (buttonNumberId, tooltipId),"),
    "another table": ("ON OldButtons (tooltipId, buttonNumberId)", "on OldButtons (tooltipId, buttonNumberId),"),
    "where clause": ("ON TooltipButtons (tooltipId, buttonNumberId) WHERE tooltipId > 1000",
                     "on TooltipButtons (tooltipId, buttonNumberId) with a WHERE clause,"),
    "expression": ("ON TooltipButtons (tooltipId + 0, buttonNumberId)", "on TooltipButtons (<expr>, buttonNumberId),"),
    "collation": ("ON TooltipButtons (tooltipId COLLATE NOCASE, buttonNumberId)",
                  "on TooltipButtons (tooltipId COLLATE NOCASE, buttonNumberId),"),
}


@pytest.mark.parametrize("definition, shown", OTHER_DEFINITIONS.values(), ids=OTHER_DEFINITIONS.keys())
def test_an_index_of_that_name_with_another_definition_is_refused_not_reported_as_done(tmp_path, definition, shown):
    source, output = _old_database(tmp_path / "old.db"), tmp_path / "indexed.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE OldButtons (tooltipId INTEGER, buttonNumberId INTEGER)")
        conn.execute("CREATE INDEX TooltipButtonsByTooltip " + definition)
    result = _index(source, ["--input-db", str(source), "--output-db", str(output)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Error: an index named TooltipButtonsByTooltip already exists " + shown in result.stdout, result.stdout
    assert "Traceback" not in result.stderr, result.stderr


def test_the_index_under_a_differently_cased_name_counts_as_there(tmp_path):
    # SQLite matches index names without regard to case, so IF NOT EXISTS would have done nothing
    # while the function reported "Created".
    path = _old_database(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE INDEX tooltipbuttonsbytooltip ON TooltipButtons (tooltipId, buttonNumberId)")
    assert tm.index_tooltip_buttons(conn) is False
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'index' AND tbl_name = 'TooltipButtons'").fetchone()[0] == 1
    conn.close()


def test_a_build_from_csv_recreates_the_index(tmp_path):
    # DROP TABLE drops the table's indexes, so a build must put this one back.
    path = _old_database(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    tm.index_tooltip_buttons(conn)
    conn.close()
    tm.csv_to_tooltips(sqlite3.connect(path), str(_csv(tmp_path)), "test")
    assert _has_index(path)
    assert _searches_by_tooltip(_plan(path))
    with sqlite3.connect(path) as conn:
        assert conn.execute(BUTTONS_QUERY, (1,)).fetchall() == [("first", "a1.html"), ("second", "a2.html")]


def test_a_build_refuses_when_another_table_holds_the_index_name(tmp_path):
    # DROP TABLE TooltipButtons leaves this index, so CREATE INDEX IF NOT EXISTS would create nothing.
    source, output = _old_database(tmp_path / "old.db"), tmp_path / "built.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE INDEX TooltipButtonsByTooltip ON TooltipCategories (category)")
    result = _run("build", ["--name", "test", "--input-csv", str(_csv(tmp_path)), "--input-db", str(source),
                            "--output-db", str(output)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Error: an index named TooltipButtonsByTooltip already exists on TooltipCategories (category)," \
        in result.stdout, result.stdout
    assert "Database population complete!" not in result.stdout, result.stdout


def test_a_build_without_a_name_is_refused(tmp_path):
    # build records the name in LastChange.who; without one it would write NULL there.
    source, output = _old_database(tmp_path / "old.db"), tmp_path / "built.db"
    result = _run("build", ["--input-csv", str(_csv(tmp_path)), "--input-db", str(source), "--output-db", str(output)])
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--name are required for the 'build' operation" in result.stderr, result.stderr
    assert not output.exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
