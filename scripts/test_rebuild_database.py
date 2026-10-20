#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "scour",
#     "pytest",
# ]
# ///
"""
test_rebuild_database.py

Covers rebuild_database.py: the copy drops the reserved bytes, keeps the page
size, and keeps everything a reader could see - schema, rows with their
rowids, sqlite_sequence, user_version, application_id - and a copy that
differs is deleted rather than kept.

The source database is made with the sqlite3 shell, the one way to give a
file reserved bytes, so it must be on PATH.

    uv run scripts/test_rebuild_database.py
"""
from __future__ import annotations

import contextlib
import io
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rebuild_database as rebuild  # noqa: E402
import test_media_bundle as base  # noqa: E402

BIG = bytes(range(256)) * 20  # spills onto overflow pages at 1 KB


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "documentation.db"
    subprocess.run(["sqlite3", str(path), "PRAGMA page_size=1024;", ".filectrl reserve_bytes 12", "VACUUM;"],
                   check=True, capture_output=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, content BLOB NOT NULL,
                              UNIQUE(path));
        CREATE TABLE TooltipButtons (tooltipId INTEGER, description TEXT,
                                     FOREIGN KEY(tooltipId) REFERENCES Content(id));
        CREATE INDEX buttons_by_tooltip ON TooltipButtons (tooltipId);
        CREATE VIEW paths AS SELECT path FROM Content;
        CREATE TRIGGER no_empty BEFORE INSERT ON Content WHEN length(NEW.content) = 0
            BEGIN SELECT RAISE(ABORT, 'empty'); END;
    """)
    conn.executemany("INSERT INTO Content (id, path, content) VALUES (?, ?, ?)",
                     [(1, "a.html", b"<p>a</p>"), (5, "big.bin", BIG), (9, "c.html", b"c")])
    conn.executemany("INSERT INTO TooltipButtons (rowid, tooltipId, description) VALUES (?, ?, ?)",
                     [(3, 1, "one"), (40, 5, None), (41, 77, "points nowhere")])
    conn.execute("UPDATE sqlite_sequence SET seq = 120 WHERE name = 'Content'")
    conn.execute("PRAGMA user_version = 7")
    conn.execute("PRAGMA application_id = 1234")
    conn.commit()
    conn.close()
    assert rebuild.reserved_bytes(path) == 12
    return path


def _run(source: Path, output: Path) -> int:
    return rebuild.run({"source": source, "output": output})


def test_the_copy_drops_the_reserved_bytes_and_nothing_else(tmp_path):
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    before = rebuild.fingerprint(source)

    assert _run(source, output) == 0

    assert rebuild.reserved_bytes(output) == 0
    with sqlite3.connect(output) as conn:
        assert conn.execute("PRAGMA page_size").fetchone()[0] == 1024
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    assert rebuild.fingerprint(output) == before
    assert rebuild.fingerprint(source) == before, "the source is never changed"


def test_rowids_gaps_sequence_and_pragmas_survive(tmp_path):
    """The parts a plain INSERT ... SELECT would quietly renumber or drop."""
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    assert _run(source, output) == 0
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT rowid FROM TooltipButtons ORDER BY rowid").fetchall() == [(3,), (40,), (41,)]
        assert conn.execute("SELECT id FROM Content ORDER BY id").fetchall() == [(1,), (5,), (9,)]
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'Content'").fetchone() == (120,)
        assert conn.execute("PRAGMA user_version").fetchone() == (7,)
        assert conn.execute("PRAGMA application_id").fetchone() == (1234,)
        assert conn.execute("SELECT content FROM Content WHERE id = 5").fetchone() == (BIG,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == [("TooltipButtons", 41, "Content", 0)]


def test_an_existing_output_is_refused(tmp_path):
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    output.write_bytes(b"keep me")
    assert _run(source, output) == 1
    assert output.read_bytes() == b"keep me"


def test_a_copy_that_differs_is_deleted(tmp_path):
    """The comparison is what makes the copy safe to swap in, so a copy that
    lost anything must not survive the run."""
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    unpatched = rebuild.rebuild

    def lossy(src, out, pack=(), page_size=None):
        maps = unpatched(src, out, pack, page_size)
        with sqlite3.connect(out) as conn:
            conn.execute("DELETE FROM TooltipButtons WHERE rowid = 40")
        return maps

    rebuild.rebuild = lossy
    try:
        assert _run(source, output) == 1
    finally:
        rebuild.rebuild = unpatched
    assert not output.exists()


def _a_spoiled_copy_is_deleted(tmp_path: Path, table: str, sql: str, source_sql: str = "") -> None:
    """The run fails naming `table`, and keeps no copy, when `sql` changes the
    copy after it is written; `source_sql` first sets up the source."""
    source, output = _source(tmp_path), tmp_path / "rebuilt.db"
    if source_sql:
        with sqlite3.connect(source) as conn:
            conn.executescript(source_sql)
    code, err = _run_spoiled(source, output, sql)
    assert code == 1 and f"table {table}" in err, err
    assert not output.exists()


def test_a_copy_with_an_altered_value_is_deleted(tmp_path):
    """Same rows, same rowids, one value changed."""
    _a_spoiled_copy_is_deleted(tmp_path, "TooltipButtons",
                               "UPDATE TooltipButtons SET description = 'One' WHERE rowid = 3")


def test_a_copy_with_values_swapped_between_rows_is_deleted(tmp_path):
    """Every value still there, on the wrong row - what a wrong id map does."""
    _a_spoiled_copy_is_deleted(tmp_path, "TooltipButtons",
                               "UPDATE TooltipButtons SET description = CASE rowid WHEN 3 THEN 'points nowhere' "
                               "ELSE 'one' END WHERE rowid IN (3, 41)")


def test_a_copy_that_turns_an_integer_into_text_is_deleted(tmp_path):
    """5 and '5' print alike, and a column with no affinity (BLOB here) keeps
    either as given."""
    _a_spoiled_copy_is_deleted(tmp_path, "Content", "UPDATE Content SET content = '5' WHERE id = 9",
                               source_sql="UPDATE Content SET content = 5 WHERE id = 9")


def test_a_copy_that_turns_null_into_an_empty_blob_is_deleted(tmp_path):
    """Both have no bytes; a digest of hex() or of COALESCE(value, x'') would
    see them alike."""
    _a_spoiled_copy_is_deleted(tmp_path, "TooltipButtons",
                               "UPDATE TooltipButtons SET description = x'' WHERE rowid = 40")


def _packable(tmp_path: Path, orphan=False, autoincrement=True) -> Path:
    """Pages whose rowid order packs badly - forty 560-byte rows, then forty
    400-byte ones, so each big row sits alone on a page where a small one
    would have fitted beside it - plus Links naming them by id through a
    declared foreign key."""
    path = tmp_path / "packable.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        PRAGMA page_size = 1024;
        CREATE TABLE Pages (id INTEGER PRIMARY KEY{' AUTOINCREMENT' if autoincrement else ''}, path TEXT NOT NULL,
                            body BLOB NOT NULL, UNIQUE(path));
        CREATE TABLE Links (pageId INTEGER, label TEXT, FOREIGN KEY(pageId) REFERENCES Pages(id));
    """)
    sizes = [560] * 40 + [400] * 40
    conn.executemany("INSERT INTO Pages (path, body) VALUES (?, ?)",
                     [(f"p{i}.html", bytes([i % 251]) * size) for i, size in enumerate(sizes)])
    conn.executemany("INSERT INTO Links (pageId, label) VALUES (?, ?)",
                     [(i, f"link to p{i - 1}") for i in range(1, len(sizes) + 1, 3)])
    if orphan:
        conn.execute("INSERT INTO Links (pageId, label) VALUES (9999, 'nowhere')")
    conn.commit()
    conn.close()
    return path


