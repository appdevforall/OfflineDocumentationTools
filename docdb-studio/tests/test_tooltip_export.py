"""Tests for CSV tooltip export, extended search, and the export/import round-trip."""

import csv
import io
import sqlite3
import tempfile
from pathlib import Path

import docdb_studio

get_tooltip_export_rows = docdb_studio.get_tooltip_export_rows
get_total_count = docdb_studio.get_total_count
get_page = docdb_studio.get_page
get_categories = docdb_studio.get_categories
get_button_number_ids = docdb_studio.get_button_number_ids
validate_csv_upload = docdb_studio.validate_csv_upload
import_csv_rows = docdb_studio.import_csv_rows
CSV_TEMPLATE_HEADERS = docdb_studio.CSV_TEMPLATE_HEADERS


def _make_db() -> Path:
    """Temp DB with the tooltip tables and a mix of buttoned / button-less rows."""
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
                (1, 'ide'), (2, 'code actions');
            INSERT INTO TooltipButtonNumbers (id) VALUES (1), (2), (3);
            INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES
                (10, 1, 'rename', 'Rename symbol', 'Rename detail'),
                (11, 1, 'noButtons', 'No buttons here', 'plain detail'),
                (12, 2, 'quickfix', 'Quick fix', 'quickfix detail');
            INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES
                (10, 1, 'Docs', 'ide/refactor/rename.html'),
                (10, 2, 'More', 'ide/refactor/rename2.html'),
                (12, 1, 'Fix docs', 'code/quickfix.html');
            """
        )
        conn.commit()
    return p


def _roundtrip_through_csv(rows: list[list[str]]) -> list[list[str]]:
    """Write rows (with header) to CSV text and read them back, as the UI does."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_TEMPLATE_HEADERS)
    writer.writerows(rows)
    reader = csv.reader(io.StringIO(buf.getvalue()))
    all_rows = list(reader)
    return all_rows[1:]  # drop header


# --- export shaping ---------------------------------------------------------


def test_export_all_returns_every_tooltip_in_template_shape() -> None:
    db = _make_db()
    try:
        rows = get_tooltip_export_rows(db)
        assert len(rows) == 3
        assert all(len(r) == len(CSV_TEMPLATE_HEADERS) for r in rows)
        # Ordered by category, tag: 'code actions'/quickfix, 'ide'/noButtons, 'ide'/rename
        assert [r[0] + "/" + r[1] for r in rows] == [
            "code actions/quickfix",
            "ide/noButtons",
            "ide/rename",
        ]
    finally:
        db.unlink(missing_ok=True)


def test_export_slots_buttons_by_button_number() -> None:
    db = _make_db()
    try:
        rows = {r[1]: r for r in get_tooltip_export_rows(db)}
        rename = rows["rename"]
        # button1 label/uri, button2 label/uri, button3 empty
        assert rename[4:] == [
            "Docs", "ide/refactor/rename.html",
            "More", "ide/refactor/rename2.html",
            "", "",
        ]
    finally:
        db.unlink(missing_ok=True)


def test_export_tooltip_without_buttons_has_empty_button_cells() -> None:
    db = _make_db()
    try:
        rows = {r[1]: r for r in get_tooltip_export_rows(db)}
        assert rows["noButtons"][4:] == ["", "", "", "", "", ""]
    finally:
        db.unlink(missing_ok=True)


def test_export_respects_active_search_subset() -> None:
    db = _make_db()
    try:
        # Category search: only the 'code actions' tooltip.
        rows = get_tooltip_export_rows(db, "code actions")
        assert [r[1] for r in rows] == ["quickfix"]
    finally:
        db.unlink(missing_ok=True)


def test_export_filter_by_uri_substring() -> None:
    db = _make_db()
    try:
        rows = get_tooltip_export_rows(db, "*ide/refactor/rename")
        assert [r[1] for r in rows] == ["rename"]
    finally:
        db.unlink(missing_ok=True)


# --- extended search --------------------------------------------------------


def test_search_matches_category() -> None:
    db = _make_db()
    try:
        assert get_total_count(db, "code actions") == 1
        assert get_page(db, 50, 0, "code actions")[0][2] == "quickfix"
    finally:
        db.unlink(missing_ok=True)


def test_search_matches_button_uri() -> None:
    db = _make_db()
    try:
        assert get_total_count(db, "*ide/refactor/rename") == 1
    finally:
        db.unlink(missing_ok=True)


def test_search_matches_uri_substring_without_wildcard() -> None:
    """A bare term (no leading '*') still matches a URI fragment in the middle:
    'rename.html' finds ide/refactor/rename.html even though the URI does not
    start with it."""
    db = _make_db()
    try:
        assert get_total_count(db, "refactor/rename") == 1
        assert get_page(db, 50, 0, "refactor/rename")[0][2] == "rename"
    finally:
        db.unlink(missing_ok=True)


