"""Tests for fetch_content_for_path and the built-in HTTP server."""

import sqlite3
import tempfile
import urllib.request
from pathlib import Path

import brotli
import pytest

import docdb_studio

fetch_content_for_path = docdb_studio.fetch_content_for_path
start_content_web_server = docdb_studio.start_content_web_server


def _make_db_with_content(rows: list[tuple[str, bytes, str, str]]) -> Path:
    """rows: [(path, raw_bytes, mime, compression)]. Compression='brotli' or 'none'."""
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.executescript(
            """
            CREATE TABLE Languages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL UNIQUE
            );
            CREATE TABLE ContentTypes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL UNIQUE,
                compression TEXT NOT NULL
            );
            CREATE TABLE Content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                languageID INTEGER NOT NULL,
                content BLOB NOT NULL,
                contentTypeID INTEGER NOT NULL,
                UNIQUE (path)
            );
            INSERT INTO Languages (id, value) VALUES (1, 'en');
            """
        )
        # Build ContentTypes from the distinct (mime, compression) tuples in rows.
        ct_ids: dict[tuple[str, str], int] = {}
        for _, _, mime, comp in rows:
            key = (mime, comp)
            if key in ct_ids:
                continue
            cur = conn.execute(
                "INSERT INTO ContentTypes (value, compression) VALUES (?, ?)",
                (mime, comp),
            )
            ct_ids[key] = cur.lastrowid or 0
        for db_path, raw, mime, comp in rows:
            stored = brotli.compress(raw) if comp == "brotli" else raw
            conn.execute(
                "INSERT INTO Content (path, languageID, content, contentTypeID) VALUES (?, ?, ?, ?)",
                (db_path, 1, stored, ct_ids[(mime, comp)]),
            )
        conn.commit()
    return p


def test_fetch_content_for_path_returns_decompressed_bytes() -> None:
    db = _make_db_with_content(
        [("hello.html", b"<h1>hi</h1>", "text/html", "brotli")]
    )
    try:
        result = fetch_content_for_path(db, "hello.html")
        assert result is not None
        body, mime = result
        assert body == b"<h1>hi</h1>"
        assert mime == "text/html"
    finally:
        db.unlink(missing_ok=True)


def test_fetch_content_for_path_assembles_chunks() -> None:
    raw = b"AB" + b"CD" + b"EF"  # logical content split across 3 stored rows
    db = _make_db_with_content(
        [
            ("big.bin", b"AB", "application/octet-stream", "none"),
            ("big.bin-1", b"CD", "application/octet-stream", "none"),
            ("big.bin-2", b"EF", "application/octet-stream", "none"),
        ]
    )
    try:
        result = fetch_content_for_path(db, "big.bin")
        assert result is not None
        body, mime = result
        assert body == raw
        assert mime == "application/octet-stream"
    finally:
        db.unlink(missing_ok=True)


def test_fetch_content_for_path_missing_returns_none() -> None:
    db = _make_db_with_content(
        [("present.txt", b"x", "text/plain", "none")]
    )
    try:
        assert fetch_content_for_path(db, "absent.txt") is None
    finally:
        db.unlink(missing_ok=True)


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_server_serves_content() -> None:
    db = _make_db_with_content(
        [("page.html", b"<p>served</p>", "text/html", "brotli")]
    )
    port = _free_port()
    server = start_content_web_server(db, port=port)
    assert server is not None
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/page.html", timeout=5
        ) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "text/html"
            body = resp.read()
        assert body == b"<p>served</p>"
    finally:
        server.shutdown()
        server.server_close()
        db.unlink(missing_ok=True)


def test_http_server_returns_404_for_missing() -> None:
    db = _make_db_with_content(
        [("present.txt", b"x", "text/plain", "none")]
    )
    port = _free_port()
    server = start_content_web_server(db, port=port)
    assert server is not None
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/missing.txt", timeout=5
            )
        assert exc_info.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        db.unlink(missing_ok=True)


def test_http_server_handles_url_quoted_paths() -> None:
    db = _make_db_with_content(
        [("dir/with space.txt", b"ok", "text/plain", "none")]
    )
    port = _free_port()
    server = start_content_web_server(db, port=port)
    assert server is not None
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/dir/with%20space.txt", timeout=5
        ) as resp:
            assert resp.read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()
        db.unlink(missing_ok=True)
