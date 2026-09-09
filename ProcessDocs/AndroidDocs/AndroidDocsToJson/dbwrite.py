#!/usr/bin/env python3
"""Writing content into a documentation.db: compression, chunking, and rows.

Every contract here belongs to `ProcessKotlinDocs/ProcessKotlinWebsiteJSON/populate_db.py`, which
is where it was worked out and where the reasoning is written down at length. This is a second
implementation only because that module cannot be imported from `main`: it does
`from build_nav import build_node` at import time, and `build_nav.py` lands with ADFA-4739, which
is not merged. Once it is, delete this and import from there instead -- the names and signatures
below are deliberately the same ones, so that change is a change of import line.

What must not drift from populate_db.py, because the server or the data depends on it:

  * CHUNK_SIZE is WebServer.kt's `contentChunkSize`. Its request handler decides a row is
    fragmented purely by the first row's content being exactly that many bytes, so this is not
    "about a megabyte" -- it is the identical constant on both sides.
  * A row is compressed against the database's own CompressionDictionary, via the `brotli` CLI,
    because the Python `brotli` package takes no dictionary argument. A row compressed against a
    different dictionary can decode without error into *different bytes*, so the dictionary is
    read from the database and never regenerated.
  * The base row of a path is UPDATEd, never DELETEd and re-INSERTed: Content's AddBook trigger
    fires on '%.pdf' inserts, and a delete/insert cycle replaces a curated Bookshelf entry with a
    timestamp title. Continuation rows end in "-<N>" so they never match that trigger.
"""

from __future__ import annotations

import atexit
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

CHUNK_SIZE = 1024 * 1024

_FRAGMENT_SUFFIX = re.compile(r"^(.*)-(\d+)$")


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} is not on PATH; install it (brew install {name})")
    return path


class DictionaryCompressor:
    """Compresses and decompresses against a fixed raw Brotli dictionary, via the `brotli` CLI.

    Thread-safe by having no mutable state: the dictionary is written to a temp file once, and
    each call is its own subprocess reading it.
    """

    def __init__(self, dictionary_data: bytes) -> None:
        self._brotli = find_tool("brotli")
        self._work_dir = Path(tempfile.mkdtemp(prefix="brotli-dict-"))
        self._dict_path = self._work_dir / "dictionary.bin"
        self._dict_path.write_bytes(dictionary_data)
        atexit.register(self.close)

    def _run(self, *extra: str, data: bytes) -> bytes:
        result = subprocess.run(
            [self._brotli, "-D", str(self._dict_path), *extra, "-c"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"brotli failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def compress(self, data: bytes) -> bytes:
        return self._run(data=data)

    def decompress(self, data: bytes) -> bytes:
        return self._run("-d", data=data)

    def close(self) -> None:
        shutil.rmtree(self._work_dir, ignore_errors=True)

    def __enter__(self) -> DictionaryCompressor:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def load_dictionary(conn) -> bytes:
    """The CompressionDictionary this database already holds. Never trains one: content elsewhere
    in the same database can only be decoded with the exact dictionary it was compressed against,
    so a database with no dictionary is one this script has no business writing to."""
    row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("CompressionDictionary is missing or empty; "
                           "run populate_db.py against this database first")
    return row[0]


def get_id(conn, table: str, value: str) -> int:
    row = conn.execute(f"SELECT id FROM {table} WHERE value = ?", (value,)).fetchone()
    if row is None:
        raise RuntimeError(f"{table} has no row for {value!r}; expected it to already exist")
    return row[0]


def get_content_type(conn, value: str) -> tuple[int, bool]:
    """(id, whether ContentTypes says to brotli it) for a ContentTypes.value."""
    row = conn.execute("SELECT id, compression FROM ContentTypes WHERE value = ?",
                       (value,)).fetchone()
    if row is None:
        raise RuntimeError(f"ContentTypes has no row for {value!r}; expected it to already exist")
    return row[0], row[1] == "brotli"


def upsert_template(conn, name: str, content: str) -> int:
    conn.execute(
        "INSERT INTO Templates (name, content) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET content = excluded.content",
        (name, content.encode("utf-8")),
    )
    return conn.execute("SELECT id FROM Templates WHERE name = ?", (name,)).fetchone()[0]


def fragment_chain(conn, base_path: str) -> list[tuple[int, str]]:
    """Every "<base_path>-<N>" continuation row present, as (n, path) sorted by n.

    Found by LIKE and a parsed suffix rather than by probing constructed paths, so it does not
    matter what N a chain starts at. The pattern deliberately over-matches -- `_` and `%` in a
    path are wildcards -- which is what the regex re-check is for. Never build a DELETE straight
    off that pattern.
    """
    rows = conn.execute("SELECT path FROM Content WHERE path LIKE ?",
                        (f"{base_path}-%",)).fetchall()
    chain = []
    for (path,) in rows:
        match = _FRAGMENT_SUFFIX.match(path)
        if match and match.group(1) == base_path:
            chain.append((int(match.group(2)), path))
    chain.sort()
    return chain


def _chunks(data: bytes) -> list[bytes]:
    return [data[offset:offset + CHUNK_SIZE] for offset in range(0, max(len(data), 1), CHUNK_SIZE)]


def write_content(conn, path: str, language_id: int, content_type_id: int, template_id: int,
                  data: bytes) -> int:
    """Stores `data` (already compressed, if its type says so) at `path`, chunked if it has to be.

    Chunking happens on the final bytes, matching WebServer.kt reassembling fragments before it
    ever decompresses. Works for a path that is already there and one that is not: the base row is
    updated or inserted, and continuation rows are reconciled by exact path so a shorter payload
    leaves no stale tail behind. Returns the number of rows the content occupies.
    """
    parts = _chunks(data)
    if conn.execute("SELECT 1 FROM Content WHERE path = ?", (path,)).fetchone():
        conn.execute("UPDATE Content SET content = ?, languageID = ?, contentTypeID = ?, "
                     "templateId = ? WHERE path = ?",
                     (parts[0], language_id, content_type_id, template_id, path))
    else:
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (path, language_id, parts[0], content_type_id, template_id))

    wanted = {f"{path}-{number}": part for number, part in enumerate(parts[1:], start=1)}
    existing = {fragment for _n, fragment in fragment_chain(conn, path)}
    for fragment, part in wanted.items():
        if fragment in existing:
            conn.execute("UPDATE Content SET content = ?, languageID = ?, contentTypeID = ?, "
                         "templateId = ? WHERE path = ?",
                         (part, language_id, content_type_id, template_id, fragment))
        else:
            conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, "
                         "templateId) VALUES (?, ?, ?, ?, ?)",
                         (fragment, language_id, part, content_type_id, template_id))
    for surplus in sorted(existing - set(wanted)):
        conn.execute("DELETE FROM Content WHERE path = ?", (surplus,))
    return len(parts)


def read_content(conn, path: str) -> bytes:
    """The full stored bytes at `path`: its base row plus every continuation row, in order."""
    row = conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()
    if row is None:
        return b""
    parts = [row[0]]
    if len(row[0]) < CHUNK_SIZE:
        return parts[0]
    for _n, fragment in fragment_chain(conn, path):
        fragment_row = conn.execute("SELECT content FROM Content WHERE path = ?",
                                    (fragment,)).fetchone()
        if fragment_row is not None:
            parts.append(fragment_row[0])
    return b"".join(parts)
