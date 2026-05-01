"""Tests for the just-in-time HTML anchor lookup used by the URI Browse… picker
and the on-blur anchor validation in `update_uri_status_icon`."""

import sqlite3
import tempfile
from pathlib import Path

import brotli
import flet as ft

import docdb_studio

extract_html_anchors = docdb_studio.extract_html_anchors
get_html_anchors_for_path = docdb_studio.get_html_anchors_for_path
_split_chunk_path = docdb_studio._split_chunk_path
_uri_fragment = docdb_studio._uri_fragment
update_uri_status_icon = docdb_studio.update_uri_status_icon


# ---------- extract_html_anchors ----------


def test_extract_returns_empty_list_for_anchor_free_html() -> None:
    assert extract_html_anchors(b"<html><body><p>no anchors</p></body></html>") == []


def test_extract_picks_up_id_attributes() -> None:
    blob = b'<html><body><h1 id="intro">Intro</h1><p id="p1">x</p></body></html>'
    assert extract_html_anchors(blob) == ["intro", "p1"]


def test_extract_picks_up_legacy_a_name() -> None:
    blob = b'<html><body><a name="top"></a><p>hi</p></body></html>'
    assert extract_html_anchors(blob) == ["top"]


def test_extract_picks_up_mix_of_id_and_a_name() -> None:
    blob = (
        b'<html><body>'
        b'<h1 id="top">T</h1>'
        b'<a name="legacy"></a>'
        b'<div id="middle"></div>'
        b'</body></html>'
    )
    assert extract_html_anchors(blob) == ["top", "legacy", "middle"]


def test_extract_dedupes_repeated_anchors() -> None:
    blob = b'<html><body><div id="a"/><span id="a"/><a name="a"/></body></html>'
    assert extract_html_anchors(blob) == ["a"]


def test_extract_preserves_document_order() -> None:
    blob = (
        b'<html><body>'
        b'<h1 id="zebra">Z</h1>'
        b'<h2 id="alpha">A</h2>'
        b'<h3 id="middle">M</h3>'
        b'</body></html>'
    )
    assert extract_html_anchors(blob) == ["zebra", "alpha", "middle"]


def test_extract_handles_self_closing_tags() -> None:
    blob = b'<html><body><br id="b1"/><img id="img1" src="x.png"/></body></html>'
    assert extract_html_anchors(blob) == ["b1", "img1"]


def test_extract_handles_malformed_html_without_raising() -> None:
    blob = b'<html><body><div id="ok"><span id="closed-wrong'
    out = extract_html_anchors(blob)
    assert "ok" in out


def test_extract_ignores_a_tag_without_name_attribute() -> None:
    blob = b'<html><body><a href="x.html">link</a></body></html>'
    assert extract_html_anchors(blob) == []


# ---------- _split_chunk_path ----------


def test_split_chunk_path_recognizes_chunk_suffix() -> None:
    assert _split_chunk_path("foo.html-1") == ("foo.html", 1)
    assert _split_chunk_path("a/b/big.pdf-12") == ("a/b/big.pdf", 12)


def test_split_chunk_path_returns_none_for_zero_or_negative() -> None:
    assert _split_chunk_path("foo.html-0") is None


def test_split_chunk_path_returns_none_for_non_numeric_suffix() -> None:
    assert _split_chunk_path("foo.html-abc") is None
    assert _split_chunk_path("foo-bar.html") is None


def test_split_chunk_path_returns_none_for_path_without_dash() -> None:
    assert _split_chunk_path("simple.html") is None


# ---------- get_html_anchors_for_path (integration) ----------