def _pairs(path: Path) -> list:
    with sqlite3.connect(path) as conn:
        return sorted(conn.execute("SELECT l.label, p.path FROM Links l JOIN Pages p ON p.id = l.pageId"))


def test_pack_fills_pages_that_rowid_order_leaves_part_empty(tmp_path):
    source = _packable(tmp_path)
    plain, packed = tmp_path / "plain.db", tmp_path / "packed.db"
    assert rebuild.run({"source": source, "output": plain}) == 0
    assert rebuild.run({"source": source, "output": packed, "pack": ["Pages"]}) == 0
    assert rebuild.leaf_pages(packed, "Pages") < rebuild.leaf_pages(plain, "Pages") * 0.8, (
        rebuild.leaf_pages(packed, "Pages"), rebuild.leaf_pages(plain, "Pages"))


def test_pack_renumbers_rows_and_their_references_follow(tmp_path):
    source, packed = _packable(tmp_path), tmp_path / "packed.db"
    assert rebuild.run({"source": source, "output": packed, "pack": ["Pages"]}) == 0
    with sqlite3.connect(source) as a, sqlite3.connect(packed) as b:
        assert a.execute("SELECT id, path FROM Pages").fetchall() != b.execute("SELECT id, path FROM Pages").fetchall()
        (count,) = a.execute("SELECT COUNT(*) FROM Pages").fetchone()
        (old_seq,) = a.execute("SELECT seq FROM sqlite_sequence WHERE name = 'Pages'").fetchone()
        assert b.execute("SELECT seq FROM sqlite_sequence WHERE name = 'Pages'").fetchone() == (old_seq + count,)
    assert _pairs(packed) == _pairs(source)


