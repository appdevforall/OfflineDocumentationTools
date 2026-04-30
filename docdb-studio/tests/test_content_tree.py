"""Tests for content-tree helpers in docdb_studio.py."""

import sqlite3
import tempfile
from pathlib import Path

import docdb_studio

format_bytes = docdb_studio.format_bytes
build_content_tree = docdb_studio.build_content_tree
get_paths_with_sizes = docdb_studio.get_paths_with_sizes
get_button_reference_counts = docdb_studio.get_button_reference_counts


def _make_db_with_minimal_schema() -> Path:
    """Create a temp DB with Content + TooltipButtons + supporting tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.executescript(
            """
            CREATE TABLE Languages (id INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT, compression TEXT);
            CREATE TABLE "Content" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                languageID INTEGER NOT NULL,
                content BLOB NOT NULL,
                contentTypeID INTEGER NOT NULL
            );
            CREATE TABLE TooltipButtons (
                tooltipId INTEGER,
                buttonNumberId INTEGER,
                description TEXT,
                uri TEXT
            );
            INSERT INTO Languages (id, value) VALUES (1, 'en');
            INSERT INTO ContentTypes (id, value, compression) VALUES (1, 'text/html', 'brotli');
            """
        )
        conn.commit()
    return p


# ---------- format_bytes ----------


def test_format_bytes_zero() -> None:
    assert format_bytes(0) == "0 B"


def test_format_bytes_under_1k_stays_in_bytes() -> None:
    assert format_bytes(1) == "1 B"
    assert format_bytes(999) == "999 B"
    assert format_bytes(1023) == "1023 B"


def test_format_bytes_kb_threshold() -> None:
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(1024 * 1024 - 1).endswith("KB")


def test_format_bytes_mb_and_gb() -> None:
    assert format_bytes(1024 * 1024) == "1.0 MB"
    assert format_bytes(int(1.4 * 1024 * 1024)) == "1.4 MB"
    assert format_bytes(2 * 1024 * 1024 * 1024).endswith("GB")


# ---------- build_content_tree ----------


def test_build_content_tree_empty_input() -> None:
    tree = build_content_tree([])
    assert tree == {"children": {}, "files": {}, "size": 0}


def test_build_content_tree_root_files_only() -> None:
    tree = build_content_tree([("a.html", 100), ("b.html", 200)])
    assert tree["files"] == {"a.html": 100, "b.html": 200}
    assert tree["children"] == {}
    assert tree["size"] == 300


def test_build_content_tree_groups_by_segment_and_aggregates_sizes() -> None:
    tree = build_content_tree(
        [
            ("a/x.html", 100),
            ("a/b/y.html", 200),
            ("a/b/z.html", 50),
            ("c.html", 10),
        ]
    )
    assert tree["files"] == {"c.html": 10}
    assert tree["size"] == 360
    a = tree["children"]["a"]
    assert a["files"] == {"x.html": 100}
    assert a["size"] == 350
    b = a["children"]["b"]
    assert b["files"] == {"y.html": 200, "z.html": 50}
    assert b["size"] == 250
    assert b["children"] == {}


def test_build_content_tree_skips_empty_paths() -> None:
    tree = build_content_tree([("", 100), ("a.html", 50)])
    assert tree["files"] == {"a.html": 50}
    assert tree["size"] == 50


# ---------- get_paths_with_sizes ----------


def test_get_paths_with_sizes_returns_pairs() -> None:
    db = _make_db_with_minimal_schema()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("a/foo.html", b"hello"),
            )
            conn.execute(
                'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, 1)',
                ("b/bar.html", b"hi"),
            )
            conn.commit()
        rows = sorted(get_paths_with_sizes(db))
        assert rows == [("a/foo.html", 5), ("b/bar.html", 2)]
    finally:
        db.unlink(missing_ok=True)


# ---------- get_button_reference_counts ----------


def test_get_button_reference_counts_aggregates() -> None:
    db = _make_db_with_minimal_schema()
    try:
        with sqlite3.connect(db) as conn:
            for uri in [
                "docs/intro.html",
                "docs/intro.html",
                "docs/intro.html#section",
                "docs/other.html?q=1",
            ]:
                conn.execute(
                    "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (1, 1, 'd', ?)",
                    (uri,),
                )
            conn.commit()
        counts = get_button_reference_counts(db)
        assert counts == {"docs/intro.html": 3, "docs/other.html": 1}
    finally:
        db.unlink(missing_ok=True)


def test_get_button_reference_counts_ignores_empty() -> None:
    db = _make_db_with_minimal_schema()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (1, 1, 'd', '')"
            )
            conn.execute(
                "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (1, 2, 'd', NULL)"
            )
            conn.execute(
                "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri) VALUES (1, 3, 'd', 'real.html')"
            )
            conn.commit()
        counts = get_button_reference_counts(db)
        assert counts == {"real.html": 1}
    finally:
        db.unlink(missing_ok=True)
