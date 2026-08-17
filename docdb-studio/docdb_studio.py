#!/usr/bin/env python3
"""Browse the join of Tooltips and TooltipCategories with pagination."""

import argparse
import atexit
import csv
import http.server
import io
import mimetypes
import os
import platform as _platform
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, NamedTuple

import brotli
import flet as ft
import flet_datatable2 as fdt

try:
    import posthog
except ImportError:
    posthog = None  # analytics becomes a no-op

mimetypes.add_type("text/markdown", ".md")

CONTENT_CHUNK_SIZE = 1024 * 1024  # Android consumer probes for `path-N` continuations at this size.

# Conservative batch size for SQL `WHERE col IN (?,...)` queries — older SQLite
# builds cap at 999 host parameters, modern ones at 32766; we stay well under.
SQL_PARAM_BATCH = 500

SQLITE_PAGE_SIZE_BYTES = 2048  # ADFA-5141: smallest on the real ~300MB docdb, of the sizes tested.

UI_PAGE_SIZE = 50

# Built-in HTTP server for browsing Content rows in a real browser.
CONTENT_SERVER_PORT = 6175

# PostHog project API key — write-only, safe to embed per PostHog docs.
POSTHOG_PROJECT_API_KEY = "phc_DkwupnQn98knfJXCSSn5wTRY9QB2tE4KxZf7kWhMVhGz"
POSTHOG_HOST = "https://us.i.posthog.com"
APP_VERSION = "0.1.0"  # keep in sync with pyproject.toml

# Special LastChange row that bumps on every mutation, regardless of which
# per-set row was touched. Acts as a global "database version" marker.
WHOLEDB_KEY = "wholedb"

COLUMN_LABELS = ["ID", "Category", "Actions", "Tag", "Summary", "Detail"]
ACTIONS_COLUMN_WIDTH = 200

# Zebra stripe: alternating row backgrounds (LOWEST / LOW for more pronounced contrast)
ZEBRA_EVEN = ft.Colors.SURFACE_CONTAINER_LOWEST
ZEBRA_ODD = ft.Colors.SURFACE_CONTAINER_LOW

JOIN_BASE = """
SELECT t.id, tc.category, t.tag, t.summary, t.detail
FROM Tooltips t
JOIN TooltipCategories tc ON t.categoryId = tc.id
"""
JOIN_ORDER = " ORDER BY tc.category, t.tag"

COUNT_BASE = """
SELECT COUNT(*)
FROM Tooltips t
JOIN TooltipCategories tc ON t.categoryId = tc.id
"""

SEARCH_WHERE = (
    " WHERE (t.tag LIKE ? ESCAPE '\\'"
    " OR t.summary LIKE ? ESCAPE '\\'"
    " OR t.detail LIKE ? ESCAPE '\\'"
    " OR tc.category LIKE ? ESCAPE '\\'"
    # Non-correlated subquery: SQLite evaluates it once (not per Tooltips row),
    # so URI/description search stays a single TooltipButtons scan instead of a
    # per-row nested loop. IN also keeps each tooltip to one result row.
    " OR t.id IN (SELECT tb.tooltipId FROM TooltipButtons tb"
    " WHERE tb.uri LIKE ? ESCAPE '\\' OR tb.description LIKE ? ESCAPE '\\'))"
)


def sanitize_text(value: str, *, field: str, allow_newline: bool = False) -> str:
    """NFC-normalize value; reject any Unicode "Other" category codepoint
    (Cc control, Cf format/zero-width/bidi, Cs surrogate, Co private-use, Cn unassigned).
    With allow_newline=True, U+000A is permitted.
    Raises ValueError naming `field` and the offending codepoint on first violation."""
    normalized = unicodedata.normalize("NFC", value)
    for offset, ch in enumerate(normalized):
        if allow_newline and ch == "\n":
            continue
        if unicodedata.category(ch)[0] == "C":
            name = unicodedata.name(ch, "unnamed")
            raise ValueError(
                f"{field}: disallowed character U+{ord(ch):04X} ({name}) at offset {offset}"
            )
    return normalized


def get_max_category_length(db_path: Path) -> int:
    """Return the length of the longest category string in TooltipCategories."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT MAX(LENGTH(category)) FROM TooltipCategories"
        )
        row = cur.fetchone()
        return row[0] or 0


def category_column_width(db_path: Path) -> int:
    """Compute fixed width in px for the category column from current DB data."""
    n = get_max_category_length(db_path)
    n = max(n, len("Category"))  # ensure header fits
    # Approximate px: character count * typical char width + padding
    return n * 9 + 28


def _build_like_pattern(term: str) -> str:
    """Translate a user-facing search term into a SQL LIKE pattern.

    Convention: search is anchored at the start of the string ("starts with")
    by default. The character `*` is a user-facing wildcard (matches zero or
    more characters anywhere it appears in the term). SQL LIKE metacharacters
    `%` and `_` typed by the user are escaped, so a search for `100%` finds
    the literal substring `100%` rather than acting as a wildcard.

    Examples (all assume `LIKE ? ESCAPE '\\'`):
      'foo'      -> 'foo%'        (starts with foo)
      '*foo'     -> '%foo%'       (contains foo)
      'foo*bar'  -> 'foo%bar%'    (starts with foo, contains bar later)
      '100%'     -> '100\\%%'     (starts with literal "100%")
    """
    escaped = (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return escaped.replace("*", "%") + "%"


def _build_contains_pattern(term: str) -> str:
    """Like `_build_like_pattern` but matches the term *anywhere* (substring),
    not just at the start. Used for button URIs and descriptions, where the
    useful part (a filename such as ``use.html``) is typically in the middle of
    the value rather than at its start. ``*`` still works and metacharacters are
    still escaped; e.g. ``'use.html' -> '%use.html%'``.
    """
    return "%" + _build_like_pattern(term)


def _search_params(term: str) -> tuple[str, ...]:
    # One pattern per placeholder in SEARCH_WHERE, in order: tag, summary,
    # detail, category (prefix/"starts-with"), then button uri, description
    # (substring/"contains", since URLs are searched by an interior fragment).
    prefix = _build_like_pattern(term)
    contains = _build_contains_pattern(term)
    return (prefix, prefix, prefix, prefix, contains, contains)


def get_total_count(db_path: Path, search_term: str | None = None) -> int:
    sql = COUNT_BASE.rstrip() + (SEARCH_WHERE if search_term else "")
    params = _search_params(search_term) if search_term else ()
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone()[0]


def get_page(
    db_path: Path, limit: int, offset: int, search_term: str | None = None
) -> list[tuple]:
    sql = (
        JOIN_BASE.rstrip()
        + (SEARCH_WHERE if search_term else "")
        + JOIN_ORDER
        + " LIMIT ? OFFSET ?"
    )
    params = (_search_params(search_term) + (limit, offset)) if search_term else (limit, offset)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def get_tooltip_by_id(
    db_path: Path, tooltip_id: int
) -> tuple[str, str, str, str] | None:
    """Return (category, tag, summary, detail) for the tooltip id, or None."""
    sql = """
        SELECT tc.category, t.tag, t.summary, t.detail
        FROM Tooltips t
        JOIN TooltipCategories tc ON t.categoryId = tc.id
        WHERE t.id = ?
    """
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(sql, (tooltip_id,))
        row = cur.fetchone()
        return tuple(row) if row else None


def get_tooltip_buttons(
    db_path: Path, tooltip_id: int
) -> list[tuple[int | None, str | None, str | None]]:
    """Return rows (buttonNumberId, description, uri) for the tooltip, sorted by buttonNumberId."""
    sql = """
        SELECT buttonNumberId, description, uri
        FROM TooltipButtons
        WHERE tooltipId = ?
        ORDER BY buttonNumberId
    """
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(sql, (tooltip_id,))
        return cur.fetchall()


def _uri_path_for_content_lookup(uri: str) -> str:
    """Strip ?query and #fragment; return the path for Content.path lookup."""
    u = uri or ""
    if "?" in u:
        u = u.split("?", 1)[0]
    if "#" in u:
        u = u.split("#", 1)[0]
    return u


def _uri_fragment(uri: str) -> str:
    """Return the #fragment portion of a URI (without the leading #), or '' if absent."""
    u = uri or ""
    if "#" not in u:
        return ""
    return u.split("#", 1)[1]


def uris_exist_in_content(db_path: Path, uris: list[str]) -> set[str]:
    """Return the set of URIs that exist as Content.path. Query/fragment stripped before lookup."""
    paths = [_uri_path_for_content_lookup(u) for u in uris]
    non_empty = [p for p in paths if p]
    if not non_empty:
        return set()
    try:
        with sqlite3.connect(db_path) as conn:
            placeholders = ",".join("?" * len(non_empty))
            cur = conn.execute(
                f'SELECT path FROM "Content" WHERE path IN ({placeholders})',
                non_empty,
            )
            return {row[0] for row in cur.fetchall()}
    except sqlite3.OperationalError:
        return set()


def get_all_content_paths(db_path: Path) -> set[str]:
    """Return every Content.path as a set, for fast in-memory membership checks."""
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute('SELECT path FROM "Content"')
            return {row[0] for row in cur.fetchall()}
    except sqlite3.OperationalError:
        return set()


class _AnchorCollector(HTMLParser):
    """HTMLParser subclass that collects id="..." and <a name="..."> values in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[str] = []
        self._seen: set[str] = set()

    def _consider(self, value: str | None) -> None:
        if value and value not in self._seen:
            self._seen.add(value)
            self.anchors.append(value)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attr_dict = dict(attrs)
        self._consider(attr_dict.get("id"))
        if tag.lower() == "a":
            self._consider(attr_dict.get("name"))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)


def extract_html_anchors(blob: bytes) -> list[str]:
    """Parse HTML bytes and return the in-order, deduplicated list of anchor names.

    Picks up `id="..."` on any element plus legacy `<a name="...">`. Returns an empty list
    on parse failure or when the blob has no anchors."""
    text = blob.decode("utf-8", errors="replace")
    collector = _AnchorCollector()
    try:
        collector.feed(text)
        collector.close()
    except Exception:
        # Malformed HTML — return what we have so far.
        pass
    return collector.anchors


def _split_chunk_path(path: str) -> tuple[str, int] | None:
    """If `path` looks like `<base>-<N>` for N>=1, return (base, N); else None."""
    idx = path.rfind("-")
    if idx <= 0 or idx == len(path) - 1:
        return None
    suffix = path[idx + 1 :]
    if not suffix.isdigit():
        return None
    n = int(suffix)
    if n < 1:
        return None
    return path[:idx], n


def get_html_anchors_for_path(db_path: Path, base_path: str) -> list[str]:
    """Fetch the row(s) for `base_path` (reassembling chunks and decompressing brotli),
    and return the anchors found in that HTML. Returns [] if the path is missing,
    isn't text/html, or fails to decompress."""
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                """
                SELECT c.content, ct.value, ct.compression
                FROM "Content" c
                JOIN ContentTypes ct ON c.contentTypeID = ct.id
                WHERE c.path = ?
                """,
                (base_path,),
            )
            base_row = cur.fetchone()
            if base_row is None or base_row[1] != "text/html":
                return []
            compression = base_row[2]
            parts: list[bytes] = [base_row[0]]
            n = 1
            while True:
                chunk_cur = conn.execute(
                    'SELECT content FROM "Content" WHERE path = ?',
                    (f"{base_path}-{n}",),
                )
                chunk_row = chunk_cur.fetchone()
                if chunk_row is None:
                    break
                parts.append(chunk_row[0])
                n += 1
    except sqlite3.OperationalError:
        return []
    full = b"".join(parts)
    if compression == "brotli":
        try:
            full = brotli.decompress(full)
        except brotli.error:
            return []
    return extract_html_anchors(full)


def update_uri_status_icon(
    icon: ft.Icon,
    uri: str,
    valid_paths: set[str],
    db_path: Path | None = None,
) -> None:
    """Set icon glyph/color/tooltip based on whether the URI maps to a valid Content.path.

    With `db_path=None` (cheap on_change refresh), only the path part is checked. With
    `db_path` provided (typically on_blur), a non-empty `#fragment` is also validated
    against the HTML's actual anchors via `get_html_anchors_for_path`."""
    stripped = (uri or "").strip()
    if not stripped:
        icon.icon = ft.Icons.HELP_OUTLINE
        icon.color = ft.Colors.GREY
        icon.tooltip = "Enter a URI"
        return
    path_part = _uri_path_for_content_lookup(stripped)
    if path_part not in valid_paths:
        icon.icon = ft.Icons.CLOSE
        icon.color = ft.Colors.RED
        icon.tooltip = "No matching path in Content"
        return
    fragment = _uri_fragment(stripped)
    if not fragment or db_path is None:
        icon.icon = ft.Icons.CHECK
        icon.color = ft.Colors.GREEN
        icon.tooltip = "Path exists in Content"
        return
    anchors = get_html_anchors_for_path(db_path, path_part)
    if fragment in anchors:
        icon.icon = ft.Icons.CHECK
        icon.color = ft.Colors.GREEN
        icon.tooltip = "Path and anchor both exist"
    else:
        icon.icon = ft.Icons.CLOSE
        icon.color = ft.Colors.RED
        icon.tooltip = f"Anchor '#{fragment}' not found in {path_part}"


def find_broken_button_uris(
    db_path: Path, buttons: list[tuple[int, str, str]]
) -> list[tuple[int, str]]:
    """Return [(button_number_id, uri), ...] for buttons whose URI doesn't match Content.path.

    `buttons` is the (button_number_id, description, uri) shape used in the edit/add forms.
    Empty URIs are not flagged (they will be rejected by the existing required-field check).
    """
    non_empty = [u for _, _, u in buttons if u and u.strip()]
    if not non_empty:
        return []
    valid = uris_exist_in_content(db_path, non_empty)
    broken: list[tuple[int, str]] = []
    for bid, _desc, uri in buttons:
        u = (uri or "").strip()
        if u and _uri_path_for_content_lookup(u) not in valid:
            broken.append((bid, uri))
    return broken


def get_all_button_rows(
    db_path: Path,
) -> list[tuple[int, str, str, int, str, str]]:
    """Return (tooltip_id, category, tag, button_number_id, description, uri) for every non-empty TooltipButton."""
    sql = """
        SELECT t.id, tc.category, t.tag, tb.buttonNumberId, tb.description, tb.uri
        FROM TooltipButtons tb
        JOIN Tooltips t ON tb.tooltipId = t.id
        JOIN TooltipCategories tc ON t.categoryId = tc.id
        WHERE tb.uri IS NOT NULL AND tb.uri != ''
        ORDER BY tc.category, t.tag, tb.buttonNumberId
    """
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(sql)
        return cur.fetchall()


def get_tooltip_export_rows(
    db_path: Path, search_term: str | None = None
) -> list[list[str]]:
    """Return tooltips as rows matching CSV_TEMPLATE_HEADERS, ready to round-trip
    through the update importer.

    When `search_term` is given, only tooltips matching the same filter as the
    browse list are included. Buttons are slotted by buttonNumberId
    (1 -> button1, 2 -> button2, 3 -> button3); button numbers outside 1-3 are
    omitted (the CSV template supports at most three buttons).

    Only rows that satisfy the import invariant (`validate_tooltip_row_fields`)
    are emitted — the export never produces a record the importer would reject
    (e.g. an empty or newline-bearing summary), so the round-trip stays clean.
    Use `count_exportable_tooltips` to learn how many matching tooltips were
    skipped for failing that invariant.
    """
    tooltip_sql = (
        JOIN_BASE.rstrip()
        + (SEARCH_WHERE if search_term else "")
        + JOIN_ORDER
    )
    params = _search_params(search_term) if search_term else ()
    with sqlite3.connect(db_path) as conn:
        tooltips = conn.execute(tooltip_sql, params).fetchall()
        # One pass over TooltipButtons, grouped by tooltip -> {buttonNumberId: (label, uri)}.
        buttons_by_tooltip: dict[int, dict[int, tuple[str, str]]] = {}
        for tid, bnum, desc, uri in conn.execute(
            "SELECT tooltipId, buttonNumberId, description, uri FROM TooltipButtons"
        ):
            buttons_by_tooltip.setdefault(tid, {})[bnum] = (desc or "", uri or "")

    category_name_to_id = {name: cid for cid, name in get_categories(db_path)}
    allowed_button_ids = set(get_button_number_ids(db_path))

    rows: list[list[str]] = []
    for tooltip_id, category, tag, summary, detail in tooltips:
        buttons = buttons_by_tooltip.get(tooltip_id, {})
        row = [category or "", tag or "", summary or "", detail or ""]
        for bnum in (1, 2, 3):
            label, uri = buttons.get(bnum, ("", ""))
            row.extend([label, uri])
        field_errors, _ = validate_tooltip_row_fields(
            row, category_name_to_id, allowed_button_ids
        )
        if field_errors:
            continue  # not importable — skip so the export round-trips cleanly
        rows.append(row)
    return rows


def find_broken_button_rows(
    rows: list[tuple[int, str, str, int, str, str]], valid_paths: set[str]
) -> list[tuple[int, str, str, int, str, str]]:
    """Filter button rows to just those whose URI doesn't match any path in valid_paths."""
    return [
        row
        for row in rows
        if _uri_path_for_content_lookup(row[5] or "") not in valid_paths
    ]


def find_empty_summary_tooltips(db_path: Path) -> list[tuple[int, str]]:
    """Return (tooltip_id, tag) for tooltips whose summary is empty or
    whitespace-only. These fail the import invariant (summary is required), so
    CSV export skips them; the validate page surfaces them for fixing."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id, tag FROM Tooltips WHERE TRIM(summary) = '' ORDER BY tag"
        )
        return [(row[0], row[1] or "") for row in cur.fetchall()]


def get_content_paths_count(db_path: Path, search: str | None = None) -> int:
    sql = 'SELECT COUNT(*) FROM "Content"'
    params: tuple = ()
    if search:
        sql += " WHERE path LIKE ? ESCAPE '\\'"
        params = (_build_like_pattern(search),)
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchone()[0]


def get_content_paths_page(
    db_path: Path,
    limit: int,
    offset: int,
    search: str | None = None,
) -> list[str]:
    sql = 'SELECT path FROM "Content"'
    if search:
        sql += " WHERE path LIKE ? ESCAPE '\\'"
    sql += " ORDER BY path LIMIT ? OFFSET ?"
    params: tuple
    if search:
        params = (_build_like_pattern(search), limit, offset)
    else:
        params = (limit, offset)
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute(sql, params).fetchall()]


def format_bytes(n: int) -> str:
    """Render n bytes as a short human-readable string ('847 B', '12.3 KB', '1.4 MB', '2.1 GB')."""
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PB"


def get_paths_with_sizes(db_path: Path) -> list[tuple[str, int]]:
    """Return [(path, byte_size), ...] for every Content row."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute('SELECT path, LENGTH(content) FROM "Content"')
        return cur.fetchall()