def test_packed_ids_continue_after_the_old_sequence(tmp_path):
    """AUTOINCREMENT never hands out an id twice, so neither does packing: an
    id quoted before the rebuild can stop naming a row, never name another."""
    source, packed = _packable(tmp_path), tmp_path / "packed.db"
    with sqlite3.connect(source) as conn:
        conn.execute("DELETE FROM Links WHERE pageId = 80")
        conn.execute("DELETE FROM Pages WHERE id = 80")  # the sequence now runs past the largest id
        (old_seq,) = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'Pages'").fetchone()
        old_ids = {row[0] for row in conn.execute("SELECT id FROM Pages")}
    assert rebuild.run({"source": source, "output": packed, "pack": ["Pages"]}) == 0
    with sqlite3.connect(packed) as conn:
        new_ids = sorted(row[0] for row in conn.execute("SELECT id FROM Pages"))
    assert new_ids == list(range(old_seq + 1, old_seq + 1 + len(old_ids))) and not set(new_ids) & old_ids


def _pack_and_check_ids_follow_the_largest(source: Path, packed: Path) -> None:
    with sqlite3.connect(source) as conn:
        old_ids = sorted(row[0] for row in conn.execute("SELECT id FROM Pages"))
    assert rebuild.run({"source": source, "output": packed, "pack": ["Pages"]}) == 0
    with sqlite3.connect(packed) as conn:
        new_ids = sorted(row[0] for row in conn.execute("SELECT id FROM Pages"))
    assert new_ids == list(range(old_ids[-1] + 1, old_ids[-1] + 1 + len(old_ids))), (new_ids[0], old_ids[-1])
    assert _pairs(packed) == _pairs(source)


def test_packed_ids_continue_after_the_largest_id_when_the_sequence_lags(tmp_path):
    """A hand-edited or cleared sqlite_sequence row can sit below the largest
    id. AUTOINCREMENT then hands out the id after the largest, and so must
    packing, or an old id would come to name a different row."""
    source, packed = _packable(tmp_path), tmp_path / "packed.db"
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE sqlite_sequence SET seq = 10 WHERE name = 'Pages'")
    _pack_and_check_ids_follow_the_largest(source, packed)
    with sqlite3.connect(packed) as conn:
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'Pages'").fetchone() == (160,)


def test_packed_ids_continue_after_the_largest_id_without_autoincrement(tmp_path):
    """A table with no AUTOINCREMENT has no sequence at all."""
    source, packed = _packable(tmp_path, autoincrement=False), tmp_path / "packed.db"
    _pack_and_check_ids_follow_the_largest(source, packed)


def test_page_size_gives_the_copy_that_page_size_and_nothing_else(tmp_path):
    source, copy = _packable(tmp_path), tmp_path / "copy.db"
    assert rebuild.run({"source": source, "output": copy, "pack": ["Pages"], "page_size": 4096}) == 0
    assert rebuild.page_size_of(source) == 1024 and rebuild.page_size_of(copy) == 4096
    assert _pairs(copy) == _pairs(source)


