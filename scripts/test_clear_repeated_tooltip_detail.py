#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "pytest",
# ]
# ///
"""
test_clear_repeated_tooltip_detail.py

Covers clear_repeated_tooltip_detail.py: that it empties a detail only when it
is exactly its summary, whatever collation the schema gives the columns; that
it says how many such tooltips have no buttons; and that a run is all or
nothing, a dry run writes nothing, and a second run changes nothing.

Run it either way - as a plain script:

    uv run scripts/test_clear_repeated_tooltip_detail.py

or under pytest:

    uv run --with brotli --with Pillow --with pytest pytest scripts/test_clear_repeated_tooltip_detail.py
"""
from __future__ import annotations

import contextlib
import io
import re
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clear_repeated_tooltip_detail as tool  # noqa: E402

# (id, category, summary, detail, buttons)
TOOLTIPS = [
    (1, 4, "Returns the size.", "Returns the size.", 1),
    (2, 4, "Adds an element.", "Adds an element, if there is room.", 1),
    (3, 4, "Removes it.", "", 1),
    (4, 3, "Splits a string.", "Splits a string.", 2),
    (5, 4, "Trims it.", "Trims it. ", 1),
    (6, 4, "Counts.", "counts.", 1),
    (7, 1, "Opens the file.", "Opens the file.", 0),
    (8, 4, "", "", 1),
]


def _database(tmp_path: Path, tooltips=TOOLTIPS, collate: str = "") -> Path:
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE TooltipCategories (id INTEGER PRIMARY KEY, category TEXT NOT NULL);"
        "INSERT INTO TooltipCategories VALUES (1, 'ide'), (3, 'java'), (4, 'kotlin');"
        f"CREATE TABLE Tooltips (id INTEGER PRIMARY KEY AUTOINCREMENT, categoryId INTEGER NOT NULL, tag TEXT NOT NULL,"
        f" summary TEXT NOT NULL {collate}, detail TEXT NOT NULL {collate}, UNIQUE (categoryId, tag));"
        "CREATE TABLE TooltipButtons (tooltipId INTEGER, buttonNumberId INTEGER, description TEXT, uri TEXT);"
    )
    for tooltip_id, category, summary, detail, buttons in tooltips:
        conn.execute("INSERT INTO Tooltips VALUES (?, ?, ?, ?, ?)", (tooltip_id, category, f"tag{tooltip_id}",
                                                                     summary, detail))
        for number in range(buttons):
            conn.execute("INSERT INTO TooltipButtons VALUES (?, ?, 'View full documentation', 'a/x.html')",
                         (tooltip_id, number + 1))
    conn.commit()
    conn.close()
    return db_path


