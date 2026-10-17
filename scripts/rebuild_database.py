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

    uv run scripts/rebuild_database.py documentation.db rebuilt.db --pack Content,Tooltips

Why (ADFA-3305): the database reserves 12 bytes at the end of every 1 KB page
(file header byte 20), and on the 2026-10-13 database all of them, on all
128,099 pages, are zero. Reserved space is for SQLite extensions - a per-page
checksum, an encryption nonce - and nothing here uses one; the app opens the
file with Android's own SQLite. The bytes arrived in one rebuild between
2026-05-05 and 2026-07-29, from no tool in this repository. SQLite lets a
database's reserved space grow but never shrink: VACUUM, VACUUM INTO and the
reserve_bytes file control all kept it in testing. A fresh file starts with
none. On that database after the ADFA-3305 conversion this saves 1,467,392
bytes (1.1%). Dropping them is needed once: nothing here sets reserved bytes,
and every later VACUUM keeps the zero. Packing, below, is not a one-off.

Packing (--pack TABLE): SQLite appends rows in rowid order and starts a new
leaf page when the next row does not fit, so how much of each page is left
over follows from which rows happen to be numbered next to each other.
--pack inserts the named tables' rows in an order that packs their leaf
pages best-fit (packing_order), each taking the next id AUTOINCREMENT would
hand it, and links every declared foreign key to the ids handed out - what a
fresh build does when it records each row's new id. Ids continue after the
old sequence, so an id can stop naming a row (one quoted in an old tooltip
feedback email, say) but never come to name a different one. On the
2026-10-13 database Content goes from 19,158 leaf pages to 14,524 and
Tooltips from 16,069 to 12,402. A table some row references without
a match is refused, since that orphan's id could otherwise name a new row.
The gain lasts only until rows are added, which append in arrival order, so
packing belongs in every release's rebuild.

How: a new file with the source's page size and auto_vacuum off - set
explicitly, because some SQLite builds default it on (the sqlite3 shell this
was tested with did), which adds a pointer-map page for every ~200 pages.
Every table is created from the source's own SQL and filled in rowid order,
keeping each row's rowid; then the indexes, triggers and views, then
sqlite_sequence, user_version and application_id; then VACUUM. Before the
result is kept, it is compared with the source - schema, every row of every
table with its rowid, sqlite_sequence, both pragmas, integrity_check and
foreign_key_check, with any ids --pack renumbered mapped back first - and
any difference deletes it and fails the run.

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


def alias_column(conn, table: str):
    """Index (in SELECT * order) of the column that is the rowid, or None."""
    columns = list(conn.execute(f'PRAGMA table_info("{table}")'))
    keys = [row for row in columns if row[5]]
    return keys[0][0] if len(keys) == 1 and keys[0][2].upper() == "INTEGER" else None


def _varint_len(value: int) -> int:
    length = 1
    while value > 0x7F and length < 9:
        value >>= 7
        length += 1
    return length


def _stored(kind: str, value) -> tuple:
    """(serial type, body bytes) of one value in SQLite's record format, from
    its typeof() and - for text and blobs - its length in bytes."""
    if kind == "null":
        return 0, 0
    if kind == "integer":
        if value in (0, 1):
            return 8 + value, 0
        for code, size in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 6), (6, 8)):
            if -(1 << (8 * size - 1)) <= value < (1 << (8 * size - 1)):
                return code, size
    if kind == "real":
        return 7, 8
    return (2 * value + 13 if kind == "text" else 2 * value + 12), value


def leaf_cell_size(rowid: int, stored: list, usable: int) -> int:
    """Bytes one row takes on a table leaf page, its 2-byte cell pointer
    included: payload-size and rowid varints, the record (header and body) up
    to SQLite's local-payload limit, and the 4-byte overflow page number when
    the rest spills - the file format's "B-tree Cell Format"."""
    types_len = sum(_varint_len(code) for code, _size in stored)
    header = types_len + _varint_len(types_len + 1)
    payload = header + sum(size for _code, size in stored)
    max_local = usable - 35
    min_local = (usable - 12) * 32 // 255 - 23
    if payload <= max_local:
        local, overflow = payload, 0
    else:
        k = min_local + (payload - min_local) % (usable - 4)
        local, overflow = (k if k <= max_local else min_local), 4
    return _varint_len(payload) + _varint_len(rowid) + local + overflow + 2


