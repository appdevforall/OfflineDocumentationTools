"""Tests for shared-dictionary Brotli compression in docdb_studio.py (ADFA-5153)."""

import random
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import brotli
import pytest

import docdb_studio

get_compression_dictionary = docdb_studio.get_compression_dictionary
compress_for_storage = docdb_studio.compress_for_storage
decompress_brotli = docdb_studio.decompress_brotli
get_html_anchors_for_path = docdb_studio.get_html_anchors_for_path
fetch_content_for_path = docdb_studio.fetch_content_for_path

WORDS = [
    "kotlin", "class", "fun", "val", "var", "override", "interface", "object", "companion",
    "sidebar", "nav", "template", "docs-sidebar", "toc-element", "page.peb", "Content-Type",
]


def _make_text(word_count: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return (" ".join(rng.choice(WORDS) for _ in range(word_count))).encode("utf-8")


def _train_dictionary(samples: list, dict_size: int = 16384) -> bytes:
    """Test-only dictionary trainer (docdb_studio.py never trains one itself -- see
    its own module docstring/AGENTS.md: it only ever reads a dictionary another tool
    already produced)."""
    zstd_path = shutil.which("zstd")
    assert zstd_path is not None, "zstd must be on PATH to run this test"
    work_dir = Path(tempfile.mkdtemp(prefix="docdb-studio-test-dict-"))
    try:
        sample_paths = []
        for i, sample in enumerate(samples):
            p = work_dir / f"sample_{i:03}.bin"
            p.write_bytes(sample)
            sample_paths.append(str(p))
        dict_path = work_dir / "dictionary.bin"
        subprocess.run(
            [zstd_path, "--train-fastcover", f"--maxdict={dict_size}", "-o", str(dict_path), *sample_paths],
            check=True, capture_output=True,
        )
        return dict_path.read_bytes()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _make_db(with_dictionary: bytes | None = None) -> Path:
    """Temp DB with Content/ContentTypes/Languages, and CompressionDictionary
    populated iff with_dictionary is given."""
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
            INSERT INTO Languages (id, value) VALUES (1, 'en');
            INSERT INTO ContentTypes (id, value, compression) VALUES (1, 'text/html', 'brotli');
            """
        )
        if with_dictionary is not None:
            conn.execute(
                "CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL)"
            )
            conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (with_dictionary,))
        conn.commit()
    return p


def _insert_content(db: Path, path: str, blob: bytes, content_type_id: int = 1) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, ?)',
            (path, blob, content_type_id),
        )
        conn.commit()


# ---------- get_compression_dictionary ----------


def test_returns_none_when_table_missing() -> None:
    db = _make_db()
    try:
        assert get_compression_dictionary(db) is None
    finally:
        db.unlink(missing_ok=True)


def test_returns_stored_dictionary_bytes() -> None:
    dictionary_data = _train_dictionary([_make_text(150, i) for i in range(60)])
    db = _make_db(with_dictionary=dictionary_data)
    try:
        assert get_compression_dictionary(db) == dictionary_data
    finally:
        db.unlink(missing_ok=True)


def test_caches_per_db_path() -> None:
    db = _make_db()
    try:
        assert get_compression_dictionary(db) is None
        # Mutating the row after the first (cached) lookup must not change the
        # cached result -- docdb_studio never expects a dictionary to appear or
        # change mid-session, since it never writes one itself.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL)"
            )
            conn.execute("INSERT INTO CompressionDictionary (id, data) VALUES (1, ?)", (b"late-arriving",))
            conn.commit()
        assert get_compression_dictionary(db) is None
    finally:
        db.unlink(missing_ok=True)


# ---------- compress_for_storage / decompress_brotli ----------


def test_round_trip_without_dictionary_matches_plain_brotli() -> None:
    db = _make_db()
    try:
        data = b"hello world " * 500
        compressed = compress_for_storage(data, "brotli", db)
        assert brotli.decompress(compressed) == data  # plain brotli, no dictionary involved
        assert decompress_brotli(compressed, db) == data
    finally:
        db.unlink(missing_ok=True)


def test_round_trip_with_dictionary() -> None:
    dictionary_data = _train_dictionary([_make_text(150, i) for i in range(60)])
    db = _make_db(with_dictionary=dictionary_data)
    try:
        data = _make_text(150, 9999)
        compressed = compress_for_storage(data, "brotli", db)
        with pytest.raises(brotli.error):
            brotli.decompress(compressed)  # plain decode of dictionary-compressed data fails
        assert decompress_brotli(compressed, db) == data
    finally:
        db.unlink(missing_ok=True)


def test_none_compression_passes_through_unchanged() -> None:
    db = _make_db()
    try:
        data = b"\x00\x01\x02 raw bytes"
        assert compress_for_storage(data, "none", db) == data
    finally:
        db.unlink(missing_ok=True)


# ---------- get_html_anchors_for_path / fetch_content_for_path against a real dictionary ----------


def test_get_html_anchors_for_path_decodes_dictionary_compressed_html() -> None:
    dictionary_data = _train_dictionary([_make_text(150, i) for i in range(60)])
    db = _make_db(with_dictionary=dictionary_data)
    try:
        html = b'<html><body><h1 id="intro">Intro</h1><p id="p1">x</p></body></html>'
        blob = compress_for_storage(html, "brotli", db)
        _insert_content(db, "docs/page.html", blob)
        assert get_html_anchors_for_path(db, "docs/page.html") == ["intro", "p1"]
    finally:
        db.unlink(missing_ok=True)


def test_fetch_content_for_path_decodes_dictionary_compressed_content() -> None:
    dictionary_data = _train_dictionary([_make_text(150, i) for i in range(60)])
    db = _make_db(with_dictionary=dictionary_data)
    try:
        html = b"<html><body><p>served via dictionary</p></body></html>"
        blob = compress_for_storage(html, "brotli", db)
        _insert_content(db, "docs/page.html", blob)
        result = fetch_content_for_path(db, "docs/page.html")
        assert result == (html, "text/html")
    finally:
        db.unlink(missing_ok=True)
