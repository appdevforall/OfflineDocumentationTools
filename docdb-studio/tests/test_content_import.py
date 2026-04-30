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
ImportItem = docdb_studio.ImportItem


def _make_db_with_content_schema() -> Path:
    """Create a temp DB seeded with Content/ContentTypes/Languages/LastChange schema and a few rows."""
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
        assert scan.conflicts == []
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


def test_prescan_detects_base_path_conflict(tmp_path: Path) -> None:
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
        assert scan.mapped == []
        assert len(scan.conflicts) == 1
        assert scan.conflicts[0][1] == "myroot/page.html"
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


# ---------- import_content_files ----------


def _read_content_rows(db: Path) -> list[tuple[str, int, bytes, int]]:
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            'SELECT path, languageID, content, contentTypeID FROM "Content" ORDER BY path'
        )
        return cur.fetchall()


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


def test_import_skips_file_when_chunk_path_conflicts(tmp_path: Path) -> None:
    db = _make_db_with_content_schema()
    try:
        # Pre-seed the chunk path so the import has a conflict on the second chunk.
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 3)',
                ("big.bin-1", b"\x00"),
            )
            conn.commit()
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
        summary = import_content_files(db, [item], 1, "Alice", "content-test")
        assert summary.files_imported == 0
        assert summary.files_skipped_chunk_conflict == 1
        # No rows inserted for big.bin or big.bin-1 (other than the pre-seeded one).
        rows = _read_content_rows(db)
        assert len(rows) == 1
        assert rows[0][0] == "big.bin-1"  # the pre-seeded one
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
        # Pre-seed a conflict so one html file gets skipped.
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("a.html", b"\x00"),
            )
            conn.commit()
        a = tmp_path / "a.html"
        a.write_bytes(b"<html/>")
        b = tmp_path / "b.html"
        b.write_bytes(b"<html/>")
        plan = [
            ImportItem(a, "a.html", 1, "text/html", "brotli"),
            ImportItem(b, "b.html", 1, "text/html", "brotli"),
        ]
        summary = import_content_files(db, plan, 1, "Alice", "content-test")
        assert summary.files_imported == 1
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