def _details(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return {row[0]: row[1:] for row in conn.execute("SELECT id, summary, detail FROM Tooltips")}
    finally:
        conn.close()


def _run(db_path: Path, **overrides) -> tuple:
    cfg = {"db_path": db_path, "dry_run": False, "verbose": False}
    cfg.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.run(cfg)
    return code, out.getvalue(), err.getvalue()


def test_only_a_detail_that_is_exactly_its_summary_is_emptied(tmp_path: Path):
    db = _database(tmp_path)
    before = _details(db)
    code, out, err = _run(db)
    after = _details(db)
    assert code == 0, err
    assert {i for i in before if after[i] != before[i]} == {1, 4, 7}
    for i in (1, 4, 7):
        assert after[i] == (before[i][0], "")
    assert "3 of 8 tooltip(s)" in out and "ide 1, java 1, kotlin 1" in out
    assert "saves 48 byte(s)" in out


def test_a_case_insensitive_schema_does_not_widen_the_match(tmp_path: Path):
    db = _database(tmp_path, collate="COLLATE NOCASE")
    code, _out, err = _run(db)
    assert code == 0, err
    assert _details(db)[6] == ("Counts.", "counts.")


def test_a_detail_that_is_not_text_is_left_alone(tmp_path: Path):
    db = _database(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE Tooltips SET summary = CAST(summary AS BLOB), detail = CAST(detail AS BLOB) WHERE id = 1")
    conn.commit()
    conn.close()
    code, out, err = _run(db)
    assert code == 0, err
    assert _details(db)[1] == (b"Returns the size.", b"Returns the size.") and "2 of 8 tooltip(s)" in out


def test_tooltips_without_buttons_are_counted(tmp_path: Path):
    code, out, _err = _run(_database(tmp_path))
    assert code == 0 and "1 of them have no buttons, so their tooltip loses its See More link" in out


def test_dry_run_writes_nothing(tmp_path: Path):
    db = _database(tmp_path)
    before = _details(db)
    code, out, _err = _run(db, dry_run=True)
    assert code == 0 and "3 of 8 tooltip(s)" in out and "Dry run" in out
    assert _details(db) == before and len(list(tmp_path.iterdir())) == 1


def test_a_second_run_changes_nothing(tmp_path: Path):
    db = _database(tmp_path)
    assert _run(db)[0] == 0
    once, files = _details(db), sorted(tmp_path.iterdir())
    code, out, _err = _run(db)
    assert code == 0 and out.startswith("Nothing to do")
    assert _details(db) == once and sorted(tmp_path.iterdir()) == files


def test_the_backup_holds_the_database_as_it_was(tmp_path: Path):
    db = _database(tmp_path)
    before = _details(db)
    code, out, _err = _run(db)
    backup = Path(re.search(r"Backup written to (.+)", out).group(1).strip())
    assert code == 0 and _details(backup) == before


def test_an_error_while_writing_rolls_everything_back(tmp_path: Path):
    db = _database(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TRIGGER fail BEFORE UPDATE ON Tooltips WHEN OLD.id = 4 BEGIN SELECT RAISE(ABORT, 'disk full');"
                 " END")
    conn.commit()
    conn.close()
    before = _details(db)
    try:
        _run(db)
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("the failure did not propagate")
    assert _details(db) == before


def test_a_migrated_database_is_refused_by_name(tmp_path: Path):
    db = _database(tmp_path)
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE TooltipTexts (id INTEGER PRIMARY KEY, summary TEXT, detail TEXT);"
                       "INSERT INTO TooltipTexts SELECT id, summary, detail FROM Tooltips;"
                       "CREATE TABLE T2 (id INTEGER, categoryId INTEGER, tag TEXT);"
                       "INSERT INTO T2 SELECT id, categoryId, tag FROM Tooltips;"
                       "DROP TABLE Tooltips; ALTER TABLE T2 RENAME TO Tooltips;")
    conn.close()
    code, out, err = _run(db)
    assert code == 1 and "run this step before migrate_tooltip_schema.py" in err, err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["documentation.db"]
    # Even with the old columns still on Tooltips, a TooltipTexts means the text lives there.
    (tmp_path / "half").mkdir()
    db2 = _database(tmp_path / "half")
    conn = sqlite3.connect(db2)
    conn.execute("CREATE TABLE TooltipTexts (id INTEGER PRIMARY KEY, summary TEXT, detail TEXT)")
    conn.close()
    before = _details(db2)
    code, _out, err = _run(db2)
    assert code == 1 and "migrate_tooltip_schema.py" in err and _details(db2) == before


def test_the_script_declares_what_its_imports_need():
    # `uv run scripts/clear_repeated_tooltip_detail.py` installs only what its PEP 723 block lists, and
    # optimize_db_media, which it imports, imports brotli and PIL at module level. This test file declares both
    # itself, so without this check a missing block shows up only when the step is run.
    script = Path(tool.__file__).read_text()
    block = re.search(r"^# /// script\n(.*?)^# ///$", script, re.S | re.M)
    assert block, "no PEP 723 script block"
    declared = set(re.findall(r'"([A-Za-z0-9_.-]+)', block.group(1).split("dependencies", 1)[-1]))
    imported = Path(tool.__file__).with_name("optimize_db_media.py").read_text()
    needed = {"brotli": r"^import brotli\b", "Pillow": r"^from PIL import"}
    for package, pattern in needed.items():
        assert re.search(pattern, imported, re.M), f"optimize_db_media no longer imports {package}; update this test"
        assert package in declared, f"{package} is not declared"


def test_a_database_without_tooltips_is_an_error(tmp_path: Path):
    db = tmp_path / "documentation.db"
    sqlite3.connect(db).close()
    code, _out, err = _run(db)
    assert code == 1 and "no Tooltips table" in err
    code, _out, err = _run(tmp_path / "absent.db")
    assert code == 1 and "does not exist" in err


def _main() -> int:
    """Runs the tests without pytest (`uv run scripts/test_clear_repeated_tooltip_detail.py`)."""
    failures = []
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        with tempfile.TemporaryDirectory() as directory:
            needs_tmp = "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]
            try:
                function(Path(directory)) if needs_tmp else function()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - a failure is the result here
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): {type(exc).__name__}: {exc}")
                print(f"FAIL {name}", file=sys.stderr)
            else:
                print(f"ok   {name}")
    print()
    print(f"{len(failures)} failure(s)" if failures else "all tests passed")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
