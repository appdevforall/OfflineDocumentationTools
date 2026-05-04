"""Tests for bulk-delete helpers and CSV update mode."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

import docdb_studio

delete_tooltips_bulk = docdb_studio.delete_tooltips_bulk
get_categories_for_tooltips = docdb_studio.get_categories_for_tooltips
validate_csv_upload = docdb_studio.validate_csv_upload
import_csv_rows = docdb_studio.import_csv_rows


def _make_db() -> Path:
    """Create a temp DB with the tooltip-related tables and a few seed rows."""
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.executescript(
            """
            CREATE TABLE TooltipCategories (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL
            );
            CREATE TABLE Tooltips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                categoryId INTEGER NOT NULL,
                tag TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail TEXT NOT NULL,
                UNIQUE (categoryId, tag),
                FOREIGN KEY (categoryId) REFERENCES TooltipCategories(id)
            );
            CREATE TABLE TooltipButtons (
                tooltipId INTEGER,
                buttonNumberId INTEGER,
                description TEXT,
                uri TEXT
            );
            CREATE TABLE TooltipButtonNumbers (id INTEGER UNIQUE);
            CREATE TABLE LastChange (
                documentationSet TEXT,
                changeTime TIMESTAMP,
                who TEXT
            );
            INSERT INTO TooltipCategories (id, category) VALUES
                (1, 'ide'), (2, 'java');
            INSERT INTO TooltipButtonNumbers (id) VALUES (1), (2), (3);
            INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES
                (10, 1, 'foo', 'foo summary', 'foo detail'),
                (11, 1, 'bar', 'bar summary', 'bar detail'),
                (12, 2, 'baz', 'baz summary', 'baz detail');
            INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES
                (10, 1, 'foo btn', 'foo.html'),
                (11, 1, 'bar btn', 'bar.html'),
                (12, 1, 'baz btn', 'baz.html');
            """
        )
        conn.commit()
    return p


def test_delete_tooltips_bulk_removes_rows_and_buttons() -> None:
    db = _make_db()
    try:
        deleted = delete_tooltips_bulk(db, [10, 12])
        assert deleted == 2
        with sqlite3.connect(db) as conn:
            tooltip_ids = {
                row[0] for row in conn.execute("SELECT id FROM Tooltips").fetchall()
            }
            button_tooltip_ids = {
                row[0]
                for row in conn.execute(
                    "SELECT tooltipId FROM TooltipButtons"
                ).fetchall()
            }
        assert tooltip_ids == {11}
        assert button_tooltip_ids == {11}
    finally:
        db.unlink(missing_ok=True)


def test_delete_tooltips_bulk_empty_list_is_noop() -> None:
    db = _make_db()
    try:
        assert delete_tooltips_bulk(db, []) == 0
        with sqlite3.connect(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM Tooltips").fetchone()[0]
        assert count == 3
    finally:
        db.unlink(missing_ok=True)


def test_get_categories_for_tooltips_returns_unique_names() -> None:
    db = _make_db()
    try:
        names = get_categories_for_tooltips(db, [10, 11, 12])
        assert names == {"ide", "java"}
        names_one = get_categories_for_tooltips(db, [10])
        assert names_one == {"ide"}
        assert get_categories_for_tooltips(db, []) == set()
    finally:
        db.unlink(missing_ok=True)


def test_validate_csv_upload_update_mode_accepts_existing_keys() -> None:
    db = _make_db()
    try:
        rows = [
            ["ide", "foo", "new summary", "new detail", "", "", "", "", "", ""],
            ["ide", "newone", "fresh", "fresh detail", "", "", "", "", "", ""],
        ]
        cat_map = {"ide": 1, "java": 2}
        button_ids = {1, 2, 3}

        # insert mode rejects pre-existing (ide, foo)
        errors_insert = validate_csv_upload(
            db, rows, cat_map, button_ids, mode="insert"
        )
        assert any("already exists" in e for e in errors_insert)

        # update mode accepts the same data
        errors_update = validate_csv_upload(
            db, rows, cat_map, button_ids, mode="update"
        )
        assert errors_update == []
    finally:
        db.unlink(missing_ok=True)


def test_validate_csv_upload_update_still_rejects_in_file_dupes() -> None:
    db = _make_db()
    try:
        rows = [
            ["ide", "foo", "s1", "d1", "", "", "", "", "", ""],
            ["ide", "foo", "s2", "d2", "", "", "", "", "", ""],
        ]
        errors = validate_csv_upload(
            db, rows, {"ide": 1, "java": 2}, {1, 2, 3}, mode="update"
        )
        assert any("duplicate (category, tag)" in e for e in errors)
    finally:
        db.unlink(missing_ok=True)


def test_import_csv_rows_update_mode_replaces_existing() -> None:
    db = _make_db()
    try:
        rows = [
            ["ide", "foo", "updated foo", "updated detail",
             "newlabel", "new.html", "", "", "", ""],
            ["ide", "newone", "fresh", "fresh detail", "", "", "", "", "", ""],
        ]
        inserted, updated = import_csv_rows(
            db, rows, {"ide": 1, "java": 2}, mode="update"
        )
        assert inserted == 1
        assert updated == 1

        with sqlite3.connect(db) as conn:
            foo_row = conn.execute(
                "SELECT id, summary, detail FROM Tooltips WHERE categoryId = 1 AND tag = 'foo'"
            ).fetchone()
            assert foo_row is not None
            foo_id, summary, detail = foo_row
            assert foo_id == 10  # same id, replaced in place
            assert summary == "updated foo"
            assert detail == "updated detail"

            # Old buttons gone, new button present.
            buttons = conn.execute(
                "SELECT description, uri FROM TooltipButtons WHERE tooltipId = ?",
                (foo_id,),
            ).fetchall()
            assert buttons == [("newlabel", "new.html")]

            # Newone was inserted.
            newone = conn.execute(
                "SELECT summary FROM Tooltips WHERE categoryId = 1 AND tag = 'newone'"
            ).fetchone()
            assert newone == ("fresh",)
    finally:
        db.unlink(missing_ok=True)


def test_import_csv_rows_insert_mode_returns_counts() -> None:
    db = _make_db()
    try:
        rows = [
            ["ide", "newtag", "s", "d", "", "", "", "", "", ""],
        ]
        inserted, updated = import_csv_rows(
            db, rows, {"ide": 1, "java": 2}, mode="insert"
        )
        assert inserted == 1
        assert updated == 0
    finally:
        db.unlink(missing_ok=True)


def test_import_csv_rows_rejects_unknown_mode() -> None:
    db = _make_db()
    try:
        with pytest.raises(ValueError):
            import_csv_rows(db, [], {}, mode="bogus")
    finally:
        db.unlink(missing_ok=True)
