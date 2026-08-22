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
        # A *definitive* answer is cached: docdb_studio never writes a dictionary
        # itself, so one does not appear mid-session under normal use. (An error
        # answer is not cached -- see the locked-database test below.)
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


def test_locked_database_is_not_cached_as_having_no_dictionary() -> None:
    """`sqlite3.OperationalError` also covers "database is locked", which a GUI
    hits whenever another tool writes the same file. Caching that as "no
    dictionary" would downgrade the whole session to plain Brotli: imports would
    write plain rows into a dictionary database and existing rows would fail to
    read."""
    dictionary_data = _train_dictionary([_make_text(150, i) for i in range(60)])
    db = _make_db(with_dictionary=dictionary_data)
    real_connect = docdb_studio.sqlite3.connect
    calls = {"n": 0}

    def flaky_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise docdb_studio.sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    try:
        docdb_studio.sqlite3.connect = flaky_connect
        assert get_compression_dictionary(db) is None      # the transient failure
        assert get_compression_dictionary(db) == dictionary_data   # retried, not cached
    finally:
        docdb_studio.sqlite3.connect = real_connect
        db.unlink(missing_ok=True)


def test_plain_row_in_a_dictionary_database_still_decodes() -> None:
    """A dictionary database still contains plain rows: anything a plugin
    contributes on-device, anything a script outside populate_db.py wrote, and
    everything not yet converted during a partial migration. WebServer.kt falls
    back to a plain decode for exactly this, and docdb-studio has to agree or it
    cannot read rows the app serves fine."""
    dictionary_data = _train_dictionary([_make_text(150, i) for i in range(60)])
    db = _make_db(with_dictionary=dictionary_data)
    try:
        payload = _make_text(400, seed=4242)
        plain_row = brotli.compress(payload)
        assert docdb_studio.decompress_brotli(plain_row, db) == payload
    finally:
        db.unlink(missing_ok=True)


def test_missing_brotli_cli_surfaces_as_brotli_error() -> None:
    """Every existing call site guards decoding with `except brotli.error`, so a
    missing binary has to arrive as one rather than as an unhandled RuntimeError
    out of content preview."""
    dictionary_data = _train_dictionary([_make_text(150, i) for i in range(60)])
    db = _make_db(with_dictionary=dictionary_data)
    real_which = docdb_studio.shutil.which
    try:
        docdb_studio.shutil.which = lambda name: None
        with pytest.raises(brotli.error) as excinfo:
            docdb_studio.decompress_brotli(b"anything", db)
        assert "brotli" in str(excinfo.value)
    finally:
        docdb_studio.shutil.which = real_which
        db.unlink(missing_ok=True)


def test_missing_brotli_cli_is_reported_not_swallowed(capsys) -> None:
    """A missing binary and a corrupt row both leave the preview empty, but they
    mean different things: one bad row versus nothing in this database will ever
    decode, fixable in one command. The call sites log the second."""
    dictionary_data = _train_dictionary([_make_text(150, i) for i in range(60)])
    db = _make_db(with_dictionary=dictionary_data)
    real_which = docdb_studio.shutil.which
    try:
        html = b"<html><body><p>needs the CLI</p></body></html>"
        _insert_content(db, "docs/page.html", compress_for_storage(html, "brotli", db))
        docdb_studio.shutil.which = lambda name: None

        assert fetch_content_for_path(db, "docs/page.html") is None
        assert docdb_studio.get_html_anchors_for_path(db, "docs/page.html") == []

        message = capsys.readouterr().err
        assert "docs/page.html" in message
        assert "README" in message          # tells the reader where to go
        assert message.count("error:") == 2  # once per call site, not swallowed
    finally:
        docdb_studio.shutil.which = real_which
        db.unlink(missing_ok=True)


# ---------- ADFA-5220: refuse a database this build cannot read ----------


def _set_version(db: Path, major: int, minor: int = 0, patch: int = 0) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS DocumentationDatabaseVersion (
              major INT NOT NULL, minor INT NOT NULL, patch INT NOT NULL,
              who TEXT NOT NULL, comment TEXT NOT NULL,
              changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            "INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) "
            "VALUES (?, ?, ?, 'test', 'test')",
            (major, minor, patch),
        )
        conn.commit()


def test_a_database_with_no_version_table_reads_as_unversioned() -> None:
    db = _make_db()
    try:
        assert docdb_studio.database_major_version(db) is None
    finally:
        db.unlink(missing_ok=True)


def test_the_declared_major_is_the_row_inserted_last() -> None:
    """The table is an append-only log, so a rebuild from an older pipeline is a
    downgrade and has to read as one -- the same rule the app's resolver uses."""
    db = _make_db()
    try:
        _set_version(db, 3)
        _set_version(db, 2)
        assert docdb_studio.database_major_version(db) == 2
    finally:
        db.unlink(missing_ok=True)


def test_a_supported_or_older_version_is_not_refused() -> None:
    """Older formats are strictly simpler -- no shared dictionary -- and
    decompress_brotli's plain fallback already reads them."""
    db = _make_db()
    try:
        for major in (1, docdb_studio.SUPPORTED_DATABASE_MAJOR_VERSION):
            _set_version(db, major)
            assert docdb_studio.database_major_version(db) <= docdb_studio.SUPPORTED_DATABASE_MAJOR_VERSION
    finally:
        db.unlink(missing_ok=True)


def test_a_newer_major_produces_an_actionable_refusal() -> None:
    db = _make_db()
    try:
        newer = docdb_studio.SUPPORTED_DATABASE_MAJOR_VERSION + 1
        _set_version(db, newer)
        assert docdb_studio.database_major_version(db) == newer

        message = docdb_studio.unsupported_version_error(db, newer)
        assert str(newer) in message
        assert str(docdb_studio.SUPPORTED_DATABASE_MAJOR_VERSION) in message
        # says what to do, not just what is wrong
        assert "Update docdb-studio" in message
    finally:
        db.unlink(missing_ok=True)
