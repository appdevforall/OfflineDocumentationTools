"""Tests for uris_exist_in_content (tooltip button URI -> Content.path validation)."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

import docdb_studio

uris_exist_in_content = docdb_studio.uris_exist_in_content
find_broken_button_uris = docdb_studio.find_broken_button_uris
get_all_content_paths = docdb_studio.get_all_content_paths
find_broken_button_rows = docdb_studio.find_broken_button_rows
_uri_path_for_content_lookup = docdb_studio._uri_path_for_content_lookup
get_content_paths_count = docdb_studio.get_content_paths_count
get_content_paths_page = docdb_studio.get_content_paths_page
_build_like_pattern = docdb_studio._build_like_pattern
_search_params = docdb_studio._search_params


def _make_db_with_content() -> Path:
    """Create a temp DB with Content table (minimal schema for path lookups)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.execute(
            "CREATE TABLE Languages (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO Languages (id, value) VALUES (1, 'en')"
        )
        conn.execute(
            """CREATE TABLE ContentTypes (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL,
                compression TEXT NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO ContentTypes (id, value, compression) VALUES (1, 'text', 'none')"
        )
        conn.execute(
            """CREATE TABLE "Content" (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                languageID INTEGER NOT NULL,
                content BLOB NOT NULL,
                contentTypeID INTEGER NOT NULL
            )"""
        )
        conn.commit()
    return p


def test_uris_exist_in_content_empty_list_returns_empty_set() -> None:
    db = _make_db_with_content()
    try:
        assert uris_exist_in_content(db, []) == set()
    finally:
        db.unlink(missing_ok=True)


def test_uris_exist_in_content_unknown_path_returns_empty_set() -> None:
    db = _make_db_with_content()
    try:
        assert uris_exist_in_content(db, ["/foo"]) == set()
    finally:
        db.unlink(missing_ok=True)


def test_uris_exist_in_content_returns_matching_paths() -> None:
    db = _make_db_with_content()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO "Content" (path, languageID, content, contentTypeID)
                   VALUES ('/foo', 1, X'', 1)"""
            )
            conn.commit()
        assert uris_exist_in_content(db, ["/foo"]) == {"/foo"}
    finally:
        db.unlink(missing_ok=True)


def test_uris_exist_in_content_empty_string_not_valid() -> None:
    db = _make_db_with_content()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO "Content" (path, languageID, content, contentTypeID)
                   VALUES ('/foo', 1, X'', 1)"""
            )
            conn.commit()
        assert uris_exist_in_content(db, ["", "/foo"]) == {"/foo"}
    finally:
        db.unlink(missing_ok=True)


def test_uris_exist_in_content_strips_fragment_before_lookup() -> None:
    """URI with #anchor matches Content.path without fragment."""
    db = _make_db_with_content()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO "Content" (path, languageID, content, contentTypeID)
                   VALUES ('/doc/page', 1, X'', 1)"""
            )
            conn.commit()
        assert uris_exist_in_content(db, ["/doc/page#section"]) == {"/doc/page"}
    finally:
        db.unlink(missing_ok=True)


def test_uris_exist_in_content_strips_query_before_lookup() -> None:
    """URI with ?args matches Content.path without query."""
    db = _make_db_with_content()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO "Content" (path, languageID, content, contentTypeID)
                   VALUES ('/doc/page', 1, X'', 1)"""
            )
            conn.commit()
        assert uris_exist_in_content(db, ["/doc/page?q=1&x=2"]) == {"/doc/page"}
    finally:
        db.unlink(missing_ok=True)


def test_uris_exist_in_content_no_content_table_returns_empty_set() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    with sqlite3.connect(p) as conn:
        conn.execute("CREATE TABLE TooltipCategories (id INTEGER, category TEXT)")
        conn.commit()
    try:
        assert uris_exist_in_content(p, ["/any"]) == set()
    finally:
        p.unlink(missing_ok=True)


