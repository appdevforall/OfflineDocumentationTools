#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "brotli",
#     "Pillow",
#     "scour",
#     "pytest",
# ]
# ///
"""
test_drop_ogv_videos.py

Covers drop_ogv_videos.py: the markup editing, the anchoring that keeps it off
things that merely look like references, the refusals that stop it deleting a
file something still links to, and one end-to-end run over a real SQLite
database.

The cases that matter most are the negative ones. This tool's failure mode is
not a crash but a page that quietly lost its video - either because a reference
it could not re-point was left dangling, or because the one <source> a <video>
had was dropped instead of rewritten. Both are asserted here.

Run it either way - as a plain script, needing nothing installed:

    uv run scripts/test_drop_ogv_videos.py

or under pytest, which needs the same packages on the path:

    uv run --with brotli --with Pillow --with scour --with pytest pytest scripts/test_drop_ogv_videos.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

import brotli

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drop_ogv_videos as drop  # noqa: E402


# --- the markup editing ------------------------------------------------------

THREE_SOURCES = (
    '<video controls="">\n'
    '<source src="media/anim_zoom.mp4" type="video/mp4"/>\n'
    '<source src="media/anim_zoom.webm" type="video/webm"/>\n'
    '<source src="media/anim_zoom.ogv" type="video/ogg"/>\n'
    '</video>'
)
NAMES = {"anim_zoom.ogv"}


def test_redundant_source_is_dropped_whole():
    """The WEBM is already listed, so the Ogg element goes - tag, attributes and
    the line it sat on - and the rest of the block is untouched."""
    new_text, dropped, rewritten, stranded = drop.rewrite_text(
        THREE_SOURCES, NAMES, drop.build_pattern(NAMES))
    assert (dropped, rewritten, stranded) == (1, 0, [])
    assert new_text == (
        '<video controls="">\n'
        '<source src="media/anim_zoom.mp4" type="video/mp4"/>\n'
        '<source src="media/anim_zoom.webm" type="video/webm"/>\n'
        '</video>'
    )
    # No blank line left where the element used to be.
    assert "\n\n" not in new_text


def test_lone_source_is_repointed_not_dropped():
    """A <video> whose only source is the Ogg has nothing to fall back on, so
    the element is kept and re-pointed - src AND the type that would otherwise
    tell a browser to skip a file it can actually play."""
    block = '<video>\n<source src="media/anim_zoom.ogv" type="video/ogg"/>\n</video>'
    new_text, dropped, rewritten, stranded = drop.rewrite_text(
        block, NAMES, drop.build_pattern(NAMES))
    assert (dropped, rewritten, stranded) == (0, 1, [])
    assert new_text == '<video>\n<source src="media/anim_zoom.webm" type="video/webm"/>\n</video>'


def test_source_without_a_type_attribute_keeps_none():
    """The ContactsAnim shape: sources carrying only src. Re-pointing must not
    invent a type attribute that was never there."""
    block = '<video>\n<source src="media/anim_zoom.ogv"/>\n</video>'
    new_text, _dropped, rewritten, _stranded = drop.rewrite_text(
        block, NAMES, drop.build_pattern(NAMES))
    assert rewritten == 1
    assert new_text == '<video>\n<source src="media/anim_zoom.webm"/>\n</video>'


def test_duplicate_ogv_sources_both_drop():
    """Both copies are judged against the sources present BEFORE any edit, so
    the second is not re-pointed at a WEBM the first had just introduced."""
    block = ('<video>\n<source src="a/anim_zoom.webm" type="video/webm"/>\n'
             '<source src="a/anim_zoom.ogv" type="video/ogg"/>\n'
             '<source src="a/anim_zoom.ogv" type="video/ogg"/>\n</video>')
    new_text, dropped, rewritten, stranded = drop.rewrite_text(
        block, NAMES, drop.build_pattern(NAMES))
    assert (dropped, rewritten, stranded) == (2, 0, [])
    assert ".ogv" not in new_text
    assert new_text.count(".webm") == 1


def test_other_videos_on_the_page_are_untouched():
    page = ("<p>intro</p>\n" + THREE_SOURCES + "\n"
            '<video><source src="other.mp4" type="video/mp4"/></video>\n<p>outro</p>')
    new_text, dropped, _rewritten, stranded = drop.rewrite_text(
        page, NAMES, drop.build_pattern(NAMES))
    assert (dropped, stranded) == (1, [])
    assert '<video><source src="other.mp4" type="video/mp4"/></video>' in new_text
    assert new_text.startswith("<p>intro</p>") and new_text.endswith("<p>outro</p>")


def test_reference_outside_a_video_is_reported_as_stranded():
    """A link this tool cannot re-point must be named, not silently left behind
    for the delete to dangle."""
    page = '<a href="media/anim_zoom.ogv">download</a>'
    new_text, dropped, rewritten, stranded = drop.rewrite_text(
        page, NAMES, drop.build_pattern(NAMES))
    assert (dropped, rewritten) == (0, 0)
    assert stranded == ["anim_zoom.ogv"]
    assert new_text == page  # nothing written when nothing could be re-pointed


def test_a_relative_reference_matches_however_it_is_written():
    for src in ("anim_zoom.ogv", "./media/anim_zoom.ogv", "/static/anim_zoom.ogv",
                "https://example.test/v/anim_zoom.ogv", "media/anim_zoom.ogv?v=2"):
        block = f'<video>\n<source src="{src}" type="video/ogg"/>\n</video>'
        _new, dropped, rewritten, stranded = drop.rewrite_text(
            block, NAMES, drop.build_pattern(NAMES))
        assert (dropped + rewritten, stranded) == (1, []), src


# --- the anchoring -----------------------------------------------------------

def test_pattern_ignores_ogv_that_is_not_a_filename():
    """The two shapes this database actually contains: "ogv" inside an anchor
    name (<a name="onbinddialogview">) and as a bare word in pdf.worker.mjs's
    extension table. Neither is a reference and neither may be counted as one -
    a false positive here would refuse the whole run as stranded."""
    pattern = drop.build_pattern(NAMES)
    for text in ('<a name="onbinddialogview"></a>', 'case "ogv":',
                 '<p>save it as anim_zoom.ogv</p>', '<source src="my_anim_zoom.ogv"/>',
                 '<source src="anim_zoom.ogv2"/>'):
        assert not pattern.search(text), text
    assert pattern.search('<source src="media/anim_zoom.ogv"/>')


def test_pages_that_only_look_like_they_reference_ogv_are_left_alone():
    page = '<a name="onbinddialogview"></a><p>ogv</p>'
    new_text, dropped, rewritten, stranded = drop.rewrite_text(
        page, NAMES, drop.build_pattern(NAMES))
    assert (new_text, dropped, rewritten, stranded) == (page, 0, 0, [])


# --- the database-level checks ----------------------------------------------

def _database(tmp_path: Path, pages: dict, videos: list) -> Path:
    """A miniature documentation database: the two tables this tool reads, one
    plain-Brotli text row per page, and one row per stored video."""
    db_path = tmp_path / "documentation.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE ContentTypes (id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE, "
        "compression TEXT NOT NULL);"
        "CREATE TABLE Content (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
        "languageID INTEGER NOT NULL, content BLOB NOT NULL, contentTypeID INTEGER NOT NULL, "
        "templateId INTEGER NOT NULL DEFAULT 0, UNIQUE(path));"
        "INSERT INTO ContentTypes (id, value, compression) VALUES "
        "(5, 'text/plain', 'none'), (12, 'text/html', 'brotli');"
    )
    for path, html in pages.items():
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, 12, 0)", (path, brotli.compress(html.encode("utf-8"))))
    for path in videos:
        conn.execute("INSERT INTO Content (path, languageID, content, contentTypeID, templateId) "
                     "VALUES (?, 1, ?, 5, 0)", (path, b"\x00binary video bytes"))
    conn.commit()
    conn.close()
    return db_path


def _page(conn, path: str) -> str:
    row = conn.execute("SELECT content FROM Content WHERE path = ?", (path,)).fetchone()
    return brotli.decompress(row[0]).decode("utf-8")


def _cfg(db_path: Path, **overrides) -> dict:
    cfg = {"db_path": db_path, "dry_run": False, "workers": 2, "verbose": False}
    cfg.update(overrides)
    return cfg


def test_end_to_end_drops_the_sources_and_the_files(tmp_path: Path):
    db_path = _database(
        tmp_path,
        {"a/zoom.html": "<html>\n" + THREE_SOURCES + "\n</html>"},
        ["media/anim_zoom.ogv", "media/anim_zoom.webm"],
    )
    assert drop.run(_cfg(db_path)) == 0

    conn = sqlite3.connect(db_path)
    try:
        paths = {row[0] for row in conn.execute("SELECT path FROM Content")}
        assert "media/anim_zoom.ogv" not in paths
        assert "media/anim_zoom.webm" in paths  # the replacement is not collateral
        html = _page(conn, "a/zoom.html")
        assert ".ogv" not in html
        assert html.count('<source src="media/anim_zoom.webm" type="video/webm"/>') == 1
        assert '<source src="media/anim_zoom.mp4" type="video/mp4"/>' in html
    finally:
        conn.close()


def test_dry_run_changes_nothing(tmp_path: Path):
    pages = {"a/zoom.html": "<html>\n" + THREE_SOURCES + "\n</html>"}
    db_path = _database(tmp_path, pages, ["media/anim_zoom.ogv", "media/anim_zoom.webm"])
    before = db_path.read_bytes()
    assert drop.run(_cfg(db_path, dry_run=True)) == 0
    assert db_path.read_bytes() == before
    # And no backup was taken, since nothing was at risk.
    assert not list(tmp_path.glob("*.backup-*"))


def test_an_ogv_with_no_webm_counterpart_refuses(tmp_path: Path):
    """Deleting it would leave its page with no video at all, so the run stops
    before touching anything."""
    db_path = _database(
        tmp_path,
        {"a/zoom.html": '<video>\n<source src="media/anim_zoom.ogv" type="video/ogg"/>\n</video>'},
        ["media/anim_zoom.ogv"],
    )
    assert drop.run(_cfg(db_path)) == 1
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT 1 FROM Content WHERE path = 'media/anim_zoom.ogv'").fetchone()
    finally:
        conn.close()


def test_a_reference_it_cannot_repoint_refuses(tmp_path: Path):
    """The whole run is refused, not just the page - the point of the check is
    that the video must not be deleted while anything still links to it."""
    db_path = _database(
        tmp_path,
        {"a/zoom.html": "<html>\n" + THREE_SOURCES + "\n</html>",
         "a/list.html": '<a href="media/anim_zoom.ogv">download</a>'},
        ["media/anim_zoom.ogv", "media/anim_zoom.webm"],
    )
    assert drop.run(_cfg(db_path)) == 1
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT 1 FROM Content WHERE path = 'media/anim_zoom.ogv'").fetchone()
        assert ".ogv" in _page(conn, "a/zoom.html")  # the good page is not edited either
    finally:
        conn.close()


def test_two_ogv_files_sharing_a_filename_refuse(tmp_path: Path):
    """Matching is on the basename, so a duplicate makes every reference to
    either one ambiguous."""
    db_path = _database(
        tmp_path, {},
        ["a/media/anim_zoom.ogv", "a/media/anim_zoom.webm",
         "b/media/anim_zoom.ogv", "b/media/anim_zoom.webm"],
    )
    assert drop.run(_cfg(db_path)) == 1


def test_a_database_with_no_ogv_is_a_no_op(tmp_path: Path):
    db_path = _database(tmp_path, {"a/zoom.html": "<p>no videos here</p>"}, ["media/anim_zoom.webm"])
    before = db_path.read_bytes()
    assert drop.run(_cfg(db_path)) == 0
    assert db_path.read_bytes() == before


def _main() -> int:
    """Runs the tests without pytest installed as a runner, so this file works
    the same way the tools beside it do (`uv run scripts/test_drop_ogv_videos.py`)."""
    failures = []
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        with tempfile.TemporaryDirectory() as directory:
            needs_tmp = "tmp_path" in function.__code__.co_varnames[:function.__code__.co_argcount]
            try:
                function(Path(directory)) if needs_tmp else function()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 - a failure is the result here
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                failures.append(f"{name} (line {frame.lineno}: {frame.line}): "
                                f"{type(exc).__name__}: {exc}")
                print(f"FAIL {name}", file=sys.stderr)
            else:
                print(f"ok   {name}")
    print()
    print(f"{len(failures)} failure(s)" if failures else "all tests passed")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