def test_search_on_uri_does_not_duplicate_multibutton_tooltip() -> None:
    db = _make_db()
    try:
        # 'rename' has two matching buttons; it must appear exactly once.
        assert get_total_count(db, "*ide/refactor") == 1
        rows = get_page(db, 50, 0, "*ide/refactor")
        assert len(rows) == 1
    finally:
        db.unlink(missing_ok=True)


# --- invariant filtering: never export an unimportable row ------------------


def test_export_excludes_empty_summary() -> None:
    db = _make_db()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO Tooltips (id, categoryId, tag, summary, detail) "
                "VALUES (99, 1, 'blankSummary', '', 'has detail')"
            )
            conn.commit()
        tags = [r[1] for r in get_tooltip_export_rows(db)]
        assert "blankSummary" not in tags
        # It still counts as a matching tooltip, so the UI can report it skipped.
        assert get_total_count(db) - len(get_tooltip_export_rows(db)) == 1
    finally:
        db.unlink(missing_ok=True)


def test_export_excludes_newline_in_summary() -> None:
    db = _make_db()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO Tooltips (id, categoryId, tag, summary, detail) "
                "VALUES (98, 1, 'nlSummary', 'line one\nline two', '')"
            )
            conn.commit()
        tags = [r[1] for r in get_tooltip_export_rows(db)]
        assert "nlSummary" not in tags
    finally:
        db.unlink(missing_ok=True)


def test_find_empty_summary_tooltips_flags_blank_and_whitespace() -> None:
    db = _make_db()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO Tooltips (id, categoryId, tag, summary, detail) VALUES "
                "(99, 1, 'blank', '', 'd'), (98, 1, 'spaces', '   ', 'd')"
            )
            conn.commit()
        found = {tag for _id, tag in docdb_studio.find_empty_summary_tooltips(db)}
        assert found == {"blank", "spaces"}
    finally:
        db.unlink(missing_ok=True)


def test_every_exported_row_passes_import_validation() -> None:
    """Drift guard: whatever the export emits must satisfy the importer."""
    db = _make_db()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO Tooltips (id, categoryId, tag, summary, detail) "
                "VALUES (97, 1, 'blank', '', '')"
            )
            conn.commit()
        rows = get_tooltip_export_rows(db)
        cat_map = {name: cid for cid, name in get_categories(db)}
        allowed = set(get_button_number_ids(db))
        assert validate_csv_upload(db, rows, cat_map, allowed, mode="update") == []
    finally:
        db.unlink(missing_ok=True)


# --- the important one: full round-trip -------------------------------------


def _dump(db: Path) -> tuple[list, list]:
    with sqlite3.connect(db) as conn:
        tips = conn.execute(
            "SELECT categoryId, tag, summary, detail FROM Tooltips ORDER BY categoryId, tag"
        ).fetchall()
        btns = conn.execute(
            "SELECT t.categoryId, t.tag, tb.buttonNumberId, tb.description, tb.uri "
            "FROM TooltipButtons tb JOIN Tooltips t ON tb.tooltipId = t.id "
            "ORDER BY t.categoryId, t.tag, tb.buttonNumberId"
        ).fetchall()
    return tips, btns


def test_export_reimports_unedited_without_changes() -> None:
    db = _make_db()
    try:
        before = _dump(db)
        rows = _roundtrip_through_csv(get_tooltip_export_rows(db))
        category_name_to_id = {name: cid for cid, name in get_categories(db)}
        allowed = set(get_button_number_ids(db))
        errors = validate_csv_upload(db, rows, category_name_to_id, allowed, mode="update")
        assert errors == []
        inserted, updated = import_csv_rows(db, rows, category_name_to_id, mode="update")
        assert inserted == 0
        assert updated == 3
        assert _dump(db) == before
    finally:
        db.unlink(missing_ok=True)


def test_export_edit_reimport_updates_only_edited_row() -> None:
    db = _make_db()
    try:
        rows = _roundtrip_through_csv(get_tooltip_export_rows(db))
        # Edit the summary of the 'rename' row (tag is column index 1).
        for r in rows:
            if r[1] == "rename":
                r[2] = "Rename symbol (edited)"
        category_name_to_id = {name: cid for cid, name in get_categories(db)}
        import_csv_rows(db, rows, category_name_to_id, mode="update")
        with sqlite3.connect(db) as conn:
            summaries = dict(
                conn.execute("SELECT tag, summary FROM Tooltips").fetchall()
            )
        assert summaries["rename"] == "Rename symbol (edited)"
        assert summaries["quickfix"] == "Quick fix"  # untouched
    finally:
        db.unlink(missing_ok=True)
