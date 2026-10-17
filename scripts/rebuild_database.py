#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
rebuild_database.py

Copies a documentation.db into a fresh file - same schema, same rows with the
same rowids, same page size - to drop the space SQLite gives back no other way:
the bytes reserved at the end of every page.

    uv run scripts/rebuild_database.py documentation.db rebuilt.db

Why (ADFA-3305): the database reserves 12 bytes at the end of every 1 KB page
(file header byte 20), and on the 2026-10-13 database all of them, on all
128,099 pages, are zero. Reserved space is for SQLite extensions - a per-page
checksum, an encryption nonce - and nothing here uses one; the app opens the
file with Android's own SQLite. The bytes arrived in one rebuild between
2026-05-05 and 2026-07-29, from no tool in this repository. SQLite lets a
database's reserved space grow but never shrink: VACUUM, VACUUM INTO and the
reserve_bytes file control all kept it in testing. A fresh file starts with
none. On that database after the ADFA-3305 conversion this saves 1,467,392
bytes (1.1%). It is a one-time step: nothing here sets reserved bytes, and
every later VACUUM keeps the zero.

How: a new file with the source's page size and auto_vacuum off - set
explicitly, because some SQLite builds default it on (the sqlite3 shell this
was tested with did), which adds a pointer-map page for every ~200 pages.
Every table is created from the source's own SQL and filled in rowid order,
keeping each row's rowid; then the indexes, triggers and views, then
sqlite_sequence, user_version and application_id; then VACUUM. Before the
result is kept, it is compared with the source - schema, every row of every
table with its rowid, sqlite_sequence, both pragmas, integrity_check and
foreign_key_check - and any difference deletes it and fails the run.

The source is opened read-only and never changed. OUTPUT must not exist
(ADFA-5296: a rebuilt database is saved somewhere other than where it came
from); swap it in by hand once it has been checked.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path


def read_only_uri(path: Path) -> str:
    """A SQLite URI opening `path` read-only. Built with as_uri() so a "?" or
    "#" in the path is escaped instead of starting the query string."""
    return path.resolve().as_uri() + "?mode=ro"


def reserved_bytes(path: Path) -> int:
    """Byte 20 of the file header: bytes reserved at the end of each page."""
    with open(path, "rb") as f:
        return f.read(21)[20]


def has_rowid_alias(conn, table: str) -> bool:
    """Whether the table's INTEGER PRIMARY KEY makes a column the rowid, in
    which case SELECT * already carries it and naming rowid too would clash."""
    keys = [row for row in conn.execute(f'PRAGMA table_info("{table}")') if row[5]]
    return len(keys) == 1 and keys[0][2].upper() == "INTEGER"


def user_tables(conn) -> list:
    """[(name, sql, without_rowid)] for every ordinary table, in schema order."""
    tables = []
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY rowid"):
        if name.startswith("sqlite_"):
            if name != "sqlite_sequence":
                raise RuntimeError(f"{name} is not copied by this tool; drop it (or extend the tool) and retry")
            continue
        tables.append((name, sql, "WITHOUT ROWID" in sql.upper()))
    return tables


def fingerprint(path: Path) -> dict:
    """What a rebuild must keep: schema, every row of every table in rowid
    order with its rowid, sqlite_sequence, the two header pragmas, and what
    integrity_check and foreign_key_check say."""
    conn = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        out = {
            "schema": sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")),
            "sequence": sorted(conn.execute("SELECT * FROM sqlite_sequence"))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone() else [],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
            "integrity": conn.execute("PRAGMA integrity_check").fetchall(),
            "foreign_keys": sorted(conn.execute("PRAGMA foreign_key_check")),
        }
        for name, _sql, without_rowid in user_tables(conn):
            digest, count = hashlib.sha256(), 0
            query = (f'SELECT * FROM "{name}" ORDER BY 1' if without_rowid
                     else f'SELECT rowid, * FROM "{name}" ORDER BY rowid')
            for row in conn.execute(query):
                for value in row:
                    if isinstance(value, bytes):
                        digest.update(b"B%d:" % len(value) + value)
                    else:
                        text = repr(value).encode("utf-8")
                        digest.update(b"V%d:" % len(text) + text)
                count += 1
            out[f"table {name}"] = (count, digest.hexdigest())
        return out
    finally:
        conn.close()


