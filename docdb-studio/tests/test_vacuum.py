"""Tests for vacuum_database and the DELETE helpers that invoke it.

Per project policy, every helper that issues a DELETE must vacuum afterwards
so freed pages are reclaimed immediately. These tests cover the helper itself
and one end-to-end shrinkage check per delete-bearing helper."""

import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest

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


def _make_tooltip_db(
    seed_filler_rows: int = 0, starting_page_size: int | None = None
) -> Path:
    """Tooltip-shaped temp DB with optional seed rows to inflate the file.

    `starting_page_size`, when given, is set via PRAGMA before any table is
    created — page_size only takes effect on an empty database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        if starting_page_size is not None:
            conn.execute(f"PRAGMA page_size={starting_page_size}")
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
    """Real production DBs start at page_size=1024 (ADFA-5141); exercise that
    actual 1024 -> 2048 growth, not just an already-larger compiled default."""
    db = _make_tooltip_db(seed_filler_rows=10, starting_page_size=1024)
    try:
        with sqlite3.connect(db) as conn:
            (page_size_before,) = conn.execute("PRAGMA page_size").fetchone()
        assert page_size_before == 1024
        vacuum_database(db)
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
    finally:
        db.unlink(missing_ok=True)


def test_vacuum_database_migrates_page_size_under_wal() -> None:
    """PRAGMA page_size silently fails to take effect on VACUUM under WAL
    journal mode; vacuum_database must work around it."""
    db = _make_tooltip_db(seed_filler_rows=10, starting_page_size=1024)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        vacuum_database(db)
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
            (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
        assert journal_mode.lower() == "wal"
    finally:
        db.unlink(missing_ok=True)


def test_vacuum_database_succeeds_with_other_connections_still_open() -> None:
    """The in-place VACUUM + journal_mode round-trip this replaced required
    exclusive access to db_path -- SQLite refuses to switch a WAL-mode db away
    from WAL while any other connection has it open. That made vacuum_database
    fragile against anything else in the app holding db_path open (e.g. a live
    data browser), not just the caller's own unclosed connection. VACUUM INTO
    only needs a read snapshot of the source, so this must succeed even with
    both an unrelated open connection and an unclosed caller-style connection
    still around."""
    db = _make_tooltip_db(seed_filler_rows=10, starting_page_size=1024)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")

        # An unrelated, still-open connection with a live read outstanding --
        # e.g. a UI data browser left open while an import runs elsewhere.
        browser_conn = sqlite3.connect(db)
        browser_conn.execute("SELECT * FROM Tooltips")

        # A second connection that wrote+committed and is deliberately left
        # unclosed, mirroring a caller that didn't close its own connection.
        writer_conn = sqlite3.connect(db)
        writer_conn.execute(
            "INSERT INTO Tooltips (categoryId, tag, summary, detail) VALUES (?, ?, ?, ?)",
            (1, "extra", "s", "d"),
        )
        writer_conn.commit()

        try:
            vacuum_database(db)  # must not raise "database is locked"
        finally:
            browser_conn.close()
            writer_conn.close()

        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
            (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            (count,) = conn.execute("SELECT count(*) FROM Tooltips").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
        assert journal_mode.lower() == "wal"
        assert count == 11  # 10 seeded + 1 from writer_conn
    finally:
        db.unlink(missing_ok=True)


class _FailOnVacuumIntoConn:
    """Wraps a real sqlite3.Connection, raising on VACUUM INTO. sqlite3.Connection
    is a C type and refuses attribute assignment (even per-instance), so this
    proxies everything else through to a genuine connection instead."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith("VACUUM INTO"):
            raise sqlite3.OperationalError("simulated vacuum-into failure")
        return self._real.execute(sql, *args, **kwargs)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._real.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_vacuum_database_leaves_original_untouched_if_vacuum_into_fails() -> None:
    """If VACUUM INTO fails partway, the original db_path must be left
    completely unchanged (content, page_size, journal_mode) -- vacuum_database
    only ever swaps in a fully-built replacement, never edits db_path in place
    -- and the temp file must not be left behind."""
    db = _make_tooltip_db(seed_filler_rows=10, starting_page_size=1024)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        size_before = db.stat().st_size

        real_connect = sqlite3.connect
        with mock.patch("docdb_studio.sqlite3.connect") as mock_connect:
            mock_connect.side_effect = lambda *a, **k: _FailOnVacuumIntoConn(
                real_connect(*a, **k)
            )
            with pytest.raises(sqlite3.OperationalError, match="simulated vacuum-into failure"):
                vacuum_database(db)

        assert db.stat().st_size == size_before
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
            (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert page_size == 1024
        assert journal_mode.lower() == "wal"
        leftover_tmp = list(db.parent.glob(f"{db.name}*.vacuum.tmp"))
        assert leftover_tmp == []
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


def test_delete_tooltips_bulk_no_op_still_migrates_page_size() -> None:
    """A bulk-delete call where nothing actually matched (deleted == 0) must not
    skip the ADFA-5141 page_size migration just because it never satisfies the
    `if deleted:` DB-hygiene gate -- the same gap fixed for the import paths."""
    db = _make_tooltip_db(seed_filler_rows=10, starting_page_size=1024)
    try:
        deleted = delete_tooltips_bulk(db, [999999])
        assert deleted == 0
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
    finally:
        db.unlink(missing_ok=True)


def test_delete_tooltips_bulk_under_wal_does_not_deadlock() -> None:
    """SQLite refuses to switch a WAL-mode db away from WAL while any other
    connection (even one that already committed) is still open; delete_tooltips_bulk
    must close its own connection before the triggered vacuum_database runs."""
    db = _make_tooltip_db(seed_filler_rows=10)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            ids = [
                row[0] for row in conn.execute("SELECT id FROM Tooltips").fetchall()
            ]
        conn.close()
        deleted = delete_tooltips_bulk(db, ids[:5])
        assert deleted == 5
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


def test_delete_tooltip_button_under_wal_does_not_deadlock() -> None:
    """SQLite refuses to switch a WAL-mode db away from WAL while any other
    connection (even one that already committed) is still open;
    delete_tooltip_button must close its own connection before the
    unconditionally-triggered vacuum_database runs."""
    db = _make_tooltip_db(seed_filler_rows=10)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES (?, ?, ?, ?, ?)",
                (9000, 1, "with-button", "s", "d"),
            )
            conn.execute(
                "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (?, ?, ?, ?)",
                (9000, 1, "desc", "uri.html"),
            )
            conn.commit()
        conn.close()
        delete_tooltip_button(db, 9000, 1)  # must not raise
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


