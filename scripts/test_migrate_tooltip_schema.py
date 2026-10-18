#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pytest",
# ]
# ///
"""
test_migrate_tooltip_schema.py

Covers migrate_tooltip_schema.py: that every tooltip and its buttons read back
through the new tables, with the queries Code On The Go runs, exactly as they
did through the old ones; that those queries search by key; that a second run,
a dry run and every refusal leave the file byte for byte as it was; and that a
conversion its own read-back check rejects is rolled back.

Run it either way - as a plain script:

    uv run scripts/test_migrate_tooltip_schema.py

or under pytest:

    uv run --with pytest pytest scripts/test_migrate_tooltip_schema.py

Only the standard library is needed for the first.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import re
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import migrate_tooltip_schema as tool  # noqa: E402

# What idetooltips/.../TooltipQuery.kt runs, before (PR #2160) and after this conversion.
OLD_APP_TOOLTIP_QUERY = ("SELECT T.id, T.summary, T.detail FROM Tooltips AS T "
                         "WHERE T.categoryId IN (SELECT id FROM TooltipCategories WHERE category = ?) AND T.tag = ?")
OLD_APP_BUTTONS_QUERY = "SELECT description, uri FROM TooltipButtons WHERE tooltipId = ? ORDER BY buttonNumberId"
APP_TOOLTIP_QUERY = ("SELECT T.id, X.summary, X.detail FROM Tooltips AS T JOIN TooltipTexts AS X ON X.id = T.id "
                     "WHERE T.categoryId IN (SELECT id FROM TooltipCategories WHERE category = ?) AND T.tag = ?")
APP_BUTTONS_QUERY = ("SELECT D.description, U.uri FROM TooltipButtons AS B "
                     "JOIN TooltipButtonDescriptions AS D ON D.id = B.descriptionId "
                     "JOIN TooltipButtonUris AS U ON U.id = B.uriId "
                     "WHERE B.tooltipId = ? ORDER BY B.buttonNumberId")

# The 2026-10-18 database's own DDL for the tables the tool reads or touches.
OLD_SCHEMA = """
CREATE TABLE TooltipCategories (
 id       INTEGER PRIMARY KEY,
 category TEXT NOT NULL
);
CREATE TABLE TooltipButtonNumbers (
  id   INTEGER UNIQUE
);
CREATE TABLE Tooltips (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 categoryId INTEGER NOT NULL,
 tag TEXT NOT NULL,
 summary TEXT NOT NULL,
 detail TEXT NOT NULL,
 UNIQUE (categoryId, tag),
 FOREIGN KEY(categoryId) REFERENCES TooltipCategories(id)
);
CREATE TABLE TooltipButtons (
 tooltipId INTEGER,
 buttonNumberId INTEGER,
 description TEXT,
 uri TEXT,
 FOREIGN KEY(tooltipId) REFERENCES Tooltips(id),
 FOREIGN KEY(buttonNumberId) REFERENCES TooltipButtonNumbers(id)
);
CREATE TABLE DocumentationDatabaseVersion (
  major      INT NOT NULL,
  minor      INT NOT NULL,
  patch      INT NOT NULL,
  who        TEXT NOT NULL,
  comment    TEXT NOT NULL,
  changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE Content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        languageID INTEGER NOT NULL,
        content BLOB NOT NULL,
        contentTypeID INTEGER NOT NULL,
        templateId INTEGER NOT NULL DEFAULT 0,
        UNIQUE(path)
    );
CREATE TABLE Bookshelf (contentID INTEGER NOT NULL, title STRING DEFAULT '');
CREATE TRIGGER AddBook AFTER INSERT ON Content WHEN NEW.path LIKE '%.pdf'
BEGIN
    INSERT INTO Bookshelf (contentID, title) VALUES (NEW.id, CURRENT_TIMESTAMP || NEW.id);