def packing_order(conn, table: str, page_size: int, largest: int) -> list:
    """The table's rowids in an order that fills its leaf pages well.

    SQLite appends rows in rowid order and starts a new leaf page when the next
    row does not fit, so which rows share a page - and how much of each page is
    left over - follows from the rowid order alone. Each row's exact cell size
    is computed (leaf_cell_size; its rowid sized as `largest`, the largest new
    one, so no estimate is ever low) and the rows are packed best-fit-decreasing into
    pages of a fresh file's leaf room, page_size - 8. Inserting in this order
    produces exactly the predicted page count: 14,524 leaf pages for Content
    on the 2026-10-13 database, against 19,158 in rowid order."""
    import bisect

    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    alias = alias_column(conn, table)
    probes = ", ".join(f'typeof("{c}"), CASE typeof("{c}") WHEN \'integer\' THEN "{c}" '
                       f'WHEN \'text\' THEN length(CAST("{c}" AS BLOB)) '
                       f'WHEN \'blob\' THEN length("{c}") END' for c in columns)
    rows = conn.execute(f'SELECT rowid, {probes} FROM "{table}"').fetchall()
    cells = []
    for row in rows:
        stored = [(0, 0) if index == alias else _stored(row[1 + 2 * index], row[2 + 2 * index])
                  for index in range(len(columns))]
        cells.append((leaf_cell_size(largest, stored, page_size), row[0]))
    room = page_size - 8
    pages, free = [], []  # free: sorted [(bytes left, page index)]
    for size, rowid in sorted(cells, reverse=True):
        slot = bisect.bisect_left(free, (size, -1))
        if slot < len(free):
            left, index = free.pop(slot)
            pages[index].append(rowid)
            bisect.insort(free, (left - size, index))
        else:
            pages.append([rowid])
            bisect.insort(free, (room - size, len(pages) - 1))
    return [rowid for page in pages for rowid in page]


def references_to(conn, tables: list, parent: str) -> list:
    """[(child table, column)] for every declared foreign key naming `parent`'s
    rowid - the values a renumbering of `parent` has to follow."""
    alias = alias_column(conn, parent)
    key = None if alias is None else list(conn.execute(f'PRAGMA table_info("{parent}")'))[alias][1]
    found = []
    for child, _sql, _without_rowid in tables:
        for fk in conn.execute(f'PRAGMA foreign_key_list("{child}")'):
            if fk[2] == parent and (fk[4] is None or fk[4] == key):
                found.append((child, fk[3]))
    return found


def fingerprint(path: Path, renumbered: dict = None, followed: dict = None) -> dict:
    """What a rebuild must keep: schema, every row of every table with its
    rowid, sqlite_sequence, the two header pragmas, and what integrity_check
    and foreign_key_check say.

    For a copy made with --pack, `renumbered` ({table: {new rowid: old}}) and
    `followed` ({table: {column: parent}}) map every renumbered id back, so the
    copy must still match the source exactly. Rows are compared through a
    digest per row, sorted by (original) rowid."""
    renumbered, followed = renumbered or {}, followed or {}
    conn = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        def unmap(table, rowid):
            return renumbered.get(table, {}).get(rowid, rowid)

        out = {
            "schema": sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")),
            "sequence": sorted(conn.execute("SELECT * FROM sqlite_sequence"))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone() else [],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
            "integrity": conn.execute("PRAGMA integrity_check").fetchall(),
            "foreign_keys": sorted((t, unmap(t, rowid), p, k) for t, rowid, p, k in
                                   conn.execute("PRAGMA foreign_key_check")),
        }
        for name, _sql, without_rowid in user_tables(conn):
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]
            alias = alias_column(conn, name)
            remap = {columns.index(column): parent for column, parent in followed.get(name, {}).items()}
            keyed = []
            query = f'SELECT * FROM "{name}"' if without_rowid else f'SELECT rowid, * FROM "{name}"'
            for row in conn.execute(query):
                if not without_rowid:
                    rowid, row = unmap(name, row[0]), list(row[1:])
                    if alias is not None:
                        row[alias] = rowid
                    row = [rowid] + row
                    offset = 1
                else:
                    row, offset = list(row), 0
                for index, parent in remap.items():
                    row[index + offset] = unmap(parent, row[index + offset])
                digest = hashlib.sha256()
                for value in row:
                    if isinstance(value, bytes):
                        digest.update(b"B%d:" % len(value) + value)
                    else:
                        text = repr(value).encode("utf-8")
                        digest.update(b"V%d:" % len(text) + text)
                keyed.append((row[0] if not without_rowid else digest.digest(), digest.digest()))
            keyed.sort()
            out[f"table {name}"] = (len(keyed), hashlib.sha256(b"".join(d for _k, d in keyed)).hexdigest())
        return out
    finally:
        conn.close()


