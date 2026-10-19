#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
rebuild_database.py

Copies a documentation.db into a fresh file - same schema, same rows with the
same rowids - to drop the space SQLite gives back no other way: the bytes
reserved at the end of every page. --page-size gives the copy a different page
size; otherwise it keeps the source's.

    uv run scripts/rebuild_database.py documentation.db rebuilt.db --pack Content,Tooltips --page-size 4096

On a database in documentation schema 3.0.0 (after migrate_tooltip_schema.py,
#45), Tooltips is a WITHOUT ROWID table, which --pack refuses: pack
Content,TooltipTexts there instead.

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
hand it, and links every reference to the ids handed out - what a fresh
build does when it records each row's new id. A reference is a declared
foreign key or a column listed in UNDECLARED_REFERENCES: Bookshelf.contentID
names a Content row with no FOREIGN KEY, kept in step only by Content's
AddBook and DeleteBook triggers. Ids continue after the old sequence or the
largest id, whichever is higher, as AUTOINCREMENT's do, so an id can stop
naming a row (one quoted in an old tooltip feedback email, say) but never
come to name a different one. On the 2026-10-13 database Content
goes from 19,158 leaf pages to 14,524 and Tooltips from 16,069 to 12,402. A
table some row references without a match is refused, since that orphan's
id could otherwise name a new row; so is a table with a trigger that ties
its id to a column that is neither kind of reference (check_triggers),
since that link would keep the old ids. The gain lasts only until rows are
added, which append in arrival order, so packing belongs in every release's
rebuild.

Page size (--page-size N): the 2026-10-13 database has 1 KB pages, which no
tool in this repository chose - SQLite's default was 1 KB until 3.12.0, and a
database keeps its page size through every VACUUM - and 4 KB is SQLite's
default now. Each page carries an 8-byte header and each overflow page
a 4-byte pointer to the next, so for the megabyte-sized media rows fewer,
larger pages cost less: on that database after the ADFA-3305 conversion,
packed, 4 KB pages make the file 1,090,560 bytes (0.9%) smaller, and its
release .br (brotli -q 11) 313,164 bytes smaller.
--pack plans each page's rows for the size the copy gets.

How: a new file with the chosen page size and auto_vacuum off - set
explicitly, because some SQLite builds default it on (the sqlite3 shell this
was tested with did), which adds a pointer-map page for every ~200 pages.
Every table is created from the source's own SQL and filled in rowid order,
keeping each row's rowid; then the indexes, triggers and views, then
sqlite_sequence, user_version and application_id; then VACUUM. Before the
result is kept, it is compared with the source - schema, every row of every
table with its rowid, sqlite_sequence, both pragmas, integrity_check and
foreign_key_check, with any ids --pack renumbered mapped back first (a
reference still on an old id is a difference) - and any difference deletes
it and fails the run.

The source is opened read-only and never changed. OUTPUT must not exist
(ADFA-5296: a rebuilt database is saved somewhere other than where it came
from); swap it in by hand once it has been checked.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

# Columns holding another table's rowid with no FOREIGN KEY declaring it, so
# PRAGMA foreign_key_list cannot find them: (table, column, parent table).
# --pack follows these exactly like a declared key.
UNDECLARED_REFERENCES = (
    # Kept in step with Content.id only by Content's AddBook and DeleteBook triggers.
    ("Bookshelf", "contentID", "Content"),
    # WebServer.kt renders a row through the template it names. 0 means none
    # and names no row, so --pack Templates is refused while any row has it.
    ("Content", "templateId", "Templates"),
)


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
    """[(child table, column)] for every column holding `parent`'s rowid - the
    values a renumbering of `parent` has to follow: each declared foreign key
    naming it, and each UNDECLARED_REFERENCES entry present in this database."""
    alias = alias_column(conn, parent)
    key = None if alias is None else list(conn.execute(f'PRAGMA table_info("{parent}")'))[alias][1]
    found = []
    for child, _sql, _without_rowid in tables:
        for fk in conn.execute(f'PRAGMA foreign_key_list("{child}")'):
            if fk[2] == parent and (fk[4] is None or fk[4] == key):
                found.append((child, fk[3]))
        for table, column, target in UNDECLARED_REFERENCES:
            if table.lower() == child.lower() and target.lower() == parent.lower():
                found += [(child, name) for _cid, name, *_rest in conn.execute(f'PRAGMA table_info("{child}")')
                          if name.lower() == column.lower() and (child, name) not in found]
    return found


_LITERAL_OR_COMMENT = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)
_NAME = r'"[^"]+"|\[[^\]]+\]|`[^`]+`|\w+'
_ROW_FIELD = re.compile(rf"\b(?:NEW|OLD)\s*\.\s*({_NAME})", re.I)
_WRITE_TARGET = re.compile(rf"\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)"
                           rf"\s+({_NAME})", re.I)


def _unquoted(name: str) -> str:
    return name[1:-1] if name[0] in "\"[`" else name