def build_content_tree(rows: list[tuple[str, int]]) -> dict:
    """Group (path, size) pairs into a nested tree.

    Each node is `{"children": dict[name -> node], "files": dict[name -> size], "size": int}`
    where folder `size` is the recursive sum of every descendant file size.
    Paths with no slash live in `files` at the root.
    """
    root: dict = {"children": {}, "files": {}, "size": 0}
    for path, size in rows:
        if not path:
            continue
        parts = path.split("/")
        if len(parts) == 1:
            root["files"][parts[0]] = size
            root["size"] += size
            continue
        node = root
        for segment in parts[:-1]:
            child = node["children"].get(segment)
            if child is None:
                child = {"children": {}, "files": {}, "size": 0}
                node["children"][segment] = child
            node["size"] += size
            node = child
        node["files"][parts[-1]] = size
        node["size"] += size
    return root


def get_button_reference_counts(db_path: Path) -> dict[str, int]:
    """Aggregate TooltipButtons.uri values into a {stripped_path: count} map.

    URIs are normalised via `_uri_path_for_content_lookup` (strips `?query` and `#fragment`).
    Empty values are excluded.
    """
    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT uri FROM TooltipButtons WHERE uri IS NOT NULL AND uri != ''"
        )
        for (uri,) in cur.fetchall():
            stripped = _uri_path_for_content_lookup(uri or "")
            if not stripped:
                continue
            counts[stripped] = counts.get(stripped, 0) + 1
    return counts


def get_tooltip_for_edit(
    db_path: Path, tooltip_id: int
) -> tuple[int, str, str, str, str] | None:
    """Return (category_id, category, tag, summary, detail) for the tooltip id, or None."""
    sql = """
        SELECT t.categoryId, tc.category, t.tag, t.summary, t.detail
        FROM Tooltips t
        JOIN TooltipCategories tc ON t.categoryId = tc.id
        WHERE t.id = ?
    """
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(sql, (tooltip_id,))
        row = cur.fetchone()
        return tuple(row) if row else None


def get_categories(db_path: Path) -> list[tuple[int, str]]:
    """Return (id, category) for all categories, ordered by category."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id, category FROM TooltipCategories ORDER BY category"
        )
        return cur.fetchall()


def get_category_name(db_path: Path, category_id: int) -> str | None:
    """Return category string for the given id, or None if not found."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT category FROM TooltipCategories WHERE id = ?",
            (category_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def update_last_change(
    db_path: Path, documentation_set: str, who: str | None
) -> None:
    """Stamp the LastChange row for the given documentationSet, plus the
    global WHOLEDB_KEY row, in a single transaction."""

    def _stamp(conn: sqlite3.Connection, doc_set: str) -> None:
        cur = conn.execute(
            """
            UPDATE LastChange SET changeTime = datetime('now'), who = ?
            WHERE documentationSet = ?
            """,
            (who, doc_set),
        )
        if cur.rowcount == 0:
            conn.execute(
                """
                INSERT INTO LastChange (documentationSet, changeTime, who)
                VALUES (?, datetime('now'), ?)
                """,
                (doc_set, who),
            )

    with sqlite3.connect(db_path) as conn:
        _stamp(conn, documentation_set)
        if documentation_set != WHOLEDB_KEY:
            _stamp(conn, WHOLEDB_KEY)
        conn.commit()
    conn.close()


# Serializes vacuum_database so two concurrent callers can't race: VACUUM
# INTO takes a read snapshot, and without this a write committed by another
# connection between one caller's snapshot and its later os.replace would be
# silently dropped when that stale snapshot gets swapped into place.
_vacuum_lock = threading.Lock()


def vacuum_database(db_path: Path) -> None:
    """Reclaim free pages and pin the page size (ADFA-5141) by rewriting the DB
    into a fresh file via VACUUM INTO, then atomically swapping it into place.
    Runs after every mutation that includes a DELETE — DB hygiene is
    non-negotiable per project policy.

    Serialized process-wide via _vacuum_lock: without it, two concurrent
    callers could race (see _vacuum_lock's own comment for the failure mode).

    This deliberately avoids in-place VACUUM + a journal_mode round-trip.
    Switching a WAL-mode database away from WAL requires exclusive access --
    no other connection may have the file open at all -- which is impractical
    to guarantee in a desktop app that may have other connections open
    elsewhere (e.g. a live data browser, or even the caller's own connection:
    Python's `with sqlite3.connect(...) as conn:` does not close conn on
    exit, and empirically an unclosed connection can keep the file locked
    well past its enclosing function's return). VACUUM INTO only needs a read
    snapshot of the source, so it works regardless of what else currently has
    db_path open.

    The rewrite happens in a temp file created next to db_path (so the final
    os.replace is same-filesystem and atomic, and per the ADFA-5088 CWE-377
    lesson this avoids writing into a shared, guessable /tmp). tempfile.mkstemp
    always creates its file mode 0600 regardless of the original's mode or the
    process umask, so db_path's original permission bits are restored on the
    swapped-in file (confirmed on real hardware during QA: without this, every
    vacuum silently dropped a 644 documentation.db to 600). VACUUM INTO always
    produces a plain rollback-journal file regardless of the source's
    journal_mode, so if the source was WAL, journal_mode=WAL is reapplied to
    the new file (via its final path, so the resulting -wal/-shm sidecars get
    the right name) before it replaces the original; any sidecars left behind
    by the file just replaced are then stale and removed.

    Callers should invoke this from a worker thread when triggered from the
    UI: on a ~380 MB DB the rewrite takes several seconds and would otherwise
    freeze the event loop.

    Note: this only serializes vacuum_database against itself -- it does not
    protect against a plain write (one that never reaches this function, e.g.
    a concurrent insert/update on another thread outside the DB-hygiene gate)
    committing during the snapshot-to-swap window and being silently lost.
    Closing that gap fully would mean every writer in this file taking
    _vacuum_lock around its own write+commit too; not done here since it
    depends on this app's broader UI threading model (whether the UI actually
    permits overlapping mutations on one db_path) rather than anything local
    to this function.
    """
    with _vacuum_lock:
        db_path = Path(db_path)
        original_mode = stat.S_IMODE(db_path.stat().st_mode)
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            (journal_mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            was_wal = journal_mode.lower() == "wal"
        conn.close()

        fd, tmp_name = tempfile.mkstemp(dir=db_path.parent, suffix=".vacuum.tmp")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with sqlite3.connect(db_path, timeout=30.0) as conn:
                conn.execute(f"PRAGMA page_size={SQLITE_PAGE_SIZE_BYTES}")
                # Bound parameter, not an f-string: VACUUM INTO's target
                # accepts one, which sidesteps having to escape a path that
                # contains a single quote (e.g. a user directory named
                # "David's Docs").
                conn.execute("VACUUM INTO ?", (str(tmp_path),))
            os.replace(tmp_path, db_path)
            os.chmod(db_path, original_mode)
        finally:
            tmp_path.unlink(missing_ok=True)

        # Any -wal/-shm sidecars still sitting at db_path's name at this point
        # are for the file just replaced -- guaranteed stale, since the
        # swapped-in file was just VACUUM INTO'd fresh (plain rollback-journal,
        # no sidecars).
        for suffix in ("-wal", "-shm"):
            stale = db_path.with_name(db_path.name + suffix)
            stale.unlink(missing_ok=True)

        if was_wal:
            # Reapply on db_path's final name (not tmp_path's) so the
            # resulting sidecars are named correctly -- VACUUM INTO always
            # produces a plain rollback-journal file regardless of the
            # source's journal_mode.
            with sqlite3.connect(db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")

    with _page_size_confirmed_lock:
        _page_size_confirmed.add(db_path)


# Paths already confirmed at SQLITE_PAGE_SIZE_BYTES, so _page_size_migration_pending
# can skip re-opening the DB on every import call once the one-time migration is done.
# Guarded by a lock since imports can run migration checks from worker threads.
_page_size_confirmed: set[Path] = set()
_page_size_confirmed_lock = threading.Lock()


def _page_size_migration_pending(db_path: Path) -> bool:
    """Cheap read-only check for whether vacuum_database still needs to run to
    reach SQLITE_PAGE_SIZE_BYTES (ADFA-5141).

    vacuum_database is otherwise only triggered by DB-hygiene call sites gated
    on "did this mutation delete/overwrite anything" — which a pure-insert
    workflow never satisfies. Callers OR this into that gate so the one-time
    migration still happens on the tool's common all-insert paths.
    """
    db_path = Path(db_path)
    with _page_size_confirmed_lock:
        if db_path in _page_size_confirmed:
            return False
    # Matches fetch_content_for_path's timeout: a concurrent vacuum_database can
    # hold an exclusive lock for several seconds, and the default 5s busy timeout
    # would otherwise surface a spurious failure on an import that has already committed.
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        (page_size,) = conn.execute("PRAGMA page_size").fetchone()
    conn.close()
    pending = page_size != SQLITE_PAGE_SIZE_BYTES
    if not pending:
        with _page_size_confirmed_lock:
            _page_size_confirmed.add(db_path)
    return pending


def get_categories_for_tooltips(
    db_path: Path, tooltip_ids: list[int]
) -> set[str]:
    """Return the set of category names that the given tooltip ids belong to."""
    if not tooltip_ids:
        return set()
    names: set[str] = set()
    with sqlite3.connect(db_path) as conn:
        for batch in _chunked(tooltip_ids, SQL_PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                f"""
                SELECT DISTINCT tc.category
                FROM Tooltips t
                JOIN TooltipCategories tc ON t.categoryId = tc.id
                WHERE t.id IN ({placeholders})
                """,
                batch,
            )
            for (name,) in cur.fetchall():
                if name:
                    names.add(name)
    return names


def delete_tooltips_bulk(db_path: Path, tooltip_ids: list[int]) -> int:
    """Delete the given tooltip ids and their TooltipButtons rows. Returns rows deleted from Tooltips."""
    if not tooltip_ids:
        return 0
    deleted = 0
    with sqlite3.connect(db_path) as conn:
        for batch in _chunked(tooltip_ids, SQL_PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            conn.execute(
                f"DELETE FROM TooltipButtons WHERE tooltipId IN ({placeholders})",
                batch,
            )
            cur = conn.execute(
                f"DELETE FROM Tooltips WHERE id IN ({placeholders})",
                batch,
            )
            deleted += cur.rowcount
        conn.commit()
    conn.close()
    if deleted or _page_size_migration_pending(db_path):
        vacuum_database(db_path)
    return deleted


def sanitize_clipboard_paste(text: str) -> str:
    """Strip Unicode "Other" category codepoints (control / format / surrogate / private-use)
    from a clipboard paste, while preserving newlines and tabs. NFC-normalised."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFC", text)
    return "".join(
        ch
        for ch in normalized
        if ch in ("\n", "\t") or unicodedata.category(ch)[0] != "C"
    )


def fetch_content_for_path(
    db_path: Path, base_path: str
) -> tuple[bytes, str] | None:
    """Return (decompressed_bytes, mime) for `base_path`, reassembling chunks
    and decompressing brotli where applicable. None if the path is missing or
    decompression fails."""
    try:
        # vacuum_database can hold an exclusive lock for several seconds on the
        # ~380 MB DB (longer on the one-time ADFA-5141 page_size migration);
        # the default 5s busy timeout would otherwise turn that into a false
        # 404 for content that genuinely exists.
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            cur = conn.execute(
                """
                SELECT c.content, ct.value, ct.compression
                FROM "Content" c
                JOIN ContentTypes ct ON c.contentTypeID = ct.id
                WHERE c.path = ?
                """,
                (base_path,),
            )
            base_row = cur.fetchone()
            if base_row is None:
                return None
            mime = base_row[1]
            compression = base_row[2]
            parts: list[bytes] = [base_row[0]]
            n = 1
            while True:
                chunk_cur = conn.execute(
                    'SELECT content FROM "Content" WHERE path = ?',
                    (f"{base_path}-{n}",),
                )
                chunk_row = chunk_cur.fetchone()
                if chunk_row is None:
                    break
                parts.append(chunk_row[0])
                n += 1
    except sqlite3.OperationalError:
        return None
    full = b"".join(parts)
    if compression == "brotli":
        try:
            full = brotli.decompress(full)
        except brotli.error:
            return None
    return full, mime


class _ContentHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that serves rows of the Content table by path. The bound
    subclass populates `db_path`."""

    db_path: Path = Path()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — base-class signature
        # Default would spam stdout for every request; the GUI doesn't need it.
        pass

    def do_GET(self) -> None:  # noqa: N802 — base-class signature
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path).lstrip("/")
        if not path:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"docdb-studio content server: specify a path")
            return
        result = fetch_content_for_path(self.db_path, path)
        if result is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Not found: {path}".encode("utf-8"))
            return
        body, mime = result
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_content_web_server(
    db_path: Path, port: int = CONTENT_SERVER_PORT
) -> http.server.ThreadingHTTPServer | None:
    """Bind a daemon-thread HTTP server on 127.0.0.1:port serving Content rows.
    Returns the server (so callers can shut it down) or None if the bind failed."""
    handler_cls = type(
        "_BoundContentHTTPHandler",
        (_ContentHTTPHandler,),
        {"db_path": db_path},
    )
    try:
        httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", port), handler_cls
        )
    except OSError:
        return None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def insert_tooltip(
    db_path: Path,
    category_id: int,
    tag: str,
    summary: str,
    detail: str,
) -> int:
    """Insert a new tooltip. Returns the new id. Raises sqlite3.IntegrityError if (categoryId, tag) is duplicate."""
    tag = sanitize_text(tag.strip(), field="tag")
    summary = sanitize_text(summary, field="summary")
    detail = sanitize_text(detail, field="detail", allow_newline=True)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO Tooltips (categoryId, tag, summary, detail)
            VALUES (?, ?, ?, ?)
            """,
            (category_id, tag, summary, detail),
        )
        conn.commit()
        new_id = cur.lastrowid or 0
    conn.close()
    if _page_size_migration_pending(db_path):
        vacuum_database(db_path)
    return new_id


def update_tooltip(
    db_path: Path,
    tooltip_id: int,
    category_id: int,
    tag: str,
    summary: str,
    detail: str,
) -> None:
    """Update the tooltip. Raises sqlite3.IntegrityError if (categoryId, tag) is duplicate."""
    tag = sanitize_text(tag.strip(), field="tag")
    summary = sanitize_text(summary, field="summary")
    detail = sanitize_text(detail, field="detail", allow_newline=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE Tooltips
            SET categoryId = ?, tag = ?, summary = ?, detail = ?
            WHERE id = ?
            """,
            (category_id, tag, summary, detail, tooltip_id),
        )
        conn.commit()
    conn.close()
    if _page_size_migration_pending(db_path):
        vacuum_database(db_path)


def get_button_number_ids(db_path: Path) -> list[int]:
    """Return all button number ids from TooltipButtonNumbers, ordered."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT id FROM TooltipButtonNumbers ORDER BY id")
        return [row[0] for row in cur.fetchall()]


def add_tooltip_button(
    db_path: Path,
    tooltip_id: int,
    button_number_id: int,
    description: str,
    uri: str,
) -> None:
    """Insert a row into TooltipButtons."""
    description = sanitize_text(description.strip(), field="button description")
    uri = sanitize_text(uri.strip(), field="button uri")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri)
            VALUES (?, ?, ?, ?)
            """,
            (tooltip_id, button_number_id, description, uri),
        )
        conn.commit()
    conn.close()
    if _page_size_migration_pending(db_path):
        vacuum_database(db_path)


def update_tooltip_button(
    db_path: Path,
    tooltip_id: int,
    button_number_id: int,
    description: str,
    uri: str,
) -> None:
    """Update description and uri for one TooltipButtons row."""
    description = sanitize_text(description.strip(), field="button description")
    uri = sanitize_text(uri.strip(), field="button uri")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE TooltipButtons
            SET description = ?, uri = ?
            WHERE tooltipId = ? AND buttonNumberId = ?
            """,
            (description, uri, tooltip_id, button_number_id),
        )
        conn.commit()
    conn.close()
    if _page_size_migration_pending(db_path):
        vacuum_database(db_path)


def delete_tooltip_button(
    db_path: Path, tooltip_id: int, button_number_id: int
) -> None:
    """Delete one row from TooltipButtons."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            DELETE FROM TooltipButtons
            WHERE tooltipId = ? AND buttonNumberId = ?
            """,
            (tooltip_id, button_number_id),
        )
        conn.commit()
    conn.close()
    vacuum_database(db_path)


def replace_tooltip_buttons(
    db_path: Path,
    tooltip_id: int,
    buttons: list[tuple[int, str, str]],
) -> None:
    """Replace all TooltipButtons for this tooltip with the given list (order_id, description, uri)."""
    cleaned: list[tuple[int, str, str]] = []
    for button_number_id, description, uri in buttons:
        cleaned.append(
            (
                button_number_id,
                sanitize_text(description.strip(), field="button description"),
                sanitize_text(uri.strip(), field="button uri"),
            )
        )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM TooltipButtons WHERE tooltipId = ?",
            (tooltip_id,),
        )
        for button_number_id, description, uri in cleaned:
            conn.execute(
                """
                INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri)
                VALUES (?, ?, ?, ?)
                """,
                (tooltip_id, button_number_id, description, uri),
            )
        conn.commit()
    conn.close()
    vacuum_database(db_path)


CSV_TEMPLATE_HEADERS = [
    "category",
    "tag",
    "summary",
    "detail",
    "button1_label",
    "button1_uri",
    "button2_label",
    "button2_uri",
    "button3_label",
    "button3_uri",
]


def get_csv_template_content() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_TEMPLATE_HEADERS)
    return buf.getvalue()


def get_existing_tooltip_keys(db_path: Path) -> set[tuple[int, str]]:
    """Return set of (categoryId, tag) for all tooltips."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT categoryId, tag FROM Tooltips"
        )
        return {(row[0], row[1].strip()) for row in cur.fetchall()}


_CSV_CELL_FIELD_NAMES = (
    "category", "tag", "summary", "detail",
    "button1 label", "button1 uri",
    "button2 label", "button2 uri",
    "button3 label", "button3 uri",
)


