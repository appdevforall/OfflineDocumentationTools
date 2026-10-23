#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
migrate_tooltip_schema.py

Converts a documentation.db to documentation schema 3.0.0, which Code On The Go
reads from DocumentationDatabaseVersion 3.0.0 on (ADFA-3305): keyed tooltip
tables, which store the same tooltips in less space and find them by key, and
every template stored Brotli-compressed.

- Tooltips becomes a WITHOUT ROWID table keyed on (categoryId, tag), the key the
  app looks a tooltip up by, holding only that key and the tooltip's id. Its
  UNIQUE (categoryId, tag) index, a second copy of every tag, goes: the table
  is now that index.
- summary and detail move to TooltipTexts, one row per id, which keeps
  AUTOINCREMENT and its sequence: whatever adds a tooltip inserts its text first
  and takes the id that hands out, as before, never one a deleted tooltip had.
  Left in the WITHOUT ROWID table, the text cost 1.18 MB more on the 2026-10-18
  database: such a table is an index B-tree, whose interior pages hold whole
  rows and which spills any row over 1,002 bytes (at 4 KB pages) to a mostly
  empty overflow page. That layout's .br was 0.14 MB smaller, since it keeps
  the text in key order.
- TooltipButtons becomes a WITHOUT ROWID table keyed on (tooltipId,
  buttonNumberId), so a tooltip's buttons are one key range, already in display
  order, instead of a scan of the table and a sort. Its text moves to two lookup
  tables holding each distinct string once: TooltipButtonDescriptions ("View
  full documentation" was stored 43,703 times) and TooltipButtonUris (54,799
  buttons, 20,453 URIs). Descriptions are numbered most used first, so the
  common ones take the fewest bytes in TooltipButtons; URIs in text order.
- Every Templates row is stored Brotli-compressed against the database's
  CompressionDictionary, as Content's text rows are. There is no per-row
  marker: in 3.0.0 every template is compressed, and the app always decodes
  them (the CodeOnTheGo change stacked on #2163). Doing it in the same
  transaction as the tooltip tables means a file declaring 3.0.0 always has
  both. They are compressed with the brotli CLI as populate_db.py's
  DictionaryCompressor compresses (brotli -D <dictionary>, default quality),
  with the CLI's default 24-bit window made explicit (-w 24); the re-mint that
  follows recompresses them against its new dictionary.
  That re-mint is OfflineDocumentationTools PR #48's remint_dictionary.py,
  which has to merge no later than this step does: the one before it
  recompresses Content only, so it refuses a database declaring 3.0.0.
- DocumentationDatabaseVersion becomes one row, 3.0.0: the app no longer reads
  the old tables or plain templates, so this is an incompatible change.

Tooltips.id and TooltipButtons.tooltipId are declared FOREIGN KEYs to
TooltipTexts(id), so rebuild_database.py --pack TooltipTexts renumbers all three
together. Ids keep their values here.

Tooltips.id must be unique and name a TooltipTexts row: whatever adds a tooltip
takes the id its TooltipTexts insert hands out (Code On The Go's plugin writer
does), never one of its own. Two Tooltips rows sharing an id share one text,
so deleting either key's text deletes the other's. Nothing in the schema
enforces it - a UNIQUE (id) index on Tooltips would cost about the 613 pages
this conversion saves - so this step checks it: it refuses old data with a
shared or NULL id, and its read-back finds every tooltip again with its own id
and text.

Measured on the 2026-10-18 database, packed as the pipeline packs it
(110,895,104 bytes; .br 86,918,141 at brotli -q 11 -w 24): this step leaves
106,254,336 bytes (.br 86,658,719), and rebuild_database.py --pack
Content,TooltipTexts --page-size 4096 then 105,697,280 (.br 86,693,653).
Repacking alone moved that .br by 0.17 MB, so the .br change is within noise.
On the 2026-10-18 input database (112,283,648 bytes), the 6 templates go from
50,724 bytes of text to 7,460 stored, which leaves the file 40,960 bytes smaller
than converting the tooltip tables alone.
On a desktop SQLite 3.46, warm, one pinned core, 2,000 keys: a tooltip's
buttons took 0.0033 ms instead of 1.36 ms median (a scan and sort of all
54,799), and the tooltip itself was unchanged, 0.0056 ms against 0.0057.

Before anything is committed, every tooltip and its buttons are read back
through the new tables twice and compared with what the old tables held: by its
key (categoryId, tag), which reaches every tooltip, and by (category name, tag)
with the two queries Code On The Go runs, QUERY_TOOLTIP and
QUERY_TOOLTIP_BUTTONS in idetooltips' TooltipQuery.kt (copied below as
APP_TOOLTIP_QUERY and APP_BUTTONS_QUERY). Both tables are also compared row for
row, and their row counts, and every template is decoded back and compared with
its text. Any difference rolls the whole conversion back, as does a change to
the templates or the dictionary since this step first read them. The CLI's
decoder also accepts large-window streams (brotli --large_window, a window over
24 bits), which the app's decoder, brotli4j, refuses; so a template stored as
one counts as one that does not decode, wherever this step checks.