def check_triggers(conn, table: str, references: list) -> None:
    """Refuses to pack `table` when one of its triggers ties its id to a column
    the pack would not renumber - a reference with no FOREIGN KEY that only
    the trigger knows about, as Bookshelf.contentID was.

    Each statement in a trigger ON `table` that uses NEW.<id> or OLD.<id> (its
    INTEGER PRIMARY KEY, rowid, oid or _rowid_) and writes - INSERT, REPLACE,
    UPDATE or DELETE - is taken to store or match that id in the table it
    writes, so that table must hold one of `references` and the statement must
    name it. AddBook passes: it writes NEW.id into Bookshelf.contentID, and
    into the placeholder title too, which is why the check is per statement
    and not per column. Limits of reading the SQL with a regex (string
    literals and comments are blanked first): it cannot tell which column
    receives the id, so a statement naming a followed column and writing the
    id into another one as well passes; it reads only triggers ON `table`, not
    ones on other tables that copy a reference on; it does not follow UPDATE
    ... FROM. A statement using the id whose target it cannot read is refused."""
    alias = alias_column(conn, table)
    ids = {"rowid", "oid", "_rowid_"}
    if alias is not None:
        ids.add(list(conn.execute(f'PRAGMA table_info("{table}")'))[alias][1].lower())
    followed = {}
    for child, column in references:
        followed.setdefault(child.lower(), set()).add(column.lower())
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                                  "AND tbl_name = ? COLLATE NOCASE", (table,)).fetchall():
        body = re.split(r"\bBEGIN\b", _LITERAL_OR_COMMENT.sub("''", sql), maxsplit=1, flags=re.I)[-1]
        for statement in body.split(";"):
            if not any(_unquoted(field).lower() in ids for field in _ROW_FIELD.findall(statement)):
                continue
            if re.match(r"\s*SELECT\b", statement, re.I):
                continue  # writes nothing
            target = _WRITE_TARGET.match(statement)
            child = _unquoted(target.group(1)) if target else None
            named = {_unquoted(word).lower() for word in re.findall(_NAME, statement)}
            if not followed.get((child or "").lower(), set()) & named:
                where = f"writes to {child}" if child else "writes somewhere this tool cannot read"
                raise RuntimeError(f"--pack {table}: trigger {name} uses {table}'s id in a statement that {where} "
                                   f"through no column the pack renumbers, so that link would keep the old ids; "
                                   f"declare the column a FOREIGN KEY or add it to UNDECLARED_REFERENCES")


def fingerprint(path: Path, renumbered: dict = None, followed: dict = None) -> dict:
    """What a rebuild must keep: schema, every row of every table with its
    rowid, sqlite_sequence, the two header pragmas, and what integrity_check
    and foreign_key_check say.

    For a copy made with --pack, `renumbered` ({table: {new rowid: old}}) and
    `followed` ({table: {column: parent}}) map every renumbered id back, so the
    copy must still match the source exactly. A followed column holding
    anything but NULL or a new id - an old id the pack left behind - maps to a
    marker no source row holds, so it differs. Rows are compared through a
    digest per row, sorted by (original) rowid."""
    renumbered, followed = renumbered or {}, followed or {}
    conn = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        def unmap(table, rowid):
            return renumbered.get(table, {}).get(rowid, rowid)

        def follow(parent, value):
            if value is None:
                return None
            ids = renumbered.get(parent, {})
            return ids[value] if value in ids else ("not renumbered", value)

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
                    row[index + offset] = follow(parent, row[index + offset])
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


def rebuild(source: Path, output: Path, pack=(), page_size: int = None) -> tuple:
    """Writes the copy, with page_size or the source's; returns (renumbered,
    followed, advanced): the id maps for fingerprint(), and {table: new
    sqlite_sequence value} for each packed table."""
    src = sqlite3.connect(read_only_uri(source), uri=True)
    try:
        page_size = page_size or src.execute("PRAGMA page_size").fetchone()[0]
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
            references = references_to(src, tables, table)
            check_triggers(src, table, references)
            for child, column in references:
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
        # Triggers too, so AddBook adds no Bookshelf row for each .pdf copied.
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


def page_size_of(path: Path) -> int:
    conn = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        return conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()


def page_size_arg(value: str) -> int:
    """A page size SQLite accepts: a power of two from 512 to 65536. It ignores
    any other without a word, which would leave the copy at the default."""
    size = int(value)
    if size < 512 or size > 65536 or size & (size - 1):
        raise argparse.ArgumentTypeError(f"{value} is not a power of two from 512 to 65536")
    return size


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
        renumbered, followed, advanced = rebuild(source, output, pack, cfg.get("page_size"))
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
          f"pragmas identical. Page size {page_size_of(source)} -> {page_size_of(output)}; reserved bytes per "
          f"page {reserved_bytes(source)} -> {reserved_bytes(output)}; "
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
                        "AUTOINCREMENT id, with declared foreign keys and UNDECLARED_REFERENCES following "
                        "(e.g. Content,Tooltips; Content,TooltipTexts on a schema 3.0.0 database)")
    p.add_argument("--page-size", type=page_size_arg, default=None, metavar="BYTES",
                   help="Page size for the copy, a power of two from 512 to 65536 (default: the source's; "
                        "use 4096 for documentation.db)")
    args = p.parse_args()
    sys.exit(run({"source": args.source, "output": args.output, "pack": args.pack, "page_size": args.page_size}))


if __name__ == "__main__":
    main()
