"""Tests for get_category_name and update_last_change (LastChange automation)."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

import docdb_studio

get_category_name = docdb_studio.get_category_name
update_last_change = docdb_studio.update_last_change


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
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "tooltips-ide"
        assert rows[0][1] is not None  # changeTime set
        assert rows[0][2] == "Alice"
    finally:
        db.unlink(missing_ok=True)


def test_update_last_change_updates_existing_row() -> None:
    db = _make_db()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                INSERT INTO LastChange (documentationSet, changeTime, who)
                VALUES ('tooltips-java', '2020-01-01 00:00:00', 'Bob')
                """
            )
            conn.commit()
        update_last_change(db, "tooltips-java", "Carol")
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT documentationSet, changeTime, who FROM LastChange"
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "tooltips-java"
        assert rows[0][1] != "2020-01-01 00:00:00"
        assert rows[0][2] == "Carol"
    finally:
        db.unlink(missing_ok=True)


def test_update_last_change_accepts_none_who() -> None:
    db = _make_db()
    try:
        update_last_change(db, "tooltips-ide", None)
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT documentationSet, changeTime, who FROM LastChange"
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][2] is None
    finally:
        db.unlink(missing_ok=True)
