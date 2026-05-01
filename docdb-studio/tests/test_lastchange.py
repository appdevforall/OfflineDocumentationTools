"""Tests for get_category_name and update_last_change (LastChange automation)."""

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

import docdb_studio

get_category_name = docdb_studio.get_category_name
update_last_change = docdb_studio.update_last_change
WHOLEDB_KEY = docdb_studio.WHOLEDB_KEY


def _make_db() -> Path:
    """Create a temp DB with TooltipCategories and LastChange."""
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.execute(
            """
            CREATE TABLE TooltipCategories (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE LastChange (
                documentationSet TEXT,
                changeTime TIMESTAMP,
                who TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO TooltipCategories (id, category) VALUES (1, 'ide'), (2, 'java')"
        )
        conn.commit()
    return p


def test_get_category_name_returns_name_for_valid_id() -> None:
    db = _make_db()
    try:
        assert get_category_name(db, 1) == "ide"
        assert get_category_name(db, 2) == "java"
    finally:
        db.unlink(missing_ok=True)


def test_get_category_name_returns_none_for_unknown_id() -> None:
    db = _make_db()
    try:
        assert get_category_name(db, 99) is None
    finally:
        db.unlink(missing_ok=True)


def test_update_last_change_inserts_when_no_row() -> None:
    db = _make_db()
    try:
        update_last_change(db, "tooltips-ide", "Alice")
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT documentationSet, changeTime, who FROM LastChange"
                " ORDER BY documentationSet"
            )
            rows = cur.fetchall()
        assert len(rows) == 2
        by_set = {r[0]: r for r in rows}
        assert "tooltips-ide" in by_set and WHOLEDB_KEY in by_set
        assert by_set["tooltips-ide"][1] is not None
        assert by_set["tooltips-ide"][2] == "Alice"
        assert by_set[WHOLEDB_KEY][1] == by_set["tooltips-ide"][1]
        assert by_set[WHOLEDB_KEY][2] == "Alice"
    finally:
        db.unlink(missing_ok=True)


def test_update_last_change_updates_existing_row() -> None:
    db = _make_db()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                INSERT INTO LastChange (documentationSet, changeTime, who)
                VALUES ('tooltips-java', '2020-01-01 00:00:00', 'Bob'),
                       ('wholedb',       '2020-01-01 00:00:00', 'Bob')
                """
            )
            conn.commit()
        update_last_change(db, "tooltips-java", "Carol")
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT documentationSet, changeTime, who FROM LastChange"
                " ORDER BY documentationSet"
            )
            rows = cur.fetchall()
        assert len(rows) == 2
        by_set = {r[0]: r for r in rows}
        assert by_set["tooltips-java"][1] != "2020-01-01 00:00:00"
        assert by_set["tooltips-java"][2] == "Carol"
        assert by_set[WHOLEDB_KEY][1] != "2020-01-01 00:00:00"
        assert by_set[WHOLEDB_KEY][2] == "Carol"
    finally:
        db.unlink(missing_ok=True)


def test_update_last_change_accepts_none_who() -> None:
    db = _make_db()
    try:
        update_last_change(db, "tooltips-ide", None)
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT documentationSet, who FROM LastChange"
            )
            rows = cur.fetchall()
        assert len(rows) == 2
        for _doc_set, who in rows:
            assert who is None
    finally:
        db.unlink(missing_ok=True)


def test_update_last_change_stamps_wholedb_alongside_set() -> None:
    db = _make_db()
    try:
        update_last_change(db, "content-api", "Alice")
        with sqlite3.connect(db) as conn:
            cur = conn.execute("SELECT documentationSet FROM LastChange")
            sets = {r[0] for r in cur.fetchall()}
        assert sets == {"content-api", WHOLEDB_KEY}
    finally:
        db.unlink(missing_ok=True)


def test_update_last_change_wholedb_key_is_idempotent() -> None:
    db = _make_db()
    try:
        update_last_change(db, WHOLEDB_KEY, "Alice")
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT documentationSet, who FROM LastChange"
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == WHOLEDB_KEY
        assert rows[0][1] == "Alice"
    finally:
        db.unlink(missing_ok=True)


def test_update_last_change_atomic_across_two_rows() -> None:
    db = _make_db()
    try:
        update_last_change(db, "tooltips-ide", "Alice")
        with sqlite3.connect(db) as conn:
            first = dict(
                conn.execute(
                    "SELECT documentationSet, changeTime FROM LastChange"
                ).fetchall()
            )
        # SQLite's datetime('now') has 1-second granularity, so sleep past
        # that boundary to guarantee the second stamp produces a new value.
        time.sleep(1.1)
        update_last_change(db, "tooltips-java", "Bob")
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT documentationSet, changeTime, who FROM LastChange"
                " ORDER BY documentationSet"
            )
            rows = cur.fetchall()
        by_set = {r[0]: r for r in rows}
        assert set(by_set.keys()) == {
            "tooltips-ide",
            "tooltips-java",
            WHOLEDB_KEY,
        }
        # tooltips-ide kept its original timestamp from the first call.
        assert by_set["tooltips-ide"][1] == first["tooltips-ide"]
        assert by_set["tooltips-ide"][2] == "Alice"
        # wholedb advanced to match the second call.
        assert by_set[WHOLEDB_KEY][1] == by_set["tooltips-java"][1]
        assert by_set[WHOLEDB_KEY][1] != first[WHOLEDB_KEY]
        assert by_set[WHOLEDB_KEY][2] == "Bob"
    finally:
        db.unlink(missing_ok=True)
