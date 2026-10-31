#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "pytest",
# ]
# ///
"""
test_store_pages_in_blocks.py

Covers store_pages_in_blocks.py on small synthetic databases. On the first,
three pages to a block: each of the five sets in path order with its own
blocks, the last one shorter, a/devsite/ and i/ rows taken whether templated
or not; rows held in more than one row left alone; every item sliced from its
block equal to its row's decoded bytes; blocked rows emptied, 4.0.0 declared
and nothing else changed; a second run, no version, 1.x, 2.x and 4.x refused;
a set decoded in several windows, and one page to a block so that blocks are
written while more are pending, each blocked whole and in order; an empty row
left a row; each block decoded once; --workers below 1 refused; a dry run, a
block that does not read back byte for byte or decodes longer than its items,
and, before the commit, a lost block with or without its items, a changed
block, a moved item and a blocked row left full, each writing nothing. On the
second, with the default 64: an item over 1 MiB decoded kept as a row, and a
block closed before it would decode past 4 MiB.

    uv run --with pytest --with-requirements requirements.txt python -m pytest -q scripts/test_store_pages_in_blocks.py
"""
from __future__ import annotations

import hashlib
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_db_media as opt  # noqa: E402
import store_pages_in_blocks as spb  # noqa: E402

PER_BLOCK = 3
# The cap, written out rather than read from the script, so a script with another cap fails here.
ITEM_CAP = 1024 * 1024
BLOCK_CAP = 4 * 1024 * 1024
DICTIONARY = b'{"title": "", "members": [{"name": "", "summary": ""}]} <div class="devsite-article-body">' * 64

# (path, content type, templateId). Inserted in this order, so ids do not follow paths.
BLOCKED = {
    "a/": [("a/z.html", "text/html", 7), ("a/android/widget/TextView.html", "text/html", 7),
           # Looks like a continuation row, but a/z.html is not 1 MiB, so it is a page of its own.
           ("a/z.html-1", "text/html", 7), ("a/androidx/a.html", "text/html", 7),
           ("a/Zeta.html", "text/html", 7), ("a/androidx/B.html", "text/html", 7),
           ("a/android/util/Pair.html", "text/html", 7)],
    "j/": [("j/index.html", "text/html", 0), ("j/api/b.html", "text/html", 0),
           ("j/Overview.html", "text/html", 0), ("j/api/A.html", "text/html", 0),
           # A templated javadoc JSON row: sync_javadoc_json_to_db.py keeps text/html and sets templateId.
           ("j/api/T.html", "text/html", 7)],
    "k/": [("k/html/y.html", "text/html", 9), ("k/html/x.html", "text/html", 8)],
    # Every Brotli row under a/devsite/ and i/, templated or not, HTML or not.
    "a/devsite/": [("a/devsite/reference/x.html", "text/html", 7), ("a/devsite/b.html", "text/html", 0),
                   ("a/devsite/style.css", "text/css", 0), ("a/devsite/Z.js", "text/javascript", 0)],
    "i/": [("i/topic.html", "text/html", 0), ("i/jstopics/topic.js", "text/javascript", 0),
           ("i/hm-topic.html", "text/html", 8)],
}
LEFT = [
    ("a/plain.html", "text/html", 0),  # a/ without a template
    ("a/raw.json", "text", 7),  # its type is stored uncompressed
    ("A/Upper.html", "text/html", 7),  # not under a/: GLOB tells case apart, as paths do, where LIKE would not
    ("j/style.css", "text/css", 0),  # not HTML
    ("j/logo.png", "image/png", 0),
    ("k/html/nav.html", "text/html", 0),  # no template
    ("a/devsite/logo.png", "image/png", 0),  # under a/devsite/, but not Brotli
    ("i/img.png", "image/png", 0),  # under i/, but not Brotli
    ("d/other.html", "text/html", 7),  # in no set
]
# Each stored as a 1 MiB row and a continuation row: in a set, but left as they are.
CHUNKED = [("a/big.html", "text/html", 7), ("i/book.pdf", "application/pdf", 0)]
TYPES = [("text/html", "brotli"), ("text/css", "brotli"), ("text/javascript", "brotli"),
         ("application/pdf", "brotli"), ("text", "none"), ("image/png", "none")]