def test_pack_plans_pages_for_the_copys_page_size(tmp_path, monkeypatch):
    """Planned for the source's 1 KB pages, a 4 KB copy would be packed for the
    wrong room - still correct, but not packed."""
    seen = []
    plan = rebuild.packing_order

    def spy(conn, table, page_size, largest):
        seen.append(page_size)
        return plan(conn, table, page_size, largest)

    monkeypatch.setattr(rebuild, "packing_order", spy)
    source, copy = _packable(tmp_path), tmp_path / "copy.db"
    assert rebuild.run({"source": source, "output": copy, "pack": ["Pages"], "page_size": 4096}) == 0
    assert seen == [4096]


def test_page_size_refuses_what_sqlite_would_silently_ignore():
    for size in (512, 4096, 65536):
        assert rebuild.page_size_arg(str(size)) == size
    for value in ("3000", "256", "131072", "0"):
        try:
            rebuild.page_size_arg(value)
        except Exception as exc:  # noqa: BLE001
            assert "power of two" in str(exc)
        else:
            raise AssertionError(f"{value} was accepted")


def test_pack_refuses_a_table_with_orphaned_references(tmp_path):
    """An orphan's old id could match one of the new ids, turning a dangling
    reference into a wrong one."""
    source, packed = _packable(tmp_path, orphan=True), tmp_path / "packed.db"
    assert rebuild.run({"source": source, "output": packed, "pack": ["Pages"]}) == 1
    assert not packed.exists()


def test_pack_refuses_a_table_that_does_not_exist(tmp_path):
    source, packed = _packable(tmp_path), tmp_path / "packed.db"
    assert rebuild.run({"source": source, "output": packed, "pack": ["Nope"]}) == 1
    assert not packed.exists()


def _run_spoiled(source: Path, output: Path, sql: str, pack=()) -> tuple:
    """run() with the copy changed by `sql` once it is written, as a bug in the
    copy would change it; returns (exit code, what run() printed to stderr)."""
    unpatched = rebuild.rebuild

    def spoiled(src, out, pack=(), page_size=None):
        maps = unpatched(src, out, pack, page_size)
        with sqlite3.connect(out) as conn:
            conn.executescript(sql)
        return maps

    rebuild.rebuild = spoiled
    try:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = rebuild.run({"source": source, "output": output, "pack": list(pack)})
    finally:
        rebuild.rebuild = unpatched
    return code, err.getvalue()


# documentation.db's own triggers, verbatim.
ADD_BOOK = """CREATE TRIGGER AddBook AFTER INSERT ON Content WHEN NEW.path LIKE '%.pdf'
BEGIN
    INSERT INTO Bookshelf (contentID, title) VALUES (NEW.id, CURRENT_TIMESTAMP || NEW.id);
END"""
DELETE_BOOK = """CREATE TRIGGER DeleteBook AFTER DELETE ON Content WHEN OLD.path LIKE '%.pdf'
BEGIN
DELETE FROM Bookshelf WHERE contentID = OLD.id;
END"""


