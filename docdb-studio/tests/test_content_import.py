"""Tests for content-folder import helpers in docdb_studio.py."""

import sqlite3
import tempfile
from pathlib import Path

import brotli
import pytest

import docdb_studio

walk_content_folder = docdb_studio.walk_content_folder
mime_for_filename = docdb_studio.mime_for_filename
get_content_types = docdb_studio.get_content_types
get_languages = docdb_studio.get_languages
compress_for_storage = docdb_studio.compress_for_storage
fragment_blob = docdb_studio.fragment_blob
target_paths = docdb_studio.target_paths
prescan_content_import = docdb_studio.prescan_content_import
import_content_files = docdb_studio.import_content_files
CONTENT_CHUNK_SIZE = docdb_studio.CONTENT_CHUNK_SIZE
SQL_PARAM_BATCH = docdb_studio.SQL_PARAM_BATCH
ImportItem = docdb_studio.ImportItem


def _make_db_with_content_schema() -> Path:
    """Create a temp DB seeded with Content/ContentTypes/Languages/LastChange schema and a few rows.

    Pinned to SQLITE_PAGE_SIZE_BYTES up front so these tests (about import
    behavior, not migration) don't incidentally trigger the one-time ADFA-5141
    page_size vacuum -- that path has its own dedicated tests in test_vacuum.py.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.execute(f"PRAGMA page_size={docdb_studio.SQLITE_PAGE_SIZE_BYTES}")
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
            INSERT INTO Languages (id, value) VALUES (1, 'en'), (2, 'ja');
            INSERT INTO ContentTypes (id, value, compression) VALUES
                (1, 'text/html', 'brotli'),
                (2, 'text/css', 'brotli'),
                (3, 'image/png', 'none'),
                (4, 'text/markdown', 'brotli'),
                (5, 'text/plain', 'brotli');
            """
        )
        conn.commit()
    return p


# ---------- fragment_blob ----------


def test_fragment_blob_short_returns_one_chunk() -> None:
    assert fragment_blob(b"hello") == [b"hello"]


def test_fragment_blob_empty_returns_one_empty_chunk() -> None:
    assert fragment_blob(b"") == [b""]


def test_fragment_blob_exact_chunk_size_returns_one_chunk() -> None:
    blob = b"x" * CONTENT_CHUNK_SIZE
    chunks = fragment_blob(blob)
    assert len(chunks) == 1
    assert chunks[0] == blob


def test_fragment_blob_double_chunk_size_returns_two_full_chunks() -> None:
    blob = b"x" * (CONTENT_CHUNK_SIZE * 2)
    chunks = fragment_blob(blob)
    assert len(chunks) == 2
    assert all(len(c) == CONTENT_CHUNK_SIZE for c in chunks)


def test_fragment_blob_uneven_returns_full_then_partial() -> None:
    blob = b"a" * CONTENT_CHUNK_SIZE + b"b" * 100
    chunks = fragment_blob(blob)
    assert len(chunks) == 2
    assert len(chunks[0]) == CONTENT_CHUNK_SIZE
    assert chunks[1] == b"b" * 100


def test_fragment_blob_concatenation_round_trip() -> None:
    blob = bytes(range(256)) * 5000  # 1.28 MB
    chunks = fragment_blob(blob)
    assert b"".join(chunks) == blob


# ---------- target_paths ----------


def test_target_paths_single() -> None:
    assert target_paths("docs/intro.html", 1) == ["docs/intro.html"]


def test_target_paths_multiple() -> None:
    assert target_paths("a/big.pdf", 3) == ["a/big.pdf", "a/big.pdf-1", "a/big.pdf-2"]


def test_target_paths_zero_raises() -> None:
    with pytest.raises(ValueError):
        target_paths("foo", 0)


# ---------- compress_for_storage ----------