SCHEMA = """
CREATE TABLE Languages (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE);
CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL UNIQUE,
    compression TEXT NOT NULL);
CREATE TABLE CompressionDictionary (id INTEGER PRIMARY KEY CHECK (id = 1), data BLOB NOT NULL);
CREATE TABLE Templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content BLOB NOT NULL,
    UNIQUE(name));
CREATE TABLE Content (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, languageID INTEGER NOT NULL,
    content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, templateId INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (languageID) REFERENCES Languages(id), FOREIGN KEY (contentTypeID) REFERENCES ContentTypes(id),
    UNIQUE(path)
);
CREATE TABLE Bookshelf (contentID INTEGER NOT NULL, title STRING DEFAULT '');
CREATE TABLE DocumentationDatabaseVersion (
  major INT NOT NULL, minor INT NOT NULL, patch INT NOT NULL, who TEXT NOT NULL, comment TEXT NOT NULL,
  changeTime TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO DocumentationDatabaseVersion VALUES (2, 0, 0, 'ADFA-5220', 'dictionary', '2026-08-20 00:00:00');
INSERT INTO DocumentationDatabaseVersion VALUES (3, 0, 0, 'ADFA-3305', 'keyed tooltips', '2026-10-20 00:00:00');
"""
VERSION_ROWS = "SELECT major, minor, patch, who, comment FROM DocumentationDatabaseVersion ORDER BY rowid"


def _brotli(args: list, data: bytes, dictionary: Path) -> bytes:
    return subprocess.run(["brotli", *args, "-D", str(dictionary)], input=data, stdout=subprocess.PIPE,
                          check=True).stdout


def _plain(path: str, rng: random.Random) -> bytes:
    words = " ".join(rng.choice(["member", "summary", "inherited", "View", "int", "String"])
                     for _ in range(rng.randrange(20, 400)))
    return f'{{"path": "{path}", "members": [{{"name": "{words}"}}]}}'.encode()


def _sized(path: str, size: int) -> bytes:
    """`size` bytes naming `path`, cheap to compress at any quality."""
    head = f'{{"path": "{path}", "text": "'.encode()
    return (head + b"m" * size)[:size]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create(db_path: Path) -> tuple:
    """A new database with SCHEMA, its types and templates; returns (connection, {content type: (id,
    compression)}, the dictionary as a file)."""
    dictionary = db_path.with_name("dict.bin")
    dictionary.write_bytes(DICTIONARY)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO Languages (value) VALUES ('en')")
    conn.executemany("INSERT INTO ContentTypes (value, compression) VALUES (?, ?)", TYPES)
    conn.execute("INSERT INTO CompressionDictionary VALUES (1, ?)", (DICTIONARY,))
    conn.executemany("INSERT INTO Templates (id, name, content) VALUES (?, ?, ?)",
                     [(7, "android-reference.peb", b"t7"), (8, "page.peb", b"t8"), (9, "nav.peb", b"t9")])
    types = {value: (type_id, compression) for type_id, value, compression in conn.execute("SELECT * FROM ContentTypes")}
    return conn, types, dictionary


def _insert(conn, types: dict, dictionary: Path, path: str, content_type: str, template_id: int, plain: bytes,
            quality: str = "5") -> None:
    type_id, compression = types[content_type]
    stored = _brotli(["-c", "-q", quality], plain, dictionary) if compression == "brotli" else plain
    conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) VALUES (?, 1, ?, ?, ?)",
                 (path, stored, type_id, template_id))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """(database, {path: plain bytes}): the pristine synthetic database."""
    db_path = tmp_path_factory.mktemp("blocks") / "documentation.db"
    rng = random.Random(6683)
    plain = {}
    conn, types, dictionary = _create(db_path)
    pages = [page for group in BLOCKED.values() for page in group] + LEFT
    rng.shuffle(pages)
    for path, content_type, template_id in pages:
        plain[path] = _plain(path, rng)
        _insert(conn, types, dictionary, path, content_type, template_id, plain[path])
    # One Brotli stream split across "<path>" and "<path>-1", as a real chunked row is.
    for path, content_type, template_id in CHUNKED:
        plain[path] = rng.randbytes(opt.CHUNK_SIZE + 100_000)
        stored = _brotli(["-c", "-q", "0"], plain[path], dictionary)
        assert len(stored) > opt.CHUNK_SIZE
        for part, chunk in ((path, stored[:opt.CHUNK_SIZE]), (f"{path}-1", stored[opt.CHUNK_SIZE:])):
            conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                         "VALUES (?, 1, ?, ?, ?)", (part, chunk, types[content_type][0], template_id))
    conn.execute("INSERT INTO Bookshelf (contentID, title) SELECT id, 'Pair' FROM Content "
                 "WHERE path = 'a/android/util/Pair.html'")
    conn.commit()
    conn.close()
    return db_path, plain


