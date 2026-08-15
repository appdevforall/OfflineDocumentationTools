"""Tests for vacuum_database and the DELETE helpers that invoke it.

Per project policy, every helper that issues a DELETE must vacuum afterwards
so freed pages are reclaimed immediately. These tests cover the helper itself
and one end-to-end shrinkage check per delete-bearing helper."""

import sqlite3
import tempfile
from pathlib import Path

import docdb_studio

vacuum_database = docdb_studio.vacuum_database
delete_tooltips_bulk = docdb_studio.delete_tooltips_bulk
delete_tooltip_button = docdb_studio.delete_tooltip_button
replace_tooltip_buttons = docdb_studio.replace_tooltip_buttons
import_csv_rows = docdb_studio.import_csv_rows
import_content_files = docdb_studio.import_content_files
prescan_content_import = docdb_studio.prescan_content_import
get_content_types = docdb_studio.get_content_types
ImportItem = docdb_studio.ImportItem


def _make_tooltip_db(seed_filler_rows: int = 0) -> Path:
    """Tooltip-shaped temp DB with optional seed rows to inflate the file."""
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
            INSERT INTO TooltipCategories (id, category) VALUES (1, 'ide');
            INSERT INTO TooltipButtonNumbers (id) VALUES (1), (2), (3);
            """
        )
        # Big filler rows force the file past one page so a post-vacuum
        # size difference is observable.
        filler = "x" * 4096
        for i in range(seed_filler_rows):
            conn.execute(
                "INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (?, ?, ?, ?)",
                (1, f"tag{i}", filler, filler),
            )
        conn.commit()
    return p


def _schema_rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return sorted(
            conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('table','index')"
            ).fetchall()
        )


# ---------- vacuum_database itself ----------


def test_vacuum_database_runs_without_error_and_preserves_schema() -> None:
    db = _make_tooltip_db(seed_filler_rows=10)
    try:
        before_schema = _schema_rows(db)
        vacuum_database(db)
        after_schema = _schema_rows(db)
        assert before_schema == after_schema
    finally:
        db.unlink(missing_ok=True)


def test_vacuum_database_sets_target_page_size() -> None:
    db = _make_tooltip_db(seed_filler_rows=10)
    try:
        vacuum_database(db)
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
    finally:
        db.unlink(missing_ok=True)


def test_vacuum_database_shrinks_after_large_delete() -> None:
    db = _make_tooltip_db(seed_filler_rows=200)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM Tooltips")
            conn.commit()
        size_before_vacuum = db.stat().st_size
        vacuum_database(db)
        size_after_vacuum = db.stat().st_size
        assert size_after_vacuum < size_before_vacuum
    finally:
        db.unlink(missing_ok=True)


# ---------- delete_tooltips_bulk ----------


def test_delete_tooltips_bulk_shrinks_file() -> None:
    db = _make_tooltip_db(seed_filler_rows=200)
    try:
        with sqlite3.connect(db) as conn:
            ids = [
                row[0] for row in conn.execute("SELECT id FROM Tooltips").fetchall()
            ]
        size_before = db.stat().st_size
        delete_tooltips_bulk(db, ids[:150])
        size_after = db.stat().st_size
        assert size_after < size_before
    finally:
        db.unlink(missing_ok=True)


# ---------- delete_tooltip_button ----------


def test_delete_tooltip_button_does_not_grow_file() -> None:
    db = _make_tooltip_db(seed_filler_rows=10)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES (?, ?, ?, ?, ?)",
                (9000, 1, "with-button", "s", "d"),
            )
            conn.execute(
                "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (?, ?, ?, ?)",
                (9000, 1, "desc", "uri.html"),
            )
            conn.commit()
        size_before = db.stat().st_size
        delete_tooltip_button(db, 9000, 1)
        size_after = db.stat().st_size
        assert size_after <= size_before
    finally:
        db.unlink(missing_ok=True)


# ---------- replace_tooltip_buttons ----------


def test_replace_tooltip_buttons_does_not_grow_file() -> None:
    db = _make_tooltip_db(seed_filler_rows=10)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES (?, ?, ?, ?, ?)",
                (9001, 1, "replace-me", "s", "d"),
            )
            conn.executemany(
                "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (?, ?, ?, ?)",
                [(9001, i, f"old{i}", f"old{i}.html") for i in (1, 2, 3)],
            )
            conn.commit()
        size_before = db.stat().st_size
        replace_tooltip_buttons(
            db, 9001, [(1, "new1", "new1.html"), (2, "new2", "new2.html")]
        )
        size_after = db.stat().st_size
        assert size_after <= size_before
    finally:
        db.unlink(missing_ok=True)


# ---------- import_csv_rows (update mode) ----------


def test_import_csv_rows_update_mode_does_not_grow_file() -> None:
    db = _make_tooltip_db(seed_filler_rows=10)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (?, ?, ?, ?)",
                (1, "preexisting", "old summary", "old detail"),
            )
            conn.commit()
        size_before = db.stat().st_size
        rows = [
            ["ide", "preexisting", "new summary", "new detail", "", "", "", "", "", ""],
        ]
        inserted, updated = import_csv_rows(
            db, rows, {"ide": 1}, mode="update"
        )
        assert (inserted, updated) == (0, 1)
        size_after = db.stat().st_size
        assert size_after <= size_before
    finally:
        db.unlink(missing_ok=True)


# ---------- import_content_files (orphan delete) ----------


def _make_content_db() -> Path:
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.executescript(
            """
            CREATE TABLE Languages (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
            CREATE TABLE ContentTypes (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL UNIQUE,
                compression TEXT NOT NULL
            );
            CREATE TABLE "Content" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                languageID INTEGER NOT NULL,
                content BLOB NOT NULL,
                contentTypeID INTEGER NOT NULL,
                UNIQUE(path)
            );
            CREATE TABLE LastChange (
                documentationSet TEXT,
                changeTime TIMESTAMP,
                who TEXT
            );
            INSERT INTO Languages (id, value) VALUES (1, 'en');
            INSERT INTO ContentTypes (id, value, compression) VALUES
                (1, 'text/html', 'none');
            """
        )
        conn.commit()
    return p


def test_import_content_files_orphan_phase_shrinks_file(tmp_path: Path) -> None:
    db = _make_content_db()
    try:
        # Seed many large orphan rows under "docs/" so an empty import folder
        # named "docs" deletes all of them.
        big = b"x" * 50_000
        with sqlite3.connect(db) as conn:
            for i in range(200):
                conn.execute(
                    'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, ?, ?, ?)',
                    (f"docs/orphan{i}.html", 1, big, 1),
                )
            conn.commit()
        # Empty import folder named "docs" → every existing Content row is orphan.
        empty_docs = tmp_path / "docs"
        empty_docs.mkdir()
        content_types = get_content_types(db)
        scan = prescan_content_import(db, empty_docs, content_types)
        assert scan.orphan_row_ids, "expected orphans to be detected"

        size_before = db.stat().st_size
        summary = import_content_files(
            db,
            scan.mapped,
            language_id=1,
            user_name="tester",
            documentation_set="test",
            orphan_row_ids=scan.orphan_row_ids,
            overwrite_row_ids_by_base=scan.overwrite_row_ids_by_base,
            progress_callback=None,
        )
        size_after = db.stat().st_size
        assert summary.orphans_deleted == 200
        assert size_after < size_before
    finally:
        db.unlink(missing_ok=True)


def test_import_content_files_vacuum_phase_is_reported() -> None:
    """Pure-insert import skips VACUUM; delete-bearing import fires the phase."""
    db = _make_content_db()
    try:
        # Seed one row that the import will overwrite.
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, ?, ?, ?)',
                ("docs/page.html", 1, b"old", 1),
            )
            conn.commit()
        scan = type(
            "_S",
            (),
            {
                "mapped": [],
                "orphan_row_ids": [
                    row[0]
                    for row in sqlite3.connect(db)
                    .execute('SELECT id FROM "Content"')
                    .fetchall()
                ],
                "overwrite_row_ids_by_base": {},
            },
        )()
        seen_phases: list[str] = []

        def cb(phase: str, current: int, total: int) -> None:
            seen_phases.append(phase)

        import_content_files(
            db,
            scan.mapped,
            language_id=1,
            user_name="tester",
            documentation_set="test",
            orphan_row_ids=scan.orphan_row_ids,
            overwrite_row_ids_by_base=scan.overwrite_row_ids_by_base,
            progress_callback=cb,
        )
        assert "vacuum" in seen_phases
    finally:
        db.unlink(missing_ok=True)