END;
"""

CATEGORIES = ["ide", "xml", "java", "kotlin"]
DESCRIPTIONS = ["View full documentation", "Examples", "Kotlin docs", "View full documentation"]


def _database(tmp_path: Path, schema: str = OLD_SCHEMA, major: int = 2, tooltips: int = 60) -> Path:
    """An old-shape documentation database: tooltips in every category, 0-3 buttons each inserted out of
    order, shared and unique descriptions and URIs, and a sequence past the largest id (the last tooltip
    was deleted)."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    conn.executemany("INSERT INTO TooltipCategories VALUES (?, ?)", list(enumerate(CATEGORIES, start=1)))
    conn.executemany("INSERT INTO TooltipButtonNumbers VALUES (?)", [(1,), (2,), (3,)])
    if major is not None:
        conn.execute("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) "
                     "VALUES (?, 1, 0, 'test', 'test')", (major,))
    conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID) VALUES ('a/x.html', 1, x'00', 1)")
    for n in range(tooltips):
        category = n % 4 + 1
        detail = "" if n % 7 == 0 else f"<p>Detail {n} — été</p>"
        conn.execute("INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES (?, ?, ?, ?, ?)",
                     (1000 + 3 * n, category, f"tag{n // 4}" if n % 5 else str(n), f"Summary {n}", detail))
        for number in [(), (1,), (2, 1), (3, 1, 2)][n % 4]:
            uri = f"a/shared{n % 3}.html" if number == 1 else f"a/page{n}-{number}.html#frag"
            conn.execute("INSERT INTO TooltipButtons VALUES (?, ?, ?, ?)",
                         (1000 + 3 * n, number, DESCRIPTIONS[(n + number) % 4], uri))
    conn.execute("INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES (99999, 1, 'gone', 's', 'd')")
    conn.execute("DELETE FROM Tooltips WHERE id = 99999")
    conn.commit()
    conn.close()
    return db_path


def _run(db_path: Path, **overrides) -> tuple:
    """(exit code, stdout, stderr) of one run of the tool."""
    cfg = {"db_path": db_path, "dry_run": False}
    cfg.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.run(cfg)
    return code, out.getvalue(), err.getvalue()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backups(db_path: Path) -> list:
    return sorted(db_path.parent.glob(db_path.name + ".backup-*"))


def _shown(db_path: Path, tooltip_query: str, buttons_query: str) -> dict:
    """What the app shows for every (category, tag): id, summary, detail and buttons, through the given queries."""
    conn = sqlite3.connect(db_path)
    try:
        keys = conn.execute("SELECT C.category, T.tag FROM Tooltips AS T JOIN TooltipCategories AS C "
                            "ON C.id = T.categoryId").fetchall()
        shown = {}
        for category, tag in keys:
            rows = conn.execute(tooltip_query, (category, tag)).fetchall()
            assert len(rows) == 1, (category, tag, rows)
            tooltip_id, summary, detail = rows[0]
            shown[(category, tag)] = (tooltip_id, summary, detail,
                                      conn.execute(buttons_query, (tooltip_id,)).fetchall())
        return shown
    finally:
        conn.close()


def _old_shown(db_path: Path) -> dict:
    return _shown(db_path, OLD_APP_TOOLTIP_QUERY, OLD_APP_BUTTONS_QUERY)


def _new_shown(db_path: Path) -> dict:
    return _shown(db_path, APP_TOOLTIP_QUERY, APP_BUTTONS_QUERY)


def _query(db_path: Path, sql: str, args=()) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _plan(db_path: Path, sql: str, args) -> list:
    return [row[3] for row in _query(db_path, "EXPLAIN QUERY PLAN " + sql, args)]


def test_every_tooltip_reads_back_as_the_app_showed_it(tmp_path):
    db = _database(tmp_path)
    before = _old_shown(db)
    code, out, err = _run(db)
    assert code == 0, err
    assert "read back identically (60 tooltip(s))" in out
    after = _new_shown(db)
    assert after == before
    assert sum(len(v[3]) for v in after.values()) == 90
    assert any(v[3] == [] for v in after.values()) and any(len(v[3]) == 3 for v in after.values())


def test_the_tool_checks_with_the_queries_the_app_runs():
    squash = lambda sql: re.sub(r"\s+", " ", sql).strip()  # noqa: E731
    assert squash(tool.APP_TOOLTIP_QUERY) == squash(APP_TOOLTIP_QUERY)
    assert squash(tool.APP_BUTTONS_QUERY) == squash(APP_BUTTONS_QUERY)