def validate_tooltip_row_fields(
    row: list[str],
    category_name_to_id: dict[str, int],
    allowed_button_ids: set[int],
) -> tuple[list[str], tuple[int, str] | None]:
    """Field-level validation for a single CSV/export row — the invariant a row
    must satisfy to be importable, shared by CSV import and CSV export.

    Returns ``(errors, key)`` with *unprefixed* error messages (no line numbers)
    and, when the row's identity fields are otherwise valid, ``key=(categoryId,
    tag)`` for the caller's cross-row duplicate/existence checks (``None`` when
    the row has field errors or no usable key). An empty ``errors`` list means
    the row is safe to import — and therefore safe to export for round-trip.
    """
    expected_columns = len(CSV_TEMPLATE_HEADERS)
    if len(row) != expected_columns:
        return ([f"expected {expected_columns} fields, got {len(row)}."], None)

    cleaned_cells: list[str] = []
    for cell, field_name in zip(row, _CSV_CELL_FIELD_NAMES):
        allow_nl = field_name == "detail"
        try:
            cleaned_cells.append(
                sanitize_text(cell.strip(), field=field_name, allow_newline=allow_nl)
            )
        except ValueError as e:
            return ([str(e)], None)

    (
        category_name, tag, summary, _detail,
        b1_label, b1_uri, b2_label, b2_uri, b3_label, b3_uri,
    ) = cleaned_cells

    errors: list[str] = []
    if not category_name:
        errors.append("category is required.")
    elif category_name not in category_name_to_id:
        errors.append(f"category {category_name!r} is not in TooltipCategories.")
    if not tag:
        errors.append("tag is required.")
    if not summary:
        errors.append("summary is required.")

    for pair_name, label, uri, bid in [
        ("button1", b1_label, b1_uri, 1),
        ("button2", b2_label, b2_uri, 2),
        ("button3", b3_label, b3_uri, 3),
    ]:
        has_label = bool(label)
        has_uri = bool(uri)
        if has_label != has_uri:
            errors.append(f"{pair_name} requires both label and uri or neither.")
        if (has_label or has_uri) and bid not in allowed_button_ids:
            errors.append(
                f"{pair_name} references button id {bid} which is not in TooltipButtonNumbers."
            )

    cid = category_name_to_id.get(category_name)
    key = (cid, tag) if (not errors and cid is not None and tag) else None
    return (errors, key)


def validate_csv_upload(
    db_path: Path,
    rows: list[list[str]],
    category_name_to_id: dict[str, int],
    allowed_button_ids: set[int],
    mode: str = "insert",
) -> list[str]:
    """
    Validate CSV data rows (header already stripped). Return list of error messages.
    Rows must be 10-element lists per line.

    `mode` controls duplicate handling:
      - 'insert' (default): rows whose (category, tag) already exist in the DB are rejected.
      - 'update': pre-existing rows are accepted; the import will overwrite them.
    Duplicates within the same file are always rejected, regardless of mode.
    """
    if mode not in ("insert", "update"):
        raise ValueError(f"validate_csv_upload: unknown mode {mode!r}")
    errors: list[str] = []
    if not rows:
        errors.append("File has no data rows.")
        return errors

    seen_in_file: set[tuple[int, str]] = set()
    existing = get_existing_tooltip_keys(db_path)

    for i, row in enumerate(rows):
        line_num = i + 2  # 1-based, line 1 is header
        field_errors, key = validate_tooltip_row_fields(
            row, category_name_to_id, allowed_button_ids
        )
        if field_errors:
            errors.extend(f"Line {line_num}: {e}" for e in field_errors)
            continue
        if key is not None:
            if key in seen_in_file:
                errors.append(f"Line {line_num}: duplicate (category, tag) in file.")
            elif mode == "insert" and key in existing:
                errors.append(
                    f"Line {line_num}: (category, tag) already exists in database."
                )
            else:
                seen_in_file.add(key)

    return errors


def import_csv_rows(
    db_path: Path,
    rows: list[list[str]],
    category_name_to_id: dict[str, int],
    mode: str = "insert",
) -> tuple[int, int]:
    """Insert validated CSV data rows into Tooltips and TooltipButtons (single transaction).

    `mode='insert'` adds new rows and assumes none collide (validated upstream).
    `mode='update'` replaces existing (categoryId, tag) rows: the existing tooltip
    is updated in place, its TooltipButtons are deleted, and the new buttons are
    inserted. Rows that are not pre-existing are inserted as in 'insert' mode.

    Returns (inserted_count, updated_count).
    """
    if mode not in ("insert", "update"):
        raise ValueError(f"import_csv_rows: unknown mode {mode!r}")
    inserted = 0
    updated = 0
    with sqlite3.connect(db_path) as conn:
        for row in rows:
            (
                category_name,
                tag,
                summary,
                detail,
                b1_label,
                b1_uri,
                b2_label,
                b2_uri,
                b3_label,
                b3_uri,
            ) = (c.strip() for c in row)
            cid = category_name_to_id[category_name]

            tooltip_id: int = 0
            if mode == "update":
                existing = conn.execute(
                    "SELECT id FROM Tooltips WHERE categoryId = ? AND tag = ?",
                    (cid, tag),
                ).fetchone()
                if existing is not None:
                    tooltip_id = existing[0]
                    conn.execute(
                        """
                        UPDATE Tooltips
                        SET summary = ?, detail = ?
                        WHERE id = ?
                        """,
                        (summary, detail, tooltip_id),
                    )
                    conn.execute(
                        "DELETE FROM TooltipButtons WHERE tooltipId = ?",
                        (tooltip_id,),
                    )
                    updated += 1
            if tooltip_id == 0:
                cur = conn.execute(
                    """
                    INSERT INTO Tooltips (categoryId, tag, summary, detail)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cid, tag, summary, detail),
                )
                tooltip_id = cur.lastrowid or 0
                inserted += 1
            for bid, label, uri in [
                (1, b1_label, b1_uri),
                (2, b2_label, b2_uri),
                (3, b3_label, b3_uri),
            ]:
                if label and uri:
                    conn.execute(
                        """
                        INSERT INTO TooltipButtons (tooltipId, buttonNumberId, description, uri)
                        VALUES (?, ?, ?, ?)
                        """,
                        (tooltip_id, bid, label, uri),
                    )
        conn.commit()
    conn.close()
    if updated or _page_size_migration_pending(db_path):
        vacuum_database(db_path)
    return inserted, updated


class ImportItem(NamedTuple):
    source: Path
    base_path: str
    content_type_id: int
    mime: str
    compression: str


class ScanResult(NamedTuple):
    mapped: list[ImportItem]
    unmapped: list[tuple[Path, str | None]]
    overwrites: list[str]
    overwrite_row_ids_by_base: dict[str, list[int]]
    orphans: list[str]
    orphan_row_ids: list[int]
    skipped_symlinks: list[Path]
    skipped_hidden: list[Path]
    skipped_bad_name: list[tuple[Path, str]] = []


class ImportSummary(NamedTuple):
    files_imported: int
    files_overwritten: int
    orphans_deleted: int
    rows_inserted: int
    files_skipped_error: int
    errors: list[tuple[Path, str]]
    imported_by_mime: dict[str, int]


def walk_content_folder(
    root: Path,
) -> tuple[list[Path], list[Path], list[Path]]:
    """Walk root recursively. Returns (regular_files, symlinks, hidden_files).

    - Symlinks (to anything) are reported separately and not followed.
    - Files/dirs starting with '.' are reported as hidden; hidden dirs are not descended into.
    """
    regular: list[Path] = []
    symlinks: list[Path] = []
    hidden: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name.startswith("."):
                hidden.append(p)
            elif p.is_symlink():
                symlinks.append(p)
            else:
                regular.append(p)
    return regular, symlinks, hidden


def mime_for_filename(filename: str) -> str | None:
    """Return MIME type for filename based on extension, or None if unknown."""
    mime, _ = mimetypes.guess_type(filename)
    return mime


def get_content_types(db_path: Path) -> dict[str, tuple[int, str]]:
    """Return {ContentTypes.value: (id, compression)} for every row."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT id, value, compression FROM ContentTypes")
        result = {value: (cid, compression) for cid, value, compression in cur.fetchall()}
    conn.close()
    return result


def get_languages(db_path: Path) -> list[tuple[int, str]]:
    """Return [(id, value), ...] for all languages, ordered by value."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT id, value FROM Languages ORDER BY value")
        return cur.fetchall()


def compress_for_storage(data: bytes, compression: str) -> bytes:
    """Apply compression policy. 'brotli' encodes; anything else passes through unchanged."""
    if compression == "brotli":
        return brotli.compress(data)
    return data


def fragment_blob(blob: bytes, chunk_size: int = CONTENT_CHUNK_SIZE) -> list[bytes]:
    """Split blob into chunks of up to chunk_size. Empty input returns one empty chunk."""
    if not blob:
        return [b""]
    return [blob[i : i + chunk_size] for i in range(0, len(blob), chunk_size)]


def target_paths(base_path: str, fragment_count: int) -> list[str]:
    """Return chunk paths: [base, base-1, base-2, ...] of length fragment_count."""
    if fragment_count < 1:
        raise ValueError("fragment_count must be >= 1")
    if fragment_count == 1:
        return [base_path]
    return [base_path] + [f"{base_path}-{i}" for i in range(1, fragment_count)]


def _chunked(seq: list, size: int):
    """Yield successive `size`-length slices of `seq`."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _logical_base(path: str) -> str:
    """Inverse of target_paths: strip a trailing -<digits> suffix iff present.

    'a/big.bin-2' -> 'a/big.bin'; 'a/foo.bin-extra.txt' -> unchanged (suffix
    isn't all digits); 'a/page.html' -> unchanged.
    """
    head, sep, tail = path.rpartition("-")
    if sep and head and tail.isdigit():
        return head
    return path


def _delete_content_by_ids(
    conn: sqlite3.Connection, row_ids: list[int]
) -> None:
    """Bulk-delete Content rows by id, chunked under the SQL parameter ceiling."""
    for batch in _chunked(row_ids, SQL_PARAM_BATCH):
        placeholders = ",".join("?" * len(batch))
        conn.execute(
            f'DELETE FROM "Content" WHERE id IN ({placeholders})',
            batch,
        )


def prescan_content_import(
    db_path: Path,
    folder: Path,
    content_types: dict[str, tuple[int, str]],
) -> ScanResult:
    """Walk `folder`, classify each file, and identify overwrites + orphans.

    Overwrites are mapped files whose base_path already exists in the DB (will
    be replaced). Orphans are DB rows under the chosen folder's prefix whose
    logical base path isn't represented in the import (will be deleted to make
    the DB match the folder)."""
    regular, symlinks, hidden = walk_content_folder(folder)
    candidates: list[ImportItem] = []
    unmapped: list[tuple[Path, str | None]] = []
    bad_name: list[tuple[Path, str]] = []

    for f in regular:
        rel = f.relative_to(folder.parent).as_posix()
        try:
            sanitize_text(rel, field="path")
        except ValueError as e:
            bad_name.append((f, str(e)))
            continue
        mime = mime_for_filename(f.name)
        if mime is None or mime not in content_types:
            unmapped.append((f, mime))
            continue
        cid, compression = content_types[mime]
        candidates.append(
            ImportItem(
                source=f,
                base_path=rel,
                content_type_id=cid,
                mime=mime,
                compression=compression,
            )
        )

    mapped_bases = {item.base_path for item in candidates}
    folder_prefix = folder.name + "/"
    overwrite_set: set[str] = set()
    orphan_set: set[str] = set()
    overwrite_row_ids_by_base: dict[str, list[int]] = {}
    orphan_row_ids: list[int] = []

    with sqlite3.connect(db_path) as conn:
        # Range query rather than `LIKE 'foldername/%'` so the scan uses the
        # UNIQUE(path) index (default LIKE is case-insensitive and does not).
        # Successor of '/' (0x2F) is '0' (0x30); the half-open range
        # ['foldername/', 'foldername0') is exactly the set of paths that
        # start with 'foldername/'.
        cur = conn.execute(
            'SELECT id, path FROM "Content" WHERE path >= ? AND path < ?',
            (folder_prefix, folder.name + chr(ord("/") + 1)),
        )
        for row_id, db_row_path in cur.fetchall():
            if not db_row_path.startswith(folder_prefix):
                continue
            base = _logical_base(db_row_path)
            if base in mapped_bases:
                overwrite_set.add(base)
                overwrite_row_ids_by_base.setdefault(base, []).append(row_id)
            else:
                orphan_set.add(base)
                orphan_row_ids.append(row_id)
    conn.close()

    return ScanResult(
        mapped=list(candidates),
        unmapped=unmapped,
        overwrites=sorted(overwrite_set),
        overwrite_row_ids_by_base=overwrite_row_ids_by_base,
        orphans=sorted(orphan_set),
        orphan_row_ids=orphan_row_ids,
        skipped_symlinks=symlinks,
        skipped_hidden=hidden,
        skipped_bad_name=bad_name,
    )