@pytest.fixture
def database(built, tmp_path):
    """A fresh copy of the synthetic database, and the plain bytes of its pages."""
    db_path, plain = built
    copy = tmp_path / "documentation.db"
    shutil.copyfile(db_path, copy)
    return copy, plain


# The cap's database, in path order within each set:
#   a/: one item 1 byte over the item cap (kept), one exactly at it and a small one (one block);
#   j/: four items at the item cap, exactly the block cap (one block), then three at the item cap and one of half,
#       3.5 MiB, closed before the next item, half and a byte, would take it past the block cap;
#   k/: 65 small items, 64 to a block by default.
CAP_PAGES = (
    [("a/cap/huge.html", "text/html", 7, ITEM_CAP + 1), ("a/cap/limit.html", "text/html", 7, ITEM_CAP),
     ("a/cap/small.html", "text/html", 7, 100)]
    + [(f"j/b{n}.html", "text/html", 0, ITEM_CAP) for n in range(4)]
    + [(f"j/c{n}.html", "text/html", 0, ITEM_CAP) for n in range(3)]
    + [("j/c3.html", "text/html", 0, ITEM_CAP // 2), ("j/c4.html", "text/html", 0, ITEM_CAP // 2 + 1)]
    + [(f"k/{n:02}.html", "text/html", 9, 200) for n in range(65)]
)


@pytest.fixture(scope="module")
def cap_built(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("cap") / "documentation.db"
    conn, types, dictionary = _create(db_path)
    plain = {}
    for path, content_type, template_id, size in CAP_PAGES:
        plain[path] = _sized(path, size)
        _insert(conn, types, dictionary, path, content_type, template_id, plain[path], quality="1")
    conn.commit()
    conn.close()
    return db_path, plain


@pytest.fixture
def cap_database(cap_built, tmp_path):
    db_path, plain = cap_built
    copy = tmp_path / "documentation.db"
    shutil.copyfile(db_path, copy)
    return copy, plain


def _run(db_path: Path, dry_run: bool = False, per_block: int = PER_BLOCK) -> int:
    return spb.run({"db_path": db_path, "pages_per_block": per_block, "dry_run": dry_run, "workers": 4})


def _blocks(db_path: Path) -> list:
    """[(decoded block, [(path, offset, length)] by offset)] in block-id order."""
    dictionary = db_path.with_name("read.dict.bin")
    conn = sqlite3.connect(db_path)
    try:
        dictionary.write_bytes(conn.execute("SELECT data FROM CompressionDictionary").fetchone()[0])
        blocks = []
        for block_id, data in conn.execute("SELECT id, data FROM ContentBlocks ORDER BY id").fetchall():
            items = conn.execute("SELECT c.path, i.offset, i.length FROM ContentBlockItems AS i JOIN Content AS c "
                                 "ON c.id = i.contentId WHERE i.blockId = ? ORDER BY i.offset", (block_id,)).fetchall()
            blocks.append((_brotli(["-d", "-c"], data, dictionary), items))
        return blocks
    finally:
        conn.close()


def _block_paths(db_path: Path) -> list:
    """[[path, ...] by offset] in block-id order."""
    return [[path for path, _offset, _length in items] for _data, items in _blocks(db_path)]


def _tables(db_path: Path) -> dict:
    """Every row of every table, by table. Content's columns: id, path,
    languageID, content, contentTypeID, templateId."""
    conn = sqlite3.connect(db_path)
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {name: sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall()) for name in names
                if not name.startswith("ContentBlock")}
    finally:
        conn.close()


def _query(db_path: Path, sql: str, args=()) -> list:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _execute(db_path: Path, *statements: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for sql in statements:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _in_path_order(paths) -> list:
    """ORDER BY path in SQLite's BINARY collation: by UTF-8 bytes."""
    return sorted(paths, key=lambda path: path.encode("utf-8"))


def _set_of(path: str) -> str:
    return "a/devsite/" if path.startswith("a/devsite/") else path[:2]


def _assert_nothing_written(db_path: Path, sha: str) -> None:
    assert _sha(db_path) == sha
    assert not db_path.with_name(db_path.name + "-journal").exists()
    assert _query(db_path, "SELECT name FROM sqlite_master WHERE name LIKE 'ContentBlock%'") == []


def _blocked_in_order() -> list:
    """Every blocked path, set by set in SETS order, each set in path order: the order the items are written in."""
    return [path for name, _condition in spb.SETS for path in _in_path_order(p for p, _t, _i in BLOCKED[name])]


def _assert_every_slice_is_its_page(db_path: Path, plain: dict) -> None:
    for data, items in _blocks(db_path):
        for path, offset, length in items:
            assert data[offset:offset + length] == plain[path], path


def test_blocks_follow_path_order_and_never_mix_sets(database):
    db_path, _plain = database
    assert _run(db_path) == 0
    blocks = _blocks(db_path)
    expected = _blocked_in_order()
    assert [path for _data, items in blocks for path, _offset, _length in items] == expected
    assert [name for name, _condition in spb.SETS] == ["a/", "j/", "k/", "a/devsite/", "i/"]
    # j/ takes text/html whatever its templateId: the javadoc JSON rows are templated.
    assert "j/api/T.html" in expected
    # Binary, not case-blind: a/Zeta.html sorts before a/android/..., a/androidx/B before a/androidx/a.
    assert expected[:3] == ["a/Zeta.html", "a/android/util/Pair.html", "a/android/widget/TextView.html"]
    for data, items in blocks:
        assert len({_set_of(path) for path, _offset, _length in items}) == 1
        offset = 0
        for _path, start, length in items:
            assert start == offset
            offset += length
        assert offset == len(data)


def test_the_last_block_of_a_set_is_shorter(database):
    db_path, _plain = database
    assert _run(db_path) == 0
    # a/ has 7 pages, j/ 5, k/ 2, a/devsite/ 4 and i/ 3: each set starts a block of its own.
    assert [len(items) for _data, items in _blocks(db_path)] == [3, 3, 1, 3, 2, 2, 3, 1, 3]


def test_the_summary_names_every_set_its_content_types_and_what_it_left(database, capsys):
    db_path, _plain = database
    assert _run(db_path) == 0
    out = capsys.readouterr().out
    for name, pages, blocks in (("a/", 7, 3), ("j/", 5, 2), ("k/", 2, 1), ("a/devsite/", 4, 2), ("i/", 3, 1)):
        assert f"  {name}: {pages} page(s) in {blocks} block(s);" in out
    devsite = next(line for line in out.splitlines() if line.startswith("  a/devsite/: 4 page(s)"))
    assert all(kind in devsite for kind in ("text/html 2", "text/css 1", "text/javascript 1"))
    assert ("  i/: left as rows: 0 page(s) over 1,048,576 bytes decoded, 1 page(s) held in more than one row, "
            "1 continuation row(s), and 0 empty row(s).") in out
    assert "Stored 21 page(s) in 9 block(s) of up to 3 pages and 4,194,304 bytes decoded" in out


def test_an_empty_row_stays_a_row(database, capsys):
    """An empty Brotli row has nothing to decode (`brotli -d` calls it corrupt input), so it is left as it is, and
    every other page of its set is blocked as usual."""
    db_path, plain = database
    _execute(db_path, "INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                      "SELECT 'a/empty.html', 1, x'', id, 7 FROM ContentTypes WHERE value = 'text/html'")
    assert _run(db_path) == 0
    assert [path for paths in _block_paths(db_path) for path in paths] == _blocked_in_order()
    _assert_every_slice_is_its_page(db_path, plain)
    assert _query(db_path, "SELECT c.content FROM Content AS c LEFT JOIN ContentBlockItems AS i "
                           "ON i.contentId = c.id WHERE c.path = 'a/empty.html' AND i.contentId IS NULL") == [(b"",)]
    out = capsys.readouterr().out
    assert ("  a/: left as rows: 0 page(s) over 1,048,576 bytes decoded, 1 page(s) held in more than one row, "
            "1 continuation row(s), and 1 empty row(s).") in out


def test_continuation_rows_are_left_alone(database):
    db_path, plain = database
    before = _tables(db_path)["Content"]
    assert _run(db_path) == 0
    blocked = {path for paths in _block_paths(db_path) for path in paths}
    assert "a/z.html-1" in blocked
    after = _tables(db_path)["Content"]
    dictionary = db_path.with_name("chunked.dict.bin")
    dictionary.write_bytes(DICTIONARY)
    for chunked, _type, _template in CHUNKED:
        for path in (chunked, f"{chunked}-1"):
            assert path not in blocked
            assert [row for row in after if row[1] == path] == [row for row in before if row[1] == path]
        conn = sqlite3.connect(db_path)
        try:
            first = conn.execute("SELECT content FROM Content WHERE path = ?", (chunked,)).fetchone()[0]
            stored = opt.reassemble(conn, chunked, first)
        finally:
            conn.close()
        assert _brotli(["-d", "-c"], stored, dictionary) == plain[chunked]


def test_every_slice_equals_the_rows_decoded_bytes(database):
    db_path, plain = database
    assert _run(db_path) == 0
    sliced = 0
    for data, items in _blocks(db_path):
        for path, offset, length in items:
            assert data[offset:offset + length] == plain[path], path
            sliced += 1
    assert sliced == sum(len(group) for group in BLOCKED.values())


class _RecordingPool:
    """Maps in the calling thread, recording how many rows each map() is handed."""

    def __init__(self):
        self.batches = []

    def map(self, fn, rows):
        rows = list(rows)
        self.batches.append(len(rows))
        return [fn(row) for row in rows]


class _Reversing:
    """A codec whose decode reverses the bytes, so a row and its decoded bytes differ."""

    @staticmethod
    def decompress(data: bytes, _compression: str) -> bytes:
        return data[::-1]


def test_rows_are_decoded_a_window_at_a_time_in_order(monkeypatch):
    monkeypatch.setattr(spb, "DECODE_WINDOW", 4)
    rows = [(n, f"a/{n}.html", "text/html", f"row {n}".encode()) for n in range(10)]
    pool = _RecordingPool()
    walk = spb.decoded_in_order(pool, _Reversing(), rows)
    taken = [next(walk) for _ in range(4)]
    assert pool.batches == [4], "rows past the first window were decoded before the walk reached them"
    taken += list(walk)
    assert pool.batches == [4, 4, 2]
    assert taken == [(row, row[3][::-1]) for row in rows]


@pytest.mark.parametrize("window", [2, 5])
def test_a_set_spanning_several_decode_windows_is_blocked_whole(database, monkeypatch, window):
    """a/ has 7 pages, so with a window of 2 or 5 it is decoded in more than one window, and a block of 3 straddles
    two of them. Every page must still be blocked, in order."""
    db_path, plain = database
    monkeypatch.setattr(spb, "DECODE_WINDOW", window)
    assert _run(db_path) == 0
    assert [path for paths in _block_paths(db_path) for path in paths] == _blocked_in_order()
    _assert_every_slice_is_its_page(db_path, plain)


@pytest.mark.parametrize("workers", [1, 2])
def test_blocks_are_written_in_order_while_more_are_pending(database, capsys, workers):
    """One page to a block: a/'s 7 blocks are more than the 2 * workers built ahead of the writes, so blocks are
    written while later ones are still pending. Every block must be written, and in path order."""
    db_path, plain = database
    assert spb.run({"db_path": db_path, "pages_per_block": 1, "dry_run": False, "workers": workers}) == 0
    expected = _blocked_in_order()
    assert len(BLOCKED["a/"]) > 2 * workers, "fixture premise: a/ alone fills the write-ahead limit"
    assert _block_paths(db_path) == [[path] for path in expected]
    assert _query(db_path, "SELECT id FROM ContentBlocks ORDER BY id") == [(n,) for n in range(1, len(expected) + 1)]
    _assert_every_slice_is_its_page(db_path, plain)
    assert _query(db_path, "SELECT COUNT(*) FROM Content WHERE LENGTH(content) = 0") == [(len(expected),)]
    assert f"Stored {len(expected)} page(s) in {len(expected)} block(s)" in capsys.readouterr().out


def test_blocked_rows_are_emptied_and_nothing_else_changes(database):
    db_path, _plain = database
    before = _tables(db_path)
    assert _run(db_path) == 0
    after = _tables(db_path)
    blocked = {path for group in BLOCKED.values() for path, _type, _template in group}
    old, new = before.pop("Content"), after.pop("Content")
    assert len(new) == len(old)
    for was, now in zip(old, new):
        if was[1] in blocked:
            # id, path, languageID, contentTypeID and templateId stay; content is an empty blob.
            assert now[3] == b"" and now[:3] + now[4:] == was[:3] + was[4:]
        else:
            assert now == was
    sequence = dict(before.pop("sqlite_sequence"))
    assert dict(after.pop("sqlite_sequence")) == {**sequence, "ContentBlocks": 9}
    before.pop("DocumentationDatabaseVersion")
    after.pop("DocumentationDatabaseVersion")  # test_declares_4_0_0_in_one_row
    assert after == before
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT DISTINCT typeof(content) FROM Content WHERE length(content) = 0").fetchall() == \
            [("blob",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        conn.close()


def test_declares_4_0_0_in_one_row(database):
    db_path, _plain = database
    assert _run(db_path) == 0
    assert _query(db_path, VERSION_ROWS) == [(4, 0, 0, "ADFA-6683", spb.COMMENT)]
    assert "ADFA-6683" in spb.COMMENT
    conn = sqlite3.connect(db_path)
    try:
        assert spb.declared_major(conn) == 4
    finally:
        conn.close()


def test_a_second_run_is_refused(database, capsys):
    db_path, _plain = database
    assert _run(db_path) == 0
    once = _sha(db_path)
    capsys.readouterr()
    assert _run(db_path) == 1
    assert "already has ContentBlockItems" in capsys.readouterr().err
    assert _sha(db_path) == once


def test_dry_run_writes_nothing(database, capsys):
    db_path, _plain = database
    pristine = _sha(db_path)
    versions = _query(db_path, VERSION_ROWS)
    assert _run(db_path, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "Would store 21 page(s) in 9 block(s)" in out
    assert "would declare 4.0.0" in out
    _assert_nothing_written(db_path, pristine)
    assert _query(db_path, VERSION_ROWS) == versions


JAN, OCT = "2026-01-01 00:00:00", "2026-10-21 00:00:00"


@pytest.mark.parametrize("statements, says, migrate", [
    (["DROP TABLE DocumentationDatabaseVersion"], "declares no documentation version", False),
    (["DELETE FROM DocumentationDatabaseVersion"], "declares no documentation version", False),
    ([f"INSERT INTO DocumentationDatabaseVersion VALUES (1, 0, 0, 'later', 'later', '{OCT}')"],
     "declares documentation major 1", False),
    ([f"INSERT INTO DocumentationDatabaseVersion VALUES (2, 1, 0, 'later', 'later', '{OCT}')"],
     "declares documentation major 2", True),
    ([f"INSERT INTO DocumentationDatabaseVersion VALUES (4, 0, 0, 'later', 'later', '{OCT}')"],
     "declares documentation major 4", False),
    ([f"INSERT INTO DocumentationDatabaseVersion VALUES (5, 0, 0, 'later', 'later', '{OCT}')"],
     "declares documentation major 5", False),
], ids=["no version table", "no version row", "1.x", "2.x", "4.x", "5.x"])
def test_a_database_not_declaring_3_is_refused_with_nothing_written(database, capsys, statements, says, migrate):
    """Only a 2.x database is pointed at migrate_tooltip_schema.py, which converts 2.x and refuses anything else."""
    db_path, _plain = database
    _execute(db_path, *statements)
    sha = _sha(db_path)
    assert _run(db_path) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and says in err and "nothing was written" in err, err
    assert ("run migrate_tooltip_schema.py first" in err) == migrate, err
    _assert_nothing_written(db_path, sha)


@pytest.mark.parametrize("rows, major", [
    ([(3, OCT), (2, JAN)], 3),  # a later rowid with an older changeTime does not decide
    ([(2, OCT), (3, OCT)], 3),  # a tie goes to the later rowid
    ([(3, OCT), (2, OCT)], 2),
    ([(4, JAN), (3, OCT)], 3),  # an older row declaring 4 does not decide
    ([(3, JAN), (4, OCT)], 4),
], ids=["older changeTime, later rowid", "tie, later rowid 3", "tie, later rowid 2", "older 4", "newer 4"])
def test_the_newest_version_row_decides(database, rows, major):
    """Read as the app reads it: ORDER BY changeTime DESC, rowid DESC. Each case is built so that rowid alone,
    changeTime alone with the tie broken the other way, or the greatest major would read another major."""
    db_path, _plain = database
    _execute(db_path, "DELETE FROM DocumentationDatabaseVersion",
             *(f"INSERT INTO DocumentationDatabaseVersion VALUES ({m}, 0, 0, 'test', 'test', '{t}')" for m, t in rows))
    sha = _sha(db_path)
    if major == 3:
        assert _run(db_path) == 0
        assert _query(db_path, VERSION_ROWS) == [(4, 0, 0, "ADFA-6683", spb.COMMENT)]
    else:
        assert _run(db_path) == 1
        _assert_nothing_written(db_path, sha)


def test_an_item_over_1_mib_decoded_stays_a_row(cap_database, capsys):
    db_path, plain = cap_database
    before = dict(_query(db_path, "SELECT path, content FROM Content"))
    assert len(before["a/cap/huge.html"]) < ITEM_CAP, "fixture premise: stored small, decoded over the cap"
    assert _run(db_path, per_block=64) == 0
    blocks = _block_paths(db_path)
    assert blocks[0] == ["a/cap/limit.html", "a/cap/small.html"]  # exactly 1 MiB is not over it
    assert "a/cap/huge.html" not in {path for paths in blocks for path in paths}
    after = dict(_query(db_path, "SELECT path, content FROM Content"))
    assert after["a/cap/huge.html"] == before["a/cap/huge.html"]
    out = capsys.readouterr().out
    assert "  a/: left as rows: 1 page(s) over 1,048,576 bytes decoded" in out
    assert "    kept by the cap: a/cap/huge.html" in out
    for data, items in _blocks(db_path):
        for path, offset, length in items:
            assert data[offset:offset + length] == plain[path], path


def test_a_block_closes_before_it_would_decode_past_4_mib(cap_database):
    db_path, _plain = cap_database
    assert _run(db_path, per_block=64) == 0
    blocks = _blocks(db_path)
    j_blocks = [[path for path, _offset, _length in items] for _data, items in blocks
                if items[0][0].startswith("j/")]
    # Exactly 4 MiB is not past it; 3.5 MiB closes before half a MiB and a byte more.
    assert j_blocks == [[f"j/b{n}.html" for n in range(4)], [f"j/c{n}.html" for n in range(4)], ["j/c4.html"]]
    sizes = [len(data) for data, _items in blocks]
    assert max(sizes) == BLOCK_CAP and all(size <= BLOCK_CAP for size in sizes)


def test_64_pages_to_a_block_by_default(cap_database, monkeypatch):
    db_path, _plain = cap_database
    monkeypatch.setattr(sys, "argv", ["store_pages_in_blocks.py", str(db_path), "--workers", "4"])
    with pytest.raises(SystemExit) as exited:
        spb.main()
    assert exited.value.code == 0
    k_blocks = [paths for paths in _block_paths(db_path) if paths[0].startswith("k/")]
    assert [len(paths) for paths in k_blocks] == [64, 1]


@pytest.mark.parametrize("workers", ["0", "-1"])
def test_workers_below_1_is_refused_on_an_error_line(database, capsys, monkeypatch, workers):
    db_path, _plain = database
    sha = _sha(db_path)
    monkeypatch.setattr(sys, "argv", ["store_pages_in_blocks.py", str(db_path), "--workers", workers])
    with pytest.raises(SystemExit) as exited:
        spb.main()
    assert exited.value.code == 1
    assert capsys.readouterr().err == f"error: --workers must be at least 1, not {workers}\n"
    _assert_nothing_written(db_path, sha)


class _Garbling(opt.BrotliCodec):
    """The real codec, except that the `garble`-th decode of each block it encoded comes back with one byte
    changed, the length kept, or with `extend` with one byte more at the end, every item's slice intact. `decodes`
    counts the decodes of each block, across instances."""
    garble = 1
    extend = False
    decodes = None

    def __init__(self, dictionary: bytes):
        super().__init__(dictionary)
        self._lock = threading.Lock()
        type(self).decodes = {}

    def compress(self, data: bytes, compression: str) -> bytes:
        encoded = super().compress(data, compression)
        with self._lock:
            self.decodes[encoded] = 0
        return encoded

    def decompress(self, data: bytes, compression: str) -> bytes:
        plain = super().decompress(data, compression)
        with self._lock:
            if data not in self.decodes:
                return plain
            self.decodes[data] += 1
            if self.decodes[data] != self.garble:
                return plain
        if self.extend:
            return plain + b"!"
        middle = len(plain) // 2
        return plain[:middle] + bytes([plain[middle] ^ 1]) + plain[middle + 1:]


def test_a_block_that_does_not_decode_back_byte_for_byte_writes_nothing(database, capsys, monkeypatch):
    """Garbled only on the decode-back, so the blocks stored are sound and only the byte-for-byte check before
    a row is emptied can stop the run."""
    db_path, _plain = database
    sha = _sha(db_path)
    monkeypatch.setattr(spb, "BrotliCodec", type("FirstDecode", (_Garbling,), {"garble": 1}))
    assert _run(db_path) == 1
    err = capsys.readouterr().err
    assert "does not read back byte for byte from the block starting at" in err and "nothing was written" in err, err
    _assert_nothing_written(db_path, sha)


def test_each_block_is_decoded_once(database, monkeypatch):
    """The decode-back is the only one: the check before the commit compares each stored block with the block
    built, by sha256, so a second decode, garbled here, never happens."""
    db_path, plain = database
    codec = type("SecondDecode", (_Garbling,), {"garble": 2})
    monkeypatch.setattr(spb, "BrotliCodec", codec)
    assert _run(db_path) == 0
    assert sorted(codec.decodes.values()) == [1] * 9
    _assert_every_slice_is_its_page(db_path, plain)


def test_a_block_that_decodes_longer_than_its_items_writes_nothing(database, capsys, monkeypatch):
    """One byte more at the end of the decode-back leaves every item's slice intact, so only a length check can
    stop the run."""
    db_path, _plain = database
    sha = _sha(db_path)
    monkeypatch.setattr(spb, "BrotliCodec", type("Longer", (_Garbling,), {"garble": 1, "extend": True}))
    assert _run(db_path) == 1
    err = capsys.readouterr().err
    assert "decoded back to" in err and "nothing was written" in err, err
    _assert_nothing_written(db_path, sha)


def _lose_the_last_block(conn) -> None:
    """A block and its items gone, as a lost write would leave them: their rows are empty all the same."""
    block_id = conn.execute("SELECT MAX(id) FROM ContentBlocks").fetchone()[0]
    conn.execute("DELETE FROM ContentBlockItems WHERE blockId = ?", (block_id,))
    conn.execute("DELETE FROM ContentBlocks WHERE id = ?", (block_id,))


def _lose_a_block_keeping_its_items(conn) -> None:
    """A block gone and its items left naming it: one item per blocked row still, and their rows empty."""
    conn.execute("DELETE FROM ContentBlocks WHERE id = (SELECT MAX(id) FROM ContentBlocks)")


def _change_a_stored_block(conn) -> None:
    """A block stored with one byte other than the one built, its length kept."""
    block_id, data = conn.execute("SELECT id, data FROM ContentBlocks ORDER BY id LIMIT 1").fetchone()
    conn.execute("UPDATE ContentBlocks SET data = ? WHERE id = ?", (bytes([data[0] ^ 1]) + data[1:], block_id))


def _move_an_item(conn) -> None:
    """An item located one byte later than where it was written."""
    conn.execute("UPDATE ContentBlockItems SET offset = offset + 1 WHERE contentId = "
                 "(SELECT MIN(contentId) FROM ContentBlockItems)")


def _refill_a_blocked_row(conn) -> None:
    """A blocked row whose content was never emptied."""
    conn.execute("UPDATE Content SET content = x'00' WHERE id = (SELECT MIN(contentId) FROM ContentBlockItems)")


@pytest.mark.parametrize("damage, says", [
    (_lose_the_last_block, "item(s) as written, Content id"),
    (_lose_a_block_keeping_its_items, "block(s) written are not in ContentBlocks, block 9 first"),
    (_change_a_stored_block, "block 1 as stored is not the block written"),
    (_move_an_item, "ContentBlockItems does not hold 1 item(s) as written"),
    (_refill_a_blocked_row, "1 blocked row(s) still hold their content"),
], ids=["a block lost", "a block lost, its items kept", "a block changed", "an item moved", "a row not emptied"])
def test_the_check_before_the_commit_refuses_what_the_writes_got_wrong(database, capsys, monkeypatch, damage, says):
    """Each damage leaves every stored block decoding to its items, so only the check that names it can stop the
    run."""
    db_path, _plain = database
    sha = _sha(db_path)
    check_stored = spb.check_stored

    def damaged(conn, *args):
        damage(conn)
        return check_stored(conn, *args)

    monkeypatch.setattr(spb, "check_stored", damaged)
    assert _run(db_path) == 1
    err = capsys.readouterr().err
    assert says in err and "nothing was written" in err, err
    _assert_nothing_written(db_path, sha)