def test_get_all_content_paths_returns_full_set() -> None:
    db = _make_db_with_content()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO "Content" (path, languageID, content, contentTypeID)
                   VALUES ('/foo', 1, X'', 1)"""
            )
            conn.execute(
                """INSERT INTO "Content" (path, languageID, content, contentTypeID)
                   VALUES ('bar/baz', 1, X'', 1)"""
            )
            conn.commit()
        assert get_all_content_paths(db) == {"/foo", "bar/baz"}
    finally:
        db.unlink(missing_ok=True)


def test_get_all_content_paths_empty_db_returns_empty_set() -> None:
    db = _make_db_with_content()
    try:
        assert get_all_content_paths(db) == set()
    finally:
        db.unlink(missing_ok=True)


def test_find_broken_button_uris_returns_only_broken() -> None:
    db = _make_db_with_content()
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO "Content" (path, languageID, content, contentTypeID)
                   VALUES ('docs/intro.html', 1, X'', 1)"""
            )
            conn.commit()
        buttons = [
            (1, "Intro", "docs/intro.html"),  # exists → OK
            (2, "Missing", "docs/nope.html"),  # broken
            (3, "With anchor", "docs/intro.html#part2"),  # OK after stripping
            (4, "Empty", ""),  # empty → not flagged
        ]
        assert find_broken_button_uris(db, buttons) == [(2, "docs/nope.html")]
    finally:
        db.unlink(missing_ok=True)


def test_find_broken_button_uris_empty_input_returns_empty() -> None:
    db = _make_db_with_content()
    try:
        assert find_broken_button_uris(db, []) == []
        assert find_broken_button_uris(db, [(1, "x", ""), (2, "y", "  ")]) == []
    finally:
        db.unlink(missing_ok=True)


def test_find_broken_button_rows_filters_by_validity() -> None:
    rows = [
        (10, "ide", "tag-a", 1, "desc-a", "docs/intro.html"),
        (10, "ide", "tag-a", 2, "desc-b", "docs/missing.html"),
        (11, "java", "tag-b", 1, "desc-c", "docs/intro.html#section"),
        (12, "kotlin", "tag-c", 1, "desc-d", "docs/missing2.html?q=1"),
    ]
    valid_paths = {"docs/intro.html"}
    broken = find_broken_button_rows(rows, valid_paths)
    assert [r[5] for r in broken] == ["docs/missing.html", "docs/missing2.html?q=1"]


def test_find_broken_button_rows_empty_input_returns_empty() -> None:
    assert find_broken_button_rows([], set()) == []


def test_find_broken_button_rows_strips_query_and_fragment() -> None:
    rows = [
        (1, "c", "t", 1, "d", "page#anchor"),
        (1, "c", "t", 2, "d", "page?key=val"),
    ]
    assert find_broken_button_rows(rows, {"page"}) == []  # both match after stripping


def _make_db_with_paths(paths: list[str]) -> Path:
    """Helper for picker queries: seed Content with the given paths."""
    db = _make_db_with_content()
    with sqlite3.connect(db) as conn:
        for p in paths:
            conn.execute(
                """INSERT INTO "Content" (path, languageID, content, contentTypeID)
                   VALUES (?, 1, X'', 1)""",
                (p,),
            )
        conn.commit()
    return db


def test_get_content_paths_count_no_search() -> None:
    db = _make_db_with_paths(["a", "b/c", "b/d"])
    try:
        assert get_content_paths_count(db) == 3
    finally:
        db.unlink(missing_ok=True)


def test_get_content_paths_count_with_search() -> None:
    db = _make_db_with_paths(["a", "b/c", "b/d", "abx/y"])
    try:
        # Plain term is a startswith.
        assert get_content_paths_count(db, "b/") == 2
        assert get_content_paths_count(db, "ab") == 1  # only abx/y starts with "ab"
        assert get_content_paths_count(db, "x") == 0   # nothing starts with "x"
        assert get_content_paths_count(db, "missing") == 0
        # `*` acts as a wildcard — `*x` becomes substring search.
        assert get_content_paths_count(db, "*x") == 1  # abx/y contains x
    finally:
        db.unlink(missing_ok=True)