def import_content_files(
    db_path: Path,
    plan: list[ImportItem],
    language_id: int,
    user_name: str,
    documentation_set: str,
    orphan_row_ids: list[int] | None = None,
    overwrite_row_ids_by_base: dict[str, list[int]] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> ImportSummary:
    """Insert plan items, deleting existing rows by id where prescan said to.

    `orphan_row_ids` is the bulk list of Content rows under the chosen folder
    that have no corresponding file in the import — they are deleted by id in
    one chunked pass. `overwrite_row_ids_by_base` maps each plan item's
    base_path to the list of existing row ids for that file; those rows are
    deleted just before the new chunks are inserted, so a per-file read error
    leaves the original content intact.

    Both id structures come from `prescan_content_import` and are typed
    `list[int]` / `dict[str, list[int]]`; deletion uses `id IN (...)` against
    the PRIMARY KEY index, which is O(log n) per row regardless of how big the
    Content table is.

    `progress_callback(phase, current, total)` fires per-batch in the orphan
    phase and per-item in the overwrite/add phases; the caller is responsible
    for throttling UI updates."""
    files_imported = 0
    files_overwritten = 0
    orphans_deleted = 0
    rows_inserted = 0
    files_error = 0
    errors: list[tuple[Path, str]] = []
    imported_by_mime: dict[str, int] = {}

    orphan_ids = orphan_row_ids or []
    overwrite_ids_map = overwrite_row_ids_by_base or {}
    overwrite_set = set(overwrite_ids_map.keys())
    overwrite_total = sum(1 for item in plan if item.base_path in overwrite_set)
    add_total = len(plan) - overwrite_total
    delete_total = len(orphan_ids)

    def _report(phase: str, current: int, total: int) -> None:
        if progress_callback is not None and total > 0:
            progress_callback(phase, current, total)

    with sqlite3.connect(db_path) as conn:
        # Phase 1: bulk-delete orphans by id. Reports progress per batch so a
        # large orphan list still shows the bar advancing.
        deleted_so_far = 0
        for batch in _chunked(orphan_ids, SQL_PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            conn.execute(
                f'DELETE FROM "Content" WHERE id IN ({placeholders})',
                batch,
            )
            deleted_so_far += len(batch)
            _report("delete", deleted_so_far, delete_total)
        orphans_deleted = delete_total

        # Phases 2 + 3: per-item read, delete-old-by-id, insert.
        adds_done = 0
        overwrites_done = 0
        for item in plan:
            is_overwrite = item.base_path in overwrite_set
            try:
                data = item.source.read_bytes()
            except OSError as e:
                files_error += 1
                errors.append((item.source, f"read error: {e}"))
                if is_overwrite:
                    overwrites_done += 1
                    _report("overwrite", overwrites_done, overwrite_total)
                else:
                    adds_done += 1
                    _report("add", adds_done, add_total)
                continue

            stored = compress_for_storage(data, item.compression)
            chunks = fragment_blob(stored)
            paths = target_paths(item.base_path, len(chunks))

            old_ids = overwrite_ids_map.get(item.base_path)
            if old_ids:
                _delete_content_by_ids(conn, old_ids)
                files_overwritten += 1

            for chunk_path, chunk in zip(paths, chunks):
                conn.execute(
                    'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, ?, ?, ?)',
                    (chunk_path, language_id, chunk, item.content_type_id),
                )
                rows_inserted += 1
            files_imported += 1
            imported_by_mime[item.mime] = imported_by_mime.get(item.mime, 0) + 1

            if is_overwrite:
                overwrites_done += 1
                _report("overwrite", overwrites_done, overwrite_total)
            else:
                adds_done += 1
                _report("add", adds_done, add_total)

        conn.commit()
    conn.close()

    update_last_change(db_path, documentation_set, user_name)

    if orphans_deleted or files_overwritten or _page_size_migration_pending(db_path):
        _report("vacuum", 0, 1)
        vacuum_database(db_path)
        _report("vacuum", 1, 1)

    return ImportSummary(
        files_imported=files_imported,
        files_overwritten=files_overwritten,
        orphans_deleted=orphans_deleted,
        rows_inserted=rows_inserted,
        files_skipped_error=files_error,
        errors=errors,
        imported_by_mime=imported_by_mime,
    )


def _analytics_enabled() -> bool:
    if os.environ.get("DOCDB_NO_ANALYTICS"):
        return False
    if posthog is None:
        return False
    return bool(POSTHOG_PROJECT_API_KEY) and not POSTHOG_PROJECT_API_KEY.startswith("phc_REPLACE")


def _analytics_init() -> None:
    try:
        posthog.api_key = POSTHOG_PROJECT_API_KEY
        posthog.host = POSTHOG_HOST
    except Exception:
        pass


def _capture(distinct_id: str, event: str, props: dict | None = None) -> None:
    if not _analytics_enabled():
        return
    try:
        posthog.capture(distinct_id=distinct_id, event=event, properties=props or {})
    except Exception:
        pass


def main(
    page: ft.Page,
    db_path: Path,
    total_count: int,
    error: str | None,
    user_name: str,
) -> None:
    page.title = "Browse tooltips"

    if _analytics_enabled():
        _analytics_init()
        session_start = time.monotonic()
        closed_sent = [False]
        db_size = db_path.stat().st_size if db_path.exists() else 0
        _capture(user_name, "app_started", {
            "app_version": APP_VERSION,
            "python_version": sys.version.split()[0],
            "platform": _platform.platform(),
            "total_count": total_count,
            "db_size_bytes": db_size,
        })
        _capture(user_name, "database_opened", {
            "total_count": total_count,
            "db_size_bytes": db_size,
        })

        def _send_app_closed(_e: object = None) -> None:
            if closed_sent[0]:
                return
            closed_sent[0] = True
            duration = int(time.monotonic() - session_start)
            _capture(user_name, "app_closed", {"duration_seconds": duration})
            try:
                if posthog is not None:
                    posthog.shutdown()
            except Exception:
                pass

        # macOS red-dot / native window close: Flet's `on_disconnect` is for web
        # sessions and doesn't fire for desktop. We must intercept the OS close
        # signal via Window.prevent_close, flush PostHog, then call destroy()
        # ourselves. on_disconnect/on_close/atexit are kept as fallbacks.
        async def _on_window_event(e: "ft.WindowEvent") -> None:
            if e.type == ft.WindowEventType.CLOSE:
                try:
                    _send_app_closed()
                except Exception:
                    pass
                try:
                    await page.window.destroy()
                except Exception:
                    pass

        page.on_disconnect = _send_app_closed
        page.on_close = _send_app_closed
        try:
            page.window.prevent_close = True
            page.window.on_event = _on_window_event
        except Exception:
            pass
        atexit.register(_send_app_closed)

    current_page = 1
    total_pages = max(1, (total_count + UI_PAGE_SIZE - 1) // UI_PAGE_SIZE)

    # Built-in HTTP server for browsing Content rows in a real browser.
    # Bind failure (e.g. another instance already running) is non-fatal —
    # the "Open in browser" button just won't be useful.
    content_server: dict[str, http.server.ThreadingHTTPServer | int | None] = {
        "server": None,
        "port": None,
    }
    started = start_content_web_server(db_path)
    if started is not None:
        content_server["server"] = started
        content_server["port"] = started.server_address[1]
    else:
        # Either bind failed or another docdb-studio is already serving on
        # CONTENT_SERVER_PORT — assume the latter so "Open in browser" links
        # still work for the user.
        content_server["port"] = CONTENT_SERVER_PORT

    # Tracks the currently-focused TextField so Ctrl+Shift+V can target it.
    focused_textfield: list[ft.TextField | None] = [None]

    def track_focus(tf: ft.TextField) -> ft.TextField:
        """Wrap a TextField's on_focus/on_blur to update the global focus tracker.
        Call AFTER any user-set on_focus/on_blur is assigned."""
        user_focus = tf.on_focus
        user_blur = tf.on_blur

        def _focus(e: ft.ControlEvent) -> None:
            focused_textfield[0] = tf
            if user_focus is not None:
                user_focus(e)

        def _blur(e: ft.ControlEvent) -> None:
            if focused_textfield[0] is tf:
                focused_textfield[0] = None
            if user_blur is not None:
                user_blur(e)

        tf.on_focus = _focus
        tf.on_blur = _blur
        return tf

    def paste_plain_text() -> None:
        """Ctrl+Shift+V: insert clipboard text into the focused TextField with
        all Unicode "Other" category characters stripped (controls, format,
        surrogate, private-use). Falls back to append if selection is unknown."""
        tf = focused_textfield[0]
        if tf is None:
            return
        try:
            raw = page.clipboard.get() or ""
        except Exception:
            return
        cleaned = sanitize_clipboard_paste(raw)
        if not cleaned:
            return
        current = tf.value or ""
        start = end = len(current)
        sel = getattr(tf, "selection", None)
        if sel is not None:
            try:
                if sel.is_valid:
                    start = min(sel.start, sel.end)
                    end = max(sel.start, sel.end)
            except Exception:
                pass
        tf.value = current[:start] + cleaned + current[end:]
        page.update()

    def on_keyboard_event(e: ft.KeyboardEvent) -> None:
        if e.key == "V" and e.shift and (e.ctrl or e.meta):
            paste_plain_text()

    page.on_keyboard_event = on_keyboard_event

    def show_compacting_dialog() -> None:
        """Modal indeterminate-progress dialog shown while VACUUM runs.

        Every UI delete path triggers VACUUM (DB hygiene policy — no opt-out),
        and on the ~380 MB DB that takes several seconds. The dialog reassures
        the user the app hasn't hung. Non-dismissible: VACUUM is uninterruptible
        and per policy the user can't skip it."""
        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("Compacting database…"),
                content=ft.Container(
                    content=ft.Column(
                        [
                            ft.ProgressBar(value=None, width=420),
                            ft.Text("This may take a few seconds."),
                        ],
                        tight=True,
                        spacing=12,
                    ),
                    width=460,
                ),
            )
        )
        page.update()

    def run_with_compacting_dialog(
        work: "callable", on_done: "callable"
    ) -> None:
        """Pop the Compacting dialog, run `work()` on a worker thread, then
        close the dialog and call `on_done(result_or_exception)` on the event
        loop. `work` returns whatever the caller needs; if it raises, the
        exception is passed to `on_done` instead so the caller can branch on
        type.

        Mirrors the run_import/_finish_with_result/_finish_with_error pattern
        used by the content-import flow."""
        show_compacting_dialog()

        async def _finish(result: object) -> None:
            page.pop_dialog()
            page.update()
            on_done(result)

        def _worker() -> None:
            try:
                result: object = work()
            except Exception as exc:  # noqa: BLE001
                result = exc
            page.run_task(_finish, result)

        page.run_thread(_worker)

    def show_broken_uris_warning(
        broken: list[tuple[int, str]], on_save_anyway: "callable"
    ) -> None:
        """Show a Fix / Save anyway dialog. on_save_anyway is invoked if the user proceeds."""
        items = [f"  Button {bid}: {uri}" for bid, uri in broken[:10]]
        if len(broken) > 10:
            items.append(f"  … (+{len(broken) - 10} more)")
        body = (
            f"These URIs don't match any Content.path:\n\n"
            + "\n".join(items)
            + "\n\nFix them now or save anyway."
        )

        def on_fix(_: ft.ControlEvent) -> None:
            _capture(user_name, "broken_uri_dialog_decision", {"decision": "fix", "broken_count": len(broken)})
            page.pop_dialog()
            page.update()

        def on_save_anyway_click(_: ft.ControlEvent) -> None:
            _capture(user_name, "broken_uri_dialog_decision", {"decision": "save_anyway", "broken_count": len(broken)})
            page.pop_dialog()
            page.update()
            on_save_anyway()

        title_count = f"{len(broken)} broken URI" + ("s" if len(broken) != 1 else "")
        page.show_dialog(
            ft.AlertDialog(
                title=ft.Text(title_count),
                content=ft.Text(body, selectable=True),
                actions=[
                    ft.Button("Fix", on_click=on_fix),
                    ft.TextButton("Save anyway", on_click=on_save_anyway_click),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )
        page.update()

    PICKER_PAGE_SIZE = 50

    def show_content_picker(on_pick: "callable") -> None:
        """Open a modal picker for a Content.path. After a path is clicked, if it's an HTML
        page that contains anchors, a second-step picker offers them so the caller receives
        either `path` or `path#anchor`."""
        state = {"page": 1, "search": None}

        search_field = track_focus(
            ft.TextField(
                label="Search path",
                hint_text="prefix; * is a wildcard",
                width=400,
            )
        )
        list_column = ft.Column(
            scroll=ft.ScrollMode.AUTO, expand=True, spacing=2
        )
        pager_text = ft.Text("", selectable=True)
        prev_btn = ft.Button("Previous")
        next_btn = ft.Button("Next")

        def deliver(value: str) -> None:
            page.pop_dialog()
            page.update()
            on_pick(value)

        def show_anchor_subpicker(path: str, anchors: list[str]) -> None:
            """Replace the path picker with a list of anchors for `path`."""
            page.pop_dialog()
            anchor_column = ft.Column(
                scroll=ft.ScrollMode.AUTO, expand=True, spacing=2
            )
            anchor_column.controls.append(
                ft.TextButton(
                    "(no anchor — use path only)",
                    on_click=lambda _: deliver(path),
                )
            )
            for anchor in anchors:
                anchor_column.controls.append(
                    ft.TextButton(
                        f"#{anchor}",
                        on_click=lambda e, a=anchor: deliver(f"{path}#{a}"),
                    )
                )

            def go_back(_: ft.ControlEvent) -> None:
                page.pop_dialog()
                page.update()
                show_content_picker(on_pick)

            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text(f"Anchors in {path}"),
                    content=ft.Container(
                        content=anchor_column,
                        width=700,
                        height=520,
                    ),
                    actions=[
                        ft.TextButton("Back", on_click=go_back),
                        ft.TextButton(
                            "Cancel",
                            on_click=lambda _: (page.pop_dialog(), page.update()),
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
            )
            page.update()

        def on_path_clicked(path: str) -> None:
            anchors = get_html_anchors_for_path(db_path, path)
            if not anchors:
                deliver(path)
                return
            show_anchor_subpicker(path, anchors)

        def fill_list() -> None:
            offset = (state["page"] - 1) * PICKER_PAGE_SIZE
            paths = get_content_paths_page(
                db_path, PICKER_PAGE_SIZE, offset, state["search"]
            )
            list_column.controls.clear()
            for path in paths:
                list_column.controls.append(
                    ft.TextButton(
                        path,
                        on_click=lambda e, p=path: on_path_clicked(p),
                    )
                )
            total = get_content_paths_count(db_path, state["search"])
            total_pages = max(1, (total + PICKER_PAGE_SIZE - 1) // PICKER_PAGE_SIZE)
            state["page"] = max(1, min(state["page"], total_pages))
            pager_text.value = (
                f"Page {state['page']} of {total_pages} — {total} paths"
            )
            prev_btn.disabled = state["page"] <= 1
            next_btn.disabled = state["page"] >= total_pages
            page.update()

        def do_search(_: ft.ControlEvent | None = None) -> None:
            term = (search_field.value or "").strip()
            state["search"] = term or None
            state["page"] = 1
            fill_list()

        def go_prev(_: ft.ControlEvent) -> None:
            if state["page"] > 1:
                state["page"] -= 1
                fill_list()

        def go_next(_: ft.ControlEvent) -> None:
            state["page"] += 1
            fill_list()

        search_field.on_submit = do_search
        prev_btn.on_click = go_prev
        next_btn.on_click = go_next

        page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("Pick a Content path"),
                content=ft.Container(
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    search_field,
                                    ft.Button("Search", on_click=do_search),
                                ],
                                spacing=8,
                            ),
                            ft.Container(content=list_column, expand=True),
                            ft.Row(
                                [prev_btn, pager_text, next_btn],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            ),
                        ],
                        expand=True,
                        spacing=8,
                    ),
                    width=700,
                    height=520,
                ),
                actions=[
                    ft.TextButton(
                        "Cancel",
                        on_click=lambda _: (page.pop_dialog(), page.update()),
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )
        fill_list()

    def show_view_page(
        tooltip_id: int,
        from_page: int,
        from_search_term: str | None,
        on_back: "callable | None" = None,
    ) -> None:
        tooltip = get_tooltip_by_id(db_path, tooltip_id)
        if tooltip is None:
            page.controls.clear()
            page.add(ft.Text(f"Tooltip {tooltip_id} not found.", color=ft.Colors.RED, selectable=True))
            page.update()
            return
        category, tag, summary, detail = tooltip
        _capture(user_name, "tooltip_viewed", {"tooltip_id": tooltip_id})
        page.controls.clear()
        label_style = ft.TextStyle(
            weight=ft.FontWeight.BOLD,
            size=12,
            decoration=ft.TextDecoration.UNDERLINE,
        )

        def go_back_view() -> None:
            if on_back is not None:
                on_back()
            else:
                show_browse_page(
                    get_total_count(db_path, from_search_term),
                    restore_page=from_page,
                    restore_search_term=from_search_term,
                )

        back_btn = ft.Button(
            "Back",
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE,
                color=ft.Colors.BLACK,
            ),
            on_click=lambda _: go_back_view(),
        )
        section_indent = 16
        content_controls: list[ft.Control] = [
            back_btn,
            ft.Text("ID", style=label_style),
            ft.Container(
                content=ft.Text(str(tooltip_id), selectable=True),
                padding=ft.Padding.only(left=section_indent),
            ),
            ft.Text("Category", style=label_style),
            ft.Container(
                content=ft.Text(str(category or ""), selectable=True),
                padding=ft.Padding.only(left=section_indent),
            ),
            ft.Text("Tag", style=label_style),
            ft.Container(
                content=ft.Text(str(tag or ""), selectable=True),
                padding=ft.Padding.only(left=section_indent),
            ),
            ft.Text("Summary", style=label_style),
            ft.Container(
                content=ft.Text(str(summary or ""), selectable=True, no_wrap=False),
                padding=ft.Padding.only(left=section_indent),
            ),
            ft.Text("Detail", style=label_style),
            ft.Container(
                content=ft.Text(str(detail or ""), selectable=True, no_wrap=False),
                padding=ft.Padding.only(left=section_indent),
            ),
        ]
        buttons_rows = get_tooltip_buttons(db_path, tooltip_id)
        if buttons_rows:
            content_controls.append(ft.Text("Buttons", style=label_style))
            uris = [uri or "" for _, _, uri in buttons_rows]
            valid_uris = uris_exist_in_content(db_path, uris)
            server_port = content_server.get("port") or CONTENT_SERVER_PORT

            def _build_browser_url(uri: str) -> str:
                """URL-encode a button URI for the local content server, keeping
                `?query` and `#fragment` intact."""
                rest = uri
                fragment = ""
                if "#" in rest:
                    rest, fragment = rest.split("#", 1)
                query = ""
                if "?" in rest:
                    rest, query = rest.split("?", 1)
                url = f"http://127.0.0.1:{server_port}/{urllib.parse.quote(rest)}"
                if query:
                    url += "?" + query
                if fragment:
                    url += "#" + urllib.parse.quote(fragment)
                return url

            btn_columns = [
                fdt.DataColumn2(label=ft.Text("Order"), fixed_width=48),
                fdt.DataColumn2(label=ft.Text("Description")),
                fdt.DataColumn2(label=ft.Text("URI")),
                fdt.DataColumn2(label=ft.Text("Ref"), fixed_width=48),
                fdt.DataColumn2(label=ft.Text("Open"), fixed_width=56),
            ]

            def _build_btn_row(num: int | None, desc: str | None, uri: str | None) -> ft.DataRow:
                uri_str = uri or ""
                ref_icon = (
                    ft.Icon(
                        ft.Icons.CHECK,
                        color=ft.Colors.GREEN,
                        tooltip="Path exists",
                    )
                    if _uri_path_for_content_lookup(uri_str) in valid_uris
                    else ft.Icon(
                        ft.Icons.CLOSE,
                        color=ft.Colors.RED,
                        tooltip="No matching path in Content",
                    )
                )
                open_btn = ft.IconButton(
                    icon=ft.Icons.OPEN_IN_NEW,
                    tooltip="Open in browser",
                    on_click=lambda e, u=uri_str: webbrowser.open(
                        _build_browser_url(u)
                    ),
                )
                return ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(str(num or ""), selectable=True)),
                        ft.DataCell(ft.Text(str(desc or ""), selectable=True)),
                        ft.DataCell(ft.Text(uri_str, selectable=True)),
                        ft.DataCell(ref_icon),
                        ft.DataCell(open_btn),
                    ]
                )

            btn_table = fdt.DataTable2(
                columns=btn_columns,
                rows=[_build_btn_row(num, desc, uri) for num, desc, uri in buttons_rows],
                vertical_lines=ft.BorderSide(1, ft.Colors.OUTLINE),
                horizontal_lines=ft.BorderSide(1, ft.Colors.OUTLINE),
                heading_row_color=ft.Colors.PURPLE,
                heading_text_style=ft.TextStyle(
                    color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD
                ),
                expand=True,
                column_spacing=8,
                data_row_height=28,
                heading_row_height=36,
            )
            content_controls.append(ft.Container(content=btn_table, expand=True))
        content = ft.Column(
            content_controls,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=8,
        )
        page.add(content)
        page.update()

    def show_edit_page(
        tooltip_id: int,
        from_page: int,
        from_search_term: str | None,
        on_back: "callable | None" = None,
    ) -> None:
        row = get_tooltip_for_edit(db_path, tooltip_id)
        if row is None:
            page.controls.clear()
            page.add(ft.Text(f"Tooltip {tooltip_id} not found.", color=ft.Colors.RED, selectable=True))
            page.update()
            return
        category_id, category, tag, summary, detail = row
        _capture(user_name, "tooltip_edit_started", {"tooltip_id": tooltip_id})
        categories = get_categories(db_path)
        valid_paths = get_all_content_paths(db_path)
        page.controls.clear()

        def go_back() -> None:
            if on_back is not None:
                on_back()
            else:
                show_browse_page(
                    get_total_count(db_path, from_search_term),
                    restore_page=from_page,
                    restore_search_term=from_search_term,
                )

        cancel_btn = ft.Button(
            "Cancel",
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE,
                color=ft.Colors.BLACK,
            ),
            on_click=lambda _: go_back(),
        )
        section_indent = 16
        initial_category_id = str(category_id)
        initial_tag = (tag or "").strip()
        initial_summary = summary or ""
        initial_detail = detail or ""
        buttons_state: list[tuple[int, str, str]] = [
            (num or 0, str(desc or ""), str(uri or ""))
            for num, desc, uri in get_tooltip_buttons(db_path, tooltip_id)
        ]
        initial_buttons_state: list[tuple[int, str, str]] = [
            (b[0], b[1], b[2]) for b in buttons_state
        ]

        # Slightly different from page background in both light and dark theme
        input_fill = ft.Colors.SURFACE_CONTAINER_LOW
        id_display = ft.Container(
            content=ft.Column(
                [
                    ft.Text("ID", style=ft.TextStyle(weight=ft.FontWeight.BOLD, size=12)),
                    ft.Text(str(tooltip_id), selectable=True),
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.START,
            ),
            padding=ft.Padding.only(left=section_indent),
        )
        category_dropdown = ft.Dropdown(
            label="Category",
            value=initial_category_id,
            options=[ft.dropdown.Option(str(cid), cname) for cid, cname in categories],
            width=220,
            on_select=lambda _: update_save_state(),
            fill_color=input_fill,
        )
        tag_field = track_focus(
            ft.TextField(
                label="Tag",
                value=tag or "",
                width=400,
                on_change=lambda _: update_save_state(),
                fill_color=input_fill,
            )
        )
        summary_field = track_focus(
            ft.TextField(
                label="Summary",
                value=summary or "",
                multiline=True,
                min_lines=3,
                width=400,
                on_change=lambda _: update_save_state(),
                fill_color=input_fill,
            )
        )
        detail_field = track_focus(
            ft.TextField(
                label="Detail",
                value=detail or "",
                multiline=True,
                min_lines=3,
                width=400,
                on_change=lambda _: update_save_state(),
                fill_color=input_fill,
            )
        )

        def _buttons_equal(
            a: list[tuple[int, str, str]], b: list[tuple[int, str, str]]
        ) -> bool:
            if len(a) != len(b):
                return False
            sa = sorted(a, key=lambda x: x[0])
            sb = sorted(b, key=lambda x: x[0])
            return all(x == y for x, y in zip(sa, sb))

        changed_fields_text = ft.Text(
            "",
            style=ft.TextStyle(
                color=ft.Colors.ORANGE, weight=ft.FontWeight.BOLD
            ),
            selectable=True,
        )

        def update_save_state() -> None:
            changed: list[str] = []
            if (category_dropdown.value or "") != initial_category_id:
                changed.append("Category")
            if (tag_field.value or "").strip() != initial_tag:
                changed.append("Tag")
            if (summary_field.value or "") != initial_summary:
                changed.append("Summary")
            if (detail_field.value or "") != initial_detail:
                changed.append("Detail")
            if not _buttons_equal(buttons_state, initial_buttons_state):
                changed.append("Buttons")
            has_changes = bool(changed)
            save_btn.visible = has_changes
            save_btn.disabled = not has_changes
            changed_fields_text.value = (
                "Modified: " + ", ".join(changed) if changed else ""
            )
            page.update()

        def on_save(_: ft.ControlEvent) -> None:
            cid_val = category_dropdown.value
            tag_val = (tag_field.value or "").strip()
            if not cid_val:
                _capture(user_name, "validation_error", {"error_type": "category_missing", "where": "tooltip_edit"})
                page.show_dialog(
                    ft.SnackBar(content=ft.Text("Please select a category."))
                )
                return
            if not tag_val:
                _capture(user_name, "validation_error", {"error_type": "tag_required", "where": "tooltip_edit"})
                page.show_dialog(ft.SnackBar(content=ft.Text("Tag is required.")))
                return

            def commit_save() -> None:
                def work() -> None:
                    update_tooltip(
                        db_path,
                        tooltip_id,
                        int(cid_val),
                        tag_val,
                        summary_field.value or "",
                        detail_field.value or "",
                    )
                    replace_tooltip_buttons(db_path, tooltip_id, buttons_state)
                    category_name = get_category_name(db_path, int(cid_val))
                    if category_name is not None:
                        update_last_change(
                            db_path, "tooltips-" + category_name, user_name
                        )

                def on_done(result: object) -> None:
                    if isinstance(result, sqlite3.IntegrityError):
                        _capture(user_name, "validation_error", {"error_type": "tag_duplicate", "where": "tooltip_edit"})
                        page.show_dialog(
                            ft.SnackBar(
                                content=ft.Text("Tag already exists in this category.")
                            )
                        )
                        return
                    if isinstance(result, ValueError):
                        _capture(user_name, "validation_error", {"error_type": "button_uri_invalid", "where": "tooltip_edit"})
                        page.show_dialog(ft.SnackBar(content=ft.Text(str(result))))
                        return
                    if isinstance(result, BaseException):
                        raise result
                    _capture(user_name, "tooltip_saved", {
                        "mode": "update",
                        "category_id": int(cid_val),
                        "button_count": len(buttons_state),
                    })
                    go_back()

                run_with_compacting_dialog(work, on_done)

            broken = find_broken_button_uris(db_path, buttons_state)
            if broken:
                show_broken_uris_warning(broken, commit_save)
                return
            commit_save()

        save_btn = ft.Button(
            "Save",
            on_click=on_save,
            disabled=True,
            visible=False,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.GREEN_700,
                color=ft.Colors.WHITE,
            ),
        )

        buttons_section = ft.Column([])

        def refresh_buttons_section() -> None:
            all_ids = get_button_number_ids(db_path)
            used_ids = {b[0] for b in buttons_state}
            available_ids = [i for i in all_ids if i not in used_ids]
            controls: list[ft.Control] = []
            header_style = ft.TextStyle(
                weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK
            )
            controls.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Container(
                                content=ft.Text("Order", style=header_style),
                                width=52,
                            ),
                            ft.Container(
                                content=ft.Text("Description", style=header_style),
                                expand=True,
                            ),
                            ft.Container(
                                content=ft.Text("URI", style=header_style),
                                expand=True,
                            ),
                            ft.Container(width=36),  # URI valid-status icon
                            ft.Container(width=70),  # Edit button width
                            ft.Container(width=70),  # Delete button width
                        ],
                        spacing=8,
                    ),
                    bgcolor=ft.Colors.PINK,
                )
            )
            for num, desc, uri in buttons_state:
                row_desc = ft.Text(str(desc or ""), no_wrap=False, selectable=True)
                row_uri = ft.Text(str(uri or ""), no_wrap=False, selectable=True)
                row_status_icon = ft.Icon(ft.Icons.HELP_OUTLINE)
                update_uri_status_icon(
                    row_status_icon, uri or "", valid_paths, db_path=db_path
                )
                edit_btn = ft.Button(
                    "Edit",
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.YELLOW, color=ft.Colors.BLACK
                    ),
                    on_click=lambda e, n=num, d=desc or "", u=uri or "": open_edit_button_dialog(
                        n, d, u
                    ),
                )
                del_btn = ft.Button(
                    "Delete",
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.RED, color=ft.Colors.WHITE
                    ),
                    on_click=lambda e, n=num: on_delete_button(n),
                )
                controls.append(
                    ft.Row(
                        [
                            ft.Text(str(num), width=52, selectable=True),
                            ft.Container(content=row_desc, expand=True),
                            ft.Container(content=row_uri, expand=True),
                            ft.Container(content=row_status_icon, width=36),
                            edit_btn,
                            del_btn,
                        ],
                        spacing=8,
                    )
                )
            add_order_dropdown = ft.Dropdown(
                label="Order",
                width=130,
                options=[ft.dropdown.Option(str(i), str(i)) for i in available_ids],
                fill_color=input_fill,
            )
            add_desc_field = track_focus(
                ft.TextField(
                    label="Description", width=200, fill_color=input_fill
                )
            )
            add_uri_field = ft.TextField(label="URI", width=200, fill_color=input_fill)
            add_uri_icon = ft.Icon(ft.Icons.HELP_OUTLINE)
            update_uri_status_icon(add_uri_icon, "", valid_paths)

            def on_add_uri_change(_: ft.ControlEvent) -> None:
                update_uri_status_icon(
                    add_uri_icon, add_uri_field.value or "", valid_paths
                )
                page.update()

            def on_add_uri_blur(_: ft.ControlEvent) -> None:
                update_uri_status_icon(
                    add_uri_icon,
                    add_uri_field.value or "",
                    valid_paths,
                    db_path=db_path,
                )
                page.update()

            add_uri_field.on_change = on_add_uri_change
            add_uri_field.on_blur = on_add_uri_blur
            track_focus(add_uri_field)

            def on_add_browse_pick(value: str) -> None:
                add_uri_field.value = value
                update_uri_status_icon(
                    add_uri_icon, value, valid_paths, db_path=db_path
                )
                page.update()

            add_browse_btn = ft.Button(
                "Browse…",
                on_click=lambda _: show_content_picker(on_add_browse_pick),
            )

            def on_add_button(_: ft.ControlEvent) -> None:
                o = add_order_dropdown.value
                if not o:
                    _capture(user_name, "validation_error", {"error_type": "button_order_missing", "where": "button_add"})
                    page.show_dialog(
                        ft.SnackBar(content=ft.Text("Select an order (number)."))
                    )
                    return
                desc = (add_desc_field.value or "").strip()
                uri = (add_uri_field.value or "").strip()
                if not desc:
                    _capture(user_name, "validation_error", {"error_type": "button_description_missing", "where": "button_add"})
                    page.show_dialog(
                        ft.SnackBar(content=ft.Text("Description is required."))
                    )
                    return
                if not uri:
                    _capture(user_name, "validation_error", {"error_type": "button_uri_missing", "where": "button_add"})
                    page.show_dialog(
                        ft.SnackBar(content=ft.Text("URI is required."))
                    )
                    return
                buttons_state.append((int(o), desc, uri))
                _capture(user_name, "tooltip_button_changed", {"action": "add", "context": "edit"})
                add_desc_field.value = ""
                add_uri_field.value = ""
                add_order_dropdown.value = None
                update_uri_status_icon(add_uri_icon, "", valid_paths)
                refresh_buttons_section()
                update_save_state()

            controls.append(ft.Divider())
            controls.append(
                ft.Row(
                    [
                        add_order_dropdown,
                        add_desc_field,
                        add_uri_field,
                        add_uri_icon,
                        add_browse_btn,
                        ft.Button("Add button", on_click=on_add_button),
                    ],
                    spacing=8,
                )
            )
            buttons_section.controls = controls
            page.update()

        def open_edit_button_dialog(
            button_number_id: int, current_desc: str, current_uri: str
        ) -> None:
            edit_desc = track_focus(
                ft.TextField(
                    label="Description",
                    value=current_desc,
                    width=300,
                    fill_color=input_fill,
                )
            )
            edit_uri = ft.TextField(
                label="URI", value=current_uri, width=300, fill_color=input_fill
            )
            edit_uri_icon = ft.Icon(ft.Icons.HELP_OUTLINE)
            update_uri_status_icon(edit_uri_icon, current_uri, valid_paths)

            def on_edit_uri_change(_: ft.ControlEvent) -> None:
                update_uri_status_icon(
                    edit_uri_icon, edit_uri.value or "", valid_paths
                )
                page.update()

            def on_edit_uri_blur(_: ft.ControlEvent) -> None:
                update_uri_status_icon(
                    edit_uri_icon,
                    edit_uri.value or "",
                    valid_paths,
                    db_path=db_path,
                )
                page.update()

            edit_uri.on_change = on_edit_uri_change
            edit_uri.on_blur = on_edit_uri_blur
            track_focus(edit_uri)

            def on_edit_browse_pick(value: str) -> None:
                edit_uri.value = value
                update_uri_status_icon(
                    edit_uri_icon, value, valid_paths, db_path=db_path
                )
                page.update()

            edit_browse_btn = ft.Button(
                "Browse…",
                on_click=lambda _: show_content_picker(on_edit_browse_pick),
            )

            def save_edit(_: ft.ControlEvent) -> None:
                for i, (bid, _, _) in enumerate(buttons_state):
                    if bid == button_number_id:
                        buttons_state[i] = (
                            button_number_id,
                            edit_desc.value or "",
                            edit_uri.value or "",
                        )
                        break
                _capture(user_name, "tooltip_button_changed", {"action": "edit", "context": "edit"})
                page.pop_dialog()
                refresh_buttons_section()
                update_save_state()

            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text(f"Edit button (order {button_number_id})"),
                    content=ft.Column(
                        [
                            edit_desc,
                            ft.Row(
                                [edit_uri, edit_uri_icon, edit_browse_btn],
                                spacing=8,
                            ),
                        ],
                        tight=True,
                        spacing=8,
                    ),
                    actions=[
                        ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                        ft.Button("Save", on_click=save_edit),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
            )
            page.update()

        def on_delete_button(button_number_id: int) -> None:
            buttons_state[:] = [
                b for b in buttons_state if b[0] != button_number_id
            ]
            _capture(user_name, "tooltip_button_changed", {"action": "remove", "context": "edit"})
            refresh_buttons_section()
            update_save_state()

        refresh_buttons_section()

        content = ft.Column(
            [
                ft.Row(
                    [cancel_btn, save_btn],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                changed_fields_text,
                id_display,
                ft.Container(
                    content=category_dropdown,
                    padding=ft.Padding.only(left=section_indent),
                ),
                ft.Container(
                    content=tag_field,
                    padding=ft.Padding.only(left=section_indent),
                ),
                ft.Container(
                    content=summary_field,
                    padding=ft.Padding.only(left=section_indent),
                ),
                ft.Container(
                    content=detail_field,
                    padding=ft.Padding.only(left=section_indent),
                ),
                ft.Container(
                    content=buttons_section,
                    padding=ft.Padding.only(left=section_indent),
                ),
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=8,
        )
        page.add(content)
        page.update()

    def show_add_page(
        from_page: int, from_search_term: str | None
    ) -> None:
        categories = get_categories(db_path)
        valid_paths = get_all_content_paths(db_path)
        page.controls.clear()
        cancel_btn = ft.Button(
            "Cancel",
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE,
                color=ft.Colors.BLACK,
            ),
            on_click=lambda _: show_browse_page(
                get_total_count(db_path, from_search_term),
                restore_page=from_page,
                restore_search_term=from_search_term,
            ),
        )
        section_indent = 16
        input_fill = ft.Colors.SURFACE_CONTAINER_LOW
        category_dropdown = ft.Dropdown(
            label="Category",
            value=None,
            options=[ft.dropdown.Option(str(cid), cname) for cid, cname in categories],
            width=220,
            on_select=lambda _: update_add_save_state(),
            fill_color=input_fill,
        )
        tag_field = track_focus(
            ft.TextField(
                label="Tag",
                value="",
                width=400,
                on_change=lambda _: update_add_save_state(),
                fill_color=input_fill,
            )
        )
        summary_field = track_focus(
            ft.TextField(
                label="Summary",
                value="",
                multiline=True,
                min_lines=3,
                width=400,
                on_change=lambda _: update_add_save_state(),
                fill_color=input_fill,
            )
        )
        detail_field = track_focus(
            ft.TextField(
                label="Detail (optional)",
                value="",
                multiline=True,
                min_lines=3,
                width=400,
                fill_color=input_fill,
            )
        )
        buttons_state: list[tuple[int, str, str]] = []

        save_btn = ft.Button(
            "Save",
            on_click=lambda _: on_save_add(),
            disabled=True,
            visible=False,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.GREEN_700,
                color=ft.Colors.WHITE,
            ),
        )

        def update_add_save_state() -> None:
            cid = category_dropdown.value
            tag = (tag_field.value or "").strip()
            summary = (summary_field.value or "").strip()
            can_save = bool(cid and tag and summary)
            save_btn.visible = can_save
            save_btn.disabled = not can_save
            page.update()

        buttons_section = ft.Column([])

        def refresh_buttons_section() -> None:
            all_ids = get_button_number_ids(db_path)
            used_ids = {b[0] for b in buttons_state}
            available_ids = [i for i in all_ids if i not in used_ids]
            controls: list[ft.Control] = []
            header_style = ft.TextStyle(
                weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK
            )
            controls.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Container(
                                content=ft.Text("Order", style=header_style),
                                width=52,
                            ),
                            ft.Container(
                                content=ft.Text("Description", style=header_style),
                                expand=True,
                            ),
                            ft.Container(
                                content=ft.Text("URI", style=header_style),
                                expand=True,
                            ),
                            ft.Container(width=36),
                            ft.Container(width=70),
                            ft.Container(width=70),
                        ],
                        spacing=8,
                    ),
                    bgcolor=ft.Colors.PINK,
                )
            )
            for num, desc, uri in buttons_state:
                row_desc = ft.Text(str(desc or ""), no_wrap=False, selectable=True)
                row_uri = ft.Text(str(uri or ""), no_wrap=False, selectable=True)
                row_status_icon = ft.Icon(ft.Icons.HELP_OUTLINE)
                update_uri_status_icon(
                    row_status_icon, uri or "", valid_paths, db_path=db_path
                )
                edit_btn = ft.Button(
                    "Edit",
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.YELLOW, color=ft.Colors.BLACK
                    ),
                    on_click=lambda e, n=num, d=desc or "", u=uri or "": open_edit_button_dialog(
                        n, d, u
                    ),
                )
                del_btn = ft.Button(
                    "Delete",
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.RED, color=ft.Colors.WHITE
                    ),
                    on_click=lambda e, n=num: on_delete_button(n),
                )
                controls.append(
                    ft.Row(
                        [
                            ft.Text(str(num), width=52, selectable=True),
                            ft.Container(content=row_desc, expand=True),
                            ft.Container(content=row_uri, expand=True),
                            ft.Container(content=row_status_icon, width=36),
                            edit_btn,
                            del_btn,
                        ],
                        spacing=8,
                    )
                )
            add_order_dropdown = ft.Dropdown(
                label="Order",
                width=130,
                options=[ft.dropdown.Option(str(i), str(i)) for i in available_ids],
                fill_color=input_fill,
            )
            add_desc_field = track_focus(
                ft.TextField(
                    label="Description", width=200, fill_color=input_fill
                )
            )
            add_uri_field = ft.TextField(label="URI", width=200, fill_color=input_fill)
            add_uri_icon = ft.Icon(ft.Icons.HELP_OUTLINE)
            update_uri_status_icon(add_uri_icon, "", valid_paths)

            def on_add_uri_change(_: ft.ControlEvent) -> None:
                update_uri_status_icon(
                    add_uri_icon, add_uri_field.value or "", valid_paths
                )
                page.update()

            def on_add_uri_blur(_: ft.ControlEvent) -> None:
                update_uri_status_icon(
                    add_uri_icon,
                    add_uri_field.value or "",
                    valid_paths,
                    db_path=db_path,
                )
                page.update()

            add_uri_field.on_change = on_add_uri_change
            add_uri_field.on_blur = on_add_uri_blur
            track_focus(add_uri_field)

            def on_add_browse_pick(value: str) -> None:
                add_uri_field.value = value
                update_uri_status_icon(
                    add_uri_icon, value, valid_paths, db_path=db_path
                )
                page.update()

            add_browse_btn = ft.Button(
                "Browse…",
                on_click=lambda _: show_content_picker(on_add_browse_pick),
            )

            def on_add_button(_: ft.ControlEvent) -> None:
                o = add_order_dropdown.value
                if not o:
                    _capture(user_name, "validation_error", {"error_type": "button_order_missing", "where": "button_add"})
                    page.show_dialog(
                        ft.SnackBar(content=ft.Text("Select an order (number)."))
                    )
                    return
                desc = (add_desc_field.value or "").strip()
                uri = (add_uri_field.value or "").strip()
                if not desc:
                    _capture(user_name, "validation_error", {"error_type": "button_description_missing", "where": "button_add"})
                    page.show_dialog(
                        ft.SnackBar(content=ft.Text("Description is required."))
                    )
                    return
                if not uri:
                    _capture(user_name, "validation_error", {"error_type": "button_uri_missing", "where": "button_add"})
                    page.show_dialog(
                        ft.SnackBar(content=ft.Text("URI is required."))
                    )
                    return
                buttons_state.append((int(o), desc, uri))
                _capture(user_name, "tooltip_button_changed", {"action": "add", "context": "insert"})
                add_desc_field.value = ""
                add_uri_field.value = ""
                add_order_dropdown.value = None
                update_uri_status_icon(add_uri_icon, "", valid_paths)
                refresh_buttons_section()
                page.update()

            controls.append(ft.Divider())
            controls.append(
                ft.Row(
                    [
                        add_order_dropdown,
                        add_desc_field,
                        add_uri_field,
                        add_uri_icon,
                        add_browse_btn,
                        ft.Button("Add button", on_click=on_add_button),
                    ],
                    spacing=8,
                )
            )
            buttons_section.controls = controls
            page.update()

        def open_edit_button_dialog(
            button_number_id: int, current_desc: str, current_uri: str
        ) -> None:
            edit_desc = track_focus(
                ft.TextField(
                    label="Description",
                    value=current_desc,
                    width=300,
                    fill_color=input_fill,
                )
            )
            edit_uri = ft.TextField(
                label="URI", value=current_uri, width=300, fill_color=input_fill
            )
            edit_uri_icon = ft.Icon(ft.Icons.HELP_OUTLINE)
            update_uri_status_icon(edit_uri_icon, current_uri, valid_paths)

            def on_edit_uri_change(_: ft.ControlEvent) -> None:
                update_uri_status_icon(
                    edit_uri_icon, edit_uri.value or "", valid_paths
                )
                page.update()

            def on_edit_uri_blur(_: ft.ControlEvent) -> None:
                update_uri_status_icon(
                    edit_uri_icon,
                    edit_uri.value or "",
                    valid_paths,
                    db_path=db_path,
                )
                page.update()

            edit_uri.on_change = on_edit_uri_change
            edit_uri.on_blur = on_edit_uri_blur
            track_focus(edit_uri)

            def on_edit_browse_pick(value: str) -> None:
                edit_uri.value = value
                update_uri_status_icon(
                    edit_uri_icon, value, valid_paths, db_path=db_path
                )
                page.update()

            edit_browse_btn = ft.Button(
                "Browse…",
                on_click=lambda _: show_content_picker(on_edit_browse_pick),
            )

            def save_edit(_: ft.ControlEvent) -> None:
                for i, (bid, _, _) in enumerate(buttons_state):
                    if bid == button_number_id:
                        buttons_state[i] = (
                            button_number_id,
                            edit_desc.value or "",
                            edit_uri.value or "",
                        )
                        break
                _capture(user_name, "tooltip_button_changed", {"action": "edit", "context": "insert"})
                page.pop_dialog()
                refresh_buttons_section()
                page.update()

            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text(f"Edit button (order {button_number_id})"),
                    content=ft.Column(
                        [
                            edit_desc,
                            ft.Row(
                                [edit_uri, edit_uri_icon, edit_browse_btn],
                                spacing=8,
                            ),
                        ],
                        tight=True,
                        spacing=8,
                    ),
                    actions=[
                        ft.TextButton("Cancel", on_click=lambda _: page.pop_dialog()),
                        ft.Button("Save", on_click=save_edit),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
            )
            page.update()

        def on_delete_button(button_number_id: int) -> None:
            buttons_state[:] = [
                b for b in buttons_state if b[0] != button_number_id
            ]
            _capture(user_name, "tooltip_button_changed", {"action": "remove", "context": "insert"})
            refresh_buttons_section()
            page.update()

        def on_save_add() -> None:
            cid_val = category_dropdown.value
            tag_val = (tag_field.value or "").strip()
            summary_val = (summary_field.value or "").strip()
            if not cid_val:
                _capture(user_name, "validation_error", {"error_type": "category_missing", "where": "tooltip_insert"})
                page.show_dialog(
                    ft.SnackBar(content=ft.Text("Please select a category."))
                )
                return
            if not tag_val:
                _capture(user_name, "validation_error", {"error_type": "tag_required", "where": "tooltip_insert"})
                page.show_dialog(ft.SnackBar(content=ft.Text("Tag is required.")))
                return
            if not summary_val:
                _capture(user_name, "validation_error", {"error_type": "summary_required", "where": "tooltip_insert"})
                page.show_dialog(
                    ft.SnackBar(content=ft.Text("Summary is required."))
                )
                return

            def commit_save() -> None:
                def work() -> None:
                    new_id = insert_tooltip(
                        db_path,
                        int(cid_val),
                        tag_val,
                        summary_val,
                        detail_field.value or "",
                    )
                    replace_tooltip_buttons(db_path, new_id, buttons_state)
                    category_name = get_category_name(db_path, int(cid_val))
                    if category_name is not None:
                        update_last_change(
                            db_path, "tooltips-" + category_name, user_name
                        )

                def on_done(result: object) -> None:
                    if isinstance(result, sqlite3.IntegrityError):
                        _capture(user_name, "validation_error", {"error_type": "tag_duplicate", "where": "tooltip_insert"})
                        page.show_dialog(
                            ft.SnackBar(
                                content=ft.Text("Tag already exists in this category.")
                            )
                        )
                        return
                    if isinstance(result, ValueError):
                        _capture(user_name, "validation_error", {"error_type": "button_uri_invalid", "where": "tooltip_insert"})
                        page.show_dialog(ft.SnackBar(content=ft.Text(str(result))))
                        return
                    if isinstance(result, BaseException):
                        raise result
                    _capture(user_name, "tooltip_saved", {
                        "mode": "insert",
                        "category_id": int(cid_val),
                        "button_count": len(buttons_state),
                    })
                    show_browse_page(
                        get_total_count(db_path, from_search_term),
                        restore_page=from_page,
                        restore_search_term=from_search_term,
                    )

                run_with_compacting_dialog(work, on_done)

            broken = find_broken_button_uris(db_path, buttons_state)
            if broken:
                show_broken_uris_warning(broken, commit_save)
                return
            commit_save()

        refresh_buttons_section()

        content = ft.Column(
            [
                ft.Row(
                    [cancel_btn, save_btn],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                ft.Container(
                    content=category_dropdown,
                    padding=ft.Padding.only(left=section_indent),
                ),
                ft.Container(
                    content=tag_field,
                    padding=ft.Padding.only(left=section_indent),
                ),
                ft.Container(
                    content=summary_field,
                    padding=ft.Padding.only(left=section_indent),
                ),
                ft.Container(
                    content=detail_field,
                    padding=ft.Padding.only(left=section_indent),
                ),
                ft.Container(
                    content=buttons_section,
                    padding=ft.Padding.only(left=section_indent),
                ),
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=8,
        )
        page.add(content)
        page.update()

    def show_content_tree_page(
        restore_path: list[str] | None = None,
        restore_selected_file: str | None = None,
    ) -> None:
        """Browse the Content table as a Finder-style column view, with size + reference counts."""
        _capture(user_name, "content_tree_opened")
        page.controls.clear()
        try:
            paths_with_sizes = get_paths_with_sizes(db_path)
            tree = build_content_tree(paths_with_sizes)
            ref_counts = get_button_reference_counts(db_path)
            content_types_map = get_content_types(db_path)
            all_button_rows = get_all_button_rows(db_path)
        except sqlite3.Error as e:
            page.add(
                ft.Text(f"Could not load content: {e}", color=ft.Colors.RED, selectable=True)
            )
            page.update()
            return

        # Index: stripped Content.path -> [(tooltip_id, category, tag), ...] (deduped per tooltip).
        ref_tooltips: dict[str, list[tuple[int, str, str]]] = {}
        for tid, cat, tag, _bid, _desc, uri in all_button_rows:
            stripped = _uri_path_for_content_lookup(uri or "")
            if not stripped:
                continue
            bucket = ref_tooltips.setdefault(stripped, [])
            if not any(existing_tid == tid for existing_tid, _, _ in bucket):
                bucket.append((tid, cat or "", tag or ""))

        # Reverse map for path -> (mime, compression) lookup. content_types_map is
        # {mime: (id, compression)} so id->(mime, compression) is what we want.
        ctid_to_mime: dict[int, tuple[str, str]] = {
            cid: (mime, comp) for mime, (cid, comp) in content_types_map.items()
        }

        def file_meta(full_path: str) -> tuple[str, str, int] | None:
            """Return (mime, compression, languageID) for a Content path, or None."""
            try:
                with sqlite3.connect(db_path) as conn:
                    cur = conn.execute(
                        'SELECT contentTypeID, languageID FROM "Content" WHERE path = ? LIMIT 1',
                        (full_path,),
                    )
                    row = cur.fetchone()
            except sqlite3.Error:
                return None
            if row is None:
                return None
            ctid, lang = row
            mime, comp = ctid_to_mime.get(ctid, ("?", "?"))
            return mime, comp, lang

        # Mutable nav state.
        nav: dict = {
            "path": list(restore_path) if restore_path else [],
            "selected_file": restore_selected_file,
            "sort_mode": "name",
        }

        def sorted_entries(node: dict) -> list[tuple[str, str, int, dict | None]]:
            """Return entries as (kind, name, size, child_node_or_None).

            kind is 'folder' or 'file'. For 'name' sort, folders come first then
            files, each alphabetic. For 'size' sort, all entries are mixed and
            ordered by size descending.
            """
            entries: list[tuple[str, str, int, dict | None]] = []
            for n, ch in node["children"].items():
                entries.append(("folder", n, ch["size"], ch))
            for n, sz in node["files"].items():
                entries.append(("file", n, sz, None))
            if nav["sort_mode"] == "size":
                entries.sort(key=lambda e: -e[2])
            else:
                folders = sorted(
                    [e for e in entries if e[0] == "folder"],
                    key=lambda e: e[1].lower(),
                )
                files = sorted(
                    [e for e in entries if e[0] == "file"],
                    key=lambda e: e[1].lower(),
                )
                entries = folders + files
            return entries

        columns_row = ft.Row(
            controls=[],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.START,
            auto_scroll=True,
        )

        def node_at(path_segments: list[str]) -> dict | None:
            node = tree
            for seg in path_segments:
                child = node["children"].get(seg)
                if child is None:
                    return None
                node = child
            return node

        def build_column(parent_path: list[str]) -> ft.Control:
            """Render one column showing the children of node at `parent_path`."""
            node = node_at(parent_path)
            if node is None:
                return ft.Container(width=260)
            depth = len(parent_path)
            selected_bg = ft.Colors.PRIMARY_CONTAINER
            selected_fg = ft.Colors.ON_PRIMARY_CONTAINER
            rows: list[ft.Control] = []

            for kind, name, size, child in sorted_entries(node):
                if kind == "folder":
                    assert child is not None
                    child_count = len(child["children"]) + len(child["files"])
                    is_selected = (
                        depth < len(nav["path"]) and nav["path"][depth] == name
                    )
                    label = ft.Text(
                        f"{name}/   {child_count} items · {format_bytes(size)}",
                        selectable=False,
                        weight=ft.FontWeight.BOLD,
                        color=selected_fg if is_selected else None,
                    )
                    rows.append(
                        ft.Container(
                            content=label,
                            padding=ft.Padding.symmetric(vertical=4, horizontal=8),
                            bgcolor=selected_bg if is_selected else None,
                            on_click=lambda e, n=name, d=depth: on_folder_click(n, d),
                            ink=True,
                        )
                    )
                else:
                    full_path = "/".join(parent_path + [name])
                    is_html = name.lower().endswith((".html", ".htm"))
                    refs = ref_counts.get(full_path, 0)
                    is_selected = (
                        nav["selected_file"] == full_path
                        and depth == len(nav["path"])
                    )
                    name_color = selected_fg if is_selected else None
                    secondary_color = (
                        selected_fg if is_selected else ft.Colors.GREY
                    )
                    row_children: list[ft.Control] = [
                        ft.Text(
                            name,
                            expand=True,
                            no_wrap=False,
                            selectable=False,
                            color=name_color,
                        ),
                        ft.Text(format_bytes(size), color=secondary_color),
                    ]
                    if is_html:
                        if refs == 0:
                            row_children.append(
                                ft.Text(
                                    "orphan",
                                    color=secondary_color,
                                    italic=True,
                                )
                            )
                        else:
                            row_children.append(
                                ft.Text(
                                    f"{refs} ref{'s' if refs != 1 else ''}",
                                    color=name_color,
                                )
                            )
                    row = ft.Row(row_children, spacing=8)
                    rows.append(
                        ft.Container(
                            content=row,
                            padding=ft.Padding.symmetric(vertical=4, horizontal=8),
                            bgcolor=selected_bg if is_selected else None,
                            on_click=lambda e, n=name, d=depth, p=full_path: on_file_click(
                                n, d, p
                            ),
                            ink=True,
                        )
                    )

            if not rows:
                rows.append(
                    ft.Container(
                        content=ft.Text("(empty)", color=ft.Colors.GREY, italic=True),
                        padding=ft.Padding.all(8),
                    )
                )

            return ft.Container(
                content=ft.Column(rows, scroll=ft.ScrollMode.AUTO, spacing=0, expand=True),
                width=300,
                border=ft.Border.only(right=ft.BorderSide(1, ft.Colors.OUTLINE)),
            )

        def build_detail_column() -> ft.Control:
            """Render the detail panel for nav['selected_file'] (a full path string)."""
            full_path = nav["selected_file"]
            assert full_path is not None
            size = 0
            # Walk to leaf to get size.
            parts = full_path.split("/")
            node = tree
            for seg in parts[:-1]:
                node = node["children"].get(seg, {"children": {}, "files": {}, "size": 0})
            size = node["files"].get(parts[-1], 0)
            meta = file_meta(full_path)
            mime, comp, lang = meta if meta else ("?", "?", 0)
            refs = ref_counts.get(full_path, 0)

            label_style = ft.TextStyle(weight=ft.FontWeight.BOLD)
            is_html_file = full_path.lower().endswith((".html", ".htm"))

            server_port = content_server.get("port") or CONTENT_SERVER_PORT
            browser_url = f"http://127.0.0.1:{server_port}/{urllib.parse.quote(full_path)}"

            def _open_in_browser(_: ft.ControlEvent) -> None:
                _capture(user_name, "content_path_opened_in_browser", {"mime_type": mime})
                webbrowser.open(browser_url)

            open_in_browser_btn = ft.Button(
                "Open in browser",
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE
                ),
                on_click=_open_in_browser,
            )
            controls: list[ft.Control] = [
                open_in_browser_btn,
                ft.Container(height=8),
                ft.Text("Path", style=label_style),
                ft.Text(full_path, selectable=True, no_wrap=False),
                ft.Container(height=8),
                ft.Text("MIME", style=label_style),
                ft.Text(mime, selectable=True),
                ft.Container(height=8),
                ft.Text("Compression", style=label_style),
                ft.Text(comp, selectable=True),
                ft.Container(height=8),
                ft.Text("Language ID", style=label_style),
                ft.Text(str(lang), selectable=True),
                ft.Container(height=8),
                ft.Text("Size", style=label_style),
                ft.Text(f"{format_bytes(size)} ({size} bytes)", selectable=True),
            ]
            if is_html_file:
                controls.extend(
                    [
                        ft.Container(height=8),
                        ft.Text("References", style=label_style),
                        ft.Text(
                            "orphan" if refs == 0 else f"{refs} TooltipButton{'s' if refs != 1 else ''}",
                            color=ft.Colors.GREY if refs == 0 else None,
                            italic=refs == 0,
                            selectable=True,
                        ),
                    ]
                )
            if is_html_file and refs > 0:
                controls.append(ft.Container(height=8))
                controls.append(ft.Text("Referenced by", style=label_style))
                tooltips_here = ref_tooltips.get(full_path, [])

                def _make_back_to_tree() -> "callable":
                    saved_path = list(nav["path"])
                    saved_file = nav["selected_file"]
                    return lambda: show_content_tree_page(
                        restore_path=saved_path,
                        restore_selected_file=saved_file,
                    )

                for tid, cat, tag in sorted(
                    tooltips_here, key=lambda x: (x[1].lower(), x[2].lower())
                ):
                    view_btn = ft.Button(
                        "View",
                        style=ft.ButtonStyle(
                            bgcolor=ft.Colors.GREEN, color=ft.Colors.WHITE
                        ),
                        on_click=lambda e, t=tid: show_view_page(
                            t, 1, None, on_back=_make_back_to_tree()
                        ),
                    )
                    edit_btn = ft.Button(
                        "Edit",
                        style=ft.ButtonStyle(
                            bgcolor=ft.Colors.YELLOW, color=ft.Colors.BLACK
                        ),
                        on_click=lambda e, t=tid: show_edit_page(
                            t, 1, None, on_back=_make_back_to_tree()
                        ),
                    )
                    controls.append(
                        ft.Row(
                            [
                                ft.Text(
                                    cat or "(no category)",
                                    color=ft.Colors.GREY,
                                    selectable=True,
                                ),
                                ft.Text(
                                    f"#{tid}",
                                    color=ft.Colors.GREY,
                                    selectable=True,
                                ),
                                ft.Text(
                                    tag or "(no tag)",
                                    expand=True,
                                    no_wrap=False,
                                    selectable=True,
                                ),
                                view_btn,
                                edit_btn,
                            ],
                            spacing=8,
                        )
                    )

            return ft.Container(
                content=ft.Column(
                    controls,
                    spacing=2,
                    scroll=ft.ScrollMode.AUTO,
                    expand=True,
                ),
                width=420,
                padding=ft.Padding.all(12),
                border=ft.Border.only(right=ft.BorderSide(1, ft.Colors.OUTLINE)),
            )

        sort_row = ft.Row(controls=[], spacing=8)

        def build_sort_button(mode: str, label: str) -> ft.Control:
            active = nav["sort_mode"] == mode
            return ft.Button(
                label,
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.BLUE_700 if active else ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    color=ft.Colors.WHITE if active else ft.Colors.ON_SURFACE,
                ),
                on_click=lambda _, m=mode: set_sort_mode(m),
            )

        def set_sort_mode(mode: str) -> None:
            if nav["sort_mode"] != mode:
                nav["sort_mode"] = mode
                rerender()

        def rerender() -> None:
            sort_row.controls.clear()
            sort_row.controls.append(ft.Text("Sort:", weight=ft.FontWeight.BOLD))
            sort_row.controls.append(build_sort_button("name", "Name"))
            sort_row.controls.append(build_sort_button("size", "Size (largest first)"))
            columns_row.controls.clear()
            # One column for root, plus one per drill-in level.
            for depth in range(len(nav["path"]) + 1):
                columns_row.controls.append(build_column(nav["path"][:depth]))
            if nav["selected_file"] is not None:
                columns_row.controls.append(build_detail_column())
            page.update()

        def on_folder_click(name: str, depth: int) -> None:
            nav["path"] = nav["path"][:depth] + [name]
            nav["selected_file"] = None
            rerender()

        def on_file_click(name: str, depth: int, full_path: str) -> None:
            nav["path"] = nav["path"][:depth]
            nav["selected_file"] = full_path
            rerender()

        back_btn = ft.Button(
            "Back",
            style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE, color=ft.Colors.BLACK),
            on_click=lambda _: show_browse_page(get_total_count(db_path)),
        )
        total_files = len(paths_with_sizes)
        total_bytes = tree["size"]
        header = ft.Text(
            f"{total_files} files · {format_bytes(total_bytes)} total",
            style=ft.TextStyle(weight=ft.FontWeight.BOLD, size=14),
            selectable=True,
        )

        page.add(
            ft.Column(
                [
                    ft.Row([back_btn, header], spacing=12),
                    sort_row,
                    ft.Container(content=columns_row, expand=True),
                ],
                expand=True,
                spacing=8,
            )
        )
        rerender()

    def show_validate_uris_page(restore_page: int | None = None) -> None:
        """Audit tooltips for problems (broken button URIs and empty summaries);
        render each problem tooltip with a Problem-type column and Edit link."""
        page.controls.clear()
        button_rows = get_all_button_rows(db_path)
        valid_paths = get_all_content_paths(db_path)
        broken = find_broken_button_rows(button_rows, valid_paths)
        empty_summary = find_empty_summary_tooltips(db_path)

        # One row per (tooltip, problem type). A tooltip with both an empty
        # summary and a broken URI appears once for each.
        seen_broken: set[int] = set()
        broken_tooltips: list[tuple[int, str]] = []
        for tid, _category, tag, _bid, _desc, _uri in broken:
            if tid in seen_broken:
                continue
            seen_broken.add(tid)
            broken_tooltips.append((tid, tag or ""))

        issues: list[tuple[int, str, str]] = (
            [(tid, tag, "Broken URI") for tid, tag in broken_tooltips]
            + [(tid, tag, "Empty summary") for tid, tag in empty_summary]
        )

        _capture(user_name, "validate_tooltips_opened", {
            "broken_uri_tooltips": len(broken_tooltips),
            "broken_buttons": len(broken),
            "empty_summary_tooltips": len(empty_summary),
            "total_buttons": len(button_rows),
        })

        back_btn = ft.Button(
            "Back",
            style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE, color=ft.Colors.BLACK),
            on_click=lambda _: show_browse_page(get_total_count(db_path)),
        )
        header = ft.Text(
            f"{len(issues)} issue{'s' if len(issues) != 1 else ''}: "
            f"{len(broken_tooltips)} tooltip"
            f"{'s' if len(broken_tooltips) != 1 else ''} with broken URIs "
            f"({len(broken)} of {len(button_rows)} buttons), "
            f"{len(empty_summary)} with empty summary.",
            style=ft.TextStyle(weight=ft.FontWeight.BOLD, size=14),
            selectable=True,
        )

        if not issues:
            page.add(
                ft.Column(
                    [
                        back_btn,
                        header,
                        ft.Text(
                            "All tooltips have a summary and every button URI "
                            "matches a Content.path.",
                            selectable=True,
                        ),
                    ],
                    spacing=8,
                )
            )
            page.update()
            return

        columns = [
            fdt.DataColumn2(label=ft.Text("ID"), fixed_width=72),
            fdt.DataColumn2(label=ft.Text("Tag")),
            fdt.DataColumn2(label=ft.Text("Problem"), fixed_width=160),
            fdt.DataColumn2(label=ft.Text("Edit"), fixed_width=120),
        ]

        total_pages_local = max(
            1, (len(issues) + UI_PAGE_SIZE - 1) // UI_PAGE_SIZE
        )
        initial_page = (
            max(1, min(restore_page, total_pages_local))
            if restore_page is not None
            else 1
        )
        validator_state = {"page": initial_page}
        table = fdt.DataTable2(
            columns=columns,
            rows=[],
            vertical_lines=ft.BorderSide(1, ft.Colors.OUTLINE),
            horizontal_lines=ft.BorderSide(1, ft.Colors.OUTLINE),
            heading_row_color=ft.Colors.BLUE,
            heading_text_style=ft.TextStyle(
                color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD
            ),
            expand=True,
        )
        pager_text = ft.Text("", selectable=True)
        prev_btn = ft.Button("Previous")
        next_btn = ft.Button("Next")

        def fill_table() -> None:
            offset = (validator_state["page"] - 1) * UI_PAGE_SIZE
            page_slice = issues[offset : offset + UI_PAGE_SIZE]
            table.rows.clear()
            for tid, tag, problem in page_slice:
                edit_btn = ft.Button(
                    "Edit",
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.YELLOW, color=ft.Colors.BLACK
                    ),
                    on_click=lambda e, t=tid, p=validator_state[
                        "page"
                    ]: show_edit_page(
                        t,
                        1,
                        None,
                        on_back=lambda: show_validate_uris_page(restore_page=p),
                    ),
                )
                table.rows.append(
                    ft.DataRow(
                        cells=[
                            ft.DataCell(ft.Text(str(tid), selectable=True)),
                            ft.DataCell(ft.Text(tag, selectable=True)),
                            ft.DataCell(ft.Text(problem, selectable=True)),
                            ft.DataCell(edit_btn),
                        ]
                    )
                )

        def update_pager() -> None:
            start = (
                (validator_state["page"] - 1) * UI_PAGE_SIZE + 1
                if issues
                else 0
            )
            end = min(validator_state["page"] * UI_PAGE_SIZE, len(issues))
            pager_text.value = (
                f"Page {validator_state['page']} of {total_pages_local}"
                f" — issues {start}–{end} of {len(issues)}"
            )
            prev_btn.disabled = validator_state["page"] <= 1
            next_btn.disabled = validator_state["page"] >= total_pages_local

        def go_prev(_: ft.ControlEvent) -> None:
            if validator_state["page"] > 1:
                validator_state["page"] -= 1
                fill_table()
                update_pager()
                page.update()

        def go_next(_: ft.ControlEvent) -> None:
            if validator_state["page"] < total_pages_local:
                validator_state["page"] += 1
                fill_table()
                update_pager()
                page.update()

        prev_btn.on_click = go_prev
        next_btn.on_click = go_next

        pager_row = ft.Row(
            [prev_btn, pager_text, next_btn],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )
        page.add(
            ft.Column(
                [
                    back_btn,
                    header,
                    ft.Container(content=table, expand=True),
                    pager_row,
                ],
                expand=True,
                spacing=8,
            )
        )
        fill_table()
        update_pager()
        page.update()

    def show_browse_page(
        initial_total_count: int,
        restore_page: int | None = None,
        restore_search_term: str | None = None,
    ) -> None:
        nonlocal current_page, total_pages
        total_count = initial_total_count
        total_pages = max(1, (total_count + UI_PAGE_SIZE - 1) // UI_PAGE_SIZE)
        current_page = restore_page if restore_page is not None else 1
        current_page = max(1, min(current_page, total_pages))
        page.controls.clear()

        search_input = track_focus(
            ft.TextField(
                label="Search (tag, summary, detail, category, URI)",
                hint_text="prefix; * is a wildcard",
                width=220,
                value=restore_search_term or "",
                on_submit=lambda e: run_search(e),
            )
        )

        def run_search(_: ft.ControlEvent) -> None:
            nonlocal current_page, total_count, total_pages
            term = (search_input.value or "").strip()
            search_term = term if term else None
            total_count = get_total_count(db_path, search_term)
            _capture(user_name, "search_executed", {
                "term_length": len(term),
                "results_count": total_count,
                "is_wildcard": "*" in term,
            })
            total_pages = max(1, (total_count + UI_PAGE_SIZE - 1) // UI_PAGE_SIZE)
            current_page = 1
            fill_table()
            update_pager()

        search_btn = ft.Button("Search", on_click=run_search)
        search_bar = ft.Row(controls=[search_input, search_btn], spacing=8)

        # Persists across pagination: tooltip ids the user has checked for bulk delete.
        selected_ids: set[int] = set()

        select_column = fdt.DataColumn2(label=ft.Text(""), fixed_width=44)
        id_column = fdt.DataColumn2(label=ft.Text("ID"), fixed_width=128)
        category_column = fdt.DataColumn2(label=ft.Text("Category"))
        actions_column = fdt.DataColumn2(
            label=ft.Text("Actions"), fixed_width=ACTIONS_COLUMN_WIDTH
        )
        tag_column = fdt.DataColumn2(label=ft.Text("Tag"), size=fdt.DataColumnSize.S)
        columns = [
            select_column,
            id_column,
            category_column,
            actions_column,
            tag_column,
            fdt.DataColumn2(label=ft.Text("Summary")),
            fdt.DataColumn2(label=ft.Text("Detail")),
        ]
        view_btn_style = ft.ButtonStyle(bgcolor=ft.Colors.GREEN, color=ft.Colors.WHITE)
        edit_btn_style = ft.ButtonStyle(
            bgcolor=ft.Colors.YELLOW, color=ft.Colors.BLACK
        )
        table = fdt.DataTable2(
            columns=columns,
            rows=[],
            vertical_lines=ft.BorderSide(1, ft.Colors.OUTLINE),
            horizontal_lines=ft.BorderSide(1, ft.Colors.OUTLINE),
            heading_row_color=ft.Colors.BLUE,
            heading_text_style=ft.TextStyle(color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
            expand=True,
        )

        def fill_table() -> None:
            category_column.fixed_width = category_column_width(db_path)
            term = (search_input.value or "").strip()
            search_term = term if term else None
            offset = (current_page - 1) * UI_PAGE_SIZE
            rows = get_page(db_path, UI_PAGE_SIZE, offset, search_term)
            table.rows.clear()
            for i, row in enumerate(rows):
                tooltip_id, category_val, tag_val, summary_val, detail_val = row

                def _on_check(e: ft.ControlEvent, tid: int = tooltip_id) -> None:
                    if e.control.value:
                        selected_ids.add(tid)
                    else:
                        selected_ids.discard(tid)
                    update_bulk_delete_button()

                check = ft.Checkbox(
                    value=tooltip_id in selected_ids,
                    on_change=_on_check,
                )
                select_cell = ft.DataCell(check)
                id_cell = ft.DataCell(
                    ft.Text(str(tooltip_id), selectable=True)
                )
                category_cell = ft.DataCell(
                    ft.Text(str(category_val or ""), no_wrap=False, selectable=True)
                )
                tag_cell = ft.DataCell(
                    ft.Text(str(tag_val or ""), no_wrap=False, selectable=True)
                )
                summary_cell = ft.DataCell(
                    ft.Text(str(summary_val or ""), no_wrap=False, selectable=True)
                )
                detail_cell = ft.DataCell(
                    ft.Text(str(detail_val or ""), no_wrap=False, selectable=True)
                )
                view_btn = ft.Button(
                    "View",
                    style=view_btn_style,
                    on_click=lambda e, id=tooltip_id: show_view_page(
                        id, current_page, (search_input.value or "").strip() or None
                    ),
                )
                edit_btn = ft.Button(
                    "Edit",
                    style=edit_btn_style,
                    on_click=lambda e, id=tooltip_id: show_edit_page(
                        id, current_page, (search_input.value or "").strip() or None
                    ),
                )
                actions_cell = ft.DataCell(
                    ft.Row([view_btn, edit_btn], spacing=8)
                )
                table.rows.append(
                    ft.DataRow(
                        color=ZEBRA_EVEN if i % 2 == 0 else ZEBRA_ODD,
                        cells=[
                            select_cell,
                            id_cell,
                            category_cell,
                            actions_cell,
                            tag_cell,
                            summary_cell,
                            detail_cell,
                        ],
                    )
                )
            page.update()

        def go_prev(_: ft.ControlEvent) -> None:
            nonlocal current_page
            if current_page > 1:
                current_page -= 1
                fill_table()
                update_pager()

        def go_next(_: ft.ControlEvent) -> None:
            nonlocal current_page
            if current_page < total_pages:
                current_page += 1
                fill_table()
                update_pager()

        pager_text = ft.Text(selectable=True)
        pager_row = ft.Row(controls=[ft.Text(""), pager_text, ft.Text("")])

        def update_pager() -> None:
            start = (current_page - 1) * UI_PAGE_SIZE + 1 if total_count > 0 else 0
            end = min(current_page * UI_PAGE_SIZE, total_count)
            pager_text.value = f"Page {current_page} of {total_pages}  —  rows {start}–{end} of {total_count}"
            prev_btn.disabled = current_page <= 1
            next_btn.disabled = current_page >= total_pages
            page.update()

        prev_btn = ft.Button("Previous", on_click=go_prev)
        next_btn = ft.Button("Next", on_click=go_next)
        pager_row.controls[0] = prev_btn
        pager_row.controls[2] = next_btn

        def open_add_many_dialog(mode: str = "insert") -> None:
            mode_label = "insert" if mode == "insert" else "update"

            async def download_template(_: ft.ControlEvent) -> None:
                page.pop_dialog()
                page.update()
                picker = ft.FilePicker()
                path = await picker.save_file(
                    dialog_title="Save CSV template",
                    file_name="tooltips_template.csv",
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["csv"],
                )
                if path:
                    with open(
                        path, "w", newline="", encoding="utf-8"
                    ) as f:
                        f.write(get_csv_template_content())
                    _capture(user_name, "csv_template_downloaded")
                    page.update()

            async def upload_csv(_: ft.ControlEvent) -> None:
                page.pop_dialog()
                page.update()
                picker = ft.FilePicker()
                files = await picker.pick_files(
                    dialog_title="Upload CSV file",
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["csv"],
                    allow_multiple=False,
                )
                if not files or not files[0].path:
                    return
                path = files[0].path
                try:
                    with open(path, newline="", encoding="utf-8", errors="strict") as f:
                        reader = csv.reader(f)
                        all_rows = list(reader)
                except (OSError, UnicodeDecodeError) as e:
                    msg = (
                        f"File is not valid UTF-8: {e}"
                        if isinstance(e, UnicodeDecodeError)
                        else str(e)
                    )
                    page.show_dialog(
                        ft.AlertDialog(
                            title=ft.Text("Upload failed"),
                            content=ft.Text(msg, selectable=True),
                            actions=[ft.TextButton("OK", on_click=lambda e: (page.pop_dialog(), page.update()))],
                        )
                    )
                    page.update()
                    return
                if not all_rows:
                    page.show_dialog(
                        ft.AlertDialog(
                            title=ft.Text("Upload failed"),
                            content=ft.Text("File is empty or has no header row.", selectable=True),
                            actions=[ft.TextButton("OK", on_click=lambda e: (page.pop_dialog(), page.update()))],
                        )
                    )
                    page.update()
                    return
                header = [c.strip() for c in all_rows[0]]
                if header != list(CSV_TEMPLATE_HEADERS):
                    page.show_dialog(
                        ft.AlertDialog(
                            title=ft.Text("Upload failed"),
                            content=ft.Text(
                                "Header row must match template. Expected: "
                                + ", ".join(CSV_TEMPLATE_HEADERS),
                                selectable=True,
                            ),
                            actions=[ft.TextButton("OK", on_click=lambda e: (page.pop_dialog(), page.update()))],
                        )
                    )
                    page.update()
                    return
                data_rows = all_rows[1:]
                categories = get_categories(db_path)
                category_name_to_id = {name: cid for cid, name in categories}
                allowed_button_ids = set(get_button_number_ids(db_path))
                validation_errors = validate_csv_upload(
                    db_path,
                    data_rows,
                    category_name_to_id,
                    allowed_button_ids,
                    mode=mode,
                )
                if validation_errors:
                    page.show_dialog(
                        ft.AlertDialog(
                            title=ft.Text("Upload failed"),
                            content=ft.Column(
                                [ft.Text(e, selectable=True) for e in validation_errors],
                                scroll=ft.ScrollMode.AUTO,
                                height=300,
                            ),
                            actions=[ft.TextButton("OK", on_click=lambda e: (page.pop_dialog(), page.update()))],
                        )
                    )
                    page.update()
                    return
                def work() -> tuple[int, int]:
                    counts = import_csv_rows(
                        db_path, data_rows, category_name_to_id, mode=mode
                    )
                    for category_name in {row[0].strip() for row in data_rows}:
                        update_last_change(
                            db_path,
                            "tooltips-" + category_name,
                            user_name,
                        )
                    return counts

                def on_done(result: object) -> None:
                    if isinstance(result, BaseException):
                        raise result
                    inserted, updated = result  # type: ignore[misc]
                    _capture(user_name, "csv_imported", {
                        "mode": mode,
                        "inserted_count": inserted,
                        "updated_count": updated,
                        "row_count": len(data_rows),
                    })
                    show_simple_dialog(
                        "Add many — done",
                        f"Inserted: {inserted}\nUpdated: {updated}",
                    )
                    show_browse_page(
                        get_total_count(db_path, (search_input.value or "").strip() or None),
                        restore_page=current_page,
                        restore_search_term=(search_input.value or "").strip() or None,
                    )

                run_with_compacting_dialog(work, on_done)

            if mode == "insert":
                title = "Add many — insert only"
                body = (
                    "Download a CSV template or upload a CSV file to add multiple tooltips.\n\n"
                    "Insert mode: rows whose (category, tag) already exist in the database are rejected."
                )
            else:
                title = "Add many — update existing"
                body = (
                    "Download a CSV template or upload a CSV file to add or replace multiple tooltips.\n\n"
                    "Update mode: rows whose (category, tag) already exist will REPLACE the existing tooltip "
                    "(summary, detail, and buttons). Rows for new (category, tag) pairs are inserted."
                )

            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text(title),
                    content=ft.Text(body, selectable=True),
                    actions=[
                        ft.TextButton(
                            "Cancel",
                            on_click=lambda e: (page.pop_dialog(), page.update()),
                        ),
                        ft.Button(
                            "Download CSV template",
                            on_click=download_template,
                        ),
                        ft.Button(
                            "Upload CSV file",
                            on_click=upload_csv,
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
            )
            page.update()

        orange_btn_style = ft.ButtonStyle(
            bgcolor=ft.Colors.ORANGE,
            color=ft.Colors.BLACK,
        )

        def show_simple_dialog(title: str, body: str) -> None:
            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text(title),
                    content=ft.Text(body, selectable=True),
                    actions=[
                        ft.TextButton(
                            "OK",
                            on_click=lambda e: (page.pop_dialog(), page.update()),
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
            )
            page.update()

        async def run_import_content_flow() -> None:
            picker = ft.FilePicker()
            folder_str = await picker.get_directory_path(
                dialog_title="Choose content folder",
            )
            if not folder_str:
                return

            folder = Path(folder_str)
            if not folder.is_dir():
                show_simple_dialog("Import failed", f"Not a directory: {folder}")
                return

            try:
                languages = get_languages(db_path)
            except sqlite3.Error as e:
                show_simple_dialog("Import failed", f"Could not read Languages: {e}")
                return
            if not languages:
                show_simple_dialog(
                    "Import failed",
                    "Languages table is empty; cannot import content.",
                )
                return

            doc_set = f"content-{folder.name}" if folder.name else "content"

            language_dd = ft.Dropdown(
                label="Language",
                options=[
                    ft.dropdown.Option(str(lid), lvalue) for lid, lvalue in languages
                ],
                value=str(languages[0][0]),
                width=240,
            )

            def open_settings_dialog() -> None:
                page.show_dialog(
                    ft.AlertDialog(
                        title=ft.Text("Import content folder"),
                        content=ft.Column(
                            [
                                ft.Text(f"Folder: {folder}", selectable=True),
                                ft.Text(
                                    f"DB paths will start with: {folder.name}/",
                                    selectable=True,
                                ),
                                ft.Text(
                                    f"Will record LastChange as: {doc_set}",
                                    selectable=True,
                                ),
                                language_dd,
                            ],
                            tight=True,
                            spacing=8,
                        ),
                        actions=[
                            ft.TextButton(
                                "Cancel",
                                on_click=lambda e: (
                                    page.pop_dialog(),
                                    page.update(),
                                ),
                            ),
                            ft.Button(
                                "Preview",
                                style=orange_btn_style,
                                on_click=lambda e: do_preview(),
                            ),
                        ],
                        actions_alignment=ft.MainAxisAlignment.END,
                    )
                )
                page.update()

            def do_preview() -> None:
                page.pop_dialog()
                page.update()
                try:
                    content_types = get_content_types(db_path)
                except sqlite3.Error as e:
                    show_simple_dialog("Preview failed", f"Could not read ContentTypes: {e}")
                    return
                if not content_types:
                    show_simple_dialog(
                        "Preview failed",
                        "ContentTypes table is empty; cannot import content.",
                    )
                    return
                try:
                    scan = prescan_content_import(db_path, folder, content_types)
                except (sqlite3.Error, OSError) as e:
                    show_simple_dialog("Preview failed", f"Scan error: {e}")
                    return
                show_preview_dialog(scan)

            def _format_counter(items: list[str], limit: int = 8) -> str:
                if not items:
                    return ""
                if len(items) <= limit:
                    return ", ".join(items)
                shown = ", ".join(items[:limit])
                return f"{shown}, … (+{len(items) - limit} more)"

            def show_path_list_dialog(title: str, paths: list[str]) -> None:
                """Open a stacked dialog with a virtualized ListView of paths.
                Used for the per-category 'View list' buttons in the preview."""
                rows = [
                    ft.Text(p, selectable=True, font_family="monospace", size=12)
                    for p in paths
                ]
                page.show_dialog(
                    ft.AlertDialog(
                        title=ft.Text(f"{title} ({len(paths)})"),
                        content=ft.Container(
                            content=ft.ListView(controls=rows, expand=True),
                            width=720,
                            height=480,
                        ),
                        actions=[
                            ft.TextButton(
                                "Close",
                                on_click=lambda e: (
                                    page.pop_dialog(),
                                    page.update(),
                                ),
                            ),
                        ],
                        actions_alignment=ft.MainAxisAlignment.END,
                    )
                )
                page.update()

            def _action_row(label: str, dialog_title: str, paths: list[str]) -> ft.Row:
                return ft.Row(
                    [
                        ft.Text(label),
                        ft.TextButton(
                            "View list",
                            on_click=lambda e, t=dialog_title, p=paths: show_path_list_dialog(t, p),
                        ),
                    ],
                    spacing=8,
                )

            def show_preview_dialog(scan: ScanResult) -> None:
                by_type: dict[str, int] = {}
                for item in scan.mapped:
                    by_type[item.mime] = by_type.get(item.mime, 0) + 1
                unmapped_exts: dict[str, int] = {}
                for src, _mime in scan.unmapped:
                    ext = src.suffix or "(no extension)"
                    unmapped_exts[ext] = unmapped_exts.get(ext, 0) + 1
                unmapped_summary = _format_counter(
                    [f"{ext} ({n})" for ext, n in sorted(unmapped_exts.items())]
                )

                overwrite_set = set(scan.overwrites)
                adds = sorted(
                    item.base_path
                    for item in scan.mapped
                    if item.base_path not in overwrite_set
                )

                controls: list[ft.Control] = []
                controls.append(ft.Text(f"Folder: {folder}", selectable=True))
                controls.append(
                    ft.Text(
                        f"Files found: {len(scan.mapped) + len(scan.unmapped)}"
                    )
                )
                controls.append(ft.Text(""))
                controls.append(ft.Text(f"Mapped (will import): {len(scan.mapped)}"))
                for mime, n in sorted(by_type.items()):
                    controls.append(ft.Text(f"  {mime}: {n}"))
                if adds:
                    controls.append(_action_row(f"Will add: {len(adds)}", "Will add", adds))
                if scan.overwrites:
                    controls.append(
                        _action_row(
                            f"Will overwrite: {len(scan.overwrites)}",
                            "Will overwrite",
                            list(scan.overwrites),
                        )
                    )
                if scan.orphans:
                    controls.append(
                        _action_row(
                            f"Will delete (not in folder): {len(scan.orphans)}",
                            "Will delete",
                            list(scan.orphans),
                        )
                    )
                if scan.unmapped:
                    controls.append(
                        ft.Text(
                            f"Unmapped (will skip): {len(scan.unmapped)} — {unmapped_summary}"
                        )
                    )
                if scan.skipped_symlinks:
                    controls.append(
                        ft.Text(f"Symlinks skipped: {len(scan.skipped_symlinks)}")
                    )
                if scan.skipped_hidden:
                    controls.append(
                        ft.Text(f"Hidden files skipped: {len(scan.skipped_hidden)}")
                    )
                if scan.skipped_bad_name:
                    sample = ", ".join(
                        str(p) for p, _ in scan.skipped_bad_name[:5]
                    )
                    controls.append(
                        ft.Text(
                            f"Disallowed filename chars (will skip): {len(scan.skipped_bad_name)} — {sample}"
                        )
                    )

                has_work = bool(scan.mapped or scan.orphans)
                actions: list[ft.Control] = [
                    ft.TextButton(
                        "Close" if not has_work else "Cancel",
                        on_click=lambda e: (page.pop_dialog(), page.update()),
                    ),
                ]
                if has_work:
                    actions.append(
                        ft.Button(
                            "Import",
                            style=orange_btn_style,
                            on_click=lambda e: do_import(scan),
                        )
                    )
                page.show_dialog(
                    ft.AlertDialog(
                        title=ft.Text("Preview"),
                        content=ft.Container(
                            content=ft.Column(
                                controls,
                                scroll=ft.ScrollMode.AUTO,
                                tight=True,
                            ),
                            width=640,
                            height=420,
                        ),
                        actions=actions,
                        actions_alignment=ft.MainAxisAlignment.END,
                    )
                )
                page.update()

            def do_import(scan: ScanResult) -> None:
                page.pop_dialog()
                page.update()
                try:
                    language_id = int(language_dd.value or languages[0][0])
                except (ValueError, TypeError):
                    language_id = languages[0][0]

                overwrite_set = set(scan.overwrites)
                overwrite_count = sum(
                    1 for item in scan.mapped if item.base_path in overwrite_set
                )
                add_count = len(scan.mapped) - overwrite_count
                delete_count = len(scan.orphans)

                delete_text = ft.Text(
                    f"Deleting orphans: 0 / {delete_count}"
                )
                delete_bar = ft.ProgressBar(value=0.0, width=420)
                overwrite_text = ft.Text(
                    f"Overwriting files: 0 / {overwrite_count}"
                )
                overwrite_bar = ft.ProgressBar(value=0.0, width=420)
                add_text = ft.Text(f"Adding files: 0 / {add_count}")
                add_bar = ft.ProgressBar(value=0.0, width=420)
                # Visible only once the helper enters its vacuum phase. Bar is
                # indeterminate because SQLite VACUUM emits no progress.
                vacuum_text = ft.Text("Compacting database…", visible=False)
                vacuum_bar = ft.ProgressBar(value=None, width=420, visible=False)

                progress_controls: list[ft.Control] = []
                if delete_count:
                    progress_controls.extend([delete_text, delete_bar])
                if overwrite_count:
                    progress_controls.extend([overwrite_text, overwrite_bar])
                if add_count:
                    progress_controls.extend([add_text, add_bar])
                if delete_count or overwrite_count:
                    progress_controls.extend([vacuum_text, vacuum_bar])

                page.show_dialog(
                    ft.AlertDialog(
                        modal=True,
                        title=ft.Text("Importing…"),
                        content=ft.Container(
                            content=ft.Column(
                                progress_controls,
                                tight=True,
                                spacing=8,
                            ),
                            width=480,
                        ),
                    )
                )
                page.update()

                last_update = [0.0]

                # Flet's send queue is an asyncio.Queue, which is NOT thread-safe.
                # Calling page.update / show_dialog / pop_dialog directly from the
                # worker thread races with the loop-side reader and stops flushing
                # after the first push. Instead the worker only mutates control
                # values (a plain attribute write), and any method that touches the
                # send queue is scheduled via page.run_task so it runs on the
                # event loop.

                async def _flush_update() -> None:
                    page.update()

                async def _finish_with_result(summary: ImportSummary) -> None:
                    _capture(user_name, "content_folder_imported", {
                        "files_imported": summary.files_imported,
                        "files_overwritten": summary.files_overwritten,
                        "orphans_deleted": summary.orphans_deleted,
                        "rows_inserted": summary.rows_inserted,
                        "files_skipped_error": summary.files_skipped_error,
                    })
                    page.pop_dialog()
                    page.update()
                    show_result_dialog(summary)

                async def _finish_with_error(message: str) -> None:
                    page.pop_dialog()
                    page.update()
                    show_simple_dialog("Import failed", message)

                def on_progress(phase: str, current: int, total: int) -> None:
                    if phase == "delete":
                        delete_text.value = f"Deleting orphans: {current} / {total}"
                        delete_bar.value = current / total
                    elif phase == "overwrite":
                        overwrite_text.value = (
                            f"Overwriting files: {current} / {total}"
                        )
                        overwrite_bar.value = current / total
                    elif phase == "add":
                        add_text.value = f"Adding files: {current} / {total}"
                        add_bar.value = current / total
                    elif phase == "vacuum":
                        # current==0 marks vacuum start (reveal the indeterminate
                        # bar); current==1 marks completion (hide it again so the
                        # final result dialog isn't preceded by a half-open row).
                        vacuum_text.visible = current == 0
                        vacuum_bar.visible = current == 0
                        page.run_task(_flush_update)
                        return

                    now = time.monotonic()
                    # Always flush the first and last tick of a phase so phase
                    # transitions are visible; otherwise throttle to ~10 Hz so we
                    # don't queue thousands of trivial UI updates.
                    if (
                        current == 1
                        or current == total
                        or now - last_update[0] >= 0.1
                    ):
                        last_update[0] = now
                        page.run_task(_flush_update)

                def run_import() -> None:
                    try:
                        summary = import_content_files(
                            db_path,
                            scan.mapped,
                            language_id=language_id,
                            user_name=user_name,
                            documentation_set=doc_set,
                            orphan_row_ids=scan.orphan_row_ids,
                            overwrite_row_ids_by_base=scan.overwrite_row_ids_by_base,
                            progress_callback=on_progress,
                        )
                    except Exception as e:
                        import traceback

                        traceback.print_exc()
                        page.run_task(
                            _finish_with_error, f"{type(e).__name__}: {e}"
                        )
                        return
                    page.run_task(_finish_with_result, summary)

                page.run_thread(run_import)

            def show_result_dialog(summary: ImportSummary) -> None:
                lines = [
                    f"Imported: {summary.files_imported} files "
                    f"({summary.rows_inserted} rows including chunks)",
                ]
                if summary.files_overwritten:
                    lines.append(
                        f"Files overwritten: {summary.files_overwritten}"
                    )
                if summary.orphans_deleted:
                    lines.append(
                        f"Orphans deleted: {summary.orphans_deleted}"
                    )
                if summary.imported_by_mime:
                    lines.append("")
                    lines.append("By content type:")
                    for mime, count in sorted(summary.imported_by_mime.items()):
                        lines.append(f"  {mime}: {count}")
                if summary.files_skipped_error:
                    lines.append(
                        f"Skipped (read error): {summary.files_skipped_error}"
                    )
                if summary.errors:
                    lines.append("")
                    lines.append("Errors:")
                    for src, msg in summary.errors[:10]:
                        lines.append(f"  {src.name}: {msg}")
                    if len(summary.errors) > 10:
                        lines.append(f"  … (+{len(summary.errors) - 10} more)")
                page.show_dialog(
                    ft.AlertDialog(
                        title=ft.Text("Import complete"),
                        content=ft.Container(
                            content=ft.Column(
                                [ft.Text("\n".join(lines), selectable=True)],
                                scroll=ft.ScrollMode.AUTO,
                                tight=True,
                            ),
                            width=600,
                            height=300,
                        ),
                        actions=[
                            ft.TextButton(
                                "OK",
                                on_click=lambda e: (
                                    page.pop_dialog(),
                                    page.update(),
                                ),
                            ),
                        ],
                        actions_alignment=ft.MainAxisAlignment.END,
                    )
                )
                page.update()

            open_settings_dialog()

        def open_add_chooser(_: ft.ControlEvent) -> None:
            def choose_add_one(_: ft.ControlEvent) -> None:
                page.pop_dialog()
                page.update()
                show_add_page(
                    current_page,
                    (search_input.value or "").strip() or None,
                )

            def choose_add_many_insert(_: ft.ControlEvent) -> None:
                page.pop_dialog()
                page.update()
                open_add_many_dialog(mode="insert")

            def choose_add_many_update(_: ft.ControlEvent) -> None:
                page.pop_dialog()
                page.update()
                open_add_many_dialog(mode="update")

            async def choose_import_content(_: ft.ControlEvent) -> None:
                page.pop_dialog()
                page.update()
                await run_import_content_flow()

            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text("Add"),
                    content=ft.Text("What would you like to add?"),
                    actions=[
                        ft.TextButton(
                            "Cancel",
                            on_click=lambda e: (page.pop_dialog(), page.update()),
                        ),
                        ft.Button(
                            "Add one tooltip",
                            style=orange_btn_style,
                            on_click=choose_add_one,
                        ),
                        ft.Button(
                            "Add many tooltips (insert only)",
                            style=orange_btn_style,
                            on_click=choose_add_many_insert,
                        ),
                        ft.Button(
                            "Add many tooltips (update existing)",
                            style=orange_btn_style,
                            on_click=choose_add_many_update,
                        ),
                        ft.Button(
                            "Import content folder",
                            style=orange_btn_style,
                            on_click=choose_import_content,
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
            )
            page.update()

        add_btn = ft.Button(
            "Add",
            style=orange_btn_style,
            on_click=open_add_chooser,
        )
        validate_btn = ft.Button(
            "Validate tooltips",
            style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE),
            on_click=lambda _: show_validate_uris_page(),
        )
        browse_content_btn = ft.Button(
            "Browse content",
            style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE),
            on_click=lambda _: show_content_tree_page(),
        )

        async def export_csv(_: ft.ControlEvent) -> None:
            # Export the current view: all tooltips when the search box is empty,
            # otherwise just the rows matching the active search.
            term = (search_input.value or "").strip()
            search_term = term if term else None
            rows = get_tooltip_export_rows(db_path, search_term)
            # Rows the importer would reject (e.g. empty summary) are excluded so
            # the file round-trips cleanly; report how many were left out.
            skipped = get_total_count(db_path, search_term) - len(rows)
            picker = ft.FilePicker()
            path = await picker.save_file(
                dialog_title="Export tooltips CSV",
                file_name="tooltips.csv",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["csv"],
            )
            if not path:
                return
            try:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(CSV_TEMPLATE_HEADERS)
                    writer.writerows(rows)
            except OSError as e:
                page.show_dialog(
                    ft.AlertDialog(
                        title=ft.Text("Export failed"),
                        content=ft.Text(str(e), selectable=True),
                        actions=[
                            ft.TextButton(
                                "OK",
                                on_click=lambda e: (page.pop_dialog(), page.update()),
                            )
                        ],
                    )
                )
                page.update()
                return
            _capture(user_name, "tooltips_exported", {
                "row_count": len(rows),
                "skipped_count": skipped,
                "filtered": search_term is not None,
            })
            message = f"Exported {len(rows)} tooltips to {path}"
            if skipped:
                message += (
                    f"\n\nSkipped {skipped} tooltip(s) that cannot be re-imported "
                    "(e.g. empty summary). Use \"Validate tooltips\" to find and fix them."
                )
            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text("Export complete"),
                    content=ft.Text(message, selectable=True),
                    actions=[
                        ft.TextButton(
                            "OK",
                            on_click=lambda e: (page.pop_dialog(), page.update()),
                        )
                    ],
                )
            )
            page.update()

        export_btn = ft.Button(
            "Export CSV",
            style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE),
            on_click=export_csv,
        )

        def confirm_bulk_delete(_: ft.ControlEvent) -> None:
            ids = sorted(selected_ids)
            if not ids:
                return

            def do_delete(_: ft.ControlEvent) -> None:
                page.pop_dialog()
                page.update()
                affected_categories = get_categories_for_tooltips(db_path, ids)

                def work() -> int:
                    deleted_local = delete_tooltips_bulk(db_path, ids)
                    for cat in affected_categories:
                        update_last_change(db_path, "tooltips-" + cat, user_name)
                    return deleted_local

                def on_done(result: object) -> None:
                    if isinstance(result, sqlite3.Error):
                        page.show_dialog(
                            ft.AlertDialog(
                                title=ft.Text("Bulk delete failed"),
                                content=ft.Text(str(result), selectable=True),
                                actions=[
                                    ft.TextButton(
                                        "OK",
                                        on_click=lambda e: (
                                            page.pop_dialog(),
                                            page.update(),
                                        ),
                                    )
                                ],
                            )
                        )
                        page.update()
                        return
                    if isinstance(result, BaseException):
                        raise result
                    deleted = int(result)
                    _capture(user_name, "tooltips_bulk_deleted", {
                        "count": deleted,
                        "category_count": len(affected_categories),
                    })
                    selected_ids.clear()
                    term = (search_input.value or "").strip()
                    show_browse_page(
                        get_total_count(db_path, term or None),
                        restore_page=current_page,
                        restore_search_term=term or None,
                    )
                    page.show_dialog(
                        ft.SnackBar(
                            content=ft.Text(
                                f"Deleted {deleted} tooltip" + ("s" if deleted != 1 else "")
                            )
                        )
                    )
                    page.update()

                run_with_compacting_dialog(work, on_done)

            page.show_dialog(
                ft.AlertDialog(
                    title=ft.Text("Bulk delete"),
                    content=ft.Text(
                        f"Permanently delete {len(ids)} tooltip"
                        f"{'s' if len(ids) != 1 else ''}? "
                        "This also removes their TooltipButtons rows and cannot be undone.",
                        selectable=True,
                    ),
                    actions=[
                        ft.TextButton(
                            "Cancel",
                            on_click=lambda e: (page.pop_dialog(), page.update()),
                        ),
                        ft.Button(
                            "Proceed",
                            style=ft.ButtonStyle(
                                bgcolor=ft.Colors.RED, color=ft.Colors.WHITE
                            ),
                            on_click=do_delete,
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
            )
            page.update()

        # Initial style is grey because the button starts disabled (no rows
        # selected). update_bulk_delete_button swaps to red once n > 0 so the
        # destructive affordance only shows red when it'll actually do something.
        bulk_delete_btn = ft.Button(
            "Bulk delete",
            style=ft.ButtonStyle(bgcolor=ft.Colors.GREY_400, color=ft.Colors.WHITE),
            on_click=confirm_bulk_delete,
            disabled=True,
        )

        def update_bulk_delete_button() -> None:
            n = len(selected_ids)
            bulk_delete_btn.content = (
                f"Bulk delete ({n})" if n else "Bulk delete"
            )
            bulk_delete_btn.disabled = n == 0
            bulk_delete_btn.style = ft.ButtonStyle(
                bgcolor=ft.Colors.RED if n else ft.Colors.GREY_400,
                color=ft.Colors.WHITE,
            )
            page.update()

        table_holder = ft.Container(content=table, expand=True)
        bottom_row = ft.Row(
            controls=[
                pager_row,
                add_btn,
                validate_btn,
                browse_content_btn,
                export_btn,
                bulk_delete_btn,
                search_bar,
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )
        pager_bar = ft.Container(
            content=bottom_row,
            padding=ft.Padding.only(top=8, bottom=8, left=8, right=8),
            expand=False,
        )
        page.add(
            ft.Column(
                [table_holder, pager_bar],
                expand=True,
            )
        )
        fill_table()
        update_pager()

    if error:
        page.add(ft.Text(f"Error: {error}", color=ft.Colors.RED, selectable=True))
        return
    show_browse_page(total_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Browse tooltips join with pagination.")
    parser.add_argument(
        "database",
        type=Path,
        nargs="?",
        default=Path("documentation.db"),
        help="Path to SQLite database (default: documentation.db)",
    )
    parser.add_argument(
        "--user",
        required=True,
        help="Your name for LastChange tracking when editing tooltips",
    )
    args = parser.parse_args()

    error: str | None = None
    total_count = 0
    if not args.database.exists():
        error = f"File not found: {args.database}"
    else:
        try:
            total_count = get_total_count(args.database)
        except sqlite3.Error as e:
            error = str(e)

    def make_main(page: ft.Page) -> None:
        main(page, args.database, total_count, error, user_name=args.user)

    ft.run(main=make_main)