def rebuild(source: Path, output: Path, pack=()) -> tuple:
    """Writes the copy; returns (renumbered, followed, advanced): the id maps for
    fingerprint(), and {table: new sqlite_sequence value} for each packed table."""
    src = sqlite3.connect(read_only_uri(source), uri=True)
    try:
        page_size = src.execute("PRAGMA page_size").fetchone()[0]
        user_version = src.execute("PRAGMA user_version").fetchone()[0]
        application_id = src.execute("PRAGMA application_id").fetchone()[0]
        tables = user_tables(src)
        aliases = {name: alias_column(src, name) for name, _sql, _w in tables}
        without = {name: without_rowid for name, _sql, without_rowid in tables}
        has_sequence = bool(src.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone())
        sequence = dict(src.execute("SELECT name, seq FROM sqlite_sequence")) if has_sequence else {}
        renumbered, followed, advanced = {}, {}, {}
        for table in pack:
            if table not in without:
                raise RuntimeError(f"--pack {table}: no such table")
            if without[table]:
                raise RuntimeError(f"--pack {table}: a WITHOUT ROWID table has no rowids to renumber")
            for child, column in references_to(src, tables, table):
                orphans = src.execute(f'SELECT COUNT(*) FROM "{child}" WHERE "{column}" IS NOT NULL AND '
                                      f'"{column}" NOT IN (SELECT rowid FROM "{table}")').fetchone()[0]
                if orphans:
                    raise RuntimeError(f"--pack {table}: {orphans} row(s) of {child}.{column} name no {table} "
                                       "row, and an orphan's old id could match a new one; fix them first")
                followed.setdefault(child, {})[column] = table
            # Each row takes the next id AUTOINCREMENT would hand out - after both
            # the old sequence and the largest old id - so no id ever comes to
            # name a different row than it used to; it can only stop naming one.
            base = max(sequence.get(table, 0),
                       src.execute(f'SELECT COALESCE(MAX(rowid), 0) FROM "{table}"').fetchone()[0])
            count = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            order = packing_order(src, table, page_size, base + count)
            renumbered[table] = {base + n: old for n, old in enumerate(order, start=1)}
            if table in sequence:
                advanced[table] = base + count
        others = src.execute("SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') "
                             "AND sql IS NOT NULL ORDER BY rowid").fetchall()
    finally:
        src.close()

    dst = sqlite3.connect(output.resolve().as_uri(), uri=True, isolation_level=None)
    try:
        # Both only take effect on an empty file, so before anything is created.
        dst.execute(f"PRAGMA page_size = {int(page_size)}")
        dst.execute("PRAGMA auto_vacuum = NONE")
        dst.execute("ATTACH DATABASE ? AS source", (read_only_uri(source),))
        maps = {}
        for number, (table, mapping) in enumerate(renumbered.items()):
            maps[table] = f"temp.pack_{number}"
            dst.execute(f"CREATE TEMP TABLE pack_{number} (old INTEGER PRIMARY KEY, new INTEGER NOT NULL)")
            dst.executemany(f"INSERT INTO temp.pack_{number} VALUES (?, ?)",
                            ((old, new) for new, old in mapping.items()))
        dst.execute("BEGIN")
        for name, sql, without_rowid in tables:
            dst.execute(sql)
            columns = [row[1] for row in dst.execute(f'PRAGMA main.table_info("{name}")')]
            values = []
            for column in columns:
                parent = followed.get(name, {}).get(column)
                values.append(f'COALESCE((SELECT new FROM {maps[parent]} WHERE old = s."{column}"), s."{column}")'
                              if parent else f's."{column}"')
            targets = ", ".join(f'"{column}"' for column in columns)
            if name in maps:
                # Renumbered: each row takes its new rowid, in packing order.
                if aliases[name] is not None:
                    values[aliases[name]] = "m.new"
                else:
                    targets, values = f"rowid, {targets}", ["m.new"] + values
                dst.execute(f'INSERT INTO main."{name}" ({targets}) SELECT {", ".join(values)} '
                            f'FROM source."{name}" AS s JOIN {maps[name]} AS m ON m.old = s.rowid ORDER BY m.new')
            elif without_rowid:
                dst.execute(f'INSERT INTO main."{name}" ({targets}) SELECT {", ".join(values)} FROM source."{name}" AS s')
            else:
                if aliases[name] is None:
                    targets, values = f"rowid, {targets}", ["s.rowid"] + values
                dst.execute(f'INSERT INTO main."{name}" ({targets}) SELECT {", ".join(values)} '
                            f'FROM source."{name}" AS s ORDER BY s.rowid')
        # Indexes after the rows: building one over sorted data packs it.
        for (sql,) in others:
            dst.execute(sql)
        if has_sequence:
            dst.execute("DELETE FROM main.sqlite_sequence")
            dst.execute("INSERT INTO main.sqlite_sequence SELECT * FROM source.sqlite_sequence")
            dst.executemany("UPDATE main.sqlite_sequence SET seq = ? WHERE name = ?",
                            [(seq, name) for name, seq in advanced.items()])
        dst.execute("COMMIT")
        dst.execute("DETACH DATABASE source")
        dst.execute(f"PRAGMA user_version = {int(user_version)}")
        dst.execute(f"PRAGMA application_id = {int(application_id)}")
        dst.execute("VACUUM")
    finally:
        dst.close()
    return renumbered, followed, advanced