def test_get_content_paths_page_pagination_and_search() -> None:
    db = _make_db_with_paths([f"docs/p{i:03d}.html" for i in range(60)])
    try:
        page1 = get_content_paths_page(db, 25, 0)
        page2 = get_content_paths_page(db, 25, 25)
        assert len(page1) == 25
        assert len(page2) == 25
        assert page1[0] == "docs/p000.html"
        assert page2[0] == "docs/p025.html"
        # Startswith narrows the result.
        narrow = get_content_paths_page(db, 10, 0, "docs/p01")
        assert all(p.startswith("docs/p01") for p in narrow)
        assert len(narrow) == 10
        # Bare "p01" no longer matches (paths start with "docs/").
        assert get_content_paths_page(db, 10, 0, "p01") == []
        # `*` wildcard recovers substring search.
        narrow_substring = get_content_paths_page(db, 10, 0, "*p01")
        assert len(narrow_substring) == 10
        assert all("p01" in p for p in narrow_substring)
    finally:
        db.unlink(missing_ok=True)


def test_get_content_paths_search_anchored_to_start_per_todo() -> None:
    """The TODO example: searching `i/a` finds `i/apple` but NOT `a/tini/apple`."""
    db = _make_db_with_paths(["i/apple", "a/tini/apple"])
    try:
        assert get_content_paths_count(db, "i/a") == 1
        assert get_content_paths_page(db, 25, 0, "i/a") == ["i/apple"]
        # `*apple` finds both via substring fallback.
        assert get_content_paths_count(db, "*apple") == 2
    finally:
        db.unlink(missing_ok=True)


def test_get_content_paths_search_escapes_sql_wildcards() -> None:
    """Literal `%` and `_` in the search term are escaped, not treated as SQL wildcards."""
    db = _make_db_with_paths(["100%off", "1000off", "abc_def", "abcXdef"])
    try:
        # "100%" should match only the literal "100%off", not "1000off".
        assert get_content_paths_count(db, "100%") == 1
        assert get_content_paths_page(db, 10, 0, "100%") == ["100%off"]
        # "abc_" should match only "abc_def" (literal underscore), not "abcXdef".
        assert get_content_paths_count(db, "abc_") == 1
        assert get_content_paths_page(db, 10, 0, "abc_") == ["abc_def"]
    finally:
        db.unlink(missing_ok=True)


def test_build_like_pattern_translates_user_wildcard_and_escapes_meta() -> None:
    # Plain term: anchored-at-start by trailing %.
    assert _build_like_pattern("foo") == "foo%"
    # `*` becomes a SQL `%` wildcard inside the term.
    assert _build_like_pattern("*foo") == "%foo%"
    assert _build_like_pattern("foo*bar") == "foo%bar%"
    # SQL metacharacters typed literally are escaped.
    assert _build_like_pattern("100%") == "100\\%%"
    assert _build_like_pattern("a_b") == "a\\_b%"
    # Backslash is also escaped (so it doesn't accidentally escape the next char).
    assert _build_like_pattern("a\\b") == "a\\\\b%"


def test_search_params_uses_anchored_pattern() -> None:
    """Pin the tooltip-browse search semantics: tag/summary/detail/category are
    anchored ("starts with"), while button uri/description match as a substring
    ("contains"), since URLs are searched by an interior fragment. One param per
    placeholder in SEARCH_WHERE, in that order."""
    assert _search_params("foo") == ("foo%", "foo%", "foo%", "foo%", "%foo%", "%foo%")
    # With a leading wildcard every column is already a substring match. The
    # contains columns keep their extra leading '%' (harmless, still matches).
    assert _search_params("*foo") == ("%foo%", "%foo%", "%foo%", "%foo%", "%%foo%", "%%foo%")