def test_compress_for_storage_brotli_round_trip() -> None:
    data = b"hello world " * 1000
    compressed = compress_for_storage(data, "brotli")
    assert compressed != data
    assert brotli.decompress(compressed) == data


def test_compress_for_storage_none_passthrough() -> None:
    data = b"\x00\x01\x02 some bytes"
    assert compress_for_storage(data, "none") is data or compress_for_storage(data, "none") == data


# ---------- mime_for_filename ----------


def test_mime_for_filename_html() -> None:
    assert mime_for_filename("page.html") == "text/html"


def test_mime_for_filename_markdown_registered() -> None:
    assert mime_for_filename("README.md") == "text/markdown"


def test_mime_for_filename_unknown_extension_returns_none() -> None:
    assert mime_for_filename("data.xyzunknown") is None


def test_mime_for_filename_no_extension_returns_none() -> None:
    assert mime_for_filename("LICENSE") is None


# ---------- walk_content_folder ----------


def test_walk_content_folder_lists_regular_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.html").write_text("b")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.css").write_text("c")
    regular, symlinks, hidden = walk_content_folder(tmp_path)
    rel_paths = sorted(p.relative_to(tmp_path).as_posix() for p in regular)
    assert rel_paths == ["a.txt", "b.html", "sub/c.css"]
    assert symlinks == []
    assert hidden == []


def test_walk_content_folder_skips_dot_files(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / ".hidden").write_text("y")
    regular, _, hidden = walk_content_folder(tmp_path)
    rel_regular = sorted(p.name for p in regular)
    rel_hidden = sorted(p.name for p in hidden)
    assert rel_regular == ["real.txt"]
    assert rel_hidden == [".hidden"]