Templates must arrive plain, as every tool that writes Templates writes them.
One that already decodes against the dictionary is taken for one compressed
already - all of them, or a mix that means some other step ran - and refused,
since compressing it again would hide its text; so is one that is neither UTF-8
text nor such a stream. A plain text can be a valid stream too, if rarely: each
of the 1-byte texts 3 5 7 9 ; = ? is a complete one that decodes to empty (? is
what an empty template compresses to), and no 2- or 3-byte printable text is. A
text starting with < or { never is: that byte sets a bit RFC 7932 requires to be
0. Such a template is refused here before the conversion, as compressed
already; after it, it would pass for compressed, and the app would show it
empty. A database without a CompressionDictionary is refused too. Compressing
needs the brotli CLI on PATH.

Pipeline position: on the copy that ships, after every step that writes
tooltips, all of which write the old tables (TooltipManager.py, docdb-studio,
sync_kdoc_json_to_db.py, the one-off scripts), and after every step that writes
templates, all of which write them plain. On that copy the order is: the
minify, the row drops, the Kotlin trim, the a/ pages-to-templates step, this
step, PR #48's remint_dictionary.py --seeds 4, and rebuild_database.py, which
then packs Content,TooltipTexts: it cannot pack a WITHOUT ROWID table, so not
Tooltips. hm_pages_to_templates.py (PR #43) writes templates too, so it also
goes before this step, after the i/ page steps it lists. populate_db.py,
sync_javadoc_json_to_db.py and load_android_json_db.py refuse a database
declaring 3.0.0 or later; the steps in the open PRs do not check.
Never run it on the master database those tools edit: they read or write the
old shape and plain templates (check-tools, which docdb-regression-test.yaml
runs daily, fails on the new tooltip tables), and nothing converts it back.

A database already converted, declaring major version 3 or later, and whose
every template decodes against the dictionary, is left alone (exit 0). One
already in the new shape that declares less, or holds a template that does not
decode, is refused: the app refuses it too, and only a step run after the
conversion, or a hand edit, can have rewritten its version row or a template,
so this step does not guess what else changed. One whose tooltip tables are in
neither shape, or hold data the new keys cannot (a button with no tooltip, two
buttons with one number, a NULL), or that does not declare major version 2, is
refused too. A refusal writes nothing and exits 1.

Usage:
    ./migrate_tooltip_schema.py documentation.db [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FROM_MAJOR, TO_MAJOR = 2, 3
WHO = "ADFA-3305"
COMMENT = ("Key Tooltips by (categoryId, tag) and TooltipButtons by (tooltipId, buttonNumberId), both WITHOUT ROWID; "
           "tooltip text moved to TooltipTexts, button text to TooltipButtonDescriptions and TooltipButtonUris; "
           "every template Brotli-compressed against CompressionDictionary")

# PR #39 (ADFA-6597) adds this index for the old buttons query; the new primary key is the same index.
REPLACED_INDEX = "TooltipButtonsByTooltip"

OLD_TOOLTIP_COLUMNS = ("id", "categoryId", "tag", "summary", "detail")
NEW_TOOLTIP_COLUMNS = ("id", "categoryId", "tag")
TEXT_COLUMNS = ("id", "summary", "detail")
OLD_BUTTON_COLUMNS = ("tooltipId", "buttonNumberId", "description", "uri")
NEW_BUTTON_COLUMNS = ("tooltipId", "buttonNumberId", "descriptionId", "uriId")
LOOKUPS = {"TooltipButtonDescriptions": ("id", "description"), "TooltipButtonUris": ("id", "uri")}
TABLES = ("Tooltips", "TooltipTexts", "TooltipButtons", *LOOKUPS)

NEW_TABLES = """
CREATE TABLE TooltipTexts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 summary TEXT NOT NULL,
 detail TEXT NOT NULL
);
CREATE TABLE Tooltips (
 id INTEGER NOT NULL,
 categoryId INTEGER NOT NULL,
 tag TEXT NOT NULL,
 PRIMARY KEY (categoryId, tag),
 FOREIGN KEY(categoryId) REFERENCES TooltipCategories(id),
 FOREIGN KEY(id) REFERENCES TooltipTexts(id)
) WITHOUT ROWID;
CREATE TABLE TooltipButtonDescriptions (
 id INTEGER PRIMARY KEY,
 description TEXT NOT NULL
);
CREATE TABLE TooltipButtonUris (
 id INTEGER PRIMARY KEY,
 uri TEXT NOT NULL
);
CREATE TABLE TooltipButtons (
 tooltipId INTEGER NOT NULL,
 buttonNumberId INTEGER NOT NULL,
 descriptionId INTEGER NOT NULL,
 uriId INTEGER NOT NULL,
 PRIMARY KEY (tooltipId, buttonNumberId),
 FOREIGN KEY(tooltipId) REFERENCES TooltipTexts(id),
 FOREIGN KEY(buttonNumberId) REFERENCES TooltipButtonNumbers(id),
 FOREIGN KEY(descriptionId) REFERENCES TooltipButtonDescriptions(id),
 FOREIGN KEY(uriId) REFERENCES TooltipButtonUris(id)
) WITHOUT ROWID;
"""

FILL = (
    "INSERT INTO TooltipTexts (id, summary, detail) SELECT id, summary, detail FROM Tooltips_old ORDER BY id",
    "INSERT INTO Tooltips (id, categoryId, tag) SELECT id, categoryId, tag FROM Tooltips_old ORDER BY categoryId, tag",
    # TooltipTexts carries on from the old table's sequence, so no id a deleted tooltip had is handed out again.
    "DELETE FROM sqlite_sequence WHERE name = 'TooltipTexts'",
    "INSERT INTO sqlite_sequence (name, seq) SELECT 'TooltipTexts', MAX("
    "COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'Tooltips_old'), 0), "
    "COALESCE((SELECT MAX(id) FROM TooltipTexts), 0))",
    "INSERT INTO TooltipButtonDescriptions (description) "
    "SELECT description FROM TooltipButtons_old GROUP BY description ORDER BY COUNT(*) DESC, description",
    "INSERT INTO TooltipButtonUris (uri) SELECT uri FROM TooltipButtons_old GROUP BY uri ORDER BY uri",
    "INSERT INTO TooltipButtons (tooltipId, buttonNumberId, descriptionId, uriId) "
    "SELECT b.tooltipId, b.buttonNumberId, d.id, u.id FROM TooltipButtons_old AS b "
    "JOIN TooltipButtonDescriptions AS d ON d.description = b.description "
    "JOIN TooltipButtonUris AS u ON u.uri = b.uri ORDER BY b.tooltipId, b.buttonNumberId",
)

# What Code On The Go runs, QUERY_TOOLTIP and QUERY_TOOLTIP_BUTTONS in idetooltips/.../TooltipQuery.kt (CodeOnTheGo
# #2163 at 5cc8d2bc9): the tooltip by its category's name and its tag, then its buttons.
APP_TOOLTIP_QUERY = ("SELECT T.id, X.summary, X.detail FROM Tooltips AS T JOIN TooltipTexts AS X ON X.id = T.id "
                     "WHERE T.categoryId IN (SELECT id FROM TooltipCategories WHERE category = ?) AND T.tag = ?")
APP_BUTTONS_QUERY = ("SELECT D.description, U.uri FROM TooltipButtons AS B "
                     "JOIN TooltipButtonDescriptions AS D ON D.id = B.descriptionId "
                     "JOIN TooltipButtonUris AS U ON U.id = B.uriId "
                     "WHERE B.tooltipId = ? ORDER BY B.buttonNumberId")
# The same lookup by categoryId: one row per key even where two categories share a name, and it reaches a tooltip
# whose categoryId names no category, which the app's query cannot.
KEY_QUERY = ("SELECT T.id, X.summary, X.detail FROM Tooltips AS T JOIN TooltipTexts AS X ON X.id = T.id "
             "WHERE T.categoryId = ? AND T.tag = ?")

OLD_TOOLTIP_ROWS = "SELECT id, categoryId, tag, summary, detail FROM Tooltips ORDER BY categoryId, tag"
# What the app's query found in the old tables, by (category name, tag).
OLD_APP_ROWS = ("SELECT C.category, T.tag, T.id, T.summary, T.detail FROM Tooltips AS T "
                "JOIN TooltipCategories AS C ON C.id = T.categoryId")
NEW_TOOLTIP_ROWS = ("SELECT T.id, T.categoryId, T.tag, X.summary, X.detail FROM Tooltips AS T "
                    "JOIN TooltipTexts AS X ON X.id = T.id ORDER BY T.categoryId, T.tag")
OLD_BUTTON_ROWS = "SELECT tooltipId, buttonNumberId, description, uri FROM TooltipButtons ORDER BY tooltipId, buttonNumberId"
NEW_BUTTON_ROWS = ("SELECT B.tooltipId, B.buttonNumberId, D.description, U.uri FROM TooltipButtons AS B "
                   "JOIN TooltipButtonDescriptions AS D ON D.id = B.descriptionId "
                   "JOIN TooltipButtonUris AS U ON U.id = B.uriId ORDER BY B.tooltipId, B.buttonNumberId")

# Data the new keys cannot hold, checked before anything is written.
PROBLEMS = (
    ("button(s) with a NULL tooltipId or buttonNumberId",
     "SELECT COUNT(*) FROM TooltipButtons WHERE tooltipId IS NULL OR buttonNumberId IS NULL"),
    ("button(s) with a NULL description or uri",
     "SELECT COUNT(*) FROM TooltipButtons WHERE description IS NULL OR uri IS NULL"),
    ("button(s) whose tooltipId names no tooltip",
     "SELECT COUNT(*) FROM TooltipButtons WHERE tooltipId NOT IN (SELECT id FROM Tooltips)"),
    ("(tooltipId, buttonNumberId) pair(s) held by more than one button",
     "SELECT COUNT(*) FROM (SELECT 1 FROM TooltipButtons GROUP BY tooltipId, buttonNumberId HAVING COUNT(*) > 1)"),
    ("(categoryId, tag) pair(s) held by more than one tooltip",
     "SELECT COUNT(*) FROM (SELECT 1 FROM Tooltips GROUP BY categoryId, tag HAVING COUNT(*) > 1)"),
    ("tooltip id(s) held by more than one tooltip, or NULL",
     "SELECT COUNT(*) FROM (SELECT 1 FROM Tooltips GROUP BY id HAVING COUNT(*) > 1 OR id IS NULL)"),
    ("tooltip(s) with a NULL categoryId, tag, summary or detail",
     "SELECT COUNT(*) FROM Tooltips WHERE categoryId IS NULL OR tag IS NULL OR summary IS NULL OR detail IS NULL"),
)

_WITHOUT_ROWID = re.compile(r"\)\s*WITHOUT\s+ROWID\s*;?\s*$", re.I)


class Refused(Exception):
    """The database cannot be converted as it is; nothing was written."""


def human(n: int) -> str:
    return f"{n:,}"


# Brotli's largest RFC 7932 window. It is the CLI's default for input on stdin (the same bytes with or without
# -w 24); given a file, the CLI picks its own. compress() asks for it explicitly.
WINDOW_BITS = 24


def large_window(stream: bytes) -> bool:
    """Whether a Brotli stream uses the large-window extension, the only way its window can exceed 24 bits: its
    first seven bits, least significant first, are 1, 000, 001, which RFC 7932's window header leaves invalid. The
    CLI's decoder accepts such a stream; brotli4j's, which the app decodes templates with, refuses it."""
    return bool(stream) and stream[0] & 0x7F == 0x11


class DictionaryCodec:
    """Brotli against the database's shared dictionary, through the brotli CLI and invoked as populate_db.py's
    DictionaryCompressor invokes it (`brotli -D <dictionary> -c`, default quality, here with its default window
    made explicit), since the Python brotli package takes no dictionary. Not imported from there: populate_db.py
    needs markdown-it, and this step needs nothing outside the standard library but the CLI. The dictionary is
    written once to a private temporary file.

    The CLI exits 1 both when it cannot run and when its input is no stream against the dictionary, so a CLI
    that fails would make every template look undecodable. The codec therefore round-trips a probe text first
    and raises Refused, with the CLI's own message, if that fails: after it passes, a template that does not
    decode is the template's fault."""

    PROBE = b"<p>{{ probe | raw }}</p>"

    def __init__(self, dictionary: bytes):
        self._brotli = shutil.which("brotli")
        if self._brotli is None:
            raise Refused("the brotli CLI is not on PATH; 3.0.0 stores every template compressed with it")
        self._dir = tempfile.TemporaryDirectory(prefix="migrate-schema-dictionary-")
        self._dictionary = Path(self._dir.name) / "dictionary.bin"
        self._dictionary.write_bytes(dictionary)
        self.dictionary = dictionary
        stream, error = self._run("-w", str(WINDOW_BITS), data=self.PROBE)
        if stream is not None:
            text, error = self._run("-d", data=stream)
            if text == self.PROBE:
                return
            error = error or "it decoded to other bytes"
        self.close()
        raise Refused(f"the brotli CLI ({self._brotli}) could not compress and decode a test text against the "
                      f"dictionary: {error}")

    def _run(self, *args: str, data: bytes) -> tuple:
        """(output, None) if the CLI succeeds, else (None, what it printed on stderr)."""
        result = subprocess.run([self._brotli, "-D", str(self._dictionary), *args, "-c"], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            return result.stdout, None
        return None, result.stderr.decode("utf-8", "replace").strip() or f"exit status {result.returncode}"

    def compress(self, data: bytes) -> bytes:
        out, error = self._run("-w", str(WINDOW_BITS), data=data)
        if out is None:
            raise RuntimeError(f"the brotli CLI could not compress a template: {error}")
        return out

    def decompress(self, data: bytes):
        """The plaintext, or None if `data` is not a Brotli stream against this dictionary. A large-window stream
        decodes here, though not in the app: see large_window."""
        return self._run("-d", data=data)[0]

    def close(self) -> None:
        self._dir.cleanup()


def load_codec(conn) -> DictionaryCodec:
    """A codec for the database's CompressionDictionary; raises Refused if it has none."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'CompressionDictionary'"
                        ).fetchone():
        raise Refused("no CompressionDictionary table; 3.0.0 stores every template compressed against it")
    row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
    if row is None or not row[0]:
        raise Refused("CompressionDictionary holds no dictionary; 3.0.0 stores every template compressed against it")
    return DictionaryCodec(bytes(row[0]))


def stored_templates(conn) -> dict:
    """Every Templates row as {id: (name, stored bytes)}; empty if there is no Templates table."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'Templates'").fetchone():
        return {}
    out = {}
    for template_id, name, content in conn.execute("SELECT id, name, content FROM Templates ORDER BY id"):
        out[template_id] = (name, None if content is None
                            else content.encode("utf-8") if isinstance(content, str) else bytes(content))
    return out


def _parallel(function, items: list) -> list:
    with ThreadPoolExecutor(max_workers=min(32, os.cpu_count() or 4)) as pool:
        return list(pool.map(function, items))


def plain_templates(conn, codec: DictionaryCodec) -> tuple:
    """({id: (name, plaintext)}, problems) for a database whose templates are still plain text, as every
    tool that writes Templates writes them. A template that already decodes against the dictionary is taken
    for one compressed already - every one, or a mix that means some other step ran - and compressing it
    again would hide its text, so it is a problem; so is one that is neither UTF-8 text nor such a stream,
    or NULL. A very short text can also be a valid stream (see the module docstring); one is refused here as
    compressed already rather than guessed at."""
    stored = stored_templates(conn)
    decoded = _parallel(lambda item: None if item[1][1] is None else codec.decompress(item[1][1]),
                        list(stored.items()))
    plain, compressed, unreadable = {}, [], []
    for (template_id, (name, data)), as_brotli in zip(stored.items(), decoded):
        if data is None:
            unreadable.append(name)
        elif as_brotli is not None:
            compressed.append(name)
        else:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                unreadable.append(name)
                continue
            plain[template_id] = (name, data)
    found = []
    if compressed:
        found.append(f"{human(len(compressed))} of {human(len(stored))} template(s) already decode against the "
                     f"dictionary, so they look compressed already ({', '.join(compressed[:5])}"
                     f"{', ...' if len(compressed) > 5 else ''}); this step compresses plain templates only")
    if unreadable:
        found.append(f"{human(len(unreadable))} template(s) are neither UTF-8 text nor compressed against the "
                     f"dictionary ({', '.join(unreadable[:5])}{', ...' if len(unreadable) > 5 else ''})")
    return plain, found


def compress_templates(conn, codec: DictionaryCodec, plain: dict) -> int:
    """Stores every template compressed, inside the caller's transaction; returns the stored bytes. Each one
    is proved to decode back to its exact text, and to be a stream the app can decode, before it is written."""
    def one(item):
        template_id, (name, data) = item
        blob = codec.compress(data)
        if large_window(blob):
            raise RuntimeError(f"template {name} was compressed to a large-window stream, which the app cannot decode")
        if codec.decompress(blob) != data:
            raise RuntimeError(f"template {name} does not decode back to its text after compression")
        return template_id, blob
    total = 0
    for template_id, blob in _parallel(one, list(plain.items())):
        conn.execute("UPDATE Templates SET content = ? WHERE id = ?", (blob, template_id))
        total += len(blob)
    return total


def templates_read_back(conn, codec: DictionaryCodec, plain: dict) -> list:
    """Differences between the plain templates and what Templates now holds, decoded the way the app decodes
    them; empty if none."""
    differences = []
    stored = stored_templates(conn)
    if {i: n for i, (n, _) in stored.items()} != {i: n for i, (n, _) in plain.items()}:
        differences.append("the Templates rows' ids or names changed")
    decoded = _parallel(lambda item: codec.decompress(item[1][1]) if item[1][1] is not None else None,
                        list(stored.items()))
    for (template_id, (name, data)), text in zip(stored.items(), decoded):
        if template_id in plain and text != plain[template_id][1]:
            differences.append(f"template {name} does not decode to its text")
        elif large_window(data):
            differences.append(f"template {name} is a large-window stream, which the app cannot decode")
    return differences


def backup_database(db_path: Path) -> Path:
    """VACUUM INTO a timestamped sibling, the next free suffix if that exists. The
    same as optimize_db_media.backup_database, which this step does not import
    because that module needs Pillow and the brotli package, and this one needs
    neither."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{stamp}")
    attempt = 1
    while backup_path.exists():
        attempt += 1
        backup_path = db_path.with_name(f"{db_path.name}.backup-{stamp}-{attempt}")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(backup_path),))
    finally:
        conn.close()
    return backup_path


def table_shape(conn, name: str):
    """(column names, primary key column names, WITHOUT ROWID) of a table, or None if there is none."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ? COLLATE NOCASE",
                       (name,)).fetchone()
    if row is None:
        return None
    info = list(conn.execute(f'PRAGMA table_info("{name}")'))
    columns = tuple(c[1].lower() for c in info)
    key = tuple(c[1].lower() for c in sorted((c for c in info if c[5]), key=lambda c: c[5]))
    return columns, key, bool(_WITHOUT_ROWID.search(row[0] or ""))


def _lower(names) -> tuple:
    return tuple(n.lower() for n in names)


def schema_state(conn) -> str:
    """"old" or "new"; raises Refused for tooltip tables in neither shape."""
    shapes = {name: table_shape(conn, name) for name in TABLES}
    if shapes["Tooltips"] is None or shapes["TooltipButtons"] is None:
        raise Refused("no Tooltips or no TooltipButtons table")
    tooltips, texts, buttons = shapes["Tooltips"], shapes["TooltipTexts"], shapes["TooltipButtons"]
    old = (tooltips[0] == _lower(OLD_TOOLTIP_COLUMNS) and not tooltips[2]
           and buttons[0] == _lower(OLD_BUTTON_COLUMNS) and not buttons[2]
           and all(shapes[name] is None for name in ("TooltipTexts", *LOOKUPS)))
    new = (tooltips == (_lower(NEW_TOOLTIP_COLUMNS), ("categoryid", "tag"), True)
           and texts == (_lower(TEXT_COLUMNS), ("id",), False)
           and buttons == (_lower(NEW_BUTTON_COLUMNS), ("tooltipid", "buttonnumberid"), True)
           and all(shapes[name] is not None and shapes[name][0] == _lower(columns)
                   for name, columns in LOOKUPS.items()))
    if old:
        return "old"
    if new:
        return "new"
    found = "; ".join(f"{name} ({', '.join(shape[0])}{', WITHOUT ROWID' if shape[2] else ''})"
                      for name, shape in shapes.items() if shape is not None)
    raise Refused(f"the tooltip tables are in neither the old nor the new shape: {found}")


def newest_major(conn):
    """The newest DocumentationDatabaseVersion row's major, as the app reads it; raises Refused if there is none."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'DocumentationDatabaseVersion'"
                        ).fetchone():
        raise Refused("no DocumentationDatabaseVersion table")
    row = conn.execute("SELECT major FROM DocumentationDatabaseVersion ORDER BY changeTime DESC, rowid DESC LIMIT 1"
                       ).fetchone()
    if row is None:
        raise Refused("DocumentationDatabaseVersion is empty")
    return row[0]


def problems(conn) -> list:
    """What keeps this old-shape database from converting, as messages; empty if nothing does."""
    found = []
    major = newest_major(conn)
    if major != FROM_MAJOR:
        # Below 2 the app would take dictionary-free Brotli rows for dictionary ones; 3 or more is
        # already some other incompatible change, and this one would need the next number.
        found.append(f"DocumentationDatabaseVersion declares major {major}; this step converts {FROM_MAJOR} to "
                     f"{TO_MAJOR}")
    for what, query in PROBLEMS:
        count = conn.execute(query).fetchone()[0]
        if count:
            found.append(f"{human(count)} {what}")
    # Dropping the old tables would drop these with them, silently, or leave them naming columns that are gone.
    names = ", ".join(f"'{t}'" for t in TABLES)
    for name, table in conn.execute(
            f"SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND tbl_name COLLATE NOCASE IN ({names}) "
            "AND sql IS NOT NULL ORDER BY name"):
        if not (name.lower() == REPLACED_INDEX.lower() and table.lower() == "tooltipbuttons"):
            found.append(f"index {name} on {table}, which the conversion would drop; drop it or carry it over by hand")
    mentions = re.compile(r"\b(?:" + "|".join(TABLES) + r")\b", re.I)
    for kind, name, sql in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'view') "
                                        "ORDER BY name"):
        if mentions.search(sql or ""):
            found.append(f"{kind} {name} uses the tooltip tables, which change shape; drop it or rewrite it by hand")
    return found


def _digest(value) -> bytes:
    return hashlib.sha256(repr(value).encode("utf-8")).digest()


def _rows_digest(cursor) -> tuple:
    digest, count = hashlib.sha256(), 0
    for row in cursor:
        digest.update(_digest(row))
        count += 1
    return count, digest.hexdigest()


def snapshot(conn) -> dict:
    """What the old tables hold: each tooltip as the app shows it, keyed by (categoryId, tag) and, as the app's
    query finds them, by (category name, tag); and both tables row for row."""
    by_tooltip = {}
    for tooltip_id, _number, description, uri in conn.execute(OLD_BUTTON_ROWS):
        by_tooltip.setdefault(tooltip_id, []).append((description, uri))
    shown = {}
    for tooltip_id, category_id, tag, summary, detail in conn.execute(OLD_TOOLTIP_ROWS):
        shown[(category_id, tag)] = _digest((tooltip_id, summary, detail, by_tooltip.get(tooltip_id, [])))
    app = {}
    for category, tag, tooltip_id, summary, detail in conn.execute(OLD_APP_ROWS):
        app.setdefault((category, tag), []).append(
            _digest((tooltip_id, summary, detail, by_tooltip.get(tooltip_id, []))))
    return {"shown": shown, "app": {key: sorted(found) for key, found in app.items()},
            "tooltips": _rows_digest(conn.execute(OLD_TOOLTIP_ROWS)),
            "buttons": _rows_digest(conn.execute(OLD_BUTTON_ROWS))}


def _app_view(conn, category: str, tag: str) -> list:
    """What the app shows for (category, tag) through the new tables, with its own two queries."""
    return sorted(_digest((tooltip_id, summary, detail,
                           [tuple(b) for b in conn.execute(APP_BUTTONS_QUERY, (tooltip_id,))]))
                  for tooltip_id, summary, detail in conn.execute(APP_TOOLTIP_QUERY, (category, tag)).fetchall())


def read_back(conn, before: dict) -> list:
    """Differences between `before` and the new tables, read the way the app reads them; empty if none."""
    differences = []
    for (category_id, tag), expected in before["shown"].items():
        rows = conn.execute(KEY_QUERY, (category_id, tag)).fetchall()
        if len(rows) != 1:
            differences.append(f"tooltip ({category_id}, {tag!r}) reads back as {len(rows)} row(s)")
            continue
        tooltip_id, summary, detail = rows[0]
        buttons = [tuple(b) for b in conn.execute(APP_BUTTONS_QUERY, (tooltip_id,))]
        if _digest((tooltip_id, summary, detail, buttons)) != expected:
            differences.append(f"tooltip ({category_id}, {tag!r}) reads back differently")
    for (category, tag), expected in before["app"].items():
        found = _app_view(conn, category, tag)
        if found != expected:
            differences.append(f"tooltip ({category!r}, {tag!r}) reads back differently through the app's query "
                               f"({len(found)} row(s), {len(expected)} expected)")
    for table in ("Tooltips", "TooltipTexts"):
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if total != len(before["shown"]):
            differences.append(f"{table} holds {human(total)} row(s), not {human(len(before['shown']))}")
    if _rows_digest(conn.execute(NEW_TOOLTIP_ROWS)) != before["tooltips"]:
        differences.append("Tooltips' rows differ")
    if _rows_digest(conn.execute(NEW_BUTTON_ROWS)) != before["buttons"]:
        differences.append("TooltipButtons' rows differ")
    return differences


def convert(conn) -> list:
    """Rewrites the tooltip tables inside the caller's transaction; returns what it dropped besides them. The
    version row is declare_version's, once the templates are compressed too."""
    dropped = [name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ? COLLATE NOCASE "
        "AND tbl_name = 'TooltipButtons' COLLATE NOCASE", (REPLACED_INDEX,))]
    # Renamed first, so the new tables are created under their own names with the SQL above as written.
    conn.execute("ALTER TABLE TooltipButtons RENAME TO TooltipButtons_old")
    conn.execute("ALTER TABLE Tooltips RENAME TO Tooltips_old")
    for statement in NEW_TABLES.split(";"):
        if statement.strip():
            conn.execute(statement)
    for statement in FILL:
        conn.execute(statement)
    # Dropping Tooltips_old also drops its sqlite_sequence row: nothing hands out its ids any more.
    conn.execute("DROP TABLE TooltipButtons_old")
    conn.execute("DROP TABLE Tooltips_old")
    return dropped


def declare_version(conn) -> None:
    conn.execute("DELETE FROM DocumentationDatabaseVersion")
    conn.execute("INSERT INTO DocumentationDatabaseVersion (major, minor, patch, who, comment) VALUES (?, 0, 0, ?, ?)",
                 (TO_MAJOR, WHO, COMMENT))


def run(cfg: dict) -> int:
    db_path = cfg["db_path"]
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path, isolation_level=None)
    codec = None
    try:
        try:
            if schema_state(conn) == "new":
                major = newest_major(conn)
                if major < TO_MAJOR:
                    raise Refused(f"the tooltip tables are already in the new shape, but DocumentationDatabaseVersion "
                                  f"declares major {major}, so the app refuses this file; a step run after the "
                                  f"conversion rewrote the version row. Declare {TO_MAJOR}.0.0 by hand once you know "
                                  "it changed nothing else")
                codec = load_codec(conn)
                stored = stored_templates(conn)
                decoded = _parallel(lambda item: None if item[1] is None else codec.decompress(item[1]),
                                    list(stored.values()))
                plain = [name for (name, data), text in zip(stored.values(), decoded)
                         if text is None or large_window(data)]
                if plain:
                    raise Refused(f"the tooltip tables are already in the new shape, but {human(len(plain))} of "
                                  f"{human(len(stored))} template(s) do not decode against the dictionary as the app "
                                  f"decodes them ({', '.join(plain[:5])}{', ...' if len(plain) > 5 else ''}), and "
                                  f"{TO_MAJOR}.0.0 stores every template compressed; a step run after the conversion "
                                  "wrote them")
                print(f"{db_path}: the tooltip tables are already in the new shape and every template is "
                      "compressed; nothing to do.")
                return 0
            found = problems(conn)
            try:
                codec = load_codec(conn)
                plain, template_problems = plain_templates(conn, codec)
                found += template_problems
            except Refused as exc:
                found.append(str(exc))
        except Refused as exc:
            print(f"error: {exc}; nothing was changed", file=sys.stderr)
            return 1
        if found:
            for problem in found:
                print(f"error: {problem}", file=sys.stderr)
            print("Nothing was changed.", file=sys.stderr)
            return 1

        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("Tooltips", "TooltipButtons")}
        distinct = conn.execute("SELECT COUNT(DISTINCT description), COUNT(DISTINCT uri) FROM TooltipButtons").fetchone()
        text_bytes = sum(len(data) for _name, data in plain.values())
        print(f"Converting {human(counts['Tooltips'])} tooltip(s) and {human(counts['TooltipButtons'])} button(s); "
              f"{human(distinct[0])} distinct description(s), {human(distinct[1])} distinct URI(s); "
              f"compressing {human(len(plain))} template(s), {human(text_bytes)} bytes of text.")

        if not cfg["dry_run"]:
            print(f"Backing up {db_path} ...")
            print(f"Backup written to {backup_database(db_path)}")
        size_before = db_path.stat().st_size
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = snapshot(conn)
            # The templates checked above are the ones compressed: none may have changed since, nor the dictionary
            # the codec read before this transaction, which they are compressed against.
            if {i: d for i, (_n, d) in stored_templates(conn).items()} != {i: d for i, (_n, d) in plain.items()}:
                raise Refused("the templates changed while this step ran")
            row = conn.execute("SELECT data FROM CompressionDictionary WHERE id = 1").fetchone()
            if row is None or row[0] != codec.dictionary:
                raise Refused("the dictionary changed while this step ran")
            dropped = convert(conn)
            stored_bytes = compress_templates(conn, codec, plain)
            declare_version(conn)
            differences = read_back(conn, before) + templates_read_back(conn, codec, plain)
        except Refused as exc:
            conn.execute("ROLLBACK")
            print(f"error: {exc}; rolled back, nothing was changed", file=sys.stderr)
            return 1
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if differences:
            conn.execute("ROLLBACK")
            for difference in differences[:20]:
                print(f"error: {difference}", file=sys.stderr)
            print(f"error: {human(len(differences))} difference(s) reading the tooltips or templates back; rolled "
                  "back, nothing was changed", file=sys.stderr)
            return 1
        print(f"Every tooltip and its buttons read back identically ({human(len(before['shown']))} tooltip(s)).")
        print(f"Every template decodes back to its text ({human(len(plain))} template(s), {human(text_bytes)} -> "
              f"{human(stored_bytes)} bytes).")
        for name in dropped:
            print(f"  dropped index {name}: TooltipButtons' new primary key is the same index")
        if cfg["dry_run"]:
            conn.execute("ROLLBACK")
            print("Dry run: nothing written.")
            return 0
        conn.execute("COMMIT")
    except (sqlite3.Error, RuntimeError) as exc:
        print(f"error: {exc}; nothing was changed", file=sys.stderr)
        return 1
    finally:
        if codec is not None:
            codec.close()
        conn.close()

    print("Vacuuming database...")
    vac = sqlite3.connect(db_path)
    try:
        vac.execute("VACUUM")
    finally:
        vac.close()
    print(f"\nDone. DocumentationDatabaseVersion {FROM_MAJOR}.x -> {TO_MAJOR}.0.0; "
          f"{human(size_before)} -> {human(db_path.stat().st_size)} bytes.")
    print("Next: re-mint the dictionary with OfflineDocumentationTools PR #48's remint_dictionary.py --seeds 4, "
          f"which recompresses the templates too in {TO_MAJOR}.0.0 (one without it refuses this file), then "
          "rebuild_database.py with --pack Content,TooltipTexts; it cannot pack the WITHOUT ROWID Tooltips.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db_path", type=Path, help="SQLite database to convert in place, e.g. documentation.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Convert, compress and check inside a transaction, then roll it back: nothing written or "
                        "backed up")
    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(run({"db_path": args.db_path, "dry_run": args.dry_run}))


if __name__ == "__main__":
    main()