def leaf_pages(path: Path, table: str) -> int:
    conn = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM dbstat WHERE name = ? AND pagetype = 'leaf'", (table,)).fetchone()[0]
    finally:
        conn.close()


def run(cfg: dict) -> int:
    source, output = cfg["source"], cfg["output"]
    if not source.is_file():
        print(f"error: {source} does not exist", file=sys.stderr)
        return 1
    if output.exists():
        print(f"error: {output} already exists; give a new path", file=sys.stderr)
        return 1
    pack = cfg.get("pack") or ()
    try:
        renumbered, followed, advanced = rebuild(source, output, pack)
        before, after = fingerprint(source), fingerprint(output, renumbered, followed)
        # A packed table's sequence moves on by its row count; nothing else may.
        before["sequence"] = sorted((name, advanced.get(name, seq)) for name, seq in before["sequence"])
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
    for table in pack:
        moved = ", ".join(f"{child}.{column}" for child, columns in followed.items()
                          for column, parent in columns.items() if parent == table)
        print(f"  {table} renumbered for packing: {leaf_pages(source, table):,} -> {leaf_pages(output, table):,} "
              f"leaf pages{f'; followed by {moved}' if moved else ''}.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", type=Path, help="Database to copy, e.g. documentation.db; opened read-only")
    p.add_argument("output", type=Path, help="Path for the rebuilt copy; must not exist")
    p.add_argument("--pack", default=None, metavar="TABLE[,TABLE...]",
                   type=lambda value: [t.strip() for t in value.split(",") if t.strip()],
                   help="Insert these tables' rows in an order that fills their leaf pages, each taking the next "
                        "AUTOINCREMENT id, with declared foreign keys following (e.g. Content,Tooltips)")
    args = p.parse_args()
    sys.exit(run({"source": args.source, "output": args.output, "pack": args.pack}))


if __name__ == "__main__":
    main()