def test_replace_tooltip_buttons_under_wal_does_not_deadlock() -> None:
    """SQLite refuses to switch a WAL-mode db away from WAL while any other
    connection (even one that already committed) is still open;
    replace_tooltip_buttons must close its own connection before the
    unconditionally-triggered vacuum_database runs."""
    db = _make_tooltip_db(seed_filler_rows=10)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES (?, ?, ?, ?, ?)",
                (9001, 1, "replace-me", "s", "d"),
            )
            conn.commit()
        conn.close()
        replace_tooltip_buttons(db, 9001, [(1, "new1", "new1.html")])  # must not raise
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


def test_import_csv_rows_pure_insert_still_migrates_page_size() -> None:
    """A pure-insert import (no rows updated) must not skip the ADFA-5141
    page_size migration just because it never satisfies the DB-hygiene
    delete/overwrite gate."""
    db = _make_tooltip_db(seed_filler_rows=10, starting_page_size=1024)
    try:
        rows = [
            ["ide", "brand-new", "summary", "detail", "", "", "", "", "", ""],
        ]
        inserted, updated = import_csv_rows(db, rows, {"ide": 1}, mode="insert")
        assert (inserted, updated) == (1, 0)
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
    finally:
        db.unlink(missing_ok=True)


def test_import_csv_rows_pure_insert_migrates_page_size_under_wal() -> None:
    """The migration-triggered vacuum_database must not deadlock: SQLite refuses
    to switch a WAL-mode db away from WAL while any other connection (even one
    that already committed) is still open, so every mutating helper that can
    trigger vacuum_database must close its own connection first."""
    db = _make_tooltip_db(seed_filler_rows=10, starting_page_size=1024)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        rows = [
            ["ide", "brand-new", "summary", "detail", "", "", "", "", "", ""],
        ]
        inserted, updated = import_csv_rows(db, rows, {"ide": 1}, mode="insert")
        assert (inserted, updated) == (1, 0)
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
    finally:
        db.unlink(missing_ok=True)