def _bookshelf(tmp_path: Path) -> Path:
    """Content, Templates and Bookshelf as documentation.db has them -
    Bookshelf.contentID and Content.templateId name rows through no FOREIGN
    KEY - with Content rows that pack in another order than their ids (as
    _packable's do), and a curated Bookshelf row for every .pdf."""
    path = tmp_path / "bookshelf.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        PRAGMA page_size = 1024;
        CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL,
                                UNIQUE(name));
        CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, content BLOB NOT NULL,
                              templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));
        CREATE TABLE BookCategories (id INTEGER PRIMARY KEY AUTOINCREMENT, category STRING,
                                     description STRING DEFAULT '', UNIQUE(category));
        CREATE TABLE Bookshelf (contentID INTEGER NOT NULL, title STRING DEFAULT '', description STRING DEFAULT '',
                                bookCategoryID INTEGER, FOREIGN KEY (bookCategoryID) REFERENCES BookCategories(id),
                                UNIQUE(title, bookCategoryId));
        {ADD_BOOK};
        {DELETE_BOOK};
        INSERT INTO Templates (name, content) VALUES ('page.peb', x'01'), ('nav.peb', x'02');
        INSERT INTO BookCategories (category) VALUES ('Java'), ('Kotlin');
    """)
    sizes = [560] * 40 + [400] * 40
    conn.executemany("INSERT INTO Content (path, content, templateId) VALUES (?, ?, ?)",
                     [(f"p{i}.{'pdf' if i % 5 == 0 else 'html'}", bytes([i % 251]) * size, 1 + i % 2)
                      for i, size in enumerate(sizes)])
    conn.execute("DELETE FROM Bookshelf")  # AddBook's placeholder rows, replaced by curated ones
    conn.execute("INSERT INTO Bookshelf (contentID, title, description, bookCategoryID) "
                 "SELECT id, 'Book ' || id, 'About ' || path, 1 + id % 2 FROM Content WHERE path LIKE '%.pdf'")
    conn.commit()
    conn.close()
    return path


def _shelf(path: Path) -> list:
    with sqlite3.connect(path) as conn:
        return sorted(conn.execute("SELECT b.rowid, c.path, b.title, b.description, b.bookCategoryID "
                                   "FROM Bookshelf b JOIN Content c ON c.id = b.contentID"))


def test_pack_relinks_bookshelf_which_no_foreign_key_ties_to_content(tmp_path):
    """Only the AddBook and DeleteBook triggers tie Bookshelf.contentID to
    Content.id, so foreign_key_list cannot find it; packing Content must move
    it to the new ids all the same, keep the rest of each row, and add no row
    (the triggers must not fire while the rows are copied)."""
    source, packed = _bookshelf(tmp_path), tmp_path / "packed.db"
    shelf = _shelf(source)
    assert len(shelf) == 16
    assert rebuild.run({"source": source, "output": packed, "pack": ["Content"]}) == 0
    assert _shelf(packed) == shelf
    with sqlite3.connect(packed) as conn:
        assert conn.execute("SELECT COUNT(*) FROM Bookshelf").fetchone() == (len(shelf),)
        assert conn.execute("SELECT MIN(id) FROM Content").fetchone() == (81,), "the ids did change"
        # DeleteBook still finds a book's shelf row by its new id.
        conn.execute("DELETE FROM Content WHERE path = 'p5.pdf'")
        assert conn.execute("SELECT COUNT(*) FROM Bookshelf").fetchone() == (len(shelf) - 1,)


def test_a_bookshelf_row_left_on_its_old_id_fails_the_run(tmp_path):
    """The comparison must see a reference the pack missed: an old id names no
    row of the copy, so it may not map back to a match."""
    source, packed = _bookshelf(tmp_path), tmp_path / "packed.db"
    with sqlite3.connect(source) as conn:
        rowid, old_id = conn.execute("SELECT rowid, contentID FROM Bookshelf ORDER BY rowid LIMIT 1").fetchone()
    code, err = _run_spoiled(source, packed, f"UPDATE Bookshelf SET contentID = {old_id} WHERE rowid = {rowid}",
                             pack=["Content"])
    assert code == 1 and "table Bookshelf" in err, err
    assert not packed.exists()


def test_pack_templates_relinks_content_templateid(tmp_path):
    source, packed = _bookshelf(tmp_path), tmp_path / "packed.db"
    query = "SELECT c.path, t.name FROM Content c JOIN Templates t ON t.id = c.templateId"
    with sqlite3.connect(source) as conn:
        pages = sorted(conn.execute(query))
    assert len(pages) == 80
    assert rebuild.run({"source": source, "output": packed, "pack": ["Templates"]}) == 0
    with sqlite3.connect(packed) as conn:
        assert sorted(conn.execute(query)) == pages
        assert conn.execute("SELECT MIN(id) FROM Templates").fetchone() == (3,), "the ids did change"


def _with_untemplated_rows(tmp_path: Path) -> Path:
    """_bookshelf with every third Content row on templateId 0, which the app
    serves as stored: documentation.db has 7,156 such rows."""
    source = _bookshelf(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE Content SET templateId = 0 WHERE id % 3 = 0")
    return source


def _untemplated(path: Path) -> list:
    with sqlite3.connect(path) as conn:
        return sorted(conn.execute("SELECT path FROM Content WHERE templateId = 0"))


def test_templateid_zero_is_not_an_orphan(tmp_path):
    """0 names no Templates row by design; counting it as an orphan refused
    --pack Templates on every documentation.db."""
    source = _with_untemplated_rows(tmp_path)
    rebuild.rebuild(source, tmp_path / "packed.db", ["Templates"])  # raises on an orphan
    assert _untemplated(tmp_path / "packed.db") == _untemplated(source) != []


def test_the_identity_check_takes_templateid_zero_as_none(tmp_path):
    """0 is not a renumbered id, so it must compare as itself, not as an old id
    the pack left behind."""
    source, packed = _with_untemplated_rows(tmp_path), tmp_path / "packed.db"
    renumbered, followed, _advanced = rebuild.rebuild(source, packed, ["Templates"])
    assert followed == {"Content": {"templateId": "Templates"}}
    assert (rebuild.fingerprint(packed, renumbered, followed)["table Content"]
            == rebuild.fingerprint(source)["table Content"])


def test_pack_templates_keeps_the_zeros_and_relinks_the_rest(tmp_path):
    source, packed = _with_untemplated_rows(tmp_path), tmp_path / "packed.db"
    query = "SELECT c.path, t.name FROM Content c JOIN Templates t ON t.id = c.templateId"
    with sqlite3.connect(source) as conn:
        pages = sorted(conn.execute(query))
    assert rebuild.run({"source": source, "output": packed, "pack": ["Templates"]}) == 0
    assert _untemplated(packed) == _untemplated(source)
    with sqlite3.connect(packed) as conn:
        assert sorted(conn.execute(query)) == pages
        assert conn.execute("SELECT MIN(id) FROM Templates").fetchone() == (3,), "the ids did change"


def test_a_templateid_naming_no_template_is_still_an_orphan(tmp_path):
    """Only 0 means none: any other id with no row could come to name a new one."""
    source, packed = _with_untemplated_rows(tmp_path), tmp_path / "packed.db"
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE Content SET templateId = 99 WHERE path = 'p1.html'")
    with contextlib.redirect_stderr(io.StringIO()) as err:
        assert rebuild.run({"source": source, "output": packed, "pack": ["Templates"]}) == 1
    assert "1 row(s) of Content.templateId name no Templates row" in err.getvalue(), err.getvalue()
    assert not packed.exists()


def test_a_template_with_id_zero_is_refused(tmp_path):
    """Then 0 would both mean none and name that template."""
    source, packed = _with_untemplated_rows(tmp_path), tmp_path / "packed.db"
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO Templates (id, name, content) VALUES (0, 'zero.peb', x'00')")
    with contextlib.redirect_stderr(io.StringIO()) as err:
        assert rebuild.run({"source": source, "output": packed, "pack": ["Templates"]}) == 1
    assert "Content.templateId = 0 means no Templates row, but one has rowid 0" in err.getvalue(), err.getvalue()
    assert not packed.exists()


def test_pack_refuses_a_table_whose_trigger_links_its_id_elsewhere(tmp_path):
    """A trigger that stores or matches the id in a column no followed
    reference covers is a link the pack would leave on the old ids."""
    triggers = {
        "LogPage": "CREATE TRIGGER LogPage AFTER INSERT ON Pages BEGIN INSERT INTO Log (pageId) VALUES (NEW.id); END",
        "ForgetPage": 'CREATE TRIGGER "ForgetPage" AFTER DELETE ON Pages BEGIN '
                      'DELETE FROM "Log" WHERE pageId = OLD.rowid; END',
    }
    for name, trigger in triggers.items():
        directory = tmp_path / name
        directory.mkdir()
        source, packed = _packable(directory), directory / "packed.db"
        with sqlite3.connect(source) as conn:
            conn.execute("CREATE TABLE Log (pageId INTEGER)")
            conn.execute(trigger)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert rebuild.run({"source": source, "output": packed, "pack": ["Pages"]}) == 1
        assert f"trigger {name} " in err.getvalue(), err.getvalue()
        assert not packed.exists()


if __name__ == "__main__":
    sys.exit(base._main(globals()))