def _make_db_with_html_content() -> Path:
    """Temp DB with Content + ContentTypes seeded for HTML and CSS."""
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
            INSERT INTO ContentTypes (id, value, compression) VALUES
                (1, 'text/html', 'brotli'),
                (2, 'text/css', 'brotli');
            """
        )
        conn.commit()
    return p


def _insert_content(
    db: Path, path: str, blob: bytes, content_type_id: int = 1
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            'INSERT INTO "Content" (path, languageID, content, contentTypeID) VALUES (?, 1, ?, ?)',
            (path, blob, content_type_id),
        )
        conn.commit()


def test_get_anchors_decompresses_brotli_html() -> None:
    db = _make_db_with_html_content()
    try:
        html = b'<html><body><h1 id="top">T</h1><p id="footer">f</p></body></html>'
        _insert_content(db, "docs/page.html", brotli.compress(html))
        assert get_html_anchors_for_path(db, "docs/page.html") == ["top", "footer"]
    finally:
        db.unlink(missing_ok=True)


def test_get_anchors_returns_empty_when_html_has_no_anchors() -> None:
    db = _make_db_with_html_content()
    try:
        empty = brotli.compress(b"<html><body><p>nothing here</p></body></html>")
        _insert_content(db, "docs/blank.html", empty)
        assert get_html_anchors_for_path(db, "docs/blank.html") == []
    finally:
        db.unlink(missing_ok=True)


def test_get_anchors_reassembles_chunked_html() -> None:
    db = _make_db_with_html_content()
    try:
        full_html = (
            b'<html><body><h1 id="top">Top</h1>'
            + b"<p>" + b"x" * 200 + b"</p>"
            + b'<h2 id="bottom">Bottom</h2></body></html>'
        )
        compressed = brotli.compress(full_html)
        half = len(compressed) // 2
        _insert_content(db, "docs/big.html", compressed[:half])
        _insert_content(db, "docs/big.html-1", compressed[half:])
        assert get_html_anchors_for_path(db, "docs/big.html") == ["top", "bottom"]
    finally:
        db.unlink(missing_ok=True)


def test_get_anchors_returns_empty_for_non_html_content_type() -> None:
    db = _make_db_with_html_content()
    try:
        _insert_content(
            db,
            "styles/main.css",
            brotli.compress(b'.rule { color: red; }  /* id="not-an-anchor" */'),
            content_type_id=2,
        )
        assert get_html_anchors_for_path(db, "styles/main.css") == []
    finally:
        db.unlink(missing_ok=True)


def test_get_anchors_returns_empty_for_unknown_path() -> None:
    db = _make_db_with_html_content()
    try:
        assert get_html_anchors_for_path(db, "nope.html") == []
    finally:
        db.unlink(missing_ok=True)


def test_get_anchors_returns_empty_when_no_content_table() -> None:
    fd, path = tempfile.mkstemp(suffix=".db")
    Path(path).unlink(missing_ok=True)
    p = Path(path)
    try:
        with sqlite3.connect(p) as conn:
            conn.execute("CREATE TABLE other (x INTEGER)")
            conn.commit()
        assert get_html_anchors_for_path(p, "anything") == []
    finally:
        p.unlink(missing_ok=True)


# ---------- _uri_fragment ----------


def test_uri_fragment_returns_empty_when_no_hash() -> None:
    assert _uri_fragment("docs/page.html") == ""


def test_uri_fragment_returns_empty_when_hash_at_end() -> None:
    assert _uri_fragment("docs/page.html#") == ""


def test_uri_fragment_returns_text_after_hash() -> None:
    assert _uri_fragment("docs/page.html#section-1") == "section-1"


def test_uri_fragment_handles_query_before_hash() -> None:
    assert _uri_fragment("docs/page.html?q=1#section-1") == "section-1"


# ---------- update_uri_status_icon (on-blur anchor validation) ----------


def _make_icon() -> ft.Icon:
    return ft.Icon(ft.Icons.HELP_OUTLINE)


def test_status_icon_empty_uri_shows_grey_help() -> None:
    icon = _make_icon()
    update_uri_status_icon(icon, "", set())
    assert icon.icon == ft.Icons.HELP_OUTLINE
    assert icon.color == ft.Colors.GREY


def test_status_icon_valid_path_no_fragment_shows_green() -> None:
    icon = _make_icon()
    update_uri_status_icon(icon, "docs/page.html", {"docs/page.html"})
    assert icon.icon == ft.Icons.CHECK
    assert icon.color == ft.Colors.GREEN


def test_status_icon_invalid_path_shows_red() -> None:
    icon = _make_icon()
    update_uri_status_icon(icon, "missing.html", {"docs/page.html"})
    assert icon.icon == ft.Icons.CLOSE
    assert icon.color == ft.Colors.RED


def test_status_icon_skips_anchor_check_when_db_path_omitted() -> None:
    """on_change path: cheap, only validates the path even if a fragment is present."""
    icon = _make_icon()
    update_uri_status_icon(
        icon, "docs/page.html#anywhere", {"docs/page.html"}
    )
    assert icon.icon == ft.Icons.CHECK
    assert icon.color == ft.Colors.GREEN


def test_status_icon_with_db_path_validates_existing_anchor_green() -> None:
    db = _make_db_with_html_content()
    try:
        html = b'<html><body><h1 id="install">i</h1></body></html>'
        _insert_content(db, "docs/page.html", brotli.compress(html))
        icon = _make_icon()
        update_uri_status_icon(
            icon,
            "docs/page.html#install",
            {"docs/page.html"},
            db_path=db,
        )
        assert icon.icon == ft.Icons.CHECK
        assert icon.color == ft.Colors.GREEN
        assert "anchor" in (icon.tooltip or "").lower()
    finally:
        db.unlink(missing_ok=True)


def test_status_icon_with_db_path_flags_missing_anchor_red() -> None:
    db = _make_db_with_html_content()
    try:
        html = b'<html><body><h1 id="install">i</h1></body></html>'
        _insert_content(db, "docs/page.html", brotli.compress(html))
        icon = _make_icon()
        update_uri_status_icon(
            icon,
            "docs/page.html#nonsense",
            {"docs/page.html"},
            db_path=db,
        )
        assert icon.icon == ft.Icons.CLOSE
        assert icon.color == ft.Colors.RED
        assert "nonsense" in (icon.tooltip or "")
    finally:
        db.unlink(missing_ok=True)


def test_status_icon_empty_fragment_treated_as_no_anchor() -> None:
    """A trailing '#' is valid (browsers scroll to top)."""
    db = _make_db_with_html_content()
    try:
        html = b"<html><body><p>no anchors</p></body></html>"
        _insert_content(db, "docs/page.html", brotli.compress(html))
        icon = _make_icon()
        update_uri_status_icon(
            icon,
            "docs/page.html#",
            {"docs/page.html"},
            db_path=db,
        )
        assert icon.icon == ft.Icons.CHECK
        assert icon.color == ft.Colors.GREEN
    finally:
        db.unlink(missing_ok=True)