def test_page_size_migration_pending_is_cached_once_confirmed() -> None:
    """Once a DB is confirmed at SQLITE_PAGE_SIZE_BYTES, _page_size_migration_pending
    must not keep reopening a connection to re-check on every call -- that would
    add overhead (and busy-timeout risk) to every import forever, long after the
    one-time migration is done."""
    db = _make_tooltip_db(seed_filler_rows=10, starting_page_size=1024)
    try:
        docdb_studio._page_size_confirmed.discard(Path(db))
        assert docdb_studio._page_size_migration_pending(db) is True
        vacuum_database(db)
        assert Path(db) in docdb_studio._page_size_confirmed

        real_connect = sqlite3.connect
        with mock.patch("docdb_studio.sqlite3.connect") as mock_connect:
            mock_connect.side_effect = real_connect
            assert docdb_studio._page_size_migration_pending(db) is False
            mock_connect.assert_not_called()
    finally:
        docdb_studio._page_size_confirmed.discard(Path(db))
        db.unlink(missing_ok=True)


# ---------- import_content_files (orphan delete) ----------


def _make_content_db(starting_page_size: int | None = None) -> Path:
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        if starting_page_size is not None:
            conn.execute(f"PRAGMA page_size={starting_page_size}")
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


def test_import_content_files_pure_insert_still_migrates_page_size(
    tmp_path: Path,
) -> None:
    """A pure-insert import (no orphans, no overwrites) must not skip the
    ADFA-5141 page_size migration just because it never satisfies the
    DB-hygiene delete/overwrite gate."""
    db = _make_content_db(starting_page_size=1024)
    try:
        new_file = tmp_path / "docs" / "new.html"
        new_file.parent.mkdir()
        new_file.write_bytes(b"hello")
        content_types = get_content_types(db)
        scan = prescan_content_import(db, tmp_path / "docs", content_types)
        assert not scan.orphan_row_ids
        assert not scan.overwrite_row_ids_by_base

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
        assert summary.orphans_deleted == 0
        assert summary.files_overwritten == 0
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
    finally:
        db.unlink(missing_ok=True)


def test_import_content_files_pure_insert_migrates_page_size_under_wal(
    tmp_path: Path,
) -> None:
    """SQLite refuses to switch a WAL-mode db away from WAL while any other
    connection (even one that already committed) is still open;
    import_content_files must close its own connection before the migration
    -triggered vacuum_database runs."""
    db = _make_content_db(starting_page_size=1024)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        new_file = tmp_path / "docs" / "new.html"
        new_file.parent.mkdir()
        new_file.write_bytes(b"hello")
        content_types = get_content_types(db)
        scan = prescan_content_import(db, tmp_path / "docs", content_types)

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
        assert summary.files_imported == 1
        with sqlite3.connect(db) as conn:
            (page_size,) = conn.execute("PRAGMA page_size").fetchone()
        assert page_size == docdb_studio.SQLITE_PAGE_SIZE_BYTES
    finally:
        db.unlink(missing_ok=True)


def test_import_content_files_vacuum_phase_is_reported() -> None:
    """Delete-bearing import fires the vacuum phase (a pure-insert import also
    fires it once, to migrate page_size -- see
    test_import_content_files_pure_insert_still_migrates_page_size)."""
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