def test_walk_content_folder_skips_dot_dirs(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("x")
    regular, _, _ = walk_content_folder(tmp_path)
    assert sorted(p.name for p in regular) == ["a.txt"]


def test_walk_content_folder_skips_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    regular, symlinks, _ = walk_content_folder(tmp_path)
    rel_regular = sorted(p.name for p in regular)
    rel_symlinks = sorted(p.name for p in symlinks)
    assert rel_regular == ["real.txt"]
    assert rel_symlinks == ["link.txt"]


# ---------- DB queries ----------


def test_get_content_types_returns_mime_dict() -> None:
    db = _make_db_with_content_schema()
    try:
        types = get_content_types(db)
        assert types["text/html"] == (1, "brotli")
        assert types["image/png"] == (3, "none")
        assert "text/markdown" in types
    finally:
        db.unlink(missing_ok=True)


def test_get_languages_returns_ordered_pairs() -> None:
    db = _make_db_with_content_schema()
    try:
        langs = get_languages(db)
        assert langs == [(1, "en"), (2, "ja")]
    finally:
        db.unlink(missing_ok=True)


# ---------- prescan_content_import ----------


def test_prescan_classifies_mapped_and_unmapped(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        root = tmp_path / "myroot"
        root.mkdir()
        (root / "page.html").write_text("<html/>")
        (root / "style.css").write_text("body{}")
        (root / "data.xyzunknown").write_text("?")
        scan = prescan_content_import(db, root, get_content_types(db))
        mapped_paths = sorted(item.base_path for item in scan.mapped)
        assert mapped_paths == ["myroot/page.html", "myroot/style.css"]
        unmapped_names = sorted(p.name for p, _ in scan.unmapped)
        assert unmapped_names == ["data.xyzunknown"]
        assert scan.overwrites == []
    finally:
        db.unlink(missing_ok=True)


def test_prescan_keeps_picked_folder_basename_in_paths(tmp_path: Path) -> None:
    """Regression: picking /.../h/ with a file h/a.txt must produce DB path 'h/a.txt'."""
    db = _make_db_with_content_schema()
    try:
        h = tmp_path / "h"
        h.mkdir()
        (h / "a.txt").write_text("hello")
        scan = prescan_content_import(db, h, get_content_types(db))
        assert [item.base_path for item in scan.mapped] == ["h/a.txt"]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_preserves_subdirectory_structure(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        root = tmp_path / "docs"
        (root / "guide").mkdir(parents=True)
        (root / "intro.html").write_text("<html/>")
        (root / "guide" / "deep.html").write_text("<html/>")
        scan = prescan_content_import(db, root, get_content_types(db))
        paths = sorted(item.base_path for item in scan.mapped)
        assert paths == ["docs/guide/deep.html", "docs/intro.html"]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_marks_base_path_collision_as_overwrite(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("myroot/page.html", b"\x00"),
            )
            conn.commit()
        root = tmp_path / "myroot"
        root.mkdir()
        (root / "page.html").write_text("<html/>")
        scan = prescan_content_import(db, root, get_content_types(db))
        assert [item.base_path for item in scan.mapped] == ["myroot/page.html"]
        assert scan.overwrites == ["myroot/page.html"]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_reports_symlinks_and_hidden(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        root = tmp_path / "myroot"
        root.mkdir()
        real = root / "real.html"
        real.write_text("<html/>")
        (root / "link.html").symlink_to(real)
        (root / ".dotfile").write_text("hidden")
        scan = prescan_content_import(db, root, get_content_types(db))
        mapped_names = sorted(item.source.name for item in scan.mapped)
        assert mapped_names == ["real.html"]
        assert sorted(p.name for p in scan.skipped_symlinks) == ["link.html"]
        assert sorted(p.name for p in scan.skipped_hidden) == [".dotfile"]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_routes_filenames_with_zero_width_chars_to_skipped_bad_name(
    tmp_path: Path,
) -> None:
    db = _make_db_with_content_schema()
    try:
        root = tmp_path / "myroot"
        root.mkdir()
        (root / "good.html").write_text("<html/>")
        # File whose name contains a zero-width space (U+200B).
        bad_name = "evil​name.html"
        (root / bad_name).write_text("<html/>")
        scan = prescan_content_import(db, root, get_content_types(db))
        mapped_names = sorted(item.source.name for item in scan.mapped)
        bad_names = sorted(p.name for p, _ in scan.skipped_bad_name)
        assert mapped_names == ["good.html"]
        assert bad_names == [bad_name]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_finds_orphans_under_chosen_folder(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            for path in ("a/keep.html", "a/old.html"):
                conn.execute(
                    'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                    (path, b"\x00"),
                )
            conn.commit()
        root = tmp_path / "a"
        root.mkdir()
        (root / "keep.html").write_text("<html/>")
        scan = prescan_content_import(db, root, get_content_types(db))
        assert [item.base_path for item in scan.mapped] == ["a/keep.html"]
        assert scan.orphans == ["a/old.html"]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_orphans_excludes_other_top_level_folders(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            for path in ("a/foo.html", "b/bar.html", "c/sub/deep.html"):
                conn.execute(
                    'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                    (path, b"\x00"),
                )
            conn.commit()
        root = tmp_path / "a"
        root.mkdir()
        (root / "foo.html").write_text("<html/>")
        scan = prescan_content_import(db, root, get_content_types(db))
        # Only a/foo.html is in scope and it's in the import — no orphans. Other
        # top-level folders (b/, c/) are out of scope and untouched.
        assert scan.orphans == []
    finally:
        db.unlink(missing_ok=True)


def test_prescan_orphans_includes_subfolder_files(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("a/sub/deep.html", b"\x00"),
            )
            conn.commit()
        root = tmp_path / "a"
        root.mkdir()
        (root / "top.html").write_text("<html/>")
        scan = prescan_content_import(db, root, get_content_types(db))
        assert scan.orphans == ["a/sub/deep.html"]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_orphans_groups_chunks_under_logical_base(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            for suffix in ("", "-1", "-2"):
                conn.execute(
                    'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                    (f"a/big.bin{suffix}", b"\x00"),
                )
            conn.commit()
        root = tmp_path / "a"
        root.mkdir()
        (root / "other.html").write_text("<html/>")
        scan = prescan_content_import(db, root, get_content_types(db))
        # big.bin is reported as a single orphan, not three.
        assert scan.orphans == ["a/big.bin"]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_overwrite_check_batches_large_candidate_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IN-query for the overwrite check must chunk so it doesn't blow past
    SQLite's host-parameter ceiling on large folders."""
    # Force a tiny batch size so the test crosses several batch boundaries
    # without having to materialize thousands of files.
    monkeypatch.setattr(docdb_studio, "SQL_PARAM_BATCH", 5)
    db = _make_db_with_content_schema()
    try:
        # 12 candidate files: more than 2x the (patched) batch size, with a
        # partial trailing batch. Pre-seed every other one so the overwrite set
        # is non-trivial and crosses batch boundaries.
        root = tmp_path / "many"
        root.mkdir()
        with sqlite3.connect(db) as conn:
            for i in range(12):
                name = f"f{i:02d}.html"
                (root / name).write_text("<html/>")
                if i % 2 == 0:
                    conn.execute(
                        'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                        (f"many/{name}", b"\x00"),
                    )
            conn.commit()
        scan = prescan_content_import(db, root, get_content_types(db))
        assert len(scan.mapped) == 12
        assert scan.overwrites == [f"many/f{i:02d}.html" for i in range(0, 12, 2)]
    finally:
        db.unlink(missing_ok=True)


def test_prescan_orphans_treats_non_digit_suffix_as_its_own_file(
    tmp_path: Path,
) -> None:
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            # 'a/foo.bin-extra.txt' is its own file (suffix isn't digits), not a
            # chunk of 'a/foo.bin'. It should be reported as an orphan in its
            # own right.
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                ("a/foo.bin-extra.txt", b"\x00"),
            )
            conn.commit()
        root = tmp_path / "a"
        root.mkdir()
        (root / "foo.bin").write_bytes(b"new")
        scan = prescan_content_import(db, root, get_content_types(db))
        assert "a/foo.bin-extra.txt" in scan.orphans
        assert "a/foo.bin" not in scan.orphans  # imported, not orphaned
    finally:
        db.unlink(missing_ok=True)


# ---------- import_content_files ----------


def _read_content_rows(db: Path) -> list[tuple[str, int, bytes, int]]:
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            'SELECT path, languageID, content, contentTypeID FROM "Content" ORDER BY path'
        )
        return cur.fetchall()


def _ids_for_paths(db: Path, paths: list[str]) -> list[int]:
    """Look up row ids for the given paths (test helper)."""
    if not paths:
        return []
    with sqlite3.connect(db) as conn:
        placeholders = ",".join("?" * len(paths))
        cur = conn.execute(
            f'SELECT id FROM "Content" WHERE path IN ({placeholders})',
            paths,
        )
        return [row[0] for row in cur.fetchall()]


def test_import_inserts_single_chunk_compressed(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        src = tmp_path / "page.html"
        body = b"<html><body>" + b"x" * 5000 + b"</body></html>"
        src.write_bytes(body)
        item = ImportItem(
            source=src,
            base_path="page.html",
            content_type_id=1,
            mime="text/html",
            compression="brotli",
        )
        summary = import_content_files(
            db, [item], language_id=1, user_name="Alice", documentation_set="content-test"
        )
        assert summary.files_imported == 1
        assert summary.rows_inserted == 1
        rows = _read_content_rows(db)
        assert len(rows) == 1
        path, lang, blob, ctid = rows[0]
        assert path == "page.html"
        assert lang == 1
        assert ctid == 1
        assert brotli.decompress(blob) == body
    finally:
        db.unlink(missing_ok=True)


def test_import_inserts_raw_bytes_for_none_compression(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        src = tmp_path / "img.png"
        body = b"\x89PNG\r\n\x1a\n" + b"raw bytes"
        src.write_bytes(body)
        item = ImportItem(
            source=src,
            base_path="img.png",
            content_type_id=3,
            mime="image/png",
            compression="none",
        )
        import_content_files(db, [item], 1, "Alice", "content-test")
        rows = _read_content_rows(db)
        assert rows[0][2] == body  # exact bytes, no compression
    finally:
        db.unlink(missing_ok=True)


def test_import_fragments_large_files(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        # Use compression='none' so the file size in storage equals the file size on disk.
        src = tmp_path / "big.bin"
        body = b"X" * (CONTENT_CHUNK_SIZE + 500)
        src.write_bytes(body)
        item = ImportItem(
            source=src,
            base_path="big.bin",
            content_type_id=3,
            mime="image/png",  # mapped to 'none' compression
            compression="none",
        )
        summary = import_content_files(db, [item], 1, "Alice", "content-test")
        assert summary.files_imported == 1
        assert summary.rows_inserted == 2
        rows = _read_content_rows(db)
        assert len(rows) == 2
        paths = sorted(r[0] for r in rows)
        assert paths == ["big.bin", "big.bin-1"]
        # Concatenation reproduces the original bytes:
        by_path = {r[0]: r[2] for r in rows}
        assert by_path["big.bin"] + by_path["big.bin-1"] == body
        assert len(by_path["big.bin"]) == CONTENT_CHUNK_SIZE
        assert len(by_path["big.bin-1"]) == 500
    finally:
        db.unlink(missing_ok=True)


def test_import_overwrites_existing_chunks(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        # Pre-seed both chunks of a prior import with arbitrary bytes.
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                ("big.bin", b"OLD-PART-1"),
            )
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                ("big.bin-1", b"OLD-PART-2"),
            )
            conn.commit()
        old_ids = _ids_for_paths(db, ["big.bin", "big.bin-1"])
        src = tmp_path / "big.bin"
        body = b"X" * (CONTENT_CHUNK_SIZE + 500)
        src.write_bytes(body)
        item = ImportItem(
            source=src,
            base_path="big.bin",
            content_type_id=3,
            mime="image/png",
            compression="none",
        )
        summary = import_content_files(
            db,
            [item],
            1,
            "Alice",
            "content-test",
            overwrite_row_ids_by_base={"big.bin": old_ids},
        )
        assert summary.files_imported == 1
        assert summary.files_overwritten == 1
        assert summary.errors == []
        rows = _read_content_rows(db)
        assert sorted(r[0] for r in rows) == ["big.bin", "big.bin-1"]
        by_path = {r[0]: r[2] for r in rows}
        assert by_path["big.bin"] + by_path["big.bin-1"] == body
    finally:
        db.unlink(missing_ok=True)


def test_import_replacement_with_fewer_chunks_leaves_no_orphans(
    tmp_path: Path,
) -> None:
    db = _make_db_with_content_schema()
    try:
        # Simulate a prior 5-chunk import.
        with sqlite3.connect(db) as conn:
            for i, suffix in enumerate(["", "-1", "-2", "-3", "-4"]):
                conn.execute(
                    'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                    (f"set/big.bin{suffix}", f"OLD-{i}".encode()),
                )
            conn.commit()
        old_ids = _ids_for_paths(
            db,
            [f"set/big.bin{suffix}" for suffix in ("", "-1", "-2", "-3", "-4")],
        )
        src = tmp_path / "big.bin"
        # New file fits in 2 chunks under compression='none'.
        body = b"Y" * (CONTENT_CHUNK_SIZE + 100)
        src.write_bytes(body)
        item = ImportItem(
            source=src,
            base_path="set/big.bin",
            content_type_id=3,
            mime="image/png",
            compression="none",
        )
        summary = import_content_files(
            db,
            [item],
            1,
            "Alice",
            "content-test",
            overwrite_row_ids_by_base={"set/big.bin": old_ids},
        )
        assert summary.files_imported == 1
        assert summary.files_overwritten == 1
        assert summary.rows_inserted == 2
        rows = _read_content_rows(db)
        assert sorted(r[0] for r in rows) == ["set/big.bin", "set/big.bin-1"]
        by_path = {r[0]: r[2] for r in rows}
        assert by_path["set/big.bin"] + by_path["set/big.bin-1"] == body
    finally:
        db.unlink(missing_ok=True)


def test_import_overwrite_does_not_touch_unrelated_lookalike_paths(
    tmp_path: Path,
) -> None:
    db = _make_db_with_content_schema()
    try:
        # Pre-seed the real target plus a literal sibling that shares the prefix
        # but isn't a numbered chunk (suffix is not all digits).
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                ("foo.bin", b"OLD-FOO"),
            )
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                ("foo.bin-extra.txt", b"UNRELATED"),
            )
            conn.commit()
        # Caller provides ONLY foo.bin's id — the lookalike isn't in the
        # delete list, so it must survive.
        foo_ids = _ids_for_paths(db, ["foo.bin"])
        src = tmp_path / "foo.bin"
        body = b"NEW-FOO-CONTENT"
        src.write_bytes(body)
        item = ImportItem(
            source=src,
            base_path="foo.bin",
            content_type_id=3,
            mime="image/png",
            compression="none",
        )
        summary = import_content_files(
            db,
            [item],
            1,
            "Alice",
            "content-test",
            overwrite_row_ids_by_base={"foo.bin": foo_ids},
        )
        assert summary.files_overwritten == 1
        rows = _read_content_rows(db)
        by_path = {r[0]: r[2] for r in rows}
        assert by_path["foo.bin"] == body
        assert by_path["foo.bin-extra.txt"] == b"UNRELATED"
    finally:
        db.unlink(missing_ok=True)


def test_import_updates_lastchange_with_documentation_set(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        src = tmp_path / "page.html"
        src.write_bytes(b"<html/>")
        item = ImportItem(
            source=src,
            base_path="page.html",
            content_type_id=1,
            mime="text/html",
            compression="brotli",
        )
        import_content_files(db, [item], 1, "Alice", "content-mydocs")
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT documentationSet, who FROM LastChange WHERE documentationSet = ?",
                ("content-mydocs",),
            )
            row = cur.fetchone()
        assert row == ("content-mydocs", "Alice")
    finally:
        db.unlink(missing_ok=True)


def test_import_summary_breaks_down_by_mime(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        a = tmp_path / "a.html"
        a.write_bytes(b"<html/>")
        b = tmp_path / "b.html"
        b.write_bytes(b"<html/>")
        c = tmp_path / "c.png"
        c.write_bytes(b"\x89PNG\r\n\x1a\n")
        plan = [
            ImportItem(a, "a.html", 1, "text/html", "brotli"),
            ImportItem(b, "b.html", 1, "text/html", "brotli"),
            ImportItem(c, "c.png", 3, "image/png", "none"),
        ]
        summary = import_content_files(db, plan, 1, "Alice", "content-test")
        assert summary.files_imported == 3
        assert summary.imported_by_mime == {"text/html": 2, "image/png": 1}
    finally:
        db.unlink(missing_ok=True)


def test_import_summary_excludes_skipped_files_from_mime_breakdown(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        # One real file and one missing source — the missing source triggers a
        # read-error skip and should not appear in the mime breakdown.
        a = tmp_path / "a.html"
        a.write_bytes(b"<html/>")
        plan = [
            ImportItem(a, "a.html", 1, "text/html", "brotli"),
            ImportItem(
                tmp_path / "missing.html", "missing.html", 1, "text/html", "brotli"
            ),
        ]
        summary = import_content_files(db, plan, 1, "Alice", "content-test")
        assert summary.files_imported == 1
        assert summary.files_skipped_error == 1
        assert summary.imported_by_mime == {"text/html": 1}
    finally:
        db.unlink(missing_ok=True)


def test_import_handles_missing_source_file(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        item = ImportItem(
            source=tmp_path / "does-not-exist.html",
            base_path="does-not-exist.html",
            content_type_id=1,
            mime="text/html",
            compression="brotli",
        )
        summary = import_content_files(db, [item], 1, "Alice", "content-test")
        assert summary.files_imported == 0
        assert summary.files_skipped_error == 1
        assert _read_content_rows(db) == []
    finally:
        db.unlink(missing_ok=True)


def test_import_deletes_orphans_with_chunks(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            for suffix in ("", "-1", "-2"):
                conn.execute(
                    'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                    (f"a/old.bin{suffix}", b"OLD"),
                )
            # An unrelated row outside the chosen folder must survive.
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("b/keep.html", b"KEEP"),
            )
            conn.commit()
        orphan_ids = _ids_for_paths(
            db, ["a/old.bin", "a/old.bin-1", "a/old.bin-2"]
        )
        new_src = tmp_path / "new.html"
        new_src.write_bytes(b"<html/>")
        item = ImportItem(new_src, "a/new.html", 1, "text/html", "brotli")
        summary = import_content_files(
            db,
            [item],
            1,
            "Alice",
            "content-test",
            orphan_row_ids=orphan_ids,
        )
        # orphans_deleted reflects rows passed in (one logical file = three rows).
        assert summary.orphans_deleted == 3
        assert summary.files_imported == 1
        rows = _read_content_rows(db)
        assert sorted(r[0] for r in rows) == ["a/new.html", "b/keep.html"]
    finally:
        db.unlink(missing_ok=True)


def test_full_import_flow_makes_db_match_folder(tmp_path: Path) -> None:
    """Integration: prescan + import together sync the chosen folder, leave others alone."""
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("a/orphan.html", b"OLD"),
            )
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("b/keep.html", b"KEEP"),
            )
            conn.commit()
        root = tmp_path / "a"
        root.mkdir()
        (root / "new.html").write_bytes(b"<html/>")
        scan = prescan_content_import(db, root, get_content_types(db))
        assert scan.orphans == ["a/orphan.html"]
        assert len(scan.orphan_row_ids) == 1
        summary = import_content_files(
            db,
            scan.mapped,
            1,
            "Alice",
            "content-test",
            orphan_row_ids=scan.orphan_row_ids,
            overwrite_row_ids_by_base=scan.overwrite_row_ids_by_base,
        )
        assert summary.orphans_deleted == 1
        assert summary.files_imported == 1
        rows = _read_content_rows(db)
        assert sorted(r[0] for r in rows) == ["a/new.html", "b/keep.html"]
    finally:
        db.unlink(missing_ok=True)


def test_full_import_flow_preserves_lookalike_paths(tmp_path: Path) -> None:
    """End-to-end safety: prescan classifies foo.html-extra.txt as its own
    file (suffix isn't all digits), so importing folder a/ with only foo.html
    doesn't accidentally fold it INTO foo.html's chunks during overwrite.

    Note: under the 'DB matches folder' contract, the lookalike still gets
    deleted as an orphan. What this test pins is that the *overwrite ids* for
    foo.html don't include the lookalike row."""
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("a/foo.html", b"OLD-FOO"),
            )
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("a/foo.html-extra.txt", b"UNRELATED"),
            )
            conn.commit()
        lookalike_id = _ids_for_paths(db, ["a/foo.html-extra.txt"])[0]
        root = tmp_path / "a"
        root.mkdir()
        (root / "foo.html").write_bytes(b"<html>NEW</html>")
        scan = prescan_content_import(db, root, get_content_types(db))
        assert scan.overwrites == ["a/foo.html"]
        assert scan.orphans == ["a/foo.html-extra.txt"]
        # The crux: the overwrite delete-set for foo.html does NOT include
        # the lookalike id. (If prescan had wrongly folded it in, the
        # lookalike would be classified as a chunk and we'd see its id here.)
        assert lookalike_id not in scan.overwrite_row_ids_by_base["a/foo.html"]
        # And it's separately marked as an orphan to be deleted.
        assert lookalike_id in scan.orphan_row_ids
    finally:
        db.unlink(missing_ok=True)


def test_import_pure_orphan_cleanup_with_no_files_to_import(tmp_path: Path) -> None:
    """Empty folder under a prefix should still trigger orphan cleanup."""
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            for path in ("a/x.html", "a/y.html"):
                conn.execute(
                    'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                    (path, b"OLD"),
                )
            conn.commit()
        root = tmp_path / "a"
        root.mkdir()  # empty
        scan = prescan_content_import(db, root, get_content_types(db))
        assert scan.mapped == []
        assert scan.orphans == ["a/x.html", "a/y.html"]
        summary = import_content_files(
            db,
            scan.mapped,
            1,
            "Alice",
            "content-test",
            orphan_row_ids=scan.orphan_row_ids,
        )
        assert summary.orphans_deleted == 2
        assert _read_content_rows(db) == []
    finally:
        db.unlink(missing_ok=True)


def test_import_progress_callback_reports_three_phases(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("a/orphan.html", b"OLD"),
            )
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("a/overwrite.html", b"OLD"),
            )
            conn.commit()
        orphan_id = _ids_for_paths(db, ["a/orphan.html"])[0]
        overwrite_id = _ids_for_paths(db, ["a/overwrite.html"])[0]
        ow_src = tmp_path / "overwrite.html"
        ow_src.write_bytes(b"<html/>")
        new_src = tmp_path / "new.html"
        new_src.write_bytes(b"<html/>")
        plan = [
            ImportItem(ow_src, "a/overwrite.html", 1, "text/html", "brotli"),
            ImportItem(new_src, "a/new.html", 1, "text/html", "brotli"),
        ]
        progress_log: list[tuple[str, int, int]] = []
        import_content_files(
            db,
            plan,
            1,
            "Alice",
            "content-test",
            orphan_row_ids=[orphan_id],
            overwrite_row_ids_by_base={"a/overwrite.html": [overwrite_id]},
            progress_callback=lambda phase, cur, tot: progress_log.append(
                (phase, cur, tot)
            ),
        )
        # Each phase emits its terminal tick.
        assert ("delete", 1, 1) in progress_log
        assert ("overwrite", 1, 1) in progress_log
        assert ("add", 1, 1) in progress_log
        # Phase order: deletes before any imports; the overwrite item is
        # planned first so its tick precedes the add tick.
        delete_idx = progress_log.index(("delete", 1, 1))
        overwrite_idx = progress_log.index(("overwrite", 1, 1))
        add_idx = progress_log.index(("add", 1, 1))
        assert delete_idx < overwrite_idx < add_idx
    finally:
        db.unlink(missing_ok=True)


def test_import_progress_callback_skipped_when_phase_empty(tmp_path: Path) -> None:
    """No callback fires for a phase with zero work (avoid divide-by-zero in UIs)."""
    db = _make_db_with_content_schema()
    try:
        src = tmp_path / "page.html"
        src.write_bytes(b"<html/>")
        plan = [ImportItem(src, "a/page.html", 1, "text/html", "brotli")]
        seen_phases: set[str] = set()
        import_content_files(
            db,
            plan,
            1,
            "Alice",
            "content-test",
            progress_callback=lambda phase, cur, tot: seen_phases.add(phase),
        )
        # No orphans, no overwrites — only "add" should fire.
        assert seen_phases == {"add"}
    finally:
        db.unlink(missing_ok=True)
