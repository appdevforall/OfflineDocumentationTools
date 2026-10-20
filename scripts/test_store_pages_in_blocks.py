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

Covers store_pages_in_blocks.py on a small synthetic database, three pages to
a block: each set in path order with its own blocks, the last one shorter;
rows held in more than one row left alone; every item sliced from its block
equal to its row's decoded bytes; blocked rows emptied and nothing else
changed; a second run refused; a dry run writing nothing.

    uv run --with pytest --with-requirements requirements.txt python -m pytest -q scripts/test_store_pages_in_blocks.py
"""
from __future__ import annotations

import hashlib
import random
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_db_media as opt  # noqa: E402
import store_pages_in_blocks as spb  # noqa: E402

PER_BLOCK = 3
DICTIONARY = b'{"title": "", "members": [{"name": "", "summary": ""}]} <div class="devsite-article-body">' * 64

# (path, content type, templateId). Inserted in this order, so ids do not follow paths.
BLOCKED = {
    "a/": [("a/z.html", "text/html", 7), ("a/android/widget/TextView.html", "text/html", 7),
           # Looks like a continuation row, but a/z.html is not 1 MiB, so it is a page of its own.
           ("a/z.html-1", "text/html", 7), ("a/androidx/a.html", "text/html", 7),
           ("a/Zeta.html", "text/html", 7), ("a/androidx/B.html", "text/html", 7),
           ("a/android/util/Pair.html", "text/html", 7)],
    "j/": [("j/index.html", "text/html", 0), ("j/api/b.html", "text/html", 0),
           ("j/Overview.html", "text/html", 0), ("j/api/A.html", "text/html", 0)],
    "k/": [("k/html/y.html", "text/html", 9), ("k/html/x.html", "text/html", 8)],
}
LEFT = [
    ("a/devsite/reference/x.html", "text/html", 7),  # a/devsite/ is not in the a/ set
    ("a/plain.html", "text/html", 0),  # no template
    ("a/raw.json", "text", 7),  # its type is stored uncompressed
    ("j/style.css", "text/css", 0),  # not HTML
    ("j/logo.png", "image/png", 0),
    ("k/html/nav.html", "text/html", 0),  # no template
    ("i/topic.html", "text/html", 7),  # not one of the sets
]
CHUNKED = "a/big.html"  # a/ templated, but stored as a 1 MiB row and a continuation row
TYPES = [("text/html", "brotli"), ("text/css", "brotli"), ("text", "none"), ("image/png", "none")]
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
"""


def _brotli(args: list, data: bytes, dictionary: Path) -> bytes:
    return subprocess.run(["brotli", *args, "-D", str(dictionary)], input=data, stdout=subprocess.PIPE,
                          check=True).stdout


def _plain(path: str, rng: random.Random) -> bytes:
    words = " ".join(rng.choice(["member", "summary", "inherited", "View", "int", "String"])
                     for _ in range(rng.randrange(20, 400)))
    return f'{{"path": "{path}", "members": [{{"name": "{words}"}}]}}'.encode()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """(database, {path: plain bytes}): the pristine synthetic database."""
    work = tmp_path_factory.mktemp("blocks")
    dictionary = work / "dict.bin"
    dictionary.write_bytes(DICTIONARY)
    rng = random.Random(6683)
    db_path = work / "documentation.db"
    plain = {}
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO Languages (value) VALUES ('en')")
    conn.executemany("INSERT INTO ContentTypes (value, compression) VALUES (?, ?)", TYPES)
    conn.execute("INSERT INTO CompressionDictionary VALUES (1, ?)", (DICTIONARY,))
    conn.executemany("INSERT INTO Templates (id, name, content) VALUES (?, ?, ?)",
                     [(7, "android-reference.peb", b"t7"), (8, "page.peb", b"t8"), (9, "nav.peb", b"t9")])
    types = {value: (type_id, compression) for type_id, value, compression in conn.execute("SELECT * FROM ContentTypes")}
    pages = [page for group in BLOCKED.values() for page in group] + LEFT
    rng.shuffle(pages)
    for path, content_type, template_id in pages:
        plain[path] = _plain(path, rng)
        type_id, compression = types[content_type]
        stored = _brotli(["-c", "-q", "5"], plain[path], dictionary) if compression == "brotli" else plain[path]
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, ?)", (path, stored, type_id, template_id))
    # One Brotli stream split across "<path>" and "<path>-1", as a real chunked row is.
    plain[CHUNKED] = rng.randbytes(opt.CHUNK_SIZE + 100_000)
    stored = _brotli(["-c", "-q", "0"], plain[CHUNKED], dictionary)
    assert len(stored) > opt.CHUNK_SIZE
    for path, chunk in ((CHUNKED, stored[:opt.CHUNK_SIZE]), (f"{CHUNKED}-1", stored[opt.CHUNK_SIZE:])):
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, ?, 7)", (path, chunk, types["text/html"][0]))
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