def rebuild(source: Path, output: Path) -> None:
    src = sqlite3.connect(read_only_uri(source), uri=True)
    try:
        page_size = src.execute("PRAGMA page_size").fetchone()[0]
        user_version = src.execute("PRAGMA user_version").fetchone()[0]
        application_id = src.execute("PRAGMA application_id").fetchone()[0]
        tables = user_tables(src)
        aliases = {name: has_rowid_alias(src, name) for name, _sql, _w in tables}
        others = src.execute("SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') "
                             "AND sql IS NOT NULL ORDER BY rowid").fetchall()
        has_sequence = bool(src.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone())
    finally:
        src.close()

    dst = sqlite3.connect(output.resolve().as_uri(), uri=True, isolation_level=None)
    try:
        # Both only take effect on an empty file, so before anything is created.
        dst.execute(f"PRAGMA page_size = {int(page_size)}")
        dst.execute("PRAGMA auto_vacuum = NONE")
        dst.execute("ATTACH DATABASE ? AS source", (read_only_uri(source),))
        dst.execute("BEGIN")
        for name, sql, without_rowid in tables:
            dst.execute(sql)
            if without_rowid or aliases[name]:
                order = "" if without_rowid else " ORDER BY rowid"
                dst.execute(f'INSERT INTO main."{name}" SELECT * FROM source."{name}"{order}')
            else:
                columns = ", ".join(f'"{row[1]}"' for row in dst.execute(f'PRAGMA main.table_info("{name}")'))
                dst.execute(f'INSERT INTO main."{name}" (rowid, {columns}) '
                            f'SELECT rowid, {columns} FROM source."{name}" ORDER BY rowid')
        # Indexes after the rows: building one over sorted data packs it.
        for (sql,) in others:
            dst.execute(sql)
        if has_sequence:
            dst.execute("DELETE FROM main.sqlite_sequence")
            dst.execute("INSERT INTO main.sqlite_sequence SELECT * FROM source.sqlite_sequence")
        dst.execute("COMMIT")
        dst.execute("DETACH DATABASE source")
        dst.execute(f"PRAGMA user_version = {int(user_version)}")
        dst.execute(f"PRAGMA application_id = {int(application_id)}")
        dst.execute("VACUUM")
    finally:
        dst.close()


def run(cfg: dict) -> int:
    source, output = cfg["source"], cfg["output"]
    if not source.is_file():
        print(f"error: {source} does not exist", file=sys.stderr)
        return 1
    if output.exists():
        print(f"error: {output} already exists; give a new path", file=sys.stderr)
        return 1
    try:
        rebuild(source, output)
        before, after = fingerprint(source), fingerprint(output)
    except (sqlite3.Error, RuntimeError) as exc:
        output.unlink(missing_ok=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    differ = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    if differ:
        output.unlink(missing_ok=True)
        print(f"error: the rebuilt copy differs from {source} in: {', '.join(differ)}; nothing was kept",
              file=sys.stderr)
        return 1
    tables = sum(1 for key in before if key.startswith("table "))
    rows = sum(before[key][0] for key in before if key.startswith("table "))
    print(f"Rebuilt {source} -> {output}: {tables} table(s), {rows:,} row(s), schema, sqlite_sequence and "
          f"pragmas identical. Reserved bytes per page {reserved_bytes(source)} -> {reserved_bytes(output)}; "
          f"{source.stat().st_size:,} -> {output.stat().st_size:,} bytes.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", type=Path, help="Database to copy, e.g. documentation.db; opened read-only")
    p.add_argument("output", type=Path, help="Path for the rebuilt copy; must not exist")
    args = p.parse_args()
    sys.exit(run({"source": args.source, "output": args.output}))


if __name__ == "__main__":
    main()