def test_writes_the_new_tables(tmp_path):
    db = _database(tmp_path)
    assert _run(db)[0] == 0
    schema = {name: sql for name, sql in _query(db, "SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
    assert re.search(r"\)\s*WITHOUT ROWID$", schema["Tooltips"]) and re.search(r"\)\s*WITHOUT ROWID$",
                                                                                schema["TooltipButtons"])
    assert "WITHOUT ROWID" not in schema["TooltipTexts"] and "AUTOINCREMENT" in schema["TooltipTexts"]
    assert [r[1] for r in _query(db, "PRAGMA table_info(Tooltips)")] == ["id", "categoryId", "tag"]
    assert [r[1] for r in _query(db, "PRAGMA table_info(TooltipButtons)")] == list(tool.NEW_BUTTON_COLUMNS)
    # The UNIQUE (categoryId, tag) index is gone, and nothing else indexes the tooltip tables.
    assert _query(db, "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name LIKE 'Tooltip%' "
                      "AND name != 'sqlite_autoindex_TooltipButtonNumbers_1'") == []
    assert _query(db, "SELECT major, minor, patch, who FROM DocumentationDatabaseVersion") == [(3, 0, 0, "ADFA-3305")]
    assert _query(db, "PRAGMA integrity_check") == [("ok",)]
    assert _query(db, "PRAGMA foreign_key_check") == []
    # The old sequence carries on in TooltipTexts: 99999 was handed out and deleted, so it is not handed out again.
    assert _query(db, "SELECT name, seq FROM sqlite_sequence WHERE name LIKE 'Tooltip%'") == [("TooltipTexts", 99999)]
    assert _query(db, "SELECT COUNT(*) FROM Bookshelf") == [(0,)]


def test_numbers_descriptions_most_used_first_and_uris_in_text_order(tmp_path):
    db = _database(tmp_path)
    assert _run(db)[0] == 0
    counts = dict(_query(db, "SELECT D.description, COUNT(*) FROM TooltipButtons B "
                             "JOIN TooltipButtonDescriptions D ON D.id = B.descriptionId GROUP BY D.id"))
    descriptions = [d for (d,) in _query(db, "SELECT description FROM TooltipButtonDescriptions ORDER BY id")]
    assert descriptions == sorted(counts, key=lambda d: (-counts[d], d))
    assert descriptions[0] == "View full documentation"
    uris = [u for (u,) in _query(db, "SELECT uri FROM TooltipButtonUris ORDER BY id")]
    assert uris == sorted(uris) and len(uris) == len(set(uris))
    assert len(descriptions) == len(set(descriptions)) == 3


def test_the_app_finds_tooltips_and_buttons_by_key(tmp_path):
    db = _database(tmp_path)
    assert _run(db)[0] == 0
    plan = _plan(db, APP_TOOLTIP_QUERY, ("java", "tag2"))
    assert any(re.search(r"USING PRIMARY KEY \(categoryId=\? AND tag=\?\)", p) for p in plan), plan
    assert any(re.search(r"\bX\b.*USING INTEGER PRIMARY KEY", p) for p in plan), plan
    assert not any(re.search(r"\bSCAN (TABLE )?(T|X)\b", p) for p in plan), plan
    plan = _plan(db, APP_BUTTONS_QUERY, (1003,))
    assert any(re.search(r"USING PRIMARY KEY \(tooltipId=\?\)", p) for p in plan), plan
    assert not any("TEMP B-TREE" in p or p.startswith("SCAN") for p in plan), plan


def test_a_second_run_changes_nothing(tmp_path):
    db = _database(tmp_path)
    assert _run(db)[0] == 0
    for backup in _backups(db):
        backup.unlink()
    sha = _sha(db)
    code, out, _err = _run(db)
    assert code == 0 and "already in the new shape; nothing to do" in out
    assert _sha(db) == sha and _backups(db) == []


def test_a_dry_run_converts_and_checks_but_writes_nothing(tmp_path):
    db = _database(tmp_path)
    sha = _sha(db)
    code, out, _err = _run(db, dry_run=True)
    assert code == 0 and "read back identically" in out and "Dry run: nothing written." in out
    assert _sha(db) == sha and _backups(db) == []


def test_backs_up_the_database_before_writing(tmp_path):
    db = _database(tmp_path)
    before = _old_shown(db)
    assert _run(db)[0] == 0
    backups = _backups(db)
    assert len(backups) == 1 and _old_shown(backups[0]) == before


def test_drops_the_buttons_index_the_new_key_replaces(tmp_path):
    db = _database(tmp_path, OLD_SCHEMA + "CREATE INDEX TooltipButtonsByTooltip ON TooltipButtons "
                                          "(tooltipId, buttonNumberId);")
    code, out, err = _run(db)
    assert code == 0, err
    assert "dropped index TooltipButtonsByTooltip" in out
    assert _query(db, "SELECT name FROM sqlite_master WHERE name = 'TooltipButtonsByTooltip'") == []


def test_converts_empty_tooltip_tables(tmp_path):
    db = _database(tmp_path, tooltips=0)
    assert _run(db)[0] == 0
    assert _query(db, "SELECT COUNT(*) FROM Tooltips") == [(0,)]
    assert _query(db, "SELECT seq FROM sqlite_sequence WHERE name = 'TooltipTexts'") == [(99999,)]


def test_a_table_without_autoincrement_starts_the_sequence_after_its_largest_id(tmp_path):
    db = _database(tmp_path, OLD_SCHEMA.replace("id INTEGER PRIMARY KEY AUTOINCREMENT,\n categoryId",
                                                "id INTEGER PRIMARY KEY,\n categoryId"))
    before = _old_shown(db)
    assert _run(db)[0] == 0
    assert _new_shown(db) == before
    assert _query(db, "SELECT seq FROM sqlite_sequence WHERE name = 'TooltipTexts'") == [(1000 + 3 * 59,)]


def _assert_refused(tmp_path, phrase: str, sql: str = "", schema: str = OLD_SCHEMA, major=2):
    db = _database(tmp_path, schema, major)
    if sql:
        conn = sqlite3.connect(db)
        conn.executescript(sql)
        conn.close()
    sha = _sha(db)
    code, _out, err = _run(db)
    assert code == 1, err
    assert phrase in err, err
    assert _sha(db) == sha and _backups(db) == []


def test_refuses_a_button_without_description(tmp_path):
    _assert_refused(tmp_path, "1 button(s) with a NULL description or uri",
                    "UPDATE TooltipButtons SET description = NULL WHERE rowid = 1")


def test_refuses_a_button_without_uri(tmp_path):
    _assert_refused(tmp_path, "1 button(s) with a NULL description or uri",
                    "UPDATE TooltipButtons SET uri = NULL WHERE rowid = 2")


def test_refuses_a_button_without_tooltip_id(tmp_path):
    _assert_refused(tmp_path, "1 button(s) with a NULL tooltipId or buttonNumberId",
                    "UPDATE TooltipButtons SET tooltipId = NULL WHERE rowid = 1")


def test_refuses_a_button_without_number(tmp_path):
    _assert_refused(tmp_path, "1 button(s) with a NULL tooltipId or buttonNumberId",
                    "UPDATE TooltipButtons SET buttonNumberId = NULL WHERE rowid = 1")


def test_refuses_a_button_whose_tooltip_is_gone(tmp_path):
    _assert_refused(tmp_path, "1 button(s) whose tooltipId names no tooltip",
                    "INSERT INTO TooltipButtons VALUES (5, 1, 'x', 'y')")


def test_refuses_two_buttons_with_one_number(tmp_path):
    _assert_refused(tmp_path, "1 (tooltipId, buttonNumberId) pair(s) held by more than one button",
                    "INSERT INTO TooltipButtons VALUES (1003, 1, 'x', 'y')")


def test_refuses_two_tooltips_with_one_key(tmp_path):
    _assert_refused(tmp_path, "1 (categoryId, tag) pair(s) held by more than one tooltip",
                    "INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (2, 'tag0', 's', 'd')",
                    OLD_SCHEMA.replace(" UNIQUE (categoryId, tag),\n", ""))


def test_refuses_two_tooltips_with_one_id(tmp_path):
    _assert_refused(tmp_path, "1 tooltip id(s) held by more than one tooltip, or NULL",
                    "INSERT INTO Tooltips VALUES (1000, 1, 'other', 's', 'd')",
                    OLD_SCHEMA.replace("id INTEGER PRIMARY KEY AUTOINCREMENT,", "id INTEGER,"))


def test_refuses_a_tooltip_without_id(tmp_path):
    _assert_refused(tmp_path, "1 tooltip id(s) held by more than one tooltip, or NULL",
                    "INSERT INTO Tooltips VALUES (NULL, 1, 'other', 's', 'd')",
                    OLD_SCHEMA.replace("id INTEGER PRIMARY KEY AUTOINCREMENT,", "id INTEGER,"))


def test_refuses_a_tooltip_without_text(tmp_path):
    _assert_refused(tmp_path, "1 tooltip(s) with a NULL categoryId, tag, summary or detail",
                    "INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (1, 'other', 's', NULL)",
                    OLD_SCHEMA.replace("detail TEXT NOT NULL,", "detail TEXT,"))


def test_refuses_a_database_declaring_version_1(tmp_path):
    _assert_refused(tmp_path, "DocumentationDatabaseVersion declares major 1; this step converts 2 to 3", major=1)


def test_refuses_a_database_declaring_version_3_with_old_tables(tmp_path):
    _assert_refused(tmp_path, "DocumentationDatabaseVersion declares major 3; this step converts 2 to 3", major=3)


def test_refuses_a_database_without_a_version(tmp_path):
    _assert_refused(tmp_path, "DocumentationDatabaseVersion is empty", major=None)


def test_refuses_a_database_without_the_version_table(tmp_path):
    _assert_refused(tmp_path, "no DocumentationDatabaseVersion table", "DROP TABLE DocumentationDatabaseVersion")


def test_reads_the_newest_version_row_as_the_app_does(tmp_path):
    _assert_refused(tmp_path, "declares major 1",
                    "INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment, changeTime) "
                    "VALUES (1, 0, 0, 'later', 'downgrade', '2999-01-01')")


def test_refuses_another_index_on_the_tooltip_tables(tmp_path):
    _assert_refused(tmp_path, "index TooltipsBySummary on Tooltips, which the conversion would drop",
                    "CREATE INDEX TooltipsBySummary ON Tooltips (summary)")


def test_refuses_a_trigger_on_the_tooltip_tables(tmp_path):
    _assert_refused(tmp_path, "trigger Stamp uses the tooltip tables",
                    "CREATE TRIGGER Stamp AFTER INSERT ON TooltipButtons BEGIN SELECT 1; END")


def test_refuses_a_view_of_the_tooltip_tables(tmp_path):
    _assert_refused(tmp_path, "view Flat uses the tooltip tables",
                    "CREATE VIEW Flat AS SELECT tag, description FROM Tooltips JOIN TooltipButtons ON tooltipId = id")


def test_refuses_tables_in_neither_shape(tmp_path):
    _assert_refused(tmp_path, "in neither the old nor the new shape",
                    "ALTER TABLE TooltipButtons ADD COLUMN extra TEXT")


def test_refuses_a_half_converted_database(tmp_path):
    _assert_refused(tmp_path, "in neither the old nor the new shape",
                    "CREATE TABLE TooltipButtonUris (id INTEGER PRIMARY KEY, uri TEXT NOT NULL)")


def test_refuses_a_database_without_tooltips(tmp_path):
    _assert_refused(tmp_path, "no Tooltips or no TooltipButtons table", "DROP TABLE TooltipButtons")


def test_refuses_a_missing_file(tmp_path):
    code, _out, err = _run(tmp_path / "nothing.db")
    assert code == 1 and "does not exist" in err
    assert not (tmp_path / "nothing.db").exists()


def _with_fill(index: int, old: str, new: str):
    """A context in which step `index` of the conversion is rewritten, standing in for a conversion bug."""
    @contextlib.contextmanager
    def patched():
        original = tool.FILL
        assert old in original[index]
        tool.FILL = original[:index] + (original[index].replace(old, new),) + original[index + 1:]
        try:
            yield
        finally:
            tool.FILL = original
    return patched()


def _assert_rolled_back(tmp_path, patch, phrase: str):
    db = _database(tmp_path)
    before = _old_shown(db)
    with patch:
        code, _out, err = _run(db)
    assert code == 1 and phrase in err and "rolled back, nothing was changed" in err, err
    assert _old_shown(db) == before
    assert _query(db, "SELECT name FROM sqlite_master WHERE name IN ('TooltipTexts', 'TooltipButtonUris')") == []


def test_rolls_back_a_conversion_that_changes_a_tooltip(tmp_path):
    _assert_rolled_back(tmp_path, _with_fill(0, "SELECT id, summary, detail", "SELECT id, summary || '!', detail"),
                        "reads back differently")


def test_rolls_back_a_conversion_that_loses_a_tooltip(tmp_path):
    _assert_rolled_back(tmp_path, _with_fill(1, "FROM Tooltips_old ORDER BY", "FROM Tooltips_old WHERE id != 1000 "
                                                                              "ORDER BY"),
                        "reads back as 0 row(s)")


def test_rolls_back_a_conversion_that_loses_a_button(tmp_path):
    _assert_rolled_back(tmp_path, _with_fill(6, "ORDER BY b.tooltipId", "WHERE b.buttonNumberId != 3 ORDER BY "
                                                                        "b.tooltipId"),
                        "reads back differently")


def test_rolls_back_a_conversion_that_reorders_buttons(tmp_path):
    _assert_rolled_back(tmp_path, _with_fill(6, "SELECT b.tooltipId, b.buttonNumberId,",
                                             "SELECT b.tooltipId, 4 - b.buttonNumberId,"),
                        "reads back differently")


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_migrate_tooltip_schema.py`)."""
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
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): "
                                f"{type(exc).__name__}: {exc}")
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