def _run(db_path: Path, dry_run: bool = False) -> int:
    return spb.run({"db_path": db_path, "pages_per_block": PER_BLOCK, "dry_run": dry_run, "workers": 4})


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


def _in_path_order(paths) -> list:
    """ORDER BY path in SQLite's BINARY collation: by UTF-8 bytes."""
    return sorted(paths, key=lambda path: path.encode("utf-8"))


def test_blocks_follow_path_order_and_never_mix_sets(database):
    db_path, _plain = database
    assert _run(db_path) == 0
    blocks = _blocks(db_path)
    expected = [path for name in ("a/", "j/", "k/") for path in _in_path_order(p for p, _t, _i in BLOCKED[name])]
    assert [path for _data, items in blocks for path, _offset, _length in items] == expected
    # Binary, not case-blind: a/Zeta.html sorts before a/android/..., a/androidx/B before a/androidx/a.
    assert expected[:3] == ["a/Zeta.html", "a/android/util/Pair.html", "a/android/widget/TextView.html"]
    for data, items in blocks:
        assert len({path[:2] for path, _offset, _length in items}) == 1
        offset = 0
        for _path, start, length in items:
            assert start == offset
            offset += length
        assert offset == len(data)


def test_the_last_block_of_a_set_is_shorter(database):
    db_path, _plain = database
    assert _run(db_path) == 0
    # a/ has 7 pages, j/ 4 and k/ 2: each set starts a block of its own.
    assert [len(items) for _data, items in _blocks(db_path)] == [3, 3, 1, 3, 1, 2]


def test_continuation_rows_are_left_alone(database):
    db_path, plain = database
    before = _tables(db_path)["Content"]
    assert _run(db_path) == 0
    blocked = {path for _data, items in _blocks(db_path) for path, _offset, _length in items}
    assert CHUNKED not in blocked and f"{CHUNKED}-1" not in blocked
    assert "a/z.html-1" in blocked
    after = _tables(db_path)["Content"]
    for path in (CHUNKED, f"{CHUNKED}-1"):
        assert [row for row in after if row[1] == path] == [row for row in before if row[1] == path]
    conn = sqlite3.connect(db_path)
    try:
        first = conn.execute("SELECT content FROM Content WHERE path = ?", (CHUNKED,)).fetchone()[0]
        stored = opt.reassemble(conn, CHUNKED, first)
    finally:
        conn.close()
    dictionary = db_path.with_name("chunked.dict.bin")
    dictionary.write_bytes(DICTIONARY)
    assert _brotli(["-d", "-c"], stored, dictionary) == plain[CHUNKED]


def test_every_slice_equals_the_rows_decoded_bytes(database):
    db_path, plain = database
    assert _run(db_path) == 0
    sliced = 0
    for data, items in _blocks(db_path):
        for path, offset, length in items:
            assert data[offset:offset + length] == plain[path], path
            sliced += 1
    assert sliced == sum(len(group) for group in BLOCKED.values())


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
    assert dict(after.pop("sqlite_sequence")) == {**sequence, "ContentBlocks": 6}
    assert after == before
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT DISTINCT typeof(content) FROM Content WHERE length(content) = 0").fetchall() == \
            [("blob",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
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
    assert _run(db_path, dry_run=True) == 0
    assert "Would store 13 page(s) in 6 block(s)" in capsys.readouterr().out
    assert _sha(db_path) == pristine
    assert not db_path.with_name(db_path.name + "-journal").exists()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'ContentBlock%'").fetchall() == []
    finally:
        conn.close()
